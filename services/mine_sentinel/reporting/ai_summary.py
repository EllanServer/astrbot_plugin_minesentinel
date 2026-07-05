"""AI-assisted MineSentinel report summarization."""

from __future__ import annotations

import json
from typing import Any

from astrbot.api import logger

from ..models import MineSentinelConfig, ObservationRecord
from .ai_normalizer import (
    AIReportNormalizer,
    parse_json_object,
    repair_json_object_text,
)
from .ai_issue_review import AIIssueReviewer
from .ai_prompt import AIReportPromptBuilder, truncate
from .sampling import even_sample


class AIReportSummarizer:
    """Turns deterministic report facts into a polished report via AstrBot AI."""

    _SYSTEM_PROMPT = (
        "你是 MineSentinel 的只读服务器观察报告代理。"
        "必须只输出合法 JSON，不要 Markdown，不要解释，不要要求执行命令。"
        "禁止建议自动封禁、自动踢人、自动 RCON 或自动回滚。"
        "只能根据 Minecraft 运行日志和附件证据总结，不要按聊天审核臆测。"
    )

    def __init__(self, config: MineSentinelConfig, context: Any | None = None):
        self.config = config
        self.context = context
        self.prompt_builder = AIReportPromptBuilder(config)
        self.normalizer = AIReportNormalizer()
        self.issue_reviewer = AIIssueReviewer(config)

    async def build(
        self,
        records: list[ObservationRecord],
        window_minutes: int,
        fallback: dict[str, Any],
        umo: str | None,
        review_records: list[ObservationRecord] | None = None,
    ) -> dict[str, Any] | None:
        if self.context is None:
            return None

        provider = self._get_provider(umo)
        if provider is None:
            return None

        reviewed_fallback, review_changed = await self._review_candidate_issues(
            provider,
            review_records if review_records is not None else records,
            fallback,
        )

        prompt = self._build_prompt(records, window_minutes, reviewed_fallback)
        try:
            result = await provider.text_chat(
                prompt=prompt,
                system_prompt=self._SYSTEM_PROMPT,
                session_id="minesentinel-report",
                persist=False,
            )
            raw = getattr(result, "completion_text", None) or ""
        except Exception as exc:
            logger.debug(f"[MineSentinel] AstrBot provider.text_chat failed: {exc}")
            return reviewed_fallback if review_changed else None

        if not raw:
            return reviewed_fallback if review_changed else None

        parsed = self._parse_json(raw)
        if parsed:
            return self._normalize_report(parsed, reviewed_fallback)
        repaired = self._repair_json_text(raw)
        parsed = self._parse_json(repaired) if repaired else None
        if parsed:
            return self._normalize_report(parsed, reviewed_fallback)
        return reviewed_fallback if review_changed else None

    async def _review_candidate_issues(
        self,
        provider: Any,
        records: list[ObservationRecord],
        fallback: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        prompts = self.issue_reviewer.build_prompts(records, fallback)
        if not prompts:
            return fallback, False
        decisions: list[dict[str, Any]] = []
        any_response = False
        for prompt in prompts:
            try:
                result = await provider.text_chat(
                    prompt=prompt,
                    system_prompt=self._SYSTEM_PROMPT,
                    session_id="minesentinel-issue-review",
                    persist=False,
                )
                raw = getattr(result, "completion_text", None) or ""
            except Exception as exc:
                logger.debug(f"[MineSentinel] AstrBot issue review failed: {exc}")
                continue
            if not raw:
                continue
            any_response = True
            decisions.extend(self.issue_reviewer.parse_review_decisions(raw))
        if not any_response:
            return fallback, False
        if not decisions:
            return fallback, False
        return self.issue_reviewer.apply_review(
            json.dumps({"issues": decisions}, ensure_ascii=False),
            fallback,
        )

    def _get_provider(self, umo: str | None) -> Any | None:
        try:
            provider_id = self.config.report.provider_id.strip()
            if provider_id:
                return self.context.get_provider_by_id(provider_id)

            getter = getattr(self.context, "get_using_provider", None)
            if not callable(getter):
                return None
            if umo:
                return getter(umo)
            try:
                return getter()
            except TypeError:
                return getter(umo)
        except Exception as exc:
            logger.debug(f"[MineSentinel] get AstrBot provider failed: {exc}")
            return None

    def _build_prompt(
        self,
        records: list[ObservationRecord],
        window_minutes: int,
        fallback: dict[str, Any],
    ) -> str:
        return self.prompt_builder.build(records, window_minutes, fallback)

    def _parse_json(self, text: str) -> dict[str, Any] | None:
        return parse_json_object(text)

    def _repair_json_text(self, text: str) -> str:
        return repair_json_object_text(text)

    def _normalize_report(
        self,
        data: dict[str, Any],
        fallback: dict[str, Any],
    ) -> dict[str, Any]:
        return self.normalizer.normalize_report(data, fallback)

    def _normalize_issues(self, result: dict[str, Any], fallback: dict[str, Any]):
        return self.normalizer.normalize_issues(result, fallback)

    def _compact_fallback(self, fallback: dict[str, Any]) -> dict[str, Any]:
        return self.prompt_builder.compact_fallback(fallback)

    def _timeline_chunks(
        self,
        records: list[ObservationRecord],
    ) -> list[dict[str, Any]]:
        return self.prompt_builder.timeline_chunks(records)

    def _sample_for_ai(
        self,
        records: list[ObservationRecord],
        fallback: dict[str, Any] | None = None,
    ) -> list[ObservationRecord]:
        return self.prompt_builder.sample_for_ai(records, fallback)

    def _compact_record(self, record: ObservationRecord) -> dict[str, Any]:
        return self.prompt_builder.compact_record(record)

    @staticmethod
    def _sample_records(records: list[Any], max_records: int) -> list[Any]:
        return even_sample(records, max_records)

    def _drop_chunk_samples(self, chunks: list[dict[str, Any]]) -> bool:
        return self.prompt_builder.drop_chunk_samples(chunks)

    @staticmethod
    def _truncate(value: str, max_length: int) -> str:
        return truncate(value, max_length)
