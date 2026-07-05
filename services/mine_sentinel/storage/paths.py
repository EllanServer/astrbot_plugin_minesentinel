"""Filesystem path helpers for MineSentinel JSONL storage."""

from __future__ import annotations

import re
import time
from pathlib import Path

from ..models import ObservationRecord


def record_path(observation_dir: Path, record: ObservationRecord) -> Path:
    server_id = safe_name(record.server_id or "unknown")
    day = time.strftime("%Y%m%d", time.localtime(max(0, record.timestamp) / 1000))
    return observation_dir / server_id / f"{day}.jsonl"


def export_path(
    export_dir: Path,
    window_minutes: int,
    server_id: str | None = None,
    label: str = "",
    now: int | None = None,
    suffix: str = ".jsonl",
) -> Path:
    """Generate a deterministic export file path for the given window.

    The stem encodes (start_day, start_time, end_day, end_time, server_id),
    rounded to the minute. Two exports for the same window/server within
    the same minute produce the same path, enabling ``export_reuse_existing``.
    """
    timestamp = int(time.time()) if now is None else now
    stem = window_export_stem(window_minutes, timestamp, server_id)
    path = export_dir / f"{stem}{suffix}"
    if label and path.exists():
        path = export_dir / f"{stem}_{safe_name(label)}{suffix}"
    return path


def window_export_stem(
    window_minutes: int,
    end_timestamp: int,
    server_id: str | None = None,
) -> str:
    start_timestamp = max(0, end_timestamp - max(1, window_minutes) * 60)
    start_day = time.strftime("%Y%m%d", time.localtime(start_timestamp))
    end_day = time.strftime("%Y%m%d", time.localtime(end_timestamp))
    start_time = time.strftime("%H%M", time.localtime(start_timestamp))
    end_time = time.strftime("%H%M", time.localtime(end_timestamp))
    if start_day == end_day:
        stem = f"mine_sentinel_{start_day}_{start_time}_{end_time}"
    else:
        stem = f"mine_sentinel_{start_day}_{start_time}_{end_day}_{end_time}"
    if server_id:
        stem = f"{stem}_{safe_name(server_id)}"
    return stem


def candidate_files(
    observation_dir: Path,
    server_id: str | None,
    cutoff_ms: int | None = None,
) -> list[Path]:
    cutoff_day = ""
    if cutoff_ms is not None:
        cutoff_day = time.strftime(
            "%Y%m%d",
            time.localtime(max(0, cutoff_ms) / 1000),
        )

    if server_id:
        files = (observation_dir / safe_name(server_id)).glob("*.jsonl")
    else:
        files = observation_dir.glob("*/*.jsonl")
    return sorted(path for path in files if not cutoff_day or path.stem >= cutoff_day)


def cleanup_old_files(
    observation_dir: Path,
    export_dir: Path,
    retention_minutes: int,
):
    cutoff_day = time.strftime(
        "%Y%m%d",
        time.localtime(time.time() - retention_minutes * 60),
    )
    for path in observation_dir.glob("*/*.jsonl"):
        if path.stem < cutoff_day:
            path.unlink(missing_ok=True)
            # 同时清理对应的 .idx 偏移索引文件
            path.with_suffix(".idx").unlink(missing_ok=True)

    export_cutoff = time.time() - max(retention_minutes, 60) * 60
    # 清理 export 目录下的 .jsonl 和 .jsonl.gz 文件
    for pattern in ("*.jsonl", "*.jsonl.gz"):
        for path in export_dir.glob(pattern):
            try:
                if path.stat().st_mtime < export_cutoff:
                    path.unlink(missing_ok=True)
            except FileNotFoundError:
                pass


def safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return safe[:80] or "unknown"
