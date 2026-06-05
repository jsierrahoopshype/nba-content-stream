"""Tests for v2.2 animation math (counter, slide, block fade)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from lib import anim  # noqa: E402


def test_count_up_endpoints():
    assert anim.count_up(0.0, 100, 0.8) == 0
    assert anim.count_up(0.8, 100, 0.8) == 100
    assert anim.count_up(5.0, 100, 0.8) == 100   # past duration clamps
    assert anim.count_up(-1.0, 100, 0.8) == 0


def test_count_up_is_monotonic_non_decreasing():
    vals = [anim.count_up(i / 20 * 0.8, 100, 0.8) for i in range(21)]
    assert all(b >= a for a, b in zip(vals, vals[1:]))
    assert max(vals) == 100


def test_count_up_zero_total_and_zero_duration():
    assert anim.count_up(0.4, 0, 0.8) == 0
    assert anim.count_up(0.0, 50, 0.0) == 50  # instant


def test_slide_offset_starts_at_distance_ends_at_zero():
    assert anim.slide_offset(0.0, 120.0, 0.3) == pytest.approx(120.0)
    assert anim.slide_offset(0.3, 120.0, 0.3) == 0.0
    assert anim.slide_offset(1.0, 120.0, 0.3) == 0.0


def test_block_alpha_reaches_one_within_fade():
    assert anim.block_alpha(0.0, fade_in=0.35) == 0.0
    assert anim.block_alpha(0.35, fade_in=0.35) == pytest.approx(1.0)
    assert anim.block_alpha(1.0, fade_in=0.35) == pytest.approx(1.0)


def test_block_alpha_fast_enough_for_retention_rule():
    # v2.2 rule: legible well under 0.4s — alpha should be high by 0.35s.
    assert anim.block_alpha(0.3, fade_in=0.35) > 0.9
