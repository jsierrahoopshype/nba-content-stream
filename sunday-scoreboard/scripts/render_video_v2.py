"""Orchestrate the v2 "Spotlight Edit" pipeline (v2.2 social edit).

  fetch_week_data → cluster_beats → players-only + dedupe + rank
    → fetch_engagement (live Bluesky counts, cached per week)
    → enrich_v2 (portraits, roster/self-promo-filtered quote, spike)
    → cold open → beats in COUNTDOWN order (#N→#1) → outro → CTA
    → ffmpeg_compose: hard-cut concat + music mux + cut-timeline sidecar

v2 is parallel to v1 (shared lib/ + v1 scripts untouched). v2.2 is
vertical-first (9:16 default); square + horizontal still render.

CLI:
  python scripts/render_video_v2.py --week-of 2026-06-01 --top-n 5            # vertical
  python scripts/render_video_v2.py --week-of 2026-06-01 --format square
  python scripts/render_video_v2.py --week-of 2026-06-01 --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from lib import archive_client, beat_select, canonical_lookup, ffmpeg_compose  # noqa: E402
from lib import format_specs as fs  # noqa: E402
from lib import quote_filter, sparkline  # noqa: E402
from lib.engagement_score import (  # noqa: E402
    Engagement,
    bluesky_candidates,
    quote_text,
)
from lib.reporter_lookup import reporters_from_items  # noqa: E402
from cluster_beats import Beat, cluster_beats  # noqa: E402
from fetch_week_data import WeekRange, gather_week  # noqa: E402
from rank_beats import filter_noise, rank_beats  # noqa: E402
import fetch_engagement  # noqa: E402
from render_beat_v2 import BeatRenderDataV2, beat_phase_plan, render_beat_v2  # noqa: E402
from render_coldopen_v2 import render_coldopen_v2, COLD_OPEN_SECONDS  # noqa: E402
from render_outro_v2 import render_outro_v2  # noqa: E402
from render_cta_v2 import render_cta_v2  # noqa: E402

logger = logging.getLogger("render_video_v2")

_WEEKDAY_FULL = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
]


# ---------------------------------------------------------------------------
# Per-entity week index — the sparkline needs the entity's *whole week*,
# not just the 24h-island beat cluster.
# ---------------------------------------------------------------------------


def _entity_week_items(items: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Map (slug, kind) → every week item tagging that entity. Mirrors
    cluster_beats' multi-entity tagging so the spike chart counts every
    mention, including shared articles."""
    index: dict[tuple[str, str], list[dict]] = {}
    for it in items:
        for slug in it.get("players") or []:
            index.setdefault((slug, "player"), []).append(it)
        for slug in it.get("teams") or []:
            index.setdefault((slug, "team"), []).append(it)
    return index


def _peak_callout(week_start: datetime, counts: list[int]) -> str | None:
    pk = sparkline.peak_index(counts)
    if pk < 0:
        return None
    weekday = (week_start + timedelta(days=pk)).weekday()
    return f"{_WEEKDAY_FULL[weekday]}: +{counts[pk]} mentions"


def _spike_source_mix(items: list[dict], peak_index: int, week_start: datetime) -> dict[str, int]:
    """Source mix of the entity's items on the peak day (or whole week
    if no peak), capped at 5 entries for the pill row."""
    if peak_index >= 0:
        day_start = week_start + timedelta(days=peak_index)
        day_end = day_start + timedelta(days=1)
        subset = [
            it for it in items
            if (ts := _parse_iso(it.get("published_at", ""))) and day_start <= ts < day_end
        ]
    else:
        subset = items
    mix: dict[str, int] = {}
    for it in subset:
        src = it.get("source") or "unknown"
        mix[src] = mix.get(src, 0) + 1
    # Keep the 5 strongest sources for the pill row.
    return dict(sorted(mix.items(), key=lambda kv: -kv[1])[:5])


def _parse_iso(iso: str):
    from datetime import timezone
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------


