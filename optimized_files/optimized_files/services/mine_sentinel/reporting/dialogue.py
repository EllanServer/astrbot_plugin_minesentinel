"""Player dialogue issue detection for MineSentinel."""

from __future__ import annotations

from typing import Any

from ..models import MineSentinelConfig, ObservationRecord
from .dialogue_rules import DialogueRule, dialogue_rules_from_config
from .dialogue_context import DialogueContinuationTracker
from .dialogue_evidence import DialogueEvidenceContextBuilder
from .dialogue_output import DialogueIssueBuilder
from .dialogue_signals import (
    DialogueSignalCollector,
    replaceable_sample_index,
    signal_fingerprint,
)
from .dialogue_terms import RuleTermMatcher, normalize_text


MODERATION_CATEGORY = "moderation"


class PlayerDialogueAnalyzer:
    """Detects actionable issues from player chat, separate from generic events."""

    def __init__(self, config: MineSentinelConfig):
        self.config = config
        self.rules = dialogue_rules_from_config(config.dialogue.custom_rules)
        # Pre-compile all rule keywords into one alternation regex so each chat
        # record is scanned once instead of rules × keywords str.find passes.
        self._matcher = RuleTermMatcher(
            (rule, rule.keywords, rule.urgent_terms) for rule in self.rules
        )
        self.output = DialogueIssueBuilder(config)

    def analyze(self, records: list[ObservationRecord]) -> dict[str, Any]:
        if not self.config.dialogue.enabled:
            return {"findings": [], "issues": [], "category_lines": {}}

        collector = DialogueSignalCollector(
            self.config.dialogue.max_issue_records,
            self.config.dialogue.incident_gap_seconds,
        )
        continuation = DialogueContinuationTracker(
            self.config.dialogue.continuation_window_seconds
        )
        chat_records = sorted(
            (
                record
                for record in records
                if record.kind == "CHAT" and record.content.strip()
            ),
            key=lambda item: item.timestamp,
        )
        for record in chat_records:
            text = normalize_text(record.content)
            # Single-pass scan: one regex finds every keyword/urgent hit per rule.
            direct_matches: list[tuple[DialogueRule, list[str]]] = []
            for rule, (keywords, _urgent) in self._matcher.scan(text).items():
                if keywords:
                    direct_matches.append((rule, keywords))
            if direct_matches:
                for rule, terms in self._prioritized_direct_matches(direct_matches):
                    collector.add(record, rule, terms, text)
                    continuation.remember(record, rule)
                continue
            for match in continuation.matches(record, text):
                collector.add(record, match.rule, match.terms, text)

        groups = collector.groups()
        DialogueEvidenceContextBuilder(
            self.config.dialogue.context_window_seconds,
            self.config.dialogue.context_messages_per_side,
            self.config.report.max_ai_content_length,
        ).attach(groups, chat_records)
        return self.output.build(groups)

    def classify_record(self, record: ObservationRecord) -> str | None:
        rule = self.matched_rule(record)
        return rule.category if rule else None

    def matched_rule(self, record: ObservationRecord) -> DialogueRule | None:
        if record.kind != "CHAT" or not record.content.strip():
            return None
        text = normalize_text(record.content)
        best_rule = None
        best_count = 0
        for rule, terms in self._matcher.matched_keywords(text).items():
            count = len(terms)
            if count > best_count or (
                count
                and count == best_count
                and self._rule_priority(rule) < self._rule_priority(best_rule)
            ):
                best_rule = rule
                best_count = count
        return best_rule

    def _prioritized_direct_matches(
        self,
        matches: list[tuple[DialogueRule, list[str]]],
    ) -> list[tuple[DialogueRule, list[str]]]:
        if any(rule.category == MODERATION_CATEGORY for rule, _ in matches):
            return [
                (rule, terms)
                for rule, terms in matches
                if rule.category == MODERATION_CATEGORY
            ]
        return matches

    @staticmethod
    def _rule_priority(rule: DialogueRule | None) -> int:
        if rule is None:
            return 100
        if rule.category == MODERATION_CATEGORY:
            return 0
        return 10

    def _add_evidence_sample(
        self,
        samples: list[ObservationRecord],
        record: ObservationRecord,
    ):
        collector = DialogueSignalCollector(self.config.dialogue.max_issue_records)
        collector._add_evidence_sample(samples, record)

    @staticmethod
    def _replaceable_sample_index(sample_players: list[str]) -> int:
        return replaceable_sample_index(sample_players)

    @staticmethod
    def _signal_fingerprint(record: ObservationRecord, normalized_text: str) -> str:
        return signal_fingerprint(record, normalized_text)
