"""Font discovery and disk cache for rendered cards."""

from __future__ import annotations

from pathlib import Path

from PIL import ImageFont

from astrbot.api import logger

try:
    import aiohttp
except Exception:  # pragma: no cover - minimal test environments may omit aiohttp.
    aiohttp = None


DEFAULT_FONT_FILENAME = "LXGWWenKaiGB-Regular.ttf"
DEFAULT_FONT_URLS = [
    "https://ghproxy.net/https://raw.githubusercontent.com/lxgw/LxgwWenkaiGB/main/fonts/TTF/LXGWWenKaiGB-Regular.ttf",
    "https://jsd.cdn.zzko.cn/gh/lxgw/LxgwWenkaiGB@main/fonts/TTF/LXGWWenKaiGB-Regular.ttf",
    "https://raw.githubusercontent.com/lxgw/LxgwWenkaiGB/main/fonts/TTF/LXGWWenKaiGB-Regular.ttf",
    "https://cdn.jsdelivr.net/gh/lxgw/LxgwWenkaiGB@main/fonts/TTF/LXGWWenKaiGB-Regular.ttf",
]


class FontProvider:
    def __init__(
        self,
        font_dir: Path,
        filename: str = DEFAULT_FONT_FILENAME,
        urls: list[str] | None = None,
    ):
        self.font_dir = font_dir
        self.font_path = font_dir / filename
        self.urls = urls or DEFAULT_FONT_URLS
        # Cache of loaded ImageFont objects keyed by (resolved_source, size).
        # ImageFont.truetype parses the font file every call; for a multi-MB
        # CJK font and repeated renders this is a major cost. Resolution of the
        # underlying file is stable for the provider lifetime, so we cache by
        # (source_label, size) once a font loads successfully.
        self._font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}
        self._resolved_source: str | None = None

    async def ensure_cached(self):
        self.font_dir.mkdir(parents=True, exist_ok=True)
        if self.font_path.exists() and self.font_path.stat().st_size > 0:
            self._resolved_source = str(self.font_path)
            return
        if aiohttp is None:
            logger.warning("[Renderer] aiohttp 不可用，跳过字体下载")
            return

        timeout = aiohttp.ClientTimeout(total=60)
        for url in self.urls:
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.read()
                if len(data) < 100 * 1024:
                    continue
                self.font_path.write_bytes(data)
                logger.info(
                    f"[Renderer] 已缓存中文字体: {self.font_path.name} ({len(data) // 1024}KB)"
                )
                self._resolved_source = str(self.font_path)
                return
            except Exception as exc:
                logger.debug(f"[Renderer] 字体下载失败: {url} -> {exc}")
        logger.warning("[Renderer] 字体下载失败，将回退到系统字体")

    def font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        source = self._resolve_source()
        cache_key = (source, size)
        cached = self._font_cache.get(cache_key)
        if cached is not None:
            return cached
        loaded = self._load_font(source, size)
        self._font_cache[cache_key] = loaded
        return loaded

    def _resolve_source(self) -> str:
        """Return the best available font source path/name, cached on first use."""
        if self._resolved_source is not None:
            return self._resolved_source
        if self.font_path.exists() and self.font_path.stat().st_size > 0:
            self._resolved_source = str(self.font_path)
            return self._resolved_source
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            custom_font = Path(get_astrbot_data_path()) / "font.ttf"
            if custom_font.exists() and custom_font.stat().st_size > 0:
                self._resolved_source = str(custom_font)
                return self._resolved_source
        except Exception:
            pass
        for font_name in (
            "msyh.ttc",
            "simsun.ttc",
            "NotoSansCJK-Regular.ttc",
            "wqy-microhei.ttc",
            "PingFang.ttc",
            "DroidSansFallback.ttf",
        ):
            try:
                # Probe availability by attempting a load; reuse on success.
                ImageFont.truetype(font_name, size=12)
                self._resolved_source = font_name
                return self._resolved_source
            except Exception:
                continue
        # Nothing usable; fall back to default bitmap font under a sentinel key.
        self._resolved_source = "__default__"
        return self._resolved_source

    def _load_font(
        self, source: str, size: int
    ) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        if source != "__default__":
            try:
                return ImageFont.truetype(source, size=size)
            except Exception as exc:
                logger.debug(f"[Renderer] 加载已缓存字体失败: {exc}")
                # Invalidate the resolved source so a later call can re-resolve.
                self._resolved_source = None
        if not self.font_path.exists():
            logger.warning(
                f"[Renderer] 无法加载中文字体，渲染结果可能乱码。请确保网络畅通以自动下载字体，或手动放置字体文件到: {self.font_path}"
            )
        return ImageFont.load_default()
