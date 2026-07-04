"""Record sampling strategies for bounded AI report prompts."""

from __future__ import annotations

from typing import Any

from ..models import ObservationRecord


def sample_records_for_ai(
    records: list[ObservationRecord],
    max_records: int,
    fallback: dict[str, Any] | None = None,
) -> list[ObservationRecord]:
    """Keep the AI prompt small while preserving issue-relevant log evidence."""

    if len(records) <= max_records:
        return list(records)
    if max_records <= 0:
        return []
    if max_records == 1:
        priority = _priority_records(records, fallback)
        return [priority[0][1] if priority else records[-1]]

    priority_quota = max(1, min(max_records, max_records * 2 // 3))
    selected: list[ObservationRecord] = []
    selected_ids: set[int] = set()

    for _, record in _priority_records(records, fallback)[:priority_quota]:
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
    fallback: dict[str, Any] | None,
) -> list[tuple[float, ObservationRecord]]:
    focus = _focus_from_fallback(fallback or {})
    if not any(focus.values()):
        return []

    ranked = []
    for record in records:
        score = _record_score(record, focus)
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


def _record_score(record: ObservationRecord, focus: dict[str, set[str]]) -> float:
    text = _norm(f"{record.content} {' '.join(record.tags)}")
    evidence_text = _norm(record.evidence_text())

    score = 0.0
    if record.kind == "SERVER_LOG":
        score += 1.0
        if any(
            marker in text
            for marker in (
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
        ):
            score += 4.0
    if any(term and term in text for term in focus["terms"]):
        score += 4.0
    content = _norm(record.content)
    if any(
        sample and (sample == evidence_text or (content and content in sample))
        for sample in focus["evidence"]
    ):
        score += 8.0
    if record.kind != "SERVER_LOG" and score > 0:
        score *= 0.5
    return score


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


def _norm(value: Any) -> str:
    return " ".join(str(value or "").lower().split())
