# How To: Extract Meteorology (the command line)

How to drive the `extract_um_met` CLI: the subcommands, their options, the
extract → join → append workflow, resume behaviour, adding a domain, and running
under SLURM.

> **Before this:** finish [setup.md](setup.md) (env + config) and skim
> [concepts.md](concepts.md) (data types, grids, domains).

**Every command assumes:**

```bash
cd /home/users/$USER/extract_UM_met     # config.yaml is read from here
module purge && module load jaspy
export TMPDIR=/work/scratch-pw5/$USER/tmp && mkdir -p "$TMPDIR"
```

---

## 1. The subcommands

```
python -m extract_um_met <command> [options]
```

| Command | Does | Use when |
| ------- | ---- | -------- |
| **`run`** | extract → join → append to yearly zarr store (with resume) | the main workflow — producing data |
| **`extract`** | write per-region zarr intermediates only | debugging / retrying a flaky region |
| **`make-native-grid`** | save native UM grids to `data/` | one-off setup; before `native` mode |
| **`extract-topog`** | write the static orography ancillary for a domain-year | pairing topography with a store — see [topography.md](topography.md) |

Run any command with `-h` to see its options, e.g. `python -m extract_um_met run -h`.

:::{note}
**`extract-topog` is documented on its own page**, [topography.md](topography.md).
It writes the static orography ancillary that pairs with a store.
:::

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
| `--keep-intermediates` | keep per-region intermediates in scratch (default: delete after append) |
| `--suffix TAG` | tag the store as `{DOMAIN}_{TAG}_Met_...` so a variant sits beside the main store (see below) |

### The `--date` forms

- **`YYYY`** — all 12 months into `{DOMAIN}_Met_{YYYY}.zarr`.
- **`YYYYMM`** — that month, appended to the yearly store.
- **`YYYYMMDD`** — a single day into a **separate** `{DOMAIN}_Met_{YYYYMMDD}.zarr`
  debug store (always a fresh write; never touches the yearly stores). The cheap
  end-to-end smoke test — ~8 files per region.

### Resume semantics

For an existing yearly store (no `--overwrite`), resume is driven by the **days
actually present** in the store:

- Periods whose days are already present are **skipped**; only later ones are
  appended.
- Backfilling an **earlier** missing period via append is **not possible** (time
  order must be preserved). The run warns and you rebuild that year with
  `--overwrite`.
- Re-running a complete year is a no-op (it just re-finalises attrs).

Example — first pass does Jan–Jun, later you run the full year: Jan–Jun are
skipped, Jul–Dec appended.

Because resume reads real timestamps rather than a period counter, changing a
domain's `intermediate_period` part-way through a year is safe.

### Running a variant side by side

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
intermediates are tagged too (`{DOMAIN}_{TAG}_Met_..._{region}.zarr`), so variants
never share intermediates. A leading underscore on the tag is trimmed
(`--suffix _coarse` == `--suffix coarse`).

---

## 3. `extract` — per-region intermediates only

Writes per-region zarr intermediates to `{scratch}/files/` without joining or
appending to the yearly store. Useful to isolate one region or inspect an
intermediate.

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
| `--suffix TAG` | tag the intermediate names `{DOMAIN}_{TAG}_Met_...` (same tag `run` uses) |

