"""Resolve entity slugs to display names, portraits, and team context.

Lazy-loads the canonical players/teams JSON from the archive and
caches it in memory for the lifetime of the process. The ESPN team-
abbreviation override map mirrors `_ESPN_TEAM_ABBR` in
nba-content-stream's `scripts/prerender_pages.py` — duplicated here
intentionally so this sub-project has no runtime dependency on a
script outside `sunday-scoreboard/`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import archive_client

# Mirror of nba-content-stream/scripts/prerender_pages.py::_ESPN_TEAM_ABBR.
# Keep in sync if either side changes.
_ESPN_TEAM_ABBR = {
    "atlanta-hawks": "atl",
    "boston-celtics": "bos",
    "brooklyn-nets": "bkn",
    "charlotte-hornets": "cha",
    "chicago-bulls": "chi",
    "cleveland-cavaliers": "cle",
    "dallas-mavericks": "dal",
    "denver-nuggets": "den",
    "detroit-pistons": "det",
    "golden-state-warriors": "gs",
    "houston-rockets": "hou",
    "indiana-pacers": "ind",
    "los-angeles-clippers": "lac",
    "los-angeles-lakers": "lal",
    "memphis-grizzlies": "mem",
    "miami-heat": "mia",
    "milwaukee-bucks": "mil",
    "minnesota-timberwolves": "min",
    "new-orleans-pelicans": "no",
    "new-york-knicks": "ny",
    "oklahoma-city-thunder": "okc",
    "orlando-magic": "orl",
    "philadelphia-76ers": "phi",
    "phoenix-suns": "phx",
    "portland-trail-blazers": "por",
    "sacramento-kings": "sac",
    "san-antonio-spurs": "sa",
    "toronto-raptors": "tor",
    "utah-jazz": "utah",
    "washington-wizards": "wsh",
}


_PLAYERS_CACHE: dict | None = None
_TEAMS_CACHE: dict | None = None


def _strip_meta(blob: dict | None) -> dict:
    if not blob:
        return {}
    return {k: v for k, v in blob.items() if not k.startswith("_")}


def players() -> dict:
    global _PLAYERS_CACHE
    if _PLAYERS_CACHE is None:
        _PLAYERS_CACHE = _strip_meta(archive_client.fetch_canonical_players())
    return _PLAYERS_CACHE


def teams() -> dict:
    global _TEAMS_CACHE
    if _TEAMS_CACHE is None:
        _TEAMS_CACHE = _strip_meta(archive_client.fetch_canonical_teams())
    return _TEAMS_CACHE


def reset_caches() -> None:
    """Test hook — clear the lazy caches between runs that swap data."""
    global _PLAYERS_CACHE, _TEAMS_CACHE
    _PLAYERS_CACHE = None
    _TEAMS_CACHE = None


@dataclass(frozen=True)
class EntityInfo:
    """Normalized lookup result for a player or team slug.

    `portrait_url` is None if neither a headshot nor a team logo URL
    could be derived; callers render the initials fallback in that
    case.
    """

    slug: str
    kind: str           # "player" | "team"
    name: str
    portrait_url: str | None
    initials: str
    team_context: str | None  # display name of player's team, else None


def _initials(name: str) -> str:
    parts = [p for p in name.split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def headshot_url(player_slug: str) -> Optional[str]:
    p = players().get(player_slug)
    if not p or not p.get("headshot_filename"):
        return None
    return f"{archive_client.HEADSHOTS_BASE}/{p['headshot_filename']}"


def team_logo_url(team_slug: str) -> Optional[str]:
    """Resolve a team slug to a logo URL.

    Order of preference:
      1. Local bundled PNG at `assets/templates/team-logos/{slug}.png`
         (file:// URL). Prefer-local so the pipeline runs on networks
         that block the ESPN CDN — host_not_allowed denials happen on
         the cloud-render runner if the outbound policy doesn't
         allow a.espncdn.com.
      2. ESPN CDN URL via the abbreviation override map.
    Returns None if neither is available; renderer falls back to an
    initials circle.
    """
    # Local bundle (added incrementally — most slugs won't have one).
    local = _local_team_logo_path(team_slug)
    if local is not None:
        return local.as_uri()
    abbr = _ESPN_TEAM_ABBR.get(team_slug)
    if not abbr:
        return None
    return f"{archive_client.ESPN_LOGO_BASE}/{abbr}.png"


def _local_team_logo_path(team_slug: str):
    """Return a Path if a bundled logo exists, else None."""
    from pathlib import Path
    here = Path(__file__).resolve().parent.parent.parent
    candidate = here / "assets" / "templates" / "team-logos" / f"{team_slug}.png"
    if candidate.exists():
        return candidate
    return None


def lookup(slug: str, kind_hint: str | None = None) -> EntityInfo:
    """Resolve a slug to an EntityInfo. `kind_hint` ("player"/"team")
    short-circuits the player-first lookup when the caller already
    knows the entity's kind (avoids one redundant dict miss)."""
    if kind_hint in (None, "player"):
        p = players().get(slug)
        if p:
            name = p.get("name") or slug
            team_slug = p.get("team")
            team_name = None
            if team_slug:
                t = teams().get(team_slug) or {}
                team_name = t.get("name")
            return EntityInfo(
                slug=slug,
                kind="player",
                name=name,
                portrait_url=headshot_url(slug),
                initials=_initials(name),
                team_context=team_name,
            )
    if kind_hint in (None, "team"):
        t = teams().get(slug)
        if t:
            name = t.get("name") or slug
            return EntityInfo(
                slug=slug,
                kind="team",
                name=name,
                portrait_url=team_logo_url(slug),
                initials=_initials(name),
                team_context=None,
            )
    # Unknown slug — render with initials only. We don't fail the
    # whole render for one mystery slug; the bucket of items that
    # surfaced it is still recap-worthy.
    return EntityInfo(
        slug=slug,
        kind=kind_hint or "player",
        name=slug.replace("-", " ").title(),
        portrait_url=None,
        initials=_initials(slug.replace("-", " ")),
        team_context=None,
    )
