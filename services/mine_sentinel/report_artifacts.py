"""Report assembly and JSONL export orchestration."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .models import MineSentinelConfig, ObservationRecord
from .reporting import MineSentinelReporter
from .storage import DiskObservationStore, RecentObservationWindow


class MineSentinelReportArtifacts:
    """Build reports and attach their complete observation export when enabled."""

    def __init__(
        self,
        config: MineSentinelConfig,
        reporter: MineSentinelReporter,
        disk_store: DiskObservationStore | None,
        thread_runner: Callable[..., Awaitable[Any]] | None = None,
    ):
        self.config = config
        self.reporter = reporter
        self.disk_store = disk_store
        self.thread_runner = thread_runner or asyncio.to_thread
        self.last_error = ""

    async def build(
        self,
        records: list[ObservationRecord],
        window_minutes: int,
        server_id: str | None,
        umo: str | None,
        window_data: RecentObservationWindow | None = None,
    ) -> dict:
        report = await self.reporter.build_report(
            records,
            window_minutes,
            server_id,
            umo,
        )
        self._append_window_metadata(report, window_minutes)
        self._append_bounded_window_note(report, window_data)
        export_records = self.reporter.rules.filter_records_for_report(records)
        export_path = await self.export_report_records(
            export_records,
            window_minutes,
            server_id,
            umo,
            export_full_window=bool(window_data and window_data.truncated),
        )
        if export_path:
            report["_export_file_path"] = str(export_path)
            report["_export_file_name"] = export_path.name
            report.setdefault("ops_notes", [])
            report["ops_notes"].append(f"完整审计日志附件：{export_path.name}")
        return report

    async def export_report_records(
        self,
        records: list[ObservationRecord],
        window_minutes: int,
        server_id: str | None,
        umo: str | None,
        export_full_window: bool = False,
    ) -> Path | None:
        if not self.disk_store:
            return None
        if not self.config.report.send_full_log_file:
            return None
        try:
            label = umo or "manual"
            if export_full_window:
                return await self.thread_runner(
                    self.disk_store.export_recent,
                    window_minutes,
                    server_id,
                    label,
                    self.reporter.rules.record_allowed_for_report,
                )
            return await self.thread_runner(
                self.disk_store.export_records,
                records,
                window_minutes,
                server_id,
                label,
            )
        except Exception as exc:
            self.last_error = f"导出完整 observation 文件失败: {exc}"
            logger.error(f"[MineSentinel] {self.last_error}")
            return None

    @staticmethod
    def report_file_path(report: dict) -> Path | None:
        value = report.get("_export_file_path")
        if not value:
            return None
        return Path(str(value))

    @staticmethod
    def _append_window_metadata(report: dict, window_minutes: int):
        end_ms = int(time.time() * 1000)
        start_ms = max(0, end_ms - max(1, window_minutes) * 60 * 1000)
        report.setdefault("_window_minutes", window_minutes)
        report.setdefault("window_start_ts", start_ms)
        report.setdefault("window_end_ts", end_ms)

    @staticmethod
    def _append_bounded_window_note(
        report: dict,
        window_data: RecentObservationWindow | None,
    ):
        if not window_data or not window_data.truncated:
            return
        report.setdefault("ops_notes", [])
        report["ops_notes"].append(
            "窗口 observation 数量较大，"
            f"完整窗口 {window_data.total_count} 条，"
            f"本次内存分析使用 {window_data.retained_count} 条有界样本；"
            "完整记录仍以 JSONL 落盘。"
        )
