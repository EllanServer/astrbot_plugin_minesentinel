"""Prompt construction for AI-assisted MineSentinel reports."""

from __future__ import annotations

import json
import math
from collections import Counter
from typing import Any

from ..anomaly_detector import get_anomaly_detector
from ..models import MineSentinelConfig, ObservationRecord
from .sampling import even_sample, sample_records_for_ai


MAX_EVIDENCE_SAMPLE_CHARS = 520
MAX_CONTEXT_LINE_CHARS = 180
MAX_CONTEXT_LINES = 5
MAX_ANOMALY_SAMPLES = 3
MAX_ANOMALY_EVIDENCE = 10
MAX_CONTEXT_STRING_CHARS = 220
MAX_CONTEXT_LIST_ITEMS = 8
MAX_CONTEXT_DICT_ITEMS = 12
MAX_CONTEXT_KEY_CHARS = 64
MAX_CONTEXT_DEPTH = 2

LOW_VALUE_ANOMALY_CATEGORIES = {"启动与关闭", "指标观察"}
LOW_VALUE_ANOMALY_LIFECYCLE_MARKERS = (
    "successfully loaded",
    "has been successfully loaded",
    "registered listener",
    "loaded request manager",
    "loaded settings manager",
    "loaded player manager",
    "loaded inventory manager",
    "loaded queue manager",
    "loaded queue signs",
    "repair of failed migration",
    "no failed migration detected",
    "unknown or incomplete command",
)

AI_CONTEXT_FIELDS = (
    "level",
    "serverType",
    "source",
    "templateId",
    "template",
    "templateSize",
    "templateFallback",
    "fingerprint",
    "anomalyScore",
    "anomalyReason",
    "anomalyBaseline",
    "anomalyCurrentCount",
    "dailyNoise",
    "infoDownsampled",
    "redactionCount",
    "llmQualityScore",
    "llmCleanHash",
    "dataQualityFlags",
    "qualityFlags",
    "chatPlayer",
    "chatMessage",
    "vulcanPlayer",
    "vulcanCheck",
)

AI_CONTEXT_NESTED_FIELDS = (
    "opsClassification",
    "chatClassification",
    "templateParams",
)

AI_RECORD_BASE_FIELDS = (
    "level",
    "dailyNoise",
    "infoDownsampled",
    "redactionCount",
    "dataQualityFlags",
    "qualityFlags",
    "chatPlayer",
    "chatMessage",
    "vulcanPlayer",
    "vulcanCheck",
)

AI_CONTEXT_SKIP_KEYS = {
    "attributes",
    "content",
    "event",
    "llmCleanText",
    "message",
    "otel",
    "raw",
}

PROMPT_GENERIC_TAGS = {
    "server_log",
    "runtime_log",
    "minecraft",
    "info",
    "warn",
    "warning",
    "error",
}


