"""Fetch nba-content-stream archive data via raw.githubusercontent.com.

Treats the upstream data source as external even though sunday-scoreboard
currently lives inside the same repo — the migration to a standalone
repo is documented in DESIGN.md, and pulling from raw.githubusercontent
makes that migration a no-op.

The client caches each fetched JSON on disk under a tmpdir so re-runs
during the same render pass don't hammer GitHub. Cache is keyed on URL
+ today's UTC date — manifests can update through the day, but we
don't need sub-minute freshness for a weekly recap.
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger("archive_client")

UPSTREAM_OWNER = "jsierrahoopshype"
UPSTREAM_REPO = "nba-content-stream"
UPSTREAM_BRANCH = "main"
RAW_BASE = (
    f"https://raw.githubusercontent.com/{UPSTREAM_OWNER}/{UPSTREAM_REPO}/"
    f"{UPSTREAM_BRANCH}"
)

# Headshots live in a separate repo with a flat directory.
HEADSHOTS_BASE = (
    "https://raw.githubusercontent.com/jsierrahoopshype/"
    "nba-headshots/main/players/headshots/face"
)

# ESPN CDN team logos.
ESPN_LOGO_BASE = "https://a.espncdn.com/i/teamlogos/nba/500"

# Bluesky AppView CDN for reporter avatars.
BSKY_AVATAR_BASE = "https://cdn.bsky.app/img/avatar/plain"

# Default cache lives under the OS tmpdir so it survives between
# invocations within a CI run but doesn't pollute the working tree.
_CACHE_DIR = Path(tempfile.gettempdir()) / "sunday-scoreboard-cache"


def _cache_path(url: str) -> Path:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return _CACHE_DIR / today / digest


def _fetch_with_retry(
    url: str, *, max_attempts: int = 3, timeout: float = 15.0
) -> bytes | None:
    """GET `url` with exponential backoff. Returns the response body or
    None on persistent failure — callers decide whether a 404 is fatal
    (per-entity index files can legitimately not exist for low-volume
    slugs) or recoverable.

    A browser-like User-Agent is sent because ESPN's CDN returns 403
    Forbidden to bare `python-requests/x.x.x` clients. raw.github
    accepts either, so a single UA covers every source we touch.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; SundayScoreboard/1.0; "
            "+https://github.com/jsierrahoopshype/nba-content-stream)"
        ),
        "Accept": "*/*",
    }
    delay = 1.0
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.get(url, timeout=timeout, headers=headers)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as exc:
            if attempt == max_attempts:
                logger.warning("fetch failed (%s): %s", url, exc)
                return None
            time.sleep(delay)
            delay *= 2
    return None


def fetch_json(path: str, *, cache: bool = True) -> Any | None:
    """Fetch `<RAW_BASE>/<path>` and decode as JSON.

    `path` is a repo-relative path like 'data/index/manifest.json'.
    Returns None if the file is missing or unreadable. Disk-cached by
    URL within today's UTC date.
    """
    url = f"{RAW_BASE}/{path}"
    cpath = _cache_path(url)
    if cache and cpath.exists():
        try:
            return json.loads(cpath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    body = _fetch_with_retry(url)
    if body is None:
        return None
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError as exc:
        logger.warning("non-JSON response from %s: %s", url, exc)
        return None
    if cache:
        cpath.parent.mkdir(parents=True, exist_ok=True)
        cpath.write_bytes(body)
    return decoded


def fetch_binary(url: str, *, cache: bool = True) -> bytes | None:
    """Fetch any binary URL (headshot, logo, avatar) with disk cache.

    Avatars and logos are stable, so the cache key uses the URL hash
    without the daily-date prefix to maximize reuse across days.

    `file://` URLs are read directly off disk so bundled team logos
    (see canonical_lookup._local_team_logo_path) don't need a fake
    HTTP server.
    """
    if url.startswith("file://"):
        from urllib.parse import urlparse, unquote
        path = Path(unquote(urlparse(url).path))
        return path.read_bytes() if path.exists() else None
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    cpath = _CACHE_DIR / "bin" / digest
    if cache and cpath.exists():
        return cpath.read_bytes()
    body = _fetch_with_retry(url)
    if body is None:
        return None
    if cache:
        cpath.parent.mkdir(parents=True, exist_ok=True)
        cpath.write_bytes(body)
    return body


# ----- Convenience wrappers -----


def fetch_manifest() -> dict | None:
    return fetch_json("data/index/manifest.json")


def fetch_feed() -> dict | None:
    return fetch_json("data/index/feed.json")


def fetch_player_index(slug: str) -> dict | None:
    return fetch_json(f"data/index/players/{slug}.json")


def fetch_team_index(slug: str) -> dict | None:
    return fetch_json(f"data/index/teams/{slug}.json")


def fetch_canonical_players() -> dict | None:
    return fetch_json("data/canonical/players.json")


def fetch_canonical_teams() -> dict | None:
    return fetch_json("data/canonical/teams.json")
