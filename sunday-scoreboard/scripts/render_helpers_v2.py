"""Shared render helpers for the v2.2 social edit.

Small pieces used across the cold open, beats, outro, and CTA so the
look stays consistent: the persistent HoopsMatic brand mark (ambient on
every phase) and the diagonal accent strip used on light-background
phases.
"""

from __future__ import annotations

from PIL import Image

from lib import draw, parallax
from lib import format_specs as fs
from lib import style22


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
