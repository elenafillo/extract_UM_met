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

from .config import (
    Config,
    resolve_config_value,
    resolve_flux_time_regrid,
    resolve_max_level_m,
    resolve_wind_vertical_regrid,
    store_stem,
)
from .iris_io import load_files, delete_iris, _pick, _theta_ladder_cube
from .staggering import destagger_winds_onto_theta, rho_ladder, rho_ladder_attrs
from .grid import build_target_grid
from .periods import month_days, period_tag, next_day
from .sources import get_domain_source
from .metadata import (
    FLUX_TIME_DIM,
    add_delta_attrs,
    apply_cf_metadata,
    to_zarr_schema,
)
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
# forecast run; what happens to that stamp is the domain's `flux_time_regrid`
# (see _stamp_averaged_time, which also describes the window in the fields'
# `averaging*` attributes).


def _apply_max_level(levels, mass_cube, max_level_m):
    """
    Drop levels above ``max_level_m``, keeping the first selected level above it.

    That bracketing level is deliberate: a consumer interpolating onto a ceiling
    of exactly ``max_level_m`` (GLIDE's ``alt_max_m``) then has a level above to
    interpolate to instead of extrapolating off the top of the store. The store's
    top level therefore sits just *above* its stated ceiling.

    Applied to the already-resolved selection, so the bracketing level is the next
    level the store would have held, not the next model level in the archive.

    Logs what the ceiling resolved to, including the top level's height, since
    ``max_level_m`` is a metre value and the thing a reader wants to know is which
    ladder it produced. That height is above **sea level**; what it comes to above
    ground needs the orography, so :func:`extract_um_met.join._agl_ceiling_message`
    says the rest wherever an ancillary is loaded.
    """
    heights = dict(zip(
        mass_cube.coord("model_level_number").points,
        mass_cube.coord("level_height").points,
    ))
    below = [lvl for lvl in levels if heights[lvl] <= max_level_m]
    above = [lvl for lvl in levels if heights[lvl] > max_level_m]
    if not below:
        raise ValueError(
            f"max_level_m={max_level_m:g} is below the lowest selected model level "
            f"(level {levels[0]} at {heights[levels[0]]:g} m); nothing would be extracted."
        )
    kept = below + above[:1]
    log(f"max_level_m={max_level_m:g}: keeping {len(kept)} of {len(levels)} levels "
        f"(top level {kept[-1]} at {heights[kept[-1]]:.1f} m above sea level, which "
        f"brackets the ceiling)")
    return kept


def _averaging_prose(mode, hours):
    """
    The ``averaging`` attribute's text for one ``flux_time_regrid`` mode.

    The three modes do different things to a field, not different amounts of the
    same thing, so they get three sentences rather than one with the numbers
    swapped: under ``snap`` the value is a true mean stamped at the wrong time,
    under ``interpolate`` it is stamped at the right time but is no longer a mean
    of anything, and under ``keep`` it is both, at the price of its own axis.
    """
    sampling = ("The interval in cell_methods is the UM's sampling interval "
                "within the window, not the window itself.")
    if mode == "snap":
        return (
            f"{hours:g}-hour time-mean over [timestamp - {hours:g} h, timestamp], "
            f"stamped at the interval END (re-stamped from the UM's interval-midpoint "
            f"convention) so it shares the instantaneous fields' time axis. Read as an "
            f"instantaneous value its validity time is the window midpoint, "
            f"{hours / 2:g} h before the timestamp. {sampling}"
        )
    if mode == "interpolate":
        return (
            f"{hours:g}-hour time-mean, linearly interpolated in time from the UM's "
            f"interval midpoints onto the instantaneous fields' timestamps, so the "
            f"value is centred on its timestamp instead of being {hours / 2:g} h late. "
            f"It is therefore no longer the mean of any one window but a weighted mean "
            f"of the two windows either side, and no longer conserves the integral. "
            f"Every value is bracketed by two real windows — the last timestamp of a "
            f"period is bracketed by the first analysis of the next, read for that "
            f"window alone — and nothing is extrapolated. Where the archive has no "
            f"such analysis the timestamp is dropped rather than invented. {sampling}"
        )
    return (
        f"{hours:g}-hour time-mean over [{FLUX_TIME_DIM} - {hours / 2:g} h, "
        f"{FLUX_TIME_DIM} + {hours / 2:g} h], left untouched on its own "
        f"{FLUX_TIME_DIM} axis at the window midpoint — the time it is valid. "
        f"There is one {FLUX_TIME_DIM} point per window the archive supplies, "
        f"which is not necessarily one per 'time' step: before Mk10 only every "
        f"other step closes a window. {sampling}"
    )


