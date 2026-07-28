"""
Zarr I/O utilities for yearly meteorology stores.

Appends monthly domain datasets to a yearly zarr store (one store per year,
appended along the time dimension), and finalizes provenance attributes once a
year's months are all written.

The default chunk layout keeps ``time=1`` (single-timestep scattered reads are
the dominant access pattern), groups levels in 3s, and keeps lat/lon whole. A
domain can override it with ``output_chunks:`` in config when its own access
pattern differs.
"""

import datetime
import getpass

import numpy as np
import xarray as xr


_ZARR_CHUNKS = {"time": 1, "lat": -1, "lon": -1, "levels": 3}

# The output schema's dims — the only keys a domain may chunk on.
CHUNK_DIMS = ("time", "levels", "lat", "lon")


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


def _rechunk(ds, chunks=None):
    """Apply the on-disk zarr chunking, using -1 (whole) for unlisted dims."""
    chunks = chunks or _ZARR_CHUNKS
    return ds.chunk({dim: chunks.get(dim, -1) for dim in ds.dims})


def _to_zarr(ds, store_path, mode=None, append_dim=None, zarr_format=2, encoding=None):
    """
    Write to zarr, tolerating xarray/zarr versions that don't accept zarr_format.

    ``zarr_format`` and ``encoding`` are only meaningful when creating the store
    (``mode='w'``); on append the format/encoding are fixed by the existing store.
    """
    kwargs = dict(consolidated=True)
    if mode is not None:
        kwargs["mode"] = mode
    if append_dim is not None:
        kwargs["append_dim"] = append_dim
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


def _build_encoding(ds, zarr_format):
    """
    Build an explicit, reproducible on-disk encoding for the store's first write.

    - Data variables: float32 with a zstd Blosc compressor (zarr v2) and a NaN
      fill value, so the layout is fully specified rather than relying on
      library defaults.
    - Time coordinate: pinned CF units/calendar.

    Compression is only set for zarr v2 (numcodecs Blosc objects); for v3 the
    store keeps xarray/zarr defaults.
    """
    compressor = None
    if zarr_format == 2:
        try:
            import numcodecs

            compressor = numcodecs.Blosc(
                cname="zstd", clevel=3, shuffle=numcodecs.Blosc.SHUFFLE
            )
        except Exception:
            compressor = None

    encoding = {}
    for v in ds.data_vars:
        enc = {}
        if np.issubdtype(ds[v].dtype, np.floating):
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


def append_month_to_year_store(month_ds, store_path, zarr_format=2, first=False,
                               chunks=None):
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

    Returns
    -------
    str
        Path to the updated zarr store.
    """
    ds = _rechunk(month_ds, chunks)

    if first:
        encoding = _build_encoding(ds, zarr_format)
        _to_zarr(ds, store_path, mode="w", zarr_format=zarr_format, encoding=encoding)
    else:
        _to_zarr(ds, store_path, append_dim="time")

    return str(store_path)


def finalize_attrs(store_path, extra_attrs=None, chunks=None):
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
        "months_present": months_present,
        "n_months_present": len(months_present),
        "missing_months": missing_months,
        "year_complete": not missing_months,
    }
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
