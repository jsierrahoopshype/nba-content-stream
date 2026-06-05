"""v2 "Spotlight Edit" per-beat renderer — v2.2 social-first rebuild.

Fast, full-bleed, retention-first. Three phases, hard-cut between beats:

  Phase 1  Hero        full-bleed DUOTONE portrait (≥80% height), big
                       uppercase name over the lower third, animated
                       mention counter, big rank glyph, source pills.
  Phase 2  Quote card  white rounded card on the light page (accent
                       left border, soft shadow): larger reporter
                       avatar + name + handle, quote slammed in as a
                       block, engagement ticker counting up.
  Phase 3  Spike       area-filled 7-day sparkline (no sparse dead air),
                       peak callout, source pills + context row.

Pace (v2.2): hero 2.5s → quote 3.5s → spark 2.0s = 8.0s/beat. The #1
payoff beat gets +2.0s and the most dramatic entrance. Hard cuts
between beats (the next rank glyph slams in). The HoopsMatic brand mark
is ambient on every phase.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from moviepy import VideoClip
from PIL import Image, ImageDraw

from lib import anim, draw, duotone, easing, parallax, quote_filter, sparkline
from lib import format_specs as fs
from lib import style22
from lib.canonical_lookup import EntityInfo
from lib.engagement_score import Engagement
from lib.reporter_lookup import Reporter
import render_helpers_v2 as helpers

logger = logging.getLogger("render_beat_v2")

# Phase durations (seconds). Base beat = 8.0s; the #1 payoff adds +2.0s.
HERO_SECONDS = 2.5
QUOTE_SECONDS = 3.5
SPARK_SECONDS = 2.0
PAYOFF_EXTRA = {"hero": 1.0, "quote": 0.5, "spark": 0.5}

MAX_QUOTE_WORDS = 12


def beat_phase_plan(is_payoff: bool = False) -> list[tuple[str, float]]:
    """Ordered (phase_name, duration) for one beat. The payoff beat is
    longer for dramatic effect. Used by the renderer and by the cut-
    timeline sidecar so both agree on the boundaries."""
    extra = PAYOFF_EXTRA if is_payoff else {}
    return [
        ("hero", HERO_SECONDS + extra.get("hero", 0.0)),
        ("quote", QUOTE_SECONDS + extra.get("quote", 0.0)),
        ("spark", SPARK_SECONDS + extra.get("spark", 0.0)),
    ]


def beat_seconds(is_payoff: bool = False) -> float:
    return sum(d for _, d in beat_phase_plan(is_payoff))


@dataclass
class BeatRenderDataV2:
    """Everything the renderer needs for one beat, prefetched so the
    per-frame callback never touches the network."""

    rank: int
    entity: EntityInfo
    mention_count: int
    source_mix: dict[str, int]

    portrait_bytes: bytes | None = None

    quote_text: str = ""
    quote_reporter: Reporter | None = None
    quote_avatar_bytes: bytes | None = None
    engagement: Engagement | None = None

    weekly_counts: list[int] = field(default_factory=list)
    weekly_total: int = 0
    day_labels: list[str] = field(default_factory=list)
    spike_source_mix: dict[str, int] = field(default_factory=dict)
    peak_callout: str | None = None
    context_line: str = ""

    is_payoff: bool = False


# ---------------------------------------------------------------------------
# Phase 1 — full-bleed duotone hero
# ---------------------------------------------------------------------------


def _draw_hero(spec: fs.FormatSpec, beat: BeatRenderDataV2, t: float, dur: float) -> Image.Image:
    m = style22.metrics(spec)
    prog = t / dur

    # Full-bleed duotone portrait. Payoff gets a stronger push-in.
    zoom_span = 0.12 if beat.is_payoff else 0.05
    zoom = (1.0 + zoom_span) - zoom_span * easing.quart_out(min(1.0, prog))
    bw, bh = int(spec.width * zoom), int(spec.height * zoom)
    portrait, had_img = duotone.hero_portrait(beat.portrait_bytes, (bw, bh), anchor="top")
    canvas = Image.new("RGBA", (spec.width, spec.height), fs.hex_to_rgb(style22.DUOTONE_SHADOW) + (255,))
    canvas.alpha_composite(portrait, (-(bw - spec.width) // 2, -(bh - spec.height) // 2))

    if not had_img:  # 404 fallback — big initials on the brand panel
        init_fnt = draw.font(fs.FONT_BOLD, int(spec.height * 0.32))
        draw.draw_text(canvas, (spec.width // 2, spec.height // 2 - int(spec.height * 0.08)),
                       beat.entity.initials, init_fnt, fill=style22.DUOTONE_HIGHLIGHT, anchor="mm", alpha=235)

    # Bottom scrim so the name reads on any photo.
    scrim = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(scrim).rectangle((0, int(spec.height * 0.5), spec.width, spec.height), fill=(10, 23, 48, 165))
    canvas.alpha_composite(scrim, (0, 0))

    # Rank glyph — slams in from the left, legible by 0.3s.
    rank_fnt = draw.font(fs.FONT_MONO_BOLD, m["rank"])
    rx = int(spec.pad + anim.slide_offset(t, -spec.width * 0.3, 0.3))
    draw.draw_text(canvas, (rx, spec.pad), f"#{beat.rank}", rank_fnt, fill=fs.ACCENT, alpha=240, anchor="lt")

    # Animated mention counter (0 → N over 0.8s), upper-right block.
    count_val = anim.count_up(t, beat.mention_count, 0.8)
    count_fnt = draw.font(fs.FONT_MONO_BOLD, m["count"])
    label_fnt = draw.font(fs.FONT_MONO, m["count_label"])
    cx = spec.width - spec.pad
    cy = int(spec.height * 0.30)
    draw.draw_text(canvas, (cx, cy), f"{count_val:,}", count_fnt, fill=style22.DUOTONE_HIGHLIGHT, anchor="rt")
    draw.draw_text(canvas, (cx, cy + int(m["count"] * 0.95)), "MENTIONS", label_fnt, fill=fs.ACCENT, anchor="rt")

    # Player name — huge uppercase over the lower third, slams in block.
    name_fnt = draw.font(fs.FONT_BOLD, m["name"])
    max_w = spec.width - spec.pad * 2
    lines = draw.wrap_text(beat.entity.name.upper(), name_fnt, max_w, max_lines=2)
    lh = int(m["name"] * 1.02)
    a = anim.block_alpha(t, fade_in=0.3)
    off = int(anim.slide_offset(t, spec.height * 0.05, 0.3))
    nlayer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    name_top = int(spec.height * 0.66) + off
    for i, ln in enumerate(lines):
        draw.draw_text(nlayer, (spec.pad, name_top + i * lh), ln, name_fnt, fill=style22.DUOTONE_HIGHLIGHT, anchor="lt")
    sub = beat.entity.team_context.upper() if (beat.entity.kind == "player" and beat.entity.team_context) else "PLAYER"
    draw.draw_text(nlayer, (spec.pad, name_top + len(lines) * lh + 6), sub, draw.font(fs.FONT_MONO, m["name_sub"]), fill=fs.ACCENT, anchor="lt")
    canvas.alpha_composite(parallax.apply_layer_alpha(nlayer, a), (0, 0))

    # Source pills bottom edge.
    helpers.centered_pill_row(canvas, spec, beat.source_mix, m, spec.height - spec.pad - m["pill"] - 16,
                              alpha=anim.block_alpha(t, fade_in=0.4))
    helpers.brand_mark(canvas, spec, m, on_dark=True)
    return canvas


# ---------------------------------------------------------------------------
# Phase 2 — quote card
# ---------------------------------------------------------------------------


def _limit_words(text: str, max_words: int) -> str:
    words = text.split()
    return text if len(words) <= max_words else " ".join(words[:max_words]).rstrip(",;:") + "…"


def _draw_quote(spec: fs.FormatSpec, beat: BeatRenderDataV2, t: float, dur: float) -> Image.Image:
    m = style22.metrics(spec)
    canvas = draw.new_canvas(spec, fill=fs.BACKGROUND)
    canvas.alpha_composite(helpers.diagonal_accent_strip(spec), (0, 0))

    rep = beat.quote_reporter

    if not beat.quote_text:
        # Clean no-quote state (still filled — no dead air): a card with
        # the spotlight context rather than airing nothing.
        msg_fnt = draw.font(fs.FONT_BOLD, m["reporter_name"])
        card = (spec.pad, int(spec.height * 0.4), spec.width - spec.pad, int(spec.height * 0.6))
        draw.rounded_rect(canvas, card, radius=int(24 * m["k"]), fill=fs.SURFACE, outline=fs.BORDER, shadow=True, shadow_blur=int(28 * m["k"]))
        draw.draw_text(canvas, (spec.width // 2, (card[1] + card[3]) // 2), "No standout reporter quote this week.", msg_fnt, fill=fs.TEXT_SECONDARY, anchor="mm")
        helpers.brand_mark(canvas, spec, m)
        return canvas

    # Card geometry — large so the frame is never mostly empty.
    cx0, cx1 = spec.pad, spec.width - spec.pad
    cy0 = int(spec.height * 0.20)
    cy1 = int(spec.height * 0.78)
    radius = int(24 * m["k"])
    draw.rounded_rect(canvas, (cx0, cy0, cx1, cy1), radius=radius, fill=fs.SURFACE, outline=fs.BORDER,
                      shadow=True, shadow_offset=(0, int(10 * m["k"])), shadow_blur=int(30 * m["k"]))
    # Thick accent left border.
    border_w = max(6, int(8 * m["k"]))
    ImageDraw.Draw(canvas).rounded_rectangle((cx0, cy0, cx0 + border_w * 2, cy1), radius=radius, fill=draw.rgb(fs.ACCENT, 255))
    ImageDraw.Draw(canvas).rectangle((cx0 + border_w, cy0, cx0 + border_w * 2, cy1), fill=draw.rgb(fs.ACCENT, 255))

    pad_in = int(48 * m["k"])
    inner_x = cx0 + border_w + pad_in
    inner_w = cx1 - inner_x - pad_in

    # Soft accent header band inside the card behind the reporter row —
    # gives the card structure and coverage from frame 0.
    avatar_d = int(96 * m["k"])
    band_b = cy0 + pad_in + avatar_d + int(20 * m["k"])
    ImageDraw.Draw(canvas).rounded_rectangle(
        (cx0 + border_w, cy0, cx1, band_b), radius=radius, fill=draw.rgb(fs.ACCENT_DIM, 255)
    )
    ImageDraw.Draw(canvas).rectangle((cx0 + border_w, band_b - radius, cx1, band_b), fill=draw.rgb(fs.ACCENT_DIM, 255))

    # Reporter row at card top (larger avatar).
    if rep:
        avatar = draw.circle_image(beat.quote_avatar_bytes, avatar_d,
                                   fallback_initials=(rep.display_name or rep.handle)[:1].upper(),
                                   fallback_bg=fs.ACCENT_DIM, fallback_fg=fs.ACCENT)
        canvas.alpha_composite(avatar, (inner_x, cy0 + pad_in))
        tx = inner_x + avatar_d + int(24 * m["k"])
        draw.draw_text(canvas, (tx, cy0 + pad_in + avatar_d // 2 - 6), rep.display_name, draw.font(fs.FONT_BOLD, m["reporter_name"]), fill=fs.TEXT, anchor="lb")
        draw.draw_text(canvas, (tx, cy0 + pad_in + avatar_d // 2 + 6), f"@{rep.handle}", draw.font(fs.FONT_MONO, m["handle"]), fill=fs.TEXT_SECONDARY, anchor="lt")

    # Quote text — block slide-up + fade over 0.35s (not line-by-line).
    quote_fnt = draw.font(fs.FONT_REGULAR, m["quote"])
    text = _limit_words(beat.quote_text, MAX_QUOTE_WORDS)
    lines = quote_filter.prepare_quote_lines(text, lambda s: draw.measure_text(s, quote_fnt)[0], inner_w, max_lines=4)
    leading = int(m["quote"] * 1.35)
    qy = cy0 + pad_in + avatar_d + int(40 * m["k"])
    a = anim.block_alpha(t, fade_in=0.35)
    off = int(anim.slide_offset(t, spec.height * 0.03, 0.35))
    qlayer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    for i, ln in enumerate(lines):
        draw.draw_text(qlayer, (inner_x, qy + i * leading + off), ln, quote_fnt, fill=fs.TEXT, anchor="lt")
    canvas.alpha_composite(parallax.apply_layer_alpha(qlayer, a), (0, 0))

    # Engagement ticker bottom-right of the card, counts up over 0.8s.
    if beat.engagement is not None:
        total = anim.count_up(t, beat.engagement.total, 0.8)
        big = draw.font(fs.FONT_MONO_BOLD, m["ticker"])
        small = draw.font(fs.FONT_MONO, m["ticker_label"])
        draw.draw_text(canvas, (cx1 - pad_in, cy1 - pad_in), f"{total:,}", big, fill=fs.ACCENT, anchor="rb")
        e = beat.engagement
        draw.draw_text(canvas, (cx1 - pad_in, cy1 - pad_in + 6),
                       f"{e.likes:,} likes · {e.reposts:,} reposts · {e.replies:,} replies", small,
                       fill=fs.TEXT_SECONDARY, anchor="rt")

    helpers.brand_mark(canvas, spec, m)
    return canvas


# ---------------------------------------------------------------------------
# Phase 3 — area-filled spike sparkline
# ---------------------------------------------------------------------------


def _draw_sparkline(spec: fs.FormatSpec, beat: BeatRenderDataV2, t: float, dur: float) -> Image.Image:
    m = style22.metrics(spec)
    canvas = draw.new_canvas(spec, fill=fs.BACKGROUND)

    # Full-frame stat panel (present from frame 0 → no phase-start dead
    # air). Soft brand blue reads as an intentional "stat card", not the
    # empty app background.
    draw.rounded_rect(canvas, (spec.pad, spec.pad, spec.width - spec.pad, spec.height - spec.pad),
                      radius=int(28 * m["k"]), fill=fs.ACCENT_DIM)

    # Header + big weekly total (fills the upper third).
    draw.draw_text(canvas, (spec.width // 2, spec.pad), f"#{beat.rank}  {beat.entity.name.upper()}",
                   draw.font(fs.FONT_BOLD, m["spark_head"]), fill=fs.TEXT, anchor="mt")
    top_y = spec.pad + int(m["spark_head"] * 1.4)
    draw.draw_text(canvas, (spec.width // 2, top_y), str(beat.weekly_total),
                   draw.font(fs.FONT_MONO_BOLD, m["spark_total"]), fill=fs.ACCENT, anchor="mt")
    draw.draw_text(canvas, (spec.width // 2, top_y + int(m["spark_total"] * 0.95)), "MENTIONS THIS WEEK",
                   draw.font(fs.FONT_MONO, m["spark_total_label"]), fill=fs.TEXT_SECONDARY, anchor="mt")

    # Chart box — middle band.
    box_top = top_y + int(m["spark_total"] * 1.25)
    box_h = int(spec.height * 0.30)
    box = (spec.pad + int(40 * m["k"]), box_top, spec.width - spec.pad - int(40 * m["k"]), box_top + box_h)

    # Animated AREA FILL under the line (kills the sparse dead-air look),
    # then the line + labels + peak callout on top.
    spark_t = min(1.0, t / 1.0)
    pts = sparkline.sparkline_points(beat.weekly_counts, box)
    if pts:
        full, partial = sparkline.draw_progress(len(pts), spark_t)
        vis = list(pts[:full])
        if 0 < full < len(pts) and partial > 0:
            a0, b0 = pts[full - 1], pts[full]
            vis.append((int(a0[0] + (b0[0] - a0[0]) * partial), int(a0[1] + (b0[1] - a0[1]) * partial)))
        if len(vis) >= 2:
            poly = vis + [(vis[-1][0], box[3]), (vis[0][0], box[3])]
            fill_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            ImageDraw.Draw(fill_layer).polygon(poly, fill=draw.rgb(fs.ACCENT, 70))
            canvas.alpha_composite(fill_layer, (0, 0))
    sparkline.draw_sparkline(canvas, box, beat.weekly_counts, spark_t,
                             labels=beat.day_labels, peak_callout=beat.peak_callout)

    # Source pills + context row below (fills the lower third).
    a = anim.block_alpha(t - 0.4, fade_in=0.35)
    helpers.centered_pill_row(canvas, spec, beat.spike_source_mix, m, box[3] + int(70 * m["k"]), alpha=a)
    if beat.context_line:
        draw.draw_text(canvas, (spec.width // 2, box[3] + int(70 * m["k"]) + m["pill"] + int(40 * m["k"])),
                       beat.context_line, draw.font(fs.FONT_MONO, m["context"]),
                       fill=fs.TEXT_SECONDARY, anchor="mt", alpha=int(255 * a))
    helpers.brand_mark(canvas, spec, m)
    return canvas


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_beat_v2(spec: fs.FormatSpec, beat: BeatRenderDataV2) -> VideoClip:
    """Return one VideoClip covering all three spotlight phases, paced
    per the v2.2 budget (longer for the #1 payoff beat)."""
    plan = beat_phase_plan(beat.is_payoff)
    durs = {name: d for name, d in plan}
    t_hero = durs["hero"]
    t_quote = durs["quote"]
    t_spark = durs["spark"]
    total = t_hero + t_quote + t_spark

    def make_frame(t):
        if t < t_hero:
            img = _draw_hero(spec, beat, t, t_hero)
        elif t < t_hero + t_quote:
            img = _draw_quote(spec, beat, t - t_hero, t_quote)
        else:
            img = _draw_sparkline(spec, beat, t - t_hero - t_quote, t_spark)
        return draw.to_numpy(img)

    return VideoClip(make_frame, duration=total)
