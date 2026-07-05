"""Observation JSONL serialization and normalization.

Rust 加速是可选的：当 ``mine_sentinel_rs`` 平台 wheel 可导入时，热路径
(``normalize_record`` / ``record_to_json`` / ``json_line`` / ``dedupe_key``)
全部委托给原生扩展；缺失时自动降级为纯 Python 实现，插件仍可正常加载。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

try:
    from mine_sentinel_rs import ObservationRecordCodec as _RsObservationRecordCodec

    _HAS_RUST = True
except ImportError:  # pragma: no cover - 纯 Python 降级路径
    _HAS_RUST = False

from ..models import MineSentinelConfig, ObservationRecord
from .offset_index import JsonlOffsetIndex

_JSON_DUMPS = json.dumps
_JSON_LOADS = json.loads
_WS_RE = re.compile(r"\s+")


class ObservationRecordCodec:
    """Converts observation records to bounded JSONL-safe payloads.

    当 ``mine_sentinel_rs`` 可导入时，per-record 热路径全部走 Rust；否则
    使用下方纯 Python 实现。两条路径的行为应当等价（同样的截断、compact、
    dedupe key 算法），只是性能不同。
    """

    def __init__(self, config: MineSentinelConfig):
        self.config = config
        self.max_content_length = config.storage.max_content_length
        self.max_tags_per_record = config.max_tags_per_record
        self.max_raw_fields = config.max_raw_fields
        self.include_raw = config.storage.include_raw
        self.dedupe_window_seconds = max(1, config.dedupe_window_seconds)

        if _HAS_RUST:
            self._rs = _RsObservationRecordCodec(
                self.max_content_length,
                self.max_tags_per_record,
                self.max_raw_fields,
                self.include_raw,
                self.dedupe_window_seconds,
            )
        else:
            self._rs = None

    # ------------------------------------------------------------------
    # normalize_record: 原地裁剪 record 的 content/tags/context/raw
    # ------------------------------------------------------------------
    def normalize_record(self, record: ObservationRecord):
        if self._rs is not None:
            self._rs.normalize_record(record)
            return
        # 纯 Python 降级
        max_len = self.max_content_length
        record.content = self.truncate(record.content, max_len)
        record.tags = [
            self.truncate(str(tag), max_len)
            for tag in record.tags[: self.max_tags_per_record]
        ]
        record.context = self.compact_dict(record.context, self.max_raw_fields)
        record.raw = (
            self.compact_dict(record.raw, self.max_raw_fields)
            if self.include_raw
            else {}
        )

    # ------------------------------------------------------------------
    # record_to_json: 构建 JSONL-safe dict
    # ------------------------------------------------------------------
    def record_to_json(self, record: ObservationRecord) -> dict[str, Any]:
        if self._rs is not None:
            return self._rs.record_to_json(record)
        # 纯 Python 降级
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
            "raw": record.raw if self.include_raw else {},
        }

    # ------------------------------------------------------------------
    # json_line: 序列化为单行 JSON
    # ------------------------------------------------------------------
    def json_line(self, record: ObservationRecord) -> str:
        if self._rs is not None:
            return self._rs.json_line(record)
        # 纯 Python 降级
        return _JSON_DUMPS(
            self.record_to_json(record),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    # ------------------------------------------------------------------
    # JSONL 读取（不涉及热路径，始终用 Python）
    # ------------------------------------------------------------------
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
        index: JsonlOffsetIndex | None = None,
    ):
        """Yield JSONL rows whose timestamp falls in [cutoff_ms, end_ms).

        假设 JSONL 文件按 timestamp 单调递增写入（tailer 保证）。
        ``end_ms`` 是右开边界：``ts == end_ms`` 的行不包含在窗口内，
        与注释语义一致。

        当传入 ``index`` 时，先通过索引二分查找 ``cutoff_ms`` 附近的
        byte offset 再 ``seek``，避免从文件开头顺序扫描。索引是可选的：
        无索引时退化为全量扫描，行为与原来一致。
        """
        start_offset = 0
        if index is not None:
            start_offset = index.seek_offset(cutoff_ms)
        try:
            # 用二进制模式打开，以便在 text mode 下也能 seek 到任意 byte offset。
            # 不带索引时 start_offset==0，等价于从头读。
            with path.open("rb") as handle:
                if start_offset > 0:
                    handle.seek(start_offset)
                for raw_line in handle:
                    line = raw_line.decode("utf-8", errors="replace").strip()
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
                    if end_ms is not None and ts >= end_ms:
                        # 右开边界：ts == end_ms 不在窗口内。
                        # 假设文件按 timestamp 单调递增，遇到首个越界行即可 break。
                        break
                    yield data
        except FileNotFoundError:
            return

    # ------------------------------------------------------------------
    # dedupe_key: event_id 优先，否则 blake2b16(kind|server|identity|content|bucket)
    # ------------------------------------------------------------------
    def dedupe_key(self, record: ObservationRecord) -> str:
        if record.event_id:
            return record.event_id
        if self._rs is not None:
            return self._rs.dedupe_key(record)
        # 纯 Python 降级：镜像 Rust 的 blake2b16 算法
        identity = record.identity or ""
        content_lower = _WS_RE.sub(" ", record.content.lower()).strip()
        bucket = int(record.timestamp or 0) // (self.dedupe_window_seconds * 1000)
        raw = f"{record.kind}|{record.server_id}|{identity}|{content_lower}|{bucket}"
        digest = hashlib.blake2b(raw.encode("utf-8"), digest_size=16).hexdigest()
        return f"h:{digest}"

    # ------------------------------------------------------------------
    # compact / truncate 辅助（纯 Python 降级路径和外部调用共用）
    # ------------------------------------------------------------------
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
        # 嵌套 dict / list 递归 compact，保持结构化（而非 JSON dump 成字符串）。
        # 这让 context["otel"] 等嵌套字段可被 OTel-compatible 系统按字段检索。
        if isinstance(value, dict):
            return self.compact_dict(value, self.max_raw_fields)
        if isinstance(value, list):
            return [
                self.compact_value(item)
                for item in value[: self.max_raw_fields]
            ]
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

    @property
    def uses_native(self) -> bool:
        """是否正在使用 Rust 原生扩展（供诊断/日志用）。"""
        return self._rs is not None
