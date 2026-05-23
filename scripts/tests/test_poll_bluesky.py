"""Tests for `scripts.poll_bluesky`.

Fixtures are plain dicts matching the public AppView's JSON for
`app.bsky.feed.getAuthorFeed` (lexicon: `app.bsky.feed.defs#feedViewPost`),
so the same shape the production code sees from
https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed. Tests cover:

- The host guard (URL goes to public.api.bsky.app, NOT bsky.social).
- 401 from the AppView is logged and counted as a fetch_error, not crashing.
- The filter (reposts/replies dropped, top-level + quote-posts kept).
- Mapping a feedViewPost → item (id, url, media type, quote flag,
  engagement, entity detection, ISO normalization).
- Per-reporter fetch error isolation and the `--since` cutoff.
- Dry-run skips shard write; non-dry-run writes + dedupes on re-run.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests

from scripts import poll_bluesky
from scripts.lib import shards
from scripts.lib.canonical import load_canonical
from scripts.lib.shards import load_shard, validate_item
from scripts.poll_bluesky import (
    APPVIEW_BASE_URL,
    GET_AUTHOR_FEED_PATH,
    _actor_for,
    _at_uri_to_id,
    _has_repost_reason,
    _is_quote_post,
    _is_reply,
    _public_url,
    collect_items,
    fetch_author_feed,
    map_post_to_item,
    should_include,
)


# ---------------------------------------------------------------------------
# Fixture builders (plain dicts, matching the AppView JSON shape)
# ---------------------------------------------------------------------------


def _author(
    handle="reporter.bsky.social",
    display_name="Reporter",
    did="did:plc:r1",
) -> dict:
    return {
        "$type": "app.bsky.actor.defs#profileViewBasic",
        "did": did,
        "handle": handle,
        "displayName": display_name,
    }


def _record(
    text="post body",
    created_at="2026-05-21T14:30:00Z",
    reply=None,
    embed=None,
) -> dict:
    record: dict = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": created_at,
    }
    if reply is not None:
        record["reply"] = reply
    if embed is not None:
        record["embed"] = embed
    return record


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
) -> dict:
    post: dict = {
        "$type": "app.bsky.feed.defs#postView",
        "uri": uri,
        "cid": cid,
        "author": author if author is not None else _author(),
        "record": record if record is not None else _record(),
        "indexedAt": indexed_at,
        "likeCount": like_count,
        "repostCount": repost_count,
        "replyCount": reply_count,
        "quoteCount": quote_count,
    }
    if embed is not None:
        post["embed"] = embed
    return post


def _feed_view(post=None, reason=None) -> dict:
    fv: dict = {
        "$type": "app.bsky.feed.defs#feedViewPost",
        "post": post if post is not None else _post(),
    }
    if reason is not None:
        fv["reason"] = reason
    return fv


def _repost_reason() -> dict:
    return {
        "$type": "app.bsky.feed.defs#reasonRepost",
        "by": _author("reposter.bsky.social", "Reposter", "did:plc:other"),
        "indexedAt": "2026-05-21T14:35:00Z",
    }


def _record_embed_view() -> dict:
    return {"$type": "app.bsky.embed.record#view", "record": {}}


def _images_embed_view() -> dict:
    return {"$type": "app.bsky.embed.images#view", "images": []}


def _reply_ref() -> dict:
    return {
        "parent": {"uri": "at://x/app.bsky.feed.post/p", "cid": "c1"},
        "root": {"uri": "at://x/app.bsky.feed.post/r", "cid": "c2"},
    }


# ---------------------------------------------------------------------------
# Fake HTTP session used by fetch_author_feed tests
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status: int, payload: dict | None = None):
        self.status_code = status
        self._payload = payload or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} from fake",
                response=SimpleNamespace(status_code=self.status_code),
            )

    def json(self) -> dict:
        return self._payload


class _FakeSession:
    """Captures the last GET URL/params; returns a configured response."""

    def __init__(self, response: _FakeResp):
        self.response = response
        self.last_url: str | None = None
        self.last_params: dict | None = None

    def get(self, url, params=None, timeout=None):
        self.last_url = url
        self.last_params = params
        return self.response


# ---------------------------------------------------------------------------
# Endpoint host guard — the regression test for the 401 bug
# ---------------------------------------------------------------------------


def test_fetch_author_feed_targets_public_appview_not_bsky_social():
    """If this ever flips back to bsky.social we get auth-walled (401)."""
    session = _FakeSession(_FakeResp(200, {"feed": []}))
    fetch_author_feed(session, "did:plc:abc", limit=50)

    assert session.last_url is not None
    assert session.last_url.startswith("https://public.api.bsky.app/")
    assert "bsky.social" not in session.last_url
    # And the exact path
    assert session.last_url == APPVIEW_BASE_URL + GET_AUTHOR_FEED_PATH


def test_fetch_author_feed_sends_no_replies_filter():
    session = _FakeSession(_FakeResp(200, {"feed": []}))
    fetch_author_feed(session, "did:plc:abc", limit=25)
    assert session.last_params == {
        "actor": "did:plc:abc",
        "filter": "posts_no_replies",
        "limit": "25",
    }


def test_fetch_author_feed_raises_on_401():
    """A 401 must bubble up so collect_items records a fetch error."""
    session = _FakeSession(_FakeResp(401))
    with pytest.raises(requests.HTTPError):
        fetch_author_feed(session, "did:plc:abc", limit=50)


def test_collect_items_treats_401_as_fetch_error(vocab=None):
    """End-to-end: a reporter whose AppView call 401s is skipped, not fatal.

    Guards against regressing to the authenticated bsky.social endpoint.
    """
    players, teams = load_canonical()

    def fetcher(actor, limit):
        # Simulate what fetch_author_feed does when the AppView returns 401.
        raise requests.HTTPError("401 Client Error: Unauthorized")

    reporters = [{"handle": "r.bsky.social", "did": "did:plc:r1"}]
    items, stats = collect_items(reporters, fetcher, players, teams, None, 50)
    assert items == []
    assert stats["fetch_errors"] == 1
    assert stats["kept"] == 0


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_at_uri_to_id_url_encodes_path():
    out = _at_uri_to_id("at://did:plc:abc123/app.bsky.feed.post/3kxyz")
    assert out.startswith("bs-")
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
    assert _actor_for({"handle": "x.bsky.social", "did": "did:plc:abc"}) == "did:plc:abc"


def test_actor_for_falls_back_to_handle_when_did_none():
    assert _actor_for({"handle": "x.bsky.social", "did": None}) == "x.bsky.social"


def test_actor_for_falls_back_when_did_missing_key():
    # Override-added reporters have no 'did' key at all.
    assert _actor_for({"handle": "added.bsky.social"}) == "added.bsky.social"


def test_actor_for_falls_back_when_did_malformed():
    assert _actor_for({"handle": "x.bsky.social", "did": "not-a-did"}) == "x.bsky.social"


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
    fv = _feed_view(post=_post(embed=_record_embed_view()))
    assert should_include(fv) is True
    assert _is_quote_post(fv["post"]) is True


# ---------------------------------------------------------------------------
# map_post_to_item
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def vocab():
    return load_canonical()


def test_map_post_produces_valid_item(vocab):
    players, teams = vocab
    fv = _feed_view()
    item = map_post_to_item(fv, {"handle": "reporter.bsky.social"}, players, teams)
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
            record=_record(text="LeBron James and the Lakers got the win tonight.")
        )
    )
    item = map_post_to_item(fv, {"handle": "reporter.bsky.social"}, players, teams)
    assert "lebron-james" in item["players"]
    assert "los-angeles-lakers" in item["teams"]


def test_map_post_image_embed_sets_media_image(vocab):
    players, teams = vocab
    fv = _feed_view(post=_post(embed=_images_embed_view()))
    item = map_post_to_item(fv, {"handle": "reporter.bsky.social"}, players, teams)
    assert item["media"]["type"] == "image"


def test_map_post_quote_post_flagged(vocab):
    players, teams = vocab
    fv = _feed_view(post=_post(embed=_record_embed_view()))
    item = map_post_to_item(fv, {"handle": "reporter.bsky.social"}, players, teams)
    assert item.get("is_quote_post") is True


def test_map_post_published_at_normalized_to_z(vocab):
    players, teams = vocab
    fv = _feed_view(post=_post(record=_record(created_at="2026-05-21T14:30:00.000Z")))
    item = map_post_to_item(fv, {"handle": "reporter.bsky.social"}, players, teams)
    assert item["published_at"] == "2026-05-21T14:30:00Z"


def test_map_post_body_excerpt_full_text(vocab):
    players, teams = vocab
    fv = _feed_view(post=_post(record=_record(text="short post")))
    item = map_post_to_item(fv, {"handle": "reporter.bsky.social"}, players, teams)
    # Bluesky posts are short; body_excerpt is the full text.
    assert item["body_excerpt"] == "short post"


def test_map_post_uses_reporter_record_when_author_missing_display(vocab):
    """If the live post strips displayName, the reporter record fills it."""
    players, teams = vocab
    author = _author()
    author.pop("displayName")
    fv = _feed_view(post=_post(author=author))
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
        _feed_view(post=_post(record=_record(reply=_reply_ref()))),  # drop (reply)
        _feed_view(post=_post(uri="at://did:plc:r1/app.bsky.feed.post/3kq2")),
    ]
    reporters = [{"handle": "reporter.bsky.social", "did": "did:plc:r1"}]

    items, stats = collect_items(
        reporters, lambda actor, limit: feed, players, teams, None, 50
    )
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

    items, stats = collect_items(
        [{"handle": "r.bsky.social", "did": "did:plc:r1"}],
        lambda actor, limit: feed,
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
# End-to-end via run()
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
    fake_fetcher = lambda actor, limit: feed  # noqa: E731

    with patch("scripts.lib.sources._fetch_with_retry", return_value=csv_text):
        rc = poll_bluesky.run(
            ["--dry-run", "--since", "2026-05-01T00:00:00Z"],
            feed_fetcher=fake_fetcher,
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
                record=_record(
                    text="Celtics tonight.", created_at="2026-05-21T11:00:00Z"
                ),
            )
        ),
    ]
    fake_fetcher = lambda actor, limit: feed  # noqa: E731

    with patch("scripts.lib.sources._fetch_with_retry", return_value=csv_text):
        rc1 = poll_bluesky.run(
            ["--since", "2026-05-01T00:00:00Z"], feed_fetcher=fake_fetcher
        )
        rc2 = poll_bluesky.run(
            ["--since", "2026-05-01T00:00:00Z"], feed_fetcher=fake_fetcher
        )

    assert rc1 == 0 and rc2 == 0

    from scripts.lib.utils import today_utc_date

    shard = load_shard("bluesky", today_utc_date())
    assert len(shard["items"]) == 2
    ids = [it["id"] for it in shard["items"]]
    assert ids[0].startswith("bs-") and ids[1].startswith("bs-")
    assert len(set(ids)) == 2


def test_run_reporter_filter_narrows_to_one(tmp_path, monkeypatch, vocab):
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

    def fake_fetcher(actor, limit):
        actors_called.append(actor)
        return []

    with patch("scripts.lib.sources._fetch_with_retry", return_value=csv_text):
        rc = poll_bluesky.run(
            ["--dry-run", "--reporter", "b.bsky.social"],
            feed_fetcher=fake_fetcher,
        )

    assert rc == 0
    assert actors_called == ["did:plc:b"]


# ---------------------------------------------------------------------------
# --since / --since-hours CLI parity with the other pollers
# ---------------------------------------------------------------------------


def _single_reporter_csv() -> str:
    return (
        "Handle,Display Name,DID\n"
        "reporter.bsky.social,The Reporter,did:plc:r1\n"
    )


def _setup_run_env(tmp_path, monkeypatch) -> None:
    """Wire DATA_DIR + an empty overrides file for a run() invocation."""
    monkeypatch.setattr(shards, "DATA_DIR", tmp_path)
    overrides_path = tmp_path / "bluesky_overrides.json"
    overrides_path.write_text('{"add": [], "remove": []}')
    monkeypatch.setattr(poll_bluesky, "OVERRIDES_PATH", overrides_path)


def test_run_accepts_since_hours_flag(tmp_path, monkeypatch, vocab):
    """The flag the GH Actions workflow passes (--since-hours 24) must parse."""
    _setup_run_env(tmp_path, monkeypatch)
    fake_fetcher = lambda actor, limit: []  # noqa: E731
    with patch(
        "scripts.lib.sources._fetch_with_retry",
        return_value=_single_reporter_csv(),
    ):
        rc = poll_bluesky.run(
            ["--dry-run", "--since-hours", "24"],
            feed_fetcher=fake_fetcher,
        )
    assert rc == 0


def test_since_hours_computes_cutoff_in_the_past(tmp_path, monkeypatch, vocab):
    """--since-hours N → cutoff is now() - N hours (UTC)."""
    _setup_run_env(tmp_path, monkeypatch)

    # Build two posts: one inside the window, one outside.
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    inside = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    outside = (now - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")

    feed = [
        _feed_view(
            post=_post(
                uri="at://did:plc:r1/app.bsky.feed.post/inside",
                record=_record(created_at=inside),
            )
        ),
        _feed_view(
            post=_post(
                uri="at://did:plc:r1/app.bsky.feed.post/outside",
                record=_record(created_at=outside),
            )
        ),
    ]

    with patch(
        "scripts.lib.sources._fetch_with_retry",
        return_value=_single_reporter_csv(),
    ):
        rc = poll_bluesky.run(
            ["--since-hours", "24"], feed_fetcher=lambda a, l: feed
        )
    assert rc == 0

    from scripts.lib.utils import today_utc_date

    shard = load_shard("bluesky", today_utc_date())
    # Only the inside-window post lands; --since-hours 24 drops the 48h-old one.
    ids = [it["id"] for it in shard["items"]]
    assert any("inside" in i for i in ids)
    assert not any("outside" in i for i in ids)


def test_since_and_since_hours_are_mutually_exclusive(
    tmp_path, monkeypatch, capsys, vocab
):
    """Passing both must fail at argparse (exit 2)."""
    _setup_run_env(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as excinfo:
        poll_bluesky.run(
            [
                "--dry-run",
                "--since",
                "2026-05-01T00:00:00Z",
                "--since-hours",
                "24",
            ],
            feed_fetcher=lambda a, l: [],
        )
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "--since" in err and "--since-hours" in err


def test_default_when_neither_flag_given_is_24h(tmp_path, monkeypatch, vocab):
    """Neither flag → 24h-ago cutoff. Same default as the other pollers."""
    _setup_run_env(tmp_path, monkeypatch)

    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    inside = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    outside = (now - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")

    feed = [
        _feed_view(
            post=_post(
                uri="at://did:plc:r1/app.bsky.feed.post/inside",
                record=_record(created_at=inside),
            )
        ),
        _feed_view(
            post=_post(
                uri="at://did:plc:r1/app.bsky.feed.post/outside",
                record=_record(created_at=outside),
            )
        ),
    ]

    with patch(
        "scripts.lib.sources._fetch_with_retry",
        return_value=_single_reporter_csv(),
    ):
        rc = poll_bluesky.run([], feed_fetcher=lambda a, l: feed)
    assert rc == 0

    from scripts.lib.utils import today_utc_date

    shard = load_shard("bluesky", today_utc_date())
    ids = [it["id"] for it in shard["items"]]
    assert any("inside" in i for i in ids)
    assert not any("outside" in i for i in ids)
