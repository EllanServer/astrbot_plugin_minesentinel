"""Minecraft avatar fetching and disk cache."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from PIL import Image, ImageDraw

from astrbot.api import logger

from .common import norm

try:
    import aiohttp
except Exception:  # pragma: no cover - exercised in minimal test environments.
    aiohttp = None


AvatarRequest = tuple[str, str, int]

# Max in-memory avatar images to cache. Each is small (size×size RGBA), so 256
# entries is negligible memory but avoids repeated disk reads / HTTP fetches for
# the same player across status cards, player lists, and detail cards.
_AVATAR_LRU_SIZE = 256


class AvatarProvider:
    def __init__(self, avatar_dir: Path, max_concurrency: int = 8):
        self.avatar_dir = avatar_dir
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        # Lazy-init: created on first fetch and reused across all subsequent
        # calls. Each aiohttp.ClientSession opens a connection pool, so
        # re-using one avoids repeated TCP/TLS handshakes for the same CDN.
        self._session: aiohttp.ClientSession | None = None
        # LRU cache of recently loaded avatar images keyed by
        # (player_name_lower, player_uuid_lower, size).
        self._image_lru: OrderedDict[tuple[str, str, int], Image.Image] = OrderedDict()

    def ensure_cache_dir(self):
        self.avatar_dir.mkdir(parents=True, exist_ok=True)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session

    async def close(self):
        """Close the reusable HTTP session. Call on shutdown."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    def _lru_get(self, key: tuple[str, str, int]) -> Image.Image | None:
        img = self._image_lru.get(key)
        if img is not None:
            self._image_lru.move_to_end(key)
        return img

    def _lru_put(self, key: tuple[str, str, int], img: Image.Image):
        self._image_lru[key] = img
        self._image_lru.move_to_end(key)
        while len(self._image_lru) > _AVATAR_LRU_SIZE:
            self._image_lru.popitem(last=False)

    async def get_avatar(
        self,
        player_name: str,
        player_uuid: str,
        size: int,
    ) -> Image.Image:
        async with self._semaphore:
            return await self._get_avatar_uncapped(player_name, player_uuid, size)

    async def get_avatars(
        self,
        requests: list[AvatarRequest],
    ) -> dict[AvatarRequest, Image.Image]:
        unique_requests = list(dict.fromkeys(requests))
        avatars = await asyncio.gather(
            *(
                self.get_avatar(player_name, player_uuid, size)
                for player_name, player_uuid, size in unique_requests
            )
        )
        return dict(zip(unique_requests, avatars))

    async def _get_avatar_uncapped(
        self,
        player_name: str,
        player_uuid: str,
        size: int,
    ) -> Image.Image:
        # LRU check first — avoids disk I/O entirely on cache hit.
        lru_key = (norm(player_name).lower(), norm(player_uuid).lower(), size)
        cached = self._lru_get(lru_key)
        if cached is not None:
            return cached

        self.ensure_cache_dir()
        key = self.cache_key(player_name, player_uuid)
        path = self.avatar_dir / f"{key}_{size}.png"
        if path.exists():
            try:
                img = Image.open(path).convert("RGBA")
                self._lru_put(lru_key, img)
                return img
            except Exception:
                with contextlib.suppress(Exception):
                    path.unlink()

        face = await self.fetch_avatar_face(player_name, player_uuid)
        if face is None:
            face = placeholder_avatar_face()
        avatar = face.resize((size, size), Image.Resampling.NEAREST)
        with contextlib.suppress(Exception):
            avatar.save(path, format="PNG")
        self._lru_put(lru_key, avatar)
        return avatar

    async def fetch_avatar_face(
        self,
        player_name: str,
        player_uuid: str,
    ) -> Image.Image | None:
        if aiohttp is None:
            return None
        name = norm(player_name)
        uuid = norm(player_uuid).replace("-", "")
        try:
            session = await self._get_session()
            if name:
                for url in (
                    f"https://mc-heads.net/avatar/{quote(name)}/8",
                    f"https://minotar.net/helm/{quote(name)}/8.png",
                ):
                    img = await download_image(session, url)
                    if img is not None:
                        return img.resize((8, 8), Image.Resampling.NEAREST)
            if uuid:
                for url in (
                    f"https://crafatar.com/avatars/{uuid}?size=8&overlay",
                    f"https://mc-heads.net/avatar/{uuid}/8",
                ):
                    img = await download_image(session, url)
                    if img is not None:
                        return img.resize((8, 8), Image.Resampling.NEAREST)

            resolved_uuid = uuid
            if not resolved_uuid and name:
                lookup = (
                    f"https://api.mojang.com/users/profiles/minecraft/{quote(name)}"
                )
                async with session.get(lookup) as resp:
                    if resp.status == 200:
                        profile = await resp.json(content_type=None)
                        resolved_uuid = str(profile.get("id", ""))
            if not resolved_uuid:
                return None

            profile_url = (
                "https://sessionserver.mojang.com/session/minecraft/profile/"
                f"{resolved_uuid}"
            )
            async with session.get(profile_url) as resp:
                if resp.status != 200:
                    return None
                profile_data = await resp.json(content_type=None)

            textures_b64 = ""
            for prop in profile_data.get("properties", []):
                if prop.get("name") == "textures":
                    textures_b64 = prop.get("value", "")
                    break
            if not textures_b64:
                return None

            decoded = base64.b64decode(textures_b64).decode("utf-8")
            textures_obj = json.loads(decoded)
            skin_url = (
                textures_obj.get("textures", {}).get("SKIN", {}).get("url", "")
            )
            if not skin_url:
                return None
            skin = await download_image(session, skin_url)
            if skin is None or skin.width < 16 or skin.height < 16:
                return None
            face = skin.crop((8, 8, 16, 16))
            if skin.width >= 64 and skin.height >= 16:
                overlay = skin.crop((40, 8, 48, 16))
                face = Image.alpha_composite(face, overlay)
            return face
        except Exception as exc:
            logger.debug(
                f"[Renderer] 获取玩家头像失败: {player_name}/{player_uuid} -> {exc}"
            )
            return None

    @staticmethod
    def cache_key(player_name: str, player_uuid: str) -> str:
        return (
            norm(player_name).lower()
            or norm(player_uuid).replace("-", "").lower()
            or "unknown"
        )


async def download_image(
    session: aiohttp.ClientSession,
    url: str,
) -> Image.Image | None:
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            data = await resp.read()
        if not data:
            return None
        return Image.open(BytesIO(data)).convert("RGBA")
    except Exception:
        return None


def placeholder_avatar_face() -> Image.Image:
    face = Image.new("RGBA", (8, 8), "#d1d5db")
    draw = ImageDraw.Draw(face)
    for yy in range(0, 8, 2):
        for xx in range((yy // 2) % 2, 8, 2):
            draw.point((xx, yy), fill="#9ca3af")
    draw.point((2, 3), fill="#374151")
    draw.point((5, 3), fill="#374151")
    return face


def rounded_avatar(img: Image.Image, radius: int = 10) -> Image.Image:
    avatar = img.convert("RGBA")
    mask = Image.new("L", avatar.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        (0, 0, avatar.width, avatar.height),
        radius=radius,
        fill=255,
    )
    avatar.putalpha(mask)
    return avatar
