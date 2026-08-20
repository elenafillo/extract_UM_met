# Known problems

Defects that have been found in this pipeline, quirks of individual files in the
archive that it cannot paper over, and quirks of JASMIN itself that arrive
disguised as either.

[Troubleshooting](troubleshooting.md) answers *"it printed this error, what do I
type"*. This page answers *"my output looks wrong"* and *"is the store I already
have affected"* — the failures that are silent, or that surface a long way from
their cause. Each entry says what went wrong, which output it touched, and
whether it is fixed.

---

## 1. Fixed defects

### 1.1 Footprint padding duplicated the footprint's own edges

*Found and fixed 2026-08-05. Affected every `footprint`-mode grid built before
that date.*

`_build_footprint_grid` padded outward with `range(edge_size)`, starting at
`i = 0` — so the first "padding" cell on each side was
`np.max(latitudes) + delta_lat * 0`, the footprint's own edge value a second
time. Two consequences:

- **A duplicated latitude and longitude in the target grid**, sitting at the
  footprint's own edges — mid-grid, not at the boundary. Each per-region
  intermediate inherited whichever duplicate fell inside it, which is what the
  `Dropping 1 duplicate values in 'longitude'` lines in the join log were.
- **One fewer padding cell per side than configured.** `edge_size: [100, 100]`
  produced 99 cells of padding, not 100.

No store contains a duplicated row: `drop_duplicate_coords` removed them during
the join, before anything was written. The lasting effect was the smaller
domain. Padding now starts at `i = 1`; a clean run prints no `Dropping N
duplicate values` lines at all.

### 1.2 The target grid was silently truncated to the domain's regions

*Warning added 2026-08-05. The truncation itself is inherent to how regions are
extracted — it is now loud instead of silent.*

Extraction interpolates a region onto the **whole** target grid and then slices
the result to that region's own bounds ([extract.py][extract-slice]). A target
point that falls outside every region in `world_regions_codes` therefore lands
in no region file at all and vanishes from the joined domain. Nothing said so:
the log printed

```
target grid: 384 lat x 308 lon
...
20170101 dataset ready: {'time': 56, 'levels': 20, 'lat': 359, 'lon': 306}
```

and treated that as success.

For **SA**, `edge_size_lat: [100, 100]` pads roughly 23° north of the footprint,
past the northern edge of regions 6 and 9 at 25.078125°N. Twenty-three rows were
dropped from every period. The domain stops at 25.078125°N regardless of what
the config asks for; reaching further north needs regions 2 and 5 added to
`world_regions_codes`.

`build_target_grid` now clips the grid to the union of the domain's regions and
prints a banner naming the regions that would keep the rest, so the grid a run
reports is the grid it writes.

---

## 2. File quirks

Oddities in the files, rather than in this code. They are worth knowing
because they break anything that assumes the archive is uniform.

### Mk9 and Mk10 cubes do not have the same structure

The Mk9 `.pp` cubes and before create `dim_0`, a bare `0, 1, 2, …` index coordinate along `time`. Nothing creates it
deliberately: `_build_time_axis` stacks the cube's forecast dimensions into a
MultiIndex, and one of those dimensions is anonymous in the Mk9 cubes, so it
arrives from `DataArray.from_iris` named `dim_0` and survives the stack as a
leftover level. Mk10 cubes do not have it. `dim_0` is now listed in `_DROP_VARIABLES` in `join.py`, so no new store gets
one.

The anonymous dimension has a cause, and it is the entry below: pre-Mk10 the
averaged cubes carry eight *distinct* `(forecast_period, forecast_reference_time)`
pairs rather than a 2 × 4 grid, so iris cannot merge them onto a named dimension.

### Before Mk10 the archive holds a flux window on only half the steps

*Found 2026-08-20, from the raw `.pp` files. Fixed the same day. Every store
built before the fix for a period up to **2017-06-30** is affected, silently —
the three time-averaged fields only.*

Up to Mk9 the flux and stress fields **interleave genuine 3-hour means with
zero-length `T+0` records**. Per day, per averaged field, eight records:

```
00:00  bounds [00:00, 00:00]   width 0 h   fp 0.0    <- T+0 analysis record
01:30  bounds [00:00, 03:00]   width 3 h   fp 4.5    <- genuine 3-hour mean
06:00  bounds [06:00, 06:00]   width 0 h   fp 0.0
07:30  bounds [06:00, 09:00]   width 3 h   fp 4.5
   … degenerate on 00/06/12/18, real windows ending 03/09/15/21
```

