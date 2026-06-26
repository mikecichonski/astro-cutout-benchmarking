#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
runner_script=""

if [[ $# -eq 0 ]]; then
  echo "Usage: sbatch ... vlkb_benchmark_job.sbatch --binary /path/to/vlkb --mode imcopy|imcopydav --source <path-or-url> [--rows-per-chunk N] ..." >&2
  exit 2
fi

output_dir=""
env_script=""
forwarded_args=()
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  if [[ "${args[$i]}" == "--output-dir" && $((i + 1)) -lt ${#args[@]} ]]; then
    output_dir="${args[$((i + 1))]}"
  fi
  if [[ "${args[$i]}" == "--env-script" && $((i + 1)) -lt ${#args[@]} ]]; then
    env_script="${args[$((i + 1))]}"
    i=$((i + 1))
    continue
  fi
  if [[ "${args[$i]}" == "--runner-script" && $((i + 1)) -lt ${#args[@]} ]]; then
    runner_script="${args[$((i + 1))]}"
    i=$((i + 1))
    continue
  fi
  forwarded_args+=("${args[$i]}")
done

if [[ -n "$output_dir" ]]; then
  mkdir -p "$output_dir"
  {
    echo "date_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "hostname=$(hostname)"
    echo "slurm_job_id=${SLURM_JOB_ID:-}"
    echo "slurm_job_name=${SLURM_JOB_NAME:-}"
    echo "slurm_cpus_per_task=${SLURM_CPUS_PER_TASK:-}"
    echo "slurm_job_nodelist=${SLURM_JOB_NODELIST:-}"
    echo "slurm_partition=${SLURM_JOB_PARTITION:-}"
    echo "env_script=${env_script:-}"
  } > "$output_dir/job-env.txt"

  if command -v lscpu >/dev/null 2>&1; then
    lscpu > "$output_dir/lscpu.txt"
  fi
  if command -v numactl >/dev/null 2>&1; then
    numactl --hardware > "$output_dir/numactl-hardware.txt"
  fi
fi

if [[ -n "$env_script" ]]; then
  # shellcheck source=/dev/null
  source "$env_script"
fi

if [[ -z "$runner_script" ]]; then
  runner_script="$script_dir/vlkb_cutout_bench.py"
fi

python3 "$runner_script" run "${forwarded_args[@]}"
