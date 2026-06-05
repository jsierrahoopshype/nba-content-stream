"""v2.2 CTA end-card — the closing screenshot frame (~2.5s).

"Full data → HoopsMatic.com" big, "NBA Content Stream · updated every
15 minutes" beneath, brand mark center. Clean and bold: an accent panel
behind the headline keeps the frame from being empty (dead-air rule)
and gives the screenshot a branded block.
"""

from __future__ import annotations

from moviepy import VideoClip
from PIL import Image

from lib import anim, draw, parallax
from lib import format_specs as fs
from lib import style22
import render_helpers_v2 as helpers

CTA_SECONDS = 2.5

HEADLINE = "Full data → HoopsMatic.com"
SUBLINE = "NBA Content Stream · updated every 15 minutes"


def _frame(spec, t, dur):
    m = style22.metrics(spec)
    canvas = draw.new_canvas(spec, fill=fs.BACKGROUND)
    canvas.alpha_composite(helpers.diagonal_accent_strip(spec, alpha=0.14), (0, 0))

    cx, cy = spec.width // 2, spec.height // 2
    a = anim.block_alpha(t, fade_in=0.3)

    # Accent panel behind the headline (fills the frame, brand block).
    panel_h = int(spec.height * 0.30)
    panel = (spec.pad, cy - panel_h // 2, spec.width - spec.pad, cy + panel_h // 2)
    draw.rounded_rect(canvas, panel, radius=int(28 * m["k"]), fill=fs.ACCENT,
                      shadow=True, shadow_offset=(0, int(10 * m["k"])), shadow_blur=int(30 * m["k"]))

    head_fnt = draw.font(fs.FONT_BOLD, m["cta"])
    sub_fnt = draw.font(fs.FONT_MONO_BOLD, m["cta_sub"])
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    for i, ln in enumerate(draw.wrap_text(HEADLINE, head_fnt, spec.width - spec.pad * 3, max_lines=2)):
        draw.draw_text(layer, (cx, cy - int(m["cta"] * 0.3) + i * int(m["cta"] * 1.05)), ln, head_fnt,
                       fill=style22.DUOTONE_HIGHLIGHT, anchor="mm")
    draw.draw_text(layer, (cx, panel[3] + int(40 * m["k"])), SUBLINE, sub_fnt, fill=fs.TEXT_SECONDARY, anchor="mt")
    canvas.alpha_composite(parallax.apply_layer_alpha(layer, a), (0, 0))

    # Brand mark centered above the panel for the end-card.
    draw.draw_text(canvas, (cx, panel[1] - int(40 * m["k"])), "HoopsMatic",
                   draw.font(fs.FONT_BOLD, m["brand"] * 2), fill=fs.ACCENT, anchor="mb", alpha=int(255 * a))
    return draw.to_numpy(canvas)


def render_cta_v2(spec, *, duration: float | None = None) -> VideoClip:
    dur = duration if duration is not None else CTA_SECONDS

    def make_frame(t):
        return _frame(spec, t, dur)

    return VideoClip(make_frame, duration=dur)
