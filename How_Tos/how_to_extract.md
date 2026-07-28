# How To: Extract Meteorology (the command line)

How to drive the `extract_um_met` CLI: the three subcommands, their options, the
extract → join → append workflow, resume behaviour, adding a domain, and running
under SLURM.

> **Before this:** finish [how_to_setup.md](how_to_setup.md) (env + config) and
> skim [how_to_datatypes.md](how_to_datatypes.md) (data types, grids, domains).

**Every command assumes:**

```bash
cd /home/users/$USER/extract_um_met     # config.yaml is read from here
module purge && module load jaspy
export TMPDIR=/work/scratch-pw5/$USER/tmp && mkdir -p "$TMPDIR"
```

---

## 1. The three subcommands

```
python -m extract_um_met <command> [options]
```

| Command | Does | Use when |
| ------- | ---- | -------- |
| **`run`** | extract → join → append to yearly zarr store (with resume) | the main workflow — producing data |
| **`extract`** | write per-region NetCDF intermediates only | debugging / retrying a flaky region |
| **`make-native-grid`** | save native UM grids to `data/` | one-off setup; before `native` mode |

Run any command with `-h` to see its options, e.g. `python -m extract_um_met run -h`.

---

## 2. `run` — the main workflow

For each month it extracts every world region, joins them into the domain, and
appends the month to the yearly store; then finalises provenance attributes.

```bash
python -m extract_um_met run --domain SA --date 2016          # full year
python -m extract_um_met run --domain SA --date 201601        # one month
python -m extract_um_met run --domain SA --date 20160115      # single day → debug store
python -m extract_um_met run --domain SA --date 2016 --overwrite       # rebuild
python -m extract_um_met run --domain SA --date 201601 --grid-mode native
python -m extract_um_met run --domain SA --date 201601 --dry-run
```

### `run` options

| Option | Meaning |
| ------ | ------- |
| `--domain` *(required)* | domain key (`SA`) **or** `domain_name` (`SOUTHAMERICA`) |
| `--date` *(required)* | `YYYY` (full year) · `YYYYMM` (month) · `YYYYMMDD` (single day) |
| `--overwrite` | rebuild the year from scratch (default: resume) |
| `--dry-run` | resolve config/domain/dates and report; write nothing |
| `--grid-mode {footprint,regular,native}` | override the domain's config grid mode |
| `--zarr-format {2,3}` | on-disk zarr version (default: `config.zarr_format`) |
| `--keep-intermediates` | keep per-region NetCDFs in scratch (default: delete after append) |
| `--suffix TAG` | tag the store as `{DOMAIN}_{TAG}_Met_...` so a variant sits beside the main store (see below) |

### The `--date` forms

- **`YYYY`** — all 12 months into `{DOMAIN}_Met_{YYYY}.zarr`.
- **`YYYYMM`** — that month, appended to the yearly store.
- **`YYYYMMDD`** — a single day into a **separate** `{DOMAIN}_Met_{YYYYMMDD}.zarr`
  debug store (always a fresh write; never touches the yearly stores). The cheap
  end-to-end smoke test — ~8 files per region.

### Resume semantics

For an existing yearly store (no `--overwrite`):

- Months already present are **skipped**; only later months are appended.
- Backfilling an **earlier** missing month via append is **not possible** (time
  order must be preserved). The run warns and you rebuild that year with
  `--overwrite`.
- Re-running a complete year is a no-op (it just re-finalises attrs).

Example — first pass does Jan–Jun, later you run the full year: Jan–Jun are
skipped, Jul–Dec appended.

### `--suffix` — variant stores side by side

By default a domain's store is `{DOMAIN}/{DOMAIN}_Met_{date}.zarr`. Passing
`--suffix TAG` tags the **filename** (not the folder) so a variant run lands next
to the main store instead of resuming/overwriting it:

```bash
python -m extract_um_met run --domain NA --date 2014                       # NA/NA_Met_2014.zarr
python -m extract_um_met run --domain NA --date 2014 --suffix coarse       # NA/NA_coarse_Met_2014.zarr
python -m extract_um_met run --domain NA --date 2014 --grid-mode native --suffix native
```

Use it to keep a `--grid-mode` / config experiment separate from the production
store. Each suffix has its **own** resume state, and its per-region scratch
intermediates are tagged too (`{DOMAIN}_{TAG}_Met_..._{region}.nc`), so variants
never share intermediates. A leading underscore on the tag is trimmed
(`--suffix _coarse` == `--suffix coarse`).

---

## 3. `extract` — per-region intermediates only

Writes per-region NetCDF intermediates to `{scratch}/files/` without joining or
touching zarr. Useful to isolate one region or inspect an intermediate.

```bash
python -m extract_um_met extract --domain SA --date 201601             # all regions, month
python -m extract_um_met extract --domain SA --date 201601 --region 6  # one region
python -m extract_um_met extract --domain SA --date 20160115 --region 6  # one region, one day
```

| Option | Meaning |
| ------ | ------- |
| `--domain` *(required)* | domain key or `domain_name` |
| `--date` *(required)* | `YYYYMM` (month) or `YYYYMMDD` (day) — **no** `YYYY` form here |
| `--region` | a single world-region ID; omit to do all of the domain's regions |
| `--suffix TAG` | tag the intermediate filenames `{DOMAIN}_{TAG}_Met_...` (same tag `run` uses) |

These intermediates are transient (a `run` deletes them after the month appends,
unless `--keep-intermediates`). They exist to bound peak memory, not as a product.

---

## 4. `make-native-grid` — build native grids

