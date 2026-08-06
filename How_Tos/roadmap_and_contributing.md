# Roadmap and contributing

Remaining work and open questions for the `extract_um_met` package, and how to
raise something. This is the **planning** document — for how to install,
configure and run the tool see [setup.md](setup.md) and
[extracting.md](extracting.md), and for the codebase map
[codebase.md](codebase.md).

---

## Still to do

### Data types
- **UM1p5km (1.5 km UK LimitedArea):** the source is registered but its `uk16`
  region bounds are not generated yet — the 16 tiles are on a rotated-pole grid.
  Generalise `make-native-grid` to derive and save their true-lat/lon bounds
  under `data/UM1p5km/`, then wire the join. Confirm the Mk2/Mk3 date boundaries
  (only Mk4 from 2017-07 is currently confirmed).
- **NZCSM — merge [PR #5](https://github.com/elenafillo/extract_UM_met/pull/5)
  first.** Kirstin Gerrand's `add_topog` branch carries most of the outstanding
  New Zealand work:
  - two NZCSM extraction fixes — `_build_time_axis` broadcasts a scalar (or
    otherwise non-time-dimensioned) `forecast_period` / `forecast_reference_time`
    onto the time dimension instead of assuming both stack, and the non-tiled run
    path skips a day with no archive files rather than failing the whole year;
  - `extract-topog`, a static ancillary field: a native-grid (rotated-pole)
    topography `.pp` regridded through `rotated.py` onto the *same* target grid
    met extraction builds for the domain, so the two line up pixel for pixel.
    Output is `{ancillary_save_directory}/{domain}/{domain}_topog.nc`,
    configured by a new `ancillary_types:` block keyed by data type.

  It branched before the package rename (`met_extract` → `extract_um_met`) and
  the docs restructure, so it needs porting onto both rather than a straight
  merge.
- **NZCSM, after that PR:** point `data_types.NZCSM.archive_directory` at the
  real archive once it exists (samples currently under `pp_data/`). Add the
  deferred stress fields (`surface_downward_*_stress`) — they are rotated-grid
  vectors needing `rotate_winds` combined with interval-end averaging (the
  hardest case).
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

### Add tests!

### DIY Testing & validation
- Single-month smoke test: `run --domain SA --date 201601`; open the store and
  check dims, vars, `time` coverage, provenance attrs.
- Append correctness: full year, confirm 12 months; re-run confirms resume;
  `--overwrite` confirms rebuild.
- Format round-trip: read a `zarr_format=2` store from both zarr 2.18.7 and
  zarr 3.x environments.
- Value parity: compare one (domain, month) against a known-good monthly NetCDF.
- Grid modes: footprint vs native on the same small domain (native → native
  lat/lon, no interp; footprint → today's grid).
- SLURM deployment: submit one (domain, year) through
  `scripts/launch_met_array.sh`; confirm the store lands in
  `zarr_save_directory`.

---

## Open questions & assumptions

- **Mk resolution changes** happen at mid-year boundaries (2013-05, 2014-07,
  2015-08, 2017-07, 2022-06). Native mode snaps a year to one canonical Mk grid —
  the majority by month count — and interpolates the off-resolution months. It
  should arguably default to the coarsest grid in the year instead; and if
  adjacent Mk native grids turn out to be nested, those months could use `.sel()`
  rather than interpolation.
- **Zarr format** is `zarr_format=2` by default, readable by both zarr 2.18.7 and
  3.x. Switch to `3` only once all consumers are on zarr 3.x.
- **Variable selection** excludes the 1.5 m `air_temperature`, for exact parity
  with the original script. Selection is by name + level presence, so an archive
  or iris-ordering change raises a clear error rather than mis-selecting.
- **Parallel extraction and `native` mode do not mix.** `run` snaps a year to one
  canonical Mk grid while a standalone `extract` uses each month's own Mk, so
  per-region array tasks would land on different grids and the join would produce
  a mixed-grid store. `launch_met_array.sh` refuses native domains for this
  reason; making the two work together means teaching `extract` the year's
  canonical grid.

---

## Found a problem, or want something added?

Open an issue at
[github.com/elenafillo/extract_UM_met/issues](https://github.com/elenafillo/extract_UM_met/issues/new).

Before you do, [Troubleshooting](troubleshooting.md) covers the errors that have
a known fix, and [Known problems](known_problems.md) lists the defects and file
quirks already identified.

For a bug, the useful things to include are the command you ran, the domain and
date, the relevant `config.yaml` block, and the error or the log around where it
went wrong.

## Credits

`extract_um_met` is written by Elena Fillola using Claude Opus 5 and by Jeff Clark, as
part of research at the [Atmospheric Chemistry Research Group](https://www.bristol.ac.uk/chemistry/research/acrg/)
at the University of Bristol.