Each region lands as `{scratch}/files/{DOMAIN}_Met_{tag}_{region}.zarr`, where
`tag` is `YYYYMM` for a whole-month period and `YYYYMMDD` for a shorter one — see
[reference.md §5](reference.md#5-scratch-intermediates-and-peak-memory). On a
non-tiled source (e.g. `NZCSM`) there are no regions, so `extract` writes the
whole domain to a single `{DOMAIN}_Met_{tag}.zarr` instead.

These intermediates are transient (a `run` deletes them after the month appends,
unless `--keep-intermediates`). They exist to bound peak memory, not as a product.

---

## 4. `make-native-grid` — build native grids

One-off; see [concepts.md](concepts.md#5-native-grids-data).

```bash
python -m extract_um_met make-native-grid                        # all Mks
python -m extract_um_met make-native-grid --mk 9                 # one Mk
python -m extract_um_met make-native-grid --dry-run             # no write
python -m extract_um_met make-native-grid --output-dir /custom/path/
```

| Option | Meaning |
| ------ | ------- |
| `--mk` | Mk version (6–12); omit for all available |
| `--output-dir` | override `config.native_grid_directory` |
| `--dry-run` | report what it would extract; write nothing |

---

## 5. Workflow at a glance

```
python -m extract_um_met run --domain SA --date 2016
   │
   ├─ resolve config, domain, date → 2016-01 … 2016-12, build target grid
   │  split each month into periods (intermediate_period: month by default)
   │
   └─ for each period:
        ├─ extract each world region        (load .pp(.gz) → align winds →
        │                                     build time dim → regrid → slice →
        │                                     write per-region zarr to scratch)
        ├─ join regions → domain Dataset     (concat lat within lon columns, then
        │                                     lon; fill seams; rename; stamp CF)
        └─ append the period to the yearly zarr store
   finalize provenance/completeness attrs

OUTPUT: {zarr_save_directory}/SOUTHAMERICA/SOUTHAMERICA_Met_2016.zarr
```

Non-tiled sources (e.g. `NZCSM`) skip the join: each day is extracted whole-domain
and appended one day-batch at a time, straight from memory — no per-region
intermediate is staged, so `--keep-intermediates` does nothing for them.

---

## 6. Add a new domain

Add a block under `domains:` in `config.yaml` (see
[concepts.md](concepts.md) for regions and grid modes):

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
    # Optional: on-disk chunking of this domain's store; omitted dims keep the
    # default {time: 1, levels: 3, lat: -1, lon: -1}. See reference.md §4.
    # output_chunks:
    #   time: 24
    # Optional: how much time one scratch intermediate covers. Shorten it if a
    # big domain runs out of memory. See reference.md §5.
    # intermediate_period: week        # month (default) | week | day
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

Extraction is memory-heavy — a month of a multi-region domain can want 100+ GB —
so on JASMIN submit it as a batch job rather than running it on a login node.
The repo ships the job scripts under `scripts/`; you should not need to write
your own.

### Parallel: one array task per (month, region)

`scripts/launch_met_array.sh` is the normal way to do a year. It splits the work
so each array task extracts **one region of one month** into a scratch
intermediate, then submits a dependent job that joins them and appends each month
to the yearly store:

```bash
bash scripts/launch_met_array.sh 2015 NA            # a full year
bash scripts/launch_met_array.sh 201501 NA          # a single month
bash scripts/launch_met_array.sh 2015 NA coarse     # → NORTHAFRICA_coarse_Met_2015.zarr
```

Run it **on a login node** — it submits jobs, it is not itself a batch script. It
reads the domain's region codes and grid mode from `config.yaml` through the
package itself, so the domain table is never duplicated in the scripts. Account,
QoS, partition and the concurrency cap are set at the top of the script.

It submits two jobs:

| Script | Job | Sizing |
| ------ | --- | ------ |
| `scripts/extract_region.sbatch` | array, one task per (month, region) | 200 GB, 6 CPUs, 3 h, max 12 concurrent |
| `scripts/join_year.sbatch` | one serial job, `afterok` the array | 150 GB, 6 CPUs, 8 h |

The join stays serial because months must be appended in **time order**. It runs
the ordinary `run` command, which reuses the intermediates the array already
wrote instead of re-extracting them.

Logs land in `scripts/logs/`; watch progress with `squeue -u $USER`.

The launcher **refuses two cases on purpose**:

- **Non-tiled domains** (e.g. `NZ` / NZCSM) — there are no regions to split
  across array tasks.
- **`native` grid mode** — `run` snaps a whole year to one canonical Mk grid,
  while a standalone `extract` uses each month's own Mk. The intermediates would
  land on different grids and the join would silently produce a mixed-grid store.

For either, use a single serial job.

### Serial: one job for the whole run

For a single month, a smoke test, a non-tiled domain, or native mode,
`scripts/launch_met.sh` runs the command end to end in one job. **Submit it from
the repo root** — it `cd`s to `$SLURM_SUBMIT_DIR`, which is where `config.yaml`
is read from:

```bash
sbatch scripts/launch_met.sh 2016 SA
sbatch scripts/launch_met.sh 201601 SA
sbatch scripts/launch_met.sh 20160115 SA          # single-day smoke test
sbatch scripts/launch_met.sh 2016 NA coarse       # → NORTHAFRICA_coarse_Met_2016.zarr
```

Arguments are `DATE` (required), `DOMAIN` (default `SA`) and `SUFFIX` (optional).
Two flags are opt-in:

```bash
sbatch -J SA2016 scripts/launch_met.sh 2016 SA                       # readable job name
sbatch --export=ALL,OVERWRITE=1 scripts/launch_met.sh 2016 SA        # rebuild, don't resume
```

It defaults to **resuming**, matching the CLI. Sizing is 200 GB, 5 CPUs, 8 h —
edit the `#SBATCH` lines if your domain needs more.

---

## 8. Check the output

```python
import xarray as xr
ds = xr.open_zarr(".../SOUTHAMERICA/SOUTHAMERICA_Met_2016.zarr")
print(ds)                       # dims (time, levels, lat, lon), variables
print(ds.time.values[[0, -1]])  # coverage
print(ds.attrs["months_present"], ds.attrs["missing_months"], ds.attrs["year_complete"])
```

The full list of coordinates and attributes a store carries is in
[reference.md §1](reference.md#1-what-a-store-contains). To check archive
coverage before or after a run, there is a `check_met_files.py` helper.

---

## 9. When something goes wrong

`--dry-run` separates configuration problems from data problems, and a single-day
run is the cheapest real test (both shown in §2). Error messages, causes and
fixes are collected in [troubleshooting.md](troubleshooting.md); silent defects
and file quirks are in [known_problems.md](known_problems.md).
