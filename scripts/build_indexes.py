"""Build per-entity, trending, feed, and manifest indexes from hot shards.

Reads every `data/{source}/{YYYY-MM-DD}.json` shard, dedupes by item id,
filters to the rolling window, and regenerates these files under
`data/index/`:

  - `players/{slug}.json` — one per player with content. Compact item
    refs only.
  - `teams/{slug}.json` — same shape, per team.
  - `trending.json` — top-N items across all sources, scored by
    recency + source weight (DESIGN.md 7.7).
  - `feed.json` — homepage merged stream, all sources, newest-first.
  - `manifest.json` — slug list with counts for the search box +
    pre-render step.

The build is idempotent: every run rebuilds from scratch, so edits or
deletions in shards propagate cleanly. We never touch cold-tier/R2,
never look inside `data/index/` or `data/sources/`, and never write
back to the source shards.

CLI:
  --dry-run        Compute + report counts; write nothing.
  --window-days N  Override the rolling window. Default: 30.
  -v / --verbose   Debug logging.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from scripts.lib import shards as shards_module
from scripts.lib.canonical import load_canonical
from scripts.lib.utils import parse_to_iso, utc_now_iso

logger = logging.getLogger("build_indexes")

REPO_ROOT = Path(__file__).resolve().parent.parent

# Tunable constants. The defaults aim for "fast page load on mobile":
# per-entity files cap at ~50KB each at the 500-item ceiling.
WINDOW_DAYS = 30                    # rolling window for per-entity indexes
MAX_ITEMS_PER_ENTITY = 500          # hard cap per player/team file
FEED_WINDOW_DAYS = 7                # homepage stream window
MAX_FEED_ITEMS = 1000               # homepage stream cap
TRENDING_LIMIT = 40                 # top-N for trending.json
TRENDING_WINDOW_HOURS = 72          # only score items from the last 3 days

# DESIGN.md 7.7: score = source_weight / (hours + 2) ** 1.5. Long-form
# (youtube, substack) weighted slightly above raw feeds (bluesky, reddit)
# because the long-form work tends to be more durable signal. No
# engagement data on most sources, so weight + recency is the signal.
SOURCE_WEIGHTS = {
    "youtube": 1.3,
    "substack": 1.3,
    "google-news": 1.1,
    "bluesky": 1.0,
    "reddit": 1.0,
}

# Source dirs we read. Mirrors SHARD_FORMAT.md's source enum.
SOURCES = ("bluesky", "google-news", "reddit", "substack", "youtube")

# Index output paths (computed from shards.DATA_DIR at call time so
# tests can monkeypatch the data dir).
def _index_root() -> Path:
    return shards_module.DATA_DIR / "index"


# ---------------------------------------------------------------------------
# Shard reading
# ---------------------------------------------------------------------------


def _iter_shard_files(data_dir: Path) -> Iterable[Path]:
    """Yield every hot-tier shard file under data/{source}/*.json."""
    for source in SOURCES:
        source_dir = data_dir / source
        if not source_dir.is_dir():
            continue
        for path in sorted(source_dir.glob("*.json")):
            # Files are YYYY-MM-DD.json; skip anything else (manifest
            # files, hidden, etc.) defensively.
            if not path.stem[:4].isdigit():
                continue
            yield path


def load_all_items(data_dir: Optional[Path] = None) -> List[dict]:
    """Read every hot-tier shard, dedupe by item id, return sorted desc.

    Items missing `id` or `published_at` are dropped silently (logged
    at DEBUG) — the validator catches these at write time, so anything
    arriving here that's malformed is a bug worth investigating but not
    a reason to abort the index build.
    """
    data_dir = data_dir or shards_module.DATA_DIR
    seen: Dict[str, dict] = {}
    for path in _iter_shard_files(data_dir):
        with path.open(encoding="utf-8") as f:
            blob = json.load(f)
        for item in blob.get("items", []) or []:
            item_id = item.get("id")
            published = item.get("published_at")
            if not item_id or not published:
                logger.debug("dropping malformed item in %s: %r", path, item)
                continue
            if item_id in seen:
                # First occurrence wins; duplicates across shards
                # shouldn't happen but if they do we don't double-count.
                continue
            seen[item_id] = item
    items = list(seen.values())
    items.sort(key=lambda it: it.get("published_at", ""), reverse=True)
    return items


# ---------------------------------------------------------------------------
# Compact item shape
# ---------------------------------------------------------------------------


def _compact_item(item: dict) -> dict:
    """Strip a full shard item down to the fields the frontend renders.

    Drops the engagement block (all-null on Reddit, partial elsewhere
    and not used in v1), the matched_query debug field, the
    google_url duplicate, the ingested_at internal timestamp, and the
    media duration that doesn't exist anyway. Keeps the players/teams
    arrays so cards can render clickable tags for OTHER entities.
    """
    author = item.get("author") or {}
    return {
        "id": item["id"],
        "source": item["source"],
        "published_at": item["published_at"],
        "title": item.get("title", ""),
        "url": item.get("url", ""),
        "author": author.get("display_name") or author.get("handle") or "",
        "thumbnail": item.get("thumbnail"),
        "body_excerpt": item.get("body_excerpt"),
        "players": list(item.get("players") or []),
        "teams": list(item.get("teams") or []),
    }


# ---------------------------------------------------------------------------
# Window filter
# ---------------------------------------------------------------------------


def _within_window(item_iso: str, cutoff_iso: str) -> bool:
    return item_iso >= cutoff_iso


def _cutoff_iso(days: int, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return parse_to_iso(now - timedelta(days=days))


# ---------------------------------------------------------------------------
# Per-entity index builds
# ---------------------------------------------------------------------------


def _group_by_entity(
    items: List[dict],
    field: str,
    cutoff_iso: str,
) -> Dict[str, List[dict]]:
    """Bucket items by their `players` or `teams` slugs.

    Items outside the window are excluded. Each slug's bucket is
    descending by published_at (input is already sorted that way, so
    bucket order is preserved). Buckets are capped at
    MAX_ITEMS_PER_ENTITY.
    """
    buckets: Dict[str, List[dict]] = {}
    for item in items:
        if not _within_window(item["published_at"], cutoff_iso):
            continue
        for slug in item.get(field) or []:
            bucket = buckets.setdefault(slug, [])
            if len(bucket) < MAX_ITEMS_PER_ENTITY:
                bucket.append(item)
    return buckets


def build_entity_indexes(
    items: List[dict],
    canonical_map: Dict[str, dict],
    field: str,
    cutoff_iso: str,
    generated_at: str,
) -> Dict[str, dict]:
    """Build the player or team index files (in memory).

    `field` is `"players"` or `"teams"`. Only slugs present in
    `canonical_map` AND with ≥1 in-window item produce a file —
    unknown slugs (tagger drift, manual shard edits) are skipped with
    a debug log.
    """
    buckets = _group_by_entity(items, field, cutoff_iso)
    out: Dict[str, dict] = {}
    for slug, bucket in buckets.items():
        info = canonical_map.get(slug)
        if not info:
            logger.debug("skipping unknown %s slug: %s", field, slug)
            continue
        out[slug] = {
            "slug": slug,
            "name": str(info["name"]),
            "generated_at": generated_at,
            "count": len(bucket),
            "items": [_compact_item(it) for it in bucket],
        }
    return out


# ---------------------------------------------------------------------------
# Trending
# ---------------------------------------------------------------------------


def _score(item: dict, now: datetime) -> float:
    """`source_weight / (hours_since_publish + 2) ** 1.5` per DESIGN.md 7.7."""
    weight = SOURCE_WEIGHTS.get(item.get("source", ""), 1.0)
    try:
        pub = datetime.strptime(item["published_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (ValueError, KeyError):
        return 0.0
    hours = max(0.0, (now - pub).total_seconds() / 3600.0)
    return weight / ((hours + 2.0) ** 1.5)


def build_trending(
    items: List[dict],
    generated_at: str,
    now: Optional[datetime] = None,
    limit: Optional[int] = None,
    window_hours: Optional[int] = None,
) -> dict:
    """Top-N items across all sources, scored by recency + source weight."""
    if limit is None:
        limit = TRENDING_LIMIT
    if window_hours is None:
        window_hours = TRENDING_WINDOW_HOURS
    now = now or datetime.now(timezone.utc)
    cutoff = parse_to_iso(now - timedelta(hours=window_hours))
    candidates = [it for it in items if it.get("published_at", "") >= cutoff]
    scored: List[Tuple[float, dict]] = [
        (_score(it, now), it) for it in candidates
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    top = scored[:limit]
    return {
        "generated_at": generated_at,
        "window_hours": window_hours,
        "weights": SOURCE_WEIGHTS,
        "count": len(top),
        "items": [
            {**_compact_item(it), "trending_score": round(s, 4)}
            for s, it in top
        ],
    }


# ---------------------------------------------------------------------------
# Feed
# ---------------------------------------------------------------------------


def build_feed(
    items: List[dict],
    generated_at: str,
    window_days: Optional[int] = None,
    cap: Optional[int] = None,
) -> dict:
    """Merged recent stream — all sources, newest-first, capped."""
    if window_days is None:
        window_days = FEED_WINDOW_DAYS
    if cap is None:
        cap = MAX_FEED_ITEMS
    cutoff = _cutoff_iso(window_days)
    windowed = [it for it in items if it.get("published_at", "") >= cutoff]
    capped = windowed[:cap]
    return {
        "generated_at": generated_at,
        "window_days": window_days,
        "count": len(capped),
        "items": [_compact_item(it) for it in capped],
    }


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def build_manifest(
    items: List[dict],
    player_indexes: Dict[str, dict],
    team_indexes: Dict[str, dict],
    window_days: int,
    generated_at: str,
) -> dict:
    """Slugs with content (sorted desc by count) + source histogram."""
    source_counts: Dict[str, int] = {}
    for item in items:
        src = item.get("source", "")
        source_counts[src] = source_counts.get(src, 0) + 1

    def _entries(idx: Dict[str, dict]) -> List[dict]:
        rows = [
            {"slug": idx_entry["slug"], "name": idx_entry["name"], "count": idx_entry["count"]}
            for idx_entry in idx.values()
        ]
        rows.sort(key=lambda r: (-r["count"], r["slug"]))
        return rows

    return {
        "generated_at": generated_at,
        "window_days": window_days,
        "total_items": len(items),
        "sources": dict(sorted(source_counts.items())),
        "players": _entries(player_indexes),
        "teams": _entries(team_indexes),
    }


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _clear_dir(path: Path) -> None:
    """Remove a directory tree if it exists. Used to keep entity dirs clean.

    We rebuild every cycle, so stale files from a previous build (e.g. a
    player who dropped out of the window) shouldn't linger. Trending and
    feed and manifest files are overwritten in place, but per-entity
    files would otherwise leak.
    """
    if path.exists():
        shutil.rmtree(path)


def write_all_indexes(
    player_indexes: Dict[str, dict],
    team_indexes: Dict[str, dict],
    trending: dict,
    feed: dict,
    manifest: dict,
    index_root: Optional[Path] = None,
) -> None:
    root = index_root or _index_root()
    root.mkdir(parents=True, exist_ok=True)

    players_dir = root / "players"
    teams_dir = root / "teams"
    _clear_dir(players_dir)
    _clear_dir(teams_dir)
    for slug, blob in player_indexes.items():
        _write_json(players_dir / f"{slug}.json", blob)
    for slug, blob in team_indexes.items():
        _write_json(teams_dir / f"{slug}.json", blob)

    _write_json(root / "trending.json", trending)
    _write_json(root / "feed.json", feed)
    _write_json(root / "manifest.json", manifest)


# ---------------------------------------------------------------------------
# Build orchestration
# ---------------------------------------------------------------------------


def build_indexes(
    window_days: int = WINDOW_DAYS,
    data_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Tuple[dict, dict, dict, dict, dict]:
    """Read all shards and build the five index blobs in memory.

    Returns `(player_indexes, team_indexes, trending, feed, manifest)`.
    Tests use this directly; the CLI persists the result.
    """
    data_dir = data_dir or shards_module.DATA_DIR
    items = load_all_items(data_dir)
    players_dict, teams_dict = load_canonical()

    now = now or datetime.now(timezone.utc)
    generated_at = parse_to_iso(now)
    entity_cutoff = parse_to_iso(now - timedelta(days=window_days))

    player_indexes = build_entity_indexes(
        items, players_dict, "players", entity_cutoff, generated_at
    )
    team_indexes = build_entity_indexes(
        items, teams_dict, "teams", entity_cutoff, generated_at
    )
    trending = build_trending(items, generated_at, now=now)
    feed = build_feed(items, generated_at)
    manifest = build_manifest(
        items, player_indexes, team_indexes, window_days, generated_at
    )
    return player_indexes, team_indexes, trending, feed, manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Rebuild data/index/ from the hot-tier shards."
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute + log counts; don't write any index files.",
    )
    p.add_argument(
        "--window-days",
        type=int,
        default=WINDOW_DAYS,
        help=f"Rolling window for per-entity indexes. Default: {WINDOW_DAYS}.",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose logging."
    )
    return p


def run(argv: Optional[List[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    start = time.perf_counter()
    try:
        player_indexes, team_indexes, trending, feed, manifest = build_indexes(
            window_days=args.window_days
        )
    except FileNotFoundError as exc:
        logger.error("missing required input: %s", exc)
        return 1

    total = manifest["total_items"]
    if total == 0:
        logger.error("no shard items found; nothing to index")
        return 1

    elapsed = time.perf_counter() - start
    logger.info(
        "indexed %d items in %.2fs: %d player files, %d team files, "
        "%d trending, %d in feed, window_days=%d",
        total,
        elapsed,
        len(player_indexes),
        len(team_indexes),
        trending["count"],
        feed["count"],
        args.window_days,
    )

    if args.dry_run:
        print(f"DRY RUN — would write to {_index_root()}")
        print(f"  total items processed: {total}")
        print(f"  player indexes: {len(player_indexes)}")
        print(f"  team indexes: {len(team_indexes)}")
        print(f"  trending items: {trending['count']}")
        print(f"  feed items: {feed['count']}")
        top_players = manifest["players"][:5]
        if top_players:
            print(f"  top players: " + ", ".join(
                f"{p['name']} ({p['count']})" for p in top_players
            ))
        top_teams = manifest["teams"][:5]
        if top_teams:
            print(f"  top teams: " + ", ".join(
                f"{t['name']} ({t['count']})" for t in top_teams
            ))
        return 0

    write_all_indexes(player_indexes, team_indexes, trending, feed, manifest)
    return 0


if __name__ == "__main__":
    sys.exit(run())
