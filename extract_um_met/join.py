"""
Joining per-region extractions into one period's domain dataset.

Extracts each world region in a domain (writing per-region zarr intermediates),
then stitches them into a single dataset for the period: regions in the same longitude
column are concatenated along latitude, then the columns are concatenated along
longitude. Coordinates are normalised/deduplicated and 1-cell join seams filled.
The result is renamed to the zarr-ready schema (lat/lon/levels) and returned.

Ported from ``satellite_met_join_v2.py``.
"""

import datetime
import glob
import os
import shutil

import numpy as np
import xarray as xr
import dask

from .config import Config, resolve_config_value, store_stem
from .metadata import apply_cf_metadata
from .periods import month_days, period_tag
from .sources import get_source
from .regions import (
    build_domain_grid,
    drop_duplicate_coords,
)
from .grid import build_target_grid
from .extract import extract_region, log


COORD_DECIMALS = 6

# UM world regions laid out as they connect geographically. Regions in the same
# column sit on top of each other (join along latitude); columns sit side by side
# (join along longitude). Polar caps 1 and 14 are handled separately.
GLOBAL_REGION_GRID = [
    [2, 3, 4, 5],
    [6, 7, 8, 9],
    [10, 11, 12, 13],
]

# Variables carried through from iris that are not useful downstream and can
# break concatenation when they differ across files. forecast_period and
# forecast_reference_time are intentionally NOT dropped — they are kept as
# time-indexed provenance coordinates by the extraction step.
_DROP_VARIABLES = [
    "level_height_0",
    "sigma_0",
]


def normalize_coords(ds, decimals=COORD_DECIMALS):
    """Round lat/lon to avoid tiny cross-region floating-point mismatches."""
    return ds.assign_coords(
        latitude=np.round(ds.latitude.values.astype(np.float64), decimals).astype(np.float32),
        longitude=np.round(ds.longitude.values.astype(np.float64), decimals).astype(np.float32),
    )


def fill_join_seams(ds):
    """
    Fill 1-cell NaN seams introduced at region boundaries during stitching.

    ``interpolate_na`` treats its dim as a core dimension under dask, so that dim
    must sit in a single chunk. Rechunking once up front is not enough: each pass
    re-splits the *other* horizontal dim, so every pass rechunks the dim it is
    about to interpolate. time stays chunked throughout, which is what keeps this
    streaming a block at a time rather than pulling the whole month in.
    """
    ds = ds.chunk({"latitude": -1}).interpolate_na(dim="latitude", method="linear", limit=1)
    ds = ds.chunk({"longitude": -1}).interpolate_na(dim="longitude", method="linear", limit=1)
    ds = ds.chunk({"latitude": -1}).interpolate_na(dim="latitude", method="nearest", limit=1)
    ds = ds.chunk({"longitude": -1}).interpolate_na(dim="longitude", method="nearest", limit=1)
    return ds


def _preprocess_for_zarr(ds):
    """Rename to the zarr schema (lat/lon/levels) and drop unhelpful variables."""
    ds = ds.drop_vars([v for v in _DROP_VARIABLES if v in ds.variables], errors="ignore")

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


def _region_intermediate_path(scratch_dir, stem, date_tag, region_id):
    """Path of one region's zarr intermediate."""
    return os.path.join(scratch_dir, f"{stem}_Met_{date_tag}_{region_id}.zarr")


def _open_region(file_path, region_id):
    """Open one per-region zarr intermediate and clean its coordinates for stitching."""
    log(f"Opening region {region_id}: {file_path}")
    ds = xr.open_zarr(file_path)
    # Drop the encoding zarr attaches on read (chunking, compressors, serializer,
    # zarr format). It describes how the scratch intermediate happens to be stored,
    # which has nothing to do with the yearly store; left in place it breaks that
    # write whenever the two layouts or zarr formats disagree.
    for var in ds.variables.values():
        var.encoding = {}
    # Only cast what isn't already float32 — extraction writes float32, so this
    # is normally a no-op and skipping it keeps the graph clean.
    for v in ds.data_vars:
        if ds[v].dtype != "float32":
            ds[v] = ds[v].astype("float32")
    ds = normalize_coords(ds)
    ds = ds.sortby("longitude")
    ds = drop_duplicate_coords(ds, "longitude")
    ds = drop_duplicate_coords(ds, "latitude")
    return ds


