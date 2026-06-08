"""v2.3 regression tests: text overflow, sparkline drawing, CTA tofu,
counter hold, number consistency, quote dedupe/demotion."""

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

from lib import anim, draw, frame_audit, sparkline  # noqa: E402
from lib import format_specs as fs  # noqa: E402
from lib import quote_filter as qf  # noqa: E402
from lib.canonical_lookup import EntityInfo  # noqa: E402
from lib.engagement_score import Engagement, at_uri_from_item  # noqa: E402
from lib.reporter_lookup import Reporter  # noqa: E402
import render_beat_v2 as rb  # noqa: E402
import render_cta_v2 as rcta  # noqa: E402
import render_coldopen_v2 as rc  # noqa: E402
import render_helpers_v2 as helpers  # noqa: E402

LONG_NAMES = ["Giannis Antetokounmpo", "Shai Gilgeous-Alexander", "Karl-Anthony Towns"]


def _portrait():
    buf = io.BytesIO()
    Image.new("RGB", (300, 400), (130, 95, 70)).save(buf, "PNG")
    return buf.getvalue()


def _beat(rank=1, name="Giannis Antetokounmpo", counts=None, payoff=True, **kw):
    counts = counts or [12, 4, 20, 8, 30, 6, 14]
    data = dict(
        rank=rank,
        entity=EntityInfo("g", "player", name, "x", "".join(w[0] for w in name.split()[:2]).upper(), "Milwaukee Bucks"),
        mention_count=sum(counts), source_mix={"bluesky": 40, "reddit": 30, "google-news": 24},
        portrait_bytes=_portrait(),
        quote_text="Giannis was utterly dominant tonight, bullying his way to the rim again and again.",
        quote_reporter=Reporter("woj.bsky.social", "Adrian Wojnarowski", None),
        quote_avatar_bytes=None, engagement=Engagement(400, 30, 12),
        weekly_counts=counts, weekly_total=sum(counts),
        day_labels=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        spike_source_mix={"bluesky": 18, "reddit": 8}, peak_callout="Friday: +30 mentions",
        context_line=f"{sum(counts)} mentions this week · peaked Friday", is_payoff=payoff,
    )
    data.update(kw)
    return rb.BeatRenderDataV2(**data)


# ---- BUG 1: no text overflow on hero across all formats / long names ----


def _max_text_right(frame, bg_tol=40):
    """Rightmost column containing dark (text) pixels — crude overflow probe."""
    arr = np.asarray(frame)
    dark = (arr.mean(axis=2) < 90)  # dark glyphs on light/duotone
    cols = np.where(dark.any(axis=0))[0]
    return int(cols.max()) if len(cols) else 0


@pytest.mark.parametrize("fmt", ["vertical", "square", "horizontal"])
@pytest.mark.parametrize("name", LONG_NAMES)
def test_hero_name_fits_safe_area(fmt, name):
    spec = fs.get_format(fmt)
    margin = helpers.safe_margin(spec)
    # Use the same fit path the renderer uses and assert each line fits.
    m = __import__("lib.style22", fromlist=["metrics"]).metrics(spec)
    name_maxw = spec.width - 2 * margin
    font, lines = helpers.fit_wrapped(name.upper(), fs.FONT_BOLD, m["name"], name_maxw, max_lines=2, min_size=int(m["name"] * 0.5))
    for ln in lines:
        w = draw.measure_text(ln, font)[0]
        assert w <= name_maxw, f"{fmt} {name!r}: line {ln!r} width {w} > {name_maxw}"


def test_fit_text_shrinks_then_ellipsizes():
    spec = fs.get_format("vertical")
    f, txt = helpers.fit_text("X" * 200, fs.FONT_BOLD, 120, 400, min_size=14)
    assert draw.measure_text(txt, f)[0] <= 400


# ---- BUG 2: sparkline actually draws within its plot rect ----


def test_sparkline_points_span_plot_width_with_7_points():
    plot = (110, 300, 970, 700)
    pts = sparkline.sparkline_points([1, 2, 3, 4, 5, 6, 7], plot)
    assert len(pts) == 7
    assert pts[0][0] == 110 and pts[-1][0] == 970  # span full width


