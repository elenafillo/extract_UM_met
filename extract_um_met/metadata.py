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

# The second time axis a domain gets under ``flux_time_regrid: keep`` — the
# time-averaged fields left at their window midpoint, which is when they are
# actually valid, instead of being moved onto the instantaneous fields' steps.
# Named for the fields it carries rather than 'time_stress': the sensible heat
# flux rides it too, and that is a flux and not a stress.
FLUX_TIME_DIM = "time_flux"

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
    FLUX_TIME_DIM: {
        "standard_name": "time",
        "long_name": (
            "validity time of the time-averaged fields: the midpoint of each "
            "averaging window the archive supplies, which is not necessarily one "
            "per step of 'time'"
        ),
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


def _levels_description(levels, max_level_m=None, wind_vertical_regrid="snap"):
    """
    Describe a level selection from the level numbers themselves.

    Which levels are kept is configurable (``level_stride``, ``model_levels``,
    ``max_level_m``, ``wind_vertical_regrid``), so reading the coordinate is the
    only way this description stays true to what the store actually holds.

    Anything that trims the top of the ladder has to suppress the word "all":
    those levels are still numbered 1..n and would otherwise be described as
    "all n model levels", which is the mislabelling this function exists to
    prevent. A ceiling does that, and so does ``interpolate``, which cannot keep
    the source's topmost model level — there is no rho level above it to
    de-stagger from. A subsampled selection never claimed "all" in the first
    place, so it needs no adjusting.

    ``wind_vertical_regrid`` is ``None`` for a store with no winds, which trims
    nothing and so reads like ``snap`` here.
    """
    n = len(levels)
    if levels == list(range(1, n + 1)):
        whole = max_level_m is None and wind_vertical_regrid != "interpolate"
        described = f"all {n} model levels" if whole else f"the lowest {n} model levels"
    else:
        described = f"{n} model levels subsampled from 1-{levels[-1]}"
    if max_level_m is not None:
        described += f", capped at {max_level_m:g} m (the top level brackets the ceiling)"
    return described


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
                      source_name=None, max_level_m=None, wind_vertical_regrid="snap",
                      flux_time_regrid="snap", coord_decimals=None):
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
    wind_vertical_regrid : str, optional
        ``'snap'`` (default) or ``'interpolate'`` — see
        :func:`extract_um_met.config.resolve_wind_vertical_regrid`. Recorded as
        an attribute so a store can never be ambiguous about which it is, and
        branches the comment: the two produce fields of identical shape,
        coordinates and dtype, so the prose is all a reader has to go on. The
        companion ``wind_level_height_rho`` / ``wind_sigma_rho`` attributes are
        not passed here — they are stamped onto the region intermediates by
        ``extract.extract_region``, where the cubes' own ladder is still
        readable, and survive the join's concats into ``ds.attrs`` untouched.

        ``None`` for a store whose source has no ``wind_vars``: neither the
        attribute nor the sentence is written, because there are no winds for
        either to be about. The caller decides that, not this function — the
        store's variable set is the source's business.
    flux_time_regrid : str, optional
        ``'snap'`` (default), ``'interpolate'`` or ``'keep'`` — see
        :func:`extract_um_met.config.resolve_flux_time_regrid`. Recorded for the
        same reason as ``wind_vertical_regrid``: ``snap`` and ``interpolate``
        differ only in the averaged fields' values, so nothing about the store's
        shape says which it is. The per-variable ``averaging*`` attributes say
        the same thing field by field; this is the store-level statement the
        append guard reads.

        ``None`` where both averaged groups are empty, on the same reasoning: the
        mode still resolves to something (and below Mk10 resolves to ``keep``
        whatever the domain asked for), but a store with no fluxes in it must not
        claim a ``time_flux`` axis it does not have.
    coord_decimals : int, optional
        The decimal places lat/lon were rounded to before the float32 cast — see
        :func:`extract_um_met.config.resolve_coord_decimals`. Recorded so two
        stores that will not align can be told apart from their attributes
        rather than from the union axis and silent NaN fill that alignment
        produces. ``None`` on paths that do not round at all (the non-tiled
        single-file path), where recording a value would claim a convention the
        coordinates were never put on.

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
        [int(v) for v in ds["levels"].values], max_level_m, wind_vertical_regrid
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

    # The 'snap' wording has to say two things, because saying only the first is
    # what misled consumers before: u/v were never moved vertically, *and* the
    # rho level_height/sigma they arrived with were replaced by the theta values
    # at the same level index (combine_by_coords(compat="override") keeps the
    # mass fields' ladder). So the coordinate describes the mass fields and
    # overstates the height of the winds underneath it. 'interpolate' is the case
    # where level_height genuinely does apply to u/v, so the two must not share a
    # sentence.
    if wind_vertical_regrid is None:
        wind_note = ""
    elif wind_vertical_regrid == "interpolate":
        wind_note = (
            "winds interpolated horizontally onto the mass/pressure grid and "
            "de-staggered vertically onto the theta (mass) levels by a 2-point "
            "weighted mean of the bracketing rho levels, so level_height applies "
            "to u/v as well; the source's topmost model level is absent, having "
            "no rho level above it to de-stagger from"
        )
    else:
        wind_note = (
            "winds interpolated horizontally onto the mass/pressure grid, in the "
            "horizontal only: u/v keep their native rho-level values, but the rho "
            "level_height/sigma they came with have been replaced by the theta "
            "values at the same level index, so level_height describes the mass "
            "fields and overstates the height of u/v — their true heights are in "
            "wind_level_height_rho"
        )
    # Only worth a sentence when it is not the default: a store saying nothing
    # about its fluxes is a snapped one, which is what every store said before
    # the key existed. None is the store with no averaged fields at all, which
    # must not describe a timestamp convention for fields it does not carry.
    flux_note = {
        None: "",
        "snap": "",
        "interpolate": (
            "the time-averaged fields (fluxes/stresses) are interpolated in "
            "time onto the instantaneous steps, so they are centred on their "
            "timestamp rather than half a window late, and are no longer the "
            "mean of any single window"
        ),
        "keep": (
            f"the time-averaged fields (fluxes/stresses) are on their own "
            f"{FLUX_TIME_DIM} axis at the midpoint of their averaging window — "
            f"untouched means stamped at the time they are valid, one point per "
            f"window the archive supplies"
        ),
    }[flux_time_regrid]
    # Assembled from whichever clauses apply rather than concatenated, so a store
    # without winds or without fluxes reads as a sentence instead of opening on a
    # stray separator.
    notes = [note for note in (wind_note, flux_note) if note]
    notes.append(f"retained {levels_description}")
    notes.append(f"grid_mode={grid_mode}")
    if not use_interp:
        notes.append("fields kept on the native UM grid (no spatial interpolation)")
    auto_comment = "; ".join(notes)
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
        "wind_vertical_regrid": wind_vertical_regrid,
        "flux_time_regrid": flux_time_regrid,
    }
    if mk is not None:
        attrs["mk_version"] = int(mk)
    if source_name:
        attrs["data_type"] = source_name
    if max_level_m is not None:
        attrs["max_level_m"] = float(max_level_m)
    if coord_decimals is not None:
        attrs["coord_decimals"] = int(coord_decimals)
    ds.attrs.update(_clean(attrs))

    # update() can only add. The region intermediates carry their own
    # wind_vertical_regrid / flux_time_regrid stamps — extract.extract_region
    # writes them unconditionally, for the join's staleness guards to read — and
    # those survive the concat into ds.attrs. So a None mode has to *remove* the
    # inherited attribute rather than merely decline to write its own, or the
    # store would still describe fields it does not carry.
    for key, mode in (("wind_vertical_regrid", wind_vertical_regrid),
                      ("flux_time_regrid", flux_time_regrid)):
        if mode is None:
            ds.attrs.pop(key, None)

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
