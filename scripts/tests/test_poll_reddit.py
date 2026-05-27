"""Tests for `scripts.poll_reddit`.

Fixtures:
  - `fixtures/reddit_nba_top.xml`: 6 Atom entries covering text/self,
    link-post-without-selftext, long-selftext, old (>24h), emoji title,
    and a deleted-author entry.
  - `fixtures/reddit_nba_hot.xml`: 2 entries, one of which shares a
    post id (`t3_1abc234`) with the top fixture — used to test
    cross-feed dedup.

Tests inject a `feed_fetcher` to bypass the network. Privacy rules
from DESIGN.md 4.4 are exercised explicitly:
  - `url` is always the reddit thread (comments) URL.
  - body_excerpt is capped at 280 and only contains selftext.
  - Link posts have no body_excerpt.
  - Comment text is never read.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import feedparser
import pytest
import requests

from scripts import poll_reddit
from scripts.lib import shards
from scripts.lib.canonical import load_canonical
from scripts.lib.shards import load_shard, validate_item
from scripts.poll_reddit import (
    cap_excerpt,
    collect_items,
    extract_post_id,
    extract_selftext,
    extract_username,
    fetch_feed,
    is_reddit_thread_url,
    load_config,
    map_entry_to_item,
    normalize_thread_url,
    strip_html,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def top_feed():
    return feedparser.parse((FIXTURES / "reddit_nba_top.xml").read_bytes())


@pytest.fixture(scope="module")
def hot_feed():
    return feedparser.parse((FIXTURES / "reddit_nba_hot.xml").read_bytes())


@pytest.fixture(scope="module")
def vocab():
    return load_canonical()


# ---------------------------------------------------------------------------
# Selftext extraction + boilerplate stripping
# ---------------------------------------------------------------------------


def test_extract_selftext_returns_only_between_sc_markers():
    html = (
        '<!-- SC_OFF --><div class="md"><p>The actual post text.</p></div>'
        "<!-- SC_ON --> &#32; submitted by &#32; "
        '<a href="https://www.reddit.com/user/x">/u/x</a> <br/> '
        '<a href="https://www.reddit.com/r/nba/comments/abc/">[comments]</a>'
    )
    assert extract_selftext(html) == "The actual post text."


def test_extract_selftext_returns_none_for_link_post():
    # Link posts have no SC_OFF / SC_ON wrappers — just submitted-by + link.
    html = (
        '&#32; submitted by &#32; <a href="x">/u/x</a> <br/> '
        '<a href="https://youtu.be/abc">[link]</a> '
        '<a href="https://www.reddit.com/r/nba/comments/def/">[comments]</a>'
    )
    assert extract_selftext(html) is None


def test_extract_selftext_returns_none_for_empty_html():
    assert extract_selftext("") is None
    assert extract_selftext(None) is None  # type: ignore[arg-type]


def test_strip_html_decodes_and_collapses():
    out = strip_html("<p>line 1</p>\n  <p>line&apos;s 2</p>")
    assert out == "line 1 line's 2"


# ---------------------------------------------------------------------------
# Excerpt cap
# ---------------------------------------------------------------------------


def test_cap_excerpt_short_text_unchanged():
    text = "Short post."
    assert cap_excerpt(text, 280) == text


def test_cap_excerpt_caps_long_text_at_word_boundary():
    text = "word " * 200  # 1000 chars
    out = cap_excerpt(text, 280)
    # Cap honored
    assert len(out) <= 281  # +1 for ellipsis
    # Ellipsis appended
    assert out.endswith("…")
    # Cut at a word boundary, not mid-word
    body = out.rstrip("…").rstrip()
    assert not body.endswith("wor")


def test_cap_excerpt_keeps_unicode_intact():
    text = "Wemby 🏀 had a huge night " + "x" * 400
    out = cap_excerpt(text, 280)
    assert "🏀" in out
    assert len(out) <= 281


# ---------------------------------------------------------------------------
# Post id + username extraction
# ---------------------------------------------------------------------------


def test_extract_post_id_from_t3_id(top_feed):
    assert extract_post_id(top_feed.entries[0]) == "t3_1abc234"


def test_extract_post_id_falls_back_to_url():
    # If feedparser ever stops giving us t3_ ids, scrape the comments URL.
    fake = {
        "id": "tag:reddit.com,2008:/r/nba/comments/9zzzzzz/",
        "link": "https://www.reddit.com/r/nba/comments/9zzzzzz/foo/",
    }
    assert extract_post_id(fake) == "t3_9zzzzzz"


def test_extract_post_id_returns_none_when_unresolvable():
    assert extract_post_id({"id": "", "link": ""}) is None


def test_extract_username_strips_slash_u(top_feed):
    assert extract_username(top_feed.entries[0]) == "sportsfan42"


def test_extract_username_handles_deleted_author(top_feed):
    # 6th entry in the fixture has no <author> tag.
    deleted_entry = top_feed.entries[5]
    assert extract_username(deleted_entry) == "[deleted]"


# ---------------------------------------------------------------------------
# Thread URL guard (the critical privacy rule)
# ---------------------------------------------------------------------------


def test_is_reddit_thread_url_accepts_canonical():
    assert is_reddit_thread_url(
        "https://www.reddit.com/r/nba/comments/abc123/title_slug/"
    )


def test_is_reddit_thread_url_accepts_old_and_new_subdomains():
    assert is_reddit_thread_url(
        "https://old.reddit.com/r/nba/comments/abc123/"
    )
    assert is_reddit_thread_url(
        "https://new.reddit.com/r/nba/comments/abc123/"
    )


def test_is_reddit_thread_url_rejects_external():
    assert not is_reddit_thread_url("https://youtu.be/abc")
    assert not is_reddit_thread_url("https://espn.com/nba/story/123")


def test_is_reddit_thread_url_rejects_reddit_non_thread():
    # User profile, subreddit homepage, search — not allowed as `url`.
    assert not is_reddit_thread_url("https://www.reddit.com/user/x")
    assert not is_reddit_thread_url("https://www.reddit.com/r/nba/")


def test_normalize_thread_url_passthrough_for_thread_link():
    url = "https://www.reddit.com/r/nba/comments/abc/title/"
    assert normalize_thread_url(url, "t3_abc", "nba") == url


def test_normalize_thread_url_reconstructs_for_external_link():
    # If the feed gives an external article URL (link post), we
    # rebuild the thread URL from the post id + subreddit.
    rebuilt = normalize_thread_url("https://espn.com/article", "t3_xyz789", "nba")
    assert rebuilt == "https://www.reddit.com/r/nba/comments/xyz789/"


def test_normalize_thread_url_returns_none_when_unrecoverable():
    assert normalize_thread_url("https://espn.com/article", None, "nba") is None


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def test_map_entry_valid_selfpost(top_feed, vocab):
    players, teams = vocab
    item = map_entry_to_item(top_feed.entries[0], "nba", players, teams, 280)
    assert item is not None
    assert validate_item(item) == []
    assert item["source"] == "reddit"
    assert item["id"] == "rd-t3_1abc234"
    # url is the reddit thread — never anything else.
    assert item["url"].startswith("https://www.reddit.com/r/nba/comments/1abc234/")
    assert item["author"]["handle"] == "sportsfan42"
    assert item["author"]["url"] == "https://www.reddit.com/user/sportsfan42"
    # Selftext extracted, comments boilerplate stripped.
    assert "body_excerpt" in item
    assert "submitted by" not in item["body_excerpt"]
    assert "[comments]" not in item["body_excerpt"]
    assert "LeBron James" in item["body_excerpt"]


def test_map_entry_extracts_media_thumbnail_when_present(vocab):
    """Reddit RSS <media:thumbnail url="..."/> -> item.thumbnail."""
    players, teams = vocab
    xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<feed xmlns="http://www.w3.org/2005/Atom" '
        b'xmlns:media="http://search.yahoo.com/mrss/">'
        b"<entry>"
        b"<author><name>/u/x</name></author>"
        b"<id>t3_abc</id>"
        b'<link href="https://www.reddit.com/r/nba/comments/abc/title/"/>'
        b"<published>2026-05-27T14:30:00+00:00</published>"
        b"<title>Wemby block</title>"
        b'<media:thumbnail url="https://b.thumbs.redditmedia.com/thumb.jpg" '
        b'width="140" height="140"/>'
        b"</entry></feed>"
    )
    e = feedparser.parse(xml).entries[0]
    item = map_entry_to_item(e, "nba", players, teams, 280)
    assert item is not None
    assert item.get("thumbnail") == "https://b.thumbs.redditmedia.com/thumb.jpg"
    # url stays the reddit thread — thumbnail does not change the
    # privacy posture.
    assert item["url"].startswith("https://www.reddit.com/r/nba/comments/abc")


def test_map_entry_no_media_no_thumbnail(top_feed, vocab):
    """An entry with no media:thumbnail must NOT have a thumbnail field."""
    players, teams = vocab
    item = map_entry_to_item(top_feed.entries[0], "nba", players, teams, 280)
    assert "thumbnail" not in item


def test_map_entry_link_post_omits_body_excerpt(top_feed, vocab):
    """Highlights post — no selftext → no body_excerpt key at all."""
    players, teams = vocab
    item = map_entry_to_item(top_feed.entries[1], "nba", players, teams, 280)
    assert item is not None
    assert "body_excerpt" not in item
    # url is STILL the reddit thread, not the external youtu.be link.
    assert "youtu.be" not in item["url"]
    assert item["url"].startswith("https://www.reddit.com/r/nba/comments/1def567/")


def test_map_entry_long_selftext_capped_at_max(top_feed, vocab):
    players, teams = vocab
    item = map_entry_to_item(top_feed.entries[2], "nba", players, teams, 280)
    assert item is not None
    excerpt = item["body_excerpt"]
    # Length capped (allow +1 for the ellipsis char).
    assert len(excerpt) <= 281
    assert excerpt.endswith("…")


def test_map_entry_emoji_title_preserved(top_feed, vocab):
    players, teams = vocab
    item = map_entry_to_item(top_feed.entries[4], "nba", players, teams, 280)
    assert "🦌" in item["title"]
    # Giannis is in canonical → should be tagged.
    assert "giannis-antetokounmpo" in item["players"]


def test_map_entry_entity_detection_from_title(top_feed, vocab):
    players, teams = vocab
    item = map_entry_to_item(top_feed.entries[0], "nba", players, teams, 280)
    # Title: "LeBron drops 40 as Lakers beat Celtics by 25"
    assert "lebron-james" in item["players"]
    assert "los-angeles-lakers" in item["teams"]
    assert "boston-celtics" in item["teams"]


def test_map_entry_engagement_block_is_all_null(top_feed, vocab):
    """Reddit RSS doesn't expose live scores, so engagement is all null."""
    players, teams = vocab
    item = map_entry_to_item(top_feed.entries[0], "nba", players, teams, 280)
    assert item["engagement"] == {
        "likes": None,
        "reposts": None,
        "comments": None,
        "score": None,
        "views": None,
    }


