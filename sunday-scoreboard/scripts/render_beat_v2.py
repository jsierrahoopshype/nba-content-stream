"""v2 "Spotlight Edit" per-beat renderer (with v2.1 layout fixes).

Three phases, hard-cut between beats (the next rank glyph slides in at
the top of the next hero — no crossfade):

  Phase 1  Hero portrait   (4s)  parallax gradient halo behind a
                                 contained slow-zoom headshot; rank
                                 glyph slides from top; name reveals
                                 letter-by-letter; pulsing mention
                                 count; source-mix pills. Every element
                                 sits in a reserved, non-overlapping
                                 zone (lib/layout.hero_layout).
  Phase 2  Bluesky quote   (5s)  CLEAN brand treatment — light bg with
                                 a subtle diagonal accent strip; the
                                 week's best *reporter* quote fades in
                                 line-by-line (sentence-safe, never
                                 mid-word); small avatar + reporter
                                 identity; engagement ticker 0→total.
  Phase 3  Mention spike   (3s)  per-player 7-day sparkline in the
                                 middle 50%; peak callout above; spike
                                 source-mix pills + a one-line context
                                 row below. No dead whitespace.

v2.1 changes are layout/content only — the 3-phase structure and per-
beat timing are unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from moviepy import VideoClip
from PIL import Image, ImageDraw

from lib import draw, easing, layout, parallax, quote_filter, sparkline
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
    """Everything the renderer needs for one beat, prefetched so the
    per-frame callback never touches the network."""

    rank: int
    entity: EntityInfo
    mention_count: int                       # cluster mentions (ranking signal)
    source_mix: dict[str, int]

    # Phase 1
    portrait_bytes: bytes | None = None

    # Phase 2 — best reporter quote of the week (already cleaned)
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
    context_line: str = ""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _entity_sub(entity: EntityInfo) -> str:
    if entity.kind == "player" and entity.team_context:
        return entity.team_context.upper()
    return "TEAM" if entity.kind == "team" else "PLAYER"


def _measure_pill_row(spec: fs.FormatSpec, mix: dict[str, int], fnt) -> tuple[int, int]:
    """Width + height of a source-pill row without committing it to the
    frame (draw onto a scratch layer, read the returned width)."""
    scratch = Image.new("RGBA", (spec.width, spec.height), (0, 0, 0, 0))
    w = draw.draw_source_mix(scratch, (0, 0), mix, fnt, gap=12)
    _, th = draw.measure_text("Bluesky · 9", fnt)
    return (w, th + 16)


def _hero_fonts(spec: fs.FormatSpec):
    return {
        "rank": draw.font(fs.FONT_MONO_BOLD, int(spec.rank_font_size * 0.7)),
        "name": draw.font(fs.FONT_BOLD, spec.title_font_size),
        "sub": draw.font(fs.FONT_MONO, spec.meta_font_size),
        "count": draw.font(fs.FONT_MONO_BOLD, int(spec.title_font_size * 1.1)),
        "count_label": draw.font(fs.FONT_MONO, spec.meta_font_size),
        "pill": draw.font(fs.FONT_MONO_BOLD, max(16, spec.meta_font_size - 2)),
    }


def _hero_zones(spec: fs.FormatSpec, beat: BeatRenderDataV2, fnts) -> tuple[dict, str]:
    """Compute the non-overlapping hero zones for this beat. Returns the
    zone dict and the (possibly truncated) name string that fits."""
    name_fit = draw.fit_text_to_width(beat.entity.name, fnts["name"], spec.width - spec.pad * 2)
    sub_text = _entity_sub(beat.entity)
    count_text = str(beat.mention_count)
    zones = layout.hero_layout(
        spec,
        rank_size=draw.measure_text(f"#{beat.rank}", fnts["rank"]),
        name_size=draw.measure_text(name_fit, fnts["name"]),
        sub_size=draw.measure_text(sub_text, fnts["sub"]),
        count_size=draw.measure_text(count_text, fnts["count"]),
        count_label_size=draw.measure_text("MENTIONS", fnts["count_label"]),
        pill_size=_measure_pill_row(spec, beat.source_mix, fnts["pill"]),
    )
    return zones, name_fit


# ---------------------------------------------------------------------------
# Phase 1 — hero portrait (collision-safe zones)
# ---------------------------------------------------------------------------


def _draw_hero(spec: fs.FormatSpec, beat: BeatRenderDataV2, t: float) -> Image.Image:
    prog = t / HERO_SECONDS
    canvas = draw.new_canvas(spec, fill=fs.BACKGROUND)
    fnts = _hero_fonts(spec)
    zones, name_fit = _hero_zones(spec, beat, fnts)
    portrait_box = zones["portrait"]
    d = portrait_box.width

    # Parallax gradient halo behind the portrait zone (subtle, brand).
    halo_d = int(d * 1.5)
    halo = parallax.vertical_gradient((halo_d, halo_d))
    bg_offset = parallax.parallax_offset(prog, distance=-160.0, speed=0.3)
    halo_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    hx = portrait_box.cx - halo_d // 2 + int(bg_offset)
    hy = portrait_box.cy - halo_d // 2
    halo_layer.alpha_composite(halo, (hx, hy))
    canvas.alpha_composite(parallax.apply_layer_alpha(halo_layer, 0.18), (0, 0))

    # Portrait — contained in its zone with a gentle settle-zoom and the
    # opposing (faster) parallax drift.
    portrait = draw.circle_image(
        beat.portrait_bytes, d,
        fallback_initials=beat.entity.initials,
        fallback_bg=fs.ACCENT_DIM, fallback_fg=fs.ACCENT,
        contain=(beat.entity.kind == "team"),
    )
    scale = parallax.zoom_scale(prog, start=1.08, end=1.0)
    fg_offset = parallax.parallax_offset(prog, distance=120.0, speed=1.0)
    nd = max(1, int(d * scale))
    scaled = portrait.resize((nd, nd), Image.LANCZOS)
    canvas.alpha_composite(
        scaled,
        (portrait_box.cx - nd // 2 + int(fg_offset), portrait_box.cy - nd // 2),
    )

    # Rank glyph — slides into its zone from above (quart-out, 0.6s).
    slide = easing.quart_out(min(1.0, t / 0.6))
    rank_box = zones["rank"]
    ry = int(rank_box.y0 - rank_box.height + rank_box.height * slide * 2)
    ry = min(rank_box.y0, ry)
    draw.draw_text(canvas, (rank_box.x0, ry), f"#{beat.rank}", fnts["rank"], fill=fs.ACCENT, alpha=235, anchor="lt")

    # Name — letter-by-letter reveal centered in its zone, starts at 2s.
    name_box = zones["name"]
    _staggered_text(canvas, (name_box.cx, name_box.cy), name_fit, fnts["name"], t - 2.0, fill=fs.TEXT)

    # Sub (team context) — fades in with the last letters.
    sub_box = zones["sub"]
    sub_alpha = easing.fade(t - 2.4, fade_in=0.5, fade_out=0.0, dur=max(0.6, HERO_SECONDS - 2.4))
    if sub_alpha > 0.02:
        draw.draw_text(canvas, (sub_box.cx, sub_box.cy), _entity_sub(beat.entity), fnts["sub"], fill=fs.TEXT_SECONDARY, anchor="mm", alpha=int(255 * sub_alpha))

    # Mention count + label — own right-aligned zone above the pills,
    # appears at 3s with a subtle pulse.
    if t >= 3.0:
        count_box = zones["count"]
        label_box = zones["count_label"]
        pulse = easing.pulse(t)
        count_fnt = draw.font(fs.FONT_MONO_BOLD, int(int(spec.title_font_size * 1.1) * pulse))
        draw.draw_text(canvas, (count_box.x1, count_box.y1), str(beat.mention_count), count_fnt, fill=fs.ACCENT, anchor="rb")
        draw.draw_text(canvas, (label_box.x1, label_box.y0), "MENTIONS", fnts["count_label"], fill=fs.TEXT_SECONDARY, anchor="rt")

    # Source-mix pills in the bottom zone, fade in after 0.8s.
    pills_box = zones["pills"]
    pill_alpha = easing.fade(t, fade_in=0.8, fade_out=0.0, dur=HERO_SECONDS)
    if pill_alpha > 0.02:
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw.draw_source_mix(layer, (pills_box.x0, pills_box.y0), beat.source_mix, fnts["pill"], gap=12)
        canvas.alpha_composite(parallax.apply_layer_alpha(layer, pill_alpha), (0, 0))

    return canvas


def _staggered_text(canvas, center, text, fnt, t, *, fill, per_letter=0.05, fade_in=0.25):
    """Draw `text` centered on `center`, each letter fading in 50ms after
    the previous (quart-out). Spaces advance but aren't drawn."""
    if not text:
        return
    widths = [draw.measure_text(ch or " ", fnt)[0] for ch in text]
    x = center[0] - sum(widths) // 2
    for i, ch in enumerate(text):
        a = parallax.letter_stagger_alpha(i, t, per_letter=per_letter, fade_in=fade_in)
        if ch.strip() and a > 0.01:
            draw.draw_text(canvas, (x, center[1]), ch, fnt, fill=fill, anchor="lm", alpha=int(255 * a))
        x += widths[i]


