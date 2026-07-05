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

    The stem encodes (start_day, start_time, end_day, end_time, server_id)
    plus a millisecond-precision ``_t{end_timestamp}`` suffix. Two exports
    with the *exact same* ``end_timestamp`` (e.g. periodic-report retries
    using a fixed ``scheduled_window_end_ms``) produce the same path,
    enabling ``export_reuse_existing``. Two manual ``/mc report now``
    issued milliseconds apart will have different ``end_timestamp`` and
    thus different paths, avoiding accidental reuse of a stale attachment.

    PR9 hotfix v5: ``now`` 接受秒级（< 10^12）或毫秒级（>= 10^12）时间戳，
    内部统一转毫秒。

    PR9 hotfix: ``label`` 非空时**始终**加入文件名，不再依赖基础路径是否
    已存在。之前的行为会导致同窗口内第一次带 label 的导出生成无 label
    文件名，第二次才生成带 label 的，命名不稳定且 ``export_reuse_existing``
    可能错误命中无 label 的文件。
    """
    if now is None:
        timestamp = int(time.time() * 1000)
    else:
        timestamp = now
    stem = window_export_stem(window_minutes, timestamp, server_id)
    if label:
        # 始终加入 label，使文件名对 (window, server, label) 确定。
        path = export_dir / f"{stem}_{safe_name(label)}{suffix}"
    else:
        path = export_dir / f"{stem}{suffix}"
    return path


def window_export_stem(
    window_minutes: int,
    end_timestamp: int,
    server_id: str | None = None,
) -> str:
    # PR9 hotfix v5: end_timestamp 改为毫秒级精度。
    # 之前是秒级（int(time.time())），同一秒内连续两次 /mc report now
    # 仍可能复用旧附件。改为毫秒后（int(time.time()*1000)），
    # 同一毫秒连续 report 的概率极低，误复用风险大幅降低。
    # export_reuse_existing 仍对"完全相同窗口"（如 periodic report
    # 用固定 scheduled_window_end_ms 重试）有效。
    # 兼容：若调用方传入秒级（< 10^12），自动 *1000 转毫秒。
    if end_timestamp < 10_000_000_000:  # < 10^12 → 秒级
        end_ms = end_timestamp * 1000
    else:
        end_ms = end_timestamp
    start_ms = max(0, end_ms - max(1, window_minutes) * 60 * 1000)
    start_timestamp = start_ms // 1000
    end_timestamp_s = end_ms // 1000
    start_day = time.strftime("%Y%m%d", time.localtime(start_timestamp))
    end_day = time.strftime("%Y%m%d", time.localtime(end_timestamp_s))
    start_time = time.strftime("%H%M", time.localtime(start_timestamp))
    end_time = time.strftime("%H%M", time.localtime(end_timestamp_s))
    if start_day == end_day:
        stem = f"mine_sentinel_{start_day}_{start_time}_{end_time}"
    else:
        stem = f"mine_sentinel_{start_day}_{start_time}_{end_day}_{end_time}"
    if server_id:
        stem = f"{stem}_{safe_name(server_id)}"
    # 追加毫秒级 end_timestamp，避免同秒内连续 /mc report now 复用旧附件。
    stem = f"{stem}_t{int(end_ms)}"
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
    # 注意：observation 按天分片（YYYYMMDD.jsonl），清理粒度为天。
    # 当天的文件永远不会被删（path.stem < cutoff_day 不成立），
    # 即使 retention_minutes=60，当天文件也会保留到跨天后才删。
    # export 文件按 mtime 清理，粒度为秒，不受此限制。
    # 后续若需小时级 observation 保留，应改为 hourly shard（YYYYMMDD_HH.jsonl）。
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
