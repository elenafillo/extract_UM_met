"""
Zarr I/O utilities for yearly meteorology stores.

Appends monthly domain datasets to a yearly zarr store (one store per year,
appended along the time dimension), and finalizes provenance attributes once a
year's months are all written.

The default chunk layout keeps ``time=1`` (single-timestep scattered reads are
the dominant access pattern), groups levels in 3s, and keeps lat/lon whole. A
domain can override it with ``output_chunks:`` in config when its own access
pattern differs. The layout applies to the data variables only; coordinates are
always written whole.
"""

import contextlib
import datetime
import getpass
import warnings

import numpy as np
import xarray as xr

from . import glide


_ZARR_CHUNKS = {"time": 1, "lat": -1, "lon": -1, "levels": 3}

# The output schema's dims — the only keys a domain may chunk on.
CHUNK_DIMS = ("time", "levels", "lat", "lon")

# zstd level used when a store is created, unless the top-level config key or the
# domain sets one.
_COMPRESSION_LEVEL = 5

# On-disk precision for the data variables. 'int16' writes the variables in
# glide.QUANT_RANGES with CF scale_factor/add_offset and leaves the rest float32.
_PRECISIONS = ("float32", "int16")


def resolve_output_chunks(domain_cfg):
    """
    Chunking for a domain's yearly store: the default with ``output_chunks:``
    from the domain's config block merged over it.

    Values are chunk lengths along that dim; ``-1`` means one chunk spanning the
    whole dim. Naming only some dims leaves the rest at their default. An
    unknown dim raises here, at config resolution, rather than silently leaving
    the default in place.
    """
    override = domain_cfg.get("output_chunks")
    if not override:
        return dict(_ZARR_CHUNKS)

    if not isinstance(override, dict):
        raise ValueError(
            f"domain output_chunks must be a mapping of {{dim: chunk_size}}, "
            f"got {override!r}"
        )
    unknown = sorted(set(override) - set(CHUNK_DIMS))
    if unknown:
        raise ValueError(
            f"domain output_chunks: unknown dim(s) {unknown}. "
            f"Valid dims: {list(CHUNK_DIMS)}"
        )

    chunks = dict(_ZARR_CHUNKS)
    for dim, size in override.items():
        size = int(size)
        if size == 0 or size < -1:
            raise ValueError(
                f"domain output_chunks: chunk size for {dim!r} must be a "
                f"positive integer or -1 (whole dim), got {size}"
            )
        chunks[dim] = size
    return chunks


def resolve_compression_level(cfg, domain_cfg):
    """
    zstd compression level for a store's first write: the domain's
    ``compression_level:`` — which its ``output_profile`` may have supplied — else
    the top-level config key, else :data:`_COMPRESSION_LEVEL`.

    Validated here, at config resolution, so a bad level fails before any
    extraction runs rather than at the first write.
    """
    default = cfg.get("compression_level", _COMPRESSION_LEVEL)
    level = int(domain_cfg.get("compression_level", default))
    if not 1 <= level <= 22:
        raise ValueError(
            f"compression_level must be a zstd level from 1 to 22, got {level}"
        )
    return level


def resolve_precision(domain_cfg):
    """
    On-disk precision for a domain's data variables: its ``precision:`` key —
    which an ``output_profile`` may have supplied — or float32.

    Fixed when a store is created, like the chunking and the compression level,
    so changing it needs ``--overwrite``. Validated here so a typo fails before
    any extraction runs.

    ``int16`` is only accepted under ``output_profile: glide``. The quantisation
    ranges are GLIDE's own table, published for their field set, so on any other
    domain a variable outside that set — ``air_pressure``, which the glide
    profile drops, or anything a ``data_types:`` override adds — would quietly
    stay float32 and the store would be part quantised with nothing on its face
    saying which half.
    """
    precision = domain_cfg.get("precision", "float32")
    if precision not in _PRECISIONS:
        raise ValueError(
            f"domain precision must be one of {list(_PRECISIONS)}, "
            f"got {precision!r}"
        )
    if precision == "int16" and domain_cfg.get("output_profile") != "glide":
        raise ValueError(
            "domain precision: int16 needs output_profile: glide. The "
            "quantisation ranges only cover GLIDE's field set, so elsewhere the "
            "store would come out part int16 and part float32. This domain has "
            f"output_profile: {domain_cfg.get('output_profile')!r}."
        )
    return precision


