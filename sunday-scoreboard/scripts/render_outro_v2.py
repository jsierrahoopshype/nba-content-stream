"""v2 outro — animated top-10 leaderboard.

Same chyron-style summary as v1 but with animated count-up bars: each
row's mention bar grows left-to-right (quart-out) behind the rank +
name + count, staggered so the leaderboard "fills in" rather than
appearing all at once. ~8s.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from moviepy import VideoClip
from PIL import Image, ImageDraw

from lib import draw, easing
from lib import format_specs as fs


def _draw_frame(
    spec: fs.FormatSpec,
    week_of: datetime,
    rows: list[tuple[int, str, int]],  # (rank, name, count)
    t: float,
    dur: float,
):
    canvas = draw.new_canvas(spec, fill=fs.BACKGROUND)

    label_fnt = draw.font(fs.FONT_MONO_BOLD, max(18, spec.meta_font_size + 4))
    head_fnt = draw.font(fs.FONT_BOLD, int(spec.title_font_size * 0.9))
    sub_fnt = draw.font(fs.FONT_MONO, spec.meta_font_size)

    draw.draw_text(canvas, (spec.width // 2, spec.pad + 10), "THE WEEK", label_fnt, fill=fs.ACCENT, anchor="mt")
    head_y = spec.pad + 10 + max(18, spec.meta_font_size + 4) + 16
    draw.draw_text(canvas, (spec.width // 2, head_y), "Top 10 by mentions", head_fnt, fill=fs.TEXT, anchor="mt")
    sub_y = head_y + int(spec.title_font_size * 0.9) + 12
    draw.draw_text(canvas, (spec.width // 2, sub_y), f"Week of {week_of.strftime('%B %d, %Y')}", sub_fnt, fill=fs.TEXT_SECONDARY, anchor="mt")

    rank_fnt = draw.font(fs.FONT_MONO_BOLD, spec.headline_font_size)
    name_fnt = draw.font(fs.FONT_BOLD, spec.headline_font_size)
    count_fnt = draw.font(fs.FONT_MONO_BOLD, spec.headline_font_size)

    header_h = sub_y + spec.meta_font_size + 60
    footer_h = spec.pad + 80
    row_zone = spec.height - header_h - footer_h
    n = len(rows)
    if n == 0:
        return draw.to_numpy(canvas)
    row_h = row_zone // n
    max_count = max((c for _, _, c in rows), default=1) or 1
    name_x = spec.pad + 120
    count_x = spec.width - spec.pad - 30
    bar_x0 = name_x
    bar_x1_max = count_x - 160

    for idx, (rank, name, count) in enumerate(rows):
        local_t = t - 0.4 - idx * 0.16
        if local_t <= 0:
            continue
        alpha = easing.fade(local_t, fade_in=0.5, fade_out=0.6, dur=max(0.6, dur - 0.5 - idx * 0.16))
        if alpha < 0.02:
            continue
        row_y = header_h + idx * row_h + row_h // 2
        sub = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(sub)

        # Animated mention bar (dim accent) growing to a count-proportional width.
        grow = easing.quart_out(min(1.0, local_t / 0.7))
        bar_w = int((bar_x1_max - bar_x0) * (count / max_count) * grow)
        if bar_w > 2:
            sd.rounded_rectangle(
                (bar_x0, row_y - row_h // 4, bar_x0 + bar_w, row_y + row_h // 4),
                radius=row_h // 6, fill=draw.rgb(fs.ACCENT_DIM, 255),
            )

        if idx > 0:
            sd.rectangle((spec.pad, header_h + idx * row_h, spec.width - spec.pad, header_h + idx * row_h + 1), fill=draw.rgb(fs.BORDER, 255))

        draw.draw_text(sub, (spec.pad + 40, row_y), f"{rank:>2}", rank_fnt, fill=fs.ACCENT, anchor="mm")
        name_fit = draw.fit_text_to_width(name, name_fnt, bar_x1_max - name_x)
        draw.draw_text(sub, (name_x, row_y), name_fit, name_fnt, fill=fs.TEXT, anchor="lm")
        # Count ticks up with the bar.
        shown = int(count * grow)
        draw.draw_text(sub, (count_x, row_y), str(shown), count_fnt, fill=fs.TEXT, anchor="rm")

        if alpha < 1.0:
            r, g, b, a = sub.split()
            a = a.point(lambda p: int(p * alpha))
            sub = Image.merge("RGBA", (r, g, b, a))
        canvas.alpha_composite(sub, (0, 0))

    foot_fnt = draw.font(fs.FONT_MONO, spec.meta_font_size)
    draw.draw_text(canvas, (spec.width // 2, spec.height - spec.pad - 10), "HoopsMatic · NBA Content Stream", foot_fnt, fill=fs.TEXT_SECONDARY, anchor="mb")
    return draw.to_numpy(canvas)


def render_outro_v2(
    spec: fs.FormatSpec,
    week_of: datetime,
    rows: Iterable[tuple[int, str, int]],
    *,
    duration: float | None = None,
) -> VideoClip:
    dur = duration if duration is not None else spec.outro_seconds
    rows_list = list(rows)

    def make_frame(t):
        return _draw_frame(spec, week_of, rows_list, t, dur)

    return VideoClip(make_frame, duration=dur)
