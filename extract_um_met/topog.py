"""
Static ancillary topography (orography) extraction.

Unlike the met pipeline, an ancillary is a single static array per domain: one
native-grid file in, one NetCDF out. The field is landed on the *same* target
grid met extraction builds for the domain, so the two line up pixel for pixel —
the UM vertical coordinate is terrain-following
(``z = level_height + sigma * orog``), so a misaligned orography silently
corrupts every derived height.

Output is per **year**, matching the met stores: ``{domain}_topog_{year}.nc``
beside ``{domain}_Met_{year}.zarr``. The UM Global orography changes between Mk
blocks (see How_Tos/topography.md §3), and the Mk boundaries fall mid-year, so
keying the output by year rather than by Mk means no downstream consumer has to
reimplement the Mk calendar to pick the right file.

A domain whose grid mode is ``global`` is the exception: it skips the target
grid and writes the ancillary at its own resolution over the whole globe. That
file is domain-agnostic — a native-mode consumer subsets its own box out of it,
and a new native-mode domain needs no extraction at all. Footprint and regular
domains still need their own file, because their target grid is an interpolated
mesh that does not lie on the UM native ladder.
"""

import datetime
from collections import Counter
from pathlib import Path

import iris
import numpy as np
import xarray as xr

from .config import Config, resolve_config_value
from .grid import build_target_grid
from .join import COORD_DECIMALS
from .rotated import latlon_target_cube, latlon_regridder, rotated_pole_attrs
from .sources import get_source

# The met is normalised to [-180, 180]; the UM Global ancillary ships on 0..360.
# Coordinates on both are dyadic (multiples of 1/64 degree and finer), so an
# exact match is expected and this tolerance only guards float round-trips.
_GRID_TOLERANCE_DEG = 1e-4


def _load_single_cube(filepath, standard_name):
    """Load `filepath` and return its one cube matching `standard_name`."""
    cubes = iris.load(filepath)
    matches = [c for c in cubes if c.name() == standard_name]
    if len(matches) != 1:
        available = [c.name() for c in cubes]
        raise ValueError(
            f"expected exactly 1 '{standard_name}' cube in {filepath}, found "
            f"{len(matches)}. Cubes in file: {available}"
        )
    return matches[0]


def _canonical_mk(source, year):
    """
    The Mk block a whole year is treated as being on, or None for Mk-less sources.

    Majority by month count — the same rule ``run`` uses to snap a native-mode
    year to one grid, so the ancillary and the store agree on boundary years.

    ``Counter.most_common`` and not ``max(set(...), key=count)``: 2014 splits
    6-6 between Mk7 and Mk8 (2017 likewise between Mk9 and Mk10), and only
    ``most_common`` breaks that tie the way ``run`` does — toward the earlier
    block, because ``mks`` is built in month order.
    """
    mks = [source.get_mk(year, month) for month in range(1, 13)]
    return Counter(mks).most_common(1)[0][0]


def _resolve_topog_file(cfg, source_name, mk):
    """
    Path to the native topography file for this data type and Mk.

    Two shapes are supported: ``topog_by_mk`` (a directory plus one subdirectory
    per Mk, for sources whose orography changes across the record) and a single
    ``native_topog_file`` (for sources with one field for all time).
    """
    overrides = (cfg.get("ancillary_types", {}) or {}).get(source_name, {}) or {}

    topog_by_mk = overrides.get("topog_by_mk")
    if topog_by_mk:
        if mk not in topog_by_mk:
            raise ValueError(
                f"No ancillary_types.{source_name}.topog_by_mk entry for Mk{mk}. "
                f"Configured: {sorted(topog_by_mk)}"
            )
        directory = resolve_config_value(overrides.get("native_topog_directory"), cfg.data)
        if not directory:
            raise ValueError(
                f"ancillary_types.{source_name}.topog_by_mk is set but "
                "native_topog_directory is not"
            )
        return Path(directory) / topog_by_mk[mk] / "topog.pp"

    topog_file = resolve_config_value(overrides.get("native_topog_file"), cfg.data)
    if not topog_file:
        raise ValueError(
            f"No ancillary_types.{source_name}.native_topog_file or .topog_by_mk "
            "configured"
        )
    return Path(topog_file)


