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

The pipeline already absorbs this. `_restamp_to_interval_end` moves a bounded
time point to its interval end, which here is exactly 15:00, putting the averaged
fields back on the shared axis.

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
