from __future__ import annotations

import hashlib
import json
import re
import sys
import types


_WS_RE = re.compile(r"\s+")

LOW_VALUE_AI_TEXT_MARKERS = (
    "repair of failed migration",
    "no failed migration detected",
    "unknown or incomplete command",
)


def _norm(value) -> str:
    return _WS_RE.sub(" ", str(value or "").lower()).strip()


class ObservationRecordCodecStub:
    """Pure Python test double for mine_sentinel_rs.ObservationRecordCodec."""

    def __init__(
        self,
        max_content_length,
        max_tags_per_record,
        max_raw_fields,
        include_raw,
        dedupe_window_seconds,
    ):
        self.max_content_length = int(max_content_length)
        self.max_tags_per_record = int(max_tags_per_record)
        self.max_raw_fields = int(max_raw_fields)
        self.include_raw = bool(include_raw)
        self.dedupe_window_seconds = max(1, int(dedupe_window_seconds))

    @staticmethod
    def _truncate(value, max_length):
        if max_length <= 0:
            return ""
        if len(value) <= max_length:
            return value
        if max_length <= 3:
            return value[:max_length]
        return value[: max_length - 3] + "..."

    def _compact_value(self, value):
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return self._truncate(value, self.max_content_length)
        if isinstance(value, dict):
            return self._compact_dict(value, self.max_raw_fields)
        if isinstance(value, list):
            return [self._compact_value(v) for v in value[: self.max_raw_fields]]
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
        return self._truncate(text, self.max_content_length)

    def _compact_dict(self, data, max_fields):
        compact = {}
        for index, (key, value) in enumerate((data or {}).items()):
            if index >= max_fields:
                break
            compact[str(key)] = self._compact_value(value)
        return compact

    def normalize_record(self, record):
        record.content = self._truncate(record.content, self.max_content_length)
        record.tags = [
            self._truncate(str(tag), self.max_content_length)
            for tag in record.tags[: self.max_tags_per_record]
        ]
        record.context = self._compact_dict(record.context, self.max_raw_fields)
        record.raw = (
            self._compact_dict(record.raw, self.max_raw_fields)
            if self.include_raw
            else {}
        )

    def record_to_json(self, record):
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

    def json_line(self, record):
        return json.dumps(
            self.record_to_json(record),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def dedupe_key(self, record):
        if record.event_id:
            return record.event_id
        identity = record.identity or ""
        content_lower = _WS_RE.sub(" ", record.content.lower()).strip()
        bucket = int(record.timestamp or 0) // (self.dedupe_window_seconds * 1000)
        raw = f"{record.kind}|{record.server_id}|{identity}|{content_lower}|{bucket}"
        digest = hashlib.blake2b(raw.encode("utf-8"), digest_size=16).hexdigest()
        return f"h:{digest}"


def observation_priority_score_stub(record, matcher=None):
    if record.kind != "SERVER_LOG":
        return 0.0
    text = f"{record.content} {' '.join(record.tags)}".lower()
    if any(
        marker in text
        for marker in (
            "loop_suppressed",
            "fatal",
            "severe",
            "error",
            "exception",
            "failed",
            "timeout",
            "warn",
            "warning",
            "ban",
            "kick",
            "mute",
            "report",
            "spam",
            "grief",
            "cheat",
        )
    ):
        return 5.0
    return 1.0


def ai_sampling_features_batch_stub(records):
    out = []
    for record in records:
        ctx = record.context or {}
        tags = tuple(str(tag) for tag in (record.tags or []))
        clean_hash = str(ctx.get("llmCleanHash") or "").strip()
        clean_text = str(ctx.get("llmCleanText") or "").strip()
        if clean_hash:
            clean_key = clean_hash
        elif clean_text:
            clean_key = _norm(clean_text)
        else:
            clean_key = _norm(record.content)
        context_terms = []
        for key in ("opsHintCode", "opsHintSeverity"):
            value = str(ctx.get(key) or "").strip()
            if value:
                context_terms.append(value)
        ops = ctx.get("opsClassification")
        if isinstance(ops, dict):
            for key in ("category", "subtype", "severity"):
                value = str(ops.get(key) or "").strip()
                if value:
                    context_terms.append(value)
            if ops.get("needs_admin"):
                context_terms.append("needs_admin")
            if ops.get("opsObservation"):
                context_terms.append("ops_observation")
        text = _norm(f"{record.content} {' '.join(tags)} {' '.join(context_terms)}")
        source = record.backend_server or record.server_id
        player = f"{record.player_name}: " if record.player_name else ""
        evidence_text = _norm(f"[{source}] {player}{record.content}".strip())
        daily_noise = "daily_noise" in tags
        low_value = daily_noise or any(marker in text for marker in LOW_VALUE_AI_TEXT_MARKERS)
        if isinstance(ops, dict):
            category = str(ops.get("category") or "")
            severity = str(ops.get("severity") or "").lower()
            needs_admin = bool(ops.get("needs_admin"))
            ops_observation = bool(ops.get("opsObservation"))
            if (
                ops_observation
                and not needs_admin
                and severity in {"", "info", "low"}
            ):
                low_value = True
            if (
                category in {"启动与关闭", "指标观察"}
                and not needs_admin
                and severity in {"", "info", "low"}
            ):
                low_value = True
        try:
            quality = int(ctx.get("llmQualityScore"))
        except (TypeError, ValueError):
            quality = 50
        out.append(
            (
                clean_key,
                text,
                evidence_text,
                _norm(record.content),
                low_value,
                quality,
                int(record.timestamp or 0),
                record.kind == "SERVER_LOG",
                "anomaly_spike" in tags,
                "new_template" in tags,
                daily_noise,
            )
        )
    return out


def install_mine_sentinel_rs_stub_if_missing() -> bool:
    try:
        import mine_sentinel_rs  # noqa: F401
    except ImportError:
        native_stub = types.ModuleType("mine_sentinel_rs")
        native_stub._is_stub = True
        native_stub.ObservationRecordCodec = ObservationRecordCodecStub
        native_stub.observation_priority_score = observation_priority_score_stub
        native_stub.ai_sampling_features_batch = ai_sampling_features_batch_stub
        sys.modules["mine_sentinel_rs"] = native_stub
        return True
    return False
