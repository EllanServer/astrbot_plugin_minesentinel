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
MAX_RISK_LINES = 6
MAX_ACTIONS = 6
MAX_INLINE_EVIDENCE_CHARS = 240

_LABELS = DEFAULT_LABELS
_INCIDENT_GROUPER = IncidentGrouper()
_ISSUE_POLICY = IssuePolicy()
_PRESENTATION_BUILDER = ReportPresentationBuilder(
    issue_policy=_ISSUE_POLICY,
    incident_grouper=IncidentGrouper(merge_window_ms=60 * 60 * 1000),
)
_STABILITY_CATEGORIES = {"complaint", "network", "plugin", "cross_server", "bug"}
_STABILITY_TAGS = {
    "server_log_performance",
    "server_log_network",
    "server_log_plugin",
    "server_log_cross_server",
    "server_log_warn",
    "server_log_error",
}
_ASSET_KEYWORDS = ("商店", "扣款", "经济", "流水", "背包", "同步", "复制", "物品", "shop", "money", "balance")
_REVIEW_CATEGORIES = {"community", "chat_review", "moderation"}
_REVIEW_TAGS = {"server_log_community", "server_log_chat_review", "server_log_auth", "server_log_permission"}


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
    all_groups = presentation.incidents
    incident_groups, observation_groups = _split_incident_groups(all_groups)
    immediate_count = len(incident_groups)
    duration = _format_duration(report)
    player_count = _player_count(report, presentation.unique_players)
    high_risk_count = _high_risk_count(incident_groups)
    manual_review_count = _manual_review_count(incident_groups)

    lines = [
        f"时间范围：{_format_time_window(report)}",
        f"服务器：{_format_servers(report)}",
        f"完整聊天记录：{_format_attachment(report)}",
        "",
        "一、整体情况",
        *_overall_lines(
            report,
            player_count,
            immediate_count,
            len(observation_groups),
            high_risk_count,
            manual_review_count,
            duration,
            incident_groups,
        ),
        "",
        "二、重点事件总结",
    ]
    _append_numbered(lines, _event_summaries(report, categories, issues, incident_groups))

    lines.extend(["", "三、聊天与社区观察"])
    for line in _observation_lines(report, observation_groups):
        lines.append(f"* {line}")

    lines.extend(["", "四、玩家问题/投诉识别"])
    for line in _player_problem_lines(issues, incident_groups + observation_groups):
        lines.append(f"* {line}")

    lines.extend(["", "五、风险提醒与建议处理", "风险提醒"])
    for line in _risk_lines(
        report,
        issues,
        immediate,
        immediate_count,
        incident_groups,
        observation_groups,
    ):
        if line.endswith("："):
            lines.append(line)
        else:
            lines.append(f"- {line}")

    lines.extend(["", "建议处理"])
    _append_numbered(lines, _action_lines(issues))

    lines.extend(
        [
            "",
            f"证据：共 {presentation.total_count} 条观察，涉及玩家 {player_count} 人。",
        ]
    )
    lines.extend(
        [
            "",
            f"本次总结由 AI 根据完整{duration}聊天上下文、玩家事件和服务器指标生成。",
        ]
    )
    return "\n".join(lines)


def _overall_lines(
    report: dict,
    player_count: int,
    incident_count: int,
    observation_count: int,
    high_risk_count: int,
    manual_review_count: int,
    duration: str,
    incident_groups: list[IncidentGroup],
) -> list[str]:
    issues = list(report.get("issues") or [])
    if not incident_count:
        status = "稳定"
    elif any(str(issue.get("category") or "") in {"complaint", "player_feedback", "network", "bug", "plugin"} for issue in issues):
        status = "存在集中异常反馈"
    else:
        status = "存在异常"
    active_players = _active_players(report)
    lines = [
        f"过去{duration}内，服务器整体情况为：{status}。",
        f"本窗口共有 {player_count} 名玩家出现记录，主要活跃玩家包括：{active_players}。",
        (
            f"本次巡检识别到 {incident_count} 个重点事件，高风险事件 {high_risk_count} 个，"
            f"待人工复核 {manual_review_count} 个；一般观察 {observation_count} 个。"
        ),
    ]
    main_time = _main_incident_time(incident_groups)
    if main_time:
        lines.append(
            f"主要异常集中在 {main_time} 左右，其他时间段未发现明显持续性冲突或大规模异常。"
        )
    return lines


def _player_count(report: dict, fallback: int) -> int:
    chat_topics = report.get("chat_topics") or {}
    try:
        chat_players = int(chat_topics.get("unique_players") or 0)
    except (TypeError, ValueError):
        chat_players = 0
    return max(int(fallback or 0), chat_players)


