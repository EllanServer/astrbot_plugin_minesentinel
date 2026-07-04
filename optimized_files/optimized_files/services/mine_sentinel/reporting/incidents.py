"""Incident-level aggregation for MineSentinel report issues."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


INCIDENT_MERGE_WINDOW_MS = 5 * 60 * 1000
ACTIONABLE_SEVERITIES = {"medium", "high", "critical"}
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
MODERATION_TAGS = {"cheat_or_grief_report", "chat_conflict"}
SUGGESTION_TAGS = {"player_suggestion"}
ABUSE_HINTS = (
    "复制",
    "dupe",
    "刷物品",
    "外挂",
    "作弊",
    "飞行",
    "透视",
    "举报",
    "炸家",
    "偷东西",
)


@dataclass
class IncidentGroup:
    """A reader-facing incident assembled from one or more deterministic issues."""

    family: str
    scopes: set[str] = field(default_factory=set)
    issues: list[dict[str, Any]] = field(default_factory=list)
    start_ts: int = 0
    end_ts: int = 0
    max_severity: str = "low"

    @classmethod
    def from_issue(cls, issue: dict[str, Any]) -> "IncidentGroup":
        start, end = issue_time_bounds(issue)
        return cls(
            family=issue_family(issue),
            scopes=set(issue_scopes(issue)),
            issues=[issue],
            start_ts=start,
            end_ts=end,
            max_severity=str(issue.get("severity") or "low").lower(),
        )

    def add(self, issue: dict[str, Any]):
        start, end = issue_time_bounds(issue)
        self.issues.append(issue)
        self.scopes.update(issue_scopes(issue))
        if start:
            self.start_ts = min(self.start_ts or start, start)
        if end:
            self.end_ts = max(self.end_ts, end)
        if severity_rank(issue) > SEVERITY_RANK.get(self.max_severity, 0):
            self.max_severity = str(issue.get("severity") or "low").lower()


class IssuePolicy:
    """Classify issues for presentation and report dispatch decisions."""

    @staticmethod
    def actionable_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            issue
            for issue in issues
            if not is_passive_issue(issue)
            and (
                issue.get("should_alert")
                or str(issue.get("severity") or "").lower() in ACTIONABLE_SEVERITIES
            )
        ]

    @staticmethod
    def is_metric_issue(issue: dict[str, Any]) -> bool:
        return is_metric_issue(issue)

    @staticmethod
    def is_moderation_issue(issue: dict[str, Any]) -> bool:
        return (
            str(issue.get("category") or "").lower() == "moderation"
            or str(issue.get("tag") or "").lower() == "chat_conflict"
        )


class IncidentGrouper:
    """Group issue-level facts into incident-level summaries."""

    def __init__(self, merge_window_ms: int = INCIDENT_MERGE_WINDOW_MS):
        self.merge_window_ms = max(0, int(merge_window_ms))

    def group(self, issues: list[dict[str, Any]]) -> list[IncidentGroup]:
        groups: list[IncidentGroup] = []
        # Inverted index: (family, scope) -> list of groups that share that
        # scope. Lets each new issue probe only candidate groups sharing at
        # least one scope, instead of scanning every existing group (O(N×M) ->
        # roughly O(N×candidates_per_scope)).
        index: dict[tuple[str, str], list[IncidentGroup]] = {}
        for issue in sorted(issues, key=issue_sort_key):
            if is_metric_issue(issue):
                continue
            family = issue_family(issue)
            scopes = set(issue_scopes(issue))
            # Gather candidate groups that share at least one scope.
            seen_ids: set[int] = set()
            candidates: list[IncidentGroup] = []
            for scope in scopes:
                for group in index.get((family, scope), ()):
                    if id(group) not in seen_ids:
                        seen_ids.add(id(group))
                        candidates.append(group)
            placed = False
            for group in candidates:
                if self.can_merge(group, issue):
                    group.add(issue)
                    # New scopes from the issue may expand the group's index
                    # footprint so future issues with those scopes find it.
                    for new_scope in group.scopes - scopes:
                        index.setdefault((family, new_scope), []).append(group)
                    placed = True
                    break
            if not placed:
                new_group = IncidentGroup.from_issue(issue)
                groups.append(new_group)
                for scope in new_group.scopes:
                    index.setdefault((family, scope), []).append(new_group)
        groups.sort(key=incident_sort_key)
        return groups

    def can_merge(self, group: IncidentGroup, issue: dict[str, Any]) -> bool:
        if group.family != issue_family(issue):
            return False
        issue_scope_values = set(issue_scopes(issue))
        if group.scopes and issue_scope_values and group.scopes.isdisjoint(issue_scope_values):
            return False
        return self.times_close(group, issue)

    def times_close(self, group: IncidentGroup, issue: dict[str, Any]) -> bool:
        issue_start, issue_end = issue_time_bounds(issue)
        if not issue_start or not issue_end or not group.start_ts or not group.end_ts:
            return True
        return (
            issue_start <= group.end_ts + self.merge_window_ms
            and group.start_ts <= issue_end + self.merge_window_ms
        )


def is_passive_issue(issue: dict[str, Any]) -> bool:
    return str(issue.get("category") or "").lower() == "daily" or is_metric_issue(issue)


def is_metric_issue(issue: dict[str, Any]) -> bool:
    tag = str(issue.get("tag") or "").lower()
    source_tag = str(issue.get("source_tag") or "").lower()
    category = str(issue.get("category") or "").lower()
    return tag == "server_metrics" or (
        category == "daily" and ("metric" in tag or "metric" in source_tag)
    )


def issue_sort_key(issue: dict[str, Any]) -> tuple[int, int, str]:
    start, end = issue_time_bounds(issue)
    ts = start or end or 0
    return (ts if ts else 2**63 - 1, -severity_rank(issue), str(issue.get("tag") or ""))


def incident_sort_key(group: IncidentGroup) -> tuple[int, int]:
    severity = SEVERITY_RANK.get(str(group.max_severity or "low"), 0)
    return (group.start_ts if group.start_ts else 2**63 - 1, -severity)


def issue_time_bounds(issue: dict[str, Any]) -> tuple[int, int]:
    first = as_millis(issue.get("first_seen_ts"))
    last = as_millis(issue.get("last_seen_ts"))
    if first and last:
        return min(first, last), max(first, last)
    value = first or last
    return value, value


def issue_family(issue: dict[str, Any]) -> str:
    category = str(issue.get("category") or "").lower()
    tag = str(issue.get("tag") or "").lower()
    if category == "moderation" or tag in MODERATION_TAGS:
        return "moderation"
    if tag == "feature_broken" and looks_like_abuse(issue):
        return "moderation"
    if category == "suggestion" or tag in SUGGESTION_TAGS:
        return "suggestion"
    return "operations"


def looks_like_abuse(issue: dict[str, Any]) -> bool:
    text_parts = [
        str(issue.get("tag") or ""),
        str(issue.get("title") or ""),
        " ".join(str(term) for term in issue.get("dialogue_terms") or []),
        " ".join(str(sample) for sample in issue.get("evidence_samples") or []),
    ]
    text = " ".join(text_parts).lower()
    return any(hint.lower() in text for hint in ABUSE_HINTS)


def issue_scopes(issue: dict[str, Any]) -> list[str]:
    scopes: list[str] = []
    for value in issue.get("affected_servers") or []:
        append_scope(scopes, str(value))
    for value in issue.get("affected_locations") or []:
        raw = str(value)
        append_scope(scopes, raw)
        server = re.split(r"[/@]", raw, maxsplit=1)[0]
        append_scope(scopes, server)
    return scopes or ["__window__"]


def append_scope(scopes: list[str], value: str):
    value = value.strip()
    if value and value not in scopes:
        scopes.append(value)


def severity_rank(issue: dict[str, Any]) -> int:
    return SEVERITY_RANK.get(str(issue.get("severity") or "low").lower(), 0)


def as_millis(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0
