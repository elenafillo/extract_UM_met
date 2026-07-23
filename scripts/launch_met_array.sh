#!/bin/bash
# Launch parallel per-region extraction, then a dependent join.
#
# Run this on a login node (it submits jobs, it is not itself an sbatch script):
#   bash scripts/launch_met_array.sh 2015 NA
#   bash scripts/launch_met_array.sh 201501 NA          # one month
#   bash scripts/launch_met_array.sh 2015 NA coarse     # → NORTHAFRICA_coarse_Met_2015.zarr
#
# One array task per (month, region) writes a scratch intermediate; the join job
# then runs the normal `run` command, which reuses those intermediates
# (join_month defaults to reuse_existing=True) and appends each month to the
# yearly zarr store.

set -euo pipefail

DATE=${1:?Pass a date: YYYY (full year) or YYYYMM (single month)}
DOMAIN=${2:-NA}
SUFFIX=${3:-}

ACCOUNT=acrg
QOS=high
PARTITION=standard
MAX_CONCURRENT=12          # array tasks running at once

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$REPO_ROOT"

module purge
module load jaspy

# Region codes and grid mode come from config.yaml via the package itself, so
# this script never duplicates the domain table.
read -r REGIONS GRID_MODE < <(python -c "
from extract_um_met.config import Config
cfg = Config()
domain_cfg = cfg.get_domain(cfg.resolve_domain_name('$DOMAIN'))
regions = domain_cfg.get('world_regions_codes')
# ':' not ',' — this list is passed through sbatch --export, whose own
# separator is a comma.
print(':'.join(str(r) for r in regions) if regions else 'none',
      domain_cfg.get('grid', {}).get('mode', 'footprint'))
")

# An unknown domain makes the query above exit non-zero, which process
# substitution hides from set -e — catch the resulting empty read here.
if [ -z "${REGIONS:-}" ]; then
    echo "ERROR: could not read domain '$DOMAIN' from config.yaml." >&2
    exit 1
fi

if [ "$REGIONS" = "none" ]; then
    echo "ERROR: $DOMAIN is a non-tiled domain (no world regions) — there is" >&2
    echo "       nothing to split across array tasks. Use scripts/launch_met.sh." >&2
    exit 1
fi

# In native mode `run` snaps the whole year to one canonical Mk, while a
# standalone `extract` uses each month's own Mk. The intermediates would be on
# different grids and the join would silently produce a mixed-grid store.
if [ "$GRID_MODE" = "native" ]; then
    echo "ERROR: $DOMAIN uses grid mode 'native', which is not safe to extract" >&2
    echo "       per-region in parallel (see how_to_datatypes.md). Use" >&2
    echo "       scripts/launch_met.sh instead." >&2
    exit 1
fi

case ${#DATE} in
    4) START_MONTH=1;             N_MONTHS=12 ;;
    6) START_MONTH=${DATE:4:2};   N_MONTHS=1  ;;
    *) echo "ERROR: --date must be YYYY or YYYYMM, got '$DATE'" >&2; exit 1 ;;
esac
YEAR=${DATE:0:4}
START_MONTH=$((10#$START_MONTH))

N_REGIONS=$(awk -F: '{print NF}' <<< "$REGIONS")
N_TASKS=$((N_MONTHS * N_REGIONS))

echo "Domain:  $DOMAIN ($GRID_MODE grid)"
echo "Year:    $YEAR, months $START_MONTH..$((START_MONTH + N_MONTHS - 1))"
echo "Regions: ${REGIONS//:/ }"
echo "Suffix:  ${SUFFIX:-(none)}"
echo "Extract: $N_TASKS array task(s), max $MAX_CONCURRENT concurrent"

EXPORT_VARS="ALL,REPO_ROOT=$REPO_ROOT,DOMAIN=$DOMAIN,SUFFIX=$SUFFIX,YEAR=$YEAR,START_MONTH=$START_MONTH,REGIONS=$REGIONS,DATE=$DATE"

EXTRACT_ID=$(sbatch --parsable \
    --account="$ACCOUNT" --qos="$QOS" -p "$PARTITION" \
    --array="0-$((N_TASKS - 1))%$MAX_CONCURRENT" \
    --job-name="${DOMAIN}${DATE}_extract" \
    --export="$EXPORT_VARS" \
    "$REPO_ROOT/scripts/extract_region.sbatch")
echo "Submitted extract array: $EXTRACT_ID"

JOIN_ID=$(sbatch --parsable \
    --account="$ACCOUNT" --qos="$QOS" -p "$PARTITION" \
    --dependency="afterok:$EXTRACT_ID" \
    --job-name="${DOMAIN}${DATE}_join" \
    --export="$EXPORT_VARS" \
    "$REPO_ROOT/scripts/join_year.sbatch")
echo "Submitted join (after extract array succeeds): $JOIN_ID"
echo
echo "Watch with:  squeue -u \$USER"
echo "Logs in:     $REPO_ROOT/scripts/logs/"
