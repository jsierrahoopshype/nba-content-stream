"""v2.2 outro — animated top-N leaderboard.

Header copy uses the ACTUAL beat count (never hardcoded "10"). Rows in
rank order (1 at top is fine for the table), each with a count-up bar.
Fast (≈3s), brand mark ambient. The closing CTA is a separate end-card
(render_cta_v2).
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from moviepy import VideoClip
from PIL import Image, ImageDraw

from lib import anim, draw, easing
from lib import format_specs as fs
from lib import style22
import render_helpers_v2 as helpers

OUTRO_SECONDS = 3.0


def outro_headline(n: int) -> str:
    """Leaderboard header copy — N from the actual beat count, never
    hardcoded "10"."""
    return f"Top {n} by mentions"


def _draw_frame(spec, week_of, rows, t, dur):
    m = style22.metrics(spec)
    n = len(rows)
    canvas = draw.new_canvas(spec, fill=fs.BACKGROUND)
    canvas.alpha_composite(helpers.diagonal_accent_strip(spec, alpha=0.12), (0, 0))
    # Full-frame leaderboard panel (present from frame 0 → no dead air).
    draw.rounded_rect(canvas, (spec.pad, spec.pad, spec.width - spec.pad, spec.height - spec.pad),
                      radius=int(28 * m["k"]), fill=fs.ACCENT_DIM)

    draw.draw_text(canvas, (spec.width // 2, spec.pad), "THE WEEK",
                   draw.font(fs.FONT_MONO_BOLD, m["outro_label"]), fill=fs.ACCENT, anchor="mt")
    head_y = spec.pad + int(m["outro_label"] * 1.5)
    # N from the real beat count — never hardcoded.
    draw.draw_text(canvas, (spec.width // 2, head_y), outro_headline(n),
                   draw.font(fs.FONT_BOLD, m["outro_head"]), fill=fs.TEXT, anchor="mt")
    sub_y = head_y + int(m["outro_head"] * 1.2)
    draw.draw_text(canvas, (spec.width // 2, sub_y), f"Week of {week_of.strftime('%B %d, %Y')}",
                   draw.font(fs.FONT_MONO, m["context"]), fill=fs.TEXT_SECONDARY, anchor="mt")

    row_fnt = draw.font(fs.FONT_MONO_BOLD, m["row"])
    name_fnt = draw.font(fs.FONT_BOLD, m["row"])
    header_h = sub_y + int(m["context"] * 2.2)
    footer_h = spec.pad + int(m["brand"] * 2)
    zone = spec.height - header_h - footer_h
    if n == 0:
        return draw.to_numpy(canvas)
    row_h = zone // n
    max_count = max((c for _, _, c in rows), default=1) or 1
    name_x = spec.pad + int(110 * m["k"])
    count_x = spec.width - spec.pad
    bar_x1 = count_x - int(150 * m["k"])

    for idx, (rank, name, count) in enumerate(rows):
        local = t - 0.25 - idx * 0.12
        if local <= 0:
            continue
        grow = easing.quart_out(min(1.0, local / 0.6))
        row_y = header_h + idx * row_h + row_h // 2
        sub = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(sub)
        bar_w = int((bar_x1 - name_x) * (count / max_count) * grow)
        if bar_w > 2:
            sd.rounded_rectangle((name_x, row_y - row_h // 4, name_x + bar_w, row_y + row_h // 4),
                                 radius=row_h // 6, fill=draw.rgb(fs.ACCENT, 90))
        if idx > 0:
            sd.rectangle((spec.pad, header_h + idx * row_h, spec.width - spec.pad, header_h + idx * row_h + 1), fill=draw.rgb(fs.BORDER, 255))
        draw.draw_text(sub, (spec.pad + int(36 * m["k"]), row_y), f"{rank:>2}", row_fnt, fill=fs.ACCENT, anchor="mm")
        draw.draw_text(sub, (name_x, row_y), draw.fit_text_to_width(name, name_fnt, bar_x1 - name_x), name_fnt, fill=fs.TEXT, anchor="lm")
        draw.draw_text(sub, (count_x, row_y), str(int(count * grow)), row_fnt, fill=fs.TEXT, anchor="rm")
        canvas.alpha_composite(sub, (0, 0))

    helpers.brand_mark(canvas, spec, m)
    return draw.to_numpy(canvas)


def render_outro_v2(spec, week_of: datetime, rows: Iterable[tuple[int, str, int]], *, duration: float | None = None) -> VideoClip:
    dur = duration if duration is not None else OUTRO_SECONDS
    rows_list = list(rows)

    def make_frame(t):
        return _draw_frame(spec, week_of, rows_list, t, dur)

    return VideoClip(make_frame, duration=dur)
