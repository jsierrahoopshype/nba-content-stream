"""Poll public Reddit RSS feeds and append to today's shard.

Source mechanics (per DESIGN.md 4.4):
  - r/nba only for v1.
  - Public RSS/Atom: `https://www.reddit.com/r/nba/top/.rss?t=day` is the
    quality gate. `hot/.rss` adds freshness coverage. NO OAuth, NO API
    key, NO authenticated endpoints.
  - Reddit requires a descriptive User-Agent or returns 429. Set one,
    use a `requests.Session`, retry once on 429/403 with a short
    backoff, and bail cleanly if that doesn't recover.

Privacy / copyright rules (also from DESIGN.md 4.4):
  - `url` MUST be the Reddit thread URL (the /comments/<id>/ link),
    never a deep-linked external article. Users click through to Reddit.
  - `body_excerpt` is only the original poster's selftext, capped at
    `excerpt_max_chars` (280). Link posts (no selftext) omit it
    entirely. We never read or store comment text.
  - Reddit wraps selftext in `<!-- SC_OFF --> ... <!-- SC_ON -->`; we
    extract between those markers so the "[link] [comments] submitted
    by /u/..." boilerplate doesn't leak into excerpts.

Pipeline:
  1. Load config (`data/sources/reddit_config.json`).
  2. For each subreddit × feed combo, GET the RSS via `requests` with
     the right UA, handle 429/403 with one retry, parse with feedparser.
  3. Per-feed failures logged and skipped. If every feed fails, exit 1.
  4. Polite sleep (~1s) between feeds — Reddit is stricter than Google.
  5. For each entry: extract post id (the `t3_xxxxx` fullname), filter
     by `--since-hours`, build the item, detect canonical entities in
     the TITLE.
  6. Dedup across feeds by post id (the same thread can appear in both
     top and hot).
  7. Group by UTC publish date, append via `shards.append_items`.

CLI:
  --since-hours N    Only keep entries published in the last N hours.
                     Default: 24.
  --subreddit S      Override the subreddit list (debug aid).
  --feed F           Run a single feed, e.g. "top/.rss?t=day" (debug).
  --max-per-feed N   Cap entries per feed. Default: 50.
  --dry-run          Print items that would be appended; no shard write.
  -v / --verbose     Debug logging.
"""

from __future__ import annotations

import argparse
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

logger = logging.getLogger("poll_reddit")

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "data" / "sources" / "reddit_config.json"

REDDIT_BASE_URL = "https://www.reddit.com"
INTER_FEED_SLEEP_SEC = 1.0  # Reddit is stricter than Google about pace
RATE_LIMIT_BACKOFF_SEC = 5.0  # single retry on 429/403

# Reddit demands a descriptive User-Agent or it returns 429 outright.
# Format follows their published guidance: "<platform>/<version> (<info>)".
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
    """Load + lightly validate `reddit_config.json`."""
    with path.open(encoding="utf-8") as f:
        blob = json.load(f)
    config = _strip_meta(blob)
    for key in ("subreddits", "feeds", "excerpt_max_chars"):
        if key not in config:
            raise ValueError(f"reddit_config.json missing '{key}'")
    if not config["subreddits"] or not config["feeds"]:
        raise ValueError("reddit_config.json: subreddits and feeds must be non-empty")
    return config


# ---------------------------------------------------------------------------
# HTML / selftext extraction
# ---------------------------------------------------------------------------


# Reddit wraps selftext between these comment markers (SC = SelfContent).
# When the markers are absent the post is a link post (no selftext).
_SC_RE = re.compile(r"<!--\s*SC_OFF\s*-->(.*?)<!--\s*SC_ON\s*-->", re.DOTALL)


def extract_selftext(content_html: str) -> Optional[str]:
    """Return the post's selftext as plaintext, or None for link posts.

    Reddit RSS wraps selftext between `<!-- SC_OFF -->` and `<!-- SC_ON
    -->`. If those markers are missing, the post is a link post (or has
    empty selftext) — return None and the caller omits `body_excerpt`
    entirely. This protects against the "submitted by /u/... [link]
    [comments]" boilerplate leaking into excerpts.
    """
    if not content_html:
        return None
    m = _SC_RE.search(content_html)
    if not m:
        return None
    body = strip_html(m.group(1))
    return body or None


