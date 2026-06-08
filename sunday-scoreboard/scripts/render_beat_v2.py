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

# v2.3: counter settles fast then holds (no "0 MENTIONS" caught mid-roll).
COUNTER_SETTLE = 0.4
# Consistent lower-third band top (fraction of frame height) so name /
# team / counter baselines don't jump between beats.
LOWER_THIRD = 0.60


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
    k = m["k"]
    margin = helpers.safe_margin(spec)
    prog = t / dur

    # Full-bleed legible duotone portrait. Payoff gets a stronger push-in.
    zoom_span = 0.12 if beat.is_payoff else 0.05
    zoom = (1.0 + zoom_span) - zoom_span * easing.quart_out(min(1.0, prog))
    bw, bh = int(spec.width * zoom), int(spec.height * zoom)
    portrait, had_img = duotone.hero_portrait(beat.portrait_bytes, (bw, bh), anchor="top")
    canvas = Image.new("RGBA", (spec.width, spec.height), fs.hex_to_rgb(duotone.DUOTONE_SHADOW_V23) + (255,))
    canvas.alpha_composite(portrait, (-(bw - spec.width) // 2, -(bh - spec.height) // 2))

    if not had_img:  # 404 fallback — big initials on the brand panel
        init_fnt, _ = helpers.fit_text(beat.entity.initials, fs.FONT_BOLD, int(spec.height * 0.32), spec.width - 2 * margin)
        draw.draw_text(canvas, (spec.width // 2, int(spec.height * 0.40)),
                       beat.entity.initials, init_fnt, fill=style22.DUOTONE_HIGHLIGHT, anchor="mm", alpha=235)

    # Bottom scrim so the name reads on any photo.
    scrim = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(scrim).rectangle((0, int(spec.height * 0.5), spec.width, spec.height), fill=(10, 23, 48, 170))
    canvas.alpha_composite(scrim, (0, 0))

    # Rank glyph — slams in from the left (fit within the safe area).
    rank_fnt = helpers.fit_font(f"#{beat.rank}", fs.FONT_MONO_BOLD, m["rank"], spec.width - 2 * margin)
    rx = int(margin + anim.slide_offset(t, -spec.width * 0.3, 0.3))
    draw.draw_text(canvas, (rx, margin), f"#{beat.rank}", rank_fnt, fill=fs.ACCENT, alpha=240, anchor="lt")

    # ---- Lower-third band: counter (right) + name (left) + team. Fixed
    # anchors so baselines are consistent across beats. ----
    band_top = int(spec.height * LOWER_THIRD)
    a = anim.block_alpha(t, fade_in=0.3)

    # Counter: font sized to the FINAL value (stable as it counts), right-
    # aligned (grows leftward), settles in 0.4s then holds.
    final_str = f"{beat.mention_count:,}"
    count_fnt = helpers.fit_font(final_str, fs.FONT_MONO_BOLD, m["count"], int(spec.width * 0.40))
    cnum_w = draw.measure_text(final_str, count_fnt)[0]
    count_val = anim.count_up(t, beat.mention_count, COUNTER_SETTLE)
    cx = spec.width - margin
    draw.draw_text(canvas, (cx, band_top), f"{count_val:,}", count_fnt, fill=style22.DUOTONE_HIGHLIGHT, anchor="rt")
    lbl_fnt, _ = helpers.fit_text("MENTIONS", fs.FONT_MONO, m["count_label"], int(spec.width * 0.40))
    draw.draw_text(canvas, (cx, band_top + count_fnt.size + int(8 * k)), "MENTIONS", lbl_fnt, fill=fs.ACCENT, anchor="rt")

    # Name: left, ≤2 lines, auto-shrunk so the longest word fits; bottom-
    # aligned within a reserved 2-line block so the team baseline is fixed.
    name_maxw = (spec.width - margin) - cnum_w - int(40 * k) - margin
    name_fnt, name_lines = helpers.fit_wrapped(
        helpers.safe_text(beat.entity.name.upper()), fs.FONT_BOLD, m["name"],
        name_maxw, max_lines=2, min_size=int(m["name"] * 0.5),
    )
    name_lh = int(name_fnt.size * 1.04)
    # Reserve a FIXED 2-line block (from the max name size) so the team
    # baseline below is identical across beats regardless of how far an
    # individual name had to shrink (FIX 8 — no jumping baselines).
    name_block_h = 2 * int(m["name"] * 1.04)
    nlayer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    for i, ln in enumerate(reversed(name_lines)):  # bottom-aligned within the block
        y = band_top + name_block_h - (i + 1) * name_lh
        draw.draw_text(nlayer, (margin, y), ln, name_fnt, fill=style22.DUOTONE_HIGHLIGHT, anchor="lt")
    sub = beat.entity.team_context.upper() if (beat.entity.kind == "player" and beat.entity.team_context) else "PLAYER"
    sub_fnt, sub_txt = helpers.fit_text(helpers.safe_text(sub), fs.FONT_MONO, m["name_sub"], spec.width - 2 * margin)
    draw.draw_text(nlayer, (margin, band_top + name_block_h + int(12 * k)), sub_txt, sub_fnt, fill=fs.ACCENT, anchor="lt")
    canvas.alpha_composite(parallax.apply_layer_alpha(nlayer, a), (0, 0))

    # Source pills bottom edge.
    helpers.centered_pill_row(canvas, spec, beat.source_mix, m, spec.height - margin - m["pill"] - int(16 * k),
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

    # Reporter row at card top (larger avatar; name/handle fit the card).
    if rep:
        avatar = draw.circle_image(beat.quote_avatar_bytes, avatar_d,
                                   fallback_initials=(rep.display_name or rep.handle)[:1].upper(),
                                   fallback_bg=fs.ACCENT_DIM, fallback_fg=fs.ACCENT)
        canvas.alpha_composite(avatar, (inner_x, cy0 + pad_in))
        tx = inner_x + avatar_d + int(24 * m["k"])
        text_w = cx1 - pad_in - tx
        nfnt, ntxt = helpers.fit_text(helpers.safe_text(rep.display_name), fs.FONT_BOLD, m["reporter_name"], text_w)
        hfnt, htxt = helpers.fit_text(f"@{rep.handle}", fs.FONT_MONO, m["handle"], text_w)
        draw.draw_text(canvas, (tx, cy0 + pad_in + avatar_d // 2 - 6), ntxt, nfnt, fill=fs.TEXT, anchor="lb")
        draw.draw_text(canvas, (tx, cy0 + pad_in + avatar_d // 2 + 6), htxt, hfnt, fill=fs.TEXT_SECONDARY, anchor="lt")

    # Quote text — block slide-up + fade over 0.35s (not line-by-line).
    # Shrink the quote font until the longest word fits the card width so
    # nothing clips, then wrap sentence-safe.
    text = helpers.safe_text(_limit_words(beat.quote_text, MAX_QUOTE_WORDS))
    quote_fnt = helpers.fit_font(
        max(text.split(), key=len, default=text), fs.FONT_REGULAR, m["quote"], inner_w, min_size=int(m["quote"] * 0.55)
    )
    lines = quote_filter.prepare_quote_lines(text, lambda s: draw.measure_text(s, quote_fnt)[0], inner_w, max_lines=4)
    leading = int(quote_fnt.size * 1.35)
    qy = cy0 + pad_in + avatar_d + int(40 * m["k"])
    a = anim.block_alpha(t, fade_in=0.35)
    off = int(anim.slide_offset(t, spec.height * 0.03, 0.35))
    qlayer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    for i, ln in enumerate(lines):
        draw.draw_text(qlayer, (inner_x, qy + i * leading + off), ln, quote_fnt, fill=fs.TEXT, anchor="lt")
    canvas.alpha_composite(parallax.apply_layer_alpha(qlayer, a), (0, 0))

    # Engagement ticker bottom-right of the card, settles in 0.4s, holds.
    if beat.engagement is not None:
        total = anim.count_up(t, beat.engagement.total, COUNTER_SETTLE)
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


def _sparkline_plot_rect(spec, panel, header_bottom, axis_h, k):
    """Compute the chart plot rect explicitly from the actual phase
    panel (NOT a square assumption) so it's valid at every aspect."""
    inner = int(40 * k)
    px0, px1 = panel[0] + inner, panel[2] - inner
    bottom_reserve = axis_h + int(50 * k)  # axis labels + pills/context band
    plot_top = header_bottom + int(40 * k)
    plot_bottom = panel[3] - bottom_reserve
    if plot_bottom - plot_top < int(140 * k):  # guard: never collapse the chart
        plot_bottom = plot_top + int(140 * k)
    return (px0, plot_top, px1, plot_bottom)


def _draw_sparkline(spec: fs.FormatSpec, beat: BeatRenderDataV2, t: float, dur: float) -> Image.Image:
    m = style22.metrics(spec)
    k = m["k"]
    margin = helpers.safe_margin(spec)
    cx = spec.width // 2
    canvas = draw.new_canvas(spec, fill=fs.BACKGROUND)

    panel = (margin, margin, spec.width - margin, spec.height - margin)
    draw.rounded_rect(canvas, panel, radius=int(28 * k), fill=fs.ACCENT_DIM)
    inner = int(40 * k)
    avail = (panel[2] - inner) - (panel[0] + inner)

    # Counts forced to length 7; the displayed total is the SUM of these
    # 7 values, so hero / sparkline / outro all show the same number.
    counts = list(beat.weekly_counts)[:7] + [0] * max(0, 7 - len(beat.weekly_counts))
    total = sum(counts)
    labels = beat.day_labels if len(beat.day_labels) == 7 else ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    # Header (fit) + big total (fit).
    head_fnt, head_txt = helpers.fit_text(helpers.safe_text(f"#{beat.rank}  {beat.entity.name.upper()}"),
                                          fs.FONT_BOLD, m["spark_head"], avail)
    draw.draw_text(canvas, (cx, panel[1] + inner), head_txt, head_fnt, fill=fs.TEXT, anchor="mt")
    ty = panel[1] + inner + head_fnt.size + int(18 * k)
    total_fnt = helpers.fit_font(str(total), fs.FONT_MONO_BOLD, m["spark_total"], avail)
    draw.draw_text(canvas, (cx, ty), str(total), total_fnt, fill=fs.ACCENT, anchor="mt")
    lab_fnt, lab_txt = helpers.fit_text("MENTIONS THIS WEEK", fs.FONT_MONO, m["spark_total_label"], avail)
    draw.draw_text(canvas, (cx, ty + total_fnt.size + int(6 * k)), lab_txt, lab_fnt, fill=fs.TEXT_SECONDARY, anchor="mt")
    header_bottom = ty + total_fnt.size + int(6 * k) + lab_fnt.size

    axis_fnt = helpers.fit_font("Wed", fs.FONT_MONO, m["axis"], max(20, avail // 8))
    plot = _sparkline_plot_rect(spec, panel, header_bottom, axis_fnt.size + int(12 * k), k)
    px0, _, px1, pby = plot

    spark_t = min(1.0, t / 1.0)
    pts = sparkline.sparkline_points(counts, plot)
    dr = ImageDraw.Draw(canvas)
    dr.line([(px0, pby), (px1, pby)], fill=draw.rgb(fs.BORDER, 255), width=max(2, int(2 * k)))

    full, partial = sparkline.draw_progress(len(pts), spark_t)
    vis = list(pts[:full])
    if 0 < full < len(pts) and partial > 0:
        a0, b0 = pts[full - 1], pts[full]
        vis.append((int(a0[0] + (b0[0] - a0[0]) * partial), int(a0[1] + (b0[1] - a0[1]) * partial)))
    if len(vis) >= 2:
        fl = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(fl).polygon(vis + [(vis[-1][0], pby), (vis[0][0], pby)], fill=draw.rgb(fs.ACCENT, 90))
        canvas.alpha_composite(fl, (0, 0))
        dr.line(vis, fill=draw.rgb(fs.ACCENT, 255), width=max(4, int(5 * k)), joint="curve")
    elif len(vis) == 1:
        xx, yy = vis[0]
        dr.ellipse((xx - 5, yy - 5, xx + 5, yy + 5), fill=draw.rgb(fs.ACCENT, 255))

    # Peak marker + callout once the draw passes the peak.
    pk = sparkline.peak_index(counts)
    if pk >= 0 and ((full - 1) >= pk or full >= len(pts)):
        ppx, ppy = pts[pk]
        r = int(9 * k)
        dr.ellipse((ppx - r, ppy - r, ppx + r, ppy + r), fill=draw.rgb(fs.ACCENT, 255))
        if beat.peak_callout:
            co_fnt, co_txt = helpers.fit_text(helpers.safe_text(beat.peak_callout), fs.FONT_MONO_BOLD, m["callout"], avail)
            half = draw.measure_text(co_txt, co_fnt)[0] // 2
            cax = min(max(ppx, px0 + half), px1 - half)
            draw.draw_text(canvas, (cax, max(plot[1], ppy - int(30 * k))), co_txt, co_fnt, fill=fs.ACCENT, anchor="mb")

    # X-axis labels — fit + clamp first/last so they never clip the edge.
    for (lpx, _), lbl in zip(pts, labels):
        lt = helpers.safe_text(lbl)
        lw = draw.measure_text(lt, axis_fnt)[0]
        ax = min(max(lpx, px0 + lw // 2), px1 - lw // 2)
        draw.draw_text(canvas, (ax, pby + int(12 * k)), lt, axis_fnt, fill=fs.TEXT_SECONDARY, anchor="mt")

    # Source pills + context below the axis.
    a = anim.block_alpha(t - 0.3, fade_in=0.3)
    pills_y = pby + int(12 * k) + axis_fnt.size + int(30 * k)
    helpers.centered_pill_row(canvas, spec, beat.spike_source_mix, m, pills_y, alpha=a)
    if beat.context_line:
        ctx_fnt, ctx_txt = helpers.fit_text(helpers.safe_text(beat.context_line), fs.FONT_MONO, m["context"], avail)
        draw.draw_text(canvas, (cx, pills_y + m["pill"] + int(30 * k)), ctx_txt, ctx_fnt,
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
