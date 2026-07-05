"""MineSentinel service entry point."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .alerts import MineSentinelAlertEngine
from .delivery import MineSentinelDelivery
from .dispatch import MineSentinelReportDispatcher
from .formatter import format_report
from .hourly_summary import (
    HourlySummary,
    HourlySummaryStore,
    HourlySummarizer,
    format_cycle_report,
)
from .jobs import HourlySummaryJob, PeriodicReportJob
from .models import MineSentinelConfig, ObservationRecord
from .report_artifacts import MineSentinelReportArtifacts
from .reporting import MineSentinelReporter
from .reporting.image_renderer import MineSentinelReportImageRenderer
from .reporting.report_result import MineSentinelRenderedReport
from .routing import MineSentinelTargetRouter
from .runtime_log import MineSentinelRuntimeLogTailer, build_hour_observations
from .storage import DiskObservationStore, RecentObservationWindow
from .template_miner import get_template_miner
from .anomaly_detector import get_anomaly_detector
from .io_executor import build_io_executor, executor_runner, shutdown_io_executor


class MineSentinelService:
    def __init__(
        self,
        context: Any,
        config_data: dict | None,
        get_server_config: Callable[[str], Any | None],
        storage_dir: str | Path | None = None,
        io_runner: Callable[..., Awaitable[Any]] | None = None,
        report_thread_runner: Callable[..., Awaitable[Any]] | None = None,
    ):
        self.context = context
        self.config = MineSentinelConfig.from_dict(config_data)
        self.get_server_config = get_server_config
        # PR9: 专用 bounded ThreadPoolExecutor。当配置 io_workers > 0 且调用方
        # 未注入 io_runner 时，创建独立线程池隔离 MineSentinel 的 IO 任务。
        self._io_executor: ThreadPoolExecutor | None = None
        if io_runner is None:
            self._io_executor = build_io_executor(
                self.config.runtime_log.io_workers
            )
            self.io_runner = executor_runner(self._io_executor)
        else:
            self.io_runner = io_runner
        self.last_report_time = 0.0
        self.last_error = ""
        storage_path = Path(storage_dir) if storage_dir else Path(__file__).parent / ".cache"
        self.disk_store = (
            DiskObservationStore(self.config, storage_path)
            if storage_dir and self.config.storage.enabled
            else None
        )
        self.reporter = MineSentinelReporter(self.config, context)
        self.report_artifacts = MineSentinelReportArtifacts(
            self.config,
            self.reporter,
            self.disk_store,
            thread_runner=report_thread_runner or self.io_runner,
        )
        self.report_image_renderer = MineSentinelReportImageRenderer(
            storage_path / "render_cache"
        )
        self.delivery = MineSentinelDelivery(context)
        self.target_router = MineSentinelTargetRouter(
            get_server_config,
            lambda: self.config.report.delivery_targets,
        )
        self.dispatcher = MineSentinelReportDispatcher(
            self.delivery,
            self.target_router,
            self._set_last_error,
        )
        self.alerts = MineSentinelAlertEngine(self.config)
        # 初始化模板矿工和异常检测器单例（首次调用传入 config 参数），
        # 后续 runtime_log / ai_prompt 无参获取已创建的实例。
        rt_cfg = self.config.runtime_log
        get_template_miner(max_namespaces=rt_cfg.template_max_namespaces)
        get_anomaly_detector(
            max_templates_per_server=rt_cfg.anomaly_max_templates_per_server,
            inactive_template_ttl_hours=rt_cfg.anomaly_inactive_template_ttl_hours,
            cleanup_interval=rt_cfg.anomaly_cleanup_interval,
        )
        self.runtime_log_tailer = MineSentinelRuntimeLogTailer(
            self.config.runtime_log,
            self.handle_batch,
            io_runner=self.io_runner,
        )
        self._periodic_report_job = PeriodicReportJob(
            self.config,
            self._run_periodic_report_once,
            lambda: self.last_report_time,
        )
        self.hourly_summarizer = HourlySummarizer(self.config, context)
        self.hourly_store = HourlySummaryStore(storage_path) if storage_dir else None
        # Per-server in-memory cycle buffer of recent HourlySummary objects.
        self._hourly_cycle_buffers: dict[str, list[HourlySummary]] = {}
        self._hourly_cycle_starts: dict[str, int] = {}
        self._hourly_job = HourlySummaryJob(
            self.config,
            self._run_hourly_for_source,
        )

    def start(self):
        if not self.config.enabled:
            logger.info("[MineSentinel] 已禁用")
            return
        hourly_enabled = self.config.hourly_summary.enabled
        poll_enabled = (
            self.config.runtime_log.enabled
            and (not hourly_enabled or self.config.hourly_summary.poll_enabled)
        )
        if self.config.report.enabled and not hourly_enabled:
            self._periodic_report_job.start()
            logger.info(
                "[MineSentinel] 定时 AI 报告已启用，"
                f"间隔 {self.config.report.interval_minutes} 分钟"
            )
        if hourly_enabled:
            self._hourly_job.start()
            cycle = self.config.hourly_summary.hours_per_cycle
            logger.info(
                "[MineSentinel] hourly 模式已启用：每整点读取上一小时日志并总结，"
                f"每 {cycle} 小时整合一次发送；启动时立即补读当前小时已过部分。"
            )
            sources = self._hourly_job._enabled_sources()
            if not sources:
                logger.warning(
                    "[MineSentinel] hourly 模式已启用但未配置任何日志源，"
                    "请在 mine_sentinel.runtime_log.sources 中至少添加一个服务器。"
                )
            else:
                logger.info(
                    "[MineSentinel] hourly 日志源："
                    + ", ".join(
                        f"{s.server_id}({s.server_type})" for s in sources
                    )
                )
            if self.config.hourly_summary.poll_enabled:
                logger.info(
                    "[MineSentinel] 同时启用实时轮询（poll_enabled=true）；"
                    "如不需要实时告警可关闭以进一步降低 IO。"
                )
        if poll_enabled:
            self.runtime_log_tailer.start()
        elif self.config.runtime_log.enabled and hourly_enabled:
            logger.info(
                "[MineSentinel] 已禁用实时轮询，仅按小时读取日志；"
                "MC 服务端 mspt/tps 不会受影响。"
            )
        logger.info("[MineSentinel] 服务已启动")

    async def stop(self):
        await self.runtime_log_tailer.stop()
        await self._periodic_report_job.stop()
        await self._hourly_job.stop()
        # PR9: 关闭专用 bounded ThreadPoolExecutor（如有）。
        shutdown_io_executor(self._io_executor)
        self._io_executor = None

    async def handle_batch(self, server_id: str, payload: dict):
        if not self.config.enabled:
            return
        try:
            written = 0
            if self.disk_store:
                try:
                    written = await self.io_runner(
                        self.disk_store.add_batch,
                        server_id,
                        payload or {},
                    )
                except Exception as exc:
                    self.last_error = f"写入硬盘 observation 失败: {exc}"
                    logger.error(f"[MineSentinel] {self.last_error}")
            else:
                self.last_error = "硬盘 observation 存储未启用，batch 已忽略"
                logger.error(f"[MineSentinel] {self.last_error}")
                return
            logger.debug(
                f"[MineSentinel] batch {server_id}: written_to_disk={written}"
            )
            if self.config.alert.enabled and written:
                await self._maybe_alert(server_id)
        except Exception as exc:
            self.last_error = str(exc)
            logger.error(f"[MineSentinel] 处理 observation batch 失败: {exc}")

    def monitor_status(self) -> str:
        lines = [
            "MineSentinel 监控状态",
            f"启用状态：{'启用' if self.config.enabled else '禁用'}",
            "存储模式：硬盘 JSONL（无 observation 内存缓存）",
            f"硬盘存储：{'启用' if self.disk_store else '禁用'}",
            (
                "MC 运行日志读取："
                f"{'启用' if self.config.runtime_log.enabled else '禁用'}"
                f"（{len(self.runtime_log_tailer.enabled_sources)} 个来源）"
            ),
        ]
        if self.config.hourly_summary.enabled:
            cycle = self.config.hourly_summary.hours_per_cycle
            lines.append(
                "hourly 模式：启用"
                f"（每 {cycle} 小时整合一次，poll_enabled="
                f"{self.config.hourly_summary.poll_enabled}）"
            )
            for sid, buf in self._hourly_cycle_buffers.items():
                lines.append(
                    f"  - {sid}: 周期进度 {len(buf)}/{cycle}"
                )
        else:
            lines.append("hourly 模式：禁用")
        if self.disk_store:
            lines.extend(
                [
                    f"observation 目录：{self.disk_store.observation_dir}",
                    f"export 目录：{self.disk_store.export_dir}",
                    "报告内存上限："
                    f"{self.config.report.max_records_in_memory} 条 observation",
                ]
            )
        lines.extend(
            [
                f"last_report_time：{self._format_ts(self.last_report_time) if self.last_report_time else '无'}",
                f"last_error：{self.last_error or '无'}",
            ]
        )
        return "\n".join(lines)

    async def report_now(
        self,
        current_session: str,
        server_id: str | None = None,
        window_minutes: int | None = None,
    ) -> str:
        result = await self.report_now_result(
            current_session,
            server_id,
            window_minutes,
            render_image=False,
        )
        return result.text

    async def report_now_result(
        self,
        current_session: str,
        server_id: str | None = None,
        window_minutes: int | None = None,
        render_image: bool | None = None,
    ) -> MineSentinelRenderedReport:
        if not self.config.enabled:
            return MineSentinelRenderedReport("MineSentinel 未启用")
        window = self._report_window_minutes(window_minutes)
        window_data = await self._recent_window(window, server_id)
        records = window_data.records
        if not records:
            return MineSentinelRenderedReport("最近窗口内没有足够数据")

        report = await self._build_report(
            records,
            window,
            server_id,
            current_session,
            window_data,
        )
        self.last_report_time = time.time()
        text = format_report(report, window_data.total_count, 0, window_data.unique_players)
        report_file = self._report_file_path(report)
        image = await self._render_report_image(
            report,
            window_data.total_count,
            0,
            window_data.unique_players,
            render_image,
        )
        if report_file and current_session:
            await self.dispatcher.send_file(current_session, report_file)

        if self._has_report_delivery_targets():
            await self.dispatcher.send_to_target_sessions(
                text,
                records,
                current_session,
                include_server_targets=self.config.report.send_to_target_sessions,
                image=image,
            )
        return MineSentinelRenderedReport(text, image=image, report_file=report_file)

    async def _run_periodic_report_once(self) -> bool:
        window = self._report_window_minutes()
        window_data = await self._recent_window(window)
        records = window_data.records
        if not records:
            return False
        if not self._has_report_delivery_targets():
            return False

        sent_any = False
        for umo, scoped_records in sorted(
            self.dispatcher.records_by_session(
                records,
                include_server_targets=self.config.report.send_to_target_sessions,
            ).items()
        ):
            report = await self._build_report(
                scoped_records,
                window,
                None,
                umo,
                window_data,
            )
            unique_players = len(
                {record.identity for record in scoped_records if record.identity}
            )
            text = format_report(
                report,
                len(scoped_records),
                0,
                unique_players,
            )
            image = await self._render_report_image(
                report,
                len(scoped_records),
                0,
                unique_players,
                None,
            )
            sent_any = await self.dispatcher.send_report(
                umo,
                text,
                image=image,
                file_path=self._report_file_path(report),
            ) or sent_any
        if sent_any:
            self.last_report_time = time.time()
        return sent_any

    async def _maybe_alert(self, server_id: str):
        if not self.alerts.should_analyze(server_id):
            return

        window = self.config.alert.window_minutes
        records = await self._recent_records(window, server_id)
        if not records:
            return
        report = self.reporter.build_heuristic_report(
            records,
            window,
            server_id,
        )
        for text in self.alerts.build_messages(server_id, report):
            await self.dispatcher.send_to_target_sessions(
                text,
                records,
                include_server_targets=self.config.report.send_to_target_sessions,
            )

    async def _run_hourly_for_source(
        self, hour_start_ms: int, hour_end_ms: int, server_id: str
    ):
        """Process one hour for one source: read logs, summarize, maybe deliver cycle report."""
        source = self._find_source(server_id)
        if source is None:
            logger.warning(
                f"[MineSentinel] hourly 找不到 server_id={server_id} 的日志源"
            )
            return

        # Read the hour's logs in a worker thread to avoid blocking the event loop.
        max_lines = self.config.hourly_summary.max_log_lines_per_hour
        max_records = self.config.hourly_summary.max_records_per_hour
        max_line_length = self.config.runtime_log.max_line_length
        observations = await self.io_runner(
            build_hour_observations,
            source,
            hour_start_ms,
            hour_end_ms,
            max_lines,
            max_records,
            max_line_length,
            self.config.runtime_log,
        )
        records = [ObservationRecord.from_dict(o) for o in observations]
        if not records:
            logger.info(
                f"[MineSentinel] hourly {server_id} "
                f"({self._format_ts(hour_start_ms/1000)}~{self._format_ts(hour_end_ms/1000)}) "
                f"无日志记录，跳过总结"
            )
            return

        # Build the hourly summary via AI (or heuristic fallback).
        hourly = await self.hourly_summarizer.build_hourly_summary(
            records,
            source,
            hour_start_ms,
            hour_end_ms,
            umo=None,
        )
        if self.hourly_store:
            try:
                await self.io_runner(self.hourly_store.save, hourly)
            except Exception as exc:
                logger.warning(f"[MineSentinel] hourly 保存磁盘失败: {exc}")
        # Keep in-memory cycle buffer for the integration step.
        cycle_start = self._ensure_cycle_start(server_id, hour_start_ms)
        buf = self._hourly_cycle_buffers.setdefault(server_id, [])
        # Drop any summaries that fall outside the current cycle window.
        buf[:] = [h for h in buf if h.hour_start_ms >= cycle_start]
        buf.append(hourly)
        logger.info(
            f"[MineSentinel] hourly {server_id} 完成 "
            f"({hourly.hour_label}): records={hourly.records_count} "
            f"err={hourly.error_count} warn={hourly.warning_count} "
            f"source={hourly.source} 周期进度={len(buf)}/{self.config.hourly_summary.hours_per_cycle}"
        )

        # Check whether the cycle is complete.
        hours_per_cycle = self.config.hourly_summary.hours_per_cycle
        if len(buf) >= hours_per_cycle:
            await self._finalize_hourly_cycle(server_id, buf, cycle_start)
            # Reset the cycle for the next window.
            self._hourly_cycle_buffers[server_id] = []
            self._hourly_cycle_starts[server_id] = 0
            # Cleanup old persisted summaries beyond retention.
            if self.hourly_store:
                try:
                    await self.io_runner(
                        self.hourly_store.cleanup_old_summaries,
                        server_id,
                        self.config.hourly_summary.retention_cycles,
                        hours_per_cycle,
                    )
                except Exception as exc:
                    logger.debug(f"[MineSentinel] hourly 清理旧文件失败: {exc}")

    async def _finalize_hourly_cycle(
        self,
        server_id: str,
        summaries: list[HourlySummary],
        cycle_start_ms: int,
    ):
        if not summaries:
            return
        # Prefer reloading from disk to survive restarts.
        cycle_end_ms = summaries[-1].hour_end_ms
        if self.hourly_store:
            try:
                persisted = await self.io_runner(
                    self.hourly_store.list_cycle_summaries,
                    server_id,
                    cycle_start_ms,
                    cycle_end_ms,
                )
                if len(persisted) >= len(summaries):
                    summaries = persisted
            except Exception as exc:
                logger.warning(
                    f"[MineSentinel] hourly 从磁盘重载周期总结失败: {exc}"
                )

        source = self._find_source(server_id)
        server_name = source.server_name if source else server_id
        report = await self.hourly_summarizer.build_cycle_report(
            summaries, server_id, umo=None
        )
        text = format_cycle_report(report, summaries, server_name)
        # Try to load persisted summaries again to ensure we have the latest.
        if not self._has_report_delivery_targets():
            logger.warning(
                f"[MineSentinel] hourly 周期 {server_id} 完成但未配置投递目标，"
                "报告仅记录到日志，请配置 mine_sentinel.report.delivery_targets"
            )
            logger.info(f"[MineSentinel] hourly 周期报告预览:\n{text}")
            self.last_report_time = time.time()
            return
        # Build an empty records list for dispatcher compatibility (the full
        # content already lives in the cycle report text).
        sent = await self.dispatcher.send_to_target_sessions(
            text,
            [],
            include_server_targets=self.config.report.send_to_target_sessions,
        )
        if sent:
            self.last_report_time = time.time()
            logger.info(
                f"[MineSentinel] hourly 周期报告已发送 server={server_id} "
                f"hours={len(summaries)}"
            )
        else:
            logger.warning(
                f"[MineSentinel] hourly 周期报告发送失败 server={server_id}"
            )

    def _ensure_cycle_start(self, server_id: str, hour_start_ms: int) -> int:
        """Return the cycle start timestamp, initializing it if needed."""
        current = self._hourly_cycle_starts.get(server_id, 0)
        if current == 0:
            # Start a fresh cycle: anchor at the first hour we see.
            current = hour_start_ms
            self._hourly_cycle_starts[server_id] = current
        return current

    def _find_source(self, server_id: str):
        for source in self.config.runtime_log.sources:
            if source.server_id == server_id:
                return source
        return None

    def _report_window_minutes(self, window_minutes: int | None = None) -> int:
        return max(1, window_minutes or self.config.report.default_window_minutes)

    async def _render_report_image(
        self,
        report: dict,
        total_count: int,
        dedupe_count: int,
        unique_players: int,
        render_image: bool | None,
    ):
        should_render = self.config.report.send_as_image if render_image is None else render_image
        if not should_render:
            return None
        try:
            return await self.report_image_renderer.render(
                report,
                total_count,
                dedupe_count,
                unique_players,
            )
        except Exception as exc:
            logger.warning(f"[MineSentinel] 渲染图片报告失败，回退文本: {exc}")
            return None

    def _has_report_delivery_targets(self) -> bool:
        return bool(
            self.config.report.send_to_target_sessions
            or self.config.report.delivery_targets
        )

    async def _build_report(
        self,
        records: list[ObservationRecord],
        window: int,
        server_id: str | None,
        umo: str | None,
        window_data: RecentObservationWindow | None = None,
    ) -> dict:
        report = await self.report_artifacts.build(
            records,
            window,
            server_id,
            umo,
            window_data,
        )
        if self.report_artifacts.last_error:
            self.last_error = self.report_artifacts.last_error
        return report

    async def _recent_window(
        self,
        window_minutes: int,
        server_id: str | None = None,
    ) -> RecentObservationWindow:
        if self.disk_store:
            try:
                return await self.io_runner(
                    self.disk_store.recent_window,
                    window_minutes,
                    server_id,
                )
            except Exception as exc:
                self.last_error = f"读取硬盘 observation 失败: {exc}"
                logger.error(f"[MineSentinel] {self.last_error}")
        return RecentObservationWindow([], 0, 0, False, 0)

    async def _recent_records(
        self,
        window_minutes: int,
        server_id: str | None = None,
    ) -> list[ObservationRecord]:
        return (await self._recent_window(window_minutes, server_id)).records

    async def _export_report_records(
        self,
        records: list[ObservationRecord],
        window_minutes: int,
        server_id: str | None,
        umo: str | None,
        export_full_window: bool = False,
    ):
        path = await self.report_artifacts.export_report_records(
            records,
            window_minutes,
            server_id,
            umo,
            export_full_window,
        )
        if self.report_artifacts.last_error:
            self.last_error = self.report_artifacts.last_error
        return path

    def _report_file_path(self, report: dict) -> Path | None:
        return self.report_artifacts.report_file_path(report)

    def _set_last_error(self, message: str):
        self.last_error = message

    @staticmethod
    def _format_ts(value: float) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))