def _store_coord(values):
    """
    A coordinate in the met store's own float32 representation.

    ``join.normalize_coords`` rounds to ``COORD_DECIMALS`` before casting, and for
    ``|lon| < 16`` — where float32 resolves finer than 1e-6 — that lands one ULP
    off the true grid value. Reproducing it here rather than casting the exact
    value is what keeps this index *bit-identical* to the store's: xarray aligns
    on exact equality, so a one-ULP difference merges to a union axis and fills
    the mismatched columns with NaN instead of raising.
    """
    return np.round(values.astype(np.float64), COORD_DECIMALS).astype(np.float32)


def _normalised_orography(cube):
    """
    The orography cube as a lat/lon DataArray on [-180, 180].

    Same transform extract.py applies to the met. It wraps a global field at
    the dateline, so re-sort — extract.py does not, because it normalises one
    region at a time and each region is already contiguous.
    """
    orog = xr.DataArray(
        cube.data,
        dims=("lat", "lon"),
        coords={
            "lat": cube.coord("latitude").points,
            "lon": cube.coord("longitude").points,
        },
    )
    return orog.assign_coords(lon=((orog.lon + 180) % 360) - 180).sortby("lon")


def _orography_global(cube, source):
    """
    The ancillary as it ships: the whole globe at the UM's own resolution.

    Nothing is built, clipped or interpolated — the UM Global ancillary already
    *is* a global native-resolution field, so normalising longitude is the only
    transform. The result is domain-agnostic: a native-mode consumer subsets its
    own box straight out of it, and a new domain needs no extraction at all.
    """
    if source.grid_type != "regular":
        raise ValueError(
            f"grid mode 'global' needs a regular lat/lon source, but {source.name} "
            f"is {source.grid_type}. A rotated-pole ancillary only means anything "
            "on a domain target grid."
        )

    orog = _normalised_orography(cube)
    return orog.lat.values, orog.lon.values, orog.values


def _orography_on_target(cube, source, target_lat, target_lon, use_interp):
    """
    Put the native-grid orography cube onto the domain's target grid.

    Rotated-pole sources are regridded through :mod:`extract_um_met.rotated`, the
    same route their met takes. Regular lat/lon sources are normalised to
    [-180, 180] and then either interpolated (footprint/regular modes) or
    subset (native mode, where the ancillary is already on exactly the target
    grid).
    """
    if source.grid_type == "rotated_pole":
        target_cube = latlon_target_cube(target_lat, target_lon)
        regridded = latlon_regridder(cube, target_cube)(cube)
        return regridded.data, rotated_pole_attrs(cube)

    orog = _normalised_orography(cube)

    if use_interp:
        orog = orog.interp(lat=target_lat, lon=target_lon)
    else:
        # Native mode: the ancillary is on exactly this grid. Select with a
        # tolerance so a grid mismatch raises instead of silently snapping to a
        # neighbouring cell — a half-cell offset is undetectable downstream.
        orog = orog.sel(
            lat=target_lat, lon=target_lon,
            method="nearest", tolerance=_GRID_TOLERANCE_DEG,
        )

    return orog.values, {}


