"""v2.2 cold open — replaces the branded intro title card.

~3s: the #1 player's portrait full-bleed (duotone, slow zoom), with a
hook line that slams in within 0.3s. The #1 player's NAME is withheld
(face only, no caption) — the tease for the finale. The HoopsMatic
brand mark is ambient (small corner), never the opener.
"""

from __future__ import annotations

from moviepy import VideoClip
from PIL import Image, ImageDraw

from lib import anim, draw, duotone, parallax
from lib import format_specs as fs
from lib import style22
import render_helpers_v2 as helpers

COLD_OPEN_SECONDS = 3.0

HOOK_LINE_1 = "The NBA internet had one obsession this week"


def _frame(spec: fs.FormatSpec, portrait_bytes, top_n: int, t: float, dur: float):
    m = style22.metrics(spec)
    # Full-bleed duotone portrait, slight slow zoom (1.0 -> 1.06).
    zoom = 1.0 + 0.06 * min(1.0, t / dur)
    big = (int(spec.width * zoom), int(spec.height * zoom))
    portrait, _ = duotone.hero_portrait(portrait_bytes, big, anchor="top")
    canvas = Image.new("RGBA", (spec.width, spec.height), fs.hex_to_rgb(duotone.DUOTONE_SHADOW_V23) + (255,))
    canvas.alpha_composite(portrait, (-(big[0] - spec.width) // 2, -(big[1] - spec.height) // 2))

    # Scrim under the hook so type stays legible on any photo.
    scrim = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(scrim).rectangle(
        (0, int(spec.height * 0.55), spec.width, spec.height), fill=(10, 23, 48, 150)
    )
    canvas.alpha_composite(scrim, (0, 0))

    # Hook slams in within 0.3s: block fade + short slide-up. Fit to the
    # safe area so nothing clips on any format.
    a = anim.block_alpha(t, fade_in=0.3)
    off = int(anim.slide_offset(t, spec.height * 0.04, 0.3))
    margin = helpers.safe_margin(spec)
    max_w = spec.width - 2 * margin
    hook_fnt, lines = helpers.fit_wrapped(helpers.safe_text(HOOK_LINE_1), fs.FONT_BOLD, m["hook"], max_w, max_lines=3)
    lh = int(hook_fnt.size * 1.15)
    y0 = int(spec.height * 0.62) + off
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    for i, ln in enumerate(lines):
        draw.draw_text(layer, (margin, y0 + i * lh), ln, hook_fnt, fill=style22.DUOTONE_HIGHLIGHT, anchor="lt")
    sub_fnt, sub_txt = helpers.fit_text(helpers.safe_text(f"Top {top_n} by mentions · counted down"),
                                        fs.FONT_MONO_BOLD, m["hook_sub"], max_w)
    draw.draw_text(layer, (margin, y0 + len(lines) * lh + m["hook_sub"]), sub_txt, sub_fnt, fill=fs.ACCENT, anchor="lt")
    canvas.alpha_composite(parallax.apply_layer_alpha(layer, a), (0, 0))

    helpers.brand_mark(canvas, spec, m, on_dark=True)
    return draw.to_numpy(canvas)


def render_coldopen_v2(spec: fs.FormatSpec, portrait_bytes, top_n: int, *, duration: float | None = None) -> VideoClip:
    dur = duration if duration is not None else COLD_OPEN_SECONDS

    def make_frame(t):
        return _frame(spec, portrait_bytes, top_n, t, dur)

    return VideoClip(make_frame, duration=dur)
