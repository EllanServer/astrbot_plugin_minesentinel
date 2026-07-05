"""Rule-based analysis for Minecraft runtime logs.

分类优先级（先命中先返回）：
    community > chat_review > player_feedback > community_ops
    > complaint > network > plugin > cross_server > moderation > bug > economy > daily

严重级别：
    critical: 崩溃/OOM/watchdog/服务停止/代理大面积不可用
    high:     循环刷屏、多条 ERROR、插件加载失败、多服务器受影响、性能问题重复
              chat_review 出现威胁/隐私泄露/严重骚扰、community_ops 活动事故
    medium:   单条 ERROR、多条 WARN、单次性能警告、权限/登录/网络异常
              单次聊天违规/广告/可疑链接、活动奖励争议
    low:      单条 WARN、日常 join/quit/start/stop、普通玩家建议、普通活动公告

告警策略：
    critical 直告；high 默认 evidence_count >= min_evidence_count；
    medium 仅在多服务器/多后端、证据数较多或命中敏感词时告警；low 不告警。
    chat_review 默认不告警，除非 severity>=high / evidence_count>=5 / 命中威胁/开盒；
    player_feedback 通常不告警；community_ops 仅活动事故/奖励异常/大范围不满才告警。
"""

from __future__ import annotations

import re
from collections import defaultdict
from functools import lru_cache
from typing import Any

from ..models import MineSentinelConfig, ObservationRecord
from .common import SEVERITY_RANK, format_locations, location_list


# --- 分类关键词表 ---------------------------------------------------------
CATEGORY_KEYS = {
    "daily": (
        "info",
        "started",
        "stopped",
        "done",
        "join",
        "joined",
        "quit",
        "left the game",
        "connected",
        "disconnected",
    ),
    "complaint": (
        "can't keep up",
        "overloaded",
        "lag",
        "lagging",
        "timeout",
        "timed out",
        "tps",
        "mspt",
        "server is overloaded",
        "moved too quickly",
        "moved wrongly",
        "卡顿",
        "延迟",
        "掉线",
        "超时",
        "服务器卡",
    ),
    "bug": (
        "error",
        "exception",
        "failed",
        "failure",
        "fatal",
        "severe",
        "crash",
        "warn",
        "warning",
        "stacktrace",
        "traceback",
        "nullpointerexception",
        "illegalargumentexception",
        "classnotfoundexception",
        "nosuchmethoderror",
        "unsupportedoperationexception",
        "cannot invoke",
        "报错",
        "异常",
        "失败",
        "警告",
        "崩溃",
    ),
    "network": (
        "connection reset",
        "connection refused",
        "connection timed out",
        "read timed out",
        "broken pipe",
        "socket",
        "netty",
        "io.netty",
        "disconnect",
        "disconnected",
        "lost connection",
        "网络",
        "连接失败",
        "连接超时",
        "断开连接",
    ),
    "plugin": (
        "plugin",
        "plugins",
        "enabled plugin",
        "disabling plugin",
        "could not load",
        "could not enable",
        "depend",
        "dependency",
        "softdepend",
        "插件",
        "依赖",
        "加载失败",
        "启用失败",
    ),
    "economy": (
        "economy",
        "vault",
        "shop",
        "money",
        "coin",
        "balance",
        "pay",
        "sell",
        "buy",
        "auction",
        "market",
        "trade",
        "商店",
        "经济",
        "金币",
        "余额",
        "交易",
        "拍卖",
    ),
    "community": (
        "ban",
        "banned",
        "kick",
        "kicked",
        "mute",
        "muted",
        "grief",
        "cheat",
        "cheating",
        "anticheat",
        "xray",
        "fly",
        "speed",
        "reach",
        "kill aura",
        "killaura",
        "violation",
        "vl",
        "封禁",
        "禁言",
        "踢出",
        "作弊",
        "外挂",
    ),
    "chat_review": (
        # 仅保留违规信号词；generic chat/message/said/tell/msg/whisper/pm
        # 会命中所有 [Async Chat Thread] 日志，导致 player_feedback 永远不可达，
        # 且与"辱骂/广告/骚扰/刷屏归 chat_review"的设计意图不符。
        "swear",
        "profanity",
        "insult",
        "abuse",
        "harassment",
        "threat",
        "toxic",
        "advertising",
        "ad",
        "link",
        "url",
        "discord.gg",
        "辱骂",
        "骂人",
        "脏话",
        "骚扰",
        "威胁",
        "广告",
        "刷屏",
        "私聊",
        "举报聊天",
    ),
    "player_feedback": (
        "suggest",
        "suggestion",
        "feedback",
        "idea",
        "request",
        "feature request",
        "proposal",
        "wish",
        "hope",
        "建议",
        "反馈",
        "想法",
        "希望",
        "能不能",
        "可不可以",
        "加个",
        "新增",
        "优化",
        "改进",
    ),
    "community_ops": (
        "event",
        "activity",
        "announcement",
        "notice",
        "reward",
        "vote",
        "poll",
        "rank",
        "season",
        "competition",
        "discord",
        "qq group",
        "community",
        "运营",
        "活动",
        "公告",
        "通知",
        "奖励",
        "投票",
        "赛季",
        "比赛",
        "招募",
        "群",
        "社区",
    ),
    "moderation": (
        "whitelist",
        "permission",
        "permissions",
        "auth",
        "login",
        "logged in",
        "logged out",
        "premium",
        "offline mode",
        "online mode",
        "uuid",
        "session",
        "白名单",
        "权限",
        "登录",
        "认证",
        "正版验证",
    ),
    "cross_server": (
        "velocity",
        "bungeecord",
        "bungee",
        "proxy",
        "backend",
        "server switch",
        "forwarding",
        "modern forwarding",
        "player forwarding",
        "ip forwarding",
        "connection request",
        "转发",
        "后端",
        "代理",
        "跨服",
    ),
    "suggestion": (),
}

