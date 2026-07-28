# Reference: variables, levels & output

Lookup tables for what a store contains and how to change it: the extracted
variables, the coordinates and attributes that come with them, how to override
the selection, the on-disk format and chunking, and the full raw UM inventory.

> **See also:** [concepts.md](concepts.md) for what the data types, Mk blocks,
> regions and grid modes *are* — read that first if you are deciding what to
> extract. This page is for looking things up once you are.

---

## 1. What a store contains

### Variables

`UM_Global` extraction keeps **12 variables**, all from the `I` variable set.
Units are whatever the archive carries, passed through by iris unchanged.

| Variable | Units | On model levels? | How it is produced |
| -------- | ----- | ---------------- | ------------------ |
| `air_pressure` | Pa | yes | instantaneous, mass grid |
| `air_pressure_at_sea_level` | Pa | no | instantaneous, mass grid |
| `air_temperature` | K | yes | instantaneous, mass grid |
| `atmosphere_boundary_layer_thickness` | — | no | instantaneous, mass grid |
| `specific_humidity` | kg kg-1 | yes | instantaneous, mass grid |
| `surface_air_pressure` | Pa | no | instantaneous, mass grid |
| `upward_air_velocity` | m s-1 | yes | instantaneous, mass grid |
| `x_wind` | m s-1 | yes | staggered → interpolated onto the mass grid |
| `y_wind` | m s-1 | yes | staggered → interpolated onto the mass grid |
| `surface_upward_sensible_heat_flux` | W m-2 | no | 3-hour mean, re-stamped to interval end |
| `atmosphere_downward_eastward_stress` | Pa | no | 3-hour mean, re-stamped to interval end |
| `atmosphere_downward_northward_stress` | Pa | no | 3-hour mean, re-stamped to interval end |

