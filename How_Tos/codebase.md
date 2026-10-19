# How the Code Works: structure & pipeline

A map of the codebase: the file layout, what each module does, and how a single
`run` flows through them. Read this if you want to modify the pipeline (add a data
type, a grid mode, or a variable) rather than just use it.

> **See also:** [setup.md](setup.md), [concepts.md](concepts.md),
> [extracting.md](extracting.md), [reference.md](reference.md).

---

## 1. Repo layout

```
extract_UM_met/                    # ← the repo
├── extract_um_met/                # ← the package (all current logic)
│   ├── __init__.py             # package metadata (version)
│   ├── __main__.py             # entry point: `python -m extract_um_met`
│   ├── cli.py                  # argparse CLI + run/extract/make-native-grid orchestration
│   ├── config.py               # config loading, {user} templating, Config accessor
│   ├── sources.py              # MetSource data-type descriptors + registry
│   ├── periods.py              # split a month into intermediate-file periods
│   ├── iris_io.py              # reading .pp/.pp.gz with iris; Mk calendar; cube selection
│   ├── regions.py              # world-14 region bounds & domain-grid trimming
│   ├── grid.py                 # build target grid (footprint/regular/native); native-grid I/O
│   ├── rotated.py              # rotated-pole regridding + wind rotation (UM1p5km, NZCSM)
│   ├── staggering.py           # vertical de-staggering of u/v onto the theta levels
│   ├── extract.py              # per-region + single-file extraction
│   ├── join.py                 # stitch regions → domain dataset for one period
│   ├── topog.py                # static orography ancillary (`extract-topog`)
│   ├── metadata.py             # CF / provenance attributes
│   └── zarr_io.py              # append to yearly zarr store + finalize attrs
│
├── How_Tos/                    # documentation sources (these pages)
├── docs/                       # Sphinx config + one-line include stubs → the site
├── scripts/                    # SLURM launchers and job scripts
├── data/                       # saved native grids (git-ignored, regenerable)
├── config.example.yaml         # committed config template
├── config.yaml                 # your personal config (git-ignored)
├── check_met_files.py          # standalone archive-coverage checker
└── README.md                   # overview + pointers
```

A working checkout usually holds more than this — `logs/`, `pp_data/`,
`config.yaml` and local notebooks are git-ignored by design. Anything you find in
the root that is not listed above is not part of the repo; the supported
workflow is `python -m extract_um_met`.

---

## 2. The package, module by module

### Entry & configuration

- {src}`__init__.py <extract_um_met/__init__.py>` — package metadata; holds
  `__version__`.
- {src}`__main__.py <extract_um_met/__main__.py>` — makes
  `python -m extract_um_met …` work; just calls `cli.main()`.
- {src}`cli.py <extract_um_met/cli.py>` — the command layer. Defines the `run`,
  `extract`, `make-native-grid` and `extract-topog` subcommands (argparse),
  parses `--date` into year/month/day periods, and orchestrates the workflow:
  resume logic (which days are already in a store, via `_read_present_days`), the
  single-day debug path (`_run_single_day`), and the non-tiled single-file path
  (`_run_non_tiled`). It is the only module that stitches the others together.
- {src}`config.py <extract_um_met/config.py>` — `load_config()` reads
  `config.yaml` from the CWD and fills `user` from `$USER`;
  `resolve_config_value()` expands `{user}` templates; the `Config` class wraps
  the dict with `get()`, `get_domain()`, and `resolve_domain_name()` (accepts a
  key like `SA` or a `domain_name` like `SOUTHAMERICA`).

### The data-type model

- {src}`sources.py <extract_um_met/sources.py>` — the abstraction that lets one
  pipeline serve very different products. `MetSource` is a frozen dataclass
  describing a data type on every axis (archive path, filename template, grid
  type, region scheme, tiled?, level count, cadence, compression, Mk calendar,
  which variable groups, …). The `SOURCES` registry holds `UM_Global`,
  `UM1p5km`, `NZCSM`; `get_source(name, cfg)` returns one with the `data_types:`
  overrides applied, and `get_domain_source(domain_cfg, cfg)` layers a domain's
  own variable groups, levels and `drop_vars` on top of that. It also owns the Mk
  calendars, the region-scheme lookup, level subsampling (`levels()`), and
  filename globbing (`list_files`).
  **This is the data model only — it reads no cubes.**
