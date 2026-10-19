# Troubleshooting

Symptoms, what causes them, and what to do. Errors are grouped by the stage they
come from; the message quoted is the one the tool actually prints.

> **Check these three first.** Most reports come down to one of them:
>
> ```bash
> cd /home/users/$USER/extract_UM_met     # config.yaml is read from the CWD
> module purge && module load jaspy       # the supported Python environment
> export TMPDIR=/work/scratch-pw5/$USER/tmp && mkdir -p "$TMPDIR"
> ```

---

## 1. Config and domain

| Message / symptom | Cause and fix |
| ----------------- | ------------- |
| `config.yaml did not parse to a dictionary` | No `config.yaml`, or you are not in the repo root. `cp config.example.yaml config.yaml`, then `cd` to the repo root — see [setup.md §4](setup.md#4-create-your-configyaml). |
| `Domain 'X' not found by key or domain_name` | Typo, or the domain is not in your `config.yaml`. `--domain` takes either the key (`SA`) or the `domain_name` (`SOUTHAMERICA`). |
| `domain output_chunks: unknown dim(s) {...}` | Chunk keys are `time`, `levels`, `lat`, `lon` — not `latitude`/`longitude`. This raises at config load rather than silently ignoring you. |
| `domain output_chunks: chunk size for 'X' must be a positive integer or -1` | `-1` means "one chunk spanning the whole dim"; `0` and negatives other than `-1` are not valid. |
| `data_types override 'X' must be a list of ...` / `domain 'EUROPE' 'X' must be a list of ...` | A malformed `mass_vars` / `wind_vars` / `model_levels` block. The message names the block it came from, since both `data_types:` and a domain accept these keys. See [reference.md §2](reference.md#2-choosing-your-own-variables-and-levels) for the exact forms. |
| `... 'X': level(s) {...} out of range` | `model_levels` is **1-based** and must fit the source (L59 global, L70 NZCSM). |
| `... 'wind_vars': the winds are extracted as a u/v pair` | You named one wind. Give both or neither — u and v are rotated onto the mass grid together, so a lone one has nothing to rotate against. |
| `domain intermediate_period: unknown period 'X'` | Valid values are `month` (default), `week` and `day` — see [reference.md §5](reference.md#5-scratch-intermediates-and-peak-memory). Raised at config resolution rather than part-way through a run. |
| `Could not parse --date 'X'. Use YYYY, YYYYMM, or YYYYMMDD.` | Only those three forms. Also `Month out of range` / `Day out of range`. |
| `--date all is not supported for extraction` | The `extract` subcommand takes `YYYYMM` or `YYYYMMDD` only — no full-year form. Use `run` for a year. |

---

## 2. Archive and missing files

| Message / symptom | Cause and fix |
| ----------------- | ------------- |
| `No archive files matched: {pattern}` / `No files matched: {pattern}` | Usually `data_types.<type>.archive_directory` is wrong or unset. Check the printed pattern against what is actually on disk — it tells you the Mk folder and filename the tool expected. |
| `No Mk version found for year=…, month=…` | The date is outside the known Mk calendar: before 2011-01, or **past the newest block**, which happens when the archive gains a new Mk. Extend `iris_io.get_Mk` and `cli.MK_SAMPLE_TIMESTEPS` — see [concepts.md §2](concepts.md#2-the-global-um-archive-um_global). |
| `File not found: {filepath}` | A single expected file is absent. Known-bad files are skipped on load; a hard failure here means the archive is incomplete for that timestep. |
| `FileNotFoundError` from inside `iris.load`, for a file you *can* read from a login node | The node, not the archive: the archive entries are symlinks into `/badc`, which some nodes come up without. The glob matches the link, the open follows it and fails. Resubmit, and preflight long jobs — [known_problems §3](known_problems.md#3-environment-quirks). |
| `UM1p5km Mk calendar for YYYY-MM is not yet defined` | Only Mk4 (2017-07 onward) is confirmed for the 1.5 km UK source. Earlier boundaries are unknown — see the [roadmap](roadmap_and_contributing.md). |
| `uk16 region bounds are not generated yet` | `UM1p5km` is registered but not runnable: its 16 rotated-pole tiles have no saved true-lat/lon bounds yet. |

### Cube selection

```
expected exactly 1 cube for name='air_temperature' want_levels=True, found N.
Loaded cubes (name, has_levels): [...]
```

Selection is by **name + level presence**, so this is the deliberate loud failure
when the archive changes shape or an override is wrong. The message lists every
cube it did load — compare that against your `mass_vars` / `wind_vars` entries,
paying attention to the `has_levels` flag in each pair. Two `air_temperature`
cubes exist in the `I` set (1.5 m and model-level), which is exactly why the
level flag has to be right; see [reference.md §3](reference.md#3-raw-um-variable-inventory).

---

## 3. Grids and footprints

| Message / symptom | Cause and fix |
| ----------------- | ------------- |
| `Native grid not found for Mk N. Looked for: …` | `data/` is empty on a fresh clone — the grids are regenerable and git-ignored. Run `python -m extract_um_met make-native-grid` (or `--mk N` for just one). |
| `Footprint file not found: {path}` | Check `reference_footprints_directory` in config and the domain's `footprint:` filename. |
| `Footprint mode requires a 'footprint' key in the domain config` | A domain with no `grid:` block defaults to footprint mode, so it still needs a `footprint:`. Give it one, or set an explicit `grid.mode`. |
| `Cube does not have latitude/longitude coordinates` | The loaded cube is not on a recognisable lat/lon grid — usually a rotated-pole source reaching the regular-grid path. |
| `Unknown grid mode: global` | `mode: global` is `extract-topog` only; `run` and `extract` have no whole-globe path. Use `extract-topog --domain GLOBAL` — see [topography.md](topography.md). |
| `No ancillary_types.X.topog_by_mk entry for Mk N` | `extract-topog` on a year whose Mk has no ancillary mapped. Add the Mk to `topog_by_mk` — see [topography.md §4](topography.md#4-which-ancillary-each-mk-uses). |

---

## 4. Regions and joining

| Message / symptom | Cause and fix |
| ----------------- | ------------- |
| `source 'X' is tiled; a region is required` | `extract` on a tiled source needs `--region`, or omit it to do all of the domain's regions. |
| `source 'X' is not tiled (region_scheme='none'); per-region extract needs a region scheme` | You passed `--region` to a non-tiled source such as `NZCSM`, which has one file per timestep and no tiles. |
| `Missing intermediate for region N in {scratch}` | A per-region intermediate was expected but is absent — scratch was purged, or an earlier `extract` failed. A `run` regenerates missing regions automatically; re-run it. |
| `Region N {date}: {n_dup} duplicate timestamp(s)` | The archive returned repeated valid times for that region-month. Worth inspecting before trusting the month. |
| `more than one (forecast_reference_time, forecast_period) combination` | A timestep is ambiguous about which model run it came from, so the forecast provenance coords cannot be built. |
| `ERROR: X is a non-tiled domain (no world regions)` | From `launch_met_array.sh`: there is nothing to split across array tasks. Submit a single serial job instead — see [extracting.md §7](extracting.md#7-run-under-slurm). |
| `ERROR: X uses grid mode 'native', which is not safe to extract per-region in parallel` | `run` snaps a year to one canonical Mk grid while a standalone `extract` uses each month's own Mk, so parallel intermediates would land on different grids. Use a single serial job for native mode. |

---

## 5. Memory, disk and runtime

| Symptom | Cause and fix |
| ------- | ------------- |
| Node `/tmp` fills, or decompression fails | `TMPDIR` is not pointed at scratch. JASMIN's `/tmp` is a small tmpfs and is routinely full; Mk6–Mk9 are gzipped and must be decompressed somewhere. See [setup.md §3](setup.md#3-set-tmpdir-to-scratch). |
| Out-of-memory under SLURM | Extraction holds one period of one region in memory, so the first thing to reach for is a shorter `intermediate_period` — `week` needs roughly a quarter of `month`, `day` a thirtieth ([reference.md §5](reference.md#5-scratch-intermediates-and-peak-memory)). Also raise `--mem`, run a month at a time rather than a year, and leave `--keep-intermediates` off. |
| Dates from 2017-07 much slower and larger | **Not a fault.** Mk10 onward is N1280 — ~1.67× finer per axis, ~2.8× the points per level. Budget time and disk accordingly ([concepts.md §2](concepts.md#2-the-global-um-archive-um_global)). |
| Scratch filling up during a run | Per-region intermediates live in `{scratch}/files/` and are deleted after each month appends — unless `--keep-intermediates` is set. Check for leftovers from a failed run. |
| Long run killed on the login node | Extraction is memory-heavy; submit it with `sbatch` instead. See [extracting.md §7](extracting.md#7-run-under-slurm). |

---

## 6. Stores, resume and appending

| Symptom | Cause and fix |
| ------- | ------------- |
| "cannot backfill earlier missing month" | Appending must preserve time order, so a missing *earlier* month cannot be filled in later. Rebuild that year with `--overwrite`. |
| Re-running a year does nothing | Expected — months already present are skipped and only the provenance attrs are re-finalised. Use `--overwrite` to force a rebuild. |
| A variant run overwrote the production store | Use `--suffix TAG` so the variant lands beside it as `{DOMAIN}_{TAG}_Met_{year}.zarr`. Each suffix keeps its own resume state and its own scratch intermediates. |
| `output_chunks` change had no effect | Chunking is fixed when a store is first created; later months inherit it. Rebuild the year with `--overwrite`. |
| Store unreadable in another environment | `zarr_format: 2` is readable by both zarr 2.18.7 and 3.x; `3` is 3.x only. Check what the consumer supports. |
| Year looks short | Check `ds.attrs["year_complete"]` and `ds.attrs["missing_months"]` before anything else. |

---

## 7. Output that looks wrong

These are known limitations rather than crashes — they produce data, just not the
data you might expect. All are tracked in the [roadmap](roadmap_and_contributing.md).

- **A domain is a cell bigger than its bounds on each edge.** That is deliberate:
  `lat_bounds`/`lon_bounds` are bracketed, so the box you ask for falls inside
  the grid you get rather than a fraction of a cell outside it (see
  [concepts](concepts.md#4-grid-modes)). Ask for bounds that land on grid points
  if you want the extraction tight.
- **NZCSM artifacts at the domain edges.** Extracted New Zealand data shows edge
  artifacts, most likely from rotated-pole → regular lat/lon regridding at the
  boundary (extrapolation into partial-coverage cells) and/or dateline handling,
  since the domain crosses 180°. Inspect edge cells against the native grid
  before treating NZ output as usable.
- **A year spanning an Mk boundary mixes resolutions in `native` mode.** The year
  snaps to one canonical Mk grid (the majority by month count) and off-resolution
  months are interpolated onto it. Footprint and regular modes always interpolate
  onto a fixed target, so they never see this.

---

## Still stuck?

Re-run with `--dry-run` — it resolves config, domain and dates and reports what it
*would* do without writing anything, which separates configuration problems from
data problems:

```bash
python -m extract_um_met run --domain SA --date 201601 --dry-run
```

Then narrow to the cheapest real run — a single day writes a small standalone
debug store and never touches your yearly stores:

```bash
python -m extract_um_met run --domain SA --date 20160115
```

To isolate one region, use `extract` with `--region` and inspect the intermediate
it leaves in `{scratch}/files/`.
