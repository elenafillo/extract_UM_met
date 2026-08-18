"""
Per-region extraction of UM meteorology data.

Extracts data from UM .pp files for a single world region, aligns winds onto the
mass grid, applies the target grid (footprint/regular = interpolate, native =
passthrough), slices to the region's bounds, and writes a per-region zarr
intermediate to scratch. The join step later stitches these intermediates into a
full domain.

Ported from ``satellite_met_1b1_fixed_v3.py`` (per-region body).
"""

import datetime
import os
import warnings
from fnmatch import fnmatch

import iris
import numpy as np
import xarray as xr
import dask

from .config import Config, resolve_config_value, store_stem
from .iris_io import load_files, delete_iris
from .grid import build_target_grid
from .periods import month_days, period_tag
from .sources import get_source
from .metadata import to_zarr_schema, add_delta_attrs, apply_cf_metadata
from .rotated import (
    latlon_target_cube,
    latlon_regridder,
    rotate_winds_true_north,
    rotated_pole_attrs,
)


# Variable groups (which cubes to extract and how each is gridded) now live on
# the MetSource — see extract_um_met.sources. They are selected by (name,
# want_levels) via _pick. Time-averaged fields (fluxes/stresses) are means over
# the source's averaging window, stamped at the interval MIDPOINT from a different
# forecast run; we re-stamp them to the interval END to align with the
# instantaneous fields (see _restamp_to_interval_end, which also describes the
# window in the fields' `averaging*` attributes).


def _has_levels(cube):
    """True if the cube carries a model_level_number coordinate."""
    return "model_level_number" in {co.name() for co in cube.coords()}


def _pick(cubes, name, want_levels):
    """
    Select the single cube matching ``name`` and level-dimension presence.

    Selecting by name + level presence (rather than positional index) makes the
    variable choice explicit and fails loudly if the archive's cube set changes,
    instead of silently returning the wrong array.

    Raises
    ------
    ValueError
        If zero or more than one cube matches.
    """
    matches = [c for c in cubes if c.name() == name and _has_levels(c) == want_levels]
    if len(matches) != 1:
        available = [(c.name(), _has_levels(c)) for c in cubes]
        raise ValueError(
            f"expected exactly 1 cube for name={name!r} want_levels={want_levels}, "
            f"found {len(matches)}. Loaded cubes (name, has_levels): {available}"
        )
    return matches[0]


def _averaging_attrs(time_coord):
    """
    Describe the averaging window of a time-averaged cube, from its time bounds.

    Two things a consumer needs and cannot get from ``cell_methods``, whose
    ``interval:`` is the UM's *sampling* interval inside the window rather than
    the window itself: how long the window is, and where the timestamp sits on
    it once :func:`_restamp_to_interval_end` has moved it to the interval end.

    ``time`` is still on the (forecast_period, forecast_reference_time) grid here
    — :func:`_build_time_axis` collapses it later — so ``bounds`` is 3-D and
    ``bounds[0]`` is a whole row of cells, not one window. Flatten to one row per
    cell first; measuring the row gave 6 h for a 3-hourly field.
    """
    bounds = np.sort(time_coord.bounds.reshape(-1, 2), axis=1)
    widths = np.unique(bounds[:, 1] - bounds[:, 0])
    if widths.size != 1:
        raise ValueError(
            f"time-averaged field has {widths.size} different averaging windows "
            f"in one period ({widths} in {time_coord.units}); one 'averaging' "
            f"attribute cannot describe them all."
        )
    to_date = time_coord.units.num2date
    hours = (to_date(bounds[0, 1]) - to_date(bounds[0, 0])).total_seconds() / 3600
    return {
        "averaging": (
            f"{hours:g}-hour time-mean over [timestamp - {hours:g} h, timestamp], "
            f"stamped at the interval END (re-stamped from the UM's interval-midpoint "
            f"convention) so it shares the instantaneous fields' time axis. Read as an "
            f"instantaneous value its validity time is the window midpoint, "
            f"{hours / 2:g} h before the timestamp. The interval in cell_methods is the "
            f"UM's sampling interval within the window, not the window itself."
        ),
        "averaging_period_hours": hours,
        "averaging_midpoint_offset_hours": -hours / 2,
    }