def _active_players(report: dict) -> str:
    chat_topics = report.get("chat_topics") or {}
    players: list[str] = []
    for item in chat_topics.get("top_players") or []:
        player = str((item or {}).get("player") or "").strip()
        if player and player != "(unknown)" and player not in players:
            players.append(player)
        if len(players) >= 5:
            break
    if not players:
        for issue in report.get("issues") or []:
            for player in issue.get("players") or []:
                value = str(player).strip()
                if value and value not in players:
                    players.append(value)
                if len(players) >= 5:
                    break
            if len(players) >= 5:
                break
    return "、".join(players) if players else "无明确玩家"


def _high_incident_count(groups: list[IncidentGroup]) -> int:
    return sum(1 for group in groups if str(group.max_severity or "").lower() in {"high", "critical"})


def _split_incident_groups(
    groups: list[IncidentGroup],
) -> tuple[list[IncidentGroup], list[IncidentGroup]]:
    incidents: list[IncidentGroup] = []
    observations: list[IncidentGroup] = []
    for group in groups:
        if _is_suppressed_group(group):
            continue
        if _is_observation_group(group):
            observations.append(group)
        else:
            incidents.append(group)
    return incidents, observations


def _is_suppressed_group(group: IncidentGroup) -> bool:
    if group.family != "operations":
        return False
    if str(group.max_severity or "").lower() not in {"low", "medium"}:
        return False
    evidence_count = 0
    for issue in group.issues:
        try:
            evidence_count += int(issue.get("evidence_count") or 0)
        except (TypeError, ValueError):
            evidence_count += 0
    return evidence_count <= 1


def _is_observation_group(group: IncidentGroup) -> bool:
    if group.family == "community_ops":
        return not _community_ops_has_accident_signal(group)
    if group.family == "chat_review":
        labels = set(_unique_issue_values(list(group.issues), "chat_labels"))
        if labels and labels <= {"刷屏", "玩笑"}:
            return True
        text = _group_text(group)
        if "刷屏" in labels or "重复" in text or "hhhh" in text or "......" in text:
            return True
        return str(group.max_severity or "").lower() in {"low", "medium"}
    return False


def _community_ops_has_accident_signal(group: IncidentGroup) -> bool:
    text = _group_text(group)
    if str(group.max_severity or "").lower() not in {"high", "critical"}:
        return False
    return any(marker in text for marker in ("奖励发放异常", "活动事故", "大范围玩家不满", "事故"))


def _high_risk_count(groups: list[IncidentGroup]) -> int:
    total = 0
    for group in groups:
        severity = str(group.max_severity or "").lower()
        if severity not in {"high", "critical"}:
            continue
        if group.family in {"community", "chat_review", "moderation"}:
            total += 1
        elif severity == "critical":
            total += 1
    return total


def _ops_incident_count(groups: list[IncidentGroup]) -> int:
    return sum(1 for group in groups if _group_has_ops_signal(group))


def _manual_review_count(groups: list[IncidentGroup]) -> int:
    return sum(1 for group in groups if _requires_manual_review(group))


def _requires_manual_review(group: IncidentGroup) -> bool:
    labels = set(_unique_issue_values(list(group.issues), "chat_labels"))
    if group.family == "community" and labels & {"外挂举报", "飞行举报", "透视/Xray", "自动挖矿"}:
        return True
    return False


def _group_has_ops_signal(group: IncidentGroup) -> bool:
    if group.family == "operations":
        return True
    for issue in group.issues:
        if issue.get("ops_subtypes") or str(issue.get("category") or "") in _STABILITY_CATEGORIES:
            return True
    return False


def _group_text(group: IncidentGroup) -> str:
    parts: list[str] = [group.family, str(group.max_severity or "")]
    for issue in group.issues:
        parts.append(str(issue.get("category") or ""))
        parts.append(str(issue.get("tag") or ""))
        parts.extend(str(item) for item in issue.get("chat_labels") or [])
        parts.extend(str(item) for item in issue.get("ops_subtypes") or [])
        parts.extend(str(item) for item in issue.get("issue_terms") or [])
        parts.extend(str(item) for item in issue.get("evidence_samples") or [])
    return " ".join(parts).lower()


def _main_incident_time(groups: list[IncidentGroup]) -> str:
    if not groups:
        return ""
    group = sorted(
        groups,
        key=lambda item: (
            -len(item.issues),
            item.start_ts if item.start_ts else 2**63 - 1,
        ),
    )[0]
    return _incident_time_text(group).replace(" 左右", "")


