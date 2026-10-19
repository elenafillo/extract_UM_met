#!/bin/bash
#SBATCH -p standard
#SBATCH --account=acrg
#SBATCH --qos=high
#SBATCH --mem=150GB
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=5
#SBATCH --output=scripts/logs/%x_%j.out
#SBATCH --error=scripts/logs/%x_%j.err
#SBATCH -t 06:00:00

# One serial job: extract, join and append, end to end. Submit from the repo
# root so config.yaml is picked up.
#
#   sbatch scripts/launch_met.sh 2016 SA
#   sbatch scripts/launch_met.sh 201601 SA
#   sbatch scripts/launch_met.sh 201606,201607 SA     # two months, one store
#   sbatch scripts/launch_met.sh 20160115 SA          # single-day smoke test
#   sbatch scripts/launch_met.sh 2016 NA coarse       # → NORTHAFRICA_coarse_Met_2016.zarr
#
# Job names default to the script name; pass -J for something readable:
#   sbatch -J SA2016 scripts/launch_met.sh 2016 SA
#
# Rebuild a year from scratch instead of resuming:
#   sbatch --export=ALL,OVERWRITE=1 scripts/launch_met.sh 2016 SA
#
# Use this for a single month, a smoke test, a non-tiled domain (NZCSM), or
# native grid mode. For a full year of a tiled domain, scripts/launch_met_array.sh
# is faster — it extracts each (month, region) as a separate array task.

set -euo pipefail

DATES=${1:?Pass a date: YYYY (full year), YYYYMM (month) or YYYYMMDD (day). Comma-separate several.}
DOMAIN=${2:-SA}
SUFFIX=${3:-}

cd "${SLURM_SUBMIT_DIR:-$PWD}"   # the repo root; config.yaml is read from here

module purge
module load jaspy

export TMPDIR=/work/scratch-pw5/$USER/tmp
# -p races on parallel scratch (stale stat after EEXIST); tolerate a concurrent create.
mkdir -p "$TMPDIR" 2>/dev/null || [ -d "$TMPDIR" ]

echo "Domain: $DOMAIN"
echo "Dates:  $DATES"
echo "Suffix: ${SUFFIX:-(none)}"

# --date takes one YYYY/YYYYMM/YYYYMMDD, so several months is several runs. They
# share a yearly store: the first creates it, the rest append.
OVERWRITE_FLAG=${OVERWRITE:+--overwrite}

for DATE in ${DATES//,/ }; do
    echo ""
    echo "########## $DATE ##########"

    # Pass --suffix and --overwrite only when set, so the defaults are the main
    # store and resume.
    python -m extract_um_met run \
        --domain "$DOMAIN" \
        --date "$DATE" \
        ${SUFFIX:+--suffix "$SUFFIX"} \
        $OVERWRITE_FLAG

    # Only the first date rebuilds the year; carrying --overwrite into the rest
    # would wipe the store this loop is filling.
    OVERWRITE_FLAG=
done