def test_map_entry_deleted_author_marked(top_feed, vocab):
    players, teams = vocab
    item = map_entry_to_item(top_feed.entries[5], "nba", players, teams, 280)
    assert item["author"]["handle"] == "[deleted]"
    assert item["author"]["url"] is None


# ---------------------------------------------------------------------------
# fetch_feed: 429 retry behavior
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status: int, content: bytes = b""):
        self.status_code = status
        self.content = content

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class _FakeSession:
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls = 0
        self.last_url: str | None = None

    def get(self, url, timeout=None):
        self.calls += 1
        self.last_url = url
        return self._responses.pop(0)


def test_fetch_feed_targets_correct_url():
    body = (FIXTURES / "reddit_nba_top.xml").read_bytes()
    session = _FakeSession([_FakeResp(200, body)])
    fetch_feed(session, "nba", "top/.rss?t=day", limit=50, backoff_sec=0)
    assert session.last_url == "https://www.reddit.com/r/nba/top/.rss?t=day"


def test_fetch_feed_retries_once_on_429_then_succeeds():
    body = (FIXTURES / "reddit_nba_top.xml").read_bytes()
    session = _FakeSession([_FakeResp(429), _FakeResp(200, body)])
    entries = fetch_feed(
        session, "nba", "top/.rss?t=day", limit=50, backoff_sec=0
    )
    assert session.calls == 2
    assert len(entries) > 0


