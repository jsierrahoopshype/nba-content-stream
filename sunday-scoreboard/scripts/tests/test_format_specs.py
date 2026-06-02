"""Smoke tests for format_specs + source_styling."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from lib import format_specs as fs  # noqa: E402
from lib import source_styling  # noqa: E402


def test_three_formats_registered():
    assert set(fs.FORMAT_SPECS) == {"horizontal", "square", "vertical"}


def test_get_format_rejects_unknown_key():
    with pytest.raises(ValueError):
        fs.get_format("portrait")


def test_horizontal_is_landscape():
    spec = fs.get_format("horizontal")
    assert spec.width > spec.height
    assert spec.aspect > 1.7  # ~16:9


def test_square_is_square():
    spec = fs.get_format("square")
    assert spec.width == spec.height
    assert spec.aspect == pytest.approx(1.0)


def test_vertical_is_portrait():
    spec = fs.get_format("vertical")
    assert spec.height > spec.width
    assert spec.aspect < 0.6


def test_beat_seconds_sums_phases():
    spec = fs.get_format("square")
    expected = (
        spec.title_seconds
        + spec.headlines_seconds
        + spec.reporters_seconds
        + spec.transition_seconds
    )
    assert spec.beat_seconds == expected


def test_hex_to_rgb_round_trip():
    assert fs.hex_to_rgb("#3b82f6") == (59, 130, 246)
    assert fs.hex_to_rgb("ffffff") == (255, 255, 255)
    assert fs.hex_to_rgb("#000000") == (0, 0, 0)


def test_fonts_exist_on_disk():
    """The repo ships the TTFs under assets/fonts/. If a font is
    missing the entire render fails — assert at test time so the CI
    surfaces it cleanly instead of crashing inside Pillow."""
    for path in (fs.FONT_REGULAR, fs.FONT_BOLD, fs.FONT_MONO, fs.FONT_MONO_BOLD):
        assert path.exists(), f"missing font: {path}"


def test_known_sources_have_styles():
    for src in source_styling.ALL_SOURCES:
        style = source_styling.source_style(src)
        assert style.color.startswith("#")
        assert style.label  # non-empty label


def test_unknown_source_falls_back_to_neutral():
    style = source_styling.source_style("podcast-bro")
    assert style.label == "podcast-bro"
    assert style.color == "#71717a"
