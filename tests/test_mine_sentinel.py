from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import re
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


astrbot = sys.modules.get("astrbot") or types.ModuleType("astrbot")
api = sys.modules.get("astrbot.api") or types.ModuleType("astrbot.api")
api.logger = getattr(api, "logger", _Logger())
sys.modules.update({"astrbot": astrbot, "astrbot.api": api})

if "mine_sentinel_rs" not in sys.modules:
    _STUB_WS_RE = re.compile(r"\s+")

    native_stub = types.ModuleType("mine_sentinel_rs")

    class _ObservationRecordCodec:
        """纯 Python 模拟 mine_sentinel_rs.ObservationRecordCodec。

        镜像 Rust 扩展的 4 个方法（normalize_record / record_to_json /
        json_line / dedupe_key），使测试在不编译 Rust wheel 时也能跑通
        "Rust 路径"的代码分支。
        """

        def __init__(
            self,
            max_content_length,
            max_tags_per_record,
            max_raw_fields,
            include_raw,
            dedupe_window_seconds,
        ):
            self.max_content_length = int(max_content_length)
            self.max_tags_per_record = int(max_tags_per_record)
            self.max_raw_fields = int(max_raw_fields)
            self.include_raw = bool(include_raw)
            self.dedupe_window_seconds = max(1, int(dedupe_window_seconds))

        @staticmethod
        def _truncate(value, max_length):
            if max_length <= 0:
                return ""
            if len(value) <= max_length:
                return value
            if max_length <= 3:
                return value[:max_length]
            return value[: max_length - 3] + "..."

        def _compact_value(self, value):
            if value is None or isinstance(value, (bool, int, float)):
                return value
            if isinstance(value, str):
                return self._truncate(value, self.max_content_length)
            if isinstance(value, dict):
                return self._compact_dict(value, self.max_raw_fields)
            if isinstance(value, list):
                return [self._compact_value(v) for v in value[: self.max_raw_fields]]
            try:
                text = json.dumps(value, ensure_ascii=False, default=str)
            except Exception:
                text = str(value)
            return self._truncate(text, self.max_content_length)

        def _compact_dict(self, data, max_fields):
            compact = {}
            for index, (key, value) in enumerate((data or {}).items()):
                if index >= max_fields:
                    break
                compact[str(key)] = self._compact_value(value)
            return compact

        def normalize_record(self, record):
            record.content = self._truncate(record.content, self.max_content_length)
            record.tags = [
                self._truncate(str(tag), self.max_content_length)
                for tag in record.tags[: self.max_tags_per_record]
            ]
            record.context = self._compact_dict(record.context, self.max_raw_fields)
            record.raw = (
                self._compact_dict(record.raw, self.max_raw_fields)
                if self.include_raw
                else {}
            )

        def record_to_json(self, record):
            return {
                "eventId": record.event_id,
                "kind": record.kind,
                "timestamp": record.timestamp,
                "serverId": record.server_id,
                "serverName": record.server_name,
                "backendServer": record.backend_server,
                "proxyId": record.proxy_id,
                "player": {
                    "name": record.player_name,
                    "uuidHash": record.player_uuid_hash,
                },
                "content": record.content,
                "tags": record.tags,
                "context": record.context,
                "raw": record.raw if self.include_raw else {},
            }

        def json_line(self, record):
            return json.dumps(
                self.record_to_json(record),
                ensure_ascii=False,
                separators=(",", ":"),
            )

        def dedupe_key(self, record):
            if record.event_id:
                return record.event_id
            identity = record.identity or ""
            content_lower = _STUB_WS_RE.sub(" ", record.content.lower()).strip()
            bucket = int(record.timestamp or 0) // (self.dedupe_window_seconds * 1000)
            raw = f"{record.kind}|{record.server_id}|{identity}|{content_lower}|{bucket}"
            digest = hashlib.blake2b(raw.encode("utf-8"), digest_size=16).hexdigest()
            return f"h:{digest}"

    def _observation_priority_score(record, matcher=None):
        if record.kind != "SERVER_LOG":
            return 0.0
        text = f"{record.content} {' '.join(record.tags)}".lower()
        if any(
            marker in text
            for marker in (
                "loop_suppressed",
                "fatal",
                "severe",
                "error",
                "exception",
                "failed",
                "timeout",
                "warn",
                "warning",
                "ban",
                "kick",
                "mute",
                "report",
                "spam",
                "grief",
                "cheat",
            )
        ):
            return 5.0
        return 1.0

    native_stub.ObservationRecordCodec = _ObservationRecordCodec
    native_stub.observation_priority_score = _observation_priority_score
    sys.modules["mine_sentinel_rs"] = native_stub

from services.mine_sentinel.models import MineSentinelConfig, ObservationRecord
from services.mine_sentinel.reporting.rules import HeuristicReportBuilder
from services.mine_sentinel.runtime_log import (
    MineSentinelRuntimeLogTailer,
    _build_observation,
    _read_appended_lines,
    _resolve_log_file,
    _logs_dir,
    build_hour_observations,
    read_hour_log_lines,
)
from services.mine_sentinel.storage import DiskObservationStore
from services.mine_sentinel.storage.offset_index import JsonlOffsetIndex
from services.mine_sentinel.hourly_summary import (
    HourlySummary,
    HourlySummaryStore,
    HourlySummarizer,
    format_cycle_report,
)
from services.mine_sentinel.jobs import HourlySummaryJob
from handlers.mine_sentinel_commands import parse_report_args, parse_window_minutes