def _player_problem_lines(
    issues: list[dict[str, Any]],
    groups: list[IncidentGroup],
) -> list[str]:
    buckets: list[dict[str, Any]] = []
    for group in groups:
        group_issues = list(group.issues)
        if not any(_issue_has_player_feedback(issue) for issue in group_issues):
            continue
        players = _players_from_issues(group_issues)
        if not players:
            continue
        issue_type = _player_issue_type(group)
        status = _problem_status(group_issues)
        action = _player_problem_action(group)
        key = (issue_type, status, action)
        bucket = next((item for item in buckets if item["key"] == key), None)
        if bucket is None:
            bucket = {
                "key": key,
                "players": [],
                "issue_type": issue_type,
                "status": status,
                "action": action,
            }
            buckets.append(bucket)
        for player in players:
            if player not in bucket["players"]:
                bucket["players"].append(player)
    lines = [
        (
            f"{_format_players(bucket['players'])}：{bucket['issue_type']}。"
            f"当前状态：{bucket['status']}。建议：{bucket['action']}"
        )
        for bucket in buckets[:6]
    ]
    if lines:
        return lines
    return ["未识别到明确玩家问题/投诉；当前需要关注的是上方事件中的运行、聊天或管理风险。"]


def _players_from_issues(issues: list[dict[str, Any]]) -> list[str]:
    players: list[str] = []
    for issue in issues:
        for player in issue.get("players") or []:
            value = str(player).strip()
            if value and value not in players:
                players.append(value)
    return players


def _issue_has_player_feedback(issue: dict[str, Any]) -> bool:
    if not issue.get("players"):
        return False
    category = str(issue.get("category") or "")
    if category in {"player_feedback", "chat_review", "community", "moderation"}:
        return True
    return bool(issue.get("chat_labels")) and not bool(issue.get("ops_subtypes"))


def _player_issue_type(group: IncidentGroup) -> str:
    labels = set(_unique_issue_values(list(group.issues), "chat_labels"))
    if group.family == "community" and labels & {"外挂举报", "飞行举报", "透视/Xray", "自动挖矿"}:
        return "涉及外挂/飞行相关聊天举报"
    if group.family == "chat_review" and ("刷屏" in labels or "重复" in _group_text(group)):
        return "出现短时间重复或无意义聊天"
    if "权限异常" in labels:
        return "出现找管理员/投诉或权限相关反馈"
    if "管理员求助" in labels or "证据提交" in labels:
        return "出现找管理员/投诉相关反馈"
    return f"反馈 {'、'.join(_incident_labels(list(group.issues))[:4]) or '玩家问题'}"


def _player_problem_action(group: IncidentGroup) -> str:
    labels = set(_unique_issue_values(list(group.issues), "chat_labels"))
    if group.family == "community" and labels & {"外挂举报", "飞行举报", "透视/Xray", "自动挖矿"}:
        return "人工复核视频、反作弊日志、移动轨迹和上下文，不建议仅凭聊天处罚。"
    if group.family == "chat_review":
        return "先观察是否持续；只有影响聊天秩序或伴随辱骂、广告时再升级处理。"
    if "权限异常" in labels:
        return "复核聊天上下文，确认是否已有处理；只有出现明确权限丢失或命令失败证据时再查权限插件日志。"
    return "复核上下文并确认是否已有管理员处理。"


def _problem_status(issues: list[dict[str, Any]]) -> str:
    if any(str(issue.get("category") or "") == "community" for issue in issues):
        return "待人工复核"
    if any(str(issue.get("category") or "") == "chat_review" for issue in issues):
        return "低风险观察"
    if any(str(issue.get("severity") or "").lower() in {"high", "critical"} for issue in issues):
        return "需要管理员确认"
    if any(str(issue.get("category") or "") in {"complaint", "player_feedback"} for issue in issues):
        return "未看到明确处理结果"
    return "需要管理员确认"


def _observation_lines(report: dict, groups: list[IncidentGroup]) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for group in groups[:6]:
        labels = _incident_labels(list(group.issues))
        title = _incident_display_title(group, labels, observation=True)
        _append_unique(
            lines,
            seen,
            f"{title}。等级：{_severity_label(group)}。处理：{_incident_recommended_action(group)}",
        )
    if not lines:
        chat_topics = report.get("chat_topics") or {}
        if chat_topics.get("total_messages"):
            active = _active_players(report)
            lines.append(f"玩家聊天以普通互动为主，主要活跃玩家包括：{active}。")
        else:
            lines.append("本窗口未识别到需要单独记录的低风险聊天或社区观察。")
    return lines


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
    if incident_groups is not None and not groups:
        return ["本窗口未发现需要管理员优先处理的事故或玩家问题。"]
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
    title = _incident_display_title(group, labels)
    time_part = _incident_time_text(group)
    evidence = _incident_key_evidence(issues, limit=3)
    evidence_strength = _evidence_strength_line(group)
    lines = [
        f"{time_part}，{title}。",
        f"等级：{_severity_label(group)}。",
        f"状态：{_group_status(group)}。",
    ]
    if evidence_strength:
        lines.append(f"证据强度：{evidence_strength}。")
    lines.extend(
        [
            f"影响范围：{_impact_scope(group)}。",
            "摘要：",
            _incident_summary_sentence(group, labels),
            "关键证据：",
        ]
    )
    if evidence:
        lines.extend(f"* {item}" for item in evidence)
    else:
        lines.append("* 无可直接展示的关键证据，需查看完整附件。")
    lines.extend(
        [
            f"初步判断：{_incident_judgement_line(group)}",
            f"建议处理：{_incident_recommended_action(group)}",
        ]
    )
    return "\n".join(lines)


