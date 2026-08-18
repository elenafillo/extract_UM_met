"""
CF-convention metadata for extract_um_met output.

Single place that owns the coordinate and global attributes written into the
yearly zarr stores, so the output is self-describing (analysis-ready) and its
provenance is auditable. Organisation-specific fields (institution, references,
title, source, comment) are read from the ``metadata:`` block in config.yaml;
empty values are omitted rather than written as blank attributes.

Variable-level attributes (units, standard_name, …) are intentionally left as
carried through from iris — the extraction/join steps run with
``xr.set_options(keep_attrs=True)`` so those survive.
"""

import datetime


CONVENTIONS_DEFAULT = "CF-1.10"

# CF attributes for coordinate variables, applied where the coordinate exists.
CF_COORD_ATTRS = {
    "lat": {
        "standard_name": "latitude",
        "long_name": "latitude",
        "units": "degrees_north",
        "axis": "Y",
    },
    "lon": {
        "standard_name": "longitude",
        "long_name": "longitude",
        "units": "degrees_east",
        "axis": "X",
    },
    "time": {
        "standard_name": "time",
        "long_name": "time",
        "axis": "T",
    },
    "levels": {
        "positive": "up",
    },
    "forecast_period": {
        "standard_name": "forecast_period",
        "long_name": "time since the forecast reference time",
    },
    "forecast_reference_time": {
        "standard_name": "forecast_reference_time",
        "long_name": "model run (analysis) reference time",
    },
}

# For these coords, keep any iris-provided attrs (esp. units) and only fill gaps;
# for the rest, the CF attrs above are authoritative.
_FILL_ONLY = {"forecast_period", "forecast_reference_time"}


def _levels_description(levels, max_level_m=None):
    """
    Describe a level selection from the level numbers themselves.

    Which levels are kept is configurable (``level_stride``, ``model_levels``,
    ``max_level_m``), so reading the coordinate is the only way this description
    stays true to what the store actually holds.

    A ceiling has to be named explicitly: capped levels are still 1..n and would
    otherwise be described as "all n model levels", which is the mislabelling
    this function exists to prevent.
    """
    n = len(levels)
    contiguous = levels == list(range(1, n + 1))
    if max_level_m is None:
        if contiguous:
            return f"all {n} model levels"
        return f"{n} model levels subsampled from 1-{levels[-1]}"
    capped = f"capped at {max_level_m:g} m (the top level brackets the ceiling)"
    if contiguous:
        return f"the lowest {n} model levels, {capped}"
    return f"{n} model levels subsampled from 1-{levels[-1]}, {capped}"


def _clean(mapping):
    """Drop keys whose value is None or an empty/whitespace-only string."""
    out = {}
    for k, v in mapping.items():
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        out[k] = v
    return out


