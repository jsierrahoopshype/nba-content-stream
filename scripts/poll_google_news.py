"""Poll Google News RSS and append to today's shard.

Pipeline:
  1. Load query config (`data/sources/google_news_queries.json`) and
     rotation state (`data/sources/google_news_state.json`).
  2. Build the query list for this cycle:
       topic_queries (run every cycle)
       + a rotating subset of canonical players (`players_per_cycle`)
       + a rotating subset of canonical teams (`teams_per_cycle`)
     The rotation is the trick that lets us cover every player/team
     without firing one request per entity every 15 min — see the
     module-level note below.
  3. For each query, fetch the RSS via feedparser. Per-query failures
     are logged and skipped; the run continues.
  4. For each entry: split `headline - Publisher` on the LAST ` - `,
     gate on the publisher whitelist (fuzzy substring match), filter
     by `--since-hours`, detect canonical entities in the HEADLINE,
     and build the SHARD_FORMAT.md item.
  5. Dedup across all queries by `sha1(normalized_headline | publisher)`
     — Google News surfaces the same article under multiple queries
     (LeBron + Lakers + "NBA trade rumors") with different redirect
     URLs each time, so URL-based dedup isn't enough.
  6. Append to today's shard via `shards.append_items`.
  7. Advance the rotation cursors and save state.

The rotation: with ~39 canonical players and `players_per_cycle=25`,
every player is queried roughly every other 15-min cycle. With 30 teams
and `teams_per_cycle=10`, every team every ~3 cycles. As the canonical
player list grows toward ~375, this stretches to ~4-hour coverage per
player. Google News is a *slow backstop* — Bluesky and Reddit catch
breaking news faster. The topic queries (5 of them) run every cycle
and carry the freshness load.

Google redirect URL limitation: links are
`news.google.com/rss/articles/CBM...` redirects. We DO NOT make
per-item HTTP requests to resolve these — too slow, too rate-limit-y.
We do extract the first `<a href>` from the RSS description as a
best-effort real URL when present, and always keep the Google URL in a
separate `google_url` field. If extraction fails, `url` falls back to
the Google URL.

CLI:
  --since-hours N    Only keep entries published in the last N hours.
                     Default: 24.
  --query Q          Run a single ad-hoc query, skip rotation. Debug aid.
  --no-rotation      Run only topic_queries; skip entity rotation.
                     Useful for a fast smoke test.
  --max-per-query N  Cap on entries pulled per query feed. Default: 50.
  --dry-run          Print what would be appended; don't touch the shard.
  -v / --verbose     Debug logging.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

import feedparser
import requests

from scripts.lib import shards
from scripts.lib.canonical import detect_entities, load_canonical
from scripts.lib.shards import append_items, validate_item
from scripts.lib.utils import parse_to_iso, today_utc_date, utc_now_iso

logger = logging.getLogger("poll_google_news")

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_SOURCES_DIR = REPO_ROOT / "data" / "sources"
QUERIES_PATH = DATA_SOURCES_DIR / "google_news_queries.json"
STATE_PATH = DATA_SOURCES_DIR / "google_news_state.json"

GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"
INTER_QUERY_SLEEP_SEC = 0.5  # be polite to Google News
DEFAULT_USER_AGENT = "nba-content-stream/0.1 (+poll_google_news)"


# ---------------------------------------------------------------------------
# Config + rotation state
# ---------------------------------------------------------------------------


def _strip_meta(blob: dict) -> dict:
    return {k: v for k, v in blob.items() if not k.startswith("_")}


def load_query_config(path: Path = QUERIES_PATH) -> dict:
    """Load and lightly validate the query config JSON."""
    with path.open(encoding="utf-8") as f:
        blob = json.load(f)
    config = _strip_meta(blob)
    for key in ("topic_queries", "entity_queries", "publisher_whitelist"):
        if key not in config:
            raise ValueError(f"google_news_queries.json missing '{key}'")
    return config


def load_rotation_state(path: Path = STATE_PATH) -> Tuple[int, int]:
    """Return `(player_cursor, team_cursor)`. Missing file → (0, 0)."""
    if not path.exists():
        return 0, 0
    with path.open(encoding="utf-8") as f:
        blob = json.load(f)
    return int(blob.get("player_cursor", 0)), int(blob.get("team_cursor", 0))


def save_rotation_state(
    path: Path, player_cursor: int, team_cursor: int
) -> None:
    """Write `(player_cursor, team_cursor)`, preserving the `_meta` block."""
    blob: dict
    if path.exists():
        with path.open(encoding="utf-8") as f:
            blob = json.load(f)
    else:
        blob = {}
    blob["player_cursor"] = player_cursor
    blob["team_cursor"] = team_cursor
    with path.open("w", encoding="utf-8") as f:
        json.dump(blob, f, indent=2, ensure_ascii=False)
        f.write("\n")


def select_rotation_slice(
    items: List[str], cursor: int, count: int
) -> Tuple[List[str], int]:
    """Take `count` items starting at `cursor`, wrapping around the list.

    Returns `(selected, next_cursor)`. `next_cursor` is `(cursor + count) %
    len(items)`. If `count >= len(items)`, returns everything (deduped,
    in source order) and advances the cursor by `count` mod len.
    """
    if not items:
        return [], 0
    n = len(items)
    if count >= n:
        return list(items), (cursor + count) % n
    cursor = cursor % n
    end = cursor + count
    if end <= n:
        selected = items[cursor:end]
    else:
        selected = items[cursor:] + items[: end - n]
    return selected, end % n


# ---------------------------------------------------------------------------
# Query list assembly
# ---------------------------------------------------------------------------


def _canonical_names(canonical_dict: dict) -> List[str]:
    """Return canonical display names in dict-insertion order."""
    return [str(v["name"]) for v in canonical_dict.values()]


def build_query_list(
    config: dict,
    players_dict: dict,
    teams_dict: dict,
    player_cursor: int,
    team_cursor: int,
    no_rotation: bool,
) -> Tuple[List[str], int, int]:
    """Return `(queries, next_player_cursor, next_team_cursor)`.

    Topic queries always run. Entity queries are skipped if
    `no_rotation` is True or if `entity_queries.enabled` is False.
    """
    queries: List[str] = list(config["topic_queries"])
    next_player_cursor = player_cursor
    next_team_cursor = team_cursor

    eq = config.get("entity_queries", {}) or {}
    if no_rotation or not eq.get("enabled", False):
        return queries, next_player_cursor, next_team_cursor

    players = _canonical_names(players_dict)
    teams = _canonical_names(teams_dict)

    player_slice, next_player_cursor = select_rotation_slice(
        players, player_cursor, int(eq.get("players_per_cycle", 0))
    )
    team_slice, next_team_cursor = select_rotation_slice(
        teams, team_cursor, int(eq.get("teams_per_cycle", 0))
    )
    queries.extend(player_slice)
    queries.extend(team_slice)
    return queries, next_player_cursor, next_team_cursor


# ---------------------------------------------------------------------------
# Publisher whitelist
# ---------------------------------------------------------------------------


def _flatten_whitelist(wl: dict) -> List[str]:
    return list(wl.get("tier1", [])) + list(wl.get("tier2", []))


def publisher_allowed(publisher: str, whitelist: dict) -> bool:
    """Fuzzy substring match in either direction, case-insensitive.

    "ESPN.com" matches "ESPN" (whitelist entry is substring of publisher).
    "ESPN" matches "ESPN" (exact).
    Conservative on purpose: we'd rather drop a marginal source than
    let an SEO farm slip through. Jorge can grow the list when he sees
    real output.
    """
    if not publisher:
        return False
    pub_lower = publisher.lower().strip()
    if not pub_lower:
        return False
    for entry in _flatten_whitelist(whitelist):
        e = entry.lower().strip()
        if not e:
            continue
        if e in pub_lower or pub_lower in e:
            return True
    return False


# ---------------------------------------------------------------------------
# Title + description parsing
# ---------------------------------------------------------------------------


_TITLE_SPLIT = " - "


def split_headline_publisher(title: str) -> Tuple[str, str]:
    """Split `headline - Publisher` on the LAST ` - `.

    Google News always appends ` - Publisher` to the entry title. The
    headline itself may contain ` - ` (e.g. "Lakers - and Celtics -
    headed for collision") so we split on the rightmost occurrence.
    Returns `(headline, publisher)`. If the title has no ` - ` separator,
    returns `(title, "")` and lets the publisher come from
    `entry.source.title` if present.
    """
    if not title:
        return "", ""
    idx = title.rfind(_TITLE_SPLIT)
    if idx < 0:
        return title.strip(), ""
    headline = title[:idx].strip()
    publisher = title[idx + len(_TITLE_SPLIT) :].strip()
    return headline, publisher


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_HREF_RE = re.compile(r'<a\s+[^>]*href=["\']([^"\']+)["\']', re.IGNORECASE)


def strip_html(text: str) -> str:
    """Strip HTML tags and collapse whitespace. HTML entities decoded."""
    if not text:
        return ""
    no_tags = _TAG_RE.sub(" ", text)
    decoded = unescape(no_tags)
    return _WS_RE.sub(" ", decoded).strip()


def extract_real_url(description_html: str) -> Optional[str]:
    """Best-effort: the first `<a href>` in the description.

    Google News descriptions usually contain an anchor pointing at the
    publisher article. When present we use it as the canonical `url`;
    when absent we fall back to the Google redirect URL. We never make
    a follow-up HTTP request — too slow at scale, and Google rate-limits.
    """
    if not description_html:
        return None
    m = _HREF_RE.search(description_html)
    if not m:
        return None
    url = m.group(1).strip()
    if not url:
        return None
    # Skip anchors that just bounce back to news.google.com (no real win).
    if "news.google.com" in url:
        return None
    return url


# ---------------------------------------------------------------------------
# Dedup id
# ---------------------------------------------------------------------------


def _normalize_for_dedup(s: str) -> str:
    """Lowercase, strip, collapse whitespace. Conservative on purpose."""
    return _WS_RE.sub(" ", s.lower().strip())


def make_item_id(headline: str, publisher: str) -> str:
    """`gn-{first 16 hex chars of sha1(normalized_headline | publisher)}`.

    Same article surfaced under multiple queries (LeBron + Lakers +
    "NBA trade rumors") gets the same id — that's the whole point.
    The 16-char truncation gives 2^64 codepoints, plenty for the
    article volume we'll ever see.
    """
    key = f"{_normalize_for_dedup(headline)}|{_normalize_for_dedup(publisher)}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"gn-{digest}"


# ---------------------------------------------------------------------------
# Mapping an entry to a shard item
# ---------------------------------------------------------------------------


def _entry_published_iso(entry) -> Optional[str]:
    """Return the entry's publication time as ISO 8601 UTC, or None."""
    published = entry.get("published") or entry.get("updated")
    if not published:
        return None
    try:
        return parse_to_iso(published)
    except Exception:
        return None


def _entry_publisher(entry, headline_publisher: str) -> str:
    """Prefer the title-derived publisher; fall back to the `<source>` tag."""
    if headline_publisher:
        return headline_publisher
    source = entry.get("source") or {}
    if isinstance(source, dict):
        return source.get("title") or ""
    return ""


def map_entry_to_item(
    entry,
    matched_query: str,
    players_dict,
    teams_dict,
    ingested_at: Optional[str] = None,
) -> Optional[dict]:
    """Build a SHARD_FORMAT.md item from one feedparser entry.

    Returns None if the entry is unusable (no title or no published_at).
    """
    title = entry.get("title") or ""
    headline, title_publisher = split_headline_publisher(title)
    publisher = _entry_publisher(entry, title_publisher)
    if not headline:
        return None

    published_at = _entry_published_iso(entry)
    if not published_at:
        return None

    google_url = entry.get("link") or ""
    description_html = entry.get("summary") or entry.get("description") or ""
    real_url = extract_real_url(description_html)
    url = real_url or google_url
    body_excerpt = strip_html(description_html)[:280] if description_html else ""

    player_slugs, team_slugs = detect_entities(headline, players_dict, teams_dict)

    item: dict = {
        "id": make_item_id(headline, publisher),
        "source": "google-news",
        "published_at": published_at,
        "ingested_at": ingested_at or utc_now_iso(),
        "url": url,
        "google_url": google_url,
        "title": headline,
        "author": {
            "handle": publisher,
            "display_name": publisher,
            "url": None,
        },
        "media": {"type": "text"},
        "players": player_slugs,
        "teams": team_slugs,
        "matched_query": matched_query,
    }
    if body_excerpt:
        item["body_excerpt"] = body_excerpt
    return item


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


FeedFetcher = Callable[[str, int], list]


def fetch_query(session: requests.Session, query: str, limit: int) -> list:
    """Fetch the Google News RSS for `query`, return feedparser entries.

    Raises on HTTP error (caller logs and skips that query).
    """
    params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    resp = session.get(GOOGLE_NEWS_RSS_URL, params=params, timeout=15)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"feedparser couldn't parse feed: {parsed.bozo_exception}")
    return list(parsed.entries[:limit])


