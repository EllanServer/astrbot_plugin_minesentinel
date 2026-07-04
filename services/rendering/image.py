"""Small image helpers for MineSentinel report rendering."""

from __future__ import annotations

from io import BytesIO

from PIL import Image


def save_png(image: Image.Image) -> BytesIO:
    out = BytesIO()
    image.save(out, format="PNG", optimize=True)
    out.seek(0)
    return out
