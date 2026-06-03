"""Parallax + Ken Burns helpers for the v2 spotlight renderer.

MoviePy has no native parallax — we get it by rendering each frame
ourselves with shifted/scaled layers, exactly like the v1 renderers
draw per-frame PIL images. This module keeps the *math* (zoom curves,
parallax offsets, letter-stagger alphas) as pure functions so they're
unit-testable, and wraps the heavier PIL transforms (gradient plate,
scaled placement, Ken Burns pan, desaturate+blur) thinly on top.

Convention: every transform returns an RGBA image at the canvas size
so the caller can `alpha_composite` it straight onto the frame.
"""

from __future__ import annotations

from PIL import Image, ImageEnhance, ImageFilter

from . import easing
from . import format_specs as fs

# Default gradient endpoints: HoopsMatic blue → deep navy. The hero
# background plate parallaxes behind the portrait.
GRADIENT_TOP = fs.ACCENT          # "#3b82f6"
GRADIENT_BOTTOM = "#0a1730"       # deep navy so the portrait pops


# ---------------------------------------------------------------------------
# Pure math — tested directly.
# ---------------------------------------------------------------------------


def zoom_scale(t: float, *, start: float = 1.0, end: float = 0.6, ease=easing.quart_out) -> float:
    """Portrait zoom factor at progress `t` (0→1). Defaults match the
    brief: 100% → 60% over the hero phase, decelerating (quart-out)."""
    e = ease(max(0.0, min(1.0, t)))
    return start + (end - start) * e


def parallax_offset(
    t: float, *, distance: float, speed: float = 1.0, ease=easing.quart_out
) -> float:
    """Pixel offset of a parallax layer at progress `t`.

    `speed` < 1 makes the layer move slower than the foreground — the
    background gradient uses 0.3 so it drifts at 0.3× the portrait's
    motion, the cue that sells depth.
    """
    e = ease(max(0.0, min(1.0, t)))
    return distance * e * speed


def letter_stagger_alpha(
    index: int, t: float, *, per_letter: float = 0.05, fade_in: float = 0.25
) -> float:
    """Alpha (0→1) for the `index`-th letter of a staggered name
    reveal at time `t` seconds. Each letter starts `per_letter` (50ms)
    after the previous and fades in over `fade_in` with quart-out."""
    local = t - index * per_letter
    if local <= 0:
        return 0.0
    return easing.quart_out(min(1.0, local / fade_in))


def ken_burns_offset(t: float, *, pan_px: float = 40.0, ease=easing.sin_in_out) -> float:
    """Horizontal pan offset for a Ken Burns move at progress `t`.

    Centered so the pan runs from `-pan_px/2` to `+pan_px/2` — the
    midpoint sits dead-center, avoiding a hard edge at either end.
    """
    e = ease(max(0.0, min(1.0, t)))
    return -pan_px / 2 + pan_px * e


# ---------------------------------------------------------------------------
# PIL transforms — thin wrappers around the math above.
# ---------------------------------------------------------------------------


def vertical_gradient(
    size: tuple[int, int], top: str = GRADIENT_TOP, bottom: str = GRADIENT_BOTTOM
) -> Image.Image:
    """An RGBA vertical gradient plate `size` px, `top`→`bottom`.

    Built oversized-agnostic: we render the gradient as a 1-px-wide
    column then stretch — cheap and exact. numpy keeps it vectorized.
    """
    import numpy as np

    w, h = size
    h = max(1, h)
    w = max(1, w)
    ramp = np.linspace(0.0, 1.0, h)[:, None]
    top_rgb = np.array(fs.hex_to_rgb(top), dtype=np.float32)
    bot_rgb = np.array(fs.hex_to_rgb(bottom), dtype=np.float32)
    col = (top_rgb * (1.0 - ramp) + bot_rgb * ramp).astype(np.uint8)  # (h, 3)
    rows = np.repeat(col[:, None, :], w, axis=1)                       # (h, w, 3)
    alpha = np.full((h, w, 1), 255, dtype=np.uint8)
    arr = np.concatenate([rows, alpha], axis=2)
    # Mode is inferred from the 4-channel array; passing it explicitly is
    # deprecated in Pillow 12+.
    return Image.fromarray(arr).convert("RGBA")


def place_scaled(
    img: Image.Image,
    canvas_size: tuple[int, int],
    scale: float,
    offset: tuple[float, float] = (0.0, 0.0),
) -> Image.Image:
    """Center `img` on a transparent `canvas_size` canvas, scaled by
    `scale` and shifted by `offset`. Returns the RGBA canvas."""
    cw, ch = canvas_size
    nw = max(1, int(round(img.width * scale)))
    nh = max(1, int(round(img.height * scale)))
    scaled = img.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    x = (cw - nw) // 2 + int(round(offset[0]))
    y = (ch - nh) // 2 + int(round(offset[1]))
    canvas.alpha_composite(scaled.convert("RGBA"), (x, y))
    return canvas


def cover_scale(img_size: tuple[int, int], canvas_size: tuple[int, int]) -> float:
    """Scale factor so `img_size` fully covers `canvas_size` (object-fit:
    cover) — the larger of the two axis ratios."""
    iw, ih = img_size
    cw, ch = canvas_size
    return max(cw / max(1, iw), ch / max(1, ih))


def ken_burns(
    img: Image.Image,
    canvas_size: tuple[int, int],
    t: float,
    *,
    pan_px: float = 40.0,
    zoom_from: float = 1.08,
    zoom_to: float = 1.20,
    ease=easing.sin_in_out,
) -> Image.Image:
    """Slow pan + slight zoom over a cover-cropped `img`. Returns an
    RGBA canvas the size of `canvas_size`. Used for the desaturated
    blurred headshot behind the quote phase."""
    e = ease(max(0.0, min(1.0, t)))
    base = cover_scale(img.size, canvas_size)
    scale = base * (zoom_from + (zoom_to - zoom_from) * e)
    offx = ken_burns_offset(t, pan_px=pan_px, ease=ease)
    return place_scaled(img, canvas_size, scale, offset=(offx, 0.0))


def desaturate_blur(
    img: Image.Image,
    *,
    blur: int = 18,
    saturation: float = 0.25,
    brightness: float = 0.55,
) -> Image.Image:
    """Desaturate, blur, and darken `img` so quote text reads on top of
    it. Returns RGBA."""
    rgb = img.convert("RGB")
    rgb = ImageEnhance.Color(rgb).enhance(saturation)
    rgb = ImageEnhance.Brightness(rgb).enhance(brightness)
    rgb = rgb.filter(ImageFilter.GaussianBlur(blur))
    return rgb.convert("RGBA")


def apply_layer_alpha(layer: Image.Image, alpha: float) -> Image.Image:
    """Scale an RGBA layer's alpha channel by `alpha` (0→1). Returns a
    new image; `alpha >= 1` returns the input unchanged."""
    if alpha >= 1.0:
        return layer
    if alpha <= 0.0:
        return Image.new("RGBA", layer.size, (0, 0, 0, 0))
    r, g, b, a = layer.convert("RGBA").split()
    a = a.point(lambda p: int(p * alpha))
    return Image.merge("RGBA", (r, g, b, a))
