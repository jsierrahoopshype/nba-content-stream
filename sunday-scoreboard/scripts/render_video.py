"""Orchestrate the full Sunday Scoreboard pipeline.

  fetch_week_data
    → cluster_beats
    → rank_beats
    → enrich (lookup portraits, reporters, prefetch binaries)
    → render intro + beats + outro
    → ffmpeg_compose: stitch + add music

CLI:
  python scripts/render_video.py --week-of 2026-05-25 --format square
  python scripts/render_video.py --week-of 2026-05-25 --all-formats
  python scripts/render_video.py --week-of 2026-05-25 --format square --dry-run

`--dry-run` skips the actual video encode but still walks the
pipeline so you can see the data choice, beat counts, and per-beat
metadata that would surface in the render.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow `python scripts/render_video.py` from the sunday-scoreboard root.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from lib import archive_client, canonical_lookup, ffmpeg_compose  # noqa: E402
from lib import format_specs as fs  # noqa: E402
from lib.reporter_lookup import reporters_from_items  # noqa: E402
from cluster_beats import Beat, cluster_beats  # noqa: E402
from fetch_week_data import WeekRange, gather_week  # noqa: E402
from rank_beats import rank_and_filter  # noqa: E402
from render_beat import BeatRenderData, render_beat  # noqa: E402
from render_intro import render_intro  # noqa: E402
from render_outro import render_outro  # noqa: E402

logger = logging.getLogger("render_video")


# ---------------------------------------------------------------------------
# Enrichment — turn a cluster Beat into a BeatRenderData with prefetched
# portraits, reporters, and chosen headlines.
# ---------------------------------------------------------------------------


def _relative_when(item: dict, week_end: datetime) -> str:
    iso = item.get("published_at") or ""
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return ""
    delta = week_end - ts
    hours = int(delta.total_seconds() / 3600)
    if hours < 1:
        return "just now"
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _pick_headlines(beat: Beat, week_end: datetime, *, n: int = 3) -> list[dict]:
    """Pick `n` representative headlines from a cluster.

    Strategy: take one per source where possible (so the headline-roll
    feels source-diverse), then fill from the rest by recency. Falls
    back to bare recency when the cluster has fewer sources than `n`.
    """
    by_source: dict[str, list[dict]] = {}
    for it in beat.items:
        by_source.setdefault(it.get("source") or "unknown", []).append(it)
    for arr in by_source.values():
        arr.sort(key=lambda x: x.get("published_at", ""), reverse=True)
    out: list[dict] = []
    seen_ids: set[str] = set()
    # Pass 1: one per source.
    for src, arr in by_source.items():
        if arr and len(out) < n:
            picked = arr[0]
            out.append(picked)
            seen_ids.add(picked.get("id", ""))
    # Pass 2: fill by recency from anything not yet chosen.
    if len(out) < n:
        rest = sorted(
            (it for it in beat.items if it.get("id") not in seen_ids),
            key=lambda x: x.get("published_at", ""), reverse=True,
        )
        out.extend(rest[: n - len(out)])
    # Convert to the renderer's dict shape.
    rendered = []
    mix = beat.source_mix
    for h in out[:n]:
        rendered.append({
            "title": h.get("title") or "(no title)",
            "source": h.get("source") or "",
            "when_ago": _relative_when(h, week_end),
            "rank_in_mix": mix.get(h.get("source") or "", 1),
        })
    return rendered


def enrich(beats: list[Beat], week: WeekRange) -> list[BeatRenderData]:
    """Hydrate beats with canonical entity info, top reporters,
    chosen headlines, and prefetched portrait + avatar bytes."""
    enriched: list[BeatRenderData] = []
    for idx, beat in enumerate(beats, start=1):
        info = canonical_lookup.lookup(beat.entity, kind_hint=beat.entity_kind)
        portrait_bytes = (
            archive_client.fetch_binary(info.portrait_url)
            if info.portrait_url
            else None
        )
        reporters = reporters_from_items(beat.items, max_count=3)
        avatar_bytes = [
            archive_client.fetch_binary(r.avatar_url) if r.avatar_url else None
            for r in reporters
        ]
        enriched.append(
            BeatRenderData(
                rank=idx,
                entity=info,
                mention_count=beat.mention_count,
                top_headlines=_pick_headlines(beat, week.end),
                top_reporters=reporters,
                source_mix=beat.source_mix,
                time_span_hours=beat.time_span_hours,
                portrait_bytes=portrait_bytes,
                reporter_avatar_bytes=avatar_bytes,
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
) -> list[Path]:
    """Build the recap end to end. Returns paths of the produced MP4s
    (empty list if `dry_run`)."""
    week = WeekRange.from_date_str(week_of)
    items = gather_week(week)
    if not items:
        logger.error("no items fetched for week %s — aborting", week_of)
        return []

    raw_beats = cluster_beats(items, window_hours=24)
    ranked = rank_and_filter(raw_beats, top_n=top_n)
    logger.info(
        "clustered %d beats from %d items → ranked top %d",
        len(raw_beats), len(items), len(ranked),
    )

    if not ranked:
        logger.error("no beats survived noise filter — aborting")
        return []

    enriched = enrich(ranked, week)
    for b in enriched:
        logger.info(
            "  #%d %-30s %5d mentions · %s sources · %.1fh span",
            b.rank, b.entity.name, b.mention_count,
            len(b.source_mix), b.time_span_hours,
        )

    if dry_run:
        logger.info("--dry-run: skipping encode")
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    produced: list[Path] = []
    for key in fmt_keys:
        spec = fs.get_format(key)
        logger.info("rendering format=%s (%dx%d)", key, spec.width, spec.height)
        produced.append(
            _render_one_format(
                week, spec, enriched, out_dir, week_of,
            )
        )
    return produced


def _render_one_format(
    week: WeekRange,
    spec: fs.FormatSpec,
    enriched: list[BeatRenderData],
    out_dir: Path,
    week_of: str,
) -> Path:
    intro = render_intro(spec, week.week_of)
    beats = [render_beat(spec, b) for b in enriched]
    leaderboard_rows = [
        (b.rank, b.entity.name, b.mention_count) for b in enriched
    ]
    outro = render_outro(spec, week.week_of, leaderboard_rows)

    full = ffmpeg_compose.concat_clips([intro, *beats, outro])
    silent = out_dir / f"{week_of}_{spec.key}.silent.mp4"
    final = out_dir / f"{week_of}_{spec.key}.mp4"
    ffmpeg_compose.write_silent(full, silent, fps=spec.fps)
    ffmpeg_compose.mux_music(silent, fs.MUSIC_FILE, final)
    # Keep only the final MP4 — silent intermediate is debug-only.
    if silent.exists() and final.exists():
        silent.unlink()
    logger.info("wrote %s", final)
    return final


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Sunday Scoreboard render orchestrator.")
    p.add_argument("--week-of", required=True, help="Sunday opening the week (UTC, YYYY-MM-DD).")
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--format",
        choices=list(fs.FORMAT_SPECS),
        help="Render a single format.",
    )
    g.add_argument(
        "--all-formats", action="store_true",
        help="Render all three formats (horizontal, square, vertical).",
    )
    p.add_argument(
        "--out-dir", default=None,
        help="Output dir (default: sunday-scoreboard/outputs).",
    )
    p.add_argument(
        "--top-n", type=int, default=10,
        help="Number of beats to include (default 10).",
    )
    p.add_argument("--dry-run", action="store_true", help="Walk pipeline, skip encode.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def run(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.all_formats:
        fmt_keys = list(fs.FORMAT_SPECS)
    elif args.format:
        fmt_keys = [args.format]
    else:
        fmt_keys = ["square"]
    out_dir = Path(args.out_dir) if args.out_dir else fs.OUTPUTS_DIR
    paths = run_pipeline(
        week_of=args.week_of,
        fmt_keys=fmt_keys,
        out_dir=out_dir,
        dry_run=args.dry_run,
        top_n=args.top_n,
    )
    if paths:
        for p_ in paths:
            print(p_)
    return 0 if (args.dry_run or paths) else 1


if __name__ == "__main__":
    sys.exit(run())
