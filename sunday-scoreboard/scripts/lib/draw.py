"""Pillow drawing primitives shared by every renderer.

These are intentionally low-level: rounded rectangles, circular
crops, drop shadows, source pills, text alignment. Renderers compose
these into segment frames. No moviepy in here — the renderers wrap
PIL output in ImageClip themselves.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import format_specs as fs
from . import source_styling

logger = logging.getLogger("draw")


# ---------------------------------------------------------------------------
# Font cache — Pillow's ImageFont.truetype is cheap to re-load but tests
# create lots of frames; cache by (path, size).
# ---------------------------------------------------------------------------

_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    key = (str(path), int(size))
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    loaded = ImageFont.truetype(str(path), size)
    _FONT_CACHE[key] = loaded
    return loaded


# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------


def rgb(hex_str: str, alpha: int | None = None) -> tuple[int, ...]:
    r, g, b = fs.hex_to_rgb(hex_str)
    if alpha is None:
        return (r, g, b)
    return (r, g, b, alpha)


# ---------------------------------------------------------------------------
# Layers and canvases
# ---------------------------------------------------------------------------


def new_canvas(spec: fs.FormatSpec, fill: str = fs.BACKGROUND) -> Image.Image:
    """Fresh RGBA canvas at the spec's dimensions, filled with `fill`.
    Returned image is RGBA so subsequent paste-with-alpha operations
    composite correctly."""
    canvas = Image.new("RGBA", (spec.width, spec.height), rgb(fill, 255))
    return canvas


# ---------------------------------------------------------------------------
# Rounded rectangle with optional drop shadow.
# ---------------------------------------------------------------------------


def rounded_rect(
    canvas: Image.Image,
    xy: tuple[int, int, int, int],
    radius: int,
    fill: str | None = None,
    outline: str | None = None,
    outline_width: int = 1,
    shadow: bool = False,
    shadow_offset: tuple[int, int] = (0, 8),
    shadow_blur: int = 20,
) -> None:
    """Draw a rounded rect into `canvas`. Optional soft drop shadow
    composes through a separate blurred layer so the shadow is real
    Gaussian, not a stamped offset (which reads cheap)."""
    if shadow:
        sx, sy = shadow_offset
        # Shadow size needs padding so the Gaussian doesn't clip; we
        # render into a layer larger than the rect, blur, then paste.
        pad = shadow_blur * 2
        layer_w = xy[2] - xy[0] + pad * 2
        layer_h = xy[3] - xy[1] + pad * 2
        layer = Image.new("RGBA", (layer_w, layer_h), (0, 0, 0, 0))
        layer_draw = ImageDraw.Draw(layer)
        layer_draw.rounded_rectangle(
            (pad, pad, pad + (xy[2] - xy[0]), pad + (xy[3] - xy[1])),
            radius=radius,
            fill=fs.SHADOW,
        )
        layer = layer.filter(ImageFilter.GaussianBlur(shadow_blur))
        canvas.alpha_composite(
            layer, (xy[0] - pad + sx, xy[1] - pad + sy)
        )

    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        xy,
        radius=radius,
        fill=rgb(fill, 255) if fill else None,
        outline=rgb(outline, 255) if outline else None,
        width=outline_width,
    )


# ---------------------------------------------------------------------------
# Text — measure + draw with optional truncation.
# ---------------------------------------------------------------------------


def measure_text(text: str, fnt: ImageFont.FreeTypeFont) -> tuple[int, int]:
    """Pixel (width, height) of `text` at `fnt`. Uses textbbox so the
    height accounts for descenders accurately."""
    if not text:
        return (0, 0)
    bbox = fnt.getbbox(text)
    return (bbox[2] - bbox[0], bbox[3] - bbox[1])


def draw_text(
    canvas: Image.Image,
    xy: tuple[int, int],
    text: str,
    fnt: ImageFont.FreeTypeFont,
    fill: str = fs.TEXT,
    *,
    anchor: str = "la",
    alpha: int = 255,
) -> None:
    """Wrapper for ImageDraw.text. `anchor` follows Pillow's two-char
    convention (la=top-left, mm=center, etc.)."""
    if not text:
        return
    color = rgb(fill, alpha)
    ImageDraw.Draw(canvas).text(xy, text, font=fnt, fill=color, anchor=anchor)


def fit_text_to_width(
    text: str,
    fnt: ImageFont.FreeTypeFont,
    max_width: int,
    suffix: str = "…",
) -> str:
    """Truncate `text` with `suffix` if it overflows `max_width`."""
    if not text:
        return text
    w, _ = measure_text(text, fnt)
    if w <= max_width:
        return text
    suffix_w, _ = measure_text(suffix, fnt)
    target = max_width - suffix_w
    if target <= 0:
        return suffix
    # Binary-search the trim length so we only measure log(n) times.
    lo, hi = 0, len(text)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = text[:mid]
        w, _ = measure_text(candidate, fnt)
        if w <= target:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best.rstrip() + suffix


def wrap_text(
    text: str,
    fnt: ImageFont.FreeTypeFont,
    max_width: int,
    max_lines: int = 2,
) -> list[str]:
    """Word-wrap `text` into at most `max_lines` lines that each fit
    inside `max_width`. The final line is truncated with ellipsis if
    the text overflows the line budget."""
    words = text.split()
    lines: list[str] = []
    cur = ""
    for word in words:
        candidate = word if not cur else f"{cur} {word}"
        w, _ = measure_text(candidate, fnt)
        if w <= max_width:
            cur = candidate
        else:
            if cur:
                lines.append(cur)
            cur = word
            if len(lines) == max_lines:
                # Roll the remaining words into the last line and
                # ellipsize.
                tail = " ".join([cur] + words[words.index(word) + 1:])
                lines[-1] = fit_text_to_width(
                    lines[-1] + " " + tail, fnt, max_width
                )
                return lines
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = fit_text_to_width(lines[-1], fnt, max_width)
    return lines


# ---------------------------------------------------------------------------
# Portraits — circular crop with optional initials fallback.
# ---------------------------------------------------------------------------


def circle_image(
    src: bytes | None,
    diameter: int,
    *,
    fallback_initials: str = "",
    fallback_bg: str = fs.ACCENT_DIM,
    fallback_fg: str = fs.ACCENT,
    contain: bool = False,
) -> Image.Image:
    """Return an RGBA image of `diameter`x`diameter` containing the
    decoded `src` cropped to a circle, OR — if `src` is None or
    unreadable — a colored circle with the entity initials.

    `contain=True` switches the inner image from cover (object-fit:
    cover) to contain — used for transparent team logos so they
    don't get cropped to the face of the logo like a player headshot.
    """
    out = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    if src:
        try:
            inner = Image.open(io.BytesIO(src)).convert("RGBA")
        except Exception as exc:
            logger.warning("portrait decode failed: %s", exc)
            inner = None
    else:
        inner = None

    if inner is None:
        return _initials_circle(diameter, fallback_initials, fallback_bg, fallback_fg)

    if contain:
        # Fit inside the circle with padding so the logo isn't cropped.
        pad = max(1, diameter // 10)
        inner_box = diameter - pad * 2
        scale = min(inner_box / inner.width, inner_box / inner.height)
        new_w = max(1, int(inner.width * scale))
        new_h = max(1, int(inner.height * scale))
        inner = inner.resize((new_w, new_h), Image.LANCZOS)
        # Background tint so transparent logos read against the bg.
        bg = Image.new("RGBA", (diameter, diameter), rgb(fs.SURFACE, 255))
        offset = ((diameter - new_w) // 2, (diameter - new_h) // 2)
        bg.alpha_composite(inner, offset)
        inner = bg
    else:
        # Cover crop — scale so the shorter side fills the diameter,
        # center-crop the rest (top-anchored for player headshots so
        # the face stays centered).
        scale = max(diameter / inner.width, diameter / inner.height)
        new_w = max(diameter, int(inner.width * scale))
        new_h = max(diameter, int(inner.height * scale))
        inner = inner.resize((new_w, new_h), Image.LANCZOS)
        left = (new_w - diameter) // 2
        # Top-anchor: headshots have heads near the top. Skipping
        # straight-center crops the player's chin out on tall poses.
        top = 0
        inner = inner.crop((left, top, left + diameter, top + diameter))

    mask = Image.new("L", (diameter, diameter), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, diameter, diameter), fill=255)
    out.paste(inner, (0, 0), mask)
    return out


def _initials_circle(
    diameter: int, initials: str, bg: str, fg: str
) -> Image.Image:
    img = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((0, 0, diameter, diameter), fill=rgb(bg, 255))
    # Pick a font size that fills ~45% of the diameter.
    sz = max(12, int(diameter * 0.45))
    fnt = font(fs.FONT_BOLD, sz)
    draw_text(
        img,
        (diameter // 2, diameter // 2),
        initials or "?",
        fnt,
        fill=fg,
        anchor="mm",
    )
    return img


# ---------------------------------------------------------------------------
# Source-mix pills — a horizontal row of small rounded pills, one per
# source that contributed at least one item to the cluster.
# ---------------------------------------------------------------------------


def draw_source_pill(
    canvas: Image.Image,
    xy: tuple[int, int],
    source: str,
    count: int,
    fnt: ImageFont.FreeTypeFont,
    *,
    pad_x: int = 14,
    pad_y: int = 8,
) -> tuple[int, int]:
    """Draw a single source pill at `xy` (top-left). Returns the
    (width, height) of the rendered pill so the caller can advance."""
    style = source_styling.source_style(source)
    label = f"{style.label} · {count}"
    text_w, text_h = measure_text(label, fnt)
    w = text_w + pad_x * 2
    h = text_h + pad_y * 2
    rounded_rect(
        canvas,
        (xy[0], xy[1], xy[0] + w, xy[1] + h),
        radius=h // 2,
        fill=style.color_dim,
    )
    # The label uses the source's strong color so the pill reads at a
    # glance even when several stack on a row.
    draw_text(
        canvas,
        (xy[0] + w // 2, xy[1] + h // 2),
        label,
        fnt,
        fill=style.color,
        anchor="mm",
    )
    return (w, h)


def draw_source_mix(
    canvas: Image.Image,
    xy: tuple[int, int],
    mix: dict[str, int],
    fnt: ImageFont.FreeTypeFont,
    *,
    gap: int = 12,
) -> int:
    """Draw a row of source-pills. Returns the total row width.

    Renders sources in a stable order so consecutive beats with the
    same source profile look identical (not jittery as dict order
    changes)."""
    order = ("bluesky", "google-news", "reddit", "substack", "youtube")
    x = xy[0]
    y = xy[1]
    total_w = 0
    for src in order:
        if src not in mix or mix[src] <= 0:
            continue
        w, _ = draw_source_pill(canvas, (x, y), src, mix[src], fnt)
        x += w + gap
        total_w += w + gap
    return max(0, total_w - gap)


# ---------------------------------------------------------------------------
# PIL frame → numpy/RGB for moviepy ImageClip ingestion.
# ---------------------------------------------------------------------------


def to_rgb(img: Image.Image) -> Image.Image:
    """Flatten RGBA → RGB on a brand-background plate. moviepy's
    ImageClip is happiest with RGB arrays; encoding RGBA to MP4 loses
    the alpha anyway."""
    if img.mode == "RGB":
        return img
    flat = Image.new("RGB", img.size, rgb(fs.BACKGROUND, 255))
    flat.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
    return flat


def to_numpy(img: Image.Image):
    """Image → numpy.ndarray (uint8, RGB). Lazy-import numpy so this
    module stays light when only measuring text."""
    import numpy as np

    return np.asarray(to_rgb(img))
