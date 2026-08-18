# Topography: validation and evidence

Supporting material for [topography.md](topography.md) — why the UM
`surface_altitude` ancillary is the right field, how it was checked against the
met, and how to redo any of it. Written for the GLIDE team and anyone auditing
the choices; not needed to *use* `extract-topog`.

---

## 1. Why this retires `air_pressure`

GLIDE's converter reads the 3-D `air_pressure` only to back-solve the orography
it does not have (`derive_orography` in `glide_stuff/convert_um_met.py`).
Shipping the ancillary removes that need, worth roughly **30 GB per
domain-year**.

The ancillary is not merely equivalent to that back-solve — it is better. See §2.

---

## 2. Ancillary vs the hydrostatic back-solve

Two fully independent routes to the same quantity, compared over
`EUROPE_Met_2023.zarr` (December 2023, 1,122 × 1,282 = 1,438,404 points, ~814,000
of them sea, sea identified as
`|surface_air_pressure − air_pressure_at_sea_level| < 30 Pa`):

| Quantity | Result |
| -------- | ------ |
| Back-solved orography at sea points | +0.37 to +0.56 ± ~1.5 m |
| **Ancillary at sea points** | **+0.00 ± 0.11 m** |
| Ancillary exactly 0 at sea | **99.30%** of sea points |
| Ancillary − back-solved, all points | −0.4 to −0.6 ± 1.5–1.7 m |
| Correlation | **0.999995** |

The two fields agree to well under a metre in the mean across 1.4 million points,
so the ancillary is the right field on the right grid. At sea, where truth is
exactly 0, the ancillary returns 0.00 ± 0.11 m while the back-solve carries a
+0.5 m bias.

The residual ±1.5 m spread is the hydrostatic solve's integration error, not
disagreement about terrain: the ~0.7% of sea points where the ancillary is
non-zero are small coastal and inland-water values (down to −2.8 m), and the
largest departures (up to ~160 m) are all in steep terrain, where the back-solve
is weakest.

The −340 m minimum is physical — the Jordan valley and Caspian depression. It is
also the true global land minimum, so a whole-globe extraction bottoms out at the
same value.

> The 1,122 × 1,282 extent is the `EUROPE_Met_2023.zarr` that existed at the
> time, wider than the current config produces (741 × 974). Only the point counts
> depend on that; the comparison is point-wise on a shared grid, so the agreement
> does not.

---

## 3. The fields differ between Mk blocks

Mk6 and Mk7 genuinely share one field. **Mk8/Mk9 and Mk10/Mk11 do not**, despite
identical grids:

| Pair | Mean Δ | Std Δ | Max \|Δ\| | Points differing | Land/sea flips |
| ---- | ------ | ----- | --------- | ---------------- | -------------- |
| Mk9 − Mk8 | +0.11 m | 2.96 m | 104 m | 34.0% | — |
| Mk11 − Mk10 | −0.76 m | 36.55 m | 2082 m | 34.9% (24% by >10 m) | 45,654 |

A third of the globe moved between Mk10 and Mk11, some cells by over 2 km, and
45,654 points changed land/sea status. Substituting a neighbouring block's file
because the grids match puts heights badly wrong in mountainous cells while
looking perfectly well-formed.

---

## 4. Mk12 evidence

### No published ancillary

The tree was last written September 2022, when `UMG_Mk11_ps45` was added. Mk12
met has been arriving since December 2025 with no matching ancillary.

### Not inside the met files either

Checked across both file sets (`_I_` and `_M_`), the earliest
(`MO202512010000`) and latest (`MO202608102100`) timesteps, and two region tiles:

| Check | Mk12 | Mk11 |
| ----- | ---- | ---- |
| `m01s00i033` as a STASH field | absent | absent |
| Distinct STASH codes (`_I_` / `_M_`) | 18 / 12 | 18 / 12 — *identical set* |
| `surface_altitude` as an iris aux coord | absent | absent |
| Derived `altitude` coordinate | absent | absent |

The 3-D cubes carry `level_height` and `sigma` but no `surface_altitude`, so
`z = level_height + sigma * orog` cannot be closed from the met alone.

### The field has changed since Mk11

