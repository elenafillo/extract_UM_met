"""
Command-line interface for meteorology extraction.

Provides subcommands for extracting meteorology data:
  - run: extract and join monthly data, append to yearly zarr store
  - extract: extract per-region intermediates only
  - make-native-grid: save native UM grids for reference

Example usage:
  python -m extract_um_met run --domain SA --date 2016
  python -m extract_um_met extract --domain SA --date 201601 --region 6
  python -m extract_um_met make-native-grid --mk 10
"""

import argparse
import calendar
import shutil
import sys
from collections import Counter
from pathlib import Path

import iris
import numpy as np
import xarray as xr
import yaml

from .config import Config, load_config, resolve_config_value, store_stem
from .iris_io import get_Mk, load_iris, remove_coord_callback
from .regions import get_saved_region_bounds
from .grid import extract_native_grid, save_native_grid, build_target_grid
from .extract import extract_region, extract_single
from .join import join_month, cleanup_region_intermediates
from .sources import get_source
from .zarr_io import append_month_to_year_store, finalize_attrs, resolve_output_chunks


# One sample timestep per Mk, for make-native-grid. The native grid is fixed
# within an Mk block, so any timestep in range gives the same answer — naming
# one lets us build the exact path instead of listing the Mk folder.
MK_SAMPLE_TIMESTEPS = {
    6: "201201010000",
    7: "201401010000",
    8: "201501010000",
    9: "201601010000",
    10: "201801010000",
    11: "202301010000",
    12: "202603010000",
}


def _make_native_grid_parser(subparsers):
    """Create the 'make-native-grid' subcommand parser."""
    parser = subparsers.add_parser(
        'make-native-grid',
        help='Extract and save native UM grids for reference'
    )

    parser.add_argument(
        '--mk',
        type=int,
        default=None,
        help='Mk version to extract (6, 7, 8, 9, 10, 11, 12). If None, extracts all available.'
    )

    parser.add_argument(
        '--output-dir',
        type=str,
        default=None,
        help='Output directory for native grids (overrides config.native_grid_directory)'
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done, but do not save grids'
    )

    parser.set_defaults(func=cmd_make_native_grid)
    return parser


