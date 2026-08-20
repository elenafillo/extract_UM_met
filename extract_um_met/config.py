"""
Configuration loading and resolution utilities.

Loads the project config.yaml and provides utilities for accessing and
resolving configuration values with string templating.
"""

import os
import yaml

from .glide import resolve_output_profile


def load_config():
    """
    Load the project configuration file and normalize required keys.

    The function first looks for `config.yaml` in the current working directory,
    and falls back to `config.yml` if needed. The parsed YAML must produce a
    dictionary-like object; otherwise a ValueError is raised.

    For portability across user accounts, this helper guarantees that a `user`
    value exists in the returned config. If `user` is missing or empty in the
    file, it is populated from the `USER` environment variable (or an empty
    string if that variable is unavailable).

    Returns
    -------
    dict
        Parsed and normalized configuration mapping.
    """
    config_path = "config.yaml" if os.path.exists("config.yaml") else "config.yml"
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    if not isinstance(config, dict):
        raise ValueError("config.yaml did not parse to a dictionary")

    if not config.get("user"):
        config["user"] = os.environ.get("USER", "")

    return config


def store_stem(domain_name, suffix=None):
    """
    Build the output file stem for a domain, optionally tagged with a suffix.

    With no suffix the stem is just ``domain_name`` (e.g. ``NA``); with a suffix
    it is ``{domain_name}_{suffix}`` (e.g. ``NA_coarse``). This lets a variant
    extraction (a different grid mode, config tweak, etc.) live side by side with
    the main store under the same domain folder, e.g.::

        {zarr_dir}/NA/NA_Met_2014.zarr
        {zarr_dir}/NA/NA_coarse_Met_2014.zarr

    Surrounding whitespace and underscores on the suffix are trimmed so
    ``"_coarse"`` and ``"coarse"`` behave the same.

    Parameters
    ----------
    domain_name : str
        The domain's ``domain_name`` (e.g. ``NA``).
    suffix : str, optional
        Extra tag to append; if None or empty after trimming, the stem is just
        ``domain_name``.

    Returns
    -------
    str
        The file stem to use in output/intermediate filenames.
    """
    if suffix is None:
        return domain_name
    suffix = str(suffix).strip().strip("_")
    return f"{domain_name}_{suffix}" if suffix else domain_name


def resolve_config_value(value, config):
    """
    Resolve string templates in individual config values.

    If `value` is a string, this function applies Python `str.format(**config)`
    so placeholders such as `{user}` are expanded from keys in the config
    mapping. Non-string values are returned unchanged.

    Missing placeholders are handled safely: if a required key is not present,
    the original string is returned without raising an exception. This lets the
    caller decide whether unresolved placeholders are acceptable.

    Parameters
    ----------
    value : Any
        Raw config value to resolve.
    config : dict
        Configuration mapping used as format context.

    Returns
    -------
    Any
        Resolved value (for strings) or the original value (for non-strings or
        unresolved templates).
    """
    if not isinstance(value, str):
        return value
    try:
        return value.format(**config)
    except KeyError:
        return value


def resolve_max_level_m(domain_cfg):
    """
    A domain's model-level height ceiling in metres, or None if it has none.

    Per domain rather than under ``data_types:``, because that block is shared by
    every domain reading the same archive and a ceiling is a property of what the
    domain is for. It composes with the source's own level selection
    (``level_stride`` / ``model_levels``), which is applied first.
    """
    ceiling = domain_cfg.get("max_level_m")
    if ceiling is None:
        return None
    ceiling = float(ceiling)
    if ceiling <= 0:
        raise ValueError(
            f"domain max_level_m must be a positive height in metres, got {ceiling:g}"
        )
    return ceiling


def resolve_wind_vertical_regrid(domain_cfg):
    """
    How a domain treats the winds' vertical staggering: ``"snap"`` or ``"interpolate"``.

    ``snap`` is the default and is today's behaviour exactly — u/v keep their rho
    values on the theta-labelled level index — so existing stores stay
    bit-identical and the correction is opt-in. ``interpolate`` moves them onto
    the theta levels (see :mod:`extract_um_met.staggering`), which drops the top
    model level and makes the store incompatible with one built the other way.

    Per domain, beside ``max_level_m``: the ``data_types:`` block is shared by
    every domain reading the same archive, so a per-source key could never let a
    de-staggered GLIDE domain and a snapped NAME domain coexist.

    Raises on an unrecognised value rather than falling back to the default. The
    two modes produce fields of identical shape, coordinates and dtype, so a typo
    that quietly reverted to ``snap`` would leave nothing downstream to notice.
    """
    modes = ("snap", "interpolate")
    mode = domain_cfg.get("wind_vertical_regrid")
    if mode is None:
        return "snap"
    if mode not in modes:
        raise ValueError(
            f"domain wind_vertical_regrid must be one of {list(modes)}, got {mode!r}"
        )
    return mode


# First Mk whose fluxes are a 3-hour mean at every step. Before it the archive
# supplies a window on only half the steps and fills the rest with zero-length
# T+0 records (see How_Tos/known_problems.md), so there is no step-for-step flux
# series to snap or interpolate onto and only `keep` can describe what is there.
MK_REGULAR_FLUX_WINDOWS = 10