def _restamp_to_interval_end(cube):
    """
    Re-stamp a time-averaged cube's time onto its interval END, dropping bounds.

    Averaged fields (fluxes/stresses) are stored at the interval midpoint with
    time bounds [start, end]; the end equals the instantaneous fields' valid time.
    Moving the point to the end lets them share one time axis. Modifies and
    returns the cube, along with the ``averaging*`` attributes describing the
    window — read off the bounds here, the last point at which they exist.
    """
    tc = cube.coord("time")
    averaging = {}
    if tc.has_bounds():
        averaging = _averaging_attrs(tc)
        tc.points = tc.bounds.max(axis=-1)   # interval end (robust to bound order)
        tc.bounds = None
    if cube.coords("forecast_period") and cube.coord("forecast_period").has_bounds():
        cube.coord("forecast_period").bounds = None
    return cube, averaging


def _build_time_axis(ds, keep_provenance):
    """
    Put a dataset/dataarray onto a real ``time`` dimension using its ``time`` aux
    coordinate, collapsing whatever forecast dims that coord spans.

    The instantaneous fields arrive on a (forecast_period, forecast_reference_time)
    grid with ``time`` as an aux coord; the averaged fields may span only one of
    those. Stacking exactly the dims ``time`` depends on handles both.

    Parameters
    ----------
    ds : xarray.Dataset or DataArray
    keep_provenance : bool
        If True, retain forecast_period/forecast_reference_time as time-indexed
        coords (used for the instantaneous met); if False, drop them (the averaged
        fields' forecast bookkeeping differs and is not meaningful once merged).
    """
    if "time" in ds.dims:
        return ds

    time_dims = list(ds["time"].dims)
    if not time_dims:                     # scalar time → length-1 time dim
        return ds.expand_dims("time")

    ds = ds.stack(newtime=time_dims).swap_dims({"newtime": "time"})
    if keep_provenance:
        fp = ds["forecast_period"].values
        frt = ds["forecast_reference_time"].values
        ds = ds.drop_vars(["forecast_period", "forecast_reference_time", "newtime"])
        ds = ds.assign_coords(
            forecast_period=("time", fp),
            forecast_reference_time=("time", frt),
        )
    else:
        ds = ds.drop_vars(
            ["forecast_period", "forecast_reference_time", "newtime"], errors="ignore"
        )
    return ds.sortby("time")


def _check_unique_time(ds, region_id, date):
    """Fail loudly if the built time axis has duplicate timestamps."""
    tv = ds["time"].values
    n_dup = int(len(tv) - len(np.unique(tv)))
    if n_dup:
        raise ValueError(
            f"Region {region_id} {date}: {n_dup} duplicate timestamp(s) out of "
            f"{len(tv)} after building the time axis. Some valid times arise from "
            f"more than one (forecast_reference_time, forecast_period) combination, "
            f"so those coordinates cannot be retained unambiguously. Inspect the "
            f"source files for overlapping forecast periods / reference times."
        )


# Coordinates that describe a single axis, mapped to the dimension they must never
# acquire.
_ONE_AXIS_COORDS = {
    "level_height": "time",
    "sigma": "time",
    "forecast_period": "model_level_number",
    "forecast_reference_time": "model_level_number",
}


