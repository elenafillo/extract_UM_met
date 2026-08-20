"""
Vertical de-staggering of the winds onto the theta (mass) levels.

The UM is staggered Charney-Phillips in the vertical: u and v live on *rho*
levels, which sit at the arithmetic midpoints of the *theta* levels carrying
temperature, humidity, vertical velocity and pressure. Extraction moves the
winds onto the mass grid horizontally only, so a store's ``level_height``
describes the theta ladder while the winds underneath it are still on rho — the
10 m wind labelled as the 20 m wind, a systematic +6.3% low bias at level 1.

This module moves them, with a 2-point weighted mean of the bracketing rho
levels::

    w[j]       = (lh_rho[j+1] - lh_theta[j]) / (lh_rho[j+1] - lh_rho[j])
    u_theta[j] = w[j]*u_rho[j] + (1 - w[j])*u_rho[j+1]

The weights are a static 1-D vector — **no terrain and no time dependence**.
That holds because the rho levels are exact arithmetic midpoints of the theta
levels in *both* ``level_height`` and ``sigma`` (sigma_rho(2) = 0.9958282
against a theta mean of 0.9958286), so the terrain-following geometric height
``z = level_height + sigma*orog`` is a midpoint for any orography whatsoever.
On the L59 ladder w[1] = 0.625 and w[2] = 0.583. They are derived from the
cubes' own ``level_height`` coordinates rather than hardcoded, so the same code
is correct across Mk6-12 and any other ladder.

**The top model level is dropped.** rho 59 sits *below* theta 59, so the topmost
theta level has no bracketing rho level above it. Rather than hold the top rho
value constant — which would leave one level quietly un-de-staggered inside an
otherwise corrected store — the level comes off the returned selection, and so
off *every* variable rather than just the winds, or the store goes ragged.

**This must run before the level subsample.** Theta level *k* is built from rho
*k* and *k+1*, so with ``level_stride: 3`` the neighbour it needs has already
been discarded. Ordering against a ``max_level_m`` ceiling is free, since both
that and the trim here are independent filters over the same level list.

**Scope: the UM global archive only.** The limited-area sources (NZCSM) stay
staggered for now; they are read by ``extract.extract_single``, which does not
call this module.

Deliberately untouched: ``averaged_staggered_vars``, the surface stresses. Those
are single-level fields, staggered horizontally only — there is no vertical
ladder to move them along, and C5 derives u* from them as they are.
"""

import iris
import numpy as np

from .iris_io import _pick, _theta_ladder_cube


def rho_ladder(cube, source):
    """
    The winds' native rho ladder, as ``{model_level_number: (level_height, sigma)}``.

    Must be read *before* :func:`destagger_winds_onto_theta`, which replaces the
    wind cubes' vertical coordinates with the theta ladder. Keyed by level number
    rather than returned as arrays because the store's final level selection is
    only known later — after any ``max_level_m`` ceiling has resolved — and the
    recorded ladder has to line up with it index for index.

    Parameters
    ----------
    cube : iris.cube.CubeList
        Cubes as loaded, winds included.
    source : extract_um_met.sources.MetSource
        Describes which cubes are the winds.

    Returns
    -------
    dict
        ``{int level number: (level_height in m, sigma)}`` for every level the
        winds were loaded on. Empty for a source configured without winds —
        this runs on every extraction, in both modes, so it must not be the
        thing that breaks a wind-free variable set.
    """
    if not source.wind_vars:
        return {}
    wind = _pick(cube, source.wind_vars[0][0], True)
    return {
        int(n): (float(h), float(s))
        for n, h, s in zip(
            wind.coord("model_level_number").points,
            _ladder(wind, "level_height"),
            _ladder(wind, "sigma"),
        )
    }


def rho_ladder_attrs(ladder, levels):
    """
    The ``wind_level_height_rho`` / ``wind_sigma_rho`` store attributes.

    Written in **both** modes. Under ``snap`` they say where u/v actually are,
    which is the thing the store was previously silent about; under
    ``interpolate`` they say where u/v came from, which together with the
    weights makes the operation auditable and invertible. Plain lists of floats,
    so they are JSON-serialisable and the store schema is unchanged — GLIDE's
    one-``level``-coordinate rule is not in play.

    Parameters
    ----------
    ladder : dict
        From :func:`rho_ladder`, read before any de-staggering.
    levels : list of int
        The store's final level selection, after the ceiling and the de-stagger
        trim.

    Returns
    -------
    dict
        The two attributes, index-for-index with ``levels``; empty if the source
        has no winds to describe.
    """
    if not ladder:
        return {}
    return {
        "wind_level_height_rho": [ladder[lvl][0] for lvl in levels],
        "wind_sigma_rho": [ladder[lvl][1] for lvl in levels],
    }