def join_month(
    domain_key,
    year,
    month,
    cfg,
    target=None,
    use_interp=None,
    scratch_dir=None,
    extract_missing=True,
    reuse_existing=True,
    days=None,
    source=None,
    suffix=None,
):
    """
    Extract and join all regions for a domain/year/month (or a shorter period).

    Parameters
    ----------
    domain_key : str
        Domain key (e.g., 'SA').
    year : int
        Year (e.g., 2016).
    month : int or str
        Calendar month (1-12 int, or "01".."12").
    cfg : extract_um_met.config.Config
        Configuration object.
    target : tuple of np.ndarray, optional
        Precomputed ``(target_lat, target_lon)``. If None, built from the grid spec.
    use_interp : bool, optional
        Whether extraction interpolates onto the target grid. If None, inferred.
    scratch_dir : str, optional
        Directory for per-region intermediates. Defaults to ``{scratch_path}/files/``.
    extract_missing : bool, optional
        If True (default), run extraction for regions whose intermediate is absent.
    reuse_existing : bool, optional
        If True (default), skip extraction for regions whose intermediate exists.
    days : list of str, optional
        The ``YYYYMMDD`` day keys to join, from
        :func:`extract_um_met.periods.period_chunks`. Defaults to the whole month.
        Intermediates carry the period's tag (see
        :func:`extract_um_met.periods.period_tag`).
    suffix : str, optional
        Extra tag appended to the domain name in the per-region intermediate
        filenames, so a variant run reuses/cleans up only its own intermediates.
        See :func:`extract_um_met.config.store_stem`.

    Returns
    -------
    xarray.Dataset
        Domain dataset, renamed to the zarr schema (lat/lon/levels), ready to
        append to a store.
    """
    if isinstance(cfg, dict):
        cfg = Config(cfg)

    month_int = int(month)
    if days is None:
        days = month_days(year, month_int)
    date_tag = period_tag(year, month_int, days)
    domain_cfg = cfg.get_domain(domain_key)
    domain_name = domain_cfg["domain_name"]
    stem = store_stem(domain_name, suffix)
    regions = domain_cfg["world_regions_codes"]

    scratch_root = resolve_config_value(cfg.get("scratch_path", ""), cfg.data)
    if scratch_dir is None:
        scratch_dir = os.path.join(scratch_root, "files")
    
    try:
        os.makedirs(scratch_dir, exist_ok=True)
    except FileExistsError:
        pass
    

    if source is None:
        source = get_source(domain_cfg.get("data_type", "UM_Global"), cfg)
    mk = source.get_mk(year, month_int)

    # Resolve the target grid once for the whole period.
    if target is None:
        target_lat, target_lon, inferred_interp = build_target_grid(domain_cfg, cfg, mk=mk)
        target = (target_lat, target_lon)
        if use_interp is None:
            use_interp = inferred_interp
    elif use_interp is None:
        grid_mode = domain_cfg.get("grid", {}).get("mode", "footprint")
        use_interp = grid_mode != "native"

    # 1. Ensure each region has an intermediate file.
    for region_id in regions:
        region_file = _region_intermediate_path(scratch_dir, stem, date_tag, region_id)
        if reuse_existing and os.path.exists(region_file):
            log(f"region {region_id} intermediate already exists, reusing")
            continue
        if not extract_missing:
            raise FileNotFoundError(
                f"Missing region intermediate and extract_missing=False: {region_file}"
            )
        extract_region(
            domain_key,
            year,
            month_int,
            region_id,
            cfg,
            target=target,
            use_interp=use_interp,
            scratch_dir=scratch_dir,
            days=days,
            source=source,
            suffix=suffix,
        )

    # Confirm all region files now exist.
    for region_id in regions:
        if not os.path.exists(
            _region_intermediate_path(scratch_dir, stem, date_tag, region_id)
        ):
            raise FileNotFoundError(
                f"Missing intermediate for region {region_id} in {scratch_dir}"
            )

    # 2. Stitch regions into the domain grid.
    region_grid = build_domain_grid(GLOBAL_REGION_GRID, regions)
    log(f"region grid: {region_grid}")

    with dask.config.set(**{"array.slicing.split_large_chunks": True}), \
            xr.set_options(keep_attrs=True):
        lat_arrays = []
        for column in zip(*region_grid):
            lat_datasets = []
            for region_id in column:
                if region_id is None:
                    continue
                file_path = _region_intermediate_path(
                    scratch_dir, stem, date_tag, region_id
                )
                lat_datasets.append(_open_region(file_path, region_id))

            log(f"Merging column {column} along latitude")
            merged_col = xr.concat(
                lat_datasets,
                dim="latitude",
                join="inner",
                coords="minimal",
                compat="override",
            )
            merged_col = merged_col.sortby("latitude").drop_duplicates(dim="latitude")
            merged_col = merged_col.sortby(["latitude", "longitude"])
            lat_arrays.append(merged_col)

        log("Concatenating all columns along longitude")
        met = xr.concat(
            lat_arrays,
            dim="longitude",
            join="inner",
            coords="minimal",
            compat="override",
        )
        met = met.drop_duplicates(dim="longitude")
        met = met.sortby(["latitude", "longitude", "time"])
        log(f"Full domain assembled — shape: {dict(met.sizes)}")

        log("Filling potential 1-cell seams across region joins")
        met = fill_join_seams(met)
        met = met.assign_coords(
            latitude=met.latitude.astype(np.float32),
            longitude=met.longitude.astype(np.float32),
        )
        met = met.transpose("model_level_number", "latitude", "longitude", "time", ...)

        # Drop spurious dataset-level attrs inherited from the iris cubes (these
        # are per-variable properties that don't belong at dataset level).
        for attr in ("units", "standard_name", "STASH"):
            met.attrs.pop(attr, None)

        # Rename to the zarr-ready schema (lat/lon/levels).
        met = _preprocess_for_zarr(met)

        # Record the mean grid spacing (degrees) as attributes.
        lat_vals = np.asarray(met["lat"].values)
        lon_vals = np.asarray(met["lon"].values)
        if lat_vals.size > 1:
            met.attrs["delta_lat"] = float(np.abs(np.diff(lat_vals)).mean())
        if lon_vals.size > 1:
            met.attrs["delta_lon"] = float(np.abs(np.diff(lon_vals)).mean())

        # Stamp CF coordinate + global metadata (institution/references/... from
        # config; empty fields omitted).
        grid_mode = domain_cfg.get("grid", {}).get("mode", "footprint")
        met = apply_cf_metadata(
            met, cfg, mk=mk, grid_mode=grid_mode,
            domain_name=domain_name, year=year, use_interp=use_interp,
            source_name=source.name,
        )

        log(f"{date_tag} dataset ready: {dict(met.sizes)} "
            f"(delta_lat={met.attrs.get('delta_lat', 'n/a')}, "
            f"delta_lon={met.attrs.get('delta_lon', 'n/a')})")

    return met


def cleanup_region_intermediates(domain_name, date_tag, scratch_dir, suffix=None):
    """Delete the per-region zarr intermediates for one period's ``date_tag``."""
    stem = store_stem(domain_name, suffix)
    pattern = os.path.join(scratch_dir, f"{stem}_Met_{date_tag}_*.zarr")
    removed = 0
    for f in glob.glob(pattern):
        try:
            shutil.rmtree(f)
            removed += 1
        except OSError:
            pass
    log(f"cleaned up {removed} region intermediate(s) for {stem} {date_tag}")
    return removed