class MineSentinelRuntimeLogAuditTests(unittest.TestCase):
    def test_report_command_window_parsing(self):
        self.assertEqual(parse_window_minutes("8h"), 480)
        self.assertEqual(parse_window_minutes("30m"), 30)
        self.assertEqual(parse_window_minutes("15min"), 15)
        self.assertIsNone(parse_window_minutes("survival"))

        target = parse_report_args(["survival", "8h"])

        self.assertEqual(target.server_id, "survival")
        self.assertEqual(target.window_minutes, 480)

    def test_runtime_log_config_parses_root_source(self):
        config = MineSentinelConfig.from_dict(
            {
                "runtime_log": {
                    "sources": [
                        {
                            "server_id": "survival",
                            "server_name": "Survival",
                            "root": "D:\\minecraftserver",
                        }
                    ],
                    "backfill_window_minutes": 480,
                    "loop_filter_enabled": True,
                }
            }
        )

        self.assertTrue(config.runtime_log.enabled)
        self.assertEqual(config.runtime_log.sources[0].server_id, "survival")
        self.assertEqual(config.runtime_log.sources[0].root, "D:\\minecraftserver")
        self.assertEqual(config.runtime_log.backfill_window_minutes, 480)
        self.assertTrue(config.runtime_log.loop_filter_enabled)

    def test_store_accepts_server_logs_only(self):
        config = MineSentinelConfig.from_dict({})
        now = int(time.time() * 1000)
        payload = {
            "serverId": "survival",
            "observations": [
                {
                    "eventId": "chat-1",
                    "kind": "CHAT",
                    "timestamp": now,
                    "serverId": "survival",
                    "player": {"name": "Alice", "uuidHash": "uuid-Alice"},
                    "content": "hello",
                },
                {
                    "eventId": "non-log-1",
                    "kind": "PLAYER_EVENT",
                    "timestamp": now,
                    "serverId": "survival",
                    "content": "Steve joined the game",
                },
                {
                    "eventId": "log-1",
                    "kind": "SERVER_LOG",
                    "timestamp": now,
                    "serverId": "survival",
                    "content": "[Server thread/ERROR]: Test plugin failed",
                    "tags": ["server_log", "error"],
                    "context": {"level": "ERROR"},
                },
            ],
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            store = DiskObservationStore(config, Path(tmp_dir))
            written = store.add_batch("survival", payload)
            records = store.recent(60, "survival")

        self.assertEqual(written, 1)
        self.assertEqual([record.kind for record in records], ["SERVER_LOG"])

    def test_backfill_reads_compressed_logs_and_filters_error_loop(self):
        asyncio.run(self._run_backfill_flow())

    async def _run_backfill_flow(self):
        _install_astrbot_stubs()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "server"
            logs_dir = root / "logs"
            logs_dir.mkdir(parents=True)

            now = time.localtime()
            date_text = time.strftime("%Y-%m-%d", now)
            time_text = time.strftime("%H:%M:%S", now)
            archive = logs_dir / f"{date_text}-1.log.gz"
            repeated = [
                f"[{time_text} ERROR]: Failed to tick plugin Example id={idx}"
                for idx in range(8)
            ]
            with gzip.open(archive, "wt", encoding="utf-8") as handle:
                handle.write("\n".join(repeated))
                handle.write("\n")
            (logs_dir / "latest.log").write_text(
                f"[{time_text} INFO]: Server started\n",
                encoding="utf-8",
            )

            config = MineSentinelConfig.from_dict(
                {
                    "runtime_log": {
                        "sources": [
                            {
                                "server_id": "survival",
                                "server_name": "Survival",
                                "root": str(root),
                            }
                        ],
                        "backfill_window_minutes": 480,
                        "loop_filter_window_seconds": 300,
                        "loop_summary_interval_seconds": 1,
                    }
                }
            )
            batches = []

            async def handle_batch(server_id, payload):
                batches.append((server_id, payload))

            tailer = MineSentinelRuntimeLogTailer(
                config.runtime_log,
                handle_batch,
                io_runner=_run_sync,
            )
            state = types.SimpleNamespace(source=config.runtime_log.sources[0])
            await tailer._backfill_source(state, config.runtime_log.backfill_window_minutes)

            observations = [
                item
                for _server_id, payload in batches
                for item in payload.get("observations", [])
            ]

        self.assertTrue(observations)
        self.assertTrue(any(item["kind"] == "SERVER_LOG" for item in observations))
        self.assertTrue(any(item["context"]["compressed"] for item in observations))
        self.assertTrue(
            any("loop_suppressed" in item.get("tags", []) for item in observations)
        )
        self.assertLess(len(observations), len(repeated))

    def test_report_builder_summarizes_server_log_errors(self):
        now = int(time.time() * 1000)
        records = [
            ObservationRecord(
                event_id="log-1",
                kind="SERVER_LOG",
                timestamp=now,
                server_id="survival",
                server_name="Survival",
                content="[Server thread/ERROR]: Failed to load datapack",
                tags=["server_log", "runtime_log", "error"],
                context={"level": "ERROR"},
            ),
            ObservationRecord(
                event_id="log-2",
                kind="SERVER_LOG",
                timestamp=now + 1000,
                server_id="survival",
                server_name="Survival",
                content="同类服务器报错已合并：7 条重复日志被过滤；首条样本：Failed to load datapack",
                tags=["server_log", "runtime_log", "loop_suppressed", "error"],
                context={"level": "ERROR", "loopSuppressed": 7},
            ),
        ]

        report = HeuristicReportBuilder(MineSentinelConfig.from_dict({})).build(
            records,
            480,
            "survival",
        )

        self.assertEqual(report["log_count"], 2)
        self.assertTrue(any(issue["category"] == "bug" for issue in report["issues"]))
        self.assertTrue(any("重复服务器报错循环日志" in note for note in report["ops_notes"]))

    def test_report_builder_splits_community_management(self):
        now = int(time.time() * 1000)
        records = [
            ObservationRecord(
                event_id="log-community",
                kind="SERVER_LOG",
                timestamp=now,
                server_id="survival",
                server_name="Survival",
                content="[Server thread/WARN]: Player Steve was muted for spam",
                tags=["server_log", "runtime_log", "warning"],
                context={"level": "WARN"},
            )
        ]

        report = HeuristicReportBuilder(MineSentinelConfig.from_dict({})).build(
            records,
            480,
            "survival",
        )

        self.assertTrue(report["categories"]["community"])
        self.assertTrue(any(issue["category"] == "community" for issue in report["issues"]))

    def test_log_source_supports_logs_dir_and_server_type(self):
        config = MineSentinelConfig.from_dict(
            {
                "runtime_log": {
                    "sources": [
                        {
                            "server_id": "proxy",
                            "server_name": "Velocity 代理",
                            "server_type": "velocity",
                            "logs_dir": "/opt/velocity/logs",
                        },
                        {
                            "server_id": "survival",
                            "server_type": "paper",
                            "root": "/opt/paper",
                        },
                        {
                            "server_id": "creative",
                            "log_file": "/opt/creative/logs/latest.log",
                        },
                    ]
                }
            }
        )

        sources = config.runtime_log.sources
        self.assertEqual(len(sources), 3)

        proxy = sources[0]
        self.assertEqual(proxy.server_type, "velocity")
        self.assertEqual(proxy.logs_dir, "/opt/velocity/logs")
        self.assertEqual(
            _resolve_log_file(proxy),
            Path("/opt/velocity/logs/latest.log"),
        )
        self.assertEqual(_logs_dir(proxy), Path("/opt/velocity/logs"))

        survival = sources[1]
        self.assertEqual(survival.server_type, "minecraft")  # paper 归一为 minecraft
        self.assertEqual(
            _resolve_log_file(survival),
            Path("/opt/paper/logs/latest.log"),
        )

        creative = sources[2]
        self.assertEqual(creative.server_type, "minecraft")
        self.assertEqual(
            _resolve_log_file(creative),
            Path("/opt/creative/logs/latest.log"),
        )

    def test_resolve_log_file_prefers_log_file_over_logs_dir_and_root(self):
        from services.mine_sentinel.models import MineSentinelLogSourceConfig

        source = MineSentinelLogSourceConfig(
            root="/opt/paper",
            logs_dir="/var/log/paper",
            log_file="/tmp/explicit.log",
        )
        self.assertEqual(_resolve_log_file(source), Path("/tmp/explicit.log"))
        self.assertEqual(_logs_dir(source), Path("/var/log/paper"))

        source_no_logfile = MineSentinelLogSourceConfig(
            root="/opt/paper",
            logs_dir="/var/log/paper",
        )
        self.assertEqual(
            _resolve_log_file(source_no_logfile),
            Path("/var/log/paper/latest.log"),
        )

        source_only_root = MineSentinelLogSourceConfig(root="/opt/paper")
        self.assertEqual(
            _resolve_log_file(source_only_root),
            Path("/opt/paper/logs/latest.log"),
        )

    def test_build_observation_marks_velocity_proxy_tag(self):
        from services.mine_sentinel.models import MineSentinelLogSourceConfig

        proxy_source = MineSentinelLogSourceConfig(
            server_id="proxy",
            server_name="Velocity",
            server_type="velocity",
        )
        observation = _build_observation(
            proxy_source,
            Path("/opt/velocity/logs/latest.log"),
            "[12:00:00 INFO]: [connected player] Bob -> survival",
            1700000000000,
            1000,
        )
        self.assertIn("velocity", observation["tags"])
        self.assertIn("proxy", observation["tags"])
        self.assertEqual(observation["context"]["serverType"], "velocity")

        mc_source = MineSentinelLogSourceConfig(
            server_id="survival",
            server_name="Survival",
            server_type="minecraft",
        )
        observation_mc = _build_observation(
            mc_source,
            Path("/opt/paper/logs/latest.log"),
            "[12:00:00 INFO]: Done!",
            1700000000000,
            1000,
        )
        self.assertIn("minecraft", observation_mc["tags"])
        self.assertNotIn("velocity", observation_mc["tags"])
        self.assertEqual(observation_mc["context"]["serverType"], "minecraft")

    def test_start_warns_when_no_sources_configured(self):
        captured = []

        class CaptureLogger:
            def warning(self, msg, *args, **kwargs):
                captured.append(msg)

            def info(self, *args, **kwargs):
                pass

            def error(self, *args, **kwargs):
                pass

            def debug(self, *args, **kwargs):
                pass

        import services.mine_sentinel.runtime_log as runtime_log_module

        original_logger = runtime_log_module.logger
        runtime_log_module.logger = CaptureLogger()
        try:
            config = MineSentinelConfig.from_dict({"runtime_log": {"enabled": True}})
            tailer = MineSentinelRuntimeLogTailer(
                config.runtime_log,
                batch_handler=lambda *a, **kw: None,
                io_runner=_run_sync,
            )
            tailer.start()
        finally:
            runtime_log_module.logger = original_logger

        self.assertTrue(captured)
        self.assertTrue(
            any("未配置任何 Minecraft 运行日志源" in msg for msg in captured),
            f"expected no-sources warning, got: {captured}",
        )

    def test_read_appended_lines_backlogs_instead_of_dropping(self):
        """burst 超过 max_lines 时应把剩余行存入 backlog 而非丢弃。"""
        from collections import deque
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "burst.log"
            # 写入 10 行，max_lines=3 → 本轮处理前 3 行，剩余 7 行存入 backlog
            path.write_text("\n".join(f"line {i}" for i in range(10)) + "\n")
            lines, position, partial_line, backlog, dropped = _read_appended_lines(
                path, 0, b"", deque(), max_bytes=65536, max_lines=3, max_line_length=1000
            )
            self.assertEqual(len(lines), 3)
            self.assertEqual(dropped, 0)  # 不丢弃，推迟到下一轮
            self.assertEqual(len(backlog), 7)  # backlog 有 7 行
            self.assertEqual(partial_line, b"")  # 无未闭合行
            self.assertGreater(position, 0)
            # 本轮处理前 3 行
            self.assertEqual(lines[0], "line 0")
            self.assertEqual(lines[2], "line 2")
            # backlog 中包含剩余 7 行（按顺序）
            self.assertEqual(list(backlog), [f"line {i}" for i in range(3, 10)])

    def test_read_appended_lines_backlog_processed_next_poll(self):
        """backlog 应在下一轮被处理，而非永久丢失。"""
        from collections import deque
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "burst.log"
            path.write_text("\n".join(f"line {i}" for i in range(10)) + "\n")
            # 第一轮：处理前 3 行，剩余 7 行存入 backlog
            lines1, position, partial1, backlog1, dropped1 = _read_appended_lines(
                path, 0, b"", deque(), max_bytes=65536, max_lines=3, max_line_length=1000
            )
            self.assertEqual(len(lines1), 3)
            self.assertEqual(dropped1, 0)
            self.assertEqual(len(backlog1), 7)
            # 第二轮：position 已到文件末尾，但 backlog 仍有数据
            lines2, position2, partial2, backlog2, dropped2 = _read_appended_lines(
                path, position, partial1, backlog1, max_bytes=65536, max_lines=3, max_line_length=1000
            )
            # backlog 中的前 3 行被处理
            self.assertEqual(len(lines2), 3)
            self.assertEqual(dropped2, 0)
            self.assertEqual(lines2[0], "line 3")
            self.assertEqual(len(backlog2), 4)
            # 第三轮：处理剩余 4 行中的前 3 行
            lines3, position3, partial3, backlog3, dropped3 = _read_appended_lines(
                path, position2, partial2, backlog2, max_bytes=65536, max_lines=3, max_line_length=1000
            )
            self.assertEqual(len(lines3), 3)
            self.assertEqual(lines3[0], "line 6")
            self.assertEqual(len(backlog3), 1)
            # 第四轮：处理最后 1 行
            lines4, position4, partial4, backlog4, dropped4 = _read_appended_lines(
                path, position3, partial3, backlog3, max_bytes=65536, max_lines=3, max_line_length=1000
            )
            self.assertEqual(len(lines4), 1)
            self.assertEqual(lines4[0], "line 9")
            self.assertEqual(len(backlog4), 0)
            self.assertEqual(partial4, b"")

    def test_read_appended_lines_drops_when_backlog_exceeds_limit(self):
        """backlog 超过 max_lines*4 时才丢弃最旧的行。"""
        from collections import deque
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "huge_burst.log"
            # max_lines=3 → backlog 上限 12 行；写 20 行
            path.write_text("\n".join(f"line {i}" for i in range(20)) + "\n")
            lines, position, partial_line, backlog, dropped = _read_appended_lines(
                path, 0, b"", deque(), max_bytes=65536, max_lines=3, max_line_length=1000
            )
            self.assertEqual(len(lines), 3)
            # 20 - 3(processed) - 12(backlog) = 5 dropped
            self.assertEqual(dropped, 5)
            # backlog 中保留最后 12 行 (line 8 ~ line 19)
            backlog_list = list(backlog)
            self.assertEqual(len(backlog_list), 12)
            self.assertEqual(backlog_list[0], "line 8")
            self.assertEqual(backlog_list[-1], "line 19")
            self.assertNotIn("line 7", backlog_list)

    def test_read_appended_lines_no_drop_when_under_limit(self):
        from collections import deque
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ok.log"
            path.write_text("a\nb\n")
            lines, _position, partial_line, backlog, dropped = _read_appended_lines(
                path, 0, b"", deque(), max_bytes=65536, max_lines=10, max_line_length=1000
            )
            self.assertEqual(lines, ["a", "b"])
            self.assertEqual(dropped, 0)
            self.assertEqual(len(backlog), 0)
            self.assertEqual(partial_line, b"")

    def test_read_appended_lines_preserves_partial_line_across_polls(self):
        """未闭合的 partial_line 应跨轮保留，与文件新数据拼接。"""
        from collections import deque
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "partial.log"
            # 写入不完整的一行（无换行符）
            path.write_text("incomplete line without newline")
            lines1, position1, partial1, backlog1, dropped1 = _read_appended_lines(
                path, 0, b"", deque(), max_bytes=65536, max_lines=10, max_line_length=1000
            )
            # 无完整行，全部进 partial_line
            self.assertEqual(len(lines1), 0)
            self.assertEqual(partial1, b"incomplete line without newline")
            self.assertEqual(len(backlog1), 0)
            # 追加换行符使之成为完整行
            with path.open("a") as f:
                f.write("\nsecond line\n")
            lines2, position2, partial2, backlog2, dropped2 = _read_appended_lines(
                path, position1, partial1, backlog1, max_bytes=65536, max_lines=10, max_line_length=1000
            )
            self.assertEqual(lines2, ["incomplete line without newline", "second line"])
            self.assertEqual(partial2, b"")
            self.assertEqual(len(backlog2), 0)

    def test_read_appended_lines_preserves_partial_line_across_polls(self):
        """未闭合的 partial_line 应跨轮保留，与文件新数据拼接。"""
        from collections import deque
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "partial.log"
            # 写入不完整的一行（无换行符）
            path.write_text("incomplete line without newline")
            lines1, position1, partial1, backlog1, dropped1 = _read_appended_lines(
                path, 0, b"", deque(), max_bytes=65536, max_lines=10, max_line_length=1000
            )
            # 无完整行，全部进 partial_line
            self.assertEqual(len(lines1), 0)
            self.assertEqual(partial1, b"incomplete line without newline")
            self.assertEqual(len(backlog1), 0)
            # 追加换行符使之成为完整行
            with path.open("a") as f:
                f.write("\nsecond line\n")
            lines2, position2, partial2, backlog2, dropped2 = _read_appended_lines(
                path, position1, partial1, backlog1, max_bytes=65536, max_lines=10, max_line_length=1000
            )
            self.assertEqual(lines2, ["incomplete line without newline", "second line"])
            self.assertEqual(partial2, b"")
            self.assertEqual(len(backlog2), 0)

    def test_read_appended_lines_handles_utf8_multibyte_split_across_polls(self):
        """PR9 hotfix v5: 中文 UTF-8 多字节字符被 max_bytes 切断时，
        partial_line 应保留未消费的 bytes，下一轮拼接后正确解码，
        不应出现 U+FFFD 替换字符污染日志证据。"""
        from collections import deque
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chinese.log"
            # 一行中文日志，UTF-8 编码后每字 3 字节。
            # "玩家进入服务器：EllanServer" = 11 个中文字符 + 半角 ASCII
            full_line = "玩家进入服务器：EllanServer\n"
            full_bytes = full_line.encode("utf-8")
            # 切断在第 5 个中文字符之后（"玩家进入服" = 15 bytes），
            # 切断点正好在多字节字符边界前（第 5 个字符完整，第 6 个字符还没开始）
            # 改成切断在第 5 个字符中间：切在 byte 14（第 5 个字符的第 2 字节）
            cut_at = 14  # 切在 "务" 字的中间（bytes 15-17 是 "务"）
            first_chunk = full_bytes[:cut_at]
            rest_chunk = full_bytes[cut_at:]
            # 写入第一段（无换行，且切断在多字节字符中间）
            with path.open("wb") as f:
                f.write(first_chunk)
            # 第一轮：读到切断的 bytes，partial_line 应保留未消费的尾部
            lines1, position1, partial1, backlog1, dropped1 = _read_appended_lines(
                path, 0, b"", deque(), max_bytes=65536, max_lines=10, max_line_length=1000
            )
            # 无完整行（无换行），lines 应为空
            self.assertEqual(len(lines1), 0)
            # partial1 应是非空 bytes（包含已解码的 "玩家进入" + 切断的 "服" 字前 2 bytes）
            self.assertGreater(len(partial1), 0)
            self.assertEqual(len(backlog1), 0)
            # 追加剩余 bytes（含换行符）
            with path.open("ab") as f:
                f.write(rest_chunk)
            # 第二轮：partial1 与新 bytes 拼接，应正确解码出完整中文行
            lines2, position2, partial2, backlog2, dropped2 = _read_appended_lines(
                path, position1, partial1, backlog1, max_bytes=65536, max_lines=10, max_line_length=1000
            )
            # 应该解码出完整的中文行，无 U+FFFD
            self.assertEqual(len(lines2), 1)
            self.assertEqual(lines2[0], "玩家进入服务器：EllanServer")
            self.assertEqual(partial2, b"")
            self.assertEqual(len(backlog2), 0)

    def test_build_hour_observations_respects_max_line_length(self):
        """build_hour_observations 应当用 max_line_length 裁剪超长行，而不是把 hour_start_ms 当长度。"""
        from datetime import datetime, timedelta
        from services.mine_sentinel.models import MineSentinelLogSourceConfig

        now = datetime.now()
        cur_hour = now.replace(minute=0, second=0, microsecond=0)
        prev_hour = cur_hour - timedelta(hours=1)
        hour_start_ms = int(prev_hour.timestamp() * 1000)
        hour_end_ms = int(cur_hour.timestamp() * 1000)
        long_payload = "X" * 5000
        line = f"[{prev_hour:%H:%M:%S}] [Server thread/INFO]: {long_payload}"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            logs_dir = tmp_path / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            (logs_dir / "latest.log").write_text(line + "\n", encoding="utf-8")
            source = MineSentinelLogSourceConfig(
                server_id="srv", server_name="Srv",
                server_type="minecraft", root=str(tmp_path),
            )
            observations = build_hour_observations(
                source, hour_start_ms, hour_end_ms,
                max_records=10, max_line_length=50,
            )
            self.assertEqual(len(observations), 1)
            # content 应被裁剪到 max_line_length=50 左右，远小于原始 5000
            self.assertLess(len(observations[0]["content"]), 100)

    def test_template_miner_parses_log_into_template_and_params(self):
        """drain3 应当把相似日志归为同一模板，参数化变量。"""
        from services.mine_sentinel.template_miner import LogTemplateMiner

        miner = LogTemplateMiner()
        if not miner.available:
            self.skipTest("drain3 未安装，跳过模板解析测试")

        r1 = miner.parse("[14:02:11 INFO]: Steve joined the game")
        r2 = miner.parse("[14:02:14 INFO]: Alex joined the game")
        # 同一模板（玩家加入），template_id 应相同
        self.assertEqual(r1.template_id, r2.template_id)
        self.assertGreater(r2.cluster_size, r1.cluster_size)
        # 模板应参数化玩家名
        self.assertIn("<*>", r2.template)

    def test_build_observation_includes_template_id_in_context(self):
        """_build_observation 应当在 context 里写入 templateId/template/templateSize。"""
        from services.mine_sentinel.models import MineSentinelLogSourceConfig

        source = MineSentinelLogSourceConfig(
            server_id="srv", server_name="Srv",
            server_type="minecraft", root="/tmp",
        )
        obs = _build_observation(
            source, Path("/tmp/logs/latest.log"),
            "[14:02:11 INFO]: Steve joined the game",
            timestamp_ms=1700000000000, max_line_length=1000,
        )
        ctx = obs["context"]
        self.assertIn("templateId", ctx)
        self.assertIn("template", ctx)
        self.assertIn("templateSize", ctx)

    def test_build_observation_includes_otel_fields(self):
        """_build_observation 应当在 context.otel 写入 OTel Logs Data Model 字段。"""
        from services.mine_sentinel.models import MineSentinelLogSourceConfig

        source = MineSentinelLogSourceConfig(
            server_id="srv", server_name="Survival",
            server_type="minecraft", root="/tmp",
        )
        obs = _build_observation(
            source, Path("/tmp/logs/latest.log"),
            "[14:02:11 ERROR]: Failed to tick plugin Example",
            timestamp_ms=1700000000000, max_line_length=1000,
        )
        otel = obs["context"]["otel"]
        # Timestamp / ObservedTimestamp / SeverityText / SeverityNumber / Body / EventName
        self.assertEqual(otel["timestamp"], 1700000000000)
        self.assertGreaterEqual(otel["observedTimestamp"], 1700000000000)
        self.assertEqual(otel["severityText"], "ERROR")
        self.assertEqual(otel["severityNumber"], 17)
        self.assertIn("Failed to tick plugin", otel["body"])
        self.assertTrue(otel["eventName"])
        # Resource（来源属性）
        self.assertEqual(otel["resource"]["service.name"], "srv")
        self.assertEqual(otel["resource"]["service.namespace"], "minecraft")
        self.assertEqual(otel["resource"]["host.name"], "Survival")
        # Attributes（日志特有属性）
        attrs = otel["attributes"]
        self.assertEqual(attrs["log.file.name"], "latest.log")
        self.assertIn("template.id", attrs)
        self.assertIn("fingerprint", attrs)
        self.assertIn("anomaly.score", attrs)

    def test_severity_number_maps_otel_levels(self):
        """_severity_number 应当把 MC 日志级别映射为 OTel SeverityNumber。"""
        from services.mine_sentinel.runtime_log import _severity_number

        self.assertEqual(_severity_number("TRACE"), 1)
        self.assertEqual(_severity_number("DEBUG"), 5)
        self.assertEqual(_severity_number("INFO"), 9)
        self.assertEqual(_severity_number("WARN"), 13)
        self.assertEqual(_severity_number("WARNING"), 13)
        self.assertEqual(_severity_number("ERROR"), 17)
        self.assertEqual(_severity_number("FATAL"), 21)
        self.assertEqual(_severity_number("SEVERE"), 21)
        # 未知级别默认 INFO
        self.assertEqual(_severity_number("UNKNOWN"), 9)

    def test_otel_dict_survives_compact_as_dict(self):
        """compact_value 应当递归 compact 嵌套 dict，而不是 stringify。

        context["otel"] 必须在 normalize_record 后仍为 dict，
        否则 OTel-compatible 系统无法按字段检索。
        """
        from services.mine_sentinel.storage.codec import ObservationRecordCodec

        config = MineSentinelConfig.from_dict({})
        codec = ObservationRecordCodec(config)
        record = ObservationRecord(
            event_id="", kind="SERVER_LOG", timestamp=1700000000000,
            server_id="srv", server_name="Srv",
            content="[14:02 ERROR]: Failed to tick plugin Example",
            tags=["server_log", "error"],
            context={
                "level": "ERROR",
                "otel": {
                    "severityNumber": 17,
                    "severityText": "ERROR",
                    "eventName": "T1",
                    "resource": {"service.name": "srv"},
                    "attributes": {"template.id": "T1", "anomaly.score": 0.8},
                },
            },
        )
        codec.normalize_record(record)
        # otel 必须仍然是 dict（而不是被 JSON dump 成字符串）
        otel = record.context["otel"]
        self.assertIsInstance(otel, dict)
        self.assertEqual(otel["severityNumber"], 17)
        self.assertEqual(otel["severityText"], "ERROR")
        # 嵌套 dict 也应保持结构
        self.assertIsInstance(otel["resource"], dict)
        self.assertEqual(otel["resource"]["service.name"], "srv")
        self.assertIsInstance(otel["attributes"], dict)
        self.assertEqual(otel["attributes"]["template.id"], "T1")

    def test_read_jsonl_window_end_ms_is_exclusive(self):
        """read_jsonl_window 的 end_ms 应是右开边界：ts == end_ms 的行不包含在窗口内。

        PR8 修复：之前代码用 ``ts > end_ms`` 判断（即 ts == end_ms 仍 yield），
        与注释 ``[cutoff_ms, end_ms)`` 语义不一致。现在改为 ``ts >= end_ms``。
        """
        from services.mine_sentinel.storage.codec import ObservationRecordCodec

        config = MineSentinelConfig.from_dict({})
        codec = ObservationRecordCodec(config)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "window.jsonl"
            # 写入 5 行，timestamp 分别为 1000/2000/3000/4000/5000
            rows = [{"timestamp": ts, "content": f"line-{ts}"} for ts in range(1000, 6000, 1000)]
            with path.open("w", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row) + "\n")

            # 窗口 [2000, 4000)：应只包含 2000 和 3000，不含 4000
            collected = list(codec.read_jsonl_window(path, cutoff_ms=2000, end_ms=4000))
            timestamps = [r["timestamp"] for r in collected]
            self.assertEqual(timestamps, [2000, 3000], f"end_ms 应是右开，实际 {timestamps}")
            # 4000 不应出现
            self.assertNotIn(4000, timestamps, "ts == end_ms 不应在窗口内")

            # 窗口 [1000, 5000)：应包含 1000/2000/3000/4000，不含 5000
            collected = list(codec.read_jsonl_window(path, cutoff_ms=1000, end_ms=5000))
            timestamps = [r["timestamp"] for r in collected]
            self.assertEqual(timestamps, [1000, 2000, 3000, 4000])
            self.assertNotIn(5000, timestamps)

            # end_ms=None：包含到文件末尾
            collected = list(codec.read_jsonl_window(path, cutoff_ms=3000, end_ms=None))
            timestamps = [r["timestamp"] for r in collected]
            self.assertEqual(timestamps, [3000, 4000, 5000])

    def test_ai_prompt_includes_anomaly_evidence(self):
        """AI prompt 应当包含预计算的异常证据，而非让 LLM 重新检测。"""
        from services.mine_sentinel.reporting.ai_prompt import AIReportPromptBuilder
        from services.mine_sentinel.anomaly_detector import TemplateAnomalyDetector
        import services.mine_sentinel.anomaly_detector as ad_module

        # 临时替换全局检测器，注入已知异常
        test_detector = TemplateAnomalyDetector(
            bucket_seconds=1, ewma_alpha=0.3,
            spike_threshold=2.0, min_baseline_count=2,
        )
        # 积累基线 + 突增
        for i in range(3):
            test_detector.observe("srv", "T_SPIKE",
                                  template="<*> connection reset",
                                  level="WARN", timestamp_ms=i * 1000)
        for i in range(10):
            test_detector.observe("srv", "T_SPIKE",
                                  template="<*> connection reset",
                                  level="WARN", timestamp_ms=5000 + i * 100)
        old_global = ad_module._global_detector
        ad_module._global_detector = test_detector
        try:
            config = MineSentinelConfig.from_dict({})
            builder = AIReportPromptBuilder(config)
            # 构造一条匹配 T_SPIKE 模板的记录
            from services.mine_sentinel.models import ObservationRecord
            record = ObservationRecord(
                event_id="log-1", kind="SERVER_LOG", timestamp=1700000000000,
                server_id="srv", server_name="Srv",
                content="[14:00 WARN]: connection reset by peer",
                tags=["server_log", "warn"],
                context={"level": "WARN", "templateId": "T_SPIKE"},
            )
            fallback = HeuristicReportBuilder(config).build([record], 60)
            prompt = builder.build([record], 60, fallback)
            # prompt 应包含异常证据段
            self.assertIn("异常证据:", prompt)
            # 异常证据段应包含模板和分数
            self.assertIn("T_SPIKE", prompt)
            self.assertIn("ewma_spike", prompt)
        finally:
            ad_module._global_detector = old_global

    def test_anomaly_evidence_returns_empty_without_anomalies(self):
        """无异常时 anomaly_evidence 应返回空列表。"""
        from services.mine_sentinel.reporting.ai_prompt import AIReportPromptBuilder
        from services.mine_sentinel.anomaly_detector import TemplateAnomalyDetector
        import services.mine_sentinel.anomaly_detector as ad_module

        # 用全新检测器（无异常）
        clean_detector = TemplateAnomalyDetector()
        old_global = ad_module._global_detector
        ad_module._global_detector = clean_detector
        try:
            config = MineSentinelConfig.from_dict({})
            builder = AIReportPromptBuilder(config)
            evidence = builder.anomaly_evidence([])
            self.assertEqual(evidence, [])
        finally:
            ad_module._global_detector = old_global

    def test_loop_filter_dedupes_by_template_id(self):
        """loop_filter 应当按 templateId 合并同类日志，而不是只看 fingerprint。"""
        from services.mine_sentinel.models import MineSentinelRuntimeLogConfig
        from services.mine_sentinel.runtime_log import RuntimeLogLoopFilter

        config = MineSentinelRuntimeLogConfig(
            enabled=True, loop_filter_enabled=True,
            loop_filter_window_seconds=300, loop_summary_interval_seconds=60,
        )
        lf = RuntimeLogLoopFilter(config)
        # 两条相似日志（同模板，不同玩家名）
        base_ctx = {
            "level": "WARN", "serverType": "minecraft",
            "templateId": "T1", "template": "<*> WARN]: connection reset",
            "templateSize": 1, "fingerprint": "fp1",
        }
        obs1 = {"serverId": "srv", "content": "[14:00 WARN]: connection reset",
                "timestamp": 1700000000000, "context": dict(base_ctx), "tags": []}
        obs2 = {"serverId": "srv", "content": "[14:01 WARN]: connection reset",
                "timestamp": 1700000030000, "context": dict(base_ctx), "tags": []}
        r1 = lf.process(obs1)
        r2 = lf.process(obs2)
        self.assertEqual(len(r1), 1)  # 首条直接放行
        self.assertEqual(len(r2), 0)  # 第二条被合并（30s < summary_interval=60s）

    def test_anomaly_detector_detects_spike(self):
        """异常检测器应当识别模板计数突增。"""
        from services.mine_sentinel.anomaly_detector import TemplateAnomalyDetector

        detector = TemplateAnomalyDetector(
            bucket_seconds=1, ewma_alpha=0.3,
            spike_threshold=2.5, min_baseline_count=3,
        )
        # 先积累基线（低计数）
        for i in range(5):
            detector.observe("srv", "T1", template="<*> connection reset",
                             level="WARN", timestamp_ms=i * 1000)
        # 突增：短时间内大量同类日志
        # 20 条分布在两个 bucket（5xxx 和 6xxx），各 10 条。
        # 第一个突增 bucket（5xxx）在累计到 ~5 条时超过 baseline*spike_threshold，
        # 异常发生在 spike_results[4:10]；第二个 bucket 的 baseline 已被第一个
        # bucket 拉高（EWMA 在 bucket 切换时更新），因此检测整个 spike 阶段即可。
        spike_results = []
        for i in range(20):
            r = detector.observe("srv", "T1", template="<*> connection reset",
                                 level="WARN", timestamp_ms=5000 + i * 100)
            spike_results.append(r)
        # 突增阶段应当出现异常告警
        self.assertTrue(any(r.is_anomaly for r in spike_results))
        self.assertTrue(any(r.score >= 0.5 for r in spike_results))

    def test_anomaly_detector_marks_new_template(self):
        """新模板首次出现应当有 novelty 分数。"""
        from services.mine_sentinel.anomaly_detector import TemplateAnomalyDetector

        detector = TemplateAnomalyDetector()
        r = detector.observe("srv", "NEW_TPL", template="first time seen",
                             level="INFO", timestamp_ms=1000)
        self.assertGreater(r.score, 0.0)
        self.assertIn("new_template", r.reason)

    def test_anomaly_detector_evicts_when_per_server_exceeds_max(self):
        """per-server 模板数超过 max_templates_per_server 时应当淘汰最久未见的。"""
        from services.mine_sentinel.anomaly_detector import TemplateAnomalyDetector

        detector = TemplateAnomalyDetector(
            max_templates_per_server=3,
            cleanup_interval=1000,  # 不触发周期清理
        )
        # 创建 3 个模板，每个 last_seen_ms 递增
        for i in range(3):
            detector.observe("srv", f"T{i}", template=f"tpl{i}",
                             level="INFO", timestamp_ms=1000 + i * 1000)
        snap = detector.snapshot()
        self.assertEqual(snap["per_server_count"].get("srv"), 3)
        self.assertEqual(snap["cleanup_count"], 0)

        # 创建第 4 个：超出上限，最久未见的 T0 应被淘汰
        detector.observe("srv", "T3", template="tpl3",
                         level="INFO", timestamp_ms=5000)
        snap = detector.snapshot()
        self.assertEqual(snap["per_server_count"].get("srv"), 3)
        self.assertGreaterEqual(snap["cleanup_count"], 1)
        self.assertGreater(snap["last_cleanup_ms"], 0)
        # T0 应该已经被淘汰（最久未见）
        survivor_ids = {a["template_id"] for a in snap["anomalies"]}
        # anomalies 只列 score 最高的 20 个，但 T0 若被淘汰应不在分片 stats 里
        # 直接检查内部状态（PR9: per-server 分片，stats 按 template_id 索引）
        shard = detector._shard_for("srv")
        with shard.lock:
            survivor_ids = set(shard.stats.keys())
        self.assertNotIn("T0", survivor_ids)
        self.assertIn("T3", survivor_ids)

    def test_anomaly_detector_cleans_inactive_templates_by_ttl(self):
        """超过 inactive_template_ttl_hours 未活跃的模板应被周期性清理。"""
        from services.mine_sentinel.anomaly_detector import TemplateAnomalyDetector

        detector = TemplateAnomalyDetector(
            inactive_template_ttl_hours=1,  # 1 小时 TTL
            cleanup_interval=3,  # 每 3 次 observe 触发清理
        )
        # 早期模板：1 小时之前
        old_ms = 1000
        detector.observe("srv", "OLD_TPL", template="old",
                         level="INFO", timestamp_ms=old_ms)
        # 当前时间（超过 TTL）
        now_ms = old_ms + 2 * 3600 * 1000  # 2 小时后
        detector.observe("srv", "FRESH_TPL", template="fresh",
                         level="INFO", timestamp_ms=now_ms)
        detector.observe("srv", "FRESH_TPL", template="fresh",
                         level="INFO", timestamp_ms=now_ms + 1000)
        # 第 3 次 observe 触发清理，OLD_TPL last_seen_ms < cutoff 应被清除
        detector.observe("srv", "FRESH_TPL", template="fresh",
                         level="INFO", timestamp_ms=now_ms + 2000)
        snap = detector.snapshot()
        # PR9: per-server 分片，stats 按 template_id 索引
        shard = detector._shard_for("srv")
        with shard.lock:
            survivor_ids = set(shard.stats.keys())
        self.assertNotIn("OLD_TPL", survivor_ids)
        self.assertIn("FRESH_TPL", survivor_ids)
        self.assertGreaterEqual(snap["cleanup_count"], 1)

    def test_anomaly_detector_snapshot_reports_per_server_and_cleanup(self):
        """snapshot 应当输出 per_server_count / cleanup_count / last_cleanup_ms。"""
        from services.mine_sentinel.anomaly_detector import TemplateAnomalyDetector

        detector = TemplateAnomalyDetector(
            max_templates_per_server=2,
            cleanup_interval=1000,
        )
        detector.observe("srv_a", "T1", template="a1", level="INFO", timestamp_ms=1000)
        detector.observe("srv_b", "T1", template="b1", level="INFO", timestamp_ms=1000)
        # 触发 srv_a 淘汰
        detector.observe("srv_a", "T2", template="a2", level="INFO", timestamp_ms=2000)
        detector.observe("srv_a", "T3", template="a3", level="INFO", timestamp_ms=3000)

        snap = detector.snapshot()
        self.assertIn("per_server_count", snap)
        self.assertIn("cleanup_count", snap)
        self.assertIn("last_cleanup_ms", snap)
        self.assertGreaterEqual(snap["cleanup_count"], 1)
        # 至少有一个 server 有计数
        self.assertTrue(any(v > 0 for v in snap["per_server_count"].values()))