def cmd_make_native_grid(args):
    """
    Extract and save native UM grids for all 14 world regions.

    For each Mk version, extracts the native grid from one file per region,
    calculates resolution (dlat/dlon), and saves:
      - Per-region grids (native_grid_Mk{mk}_region{r}.nc)
      - Stitched global grid (native_grid_Mk{mk}_global.nc)
      - Metadata with resolutions (native_grid_Mk{mk}_info.yaml)
    """
    import os
    import numpy as np
    import xarray as xr
    import yaml

    cfg = Config()
    output_dir = args.output_dir or cfg.get('native_grid_directory', 'data/')
    try:
        os.makedirs(output_dir, exist_ok=True)
    except FileExistsError:
        pass

    # Determine which Mk(s) to extract
    if args.mk is not None:
        mks = [args.mk]
    else:
        mks = sorted(MK_SAMPLE_TIMESTEPS)

    # Native grids are a Global-UM concept; take the archive path from that source.
    source = get_source("UM_Global", cfg)
    met_archive_dir = source.archive_directory
    if not met_archive_dir:
        print("ERROR: UM_Global archive not configured "
              "(set data_types.UM_Global.archive_directory)")
        sys.exit(1)

    # Mk6-9 files are gzipped and must be decompressed before iris reads them.
    # Use the configured scratch path like the rest of the pipeline — the default
    # tempfile location is /tmp, a 2 GB tmpfs on JASMIN nodes.
    scratch_dir = resolve_config_value(cfg.get('scratch_path', ''), cfg.data)
    os.makedirs(scratch_dir, exist_ok=True)

    # All 14 world regions
    all_regions = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]

    for mk in mks:
        try:
            print(f"\n{'='*60}")
            print(f"Extracting native grids for Mk {mk}")
            print(f"{'='*60}")

            mk_folder = os.path.join(met_archive_dir, f"UMG_Mk{mk}PT")

            if not os.path.isdir(mk_folder):
                print(f"  WARNING: Mk folder not found: {mk_folder}")
                print(f"  Skipping Mk {mk}")
                continue

            sample_date = MK_SAMPLE_TIMESTEPS[mk]
            print(f"Sample timestep: {sample_date}")

            # Extract grids for each region
            region_grids = {}  # region_id -> (lat, lon)
            resolution_info = {}  # region_id -> {dlat, dlon}

            for region_id in all_regions:
                try:
                    sample_file = source.filepath(sample_date, region=region_id, mk=mk)

                    if not os.path.exists(sample_file):
                        print(f"  Region {region_id:2d}: No file at {os.path.basename(sample_file)}")
                        continue

                    print(f"  Region {region_id:2d}: Loading {os.path.basename(sample_file)}")

                    try:
                        # Handle .pp.gz files by decompressing first
                        if sample_file.endswith('.gz'):
                            import gzip
                            import shutil
                            tmp_path = os.path.join(
                                scratch_dir, os.path.basename(sample_file)[:-len('.gz')]
                            )
                            with gzip.open(sample_file, 'rb') as f_in, open(tmp_path, 'wb') as f_out:
                                shutil.copyfileobj(f_in, f_out)
                            try:
                                cubes = iris.load(tmp_path, callback=remove_coord_callback)
                            finally:
                                os.remove(tmp_path)
                        else:
                            cubes = iris.load(sample_file, callback=remove_coord_callback)
                    except Exception as e:
                        print(f"              WARNING: Could not load: {e}")
                        continue

                    if not cubes or len(cubes) == 0:
                        print(f"              WARNING: No cubes loaded")
                        continue

                    cube = cubes[0]
                    lat, lon = extract_native_grid(cube)

                    # Calculate resolution
                    if lat.ndim == 1:
                        dlat = np.abs(np.diff(lat)).mean()
                    else:
                        dlat = np.abs(np.diff(lat, axis=0)).mean()

                    if lon.ndim == 1:
                        dlon = np.abs(np.diff(lon)).mean()
                    else:
                        dlon = np.abs(np.diff(lon, axis=1)).mean()

                    region_grids[region_id] = (lat, lon)
                    resolution_info[region_id] = {'dlat': float(dlat), 'dlon': float(dlon), 'shape': lat.shape}

                    print(f"              ✓ Extracted: shape {lat.shape}, dlat={dlat:.6f}, dlon={dlon:.6f}")

                    # Save per-region grid
                    if not args.dry_run:
                        region_grid_file = os.path.join(output_dir, f"native_grid_Mk{mk}_region{region_id:02d}.nc")
                        ds = xr.Dataset({
                            'latitude': (['y', 'x'] if lat.ndim == 2 else ['latitude'], lat),
                            'longitude': (['y', 'x'] if lon.ndim == 2 else ['longitude'], lon),
                        })
                        ds.attrs['mk_version'] = mk
                        ds.attrs['region_id'] = region_id
                        ds.attrs['dlat'] = float(dlat)
                        ds.attrs['dlon'] = float(dlon)
                        ds.to_netcdf(region_grid_file)

                except Exception as e:
                    print(f"              ERROR: {e}")
                    import traceback
                    traceback.print_exc()
                    continue

            if not region_grids:
                print(f"  ERROR: No regions extracted for Mk {mk}")
                continue

            # Stitch regions into global grid
            print(f"\n  Stitching {len(region_grids)} regions into global grid...")
            try:
                from .regions import build_domain_grid, drop_duplicate_coords

                # Build full global grid as an xarray Dataset with all regions
                global_lats = []
                global_lons = []

                for region_id in sorted(region_grids.keys()):
                    lat, lon = region_grids[region_id]
                    if lat.ndim == 1:
                        global_lats.extend(lat.tolist())
                    else:
                        global_lats.extend(lat.flatten().tolist())
                    if lon.ndim == 1:
                        global_lons.extend(lon.tolist())
                    else:
                        global_lons.extend(lon.flatten().tolist())

                # Create merged coordinates (unique values, sorted)
                global_lat_unique = np.unique(np.array(global_lats))
                global_lon_unique = np.unique(np.array(global_lons))

                # Unwrap longitude if it wraps around (e.g., 0-405 instead of -180-180 or 0-360)
                # This happens when data crosses the dateline
                lon_range = global_lon_unique.max() - global_lon_unique.min()
                if lon_range > 350:  # Likely wraps around
                    # Normalize to [-180, 180]
                    global_lon_unique = np.where(global_lon_unique > 180, global_lon_unique - 360, global_lon_unique)
                    global_lon_unique = np.unique(global_lon_unique)  # Remove duplicates and re-sort
                    print(f"    Longitude unwrapped from wrapping format")

                print(f"    Global lat points: {len(global_lat_unique)} (range: {global_lat_unique.min():.2f} to {global_lat_unique.max():.2f})")
                print(f"    Global lon points: {len(global_lon_unique)} (range: {global_lon_unique.min():.2f} to {global_lon_unique.max():.2f})")

                # Save global grid
                if not args.dry_run:
                    global_grid_file = os.path.join(output_dir, f"native_grid_Mk{mk}_global.nc")
                    ds_global = xr.Dataset({
                        'latitude': (['latitude'], global_lat_unique),
                        'longitude': (['longitude'], global_lon_unique),
                    })
                    ds_global.attrs['mk_version'] = mk
                    ds_global.attrs['num_regions'] = len(region_grids)
                    ds_global.attrs['regions'] = ','.join(str(r) for r in sorted(region_grids.keys()))
                    ds_global.to_netcdf(global_grid_file)
                    print(f"  ✓ Saved global grid to {os.path.basename(global_grid_file)}")

                # Save metadata
                if not args.dry_run:
                    info_file = os.path.join(output_dir, f"native_grid_Mk{mk}_info.yaml")
                    info = {
                        'mk_version': mk,
                        'regions': resolution_info,
                        'global': {
                            'num_lat_points': len(global_lat_unique),
                            'num_lon_points': len(global_lon_unique),
                            'lat_range': [float(global_lat_unique.min()), float(global_lat_unique.max())],
                            'lon_range': [float(global_lon_unique.min()), float(global_lon_unique.max())],
                        }
                    }
                    with open(info_file, 'w') as f:
                        yaml.dump(info, f, default_flow_style=False)
                    print(f"  ✓ Saved metadata to {os.path.basename(info_file)}")
                else:
                    print(f"  (dry-run) Would save global grid and metadata")

            except Exception as e:
                print(f"  ERROR stitching global grid: {e}")
                import traceback
                traceback.print_exc()
                continue

        except Exception as e:
            print(f"  ERROR processing Mk {mk}: {e}")
            import traceback
            traceback.print_exc()
            continue

    print(f"\n{'='*60}")
    print("Done!")
    print(f"{'='*60}")


