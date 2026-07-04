"""MineSentinel audit services."""

from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = ["MineSentinelService"]

if TYPE_CHECKING:
    from .mine_sentinel import MineSentinelService


def __getattr__(name: str):
    if name == "MineSentinelService":
        from .mine_sentinel import MineSentinelService

        return MineSentinelService
    raise AttributeError(name)
