"""
Configuration loading and resolution utilities.

Loads the project config.yaml and provides utilities for accessing and
resolving configuration values with string templating.
"""

import os
import yaml


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

        Parameters
        ----------
        domain_key : str
            The domain key (e.g., 'SA', 'NA').

        Returns
        -------
        dict
            The domain configuration dict.

        Raises
        ------
        KeyError
            If the domain is not found.
        """
        return self.data['domains'][domain_key]

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
