"""Observation JSONL serialization and normalization."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from mine_sentinel_rs import ObservationRecordCodec as _RsObservationRecordCodec
except ImportError as exc:  # pragma: no cover - import-time deployment guard
    raise RuntimeError(
        "mine_sentinel_rs native extension is required. Install the platform "
        "wheel built by the 'Build Rust wheels' GitHub Actions workflow."
    ) from exc

from ..models import MineSentinelConfig, ObservationRecord

_JSON_DUMPS = json.dumps
_JSON_LOADS = json.loads


class ObservationRecordCodec:
    """Converts observation records to bounded JSONL-safe payloads."""

    def __init__(self, config: MineSentinelConfig):
        self.config = config
        self.max_content_length = config.storage.max_content_length
        self.max_tags_per_record = config.max_tags_per_record
        self.max_metric_fields = config.max_metric_fields
        self.max_raw_fields = config.max_raw_fields
        self.include_raw = config.storage.include_raw
        self._rs = _RsObservationRecordCodec(
            self.max_content_length,
            self.max_tags_per_record,
            self.max_metric_fields,
            self.max_raw_fields,
            self.include_raw,
            config.dedupe_window_seconds,
        )
        self._rs_dedupe_key = self._rs.dedupe_key

    def normalize_record(self, record: ObservationRecord):
        max_len = self.max_content_length
        record.content = self.truncate(record.content, max_len)
        record.tags = [
            self.truncate(str(tag), max_len)
            for tag in record.tags[: self.max_tags_per_record]
        ]
        record.metrics = self.compact_dict(record.metrics, self.max_metric_fields)
        record.context = self.compact_dict(record.context, self.max_raw_fields)
        record.raw = (
            self.compact_dict(record.raw, self.max_raw_fields)
            if self.include_raw
            else {}
        )

    def record_to_json(self, record: ObservationRecord) -> dict[str, Any]:
        return {
            "eventId": record.event_id,
            "kind": record.kind,
            "timestamp": record.timestamp,
            "serverId": record.server_id,
            "serverName": record.server_name,
            "backendServer": record.backend_server,
            "proxyId": record.proxy_id,
            "player": {
                "name": record.player_name,
                "uuidHash": record.player_uuid_hash,
            },
            "content": record.content,
            "tags": record.tags,
            "context": record.context,
            "metrics": record.metrics,
            "raw": record.raw if self.include_raw else {},
        }

    def json_line(self, record: ObservationRecord) -> str:
        return _JSON_DUMPS(
            self.record_to_json(record),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def read_jsonl(self, path: Path):
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = _JSON_LOADS(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data, dict):
                        yield data
        except FileNotFoundError:
            return

    def read_jsonl_window(
        self,
        path: Path,
        cutoff_ms: int,
        end_ms: int | None = None,
    ):
        """Yield JSONL rows whose timestamp falls in [cutoff_ms, end_ms)."""
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = _JSON_LOADS(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(data, dict):
                        continue
                    ts = data.get("timestamp")
                    if not isinstance(ts, (int, float)):
                        yield data
                        continue
                    if ts < cutoff_ms:
                        continue
                    if end_ms is not None and ts > end_ms:
                        break
                    yield data
        except FileNotFoundError:
            return

    def dedupe_key(self, record: ObservationRecord) -> str:
        if record.event_id:
            return record.event_id
        return self._rs_dedupe_key(record)

    def compact_dict(self, data: dict[str, Any], max_fields: int) -> dict[str, Any]:
        compact: dict[str, Any] = {}
        for index, (key, value) in enumerate((data or {}).items()):
            if index >= max_fields:
                break
            compact[str(key)] = self.compact_value(value)
        return compact

    def compact_value(self, value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return self.truncate(value, self.max_content_length)
        try:
            text = _JSON_DUMPS(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
        return self.truncate(text, self.max_content_length)

    @staticmethod
    def truncate(value: str, max_length: int) -> str:
        if max_length <= 0:
            return ""
        if len(value) <= max_length:
            return value
        if max_length <= 3:
            return value[:max_length]
        return value[: max_length - 3] + "..."
