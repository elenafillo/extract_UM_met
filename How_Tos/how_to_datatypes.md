# How To: Understand the Data Types & Grids

What you can extract, how the raw UM archive is organised, and how the target
grid is built. Read this to decide **which data type, domain and grid mode** to
put in your config before running.

> **See also:** [how_to_setup.md](how_to_setup.md) (environment + config) and
> [how_to_extract.md](how_to_extract.md) (the commands).

---

## 1. Data types (sources)

A **data type** (a `MetSource`, defined in `extract_um_met/sources.py`) is one met
product. They differ on almost every axis — archive layout, filename pattern, Mk
calendar, grid projection, region scheme, level count, cadence, compression, and
whether they are *tiled* (many region files that must be joined) or a single file
per timestep. The pipeline dispatches on these properties instead of hardcoding
one product.

| Data type | Archive | Grid | Regions | Tiled? | Levels | Cadence | Mk calendar | Status |
| --------- | ------- | ---- | ------- | ------ | ------ | ------- | ----------- | ------ |
| **`UM_Global`** | `…/met_archive/Global/` | regular lat/lon | `world14` (14 tiles) | yes → join | L59 | 3-hourly | Mk6–12 | **working** (Mk6–12) |
| **`UM1p5km`** | `…/met_archive/LimitedArea/` | rotated pole | `uk16` (16 UK tiles) | yes → join | L57 | 1-hourly | Mk2–4 (Mk4 confirmed) | region bounds **not generated yet** |
| **`NZCSM`** | TBD (config) | rotated pole | none (one file) | no join | L70 | 1-hourly | none | **partial** (12 vars ship; stresses deferred) |

A domain picks its data type with `data_type:` in config (default `UM_Global`
if omitted). Structural properties are code-defined; only **archive paths** (and
a few tuning scalars) are overridable in `config.yaml → data_types:`.

- **Tiled** sources (`UM_Global`, `UM1p5km`) split the globe/UK into region
  files. A domain names the regions it needs (`world_regions_codes`), the
  pipeline extracts each, then **joins** them into the domain.
- **Non-tiled** sources (`NZCSM`) deliver one file covering the whole domain per
  timestep — no join; the run extracts and appends a day at a time.

---

## 2. The Global UM archive (`UM_Global`)

Hosted on JASMIN at `/gws/ssde/j25a/name/met_archive/Global/`. From 2011 the UM
is released in **"Mk" blocks**, in folders `UMG_Mk{N}PT/`. Each Mk covers a time
period at a given native resolution:

| Mk | Dates | Native (Δlat × Δlon) | Grid size (lat × lon) |
| --- | ----- | -------------------- | --------------------- |
| Mk6 | Jan 2011 – Mar 2013 | 0.234375° × 0.351562° | 769 × 1024 |
| Mk7 | Apr 2013 – Jun 2014 | 0.234375° × 0.351562° | 769 × 1024 |
| Mk8 | Jul 2014 – Jul 2015 | 0.15625° × 0.234375° | 1152 × 1536 |
| Mk9 | Aug 2015 – Jun 2017 | 0.15625° × 0.234375° | 1152 × 1536 |
| Mk10 | Jul 2017 – May 2022 | 0.09375° × 0.140625° | 1920 × 2560 |
| Mk11 | Jun 2022 – Jan 2026 | 0.09375° × 0.140625° | 1920 × 2560 |
| Mk12 | Feb 2026 – | 0.09375° × 0.140625° | 1920 × 2560 |

**Availability:** Mk6–Mk12 (2011-01 onward) are all extractable. Every grid size
above is what `make-native-grid` actually stitches from the 14 region files, not
a nominal figure.

Mk10 is a **resolution jump**: the global model moves from N768 (Mk8/9) to N1280,
so Δlat/Δlon halve and the grid is ~2.8× more points per level. Mk11 and Mk12
stay on N1280. Expect longer extractions, larger intermediates and bigger Zarr
stores for any date from 2017-07 onward.

Mk blocks overlap slightly in the archive (e.g. `UMG_Mk11PT/` starts at 2022-04 while
Mk10 runs to 2022-05, and `UMG_Mk12PT/` starts at 2025-12 while Mk11 runs to
2026-01). The boundaries in the table are the ones used in the code via `get_Mk`: the switch happens the month after the previous block's last full month.

Note that the Mk boundary for a date is resolved in code (`iris_io.get_Mk`); you never set it manually — just give a `--date`.

### Filename pattern

```
MO{YYYYMMDD}{HHMM}.UMG_Mk{N}_{set}_L59PT{region}.pp[.gz]
      e.g.  MO201403080000.UMG_Mk7_I_L59PT10.pp
```