# ---------------------------------------------------------------------------
# Collect
# ---------------------------------------------------------------------------


def _within_since(item_iso: str, since_iso: Optional[str]) -> bool:
    if since_iso is None:
        return True
    return item_iso >= since_iso


def collect_items(
    queries: List[str],
    feed_fetcher: FeedFetcher,
    whitelist: dict,
    players_dict,
    teams_dict,
    since_iso: Optional[str],
    max_per_query: int,
    sleep_sec: float = INTER_QUERY_SLEEP_SEC,
) -> Tuple[List[dict], Dict[str, int]]:
    """Run all queries, filter + dedup, return (items, stats).

    `feed_fetcher(query, limit)` returns feedparser entries. Injected so
    tests can avoid the network.
    """
    stats = {
        "queries": len(queries),
        "entries_seen": 0,
        "dropped_whitelist": 0,
        "dropped_since": 0,
        "deduped": 0,
        "kept": 0,
        "query_errors": 0,
    }
    items_by_id: Dict[str, dict] = {}
    ingested_at = utc_now_iso()

    for i, query in enumerate(queries):
        if i > 0 and sleep_sec > 0:
            time.sleep(sleep_sec)
        try:
            entries = feed_fetcher(query, max_per_query)
        except Exception as exc:
            stats["query_errors"] += 1
            logger.warning("query %r failed: %s", query, exc)
            continue

        kept_for_query = 0
        for entry in entries:
            stats["entries_seen"] += 1
            item = map_entry_to_item(
                entry, query, players_dict, teams_dict, ingested_at
            )
            if item is None:
                continue
            publisher = item["author"]["handle"]
            if not publisher_allowed(publisher, whitelist):
                stats["dropped_whitelist"] += 1
                continue
            if not _within_since(item["published_at"], since_iso):
                stats["dropped_since"] += 1
                continue
            errs = validate_item(item)
            if errs:
                logger.warning(
                    "dropping invalid item %s from %r: %s",
                    item.get("id"),
                    query,
                    errs,
                )
                continue
            if item["id"] in items_by_id:
                stats["deduped"] += 1
                # Keep the earlier-query attribution; don't overwrite.
                continue
            items_by_id[item["id"]] = item
            kept_for_query += 1

        logger.debug("query %r → kept %d", query, kept_for_query)
        stats["kept"] += kept_for_query

    return list(items_by_id.values()), stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_since_iso(hours: int) -> str:
    return parse_to_iso(datetime.now(timezone.utc) - timedelta(hours=hours))


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Poll Google News RSS into today's shard."
    )
    p.add_argument(
        "--since-hours",
        type=int,
        default=24,
        help="Only keep entries published in the last N hours. Default: 24.",
    )
    p.add_argument(
        "--query",
        default=None,
        help="Run a single ad-hoc query; skips topic + rotation. Debug aid.",
    )
    p.add_argument(
        "--no-rotation",
        action="store_true",
        help="Only run topic_queries; skip the entity rotation. Faster.",
    )
    p.add_argument(
        "--max-per-query",
        type=int,
        default=50,
        help="Cap entries pulled per query feed. Default: 50.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print items that would be written; don't touch the shard.",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose logging."
    )
    return p


