"""v2.2 CTA end-card — the closing screenshot frame (~2.5s).

"FULL DATA ▾ HoopsMatic.com" (the arrow is a DRAWN triangle — DM Sans
has no → glyph, which rendered as tofu in v2.2), "NBA Content Stream ·
updated every 15 minutes" beneath, brand mark above. An accent panel
behind the URL keeps the frame full. All text is fit to the safe area.
"""

from __future__ import annotations

from moviepy import VideoClip
from PIL import Image

from lib import anim, draw, parallax
from lib import format_specs as fs
from lib import style22
import render_helpers_v2 as helpers

CTA_SECONDS = 2.5

# Pure-ASCII strings only — no glyphs outside the DM Sans cmap.
HEADLINE_LABEL = "FULL DATA"
HEADLINE_URL = "HoopsMatic.com"
SUBLINE = "NBA Content Stream · updated every 15 minutes"


def _frame(spec, t, dur):
    m = style22.metrics(spec)
    k = m["k"]
    margin = helpers.safe_margin(spec)
    maxw = spec.width - 2 * margin
    canvas = draw.new_canvas(spec, fill=fs.BACKGROUND)
    canvas.alpha_composite(helpers.diagonal_accent_strip(spec, alpha=0.14), (0, 0))

    cx, cy = spec.width // 2, spec.height // 2
    a = anim.block_alpha(t, fade_in=0.3)

    # Accent panel behind the URL (fills the frame, brand block).
    panel_h = int(spec.height * 0.30)
    panel = (margin, cy - panel_h // 2, spec.width - margin, cy + panel_h // 2)
    draw.rounded_rect(canvas, panel, radius=int(28 * k), fill=fs.ACCENT,
                      shadow=True, shadow_offset=(0, int(10 * k)), shadow_blur=int(30 * k))
    panel_w = panel[2] - panel[0] - int(80 * k)

    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    # "FULL DATA" label.
    label_fnt, label_txt = helpers.fit_text(HEADLINE_LABEL, fs.FONT_MONO_BOLD, m["cta_sub"], panel_w)
    draw.draw_text(layer, (cx, panel[1] + int(36 * k)), label_txt, label_fnt, fill=style22.DUOTONE_HIGHLIGHT, anchor="mt")
    # Drawn down-triangle marker (replaces the → glyph).
    tri_y = panel[1] + int(36 * k) + label_fnt.size + int(22 * k)
    helpers.draw_triangle(layer, (cx, tri_y), int(16 * k), style22.DUOTONE_HIGHLIGHT, direction="down")
    # The URL, big, fit to the panel width.
    url_fnt, url_txt = helpers.fit_text(HEADLINE_URL, fs.FONT_BOLD, m["cta"], panel_w)
    draw.draw_text(layer, (cx, tri_y + int(20 * k)), url_txt, url_fnt, fill=style22.DUOTONE_HIGHLIGHT, anchor="mt")
    # Subtitle below the panel (fit both edges).
    sub_fnt, sub_txt = helpers.fit_text(helpers.safe_text(SUBLINE), fs.FONT_MONO, m["cta_sub"], maxw)
    draw.draw_text(layer, (cx, panel[3] + int(40 * k)), sub_txt, sub_fnt, fill=fs.TEXT_SECONDARY, anchor="mt")
    canvas.alpha_composite(parallax.apply_layer_alpha(layer, a), (0, 0))

    # Brand mark centered above the panel.
    brand_fnt, brand_txt = helpers.fit_text("HoopsMatic", fs.FONT_BOLD, m["brand"] * 2, maxw)
    draw.draw_text(canvas, (cx, panel[1] - int(40 * k)), brand_txt, brand_fnt, fill=fs.ACCENT, anchor="mb", alpha=int(255 * a))
    return draw.to_numpy(canvas)


def render_cta_v2(spec, *, duration: float | None = None) -> VideoClip:
    dur = duration if duration is not None else CTA_SECONDS

    def make_frame(t):
        return _frame(spec, t, dur)

    return VideoClip(make_frame, duration=dur)
