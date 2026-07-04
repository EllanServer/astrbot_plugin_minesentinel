"""Disk-backed MineSentinel observation store."""

from __future__ import annotations

import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from ..models import MineSentinelConfig, ObservationRecord
from .codec import ObservationRecordCodec
from .dedupe import DedupeTracker
from .models import RecentObservationWindow
from .paths import (
    candidate_files,
    cleanup_old_files,
    export_path,
    record_path,
    safe_name,
)
from .window import RecentWindowBuilder
from ..reporting.dialogue_rules import dialogue_rules_from_config


class DiskObservationStore:
    """Append-only JSONL store used as the complete report source."""

    def __init__(self, config: MineSentinelConfig, root_dir: Path):
        self.config = config
        self.root_dir = root_dir
        self.observation_dir = root_dir / "observations"
        self.export_dir = root_dir / "exports"
        self.codec = ObservationRecordCodec(config)
        self.observation_dir.mkdir(parents=True, exist_ok=True)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self._last_cleanup_at: float | None = None
        # Short-lived cache for the most recent window read, so that an alert
        # triggered right after a periodic report (or vice versa) does not scan
        # disk twice for the same window. Key: (window_minutes, server_id).
        self._window_cache_key: tuple[int, str | None] | None = None
        self._window_cache_value: RecentObservationWindow | None = None
        self._window_cache_at: float = 0.0
        self._window_cache_ttl: float = 30.0

    def add_batch(self, server_id: str, payload: dict[str, Any]) -> int:
        if not self.config.enabled or not self.config.storage.enabled:
            return 0

        observations = payload.get("observations") or []
        if not isinstance(observations, list):
            return 0

        now = time.time()
        cutoff_ms = int((now - self.config.storage.retention_minutes * 60) * 1000)
        batch_server_id = str(payload.get("serverId") or server_id)
        batch_server_name = str(payload.get("serverName") or batch_server_id)

        written = 0
        handles = {}
        with ExitStack() as stack:
            for item in observations:
                if not isinstance(item, dict):
                    continue
                record = ObservationRecord.from_dict(
                    item,
                    batch_server_id,
                    batch_server_name,
                )
                if not record.server_id:
                    record.server_id = batch_server_id
                if record.timestamp and record.timestamp < cutoff_ms:
                    continue
                self.codec.normalize_record(record)
                path = self._record_path(record)
                handle = handles.get(path)
                if handle is None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    handle = stack.enter_context(path.open("a", encoding="utf-8"))
                    handles[path] = handle
                handle.write(self.codec.json_line(record))
                handle.write("\n")
                written += 1

        self.cleanup_if_due(now)
        # New observations invalidate the cached window read.
        self._window_cache_key = None
        self._window_cache_value = None
        return written

    def recent(
        self,
        window_minutes: int,
        server_id: str | None = None,
    ) -> list[ObservationRecord]:
        return self.recent_window(window_minutes, server_id).records

    def recent_window(
        self,
        window_minutes: int,
        server_id: str | None = None,
        max_records: int | None = None,
    ) -> RecentObservationWindow:
        if not self.config.enabled or not self.config.storage.enabled:
            return RecentObservationWindow([], 0, 0, False, 0)

        cache_key = (window_minutes, server_id)
        now = time.time()
        if (
            self._window_cache_key == cache_key
            and self._window_cache_value is not None
            and now - self._window_cache_at < self._window_cache_ttl
            and max_records is None
        ):
            return self._window_cache_value

        cutoff_ms = int((now - window_minutes * 60) * 1000)
        # window end bound = now; lets read_jsonl_window stop scanning a file
        # once it encounters records beyond the window upper bound.
        end_ms = int(now * 1000)
        limit = max(1, max_records or self.config.report.max_records_in_memory)
        builder = RecentWindowBuilder(
            limit,
            dialogue_rules_from_config(self.config.dialogue.custom_rules),
        )
        with self._dedupe_tracker() as seen:
            for path in self._candidate_files(server_id, cutoff_ms):
                for row in self.codec.read_jsonl_window(path, cutoff_ms, end_ms):
                    record = ObservationRecord.from_dict(row)
                    if record.timestamp < cutoff_ms:
                        continue
                    if record.timestamp > end_ms:
                        continue
                    key = self.codec.dedupe_key(record)
                    if seen.seen_or_add(key):
                        continue
                    builder.add(record)
        result = builder.build()

        if max_records is None:
            self._window_cache_key = cache_key
            self._window_cache_value = result
            self._window_cache_at = now
        return result

    def export_records(
        self,
        records: list[ObservationRecord],
        window_minutes: int,
        server_id: str | None = None,
        label: str = "",
    ) -> Path | None:
        if not records:
            return None
        now = int(time.time())
        path = export_path(self.export_dir, window_minutes, server_id, label, now)
        with path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(self.codec.json_line(record))
                handle.write("\n")
        return path

    def export_recent(
        self,
        window_minutes: int,
        server_id: str | None = None,
        label: str = "",
    ) -> Path | None:
        if not self.config.enabled or not self.config.storage.enabled:
            return None

        now_ts = int(time.time())
        cutoff_ms = int((now_ts - window_minutes * 60) * 1000)
        end_ms = int(now_ts * 1000)
        path = export_path(self.export_dir, window_minutes, server_id, label, now_ts)

        written = 0
        with self._dedupe_tracker() as seen:
            with path.open("w", encoding="utf-8") as handle:
                for source_path in self._candidate_files(server_id, cutoff_ms):
                    for row in self.codec.read_jsonl_window(source_path, cutoff_ms, end_ms):
                        record = ObservationRecord.from_dict(row)
                        if record.timestamp < cutoff_ms:
                            continue
                        if record.timestamp > end_ms:
                            continue
                        key = self.codec.dedupe_key(record)
                        if seen.seen_or_add(key):
                            continue
                        handle.write(self.codec.json_line(record))
                        handle.write("\n")
                        written += 1
        if not written:
            path.unlink(missing_ok=True)
            return None
        return path

    def cleanup_if_due(self, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        interval = max(0, self.config.storage.cleanup_interval_seconds)
        if (
            interval > 0
            and self._last_cleanup_at is not None
            and current - self._last_cleanup_at < interval
        ):
            return False
        self.cleanup()
        self._last_cleanup_at = current
        return True

    def cleanup(self):
        cleanup_old_files(
            self.observation_dir,
            self.export_dir,
            self.config.storage.retention_minutes,
        )

    def _record_path(self, record: ObservationRecord) -> Path:
        return record_path(self.observation_dir, record)

    def _candidate_files(
        self,
        server_id: str | None,
        cutoff_ms: int | None = None,
    ) -> list[Path]:
        return candidate_files(self.observation_dir, server_id, cutoff_ms)

    def _read_jsonl(self, path: Path):
        yield from self.codec.read_jsonl(path)

    def _normalize_record(self, record: ObservationRecord):
        self.codec.normalize_record(record)

    def _record_to_json(self, record: ObservationRecord) -> dict[str, Any]:
        return self.codec.record_to_json(record)

    def _compact_dict(self, data: dict[str, Any], max_fields: int) -> dict[str, Any]:
        return self.codec.compact_dict(data, max_fields)

    def _compact_value(self, value: Any) -> Any:
        return self.codec.compact_value(value)

    def _dedupe_key(self, record: ObservationRecord) -> str:
        return self.codec.dedupe_key(record)

    def _dedupe_tracker(self) -> DedupeTracker:
        return DedupeTracker(
            max_memory_keys=self.config.storage.dedupe_memory_limit,
            temp_dir=self.root_dir / "tmp",
        )

    @staticmethod
    def _safe_name(value: str) -> str:
        return safe_name(value)

    @staticmethod
    def _truncate(value: str, max_length: int) -> str:
        return ObservationRecordCodec.truncate(value, max_length)
