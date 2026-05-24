"""Poll YouTube channel uploads via the Data API v3, append to today's shard.

SCOPE — EPISODES ONLY. No transcripts, no Gemini, no AI, no quote
extraction. Each item is a video's public metadata (title, channel,
publish date, description excerpt, thumbnail, link) plus free-tier
regex entity tags from canonical detection. DESIGN.md originally
specced transcript fetch + Gemini extraction; both are CANCELLED.

Channel list comes from the jsierrahoopshype/nba-podcast-stream repo
(the `CHANNELS` Python list literal in `app.py`). We parse it via the
existing `sources.parse_python_list_literal` AST helper (safe — no
eval), then merge with `data/sources/youtube_overrides.json`
(add/remove of bare channel IDs). The conditional/keyword-filtered
channels from nba-podcast-stream's CONDITIONAL_CHANNELS dict are NOT
included in v1 — they require a per-episode keyword filter that we'd
rather solve by storing all episodes and letting downstream filter on
canonical entity tags. Documented as a deferred decision.

Quota math (hourly cron):
  channels.list (cached): 1 unit per batch of up to 50 IDs. 57
    channels = 2 calls = 2 units. ONE-TIME until the cache loses an
    entry.
  playlistItems.list: 1 unit per channel per cycle.
  Per cycle (steady state): ~57 units.
  Per day (24 cycles): ~1,370 units. Well under the 10,000 default
    quota.

Auth: API key from `YOUTUBE_API_KEY` environment variable. No
hardcoded keys. Missing key → exit 1 with a clear message.

CLI:
  --since-hours N      Only keep videos published in the last N hours.
                       Default: 24.
  --channel CHANNEL_ID Restrict polling to a single channel (debug).
  --max-per-channel N  Cap items pulled per channel. Default: 10.
  --dry-run            Print what would be appended; no shard write.
  -v / --verbose       Debug logging.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from scripts.lib import shards
from scripts.lib.canonical import detect_entities, load_canonical
from scripts.lib.shards import append_items, validate_item
from scripts.lib.sources import (
    SourcesError,
    load_effective_list,
    parse_python_list_literal,
)
from scripts.lib.utils import parse_to_iso, today_utc_date, utc_now_iso

logger = logging.getLogger("poll_youtube")

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_SOURCES_DIR = REPO_ROOT / "data" / "sources"
OVERRIDES_PATH = DATA_SOURCES_DIR / "youtube_overrides.json"
CHANNEL_CACHE_PATH = DATA_SOURCES_DIR / "youtube_channel_cache.json"

LIVE_CHANNELS_URL = (
    "https://raw.githubusercontent.com/jsierrahoopshype/"
    "nba-podcast-stream/main/app.py"
)
LIVE_CHANNELS_VARIABLE = "CHANNELS"

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
CHANNELS_BATCH_SIZE = 50  # YouTube API max
INTER_CHANNEL_SLEEP_SEC = 0.0  # api-key calls don't need polite sleeps
EXCERPT_MAX_CHARS = 280
DEFAULT_USER_AGENT = (
    "nba-content-stream/0.1 (HoopsMatic; "
    "+https://github.com/jsierrahoopshype/nba-content-stream)"
)


# ---------------------------------------------------------------------------
# Channel list: live + overrides
# ---------------------------------------------------------------------------


def _parser(text: str) -> List[str]:
    """Adapter so `load_effective_list` can take our typed parser."""
    return parse_python_list_literal(text, LIVE_CHANNELS_VARIABLE)


def load_channel_list(overrides_path: Path = OVERRIDES_PATH) -> List[str]:
    """Live list (nba-podcast-stream/app.py CHANNELS) ∪ overrides.add \\ remove."""
    return load_effective_list("youtube", LIVE_CHANNELS_URL, _parser, overrides_path)


# ---------------------------------------------------------------------------
# Channel cache (uploads playlist id + title)
# ---------------------------------------------------------------------------


def load_channel_cache(path: Path = CHANNEL_CACHE_PATH) -> Dict[str, dict]:
    """Return the cache dict (channel_id -> entry). Missing file → {}."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        blob = json.load(f)
    cache = blob.get("cache", {})
    return cache if isinstance(cache, dict) else {}


