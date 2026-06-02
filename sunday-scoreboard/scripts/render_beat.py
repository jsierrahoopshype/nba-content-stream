"""Per-beat segment renderer.

A beat segment is ~13s and has four phases:
  1. Title card     (3s)  — rank, portrait, name, count, source mix
  2. Headlines roll (6s)  — three source headlines slide in/out
  3. Reporters     (3s)   — three reporter avatars + handles
  4. Transition    (1s)   — slide out, next beat slides up

Layout differs per format key (side-by-side / stacked / vertical
stack). Each phase is its own VideoClip; we concatenate them with
fade crossover so the result is a single playable beat clip.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from moviepy import CompositeVideoClip, VideoClip
from PIL import Image

from lib import archive_client, draw, easing
from lib import format_specs as fs
from lib.canonical_lookup import EntityInfo
from lib.reporter_lookup import Reporter

logger = logging.getLogger("render_beat")


# ---------------------------------------------------------------------------
# Beat — the data shape the renderer consumes. Built by render_video.py
# from cluster_beats.Beat + lookups.
# ---------------------------------------------------------------------------


@dataclass
class BeatRenderData:
    rank: int
    entity: EntityInfo
    mention_count: int
    top_headlines: list[dict]   # {"title", "source", "published_at"} dicts
    top_reporters: list[Reporter]
    source_mix: dict[str, int]
    time_span_hours: float
    # Pre-fetched binary blobs so the per-frame renderer doesn't block
    # on network. None if upstream 404'd; the renderer falls back to
    # initials/letters.
    portrait_bytes: bytes | None = None
    reporter_avatar_bytes: list[bytes | None] = None


# ---------------------------------------------------------------------------
# Title-card phase
# ---------------------------------------------------------------------------


def _draw_title_card(
    spec: fs.FormatSpec,
    beat: BeatRenderData,
    t: float,
    dur: float,
) -> Image.Image:
    canvas = draw.new_canvas(spec, fill=fs.BACKGROUND)

    # Big rank glyph in the corner — anchor varies per format.
    rank_fnt = draw.font(fs.FONT_MONO_BOLD, spec.rank_font_size)
    rank_text = f"#{beat.rank}"
    if spec.layout == "side-by-side":
        # Horizontal: rank top-left over the portrait area.
        rx = spec.pad + 20
        ry = spec.pad + 10
        draw.draw_text(
            canvas, (rx, ry), rank_text, rank_fnt,
            fill=fs.ACCENT, alpha=42, anchor="lt",
        )
    elif spec.layout == "stacked":
        # Square: top-left, smaller weight (less visual real estate).
        rx = spec.pad
        ry = spec.pad
        draw.draw_text(
            canvas, (rx, ry), rank_text,
            draw.font(fs.FONT_MONO_BOLD, int(spec.rank_font_size * 0.7)),
            fill=fs.ACCENT, alpha=48, anchor="lt",
        )
    else:  # vertical-stack
        rx = spec.pad
        ry = spec.pad
        draw.draw_text(
            canvas, (rx, ry), rank_text, rank_fnt,
            fill=fs.ACCENT, alpha=46, anchor="lt",
        )

    # Portrait (circular, with initials fallback).
    portrait = draw.circle_image(
        beat.portrait_bytes,
        spec.portrait_size,
        fallback_initials=beat.entity.initials,
        fallback_bg=fs.ACCENT_DIM,
        fallback_fg=fs.ACCENT,
        contain=(beat.entity.kind == "team"),
    )

    # Per-format placement
    if spec.layout == "side-by-side":
        # Portrait left third, content right two-thirds.
        portrait_x = spec.pad + 100
        portrait_y = (spec.height - spec.portrait_size) // 2
        content_x = portrait_x + spec.portrait_size + 80
        content_max_w = spec.width - content_x - spec.pad
    elif spec.layout == "stacked":
        # Portrait top center, content below.
        portrait_x = (spec.width - spec.portrait_size) // 2
        portrait_y = spec.pad + 90
        content_x = spec.pad
        content_max_w = spec.width - spec.pad * 2
    else:  # vertical-stack
        portrait_x = (spec.width - spec.portrait_size) // 2
        portrait_y = spec.height // 4
        content_x = spec.pad
        content_max_w = spec.width - spec.pad * 2

    # Entrance: portrait scales up from 92% with quart-out over the
    # first 0.6s.
    pct = easing.quart_out(min(1.0, t / 0.6))
    portrait_scaled = portrait
    if pct < 1.0:
        target_d = int(spec.portrait_size * (0.92 + 0.08 * pct))
        portrait_scaled = portrait.resize((target_d, target_d), Image.LANCZOS)
        dx = portrait_x + (spec.portrait_size - target_d) // 2
        dy = portrait_y + (spec.portrait_size - target_d) // 2
        canvas.alpha_composite(portrait_scaled, (dx, dy))
    else:
        canvas.alpha_composite(portrait_scaled, (portrait_x, portrait_y))

    # Name + secondary (team context for players / "TEAM" tag for teams)
    name_fnt = draw.font(fs.FONT_BOLD, spec.title_font_size)
    sub_fnt = draw.font(fs.FONT_MONO, spec.meta_font_size)

    name = beat.entity.name
    if spec.layout == "side-by-side":
        nx, ny = content_x, portrait_y + 40
        name_anchor = "lt"
        sub_anchor = "lt"
    else:
        nx = spec.width // 2
        ny = portrait_y + spec.portrait_size + 40
        name_anchor = "mt"
        sub_anchor = "mt"

    # Truncate name to fit available width.
    name_fit = draw.fit_text_to_width(name, name_fnt, content_max_w)
    draw.draw_text(canvas, (nx, ny), name_fit, name_fnt, fill=fs.TEXT, anchor=name_anchor)
    _, name_h = draw.measure_text(name_fit, name_fnt)
    sub_y = ny + name_h + 16

    if beat.entity.kind == "player" and beat.entity.team_context:
        sub_text = beat.entity.team_context.upper()
    elif beat.entity.kind == "team":
        sub_text = "TEAM"
    else:
        sub_text = "PLAYER"
    draw.draw_text(canvas, (nx, sub_y), sub_text, sub_fnt, fill=fs.TEXT_SECONDARY, anchor=sub_anchor)

    # Mention count: large mono with subtle pulse + "mentions" label.
    pulse_scale = easing.pulse(t)
    count_fnt = draw.font(
        fs.FONT_MONO_BOLD,
        int(spec.title_font_size * 1.6 * pulse_scale),
    )
    count_label_fnt = draw.font(fs.FONT_MONO, spec.meta_font_size)
    count_text = str(beat.mention_count)
    if spec.layout == "side-by-side":
        # Bottom-right of the content area.
        cx = spec.width - spec.pad - 20
        cy = portrait_y + spec.portrait_size - 40
        count_anchor = "rb"
        label_anchor = "rb"
        label_y = cy + 32
    else:
        # Below the name/sub stack.
        _, sub_h = draw.measure_text(sub_text, sub_fnt)
        cx = spec.width // 2
        cy = sub_y + sub_h + 80
        count_anchor = "mt"
        label_anchor = "mt"
        label_y = cy + int(spec.title_font_size * 1.6) + 8

    draw.draw_text(canvas, (cx, cy), count_text, count_fnt, fill=fs.ACCENT, anchor=count_anchor)
    draw.draw_text(canvas, (cx, label_y), "MENTIONS", count_label_fnt, fill=fs.TEXT_SECONDARY, anchor=label_anchor)

    # Source-mix pills at the bottom — fade in after 0.8s.
    mix_alpha = easing.fade(t - 0.8, fade_in=0.5, fade_out=0.3, dur=max(0.5, dur - 0.8))
    if mix_alpha > 0.02:
        pill_fnt = draw.font(fs.FONT_MONO_BOLD, max(16, spec.meta_font_size - 2))
        # Render to a sub-layer so we can apply mix_alpha cleanly.
        sub = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        row_w = draw.draw_source_mix(
            sub, (0, 0), beat.source_mix, pill_fnt, gap=14
        )
        mx = (spec.width - row_w) // 2
        my = spec.height - spec.pad - 60
        scratch = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        scratch.alpha_composite(sub, (mx, my))
        # Apply alpha to the whole scratch layer.
        if mix_alpha < 1.0:
            r, g, b, a = scratch.split()
            a = a.point(lambda p: int(p * mix_alpha))
            scratch = Image.merge("RGBA", (r, g, b, a))
        canvas.alpha_composite(scratch, (0, 0))

    return canvas


# ---------------------------------------------------------------------------
# Headlines-roll phase
# ---------------------------------------------------------------------------


def _draw_headlines(
    spec: fs.FormatSpec,
    beat: BeatRenderData,
    t: float,
    dur: float,
) -> Image.Image:
    canvas = draw.new_canvas(spec, fill=fs.BACKGROUND)

    # Faded portrait backdrop (~10% opacity) reinforces the entity
    # without competing with the headlines for attention.
    if beat.portrait_bytes:
        from PIL import ImageFilter
        bg_d = max(spec.width, spec.height)
        portrait_bg = draw.circle_image(
            beat.portrait_bytes, bg_d,
            fallback_initials=beat.entity.initials,
            contain=False,
        )
        portrait_bg = portrait_bg.filter(ImageFilter.GaussianBlur(20))
        # Place + dim.
        backdrop = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        bx = (spec.width - bg_d) // 2
        by = (spec.height - bg_d) // 2
        backdrop.alpha_composite(portrait_bg, (bx, by))
        r, g, b, a = backdrop.split()
        a = a.point(lambda p: int(p * 0.10))
        backdrop = Image.merge("RGBA", (r, g, b, a))
        canvas.alpha_composite(backdrop, (0, 0))

    # Small rank + name across the top so the viewer keeps context.
    rank_fnt = draw.font(fs.FONT_MONO_BOLD, max(18, spec.meta_font_size + 4))
    name_fnt = draw.font(fs.FONT_BOLD, int(spec.title_font_size * 0.55))
    rank_label = f"#{beat.rank}  {beat.entity.name}"
    draw.draw_text(
        canvas, (spec.width // 2, spec.pad + 20),
        rank_label, name_fnt, fill=fs.TEXT, anchor="mt",
    )

    # The 3 headlines split the headlines window evenly. Each one has
    # a fade-in/fade-out within its slot, plus a small vertical slide
    # from below (24px → 0).
    slot = dur / max(1, len(beat.top_headlines) or 1)
    headline_fnt = draw.font(fs.FONT_BOLD, spec.headline_font_size)
    meta_fnt = draw.font(fs.FONT_MONO, spec.meta_font_size)

    # Card geometry — one card slot, the 3 headlines cycle through it.
    card_max_w = spec.width - spec.pad * 2
    card_w = min(card_max_w, 1200 if spec.layout == "side-by-side" else card_max_w)
    card_x = (spec.width - card_w) // 2
    card_h = (spec.headline_font_size * 3 + 100)
    card_y = (spec.height - card_h) // 2

    for idx, headline in enumerate(beat.top_headlines):
        local_t = t - idx * slot
        if local_t < 0 or local_t >= slot:
            continue
        alpha = easing.fade(local_t, fade_in=0.35, fade_out=0.35, dur=slot)
        if alpha < 0.02:
            continue
        slide_in = (1 - easing.quart_out(min(1.0, local_t / 0.5))) * 28

        sub = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw.rounded_rect(
            sub,
            (card_x, card_y, card_x + card_w, card_y + card_h),
            radius=18,
            fill=fs.SURFACE,
            outline=fs.BORDER,
            shadow=True,
            shadow_offset=(0, 8),
            shadow_blur=22,
        )
        # Source pill — left edge of the card.
        src = headline.get("source", "")
        pill_fnt = draw.font(fs.FONT_MONO_BOLD, max(14, spec.meta_font_size - 4))
        src_w, src_h = draw.draw_source_pill(
            sub,
            (card_x + 28, card_y + 28),
            src, headline.get("rank_in_mix", 1),
            pill_fnt,
        )
        # When-ago label, right of the pill.
        when = headline.get("when_ago", "")
        if when:
            draw.draw_text(
                sub,
                (card_x + 28 + src_w + 12, card_y + 28 + src_h // 2),
                when, meta_fnt, fill=fs.TEXT_SECONDARY, anchor="lm",
            )
        # Headline text — wrapped to 2 lines.
        title = headline.get("title", "")
        wrapped = draw.wrap_text(
            title, headline_fnt, card_w - 56, max_lines=2
        )
        ty = card_y + 28 + src_h + 22
        for line in wrapped:
            draw.draw_text(sub, (card_x + 28, ty), line, headline_fnt, fill=fs.TEXT, anchor="lt")
            ty += spec.headline_font_size + 12

        # Apply slide + alpha to the sub-layer.
        if slide_in > 0:
            shifted = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            shifted.alpha_composite(sub, (0, int(slide_in)))
            sub = shifted
        if alpha < 1.0:
            r, g, b, a = sub.split()
            a = a.point(lambda p: int(p * alpha))
            sub = Image.merge("RGBA", (r, g, b, a))
        canvas.alpha_composite(sub, (0, 0))

    return canvas


# ---------------------------------------------------------------------------
# Reporters phase
# ---------------------------------------------------------------------------


def _draw_reporters(
    spec: fs.FormatSpec,
    beat: BeatRenderData,
    t: float,
    dur: float,
) -> Image.Image:
    canvas = draw.new_canvas(spec, fill=fs.BACKGROUND)

    # Title.
    title_fnt = draw.font(fs.FONT_BOLD, int(spec.title_font_size * 0.7))
    sub_fnt = draw.font(fs.FONT_MONO, spec.meta_font_size)
    draw.draw_text(
        canvas,
        (spec.width // 2, spec.pad + 60),
        "Reporters covering",
        title_fnt, fill=fs.TEXT, anchor="mt",
    )
    rank_label = f"#{beat.rank}  {beat.entity.name}"
    draw.draw_text(
        canvas,
        (spec.width // 2, spec.pad + 60 + int(spec.title_font_size * 0.7) + 16),
        rank_label, sub_fnt, fill=fs.TEXT_SECONDARY, anchor="mt",
    )

    if not beat.top_reporters:
        # No Bluesky reporters in this cluster — show a hint pill so
        # the segment still reads as intentional.
        msg_fnt = draw.font(fs.FONT_REGULAR, spec.headline_font_size)
        draw.draw_text(
            canvas, (spec.width // 2, spec.height // 2),
            "No Bluesky reporters in this cluster.",
            msg_fnt, fill=fs.TEXT_SECONDARY, anchor="mm",
        )
        return canvas

    avatar_d = spec.portrait_size // 2
    gap = 60
    n = len(beat.top_reporters)
    row_w = n * avatar_d + (n - 1) * gap
    row_x = (spec.width - row_w) // 2
    row_y = (spec.height - avatar_d) // 2

    handle_fnt = draw.font(fs.FONT_MONO_BOLD, max(20, spec.meta_font_size))
    name_fnt = draw.font(fs.FONT_REGULAR, spec.meta_font_size - 2)

    for idx, rep in enumerate(beat.top_reporters):
        # Staggered entrance: 0.25s offset per reporter.
        local_t = t - idx * 0.25
        alpha = easing.fade(local_t, fade_in=0.45, fade_out=0.3, dur=max(0.6, dur - 0.5))
        if alpha < 0.02:
            continue
        scale = 0.85 + 0.15 * easing.quart_out(min(1.0, local_t / 0.5))
        d = max(8, int(avatar_d * scale))

        avatar_bytes = (
            beat.reporter_avatar_bytes[idx]
            if beat.reporter_avatar_bytes and idx < len(beat.reporter_avatar_bytes)
            else None
        )
        initials = (rep.display_name or rep.handle)[:1].upper()
        avatar_img = draw.circle_image(
            avatar_bytes, d,
            fallback_initials=initials,
            fallback_bg=fs.ACCENT_DIM, fallback_fg=fs.ACCENT,
        )
        ax = row_x + idx * (avatar_d + gap) + (avatar_d - d) // 2
        ay = row_y + (avatar_d - d) // 2

        sub = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        sub.alpha_composite(avatar_img, (ax, ay))
        # Handle (mono) below avatar; display name (small, regular)
        # below that.
        handle_txt = f"@{rep.handle}"
        handle_fit = draw.fit_text_to_width(handle_txt, handle_fnt, avatar_d + gap)
        draw.draw_text(
            sub,
            (row_x + idx * (avatar_d + gap) + avatar_d // 2, row_y + avatar_d + 18),
            handle_fit, handle_fnt, fill=fs.TEXT, anchor="mt",
        )
        name_fit = draw.fit_text_to_width(rep.display_name, name_fnt, avatar_d + gap)
        if name_fit and name_fit != handle_txt:
            draw.draw_text(
                sub,
                (row_x + idx * (avatar_d + gap) + avatar_d // 2, row_y + avatar_d + 18 + max(20, spec.meta_font_size + 6)),
                name_fit, name_fnt, fill=fs.TEXT_SECONDARY, anchor="mt",
            )
        if alpha < 1.0:
            r, g, b, a = sub.split()
            a = a.point(lambda p: int(p * alpha))
            sub = Image.merge("RGBA", (r, g, b, a))
        canvas.alpha_composite(sub, (0, 0))

    return canvas


# ---------------------------------------------------------------------------
# Transition (1s slide up)
# ---------------------------------------------------------------------------


def _draw_transition(
    spec: fs.FormatSpec,
    beat: BeatRenderData,
    t: float,
    dur: float,
) -> Image.Image:
    # Render the reporters-phase final frame, then slide it up while
    # an accent bar grows across the bottom — feels like a chyron
    # advance.
    base = _draw_reporters(spec, beat, dur, dur)  # final reporters state
    shift = int(easing.sin_in_out(min(1.0, t / dur)) * spec.height * 0.25)
    canvas = draw.new_canvas(spec, fill=fs.BACKGROUND)
    canvas.alpha_composite(base, (0, -shift))

    # Sliding accent bar at the bottom.
    bar_progress = easing.quart_out(min(1.0, t / dur))
    bar_w = int(spec.width * bar_progress)
    if bar_w > 0:
        from PIL import ImageDraw as _ID
        d = _ID.Draw(canvas)
        bar_y = spec.height - 80
        d.rectangle(
            (0, bar_y, bar_w, bar_y + 6), fill=draw.rgb(fs.ACCENT, 255),
        )

    return canvas


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_beat(spec: fs.FormatSpec, beat: BeatRenderData) -> VideoClip:
    """Return a single VideoClip covering all four phases of one beat."""

    t_title = spec.title_seconds
    t_head = spec.headlines_seconds
    t_rep = spec.reporters_seconds
    t_trans = spec.transition_seconds
    total = t_title + t_head + t_rep + t_trans

    def make_frame(t):
        if t < t_title:
            img = _draw_title_card(spec, beat, t, t_title)
        elif t < t_title + t_head:
            img = _draw_headlines(spec, beat, t - t_title, t_head)
        elif t < t_title + t_head + t_rep:
            img = _draw_reporters(spec, beat, t - t_title - t_head, t_rep)
        else:
            img = _draw_transition(
                spec, beat, t - t_title - t_head - t_rep, t_trans
            )
        return draw.to_numpy(img)

    return VideoClip(make_frame, duration=total)