def _fold_forecast_grid_onto_time(ds):
    """
    Put variables left on the (forecast_period, forecast_reference_time) grid onto
    the shared time axis.

    iris does not always merge every variable in a month the same way: for 2013-03
    it put the leveled cubes straight onto a ``time`` dimension, so
    :func:`_build_time_axis` took its early return and the level-less surface
    fields were never stacked. Each (reference time, period) pair is exactly one
    valid time, so stack the pair and re-stamp it with their sum.
    """
    if "forecast_period" not in ds.dims:
        return ds

    names = sorted(v for v in ds.data_vars if "forecast_period" in ds[v].dims)
    log(f"WARNING: {', '.join(names)} left on the (forecast_period, "
        f"forecast_reference_time) grid; folding onto the valid-time axis")

    folded = ds[names].stack(newtime=("forecast_period", "forecast_reference_time"))
    fp = folded["forecast_period"].values
    frt = folded["forecast_reference_time"].values
    folded = folded.drop_vars(["forecast_period", "forecast_reference_time", "newtime"])
    folded = folded.assign_coords(
        time=("newtime", frt + fp),
        forecast_period=("newtime", fp),
        forecast_reference_time=("newtime", frt),
    ).swap_dims({"newtime": "time"})

    rest = ds.drop_vars(names).drop_dims(["forecast_period", "forecast_reference_time"])
    return xr.merge([rest, folded], join="inner", compat="override")


def _collapse_redundant_coord_dims(ds):
    """
    Reduce one-axis coordinates that iris left broadcast over a second dimension.

    ``level_height``/``sigma`` describe the vertical grid, ``forecast_period``/
    ``forecast_reference_time`` the time axis. When one source file in a month is
    inconsistent, iris cannot merge these into a single coordinate and leaves them
    2-D. Seen for 2012-03-15 03:00, where the hybrid-height coefficients are
    rounded differently in the last float32 bit (7700.039 vs 7700.040), which also
    leaves the forecast coordinates broadcast across level.

    Takes the first slice along the surplus dimension, warning with the size of the
    discrepancy discarded so a genuine mid-month change cannot pass unnoticed.
    """
    for name, extra_dim in _ONE_AXIS_COORDS.items():
        if name in ds.variables and extra_dim in ds[name].dims:
            first = ds[name].isel({extra_dim: 0}, drop=True)
            spread = np.abs(ds[name] - first).max().values
            log(f"WARNING: {name} varies along {extra_dim} (max deviation {spread}); "
                f"collapsing to the first {extra_dim} slice")
            ds[name] = first
    return ds


def _normalize_region_shape(ds):
    """
    Force a region's dataset into the canonical shape before it is written.

    A month whose source files are internally inconsistent comes out of the merge
    with variables off the time axis, or coordinates carrying a dimension they
    should not (see the two helpers above). Either way the intermediate is written
    malformed and nothing notices until the monthly dataset is appended to a yearly
    store built from well-formed months. Reform here, at the one boundary where the
    intermediate is created, so everything downstream can assume the shape.
    """
    return _collapse_redundant_coord_dims(_fold_forecast_grid_onto_time(ds))


def log(msg):
    """Print a timestamped message."""
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _dir_size_mb(path):
    """Total on-disk size (MB) of a directory tree (e.g. a zarr store)."""
    total = sum(
        os.path.getsize(os.path.join(root, name))
        for root, _dirs, files in os.walk(path)
        for name in files
    )
    return total / (1024 * 1024)