One-off; see [how_to_datatypes.md](how_to_datatypes.md#5-native-grids-data).

```bash
python -m extract_um_met make-native-grid --sample-date 201601   # all Mks
python -m extract_um_met make-native-grid --mk 9                 # one Mk
python -m extract_um_met make-native-grid --dry-run             # no write
python -m extract_um_met make-native-grid --output-dir /custom/path/
```

| Option | Meaning |
| ------ | ------- |
| `--mk` | Mk version (6–11); omit for all available |
| `--sample-date` | `YYYYMM` to sample a file from (default `201601`) |
| `--output-dir` | override `config.native_grid_directory` |
| `--dry-run` | report what it would extract; write nothing |

---

## 5. Workflow at a glance

```
python -m extract_um_met run --domain SA --date 2016
   │
   ├─ resolve config, domain, date → 2016-01 … 2016-12, build target grid
   │
   └─ for each month:
        ├─ extract each world region        (load .pp(.gz) → align winds →
        │                                     build time dim → regrid → slice →
        │                                     write per-region NetCDF to scratch)
        ├─ join regions → domain Dataset     (concat lat within lon columns, then
        │                                     lon; fill seams; rename; stamp CF)
        └─ append the month to the yearly zarr store
   finalize provenance/completeness attrs

OUTPUT: {zarr_save_directory}/SOUTHAMERICA/SOUTHAMERICA_Met_2016.zarr
```

Non-tiled sources (e.g. `NZCSM`) skip the join: each day is extracted whole-domain
and appended one day-batch at a time.

---

## 6. Add a new domain

Add a block under `domains:` in `config.yaml` (see
[how_to_datatypes.md](how_to_datatypes.md) for regions and grid modes):

```yaml
domains:
  MYDOMAIN:
    domain_name: "MY_DOMAIN"          # used in output paths / provenance
    footprint: "my_reference_footprint.nc"   # for footprint mode
    world_regions_codes: [6, 7]       # world regions it overlaps (tiled sources)
    grid:
      mode: footprint                 # footprint | regular | native
      edge_size_lat: [100, 100]
      edge_size_lon: [85, 100]
```

Then dry-run, then smoke-test a single day, then run for real:

```bash
python -m extract_um_met run --domain MYDOMAIN --date 201601 --dry-run
python -m extract_um_met run --domain MYDOMAIN --date 20160115
python -m extract_um_met run --domain MYDOMAIN --date 2016
```

To find which `world_regions_codes` a bounding box needs:

```python
from extract_um_met.regions import find_overlapping_regions
find_overlapping_regions(min_lat=-25, max_lat=15, min_lon=-75, max_lon=-30)
```

---

## 7. Run under SLURM

Extraction is memory-heavy (a month of a multi-region domain can want 100+ GB).
On JASMIN submit it as a batch job rather than running long jobs on the login
node. A minimal template using the current CLI:

```bash
#!/bin/bash
#SBATCH -p standard
#SBATCH --account=acrg
#SBATCH --qos=standard
#SBATCH --mem=200GB
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --job-name=SA_2016
#SBATCH --output=logs/%x_%j.out
#SBATCH --error=logs/%x_%j.err
#SBATCH -t 08:00:00

cd /home/users/$USER/extract_um_met
module purge
module load jaspy
export TMPDIR=/work/scratch-pw5/$USER/tmp
mkdir -p "$TMPDIR"

python -m extract_um_met run --domain SA --date 2016
```

Submit with `sbatch your_job.slurm`. To parallelise across months, use a SLURM
array and map `$SLURM_ARRAY_TASK_ID` (1–12) to a `--date YYYYMM`:

```bash
#SBATCH --array=1-12
month=$(printf "%02d" "$SLURM_ARRAY_TASK_ID")
python -m extract_um_met run --domain SA --date "2016${month}"
```

> **Note:** the old `run_single_job.txt` / `run_parallel_job.txt` templates in the
> repo root call the deprecated `satellite_met_1b1_fixed_v3.py` /
> `satellite_met_join_v2.py` scripts — **not** the `extract_um_met` package. Use the
> template above instead. (Array-appending months in parallel can race the zarr
> store; prefer running one year sequentially, or one array task per year.)

---

## 8. Check the output

```python
import xarray as xr
ds = xr.open_zarr(".../SOUTHAMERICA/SOUTHAMERICA_Met_2016.zarr")
print(ds)                       # dims (time, levels, lat, lon), variables
print(ds.time.values[[0, -1]])  # coverage
print(ds.attrs["months_present"], ds.attrs["missing_months"], ds.attrs["year_complete"])
```

Provenance attrs (`grid_mode`, `months_present`, `missing_months`,
`year_complete`, `time_start/end`, native-grid info, …) are written by
`finalize_attrs` after each year. There is also a `check_met_files.py` helper and
notebooks under `notebooks/` for validation.

---

## 9. Common issues

| Symptom | Likely cause / fix |
| ------- | ------------------ |
| `config.yaml did not parse` / domain not found | not in the repo root, or no `config.yaml` — `cp config.example.yaml config.yaml` |
| "missing files" past the newest Mk block | the archive has moved to a new Mk — extend `iris_io.get_Mk` and `cli.MK_SAMPLE_TIMESTEPS` (see [datatypes](how_to_datatypes.md#2-the-global-um-archive-um_global)) |
| dates ≥ 2017-07 much slower / larger than expected | Mk10 onward is N1280, ~2.8× the points of Mk8/9 — not a fault |
| Node `/tmp` fills / decompression fails | `TMPDIR` not set to scratch — see [setup §3](how_to_setup.md#3-set-tmpdir-to-scratch) |
| "cannot backfill earlier missing month" | append needs time order — rerun the year with `--overwrite` |
| `Native grid not found for Mk N` | run `make-native-grid --mk N` first |
| Out-of-memory in SLURM | raise `--mem`; do a month at a time; keep intermediates off |
</content>
