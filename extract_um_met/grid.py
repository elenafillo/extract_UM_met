"""
Grid building and native UM grid management.

Functions to construct target extraction grids based on grid mode (footprint/regular/native),
save and load native UM grids for reference, and utilities for working with grid specs.
"""

import os
import numpy as np
import xarray as xr
from pathlib import Path

from .config import load_config, resolve_config_value, Config
from .regions import get_saved_region_bounds, find_overlapping_regions
from .iris_io import load_iris, get_Mk


def extract_native_grid(cube, world_region_id=None):
    """
    Extract the native UM latitude/longitude grid from an iris cube.

    Reads the cube's native coordinates without any interpolation. If a
    world_region_id is provided, returns the global grid; the caller should
    subset it if needed.

    Parameters
    ----------
    cube : iris.Cube
        The cube from which to extract the native grid.
    world_region_id : int, optional
        World region ID (for documentation/naming only; not used for subsetting).

    Returns
    -------
    lat_array, lon_array : np.ndarray
        1D or 2D native latitude and longitude arrays from the cube's coordinates.
    """
    # Get the cube's coordinate names (may be 'latitude' and 'longitude' or similar)
    lat_coord = None
    lon_coord = None

    for coord in cube.coords():
        if coord.standard_name == 'latitude':
            lat_coord = coord
        elif coord.standard_name == 'longitude':
            lon_coord = coord

    if lat_coord is None or lon_coord is None:
        raise ValueError(f"Cube does not have latitude/longitude coordinates")

    return lat_coord.points, lon_coord.points


def save_native_grid(lat_array, lon_array, mk_version, output_dir="data/", world_region_id=None):
    """
    Save a native UM grid to a NetCDF file.

    Saves one native grid per Mk version. The grid can be global or regional;
    regional grids are saved with the region ID in the filename for reference.

    Parameters
    ----------
    lat_array : np.ndarray
        1D or 2D latitude array.
    lon_array : np.ndarray
        1D or 2D longitude array.
    mk_version : int
        The Mk version this grid belongs to (6, 7, 8, 9, 10, 11, ...).
    output_dir : str, optional
        Directory to save the grid file (default: "data/").
    world_region_id : int, optional
        If provided, includes region ID in filename for reference.
    """
    output_dir = Path(output_dir)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        pass

    if world_region_id is not None:
        filename = output_dir / f"native_grid_Mk{mk_version}_region{world_region_id}.nc"
    else:
        filename = output_dir / f"native_grid_Mk{mk_version}.nc"

    # Create a minimal xarray dataset
    if lat_array.ndim == 1 and lon_array.ndim == 1:
        ds = xr.Dataset({
            'latitude': (['latitude'], lat_array),
            'longitude': (['longitude'], lon_array),
        })
    else:
        # 2D arrays
        ds = xr.Dataset({
            'latitude': (['y', 'x'], lat_array),
            'longitude': (['y', 'x'], lon_array),
        })

    ds.attrs['mk_version'] = mk_version
    if world_region_id is not None:
        ds.attrs['world_region_id'] = world_region_id
    ds.attrs['created_with'] = 'extract_um_met.grid.save_native_grid'

    ds.to_netcdf(str(filename))
    print(f"Saved native grid to {filename}")
    return str(filename)


def load_native_grid(mk_version, output_dir="data/"):
    """
    Load a saved native UM grid for a given Mk version.

    Parameters
    ----------
    mk_version : int
        The Mk version (6, 7, 8, 9, 10, 11, ...).
    output_dir : str, optional
        Directory containing saved grid files (default: "data/").

    Returns
    -------
    lat_array, lon_array : np.ndarray
        The loaded latitude and longitude arrays.

    Raises
    ------
    FileNotFoundError
        If no grid file is found for the Mk version.
    """
    output_dir = Path(output_dir)

    # Prefer the stitched global grid (produced by `make-native-grid`), then
    # fall back to a plain per-Mk file if that is what exists.
    candidates = [
        output_dir / f"native_grid_Mk{mk_version}_global.nc",
        output_dir / f"native_grid_Mk{mk_version}.nc",
    ]
    for filename in candidates:
        if filename.exists():
            ds = xr.open_dataset(str(filename))
            lat = ds['latitude'].values
            lon = ds['longitude'].values
            ds.close()
            return lat, lon

    raise FileNotFoundError(
        f"Native grid not found for Mk {mk_version}. Looked for: "
        + ", ".join(str(c) for c in candidates)
        + ". Run `python -m extract_um_met make-native-grid --mk "
        + f"{mk_version}` first."
    )


# Fallback grid spacing used by footprint padding, matching the NAME reference
# resolution used by the original extraction script.
_DEFAULT_DELTA_LAT = 0.234
_DEFAULT_DELTA_LON = 0.352