def _make_run_parser(subparsers):
    """Create the 'run' subcommand parser."""
    parser = subparsers.add_parser(
        'run',
        help='Extract, join, and append meteorology data to yearly zarr store'
    )

    parser.add_argument(
        '--domain',
        type=str,
        required=True,
        help='Domain key (e.g., SA) or domain_name (e.g., SOUTHAMERICA)'
    )

    parser.add_argument(
        '--date',
        type=str,
        required=True,
        help='Date to process: YYYY (full year), YYYYMM (single month), or '
             'YYYYMMDD (single day → separate debug store, cheap smoke test)'
    )

    parser.add_argument(
        '--overwrite',
        action='store_true',
        help='Overwrite existing yearly store (default: resume from where it left off)'
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done, but do not write to zarr'
    )

    parser.add_argument(
        '--grid-mode',
        type=str,
        choices=['footprint', 'regular', 'native'],
        help='Override grid mode (normally from config)'
    )

    parser.add_argument(
        '--zarr-format',
        type=int,
        default=None,
        help='Zarr format version to write (2 or 3). Defaults to config.zarr_format.'
    )

    parser.add_argument(
        '--keep-intermediates',
        action='store_true',
        help='Keep per-region zarr intermediates in scratch (default: delete after append)'
    )

    parser.add_argument(
        '--suffix',
        type=str,
        default=None,
        help='Optional tag appended to the domain name in the output store: '
             '{domain}/{domain}_{suffix}_Met_{date}.zarr. Lets a variant run '
             '(e.g. NA_coarse) sit beside the main NA store without overwriting it.'
    )

    parser.set_defaults(func=cmd_run)
    return parser


