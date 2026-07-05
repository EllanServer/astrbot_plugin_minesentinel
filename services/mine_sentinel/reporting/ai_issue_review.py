"""AI false-positive review for candidate MineSentinel issues."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from ..models import MineSentinelConfig, ObservationRecord
from .ai_normalizer import parse_json_object, repair_json_object_text
from .ai_prompt import truncate


CONTEXT_RADIUS = 20
MAX_REVIEW_ISSUES = 12
MAX_HIT_WINDOWS_PER_ISSUE = 3
MAX_RECORD_CONTENT_CHARS = 360
MAX_REVIEW_PROMPT_CHARS = 80_000
DROP_CONFIDENCE_THRESHOLD = 0.65

_DROP_DECISIONS = {"drop", "false_positive", "false-positive", "ignore", "discard"}


class AIIssueReviewer:
    """Builds bounded +/-20-record review prompts and filters false positives."""

    def __init__(self, config: MineSentinelConfig):
        self.config = config

    def build_prompt(
        self,
        records: list[ObservationRecord],
        fallback: dict[str, Any],
    ) -> str:
        payload = self.build_payload(records, fallback)
        if not payload.get("issues"):
            return ""
        return self._fit_prompt(payload)

    def build_prompts(
        self,
        records: list[ObservationRecord],
        fallback: dict[str, Any],
    ) -> list[str]:
        payload = self.build_payload(records, fallback)
        issues = payload.get("issues") or []
        prompts: list[str] = []
        for issue in issues:
            issue_payload = dict(payload)
            issue_payload["issues"] = [issue]
            issue_payload["unreviewed_issue_count"] = max(0, len(issues) - 1)
            prompt = self._fit_prompt(issue_payload)
            if prompt:
                prompts.append(prompt)
        return prompts

    def parse_review_decisions(self, raw_text: str) -> list[dict[str, Any]]:
        data = parse_json_object(raw_text)
        if not data:
            repaired = repair_json_object_text(raw_text)
            data = parse_json_object(repaired) if repaired else None
        raw_decisions = data.get("issues") if isinstance(data, dict) else None
        if not isinstance(raw_decisions, list):
            return []
        decisions: list[dict[str, Any]] = []
        for item in raw_decisions:
            if isinstance(item, dict):
                decisions.append(item)
        return decisions

    def build_payload(
        self,
        records: list[ObservationRecord],
        fallback: dict[str, Any],
    ) -> dict[str, Any]:
        issues = [
            issue for issue in (fallback.get("issues") or []) if isinstance(issue, dict)
        ]
        reviewed_issues = [
            self._compact_issue(index, issue, records)
            for index, issue in enumerate(issues[:MAX_REVIEW_ISSUES])
        ]
        return {
            "task": (
                "Review MineSentinel candidate issues. Each context array contains "
                "the matched evidence record plus up to 20 records before and after it. "
                "Return drop only for clear false positives; keep when uncertain."
            ),
            "context_radius": CONTEXT_RADIUS,
            "drop_confidence_threshold": DROP_CONFIDENCE_THRESHOLD,
            "issues": reviewed_issues,
            "unreviewed_issue_count": max(0, len(issues) - len(reviewed_issues)),
        }

    def apply_review(
        self,
        raw_text: str,
        fallback: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        raw_decisions = self.parse_review_decisions(raw_text)
        if not raw_decisions:
            return fallback, False

        decisions_by_index = self._decisions_by_index(raw_decisions)
        if not decisions_by_index:
            return fallback, False

        issues = [
            issue for issue in (fallback.get("issues") or []) if isinstance(issue, dict)
        ]
        kept: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        dropped_count = 0

        for index, issue in enumerate(issues):
            decision = decisions_by_index.get(index)
            should_drop = self._should_drop(decision)
            if decision:
                decisions.append(
                    {
                        "index": index,
                        "decision": str(decision.get("decision") or "keep"),
                        "confidence": _as_float(decision.get("confidence"), 0.0),
                        "reason": truncate(str(decision.get("reason") or ""), 240),
                        "dropped": should_drop,
                    }
                )
            if should_drop:
                dropped_count += 1
                continue
            kept.append(issue)

        reviewed = deepcopy(fallback)
        reviewed["issues"] = kept
        if dropped_count > 0:
            self._prune_categories_for_dropped_issues(reviewed, issues, kept, decisions)
        reviewed["ai_issue_review"] = {
            "enabled": True,
            "context_radius": CONTEXT_RADIUS,
            "reviewed": len(decisions),
            "dropped": dropped_count,
            "decisions": decisions,
        }
        if dropped_count > 0 and not kept:
            reviewed["incident_findings"] = []
        return reviewed, True

    def _fit_prompt(self, payload: dict[str, Any]) -> str:
        max_chars = min(
            MAX_REVIEW_PROMPT_CHARS,
            max(10_000, int(getattr(self.config.report, "max_ai_prompt_chars", 0) or 0)),
        )
        current = deepcopy(payload)
        while True:
            prompt = _review_prompt_text(current)
            if len(prompt) <= max_chars:
                return prompt
            issues = current.get("issues") or []
            if len(issues) > 1:
                current["issues"] = issues[:-1]
                current["unreviewed_issue_count"] = (
                    int(current.get("unreviewed_issue_count") or 0) + 1
                )
                continue
            for issue in issues:
                context = issue.get("context") or []
                if len(context) > 9:
                    hits = [item for item in context if item.get("hit")]
                    others = [item for item in context if not item.get("hit")]
                    issue["context"] = sorted(
                        hits + others[::2],
                        key=lambda item: int(item.get("record_index") or 0),
                    )
                    break
            else:
                return prompt[:max_chars]

    def _compact_issue(
        self,
        index: int,
        issue: dict[str, Any],
        records: list[ObservationRecord],
    ) -> dict[str, Any]:
        samples = [str(sample) for sample in (issue.get("evidence_samples") or []) if sample]
        hit_indexes = self._find_hit_indexes(records, issue, samples)
        return {
            "index": index,
            "category": issue.get("category"),
            "tag": issue.get("tag"),
            "severity": issue.get("severity"),
            "affected_locations": (issue.get("affected_locations") or [])[:6],
            "issue_terms": (issue.get("issue_terms") or [])[:10],
            "players": (issue.get("players") or [])[:8],
            "first_seen_ts": issue.get("first_seen_ts"),
            "last_seen_ts": issue.get("last_seen_ts"),
            "evidence_samples": [truncate(sample, 420) for sample in samples[:4]],
            "context": self._context_window(records, hit_indexes),
        }

    def _find_hit_indexes(
        self,
        records: list[ObservationRecord],
        issue: dict[str, Any],
        samples: list[str],
    ) -> list[int]:
        hits: list[int] = []
        evidence_index = {
            _normalize_text(record.evidence_text()): index
            for index, record in enumerate(records)
        }
        content_index = [
            (_normalize_text(record.content), index)
            for index, record in enumerate(records)
            if record.content
        ]

        for sample in samples:
            for line in _sample_lines(sample):
                normalized = _normalize_text(line)
                index = evidence_index.get(normalized)
                if index is not None:
                    _append_unique_int(hits, index)
                    continue
                for content, record_index in content_index:
                    if content and (content in normalized or normalized in content):
                        _append_unique_int(hits, record_index)
                        break

        if hits:
            return sorted(hits)[:MAX_HIT_WINDOWS_PER_ISSUE]

        first_ts = _as_int(issue.get("first_seen_ts"))
        last_ts = _as_int(issue.get("last_seen_ts"))
        if first_ts is None and last_ts is None:
            return []
        if first_ts is None:
            first_ts = last_ts
        if last_ts is None:
            last_ts = first_ts
        assert first_ts is not None and last_ts is not None
        low = min(first_ts, last_ts)
        high = max(first_ts, last_ts)
        for index, record in enumerate(records):
            if low <= int(record.timestamp or 0) <= high:
                _append_unique_int(hits, index)
                if len(hits) >= MAX_HIT_WINDOWS_PER_ISSUE:
                    break
        return hits

    def _context_window(
        self,
        records: list[ObservationRecord],
        hit_indexes: list[int],
    ) -> list[dict[str, Any]]:
        if not hit_indexes:
            return []
        selected: set[int] = set()
        for hit_index in hit_indexes[:MAX_HIT_WINDOWS_PER_ISSUE]:
            start = max(0, hit_index - CONTEXT_RADIUS)
            end = min(len(records), hit_index + CONTEXT_RADIUS + 1)
            selected.update(range(start, end))
        ordered_hits = sorted(hit_indexes[:MAX_HIT_WINDOWS_PER_ISSUE])
        return [
            self._compact_record(records[index], index, ordered_hits)
            for index in sorted(selected)
        ]

    def _compact_record(
        self,
        record: ObservationRecord,
        index: int,
        hit_indexes: list[int],
    ) -> dict[str, Any]:
        context = record.context or {}
        nearest_hit = min(hit_indexes, key=lambda hit: abs(hit - index))
        player = (
            record.player_name
            or str(context.get("chatPlayer") or "")
            or str(context.get("player") or "")
        )
        item: dict[str, Any] = {
            "record_index": index,
            "offset": index - nearest_hit,
            "hit": index in hit_indexes,
            "timestamp": record.timestamp,
            "server": record.server_name or record.server_id,
            "backend": record.backend_server,
            "kind": record.kind,
            "level": context.get("level"),
            "player": player,
            "tags": (record.tags or [])[:8],
            "content": truncate(record.content, MAX_RECORD_CONTENT_CHARS),
        }
        for key in ("chatMessage", "opsClassification", "chatClassification"):
            value = context.get(key)
            if value:
                item[key] = value
        return item

    @staticmethod
    def _decisions_by_index(
        raw_decisions: list[Any],
    ) -> dict[int, dict[str, Any]]:
        decisions: dict[int, dict[str, Any]] = {}
        for item in raw_decisions:
            if not isinstance(item, dict):
                continue
            index = _as_int(item.get("index"))
            if index is None or index < 0 or index >= MAX_REVIEW_ISSUES:
                continue
            decisions[index] = item
        return decisions

    @staticmethod
    def _should_drop(decision: dict[str, Any] | None) -> bool:
        if not decision:
            return False
        value = str(decision.get("decision") or "").strip().lower()
        confidence = _as_float(decision.get("confidence"), 0.0)
        return value in _DROP_DECISIONS and confidence >= DROP_CONFIDENCE_THRESHOLD

    @staticmethod
    def _prune_categories_for_dropped_issues(
        reviewed: dict[str, Any],
        original_issues: list[dict[str, Any]],
        kept_issues: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
    ):
        categories = reviewed.get("categories")
        if not isinstance(categories, dict):
            return
        kept_categories = {
            str(issue.get("category") or "")
            for issue in kept_issues
            if issue.get("category")
        }
        dropped_categories: set[str] = set()
        for decision in decisions:
            if not decision.get("dropped"):
                continue
            index = _as_int(decision.get("index"))
            if index is None or index < 0 or index >= len(original_issues):
                continue
            category = str(original_issues[index].get("category") or "")
            if category and category not in kept_categories:
                dropped_categories.add(category)
        for category in dropped_categories:
            if category in categories:
                categories[category] = []


def _review_prompt_text(payload: dict[str, Any]) -> str:
    return (
        "You are a strict false-positive reviewer for Minecraft server reports.\n"
        "The detector has already found candidate issues. Inspect the evidence and "
        "the +/-20 surrounding log/chat records for each issue.\n"
        "Return JSON only with this schema: "
        '{"issues":[{"index":0,"decision":"keep|drop","confidence":0.0,'
        '"reason":"short reason"}]}.\n'
        "Use decision=drop only when the context clearly proves the candidate is a "
        "false positive, normal lifecycle noise, ordinary harmless chat, or an "
        "unsupported over-classification. If evidence is incomplete, ambiguous, or "
        "could affect stability, assets, security, moderation, or player experience, "
        "choose keep. Do not create new issues. Do not recommend automated punishment.\n"
        "Payload:\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _sample_lines(sample: str) -> list[str]:
    lines = [line.strip() for line in str(sample or "").splitlines() if line.strip()]
    return lines or [str(sample or "")]


def _append_unique_int(values: list[int], value: int):
    if value not in values:
        values.append(value)


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