class MineSentinelRulesTests(unittest.TestCase):
    """Tests for the refactored rules engine: network/plugin categories and critical direct alert."""

    def _make_record(self, content, level="INFO", server_id="survival", tags=None, context=None):
        now = int(time.time() * 1000)
        return ObservationRecord(
            event_id=f"log-{abs(hash(content)) % 10_000_000}",
            kind="SERVER_LOG",
            timestamp=now,
            server_id=server_id,
            server_name=server_id.capitalize(),
            content=content,
            tags=tags or ["server_log", "runtime_log"],
            context={"level": level, **(context or {})},
        )

    def test_network_category_classifies_connection_reset(self):
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        record = self._make_record(
            "io.netty.channel.unix.Errors$NativeConnectException: connect(..) failed: Connection refused",
            level="WARN",
        )
        self.assertEqual(builder.classify(record), "network")
        self.assertEqual(builder.tag(record), "server_log_network")

    def test_network_category_classifies_broken_pipe(self):
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        record = self._make_record(
            "[Server thread/WARN]: Broken pipe during packet flush",
            level="WARN",
        )
        self.assertEqual(builder.classify(record), "network")

    def test_plugin_category_classifies_could_not_load(self):
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        record = self._make_record(
            "[Server thread/ERROR]: Could not load plugin EssentialsX-2.20.1.jar",
            level="ERROR",
        )
        self.assertEqual(builder.classify(record), "plugin")
        self.assertEqual(builder.tag(record), "server_log_plugin")

    def test_plugin_category_classifies_dependency_missing(self):
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        record = self._make_record(
            "[Server thread/ERROR]: Plugin DependsOnVault missing dependency: Vault",
            level="ERROR",
        )
        self.assertEqual(builder.classify(record), "plugin")

    def test_critical_marker_raises_severity_to_critical(self):
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        record = self._make_record(
            "[Server thread/FATAL]: OutOfMemoryError: Java heap space",
            level="FATAL",
        )
        # 单条 critical marker 直接归 critical
        severity = builder._severity([record])
        self.assertEqual(severity, "critical")

    def test_critical_marker_watchdog(self):
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        record = self._make_record(
            "[Watchdog thread/ERROR]: Server thread froze for 60 seconds (tick took too long)",
            level="ERROR",
        )
        self.assertEqual(builder._severity([record]), "critical")

    def test_critical_direct_alert_ignores_min_evidence_count(self):
        """critical 严重级别应当绕过 min_evidence_count 直接告警。"""
        config = MineSentinelConfig.from_dict(
            {"alert": {"enabled": True, "min_severity": "high", "min_evidence_count": 5}}
        )
        builder = HeuristicReportBuilder(config)
        record = self._make_record(
            "[Server thread/FATAL]: Server stopped unexpectedly (crash)",
            level="FATAL",
        )
        report = builder.build([record], 60, "survival")
        critical_issues = [
            issue for issue in report["issues"] if issue["severity"] == "critical"
        ]
        self.assertTrue(critical_issues, "expected at least one critical issue")
        # critical 直告：即使 evidence_count=1（< min_evidence_count=5）也应当告警
        self.assertTrue(
            all(issue["should_alert"] for issue in critical_issues),
            "critical issues must alert regardless of min_evidence_count",
        )
        self.assertTrue(report["any_alert"])
        self.assertEqual(report["max_severity"], "critical")

    def test_critical_marker_crash_in_chinese(self):
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        record = self._make_record(
            "[Server thread/ERROR]: 服务器内存溢出，进程崩溃",
            level="ERROR",
        )
        self.assertEqual(builder._severity([record]), "critical")

    def test_plugin_load_failure_raises_to_high(self):
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        record = self._make_record(
            "[Server thread/ERROR]: Could not enable plugin 'ShopUI' - dependency missing",
            level="ERROR",
        )
        # 单条 plugin load/enable failure 即提级 high
        self.assertEqual(builder._severity([record]), "high")

    def test_performance_repeat_raises_to_high(self):
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        # 注意：不能用 "Can't keep up! Is the server overloaded" ——
        # 该完整短语本身是 CRITICAL_MARKER，会直接归 critical。
        records = [
            self._make_record(
                "[Server thread/WARN]: Server is lagging badly, TPS dropped to 5",
                level="WARN",
            )
            for _ in range(3)
        ]
        self.assertEqual(builder._severity(records), "high")

    def test_network_error_repeat_raises_to_high(self):
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        records = [
            self._make_record(
                f"[Server thread/WARN]: Connection reset by peer (player{_})",
                level="WARN",
            )
            for _ in range(5)
        ]
        self.assertEqual(builder._severity(records), "high")

    def test_multi_server_medium_forces_alert(self):
        """多服务器 + medium 应当强制告警，即使 evidence_count 不足。"""
        config = MineSentinelConfig.from_dict(
            {"alert": {"enabled": True, "min_severity": "high", "min_evidence_count": 5}}
        )
        builder = HeuristicReportBuilder(config)
        # 单条 ERROR 在两个不同 server 上 → severity=medium（单 ERROR）+ multi_scope → 强制告警
        records = [
            self._make_record(
                "[Server thread/ERROR]: Failed to load chunk",
                level="ERROR",
                server_id="survival",
            ),
            self._make_record(
                "[Server thread/ERROR]: Failed to load chunk",
                level="ERROR",
                server_id="creative",
            ),
        ]
        report = builder.build(records, 60)
        multi_issues = [
            issue
            for issue in report["issues"]
            if len(issue.get("affected_servers", [])) >= 2
        ]
        self.assertTrue(multi_issues, "expected multi-server issue")
        self.assertTrue(
            any(issue["should_alert"] for issue in multi_issues),
            "multi-server medium issue must force alert",
        )

    def test_low_severity_does_not_alert(self):
        """low 严重级别不应当告警。"""
        config = MineSentinelConfig.from_dict(
            {"alert": {"enabled": True, "min_severity": "low", "min_evidence_count": 1}}
        )
        builder = HeuristicReportBuilder(config)
        record = self._make_record(
            "[Server thread/INFO]: Steve joined the game",
            level="INFO",
        )
        report = builder.build([record], 60, "survival")
        # daily + low 被跳过（不进 issues），但即使进了也不应告警
        for issue in report["issues"]:
            if issue["severity"] == "low":
                self.assertFalse(issue["should_alert"])
        self.assertFalse(report["any_alert"])

    def test_classify_priority_community_beats_complaint(self):
        """community 优先级最高，即便同时包含 lag/tps 等性能关键词。"""
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        record = self._make_record(
            "[Server thread/WARN]: Anticheat flagged Steve for kill aura (TPS drop reported)",
            level="WARN",
        )
        self.assertEqual(builder.classify(record), "community")

    def test_classify_priority_complaint_beats_network(self):
        """complaint 优先级高于 network（即使含 disconnect 也要先归 complaint）。"""
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        record = self._make_record(
            "[Server thread/WARN]: Can't keep up! Player disconnected due to lag",
            level="WARN",
        )
        self.assertEqual(builder.classify(record), "complaint")

    def test_classify_priority_network_beats_bug(self):
        """network 优先级高于 bug：连接异常不应当归入 bug。"""
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        record = self._make_record(
            "[Server thread/ERROR]: java.net.SocketException: Connection reset",
            level="ERROR",
        )
        self.assertEqual(builder.classify(record), "network")

    def test_classify_priority_plugin_beats_bug(self):
        """plugin 优先级高于 bug：插件加载失败不应当归入普通 bug。"""
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        record = self._make_record(
            "[Server thread/ERROR]: Could not load plugin; see stacktrace below",
            level="ERROR",
        )
        self.assertEqual(builder.classify(record), "plugin")

    def test_ops_notes_include_counters(self):
        """ops_notes 应当包含 PERFORMANCE/NETWORK/PLUGIN 计数和影响范围信息。"""
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        records = [
            self._make_record(
                "[Server thread/WARN]: Can't keep up! Running behind",
                level="WARN",
                server_id="survival",
            ),
            self._make_record(
                "[Server thread/WARN]: io.netty connection reset",
                level="WARN",
                server_id="creative",
            ),
            self._make_record(
                "[Server thread/ERROR]: Could not load plugin ShopUI",
                level="ERROR",
                server_id="survival",
            ),
        ]
        report = builder.build(records, 60)
        counters = report["counters"]
        self.assertGreaterEqual(counters["performance"], 1)
        self.assertGreaterEqual(counters["network"], 1)
        self.assertGreaterEqual(counters["plugin"], 1)
        self.assertGreaterEqual(counters["error"], 1)
        joined = " ".join(report["ops_notes"])
        self.assertIn("PERFORMANCE", joined)
        self.assertIn("NETWORK", joined)
        self.assertIn("PLUGIN", joined)

    def test_suggest_action_per_category(self):
        """不同分类应当给出有针对性的推荐动作。"""
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        cases = [
            ("complaint", "server_log_performance", "TPS"),
            ("network", "server_log_network", "代理到后端的连通性"),
            ("plugin", "server_log_plugin", "依赖插件"),
            ("community", "server_log_community", "社区管理流程"),
            ("cross_server", "server_log_cross_server", "forwarding"),
        ]
        for category, tag, keyword in cases:
            action = builder._suggest_action(category, tag, "medium")
            self.assertIn(keyword, action, f"category={category} action missing keyword '{keyword}'")

    def test_suggest_action_critical(self):
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        action = builder._suggest_action("bug", "server_log_error", "critical")
        self.assertIn("崩溃报告", action)
        self.assertIn("回滚", action)

    # --- 新增分类：chat_review / player_feedback / community_ops ---
    def test_chat_review_classifies_profanity(self):
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        record = self._make_record(
            "[Async Chat Thread]: <Steve> swore in chat (profanity detected)",
            level="INFO",
        )
        self.assertEqual(builder.classify(record), "chat_review")
        self.assertEqual(builder.tag(record), "server_log_chat_review")

    def test_chat_review_classifies_advertising_link(self):
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        record = self._make_record(
            "[Async Chat Thread]: <Alex> posted advertising link discord.gg/xxxx",
            level="INFO",
        )
        self.assertEqual(builder.classify(record), "chat_review")

    def test_chat_review_classifies_chinese_abuse(self):
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        record = self._make_record(
            "[Async Chat Thread]: <Notch> 在聊天中辱骂其他玩家",
            level="INFO",
        )
        self.assertEqual(builder.classify(record), "chat_review")

    def test_chat_review_word_boundary_ad_does_not_match_load(self):
        """短词 'ad' 不应匹配 'load'/'road' 等普通英文词（词边界保护）。"""
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        record = self._make_record(
            "[Server thread/INFO]: Failed to load datapack road builder",
            level="INFO",
        )
        # 不应被误判为 chat_review
        self.assertNotEqual(builder.classify(record), "chat_review")

    def test_chat_review_word_boundary_ad_matches_standalone_ad(self):
        """独立的 'ad' 词应当匹配 chat_review。"""
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        record = self._make_record(
            "[Async Chat Thread]: <Spammer> posted an ad for shop",
            level="INFO",
        )
        self.assertEqual(builder.classify(record), "chat_review")

    def test_chat_review_threat_raises_to_high_and_alerts(self):
        """chat_review 命中威胁敏感词应提级 high 并强制告警。"""
        config = MineSentinelConfig.from_dict(
            {"alert": {"enabled": True, "min_severity": "high", "min_evidence_count": 5}}
        )
        builder = HeuristicReportBuilder(config)
        record = self._make_record(
            "[Async Chat Thread]: <BadActor> made a threat against another player",
            level="INFO",
        )
        report = builder.build([record], 60, "survival")
        chat_issues = [
            issue for issue in report["issues"] if issue["category"] == "chat_review"
        ]
        self.assertTrue(chat_issues, "expected chat_review issue")
        self.assertEqual(chat_issues[0]["severity"], "high")
        self.assertTrue(chat_issues[0]["should_alert"])

    def test_chat_review_low_does_not_alert(self):
        """chat_review 低严重度（普通聊天）不应告警。"""
        config = MineSentinelConfig.from_dict(
            {"alert": {"enabled": True, "min_severity": "low", "min_evidence_count": 1}}
        )
        builder = HeuristicReportBuilder(config)
        record = self._make_record(
            "[Async Chat Thread]: <Steve> said hello in chat",
            level="INFO",
        )
        report = builder.build([record], 60, "survival")
        chat_issues = [
            issue for issue in report["issues"] if issue["category"] == "chat_review"
        ]
        if chat_issues:
            # 普通聊天 severity=medium（chat_review 单条即 medium），
            # 但 chat_review 默认不告警（需 high/5条/敏感词）
            self.assertFalse(
                chat_issues[0]["should_alert"],
                "普通 chat_review 不应告警",
            )

    def test_chat_review_five_records_alert(self):
        """chat_review evidence_count >= 5 应当告警。"""
        config = MineSentinelConfig.from_dict(
            {"alert": {"enabled": True, "min_severity": "high", "min_evidence_count": 5}}
        )
        builder = HeuristicReportBuilder(config)
        records = [
            self._make_record(
                f"[Async Chat Thread]: <Bot{_}> posted advertising link in chat",
                level="INFO",
            )
            for _ in range(5)
        ]
        report = builder.build(records, 60, "survival")
        chat_issues = [
            issue for issue in report["issues"] if issue["category"] == "chat_review"
        ]
        self.assertTrue(chat_issues)
        self.assertTrue(
            chat_issues[0]["should_alert"],
            "5 条 chat_review 应当告警",
        )

    def test_player_feedback_classifies_suggestion(self):
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        record = self._make_record(
            "[Async Chat Thread]: <Steve> 建议加个新的副本玩法",
            level="INFO",
        )
        self.assertEqual(builder.classify(record), "player_feedback")
        self.assertEqual(builder.tag(record), "server_log_player_feedback")

    def test_player_feedback_does_not_alert(self):
        """player_feedback 通常不告警。"""
        config = MineSentinelConfig.from_dict(
            {"alert": {"enabled": True, "min_severity": "low", "min_evidence_count": 1}}
        )
        builder = HeuristicReportBuilder(config)
        records = [
            self._make_record(
                f"[Async Chat Thread]: <Player{_}> 希望能不能优化一下商店",
                level="INFO",
            )
            for _ in range(5)
        ]
        report = builder.build(records, 60, "survival")
        feedback_issues = [
            issue for issue in report["issues"] if issue["category"] == "player_feedback"
        ]
        if feedback_issues:
            self.assertFalse(
                all(issue["should_alert"] for issue in feedback_issues),
                "player_feedback 不应告警",
            )

    def test_community_ops_classifies_event(self):
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        record = self._make_record(
            "[Server thread/INFO]: Summer event activity started, reward dispatched",
            level="INFO",
        )
        self.assertEqual(builder.classify(record), "community_ops")
        self.assertEqual(builder.tag(record), "server_log_community_ops")

    def test_community_ops_severe_raises_to_high(self):
        """community_ops 命中事故关键词应提级 high 并告警。"""
        config = MineSentinelConfig.from_dict(
            {"alert": {"enabled": True, "min_severity": "high", "min_evidence_count": 5}}
        )
        builder = HeuristicReportBuilder(config)
        record = self._make_record(
            "[Server thread/ERROR]: 奖励发放异常，活动事故导致大范围玩家不满",
            level="ERROR",
        )
        report = builder.build([record], 60, "survival")
        ops_issues = [
            issue for issue in report["issues"] if issue["category"] == "community_ops"
        ]
        self.assertTrue(ops_issues, "expected community_ops issue")
        self.assertEqual(ops_issues[0]["severity"], "high")
        self.assertTrue(ops_issues[0]["should_alert"])

    def test_community_ops_normal_does_not_alert(self):
        """普通 community_ops 公告不应告警。"""
        config = MineSentinelConfig.from_dict(
            {"alert": {"enabled": True, "min_severity": "low", "min_evidence_count": 1}}
        )
        builder = HeuristicReportBuilder(config)
        record = self._make_record(
            "[Server thread/INFO]: 新公告：本周活动开启，请查看奖励详情",
            level="INFO",
        )
        report = builder.build([record], 60, "survival")
        ops_issues = [
            issue for issue in report["issues"] if issue["category"] == "community_ops"
        ]
        if ops_issues:
            self.assertFalse(
                ops_issues[0]["should_alert"],
                "普通 community_ops 不应告警",
            )

    def test_classify_priority_chat_review_beats_player_feedback(self):
        """chat_review 优先级高于 player_feedback。"""
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        # 同时包含建议和辱骂 → 应归 chat_review
        record = self._make_record(
            "[Async Chat Thread]: <Troll> 建议你们都去死（辱骂+威胁）",
            level="INFO",
        )
        self.assertEqual(builder.classify(record), "chat_review")

    def test_classify_priority_community_beats_chat_review(self):
        """community 优先级高于 chat_review。"""
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        # 同时包含作弊和聊天 → 应归 community
        record = self._make_record(
            "[Server thread/WARN]: Anticheat flagged Steve for cheat, chat log reviewed",
            level="WARN",
        )
        self.assertEqual(builder.classify(record), "community")

    def test_ops_notes_include_chat_review_feedback_ops_counters(self):
        """ops_notes 应包含 chat_review/player_feedback/community_ops 计数。"""
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        records = [
            self._make_record(
                "[Async Chat Thread]: <A> 辱骂玩家",
                level="INFO",
                server_id="survival",
            ),
            self._make_record(
                "[Async Chat Thread]: <B> 建议加个新功能",
                level="INFO",
                server_id="survival",
            ),
            self._make_record(
                "[Server thread/INFO]: 活动公告：奖励已发放",
                level="INFO",
                server_id="survival",
            ),
        ]
        report = builder.build(records, 60)
        counters = report["counters"]
        self.assertGreaterEqual(counters["chat_review"], 1)
        self.assertGreaterEqual(counters["player_feedback"], 1)
        self.assertGreaterEqual(counters["community_ops"], 1)
        joined = " ".join(report["ops_notes"])
        self.assertIn("聊天审查", joined)
        self.assertIn("玩家建议", joined)
        self.assertIn("社区运营", joined)

    def test_suggest_action_for_new_categories(self):
        """新分类应有针对性的推荐动作。"""
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        cases = [
            ("chat_review", "server_log_chat_review", "聊天审查流程"),
            ("player_feedback", "server_log_player_feedback", "工单"),
            ("community_ops", "server_log_community_ops", "社区运营"),
        ]
        for category, tag, keyword in cases:
            action = builder._suggest_action(category, tag, "medium")
            self.assertIn(keyword, action, f"category={category} action missing '{keyword}'")

    def test_categories_dict_includes_new_categories(self):
        """build 输出的 categories 应包含新分类键。"""
        builder = HeuristicReportBuilder(MineSentinelConfig.from_dict({}))
        record = self._make_record("[Server thread/INFO]: Done!", level="INFO")
        report = builder.build([record], 60, "survival")
        for key in ("chat_review", "player_feedback", "community_ops"):
            self.assertIn(key, report["categories"])
            self.assertIsInstance(report["categories"][key], list)

    # --- PR3: 异常分数提级 severity ---
    def test_severity_promoted_by_anomaly_score(self):
        """异常分数 >= 0.6 应当把 severity 至少提升到 high。"""
        config = MineSentinelConfig.from_dict({})
        builder = HeuristicReportBuilder(config)
        # 构造一条普通 WARN 日志，但带高异常分数
        record = self._make_record(
            "[14:00 WARN]: connection reset by peer",
            level="WARN",
            context={"anomalyScore": 0.7, "anomalyReason": "ewma_spike: ratio=4.0"},
        )
        report = builder.build([record], window_minutes=60)
        self.assertEqual(report["max_severity"], "high")

    def test_severity_critical_for_extreme_anomaly(self):
        """异常分数 >= 0.8 应当直接提级 critical。"""
        config = MineSentinelConfig.from_dict({})
        builder = HeuristicReportBuilder(config)
        record = self._make_record(
            "[14:00 WARN]: connection reset",
            level="WARN",
            context={"anomalyScore": 0.9, "anomalyReason": "ewma_spike: ratio=8.0"},
        )
        report = builder.build([record], window_minutes=60)
        self.assertEqual(report["max_severity"], "critical")


