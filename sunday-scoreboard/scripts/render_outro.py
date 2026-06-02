"""Top-10 leaderboard outro card.

Shows the full ranked list at a glance — viewer sees who they just
heard about. Each row: rank, name, mention count. No portraits in
the outro (we already showed them per-beat); the goal is a clean
chyron-style summary.

Duration ~8s with a quart-out fade-in of the rows, staggered.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from moviepy import VideoClip
from PIL import Image

from lib import draw, easing
from lib import format_specs as fs


def _draw_outro_frame(
    spec: fs.FormatSpec,
    week_of: datetime,
    rows: list[tuple[int, str, int]],  # (rank, name, count)
    t: float,
    dur: float,
):
    canvas = draw.new_canvas(spec, fill=fs.BACKGROUND)

    # Header
    label_fnt = draw.font(fs.FONT_MONO_BOLD, max(18, spec.meta_font_size + 4))
    head_fnt = draw.font(fs.FONT_BOLD, int(spec.title_font_size * 0.9))
    sub_fnt = draw.font(fs.FONT_MONO, spec.meta_font_size)

    draw.draw_text(
        canvas, (spec.width // 2, spec.pad + 10), "THE WEEK",
        label_fnt, fill=fs.ACCENT, anchor="mt",
    )
    draw.draw_text(
        canvas, (spec.width // 2, spec.pad + 10 + max(18, spec.meta_font_size + 4) + 16),
        "Top 10 by mentions", head_fnt, fill=fs.TEXT, anchor="mt",
    )
    sub_text = f"Week of {week_of.strftime('%B %d, %Y')}"
    draw.draw_text(
        canvas, (spec.width // 2, spec.pad + 10 + max(18, spec.meta_font_size + 4) + 16 + int(spec.title_font_size * 0.9) + 12),
        sub_text, sub_fnt, fill=fs.TEXT_SECONDARY, anchor="mt",
    )

    # Row layout — fits all 10 rows in a column with consistent
    # spacing. Each row has rank (mono) · name (DM Sans bold) · count
    # (mono).
    rank_fnt = draw.font(fs.FONT_MONO_BOLD, spec.headline_font_size)
    name_fnt = draw.font(fs.FONT_BOLD, spec.headline_font_size)
    count_fnt = draw.font(fs.FONT_MONO_BOLD, spec.headline_font_size)

    header_h = spec.pad + 10 + max(18, spec.meta_font_size + 4) + 16 + int(spec.title_font_size * 0.9) + 12 + spec.meta_font_size + 60
    footer_h = spec.pad + 80
    row_zone = spec.height - header_h - footer_h
    n = len(rows)
    if n == 0:
        return draw.to_numpy(canvas)
    row_h = row_zone // n
    name_max_w = spec.width - spec.pad * 2 - 240  # rank + count columns

    for idx, (rank, name, count) in enumerate(rows):
        # Stagger entrance — 0.18s offset each.
        local_t = t - 0.4 - idx * 0.18
        if local_t <= 0:
            continue
        alpha = easing.fade(local_t, fade_in=0.5, fade_out=0.6, dur=max(0.6, dur - 0.5 - idx * 0.18))
        if alpha < 0.02:
            continue
        slide_in = (1 - easing.quart_out(min(1.0, local_t / 0.6))) * 24

        row_y = header_h + idx * row_h + row_h // 2
        sub = Image.new("RGBA", canvas.size, (0, 0, 0, 0))

        # Divider above each row (except first).
        if idx > 0:
            from PIL import ImageDraw as _ID
            d = _ID.Draw(sub)
            div_y = header_h + idx * row_h
            d.rectangle(
                (spec.pad, div_y, spec.width - spec.pad, div_y + 1),
                fill=draw.rgb(fs.BORDER, 255),
            )

        # Rank.
        draw.draw_text(
            sub, (spec.pad + 40, row_y), f"{rank:>2}",
            rank_fnt, fill=fs.ACCENT, anchor="mm",
        )
        # Name (truncated).
        name_fit = draw.fit_text_to_width(name, name_fnt, name_max_w)
        draw.draw_text(
            sub, (spec.pad + 120, row_y), name_fit,
            name_fnt, fill=fs.TEXT, anchor="lm",
        )
        # Count.
        draw.draw_text(
            sub, (spec.width - spec.pad - 30, row_y), str(count),
            count_fnt, fill=fs.TEXT, anchor="rm",
        )

        if slide_in > 0:
            shifted = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            shifted.alpha_composite(sub, (0, int(slide_in)))
            sub = shifted
        if alpha < 1.0:
            r, g, b, a = sub.split()
            a = a.point(lambda p: int(p * alpha))
            sub = Image.merge("RGBA", (r, g, b, a))
        canvas.alpha_composite(sub, (0, 0))

    # Footer byline.
    foot_fnt = draw.font(fs.FONT_MONO, spec.meta_font_size)
    draw.draw_text(
        canvas, (spec.width // 2, spec.height - spec.pad - 10),
        "HoopsMatic · NBA Content Stream", foot_fnt,
        fill=fs.TEXT_SECONDARY, anchor="mb",
    )

    return draw.to_numpy(canvas)


def render_outro(
    spec: fs.FormatSpec,
    week_of: datetime,
    rows: Iterable[tuple[int, str, int]],
    *,
    duration: float | None = None,
) -> VideoClip:
    """Build the outro VideoClip. `rows` is the same ordering used in
    the beat segments — the leaderboard is consistent end-to-end."""
    dur = duration if duration is not None else spec.outro_seconds
    rows_list = list(rows)

    def make_frame(t):
        return _draw_outro_frame(spec, week_of, rows_list, t, dur)

    return VideoClip(make_frame, duration=dur)
