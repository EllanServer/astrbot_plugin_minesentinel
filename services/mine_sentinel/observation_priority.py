"""Rust-backed observation priority scoring."""

from __future__ import annotations

from functools import lru_cache

try:
    from mine_sentinel_rs import (
        observation_priority_score as _rs_observation_priority_score,
    )
except ImportError as exc:  # pragma: no cover - import-time deployment guard
    raise RuntimeError(
        "mine_sentinel_rs native extension is required. Install the platform "
        "wheel built by the 'Build Rust wheels' GitHub Actions workflow."
    ) from exc

from .models import ObservationRecord
from .reporting.dialogue_rules import DialogueRule, dialogue_rules_from_config
from .reporting.dialogue_terms import RuleTermMatcher


@lru_cache(maxsize=64)
def matcher_for_rules(
    rules: tuple[DialogueRule, ...] | None = None,
) -> RuleTermMatcher:
    """Return a cached Rust matcher for the rule tuple."""
    active_rules = dialogue_rules_from_config(None) if rules is None else rules
    return RuleTermMatcher(
        (rule, rule.keywords, rule.urgent_terms) for rule in active_rules
    )


def observation_priority_score(
    record: ObservationRecord,
    rules: tuple[DialogueRule, ...] | None = None,
    matcher: RuleTermMatcher | None = None,
) -> float:
    """Score records that should survive bounded-memory report sampling."""
    active_matcher = matcher if matcher is not None else matcher_for_rules(rules)
    return _rs_observation_priority_score(record, active_matcher)


__all__ = ["matcher_for_rules", "observation_priority_score"]
