"""Shared issue text formatting helpers for MineSentinel messages."""

from __future__ import annotations

import time
from typing import Any


def format_issue_incident(issue: dict[str, Any]) -> str:
    value = issue.get("incident_index")
    if value is None:
        return ""
    try:
        return f"事件 #{int(value) + 1}"
    except (TypeError, ValueError):
        return f"事件 {value}"


def format_issue_time_range(issue: dict[str, Any]) -> str:
    first_ts = _as_millis(issue.get("first_seen_ts"))
    last_ts = _as_millis(issue.get("last_seen_ts"))
    if not first_ts and not last_ts:
        return ""
    if first_ts and last_ts and first_ts != last_ts:
        return f"{format_millis(first_ts)} ~ {format_millis(last_ts)}"
    return format_millis(first_ts or last_ts)


def format_issue_terms(issue: dict[str, Any]) -> str:
    terms = issue.get("issue_terms") or issue.get("top_terms") or []
    if not isinstance(terms, list):
        return ""
    clean_terms = [str(term) for term in terms if term]
    return "、".join(clean_terms[:6])


def format_millis(value: int) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(value / 1000))


def _as_millis(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0