def test_fetch_feed_raises_after_second_429():
    session = _FakeSession([_FakeResp(429), _FakeResp(429)])
    with pytest.raises(requests.HTTPError):
        fetch_feed(session, "nba", "top/.rss?t=day", limit=50, backoff_sec=0)
    assert session.calls == 2


def test_fetch_feed_retries_on_403():
    body = (FIXTURES / "reddit_nba_top.xml").read_bytes()
    session = _FakeSession([_FakeResp(403), _FakeResp(200, body)])
    entries = fetch_feed(
        session, "nba", "top/.rss?t=day", limit=50, backoff_sec=0
    )
    assert session.calls == 2
    assert len(entries) > 0


# ---------------------------------------------------------------------------
# collect_items: dedup, since, error isolation
# ---------------------------------------------------------------------------


def test_collect_items_filters_old_and_dedupes_across_feeds(
    top_feed, hot_feed, vocab
):
    """Post that appears in both top and hot dedupes to one item."""
    players, teams = vocab

    def fetcher(subreddit, feed_path, limit):
        if "top" in feed_path:
            return list(top_feed.entries)
        if "hot" in feed_path:
            return list(hot_feed.entries)
        return []

    items, stats = collect_items(
        subreddits=["nba"],
        feeds=["top/.rss?t=day", "hot/.rss"],
        feed_fetcher=fetcher,
        players_dict=players,
        teams_dict=teams,
        since_iso="2026-05-21T00:00:00Z",
        max_per_feed=50,
        excerpt_max_chars=280,
        sleep_sec=0,
    )

    ids = sorted(it["id"] for it in items)
    # The shared post (rd-t3_1abc234) appears once.
    assert ids.count("rd-t3_1abc234") == 1
    # Top-only entries: 1abc234, 1def567, 1ghi890, 1mno345, 1pqr678 (deleted) — old one (1jkl012) excluded
    # Hot-only entries: 1stu901
    # Hot also has the dup 1abc234 which dedupes.
    assert "rd-t3_1stu901" in ids  # only-in-hot kept
    assert "rd-t3_1jkl012" not in ids  # old, dropped by since
    # Dedup counter ticked exactly once.
    assert stats["deduped"] == 1
    # Old-news dropped exactly once.
    assert stats["dropped_since"] == 1
    assert stats["feed_errors"] == 0