- `HHMM` — sub-daily period: `0000, 0300, … 2100` (3-hourly → 8 files/day).
- `{set}` — variable set token `I` or `M` (see below). *(Mk6 files omit the
  `_I_` token — handled automatically.)*
- `{region}` — world region `1`–`14` (see §4).
- Mk6–Mk9 files are **gzipped** (`.pp.gz`) and are decompressed under
  `scratch_path` before reading; Mk10–Mk12 are plain `.pp`.

---

## 3. Grid modes

Every domain is extracted onto a **target grid** chosen by its `grid.mode`
(built in `extract_um_met/grid.py`). Set this per domain in config:

| Mode | Target grid | Interpolates? | Needs |
| ---- | ----------- | ------------- | ----- |
| **`footprint`** (default) | a reference footprint's lat/lon, padded outward by edge cells | yes | `footprint:` file + `edge_size_lat/lon` |
| **`regular`** | `np.arange` mesh from bounds + step | yes | `lat_bounds`, `lon_bounds`, `dlat`, `dlon` |
| **`native`** | the saved native UM grid (subset to bounds) | **no** (passthrough on matching Mk) | pre-built native grids in `data/` |

If a domain has no `grid:` block, it defaults to **footprint** with the domain's
`footprint` key and `default_edge_size`.

**Footprint mode** loads the reference footprint, crops it to where measurements
actually are (`release_lat/lon` if present), takes its native lat/lon, then pads
outward by `edge_size_*` cells using the NAME reference spacing (Δlat 0.234,
Δlon 0.352). This reproduces the original extraction behaviour.

**Native mode** snaps a whole year to **one canonical Mk grid** (the majority Mk
by month count) so time-appends stay aligned; months already on that Mk pass
through untouched, off-Mk months are interpolated onto it.

> ⚠️ **Native-mode cropping gap:** `lat_bounds`/`lon_bounds` in a `native` block
> currently shape only the canonical target grid — extraction still passes each
> region through at its full region extent (sliced to region bounds only), so a
> native domain is not yet cropped to a tight bounding box. Footprint/regular
> modes are unaffected. (Known limitation; see `usage.md`.)

### Grid spec examples (config)

```yaml
# footprint (default)
grid:
  mode: footprint
  footprint: "GOSAT-BRAZIL-column_SOUTHAMERICA_201801.nc"
  edge_size_lat: [100, 100]
  edge_size_lon: [85, 100]

# regular
grid:
  mode: regular
  lat_bounds: [-25, 15]
  lon_bounds: [-75, -30]
  dlat: 0.234
  dlon: 0.352

# native
grid:
  mode: native
  lat_bounds: [-17, 59]
  lon_bounds: [45, 127]
  native_grid: majority   # or "latest", "Mk9", etc.
```

---

## 4. World regions (the `world14` scheme)

Tiled Global data is split into **14 world regions**. A domain lists the ones it
overlaps in `world_regions_codes`; the pipeline extracts each and joins them.
Bounds are `[min_lat, max_lat, min_lon, max_lon]` (from
`extract_um_met/regions.py`):

| Region | Lat band | Lon band | Notes |
| ------ | -------- | -------- | ----- |
| 1  | 79.9 … 90.0 | polar cap | North pole |
| 2  | 25.0 … 80.0 | −45 … 45 | |
| 3  | 25.0 … 80.0 | 45 … 135 | |
| 4  | 25.0 … 80.0 | 135 … −135 | crosses dateline |
| 5  | 25.0 … 80.0 | −135 … −45 | |
| 6  | −25.1 … 25.1 | −45 … 45 | equatorial |
| 7  | −25.1 … 25.1 | 45 … 135 | equatorial |
| 8  | −25.1 … 25.1 | 135 … −135 | equatorial, crosses dateline |
| 9  | −25.1 … 25.1 | −135 … −45 | equatorial |
| 10 | −80.0 … −25.0 | −45 … 45 | |
| 11 | −80.0 … −25.0 | 45 … 135 | |
| 12 | −80.0 … −25.0 | 135 … −135 | crosses dateline |
| 13 | −80.0 … −25.0 | −135 … −45 | |
| 14 | −90.0 … −79.9 | polar cap | South pole |