class MineSentinelHourlySummaryTests(unittest.IsolatedAsyncioTestCase):
    """Tests for the hourly summary mode (no polling, per-hour log read + AI integrate)."""

    def setUp(self):
        _install_astrbot_stubs()

    def _make_log_dir(self, tmp: Path, lines: list[tuple[str, str]]) -> Path:
        """Create a logs/ dir under tmp with latest.log and one .log.gz archive.

        Each entry is (date_or_time, line); we just write the line verbatim.
        """
        logs = tmp / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "latest.log").write_text(
            "\n".join(line for _, line in lines) + "\n", encoding="utf-8"
        )
        return tmp

    def test_read_hour_log_lines_filters_by_timestamp(self):
        from datetime import datetime, timedelta
        from services.mine_sentinel.models import MineSentinelLogSourceConfig

        # Use the current wall-clock hour so the timestamp parser's
        # "future time -> subtract 24h" heuristic doesn't kick in.
        now = datetime.now()
        cur_hour = now.replace(minute=0, second=0, microsecond=0)
        prev_hour = cur_hour - timedelta(hours=1)
        hour_a_start_ms = int(prev_hour.timestamp() * 1000)
        hour_b_start_ms = int(cur_hour.timestamp() * 1000)
        hour_a_end_ms = hour_b_start_ms
        lines = [
            f"[{prev_hour:%H:%M:%S}] [Server thread/INFO]: hour A line 1",
            f"[{prev_hour + timedelta(minutes=30):%H:%M:%S}] [Server thread/INFO]: hour A line 2",
            f"[{cur_hour:%H:%M:%S}] [Server thread/INFO]: hour B line 1",
            f"[{cur_hour + timedelta(minutes=45):%H:%M:%S}] [Server thread/INFO]: hour B line 2",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._make_log_dir(tmp_path, [(0, line) for line in lines])

            source = MineSentinelLogSourceConfig(
                server_id="srv",
                server_name="Srv",
                server_type="minecraft",
                root=str(tmp_path),
            )
            rows = read_hour_log_lines(source, hour_a_start_ms, hour_a_end_ms)
            self.assertEqual(len(rows), 2)
            for line, ts, _path in rows:
                self.assertIn("hour A", line)

            rows_b = read_hour_log_lines(
                source, hour_b_start_ms, hour_b_start_ms + 3600 * 1000
            )
            self.assertEqual(len(rows_b), 2)
            for line, ts, _path in rows_b:
                self.assertIn("hour B", line)

    def test_build_hour_observations_returns_observation_dicts(self):
        from datetime import datetime, timedelta
        from services.mine_sentinel.models import MineSentinelLogSourceConfig

        now = datetime.now()
        cur_hour = now.replace(minute=0, second=0, microsecond=0)
        prev_hour = cur_hour - timedelta(hours=1)
        hour_start_ms = int(prev_hour.timestamp() * 1000)
        hour_end_ms = int(cur_hour.timestamp() * 1000)
        lines = [
            f"[{prev_hour:%H:%M:%S}] [Server thread/INFO]: Done (3.5s)! For help, type help",
            f"[{prev_hour + timedelta(minutes=10):%H:%M:%S}] [Server thread/WARN]: Can't keep up!",
            f"[{prev_hour + timedelta(minutes=20):%H:%M:%S}] [Server thread/ERROR]: Exception ticking entity",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._make_log_dir(tmp_path, [(0, line) for line in lines])
            source = MineSentinelLogSourceConfig(
                server_id="srv",
                server_name="Srv",
                server_type="minecraft",
                root=str(tmp_path),
            )
            observations = build_hour_observations(
                source, hour_start_ms, hour_end_ms, max_records=10
            )
            self.assertEqual(len(observations), 3)
            for obs in observations:
                self.assertEqual(obs["kind"], "SERVER_LOG")
                self.assertEqual(obs["serverId"], "srv")
                self.assertEqual(obs["context"]["serverType"], "minecraft")
                self.assertEqual(
                    obs["context"]["source"], "astrbot_hourly_read"
                )
            self.assertTrue(any("error" in o["tags"] for o in observations))
            self.assertTrue(any("warning" in o["tags"] for o in observations))

    def test_hourly_summary_store_save_load_and_list_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HourlySummaryStore(Path(tmp))
            hs = HourlySummary(
                server_id="srv",
                server_name="Srv",
                hour_start_ms=1700000000000,
                hour_end_ms=1700003600000,
                records_count=10,
                error_count=1,
                warning_count=2,
                info_count=7,
                summary="小时总结",
                key_issues=[{"title": "x", "severity": "high"}],
                top_events=["e1", "e2"],
                source="ai",
            )
            path = store.save(hs)
            self.assertTrue(path.exists())

            loaded = store.load("srv", hs.hour_start_ms)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.records_count, 10)
            self.assertEqual(loaded.summary, "小时总结")

            cycle_summaries = store.list_cycle_summaries(
                "srv",
                hs.hour_start_ms,
                hs.hour_end_ms + 3600 * 1000,
            )
            self.assertEqual(len(cycle_summaries), 1)
            self.assertEqual(cycle_summaries[0].server_id, "srv")

    def test_hourly_summarizer_falls_back_to_heuristic_without_provider(self):
        from services.mine_sentinel.models import MineSentinelLogSourceConfig

        config = MineSentinelConfig.from_dict({})
        summarizer = HourlySummarizer(config, context=None)
        records = [
            ObservationRecord(
                event_id="srv:1",
                kind="SERVER_LOG",
                timestamp=1700000000000,
                server_id="srv",
                content="[14:00:00] [Server thread/INFO]: Done",
                tags=["server_log", "runtime_log", "info", "minecraft"],
                context={"serverType": "minecraft"},
            ),
            ObservationRecord(
                event_id="srv:2",
                kind="SERVER_LOG",
                timestamp=1700000100000,
                server_id="srv",
                content="[14:10:00] [Server thread/ERROR]: boom",
                tags=["server_log", "runtime_log", "error", "exception", "minecraft"],
                context={"serverType": "minecraft"},
            ),
        ]
        source = MineSentinelLogSourceConfig(
            server_id="srv", server_name="Srv", server_type="minecraft"
        )
        hourly = asyncio.get_event_loop().run_until_complete(
            summarizer.build_hourly_summary(
                records, source, 1700000000000, 1700003600000, umo=None
            )
        )
        self.assertEqual(hourly.source, "heuristic")
        self.assertEqual(hourly.records_count, 2)
        self.assertEqual(hourly.error_count, 1)
        self.assertGreater(len(hourly.summary), 0)

    def test_cycle_report_heuristic_integrates_hourly_summaries(self):
        config = MineSentinelConfig.from_dict({})
        summarizer = HourlySummarizer(config, context=None)
        summaries = [
            HourlySummary(
                server_id="srv",
                server_name="Srv",
                hour_start_ms=1700000000000 + i * 3600000,
                hour_end_ms=1700000000000 + (i + 1) * 3600000,
                records_count=10 + i,
                error_count=i,
                warning_count=2,
                info_count=8 - i,
                summary=f"第 {i+1} 小时总结",
                key_issues=[{"title": f"issue-{i}", "severity": "high"}],
                top_events=[f"event-{i}"],
                source="heuristic",
            )
            for i in range(8)
        ]
        report = asyncio.get_event_loop().run_until_complete(
            summarizer.build_cycle_report(summaries, "srv", umo=None)
        )
        self.assertEqual(report["source"], "heuristic")
        self.assertEqual(report["total_records"], sum(10 + i for i in range(8)))
        self.assertEqual(report["total_errors"], sum(range(8)))
        self.assertEqual(len(report["timeline"]), 8)

        text = format_cycle_report(report, summaries, "Srv")
        self.assertIn("MineSentinel 周期报告", text)
        self.assertIn("8 小时", text)
        self.assertIn("第 1 小时总结", text)

    def test_hourly_summary_job_seconds_until_next_hour_aligns_to_wall_clock(self):
        # 14:35:00 -> next hour at 15:00:00 = 1500 seconds.
        next_hour = HourlySummaryJob.seconds_until_next_hour(
            time.mktime(time.strptime("2026-07-05 14:35:00", "%Y-%m-%d %H:%M:%S"))
        )
        self.assertAlmostEqual(next_hour, 1500.0, delta=1.0)
        # 14:00:30 -> next hour at 15:00:00 = 3570 seconds.
        next_hour2 = HourlySummaryJob.seconds_until_next_hour(
            time.mktime(time.strptime("2026-07-05 14:00:30", "%Y-%m-%d %H:%M:%S"))
        )
        self.assertAlmostEqual(next_hour2, 3570.0, delta=1.0)

    def test_config_parses_hourly_summary_section(self):
        config = MineSentinelConfig.from_dict(
            {
                "hourly_summary": {
                    "enabled": True,
                    "hours_per_cycle": 4,
                    "window_minutes": 60,
                    "poll_enabled": False,
                    "provider_id": "openai:gpt-4",
                    "max_records_per_hour": 1000,
                    "max_log_lines_per_hour": 5000,
                    "retention_cycles": 3,
                }
            }
        )
        self.assertTrue(config.hourly_summary.enabled)
        self.assertEqual(config.hourly_summary.hours_per_cycle, 4)
        self.assertFalse(config.hourly_summary.poll_enabled)
        self.assertEqual(config.hourly_summary.provider_id, "openai:gpt-4")
        self.assertEqual(config.hourly_summary.max_records_per_hour, 1000)
        self.assertEqual(config.hourly_summary.retention_cycles, 3)

    async def test_service_hourly_mode_skips_polling(self):
        """When hourly_summary.enabled is True and poll_enabled is False,
        the runtime_log_tailer must NOT be started."""
        import services.mine_sentinel.service as service_module
        from services.mine_sentinel.service import MineSentinelService

        with tempfile.TemporaryDirectory() as tmp:
            config_data = {
                "enabled": True,
                "runtime_log": {
                    "enabled": True,
                    "sources": [
                        {
                            "server_id": "srv",
                            "server_name": "Srv",
                            "server_type": "minecraft",
                            "root": tmp,
                        }
                    ],
                },
                "hourly_summary": {
                    "enabled": True,
                    "poll_enabled": False,
                    "hours_per_cycle": 8,
                },
                "report": {"enabled": False},
            }

            class _FakeContext:
                pass

            service = MineSentinelService(
                context=_FakeContext(),
                config_data=config_data,
                get_server_config=lambda sid: None,
                storage_dir=tmp,
                io_runner=_run_sync,
            )

            tailer_started = False
            original_start = service.runtime_log_tailer.start

            def _spy_start():
                nonlocal tailer_started
                tailer_started = True
                original_start()

            service.runtime_log_tailer.start = _spy_start

            try:
                service.start()
            finally:
                await service.stop()

            self.assertFalse(
                tailer_started,
                "runtime_log_tailer should NOT start when hourly mode is on and poll_enabled is false",
            )

    async def test_service_hourly_calls_run_hour_per_source(self):
        """The HourlySummaryJob should invoke _run_hourly_for_source once per source."""
        from services.mine_sentinel.service import MineSentinelService

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            logs = tmp_path / "logs"
            logs.mkdir(parents=True)
            (logs / "latest.log").write_text(
                "[14:00:00] [Server thread/INFO]: hello\n", encoding="utf-8"
            )
            config_data = {
                "enabled": True,
                "runtime_log": {
                    "enabled": True,
                    "sources": [
                        {
                            "server_id": "srv",
                            "server_name": "Srv",
                            "server_type": "minecraft",
                            "root": str(tmp_path),
                        }
                    ],
                },
                "hourly_summary": {
                    "enabled": True,
                    "poll_enabled": False,
                    "hours_per_cycle": 8,
                },
                "report": {"enabled": False},
            }

            class _FakeContext:
                pass

            service = MineSentinelService(
                context=_FakeContext(),
                config_data=config_data,
                get_server_config=lambda sid: None,
                storage_dir=tmp,
                io_runner=_run_sync,
            )

            called: list[tuple[int, int, str]] = []

            async def _spy_run_hour(h_start, h_end, sid):
                called.append((h_start, h_end, sid))

            service._run_hourly_for_source = _spy_run_hour
            # Rebind the job's run_hour to our spy since it was captured at construction.
            service._hourly_job.run_hour = _spy_run_hour

            # Manually invoke the partial-hour handler as if the job just started.
            await service._hourly_job._process_current_partial_hour()

            self.assertEqual(len(called), 1)
            self.assertEqual(called[0][2], "srv")


async def _run_sync(func, *args):
    return func(*args)


def _install_astrbot_stubs():
    class Logger:
        def debug(self, *args, **kwargs):
            pass

        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

    astrbot = sys.modules.get("astrbot") or types.ModuleType("astrbot")
    api = sys.modules.get("astrbot.api") or types.ModuleType("astrbot.api")
    api.logger = getattr(api, "logger", Logger())
    api.__path__ = []  # mark as package so `from astrbot.api.X import Y` works
    # Stub astrbot.api.event so service -> delivery can import MessageChain.
    if "astrbot.api.event" not in sys.modules:
        event_mod = types.ModuleType("astrbot.api.event")

        class _MessageChain:
            def __init__(self, nodes=None):
                self.nodes = list(nodes or [])

        event_mod.MessageChain = _MessageChain
        sys.modules["astrbot.api.event"] = event_mod
    # Stub astrbot.api.message_components for Plain etc.
    if "astrbot.api.message_components" not in sys.modules:
        comp_mod = types.ModuleType("astrbot.api.message_components")

        class _Plain:
            def __init__(self, text=""):
                self.text = str(text)

        comp_mod.Plain = _Plain
        sys.modules["astrbot.api.message_components"] = comp_mod
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api})


class MineSentinelEndToEndIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """端到端集成测试：从文件读取 → tailer 处理 → JSONL 落盘的完整链路。

    覆盖发版前 3 个关键验证：
    - #3 burst backlog：突发日志超过 max_lines_per_poll 时不丢行，下轮继续处理
    - #4 多 server_id Drain3 namespace 隔离：不同服的同模板不互相污染
    - #5 JSONL 中 context.otel 保持嵌套 dict（端到端落盘验证）
    """

    def _make_config(self, tmp_dir, sources, max_lines_per_poll=3):
        return MineSentinelConfig.from_dict({
            "runtime_log": {
                "sources": sources,
                "backfill_on_start": False,
                "initial_lines": 0,
                "poll_interval_seconds": 1,
                "max_bytes_per_poll": 65536,
                "max_lines_per_poll": max_lines_per_poll,
                "loop_filter_enabled": False,  # E2E 关注不丢行，关闭合并
            },
            "storage": {"enabled": True, "dir": str(Path(tmp_dir) / "store")},
        })

    async def test_burst_backlog_e2e_no_lines_lost(self):
        """#3 E2E: burst 超过 max_lines_per_poll 时，多轮 poll 后总 observation 数 == 原始行数。"""
        _install_astrbot_stubs()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "latest.log"
            # 写入 10 行 INFO 日志（INFO 不会被 loop_filter 合并）
            ts = "12:00:00"
            lines_written = [f"[{ts} INFO]: daily event number {i}" for i in range(10)]
            log_path.write_text("\n".join(lines_written) + "\n", encoding="utf-8")

            config = self._make_config(
                tmp,
                [{"server_id": "srv", "server_name": "Srv", "log_file": str(log_path)}],
                max_lines_per_poll=3,
            )

            collected = []

            async def handle_batch(server_id, payload):
                collected.extend(payload.get("observations", []))

            tailer = MineSentinelRuntimeLogTailer(
                config.runtime_log, handle_batch, io_runner=_run_sync,
            )
            source = config.runtime_log.sources[0]
            from services.mine_sentinel.runtime_log import _SourceState
            state = _SourceState(source=source, log_file=log_path)

            # 多轮 poll 直到 backlog 清空（position 到文件末尾且无 pending）
            for _ in range(10):
                await tailer._poll_source(state)
                if not state.has_pending and state.position >= log_path.stat().st_size:
                    break

            # 关键断言：10 行全部收到，不丢行
            self.assertEqual(
                len(collected), 10,
                f"应收到 10 条 observation（不丢行），实际 {len(collected)}",
            )
            # 验证内容完整（按顺序）
            contents = [obs["content"] for obs in collected]
            for i in range(10):
                self.assertTrue(
                    any(f"number {i}" in c for c in contents),
                    f"第 {i} 行丢失",
                )

    def test_multi_server_template_namespace_isolation(self):
        """#4 E2E: 两个 server_id 的相同日志模板应分属不同 namespace，不互相污染 new_template。"""
        _install_astrbot_stubs()
        from services.mine_sentinel.template_miner import LogTemplateMiner

        miner = LogTemplateMiner()
        # 同一条日志分别在 srv_a 和 srv_b 解析
        line = "[12:00:00 INFO]: Steve joined the game"
        r_a1 = miner.parse(line, server_id="srv_a")
        r_a2 = miner.parse(line, server_id="srv_a")
        r_b1 = miner.parse(line, server_id="srv_b")

        # srv_a 第一次：new_template=True
        self.assertTrue(r_a1.is_new_template, "srv_a 首次应为新模板")
        # srv_a 第二次：new_template=False（同 namespace 已见过）
        self.assertFalse(r_a2.is_new_template, "srv_a 第二次不应为新模板")
        # srv_b 第一次：仍应是 new_template=True（独立 namespace）
        self.assertTrue(
            r_b1.is_new_template,
            "srv_b 首次应仍为新模板（namespace 隔离）",
        )
        # 两个 server 的 template_id 可以相同（Drain3 内部 ID），
        # 但 new_template 判定必须独立
        # 验证 snapshot 有两个 namespace
        snap = miner.snapshot()
        self.assertIn("srv_a", snap["namespaces"])
        self.assertIn("srv_b", snap["namespaces"])

    def test_jsonl_otel_dict_survives_end_to_end(self):
        """#5 E2E: 通过 DiskObservationStore 落盘的 JSONL 中 context.otel 仍是嵌套 dict。"""
        _install_astrbot_stubs()
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = Path(tmp) / "store"
            config = MineSentinelConfig.from_dict({
                "storage": {"enabled": True},
            })
            store = DiskObservationStore(config, Path(store_dir))

            # 构造一条带深层 OTel 嵌套的 observation payload
            observation = {
                "eventId": "e2e-1",
                "kind": "SERVER_LOG",
                "timestamp": int(time.time() * 1000),
                "serverId": "srv",
                "serverName": "Srv",
                "content": "[ERROR]: connection reset",
                "tags": ["server_log", "error"],
                "context": {
                    "level": "ERROR",
                    "otel": {
                        "severityNumber": 17,
                        "severityText": "ERROR",
                        "eventName": "network.error",
                        "body": "[ERROR]: connection reset",
                        "resource": {"service.name": "minecraft-srv"},
                        "attributes": {
                            "template.id": "T1",
                            "loop.suppressed": 0,
                        },
                    },
                },
            }
            store.add_batch("srv", {"observations": [observation]})

            # 读回 JSONL 文件，验证 otel 是嵌套 dict
            jsonl_files = list(Path(store_dir).rglob("*.jsonl"))
            self.assertTrue(jsonl_files, "应至少有一个 JSONL 文件")
            lines = []
            for f in jsonl_files:
                lines.extend(f.read_text(encoding="utf-8").strip().splitlines())
            self.assertTrue(lines, "JSONL 文件应有内容")
            parsed = json.loads(lines[-1])

            # 关键断言：otel 是 dict，不是字符串
            self.assertIsInstance(
                parsed["context"]["otel"], dict,
                "落盘 JSONL 的 context.otel 应是 dict，不是字符串",
            )
            self.assertEqual(parsed["context"]["otel"]["eventName"], "network.error")
            self.assertIsInstance(
                parsed["context"]["otel"]["resource"], dict,
                "otel.resource 应是 dict",
            )
            self.assertEqual(
                parsed["context"]["otel"]["resource"]["service.name"],
                "minecraft-srv",
            )
            self.assertIsInstance(
                parsed["context"]["otel"]["attributes"], dict,
                "otel.attributes 应是 dict",
            )
            self.assertEqual(parsed["context"]["otel"]["attributes"]["template.id"], "T1")


class MineSentinelRustPythonEquivalenceTests(unittest.TestCase):
    """Rust 路径与纯 Python fallback 的输出等价性验证。

    这组测试只在真实 mine_sentinel_rs wheel 已安装时才做实质断言；
    stub 模式下跳过（因为 stub 本身就是 Python 实现，比较无意义）。
    目的：验证 PR6 的 P0/P1-A/B 修复——Rust 可选化 + 热路径接入——
    两条路径行为完全等价。
    """

    def _has_real_rust(self) -> bool:
        try:
            import mine_sentinel_rs  # noqa: F401
            # stub 是 types.ModuleType，真实扩展的模块名是 builtins
            mod = sys.modules.get("mine_sentinel_rs")
            return mod is not None and not hasattr(mod, "_is_stub")
        except ImportError:
            return False

    def _make_record(self):
        from services.mine_sentinel.models import ObservationRecord

        return ObservationRecord(
            event_id="",
            kind="SERVER_LOG",
            timestamp=1700000000000,
            server_id="survival",
            server_name="Survival",
            backend_server="",
            proxy_id="",
            player_name="Steve",
            player_uuid_hash="abc123",
            content="[ERROR]: Connection reset by peer: io.netty.channel.unix.Errors",
            tags=["server_log", "runtime_log", "error", "network"],
            context={
                "level": "ERROR",
                "fingerprint": "fp123",
                "templateId": "T5",
                "template": "<*> ERROR]: Connection reset",
                "templateSize": 3,
                "anomalyScore": 0.75,
                "otel": {
                    "timestamp": 1700000000000,
                    "observedTimestamp": 1700000000123,
                    "severityText": "ERROR",
                    "severityNumber": 17,
                    "body": "[ERROR]: Connection reset by peer",
                    "eventName": "network.error",
                    "resource": {"service.name": "minecraft-survival"},
                    "attributes": {
                        "template.id": "T5",
                        "loop.suppressed": 0,
                    },
                },
            },
            raw={},
        )

    def test_codec_rust_equals_python_normalize_and_json(self):
        """Rust 与纯 Python 路径的 normalize_record / record_to_json / json_line 输出应完全相等。"""
        if not self._has_real_rust():
            self.skipTest("需要真实 mine_sentinel_rs wheel 才能验证等价性")

        from services.mine_sentinel.storage import codec as codec_mod
        from services.mine_sentinel.models import MineSentinelConfig

        cfg = MineSentinelConfig.from_dict({})

        # 路径 1：Rust（_HAS_RUST=True，_rs 不为 None）
        codec_rust = ObservationRecordCodec(cfg) if False else codec_mod.ObservationRecordCodec(cfg)
        self.assertTrue(codec_rust.uses_native, "Rust 路径应启用")

        rec_rust = self._make_record()
        codec_rust.normalize_record(rec_rust)
        json_rust = codec_rust.record_to_json(rec_rust)
        line_rust = codec_rust.json_line(rec_rust)
        key_rust = codec_rust.dedupe_key(rec_rust)

        # 路径 2：纯 Python（强制 _rs=None）
        codec_py = codec_mod.ObservationRecordCodec(cfg)
        codec_py._rs = None  # 强制走纯 Python fallback
        self.assertFalse(codec_py.uses_native)

        rec_py = self._make_record()
        codec_py.normalize_record(rec_py)
        json_py = codec_py.record_to_json(rec_py)
        line_py = codec_py.json_line(rec_py)
        key_py = codec_py.dedupe_key(rec_py)

        # 关键断言：两条路径输出必须完全相等
        self.assertEqual(rec_rust.content, rec_py.content, "content 不一致")
        self.assertEqual(rec_rust.tags, rec_py.tags, "tags 不一致")
        self.assertEqual(rec_rust.context, rec_py.context, "context 不一致（含 OTel 嵌套）")
        self.assertEqual(json_rust, json_py, "record_to_json 输出不一致")
        self.assertEqual(line_rust, line_py, "json_line 输出不一致")
        self.assertEqual(key_rust, key_py, "dedupe_key 不一致")

    def test_codec_rust_equals_python_with_long_content_and_many_tags(self):
        """超长 content / 超多 tags / 深层嵌套 OTel 下两条路径仍应相等。"""
        if not self._has_real_rust():
            self.skipTest("需要真实 mine_sentinel_rs wheel 才能验证等价性")

        from services.mine_sentinel.storage import codec as codec_mod
        from services.mine_sentinel.models import MineSentinelConfig, ObservationRecord

        cfg = MineSentinelConfig.from_dict({})

        long_content = ("[WARN]: spam spam " * 500) + " trailing tail"
        many_tags = [f"tag_{i}" for i in range(50)]
        deep_otel = {
            "severityNumber": 13,
            "body": "x" * 5000,
            "resource": {"service.name": "s", "host.id": "h" * 200},
            "attributes": {f"k{i}": f"v{i}" for i in range(30)},
        }

        codec_rust = codec_mod.ObservationRecordCodec(cfg)
        self.assertTrue(codec_rust.uses_native)
        codec_py = codec_mod.ObservationRecordCodec(cfg)
        codec_py._rs = None

        for codec, label in [(codec_rust, "rust"), (codec_py, "py")]:
            rec = ObservationRecord(
                event_id="", kind="SERVER_LOG", timestamp=1700000000000,
                server_id="srv", server_name="Srv", content=long_content,
                tags=many_tags,
                context={"level": "WARN", "otel": deep_otel},
                raw={},
            )
            codec.normalize_record(rec)
            json_out = codec.record_to_json(rec)
            line = codec.json_line(rec)
            if label == "rust":
                rec_rust, json_rust, line_rust = rec, json_out, line
            else:
                rec_py, json_py, line_py = rec, json_out, line

        self.assertEqual(rec_rust.content, rec_py.content)
        self.assertEqual(rec_rust.tags, rec_py.tags)
        self.assertEqual(rec_rust.context, rec_py.context)
        self.assertEqual(json_rust, json_py)
        self.assertEqual(line_rust, line_py)
        # OTel 仍应是 dict（不是字符串）
        self.assertIsInstance(json_rust["context"]["otel"], dict)
        self.assertIsInstance(json_rust["context"]["otel"]["attributes"], dict)

    def test_observation_priority_rust_equals_python(self):
        """observation_priority_score 的 Rust 与纯 Python 路径应给出相同分数。"""
        if not self._has_real_rust():
            self.skipTest("需要真实 mine_sentinel_rs wheel 才能验证等价性")

        from services.mine_sentinel import observation_priority as op_mod
        from services.mine_sentinel.models import ObservationRecord

        cases = [
            ("[ERROR]: something failed", ["server_log", "error"], 5.0),
            ("[INFO]: player joined", ["server_log", "info"], 1.0),
            ("[WARN]: connection reset", ["server_log", "warn", "network"], 5.0),
            ("[FATAL]: crash", ["server_log", "fatal"], 5.0),
            ("player was banned", ["server_log", "ban"], 5.0),
            ("normal chat message", ["server_log"], 1.0),
        ]

        # Rust 路径
        self.assertTrue(op_mod._HAS_RUST, "应启用 Rust 路径")
        for content, tags, expected in cases:
            rec = ObservationRecord(
                event_id="", kind="SERVER_LOG", timestamp=1700000000000,
                server_id="srv", server_name="Srv", content=content,
                tags=tags, context={"level": "INFO"}, raw={},
            )
            score_rust = op_mod.observation_priority_score(rec)
            self.assertEqual(
                score_rust, expected,
                f"Rust 路径 score 错误: content={content!r} tags={tags}",
            )

        # 纯 Python 路径（强制 _HAS_RUST=False）
        original = op_mod._HAS_RUST
        try:
            op_mod._HAS_RUST = False
            for content, tags, expected in cases:
                rec = ObservationRecord(
                    event_id="", kind="SERVER_LOG", timestamp=1700000000000,
                    server_id="srv", server_name="Srv", content=content,
                    tags=tags, context={"level": "INFO"}, raw={},
                )
                score_py = op_mod.observation_priority_score(rec)
                self.assertEqual(
                    score_py, expected,
                    f"Python 路径 score 错误: content={content!r} tags={tags}",
                )
        finally:
            op_mod._HAS_RUST = original

    def test_non_server_log_kind_returns_zero(self):
        """非 SERVER_LOG kind 的 priority 应为 0（两条路径一致）。"""
        from services.mine_sentinel import observation_priority as op_mod
        from services.mine_sentinel.models import ObservationRecord

        rec = ObservationRecord(
            event_id="", kind="PLAYER_CHAT", timestamp=1700000000000,
            server_id="srv", server_name="Srv", content="error failed crash",
            tags=["error"], context={}, raw={},
        )
        # 即便 content 含 error，kind 不是 SERVER_LOG 也应返回 0
        original = op_mod._HAS_RUST
        try:
            if op_mod._HAS_RUST:
                self.assertEqual(op_mod.observation_priority_score(rec), 0.0)
            op_mod._HAS_RUST = False
            self.assertEqual(op_mod.observation_priority_score(rec), 0.0)
        finally:
            op_mod._HAS_RUST = original


class MineSentinelConfigExposureTests(unittest.TestCase):
    """验证 PR7 新增的 anomaly/template 配置项能从 _conf_schema 透传到单例。"""

    def test_runtime_log_config_parses_anomaly_and_template_params(self):
        """MineSentinelRuntimeLogConfig 应解析 4 个新参数。"""
        config = MineSentinelConfig.from_dict({
            "runtime_log": {
                "template_max_namespaces": 8,
                "anomaly_max_templates_per_server": 100,
                "anomaly_inactive_template_ttl_hours": 12,
                "anomaly_cleanup_interval": 50,
            }
        })
        rt = config.runtime_log
        self.assertEqual(rt.template_max_namespaces, 8)
        self.assertEqual(rt.anomaly_max_templates_per_server, 100)
        self.assertEqual(rt.anomaly_inactive_template_ttl_hours, 12)
        self.assertEqual(rt.anomaly_cleanup_interval, 50)

    def test_runtime_log_config_uses_defaults_when_absent(self):
        """无配置时应使用代码默认值。"""
        config = MineSentinelConfig.from_dict({})
        rt = config.runtime_log
        self.assertEqual(rt.template_max_namespaces, 16)
        self.assertEqual(rt.anomaly_max_templates_per_server, 500)
        self.assertEqual(rt.anomaly_inactive_template_ttl_hours, 24)
        self.assertEqual(rt.anomaly_cleanup_interval, 200)

    def test_get_template_miner_accepts_max_namespaces(self):
        """get_template_miner 首次调用应接受 max_namespaces 参数。"""
        from services.mine_sentinel.template_miner import (
            get_template_miner, reset_template_miner,
        )
        reset_template_miner()
        try:
            miner = get_template_miner(max_namespaces=4)
            self.assertEqual(miner._max_namespaces, 4)
            # 再次调用（无参数）应返回同一实例
            self.assertIs(get_template_miner(), miner)
        finally:
            reset_template_miner()

    def test_get_anomaly_detector_accepts_config_params(self):
        """get_anomaly_detector 首次调用应接受 3 个 config 参数。"""
        from services.mine_sentinel.anomaly_detector import (
            get_anomaly_detector, reset_anomaly_detector,
        )
        reset_anomaly_detector()
        try:
            detector = get_anomaly_detector(
                max_templates_per_server=50,
                inactive_template_ttl_hours=6,
                cleanup_interval=10,
            )
            self.assertEqual(detector._max_templates_per_server, 50)
            self.assertEqual(detector._inactive_ttl_ms, 6 * 3600 * 1000)
            self.assertEqual(detector._cleanup_interval, 10)
            # 再次调用应返回同一实例
            self.assertIs(get_anomaly_detector(), detector)
        finally:
            reset_anomaly_detector()


