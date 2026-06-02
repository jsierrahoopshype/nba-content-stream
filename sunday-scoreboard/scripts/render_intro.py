"""Branded intro card for the recap.

Layout:
  - "SUNDAY" (mono uppercase, smaller, accent color)
  - "SCOREBOARD" (DM Sans Bold, huge)
  - subtitle "Week of YYYY-MM-DD · NBA news ranked by mention volume"
  - HoopsMatic byline at bottom

Duration ~6s with a quart-out fade-in over the first 1.2s.
"""

from __future__ import annotations

from datetime import datetime

from moviepy import VideoClip

from lib import draw, easing
from lib import format_specs as fs


def _render_frame(
    spec: fs.FormatSpec, week_of: datetime, t: float, dur: float
):
    canvas = draw.new_canvas(spec, fill=fs.BACKGROUND)

    # The whole card fades in over the first 1.2s; alpha is applied
    # uniformly via a post-step (per-pixel alpha blend with the
    # background plate).
    alpha = easing.fade(t, fade_in=1.2, fade_out=0.6, dur=dur)

    label = "SUNDAY"
    headline = "SCOREBOARD"
    sub = f"Week of {week_of.strftime('%B %d, %Y')}"
    foot = "NBA news ranked by mention volume · HoopsMatic"

    label_fnt = draw.font(fs.FONT_MONO_BOLD, max(18, spec.meta_font_size + 4))
    head_fnt = draw.font(fs.FONT_BOLD, int(spec.title_font_size * 1.6))
    sub_fnt = draw.font(fs.FONT_REGULAR, spec.headline_font_size)
    foot_fnt = draw.font(fs.FONT_MONO, spec.meta_font_size)

    cx = spec.width // 2
    cy = spec.height // 2

    # Stack: label · headline · sub. Centered around mid-height.
    label_w, label_h = draw.measure_text(label, label_fnt)
    head_w, head_h = draw.measure_text(headline, head_fnt)
    sub_w, sub_h = draw.measure_text(sub, sub_fnt)
    block_h = label_h + head_h + sub_h + 80
    top = cy - block_h // 2

    # Label
    draw.draw_text(
        canvas, (cx, top + label_h // 2), label,
        label_fnt, fill=fs.ACCENT, anchor="mm",
    )
    # Headline
    draw.draw_text(
        canvas, (cx, top + label_h + 30 + head_h // 2), headline,
        head_fnt, fill=fs.TEXT, anchor="mm",
    )
    # Sub
    draw.draw_text(
        canvas,
        (cx, top + label_h + 30 + head_h + 30 + sub_h // 2),
        sub, sub_fnt, fill=fs.TEXT_SECONDARY, anchor="mm",
    )
    # Foot
    draw.draw_text(
        canvas, (cx, spec.height - spec.pad - 10), foot,
        foot_fnt, fill=fs.TEXT_SECONDARY, anchor="mb",
    )

    # Accent divider above the foot, animated in from the center.
    div_w_max = min(280, spec.width // 3)
    grow = easing.quart_out(min(1.0, t / 1.4))
    half_w = int((div_w_max / 2) * grow)
    if half_w > 1:
        from PIL import ImageDraw as _ID
        d = _ID.Draw(canvas)
        y = spec.height - spec.pad - 10 - sub_h - 24
        d.rectangle(
            (cx - half_w, y, cx + half_w, y + 4),
            fill=draw.rgb(fs.ACCENT, 255),
        )

    # Apply global alpha via a flat overlay → blend with bg.
    if alpha < 1.0:
        from PIL import Image
        bg = Image.new("RGB", canvas.size, draw.rgb(fs.BACKGROUND, 255))
        flat = draw.to_rgb(canvas)
        flat = Image.blend(bg, flat, alpha)
        return draw.to_numpy(flat)
    return draw.to_numpy(canvas)


def render_intro(spec: fs.FormatSpec, week_of: datetime, *, duration: float | None = None) -> VideoClip:
    """Return a moviepy VideoClip for the intro card."""
    dur = duration if duration is not None else spec.intro_seconds

    def make_frame(t):
        return _render_frame(spec, week_of, t, dur)

    return VideoClip(make_frame, duration=dur)
