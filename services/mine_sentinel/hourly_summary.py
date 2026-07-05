"""MineSentinel hourly summary mode.

Every hour, on the hour, the job reads that hour's Minecraft logs directly
from the server's logs/ directory (latest.log + .log.gz archives), turns them
into in-memory observations, and asks the LLM to produce a compact hourly
summary. The summary is persisted to disk and kept in memory.

After `hours_per_cycle` (default 8) hourly summaries have been collected,
the job asks the LLM to integrate them into a single cycle report and
delivers it via the regular report dispatcher. The cycle then restarts.

This mode does NOT poll latest.log in a tight loop, so it has zero impact on
the Minecraft server's mspt/tps.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .models import MineSentinelConfig, ObservationRecord
from .reporting.ai_normalizer import parse_json_object, repair_json_object_text
from .reporting.rules import HeuristicReportBuilder


@dataclass
class HourlySummary:
    """A single hour's summary produced by the LLM (or heuristic fallback)."""

    server_id: str
    server_name: str
    hour_start_ms: int
    hour_end_ms: int
    records_count: int
    error_count: int
    warning_count: int
    info_count: int
    summary: str
    key_issues: list[dict[str, Any]] = field(default_factory=list)
    top_events: list[str] = field(default_factory=list)
    source: str = "heuristic"  # heuristic | ai
    raw_report: dict[str, Any] = field(default_factory=dict)

    @property
    def hour_label(self) -> str:
        start = datetime.fromtimestamp(self.hour_start_ms / 1000)
        end = datetime.fromtimestamp(self.hour_end_ms / 1000)
        return f"{start:%Y-%m-%d %H:00}~{end:%H:00}"


