"""Font discovery and disk cache for rendered cards."""

from __future__ import annotations

from pathlib import Path

from PIL import ImageFont

from astrbot.api import logger

try:
    import aiohttp
except Exception:  # pragma: no cover - minimal test environments may omit aiohttp.
    aiohttp = None


DEFAULT_FONT_FILENAME = "NotoSansSC-Variable.ttf"
# 可选的字体 SHA-256 校验值。留空表示不校验哈希（仍会校验字体头部魔数与
# 体积上限）。若运维通过环境变量 MINESENTINEL_FONT_SHA256 设置了正确哈希，
# 下载内容必须匹配才落盘，防止第三方 CDN 被劫持投递恶意字体经 freetype
# 原生解析触发 RCE。
import os as _os

DEFAULT_FONT_SHA256 = (_os.environ.get("MINESENTINEL_FONT_SHA256") or "").lower()
DEFAULT_FONT_URLS = [
    "https://raw.githubusercontent.com/google/fonts/main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf",
    "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf",
    "https://ghproxy.net/https://raw.githubusercontent.com/google/fonts/main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf",
]
# 字体下载体积上限（字节）。Noto Sans SC Variable TTF 约 18MB，设 32MB
# 余量。超过即拒绝并中止下载，防止恶意服务器返回超大响应耗尽内存。
MAX_FONT_DOWNLOAD_BYTES = 32 * 1024 * 1024
# 小于此值视为无效字体文件。
MIN_FONT_DOWNLOAD_BYTES = 100 * 1024


def _sha256_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _is_valid_ttf_header(data: bytes) -> bool:
    """快速校验下载内容是否为合法 TrueType/OpenType 字体头部。

    TTF/OTF 文件以 sfnt 版本魔数开头：
    - TrueType: 0x00010000 或 'true'
    - OpenType (CFF): 'OTTO'
    - TTC: 'ttcf'
    这一层校验无法替代完整 freetype 解析，但能在落盘前挡掉明显非字体
    二进制（如 HTML 错误页、脚本），缩小攻击面。
    """
    if len(data) < 4:
        return False
    head = data[:4]
    return head in (
        b"\x00\x01\x00\x00",  # TTF v1
        b"true",  # TTF (Apple variant)
        b"OTTO",  # OTF (CFF)
        b"ttcf",  # TTC collection
        b"typ1",  # PostScript Type 1
    )


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
        # Cache of loaded ImageFont objects keyed by (resolved_source, size, weight).
        # ImageFont.truetype parses the font file every call; for a multi-MB
        # CJK font and repeated renders this is a major cost. Resolution of the
        # underlying file is stable for the provider lifetime, so we cache by
        # (source_label, size) once a font loads successfully.
        self._font_cache: dict[
            tuple[str, int, str],
            ImageFont.FreeTypeFont | ImageFont.ImageFont,
        ] = {}
        self._resolved_source: str | None = None

    async def ensure_cached(self):
        self.font_dir.mkdir(parents=True, exist_ok=True)
        # 避免 exists() 与 stat() 之间的 TOCTOU 竞态：直接 stat()，文件被删除
        # 时抛 FileNotFoundError。
        try:
            if self.font_path.stat().st_size > 0:
                self._resolved_source = str(self.font_path)
                return
        except FileNotFoundError:
            pass
        if aiohttp is None:
            logger.warning("[Renderer] aiohttp 不可用，跳过字体下载")
            return

        timeout = aiohttp.ClientTimeout(total=60)
        # 复用同一个 ClientSession 跨多个 URL，避免每 URL 新建连接池的开销。
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for url in self.urls:
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            continue
                        # 流式下载到内存并强制上限，防止恶意服务器返回
                        # 超大响应耗尽内存（DoS/OOM）。
                        data = bytearray()
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            data.extend(chunk)
                            if len(data) > MAX_FONT_DOWNLOAD_BYTES:
                                logger.debug(
                                    f"[Renderer] 字体下载超过上限 "
                                    f"{MAX_FONT_DOWNLOAD_BYTES} 字节，中止: {url}"
                                )
                                data = None
                                break
                        if data is None:
                            continue
                        data = bytes(data)
                    if len(data) < MIN_FONT_DOWNLOAD_BYTES:
                        continue
                    # 校验字体头部魔数，挡掉 HTML 错误页/脚本等非字体二进制。
                    if not _is_valid_ttf_header(data):
                        logger.debug(f"[Renderer] 字体头部魔数非法，跳过: {url}")
                        continue
                    # 若配置了 SHA-256，强制校验；不匹配则拒绝落盘。
                    if DEFAULT_FONT_SHA256:
                        digest = _sha256_hex(data)
                        if digest != DEFAULT_FONT_SHA256:
                            logger.warning(
                                f"[Renderer] 字体 SHA-256 不匹配，拒绝落盘: {url} "
                                f"(expected={DEFAULT_FONT_SHA256[:12]}..., got={digest[:12]}...)"
                            )
                            continue
                    # 原子写：先写临时文件再 rename，避免并发写盘竞态写出损坏字体。
                    tmp_path = self.font_path.with_suffix(self.font_path.suffix + ".tmp")
                    tmp_path.write_bytes(data)
                    tmp_path.replace(self.font_path)
                    logger.info(
                        f"[Renderer] 已缓存中文字体: {self.font_path.name} ({len(data) // 1024}KB)"
                    )
                    self._resolved_source = str(self.font_path)
                    return
                except Exception as exc:
                    logger.debug(f"[Renderer] 字体下载失败: {url} -> {exc}")
        logger.warning("[Renderer] 字体下载失败，将回退到系统字体")

    def font(
        self,
        size: int,
        weight: str = "regular",
    ) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        source = self._resolve_source()
        normalized_weight = str(weight or "regular").lower()
        cache_key = (source, size, normalized_weight)
        cached = self._font_cache.get(cache_key)
        if cached is not None:
            return cached
        loaded = self._load_font(source, size, normalized_weight)
        self._font_cache[cache_key] = loaded
        return loaded

    def _resolve_source(self) -> str:
        """Return the best available font source path/name, cached on first use."""
        if self._resolved_source is not None:
            return self._resolved_source
        # 避免 exists() 与 stat() 之间的 TOCTOU 竞态。
        try:
            if self.font_path.stat().st_size > 0:
                self._resolved_source = str(self.font_path)
                return self._resolved_source
        except FileNotFoundError:
            pass
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            custom_font = Path(get_astrbot_data_path()) / "font.ttf"
            # 直接 stat()，文件不存在时由下方 except 捕获。
            if custom_font.stat().st_size > 0:
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
        self,
        source: str,
        size: int,
        weight: str = "regular",
    ) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        if source != "__default__":
            try:
                loaded = ImageFont.truetype(source, size=size)
                variation = {
                    "thin": b"Thin",
                    "light": b"Light",
                    "regular": b"Regular",
                    "medium": b"Medium",
                    "semibold": b"SemiBold",
                    "bold": b"Bold",
                }.get(weight, b"Regular")
                try:
                    if variation in loaded.get_variation_names():
                        loaded.set_variation_by_name(variation)
                except (AttributeError, OSError):
                    pass
                return loaded
            except Exception as exc:
                logger.debug(f"[Renderer] 加载已缓存字体失败: {exc}")
                # Invalidate the resolved source so a later call can re-resolve.
                self._resolved_source = None
        if not self.font_path.exists():
            logger.warning(
                f"[Renderer] 无法加载中文字体，渲染结果可能乱码。请确保网络畅通以自动下载字体，或手动放置字体文件到: {self.font_path}"
            )
        return ImageFont.load_default()
