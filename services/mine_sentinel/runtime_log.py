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
        fingerprint = str(context.get("fingerprint") or "")
        if not fingerprint:
            return [observation]

        now_ms = _as_millis(observation.get("timestamp")) or int(time.time() * 1000)
        key = f"{observation.get('serverId') or ''}:{fingerprint}"
        entry = self._entries.get(key)
        window_ms = max(1, self.config.loop_filter_window_seconds) * 1000
        if entry is None or now_ms - entry.last_ts > window_ms:
            self._entries[key] = _LoopEntry(
                fingerprint=fingerprint,
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
        context = dict(entry.context)
        context.update(
            {
                "loopSuppressed": suppressed,
                "loopFirstTimestamp": entry.first_ts,
                "loopLastTimestamp": entry.last_ts,
            }
        )
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
            logger.info("[MineSentinel] runtime log ingestion has no configured sources")
            return
        self._stopping.clear()
        for source in sources:
            log_file = _resolve_log_file(source)
            if log_file is None:
                continue
            self._tasks.append(asyncio.create_task(self._run_source(source, log_file)))
        logger.info(f"[MineSentinel] runtime log ingestion started: {len(self._tasks)} source(s)")

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
        state = _SourceState(source=source, log_file=log_file)
        try:
            if self.config.backfill_on_start:
                await self._backfill_source(state, self.config.backfill_window_minutes)
            elif self.config.initial_lines:
                await self._emit_initial_tail(state)
            state.position = await self.io_runner(_file_size, state.log_file) or 0
            while not self._stopping.is_set():
                await asyncio.sleep(max(1, self.config.poll_interval_seconds))
                await self._poll_source(state)
                await self._emit_observations(self.loop_filter.drain_due(force=False))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                f"[MineSentinel] runtime log source {source.server_id} stopped: {exc}"
            )

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

        lines, position, partial = await self.io_runner(
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
    if source.root:
        return Path(source.root).expanduser() / "logs" / "latest.log"
    return None


def _logs_dir(source: MineSentinelLogSourceConfig) -> Path | None:
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
) -> tuple[list[str], int, str]:
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, position))
            data = handle.read(max(1, max_bytes))
            new_position = handle.tell()
    except OSError:
        return [], position, partial
    if not data:
        return [], new_position, partial
    text = data.decode("utf-8", errors="replace")
    if partial:
        text = partial + text
    parts = text.splitlines(keepends=True)
    next_partial = ""
    if parts and not parts[-1].endswith(("\n", "\r")):
        next_partial = parts.pop()
    if len(next_partial) > max_line_length * 2:
        next_partial = next_partial[-max_line_length:]
    lines = [_truncate(part.rstrip("\r\n"), max_line_length) for part in parts if part.strip()]
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return lines, new_position, next_partial


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
    digest = hashlib.sha1(
        f"{source.server_id}:{timestamp_ms}:{fingerprint}:{log_file.name}".encode("utf-8")
    ).hexdigest()[:20]
    tags = ["server_log", "runtime_log", level.lower()]
    lowered = content.lower()
    if "exception" in lowered:
        tags.append("exception")
    if level in {"ERROR", "FATAL", "SEVERE"}:
        tags.append("error")
    elif level in {"WARN", "WARNING"}:
        tags.append("warning")
    return {
        "eventId": f"local-log:{source.server_id}:{digest}",
        "kind": "SERVER_LOG",
        "timestamp": timestamp_ms,
        "serverId": source.server_id or "minecraft",
        "serverName": source.server_name or source.server_id or "Minecraft",
        "content": content,
        "tags": tags,
        "context": {
            "source": "astrbot_runtime_log",
            "logFile": str(log_file),
            "level": level,
            "fingerprint": fingerprint,
            "compressed": log_file.name.lower().endswith(".gz"),
        },
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
