"""Poll Bluesky reporter feeds and append to today's shard.

Pipeline:
  1. Load the effective reporter list = `cdechoch/nba-buzz` CSV ∪ local
     overrides (`data/sources/bluesky_overrides.json`).
  2. For each reporter, call `app.bsky.feed.getAuthorFeed` on the
     unauthenticated public AppView (`public.api.bsky.app`) with
     `filter=posts_no_replies`. Use the DID as the actor when present
     (stable across handle changes); fall back to the handle otherwise.
  3. Drop reposts (`reason.$type == reasonRepost`); keep top-level posts
     and quote-posts.
  4. Map each post to a shard item per `docs/SHARD_FORMAT.md` and append
     to `data/bluesky/{today-utc}.json` via `shards.append_items`.

CLI (--since and --since-hours are mutually exclusive):
  --since ISO     Only keep posts whose record.createdAt >= this UTC
                  timestamp.
  --since-hours N Only keep posts published in the last N hours.
                  Default when neither flag is given: 24.
  --limit N       Max posts to fetch per reporter (AppView caps at 100).
                  Default: 50.
  --dry-run       Print the items that would be written, don't touch
                  the shard.
  --reporter H    Restrict to a single reporter handle (debugging).

The public AppView lexicon response is the standard
`app.bsky.feed.defs#feedViewPost`, so field names are camelCase: e.g.
`post.author.displayName`, `record.createdAt`, `embed.$type`.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

from scripts.lib import shards
from scripts.lib.canonical import detect_entities, load_canonical
from scripts.lib.shards import append_items, validate_item
from scripts.lib.sources import (
    SourcesError,
    load_effective_records,
    parse_bluesky_csv,
)
from scripts.lib.utils import parse_to_iso, today_utc_date, utc_now_iso

logger = logging.getLogger("poll_bluesky")

REPO_ROOT = Path(__file__).resolve().parent.parent
OVERRIDES_PATH = REPO_ROOT / "data" / "sources" / "bluesky_overrides.json"

LIVE_LIST_URL = (
    "https://huggingface.co/spaces/cdechoch/nba-buzz/raw/main/bluesky_handles.csv"
)

# Unauthenticated public AppView. The atproto.Client default
# (bsky.social) requires auth and returns 401 on these reads, which is
# what bit us on the first dry-run.
APPVIEW_BASE_URL = "https://public.api.bsky.app"
GET_AUTHOR_FEED_PATH = "/xrpc/app.bsky.feed.getAuthorFeed"


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def _embed_type(node: dict) -> str:
    """Return the `$type` of a record/embed/reason node, or empty string."""
    if not isinstance(node, dict):
        return ""
    return node.get("$type", "") or ""


def _has_repost_reason(feed_view: dict) -> bool:
    """True if the feed view represents a repost (drop these).

    A `reasonRepost` means the actor reshared someone else's post; we
    only want original posts and quote-posts from the reporter.
    """
    reason = feed_view.get("reason")
    return "reasonRepost" in _embed_type(reason)


def _is_reply(feed_view: dict) -> bool:
    """True if the post is a reply.

    Defensive: we already ask the server for `posts_no_replies`, but a
    `record.reply` field unambiguously identifies replies if anything
    slips through.
    """
    record = feed_view.get("post", {}).get("record", {})
    return isinstance(record, dict) and record.get("reply") is not None


def should_include(feed_view: dict) -> bool:
    """Apply the Bluesky shard filter: top-level posts and quote-posts only."""
    if _has_repost_reason(feed_view):
        return False
    if _is_reply(feed_view):
        return False
    return True


# ---------------------------------------------------------------------------
# Mapping a Bluesky post to a shard item
# ---------------------------------------------------------------------------


def _is_quote_post(post: dict) -> bool:
    """True if the post embeds another record (quote-post)."""
    return "app.bsky.embed.record" in _embed_type(post.get("embed"))


def _has_image_embed(post: dict) -> bool:
    return "app.bsky.embed.images" in _embed_type(post.get("embed"))


def _media_type(post: dict) -> str:
    """`image` if the post has image attachments, else `text`."""
    if _has_image_embed(post):
        return "image"
    return "text"


def _at_uri_to_id(uri: str) -> str:
    """Map `at://did:plc:.../app.bsky.feed.post/rkey` to `bs-<urlencoded>`.

    Per SHARD_FORMAT.md the Bluesky id is `bs-{at_uri_path}` URL-encoded.
    The "path" is everything after the `at://` scheme prefix.
    """
    if uri.startswith("at://"):
        path = uri[len("at://") :]
    else:
        path = uri
    return f"bs-{quote(path, safe='')}"


def _rkey_from_uri(uri: str) -> str:
    """Return the rkey (last segment) of an AT URI."""
    return uri.rsplit("/", 1)[-1]


def _public_url(handle: str, uri: str) -> str:
    """Build the public bsky.app URL for a post."""
    return f"https://bsky.app/profile/{handle}/post/{_rkey_from_uri(uri)}"


def map_post_to_item(
    feed_view: dict,
    reporter: Dict[str, Any],
    players_dict,
    teams_dict,
    ingested_at: Optional[str] = None,
) -> dict:
    """Build a SHARD_FORMAT.md item from one `feedViewPost`.

    `reporter` is the effective-list record (handle/display_name/did, with
    fields possibly missing for override-added handles). We prefer the
    Bluesky-supplied author profile on the post itself for handle and
    display_name, since it reflects current state, and fall back to the
    reporter record when that's unavailable.
    """
    post = feed_view["post"]
    record = post.get("record", {}) or {}
    author = post.get("author", {}) or {}

    handle = author.get("handle") or reporter.get("handle") or ""
    display_name = (
        author.get("displayName") or reporter.get("display_name") or handle
    )

    text = record.get("text") or ""
    created_at = record.get("createdAt") or post.get("indexedAt")
    if created_at is None:
        raise ValueError("post is missing both record.createdAt and post.indexedAt")
    published_at = parse_to_iso(created_at)

    player_slugs, team_slugs = detect_entities(text, players_dict, teams_dict)

    title = text.split("\n", 1)[0][:280] if text else "(no text)"

    item: dict = {
        "id": _at_uri_to_id(post["uri"]),
        "source": "bluesky",
        "published_at": published_at,
        "ingested_at": ingested_at or utc_now_iso(),
        "url": _public_url(handle, post["uri"]),
        "title": title,
        "author": {
            "handle": handle,
            "display_name": display_name,
            "url": f"https://bsky.app/profile/{handle}",
        },
        "body_excerpt": text,
        "media": {"type": _media_type(post)},
        "engagement": {
            "likes": post.get("likeCount"),
            "reposts": post.get("repostCount"),
        },
        "players": player_slugs,
        "teams": team_slugs,
    }
    if _is_quote_post(post):
        item["is_quote_post"] = True
    return item


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


FeedFetcher = Callable[[str, int], List[dict]]


def fetch_author_feed(
    session: requests.Session, actor: str, limit: int
) -> List[dict]:
    """Call `app.bsky.feed.getAuthorFeed` on the public AppView.

    Returns the `feed` array (list of feedViewPost dicts). Raises
    `requests.HTTPError` on 4xx/5xx — the caller logs and skips that
    reporter rather than aborting the run.
    """
    url = APPVIEW_BASE_URL + GET_AUTHOR_FEED_PATH
    params = {"actor": actor, "filter": "posts_no_replies", "limit": str(limit)}
    resp = session.get(url, params=params, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    feed = payload.get("feed", [])
    if not isinstance(feed, list):
        raise ValueError(f"unexpected feed payload shape from {url}")
    return feed


# ---------------------------------------------------------------------------
# Collect
# ---------------------------------------------------------------------------


def _actor_for(reporter: Dict[str, Any]) -> str:
    """Prefer DID (stable) over handle (mutable).

    A DID is considered usable if it starts with `did:` — guards against
    blank strings and obviously-malformed values. Override-added
    reporters have no DID and fall through to the handle.
    """
    did = reporter.get("did")
    if isinstance(did, str) and did.startswith("did:"):
        return did
    return reporter["handle"]


def _within_since(item_iso: str, since_iso: Optional[str]) -> bool:
    if since_iso is None:
        return True
    return item_iso >= since_iso


def collect_items(
    reporters: List[Dict[str, Any]],
    feed_fetcher: FeedFetcher,
    players_dict,
    teams_dict,
    since_iso: Optional[str],
    limit: int,
) -> Tuple[List[dict], Dict[str, int]]:
    """Iterate reporters → posts → mapped items. Returns (items, stats).

    `feed_fetcher(actor, limit)` returns an iterable of feedViewPost
    dicts. Injected so tests can avoid the live network. Any exception
    from the fetcher (HTTPError, ConnectionError, ValueError, ...) is
    logged once and the reporter is skipped — one bad reporter doesn't
    kill the run.
    """
    stats = {
        "reporters": len(reporters),
        "posts_seen": 0,
        "dropped_filter": 0,
        "dropped_since": 0,
        "kept": 0,
        "fetch_errors": 0,
    }
    items: List[dict] = []
    ingested_at = utc_now_iso()

    for reporter in reporters:
        actor = _actor_for(reporter)
        try:
            feed = feed_fetcher(actor, limit)
        except Exception as exc:
            stats["fetch_errors"] += 1
            logger.warning(
                "getAuthorFeed failed for %s (%s): %s",
                reporter.get("handle"),
                actor,
                exc,
            )
            continue

        for fv in feed:
            stats["posts_seen"] += 1
            if not should_include(fv):
                stats["dropped_filter"] += 1
                continue
            try:
                item = map_post_to_item(
                    fv, reporter, players_dict, teams_dict, ingested_at
                )
            except Exception as exc:
                logger.warning(
                    "skipping unmappable post from %s: %s",
                    reporter.get("handle"),
                    exc,
                )
                continue
            if not _within_since(item["published_at"], since_iso):
                stats["dropped_since"] += 1
                continue
            errs = validate_item(item)
            if errs:
                logger.warning(
                    "dropping invalid item %s from %s: %s",
                    item.get("id"),
                    reporter.get("handle"),
                    errs,
                )
                continue
            items.append(item)
            stats["kept"] += 1
    return items, stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_since_iso(hours: int = 24) -> str:
    """`hours` before now, ISO 8601 UTC with Z suffix.

    Matches `poll_google_news._default_since_iso` and
    `poll_reddit._default_since_iso` so the cutoff semantics are
    identical across all three pollers.
    """
    return parse_to_iso(datetime.now(timezone.utc) - timedelta(hours=hours))


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Poll Bluesky reporters into today's shard."
    )
    # --since (ISO timestamp) and --since-hours (int N) are two ways to
    # spell the same cutoff. The other pollers (Google News, Reddit) only
    # accept --since-hours, and the GH Actions workflow calls all three
    # the same way; --since stays for backward compat and ad-hoc local use.
    since_group = p.add_mutually_exclusive_group()
    since_group.add_argument(
        "--since",
        default=None,
        help="Only keep posts at or after this UTC ISO timestamp. "
        "Mutually exclusive with --since-hours.",
    )
    since_group.add_argument(
        "--since-hours",
        type=int,
        default=None,
        help="Only keep posts published in the last N hours. "
        "Mutually exclusive with --since. Default when neither is given: 24.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max posts per reporter (AppView cap is 100). Default: 50.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print items that would be written; don't touch the shard.",
    )
    p.add_argument(
        "--reporter",
        default=None,
        help="Restrict polling to a single reporter handle (for debugging).",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose logging."
    )
    return p


def _make_session() -> requests.Session:
    """A bare requests.Session — no auth, no cookies, just a UA."""
    session = requests.Session()
    session.headers.update({"User-Agent": "nba-content-stream/0.1 (+poll_bluesky)"})
    return session


def run(
    argv: Optional[List[str]] = None,
    session: Optional[requests.Session] = None,
    feed_fetcher: Optional[FeedFetcher] = None,
) -> int:
    """Entry point. Tests pass `feed_fetcher` to bypass the network."""
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        reporters = load_effective_records(
            "bluesky", LIVE_LIST_URL, parse_bluesky_csv, OVERRIDES_PATH
        )
    except SourcesError as exc:
        logger.error("cannot proceed: %s", exc)
        return 2

    if args.reporter:
        reporters = [r for r in reporters if r.get("handle") == args.reporter]
        if not reporters:
            logger.error("no reporter matches --reporter %s", args.reporter)
            return 2

    if args.since is not None:
        since_iso = parse_to_iso(args.since)
    elif args.since_hours is not None:
        since_iso = _default_since_iso(args.since_hours)
    else:
        since_iso = _default_since_iso(24)
    logger.info(
        "polling %d reporters, since=%s, limit=%d",
        len(reporters),
        since_iso,
        args.limit,
    )

    players_dict, teams_dict = load_canonical()

    if feed_fetcher is None:
        session = session or _make_session()

        def feed_fetcher(actor: str, limit: int) -> List[dict]:  # noqa: F811
            return fetch_author_feed(session, actor, limit)

    items, stats = collect_items(
        reporters, feed_fetcher, players_dict, teams_dict, since_iso, args.limit
    )
    logger.info("stats: %s", stats)

    if args.dry_run:
        print(f"DRY RUN — would append {len(items)} items to today's shard.")
        for item in items[:5]:
            print(
                f"  - {item['id']} {item['published_at']} "
                f"{item['author']['handle']}: {item['title'][:80]}"
            )
        if len(items) > 5:
            print(f"  ... and {len(items) - 5} more")
        return 0

    date = today_utc_date()
    appended = append_items("bluesky", date, items)
    logger.info("appended %d new items to data/bluesky/%s.json", appended, date)
    return 0


if __name__ == "__main__":
    sys.exit(run())
