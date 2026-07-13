"""Tests for the additive override merge in build_players_canonical.

players.json is fully regenerated from nba-headshots' active.json each
run, so hand-added players (e.g. the incoming draft class) must be
re-merged via player_overrides.json after every rebuild. These tests
cover that merge (apply_overrides) plus a schema check of the shipped
overrides file.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts import build_players_canonical as bpc

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OVERRIDES_PATH = REPO_ROOT / "data" / "canonical" / "player_overrides.json"
TEAMS_PATH = REPO_ROOT / "data" / "canonical" / "teams.json"


def _active_entry(slug, aliases=None, nba_id=123, headshot="123-x.png"):
    return {
        "name": slug.replace("-", " ").title(),
        "full_name": slug.replace("-", " ").title(),
        "first_name": "X",
        "last_name": "Y",
        "team": "atlanta-hawks",
        "nba_id": nba_id,
        "headshot_filename": headshot,
        "aliases": aliases or [],
    }


def test_apply_overrides_adds_missing_slug_as_is():
    out = {"_meta": {}, "bam-adebayo": _active_entry("bam-adebayo")}
    overrides = {
        "koa-peat": {
            "name": "Koa Peat", "team": "phoenix-suns", "nba_id": None,
            "headshot_filename": None, "aliases": ["Peat"],
            "draft_status": "2026 draft class",
        }
    }
    added, superseded = bpc.apply_overrides(out, overrides)
    assert (added, superseded) == (1, 0)
    assert out["koa-peat"] == overrides["koa-peat"]  # added verbatim
    assert out["koa-peat"]["nba_id"] is None          # stays null


def test_apply_overrides_supersedes_but_merges_aliases():
    # nba-headshots has caught up: the real active entry exists already.
    out = {
        "_meta": {},
        "koa-peat": _active_entry("koa-peat", aliases=["KP"], nba_id=999, headshot="999-koa.png"),
    }
    overrides = {
        "koa-peat": {
            "name": "Koa Peat", "team": "phoenix-suns", "nba_id": None,
            "headshot_filename": None, "aliases": ["Peat", "KP"],
            "draft_status": "2026 draft class",
        }
    }
    added, superseded = bpc.apply_overrides(out, overrides)
    assert (added, superseded) == (0, 1)
    # Real active data WINS (nba_id / headshot preserved) ...
    assert out["koa-peat"]["nba_id"] == 999
    assert out["koa-peat"]["headshot_filename"] == "999-koa.png"
    # ... but the curated alias is merged in, without duplicating "KP".
    assert out["koa-peat"]["aliases"] == ["KP", "Peat"]
    # override's draft_status is NOT grafted onto the real record
    assert "draft_status" not in out["koa-peat"]


def test_apply_overrides_is_idempotent():
    out = {"_meta": {}, "existing": _active_entry("existing")}
    overrides = {
        "rookie": {"name": "Rookie", "team": "utah-jazz", "nba_id": None,
                   "headshot_filename": None, "aliases": ["Rook"],
                   "draft_status": "2026 draft class"},
    }
    bpc.apply_overrides(out, overrides)
    snapshot = json.loads(json.dumps(out))
    # Re-applying the same overrides changes nothing (no dup entries/aliases).
    bpc.apply_overrides(out, overrides)
    assert out == snapshot


def test_apply_overrides_skips_meta_key():
    out = {"_meta": {}}
    added, superseded = bpc.apply_overrides(out, {"_meta": {"x": 1}, "a-b": _active_entry("a-b")})
    assert added == 1 and superseded == 0
    assert "_meta" in out and out["_meta"] == {}  # untouched


def test_shipped_overrides_are_valid():
    """The committed player_overrides.json is well-formed: unique slugs,
    valid team refs, required keys, all draft-class (nba_id null)."""
    overrides = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    teams = json.loads(TEAMS_PATH.read_text(encoding="utf-8"))
    team_slugs = {k for k in teams if not k.startswith("_")}
    assert len(overrides) == 75
    required = {"name", "full_name", "first_name", "last_name", "team",
                "nba_id", "headshot_filename", "aliases", "draft_status"}
    for slug, e in overrides.items():
        assert required <= set(e), f"{slug} missing keys: {required - set(e)}"
        assert e["team"] in team_slugs, f"{slug}: bad team {e['team']}"
        assert e["nba_id"] is None and e["headshot_filename"] is None
        assert isinstance(e["aliases"], list)
    # Thomas Sorber deliberately excluded (already in players.json).
    assert "thomas-sorber" not in overrides


# ---------------------------------------------------------------------------
# alternate_names.csv wiring (issue #31): a rebuild must PRESERVE curated
# bare-surname aliases instead of stripping them. Those surnames are
# runtime-tagged only via players.json (the CSV is not read at tag time),
# so stripping them silently breaks tagging for ~48 stars.
# ---------------------------------------------------------------------------

ALT_NAMES_PATH = REPO_ROOT / "data" / "sources" / "alternate_names.csv"


def test_existing_aliases_keeps_curated_surname_drops_uncurated():
    prev = {
        "_meta": {},
        "tyrese-maxey": {"name": "Tyrese Maxey", "aliases": ["Maxey"]},          # curated surname
        "some-rookie": {"name": "Some Rookie", "aliases": ["Rookie"]},           # NOT curated
        "star-player": {"name": "Star Player", "aliases": ["Star", "Player"]},   # mix
    }
    alt_map = {"Tyrese Maxey": {"Maxey"}, "Star Player": {"Star"}}
    out = bpc._existing_aliases(prev, alt_map)
    assert out["tyrese-maxey"] == ["Maxey"]        # curated surname survives
    assert "some-rookie" not in out                # uncurated bare surname dropped -> empty
    assert out["star-player"] == ["Star"]          # "Player" (uncurated last name) dropped, "Star" kept


def test_existing_aliases_without_altmap_strips_all_bare_surnames():
    # Back-compat: no alt_map -> the original strip-everything behavior.
    prev = {"a-b": {"name": "A B", "aliases": ["B", "Nick"]}}
    assert bpc._existing_aliases(prev) == {"a-b": ["Nick"]}


def test_load_alternate_names_parses_matches(tmp_path):
    p = tmp_path / "alt.csv"
    p.write_text(
        'full_name,mentions_match\n'
        'Tyrese Maxey,Maxey\n'
        'Zion Williamson,"Zion,Williamson"\n',
        encoding="utf-8",
    )
    m = bpc._load_alternate_names(p)
    assert m["Tyrese Maxey"] == {"Maxey"}
    assert m["Zion Williamson"] == {"Zion", "Williamson"}


def test_shipped_csv_covers_the_curated_star_surnames():
    """The shipped alternate_names.csv must list the safe surnames the
    build relies on to survive a rebuild (spot-check a representative set
    of the ~48). If one drops out of the CSV, its rebuild-preservation
    would silently break — this catches that."""
    alt = bpc._load_alternate_names(ALT_NAMES_PATH)
    for full_name, surname in [
        ("Tyrese Maxey", "Maxey"), ("Jayson Tatum", "Tatum"),
        ("Kevin Durant", "Durant"), ("Victor Wembanyama", "Wembanyama"),
        ("Damian Lillard", "Lillard"), ("Joel Embiid", "Embiid"),
        ("Ja Morant", "Morant"),
    ]:
        assert surname in alt.get(full_name, set()), f"{surname} missing for {full_name}"