def _rechunk(ds, chunks=None):
    """
    Apply the on-disk zarr chunking to the data variables, using -1 (whole) for
    unlisted dims.

    Only the data variables are chunked; the coordinates are loaded instead.
    ``Dataset.chunk`` leaves dimension coordinates as pandas indexes but
    dask-ifies the non-dimension ones (``dim_0``, ``forecast_period``,
    ``forecast_reference_time`` along time; ``level_height``, ``sigma`` along
    levels), which would then inherit the data-variable layout and land one
    8-byte file per timestep. Merely skipping them is not enough: they would
    stay dask-backed with whatever chunking they arrived with, the store would
    adopt it, and since it need not divide a month the next append starts
    mid-chunk and fails xarray's alignment check. Loaded, they are written
    eagerly and whole, like ``time``.
    """
    chunks = chunks or _ZARR_CHUNKS
    loaded_coords = {
        name: coord.compute() for name, coord in ds.coords.items() if coord.chunks
    }
    return ds.assign_coords(loaded_coords).assign({
        name: var.chunk({dim: chunks.get(dim, -1) for dim in var.dims})
        for name, var in ds.data_vars.items()
    })


def _to_zarr(ds, store_path, mode=None, append_dim=None, zarr_format=2, encoding=None):
    """
    Write to zarr, tolerating xarray/zarr versions that don't accept zarr_format.

    ``zarr_format`` and ``encoding`` are only meaningful when creating the store
    (``mode='w'``); on append the format/encoding are fixed by the existing store.

    Appends set ``align_chunks``: a month missing a timestep (a known-bad source
    file dropped by ``load_files``, intersected away by the region join) leaves
    the store an odd length, so the next month starts mid-chunk and xarray
    rejects the write. Aligning rechunks the incoming month onto the store's
    chunk grid instead.
    """
    kwargs = dict(consolidated=True)
    if mode is not None:
        kwargs["mode"] = mode
    if append_dim is not None:
        kwargs["append_dim"] = append_dim
        kwargs["align_chunks"] = True
    if encoding is not None:
        kwargs["encoding"] = encoding

    if append_dim is None and zarr_format is not None:
        try:
            ds.to_zarr(str(store_path), zarr_format=zarr_format, **kwargs)
            return
        except TypeError:
            # Older xarray uses zarr_version; older still ignores it entirely.
            try:
                ds.to_zarr(str(store_path), zarr_version=zarr_format, **kwargs)
                return
            except TypeError:
                pass

    ds.to_zarr(str(store_path), **kwargs)


def _build_encoding(ds, zarr_format, compression_level, precision="float32"):
    """
    Build an explicit, reproducible on-disk encoding for the store's first write.

    - Data variables: float32 with a zstd Blosc compressor (zarr v2) and a NaN
      fill value, so the layout is fully specified rather than relying on
      library defaults.
    - With ``precision='int16'``, the variables GLIDE gave a range for are
      written as int16 with CF scale_factor/add_offset and **no** fill value
      instead (see :func:`extract_um_met.glide.quantisation_encoding`). Anything
      without a range — the stresses, and every coordinate — stays as above.
    - Time coordinate: pinned CF units/calendar.

    Compression is only set for zarr v2 (numcodecs Blosc objects); for v3 the
    store keeps xarray/zarr defaults.
    """
    compressor = None
    if zarr_format == 2:
        try:
            import numcodecs

            compressor = numcodecs.Blosc(
                cname="zstd", clevel=compression_level, shuffle=numcodecs.Blosc.SHUFFLE
            )
        except Exception:
            compressor = None

    encoding = {}
    for v in ds.data_vars:
        enc = {}
        if precision == "int16" and v in glide.QUANT_RANGES:
            enc.update(glide.quantisation_encoding(v))
        elif np.issubdtype(ds[v].dtype, np.floating):
            enc["dtype"] = "float32"
            enc["_FillValue"] = np.float32(np.nan)
        if compressor is not None:
            enc["compressor"] = compressor
        if enc:
            encoding[v] = enc

    if "time" in ds.coords and np.issubdtype(ds["time"].dtype, np.datetime64):
        encoding["time"] = {
            "units": "seconds since 1970-01-01",
            "calendar": "proleptic_gregorian",
        }

    return encoding


