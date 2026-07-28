# extract_um_met — Development Roadmap

Status, remaining work, and open questions for the `extract_um_met` package. This
file is the **planning** document; for how to install, configure, and run the
tool, and for the codebase map, see [`How_Tos/`](https://github.com/elenafillo/extract_um_met/tree/zarr_stores/How_Tos/):

- [how_to_setup.md](https://github.com/elenafillo/extract_um_met/blob/zarr_stores/How_Tos/how_to_setup.md) — environment & config
- [how_to_datatypes.md](https://github.com/elenafillo/extract_um_met/blob/zarr_stores/How_Tos/how_to_datatypes.md) — data types, grids, regions
- [how_to_extract.md](https://github.com/elenafillo/extract_um_met/blob/zarr_stores/How_Tos/how_to_extract.md) — the CLI
- [how_to_code.md](https://github.com/elenafillo/extract_um_met/blob/zarr_stores/How_Tos/how_to_code.md) — module map & pipeline

---

## Status snapshot

**Working end to end (Global UM):** config → per-region extraction → join →
yearly Zarr append, with resume, three grid modes, single-day smoke test,
`make-native-grid`, and CF/provenance metadata. Global Mk6–Mk12 (2011-01 onward)
are extractable. Mk10 onward is N1280 (0.09375° × 0.140625°), roughly 2.8× the
points of Mk8/9, so post-2017-07 runs are correspondingly heavier.

**Data-type abstraction landed:** the pipeline dispatches on a `MetSource`
descriptor (`extract_um_met/sources.py`) instead of hardcoding the global UM.
`UM_Global`, `UM1p5km`, and `NZCSM` are registered; archive paths are set per
environment in `config.yaml → data_types:`.

**Recently completed**
- CF / ARCO metadata (`metadata.py` + the config `metadata:` block); pinned
  zstd Blosc compression + CF `time` encoding; empty config fields omitted.
- `forecast_reference_time(time)` / `forecast_period(time)` retained as
  time-indexed provenance coords (guarded against ambiguous timestamps).
- `delta_lat` / `delta_lon` global attributes (mean grid spacing).
- Single-day extraction (`--date YYYYMMDD`) → standalone debug store.
- Time-averaged surface fields (sensible-heat flux + stresses) extracted as
  3-hour means, re-stamped to the interval end to align with instantaneous
  fields (12-variable output for `UM_Global`).
- Name-based cube selection (`_pick`) — raises on a changed archive instead of
  silently mis-selecting.

---

## Still to do

### Data types
- **UM1p5km (1.5 km UK LimitedArea):** the source is registered but its `uk16`
  region bounds are not generated yet — the 16 tiles are on a rotated-pole grid.
  Generalise `make-native-grid` to derive and save their true-lat/lon bounds
  under `data/UM1p5km/`, then wire the join. Confirm the Mk2/Mk3 date boundaries
  (only Mk4 from 2017-07 is currently confirmed).
- **NZCSM:** point `data_types.NZCSM.archive_directory` at the real archive once
  it exists (samples currently under `pp_data/`). Add the deferred stress fields
  (`surface_downward_*_stress`) — they are rotated-grid vectors needing
  `rotate_winds` combined with interval-end averaging (the hardest case).
- **NZCSM extraction artifacts:** the extracted NZ data shows artifacts,
  particularly at the domain edges — needs investigating. Likely candidates: the
  rotated-pole → regular lat/lon regridding at the boundary (extrapolation /
  partial-coverage cells beyond the native grid), and/or the dateline handling
  (the domain crosses 180°, longitudes in 0–360). Inspect the edge cells against
  the native grid before treating NZ output as usable.
- Save each data type's grid/coverage/resolution metadata under `data/<name>/`
  (mirroring `Global`) so provenance is per-source.

### Extraction correctness
- **Native-grid cropping gap:** in `native` mode, extraction passes each region
  through at its full extent (sliced to region bounds only), so `lat_bounds` /
  `lon_bounds` in a native `grid:` block currently shape only the canonical
  target grid, not the extracted output. Add a final crop to those bounds so a
  native domain matches its footprint-mode counterpart in size.
- **Single-region fast path:** add a direct extract → setup path (skip the join)
  for domains that map to one world region. `join_month` already handles a
  1-region domain as a trivial concat, but a direct path would avoid the join
  machinery.

### Packaging & deployment
- `environment.yml` — pin the tested stack (Python 3.12, xarray 2025.7.0, dask
  2025.5.1, zarr 3.0.10, iris 3.12.2, numpy 2.2.6). Today the supported
  environment is JASMIN's `jaspy` module.
- `scripts/` SLURM templates using the new CLI:
  `run_year.slurm` (array over domain/year) and `make_native_grid.slurm`. The
  root `run_*_job.txt` files are the deprecated pre-refactor originals.
- Remove/shim the legacy root files once callers are migrated:
  `met_functions.py`, `satellite_met_1b1_fixed_v3.py`, `satellite_met_join_v2.py`
  (+ `copy`), `data_utils/convert_met_to_zarr.py`, `Untitled.ipynb`, the old job
  `.txt` files.

---

## Roadmap phases

### Phase 1 — Core extraction & joining ✓ DONE
`extract.py`, `join.py`, `metadata.py`, `zarr_io.py`, and the
`run`/`extract`/`make-native-grid` CLI are implemented and wired together, with
name-based cube selection, CF/provenance metadata, and the forecast provenance
coords.

### Phase 2 — Documentation & deployment  (in progress)
- ✓ Rewrite `README.md`; add the `How_Tos/` guides.
- ☐ `environment.yml` (pin producer versions).
- ☐ SLURM templates under `scripts/`.
- ☐ Clean up the legacy root files.

### Phase 3 — Testing & validation
- ☐ Single-month smoke test: `run --domain SA --date 201601`; open the store and
  check dims, vars, `time` coverage, provenance attrs.
- ☐ Append correctness: full year, confirm 12 months; re-run confirms resume;
  `--overwrite` confirms rebuild.
- ☐ Format round-trip: read a `zarr_format=2` store from both zarr 2.18.7 and
  zarr 3.x environments.
- ☐ Value parity: compare one (domain, month) against a known-good monthly
  NetCDF.
- ☐ Grid modes: footprint vs native on the same small domain (native → native
  lat/lon, no interp; footprint → today's grid).
- ☐ SLURM deployment: submit one (domain, year); confirm the store lands in
  `zarr_save_directory`.

---

## Open questions & assumptions

- **Mk resolution changes** at mid-year boundaries (2013-05, 2014-07, 2015-08,
  2017-07, 2022-06). Native-mode extraction snaps a year to one canonical Mk grid
  (majority by month count) and interpolates only off-resolution months.
  *Open check:* whether adjacent Mk native grids are nested, which would allow
  `.sel()` for off-res months instead of interpolation.
- **Zarr format:** write `zarr_format=2` by default (readable by both zarr 2.18.7
  and 3.x); switch to `3` only once all consumers are on zarr 3.x.
- **Per-region intermediates** are single-file NetCDF in `{scratch}/files/`
  (transient; deleted after the month appends unless `--keep-intermediates`).
  NetCDF over zarr here is deliberate — one file per region is cheaper on shared
  scratch and easy to inspect; their purpose is to bound peak memory, not to be a
  product.
- **Variable selection** excludes the 1.5 m `air_temperature` (exact parity with
  the original script). Selection is by name + level presence, so an archive or
  iris-ordering change raises a clear error rather than mis-selecting.
</content>
