"""Tests for the Phase 2 shared library.

Covers canonical entity detection (names, aliases, last-name
disambiguation), shard read/write round-trips, item validation, dedup
on append, and ISO date conversion from RSS-format strings.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.lib import canonical, shards
from scripts.lib.canonical import detect_entities, load_canonical
from scripts.lib.shards import (
    append_items,
    load_shard,
    save_shard,
    shard_path,
    validate_item,
)
from scripts.lib.utils import parse_to_iso, strip_html, today_utc_date, utc_now_iso


# ---------------------------------------------------------------------------
# canonical / detect_entities
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def vocab():
    return load_canonical()


def test_load_canonical_returns_dicts(vocab):
    players, teams = vocab
    assert isinstance(players, dict) and isinstance(teams, dict)
    assert "lebron-james" in players
    assert "los-angeles-lakers" in teams
    assert not any(k.startswith("_") for k in players)
    assert not any(k.startswith("_") for k in teams)


def test_detect_players_by_canonical_name(vocab):
    players, teams = vocab
    text = (
        "LeBron James, Stephen Curry, Giannis Antetokounmpo, "
        "Nikola Jokic, and Jayson Tatum all played tonight."
    )
    player_slugs, _ = detect_entities(text, players, teams)
    assert "lebron-james" in player_slugs
    assert "stephen-curry" in player_slugs
    assert "giannis-antetokounmpo" in player_slugs
    assert "nikola-jokic" in player_slugs
    assert "jayson-tatum" in player_slugs


def test_detect_teams_by_canonical_name(vocab):
    players, teams = vocab
    text = "The Los Angeles Lakers beat the Boston Celtics, with the Denver Nuggets a close third."
    _, team_slugs = detect_entities(text, players, teams)
    assert "los-angeles-lakers" in team_slugs
    assert "boston-celtics" in team_slugs
    assert "denver-nuggets" in team_slugs


def test_detect_aliases(vocab):
    players, teams = vocab
    text = "Wemby and KAT got buckets while SGA, KD, and PG watched from the bench."
    player_slugs, _ = detect_entities(text, players, teams)
    assert "victor-wembanyama" in player_slugs
    assert "karl-anthony-towns" in player_slugs
    assert "shai-gilgeous-alexander" in player_slugs
    assert "kevin-durant" in player_slugs
    assert "paul-george" in player_slugs


def test_detect_team_aliases(vocab):
    players, teams = vocab
    text = "Cavs vs Dubs and the Sixers in the East."
    _, team_slugs = detect_entities(text, players, teams)
    assert "cleveland-cavaliers" in team_slugs
    assert "golden-state-warriors" in team_slugs
    assert "philadelphia-76ers" in team_slugs


def test_detection_is_case_insensitive(vocab):
    players, teams = vocab
    player_slugs, team_slugs = detect_entities(
        "lebron and the LAKERS are back", players, teams
    )
    assert "lebron-james" in player_slugs
    assert "los-angeles-lakers" in team_slugs


def test_word_boundary_prevents_substring_match(vocab):
    players, teams = vocab
    # "King" appears here but "King James" does not; LBJ shouldn't match.
    player_slugs, _ = detect_entities("The kingdom of basketball", players, teams)
    assert "lebron-james" not in player_slugs


def test_empty_text_returns_empty_lists(vocab):
    players, teams = vocab
    assert detect_entities("", players, teams) == ([], [])


# ---------------------------------------------------------------------------
# Regression: short all-caps aliases must not match common English words.
#
# A user reported Bluesky cards with random "Game 7" posts tagged with
# new-orleans-pelicans and washington-wizards. Root cause: the Pelicans
# carried `NO` as an alias and the Wizards carried `WAS`, both two/three
# letter all-caps strings. The tagger is case-insensitive word-boundary,
# so any sentence containing "no" or "was" tagged those teams. Both
# aliases removed; these tests pin the fix.
# ---------------------------------------------------------------------------


def test_common_word_was_does_not_tag_wizards(vocab):
    players, teams = vocab
    for text in [
        "This was always going to Game 7",
        "Thunder-Spurs Game 1 was an all-time classic",
        "Neemias Queta was a top-10 center in the regular season",
    ]:
        _, team_slugs = detect_entities(text, players, teams)
        assert "washington-wizards" not in team_slugs, (
            f"regression: 'was' in {text!r} tagged washington-wizards"
        )


def test_common_word_no_does_not_tag_pelicans(vocab):
    players, teams = vocab
    for text in [
        "I have no idea what is happening",
        "There is no clear front-runner this year",
        "He said no to the deal",
    ]:
        _, team_slugs = detect_entities(text, players, teams)
        assert "new-orleans-pelicans" not in team_slugs, (
            f"regression: 'no' in {text!r} tagged new-orleans-pelicans"
        )


def test_long_aliases_for_pelicans_and_wizards_still_work(vocab):
    """Removing NO/WAS must not break the real-name and -ish aliases."""
    players, teams = vocab
    _, t1 = detect_entities("Pelicans win in OT", players, teams)
    assert "new-orleans-pelicans" in t1
    _, t2 = detect_entities("NOP picked up the option", players, teams)
    assert "new-orleans-pelicans" in t2
    _, t3 = detect_entities("The Wizards traded for him", players, teams)
    assert "washington-wizards" in t3
    _, t4 = detect_entities("Wiz fans expecting a quiet offseason", players, teams)
    assert "washington-wizards" in t4


# ---------------------------------------------------------------------------
# disambiguation
# ---------------------------------------------------------------------------


def test_murray_alone_is_ambiguous(vocab):
    players, teams = vocab
    player_slugs, _ = detect_entities("Murray had 30 points tonight.", players, teams)
    assert "jamal-murray" not in player_slugs
    assert "dejounte-murray" not in player_slugs


def test_murray_with_full_first_name_resolves(vocab):
    """After the Cluster C tagger tightening, bare last names no longer
    match. The user must say "Jamal Murray" / "Dejounte Murray" to
    tag a specific Murray. Team context alone is no longer enough.

    This is a deliberate trade-off: with 530+ canonical players, common
    surnames (Mitchell, Murray, Williams, Thompson, Brown, Johnson, ...)
    collide too much, and even the team-context disambiguator produced
    false positives when a post mentioned a team without naming the
    player. Test pins the new behavior.
    """
    players, teams = vocab
    p1, _ = detect_entities("Jamal Murray and the Nuggets pulled away.", players, teams)
    assert "jamal-murray" in p1
    assert "dejounte-murray" not in p1

    p2, _ = detect_entities("Dejounte Murray and the Pelicans got a big road win.", players, teams)
    assert "dejounte-murray" in p2
    assert "jamal-murray" not in p2

    # Bare "Murray" with team context: still doesn't tag (the conservative
    # new behavior).
    p3, t3 = detect_entities("Murray and the Nuggets pulled away.", players, teams)
    assert p3 == []
    assert "denver-nuggets" in t3


def test_bare_last_name_does_not_tag(vocab):
    """Common surnames must not auto-tag. Regression for the broader
    canonical: Mitchell / Williams / Thompson / Brown / Johnson are
    shared by multiple active players and a bare match would either
    false-positive or get silently dropped — both bad. Tagger now
    requires the full first+last."""
    players, teams = vocab
    for text in [
        "Mitchell played well",         # Donovan Mitchell / Mitchell Robinson
        "Williams traded to OKC",       # multiple Williamses
        "Thompson dropped 40",          # multiple Thompsons
        "Brown was incredible",         # multiple Browns
        "Johnson stepped up",           # multiple Johnsons
    ]:
        p, _ = detect_entities(text, players, teams)
        assert p == [], f"bare last name in {text!r} tagged {p}"


def test_full_first_last_name_disambiguates_shared_surnames(vocab):
    """The flip side: when the post DOES use the full name, both
    Mitchells / Robinsons resolve correctly to the right slug."""
    players, teams = vocab
    p1, _ = detect_entities("Mitchell Robinson injured his ankle", players, teams)
    assert "mitchell-robinson" in p1
    assert "donovan-mitchell" not in p1

    p2, _ = detect_entities("Donovan Mitchell scored 30", players, teams)
    assert "donovan-mitchell" in p2
    assert "mitchell-robinson" not in p2


def test_ambiguous_alias_collision_is_dropped(vocab):
    """'JB' is an alias for both Jalen Brunson and Jaylen Brown."""
    players, teams = vocab
    player_slugs, _ = detect_entities("JB went off tonight.", players, teams)
    assert "jalen-brunson" not in player_slugs
    assert "jaylen-brown" not in player_slugs


def test_ambiguous_alias_resolved_by_team(vocab):
    players, teams = vocab
    player_slugs, _ = detect_entities(
        "JB and the Knicks won by 12.", players, teams
    )
    assert "jalen-brunson" in player_slugs
    assert "jaylen-brown" not in player_slugs


# ---------------------------------------------------------------------------
# shards: read/write, dedup, validation
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """Redirect shard reads/writes to a tmp directory."""
    monkeypatch.setattr(shards, "DATA_DIR", tmp_path)
    return tmp_path


def _valid_item(item_id: str, published_at: str = "2026-05-21T14:30:00Z") -> dict:
    return {
        "id": item_id,
        "source": "bluesky",
        "published_at": published_at,
        "ingested_at": "2026-05-21T14:31:00Z",
        "url": "https://bsky.app/profile/example.bsky.social/post/abc",
        "title": "test post",
        "author": {"handle": "@example.bsky.social", "display_name": "Example"},
        "players": ["lebron-james"],
        "teams": ["los-angeles-lakers"],
    }


def test_shard_path_uses_data_dir(isolated_data_dir):
    p = shard_path("bluesky", "2026-05-21")
    assert p == isolated_data_dir / "bluesky" / "2026-05-21.json"


def test_load_shard_returns_fresh_envelope_when_missing(isolated_data_dir):
    shard = load_shard("bluesky", "2026-05-21")
    assert shard["date"] == "2026-05-21"
    assert shard["source"] == "bluesky"
    assert shard["items"] == []
    assert "generated_at" in shard


def test_save_and_load_round_trip(isolated_data_dir):
    shard = {
        "date": "2026-05-21",
        "source": "bluesky",
        "generated_at": "2026-05-21T14:30:00Z",
        "items": [_valid_item("bs-1")],
    }
    save_shard("bluesky", "2026-05-21", shard)
    loaded = load_shard("bluesky", "2026-05-21")
    assert loaded["date"] == "2026-05-21"
    assert loaded["source"] == "bluesky"
    assert len(loaded["items"]) == 1
    assert loaded["items"][0]["id"] == "bs-1"


def test_save_shard_pretty_prints_with_indent_2(isolated_data_dir):
    save_shard(
        "bluesky",
        "2026-05-21",
        {
            "date": "2026-05-21",
            "source": "bluesky",
            "generated_at": "2026-05-21T14:30:00Z",
            "items": [],
        },
    )
    raw = (isolated_data_dir / "bluesky" / "2026-05-21.json").read_text()
    assert '  "date"' in raw  # 2-space indent
    assert raw.endswith("\n")


def test_append_items_dedupes_by_id(isolated_data_dir):
    a = _valid_item("bs-1", "2026-05-21T10:00:00Z")
    b = _valid_item("bs-2", "2026-05-21T11:00:00Z")
    first = append_items("bluesky", "2026-05-21", [a, b])
    assert first == 2

    # Re-append the same items; nothing should be added.
    second = append_items("bluesky", "2026-05-21", [a, b])
    assert second == 0

    shard = load_shard("bluesky", "2026-05-21")
    assert [it["id"] for it in shard["items"]] == ["bs-1", "bs-2"]


def test_append_items_sorts_chronologically(isolated_data_dir):
    later = _valid_item("bs-late", "2026-05-21T20:00:00Z")
    earlier = _valid_item("bs-early", "2026-05-21T08:00:00Z")
    middle = _valid_item("bs-mid", "2026-05-21T14:00:00Z")
    append_items("bluesky", "2026-05-21", [later, earlier, middle])
    shard = load_shard("bluesky", "2026-05-21")
    assert [it["id"] for it in shard["items"]] == ["bs-early", "bs-mid", "bs-late"]


def test_validate_item_accepts_valid(isolated_data_dir):
    assert validate_item(_valid_item("bs-ok")) == []


def test_validate_item_catches_missing_fields():
    item = _valid_item("bs-x")
    del item["url"]
    del item["title"]
    errs = validate_item(item)
    assert any("url" in e for e in errs)
    assert any("title" in e for e in errs)


def test_validate_item_catches_wrong_id_prefix():
    item = _valid_item("yt-wrong")  # yt- prefix but source is bluesky
    errs = validate_item(item)
    assert any("must start with 'bs-'" in e for e in errs)


def test_validate_item_catches_unknown_source():
    item = _valid_item("bs-1")
    item["source"] = "tiktok"
    errs = validate_item(item)
    assert any("unknown source" in e for e in errs)


def test_validate_item_catches_author_shape():
    item = _valid_item("bs-1")
    item["author"] = {"handle": "@x"}  # missing display_name
    errs = validate_item(item)
    assert any("display_name" in e for e in errs)


def test_validate_item_catches_non_list_players():
    item = _valid_item("bs-1")
    item["players"] = "lebron-james"
    errs = validate_item(item)
    assert any("players must be a list" in e for e in errs)


# ---------------------------------------------------------------------------
# utils
# ---------------------------------------------------------------------------


_ISO_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_utc_now_iso_shape():
    assert _ISO_PATTERN.match(utc_now_iso())


def test_today_utc_date_shape():
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", today_utc_date())


def test_parse_to_iso_from_rss_pubdate():
    # RFC 822 with +0000
    out = parse_to_iso("Wed, 21 May 2026 14:30:00 +0000")
    assert out == "2026-05-21T14:30:00Z"


def test_parse_to_iso_from_rss_pubdate_with_gmt():
    out = parse_to_iso("Wed, 21 May 2026 14:30:00 GMT")
    assert out == "2026-05-21T14:30:00Z"


def test_parse_to_iso_converts_offset_to_utc():
    # 14:30 in -05:00 is 19:30 UTC
    out = parse_to_iso("2026-05-21T14:30:00-05:00")
    assert out == "2026-05-21T19:30:00Z"


def test_parse_to_iso_treats_naive_as_utc():
    out = parse_to_iso("2026-05-21T14:30:00")
    assert out == "2026-05-21T14:30:00Z"


def test_parse_to_iso_accepts_datetime():
    dt = datetime(2026, 5, 21, 14, 30, 0, tzinfo=timezone.utc)
    assert parse_to_iso(dt) == "2026-05-21T14:30:00Z"


def test_parse_to_iso_z_suffix_already():
    assert parse_to_iso("2026-05-21T14:30:00Z") == "2026-05-21T14:30:00Z"


# ---------------------------------------------------------------------------
# strip_html (promoted from poll_google_news / poll_reddit copies)
# ---------------------------------------------------------------------------


def test_strip_html_removes_tags_and_decodes_entities():
    assert strip_html("<p>Wemby&apos;s rise</p>") == "Wemby's rise"


def test_strip_html_collapses_whitespace_to_single_spaces():
    assert strip_html("<p>line1</p>\n  <p>line2</p>") == "line1 line2"


def test_strip_html_empty_inputs_return_empty_string():
    assert strip_html("") == ""
    assert strip_html(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Cluster C: expanded canonical (~530 active players) + headshot/team
# logo URL builders. Sanity checks against the shipped canonical so a
# rebuild that drops fields gets caught.
# ---------------------------------------------------------------------------


def test_canonical_has_active_roster_count(vocab):
    """Canonical should have ~500+ active players post-Cluster-C."""
    players, _ = vocab
    assert len(players) >= 500, (
        f"player canonical shrunk to {len(players)}; the active-roster "
        "rebuild may have failed"
    )


def test_every_player_has_team_and_headshot_filename(vocab):
    """Each entry should map to a known team slug and carry the
    upstream headshot filename so the frontend can build the URL."""
    players, teams = vocab
    bad = []
    for slug, info in players.items():
        if not info.get("team") or info["team"] not in teams:
            bad.append((slug, "team", info.get("team")))
        if not info.get("headshot_filename"):
            bad.append((slug, "headshot_filename"))
    assert not bad, f"first 5 issues: {bad[:5]}"


# ---------------------------------------------------------------------------
# Phase-3 polish-5: punctuation-tolerant tagging (Fix 4).
# Real-world posts drop apostrophes, swap hyphens for spaces, and elide
# periods. The tagger now indexes each name's punctuation variants so
# casual spellings tag the same slug as the canonical form.
# ---------------------------------------------------------------------------


def test_apostrophe_dropped_in_name_still_tags(vocab):
    players, teams = vocab
    p1, _ = detect_entities("DeAaron Fox leads both conference finals", players, teams)
    p2, _ = detect_entities("De'Aaron Fox leads both conference finals", players, teams)
    assert "deaaron-fox" in p1, "missing apostrophe must still tag"
    assert "deaaron-fox" in p2, "canonical apostrophe spelling must still tag"


def test_hyphen_swapped_for_space_still_tags(vocab):
    players, teams = vocab
    # "Trayce Jackson-Davis" with the hyphen elided as a space.
    p1, _ = detect_entities("Trayce Jackson Davis dunked", players, teams)
    p2, _ = detect_entities("Trayce Jackson-Davis dunked", players, teams)
    assert "trayce-jackson-davis" in p1
    assert "trayce-jackson-davis" in p2


def test_diacritic_player_resolves_to_ascii_slug(vocab):
    """Players with diacritics in the name (Jokić, Dončić, Schröder,
    Porziņģis) should resolve under the ASCII-folded slug AND tag
    correctly from the un-accented spelling that most posts use."""
    players, teams = vocab
    assert "nikola-jokic" in players
    assert "luka-doncic" in players
    assert "dennis-schroder" in players
    # The folded ASCII spelling must tag.
    p, _ = detect_entities("Nikola Jokic triple-double", players, teams)
    assert "nikola-jokic" in p


# ---------------------------------------------------------------------------
# Archive backfill: build_indexes re-tags at index time. Idempotent —
# running twice produces identical output.
# ---------------------------------------------------------------------------


def test_retag_items_is_idempotent(vocab):
    from scripts.build_indexes import _retag_items

    players, teams = vocab
    items = [
        {"title": "LeBron James scored 40 for the Lakers", "players": ["stale"], "teams": ["stale"]},
        {"title": "Nikola Jokic triple-double", "players": [], "teams": []},
        {"title": "Just some text with no entities", "players": ["lebron-james"], "teams": []},
    ]
    _retag_items(items, players, teams)
    snap = [dict(it) for it in items]
    _retag_items(items, players, teams)
    assert items == snap, "second retag run produced different output"
    # Stale tags wiped, correct ones added.
    assert "lebron-james" in items[0]["players"]
    assert "los-angeles-lakers" in items[0]["teams"]
    assert "stale" not in items[0]["players"]
    assert "nikola-jokic" in items[1]["players"]
    # No-entity title clears the stale tag.
    assert items[2]["players"] == []


def test_strip_html_preserves_unicode():
    assert strip_html("<p>Giannis 🦌 dunks</p>") == "Giannis 🦌 dunks"


# ---------------------------------------------------------------------------
# Polish-8: curated alias additions from data/sources/alternate_names.csv.
# Each test exercises a single alias added in this PR; together they
# document the active-player aliases we trust enough to ship and
# confirm that the safety-filtered rejections still don't tag.
# ---------------------------------------------------------------------------


def test_polish8_alias_wemby_tags_wembanyama(vocab):
    players, teams = vocab
    p, _ = detect_entities("Wemby is on fire tonight", players, teams)
    assert p == ["victor-wembanyama"]


def test_polish8_alias_shai_tags_sga(vocab):
    players, teams = vocab
    p, _ = detect_entities("Shai pulled up from 30 feet", players, teams)
    assert p == ["shai-gilgeous-alexander"]


def test_polish8_alias_jjj_tags_jaren_jackson_jr(vocab):
    players, teams = vocab
    p, _ = detect_entities("JJJ blocked four shots", players, teams)
    assert p == ["jaren-jackson-jr"]


def test_polish8_alias_jdub_tags_jalen_williams(vocab):
    players, teams = vocab
    p, _ = detect_entities("JDub stepped up in Game 7", players, teams)
    assert p == ["jalen-williams"]


def test_polish8_alias_maxey_tags_tyrese_maxey(vocab):
    players, teams = vocab
    p, _ = detect_entities("Maxey carried the offense", players, teams)
    assert p == ["tyrese-maxey"]


def test_polish8_alias_dame_lillard_tags_damian(vocab):
    players, teams = vocab
    p, _ = detect_entities("Dame Lillard hit the buzzer beater", players, teams)
    assert "damian-lillard" in p


def test_polish8_alias_kentavious_tags_kcp(vocab):
    players, teams = vocab
    p, _ = detect_entities("Kentavious knocked down five threes", players, teams)
    assert p == ["kentavious-caldwell-pope"]


def test_polish8_alias_kcp_tags_kentavious(vocab):
    players, teams = vocab
    p, _ = detect_entities("KCP locked up the perimeter", players, teams)
    assert p == ["kentavious-caldwell-pope"]


def test_polish8_alias_uncle_drew_tags_kyrie(vocab):
    players, teams = vocab
    p, _ = detect_entities("Uncle Drew put on a show", players, teams)
    assert p == ["kyrie-irving"]


def test_polish8_alias_chef_curry_tags_steph(vocab):
    players, teams = vocab
    p, _ = detect_entities("Chef Curry cooked again", players, teams)
    assert p == ["stephen-curry"]


def test_polish8_skipped_bald_eagle_does_not_tag_caruso(vocab):
    """'Bald Eagle' was a CSV alias for Alex Caruso but is in the
    safety blocklist (US national symbol; non-NBA news collides)."""
    players, teams = vocab
    p, _ = detect_entities("A bald eagle was spotted soaring", players, teams)
    assert "alex-caruso" not in p


def test_polish8_skipped_kai_does_not_tag_kyrie(vocab):
    """'Kai' was a CSV alias for Kyrie Irving but blocked as a common
    given name."""
    players, teams = vocab
    p, _ = detect_entities("Kai ate breakfast and went surfing", players, teams)
    assert "kyrie-irving" not in p


def test_polish8_skipped_dunn_does_not_tag(vocab):
    """'Dunn' was a CSV alias for Kris Dunn but skipped because the
    surname collides with multiple active players."""
    players, teams = vocab
    p, _ = detect_entities("Dunn played well off the bench", players, teams)
    assert p == []


def test_polish8_retired_player_kobe_does_not_tag(vocab):
    """Retired aliases from the CSV are filtered by active-only design;
    'Kobe' references should never tag a current canonical entry."""
    players, teams = vocab
    p, _ = detect_entities("Kobe-like footwork on that turnaround", players, teams)
    assert p == []
