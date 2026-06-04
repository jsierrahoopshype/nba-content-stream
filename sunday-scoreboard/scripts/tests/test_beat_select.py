"""Tests for v2.1 beat selection: players-only + per-player dedupe."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from cluster_beats import cluster_beats  # noqa: E402
from rank_beats import filter_noise, rank_beats  # noqa: E402
from lib import beat_select  # noqa: E402

BASE = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _item(iid, source, hours, *, players=None, teams=None):
    return {
        "id": iid,
        "source": source,
        "published_at": (BASE + timedelta(hours=hours)).isoformat().replace("+00:00", "Z"),
        "players": players or [],
        "teams": teams or [],
        "title": f"item {iid}",
    }


def test_players_only_drops_team_beats():
    items = [
        _item("a", "bluesky", 0, players=["wemby"], teams=["spurs"]),
        _item("b", "reddit", 2, players=["wemby"]),
        _item("c", "bluesky", 4, teams=["spurs"]),
    ]
    beats = cluster_beats(items)
    players = beat_select.players_only(beats)
    assert players  # at least the wemby beat
    assert all(b.entity_kind == "player" for b in players)
    assert all(b.entity != "spurs" for b in players)


def test_one_beat_per_player_keeps_highest_and_distinct():
    # Knicks-style defect: same player split into two beats >24h apart.
    items = [
        # knicks player beat #1 — 3 mentions (day 0)
        _item("k1", "bluesky", 0, players=["jalen-brunson"]),
        _item("k2", "reddit", 1, players=["jalen-brunson"]),
        _item("k3", "youtube", 2, players=["jalen-brunson"]),
        # knicks player beat #2 — 2 mentions (day 3, separate island)
        _item("k4", "bluesky", 72, players=["jalen-brunson"]),
        _item("k5", "reddit", 73, players=["jalen-brunson"]),
    ]
    beats = cluster_beats(items)
    assert len(beats) == 2  # two islands for the same player
    ranked = rank_beats(filter_noise(beats), top_n=99)
    deduped = beat_select.one_beat_per_player(ranked)
    assert len(deduped) == 1
    assert deduped[0].entity == "jalen-brunson"
    assert deduped[0].mention_count == 3  # the higher-mention island kept


def test_one_beat_per_player_preserves_rank_order_across_players():
    items = []
    # sga: 4 mentions
    for i, src in enumerate(["bluesky", "reddit", "youtube", "google-news"]):
        items.append(_item(f"s{i}", src, i, players=["sga"]))
    # wemby: 2 mentions
    items += [_item("w1", "bluesky", 0, players=["wemby"]), _item("w2", "reddit", 1, players=["wemby"])]
    beats = cluster_beats(items)
    ranked = rank_beats(filter_noise(beats), top_n=99)
    deduped = beat_select.one_beat_per_player(ranked)
    assert [b.entity for b in deduped] == ["sga", "wemby"]


def test_full_selection_yields_distinct_players():
    items = []
    # Make 12 players, some with two islands.
    for p in range(12):
        items += [
            _item(f"p{p}-a", "bluesky", 0, players=[f"player-{p}"]),
            _item(f"p{p}-b", "reddit", 1, players=[f"player-{p}"]),
        ]
        if p % 3 == 0:  # split some into a second island
            items.append(_item(f"p{p}-c", "bluesky", 72, players=[f"player-{p}"]))
    # plus a team that must be excluded
    items += [
        _item("t1", "bluesky", 0, teams=["lakers"]),
        _item("t2", "reddit", 1, teams=["lakers"]),
        _item("t3", "youtube", 2, teams=["lakers"]),
    ]
    raw = cluster_beats(items)
    player_beats = beat_select.players_only(raw)
    ranked = rank_beats(filter_noise(player_beats), top_n=len(player_beats))
    top = beat_select.one_beat_per_player(ranked)[:10]
    slugs = [b.entity for b in top]
    assert len(top) == 10
    assert len(set(slugs)) == 10           # all distinct
    assert all(s != "lakers" for s in slugs)  # no team beats
