#!/usr/bin/env python3
"""Submit the CANFAR benchmark matrix as a headless session via the CANFAR API."""

from __future__ import annotations

import argparse
import shlex
import sys
import time
from typing import Any, Dict, Optional, Sequence


TERMINAL_STATUSES = {"Completed", "Failed", "Error", "Terminated", "Succeeded"}


def shell_join(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(part)) for part in command)


def parse_env(values: Optional[Sequence[str]]) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for value in values or []:
        if "=" not in value:
            raise SystemExit(f"--env values must be KEY=VALUE, got: {value}")
        key, raw = value.split("=", 1)
        env[key] = raw
    return env


def matrix_command(args: argparse.Namespace) -> list[str]:
    command = [
        "python3",
        "benchmarks/canfar/run_canfar_matrix.py",
        "--engine",
        args.engine,
        "--binary",
        args.binary,
        "--source",
        args.source,
        "--output-root",
        args.output_root or f"{args.repo_dir.rstrip('/')}/results/canfar",
        "--warmup",
        str(args.warmup),
        "--repeats",
        str(args.repeats),
    ]
    if args.mode:
        command.extend(["--mode", args.mode])
    if args.extnum is not None:
        command.extend(["--extnum", str(args.extnum)])
    if args.target_mib:
        command.append("--target-mib")
        command.extend(str(target_mib) for target_mib in args.target_mib)
    if args.pixel_filter:
        command.append("--pixel-filter")
        command.extend(args.pixel_filter)
    if args.parallel_workers:
        command.append("--parallel-workers")
        command.extend(str(value) for value in args.parallel_workers)
    if args.rows_per_chunk:
        command.append("--rows-per-chunk")
        command.extend(str(value) for value in args.rows_per_chunk)
    if args.label_prefix:
        command.extend(["--label-prefix", args.label_prefix])
    if args.session_name:
        command.extend(["--session-name", args.session_name])
    return command


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch astro cutout benchmarks in a CANFAR headless session."
    )
    parser.add_argument(
        "--repo-dir",
        required=True,
        help="Path to this repository inside CANFAR, e.g. /arc/projects/<project>/astro-cutout-benchmarking.",
    )
    parser.add_argument("--name", default="astro-cutout-benchmark")
    parser.add_argument("--image", default="images.canfar.net/skaha/astroml:latest")
    parser.add_argument("--cores", type=int, help="Fixed CPU cores. Omit for CANFAR flexible mode.")
    parser.add_argument("--ram", type=int, help="Fixed RAM in GB. Omit for CANFAR flexible mode.")
    parser.add_argument("--gpu", type=int)
    parser.add_argument("--replicas", type=int, default=1)
    parser.add_argument("--env", action="append", help="Environment variable for the session, KEY=VALUE.")
    parser.add_argument("--wait", action="store_true", help="Poll until the headless session reaches a terminal state.")
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--print-logs", action="store_true")
    parser.add_argument("--dry-run", action="store_true")

    parser.add_argument("--engine", choices=("subfits", "vlkb-soda"), required=True)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--mode", choices=("subfits", "imcopy", "imcopydav"))
    parser.add_argument("--extnum", type=int, default=0)
    parser.add_argument("--target-mib", nargs="+", type=int)
    parser.add_argument("--pixel-filter", nargs="+")
    parser.add_argument("--parallel-workers", nargs="+", type=int, default=[1])
    parser.add_argument("--rows-per-chunk", nargs="+", type=int)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output-root")
    parser.add_argument("--session-name")
    parser.add_argument("--label-prefix", default="")
    return parser.parse_args(argv)


def wait_for_sessions(session: Any, ids: Sequence[str], poll_seconds: int) -> None:
    pending = set(ids)
    while pending:
        infos = session.info(list(pending))
        if isinstance(infos, dict):
            infos = [infos]
        for info in infos:
            session_id = str(info.get("id", ""))
            status = str(info.get("status", "unknown"))
            print(f"{session_id}\t{status}")
            if session_id and status in TERMINAL_STATUSES:
                pending.discard(session_id)
        if pending:
            time.sleep(poll_seconds)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    inner = f"cd {shlex.quote(args.repo_dir)} && {shell_join(matrix_command(args))}"
    bash_args = f"-lc {shlex.quote(inner)}"

    print(f"image={args.image}")
    print(f"cmd=bash {bash_args}")

    if args.dry_run:
        return 0

    try:
        from canfar.sessions import Session
    except ImportError as exc:
        raise SystemExit(
            "Install and authenticate the CANFAR client first: "
            "python3 -m pip install --upgrade canfar && canfar login cadc"
        ) from exc

    session = Session()
    ids = session.create(
        name=args.name,
        image=args.image,
        cores=args.cores,
        ram=args.ram,
        gpu=args.gpu,
        kind="headless",
        cmd="bash",
        args=bash_args,
        env=parse_env(args.env),
        replicas=args.replicas,
    )
    if not ids:
        print("CANFAR did not create a session. Set CANFAR_LOGLEVEL=DEBUG for client details.", file=sys.stderr)
        return 1

    print("session_ids=" + ",".join(ids))
    if args.wait:
        wait_for_sessions(session, ids, args.poll_seconds)
    if args.print_logs:
        logs = session.logs(ids)
        if isinstance(logs, dict):
            for session_id, text in logs.items():
                print(f"===== {session_id} =====")
                print(text)
        elif logs:
            print(logs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