def build_target_grid(domain_cfg, cfg=None, mk=None):
    """
    Build a target extraction grid based on the domain's grid specification.

    Supports three grid modes:
    - `footprint` (default): uses a reference footprint file, padded by edge sizes.
    - `regular`: builds a regular 1D lat/lon grid from bounds and resolution.
    - `native`: loads the saved native UM grid (no interpolation for matching Mk).

    Parameters
    ----------
    domain_cfg : dict
        The domain configuration from config.yaml (e.g., config['domains']['SA']).
    cfg : Config or dict, optional
        The full configuration object. If None, load_config() is called.
    mk : int, optional
        The Mk version of the data being extracted. Used by native mode to pick
        the canonical native grid (defaults to this Mk when the spec says
        'majority'/'latest').

    Returns
    -------
    lat_array, lon_array : np.ndarray
        1D latitude and longitude arrays forming the target grid.
    use_interp : bool
        Whether extraction should interpolate onto this grid (True for
        footprint/regular, False for native exact matches).
    """
    if cfg is None:
        cfg = load_config()
    if isinstance(cfg, dict):
        cfg = Config(cfg)

    grid_spec = domain_cfg.get('grid', {})
    mode = grid_spec.get('mode', 'footprint')

    if mode == 'footprint':
        lat, lon = _build_footprint_grid(domain_cfg, grid_spec, cfg)
        use_interp = True

    elif mode == 'regular':
        lat, lon = _build_regular_grid(grid_spec)
        use_interp = True

    elif mode == 'native':
        lat, lon = _build_native_grid(domain_cfg, grid_spec, cfg, mk)
        use_interp = False

    else:
        raise ValueError(f"Unknown grid mode: {mode}")

    lat, lon = _clip_to_regions(lat, lon, domain_cfg, cfg)
    return lat, lon, use_interp


def _clip_to_regions(lat, lon, domain_cfg, cfg):
    """
    Drop target grid points that no world region of this domain covers.

    Extraction interpolates a region onto the whole target grid and then slices
    the result to that region's own bounds, so a point outside every region
    survives in no region file and vanishes from the joined domain without
    comment. Clipping here instead keeps the grid a run reports equal to the grid
    it writes, and says which regions are missing.

    Parameters
    ----------
    lat, lon : np.ndarray
        The 1D target grid arrays to clip.
    domain_cfg : dict
        Domain configuration, read for ``world_regions_codes`` and ``data_type``.
    cfg : Config
        Configuration object, used to resolve the source's region scheme.

    Returns
    -------
    lat, lon : np.ndarray
        The target grid restricted to the domain's regions.
    """
    from .sources import get_source

    bounds = get_source(domain_cfg.get('data_type', 'UM_Global'), cfg).region_bounds()
    if bounds is None:                    # single-file source: nothing is tiled
        return lat, lon

    regions = domain_cfg['world_regions_codes']

    lat_keep = np.zeros(lat.shape, dtype=bool)
    lon_keep = np.zeros(lon.shape, dtype=bool)
    for region_id in regions:
        r_min_lat, r_max_lat, r_min_lon, r_max_lon = bounds[region_id]
        lat_keep |= (lat >= r_min_lat) & (lat <= r_max_lat)
        if r_min_lon <= r_max_lon:
            lon_keep |= (lon >= r_min_lon) & (lon <= r_max_lon)
        else:                             # region crosses the dateline
            lon_keep |= (lon >= r_min_lon) | (lon <= r_max_lon)

    if not (lat_keep.all() and lon_keep.all()):
        _warn_clipped(lat, lon, lat_keep, lon_keep, regions, bounds)

    return lat[lat_keep], lon[lon_keep]


def _warn_clipped(lat, lon, lat_keep, lon_keep, regions, bounds):
    """Announce loudly which part of the target grid the domain's regions can't reach."""
    missing = [
        region_id
        for region_id in find_overlapping_regions(
            lat.min(), lat.max(), lon.min(), lon.max(), region_bounds=bounds
        )
        if region_id not in regions
    ]

    banner = "!" * 78
    print(banner)
    print("WARNING: the target grid extends beyond the domain's world regions.")
    for name, values, keep in (("latitude ", lat, lat_keep), ("longitude", lon, lon_keep)):
        if keep.all():
            print(f"  {name}: all {values.size} points covered")
        else:
            kept = values[keep]
            print(f"  {name}: DROPPED {int((~keep).sum())} of {values.size} points — "
                  f"{values.min():.6f}..{values.max():.6f} clipped to "
                  f"{kept.min():.6f}..{kept.max():.6f}")
    print(f"  world_regions_codes = {sorted(regions)} does not cover the requested extent.")
    if missing:
        print(f"  Add region(s) {missing} to world_regions_codes to keep the full grid,")
        print("  or reduce the edge padding so the grid stays inside the regions you have.")
    print(banner)