def test_collect_items_isolates_failing_feed(top_feed, vocab):
    players, teams = vocab

    def fetcher(subreddit, feed_path, limit):
        if "hot" in feed_path:
            raise requests.HTTPError("429")
        return list(top_feed.entries)

    items, stats = collect_items(
        subreddits=["nba"],
        feeds=["top/.rss?t=day", "hot/.rss"],
        feed_fetcher=fetcher,
        players_dict=players,
        teams_dict=teams,
        since_iso="2026-05-21T00:00:00Z",
        max_per_feed=50,
        excerpt_max_chars=280,
        sleep_sec=0,
    )
    assert stats["feed_errors"] == 1
    assert len(items) > 0  # top still produced items


def test_collect_items_every_item_url_is_reddit_thread(
    top_feed, hot_feed, vocab
):
    """Hard guarantee from DESIGN.md 4.4 — sweeps the full output."""
    players, teams = vocab

    def fetcher(subreddit, feed_path, limit):
        return list(top_feed.entries) + list(hot_feed.entries)

    items, _ = collect_items(
        subreddits=["nba"],
        feeds=["top/.rss?t=day"],
        feed_fetcher=fetcher,
        players_dict=players,
        teams_dict=teams,
        since_iso="2026-05-21T00:00:00Z",
        max_per_feed=50,
        excerpt_max_chars=280,
        sleep_sec=0,
    )
    assert len(items) > 0
    for item in items:
        assert is_reddit_thread_url(item["url"]), (
            f"Non-reddit-thread url leaked into shard: {item['url']}"
        )


