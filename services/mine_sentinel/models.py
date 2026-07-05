"""MineSentinel models and configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_REPORT_INTERVAL_HOURS = 8
DEFAULT_REPORT_INTERVAL_MINUTES = DEFAULT_REPORT_INTERVAL_HOURS * 60


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _positive_int(value: Any, default: int) -> int:
    return max(1, _as_int(value, default))


def _nonnegative_int(value: Any, default: int) -> int:
    return max(0, _as_int(value, default))


def _report_interval_minutes(report_data: dict[str, Any]) -> int:
    if "interval_hours" in report_data and report_data.get("interval_hours") not in (
        None,
        "",
    ):
        hours = _as_float(report_data.get("interval_hours"), DEFAULT_REPORT_INTERVAL_HOURS)
        return max(1, int(round(hours * 60)))
    return _positive_int(
        report_data.get("interval_minutes"),
        DEFAULT_REPORT_INTERVAL_MINUTES,
    )


@dataclass
class MineSentinelReportConfig:
    default_window_minutes: int = DEFAULT_REPORT_INTERVAL_MINUTES
    send_to_target_sessions: bool = True
    delivery_targets: list[Any] = field(default_factory=list)
    include_evidence_samples: bool = True
    max_evidence_samples: int = 5
    provider_id: str = ""
    enabled: bool = True
    interval_minutes: int = DEFAULT_REPORT_INTERVAL_MINUTES
    cooldown_seconds: int = 600
    max_ai_records: int = 120
    max_records_in_memory: int = 50000
    max_ai_prompt_chars: int = 100000
    max_ai_content_length: int = 240
    send_full_log_file: bool = True
    send_as_image: bool = True
    # PR9: 导出附件优化——压缩格式 + 同窗口复用
    export_format: str = "jsonl"  # "jsonl" | "jsonl.gz"
    export_reuse_existing: bool = True


@dataclass
class MineSentinelAlertConfig:
    enabled: bool = False
    min_severity: str = "high"
    cooldown_seconds: int = 600
    min_evidence_count: int = 3
    window_minutes: int = 30
    analysis_interval_seconds: int = 60


@dataclass
class MineSentinelHourlySummaryConfig:
    """Hourly summary mode: read logs once per hour, summarize, and integrate every N hours.

    When enabled, the runtime log tailer's polling loop is skipped; instead a scheduled
    job reads the logs of each past hour directly from the logs/ directory at the start
    of every hour, builds an hourly summary, and after `hours_per_cycle` summaries
    integrates them into a single cycle report for delivery.
    """

    enabled: bool = False
    hours_per_cycle: int = 8
    window_minutes: int = 60
    poll_enabled: bool = False  # 是否同时启用实时轮询（默认关闭，纯按小时读取）
    provider_id: str = ""
    max_records_per_hour: int = 5000
    max_log_lines_per_hour: int = 20000
    retention_cycles: int = 2  # 磁盘上保留多少个历史周期的 hourly summary


@dataclass
class MineSentinelStorageConfig:
    enabled: bool = True
    retention_minutes: int = DEFAULT_REPORT_INTERVAL_MINUTES
    cleanup_interval_seconds: int = 300
    include_raw: bool = False
    max_content_length: int = 4000
    dedupe_memory_limit: int = 100000


@dataclass
class MineSentinelLogSourceConfig:
    server_id: str = ""
    server_name: str = ""
    server_type: str = "minecraft"  # minecraft | velocity
    root: str = ""
    logs_dir: str = ""
    log_file: str = ""
    target_sessions: list[Any] = field(default_factory=list)
    delivery_targets: list[Any] = field(default_factory=list)
    enabled: bool = True


@dataclass
class MineSentinelRuntimeLogConfig:
    enabled: bool = True
    sources: list[MineSentinelLogSourceConfig] = field(default_factory=list)
    poll_interval_seconds: int = 5
    backfill_on_start: bool = True
    backfill_window_minutes: int = DEFAULT_REPORT_INTERVAL_MINUTES
    initial_lines: int = 200
    max_backfill_files: int = 16
    max_backfill_lines: int = 50000
    max_lines_per_poll: int = 200
    max_line_length: int = 1000
    max_bytes_per_poll: int = 262144
    loop_filter_enabled: bool = True
    loop_filter_window_seconds: int = 300
    loop_summary_interval_seconds: int = 300
    # 模板/异常检测调参（PR7 暴露到 _conf_schema，便于运维调优）
    template_max_namespaces: int = 16
    anomaly_max_templates_per_server: int = 500
    anomaly_inactive_template_ttl_hours: int = 24
    anomaly_cleanup_interval: int = 200
    # PR9: 普通 INFO 降采样——高日志量服 CPU 优化
    # - template_parse_mode: "all" 全量解析 | "warn_error" 只解析 WARN+ | "interesting" 只解析 WARN+/命中关键词的 INFO
    # - anomaly_track_info: False 时普通 INFO 不进入 anomaly detector（仅 fingerprint + 简化 observation）
    template_parse_mode: str = "all"
    anomaly_track_info: bool = True
    # PR9: 专用 bounded ThreadPoolExecutor，避免和 asyncio 默认线程池争用。
    # 0 表示沿用 asyncio.to_thread（默认池），>0 表示创建独立的有界池。
    io_workers: int = 0


@dataclass(slots=True)
class MineSentinelConfig:
    enabled: bool = True
    retention_minutes: int = DEFAULT_REPORT_INTERVAL_MINUTES
    max_tags_per_record: int = 8
    max_raw_fields: int = 16
    dedupe_window_seconds: int = 120
    storage: MineSentinelStorageConfig = field(default_factory=MineSentinelStorageConfig)
    runtime_log: MineSentinelRuntimeLogConfig = field(
        default_factory=MineSentinelRuntimeLogConfig
    )
    report: MineSentinelReportConfig = field(default_factory=MineSentinelReportConfig)
    alert: MineSentinelAlertConfig = field(default_factory=MineSentinelAlertConfig)
    hourly_summary: MineSentinelHourlySummaryConfig = field(
        default_factory=MineSentinelHourlySummaryConfig
    )

    @classmethod
    def from_dict(cls, data: dict | None) -> "MineSentinelConfig":
        data = data or {}
        storage_data = data.get("storage", {}) or {}
        runtime_log_data = data.get("runtime_log", {}) or {}
        report_data = data.get("report", {}) or {}
        alert_data = data.get("alert", {}) or {}
        hourly_data = data.get("hourly_summary", {}) or {}
        interval_minutes = _report_interval_minutes(report_data)
        default_window_minutes = _positive_int(
            report_data.get("default_window_minutes"),
            interval_minutes,
        )
        retention_minutes = _positive_int(
            data.get("retention_minutes"),
            max(DEFAULT_REPORT_INTERVAL_MINUTES, default_window_minutes, interval_minutes),
        )
        return cls(
            enabled=data.get("enabled", True),
            retention_minutes=max(retention_minutes, default_window_minutes),
            max_tags_per_record=_positive_int(data.get("max_tags_per_record"), 8),
            max_raw_fields=_positive_int(data.get("max_raw_fields"), 16),
            dedupe_window_seconds=_positive_int(data.get("dedupe_window_seconds"), 120),
            storage=MineSentinelStorageConfig(
                enabled=bool(storage_data.get("enabled", True)),
                retention_minutes=max(
                    _positive_int(storage_data.get("retention_minutes"), retention_minutes),
                    default_window_minutes,
                ),
                cleanup_interval_seconds=max(
                    0,
                    _as_int(storage_data.get("cleanup_interval_seconds"), 300),
                ),
                include_raw=bool(storage_data.get("include_raw", False)),
                max_content_length=_positive_int(
                    storage_data.get("max_content_length"),
                    4000,
                ),
                dedupe_memory_limit=_positive_int(
                    storage_data.get("dedupe_memory_limit"),
                    100000,
                ),
            ),
            runtime_log=MineSentinelRuntimeLogConfig(
                enabled=_as_bool(runtime_log_data.get("enabled"), True),
                sources=_runtime_log_sources(runtime_log_data.get("sources")),
                poll_interval_seconds=_positive_int(
                    runtime_log_data.get("poll_interval_seconds"),
                    5,
                ),
                backfill_on_start=_as_bool(
                    runtime_log_data.get("backfill_on_start"),
                    True,
                ),
                backfill_window_minutes=_positive_int(
                    runtime_log_data.get("backfill_window_minutes"),
                    default_window_minutes,
                ),
                initial_lines=_nonnegative_int(
                    runtime_log_data.get("initial_lines"),
                    200,
                ),
                max_backfill_files=_positive_int(
                    runtime_log_data.get("max_backfill_files"),
                    16,
                ),
                max_backfill_lines=_positive_int(
                    runtime_log_data.get("max_backfill_lines"),
                    50000,
                ),
                max_lines_per_poll=_positive_int(
                    runtime_log_data.get("max_lines_per_poll"),
                    200,
                ),
                max_line_length=_positive_int(
                    runtime_log_data.get("max_line_length"),
                    1000,
                ),
                max_bytes_per_poll=_positive_int(
                    runtime_log_data.get("max_bytes_per_poll"),
                    262144,
                ),
                loop_filter_enabled=_as_bool(
                    runtime_log_data.get("loop_filter_enabled"),
                    True,
                ),
                loop_filter_window_seconds=_positive_int(
                    runtime_log_data.get("loop_filter_window_seconds"),
                    300,
                ),
                loop_summary_interval_seconds=_positive_int(
                    runtime_log_data.get("loop_summary_interval_seconds"),
                    300,
                ),
                template_max_namespaces=_positive_int(
                    runtime_log_data.get("template_max_namespaces"),
                    16,
                ),
                anomaly_max_templates_per_server=_positive_int(
                    runtime_log_data.get("anomaly_max_templates_per_server"),
                    500,
                ),
                anomaly_inactive_template_ttl_hours=_positive_int(
                    runtime_log_data.get("anomaly_inactive_template_ttl_hours"),
                    24,
                ),
                anomaly_cleanup_interval=_positive_int(
                    runtime_log_data.get("anomaly_cleanup_interval"),
                    200,
                ),
                template_parse_mode=str(
                    runtime_log_data.get("template_parse_mode", "all")
                ).strip().lower(),
                anomaly_track_info=_as_bool(
                    runtime_log_data.get("anomaly_track_info"),
                    True,
                ),
                io_workers=max(
                    0,
                    _as_int(runtime_log_data.get("io_workers"), 0),
                ),
            ),
            report=MineSentinelReportConfig(
                default_window_minutes=default_window_minutes,
                send_to_target_sessions=bool(report_data.get("send_to_target_sessions", True)),
                delivery_targets=_list_values(report_data.get("delivery_targets")),
                include_evidence_samples=bool(report_data.get("include_evidence_samples", True)),
                max_evidence_samples=_positive_int(report_data.get("max_evidence_samples"), 5),
                provider_id=str(report_data.get("provider_id", "")),
                enabled=bool(report_data.get("enabled", True)),
                interval_minutes=interval_minutes,
                cooldown_seconds=max(0, _as_int(report_data.get("cooldown_seconds"), 600)),
                max_ai_records=_positive_int(report_data.get("max_ai_records"), 120),
                max_records_in_memory=_positive_int(
                    report_data.get("max_records_in_memory"),
                    50000,
                ),
                max_ai_prompt_chars=_positive_int(
                    report_data.get("max_ai_prompt_chars"),
                    100000,
                ),
                max_ai_content_length=_positive_int(
                    report_data.get("max_ai_content_length"),
                    240,
                ),
                send_full_log_file=bool(report_data.get("send_full_log_file", True)),
                send_as_image=bool(report_data.get("send_as_image", True)),
                export_format=str(
                    report_data.get("export_format", "jsonl")
                ).strip().lower(),
                export_reuse_existing=bool(
                    report_data.get("export_reuse_existing", True)
                ),
            ),
            alert=MineSentinelAlertConfig(
                enabled=bool(alert_data.get("enabled", False)),
                min_severity=str(alert_data.get("min_severity", "high")),
                cooldown_seconds=max(0, _as_int(alert_data.get("cooldown_seconds"), 600)),
                min_evidence_count=_positive_int(alert_data.get("min_evidence_count"), 3),
                window_minutes=_positive_int(alert_data.get("window_minutes"), 30),
                analysis_interval_seconds=max(
                    0,
                    _as_int(alert_data.get("analysis_interval_seconds"), 60),
                ),
            ),
            hourly_summary=MineSentinelHourlySummaryConfig(
                enabled=_as_bool(hourly_data.get("enabled"), False),
                hours_per_cycle=_positive_int(hourly_data.get("hours_per_cycle"), 8),
                window_minutes=_positive_int(hourly_data.get("window_minutes"), 60),
                poll_enabled=_as_bool(hourly_data.get("poll_enabled"), False),
                provider_id=str(hourly_data.get("provider_id") or ""),
                max_records_per_hour=_positive_int(
                    hourly_data.get("max_records_per_hour"), 5000
                ),
                max_log_lines_per_hour=_positive_int(
                    hourly_data.get("max_log_lines_per_hour"), 20000
                ),
                retention_cycles=_positive_int(hourly_data.get("retention_cycles"), 2),
            ),
        )


def _runtime_log_sources(value: Any) -> list[MineSentinelLogSourceConfig]:
    if not isinstance(value, list):
        return []
    sources: list[MineSentinelLogSourceConfig] = []
    for index, item in enumerate(value):
        source = _runtime_log_source(item, index)
        if source:
            sources.append(source)
    return sources


def _runtime_log_source(item: Any, index: int) -> MineSentinelLogSourceConfig | None:
    if isinstance(item, str):
        path_text = item.strip()
        if not path_text:
            return None
        root, log_file = _split_runtime_log_path(path_text)
        return MineSentinelLogSourceConfig(
            server_id=_infer_log_source_id(root or log_file, index),
            server_name=_infer_log_source_id(root or log_file, index),
            root=root,
            log_file=log_file,
            enabled=True,
        )
    if not isinstance(item, dict):
        return None

    raw_path = str(item.get("path") or "").strip()
    root = str(item.get("root") or item.get("server_root") or "").strip()
    log_file = str(item.get("log_file") or item.get("log") or "").strip()
    logs_dir = str(item.get("logs_dir") or "").strip()
    if raw_path and not (root or log_file or logs_dir):
        root, log_file = _split_runtime_log_path(raw_path)
    if logs_dir and not log_file:
        log_file = str(Path(logs_dir) / "latest.log")

    source_hint = root or logs_dir or log_file
    server_id = str(item.get("server_id") or "").strip()
    server_name = str(item.get("server_name") or item.get("name") or "").strip()
    if not server_id:
        server_id = server_name or _infer_log_source_id(source_hint, index)
    if not server_name:
        server_name = server_id
    if not (root or log_file or logs_dir):
        return None
    server_type = str(item.get("server_type") or item.get("type") or "").strip().lower()
    if server_type not in {"minecraft", "velocity", "paper", "spigot", "purpur", "folia"}:
        server_type = "minecraft" if not server_type else "velocity" if server_type == "velocity" else "minecraft"
    if server_type in {"paper", "spigot", "purpur", "folia"}:
        server_type = "minecraft"
    target_sessions = _list_values(item.get("target_sessions"))
    delivery_targets = _list_values(item.get("delivery_targets"))
    return MineSentinelLogSourceConfig(
        server_id=server_id,
        server_name=server_name,
        server_type=server_type,
        root=root,
        logs_dir=logs_dir,
        log_file=log_file,
        target_sessions=target_sessions,
        delivery_targets=delivery_targets,
        enabled=_as_bool(item.get("enabled"), True),
    )


def _split_runtime_log_path(path_text: str) -> tuple[str, str]:
    path = Path(path_text)
    name = path.name.lower()
    if name == "latest.log" or name.endswith(".log") or name.endswith(".log.gz"):
        return "", path_text
    return path_text, ""


def _list_values(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set)):
        raw = value
    else:
        raw = [value]
    return [item for item in raw if item not in (None, "")]


def _infer_log_source_id(path_text: str, index: int) -> str:
    if not path_text:
        return f"minecraft_{index + 1}"
    path = Path(path_text)
    if path.name.lower() == "latest.log" or path.name.lower().endswith((".log", ".gz")):
        candidate = path.parent.parent.name or path.parent.name or path.stem
    else:
        candidate = path.name
    normalized = "".join(ch if ch.isalnum() else "_" for ch in candidate).strip("_")
    return normalized or f"minecraft_{index + 1}"


@dataclass(slots=True)
class ObservationRecord:
    event_id: str = ""
    kind: str = ""
    timestamp: int = 0
    server_id: str = ""
    server_name: str = ""
    backend_server: str = ""
    proxy_id: str = ""
    player_name: str = ""
    player_uuid_hash: str = ""
    content: str = ""
    tags: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], batch_server_id: str = "", batch_server_name: str = ""
    ) -> "ObservationRecord":
        player = data.get("player") or {}
        return cls(
            event_id=str(data.get("eventId") or ""),
            kind=str(data.get("kind") or ""),
            timestamp=_as_int(data.get("timestamp"), 0),
            server_id=str(data.get("serverId") or batch_server_id),
            server_name=str(data.get("serverName") or batch_server_name),
            backend_server=str(data.get("backendServer") or ""),
            proxy_id=str(data.get("proxyId") or ""),
            player_name=str(player.get("name") or ""),
            player_uuid_hash=str(player.get("uuidHash") or ""),
            content=str(data.get("content") or ""),
            tags=[str(t) for t in data.get("tags", []) if t is not None],
            context=dict(data.get("context") or {}),
            raw=dict(data.get("raw") or {}),
        )

    @property
    def identity(self) -> str:
        return self.player_uuid_hash or self.player_name

    def evidence_text(self) -> str:
        source = self.backend_server or self.server_id
        player = f"{self.player_name}: " if self.player_name else ""
        return f"[{source}] {player}{self.content}".strip()