def _ancillary_global_attrs(cfg, domain_name, source_name, mk, year, topog_file, grid_comment):
    """Global attrs in the spirit of metadata.apply_cf_metadata, sized for a static field."""
    md = cfg.get("metadata", {}) or {}
    if not isinstance(md, dict):
        md = {}

    comment = f"UM ancillary surface_altitude (STASH m01s00i033), {grid_comment}"

    now = datetime.datetime.now().isoformat(timespec="seconds")
    attrs = {
        "Conventions": md.get("conventions") or "CF-1.10",
        "title": md.get("title") or f"UM orography, {domain_name} {year}",
        "institution": md.get("institution", ""),
        "references": md.get("references", ""),
        "source": str(topog_file),
        "history": f"{now}: extracted by extract_um_met.topog",
        "comment": comment,
        "author": cfg.get("met_extract_author", ""),
        "data_type": source_name,
    }
    if mk is not None:
        attrs["mk_version"] = int(mk)
    return {k: v for k, v in attrs.items() if v != "" and v is not None}


def extract_topog(domain_key, cfg, year):
    """
    Write a domain's orography for one year, on the same grid as its met store.

    Parameters
    ----------
    domain_key : str
        Resolved domain key (e.g. 'EUROPE'), as returned by
        ``Config.resolve_domain_name``. Callers going through the CLI resolve
        this once at the argument-parsing boundary, matching extract_region.
    cfg : extract_um_met.config.Config
    year : int
        Year to extract for. Selects the Mk block (majority month count) and
        names the output, so it pairs with ``{domain}_Met_{year}.zarr``.

    Returns
    -------
    pathlib.Path
        Path to the written NetCDF.
    """
    if isinstance(cfg, dict):
        cfg = Config(cfg)

    domain_cfg = cfg.get_domain(domain_key)
    domain_name = domain_cfg["domain_name"]
    source_name = domain_cfg.get("data_type", "UM_Global")
    source = get_source(source_name, cfg)

    mk = _canonical_mk(source, year)
    topog_file = _resolve_topog_file(cfg, source_name, mk)
    print(f"    Mk{mk} ancillary: {topog_file}")

    cube = _load_single_cube(topog_file, "surface_altitude")

    if domain_cfg.get("grid", {}).get("mode") == "global":
        target_lat, target_lon, data = _orography_global(cube, source)
        extra_attrs = {}
        grid_comment = "the full UM native global grid (no spatial interpolation)"
        print(f"    global native grid: {len(target_lat)} lat x {len(target_lon)} lon")
    else:
        # mk is what makes this the *same* grid the met run built for this year.
        target_lat, target_lon, use_interp = build_target_grid(domain_cfg, cfg, mk=mk)
        grid_comment = (
            "interpolated onto the domain's met target grid" if use_interp
            else "on the domain's met target grid, kept on the native UM grid "
                 "(no spatial interpolation)"
        )
        print(f"    target grid: {len(target_lat)} lat x {len(target_lon)} lon "
              f"({'interpolated' if use_interp else 'native subset'})")
        data, extra_attrs = _orography_on_target(cube, source, target_lat, target_lon, use_interp)

    da = xr.DataArray(
        np.asarray(data, dtype="float32"),
        dims=("lat", "lon"),
        coords={"lat": _store_coord(target_lat), "lon": _store_coord(target_lon)},
        name="surface_altitude",
        attrs={
            "units": "m",
            "standard_name": "surface_altitude",
            "long_name": "surface altitude",
            **extra_attrs,
        },
    )
    ds = da.to_dataset()
    ds["lat"].attrs = {"units": "degrees_north", "standard_name": "latitude"}
    ds["lon"].attrs = {"units": "degrees_east", "standard_name": "longitude"}
    ds.attrs.update(
        _ancillary_global_attrs(cfg, domain_name, source_name, mk, year, topog_file, grid_comment)
    )

    # Default beside the met stores, so a domain's orography sits next to the
    # yearly zarr it belongs with.
    save_root = cfg.get("ancillary_save_directory") or cfg.get("zarr_save_directory")
    out_path = Path(save_root) / domain_name / f"{domain_name}_topog_{year}.nc"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ds.to_netcdf(out_path)
    print(f"    range {float(da.min()):.1f} .. {float(da.max()):.1f} m")
    return out_path