def _parse_date_arg(date_str):
    """
    Parse the --date argument into a list of (year, month, day) tuples.

    Accepts 'YYYY' (all 12 months), 'YYYYMM' (single month), or 'YYYYMMDD'
    (single day, for a cheap pipeline smoke test). day is None for year/month.
    'all' is not supported (there is no bounded range to iterate).

    Returns
    -------
    list of tuple
        List of (year, month, day) tuples.
    """
    date_str = str(date_str).strip()

    if date_str.lower() == 'all':
        raise ValueError(
            "--date all is not supported for extraction; specify a year (YYYY), "
            "month (YYYYMM), or day (YYYYMMDD)."
        )

    if len(date_str) == 4 and date_str.isdigit():
        year = int(date_str)
        return [(year, m, None) for m in range(1, 13)]

    if len(date_str) == 6 and date_str.isdigit():
        year = int(date_str[:4])
        month = int(date_str[4:6])
        if not 1 <= month <= 12:
            raise ValueError(f"Month out of range in --date {date_str}")
        return [(year, month, None)]

    if len(date_str) == 8 and date_str.isdigit():
        year = int(date_str[:4])
        month = int(date_str[4:6])
        day = int(date_str[6:8])
        if not 1 <= month <= 12:
            raise ValueError(f"Month out of range in --date {date_str}")
        if not 1 <= day <= 31:
            raise ValueError(f"Day out of range in --date {date_str}")
        return [(year, month, day)]

    raise ValueError(f"Could not parse --date '{date_str}'. Use YYYY, YYYYMM, or YYYYMMDD.")


def _read_present_months(store_path, year):
    """Return the set of 'MM' strings already present in a yearly zarr store."""
    try:
        with xr.open_zarr(str(store_path), consolidated=True) as ds:
            times = np.asarray(ds.time.values).astype("datetime64[s]")
        return {str(t)[5:7] for t in times if str(t)[:4] == str(year)}
    except Exception as exc:
        print(f"  WARNING: could not read existing store {store_path}: {exc}")
        return set()


def _expand_days(periods):
    """Expand (year, month, day|None) periods into a list of (year, 'YYYYMMDD')."""
    out = []
    for year, month, day in periods:
        if day is not None:
            out.append((year, f"{year}{month:02d}{day:02d}"))
        else:
            for dd in range(1, calendar.monthrange(year, month)[1] + 1):
                out.append((year, f"{year}{month:02d}{dd:02d}"))
    return out


def _read_present_days(store_path, year):
    """Return the set of 'YYYYMMDD' day keys already present in a store."""
    try:
        with xr.open_zarr(str(store_path), consolidated=True) as ds:
            times = np.asarray(ds.time.values).astype("datetime64[s]")
        return {str(t)[:10].replace("-", "") for t in times if str(t)[:4] == str(year)}
    except Exception as exc:
        print(f"  WARNING: could not read existing store {store_path}: {exc}")
        return set()