So there are only **four real windows a day, six hours apart**. The windows
between them — `[03,06]`, `[09,12]`, `[15,18]`, `[21,24]` — are **not in the
archive at all**; the 06:00 file holds exactly one flux field and it is the
degenerate one.

Two things make it hard to spot. The `T+0` records hold **real, plausible
values** (sensible heat flux `-324.9 / 3.25 / 216.9` min/mean/max against
`-236.8 / 3.11 / 194.6` for the mean beside it), and **`cell_methods` is
identical on both** — `mean over time, interval: 1 hour`. The **width of the time
bounds is the only discriminator.**

Verified across the whole archive, region PT2, all three averaged fields on
Mk6/8/9/10 and the heat flux on Mk7/11/12:

| Mk | day | widths (h) | zero-width | `forecast_period` | real windows/day | end spacing |
| -- | --- | ---------- | ---------- | ----------------- | ---------------- | ----------- |
| 6  | 20100120 | `[0, 3]` | 4/8 | `0, 4.5` | 4 | 6 h |
| 7  | 20130411 | `[0, 3]` | 4/8 | `0, 4.5` | 4 | 6 h |
| 8  | 20150505 | `[0, 3]` | 4/8 | `0, 4.5` | 4 | 6 h |
| 8  | 20150824 | `[0, 3]` | 4/8 | `0, 4.5` | 4 | 6 h |
| 9  | 20170710 | `[0, 3]` | 4/8 | `0, 4.5` | 4 | 6 h |
| 10 | 20210505 | `[3]` | 0/8 | `-1.5, 1.5` | 8 | 3 h |
| 11 | 20220401 | `[3]` | 0/8 | `-1.5, 1.5` | 8 | 3 h |
| 12 | 20251201 | `[3]` | 0/8 | `-1.5, 1.5` | 8 | 3 h |

**The break is exactly 2017-07-01**, the Mk9 → Mk10 boundary in
`iris_io.get_Mk`, so the affected span is everything up to 2017-06-30 — most of
the historical archive. The `forecast_period` change from `{0, 4.5}` to
`{-1.5, +1.5}` is the archive-side signature: from Mk10 each analysis carries the
window before it *and* the window after it, on the same four analyses the
instantaneous fields use.

**The instantaneous fields are unaffected** and identical across generations
(`fp {0, 3}` × analyses 00/06/12/18, eight steps). Only
`atmosphere_downward_eastward_stress`, `atmosphere_downward_northward_stress` and
`surface_upward_sensible_heat_flux` differ.

#### What it did to the stores

`snap` moves a bounded time point to its interval end, and a zero-width record's
end *is* its own timestamp. So the averaged fields came out on a full eight-step
axis, matching the instantaneous fields, and looked entirely healthy — while
**alternating an instantaneous `T+0` flux with a 3-hour mean from step to step**,
with nothing in the store recording which is which. Anything integrating or
averaging those fields over a pre-July-2017 period is working on half real means
and half instantaneous snapshots.

**Is the store I already have affected?** If it covers any year up to 2017, has
no `time_flux` coordinate, and its `flux_time_regrid` attribute is absent or
`snap`, then yes, for those three variables. There is no in-place repair —
rebuild the year with `--overwrite`.

#### The fix

Two parts, both automatic; no config change makes a pre-Mk10 store correct or
incorrect.

- **The degenerate records are dropped at read time.**
  `extract._drop_degenerate_windows` filters every zero-width record before
  anything measures or re-stamps a window, in all three `flux_time_regrid` modes.
  It is a no-op from Mk10. Without it the "2 different averaging windows" guard in
  `_averaging_attrs` also fires, because the surviving widths genuinely disagree.
- **A year containing any pre-Mk10 month is forced to `keep`.**
  `config.resolve_flux_time_regrid` returns `keep` below
  `MK_REGULAR_FLUX_WINDOWS = 10` whatever the domain asks for, since `snap` and
  `interpolate` both put one flux value on every instantaneous step and that
  archive has a window for only half of them. It resolves against
  `sources.MetSource.min_mk_in_year`, **not** the month in hand: Mk boundaries
  fall on month edges, so per-month resolution would put the averaged fields on
  `time_flux` for Jan–Jun 2017 and on `time` for Jul–Dec and append both into one
  store. **All of 2017 is therefore `keep`**, July onward included.

The override is logged, and the store's `flux_time_regrid` attribute records what
was *done*, not what was asked — so it is visible rather than silent. It does not
raise: the cadence is a property of the archive, not of the config, and the same
domain block is correct for 2016 and 2022, so raising would make any multi-year
run impossible to express.

