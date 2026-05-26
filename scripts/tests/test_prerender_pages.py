"""Tests for `scripts.prerender_pages`.

Builds a small manifest in a tmp dir, points the script at it, and
asserts the generated HTML files have the right SEO baked in and that
sitemap.xml lists everything. No real shards needed — the prerender
step only reads the manifest, not the source shards.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import prerender_pages


def _write_manifest(path: Path, players, teams) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-05-26T00:00:00Z",
                "window_days": 30,
                "total_items": 100,
                "sources": {"bluesky": 50, "youtube": 50},
                "players": players,
                "teams": teams,
            }
        )
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_avatar_initials_single_name():
    initials, color = prerender_pages._avatar("Wemby")
    assert initials == "WE"
    assert color.startswith("#")


def test_avatar_initials_two_part_name():
    initials, _ = prerender_pages._avatar("Stephen Curry")
    assert initials == "SC"


def test_avatar_initials_three_part_name():
    """First initial + last initial — middle ignored."""
    initials, _ = prerender_pages._avatar("Shai Gilgeous Alexander")
    assert initials == "SA"


def test_avatar_color_is_deterministic_for_same_name():
    _, c1 = prerender_pages._avatar("LeBron James")
    _, c2 = prerender_pages._avatar("LeBron James")
    assert c1 == c2


# ---------------------------------------------------------------------------
# Page rendering: SEO + structure
# ---------------------------------------------------------------------------


def test_player_page_has_seo_baked_in():
    html_text = prerender_pages._render_page(
        "player", "stephen-curry", "Stephen Curry", 47
    )
    assert "<title>Stephen Curry — NBA News" in html_text
    assert 'name="description"' in html_text
    assert "The latest Stephen Curry news" in html_text
    assert 'property="og:title"' in html_text
    assert 'property="og:description"' in html_text
    assert 'name="ncs-entity" data-kind="player" data-slug="stephen-curry"' in html_text


def test_team_page_marks_kind_team():
    html_text = prerender_pages._render_page(
        "team", "los-angeles-lakers", "Los Angeles Lakers", 31
    )
    assert 'data-kind="team"' in html_text
    assert 'data-slug="los-angeles-lakers"' in html_text


def test_team_page_marks_teams_tab_active():
    """The Teams tab should be `class="active"` on team pages."""
    html_text = prerender_pages._render_page("team", "los-angeles-lakers", "Los Angeles Lakers", 31)
    # ../teams.html with active class
    assert '../teams.html" class="active"' in html_text
    # And the players tab is NOT active
    assert '../players.html"' in html_text  # plain link, no active class on this line
    # Players tab without "active" class — check by absence of active right after teams.html
    assert '../players.html" class="active"' not in html_text


def test_player_page_marks_players_tab_active():
    html_text = prerender_pages._render_page("player", "stephen-curry", "Stephen Curry", 47)
    assert '../players.html" class="active"' in html_text
    assert '../teams.html" class="active"' not in html_text


def test_page_escapes_special_chars_in_name():
    """A name with an apostrophe / ampersand mustn't break the HTML."""
    html_text = prerender_pages._render_page(
        "player", "deaaron-fox", "De'Aaron Fox & Friends", 12
    )
    # Apostrophe rendered as &#x27; via html.escape — ensure no raw quotes
    # break the OG meta or title attributes.
    assert "De'Aaron Fox" not in html_text  # raw form should NOT appear
    assert "&amp;" in html_text  # ampersand escaped


def test_page_loads_canonical_url():
    html_text = prerender_pages._render_page("player", "stephen-curry", "Stephen Curry", 47)
    assert 'rel="canonical"' in html_text
    assert "players/stephen-curry.html" in html_text


def test_page_includes_mini_chart_placeholder():
    html_text = prerender_pages._render_page("player", "stephen-curry", "Stephen Curry", 47)
    assert 'id="chart"' in html_text
    assert "Mentions, last 14 days" in html_text


def test_page_references_entity_js():
    """entity.js must be referenced or the page won't load anything."""
    html_text = prerender_pages._render_page("player", "stephen-curry", "Stephen Curry", 47)
    assert "../assets/entity.js" in html_text
    assert "../assets/config.js" in html_text
    assert "../assets/common.js" in html_text


# ---------------------------------------------------------------------------
# generate_pages orchestration
# ---------------------------------------------------------------------------


def test_generate_pages_writes_one_file_per_entity(tmp_path):
    players_dir = tmp_path / "players"
    teams_dir = tmp_path / "teams"
    sitemap = tmp_path / "sitemap.xml"
    manifest = {
        "players": [
            {"slug": "stephen-curry", "name": "Stephen Curry", "count": 5},
            {"slug": "lebron-james", "name": "LeBron James", "count": 3},
        ],
        "teams": [
            {"slug": "los-angeles-lakers", "name": "Los Angeles Lakers", "count": 2},
        ],
    }
    n_p, n_t = prerender_pages.generate_pages(
        manifest, players_out=players_dir, teams_out=teams_dir, sitemap_path=sitemap
    )
    assert (n_p, n_t) == (2, 1)
    assert (players_dir / "stephen-curry.html").exists()
    assert (players_dir / "lebron-james.html").exists()
    assert (teams_dir / "los-angeles-lakers.html").exists()
    assert sitemap.exists()