def _run_non_tiled(cfg, domain_key, domain_cfg, domain_name, grid_mode,
                   zarr_format, periods, zarr_dir, args, source, suffix=None,
                   output_chunks=None):
    """
    Run the extract → append path for a non-tiled single-file source (e.g. NZCSM).

    There is no join: each day is extracted whole-domain (regridded onto the
    target grid) and appended to the yearly store, one day-batch at a time so the
    big single-file timesteps never all sit in memory.
    """
    stem = store_stem(domain_name, suffix)
    target_lat, target_lon, _ = build_target_grid(domain_cfg, cfg)
    target = (target_lat, target_lon)
    print(f"    non-tiled {source.name}: target grid {len(target_lat)} lat x "
          f"{len(target_lon)} lon; batch = 1 day")

    day_items = _expand_days(periods)
    years = {}
    for year, day_key in day_items:
        years.setdefault(year, []).append(day_key)

    scratch_root = resolve_config_value(cfg.get("scratch_path", ""), cfg.data)
    scratch_dir = str(Path(scratch_root) / "files")

    written_stores = []   # zarr stores finalized this run, printed at the end
    for year in sorted(years):
        day_keys = sorted(years[year])
        store_path = Path(zarr_dir) / domain_name / f"{stem}_Met_{year}.zarr"
        print(f"\n=== {year} → {store_path} ({len(day_keys)} day(s)) ===")

        store_exists = store_path.exists()
        fresh = args.overwrite or not store_exists
        if store_exists and args.overwrite and not args.dry_run:
            print("    --overwrite: removing existing store")
            shutil.rmtree(store_path)

        to_do = day_keys
        if store_exists and not args.overwrite:
            present = _read_present_days(store_path, year)
            if present:
                last = max(present)
                to_do = [d for d in day_keys if d not in present and d > last]
                if present:
                    print(f"    already present: {len(present)} day(s); to do: {len(to_do)}")

        if not to_do:
            print("    nothing to do.")
            continue

        if args.dry_run:
            print(f"    [DRY RUN] would extract+append {len(to_do)} day(s) to {store_path}")
            continue

        for i, day_key in enumerate(to_do):
            print(f"\n  --- {domain_name} {day_key} ({source.name}) ---")
            day_ds = extract_single(domain_key, day_key, cfg, target=target, source=source,
                                    suffix=suffix)
            first = fresh and i == 0
            append_month_to_year_store(day_ds, store_path, zarr_format=zarr_format,
                                       first=first, chunks=output_chunks)
            print(f"  appended {day_key} (first={first})")

        finalize_attrs(store_path, extra_attrs={"grid_mode": grid_mode, "data_type": source.name},
                       chunks=output_chunks)
        print(f"    finalized store attributes for {year}")
        written_stores.append(store_path)

    print("\nDone.")
    if written_stores:
        print("Saved to:")
        for path in written_stores:
            print(f"  {path}")
    elif args.dry_run:
        print("(dry run — nothing written)")


