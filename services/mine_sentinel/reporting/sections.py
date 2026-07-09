"""Structured five-section report contract for MineSentinel."""

from __future__ import annotations

import re
from typing import Any

from .labels import DEFAULT_LABELS


MAX_SECTION_BULLETS = 8
MAX_SECTION_BULLET_CHARS = 220

REPORT_SECTION_SPECS = (
    ("overall", "一、整体情况"),
    ("incidents", "二、重点事件总结"),
    ("community", "三、聊天与社区观察"),
    ("player_problems", "四、玩家问题/投诉识别"),
    ("risk_actions", "五、风险提醒与建议处理"),
)

SEVERITY_TITLES = {
    "critical": "紧急",
    "high": "高风险",
    "medium": "需关注",
    "low": "观察",
}
SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3, "critical": 4}
LEVEL_TITLES = {
    "INFO": "信息",
    "WARN": "警告",
    "WARNING": "警告",
    "ERROR": "错误",
    "SEVERE": "严重错误",
    "FATAL": "致命错误",
}


def normalize_report_sections(
    raw_sections: Any,
    report: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return all five sections in stable order with compact text bullets."""
    incoming = _sections_by_id(raw_sections)
    sections: list[dict[str, Any]] = []
    for section_id, title in REPORT_SECTION_SPECS:
        raw = incoming.get(section_id) or {}
        bullets = _clean_bullets(
            raw.get("bullets") or raw.get("items") or raw.get("lines") or []
        )
        if not bullets:
            bullets = _fallback_bullets(section_id, report)
        sections.append(
            {
                "id": section_id,
                "title": title,
                "bullets": bullets[:MAX_SECTION_BULLETS],
            }
        )
    return sections


def build_report_sections(report: dict[str, Any]) -> list[dict[str, Any]]:
    return normalize_report_sections([], report)


def _sections_by_id(raw_sections: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_sections, list):
        return {}
    sections: dict[str, dict[str, Any]] = {}
    valid_ids = {section_id for section_id, _ in REPORT_SECTION_SPECS}
    for raw in raw_sections:
        if not isinstance(raw, dict):
            continue
        section_id = str(raw.get("id") or "").strip()
        if section_id in valid_ids and section_id not in sections:
            sections[section_id] = raw
    return sections


def _fallback_bullets(section_id: str, report: dict[str, Any]) -> list[str]:
    if section_id == "overall":
        return _overall_bullets(report)
    if section_id == "incidents":
        return _incident_bullets(report)
    if section_id == "community":
        return _community_bullets(report)
    if section_id == "player_problems":
        return _player_problem_bullets(report)
    if section_id == "risk_actions":
        return _risk_action_bullets(report)
    return []


def _overall_bullets(report: dict[str, Any]) -> list[str]:
    bullets = _clean_bullets([report.get("summary")])
    servers = ", ".join(str(server) for server in (report.get("servers") or []) if server)
    issue_count = sum(1 for issue in (report.get("issues") or []) if isinstance(issue, dict))
    max_severity = str(report.get("max_severity") or "low").lower()
    status_parts = []
    if servers:
        status_parts.append(f"监控范围：{servers}")
    status_parts.append(f"需关注事件：{issue_count} 组")
    status_parts.append(f"最高风险：{SEVERITY_TITLES.get(max_severity, '观察')}")
    bullets.append(_truncate("；".join(status_parts) + "。"))
    return bullets or ["本窗口暂无可汇总的服务器运行日志。"]


def _incident_bullets(report: dict[str, Any]) -> list[str]:
    findings = report.get("incident_findings") or []
    bullets = _clean_bullets(findings)
    if bullets:
        return bullets
    aggregates: dict[str, dict[str, Any]] = {}
    for order, issue in enumerate(report.get("issues") or []):
        if not isinstance(issue, dict):
            continue
        title = _issue_title(issue)
        action = str(issue.get("suggested_action") or "").strip()
        if not title:
            continue
        key = " ".join(title.lower().split())
        bucket = aggregates.get(key)
        if bucket is None:
            bucket = {
                "title": title,
                "action": action,
                "count": 0,
                "servers": [],
                "severity": "low",
                "order": order,
            }
            aggregates[key] = bucket
        bucket["count"] += _positive_int(issue.get("evidence_count"), 1)
        for server in issue.get("affected_servers") or []:
            value = str(server).strip()
            if value and value not in bucket["servers"]:
                bucket["servers"].append(value)
        severity = str(issue.get("severity") or "low").lower()
        if SEVERITY_ORDER.get(severity, 0) > SEVERITY_ORDER.get(bucket["severity"], 0):
            bucket["severity"] = severity
        if not bucket["action"] and action:
            bucket["action"] = action
    ranked = sorted(
        aggregates.values(),
        key=lambda item: (
            -SEVERITY_ORDER.get(item["severity"], 0),
            -item["count"],
            item["order"],
        ),
    )
    for item in ranked:
        scope = "、".join(item["servers"][:3]) or "当前监控范围"
        prefix = SEVERITY_TITLES.get(item["severity"], "观察")
        line = f"{prefix}·{item['title']}：{item['count']} 条证据，影响 {scope}"
        if item["action"]:
            line += f"；建议：{item['action'].rstrip('。！？.!?')}"
        bullets.append(_truncate(line + "。"))
    return bullets[:MAX_SECTION_BULLETS] or ["未发现需要立即处理的重点事件。"]


def _community_bullets(report: dict[str, Any]) -> list[str]:
    bullets = _clean_bullets([report.get("chat_summary")])
    categories = report.get("categories") or {}
    for key in ("community", "chat_review", "community_ops"):
        bullets.extend(
            _clean_bullets(
                [_category_line(value, key) for value in (categories.get(key) or [])[:3]]
            )
        )
    return bullets[:MAX_SECTION_BULLETS] or ["本窗口未发现明显聊天或社区运营异常。"]


def _player_problem_bullets(report: dict[str, Any]) -> list[str]:
    categories = report.get("categories") or {}
    bullets: list[str] = []
    for key in ("complaint", "player_feedback", "suggestion"):
        bullets.extend(
            _clean_bullets(
                [_category_line(value, key) for value in (categories.get(key) or [])[:4]]
            )
        )
    return bullets[:MAX_SECTION_BULLETS] or ["本窗口未发现集中玩家投诉或待跟进反馈。"]


def _risk_action_bullets(report: dict[str, Any]) -> list[str]:
    bullets = _clean_bullets(
        [_humanize_ops_note(note) for note in (report.get("ops_notes") or [])]
    )
    for issue in report.get("issues") or []:
        if isinstance(issue, dict):
            bullets.extend(_clean_bullets([issue.get("suggested_action")]))
    return _dedupe(bullets)[:MAX_SECTION_BULLETS] or ["保持观察，无需立即执行管理动作。"]


def _clean_bullets(values: Any) -> list[str]:
    if not isinstance(values, list):
        values = [values]
    bullets: list[str] = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("text") or value.get("summary") or value.get("title")
        text = _truncate(str(value or "").strip())
        if text:
            bullets.append(text)
    return _dedupe(bullets)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = " ".join(value.lower().split())
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _issue_title(issue: dict[str, Any]) -> str:
    incident_title = str(issue.get("incident_title") or "").strip()
    if incident_title and not DEFAULT_LABELS.looks_like_raw_tag(incident_title):
        return incident_title
    subtypes = []
    for subtype in issue.get("ops_subtypes") or []:
        value = str(subtype).strip()
        if value and value not in subtypes:
            subtypes.append(value)
    if subtypes:
        return "、".join(subtypes[:2])
    return DEFAULT_LABELS.issue_title(issue)


def _category_line(value: Any, category: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    tag, separator, details = text.partition(":")
    if not separator:
        return text
    title = DEFAULT_LABELS.tag_title(tag) or DEFAULT_LABELS.category_titles.get(
        category, "运行日志观察"
    )
    title = title.removesuffix("日志")
    details = details.strip().replace("条运行日志", "条")
    for level, label in LEVEL_TITLES.items():
        details = re.sub(rf"\b{level}\b", label, details, flags=re.IGNORECASE)
    details = re.sub(r"\s*,\s*", "、", details)
    return f"{title}：{details}"


def _humanize_ops_note(value: Any) -> str:
    text = str(value or "").strip()
    for severity, title in SEVERITY_TITLES.items():
        text = re.sub(
            rf"最高严重级别\s*{severity}\b",
            f"最高风险 {title}",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            rf"severity\s*=\s*{severity}\b",
            f"风险等级={title}",
            text,
            flags=re.IGNORECASE,
        )
    return text


def _positive_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _truncate(value: str) -> str:
    if len(value) <= MAX_SECTION_BULLET_CHARS:
        return value
    return value[: MAX_SECTION_BULLET_CHARS - 3] + "..."
