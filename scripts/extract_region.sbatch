#!/bin/bash
# One array task = one (month, region) pair → one scratch .zarr intermediate.
# Submitted by scripts/launch_met_array.sh, which sets DOMAIN / YEAR /
# START_MONTH / REGIONS / SUFFIX and the array range. Do not sbatch directly.
#
# Sizing is per region-month: ~23 min and well under 100 GB for the largest
# NORTHAFRICA region observed. Raise --mem if a bigger domain OOMs.
#SBATCH --mem=200GB
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=6
#SBATCH --output=scripts/logs/%x_%A_%a.out
#SBATCH --error=scripts/logs/%x_%A_%a.err
#SBATCH -t 03:00:00

set -euo pipefail

cd "$REPO_ROOT"          # exported by launch_met_array.sh; config.yaml is read from here

module purge
module load jaspy

export TMPDIR=/work/scratch-pw5/$USER/tmp
# -p races on parallel scratch (stale stat after EEXIST); tolerate a concurrent create.
mkdir -p "$TMPDIR" 2>/dev/null || [ -d "$TMPDIR" ]

IFS=':' read -ra REGION_LIST <<< "$REGIONS"
N_REGIONS=${#REGION_LIST[@]}

MONTH=$((START_MONTH + SLURM_ARRAY_TASK_ID / N_REGIONS))
REGION=${REGION_LIST[$((SLURM_ARRAY_TASK_ID % N_REGIONS))]}
YYYYMM=$(printf "%04d%02d" "$YEAR" "$MONTH")

echo "Task $SLURM_ARRAY_TASK_ID → $DOMAIN $YYYYMM region $REGION"

python -m extract_um_met extract \
    --domain "$DOMAIN" \
    --date "$YYYYMM" \
    --region "$REGION" \
    ${SUFFIX:+--suffix "$SUFFIX"}