def _run_single_day(cfg, domain_key, domain_cfg, domain_name, grid_mode,
                    zarr_format, ymd, zarr_dir, args, source, suffix=None,
                    output_chunks=None):
    """
    Run the full extract → join → write path for a single day.

    Writes a standalone ``{DOMAIN}_Met_{YYYYMMDD}.zarr`` debug store (always a
    fresh ``mode='w'`` write) so a smoke test never touches the real yearly
    stores. Loads only that day's files per region.
    """
    stem = store_stem(domain_name, suffix)
    year, month, day = ymd
    tag = f"{year}{month:02d}{day:02d}"
    store_path = Path(zarr_dir) / domain_name / f"{stem}_Met_{tag}.zarr"
    print(f"\n=== single-day smoke test {tag} → {store_path} ===")

    mk = source.get_mk(year, month)
    if grid_mode == "native":
        target_lat, target_lon, _ = build_target_grid(domain_cfg, cfg, mk=mk)
        use_interp = False
    else:
        target_lat, target_lon, _ = build_target_grid(domain_cfg, cfg)
        use_interp = True
    target = (target_lat, target_lon)
    mk_label = f"Mk{mk}" if mk is not None else source.name
    print(f"    {mk_label}, {'native passthrough' if not use_interp else 'interp'}, "
          f"target grid {len(target_lat)} lat x {len(target_lon)} lon")

    if args.dry_run:
        print(f"    [DRY RUN] would extract+join {tag} and write to {store_path}")
        return

    if store_path.exists():
        if args.overwrite:
            print("    --overwrite: removing existing debug store")
            shutil.rmtree(store_path)
        else:
            print(f"    store already exists; use --overwrite to rebuild. Skipping.")
            return

    scratch_root = resolve_config_value(cfg.get("scratch_path", ""), cfg.data)
    scratch_dir = str(Path(scratch_root) / "files")

    day_ds = join_month(
        domain_key, year, month, cfg,
        target=target, use_interp=use_interp, scratch_dir=scratch_dir, day=day,
        source=source, suffix=suffix,
    )
    append_month_to_year_store(day_ds, store_path, zarr_format=zarr_format, first=True,
                               chunks=output_chunks)
    print(f"    wrote {tag} to {store_path}")

    if not args.keep_intermediates:
        cleanup_region_intermediates(domain_name, year, month, scratch_dir, day=day,
                                     suffix=suffix)

    finalize_attrs(store_path, extra_attrs={"grid_mode": grid_mode, "single_day": tag},
                   chunks=output_chunks)
    print("    finalized store attributes.")
    print("\nDone (single-day smoke test).")
    print(f"Saved to:\n  {store_path}")


