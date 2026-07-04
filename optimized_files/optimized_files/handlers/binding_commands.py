"""Minecraft account binding command helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent


class BindingCommandHandler:
    def __init__(
        self,
        binding_service,
        get_server_config,
        resolve_server_or_pending: Callable[..., tuple[object | None, str]],
    ):
        self.binding_service = binding_service
        self.get_server_config = get_server_config
        self.resolve_server_or_pending = resolve_server_or_pending

    async def handle_bind(self, event: "AstrMessageEvent", player_id: str):
        """Bind the current chat account to a Minecraft player on a server."""
        player_id = (player_id or "").strip()
        if not player_id:
            yield event.plain_result("❌ 请指定要绑定的游戏ID")
            return

        server, msg = self.resolve_server_or_pending(
            event.unified_msg_origin,
            action="bind",
            args={"player_id": player_id},
        )
        if server is None:
            if msg:
                yield event.plain_result(msg)
            return

        async for result in self.do_bind(event, server, player_id):
            yield result

    async def do_bind(self, event: "AstrMessageEvent", server, player_id: str):
        """Execute binding after a server has been selected."""
        config = self.get_server_config(server.server_id)
        if config and not config.bind_enable:
            yield event.plain_result("❌ 绑定功能未启用")
            return

        success, message = await self.binding_service.bind(
            platform=event.get_platform_name(),
            user_id=event.get_sender_id(),
            mc_player_name=player_id,
            server_id=server.server_id,
        )

        prefix = "✅" if success else "❌"
        yield event.plain_result(f"{prefix} {message}")

    async def handle_unbind(self, event: "AstrMessageEvent"):
        """Remove the current chat account's Minecraft binding."""
        success, message = await self.binding_service.unbind(
            platform=event.get_platform_name(),
            user_id=event.get_sender_id(),
        )

        prefix = "✅" if success else "❌"
        yield event.plain_result(f"{prefix} {message}")
