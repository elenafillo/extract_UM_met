# extract_UM_met

Extract Numerical Weather Prediction (NWP) meteorology from the Met Office **Unified Model (UM)** — `.pp` files archived on [JASMIN](https://www.jasmin.ac.uk/) and [CEDA](https://archive.ceda.ac.uk/) — into analysis-ready yearly [Zarr](https://zarr.dev/) stores.

`extract_um_met` is a single command-line tool that extracts each world region,
joins them into your domain, and appends the result to a yearly store, with
support for multiple data types (global UM, limited-area, convective-scale),
three grid modes, and CF/provenance metadata.

```bash
python -m extract_um_met run --domain SA --date 2016
# → {zarr_save_directory}/SOUTHAMERICA/SOUTHAMERICA_Met_2016.zarr
```

## Accessing the data
The data is accessible via JASMIN ([you will need to link your CEDA and JASMIN accounts](https://help.ceda.ac.uk/article/5105-linking-your-jasmin-account)) or avaiable to [download directly from CEDA](https://data.ceda.ac.uk/badc/name_nwp/data/global) (available only from 2015-2024).

## Quick start (on Jasmin)

```bash
cd extract_iris_met
module purge && module load jaspy                       # JASMIN Python env
export TMPDIR=/work/scratch-pw5/$USER/tmp && mkdir -p "$TMPDIR"
cp config.example.yaml config.yaml                      # then edit your copy
python -m extract_um_met make-native-grid                # one-off
python -m extract_um_met run --domain SA --date 20160115   # single-day smoke test
```

`config.yaml` is git-ignored (per-user paths). The shared structure — domains,
data-type archive paths, grid specs — lives in `config.example.yaml`.

## Documentation → [`How_Tos/`](How_Tos/)

| Guide | What it covers |
| ----- | -------------- |
| [How_Tos/how_to_setup.md](How_Tos/how_to_setup.md) | Environment (jaspy), `TMPDIR`, writing `config.yaml`, native grids — **start here** |
| [How_Tos/how_to_datatypes.md](How_Tos/how_to_datatypes.md) | The data types, Mk blocks & resolutions, world regions, grid modes, variables |
| [How_Tos/how_to_extract.md](How_Tos/how_to_extract.md) | The CLI: `run` / `extract` / `make-native-grid`, options, resume, SLURM, adding a domain |
| [How_Tos/how_to_code.md](How_Tos/how_to_code.md) | Codebase map — what each module does and how the pipeline flows |

Development roadmap and open questions: [`usage.md`](usage.md).

Also check [https://elenafillo.github.io/extract_UM_met/](https://elenafillo.github.io/extract_UM_met/).

## At a glance

- **Data types:** `UM_Global` (global UM, Mk6–12), `UM1p5km` (1.5 km UK
  limited-area), `NZCSM` (New Zealand convective-scale). Only the archive path is
  per-environment; structural properties are code-defined in
  [`extract_um_met/sources.py`](extract_um_met/sources.py).
- **Availability:** Global Mk6–Mk12 (2011-01 onward) are extractable. Mk10 onward
  runs at N1280 (0.09375° × 0.140625°) versus N768 for Mk8/9, so dates from
  2017-07 cost noticeably more time and disk.
- **Grid modes:** `footprint` (default), `regular`, `native` — set per domain in
  config. See [how_to_datatypes.md](How_Tos/how_to_datatypes.md).
- **Output:** one yearly Zarr store per domain, dims `time × levels × lat × lon`,
  float32 + zstd, CF + provenance attributes.

> **Note:** the `satellite_met_*.py` / `met_functions.py` scripts and the
> `run_*_job.txt` SLURM files in the repo root are the pre-refactor originals,
> kept for reference only — the supported workflow is `python -m extract_um_met`.
</content>