The three production kinds correspond one-to-one to the config keys you override
in [§2](#2-choosing-your-own-variables-and-levels): `mass_vars`, `wind_vars`, and
`averaged_mass_vars` / `averaged_staggered_vars`. Selection is by **name + level
presence**.

### Dimensions and coordinates

Dims are `time × levels × lat × lon`. CF attributes are set by
`metadata.apply_cf_metadata`:

| Coord | A dimension? | Meaning |
| ----- | ------------ | ------- |
| `time` | yes | validity time; `standard_name: time`, `axis: T` |
| `levels` | yes | UM model level number, subsampled (see below); `positive: up` |
| `lat` | yes | `degrees_north`, `axis: Y` |
| `lon` | yes | `degrees_east`, `axis: X` |
| `forecast_reference_time` | no — indexed by `time` | model run (analysis) reference time |
| `forecast_period` | no — indexed by `time` | time since the forecast reference time |

The two forecast coords are retained as time-indexed **provenance**, not as
dimensions: they record which model run each timestep came from.

**Model levels** are subsampled — level 1 plus every 3rd level up to L59, i.e.
`[1, 3, 6, 9, …, 57]` (`level_stride`, default 3).

### Global attributes

CF attributes, written from the config `metadata:` block — empty config fields
are omitted rather than written blank:

`Conventions` · `title` · `institution` · `source` · `references` · `history` ·
`comment` · `author` · `grid_mode` · `mk_version` · `data_type` ·
`delta_lat` / `delta_lon` (mean grid spacing)

Provenance and completeness attributes, written by `zarr_io.finalize_attrs`
after each year:

`created_by` · `created_at` · `time_start` · `time_end` ·
`n_time` / `n_lat` / `n_lon` / `n_levels` · `zarr_chunks` · `months_present` ·
`n_months_present` · `missing_months` · `year_complete`

`year_complete` and `missing_months` are the quickest check that a year finished:

```python
import xarray as xr
ds = xr.open_zarr(".../SOUTHAMERICA/SOUTHAMERICA_Met_2016.zarr")
ds.attrs["year_complete"], ds.attrs["missing_months"]
```

---

## 2. Choosing your own variables and levels

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
[inventory in §3](#3-raw-um-variable-inventory). Any variable group may be set to
`[]` to drop it entirely.

Two things to watch. Editing these changes **every** run using that config, so
keep a job-specific config (or a `--suffix`) for variant extractions rather than
editing in place. And because stores are appended to per month, changing the
variable or level selection part-way through a year will not line up with the
months already written — rebuild that year with `--overwrite`.

---

## 3. Raw UM variable inventory

Everything a `UM_Global` `.pp` file carries, by variable set. **✓** marks the 12
fields extraction keeps (all from the `I` set) — see [§1](#1-what-a-store-contains).
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
silently taking the wrong one.

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

The set is the `{set}` token in the filename (see
[concepts.md](concepts.md#filename-pattern)) — a domain-month has an `I` file and
an `M` file per timestep. Extraction reads only `I` today; pulling `M` fields
would mean loading the second file too.

---

## 4. Output format and chunking

One **yearly Zarr store** per domain/year:

```
{zarr_save_directory}/{DOMAIN_NAME}/{DOMAIN_NAME}_Met_{YYYY}.zarr
   e.g.  …/satellite_met_zarr/SOUTHAMERICA/SOUTHAMERICA_Met_2016.zarr
```

Float32 with zstd Blosc compression, CF `time` encoding, NaN fill. Single-day
smoke tests write a separate `…_Met_{YYYYMMDD}.zarr` debug store, and
`--suffix TAG` writes `…_{TAG}_Met_{YYYY}.zarr` beside the main store.

`zarr_format` (config, or `--zarr-format`) picks the on-disk version: `2` is
readable by both zarr 2.18.7 and 3.x, `3` by 3.x only. Per-region scratch
intermediates are written in the same format, so scratch and output never
straddle two versions.

### Choosing the output chunks

The default chunk shape is `{time: 1, levels: 3, lat: -1, lon: -1}`, tuned for
the dominant access pattern: `time: 1` because scattered single-timestep reads
are the common case, levels in groups of 3, lat/lon whole. A domain can override
it in config:

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
starts. The shape actually used is recorded on the store as `zarr_chunks`.

Which way to go depends on how you read the store: keep `time: 1` for reading
whole fields at scattered timesteps, and raise it when you mostly pull long time
series at a few points, which the default makes expensive (one chunk touched per
timestep).

> **Applies at store creation only.** The chunk layout is fixed when a store is
> first written; later months inherit it. Changing `output_chunks` for a year
> that already exists has no effect until you rebuild it with `--overwrite`.

---

## 5. Scratch intermediates and peak memory

Extraction stages each region into a transient zarr store under
`{scratch}/files/`, joins them, and appends the result to the yearly store.
Extraction holds **one period of one region** in memory at a time, so how much
time one intermediate covers is what sets the pipeline's peak memory. A domain
chooses it with `intermediate_period`:

```yaml
domains:
  SA:
    domain_name: "SOUTHAMERICA"
    intermediate_period: week      # month (default) | week | day
```

| Period | One intermediate covers | Peak memory | Scratch stores per region-month |
| ------ | ----------------------- | ----------- | ------------------------------- |
| `month` *(default)* | the whole month | baseline | 1 |
| `week` | a 7-day block | roughly a quarter of monthly | 5 |
| `day` | one day | roughly a thirtieth | 28–31 |

Weeks are 7-day blocks **within** the month — 1–7, 8–14, 15–21, 22–28, 29–end,
so the last block is short. Periods never cross a month boundary, which keeps one
Mk version per period and leaves the yearly store keyed exactly as before.

**The output is identical either way.** This knob only changes how the work is
broken up on the way there. The cost of a shorter period is more (smaller)
scratch stores and more appends to the yearly store.

Reach for it when a large domain runs out of memory — big domains, many model
levels and fine target grids all push in the same direction. Note that Mk10
onward is ~2.8× the points of Mk8/9, so a domain that fits monthly in 2016 may
not in 2018.

### How periods are named

A period covering a whole month is tagged `YYYYMM`, the name monthly
intermediates have always had, so existing scratch stores stay reusable. Any
shorter period is tagged with its first day, `YYYYMMDD`:

```
{scratch}/files/SOUTHAMERICA_Met_201601_6.zarr      # month period
{scratch}/files/SOUTHAMERICA_Met_20160108_6.zarr    # week period starting the 8th
```

Resume is driven by the **days actually present** in the yearly store, not by the
period setting, so changing `intermediate_period` part-way through a year is safe
— already-written days are skipped either way.