# 分类匹配优先级（先匹配先返回）。daily 永远兜底。
CLASSIFY_PRIORITY = (
    "community",
    "chat_review",
    "player_feedback",
    "community_ops",
    "complaint",
    "network",
    "plugin",
    "cross_server",
    "moderation",
    "bug",
    "economy",
    "daily",
)

# --- Marker 常量 ---------------------------------------------------------
ERROR_MARKERS = (
    "error",
    "exception",
    "failed",
    "failure",
    "fatal",
    "severe",
    "crash",
    "报错",
    "异常",
    "失败",
)
WARN_MARKERS = ("warn", "warning", "警告")
PERFORMANCE_MARKERS = (
    "can't keep up",
    "overloaded",
    "lag",
    "timeout",
    "timed out",
    "tps",
    "mspt",
    "卡顿",
    "延迟",
    "超时",
)
NETWORK_MARKERS = CATEGORY_KEYS["network"]
PLUGIN_MARKERS = CATEGORY_KEYS["plugin"]
COMMUNITY_MARKERS = CATEGORY_KEYS["community"]
CHAT_REVIEW_MARKERS = CATEGORY_KEYS["chat_review"]
PLAYER_FEEDBACK_MARKERS = CATEGORY_KEYS["player_feedback"]
COMMUNITY_OPS_MARKERS = CATEGORY_KEYS["community_ops"]
CRITICAL_MARKERS = (
    "fatal",
    "severe",
    "crash",
    "outofmemoryerror",
    "out of memory",
    "watchdog",
    "server stopped",
    "tick took",
    "can't keep up! is the server overloaded",
    "崩溃",
    "内存溢出",
)
# chat_review 中的敏感词：命中即提级 high 并强制告警
CHAT_SENSITIVE_MARKERS = (
    "threat",
    "dox",
    "privacy",
    "威胁",
    "开盒",
    "人肉",
    "隐私",
)
# community_ops 中的事故关键词：命中即提级 high
COMMUNITY_OPS_SEVERE_MARKERS = (
    "奖励发放异常",
    "活动配置错误",
    "活动事故",
    "大范围玩家不满",
    "玩家不满",
    "事故",
)


# --- 关键词匹配辅助 -----------------------------------------------------
def _is_word_key(key: str) -> bool:
    """纯 ASCII 单字关键词（如 ad/pm/vl/fly）需要词边界匹配，避免误伤 load/road 等。"""
    return (
        bool(key)
        and key.isascii()
        and key.isalpha()
        and " " not in key
        and len(key) <= 6
    )


@lru_cache(maxsize=256)
def _word_boundary_regex(keys: tuple[str, ...]) -> "re.Pattern[str] | None":
    """把短英文词编译成单个词边界正则。"""
    word_keys = [k for k in keys if _is_word_key(k)]
    if not word_keys:
        return None
    return re.compile(r"\b(?:" + "|".join(re.escape(k) for k in word_keys) + r")\b")