@contextlib.contextmanager
def _quiet_missing_fill_value_warning():
    """
    Silence xarray's ``_FillValue`` warning for the int16 writes.

    Omitting ``_FillValue`` is deliberate (see
    :func:`extract_um_met.glide.quantisation_encoding`) and the period is checked
    for finiteness before it is written, so the warning is noise that would
    otherwise appear once per variable per append and read as a problem. The
    filter matches only that message, so nothing else is hidden.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=".*integer dtype without any _FillValue.*",
            category=xr.SerializationWarning,
        )
        yield


def _check_store_levels(ds, store_path):
    """
    Confirm an incoming period's levels match the store it is being appended to.

    A height ceiling (``max_level_m``) resolves against the Mk's ``level_height``
    ladder, so a mid-year Mk change whose ladder moved resolves to a different
    level set. Appending that would mix two vertical grids in one store; raise
    here, naming both selections, rather than let zarr fail on a shape mismatch
    far from the cause.
    """
    if "levels" not in ds.dims:
        return
    with xr.open_zarr(store_path, consolidated=True) as existing:
        store_levels = [int(v) for v in existing["levels"].values]
    levels = [int(v) for v in ds["levels"].values]
    if levels != store_levels:
        raise ValueError(
            f"cannot append: this period has {len(levels)} model levels "
            f"(top {levels[-1]}) but {store_path} holds {len(store_levels)} "
            f"(top {store_levels[-1]}). The level selection changed mid-store — "
            f"with max_level_m set, an Mk boundary whose level_height ladder moved "
            f"does this. Rebuild the year with --overwrite."
        )


def _check_store_wind_regrid(ds, store_path):
    """
    Confirm an incoming period's wind treatment matches the store it joins.

    A year is appended month by month, so flipping ``wind_vertical_regrid``
    partway would put two different definitions of u/v on one time axis. Unlike
    a level change this has no shape to give it away — the two are identical in
    shape, coordinates and dtype — so without this the store would carry a
    silent discontinuity at whichever month the config changed.

    Periods and stores written before the option existed carry no attribute and
    are snapped, so the default keeps an in-flight append working.
    """
    with xr.open_zarr(store_path, consolidated=True) as existing:
        store_mode = existing.attrs.get("wind_vertical_regrid", "snap")
    mode = ds.attrs.get("wind_vertical_regrid", "snap")
    if mode != store_mode:
        raise ValueError(
            f"cannot append: this period was built with wind_vertical_regrid="
            f"{mode!r} but {store_path} holds {store_mode!r}. The wind treatment "
            f"changed mid-store, which would leave u/v meaning two different "
            f"things on one time axis. Rebuild the year with --overwrite."
        )


def append_month_to_year_store(month_ds, store_path, zarr_format=2, first=False,
                               chunks=None, compression_level=None,
                               precision="float32"):
    """
    Append a monthly dataset to a yearly zarr store.

    Parameters
    ----------
    month_ds : xarray.Dataset
        The monthly domain dataset (already renamed to lat/lon/levels).
    store_path : str or pathlib.Path
        Path to the yearly zarr store.
    zarr_format : int, optional
        Zarr format version (2 or 3; default 2 for backward compatibility).
        Only applied when creating the store.
    first : bool, optional
        If True, create the store with ``mode='w'`` (setting the explicit
        encoding); otherwise append along time (encoding inherited from the store).
    chunks : dict, optional
        On-disk chunking, from :func:`resolve_output_chunks`. Defaults to
        :data:`_ZARR_CHUNKS`. Only meaningful on the first write — later appends
        inherit the layout the store was created with.
    compression_level : int, optional
        zstd level, from :func:`resolve_compression_level`. Defaults to
        :data:`_COMPRESSION_LEVEL`. Like ``chunks``, only meaningful on the
        first write.
    precision : str, optional
        ``'float32'`` (default) or ``'int16'``, from :func:`resolve_precision`.
        The encoding it selects is only set on the first write, but the range
        check it triggers runs on **every** write: each appended period is
        encoded through the store's fixed encoding, so one that drifts out of
        range would wrap silently.

    Returns
    -------
    str
        Path to the updated zarr store.
    """
    if precision == "int16":
        glide.check_quantisation_range(month_ds)

    ds = _rechunk(month_ds, chunks)

    with _quiet_missing_fill_value_warning():
        if first:
            encoding = _build_encoding(ds, zarr_format,
                                       compression_level or _COMPRESSION_LEVEL,
                                       precision)
            _to_zarr(ds, store_path, mode="w", zarr_format=zarr_format,
                     encoding=encoding)
        else:
            _check_store_levels(ds, store_path)
            _check_store_wind_regrid(ds, store_path)
            _to_zarr(ds, store_path, append_dim="time")

    return str(store_path)


def finalize_attrs(store_path, extra_attrs=None, chunks=None, compression_level=None,
                   precision="float32", output_profile=None):
    """
    Write provenance/completeness attributes onto a yearly zarr store.

    Reopens the store to read its actual time coverage, computes month
    completeness, sets provenance attributes on the root group, and
    re-consolidates the metadata. Best-effort: a failure here does not corrupt
    the already-written data.

    Parameters
    ----------
    store_path : str or pathlib.Path
        Path to the yearly zarr store.
    extra_attrs : dict, optional
        Additional attributes to record (e.g., grid_mode, native_grid_info).
    chunks : dict, optional
        The chunking the store was written with, recorded as ``zarr_chunks``.
        Defaults to :data:`_ZARR_CHUNKS`.
    compression_level : int, optional
        The zstd level the store was written with, recorded as
        ``zarr_compression``. Defaults to :data:`_COMPRESSION_LEVEL`.
    precision : str, optional
        The precision the store was written with, recorded as
        ``zarr_precision``.
    output_profile : str, optional
        The domain's ``output_profile``, recorded under that name when it has
        one, so a store says on its own face which consumer it was built for.

    Returns
    -------
    dict
        The attributes written (useful for logging/inspection).
    """
    store_path = str(store_path)

    with xr.open_zarr(store_path, consolidated=True) as ds:
        times = np.asarray(ds.time.values)
        months_present = sorted({str(t)[5:7] for t in times.astype("datetime64[s]")})
        sizes = dict(ds.sizes)

    missing_months = sorted({f"{i:02d}" for i in range(1, 13)} - set(months_present))

    attrs = {
        "created_by": getpass.getuser(),
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "n_lat": int(sizes.get("lat", -1)),
        "n_lon": int(sizes.get("lon", -1)),
        "n_time": int(sizes.get("time", -1)),
        "n_levels": int(sizes.get("levels", -1)),
        "time_start": str(times[0]),
        "time_end": str(times[-1]),
        "zarr_chunks": str(chunks or _ZARR_CHUNKS),
        # Data variables only: the coordinates are small and are left to
        # xarray/zarr's own default compressor.
        "zarr_compression": (
            f"blosc(cname=zstd, clevel={compression_level or _COMPRESSION_LEVEL}, "
            f"shuffle=SHUFFLE) on the data variables"
        ),
        "zarr_precision": (
            "int16 with CF scale_factor/add_offset over the ranges in each "
            "variable's own attributes; no _FillValue, so the store is validated "
            "as finite before every write"
            if precision == "int16" else "float32"
        ),
        "months_present": months_present,
        "n_months_present": len(months_present),
        "missing_months": missing_months,
        "year_complete": not missing_months,
    }
    if output_profile:
        attrs["output_profile"] = output_profile
    if extra_attrs:
        attrs.update(extra_attrs)

    try:
        import zarr

        group = zarr.open_group(store_path, mode="r+")
        group.attrs.update(attrs)
        try:
            zarr.consolidate_metadata(store_path)
        except Exception:
            # consolidate_metadata signature/behaviour varies across zarr
            # versions; the attrs are written regardless.
            pass
    except Exception as exc:
        print(f"WARNING: could not finalize store attributes on {store_path}: {exc}")

    return attrs
