#!/usr/bin/env bash
# Environment setup for subfits on Dug.
# Source this before running the benchmark harness, and pass it as --env-script for sbatch jobs.
# NOTE: Do not use set -e here — this file is sourced into interactive shells.

if ! command -v module >/dev/null 2>&1 && [ -f /etc/profile.d/modules.sh ]; then
  source /etc/profile.d/modules.sh
fi

module load gcc/9.2.0
module load python/3.10.13

# Ensure ~/.local/bin is on PATH (ensurepip installs pip there)
export PATH="$HOME/.local/bin:$PATH"

# Bootstrap pip if needed, then install subfits dependencies
if ! python3 -c "import astropy, numpy, scipy" 2>/dev/null; then
  python3 -m ensurepip --user 2>/dev/null || true
  python3 -m pip install --user astropy numpy scipy
fi
