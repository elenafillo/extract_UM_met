"""
GLIDE-specific output settings.

Everything in this module exists because the GLIDE team asked for it
(``glide_stuff/um_met_extraction.md``, sections 1, 7 and 8) and is off by
default: a domain opts in with ``output_profile: glide`` and NAME's product is
unchanged.

A profile is a set of defaults, not a lock. It fills in the keys a domain has not
set, and an explicit key on the domain always wins — so a domain can take the
profile and still choose its own height ceiling. It is applied in
:meth:`extract_um_met.config.Config.get_domain`, which every command and every
per-region worker goes through, so nothing downstream needs to know a profile
exists.
"""

import numpy as np
import xarray as xr

from .metadata import FLUX_TIME_DIM


# GLIDE's requirements expressed as ordinary domain config keys. All but
# max_level_m are properties of their reader rather than of any particular run:
# max_level_m should track the run's own alt_max_m, so it is the one value a
# domain is expected to override.
GLIDE_PROFILE = {
    "output_chunks": {"time": 1, "levels": -1, "lat": 96, "lon": 96},
    "compression_level": 5,
    # Their alt_max_m is 15000 m above GROUND; max_level_m cuts on level_height,
    # which is height above SEA LEVEL once sigma -> 0 aloft. The two differ by the
    # orography, so the rule is max_level_m >= alt_max_m + max(orog) over the
    # domain — ~20600 m for EUROPE, whose box holds the Alps, the Caucasus
    # (~5.6 km) and Greenland (~3.7 km). At 15000 a column over 3 km of terrain
    # would reach only 12000 m AGL and GLIDE would constant-extrapolate above it
    # SILENTLY. On the L59 ladder this keeps 53 of 59 levels (12000 kept 42).
    "max_level_m": 20000,
    # Their section 11 row 9: the level coordinate must be contiguous from level 1
    # upward, because their hydrostatic pressure integration starts at the surface
    # — and a gapped ladder fails SILENTLY, giving wrong pressures rather than an
    # error. The ceiling above trims from the top, which is fine; what is not fine
    # is the archive-wide 'model_levels: [1, 2, 3, 4, 5]' NAME runs with, which
    # would satisfy contiguity and still be useless. Reachable here only since the
    # key moved from data_types: to the domain.
    "model_levels": "all",
    "precision": "int16",
    # air_pressure is ~30 GB per domain-year and was only ever read to back-solve
    # the orography, which extract-topog now ships as an ancillary; the MSLP is
    # not in GLIDE's variable set. Their section 2.
    "drop_vars": ["air_pressure", "air_pressure_at_sea_level"],
    # 'keep' is the lossless mode and the one the physics wants, but it is not
    # readable by GLIDE: their reader selects one scalar timestamp and packs
    # everything into a [channels, z, y, x] tensor, so a variable on a second time
    # axis is never time-selected and arrives with time_flux sitting where the
    # vertical axis is expected. 'interpolate' costs them nothing they were using
    # — they read the flux as a point value and interpolate between brackets
    # anyway, so a mean re-centred on its own timestamp is exactly the estimate
    # they want — while 'snap' leaves a 1.5 h lag in the stability regime at the
    # time of day the flux moves fastest.
    "flux_time_regrid": "interpolate",
    # The stored winds are on rho levels but labelled with the theta ladder, so
    # the store hands out the 10 m wind as the 20 m wind: +6.3% low at level 1,
    # +3.9% at level 2. Their AGL ladder puts five targets at or below 67 m and
    # its two lowest (0 and 10 m) below store level 1, so level 1's wind alone
    # sets three of the four lowest points on it.
    "wind_vertical_regrid": "interpolate",
    # 7 is the correct number for the Mk8-12 grids, whose half-cell-offset
    # longitudes are multiples of 9/128 or 15/128. The 6 every other domain keeps
    # rounds those off below |lon| = 16, where float32 resolves finer than 1e-6,
    # and GLIDE reads stores as a SET aligned on exact equality — so for them the
    # ULP is a hard failure, not the cosmetic issue it is elsewhere. A Mk6-7
    # domain would need 8.
    "coord_decimals": 7,
}

