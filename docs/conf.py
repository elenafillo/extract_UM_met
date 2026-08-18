"""Sphinx configuration for the extract_UM_met documentation site.

The pages in ``docs/`` are one-line MyST ``include`` stubs pointing at
``How_Tos/``, which stays the single source of truth — edit the files there,
not here.
"""

from datetime import date

# Every link the docs make to a source file is built from these two values.
# When zarr_stores merges, set GITHUB_BRANCH = "main" and all of them follow.
GITHUB_REPO = "https://github.com/elenafillo/extract_UM_met"
GITHUB_BRANCH = "zarr_stores"

project = "extract_UM_met"
author = "Elena Fillola"
copyright = f"{date.today().year}, {author}"

extensions = ["myst_parser", "sphinx_design", "sphinx.ext.extlinks"]

# Write source-file links as {src}`extract_um_met/cli.py`, or with custom text
# as {src}`cli.py <extract_um_met/cli.py>`. The branch is never spelled out in
# a page, so it only ever changes here.
extlinks = {"src": (f"{GITHUB_REPO}/blob/{GITHUB_BRANCH}/%s", "%s")}

# colon_fence lets the how-tos use ::: blocks; heading anchors let cross-page
# links like setup.md#3-set-tmpdir-to-scratch resolve.
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
        {"title": "Setup", "url": "setup"},
        {"title": "Extracting", "url": "extracting"},
        {"title": "Concepts", "url": "concepts"},
        {"title": "Reference", "url": "reference"},
        {"title": "Topography", "url": "topography"},
        {"title": "Troubleshooting", "url": "troubleshooting"},
        {"title": "Codebase", "url": "codebase"},
    ],
}