# ---------------------------------------------------------------------------
# Phase 2 — best Bluesky quote (clean brand treatment)
# ---------------------------------------------------------------------------


def _diagonal_accent_strip(spec: fs.FormatSpec) -> Image.Image:
    """A subtle diagonal accent gradient strip — the clean replacement
    for the muddy blurred-portrait backdrop."""
    W, H = spec.width, spec.height
    strip = parallax.vertical_gradient((int(H * 1.5), int(W * 0.22)), fs.ACCENT, fs.ACCENT_DIM)
    strip = strip.rotate(-20, expand=True)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    layer.alpha_composite(strip, (int(W * 0.55), int(-H * 0.25)))
    return parallax.apply_layer_alpha(layer, 0.16)


def _draw_quote(spec: fs.FormatSpec, beat: BeatRenderDataV2, t: float) -> Image.Image:
    canvas = draw.new_canvas(spec, fill=fs.BACKGROUND)
    canvas.alpha_composite(_diagonal_accent_strip(spec), (0, 0))

    rep = beat.quote_reporter

    # Reporter attribution top-left (small avatar + name + handle) at 1s.
    avatar_d = max(48, spec.portrait_size // 5)
    head_bottom = spec.pad + avatar_d
    if rep and t >= 1.0:
        ra = easing.fade(t - 1.0, fade_in=0.4, fade_out=0.0, dur=max(0.6, QUOTE_SECONDS - 1.0))
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

    if not beat.quote_text:
        # No roster-quality quote survived the filters — show the
        # spotlight cleanly rather than airing marketing copy.
        msg_fnt = draw.font(fs.FONT_REGULAR, spec.headline_font_size)
        draw.draw_text(canvas, (spec.width // 2, spec.height // 2), "No standout reporter quote this week.", msg_fnt, fill=fs.TEXT_SECONDARY, anchor="mm")
        return canvas

    # Quote text — large, DM Sans Regular, leading 1.4, sentence-safe
    # wrap (never mid-word), line-by-line fade.
    quote_fnt = draw.font(fs.FONT_REGULAR, int(spec.headline_font_size * 1.25))
    fsize = int(spec.headline_font_size * 1.25)
    max_w = spec.width - spec.pad * 2
    lines = quote_filter.prepare_quote_lines(
        beat.quote_text, lambda s: draw.measure_text(s, quote_fnt)[0], max_w, max_lines=5
    )
    leading = int(fsize * 1.4)
    block_h = leading * len(lines)
    start_y = max(head_bottom + 40, (spec.height - block_h) // 2)
    per_line = 1.5 / max(1, len(lines))
    for i, line in enumerate(lines):
        la = easing.fade(t - i * per_line, fade_in=0.4, fade_out=0.0, dur=max(0.6, QUOTE_SECONDS - i * per_line))
        if la < 0.02:
            continue
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw.draw_text(layer, (spec.pad, start_y + i * leading), line, quote_fnt, fill=fs.TEXT, anchor="lt")
        canvas.alpha_composite(parallax.apply_layer_alpha(layer, la), (0, 0))

    # "via @reporter" under the quote.
    if rep:
        via_fnt = draw.font(fs.FONT_MONO, spec.meta_font_size)
        va = easing.fade(t - 1.5, fade_in=0.4, fade_out=0.0, dur=max(0.6, QUOTE_SECONDS - 1.5))
        draw.draw_text(canvas, (spec.pad, start_y + block_h + 18), f"via @{rep.handle}", via_fnt, fill=fs.TEXT_SECONDARY, anchor="lt", alpha=int(255 * va))

    # Engagement ticker bottom-right: 0→total over 1.5s, pulse on the
    # final tick.
    if beat.engagement is not None:
        eng = beat.engagement
        tick = easing.quart_out(min(1.0, t / 1.5))
        shown = int(eng.total * tick)
        scale = easing.pulse(t, period=0.5, amplitude=0.10) if t >= 1.5 else 1.0
        big_fnt = draw.font(fs.FONT_MONO_BOLD, int((spec.title_font_size * 0.9) * scale))
        small_fnt = draw.font(fs.FONT_MONO, spec.meta_font_size)
        cx = spec.width - spec.pad
        cy = spec.height - spec.pad - 30
        draw.draw_text(canvas, (cx, cy), f"{shown:,}", big_fnt, fill=fs.ACCENT, anchor="rb")
        breakdown = f"{eng.likes:,} likes · {eng.reposts:,} reposts · {eng.replies:,} replies"
        draw.draw_text(canvas, (cx, cy + 8), breakdown, small_fnt, fill=fs.TEXT_SECONDARY, anchor="rt")

    return canvas


# ---------------------------------------------------------------------------
# Phase 3 — mention spike sparkline (rebalanced vertically)
# ---------------------------------------------------------------------------


def _draw_sparkline(spec: fs.FormatSpec, beat: BeatRenderDataV2, t: float) -> Image.Image:
    canvas = draw.new_canvas(spec, fill=fs.BACKGROUND)

    # Header: rank + name for continuity.
    head_fnt = draw.font(fs.FONT_BOLD, int(spec.title_font_size * 0.6))
    draw.draw_text(canvas, (spec.width // 2, spec.pad), f"#{beat.rank}  {beat.entity.name}", head_fnt, fill=fs.TEXT, anchor="mt")

    # Chart occupies the middle 50% of the frame height.
    box_top = int(spec.height * 0.28)
    box_h = int(spec.height * 0.40)
    box = (spec.pad + 40, box_top, spec.width - spec.pad - 40, box_top + box_h)

    spark_t = min(1.0, t / 1.5)
    sparkline.draw_sparkline(
        canvas, box, beat.weekly_counts, spark_t,
        labels=beat.day_labels, peak_callout=beat.peak_callout,
    )

    # Below the chart: source-mix pills, then a one-line context row —
    # keeps the bottom from being dead whitespace.
    if t >= 1.2:
        pa = easing.fade(t - 1.2, fade_in=0.4, fade_out=0.0, dur=max(0.6, SPARK_SECONDS - 1.2))
        pill_fnt = draw.font(fs.FONT_MONO_BOLD, max(16, spec.meta_font_size - 2))
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        row_w = draw.draw_source_mix(layer, (0, 0), beat.spike_source_mix, pill_fnt, gap=12)
        # Center the row.
        pills_y = box[3] + 60
        shifted = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        shifted.alpha_composite(layer, ((spec.width - row_w) // 2, pills_y))
        canvas.alpha_composite(parallax.apply_layer_alpha(shifted, pa), (0, 0))

        if beat.context_line:
            ctx_fnt = draw.font(fs.FONT_MONO, spec.meta_font_size)
            draw.draw_text(canvas, (spec.width // 2, pills_y + 60), beat.context_line, ctx_fnt, fill=fs.TEXT_SECONDARY, anchor="mt", alpha=int(255 * pa))

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
