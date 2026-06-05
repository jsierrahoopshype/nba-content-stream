"""Duotone portrait treatment + cover-crop for the v2.2 full-bleed hero.

Every player's headshot is mapped to a two-tone brand-blue/white wash so
the spotlight reads visually consistent and bold regardless of the
source photo's lighting or color. Shadows map to a deep brand blue,
highlights to near-white.
"""

from __future__ import annotations

import io
import logging
import math

from PIL import Image, ImageOps

from . import format_specs as fs
from . import style22

logger = logging.getLogger("duotone")


def _build_lut(shadow: tuple[int, int, int], highlight: tuple[int, int, int]):
    """Per-channel 256-entry LUTs mapping luminance 0→shadow, 255→highlight."""
    luts = []
    for ch in range(3):
        lo, hi = shadow[ch], highlight[ch]
        luts.append([int(lo + (hi - lo) * v / 255.0) for v in range(256)])
    return luts


def duotone(
    img: Image.Image,
    *,
    shadow: str = style22.DUOTONE_SHADOW,
    highlight: str = style22.DUOTONE_HIGHLIGHT,
    autocontrast: bool = True,
) -> Image.Image:
    """Map `img` to a brand-blue duotone. Returns RGBA."""
    g = img.convert("L")
    if autocontrast:
        g = ImageOps.autocontrast(g, cutoff=1)
    luts = _build_lut(fs.hex_to_rgb(shadow), fs.hex_to_rgb(highlight))
    r = g.point(luts[0])
    gg = g.point(luts[1])
    b = g.point(luts[2])
    return Image.merge("RGB", (r, gg, b)).convert("RGBA")


def cover_crop(img: Image.Image, size: tuple[int, int], *, anchor: str = "center") -> Image.Image:
    """Object-fit: cover. Scale `img` to fully cover `size`, then crop.
    `anchor="top"` keeps faces in frame for tall portraits."""
    cw, ch = size
    scale = max(cw / max(1, img.width), ch / max(1, img.height))
    nw = max(cw, int(math.ceil(img.width * scale)))
    nh = max(ch, int(math.ceil(img.height * scale)))
    resized = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - cw) // 2
    top = 0 if anchor == "top" else (nh - ch) // 2
    return resized.crop((left, top, left + cw, top + ch))


def duotone_panel(size: tuple[int, int], *, shadow: str = style22.DUOTONE_SHADOW) -> Image.Image:
    """A solid brand-blue panel at `size` — the headshot-404 fallback
    base (initials are drawn on top by the caller at full scale)."""
    return Image.new("RGBA", size, fs.hex_to_rgb(shadow) + (255,))


def hero_portrait(
    portrait_bytes: bytes | None,
    size: tuple[int, int],
    *,
    anchor: str = "top",
) -> tuple[Image.Image, bool]:
    """Return a duotoned, cover-cropped portrait filling `size`, plus a
    `had_image` flag. On decode failure / 404 returns a solid brand
    duotone panel so the caller can draw big initials over it (never a
    tiny circle)."""
    if portrait_bytes:
        try:
            src = Image.open(io.BytesIO(portrait_bytes)).convert("RGBA")
            return cover_crop(duotone(src), size, anchor=anchor), True
        except Exception as exc:  # noqa: BLE001
            logger.warning("hero portrait decode failed: %s", exc)
    return duotone_panel(size), False
