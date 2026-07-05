"""Application-level presentation model for MineSentinel reports."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from .incidents import IncidentGroup, IncidentGrouper, IssuePolicy


@dataclass(frozen=True)
class ReportPresentation:
    """Normalized view model consumed by report renderers."""

    report: dict[str, Any]
    categories: dict[str, Any]
    issues: list[dict[str, Any]]
    actionable_issues: list[dict[str, Any]]
    incidents: list[IncidentGroup]
    total_count: int
    dedupe_count: int
    unique_players: int


class ReportPresentationBuilder:
    """Build a renderer-friendly view model from raw report facts."""

    def __init__(
        self,
        issue_policy: IssuePolicy | None = None,
        incident_grouper: IncidentGrouper | None = None,
    ):
        self.issue_policy = issue_policy or IssuePolicy()
        self.incident_grouper = incident_grouper or IncidentGrouper()

    def build(
        self,
        report: dict,
        total_count: int,
        dedupe_count: int,
        unique_players: int,
    ) -> ReportPresentation:
        categories = report.get("categories") or {}
        issues = [
            _tighten_display_time_bounds(issue)
            for issue in report.get("issues") or []
            if isinstance(issue, dict)
        ]
        actionable = self.issue_policy.actionable_issues(issues)
        incidents = self.incident_grouper.group(actionable)
        return ReportPresentation(
            report=report,
            categories=categories,
            issues=issues,
            actionable_issues=actionable,
            incidents=incidents,
            total_count=total_count,
            dedupe_count=dedupe_count,
            unique_players=unique_players,
        )


def _tighten_display_time_bounds(issue: dict[str, Any]) -> dict[str, Any]:
    category = str(issue.get("category") or "").lower()
    if category not in {"complaint", "network", "plugin", "cross_server", "bug", "economy"}:
        return issue
    first = _as_millis(issue.get("first_seen_ts"))
    last = _as_millis(issue.get("last_seen_ts"))
    if not first or not last or last - first <= 30 * 60 * 1000:
        return issue
    sample_times = _sample_times(issue.get("evidence_samples") or [], first)
    if not sample_times:
        return issue
    tightened = dict(issue)
    tightened["first_seen_ts"] = min(sample_times)
    tightened["last_seen_ts"] = max(sample_times)
    return tightened


def _sample_times(samples: list[Any], anchor_ts: int) -> list[int]:
    values: list[int] = []
    for sample in samples:
        text = str(sample or "")
        for match in re.finditer(r"\[(?P<hms>\d{2}:\d{2}:\d{2})\]", text):
            values.append(_hms_to_millis(match.group("hms"), anchor_ts))
    return values


def _hms_to_millis(value: str, anchor_ts: int) -> int:
    anchor = time.localtime(anchor_ts / 1000)
    hour, minute, second = (int(part) for part in value.split(":"))
    candidate = time.mktime(
        (
            anchor.tm_year,
            anchor.tm_mon,
            anchor.tm_mday,
            hour,
            minute,
            second,
            anchor.tm_wday,
            anchor.tm_yday,
            anchor.tm_isdst,
        )
    )
    ts = int(candidate * 1000)
    if ts < anchor_ts - 12 * 60 * 60 * 1000:
        ts += 24 * 60 * 60 * 1000
    elif ts > anchor_ts + 12 * 60 * 60 * 1000:
        ts -= 24 * 60 * 60 * 1000
    return ts


def _as_millis(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0
