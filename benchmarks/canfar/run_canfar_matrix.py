#!/usr/bin/env python3
"""Run a local benchmark matrix inside a CANFAR container or any shell."""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import pathlib
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "value"


def repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def runner_script(engine: str) -> pathlib.Path:
    root = repo_root()
    if engine == "subfits":
        return root / "benchmarks" / "slurm" / "subfits" / "subfits_cutout_bench.py"
    return root / "benchmarks" / "slurm" / "vlkb-soda" / "vlkb_cutout_bench.py"


def read_json(path: pathlib.Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def write_tsv(path: pathlib.Path, rows: Sequence[Dict[str, Any]], columns: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def append_tsv(path: pathlib.Path, row: Dict[str, Any], columns: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), delimiter="\t", extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def shell_join(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(part)) for part in command)


def cut_specs(args: argparse.Namespace) -> List[Dict[str, str]]:
    specs: List[Dict[str, str]] = []
    for target_mib in args.target_mib or []:
        specs.append({"target_mib": str(target_mib), "pixel_filter": ""})
    for pixel_filter in args.pixel_filter or []:
        specs.append({"target_mib": "", "pixel_filter": pixel_filter})
    if not specs:
        raise SystemExit("Provide at least one --target-mib or --pixel-filter.")
    return specs


def default_mode(engine: str, raw_mode: Optional[str]) -> str:
    if raw_mode:
        return raw_mode
    return "subfits" if engine == "subfits" else "imcopy"


def build_run_command(
    args: argparse.Namespace,
    *,
    output_dir: pathlib.Path,
    label: str,
    cut_spec: Dict[str, str],
    parallel_workers: int,
    rows_per_chunk: Optional[int],
) -> List[str]:
    engine_tool = "subfits" if args.engine == "subfits" else "vlkb"
    mode = default_mode(args.engine, args.mode)
    command = [
        sys.executable,
        str(runner_script(args.engine)),
        "run",
        "--tool",
        engine_tool,
        "--binary",
        args.binary,
        "--mode",
        mode,
        "--source",
        args.source,
        "--extnum",
        str(args.extnum),
        "--warmup",
        str(args.warmup),
        "--repeats",
        str(args.repeats),
        "--parallel-workers",
        str(parallel_workers),
        "--output-dir",
        str(output_dir),
        "--label",
        label,
    ]
    if cut_spec["target_mib"]:
        command.extend(["--target-mib", cut_spec["target_mib"]])
    if cut_spec["pixel_filter"]:
        command.extend(["--pixel-filter", cut_spec["pixel_filter"]])
    if args.engine == "vlkb-soda" and rows_per_chunk is not None:
        command.extend(["--rows-per-chunk", str(rows_per_chunk)])
    return command


def summarize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    result_path = pathlib.Path(str(row["output_dir"])) / "result.json"
    data = read_json(result_path)
    summary = data.get("summary", {}) if isinstance(data, dict) else {}
    benchmark = data.get("benchmark", {}) if isinstance(data, dict) else {}
    return {
        **row,
        "result_json": str(result_path),
        "runner_succeeded": str(row.get("returncode", "")) == "0",
        "runner_summary_successful_repeats": summary.get("successful_repeats", ""),
        "runner_summary_failed_repeats": summary.get("failed_repeats", ""),
        "runner_summary_median_seconds": summary.get("median_seconds", ""),
        "runner_summary_median_throughput_mib_s": summary.get("median_throughput_mib_s", ""),
        "runner_summary_estimated_total_bytes_per_repeat": summary.get(
            "estimated_total_bytes_per_repeat", ""
        ),
        "runner_benchmark_pixel_filter": benchmark.get("pixel_filter", ""),
    }


def write_summary(session_dir: pathlib.Path, rows: Sequence[Dict[str, Any]]) -> pathlib.Path:
    summary_rows = [summarize_row(row) for row in rows]
    preferred = [
        "row_id",
        "engine",
        "mode",
        "source",
        "target_mib",
        "pixel_filter",
        "parallel_workers",
        "rows_per_chunk",
        "returncode",
        "runner_summary_successful_repeats",
        "runner_summary_failed_repeats",
        "runner_summary_median_seconds",
        "runner_summary_median_throughput_mib_s",
        "runner_benchmark_pixel_filter",
        "result_json",
        "output_dir",
    ]
    all_columns = list(dict.fromkeys([*preferred, *[key for row in summary_rows for key in row.keys()]]))
    summary_path = session_dir / "summary.csv"
    with open(summary_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=all_columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(summary_rows)
    return summary_path


def row_product(args: argparse.Namespace) -> Iterable[tuple[Dict[str, str], int, Optional[int]]]:
    rows_per_chunk_values: Sequence[Optional[int]]
    if args.engine == "vlkb-soda":
        rows_per_chunk_values = args.rows_per_chunk or [None]
    else:
        rows_per_chunk_values = [None]
    return itertools.product(cut_specs(args), args.parallel_workers, rows_per_chunk_values)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a cutout benchmark matrix without Slurm, suitable for CANFAR headless sessions."
    )
    parser.add_argument("--engine", choices=("subfits", "vlkb-soda"), required=True)
    parser.add_argument("--binary", required=True, help="Path to the subfits or vlkb executable.")
    parser.add_argument("--source", required=True, help="Local FITS path or HTTP(S) FITS URL.")
    parser.add_argument("--mode", choices=("subfits", "imcopy", "imcopydav"))
    parser.add_argument("--extnum", type=int, default=0)
    parser.add_argument("--target-mib", nargs="+", type=int, help="Derived centered cutout sizes.")
    parser.add_argument("--pixel-filter", nargs="+", help="Explicit FITS pixel filters.")
    parser.add_argument("--parallel-workers", nargs="+", type=int, default=[1])
    parser.add_argument("--rows-per-chunk", nargs="+", type=int, help="VLKB imcopy/imcopydav rows per chunk.")
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output-root", default="results/canfar")
    parser.add_argument("--session-name", help="Explicit output session directory name.")
    parser.add_argument("--label-prefix", default="")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    mode = default_mode(args.engine, args.mode)
    if args.engine == "subfits" and mode != "subfits":
        raise SystemExit("subfits benchmarks must use --mode subfits.")
    if args.engine == "vlkb-soda" and mode == "subfits":
        raise SystemExit("vlkb-soda benchmarks must use --mode imcopy or --mode imcopydav.")

    script = runner_script(args.engine)
    if not script.exists():
        raise SystemExit(f"Benchmark runner not found: {script}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    session_name = args.session_name or f"{timestamp}_{args.engine}"
    session_dir = pathlib.Path(args.output_root).expanduser().resolve() / session_name
    session_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = session_dir / "manifest.tsv"

    rows: List[Dict[str, Any]] = []
    columns = [
        "created_at_utc",
        "started_at_utc",
        "finished_at_utc",
        "row_id",
        "engine",
        "mode",
        "binary",
        "source",
        "target_mib",
        "pixel_filter",
        "parallel_workers",
        "rows_per_chunk",
        "output_dir",
        "returncode",
        "command_shell",
    ]

    for index, (cut_spec, parallel_workers, rows_per_chunk) in enumerate(row_product(args), start=1):
        cut_label = f"mib{cut_spec['target_mib']}" if cut_spec["target_mib"] else f"pix{index}"
        chunk_label = f"chunk{rows_per_chunk}" if rows_per_chunk is not None else "chunkdefault"
        prefix = f"{slugify(args.label_prefix)}-" if args.label_prefix else ""
        row_id = slugify(f"{prefix}{default_mode(args.engine, args.mode)}-{cut_label}-w{parallel_workers}-{chunk_label}")
        output_dir = session_dir / row_id
        command = build_run_command(
            args,
            output_dir=output_dir,
            label=row_id,
            cut_spec=cut_spec,
            parallel_workers=parallel_workers,
            rows_per_chunk=rows_per_chunk,
        )
        row: Dict[str, Any] = {
            "created_at_utc": utc_now(),
            "started_at_utc": "",
            "finished_at_utc": "",
            "row_id": row_id,
            "engine": args.engine,
            "mode": default_mode(args.engine, args.mode),
            "binary": args.binary,
            "source": args.source,
            "target_mib": cut_spec["target_mib"],
            "pixel_filter": cut_spec["pixel_filter"],
            "parallel_workers": parallel_workers,
            "rows_per_chunk": rows_per_chunk or "",
            "output_dir": str(output_dir),
            "returncode": "",
            "command_shell": shell_join(command),
        }

        print(row["command_shell"])
        if args.dry_run:
            row["returncode"] = "dry-run"
            append_tsv(manifest_path, row, columns)
            rows.append(row)
            continue

        output_dir.mkdir(parents=True, exist_ok=True)
        row["started_at_utc"] = utc_now()
        with open(output_dir / "controller.stdout", "w", encoding="utf-8") as stdout, open(
            output_dir / "controller.stderr", "w", encoding="utf-8"
        ) as stderr:
            completed = subprocess.run(command, cwd=str(repo_root()), stdout=stdout, stderr=stderr)
        row["finished_at_utc"] = utc_now()
        row["returncode"] = completed.returncode
        append_tsv(manifest_path, row, columns)
        rows.append(row)

    summary_path = write_summary(session_dir, rows)
    print(f"session_dir={session_dir}")
    print(f"manifest={manifest_path}")
    print(f"summary_csv={summary_path}")
    return 0 if all(str(row["returncode"]) in {"0", "dry-run"} for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