PROFILES = {"glide": GLIDE_PROFILE}


def resolve_output_profile(domain_cfg):
    """
    Fill in the defaults of a domain's ``output_profile:``, leaving its own keys.

    Returns the config unchanged when the domain names no profile. An unknown
    name raises here, at config resolution, rather than silently leaving every
    default in place — the same treatment ``output_chunks`` gets.
    """
    name = domain_cfg.get("output_profile")
    if name is None:
        return domain_cfg
    if name not in PROFILES:
        raise ValueError(
            f"domain output_profile: unknown profile {name!r}. "
            f"Available: {sorted(PROFILES)}"
        )
    return {**PROFILES[name], **domain_cfg}


# ---------------------------------------------------------------------------
# The output schema: GLIDE's own names for our fields and coordinates.
# ---------------------------------------------------------------------------

# Their names, from their section 1. Two absences, for opposite reasons:
# specific_humidity is not here because the two sides already call it that, and
# the two stresses are not here because GLIDE's variable set has no entry for
# them at all. The stresses still ship, under our CF names — friction_velocity is
# derived from them, and they are what makes that derivation auditable.
VAR_RENAMES = {
    "x_wind": "u_component_of_wind",
    "y_wind": "v_component_of_wind",
    "upward_air_velocity": "vertical_velocity",
    "air_temperature": "temperature",
    "atmosphere_boundary_layer_thickness": "boundary_layer_height",
    "surface_air_pressure": "surface_pressure",
    "surface_upward_sensible_heat_flux": "surface_sensible_heat_flux",
}

HEAT_FLUX = VAR_RENAMES["surface_upward_sensible_heat_flux"]

# levels -> level is mandatory rather than cosmetic (their section 11, row 2):
# their reader auto-corrects a level coordinate only against the candidate list
# ("hybrid", "level", "model_level"), which does not include 'levels', and their
# runtime constructs the reader with no level_name override — so a store calling
# it anything else is a hard failure at run start with no config escape.
COORD_RENAMES = {"levels": "level", "lat": "latitude", "lon": "longitude"}

# Time-indexed provenance coordinates that mean nothing to GLIDE. They are kept
# in the NAME product, which is why this is a drop here rather than upstream.
DROPPED_COORDS = ("forecast_period", "forecast_reference_time")


def level_dim(ds):
    """
    The name of ``ds``'s level dimension: ``level`` under the GLIDE schema and
    ``levels`` everywhere else, or None for a dataset with no levels at all.

    Exists because :func:`apply_glide_schema` renames the dimension a store is
    written on, so anything keyed on the name has to ask rather than assume.
    """
    for name in ("levels", "level"):
        if name in ds.dims:
            return name
    return None


def rename_chunks(chunks):
    """
    Put a resolved ``output_chunks`` mapping onto the GLIDE dimension names.

    The chunking is configured, validated and defaulted in our own names — the
    profile's ``output_chunks`` and :data:`extract_um_met.zarr_io._ZARR_CHUNKS`
    both — while the dataset it is applied to has been through
    :func:`apply_glide_schema`. Renaming the mapping alongside the dataset is
    what keeps the two in step.

    This is not cosmetic. ``zarr_io._rechunk`` reads the mapping with
    ``chunks.get(dim, -1)``, so a key that no longer matches a dimension does not
    fail — it silently means "one chunk spanning the whole dimension", and the
    96-cell tiling evaporates into 34 MB chunks with nothing on the store's face
    saying so.
    """
    return {COORD_RENAMES.get(dim, dim): size for dim, size in chunks.items()}


_HEAT_FLUX_ATTRS = {
    "long_name": "surface sensible heat flux, positive downward",
    "comment": (
        "Sign-flipped from the UM's positive-upward field to the ECMWF/ERA5 "
        "convention, positive DOWNWARD: a daytime upward flux is negative here. "
        "GLIDE's reader negates unconditionally on read. The CF standard_name is "
        "deliberately absent — the UM name says 'upward', which is no longer true "
        "of these values, and the NAME product keeps both the CF name and the "
        "original sign."
    ),
}