A fixed pre-Mk10 day comes out `time = 8`, `time_flux = 4`, with `time_flux` at
`01:30, 07:30, 13:30, 19:30` — the four real midpoints in the `.pp` files, and
nothing invented. Note **`time` stays 8**: `keep` merges the fluxes onto a
differently *named* dimension, which is a union rather than a join, so the
instantaneous fields keep all eight steps. Under `snap` the same day would inner-
join four flux ends against eight instantaneous steps and halve the store.

Two consequences to carry: any consumer requiring a single time coordinate
(GLIDE's `met_schema.md` does) **cannot be served a pre-2018 store** as it
stands; and a derived quantity built from the stresses — `friction_velocity` —
inherits `time_flux` there while its other inputs sit on `time`, which an xarray
expression will broadcast into a `(time, time_flux)` outer product rather than
refuse.

### The 2014-02-01 15:00 dump carries the previous stamp's data time

Every field in the `MO201402011500` files has a validity time (`t1`) of 15:00 but
a **data time (`t2`) of 12:00**. Everywhere else the two agree. Iris reads the
mismatch as a three-hour meaning period, so the three time-processed fields
arrive at the interval *midpoint*, with bounds `[12:00, 15:00]`:

```
atmosphere_downward_eastward_stress      time=['2014-02-01 13:30:00']
atmosphere_downward_northward_stress     time=['2014-02-01 13:30:00']
surface_upward_sensible_heat_flux        time=['2014-02-01 13:30:00']
```

while the other sixteen cubes sit at 15:00. The files themselves are sound: the
gzip is intact, all 374 fields decode, and `iris.load` returns the full cube set.

It is the whole dump, not one file — tiles 6, 8, 9, 10 and 13 all show it, and
the 12:00 and 18:00 stamps either side are clean. That includes the region-8
file re-archived on 2020-06-27; the later archive date changes nothing, it
carries the same `t2`.

The pipeline already absorbs this. Under `snap`, `_stamp_averaged_time` moves a
bounded time point to its interval end, which here is exactly 15:00, putting the
averaged fields back on the shared axis. 2014 is pre-Mk10, so the mode is now
forced to `keep` (above) and they instead stay at 13:30 on `time_flux` — the
genuine midpoint of the `[12:00, 15:00]` window, and one of the four midpoints
that era has every day. Either way the day needs no special handling.

---

## 3. Environment quirks

Neither the files nor this code, but they surface as if they were.

### A job can fail with "No such file" for a file that is definitely there

A run dies about a minute in, inside `iris.load`, on an archive file you can list
and read perfectly well from a login node:

```
FileNotFoundError: [Errno 2] No such file or directory:
  '/gws/ssde/j25a/name/met_archive/Global/UMG_Mk11PT/MO202301180000.UMG_Mk11_I_L59PT2.pp'
```

The archive entries are **symlinks into `/badc`**:

```
MO202301180000.UMG_Mk11_I_L59PT2.pp -> /badc/name_nwp/data/global/UMG_Mk11/2023/01/18/...
```

`MetSource.list_files` finds files with `glob.glob`, which matches a symlink by
name **without following it**, so the run reports the full set ("8 file(s) to
load") and looks healthy. `iris.load` then calls `open()`, which does follow the
link — and if `/badc` is not resolvable on the node the job landed on, the link
dangles and the open fails with ENOENT. A glob that succeeds followed by an open
that reports the file missing is the signature.

**Not every node has the archive.** This is a property of the node, not of the
day or the file: an identical job minutes earlier, on a different node of the
same partition, read the same days without complaint. Don't treat any particular
node as the culprit — assume any node may come up without `/badc` and check.

Before concluding data is missing, check from a login node — `ls -laL` on the
file (dereferencing the link) and a count of the day's files against a day you
know works. If those are fine, the archive is fine.

**Resubmit.** It usually lands on a working node; `--exclude=<node>` if it keeps
returning to a bad one. For anything longer than a smoke test, preflight it so a
bad node costs seconds rather than a wasted allocation:

```bash
echo "running on $(hostname)"
PROBE=<one file the run needs>
if ! head -c 1 "$PROBE" >/dev/null 2>&1; then
    echo "PREFLIGHT FAIL on $(hostname): cannot read $PROBE"
    echo "  target: $(readlink -f "$PROBE" 2>/dev/null || echo '<unresolvable>')"
    exit 1
fi
```
