"""Public renderer facade for Minecraft server/player info."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from astrbot.api import logger

from .rendering import (
    RenderResult,
    format_multi_player_list_text,
    format_player_detail_text,
    format_server_status_text,
)
from .rendering.cards import ImageCardRenderer

if TYPE_CHECKING:
    from ..core.models import PlayerDetail, PlayerInfo, ServerInfo, ServerStatus


class InfoRenderer:
    """Chooses text or image rendering and keeps image failures non-fatal."""

    def __init__(self, text2image_enabled: bool = True, cache_dir: Path | None = None):
        self.text2image_enabled = text2image_enabled
        self._cache_dir = cache_dir or (Path(__file__).parent.parent / ".cache")
        self._image_cards = ImageCardRenderer(self._cache_dir)

    async def render_multi_server_status(
        self,
        cards: list[tuple[str, "ServerInfo", "ServerStatus"]],
        as_image: bool = True,
    ) -> RenderResult:
        if not cards:
            return RenderResult(" 没有可渲染的服务器状态", is_image=False)

        if not as_image or not self.text2image_enabled:
            return RenderResult(self._multi_server_status_text(cards), is_image=False)

        try:
            out = await self._image_cards.render_multi_server_status(cards)
            return RenderResult(out, is_image=True)
        except Exception as exc:
            logger.warning(f"[Renderer] 多服务器状态合图失败，回退文本: {exc}")
            return RenderResult(self._multi_server_status_text(cards), is_image=False)

    async def render_multi_player_list(
        self,
        cards: list[tuple[str, list["PlayerInfo"], int, str]],
        as_image: bool = True,
    ) -> RenderResult:
        if not cards:
            return RenderResult(" 没有可渲染的玩家列表", is_image=False)

        if not as_image or not self.text2image_enabled:
            return RenderResult(self._format_multi_player_list_text(cards), is_image=False)

        try:
            out = await self._image_cards.render_multi_player_list(cards)
            return RenderResult(out, is_image=True)
        except Exception as exc:
            logger.warning(f"[Renderer] 多服务器玩家列表合图失败，回退文本: {exc}")
            return RenderResult(self._format_multi_player_list_text(cards), is_image=False)

    async def render_multi_player_detail(
        self,
        cards: list[tuple[str, "PlayerDetail"]],
        as_image: bool = True,
    ) -> RenderResult:
        if not cards:
            return RenderResult(" 没有可渲染的玩家详情", is_image=False)

        if not as_image or not self.text2image_enabled:
            return RenderResult(self._multi_player_detail_text(cards), is_image=False)

        try:
            out = await self._image_cards.render_multi_player_detail(cards)
            return RenderResult(out, is_image=True)
        except Exception as exc:
            logger.warning(f"[Renderer] 多服务器玩家详情合图失败，回退文本: {exc}")
            return RenderResult(self._multi_player_detail_text(cards), is_image=False)

    async def render_server_status(
        self,
        server_info: "ServerInfo",
        server_status: "ServerStatus",
        as_image: bool = True,
    ) -> RenderResult:
        return await self.render_multi_server_status(
            [("", server_info, server_status)],
            as_image=as_image,
        )

    async def render_player_list(
        self,
        players: list["PlayerInfo"],
        total: int,
        server_name: str = "",
        as_image: bool = True,
    ) -> RenderResult:
        return await self.render_multi_player_list(
            [("", players, total, server_name)],
            as_image=as_image,
        )

    async def render_player_detail(
        self,
        player: "PlayerDetail",
        server_tag: str = "",
        as_image: bool = True,
    ) -> RenderResult:
        if not as_image or not self.text2image_enabled:
            return RenderResult(
                self._format_player_detail_text(player, server_tag=server_tag),
                is_image=False,
            )

        try:
            out = await self._image_cards.render_player_detail(
                player,
                server_tag=server_tag,
            )
            return RenderResult(out, is_image=True)
        except Exception as exc:
            logger.warning(f"[Renderer] 玩家详情图片渲染失败，回退文本: {exc}")
            return RenderResult(
                self._format_player_detail_text(player, server_tag=server_tag),
                is_image=False,
            )

    def _multi_server_status_text(
        self,
        cards: list[tuple[str, "ServerInfo", "ServerStatus"]],
    ) -> str:
        return "\n\n".join(
            self._format_server_status_text(info, status, server_tag=tag)
            for tag, info, status in cards
        )

    def _multi_player_detail_text(
        self,
        cards: list[tuple[str, "PlayerDetail"]],
    ) -> str:
        return "\n\n".join(
            self._format_player_detail_text(player, server_tag=tag)
            for tag, player in cards
        )

    def _format_multi_player_list_text(
        self,
        cards: list[tuple[str, list["PlayerInfo"], int, str]],
    ) -> str:
        return format_multi_player_list_text(cards)

    def _format_server_status_text(
        self,
        info: "ServerInfo",
        status: "ServerStatus",
        server_tag: str = "",
    ) -> str:
        return format_server_status_text(info, status, server_tag=server_tag)

    def _format_player_detail_text(
        self,
        player: "PlayerDetail",
        server_tag: str = "",
    ) -> str:
        return format_player_detail_text(player, server_tag=server_tag)