def _negate_heat_flux(ds):
    """
    Flip the sensible heat flux onto GLIDE's sign convention, under their name.

    Only ever applied to the renamed variable. Our objection to negating was that
    a CF ``standard_name`` saying ``upward`` on downward-positive data is exactly
    the failure it invites — under the GLIDE name there is no directional claim
    left to contradict, so the objection goes with the name.

    The quantisation range is symmetric (+-3000 W m-2), so nothing about the
    int16 encoding depends on which sign the field carries.
    """
    flux = -ds[HEAT_FLUX]
    flux.attrs = {k: v for k, v in ds[HEAT_FLUX].attrs.items() if k != "standard_name"}
    flux.attrs.update(_HEAT_FLUX_ATTRS)
    ds[HEAT_FLUX] = flux
    return ds


# The two stresses, under the names the archive gives them — they are not in
# VAR_RENAMES because GLIDE's variable set has no entry for a stress at all.
STRESS_EAST = "atmosphere_downward_eastward_stress"
STRESS_NORTH = "atmosphere_downward_northward_stress"

# GLIDE's own constants, copied from convert_um_met.py:104-107 so that u* here and
# u* in their converter are the same number rather than nearly the same one. Their
# converter is a reference copy in glide_stuff/, not something to import.
R_DRY_AIR_J_KG_K = 287.05
TV_COEFF = 0.6077


def _at_lowest_level(ds, name, level):
    """
    ``ds[name]`` on the bottom model level, without the vertical coordinates.

    Selected on the lowest ``model_level_number`` rather than on index 0, so it is
    the bottom of the atmosphere whichever way round the ladder is stored.

    ``sel`` leaves ``level`` behind as a scalar coordinate, and ``level_height``
    and ``sigma`` ride along with it. Those would collide with the store's own
    level-dimension versions of themselves the moment the result is assigned back
    into the dataset, so they go here: ``reset_coords(drop=True)`` takes exactly
    the non-dimension coordinates, which by this point in the schema are only
    those three.
    """
    return ds[name].sel({level: ds[level].min()}).reset_coords(drop=True)