def save_channel_cache(path: Path, cache: Dict[str, dict]) -> None:
    """Write the cache, preserving the `_meta` block if the file existed."""
    blob: dict
    if path.exists():
        with path.open(encoding="utf-8") as f:
            blob = json.load(f)
    else:
        blob = {}
    blob["cache"] = cache
    with path.open("w", encoding="utf-8") as f:
        json.dump(blob, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _chunked(seq: List[str], n: int) -> List[List[str]]:
    return [seq[i : i + n] for i in range(0, len(seq), n)]


# ---------------------------------------------------------------------------
# API callers (injectable for tests)
# ---------------------------------------------------------------------------


ChannelsFetcher = Callable[[List[str]], Dict[str, Any]]
PlaylistItemsFetcher = Callable[[str, int], Dict[str, Any]]


def make_channels_fetcher(
    session: requests.Session, api_key: str
) -> ChannelsFetcher:
    """Return a `channels.list` caller bound to the session and key."""

    def fetch(channel_ids: List[str]) -> Dict[str, Any]:
        params = {
            "part": "contentDetails,snippet",
            "id": ",".join(channel_ids),
            "maxResults": str(len(channel_ids)),
            "key": api_key,
        }
        resp = session.get(
            f"{YOUTUBE_API_BASE}/channels", params=params, timeout=15
        )
        resp.raise_for_status()
        return resp.json()

    return fetch


def make_playlist_items_fetcher(
    session: requests.Session, api_key: str
) -> PlaylistItemsFetcher:
    """Return a `playlistItems.list` caller bound to the session and key."""

    def fetch(playlist_id: str, max_results: int) -> Dict[str, Any]:
        params = {
            "part": "snippet",
            "playlistId": playlist_id,
            "maxResults": str(max_results),
            "key": api_key,
        }
        resp = session.get(
            f"{YOUTUBE_API_BASE}/playlistItems", params=params, timeout=15
        )
        resp.raise_for_status()
        return resp.json()

    return fetch


def resolve_channels(
    channel_ids: List[str],
    cache: Dict[str, dict],
    channels_fetcher: ChannelsFetcher,
) -> Tuple[Dict[str, dict], int]:
    """Look up uploads-playlist-id + title for `channel_ids` using cache.

    Returns `(resolved, channels_list_units_used)`. `resolved` is keyed
    by channel id and merges cached entries with any newly-resolved ones.
    Newly-resolved entries are also added to `cache` in-place so the
    caller can persist them.
    """
    resolved: Dict[str, dict] = {}
    missing: List[str] = []
    for cid in channel_ids:
        entry = cache.get(cid)
        if entry and entry.get("uploads_playlist_id"):
            resolved[cid] = entry
        else:
            missing.append(cid)

    units = 0
    for batch in _chunked(missing, CHANNELS_BATCH_SIZE):
        payload = channels_fetcher(batch)
        units += 1
        for item in payload.get("items", []) or []:
            cid = item.get("id")
            uploads = (
                item.get("contentDetails", {})
                .get("relatedPlaylists", {})
                .get("uploads")
            )
            title = item.get("snippet", {}).get("title") or cid or ""
            if not cid or not uploads:
                continue
            entry = {
                "uploads_playlist_id": uploads,
                "channel_title": title,
                "resolved_at": utc_now_iso(),
            }
            cache[cid] = entry
            resolved[cid] = entry

    return resolved, units


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _best_thumbnail(thumbnails: dict) -> Optional[str]:
    """Pick the highest-quality available thumbnail URL.

    YouTube returns thumbnails as a dict keyed by size name. We prefer
    `maxres`, then `standard`, then `high`, then `medium`, then `default`.
    """
    if not isinstance(thumbnails, dict):
        return None
    for size in ("maxres", "standard", "high", "medium", "default"):
        entry = thumbnails.get(size)
        if isinstance(entry, dict):
            url = entry.get("url")
            if url:
                return url
    return None


def _excerpt(description: str) -> str:
    """Whitespace-normalized first 280 chars of the description."""
    if not description:
        return ""
    collapsed = " ".join(description.split())
    if len(collapsed) <= EXCERPT_MAX_CHARS:
        return collapsed
    cut = collapsed[:EXCERPT_MAX_CHARS]
    space = cut.rfind(" ")
    if space > EXCERPT_MAX_CHARS * 0.6:
        cut = cut[:space]
    return cut.rstrip() + "…"


def map_playlist_item_to_item(
    pi_item: dict,
    channel_id: str,
    players_dict,
    teams_dict,
    ingested_at: Optional[str] = None,
) -> Optional[dict]:
    """Build a SHARD_FORMAT.md item from one playlistItems.list snippet entry.

    Returns None if the entry can't be mapped safely — missing videoId,
    title, or publishedAt.
    """
    snippet = pi_item.get("snippet") or {}
    resource = snippet.get("resourceId") or {}
    video_id = resource.get("videoId")
    if not video_id:
        return None
    title = snippet.get("title") or ""
    if not title:
        return None
    published_raw = snippet.get("publishedAt")
    if not published_raw:
        return None
    try:
        published_at = parse_to_iso(published_raw)
    except Exception:
        return None

    description = snippet.get("description") or ""
    excerpt = _excerpt(description)

    # Tag from title + description. Channel descriptions are noisy but
    # often name the topic of the episode in the first sentence, which
    # helps recall on shows whose titles are stylized.
    detect_text = f"{title}\n{description}" if description else title
    player_slugs, team_slugs = detect_entities(detect_text, players_dict, teams_dict)

    channel_title = snippet.get("channelTitle") or channel_id

    item: dict = {
        "id": f"yt-{video_id}",
        "source": "youtube",
        "published_at": published_at,
        "ingested_at": ingested_at or utc_now_iso(),
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": title.strip(),
        "author": {
            "handle": channel_id,
            "display_name": channel_title,
            "url": f"https://www.youtube.com/channel/{channel_id}",
        },
        "media": {"type": "video"},
        "players": player_slugs,
        "teams": team_slugs,
    }
    thumb = _best_thumbnail(snippet.get("thumbnails"))
    if thumb:
        item["thumbnail"] = thumb
    if excerpt:
        item["body_excerpt"] = excerpt
    return item


# ---------------------------------------------------------------------------
# Collect
# ---------------------------------------------------------------------------


def _within_since(item_iso: str, since_iso: Optional[str]) -> bool:
    if since_iso is None:
        return True
    return item_iso >= since_iso


def collect_items(
    channel_ids: List[str],
    cache: Dict[str, dict],
    channels_fetcher: ChannelsFetcher,
    playlist_items_fetcher: PlaylistItemsFetcher,
    players_dict,
    teams_dict,
    since_iso: Optional[str],
    max_per_channel: int,
    sleep_sec: float = INTER_CHANNEL_SLEEP_SEC,
) -> Tuple[List[dict], Dict[str, int]]:
    """Resolve + fetch + map for all channels. Returns (items, stats).

    The cache is mutated in-place for newly-resolved channels; the
    caller persists it.
    """
    stats = {
        "channels": len(channel_ids),
        "videos_seen": 0,
        "dropped_since": 0,
        "deduped": 0,
        "kept": 0,
        "channel_errors": 0,
        "quota_units_estimate": 0,
    }

    resolved, resolve_units = resolve_channels(channel_ids, cache, channels_fetcher)
    stats["quota_units_estimate"] += resolve_units

    items_by_id: Dict[str, dict] = {}
    ingested_at = utc_now_iso()

    for i, channel_id in enumerate(channel_ids):
        if i > 0 and sleep_sec > 0:
            time.sleep(sleep_sec)
        entry = resolved.get(channel_id)
        if not entry:
            stats["channel_errors"] += 1
            logger.warning("channel %s did not resolve (missing from API response)", channel_id)
            continue
        try:
            payload = playlist_items_fetcher(
                entry["uploads_playlist_id"], max_per_channel
            )
            stats["quota_units_estimate"] += 1
        except Exception as exc:
            stats["channel_errors"] += 1
            logger.warning(
                "playlistItems.list failed for %s (%s): %s",
                channel_id,
                entry.get("channel_title"),
                exc,
            )
            continue

        kept_for_channel = 0
        for pi in payload.get("items", []) or []:
            stats["videos_seen"] += 1
            item = map_playlist_item_to_item(
                pi, channel_id, players_dict, teams_dict, ingested_at
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
                    channel_id,
                    errs,
                )
                continue
            if item["id"] in items_by_id:
                stats["deduped"] += 1
                continue
            items_by_id[item["id"]] = item
            kept_for_channel += 1

        logger.debug(
            "youtube %s (%s) -> kept %d",
            channel_id,
            entry.get("channel_title"),
            kept_for_channel,
        )
        stats["kept"] += kept_for_channel

    return list(items_by_id.values()), stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_since_iso(hours: int) -> str:
    return parse_to_iso(datetime.now(timezone.utc) - timedelta(hours=hours))


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Poll YouTube channel uploads into today's shard."
    )
    p.add_argument(
        "--since-hours",
        type=int,
        default=24,
        help="Only keep videos published in the last N hours. Default: 24.",
    )
    p.add_argument(
        "--channel",
        default=None,
        help="Restrict polling to a single channel ID (debug aid).",
    )
    p.add_argument(
        "--max-per-channel",
        type=int,
        default=10,
        help="Cap items pulled per channel. Default: 10.",
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
    channels_fetcher: Optional[ChannelsFetcher] = None,
    playlist_items_fetcher: Optional[PlaylistItemsFetcher] = None,
    overrides_path: Path = OVERRIDES_PATH,
    cache_path: Path = CHANNEL_CACHE_PATH,
    sleep_sec: Optional[float] = None,
    api_key: Optional[str] = None,
) -> int:
    """Entry point. Tests pass `channels_fetcher` + `playlist_items_fetcher`
    to bypass the network.
    """
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # API key required for real calls; tests inject the fetchers and skip this.
    if channels_fetcher is None or playlist_items_fetcher is None:
        api_key = api_key or os.environ.get("YOUTUBE_API_KEY")
        if not api_key:
            logger.error(
                "YOUTUBE_API_KEY not set (export it in your shell or as a GH "
                "Actions secret)"
            )
            return 1

    try:
        channels = load_channel_list(overrides_path)
    except SourcesError as exc:
        logger.error("cannot load channel list: %s", exc)
        return 1

    if args.channel:
        channels = [c for c in channels if c == args.channel]
        if not channels:
            logger.error("no channel matches --channel %s", args.channel)
            return 1

    players_dict, teams_dict = load_canonical()
    cache = load_channel_cache(cache_path)
    since_iso = _default_since_iso(args.since_hours)
    logger.info(
        "polling %d channel(s), since=%s (%dh), max_per_channel=%d, cached=%d",
        len(channels),
        since_iso,
        args.since_hours,
        args.max_per_channel,
        sum(1 for cid in channels if cid in cache),
    )

    if channels_fetcher is None or playlist_items_fetcher is None:
        session = session or _make_session()
        channels_fetcher = channels_fetcher or make_channels_fetcher(session, api_key)
        playlist_items_fetcher = playlist_items_fetcher or make_playlist_items_fetcher(
            session, api_key
        )

    effective_sleep = sleep_sec if sleep_sec is not None else INTER_CHANNEL_SLEEP_SEC
    items, stats = collect_items(
        channels,
        cache,
        channels_fetcher,
        playlist_items_fetcher,
        players_dict,
        teams_dict,
        since_iso,
        args.max_per_channel,
        effective_sleep,
    )
    logger.info("stats: %s", stats)

    # Persist any newly-resolved cache entries even on dry-run; the cache
    # is shared state, not shard data.
    save_channel_cache(cache_path, cache)

    if stats["channel_errors"] == stats["channels"] and stats["channels"] > 0:
        logger.error("every channel failed; aborting without write")
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
                f"{item['author']['display_name']}: {item['title'][:80]}{tag_str}"
            )
            print(f"      {item['url']}")
        if len(items) > 5:
            print(f"  ... and {len(items) - 5} more")
        return 0

    items_by_date: Dict[str, List[dict]] = {}
    for item in items:
        date = item["published_at"][:10]
        items_by_date.setdefault(date, []).append(item)

    for date, batch in sorted(items_by_date.items()):
        appended = append_items("youtube", date, batch)
        logger.info(
            "appended %d new items to data/youtube/%s.json", appended, date
        )

    if not items_by_date:
        logger.info("no items kept this cycle")

    return 0


if __name__ == "__main__":
    sys.exit(run())
