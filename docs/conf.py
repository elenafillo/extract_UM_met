"""Sphinx configuration for the extract_UM_met documentation site.

The how-to pages in ``docs/`` are symlinks into ``How_Tos/``, which stays the
single source of truth — edit the files there, not here.
"""

from datetime import date

# Change this if the repo is renamed or moved. Note the how-tos also carry
# absolute github.com links (to source files, which have no page on this site);
# those are pinned to the zarr_stores branch until it merges to main.
GITHUB_REPO = "https://github.com/elenafillo/extract_UM_met"

project = "extract_UM_met"
author = "Elena Fillola"
copyright = f"{date.today().year}, {author}"

extensions = ["myst_parser", "sphinx_design"]

# colon_fence lets the how-tos use ::: blocks; heading anchors let cross-page
# links like how_to_setup.md#3-set-tmpdir-to-scratch resolve.
myst_enable_extensions = ["colon_fence", "deflist", "linkify", "substitution"]
myst_heading_anchors = 3

exclude_patterns = ["_build"]

html_theme = "shibuya"
html_title = "extract_UM_met"
html_static_path = ["_static"]
html_css_files = ["custom.css"]

html_theme_options = {
    "github_url": GITHUB_REPO,
    "accent_color": "teal",
    "globaltoc_expand_depth": 1,
    "nav_links": [
        {"title": "Setup", "url": "how_to_setup"},
        {"title": "Data types", "url": "how_to_datatypes"},
        {"title": "Extracting", "url": "how_to_extract"},
        {"title": "Codebase", "url": "how_to_code"},
    ],
}