def _add_friction_velocity(ds, level):
    """
    Derive ``friction_velocity`` from the two surface stresses (their section 5).

    The archive carries no u* diagnostic, so it comes from the stresses whoever
    does it; doing it here is what lets u* be quantised over its own (0, 10) range
    in :data:`QUANT_RANGES`. The alternative — shipping only the stresses and
    quantising those — has no range consistent with their section 7 table:
    inverting u* = sqrt(tau/rho) over their table's implied +-120 N m-2 puts the u*
    floor at 0.055 m s-1, above their own section 9 sanity bound of 0.03, so it
    would collapse the resolution of exactly the calm stable nights their
    turbulence scheme is most sensitive to. The stresses still ship, unquantised,
    which is what keeps this derivation auditable.

    GLIDE's formula exactly (``convert_um_met.py:415-430``): air density from the
    surface pressure and the virtual temperature of the lowest model level. The
    stresses are that same model level 1 rather than the surface (A9), so u* is
    self-consistent — a surface-layer scale evaluated at a surface-layer height —
    and the level it belongs to is recorded in its attributes, since nothing else
    in the store says which one it is.
    """
    if FLUX_TIME_DIM in ds[STRESS_EAST].dims:
        raise ValueError(
            f"the surface stresses are on the {FLUX_TIME_DIM} axis, so "
            f"friction_velocity cannot be derived: it would land on that axis too, "
            f"while the temperature and humidity it needs are on 'time'. This is "
            f"flux_time_regrid: keep, which GLIDE cannot read whatever else the "
            f"store gets right (see GLIDE_PROFILE) — the profile's default of "
            f"'interpolate' has been overridden on this domain."
        )

    virtual_t = _at_lowest_level(ds, "temperature", level) * (
        1.0 + TV_COEFF * _at_lowest_level(ds, "specific_humidity", level)
    )
    density = ds["surface_pressure"] / (R_DRY_AIR_J_KG_K * virtual_t)
    ustar = np.sqrt(np.hypot(ds[STRESS_EAST], ds[STRESS_NORTH]) / density)

    lowest = ds[level].min()
    ustar.attrs = {
        "units": "m s-1",
        "long_name": "friction velocity",
        "comment": (
            "Derived on this side with GLIDE's own formula "
            "(convert_um_met.py, derive_friction_velocity): "
            "u* = sqrt(hypot(tau_east, tau_north) / rho), with "
            f"rho = surface_pressure / (R_dry * T_v), R_dry = {R_DRY_AIR_J_KG_K} "
            f"J kg-1 K-1 and T_v = T (1 + {TV_COEFF} q) from the lowest model "
            "level. There is no u* diagnostic in the archive; the stresses it is "
            "derived from ship alongside it, unquantised. u* is a nonlinear "
            "function of two window means and is therefore not itself the mean of "
            "anything, which is why it carries no 'averaging' prose."
        ),
        # They key their own time shift off this attribute being present and zero
        # — absent reads as 'old store, unknown' — and they asked for it on every
        # variable that has it in every mode. u* takes its timestamp from the
        # stresses, so it has exactly the same offset they do.
        "averaging_midpoint_offset_hours": ds[STRESS_EAST].attrs[
            "averaging_midpoint_offset_hours"
        ],
        "derived_from": (
            f"{STRESS_EAST}, {STRESS_NORTH}, surface_pressure, temperature, "
            f"specific_humidity"
        ),
        # A9: the stresses are model level 1, not the surface, and nothing else in
        # the store says so — their own level_height and sigma are discarded by the
        # merge. Whatever height they are really at propagates into u*, so it is
        # recorded on the variable that inherits it.
        "stress_model_level_number": int(lowest),
        "stress_level_height": float(ds["level_height"].sel({level: lowest})),
        "stress_sigma": float(ds["sigma"].sel({level: lowest})),
    }
    ds["friction_velocity"] = ustar
    return ds


# Their GRAVITY_M_S2 (convert_um_met.py) and the UM's own value. GLIDE divides
# the geopotential by g on read to get back a height, so using the same constant
# makes that round trip exact rather than merely close.
GRAVITY_M_S2 = 9.80665


def _orography_on_store_grid(ds, orog):
    """
    The ancillary's orography under the store's own horizontal coordinates.

    The ancillary is written on our names (``lat``/``lon``) by ``extract-topog``,
    while by this point the store is on GLIDE's, so the rename is what lets the
    two be compared at all.

    The comparison is exact equality and a **raise**, not an xarray alignment.
    Both sides round to the domain's ``coord_decimals`` before casting to float32
    (``join.normalize_coords`` and ``topog._store_coord``), so bit-identical axes
    are the expected outcome and anything else means the two were built from
    different configs — most likely an ancillary still on the old 6-decimal
    convention (B4). Left to xarray that would merge onto a union axis and fill
    the difference with NaN, and a non-finite geopotential is what GLIDE rejects
    as "store is incomplete or corrupted" — after the run, on their side, rather
    than here.
    """
    orog = orog.rename({"lat": "latitude", "lon": "longitude"})
    for axis in ("latitude", "longitude"):
        theirs, ours = orog[axis].values, ds[axis].values
        if np.array_equal(theirs, ours):
            continue
        if theirs.shape == ours.shape:
            i = int(np.argmax(theirs != ours))
            detail = f"same length, first difference at index {i}: {theirs[i]!r} vs {ours[i]!r}"
        else:
            detail = f"{theirs.size} values against {ours.size}"
        raise ValueError(
            f"the orography ancillary's {axis} does not match the store's — "
            f"{detail}. It was written with "
            f"coord_decimals={orog.attrs.get('coord_decimals')} "
            f"({orog.attrs.get('ancillary_file')}). The geopotentials are built from "
            f"it cell by cell, so the two grids must be identical rather than merely "
            f"overlapping; regenerate the ancillary from the config this store is "
            f"being built with."
        )
    return orog


