"""
Iris data I/O utilities for loading UM meteorology data.

Functions for loading .pp and .pp.gz files, handling coordinate callbacks,
and determining which Mk version to use for a given date.
"""

import iris
import os
import glob
import gzip
import shutil
import datetime
import concurrent.futures


def remove_coord_callback(cube, field, filename):
    """
    Remove problematic coordinates from iris cube during loading.

    Removes the 'um_version' attribute if present, which can cause issues
    in subsequent processing.

    Parameters
    ----------
    cube : iris.Cube
        The cube being loaded.
    field : object
        The field metadata.
    filename : str
        The filename being loaded from.
    """
    if "um_version" in cube.attributes.keys():
        cube.attributes.pop("um_version")


def _has_levels(cube):
    """True if the cube carries a model_level_number coordinate."""
    return "model_level_number" in {co.name() for co in cube.coords()}


def _pick(cubes, name, want_levels):
    """
    Select the single cube matching ``name`` and level-dimension presence.

    Selecting by name + level presence (rather than positional index) makes the
    variable choice explicit and fails loudly if the archive's cube set changes,
    instead of silently returning the wrong array.

    Raises
    ------
    ValueError
        If zero or more than one cube matches.
    """
    matches = [c for c in cubes if c.name() == name and _has_levels(c) == want_levels]
    if len(matches) != 1:
        available = [(c.name(), _has_levels(c)) for c in cubes]
        raise ValueError(
            f"expected exactly 1 cube for name={name!r} want_levels={want_levels}, "
            f"found {len(matches)}. Loaded cubes (name, has_levels): {available}"
        )
    return matches[0]


def _theta_ladder_cube(cube, source):
    """
    The first leveled mass cube — the theta ladder the vertical grid is read from.

    Must be a mass field: u/v sit on rho levels, which are the midpoints between
    the theta levels, so a height ceiling resolved against a wind cube selects a
    different set of level numbers, and a de-stagger read from one would pair the
    ladder with itself.
    """
    name = next((n for n, want_levels in source.mass_vars if want_levels), None)
    if name is None:
        raise ValueError(
            f"a model-level mass field is needed to read level_height from, "
            f"but no mass_vars entry for source {source.name!r} carries model levels "
            f"(mass_vars={source.mass_vars})."
        )
    return _pick(cube, name, True)


def get_Mk(year, month):
    """
    Determine the UM Mk version for a given year/month.

    The Mk version determines the file naming convention and native resolution
    of UM data. Mk boundaries can fall mid-year.

    Parameters
    ----------
    year : int
        The year (e.g., 2016).
    month : str or int
        The month, either 1-12 (int) or "01"-"12" (str).

    Returns
    -------
    int
        The Mk version (6, 7, 8, 9, 10, 11, or 12).

    Raises
    ------
    ValueError
        If no Mk version is defined for the given date.
    """
    month = int(month) if isinstance(month, str) else month
    month_str = f"{month:02d}"

    if year in (2011, 2012) or (year == 2013 and month in range(1, 5)):
        return 6
    elif (year == 2013 and month in range(5, 13)) or (year == 2014 and month in range(1, 7)):
        return 7
    elif (year == 2014 and month in range(7, 13)) or (year == 2015 and month in range(1, 8)):
        return 8
    elif (year == 2015 and month in range(8, 13)) or (year == 2016) or (year == 2017 and month in range(1, 7)):
        return 9
    elif (year == 2017 and month in range(7, 13)) or (2017 < year < 2022) or (year == 2022 and month in range(1, 6)):
        return 10
    elif (year == 2022 and month in range(6, 13)) or (2022 < year < 2026):
        return 11
    elif year >= 2026:
        return 12
    else:
        raise ValueError(f"No Mk version found for year={year}, month={month_str}")


# Basenames skipped at load time. Empty: no file in the archive currently needs
# it. The 2014-02-01 15:00 dump used to be listed here, but the pipeline absorbs
# its odd data time (see How_Tos/known_problems.md), so it loads like any other.
_KNOWN_BAD_FILES = set()


def _decompress_workers():
    """Thread count for parallel decompression: the task's CPUs, else all cores."""
    return int(os.environ.get("SLURM_CPUS_PER_TASK") or os.cpu_count() or 1)