def test_generate_pages_dry_run_writes_nothing(tmp_path):
    players_dir = tmp_path / "players"
    teams_dir = tmp_path / "teams"
    sitemap = tmp_path / "sitemap.xml"
    manifest = {
        "players": [{"slug": "x", "name": "X", "count": 1}],
        "teams": [],
    }
    n_p, n_t = prerender_pages.generate_pages(
        manifest,
        players_out=players_dir,
        teams_out=teams_dir,
        sitemap_path=sitemap,
        dry_run=True,
    )
    assert (n_p, n_t) == (1, 0)
    assert not players_dir.exists()
    assert not sitemap.exists()


def test_generate_pages_is_idempotent(tmp_path):
    """A second build wipes the first build's files and writes the same set."""
    players_dir = tmp_path / "players"
    teams_dir = tmp_path / "teams"
    sitemap = tmp_path / "sitemap.xml"
    manifest1 = {
        "players": [
            {"slug": "stephen-curry", "name": "Stephen Curry", "count": 5},
            {"slug": "lebron-james", "name": "LeBron James", "count": 3},
        ],
        "teams": [],
    }
    prerender_pages.generate_pages(manifest1, players_out=players_dir, teams_out=teams_dir, sitemap_path=sitemap)

    # Second build with one player removed — the stale file must be wiped.
    manifest2 = {
        "players": [
            {"slug": "stephen-curry", "name": "Stephen Curry", "count": 5},
        ],
        "teams": [],
    }
    prerender_pages.generate_pages(manifest2, players_out=players_dir, teams_out=teams_dir, sitemap_path=sitemap)
    assert (players_dir / "stephen-curry.html").exists()
    assert not (players_dir / "lebron-james.html").exists()


def test_generate_pages_preserves_non_html_files(tmp_path):
    """A safety check: non-HTML files in players/ must not be deleted."""
    players_dir = tmp_path / "players"
    teams_dir = tmp_path / "teams"
    sitemap = tmp_path / "sitemap.xml"
    players_dir.mkdir(parents=True)
    (players_dir / "README.md").write_text("don't delete me")
    prerender_pages.generate_pages(
        {"players": [{"slug": "x", "name": "X", "count": 1}], "teams": []},
        players_out=players_dir, teams_out=teams_dir, sitemap_path=sitemap,
    )
    assert (players_dir / "README.md").exists()


def test_generate_pages_handles_special_chars_in_slug(tmp_path):
    """A slug like deaaron-fox should produce a sensible file path."""
    players_dir = tmp_path / "players"
    teams_dir = tmp_path / "teams"
    sitemap = tmp_path / "sitemap.xml"
    manifest = {
        "players": [{"slug": "deaaron-fox", "name": "De'Aaron Fox", "count": 4}],
        "teams": [],
    }
    n_p, _ = prerender_pages.generate_pages(
        manifest, players_out=players_dir, teams_out=teams_dir, sitemap_path=sitemap,
    )
    assert n_p == 1
    assert (players_dir / "deaaron-fox.html").exists()


# ---------------------------------------------------------------------------
# Sitemap
# ---------------------------------------------------------------------------


def test_sitemap_includes_homepage_and_all_entities(tmp_path):
    players_dir = tmp_path / "players"
    teams_dir = tmp_path / "teams"
    sitemap = tmp_path / "sitemap.xml"
    manifest = {
        "players": [{"slug": "stephen-curry", "name": "Stephen Curry", "count": 5}],
        "teams": [{"slug": "los-angeles-lakers", "name": "Los Angeles Lakers", "count": 3}],
    }
    prerender_pages.generate_pages(manifest, players_out=players_dir, teams_out=teams_dir, sitemap_path=sitemap)
    text = sitemap.read_text()
    assert "<?xml" in text
    assert "<urlset" in text
    # Homepage + directory pages
    assert "/index.html</loc>" in text
    assert "/players.html</loc>" in text
    assert "/teams.html</loc>" in text
    # Entity pages
    assert "/players/stephen-curry.html</loc>" in text
    assert "/teams/los-angeles-lakers.html</loc>" in text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_run_missing_manifest_returns_1(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(prerender_pages, "MANIFEST_PATH", tmp_path / "missing.json")
    with caplog.at_level("ERROR"):
        rc = prerender_pages.run([])
    assert rc == 1
    assert any("manifest not found" in r.message for r in caplog.records)


def test_run_dry_run_zero_exit(tmp_path, monkeypatch, capsys):
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(
        manifest_path,
        players=[{"slug": "x", "name": "X", "count": 1}],
        teams=[],
    )
    monkeypatch.setattr(prerender_pages, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(prerender_pages, "PLAYERS_OUT_DIR", tmp_path / "players")
    monkeypatch.setattr(prerender_pages, "TEAMS_OUT_DIR", tmp_path / "teams")
    monkeypatch.setattr(prerender_pages, "SITEMAP_PATH", tmp_path / "sitemap.xml")
    rc = prerender_pages.run(["--dry-run"])
    assert rc == 0
    captured = capsys.readouterr().out
    assert "DRY RUN" in captured
    assert not (tmp_path / "players").exists()
