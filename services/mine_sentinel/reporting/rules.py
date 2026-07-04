"""Rule-based analysis for Minecraft runtime logs."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..models import MineSentinelConfig, ObservationRecord
from .common import SEVERITY_RANK, format_locations, location_list


CATEGORY_KEYS = {
    "daily": ("info", "started", "stopped", "done", "join", "quit"),
    "complaint": (
        "can't keep up",
        "overloaded",
        "lag",
        "timeout",
        "timed out",
        "disconnect",
        "延迟",
        "卡顿",
        "超时",
        "掉线",
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
        "报错",
        "异常",
        "失败",
        "警告",
    ),
    "economy": ("economy", "vault", "shop", "money", "coin", "商店", "经济"),
    "community": (
        "ban",
        "banned",
        "kick",
        "kicked",
        "mute",
        "muted",
        "report",
        "reported",
        "spam",
        "profanity",
        "grief",
        "griefing",
        "cheat",
        "anticheat",
        "anti-cheat",
        "xray",
        "举报",
        "封禁",
        "禁言",
        "踢出",
        "刷屏",
        "作弊",
        "外挂",
        "破坏",
    ),
    "moderation": ("whitelist", "permission", "auth", "login", "权限", "白名单"),
    "suggestion": (),
    "cross_server": ("velocity", "proxy", "backend", "server switch", "转发", "后端"),
}

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
COMMUNITY_MARKERS = CATEGORY_KEYS["community"]
WARN_MARKERS = ("warn", "warning", "警告")
PERFORMANCE_MARKERS = (
    "can't keep up",
    "overloaded",
    "lag",
    "timeout",
    "timed out",
    "tps",
    "卡顿",
    "延迟",
    "超时",
)


class HeuristicReportBuilder:
    """Build deterministic fallback facts from SERVER_LOG records."""

    def __init__(self, config: MineSentinelConfig):
        self.config = config

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
        for (category, tag), group in sorted(
            buckets.items(), key=lambda item: len(item[1]), reverse=True
        ):
            severity = self._severity(group)
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
                    "suggested_action": self._suggest_action(tag, severity),
                    "should_alert": self._should_alert(severity, len(group)),
                }
            )

        if not categories["daily"]:
            categories["daily"].append(
                f"窗口内收到 {len(log_records)} 条 Minecraft 运行日志观察。"
            )

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
            "categories": {
                "daily": categories.get("daily", []),
                "complaint": categories.get("complaint", []),
                "bug": categories.get("bug", []),
                "economy": categories.get("economy", []),
                "community": categories.get("community", []),
                "moderation": categories.get("moderation", []),
                "suggestion": categories.get("suggestion", []),
                "cross_server": categories.get("cross_server", []),
            },
            "issues": issues,
            "ops_notes": self._ops_notes(log_records),
        }

    def classify(self, record: ObservationRecord) -> str:
        text = self._record_text(record)
        if any(marker in text for marker in COMMUNITY_MARKERS):
            return "community"
        if any(marker in text for marker in PERFORMANCE_MARKERS):
            return "complaint"
        if any(marker in text for marker in ERROR_MARKERS + WARN_MARKERS):
            return "bug"
        for category, keywords in CATEGORY_KEYS.items():
            if category in {"daily", "bug", "complaint"}:
                continue
            if any(keyword and keyword in text for keyword in keywords):
                return category
        return "daily"

    def tag(self, record: ObservationRecord) -> str:
        text = self._record_text(record)
        level = str((record.context or {}).get("level") or "").lower()
        if "loop_suppressed" in record.tags:
            return f"server_log_loop_{level or 'warn'}"
        if any(marker in text for marker in COMMUNITY_MARKERS):
            return "server_log_community"
        if any(marker in text for marker in CATEGORY_KEYS["moderation"]):
            return "server_log_auth"
        if any(marker in text for marker in PERFORMANCE_MARKERS):
            return "server_log_performance"
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

    def _severity(self, group: list[ObservationRecord]) -> str:
        text = " ".join(self._record_text(record) for record in group)
        if "loop_suppressed" in text:
            return "high"
        if any(marker in text for marker in ("fatal", "severe", "crash")):
            return "critical"
        if any(marker in text for marker in ERROR_MARKERS):
            return "high" if len(group) >= 2 else "medium"
        if any(marker in text for marker in WARN_MARKERS + PERFORMANCE_MARKERS):
            return "medium" if len(group) >= 2 else "low"
        return "low"

    def _should_alert(self, severity: str, evidence_count: int) -> bool:
        alert = self.config.alert
        return (
            alert.enabled
            and SEVERITY_RANK.get(severity, 0)
            >= SEVERITY_RANK.get(alert.min_severity, 3)
            and evidence_count >= alert.min_evidence_count
        )

    def _suggest_action(self, tag: str, severity: str) -> str:
        if tag.startswith("server_log_loop_"):
            return "优先查看首条样本对应的插件或服务端模块，避免重复报错继续刷屏。"
        if tag == "server_log_community":
            return "交给社区管理流程复核，保留日志时间点、玩家名和对应插件上下文。"
        if tag == "server_log_auth":
            return "检查权限、登录、白名单或认证插件配置，并按日志时间点复核影响范围。"
        if tag == "server_log_performance":
            return "检查 TPS、内存、实体数量、区块加载和近期插件任务，确认是否存在性能瓶颈。"
        if severity in ("high", "critical"):
            return "尽快查看 Minecraft latest.log 与压缩历史日志，确认根因后再处理。"
        if severity == "medium":
            return "继续观察同类 WARN/ERROR 是否扩大，必要时按日志文件和时间点人工复核。"
        return "持续观察运行日志即可。"

    def _ops_notes(self, records: list[ObservationRecord]) -> list[str]:
        notes: list[str] = []
        loop_summaries = [
            record for record in records if "loop_suppressed" in record.tags
        ]
        suppressed = sum(
            int((record.context or {}).get("loopSuppressed") or 0)
            for record in loop_summaries
        )
        if suppressed:
            notes.append(f"已过滤 {suppressed} 条重复服务器报错循环日志。")
        error_count = sum(
            1
            for record in records
            if any(marker in self._record_text(record) for marker in ERROR_MARKERS)
        )
        warn_count = sum(
            1
            for record in records
            if any(marker in self._record_text(record) for marker in WARN_MARKERS)
        )
        if error_count or warn_count:
            notes.append(f"窗口内 ERROR/异常 {error_count} 条，WARN/警告 {warn_count} 条。")
        return notes

    @staticmethod
    def _issue_terms(group: list[ObservationRecord]) -> list[str]:
        terms: list[str] = []
        for marker in ERROR_MARKERS + WARN_MARKERS + PERFORMANCE_MARKERS + COMMUNITY_MARKERS:
            if any(marker in HeuristicReportBuilder._record_text(record) for record in group):
                terms.append(marker)
            if len(terms) >= 8:
                break
        return terms

    @staticmethod
    def _record_text(record: ObservationRecord) -> str:
        return f"{record.content} {' '.join(record.tags)}".lower()
