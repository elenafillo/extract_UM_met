"""
Utilities for working with UM world regions and domain grids.

Functions for finding overlapping regions, building domain-specific grids,
and managing region bounds and deduplications.
"""

import numpy as np
from .config import load_config


def get_saved_region_bounds():
    """
    Return the saved latitude/longitude bounds for each world region.

    The returned mapping is used by the extraction and join scripts to work
    out which global region files overlap a given domain. The bounds reflect
    the saved region layout used elsewhere in the repository, including the
    dateline-crossing regions.

    Returns
    -------
    dict
        Mapping of region ID to [min_lat, max_lat, min_lon, max_lon].
    """
    region_bounds = {
        1: [79.921875, 89.953125, 0.0703125, -0.0703125],
        2: [24.984375, 80.015625, -45.070312, 45.070312],
        3: [24.984375, 80.015625, 44.929688, 135.07031],
        4: [24.984375, 80.015625, 134.92969, -134.92969],
        5: [24.984375, 80.015625, -135.07031, -44.929688],
        6: [-25.078125, 25.078125, -45.070312, 45.070312],
        7: [-25.078125, 25.078125, 44.929688, 135.07031],
        8: [-25.078125, 25.078125, 134.92969, -134.92969],
        9: [-25.078125, 25.078125, -135.07031, -44.929688],
        10: [-80.015625, -24.984375, -45.070312, 45.070312],
        11: [-80.015625, -24.984375, 44.929688, 135.07031],
        12: [-80.015625, -24.984375, 134.92969, -134.92969],
        13: [-80.015625, -24.984375, -135.07031, -44.929688],
        14: [-89.953125, -79.921875, 0.0703125, -0.0703125],
    }
    return region_bounds


def find_overlapping_regions(min_lat, max_lat, min_lon, max_lon, region_bounds=None):
    """
    Find the world-region IDs that overlap a latitude/longitude box.

    This is a convenience wrapper around the saved region bounds. It checks
    each world region and returns the IDs whose latitude and longitude ranges
    intersect the supplied bounding box.

    Parameters
    ----------
    min_lat, max_lat : float
        Latitude limits of the box to test.
    min_lon, max_lon : float
        Longitude limits of the box to test.
    region_bounds : dict, optional
        Region layout to test against, as returned by
        :func:`get_saved_region_bounds`. Defaults to the saved world-14 bounds;
        pass a source's own bounds to test a different tiling.

    Returns
    -------
    list of int
        Region IDs that intersect the supplied bounding box.
    """
    if region_bounds is None:
        region_bounds = get_saved_region_bounds()
    overlapping_regions = []

    for region_id, (r_min_lat, r_max_lat, r_min_lon, r_max_lon) in region_bounds.items():
        # Check if the bounding boxes overlap
        lat_overlap = not (max_lat < r_min_lat or min_lat > r_max_lat)
        lon_overlap = not (max_lon < r_min_lon or min_lon > r_max_lon)

        if lat_overlap and lon_overlap:
            overlapping_regions.append(region_id)

    return overlapping_regions


def build_domain_grid(global_grid, regions_to_include):
    """
    Trim a global region grid down to just the regions needed for one domain.

    Cells that are not part of the requested domain are replaced with None.
    Entire rows and columns that become empty after filtering are removed so the
    result is the smallest rectangular grid that still preserves the target
    layout.

    Parameters
    ----------
    global_grid : list of list of int
        Full region grid used by the domain-joining logic.
    regions_to_include : list of int
        Region IDs that should remain in the returned grid.

    Returns
    -------
    list of list
        Trimmed grid containing only the requested regions.

    Examples
    --------
    >>> build_domain_grid([[2, 3], [6, 7]], [3, 7])
    [[None, 3], [None, 7]]
    """
    # Create a mask for the regions to include
    trimmed_grid = []
    for row in global_grid:
        new_row = [cell if cell in regions_to_include else None for cell in row]
        if any(cell is not None for cell in new_row):  # Keep rows with at least one valid region
            trimmed_grid.append(new_row)

    # Now trim columns (transpose → filter → transpose back)
    transposed = list(map(list, zip(*trimmed_grid)))
    trimmed_transposed = [
        col for col in transposed if any(cell is not None for cell in col)
    ]
    final_grid = list(map(list, zip(*trimmed_transposed)))  # Back to row-major

    return final_grid


def drop_duplicate_coords(ds, dim):
    """
    Drop duplicate coordinate values along a given dimension.

    This helper keeps the first occurrence of each coordinate value and drops
    later duplicates. It is used to clean up stitched datasets where floating-
    point precision or concatenation can create repeated latitude or longitude
    coordinate entries.

    Parameters
    ----------
    ds : xarray.Dataset
        Dataset to clean.
    dim : str
        Coordinate dimension to deduplicate.

    Returns
    -------
    xarray.Dataset
        Dataset with duplicate coordinate values removed along `dim`.
    """
    coord_vals = ds[dim].values
    _, unique_idx = np.unique(coord_vals, return_index=True)
    if len(unique_idx) < len(coord_vals):
        print(f"Dropping {len(coord_vals) - len(unique_idx)} duplicate values in '{dim}'")
    return ds.isel({dim: sorted(unique_idx)})


def get_edge_size(domain, size_type):
    """
    Return the configured edge size for a domain, or the default fallback.

    The config file can define per-domain values for `edge_size_lat` and
    `edge_size_lon`. If a domain does not define the requested size type, the
    global `default_edge_size` value is returned instead.

    Parameters
    ----------
    domain : str
        Domain key from config.yaml, for example ``SA`` or ``INDIA``.
    size_type : str
        Which size setting to fetch, typically ``edge_size_lat`` or
        ``edge_size_lon``.

    Returns
    -------
    list
        Two-element list describing the requested edge size.
    """
    config = load_config()
    default_edge_size = config.get('default_edge_size', [100, 100])

    try:
        return config['domains'][domain].get(size_type, default_edge_size)
    except KeyError:
        return default_edge_size