def _incident_summary_sentence(group: IncidentGroup, labels: list[str]) -> str:
    issues = list(group.issues)
    ops_subtypes = set(_unique_issue_values(issues, "ops_subtypes"))
    chat_labels = set(_unique_issue_values(issues, "chat_labels"))
    time_part = _incident_time_text(group).replace(" 左右", "")
    if {"插件加载/启用失败", "数据库超时"} <= ops_subtypes:
        return (
            "服务器在该时间段出现插件加载失败，同时 MariaDB 与 QuickShop 相关连接出现超时。"
            "当前未看到明确玩家直接反馈，但该问题可能影响商店、经济、权限或数据同步功能。"
        )
    if "数据库超时" in ops_subtypes:
        return (
            f"{time_part} 左右，服务器日志出现数据库连接超时线索，可能影响商店、经济、权限或玩家数据同步；"
            "需要结合数据库可用性、连接池和同时间玩家反馈确认影响范围。"
        )
    if group.family in {"player_feedback", "moderation"} and (
        "管理员求助" in chat_labels or "权限异常" in chat_labels or "证据提交" in chat_labels
    ):
        return (
            "玩家在聊天中出现找管理员、投诉或反馈问题的内容，但当前记录中未看到明确处理结果。"
            "建议管理员复核上下文，确认是否已有线下处理。"
        )
    if group.family == "community" and chat_labels & {"外挂举报", "飞行举报", "透视/Xray", "自动挖矿"}:
        return (
            "聊天中出现外挂、飞行相关举报，但当前未看到反作弊日志、视频证据或管理员确认。"
            "建议结合反作弊记录、移动轨迹、玩家位置和上下文复核，不建议仅凭聊天直接处罚。"
        )
    if group.family == "chat_review" and ("刷屏" in chat_labels or "重复" in _group_text(group)):
        return (
            "短时间内出现重复或无意义聊天内容，可能影响聊天可读性；当前未见持续辱骂、广告或明确恶意证据，"
            "建议先观察。"
        )
    if group.family == "community_ops":
        return "玩家围绕活动、区域、普通问答或社区内容有正常交流，当前未发现明显冲突或需要管理员立即处理的问题。"
    label_text = "、".join(labels[:4]) if labels else "运行日志异常"
    location = _incident_locations(list(group.issues))
    location_part = f"，位置/后端为 {location}" if location != "未知" else ""
    severity = str(group.max_severity or "low")
    return (
        f"在同一事件窗口内识别到 {label_text}{location_part}，"
        f"最高严重级别为 {severity}，已按时间和作用域合并为一个事件。"
    )


def _severity_label(group: IncidentGroup) -> str:
    if _is_observation_group(group):
        return "Low"
    severity = str(group.max_severity or "low").lower()
    return {
        "critical": "Critical",
        "high": "High",
        "medium": "Medium",
        "low": "Low",
        "info": "Info",
    }.get(severity, severity.capitalize() or "Low")


def _group_status(group: IncidentGroup) -> str:
    labels = set(_unique_issue_values(list(group.issues), "chat_labels"))
    if group.family == "community" and labels & {"外挂举报", "飞行举报", "透视/Xray", "自动挖矿"}:
        return "待人工复核；不建议仅凭聊天处罚"
    if group.family in {"player_feedback", "moderation"}:
        return "未看到明确处理结果"
    if group.family == "chat_review":
        return "低风险观察，暂不处罚"
    if group.family == "community_ops":
        return "社区观察，无需立即处理"
    if group.family == "operations":
        return "需要管理员确认"
    return "需要管理员确认"


def _evidence_strength_line(group: IncidentGroup) -> str:
    labels = set(_unique_issue_values(list(group.issues), "chat_labels"))
    if group.family == "community" and labels & {"外挂举报", "飞行举报", "透视/Xray", "自动挖矿"}:
        return "聊天证据为主，暂缺反作弊日志、视频或管理员确认"
    return ""


def _impact_scope(group: IncidentGroup) -> str:
    issues = list(group.issues)
    ops_subtypes = _unique_issue_values(issues, "ops_subtypes")
    locations = _incident_locations(issues)
    players = _incident_players(issues)
    if ops_subtypes:
        parts = []
        if locations != "未知":
            parts.append(locations)
        parts.extend(ops_subtypes[:4])
        return " / ".join(parts) if parts else "服务器运维链路"
    if players != "未知":
        return f"玩家：{players}"
    return locations if locations != "未知" else "当前聊天/社区上下文"


