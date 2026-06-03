"""Smoke tests for canonical_lookup + reporter_lookup.

These do not hit the network — canonical_lookup is monkeypatched
with synthetic dicts so we can assert the resolution logic without
depending on raw.githubusercontent uptime.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from lib import canonical_lookup, reporter_lookup  # noqa: E402
from lib.reporter_lookup import Reporter  # noqa: E402


@pytest.fixture(autouse=True)
def _stub_canonical(monkeypatch):
    """Inject synthetic canonical so tests are hermetic."""
    monkeypatch.setattr(canonical_lookup, "_PLAYERS_CACHE", {
        "victor-wembanyama": {
            "name": "Victor Wembanyama",
            "team": "san-antonio-spurs",
            "headshot_filename": "1641705-victor-wembanyama.png",
        },
        "lebron-james": {
            "name": "LeBron James",
            "team": "los-angeles-lakers",
            "headshot_filename": "2544-lebron-james.png",
        },
        "no-headshot": {
            "name": "No Headshot Player",
            "team": "los-angeles-lakers",
        },
    })
    monkeypatch.setattr(canonical_lookup, "_TEAMS_CACHE", {
        "san-antonio-spurs": {"name": "San Antonio Spurs"},
        "los-angeles-lakers": {"name": "Los Angeles Lakers"},
    })
    yield
    canonical_lookup.reset_caches()


def test_lookup_player_returns_team_context_and_headshot():
    info = canonical_lookup.lookup("victor-wembanyama")
    assert info.kind == "player"
    assert info.name == "Victor Wembanyama"
    assert info.team_context == "San Antonio Spurs"
    assert info.portrait_url and info.portrait_url.endswith("1641705-victor-wembanyama.png")
    assert info.initials == "VW"


def test_lookup_team_returns_espn_logo_url():
    info = canonical_lookup.lookup("los-angeles-lakers", kind_hint="team")
    assert info.kind == "team"
    assert info.portrait_url == "https://a.espncdn.com/i/teamlogos/nba/500/lal.png"
    assert info.initials == "LL"


def test_lookup_team_unknown_slug_returns_initials_no_url():
    info = canonical_lookup.lookup("imaginary-team", kind_hint="team")
    assert info.kind == "team"
    assert info.portrait_url is None
    assert info.initials


def test_lookup_player_with_no_headshot_filename_skips_url():
    info = canonical_lookup.lookup("no-headshot")
    assert info.kind == "player"
    assert info.portrait_url is None


def test_espn_abbr_override_for_spurs_and_pelicans_and_wizards():
    # The override map mirrors prerender_pages.py; check the slugs
    # ESPN spells differently from their canonical abbr.
    assert canonical_lookup._ESPN_TEAM_ABBR["san-antonio-spurs"] == "sa"
    assert canonical_lookup._ESPN_TEAM_ABBR["new-orleans-pelicans"] == "no"
    assert canonical_lookup._ESPN_TEAM_ABBR["washington-wizards"] == "wsh"


# ---- reporter_lookup ----


def _bsky_item(handle, *, count_offset=0, avatar=None, display=None, published="2026-05-25T10:00:00Z"):
    return {
        "source": "bluesky",
        "author_handle": handle,
        "author_avatar": avatar,
        "author": display or handle,
        "published_at": published,
        "url": f"https://bsky.app/profile/{handle}/post/x{count_offset}",
        "title": f"post by {handle}",
    }


def test_reporters_ranks_by_post_count():
    items = [
        _bsky_item("woj", count_offset=1, avatar="https://cdn/woj.png", display="Woj"),
        _bsky_item("woj", count_offset=2, display="Woj"),
        _bsky_item("woj", count_offset=3, display="Woj"),
        _bsky_item("shams", count_offset=1, display="Shams"),
        _bsky_item("marc", count_offset=1, display="Marc Stein"),
        _bsky_item("shams", count_offset=2, display="Shams"),
    ]
    reps = reporter_lookup.reporters_from_items(items, max_count=3)
    assert [r.handle for r in reps] == ["woj", "shams", "marc"]
    assert reps[0].display_name == "Woj"
    assert reps[0].avatar_url == "https://cdn/woj.png"


def test_reporters_skips_non_bluesky_sources():
    items = [
        {"source": "reddit", "title": "ignore me"},
        _bsky_item("woj"),
    ]
    reps = reporter_lookup.reporters_from_items(items)
    assert [r.handle for r in reps] == ["woj"]


def test_reporters_falls_back_to_url_handle_when_field_missing():
    items = [
        {
            "source": "bluesky",
            "url": "https://bsky.app/profile/shams.bsky.social/post/abc",
            "published_at": "2026-05-25T10:00:00Z",
        }
    ]
    reps = reporter_lookup.reporters_from_items(items)
    assert reps and reps[0].handle == "shams.bsky.social"


def test_reporters_empty_when_no_bluesky_items():
    assert reporter_lookup.reporters_from_items([{"source": "youtube"}]) == []