def cap_excerpt(text: str, max_chars: int) -> str:
    """Truncate to `max_chars`, trimming at the last word boundary."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    # Prefer cutting at a space so we don't slice a word in half.
    space = cut.rfind(" ")
    if space > max_chars * 0.6:  # avoid over-aggressive trim
        cut = cut[:space]
    return cut.rstrip() + "…"


# ---------------------------------------------------------------------------
# Reddit id + author handling
# ---------------------------------------------------------------------------


_T3_RE = re.compile(r"\bt3_[a-z0-9]+\b", re.IGNORECASE)


def extract_post_id(entry) -> Optional[str]:
    """Return the Reddit `t3_<base36>` fullname for an entry, or None.

    feedparser sets `entry.id` to `t3_xxxxx` for Reddit RSS in the
    versions we use. If that ever changes (e.g. Reddit migrates to a
    `tag:reddit.com,...` URI) we fall back to scanning the id string
    for a `t3_` pattern, then the comments URL.

    We keep the `t3_` prefix in the shard id. It's Reddit's canonical
    fullname format and is what their API expects if we ever need to
    cross-reference. Item id becomes `rd-t3_xxxxx`.
    """
    raw = entry.get("id") or ""
    if raw.startswith("t3_"):
        return raw
    m = _T3_RE.search(raw)
    if m:
        return m.group(0)
    link = entry.get("link") or ""
    # Reddit thread URLs look like /r/<sub>/comments/<base36>/<slug>/
    parts = link.split("/comments/", 1)
    if len(parts) == 2:
        rest = parts[1].split("/", 1)[0]
        if rest:
            return f"t3_{rest}"
    return None


def extract_username(entry) -> str:
    """Return the bare reddit username, or '[deleted]' if absent.

    feedparser surfaces `entry.author` as `/u/<name>`; strip the slash.
    """
    raw = entry.get("author") or ""
    if not raw:
        return "[deleted]"
    name = raw.strip()
    if name.startswith("/u/"):
        name = name[len("/u/") :]
    elif name.startswith("u/"):
        name = name[len("u/") :]
    return name or "[deleted]"


def user_profile_url(username: str) -> Optional[str]:
    """Return the canonical user profile URL, or None for deleted users."""
    if not username or username == "[deleted]":
        return None
    return f"https://www.reddit.com/user/{username}"


# ---------------------------------------------------------------------------
# Thread URL guard
# ---------------------------------------------------------------------------


_REDDIT_THREAD_RE = re.compile(
    r"^https?://(www\.|old\.|new\.)?reddit\.com/r/[^/]+/comments/[a-z0-9]+",
    re.IGNORECASE,
)


def is_reddit_thread_url(url: str) -> bool:
    """True if `url` is a Reddit comments-thread URL (the only kind we store)."""
    if not url:
        return False
    return bool(_REDDIT_THREAD_RE.match(url))


def normalize_thread_url(url: str, post_id: Optional[str], subreddit: str) -> Optional[str]:
    """Coerce to a canonical reddit thread URL.

    If `url` is already a Reddit thread link, return it as-is. Otherwise
    (e.g. some feeds set `link` to an external article URL for link
    posts), reconstruct the thread URL from the subreddit + post id.
    Returns None if we can't produce a thread URL — the caller skips
    the entry rather than storing an external link, per DESIGN.md 4.4.
    """
    if is_reddit_thread_url(url):
        return url
    if post_id and post_id.startswith("t3_") and subreddit:
        base36 = post_id[len("t3_") :]
        return f"https://www.reddit.com/r/{subreddit}/comments/{base36}/"
    return None


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


def _entry_content_html(entry) -> str:
    """Best-effort: the html body of the entry."""
    contents = entry.get("content")
    if isinstance(contents, list) and contents:
        first = contents[0]
        if isinstance(first, dict):
            return first.get("value") or ""
    return entry.get("summary") or entry.get("description") or ""


def map_entry_to_item(
    entry,
    subreddit: str,
    players_dict,
    teams_dict,
    excerpt_max_chars: int,
    ingested_at: Optional[str] = None,
) -> Optional[dict]:
    """Build a SHARD_FORMAT.md item from one Reddit RSS entry.

    Returns None if the entry can't be mapped safely — missing post id,
    missing publish time, or no derivable Reddit thread URL.
    """
    post_id = extract_post_id(entry)
    if not post_id:
        return None

    published_at = _entry_published_iso(entry)
    if not published_at:
        return None

    raw_link = entry.get("link") or ""
    thread_url = normalize_thread_url(raw_link, post_id, subreddit)
    if not thread_url:
        # No safe thread URL → drop. Never store a deep external link.
        return None

    title = entry.get("title") or ""
    if not title:
        return None

    username = extract_username(entry)

    content_html = _entry_content_html(entry)
    selftext = extract_selftext(content_html)

    player_slugs, team_slugs = detect_entities(title, players_dict, teams_dict)

    item: dict = {
        "id": f"rd-{post_id}",
        "source": "reddit",
        "published_at": published_at,
        "ingested_at": ingested_at or utc_now_iso(),
        "url": thread_url,
        "title": title,
        "author": {
            "handle": username,
            "display_name": username,
            "url": user_profile_url(username),
        },
        "media": {"type": "text"},
        "engagement": {
            "likes": None,
            "reposts": None,
            "comments": None,
            "score": None,
            "views": None,
        },
        "players": player_slugs,
        "teams": team_slugs,
    }
    if selftext:
        item["body_excerpt"] = cap_excerpt(selftext, excerpt_max_chars)
    return item


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


FeedFetcher = Callable[[str, str, int], list]  # (subreddit, feed_path, limit)


def _feed_url(subreddit: str, feed_path: str) -> str:
    return f"{REDDIT_BASE_URL}/r/{subreddit}/{feed_path}"


def fetch_feed(
    session: requests.Session,
    subreddit: str,
    feed_path: str,
    limit: int,
    backoff_sec: float = RATE_LIMIT_BACKOFF_SEC,
) -> list:
    """GET a Reddit RSS feed, retry once on 429/403, return feedparser entries.

    The single retry is the right size: Reddit's rate-limit windows are
    short (~1s for unauth), but DC IPs sometimes get a hard 429 that
    no amount of waiting will fix. Retry once, then surface the error
    so the caller logs + skips the feed and the cycle continues.
    """
    url = _feed_url(subreddit, feed_path)
    for attempt in (1, 2):
        resp = session.get(url, timeout=15)
        if resp.status_code in (429, 403):
            logger.warning(
                "reddit %s/%s returned %d on attempt %d",
                subreddit,
                feed_path,
                resp.status_code,
                attempt,
            )
            if attempt == 1:
                time.sleep(backoff_sec)
                continue
            resp.raise_for_status()
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        if parsed.bozo and not parsed.entries:
            raise ValueError(
                f"feedparser couldn't parse {url}: {parsed.bozo_exception}"
            )
        return list(parsed.entries[:limit])
    # Unreachable — raise_for_status above triggers on the second pass.
    raise RuntimeError("fetch_feed exited the retry loop without returning")


# ---------------------------------------------------------------------------
# Collect
# ---------------------------------------------------------------------------


def _within_since(item_iso: str, since_iso: Optional[str]) -> bool:
    if since_iso is None:
        return True
    return item_iso >= since_iso


def collect_items(
    subreddits: List[str],
    feeds: List[str],
    feed_fetcher: FeedFetcher,
    players_dict,
    teams_dict,
    since_iso: Optional[str],
    max_per_feed: int,
    excerpt_max_chars: int,
    sleep_sec: float = INTER_FEED_SLEEP_SEC,
) -> Tuple[List[dict], Dict[str, int]]:
    """Run all (subreddit, feed) pairs, filter + dedup, return (items, stats).

    `feed_fetcher(subreddit, feed_path, limit)` returns feedparser entries.
    Injected so tests bypass the network.
    """
    pairs = [(sub, feed) for sub in subreddits for feed in feeds]
    stats = {
        "feeds": len(pairs),
        "entries_seen": 0,
        "dropped_since": 0,
        "deduped": 0,
        "kept": 0,
        "feed_errors": 0,
    }
    items_by_id: Dict[str, dict] = {}
    ingested_at = utc_now_iso()

    for i, (subreddit, feed_path) in enumerate(pairs):
        if i > 0 and sleep_sec > 0:
            time.sleep(sleep_sec)
        try:
            entries = feed_fetcher(subreddit, feed_path, max_per_feed)
        except Exception as exc:
            stats["feed_errors"] += 1
            logger.warning(
                "reddit feed %s/%s failed: %s", subreddit, feed_path, exc
            )
            continue

        kept_for_feed = 0
        for entry in entries:
            stats["entries_seen"] += 1
            item = map_entry_to_item(
                entry,
                subreddit,
                players_dict,
                teams_dict,
                excerpt_max_chars,
                ingested_at,
            )
            if item is None:
                continue
            if not _within_since(item["published_at"], since_iso):
                stats["dropped_since"] += 1
                continue
            errs = validate_item(item)
            if errs:
                logger.warning(
                    "dropping invalid item %s from %s/%s: %s",
                    item.get("id"),
                    subreddit,
                    feed_path,
                    errs,
                )
                continue
            if item["id"] in items_by_id:
                stats["deduped"] += 1
                continue
            items_by_id[item["id"]] = item
            kept_for_feed += 1

        logger.debug(
            "reddit %s/%s → kept %d", subreddit, feed_path, kept_for_feed
        )
        stats["kept"] += kept_for_feed

    return list(items_by_id.values()), stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_since_iso(hours: int) -> str:
    return parse_to_iso(datetime.now(timezone.utc) - timedelta(hours=hours))


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Poll Reddit RSS feeds into today's shard."
    )
    p.add_argument(
        "--since-hours",
        type=int,
        default=24,
        help="Only keep entries published in the last N hours. Default: 24.",
    )
    p.add_argument(
        "--subreddit",
        default=None,
        help="Override config: run against a single subreddit (debug aid).",
    )
    p.add_argument(
        "--feed",
        default=None,
        help="Override config: run a single feed (e.g. 'top/.rss?t=day').",
    )
    p.add_argument(
        "--max-per-feed",
        type=int,
        default=50,
        help="Cap entries per feed. Default: 50.",
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
        logger.error("cannot load reddit config: %s", exc)
        return 1

    subreddits = [args.subreddit] if args.subreddit else config["subreddits"]
    feeds = [args.feed] if args.feed else config["feeds"]
    excerpt_max_chars = int(config["excerpt_max_chars"])

    players_dict, teams_dict = load_canonical()
    since_iso = _default_since_iso(args.since_hours)
    logger.info(
        "polling %d feed(s) across %d subreddit(s), since=%s (%dh), max_per_feed=%d",
        len(feeds),
        len(subreddits),
        since_iso,
        args.since_hours,
        args.max_per_feed,
    )

    if feed_fetcher is None:
        session = session or _make_session()

        def feed_fetcher(subreddit: str, feed_path: str, limit: int):  # noqa: F811
            return fetch_feed(session, subreddit, feed_path, limit)

    effective_sleep = sleep_sec if sleep_sec is not None else INTER_FEED_SLEEP_SEC
    items, stats = collect_items(
        subreddits,
        feeds,
        feed_fetcher,
        players_dict,
        teams_dict,
        since_iso,
        args.max_per_feed,
        excerpt_max_chars,
        effective_sleep,
    )
    logger.info("stats: %s", stats)

    if stats["feed_errors"] == stats["feeds"] and stats["feeds"] > 0:
        logger.error("every reddit feed failed; aborting without write")
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
            excerpt_str = f"  excerpt: {excerpt[:60]}…" if excerpt else "  (link post)"
            print(
                f"  - {item['id']} {item['published_at']} "
                f"u/{item['author']['handle']}: {item['title'][:80]}{tag_str}"
            )
            print(f"      {item['url']}")
            print(f"    {excerpt_str}")
        if len(items) > 5:
            print(f"  ... and {len(items) - 5} more")
        return 0

    items_by_date: Dict[str, List[dict]] = {}
    for item in items:
        date = item["published_at"][:10]
        items_by_date.setdefault(date, []).append(item)

    for date, batch in sorted(items_by_date.items()):
        appended = append_items("reddit", date, batch)
        logger.info(
            "appended %d new items to data/reddit/%s.json", appended, date
        )

    if not items_by_date:
        logger.info("no items kept this cycle")

    return 0


if __name__ == "__main__":
    sys.exit(run())