def _incident_judgement_line(group: IncidentGroup) -> str:
    labels = set(_unique_issue_values(list(group.issues), "chat_labels"))
    if group.family == "community" and labels & {"外挂举报", "飞行举报", "透视/Xray", "自动挖矿"}:
        return "当前只能说明存在玩家举报或外挂/飞行相关讨论，不能直接判定违规；需要结合外部证据复核。"
    if group.family == "chat_review":
        return "仅作为聊天观察；未达到明确恶意刷屏、广告或辱骂程度。"
    if group.family in {"player_feedback", "moderation"}:
        return "这是玩家侧反馈，不等同于已确认权限异常或管理事故。"
    return _judgement(group)


def _incident_recommended_action(group: IncidentGroup) -> str:
    issues = list(group.issues)
    ops_subtypes = set(_unique_issue_values(issues, "ops_subtypes"))
    labels = set(_unique_issue_values(issues, "chat_labels"))
    if {"插件加载/启用失败", "数据库超时"} <= ops_subtypes:
        return "检查插件版本与依赖、MariaDB 可用性、QuickShop 配置、网络连通性和 latest.log 完整堆栈。"
    if group.family == "community" and labels & {"外挂举报", "飞行举报", "透视/Xray", "自动挖矿"}:
        return "结合反作弊日志、视频、移动轨迹和上下文人工复核，不建议仅凭聊天处罚。"
    if group.family in {"player_feedback", "moderation"}:
        return "复核聊天上下文，确认是否已有管理员线下处理。"
    if group.family == "chat_review":
        return "仅观察；若后续持续刷屏、影响聊天可读性或伴随辱骂广告，再升级处理。"
    if group.family == "community_ops":
        return "作为社区观察记录，无需立即处理。"
    return _incident_action(issues) or _default_incident_action(group)


def _incident_key_evidence(
    issues: list[dict[str, Any]],
    limit: int = 4,
) -> list[str]:
    evidence: list[str] = []
    seen: set[str] = set()
    issue_lines: list[list[str]] = []
    for issue in sorted(issues, key=issue_sort_key):
        lines: list[str] = []
        for sample in issue.get("evidence_samples") or []:
            for line in _format_evidence_sample(str(sample)):
                if _is_low_value_evidence_line(line):
                    continue
                key = _dedupe_key(line)
                if key in seen:
                    continue
                seen.add(key)
                lines.append(_truncate(line, MAX_INLINE_EVIDENCE_CHARS))
        if lines:
            issue_lines.append(lines)
    max_depth = max((len(lines) for lines in issue_lines), default=0)
    for depth in range(max_depth):
        for lines in issue_lines:
            if depth >= len(lines):
                continue
            evidence.append(lines[depth])
            if len(evidence) >= limit:
                return evidence
    return evidence


def _is_low_value_evidence_line(line: str) -> bool:
    text = str(line or "").strip()
    lower = text.lower()
    if any(marker in lower for marker in ("error", "warn", "failed", "failure", "exception", "timed out", "timeout", "refused")):
        return False
    low_value_markers = (
        "hikaripool",
        " - starting",
        " - started",
        "loaded plugin",
        "enabled plugin",
        "successfully",
        "connecting to ",
    )
    return any(marker in lower for marker in low_value_markers)


def _format_evidence_sample(sample: str) -> list[str]:
    text = sample.strip()
    if not text:
        return []

    # Chat evidence emitted by rules.py uses "|" to include a small before/after
    # context. Prefer the marked hit line so each event keeps 2-4 precise facts.
    if text.startswith("[chat"):
        segments = [part.strip() for part in text.split("|") if part.strip()]
        hit_segments = [part for part in segments if part.startswith(">")]
        chosen = hit_segments or segments[:1]
        out: list[str] = []
        for segment in chosen:
            segment = re.sub(r"^\[chat[^\]]*\]\s*", "", segment).strip()
            segment = segment.lstrip(">").strip()
            match = re.match(r"(?P<time>\d{2}:\d{2}:\d{2})\s+<(?P<player>[^>]+)>\s*(?P<message>.*)", segment)
            if match:
                out.append(
                    f"{match.group('time')} {match.group('player')}: {match.group('message').strip()}"
                )
            elif segment:
                out.append(segment)
        return out

    out = []
    for line in _sample_evidence_lines(text):
        match = re.search(r"\[(?P<time>\d{2}:\d{2}:\d{2})\]\s*(?P<body>.*)", line)
        if match:
            out.append(_compact_server_evidence(match.group("time"), match.group("body")))
        else:
            out.append(_compact_server_evidence("", line))
    return out[:2]


