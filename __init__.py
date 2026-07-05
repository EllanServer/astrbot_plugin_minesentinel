"""MineSentinel runtime-log audit plugin for AstrBot."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .main import MinecraftAdapterPlugin

__all__ = ["MinecraftAdapterPlugin"]


def __getattr__(name: str):
    if name == "MinecraftAdapterPlugin":
        from .main import MinecraftAdapterPlugin

        return MinecraftAdapterPlugin
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
