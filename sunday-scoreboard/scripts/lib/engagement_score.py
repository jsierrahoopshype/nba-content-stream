"""Bluesky engagement scoring + AT-URI derivation for the v2 spotlight.

Pure-data module — no Pillow / moviepy / requests imports so it stays
cheap to load in tests and in `fetch_engagement.py`. The network fetch
itself lives in `scripts/fetch_engagement.py`; this module is only the
*logic*: turn an archive item into a Bluesky AT-URI, score a post's
engagement, and pick the single best-quote candidate per player.

The scoring weights replies and reposts higher than likes because an
active reply/repost is stronger social proof than a passive like — a
post people argue about or amplify is a better "quote of the week"
than one that merely accrued hearts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import unquote

# Score weights — replies > reposts > likes.
LIKE_WEIGHT = 1
REPOST_WEIGHT = 2
REPLY_WEIGHT = 3

_POST_COLLECTION = "app.bsky.feed.post"


@dataclass(frozen=True)
class Engagement:
    """A post's live engagement counts, fetched from getPostThread.

    `score` applies the reply/repost-weighted formula; `total` is the
    raw sum used for the speedometer ticker in the quote phase.
    """

    likes: int = 0
    reposts: int = 0
    replies: int = 0

    @property
    def score(self) -> int:
        return (
            self.likes * LIKE_WEIGHT
            + self.reposts * REPOST_WEIGHT
            + self.replies * REPLY_WEIGHT
        )

    @property
    def total(self) -> int:
        return self.likes + self.reposts + self.replies

    def to_dict(self) -> dict:
        return {"likes": self.likes, "reposts": self.reposts, "replies": self.replies}

    @classmethod
    def from_dict(cls, d: dict) -> "Engagement":
        return cls(
            likes=int(d.get("likes", 0) or 0),
            reposts=int(d.get("reposts", 0) or 0),
            replies=int(d.get("replies", 0) or 0),
        )


def score_counts(likes: int, reposts: int, replies: int) -> int:
    """Standalone scoring helper: `(likes + reposts*2 + replies*3)`."""
    return (
        int(likes) * LIKE_WEIGHT
        + int(reposts) * REPOST_WEIGHT
        + int(replies) * REPLY_WEIGHT
    )


# ---------------------------------------------------------------------------
# AT-URI derivation
# ---------------------------------------------------------------------------


def _did_from_cdn_url(url: str) -> Optional[str]:
    """Pull the `did:plc:…` segment out of a bsky CDN URL.

    Avatar / thumbnail URLs look like
    `https://cdn.bsky.app/img/feed_thumbnail/plain/<did>/<cid>@jpeg`,
    so the DID is the path segment immediately after `/plain/`.
    """
    marker = "/plain/"
    idx = url.find(marker)
    if idx < 0:
        return None
    rest = url[idx + len(marker):]
    did = rest.split("/", 1)[0]
    return did if did.startswith("did:") else None


def _rkey_from_post_url(url: str) -> Optional[str]:
    """`https://bsky.app/profile/<h>/post/<rkey>` → `<rkey>`."""
    marker = "/post/"
    idx = url.find(marker)
    if idx < 0:
        return None
    rkey = url[idx + len(marker):].split("/", 1)[0].split("?", 1)[0]
    return rkey or None


def at_uri_from_item(item: dict) -> Optional[str]:
    """Best-effort AT-URI (`at://did/app.bsky.feed.post/rkey`) for a
    Bluesky archive item.

    Primary path: the archive `id` already encodes the full URI as
    `bs-` + URL-encoded `<did>/app.bsky.feed.post/<rkey>`. Decoding it
    yields a getPostThread-ready URI with no handle→DID round-trip.

    Fallback: reconstruct from the DID embedded in a CDN URL
    (`thumbnail` / `media.thumb`) plus the rkey in the post `url`.

    Returns None for non-Bluesky items or when neither path resolves.
    """
    if item.get("source") != "bluesky":
        return None

    raw_id = item.get("id") or ""
    if raw_id.startswith("bs-"):
        decoded = unquote(raw_id[len("bs-"):])
        if decoded.startswith("at://"):
            return decoded
        if _POST_COLLECTION in decoded and decoded.startswith("did:"):
            return f"at://{decoded}"

    # Fallback: DID from a CDN URL + rkey from the post URL.
    url = item.get("url") or ""
    rkey = _rkey_from_post_url(url)
    did = None
    for candidate in (item.get("thumbnail"), (item.get("media") or {}).get("thumb")):
        if candidate:
            did = _did_from_cdn_url(candidate)
            if did:
                break
    if did and rkey:
        return f"at://{did}/{_POST_COLLECTION}/{rkey}"
    return None


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------


def bluesky_candidates(items: Iterable[dict]) -> list[dict]:
    """Return the Bluesky items from a cluster — the only items that
    carry a quotable AT-URI. Non-Bluesky items are dropped."""
    return [it for it in items if it.get("source") == "bluesky"]


def best_quote(
    candidates: list[dict],
    engagement_by_uri: dict[str, Engagement],
) -> Optional[tuple[dict, Optional[Engagement]]]:
    """Pick the single best quote for a player from their candidate
    Bluesky posts.

    Strategy:
      1. Score every candidate that has a fetched Engagement.
      2. If the best score is > 0, pick it (ties → most recent).
      3. Otherwise — no engagement was fetched, or every candidate
         scored 0 — fall back to the most recent candidate. This is
         the brief's "don't fail if engagement returns 0; fall back to
         recency" rule.

    Returns `(item, engagement_or_none)` or None when there are no
    candidates at all.
    """
    if not candidates:
        return None

    def recency_key(it: dict) -> str:
        return it.get("published_at") or ""

    scored: list[tuple[int, str, dict, Engagement]] = []
    for it in candidates:
        uri = at_uri_from_item(it)
        eng = engagement_by_uri.get(uri) if uri else None
        if eng is not None:
            scored.append((eng.score, recency_key(it), it, eng))

    if scored:
        best = max(scored, key=lambda row: (row[0], row[1]))
        if best[0] > 0:
            return (best[2], best[3])

    # Recency fallback — newest candidate; attach engagement if we have it.
    newest = max(candidates, key=recency_key)
    uri = at_uri_from_item(newest)
    return (newest, engagement_by_uri.get(uri) if uri else None)


def quote_text(item: dict) -> str:
    """The displayable post text. Prefers the fuller `body_excerpt`,
    falls back to `title`, then to an empty string."""
    return (item.get("body_excerpt") or item.get("title") or "").strip()