def _context_line(total: int, week_start: datetime, counts: list[int]) -> str:
    """One-line spike context, e.g. "378 mentions this week · peaked
    Wednesday". Drops the "peaked" clause when there's no peak."""
    pk = sparkline.peak_index(counts)
    base = f"{total} mentions this week"
    if pk < 0:
        return base
    weekday = (week_start + timedelta(days=pk)).weekday()
    return f"{base} · peaked {_WEEKDAY_FULL[weekday]}"


def enrich_v2(
    beats: list[Beat],
    week: WeekRange,
    all_items: list[dict],
    engagement_by_uri: dict[str, Engagement],
    *,
    roster: set[str],
    blocklist: set[str],
    selfpromo: list[str] | None = None,
) -> list[BeatRenderDataV2]:
    """Hydrate ranked beats into v2 render data: portrait, best *reporter*
    quote (roster-filtered, self-promo-filtered, engagement-scored), and
    the 7-day spike series."""
    week_index = _entity_week_items(all_items)
    selfpromo = selfpromo or []
    enriched: list[BeatRenderDataV2] = []

    for rank, beat in enumerate(beats, start=1):
        info = canonical_lookup.lookup(beat.entity, kind_hint=beat.entity_kind)
        portrait_bytes = (
            archive_client.fetch_binary(info.portrait_url) if info.portrait_url else None
        )

        # Best quote: hard filters (roster, blocklist, length, emoji/caps,
        # self-promo) then engagement score. clean_text strips emoji.
        candidates = bluesky_candidates(beat.items)
        chosen, stages = quote_filter.select_quote_staged(
            candidates, engagement_by_uri,
            roster=roster, blocklist=blocklist, selfpromo=selfpromo,
        )
        logger.info("  %s", stages.log_line(beat.entity))
        q_text = ""
        reporter = None
        avatar_bytes = None
        engagement = None
        if chosen is not None:
            item, engagement = chosen
            q_text = quote_filter.clean_text(quote_text(item))
            reps = reporters_from_items([item], max_count=1)
            if reps:
                reporter = reps[0]
                avatar_bytes = (
                    archive_client.fetch_binary(reporter.avatar_url)
                    if reporter.avatar_url else None
                )

        # 7-day spike series from the entity's whole-week items.
        week_items = week_index.get((beat.entity, beat.entity_kind), beat.items)
        counts = sparkline.daily_mention_counts(week_items, week.week_of, days=7)
        labels = sparkline.day_labels(week.week_of, days=7)
        pk = sparkline.peak_index(counts)
        total = sum(counts)

        enriched.append(
            BeatRenderDataV2(
                rank=rank,
                entity=info,
                mention_count=beat.mention_count,
                source_mix=beat.source_mix,
                portrait_bytes=portrait_bytes,
                quote_text=q_text,
                quote_reporter=reporter,
                quote_avatar_bytes=avatar_bytes,
                engagement=engagement,
                weekly_counts=counts,
                weekly_total=total,
                day_labels=labels,
                spike_source_mix=_spike_source_mix(week_items, pk, week.week_of),
                peak_callout=_peak_callout(week.week_of, counts),
                context_line=_context_line(total, week.week_of, counts),
                is_payoff=(rank == 1),  # #1 is the countdown payoff
            )
        )
    return enriched


# ---------------------------------------------------------------------------
# v2.2 render order + cut timeline
# ---------------------------------------------------------------------------


def countdown_order(beats: list) -> list:
    """Render beats in ascending suspense — #5 first, #1 (payoff) last.
    Rank glyphs still show the true rank."""
    return sorted(beats, key=lambda b: -b.rank)


