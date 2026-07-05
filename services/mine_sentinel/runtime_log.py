"""Local Minecraft runtime log ingestion for MineSentinel."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .models import (
    DEFAULT_DAILY_NOISE_PATTERNS,
    MineSentinelLogSourceConfig,
    MineSentinelRuntimeLogConfig,
)
from .template_miner import ParsedTemplate, get_template_miner
from .anomaly_detector import AnomalyResult, TemplateStat, get_anomaly_detector


BatchHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]
IoRunner = Callable[..., Awaitable[Any]]

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_FULL_TS_RE = re.compile(
    r"^\[?(?P<date>\d{4}-\d{2}-\d{2})[ T]"
    r"(?P<time>\d{2}:\d{2}:\d{2})(?:[.,](?P<ms>\d{1,6}))?"
)
_TIME_RE = re.compile(
    r"^\[?(?P<time>\d{2}:\d{2}:\d{2})(?:[.,](?P<ms>\d{1,6}))?\]?"
)
_ARCHIVE_DATE_RE = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})")
_LEVEL_RE = re.compile(
    r"(?:^|[\[/\s:])(?P<level>FATAL|SEVERE|ERROR|WARN|WARNING|INFO|DEBUG|TRACE)"
    r"(?:[\]/\s:]|$)",
    re.IGNORECASE,
)
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_HEX_RE = re.compile(r"\b0x[0-9a-f]+\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"(?<![A-Za-z_])-?\d+(?:\.\d+)?")
_SPACE_RE = re.compile(r"\s+")
_PREFIX_RE = re.compile(
    r"^\[?\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?\]?\s*"
    r"(?:\[[^\]]+\]\s*)?(?:\[[A-Z]+\]\s*)?",
    re.IGNORECASE,
)
_ERROR_WORDS = (
    "error",
    "exception",
    "failed",
    "failure",
    "fatal",
    "severe",
    "stacktrace",
    "timeout",
    "timed out",
    "unable",
    "cannot",
    "can't",
    "could not",
    "warn",
    "warning",
    "crash",
    "报错",
    "异常",
    "失败",
    "超时",
    "警告",
)

# 聊天行检测：Minecraft 原生 [Async Chat Thread] 线程标签或 <player> 前缀。
# 用于 chat_summary 热点总结，提取玩家名和消息内容。
_CHAT_THREAD_RE = re.compile(r"\[Async Chat Thread[^\]]*\]\s*:?\s*", re.IGNORECASE)
# 原生聊天：<player> message
_CHAT_PLAYER_PREFIX_RE = re.compile(r"^\s*<(?P<player>[^>\s]{1,40})>\s*(?P<message>.*)$")
# 聊天插件（CarbonChat 等）：[Not Secure] [频道] player >> message
# 兼容 [Not Secure] 可选、频道名可含中文/数字、>> 分隔符。
_CHAT_PLUGIN_RE = re.compile(
    r"(?:\[Not Secure\]\s*)?"  # 可选的 [Not Secure] 标记
    r"(?:\[[^\]]{1,30}\]\s*)*"  # 0 或多个 [频道] 标记
    r"(?P<player>[A-Za-z0-9_]{1,16})\s*>>\s*(?P<message>.+)$"
)
# Vulcan 反作弊插件告警检测。Vulcan 告警的标准格式：
#   [Vulcan] PlayerName failed CheckName (VL: 5)
#   [Vulcan] PlayerName failed CheckName (Type X) (N/M)   ← 真实 mclo.gs 格式
# 注意：[Vulcan] 前缀也会出现在插件生命周期日志（Loading/Enabling/Starting/hook），
# 因此必须用 "failed" 关键词区分告警和生命周期日志，避免误判。
# check 名捕获完整子类型：如 "Invalid (Type E)" 而非只 "Invalid"。
_VULCAN_PLAYER_RE = re.compile(
    r"\[Vulcan\][\]:>\s]*"  # [Vulcan] 前缀
    r"(?P<player>[A-Za-z0-9_]{1,16})\s+"
    r"failed\s+"
    r"(?P<check>[A-Za-z]+(?:\s*\([^)]+\))?)",  # CheckName 或 CheckName (Type X)
    re.IGNORECASE,
)

# PR9: interesting-only 模式下，普通 INFO 命中这些关键词才进 template/anomaly。
# 覆盖 Minecraft 服常见的事故线索：性能、连接、世界/区块、内存/GC、插件异常。
_INTERESTING_INFO_KEYWORDS = (
    "tps",
    "mspt",
    "lag",
    "overloaded",
    "can't keep up",
    "cannot keep up",
    "running behind",
    "timeout",
    "timed out",
    "disconnect",
    "disconnected",
    "lost connection",
    "moved too quickly",
    "moved wrongly",
    "kicked",
    "banned",
    "exception",
    "stacktrace",
    "failed",
    "failure",
    "plugin",
    "chunk",
    "world",
    "gc",
    "garbage collect",
    "memory",
    "outofmemory",
    "out of memory",
    "deadlock",
    "watchdog",
    "stuck",
    "thread",
    "save",
    "saving",
    "saved",
    "crash",
    "restart",
    "stopped",
    "started",
    "reloaded",
    "reload",
    "error",
    "warn",
    "severe",
    "fatal",
    "严重",
    "卡顿",
    "延迟",
    "掉线",
    "断开",
    "异常",
    "报错",
    "失败",
    "超时",
    "内存",
    "插件",
    "崩服",
    "卡死",
)


@dataclass
class _SourceState:
    source: MineSentinelLogSourceConfig
    log_file: Path
    position: int = 0
    partial: str = ""  # 兼容字段：仅在无 backlog 时表示未闭合行；新代码用 partial_line
    # PR9 hotfix v5: partial_line 改为 bytes-level，保留未闭合的尾部
    # bytes（可能切断在 UTF-8 多字节字符中间），下一轮读取时与新 bytes
    # 拼接后用增量 UTF-8 解码器解码。避免 errors="replace" 把半个
    # 中文字符替换成 U+FFFD，污染日志证据。
    partial_line: bytes = b""  # 未闭合的尾部 bytes（未解码）
    backlog: deque[str] = field(default_factory=deque)  # 完整行积压队列
    last_timestamp_ms: int = 0
    missing_logged: bool = False

    @property
    def has_pending(self) -> bool:
        """是否有待处理数据（backlog 或未闭合行）。"""
        return bool(self.backlog) or bool(self.partial_line) or bool(self.partial)


@dataclass
class _LoopEntry:
    fingerprint: str
    first_ts: int
    last_ts: int
    last_emit_ts: int
    count: int
    suppressed: int
    level: str
    sample: str
    server_id: str
    server_name: str
    context: dict[str, Any]


class RuntimeLogLoopFilter:
    """Suppress repeated error loops before they enter JSONL storage."""

    def __init__(self, config: MineSentinelRuntimeLogConfig):
        self.config = config
        self._entries: dict[str, _LoopEntry] = {}

    def process(self, observation: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.config.loop_filter_enabled:
            return [observation]
        if not self._is_loop_candidate(observation):
            return [observation]

        context = dict(observation.get("context") or {})
        # 优先用 drain3 模板 ID 去重（同一模板的日志归为一类），
        # 不可用时回退到 fingerprint（基于正则替换的哈希）。
        dedupe_key = str(context.get("templateId") or "")
        fingerprint = str(context.get("fingerprint") or "")
        if not dedupe_key:
            dedupe_key = fingerprint
        if not dedupe_key:
            return [observation]

        now_ms = _as_millis(observation.get("timestamp")) or int(time.time() * 1000)
        key = f"{observation.get('serverId') or ''}:{dedupe_key}"
        entry = self._entries.get(key)
        window_ms = max(1, self.config.loop_filter_window_seconds) * 1000
        if entry is None or now_ms - entry.last_ts > window_ms:
            self._entries[key] = _LoopEntry(
                fingerprint=fingerprint or dedupe_key,
                first_ts=now_ms,
                last_ts=now_ms,
                last_emit_ts=now_ms,
                count=1,
                suppressed=0,
                level=str(context.get("level") or "WARN"),
                sample=str(observation.get("content") or ""),
                server_id=str(observation.get("serverId") or ""),
                server_name=str(observation.get("serverName") or ""),
                context=context,
            )
            return [observation]

        entry.count += 1
        entry.suppressed += 1
        entry.last_ts = now_ms
        # 更新模板大小（drain3 模式下会随样本增长）
        new_size = int(context.get("templateSize") or 0)
        if new_size > 0:
            entry.context["templateSize"] = new_size
        summary_ms = max(1, self.config.loop_summary_interval_seconds) * 1000
        if now_ms - entry.last_emit_ts >= summary_ms:
            summary = self._summary(entry)
            entry.suppressed = 0
            entry.last_emit_ts = now_ms
            return [summary]
        return []

    def drain_due(self, force: bool = False) -> list[dict[str, Any]]:
        if not self.config.loop_filter_enabled:
            return []
        now_ms = int(time.time() * 1000)
        summary_ms = max(1, self.config.loop_summary_interval_seconds) * 1000
        expired_ms = max(1, self.config.loop_filter_window_seconds) * 1000
        summaries: list[dict[str, Any]] = []
        expired: list[str] = []
        for key, entry in list(self._entries.items()):
            if entry.suppressed and (force or now_ms - entry.last_emit_ts >= summary_ms):
                summaries.append(self._summary(entry))
                entry.suppressed = 0
                entry.last_emit_ts = now_ms
            if now_ms - entry.last_ts > expired_ms:
                expired.append(key)
        for key in expired:
            self._entries.pop(key, None)
        return summaries

    def _summary(self, entry: _LoopEntry) -> dict[str, Any]:
        suppressed = max(1, entry.suppressed)
        digest = hashlib.sha1(
            f"{entry.server_id}:{entry.fingerprint}:{entry.last_emit_ts}".encode("utf-8")
        ).hexdigest()[:16]
        observed_ms = int(time.time() * 1000)
        context = dict(entry.context)
        context.update(
            {
                "loopSuppressed": suppressed,
                "loopFirstTimestamp": entry.first_ts,
                "loopLastTimestamp": entry.last_ts,
            }
        )
        # 刷新 OTel 字段：summary 是新事件，timestamp 用最后一条的时间，
        # observedTimestamp 用当前时间，body/eventName 保持原模板信息。
        otel = dict(context.get("otel") or {})
        otel["timestamp"] = entry.last_ts
        otel["observedTimestamp"] = observed_ms
        otel["body"] = (
            f"同类服务器报错已合并：{suppressed} 条重复日志被过滤；"
            f"首条样本：{_truncate(entry.sample, 320)}"
        )
        if "attributes" in otel:
            attrs = dict(otel["attributes"])
            attrs["loop.suppressed"] = suppressed
            attrs["loop.first_timestamp"] = entry.first_ts
            attrs["loop.last_timestamp"] = entry.last_ts
            otel["attributes"] = attrs
        context["otel"] = otel
        return {
            "eventId": f"local-log-loop:{entry.server_id}:{digest}",
            "kind": "SERVER_LOG",
            "timestamp": entry.last_ts,
            "serverId": entry.server_id,
            "serverName": entry.server_name,
            "content": (
                f"同类服务器报错已合并：{suppressed} 条重复日志被过滤；"
                f"首条样本：{_truncate(entry.sample, 320)}"
            ),
            "tags": [
                "server_log",
                "runtime_log",
                "loop_suppressed",
                entry.level.lower(),
            ],
            "context": context,
        }

    @staticmethod
    def _is_loop_candidate(observation: dict[str, Any]) -> bool:
        context = observation.get("context") or {}
        level = str(context.get("level") or "").upper()
        if level in {"WARN", "WARNING", "ERROR", "FATAL", "SEVERE"}:
            return True
        text = f"{observation.get('content') or ''} {' '.join(observation.get('tags') or [])}"
        lowered = text.lower()
        return any(word in lowered for word in _ERROR_WORDS)


class MineSentinelRuntimeLogTailer:
    """Backfill and tail Minecraft logs from paths configured in AstrBot."""

    def __init__(
        self,
        config: MineSentinelRuntimeLogConfig,
        batch_handler: BatchHandler,
        io_runner: IoRunner | None = None,
    ):
        self.config = config
        self.batch_handler = batch_handler
        self.io_runner = io_runner or asyncio.to_thread
        self.loop_filter = RuntimeLogLoopFilter(config)
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()
        # Supervisor settings for transient-failure recovery (file permission,
        # rotation race, network drive hiccup). Exponential backoff with cap.
        self._initial_backoff_seconds = 5.0
        self._max_backoff_seconds = 300.0
        self._max_restarts = 10

    @property
    def enabled_sources(self) -> list[MineSentinelLogSourceConfig]:
        return [
            source
            for source in self.config.sources
            if source.enabled and _resolve_log_file(source) is not None
        ]

    def start(self):
        if not self.config.enabled:
            return
        sources = self.enabled_sources
        if not sources:
            logger.warning(
                "[MineSentinel] 未配置任何 Minecraft 运行日志源，"
                "请在 _conf_schema 的 mine_sentinel.runtime_log.sources 中指定一个或多个服务器，"
                "例如：{server_id: 'survival', server_type: 'minecraft', root: '/path/to/paper'} "
                "或 {server_type: 'velocity', logs_dir: '/path/to/velocity/logs'}。"
                "Velocity 群组服请把 Velocity 根目录和每个后端服分别添加为一个 source。"
            )
            return
        self._stopping.clear()
        for source in sources:
            log_file = _resolve_log_file(source)
            if log_file is None:
                continue
            self._tasks.append(asyncio.create_task(self._run_source(source, log_file)))
        logger.info(
            f"[MineSentinel] runtime log ingestion started: {len(self._tasks)} source(s) "
            f"-> {', '.join(f'{s.server_id}({s.server_type})' for s in sources if _resolve_log_file(s) is not None)}"
        )

    async def stop(self):
        self._stopping.set()
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self._emit_observations(self.loop_filter.drain_due(force=True))

    async def _run_source(self, source: MineSentinelLogSourceConfig, log_file: Path):
        """Supervisor wrapper: restart the tailer loop on transient failures."""
        backoff = self._initial_backoff_seconds
        max_backoff = self._max_backoff_seconds
        consecutive_failures = 0
        while not self._stopping.is_set():
            try:
                await self._run_source_loop(source, log_file)
                # Normal exit (stopping) — no retry needed.
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_failures += 1
                logger.error(
                    f"[MineSentinel] runtime log source {source.server_id} crashed "
                    f"(attempt {consecutive_failures}): {exc}. Restarting in {backoff}s."
                )
                if consecutive_failures >= self._max_restarts:
                    logger.error(
                        f"[MineSentinel] runtime log source {source.server_id} reached "
                        f"max_restarts={self._max_restarts}, giving up."
                    )
                    return
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=backoff)
                    return  # Stopped during backoff.
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, max_backoff)

    async def _run_source_loop(self, source: MineSentinelLogSourceConfig, log_file: Path):
        state = _SourceState(source=source, log_file=log_file)
        if self.config.backfill_on_start:
            await self._backfill_source(state, self.config.backfill_window_minutes)
        elif self.config.initial_lines:
            await self._emit_initial_tail(state)
        state.position = await self.io_runner(_file_size, state.log_file) or 0
        while not self._stopping.is_set():
            await asyncio.sleep(max(1, self.config.poll_interval_seconds))
            await self._poll_source(state)
            await self._emit_observations(self.loop_filter.drain_due(force=False))

    async def _poll_source(self, state: _SourceState):
        size = await self.io_runner(_file_size, state.log_file)
        if size is None:
            if not state.missing_logged:
                logger.warning(f"[MineSentinel] runtime log not found: {state.log_file}")
                state.missing_logged = True
            return
        state.missing_logged = False

        if size < state.position:
            # 日志轮转：旧文件被截断/重命名，新文件从头开始。
            # PR9 hotfix: 不要直接清空 backlog——未处理的旧行应继续被 drain。
            # partial_line 是旧文件最后一条未闭合的行（bytes），轮转意味着
            # 旧文件已 EOF，把它解码为完整行追加到 backlog（避免与新文件首行
            # 拼接产生垃圾）。PR9 hotfix v5: partial_line 是 bytes，需先解码。
            had_pending = bool(state.backlog) or bool(state.partial_line)
            if state.partial_line:
                # 轮转时旧文件已 EOF，残留 bytes 视为完整行；用 errors="replace"
                # 兜底（理论上正常路径下 partial_line 已是完整 UTF-8 序列）。
                state.backlog.append(
                    state.partial_line.decode("utf-8", errors="replace")
                )
                state.partial_line = b""
                state.partial = ""
            await self._backfill_source(
                state,
                max(10, self.config.poll_interval_seconds * 3 // 60 + 1),
            )
            state.position = 0
            if had_pending:
                logger.info(
                    f"[MineSentinel] runtime log {state.source.server_id} "
                    f"rotated; {len(state.backlog)} pending backlog line(s) "
                    f"will be drained before reading new file content."
                )
        if size == state.position and not state.has_pending:
            # 无新数据且无 backlog：本轮无事可做。
            # 注意：如果有 backlog 未处理，即使文件没新数据也必须继续，
            # 否则 backlog 会永久滞留（PR7 修复）。
            return

        lines, position, partial_line, backlog, dropped_count = await self.io_runner(
            _read_appended_lines,
            state.log_file,
            state.position,
            state.partial_line,
            state.backlog,
            self.config.max_bytes_per_poll,
            self.config.max_lines_per_poll,
            self.config.max_line_length,
        )
        state.position = position
        state.partial_line = partial_line
        state.backlog = backlog
        # 清空旧 partial 字段（兼容）：新代码用 partial_line + backlog
        state.partial = ""
        if dropped_count > 0:
            logger.warning(
                f"[MineSentinel] runtime log {state.source.server_id} dropped "
                f"{dropped_count} line(s) in burst (max_lines_per_poll="
                f"{self.config.max_lines_per_poll}); consider raising the limit."
            )
            await self._emit_dropped_observation(state, dropped_count)
        await self._emit_lines(state, lines, state.log_file)

    async def _emit_initial_tail(self, state: _SourceState):
        lines = await self.io_runner(
            _read_tail_lines,
            state.log_file,
            self.config.initial_lines,
            self.config.max_line_length,
        )
        await self._emit_lines(state, lines, state.log_file)

    async def _backfill_source(self, state: _SourceState, window_minutes: int):
        cutoff_ms = int((time.time() - max(1, window_minutes) * 60) * 1000)
        rows = await self.io_runner(
            _read_backfill_lines,
            state.source,
            self.config,
            cutoff_ms,
        )
        if not rows:
            return
        observations: list[dict[str, Any]] = []
        emitted = 0
        emit_threshold = max(1, self.config.max_lines_per_poll)
        for line, timestamp_ms, path_text in rows:
            observations.extend(
                self.loop_filter.process(
                    _build_observation(
                        state.source,
                        Path(path_text),
                        line,
                        timestamp_ms,
                        self.config.max_line_length,
                        runtime_config=self.config,
                    )
                )
            )
            if len(observations) >= emit_threshold:
                emitted += len(observations)
                await self._emit_observations(observations)
                observations = []
        observations.extend(self.loop_filter.drain_due(force=True))
        emitted += len(observations)
        await self._emit_observations(observations)
        logger.info(
            f"[MineSentinel] runtime log backfilled {emitted} record(s) "
            f"for {state.source.server_id}"
        )

    async def _emit_lines(
        self,
        state: _SourceState,
        lines: list[str],
        log_file: Path,
    ):
        if not lines:
            return
        observations: list[dict[str, Any]] = []
        base_date = _infer_file_date(log_file)
        for line in lines:
            timestamp_ms = _parse_log_timestamp(line, base_date, state.last_timestamp_ms)
            state.last_timestamp_ms = timestamp_ms
            observation = _build_observation(
                state.source,
                log_file,
                line,
                timestamp_ms,
                self.config.max_line_length,
                runtime_config=self.config,
            )
            observations.extend(self.loop_filter.process(observation))
        await self._emit_observations(observations)

    async def _emit_dropped_observation(self, state: _SourceState, dropped_count: int):
        """Emit a synthetic observation so burst drops surface in reports, not just logs."""
        timestamp_ms = int(time.time() * 1000)
        server_id = state.source.server_id or "minecraft"
        server_name = state.source.server_name or state.source.server_id or "Minecraft"
        server_type = (state.source.server_type or "minecraft").lower()
        body = (
            f"本窗口审计日志不完整：突发日志量超过 backlog 上限，"
            f"{dropped_count} 行早期日志被丢弃（max_lines_per_poll="
            f"{self.config.max_lines_per_poll}）。建议调大 max_bytes_per_poll / max_lines_per_poll。"
        )
        observation = {
            "eventId": f"local-drop:{state.source.server_id}:{timestamp_ms}",
            "kind": "SERVER_LOG",
            "timestamp": timestamp_ms,
            "serverId": server_id,
            "serverName": server_name,
            "content": body,
            "tags": ["server_log", "runtime_log", "loop_suppressed", "warn"],
            "context": {
                "source": "astrbot_runtime_log",
                "logFile": str(state.log_file),
                "level": "WARN",
                "loopSuppressed": dropped_count,
                "drop_event": True,
                "serverType": server_type,
                "otel": _otel_fields(
                    timestamp_ms=timestamp_ms,
                    observed_ms=timestamp_ms,
                    level="WARN",
                    body=body,
                    event_name="mine_sentinel.dropped_lines",
                    server_id=server_id,
                    server_type=server_type,
                    server_name=server_name,
                    log_file=str(state.log_file),
                    attributes={
                        "drop.count": dropped_count,
                        "drop.max_lines_per_poll": self.config.max_lines_per_poll,
                        "log.compressed": state.log_file.name.lower().endswith(".gz"),
                    },
                ),
            },
        }
        await self._emit_observations(self.loop_filter.process(observation))

    async def _emit_observations(self, observations: list[dict[str, Any]]):
        if not observations:
            return
        by_server: dict[str, list[dict[str, Any]]] = {}
        names: dict[str, str] = {}
        for observation in observations:
            server_id = str(observation.get("serverId") or "minecraft")
            by_server.setdefault(server_id, []).append(observation)
            names.setdefault(server_id, str(observation.get("serverName") or server_id))
        for server_id, items in by_server.items():
            for index in range(0, len(items), max(1, self.config.max_lines_per_poll)):
                chunk = items[index : index + self.config.max_lines_per_poll]
                await self.batch_handler(
                    server_id,
                    {
                        "serverId": server_id,
                        "serverName": names.get(server_id) or server_id,
                        "observations": chunk,
                    },
                )


def _resolve_log_file(source: MineSentinelLogSourceConfig) -> Path | None:
    if source.log_file:
        return Path(source.log_file).expanduser()
    if source.logs_dir:
        return Path(source.logs_dir).expanduser() / "latest.log"
    if source.root:
        return Path(source.root).expanduser() / "logs" / "latest.log"
    return None


def _logs_dir(source: MineSentinelLogSourceConfig) -> Path | None:
    if source.logs_dir:
        return Path(source.logs_dir).expanduser()
    if source.root:
        return Path(source.root).expanduser() / "logs"
    log_file = _resolve_log_file(source)
    return log_file.parent if log_file else None


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _read_appended_lines(
    path: Path,
    position: int,
    partial_line: bytes,
    backlog: deque[str],
    max_bytes: int,
    max_lines: int,
    max_line_length: int,
) -> tuple[list[str], int, bytes, deque[str], int]:
    """Read appended bytes from ``path`` and split into lines.

    Returns ``(lines, new_position, next_partial_line, next_backlog, dropped_count)``.

    Burst handling 采用 deque backlog（PR9 优化）：

    1. 优先从 backlog 弹出 ``max_lines`` 行处理；
    2. backlog 不够时再读文件追加新行；
    3. 新读出来超过 ``max_lines`` 的完整行 append 到 backlog 末尾；
    4. ``partial_line`` 只保存未闭合的一行的尾部 bytes（未解码）。

    PR9 hotfix v5: ``partial_line`` 是 bytes-level。读取的 ``data`` 是
    bytes，与 ``partial_line`` 拼接后用 ``codecs.getincrementaldecoder``
    增量解码：当 bytes 切断在 UTF-8 多字节字符中间时，增量解码器返回
    已解码的前缀，未消费的尾部 bytes 保留在 decoder 内部 buffer，
    通过 ``decoder.buffer`` 取出存入 ``next_partial_line``，下一轮再拼接。
    避免 ``errors="replace"`` 把半个中文字符替换成 U+FFFD 污染证据。

    只有当 backlog 累积超过 ``max_lines * 4`` 行时，才丢弃最旧的行并报告
    ``dropped_count``。相比旧 str partial 方案，避免了大 burst 时反复
    split/join 的 O(n) 字符串复制开销。
    """
    import codecs

    max_backlog_lines = max(1, max_lines * 4)

    # 第一步：先从 backlog 取 max_lines 行
    lines: list[str] = []
    while len(lines) < max_lines and backlog:
        lines.append(backlog.popleft())

    new_position = position
    dropped_count = 0

    # 第二步：如果 backlog 已经填满本轮 max_lines，不需要读文件
    # 但要处理 backlog 超限丢弃
    if len(lines) >= max_lines:
        if len(backlog) > max_backlog_lines:
            dropped_count = len(backlog) - max_backlog_lines
            while dropped_count > 0 and backlog:
                backlog.popleft()
        return lines, new_position, partial_line, backlog, dropped_count

    # 第三步：backlog 不足 max_lines，读文件追加
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, position))
            data = handle.read(max(1, max_bytes))
            new_position = handle.tell()
    except OSError:
        return lines, position, partial_line, backlog, 0

    if not data:
        # 无新数据：可能 backlog 还剩一些（少于 max_lines），本轮就处理这些
        return lines, new_position, partial_line, backlog, 0

    # PR9 hotfix v5: bytes-level 增量 UTF-8 解码。
    # 把上一轮残留的 partial_line bytes 与本轮新读到的 bytes 拼接，
    # 用 IncrementalDecoder 解码。strict 模式：若拼接后仍切断在多字节
    # 字符中间，decoder.decode(raw, final=False) 返回已解码的前缀，
    # 未消费的尾部 bytes 保留在 decoder.buffer 中。
    # 若存在非法 UTF-8 字节序列（不是切断，而是真的损坏），strict 会
    # 抛 UnicodeDecodeError，此时回退 errors="replace" 解码全量。
    raw = partial_line + data
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        text = decoder.decode(raw, final=False)
        truncated_bytes = decoder.buffer  # 切断在多字节字符中间的尾部
    except UnicodeDecodeError:
        # 真正的 UTF-8 损坏（非切断），回退 replace 模式，buffer 清空。
        text = raw.decode("utf-8", errors="replace")
        truncated_bytes = b""

    parts = text.splitlines(keepends=True)
    if parts and not parts[-1].endswith(("\n", "\r")):
        next_partial_str = parts.pop()
    else:
        next_partial_str = ""

    new_lines = [_truncate(part.rstrip("\r\n"), max_line_length) for part in parts if part.strip()]

    # 填满本轮 lines，剩余 append 到 backlog
    for line in new_lines:
        if len(lines) < max_lines:
            lines.append(line)
        else:
            backlog.append(line)

    # backlog 超限丢弃最旧的
    if len(backlog) > max_backlog_lines:
        dropped_count = len(backlog) - max_backlog_lines
        for _ in range(dropped_count):
            if not backlog:
                break
            backlog.popleft()

    # 计算 next_partial_line（bytes）：
    # - next_partial_str：已解码但未闭合的完整行（无换行）
    # - truncated_bytes：切断在多字节字符中间的尾部（decoder 未消费）
    # 两者可能同时存在：text 末尾是完整行 + 切断的字符尾部。
    # 拼接顺序：next_partial_str 编码 + truncated_bytes。
    if next_partial_str or truncated_bytes:
        next_partial_line = next_partial_str.encode("utf-8") + truncated_bytes
        # 裁剪过长的 partial_line（按 bytes 长度，避免跨轮堆积超长缓冲）。
        # 注意：按 bytes 切片可能再次切断 UTF-8，但下一轮的 IncrementalDecoder
        # 会把切断的尾部作为新的 truncated_bytes 保留，不会污染。
        if len(next_partial_line) > max_line_length * 6:
            next_partial_line = next_partial_line[-(max_line_length * 4) :]
    else:
        next_partial_line = b""

    return lines, new_position, next_partial_line, backlog, dropped_count


def _read_tail_lines(path: Path, line_count: int, max_line_length: int) -> list[str]:
    if line_count <= 0:
        return []
    try:
        chunks: list[bytes] = []
        total = 0
        newline_count = 0
        max_bytes = max(65536, line_count * (max_line_length + 80))
        with path.open("rb") as handle:
            handle.seek(0, 2)
            position = handle.tell()
            while position > 0 and newline_count <= line_count and total < max_bytes:
                read_size = min(8192, position, max_bytes - total)
                position -= read_size
                handle.seek(position)
                chunk = handle.read(read_size)
                chunks.append(chunk)
                total += len(chunk)
                newline_count += chunk.count(b"\n")
        text = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    return [_truncate(line, max_line_length) for line in lines[-line_count:] if line.strip()]


def _read_backfill_lines(
    source: MineSentinelLogSourceConfig,
    config: MineSentinelRuntimeLogConfig,
    cutoff_ms: int,
) -> list[tuple[str, int, str]]:
    rows: list[tuple[str, int, str]] = []
    for path in _backfill_candidates(source, config, cutoff_ms):
        base_date = _infer_file_date(path)
        last_ts = 0
        for raw_line in _iter_log_file(path):
            line = _truncate(raw_line.rstrip("\r\n"), config.max_line_length)
            if not line.strip():
                continue
            timestamp_ms = _parse_log_timestamp(line, base_date, last_ts)
            last_ts = timestamp_ms
            if timestamp_ms < cutoff_ms:
                continue
            rows.append((line, timestamp_ms, str(path)))
            if len(rows) >= config.max_backfill_lines:
                return rows
    return rows


# PR9: per-process 已扫描 .gz 归档缓存。
# 同一个 .log.gz 文件内容不会变化（归档后只读），同一进程内对同一小时
# 重复扫描会浪费 CPU 和磁盘 IO。缓存键 (path, mtime, hour_start_ms)，
# 命中时直接复用上次结果。latest.log 不缓存（实时增长）。
# PR9 hotfix: 改用 OrderedDict 实现 LRU 淘汰，命中时移到末尾（最近使用），
# 上限超过时弹出最旧（最久未用）条目，替代原来按 key 字典序保留的粗略策略。
from collections import OrderedDict as _OrderedDict

_GZ_SCAN_CACHE_MAX_ENTRIES = 64
_gz_scan_cache: _OrderedDict[tuple[str, int, int], list[tuple[str, int, str]]] = _OrderedDict()


def _gz_scan_cache_get(
    path: Path, mtime: int, hour_start_ms: int
) -> list[tuple[str, int, str]] | None:
    key = (str(path), mtime, hour_start_ms)
    value = _gz_scan_cache.get(key)
    if value is not None:
        # LRU: 命中时移到末尾（最近使用）。
        _gz_scan_cache.move_to_end(key)
    return value


def _gz_scan_cache_put(
    path: Path,
    mtime: int,
    hour_start_ms: int,
    rows: list[tuple[str, int, str]],
) -> None:
    key = (str(path), mtime, hour_start_ms)
    if key in _gz_scan_cache:
        # 已存在：更新值并移到末尾。
        _gz_scan_cache[key] = rows
        _gz_scan_cache.move_to_end(key)
        return
    while len(_gz_scan_cache) >= _GZ_SCAN_CACHE_MAX_ENTRIES:
        # LRU 淘汰：弹出最旧（最久未用）的条目。
        _gz_scan_cache.popitem(last=False)
    _gz_scan_cache[key] = rows


def _file_date_overlaps_hour(
    file_date: date | None,
    hour_start_ms: int,
    hour_end_ms: int,
) -> bool:
    """PR9: 文件名日期是否与目标小时区间重叠（含前一天，覆盖跨日边界）。

    无文件名日期时返回 True（保守保留，让 mtime/内容过滤决定）。
    """
    if file_date is None:
        return True
    start_day = datetime.fromtimestamp(hour_start_ms / 1000).date()
    end_day = datetime.fromtimestamp(hour_end_ms / 1000).date()
    # 允许前一天（跨日边界的归档）和后一天（极端时钟漂移）。
    return file_date >= start_day - timedelta(days=1) and file_date <= end_day + timedelta(days=1)


def read_hour_log_lines(
    source: MineSentinelLogSourceConfig,
    hour_start_ms: int,
    hour_end_ms: int,
    max_lines: int = 20000,
    max_line_length: int = 4000,
) -> list[tuple[str, int, str]]:
    """Read log lines whose timestamp falls in [hour_start_ms, hour_end_ms).

    Directly scans the logs/ directory (latest.log + archives) without any
    long-lived polling loop, so it has zero impact on Minecraft server mspt/tps.
    Returns a list of (line, timestamp_ms, file_path) tuples.

    PR9 优化：
    - 文件名日期预过滤：归档文件名日期与目标小时不在同一天（含前一天）则跳过，
      避免 .gz 很多时逐个解压扫描。
    - .gz 已扫描缓存：同一进程内对同一 (path, mtime, hour_start) 重复扫描时
      直接复用上次结果，归档文件内容不会变化。
    """
    if hour_end_ms <= hour_start_ms:
        return []
    # Start scanning a bit before the hour so lines without a date prefix but
    # carrying only HH:MM:SS can still be attributed correctly via fallback walk.
    scan_cutoff_ms = hour_start_ms - 60 * 60 * 1000
    # Reuse the backfill candidate selection so .log.gz archives are included.
    dummy_config = MineSentinelRuntimeLogConfig(
        backfill_on_start=True,
        backfill_window_minutes=max(
            120, int((hour_end_ms - scan_cutoff_ms) / 60000) + 60
        ),
        max_backfill_files=20,
        max_backfill_lines=max_lines,
    )
    rows: list[tuple[str, int, str]] = []
    for path in _backfill_candidates(source, dummy_config, scan_cutoff_ms):
        # PR9: 文件名日期预过滤。归档 .log.gz 通常按日期命名，
        # 如果文件名日期明显不在目标小时附近，直接跳过不解压。
        file_date = _date_from_filename(path)
        if not _file_date_overlaps_hour(file_date, hour_start_ms, hour_end_ms):
            continue
        is_gz = path.name.lower().endswith(".gz")
        # PR9: .gz 已扫描缓存。归档文件不会变化，同进程重复扫描直接复用。
        if is_gz:
            try:
                mtime = int(path.stat().st_mtime)
            except OSError:
                mtime = 0
            cached = _gz_scan_cache_get(path, mtime, hour_start_ms)
            if cached is not None:
                for line, ts, fp in cached:
                    if ts < hour_start_ms or ts >= hour_end_ms:
                        continue
                    rows.append((line, ts, fp))
                    if len(rows) >= max_lines:
                        return rows
                continue
            cached_rows: list[tuple[str, int, str]] = []
            base_date = _infer_file_date(path)
            last_ts = 0
            for raw_line in _iter_log_file(path):
                line = _truncate(raw_line.rstrip("\r\n"), max_line_length)
                if not line.strip():
                    continue
                timestamp_ms = _parse_log_timestamp(line, base_date, last_ts)
                last_ts = timestamp_ms
                # 缓存该 .gz 在 [hour_start-1h, hour_end] 范围内的所有行，
                # 后续同小时重复扫描可直接复用（不必重新解压）。
                if timestamp_ms >= scan_cutoff_ms and timestamp_ms < hour_end_ms:
                    cached_rows.append((line, timestamp_ms, str(path)))
                if timestamp_ms < hour_start_ms:
                    continue
                if timestamp_ms >= hour_end_ms:
                    continue
                rows.append((line, timestamp_ms, str(path)))
                if len(rows) >= max_lines:
                    _gz_scan_cache_put(path, mtime, hour_start_ms, cached_rows)
                    return rows
            _gz_scan_cache_put(path, mtime, hour_start_ms, cached_rows)
            continue
        # latest.log / 普通 .log：实时增长，不缓存。
        base_date = _infer_file_date(path)
        last_ts = 0
        for raw_line in _iter_log_file(path):
            line = _truncate(raw_line.rstrip("\r\n"), max_line_length)
            if not line.strip():
                continue
            timestamp_ms = _parse_log_timestamp(line, base_date, last_ts)
            last_ts = timestamp_ms
            if timestamp_ms < hour_start_ms:
                continue
            if timestamp_ms >= hour_end_ms:
                continue
            rows.append((line, timestamp_ms, str(path)))
            if len(rows) >= max_lines:
                return rows
    return rows


def build_hour_observations(
    source: MineSentinelLogSourceConfig,
    hour_start_ms: int,
    hour_end_ms: int,
    max_lines: int = 20000,
    max_records: int = 5000,
    max_line_length: int = 1000,
    runtime_config: MineSentinelRuntimeLogConfig | None = None,
) -> list[dict]:
    """Read an hour of logs and turn them into observation dicts (in-memory only).

    Does not write to disk; the caller decides what to do with the result.
    """
    rows = read_hour_log_lines(
        source,
        hour_start_ms,
        hour_end_ms,
        max_lines=max_lines,
        max_line_length=max_line_length,
    )
    if not rows:
        return []
    observations: list[dict] = []
    log_file = _resolve_log_file(source)
    log_file_str = str(log_file) if log_file else ""
    for line, timestamp_ms, source_file in rows:
        level = _detect_level(line)
        # Reuse _build_observation so the schema stays consistent with the polling path.
        observation = _build_observation(
            source,
            Path(source_file),
            line,
            timestamp_ms,
            max_line_length,
            runtime_config=runtime_config,
        )
        # Override the logFile context to point at the actual source file,
        # and drop the compressed flag (it was inferred from the original latest.log).
        if isinstance(observation.get("context"), dict):
            observation["context"]["logFile"] = source_file
            observation["context"]["source"] = "astrbot_hourly_read"
        observations.append(observation)
        if len(observations) >= max_records:
            break
    return observations


def _backfill_candidates(
    source: MineSentinelLogSourceConfig,
    config: MineSentinelRuntimeLogConfig,
    cutoff_ms: int,
) -> list[Path]:
    logs_dir = _logs_dir(source)
    if logs_dir is None:
        return []
    candidates: dict[str, Path] = {}
    latest = _resolve_log_file(source)
    if latest:
        candidates[str(latest)] = latest
    try:
        for path in logs_dir.iterdir():
            name = path.name.lower()
            if path.is_file() and (
                name == "latest.log" or name.endswith(".log") or name.endswith(".log.gz")
            ):
                candidates[str(path)] = path
    except OSError:
        return [latest] if latest and latest.exists() else []

    cutoff_sec = cutoff_ms / 1000
    recent: list[Path] = []
    for path in candidates.values():
        try:
            stat = path.stat()
        except OSError:
            continue
        file_date = _date_from_filename(path)
        include_by_date = False
        if file_date:
            cutoff_day = datetime.fromtimestamp(cutoff_sec).date() - timedelta(days=1)
            include_by_date = file_date >= cutoff_day
        if stat.st_mtime >= cutoff_sec - 86400 or include_by_date or path == latest:
            recent.append(path)
    recent.sort(key=lambda item: _safe_mtime(item), reverse=True)
    recent = recent[: max(1, config.max_backfill_files)]
    recent.sort(key=lambda item: _safe_mtime(item))
    return recent


def _iter_log_file(path: Path):
    try:
        if path.name.lower().endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
                yield from handle
        else:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                yield from handle
    except OSError:
        return


def _should_parse_and_track(
    level: str,
    lowered_content: str,
    runtime_config: MineSentinelRuntimeLogConfig | None,
) -> tuple[bool, bool]:
    """决定是否对这条日志做 Drain3 解析和异常检测。

    返回 (run_template, run_anomaly)：
    - run_template=False 时用 fingerprint 作为 template_id（降采样，跳过 parse tree 更新）
    - run_anomaly=False 时跳过 anomaly detector.observe（不更新 EWMA/分位数基线）

    策略（PR9 INFO 降采样）：
    - mode="all"：全量解析；anomaly 是否跟踪 INFO 由 anomaly_track_info 控制
    - mode="warn_error"：只 WARN/ERROR/FATAL/SEVERE 才解析和跟踪
    - mode="interesting"：WARN+ 始终解析；INFO 命中关键词才解析和跟踪
    """
    if runtime_config is None:
        return True, True

    mode = (runtime_config.template_parse_mode or "all").strip().lower()
    if mode not in {"all", "warn_error", "interesting"}:
        mode = "all"

    upper = level.upper()
    is_warn_plus = upper in {"ERROR", "FATAL", "SEVERE", "WARN", "WARNING"}

    if mode == "all":
        # 全量解析；anomaly 是否跟踪 INFO 由单独开关控制
        return True, runtime_config.anomaly_track_info or is_warn_plus

    # warn_error / interesting：WARN+ 始终解析和跟踪
    if is_warn_plus:
        return True, True

    if mode == "warn_error":
        # INFO/DEBUG 都不解析、不跟踪
        return False, False

    # mode == "interesting"：INFO 命中关键词才解析和跟踪
    if _is_interesting_info(lowered_content):
        return True, True
    # 普通 INFO：用 fingerprint，不进 anomaly
    return False, False


def _is_interesting_info(lowered_content: str) -> bool:
    """检查一条 INFO 日志是否值得关注（template/anomaly）。

    覆盖 Minecraft 服常见的事故线索：性能（tps/mspt/lag/overloaded）、
    连接（disconnect/timeout/kicked）、世界/区块、内存/GC、插件异常、
    保存/重启等关键状态变化。
    """
    return any(kw in lowered_content for kw in _INTERESTING_INFO_KEYWORDS)


def _skip_parsed_template(content: str, fingerprint: str) -> ParsedTemplate:
    """构造降采样的 ParsedTemplate（不调用 drain3，用 fingerprint 作为 id）。"""
    return ParsedTemplate(
        template_id=fingerprint,
        template=content,
        params=[],
        is_new_template=False,
        cluster_size=0,
        fallback=True,
        fallback_fingerprint=fingerprint,
    )


# daily_noise_patterns 编译缓存：key=tuple(patterns) → list[re.Pattern]。
# 避免每条日志都重新编译正则；config 不变时直接复用。
_NOISE_PATTERN_CACHE: dict[tuple[str, ...], list["re.Pattern[str]"]] = {}


def _compile_noise_patterns(patterns: list[str]) -> list["re.Pattern[str]"]:
    """编译 daily_noise_patterns，缓存结果。无效正则会被忽略并记日志。"""
    key = tuple(patterns)
    cached = _NOISE_PATTERN_CACHE.get(key)
    if cached is not None:
        return cached
    compiled: list["re.Pattern[str]"] = []
    for raw in patterns:
        try:
            compiled.append(re.compile(raw, re.IGNORECASE))
        except re.error as exc:
            logger.warning(
                f"[MineSentinel] daily_noise_patterns 正则 {raw!r} 编译失败：{exc}，已忽略。"
            )
    _NOISE_PATTERN_CACHE[key] = compiled
    return compiled


def _match_noise_patterns(content: str, compiled: list["re.Pattern[str]"]) -> bool:
    """检查 content 是否命中任一编译后的噪声正则。"""
    return any(pattern.search(content) for pattern in compiled)


def _detect_chat_message(content: str) -> tuple[str, str] | None:
    """检测聊天行并返回 (player, message)，非聊天行返回 None。

    识别三种 Minecraft 聊天格式：
    1. ``[Async Chat Thread/INFO]: <player> message`` — 原生聊天线程
    2. ``[Async Chat Thread/INFO]: [Not Secure] [频道] player >> message``
       — CarbonChat 等聊天插件格式（真实 mclo.gs 日志验证）
    3. ``<player> message`` — 直接前缀（控制台回放）

    玩家名最长 16 字符（Minecraft 限制），消息保留原样。
    """
    # 优先匹配聊天插件格式（>> 分隔），需要先剥掉 [Async Chat Thread] 等线程前缀
    stripped = content
    if _CHAT_THREAD_RE.search(content):
        stripped = _CHAT_THREAD_RE.sub("", content).strip()
    stripped = _PREFIX_RE.sub("", stripped).strip()
    plugin_match = _CHAT_PLUGIN_RE.search(stripped)
    if plugin_match:
        player = plugin_match.group("player").strip()
        message = plugin_match.group("message").strip()
        if player and message:
            return player, message
    # 原生 <player> 前缀格式
    match = _CHAT_PLAYER_PREFIX_RE.search(stripped)
    if match:
        player = match.group("player").strip()
        message = match.group("message").strip()
        if player and message:
            return player, message
    # 仅当行包含 Async Chat Thread 线程标签但无法解析玩家时，视为无玩家聊天
    if _CHAT_THREAD_RE.search(content):
        if stripped:
            return "", stripped
    return None


# 刷屏检测阈值
# PR10 v2: 刷屏的准确定义是"同一ID短时间集中发送大量重复或高度相似的信息"，
# 不是单条消息的字符形态。单条形态检测只保留作为"无意义符号消息"子判断，
# 真正的刷屏检测在 _detect_chat_flood（玩家级时间窗口聚合）里做。
# 参考定义：百度百科"同一ID短时间集中发送大量内容重复或高度相似的信息"；
# 社区规则"连续发送5条及以上重复信息=轻微刷屏，10条及以上=恶意刷屏"。
_CHAT_MEANINGLESS_REPEAT_CHAR_MIN = 8  # 单条消息同一字符连续重复 >=8 次视为无意义符号
_CHAT_MEANINGLESS_REPEAT_CHAR_RE = re.compile(
    r"(.)\1{" + str(_CHAT_MEANINGLESS_REPEAT_CHAR_MIN - 1) + r",}"
)


def _detect_meaningless_message(message: str) -> bool:
    """检测单条消息是否为无意义符号消息（刷屏的子判断，非刷屏本身）。

    判定标准：
    1. 单字符连续重复 >=8 次（如 qqqqqqqq、wwwwwwww）
    2. 纯符号/标点消息（无字母数字汉字）

    注意：单条无意义消息不等于刷屏，需要结合玩家级时间窗口聚合
    （同一玩家短时间多条无意义消息）才构成刷屏。
    """
    if not message:
        return False
    if _CHAT_MEANINGLESS_REPEAT_CHAR_RE.search(message):
        return True
    # 纯符号/标点（无字母数字汉字）
    has_content = any(c.isalnum() or "\u4e00" <= c <= "\u9fff" for c in message)
    if not has_content and len(message) >= 3:
        return True
    return False


# --- 刷屏（玩家级时间窗口聚合）阈值 ---
# 参考社区规则：连续 5 条重复=轻微刷屏，10 条=恶意刷屏。
# 注意：刷屏的核心是"重复/相似信息"或"无意义符号"，不是单纯的"消息多"。
# 活跃玩家正常聊天也可能 60 秒内发 5 条不同内容，不应判为刷屏。
# 因此 high_frequency 阈值设高（30 秒内 >=8 条，接近轰炸级别），
# repeat_content/meaningless 阈值按社区规则（5 条重复/3 条无意义）。
_CHAT_FLOOD_HIGH_FREQ_WINDOW_MS = 30 * 1000  # 30 秒窗口（高频刷屏）
_CHAT_FLOOD_HIGH_FREQ_COUNT = 8  # 30 秒内 >=8 条消息视为高频刷屏（轰炸级别）
_CHAT_FLOOD_REPEAT_WINDOW_MS = 5 * 60 * 1000  # 5 分钟窗口（重复内容）
_CHAT_FLOOD_REPEAT_COUNT = 5  # 5 分钟内 >=5 条相同/高度相似消息视为重复刷屏
_CHAT_FLOOD_MEANINGLESS_COUNT = 3  # 5 分钟内 >=3 条无意义符号消息视为无意义刷屏


def _normalize_message(message: str) -> str:
    """归一化消息用于重复度比较：去空白、转小写、去标点。

    "Hello World!" 和 "hello world" 视为相同消息。
    """
    if not message:
        return ""
    # 去空白和标点，转小写
    cleaned = re.sub(r"\s+", "", message).lower()
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]", "", cleaned)
    return cleaned


def _detect_chat_flood(
    chat_records: list["ObservationRecord"],
) -> dict[str, list[dict[str, Any]]]:
    """检测玩家级刷屏行为，返回 {player: [flood_event, ...]}。

    每个 flood_event 含：
    - player: 玩家名
    - flood_type: high_frequency / repeat_content / meaningless
    - window_start_ms / window_end_ms: 时间窗口
    - message_count: 窗口内消息数
    - samples: 最多 5 条样本消息原文

    三类刷屏（参考百度百科+社区规则定义）：
    1. high_frequency: 60 秒内同一玩家发送 >=5 条消息（高频刷屏）
    2. repeat_content: 5 分钟内同一玩家发送 >=5 条相同/高度相似消息（重复刷屏）
    3. meaningless: 5 分钟内同一玩家发送 >=5 条无意义符号消息（无意义刷屏）
    """
    from .models import ObservationRecord as _OR  # 类型提示用

    # 按玩家聚合聊天记录
    player_records: dict[str, list[_OR]] = {}
    for record in chat_records:
        ctx = record.context or {}
        player = str(ctx.get("chatPlayer") or "").strip()
        if not player:
            continue
        player_records.setdefault(player, []).append(record)

    floods: dict[str, list[dict[str, Any]]] = {}
    for player, records in player_records.items():
        # 按时间排序
        records_sorted = sorted(records, key=lambda r: r.timestamp or 0)
        player_floods = _detect_player_floods(player, records_sorted)
        if player_floods:
            floods[player] = player_floods
    return floods


def _detect_player_floods(
    player: str, records: list["ObservationRecord"]
) -> list[dict[str, Any]]:
    """检测单个玩家的刷屏事件。"""
    floods: list[dict[str, Any]] = []
    seen_windows: set[tuple[str, int, int]] = set()  # 避免重复事件

    # 1. 高频刷屏：30 秒滑窗内 >=8 条消息（轰炸级别，避免误判活跃玩家）
    for i, start_record in enumerate(records):
        window_start = start_record.timestamp or 0
        window_end = window_start + _CHAT_FLOOD_HIGH_FREQ_WINDOW_MS
        window_records = [
            r for r in records[i:]
            if (r.timestamp or 0) >= window_start and (r.timestamp or 0) <= window_end
        ]
        if len(window_records) >= _CHAT_FLOOD_HIGH_FREQ_COUNT:
            key = ("high_frequency", window_start, window_end)
            if key in seen_windows:
                continue
            seen_windows.add(key)
            floods.append(_make_flood_event(
                player, "high_frequency", window_start,
                window_records[-1].timestamp or window_end,
                window_records,
            ))
            # 跳过这个窗口内的记录，避免重叠
            break

    # 2. 重复刷屏：5 分钟窗口内 >=3 条相同/高度相似消息
    normalized_list = [
        (r, _normalize_message(str((r.context or {}).get("chatMessage") or "")))
        for r in records
    ]
    for i, (start_record, start_norm) in enumerate(normalized_list):
        if not start_norm:
            continue
        window_start = start_record.timestamp or 0
        window_end = window_start + _CHAT_FLOOD_REPEAT_WINDOW_MS
        similar = [
            r for r, norm in normalized_list[i:]
            if (r.timestamp or 0) >= window_start
            and (r.timestamp or 0) <= window_end
            and norm
            and (norm == start_norm or _is_similar(norm, start_norm))
        ]
        if len(similar) >= _CHAT_FLOOD_REPEAT_COUNT:
            key = ("repeat_content", window_start, window_end)
            if key in seen_windows:
                continue
            seen_windows.add(key)
            floods.append(_make_flood_event(
                player, "repeat_content", window_start,
                similar[-1].timestamp or window_end,
                similar,
            ))
            break

    # 3. 无意义刷屏：5 分钟窗口内 >=5 条无意义符号消息
    meaningless_records = [
        r for r in records
        if _detect_meaningless_message(str((r.context or {}).get("chatMessage") or ""))
    ]
    for i, start_record in enumerate(meaningless_records):
        window_start = start_record.timestamp or 0
        window_end = window_start + _CHAT_FLOOD_REPEAT_WINDOW_MS
        window_records = [
            r for r in meaningless_records[i:]
            if (r.timestamp or 0) >= window_start and (r.timestamp or 0) <= window_end
        ]
        if len(window_records) >= _CHAT_FLOOD_MEANINGLESS_COUNT:
            key = ("meaningless", window_start, window_end)
            if key in seen_windows:
                continue
            seen_windows.add(key)
            floods.append(_make_flood_event(
                player, "meaningless", window_start,
                window_records[-1].timestamp or window_end,
                window_records,
            ))
            break

    return floods


def _is_similar(norm_a: str, norm_b: str) -> bool:
    """判断两条归一化后的消息是否高度相似（构成重复刷屏）。

    刷屏的"重复"要求内容几乎相同，不是话题相关。
    判定标准（满足任一）：
    1. 归一化后完全相同
    2. 子串关系：短的是长的子串（如 "加群" vs "加群啊"）
    3. 中等长度消息（3-8 字符）编辑距离 <=1（处理 "好的呀" vs "好的" 等个别字符差异）

    长度 1-2 的单字/双字消息要求完全相同——"对" vs "绷" 编辑距离只有 1，
    但它们是不同内容，不是重复刷屏。
    长消息（>8 字符）不用编辑距离——"message 0" 和 "message 1" 编辑距离只有 1，
    但它们是不同内容。
    """
    if not norm_a or not norm_b:
        return False
    # 1. 完全相同
    if norm_a == norm_b:
        return True
    # 2. 长度差太大不算相似
    if abs(len(norm_a) - len(norm_b)) > 2:
        return False
    # 3. 子串关系（短的是长的子串）
    if len(norm_a) <= len(norm_b):
        if norm_a in norm_b:
            return True
    else:
        if norm_b in norm_a:
            return True
    # 4. 仅对中等长度消息（3-8 字符）用编辑距离，处理个别字符差异
    max_len = max(len(norm_a), len(norm_b))
    if 3 <= max_len <= 8 and _edit_distance(norm_a, norm_b) <= 1:
        return True
    return False


def _edit_distance(a: str, b: str) -> int:
    """计算两个字符串的编辑距离（Levenshtein）。"""
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            cost = 0 if ca == cb else 1
            curr.append(min(curr[-1] + 1, prev[j + 1] + 1, prev[j] + cost))
        prev = curr
    return prev[-1]


def _make_flood_event(
    player: str,
    flood_type: str,
    window_start: int,
    window_end: int,
    records: list["ObservationRecord"],
) -> dict[str, Any]:
    """构造一个刷屏事件。"""
    samples = []
    for r in records[:5]:
        msg = str((r.context or {}).get("chatMessage") or r.content).strip()
        if msg:
            samples.append(msg[:150])
    return {
        "player": player,
        "flood_type": flood_type,
        "window_start_ms": window_start,
        "window_end_ms": window_end,
        "message_count": len(records),
        "samples": samples,
    }


def _detect_vulcan_alert(content: str) -> dict[str, str] | None:
    """检测 Vulcan 反作弊告警并返回 {player, check}，非告警行返回 None。

    只匹配 ``[Vulcan] PlayerName failed CheckName`` 格式的真实告警，
    排除 ``[Vulcan] Loading/Enabling/Starting/hook`` 等插件生命周期日志
    （这些日志也带 [Vulcan] 前缀但不是告警，真实 mclo.gs 日志验证）。

    标准格式：``[Vulcan] PlayerName failed CheckName (VL: 5)``
    或带前缀：``[Server thread/INFO]: [Vulcan] PlayerName failed ...``
    """
    match = _VULCAN_PLAYER_RE.search(content)
    if match:
        return {
            "player": match.group("player").strip(),
            "check": match.group("check").strip(" :,."),
        }
    return None


def _skip_anomaly_result(
    server_id: str,
    template_id: str,
    template: str,
    level: str,
) -> AnomalyResult:
    """构造降采样的 AnomalyResult（不调用 anomaly detector.observe）。"""
    stat = TemplateStat(
        server_id=server_id,
        template_id=template_id,
        template=template,
        level=level,
    )
    return AnomalyResult(
        is_anomaly=False,
        score=0.0,
        reason="skipped: info downsampling",
        stat=stat,
        current_count=0,
        baseline=0.0,
    )


def _build_observation(
    source: MineSentinelLogSourceConfig,
    log_file: Path,
    line: str,
    timestamp_ms: int,
    max_line_length: int,
    runtime_config: MineSentinelRuntimeLogConfig | None = None,
) -> dict[str, Any]:
    content = _sanitize_line(_truncate(line, max_line_length))
    level = _detect_level(content)
    fingerprint = _fingerprint(content)
    lowered = content.lower()
    server_id = source.server_id or "minecraft"

    # PR9: 决定是否对这条日志做 Drain3 解析和异常检测。
    # 高日志量服可设 template_parse_mode="interesting" + anomaly_track_info=false
    # 来跳过普通 INFO 的重处理，大幅降低 CPU。
    run_template, run_anomaly = _should_parse_and_track(level, lowered, runtime_config)

    if run_template:
        # 模板解析：drain3 可用时返回 template_id，否则降级为 fingerprint
        parsed = get_template_miner().parse(content, server_id=server_id or "default")
    else:
        # 降采样：用 fingerprint 作为 template_id，不更新 drain3 parse tree
        parsed = _skip_parsed_template(content, fingerprint)
    template_id = parsed.template_id
    template = parsed.template

    if run_anomaly:
        # 异常检测：基于模板计数突增（EWMA + 分位数）
        anomaly = get_anomaly_detector().observe(
            server_id=server_id,
            template_id=template_id,
            template=template,
            level=level,
            timestamp_ms=timestamp_ms,
        )
    else:
        # 降采样：跳过 anomaly detector.observe，返回零值结果
        anomaly = _skip_anomaly_result(server_id, template_id, template, level)
    digest = hashlib.sha1(
        f"{source.server_id}:{timestamp_ms}:{fingerprint}:{log_file.name}".encode("utf-8")
    ).hexdigest()[:20]
    tags = ["server_log", "runtime_log", level.lower()]
    server_type = (source.server_type or "minecraft").lower()
    if server_type == "velocity":
        tags.append("velocity")
        tags.append("proxy")
    else:
        tags.append("minecraft")
    if "exception" in lowered:
        tags.append("exception")
    if level in {"ERROR", "FATAL", "SEVERE"}:
        tags.append("error")
    elif level in {"WARN", "WARNING"}:
        tags.append("warning")
    if parsed.is_new_template:
        tags.append("new_template")
    if anomaly.is_anomaly:
        tags.append("anomaly_spike")
    if not run_template:
        # 标记降采样记录，便于后续报告/调试识别
        tags.append("info_downsampled")
    # PR10: daily_noise / chat_message / anticheat_vulcan 检测。
    # 这些标签独立于上面的级别/异常标签，由专用配置开关控制。
    daily_noise_hit = False
    chat_info: dict[str, Any] | None = None
    vulcan_info: dict[str, str] | None = None
    if runtime_config is not None:
        if runtime_config.daily_noise_filter_enabled:
            # PR10 hotfix: daily_noise 只过滤 INFO 级别，WARN/ERROR/FATAL 永远不过滤。
            # 真实日志验证：luckperms-worker-N/WARN: Failed to validate connection
            # 是 HikariCP 连接池异常，必须告警；如果用 'luckperms-worker-N/' pattern
            # 一刀切过滤会把真实异常也吞掉。INFO 级别的 luckperms 常规日志才过滤。
            if level == "INFO":
                # 用户配置为空时使用 DEFAULT_DAILY_NOISE_PATTERNS；非空则只用用户的。
                patterns = (
                    runtime_config.daily_noise_patterns
                    if runtime_config.daily_noise_patterns
                    else list(DEFAULT_DAILY_NOISE_PATTERNS)
                )
                if patterns and _match_noise_patterns(content, _compile_noise_patterns(patterns)):
                    tags.append("daily_noise")
                    daily_noise_hit = True
        if runtime_config.chat_summary_enabled:
            chat_info = _detect_chat_message(content)
            if chat_info is not None:
                tags.append("chat_message")
                # PR10 v2: 单条消息不再打 chat_spam 标签——刷屏是玩家级时间窗口
                # 聚合行为（同一ID短时间大量重复/相似消息），不是单条形态。
                # 仅标记 meaningless 子标签供聚合阶段使用。
                if _detect_meaningless_message(chat_info[1]):
                    tags.append("chat_meaningless")
        if runtime_config.vulcan_detect_enabled:
            vulcan_info = _detect_vulcan_alert(content)
            if vulcan_info is not None:
                tags.append("anticheat_vulcan")
    observed_ms = int(time.time() * 1000)
    server_name = source.server_name or source.server_id or "Minecraft"
    context = {
        "source": "astrbot_runtime_log",
        "logFile": str(log_file),
        "level": level,
        "fingerprint": fingerprint,
        "compressed": log_file.name.lower().endswith(".gz"),
        "serverType": server_type,
        "templateId": template_id,
        "template": template,
        "templateSize": parsed.cluster_size,
        "anomalyScore": round(anomaly.score, 3),
        "anomalyReason": anomaly.reason,
        "anomalyBaseline": round(anomaly.baseline, 2),
        "anomalyCurrentCount": anomaly.current_count,
        # OpenTelemetry Logs Data Model 结构化字段
        "otel": _otel_fields(
            timestamp_ms=timestamp_ms,
            observed_ms=observed_ms,
            level=level,
            body=content,
            event_name=template_id,
            server_id=server_id,
            server_type=server_type,
            server_name=server_name,
            log_file=str(log_file),
            attributes={
                "template.id": template_id,
                "template.size": parsed.cluster_size,
                "fingerprint": fingerprint,
                "log.compressed": log_file.name.lower().endswith(".gz"),
                "anomaly.score": round(anomaly.score, 3),
                "anomaly.reason": anomaly.reason,
                "anomaly.baseline": round(anomaly.baseline, 2),
                "anomaly.current_count": anomaly.current_count,
            },
        ),
    }
    if daily_noise_hit:
        context["dailyNoise"] = True
        context["otel"]["attributes"]["daily.noise"] = True
    if chat_info is not None:
        player, message = chat_info
        context["chatPlayer"] = player
        context["chatMessage"] = message
        context["otel"]["attributes"]["chat.player"] = player
        context["otel"]["attributes"]["chat.message"] = message
    if vulcan_info is not None:
        context["vulcanPlayer"] = vulcan_info.get("player", "")
        context["vulcanCheck"] = vulcan_info.get("check", "")
        context["otel"]["attributes"]["vulcan.player"] = vulcan_info.get("player", "")
        context["otel"]["attributes"]["vulcan.check"] = vulcan_info.get("check", "")
    if parsed.params:
        context["templateParams"] = parsed.params[:8]
        context["otel"]["attributes"]["template.params"] = parsed.params[:8]
    if parsed.fallback:
        context["templateFallback"] = True
        context["otel"]["attributes"]["template.fallback"] = True
    if not run_template:
        context["infoDownsampled"] = True
        context["otel"]["attributes"]["info.downsampled"] = True
    return {
        "eventId": f"local-log:{source.server_id}:{digest}",
        "kind": "SERVER_LOG",
        "timestamp": timestamp_ms,
        "serverId": source.server_id or "minecraft",
        "serverName": source.server_name or source.server_id or "Minecraft",
        "content": content,
        "tags": tags,
        "context": context,
    }


def _parse_log_timestamp(line: str, base_date: date | None, fallback_ms: int = 0) -> int:
    text = _ANSI_RE.sub("", line).strip()
    match = _FULL_TS_RE.match(text)
    if match:
        date_text = match.group("date")
        time_text = match.group("time")
        return _datetime_to_ms(date_text, time_text, match.group("ms"))

    match = _TIME_RE.match(text)
    if match and base_date:
        timestamp = _date_time_to_ms(base_date, match.group("time"), match.group("ms"))
        now_ms = int(time.time() * 1000)
        if timestamp - now_ms > 60 * 60 * 1000:
            timestamp -= 24 * 60 * 60 * 1000
        return timestamp

    if fallback_ms > 0:
        return fallback_ms
    return int(time.time() * 1000)


def _datetime_to_ms(date_text: str, time_text: str, ms_text: str | None) -> int:
    try:
        base = datetime.strptime(f"{date_text} {time_text}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return int(time.time() * 1000)
    millis = _parse_ms(ms_text)
    return int(base.timestamp() * 1000) + millis


def _date_time_to_ms(base_date: date, time_text: str, ms_text: str | None) -> int:
    try:
        clock = datetime.strptime(time_text, "%H:%M:%S").time()
    except ValueError:
        return int(time.time() * 1000)
    return int(datetime.combine(base_date, clock).timestamp() * 1000) + _parse_ms(ms_text)


def _parse_ms(value: str | None) -> int:
    if not value:
        return 0
    return int((value + "000")[:3])


def _infer_file_date(path: Path) -> date | None:
    return _date_from_filename(path) or _date_from_mtime(path)


def _date_from_filename(path: Path) -> date | None:
    match = _ARCHIVE_DATE_RE.search(path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group("date"), "%Y-%m-%d").date()
    except ValueError:
        return None


def _date_from_mtime(path: Path) -> date | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).date()
    except OSError:
        return date.today()


def _detect_level(line: str) -> str:
    match = _LEVEL_RE.search(line)
    if match:
        level = match.group("level").upper()
        return "WARN" if level == "WARNING" else level
    lowered = line.lower()
    if any(word in lowered for word in ("fatal", "severe", "error", "exception")):
        return "ERROR"
    if any(word in lowered for word in ("warn", "warning", "failed", "timeout")):
        return "WARN"
    return "INFO"


# --- OpenTelemetry Logs Data Model 映射 ---------------------------------
# OTel SeverityNumber: TRACE=1, DEBUG=5, INFO=9, WARN=13, ERROR=17, FATAL=21
# https://opentelemetry.io/docs/specs/otel/logs/data-model/
OTEL_SEVERITY_NUMBER = {
    "TRACE": 1,
    "DEBUG": 5,
    "INFO": 9,
    "WARN": 13,
    "WARNING": 13,
    "ERROR": 17,
    "FATAL": 21,
    "SEVERE": 21,
}


def _severity_number(level: str) -> int:
    """把 MC 日志级别映射为 OTel SeverityNumber（默认 INFO=9）。"""
    return OTEL_SEVERITY_NUMBER.get(level.upper(), 9)


def _otel_fields(
    timestamp_ms: int,
    observed_ms: int,
    level: str,
    body: str,
    event_name: str,
    server_id: str,
    server_type: str,
    server_name: str,
    log_file: str,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构建 OpenTelemetry Logs Data Model 风格的结构化字段。

    把 MineSentinel 内部观察映射到 OTel 标准，使日志可被 OTel-compatible
    工具（Collector / Loki / Tempo / Datadog）明确消费，也为后续 LLM
    证据检索提供统一字段名。

    https://opentelemetry.io/docs/specs/otel/logs/data-model/
    """
    attrs: dict[str, Any] = {
        "log.file.name": Path(log_file).name if log_file else "",
        "log.file.path": log_file,
    }
    if attributes:
        attrs.update(attributes)
    return {
        "timestamp": timestamp_ms,
        "observedTimestamp": observed_ms,
        "severityText": level,
        "severityNumber": _severity_number(level),
        "body": body,
        "eventName": event_name,
        "resource": {
            "service.name": server_id,
            "service.namespace": server_type,
            "host.name": server_name,
        },
        "attributes": attrs,
    }


def _fingerprint(line: str) -> str:
    text = _sanitize_line(line).lower()
    text = _PREFIX_RE.sub("", text)
    text = _FULL_TS_RE.sub("", text)
    text = _TIME_RE.sub("", text)
    text = _UUID_RE.sub("<uuid>", text)
    text = _IPV4_RE.sub("<ip>", text)
    text = _HEX_RE.sub("0x<num>", text)
    text = _NUMBER_RE.sub("<num>", text)
    text = _SPACE_RE.sub(" ", text).strip()
    if not text:
        text = "empty"
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:24]


def _sanitize_line(line: str) -> str:
    text = _ANSI_RE.sub("", str(line or ""))
    text = _IPV4_RE.sub("<ip>", text)
    return text.strip()


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _as_millis(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _truncate(value: str, max_length: int) -> str:
    if max_length <= 0:
        return ""
    if len(value) <= max_length:
        return value
    if max_length <= 3:
        return value[:max_length]
    return value[: max_length - 3] + "..."
