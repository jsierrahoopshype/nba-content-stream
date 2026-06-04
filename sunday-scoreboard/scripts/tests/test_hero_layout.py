"""Tests for v2.1 collision-safe hero layout zones."""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from lib import draw, layout  # noqa: E402
from lib import format_specs as fs  # noqa: E402

# The longest / trickiest canonical names — these caused the overlap in
# the first render.
LONG_NAMES = [
    "Giannis Antetokounmpo",
    "Shai Gilgeous-Alexander",
    "Karl-Anthony Towns",
    "Nikola Jokić",
    "LeBron James",
]


def test_box_geometry_helpers():
    b = layout.Box(10, 20, 110, 70)
    assert b.width == 100 and b.height == 50
    assert b.cx == 60 and b.cy == 45


def test_intersects_detects_overlap_and_separation():
    a = layout.Box(0, 0, 100, 100)
    assert layout.intersects(a, layout.Box(50, 50, 150, 150))     # overlap
    assert not layout.intersects(a, layout.Box(100, 0, 200, 100))  # edge-adjacent
    assert not layout.intersects(a, layout.Box(120, 0, 200, 100))  # apart
    # pad enforces a gap: boxes 10px apart "intersect" at pad=20
    assert layout.intersects(a, layout.Box(110, 0, 200, 100), pad=20)


def _zones_for(spec, name, rank):
    fonts = {
        "rank": draw.font(fs.FONT_MONO_BOLD, int(spec.rank_font_size * 0.7)),
        "name": draw.font(fs.FONT_BOLD, spec.title_font_size),
        "sub": draw.font(fs.FONT_MONO, spec.meta_font_size),
        "count": draw.font(fs.FONT_MONO_BOLD, int(spec.title_font_size * 1.1)),
        "clabel": draw.font(fs.FONT_MONO, spec.meta_font_size),
    }
    name_fit = draw.fit_text_to_width(name, fonts["name"], spec.width - spec.pad * 2)
    return layout.hero_layout(
        spec,
        rank_size=draw.measure_text(f"#{rank}", fonts["rank"]),
        name_size=draw.measure_text(name_fit, fonts["name"]),
        sub_size=draw.measure_text("SAN ANTONIO SPURS", fonts["sub"]),
        count_size=draw.measure_text("9999", fonts["count"]),
        count_label_size=draw.measure_text("MENTIONS", fonts["clabel"]),
        pill_size=(int(spec.width * 0.8), spec.meta_font_size + 16),
    )


@pytest.mark.parametrize("name", LONG_NAMES)
@pytest.mark.parametrize("rank", [1, 3, 10])
def test_hero_zones_never_intersect(name, rank):
    spec = fs.get_format("square")
    zones = _zones_for(spec, name, rank)
    for (ka, a), (kb, b) in itertools.combinations(zones.items(), 2):
        assert not layout.intersects(a, b), f"{ka} overlaps {kb} for {name!r} #{rank}"


@pytest.mark.parametrize("name", LONG_NAMES)
def test_hero_zones_stay_inside_frame(name):
    spec = fs.get_format("square")
    zones = _zones_for(spec, name, 1)
    for key, b in zones.items():
        assert b.x0 >= 0 and b.y0 >= 0, f"{key} off the top/left"
        assert b.x1 <= spec.width and b.y1 <= spec.height, f"{key} off the bottom/right"


def test_portrait_capped_at_45_percent_height():
    spec = fs.get_format("square")
    assert layout.hero_portrait_diameter(spec) <= int(spec.height * 0.45)
