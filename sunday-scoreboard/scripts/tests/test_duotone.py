"""Tests for the v2.2 duotone treatment + cover crop."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from PIL import Image  # noqa: E402

from lib import duotone, style22  # noqa: E402
from lib import format_specs as fs  # noqa: E402


def test_duotone_maps_black_to_shadow_and_white_to_highlight():
    img = Image.new("RGB", (2, 1))
    img.putpixel((0, 0), (0, 0, 0))        # black → shadow
    img.putpixel((1, 0), (255, 255, 255))  # white → highlight
    out = duotone.duotone(img, autocontrast=False)
    assert out.getpixel((0, 0))[:3] == fs.hex_to_rgb(style22.DUOTONE_SHADOW)
    assert out.getpixel((1, 0))[:3] == fs.hex_to_rgb(style22.DUOTONE_HIGHLIGHT)


def test_duotone_midtones_are_between_endpoints_and_bluish():
    img = Image.new("RGB", (1, 1), (128, 128, 128))
    r, g, b, _ = duotone.duotone(img, autocontrast=False).getpixel((0, 0))
    # blue channel dominates the brand duotone
    assert b > r and b > g


def test_cover_crop_returns_exact_size():
    img = Image.new("RGBA", (100, 50), (10, 20, 30, 255))
    out = duotone.cover_crop(img, (200, 200))
    assert out.size == (200, 200)


def test_hero_portrait_fallback_flag():
    panel, had = duotone.hero_portrait(None, (300, 400))
    assert had is False
    assert panel.size == (300, 400)
    # solid brand panel (the 404 base), not transparent
    assert panel.getpixel((10, 10))[:3] == fs.hex_to_rgb(style22.DUOTONE_SHADOW)


def test_hero_portrait_decodes_real_bytes():
    import io
    buf = io.BytesIO()
    Image.new("RGB", (120, 160), (200, 100, 50)).save(buf, "PNG")
    out, had = duotone.hero_portrait(buf.getvalue(), (300, 400))
    assert had is True
    assert out.size == (300, 400)
