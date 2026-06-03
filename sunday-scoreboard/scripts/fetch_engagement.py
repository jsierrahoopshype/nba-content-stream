"""Re-fetch live Bluesky engagement for the v2 spotlight quote phase.

The archive doesn't store like/repost/reply counts, so at render time
we pull them live from the public Bluesky AppView:

    GET https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread?uri=…

returns the post view with `likeCount` / `repostCount` / `replyCount`.

Fetches are paced — chunks of 10 with 500ms between chunks — mirroring
`pacedBatchFetch` in nba-content-stream's `assets/common.js` so we stay
polite to the rate limiter (worst case ~500 candidate posts across the
top 10 players → ~30-60s). Results cache to
`assets/cache/engagement_{week}.json` so re-renders don't re-hit the
network.

CLI:
    python scripts/fetch_engagement.py --week-of 2026-06-01
    python scripts/fetch_engagement.py --week-of 2026-06-01 --top-n 10
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Callable, Iterable

# Allow `python scripts/fetch_engagement.py` from the sub-project root.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import requests

from lib import format_specs as fs
from lib.engagement_score import Engagement, at_uri_from_item, bluesky_candidates

logger = logging.getLogger("fetch_engagement")

GETPOSTTHREAD_URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread"

# Pacing defaults — mirror common.js pacedBatchFetch(items, 10, 500, …).
DEFAULT_BATCH_SIZE = 10
DEFAULT_BETWEEN_MS = 500

CACHE_DIR = fs.ASSETS_DIR / "cache"

_UA = (
    "Mozilla/5.0 (compatible; SundayScoreboard/2.0; "
    "+https://github.com/jsierrahoopshype/nba-content-stream)"
)


# ---------------------------------------------------------------------------
# Paced batching — a sync port of common.js::pacedBatchFetch.
# ---------------------------------------------------------------------------


def paced_batch_fetch(
    items: list,
    fetch_fn: Callable,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    between_ms: int = DEFAULT_BETWEEN_MS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> list:
    """Run `fetch_fn` over `items` in chunks of `batch_size`, sleeping
    `between_ms` between chunks (but never after the last). Exceptions
    and `None` results are dropped; the surviving results are returned
    in input order. `sleep_fn` is injectable so tests don't actually
    wait."""
    out: list = []
    n = len(items)
    for i in range(0, n, batch_size):
        batch = items[i:i + batch_size]
        for item in batch:
            try:
                result = fetch_fn(item)
            except Exception as exc:  # noqa: BLE001 — one bad post shouldn't abort the run
                logger.debug("fetch_fn raised for %r: %s", item, exc)
                result = None
            if result is not None:
                out.append(result)
        if i + batch_size < n:
            sleep_fn(between_ms / 1000.0)
    return out


# ---------------------------------------------------------------------------
# Response parsing + single-post fetch.
# ---------------------------------------------------------------------------


def parse_engagement(payload: dict | None) -> Engagement | None:
    """Extract counts from a getPostThread JSON payload. Returns None
    if the thread/post is missing (deleted post, blocked, not-found)."""
    if not payload:
        return None
    thread = payload.get("thread")
    if not isinstance(thread, dict):
        return None
    post = thread.get("post")
    if not isinstance(post, dict):
        return None
    return Engagement(
        likes=int(post.get("likeCount", 0) or 0),
        reposts=int(post.get("repostCount", 0) or 0),
        replies=int(post.get("replyCount", 0) or 0),
    )


def fetch_post_engagement(
    uri: str,
    *,
    get: Callable = requests.get,
    timeout: float = 15.0,
) -> tuple[str, Engagement] | None:
    """Fetch one post's engagement. Returns `(uri, Engagement)` on
    success, None on any failure — callers treat a None as "no signal"
    and fall back to recency."""
    try:
        resp = get(
            GETPOSTTHREAD_URL,
            params={"uri": uri},
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        eng = parse_engagement(resp.json())
    except (requests.RequestException, ValueError) as exc:
        logger.debug("engagement fetch failed for %s: %s", uri, exc)
        return None
    if eng is None:
        return None
    return (uri, eng)


# ---------------------------------------------------------------------------
# Batch fetch for a set of URIs (with cache reuse).
# ---------------------------------------------------------------------------


def fetch_engagement_for_uris(
    uris: Iterable[str],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    between_ms: int = DEFAULT_BETWEEN_MS,
    get: Callable = requests.get,
    sleep_fn: Callable[[float], None] = time.sleep,
    known: dict[str, Engagement] | None = None,
) -> dict[str, Engagement]:
    """Resolve engagement for every URI in `uris`.

    De-dupes, skips URIs already present in `known` (the cache), paces
    the rest, and returns the merged `{uri: Engagement}` map. URIs that
    fail to fetch are simply absent from the result."""
    result: dict[str, Engagement] = dict(known or {})
    pending = [u for u in dict.fromkeys(uris) if u and u not in result]
    if not pending:
        return result

    logger.info(
        "fetching engagement for %d posts (%d cached) in chunks of %d",
        len(pending), len(result), batch_size,
    )
    fetched = paced_batch_fetch(
        pending,
        lambda u: fetch_post_engagement(u, get=get),
        batch_size=batch_size,
        between_ms=between_ms,
        sleep_fn=sleep_fn,
    )
    for uri, eng in fetched:
        result[uri] = eng
    return result


def candidate_uris_from_beats(beats_items: Iterable[list[dict]]) -> list[str]:
    """Flatten beat item-lists into the de-duped list of Bluesky
    AT-URIs to fetch. `beats_items` is an iterable of per-beat item
    lists (one per top-N player)."""
    uris: list[str] = []
    for items in beats_items:
        for it in bluesky_candidates(items):
            uri = at_uri_from_item(it)
            if uri:
                uris.append(uri)
    return list(dict.fromkeys(uris))


# ---------------------------------------------------------------------------
# Cache I/O — assets/cache/engagement_{week}.json
# ---------------------------------------------------------------------------


def cache_path(week_of: str) -> Path:
    return CACHE_DIR / f"engagement_{week_of}.json"


def load_cache(week_of: str) -> dict[str, Engagement]:
    path = cache_path(week_of)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("ignoring unreadable engagement cache %s: %s", path, exc)
        return {}
    return {uri: Engagement.from_dict(d) for uri, d in raw.items()}


def save_cache(week_of: str, data: dict[str, Engagement]) -> Path:
    path = cache_path(week_of)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {uri: eng.to_dict() for uri, eng in data.items()}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# CLI — standalone re-fetch + cache warm.
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Re-fetch Bluesky engagement for a week's top players."
    )
    p.add_argument("--week-of", required=True, help="Sunday opening the week (UTC, YYYY-MM-DD).")
    p.add_argument("--top-n", type=int, default=10, help="Players to cover (default 10).")
    p.add_argument(
        "--no-cache", action="store_true",
        help="Ignore any existing cache and refetch everything.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def run(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Import here so the module stays light for tests that only exercise
    # the pacing/parse helpers.
    from cluster_beats import cluster_beats
    from fetch_week_data import WeekRange, gather_week
    from rank_beats import rank_and_filter

    week = WeekRange.from_date_str(args.week_of)
    items = gather_week(week)
    beats = rank_and_filter(cluster_beats(items, window_hours=24), top_n=args.top_n)
    uris = candidate_uris_from_beats([b.items for b in beats])
    logger.info("collected %d candidate URIs across %d beats", len(uris), len(beats))

    known = {} if args.no_cache else load_cache(args.week_of)
    engagement = fetch_engagement_for_uris(uris, known=known)
    path = save_cache(args.week_of, engagement)
    logger.info("wrote %d engagement records to %s", len(engagement), path)
    return 0


if __name__ == "__main__":
    sys.exit(run())
