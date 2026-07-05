"""Local Minecraft runtime log ingestion for MineSentinel."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .models import MineSentinelLogSourceConfig, MineSentinelRuntimeLogConfig
from .template_miner import get_template_miner
from .anomaly_detector import get_anomaly_detector


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


@dataclass
class _SourceState:
    source: MineSentinelLogSourceConfig
    log_file: Path
    position: int = 0
    partial: str = ""
    last_timestamp_ms: int = 0
    missing_logged: bool = False


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
            await self._backfill_source(
                state,
                max(10, self.config.poll_interval_seconds * 3 // 60 + 1),
            )
            state.position = 0
            state.partial = ""
        if size == state.position:
            return

        lines, position, partial, dropped_count = await self.io_runner(
            _read_appended_lines,
            state.log_file,
            state.position,
            state.partial,
            self.config.max_bytes_per_poll,
            self.config.max_lines_per_poll,
            self.config.max_line_length,
        )
        state.position = position
        state.partial = partial
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
    partial: str,
    max_bytes: int,
    max_lines: int,
    max_line_length: int,
) -> tuple[list[str], int, str, int]:
    """Read appended bytes from ``path`` and split into lines.

    Returns ``(lines, new_position, next_partial, dropped_count)``.

    Burst handling: 当本轮完整行数超过 ``max_lines`` 时，**不再丢弃早期行**。
    前面 ``max_lines`` 行本轮处理，剩余完整行存入 ``next_partial`` 作为
    backlog，下一轮继续处理。只有当 backlog 累积超过 ``max_lines * 4`` 行时，
    才丢弃最旧的行并报告 ``dropped_count``。
    """
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, position))
            data = handle.read(max(1, max_bytes))
            new_position = handle.tell()
    except OSError:
        return [], position, partial, 0

    # 构建 text：backlog (partial) + 新数据
    if data:
        text = data.decode("utf-8", errors="replace")
        if partial:
            text = partial + text
    elif partial:
        # 无新数据但有 backlog —— 继续处理积压行
        text = partial
        new_position = position
    else:
        return [], new_position, partial, 0

    parts = text.splitlines(keepends=True)
    next_partial = ""
    if parts and not parts[-1].endswith(("\n", "\r")):
        next_partial = parts.pop()

    lines = [_truncate(part.rstrip("\r\n"), max_line_length) for part in parts if part.strip()]

    dropped_count = 0
    if len(lines) > max_lines:
        # Backlog: 本轮处理前 max_lines 行，剩余行推迟到下一轮
        backlog_lines = lines[max_lines:]
        lines = lines[:max_lines]

        # 防止 backlog 无限增长：超过 max_lines*4 行时丢弃最旧的
        max_backlog_lines = max(1, max_lines * 4)
        if len(backlog_lines) > max_backlog_lines:
            dropped_count = len(backlog_lines) - max_backlog_lines
            backlog_lines = backlog_lines[-max_backlog_lines:]

        # 构建 backlog 文本：完整行用 \n 连接并加尾部换行，
        # 再追加不完整的末行（如果有）
        backlog_text = "\n".join(backlog_lines)
        if backlog_text:
            backlog_text += "\n"
        if next_partial:
            backlog_text += next_partial
        next_partial = backlog_text
    elif next_partial:
        # 无溢出：裁剪过长的单行 partial
        if len(next_partial) > max_line_length * 2:
            next_partial = next_partial[-max_line_length:]

    return lines, new_position, next_partial, dropped_count


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
) -> list[dict]:
    """Read an hour of logs and turn them into observation dicts (in-memory only).

    Does not write to disk; the caller decides what to do with the result.
    """
    rows = read_hour_log_lines(
        source, hour_start_ms, hour_end_ms, max_lines=max_lines
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


def _build_observation(
    source: MineSentinelLogSourceConfig,
    log_file: Path,
    line: str,
    timestamp_ms: int,
    max_line_length: int,
) -> dict[str, Any]:
    content = _sanitize_line(_truncate(line, max_line_length))
    level = _detect_level(content)
    fingerprint = _fingerprint(content)
    # 模板解析：drain3 可用时返回 template_id，否则降级为 fingerprint
    parsed = get_template_miner().parse(content, server_id=source.server_id or "default")
    template_id = parsed.template_id
    template = parsed.template
    # 异常检测：基于模板计数突增（EWMA + 分位数）
    anomaly = get_anomaly_detector().observe(
        server_id=source.server_id or "minecraft",
        template_id=template_id,
        template=template,
        level=level,
        timestamp_ms=timestamp_ms,
    )
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
    lowered = content.lower()
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
    observed_ms = int(time.time() * 1000)
    server_id = source.server_id or "minecraft"
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
    if parsed.params:
        context["templateParams"] = parsed.params[:8]
        context["otel"]["attributes"]["template.params"] = parsed.params[:8]
    if parsed.fallback:
        context["templateFallback"] = True
        context["otel"]["attributes"]["template.fallback"] = True
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
