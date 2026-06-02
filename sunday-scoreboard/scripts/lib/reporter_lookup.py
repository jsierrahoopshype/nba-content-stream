"""Bluesky-reporter avatar + display-name resolution.

The archive items (`data/index/{players,teams}/{slug}.json`) carry
the reporter's handle on every Bluesky item but not consistently the
avatar URL. When an item lacks `author_avatar`, we fall back to the
handle alone — display-only — rather than skip the reporter.

Future enhancement: walk `data/sources/bluesky_handles.csv` for a
display-name + did mapping cache. For v1 we trust the item fields.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Reporter:
    handle: str             # without leading @
    display_name: str       # falls back to handle if archive missing
    avatar_url: str | None  # may be None — renderer shows initials


def reporters_from_items(items: list[dict], *, max_count: int = 3) -> list[Reporter]:
    """Pick the top `max_count` Bluesky reporters who contributed to a
    cluster. Ranking is by post count within the cluster, ties broken
    by most-recent-post.

    Non-Bluesky items are silently ignored — only reporters have an
    identifiable handle. A cluster with zero Bluesky items returns
    an empty list, and the renderer hides the reporters phase.
    """
    by_handle: dict[str, dict] = {}
    for it in items:
        if it.get("source") != "bluesky":
            continue
        handle = it.get("author_handle") or _handle_from_url(it.get("url") or "")
        if not handle:
            continue
        bucket = by_handle.setdefault(
            handle,
            {
                "count": 0,
                "latest": "",
                "display": handle,
                "avatar": None,
            },
        )
        bucket["count"] += 1
        published = it.get("published_at") or ""
        if published > bucket["latest"]:
            bucket["latest"] = published
        if it.get("author"):
            bucket["display"] = it["author"]
        if it.get("author_avatar") and not bucket["avatar"]:
            bucket["avatar"] = it["author_avatar"]
    ranked = sorted(
        by_handle.items(),
        key=lambda kv: (-kv[1]["count"], _negate_iso(kv[1]["latest"])),
    )
    out = []
    for handle, info in ranked[:max_count]:
        out.append(
            Reporter(
                handle=handle,
                display_name=info["display"] or handle,
                avatar_url=info["avatar"],
            )
        )
    return out


def _handle_from_url(url: str) -> str | None:
    """Extract the Bluesky handle from a bsky.app post URL.

    Pattern: https://bsky.app/profile/<handle>/post/<rkey>
    """
    marker = "bsky.app/profile/"
    idx = url.find(marker)
    if idx < 0:
        return None
    rest = url[idx + len(marker):]
    end = rest.find("/")
    if end < 0:
        return rest or None
    return rest[:end] or None


def _negate_iso(iso: str) -> str:
    """For sort ties: reverse-lex an ISO timestamp so higher = sooner."""
    # Inverting each char keeps lexicographic compare working.
    return "".join(chr(255 - ord(c)) if c.isprintable() else c for c in iso)
