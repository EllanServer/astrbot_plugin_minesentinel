"""Rust-backed SERVER_LOG priority scoring."""

from __future__ import annotations

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


def observation_priority_score(record: ObservationRecord) -> float:
    """Score runtime log records that should survive bounded report sampling."""
    return _rs_observation_priority_score(record)


__all__ = ["observation_priority_score"]
