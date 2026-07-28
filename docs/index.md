# extract_UM_met

Extract Numerical Weather Prediction (NWP) meteorology from the Met Office **Unified Model (UM)** — `.pp` files archived on [JASMIN](https://www.jasmin.ac.uk/) and [CEDA](https://archive.ceda.ac.uk/) — into analysis-ready yearly [Zarr](https://zarr.dev/) stores.

This large dataset is not laid out for analysis: it is thousands of gzipped `.pp`
files, split across time blocks and geographic tiles, on a grid whose resolution
changes partway through the record. `extract_um_met` is one command that pulls
the tiles your region needs, joins them, regrids them onto a target you define,
and appends the result to a single Zarr store per year.

```bash
python -m extract_um_met run --domain SA --date 2016
# → {zarr_save_directory}/SOUTHAMERICA/SOUTHAMERICA_Met_2016.zarr
```

## Accessing the data
The data is accessible via JASMIN ([you will need to link your CEDA and JASMIN accounts](https://help.ceda.ac.uk/article/5105-linking-your-jasmin-account)) or avaiable to [download directly from CEDA](https://data.ceda.ac.uk/badc/name_nwp/data/global) (available only from 2015-2024).

## How the UM archive is organised

Two things shape everything else in this tool: **Mk blocks** (how the archive is
split in time) and **world regions** (how it is split in space).

### Mk blocks — the time axis

From 2011 the UM is released in numbered **"Mk" blocks**, each covering a date
range at a fixed native resolution. The resolution *changes between blocks*,
which is why regridding onto a stable target grid is part of the job:

| Mk | Dates | Native (Δlat × Δlon) | Status |
| --- | ----- | -------------------- | ------ |
| Mk6 | Jan 2011 – Apr 2013 | 0.234° × 0.352° | extractable |
| Mk7 | May 2013 – Jun 2014 | 0.234° × 0.352° | extractable |
| Mk8 | Jul 2014 – Jul 2015 | 0.156° × 0.234° | extractable |
| Mk9 | Aug 2015 – Jun 2017 | 0.156° × 0.234° | extractable |
| Mk10 | Jul 2017 – May 2022 | 0.094° × 0.141° | extractable |
| Mk11 | Jun 2022 – Jan 2026 | 0.094° × 0.141° | extractable |
| Mk12 | Feb 2026 – | 0.094° × 0.141° | extractable |

You never set the Mk yourself — give a `--date` and the block is resolved in
code. Mk6–Mk12 are all extractable, so **2011-01 onward is available**. Note the
resolution doubles at Mk10 (N768 → N1280) and stays there, which makes anything
from 2017-07 substantially heavier to extract and store.

### World regions — the space axis

Each timestep of the global archive is split into **14 tiles**. A domain declares
which tiles it overlaps; the pipeline extracts each one and joins them:

![The 14 world regions](https://github.com/user-attachments/assets/f6dc8296-f87f-4d82-bcf5-533147d8e9a3)

Global data is 3-hourly on 59 model levels, so one year of one domain is a few
thousand tile-files reduced to a single store.

## The three data types

The UM global archive is the centre of this tool. Two limited-area products are
supported by the same machinery, at earlier stages of readiness:

| Data type | What it is | Status |
| --------- | ---------- | ------ |
| **`UM_Global`** | Global UM, 14 tiles, L59, 3-hourly | **working** (Mk6–12) — the main path |
| `UM1p5km` | 1.5 km UK limited-area (UKV), rotated pole, 16 UK tiles, L57, hourly | region bounds not generated yet |
| `NZCSM` | New Zealand convective-scale, rotated pole, one file per timestep, L70, hourly | partial — 12 variables ship, stresses deferred |

Structural properties (grid type, tiling, levels, cadence) are code-defined in
`sources.py`; only archive **paths** are per-environment. See
[Data types & grids](how_to_datatypes) for the full picture.

## Configuring it

Everything you run is driven by `config.yaml`, copied from the versioned
`config.example.yaml`:

```bash
cp config.example.yaml config.yaml    # then edit — your copy is git-ignored
```

The shared structure — domains, data-type archive paths, grid specs — is what
lives in the example file. The genuinely personal parts are your scratch and
output paths, and `met_extract_author` for provenance.

### Adding a domain

A domain is **config only, no code**. Declare the region, the tiles it overlaps,
and the grid you want it on:

```yaml
domains:
  MYDOMAIN:
    domain_name: "MY_DOMAIN"                 # used in output paths / provenance
    footprint: "my_reference_footprint.nc"   # for footprint mode
    world_regions_codes: [6, 7]              # tiles it overlaps
    grid:
      mode: footprint                        # footprint | regular | native
      edge_size_lat: [100, 100]
      edge_size_lon: [85, 100]
    output_chunks:                           # optional; on-disk chunking
      time: 24
```

Not sure which tiles you need? Ask:

```python
from extract_um_met.regions import find_overlapping_regions
find_overlapping_regions(min_lat=-25, max_lat=15, min_lon=-75, max_lon=-30)
```

Then dry-run, smoke-test one day, and only then run the year:

```bash
python -m extract_um_met run --domain MYDOMAIN --date 201601 --dry-run
python -m extract_um_met run --domain MYDOMAIN --date 20160115
python -m extract_um_met run --domain MYDOMAIN --date 2016
```

To extract an existing domain onto a *different* grid, don't add a second domain
— use `--grid-mode` with a `--suffix` so the variant sits beside the main store.

## Quick start

```bash
cd extract_UM_met
module purge && module load jaspy                              # JASMIN Python env
export TMPDIR=/work/scratch-pw5/$USER/tmp && mkdir -p "$TMPDIR"
cp config.example.yaml config.yaml                             # then edit your copy
python -m extract_um_met make-native-grid                       # one-off
python -m extract_um_met run --domain SA --date 20160115        # single-day smoke test
```

## Documentation

::::{grid} 1 1 2 2
:gutter: 3

:::{grid-item-card} Setup
:link: how_to_setup
:link-type: doc

Environment (jaspy), `TMPDIR`, writing `config.yaml`, native grids.
**Start here.**
:::

:::{grid-item-card} Data types & grids
:link: how_to_datatypes
:link-type: doc

The data types, Mk blocks & resolutions, world regions, grid modes, variables.
:::

:::{grid-item-card} Extracting
:link: how_to_extract
:link-type: doc

The CLI: `run` / `extract` / `make-native-grid`, options, resume, SLURM,
adding a domain.
:::

:::{grid-item-card} Codebase
:link: how_to_code
:link-type: doc

Module map — what each module does and how the pipeline flows.
:::

::::

## Output

One yearly Zarr store per domain, dims `time × levels × lat × lon`, float32 with
zstd compression, carrying CF and provenance attributes. Three grid modes are
available — `footprint` (default), `regular`, and `native` — set per domain.

```{toctree}
:hidden:
:maxdepth: 2

how_to_setup
how_to_datatypes
how_to_extract
how_to_code
roadmap
```
