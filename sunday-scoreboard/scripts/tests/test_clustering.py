"""Smoke tests for cluster_beats + rank_beats."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Make sibling scripts importable without installing the package.
_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from cluster_beats import cluster_beats  # noqa: E402
from rank_beats import filter_noise, rank_and_filter, rank_beats  # noqa: E402


BASE_TS = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def _item(iid, source, hours_offset, *, players=None, teams=None, title=None):
    return {
        "id": iid,
        "source": source,
        "published_at": (BASE_TS + timedelta(hours=hours_offset))
        .isoformat()
        .replace("+00:00", "Z"),
        "players": players or [],
        "teams": teams or [],
        "title": title or f"item {iid}",
    }


def test_cluster_groups_items_within_24h_window():
    items = [
        _item("a", "bluesky", 0, players=["wemby"]),
        _item("b", "reddit", 5, players=["wemby"]),
        _item("c", "google-news", 23, players=["wemby"]),
    ]
    beats = cluster_beats(items)
    assert len(beats) == 1
    assert beats[0].entity == "wemby"
    assert beats[0].mention_count == 3
    assert beats[0].source_count == 3


def test_cluster_splits_after_24h_gap():
    items = [
        _item("a", "bluesky", 0, players=["sga"]),
        _item("b", "reddit", 30, players=["sga"]),  # >24h after a
    ]
    beats = cluster_beats(items)
    assert len(beats) == 2
    assert all(b.mention_count == 1 for b in beats)


def test_cluster_handles_multi_entity_tags():
    """An article tagging both Wemby and the Spurs feeds both beats."""
    items = [
        _item("a", "google-news", 0, players=["wemby"], teams=["spurs"]),
        _item("b", "reddit", 2, players=["wemby"]),
        _item("c", "bluesky", 4, teams=["spurs"]),
    ]
    beats = cluster_beats(items)
    by_key = {(b.entity, b.entity_kind): b for b in beats}
    assert by_key[("wemby", "player")].mention_count == 2
    assert by_key[("spurs", "team")].mention_count == 2


def test_cluster_ignores_items_with_invalid_dates():
    items = [
        _item("a", "bluesky", 0, players=["wemby"]),
        {"id": "bad", "source": "bluesky", "published_at": "not-a-date", "players": ["wemby"]},
    ]
    beats = cluster_beats(items)
    assert len(beats) == 1
    assert beats[0].mention_count == 1


def test_filter_noise_drops_single_item_with_one_source():
    items = [_item("a", "bluesky", 0, players=["nobody"])]
    beats = cluster_beats(items)
    assert len(beats) == 1
    assert beats[0].mention_count == 1
    assert beats[0].source_count == 1
    filtered = filter_noise(beats)
    assert filtered == []  # below the 3-source bar for single-item beats


def test_filter_noise_keeps_multi_item_beats():
    items = [
        _item("a", "bluesky", 0, players=["wemby"]),
        _item("b", "reddit", 2, players=["wemby"]),
    ]
    beats = cluster_beats(items)
    assert filter_noise(beats) == beats


def test_rank_orders_by_mention_count():
    items = [
        # SGA: 4 mentions in window
        _item("s1", "bluesky", 0, players=["sga"]),
        _item("s2", "reddit", 1, players=["sga"]),
        _item("s3", "youtube", 2, players=["sga"]),
        _item("s4", "google-news", 3, players=["sga"]),
        # Wemby: 2 mentions in window
        _item("w1", "bluesky", 0, players=["wemby"]),
        _item("w2", "reddit", 1, players=["wemby"]),
    ]
    beats = cluster_beats(items)
    ranked = rank_beats(beats)
    assert ranked[0].entity == "sga"
    assert ranked[1].entity == "wemby"


def test_rank_truncates_to_top_n():
    items = []
    for i in range(20):
        items += [
            _item(f"x{i}-a", "bluesky", 0, players=[f"slug-{i}"]),
            _item(f"x{i}-b", "reddit", 1, players=[f"slug-{i}"]),
        ]
    beats = cluster_beats(items)
    ranked = rank_and_filter(beats, top_n=5)
    assert len(ranked) == 5


def test_rank_tie_break_uses_source_diversity():
    items = [
        # Wemby: 2 mentions, 2 distinct sources
        _item("w1", "bluesky", 0, players=["wemby"]),
        _item("w2", "reddit", 1, players=["wemby"]),
        # SGA: 2 mentions, both from bluesky (only 1 source)
        _item("s1", "bluesky", 0, players=["sga"]),
        _item("s2", "bluesky", 1, players=["sga"]),
    ]
    beats = cluster_beats(items)
    ranked = rank_beats(beats)
    # Same mention count → more-diverse-source wins.
    assert ranked[0].entity == "wemby"
    assert ranked[1].entity == "sga"