def _make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
    return session


def run(
    argv: Optional[List[str]] = None,
    session: Optional[requests.Session] = None,
    feed_fetcher: Optional[FeedFetcher] = None,
    queries_path: Path = QUERIES_PATH,
    state_path: Path = STATE_PATH,
    sleep_sec: Optional[float] = None,
) -> int:
    """Entry point. Tests pass `feed_fetcher` and `sleep_sec=0` to bypass IO."""
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = load_query_config(queries_path)
    except Exception as exc:
        logger.error("cannot load query config: %s", exc)
        return 1

    players_dict, teams_dict = load_canonical()

    if args.query:
        queries = [args.query]
        next_player_cursor = next_team_cursor = None  # don't touch state
    else:
        player_cursor, team_cursor = load_rotation_state(state_path)
        queries, next_player_cursor, next_team_cursor = build_query_list(
            config,
            players_dict,
            teams_dict,
            player_cursor,
            team_cursor,
            args.no_rotation,
        )

    if not queries:
        logger.error("no queries to run (empty config?)")
        return 1

    since_iso = _default_since_iso(args.since_hours)
    logger.info(
        "running %d queries, since=%s (%dh), max_per_query=%d",
        len(queries),
        since_iso,
        args.since_hours,
        args.max_per_query,
    )

    if feed_fetcher is None:
        session = session or _make_session()

        def feed_fetcher(query: str, limit: int):  # noqa: F811
            return fetch_query(session, query, limit)

    effective_sleep = sleep_sec if sleep_sec is not None else INTER_QUERY_SLEEP_SEC
    items, stats = collect_items(
        queries,
        feed_fetcher,
        config["publisher_whitelist"],
        players_dict,
        teams_dict,
        since_iso,
        args.max_per_query,
        effective_sleep,
    )
    logger.info("stats: %s", stats)

    if stats["query_errors"] == stats["queries"] and stats["queries"] > 0:
        logger.error("every query failed; not advancing rotation cursor")
        return 1

    if args.dry_run:
        print(f"DRY RUN — would append {len(items)} items to today's shard.")
        for item in items[:5]:
            tags = []
            if item["players"]:
                tags.append(f"players={','.join(item['players'])}")
            if item["teams"]:
                tags.append(f"teams={','.join(item['teams'])}")
            tag_str = (" [" + " ".join(tags) + "]") if tags else ""
            print(
                f"  - {item['id']} {item['published_at']} "
                f"{item['author']['handle']}: {item['title'][:80]}{tag_str}"
            )
        if len(items) > 5:
            print(f"  ... and {len(items) - 5} more")
        # Don't advance rotation on dry-run — that would shift coverage.
        return 0

    # Group items by their UTC publish date; append per shard.
    items_by_date: Dict[str, List[dict]] = {}
    for item in items:
        date = item["published_at"][:10]
        items_by_date.setdefault(date, []).append(item)

    total_appended = 0
    for date, batch in sorted(items_by_date.items()):
        appended = append_items("google-news", date, batch)
        logger.info(
            "appended %d new items to data/google-news/%s.json", appended, date
        )
        total_appended += appended
    if not items_by_date:
        # Touch today's shard so re-runs don't re-fetch as much (no-op currently).
        logger.info("no items kept this cycle")

    if next_player_cursor is not None:
        save_rotation_state(state_path, next_player_cursor, next_team_cursor)
        logger.info(
            "rotation cursors saved: player=%d team=%d",
            next_player_cursor,
            next_team_cursor,
        )

    return 0


if __name__ == "__main__":
    sys.exit(run())
