"""Pillow card renderer for Minecraft status/player views."""

from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw

from .avatar import AvatarProvider, AvatarRequest, rounded_avatar
from .common import (
    effective_player_info_server_id,
    flatten_player_cards,
    get_effective_server_name,
    is_proxy_like_name,
    mode_cn,
    norm,
    safe_percent,
)
from .fonts import FontProvider
from .image import (
    DEFAULT_THEME,
    draw_header,
    draw_progress,
    draw_section_box,
    merge_images_vertical,
    new_card,
    save_png,
    status_color,
)

if TYPE_CHECKING:
    from ...core.models import PlayerDetail, PlayerInfo, ServerInfo, ServerStatus


class ImageCardRenderer:
    """Owns image assets and all Pillow drawing routines."""

    _CARD_W = DEFAULT_THEME.card_w
    _CARD_BG = DEFAULT_THEME.card_bg

    _COLOR_PRIMARY = DEFAULT_THEME.color_primary
    _COLOR_TEXT_MAIN = DEFAULT_THEME.color_text_main
    _COLOR_TEXT_SUB = DEFAULT_THEME.color_text_sub
    _COLOR_BG_LIGHT = DEFAULT_THEME.color_bg_light

    def __init__(self, cache_dir: Path):
        self._cache_dir = cache_dir
        self._font_dir = self._cache_dir / "fonts"
        self._avatar_provider = AvatarProvider(self._cache_dir / "avatars")
        self._font_provider = FontProvider(self._font_dir)
        self._assets_ready = False
        self._asset_lock = asyncio.Lock()

    async def render_multi_server_status(
        self,
        cards: list[tuple[str, "ServerInfo", "ServerStatus"]],
    ) -> BytesIO:
        await self._ensure_assets()
        # PIL drawing is CPU-bound; run it off the event loop so WebSocket
        # heartbeats and message dispatch are not blocked while rendering.
        return await asyncio.to_thread(self._render_multi_server_status_sync, cards)

    def _render_multi_server_status_sync(
        self,
        cards: list[tuple[str, "ServerInfo", "ServerStatus"]],
    ) -> BytesIO:
        images: list[Image.Image] = []
        for tag, info, status in cards:
            single = self._render_server_status_card(
                info,
                status,
                server_tag=tag,
            )
            images.append(Image.open(single).convert("RGB"))
        return merge_images_vertical(images, gap=8, pad=10)

    async def render_multi_player_list(
        self,
        cards: list[tuple[str, list["PlayerInfo"], int, str]],
    ) -> BytesIO:
        await self._ensure_assets()
        return await self._render_multi_player_list_card(cards)

    async def render_multi_player_detail(
        self,
        cards: list[tuple[str, "PlayerDetail"]],
    ) -> BytesIO:
        await self._ensure_assets()
        images: list[Image.Image] = []
        for tag, player in cards:
            single = await self._render_player_detail_card(player, server_tag=tag)
            images.append(Image.open(single).convert("RGB"))
        return merge_images_vertical(images, gap=8, pad=10)

    async def render_player_detail(
        self,
        player: "PlayerDetail",
        server_tag: str = "",
    ) -> BytesIO:
        await self._ensure_assets()
        return await self._render_player_detail_card(player, server_tag=server_tag)

    async def _ensure_assets(self):
        if self._assets_ready:
            return
        async with self._asset_lock:
            if self._assets_ready:
                return
            self._font_dir.mkdir(parents=True, exist_ok=True)
            self._avatar_provider.ensure_cache_dir()
            await self._font_provider.ensure_cached()
            self._assets_ready = True

    def _font(self, size: int):
        return self._font_provider.font(size)

    def _draw_header(
        self,
        draw: ImageDraw.ImageDraw,
        y: int,
        title: str,
        sub_title: str,
    ) -> int:
        return draw_header(draw, y, title, sub_title, self._font)

    def _draw_section_box(
        self,
        draw: ImageDraw.ImageDraw,
        y: int,
        title: str,
        bg_color: str,
        text_color: str,
        height: int,
    ):
        draw_section_box(draw, y, title, bg_color, text_color, height, self._font)

    @staticmethod
    def _rounded_avatar(img: Image.Image, radius: int = 10) -> Image.Image:
        return rounded_avatar(img, radius=radius)

    @staticmethod
    def _norm(value: str) -> str:
        return norm(value)

    def _is_proxy_like_name(self, name: str) -> bool:
        return is_proxy_like_name(name)

    def _get_effective_server_name(
        self,
        player: "PlayerInfo | PlayerDetail",
        fallback: str,
    ) -> str:
        return get_effective_server_name(player, fallback)

    async def _get_avatar(
        self,
        player_name: str,
        player_uuid: str,
        size: int,
    ) -> Image.Image:
        return await self._avatar_provider.get_avatar(player_name, player_uuid, size)

    async def _get_avatars(
        self,
        requests: list[AvatarRequest],
    ) -> dict[AvatarRequest, Image.Image]:
        return await self._avatar_provider.get_avatars(requests)

    @staticmethod
    def _new_card(estimate_h: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
        return new_card(estimate_h)

    def _render_server_status_card(
        self,
        server_info: "ServerInfo",
        server_status: "ServerStatus",
        server_tag: str = "",
    ) -> BytesIO:
        online_count = server_info.online_count or server_status.online_players
        max_players = server_info.max_players or server_status.max_players
        uptime = server_info.uptime_formatted or server_status.uptime_formatted or "未知"
        estimate_h = 430 + len(server_status.worlds) * 72
        if not server_status.is_proxy:
            estimate_h += 80
        if server_status.is_proxy:
            estimate_h += max(1, len(server_status.backends)) * 106

        image, draw = self._new_card(estimate_h)
        body_font = self._font(24)
        small_font = self._font(20)

        title = f"服务器状态  {server_info.name}"
        if server_tag:
            title = f"{title}  ({server_tag})"
        y = self._draw_header(
            draw,
            24,
            title,
            f"{server_info.platform}  {server_info.minecraft_version}",
        )

        panel_h = 106
        gap = 14
        panel_w = (self._CARD_W - 84 - gap * 2) // 3

        stats = [
            ("在线玩家", f"{online_count}/{max_players}", None),
            ("运行时间", uptime, None),
            (
                "内存使用",
                f"{server_status.memory_used}MB / {server_status.memory_max}MB",
                "memory",
            ),
        ]

        x = 42
        for title, value, metric_kind in stats:
            draw.rounded_rectangle(
                (x, y, x + panel_w, y + panel_h),
                radius=12,
                fill=self._COLOR_BG_LIGHT,
            )
            draw.text((x + 18, y + 16), title, font=small_font, fill=self._COLOR_TEXT_SUB)
            draw.text(
                (x + 18, y + 50),
                value,
                font=self._font(30) if metric_kind != "memory" else body_font,
                fill=self._COLOR_TEXT_MAIN,
            )
            if metric_kind == "memory":
                memory = safe_percent(server_status.memory_usage_percent)
                draw_progress(
                    draw,
                    x + 18,
                    y + 80,
                    panel_w - 36,
                    12,
                    memory,
                    status_color(memory, "memory"),
                )
            x += panel_w + gap

        y += panel_h + 18

        if server_info.is_proxy and server_info.aggregate_online > 0:
            draw.rounded_rectangle(
                (42, y, self._CARD_W - 42, y + 48),
                radius=10,
                fill="#eff6ff",
            )
            draw.text(
                (58, y + 12),
                f"总在线: {server_info.aggregate_online}/{server_info.aggregate_max}",
                font=body_font,
                fill="#1d4ed8",
            )
            y += 60

        if not server_status.is_proxy:
            draw.rounded_rectangle(
                (42, y, self._CARD_W - 42, y + 84),
                radius=12,
                fill=self._COLOR_BG_LIGHT,
            )
            draw.text(
                (58, y + 14),
                "TPS (1m / 5m / 15m)",
                font=small_font,
                fill=self._COLOR_TEXT_SUB,
            )
            tx = 58
            for idx, value in enumerate(
                (server_status.tps_1m, server_status.tps_5m, server_status.tps_15m)
            ):
                text = f"{value:.1f}"
                draw.text(
                    (tx, y + 44),
                    text,
                    font=body_font,
                    fill=status_color(value, "tps"),
                )
                tx += int(draw.textlength(text, font=body_font)) + 14
                if idx < 2:
                    draw.text((tx, y + 44), "|", font=body_font, fill="#9ca3af")
                    tx += 14
            y += 96

        if server_status.worlds:
            draw.text((42, y), "世界列表", font=self._font(30), fill=self._COLOR_TEXT_MAIN)
            y += 46
            for world in server_status.worlds:
                draw.rounded_rectangle(
                    (42, y, self._CARD_W - 42, y + 50),
                    radius=10,
                    fill="#f9fafb",
                )
                draw.text(
                    (58, y + 11),
                    str(world.get("name", "world")),
                    font=body_font,
                    fill="#374151",
                )
                metric = (
                    f"玩家 {world.get('players', 0)}   "
                    f"实体 {world.get('entities', 0)}   "
                    f"区块 {world.get('loadedChunks', 0)}"
                )
                tw = int(draw.textlength(metric, font=small_font))
                draw.text(
                    (self._CARD_W - 58 - tw, y + 14),
                    metric,
                    font=small_font,
                    fill=self._COLOR_TEXT_SUB,
                )
                y += 60

        card = image.crop((0, 0, self._CARD_W, min(max(y + 28, 280), image.height)))
        return save_png(card)

    def _effective_player_server_id(
        self,
        player: "PlayerDetail",
        fallback: str,
    ) -> str:
        return self._get_effective_server_name(player, fallback)

    def _effective_player_info_server_id(
        self,
        player: "PlayerInfo",
        fallback: str,
    ) -> str:
        return effective_player_info_server_id(player, fallback)

    def _flatten_player_cards(
        self,
        cards: list[tuple[str, list["PlayerInfo"], int, str]],
    ) -> list[tuple[str, list["PlayerInfo"], int, str]]:
        return flatten_player_cards(cards)

    async def _render_multi_player_list_card(
        self,
        cards: list[tuple[str, list["PlayerInfo"], int, str]],
    ) -> BytesIO:
        flattened = self._flatten_player_cards(cards)
        total_players = sum(
            (total if total > 0 else len(players)) for _, players, total, _ in flattened
        )
        row_count = sum(max(1, len(players)) for _, players, _, _ in flattened)
        estimate_h = 170 + row_count * 74 + len(flattened) * 56

        image, draw = self._new_card(estimate_h)
        body_font = self._font(24)
        small_font = self._font(20)

        y = self._draw_header(
            draw,
            24,
            f"在线玩家总览 ({total_players})",
            "实时在线玩家列表",
        )

        if not flattened:
            draw.rounded_rectangle(
                (34, y, self._CARD_W - 34, y + 84),
                radius=12,
                fill="#f9fafb",
            )
            draw.text(
                (self._CARD_W // 2 - 110, y + 30),
                "当前没有玩家在线",
                font=body_font,
                fill="#9ca3af",
            )
            y += 96
        else:
            avatar_requests = [
                (player.name, player.uuid, 50)
                for _, players, _, _ in flattened
                for player in players
            ]
            avatar_map = await self._get_avatars(avatar_requests)
            for server_id, players, total, server_name in flattened:
                server_count = total if total > 0 else len(players)
                draw.rounded_rectangle(
                    (34, y, self._CARD_W - 34, y + 40),
                    radius=9,
                    fill="#dbeafe",
                )
                server_title = server_name or server_id
                draw.text(
                    (46, y + 10),
                    f"服务器: {server_title}   ({server_count}人)",
                    font=small_font,
                    fill="#1d4ed8",
                )
                y += 48

                for row_idx, player in enumerate(players):
                    row_bg = self._CARD_BG if row_idx % 2 == 0 else self._COLOR_BG_LIGHT
                    ping_color = status_color(player.ping, "ping")
                    draw.rounded_rectangle(
                        (42, y, self._CARD_W - 42, y + 68),
                        radius=10,
                        fill=row_bg,
                        outline="#f3f4f6",
                    )
                    draw.rounded_rectangle(
                        (46, y + 8, 52, y + 60),
                        radius=3,
                        fill=ping_color,
                    )

                    avatar_key = (player.name, player.uuid, 50)
                    avatar = avatar_map.get(avatar_key)
                    if avatar is None:
                        avatar = await self._get_avatar(player.name, player.uuid, size=50)
                    avatar = self._rounded_avatar(avatar, radius=10)
                    image.paste(avatar, (54, y + 9), avatar)

                    draw.text(
                        (118, y + 11),
                        player.name,
                        font=body_font,
                        fill=self._COLOR_TEXT_MAIN,
                    )
                    mode = mode_cn(player.game_mode)
                    line = f"模式 {mode}   世界 {player.world or '未知'}"
                    draw.text(
                        (118, y + 38),
                        line,
                        font=small_font,
                        fill=self._COLOR_TEXT_SUB,
                    )

                    ping_text = f"{player.ping}ms"
                    tw = int(draw.textlength(ping_text, font=small_font))
                    draw.text(
                        (self._CARD_W - 56 - tw, y + 23),
                        ping_text,
                        font=small_font,
                        fill=ping_color,
                    )
                    y += 76
                y += 8

        card = image.crop((0, 0, self._CARD_W, min(max(y + 20, 240), image.height)))
        return save_png(card)

    async def _render_player_detail_card(
        self,
        player: "PlayerDetail",
        server_tag: str = "",
    ) -> BytesIO:
        estimate_h = 820 if player.location else 760
        image, draw = self._new_card(estimate_h)
        body_font = self._font(24)
        small_font = self._font(20)

        y = 24
        detail_server_name = self._effective_player_server_id(player, server_tag)
        if detail_server_name:
            badge = f"服务器: {detail_server_name}"
            badge_w = int(draw.textlength(badge, font=small_font)) + 30
            draw.rounded_rectangle(
                (
                    self._CARD_W - badge_w - 30,
                    y + 6,
                    self._CARD_W - 30,
                    y + 44,
                ),
                radius=10,
                fill="#dbeafe",
            )
            draw.text(
                (self._CARD_W - badge_w - 14, y + 14),
                badge,
                font=small_font,
                fill="#1d4ed8",
            )

        avatar = await self._get_avatar(player.name, player.uuid, size=92)
        avatar = self._rounded_avatar(avatar, radius=14)
        image.paste(avatar, (34, y), avatar)

        draw.text((142, y + 4), player.name, font=self._font(42), fill=self._COLOR_TEXT_MAIN)
        draw.text((142, y + 58), player.uuid, font=small_font, fill=self._COLOR_TEXT_SUB)
        if player.is_op:
            draw.rounded_rectangle(
                (430, y + 10, 560, y + 46),
                radius=8,
                fill="#fef3c7",
                outline="#fcd34d",
            )
            draw.text((454, y + 18), "管理员", font=small_font, fill="#b45309")
        y += 136

        sections = [
            (
                "▶ 基础信息",
                "#ecfeff",
                "#155e75",
                110,
                [
                    ((44, 0), f"世界: {player.world or '未知'}", self._COLOR_TEXT_MAIN),
                    ((370, 0), f"模式: {mode_cn(player.game_mode)}", self._COLOR_TEXT_MAIN),
                    ((700, 0), f"延迟: {player.ping}ms", status_color(player.ping, "ping")),
                ],
            ),
            (
                "▶ 状态面板",
                "#eef2ff",
                "#3730a3",
                220,
                [
                    (
                        "progress",
                        44,
                        f"生命值 {player.health:.1f}/{player.max_health:.1f}",
                        (player.health / player.max_health * 100)
                        if player.max_health
                        else 0,
                        "#ef4444",
                    ),
                    ("progress", 94, f"饥饿值 {player.food_level}/20", player.food_level * 5, "#f59e0b"),
                    (
                        "progress",
                        144,
                        f"等级 {player.level} ({player.exp * 100:.1f}%)",
                        player.exp * 100,
                        "#10b981",
                    ),
                ],
            ),
            (
                "▶ 在线信息",
                "#f0fdf4",
                "#166534",
                158 if player.location else 110,
                [
                    (
                        (44, 0),
                        f"在线时长: {player.online_time_formatted or '未知'}",
                        self._COLOR_TEXT_MAIN,
                    ),
                ],
            ),
        ]

        if player.location:
            loc_text = (
                f"位置: X={player.location.get('x', 0):.1f}, "
                f"Y={player.location.get('y', 0):.1f}, "
                f"Z={player.location.get('z', 0):.1f}"
            )
            sections[2][4].append(((44, 48), loc_text, self._COLOR_TEXT_MAIN))

        for title, bg, text_color, height, items in sections:
            self._draw_section_box(draw, y, title, bg, text_color, height)
            for item in items:
                if item[0] == "progress":
                    _, progress_y, label, percent, color = item
                    draw.text(
                        (44, y + progress_y + 20),
                        label,
                        font=body_font,
                        fill="#374151",
                    )
                    draw_progress(
                        draw,
                        44,
                        y + progress_y + 54,
                        self._CARD_W - 88,
                        12,
                        safe_percent(percent),
                        color,
                    )
                else:
                    (item_x, item_y), text, color = item
                    draw.text(
                        (item_x, y + item_y + 64),
                        text,
                        font=body_font,
                        fill=color,
                    )
            y += height + 24

        card = image.crop((0, 0, self._CARD_W, min(max(y + 10, 260), image.height)))
        return save_png(card)