def _keys_match(text: str, keys: tuple[str, ...]) -> bool:
    """关键词匹配：短 ASCII 单词用词边界，其余（短语/中文）用子串。"""
    word_re = _word_boundary_regex(keys)
    if word_re is not None and word_re.search(text) is not None:
        return True
    return any(key in text for key in keys if not _is_word_key(key))


class HeuristicReportBuilder:
    """Build deterministic fallback facts from SERVER_LOG records."""

    def __init__(self, config: MineSentinelConfig):
        self.config = config
        # 预计算当前生效的分类优先级列表（应用 category_enabled / category_whitelist）。
        # daily 始终兜底，永远保留在末尾。
        self._active_priority: tuple[str, ...] = self._compute_active_priority()

    def _compute_active_priority(self) -> tuple[str, ...]:
        """根据 runtime_log.category_enabled / category_whitelist 计算生效分类。"""
        runtime = self.config.runtime_log
        whitelist = set(runtime.category_whitelist or ())
        disabled = set(
            cat for cat, enabled in (runtime.category_enabled or {}).items()
            if enabled is False
        )
        active = [
            cat
            for cat in CLASSIFY_PRIORITY
            if cat != "daily"
            and cat not in disabled
            and (not whitelist or cat in whitelist)
        ]
        # daily 永远兜底
        active.append("daily")
        return tuple(active)

    def build(
        self,
        records: list[ObservationRecord],
        window_minutes: int,
        server_id: str | None = None,
    ) -> dict[str, Any]:
        log_records = [record for record in records if record.kind == "SERVER_LOG"]
        servers = sorted({record.server_id for record in log_records if record.server_id})
        server_names = sorted(
            {
                record.server_name or record.server_id
                for record in log_records
                if record.server_name or record.server_id
            }
        )
        proxy_ids = sorted({record.proxy_id for record in log_records if record.proxy_id})
        categories: dict[str, list[str]] = {key: [] for key in CATEGORY_KEYS}
        buckets: dict[tuple[str, str], list[ObservationRecord]] = defaultdict(list)

        for record in log_records:
            category = self.classify(record)
            tag = self.tag(record)
            buckets[(category, tag)].append(record)

        for (category, tag), group in buckets.items():
            categories.setdefault(category, [])
            categories[category].append(self._category_line(tag, group))

        issues = []
        max_severity_rank = 0
        for (category, tag), group in sorted(
            buckets.items(), key=lambda item: len(item[1]), reverse=True
        ):
            severity = self._severity(group)
            severity_rank = SEVERITY_RANK.get(severity, 0)
            if severity_rank > max_severity_rank:
                max_severity_rank = severity_rank
            if category == "daily" and severity == "low":
                continue
            affected = sorted({record.server_id for record in group if record.server_id})
            backends = sorted(
                {record.backend_server for record in group if record.backend_server}
            )
            locations = location_list(group)
            samples = [
                item.evidence_text()
                for item in group[: self.config.report.max_evidence_samples]
            ]
            timestamps = [record.timestamp for record in group if record.timestamp]
            should_alert = self._should_alert(
                severity, len(group), affected, backends, category, group
            )
            issues.append(
                {
                    "category": category,
                    "tag": tag,
                    "severity": severity,
                    "confidence": min(0.98, 0.5 + len(group) * 0.08),
                    "affected_servers": affected,
                    "affected_backends": backends,
                    "affected_locations": locations,
                    "affected_locations_text": format_locations(locations),
                    "evidence_count": len(group),
                    "unique_players": 0,
                    "players": [],
                    "players_text": "无",
                    "first_seen_ts": min(timestamps) if timestamps else 0,
                    "last_seen_ts": max(timestamps) if timestamps else 0,
                    "evidence_samples": (
                        samples if self.config.report.include_evidence_samples else []
                    ),
                    "signal_count": len(group),
                    "issue_terms": self._issue_terms(group),
                    "suggested_action": self._suggest_action(category, tag, severity),
                    "should_alert": should_alert,
                }
            )

        if not categories["daily"]:
            categories["daily"].append(
                f"窗口内收到 {len(log_records)} 条 Minecraft 运行日志观察。"
            )

        max_severity = next(
            (name for name, rank in SEVERITY_RANK.items() if rank == max_severity_rank),
            "low",
        ) if max_severity_rank else "low"
        any_alert = any(issue["should_alert"] for issue in issues)
        ops_notes, counters = self._ops_notes(log_records, issues, max_severity, any_alert)

        return {
            "summary": (
                f"最近 {window_minutes} 分钟收到 {len(log_records)} 条 "
                "Minecraft 运行日志观察。"
            ),
            "time_window": f"最近 {window_minutes} 分钟",
            "servers": servers if not server_id else [server_id],
            "server_names": server_names,
            "proxy_ids": proxy_ids,
            "log_count": len(log_records),
            "incident_findings": [],
            "categories": self._categories_dict(categories),
            "issues": issues,
            "ops_notes": ops_notes,
            "max_severity": max_severity,
            "any_alert": any_alert,
            "counters": counters,
        }

    # --- 分类 -------------------------------------------------------------
    def classify(self, record: ObservationRecord) -> str:
        text = self._record_text(record)
        # 按当前生效的优先级列表匹配（已应用 category_enabled / category_whitelist），
        # daily 兜底。被关闭的分类直接跳过，记录会落到下一优先级或 daily。
        for category in self._active_priority:
            if category == "daily":
                continue
            keys = CATEGORY_KEYS.get(category, ())
            if _keys_match(text, keys):
                return category
        return "daily"

    # --- Tag --------------------------------------------------------------
    def tag(self, record: ObservationRecord) -> str:
        text = self._record_text(record)
        level = str((record.context or {}).get("level") or "").lower()
        if "loop_suppressed" in record.tags:
            return f"server_log_loop_{level or 'warn'}"
        # 按分类优先级给 tag
        category = self.classify(record)
        tag_map = {
            "community": "server_log_community",
            "chat_review": "server_log_chat_review",
            "player_feedback": "server_log_player_feedback",
            "community_ops": "server_log_community_ops",
            "complaint": "server_log_performance",
            "network": "server_log_network",
            "plugin": "server_log_plugin",
            "cross_server": "server_log_cross_server",
            "moderation": "server_log_auth",
            "economy": "server_log_economy",
        }
        if category in tag_map:
            return tag_map[category]
        return f"server_log_{level or 'info'}"

    def _category_line(self, tag: str, group: list[ObservationRecord]) -> str:
        servers = ", ".join(sorted({record.server_id for record in group if record.server_id}))
        levels = sorted(
            {
                str((record.context or {}).get("level") or "INFO").upper()
                for record in group
            }
        )
        return (
            f"{tag}: {len(group)} 条运行日志，级别 {', '.join(levels)}，"
            f"服务器 {servers or '未知'}。"
        )

    # --- 严重级别 ---------------------------------------------------------
    def _severity(self, group: list[ObservationRecord]) -> str:
        text = " ".join(self._record_text(record) for record in group)
        n = len(group)
        # 异常分数提级：模板计数突增（EWMA + 分位数）达到高分直接提级
        max_anomaly = 0.0
        for record in group:
            ctx = record.context or {}
            try:
                score = float(ctx.get("anomalyScore") or 0)
            except (TypeError, ValueError):
                score = 0.0
            if score > max_anomaly:
                max_anomaly = score
        if max_anomaly >= 0.8:
            return "critical"
        if max_anomaly >= 0.6:
            # 异常突增至少 high（除非其他规则已判 critical）
            base = self._severity_by_rules(text, n)
            return "critical" if base == "critical" else "high"
        return self._severity_by_rules(text, n)

    def _severity_by_rules(self, text: str, n: int) -> str:
        """关键词 + 计数驱动的 severity 判定（不含异常分数提级）。"""
        # critical: 崩溃 / OOM / watchdog / 服务停止 / 代理大面积不可用
        if any(marker in text for marker in CRITICAL_MARKERS):
            return "critical"
        # high: 循环刷屏
        if "loop_suppressed" in text:
            return "high"
        # high: 插件加载/启用失败
        if any(
            marker in text
            for marker in ("could not load", "could not enable", "加载失败", "启用失败")
        ):
            return "high" if n >= 1 else "medium"
        # high: chat_review 出现威胁/隐私泄露/严重骚扰
        if any(marker in text for marker in CHAT_SENSITIVE_MARKERS):
            return "high"
        # high: community_ops 活动事故/奖励异常/大范围玩家不满
        if any(marker in text for marker in COMMUNITY_OPS_SEVERE_MARKERS):
            return "high"
        # high: 多条 ERROR（substring，便于匹配 errors/failed 等）
        if any(marker in text for marker in ERROR_MARKERS):
            return "high" if n >= 2 else "medium"
        # high: 性能问题重复出现 >=3
        if any(marker in text for marker in PERFORMANCE_MARKERS):
            if n >= 3:
                return "high"
            return "medium" if n >= 1 else "low"
        # high: 网络错误 >=5
        if _keys_match(text, NETWORK_MARKERS):
            if n >= 5:
                return "high"
            return "medium" if n >= 2 else "low"
        # medium: chat_review 单次违规/广告/可疑链接/私聊举报
        if _keys_match(text, CHAT_REVIEW_MARKERS):
            return "medium" if n >= 1 else "low"
        # medium: community_ops 活动/奖励争议
        if _keys_match(text, COMMUNITY_OPS_MARKERS):
            return "medium" if n >= 1 else "low"
        # medium: player_feedback 多名玩家反复提出同类建议
        if _keys_match(text, PLAYER_FEEDBACK_MARKERS):
            return "medium" if n >= 3 else "low"
        # medium: 多条 WARN
        if any(marker in text for marker in WARN_MARKERS):
            return "medium" if n >= 2 else "low"
        # medium: 权限/登录/网络异常
        if _keys_match(text, CATEGORY_KEYS["moderation"]):
            return "medium" if n >= 1 else "low"
        return "low"

    # --- 告警判定 ---------------------------------------------------------
    def _should_alert(
        self,
        severity: str,
        evidence_count: int,
        affected_servers: list[str],
        affected_backends: list[str],
        category: str,
        group: list[ObservationRecord],
    ) -> bool:
        alert = self.config.alert
        if not alert.enabled:
            return False
        # critical 直告，不受 evidence_count 限制
        if severity == "critical":
            return True
        text = " ".join(self._record_text(record) for record in group)
        # 循环刷屏 + high/critical 强制告警
        if "loop_suppressed" in text and severity in {"high", "critical"}:
            return True
        # 多服务器/多后端 + medium/high 强制告警
        multi_scope = len(affected_servers) >= 2 or len(affected_backends) >= 2
        if multi_scope and severity in {"medium", "high"}:
            return True
        # chat_review 特殊规则：默认不告警，除非 severity>=high / evidence_count>=5 / 命中敏感词
        if category == "chat_review":
            if severity in {"high", "critical"}:
                return True
            if any(marker in text for marker in CHAT_SENSITIVE_MARKERS):
                return True
            return evidence_count >= 5
        # player_feedback 通常不告警
        if category == "player_feedback":
            return False
        # community_ops 仅活动事故/奖励异常/大范围不满才告警（已由 severity=high 覆盖）
        if category == "community_ops":
            return severity in {"high", "critical"}
        # low 不告警
        if SEVERITY_RANK.get(severity, 0) < SEVERITY_RANK.get(alert.min_severity, 3):
            return False
        # 标准：severity >= min_severity 且 evidence_count >= min_evidence_count
        return evidence_count >= alert.min_evidence_count

    # --- 推荐动作（按分类细化）--------------------------------------------
    def _suggest_action(self, category: str, tag: str, severity: str) -> str:
        if severity == "critical":
            return (
                "优先处理：检查 latest.log、崩溃报告（crash-reports/）、压缩历史日志、"
                "最近部署/重启/插件更新记录，并确认是否需要临时回滚。"
            )
        if tag.startswith("server_log_loop_"):
            return "优先查看首条样本对应的插件或服务端模块，避免重复报错继续刷屏。"
        if category == "community":
            return (
                "交由社区管理流程复核；确认处罚来源、玩家 UUID、触发规则、证据样本，"
                "避免误封。"
            )
        if category == "chat_review":
            return (
                "交由聊天审查流程复核；检查聊天原文、上下文、玩家 UUID、时间点、频道/私聊来源，"
                "并确认是否涉及辱骂、骚扰、广告、刷屏、威胁或隐私泄露。"
            )
        if category == "player_feedback":
            return (
                "整理为玩家反馈工单；记录玩家诉求、出现频率、影响范围和可执行性，"
                "交由社区运营或产品负责人评估。"
            )
        if category == "community_ops":
            return (
                "交由社区运营跟进；确认活动、公告、奖励、投票、赛季或玩家关系相关上下文，"
                "评估是否需要发布公告、回复玩家、调整活动规则或同步管理组。"
            )
        if category == "complaint" or tag == "server_log_performance":
            return (
                "检查 TPS、MSPT、内存、实体数量、区块加载、红石机器、定时任务和插件耗时；"
                "优先对照 spark/timings 与 latest.log。"
            )
        if category == "network" or tag == "server_log_network":
            return (
                "检查代理到后端的连通性、端口、防火墙、Velocity/Bungee 转发配置、"
                "后端在线状态，以及玩家来源网络是否集中异常。"
            )
        if category == "plugin" or tag == "server_log_plugin":
            return (
                "检查报错首条堆栈对应插件、插件版本、服务端核心版本、依赖插件是否缺失，"
                "以及最近是否更新过插件或配置。"
            )
        if category == "cross_server" or tag == "server_log_cross_server":
            return (
                "检查 Velocity/Bungee 配置、player-info-forwarding-mode、forwarding secret、"
                "后端服务器地址、端口、转发协议和防火墙。"
            )
        if category == "moderation" or tag == "server_log_auth":
            return (
                "检查权限组、白名单、登录插件、正版验证、UUID 模式，"
                "以及代理和后端的转发配置是否一致。"
            )
        if category == "economy" or tag == "server_log_economy":
            return (
                "检查 Vault、经济插件、商店插件、数据库连接、玩家余额数据和最近交易记录。"
            )
        if severity in {"high", "critical"}:
            return (
                "优先检查 latest.log、压缩历史日志、崩溃报告、最近部署/重启/插件更新记录，"
                "并评估是否需要回滚。"
            )
        if severity == "medium":
            return "继续观察同类 WARN/ERROR 是否扩大，并保留样本用于后续排查。"
        return "持续观察，无需立即处理。"

    # --- 运维备注（增强版）------------------------------------------------
    def _ops_notes(
        self,
        records: list[ObservationRecord],
        issues: list[dict[str, Any]],
        max_severity: str,
        any_alert: bool,
    ) -> tuple[list[str], dict[str, int]]:
        notes: list[str] = []
        counters: dict[str, int] = {
            "error": 0,
            "warn": 0,
            "performance": 0,
            "network": 0,
            "plugin": 0,
            "chat_review": 0,
            "player_feedback": 0,
            "community_ops": 0,
            "loop_suppressed": 0,
            "affected_servers": 0,
            "affected_backends": 0,
        }

        loop_summaries = [
            record for record in records if "loop_suppressed" in record.tags
        ]
        suppressed = sum(
            int((record.context or {}).get("loopSuppressed") or 0)
            for record in loop_summaries
        )
        counters["loop_suppressed"] = suppressed
        if suppressed:
            notes.append(
                f"已过滤 {suppressed} 条重复服务器报错循环日志，建议优先查看首条原始样本。"
            )

        for record in records:
            text = self._record_text(record)
            if any(marker in text for marker in ERROR_MARKERS):
                counters["error"] += 1
            if any(marker in text for marker in WARN_MARKERS):
                counters["warn"] += 1
            if any(marker in text for marker in PERFORMANCE_MARKERS):
                counters["performance"] += 1
            if _keys_match(text, NETWORK_MARKERS):
                counters["network"] += 1
            if _keys_match(text, PLUGIN_MARKERS):
                counters["plugin"] += 1
            if _keys_match(text, CHAT_REVIEW_MARKERS):
                counters["chat_review"] += 1
            if _keys_match(text, PLAYER_FEEDBACK_MARKERS):
                counters["player_feedback"] += 1
            if _keys_match(text, COMMUNITY_OPS_MARKERS):
                counters["community_ops"] += 1

        affected_servers_set: set[str] = set()
        affected_backends_set: set[str] = set()
        for issue in issues:
            affected_servers_set.update(issue.get("affected_servers") or [])
            affected_backends_set.update(issue.get("affected_backends") or [])
        counters["affected_servers"] = len(affected_servers_set)
        counters["affected_backends"] = len(affected_backends_set)

        counter_parts = []
        if counters["error"]:
            counter_parts.append(f"ERROR {counters['error']} 条")
        if counters["warn"]:
            counter_parts.append(f"WARN {counters['warn']} 条")
        if counters["performance"]:
            counter_parts.append(f"PERFORMANCE {counters['performance']} 条")
        if counters["network"]:
            counter_parts.append(f"NETWORK {counters['network']} 条")
        if counters["plugin"]:
            counter_parts.append(f"PLUGIN {counters['plugin']} 条")
        if counter_parts:
            notes.append("窗口内 " + "，".join(counter_parts) + "。")

        ops_parts = []
        if counters["chat_review"]:
            ops_parts.append(f"聊天审查 {counters['chat_review']} 条")
        if counters["player_feedback"]:
            ops_parts.append(f"玩家建议 {counters['player_feedback']} 条")
        if counters["community_ops"]:
            ops_parts.append(f"社区运营 {counters['community_ops']} 条")
        if ops_parts:
            notes.append("窗口内 " + "，".join(ops_parts) + "。")

        scope_parts = []
        if counters["affected_servers"]:
            scope_parts.append(f"{counters['affected_servers']} 个服务器")
        if counters["affected_backends"]:
            scope_parts.append(f"{counters['affected_backends']} 个后端")
        if scope_parts:
            notes.append(
                f"影响 {'、'.join(scope_parts)}，最高严重级别 {max_severity}。"
            )

        if any_alert:
            triggered = next(
                (issue for issue in issues if issue.get("should_alert")),
                None,
            )
            if triggered:
                notes.append(
                    f"已达到告警条件：severity={triggered['severity']}，"
                    f"evidence_count={triggered['evidence_count']}。"
                )
            else:
                notes.append("已达到告警条件。")

        return notes, counters

    # --- 辅助 -------------------------------------------------------------
    def _categories_dict(self, categories: dict[str, list[str]]) -> dict[str, list[str]]:
        """按固定顺序输出 categories，包含 network/plugin/chat_review/player_feedback/community_ops。"""
        return {
            "daily": categories.get("daily", []),
            "complaint": categories.get("complaint", []),
            "bug": categories.get("bug", []),
            "network": categories.get("network", []),
            "plugin": categories.get("plugin", []),
            "economy": categories.get("economy", []),
            "community": categories.get("community", []),
            "chat_review": categories.get("chat_review", []),
            "player_feedback": categories.get("player_feedback", []),
            "community_ops": categories.get("community_ops", []),
            "moderation": categories.get("moderation", []),
            "cross_server": categories.get("cross_server", []),
            "suggestion": categories.get("suggestion", []),
        }

    @staticmethod
    def _issue_terms(group: list[ObservationRecord]) -> list[str]:
        terms: list[str] = []
        # severity markers 用 substring（便于匹配 errors/failed 等）
        substring_markers = CRITICAL_MARKERS + ERROR_MARKERS + WARN_MARKERS + PERFORMANCE_MARKERS
        for marker in substring_markers:
            if any(marker in HeuristicReportBuilder._record_text(record) for record in group):
                terms.append(marker)
            if len(terms) >= 8:
                return terms
        # 分类 markers 用 _keys_match（与 classify 一致）
        category_marker_groups = (
            NETWORK_MARKERS,
            PLUGIN_MARKERS,
            COMMUNITY_MARKERS,
            CHAT_REVIEW_MARKERS,
            PLAYER_FEEDBACK_MARKERS,
            COMMUNITY_OPS_MARKERS,
        )
        combined_text = " ".join(
            HeuristicReportBuilder._record_text(record) for record in group
        )
        for markers in category_marker_groups:
            for marker in markers:
                if _is_word_key(marker):
                    # 词边界匹配的交给 _keys_match 整体判断，单独词不重复输出
                    continue
                if marker in combined_text and marker not in terms:
                    terms.append(marker)
                    if len(terms) >= 8:
                        return terms
        # 补齐词边界命中的短词
        for markers in category_marker_groups:
            word_re = _word_boundary_regex(markers)
            if word_re is None:
                continue
            for match in word_re.finditer(combined_text):
                word = match.group(0)
                if word not in terms:
                    terms.append(word)
                    if len(terms) >= 8:
                        return terms
        return terms

    @staticmethod
    def _record_text(record: ObservationRecord) -> str:
        return f"{record.content} {' '.join(record.tags)}".lower()