def _add_geopotentials(ds, orog, level):
    """
    Write ``geopotential`` and ``geopotential_at_surface`` (their section 2).

    Both are in GLIDE's ``_DERIVATION_KEYS`` and are required unconditionally,
    whatever channels a run asks for, because every height in the model comes from
    them: a missing one raises at ``_select_variables`` and their runtime builds
    the reader with no ``variable_map`` override, so there is no escape on their
    side. It is the difference between a store that opens and one that does not.

    The UM's vertical coordinate is terrain-following, so the height of a model
    level is ``z = level_height + sigma * orog`` — the same arithmetic as
    ``convert_um_met.build_geopotential``, which back-solved it from
    ``air_pressure``. That is now a validation path rather than a live one: the
    profile stops shipping ``air_pressure`` (C2), which is what makes writing
    these our job instead of theirs.

    Three things about the result are load-bearing, all from their section 2:

    - **Geopotential, not geopotential height.** They divide by g on read.
    - **No time dimension.** ``(level, latitude, longitude)`` and
      ``(latitude, longitude)``; the fields are static, and a static variable is
      written when the store is created and skipped by later appends.
    - **Never quantised.** See the note in :data:`QUANT_RANGES`: near-surface
      layer thicknesses are formed by differencing two of these, so int16 error
      would enter twice on a 20 m layer.

    Negative heights above ground are expected wherever a level runs into terrain
    and are left alone — their terrain-following resample excludes them properly
    and clamping corrupts it.
    """
    orog = _orography_on_store_grid(ds, orog)
    provenance = (
        f"z = level_height + sigma * surface_altitude, from "
        f"{orog.attrs.get('ancillary_file')} "
        f"(Mk{orog.attrs.get('mk_version')}), times g = {GRAVITY_M_S2} m s-2 — "
        f"GLIDE's own GRAVITY_M_S2, so their divide-by-g on read is exact. Static: "
        f"no time dimension. float32 and deliberately never quantised, since layer "
        f"thicknesses are formed by differencing these."
    )

    height = ds["level_height"] + ds["sigma"] * orog
    geopotential = (GRAVITY_M_S2 * height).astype(np.float32).transpose(
        level, "latitude", "longitude"
    )
    geopotential.attrs = {
        "units": "m2 s-2",
        "standard_name": "geopotential",
        "long_name": "geopotential on model levels",
        "comment": (
            f"Geopotential of the model levels over the domain's orography. "
            f"{provenance} Heights below ground occur where a level runs into "
            f"terrain and are not clamped."
        ),
    }

    surface = (GRAVITY_M_S2 * orog).astype(np.float32)
    surface.attrs = {
        "units": "m2 s-2",
        "standard_name": "surface_geopotential",
        "long_name": "geopotential at the surface",
        "comment": f"Surface geopotential, g * surface_altitude. {provenance}",
    }

    ds["geopotential"] = geopotential
    ds["geopotential_at_surface"] = surface
    return ds


def _demote_level_ladder(ds):
    """
    Move ``level_height``/``sigma`` off the coordinates and onto store attributes.

    Their section 5 draws the line at exactly this: extra *coordinates* are a
    problem, extra *attributes* are not. It was written about the rho wind ladder,
    which is why that one is already carried as
    ``wind_level_height_rho``/``wind_sigma_rho`` (see
    :func:`extract_um_met.staggering.rho_ladder_attrs`) — but the theta ladder
    these two hold is an extra coordinate on the same ``level`` dimension and sits
    on the same side of that line. Their ``met_schema.md`` also says a conforming
    store holds exactly the eleven variables plus coordinates, and these are two
    coordinates it does not name.

    Nothing is lost by the move. They are static, one value per level, and the
    only thing in the store that consumed them is
    :func:`_add_geopotentials`, which runs before this and has already turned them
    into the geopotential field GLIDE actually reads heights from. Their heights
    are metres, index-for-index with ``level``, matching the rho pair.

    Dropping the coordinates also drops the ``coordinates: "level_height sigma"``
    attribute xarray writes onto every levelled variable, which would otherwise be
    left pointing at variables the store no longer has.
    """
    ds.attrs["level_height"] = [float(v) for v in ds["level_height"].values]
    ds.attrs["sigma"] = [float(v) for v in ds["sigma"].values]
    return ds.drop_vars(["level_height", "sigma"])


