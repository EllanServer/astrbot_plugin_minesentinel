"""MineSentinel runtime-log audit plugin for AstrBot."""

from __future__ import annotations

import asyncio
import shutil
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .handlers.mine_sentinel_commands import MineSentinelCommandHandler
from .services.mine_sentinel import MineSentinelService


_MINE_SENTINEL_DATA_DIR = "mine_sentinel"
_LEGACY_PLUGIN_DATA_DIR = "astrbot_plugin_minecraft_adapter"


def _effective_mine_sentinel_config(config: dict | None) -> dict:
    """Combine the plugin and service enable switches into one source of truth."""

    root = config or {}
    nested = dict(root.get("mine_sentinel") or {})
    nested["enabled"] = bool(
        root.get("enabled", True) and nested.get("enabled", True)
    )
    return nested


def _resolve_mine_sentinel_data_path(data_root: str | Path) -> Path:
    """Return the MineSentinel data directory, migrating legacy storage once."""

    plugin_data_root = Path(data_root) / "plugin_data"
    data_path = plugin_data_root / _MINE_SENTINEL_DATA_DIR
    legacy_path = (
        plugin_data_root
        / _LEGACY_PLUGIN_DATA_DIR
        / _MINE_SENTINEL_DATA_DIR
    )
    if not data_path.exists() and legacy_path.exists():
        try:
            data_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy_path), str(data_path))
            logger.info(
                f"[MineSentinel] 已迁移历史数据目录: {legacy_path} -> {data_path}"
            )
        except Exception as exc:
            logger.warning(
                "[MineSentinel] 历史数据目录迁移失败，继续使用旧目录 "
                f"{legacy_path}: {exc}"
            )
            data_path = legacy_path
    data_path.mkdir(parents=True, exist_ok=True)
    return data_path


class MineSentinelPlugin(Star):
    """Audit-only MineSentinel plugin.

    MineSentinel reads Minecraft runtime logs directly from configured paths and
    generates AI-assisted monitoring reports.
    """

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        plugin_data_path = _resolve_mine_sentinel_data_path(get_astrbot_data_path())
        self._plugin_data_path = plugin_data_path

        self.mine_sentinel_service = MineSentinelService(
            context=context,
            config_data=_effective_mine_sentinel_config(self.config),
            get_server_config=self._server_report_config,
            storage_dir=plugin_data_path,
        )
        self.mine_sentinel_commands = MineSentinelCommandHandler(
            self.mine_sentinel_service
        )
        self._init_task: asyncio.Task | None = self._schedule_task(
            self._initialize(),
            "mine_sentinel_initialize",
        )

    def _schedule_task(self, coro, task_name: str) -> asyncio.Task | None:
        try:
            task = asyncio.create_task(coro)
        except RuntimeError as exc:
            coro.close()
            logger.error(f"[MineSentinel] 无法启动后台任务 {task_name}: {exc}")
            return None
        task.add_done_callback(lambda done: self._on_task_done(task_name, done))
        return task

    @staticmethod
    def _on_task_done(task_name: str, task: asyncio.Task):
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc:
            logger.error(f"[MineSentinel] 后台任务 {task_name} 异常退出: {exc}")

    async def _initialize(self):
        if not self.mine_sentinel_service.config.enabled:
            logger.info("[MineSentinel] 插件已禁用")
            return
        self.mine_sentinel_service.start()
        logger.info("[MineSentinel] 运行日志审计插件已启动")

    def _server_report_config(self, server_id: str):
        """Return delivery targets scoped to a runtime-log source, if present."""
        mine_sentinel = self.config.get("mine_sentinel", {}) or {}
        runtime_log = mine_sentinel.get("runtime_log", {}) or {}
        for source in runtime_log.get("sources") or []:
            if not isinstance(source, dict):
                continue
            if str(source.get("server_id") or "") != server_id:
                continue
            targets = source.get("target_sessions") or source.get("delivery_targets") or []
            return SimpleNamespace(target_sessions=list(targets))
        return None

    @filter.command_group("ms")
    def ms_group(self):
        """MineSentinel audit commands."""
        pass

    @ms_group.command("help")
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "MineSentinel 审计命令:\n"
            "/ms monitor status - 查看运行日志审计状态\n"
            "/ms report now [服务器ID] [30m|8h] - 立即生成运行日志审计报告"
        )

    @ms_group.command("monitor")
    async def cmd_monitor(self, event: AstrMessageEvent, args=GreedyStr):
        async for result in self.mine_sentinel_commands.handle_monitor(
            event,
            str(args),
        ):
            yield result

    @ms_group.command("report")
    async def cmd_report(self, event: AstrMessageEvent, args=GreedyStr):
        async for result in self.mine_sentinel_commands.handle_report(
            event,
            str(args),
        ):
            yield result

    async def terminate(self):
        logger.info("[MineSentinel] 正在关闭运行日志审计插件...")
        if self._init_task and not self._init_task.done():
            self._init_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._init_task
        self._init_task = None
        await self.mine_sentinel_service.stop()
        logger.info("[MineSentinel] 运行日志审计插件已关闭")