def _drop_degenerate_windows(cube):
    """
    Drop the zero-length "means" the pre-Mk10 archive interleaves with real ones.

    Before Mk10 a day carries eight records for each averaged field but only four
    windows: the analysis steps (00/06/12/18) hold a T+0 record whose time bounds
    are zero-width, and the forecast steps (03/09/15/21) hold the genuine 3-hour
    mean. Both arrive with the same ``cell_methods``, and the T+0 records hold
    real, plausible values, so the bounds width is the only thing separating a
    mean from an instantaneous sample. See How_Tos/known_problems.md.

    They have to go before anything measures or re-stamps the window: kept, they
    make :func:`_averaging_attrs` see two window lengths, and snapping them to
    their own timestamp is what silently interleaved instantaneous fluxes with
    means in every pre-Mk10 store built so far.

    From Mk10 every record is a real window and this returns the cube untouched —
    which is also the only case where ``time`` spans two dimensions, so the
    indexing below never has to deal with the product grid.
    """
    tc = cube.coord("time")
    bounds = np.sort(tc.bounds.reshape(-1, 2), axis=1)
    real = bounds[:, 1] > bounds[:, 0]
    if real.all():
        return cube
    (dim,) = cube.coord_dims(tc)
    index = [slice(None)] * cube.ndim
    index[dim] = np.flatnonzero(real)
    return cube[tuple(index)]


def _averaging_attrs(time_coord, mode):
    """
    Describe the averaging window of a time-averaged cube, from its time bounds.

    Two things a consumer needs and cannot get from ``cell_methods``, whose
    ``interval:`` is the UM's *sampling* interval inside the window rather than
    the window itself: how long the window is, and where the timestamp sits on it
    once ``flux_time_regrid`` has had its way with it.
    ``averaging_midpoint_offset_hours`` is how far the value's validity time lies
    from its timestamp, so it is zero in the two modes that put them together —
    which is the test a consumer should use to decide whether to apply a shift of
    its own, rather than assuming one.

    From Mk10 ``time`` is still on the (forecast_period, forecast_reference_time)
    grid here — :func:`_build_time_axis` collapses it later — so ``bounds`` is 3-D
    and ``bounds[0]`` is a whole row of cells, not one window. Flatten to one row
    per cell first; measuring the row gave 6 h for a 3-hourly field. Earlier Mks
    put the averaged fields on a single anonymous dimension instead and the
    flatten is a no-op.

    Runs after :func:`_drop_degenerate_windows`, so the differing widths this
    still raises on are genuinely different windows rather than the pre-Mk10
    archive's zero-length T+0 records.
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
        "averaging": _averaging_prose(mode, hours),
        "averaging_period_hours": hours,
        "averaging_midpoint_offset_hours": -hours / 2 if mode == "snap" else 0.0,
    }


def _stamp_averaged_time(cube, mode):
    """
    Stamp a time-averaged cube's time per ``flux_time_regrid``, dropping bounds.

    Averaged fields (fluxes/stresses) arrive at the interval midpoint with time
    bounds [start, end]; the end equals the instantaneous fields' valid time.

    Only ``snap`` moves the point to the end, so the averaged fields can share the
    instantaneous fields' axis. ``interpolate`` and ``keep`` both stamp the
    midpoint: for ``interpolate`` that is where the interpolation starts from, and
    for ``keep`` it is the answer — the field stays there, on its own axis, in
    :func:`_merge_onto_flux_time_axis`.

    The midpoint is recomputed from the bounds rather than taken from the point
    the UM supplied, so it is the centre of the window by construction and not by
    the archive's convention. Modifies and returns the cube, with the
    ``averaging*`` attributes describing the window — read off the bounds here,
    the last point at which they exist.
    """
    tc = cube.coord("time")
    averaging = {}
    if tc.has_bounds():
        cube = _drop_degenerate_windows(cube)
        tc = cube.coord("time")
        averaging = _averaging_attrs(tc, mode)
        if mode == "snap":
            tc.points = tc.bounds.max(axis=-1)   # interval end (robust to bound order)
        else:
            tc.points = tc.bounds.mean(axis=-1)  # window midpoint = validity time
        tc.bounds = None
    if cube.coords("forecast_period") and cube.coord("forecast_period").has_bounds():
        cube.coord("forecast_period").bounds = None
    return cube, averaging


def _half_window(da):
    """Half a field's averaging window, as a timedelta64, from its own attributes."""
    hours = da.attrs["averaging_period_hours"]
    return np.timedelta64(round(hours * 1800), "s")


