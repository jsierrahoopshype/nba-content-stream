"""Tests for v2.1 quote quality filters, cleaning, and truncation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from lib import quote_filter as qf  # noqa: E402
from lib.engagement_score import Engagement, at_uri_from_item  # noqa: E402


# ---- emoji / unrenderable stripping ----


def test_strip_unrenderable_removes_emoji():
    assert qf.strip_unrenderable("Game day 🏀🔥") == "Game day "
    assert "□" not in qf.clean_text("flag 🇺🇸 here")


def test_strip_preserves_accented_latin_names():
    # French, Serbian (Latin), Turkish, Croatian — all must survive.
    names = "Nikola Jokić Bogdan Bogdanović Furkan Korkmaz Théo Maledon Dāvis Bertāns"
    assert qf.strip_unrenderable(names) == names


def test_clean_text_collapses_whitespace():
    assert qf.clean_text("a   b\n\nc  🏀 d") == "a b c d"


def test_emoji_caps_ratio_flags_shouty_marketing():
    assert qf.is_mostly_emoji_or_caps("KNICKS. SPURS. GAME DAY 🏀")
    assert not qf.is_mostly_emoji_or_caps(
        "Brunson was unreal tonight, carrying the Knicks down the stretch."
    )


# ---- truncation ----


def test_truncate_at_sentence_cuts_on_boundary_no_ellipsis():
    text = "Brunson went off. The Knicks needed every bit of it tonight."
    out = qf.truncate_at_sentence(text, 30)
    assert out == "Brunson went off."
    assert "…" not in out


def test_truncate_falls_back_to_word_boundary_with_ellipsis():
    text = "Brunson was pulling up from way downtown and it kept falling"
    out = qf.truncate_at_sentence(text, 25)
    assert out.endswith("…")
    # never a mid-word fragment: the char before the ellipsis ends a word
    assert not out[:-1].endswith(" ")
    assert " " in out  # cut at a word boundary, not the very first token


def test_truncate_returns_unchanged_when_it_fits():
    assert qf.truncate_at_sentence("short enough", 50) == "short enough"


# ---- roster + blocklist ----


_ROSTER_CSV = """Handle,Display Name,DID
woj.bsky.social,Adrian Wojnarowski,did:plc:aaa
shamscharania.bsky.social,Shams Charania,did:plc:bbb