class MineSentinelOffsetIndexTests(unittest.TestCase):
    """验证 PR9 P0-2 JSONL offset 索引的正确性和集成。"""

    def test_maybe_index_respects_line_interval(self):
        """每 line_interval 行才记录一条索引。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            idx = JsonlOffsetIndex(
                Path(tmp_dir) / "test.idx",
                line_interval=3,
                time_interval_ms=10_000_000,  # 不会触发
            )
            # 前 2 行不索引，第 3 行索引
            self.assertFalse(idx.maybe_index(1000, 0))
            self.assertFalse(idx.maybe_index(1001, 10))
            self.assertTrue(idx.maybe_index(1002, 20))
            # 又 2 行不索引，第 3 行索引
            self.assertFalse(idx.maybe_index(1003, 30))
            self.assertFalse(idx.maybe_index(1004, 40))
            self.assertTrue(idx.maybe_index(1005, 50))
            self.assertEqual(idx.entry_count, 2)

    def test_maybe_index_respects_time_interval(self):
        """时间间隔到了即使行数不够也索引。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            idx = JsonlOffsetIndex(
                Path(tmp_dir) / "test.idx",
                line_interval=1000,  # 不会触发
                time_interval_ms=5000,
            )
            # 第一行不会因 time_gap 触发（_last_indexed_ts == 0）
            self.assertFalse(idx.maybe_index(1000, 0))
            # 手动制造第一条索引
            idx._timestamps.append(1000)
            idx._offsets.append(0)
            idx._last_indexed_ts = 1000
            # 第二行：time_gap=1000 < 5000 → 不触发
            self.assertFalse(idx.maybe_index(2000, 10))
            # 第三行：time_gap=5000 >= 5000 → 触发
            self.assertTrue(idx.maybe_index(6000, 20))
            self.assertEqual(idx.entry_count, 2)

    def test_seek_offset_binary_search(self):
        """seek_offset 应返回 cutoff 前最近的 offset。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            idx = JsonlOffsetIndex(
                Path(tmp_dir) / "test.idx",
                line_interval=1,  # 每行都索引，便于测试
                time_interval_ms=10_000_000,
            )
            # 模拟写入 5 条记录，offset 分别为 0, 10, 20, 30, 40
            for i, (ts, off) in enumerate(
                [(1000, 0), (2000, 10), (3000, 20), (4000, 30), (5000, 40)]
            ):
                idx.maybe_index(ts, off)

            # cutoff=2500：第一个 >= 2500 的是 ts=3000 (idx=2)，
            # 返回前一个 ts=2000 的 offset=10
            self.assertEqual(idx.seek_offset(2500), 10)

            # cutoff=3000：bisect_left 找到 idx=2 (ts=3000)，
            # 返回前一个 ts=2000 的 offset=10
            self.assertEqual(idx.seek_offset(3000), 10)

            # cutoff=1000：bisect_left 找到 idx=0 (ts=1000)，
            # idx==0 → 返回 0（从头扫）
            self.assertEqual(idx.seek_offset(1000), 0)

            # cutoff=500：所有 ts >= cutoff → 返回 0
            self.assertEqual(idx.seek_offset(500), 0)

            # cutoff=6000：所有 ts < cutoff → 返回最后一个 offset=40
            self.assertEqual(idx.seek_offset(6000), 40)

    def test_flush_and_reload_roundtrip(self):
        """flush 后 reload 应恢复全部索引条目。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            idx_path = Path(tmp_dir) / "test.idx"
            idx = JsonlOffsetIndex(idx_path, line_interval=1)
            for i in range(10):
                idx.maybe_index(1000 + i * 100, i * 50)
            idx.flush()
            self.assertTrue(idx_path.exists())

            # 新实例 reload
            idx2 = JsonlOffsetIndex(idx_path)
            idx2.load()
            self.assertEqual(idx2.entry_count, 10)
            self.assertEqual(idx2.seek_offset(1500), 200)  # ts=1400 的 offset

    def test_flush_is_append_only(self):
        """多次 flush 只追加新条目，不重写全文件。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            idx_path = Path(tmp_dir) / "test.idx"
            idx = JsonlOffsetIndex(idx_path, line_interval=1)
            # 第一次 flush 3 条
            for i in range(3):
                idx.maybe_index(1000 + i * 100, i * 10)
            idx.flush()
            first_size = idx_path.stat().st_size

            # 第二次 flush 2 条
            for i in range(3, 5):
                idx.maybe_index(1000 + i * 100, i * 10)
            idx.flush()
            second_size = idx_path.stat().st_size
            self.assertGreater(second_size, first_size)

            # reload 验证全部 5 条
            idx2 = JsonlOffsetIndex(idx_path)
            idx2.load()
            self.assertEqual(idx2.entry_count, 5)

    def test_seek_offset_empty_index(self):
        """空索引应返回 0（从头扫描）。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            idx = JsonlOffsetIndex(Path(tmp_dir) / "nonexistent.idx")
            self.assertEqual(idx.seek_offset(12345), 0)
            self.assertTrue(idx.is_empty)

    def test_store_add_batch_creates_index(self):
        """add_batch 写入 JSONL 后应同时生成 .idx 索引文件。"""
        config = MineSentinelConfig.from_dict({})
        now = int(time.time() * 1000)
        # 300 条记录，超过默认 line_interval=256，会触发至少 1 条索引
        payload = {
            "serverId": "survival",
            "observations": [
                {
                    "eventId": f"log-{i}",
                    "kind": "SERVER_LOG",
                    "timestamp": now + i * 1000,
                    "serverId": "survival",
                    "content": f"[INFO]: line {i}",
                    "tags": ["server_log"],
                    "context": {"level": "INFO"},
                }
                for i in range(300)
            ],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = DiskObservationStore(config, Path(tmp_dir))
            store.add_batch("survival", payload)

            # 找到 JSONL 文件
            jsonl_files = list(Path(tmp_dir).glob("observations/*/*.jsonl"))
            self.assertEqual(len(jsonl_files), 1)
            jsonl_path = jsonl_files[0]

            # .idx 文件应存在（300 条记录 > 256 line_interval，触发索引）
            idx_path = jsonl_path.with_suffix(".idx")
            self.assertTrue(idx_path.exists(), f"Index file {idx_path} should exist")

            # 索引应有条目
            idx = JsonlOffsetIndex(idx_path)
            idx.load()
            self.assertGreater(idx.entry_count, 0)

    def test_recent_window_uses_index_correctly(self):
        """recent_window 使用索引 seek 后仍返回正确的窗口记录。"""
        config = MineSentinelConfig.from_dict({})
        base_ts = int(time.time() * 1000)

        # 写入 300 条记录，时间跨度 300 秒（5 分钟）
        observations = []
        for i in range(300):
            observations.append({
                "eventId": f"log-{i}",
                "kind": "SERVER_LOG",
                "timestamp": base_ts - (300 - i) * 1000,
                "serverId": "survival",
                "content": f"[INFO]: line {i}",
                "tags": ["server_log"],
                "context": {"level": "INFO"},
            })

        with tempfile.TemporaryDirectory() as tmp_dir:
            store = DiskObservationStore(config, Path(tmp_dir))
            store.add_batch("survival", {"serverId": "survival", "observations": observations})

            # 读最近 1 分钟的窗口（recent(1, ...) = 1 minute window）
            records = store.recent(1, "survival")
            # 应只包含 timestamp >= base_ts - 60*1000 的记录
            cutoff = base_ts - 60 * 1000
            for r in records:
                self.assertGreaterEqual(r.timestamp, cutoff)

            # 验证索引文件确实被使用（有索引条目）
            jsonl_path = next(Path(tmp_dir).glob("observations/*/*.jsonl"))
            idx = JsonlOffsetIndex(jsonl_path.with_suffix(".idx"))
            idx.load()
            self.assertGreater(idx.entry_count, 0)

    def test_export_recent_uses_index(self):
        """export_recent 使用索引后导出内容与无索引一致。"""
        config = MineSentinelConfig.from_dict({})
        base_ts = int(time.time() * 1000)

        observations = []
        for i in range(300):
            observations.append({
                "eventId": f"log-{i}",
                "kind": "SERVER_LOG",
                "timestamp": base_ts - (300 - i) * 1000,
                "serverId": "survival",
                "content": f"[INFO]: line {i}",
                "tags": ["server_log"],
                "context": {"level": "INFO"},
            })

        with tempfile.TemporaryDirectory() as tmp_dir:
            store = DiskObservationStore(config, Path(tmp_dir))
            store.add_batch("survival", {"serverId": "survival", "observations": observations})

            # 导出最近 1 分钟的窗口
            export_path = store.export_recent(1, "survival")
            self.assertIsNotNone(export_path)
            self.assertTrue(export_path.exists())

            # 验证导出的记录都在窗口内
            records = []
            with export_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
            cutoff = base_ts - 60 * 1000
            for r in records:
                self.assertGreaterEqual(r["timestamp"], cutoff)
            self.assertGreater(len(records), 0)

    def test_cleanup_removes_idx_alongside_jsonl(self):
        """cleanup 应同时删除过期的 .jsonl 和 .idx 文件。"""
        from services.mine_sentinel.storage.paths import cleanup_old_files
        config = MineSentinelConfig.from_dict({})
        now = int(time.time() * 1000)

        with tempfile.TemporaryDirectory() as tmp_dir:
            obs_dir = Path(tmp_dir) / "observations"
            export_dir = Path(tmp_dir) / "exports"
            obs_dir.mkdir(parents=True)
            export_dir.mkdir(parents=True)

            # 创建一个 2 天前的 JSONL + IDX 文件（模拟过期）
            old_day = time.strftime(
                "%Y%m%d",
                time.localtime(time.time() - 2 * 86400),
            )
            server_dir = obs_dir / "survival"
            server_dir.mkdir(parents=True)
            old_jsonl = server_dir / f"{old_day}.jsonl"
            old_jsonl.write_text("dummy\n", encoding="utf-8")
            old_idx = server_dir / f"{old_day}.idx"
            old_idx.write_text("1000\t0\n", encoding="utf-8")

            # 创建今天的文件（不应被删）
            today = time.strftime("%Y%m%d", time.localtime())
            today_jsonl = server_dir / f"{today}.jsonl"
            today_jsonl.write_text("dummy\n", encoding="utf-8")
            today_idx = server_dir / f"{today}.idx"
            today_idx.write_text("1000\t0\n", encoding="utf-8")

            cleanup_old_files(obs_dir, export_dir, retention_minutes=480)

            self.assertFalse(old_jsonl.exists())
            self.assertFalse(old_idx.exists())
            self.assertTrue(today_jsonl.exists())
            self.assertTrue(today_idx.exists())

    def test_read_jsonl_window_with_index_matches_without(self):
        """有索引和无索引的 read_jsonl_window 结果应一致。"""
        config = MineSentinelConfig.from_dict({})
        base_ts = int(time.time() * 1000)
        observations = []
        for i in range(500):
            observations.append({
                "eventId": f"log-{i}",
                "kind": "SERVER_LOG",
                "timestamp": base_ts - (500 - i) * 1000,
                "serverId": "survival",
                "content": f"[INFO]: line {i}",
                "tags": ["server_log"],
                "context": {"level": "INFO"},
            })

        with tempfile.TemporaryDirectory() as tmp_dir:
            store = DiskObservationStore(config, Path(tmp_dir))
            store.add_batch("survival", {"serverId": "survival", "observations": observations})

            jsonl_path = next(Path(tmp_dir).glob("observations/*/*.jsonl"))
            # 1 分钟窗口
            cutoff = base_ts - 60 * 1000
            end = base_ts + 1

            # 无索引读取
            rows_no_idx = list(store.codec.read_jsonl_window(jsonl_path, cutoff, end))

            # 有索引读取
            idx = JsonlOffsetIndex.for_jsonl(jsonl_path)
            idx.load()
            rows_with_idx = list(store.codec.read_jsonl_window(
                jsonl_path, cutoff, end, index=idx
            ))

            self.assertEqual(len(rows_no_idx), len(rows_with_idx))
            for a, b in zip(rows_no_idx, rows_with_idx):
                self.assertEqual(a["eventId"], b["eventId"])


class MineSentinelExportGzipTests(unittest.TestCase):
    """验证 PR9 P1-3 export jsonl.gz + 同窗口复用。"""

    def test_export_records_gzip_format(self):
        """export_format=jsonl.gz 时应生成 .jsonl.gz 压缩文件。"""
        import gzip as gzip_module
        config = MineSentinelConfig.from_dict({
            "report": {"export_format": "jsonl.gz", "export_reuse_existing": False}
        })
        now = int(time.time() * 1000)
        records = [
            ObservationRecord(
                event_id=f"log-{i}",
                kind="SERVER_LOG",
                timestamp=now + i * 1000,
                server_id="survival",
                server_name="Survival",
                content=f"line {i}",
                tags=["server_log"],
            )
            for i in range(5)
        ]
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = DiskObservationStore(config, Path(tmp_dir))
            path = store.export_records(records, 60, "survival")
            self.assertIsNotNone(path)
            self.assertTrue(str(path).endswith(".jsonl.gz"))
            self.assertTrue(path.exists())

            # 验证 gzip 文件内容可正确解压读取
            with gzip_module.open(path, "rt", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
            self.assertEqual(len(lines), 5)
            data = json.loads(lines[0])
            self.assertEqual(data["eventId"], "log-0")

    def test_export_recent_gzip_format(self):
        """export_recent 在 jsonl.gz 模式下应生成压缩文件。"""
        import gzip as gzip_module
        config = MineSentinelConfig.from_dict({
            "report": {"export_format": "jsonl.gz", "export_reuse_existing": False}
        })
        base_ts = int(time.time() * 1000)
        observations = [
            {
                "eventId": f"log-{i}",
                "kind": "SERVER_LOG",
                "timestamp": base_ts - (10 - i) * 1000,
                "serverId": "survival",
                "content": f"line {i}",
                "tags": ["server_log"],
                "context": {"level": "INFO"},
            }
            for i in range(10)
        ]
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = DiskObservationStore(config, Path(tmp_dir))
            store.add_batch("survival", {"serverId": "survival", "observations": observations})
            path = store.export_recent(1, "survival")
            self.assertIsNotNone(path)
            self.assertTrue(str(path).endswith(".jsonl.gz"))

            with gzip_module.open(path, "rt", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
            self.assertGreater(len(lines), 0)

    def test_export_reuse_existing(self):
        """export_reuse_existing=True 时同窗口导出应复用已有文件。"""
        config = MineSentinelConfig.from_dict({
            "report": {"export_format": "jsonl", "export_reuse_existing": True}
        })
        now = int(time.time() * 1000)
        records = [
            ObservationRecord(
                event_id="log-1",
                kind="SERVER_LOG",
                timestamp=now,
                server_id="survival",
                server_name="Survival",
                content="line 1",
                tags=["server_log"],
            )
        ]
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = DiskObservationStore(config, Path(tmp_dir))
            # PR9 hotfix v5: 传固定 end_ms 模拟周期报告 retry（同窗口复用）。
            # 手动 report now 不传 end_ms，用当前时间，每次生成不同文件名。
            fixed_end_ms = int(time.time() * 1000)
            path1 = store.export_records(records, 60, "survival", end_ms=fixed_end_ms)
            self.assertIsNotNone(path1)
            self.assertTrue(path1.exists())

            # 记录文件修改时间
            mtime1 = path1.stat().st_mtime

            # 再次导出同窗口（相同 end_ms）——应复用
            path2 = store.export_records(records, 60, "survival", end_ms=fixed_end_ms)
            self.assertIsNotNone(path2)
            self.assertEqual(path1, path2)
            self.assertEqual(path2.stat().st_mtime, mtime1)

    def test_export_no_reuse_when_disabled(self):
        """export_reuse_existing=False 时应每次重新写。"""
        config = MineSentinelConfig.from_dict({
            "report": {"export_format": "jsonl", "export_reuse_existing": False}
        })
        now = int(time.time() * 1000)
        records = [
            ObservationRecord(
                event_id="log-1",
                kind="SERVER_LOG",
                timestamp=now,
                server_id="survival",
                server_name="Survival",
                content="line 1",
                tags=["server_log"],
            )
        ]
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = DiskObservationStore(config, Path(tmp_dir))
            # PR9 hotfix v5: 传固定 end_ms 使两次调用生成相同路径（毫秒级精度下，
            # 不传 end_ms 会因当前时间不同而生成不同文件名，无法验证 reuse 行为）。
            fixed_end_ms = int(time.time() * 1000)
            path1 = store.export_records(records, 60, "survival", end_ms=fixed_end_ms)
            self.assertIsNotNone(path1)
            self.assertTrue(path1.exists())
            original_content = path1.read_text(encoding="utf-8")
            self.assertIn("line 1", original_content)

            # 用不同记录再导出同窗口（相同 end_ms）——路径相同，但因 reuse=False，
            # 文件应被重写而不是复用旧内容。
            records2 = [
                ObservationRecord(
                    event_id="log-2",
                    kind="SERVER_LOG",
                    timestamp=now,
                    server_id="survival",
                    server_name="Survival",
                    content="line 2 rewritten",
                    tags=["server_log"],
                )
            ]
            path2 = store.export_records(records2, 60, "survival", end_ms=fixed_end_ms)
            self.assertIsNotNone(path2)
            self.assertEqual(path1, path2)  # 路径相同（相同 end_ms）
            rewritten_content = path2.read_text(encoding="utf-8")
            self.assertIn("line 2 rewritten", rewritten_content)
            self.assertNotIn("line 1", rewritten_content)  # 旧内容已被覆盖

    def test_cleanup_removes_gz_exports(self):
        """cleanup 应清理过期的 .jsonl.gz 导出文件。"""
        from services.mine_sentinel.storage.paths import cleanup_old_files
        with tempfile.TemporaryDirectory() as tmp_dir:
            obs_dir = Path(tmp_dir) / "observations"
            export_dir = Path(tmp_dir) / "exports"
            obs_dir.mkdir(parents=True)
            export_dir.mkdir(parents=True)

            # 创建一个过期的 .jsonl.gz 文件
            old_gz = export_dir / "old_export.jsonl.gz"
            old_gz.write_bytes(b"\x1f\x8b\x08\x00\x00\x00\x00\x00")
            old_mtime = time.time() - 2 * 3600  # 2 小时前
            import os
            os.utime(old_gz, (old_mtime, old_mtime))

            # 创建一个未过期的 .jsonl.gz 文件
            new_gz = export_dir / "new_export.jsonl.gz"
            new_gz.write_bytes(b"\x1f\x8b\x08\x00\x00\x00\x00\x00")

            cleanup_old_files(obs_dir, export_dir, retention_minutes=60)
            self.assertFalse(old_gz.exists())
            self.assertTrue(new_gz.exists())


class MineSentinelInfoDownsamplingTests(unittest.TestCase):
    """PR9: 普通 INFO 降采样（interesting-only 模式）"""

    def _make_source(self, server_id="srv"):
        from services.mine_sentinel.models import MineSentinelLogSourceConfig
        return MineSentinelLogSourceConfig(
            server_id=server_id,
            server_name=server_id,
            server_type="minecraft",
            log_file="/tmp/latest.log",
        )

    def _make_config(self, mode="interesting", track_info=False):
        from services.mine_sentinel.models import MineSentinelRuntimeLogConfig
        return MineSentinelRuntimeLogConfig(
            template_parse_mode=mode,
            anomaly_track_info=track_info,
        )

    def test_should_parse_all_mode_runs_full_pipeline(self):
        """mode=all 时所有级别都进 template/anomaly。"""
        from services.mine_sentinel.runtime_log import _should_parse_and_track
        cfg = self._make_config("all", track_info=True)
        # INFO
        run_t, run_a = _should_parse_and_track("INFO", "player joined", cfg)
        self.assertTrue(run_t)
        self.assertTrue(run_a)
        # WARN
        run_t, run_a = _should_parse_and_track("WARN", "something", cfg)
        self.assertTrue(run_t)
        self.assertTrue(run_a)

    def test_should_parse_all_mode_skips_anomaly_for_info_when_track_info_false(self):
        """mode=all + anomaly_track_info=False：INFO 不进 anomaly，WARN 仍进。"""
        from services.mine_sentinel.runtime_log import _should_parse_and_track
        cfg = self._make_config("all", track_info=False)
        run_t, run_a = _should_parse_and_track("INFO", "player joined", cfg)
        self.assertTrue(run_t)  # 仍解析模板
        self.assertFalse(run_a)  # 但不进 anomaly
        run_t, run_a = _should_parse_and_track("WARN", "something", cfg)
        self.assertTrue(run_t)
        self.assertTrue(run_a)  # WARN 始终进 anomaly

    def test_should_parse_warn_error_mode_skips_all_info(self):
        """mode=warn_error：INFO 完全跳过 template/anomaly。"""
        from services.mine_sentinel.runtime_log import _should_parse_and_track
        cfg = self._make_config("warn_error", track_info=True)
        run_t, run_a = _should_parse_and_track("INFO", "can't keep up!", cfg)
        self.assertFalse(run_t)
        self.assertFalse(run_a)
        run_t, run_a = _should_parse_and_track("ERROR", "boom", cfg)
        self.assertTrue(run_t)
        self.assertTrue(run_a)

    def test_should_parse_interesting_mode_keeps_interesting_info(self):
        """mode=interesting：命中关键词的 INFO 才进 template/anomaly。"""
        from services.mine_sentinel.runtime_log import _should_parse_and_track
        cfg = self._make_config("interesting", track_info=False)
        # 普通 INFO：跳过（第二个参数是 lowered content，与 _build_observation 调用一致）
        run_t, run_a = _should_parse_and_track("INFO", "steve joined the game", cfg)
        self.assertFalse(run_t)
        self.assertFalse(run_a)
        # 命中关键词的 INFO：保留
        run_t, run_a = _should_parse_and_track(
            "INFO", "can't keep up! running behind", cfg
        )
        self.assertTrue(run_t)
        self.assertTrue(run_a)
        # WARN：始终保留
        run_t, run_a = _should_parse_and_track("WARN", "plugin slow", cfg)
        self.assertTrue(run_t)
        self.assertTrue(run_a)

    def test_build_observation_marks_downsampled_info(self):
        """降采样的 INFO 应被标记 info_downsampled，不调用 drain3/anomaly。"""
        from services.mine_sentinel.runtime_log import _build_observation
        from services.mine_sentinel.template_miner import reset_template_miner
        from services.mine_sentinel.anomaly_detector import reset_anomaly_detector
        reset_template_miner()
        reset_anomaly_detector()
        cfg = self._make_config("warn_error", track_info=False)
        source = self._make_source()
        obs = _build_observation(
            source, Path("/tmp/latest.log"),
            "[14:00:00 INFO]: Steve joined the game",
            int(time.time() * 1000), 1000, runtime_config=cfg,
        )
        self.assertIn("info_downsampled", obs["tags"])
        self.assertTrue(obs["context"]["infoDownsampled"])
        # templateId 应为 fingerprint（降级），不是 drain3 cluster_id
        self.assertTrue(obs["context"]["templateId"])
        self.assertTrue(obs["context"]["templateFallback"])
        # anomaly 字段为零值
        self.assertEqual(obs["context"]["anomalyScore"], 0.0)
        self.assertIn("skipped", obs["context"]["anomalyReason"])

    def test_build_observation_keeps_warn_full_pipeline(self):
        """WARN 即便在 warn_error 模式下仍走完整 template/anomaly。"""
        from services.mine_sentinel.runtime_log import _build_observation
        from services.mine_sentinel.template_miner import reset_template_miner
        from services.mine_sentinel.anomaly_detector import reset_anomaly_detector
        reset_template_miner()
        reset_anomaly_detector()
        cfg = self._make_config("warn_error", track_info=False)
        source = self._make_source()
        obs = _build_observation(
            source, Path("/tmp/latest.log"),
            "[14:00:00 WARN]: something failed",
            int(time.time() * 1000), 1000, runtime_config=cfg,
        )
        self.assertNotIn("info_downsampled", obs["tags"])
        self.assertNotIn("infoDownsampled", obs.get("context", {}))


class MineSentinelShardedLockTests(unittest.TestCase):
    """PR9: template_miner / anomaly_detector per-server 分片锁"""

    def test_template_miner_per_server_locks_are_independent(self):
        """不同 server_id 应获得不同的锁实例。"""
        from services.mine_sentinel.template_miner import LogTemplateMiner
        miner = LogTemplateMiner()
        lock_a = miner._lock_for("srvA")
        lock_b = miner._lock_for("srvB")
        self.assertIsNot(lock_a, lock_b)
        # 同一 server_id 复用锁
        self.assertIs(miner._lock_for("srvA"), lock_a)

    def test_template_miner_resolve_namespace_overflow_falls_back_to_default(self):
        """超出 max_namespaces 时新 server_id 应回落到 default namespace。"""
        from services.mine_sentinel.template_miner import LogTemplateMiner
        miner = LogTemplateMiner(max_namespaces=2)
        # 占满 2 个 namespace
        ns1 = miner._resolve_namespace("srv1")
        self.assertEqual(ns1, "srv1")
        # 触发 parse 创建 miner
        if miner.available:
            miner.parse("line1", server_id="srv1")
            miner.parse("line2", server_id="srv2")
            # 第 3 个应回落到 default
            ns3 = miner._resolve_namespace("srv3")
            self.assertEqual(ns3, "default")
        else:
            # drain3 不可用时仍可验证 resolve 逻辑（不创建 miner 不超限）
            self.assertEqual(miner._resolve_namespace("srv3"), "srv3")

    def test_anomaly_detector_per_server_shards_are_independent(self):
        """不同 server_id 的 observe 应落到不同分片。"""
        from services.mine_sentinel.anomaly_detector import TemplateAnomalyDetector
        detector = TemplateAnomalyDetector()
        detector.observe("srvA", "T1", template="t", level="INFO")
        detector.observe("srvB", "T1", template="t", level="INFO")
        shard_a = detector._shard_for("srvA")
        shard_b = detector._shard_for("srvB")
        self.assertIsNot(shard_a, shard_b)
        self.assertIn("T1", shard_a.stats)
        self.assertIn("T1", shard_b.stats)
        # 两个分片各自的 stats 独立
        self.assertEqual(shard_a.server_id, "srvA")
        self.assertEqual(shard_b.server_id, "srvB")

    def test_anomaly_detector_snapshot_aggregates_across_shards(self):
        """snapshot 应聚合所有分片的统计。"""
        from services.mine_sentinel.anomaly_detector import TemplateAnomalyDetector
        detector = TemplateAnomalyDetector()
        detector.observe("srvA", "T1", template="t1", level="WARN")
        detector.observe("srvB", "T2", template="t2", level="ERROR")
        snap = detector.snapshot()
        self.assertEqual(snap["template_count"], 2)
        self.assertEqual(snap["per_server_count"].get("srvA"), 1)
        self.assertEqual(snap["per_server_count"].get("srvB"), 1)


class MineSentinelIoExecutorTests(unittest.TestCase):
    """PR9: 专用 bounded ThreadPoolExecutor"""

    def test_build_io_executor_returns_none_for_zero(self):
        from services.mine_sentinel.io_executor import build_io_executor
        self.assertIsNone(build_io_executor(0))
        self.assertIsNone(build_io_executor(-1))

    def test_build_io_executor_creates_pool_for_positive(self):
        from services.mine_sentinel.io_executor import build_io_executor
        exe = build_io_executor(2)
        try:
            self.assertIsNotNone(exe)
            self.assertEqual(exe._max_workers, 2)
        finally:
            exe.shutdown(wait=False)

    def test_executor_runner_runs_fn_in_pool(self):
        """executor_runner 提交的 fn 应在专用线程池执行。"""
        import asyncio
        import threading
        from services.mine_sentinel.io_executor import build_io_executor, executor_runner, shutdown_io_executor

        exe = build_io_executor(1)
        try:
            runner = executor_runner(exe)

            def _who():
                return threading.current_thread().name

            async def _main():
                name = await runner(_who)
                return name

            name = asyncio.run(_main())
            self.assertIn("mine-sentinel-io", name)
        finally:
            shutdown_io_executor(exe)

    def test_executor_runner_falls_back_to_to_thread_when_none(self):
        """executor 为 None 时应回退到 asyncio.to_thread。"""
        import asyncio
        from services.mine_sentinel.io_executor import executor_runner

        runner = executor_runner(None)

        def _fn(x):
            return x * 2

        async def _main():
            return await runner(_fn, 21)

        result = asyncio.run(_main())
        self.assertEqual(result, 42)


class MineSentinelGzScanCacheTests(unittest.TestCase):
    """PR9: hourly .gz 已扫描缓存 + 文件名日期预过滤"""

    def test_file_date_overlaps_hour_same_day(self):
        from services.mine_sentinel.runtime_log import _file_date_overlaps_hour
        from datetime import date
        # 2024-01-15 14:00 ~ 15:00
        start_ms = 1705327200000  # 2024-01-15 14:00 UTC
        end_ms = start_ms + 3600_000
        self.assertTrue(_file_date_overlaps_hour(date(2024, 1, 15), start_ms, end_ms))

    def test_file_date_overlaps_hour_previous_day_for_boundary(self):
        from services.mine_sentinel.runtime_log import _file_date_overlaps_hour
        from datetime import date
        start_ms = 1705327200000  # 2024-01-15 14:00 UTC
        end_ms = start_ms + 3600_000
        # 前一天（跨日边界归档）
        self.assertTrue(_file_date_overlaps_hour(date(2024, 1, 14), start_ms, end_ms))

    def test_file_date_overlaps_hour_skips_far_old(self):
        from services.mine_sentinel.runtime_log import _file_date_overlaps_hour
        from datetime import date
        start_ms = 1705327200000  # 2024-01-15 14:00 UTC
        end_ms = start_ms + 3600_000
        # 一周前的归档应跳过
        self.assertFalse(_file_date_overlaps_hour(date(2024, 1, 8), start_ms, end_ms))

    def test_file_date_overlaps_hour_none_date_is_conservative_keep(self):
        from services.mine_sentinel.runtime_log import _file_date_overlaps_hour
        start_ms = 1705327200000
        end_ms = start_ms + 3600_000
        # latest.log 无文件名日期，保守保留
        self.assertTrue(_file_date_overlaps_hour(None, start_ms, end_ms))

    def test_gz_scan_cache_reuses_same_hour(self):
        """同一 (path, mtime, hour_start) 重复扫描应命中缓存。"""
        from services.mine_sentinel.runtime_log import (
            _gz_scan_cache_get,
            _gz_scan_cache_put,
            _gz_scan_cache,
        )
        _gz_scan_cache.clear()
        path = Path("/tmp/2024-01-15-1.log.gz")
        mtime = 1234567890
        hour_start = 1705327200000
        rows = [("line1", hour_start + 1000, str(path))]
        _gz_scan_cache_put(path, mtime, hour_start, rows)
        cached = _gz_scan_cache_get(path, mtime, hour_start)
        self.assertIsNotNone(cached)
        self.assertEqual(cached, rows)
        # 不同 mtime 或 hour 不命中
        self.assertIsNone(_gz_scan_cache_get(path, mtime + 1, hour_start))
        self.assertIsNone(_gz_scan_cache_get(path, mtime, hour_start + 3600_000))

    def test_read_hour_log_lines_skips_far_old_archives(self):
        """文件名日期明显早于目标小时的归档应被跳过，不打开。"""
        import gzip as gzip_module
        from services.mine_sentinel.runtime_log import read_hour_log_lines
        from services.mine_sentinel.models import MineSentinelLogSourceConfig
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            logs = tmp_path / "logs"
            logs.mkdir(parents=True)
            # 创建一个一周前的归档，内容会触发异常如果被打开
            old_archive = logs / "2024-01-08-1.log.gz"
            with gzip_module.open(old_archive, "wt", encoding="utf-8") as f:
                f.write("[14:00:00] [Server thread/INFO]: old\n")
            # latest.log 留空
            (logs / "latest.log").write_text("", encoding="utf-8")
            source = MineSentinelLogSourceConfig(
                server_id="srv", server_name="srv", root=str(tmp_path)
            )
            # 目标小时：2024-01-15 14:00
            hour_start = 1705327200000
            hour_end = hour_start + 3600_000
            rows = read_hour_log_lines(source, hour_start, hour_end, max_lines=10)
            # 应该为空（old archive 被日期过滤跳过，latest.log 为空）
            self.assertEqual(rows, [])


class MineSentinelPr9HotfixTests(unittest.TestCase):
    """验证 PR9 hotfix 修复的正确性风险。"""

    def test_offset_index_detects_non_monotonic_and_disables_seek(self):
        """非单调时间戳应被检测并标记，seek_offset 返回 0。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            idx = JsonlOffsetIndex(
                Path(tmp_dir) / "test.idx",
                line_interval=1,  # 每行都索引
                time_interval_ms=10_000_000,
            )
            idx.load()
            self.assertTrue(idx.is_monotonic)
            # 单调递增
            idx.maybe_index(1000, 0)
            idx.maybe_index(1100, 10)
            self.assertTrue(idx.is_monotonic)
            # 时间戳回退 → 标记非单调
            idx.maybe_index(900, 20)
            self.assertFalse(idx.is_monotonic)
            # seek_offset 应返回 0（禁用 seek）
            self.assertEqual(idx.seek_offset(950), 0)

    def test_offset_index_persists_non_monotonic_header(self):
        """非单调标记应持久化到 .idx 文件头部，重载后仍生效。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            idx_path = Path(tmp_dir) / "test.idx"
            idx = JsonlOffsetIndex(idx_path, line_interval=1, time_interval_ms=10_000_000)
            idx.maybe_index(1000, 0)
            idx.maybe_index(900, 10)  # 时间戳回退，触发非单调
            idx.flush()
            # 文件应包含 #monotonic\t0 头部
            content = idx_path.read_text(encoding="utf-8")
            self.assertIn("#monotonic\t0", content)
            # 重新加载
            idx2 = JsonlOffsetIndex(idx_path, line_interval=1, time_interval_ms=10_000_000)
            idx2.load()
            self.assertFalse(idx2.is_monotonic)

    def test_read_jsonl_window_does_not_break_on_non_monotonic(self):
        """非单调文件中，窗口内记录出现在 end_ms 之后时不应被漏掉。"""
        config = MineSentinelConfig.from_dict({})
        base_ts = int(time.time() * 1000)
        # 构造非单调 JSONL：第一条 ts=base_ts+100s（超出窗口），
        # 第二条 ts=base_ts-10s（窗口内），第三条 ts=base_ts+200s（超出窗口）
        rows_data = [
            {"eventId": "future1", "timestamp": base_ts + 100_000, "serverId": "s", "content": "future1"},
            {"eventId": "in_window", "timestamp": base_ts - 10_000, "serverId": "s", "content": "in"},
            {"eventId": "future2", "timestamp": base_ts + 200_000, "serverId": "s", "content": "future2"},
        ]
        with tempfile.TemporaryDirectory() as tmp_dir:
            jsonl_path = Path(tmp_dir) / "test.jsonl"
            with jsonl_path.open("w", encoding="utf-8") as f:
                for r in rows_data:
                    f.write(json.dumps(r) + "\n")
            # 标记为非单调的索引
            idx = JsonlOffsetIndex.for_jsonl(jsonl_path)
            idx.load()
            idx._monotonic = False
            idx._monotonic_persisted = True
            cutoff = base_ts - 60_000
            end = base_ts + 1
            from services.mine_sentinel.storage.codec import ObservationRecordCodec
            codec = ObservationRecordCodec(config)
            rows = list(codec.read_jsonl_window(jsonl_path, cutoff, end, index=idx))
            # 必须包含 in_window，即使它出现在 future1 之后
            ids = [r["eventId"] for r in rows]
            self.assertIn("in_window", ids)

    def test_export_path_label_always_in_filename(self):
        """label 非空时始终加入文件名，即使基础路径不存在。"""
        from services.mine_sentinel.storage.paths import export_path
        with tempfile.TemporaryDirectory() as tmp_dir:
            export_dir = Path(tmp_dir) / "exports"
            export_dir.mkdir()
            # 第一次调用，基础路径不存在
            p1 = export_path(export_dir, 30, "srv", label="alert", now=1700000000)
            self.assertIn("alert", p1.name)
            # 无 label 时不包含
            p2 = export_path(export_dir, 30, "srv", label="", now=1700000000)
            self.assertNotIn("alert", p2.name)
            # 不同 label 生成不同文件名
            p3 = export_path(export_dir, 30, "srv", label="manual", now=1700000000)
            self.assertNotEqual(p1.name, p3.name)

    def test_rotation_preserves_backlog(self):
        """文件轮转时 backlog 不应被清空，partial_line 提升为完整行。"""
        _install_astrbot_stubs()
        from services.mine_sentinel.runtime_log import (
            MineSentinelRuntimeLogTailer,
            MineSentinelRuntimeLogConfig,
            _SourceState,
        )
        from services.mine_sentinel.models import MineSentinelLogSourceConfig
        collected = []

        async def batch_handler(server_id, payload):
            for obs in payload.get("observations", []):
                collected.append(obs)

        async def io_runner(fn, *args):
            return fn(*args)

        config = MineSentinelRuntimeLogConfig(
            enabled=True,
            poll_interval_seconds=1,
            max_lines_per_poll=2,
            max_bytes_per_poll=4096,
            backfill_on_start=False,
            initial_lines=0,
        )
        source = MineSentinelLogSourceConfig(
            server_id="srv",
            server_type="minecraft",
            log_file=None,
            root=None,
            logs_dir=None,
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "latest.log"
            # 写入 5 行，max_lines_per_poll=2，会产生 3 行 backlog
            log_path.write_text(
                "[12:00:00 INFO]: line 0\n"
                "[12:00:01 INFO]: line 1\n"
                "[12:00:02 INFO]: line 2\n"
                "[12:00:03 INFO]: line 3\n"
                "[12:00:04 INFO]: line 4\n",
                encoding="utf-8",
            )
            tailer = MineSentinelRuntimeLogTailer(config, batch_handler, io_runner=io_runner)
            state = _SourceState(source=source, log_file=log_path)
            state.position = 0  # 从头读，模拟首轮 poll

            async def run():
                await tailer._poll_source(state)
                # 第一轮：读 5 行，前 2 进 lines，后 3 进 backlog
                self.assertEqual(
                    len(state.backlog), 3,
                    f"首轮应产生 3 行 backlog，实际 {len(state.backlog)}",
                )
                # 模拟轮转：文件截断为更小内容
                log_path.write_text("[13:00:00 INFO]: new file\n", encoding="utf-8")
                await tailer._poll_source(state)
                # 轮转后 backlog 应保留（不被清空），position 归零
                self.assertGreater(len(state.backlog), 0, "轮转后 backlog 不应被清空")
                self.assertEqual(state.position, 0)

            asyncio.run(run())

    def test_gz_scan_cache_lru_eviction(self):
        """LRU 缓存满时应淘汰最久未用的条目，而非按 key 字典序。"""
        _install_astrbot_stubs()
        from services.mine_sentinel.runtime_log import (
            _gz_scan_cache,
            _gz_scan_cache_get,
            _gz_scan_cache_put,
            _GZ_SCAN_CACHE_MAX_ENTRIES,
        )
        _gz_scan_cache.clear()
        try:
            # 填满缓存
            for i in range(_GZ_SCAN_CACHE_MAX_ENTRIES):
                _gz_scan_cache_put(Path(f"/a/{i}.log.gz"), i, 0, [(f"line{i}", 0, f"/a/{i}.log.gz")])
            self.assertEqual(len(_gz_scan_cache), _GZ_SCAN_CACHE_MAX_ENTRIES)
            # 访问第 0 个（最旧），使其变最近使用
            _gz_scan_cache_get(Path("/a/0.log.gz"), 0, 0)
            # 插入新条目，应淘汰最久未用的（第 1 个，而非第 0 个）
            _gz_scan_cache_put(Path("/a/new.log.gz"), 99, 0, [("new", 0, "/a/new.log.gz")])
            self.assertEqual(len(_gz_scan_cache), _GZ_SCAN_CACHE_MAX_ENTRIES)
            # 第 0 个应仍存在（最近访问过）
            self.assertIsNotNone(_gz_scan_cache_get(Path("/a/0.log.gz"), 0, 0))
            # 第 1 个应被淘汰（最久未用）
            self.assertIsNone(_gz_scan_cache_get(Path("/a/1.log.gz"), 0, 0))
        finally:
            _gz_scan_cache.clear()

    def test_enum_validation_invalid_export_format_falls_back(self):
        """非法 export_format 应回退到默认 jsonl。"""
        _install_astrbot_stubs()
        from services.mine_sentinel.models import MineSentinelConfig
        cfg = MineSentinelConfig.from_dict({
            "report": {"export_format": "csv"},
        })
        self.assertEqual(cfg.report.export_format, "jsonl")

    def test_enum_validation_invalid_template_parse_mode_falls_back(self):
        """非法 template_parse_mode 应回退到默认 all。"""
        _install_astrbot_stubs()
        from services.mine_sentinel.models import MineSentinelConfig
        cfg = MineSentinelConfig.from_dict({
            "runtime_log": {"template_parse_mode": "verbose"},
        })
        self.assertEqual(cfg.runtime_log.template_parse_mode, "all")

    def test_enum_validation_valid_values_preserved(self):
        """合法的枚举值应被保留（大小写不敏感）。"""
        _install_astrbot_stubs()
        from services.mine_sentinel.models import MineSentinelConfig
        cfg = MineSentinelConfig.from_dict({
            "runtime_log": {"template_parse_mode": "Interesting"},
            "report": {"export_format": "JSONL.GZ"},
        })
        self.assertEqual(cfg.runtime_log.template_parse_mode, "interesting")
        self.assertEqual(cfg.report.export_format, "jsonl.gz")


class MineSentinelPr9HotfixV2Tests(unittest.TestCase):
    """验证 PR9 hotfix v2 修复的核心漏洞：_last_seen_ts 严格非单调检测。

    上一轮 hotfix 的 maybe_index() 只拿 timestamp 跟 _last_indexed_ts
    比较，但 _last_indexed_ts 只在真正写入索引条目时更新。如果乱序
    发生在两个索引点之间（1000 indexed → 1100/1200/1150 unindexed），
    1150 > 1000 不会被检测到，文件仍被标记 monotonic，读取时 early
    break 仍可能漏日志。

    本测试组验证 _last_seen_ts 跨索引点严格跟踪，以及旧 .idx 文件
    在 trust_legacy_index=False 时的保守处理。
    """

    def test_last_seen_ts_detects_regression_between_index_entries(self):
        """乱序发生在两个索引点之间也应被检测到。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            idx = JsonlOffsetIndex(
                Path(tmp_dir) / "test.idx",
                line_interval=10,  # 每 10 行才索引一条
                time_interval_ms=10_000_000,
            )
            idx.load()
            self.assertTrue(idx.is_monotonic)
            # 第一条被索引（line_interval=10，首行触发）
            idx.maybe_index(1000, 0)
            self.assertTrue(idx.is_monotonic)
            # 接下来几条不触发索引，但 _last_seen_ts 应持续更新
            idx.maybe_index(1100, 10)
            idx.maybe_index(1200, 20)
            self.assertTrue(idx.is_monotonic)
            # 1150 < 1200（_last_seen_ts），即使 > 1000（_last_indexed_ts），
            # 也应被检测为乱序
            idx.maybe_index(1150, 30)
            self.assertFalse(idx.is_monotonic)
            # seek_offset 应返回 0
            self.assertEqual(idx.seek_offset(900), 0)

    def test_new_file_flushes_trust_legacy_header(self):
        """新文件首次 flush 应写入 #trust_legacy\t1 头部。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            idx_path = Path(tmp_dir) / "test.idx"
            idx = JsonlOffsetIndex(idx_path, line_interval=1, time_interval_ms=10_000_000)
            idx.maybe_index(1000, 0)
            idx.flush()
            content = idx_path.read_text(encoding="utf-8")
            self.assertIn("#trust_legacy\t1", content)
            self.assertIn("#monotonic\t1", content)

    def test_new_file_reload_keeps_monotonic_with_trust_legacy(self):
        """带 #trust_legacy\t1 头部的文件 reload 后保持 monotonic。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            idx_path = Path(tmp_dir) / "test.idx"
            idx = JsonlOffsetIndex(idx_path, line_interval=1, time_interval_ms=10_000_000)
            idx.maybe_index(1000, 0)
            idx.maybe_index(1100, 10)
            idx.flush()
            # reload
            idx2 = JsonlOffsetIndex(idx_path, line_interval=1, time_interval_ms=10_000_000)
            idx2.load()
            self.assertTrue(idx2.is_monotonic)

    def test_legacy_idx_without_header_treated_as_monotonic_by_default(self):
        """默认 trust_legacy_index=True：旧 .idx 无 header 仍按 monotonic 处理。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            idx_path = Path(tmp_dir) / "test.idx"
            # 模拟旧版本写入的 .idx（无任何 header）
            idx_path.write_text("1000\t0\n1100\t10\n1200\t20\n", encoding="utf-8")
            idx = JsonlOffsetIndex(idx_path, trust_legacy_index=True)
            idx.load()
            # 默认信任旧文件
            self.assertTrue(idx.is_monotonic)
            # seek_offset(1150) 应返回 offset 10（1100 那条的 byte offset），
            # 因为 1150 落在 1100 和 1200 之间，seek 到 1100 的 offset。
            self.assertEqual(idx.seek_offset(1150), 10)

    def test_legacy_idx_conservative_mode_treats_as_non_monotonic(self):
        """trust_legacy_index=False：旧 .idx 无 header 视为非单调。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            idx_path = Path(tmp_dir) / "test.idx"
            # 模拟旧版本写入的 .idx（无任何 header，索引点本身单调）
            idx_path.write_text("1000\t0\n1100\t10\n1200\t20\n", encoding="utf-8")
            idx = JsonlOffsetIndex(idx_path, trust_legacy_index=False)
            idx.load()
            # 保守模式：无法证明索引点之间的行单调，视为非单调
            self.assertFalse(idx.is_monotonic)
            # seek_offset 返回 0，强制全扫
            self.assertEqual(idx.seek_offset(1050), 0)

    def test_legacy_idx_with_explicit_monotonic_header_respected(self):
        """即使 trust_legacy_index=False，显式 #monotonic\t1 仍被尊重。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            idx_path = Path(tmp_dir) / "test.idx"
            # 旧 .idx 但带显式 monotonic header（虽然是旧版本写的）
            idx_path.write_text(
                "#monotonic\t1\n1000\t0\n1100\t10\n1200\t20\n",
                encoding="utf-8",
            )
            idx = JsonlOffsetIndex(idx_path, trust_legacy_index=False)
            idx.load()
            # 显式 header 优先于 trust_legacy_index 默认
            self.assertTrue(idx.is_monotonic)

    def test_read_window_uses_full_scan_for_legacy_conservative(self):
        """trust_legacy_index=False + 旧 .idx：read_jsonl_window 不 early break。"""
        _install_astrbot_stubs()
        from services.mine_sentinel.storage.codec import ObservationRecordCodec
        config = MineSentinelConfig.from_dict({"storage": {"trust_legacy_index": False}})
        base_ts = int(time.time() * 1000)
        # 构造 JSONL：第一条 ts 在窗口外（未来），第二条在窗口内
        # 如果 early break，第二条会被漏掉
        rows_data = [
            {"eventId": "future", "timestamp": base_ts + 100_000, "serverId": "s", "content": "future"},
            {"eventId": "in_window", "timestamp": base_ts - 10_000, "serverId": "s", "content": "in"},
        ]
        with tempfile.TemporaryDirectory() as tmp_dir:
            jsonl_path = Path(tmp_dir) / "test.jsonl"
            with jsonl_path.open("w", encoding="utf-8") as f:
                for r in rows_data:
                    f.write(json.dumps(r) + "\n")
            # 旧 .idx 无 header，且 trust_legacy_index=False
            idx_path = jsonl_path.with_suffix(".idx")
            idx_path.write_text(
                f"{base_ts + 100_000}\t0\n",
                encoding="utf-8",
            )
            idx = JsonlOffsetIndex(
                idx_path,
                trust_legacy_index=config.storage.trust_legacy_index,
            )
            idx.load()
            self.assertFalse(idx.is_monotonic)  # 保守模式
            codec = ObservationRecordCodec(config)
            cutoff = base_ts - 60_000
            end = base_ts + 1
            rows = list(codec.read_jsonl_window(jsonl_path, cutoff, end, index=idx))
            ids = [r["eventId"] for r in rows]
            # 必须包含 in_window，即使它出现在 future 之后
            self.assertIn("in_window", ids)

    def test_trust_legacy_index_config_propagates_to_store(self):
        """storage.trust_legacy_index 配置应能从 from_dict 解析。"""
        _install_astrbot_stubs()
        from services.mine_sentinel.models import MineSentinelConfig
        cfg = MineSentinelConfig.from_dict({
            "storage": {"trust_legacy_index": False},
        })
        self.assertFalse(cfg.storage.trust_legacy_index)
        # 默认值
        cfg2 = MineSentinelConfig.from_dict({})
        self.assertTrue(cfg2.storage.trust_legacy_index)


class MineSentinelPr9HotfixV3Tests(unittest.TestCase):
    """验证 PR9 hotfix v3 修复的边界风险。

    1. P0: JSONL 写入改 binary append，offset 是真实 byte offset。
    2. P1: AI anomaly evidence 按 (server_id, template_id) 匹配样本。
    3. P1: export 文件名加秒级 end_timestamp，避免同分钟复用旧附件。
    4. P2: flush_bucket 同步更新 EWMA。
    """

    def test_jsonl_write_uses_binary_append_and_offset_is_byte_accurate(self):
        """写入 UTF-8 中文日志后，.idx offset 应精确指向行首 byte offset。

        回归 P0：旧代码用文本模式 tell()，返回 TextIO cookie 而非
        raw byte offset，中文日志 seek 会错位漏行。
        """
        _install_astrbot_stubs()
        from services.mine_sentinel.storage.jsonl_store import DiskObservationStore
        cfg = MineSentinelConfig.from_dict({"storage": {"enabled": True}})
        now_ms = int(time.time() * 1000)
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = DiskObservationStore(cfg, Path(tmp_dir))
            # 写入 260 行含中文的日志，触发默认 line_interval=256 的索引
            observations = [
                {"eventId": f"e{i}", "kind": "SERVER_LOG",
                 "timestamp": now_ms + i * 1000,
                 "serverId": "srv",
                 "content": f"玩家{i}加入了游戏，服务器保存了世界",
                 "tags": ["info"]}
                for i in range(260)
            ]
            store.add_batch("srv", {
                "serverId": "srv",
                "serverName": "生存服",
                "observations": observations,
            })
            # 找到当天 JSONL 和 .idx
            jsonl_files = list(Path(tmp_dir).rglob("*.jsonl"))
            self.assertEqual(len(jsonl_files), 1)
            jsonl_path = jsonl_files[0]
            idx_path = jsonl_path.with_suffix(".idx")
            self.assertTrue(idx_path.exists())
            # 读 raw bytes，验证 offset 指向行首
            raw = jsonl_path.read_bytes()
            idx = JsonlOffsetIndex(idx_path)
            idx.load()
            self.assertGreaterEqual(idx.entry_count, 1,
                                    f"应有索引条目，实际 {idx.entry_count}")
            # 对每个索引条目，seek 到 offset 后应能读到完整 JSON 行
            for ts, off in zip(idx._timestamps, idx._offsets):
                self.assertLessEqual(off, len(raw))
                line_end = raw.find(b"\n", off)
                if line_end == -1:
                    line_end = len(raw)
                line_bytes = raw[off:line_end]
                # 应该是合法 JSON，能正确解码 UTF-8 中文
                data = json.loads(line_bytes.decode("utf-8"))
                self.assertEqual(data["timestamp"], ts)
                self.assertIn("玩家", data["content"])

    def test_anomaly_evidence_uses_server_template_key(self):
        """AI 异常证据应按 (server_id, template_id) 匹配样本，不串 server。"""
        _install_astrbot_stubs()
        from services.mine_sentinel.reporting.ai_prompt import AIReportPromptBuilder
        import services.mine_sentinel.anomaly_detector as ad_mod
        from services.mine_sentinel.anomaly_detector import get_anomaly_detector
        # 重置全局检测器
        ad_mod._global_detector = None
        detector = get_anomaly_detector(max_templates_per_server=100, inactive_template_ttl_hours=1, cleanup_interval=99999)
        # 两个 server，相同的 template_id（Drain3 cluster id 从 1 开始）
        # 模拟 observe：survival template_id=T1，creative template_id=T1
        # 先制造足够多的计数触发 baseline
        for _ in range(10):
            detector.observe("survival", "T1", "survival error line", "ERROR")
        for _ in range(10):
            detector.observe("creative", "T1", "creative different error", "ERROR")
        # 突增 survival T1
        for _ in range(50):
            detector.observe("survival", "T1", "survival error line", "ERROR")
        # 构造 records：survival 和 creative 都有 templateId=T1
        from services.mine_sentinel.models import ObservationRecord
        survival_rec = ObservationRecord.from_dict({
            "eventId": "s1", "kind": "SERVER_LOG", "timestamp": 1700000000000,
            "serverId": "survival", "content": "survival error line",
            "tags": ["error"],
            "context": {"templateId": "T1", "level": "ERROR"},
        })
        creative_rec = ObservationRecord.from_dict({
            "eventId": "c1", "kind": "SERVER_LOG", "timestamp": 1700000000000,
            "serverId": "creative", "content": "creative different error",
            "tags": ["error"],
            "context": {"templateId": "T1", "level": "ERROR"},
        })
        cfg = MineSentinelConfig.from_dict({})
        builder = AIReportPromptBuilder(cfg)
        evidence = builder.anomaly_evidence([survival_rec, creative_rec])
        # 找 survival 的异常证据
        survival_ev = [e for e in evidence if e.get("server_id") == "survival"]
        self.assertTrue(survival_ev, "应有 survival 异常证据")
        # 样本应只含 survival 的内容，不含 creative
        for sample in survival_ev[0].get("samples", []):
            self.assertIn("survival", sample.lower())
            self.assertNotIn("creative", sample.lower())
        # 清理全局
        import services.mine_sentinel.anomaly_detector as ad_mod
        ad_mod._global_detector = None

    def test_export_filename_includes_second_precision_end_timestamp(self):
        """PR9 hotfix v5: 同秒内两次 export 应生成不同文件名（毫秒级 end_timestamp 不同）。"""
        from services.mine_sentinel.storage.paths import export_path
        with tempfile.TemporaryDirectory() as tmp_dir:
            export_dir = Path(tmp_dir) / "exports"
            export_dir.mkdir()
            # 同一秒内，相隔 5 毫秒（毫秒级精度）
            p1 = export_path(export_dir, 30, "srv", now=1700000000000)
            p2 = export_path(export_dir, 30, "srv", now=1700000000005)
            # 文件名应不同（_t{ms_timestamp} 后缀不同）
            self.assertNotEqual(p1.name, p2.name)
            # 都应包含 _t 前缀的毫秒级 timestamp
            self.assertIn("_t1700000000000", p1.name)
            self.assertIn("_t1700000000005", p2.name)

    def test_export_reuse_still_works_for_identical_window(self):
        """完全相同窗口的 export 仍应复用（export_reuse_existing 有效）。"""
        from services.mine_sentinel.storage.paths import export_path
        with tempfile.TemporaryDirectory() as tmp_dir:
            export_dir = Path(tmp_dir) / "exports"
            export_dir.mkdir()
            # 完全相同的 now（毫秒级）
            p1 = export_path(export_dir, 30, "srv", now=1700000000000)
            p2 = export_path(export_dir, 30, "srv", now=1700000000000)
            self.assertEqual(p1.name, p2.name)

    def test_export_path_accepts_second_or_ms_timestamp(self):
        """PR9 hotfix v5: export_path 接受秒级或毫秒级 now，内部统一转毫秒。"""
        from services.mine_sentinel.storage.paths import export_path
        with tempfile.TemporaryDirectory() as tmp_dir:
            export_dir = Path(tmp_dir) / "exports"
            export_dir.mkdir()
            # 秒级（< 10^12）→ 自动 *1000
            p_sec = export_path(export_dir, 30, "srv", now=1700000000)
            # 毫秒级（>= 10^12）
            p_ms = export_path(export_dir, 30, "srv", now=1700000000000)
            # 两者应生成相同文件名
            self.assertEqual(p_sec.name, p_ms.name)
            self.assertIn("_t1700000000000", p_sec.name)

    def test_flush_bucket_updates_ewma(self):
        """flush_bucket 后 stat.ewma_count 应 > 0，与 window 一致。"""
        from services.mine_sentinel.anomaly_detector import TemplateAnomalyDetector
        detector = TemplateAnomalyDetector(
            bucket_seconds=60, ewma_alpha=0.5, max_templates_per_server=100,
            inactive_template_ttl_hours=1, cleanup_interval=99999,
        )
        # observe 几次，填充当前桶
        now_ms = 1_700_000_000_000
        for _ in range(10):
            detector.observe("srv", "T1", "error line", "ERROR", timestamp_ms=now_ms)
        # flush_bucket（强制把当前桶 flush 到 window + ewma）
        detector.flush_bucket(server_id="srv", now_ms=now_ms + 120_000)
        shard = detector._shard_for("srv")
        with shard.lock:
            stat = shard.stats.get("T1")
            self.assertIsNotNone(stat)
            self.assertGreater(stat.ewma_count, 0.0, "flush_bucket 后 ewma_count 应 > 0")
            self.assertGreater(len(stat.window), 0, "window 应有计数")


if __name__ == "__main__":
    unittest.main()