def destagger_winds_onto_theta(cube, source, levels):
    """
    Interpolate u/v onto the theta (mass) levels.

    Parameters
    ----------
    cube : iris.cube.CubeList
        Cubes as loaded, on the full ladder — *not* level-subsampled yet.
    source : extract_um_met.sources.MetSource
        Describes which cubes are the winds and which the mass fields.
    levels : list of int
        The level selection resolved so far.

    Returns
    -------
    (iris.cube.CubeList, list of int)
        The cubes with the wind entries replaced, and ``levels`` with any level
        the de-stagger cannot produce removed. Both are returned so a caller
        cannot subsample on a level list the winds no longer have.

    Notes
    -----
    The trim drops level numbers, not list positions. Only the ladder's topmost
    level disappears, so ``model_levels: "all"`` goes 59 -> 58 while
    ``level_stride: 3`` (topping out at 57) is left untouched. Taking the last
    entry off ``levels`` instead would silently drop level 57 from a strided
    store.
    """
    theta = _theta_ladder_cube(cube, source)
    numbers = [int(n) for n in theta.coord("model_level_number").points]
    kept = set(numbers[:-1])

    # Keyed on identity: iris cube equality compares data, and the CubeList has
    # to come back in its original order for _pick to keep working downstream.
    destaggered = {}
    for name, _ in source.wind_vars:
        wind = _pick(cube, name, True)
        destaggered[id(wind)] = _onto_theta(wind, theta)

    return (
        iris.cube.CubeList([destaggered.get(id(c), c) for c in cube]),
        [lvl for lvl in levels if lvl in kept],
    )


def _onto_theta(wind, theta):
    """One wind cube moved onto the theta levels, one level shorter than it was."""
    axis = wind.coord_dims("model_level_number")[0]
    lh_rho = _ladder(wind, "level_height")
    lh_theta = _ladder(theta, "level_height")
    _check_ladders(wind, theta, lh_rho, lh_theta)

    lower = tuple(slice(None, -1) if d == axis else slice(None) for d in range(wind.ndim))
    upper = tuple(slice(1, None) if d == axis else slice(None) for d in range(wind.ndim))
    weights = (lh_rho[1:] - lh_theta[:-1]) / (lh_rho[1:] - lh_rho[:-1])
    # Shaped to broadcast along the level axis, and cast so that a float64
    # ladder cannot silently upcast a float32 field and double the store.
    w = weights.reshape([-1 if d == axis else 1 for d in range(wind.ndim)]).astype(wind.dtype)

    # core_data() rather than data: slicing and arithmetic dispatch to dask when
    # the cube is lazy, so nothing is realised until the caller loads.
    data = wind.core_data()
    out = wind[lower].copy(data=w * data[lower] + (1 - w) * data[upper])

    # The winds are on the theta ladder now, so they must describe it. Without
    # this they would still advertise rho heights, and the combine downstream
    # would be reconciling two ladders again — which is the bug in the first
    # place.
    for name in ("level_height", "sigma"):
        out.replace_coord(_trimmed(theta, name))
    return out


def _check_ladders(wind, theta, lh_rho, lh_theta):
    """
    Confirm the two ladders can be paired, and that pairing them interpolates.

    Both raise rather than warn: a de-stagger against a mismatched or
    non-bracketing ladder produces a plausible-looking wind field that is
    quietly wrong, which is exactly the failure this whole option exists to fix.
    """
    rho_numbers = wind.coord("model_level_number").points
    theta_numbers = theta.coord("model_level_number").points
    if not np.array_equal(rho_numbers, theta_numbers):
        raise ValueError(
            f"cannot de-stagger {wind.name()}: it is on model levels "
            f"{rho_numbers.tolist()} but the mass field {theta.name()} is on "
            f"{theta_numbers.tolist()}. The weights pair the two ladders index "
            f"for index, so they must cover the same levels."
        )

    bracketed = (lh_rho[:-1] < lh_theta[:-1]) & (lh_theta[:-1] < lh_rho[1:])
    if not bracketed.all():
        bad = [int(theta_numbers[j]) for j in np.flatnonzero(~bracketed)]
        raise ValueError(
            f"cannot de-stagger {wind.name()}: every theta level must lie "
            f"between its bracketing rho levels, but model level(s) {bad} do "
            f"not, so this would extrapolate rather than interpolate. The "
            f"source is not Charney-Phillips staggered, or a rho-level cube has "
            f"been mistaken for a mass one.\n"
            f"  rho   ({wind.name()}):  {np.round(lh_rho, 3).tolist()}\n"
            f"  theta ({theta.name()}): {np.round(lh_theta, 3).tolist()}"
        )


def _ladder(cube, name):
    """
    The 1-D vertical ladder ``name`` from ``cube``.

    iris occasionally leaves ``level_height``/``sigma`` broadcast over a second
    dimension, when one file in the period disagrees in the last float32 bit
    (7700.039 against 7700.040 for 2012-03-15 03:00 — see
    ``extract._collapse_redundant_coord_dims``, which collapses the same
    coordinates downstream for the same reason). That spread is ~1e-3 m against
    level spacings of tens of metres, far below anything the weights resolve, so
    the first slice is taken here too rather than failing a whole month over it.
    """
    coord = cube.coord(name)
    level_dim = cube.coord_dims("model_level_number")[0]
    index = tuple(slice(None) if d == level_dim else 0 for d in cube.coord_dims(coord))
    return coord.points[index]


def _trimmed(theta, name):
    """``name`` from the theta cube, trimmed to the levels the de-stagger keeps."""
    coord = theta.coord(name)
    level_dim = theta.coord_dims("model_level_number")[0]
    index = tuple(
        slice(None, -1) if d == level_dim else slice(None)
        for d in theta.coord_dims(coord)
    )
    return coord[index]