def _check_units(ds):
    """
    Abort if any variable reaches the store without a non-empty ``units``.

    Their section 11 row 5 makes this a hard requirement on every variable they
    read. A rename carries attributes through untouched, so the fields extracted
    from iris satisfy it on their own; the derived variables are the exposure,
    since they are born with whatever attributes they are given here.
    """
    missing = sorted(
        name for name, var in ds.data_vars.items()
        if not str(var.attrs.get("units", "")).strip()
    )
    if missing:
        raise ValueError(
            f"variables reaching a glide store without a units attribute: {missing}. "
            f"GLIDE rejects a store whose variables carry no units (their section 11)."
        )


def apply_glide_schema(ds, orography):
    """
    Put a joined period onto GLIDE's variable and coordinate names.

    Applied last, after :func:`extract_um_met.metadata.apply_cf_metadata`, so
    everything upstream keeps working in our own names and only the store itself
    changes. What that leaves downstream is the mapping in :func:`rename_chunks`
    and the level dimension in :func:`level_dim`; :data:`QUANT_RANGES` is keyed on
    GLIDE's names for the same reason, since int16 is only ever reached under this
    profile.

    The renames themselves are cheap; six things travel with them:

    - the sensible heat flux is negated (:func:`_negate_heat_flux`);
    - ``friction_velocity`` is derived (:func:`_add_friction_velocity`), after the
      renames so it is born under the name it keeps and reads the fields it needs
      under theirs;
    - the two geopotentials are written (:func:`_add_geopotentials`), which is why
      ``orography`` is a required argument rather than an optional one — a glide
      store without them does not open;
    - ``level_height``/``sigma`` are demoted to store attributes
      (:func:`_demote_level_ladder`), last of the three, since the geopotentials
      are built from them;
    - ``glide_vertical_coordinate`` is stamped on the store, without which their
      section 1 rejects it at open;
    - ``forecast_period``/``forecast_reference_time`` are dropped.

    ``orography`` is passed in rather than read here, from
    :func:`extract_um_met.topog.load_orography`: the caller is where the domain
    and the year are already known, and it keeps this module a function of what it
    is handed, which is what lets the whole schema be exercised on a fixture with
    no archive and no ancillary on disk.

    Attributes that stay true are kept: ``standard_name: x_wind`` on a variable
    named ``u_component_of_wind`` still describes the field correctly, and only
    the heat flux's becomes false.
    """
    ds = ds.drop_vars([name for name in DROPPED_COORDS if name in ds.variables])
    ds = ds.rename({**VAR_RENAMES, **COORD_RENAMES})
    ds = _negate_heat_flux(ds)
    level = level_dim(ds)
    ds = _add_friction_velocity(ds, level)
    ds = _add_geopotentials(ds, orography, level)
    ds = _demote_level_ladder(ds)
    # Their section 1 reads this before anything else and rejects a store that
    # does not carry it, whatever else the store gets right.
    ds.attrs["glide_vertical_coordinate"] = "model_level"
    _check_units(ds)
    return ds


