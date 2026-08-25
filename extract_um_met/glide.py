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


def apply_glide_schema(ds):
    """
    Put a joined period onto GLIDE's variable and coordinate names.

    Applied last, after :func:`extract_um_met.metadata.apply_cf_metadata`, so
    everything upstream keeps working in our own names and only the store itself
    changes. What that leaves downstream is the mapping in :func:`rename_chunks`
    and the level dimension in :func:`level_dim`; :data:`QUANT_RANGES` is keyed on
    GLIDE's names for the same reason, since int16 is only ever reached under this
    profile.

    The renames themselves are cheap; three things travel with them:

    - the sensible heat flux is negated (:func:`_negate_heat_flux`);
    - ``glide_vertical_coordinate`` is stamped on the store, without which their
      section 1 rejects it at open;
    - ``forecast_period``/``forecast_reference_time`` are dropped.

    Attributes that stay true are kept: ``standard_name: x_wind`` on a variable
    named ``u_component_of_wind`` still describes the field correctly, and only
    the heat flux's becomes false.
    """
    ds = ds.drop_vars([name for name in DROPPED_COORDS if name in ds.variables])
    ds = ds.rename({**VAR_RENAMES, **COORD_RENAMES})
    ds = _negate_heat_flux(ds)
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
    "friction_velocity": (0.0, 10.0),              # derived — see C5
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