def _recentre_onto_interval_ends(da):
    """
    Move an averaged field from its window midpoints onto the interval ends.

    The target timestamps are exactly the ones ``snap`` produces, so the two modes
    differ in the fields' values and never in the store's time axis — which is
    what lets ``interpolate`` be a drop-in for a consumer that already reads these
    stores, and what makes the mode worth recording as an attribute since nothing
    in the shape betrays it.

    With the windows evenly spaced each interval end sits midway between two
    midpoints, so this is the mean of the two windows either side; ``interp`` is
    used rather than that shortcut so a period with a gap in it is still weighted
    by the real spacing.

    The last end of a period lies half a window past its last midpoint, so the
    value bracketing it belongs to the next period. That one is supplied by the
    buffer analysis :func:`_buffer_files` loads; nothing is extrapolated here, and
    an end left unbracketed comes out NaN rather than invented. Where no buffer
    exists the period's final step is dropped whole in
    :func:`_trim_buffer_from_period`, so no NaN reaches the store either.

    Runs after :func:`_build_time_axis` (which needs a real 1-D time dimension to
    interpolate along) and after ``.load()`` (``interp`` needs the interpolation
    dim in one chunk).
    """
    ends = da["time"].values + _half_window(da)
    return da.interp(time=ends).astype(da.dtype).assign_attrs(da.attrs)


def _buffer_files(source, days, region_id, mk):
    """
    The one analysis that brackets a period's last interval end, for ``interpolate``.

    Interpolating onto an interval end needs a window midpoint either side of it.
    Every end but the last has both inside the period; the last sits half a window
    past the period's final midpoint, so its upper bracket is the first analysis of
    the *following* day — for a month-long period, the next month's.

    Only that 00:00 analysis is globbed, never the whole day: the record wanted is
    its ``forecast_period = -1.5 h`` window, whose midpoint falls on the period's
    own last day.

    The file is asked for on **this period's Mk**, not the next month's. The
    archive keeps writing the outgoing Mk for a while past a boundary, so at a
    boundary the leftover file is the one still on the period's grid — which is the
    only one usable here, since the time interpolation happens before the regrid
    onto the target. Where the archive has nothing (its final month, or a boundary
    with no overlap) this returns an empty list and the caller drops the
    unbracketed step rather than inventing it.
    """
    return source.list_files(f"{next_day(days[-1])}00", region=region_id, mk=mk)


