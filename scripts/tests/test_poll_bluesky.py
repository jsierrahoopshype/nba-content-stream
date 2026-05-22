"""Tests for `scripts.poll_bluesky`.

Builds FeedViewPost-shaped fixtures with SimpleNamespace so tests don't
need a live atproto client. Covers the filter (reposts/replies), the
post→item mapping (id, url, media type, quote-post flag, engagement,
entity detection), the collect_items loop (per-reporter fetch errors,
--since cutoff), and the dry-run CLI path.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts import poll_bluesky
from scripts.lib import shards
from scripts.lib.canonical import load_canonical
from scripts.lib.shards import load_shard, validate_item
from scripts.poll_bluesky import (
    _actor_for,
    _at_uri_to_id,
    _public_url,
    _has_repost_reason,
    _is_quote_post,
    _is_reply,
    collect_items,
    map_post_to_item,
    should_include,
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _author(handle="reporter.bsky.social", display_name="Reporter", did="did:plc:r1"):
    return SimpleNamespace(handle=handle, display_name=display_name, did=did)


def _record(
    text="post body",
    created_at="2026-05-21T14:30:00Z",
    reply=None,
    embed=None,
):
    return SimpleNamespace(text=text, created_at=created_at, reply=reply, embed=embed)


def _post(
    uri="at://did:plc:r1/app.bsky.feed.post/3kabc123",
    cid="bafy1",
    author=None,
    record=None,
    embed=None,
    indexed_at="2026-05-21T14:30:05Z",
    like_count=12,
    repost_count=3,
    reply_count=1,
    quote_count=0,
):
    return SimpleNamespace(
        uri=uri,
        cid=cid,
        author=author or _author(),
        record=record or _record(),
        embed=embed,
        indexed_at=indexed_at,
        like_count=like_count,
        repost_count=repost_count,
        reply_count=reply_count,
        quote_count=quote_count,
    )


def _feed_view(post=None, reason=None, reply=None):
    return SimpleNamespace(post=post or _post(), reason=reason, reply=reply)


def _repost_reason():
    return SimpleNamespace(py_type="app.bsky.feed.defs#reasonRepost")


def _record_embed():
    return SimpleNamespace(py_type="app.bsky.embed.record#view")


def _images_embed():
    return SimpleNamespace(py_type="app.bsky.embed.images#view")


def _reply_ref():
    # Real ReplyRef has parent/root strong refs; only presence matters.
    return SimpleNamespace(
        parent=SimpleNamespace(uri="x", cid="y"),
        root=SimpleNamespace(uri="x", cid="y"),
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_at_uri_to_id_url_encodes_path():
    out = _at_uri_to_id("at://did:plc:abc123/app.bsky.feed.post/3kxyz")
    assert out.startswith("bs-")
    # All special chars must be encoded so the id is safe in filenames/URLs.
    assert ":" not in out
    assert "/" not in out
    assert out == "bs-did%3Aplc%3Aabc123%2Fapp.bsky.feed.post%2F3kxyz"


def test_at_uri_to_id_handles_missing_scheme():
    out = _at_uri_to_id("did:plc:abc/app.bsky.feed.post/3kxyz")
    assert out == "bs-did%3Aplc%3Aabc%2Fapp.bsky.feed.post%2F3kxyz"


def test_public_url_uses_handle_and_rkey():
    url = _public_url(
        "nuggets.bsky.social",
        "at://did:plc:abc/app.bsky.feed.post/3kxyz",
    )
    assert url == "https://bsky.app/profile/nuggets.bsky.social/post/3kxyz"


def test_actor_for_prefers_did():
    rec = {"handle": "x.bsky.social", "did": "did:plc:abc"}
    assert _actor_for(rec) == "did:plc:abc"


def test_actor_for_falls_back_to_handle_when_did_none():
    rec = {"handle": "x.bsky.social", "did": None}
    assert _actor_for(rec) == "x.bsky.social"


def test_actor_for_falls_back_when_did_missing_key():
    # Override-added reporters have no 'did' key at all.
    rec = {"handle": "added.bsky.social"}
    assert _actor_for(rec) == "added.bsky.social"


def test_actor_for_falls_back_when_did_malformed():
    rec = {"handle": "x.bsky.social", "did": "not-a-did"}
    assert _actor_for(rec) == "x.bsky.social"


# ---------------------------------------------------------------------------
# should_include
# ---------------------------------------------------------------------------


def test_should_include_top_level_post():
    assert should_include(_feed_view()) is True


def test_should_include_drops_reposts():
    fv = _feed_view(reason=_repost_reason())
    assert _has_repost_reason(fv) is True
    assert should_include(fv) is False


def test_should_include_drops_replies():
    fv = _feed_view(post=_post(record=_record(reply=_reply_ref())))
    assert _is_reply(fv) is True
    assert should_include(fv) is False


def test_should_include_keeps_quote_posts():
    # Quote-post has embed of record type but no reply, no repost reason.
    fv = _feed_view(post=_post(embed=_record_embed()))
    assert should_include(fv) is True
    assert _is_quote_post(fv.post) is True


# ---------------------------------------------------------------------------
# map_post_to_item
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def vocab():
    return load_canonical()


def test_map_post_produces_valid_item(vocab):
    players, teams = vocab
    fv = _feed_view()
    item = map_post_to_item(
        fv, {"handle": "reporter.bsky.social"}, players, teams
    )
    assert validate_item(item) == []
    assert item["source"] == "bluesky"
    assert item["id"].startswith("bs-")
    assert item["url"].startswith("https://bsky.app/profile/")
    assert item["author"]["handle"] == "reporter.bsky.social"
    assert item["media"]["type"] == "text"
    assert item["engagement"]["likes"] == 12
    assert item["engagement"]["reposts"] == 3


def test_map_post_detects_entities(vocab):
    players, teams = vocab
    fv = _feed_view(
        post=_post(
            record=_record(
                text="LeBron James and the Lakers got the win tonight."
            )
        )
    )
    item = map_post_to_item(
        fv, {"handle": "reporter.bsky.social"}, players, teams
    )
    assert "lebron-james" in item["players"]
    assert "los-angeles-lakers" in item["teams"]


def test_map_post_image_embed_sets_media_image(vocab):
    players, teams = vocab
    fv = _feed_view(post=_post(embed=_images_embed()))
    item = map_post_to_item(
        fv, {"handle": "reporter.bsky.social"}, players, teams
    )
    assert item["media"]["type"] == "image"


def test_map_post_quote_post_flagged(vocab):
    players, teams = vocab
    fv = _feed_view(post=_post(embed=_record_embed()))
    item = map_post_to_item(
        fv, {"handle": "reporter.bsky.social"}, players, teams
    )
    assert item.get("is_quote_post") is True


def test_map_post_published_at_normalized_to_z(vocab):
    players, teams = vocab
    fv = _feed_view(
        post=_post(record=_record(created_at="2026-05-21T14:30:00.000Z"))
    )
    item = map_post_to_item(
        fv, {"handle": "reporter.bsky.social"}, players, teams
    )
    assert item["published_at"] == "2026-05-21T14:30:00Z"


def test_map_post_body_excerpt_full_text(vocab):
    players, teams = vocab
    fv = _feed_view(post=_post(record=_record(text="short post")))
    item = map_post_to_item(
        fv, {"handle": "reporter.bsky.social"}, players, teams
    )
    # Bluesky posts are short; body_excerpt is the full text.
    assert item["body_excerpt"] == "short post"


def test_map_post_uses_reporter_record_when_author_missing_display(vocab):
    """If the live post strips display_name, the reporter record fills it."""
    players, teams = vocab
    fv = _feed_view(
        post=_post(
            author=SimpleNamespace(
                handle="reporter.bsky.social",
                display_name=None,
                did="did:plc:r1",
            )
        )
    )
    item = map_post_to_item(
        fv,
        {"handle": "reporter.bsky.social", "display_name": "The Reporter"},
        players,
        teams,
    )
    assert item["author"]["display_name"] == "The Reporter"


# ---------------------------------------------------------------------------
# collect_items
# ---------------------------------------------------------------------------


def test_collect_items_drops_reposts_and_replies(vocab):
    players, teams = vocab
    feed = [
        _feed_view(),  # keep
        _feed_view(reason=_repost_reason()),  # drop (repost)
        _feed_view(post=_post(record=_record(reply=_reply_ref()))),  # drop
        _feed_view(post=_post(uri="at://did:plc:r1/app.bsky.feed.post/3kq2")),
    ]
    reporters = [{"handle": "reporter.bsky.social", "did": "did:plc:r1"}]

    def fetcher(actor, limit):
        return feed

    items, stats = collect_items(reporters, fetcher, players, teams, None, 50)
    assert stats["kept"] == 2
    assert stats["dropped_filter"] == 2
    assert stats["posts_seen"] == 4


def test_collect_items_respects_since_cutoff(vocab):
    players, teams = vocab
    feed = [
        _feed_view(post=_post(record=_record(created_at="2026-05-20T10:00:00Z"))),
        _feed_view(
            post=_post(
                uri="at://did:plc:r1/app.bsky.feed.post/late",
                record=_record(created_at="2026-05-21T18:00:00Z"),
            )
        ),
    ]

    def fetcher(actor, limit):
        return feed

    items, stats = collect_items(
        [{"handle": "r.bsky.social", "did": "did:plc:r1"}],
        fetcher,
        players,
        teams,
        since_iso="2026-05-21T00:00:00Z",
        limit=50,
    )
    assert stats["kept"] == 1
    assert stats["dropped_since"] == 1
    assert items[0]["published_at"] == "2026-05-21T18:00:00Z"


def test_collect_items_continues_when_one_reporter_errors(vocab):
    players, teams = vocab
    good_feed = [_feed_view()]

    def fetcher(actor, limit):
        if actor == "did:plc:bad":
            raise RuntimeError("network error for bad reporter")
        return good_feed

    reporters = [
        {"handle": "bad.bsky.social", "did": "did:plc:bad"},
        {"handle": "good.bsky.social", "did": "did:plc:good"},
    ]
    items, stats = collect_items(reporters, fetcher, players, teams, None, 50)
    assert stats["fetch_errors"] == 1
    assert stats["kept"] == 1
    assert items[0]["author"]["handle"] == "reporter.bsky.social"


def test_collect_items_uses_did_when_present(vocab):
    """Verify the actor preference: DID first, handle fallback."""
    players, teams = vocab
    actors_called: list[str] = []

    def fetcher(actor, limit):
        actors_called.append(actor)
        return []

    reporters = [
        {"handle": "with-did.bsky.social", "did": "did:plc:abc"},
        {"handle": "no-did.bsky.social"},
        {"handle": "blank-did.bsky.social", "did": ""},
    ]
    collect_items(reporters, fetcher, players, teams, None, 50)
    assert actors_called == [
        "did:plc:abc",
        "no-did.bsky.social",
        "blank-did.bsky.social",
    ]


# ---------------------------------------------------------------------------
# End-to-end via run() (dry-run path)
# ---------------------------------------------------------------------------


def test_run_dry_run_does_not_write_shard(tmp_path, monkeypatch, capsys, vocab):
    monkeypatch.setattr(shards, "DATA_DIR", tmp_path)

    csv_text = (
        "Handle,Display Name,DID\n"
        "reporter.bsky.social,The Reporter,did:plc:r1\n"
    )
    overrides_path = tmp_path / "bluesky_overrides.json"
    overrides_path.write_text('{"add": [], "remove": []}')
    monkeypatch.setattr(poll_bluesky, "OVERRIDES_PATH", overrides_path)

    feed = [_feed_view()]
    fake_client = SimpleNamespace(
        get_author_feed=lambda actor, filter, limit: SimpleNamespace(feed=feed)
    )

    with patch("scripts.lib.sources._fetch_with_retry", return_value=csv_text):
        rc = poll_bluesky.run(
            ["--dry-run", "--since", "2026-05-01T00:00:00Z"],
            client=fake_client,
        )

    assert rc == 0
    captured = capsys.readouterr().out
    assert "DRY RUN" in captured
    assert "would append 1 items" in captured

    # No shard file written
    assert not (tmp_path / "bluesky" / "2026-05-21.json").exists()


def test_run_writes_shard_and_dedupes(tmp_path, monkeypatch, vocab):
    monkeypatch.setattr(shards, "DATA_DIR", tmp_path)

    csv_text = (
        "Handle,Display Name,DID\n"
        "reporter.bsky.social,The Reporter,did:plc:r1\n"
    )
    overrides_path = tmp_path / "bluesky_overrides.json"
    overrides_path.write_text('{"add": [], "remove": []}')
    monkeypatch.setattr(poll_bluesky, "OVERRIDES_PATH", overrides_path)

    feed = [
        _feed_view(
            post=_post(
                uri="at://did:plc:r1/app.bsky.feed.post/3kone",
                record=_record(text="Lakers won.", created_at="2026-05-21T10:00:00Z"),
            )
        ),
        _feed_view(
            post=_post(
                uri="at://did:plc:r1/app.bsky.feed.post/3ktwo",
                record=_record(text="Celtics tonight.", created_at="2026-05-21T11:00:00Z"),
            )
        ),
    ]
    fake_client = SimpleNamespace(
        get_author_feed=lambda actor, filter, limit: SimpleNamespace(feed=feed)
    )

    with patch("scripts.lib.sources._fetch_with_retry", return_value=csv_text):
        # First run: 2 items appended.
        rc1 = poll_bluesky.run(
            ["--since", "2026-05-01T00:00:00Z"], client=fake_client
        )
        # Second run: 0 appended (dedup by id).
        rc2 = poll_bluesky.run(
            ["--since", "2026-05-01T00:00:00Z"], client=fake_client
        )

    assert rc1 == 0 and rc2 == 0

    from scripts.lib.utils import today_utc_date

    shard = load_shard("bluesky", today_utc_date())
    assert len(shard["items"]) == 2
    ids = [it["id"] for it in shard["items"]]
    assert ids[0].startswith("bs-") and ids[1].startswith("bs-")
    assert len(set(ids)) == 2


def test_run_reporter_filter_narrows_to_one(tmp_path, monkeypatch, capsys, vocab):
    monkeypatch.setattr(shards, "DATA_DIR", tmp_path)
    csv_text = (
        "Handle,Display Name,DID\n"
        "a.bsky.social,A,did:plc:a\n"
        "b.bsky.social,B,did:plc:b\n"
    )
    overrides_path = tmp_path / "bluesky_overrides.json"
    overrides_path.write_text('{"add": [], "remove": []}')
    monkeypatch.setattr(poll_bluesky, "OVERRIDES_PATH", overrides_path)

    actors_called: list[str] = []

    def fake_get_feed(actor, filter, limit):
        actors_called.append(actor)
        return SimpleNamespace(feed=[])

    fake_client = SimpleNamespace(get_author_feed=fake_get_feed)

    with patch("scripts.lib.sources._fetch_with_retry", return_value=csv_text):
        rc = poll_bluesky.run(
            ["--dry-run", "--reporter", "b.bsky.social"], client=fake_client
        )

    assert rc == 0
    assert actors_called == ["did:plc:b"]