class AIReportPromptBuilder:
    """Builds bounded prompts from complete-window deterministic facts."""

    def __init__(self, config: MineSentinelConfig):
        self.config = config

    def build(
        self,
        records: list[ObservationRecord],
        window_minutes: int,
        fallback: dict[str, Any],
    ) -> str:
        fallback_json = json.dumps(self.compact_fallback(fallback), ensure_ascii=False)
        timeline = self.timeline_chunks(records)
        compact_records = [
            self.compact_record(record)
            for record in self.sample_for_ai(records, fallback)
        ]
        anomaly_evidence = self.anomaly_evidence(records)
        chat_topics = self.compact_chat_topics(fallback.get("chat_topics") or {})
        vulcan_alerts = fallback.get("vulcan_alerts") or []
        return self.fit_prompt(
            window_minutes,
            fallback_json,
            timeline,
            compact_records,
            anomaly_evidence,
            chat_topics,
            vulcan_alerts,
        )

    def fit_prompt(
        self,
        window_minutes: int,
        fallback_json: str,
        timeline_chunks: list[dict[str, Any]],
        compact_records: list[dict[str, Any]],
        anomaly_evidence: list[dict[str, Any]],
        chat_topics: dict[str, Any] | None = None,
        vulcan_alerts: dict[str, Any] | None = None,
    ) -> str:
        max_chars = self.config.report.max_ai_prompt_chars
        records = list(compact_records)
        chunks = [dict(chunk) for chunk in timeline_chunks]
        evidence = [dict(item) for item in anomaly_evidence]
        chat_topics_data = dict(chat_topics or {})
        vulcan_alerts_data = dict(vulcan_alerts or {})

        while True:
            prompt = self.prompt_text(
                window_minutes,
                fallback_json,
                chunks,
                records,
                evidence,
                chat_topics_data,
                vulcan_alerts_data,
            )
            if len(prompt) <= max_chars:
                return prompt
            # 预算压缩优先级：先减抽样记录 → 减 chunk samples → 减 chunks → 减异常证据样本
            if records:
                records = even_sample(records, max(0, len(records) * 3 // 4))
                continue
            if self.drop_chunk_samples(chunks):
                continue
            if len(chunks) > 1:
                chunks = even_sample(chunks, max(1, len(chunks) // 2))
                continue
            if self.trim_anomaly_samples(evidence):
                continue
            # 压缩 Vulcan 告警：先减半 samples，再清空 samples
            if vulcan_alerts_data.get("samples") and len(vulcan_alerts_data["samples"]) > 3:
                vulcan_alerts_data["samples"] = vulcan_alerts_data["samples"][
                    : max(3, len(vulcan_alerts_data["samples"]) * 3 // 4)
                ]
                continue
            if vulcan_alerts_data.get("samples"):
                vulcan_alerts_data = {k: v for k, v in vulcan_alerts_data.items() if k != "samples"}
                continue
            if len(evidence) > 3:
                evidence = evidence[: max(3, len(evidence) * 3 // 4)]
                continue
            return prompt[:max_chars]

    @staticmethod
    def prompt_text(
        window_minutes: int,
        fallback_json: str,
        timeline_chunks: list[dict[str, Any]],
        compact_records: list[dict[str, Any]],
        anomaly_evidence: list[dict[str, Any]],
        chat_topics: dict[str, Any] | None = None,
        vulcan_alerts: list[dict[str, Any]] | None = None,
    ) -> str:
        chat_topics_json = json.dumps(chat_topics or {}, ensure_ascii=False)
        vulcan_alerts_json = json.dumps(vulcan_alerts or [], ensure_ascii=False)
        return (
            "你是 Minecraft 服务器只读旁路监控 MineSentinel 的报告代理。"
            "只输出合法 JSON，不要执行任何管理动作。"
            "必须使用以下 schema: summary,time_window,servers,log_count,incident_findings,"
            "categories(daily,complaint,bug,network,plugin,economy,community,chat_review,"
            "player_feedback,community_ops,moderation,cross_server,suggestion),"
            "issues(category,tag,incident_index,severity,affected_locations,issue_terms,"
            "evidence_count,signal_count,ops_categories,ops_subtypes,ops_impacts,suggested_action),"
            "chat_summary,vulcan_alerts,ops_notes,"
            "report_sections(id,title,bullets)。"
            "异常检测已由模板解析（Drain3）+ EWMA/分位数突增检测 + 关键词规则完成，"
            "你的职责是解释异常证据、判断可能原因、给出排查建议，而不是重新检测异常。"
            "输入里的'异常证据'是预计算的结构化异常列表，每条含 template_id、score、"
            "reason（ewma_spike/percentile_spike/new_template）、baseline、current_count、"
            "代表样本；score>=0.5 表示突增告警，>=0.8 表示极端突增。"
            "应优先把高 score 异常归入对应 issue 并提级 severity，"
            "在 suggested_action 中给出针对该模板的具体排查步骤。"
            "输入里的启发式初稿来自完整窗口记录；分段时间线也是完整窗口的压缩统计；"
            "当前输入只来自 AstrBot 直接读取的 Minecraft 运行日志 SERVER_LOG。"
            "issues.evidence_samples 若包含多行上下文，> 行是命中的证据日志，"
            "其余行是同服/同后端前后文。"
            "抽样观察里的 context 是来源、日志文件、日志级别、世界/维度和后端服线索，"
            "只能辅助判断前因后果；"
            "context.opsClassification 是确定性运维日志分类，字段包括 level、category、subtype、"
            "severity、impact、needs_admin；log level 不是事件类型，ERROR/WARN/INFO 只能作为严重度"
            "和候选性线索，真实事件类型必须优先使用 category/subtype/impact。"
            "INFO 默认只作上下文；WARN 是候选日志；ERROR 不自动等于 critical，只有崩溃、watchdog、"
            "世界或玩家数据保存失败、磁盘满、数据库不可用、核心插件加载失败、复制物品/经济漏洞、"
            "权限提升等才应判为 critical。"
            "服务器指标观察不要单独列事件，只能在玩家聊天反馈或同时间运维异常里作为指标观察引用。"
            "同一服务器、同一世界/后端、同一 3 到 5 分钟内的玩家异常反馈、WARN/ERROR 运维日志"
            "和指标观察应优先合并为同一个 incident。"
            "原始样本只用于补措辞，不代表全部记录。"
            "这些 JSON 字段会被组装为 QQ 群五段式总结："
            "一、整体情况；二、重点事件总结；三、聊天与社区观察；四、玩家问题/投诉识别；五、风险提醒与建议处理。"
            "report_sections 必须按上述五段顺序输出，id 固定为 overall/incidents/community/"
            "player_problems/risk_actions，bullets 每段 1 到 8 条、面向管理员可直接发送。"
            "必须区分待处理事故和低风险观察：插件/数据库/网络/经济/权限确认/外挂举报等需要管理员看的内容进入事故与事件；"
            "普通社区运营日志、正常问答、短时间无意义重复聊天进入聊天与社区观察，不计入重点事件数量。"
            "不只是聊天，运行、插件、网络、经济、反作弊、社区管理等所有类别都必须按同一套事故证据结构输出；"
            "每个 incident 必须能精准映射到 time_range、incident_title、incident_summary、players、labels、"
            "2 到 4 条关键证据、metrics_summary、judgement、recommended_action。"
            "server_metrics 不要单独作为事件列出，只能作为对应事故的指标观察引用；"
            "不要臆测、改写或输出 QQ/UMO/session target；目标会话解析由 AstrBot 插件处理；"
            "请让 summary、incident_findings、categories 和 suggested_action 面向管理员群可读，"
            "保留日志级别、时间线索、上下文结论和人工处理建议。"
            "社区管理相关日志必须单独归入 community 类，例如 ban/kick/mute/grief/cheat/"
            "举报/封禁/禁言/外挂/反作弊，不要混入 bug 或 moderation。"
            "聊天审查相关内容必须归入 chat_review 类（辱骂/广告/刷屏/骚扰/威胁/私聊/链接），"
            "不要混入 community；玩家建议/反馈/功能请求归 player_feedback；"
            "活动/公告/奖励/投票/赛季/社区运营归 community_ops；"
            "卡顿/延迟/TPS 等无明显建议语气时归 complaint。"
            "生成运行日志与事件相关内容时必须按事故聚合，而不是按问题类别拆分："
            "同一服务器、同一世界或后端、同一 3 到 5 分钟窗口内的多条异常日志，"
            "应优先合并为一个 incident，并在该 incident 内列出多个标签和影响面；"
            "不要把卡顿/延迟、掉线/回档、传送异常、经济/商店异常、插件异常、后端同步异常等类别各自写成独立事件，"
            "除非它们发生在不同时间、不同服务器/后端或明显属于不同事故；"
            "incident_index 只能表示真实事故序号，同一批上下文不要重复输出多个 事件 #1；"
            "每个事故最多保留 2 到 4 条关键证据，避免在多个事件中重复粘贴同一批上下文；"
            "如果窗口内只有一个明显异常时间点，应说明其他时间段未发现明显持续异常。"
            "建议处理必须去重并按优先级组织：先处理可能影响全服稳定性的事项，"
            "例如内存、GC、插件阻塞、后端连通性；再处理玩家资产相关事项，"
            "例如商店扣款、经济流水、背包同步、物品复制；最后处理需要证据复核的事项，"
            "例如飞行、外挂、破坏举报。"
            "不要把没有共同时间窗或共同作用域的内容硬合并，也不要把整段聊天泛化为违规。"
            "聊天热点总结：输入里的 chat_topics 是预计算的聊天统计。行为判断基于玩家上下文："
            "其中 classified_messages/admin_messages 已按消息类别、问题标签、风险等级三层预分类，"
            "字段包括 primary_category、labels、severity、needs_admin；"
            "category_counts、label_counts、severity_counts 只能帮助你判断集中趋势，不要把统计项单独列成事件。"
            "聊天分类是给事件聚合用的，不是最终事件本身；同一时间窗、同一服务器/世界内的卡顿、掉线、传送、"
            "商店扣款、背包同步等反馈应合并为一个集中异常反馈事件，并在 labels 中保留所有相关问题。"
            "单条关键词命中只是'线索'(reason=hint)，同一玩家在窗口内多次命中同类关键词"
            "（如反复发链接、反复发代练广告）才是'行为'(reason=abuse)，刷屏是'行为'(reason=flood)。"
            "你必须在 chat_summary 字段输出面向管理员的聊天热点归纳："
            "先说活跃玩家和讨论主题（1-2 句）；"
            "如果 flood_players 非空，贴出刷屏玩家、刷屏类型、时间窗口、消息数和样本原文；"
            "如果 abuse_players 非空，贴出重复违规玩家、违规类别（url 链接广告/abuse_language 辱骂/"
            "trade_ad 交易广告/sensitive 敏感词）、命中次数和样本原文；"
            "review_evidence 里 reason=hint 的是单次线索（轻度提示），reason=abuse/flood 的是行为（需关注）；"
            "贴证据时必须包含 player_total_messages（玩家总消息数，上下文）和 message 原文，"
            "让管理员能判断是偶发还是习惯性违规；没有聊天记录时输出空字符串。"
            "Vulcan 反作弊告警：输入里的 vulcan_alerts 是预聚合的结构化统计（total/"
            "unique_players/unique_checks/by_player/by_check/time_range/samples），"
            "你必须在 vulcan_alerts 字段输出面向管理员的告警摘要：先说总数和涉及玩家，"
            "再按玩家列出告警数和主要检查类型（如 dxe_explode 3020 条告警，主要是 Ground/Step/Strafe），"
            "最后给出时间范围；用于管理员快速定位作弊嫌疑玩家。无告警时输出空对象。"
            "正常登录/断开/UUID 分配/LuckPerms 常规日志已被 daily_noise 过滤，"
            "不要把它们当作异常事件输出。"
            f"时间窗口: 最近 {window_minutes} 分钟。\n"
            f"异常证据: {json.dumps(anomaly_evidence, ensure_ascii=False)}\n"
            f"聊天热点统计: {chat_topics_json}\n"
            f"Vulcan 反作弊告警: {vulcan_alerts_json}\n"
            f"启发式初稿: {fallback_json}\n"
            f"分段时间线: {json.dumps(timeline_chunks, ensure_ascii=False)}\n"
            f"抽样观察: {json.dumps(compact_records, ensure_ascii=False)}"
        )

    def anomaly_evidence(
        self,
        records: list[ObservationRecord],
    ) -> list[dict[str, Any]]:
        """从异常检测器提取结构化异常证据，附带代表样本。

        LLM 不再从原始日志中检测异常，而是直接消费预计算的异常证据：
        - template_id / template / level
        - anomaly score / reason / baseline / current_count
        - 该模板的代表日志样本（从 records 中按 templateId 匹配）
        """
        snapshot = get_anomaly_detector().snapshot()
        anomalies = snapshot.get("anomalies") or []
        if not anomalies:
            return []
        # PR9 hotfix v3: 按 (server_id, template_id) 分组记录。
        # template_miner 已按 server namespace 隔离，不同 server 的
        # Drain3 cluster id 都从 1、2、3 开始，仅按 template_id 分组
        # 会让 survival 的异常附上 creative 的日志样本。
        records_by_template: dict[tuple[str, str], list[ObservationRecord]] = {}
        for record in records:
            ctx = record.context or {}
            tid = str(ctx.get("templateId") or "")
            if tid:
                key = (str(record.server_id or ""), tid)
                records_by_template.setdefault(key, []).append(record)

        evidence: list[dict[str, Any]] = []
        for anomaly in anomalies[:MAX_ANOMALY_EVIDENCE]:
            tid = anomaly.get("template_id") or ""
            server_id = str(anomaly.get("server_id") or "")
            samples = records_by_template.get((server_id, tid), [])
            if not samples:
                continue
            if all(self._low_value_anomaly_sample(record) for record in samples):
                continue
            # 取最近几条作为代表样本
            sample_texts: list[str] = []
            for record in samples[-MAX_ANOMALY_SAMPLES:]:
                ctx = record.context or {}
                text = truncate(
                    str(ctx.get("llmCleanText") or record.content),
                    self.config.report.max_ai_content_length,
                )
                if text:
                    sample_texts.append(text)
            evidence.append(
                {
                    "template_id": tid,
                    "template": truncate(str(anomaly.get("template") or ""), 200),
                    "level": anomaly.get("level", "INFO"),
                    "score": anomaly.get("current_score", 0.0),
                    "reason": anomaly.get("reason", ""),
                    "baseline": anomaly.get("ewma_count", 0.0),
                    "total_count": anomaly.get("total_count", 0),
                    "server_id": anomaly.get("server_id", ""),
                    "first_seen_ms": anomaly.get("first_seen_ms", 0),
                    "last_seen_ms": anomaly.get("last_seen_ms", 0),
                    "samples": sample_texts,
                }
            )
        return evidence

    @staticmethod
    def _low_value_anomaly_sample(record: ObservationRecord) -> bool:
        if "daily_noise" in (record.tags or ()):
            return True
        ctx = record.context or {}
        ops = ctx.get("opsClassification")
        if isinstance(ops, dict):
            category = str(ops.get("category") or "")
            severity = str(ops.get("severity") or "").lower()
            needs_admin = bool(ops.get("needs_admin"))
            if (
                not needs_admin
                and severity in {"", "info", "low"}
                and category in LOW_VALUE_ANOMALY_CATEGORIES
            ):
                return True
        text = str(record.content or "").lower()
        return any(marker in text for marker in LOW_VALUE_ANOMALY_LIFECYCLE_MARKERS)

    @staticmethod
    def trim_anomaly_samples(evidence: list[dict[str, Any]]) -> bool:
        """压缩异常证据的样本数，返回是否做了改动。"""
        changed = False
        for item in evidence:
            samples = item.get("samples") or []
            if len(samples) > 1:
                item["samples"] = samples[:1]
                changed = True
            elif samples:
                item["samples"] = []
                changed = True
        return changed

    def compact_fallback(self, fallback: dict[str, Any]) -> dict[str, Any]:
        categories = fallback.get("categories") or {}
        compact_categories = {
            key: [
                truncate(str(item), 180)
                for item in (categories.get(key) or [])[:5]
            ]
            for key in (
                "daily",
                "complaint",
                "bug",
                "network",
                "plugin",
                "economy",
                "community",
                "chat_review",
                "player_feedback",
                "community_ops",
                "moderation",
                "cross_server",
                "suggestion",
            )
        }
        compact_issues = []
        for issue in (fallback.get("issues") or [])[:8]:
            item = dict(issue)
            samples = item.get("evidence_samples") or []
            item["evidence_samples"] = [
                compact_evidence_sample(str(sample)) for sample in samples[:2]
            ]
            compact_issues.append(item)
        compact_sections = []
        for section in (fallback.get("report_sections") or [])[:5]:
            if not isinstance(section, dict):
                continue
            compact_sections.append(
                {
                    "id": truncate(str(section.get("id") or ""), 48),
                    "title": truncate(str(section.get("title") or ""), 80),
                    "bullets": [
                        truncate(str(bullet), 180)
                        for bullet in (section.get("bullets") or [])[:5]
                    ],
                }
            )
        return {
            "summary": truncate(str(fallback.get("summary") or ""), 300),
            "time_window": fallback.get("time_window"),
            "servers": (fallback.get("servers") or [])[:20],
            "log_count": fallback.get("log_count", 0),
            "incident_findings": [
                truncate(str(item), 220)
                for item in (fallback.get("incident_findings") or [])[:8]
            ],
            "categories": compact_categories,
            "issues": compact_issues,
            "report_sections": compact_sections,
            "ops_notes": [
                truncate(str(note), 180)
                for note in (fallback.get("ops_notes") or [])[:8]
            ],
        }

    @staticmethod
    def compact_chat_topics(chat_topics: dict[str, Any]) -> dict[str, Any]:
        if not chat_topics:
            return {}
        compact = dict(chat_topics)
        for key in ("classified_messages", "admin_messages", "review_evidence"):
            items = list(compact.get(key) or [])[:12]
            compact[key] = [
                {
                    item_key: (
                        truncate(str(value), 180)
                        if isinstance(value, str)
                        else value
                    )
                    for item_key, value in dict(item).items()
                    if item_key != "context_messages"
                }
                for item in items
            ]
        for key in ("sample_messages",):
            compact[key] = [
                truncate(str(item), 180)
                for item in (compact.get(key) or [])[:8]
            ]
        return compact

    def timeline_chunks(
        self,
        records: list[ObservationRecord],
    ) -> list[dict[str, Any]]:
        if not records:
            return []

        chunk_count = min(
            8,
            max(1, self.config.report.max_ai_records // 20),
            len(records),
        )
        chunk_size = max(1, math.ceil(len(records) / chunk_count))
        chunks: list[dict[str, Any]] = []
        for index in range(0, len(records), chunk_size):
            group = records[index : index + chunk_size]
            if not group:
                continue
            kinds = Counter(record.kind for record in group)
            tags = Counter(tag for record in group for tag in record.tags if tag)
            sample_pool = [
                record for record in group
                if not self._low_value_anomaly_sample(record)
            ]
            samples = [
                truncate(record.evidence_text(), 160)
                for record in even_sample(sample_pool, min(4, len(sample_pool)))
            ]
            chunks.append(
                {
                    "start_ts": group[0].timestamp,
                    "end_ts": group[-1].timestamp,
                    "count": len(group),
                    "log_count": sum(
                        1 for record in group if record.kind == "SERVER_LOG"
                    ),
                    "kinds": dict(kinds.most_common(8)),
                    "top_tags": [tag for tag, _ in tags.most_common(8)],
                    "samples": samples,
                }
            )
        return chunks

    def sample_for_ai(
        self,
        records: list[ObservationRecord],
        fallback: dict[str, Any] | None = None,
    ) -> list[ObservationRecord]:
        return sample_records_for_ai(
            records,
            self.config.report.max_ai_records,
            fallback,
        )

    def compact_record(self, record: ObservationRecord) -> dict[str, Any]:
        context = record.context or {}
        return {
            "kind": record.kind,
            "server": record.server_id,
            "backend": record.backend_server,
            "tags": compact_prompt_tags(record.tags or []),
            "content": truncate(
                str(context.get("llmCleanText") or record.content),
                self.config.report.max_ai_content_length,
            ),
            "context": compact_context_for_ai(context, profile="record"),
            "timestamp": record.timestamp,
        }

    @staticmethod
    def drop_chunk_samples(chunks: list[dict[str, Any]]) -> bool:
        changed = False
        for chunk in chunks:
            samples = chunk.get("samples") or []
            if len(samples) > 1:
                chunk["samples"] = samples[:1]
                changed = True
            elif samples:
                chunk["samples"] = []
                changed = True
        return changed


def truncate(value: str, max_length: int) -> str:
    if max_length <= 0:
        return ""
    if len(value) <= max_length:
        return value
    if max_length <= 3:
        return value[:max_length]
    return value[: max_length - 3] + "..."


def compact_context_for_ai(
    context: dict[str, Any],
    *,
    profile: str = "diagnostic",
) -> dict[str, Any]:
    """Keep only compact, report-useful context for AI prompts."""
    if not isinstance(context, dict) or not context:
        return {}
    if profile == "record":
        return _compact_record_context_for_ai(context)

    compact: dict[str, Any] = {}
    for key in AI_CONTEXT_FIELDS:
        value = context.get(key)
        if _empty_context_value(value):
            continue
        compacted = _compact_context_value(value)
        if not _empty_context_value(compacted):
            compact[key] = compacted

    log_file = str(context.get("logFile") or context.get("file") or "").strip()
    if log_file:
        normalized = log_file.replace("\\", "/").rstrip("/")
        compact["logFileName"] = truncate(normalized.rsplit("/", 1)[-1], 96)

    for key in AI_CONTEXT_NESTED_FIELDS:
        value = context.get(key)
        if _empty_context_value(value):
            continue
        compacted = _compact_context_value(value)
        if not _empty_context_value(compacted):
            compact[key] = compacted

    return compact


def compact_prompt_tags(tags: list[str]) -> list[str]:
    compact: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        value = str(tag or "").strip()
        if not value or value in PROMPT_GENERIC_TAGS or value in seen:
            continue
        seen.add(value)
        compact.append(value)
        if len(compact) >= 6:
            break
    return compact


def _compact_record_context_for_ai(context: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in AI_RECORD_BASE_FIELDS:
        value = context.get(key)
        if key == "redactionCount" and not value:
            continue
        if key in {"dataQualityFlags", "qualityFlags"}:
            flags = [
                str(flag)
                for flag in (value or [])
                if flag and str(flag) not in {"whitespace_collapsed"}
            ][:MAX_CONTEXT_LIST_ITEMS]
            if flags:
                compact[key] = flags
            continue
        if _empty_context_value(value):
            continue
        compacted = _compact_context_value(value)
        if not _empty_context_value(compacted):
            compact[key] = compacted

    anomaly_score = _as_float(context.get("anomalyScore"), 0.0)
    if anomaly_score >= 0.5:
        compact["anomalyScore"] = round(anomaly_score, 3)
        reason = str(context.get("anomalyReason") or "").strip()
        if reason:
            compact["anomalyReason"] = truncate(reason, 80)
        baseline = _as_float(context.get("anomalyBaseline"), 0.0)
        current = _as_float(context.get("anomalyCurrentCount"), 0.0)
        if baseline:
            compact["anomalyBaseline"] = round(baseline, 3)
        if current:
            compact["anomalyCurrentCount"] = round(current, 3)

    ops = _compact_classification(context.get("opsClassification"))
    if ops:
        compact["opsClassification"] = ops
    chat = _compact_classification(context.get("chatClassification"))
    if chat:
        compact["chatClassification"] = chat
    cleaning = _compact_cleaning_summary(context)
    if cleaning:
        compact["cleaning"] = cleaning
    return compact


def _compact_classification(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    keep = (
        "level",
        "category",
        "primary_category",
        "subtype",
        "severity",
        "needs_admin",
        "opsObservation",
        "labels",
    )
    compact: dict[str, Any] = {}
    for key in keep:
        item = value.get(key)
        if _empty_context_value(item):
            continue
        compacted = _compact_context_value(item)
        if not _empty_context_value(compacted):
            compact[key] = compacted
    return compact


def _compact_cleaning_summary(context: dict[str, Any]) -> dict[str, Any]:
    """Expose LLM-cleaning quality signals without leaking clean hashes."""
    cleaning: dict[str, Any] = {}
    quality_raw = context.get("llmQualityScore")
    try:
        quality = int(quality_raw)
    except (TypeError, ValueError):
        quality = None
    if quality is not None:
        cleaning["quality"] = max(0, min(100, quality))
    redactions_raw = context.get("redactionCount")
    try:
        redactions = int(redactions_raw or 0)
    except (TypeError, ValueError):
        redactions = 0
    if redactions:
        cleaning["redactions"] = redactions
    flags: list[str] = []
    for key in ("dataQualityFlags", "qualityFlags"):
        for flag in context.get(key) or []:
            value = str(flag or "").strip()
            if value and value not in {"whitespace_collapsed"} and value not in flags:
                flags.append(value)
            if len(flags) >= MAX_CONTEXT_LIST_ITEMS:
                break
        if len(flags) >= MAX_CONTEXT_LIST_ITEMS:
            break
    if flags:
        cleaning["flags"] = flags
    return cleaning


def _compact_context_value(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return truncate(value, MAX_CONTEXT_STRING_CHARS)
    if isinstance(value, bool) or isinstance(value, int) or isinstance(value, float):
        return value
    if isinstance(value, list):
        items = [
            _compact_context_value(item, depth + 1)
            for item in value[:MAX_CONTEXT_LIST_ITEMS]
        ]
        return [item for item in items if not _empty_context_value(item)]
    if isinstance(value, dict):
        if depth >= MAX_CONTEXT_DEPTH:
            return {}
        compact: dict[str, Any] = {}
        kept = 0
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if key in AI_CONTEXT_SKIP_KEYS:
                continue
            item = _compact_context_value(raw_value, depth + 1)
            if _empty_context_value(item):
                continue
            compact[truncate(key, MAX_CONTEXT_KEY_CHARS)] = item
            kept += 1
            if kept >= MAX_CONTEXT_DICT_ITEMS:
                break
        return compact
    if value is None:
        return None
    return truncate(str(value), MAX_CONTEXT_STRING_CHARS)


def _empty_context_value(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def compact_evidence_sample(sample: str) -> str:
    if "\n" not in sample or not sample.startswith("上下文 "):
        return truncate(sample, 220)

    lines = [line for line in sample.splitlines() if line.strip()]
    if not lines:
        return ""
    target_index = next(
        (index for index, line in enumerate(lines) if line.lstrip().startswith(">")),
        min(1, len(lines) - 1),
    )
    selected_indexes = {0, target_index}
    radius = 1
    while len(selected_indexes) < min(MAX_CONTEXT_LINES, len(lines)):
        before = target_index - radius
        after = target_index + radius
        if before > 0:
            selected_indexes.add(before)
        if len(selected_indexes) >= min(MAX_CONTEXT_LINES, len(lines)):
            break
        if after < len(lines):
            selected_indexes.add(after)
        if before <= 0 and after >= len(lines):
            break
        radius += 1

    compact_lines = [
        truncate(lines[index], MAX_CONTEXT_LINE_CHARS)
        for index in sorted(selected_indexes)
    ]
    return truncate("\n".join(compact_lines), MAX_EVIDENCE_SAMPLE_CHARS)