def resolve_flux_time_regrid(domain_cfg, mk=None):
    """
    How a domain timestamps the time-averaged fields: ``"snap"``, ``"interpolate"``
    or ``"keep"``.

    The fluxes and stresses are means over the source's averaging window, which
    the UM stamps at the window MIDPOINT — half a window (1.5 h for the 3-hourly
    archive) before the instantaneous step the window closes on.

    ``snap`` is the default and is today's behaviour exactly — the point is moved
    to the interval end so the averaged fields share one axis with the
    instantaneous ones — so existing stores stay bit-identical and any correction
    is opt-in. Read as instantaneous, which is what a dispersion model does, those
    fields are then half a window late.

    ``interpolate`` interpolates them in time from their midpoints back onto those
    same interval-end steps, so a field is centred on its timestamp. The axis is
    unchanged; only the values are. The cost is that the result is no longer the
    mean of any one window — it is a weighted mean of the two either side — and no
    longer conserves the integral.

    ``keep`` leaves them at the midpoint, on a second time axis
    (:data:`extract_um_met.metadata.FLUX_TIME_DIM`) generated from the windows the
    archive actually supplies. Nothing is re-stamped and nothing is interpolated,
    so it is the only lossless mode, but it costs a store dimension and a consumer
    that can read one.

    Per domain, beside ``wind_vertical_regrid``, and for the same reason: the
    ``data_types:`` block is shared by every domain reading the same archive, so a
    per-source key could never let a corrected GLIDE domain and a snapped NAME
    domain coexist.

    Raises on an unrecognised value rather than falling back to the default.
    ``snap`` and ``interpolate`` produce fields of identical shape, coordinates
    and dtype, so a typo that quietly reverted to ``snap`` would leave nothing
    downstream to notice.

    Parameters
    ----------
    domain_cfg : dict
        The domain's config block.
    mk : int, optional
        The lowest Mk generation the store's **year** is read from, from
        :meth:`extract_um_met.sources.MetSource.min_mk_in_year` — not the Mk of
        the month in hand. Below :data:`MK_REGULAR_FLUX_WINDOWS` the mode is
        forced to ``keep`` whatever the domain asks for: the other two both put
        one flux value on every instantaneous step, and that archive has a window
        for only half of them.

        Resolving against the year is what keeps a store self-consistent. Mk
        boundaries fall on month edges, so 2017 is Mk9 to June and Mk10 from July;
        resolved per month it would put the averaged fields on ``time_flux`` for
        the first half of the 2017 store and on ``time`` for the second, which is
        not a store anyone can read. Whole years are the unit that works, so a
        year containing any pre-Mk10 month is ``keep`` throughout.

        ``None`` (the default, and every Mk-less source) leaves the mode alone.
    """
    modes = ("snap", "interpolate", "keep")
    mode = domain_cfg.get("flux_time_regrid")
    if mode is None:
        mode = "snap"
    elif mode not in modes:
        raise ValueError(
            f"domain flux_time_regrid must be one of {list(modes)}, got {mode!r}"
        )
    if mk is not None and mk < MK_REGULAR_FLUX_WINDOWS:
        return "keep"
    return mode


class Config:
    """
    Light accessor for configuration values.

    Wraps the loaded config dict and provides convenient access to domain settings
    and automatic path resolution.
    """

    def __init__(self, config_dict=None):
        """
        Initialize the Config accessor.

        Parameters
        ----------
        config_dict : dict, optional
            The raw configuration dictionary. If None, load_config() is called.
        """
        self.data = config_dict if config_dict is not None else load_config()

    def get(self, key, default=None):
        """Get a value from the config, resolving any template strings."""
        value = self.data.get(key, default)
        return resolve_config_value(value, self.data)

    def get_domain(self, domain_key):
        """
        Get the configuration for a single domain.

        An ``output_profile:`` is resolved here — its defaults filled in under
        the domain's own keys — rather than in the CLI, because
        ``extract_region``, ``extract_single`` and ``join_month`` each call this
        themselves. Resolving it anywhere else would leave the per-region
        workers reading an unmerged config.

        Parameters
        ----------
        domain_key : str
            The domain key (e.g., 'SA', 'NA').

        Returns
        -------
        dict
            The domain configuration dict, with any profile defaults merged in.

        Raises
        ------
        KeyError
            If the domain is not found.
        """
        return resolve_output_profile(self.data['domains'][domain_key])

    def resolve_domain_name(self, name_or_key):
        """
        Resolve a domain by key or by domain_name.

        Parameters
        ----------
        name_or_key : str
            Either a domain key (e.g., 'SA') or a domain_name (e.g., 'SOUTHAMERICA').

        Returns
        -------
        str
            The domain key.

        Raises
        ------
        ValueError
            If the domain is not found.
        """
        # Try as a direct key first
        if name_or_key in self.data.get('domains', {}):
            return name_or_key

        # Try matching by domain_name
        for key, domain_cfg in self.data.get('domains', {}).items():
            if domain_cfg.get('domain_name') == name_or_key:
                return key

        raise ValueError(f"Domain '{name_or_key}' not found by key or domain_name")
