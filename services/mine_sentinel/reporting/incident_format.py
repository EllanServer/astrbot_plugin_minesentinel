"""Shared formatting helpers for MineSentinel report renderers.

These functions are extracted from ``text_renderer`` and ``image_renderer``
where they previously existed as byte-for-byte identical (or trivially
differing) copies. Renderers should import from here instead of redefining
them, so bug fixes land in one place.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from ..issue_formatting import format_millis
from .incidents import IncidentGroup, issue_sort_key


def incident_title(group: IncidentGroup, labels: list[str]) -> str:
    if group.family == "community":
        return labels[0] if labels else "社区管理相关日志"
    if group.family == "chat_review":
        return labels[0] if labels else "聊天审查相关日志"
    if group.family == "player_feedback":
        return labels[0] if labels else "玩家反馈相关日志"
    if group.family == "community_ops":
        return labels[0] if labels else "社区运营相关日志"
    if group.family == "moderation":
        return "权限/登录相关运行日志异常"
    if group.family == "suggestion":
        return labels[0] if labels else "人工关注事项"
    if len(labels) > 1:
        return "服务器集中出现多类运行日志异常"
    return labels[0] if labels else "运行日志异常"


def incident_time_text(group: IncidentGroup) -> str:
    start = as_millis(group.start_ts)
    end = as_millis(group.end_ts)
    if start and end and start != end:
        return f"{format_millis(start)} ~ {format_millis(end)} 左右"
    if start or end:
        return f"{format_millis(start or end)} 左右"
    return "本窗口内"


def format_time_window(report: dict) -> str:
    start = as_millis(report.get("window_start_ts"))
    end = as_millis(report.get("window_end_ts"))
    if start and end:
        start_day = time.strftime("%Y-%m-%d", time.localtime(start / 1000))
        end_day = time.strftime("%Y-%m-%d", time.localtime(end / 1000))
        start_hm = time.strftime("%H:%M", time.localtime(start / 1000))
        end_hm = time.strftime("%H:%M", time.localtime(end / 1000))
        if start_day == end_day:
            return f"{start_day} {start_hm} - {end_hm}"
        return f"{start_day} {start_hm} - {end_day} {end_hm}"
    return str(report.get("time_window") or "未知")


def format_duration(report: dict) -> str:
    start = as_millis(report.get("window_start_ts"))
    end = as_millis(report.get("window_end_ts"))
    minutes = 0
    if start and end and end > start:
        minutes = max(1, round((end - start) / 60000))
    else:
        try:
            minutes = int(report.get("_window_minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
    if not minutes:
        match = re.search(r"最近\s*(\d+)\s*分钟", str(report.get("time_window") or ""))
        if match:
            minutes = int(match.group(1))
    if minutes and minutes >= 60:
        hours = minutes // 60
        remainder = minutes % 60
        if remainder:
            return f"约 {hours} 小时 {remainder} 分钟"
        return f"{hours} 小时"
    if minutes:
        return f"{minutes} 分钟"
    return "本窗口"


def evidence_line(total_count: int, dedupe_count: int, unique_players: int) -> str:
    if dedupe_count:
        return f"证据：共 {total_count} 条运行日志观察，去重 {dedupe_count} 条。"
    return f"证据：共 {total_count} 条运行日志观察。"


def clean_sentence(value: str) -> str:
    text = value.strip()
    text = re.sub(r"^-+\s*", "", text)
    if not text:
        return ""
    if text[-1] not in "。！？.!?":
        text += "。"
    return text


_DEDUPE_RE = re.compile(r"\s+")


def dedupe_key(value: str) -> str:
    return _DEDUPE_RE.sub("", value).lower()[:120]


def as_millis(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def quiet_window_text(report: dict, groups: list[IncidentGroup]) -> str:
    """Shared quiet-window caption used by both renderers.

    Returns an empty string when there is nothing to say.
    """
    if not groups:
        return ""
    if len(groups) == 1:
        time_text = incident_time_text(groups[0]).replace(" 左右", "")
        return (
            f"除 {time_text} 的集中异常外，当前摘要中没有体现其他时间段的"
            "持续 ERROR/WARN 或重复报错循环。"
        )
    return "除上述事件外，当前摘要中没有体现其他时间段的持续 ERROR/WARN 或重复报错循环。"


def resolve_attachment_name(report: dict) -> str:
    """Return the bare attachment file name, or "" when none was produced.

    Both renderers need this; ``text_renderer`` wrapped it in a sentence while
    ``image_renderer`` used the bare name. The shared core lives here.
    """
    name = str(report.get("_export_file_name") or "").strip()
    if not name and report.get("_export_file_path"):
        name = Path(str(report["_export_file_path"])).name
    return name


def is_attachment_note(note: str) -> bool:
    return (
        "完整 observation 文件" in note
        or "完整审计日志附件" in note
    )
