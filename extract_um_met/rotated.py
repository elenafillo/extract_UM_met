"""
Rotated-pole grid handling for LimitedArea sources (UM1p5km, NZCSM).

These sources store data on a rotated-pole grid (``grid_latitude`` /
``grid_longitude`` + a rotated north pole). To produce a regular lat/lon output
we regrid the fields onto a true lat/lon target with iris, and — because wind (and
stress) *vectors* are grid-relative — rotate them to true north first.

Validated against a NZCSM sample: ``cube.regrid(target, Linear())`` handles
rotated→regular directly (fast, full coverage), and
``iris.analysis.cartography.rotate_winds`` keeps the rotated dim coords so the
rotated vectors can then be regridded.

The rotated-pole parameters (pole lat/lon, native spacing) should be recorded in
the output metadata so the regridding provenance is preserved.
"""

import numpy as np

import iris
import iris.analysis
import iris.coords
import iris.cube
from iris.analysis import Linear
from iris.analysis.cartography import rotate_winds
from iris.coord_systems import GeogCS


# UM spherical earth radius (metres); matches the model's coordinate system.
UM_EARTH_RADIUS = 6371229.0


def latlon_target_cube(lat, lon, earth_radius=UM_EARTH_RADIUS):
    """
    Build an empty iris cube on a regular lat/lon grid to regrid onto.

    Parameters
    ----------
    lat, lon : array-like
        1D target latitudes/longitudes (degrees). For dateline-crossing domains
        (e.g. NZCSM) express longitude in 0–360 to avoid a break.
    earth_radius : float
        Sphere radius for the target GeogCS (default: UM value).

    Returns
    -------
    iris.cube.Cube
        A (lat, lon) cube usable as the target of ``cube.regrid(...)``.
    """
    gcs = GeogCS(earth_radius)
    latc = iris.coords.DimCoord(
        np.asarray(lat, "f8"), standard_name="latitude", units="degrees", coord_system=gcs
    )
    lonc = iris.coords.DimCoord(
        np.asarray(lon, "f8"), standard_name="longitude", units="degrees", coord_system=gcs
    )
    return iris.cube.Cube(
        np.zeros((len(latc.points), len(lonc.points)), "f4"),
        dim_coords_and_dims=[(latc, 0), (lonc, 1)],
    )


def latlon_regridder(src_cube, target_cube, scheme=None):
    """
    Build a reusable regridder from ``src_cube``'s grid onto a regular lat/lon target.

    ``cube.regrid(target, scheme)`` recomputes the source→target mapping on every
    call. One extract regrids ~10 fields that all sit on the same (rotated) mass
    grid, so the mapping is built once here and applied per cube instead. iris
    projects between the source and target coordinate systems, so this works
    directly for a rotated-pole source and a regular lat/lon target.

    The regridder is valid for any cube on ``src_cube``'s horizontal grid — leading
    level/time dims may differ — and iris raises if a cube's grid does not match,
    so a field on an unexpected grid fails loudly rather than being mis-mapped.

    Parameters
    ----------
    src_cube : iris.cube.Cube
        A cube on the source horizontal grid (e.g. a mass-grid field).
    target_cube : iris.cube.Cube
        Target grid from :func:`latlon_target_cube`.
    scheme : iris regridding scheme, optional
        Defaults to ``iris.analysis.Linear(extrapolation_mode="mask")``: data points
        outside native grid coverage are masked rather than extrapolated.

    Returns
    -------
    callable
        ``regridder(cube)`` → cube on the target lat/lon grid.
    """
    return (scheme or Linear(extrapolation_mode="mask")).regridder(src_cube, target_cube)


def rotate_winds_true_north(u_cube, v_cube, mass_cube, earth_radius=UM_EARTH_RADIUS):
    """
    Co-locate a (u, v) vector pair on the mass grid and rotate it to true north.

    On the UM C-grid ``u`` and ``v`` are offset from each other and from the mass
    points, so they are first regridded onto ``mass_cube``'s (rotated) grid, then
    ``rotate_winds`` turns the grid-relative components into true
    eastward/northward. The result stays on the rotated grid (keeps
    ``grid_latitude``/``grid_longitude``), ready to be regridded to lat/lon with
    :func:`latlon_regridder`.

    Parameters
    ----------
    u_cube, v_cube : iris.cube.Cube
        Grid-relative vector components (e.g. x_wind/y_wind or the stress pair),
        each possibly staggered and with a leading level dimension.
    mass_cube : iris.cube.Cube
        A mass-grid cube (e.g. air_pressure) whose horizontal grid the vectors are
        co-located onto. Should already be level-matched to u/v.
    earth_radius : float
        Sphere radius for the true-north GeogCS.

    Returns
    -------
    (iris.cube.Cube, iris.cube.Cube)
        True-eastward and true-northward components, on the rotated mass grid.
    """
    u = u_cube.regrid(mass_cube, Linear())
    v = v_cube.regrid(mass_cube, Linear())
    return rotate_winds(u, v, GeogCS(earth_radius))


def rotated_pole_attrs(cube):
    """
    Extract rotated-pole provenance from a cube's coordinate system.

    Returns a dict of attributes to stamp on the regridded output so the native
    grid is documented (empty if the cube is not on a rotated pole).
    """
    cs = cube.coord_system()
    if not isinstance(cs, iris.coord_systems.RotatedGeogCS):
        return {}
    return {
        "native_grid": "rotated_pole",
        "native_grid_north_pole_latitude": float(cs.grid_north_pole_latitude),
        "native_grid_north_pole_longitude": float(cs.grid_north_pole_longitude),
        "regridding": (
            "regridded from the native rotated-pole grid to a regular lat/lon grid "
            "(bilinear, edge points outside native coverage masked); "
            "wind/stress vectors rotated to true north beforehand"
        ),
    }
