# Topography

The UM vertical coordinate is terrain-following, so one static 2-D field — the
orography — fixes the height of every model level. `extract-topog` writes it for
a domain of your own, or for the whole globe via the ready-made `GLOBAL` domain.

> **See also:** [concepts.md](concepts.md) (Mk blocks, grid modes),
> [reference.md](reference.md) (what a store contains). The evidence behind
> these choices — field comparisons, validation against the met, and the scripts
> to reproduce any of it — is in `How_Tos/topography_validation.md` in the
> repository.

---

## 1. Extracting

```bash
# one of your own domains
python -m extract_um_met extract-topog --domain EUROPE --date 2023
# → {ancillary_save_directory}/EUROPE/EUROPE_topog_2023.nc

# the whole globe — GLOBAL ships in config.example.yaml, nothing to configure
python -m extract_um_met extract-topog --domain GLOBAL --date 2023
# → {ancillary_save_directory}/GLOBAL/GLOBAL_topog_2023.nc
```

`--date` is a year, not a month: the field is static within a year but changes
between Mk blocks, so the year selects both the ancillary and the target grid.

What you get depends on the domain's `grid.mode`:

| Mode | Domain | Output | Interpolates? | Example size |
| ---- | ------ | ------ | ------------- | ------------ |
| `global` | `GLOBAL` (shipped) | the whole globe at UM native resolution | no | 19.7 MB |
| `native` | yours | a subset of the native grid | no | 2.9 MB (EUROPE) |
| `footprint`, `regular` | yours | interpolated onto the domain's met target grid | yes | 0.4 MB (NORTHAFRICA) |

The output lands on the same grid the met run builds for that domain, so it
matches the store cell for cell. Getting this wrong is silent — a half-cell
offset still produces a well-formed file, and every derived height is then wrong
in a way nothing downstream detects.

**A global file is domain-agnostic**: a `native`-mode consumer subsets its own
box out of it, so a new native domain needs no extraction at all. Footprint and
regular domains still want their own file — their target grid is an interpolated
mesh that does not lie on the UM native ladder.

`global` is `extract-topog` only; `run` and `extract` reject the mode.

---

## 2. Using it

`level_height` and `sigma` are already coordinates on every store, so orography
is the only missing term:

```python
geopotential            = g * (level_height + sigma * orog)   # (levels, lat, lon)
geopotential_at_surface = g * orog                            # (lat, lon)
```

with `g = 9.80665`. All three inputs are static, so the geopotential is too — no
time dimension. `extract-topog` writes the orography only, never the geopotential.

---

## 3. Configuration

```yaml
ancillary_types:
  UM_Global:
    native_topog_directory: "/gws/ssde/j25a/name/data/UMAncils/Global"
    topog_by_mk:
      6:  UMG_Mk7_ps33
      7:  UMG_Mk7_ps33
      8:  UMG_Mk8_ps35
      9:  UMG_Mk9_ps38
      10: UMG_Mk10_ps41
      11: UMG_Mk11_ps45
      12: UMG_Mk11_ps45   # see §4
  NZCSM:
    native_topog_file: ""   # sources with one field for all time
```

`ancillary_save_directory` is optional and defaults to `zarr_save_directory`, so
the orography lands beside the store it belongs with. Do not write
`"{zarr_save_directory}"` in config — `resolve_config_value` expands one level
only and leaves a literal `{user}` in the path.

Global output is nothing more than a domain with `mode: global`. This block
already ships in `config.example.yaml`, so `--domain GLOBAL` works as it stands —
copy it only if you are writing a config from scratch:

```yaml
GLOBAL:
  domain_name: "GLOBAL"
  data_type: "UM_Global"
  grid:
    mode: global
```

---

## 4. Which ancillary each Mk uses

The ancillary lives at
`/gws/ssde/j25a/name/data/UMAncils/Global/<block>_ps<NN>/topog.pp` — one global
`surface_altitude` field (STASH `m01s00i033`) per block, not split into world
regions, on longitudes `0…360`.

There are **five distinct fields across Mk6–Mk11**:

| Grid (lat × lon) | Resolution | Mk blocks | Directory |
| ---------------- | ---------- | --------- | --------- |
| 769 × 1024 | 0.234375° × 0.351562° | Mk6, Mk7 | `UMG_Mk7_ps33` |
| 1152 × 1536 | 0.15625° × 0.234375° | Mk8 | `UMG_Mk8_ps35` |
| 1152 × 1536 | 0.15625° × 0.234375° | Mk9 | `UMG_Mk9_ps38` |
| 1920 × 2560 | 0.09375° × 0.140625° | Mk10 | `UMG_Mk10_ps41` |
| 1920 × 2560 | 0.09375° × 0.140625° | Mk11 | `UMG_Mk11_ps45` |

Two blocks sharing a grid does *not* mean they share a field — Mk10 and Mk11 have
identical grids but differ over a third of the globe, by up to 2 km. **Always
pick by Mk.**

**The `ps` version never matters.** Within a block every `ps` directory carries
byte-identical data; they differ only in header validity times. `GLOUM6_ps21` and
`UMG_Mk5_ps22` hold a sixth, coarser field predating the extractable record —
ignore them.

### Mk12

There is no `UMG_Mk12*` directory, and the orography is not carried inside the
Mk12 met files either. Mk12's grid matches Mk11's exactly but its *field* does
not, so **`UMG_Mk11_ps45` is used for Mk12 as a known approximation** — correct
almost everywhere, knowingly wrong in a small number of high-relief cells. The
output records `mk_version: 12` alongside `source: .../UMG_Mk11_ps45`, so the
substitution is visible in the attributes. Revisit when a `UMG_Mk12*` directory
appears.

---

## 5. Output is per year, not per Mk

`{domain}_topog_{year}.nc`, pairing with `{domain}_Met_{year}.zarr`. Mk
boundaries fall mid-year (2013-05, 2014-07, 2015-08, 2017-07, 2022-06),
so keying by year means no consumer has to reimplement the Mk calendar to pick
the right file.

Five years straddle a boundary and so contain two orographies. The majority Mk by
month count is used for the whole year — the same rule and tie-break `run` uses,
so a boundary year cannot disagree with its store.

A global year costs 3.15 MB on Mk6–7, 7.08 MB on Mk8–9 and 19.66 MB on Mk10–12:
**~215 MB for the whole 2011–2026 record**.
