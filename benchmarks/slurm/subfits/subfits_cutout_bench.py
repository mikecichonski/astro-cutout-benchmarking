#!/usr/bin/env python3
"""Benchmark harness for subfits cutouts on Dug via sbatch + seff."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import pathlib
import re
import shlex
import shutil
import socket
import statistics
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


CARD_SIZE = 80
HEADER_BLOCK = 2880
DEFAULT_HEADER_BYTES = HEADER_BLOCK * 80


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_")


def ensure_dir(path: pathlib.Path) -> pathlib.Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def is_url(source: str) -> bool:
    return source.startswith("http://") or source.startswith("https://")


def flatten(prefix: str, data: Dict[str, Any]) -> Dict[str, Any]:
    flat: Dict[str, Any] = {}
    for key, value in data.items():
        name = f"{prefix}_{slugify(str(key))}" if prefix else slugify(str(key))
        if isinstance(value, dict):
            flat.update(flatten(name, value))
        elif isinstance(value, list):
            flat[name] = json.dumps(value, sort_keys=True)
        else:
            flat[name] = value
    return flat


def read_source_prefix(source: str, byte_count: int) -> bytes:
    if is_url(source):
        request = urllib.request.Request(
            source,
            headers={"Range": f"bytes=0-{byte_count - 1}"},
        )
        with urllib.request.urlopen(request) as response:
            return response.read(byte_count)

    with open(source, "rb") as handle:
        return handle.read(byte_count)


def parse_fits_header(source: str, byte_count: int = DEFAULT_HEADER_BYTES) -> Dict[str, Any]:
    raw = read_source_prefix(source, byte_count)
    if len(raw) < CARD_SIZE:
        raise ValueError(f"Could not read enough header bytes from {source}")

    cards: List[str] = []
    end_found = False
    for offset in range(0, len(raw) - CARD_SIZE + 1, CARD_SIZE):
        card = raw[offset : offset + CARD_SIZE].decode("ascii", errors="replace")
        cards.append(card)
        if card.startswith("END"):
            end_found = True
            break

    if not end_found:
        raise ValueError(
            f"FITS END card not found in the first {len(raw)} bytes of {source}. "
            "Increase the header read size or pass --pixel-filter explicitly."
        )

    values: Dict[str, str] = {}
    for card in cards:
        key = card[:8].strip()
        if not key or key == "END" or "=" not in card[:10]:
            continue
        raw_value = card[10:80]
        value = raw_value.split("/", 1)[0].strip()
        values[key] = value.strip().strip("'")

    try:
        bitpix = int(values["BITPIX"])
        naxis = int(values["NAXIS"])
    except KeyError as exc:
        raise ValueError(f"Missing required FITS keyword in header: {exc}") from exc

    dims: List[int] = []
    for axis in range(1, naxis + 1):
        key = f"NAXIS{axis}"
        if key not in values:
            raise ValueError(f"Missing required FITS keyword in header: {key}")
        dims.append(int(values[key]))

    return {
        "bitpix": bitpix,
        "naxis": naxis,
        "dims": dims,
        "bytes_per_pixel": max(1, abs(bitpix) // 8),
        "header_bytes_read": len(raw),
    }


def derive_centered_filter(header: Dict[str, Any], target_mib: int) -> Dict[str, Any]:
    dims = [int(value) for value in header["dims"]]
    bytes_per_pixel = int(header["bytes_per_pixel"])
    full_pixel_count = math.prod(dims)
    full_bytes = full_pixel_count * bytes_per_pixel
    target_bytes = max(1, int(target_mib) * 1024 * 1024)

    varying_axes = [idx for idx, dim in enumerate(dims) if dim > 1]
    if not varying_axes:
        varying_axes = list(range(len(dims)))

    if target_bytes >= full_bytes:
        lengths = list(dims)
    else:
        scale = (target_bytes / full_bytes) ** (1.0 / max(1, len(varying_axes)))
        lengths = []
        for dim in dims:
            if dim <= 1:
                lengths.append(1)
                continue
            length = int(round(dim * scale))
            lengths.append(max(1, min(dim, length)))

    bounds: List[Tuple[int, int]] = []
    for dim, length in zip(dims, lengths):
        start = ((dim - length) // 2) + 1
        end = start + length - 1
        bounds.append((start, end))

    pixel_filter = "[" + ",".join(f"{start}:{end}" for start, end in bounds) + "]"
    estimated_bytes = bytes_per_pixel * math.prod(end - start + 1 for start, end in bounds)

    return {
        "pixel_filter": pixel_filter,
        "target_mib": target_mib,
        "estimated_bytes": estimated_bytes,
        "estimated_mib": estimated_bytes / (1024 * 1024),
        "bounds": bounds,
    }


def derive_fraction_filter(
    header: Dict[str, Any],
    data_fraction: float,
    *,
    axes_mode: str = "xy",
    anchor: str = "start",
) -> Dict[str, Any]:
    if not (0.0 < data_fraction <= 1.0):
        raise ValueError("data_fraction must be within (0, 1]")

    dims = [int(value) for value in header["dims"]]
    bytes_per_pixel = int(header["bytes_per_pixel"])
    if axes_mode == "xy":
        axis_indices = [0, 1]
    elif axes_mode == "xyz":
        axis_indices = [0, 1]
        spectral_axis_index = -1
        for index in range(len(dims) - 1, 1, -1):
            if dims[index] > 1:
                spectral_axis_index = index
                break
        if spectral_axis_index != -1:
            axis_indices.append(spectral_axis_index)
    else:
        axis_indices = list(range(len(dims)))
    axis_indices = [index for index in axis_indices if index < len(dims) and dims[index] > 1]
    if not axis_indices:
        axis_indices = [index for index, dim in enumerate(dims) if dim > 1]
    if not axis_indices:
        axis_indices = list(range(len(dims)))

    if anchor == "middle":
        anchor = "center"

    axis_scale = data_fraction ** (1.0 / max(1, len(axis_indices)))
    bounds: List[Tuple[int, int]] = []
    actual_fraction = 1.0
    for index, dim in enumerate(dims):
        if index in axis_indices and dim > 1:
            length = max(1, min(dim, int(round(dim * axis_scale))))
        else:
            length = dim

        if anchor == "start":
            start = 1
        elif anchor == "center":
            start = ((dim - length) // 2) + 1
        elif anchor == "end":
            start = dim - length + 1
        else:
            raise ValueError(f"Unsupported anchor: {anchor}")

        end = start + length - 1
        bounds.append((start, end))
        actual_fraction *= (end - start + 1) / dim

    estimated_bytes = int(round(bytes_per_pixel * math.prod(dims) * actual_fraction))
    pixel_filter = "[" + ",".join(f"{start}:{end}" for start, end in bounds) + "]"

    return {
        "pixel_filter": pixel_filter,
        "data_fraction": data_fraction,
        "anchor": anchor,
        "axes_mode": axes_mode,
        "estimated_bytes": estimated_bytes,
        "estimated_mib": estimated_bytes / (1024 * 1024),
        "actual_fraction": actual_fraction,
        "bounds": bounds,
    }


def derive_xyz_middle_channels_filter(
    header: Dict[str, Any],
    data_fraction: float,
    *,
    anchor: str = "start",
    middle_channels: int = 2,
) -> Dict[str, Any]:
    _ = middle_channels
    return derive_fraction_filter(header, data_fraction, axes_mode="xyz", anchor=anchor)


def human_gib_approx(byte_count: int) -> str:
    gib = byte_count / (1024 ** 3)
    return f"~{int(round(gib))}GB"


def gib_to_bytes(gib: float) -> int:
    return int(round(gib * (1024 ** 3)))


def input_key_for_gib(gib: float) -> str:
    if float(int(gib)) == float(gib):
        return f"g{int(gib)}"
    return "g" + str(gib).replace(".", "p")


def cutout_size_label(byte_count: int, fraction: float, *, axes_mode: str = "xy") -> str:
    percent = int(round(fraction * 100))
    axes_text = "x/y only" if axes_mode == "xy" else "x/y/z"
    return f"{percent}% ({human_gib_approx(byte_count)}, {axes_text})"


def normalize_location_key(location_key: str) -> str:
    if location_key == "center":
        return "middle"
    return location_key


def location_anchor_for_filter(location_key: str) -> str:
    location_key = normalize_location_key(location_key)
    if location_key == "middle":
        return "center"
    return location_key


def location_label(location_key: str, *, axes_mode: str = "xy") -> str:
    location_key = normalize_location_key(location_key)
    axes_text = "x/y only" if axes_mode == "xy" else "x/y/z"
    return f"{location_key} ({axes_text})"


def location_title(location_key: str) -> str:
    return normalize_location_key(location_key).capitalize()


def stage_filename(source: str, label: str) -> str:
    stem = pathlib.Path(source).stem
    return f"{stem}_{label}.fits"


def cutpixels_temp_output_name(source_with_filter: str) -> str:
    last_dot = source_with_filter.rfind(".")
    last_slash = source_with_filter.rfind("/")
    last_open_bracket = source_with_filter.rfind("[")
    last_close_bracket = source_with_filter.rfind("]")
    pixel_ranges = source_with_filter[last_open_bracket + 1 : last_close_bracket]
    pixel_ranges = pixel_ranges.replace(":", "-").replace(",", "_")
    base = source_with_filter[last_slash + 1 : last_dot]
    return f"{base}_CUT_{pixel_ranges}.fits"


def read_key_value_file(path: pathlib.Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[slugify(key)] = value.strip()
    return values


def read_lscpu_model_name(path: pathlib.Path) -> str:
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if slugify(key) == "model_name":
            return value.strip()
    return ""


def find_first_value(row: Dict[str, Any], aliases: Sequence[str]) -> str:
    for alias in aliases:
        if alias in row and row[alias]:
            return str(row[alias])
    for key, value in row.items():
        if not value:
            continue
        for alias in aliases:
            if alias in key:
                return str(value)
    return ""


def stderr_tail(path: pathlib.Path, max_bytes: int = 4096) -> str:
    if not path.exists():
        return ""
    data = path.read_bytes()
    return data[-max_bytes:].decode("utf-8", errors="replace").strip()


def read_text_maybe(path: pathlib.Path, max_bytes: Optional[int] = None) -> str:
    if not path.exists() or not path.is_file():
        return ""
    data = path.read_bytes()
    if max_bytes is not None:
        data = data[-max_bytes:]
    return data.decode("utf-8", errors="replace")


def compact_text(text: str, max_chars: int = 800) -> str:
    collapsed = " | ".join(line.strip() for line in text.splitlines() if line.strip())
    if len(collapsed) <= max_chars:
        return collapsed
    return collapsed[: max_chars - 3] + "..."


def first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def categorize_error_text(text: str, *, default: str = "generic_error") -> str:
    lowered = text.lower()
    patterns = [
        ("glibc_", "glibc_version_mismatch"),
        ("future feature annotations is not defined", "python_too_old"),
        ("error while loading shared libraries", "missing_shared_library"),
        ("invalid account or account/partition combination", "invalid_slurm_account"),
        ("jobs cannot be submitted from your home directory", "slurm_home_submission_denied"),
        ("jobhelduser", "slurm_job_held"),
        ("special_exit", "slurm_special_exit"),
        ("unrecognized arguments:", "unexpected_cli_arguments"),
        ("can't open file", "runner_script_missing"),
        ("permission denied", "permission_denied"),
        ("davix/davix.hpp: no such file or directory", "missing_davix_header"),
        ("fitsio.h: no such file or directory", "missing_cfitsio_header"),
        ("ast.h: no such file or directory", "missing_ast_header"),
        ("no such file or directory", "missing_file_or_path"),
        ("memory allocation", "memory_allocation_failure"),
        ("killed", "process_killed"),
    ]
    for needle, category in patterns:
        if needle in lowered:
            return category
    return default


def output_dirs_for_session(session_dir: pathlib.Path, manifest_rows: Sequence[Dict[str, str]]) -> List[pathlib.Path]:
    output_dirs: Dict[str, pathlib.Path] = {}
    for row in manifest_rows:
        output_dir = str(row.get("output_dir", "")).strip()
        if output_dir:
            path = pathlib.Path(output_dir).resolve()
            output_dirs[str(path)] = path

    for child in session_dir.iterdir():
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name in {"seff", "sacct", "__pycache__"}:
            continue
        output_dirs[str(child.resolve())] = child.resolve()

    return [output_dirs[key] for key in sorted(output_dirs)]


def build_scan_context(
    session_dir: pathlib.Path,
    output_dir: pathlib.Path,
    manifest_row: Optional[Dict[str, str]],
) -> Dict[str, Any]:
    result_json_path = output_dir / "result.json"
    result_data: Dict[str, Any] = {}
    result_json_decode_error = ""
    if result_json_path.exists():
        try:
            result_data = json.loads(result_json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            result_json_decode_error = str(exc)

    benchmark = result_data.get("benchmark", {}) if isinstance(result_data, dict) else {}
    summary = result_data.get("summary", {}) if isinstance(result_data, dict) else {}
    slurm = result_data.get("slurm", {}) if isinstance(result_data, dict) else {}
    base_row: Dict[str, Any] = {
        "session_dir": str(session_dir),
        "session_name": session_dir.name,
        "output_dir": str(output_dir),
        "combo_name": output_dir.name,
        "submitted_at_utc": (manifest_row or {}).get("submitted_at_utc", ""),
        "job_id": (manifest_row or {}).get("job_id", "") or slurm.get("job_id", ""),
        "job_name": (manifest_row or {}).get("job_name", "") or slurm.get("job_name", ""),
        "tool": (manifest_row or {}).get("tool", ""),
        "source_name": (manifest_row or {}).get("source_name", ""),
        "mode": (manifest_row or {}).get("mode", "") or benchmark.get("mode", ""),
        "cutout_location": (manifest_row or {}).get("cutout_location", ""),
        "cutout_dimensions": (manifest_row or {}).get("cutout_dimensions", ""),
        "cpus": (manifest_row or {}).get("cpus", ""),
        "requested_memory": (manifest_row or {}).get("requested_memory", ""),
        "input_size_gib": (manifest_row or {}).get("input_size_gib", ""),
        "input_size_label": (manifest_row or {}).get("input_size_label", ""),
        "cutout_size_percent": (manifest_row or {}).get("cutout_size_percent", ""),
        "cutout_size_label": (manifest_row or {}).get("cutout_size_label", ""),
        "file_name": (manifest_row or {}).get("file_name", ""),
        "source": (manifest_row or {}).get("source", "") or benchmark.get("source", ""),
        "pixel_filter": (manifest_row or {}).get("pixel_filter", "") or benchmark.get("pixel_filter", ""),
        "notes": (manifest_row or {}).get("notes", ""),
        "result_json": str(result_json_path),
        "runner_successful_repeats": summary.get("successful_repeats", ""),
        "runner_failed_repeats": summary.get("failed_repeats", ""),
    }
    return {
        "result_json_path": result_json_path,
        "result_data": result_data,
        "result_json_decode_error": result_json_decode_error,
        "benchmark": benchmark,
        "summary": summary,
        "slurm": slurm,
        "base_row": base_row,
    }


def output_dir_node_name(output_dir: pathlib.Path, context: Optional[Dict[str, Any]] = None) -> str:
    if context is None:
        context = build_scan_context(output_dir.parent, output_dir, None)
    slurm = context.get("slurm", {})
    node_name = str(slurm.get("job_nodelist", "")).strip()
    if node_name:
        return node_name
    job_env = read_key_value_file(output_dir / "job-env.txt")
    return str(job_env.get("slurm_job_nodelist", "")).strip()


def result_error_rows_for_output_dir(
    session_dir: pathlib.Path,
    output_dir: pathlib.Path,
    manifest_row: Optional[Dict[str, str]],
    context: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    if context is None:
        context = build_scan_context(session_dir, output_dir, manifest_row)
    result_json_path = pathlib.Path(context["result_json_path"])
    result_data = context["result_data"]
    result_json_decode_error = str(context["result_json_decode_error"])
    summary = context["summary"]
    base_row = context["base_row"]

    error_rows: List[Dict[str, Any]] = []

    def add_error(
        *,
        error_source: str,
        log_path: pathlib.Path,
        error_text: str,
        error_category: Optional[str] = None,
        phase: str = "",
        repeat_index: str = "",
        worker_index: str = "",
        returncode: str = "",
    ) -> None:
        summary_text = first_nonempty_line(error_text)
        category = error_category or categorize_error_text(
            error_text,
            default="nonzero_returncode" if str(returncode) not in {"", "0"} else "generic_error",
        )
        error_rows.append(
            {
                **base_row,
                "error_source": error_source,
                "log_path": str(log_path),
                "phase": phase,
                "repeat_index": repeat_index,
                "worker_index": worker_index,
                "returncode": returncode,
                "error_category": category,
                "error_summary": summary_text or f"{error_source} reported an error",
                "error_excerpt": compact_text(error_text),
            }
        )

    if not result_json_path.exists() and row_has_execution_artifacts(output_dir):
        add_error(
            error_source="result_json_missing",
            log_path=result_json_path,
            error_text="result.json is missing but execution artifacts exist in the output directory",
            error_category="missing_result_json",
        )
    elif result_json_decode_error:
        add_error(
            error_source="result_json_invalid",
            log_path=result_json_path,
            error_text=result_json_decode_error,
            error_category="invalid_result_json",
        )

    for pattern, source_name in (("slurm-*.err", "slurm_stderr"), ("slurm-*.out", "slurm_stdout")):
        for log_path in sorted(output_dir.glob(pattern)):
            log_text = read_text_maybe(log_path, max_bytes=8192).strip()
            if log_text:
                add_error(
                    error_source=source_name,
                    log_path=log_path,
                    error_text=log_text,
                )

    worker_error_count = 0
    for phase_key in ("warmups", "repeats"):
        phase_rows = result_data.get(phase_key, []) if isinstance(result_data, dict) else []
        phase_name = "warmup" if phase_key == "warmups" else "repeat"
        for run in phase_rows:
            repeat_index = str(run.get("repeat_index", ""))
            if not run.get("success", True) and not run.get("workers"):
                add_error(
                    error_source=f"{phase_name}_summary",
                    log_path=result_json_path,
                    error_text=f"{phase_name} {repeat_index} failed with returncodes {run.get('returncodes', [])}",
                    error_category=f"{phase_name}_failed",
                    phase=phase_name,
                    repeat_index=repeat_index,
                )
            for worker in run.get("workers", []):
                worker_index = str(worker.get("worker_index", ""))
                returncode = str(worker.get("returncode", ""))
                stderr_path_text = str(worker.get("stderr_path", "")).strip()
                stderr_path = pathlib.Path(stderr_path_text) if stderr_path_text else output_dir
                stderr_text = read_text_maybe(stderr_path, max_bytes=8192).strip()
                if not stderr_text:
                    stderr_text = str(worker.get("stderr_tail", "")).strip()
                if returncode not in {"", "0"} or stderr_text:
                    worker_error_count += 1
                    add_error(
                        error_source=f"{phase_name}_worker_stderr",
                        log_path=stderr_path,
                        error_text=stderr_text or f"{phase_name} worker returned {returncode}",
                        phase=phase_name,
                        repeat_index=repeat_index,
                        worker_index=worker_index,
                        returncode=returncode,
                    )

    failed_repeats = summary.get("failed_repeats", 0) if isinstance(summary, dict) else 0
    try:
        failed_repeats_int = int(failed_repeats or 0)
    except (TypeError, ValueError):
        failed_repeats_int = 0
    if failed_repeats_int > 0 and worker_error_count == 0:
        add_error(
            error_source="runner_summary",
            log_path=result_json_path,
            error_text=f"result.json reports {failed_repeats_int} failed repeats",
            error_category="failed_repeats",
        )

    return error_rows


def summarize_run_for_output_dir(
    session_dir: pathlib.Path,
    output_dir: pathlib.Path,
    manifest_row: Optional[Dict[str, str]],
) -> Dict[str, Any]:
    context = build_scan_context(session_dir, output_dir, manifest_row)
    result_json_path = pathlib.Path(context["result_json_path"])
    result_data = context["result_data"]
    result_json_decode_error = str(context["result_json_decode_error"])
    summary = context["summary"]
    base_row = dict(context["base_row"])

    error_rows = result_error_rows_for_output_dir(
        session_dir,
        output_dir,
        manifest_row,
        context=context,
    )

    def parse_int(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    successful_repeats = parse_int(summary.get("successful_repeats", ""))
    failed_repeats = parse_int(summary.get("failed_repeats", ""))
    has_result_json = result_json_path.exists()
    has_execution_artifacts = row_has_execution_artifacts(output_dir)

    if result_json_decode_error:
        run_status = "invalid_result_json"
    elif not has_result_json and has_execution_artifacts:
        run_status = "incomplete"
    elif not has_result_json:
        run_status = "missing"
    elif (failed_repeats or 0) > 0:
        run_status = "failed"
    elif (successful_repeats or 0) > 0 and (failed_repeats or 0) == 0:
        run_status = "success"
    elif error_rows:
        run_status = "error"
    elif isinstance(result_data, dict) and result_data:
        run_status = "result_only"
    else:
        run_status = "unknown"

    distinct_categories: List[str] = []
    distinct_paths: List[str] = []
    seen_categories: Set[str] = set()
    seen_paths: Set[str] = set()
    for row in error_rows:
        category = str(row.get("error_category", "")).strip()
        if category and category not in seen_categories:
            seen_categories.add(category)
            distinct_categories.append(category)
        log_path = str(row.get("log_path", "")).strip()
        if log_path and log_path not in seen_paths:
            seen_paths.add(log_path)
            distinct_paths.append(log_path)

    primary_error = error_rows[0] if error_rows else {}

    return {
        **base_row,
        "run_status": run_status,
        "run_succeeded": "1" if run_status == "success" else "0",
        "has_result_json": "1" if has_result_json else "0",
        "has_execution_artifacts": "1" if has_execution_artifacts else "0",
        "error_count": len(error_rows),
        "error_categories": " | ".join(distinct_categories),
        "primary_error_category": str(primary_error.get("error_category", "")),
        "primary_error_source": str(primary_error.get("error_source", "")),
        "primary_error_summary": str(primary_error.get("error_summary", "")),
        "primary_error_excerpt": str(primary_error.get("error_excerpt", "")),
        "primary_log_path": str(primary_error.get("log_path", "")),
        "all_log_paths": " | ".join(distinct_paths),
    }


def run_once(
    command_builder: Any,
    parallel_workers: int,
    repeat_index: int,
    stderr_dir: pathlib.Path,
    *,
    output_dir: pathlib.Path,
    phase_name: str,
    tool: str,
) -> Dict[str, Any]:
    ensure_dir(stderr_dir)
    stderr_handles = []
    stderr_paths: List[pathlib.Path] = []
    output_paths: List[Optional[pathlib.Path]] = []
    processes = []

    started_at = utc_now()
    start = time.perf_counter()
    with open(os.devnull, "wb") as devnull:
        for worker in range(parallel_workers):
            stderr_path = stderr_dir / f"repeat_{repeat_index:03d}_worker_{worker:03d}.stderr"
            stderr_handle = open(stderr_path, "wb")
            stderr_handles.append(stderr_handle)
            stderr_paths.append(stderr_path)
            worker_output_path: Optional[pathlib.Path] = None
            if tool == "subfits":
                tool_output_dir = ensure_dir(output_dir / "tool_output" / phase_name)
                worker_output_path = tool_output_dir / f"repeat_{repeat_index:03d}_worker_{worker:03d}.fits"
                maybe_delete_file(worker_output_path)
            output_paths.append(worker_output_path)
            processes.append(
                subprocess.Popen(
                    list(command_builder(worker_output_path)),
                    stdout=devnull,
                    stderr=stderr_handle,
                )
            )

        returncodes: List[int] = []
        for process in processes:
            returncodes.append(process.wait())
    elapsed = time.perf_counter() - start

    for handle in stderr_handles:
        handle.close()

    for output_path in output_paths:
        if output_path is not None:
            maybe_delete_file(output_path)

    worker_results: List[Dict[str, Any]] = []
    for worker, (stderr_path, returncode, output_path) in enumerate(zip(stderr_paths, returncodes, output_paths)):
        worker_results.append(
            {
                "worker_index": worker,
                "returncode": returncode,
                "stderr_path": str(stderr_path),
                "stderr_tail": stderr_tail(stderr_path),
                "output_path": str(output_path) if output_path is not None else "",
            }
        )

    return {
        "repeat_index": repeat_index,
        "started_at_utc": started_at,
        "elapsed_seconds": elapsed,
        "parallel_workers": parallel_workers,
        "returncodes": returncodes,
        "success": all(code == 0 for code in returncodes),
        "workers": worker_results,
    }


def summarize_runs(
    runs: Sequence[Dict[str, Any]],
    estimated_bytes_per_worker: int,
    parallel_workers: int,
) -> Dict[str, Any]:
    successful = [run["elapsed_seconds"] for run in runs if run.get("success")]
    total_bytes = estimated_bytes_per_worker * parallel_workers
    summary: Dict[str, Any] = {
        "successful_repeats": len(successful),
        "failed_repeats": len(runs) - len(successful),
        "estimated_cutout_bytes_per_worker": estimated_bytes_per_worker,
        "estimated_total_bytes_per_repeat": total_bytes,
    }

    if successful:
        summary.update(
            {
                "min_seconds": min(successful),
                "max_seconds": max(successful),
                "mean_seconds": statistics.mean(successful),
                "median_seconds": statistics.median(successful),
                "median_throughput_mib_s": (total_bytes / (1024 * 1024)) / statistics.median(successful),
            }
        )

    return summary


def write_json(path: pathlib.Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def manifest_fieldnames() -> List[str]:
    return [
        "submitted_at_utc",
        "job_id",
        "job_name",
        "tool",
        "source_name",
        "mode",
        "source",
        "extnum",
        "cpus",
        "requested_memory",
        "parallel_workers",
        "input_size_bytes",
        "input_size_label",
        "input_size_gib",
        "file_name",
        "target_mib",
        "pixel_filter",
        "cutout_size_bytes",
        "cutout_size_label",
        "cutout_size_percent",
        "cutout_dimensions",
        "cutout_location",
        "notes",
        "output_dir",
        "result_json",
        "slurm_stdout",
        "slurm_stderr",
    ]


def write_manifest_row(path: pathlib.Path, row: Dict[str, Any]) -> None:
    rows: List[Dict[str, Any]] = []
    if path.exists():
        rows = [dict(existing) for existing in load_manifest(path)]

    replaced = False
    row_output_dir = row.get("output_dir", "")
    if row_output_dir:
        for index, existing in enumerate(rows):
            if existing.get("output_dir", "") == row_output_dir:
                rows[index] = row
                replaced = True
                break
    if not replaced:
        rows.append(row)

    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=manifest_fieldnames(), delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def dedupe_manifest_rows(rows: Sequence[Dict[str, str]]) -> List[Dict[str, str]]:
    latest_by_key: Dict[str, Dict[str, str]] = {}
    anonymous_index = 0
    for row in rows:
        key = str(row.get("output_dir", "")).strip()
        if not key:
            anonymous_index += 1
            key = f"__row_{anonymous_index}"
        if key in latest_by_key:
            del latest_by_key[key]
        latest_by_key[key] = dict(row)
    return list(latest_by_key.values())


def load_manifest(path: pathlib.Path) -> List[Dict[str, str]]:
    with open(path, "r", newline="", encoding="utf-8") as handle:
        return dedupe_manifest_rows(list(csv.DictReader(handle, delimiter="\t")))


def shell_join(parts: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def tool_label(tool: str, mode: str) -> str:
    if tool == "subfits":
        return "subfits"
    if mode == "imcopydav":
        return "vlkb imcopydav"
    return "vlkb imcopy"


def normalize_tool_and_mode(tool: str, mode: str) -> Tuple[str, str]:
    tool_name = str(tool).strip().lower() or "vlkb"
    mode_name = str(mode).strip().lower() or "imcopy"
    if tool_name == "subfits":
        return "subfits", "subfits"
    if mode_name not in {"imcopy", "imcopydav"}:
        raise SystemExit(f"Unsupported vlkb mode: {mode}")
    return "vlkb", mode_name


def subfits_pixel_arg(pixel_filter: str) -> str:
    text = str(pixel_filter).strip()
    if not (text.startswith("[") and text.endswith("]")):
        raise SystemExit(f"Unsupported FITS pixel filter for subfits: {pixel_filter}")
    values: List[str] = []
    for axis_range in text[1:-1].split(","):
        parts = axis_range.split(":")
        if len(parts) != 2:
            raise SystemExit(f"Unsupported FITS pixel filter for subfits: {pixel_filter}")
        values.extend(part.strip() for part in parts)
    return ",".join(values)


def build_benchmark_command(
    *,
    tool: str,
    binary: str,
    mode: str,
    source: str,
    extnum: int,
    pixel_filter: str,
    output_path: Optional[pathlib.Path] = None,
) -> List[str]:
    tool_name, mode_name = normalize_tool_and_mode(tool, mode)
    if tool_name == "subfits":
        if output_path is None:
            raise SystemExit("subfits requires an output_path for benchmark execution")
        return [
            "python3",
            binary,
            "-i",
            source,
            "-o",
            str(output_path),
            "-p",
            subfits_pixel_arg(pixel_filter),
        ]
    return [
        binary,
        mode_name,
        source,
        str(extnum),
        pixel_filter,
    ]


def build_benchmark_command_shell(
    *,
    tool: str,
    binary: str,
    mode: str,
    source: str,
    extnum: int,
    pixel_filter: str,
) -> str:
    tool_name, mode_name = normalize_tool_and_mode(tool, mode)
    if tool_name == "subfits":
        command = build_benchmark_command(
            tool=tool_name,
            binary=binary,
            mode=mode_name,
            source=source,
            extnum=extnum,
            pixel_filter=pixel_filter,
            output_path=pathlib.Path("<output>.fits"),
        )
        return shell_join(command)
    command = build_benchmark_command(
        tool=tool_name,
        binary=binary,
        mode=mode_name,
        source=source,
        extnum=extnum,
        pixel_filter=pixel_filter,
    )
    return shell_join(command) + " > /dev/null"


def run_cmd(command: Sequence[str], cwd: Optional[pathlib.Path] = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            cwd=str(cwd) if cwd is not None else None,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(
            args=list(command),
            returncode=127,
            stdout="",
            stderr=f"command not found: {command[0]}",
        )


def resolve_submit_dir(args: argparse.Namespace) -> pathlib.Path:
    raw_submit_dir = getattr(args, "submit_dir", None)
    submit_dir = pathlib.Path(raw_submit_dir).resolve() if raw_submit_dir else pathlib.Path.cwd().resolve()
    home_dir = pathlib.Path.home().resolve()
    try:
        submit_dir.relative_to(home_dir)
    except ValueError:
        return submit_dir
    raise SystemExit(
        "Dug does not allow sbatch submissions from your home directory. "
        "Use --submit-dir on a scratch/project filesystem, and keep --stage-dir and --results-root there too."
    )


def normalize_seff_key(key: str, parsed: Dict[str, str]) -> str:
    parsed_key = slugify(key)
    if "%" in key or "percent" in key.lower():
        if not parsed_key.endswith("_percent"):
            parsed_key = f"{parsed_key}_percent"
    if parsed_key in parsed:
        suffix = 2
        while f"{parsed_key}_{suffix}" in parsed:
            suffix += 1
        parsed_key = f"{parsed_key}_{suffix}"
    return parsed_key


def seff_parse(raw_text: str) -> Dict[str, str]:
    parsed: Dict[str, str] = {}
    for line in raw_text.splitlines():
        if not re.match(r"^\s*[A-Za-z][^:]*:\s*.*$", line):
            continue
        key, value = line.split(":", 1)
        parsed_key = normalize_seff_key(key, parsed)
        parsed[parsed_key] = value.strip()

    nonempty_lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    for index in range(len(nonempty_lines) - 1):
        header_parts = re.split(r"\s{2,}", nonempty_lines[index])
        value_parts = re.split(r"\s{2,}", nonempty_lines[index + 1])
        if len(header_parts) < 2 or len(header_parts) != len(value_parts):
            continue
        if "JobID" not in header_parts:
            continue
        for key, value in zip(header_parts, value_parts):
            parsed_key = normalize_seff_key(key, parsed)
            parsed[parsed_key] = value.strip()
        break
    return parsed


SACCT_FIELDS = [
    "JobIDRaw",
    "JobID",
    "JobName",
    "State",
    "Elapsed",
    "TotalCPU",
    "MaxRSS",
    "AveRSS",
    "MaxVMSize",
    "ReqMem",
    "AllocCPUS",
    "NodeList",
]


def sacct_collect_for_job(job_id: str, sacct_command: str) -> Tuple[Dict[str, str], str]:
    completed = run_cmd(
        [
            sacct_command,
            "-j",
            f"{job_id},{job_id}.batch,{job_id}.extern",
            "--format",
            ",".join(SACCT_FIELDS),
            "-P",
            "-n",
        ]
    )
    raw_text = (completed.stdout + completed.stderr).strip()
    if completed.returncode != 0 or not raw_text:
        return (
            {
                "sacct_returncode": str(completed.returncode),
            },
            raw_text,
        )

    job_row: Dict[str, str] = {}
    batch_row: Dict[str, str] = {}
    extern_row: Dict[str, str] = {}
    for line in raw_text.splitlines():
        parts = line.split("|")
        if len(parts) != len(SACCT_FIELDS):
            continue
        parsed = {slugify(key): value.strip() for key, value in zip(SACCT_FIELDS, parts)}
        job_id_text = parsed.get("jobid", "")
        if job_id_text == job_id:
            job_row = parsed
        elif job_id_text.endswith(".batch"):
            batch_row = parsed
        elif job_id_text.endswith(".extern"):
            extern_row = parsed

    merged: Dict[str, str] = {
        "sacct_returncode": str(completed.returncode),
    }
    for prefix, parsed_row in (("sacct_job", job_row), ("sacct_batch", batch_row), ("sacct_extern", extern_row)):
        for key, value in parsed_row.items():
            if key == "jobid":
                continue
            merged[f"{prefix}_{key}"] = value
    return merged, raw_text


def collect_rows(manifest_path: pathlib.Path, seff_command: str, sacct_command: str) -> List[Dict[str, Any]]:
    rows = load_manifest(manifest_path)
    session_dir = manifest_path.parent
    seff_dir = ensure_dir(session_dir / "seff")
    sacct_dir = ensure_dir(session_dir / "sacct")

    collected: List[Dict[str, Any]] = []
    for row in rows:
        merged: Dict[str, Any] = dict(row)
        output_dir = pathlib.Path(row["output_dir"])
        result_json = pathlib.Path(row["result_json"])
        if result_json.exists():
            result_data = json.loads(result_json.read_text(encoding="utf-8"))
            merged.update(flatten("runner_summary", result_data.get("summary", {})))
            merged.update(flatten("runner_benchmark", result_data.get("benchmark", {})))
            if result_data.get("fits_header"):
                merged.update(flatten("fits_header", result_data["fits_header"]))

        job_env = read_key_value_file(output_dir / "job-env.txt")
        if job_env:
            merged.update({f"jobenv_{key}": value for key, value in job_env.items()})
        lscpu_model_name = read_lscpu_model_name(output_dir / "lscpu.txt")
        if lscpu_model_name:
            merged["lscpu_model_name"] = lscpu_model_name

        seff_completed = run_cmd([seff_command, row["job_id"]])
        seff_raw = (seff_completed.stdout + seff_completed.stderr).strip()
        seff_path = seff_dir / f"{row['job_id']}.txt"
        seff_path.write_text(seff_raw + ("\n" if seff_raw else ""), encoding="utf-8")
        merged["seff_raw_path"] = str(seff_path)
        merged["seff_returncode"] = seff_completed.returncode
        merged.update({f"seff_{key}": value for key, value in seff_parse(seff_raw).items()})

        sacct_fields, sacct_raw = sacct_collect_for_job(row["job_id"], sacct_command)
        sacct_path = sacct_dir / f"{row['job_id']}.txt"
        sacct_path.write_text(sacct_raw + ("\n" if sacct_raw else ""), encoding="utf-8")
        merged["sacct_raw_path"] = str(sacct_path)
        merged.update(sacct_fields)
        collected.append(merged)

    return collected


def canonical_wall_time(row: Dict[str, Any]) -> str:
    return find_first_value(
        row,
        [
            "sacct_batch_elapsed",
            "sacct_job_elapsed",
            "seff_wall_time_elapsed",
            "seff_job_wall_clock_time",
            "seff_wall_clock_time",
            "seff_elapsed",
            "seff_walltime",
        ],
    ) or str(row.get("runner_summary_median_seconds", ""))


def canonical_peak_memory(row: Dict[str, Any]) -> str:
    return find_first_value(
        row,
        [
            "sacct_batch_maxrss",
            "seff_peak_memory",
            "seff_memory_utilized",
            "seff_max_rss",
            "seff_maxrss",
            "sacct_batch_maxvmsize",
        ],
    )


def canonical_max_vmsize(row: Dict[str, Any]) -> str:
    return find_first_value(row, ["sacct_batch_maxvmsize"])


def canonical_max_rss(row: Dict[str, Any]) -> str:
    return find_first_value(row, ["sacct_batch_maxrss", "seff_max_rss", "seff_maxrss"])


def parse_slurm_elapsed_seconds(value: str) -> Optional[float]:
    text = str(value).strip()
    if not text:
        return None
    days = 0
    if "-" in text:
        days_text, text = text.split("-", 1)
        days = int(days_text)
    parts = text.split(":")
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours = "0"
        minutes, seconds = parts
    else:
        return None
    return (
        days * 86400
        + int(hours) * 3600
        + int(minutes) * 60
        + float(seconds)
    )


def canonical_cpu_usage_percent(row: Dict[str, Any]) -> str:
    total_cpu_seconds = parse_slurm_elapsed_seconds(find_first_value(row, ["sacct_batch_totalcpu", "sacct_job_totalcpu"]))
    elapsed_seconds = parse_slurm_elapsed_seconds(find_first_value(row, ["sacct_batch_elapsed", "sacct_job_elapsed", "seff_elapsed"]))
    alloc_cpus_text = find_first_value(row, ["sacct_batch_alloccpus", "sacct_job_alloccpus", "cpus"])
    try:
        alloc_cpus = int(float(str(alloc_cpus_text)))
    except (TypeError, ValueError):
        alloc_cpus = 0
    if total_cpu_seconds and elapsed_seconds and alloc_cpus > 0 and elapsed_seconds > 0:
        percent = max(0.0, min(100.0, (total_cpu_seconds / (elapsed_seconds * alloc_cpus)) * 100.0))
        return f"{percent:.1f}%"

    percent_text = find_first_value(
        row,
        [
            "seff_cpuusage_percent",
            "seff_cpu_usage_percent",
            "seff_cpu_efficiency",
            "seff_cpu_efficiency_percent",
        ],
    )
    if not percent_text:
        return ""
    try:
        percent_value = float(percent_text.replace("%", "").strip())
    except ValueError:
        return percent_text
    if alloc_cpus > 0 and percent_value > 100.0:
        percent_value = percent_value / alloc_cpus
    percent_value = max(0.0, min(100.0, percent_value))
    return f"{percent_value:.1f}%"


def canonical_cpu_usage_display(row: Dict[str, Any]) -> str:
    return canonical_cpu_usage_percent(row)


def canonical_node_type(row: Dict[str, Any]) -> str:
    raw_value = find_first_value(
        row,
        [
            "seff_node_type",
            "seff_nodetype",
            "seff_node",
        ],
    ) or str(row.get("lscpu_model_name", "")) or str(row.get("jobenv_slurm_partition", ""))
    text = str(raw_value).strip()
    lowered = text.lower()
    if "xeon phi" in lowered or "7210" in lowered or "7250" in lowered or "knl" in lowered:
        return "KNL"
    return text


def canonical_runner_succeeded(row: Dict[str, Any]) -> bool:
    successful_repeats = row.get("runner_summary_successful_repeats", "")
    failed_repeats = row.get("runner_summary_failed_repeats", "")
    median_seconds = row.get("runner_summary_median_seconds", "")
    try:
        successful_value = int(successful_repeats)
    except (TypeError, ValueError):
        successful_value = 0
    try:
        failed_value = int(failed_repeats)
    except (TypeError, ValueError):
        failed_value = 0
    return successful_value > 0 and failed_value == 0 and str(median_seconds) not in {"", "None"}


def enrich_collected_rows(collected: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    enriched: List[Dict[str, Any]] = []
    for row in collected:
        merged = dict(row)
        merged["wall_time_elapsed"] = canonical_wall_time(row)
        merged["peak_memory"] = canonical_peak_memory(row)
        merged["max_vmsize"] = canonical_max_vmsize(row)
        merged["max_rss"] = canonical_max_rss(row)
        merged["cpu_usage_percent"] = canonical_cpu_usage_percent(row)
        merged["cpu_usage_display"] = canonical_cpu_usage_display(row)
        merged["node_type"] = canonical_node_type(row)
        merged["runner_succeeded"] = canonical_runner_succeeded(row)
        enriched.append(merged)
    return enriched


def write_tsv(path: pathlib.Path, rows: Sequence[Dict[str, Any]], preferred: Optional[Sequence[str]] = None) -> None:
    all_columns = sorted({key for row in rows for key in row.keys()})
    ordered: List[str] = []
    for column in (preferred or []):
        if column in all_columns and column not in ordered:
            ordered.append(column)
    for column in manifest_fieldnames():
        if column in all_columns and column not in ordered:
            ordered.append(column)
    for column in all_columns:
        if column not in ordered:
            ordered.append(column)

    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ordered, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def choose_summary_columns(rows: Sequence[Dict[str, Any]]) -> List[str]:
    preferred = [
        "job_id",
        "source_name",
        "mode",
        "cpus",
        "parallel_workers",
        "target_mib",
        "pixel_filter",
        "runner_summary_median_seconds",
        "runner_summary_median_throughput_mib_s",
    ]

    seff_columns = []
    for key in sorted({key for row in rows for key in row.keys() if key.startswith("seff_")}):
        if any(token in key for token in ("cpu", "mem", "wall", "rss", "read", "write", "state", "exit")):
            seff_columns.append(key)
    return [column for column in preferred if column in {key for row in rows for key in row.keys()}] + seff_columns[:8]


def format_table(rows: Sequence[Dict[str, Any]], columns: Sequence[str]) -> str:
    if not rows:
        return "No rows.\n"
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        values = []
        for column in columns:
            value = row.get(column, "")
            if isinstance(value, float):
                values.append(f"{value:.3f}")
            else:
                values.append(str(value))
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines) + "\n"


def add_submit_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("submit", help="Submit a benchmark sweep via sbatch.")
    parser.add_argument("--binary", required=True, help="Path to the vlkb binary on Dug.")
    parser.add_argument("--local-source", help="Local FITS path to benchmark with imcopy.")
    parser.add_argument("--remote-source", help="HTTP(S) FITS URL to benchmark with imcopydav.")
    parser.add_argument("--cpus", nargs="+", required=True, type=int, help="Values for --cpus-per-task.")
    parser.add_argument("--target-mib", nargs="+", type=int, help="Target output sizes in MiB.")
    parser.add_argument(
        "--pixel-filter",
        action="append",
        help="Explicit FITS pixel filter, e.g. [1:512,1:512,1:32]. May be repeated.",
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--extnum", type=int, default=0)
    parser.add_argument(
        "--worker-strategy",
        choices=("one", "match-cpus", "both"),
        default="both",
        help="Whether each job runs 1 worker, cpus-per-task workers, or both variants.",
    )
    parser.add_argument(
        "--results-root",
        default=str(pathlib.Path("benchmarks") / "dug" / "results"),
        help="Parent directory for the benchmark session.",
    )
    parser.add_argument("--job-name-prefix", default="vlkb-cutout")
    parser.add_argument(
        "--env-script",
        help="Shell script sourced by the sbatch wrapper before running the benchmark.",
    )
    parser.add_argument("--time", help="SBATCH --time value, e.g. 02:00:00.")
    parser.add_argument("--partition", help="SBATCH --partition value.")
    parser.add_argument("--account", help="SBATCH --account value.")
    parser.add_argument("--qos", help="SBATCH --qos value.")
    parser.add_argument("--constraint", help="SBATCH --constraint value.")
    parser.add_argument("--reservation", help="SBATCH --reservation value.")
    parser.add_argument("--mem", help="SBATCH --mem value.")
    parser.add_argument(
        "--sbatch-arg",
        action="append",
        default=[],
        help="Extra raw sbatch option, for example --sbatch-arg=--exclusive.",
    )
    parser.add_argument("--job-script", help="Path to the sbatch wrapper script.")
    parser.add_argument(
        "--submit-dir",
        help="Directory to use as the sbatch submission working directory. Must not be under $HOME on Dug.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print sbatch commands without submitting.")
    parser.set_defaults(func=cmd_submit)


def add_run_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("run", help="Run one benchmark workload inside an sbatch job.")
    parser.add_argument("--tool", choices=("vlkb", "subfits"), default="vlkb")
    parser.add_argument("--binary", required=True, help="Path to the benchmark executable or script.")
    parser.add_argument("--mode", default="imcopy", choices=("imcopy", "imcopydav", "subfits"))
    parser.add_argument("--source", required=True, help="Local path or URL of the FITS cube.")
    parser.add_argument("--extnum", type=int, default=0)
    parser.add_argument("--pixel-filter", help="Explicit FITS pixel filter.")
    parser.add_argument("--target-mib", type=int, help="Target output size in MiB.")
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--parallel-workers", type=int, default=1)
    parser.add_argument("--output-dir", required=True, help="Per-job output directory.")
    parser.add_argument("--label", default="")
    parser.add_argument("--env-script", help="Recorded for provenance; sourced by the sbatch wrapper if used.")
    parser.set_defaults(func=cmd_run)


def add_collect_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("collect", help="Collect result JSON + seff output into CSV/Markdown.")
    parser.add_argument("--manifest", required=True, help="Path to manifest.tsv created by submit.")
    parser.add_argument("--seff-command", default="seff")
    parser.add_argument("--sacct-command", default="sacct")
    parser.add_argument("--summary-csv", help="Optional output CSV path.")
    parser.add_argument("--summary-md", help="Optional output Markdown path.")
    parser.set_defaults(func=cmd_collect)


def add_scan_errors_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "scan-errors",
        help="Scan a Dug benchmark session and emit one CSV row per run with condensed error details.",
    )
    parser.add_argument("--manifest", help="Path to manifest.tsv for the session.")
    parser.add_argument("--session-dir", help="Path to the benchmark session directory.")
    parser.add_argument("--output-csv", help="Optional output CSV path. Defaults to <session>/error_scan.csv.")
    parser.set_defaults(func=cmd_scan_errors)


def add_stage_start_matrix_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "stage-start-matrix",
        help="Create the smaller local start-of-cube x/y-only staged inputs used by the SB75060 matrix.",
    )
    parser.add_argument("--binary", required=True, help="Path to the vlkb binary.")
    parser.add_argument("--source", required=True, help="Local FITS path to the full input cube.")
    parser.add_argument(
        "--stage-dir",
        default=str(pathlib.Path("benchmarks") / "dug" / "staged"),
        help="Directory where staged FITS inputs are written.",
    )
    parser.add_argument("--force", action="store_true", help="Recreate staged inputs even if they already exist.")
    parser.set_defaults(func=cmd_stage_start_matrix)


def add_submit_start_matrix_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "submit-start-matrix",
        help="Submit the exact start-of-cube x/y-only benchmark matrix requested for SB75060.",
    )
    parser.add_argument("--binary", required=True, help="Path to the vlkb binary on Dug.")
    parser.add_argument("--source", required=True, help="Local FITS path to the full input cube.")
    parser.add_argument(
        "--stage-dir",
        default=str(pathlib.Path("benchmarks") / "dug" / "staged"),
        help="Directory containing staged FITS inputs.",
    )
    parser.add_argument(
        "--results-root",
        default=str(pathlib.Path("benchmarks") / "dug" / "results"),
        help="Parent directory for the benchmark session.",
    )
    parser.add_argument("--job-name-prefix", default="vlkb-startxy")
    parser.add_argument(
        "--env-script",
        help="Shell script sourced by the sbatch wrapper before running the benchmark.",
    )
    parser.add_argument("--time", help="SBATCH --time value, e.g. 08:00:00.")
    parser.add_argument("--partition", help="SBATCH --partition value.")
    parser.add_argument("--account", help="SBATCH --account value.")
    parser.add_argument("--qos", help="SBATCH --qos value.")
    parser.add_argument("--constraint", help="SBATCH --constraint value.")
    parser.add_argument("--reservation", help="SBATCH --reservation value.")
    parser.add_argument("--mem-big", default="8G", help="Actual SBATCH memory value for the matrix rows labelled 4G+.")
    parser.add_argument("--mem-small", default="4G", help="Actual SBATCH memory value for the matrix rows labelled 4G.")
    parser.add_argument(
        "--sbatch-arg",
        action="append",
        default=[],
        help="Extra raw sbatch option, for example --sbatch-arg=--exclusive.",
    )
    parser.add_argument("--job-script", help="Path to the sbatch wrapper script.")
    parser.add_argument(
        "--submit-dir",
        help="Directory to use as the sbatch submission working directory. Must not be under $HOME on Dug.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.set_defaults(func=cmd_submit_start_matrix)


def add_collect_start_matrix_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "collect-start-matrix",
        help="Collect the start-of-cube benchmark matrix into the requested table layout.",
    )
    parser.add_argument("--manifest", required=True, help="Path to manifest.tsv created by submit-start-matrix.")
    parser.add_argument("--seff-command", default="seff")
    parser.add_argument("--sacct-command", default="sacct")
    parser.add_argument("--summary-csv", help="Optional output CSV path.")
    parser.add_argument("--summary-md", help="Optional output Markdown path.")
    parser.set_defaults(func=cmd_collect_start_matrix)


def add_run_location_matrix_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "run-location-matrix",
        help="Run the full SB75060 matrix for start/middle/end sequentially with staging, collection, and cleanup.",
    )
    parser.add_argument("--tool", choices=("vlkb", "subfits"), default="vlkb")
    parser.add_argument("--binary", required=True, help="Path to the benchmark executable or script on Dug.")
    parser.add_argument(
        "--stage-binary",
        help="Path to the vlkb binary used for cutpixels staging. Defaults to --binary when --tool=vlkb.",
    )
    parser.add_argument("--source", required=True, help="Local FITS path to the full input cube.")
    parser.add_argument(
        "--stage-dir",
        default=str(pathlib.Path("benchmarks") / "dug" / "staged"),
        help="Directory used for temporary staged FITS inputs.",
    )
    parser.add_argument(
        "--results-root",
        default=str(pathlib.Path("benchmarks") / "dug" / "results"),
        help="Parent directory for the benchmark session.",
    )
    parser.add_argument(
        "--session-dir",
        help="Existing or explicit session directory to resume/use instead of creating a new timestamped one.",
    )
    parser.add_argument(
        "--locations",
        nargs="+",
        default=["start", "middle", "end"],
        help="Cutout locations to run. Defaults to start middle end.",
    )
    parser.add_argument("--job-name-prefix", default="subfits-locxy")
    parser.add_argument(
        "--env-script",
        help="Shell script sourced by the sbatch wrapper before running each benchmark.",
    )
    parser.add_argument("--time", help="SBATCH --time value, e.g. 08:00:00.")
    parser.add_argument("--partition", help="SBATCH --partition value.")
    parser.add_argument("--account", help="SBATCH --account value.")
    parser.add_argument("--qos", help="SBATCH --qos value.")
    parser.add_argument("--constraint", help="SBATCH --constraint value.")
    parser.add_argument("--reservation", help="SBATCH --reservation value.")
    parser.add_argument("--mem-big", default="8G", help="Actual SBATCH memory value for the rows labelled 4G+.")
    parser.add_argument("--mem-small", default="4G", help="Actual SBATCH memory value for the rows labelled 4G.")
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seff-command", default="seff")
    parser.add_argument("--sacct-command", default="sacct")
    parser.add_argument(
        "--matrix-cpus",
        nargs="+",
        type=int,
        help="Optional custom local matrix CPU counts. If set, run the cross-product grid instead of the default 9-case matrix.",
    )
    parser.add_argument(
        "--matrix-input-gib",
        nargs="+",
        type=float,
        help="Optional custom local matrix staged input sizes in GiB, for example --matrix-input-gib 187 94 47 24.",
    )
    parser.add_argument(
        "--matrix-cut-fractions",
        nargs="+",
        type=float,
        help="Optional custom local matrix cutout fractions, for example --matrix-cut-fractions 0.1 0.2 0.5 0.7.",
    )
    parser.add_argument(
        "--matrix-dimensions",
        nargs="+",
        choices=("xy", "xyz"),
        help="Optional cutout dimension modes for the custom grid. Defaults to xy. xyz means scaling x, y, and the spectral axis together to preserve the requested total data fraction.",
    )
    parser.add_argument(
        "--xyz-middle-channels",
        type=int,
        default=2,
        help="Deprecated compatibility option. xyz cutouts now scale x, y, and spectral dimensions proportionally, so this value is ignored.",
    )
    parser.add_argument(
        "--matrix-mem",
        help="Optional SBATCH --mem value to use for every custom local matrix case.",
    )
    parser.add_argument(
        "--matrix-memory-label",
        help="Optional display label for Requested Memory in the custom matrix. Defaults to --matrix-mem.",
    )
    parser.add_argument(
        "--case-index",
        nargs="+",
        type=int,
        help="Optional 1-based matrix case indices to run, e.g. --case-index 7 8 9.",
    )
    parser.add_argument(
        "--force-rerun",
        action="store_true",
        help="Rerun the selected cases even if their output directories already exist, replacing manifest rows by output_dir.",
    )
    parser.add_argument(
        "--rerun-failed-only",
        action="store_true",
        help="Only rerun selected rows whose prior result is missing, incomplete, or has failed repeats; successful rows are skipped. When resuming an existing session, the rerun automatically prefers nodes that already succeeded in that session and excludes nodes that showed glibc mismatches, unless you set --sbatch-arg=--exclude=... or --sbatch-arg=--nodelist=....",
    )
    parser.add_argument(
        "--sbatch-arg",
        action="append",
        default=[],
        help="Extra raw sbatch option, for example --sbatch-arg=--exclusive.",
    )
    parser.add_argument("--job-script", help="Path to the sbatch wrapper script.")
    parser.add_argument(
        "--submit-dir",
        help="Directory to use as the sbatch submission working directory. Must not be under $HOME on Dug.",
    )
    parser.add_argument(
        "--keep-staged",
        action="store_true",
        help="Keep staged FITS inputs after their last dependent test instead of deleting them.",
    )
    parser.set_defaults(func=cmd_run_location_matrix)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_submit_parser(subparsers)
    add_run_parser(subparsers)
    add_collect_parser(subparsers)
    add_scan_errors_parser(subparsers)
    add_stage_start_matrix_parser(subparsers)
    add_submit_start_matrix_parser(subparsers)
    add_collect_start_matrix_parser(subparsers)
    add_run_location_matrix_parser(subparsers)
    return parser.parse_args(argv)


def cmd_run(args: argparse.Namespace) -> int:
    if not args.pixel_filter and args.target_mib is None:
        raise SystemExit("run requires either --pixel-filter or --target-mib")
    if args.pixel_filter and args.target_mib is not None:
        raise SystemExit("run accepts only one of --pixel-filter or --target-mib")
    if args.parallel_workers < 1:
        raise SystemExit("--parallel-workers must be >= 1")
    tool_name, mode_name = normalize_tool_and_mode(args.tool, args.mode)

    output_dir = ensure_dir(pathlib.Path(args.output_dir).resolve())
    header = parse_fits_header(args.source) if args.target_mib is not None else None
    workload = (
        derive_centered_filter(header, args.target_mib)
        if header is not None
        else {
            "pixel_filter": args.pixel_filter,
            "target_mib": None,
            "estimated_bytes": None,
            "estimated_mib": None,
            "bounds": None,
        }
    )

    command_builder = lambda output_path: build_benchmark_command(
        tool=tool_name,
        binary=args.binary,
        mode=mode_name,
        source=args.source,
        extnum=args.extnum,
        pixel_filter=workload["pixel_filter"],
        output_path=output_path,
    )
    command_shell = build_benchmark_command_shell(
        tool=tool_name,
        binary=args.binary,
        mode=mode_name,
        source=args.source,
        extnum=args.extnum,
        pixel_filter=workload["pixel_filter"],
    )

    warmup_results = []
    for index in range(args.warmup):
        warmup_results.append(
            run_once(
                command_builder,
                args.parallel_workers,
                index,
                output_dir / "stderr" / "warmup",
                output_dir=output_dir,
                phase_name="warmup",
                tool=tool_name,
            )
        )

    repeat_results = []
    for index in range(args.repeats):
        repeat_results.append(
            run_once(
                command_builder,
                args.parallel_workers,
                index,
                output_dir / "stderr" / "repeats",
                output_dir=output_dir,
                phase_name="repeats",
                tool=tool_name,
            )
        )

    summary = summarize_runs(
        repeat_results,
        int(workload["estimated_bytes"] or 0),
        args.parallel_workers,
    )

    result = {
        "created_at_utc": utc_now(),
        "host": {
            "hostname": socket.gethostname(),
            "cwd": os.getcwd(),
        },
        "slurm": {
            "job_id": os.environ.get("SLURM_JOB_ID", ""),
            "job_name": os.environ.get("SLURM_JOB_NAME", ""),
            "cpus_per_task": os.environ.get("SLURM_CPUS_PER_TASK", ""),
            "job_nodelist": os.environ.get("SLURM_JOB_NODELIST", ""),
        },
        "benchmark": {
            "label": args.label,
            "tool": tool_name,
            "binary": args.binary,
            "mode": mode_name,
            "source": args.source,
            "source_kind": "remote" if is_url(args.source) else "local",
            "extnum": args.extnum,
            "pixel_filter": workload["pixel_filter"],
            "target_mib": workload["target_mib"],
            "parallel_workers": args.parallel_workers,
            "warmup": args.warmup,
            "repeats": args.repeats,
            "env_script": args.env_script or "",
            "command": command_builder(None if tool_name != "subfits" else pathlib.Path("<output>.fits")),
            "command_shell": command_shell,
        },
        "fits_header": header,
        "derived_workload": workload,
        "warmups": warmup_results,
        "repeats": repeat_results,
        "summary": summary,
    }

    write_json(output_dir / "result.json", result)
    if header is not None:
        write_json(output_dir / "header.json", header)
    (output_dir / "command.txt").write_text(result["benchmark"]["command_shell"] + "\n", encoding="utf-8")
    return 0


def source_specs(args: argparse.Namespace) -> List[Tuple[str, str, str]]:
    specs: List[Tuple[str, str, str]] = []
    if args.local_source:
        specs.append(("local", "imcopy", args.local_source))
    if args.remote_source:
        specs.append(("remote", "imcopydav", args.remote_source))
    if not specs:
        raise SystemExit("submit requires at least one of --local-source or --remote-source")
    return specs


def worker_counts(strategy: str, cpus: int) -> List[int]:
    if strategy == "one":
        return [1]
    if strategy == "match-cpus":
        return [cpus]
    return sorted(set([1, cpus]))


def format_combo_name(source_name: str, cpus: int, workers: int, target_mib: Optional[int], pixel_filter: Optional[str]) -> str:
    if target_mib is not None:
        workload = f"{target_mib}mib"
    else:
        workload = slugify(pixel_filter or "filter")
    return f"{source_name}_c{cpus}_w{workers}_{workload}"


def append_sbatch_option(command: List[str], option: str, value: Optional[str]) -> None:
    if value:
        command.extend([option, value])


def cmd_submit(args: argparse.Namespace) -> int:
    if bool(args.target_mib) == bool(args.pixel_filter):
        raise SystemExit("submit requires exactly one of --target-mib or --pixel-filter")

    script_path = pathlib.Path(__file__).resolve()
    default_job_script = script_path.with_name("subfits_benchmark_job.sbatch")
    job_script = pathlib.Path(args.job_script).resolve() if args.job_script else default_job_script
    if not job_script.exists():
        raise SystemExit(f"Job script not found: {job_script}")
    submit_dir = resolve_submit_dir(args)

    session_dir = ensure_dir(
        pathlib.Path(args.results_root).resolve()
        / datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    )
    manifest_path = session_dir / "manifest.tsv"
    session_meta = {
        "created_at_utc": utc_now(),
        "submit_args": {key: value for key, value in vars(args).items() if key != "func"},
        "job_script": str(job_script),
    }
    write_json(session_dir / "session.json", session_meta)

    workloads: List[Tuple[Optional[int], Optional[str]]] = []
    if args.target_mib:
        workloads.extend((target_mib, None) for target_mib in args.target_mib)
    if args.pixel_filter:
        workloads.extend((None, pixel_filter) for pixel_filter in args.pixel_filter)

    for source_name, mode, source in source_specs(args):
        for cpus in args.cpus:
            for workers in worker_counts(args.worker_strategy, cpus):
                for target_mib, pixel_filter in workloads:
                    combo_name = format_combo_name(source_name, cpus, workers, target_mib, pixel_filter)
                    output_dir = ensure_dir(session_dir / combo_name)
                    job_name = f"{args.job_name_prefix}-{combo_name}"
                    stdout_path = output_dir / "slurm-%j.out"
                    stderr_path = output_dir / "slurm-%j.err"

                    command = [
                        "sbatch",
                        "--parsable",
                        "--job-name",
                        job_name,
                        "--cpus-per-task",
                        str(cpus),
                        "--output",
                        str(stdout_path),
                        "--error",
                        str(stderr_path),
                    ]
                    append_sbatch_option(command, "--time", args.time)
                    append_sbatch_option(command, "--partition", args.partition)
                    append_sbatch_option(command, "--account", args.account)
                    append_sbatch_option(command, "--qos", args.qos)
                    append_sbatch_option(command, "--constraint", args.constraint)
                    append_sbatch_option(command, "--reservation", args.reservation)
                    append_sbatch_option(command, "--mem", args.mem)
                    command.extend(args.sbatch_arg)
                    command.append(str(job_script))
                    command.extend(["--runner-script", str(script_path)])
                    command.extend(
                        [
                            "--binary",
                            args.binary,
                            "--mode",
                            mode,
                            "--source",
                            source,
                            "--extnum",
                            str(args.extnum),
                            "--parallel-workers",
                            str(workers),
                            "--warmup",
                            str(args.warmup),
                            "--repeats",
                            str(args.repeats),
                            "--output-dir",
                            str(output_dir),
                            "--label",
                            combo_name,
                        ]
                    )
                    if args.env_script:
                        command.extend(["--env-script", str(pathlib.Path(args.env_script).resolve())])
                    if target_mib is not None:
                        command.extend(["--target-mib", str(target_mib)])
                    if pixel_filter is not None:
                        command.extend(["--pixel-filter", pixel_filter])

                    if args.dry_run:
                        print(shell_join(command))
                        continue

                    completed = run_cmd(command, cwd=submit_dir)
                    if completed.returncode != 0:
                        sys.stderr.write(completed.stderr)
                        raise SystemExit(
                            f"sbatch failed for {combo_name} with exit code {completed.returncode}"
                        )

                    raw_job_id = completed.stdout.strip()
                    job_id = raw_job_id.split(";", 1)[0]
                    write_manifest_row(
                        manifest_path,
                        {
                            "submitted_at_utc": utc_now(),
                            "job_id": job_id,
                            "job_name": job_name,
                            "source_name": source_name,
                            "mode": mode,
                            "source": source,
                            "extnum": args.extnum,
                            "cpus": cpus,
                            "parallel_workers": workers,
                            "target_mib": target_mib if target_mib is not None else "",
                            "pixel_filter": pixel_filter or "",
                            "output_dir": str(output_dir),
                            "result_json": str(output_dir / "result.json"),
                            "slurm_stdout": str(stdout_path),
                            "slurm_stderr": str(stderr_path),
                        },
                    )
                    print(f"{job_id}\t{job_name}")

    if args.dry_run:
        print(f"\nDry run complete. Session directory would be: {session_dir}")
    else:
        print(f"\nManifest: {manifest_path}")
    return 0


def fraction_tag(fraction: float) -> str:
    mapping = {
        1.0: "100pct",
        0.75: "75pct",
        0.5: "50pct",
        0.25: "25pct",
        0.125: "12p5pct",
    }
    if fraction in mapping:
        return mapping[fraction]
    percent = fraction * 100
    return f"{str(percent).replace('.', 'p')}pct"


def start_matrix_stage_definitions() -> List[Tuple[str, float]]:
    return [
        ("half", 0.5),
        ("quarter", 0.25),
        ("eighth", 0.125),
    ]


def start_matrix_case_specs() -> List[Dict[str, Any]]:
    return [
        {"cpus": 64, "requested_memory": "4G+", "input_key": "full", "cut_fraction": 0.5, "cutout_dimensions": "xy", "notes": ""},
        {"cpus": 64, "requested_memory": "4G+", "input_key": "half", "cut_fraction": 0.5, "cutout_dimensions": "xy", "notes": ""},
        {"cpus": 64, "requested_memory": "4G+", "input_key": "quarter", "cut_fraction": 0.5, "cutout_dimensions": "xy", "notes": ""},
        {"cpus": 64, "requested_memory": "4G+", "input_key": "eighth", "cut_fraction": 0.5, "cutout_dimensions": "xy", "notes": ""},
        {"cpus": 64, "requested_memory": "4G+", "input_key": "full", "cut_fraction": 0.25, "cutout_dimensions": "xy", "notes": ""},
        {"cpus": 64, "requested_memory": "4G+", "input_key": "full", "cut_fraction": 0.75, "cutout_dimensions": "xy", "notes": ""},
        {"cpus": 4, "requested_memory": "4G", "input_key": "eighth", "cut_fraction": 0.5, "cutout_dimensions": "xy", "notes": ""},
        {"cpus": 8, "requested_memory": "4G", "input_key": "eighth", "cut_fraction": 0.5, "cutout_dimensions": "xy", "notes": ""},
        {"cpus": 1, "requested_memory": "4G", "input_key": "eighth", "cut_fraction": 0.5, "cutout_dimensions": "xy", "notes": ""},
    ]


def build_location_matrix_case_specs(
    args: argparse.Namespace,
    *,
    full_size_bytes: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    dimension_modes = args.matrix_dimensions or ["xy"]
    using_custom_grid = any(
        value
        for value in (
            args.matrix_cpus,
            args.matrix_input_gib,
            args.matrix_cut_fractions,
            args.matrix_dimensions,
            args.matrix_mem,
            args.matrix_memory_label,
        )
    )
    if not using_custom_grid:
        stage_defs = {
            key: {
                "fraction": fraction,
                "stage_label": key,
            }
            for key, fraction in start_matrix_stage_definitions()
        }
        base_specs = start_matrix_case_specs()
        if dimension_modes == ["xy"]:
            return base_specs, stage_defs
        expanded_specs: List[Dict[str, Any]] = []
        for spec in base_specs:
            for dimension_mode in dimension_modes:
                expanded = dict(spec)
                expanded["cutout_dimensions"] = dimension_mode
                expanded["notes"] = expanded.get("notes", "")
                expanded_specs.append(expanded)
        return expanded_specs, stage_defs

    if not (args.matrix_cpus and args.matrix_input_gib and args.matrix_cut_fractions):
        raise SystemExit(
            "Custom local matrix mode requires --matrix-cpus, --matrix-input-gib, and --matrix-cut-fractions."
        )
    if not args.matrix_mem:
        raise SystemExit("Custom local matrix mode requires --matrix-mem.")
    if any(value < 1 for value in args.matrix_cpus):
        raise SystemExit("--matrix-cpus values must all be >= 1.")
    if any(value <= 0 for value in args.matrix_input_gib):
        raise SystemExit("--matrix-input-gib values must all be > 0.")
    if any(not (0.0 < value <= 1.0) for value in args.matrix_cut_fractions):
        raise SystemExit("--matrix-cut-fractions values must all be within (0, 1].")
    if args.xyz_middle_channels < 1:
        raise SystemExit("--xyz-middle-channels must be >= 1.")
    requested_memory = args.matrix_memory_label or args.matrix_mem

    case_specs: List[Dict[str, Any]] = []
    stage_defs: Dict[str, Dict[str, Any]] = {}
    for input_gib in args.matrix_input_gib:
        target_bytes = gib_to_bytes(input_gib)
        data_fraction = min(1.0, max(target_bytes / full_size_bytes, 1.0 / full_size_bytes))
        input_key = input_key_for_gib(input_gib)
        stage_defs[input_key] = {
            "fraction": data_fraction,
            "stage_label": input_key,
            "reuse_full_source": data_fraction >= (1.0 - 1e-9),
            "requested_input_gib": int(round(input_gib)),
        }
        for dimension_mode in dimension_modes:
            for cut_fraction in args.matrix_cut_fractions:
                for cpus in args.matrix_cpus:
                    case_specs.append(
                        {
                            "cpus": cpus,
                            "requested_memory": requested_memory,
                            "input_key": input_key,
                            "cut_fraction": cut_fraction,
                            "cutout_dimensions": dimension_mode,
                            "notes": "",
                        }
                    )
    return case_specs, stage_defs


def selected_case_numbers(args: argparse.Namespace, case_count: int) -> Optional[set]:
    raw_values = getattr(args, "case_index", None)
    if not raw_values:
        return None
    selected = set(int(value) for value in raw_values)
    valid = set(range(1, case_count + 1))
    invalid = sorted(selected - valid)
    if invalid:
        raise SystemExit(
            f"Invalid --case-index values: {invalid}. Valid case indices are 1-{case_count}."
        )
    return selected


def build_start_matrix_input_map(
    source: str,
    stage_dir: pathlib.Path,
    *,
    location_key: str = "start",
) -> Dict[str, Dict[str, Any]]:
    location_key = normalize_location_key(location_key)
    full_header = parse_fits_header(source)
    full_size = pathlib.Path(source).stat().st_size
    inputs: Dict[str, Dict[str, Any]] = {
        "full": {
            "key": "full",
            "path": str(pathlib.Path(source).resolve()),
            "header": full_header,
            "size_bytes": full_size,
            "size_label": human_gib_approx(full_size),
        }
    }

    for key, fraction in start_matrix_stage_definitions():
        path = stage_dir / stage_filename(source, f"{location_key}_xy_{fraction_tag(fraction)}")
        if not path.exists():
            raise FileNotFoundError(
                f"Missing staged input {path}. Run stage-start-matrix first."
            )
        size_bytes = path.stat().st_size
        inputs[key] = {
            "key": key,
            "path": str(path.resolve()),
            "header": parse_fits_header(str(path)),
            "size_bytes": size_bytes,
            "size_label": human_gib_approx(size_bytes),
        }

    return inputs


def run_cutpixels_stage(
    binary: str,
    source: str,
    stage_dir: pathlib.Path,
    fraction: float,
    *,
    location_key: str = "start",
    force: bool = False,
    stage_label: Optional[str] = None,
    stage_tool: str = "vlkb",
) -> Dict[str, Any]:
    ensure_dir(stage_dir)
    header = parse_fits_header(source)
    location_key = normalize_location_key(location_key)
    derived = derive_fraction_filter(
        header,
        fraction,
        axes_mode="xy",
        anchor=location_anchor_for_filter(location_key),
    )
    stage_suffix = stage_label or fraction_tag(fraction)
    stage_path = stage_dir / stage_filename(source, f"{location_key}_xy_{stage_suffix}")
    if stage_path.exists() and not force:
        size_bytes = stage_path.stat().st_size
        return {
            "path": str(stage_path.resolve()),
            "size_bytes": size_bytes,
            "size_label": human_gib_approx(size_bytes),
            "pixel_filter": derived["pixel_filter"],
            "actual_fraction": derived["actual_fraction"],
        }

    lock_path = stage_dir / f".{stage_path.name}.lock"
    acquire_lock_file(lock_path, description=f"stage {stage_path.name}")

    try:
        if stage_path.exists() and not force:
            size_bytes = stage_path.stat().st_size
            return {
                "path": str(stage_path.resolve()),
                "size_bytes": size_bytes,
                "size_label": human_gib_approx(size_bytes),
                "pixel_filter": derived["pixel_filter"],
                "actual_fraction": derived["actual_fraction"],
            }

        if stage_path.exists():
            stage_path.unlink()

        if stage_tool == "subfits":
            pixel_arg = subfits_pixel_arg(derived["pixel_filter"])
            completed = subprocess.run(
                ["python3", binary, "-i", source, "-o", str(stage_path), "-p", pixel_arg],
                cwd=str(stage_dir),
                text=True,
                capture_output=True,
                check=False,
            )
            if completed.returncode != 0:
                maybe_delete_file(stage_path)
                sys.stderr.write(completed.stdout)
                sys.stderr.write(completed.stderr)
                raise SystemExit(
                    f"subfits staging failed while creating {stage_path.name} with exit code {completed.returncode}"
                )
            if not stage_path.exists():
                raise SystemExit(f"subfits did not create the expected staged file: {stage_path}")
        else:
            source_with_filter = f"{source}{derived['pixel_filter']}"
            temp_name = cutpixels_temp_output_name(source_with_filter)
            temp_path = stage_dir / temp_name
            if temp_path.exists():
                temp_path.unlink()

            completed = subprocess.run(
                [binary, "cutpixels", source_with_filter],
                cwd=str(stage_dir),
                text=True,
                capture_output=True,
                check=False,
            )
            if completed.returncode != 0:
                maybe_delete_file(temp_path)
                sys.stderr.write(completed.stdout)
                sys.stderr.write(completed.stderr)
                raise SystemExit(
                    f"cutpixels failed while creating {stage_path.name} with exit code {completed.returncode}"
                )
            if not temp_path.exists():
                raise SystemExit(f"cutpixels did not create the expected staged file: {temp_path}")
            temp_path.replace(stage_path)

        size_bytes = stage_path.stat().st_size
        return {
            "path": str(stage_path.resolve()),
            "size_bytes": size_bytes,
            "size_label": human_gib_approx(size_bytes),
            "pixel_filter": derived["pixel_filter"],
            "actual_fraction": derived["actual_fraction"],
        }
    finally:
        maybe_delete_file(lock_path)


def cmd_stage_start_matrix(args: argparse.Namespace) -> int:
    stage_dir = ensure_dir(pathlib.Path(args.stage_dir).resolve())
    source_path = pathlib.Path(args.source).resolve()
    if not source_path.exists():
        raise SystemExit(f"Source FITS file not found: {source_path}")

    staged: Dict[str, Any] = {
        "created_at_utc": utc_now(),
        "source": str(source_path),
        "stages": [],
    }
    for key, fraction in start_matrix_stage_definitions():
        stage_info = run_cutpixels_stage(
            args.binary,
            str(source_path),
            stage_dir,
            fraction,
            force=args.force,
        )
        stage_info["key"] = key
        stage_info["fraction"] = fraction
        staged["stages"].append(stage_info)
        print(f"{key}\t{stage_info['path']}\t{stage_info['size_label']}")

    write_json(stage_dir / "start_matrix_staged.json", staged)
    print(f"\nstage_manifest={stage_dir / 'start_matrix_staged.json'}")
    return 0


def start_matrix_job_rows(
    inputs: Dict[str, Dict[str, Any]],
    *,
    location_key: str = "start",
) -> List[Dict[str, Any]]:
    location_key = normalize_location_key(location_key)
    rows: List[Dict[str, Any]] = []
    for spec in start_matrix_case_specs():
        input_info = inputs[spec["input_key"]]
        cutout = derive_fraction_filter(
            input_info["header"],
            spec["cut_fraction"],
            axes_mode="xy",
            anchor=location_anchor_for_filter(location_key),
        )
        rows.append(
            {
                "tool": "vlkb imcopy",
                "cpus": spec["cpus"],
                "requested_memory": spec["requested_memory"],
                "input_key": spec["input_key"],
                "input_path": input_info["path"],
                "input_size_bytes": input_info["size_bytes"],
                "input_size_label": input_info["size_label"],
                "cut_fraction": spec["cut_fraction"],
                "cutout_size_bytes": cutout["estimated_bytes"],
                "cutout_size_label": cutout_size_label(
                    cutout["estimated_bytes"],
                    spec["cut_fraction"],
                    axes_mode="xy",
                ),
                "pixel_filter": cutout["pixel_filter"],
                "cutout_dimensions": spec.get("cutout_dimensions", "xy"),
                "cutout_location": location_label(location_key, axes_mode="xy"),
                "notes": spec.get("notes", ""),
            }
        )
    return rows


def cmd_submit_start_matrix(args: argparse.Namespace) -> int:
    script_path = pathlib.Path(__file__).resolve()
    default_job_script = script_path.with_name("subfits_benchmark_job.sbatch")
    job_script = pathlib.Path(args.job_script).resolve() if args.job_script else default_job_script
    if not job_script.exists():
        raise SystemExit(f"Job script not found: {job_script}")
    submit_dir = resolve_submit_dir(args)

    source_path = pathlib.Path(args.source).resolve()
    if not source_path.exists():
        raise SystemExit(f"Source FITS file not found: {source_path}")

    stage_dir = pathlib.Path(args.stage_dir).resolve()
    inputs = build_start_matrix_input_map(str(source_path), stage_dir)
    session_dir = ensure_dir(
        pathlib.Path(args.results_root).resolve()
        / datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_startxy")
    )
    manifest_path = session_dir / "manifest.tsv"
    session_meta = {
        "created_at_utc": utc_now(),
        "submit_args": {key: value for key, value in vars(args).items() if key != "func"},
        "job_script": str(job_script),
    }
    write_json(session_dir / "session.json", session_meta)

    for index, row in enumerate(start_matrix_job_rows(inputs), start=1):
        mem_value = args.mem_big if row["requested_memory"] == "4G+" else args.mem_small
        combo_name = (
            f"row{index:02d}_c{row['cpus']}_"
            f"{row['requested_memory'].replace('+', 'plus')}_"
            f"{row['input_key']}_"
            f"{fraction_tag(row['cut_fraction'])}"
        )
        output_dir = ensure_dir(session_dir / combo_name)
        job_name = f"{args.job_name_prefix}-{combo_name}"
        stdout_path = output_dir / "slurm-%j.out"
        stderr_path = output_dir / "slurm-%j.err"

        command = [
            "sbatch",
            "--parsable",
            "--job-name",
            job_name,
            "--cpus-per-task",
            str(row["cpus"]),
            "--output",
            str(stdout_path),
            "--error",
            str(stderr_path),
            "--mem",
            mem_value,
        ]
        append_sbatch_option(command, "--time", args.time)
        append_sbatch_option(command, "--partition", args.partition)
        append_sbatch_option(command, "--account", args.account)
        append_sbatch_option(command, "--qos", args.qos)
        append_sbatch_option(command, "--constraint", args.constraint)
        append_sbatch_option(command, "--reservation", args.reservation)
        command.extend(args.sbatch_arg)
        command.append(str(job_script))
        command.extend(["--runner-script", str(script_path)])
        command.extend(
            [
                "--binary",
                args.binary,
                "--mode",
                "imcopy",
                "--source",
                row["input_path"],
                "--extnum",
                "0",
                "--parallel-workers",
                "1",
                "--warmup",
                "1",
                "--repeats",
                "3",
                "--output-dir",
                str(output_dir),
                "--label",
                combo_name,
                "--pixel-filter",
                row["pixel_filter"],
            ]
        )
        if args.env_script:
            command.extend(["--env-script", str(pathlib.Path(args.env_script).resolve())])

        if args.dry_run:
            print(shell_join(command))
            continue

        completed = run_cmd(command, cwd=submit_dir)
        if completed.returncode != 0:
            sys.stderr.write(completed.stderr)
            raise SystemExit(
                f"sbatch failed for {combo_name} with exit code {completed.returncode}"
            )

        raw_job_id = completed.stdout.strip()
        job_id = raw_job_id.split(";", 1)[0]
        write_manifest_row(
            manifest_path,
            {
                "submitted_at_utc": utc_now(),
                "job_id": job_id,
                "job_name": job_name,
                "tool": row["tool"],
                "source_name": "startxy",
                "mode": "imcopy",
                "source": row["input_path"],
                "extnum": 0,
                "cpus": row["cpus"],
                "requested_memory": row["requested_memory"],
                "parallel_workers": 1,
                "input_size_bytes": row["input_size_bytes"],
                "input_size_label": row["input_size_label"],
                "input_size_gib": row.get("input_size_gib", int(round(row["input_size_bytes"] / (1024 ** 3)))),
                "file_name": pathlib.Path(row["input_path"]).name,
                "target_mib": "",
                "pixel_filter": row["pixel_filter"],
                "cutout_size_bytes": row["cutout_size_bytes"],
                "cutout_size_label": row["cutout_size_label"],
                "cutout_size_percent": int(round(row["cut_fraction"] * 100)),
                "cutout_dimensions": row.get("cutout_dimensions", "xy"),
                "cutout_location": row["cutout_location"],
                "notes": row.get("notes", ""),
                "output_dir": str(output_dir),
                "result_json": str(output_dir / "result.json"),
                "slurm_stdout": str(stdout_path),
                "slurm_stderr": str(stderr_path),
            },
        )
        print(f"{job_id}\t{job_name}")

    if args.dry_run:
        print(f"\nDry run complete. Session directory would be: {session_dir}")
    else:
        print(f"\nManifest: {manifest_path}")
    return 0


def parse_sbatch_job_id(raw_stdout: str) -> str:
    raw_job_id = raw_stdout.strip()
    return raw_job_id.split(";", 1)[0]


def infer_job_id_from_output_dir(output_dir: pathlib.Path) -> str:
    job_env = read_key_value_file(output_dir / "job-env.txt")
    if job_env.get("slurm_job_id"):
        return str(job_env["slurm_job_id"])

    for pattern in ("slurm-*.out", "slurm-*.err"):
        for path in sorted(output_dir.glob(pattern)):
            stem = path.name
            if stem.startswith("slurm-"):
                job_id = stem[len("slurm-") :].split(".", 1)[0]
                if job_id.isdigit():
                    return job_id
    return ""


def row_has_execution_artifacts(output_dir: pathlib.Path) -> bool:
    if (output_dir / "job-env.txt").exists():
        return True
    if (output_dir / "command.txt").exists():
        return True
    if (output_dir / "header.json").exists():
        return True
    if (output_dir / "stderr").exists():
        return True
    if any(output_dir.glob("slurm-*.out")):
        return True
    if any(output_dir.glob("slurm-*.err")):
        return True
    return False


def maybe_delete_file(path: pathlib.Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except FileNotFoundError:
        pass


def acquire_lock_file(lock_path: pathlib.Path, *, description: str, poll_seconds: int = 5) -> pathlib.Path:
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                payload = json.loads(lock_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                payload = {}
            pid = int(payload.get("pid", 0) or 0)
            host = str(payload.get("host", "") or "")
            if host == socket.gethostname() and pid and not process_is_alive(pid):
                maybe_delete_file(lock_path)
                continue
            time.sleep(poll_seconds)
            continue

        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "description": description,
                    "host": socket.gethostname(),
                    "pid": os.getpid(),
                    "created_at_utc": utc_now(),
                },
                handle,
            )
        return lock_path


def full_input_info(source_path: pathlib.Path) -> Dict[str, Any]:
    size_bytes = source_path.stat().st_size
    return {
        "key": "full",
        "path": str(source_path.resolve()),
        "header": parse_fits_header(str(source_path)),
        "size_bytes": size_bytes,
        "size_label": human_gib_approx(size_bytes),
        "input_size_gib": int(round(size_bytes / (1024 ** 3))),
    }


def build_location_matrix_manifest_row(
    *,
    job_id: str,
    job_name: str,
    location_key: str,
    row: Dict[str, Any],
    output_dir: pathlib.Path,
    stdout_path: pathlib.Path,
    stderr_path: pathlib.Path,
) -> Dict[str, Any]:
    return {
        "submitted_at_utc": utc_now(),
        "job_id": job_id,
        "job_name": job_name,
        "tool": row["tool"],
        "source_name": location_key,
        "mode": row["mode"],
        "source": row["input_path"],
        "extnum": 0,
        "cpus": row["cpus"],
        "requested_memory": row["requested_memory"],
        "parallel_workers": 1,
        "input_size_bytes": row["input_size_bytes"],
        "input_size_label": row["input_size_label"],
        "input_size_gib": row.get("input_size_gib", int(round(row["input_size_bytes"] / (1024 ** 3)))),
        "file_name": pathlib.Path(row["input_path"]).name,
        "target_mib": "",
        "pixel_filter": row["pixel_filter"],
        "cutout_size_bytes": row["cutout_size_bytes"],
        "cutout_size_label": row["cutout_size_label"],
        "cutout_size_percent": int(round(row["cut_fraction"] * 100)),
        "cutout_dimensions": row.get("cutout_dimensions", "xy"),
        "cutout_location": row["cutout_location"],
        "notes": row.get("notes", ""),
        "output_dir": str(output_dir),
        "result_json": str(output_dir / "result.json"),
        "slurm_stdout": str(stdout_path),
        "slurm_stderr": str(stderr_path),
    }


def combo_name_for_case(
    location_key: str,
    row_index: int,
    row_width: int,
    spec: Dict[str, Any],
) -> str:
    combo_name = (
        f"{location_key}_row{row_index:0{row_width}d}_c{spec['cpus']}_"
        f"{spec['requested_memory'].replace('+', 'plus')}_"
        f"{spec['input_key']}_"
        f"{fraction_tag(spec['cut_fraction'])}"
    )
    if spec.get("cutout_dimensions", "xy") == "xyz":
        combo_name += "_xyz"
    return combo_name


def build_case_row(
    input_info: Dict[str, Any],
    spec: Dict[str, Any],
    *,
    location_key: str,
    xyz_middle_channels: int,
    benchmark_tool: str,
) -> Dict[str, Any]:
    if spec.get("cutout_dimensions", "xy") == "xyz":
        cutout = derive_xyz_middle_channels_filter(
            input_info["header"],
            spec["cut_fraction"],
            anchor=location_anchor_for_filter(location_key),
            middle_channels=xyz_middle_channels,
        )
        cutout_dimensions = "x,y,z"
        notes = spec.get("notes", "")
    else:
        cutout = derive_fraction_filter(
            input_info["header"],
            spec["cut_fraction"],
            axes_mode="xy",
            anchor=location_anchor_for_filter(location_key),
        )
        cutout_dimensions = "x,y"
        notes = spec.get("notes", "")

    return {
        "tool": tool_label(benchmark_tool, "subfits" if benchmark_tool == "subfits" else "imcopy"),
        "mode": "subfits" if benchmark_tool == "subfits" else "imcopy",
        "cpus": spec["cpus"],
        "requested_memory": spec["requested_memory"],
        "input_key": spec["input_key"],
        "input_path": input_info["path"],
        "input_size_bytes": input_info["size_bytes"],
        "input_size_label": input_info["size_label"],
        "input_size_gib": input_info.get("input_size_gib", int(round(input_info["size_bytes"] / (1024 ** 3)))),
        "cut_fraction": spec["cut_fraction"],
        "cutout_size_bytes": cutout["estimated_bytes"],
        "cutout_size_label": cutout_size_label(
            cutout["estimated_bytes"],
            spec["cut_fraction"],
            axes_mode="xy" if cutout_dimensions == "x,y" else "all",
        ),
        "pixel_filter": cutout["pixel_filter"],
        "cutout_dimensions": cutout_dimensions,
        "cutout_location": location_title(location_key),
        "notes": notes,
    }


def detect_session_failure_nodes(
    session_dir: pathlib.Path,
    manifest_rows: Sequence[Dict[str, str]],
    *,
    category: str,
) -> List[str]:
    manifest_by_output_dir = {
        str(pathlib.Path(row["output_dir"]).resolve()): row
        for row in manifest_rows
        if row.get("output_dir")
    }
    failure_nodes: Set[str] = set()
    for output_dir in output_dirs_for_session(session_dir, manifest_rows):
        manifest_row = manifest_by_output_dir.get(str(output_dir.resolve()))
        context = build_scan_context(session_dir, output_dir, manifest_row)
        error_rows = result_error_rows_for_output_dir(
            session_dir,
            output_dir,
            manifest_row,
            context=context,
        )
        if not any(str(row.get("error_category", "")) == category for row in error_rows):
            continue
        node_name = output_dir_node_name(output_dir, context=context)
        if node_name:
            failure_nodes.add(node_name)
    return sorted(failure_nodes)


def detect_session_success_nodes(
    session_dir: pathlib.Path,
    manifest_rows: Sequence[Dict[str, str]],
) -> List[str]:
    manifest_by_output_dir = {
        str(pathlib.Path(row["output_dir"]).resolve()): row
        for row in manifest_rows
        if row.get("output_dir")
    }
    success_nodes: Set[str] = set()
    for output_dir in output_dirs_for_session(session_dir, manifest_rows):
        manifest_row = manifest_by_output_dir.get(str(output_dir.resolve()))
        successful_result, _ = row_result_success(output_dir)
        if not successful_result:
            continue
        node_name = output_dir_node_name(output_dir)
        if node_name:
            success_nodes.add(node_name)
    return sorted(success_nodes)


def partition_node_names(partition: str) -> List[str]:
    partition_text = str(partition).strip()
    if not partition_text:
        return []
    completed = run_cmd(["sinfo", "-N", "-h", "-p", partition_text, "-o", "%N"])
    if completed.returncode != 0:
        return []
    nodes = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    seen: Set[str] = set()
    ordered: List[str] = []
    for node in nodes:
        if node not in seen:
            seen.add(node)
            ordered.append(node)
    return ordered


def row_result_success(output_dir: pathlib.Path) -> Tuple[bool, Optional[Dict[str, Any]]]:
    result_json_path = output_dir / "result.json"
    if not result_json_path.exists():
        return False, None
    try:
        result_data = json.loads(result_json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Could not parse existing result.json for {output_dir}: {exc}") from exc
    result_summary = result_data.get("summary", {})
    successful_repeats = int(result_summary.get("successful_repeats", 0) or 0)
    failed_repeats = int(result_summary.get("failed_repeats", 0) or 0)
    successful_result = successful_repeats > 0 and failed_repeats == 0 and result_summary.get("median_seconds") is not None
    return successful_result, result_data


def wait_for_slurm_jobs(job_ids: Sequence[str], poll_seconds: int = 30) -> None:
    pending_ids = [job_id for job_id in job_ids if job_id]
    if not pending_ids:
        return
    while True:
        completed = run_cmd(
            [
                "squeue",
                "-h",
                "-j",
                ",".join(pending_ids),
                "-o",
                "%i",
            ]
        )
        active_ids = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        if not active_ids:
            return
        time.sleep(poll_seconds)


def run_location_matrix_rows(
    source_path: pathlib.Path,
    location_key: str,
    stage_dir: pathlib.Path,
    args: argparse.Namespace,
    session_dir: pathlib.Path,
    manifest_path: pathlib.Path,
) -> None:
    location_key = normalize_location_key(location_key)
    existing_rows = load_manifest(manifest_path) if manifest_path.exists() else []
    existing_by_output_dir = {row["output_dir"]: row for row in existing_rows}
    full_info = full_input_info(source_path)
    case_specs, stage_definitions = build_location_matrix_case_specs(
        args,
        full_size_bytes=full_info["size_bytes"],
    )
    selected_cases = selected_case_numbers(args, len(case_specs))
    row_width = max(2, len(str(len(case_specs))))
    selected_specs: List[Tuple[int, Dict[str, Any]]] = [
        (index, spec)
        for index, spec in enumerate(case_specs, start=1)
        if selected_cases is None or index in selected_cases
    ]
    grouped_specs: Dict[str, List[Tuple[int, Dict[str, Any]]]] = {}
    group_order: List[str] = []
    for index, spec in selected_specs:
        key = spec["input_key"]
        if key not in grouped_specs:
            grouped_specs[key] = []
            group_order.append(key)
        grouped_specs[key].append((index, spec))

    all_submitted_job_ids: List[str] = []
    staged_cleanup_paths: List[pathlib.Path] = []
    for input_key in group_order:
        group_requires_input = False
        for row_index, spec in grouped_specs[input_key]:
            combo_name = combo_name_for_case(location_key, row_index, row_width, spec)
            output_dir = session_dir / combo_name
            existing_row = existing_by_output_dir.get(str(output_dir))
            successful_result, _ = row_result_success(output_dir)
            if not (successful_result and existing_row is not None):
                group_requires_input = True
                break

        if input_key == "full":
            input_info = dict(full_info)
            input_info["reuse_full_source"] = True
        else:
            stage_def = stage_definitions[input_key]
            if stage_def.get("reuse_full_source"):
                input_info = dict(full_info)
                input_info["key"] = input_key
                input_info["input_size_gib"] = int(stage_def.get("requested_input_gib") or input_info.get("input_size_gib") or 0)
                input_info["size_label"] = str(input_info["input_size_gib"])
                input_info["reuse_full_source"] = True
            elif group_requires_input:
                input_info = run_cutpixels_stage(
                    args.stage_binary_resolved,
                    str(source_path),
                    stage_dir,
                    stage_def["fraction"],
                    location_key=location_key,
                    stage_label=stage_def["stage_label"],
                    stage_tool=args.tool,
                )
                input_info["key"] = input_key
                input_info["header"] = parse_fits_header(input_info["path"])
                input_info["input_size_gib"] = int(stage_def.get("requested_input_gib") or round(input_info["size_bytes"] / (1024 ** 3)))
                input_info["reuse_full_source"] = False
            else:
                input_info = {
                    "path": str(stage_dir / stage_filename(str(source_path), f"{location_key}_xy_{stage_def['stage_label']}")),
                    "input_size_gib": int(stage_def.get("requested_input_gib") or 0),
                    "reuse_full_source": False,
                }
        if input_key != "full" and not input_info.get("reuse_full_source"):
            staged_path = pathlib.Path(input_info["path"])
            if staged_path not in staged_cleanup_paths:
                staged_cleanup_paths.append(staged_path)

        for row_index, spec in grouped_specs[input_key]:
            spec = dict(spec)
            spec.setdefault("cutout_dimensions", "xy")
            spec.setdefault("notes", "")
            spec["xyz_middle_channels"] = args.xyz_middle_channels
            combo_name = combo_name_for_case(location_key, row_index, row_width, spec)
            output_dir = ensure_dir(session_dir / combo_name)
            job_name = f"{args.job_name_prefix}-{combo_name}"
            stdout_path = output_dir / "slurm-%j.out"
            stderr_path = output_dir / "slurm-%j.err"

            if args.force_rerun and output_dir.exists():
                shutil.rmtree(output_dir)
                output_dir = ensure_dir(output_dir)
                existing_by_output_dir.pop(str(output_dir), None)

            existing_row = existing_by_output_dir.get(str(output_dir))
            successful_result, _ = row_result_success(output_dir)
            if not successful_result and (output_dir / "result.json").exists():
                if args.force_rerun or args.rerun_failed_only:
                    shutil.rmtree(output_dir)
                    output_dir = ensure_dir(output_dir)
                    existing_by_output_dir.pop(str(output_dir), None)
                    existing_row = None
                else:
                    raise SystemExit(
                        f"Row {combo_name} has a completed-but-failed result.json under {output_dir}. "
                        "Use --rerun-failed-only, --force-rerun, or remove that row directory before resuming."
                    )

            if row_has_execution_artifacts(output_dir) and not (output_dir / "result.json").exists():
                if args.force_rerun or args.rerun_failed_only:
                    shutil.rmtree(output_dir)
                    output_dir = ensure_dir(output_dir)
                    existing_by_output_dir.pop(str(output_dir), None)
                    existing_row = None
                else:
                    inferred_job_id = infer_job_id_from_output_dir(output_dir)
                    prior_job_text = f" Previous job id: {inferred_job_id}." if inferred_job_id else ""
                    raise SystemExit(
                        f"Row {combo_name} appears to be in progress or incomplete under {output_dir}. "
                        f"Wait for it to finish, inspect its stderr, or remove that row directory before resuming.{prior_job_text}"
                    )

            if successful_result and existing_row is not None:
                print(f"skip_existing\t{combo_name}")
                continue

            row = build_case_row(
                input_info,
                spec,
                location_key=location_key,
                xyz_middle_channels=args.xyz_middle_channels,
                benchmark_tool=args.tool,
            )

            if successful_result:
                if existing_row is None:
                    existing_job_id = infer_job_id_from_output_dir(output_dir)
                    if not existing_job_id:
                        raise SystemExit(
                            f"Resume could not infer job_id for completed row {combo_name} in {output_dir}"
                        )
                    manifest_row = build_location_matrix_manifest_row(
                        job_id=existing_job_id,
                        job_name=job_name,
                        location_key=location_key,
                        row=row,
                        output_dir=output_dir,
                        stdout_path=stdout_path,
                        stderr_path=stderr_path,
                    )
                    write_manifest_row(manifest_path, manifest_row)
                    existing_by_output_dir[str(output_dir)] = manifest_row
                print(f"skip_existing\t{combo_name}")
                continue

            mem_value = args.mem_big if row["requested_memory"] == "4G+" else args.mem_small
            if args.matrix_mem:
                mem_value = args.matrix_mem

            command = [
                "sbatch",
                "--parsable",
                "--job-name",
                job_name,
                "--cpus-per-task",
                str(row["cpus"]),
                "--output",
                str(stdout_path),
                "--error",
                str(stderr_path),
                "--mem",
                mem_value,
            ]
            append_sbatch_option(command, "--time", args.time)
            append_sbatch_option(command, "--partition", args.partition)
            append_sbatch_option(command, "--account", args.account)
            append_sbatch_option(command, "--qos", args.qos)
            append_sbatch_option(command, "--constraint", args.constraint)
            append_sbatch_option(command, "--reservation", args.reservation)
            command.extend(args.sbatch_arg)
            command.append(str(args.job_script_resolved))
            command.extend(["--runner-script", str(args.runner_script_resolved)])
            command.extend(
                [
                    "--tool",
                    args.tool,
                    "--binary",
                    args.binary,
                    "--mode",
                    row["mode"],
                    "--source",
                    row["input_path"],
                    "--extnum",
                    "0",
                    "--parallel-workers",
                    "1",
                    "--warmup",
                    str(args.warmup),
                    "--repeats",
                    str(args.repeats),
                    "--output-dir",
                    str(output_dir),
                    "--label",
                    combo_name,
                    "--pixel-filter",
                    row["pixel_filter"],
                ]
            )
            if args.env_script:
                command.extend(["--env-script", str(pathlib.Path(args.env_script).resolve())])

            completed = run_cmd(command, cwd=args.submit_dir_resolved)
            if completed.returncode != 0:
                sys.stderr.write(completed.stderr)
                raise SystemExit(
                    f"sbatch failed for {combo_name} with exit code {completed.returncode}"
                )

            job_id = parse_sbatch_job_id(completed.stdout)
            all_submitted_job_ids.append(job_id)
            manifest_row = build_location_matrix_manifest_row(
                job_id=job_id,
                job_name=job_name,
                location_key=location_key,
                row=row,
                output_dir=output_dir,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
            write_manifest_row(manifest_path, manifest_row)
            existing_by_output_dir[str(output_dir)] = manifest_row
            print(f"{job_id}\t{job_name}")

    wait_for_slurm_jobs(all_submitted_job_ids)
    if not args.keep_staged:
        for staged_path in staged_cleanup_paths:
            maybe_delete_file(staged_path)


def cmd_run_location_matrix(args: argparse.Namespace) -> int:
    script_path = pathlib.Path(__file__).resolve()
    default_job_script = script_path.with_name("subfits_benchmark_job.sbatch")
    job_script = pathlib.Path(args.job_script).resolve() if args.job_script else default_job_script
    if not job_script.exists():
        raise SystemExit(f"Job script not found: {job_script}")
    submit_dir = resolve_submit_dir(args)

    source_path = pathlib.Path(args.source).resolve()
    if not source_path.exists():
        raise SystemExit(f"Source FITS file not found: {source_path}")

    if args.stage_binary:
        args.stage_binary_resolved = args.stage_binary
    elif args.tool == "subfits":
        args.stage_binary_resolved = args.binary
    else:
        args.stage_binary_resolved = args.binary

    args.job_script_resolved = job_script
    args.runner_script_resolved = script_path
    args.submit_dir_resolved = submit_dir
    stage_dir = ensure_dir(pathlib.Path(args.stage_dir).resolve())
    results_root = pathlib.Path(args.results_root).resolve()
    if args.session_dir:
        session_dir = ensure_dir(pathlib.Path(args.session_dir).resolve())
    else:
        session_dir = ensure_dir(
            results_root / datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_locxy")
        )
    manifest_path = session_dir / "manifest.tsv"
    session_meta_path = session_dir / "session.json"
    if session_meta_path.exists():
        try:
            session_meta = json.loads(session_meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            session_meta = {}
    else:
        session_meta = {}

    if not session_meta.get("created_at_utc"):
        session_meta["created_at_utc"] = utc_now()
    session_meta["last_run_at_utc"] = utc_now()
    session_meta["submit_args"] = {
        key: value
        for key, value in vars(args).items()
        if key not in {"func", "job_script_resolved", "runner_script_resolved", "submit_dir_resolved"}
    }
    session_meta["job_script"] = str(job_script)
    session_meta["storage_strategy"] = (
        "one canonical staged cube per location+size bucket; submit groups as soon as each staged cube is ready; delete staged cubes after all jobs for that location finish"
    )
    write_json(session_dir / "session.json", session_meta)
    manifest_rows = load_manifest(manifest_path) if manifest_path.exists() else []
    explicit_node_selection = any(
        raw_arg.startswith("--exclude=") or raw_arg.startswith("--nodelist=")
        for raw_arg in args.sbatch_arg
    )
    if args.rerun_failed_only and not explicit_node_selection:
        success_nodes = detect_session_success_nodes(session_dir, manifest_rows)
        auto_exclude_nodes = detect_session_failure_nodes(
            session_dir,
            manifest_rows,
            category="glibc_version_mismatch",
        )
        if success_nodes and args.partition:
            partition_nodes = partition_node_names(args.partition)
            if partition_nodes:
                auto_exclude_nodes = sorted(
                    (set(partition_nodes) - set(success_nodes)) | set(auto_exclude_nodes)
                )
                preview = ",".join(success_nodes[:8])
                if len(success_nodes) > 8:
                    preview += ",..."
                print(f"auto_prefer_success_nodes\t{len(success_nodes)}\t{preview}")
        if auto_exclude_nodes:
            args.sbatch_arg = list(args.sbatch_arg) + [f"--exclude={','.join(auto_exclude_nodes)}"]
            preview = ",".join(auto_exclude_nodes[:8])
            if len(auto_exclude_nodes) > 8:
                preview += ",..."
            print(f"auto_exclude_glibc_failure_nodes\t{len(auto_exclude_nodes)}\t{preview}")
    lock_path = session_dir / ".run_location_matrix.lock"
    acquire_lock_file(lock_path, description="run-location-matrix session")
    try:
        for raw_location in args.locations:
            location_key = normalize_location_key(raw_location)
            if location_key not in {"start", "middle", "end"}:
                raise SystemExit(f"Unsupported location: {raw_location}")
            run_location_matrix_rows(source_path, location_key, stage_dir, args, session_dir, manifest_path)

        collected = collect_rows(manifest_path, args.seff_command, args.sacct_command)
        enriched_rows = enrich_collected_rows(collected)
        enriched_tsv = session_dir / "manifest_enriched.tsv"
        write_tsv(
            enriched_tsv,
            enriched_rows,
            preferred=[
                "job_id",
                "job_name",
                "tool",
                "source_name",
                "mode",
                "source",
                "cpus",
                "requested_memory",
                "input_size_label",
                "input_size_gib",
                "file_name",
                "cutout_size_label",
                "cutout_size_percent",
                "cutout_dimensions",
                "cutout_location",
                "notes",
                "wall_time_elapsed",
                "peak_memory",
                "max_vmsize",
                "max_rss",
                "cpu_usage_percent",
                "cpu_usage_display",
                "node_type",
            ],
        )
        summary_csv = session_dir / "location_matrix_summary.csv"
        summary_md = session_dir / "location_matrix_summary.md"
        write_start_matrix_summary(
            manifest_path,
            enriched_rows,
            summary_csv=summary_csv,
            summary_md=summary_md,
            title="SB75060 Location Matrix Summary",
        )
        print(f"\nManifest: {manifest_path}")
        print(f"manifest_enriched_tsv={enriched_tsv}")
        print(f"summary_csv={summary_csv}")
        print(f"summary_md={summary_md}")
        return 0
    finally:
        maybe_delete_file(lock_path)


def cmd_scan_errors(args: argparse.Namespace) -> int:
    manifest_path: Optional[pathlib.Path] = pathlib.Path(args.manifest).resolve() if args.manifest else None
    session_dir: Optional[pathlib.Path] = pathlib.Path(args.session_dir).resolve() if args.session_dir else None

    if manifest_path is None and session_dir is None:
        raise SystemExit("scan-errors requires either --manifest or --session-dir")
    if manifest_path is None:
        manifest_candidate = session_dir / "manifest.tsv"
        manifest_path = manifest_candidate if manifest_candidate.exists() else None
    if session_dir is None:
        session_dir = manifest_path.parent

    manifest_rows = load_manifest(manifest_path) if manifest_path and manifest_path.exists() else []
    manifest_by_output_dir = {
        str(pathlib.Path(row["output_dir"]).resolve()): row
        for row in manifest_rows
        if row.get("output_dir")
    }

    run_rows: List[Dict[str, Any]] = []
    for output_dir in output_dirs_for_session(session_dir, manifest_rows):
        manifest_row = manifest_by_output_dir.get(str(output_dir.resolve()))
        run_rows.append(summarize_run_for_output_dir(session_dir, output_dir, manifest_row))

    output_csv = pathlib.Path(args.output_csv).resolve() if args.output_csv else session_dir / "error_scan.csv"
    columns = [
        "session_dir",
        "session_name",
        "combo_name",
        "submitted_at_utc",
        "job_id",
        "job_name",
        "tool",
        "source_name",
        "mode",
        "cutout_location",
        "cutout_dimensions",
        "cpus",
        "requested_memory",
        "input_size_gib",
        "input_size_label",
        "cutout_size_percent",
        "cutout_size_label",
        "file_name",
        "source",
        "pixel_filter",
        "run_status",
        "run_succeeded",
        "has_result_json",
        "has_execution_artifacts",
        "runner_successful_repeats",
        "runner_failed_repeats",
        "error_count",
        "primary_error_category",
        "primary_error_source",
        "primary_error_summary",
        "primary_error_excerpt",
        "error_categories",
        "primary_log_path",
        "all_log_paths",
        "output_dir",
        "result_json",
        "notes",
    ]
    with open(output_csv, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(run_rows)

    status_counts: Dict[str, int] = {}
    for row in run_rows:
        status = str(row.get("run_status", "") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    rows_with_error_artifacts = sum(1 for row in run_rows if int(row.get("error_count", 0) or 0) > 0)
    failed_rows_with_errors = sum(
        1
        for row in run_rows
        if row.get("run_status") != "success" and int(row.get("error_count", 0) or 0) > 0
    )
    successful_rows_with_artifacts = sum(
        1
        for row in run_rows
        if row.get("run_status") == "success" and int(row.get("error_count", 0) or 0) > 0
    )

    error_category_counts: Dict[str, int] = {}
    for row in run_rows:
        category = str(row.get("primary_error_category", "")).strip()
        if not category:
            continue
        error_category_counts[category] = error_category_counts.get(category, 0) + 1

    print(f"session_dir={session_dir}")
    if manifest_path:
        print(f"manifest={manifest_path}")
    print(f"scan_csv={output_csv}")
    print(f"run_rows={len(run_rows)}")
    print(f"successful_rows={sum(1 for row in run_rows if row.get('run_status') == 'success')}")
    print(f"rows_with_error_artifacts={rows_with_error_artifacts}")
    print(f"failed_rows_with_errors={failed_rows_with_errors}")
    print(f"successful_rows_with_error_artifacts={successful_rows_with_artifacts}")
    for status in sorted(status_counts):
        print(f"status_{status}={status_counts[status]}")
    for category in sorted(error_category_counts):
        print(f"primary_{category}={error_category_counts[category]}")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    manifest_path = pathlib.Path(args.manifest).resolve()
    session_dir = manifest_path.parent
    collected = collect_rows(manifest_path, args.seff_command, args.sacct_command)
    enriched_rows = enrich_collected_rows(collected)

    enriched_tsv = session_dir / "manifest_enriched.tsv"
    write_tsv(
        enriched_tsv,
        enriched_rows,
        preferred=[
            "job_id",
            "job_name",
            "tool",
            "source_name",
            "mode",
            "source",
            "cpus",
            "requested_memory",
            "input_size_label",
            "input_size_gib",
            "file_name",
            "cutout_size_label",
            "cutout_size_percent",
            "cutout_dimensions",
            "cutout_location",
            "notes",
            "wall_time_elapsed",
            "peak_memory",
            "max_vmsize",
            "max_rss",
            "cpu_usage_percent",
            "cpu_usage_display",
            "node_type",
        ],
    )

    all_columns = sorted({key for row in collected for key in row.keys()})
    summary_csv = (
        pathlib.Path(args.summary_csv).resolve()
        if args.summary_csv
        else session_dir / "summary.csv"
    )
    with open(summary_csv, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=all_columns)
        writer.writeheader()
        writer.writerows(collected)

    summary_md = (
        pathlib.Path(args.summary_md).resolve()
        if args.summary_md
        else session_dir / "summary.md"
    )
    md_columns = choose_summary_columns(collected)
    markdown = [
        "# vlkb Dug benchmark summary",
        "",
        f"- created_at_utc: {utc_now()}",
        f"- manifest: {manifest_path}",
        f"- summary_csv: {summary_csv}",
        "",
        format_table(collected, md_columns).rstrip(),
        "",
        "## Notes",
        "",
        "- `seff` raw output for each job is stored under `seff/`.",
        "- Dug `seff` fields are copied through as `seff_*` CSV columns, so non-standard metrics are preserved even if they are not shown in the Markdown table.",
        "",
    ]
    summary_md.write_text("\n".join(markdown), encoding="utf-8")

    location_summary_csv = session_dir / "location_matrix_summary.csv"
    location_summary_md = session_dir / "location_matrix_summary.md"
    write_start_matrix_summary(
        manifest_path,
        enriched_rows,
        summary_csv=location_summary_csv,
        summary_md=location_summary_md,
        title="SB75060 Location Matrix Summary",
    )

    print(f"manifest_enriched_tsv={enriched_tsv}")
    print(f"summary_csv={summary_csv}")
    print(f"summary_md={summary_md}")
    print(f"location_summary_csv={location_summary_csv}")
    print(f"location_summary_md={location_summary_md}")
    return 0


def build_start_matrix_table_rows(collected: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def summary_input_size(row: Dict[str, Any]) -> str:
        value = row.get("input_size_gib", "")
        if value not in {"", None}:
            return str(value)
        size_label = str(row.get("input_size_label", ""))
        return size_label.replace("~", "").replace("GB", "").strip()

    def summary_cutout_percent(row: Dict[str, Any]) -> str:
        value = row.get("cutout_size_percent", "")
        if value not in {"", None}:
            return str(value)
        match = re.match(r"^\s*(\d+)%", str(row.get("cutout_size_label", "")))
        return match.group(1) if match else ""

    def summary_cutout_dimensions(row: Dict[str, Any]) -> str:
        value = str(row.get("cutout_dimensions", "")).strip()
        if value:
            return value
        label = str(row.get("cutout_location", "")).lower()
        if "all" in label or "z" in label:
            return "x,y,z"
        return "x,y"

    def summary_file_name(row: Dict[str, Any]) -> str:
        value = str(row.get("file_name", "")).strip()
        if value:
            return value
        source = str(row.get("source", "")).strip()
        return pathlib.Path(source).name if source else ""

    def summary_location(row: Dict[str, Any]) -> str:
        value = str(row.get("cutout_location", "")).strip()
        if not value:
            return ""
        return location_title(value.split("(", 1)[0].strip())

    table_rows: List[Dict[str, Any]] = []
    for row in collected:
        table_rows.append(
            {
                "Tool": row.get("tool", "vlkb imcopy"),
                "AllocCPU": row.get("cpus", ""),
                "Requested Memory Size": row.get("requested_memory", ""),
                "Input File Size": summary_input_size(row),
                "File": summary_file_name(row),
                "Cutout Size (%)": summary_cutout_percent(row),
                "Cutout Dimensions": summary_cutout_dimensions(row),
                "Cutout Location": summary_location(row),
                "Wall Time (Elapsed)": row.get("wall_time_elapsed", "") or canonical_wall_time(row),
                "Peak Memory Usage (MaxRSS)": row.get("peak_memory", "") or canonical_peak_memory(row),
                "CPU Usage (%)": row.get("cpu_usage_percent", "") or canonical_cpu_usage_percent(row),
                "Node Type": row.get("node_type", "") or canonical_node_type(row),
                "DUG JobId": row.get("job_id", ""),
                "Notes": row.get("notes", ""),
            }
        )
    return table_rows


def write_start_matrix_summary(
    manifest_path: pathlib.Path,
    collected: Sequence[Dict[str, Any]],
    *,
    summary_csv: pathlib.Path,
    summary_md: pathlib.Path,
    title: str = "SB75060 Location Matrix Summary",
) -> None:
    table_rows = build_start_matrix_table_rows(collected)

    ordered_columns = [
        "Tool",
        "AllocCPU",
        "Requested Memory Size",
        "Input File Size",
        "File",
        "Cutout Size (%)",
        "Cutout Dimensions",
        "Cutout Location",
        "Wall Time (Elapsed)",
        "Peak Memory Usage (MaxRSS)",
        "CPU Usage (%)",
        "Node Type",
        "DUG JobId",
        "Notes",
    ]

    with open(summary_csv, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ordered_columns)
        writer.writeheader()
        writer.writerows(table_rows)

    markdown = [
        f"# {title}",
        "",
        f"- created_at_utc: {utc_now()}",
        f"- manifest: {manifest_path}",
        f"- summary_csv: {summary_csv}",
        "",
        format_table(table_rows, ordered_columns).rstrip(),
        "",
    ]
    summary_md.write_text("\n".join(markdown), encoding="utf-8")


def cmd_collect_start_matrix(args: argparse.Namespace) -> int:
    manifest_path = pathlib.Path(args.manifest).resolve()
    session_dir = manifest_path.parent
    collected = collect_rows(manifest_path, args.seff_command, args.sacct_command)
    enriched_rows = enrich_collected_rows(collected)

    enriched_tsv = session_dir / "manifest_enriched.tsv"
    write_tsv(
        enriched_tsv,
        enriched_rows,
        preferred=[
            "job_id",
            "job_name",
            "tool",
            "source_name",
            "mode",
            "source",
            "cpus",
            "requested_memory",
            "input_size_label",
            "input_size_gib",
            "file_name",
            "cutout_size_label",
            "cutout_size_percent",
            "cutout_dimensions",
            "cutout_location",
            "notes",
            "wall_time_elapsed",
            "peak_memory",
            "max_vmsize",
            "max_rss",
            "cpu_usage_percent",
            "cpu_usage_display",
            "node_type",
        ],
    )

    summary_csv = (
        pathlib.Path(args.summary_csv).resolve()
        if args.summary_csv
        else session_dir / "start_matrix_summary.csv"
    )
    summary_md = (
        pathlib.Path(args.summary_md).resolve()
        if args.summary_md
        else session_dir / "start_matrix_summary.md"
    )
    write_start_matrix_summary(
        manifest_path,
        enriched_rows,
        summary_csv=summary_csv,
        summary_md=summary_md,
        title="SB75060 Start-Location Matrix Summary",
    )

    print(f"manifest_enriched_tsv={enriched_tsv}")
    print(f"summary_csv={summary_csv}")
    print(f"summary_md={summary_md}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
