"""Poll Substack publications via public RSS, append to today's shard.

DESIGN NOTE: Substack is a RAW feed source. No AI, no Gemini, no
extraction block. DESIGN.md originally specced Gemini extraction here
but that was cancelled — Jorge's constraint is no paid AI in the
ingest path. Each item is title + author/publication + publish date +
short HTML-stripped excerpt + link + free-tier regex entity tags.
That's it.

COPYRIGHT POSTURE (matches Reddit): Substack post bodies are
copyrighted. We store ONLY a short excerpt capped at 280 chars,
stripped of HTML, and always link back to the original post. No full
body content in the shard. Paid posts only emit a preview in RSS
anyway; for free posts we deliberately truncate.

Pipeline:
  1. Load publications config (data/sources/substack_publications.json).
  2. For each publication, GET its RSS feed via requests Session,
     parse with feedparser. Per-pub failures logged and skipped.
  3. ~0.5s polite sleep between publications.
  4. For each entry: filter by --since-hours, build the item, detect
     canonical entities in title + excerpt (more text helps tagging).
  5. Dedup by item id (publication slug + post slug or hashed link).
  6. Group by UTC publish date, append via shards.append_items.

CLI:
  --since-hours N    Only keep entries published in the last N hours.
                     Default: 24. (Substack posts are infrequent; for
                     a local smoke-test use 168 = 7 days.)
  --publication SLUG Poll a single publication (debug aid).
  --max-per-feed N   Cap entries per feed. Default: 30.
  --dry-run          Print items that would be appended; no shard write.
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
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import feedparser
import requests

from scripts.lib import shards
from scripts.lib.canonical import detect_entities, load_canonical
from scripts.lib.shards import append_items, validate_item
from scripts.lib.utils import parse_to_iso, strip_html, today_utc_date, utc_now_iso

logger = logging.getLogger("poll_substack")

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "data" / "sources" / "substack_publications.json"

INTER_FEED_SLEEP_SEC = 0.5
EXCERPT_MAX_CHARS = 280
DEFAULT_USER_AGENT = (
    "nba-content-stream/0.1 (HoopsMatic; "
    "+https://github.com/jsierrahoopshype/nba-content-stream)"
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _strip_meta(blob: dict) -> dict:
    return {k: v for k, v in blob.items() if not k.startswith("_")}


def load_config(path: Path = CONFIG_PATH) -> dict:
    """Load + lightly validate `substack_publications.json`."""
    with path.open(encoding="utf-8") as f:
        blob = json.load(f)
    config = _strip_meta(blob)
    if "publications" not in config:
        raise ValueError("substack_publications.json missing 'publications'")
    pubs = config["publications"]
    if not isinstance(pubs, list) or not pubs:
        raise ValueError("'publications' must be a non-empty list")
    for pub in pubs:
        for key in ("slug", "name", "feed"):
            if key not in pub:
                raise ValueError(
                    f"publication entry missing '{key}': {pub!r}"
                )
    return config


# ---------------------------------------------------------------------------
# Item id + excerpt
# ---------------------------------------------------------------------------


_POST_SLUG_RE = re.compile(r"/p/([a-z0-9][a-z0-9\-]*)", re.IGNORECASE)


def _post_slug_from_link(link: str) -> Optional[str]:
    """Substack URLs look like https://{sub}.substack.com/p/{slug}.

    Returns the `{slug}` segment, lowercased, or None if no /p/ segment.
    """
    if not link:
        return None
    m = _POST_SLUG_RE.search(link)
    if not m:
        return None
    return m.group(1).lower()


def _hash_link(link: str) -> str:
    """16-char hex of sha1(link). Stable fallback id source."""
    return hashlib.sha1(link.encode("utf-8")).hexdigest()[:16]


def make_item_id(publication_slug: str, entry) -> Optional[str]:
    """Build `ss-{publication_slug}-{post_id}`.

    Prefers the URL's /p/{post-slug} segment because it's human-readable
    and stable per post. Falls back to a sha1 hash of the link when the
    URL doesn't expose a post slug. Returns None if there's no link at
    all (the caller drops the entry).
    """
    link = entry.get("link") or ""
    if not link:
        return None
    post_slug = _post_slug_from_link(link)
    if post_slug:
        return f"ss-{publication_slug}-{post_slug}"
    return f"ss-{publication_slug}-{_hash_link(link)}"


def _entry_excerpt(entry) -> str:
    """Pull the best plain-text excerpt out of an entry's HTML payload.

    Substack RSS uses `summary` and/or `content` (a list of {value: html}).
    We prefer `content[0].value` when present (it's the fuller payload),
    fall back to `summary`. Either way we strip HTML and cap at
    EXCERPT_MAX_CHARS.
    """
    contents = entry.get("content")
    html = ""
    if isinstance(contents, list) and contents:
        first = contents[0]
        if isinstance(first, dict):
            html = first.get("value") or ""
    if not html:
        html = entry.get("summary") or entry.get("description") or ""
    if not html:
        return ""
    text = strip_html(html)
    if len(text) <= EXCERPT_MAX_CHARS:
        return text
    cut = text[: EXCERPT_MAX_CHARS]
    space = cut.rfind(" ")
    if space > EXCERPT_MAX_CHARS * 0.6:
        cut = cut[:space]
    return cut.rstrip() + "…"


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _entry_published_iso(entry) -> Optional[str]:
    raw = entry.get("published") or entry.get("updated")
    if not raw:
        return None
    try:
        return parse_to_iso(raw)
    except Exception:
        return None


def map_entry_to_item(
    entry,
    publication: dict,
    players_dict,
    teams_dict,
    ingested_at: Optional[str] = None,
) -> Optional[dict]:
    """Build a SHARD_FORMAT.md item from one Substack RSS entry.

    Returns None if the entry can't be safely mapped — missing link,
    missing title, or missing publish time.
    """
    title = entry.get("title") or ""
    if not title:
        return None
    item_id = make_item_id(publication["slug"], entry)
    if not item_id:
        return None
    published_at = _entry_published_iso(entry)
    if not published_at:
        return None

    link = entry.get("link") or ""
    excerpt = _entry_excerpt(entry)

    # Tag from title + excerpt (excerpt is short; combining boosts recall
    # on, e.g., "Wemby's blocks vs. Jokic" headlines where the excerpt
    # mentions the teams).
    detect_text = f"{title}\n{excerpt}" if excerpt else title
    player_slugs, team_slugs = detect_entities(detect_text, players_dict, teams_dict)

    item: dict = {
        "id": item_id,
        "source": "substack",
        "published_at": published_at,
        "ingested_at": ingested_at or utc_now_iso(),
        "url": link,
        "title": title.strip(),
        "author": {
            "handle": publication["slug"],
            "display_name": publication["name"],
            "url": f"https://{publication['slug']}.substack.com",
        },
        "media": {"type": "text"},
        "players": player_slugs,
        "teams": team_slugs,
    }
    if excerpt:
        item["body_excerpt"] = excerpt
    return item


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


FeedFetcher = Callable[[dict, int], list]


def fetch_feed(
    session: requests.Session, publication: dict, limit: int
) -> list:
    """GET a publication's RSS, return up to `limit` feedparser entries.

    Raises on HTTP error; caller logs and continues with the next pub.
    """
    resp = session.get(publication["feed"], timeout=15)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        raise ValueError(
            f"feedparser couldn't parse {publication['feed']}: "
            f"{parsed.bozo_exception}"
        )
    return list(parsed.entries[:limit])


# ---------------------------------------------------------------------------
# Collect
# ---------------------------------------------------------------------------


def _within_since(item_iso: str, since_iso: Optional[str]) -> bool:
    if since_iso is None:
        return True
    return item_iso >= since_iso


def collect_items(
    publications: List[dict],
    feed_fetcher: FeedFetcher,
    players_dict,
    teams_dict,
    since_iso: Optional[str],
    max_per_feed: int,
    sleep_sec: float = INTER_FEED_SLEEP_SEC,
) -> Tuple[List[dict], Dict[str, int]]:
    """Iterate publications, filter + dedup, return (items, stats)."""
    stats = {
        "publications": len(publications),
        "entries_seen": 0,
        "dropped_since": 0,
        "deduped": 0,
        "kept": 0,
        "feed_errors": 0,
    }
    items_by_id: Dict[str, dict] = {}
    ingested_at = utc_now_iso()

    for i, publication in enumerate(publications):
        if i > 0 and sleep_sec > 0:
            time.sleep(sleep_sec)
        try:
            entries = feed_fetcher(publication, max_per_feed)
        except Exception as exc:
            stats["feed_errors"] += 1
            logger.warning(
                "substack feed %s failed: %s", publication.get("slug"), exc
            )
            continue

        kept_for_pub = 0
        for entry in entries:
            stats["entries_seen"] += 1
            item = map_entry_to_item(
                entry, publication, players_dict, teams_dict, ingested_at
            )
            if item is None:
                continue
            if not _within_since(item["published_at"], since_iso):
                stats["dropped_since"] += 1
                continue
            errs = validate_item(item)
            if errs:
                logger.warning(
                    "dropping invalid item %s from %s: %s",
                    item.get("id"),
                    publication.get("slug"),
                    errs,
                )
                continue
            if item["id"] in items_by_id:
                stats["deduped"] += 1
                continue
            items_by_id[item["id"]] = item
            kept_for_pub += 1

        logger.debug(
            "substack %s → kept %d", publication.get("slug"), kept_for_pub
        )
        stats["kept"] += kept_for_pub

    return list(items_by_id.values()), stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_since_iso(hours: int) -> str:
    """`hours` before now, ISO 8601 UTC with Z suffix.

    Same shape as the helpers in poll_bluesky / poll_google_news /
    poll_reddit so cutoff semantics are identical across pollers.
    """
    return parse_to_iso(datetime.now(timezone.utc) - timedelta(hours=hours))


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Poll Substack publications into today's shard."
    )
    p.add_argument(
        "--since-hours",
        type=int,
        default=24,
        help="Only keep entries published in the last N hours. Default: 24.",
    )
    p.add_argument(
        "--publication",
        default=None,
        help="Restrict polling to a single publication slug (debug aid).",
    )
    p.add_argument(
        "--max-per-feed",
        type=int,
        default=30,
        help="Cap entries per feed. Default: 30.",
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
    config_path: Path = CONFIG_PATH,
    sleep_sec: Optional[float] = None,
) -> int:
    """Entry point. Tests pass `feed_fetcher` and `sleep_sec=0` to bypass IO."""
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = load_config(config_path)
    except Exception as exc:
        logger.error("cannot load substack config: %s", exc)
        return 1

    publications = config["publications"]
    if args.publication:
        publications = [p for p in publications if p["slug"] == args.publication]
        if not publications:
            logger.error("no publication matches --publication %s", args.publication)
            return 1

    players_dict, teams_dict = load_canonical()
    since_iso = _default_since_iso(args.since_hours)
    logger.info(
        "polling %d publication(s), since=%s (%dh), max_per_feed=%d",
        len(publications),
        since_iso,
        args.since_hours,
        args.max_per_feed,
    )

    if feed_fetcher is None:
        session = session or _make_session()

        def feed_fetcher(pub: dict, limit: int):  # noqa: F811
            return fetch_feed(session, pub, limit)

    effective_sleep = sleep_sec if sleep_sec is not None else INTER_FEED_SLEEP_SEC
    items, stats = collect_items(
        publications,
        feed_fetcher,
        players_dict,
        teams_dict,
        since_iso,
        args.max_per_feed,
        effective_sleep,
    )
    logger.info("stats: %s", stats)

    if stats["feed_errors"] == stats["publications"] and stats["publications"] > 0:
        logger.error("every substack feed failed; aborting without write")
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
            excerpt = item.get("body_excerpt", "")
            print(
                f"  - {item['id']} {item['published_at']} "
                f"{item['author']['handle']}: {item['title'][:80]}{tag_str}"
            )
            print(f"      {item['url']}")
            if excerpt:
                print(f"      excerpt: {excerpt[:80]}…")
        if len(items) > 5:
            print(f"  ... and {len(items) - 5} more")
        return 0

    items_by_date: Dict[str, List[dict]] = {}
    for item in items:
        date = item["published_at"][:10]
        items_by_date.setdefault(date, []).append(item)

    for date, batch in sorted(items_by_date.items()):
        appended = append_items("substack", date, batch)
        logger.info(
            "appended %d new items to data/substack/%s.json", appended, date
        )

    if not items_by_date:
        logger.info("no items kept this cycle")

    return 0


if __name__ == "__main__":
    sys.exit(run())
