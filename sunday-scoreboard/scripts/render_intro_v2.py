"""v2 intro card — more dynamic than v1's static fade.

"SUNDAY" slides up over a parallaxing gradient plate, "SCOREBOARD"
reveals letter-by-letter, and a "THE SPOTLIGHT EDIT" tag fades in
under an animated accent rule. Same brand tokens as the rest of the
pipeline; ~6s.
"""

from __future__ import annotations

from datetime import datetime

from moviepy import VideoClip
from PIL import Image

from lib import draw, easing, parallax
from lib import format_specs as fs


def _render_frame(spec: fs.FormatSpec, week_of: datetime, t: float, dur: float):
    canvas = draw.new_canvas(spec, fill=fs.BACKGROUND)

    # Parallaxing gradient band across the middle third.
    band_h = spec.height // 3
    grad = parallax.vertical_gradient((int(spec.width * 1.2), band_h))
    offset = parallax.parallax_offset(min(1.0, t / dur), distance=-120.0, speed=0.4)
    band = parallax.place_scaled(grad, (spec.width, band_h), 1.0, (offset, 0))
    canvas.alpha_composite(parallax.apply_layer_alpha(band, 0.16), (0, (spec.height - band_h) // 2))

    cx = spec.width // 2
    cy = spec.height // 2

    # "SUNDAY" — slides up into place over the first 0.8s.
    label_fnt = draw.font(fs.FONT_MONO_BOLD, max(20, spec.meta_font_size + 8))
    slide = easing.quart_out(min(1.0, t / 0.8))
    label_y = int(cy - 120 + (1 - slide) * 60)
    draw.draw_text(
        canvas, (cx, label_y), "SUNDAY", label_fnt,
        fill=fs.ACCENT, anchor="mm", alpha=int(255 * slide),
    )

    # "SCOREBOARD" — letter-by-letter reveal starting at 0.4s.
    head_fnt = draw.font(fs.FONT_BOLD, int(spec.title_font_size * 1.5))
    text = "SCOREBOARD"
    widths = [draw.measure_text(ch, head_fnt)[0] for ch in text]
    total_w = sum(widths)
    x = cx - total_w // 2
    for i, ch in enumerate(text):
        a = parallax.letter_stagger_alpha(i, t - 0.4, per_letter=0.06, fade_in=0.3)
        if a > 0.01:
            draw.draw_text(canvas, (x, cy), ch, head_fnt, fill=fs.TEXT, anchor="lm", alpha=int(255 * a))
        x += widths[i]

    # Animated accent rule under the title.
    grow = easing.quart_out(min(1.0, t / 1.4))
    half = int(min(320, spec.width // 3) / 2 * grow)
    if half > 1:
        from PIL import ImageDraw as _ID
        y = cy + int(spec.title_font_size * 1.0)
        _ID.Draw(canvas).rectangle((cx - half, y, cx + half, y + 5), fill=draw.rgb(fs.ACCENT, 255))

    # Subtitle + tag fade in after 1.2s.
    sub_alpha = easing.fade(t - 1.2, fade_in=0.6, fade_out=0.6, dur=max(1.0, dur - 1.2))
    if sub_alpha > 0.02:
        tag_fnt = draw.font(fs.FONT_MONO, spec.meta_font_size + 2)
        sub_fnt = draw.font(fs.FONT_REGULAR, spec.headline_font_size)
        y = cy + int(spec.title_font_size * 1.0) + 50
        draw.draw_text(canvas, (cx, y), "THE SPOTLIGHT EDIT", tag_fnt, fill=fs.ACCENT, anchor="mt", alpha=int(255 * sub_alpha))
        draw.draw_text(
            canvas, (cx, y + spec.meta_font_size + 24),
            f"Week of {week_of.strftime('%B %d, %Y')}", sub_fnt,
            fill=fs.TEXT_SECONDARY, anchor="mt", alpha=int(255 * sub_alpha),
        )

    foot_fnt = draw.font(fs.FONT_MONO, spec.meta_font_size)
    draw.draw_text(
        canvas, (cx, spec.height - spec.pad - 10),
        "Top 10 players · ranked by mention volume · HoopsMatic",
        foot_fnt, fill=fs.TEXT_SECONDARY, anchor="mb",
    )

    # Global fade in/out envelope.
    alpha = easing.fade(t, fade_in=0.8, fade_out=0.6, dur=dur)
    if alpha < 1.0:
        bg = Image.new("RGB", canvas.size, draw.rgb(fs.BACKGROUND, 255))
        flat = Image.blend(bg, draw.to_rgb(canvas), alpha)
        return draw.to_numpy(flat)
    return draw.to_numpy(canvas)


def render_intro_v2(spec: fs.FormatSpec, week_of: datetime, *, duration: float | None = None) -> VideoClip:
    dur = duration if duration is not None else spec.intro_seconds

    def make_frame(t):
        return _render_frame(spec, week_of, t, dur)

    return VideoClip(make_frame, duration=dur)