def _get_edge_size(domain_cfg, grid_spec, cfg, key):
    """Resolve an edge size, preferring the grid spec, then the domain, then default."""
    if key in grid_spec:
        return grid_spec[key]
    if key in domain_cfg:
        return domain_cfg[key]
    return cfg.get('default_edge_size', [100, 100])


def _build_footprint_grid(domain_cfg, grid_spec, cfg):
    """
    Build a 1D lat/lon grid from a reference footprint file.

    Reproduces the original extraction behaviour: crop the footprint to the
    bounding box of the actual measurement release locations (if present), take
    its native lat/lon, then pad outward by the configured number of edge cells
    using the NAME reference spacing.

    Parameters
    ----------
    domain_cfg : dict
        Domain configuration dict.
    grid_spec : dict
        The domain's grid specification (may hold 'footprint', 'edge_size_*').
    cfg : Config
        Configuration object.

    Returns
    -------
    lat_array, lon_array : np.ndarray
        Padded 1D latitude and longitude arrays.
    """
    footprint_filename = grid_spec.get('footprint') or domain_cfg.get('footprint')
    if not footprint_filename:
        raise ValueError("Footprint mode requires a 'footprint' key in the domain config")

    footprint_dir = resolve_config_value(
        cfg.get('reference_footprints_directory', 'data/'),
        cfg.data
    )
    footprint_path = os.path.join(footprint_dir, footprint_filename)

    if not os.path.exists(footprint_path):
        raise FileNotFoundError(f"Footprint file not found: {footprint_path}")

    fp = xr.load_dataset(footprint_path)

    # Crop to where measurements actually are, so padding grows outward from the
    # release locations rather than the footprint's full extent. That box only
    # exists for a footprint whose releases are spread over the domain, as a
    # satellite column's soundings are. A fixed site (a tower — RGL releases from
    # one coordinate at every timestep) has no box: its release point lands
    # between two cell centres, so slicing on it selects nothing and leaves an
    # empty grid. The footprint's own extent is already the region that site
    # sees, so it is kept whole.
    if 'release_lon' in fp and 'release_lat' in fp:
        release_lon_min, release_lon_max = float(fp.release_lon.min()), float(fp.release_lon.max())
        release_lat_min, release_lat_max = float(fp.release_lat.min()), float(fp.release_lat.max())
        if release_lon_min < release_lon_max and release_lat_min < release_lat_max:
            fp = fp.sel(
                lon=slice(release_lon_min, release_lon_max),
                lat=slice(release_lat_min, release_lat_max),
            )
        else:
            print(f"  footprint releases from a single location "
                  f"({release_lat_min:.4f}, {release_lon_min:.4f}); "
                  f"keeping its full extent to pad outward from")

    latitudes = list(fp.lat.values)
    longitudes = list(fp.lon.values)
    fp.close()

    edge_size_lat = _get_edge_size(domain_cfg, grid_spec, cfg, 'edge_size_lat')
    edge_size_lon = _get_edge_size(domain_cfg, grid_spec, cfg, 'edge_size_lon')

    delta_lat = grid_spec.get('dlat', _DEFAULT_DELTA_LAT)
    delta_lon = grid_spec.get('dlon', _DEFAULT_DELTA_LON)

    # Padding starts at i=1: i=0 would re-add the footprint's own min/max, leaving
    # a duplicated row and column mid-grid and one fewer padding cell per side.
    longitudes = np.array(sorted(
        longitudes
        + [np.max(longitudes) + delta_lon * i for i in range(1, edge_size_lon[1] + 1)]
        + [np.min(longitudes) - delta_lon * i for i in range(1, edge_size_lon[0] + 1)]
    ))
    latitudes = np.array(sorted(
        latitudes
        + [np.max(latitudes) + delta_lat * i for i in range(1, edge_size_lat[1] + 1)]
        + [np.min(latitudes) - delta_lat * i for i in range(1, edge_size_lat[0] + 1)]
    ))

    return latitudes, longitudes