Mk12's grid is identical to Mk11's — the saved native grids agree to `0.0` in
both coordinates — so the Mk11 ancillary can at least be *subset* onto a Mk12
store without interpolation. But grid equality does not imply field equality.
Back-solving Mk12's orography from one raw region file and comparing against the
Mk11 ancillary, over world region 2:

| Compared against Mk11 ancillary | Mean ± std (land) | Max \|Δ\| | Points off by >25 m |
| ------------------------------- | ----------------- | --------- | ------------------- |
| Mk11 back-solve *(control)* | −0.66 ± 1.88 m | 21 m | **0** |
| Mk12 back-solve | −1.01 ± 18.19 m | 675 m | **3,650 (0.97%)** |

The control fixes the method's own error floor: against its own published
ancillary, nothing exceeds 25 m. Mk12 breaks that floor. **Mk12 uses a different
orography from Mk11**, and `UMG_Mk11_ps45` is a knowing approximation for it.

### Archive boundary

Mk11 and Mk12 overlap. 2025-12 is written in full under both (6,944 files each),
but Mk11's 2026-01 is only two-thirds written (4,704 files against Mk12's 6,944).
`get_Mk` therefore ends Mk11 at 2025-12 and starts Mk12 at 2026-01, putting the
switch on the year boundary so a whole extraction year sits in one block. Using
Mk11 for 2026-01 would silently drop about ten days.

---

## 5. Grid alignment

**The ancillary is on exactly the met's native grid.** Against the saved native
grids in `data/`, every Mk from 6 to 11 matches to `0.0` in both coordinates —
not "within tolerance", exactly equal.

Two steps align it:

**1. Normalise longitude.** The ancillary is `0…360`; stores are `-180…180`.
Apply the same transform `extract.py` uses, then re-sort — the ancillary is a
single global field, so normalising wraps it at the dateline:

```python
orog = orog.assign_coords(lon=((orog.lon + 180) % 360) - 180).sortby("lon")
```

(`extract.py` does not sort, because it normalises one region at a time and each
region is already contiguous. A whole global field is not.)

**2. Land it on the target grid.** In native mode the store's coordinates sit
exactly on the ancillary's ladder, so select with an explicit tolerance and let a
mismatch raise rather than snap to a neighbouring cell:

```python
orog = orog.sel(lat=met.lat.values, lon=met.lon.values, method="nearest", tolerance=1e-4)
```

`global` is the degenerate case: the ancillary already *is* a whole-globe native
field, so step 1 is the only work and nothing can misalign.

---

## 6. Measured output

| Domain | Mode | Grid | Size |
| ------ | ---- | ---- | ---- |
| NORTHAFRICA 2023 | footprint (interpolated) | 298 × 322 | 0.40 MB |
| EUROPE 2023 | native (subset) | 741 × 974 | 2.9 MB |
| GLOBAL 2023 | global | 1920 × 2560 | 19.7 MB |
| GLOBAL 2026 | global | 1920 × 2560 | 19.7 MB |

Checks that passed:

- **NORTHAFRICA lands exactly on `build_target_grid`'s output** — max abs
  difference `0.0` on both axes.
- **GLOBAL is a true whole globe** — spacing exactly 0.09375° × 0.140625°
  everywhere, spanning exactly 360° × 180°, longitude strictly monotonic (the
  dateline re-sort worked), zero NaNs.
- **GLOBAL 2023 and 2026 are identical arrays**, as they must be — both fall back
  to `UMG_Mk11_ps45`, and 2026 records `mk_version: 12` against that source.
- **The global file is a lossless intermediate** — re-interpolating it onto
  NORTHAFRICA's grid reproduces the NORTHAFRICA file to max |diff| `0.0000 m`,
  correlation 1.0.

NORTHAFRICA's −281.9 m minimum is shallower than the global −340.1 m because
interpolating onto its coarser 0.234° × 0.352° mesh smooths the extreme.

These confirm the output matches *what the config asks for*. Confirming it
matches a **store** is a separate check, worth running whenever a store's config
may have moved since it was written:

```python
assert np.array_equal(topog.lat.values, met.lat.values)
assert np.array_equal(topog.lon.values, met.lon.values)
```

---

## 7. Reproducing all of this

