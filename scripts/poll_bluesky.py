"""Poll Bluesky reporter feeds and append to today's shard.

Pipeline:
  1. Load the effective reporter list = `cdechoch/nba-buzz` CSV ∪ local
     overrides (`data/sources/bluesky_overrides.json`).
  2. For each reporter, call `app.bsky.feed.getAuthorFeed` with
     `filter=posts_no_replies`. Use the DID as the actor when present
     (stable across handle changes); fall back to the handle otherwise.
  3. Drop reposts (`reason` is `reasonRepost`); keep top-level posts
     and quote-posts.
  4. Map each post to a shard item per `docs/SHARD_FORMAT.md` and append
     to `data/bluesky/{today-utc}.json` via `shards.append_items`.

CLI:
  --since ISO     Only keep posts whose record.created_at >= this UTC
                  timestamp. Default: 24h ago.
  --limit N       Max posts to fetch per reporter (atproto caps at 100).
                  Default: 50.
  --dry-run       Print the items that would be written, don't touch
                  the shard.
  --reporter H    Restrict to a single reporter handle (debugging).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

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


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def _has_repost_reason(feed_view) -> bool:
    """True if the feed view represents a repost (drop these).

    A `reasonRepost` means the actor reshared someone else's post; we
    only want original posts and quote-posts from the reporter.
    """
    reason = getattr(feed_view, "reason", None)
    if reason is None:
        return False
    py_type = getattr(reason, "py_type", "") or ""
    return "reasonRepost" in py_type


def _is_reply(feed_view) -> bool:
    """True if the post is a reply.

    Defensive: we already ask the server for `posts_no_replies`, but a
    `record.reply` field unambiguously identifies replies if anything
    slips through (e.g. an override-added reporter on a different
    code path).
    """
    record = getattr(feed_view.post, "record", None)
    if record is None:
        return False
    return getattr(record, "reply", None) is not None


def should_include(feed_view) -> bool:
    """Apply the Bluesky shard filter: top-level posts and quote-posts only."""
    if _has_repost_reason(feed_view):
        return False
    if _is_reply(feed_view):
        return False
    return True


# ---------------------------------------------------------------------------
# Mapping a Bluesky post to a shard item
# ---------------------------------------------------------------------------


def _is_quote_post(post) -> bool:
    """True if the post embeds another record (quote-post)."""
    embed = getattr(post, "embed", None)
    if embed is None:
        return False
    py_type = getattr(embed, "py_type", "") or ""
    return "app.bsky.embed.record" in py_type


def _has_image_embed(post) -> bool:
    embed = getattr(post, "embed", None)
    if embed is None:
        return False
    py_type = getattr(embed, "py_type", "") or ""
    return "app.bsky.embed.images" in py_type


def _media_type(post) -> str:
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
        path = uri[len("at://"):]
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
    feed_view,
    reporter: Dict[str, Any],
    players_dict,
    teams_dict,
    ingested_at: Optional[str] = None,
) -> dict:
    """Build a SHARD_FORMAT.md item from one `FeedViewPost`.

    `reporter` is the effective-list record (handle/display_name/did, with
    fields possibly missing for override-added handles). We prefer the
    Bluesky-supplied author profile on the post itself for handle and
    display_name, since it reflects current state, and fall back to the
    reporter record when that's unavailable.
    """
    post = feed_view.post
    record = post.record
    author = post.author

    handle = (
        getattr(author, "handle", None)
        or reporter.get("handle")
        or ""
    )
    display_name = (
        getattr(author, "display_name", None)
        or reporter.get("display_name")
        or handle
    )

    text = getattr(record, "text", "") or ""
    created_at = getattr(record, "created_at", None) or post.indexed_at
    published_at = parse_to_iso(created_at)

    player_slugs, team_slugs = detect_entities(text, players_dict, teams_dict)

    title = text.split("\n", 1)[0][:280] if text else "(no text)"

    item: dict = {
        "id": _at_uri_to_id(post.uri),
        "source": "bluesky",
        "published_at": published_at,
        "ingested_at": ingested_at or utc_now_iso(),
        "url": _public_url(handle, post.uri),
        "title": title,
        "author": {
            "handle": handle,
            "display_name": display_name,
            "url": f"https://bsky.app/profile/{handle}",
        },
        "body_excerpt": text,
        "media": {"type": _media_type(post)},
        "engagement": {
            "likes": getattr(post, "like_count", None),
            "reposts": getattr(post, "repost_count", None),
        },
        "players": player_slugs,
        "teams": team_slugs,
    }
    if _is_quote_post(post):
        item["is_quote_post"] = True
    return item


# ---------------------------------------------------------------------------
# Fetch + run
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


def fetch_author_feed(client, actor: str, limit: int):
    """Call atproto's getAuthorFeed with the no-replies server filter."""
    resp = client.get_author_feed(
        actor=actor, filter="posts_no_replies", limit=limit
    )
    return resp.feed


def _within_since(item_iso: str, since_iso: Optional[str]) -> bool:
    if since_iso is None:
        return True
    return item_iso >= since_iso


def collect_items(
    reporters: List[Dict[str, Any]],
    feed_fetcher,
    players_dict,
    teams_dict,
    since_iso: Optional[str],
    limit: int,
) -> Tuple[List[dict], Dict[str, int]]:
    """Iterate reporters → posts → mapped items. Returns (items, stats).

    `feed_fetcher(actor, limit)` returns an iterable of FeedViewPost. The
    function is injected so tests can avoid the live network.
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


def _default_since() -> str:
    """24h before now, ISO 8601 UTC with Z suffix."""
    return parse_to_iso(datetime.now(timezone.utc) - timedelta(hours=24))


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Poll Bluesky reporters into today's shard.")
    p.add_argument(
        "--since",
        default=None,
        help="Only keep posts at or after this UTC ISO timestamp. "
        "Default: 24h ago.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max posts per reporter (atproto cap is 100). Default: 50.",
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


def _make_client():
    """Construct an unauthenticated atproto Client.

    The public AppView accepts unauthenticated reads for getAuthorFeed,
    so we don't log in. Imported lazily so tests don't need atproto.
    """
    from atproto import Client  # type: ignore
    return Client()


def run(argv: Optional[List[str]] = None, client=None) -> int:
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

    since_iso = parse_to_iso(args.since) if args.since else _default_since()
    logger.info(
        "polling %d reporters, since=%s, limit=%d",
        len(reporters),
        since_iso,
        args.limit,
    )

    players_dict, teams_dict = load_canonical()
    client = client or _make_client()

    def feed_fetcher(actor: str, limit: int):
        return fetch_author_feed(client, actor, limit)

    items, stats = collect_items(
        reporters, feed_fetcher, players_dict, teams_dict, since_iso, args.limit
    )
    logger.info("stats: %s", stats)

    if args.dry_run:
        print(f"DRY RUN — would append {len(items)} items to today's shard.")
        for item in items[:5]:
            print(f"  - {item['id']} {item['published_at']} {item['author']['handle']}: {item['title'][:80]}")
        if len(items) > 5:
            print(f"  ... and {len(items) - 5} more")
        return 0

    date = today_utc_date()
    appended = append_items("bluesky", date, items)
    logger.info("appended %d new items to data/bluesky/%s.json", appended, date)
    return 0


if __name__ == "__main__":
    sys.exit(run())
