# extract_UM_met

Extract Numerical Weather Prediction (NWP) meteorology from the Met Office **Unified Model (UM)** — `.pp` files archived on [JASMIN](https://www.jasmin.ac.uk/) and [CEDA](https://archive.ceda.ac.uk/) — into analysis-ready yearly [Zarr](https://zarr.dev/) stores.

The archive is not laid out for analysis: it is thousands of gzipped `.pp` files,
split across time blocks and geographic tiles, on a grid whose resolution changes
partway through the record. `extract_um_met` is one command that pulls the tiles
your region needs, joins them, regrids them onto a target you define, and appends
the result to a single store per year.

```bash
python -m extract_um_met run --domain SA --date 2016
# → {zarr_save_directory}/SOUTHAMERICA/SOUTHAMERICA_Met_2016.zarr
```

## Accessing the data

The raw archive is available via [JASMIN](https://www.jasmin.ac.uk/). If you have access to the NAME workspace, you can access the full record, from 2011 onwards. Otherwise, you can access data for 2015–2024 only by
[linking your CEDA and JASMIN accounts](https://help.ceda.ac.uk/article/5105-linking-your-jasmin-account) or by [downloading the data directly from CEDA](https://data.ceda.ac.uk/badc/name_nwp/data/global).

## Three ideas shape everything else

**Mk blocks** split the archive in time. From 2011 the UM is released in numbered
blocks, each at a fixed native resolution — and the resolution changes between
them, which is why regridding onto a stable target is part of the job. You never
choose the Mk: give a `--date` and it is resolved in code.

**World regions** split it in space. Each timestep of the global archive arrives
as 14 tiles; a domain declares which ones it overlaps, and the pipeline extracts
each and joins them.

**Grid modes** decide what you land on — `footprint` (default), `regular`, or
`native` — set per domain in `config.yaml`. Adding a domain is config only, no
code.

All three are covered in [Concepts](concepts.md).

## Documentation

::::{grid} 1 1 2 2
:gutter: 3

:::{grid-item-card} Setup
:link: setup
:link-type: doc

Environment (jaspy), `TMPDIR`, writing `config.yaml`, native grids.
**Start here.**
:::

:::{grid-item-card} Extracting
:link: extracting
:link-type: doc

The CLI: `run` / `extract` / `make-native-grid`, options, resume, SLURM,
adding a domain.
:::

:::{grid-item-card} Concepts
:link: concepts
:link-type: doc

The data types, Mk blocks & resolutions, world regions, grid modes.
:::

:::{grid-item-card} Reference
:link: reference
:link-type: doc

Variables, model levels, output format and chunking, raw UM inventory.
:::

:::{grid-item-card} Troubleshooting
:link: troubleshooting
:link-type: doc

Error messages, what causes them, and what to do about each.
:::

:::{grid-item-card} Codebase
:link: codebase
:link-type: doc

Module map — what each module does and how the pipeline flows.
:::

:::{grid-item-card} Roadmap
:link: roadmap
:link-type: doc

Status, remaining work, and the open questions behind current behaviour.
:::

::::

## Common tasks

| I want to… | Go to |
| ---------- | ----- |
| Get set up on JASMIN for the first time | [Setup](setup.md) |
| Extract a year of an existing domain | [Extracting](extracting.md) |
| Add a new domain | [Extracting §6](extracting.md#6-add-a-new-domain) |
| Work out which world regions my area needs | [Concepts §3](concepts.md#3-world-regions-the-world14-scheme) |
| Change which variables or levels are extracted | [Reference §2](reference.md#2-choosing-your-own-variables-and-levels) |
| Change how the output store is chunked | [Reference §4](reference.md#4-output-format-and-chunking) |
| Find out what is in a store I already have | [Reference §1](reference.md#1-what-a-store-contains) |
| Submit a big run to SLURM | [Extracting §7](extracting.md#7-run-under-slurm) |
| Work out why a run failed | [Troubleshooting](troubleshooting.md) |
| Add a data type or grid mode | [Codebase §4](codebase.md#4-where-to-change-things) |

```{toctree}
:hidden:
:maxdepth: 2

setup
extracting
concepts
reference
troubleshooting
codebase
roadmap
```