def apply_cf_metadata(ds, cfg, *, mk, grid_mode, domain_name, year, use_interp=True,
                      source_name=None, max_level_m=None):
    """
    Stamp CF coordinate and global attributes onto a monthly/period dataset.

    Coordinate attributes are set for whichever of lat/lon/time/levels/
    forecast_* are present. Global attributes combine config-provided fields (the
    ``metadata:`` block) with auto-generated defaults; empty config fields are
    omitted. Existing variable attributes carried through from iris are untouched.

    Parameters
    ----------
    ds : xarray.Dataset
        Dataset to annotate (coords already renamed to lat/lon/levels).
    cfg : extract_um_met.config.Config
        Configuration object (provides the metadata: block and author).
    mk : int
        UM Mk version the data came from.
    grid_mode : str
        Grid mode used ('footprint' | 'regular' | 'native').
    domain_name : str
        Domain name (e.g., 'INDIA').
    year : int
        Year (used for the auto title).
    use_interp : bool
        Whether fields were interpolated onto a target grid (affects the comment).
    max_level_m : float, optional
        The domain's level ceiling in metres, if it has one. Recorded as an
        attribute and named in the level description, which would otherwise
        describe a capped 1..n selection as every level the source has.

    Returns
    -------
    xarray.Dataset
        The same dataset, with attributes set (modified in place and returned).
    """
    # --- coordinate attributes ---
    for name, attrs in CF_COORD_ATTRS.items():
        if name not in ds.variables:
            continue
        existing = dict(ds[name].attrs)
        if name in _FILL_ONLY:
            ds[name].attrs = {**attrs, **existing}   # iris attrs win (keep real units)
        else:
            ds[name].attrs = {**existing, **attrs}   # CF attrs win

    levels_description = _levels_description(
        [int(v) for v in ds["levels"].values], max_level_m
    )
    ds["levels"].attrs["long_name"] = f"UM model level number: {levels_description}"

    # --- global attributes ---
    md = cfg.get("metadata", {}) or {}
    if not isinstance(md, dict):
        md = {}

    conventions = md.get("conventions") or CONVENTIONS_DEFAULT
    title = md.get("title") or f"UM meteorology, {domain_name} {year}"
    if mk is not None:
        source = md.get("source") or (
            f"Met Office Unified Model (UM) Mk{mk}, extracted from the NAME global met archive"
        )
    else:
        source = md.get("source") or (f"{source_name} met archive" if source_name else "met archive")

    # "horizontally" is load-bearing: u/v are interpolated onto the mass grid in
    # the horizontal only and stay on their native rho levels, while the mass
    # fields are on theta levels. Consumers have read the old wording as a claim
    # that the winds had also been de-staggered vertically.
    auto_comment = (
        "winds interpolated horizontally onto the mass/pressure grid (horizontal "
        "only: u/v remain on rho levels, no vertical de-staggering); retained "
        f"{levels_description}; grid_mode={grid_mode}"
    )
    if not use_interp:
        auto_comment += "; fields kept on the native UM grid (no spatial interpolation)"
    user_comment = (md.get("comment") or "").strip()
    comment = f"{auto_comment}. {user_comment}" if user_comment else auto_comment

    now = datetime.datetime.now().isoformat(timespec="seconds")
    history = f"{now}: extracted by extract_um_met"

    attrs = {
        "Conventions": conventions,
        "title": title,
        "institution": md.get("institution", ""),
        "source": source,
        "references": md.get("references", ""),
        "history": history,
        "comment": comment,
        "author": cfg.get("met_extract_author", ""),
        "grid_mode": grid_mode,
    }
    if mk is not None:
        attrs["mk_version"] = int(mk)
    if source_name:
        attrs["data_type"] = source_name
    if max_level_m is not None:
        attrs["max_level_m"] = float(max_level_m)
    ds.attrs.update(_clean(attrs))

    return ds


def to_zarr_schema(ds):
    """
    Rename coordinates to the zarr-ready schema and drop hybrid-height helpers.

    ``latitude``/``longitude``/``model_level_number`` → ``lat``/``lon``/``levels``;
    ``level_height``/``sigma`` (and their ``_0`` merge-suffixed variants) are
    dropped. Shared by the tiled (join) and non-tiled (single-file) paths so both
    produce identical output shapes.
    """
    drop = [v for v in ("level_height", "sigma", "level_height_0", "sigma_0")
            if v in ds.variables]
    if drop:
        ds = ds.drop_vars(drop, errors="ignore")

    rename = {}
    if "latitude" in ds.dims:
        rename["latitude"] = "lat"
    if "longitude" in ds.dims:
        rename["longitude"] = "lon"
    if "model_level_number" in ds.dims:
        rename["model_level_number"] = "levels"
    if rename:
        ds = ds.rename(rename)

    for dim in ("lat", "lon"):
        if dim in ds.dims and ds.get_index(dim).has_duplicates:
            ds = ds.drop_duplicates(dim)
    return ds


def add_delta_attrs(ds):
    """Record the mean grid spacing (degrees) as ``delta_lat``/``delta_lon``."""
    import numpy as np

    if "lat" in ds and ds["lat"].size > 1:
        ds.attrs["delta_lat"] = float(np.abs(np.diff(np.asarray(ds["lat"].values))).mean())
    if "lon" in ds and ds["lon"].size > 1:
        ds.attrs["delta_lon"] = float(np.abs(np.diff(np.asarray(ds["lon"].values))).mean())
    return ds
