"""Record sampling strategies for bounded AI report prompts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import ObservationRecord

try:
    from mine_sentinel_rs import ai_sampling_features_batch as _rs_ai_sampling_features_batch

    _HAS_RUST_SAMPLING_FEATURES = True
except ImportError:  # pragma: no cover - optional native acceleration
    _rs_ai_sampling_features_batch = None
    _HAS_RUST_SAMPLING_FEATURES = False


LOW_VALUE_AI_OPS_CATEGORIES = {"启动与关闭", "指标观察"}
LOW_VALUE_AI_TEXT_MARKERS = (
    "repair of failed migration",
    "no failed migration detected",
    "unknown or incomplete command",
)
AI_SCORE_MARKERS = (
    "error",
    "exception",
    "failed",
    "fatal",
    "severe",
    "warn",
    "warning",
    "loop_suppressed",
    "报错",
    "异常",
    "失败",
    "超时",
)


@dataclass(slots=True)
class _SampleFeature:
    clean_key: str
    text: str
    evidence_text: str
    content_text: str
    low_value: bool
    quality: int
    timestamp: int
    is_server_log: bool
    anomaly_spike: bool
    new_template: bool
    daily_noise: bool


def sample_records_for_ai(
    records: list[ObservationRecord],
    max_records: int,
    fallback: dict[str, Any] | None = None,
) -> list[ObservationRecord]:
    """Keep the AI prompt small while preserving issue-relevant log evidence."""

    if max_records <= 0:
        return []
    focus = _focus_from_fallback(fallback or {})
    features = _feature_map(records)
    records = _dedupe_clean_records(records, focus, features)
    focus_records = [
        record
        for record in records
        if not _feature_for(features, record).low_value
    ]
    if len(focus_records) >= max_records:
        records = focus_records
    if len(records) <= max_records:
        return list(records)
    if max_records == 1:
        priority = _priority_records(records, focus, features)
        return [priority[0][1] if priority else records[-1]]

    priority_quota = max(1, min(max_records, max_records * 2 // 3))
    selected: list[ObservationRecord] = []
    selected_ids: set[int] = set()

    for _, record in _priority_records(records, focus, features)[:priority_quota]:
        _add_record(selected, selected_ids, record)

    remaining = max_records - len(selected)
    if remaining > 0:
        for record in even_sample(records, remaining + 2):
            if _add_record(selected, selected_ids, record) and len(selected) >= max_records:
                break

    if len(selected) < max_records:
        for record in records:
            if _add_record(selected, selected_ids, record) and len(selected) >= max_records:
                break

    order = {id(record): index for index, record in enumerate(records)}
    selected.sort(key=lambda record: (record.timestamp, order.get(id(record), 0)))
    return selected[:max_records]


def _dedupe_clean_records(
    records: list[ObservationRecord],
    focus: dict[str, set[str]],
    features: dict[int, _SampleFeature] | None = None,
) -> list[ObservationRecord]:
    """Drop exact duplicate cleaned prompt texts before token-budget sampling."""

    if len(records) <= 1:
        return list(records)
    chosen: dict[str, ObservationRecord] = {}
    rank_cache: dict[int, tuple[float, int, int]] = {}
    passthrough: list[ObservationRecord] = []

    def rank(record: ObservationRecord) -> tuple[float, int, int]:
        key = id(record)
        cached = rank_cache.get(key)
        if cached is None:
            cached = _dedupe_rank(record, focus, features)
            rank_cache[key] = cached
        return cached

    for index, record in enumerate(records):
        key = _clean_key(record, features)
        if not key:
            passthrough.append(record)
            continue
        current = chosen.get(key)
        if current is None:
            chosen[key] = record
            continue
        if rank(record) > rank(current):
            chosen[key] = record

    deduped = passthrough + list(chosen.values())
    original_order = {id(record): index for index, record in enumerate(records)}
    deduped.sort(key=lambda record: (record.timestamp, original_order.get(id(record), 0)))
    return deduped


def _clean_key(
    record: ObservationRecord,
    features: dict[int, _SampleFeature] | None = None,
) -> str:
    if features is not None:
        return _feature_for(features, record).clean_key
    ctx = record.context or {}
    value = str(ctx.get("llmCleanHash") or "").strip()
    if value:
        return value
    clean_text = str(ctx.get("llmCleanText") or "").strip()
    if clean_text:
        return _norm(clean_text)
    return _norm(record.content)


def _dedupe_rank(
    record: ObservationRecord,
    focus: dict[str, set[str]],
    features: dict[int, _SampleFeature] | None = None,
) -> tuple[float, int, int]:
    feature = features.get(id(record)) if features else None
    if feature is not None:
        score = _record_score(feature, focus)
        if feature.anomaly_spike:
            score += 5.0
        if feature.new_template:
            score += 3.0
        if feature.daily_noise:
            score -= 2.0
        return score, feature.quality, feature.timestamp

    score = _python_record_score(record, focus)
    if "anomaly_spike" in (record.tags or ()):
        score += 5.0
    if "new_template" in (record.tags or ()):
        score += 3.0
    if "daily_noise" in (record.tags or ()):
        score -= 2.0
    try:
        quality = int((record.context or {}).get("llmQualityScore"))
    except (TypeError, ValueError):
        quality = 50
    return score, quality, int(record.timestamp or 0)


def even_sample(items: list[Any], max_items: int) -> list[Any]:
    if len(items) <= max_items:
        return list(items)
    if max_items <= 0:
        return []
    if max_items == 1:
        return [items[-1]]
    step = (len(items) - 1) / (max_items - 1)
    return [items[round(index * step)] for index in range(max_items)]


def _priority_records(
    records: list[ObservationRecord],
    focus: dict[str, set[str]],
    features: dict[int, _SampleFeature] | None = None,
) -> list[tuple[float, ObservationRecord]]:
    if not any(focus.values()):
        return []

    ranked = []
    for record in records:
        feature = features.get(id(record)) if features else None
        score = (
            _record_score(feature, focus)
            if feature is not None
            else _python_record_score(record, focus)
        )
        if score > 0:
            ranked.append((score, record))
    ranked.sort(key=lambda item: (-item[0], item[1].timestamp))
    return ranked


def _focus_from_fallback(fallback: dict[str, Any]) -> dict[str, set[str]]:
    terms: set[str] = set()
    evidence: set[str] = set()
    for issue in fallback.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        for term in issue.get("issue_terms") or []:
            value = _norm(term)
            if value:
                terms.add(value)
        tag = _norm(issue.get("tag", ""))
        if tag:
            terms.update(part for part in tag.replace("_", " ").split() if part)
        for sample in issue.get("evidence_samples") or []:
            value = _norm(sample)
            if value:
                evidence.add(value)

    return {
        "terms": terms,
        "evidence": evidence,
    }


def _record_score(feature: _SampleFeature, focus: dict[str, set[str]]) -> float:
    score = 0.0
    if feature.is_server_log:
        score += 1.0
        if any(marker in feature.text for marker in AI_SCORE_MARKERS):
            score += 4.0
    if any(term and term in feature.text for term in focus["terms"]):
        score += 4.0
    if any(
        sample and (
            sample == feature.evidence_text
            or (feature.content_text and feature.content_text in sample)
        )
        for sample in focus["evidence"]
    ):
        score += 8.0
    if not feature.is_server_log and score > 0:
        score *= 0.5
    return score


def _python_record_score(record: ObservationRecord, focus: dict[str, set[str]]) -> float:
    text = _norm(f"{record.content} {' '.join(record.tags or [])}")

    score = 0.0
    if record.kind == "SERVER_LOG":
        score += 1.0
        if any(marker in text for marker in AI_SCORE_MARKERS):
            score += 4.0
    if any(term and term in text for term in focus["terms"]):
        score += 4.0
    elif focus["terms"]:
        context_text = _norm(" ".join(_context_terms_for_sampling(record.context or {})))
        if context_text and any(term and term in context_text for term in focus["terms"]):
            score += 4.0
    if focus["evidence"]:
        evidence_text = _norm(record.evidence_text())
        content = _norm(record.content)
        if any(
            sample and (sample == evidence_text or (content and content in sample))
            for sample in focus["evidence"]
        ):
            score += 8.0
    if record.kind != "SERVER_LOG" and score > 0:
        score *= 0.5
    return score


def _low_value_for_ai(
    record: ObservationRecord,
    features: dict[int, _SampleFeature] | None = None,
) -> bool:
    if features:
        feature = features.get(id(record))
        if feature is not None:
            return feature.low_value
    if "daily_noise" in (record.tags or ()):
        return True
    text = _norm(f"{record.content} {' '.join(record.tags or [])}")
    if any(marker in text for marker in LOW_VALUE_AI_TEXT_MARKERS):
        return True
    ops = (record.context or {}).get("opsClassification")
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
            return True
        if (
            category in LOW_VALUE_AI_OPS_CATEGORIES
            and not needs_admin
            and severity in {"", "info", "low"}
        ):
            return True
    return False


def _add_record(
    selected: list[ObservationRecord],
    selected_ids: set[int],
    record: ObservationRecord,
) -> bool:
    key = id(record)
    if key in selected_ids:
        return False
    selected.append(record)
    selected_ids.add(key)
    return True


def _feature_map(records: list[ObservationRecord]) -> dict[int, _SampleFeature]:
    if not records:
        return {}
    if _HAS_RUST_SAMPLING_FEATURES and _rs_ai_sampling_features_batch is not None:
        try:
            rows = _rs_ai_sampling_features_batch(records)
            if len(rows) == len(records):
                return {
                    id(record): _feature_from_row(row)
                    for record, row in zip(records, rows, strict=True)
                }
        except Exception:
            pass
    return {}


def _feature_for(
    features: dict[int, _SampleFeature] | None,
    record: ObservationRecord,
) -> _SampleFeature:
    if features:
        feature = features.get(id(record))
        if feature is not None:
            return feature
    feature = _python_feature(record)
    if features is not None:
        features[id(record)] = feature
    return feature


def _feature_from_row(row: Any) -> _SampleFeature:
    return _SampleFeature(
        clean_key=str(row[0] or ""),
        text=str(row[1] or ""),
        evidence_text=str(row[2] or ""),
        content_text=str(row[3] or ""),
        low_value=bool(row[4]),
        quality=int(row[5]),
        timestamp=int(row[6]),
        is_server_log=bool(row[7]),
        anomaly_spike=bool(row[8]),
        new_template=bool(row[9]),
        daily_noise=bool(row[10]),
    )


def _python_feature(record: ObservationRecord) -> _SampleFeature:
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
    context_text = " ".join(_context_terms_for_sampling(ctx))
    text = _norm(f"{record.content} {' '.join(tags)} {context_text}")
    source = record.backend_server or record.server_id
    player = f"{record.player_name}: " if record.player_name else ""
    evidence_text = _norm(f"[{source}] {player}{record.content}".strip())
    daily_noise = "daily_noise" in tags
    low_value = daily_noise or any(marker in text for marker in LOW_VALUE_AI_TEXT_MARKERS)
    ops = ctx.get("opsClassification")
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
            category in LOW_VALUE_AI_OPS_CATEGORIES
            and not needs_admin
            and severity in {"", "info", "low"}
        ):
            low_value = True
    try:
        quality = int(ctx.get("llmQualityScore"))
    except (TypeError, ValueError):
        quality = 50
    return _SampleFeature(
        clean_key=clean_key,
        text=text,
        evidence_text=evidence_text,
        content_text=_norm(record.content),
        low_value=low_value,
        quality=quality,
        timestamp=int(record.timestamp or 0),
        is_server_log=record.kind == "SERVER_LOG",
        anomaly_spike="anomaly_spike" in tags,
        new_template="new_template" in tags,
        daily_noise=daily_noise,
    )


def _context_terms_for_sampling(ctx: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    hint_code = str(ctx.get("opsHintCode") or "").strip()
    if hint_code:
        terms.append(hint_code)
    hint_severity = str(ctx.get("opsHintSeverity") or "").strip()
    if hint_severity:
        terms.append(hint_severity)
    ops = ctx.get("opsClassification")
    if isinstance(ops, dict):
        for key in ("category", "subtype", "severity"):
            value = str(ops.get(key) or "").strip()
            if value:
                terms.append(value)
        if ops.get("needs_admin"):
            terms.append("needs_admin")
        if ops.get("opsObservation"):
            terms.append("ops_observation")
    return terms


def _norm(value: Any) -> str:
    return " ".join(str(value or "").lower().split())