def _dataarray_from_iris_safely(cube):
    """Convert an iris cube to an xarray DataArray, muting the timedelta-decode warning."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="In a future version, xarray will not decode timedelta values.*",
            category=FutureWarning,
        )
        return xr.DataArray.from_iris(cube)


def build_region_filepath(met_archive_directory, mk):
    """
    Build the (prefix, suffix) glob template that ``load_iris`` expands per region.

    The resulting filename is
    ``{prefix}{date}{suffix}{region}.pp[.gz]`` — matching every 3-hourly file for
    the month/region. Mk 6 files omit the ``_I_`` variable-set tag; Mk 7+ include
    it.

    Parameters
    ----------
    met_archive_directory : str
        Base met archive directory.
    mk : int
        UM Mk version.

    Returns
    -------
    list of str
        ``[prefix, suffix]`` for ``load_iris``.
    """
    base = os.path.join(met_archive_directory, f"UMG_Mk{mk}PT", "MO")
    if mk == 6:
        return [base, f"*.UMG_Mk{mk}_L59PT"]
    return [base, f"*.UMG_Mk{mk}_I_L59PT"]


def extract_region(
    domain_key,
    year,
    month,
    region_id,
    cfg,
    target=None,
    use_interp=None,
    scratch_dir=None,
    save=True,
    cleanup_pp=True,
    days=None,
    source=None,
    suffix=None,
):
    """
    Extract a single world region for a domain/year/month (or a shorter period).

    Loads the region's UM data for the period, aligns winds onto the mass grid,
    applies the target grid, slices to the region's bounds (handling dateline
    crossings), and writes a per-region zarr intermediate to scratch.

    Parameters
    ----------
    domain_key : str
        Domain key (e.g., 'SA').
    year : int
        Year (e.g., 2016).
    month : int or str
        Calendar month, as an int 1-12 or a "01".."12" string.
    region_id : int
        World region ID (1-14).
    cfg : extract_um_met.config.Config
        Configuration object.
    target : tuple of np.ndarray, optional
        Precomputed ``(target_lat, target_lon)`` 1D arrays. If None, the target
        grid is built from the domain's grid spec via ``build_target_grid``.
    use_interp : bool, optional
        Whether to interpolate onto the target grid. If None, inferred from the
        grid mode (footprint/regular → True, native → False).
    scratch_dir : str, optional
        Directory for per-region intermediates. Defaults to
        ``{scratch_path}/files/``.
    save : bool, optional
        If True (default), write the intermediate and return its path. If False,
        return the in-memory dataset instead.
    cleanup_pp : bool, optional
        If True (default), delete the region's unzipped .pp scratch files after
        a successful load to conserve scratch space.
    days : list of str, optional
        The ``YYYYMMDD`` day keys to load, from
        :func:`extract_um_met.periods.period_chunks`. Defaults to the whole month.
        A shorter period holds less in memory at once, which is what keeps large
        domains inside their memory limit; the intermediate filename then carries
        the period's tag (see :func:`extract_um_met.periods.period_tag`).
    suffix : str, optional
        Extra tag appended to the domain name in the intermediate filename
        (``{domain_name}_{suffix}_Met_...``), so variant extractions don't share
        scratch intermediates. See :func:`extract_um_met.config.store_stem`.

    Returns
    -------
    pathlib.Path or xarray.Dataset
        Path to the per-region zarr store (if ``save``), else the dataset.
    """
    if isinstance(cfg, dict):
        cfg = Config(cfg)

    # The period's day keys select which archive files to load; its tag names the
    # intermediate (YYYYMM for a whole month, else the period's first day).
    month_int = int(month)
    month_key = f"{year}{month_int:02d}"
    if days is None:
        days = month_days(year, month_int)
    tag = period_tag(year, month_int, days)

    domain_cfg = cfg.get_domain(domain_key)
    domain_name = domain_cfg["domain_name"]
    stem = store_stem(domain_name, suffix)

    # Resolve the met data type ("source") — where the files live, the Mk
    # calendar, level set, and region scheme all come from it.
    if source is None:
        source = get_source(domain_cfg.get("data_type", "UM_Global"), cfg)

    scratch_root = resolve_config_value(cfg.get("scratch_path", ""), cfg.data)
    if scratch_dir is None:
        scratch_dir = os.path.join(scratch_root, "files")
    # exist_ok=True isn't race-safe on parallel scratch: concurrent array
    # tasks can lose the mkdir and see a stale isdir(), so ignore EEXIST.
    try:
        os.makedirs(scratch_dir, exist_ok=True)
    except FileExistsError:
        pass

    mk = source.get_mk(year, month_int)
    levels = source.levels()

    # Resolve the target grid if not provided.
    if target is None:
        target_lat, target_lon, inferred_interp = build_target_grid(domain_cfg, cfg, mk=mk)
        if use_interp is None:
            use_interp = inferred_interp
    else:
        target_lat, target_lon = target
        if use_interp is None:
            grid_mode = domain_cfg.get("grid", {}).get("mode", "footprint")
            use_interp = grid_mode != "native"

    region_bounds = source.region_bounds()
    if region_bounds is None:
        raise ValueError(
            f"source {source.name!r} is not tiled (region_scheme='none'); "
            f"per-region extract needs a region scheme."
        )

    mk_label = f"Mk{mk}" if mk is not None else source.name
    region_start = datetime.datetime.now()
    log(f"************ Region {region_id} ({domain_name} {tag}, {mk_label}) ************")

    with dask.config.set(**{"array.slicing.split_large_chunks": True}), \
            xr.set_options(keep_attrs=True):
        t0 = datetime.datetime.now()
        # One glob for the whole month, then keep the days this period wants: the
        # Mk10+ archive folders hold ~400k files in one directory, so globbing per
        # day would repeat that listing once per day of the period.
        wanted = [source.glob_pattern(d, region=region_id, mk=mk) for d in days]
        files = [
            f for f in source.list_files(month_key, region=region_id, mk=mk)
            if any(fnmatch(f, pattern) for pattern in wanted)
        ]
        cube = load_files(files, source.load_vars(), scratch_root + os.sep)
        log(f"region {region_id} loaded in {(datetime.datetime.now() - t0).total_seconds():.1f}s")

        # --- Instantaneous group: mass-grid variables + staggered winds -------
        # Cubes are selected by name + level presence (see _pick) so a changed
        # archive raises rather than silently mis-selecting.
        most_variables = xr.combine_by_coords(
            [_dataarray_from_iris_safely(_pick(cube, name, lvl)) for name, lvl in source.mass_vars]
        )
        # Subsample model levels only if a leveled field is present — a mass_vars
        # set of purely surface/2D fields (e.g. just BL thickness) has no
        # model_level_number coordinate to select on.
        if "model_level_number" in most_variables.coords:
            most_variables = most_variables.sel(model_level_number=levels)
        mass_lat = most_variables.latitude.values
        mass_lon = most_variables.longitude.values

        winds = []
        for name, _ in source.wind_vars:
            da = xr.combine_by_coords(
                [_dataarray_from_iris_safely(_pick(cube, name, True))], compat="override"
            ).sel(model_level_number=levels)
            winds.append(da.interp(latitude=mass_lat, longitude=mass_lon))

        inst = xr.combine_by_coords([most_variables, *winds], compat="override")
        del most_variables, winds

        # --- Averaged group: fluxes/stresses, re-stamped to the interval end ---
        # These are time-means from a different forecast run, so they can't be
        # merged with the instantaneous fields until re-stamped onto the shared
        # valid-time axis (see _restamp_to_interval_end).
        avg_arrays = []
        for name in source.averaged_mass_vars:
            c, averaging = _restamp_to_interval_end(_pick(cube, name, False))
            avg_arrays.append(_dataarray_from_iris_safely(c).assign_attrs(averaging))
        for name in source.averaged_staggered_vars:
            c, averaging = _restamp_to_interval_end(_pick(cube, name, True))
            da = _dataarray_from_iris_safely(c).assign_attrs(averaging)
            if "model_level_number" in da.coords:
                da = da.drop_vars("model_level_number")
            avg_arrays.append(da.interp(latitude=mass_lat, longitude=mass_lon))
        del cube

        # --- Load into memory, then free the .pp scratch --------------------
        t0 = datetime.datetime.now()
        inst.load()
        avg_arrays = [da.load() for da in avg_arrays]
        log(f"loaded combined dataset into memory in {(datetime.datetime.now() - t0).total_seconds():.1f}s")

        if cleanup_pp:
            # Safe to delete the unzipped .pp files now the data is in memory.
            # (iris.load is lazy, so this must come after .load(), not before.)
            try:
                for day_key in days:
                    delete_iris(scratch_root + os.sep, day_key, region_id)
            except Exception as exc:  # non-fatal; just leaves scratch files behind
                log(f"could not clean up .pp scratch for region {region_id}: {exc}")

        # --- Build the valid-time axis per group, then merge on time ---------
        # Instantaneous fields retain forecast_period/forecast_reference_time as
        # provenance; each averaged field builds its own time axis (its forecast
        # dims may differ) and drops that bookkeeping before merging on valid time.
        inst = _build_time_axis(inst, keep_provenance=True)
        _check_unique_time(inst, region_id, tag)
        log("time dimension constructed; forecast_period + forecast_reference_time retained")

        if avg_arrays:
            avg_das = []
            for da in avg_arrays:
                da = _build_time_axis(da, keep_provenance=False)
                _check_unique_time(da, region_id, tag)
                avg_das.append(da)
            all_variables = xr.merge([inst, *avg_das], join="inner", compat="override")
            log(f"merged {len(avg_das)} time-averaged field(s) onto the valid-time grid")
        else:
            all_variables = inst
        del inst

        # Normalise longitude to [-180, 180].
        all_variables = all_variables.assign_coords(
            longitude=(((all_variables.longitude + 180) % 360) - 180)
        )

        # Apply the target grid.
        if use_interp:
            all_variables = all_variables.interp(latitude=target_lat, longitude=target_lon)
            log(f"interpolated region {region_id} onto target grid ({len(target_lat)}x{len(target_lon)})")
        else:
            log(f"native mode: keeping region {region_id} on its UM grid (no interpolation)")

        # Slice to this region's bounds, handling dateline crossing.
        #
        # Native passthrough skips the interp above, and that interp is what
        # otherwise applies the domain's lat/lon bounds — leaving the store the
        # full union of world_regions_codes however narrow the bounds were. So
        # clip the region box onto target_lat/target_lon here, which _build_native_grid
        # has already subset to those bounds. In interp mode the window is the whole
        # globe and the clamps below are no-ops.
        lat_min, lat_max = region_bounds[region_id][0], region_bounds[region_id][1]
        lon_min, lon_max = region_bounds[region_id][2], region_bounds[region_id][3]
        lon_lo, lon_hi = -180.0, 180.0
        if not use_interp:
            lat_min = max(lat_min, float(target_lat.min()))
            lat_max = min(lat_max, float(target_lat.max()))
            lon_lo, lon_hi = float(target_lon.min()), float(target_lon.max())

        all_variables = all_variables.sel(latitude=slice(lat_min, lat_max))
        if lon_min <= lon_max:
            all_variables = all_variables.sel(
                longitude=slice(max(lon_min, lon_lo), min(lon_max, lon_hi))
            )
        else:
            east = all_variables.sel(longitude=slice(max(lon_min, lon_lo), min(180.0, lon_hi)))
            west = all_variables.sel(longitude=slice(max(-180.0, lon_lo), min(lon_max, lon_hi)))
            all_variables = xr.concat([east, west], dim="longitude")
            log(f"region {region_id} crosses dateline; concatenated east/west slices")

        # Now that the bounds actually remove data, a region listed in
        # world_regions_codes but outside them yields an empty tile that would only
        # fail later, in the join.
        if all_variables.sizes["latitude"] == 0 or all_variables.sizes["longitude"] == 0:
            raise ValueError(
                f"region {region_id} has no overlap with the domain's lat/lon bounds "
                f"(lat {lat_min}..{lat_max}, lon {lon_lo}..{lon_hi}); drop it from "
                f"world_regions_codes or widen the bounds."
            )

        result = _normalize_region_shape(all_variables).sortby("time")
        log(f"region {region_id} extracted; dims {dict(result.sizes)}")

        if not save:
            log(
                f"---- Region {region_id} done in "
                f"{(datetime.datetime.now() - region_start).total_seconds():.1f}s (not saved) ----"
            )
            return result

        # Whole lat/lon/levels with time in blocks: big, Lustre-friendly chunks
        # that dask writes in parallel (each ~tens of MB), and that the join reads
        # back whole per region.
        result = result.chunk(
            {"model_level_number": -1, "time": 24, "latitude": -1, "longitude": -1}
        )

        store = os.path.join(scratch_dir, f"{stem}_Met_{tag}_{region_id}.zarr")
        t0 = datetime.datetime.now()
        # Same zarr format as the yearly store, so scratch and final output stay on
        # one format instead of the intermediates taking the library default (v3).
        result.to_zarr(store, mode="w", zarr_format=cfg.get("zarr_format", 2))
        size_mb = _dir_size_mb(store)
        log(
            f"---- Region {region_id} saved to {store} in "
            f"{(datetime.datetime.now() - t0).total_seconds():.1f}s ({size_mb:.1f} MB); "
            f"total {(datetime.datetime.now() - region_start).total_seconds():.1f}s ----"
        )

    return store


def extract_single(
    domain_key,
    date,
    cfg,
    target=None,
    scratch_dir=None,
    save=False,
    source=None,
    suffix=None,
):
    """
    Extract a whole-domain, single-file (non-tiled) source for a time window.

    For sources like NZCSM one file already covers the whole domain, so there is
    no region loop and no join. Fields on a rotated-pole grid are regridded onto
    the target regular lat/lon grid (iris), with winds rotated to true north
    first; time-averaged fields are reconciled onto the valid-time axis exactly as
    in :func:`extract_region`. The native rotated-pole parameters are recorded in
    the output attributes.

    Parameters
    ----------
    domain_key : str
        Domain key.
    date : str
        Glob key for the time window (e.g. 'YYYYMMDD' for a day, 'YYYYMMDDHH' for
        an hour) — matched against the source's single-file template.
    cfg : extract_um_met.config.Config
    target : tuple of np.ndarray, optional
        Precomputed ``(target_lat, target_lon)`` regular grid. If None, built from
        the domain's grid spec.
    scratch_dir : str, optional
        Directory for the intermediate zarr store (only used when ``save``).
    save : bool, optional
        If True, write an intermediate zarr store and return its path; otherwise
        return the in-memory dataset (the default for the batched run loop).
    source : extract_um_met.sources.MetSource, optional
        Resolved source; if None, resolved from the domain's ``data_type``.
    suffix : str, optional
        Extra tag appended to the domain name in the output filename
        (``{domain_name}_{suffix}_Met_...``). See
        :func:`extract_um_met.config.store_stem`.

    Returns
    -------
    pathlib.Path/str or xarray.Dataset
    """
    if isinstance(cfg, dict):
        cfg = Config(cfg)

    domain_cfg = cfg.get_domain(domain_key)
    domain_name = domain_cfg["domain_name"]
    stem = store_stem(domain_name, suffix)
    if source is None:
        source = get_source(domain_cfg.get("data_type", "UM_Global"), cfg)

    scratch_root = resolve_config_value(cfg.get("scratch_path", ""), cfg.data)
    if scratch_dir is None:
        scratch_dir = os.path.join(scratch_root, "files")
    # exist_ok=True isn't race-safe on parallel scratch: concurrent array
    # tasks can lose the mkdir and see a stale isdir(), so ignore EEXIST.
    try:
        os.makedirs(scratch_dir, exist_ok=True)
    except FileExistsError:
        pass

    levels = source.levels()
    if target is None:
        target_lat, target_lon, _ = build_target_grid(domain_cfg, cfg)
    else:
        target_lat, target_lon = target

    start = datetime.datetime.now()
    log(f"************ Single-file extract ({domain_name} {date}, {source.name}) ************")

    with dask.config.set(**{"array.slicing.split_large_chunks": True}), \
            xr.set_options(keep_attrs=True):
        t0 = datetime.datetime.now()
        files = source.list_files(date)   # region=None (non-tiled)
        cube = load_files(files, source.load_vars(), scratch_root + os.sep)
        log(f"loaded {len(files)} file(s) in {(datetime.datetime.now() - t0).total_seconds():.1f}s")

        target_cube = latlon_target_cube(target_lat, target_lon)

        # provenance of the native grid (rotated-pole params), from a mass cube
        first_name, first_lvl = source.mass_vars[0]
        first_mass_cube = _pick(cube, first_name, first_lvl)
        pole_attrs = rotated_pole_attrs(first_mass_cube)

        # Every field regridded below sits on this same rotated mass grid — the
        # winds are co-located onto it first — so the source→target mapping is
        # built once here rather than recomputed for each of the ~10 cubes.
        regridder = latlon_regridder(first_mass_cube, target_cube)

        def _regrid_da(c, name):
            da = _dataarray_from_iris_safely(regridder(c)).rename(name)
            drop = [x for x in ("level_height", "sigma", "level_height_0", "sigma_0")
                    if x in da.coords]
            return da.drop_vars(drop) if drop else da

        def _sub(c, want_levels):
            return c.extract(iris.Constraint(model_level_number=levels)) if want_levels else c

        # --- Instantaneous mass fields: subsample levels, regrid ---
        inst_arrays = []
        mass_native = None   # a native (rotated) mass cube at subsampled levels
        for name, want_levels in source.mass_vars:
            c = _sub(_pick(cube, name, want_levels), want_levels)
            if want_levels and mass_native is None:
                mass_native = c
            inst_arrays.append(_regrid_da(c, name))

        # --- Winds: co-locate on the native mass grid, rotate to true north, regrid ---
        if len(source.wind_vars) >= 2:
            uname = source.wind_vars[0][0]
            vname = source.wind_vars[1][0]
            u = _sub(_pick(cube, uname, True), True)
            v = _sub(_pick(cube, vname, True), True)
            u_t, v_t = rotate_winds_true_north(u, v, mass_native)
            inst_arrays.append(_regrid_da(u_t, uname))
            inst_arrays.append(_regrid_da(v_t, vname))

        # --- Averaged mass fields (e.g. sensible heat): restamp to interval end, regrid ---
        #     (averaged_staggered_vars are deferred for rotated sources — need
        #      vector rotation; see MetSource.deferred_vars.)
        avg_arrays = []
        for name in source.averaged_mass_vars:
            c, averaging = _restamp_to_interval_end(_pick(cube, name, False))
            avg_arrays.append(_regrid_da(c, name).assign_attrs(averaging))
        del cube

        # --- Load into memory ---
        t0 = datetime.datetime.now()
        inst = xr.merge(inst_arrays, compat="override")
        inst.load()
        avg_arrays = [da.load() for da in avg_arrays]
        log(f"regridded + loaded in {(datetime.datetime.now() - t0).total_seconds():.1f}s")

        # --- Build valid-time axis, merge averaged fields on valid time ---
        inst = _build_time_axis(inst, keep_provenance=True)
        _check_unique_time(inst, "single", date)
        if avg_arrays:
            avg_das = []
            for da in avg_arrays:
                da = _build_time_axis(da, keep_provenance=False)
                _check_unique_time(da, "single", date)
                avg_das.append(da)
            all_variables = xr.merge([inst, *avg_das], join="inner", compat="override")
        else:
            all_variables = inst
        del inst

        # Finalise to the same zarr-ready schema + CF/ARCO metadata as the tiled
        # join path (rename lat/lon/levels, delta attrs, CF attrs), then add the
        # rotated-pole provenance note.
        all_variables = to_zarr_schema(all_variables)
        all_variables = add_delta_attrs(all_variables)
        grid_mode = domain_cfg.get("grid", {}).get("mode", "regular")
        year = int(str(date)[:4])
        month = int(str(date)[4:6]) if len(str(date)) >= 6 else 1
        all_variables = apply_cf_metadata(
            all_variables, cfg, mk=source.get_mk(year, month), grid_mode=grid_mode,
            domain_name=domain_name, year=year, use_interp=True, source_name=source.name,
        )
        all_variables.attrs.update(pole_attrs)

        result = all_variables.sortby("time")
        log(f"single-file extracted; dims {dict(result.sizes)}")

        if not save:
            log(f"---- {domain_name} {date} done in "
                f"{(datetime.datetime.now() - start).total_seconds():.1f}s (not saved) ----")
            return result

        result = result.chunk(
            {"levels": -1, "time": 24, "lat": 200, "lon": 200}
        )
        store = os.path.join(scratch_dir, f"{stem}_Met_{date}.zarr")
        # Same zarr format as the yearly store, so scratch and final output stay on
        # one format instead of the intermediates taking the library default (v3).
        result.to_zarr(store, mode="w", zarr_format=cfg.get("zarr_format", 2))
        log(f"---- {domain_name} {date} saved to {store} "
            f"({_dir_size_mb(store):.1f} MB) in "
            f"{(datetime.datetime.now() - start).total_seconds():.1f}s ----")

    return store
