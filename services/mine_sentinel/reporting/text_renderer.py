"""Text renderer for MineSentinel QQ reports."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

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
    incident_grouper=IncidentGrouper(),
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
_OBSERVATION_TAG_RE = re.compile(
    r"^(?P<tag>server_log_[a-z0-9_]+_observation):\s*(?P<count>\d+)\s*条",
    re.IGNORECASE,
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
    all_groups = presentation.incidents
    incident_groups, observation_groups = _split_incident_groups(all_groups)
    category_observations = _category_observation_lines(report)
    immediate_count = len(incident_groups)
    duration = _format_duration(report)
    player_count = _player_count(report, presentation.unique_players)
    high_risk_count = _high_risk_count(incident_groups)
    manual_review_count = _manual_review_count(incident_groups)

    lines = [
        f"时间范围：{_format_time_window(report)}",
        f"服务器：{_format_servers(report)}",
        f"附件：{_format_attachment(report)}",
        "",
        "一、整体情况",
        *_overall_lines(
            report,
            player_count,
            immediate_count,
            len(observation_groups) + len(category_observations),
            high_risk_count,
            manual_review_count,
            duration,
            incident_groups,
        ),
        "",
        "二、重点事件总结",
    ]
    _append_numbered(lines, _event_summaries(report, categories, issues, incident_groups))
    quiet_line = _quiet_window_line(report, incident_groups)
    if quiet_line:
        lines.append(f"补充：{quiet_line}")

    lines.extend(["", "三、聊天与社区观察"])
    for line in _observation_lines(report, observation_groups):
        lines.append(f"* {line}")

    lines.extend(["", "四、玩家问题/投诉识别"])
    for line in _player_problem_lines(issues, incident_groups + observation_groups):
        lines.append(f"* {line}")

    lines.extend(["", "五、风险提醒与建议处理", "风险分级"])
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

    lines.extend(["", "处置顺序"])
    _append_numbered(lines, _action_lines(issues, incident_groups, observation_groups))

    lines.extend(
        [
            "",
            f"证据：共 {presentation.total_count} 条观察，涉及玩家 {player_count} 人。",
        ]
    )
    lines.extend(
        [
            "",
            f"本报告基于{_duration_with_prefix('完整', duration)}运行日志、玩家事件和结构化分类生成。",
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
    elif any(
        str(issue.get("category") or "") in {"complaint", "player_feedback"}
        for issue in issues
    ):
        status = "存在集中异常反馈"
    else:
        status = "发现需要处理的运行风险"
    active_players = _active_players(report)
    lines = [
        f"{_duration_with_prefix('过去', duration)}内，服务器整体情况为：{status}。",
        f"本窗口记录到 {player_count} 名活跃玩家，主要包括：{active_players}。",
        (
            f"本次巡检识别到 {incident_count} 个重点事件，高风险事件 {high_risk_count} 个，"
            f"待人工复核 {manual_review_count} 个；一般观察 {observation_count} 个。"
        ),
    ]
    time_summary = _incident_time_distribution(incident_groups)
    if time_summary:
        lines.append(time_summary)
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
    if group.family == "community":
        labels = set(_unique_issue_values(list(group.issues), "chat_labels"))
        return (
            not labels
            and str(group.max_severity or "").lower() in {"low", "medium"}
        )
    if group.family == "community_ops":
        return not _community_ops_has_accident_signal(group)
    if group.family == "chat_review":
        if str(group.max_severity or "").lower() in {"high", "critical"}:
            return False
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
    return sum(
        1
        for group in groups
        if str(group.max_severity or "").lower() in {"high", "critical"}
    )


def _duration_with_prefix(prefix: str, duration: str) -> str:
    separator = "" if duration.startswith(("约 ", "本窗口")) else " "
    return f"{prefix}{separator}{duration}"


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


def _is_auth_security_group(group: IncidentGroup) -> bool:
    issues = list(group.issues)
    ops_categories = set(_unique_issue_values(issues, "ops_categories"))
    ops_subtypes = set(_unique_issue_values(issues, "ops_subtypes"))
    return (
        "认证与接入安全" in ops_categories
        or "离线模式/认证绕过风险" in ops_subtypes
    )


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


def _incident_time_distribution(groups: list[IncidentGroup]) -> str:
    if not groups:
        return ""
    starts = [group.start_ts for group in groups if group.start_ts]
    ends = [group.end_ts for group in groups if group.end_ts]
    if len(groups) > 1 and starts and ends and max(ends) - min(starts) > 30 * 60 * 1000:
        first = _format_time_window(
            {"time_window": {"start": min(starts), "end": min(starts)}}
        ).split(" - ", 1)[0]
        last = _format_time_window(
            {"time_window": {"start": max(ends), "end": max(ends)}}
        ).split(" - ", 1)[0]
        return (
            f"重点事件分布在 {first} 至 {last} 的多个时间段，"
            "应按风险等级和证据时间逐段复核。"
        )
    group = sorted(
        groups,
        key=lambda item: (
            -len(item.issues),
            item.start_ts if item.start_ts else 2**63 - 1,
        ),
    )[0]
    main_time = _incident_time_text(group).replace(" 左右", "")
    return f"主要异常集中在 {main_time} 左右，其他时间段未发现明显持续性冲突或大规模异常。"


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
        return "出现命令或功能权限异常反馈"
    if "管理员求助" in labels or "证据提交" in labels:
        return "出现找管理员/投诉相关反馈"
    return f"反馈 {'、'.join(_incident_labels(list(group.issues))[:4]) or '玩家问题'}"


def _player_problem_action(group: IncidentGroup) -> str:
    labels = set(_unique_issue_values(list(group.issues), "chat_labels"))
    if group.family == "community" and labels & {"外挂举报", "飞行举报", "透视/Xray", "自动挖矿"}:
        return "人工复核视频、反作弊日志、移动轨迹和上下文，不建议仅凭聊天处罚。"
    if group.family == "chat_review":
        if str(group.max_severity or "").lower() in {"high", "critical"}:
            return "核对消息原文、频率、持续时间和上下文，确认违规后再由管理员处理。"
        return "先观察是否持续；只有影响聊天秩序或伴随辱骂、广告时再升级处理。"
    if "权限异常" in labels:
        return "复核聊天上下文，确认是否已有处理；只有出现明确权限丢失或命令失败证据时再查权限插件日志。"
    return "复核上下文并确认是否已有管理员处理。"


def _problem_status(issues: list[dict[str, Any]]) -> str:
    if any(str(issue.get("category") or "") == "community" for issue in issues):
        return "待人工复核"
    if any(
        str(issue.get("severity") or "").lower() in {"high", "critical"}
        for issue in issues
    ):
        return "需要管理员确认"
    if any(str(issue.get("category") or "") == "chat_review" for issue in issues):
        return "低风险观察"
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
    for line in _category_observation_lines(report):
        _append_unique(lines, seen, line)
    chat_topics = report.get("chat_topics") or {}
    if chat_topics.get("total_messages") and not any(
        group.family in {"community", "chat_review", "community_ops"}
        for group in groups
    ):
        active = _active_players(report)
        _append_unique(
            lines,
            seen,
            f"玩家聊天以普通互动为主，主要活跃玩家包括：{active}。",
        )
    if not lines:
        lines.append("本窗口未识别到需要单独记录的低风险聊天或社区观察。")
    return lines


def _category_observation_lines(report: dict) -> list[str]:
    categories = report.get("categories") or {}
    counts: dict[str, int] = {}
    for values in categories.values():
        for raw in values or ():
            match = _OBSERVATION_TAG_RE.match(str(raw or "").strip())
            if not match:
                continue
            tag = match.group("tag").lower()
            counts[tag] = counts.get(tag, 0) + int(match.group("count"))

    lines: list[str] = []
    plugin_count = counts.pop("server_log_plugin_observation", 0)
    if plugin_count:
        lines.append(
            f"插件低风险观察 {plugin_count} 条：主要涉及更新检查、兼容性/弃用、本地化或可降级提示，未升级为重点事件。"
        )
    performance_count = counts.pop("server_log_performance_observation", 0)
    if performance_count:
        lines.append(
            f"性能旁证 {performance_count} 条：当前未形成持续卡顿或网络事故，仅保留用于时间线对照。"
        )
    community_ops_count = counts.pop("server_log_community_ops_observation", 0)
    if community_ops_count:
        lines.append(
            f"社区活动与普通问答 {community_ops_count} 条：当前未发现活动事故、奖励异常或需要立即处理的社区冲突。"
        )
    player_feedback_count = counts.pop("server_log_player_feedback_observation", 0)
    if player_feedback_count:
        lines.append(
            f"普通建议与反馈 {player_feedback_count} 条：已保留为运营观察，未发现需要立即处理的玩家故障。"
        )
    for tag, count in sorted(counts.items()):
        label = tag.removeprefix("server_log_").removesuffix("_observation")
        lines.append(f"{label} 低风险观察 {count} 条，当前未升级为重点事件。")
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

    if groups:
        hidden_count = len(groups) - MAX_EVENT_SUMMARIES
        if hidden_count > 0:
            items.append(
                f"另有 {hidden_count} 个重点事件未在正文展开；正文已优先展示风险最高的 "
                f"{MAX_EVENT_SUMMARIES} 个，完整证据见附件。"
            )
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
    research_sources = _incident_research_sources(group)
    if research_sources:
        lines.append(f"参考来源：{research_sources}")
    return "\n".join(lines)


def _incident_summary_sentence(group: IncidentGroup, labels: list[str]) -> str:
    issues = list(group.issues)
    ops_subtypes = set(_unique_issue_values(issues, "ops_subtypes"))
    chat_labels = set(_unique_issue_values(issues, "chat_labels"))
    time_part = _incident_time_text(group).replace(" 左右", "")
    if _is_auth_security_group(group):
        return (
            "服务端明确处于离线认证模式，不会自行校验玩家用户名。"
            "若后端可被公网绕过代理直连，攻击者可能冒用任意玩家身份；若仅受控代理可达，则需核对转发密钥和防火墙边界。"
        )
    plugin_configuration = {
        "配置解析异常",
        "技能/内容定义错误",
        "外部 API 凭据缺失",
        "依赖缺失/功能降级",
        "插件不安全模式",
        "外部资源获取失败",
    }
    external_dependencies = {
        "数据库超时",
        "数据库连接异常",
        "经济/商店异常",
        "网络连接异常",
    }
    if "传送/位置异常" in ops_subtypes and chat_labels & {
        "传送异常",
        "虚空/卡位置",
        "跨服异常",
        "世界切换异常",
    }:
        return (
            f"{time_part} 内同时出现服务端传送/位置 WARN 与玩家侧传送、卡位置反馈。"
            "两类证据指向同一链路，应按时间点核对传送插件、世界加载和玩家位置保存。"
        )
    if ops_subtypes & plugin_configuration and ops_subtypes & external_dependencies:
        return (
            f"{time_part} 内同时出现插件配置/内容定义、外部 API 或依赖问题，以及数据库、经济或网络连接线索。"
            "这些是启动窗口内并发的多项异常，应按插件和证据时间分别处理，不应视为单一 Java 报错。"
        )
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
    if group.family in {"player_feedback", "moderation"} and "权限异常" in chat_labels:
        return (
            "玩家明确反馈命令或功能无权限，但当前仅有聊天证据，尚未看到权限插件侧的拒绝日志或配置核验结果。"
            "应先确认目标命令、权限节点、玩家权限组和复现结果。"
        )
    if group.family in {"player_feedback", "moderation"} and (
        "管理员求助" in chat_labels or "证据提交" in chat_labels
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
        if str(group.max_severity or "").lower() in {"high", "critical"}:
            labels_text = "、".join(sorted(chat_labels)) or "重复聊天"
            return (
                f"短时间内集中出现 {labels_text}，证据量和风险等级已超过一般聊天观察；"
                "需要管理员核对原文、频率和上下文后决定是否处理。"
            )
        return (
            "短时间内出现重复或无意义聊天内容，可能影响聊天可读性；当前未见持续辱骂、广告或明确恶意证据，"
            "建议先观察。"
        )
    if group.family == "community_ops":
        return "玩家围绕活动、区域、普通问答或社区内容有正常交流，当前未发现明显冲突或需要管理员立即处理的问题。"
    label_text = "、".join(labels[:4]) if labels else "运行日志异常"
    location = _incident_locations(list(group.issues))
    location_part = f"，位置/后端为 {location}" if location != "未知" else ""
    severity = _severity_label(group)
    return (
        f"在同一事件窗口内识别到 {label_text}{location_part}，"
        f"最高风险等级为{severity}，已按时间和作用域合并为一个事件。"
    )


def _severity_label(group: IncidentGroup) -> str:
    if _is_observation_group(group):
        return "低"
    severity = str(group.max_severity or "low").lower()
    return {
        "critical": "严重",
        "high": "高",
        "medium": "中",
        "low": "低",
        "info": "信息",
    }.get(severity, severity or "低")


def _group_status(group: IncidentGroup) -> str:
    labels = set(_unique_issue_values(list(group.issues), "chat_labels"))
    if _is_auth_security_group(group):
        return "需要管理员确认代理边界与后端访问控制"
    if group.family == "community" and labels & {"外挂举报", "飞行举报", "透视/Xray", "自动挖矿"}:
        return "待人工复核；不建议仅凭聊天处罚"
    if group.family in {"player_feedback", "moderation"}:
        return "未看到明确处理结果"
    if group.family == "chat_review":
        if str(group.max_severity or "").lower() in {"high", "critical"}:
            return "需要管理员复核集中聊天行为"
        return "低风险观察，暂不处罚"
    if group.family == "community_ops":
        return "社区观察，无需立即处理"
    if group.family == "operations":
        return "需要管理员确认"
    return "需要管理员确认"


def _evidence_strength_line(group: IncidentGroup) -> str:
    labels = set(_unique_issue_values(list(group.issues), "chat_labels"))
    details: list[str] = []
    if group.family == "community" and labels & {"外挂举报", "飞行举报", "透视/Xray", "自动挖矿"}:
        details.append("聊天证据为主，暂缺反作弊日志、视频或管理员确认")
    diagnoses = [
        issue.get("ai_diagnosis")
        for issue in group.issues
        if isinstance(issue.get("ai_diagnosis"), dict)
    ]
    if diagnoses:
        radius = max(int(item.get("context_radius") or 0) for item in diagnoses)
        context_count = max(int(item.get("context_records") or 0) for item in diagnoses)
        ai_detail = f"AI 已复核命中行前后各 {radius} 条原文"
        if any(item.get("expanded") for item in diagnoses):
            ai_detail += f"，并智能扩展至 {context_count} 条上下文记录"
        if any(item.get("web_researched") for item in diagnoses):
            ai_detail += "，已辅助检索外部资料"
        details.append(ai_detail)
    return "；".join(details)


def _impact_scope(group: IncidentGroup) -> str:
    issues = list(group.issues)
    ops_subtypes = _unique_issue_values(issues, "ops_subtypes")
    ops_categories = _unique_issue_values(issues, "ops_categories")
    locations = _incident_locations(issues)
    players = _incident_players(issues)
    if ops_subtypes:
        parts = []
        if locations != "未知":
            parts.append(locations)
        if len(ops_subtypes) > 4 and ops_categories:
            parts.extend(ops_categories[:4])
        else:
            parts.extend(ops_subtypes[:4])
        return " / ".join(parts) if parts else "服务器运维链路"
    if players != "未知":
        return f"玩家：{players}"
    return locations if locations != "未知" else "当前聊天/社区上下文"


def _incident_judgement_line(group: IncidentGroup) -> str:
    ai_assessment = _incident_ai_assessment(group)
    if ai_assessment:
        return ai_assessment
    labels = set(_unique_issue_values(list(group.issues), "chat_labels"))
    if _is_auth_security_group(group):
        return "这是服务端明确输出的认证配置风险，不是玩家聊天反馈；是否可接受取决于后端是否只能由受控代理访问。"
    if group.family == "community" and labels & {"外挂举报", "飞行举报", "透视/Xray", "自动挖矿"}:
        return "当前只能说明存在玩家举报或外挂/飞行相关讨论，不能直接判定违规；需要结合外部证据复核。"
    if group.family == "chat_review":
        if str(group.max_severity or "").lower() in {"high", "critical"}:
            return "证据显示集中重复聊天并伴随高风险标签；仍需人工核对原文与上下文，不能仅凭标签自动处罚。"
        return "仅作为聊天观察；未达到明确恶意刷屏、广告或辱骂程度。"
    if group.family in {"player_feedback", "moderation"}:
        return "这是玩家侧反馈，不等同于已确认权限异常或管理事故。"
    return _judgement(group)


def _incident_ai_assessment(group: IncidentGroup) -> str:
    assessments: list[str] = []
    seen: set[str] = set()
    for issue in sorted(group.issues, key=issue_sort_key):
        if not isinstance(issue.get("ai_diagnosis"), dict):
            continue
        assessment = _clean_sentence(str(issue.get("ai_assessment") or "").strip())
        key = _dedupe_key(assessment)
        if not assessment or key in seen:
            continue
        seen.add(key)
        assessments.append(assessment.rstrip("。"))
        if len(assessments) >= 2:
            break
    return "；".join(assessments) + ("。" if assessments else "")


def _ai_incident_action(issues: list[dict[str, Any]]) -> str:
    ai_issues = [
        issue
        for issue in issues
        if issue.get("ai_diagnosis") or issue.get("ai_suggested_action")
    ]
    return _incident_action(ai_issues)


def _incident_research_sources(group: IncidentGroup) -> str:
    sources: list[str] = []
    seen: set[str] = set()
    for issue in sorted(group.issues, key=issue_sort_key):
        diagnosis = issue.get("ai_diagnosis")
        if not isinstance(diagnosis, dict):
            continue
        for source in diagnosis.get("research_sources") or []:
            if not isinstance(source, dict):
                continue
            url = str(source.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            title = str(source.get("title") or "").strip()
            try:
                host = urlparse(url).netloc
            except ValueError:
                host = ""
            label = title or host or "外部资料"
            sources.append(f"{label}：{url}")
            if len(sources) >= 2:
                return "；".join(sources)
    return "；".join(sources)


def _incident_recommended_action(group: IncidentGroup) -> str:
    issues = list(group.issues)
    ai_action = _ai_incident_action(issues)
    if ai_action:
        return ai_action
    ops_subtypes = set(_unique_issue_values(issues, "ops_subtypes"))
    labels = set(_unique_issue_values(issues, "chat_labels"))
    if _is_auth_security_group(group):
        return (
            "确认后端端口不可被公网直连，并核对 Velocity/Bungee 转发模式、forwarding secret 与防火墙；"
            "若无法保证代理隔离，应启用正版验证或立即收紧后端访问。"
        )
    if {"插件加载/启用失败", "数据库超时"} <= ops_subtypes:
        return "检查插件版本与依赖、MariaDB 可用性、QuickShop 配置、网络连通性和 latest.log 完整堆栈。"
    if group.family == "community" and labels & {"外挂举报", "飞行举报", "透视/Xray", "自动挖矿"}:
        return "结合反作弊日志、视频、移动轨迹和上下文人工复核，不建议仅凭聊天处罚。"
    if group.family in {"player_feedback", "moderation"}:
        return "复核聊天上下文，确认是否已有管理员线下处理。"
    if group.family == "chat_review":
        if str(group.max_severity or "").lower() in {"high", "critical"}:
            return "核对证据中的消息原文、发送频率、持续时间和上下文；确认确有广告、辱骂或恶意刷屏后再按规则人工处理。"
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
    text = text.lstrip(": ")
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
            if len(ops_subtypes) > 4 and len(ops_categories) > 1:
                return (
                    f"确定性分类显示该窗口并发涉及 {'、'.join(ops_categories[:4])}；"
                    "这些线索不应被压成单一 Java 故障，需按证据时间和具体插件分别确认根因。"
                )
            return (
                f"确定性分类显示本事件涉及 {'、'.join(ops_categories[:3]) or '运维日志'}"
                f"（{'、'.join(ops_subtypes[:4])}）；需按同一时间窗的完整日志与指标确认根因，"
                "存在玩家反馈时再做交叉验证。"
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

    if any(_is_auth_security_group(group) for group in groups):
        high.append(
            "接入安全：服务器处于离线认证模式；必须确认后端只能由受控代理访问，避免绕过代理冒用玩家身份。"
        )
    if {"插件加载/启用失败", "数据库超时"} <= ops_subtypes:
        high.append(
            "服务器在主要异常时间段出现插件加载失败和 MariaDB/QuickShop 连接超时，可能影响商店、经济、权限或数据同步。"
        )
    elif "数据库超时" in ops_subtypes:
        high.append(
            "数据库连接池在多个时间点出现超时或失效连接，可能影响商店、经济、权限和玩家数据同步。"
        )
    elif "插件加载/启用失败" in ops_subtypes:
        high.append("存在插件加载或启用失败，相关玩法与依赖功能可能不可用。")
    elif _ops_incident_count(groups):
        high.append("存在运维异常，需要优先复核对应时间点的 latest.log、插件堆栈和后端依赖。")

    if chat_labels & {"外挂举报", "飞行举报", "透视/Xray", "自动挖矿"}:
        review.append("聊天中出现疑似外挂/飞行举报，当前缺少反作弊日志、视频或管理员确认，不建议仅凭聊天处罚。")

    if any(
        group.family == "chat_review"
        and str(group.max_severity or "").lower() in {"high", "critical"}
        for group in groups
    ):
        high.append("出现集中刷屏、广告、辱骂或其他高风险聊天行为，需要管理员核对原文、频率与上下文。")

    if any(
        group.family in {"player_feedback", "moderation"}
        and not _is_auth_security_group(group)
        for group in groups
    ):
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


def _action_lines(
    issues: list[dict[str, Any]],
    incident_groups: list[IncidentGroup] | None = None,
    observation_groups: list[IncidentGroup] | None = None,
) -> list[str]:
    actions: list[str] = []
    seen: set[str] = set()
    incidents = incident_groups
    observations = observation_groups
    if incidents is None or observations is None:
        # 向后兼容：未传入已计算的分组时回退到重新分组。
        groups = _INCIDENT_GROUPER.group(_ISSUE_POLICY.actionable_issues(issues))
        incidents, observations = _split_incident_groups(groups)
    grouped_issues = [issue for group in (incidents + observations) for issue in group.issues]
    ops_categories = set(_unique_issue_values(grouped_issues, "ops_categories"))
    ops_subtypes = set(_unique_issue_values(grouped_issues, "ops_subtypes"))
    chat_labels = set(_unique_issue_values(grouped_issues, "chat_labels"))
    covered_ops_categories: set[str] = set()
    if any(_is_auth_security_group(group) for group in incidents):
        _append_unique(
            actions,
            seen,
            "先确认后端端口不可被公网直连，再核对 Velocity/Bungee 转发模式、forwarding secret 与防火墙；无法保证代理隔离时应启用正版验证。",
        )
        covered_ops_categories.add("认证与接入安全")
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
        covered_ops_categories.update({"数据库与存储", "经济与资产"})
    elif "经济与资产" in ops_categories:
        _append_unique(
            actions,
            seen,
            "按玩家和时间核对 Vault、商店记录、经济流水、余额变更和物品发放，再决定是否补偿。",
        )
        covered_ops_categories.add("经济与资产")
    if "插件与模组" in ops_categories:
        _append_unique(
            actions,
            seen,
            "按证据逐项检查插件配置与内容定义、依赖版本、API 凭据和最近变更；优先处理会禁用玩法或影响玩家流程的项目。",
        )
        covered_ops_categories.add("插件与模组")
    if "网络与代理" in ops_categories:
        _append_unique(
            actions,
            seen,
            "区分玩家接入链路与插件外联失败，分别检查代理到后端连通性、外部服务状态、超时配置和防火墙。",
        )
        covered_ops_categories.add("网络与代理")
    if chat_labels & {"外挂举报", "飞行举报", "透视/Xray", "自动挖矿"}:
        _append_unique(
            actions,
            seen,
            "对外挂/飞行相关举报进行人工复核，结合视频、反作弊日志、移动轨迹和上下文，不建议仅凭聊天处罚。",
        )
    if any(
        group.family in {"player_feedback", "moderation"}
        and not _is_auth_security_group(group)
        for group in incidents
    ):
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
    if any(group.family == "chat_review" for group in incidents):
        _append_unique(
            actions,
            seen,
            "核对集中聊天行为的原文、频率、持续时间和上下文，确认违反规则后再由管理员处理。",
        )

    ordered_issues = sorted(
        _ISSUE_POLICY.actionable_issues(issues),
        key=lambda issue: (_action_priority(issue), issue_sort_key(issue)),
    )
    for issue in ordered_issues:
        if len(actions) >= MAX_ACTIONS:
            break
        issue_ops_categories = {
            str(value)
            for value in (issue.get("ops_categories") or ())
            if str(value)
        }
        if issue_ops_categories and issue_ops_categories.issubset(covered_ops_categories):
            continue
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
        return f"完整聊天记录已保存为 {name}"
    return "未生成完整聊天附件"


def _format_players(players: list[str]) -> str:
    if not players:
        return "未知"
    # 先过滤空字符串与纯空白，再切片和计数，避免 "等 N 人" 把空玩家算进去。
    players = [str(p).strip() for p in players if str(p).strip()]
    if not players:
        return "未知"
    shown = players[:16]
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
    if _is_auth_security_group(group):
        return "服务器离线模式与身份认证风险"
    plugin_configuration = {
        "配置解析异常",
        "技能/内容定义错误",
        "外部 API 凭据缺失",
        "依赖缺失/功能降级",
        "插件不安全模式",
        "外部资源获取失败",
    }
    external_dependencies = {
        "数据库超时",
        "数据库连接异常",
        "经济/商店异常",
        "网络连接异常",
    }
    if "传送/位置异常" in ops_subtypes and chat_labels & {
        "传送异常",
        "虚空/卡位置",
        "跨服异常",
        "世界切换异常",
    }:
        return "传送/位置异常与玩家反馈"
    if ops_subtypes & plugin_configuration and ops_subtypes & external_dependencies:
        return "启动阶段插件配置、外部依赖与连接异常"
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
        if "权限异常" in chat_labels:
            return (
                "玩家权限异常与权限日志关联"
                if _has_strong_permission_evidence(group)
                else "玩家权限异常反馈（待核实）"
            )
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
