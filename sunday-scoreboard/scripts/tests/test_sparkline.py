"""Tests for the v2 mention-spike sparkline math + rendering."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from lib import sparkline  # noqa: E402
from lib import format_specs as fs  # noqa: E402

WEEK_START = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)  # a Monday


def _item(day_offset, hour=12):
    ts = WEEK_START + timedelta(days=day_offset, hours=hour)
    return {"published_at": ts.isoformat().replace("+00:00", "Z")}


def test_daily_counts_buckets_by_day():
    items = [_item(0), _item(0), _item(2), _item(6)]
    counts = sparkline.daily_mention_counts(items, WEEK_START, days=7)
    assert counts == [2, 0, 1, 0, 0, 0, 1]


def test_daily_counts_length_is_always_days():
    assert len(sparkline.daily_mention_counts([], WEEK_START, days=7)) == 7


def test_daily_counts_ignores_out_of_window_and_bad_dates():
    items = [_item(-1), _item(7), {"published_at": "nonsense"}, _item(3)]
    counts = sparkline.daily_mention_counts(items, WEEK_START, days=7)
    assert counts == [0, 0, 0, 1, 0, 0, 0]


def test_day_labels_start_monday():
    labels = sparkline.day_labels(WEEK_START, days=7)
    assert labels == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def test_peak_index_returns_argmax():
    assert sparkline.peak_index([1, 5, 3]) == 1
    assert sparkline.peak_index([5, 5, 3]) == 0  # first wins on tie


def test_peak_index_minus_one_for_empty_or_zero():
    assert sparkline.peak_index([]) == -1
    assert sparkline.peak_index([0, 0, 0]) == -1


def test_normalize_series_scales_to_height_per_series():
    assert sparkline.normalize_series([0, 5, 10], 100) == [0.0, 50.0, 100.0]
    # all-zero series collapses to baseline, no divide-by-zero
    assert sparkline.normalize_series([0, 0], 100) == [0.0, 0.0]


def test_sparkline_points_map_into_box_and_invert_y():
    pts = sparkline.sparkline_points([0, 10], (0, 0, 100, 100))
    assert pts[0] == (0, 100)    # value 0 → bottom of box
    assert pts[1] == (100, 0)    # peak value → top of box


def test_sparkline_points_single_value_pins_left():
    pts = sparkline.sparkline_points([4], (10, 0, 110, 100))
    assert pts == [(10, 0)]


def test_draw_progress_monotonic_and_endpoints():
    n = 7
    assert sparkline.draw_progress(n, 0.0) == (1, 0.0)
    full_end, partial_end = sparkline.draw_progress(n, 1.0)
    assert full_end == n and partial_end == 0.0
    # midpoint draws roughly half the segments
    full_mid, _ = sparkline.draw_progress(n, 0.5)
    assert 1 < full_mid < n


def test_draw_sparkline_smoke_renders_without_error():
    spec = fs.get_format("square")
    from lib import draw
    canvas = draw.new_canvas(spec)
    sparkline.draw_sparkline(
        canvas, (60, 200, 1020, 600),
        [3, 8, 2, 12, 5, 1, 7], 1.0,
        labels=sparkline.day_labels(WEEK_START),
        peak_callout="Thursday: +12 mentions",
    )
    # Canvas stays the right size and mode; rendering touched pixels.
    assert canvas.size == (spec.width, spec.height)
    assert canvas.mode == "RGBA"