def _bracket_bounds(values, bounds):
    """
    Subset an ascending 1-D coordinate to the points that bracket `bounds`.

    Keeping only the points strictly inside the bounds leaves the requested box
    outside the grid by up to a cell on each edge. A consumer whose own spatial
    slice clips silently then advects on edge-clamped data over that strip, so
    take the last point at or below the lower bound and the first at or above
    the upper one, putting the requested bounds inside the grid instead. A bound
    that lands exactly on a grid point adds nothing; one outside the grid
    clamps to the end of the axis.

    Parameters
    ----------
    values : np.ndarray
        A strictly ascending 1-D coordinate array.
    bounds : sequence of float
        The (lower, upper) bounds to bracket.

    Returns
    -------
    np.ndarray
        The bracketing slice of `values`.
    """
    lower, upper = bounds
    start = max(np.searchsorted(values, lower, side='right') - 1, 0)
    stop = np.searchsorted(values, upper, side='left') + 1
    return values[start:stop]


def _build_regular_grid(grid_spec):
    """
    Build a regular 1D lat/lon grid from bounds and resolution.

    The mesh runs from the lower bound to the first step at or beyond the upper
    one, so both bounds fall inside the grid — matching how native mode brackets
    its bounds.

    Parameters
    ----------
    grid_spec : dict
        Grid specification containing 'lat_bounds', 'lon_bounds', 'dlat', 'dlon'.

    Returns
    -------
    lat_array, lon_array : np.ndarray
        1D latitude and longitude arrays.
    """
    lat_bounds = grid_spec.get('lat_bounds')
    lon_bounds = grid_spec.get('lon_bounds')
    dlat = grid_spec.get('dlat')
    dlon = grid_spec.get('dlon')

    if lat_bounds is None or lon_bounds is None or dlat is None or dlon is None:
        raise ValueError(
            "Regular mode requires 'lat_bounds', 'lon_bounds', 'dlat', 'dlon' in grid spec"
        )

    lat = np.arange(lat_bounds[0], lat_bounds[1] + dlat, dlat)
    lon = np.arange(lon_bounds[0], lon_bounds[1] + dlon, dlon)

    return lat, lon


def _resolve_native_mk(grid_spec, mk):
    """Resolve which Mk's native grid to use from the spec and the data's Mk."""
    native_mk = grid_spec.get('native_grid', 'majority')
    if native_mk in ('majority', 'latest', None):
        if mk is None:
            raise ValueError(
                "Native mode needs the data's Mk version to pick a grid; pass mk=."
            )
        return mk
    if isinstance(native_mk, str):
        return int(native_mk.replace('Mk', ''))
    return int(native_mk)


def _build_native_grid(domain_cfg, grid_spec, cfg, mk):
    """
    Load a native UM grid, optionally subsetted to bounds.

    Loads the stitched global native grid for the canonical Mk and subsets it to
    the points bracketing lat_bounds/lon_bounds if specified, so the requested
    bounds fall inside the grid. No interpolation is implied.

    Parameters
    ----------
    domain_cfg : dict
        Domain configuration dict.
    grid_spec : dict
        Grid specification containing 'lat_bounds', 'lon_bounds' (optional),
        and 'native_grid' (optional; 'majority'/'latest' → the data's own Mk).
    cfg : Config
        Configuration object.
    mk : int or None
        The data's Mk version, used when the spec defers grid choice to the data.

    Returns
    -------
    lat_array, lon_array : np.ndarray
        1D native latitude and longitude arrays (subsetted if bounds given).
    """
    native_grid_dir = resolve_config_value(
        cfg.get('native_grid_directory', 'data/'),
        cfg.data
    )

    native_mk = _resolve_native_mk(grid_spec, mk)
    lat, lon = load_native_grid(native_mk, output_dir=native_grid_dir)

    # Subset if bounds are specified, keeping the grid points that bracket them
    lat_bounds = grid_spec.get('lat_bounds')
    lon_bounds = grid_spec.get('lon_bounds')

    if lat_bounds:
        lat = _bracket_bounds(lat, lat_bounds)
    if lon_bounds:
        lon = _bracket_bounds(lon, lon_bounds)

    return lat, lon


def get_mk_native_resolution(mk_version, native_grid_dir="data/"):
    """
    Get the native grid resolution for a Mk version.

    Calculates the mean latitude and longitude spacing from the saved grid.

    Parameters
    ----------
    mk_version : int
        The Mk version (6, 7, 8, 9, 10, 11, ...).
    native_grid_dir : str, optional
        Directory containing saved grid files.

    Returns
    -------
    dlat, dlon : float
        Mean latitude and longitude grid spacing.
    """
    lat, lon = load_native_grid(mk_version, output_dir=native_grid_dir)

    if lat.ndim == 1:
        dlat = np.abs(np.diff(lat)).mean()
    else:
        dlat = np.abs(np.diff(lat, axis=0)).mean()

    if lon.ndim == 1:
        dlon = np.abs(np.diff(lon)).mean()
    else:
        dlon = np.abs(np.diff(lon, axis=1)).mean()

    return dlat, dlon
