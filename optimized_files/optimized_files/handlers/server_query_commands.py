"""Status, player list, and player detail command group."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING

from astrbot.api.message_components import Image

from .session_state import SESSION_BINDING_ERROR

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

    from ..core.server_manager import ServerManager
    from ..services.renderer import InfoRenderer


class ServerQueryCommandHandler:
    def __init__(
        self,
        server_manager: "ServerManager",
        renderer: "InfoRenderer",
        get_server_config,
        session_state=None,
    ):
        self.server_manager = server_manager
        self.renderer = renderer
        self.get_server_config = get_server_config
        self.session_state = session_state

    async def handle_status(self, event: "AstrMessageEvent"):
        all_servers = self.get_session_all_servers(event.unified_msg_origin)
        online_servers, error = self._online_servers_or_error(all_servers)
        if error:
            yield event.plain_result(error)
            return

        cards: list[tuple[str, object, object]] = []
        errors: list[str] = []
        # Fetch each server's status concurrently; previously this was a serial
        # await loop so N servers took sum(latency) instead of max(latency).
        results = await asyncio.gather(
            *(self.collect_status_cards(server) for server in online_servers)
        )
        for server_cards, err in results:
            if err:
                errors.append(err)
                continue
            cards.extend(server_cards)

        if not cards:
            yield event.plain_result("\n".join(errors) if errors else "❌ 未获取到可用状态数据")
            return

        use_image = self.any_text2image_enabled(online_servers)
        result = await self.renderer.render_multi_server_status(
            cards,
            as_image=use_image,
        )
        yield self._event_result(event, result)

    async def do_status(self, event: "AstrMessageEvent", server):
        cards, err = await self.collect_status_cards(server)
        if err:
            yield event.plain_result(err)
            return
        config = self.get_server_config(server.server_id)
        use_image = config.text2image if config else True
        result = await self.renderer.render_multi_server_status(
            cards,
            as_image=use_image,
        )
        yield self._event_result(event, result)

    async def collect_status_cards(
        self,
        server,
    ) -> tuple[list[tuple[str, object, object]], str]:
        server_label = self.server_label(server)
        info, err = await server.rest_client.get_server_info()
        if not info:
            return [], f"❌ [{server_label}] 获取服务器信息失败: {err}"

        status, err = await server.rest_client.get_server_status()
        if not status:
            return [], f"❌ [{server_label}] 获取服务器状态失败: {err}"

        cards: list[tuple[str, object, object]] = [(server_label, info, status)]
        if status.is_proxy and status.backends:
            for backend in status.backends:
                backend_info = SimpleNamespace(
                    name=backend.name,
                    platform=backend.platform,
                    minecraft_version=backend.version,
                    online_count=backend.online_players,
                    max_players=backend.max_players,
                    uptime_formatted=backend.uptime_formatted,
                    is_proxy=False,
                    aggregate_online=0,
                    aggregate_max=0,
                )
                backend_status = SimpleNamespace(
                    is_proxy=False,
                    online_players=backend.online_players,
                    max_players=backend.max_players,
                    uptime_formatted=backend.uptime_formatted,
                    tps_1m=backend.tps_1m,
                    tps_5m=backend.tps_5m,
                    tps_15m=backend.tps_15m,
                    memory_used=backend.memory_used,
                    memory_max=backend.memory_max,
                    memory_usage_percent=backend.memory_usage_percent,
                    worlds=[],
                    backends=[],
                )
                cards.append(
                    (f"{server_label}/{backend.name}", backend_info, backend_status)
                )

        return cards, ""

    async def handle_list(self, event: "AstrMessageEvent"):
        all_servers = self.get_session_all_servers(event.unified_msg_origin)
        online_servers, error = self._online_servers_or_error(all_servers)
        if error:
            yield event.plain_result(error)
            return

        cards: list[tuple[str, list, int, str]] = []
        errors: list[str] = []
        results = await asyncio.gather(
            *(self.collect_list_cards(server) for server in online_servers)
        )
        for server_cards, err in results:
            if err:
                errors.append(err)
                continue
            cards.extend(server_cards)

        if not cards:
            yield event.plain_result("\n".join(errors) if errors else "❌ 未获取到可用玩家列表")
            return

        use_image = self.any_text2image_enabled(online_servers)
        result = await self.renderer.render_multi_player_list(cards, as_image=use_image)
        yield self._event_result(event, result)

    async def do_list(self, event: "AstrMessageEvent", server):
        cards, err = await self.collect_list_cards(server)
        if err:
            yield event.plain_result(err)
            return

        config = self.get_server_config(server.server_id)
        use_image = config.text2image if config else True
        result = await self.renderer.render_multi_player_list(cards, as_image=use_image)
        yield self._event_result(event, result)

    async def collect_list_cards(
        self,
        server,
    ) -> tuple[list[tuple[str, list, int, str]], str]:
        server_label = self.server_label(server)
        players, total, err = await server.rest_client.get_players()
        if err:
            return [], f"❌ [{server_label}] 获取玩家列表失败: {err}"
        if total == 0 and players:
            total = len(players)

        status, _ = await server.rest_client.get_server_status()
        if not status or not status.is_proxy or not status.backends:
            return [(server_label, players, total, server_label)], ""

        grouped: dict[str, list] = {}
        unknown_players: list = []
        for player in players:
            backend = (getattr(player, "server", "") or "").strip()
            if backend:
                grouped.setdefault(backend, []).append(player)
            else:
                unknown_players.append(player)

        cards: list[tuple[str, list, int, str]] = []
        for backend in status.backends:
            backend_name = (backend.name or "").strip() or "未命名后端"
            backend_players = grouped.pop(backend_name, [])
            backend_total = (
                backend.online_players
                if backend.online_players > 0
                else len(backend_players)
            )
            cards.append((backend_name, backend_players, backend_total, backend_name))

        for extra_backend, extra_players in grouped.items():
            cards.append((extra_backend, extra_players, len(extra_players), extra_backend))

        if unknown_players:
            cards.append(
                ("未标记子服", unknown_players, len(unknown_players), "未标记子服")
            )

        return cards, ""

    async def handle_player(self, event: "AstrMessageEvent", player_id: str):
        if not player_id:
            yield event.plain_result("❌ 请指定玩家ID")
            return

        all_servers = self.get_session_all_servers(event.unified_msg_origin)
        online_servers, error = self._online_servers_or_error(all_servers)
        if error:
            yield event.plain_result(error)
            return

        cards: list[tuple[str, object]] = []

        async def _resolve_one(server):
            player, _ = await server.rest_client.get_player_by_name(player_id)
            if not player:
                return None
            player_server_name = await self.resolve_player_card_server_name(
                server,
                player,
            )
            return (player_server_name, player)

        # Query each server concurrently; per server the player lookup and the
        # server-name resolution stay sequential (latter depends on former),
        # but different servers no longer wait for each other.
        resolved = await asyncio.gather(
            *(_resolve_one(server) for server in online_servers)
        )
        for item in resolved:
            if item is not None:
                cards.append(item)

        if not cards:
            yield event.plain_result("❌ 玩家在所有在线服务器中均无数据")
            return

        use_image = self.any_text2image_enabled(online_servers)
        result = await self.renderer.render_multi_player_detail(
            cards,
            as_image=use_image,
        )
        yield self._event_result(event, result)

    async def do_player(self, event: "AstrMessageEvent", server, player_id: str):
        server_label = self.server_label(server)
        player, err = await server.rest_client.get_player_by_name(player_id)
        if not player:
            yield event.plain_result(f"❌ [{server_label}] 获取玩家信息失败: {err}")
            return

        config = self.get_server_config(server.server_id)
        use_image = config.text2image if config else True
        player_server_name = await self.resolve_player_card_server_name(server, player)
        result = await self.renderer.render_player_detail(
            player,
            server_tag=player_server_name,
            as_image=use_image,
        )
        yield self._event_result(event, result)

    async def resolve_player_card_server_name(self, server, player) -> str:
        server_label = self.server_label(server)

        status, _ = await server.rest_client.get_server_status()
        if not status or not status.is_proxy or not status.backends:
            return server_label

        backend_map = {
            (backend.name or "").strip().lower(): (backend.name or "").strip()
            for backend in status.backends
            if (backend.name or "").strip()
        }

        candidate = (getattr(player, "server", "") or "").strip()
        if candidate and candidate.lower() in backend_map:
            return backend_map[candidate.lower()]

        players, _, _ = await server.rest_client.get_players()
        target_uuid = (getattr(player, "uuid", "") or "").strip().lower()
        target_name = (getattr(player, "name", "") or "").strip().lower()
        for item in players:
            player_uuid = (getattr(item, "uuid", "") or "").strip().lower()
            player_name = (getattr(item, "name", "") or "").strip().lower()
            if (target_uuid and player_uuid == target_uuid) or (
                target_name and player_name == target_name
            ):
                player_server = (getattr(item, "server", "") or "").strip()
                if player_server and player_server.lower() in backend_map:
                    return backend_map[player_server.lower()]
                if player_server and not is_proxy_like_name(player_server):
                    return player_server
                break

        if candidate and not is_proxy_like_name(candidate):
            return candidate
        return ""

    def get_session_all_servers(self, umo: str) -> list:
        if self.session_state:
            return self.session_state.get_session_all_servers(umo)
        if not umo:
            return []
        servers = []
        for server in self.server_manager.get_all_servers().values():
            config = self.get_server_config(server.server_id)
            if config and config.target_sessions and umo in config.target_sessions:
                servers.append(server)
        return servers

    def any_text2image_enabled(self, servers: list) -> bool:
        for server in servers:
            config = self.get_server_config(server.server_id)
            if config is None or config.text2image:
                return True
        return False

    def _online_servers_or_error(self, servers: list) -> tuple[list, str]:
        if not servers:
            return [], SESSION_BINDING_ERROR

        online_servers = [server for server in servers if server.connected]
        if not online_servers:
            return [], "❌ 当前会话关联的服务器均离线"
        return online_servers, ""

    @staticmethod
    def server_label(server) -> str:
        return (
            server.server_info.name
            if server.server_info and server.server_info.name
            else server.server_id
        )

    @staticmethod
    def _event_result(event: "AstrMessageEvent", result):
        if result.is_image:
            return event.chain_result([Image.fromBytes(result.image.getvalue())])
        return event.plain_result(result.text)


def is_proxy_like_name(name: str) -> bool:
    normalized = (name or "").strip().lower()
    if not normalized:
        return False
    if normalized in {"vc", "velocity", "proxy", "bungeecord", "waterfall"}:
        return True
    return any(
        marker in normalized
        for marker in ("velocity", "proxy", "bungee", "waterfall", "vc")
    )
