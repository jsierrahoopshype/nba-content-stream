"""Tests for v2 parallax + Ken Burns helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from PIL import Image  # noqa: E402

from lib import parallax  # noqa: E402
from lib import format_specs as fs  # noqa: E402


def test_zoom_scale_endpoints():
    assert parallax.zoom_scale(0.0) == pytest.approx(1.0)
    assert parallax.zoom_scale(1.0) == pytest.approx(0.6)


def test_zoom_scale_is_monotonic_decreasing():
    vals = [parallax.zoom_scale(t / 10) for t in range(11)]
    assert all(b <= a + 1e-9 for a, b in zip(vals, vals[1:]))


def test_parallax_offset_slower_layer_moves_less():
    # Same distance, lower speed → smaller magnitude offset.
    fast = parallax.parallax_offset(1.0, distance=-200.0, speed=1.0)
    slow = parallax.parallax_offset(1.0, distance=-200.0, speed=0.3)
    assert abs(slow) < abs(fast)
    assert slow == pytest.approx(-60.0)  # -200 * 1.0(eased) * 0.3


def test_parallax_offset_zero_at_start():
    assert parallax.parallax_offset(0.0, distance=-200.0, speed=0.3) == 0.0


def test_letter_stagger_alpha_progression():
    # Letter 0 is fully in before letter 5 has started.
    assert parallax.letter_stagger_alpha(0, 0.30, per_letter=0.05, fade_in=0.25) == pytest.approx(1.0)
    assert parallax.letter_stagger_alpha(5, 0.05, per_letter=0.05, fade_in=0.25) == 0.0
    # A later letter eventually reaches full alpha given enough time.
    assert parallax.letter_stagger_alpha(5, 1.0, per_letter=0.05, fade_in=0.25) == pytest.approx(1.0)


def test_ken_burns_offset_runs_symmetric_around_zero():
    start = parallax.ken_burns_offset(0.0, pan_px=40.0)
    end = parallax.ken_burns_offset(1.0, pan_px=40.0)
    assert start == pytest.approx(-20.0)
    assert end == pytest.approx(20.0)


def test_vertical_gradient_size_and_endpoints():
    grad = parallax.vertical_gradient((50, 200))
    assert grad.size == (50, 200)
    assert grad.mode == "RGBA"
    top = grad.getpixel((25, 0))
    bottom = grad.getpixel((25, 199))
    assert top[:3] == fs.hex_to_rgb(parallax.GRADIENT_TOP)
    assert bottom[:3] == fs.hex_to_rgb(parallax.GRADIENT_BOTTOM)


def test_cover_scale_fills_canvas():
    # A 100x100 image covering a 200x100 canvas needs 2x scale.
    assert parallax.cover_scale((100, 100), (200, 100)) == pytest.approx(2.0)


def test_place_scaled_returns_canvas_sized_layer():
    img = Image.new("RGBA", (100, 100), (255, 0, 0, 255))
    out = parallax.place_scaled(img, (400, 300), 0.5, offset=(10, -10))
    assert out.size == (400, 300)
    assert out.mode == "RGBA"


def test_ken_burns_returns_canvas_size():
    img = Image.new("RGBA", (300, 300), (0, 128, 255, 255))
    out = parallax.ken_burns(img, (200, 100), 0.5, pan_px=20.0)
    assert out.size == (200, 100)


def test_apply_layer_alpha_scales_alpha_channel():
    layer = Image.new("RGBA", (10, 10), (255, 255, 255, 200))
    dimmed = parallax.apply_layer_alpha(layer, 0.5)
    assert dimmed.getpixel((0, 0))[3] == 100
    # alpha >= 1 returns the same object (no-op fast path)
    assert parallax.apply_layer_alpha(layer, 1.0) is layer
    # alpha <= 0 returns fully transparent
    assert parallax.apply_layer_alpha(layer, 0.0).getpixel((0, 0))[3] == 0
