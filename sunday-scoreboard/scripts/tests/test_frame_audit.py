"""Tests for the v2.2 dead-air auditor + a render-time regression guard
that the assembled v2.2 phases are never mostly-empty background."""

from __future__ import annotations

import io
import sys
from datetime import datetime
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from lib import frame_audit  # noqa: E402
from lib import format_specs as fs  # noqa: E402


def _solid(color, size=(40, 40)):
    return np.full((size[1], size[0], 3), color, dtype=np.uint8)


def test_background_fraction_all_background_is_one():
    bg = fs.hex_to_rgb(fs.BACKGROUND)
    assert frame_audit.background_fraction(_solid(bg)) == pytest.approx(1.0)


def test_background_fraction_accent_is_zero():
    assert frame_audit.background_fraction(_solid(fs.hex_to_rgb(fs.ACCENT))) == 0.0


def test_white_card_does_not_count_as_background():
    # White (#ffffff) is content, not the #f5f5f7 app background.
    assert frame_audit.background_fraction(_solid((255, 255, 255))) == 0.0


def test_background_fraction_half_and_half():
    bg = fs.hex_to_rgb(fs.BACKGROUND)
    frame = np.concatenate([_solid(bg, (40, 40)), _solid(fs.hex_to_rgb(fs.ACCENT), (40, 40))], axis=1)
    assert frame_audit.background_fraction(frame) == pytest.approx(0.5)


class _FakeClip:
    """Minimal clip: empty bg until t>=flip, then full accent."""

    def __init__(self, duration, flip):
        self.duration = duration
        self.flip = flip
        self._bg = _solid(fs.hex_to_rgb(fs.BACKGROUND), (20, 20))
        self._fg = _solid(fs.hex_to_rgb(fs.ACCENT), (20, 20))

    def get_frame(self, t):
        return self._bg if t < self.flip else self._fg


def test_max_background_fraction_finds_worst_frame():
    worst, when = frame_audit.max_background_fraction(_FakeClip(3.0, 1.0), interval=0.5)
    assert worst == pytest.approx(1.0)
    assert when < 1.0  # the empty early frame


def test_assert_no_dead_air_raises_on_empty_clip():
    with pytest.raises(AssertionError):
        frame_audit.assert_no_dead_air(_FakeClip(2.0, 5.0), interval=0.5, label="empty")


def test_assert_no_dead_air_passes_on_full_clip():
    frame_audit.assert_no_dead_air(_FakeClip(2.0, -1.0), interval=0.5)  # always foreground


# --- Render-time regression guard: real v2.2 phases on square ---


def _fake_portrait():
    buf = io.BytesIO()
    Image.new("RGB", (300, 400), (120, 90, 60)).save(buf, "PNG")
    return buf.getvalue()


def test_assembled_v22_phases_have_no_dead_air():
    import render_beat_v2 as rb
    import render_coldopen_v2 as rc
    import render_outro_v2 as ro
    import render_cta_v2 as rcta
    from lib.canonical_lookup import EntityInfo
    from lib.engagement_score import Engagement
    from lib.reporter_lookup import Reporter

    spec = fs.get_format("square")
    pb = _fake_portrait()
    beat = rb.BeatRenderDataV2(
        rank=1,
        entity=EntityInfo("g", "player", "Giannis Antetokounmpo", "x", "GA", "Milwaukee Bucks"),
        mention_count=94, source_mix={"bluesky": 40, "reddit": 30},
        portrait_bytes=pb,
        quote_text="Giannis was utterly dominant tonight, bullying his way to the rim again and again.",
        quote_reporter=Reporter("woj.bsky.social", "Adrian Wojnarowski", None),
        quote_avatar_bytes=None, engagement=Engagement(400, 30, 12),
        weekly_counts=[12, 4, 20, 8, 30, 6, 14], weekly_total=94,
        day_labels=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        spike_source_mix={"bluesky": 18, "reddit": 8}, peak_callout="Friday: +30 mentions",
        context_line="94 mentions this week · peaked Friday", is_payoff=True,
    )
    rows = [(i, f"Player {i}", 30 - i) for i in range(1, 6)]
    clips = {
        "coldopen": rc.render_coldopen_v2(spec, pb, 5),
        "beat": rb.render_beat_v2(spec, beat),
        "outro": ro.render_outro_v2(spec, datetime(2026, 6, 1), rows),
        "cta": rcta.render_cta_v2(spec),
    }
    for label, clip in clips.items():
        frame_audit.assert_no_dead_air(clip, threshold=0.85, interval=0.5, label=label)
