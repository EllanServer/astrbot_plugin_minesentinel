"""MineSentinel runtime-log audit plugin for AstrBot."""

from __future__ import annotations

import asyncio
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


class MinecraftAdapterPlugin(Star):
    """Audit-only MineSentinel plugin.

    The old Minecraft WebSocket adapter, chat bridge, command bridge and player
    binding runtime are intentionally not started here. MineSentinel now reads
    Minecraft runtime logs directly from configured paths.
    """

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        plugin_data_path = (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / "astrbot_plugin_minecraft_adapter"
        )
        plugin_data_path.mkdir(parents=True, exist_ok=True)
        self._plugin_data_path = plugin_data_path

        self.mine_sentinel_service = MineSentinelService(
            context=context,
            config_data=self.config.get("mine_sentinel", {}),
            get_server_config=self._server_report_config,
            storage_dir=plugin_data_path / "mine_sentinel",
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
        if not self.config.get("enabled", True):
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

    @filter.command_group("mc")
    def mc_group(self):
        """MineSentinel audit commands."""
        pass

    @mc_group.command("help")
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "MineSentinel 审计命令:\n"
            "/mc monitor status - 查看运行日志审计状态\n"
            "/mc report now [服务器ID] [30m|8h] - 立即生成运行日志审计报告"
        )

    @mc_group.command("monitor")
    async def cmd_monitor(self, event: AstrMessageEvent, args=GreedyStr):
        async for result in self.mine_sentinel_commands.handle_monitor(
            event,
            str(args),
        ):
            yield result

    @mc_group.command("report")
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