- {src}`periods.py <extract_um_met/periods.py>` — splits a month into the chunks
  that become intermediate files. `resolve_intermediate_period()` validates the
  domain's `intermediate_period` (`month` default, `week`, `day`) at config
  resolution; `period_chunks()` returns the day keys per chunk; `period_tag()`
  names one (`YYYYMM` for a whole month, so existing monthly scratch stays
  reusable, else the first day `YYYYMMDD`). Periods never cross a month
  boundary, which keeps one Mk per period. Because extraction holds one period
  of one region at a time, this is what sets the pipeline's peak memory.

### Reading & gridding

- {src}`iris_io.py <extract_um_met/iris_io.py>` — turns archive paths into iris
  cubes. `get_Mk(year, month)` is the canonical Mk-boundary calendar.
  `load_files()` is the source-agnostic loader: decompresses `.pp.gz` into
  scratch (reusing what's already there), reads `.pp` directly, skips known-bad
  files. `delete_iris()` cleans up unzipped scratch; `remove_coord_callback`
  strips the `um_version` attr on load. Also the cube-selection helpers the
  pipeline shares: `_pick()` (by name + level presence) and
  `_theta_ladder_cube()` (the mass cube whose `level_height` is the theta
  ladder) — here rather than in `extract.py` so `staggering.py` can use them
  without an import cycle.
- {src}`regions.py <extract_um_met/regions.py>` — the `world14` scheme:
  `get_saved_region_bounds()` (the 14 region boxes), `find_overlapping_regions()`
  (which regions a bbox needs), `build_domain_grid()` (trim the global 3×4
  region grid to a domain), `drop_duplicate_coords()` and `get_edge_size()`.
- {src}`grid.py <extract_um_met/grid.py>` — `build_target_grid(domain_cfg, cfg,
  mk)` dispatches on `grid.mode` → footprint (pad a reference footprint),
  regular (`np.arange` mesh), or native (load the saved native grid, subset to
  bounds). Also the native-grid I/O used by `make-native-grid`:
  `extract_native_grid()`, `save_native_grid()`, `load_native_grid()`,
  `get_mk_native_resolution()`.
- {src}`rotated.py <extract_um_met/rotated.py>` — for rotated-pole sources
  (UM1p5km, NZCSM). Builds a regular lat/lon target cube, regrids
  rotated→regular with iris, and rotates grid-relative wind/stress vectors to
  true north (`rotate_winds_true_north`) before regridding. Records rotated-pole
  provenance (`rotated_pole_attrs`).
- {src}`staggering.py <extract_um_met/staggering.py>` — the **vertical** half of
  aligning the winds, which `rotated.py`/`extract.py` only ever did
  horizontally. The UM is Charney–Phillips staggered: u/v sit on rho levels, the
  midpoints of the theta levels everything else is on.
  `destagger_winds_onto_theta()` moves them with a 2-point weighted mean of the
  bracketing rho levels, returning the cubes **and** the level selection with the
  now-unproducible top level removed; `rho_ladder()` / `rho_ladder_attrs()`
  capture the winds' native ladder for the store attributes, in both modes.
  Weights come from the cubes' own `level_height`, so no ladder is hardcoded.
  Selected per domain by `wind_vertical_regrid` — see
  [reference §2](reference.md#wind-levels-wind_vertical_regrid).

### The pipeline steps

- {src}`extract.py <extract_um_met/extract.py>` — the numerical core.
  `extract_region()` extracts one world region of a tiled source;
  `extract_single()` extracts a whole-domain single-file source (no regions).
  Both: pick cubes **by name + level presence** (`_pick`, so a changed archive
  raises instead of mis-selecting), align staggered winds onto the mass grid
  horizontally (and vertically too, if the domain sets
  `wind_vertical_regrid: interpolate` — see `staggering.py`),
  keep 1-in-3 model levels, re-stamp 3-hour time-mean fields to the interval end
  (`_restamp_to_interval_end`), build a real `time` dim, regrid onto the target
  (or pass through in native mode), and slice to bounds. `extract_region` always
  writes a per-region **zarr** intermediate to scratch, covering the days of one
  period. `extract_single` only writes (a single whole-domain zarr store) when
  called with `save=True`, which is the `extract` subcommand; under `run` it
  returns the Dataset in memory and the non-tiled path appends it straight to the
  store, so a normal non-tiled run touches scratch for nothing but decompressed
  `.pp`. Every met intermediate is zarr, written in the config's `zarr_format` so
  scratch and the yearly store never straddle two formats.
- {src}`join.py <extract_um_met/join.py>` — `join_month(…, days=None)` ensures
  each region's intermediate exists for the period (calling `extract_region` for
  any missing), then stitches: concat regions in a longitude column along
  latitude, then concat the columns along longitude; fill 1-cell seams
  (`fill_join_seams`); normalise/dedupe coords (`normalize_coords`); rename to
  the `lat`/`lon`/`levels` schema; stamp CF + delta metadata. Returns the
  period's domain `xr.Dataset`. `cleanup_region_intermediates()` deletes the
  scratch stores afterwards.
- {src}`topog.py <extract_um_met/topog.py>` — the `extract-topog` product, the one
  output that is not a met store. `extract_topog()` picks the Mk's
  `surface_altitude` ancillary (`_resolve_topog_file`), normalises longitude to
  −180…180, then lands it on the same target grid the met run builds for that
  domain — interpolated in footprint/regular mode, subset with an explicit
  tolerance in native mode — so the two match cell for cell. `mode: global` skips
  the target grid entirely and writes the whole native globe. Output is
  `{domain}_topog_{year}.nc`; see [topography.md](topography.md).
- {src}`metadata.py <extract_um_met/metadata.py>` — `apply_cf_metadata()` writes
  the CF coordinate attrs and the global attrs
  (Conventions/title/institution/source/… from the config `metadata:` block,
  empty fields omitted, plus an auto processing comment). `to_zarr_schema()` and
  `add_delta_attrs()` are the shared rename/attr helpers used by both the tiled
  and non-tiled paths.
- {src}`zarr_io.py <extract_um_met/zarr_io.py>` — `append_month_to_year_store()`
  writes the first period with `mode='w'` and an explicit pinned encoding
  (`_build_encoding`: float32, NaN fill, zstd Blosc, CF `time` units) and appends
  later ones along `time`; `resolve_output_chunks()` validates a domain's
  `output_chunks`. `finalize_attrs()` reopens the store to record
  provenance/completeness (`months_present`, `missing_months`, `year_complete`,
  `time_start/end`, …) and re-consolidates metadata.

---

## 3. The processing pipeline

A tiled-source `run` (e.g. `--domain SA --date 2016`) flows top to bottom:

```
cli.cmd_run
  │  resolve domain (config.Config) · pick data type (sources.get_source)
  │  parse --date → months · build target grid (grid.build_target_grid)
  │  periods.resolve_intermediate_period → periods.period_chunks
  │  skip periods whose days are already in the store (_read_present_days)
  │
  └─ for each year → for each month → for each period:
       join.join_month(days=…)
         │  for each region needing it:
         │    extract.extract_region
         │        iris_io.load_files ── read .pp(.gz), decompress to scratch
         │        _pick cubes by name · align winds (rotated.* if rotated-pole)
         │        keep 1-in-3 levels · build time dim · regrid to target · slice
         │        → write per-region zarr to {scratch}/files/
         │  stitch regions (concat lat within lon columns, then lon)
         │  fill seams · rename → lat/lon/levels (metadata.to_zarr_schema)
         │  metadata.apply_cf_metadata (CF + provenance from config)
         │  → return the period's xr.Dataset
         │
       zarr_io.append_month_to_year_store   (first period mode='w' + encoding;
       │                                      later ones append along time)
       join.cleanup_region_intermediates    (unless --keep-intermediates)
  finalize: zarr_io.finalize_attrs           (completeness/provenance attrs)

OUTPUT: {zarr_save_directory}/{DOMAIN}/{DOMAIN}_Met_{YYYY}.zarr
```

Variations handled in `cli.py`:

- **Single day** (`--date YYYYMMDD`) → `_run_single_day`: one day, into a separate
  `{DOMAIN}_Met_{YYYYMMDD}.zarr` debug store, always a fresh write.
- **Non-tiled source** (e.g. NZCSM) → `_run_non_tiled`: no join;
  `extract.extract_single` regrids the whole-domain file and appends a day-batch
  at a time so the big timesteps never all sit in memory.

---

## 4. Where to change things

| To… | Edit |
| --- | ---- |
| Add a **data type** (new product/archive) | add a `MetSource` to `SOURCES` in {src}`sources.py <extract_um_met/sources.py>`; set its archive path in `config.yaml → data_types:` |
| Add a **grid mode** | extend `build_target_grid` and add a `_build_*_grid` in {src}`grid.py <extract_um_met/grid.py>` |
| Change **which variables** are kept | the `*_vars` groups on the domain in `config.yaml`, or under `data_types:` for every domain on that archive (no code) — defaults on the `MetSource` in {src}`sources.py <extract_um_met/sources.py>` |
| Change **level subsampling** | `level_stride` / `model_levels`, on the domain or under `data_types:` (no code) — defaults on the `MetSource` |
| Change **where u/v sit vertically** | `wind_vertical_regrid` on the domain in `config.yaml` (no code) — logic in {src}`staggering.py <extract_um_met/staggering.py>`, see [reference §2](reference.md#wind-levels-wind_vertical_regrid) |
| Change a store's **output chunking** | `output_chunks` on the domain in `config.yaml` (no code) — default `_ZARR_CHUNKS` in {src}`zarr_io.py <extract_um_met/zarr_io.py>` |
| Change **peak memory** of a run | `intermediate_period` on the domain in `config.yaml` (no code) — logic in {src}`periods.py <extract_um_met/periods.py>` |
| Add a **region scheme** (e.g. the UK-16 tiles) | implement it in `_REGION_SCHEMES` ({src}`sources.py <extract_um_met/sources.py>`) + {src}`regions.py <extract_um_met/regions.py>` |
| Change **output encoding** (dtype, compressor) | {src}`zarr_io.py <extract_um_met/zarr_io.py>` (`_build_encoding`) |
| Change **CF / provenance attrs** | {src}`metadata.py <extract_um_met/metadata.py>` + the `metadata:` config block |
| Add a **CLI option / subcommand** | {src}`cli.py <extract_um_met/cli.py>` |
| Add a **domain** | `config.yaml → domains:` only (no code) — see [extracting.md](extracting.md#6-add-a-new-domain) |

## 5. Other tracked code

- {src}`check_met_files.py` — standalone archive-coverage checker; walks the
  archive and reports missing or unreadable files, independently of the package.
- {src}`docs/conf.py` — Sphinx configuration for this site. `GITHUB_REPO` and
  `GITHUB_BRANCH` there build every source link on this page, so a branch change
  is a one-line edit.
- `scripts/` — SLURM launchers: {src}`launch_met_array.sh <scripts/launch_met_array.sh>`
  (parallel per-region array + dependent join), {src}`launch_met.sh <scripts/launch_met.sh>`
  (serial single job), and the two job scripts they submit,
  {src}`extract_region.sbatch <scripts/extract_region.sbatch>` and
  {src}`join_year.sbatch <scripts/join_year.sbatch>`.

Open questions and planned work are tracked in
[roadmap_and_contributing.md](roadmap_and_contributing.md).
