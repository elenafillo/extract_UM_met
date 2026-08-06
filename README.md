# extract_UM_met

Extract Numerical Weather Prediction (NWP) meteorology from the Met Office **Unified Model (UM)** — `.pp` files archived on [JASMIN](https://www.jasmin.ac.uk/) and [CEDA](https://archive.ceda.ac.uk/) — into analysis-ready yearly [Zarr](https://zarr.dev/) stores.

`extract_um_met` is a single command-line tool that extracts each world region,
joins them into your domain, and appends the result to a yearly store.

```bash
python -m extract_um_met run --domain SA --date 2016
# → {zarr_save_directory}/SOUTHAMERICA/SOUTHAMERICA_Met_2016.zarr
```

## Accessing the data

The raw archive is available via JASMIN ([link your CEDA and JASMIN accounts
first](https://help.ceda.ac.uk/article/5105-linking-your-jasmin-account)), or to
[download directly from CEDA](https://data.ceda.ac.uk/badc/name_nwp/data/global)
for 2015–2024 only. Running this tool on JASMIN reaches the full record.

## Quick start (on JASMIN)

```bash
git clone https://github.com/elenafillo/extract_UM_met.git
cd extract_UM_met                                          # config.yaml is read from here
module purge && module load jaspy                          # JASMIN Python env
export TMPDIR=/work/scratch-pw5/$USER/tmp && mkdir -p "$TMPDIR"
cp config.example.yaml config.yaml                         # then edit your copy
python -m extract_um_met make-native-grid                   # one-off
python -m extract_um_met run --domain SA --date 20160115    # single-day smoke test
```

`config.yaml` is git-ignored (per-user paths); the shared structure — domains,
archive paths, grid specs — lives in `config.example.yaml`.

## Documentation → [elenafillo.github.io/extract_UM_met](https://elenafillo.github.io/extract_UM_met/)

| Page | What it covers |
| ---- | -------------- |
| [Setup](https://elenafillo.github.io/extract_UM_met/setup.html) | Environment, `TMPDIR`, writing `config.yaml`, native grids — **start here** |
| [Extracting](https://elenafillo.github.io/extract_UM_met/extracting.html) | The CLI: `run` / `extract` / `make-native-grid`, resume, SLURM, adding a domain |
| [Concepts](https://elenafillo.github.io/extract_UM_met/concepts.html) | Data types, Mk blocks, world regions, grid modes |
| [Reference](https://elenafillo.github.io/extract_UM_met/reference.html) | Variables, model levels, output chunking, raw UM inventory |
| [Troubleshooting](https://elenafillo.github.io/extract_UM_met/troubleshooting.html) | Symptoms, causes and fixes |
| [Codebase](https://elenafillo.github.io/extract_UM_met/codebase.html) | Module map and pipeline flow |
| [Roadmap and contributing](https://elenafillo.github.io/extract_UM_met/roadmap_and_contributing.html) | Remaining work, open questions, how to open an issue, credits |

The pages are written in [`How_Tos/`](How_Tos/) and rendered by the Sphinx config
in [`docs/`](docs/) — edit the sources in `How_Tos/`, never `docs/`.