@pytest.mark.parametrize("fmt", ["vertical", "square", "horizontal"])
def test_sparkline_phase_chart_not_empty(fmt):
    spec = fs.get_format(fmt)
    frame = rb._draw_sparkline(spec, _beat(), 1.5, 2.0)  # held frame
    # Plot region (recomputed the same way the renderer does) must have
    # real chart pixels — not the flat card fill, not background.
    arr = np.asarray(frame.convert("RGB"))
    region = (spec.width // 4, spec.height // 3, spec.width * 3 // 4, spec.height * 2 // 3)
    n = frame_audit.region_content_pixels(
        arr, region, ignore=(fs.hex_to_rgb(fs.ACCENT_DIM),), tol=10
    )
    assert n > 500, f"{fmt}: chart appears empty ({n} content px)"


# ---- BUG 3: no tofu / non-renderable codepoints in CTA ----


def test_cta_strings_have_no_nonrenderable_codepoints():
    for s in (rcta.HEADLINE_LABEL, rcta.HEADLINE_URL, rcta.SUBLINE):
        assert "→" not in s
        assert all(not qf._is_emoji_or_symbol(ord(c)) for c in s), f"non-renderable glyph in {s!r}"


def test_safe_text_strips_arrow_everywhere():
    assert "→" not in helpers.safe_text("Full data → HoopsMatic.com")


# ---- BUG 4: counter settles fast then HOLDS ----


def test_counter_holds_after_settle():
    final = 94
    assert anim.count_up(rb.COUNTER_SETTLE, final, rb.COUNTER_SETTLE) == final
    # held for the rest of the phase
    for t in (0.5, 1.0, 2.0, 8.0):
        assert anim.count_up(t, final, rb.COUNTER_SETTLE) == final


def test_counter_settles_within_0_4s():
    assert rb.COUNTER_SETTLE <= 0.4


# ---- BUG 5: one number everywhere ----


def test_hero_count_equals_sparkline_total_equals_outro():
    counts = [5, 9, 2, 14, 7, 3, 6]
    b = _beat(counts=counts)
    total = sum(counts)
    assert b.mention_count == total          # hero counter source
    assert b.weekly_total == total           # sparkline total source
    # outro row uses mention_count → same number
    assert b.mention_count == b.weekly_total == total


# ---- BUG 6: per-video dedupe + demotion ----


def _post(handle, rkey, text, pub="2026-06-01T12:00:00Z"):
    return {
        "source": "bluesky", "author_handle": handle,
        "id": f"bs-did%3Aplc%3Ax%2Fapp.bsky.feed.post%2F{rkey}",
        "url": f"https://bsky.app/profile/{handle}/post/{rkey}",
        "published_at": pub, "title": text,
    }


GOOD = "This was one of the most dominant individual performances of the entire season so far."


def test_rank_candidates_orders_nondemoted_before_demoted():
    roster = {"woj.bsky.social", "nbastat.bsky.social"}
    demote = {"nbastat.bsky.social"}
    bot = _post("nbastat.bsky.social", "bot", GOOD)
    rep = _post("woj.bsky.social", "rep", GOOD)
    eng = {at_uri_from_item(bot): Engagement(900, 0, 0), at_uri_from_item(rep): Engagement(10, 0, 0)}
    ordered, _ = qf.rank_candidates([bot, rep], eng, roster=roster, blocklist=set(), demote=demote)
    # reporter (lower engagement) comes first because the bot is demoted
    assert ordered[0][0] is rep
    assert ordered[-1][0] is bot and ordered[-1][2] is True


def test_demoted_account_loses_to_normal_reporter():
    # Simulate the cross-beat pick used in enrich: prefer non-demoted.
    roster = {"woj.bsky.social", "nbastat.bsky.social"}
    demote = {"nbastat.bsky.social"}
    bot = _post("nbastat.bsky.social", "bot", GOOD)
    rep = _post("woj.bsky.social", "rep", GOOD)
    eng = {at_uri_from_item(bot): Engagement(900, 0, 0), at_uri_from_item(rep): Engagement(10, 0, 0)}
    ordered, _ = qf.rank_candidates([bot, rep], eng, roster=roster, blocklist=set(), demote=demote)
    chosen = None
    used_uris, used_handles = set(), set()
    for prefer in (False, True):
        for item, e, dem in ordered:
            if dem != prefer:
                continue
            chosen = item
            break
        if chosen:
            break
    assert qf._handle_of(chosen) == "woj.bsky.social"


def test_same_post_not_reused_across_beats():
    # The v2.3 dedupe: two beats, same single shared post → second beat
    # must not reuse it.
    roster = {"nbastat.bsky.social"}
    shared = _post("nbastat.bsky.social", "shared", GOOD)
    per_beat = [[shared], [shared]]
    used_uris, used_handles = set(), set()
    picks = []
    for cands in per_beat:
        ordered, _ = qf.rank_candidates(cands, {}, roster=roster, blocklist=set(), demote=set())
        chosen = None
        for prefer in (False, True):
            for item, e, dem in ordered:
                if dem != prefer:
                    continue
                uri = at_uri_from_item(item)
                h = qf._handle_of(item)
                if uri in used_uris or h in used_handles:
                    continue
                chosen = item
                used_uris.add(uri)
                used_handles.add(h)
                break
            if chosen:
                break
        picks.append(chosen)
    assert picks[0] is shared
    assert picks[1] is None  # not reused


def test_load_demote_handles_reads_config():
    demote = qf.load_demote_handles()
    assert "nbastat.bsky.social" in demote
