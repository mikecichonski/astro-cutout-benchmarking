#!/usr/bin/env python3
"""Top-level dispatcher for the astro cutout benchmark harnesses."""

from __future__ import annotations

import pathlib
import subprocess
import sys
from typing import Iterable, Sequence


ROOT = pathlib.Path(__file__).resolve().parent

COMMANDS = {
    ("slurm", "subfits"): ROOT / "benchmarks" / "slurm" / "subfits" / "subfits_cutout_bench.py",
    ("slurm", "vlkb"): ROOT / "benchmarks" / "slurm" / "vlkb-soda" / "vlkb_cutout_bench.py",
    ("slurm", "vlkb-soda"): ROOT / "benchmarks" / "slurm" / "vlkb-soda" / "vlkb_cutout_bench.py",
    ("canfar", "matrix"): ROOT / "benchmarks" / "canfar" / "run_canfar_matrix.py",
    ("canfar", "submit"): ROOT / "benchmarks" / "canfar" / "submit_canfar.py",
}

ALIASES = {
    "slurm-subfits": ("slurm", "subfits"),
    "slurm-vlkb": ("slurm", "vlkb"),
    "slurm-vlkb-soda": ("slurm", "vlkb-soda"),
    "canfar-matrix": ("canfar", "matrix"),
    "canfar-submit": ("canfar", "submit"),
}


def usage() -> str:
    return """Usage:
  python3 astro_bench.py slurm subfits <runner-args>
  python3 astro_bench.py slurm vlkb <runner-args>
  python3 astro_bench.py canfar matrix <matrix-args>
  python3 astro_bench.py canfar submit <api-submit-args>

Aliases:
  python3 astro_bench.py slurm-subfits <runner-args>
  python3 astro_bench.py slurm-vlkb <runner-args>
  python3 astro_bench.py canfar-matrix <matrix-args>
  python3 astro_bench.py canfar-submit <api-submit-args>

Examples:
  python3 astro_bench.py slurm subfits --help
  python3 astro_bench.py slurm vlkb run-location-matrix --help
  python3 astro_bench.py canfar submit --help
"""


def resolve_command(argv: Sequence[str]) -> tuple[pathlib.Path, list[str]] | None:
    if not argv:
        return None

    first = argv[0]
    if first in ALIASES:
        key = ALIASES[first]
        return COMMANDS[key], list(argv[1:])

    if len(argv) >= 2:
        key = (argv[0], argv[1])
        if key in COMMANDS:
            return COMMANDS[key], list(argv[2:])

    return None


def command_lines() -> Iterable[str]:
    for key in sorted(COMMANDS):
        yield " ".join(key)
    for alias in sorted(ALIASES):
        yield alias


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if not raw_args or raw_args[0] in {"-h", "--help"}:
        print(usage().rstrip())
        return 0
    if raw_args[0] == "--list":
        print("\n".join(command_lines()))
        return 0

    resolved = resolve_command(raw_args)
    if resolved is None:
        print(f"Unknown command: {' '.join(raw_args[:2])}", file=sys.stderr)
        print(usage().rstrip(), file=sys.stderr)
        return 2

    script, forwarded_args = resolved
    if not script.exists():
        print(f"Benchmark entry point is missing: {script}", file=sys.stderr)
        return 2

    completed = subprocess.run([sys.executable, str(script), *forwarded_args], cwd=str(ROOT))
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
