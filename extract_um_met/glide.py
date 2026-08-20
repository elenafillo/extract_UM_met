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
    "max_level_m": 12000,
    "precision": "int16",
    # air_pressure is ~30 GB per domain-year and was only ever read to back-solve
    # the orography, which extract-topog now ships as an ancillary; the MSLP is
    # not in GLIDE's variable set. Their section 2.
    "drop_vars": ["air_pressure", "air_pressure_at_sea_level"],
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


# The physical range each variable is quantised over, from their section 7 table,
# keyed on our CF names with theirs alongside. Anything absent stays float32.
#
# The two stresses are deliberately absent: their table has no stress entry,
# because they derive u* from the stresses and quantise that instead. Inverting
# their own formula for a stress range (tau = rho u*^2 -> +-120 N m-2 at
# rho ~ 1.225) puts the u* floor at 0.055 m s-1, above their own sanity lower
# bound of 0.03 — so a stress range consistent with their table is worse than not
# quantising at all. They are 2D surface fields, so float32 costs almost nothing.
QUANT_RANGES = {
    "x_wind": (-200.0, 200.0),                               # u_component_of_wind
    "y_wind": (-200.0, 200.0),                               # v_component_of_wind
    "upward_air_velocity": (-50.0, 50.0),                    # vertical_velocity
    "air_temperature": (150.0, 350.0),                       # temperature
    "specific_humidity": (0.0, 0.06),                        # specific_humidity
    "surface_air_pressure": (30000.0, 110000.0),             # surface_pressure
    "atmosphere_boundary_layer_thickness": (0.0, 15000.0),   # boundary_layer_height
    # Their range is symmetric, so it holds whichever sign convention the field
    # carries: we ship the UM's positive-upward field under its CF name and GLIDE
    # negates on read.
    "surface_upward_sensible_heat_flux": (-3000.0, 3000.0),  # surface_sensible_heat_flux
    "friction_velocity": (0.0, 10.0),                        # friction_velocity
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
