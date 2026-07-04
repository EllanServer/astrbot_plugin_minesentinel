"""Rust-backed term normalization and matching helpers."""

from __future__ import annotations

try:
    from mine_sentinel_rs import (
        RuleTermMatcher,
        matched_terms,
        message_fingerprint_py as message_fingerprint,
        normalize_text_py as normalize_text,
        term_is_negated_py as term_is_negated,
    )
except ImportError as exc:  # pragma: no cover - import-time deployment guard
    raise RuntimeError(
        "mine_sentinel_rs native extension is required. Install the platform "
        "wheel built by the 'Build Rust wheels' GitHub Actions workflow."
    ) from exc


__all__ = [
    "RuleTermMatcher",
    "matched_terms",
    "message_fingerprint",
    "normalize_text",
    "term_is_negated",
]
