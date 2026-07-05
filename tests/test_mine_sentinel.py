from __future__ import annotations

import asyncio
import gzip
import json
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
    native_stub = types.ModuleType("mine_sentinel_rs")

    class _ObservationRecordCodec:
        def __init__(
            self,
            max_content_length,
            max_tags_per_record,
            max_raw_fields,
            include_raw,
            dedupe_window_seconds,
        ):
            self.dedupe_window_seconds = max(1, int(dedupe_window_seconds))

        def dedupe_key(self, record):
            bucket = int(record.timestamp or 0) // (self.dedupe_window_seconds * 1000)
            return (
                f"{record.kind}:{record.server_id}:{record.backend_server}:"
                f"{bucket}:{record.content[:160]}"
            )

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

    def test_read_appended_lines_reports_dropped_count_in_burst(self):
        """burst 超过 max_lines 时应当返回 dropped_count 而不是静默丢弃。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "burst.log"
            # 写入 10 行，max_lines=3 应丢弃前 7 行
            path.write_text("\n".join(f"line {i}" for i in range(10)) + "\n")
            lines, position, partial, dropped = _read_appended_lines(
                path, 0, "", max_bytes=65536, max_lines=3, max_line_length=1000
            )
            self.assertEqual(len(lines), 3)
            self.assertEqual(dropped, 7)
            self.assertEqual(partial, "")
            self.assertGreater(position, 0)
            # 应保留最后 3 行（最近的日志更相关）
            self.assertEqual(lines[0], "line 7")
            self.assertEqual(lines[2], "line 9")

    def test_read_appended_lines_no_drop_when_under_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ok.log"
            path.write_text("a\nb\n")
            lines, _position, partial, dropped = _read_appended_lines(
                path, 0, "", max_bytes=65536, max_lines=10, max_line_length=1000
            )
            self.assertEqual(lines, ["a", "b"])
            self.assertEqual(dropped, 0)
            self.assertEqual(partial, "")

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


if __name__ == "__main__":
    unittest.main()
