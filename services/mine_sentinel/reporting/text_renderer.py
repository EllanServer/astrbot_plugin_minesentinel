"""Text renderer for MineSentinel QQ reports."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from ..issue_formatting import format_millis
from .incidents import IncidentGroup, IncidentGrouper, IssuePolicy, issue_sort_key
from .incident_format import (
    as_millis as _as_millis,
    clean_sentence as _clean_sentence,
    dedupe_key as _dedupe_key,
    evidence_line as _evidence_line,
    format_duration as _format_duration,
    format_time_window as _format_time_window,
    incident_time_text as _incident_time_text,
    incident_title as _incident_title,
    is_attachment_note as _is_attachment_note,
    quiet_window_text as _quiet_window_line,
    resolve_attachment_name,
)
from .labels import DEFAULT_LABELS
from .presentation import ReportPresentationBuilder

MAX_EVENT_SUMMARIES = 8
MAX_LOG_PROBLEMS = 8
MAX_RISK_LINES = 6
MAX_ACTIONS = 6
MAX_INLINE_EVIDENCE_CHARS = 240

_LABELS = DEFAULT_LABELS
_INCIDENT_GROUPER = IncidentGrouper()
_ISSUE_POLICY = IssuePolicy()
_PRESENTATION_BUILDER = ReportPresentationBuilder(
    issue_policy=_ISSUE_POLICY,
    incident_grouper=_INCIDENT_GROUPER,
)


def format_report(report: dict, total_count: int, dedupe_count: int, unique_players: int) -> str:
    presentation = _PRESENTATION_BUILDER.build(
        report,
        total_count,
        dedupe_count,
        unique_players,
    )
    categories = presentation.categories
    issues = presentation.issues
    immediate = presentation.actionable_issues
    incident_groups = presentation.incidents
    immediate_count = len(presentation.incidents)
    duration = _format_duration(report)

    lines = [
        f"时间范围：{_format_time_window(report)}",
        f"服务器：{_format_servers(report)}",
        f"完整审计日志：{_format_attachment(report)}",
        "",
        "一、整体情况",
        _overall_line(
            report,
            presentation.total_count,
            presentation.unique_players,
            immediate_count,
            duration,
        ),
        "",
        "二、运行日志与事件总结",
    ]
    _append_numbered(lines, _event_summaries(report, categories, issues, incident_groups))

    lines.extend(["", "三、日志异常识别"])
    lines.extend(_log_problem_lines(issues))

    lines.extend(["", "四、风险提醒"])
    for line in _risk_lines(report, issues, immediate, immediate_count):
        lines.append(f"- {line}")

    lines.extend(["", "五、建议处理"])
    _append_numbered(lines, _action_lines(issues))

    evidence = _evidence_line(
        presentation.total_count,
        presentation.dedupe_count,
        presentation.unique_players,
    )
    if evidence:
        lines.extend(["", evidence])
    lines.extend(
        [
            "",
            f"本次总结由 AI 根据完整 {duration}运行日志和事件上下文生成。",
        ]
    )
    return "\n".join(lines)


def _overall_line(
    report: dict,
    total_count: int,
    unique_players: int,
    immediate_count: int,
    duration: str,
) -> str:
    if immediate_count:
        status = f"发现 {immediate_count} 个需要优先关注的问题"
    else:
        status = "服务器整体稳定，未发现大规模异常"
    return (
        f"过去 {duration}{status}。共记录 {report.get('log_count', total_count)} "
        "条 Minecraft 运行日志观察。"
    )


def _event_summaries(
    report: dict,
    categories: dict[str, Any],
    issues: list[dict[str, Any]],
    incident_groups: list[IncidentGroup] | None = None,
) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()

    groups = incident_groups if incident_groups is not None else _INCIDENT_GROUPER.group(
        _ISSUE_POLICY.actionable_issues(issues)
    )
    for index, group in enumerate(groups[:MAX_EVENT_SUMMARIES], 1):
        _append_unique(items, seen, _incident_event_summary(index, group))

    quiet_line = _quiet_window_line(report, groups)
    if quiet_line and len(items) < MAX_EVENT_SUMMARIES:
        _append_unique(items, seen, quiet_line)
    if groups:
        return items or ["本窗口未发现需要特别记录的运行日志或事件。"]

    if not items:
        for finding in report.get("incident_findings") or []:
            _append_unique(items, seen, _clean_sentence(str(finding)))
            if len(items) >= MAX_EVENT_SUMMARIES:
                break

    if len(items) < MAX_EVENT_SUMMARIES:
        for key in (
            "daily",
            "complaint",
            "bug",
            "network",
            "plugin",
            "economy",
            "community",
            "chat_review",
            "player_feedback",
            "community_ops",
            "moderation",
            "cross_server",
            "suggestion",
        ):
            for item in categories.get(key) or []:
                _append_unique(items, seen, _category_event_summary(str(item)))
                if len(items) >= MAX_EVENT_SUMMARIES:
                    break
            if len(items) >= MAX_EVENT_SUMMARIES:
                break

    return items or ["本窗口未发现需要特别记录的运行日志或事件。"]


def _incident_event_summary(index: int, group: IncidentGroup) -> str:
    issues = list(group.issues)
    labels = _incident_labels(issues)
    title = _incident_title(group, labels)
    time_part = _incident_time_text(group)
    lead = f"事件 #{index}，{time_part}，{title}。"

    details = []
    players = _incident_players(issues)
    if players and players != "未知":
        details.append(f"相关玩家：{players}。")
    if labels:
        details.append(f"影响面：{'、'.join(labels[:8])}。")
    locations = _incident_locations(issues)
    if locations and locations != "未知":
        details.append(f"关联位置/后端：{locations}。")
    evidence = _incident_evidence(issues)
    if evidence:
        details.append(f"相关上下文：{evidence}")
    action = _incident_action(issues)
    if action:
        details.append(f"建议：{action}")
    if details:
        return lead + "\n   " + " ".join(details)
    return lead


def _incident_labels(issues: list[dict[str, Any]]) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for issue in sorted(issues, key=issue_sort_key):
        label = _issue_title(issue)
        key = _dedupe_key(label)
        if key in seen:
            continue
        seen.add(key)
        labels.append(label)
    return labels


def _incident_players(issues: list[dict[str, Any]]) -> str:
    players: list[str] = []
    seen: set[str] = set()
    for issue in issues:
        for player in issue.get("players") or []:
            player = str(player).strip()
            if player and player not in seen:
                seen.add(player)
                players.append(player)
    return _format_players(sorted(players))


def _incident_locations(issues: list[dict[str, Any]]) -> str:
    locations: list[str] = []
    seen: set[str] = set()
    for issue in issues:
        for location in issue.get("affected_locations") or []:
            location = str(location).strip()
            if location and location not in seen:
                seen.add(location)
                locations.append(location)
    return _format_players(sorted(locations))


def _incident_evidence(issues: list[dict[str, Any]]) -> str:
    snippets: list[str] = []
    seen: set[str] = set()
    issue_snippets = [
        _issue_evidence_snippets(issue)
        for issue in sorted(issues, key=issue_sort_key)
    ]
    max_depth = max((len(item) for item in issue_snippets), default=0)
    for index in range(max_depth):
        for candidates in issue_snippets:
            if index >= len(candidates):
                continue
            snippet = candidates[index]
            key = _dedupe_key(snippet)
            if key in seen:
                continue
            seen.add(key)
            snippets.append(snippet)
            if len(snippets) >= 4:
                return _truncate(" / ".join(snippets), MAX_INLINE_EVIDENCE_CHARS)
    return _truncate(" / ".join(snippets), MAX_INLINE_EVIDENCE_CHARS)


def _issue_evidence_snippets(issue: dict[str, Any]) -> list[str]:
    snippets: list[str] = []
    seen: set[str] = set()
    for sample in issue.get("evidence_samples") or []:
        for snippet in _sample_evidence_lines(str(sample)):
            key = _dedupe_key(snippet)
            if key in seen:
                continue
            seen.add(key)
            snippets.append(snippet)
    return snippets


def _sample_evidence_lines(sample: str) -> list[str]:
    hit_lines: list[str] = []
    context_lines: list[str] = []
    for raw_line in sample.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("上下文 "):
            continue
        is_hit = line.startswith(">")
        line = line.lstrip("> ").strip()
        if not line:
            continue
        if is_hit:
            hit_lines.append(line)
        else:
            context_lines.append(line)
    return hit_lines or context_lines[:2]


def _incident_action(issues: list[dict[str, Any]]) -> str:
    actions: list[str] = []
    seen: set[str] = set()
    for issue in sorted(issues, key=issue_sort_key):
        action = _clean_sentence(str(issue.get("suggested_action") or "").strip())
        if not action:
            continue
        key = _dedupe_key(action)
        if key in seen:
            continue
        seen.add(key)
        actions.append(action.rstrip("。"))
        if len(actions) >= 3:
            break
    if not actions:
        return ""
    suffix = "等。" if len(seen) > len(actions) else "。"
    return "；".join(actions) + suffix


def _category_event_summary(item: str) -> str:
    raw = item.strip()
    if raw.startswith("["):
        return ""
    text = _clean_sentence(raw)
    if not text:
        return ""
    return text


def _log_problem_lines(issues: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for group in _INCIDENT_GROUPER.group(
        _ISSUE_POLICY.actionable_issues(issues)
    )[:MAX_LOG_PROBLEMS]:
        group_issues = list(group.issues)
        labels = _incident_labels(group_issues)
        title = "、".join(labels[:6]) or _incident_title(group, labels)
        action = _incident_action(group_issues) or "建议管理员人工复核上下文。"
        locations = _incident_locations(group_issues)
        location_part = f"（{locations}）" if locations != "未知" else ""
        lines.append(f"- {title}{location_part}，{action}")

    return lines or ["- 没有发现需要管理员紧急处理的运行日志异常。"]


def _risk_lines(
    report: dict,
    issues: list[dict[str, Any]],
    immediate: list[dict[str, Any]],
    immediate_count: int,
) -> list[str]:
    lines: list[str] = []
    moderation_issues = [
        issue
        for issue in issues
        if _ISSUE_POLICY.is_moderation_issue(issue)
    ]
    categories_seen = {
        str(issue.get("category") or "").lower() for issue in moderation_issues
    }
    if "community" in categories_seen:
        lines.append("检测到社区管理相关日志，建议按服务器管理流程复核处理。")
    if "chat_review" in categories_seen:
        lines.append("检测到聊天审查相关日志，建议复核聊天原文、频道来源与是否涉及辱骂/广告/威胁。")
    if "community_ops" in categories_seen:
        lines.append("检测到社区运营相关日志，建议跟进活动/奖励/公告上下文，确认是否存在争议或事故。")
    if "player_feedback" in categories_seen:
        lines.append("检测到玩家建议/反馈，建议整理为运营工单汇总评估。")
    if "moderation" in categories_seen:
        lines.append("检测到权限/登录相关风险信号，建议人工复核运行日志上下文。")
    if not lines:
        lines.append("没有检测到明显重复报错、严重异常或持续性告警。")

    if immediate:
        lines.append(f"有 {immediate_count} 个事故级问题需要优先确认。")
    else:
        lines.append("没有发现需要管理员紧急处理的未解决运行日志异常。")

    if any(issue.get("tag") == "server_log_performance" for issue in issues):
        lines.append("卡顿反馈值得关注，建议下次巡检继续跟踪 TPS、内存、实体数量和红石机器。")

    for note in report.get("ops_notes") or []:
        note = str(note).strip()
        if not note or _is_attachment_note(note):
            continue
        lines.append(_clean_sentence(note))
        if len(lines) >= MAX_RISK_LINES:
            break
    return lines[:MAX_RISK_LINES]


def _action_lines(issues: list[dict[str, Any]]) -> list[str]:
    actions: list[str] = []
    seen: set[str] = set()
    for issue in _ISSUE_POLICY.actionable_issues(issues):
        action = str(issue.get("suggested_action") or "").strip()
        if action:
            _append_unique(actions, seen, _clean_sentence(action))
        if len(actions) >= MAX_ACTIONS:
            break
    if actions:
        return actions
    return [
        "继续观察服务器运行日志和关键错误线索。",
        "保留完整 JSONL 附件，必要时按服务器、日志文件和时间点人工复核。",
    ]


def _append_numbered(lines: list[str], items: list[str]):
    for index, item in enumerate(items, 1):
        parts = [part.rstrip() for part in str(item).splitlines() if part.strip()]
        if not parts:
            continue
        lines.append(f"{index}. {parts[0]}")
        for part in parts[1:]:
            lines.append(f"   {part.lstrip()}")


def _append_unique(items: list[str], seen: set[str], item: str):
    item = item.strip()
    if not item:
        return
    key = _dedupe_key(item)
    if key in seen:
        return
    seen.add(key)
    items.append(item)


def _format_servers(report: dict) -> str:
    values: list[str] = []
    server_names = report.get("server_names") or []
    server_fields = ("server_names",) if server_names else ("servers",)
    for field in server_fields + ("proxy_ids",):
        raw = report.get(field) or []
        if isinstance(raw, str):
            raw = [raw]
        for value in raw:
            value = str(value).strip()
            if value and value not in values:
                values.append(value)
    return " / ".join(values) if values else "全部"


def _format_attachment(report: dict) -> str:
    name = resolve_attachment_name(report)
    if name:
        return f"已保存为附件 {name}"
    return "未生成附件"


def _format_players(players: list[str]) -> str:
    if not players:
        return "未知"
    shown = [str(player) for player in players[:16] if str(player)]
    text = "、".join(shown)
    if len(players) > len(shown):
        text += f" 等 {len(players)} 人"
    return text or "未知"


def _issue_title(issue: dict[str, Any]) -> str:
    return _LABELS.issue_title(issue)


def _truncate(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[: max(0, max_length - 3)].rstrip() + "..."