def cmd_run(args):
    """Extract, join, and append monthly data to a yearly zarr store."""
    cfg = Config()
    domain_key = cfg.resolve_domain_name(args.domain)
    domain_cfg = cfg.get_domain(domain_key)
    domain_name = domain_cfg["domain_name"]
    suffix = getattr(args, "suffix", None)
    stem = store_stem(domain_name, suffix)

    # Optional grid-mode override.
    if args.grid_mode:
        domain_cfg = dict(domain_cfg)
        grid_spec = dict(domain_cfg.get("grid", {}))
        grid_spec["mode"] = args.grid_mode
        domain_cfg["grid"] = grid_spec
        cfg.data["domains"][domain_key] = domain_cfg
    grid_mode = domain_cfg.get("grid", {}).get("mode", "footprint")

    # Resolved up front so a bad output_chunks fails before any extraction runs.
    output_chunks = resolve_output_chunks(domain_cfg)

    zarr_format = args.zarr_format if args.zarr_format is not None else cfg.get("zarr_format", 2)

    source = get_source(domain_cfg.get("data_type", "UM_Global"), cfg)

    periods = _parse_date_arg(args.date)

    zarr_dir = resolve_config_value(cfg.get("zarr_save_directory", ""), cfg.data)
    if not zarr_dir:
        print("ERROR: zarr_save_directory not configured")
        sys.exit(1)

    suffix_note = f"  |  suffix: {suffix}" if suffix else ""
    print(f"Domain: {domain_name} ({domain_key})  |  data type: {source.name}  |  "
          f"grid mode: {grid_mode}  |  zarr v{zarr_format}{suffix_note}")
    print(f"Output chunks: {output_chunks}")

    # Non-tiled sources (e.g. NZCSM): single file per timestep, no join; extract
    # and append a day at a time.
    if not source.tiled:
        _run_non_tiled(cfg, domain_key, domain_cfg, domain_name, grid_mode,
                       zarr_format, periods, zarr_dir, args, source, suffix=suffix,
                       output_chunks=output_chunks)
        return

    # Single-day smoke test: write a separate debug store, no resume/append.
    if len(periods) == 1 and periods[0][2] is not None:
        _run_single_day(cfg, domain_key, domain_cfg, domain_name, grid_mode,
                        zarr_format, periods[0], zarr_dir, args, source, suffix=suffix,
                        output_chunks=output_chunks)
        return

    # Group requested months by year (one yearly store each).
    years = {}
    for year, month, _day in periods:
        years.setdefault(year, []).append(month)

    written_stores = []   # zarr stores finalized this run, printed at the end
    for year in sorted(years):
        months = sorted(years[year])
        store_path = Path(zarr_dir) / domain_name / f"{stem}_Met_{year}.zarr"
        print(f"\n=== {year} → {store_path} ===")
        print(f"    requested months: {[f'{m:02d}' for m in months]}")

        # Build the target grid. For native mode, snap the whole year to one
        # canonical Mk grid (majority by month count) so time-appends stay aligned;
        # months on that Mk are passed through, off-Mk months are interpolated.
        canonical_mk = None
        if grid_mode == "native":
            mks = [source.get_mk(year, m) for m in months]
            canonical_mk = Counter(mks).most_common(1)[0][0]
            target_lat, target_lon, _ = build_target_grid(domain_cfg, cfg, mk=canonical_mk)
            print(f"    native canonical Mk: {canonical_mk} "
                  f"(grid {len(target_lat)}x{len(target_lon)})")
        else:
            target_lat, target_lon, _ = build_target_grid(domain_cfg, cfg)
            print(f"    target grid: {len(target_lat)} lat x {len(target_lon)} lon")
        target = (target_lat, target_lon)

        # Resume vs fresh.
        store_exists = store_path.exists()
        fresh = args.overwrite or not store_exists

        if store_exists and args.overwrite and not args.dry_run:
            print(f"    --overwrite: removing existing store")
            shutil.rmtree(store_path)

        if store_exists and not args.overwrite:
            present = _read_present_months(store_path, year)
            if present:
                last_present = max(present)
                to_do = [m for m in months if f"{m:02d}" not in present and f"{m:02d}" > last_present]
                skipped = [f"{m:02d}" for m in months if f"{m:02d}" in present]
                blocked = [f"{m:02d}" for m in months
                           if f"{m:02d}" not in present and f"{m:02d}" < last_present]
                if skipped:
                    print(f"    already present, skipping: {skipped}")
                if blocked:
                    print(f"    WARNING: cannot backfill earlier missing month(s) {blocked} "
                          f"via append; use --overwrite to rebuild the year.")
            else:
                to_do = months
        else:
            to_do = months

        if not to_do:
            print("    nothing to do (all requested months present).")
            if not args.dry_run:
                finalize_attrs(store_path, extra_attrs={"grid_mode": grid_mode})
                written_stores.append(store_path)
            continue

        print(f"    months to process: {[f'{m:02d}' for m in to_do]}")

        if args.dry_run:
            print(f"    [DRY RUN] would write {len(to_do)} month(s) to {store_path}")
            continue

        scratch_root = resolve_config_value(cfg.get("scratch_path", ""), cfg.data)
        scratch_dir = str(Path(scratch_root) / "files")

        for i, month in enumerate(to_do):
            month_mk = source.get_mk(year, month)
            use_interp = (grid_mode != "native") or (month_mk != canonical_mk)

            mk_label = f"Mk{month_mk}" if month_mk is not None else source.name
            print(f"\n  --- {domain_name} {year}-{month:02d} ({mk_label}, "
                  f"{'interp' if use_interp else 'native'}) ---")
            month_ds = join_month(
                domain_key, year, month, cfg,
                target=target, use_interp=use_interp, scratch_dir=scratch_dir,
                source=source, suffix=suffix,
            )

            first = fresh and i == 0
            append_month_to_year_store(month_ds, store_path, zarr_format=zarr_format,
                                       first=first, chunks=output_chunks)
            print(f"  appended {year}-{month:02d} to store (first={first})")

            if not args.keep_intermediates:
                cleanup_region_intermediates(domain_name, year, month, scratch_dir,
                                             suffix=suffix)

        finalize_attrs(
            store_path,
            extra_attrs={
                "grid_mode": grid_mode,
                "native_canonical_mk": int(canonical_mk) if canonical_mk else "n/a",
            },
            chunks=output_chunks,
        )
        print(f"    finalized store attributes for {year}")
        written_stores.append(store_path)

    print("\nDone.")
    if written_stores:
        print("Saved to:")
        for path in written_stores:
            print(f"  {path}")
    elif args.dry_run:
        print("(dry run — nothing written)")