FredKatz.bsky.social,Fred Katz,did:plc:ccc
"""


def test_parse_roster_lowercases_and_skips_header_and_blanks():
    roster = qf.parse_roster(_ROSTER_CSV)
    assert roster == {"woj.bsky.social", "shamscharania.bsky.social", "fredkatz.bsky.social"}
    assert "handle" not in roster


def test_is_blocked_handle_matches_official_and_teams():
    block = {"nba.com", "lakers.bsky.social"}
    assert qf.is_blocked_handle("nba.com", block)
    assert qf.is_blocked_handle("lakers.bsky.social", block)
    assert qf.is_blocked_handle("anything.nba.com", block)       # subdomain rule
    assert qf.is_blocked_handle("nbacom.nba.com", {})            # nba.com auto-block
    assert not qf.is_blocked_handle("woj.bsky.social", block)


def test_load_blocklist_reads_shipped_config():
    block = qf.load_blocklist()
    assert "nba.com" in block
    assert "nuggets.bsky.social" in block


# ---- filters ----


def _post(handle, text, **over):
    item = {
        "source": "bluesky",
        "author_handle": handle,
        "id": "bs-did%3Aplc%3Ax%2Fapp.bsky.feed.post%2Faaa",
        "url": f"https://bsky.app/profile/{handle}/post/aaa",
        "published_at": "2026-06-01T12:00:00Z",
        "title": text,
    }
    item.update(over)
    return item


ROSTER = {"woj.bsky.social", "shamscharania.bsky.social"}
BLOCK = {"nba.com"}
GOOD = "Brunson was sensational tonight and the Knicks look like real contenders now."


def test_passes_filters_accepts_roster_reporter_with_substance():
    assert qf.passes_filters(_post("woj.bsky.social", GOOD), roster=ROSTER, blocklist=BLOCK)


def test_passes_filters_rejects_official_account():
    assert not qf.passes_filters(
        _post("nba.com", GOOD), roster=ROSTER | {"nba.com"}, blocklist=BLOCK
    )


def test_passes_filters_rejects_non_roster_handle():
    assert not qf.passes_filters(_post("randomfan.bsky.social", GOOD), roster=ROSTER, blocklist=BLOCK)


def test_passes_filters_rejects_short_post():
    assert not qf.passes_filters(_post("woj.bsky.social", "Wow."), roster=ROSTER, blocklist=BLOCK)


def test_passes_filters_rejects_mostly_caps():
    assert not qf.passes_filters(
        _post("woj.bsky.social", "KNICKS SPURS GAME DAY LETS GO BABY HUGE"),
        roster=ROSTER, blocklist=BLOCK,
    )


# ---- selection ----


def test_select_quote_picks_best_roster_survivor():
    a = _post("woj.bsky.social", GOOD, id="bs-did%3Aplc%3Ax%2Fapp.bsky.feed.post%2Faaa")
    b = _post("shamscharania.bsky.social", GOOD + " More detail here.",
              id="bs-did%3Aplc%3Ax%2Fapp.bsky.feed.post%2Fbbb")
    official = _post("nba.com", GOOD, id="bs-did%3Aplc%3Ax%2Fapp.bsky.feed.post%2Fccc")
    eng = {
        at_uri_from_item(a): Engagement(likes=10, reposts=0, replies=0),   # 10
        at_uri_from_item(b): Engagement(likes=0, reposts=0, replies=20),   # 60
        at_uri_from_item(official): Engagement(likes=9999, reposts=9999, replies=9999),
    }
    chosen = qf.select_quote([a, b, official], eng, roster=ROSTER, blocklist=BLOCK)
    assert chosen is not None
    item, _ = chosen
    assert item is b  # highest-scoring *roster* post; official excluded despite huge eng


def test_select_quote_none_when_all_filtered():
    official = _post("nba.com", GOOD)
    fan = _post("randomfan.bsky.social", GOOD)
    assert qf.select_quote([official, fan], {}, roster=ROSTER, blocklist=BLOCK) is None


# ---- word-safe wrap + prepare ----


def _char_measure(s):
    return len(s)  # 1 unit per char — font-free, deterministic


def test_wrap_to_lines_never_splits_words():
    text = "the quick brown fox jumps over the lazy dog again"
    lines = qf.wrap_to_lines(text, _char_measure, 12, max_lines=5)
    for ln in lines:
        assert len(ln) <= 12
        for word in ln.split():
            assert word in text.split()  # whole words only


def test_prepare_quote_lines_truncates_within_budget_no_midword():
    text = "Brunson was unbelievable tonight. He carried the Knicks. They win big."
    lines = qf.prepare_quote_lines(text, _char_measure, 20, max_lines=2)
    assert len(lines) <= 2
    for ln in lines:
        assert len(ln) <= 20
    joined = " ".join(lines)
    # ends cleanly: sentence punctuation or ellipsis, never a partial word
    assert joined.endswith(".") or joined.endswith("…")


def test_prepare_quote_lines_strips_emoji_first():
    lines = qf.prepare_quote_lines("Clutch 🏀 shot by Brunson", _char_measure, 100, max_lines=3)
    assert "🏀" not in " ".join(lines)


# ---- REAL-FORMAT regression (the v2.1 production 100%-rejection bug) ----
#
# Handles + roster rows below are copied VERBATIM from production:
#   - roster rows from data/sources/bluesky_handles.csv
#   - the three author-storage shapes the archive actually uses:
#       index files  → author is a display-name STRING; handle only in `url`
#       daily shards → author is a DICT with author.handle
#       (legacy)     → flat author_handle
# The original gate read only `author_handle`, which is absent from index
# items, so it rejected 100% of real candidates. These assert the join
# works across all three shapes against the real roster format.

_REAL_ROSTER_CSV = """Handle,Display Name,DID
keithsmithnba.bsky.social,KeithSmithNBA,did:plc:luofuy2uw7g4vsd2vbwidtel
btrowland.bsky.social,Brad Rowland,did:plc:yvy5ax23zpmoearxn6xsgqzf
norlander.bsky.social,Matt Norlander,did:plc:eltikd6go53huetqxq5hsikx
"""

# Index-file shape (what gather_week actually feeds the gate): author is a
# display string, NO author_handle, handle recoverable only from the url.
_REAL_INDEX_ITEM = {
    "id": "bs-did%3Aplc%3Aluofuy2uw7g4vsd2vbwidtel%2Fapp.bsky.feed.post%2F3mnfqa7iwmc24",
    "source": "bluesky",
    "author": "KeithSmithNBA",
    "url": "https://bsky.app/profile/keithsmithnba.bsky.social/post/3mnfqa7iwmc24",
    "title": "Wembanyama was the best player on the floor again tonight, full stop.",
    "published_at": "2026-06-01T20:00:00Z",
}
# Daily-shard shape: author is a dict with the handle.
_REAL_SHARD_ITEM = {
    "id": "bs-did%3Aplc%3Ayvy5ax23zpmoearxn6xsgqzf%2Fapp.bsky.feed.post%2F3mnbi5ikbqs2u",
    "source": "bluesky",
    "author": {"handle": "btrowland.bsky.social", "display_name": "Brad Rowland"},
    "url": "https://bsky.app/profile/btrowland.bsky.social/post/3mnbi5ikbqs2u",
    "title": "Brunson carried the Knicks down the stretch with a clinic in shot-making.",
    "published_at": "2026-06-01T21:00:00Z",
}
# Legacy flat shape.
_REAL_FLAT_ITEM = {
    "id": "bs-x",
    "source": "bluesky",
    "author_handle": "norlander.bsky.social",
    "url": "https://bsky.app/profile/norlander.bsky.social/post/3mnbivh6tqk2j",
    "title": "Curry's gravity warped the entire defense again, even on an off shooting night.",
    "published_at": "2026-06-01T22:00:00Z",
}


def test_handle_of_resolves_every_real_storage_shape():
    assert qf._handle_of(_REAL_INDEX_ITEM) == "keithsmithnba.bsky.social"
    assert qf._handle_of(_REAL_SHARD_ITEM) == "btrowland.bsky.social"
    assert qf._handle_of(_REAL_FLAT_ITEM) == "norlander.bsky.social"


def test_real_candidates_pass_the_roster_gate():
    """The exact failure from production: real items must NOT be rejected
    by the roster gate built from the real CSV."""
    roster = qf.parse_roster(_REAL_ROSTER_CSV)
    block = qf.load_blocklist()
    for item in (_REAL_INDEX_ITEM, _REAL_SHARD_ITEM, _REAL_FLAT_ITEM):
        assert qf._handle_of(item) in roster, item.get("url")
        assert qf.passes_filters(item, roster=roster, blocklist=block), item.get("url")


def test_normalize_handle_strips_at_case_and_whitespace():
    assert qf.normalize_handle("  @KeithSmithNBA.bsky.social  ") == "keithsmithnba.bsky.social"
    assert qf.normalize_handle("BTRowland.bsky.social") == "btrowland.bsky.social"
    assert qf.normalize_handle(None) == ""


def test_custom_domain_roster_handle_joins():
    # Not every roster handle ends in .bsky.social (e.g. basketball-reference.com);
    # the gate must not assume a suffix.
    roster = qf.parse_roster("Handle,Display Name,DID\nbasketball-reference.com,Basketball Reference,did:plc:z\n")
    item = {
        "source": "bluesky",
        "author": "Basketball Reference",
        "url": "https://bsky.app/profile/basketball-reference.com/post/3abc",
        "title": "Wembanyama's per-36 numbers this week are historically absurd across the board.",
    }
    assert qf._handle_of(item) == "basketball-reference.com"
    assert qf.passes_filters(item, roster=roster, blocklist=set())


def test_select_quote_staged_reports_stage_counts():
    roster = qf.parse_roster(_REAL_ROSTER_CSV)
    block = qf.load_blocklist()
    junk = {  # off-roster + shouty → rejected at roster stage
        "source": "bluesky", "author_handle": "randomfan.bsky.social",
        "url": "https://bsky.app/profile/randomfan.bsky.social/post/3z",
        "title": "GAME DAY LETS GO",
    }
    cands = [_REAL_INDEX_ITEM, _REAL_SHARD_ITEM, junk]
    from lib.engagement_score import Engagement, at_uri_from_item
    eng = {at_uri_from_item(_REAL_INDEX_ITEM): Engagement(likes=400, reposts=6, replies=0)}
    chosen, stages = qf.select_quote_staged(cands, eng, roster=roster, blocklist=block)
    assert stages.candidates == 3
    assert stages.after_roster == 2      # junk dropped at roster gate
    assert stages.after_blocklist == 2
    assert stages.after_quality == 2
    assert stages.picked is True
    line = stages.log_line("wembanyama")
    assert "3 bsky candidates" in line and "roster 2" in line and "picked eng=" in line