# The physical range each variable is quantised over, from their section 7 table,
# keyed on GLIDE's names — which is what the variables are called by the time an
# encoding is built, and unambiguous because int16 is only accepted under this
# profile (see zarr_io.resolve_precision). Ours are alongside. Anything absent
# stays float32.
#
# The two stresses are deliberately absent: their table has no stress entry,
# because they derive u* from the stresses and quantise that instead. Inverting
# their own formula for a stress range (tau = rho u*^2 -> +-120 N m-2 at
# rho ~ 1.225) puts the u* floor at 0.055 m s-1, above their own sanity lower
# bound of 0.03 — so a stress range consistent with their table is worse than not
# quantising at all. They are 2D surface fields, so float32 costs almost nothing.
#
# Neither geopotential may ever be added here, however tempting the 3-D one's
# size makes it (their section 2): near-surface layer thicknesses are formed by
# differencing them, so quantisation error would enter twice on a 20 m layer.
# They are absent by construction rather than by oversight, unlike the stresses
# above, which are absent for the different reason given there.
QUANT_RANGES = {
    "u_component_of_wind": (-200.0, 200.0),        # x_wind
    "v_component_of_wind": (-200.0, 200.0),        # y_wind
    "vertical_velocity": (-50.0, 50.0),            # upward_air_velocity
    "temperature": (150.0, 350.0),                 # air_temperature
    "specific_humidity": (0.0, 0.06),              # specific_humidity
    "surface_pressure": (30000.0, 110000.0),       # surface_air_pressure
    "boundary_layer_height": (0.0, 15000.0),       # atmosphere_boundary_layer_thickness
    # Their range is symmetric, so it holds under either sign convention and
    # nothing here depends on the negation _negate_heat_flux applies.
    HEAT_FLUX: (-3000.0, 3000.0),                  # surface_upward_sensible_heat_flux
    "friction_velocity": (0.0, 10.0),              # derived — _add_friction_velocity
}


def quantisation_encoding(name):
    """
    CF scale/offset encoding mapping ``name``'s physical range onto int16.

    65534 rather than 65535 steps, matching their converter, which is what
    reproduces the resolutions their section 7 quotes.

    Deliberately no ``_FillValue``: the store has no missing data (finiteness is
    checked by :func:`check_quantisation_range` before every write), and a fill
    value would make any datum landing on the sentinel decode as NaN.
    ``specific_humidity`` is legitimately 0.0 in the stratosphere, which sits one
    count off it.
    """
    low, high = QUANT_RANGES[name]
    return {
        "dtype": "int16",
        "scale_factor": np.float32((high - low) / 65534.0),
        "add_offset": np.float32(0.5 * (low + high)),
    }


def check_quantisation_range(ds):
    """
    Abort if a variable would not survive the int16 encoding it is written with.

    Two ways it would not. **Out of range**: values outside the quantisation
    range *wrap* rather than clip under CF scale/offset, so a single unexpected
    wind speed silently becomes a large negative one. **Non-finite**: with no
    ``_FillValue`` a NaN has nowhere to go and decodes as an ordinary number.

    Runs on every write, not only the store's first. The encoding is fixed when
    the store is created, but each appended period is encoded through it, so a
    later month drifting out of range would wrap with nothing to catch it. Costs
    one pass over the period being written — it is on disk in scratch already,
    and the reductions for every variable are computed in a single pass.
    """
    names = [name for name in QUANT_RANGES if name in ds.data_vars]
    reductions = {}
    for name in names:
        # skipna=False so a NaN anywhere propagates into both ends and is caught
        # below; the default would quietly skip exactly what we are looking for.
        reductions[f"{name}_min"] = ds[name].min(skipna=False)
        reductions[f"{name}_max"] = ds[name].max(skipna=False)
    spans = xr.Dataset(reductions).compute()

    for name in names:
        low, high = QUANT_RANGES[name]
        v_min = float(spans[f"{name}_min"])
        v_max = float(spans[f"{name}_max"])
        if not (np.isfinite(v_min) and np.isfinite(v_max)):
            raise ValueError(
                f"{name} contains non-finite values (min={v_min}, max={v_max}). "
                f"An int16 store carries no _FillValue — see quantisation_encoding "
                f"— so a NaN has nowhere to go and would decode as a real value. "
                f"Write this domain with precision: float32, or find the NaNs."
            )
        if v_min < low or v_max > high:
            raise ValueError(
                f"{name} spans [{v_min:.6g}, {v_max:.6g}], outside its int16 "
                f"quantisation range [{low:g}, {high:g}]. Values outside the range "
                f"wrap rather than clip, so writing this would corrupt the store "
                f"silently. Widen QUANT_RANGES or use precision: float32."
            )