def _make_extract_parser(subparsers):
    """Create the 'extract' subcommand parser."""
    parser = subparsers.add_parser(
        'extract',
        help='Extract per-region intermediates only'
    )

    parser.add_argument(
        '--domain',
        type=str,
        required=True,
        help='Domain key (e.g., SA) or domain_name (e.g., SOUTHAMERICA)'
    )

    parser.add_argument(
        '--date',
        type=str,
        required=True,
        help='Date to extract: YYYYMM (whole month) or YYYYMMDD (single day)'
    )

    parser.add_argument(
        '--region',
        type=int,
        help='Specific region to extract (if not specified, extracts all for domain)'
    )

    parser.add_argument(
        '--suffix',
        type=str,
        default=None,
        help='Optional tag appended to the domain name in the intermediate '
             'filenames ({domain}_{suffix}_Met_{date}_{region}.zarr), keeping a '
             'variant extraction separate from the main one in scratch.'
    )

    parser.set_defaults(func=cmd_extract)
    return parser


def cmd_extract(args):
    """Extract per-region intermediates for one domain/month (or single day)."""
    cfg = Config()
    domain_key = cfg.resolve_domain_name(args.domain)
    domain_cfg = cfg.get_domain(domain_key)
    suffix = getattr(args, "suffix", None)

    date_str = str(args.date).strip()
    if not date_str.isdigit() or len(date_str) not in (6, 8):
        print(f"ERROR: extract expects --date YYYYMM or YYYYMMDD, got '{args.date}'")
        sys.exit(1)
    year = int(date_str[:4])
    month = int(date_str[4:6])
    day = int(date_str[6:8]) if len(date_str) == 8 else None

    source = get_source(domain_cfg.get("data_type", "UM_Global"), cfg)

    # Non-tiled sources (e.g. NZCSM): one file over the whole domain, no regions.
    if not source.tiled:
        print(f"Extracting {domain_cfg['domain_name']} {date_str} "
              f"({source.name}, non-tiled single file)")
        path = extract_single(domain_key, date_str, cfg, source=source, save=True,
                              suffix=suffix)
        print(f"  → {path}")
        return

    if args.region is not None:
        regions = [args.region]
    else:
        regions = domain_cfg["world_regions_codes"]

    # Build the target grid once for this period's Mk.
    mk = source.get_mk(year, month)
    target_lat, target_lon, use_interp = build_target_grid(domain_cfg, cfg, mk=mk)
    target = (target_lat, target_lon)

    day_str = f"-{day:02d}" if day is not None else ""
    scope = "single day" if day is not None else "whole month"
    mk_label = f"Mk{mk}" if mk is not None else source.name
    print(f"Extracting {domain_cfg['domain_name']} {year}-{month:02d}{day_str} "
          f"({source.name}, {mk_label}, {scope}, regions {regions})")

    for region_id in regions:
        path = extract_region(
            domain_key, year, month, region_id, cfg,
            target=target, use_interp=use_interp, day=day, source=source,
            suffix=suffix,
        )
        print(f"  region {region_id} → {path}")


def main(argv=None):
    """Main entry point for the CLI."""
    parser = argparse.ArgumentParser(
        description='Extract UM meteorology data to yearly zarr stores',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    subparsers = parser.add_subparsers(
        title='subcommands',
        description='Available commands',
        dest='command'
    )

    _make_run_parser(subparsers)
    _make_extract_parser(subparsers)
    _make_native_grid_parser(subparsers)

    args = parser.parse_args(argv)

    if not hasattr(args, 'func'):
        parser.print_help()
        sys.exit(1)

    try:
        args.func(args)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
