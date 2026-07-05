"""MineSentinel report orchestration."""

from __future__ import annotations

from typing import Any

from ..models import MineSentinelConfig, ObservationRecord
from .ai_summary import AIReportSummarizer
from .rules import HeuristicReportBuilder


class MineSentinelReporter:
    """Builds rule-based reports and lets AI polish them when available."""

    def __init__(self, config: MineSentinelConfig, context: Any | None = None):
        self.rules = HeuristicReportBuilder(config)
        self.ai = AIReportSummarizer(config, context)

    async def build_report(
        self,
        records: list[ObservationRecord],
        window_minutes: int,
        server_id: str | None = None,
        umo: str | None = None,
    ) -> dict[str, Any]:
        heuristic = self.rules.build(records, window_minutes, server_id)
        ai_records = self.rules.filter_records_for_report(records)
        ai_report = await self.ai.build(
            ai_records,
            window_minutes,
            heuristic,
            umo,
            review_records=records,
        )
        return ai_report or heuristic

    def build_heuristic_report(
        self,
        records: list[ObservationRecord],
        window_minutes: int,
        server_id: str | None = None,
    ) -> dict[str, Any]:
        return self.rules.build(records, window_minutes, server_id)