def build_cut_timeline(segments: list[tuple[str, float]]) -> list[dict]:
    """Turn ordered (label, duration) segments into absolute cut points
    [{label, start, end, duration}] for the music-sync sidecar."""
    out: list[dict] = []
    t = 0.0
    for label, dur in segments:
        out.append({
            "label": label,
            "start": round(t, 3),
            "end": round(t + dur, 3),
            "duration": round(dur, 3),
        })
        t += dur
    return out


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def run_pipeline(
    week_of: str,
    fmt_keys: list[str],
    *,
    out_dir: Path,
    dry_run: bool = False,
    top_n: int = 10,
    fetch_live: bool | None = None,
) -> list[Path]:
    """Build the v2 recap. Returns produced MP4 paths (empty on
    dry-run). `fetch_live` defaults to True for real renders, False for
    dry-runs (dry-runs reuse only the cached engagement)."""
    if fetch_live is None:
        fetch_live = not dry_run

    week = WeekRange.from_date_str(week_of)
    items = gather_week(week)
    if not items:
        logger.error("no items fetched for week %s — aborting", week_of)
        return []

    # v2.1 selection: players only → noise filter → rank → one beat per
    # player → top N. Teams never become beats (only hero-card context),
    # and the top-N is N distinct players.
    raw = cluster_beats(items, window_hours=24)
    player_beats = beat_select.players_only(raw)
    ranked_all = rank_beats(filter_noise(player_beats), top_n=len(player_beats))
    ranked = beat_select.one_beat_per_player(ranked_all)[:top_n]
    if not ranked:
        logger.error("no player beats survived selection — aborting")
        return []
    logger.info(
        "selected %d distinct players from %d items (%d raw beats, %d player beats)",
        len(ranked), len(items), len(raw), len(player_beats),
    )

    # Engagement re-fetch (cached per week) over the final beats' candidates.
    known = fetch_engagement.load_cache(week_of)
    uris = fetch_engagement.candidate_uris_from_beats([b.items for b in ranked])
    if fetch_live:
        engagement = fetch_engagement.fetch_engagement_for_uris(uris, known=known)
        fetch_engagement.save_cache(week_of, engagement)
    else:
        engagement = known
        logger.info("dry-run/offline: using %d cached engagement records", len(known))

    # Editorial quote gates: reporter roster (live) + explicit blocklist
    # + self-promo opening patterns (v2.2).
    roster = quote_filter.load_roster() if fetch_live else set()
    blocklist = quote_filter.load_blocklist()
    selfpromo = quote_filter.load_selfpromo_patterns()
    logger.info(
        "quote gates: roster=%d handles, blocklist=%d handles, selfpromo=%d patterns",
        len(roster), len(blocklist), len(selfpromo),
    )

    enriched = enrich_v2(
        ranked, week, items, engagement,
        roster=roster, blocklist=blocklist, selfpromo=selfpromo,
    )
    for b in enriched:
        logger.info(
            "  #%d %-28s %4d mentions · week %d · quote=%s · eng=%s",
            b.rank, b.entity.name, b.mention_count, b.weekly_total,
            "yes" if b.quote_text else "—",
            b.engagement.total if b.engagement else "—",
        )

    # Sanity guard: a render where *every* beat lost its quote almost
    # always means a handle-format mismatch in the roster join (the v2.1
    # production bug), not a genuinely quiet week — make it loud.
    if enriched and roster and all(not b.quote_text for b in enriched):
        logger.warning(
            "roster gate rejected all candidates for all %d beats — likely a "
            "handle format mismatch (check quote pipeline counts above)",
            len(enriched),
        )

    if dry_run:
        logger.info("--dry-run: skipping encode")
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    produced: list[Path] = []
    for key in fmt_keys:
        spec = fs.get_format(key)
        logger.info("rendering v2 format=%s (%dx%d)", key, spec.width, spec.height)
        produced.append(_render_one_format(week, spec, enriched, out_dir, week_of))
    return produced