def _trim_buffer_from_period(ds, days, buffered):
    """
    Cut the buffer analysis's own steps back off the period, after interpolating.

    The buffer is loaded for one flux window and its instantaneous fields come
    with it; all of them fall on the following day, so everything from that day's
    midnight onward goes.

    With no buffer available the period's last interval end was never bracketed
    and :func:`_recentre_onto_interval_ends` left it NaN. That step is dropped
    whole — every variable, not just the averaged ones, so the store stays
    rectangular — leaving a period one step short rather than one step invented.
    """
    first_dropped = np.datetime64(
        datetime.datetime.strptime(next_day(days[-1]), "%Y%m%d")
    )
    ds = ds.isel(time=(ds["time"].values < first_dropped))
    if not buffered:
        ds = ds.isel(time=slice(None, -1))
    return ds


def _merge_onto_flux_time_axis(inst, avg_das):
    """
    Merge the averaged fields onto their own ``time_flux`` axis at the midpoints.

    The axis is *generated* from the windows the archive supplies rather than
    derived from ``time``, which is what lets this mode describe both archive
    generations. From Mk10 there is a window on every instantaneous step and the
    two axes come out the same length; before Mk10 there is one on every other
    step, so ``time_flux`` is half the length of ``time`` and carries the four
    real midpoints of each day. Deriving it as ``time - half a window`` would
    invent the four missing ones.

    Merging on a differently-named dimension is a union, not a join, so unlike
    ``snap`` and ``interpolate`` this cannot drop instantaneous steps: an inner
    join against a half-length flux axis would take the rest of the store down
    with it.

    The forecast bookkeeping is not carried across: it is the instantaneous
    fields' provenance and never described the averaged fields' own runs.
    """
    flux = [
        da.rename({"time": FLUX_TIME_DIM}).drop_vars(
            ["forecast_period", "forecast_reference_time"], errors="ignore"
        )
        for da in avg_das
    ]
    return xr.merge([inst, *flux], compat="override")


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
        source = get_domain_source(domain_cfg, cfg)

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
    max_level_m = resolve_max_level_m(domain_cfg)
    wind_regrid = resolve_wind_vertical_regrid(domain_cfg)
    # Against the year, not `mk`: the store is yearly and the averaged fields
    # cannot change axis halfway through it. See min_mk_in_year.
    year_mk = source.min_mk_in_year(year)
    flux_regrid = resolve_flux_time_regrid(domain_cfg, year_mk)
    if flux_regrid != domain_cfg.get("flux_time_regrid", "snap"):
        log(f"part of {year} comes from Mk{year_mk}, which supplies an averaging "
            f"window on only half the time steps, and the averaged fields cannot "
            f"change axis partway through a store — so flux_time_regrid is "
            f"{flux_regrid!r} for all of {year}, not "
            f"{domain_cfg.get('flux_time_regrid', 'snap')!r}. Every other Mk-dependent "
            f"choice (grid, levels, paths) is still resolved per month, as Mk{mk} here.")

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
        # `interpolate` needs one analysis past the period to bracket its last
        # interval end; every other mode reads the period and nothing else.
        buffer_files = []
        if flux_regrid == "interpolate":
            buffer_files = _buffer_files(source, days, region_id, mk)
            if buffer_files:
                log(f"buffering the next analysis ({os.path.basename(buffer_files[0])}) "
                    f"to bracket the period's last interval end")
            else:
                log(f"no analysis after {days[-1]} on Mk{mk}, so the period's last "
                    f"interval end cannot be bracketed; it will be dropped")
        cube = load_files(files + buffer_files, source.load_vars(), scratch_root + os.sep)
        log(f"region {region_id} loaded in {(datetime.datetime.now() - t0).total_seconds():.1f}s")

        # Read before the de-stagger, which replaces the wind cubes' vertical
        # coordinates with the theta ladder: afterwards there is nothing left on
        # the cubes to say where u/v actually came from.
        rho_levels = rho_ladder(cube, source)

        # Must come before the level subsample below — theta level k is built
        # from rho k and k+1, so a strided selection has already thrown the
        # neighbour away. Ordering against the ceiling is free (both are filters
        # over `levels`); this runs first only to keep the cube work together.
        if wind_regrid == "interpolate":
            cube, levels = destagger_winds_onto_theta(cube, source, levels)
            log("winds de-staggered vertically onto the theta (mass) levels; "
                f"top model level dropped, {len(levels)} levels remain")

        # The ceiling needs the Mk's level_height ladder, so it can only resolve
        # once a cube is loaded — and before the level selection is used below.
        if max_level_m is not None:
            levels = _apply_max_level(levels, _theta_ladder_cube(cube, source), max_level_m)

        # `levels` is final here and nowhere earlier, so this is the only point
        # at which the rho ladder can be recorded index-for-index with it.
        wind_attrs = {
            "wind_vertical_regrid": wind_regrid,
            **rho_ladder_attrs(rho_levels, levels),
        }

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

        # --- Averaged group: fluxes/stresses, stamped per flux_time_regrid ------
        # These are time-means from a different forecast run, so they can't be
        # merged with the instantaneous fields until stamped onto the shared
        # valid-time axis (see _stamp_averaged_time).
        avg_arrays = []
        for name in source.averaged_mass_vars:
            c, averaging = _stamp_averaged_time(_pick(cube, name, False), flux_regrid)
            avg_arrays.append(_dataarray_from_iris_safely(c).assign_attrs(averaging))
        for name in source.averaged_staggered_vars:
            c, averaging = _stamp_averaged_time(_pick(cube, name, True), flux_regrid)
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
            # The buffer analysis is unzipped into the same scratch and is not one
            # of `days`, so it needs clearing with them.
            staged = (days + [next_day(days[-1])]) if buffer_files else days
            try:
                for day_key in staged:
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
                if flux_regrid == "interpolate":
                    da = _recentre_onto_interval_ends(da)
                avg_das.append(da)
            if flux_regrid == "keep":
                all_variables = _merge_onto_flux_time_axis(inst, avg_das)
                log(f"{len(avg_das)} time-averaged field(s) on their own "
                    f"{FLUX_TIME_DIM} axis at the averaging-window midpoint "
                    f"({all_variables.sizes[FLUX_TIME_DIM]} window(s) against "
                    f"{all_variables.sizes['time']} time step(s))")
            else:
                all_variables = xr.merge([inst, *avg_das], join="inner", compat="override")
                log(f"merged {len(avg_das)} time-averaged field(s) onto the valid-time "
                    f"grid (flux_time_regrid={flux_regrid})")
        else:
            all_variables = inst
        del inst

        if flux_regrid == "interpolate":
            n_before = all_variables.sizes["time"]
            all_variables = _trim_buffer_from_period(
                all_variables, days, bool(buffer_files)
            )
            log(f"trimmed the buffer analysis back off the period "
                f"({n_before} -> {all_variables.sizes['time']} time step(s))")

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
        if FLUX_TIME_DIM in result.dims:
            result = result.sortby(FLUX_TIME_DIM)
        # Ride on the intermediate so the join can reach them: a snapped and a
        # de-staggered region are identical in shape, coords and dtype, so this
        # is the only thing that distinguishes them. Same for the flux stamping
        # under snap/interpolate. Stamped before the `save` branch so an unsaved
        # caller gets them too.
        result.attrs.update(wind_attrs)
        result.attrs["flux_time_regrid"] = flux_regrid
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
        source = get_domain_source(domain_cfg, cfg)

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
    max_level_m = resolve_max_level_m(domain_cfg)
    # The vertical de-stagger is implemented for the UM global archive only. This
    # is the limited-area path, so honouring the key here would mean silently
    # ignoring it — and a snapped field is indistinguishable from a de-staggered
    # one downstream (same shape, coordinates and dtype). Fail at config
    # resolution instead, before anything is read.
    wind_regrid = resolve_wind_vertical_regrid(domain_cfg)
    if wind_regrid == "interpolate":
        raise ValueError(
            f"domain {domain_name!r} sets wind_vertical_regrid: interpolate, but "
            f"source {source.name!r} is extracted by the non-tiled path, where the "
            f"vertical de-stagger is not implemented. Use 'snap' for this domain."
        )
    # Same argument for the whole profile: the GLIDE schema is applied in
    # join_month, which this path never reaches, so a store would come out under
    # our names — and therefore with nothing quantised, since QUANT_RANGES is
    # keyed on GLIDE's, while its attributes claimed int16.
    if domain_cfg.get("output_profile") == "glide":
        raise ValueError(
            f"domain {domain_name!r} sets output_profile: glide, but source "
            f"{source.name!r} is extracted by the non-tiled path, which does not "
            f"apply the GLIDE schema. The profile is for the tiled UM global archive."
        )
    # flux_time_regrid, by contrast, is arithmetic on the time coordinate alone
    # and has nothing source-specific in it, so all three modes work here — though
    # the year's lowest Mk still decides whether the archive can support more
    # than 'keep'.
    year = int(str(date)[:4])
    month = int(str(date)[4:6]) if len(str(date)) >= 6 else 1
    flux_regrid = resolve_flux_time_regrid(domain_cfg, source.min_mk_in_year(year))
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

        # As in extract_region: the ceiling resolves against the loaded ladder,
        # before `levels` is used by _sub below.
        if max_level_m is not None:
            levels = _apply_max_level(levels, _theta_ladder_cube(cube, source), max_level_m)

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

        # --- Averaged mass fields (e.g. sensible heat): stamp per flux_time_regrid, regrid ---
        #     (averaged_staggered_vars are deferred for rotated sources — need
        #      vector rotation; see MetSource.deferred_vars.)
        avg_arrays = []
        for name in source.averaged_mass_vars:
            c, averaging = _stamp_averaged_time(_pick(cube, name, False), flux_regrid)
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
                if flux_regrid == "interpolate":
                    da = _recentre_onto_interval_ends(da)
                avg_das.append(da)
            if flux_regrid == "keep":
                all_variables = _merge_onto_flux_time_axis(inst, avg_das)
            else:
                all_variables = xr.merge([inst, *avg_das], join="inner", compat="override")
        else:
            all_variables = inst
        del inst

        if flux_regrid == "interpolate":
            # No buffer analysis here: `date` may name a single file, a day or a
            # month, so there is no next period to reach into the way
            # extract_region does. The last interval end is therefore unbracketed
            # and NaN, and it goes — one step short beats one step invented.
            all_variables = all_variables.isel(time=slice(None, -1))
            log("dropped the final time step: its interval end has no bracketing "
                "window on this path (see _buffer_files)")

        # Finalise to the same zarr-ready schema + CF/ARCO metadata as the tiled
        # join path (rename lat/lon/levels, delta attrs, CF attrs), then add the
        # rotated-pole provenance note.
        all_variables = to_zarr_schema(all_variables)
        all_variables = add_delta_attrs(all_variables)
        grid_mode = domain_cfg.get("grid", {}).get("mode", "regular")
        all_variables = apply_cf_metadata(
            all_variables, cfg, mk=source.get_mk(year, month), grid_mode=grid_mode,
            domain_name=domain_name, year=year, use_interp=True, source_name=source.name,
            max_level_m=max_level_m,
            # None where the store has no such fields — see the same pair in
            # join.join_month.
            wind_vertical_regrid=wind_regrid if source.wind_vars else None,
            flux_time_regrid=flux_regrid if source.has_averaged_vars() else None,
        )
        all_variables.attrs.update(pole_attrs)

        result = all_variables.sortby("time")
        if FLUX_TIME_DIM in result.dims:
            result = result.sortby(FLUX_TIME_DIM)
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