def _decompress_gz(src, dest):
    """
    Decompress one ``.pp.gz`` to ``dest`` via a per-process temp file + atomic rename.

    An interrupted decompress (crash, kill, full disk) then leaves only a `.tmp`
    file, never a truncated `dest` that later runs would reuse without
    re-validating (the cause of the merge-time "meta or dtype" failures). The PID
    keeps concurrent SLURM array tasks sharing this scratch from clobbering each
    other's temp.
    """
    tmp = f"{dest}.tmp.{os.getpid()}"
    try:
        with gzip.open(src, "rb") as f_in, open(tmp, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        os.replace(tmp, dest)   # atomic on the same filesystem
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def load_files(files, vars, homefolder, callback=remove_coord_callback):
    """
    Load iris cubes from an explicit list of archive files (source-agnostic).

    Compressed files (``.pp.gz``) are decompressed into ``homefolder`` first
    (reusing any already present); uncompressed ``.pp`` files are read directly
    from the archive. Known-bad files are skipped. This is the loader used by the
    :class:`~extract_um_met.sources.MetSource`-driven pipeline, where file discovery
    is done by ``source.list_files`` rather than reconstructed here.

    Parameters
    ----------
    files : list of str
        Archive file paths (from ``source.list_files``).
    vars : list of str or iris constraint
        Variables/constraints to load.
    homefolder : str
        Scratch directory for decompressed files (trailing separator ok).
    callback : callable, optional
        iris load callback (default strips the ``um_version`` attribute).

    Returns
    -------
    iris.CubeList
    """
    if not files:
        raise FileNotFoundError("load_files: no input files to load")

    try:
        os.makedirs(homefolder, exist_ok=True)
    except FileExistsError:
        pass

    to_load = []
    jobs = []   # (src_gz, dest) pairs still needing decompression
    for f in files:
        base = os.path.basename(f)
        if base in _KNOWN_BAD_FILES or base[:-3] in _KNOWN_BAD_FILES:
            continue
        if f.endswith(".gz"):
            dest = os.path.join(homefolder, base[:-3])   # strip '.gz'
            if not os.path.exists(dest):
                jobs.append((f, dest))
            to_load.append(dest)
        else:
            to_load.append(f)   # uncompressed: read directly from the archive

    # Decompress the missing files in parallel across the task's CPUs — zlib
    # releases the GIL, so threads scale. Iterating the results propagates the
    # first failure (each job cleans up its own temp on the way out).
    if jobs:
        with concurrent.futures.ThreadPoolExecutor(max_workers=_decompress_workers()) as pool:
            list(pool.map(lambda job: _decompress_gz(*job), jobs))

    print(f"[load_files] {len(to_load)} file(s) to load "
          f"({len(jobs)} newly decompressed to scratch)")
    loaded = iris.load(to_load, vars, callback=callback)
    print(f"[load_files] loaded {len(loaded)} cube(s)")
    return loaded


def load_iris(filepath, Mk, date, vars, num, homefolder):
    """
    Load iris cubes from UM .pp or .pp.gz files.

    Handles both compressed (.pp.gz) and uncompressed (.pp) files. For
    compressed files, extracts them to a scratch directory before loading.

    Parameters
    ----------
    filepath : str or tuple of str
        Either a direct file path (str) or a 2-tuple (prefix, suffix) for
        constructing the filename. If tuple, the filename is constructed as
        prefix + date + suffix + num + ".pp[.gz]".
    Mk : int
        The UM Mk version (determines whether files are compressed).
    date : str
        The date string (e.g., "201601").
    vars : list of str or iris.util.ConstraintMixin, optional
        Variables/constraints to load. If None, loads all variables.
    num : int, optional
        Region or file number (used only with tuple-based filepath).
    homefolder : str
        Directory to extract uncompressed files to.

    Returns
    -------
    iris.CubeList
        Loaded cubes.

    Raises
    ------
    Exception
        If loading fails, raises and removes temporary files.
    """
    bad_files = []   # basenames to skip; see _KNOWN_BAD_FILES

    # If filepath is a string, load directly
    if isinstance(filepath, str):
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"File not found: {filepath}")
        print(f"[load_iris] loading single file: {filepath}")
        try:
            loaded = iris.load(filepath, vars, callback=remove_coord_callback)
            print(f"[load_iris] loaded {len(loaded)} cube(s)")
            return loaded
        except Exception as e:
            print(f"[load_iris] load failed: {e}")
            raise

    # Handle tuple-based filepath (original behavior)
    # Mk 10 files are already unzipped
    if Mk == 10:
        archive_pattern = filepath[0] + date + filepath[1] + str(num) + ".pp"
        matched = glob.glob(archive_pattern)
        print(f"[load_iris] Mk{Mk} region {num} {date}: matched {len(matched)} "
              f"uncompressed file(s)")
        print(f"[load_iris]   archive pattern: {archive_pattern}")
        if not matched:
            raise FileNotFoundError(f"No files matched: {archive_pattern}")
        try:
            loaded = iris.load(matched, vars, callback=remove_coord_callback)
        except Exception as e:
            print(f"[load_iris] load failed for region {num} {date}: {e}")
            os.system("rm -r " + homefolder + "MO" + date + "*")
            raise
        print(f"[load_iris] loaded {len(loaded)} cube(s) for region {num}")
        return loaded

    # Other Mks are compressed (.pp.gz) and must be unzipped to scratch first.
    archive_pattern = filepath[0] + date + filepath[1] + str(num) + ".pp.gz"
    files = glob.glob(archive_pattern)
    scratch_pattern = homefolder + "*" + date + filepath[1] + str(num) + ".pp"
    homefiles = glob.glob(scratch_pattern)

    print(f"[load_iris] Mk{Mk} region {num} {date}: matched {len(files)} compressed "
          f"archive file(s); {len(homefiles)} already unzipped in scratch")
    print(f"[load_iris]   archive pattern: {archive_pattern}")
    print(f"[load_iris]   scratch pattern: {scratch_pattern}")

    # Reuse already-unzipped scratch files when the full set is present.
    if files and len(homefiles) == len(files):
        bad = {homefolder + b for b in bad_files}
        kept = [h for h in homefiles if h not in bad]
        n_skipped = len(homefiles) - len(kept)
        msg = f"[load_iris] all {len(files)} file(s) already in scratch; loading without re-unzipping"
        if n_skipped:
            msg += f" ({n_skipped} known-bad file(s) skipped)"
        print(msg)
        try:
            loaded = iris.load(kept, vars, callback=remove_coord_callback)
            print(f"[load_iris] loaded {len(loaded)} cube(s) for region {num}")
            return loaded
        except Exception as e:
            print(f"[load_iris] loading cached scratch failed ({e}); re-unzipping from archive")

    if not files:
        raise FileNotFoundError(f"No archive files matched: {archive_pattern}")

    # Unzip each archive file to scratch, then load.
    try:
        all_outs = []
        n_skipped = 0
        for f in files:
            base = os.path.basename(f).replace(".gz", "")
            with gzip.open(f, "rb") as f_in:
                with open(homefolder + base, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
            if base in bad_files:
                n_skipped += 1
            else:
                all_outs.append(homefolder + base)
        msg = f"[load_iris] unzipped {len(all_outs)} file(s) to scratch"
        if n_skipped:
            msg += f" ({n_skipped} known-bad file(s) skipped)"
        print(msg)
        loaded = iris.load(all_outs, vars, callback=remove_coord_callback)
        print(f"[load_iris] loaded {len(loaded)} cube(s) for region {num}")
        return loaded
    except Exception as e:
        print(f"[load_iris] unzip/load failed for region {num} {date}: {e}")
        raise


def delete_iris(homefolder, date, num):
    """
    Delete the unzipped .pp scratch files for one region/date.

    Matches ``MO{date}*_L59PT{num}.pp`` so a region number is matched exactly at
    the end of the filename — ``num=3`` no longer also deletes region 13's files
    (which the old ``*{num}.pp`` glob did).

    Parameters
    ----------
    homefolder : str
        Directory containing the unzipped .pp files (trailing separator ok).
    date : str
        Glob key for the date (e.g., "201601" or "20150701").
    num : int
        World region number.

    Returns
    -------
    int
        Number of files removed.
    """
    pattern = os.path.join(homefolder, f"MO{date}*_L59PT{num}.pp")
    files = glob.glob(pattern)
    for f in files:
        try:
            os.remove(f)
        except OSError as e:
            print(f"[delete_iris] could not remove {f}: {e}")
    print(f"[delete_iris] removed {len(files)} unzipped .pp file(s) for region {num} {date}")
    return len(files)
