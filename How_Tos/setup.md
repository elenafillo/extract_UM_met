# How To: Set Up `extract_UM_met`

First-time setup on JASMIN: load the Python environment, create your temp
directory, and write your personal `config.yaml`. Do this once per account; after
that you only touch `config.yaml` when you add a domain or change a path.

> **See also:** [concepts.md](concepts.md) (what data/grids exist) and
> [extracting.md](extracting.md) (running the extraction). If something breaks,
> [troubleshooting.md](troubleshooting.md).

---

## 1. Get the code

```bash
cd /home/users/$USER            # or wherever you keep your repos
git clone https://github.com/elenafillo/extract_UM_met.git
cd extract_UM_met
```

Everything below is run **from the repo root** (`extract_UM_met/`). The config
loader looks for `config.yaml` in the current working directory, so `python -m
extract_um_met …` only works when you `cd` here first.

Two similar names, one letter apart: **`extract_UM_met`** is the repo (the
directory you `cd` into), **`extract_um_met`** is the Python package inside it
(what you `import` and what `python -m` runs).

---

## 2. Python environment (JASMIN)

The package runs under JASMIN's managed scientific Python stack, **jaspy**, which
already ships iris, xarray, dask, zarr, numpy and pyyaml at compatible versions:

```bash
module purge
module load jaspy
```

Put those two lines at the top of any interactive session or SLURM script.

**Dependencies** (all provided by jaspy):

| Package | Min version | Used for |
| ------- | ----------- | -------- |
| Python  | 3.10+ (tested 3.12) | — |
| iris    | ≥ 3.0 | reading `.pp` / `.pp.gz` UM files |
| xarray  | ≥ 2025.1 | datasets, zarr I/O |
| dask    | any recent | lazy / chunked processing |
| zarr    | 2.18 or 3.x | output stores (`zarr_format` picks the on-disk version) |
| pyyaml  | any | reading `config.yaml` |

There is no `environment.yml` yet; jaspy is the supported environment. If you run
off-JASMIN, create an env with the packages above.

---

## 3. Set `TMPDIR` to scratch

Reading Mk6–Mk9 data means decompressing `.pp.gz` files, and iris writes temp
files while loading. By default those land in the node's small `/tmp`, which can
fill up and kill a long run. Point `TMPDIR` at your **scratch** space (large,
fast, purged periodically) instead:

```bash
export TMPDIR=/work/scratch-pw5/$USER/tmp
mkdir -p "$TMPDIR"
```

- Run this in every session (or add it to a SLURM script) **before** calling
  `extract_um_met`.
- `/work/scratch-pw5/` is the same scratch filesystem the pipeline uses for
  per-region intermediates — see `scratch_path` in the config below.
- Scratch is not backed up and is auto-cleaned; it is the right place for
  transient temp/decompression files, not for outputs.
- `make-native-grid` does not depend on this — it decompresses into
  `scratch_path` directly — but iris's own temp files still follow `TMPDIR`.

---

## 4. Create your `config.yaml`

`config.yaml` holds per-user paths and is **git-ignored**. Copy the committed
template and edit your copy:

```bash
cp config.example.yaml config.yaml
```

Then edit `config.yaml`. The shared structure (domains, data-type paths, grid
specs) is versioned in {src}`config.example.yaml`; the only
strictly personal field is `met_extract_author`.

### Fields to check / fill

```yaml
user: ""                         # leave blank — auto-filled from $USER
met_extract_author: "Your Name (id)"   # recorded in output provenance

scratch_path: "/work/scratch-pw5/{user}/"          # per-region intermediates
zarr_save_directory: "/gws/ssde/j25b/acrg/{user}/satellite_met_zarr/"  # outputs
zarr_format: 2                   # 2 = readable by zarr 2.18.7 and 3.x; 3 = newer only

reference_footprints_directory: "/gws/ssde/j25b/acrg/elenafi/example_footprints"
native_grid_directory: "data/"   # saved native UM grids (relative to repo root)
```

- `{user}` is expanded from the `$USER` environment variable at runtime — leave
  `user: ""` and it fills itself in.
- Make sure `scratch_path`, `zarr_save_directory` and `native_grid_directory`
  exist or are writable by you.

### Metadata block (CF attributes stamped into every store)

Fill in what applies; **leave a field `""` to omit it** (blank fields are not
written as empty attributes):

```yaml
metadata:
  conventions: "CF-1.10"
  institution: "University of Bristol, Atmospheric Chemistry Research Group"
  references: ""     # DOI / URL for the dataset or method
  title: ""          # optional; auto "UM meteorology, {DOMAIN} {year}" if empty
  source: ""         # optional; auto from the UM Mk version if empty
  comment: ""        # optional; appended to the auto processing comment
```

### Data-type archive paths

Each data type's structural properties live in code
(`extract_um_met/sources.py`). The only field you must set per type is the
**archive path**:

```yaml
data_types:
  UM_Global:
    archive_directory: "/gws/ssde/j25a/name/met_archive/Global/"
  UM1p5km:
    archive_directory: "/gws/ssde/j25a/name/met_archive/LimitedArea/"
  NZCSM:
    archive_directory: ""    # set once the NZCSM archive exists
```

The same block optionally takes which **variables** and **model levels** to
extract — both defaulted, so you can leave them out until you need them. See
[concepts.md](concepts.md) for what each type is and
[reference.md §2](reference.md#2-choosing-your-own-variables-and-levels) for the
tuning keys. The chunking of a domain's output store is set separately, per
domain — see [reference.md §4](reference.md#4-output-format-and-chunking).

---

## 5. Generate native grids (one-off)

Native UM grids are regenerable and **not committed** (`data/` is git-ignored).
Before your first `native`-mode run — and safe to run any time — build them:

```bash
python -m extract_um_met make-native-grid
```

This writes `data/native_grid_Mk{6..12}_*.nc` plus `_info.yaml` metadata. Each Mk
is read from one known sample timestep (`cli.MK_SAMPLE_TIMESTEPS`) — the grid is
fixed within an Mk block, so the choice does not matter, and naming it avoids
listing the Mk10–12 folders, which hold hundreds of thousands of files. See
[concepts.md](concepts.md#5-native-grids-data).

Gzipped Mk6–9 files are decompressed into `scratch_path` from `config.yaml`, not
`/tmp` — JASMIN's `/tmp` is a 2 GB tmpfs and is routinely full.

---

## 6. Sanity-check the setup

Do a dry run — it resolves config, domain and dates without writing anything:

```bash
python -m extract_um_met run --domain SA --date 201601 --dry-run
```

Then a cheap real end-to-end test on a single day (writes a small, separate debug
store, never touches your yearly stores):

```bash
python -m extract_um_met run --domain SA --date 20160115
```

If that produces a `…_Met_20160115.zarr`, your environment, paths and config are
good. Move on to [extracting.md](extracting.md) for real runs.

---

## Setup checklist

- [ ] `cd` into the repo root
- [ ] `module purge && module load jaspy`
- [ ] `export TMPDIR=/work/scratch-pw5/$USER/tmp && mkdir -p "$TMPDIR"`
- [ ] `cp config.example.yaml config.yaml` and edit paths + `met_extract_author`
- [ ] `python -m extract_um_met make-native-grid`
- [ ] `python -m extract_um_met run --domain SA --date 20160115` (smoke test)
</content>
</invoke>