# ---------------------------------------------------------------------------
# Full run() — dry-run, write, all-fail, missing config
# ---------------------------------------------------------------------------


def _write_test_config(path: Path):
    path.write_text(
        json.dumps(
            {
                "subreddits": ["nba"],
                "feeds": ["top/.rss?t=day", "hot/.rss"],
                "excerpt_max_chars": 280,
            }
        )
    )


def test_run_dry_run_writes_nothing(tmp_path, monkeypatch, capsys, top_feed):
    monkeypatch.setattr(shards, "DATA_DIR", tmp_path)
    config_path = tmp_path / "reddit_config.json"
    _write_test_config(config_path)

    def fetcher(subreddit, feed_path, limit):
        return list(top_feed.entries)

    rc = poll_reddit.run(
        ["--dry-run", "--since-hours", "999"],
        feed_fetcher=fetcher,
        config_path=config_path,
        sleep_sec=0,
    )
    assert rc == 0
    captured = capsys.readouterr().out
    assert "DRY RUN" in captured
    # No shard written.
    assert not (tmp_path / "reddit").exists()


def test_run_writes_shard(tmp_path, monkeypatch, top_feed, hot_feed):
    monkeypatch.setattr(shards, "DATA_DIR", tmp_path)
    config_path = tmp_path / "reddit_config.json"
    _write_test_config(config_path)

    def fetcher(subreddit, feed_path, limit):
        if "top" in feed_path:
            return list(top_feed.entries)
        return list(hot_feed.entries)

    # Use a wide --since window so the test isn't time-fragile relative
    # to the fixture timestamps (the fixtures are dated 2026-05-21).
    rc = poll_reddit.run(
        ["--since-hours", "8760"],
        feed_fetcher=fetcher,
        config_path=config_path,
        sleep_sec=0,
    )
    assert rc == 0

    shard = load_shard("reddit", "2026-05-21")
    assert len(shard["items"]) > 0
    # All items hit the privacy rule.
    for item in shard["items"]:
        assert item["url"].startswith("https://www.reddit.com/r/nba/comments/")


def test_run_all_feeds_fail_returns_exit_1(tmp_path, monkeypatch):
    monkeypatch.setattr(shards, "DATA_DIR", tmp_path)
    config_path = tmp_path / "reddit_config.json"
    _write_test_config(config_path)

    def fetcher(subreddit, feed_path, limit):
        raise requests.HTTPError("429 every time")

    rc = poll_reddit.run(
        ["--since-hours", "24"],
        feed_fetcher=fetcher,
        config_path=config_path,
        sleep_sec=0,
    )
    assert rc == 1


def test_run_missing_config_returns_exit_1(tmp_path, monkeypatch):
    monkeypatch.setattr(shards, "DATA_DIR", tmp_path)
    rc = poll_reddit.run(
        ["--dry-run"],
        feed_fetcher=lambda s, f, l: [],
        config_path=tmp_path / "does_not_exist.json",
        sleep_sec=0,
    )
    assert rc == 1


def test_run_subreddit_and_feed_overrides(tmp_path, monkeypatch, top_feed):
    monkeypatch.setattr(shards, "DATA_DIR", tmp_path)
    config_path = tmp_path / "reddit_config.json"
    _write_test_config(config_path)

    calls: list[tuple[str, str]] = []

    def fetcher(subreddit, feed_path, limit):
        calls.append((subreddit, feed_path))
        return list(top_feed.entries)

    rc = poll_reddit.run(
        ["--dry-run", "--subreddit", "nbatestoverride", "--feed", "new/.rss"],
        feed_fetcher=fetcher,
        config_path=config_path,
        sleep_sec=0,
    )
    assert rc == 0
    # Exactly one call, with the overrides.
    assert calls == [("nbatestoverride", "new/.rss")]


def test_run_shipped_config_loads():
    """The shipped reddit_config.json must be parseable + r/nba only for v1."""
    config = load_config(
        Path(__file__).resolve().parent.parent.parent
        / "data"
        / "sources"
        / "reddit_config.json"
    )
    assert config["subreddits"] == ["nba"]
    # Hard cap from DESIGN.md 4.4.
    assert config["excerpt_max_chars"] <= 280