def _write_cut_timeline(
    spec: fs.FormatSpec, order: list[BeatRenderDataV2], out_dir: Path, week_of: str
) -> Path:
    """Emit the music-sync sidecar: every phase-boundary timestamp for
    the assembled video, so a future pass can beat-sync when a track is
    dropped in. ffmpeg_compose itself stays silent-fallback."""
    segments: list[tuple[str, float]] = [("cold_open", COLD_OPEN_SECONDS)]
    for b in order:
        for phase, dur in beat_phase_plan(b.is_payoff):
            segments.append((f"beat_rank{b.rank}_{phase}", dur))
    from render_outro_v2 import OUTRO_SECONDS
    from render_cta_v2 import CTA_SECONDS
    segments.append(("outro", OUTRO_SECONDS))
    segments.append(("cta", CTA_SECONDS))

    timeline = build_cut_timeline(segments)
    payload = {
        "week_of": week_of,
        "format": spec.key,
        "fps": spec.fps,
        "total_seconds": timeline[-1]["end"] if timeline else 0.0,
        "cuts": timeline,
    }
    path = out_dir / f"{week_of}_{spec.key}_cuts.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("wrote cut timeline %s (%.1fs total)", path, payload["total_seconds"])
    return path


def _render_one_format(
    week: WeekRange,
    spec: fs.FormatSpec,
    enriched: list[BeatRenderDataV2],
    out_dir: Path,
    week_of: str,
) -> Path:
    # Cold open teases the #1 player's face (no name); beats play in
    # countdown order (#5 → #1); outro leaderboard; CTA end-card.
    payoff = next((b for b in enriched if b.is_payoff), enriched[0])
    order = countdown_order(enriched)
    coldopen = render_coldopen_v2(spec, payoff.portrait_bytes, len(enriched))
    beats = [render_beat_v2(spec, b) for b in order]
    leaderboard_rows = [(b.rank, b.entity.name, b.mention_count) for b in sorted(enriched, key=lambda b: b.rank)]
    outro = render_outro_v2(spec, week.week_of, leaderboard_rows)
    cta = render_cta_v2(spec)

    # Emit the cut-timeline sidecar alongside the video.
    _write_cut_timeline(spec, order, out_dir, week_of)

    # Hard cuts throughout (no crossfade) — the social-edit pace.
    full = ffmpeg_compose.concat_clips([coldopen, *beats, outro, cta])
    silent = out_dir / f"{week_of}_{spec.key}_v2.silent.mp4"
    final = out_dir / f"{week_of}_{spec.key}_v2.mp4"
    ffmpeg_compose.write_silent(full, silent, fps=spec.fps)
    ffmpeg_compose.mux_music(silent, fs.MUSIC_FILE, final)
    if silent.exists() and final.exists():
        silent.unlink()
    logger.info("wrote %s", final)
    return final


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Sunday Scoreboard v2 (Spotlight Edit) renderer.")
    p.add_argument("--week-of", required=True, help="Sunday opening the week (UTC, YYYY-MM-DD).")
    p.add_argument(
        "--format", choices=list(fs.FORMAT_SPECS), default="vertical",
        help="Format to render. v2.2 is vertical-first (9:16); default vertical. "
             "Use --format square / horizontal for the others.",
    )
    p.add_argument("--out-dir", default=None, help="Output dir (default: outputs/).")
    p.add_argument("--top-n", type=int, default=10, help="Number of beats (default 10).")
    p.add_argument("--dry-run", action="store_true", help="Walk pipeline, skip encode.")
    p.add_argument("--no-engagement", action="store_true", help="Skip live engagement fetch; use cache only.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def run(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    out_dir = Path(args.out_dir) if args.out_dir else fs.OUTPUTS_DIR
    fetch_live = None if not args.no_engagement else False
    paths = run_pipeline(
        week_of=args.week_of,
        fmt_keys=[args.format],
        out_dir=out_dir,
        dry_run=args.dry_run,
        top_n=args.top_n,
        fetch_live=fetch_live,
    )
    for p_ in paths:
        print(p_)
    return 0 if (args.dry_run or paths) else 1


if __name__ == "__main__":
    sys.exit(run())