![The 14 world regions](https://github.com/user-attachments/assets/f6dc8296-f87f-4d82-bcf5-533147d8e9a3)

Grid layout (matches the region numbering above):

```
        -180        -45     45      135    180
  +90    ┌───────────── 1 (N pole) ──────────────┐
         │   5    │   2    │   3    │   4   │      (mid-N)
  +25    ├────────┼────────┼────────┼───────┤
         │   9    │   6    │   7    │   8   │      (equator)
  -25    ├────────┼────────┼────────┼───────┤
         │  13    │  10    │  11    │  12   │      (mid-S)
  -80    └───────────── 14 (S pole) ─────────────┘
```

To find which regions a bounding box needs, use
`regions.find_overlapping_regions(min_lat, max_lat, min_lon, max_lon)`.

Existing domains in the config and their regions:

| Domain | `domain_name` | Regions | Mode |
| ------ | ------------- | ------- | ---- |
| `SA` | SOUTHAMERICA | 6, 9, 10, 13 | footprint |
| `NA` | NORTHAFRICA | 2, 3, 6, 7 | footprint |
| `CHINA` | CHINA | 3, 4, 7, 8 | footprint |
| `INDIA` | INDIA | 3, 7 | footprint |
| `NZ` | NEWZEALAND | — (NZCSM, non-tiled) | regular |

To extract an existing domain on a different grid, don't add a second domain —
run it with `--grid-mode` and a `--suffix` so the variant lands beside the main
store — see the `--suffix` section of [how_to_extract.md](how_to_extract.md).

---

## 5. Native grids (`data/`)

Native grids are the true UM lat/lon per Mk, saved for reference and for
`native`-mode extraction. `data/` is **empty on a fresh clone** — the grids are
regenerable and git-ignored, so build them with `make-native-grid` before your
first `native`-mode run (see [how_to_setup.md](how_to_setup.md#5-generate-native-grids-one-off)):

```bash
python -m extract_um_met make-native-grid              # all Mks (6–12)
python -m extract_um_met make-native-grid --mk 9       # one Mk
python -m extract_um_met make-native-grid --dry-run    # inspect, no write
```

Each Mk is read from one known sample timestep (`cli.MK_SAMPLE_TIMESTEPS`); the
grid is fixed within an Mk block, so which timestep does not matter. Naming it
also keeps the command off a directory listing — the Mk10–12 folders hold
hundreds of thousands of files. Override it for a single block with
`--mk 12 --sample-date YYYYMMDDHHMM`.

Per Mk this writes into `data/`:

- `native_grid_Mk{N}_region{01..14}.nc` — per-region grids
- `native_grid_Mk{N}_global.nc` — stitched global grid
- `native_grid_Mk{N}_info.yaml` — resolutions + point counts (e.g. Mk9 global is
  1152 × 1536 at 0.15625° × 0.234375°)

**Why it matters:** resolution changes at Mk boundaries (Mk6/7 coarse → Mk8/9
fine → Mk10–12 finer still). Footprint and regular modes always interpolate onto
a fixed target, so they are immune. Native mode snaps to one canonical Mk grid
and only interpolates off-resolution months — which now costs more, since a year
spanning the 2017-07 boundary mixes N768 and N1280 months.

---

## 6. What variables get extracted

For `UM_Global` (the `I` variable set), extraction keeps **12 variables**,
selected by name + level presence (not file order), so a changed archive raises a
clear error instead of silently mis-picking. Groups (from `sources.py`):

**Mass variables** (already on the mass grid; L = has model levels):
`air_pressure` (L), `air_pressure_at_sea_level`, `air_temperature` (L),
`atmosphere_boundary_layer_thickness`, `specific_humidity` (L),
`surface_air_pressure`, `upward_air_velocity` (L).

**Winds** (staggered; interpolated onto the mass grid):
`x_wind` (L), `y_wind` (L).

**Time-averaged surface fields** (3-hour means, timestamp re-stamped to the
interval end):
`surface_upward_sensible_heat_flux`;
`atmosphere_downward_eastward_stress`, `atmosphere_downward_northward_stress`.

**Model levels** are subsampled: level 1 plus every 3rd level up to L59 →
`[1, 3, 6, 9, …, 57]` (`level_stride`, tunable per data type). Output renames
coords to `lat` / `lon` / `levels`.

> The full raw UM variable inventory (both `I` and `M` sets, ~30 fields) is
> listed in the [top-level README](https://github.com/elenafillo/extract_UM_met/blob/zarr_stores/README.md#variable-lists); only the 12
> above are carried into the zarr stores.

---

## 7. Output

One **yearly Zarr store** per domain/year:

```
{zarr_save_directory}/{DOMAIN_NAME}/{DOMAIN_NAME}_Met_{YYYY}.zarr
   e.g.  …/satellite_met_zarr/SOUTHAMERICA/SOUTHAMERICA_Met_2016.zarr
```

Dims `time × levels × lat × lon`, chunked `{time:1, lat:-1, lon:-1, levels:3}`,
float32 with zstd Blosc compression, CF metadata + provenance attrs. Single-day
smoke tests write a separate `…_Met_{YYYYMMDD}.zarr` debug store.

Open one with:

```python
import xarray as xr
ds = xr.open_zarr(".../SOUTHAMERICA_Met_2016.zarr")
print(ds)                 # dims, variables, coords
print(ds.attrs)           # provenance: months_present, missing_months, grid_mode, …
```
</content>
