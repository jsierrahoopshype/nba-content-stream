"""v2 "Spotlight Edit" per-beat renderer.

Each beat is a deeper, more emotional spotlight on one player than the
v1 leaderboard-roll. Three phases, hard-cut between beats (the next
rank glyph slides in at the top of the next hero — no crossfade):

  Phase 1  Hero portrait   (4s)  parallax gradient behind a slow-zoom
                                 headshot; rank glyph slides from top;
                                 name reveals letter-by-letter; pulsing
                                 mention count; source-mix pills.
  Phase 2  Bluesky quote   (5s)  desaturated, blurred, Ken-Burns
                                 headshot bg; best quote of the week
                                 fades in line-by-line; reporter
                                 identity; engagement ticker 0→total.
  Phase 3  Mention spike   (3s)  per-player 7-day sparkline drawn
                                 left-to-right; peak day callout; spike
                                 source-mix pills; weekly total.

Layout reads centered so it holds across formats; the validated target
for the first v2 PR is square (1080×1080). Per-format font sizes still
come from FormatSpec so horizontal/vertical inherit sane type scales
when they land in v2.1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from moviepy import VideoClip
from PIL import Image

from lib import draw, easing, parallax, sparkline
from lib import format_specs as fs
from lib.canonical_lookup import EntityInfo
from lib.engagement_score import Engagement
from lib.reporter_lookup import Reporter

logger = logging.getLogger("render_beat_v2")

# Phase durations (seconds). Sum = 12s/beat; hard-cut between beats.
HERO_SECONDS = 4.0
QUOTE_SECONDS = 5.0
SPARK_SECONDS = 3.0
BEAT_SECONDS = HERO_SECONDS + QUOTE_SECONDS + SPARK_SECONDS


@dataclass
class BeatRenderDataV2:
    """Everything the v2 renderer needs for one beat, prefetched so the
    per-frame callback never touches the network."""

    rank: int
    entity: EntityInfo
    mention_count: int                       # cluster mentions (ranking signal)
    source_mix: dict[str, int]

    # Phase 1
    portrait_bytes: bytes | None = None

    # Phase 2 — best quote of the week
    quote_text: str = ""
    quote_reporter: Reporter | None = None
    quote_avatar_bytes: bytes | None = None
    engagement: Engagement | None = None

    # Phase 3 — 7-day spike chart
    weekly_counts: list[int] = field(default_factory=list)
    weekly_total: int = 0
    day_labels: list[str] = field(default_factory=list)
    spike_source_mix: dict[str, int] = field(default_factory=dict)
    peak_callout: str | None = None


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _staggered_text(
    canvas: Image.Image,
    center: tuple[int, int],
    text: str,
    fnt,
    t: float,
    *,
    fill: str,
    per_letter: float = 0.05,
    fade_in: float = 0.25,
) -> None:
    """Draw `text` centered on `center`, each letter fading in 50ms
    after the previous (quart-out). Spaces are advanced but not drawn."""
    if not text:
        return
    widths = [draw.measure_text(ch or " ", fnt)[0] for ch in text]
    total_w = sum(widths)
    x = center[0] - total_w // 2
    cy = center[1]
    for i, ch in enumerate(text):
        a = parallax.letter_stagger_alpha(i, t, per_letter=per_letter, fade_in=fade_in)
        if ch.strip() and a > 0.01:
            draw.draw_text(
                canvas, (x, cy), ch, fnt, fill=fill,
                anchor="lm", alpha=int(255 * a),
            )
        x += widths[i]


def _entity_sub(entity: EntityInfo) -> str:
    if entity.kind == "player" and entity.team_context:
        return entity.team_context.upper()
    return "TEAM" if entity.kind == "team" else "PLAYER"


# ---------------------------------------------------------------------------
# Phase 1 — hero portrait with parallax
# ---------------------------------------------------------------------------


def _draw_hero(spec: fs.FormatSpec, beat: BeatRenderDataV2, t: float) -> Image.Image:
    prog = t / HERO_SECONDS
    canvas = draw.new_canvas(spec, fill=fs.BACKGROUND)

    # Background gradient parallaxes opposite the portrait at 0.3x.
    grad = parallax.vertical_gradient((int(spec.width * 1.3), spec.height))
    bg_offset = parallax.parallax_offset(prog, distance=-200.0, speed=0.3)
    canvas.alpha_composite(
        parallax.apply_layer_alpha(
            parallax.place_scaled(grad, (spec.width, spec.height), 1.0, (bg_offset, 0)),
            0.92,
        ),
        (0, 0),
    )

    # Portrait: starts ~screen-height tall, slow-zooms to 60% (quart-out).
    base_d = spec.height
    portrait = draw.circle_image(
        beat.portrait_bytes, base_d,
        fallback_initials=beat.entity.initials,
        fallback_bg=fs.ACCENT_DIM, fallback_fg=fs.ACCENT,
        contain=(beat.entity.kind == "team"),
    )
    scale = parallax.zoom_scale(prog, start=1.0, end=0.6)
    # Portrait moves with the foreground (opposite the slower bg).
    fg_offset = parallax.parallax_offset(prog, distance=200.0, speed=1.0)
    portrait_layer = parallax.place_scaled(
        portrait, (spec.width, spec.height), scale, (fg_offset, 0)
    )
    canvas.alpha_composite(portrait_layer, (0, 0))

    # Rank glyph — slides in from the top with quart-out over 0.6s.
    rank_fnt = draw.font(fs.FONT_MONO_BOLD, int(spec.rank_font_size * 1.4))
    slide = easing.quart_out(min(1.0, t / 0.6))
    target_y = spec.pad + 20
    ry = int(-spec.rank_font_size + (target_y + spec.rank_font_size) * slide)
    draw.draw_text(
        canvas, (spec.pad + 10, ry), f"#{beat.rank}", rank_fnt,
        fill=fs.ACCENT, alpha=235, anchor="lt",
    )

    # Name — staggered letter reveal starting at 2s.
    name_fnt = draw.font(fs.FONT_BOLD, spec.title_font_size)
    _staggered_text(
        canvas, (spec.width // 2, int(spec.height * 0.72)),
        beat.entity.name, name_fnt, t - 2.0, fill=fs.TEXT,
    )
    # Sub (team context) under the name, fading in with the last letters.
    sub_alpha = easing.fade(t - 2.4, fade_in=0.5, fade_out=0.0, dur=max(0.6, HERO_SECONDS - 2.4))
    if sub_alpha > 0.02:
        sub_fnt = draw.font(fs.FONT_MONO, spec.meta_font_size)
        draw.draw_text(
            canvas, (spec.width // 2, int(spec.height * 0.72) + spec.title_font_size),
            _entity_sub(beat.entity), sub_fnt,
            fill=fs.TEXT_SECONDARY, anchor="mt", alpha=int(255 * sub_alpha),
        )

    # Mention count (mono, accent, subtle pulse) bottom-right at 3s.
    if t >= 3.0:
        pulse = easing.pulse(t)
        count_fnt = draw.font(fs.FONT_MONO_BOLD, int(spec.title_font_size * 1.2 * pulse))
        label_fnt = draw.font(fs.FONT_MONO, spec.meta_font_size)
        cx = spec.width - spec.pad
        cy = spec.height - spec.pad - 40
        draw.draw_text(canvas, (cx, cy), str(beat.mention_count), count_fnt, fill=fs.ACCENT, anchor="rb")
        draw.draw_text(canvas, (cx, cy + 6), "MENTIONS", label_fnt, fill=fs.TEXT_SECONDARY, anchor="rt")

    # Source-mix pills along the bottom edge.
    pill_fnt = draw.font(fs.FONT_MONO_BOLD, max(16, spec.meta_font_size - 2))
    sub = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    row_w = draw.draw_source_mix(sub, (0, 0), beat.source_mix, pill_fnt, gap=12)
    mx = (spec.width - row_w) // 2
    my = spec.height - spec.pad + 4
    scratch = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    scratch.alpha_composite(sub, (mx, my - 60))
    canvas.alpha_composite(parallax.apply_layer_alpha(scratch, easing.fade(t, fade_in=0.8, fade_out=0.0, dur=HERO_SECONDS)), (0, 0))

    return canvas


# ---------------------------------------------------------------------------
# Phase 2 — best Bluesky quote
# ---------------------------------------------------------------------------


def _draw_quote(spec: fs.FormatSpec, beat: BeatRenderDataV2, t: float) -> Image.Image:
    prog = t / QUOTE_SECONDS
    canvas = draw.new_canvas(spec, fill=fs.BACKGROUND)

    # Background: desaturated, blurred headshot with a slow Ken Burns pan.
    if beat.portrait_bytes:
        full = draw.circle_image(
            beat.portrait_bytes, max(spec.width, spec.height),
            fallback_initials=beat.entity.initials, contain=False,
        )
        bg = parallax.desaturate_blur(full)
        kb = parallax.ken_burns(bg, (spec.width, spec.height), prog, pan_px=20.0)
        canvas.alpha_composite(parallax.apply_layer_alpha(kb, 0.6), (0, 0))

    # Reporter identity, top-left, appears at 1s.
    rep = beat.quote_reporter
    if rep and t >= 1.0:
        ra = easing.fade(t - 1.0, fade_in=0.4, fade_out=0.0, dur=max(0.6, QUOTE_SECONDS - 1.0))
        avatar_d = max(48, spec.portrait_size // 4)
        avatar = draw.circle_image(
            beat.quote_avatar_bytes, avatar_d,
            fallback_initials=(rep.display_name or rep.handle)[:1].upper(),
            fallback_bg=fs.ACCENT_DIM, fallback_fg=fs.ACCENT,
        )
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ax, ay = spec.pad, spec.pad
        layer.alpha_composite(avatar, (ax, ay))
        name_fnt = draw.font(fs.FONT_BOLD, spec.meta_font_size + 6)
        handle_fnt = draw.font(fs.FONT_MONO, spec.meta_font_size)
        tx = ax + avatar_d + 20
        draw.draw_text(layer, (tx, ay + avatar_d // 2 - 6), rep.display_name, name_fnt, fill=fs.TEXT, anchor="lb")
        draw.draw_text(layer, (tx, ay + avatar_d // 2 + 6), f"@{rep.handle}", handle_fnt, fill=fs.TEXT_SECONDARY, anchor="lt")
        canvas.alpha_composite(parallax.apply_layer_alpha(layer, ra), (0, 0))

    # Quote text — large, DM Sans Regular, leading 1.4, line-by-line fade.
    quote_fnt = draw.font(fs.FONT_REGULAR, int(spec.headline_font_size * 1.25))
    max_w = spec.width - spec.pad * 2
    lines = draw.wrap_text(beat.quote_text, quote_fnt, max_w, max_lines=5)
    leading = int(int(spec.headline_font_size * 1.25) * 1.4)
    block_h = leading * len(lines)
    start_y = (spec.height - block_h) // 2
    per_line = 1.5 / max(1, len(lines))
    for i, line in enumerate(lines):
        la = easing.fade(t - i * per_line, fade_in=0.4, fade_out=0.0, dur=max(0.6, QUOTE_SECONDS - i * per_line))
        if la < 0.02:
            continue
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw.draw_text(layer, (spec.pad, start_y + i * leading), line, quote_fnt, fill=fs.TEXT, anchor="lt")
        canvas.alpha_composite(parallax.apply_layer_alpha(layer, la), (0, 0))

    # "via @reporter" attribution under the quote.
    if rep:
        via_fnt = draw.font(fs.FONT_MONO, spec.meta_font_size)
        va = easing.fade(t - 1.5, fade_in=0.4, fade_out=0.0, dur=max(0.6, QUOTE_SECONDS - 1.5))
        draw.draw_text(
            canvas, (spec.pad, start_y + block_h + 18), f"via @{rep.handle}",
            via_fnt, fill=fs.TEXT_SECONDARY, anchor="lt", alpha=int(255 * va),
        )

    # Engagement ticker bottom-right: 0→total over 1.5s, pulse on final tick.
    if beat.engagement is not None:
        eng = beat.engagement
        tick = easing.quart_out(min(1.0, t / 1.5))
        shown = int(eng.total * tick)
        at_final = t >= 1.5
        scale = easing.pulse(t, period=0.5, amplitude=0.10) if at_final else 1.0
        big_fnt = draw.font(fs.FONT_MONO_BOLD, int((spec.title_font_size * 0.9) * scale))
        small_fnt = draw.font(fs.FONT_MONO, spec.meta_font_size)
        cx = spec.width - spec.pad
        cy = spec.height - spec.pad - 30
        draw.draw_text(canvas, (cx, cy), f"{shown:,}", big_fnt, fill=fs.ACCENT, anchor="rb")
        breakdown = f"{eng.likes:,} likes · {eng.reposts:,} reposts · {eng.replies:,} replies"
        draw.draw_text(canvas, (cx, cy + 8), breakdown, small_fnt, fill=fs.TEXT_SECONDARY, anchor="rt")

    return canvas


# ---------------------------------------------------------------------------
# Phase 3 — mention spike sparkline
# ---------------------------------------------------------------------------


def _draw_sparkline(spec: fs.FormatSpec, beat: BeatRenderDataV2, t: float) -> Image.Image:
    canvas = draw.new_canvas(spec, fill=fs.BACKGROUND)

    # Header: rank + name so context carries over from the hero.
    head_fnt = draw.font(fs.FONT_BOLD, int(spec.title_font_size * 0.6))
    draw.draw_text(
        canvas, (spec.width // 2, spec.pad),
        f"#{beat.rank}  {beat.entity.name}", head_fnt, fill=fs.TEXT, anchor="mt",
    )

    # Total weekly mentions above the chart (mono).
    total_fnt = draw.font(fs.FONT_MONO_BOLD, int(spec.title_font_size * 0.9))
    label_fnt = draw.font(fs.FONT_MONO, spec.meta_font_size)
    chart_top = spec.pad + int(spec.title_font_size * 0.6) + 50
    draw.draw_text(canvas, (spec.width // 2, chart_top), str(beat.weekly_total), total_fnt, fill=fs.ACCENT, anchor="mt")
    draw.draw_text(canvas, (spec.width // 2, chart_top + int(spec.title_font_size * 0.9) + 6), "MENTIONS THIS WEEK", label_fnt, fill=fs.TEXT_SECONDARY, anchor="mt")

    # Chart box — centered, leaving room for axis labels + pills below.
    box_top = chart_top + int(spec.title_font_size * 0.9) + 70
    box_h = int(spec.height * 0.30)
    box = (spec.pad + 40, box_top, spec.width - spec.pad - 40, box_top + box_h)

    # Animated draw over the first 1.5s of the phase.
    spark_t = min(1.0, t / 1.5)
    sparkline.draw_sparkline(
        canvas, box, beat.weekly_counts, spark_t,
        labels=beat.day_labels, peak_callout=beat.peak_callout,
    )

    # Spike source-mix pills (up to 5) below the chart, fade in after draw.
    if t >= 1.2:
        pa = easing.fade(t - 1.2, fade_in=0.4, fade_out=0.0, dur=max(0.6, SPARK_SECONDS - 1.2))
        pill_fnt = draw.font(fs.FONT_MONO_BOLD, max(16, spec.meta_font_size - 2))
        sub = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        row_w = draw.draw_source_mix(sub, (0, 0), beat.spike_source_mix, pill_fnt, gap=12)
        mx = (spec.width - row_w) // 2
        my = box[3] + 70
        scratch = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        scratch.alpha_composite(sub, (mx, my))
        canvas.alpha_composite(parallax.apply_layer_alpha(scratch, pa), (0, 0))

    return canvas


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_beat_v2(spec: fs.FormatSpec, beat: BeatRenderDataV2) -> VideoClip:
    """Return one VideoClip covering all three spotlight phases."""

    def make_frame(t):
        if t < HERO_SECONDS:
            img = _draw_hero(spec, beat, t)
        elif t < HERO_SECONDS + QUOTE_SECONDS:
            img = _draw_quote(spec, beat, t - HERO_SECONDS)
        else:
            img = _draw_sparkline(spec, beat, t - HERO_SECONDS - QUOTE_SECONDS)
        return draw.to_numpy(img)

    return VideoClip(make_frame, duration=BEAT_SECONDS)
