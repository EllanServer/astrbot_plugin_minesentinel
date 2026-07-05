"""Chinese label catalog for MineSentinel report presentation."""

from __future__ import annotations

import re
from typing import Any


CATEGORY_TITLES = {
    "daily": "日常观察",
    "complaint": "性能/可用性异常",
    "bug": "服务端/插件异常",
    "network": "网络/连接异常",
    "plugin": "插件相关日志",
    "economy": "经济/商店相关日志",
    "community": "社区管理",
    "chat_review": "聊天审查",
    "player_feedback": "玩家反馈",
    "community_ops": "社区运营",
    "moderation": "权限/登录相关日志",
    "cross_server": "代理/跨服相关日志",
    "suggestion": "人工关注事项",
}

GENERIC_TAG_TITLES = {
    "server_log_info": "服务器运行日志",
    "server_log_warn": "服务器警告日志",
    "server_log_error": "服务器错误日志",
    "server_log_fatal": "服务器严重错误日志",
    "server_log_severe": "服务器严重错误日志",
    "server_log_performance": "服务器性能异常日志",
    "server_log_loop_warn": "重复警告日志",
    "server_log_loop_error": "重复错误日志",
    "server_log_loop_fatal": "重复严重错误日志",
    "server_log_loop_severe": "重复严重错误日志",
    "server_log_community": "社区管理日志",
    "server_log_chat_review": "聊天审查日志",
    "server_log_player_feedback": "玩家反馈日志",
    "server_log_community_ops": "社区运营日志",
    "server_log_auth": "权限/登录日志",
    "server_log_network": "网络/连接日志",
    "server_log_plugin": "插件日志",
    "server_log_cross_server": "跨服/代理日志",
    "server_log_economy": "经济/商店日志",
    "plugin_error": "插件错误",
    "server_switch": "跨服切换",
}


class LabelCatalog:
    """Translate deterministic report categories and tags into reader-facing text."""

    def __init__(
        self,
        tag_titles: dict[str, str] | None = None,
        category_titles: dict[str, str] | None = None,
        generic_tag_titles: dict[str, str] | None = None,
    ):
        self.tag_titles = tag_titles or {}
        self.category_titles = category_titles or CATEGORY_TITLES
        self.generic_tag_titles = generic_tag_titles or GENERIC_TAG_TITLES

    def issue_title(self, issue: dict[str, Any]) -> str:
        title = str(issue.get("title") or "").strip()
        if title and not self.looks_like_raw_tag(title):
            return title
        tag_title = self.tag_title(issue.get("tag"))
        if tag_title:
            return tag_title
        category = str(issue.get("category") or "").lower()
        return self.category_titles.get(category) or "运行日志事件"

    def tag_title(self, value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""

        parts = [part.strip() for part in re.split(r"[,，;；]+", raw) if part.strip()]
        if len(parts) > 1:
            return "、".join(
                unique_text(
                    [label for label in (self.single_tag_title(part) for part in parts) if label]
                )
            )
        return self.single_tag_title(raw)

    def single_tag_title(self, value: str) -> str:
        tag = value.strip().lower()
        if not tag:
            return ""
        return self.tag_titles.get(tag) or self.generic_tag_titles.get(tag) or ""

    def looks_like_raw_tag(self, value: str) -> bool:
        text = value.strip()
        if not text:
            return False
        if "," in text or "，" in text or ";" in text or "；" in text:
            return any(self.single_tag_title(part) for part in re.split(r"[,，;；]+", text))
        return bool(re.fullmatch(r"[a-z0-9_:-]+", text) and self.single_tag_title(text))


def unique_text(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = re.sub(r"\s+", "", value).lower()[:120]
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


DEFAULT_LABELS = LabelCatalog()
