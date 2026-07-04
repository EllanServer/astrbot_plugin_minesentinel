"""MineSentinel runtime-log report generation components."""

from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = [
    "AIReportSummarizer",
    "HeuristicReportBuilder",
    "MineSentinelReporter",
    "even_sample",
    "sample_records_for_ai",
]

if TYPE_CHECKING:
    from .ai_summary import AIReportSummarizer
    from .reporter import MineSentinelReporter
    from .rules import HeuristicReportBuilder
    from .sampling import even_sample, sample_records_for_ai


def __getattr__(name: str):
    if name == "AIReportSummarizer":
        from .ai_summary import AIReportSummarizer

        return AIReportSummarizer
    if name == "HeuristicReportBuilder":
        from .rules import HeuristicReportBuilder

        return HeuristicReportBuilder
    if name == "MineSentinelReporter":
        from .reporter import MineSentinelReporter

        return MineSentinelReporter
    if name in {"even_sample", "sample_records_for_ai"}:
        from .sampling import even_sample, sample_records_for_ai

        return {
            "even_sample": even_sample,
            "sample_records_for_ai": sample_records_for_ai,
        }[name]
    raise AttributeError(name)
