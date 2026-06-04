"""Orchestrate the v2 "Spotlight Edit" pipeline.

  fetch_week_data
    → cluster_beats
    → rank_beats (top N)
    → fetch_engagement (live Bluesky counts, cached per week)
    → enrich_v2 (portraits, best quote, 7-day spike series)
    → render intro_v2 + spotlight beats + outro_v2
    → ffmpeg_compose: hard-cut concat + music mux

v2 is *parallel* to v1 — it reuses the shared lib/ unchanged and the
v1 scripts are untouched. The first v2 PR validates square only;
horizontal/vertical land in v2.1.

CLI:
  python scripts/render_video_v2.py --week-of 2026-06-01 --format square --top-n 10
  python scripts/render_video_v2.py --week-of 2026-06-01 --format square --dry-run
"""

from __future__ import annotations

import argparse
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
from render_beat_v2 import BeatRenderDataV2, render_beat_v2  # noqa: E402
from render_intro_v2 import render_intro_v2  # noqa: E402
from render_outro_v2 import render_outro_v2  # noqa: E402

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
) -> list[BeatRenderDataV2]:
    """Hydrate ranked beats into v2 render data: portrait, best *reporter*
    quote (roster-filtered, engagement-scored), and the 7-day spike
    series."""
    week_index = _entity_week_items(all_items)
    enriched: list[BeatRenderDataV2] = []

    for rank, beat in enumerate(beats, start=1):
        info = canonical_lookup.lookup(beat.entity, kind_hint=beat.entity_kind)
        portrait_bytes = (
            archive_client.fetch_binary(info.portrait_url) if info.portrait_url else None
        )

        # Best quote: hard filters (roster, length, emoji/caps) then
        # engagement score. clean_text strips emoji for display.
        candidates = bluesky_candidates(beat.items)
        chosen = quote_filter.select_quote(
            candidates, engagement_by_uri, roster=roster, blocklist=blocklist
        )
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
            )
        )
    return enriched


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

    # Editorial quote gates: reporter roster (live) + explicit blocklist.
    roster = quote_filter.load_roster() if fetch_live else set()
    blocklist = quote_filter.load_blocklist()
    logger.info("quote gates: roster=%d handles, blocklist=%d handles", len(roster), len(blocklist))

    enriched = enrich_v2(ranked, week, items, engagement, roster=roster, blocklist=blocklist)
    for b in enriched:
        logger.info(
            "  #%d %-28s %4d mentions · week %d · quote=%s · eng=%s",
            b.rank, b.entity.name, b.mention_count, b.weekly_total,
            "yes" if b.quote_text else "—",
            b.engagement.total if b.engagement else "—",
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


def _render_one_format(
    week: WeekRange,
    spec: fs.FormatSpec,
    enriched: list[BeatRenderDataV2],
    out_dir: Path,
    week_of: str,
) -> Path:
    intro = render_intro_v2(spec, week.week_of)
    beats = [render_beat_v2(spec, b) for b in enriched]
    leaderboard_rows = [(b.rank, b.entity.name, b.mention_count) for b in enriched]
    outro = render_outro_v2(spec, week.week_of, leaderboard_rows)

    # Hard cut between beats — concat with no crossfade (the next rank
    # glyph slides in at the top of the next hero phase).
    full = ffmpeg_compose.concat_clips([intro, *beats, outro])
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
        "--format", choices=list(fs.FORMAT_SPECS), default="square",
        help="Format to render (v2 validates square first; default square).",
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