class HourlySummaryStore:
    """Persists hourly summaries to disk as JSON, one file per hour per server."""

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir) / "hourly_summaries"

    def _server_dir(self, server_id: str) -> Path:
        safe = server_id or "default"
        path = self.base_dir / safe
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save(self, summary: HourlySummary) -> Path:
        server_dir = self._server_dir(summary.server_id)
        hour = datetime.fromtimestamp(summary.hour_start_ms / 1000)
        filename = f"{hour:%Y%m%d%H}.json"
        path = server_dir / filename
        path.write_text(
            json.dumps(asdict(summary), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def load(self, server_id: str, hour_start_ms: int) -> HourlySummary | None:
        server_dir = self._server_dir(server_id)
        hour = datetime.fromtimestamp(hour_start_ms / 1000)
        path = server_dir / f"{hour:%Y%m%d%H}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return HourlySummary(**data)
        except Exception as exc:
            logger.debug(f"[MineSentinel] failed to load hourly summary {path}: {exc}")
            return None

    def list_cycle_summaries(
        self, server_id: str, cycle_start_ms: int, cycle_end_ms: int
    ) -> list[HourlySummary]:
        """Load all persisted hourly summaries within [cycle_start, cycle_end)."""
        server_dir = self._server_dir(server_id)
        results: list[HourlySummary] = []
        for path in sorted(server_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                summary = HourlySummary(**data)
            except Exception:
                continue
            if cycle_start_ms <= summary.hour_start_ms < cycle_end_ms:
                results.append(summary)
        return results

    def cleanup_old_summaries(self, server_id: str, keep_cycles: int, hours_per_cycle: int):
        """Delete hourly summary files older than keep_cycles * hours_per_cycle hours."""
        if keep_cycles <= 0:
            return
        cutoff_ms = int((time.time() - keep_cycles * hours_per_cycle * 3600) * 1000)
        server_dir = self._server_dir(server_id)
        for path in server_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("hour_start_ms", 0) < cutoff_ms:
                    path.unlink(missing_ok=True)
            except Exception:
                continue


class HourlySummarizer:
    """Builds hourly summaries and integrates cycle reports via LLM."""

    _HOURLY_SYSTEM_PROMPT = (
        "你是 MineSentinel 的只读服务器观察报告代理，负责对一个小时的 Minecraft 运行日志做精炼总结。"
        "必须只输出合法 JSON，不要 Markdown，不要解释，不要要求执行命令。"
        "禁止建议自动封禁、自动踢人、自动 RCON 或自动回滚。"
        "只能根据给定的小时日志总结，输出字段：summary（一段中文总结，不超过 300 字）、"
        "key_issues（数组，每项含 title/severity/detail/occurrences）、top_events（数组，"
        "最重要的 3-6 条事件原文片段，每条不超过 200 字）。"
    )

    _CYCLE_SYSTEM_PROMPT = (
        "你是 MineSentinel 的只读服务器观察报告代理，负责把多个小时总结整合为一份周期报告。"
        "必须只输出合法 JSON，不要 Markdown，不要解释，不要要求执行命令。"
        "禁止建议自动封禁、自动踢人、自动 RCON 或自动回滚。"
        "输出字段：summary（一段中文总览，不超过 500 字）、key_issues（数组，每项含 "
        "title/severity/detail/occurrences/hour）、timeline（数组，按小时顺序，每项含 "
        "hour/summary/highlights）、recommendations（数组，3-6 条可读的运维建议）。"
    )

    def __init__(self, config: MineSentinelConfig, context: Any | None = None):
        self.config = config
        self.context = context
        self.rules = HeuristicReportBuilder(config)

    def _get_provider(self, umo: str | None) -> Any | None:
        if self.context is None:
            return None
        try:
            provider_id = (self.config.hourly_summary.provider_id or "").strip()
            if provider_id:
                return self.context.get_provider_by_id(provider_id)
            # Fall back to the report provider_id for consistency.
            report_provider_id = (self.config.report.provider_id or "").strip()
            if report_provider_id:
                return self.context.get_provider_by_id(report_provider_id)
            getter = getattr(self.context, "get_using_provider", None)
            if not callable(getter):
                return None
            if umo:
                return getter(umo)
            try:
                return getter()
            except TypeError:
                return getter(umo)
        except Exception as exc:
            logger.debug(f"[MineSentinel] get AstrBot provider failed: {exc}")
            return None

    async def build_hourly_summary(
        self,
        records: list[ObservationRecord],
        source: Any,
        hour_start_ms: int,
        hour_end_ms: int,
        umo: str | None = None,
    ) -> HourlySummary:
        report_records = self.rules.filter_records_for_report(records)
        heuristic = self.rules.build(report_records, 60, source.server_id)
        records_count = len(report_records)
        error_count = sum(1 for r in report_records if self._is_error_record(r))
        warning_count = sum(1 for r in report_records if self._is_warning_record(r))
        info_count = records_count - error_count - warning_count
        summary_text = self._heuristic_hourly_text(heuristic, records_count)
        key_issues = self._heuristic_key_issues(heuristic)
        top_events = self._heuristic_top_events(report_records)

        hourly = HourlySummary(
            server_id=source.server_id,
            server_name=source.server_name,
            hour_start_ms=hour_start_ms,
            hour_end_ms=hour_end_ms,
            records_count=records_count,
            error_count=error_count,
            warning_count=warning_count,
            info_count=info_count,
            summary=summary_text,
            key_issues=key_issues,
            top_events=top_events,
            source="heuristic",
            raw_report=heuristic,
        )

        provider = self._get_provider(umo)
        if provider is None:
            return hourly

        prompt = self._build_hourly_prompt(
            report_records,
            source,
            hour_start_ms,
            hour_end_ms,
            heuristic,
        )
        try:
            result = await provider.text_chat(
                prompt=prompt,
                system_prompt=self._HOURLY_SYSTEM_PROMPT,
                session_id="minesentinel-hourly",
                persist=False,
            )
            raw = getattr(result, "completion_text", None) or ""
        except Exception as exc:
            logger.debug(f"[MineSentinel] hourly provider.text_chat failed: {exc}")
            return hourly

        parsed = parse_json_object(raw)
        if parsed is None:
            repaired = repair_json_object_text(raw)
            parsed = parse_json_object(repaired) if repaired else None
        if not parsed:
            return hourly

        hourly.summary = str(parsed.get("summary") or summary_text)[:2000]
        hourly.key_issues = list(parsed.get("key_issues") or key_issues)[:50]
        hourly.top_events = [str(e) for e in (parsed.get("top_events") or top_events)][:20]
        hourly.source = "ai"
        return hourly

    async def build_cycle_report(
        self,
        hourly_summaries: list[HourlySummary],
        server_id: str,
        umo: str | None = None,
    ) -> dict[str, Any]:
        heuristic = self._build_cycle_heuristic(hourly_summaries, server_id)
        provider = self._get_provider(umo)
        if provider is None:
            return heuristic
        prompt = self._build_cycle_prompt(hourly_summaries, server_id, heuristic)
        try:
            result = await provider.text_chat(
                prompt=prompt,
                system_prompt=self._CYCLE_SYSTEM_PROMPT,
                session_id="minesentinel-cycle",
                persist=False,
            )
            raw = getattr(result, "completion_text", None) or ""
        except Exception as exc:
            logger.debug(f"[MineSentinel] cycle provider.text_chat failed: {exc}")
            return heuristic
        parsed = parse_json_object(raw)
        if parsed is None:
            repaired = repair_json_object_text(raw)
            parsed = parse_json_object(repaired) if repaired else None
        if not parsed:
            return heuristic
        return self._normalize_cycle_report(parsed, heuristic)

    def _heuristic_hourly_text(self, heuristic: dict, count: int) -> str:
        summary = heuristic.get("summary") or ""
        if not isinstance(summary, str):
            summary = str(summary)
        issues = heuristic.get("issues") or []
        if not summary:
            summary = f"本小时共记录 {count} 条日志事件"
        if issues:
            summary += f"，发现 {len(issues)} 个问题。"
        return summary[:1000]

    def _heuristic_key_issues(self, heuristic: dict) -> list[dict[str, Any]]:
        issues = heuristic.get("issues") or []
        out: list[dict[str, Any]] = []
        if isinstance(issues, dict):
            issues = issues.get("issues") or []
        for issue in issues[:20]:
            if not isinstance(issue, dict):
                continue
            out.append(
                {
                    "title": str(issue.get("title") or issue.get("category") or "未知问题"),
                    "severity": str(issue.get("severity") or "medium"),
                    "detail": str(issue.get("detail") or issue.get("description") or "")[:500],
                    "occurrences": int(issue.get("occurrences") or issue.get("count") or 1),
                }
            )
        return out

    def _heuristic_top_events(self, records: list[ObservationRecord]) -> list[str]:
        out: list[str] = []
        seen = set()
        for record in records:
            content = (record.content or "").strip()
            if not content or content in seen:
                continue
            if self._is_error_record(record) or self._is_warning_record(record):
                out.append(content[:200])
                seen.add(content)
            if len(out) >= 6:
                break
        if len(out) < 3:
            for record in records[:6]:
                content = (record.content or "").strip()
                if content and content not in seen:
                    out.append(content[:200])
                    seen.add(content)
                if len(out) >= 6:
                    break
        return out

    @staticmethod
    def _is_error_record(record: ObservationRecord) -> bool:
        level = str((record.context or {}).get("level") or "").lower()
        return "error" in (record.tags or []) or level in {"error", "fatal", "severe"}

    @staticmethod
    def _is_warning_record(record: ObservationRecord) -> bool:
        level = str((record.context or {}).get("level") or "").lower()
        return (
            "warning" in (record.tags or [])
            or "warn" in (record.tags or [])
            or level in {"warn", "warning"}
        )

    def _build_hourly_prompt(
        self,
        records: list[ObservationRecord],
        source: Any,
        hour_start_ms: int,
        hour_end_ms: int,
        heuristic: dict,
    ) -> str:
        start = datetime.fromtimestamp(hour_start_ms / 1000)
        end = datetime.fromtimestamp(hour_end_ms / 1000)
        sample = records[:200]
        lines = [
            f"服务器: {source.server_name} ({source.server_id})",
            f"类型: {source.server_type}",
            f"小时窗口: {start:%Y-%m-%d %H:00:00} ~ {end:%H:00:00}",
            f"日志条数: {len(records)}",
            "",
            "启发式初稿（参考，可修正）:",
            json.dumps(self._compact_heuristic(heuristic), ensure_ascii=False, indent=2)[:4000],
            "",
            "日志样本（最多 200 条，已截断）:",
        ]
        for record in sample:
            lines.append(f"- [{record.timestamp_ms}] {record.content[:200]}")
        return "\n".join(lines)[:20000]

    def _build_cycle_prompt(
        self,
        hourly_summaries: list[HourlySummary],
        server_id: str,
        heuristic: dict,
    ) -> str:
        lines = [
            f"服务器 ID: {server_id}",
            f"周期小时数: {len(hourly_summaries)}",
            "",
            "各小时总结（按时间顺序）:",
        ]
        for hs in hourly_summaries:
            lines.append(
                f"## {hs.hour_label} (records={hs.records_count}, "
                f"err={hs.error_count}, warn={hs.warning_count}, source={hs.source})"
            )
            lines.append(f"summary: {hs.summary}")
            if hs.key_issues:
                lines.append("issues:")
                for issue in hs.key_issues[:8]:
                    lines.append(
                        f"  - [{issue.get('severity')}] {issue.get('title')} "
                        f"(x{issue.get('occurrences')}): {issue.get('detail')}"
                    )
            if hs.top_events:
                lines.append("top_events:")
                for event in hs.top_events[:4]:
                    lines.append(f"  - {event}")
            lines.append("")
        lines.append("启发式周期初稿（参考）:")
        lines.append(json.dumps(heuristic, ensure_ascii=False)[:4000])
        return "\n".join(lines)[:30000]

    def _compact_heuristic(self, heuristic: dict) -> dict:
        return {
            "summary": heuristic.get("summary"),
            "categories": list((heuristic.get("categories") or {}).keys())
            if isinstance(heuristic.get("categories"), dict)
            else [],
            "issue_count": len(heuristic.get("issues") or []),
        }

    def _build_cycle_heuristic(
        self, hourly_summaries: list[HourlySummary], server_id: str
    ) -> dict[str, Any]:
        total = sum(hs.records_count for hs in hourly_summaries)
        errors = sum(hs.error_count for hs in hourly_summaries)
        warnings = sum(hs.warning_count for hs in hourly_summaries)
        all_issues: list[dict[str, Any]] = []
        for hs in hourly_summaries:
            for issue in hs.key_issues:
                issue_copy = dict(issue)
                issue_copy["hour"] = hs.hour_label
                all_issues.append(issue_copy)
        return {
            "server_id": server_id,
            "summary": (
                f"周期共 {len(hourly_summaries)} 小时，"
                f"累计 {total} 条日志，{errors} 条错误，{warnings} 条警告。"
            ),
            "total_records": total,
            "total_errors": errors,
            "total_warnings": warnings,
            "issues": all_issues[:50],
            "timeline": [
                {
                    "hour": hs.hour_label,
                    "summary": hs.summary,
                    "records": hs.records_count,
                    "errors": hs.error_count,
                    "warnings": hs.warning_count,
                }
                for hs in hourly_summaries
            ],
            "source": "heuristic",
        }

    def _normalize_cycle_report(
        self, parsed: dict[str, Any], fallback: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "server_id": fallback.get("server_id"),
            "summary": str(parsed.get("summary") or fallback.get("summary", ""))[:3000],
            "key_issues": list(parsed.get("key_issues") or fallback.get("issues") or [])[:50],
            "timeline": list(parsed.get("timeline") or fallback.get("timeline") or [])[:24],
            "recommendations": [
                str(r) for r in (parsed.get("recommendations") or [])
            ][:10],
            "total_records": fallback.get("total_records", 0),
            "total_errors": fallback.get("total_errors", 0),
            "total_warnings": fallback.get("total_warnings", 0),
            "source": "ai",
        }


def format_cycle_report(
    report: dict[str, Any],
    hourly_summaries: list[HourlySummary],
    server_name: str,
) -> str:
    """Render a cycle report dict into a human-readable text message."""
    lines = [
        f"📊 MineSentinel 周期报告 - {server_name}",
        f"周期：{len(hourly_summaries)} 小时",
        f"日志总数：{report.get('total_records', 0)}",
        f"错误：{report.get('total_errors', 0)}  警告：{report.get('total_warnings', 0)}",
        "",
        "总结：",
        str(report.get("summary") or ""),
        "",
    ]
    timeline = report.get("timeline") or []
    if timeline:
        lines.append("时间线：")
        for entry in timeline[:12]:
            hour = entry.get("hour") or ""
            summary = str(entry.get("summary") or "")[:120]
            lines.append(f"  • {hour}: {summary}")
        lines.append("")
    issues = report.get("key_issues") or []
    if issues:
        lines.append(f"关键问题（{len(issues)}）：")
        for issue in issues[:8]:
            title = issue.get("title") or issue.get("category") or "未知"
            severity = issue.get("severity") or "medium"
            occ = issue.get("occurrences") or 1
            hour = issue.get("hour") or ""
            lines.append(f"  • [{severity}] {title} (x{occ}) {hour}")
        lines.append("")
    recs = report.get("recommendations") or []
    if recs:
        lines.append("建议：")
        for rec in recs[:6]:
            lines.append(f"  • {rec}")
    return "\n".join(lines)
