"""Shared render helpers for the v2.2 social edit.

Small pieces used across the cold open, beats, outro, and CTA so the
look stays consistent: the persistent HoopsMatic brand mark (ambient on
every phase) and the diagonal accent strip used on light-background
phases.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from lib import draw, parallax, quote_filter
from lib import format_specs as fs
from lib import style22

# Minimum safe margin each side (≥64px at 1080w), scaled by frame width.
def safe_margin(spec: fs.FormatSpec) -> int:
    return max(spec.pad, int(round(64 * spec.width / 1080)))


def safe_width(spec: fs.FormatSpec) -> int:
    """Usable text width inside the safe margins."""
    return spec.width - 2 * safe_margin(spec)


def safe_text(text: str) -> str:
    """Strip any non-renderable codepoint (emoji/symbols/arrows) and
    collapse whitespace before it reaches a draw call — guards every
    phase against tofu while preserving accented Latin names."""
    return quote_filter.clean_text(text or "")


def fit_font(text, font_path, max_size, max_width, *, min_size=14, step=2):
    """Largest font (≤max_size, ≥min_size) at which `text` fits
    `max_width`."""
    size = max_size
    while size > min_size:
        f = draw.font(font_path, size)
        if draw.measure_text(text, f)[0] <= max_width:
            return f
        size -= step
    return draw.font(font_path, min_size)


def fit_text(text, font_path, max_size, max_width, *, min_size=14):
    """Return `(font, text)` that fits `max_width`: shrink the font, and
    if still too wide at `min_size`, ellipsize as the floor."""
    f = fit_font(text, font_path, max_size, max_width, min_size=min_size)
    if draw.measure_text(text, f)[0] <= max_width:
        return f, text
    return f, draw.fit_text_to_width(text, f, max_width)


def fit_wrapped(text, font_path, max_size, max_width, *, max_lines=2, min_size=14, step=4):
    """Wrap `text` to ≤`max_lines`, shrinking the font until even the
    longest word fits `max_width` (so long names like ANTETOKOUNMPO
    never clip mid-word). Ellipsis floor at `min_size`."""
    words = text.split() or [text]
    size = max_size
    while size >= min_size:
        f = draw.font(font_path, size)
        if max((draw.measure_text(w, f)[0] for w in words), default=0) <= max_width:
            lines = draw.wrap_text(text, f, max_width, max_lines)
            if all(draw.measure_text(ln, f)[0] <= max_width for ln in lines):
                return f, lines
        size -= step
    f = draw.font(font_path, min_size)
    lines = draw.wrap_text(text, f, max_width, max_lines)
    return f, [draw.fit_text_to_width(ln, f, max_width) for ln in lines]


def draw_triangle(canvas, center, size, color, *, direction="right") -> None:
    """Draw a small filled triangle marker (renderable replacement for
    glyphs DM Sans lacks, e.g. the → arrow)."""
    cx, cy = center
    s = size
    if direction == "right":
        pts = [(cx - s, cy - s), (cx + s, cy), (cx - s, cy + s)]
    else:  # down
        pts = [(cx - s, cy - s), (cx + s, cy - s), (cx, cy + s)]
    ImageDraw.Draw(canvas).polygon(pts, fill=draw.rgb(color, 255))


def brand_mark(canvas: Image.Image, spec: fs.FormatSpec, m: dict, *, on_dark: bool = False) -> None:
    """Persistent HoopsMatic wordmark, bottom-right. `on_dark` flips it
    to near-white for full-bleed (duotone) phases."""
    fnt = draw.font(fs.FONT_BOLD, m["brand"])
    color = style22.DUOTONE_HIGHLIGHT if on_dark else fs.ACCENT
    draw.draw_text(
        canvas,
        (spec.width - spec.pad // 2, spec.height - spec.pad // 2),
        "HoopsMatic", fnt, fill=color, anchor="rb",
        alpha=230,
    )


def diagonal_accent_strip(spec: fs.FormatSpec, *, alpha: float = 0.16) -> Image.Image:
    """A subtle diagonal accent gradient strip for light-bg phases."""
    W, H = spec.width, spec.height
    strip = parallax.vertical_gradient((int(H * 1.5), int(W * 0.24)), fs.ACCENT, fs.ACCENT_DIM)
    strip = strip.rotate(-20, expand=True)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    layer.alpha_composite(strip, (int(W * 0.55), int(-H * 0.25)))
    return parallax.apply_layer_alpha(layer, alpha)


def centered_pill_row(canvas: Image.Image, spec: fs.FormatSpec, mix: dict, m: dict, y: int, *, alpha: float = 1.0) -> None:
    """Draw a horizontally-centered source-mix pill row at vertical `y`."""
    fnt = draw.font(fs.FONT_MONO_BOLD, m["pill"])
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    row_w = draw.draw_source_mix(layer, (0, 0), mix, fnt, gap=12)
    shifted = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shifted.alpha_composite(layer, ((spec.width - row_w) // 2, y))
    canvas.alpha_composite(parallax.apply_layer_alpha(shifted, alpha), (0, 0))
