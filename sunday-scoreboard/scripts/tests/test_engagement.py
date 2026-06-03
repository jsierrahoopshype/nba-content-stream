"""Tests for v2 engagement scoring, AT-URI derivation, and paced fetch."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import fetch_engagement  # noqa: E402
from lib.engagement_score import (  # noqa: E402
    Engagement,
    at_uri_from_item,
    best_quote,
    bluesky_candidates,
    quote_text,
    score_counts,
)


# ---- scoring ----


def test_score_weights_replies_and_reposts_higher_than_likes():
    # likes*1 + reposts*2 + replies*3
    assert score_counts(10, 0, 0) == 10
    assert score_counts(0, 10, 0) == 20
    assert score_counts(0, 0, 10) == 30
    assert score_counts(5, 3, 2) == 5 + 6 + 6


def test_engagement_score_and_total():
    e = Engagement(likes=5, reposts=3, replies=2)
    assert e.total == 10
    assert e.score == 5 + 6 + 6


def test_engagement_round_trips_through_dict():
    e = Engagement(likes=4, reposts=1, replies=7)
    assert Engagement.from_dict(e.to_dict()) == e


# ---- AT-URI derivation ----


def _bs_item(**over):
    item = {
        "id": "bs-did%3Aplc%3Aluofuy2uw7g4vsd2vbwidtel%2Fapp.bsky.feed.post%2F3mmudsbrqus23",
        "source": "bluesky",
        "url": "https://bsky.app/profile/keithsmithnba.bsky.social/post/3mmudsbrqus23",
        "published_at": "2026-05-27T20:27:24Z",
        "title": "a post",
    }
    item.update(over)
    return item


def test_at_uri_decodes_from_id():
    uri = at_uri_from_item(_bs_item())
    assert uri == "at://did:plc:luofuy2uw7g4vsd2vbwidtel/app.bsky.feed.post/3mmudsbrqus23"


def test_at_uri_falls_back_to_thumbnail_did_plus_url_rkey():
    item = _bs_item(
        id="bs-garbage-not-encoded",
        thumbnail="https://cdn.bsky.app/img/feed_thumbnail/plain/did:plc:abc123/bafkrei@jpeg",
        url="https://bsky.app/profile/handle.bsky.social/post/3krkeyvalue",
    )
    assert at_uri_from_item(item) == "at://did:plc:abc123/app.bsky.feed.post/3krkeyvalue"


def test_at_uri_none_for_non_bluesky():
    assert at_uri_from_item({"source": "reddit", "id": "rd-xyz"}) is None


def test_at_uri_none_when_unresolvable():
    assert at_uri_from_item({"source": "bluesky", "id": "bs-nope", "url": ""}) is None


# ---- candidates + best quote ----


def test_bluesky_candidates_filters_source():
    items = [_bs_item(id="bs-a"), {"source": "reddit"}, _bs_item(id="bs-b")]
    assert len(bluesky_candidates(items)) == 2


def test_best_quote_picks_highest_score():
    a = _bs_item(id="bs-did%3Aplc%3Ax%2Fapp.bsky.feed.post%2Faaa", published_at="2026-05-25T00:00:00Z")
    b = _bs_item(id="bs-did%3Aplc%3Ax%2Fapp.bsky.feed.post%2Fbbb", published_at="2026-05-26T00:00:00Z")
    eng = {
        at_uri_from_item(a): Engagement(likes=100, reposts=0, replies=0),   # score 100
        at_uri_from_item(b): Engagement(likes=0, reposts=0, replies=40),    # score 120
    }
    item, chosen_eng = best_quote([a, b], eng)
    assert item is b
    assert chosen_eng.replies == 40


def test_best_quote_tie_breaks_by_recency():
    a = _bs_item(id="bs-did%3Aplc%3Ax%2Fapp.bsky.feed.post%2Faaa", published_at="2026-05-25T00:00:00Z")
    b = _bs_item(id="bs-did%3Aplc%3Ax%2Fapp.bsky.feed.post%2Fbbb", published_at="2026-05-26T00:00:00Z")
    same = Engagement(likes=10, reposts=0, replies=0)
    eng = {at_uri_from_item(a): same, at_uri_from_item(b): same}
    item, _ = best_quote([a, b], eng)
    assert item is b  # newer wins the tie


def test_best_quote_falls_back_to_recency_when_no_engagement():
    a = _bs_item(id="bs-did%3Aplc%3Ax%2Fapp.bsky.feed.post%2Faaa", published_at="2026-05-25T00:00:00Z")
    b = _bs_item(id="bs-did%3Aplc%3Ax%2Fapp.bsky.feed.post%2Fbbb", published_at="2026-05-28T00:00:00Z")
    item, eng = best_quote([a, b], {})  # nothing fetched
    assert item is b
    assert eng is None


def test_best_quote_falls_back_to_recency_when_all_zero():
    a = _bs_item(id="bs-did%3Aplc%3Ax%2Fapp.bsky.feed.post%2Faaa", published_at="2026-05-25T00:00:00Z")
    b = _bs_item(id="bs-did%3Aplc%3Ax%2Fapp.bsky.feed.post%2Fbbb", published_at="2026-05-28T00:00:00Z")
    zero = Engagement()
    eng = {at_uri_from_item(a): zero, at_uri_from_item(b): zero}
    item, _ = best_quote([a, b], eng)
    assert item is b  # recency, not "first with engagement"


def test_best_quote_none_when_no_candidates():
    assert best_quote([], {}) is None


def test_quote_text_prefers_body_excerpt():
    assert quote_text({"body_excerpt": "full text", "title": "short"}) == "full text"
    assert quote_text({"title": "only title"}) == "only title"


# ---- paced batch fetch ----


def test_paced_batch_fetch_chunks_and_paces():
    sleeps: list[float] = []
    items = list(range(25))  # 3 chunks of 10/10/5
    out = fetch_engagement.paced_batch_fetch(
        items, lambda x: x * 2, batch_size=10, between_ms=500,
        sleep_fn=lambda s: sleeps.append(s),
    )
    assert out == [x * 2 for x in items]
    # 3 chunks → 2 inter-chunk sleeps, none after the last.
    assert sleeps == [0.5, 0.5]


def test_paced_batch_fetch_drops_none_and_exceptions():
    def fetch(x):
        if x == 2:
            return None
        if x == 3:
            raise RuntimeError("boom")
        return x
    out = fetch_engagement.paced_batch_fetch(
        [1, 2, 3, 4], fetch, batch_size=2, between_ms=0, sleep_fn=lambda s: None,
    )
    assert out == [1, 4]


def test_paced_batch_fetch_no_sleep_for_single_chunk():
    sleeps: list[float] = []
    fetch_engagement.paced_batch_fetch(
        [1, 2], lambda x: x, batch_size=10, sleep_fn=lambda s: sleeps.append(s),
    )
    assert sleeps == []


# ---- response parsing + uri batch ----


def test_parse_engagement_reads_counts():
    payload = {"thread": {"post": {"likeCount": 12, "repostCount": 3, "replyCount": 5}}}
    eng = fetch_engagement.parse_engagement(payload)
    assert (eng.likes, eng.reposts, eng.replies) == (12, 3, 5)


def test_parse_engagement_none_for_missing_post():
    assert fetch_engagement.parse_engagement({"thread": {}}) is None
    assert fetch_engagement.parse_engagement(None) is None


def test_fetch_engagement_for_uris_skips_cached_and_dedupes():
    calls: list[str] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(params["uri"])

        class _R:
            def raise_for_status(self):
                return None

            def json(self):
                return {"thread": {"post": {"likeCount": 1, "repostCount": 0, "replyCount": 0}}}

        return _R()

    known = {"at://cached": Engagement(likes=9, reposts=9, replies=9)}
    result = fetch_engagement.fetch_engagement_for_uris(
        ["at://cached", "at://new", "at://new"],  # cached + duplicate new
        get=fake_get, sleep_fn=lambda s: None, known=known,
    )
    assert calls == ["at://new"]  # only the uncached, de-duped URI fetched
    assert result["at://cached"].likes == 9
    assert result["at://new"].likes == 1


def test_candidate_uris_from_beats_flattens_and_dedupes():
    a = _bs_item(id="bs-did%3Aplc%3Ax%2Fapp.bsky.feed.post%2Faaa")
    dup = _bs_item(id="bs-did%3Aplc%3Ax%2Fapp.bsky.feed.post%2Faaa")
    b = _bs_item(id="bs-did%3Aplc%3Ax%2Fapp.bsky.feed.post%2Fbbb")
    uris = fetch_engagement.candidate_uris_from_beats([[a, dup], [b, {"source": "reddit"}]])
    assert uris == [at_uri_from_item(a), at_uri_from_item(b)]