def _compact_server_evidence(time_text: str, body: str) -> str:
    text = body.strip()
    text = re.sub(r"^\[[^\]]+\]\s*", "", text)
    match = re.match(r"\[(?P<thread>[^\]/]+(?:/[^\]]+)?)\]:\s*(?P<message>.*)", text)
    if match:
        thread = match.group("thread")
        message = match.group("message").strip()
        level = ""
        if "/" in thread:
            level = thread.rsplit("/", 1)[-1].upper()
        if level:
            return f"{time_text} [{level}] {message}".strip()
        return f"{time_text} {message}".strip()
    match = re.match(r"\[(?P<thread>[^\]]+/(?P<level>[A-Z]+))\]\s*:\s*(?P<message>.*)", text)
    if match:
        return f"{time_text} [{match.group('level')}] {match.group('message').strip()}".strip()
    return f"{time_text} {text}".strip()


def _metrics_summary(group: IncidentGroup) -> str:
    issues = list(group.issues)
    categories = {str(issue.get("category") or "") for issue in issues}
    tags = {str(issue.get("tag") or "") for issue in issues}
    ops_categories = _unique_issue_values(issues, "ops_categories")
    ops_subtypes = _unique_issue_values(issues, "ops_subtypes")
    if ops_subtypes:
        subtype_text = "、".join(ops_subtypes[:4])
        category_text = "、".join(ops_categories[:3]) if ops_categories else "运维日志"
        return f"确定性运维分类指向 {category_text}：{subtype_text}；server_metrics 只能作为同时间旁证，需与日志和聊天反馈交叉确认。"
    if "server_log_performance" in tags or "complaint" in categories:
        return "存在性能/延迟相关日志或玩家反馈；server_metrics 仅作旁证，需结合 TPS、MSPT、GC、内存和插件耗时确认。"
    if "server_log_network" in tags or "server_log_cross_server" in tags:
        return "存在网络、代理或后端转发线索；需结合连接数、丢包、后端在线状态和代理日志确认。"
    if "server_log_economy" in tags or "economy" in categories:
        return "无直接 server_metrics 指标；重点核对经济插件流水、商店记录、背包/数据库同步和是否存在重复扣款。"
    if "server_log_chat_review" in tags:
        return "无直接 server_metrics 指标异常；主要依据玩家聊天原文、时间窗和上下文判定。"
    if "server_log_community" in tags or "community" in categories:
        return "无直接 server_metrics 指标异常；主要依据反作弊、处罚、举报或管理日志复核。"
    if any(tag in tags for tag in ("server_log_warn", "server_log_error", "server_log_plugin")):
        return "日志包含 WARN/ERROR 或插件异常；需结合对应插件日志、启动/重载记录和错误堆栈确认。"
    return "无明确 server_metrics 指标；以本事件的日志和聊天证据为主。"


def _judgement(group: IncidentGroup) -> str:
    family = group.family
    severity = str(group.max_severity or "low")
    categories = {str(issue.get("category") or "") for issue in group.issues}
    tags = {str(issue.get("tag") or "") for issue in group.issues}
    ops_categories = _unique_issue_values(list(group.issues), "ops_categories")
    ops_subtypes = _unique_issue_values(list(group.issues), "ops_subtypes")
    if family == "chat_review":
        return "这是局部聊天行为线索，应按引用上下文判断是玩笑、正常点名、刷屏、骚扰、广告还是隐私泄露，避免扩大化处理。"
    if family == "player_feedback":
        return "这是玩家侧反馈，需要结合前后日志确认是否已解决，不能仅凭单句反馈判定根因。"
    if family == "community":
        return "这是社区治理或反作弊相关线索，需要人工复核证据来源、玩家 UUID、触发规则和上下文。"
    if family == "operations":
        if ops_subtypes:
            return (
                f"确定性分类显示本事件涉及 {'、'.join(ops_categories[:3]) or '运维日志'}"
                f"（{'、'.join(ops_subtypes[:4])}）；需按同一时间窗的日志、玩家反馈和指标共同判定根因。"
            )
        if "economy" in categories or "server_log_economy" in tags:
            return "这是玩家资产或经济链路线索，应以经济流水、商店插件记录和背包同步结果判定是否需要补偿或回滚。"
        if categories & {"network", "cross_server"} or tags & {"server_log_network", "server_log_cross_server"}:
            return "这是连接、代理或后端同步链路线索；同时间窗内的掉线、传送、回档类反馈应按共同根因排查。"
        if categories & {"plugin", "bug"} or tags & {"server_log_plugin", "server_log_error", "server_log_warn"}:
            return "这是插件或运行异常线索；需要先定位具体插件、堆栈和触发时间，再判断是否影响玩家侧状态。"
        return f"这是服务器运行事件，最高级别 {severity}；同一时间窗内的相关标签已合并，需要按共同根因排查。"
    return f"这是 {family} 类事件，最高级别 {severity}，需要结合完整附件复核。"


