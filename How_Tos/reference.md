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
if omitted). Structural properties above are code-defined. What you *can* set per
data type in `config.yaml → data_types:` is the **archive path**, and which
**variables** and **model levels** to extract (§6). A domain separately sets the
**output chunking** of its store (§7).

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
| Mk6 | Jan 2011 – Apr 2013 | 0.234375° × 0.351562° | 769 × 1024 |
| Mk7 | May 2013 – Jun 2014 | 0.234375° × 0.351562° | 769 × 1024 |
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
hundreds of thousands of files.

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
`[1, 3, 6, 9, …, 57]` (`level_stride`). Output renames coords to `lat` / `lon` /
`levels`.

### Choosing your own variables and levels

The lists above are defaults, not fixed: both are overridable per data type under
`config.yaml → data_types:`, no code change needed.

```yaml
data_types:
  UM_Global:
    archive_directory: "/gws/ssde/j25a/name/met_archive/Global/"
    # Just temperature, humidity and winds — no averaged fields.
    mass_vars:
      - [air_temperature, true]
      - [specific_humidity, true]
      - [surface_air_pressure, false]
    wind_vars:
      - [x_wind, true]
      - [y_wind, true]
    averaged_mass_vars: []
    averaged_staggered_vars: []
    model_levels: [1, 10, 20, 30, 40, 50, 59]   # or "all" for every level
```

| Key | Form | Notes |
| --- | ---- | ----- |
| `mass_vars`, `wind_vars` | `[cube_name, has_levels]` pairs | `has_levels` must match the archive — a mismatch fails loudly at extraction |
| `averaged_mass_vars`, `averaged_staggered_vars` | bare cube names | 3-hour means, re-stamped to interval end |
| `level_stride` | int | level 1 plus every Nth level (default 3) |
| `model_levels` | list of ints, 1-based | exact levels; **overrides** `level_stride` |
| `model_levels` | `"all"` | every level the source has (L59 global, L70 NZCSM) |

Cube names must match the archive's iris names exactly — pick them from the
[inventory in §8](#8-raw-um-variable-inventory). Any variable group may be set to
`[]` to drop it entirely.

Two things to watch. Editing these changes **every** run using that config, so
keep a job-specific config (or a `--suffix`) for variant extractions rather than
editing in place. And because stores are appended to per month, changing the
variable or level selection part-way through a year will not line up with the
months already written — rebuild that year with `--overwrite`.

> The full raw UM variable inventory (both `I` and `M` sets, 30 fields) is in
> [§8 below](#8-raw-um-variable-inventory); only the 12 above are carried into
> the zarr stores.

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

### Choosing the output chunks

The chunk shape above is the default, tuned for the dominant access pattern:
`time: 1` because scattered single-timestep reads are the common case, levels in
groups of 3, lat/lon whole. A domain can override it in config:

```yaml
domains:
  SA:
    domain_name: "SOUTHAMERICA"
    output_chunks:
      time: 24            # only the dims you name change; the rest keep the default
```

Keys are `time`, `levels`, `lat`, `lon`; `-1` means one chunk spanning the whole
dim. An unrecognised key (`latitude:`) raises at config load rather than silently
leaving the default in place, and `run` prints the resolved chunks before it
starts.

Which way to go depends on how you read the store: keep `time: 1` for reading
whole fields at scattered timesteps, and raise it when you mostly pull long time
series at a few points, which the default makes expensive (one chunk touched per
timestep).

> **Applies at store creation only.** The chunk layout is fixed when a store is
> first written; later months inherit it. Changing `output_chunks` for a year
> that already exists has no effect until you rebuild it with `--overwrite`.

Open one with:

```python
import xarray as xr
ds = xr.open_zarr(".../SOUTHAMERICA_Met_2016.zarr")
print(ds)                 # dims, variables, coords
print(ds.attrs)           # provenance: months_present, missing_months, grid_mode, …
```

---

## 8. Raw UM variable inventory

Everything a `UM_Global` `.pp` file carries, by variable set. **✓** marks the 12
fields extraction keeps (all from the `I` set) — see [§6](#6-what-variables-get-extracted).
The rest are read past, not dropped for any quality reason; add one by editing
the `*_vars` tuples on the `MetSource` in `sources.py`.

The `I` set — instantaneous fields, plus the surface fluxes/stresses:

| # | Name | Kept | Notes |
| - | ---- | ---- | ----- |
| 0  | `m01s03i026` | | unnamed STASH code |
| 1  | `m01s03i319` | | has a "pseudo level" coord |
| 2  | canopy water on tiles / (kg/m^2) | | |
| 3  | `m01s03i462` | | unnamed STASH code |
| 4  | `atmosphere_downward_eastward_stress` / (Pa) | ✓ | 3-hour mean |
| 5  | `atmosphere_downward_northward_stress` / (Pa) | ✓ | 3-hour mean |
| 6  | `air_pressure` / (Pa) | ✓ | model levels |
| 7  | `air_pressure_at_sea_level` / (Pa) | ✓ | |
| 8  | `air_temperature` / (K) | | the 1.5 m field — **deliberately excluded** |
| 9  | `air_temperature` / (K) | ✓ | model levels |
| 10 | `atmosphere_boundary_layer_thickness` | ✓ | |
| 11 | `moisture_content_of_soil_layer` | | has a depth coord |
| 12 | `specific_humidity` / (kg kg-1) | ✓ | model levels |
| 13 | `surface_air_pressure` / (Pa) | ✓ | |
| 14 | `surface_upward_sensible_heat_flux` / (W m-2) | ✓ | 3-hour mean |
| 15 | `upward_air_velocity` / (m s-1) | ✓ | model levels |
| 16 | `x_wind` / (m s-1) | ✓ | model levels, staggered |
| 17 | `y_wind` / (m s-1) | ✓ | model levels, staggered |

Entries 8 and 9 are **both** `air_temperature` — 8 is the 1.5 m diagnostic, 9 is
the model-level field. This is why selection is by name **plus level presence**
rather than by name alone, and why `_pick` raises on an archive change instead of
silently taking the wrong one. Excluding the 1.5 m field keeps exact parity with
the original pre-refactor script.

The `M` set — cloud and precipitation fields. None are currently extracted:

| # | Name | Notes |
| - | ---- | ----- |
| 0  | `m01s05i220` | unnamed STASH code |
| 1  | `m01s05i223` | unnamed STASH code |
| 2  | `air_pressure_at_convective_cloud_base` / (Pa) | |
| 3  | `convective_rainfall_flux` / (kg m-2 s-1) | |
| 4  | `convective_snowfall_flux` / (kg m-2 s-1) | |
| 5  | `high_type_cloud_area_fraction` | |
| 6  | `low_type_cloud_area_fraction` | |
| 7  | `mass_fraction_of_cloud_ice_in_air` | has a height coord |
| 8  | `mass_fraction_of_cloud_liquid_water_in_air` | has a height coord |
| 9  | `medium_type_cloud_area_fraction` | |
| 10 | `stratiform_rainfall_flux` | |
| 11 | `stratiform_snowfall_flux` | |

The set is the `{set}` token in the filename (§2) — a domain-month has an `I`
file and an `M` file per timestep. Extraction reads only `I` today; pulling `M`
fields would mean loading the second file too.
</content>