`/tmp` is full, so `TMPDIR` must be set or anything importing iris fails:

```bash
module load jaspy
export TMPDIR=/work/scratch-nopw2/$USER/tmp && mkdir -p "$TMPDIR"
```

### Which Mks share a field (§3)

Header validity times differ between `ps` directories even when the data is
identical, so compare the **data**, not the file:

```python
import glob, hashlib, iris, numpy as np

for path in sorted(glob.glob("/gws/ssde/j25a/name/data/UMAncils/Global/*/topog.pp")):
    cube = iris.load_cube(path)
    digest = hashlib.md5(np.ascontiguousarray(cube.data.astype("float64"))).hexdigest()[:12]
    print(f"{path.split('/')[-2]:<16} {str(cube.shape):<14} {digest}")
```

Directories printing the same digest carry the same field.

### Is the orography inside the met files? (§4)

Run this on a new block before assuming it still needs an ancillary. Headers
only — seconds per file. Check both file sets and both ends of the block:

```python
import iris, iris.fileformats.pp

PATH = ("/gws/ssde/j25a/name/met_archive/Global/UMG_Mk12PT/"
        "MO202512010000.UMG_Mk12_I_L59PT1.pp")

stash = sorted({str(f.stash) for f in iris.fileformats.pp.load(PATH)})
print(f"{len(stash)} STASH codes; m01s00i033 present: {'m01s00i033' in stash}")

# Orography can also arrive as a hybrid-height coord rather than a field.
cube = [c for c in iris.load(PATH) if c.name() == "air_temperature" and c.ndim == 3][0]
names = [c.name() for c in cube.coords()] + [c.name() for c in cube.derived_coords]
print(f"surface_altitude as a coordinate: {'surface_altitude' in names}")
```

### Back-solving a block's orography (§2, §4)

One region file for one timestep is enough (~180 MB, seconds to read) — **this is
not a met extraction, do not run one for this**. All 59 raw levels are available,
so the integration is tighter than a store-based solve:

```python
import iris, numpy as np

GRAVITY, R_DRY, TV_COEFF, SIGMA_CEILING = 9.80665, 287.05, 0.6077, 0.15
PATH = ("/gws/ssde/j25a/name/met_archive/Global/UMG_Mk12PT/"
        "MO202602010000.UMG_Mk12_I_L59PT2.pp")

cubes = iris.load(PATH)
pick = lambda name, ndim: [c for c in cubes if c.name() == name and c.ndim == ndim][0]

p_cube = pick("air_pressure", 3)
sigma = p_cube.coord("sigma").points.astype("float64")
level_height = p_cube.coord("level_height").points.astype("float64")

# upper level where sigma is small, so the sigma separation is large and the
# result is insensitive to integration error
k_hi = int(np.nonzero(sigma <= SIGMA_CEILING)[0][0])
n_lev = k_hi + 1

p = p_cube.data[:n_lev].astype("float64")
tv = pick("air_temperature", 3).data[:n_lev].astype("float64") * (
    1.0 + TV_COEFF * pick("specific_humidity", 3).data[:n_lev].astype("float64")
)

thickness = np.zeros(p.shape[1:])
for k in range(n_lev - 1):
    thickness += (R_DRY * 0.5 * (tv[k] + tv[k + 1]) / GRAVITY) * np.log(p[k] / p[k + 1])

orog = (thickness - (level_height[k_hi] - level_height[0])) / (sigma[k_hi] - sigma[0])
```

Compare against the ancillary sampled at the same points, and **always run the
same comparison for a block that does have a published ancillary** — that control
is what makes the number interpretable.

---

## 8. Provenance

Ported from [PR #5](https://github.com/elenafillo/extract_UM_met/pull/5)
(Kirstin Gerrand's `add_topog` branch, NZCSM-only), generalised for UM Global:
per-Mk file selection rather than one path per data type; longitude
normalisation; a branch on grid type instead of a hardcoded rotated-pole path;
`mk` passed to `build_target_grid`; and the `met_extract` → `extract_um_met`
rename.

The NZCSM rotated-pole path is **unexercised** — `native_topog_file` is still
`""` and PR #5's regrid call was ported to the current `rotated.py` API
(`latlon_regridder(src, target)`) without being run.