def _default_incident_action(group: IncidentGroup) -> str:
    categories = {str(issue.get("category") or "") for issue in group.issues}
    tags = {str(issue.get("tag") or "") for issue in group.issues}
    ops_categories = set(_unique_issue_values(list(group.issues), "ops_categories"))
    if group.family == "chat_review":
        return "逐条查看聊天原文和上下文，只处理证据中列出的玩家与时间段。"
    if "数据库与存储" in ops_categories:
        return "先核对数据库连接池、磁盘空间、慢查询和玩家/经济数据写入，再判断是否需要补偿或恢复。"
    if "传送与位置" in ops_categories:
        return "按时间点复查传送插件、跨世界加载、后端转发和玩家位置保存记录。"
    if "插件与模组" in ops_categories:
        return "定位具体插件、事件名或任务名，核对版本、依赖、配置和最近变更。"
    if "economy" in categories or "server_log_economy" in tags:
        return "按玩家和时间核对经济流水、商店记录、背包同步和重复扣款，再决定是否补偿。"
    if group.family == "operations":
        return "先确认是否影响全服稳定性，再按插件、网络、存储或后端同步方向排查。"
    return "保留证据并交由管理员按上下文复核。"


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


def _risk_lines(
    report: dict,
    issues: list[dict[str, Any]],
    immediate: list[dict[str, Any]],
    immediate_count: int,
    incident_groups: list[IncidentGroup] | None = None,
    observation_groups: list[IncidentGroup] | None = None,
) -> list[str]:
    groups = incident_groups
    observations = observation_groups
    if groups is None or observations is None:
        grouped = _INCIDENT_GROUPER.group(_ISSUE_POLICY.actionable_issues(issues))
        groups, observations = _split_incident_groups(grouped)

    high: list[str] = []
    review: list[str] = []
    low: list[str] = []
    all_issues = [issue for group in (groups + observations) for issue in group.issues]
    ops_subtypes = set(_unique_issue_values(all_issues, "ops_subtypes"))
    chat_labels = set(_unique_issue_values(all_issues, "chat_labels"))

    if "插件加载/启用失败" in ops_subtypes or "数据库超时" in ops_subtypes:
        high.append(
            "服务器在主要异常时间段出现插件加载失败和 MariaDB/QuickShop 连接超时，可能影响商店、经济、权限或数据同步。"
        )
    elif _ops_incident_count(groups):
        high.append("存在运维异常，需要优先复核对应时间点的 latest.log、插件堆栈和后端依赖。")

    if chat_labels & {"外挂举报", "飞行举报", "透视/Xray", "自动挖矿"}:
        review.append("聊天中出现疑似外挂/飞行举报，当前缺少反作弊日志、视频或管理员确认，不建议仅凭聊天处罚。")

    if any(group.family in {"player_feedback", "moderation"} for group in groups):
        review.append("出现管理员求助或投诉内容，但未看到明确闭环，建议复核上下文。")
    if any(group.family == "chat_review" for group in observations):
        low.append("短时间重复/无意义聊天目前不构成明确违规，仅需观察。")
    if any(group.family == "community_ops" for group in observations):
        low.append("社区问答和普通互动目前不构成事故，仅作为社区观察记录。")

    if not chat_labels & {"辱骂", "人身攻击", "广告", "诈骗", "引战"}:
        low.append("未发现持续性辱骂、广告、大规模引战或明显群体冲突。")

    lines: list[str] = []
    if high:
        lines.append("高优先级：")
        lines.extend(high[:3])
    if review:
        lines.append("需要人工复核：")
        lines.extend(review[:3])
    if low:
        lines.append("低风险观察：")
        lines.extend(low[:3])
    if not lines:
        lines.append("未发现需要单独提示的运行、聊天或社区风险。")
    return lines[:MAX_RISK_LINES + 3]


def _action_lines(issues: list[dict[str, Any]]) -> list[str]:
    actions: list[str] = []
    seen: set[str] = set()
    groups = _INCIDENT_GROUPER.group(_ISSUE_POLICY.actionable_issues(issues))
    incidents, observations = _split_incident_groups(groups)
    grouped_issues = [issue for group in (incidents + observations) for issue in group.issues]
    ops_subtypes = set(_unique_issue_values(grouped_issues, "ops_subtypes"))
    chat_labels = set(_unique_issue_values(grouped_issues, "chat_labels"))
    if "插件加载/启用失败" in ops_subtypes:
        _append_unique(
            actions,
            seen,
            "优先检查异常时间点 latest.log 和 crash-reports，确认插件加载失败的完整堆栈、插件名、版本和依赖关系。",
        )
    if "数据库超时" in ops_subtypes:
        _append_unique(
            actions,
            seen,
            "检查 MariaDB / QuickShop 相关配置，确认数据库地址、端口、账号、连接池、网络连通性和数据库可用性。",
        )
        _append_unique(
            actions,
            seen,
            "复核依赖数据库的功能是否受影响，重点检查商店、经济、权限、玩家数据同步和跨服数据同步。",
        )
    if chat_labels & {"外挂举报", "飞行举报", "透视/Xray", "自动挖矿"}:
        _append_unique(
            actions,
            seen,
            "对外挂/飞行相关举报进行人工复核，结合视频、反作弊日志、移动轨迹和上下文，不建议仅凭聊天处罚。",
        )
    if any(group.family in {"player_feedback", "moderation"} for group in incidents):
        _append_unique(
            actions,
            seen,
            "对找管理员、投诉或权限相关反馈复核上下文，确认是否已有处理闭环。",
        )
    if any(group.family == "chat_review" for group in observations):
        _append_unique(
            actions,
            seen,
            "对短时间重复聊天先观察，只有在持续刷屏、广告、辱骂或影响聊天秩序时再升级处理。",
        )

    ordered_issues = sorted(
        _ISSUE_POLICY.actionable_issues(issues),
        key=lambda issue: (_action_priority(issue), issue_sort_key(issue)),
    )
    for issue in ordered_issues:
        if len(actions) >= MAX_ACTIONS:
            break
        action = str(issue.get("suggested_action") or "").strip()
        if action:
            _append_unique(actions, seen, _clean_sentence(action))
    if actions:
        return actions
    return [
        "继续观察服务器运行日志和关键错误线索。",
        "保留完整 JSONL 附件，必要时按服务器、日志文件和时间点人工复核。",
    ]


