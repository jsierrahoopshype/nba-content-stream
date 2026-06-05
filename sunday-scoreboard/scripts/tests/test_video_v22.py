"""Tests for v2.2 orchestration: countdown order, cut timeline, pacing,
outro copy."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import render_video_v2 as rv  # noqa: E402
import render_beat_v2 as rb  # noqa: E402
from render_outro_v2 import outro_headline, OUTRO_SECONDS  # noqa: E402
from render_coldopen_v2 import COLD_OPEN_SECONDS  # noqa: E402
from render_cta_v2 import CTA_SECONDS  # noqa: E402


def test_countdown_order_is_descending_rank():
    beats = [SimpleNamespace(rank=r) for r in (1, 2, 3, 4, 5)]
    order = rv.countdown_order(beats)
    assert [b.rank for b in order] == [5, 4, 3, 2, 1]  # #1 last (payoff)


def test_beat_phase_plan_payoff_is_longer_and_within_budget():
    normal = rb.beat_seconds(False)
    payoff = rb.beat_seconds(True)
    assert normal == pytest.approx(8.0)         # hero 2.5 + quote 3.5 + spark 2.0
    assert payoff == pytest.approx(10.0)        # +2.0s for the finale
    # hard requirement: every beat in the 6–10.5s window
    assert 6.0 <= normal <= 10.5
    assert 6.0 <= payoff <= 10.5


def test_phase_plan_names_and_order():
    assert [p for p, _ in rb.beat_phase_plan(False)] == ["hero", "quote", "spark"]


def test_build_cut_timeline_is_contiguous_and_cumulative():
    segs = [("a", 3.0), ("b", 8.0), ("c", 2.5)]
    tl = rv.build_cut_timeline(segs)
    assert [c["label"] for c in tl] == ["a", "b", "c"]
    assert tl[0]["start"] == 0.0 and tl[0]["end"] == 3.0
    assert tl[1]["start"] == 3.0 and tl[1]["end"] == 11.0
    assert tl[2]["end"] == 13.5
    # contiguous: each start == previous end
    for prev, nxt in zip(tl, tl[1:]):
        assert nxt["start"] == prev["end"]


def test_total_runtime_top5_under_budget():
    # cold open + 4 normal beats + 1 payoff + outro + cta ≤ ~50s
    total = COLD_OPEN_SECONDS + 4 * rb.beat_seconds(False) + rb.beat_seconds(True) + OUTRO_SECONDS + CTA_SECONDS
    assert total <= 51.0


def test_total_runtime_top10_under_budget():
    total = COLD_OPEN_SECONDS + 9 * rb.beat_seconds(False) + rb.beat_seconds(True) + OUTRO_SECONDS + CTA_SECONDS
    assert total <= 95.0


def test_outro_headline_uses_actual_n():
    assert outro_headline(5) == "Top 5 by mentions"
    assert outro_headline(3) == "Top 3 by mentions"
    assert outro_headline(10) == "Top 10 by mentions"
