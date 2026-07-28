#!/bin/bash
# Join the per-region intermediates and append each month to the yearly zarr
# store. Submitted by scripts/launch_met_array.sh with a dependency on the
# extract array, so every intermediate already exists by the time this runs —
# join_month reuses them (reuse_existing=True) rather than re-extracting.
# Do not sbatch directly.
#
# Months must be appended in order, so this stays a single serial job:
# ~10 min/month observed for NORTHAFRICA.
#SBATCH --mem=150GB
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=6
#SBATCH --output=scripts/logs/%x_%j.out
#SBATCH --error=scripts/logs/%x_%j.err
#SBATCH -t 08:00:00

set -euo pipefail

cd "$REPO_ROOT"          # exported by launch_met_array.sh; config.yaml is read from here

module purge
module load jaspy

export TMPDIR=/work/scratch-pw5/$USER/tmp
# -p races on parallel scratch (stale stat after EEXIST); tolerate a concurrent create.
mkdir -p "$TMPDIR" 2>/dev/null || [ -d "$TMPDIR" ]

echo "Joining $DOMAIN $DATE"

python -m extract_um_met run \
    --domain "$DOMAIN" \
    --date "$DATE" \
    ${SUFFIX:+--suffix "$SUFFIX"} \
    --overwrite