def _action_priority(issue: dict[str, Any]) -> int:
    category = str(issue.get("category") or "").lower()
    tag = str(issue.get("tag") or "").lower()
    text = " ".join(
        [
            category,
            tag,
            str(issue.get("title") or ""),
            str(issue.get("suggested_action") or ""),
            " ".join(str(term) for term in issue.get("issue_terms") or []),
        ]
    ).lower()
    if category in _STABILITY_CATEGORIES or tag in _STABILITY_TAGS:
        return 0
    if category == "economy" or tag == "server_log_economy" or any(keyword.lower() in text for keyword in _ASSET_KEYWORDS):
        return 1
    if category in _REVIEW_CATEGORIES or tag in _REVIEW_TAGS:
        return 2
    return 3


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
    chat_labels = [
        str(label).strip()
        for label in (issue.get("chat_labels") or [])
        if str(label).strip()
    ]
    if chat_labels:
        return "、".join(chat_labels[:4])
    ops_subtypes = [
        str(label).strip()
        for label in (issue.get("ops_subtypes") or [])
        if str(label).strip()
    ]
    if ops_subtypes:
        return "、".join(ops_subtypes[:4])
    return _LABELS.issue_title(issue)


def _incident_display_title(
    group: IncidentGroup,
    labels: list[str],
    observation: bool = False,
) -> str:
    issues = list(group.issues)
    label_set = set(labels)
    ops_subtypes = set(_unique_issue_values(issues, "ops_subtypes"))
    chat_labels = set(_unique_issue_values(issues, "chat_labels"))
    if {"插件加载/启用失败", "数据库超时"} <= ops_subtypes:
        return "插件加载失败与数据库连接超时"
    if "数据库超时" in ops_subtypes:
        if "经济/商店异常" in ops_subtypes or "商店异常" in label_set:
            return "数据库连接超时与商店/经济插件异常"
        return "数据库连接超时"
    if "插件加载/启用失败" in ops_subtypes:
        return "插件加载失败"
    if group.family in {"player_feedback", "moderation"} and (
        "管理员求助" in chat_labels or "权限异常" in chat_labels or "证据提交" in chat_labels
    ):
        if "权限异常" in chat_labels and _has_strong_permission_evidence(group):
            return "玩家管理求助与权限相关反馈"
        return "玩家管理求助与投诉未闭环"
    if group.family == "community" and chat_labels & {"外挂举报", "飞行举报", "透视/Xray", "自动挖矿"}:
        return "疑似外挂/飞行相关举报"
    if group.family == "chat_review" and ("刷屏" in chat_labels or "重复" in _group_text(group)):
        return "短时间重复/无意义聊天内容"
    if group.family == "community_ops":
        return "社区活动与普通问答" if observation else "社区运营相关观察"
    if observation and group.family == "chat_review":
        return "聊天观察"
    return _incident_title(group, labels)


def _has_strong_permission_evidence(group: IncidentGroup) -> bool:
    text = _group_text(group)
    return any(
        marker in text
        for marker in (
            "luckperms",
            "permissionsex",
            "权限组",
            "权限丢失",
            "没有权限",
            "无法使用命令",
            "命令用不了",
            "权限/命令异常",
            "权限/登录相关运行日志异常",
        )
    )


def _unique_issue_values(issues: list[dict[str, Any]], key: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for issue in issues:
        for raw in issue.get(key) or []:
            value = str(raw).strip()
            if not value or value in seen:
                continue
            seen.add(value)
            values.append(value)
    return values


def _truncate(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[: max(0, max_length - 3)].rstrip() + "..."
