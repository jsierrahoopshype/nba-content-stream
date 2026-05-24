"""Tests for `scripts.poll_substack`.

Fixture: `fixtures/substack_feed.xml` — 5 entries covering:
  - Free post with full-ish content mentioning a known player + team
  - Long-form post (excerpt must cap at 280 chars at a word boundary)
  - Post with HTML in content (must be stripped)
  - Old post (>24h, must be dropped by --since-hours)
  - Emoji + unicode in title + a known player (Wemby) tag

Tests inject a `feed_fetcher` to bypass the network and use a real
fixture publication record (slug=marcstein, name="The Stein Line").
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import feedparser
import pytest
import requests

from scripts import poll_substack
from scripts.lib import shards
from scripts.lib.canonical import load_canonical
from scripts.lib.shards import load_shard, validate_item
from scripts.poll_substack import (
    EXCERPT_MAX_CHARS,
    _entry_excerpt,
    _post_slug_from_link,
    collect_items,
    fetch_feed,
    load_config,
    make_item_id,
    map_entry_to_item,
)

FIXTURES = Path(__file__).parent / "fixtures"
PUBLICATION = {
    "slug": "marcstein",
    "name": "The Stein Line",
    "feed": "https://marcstein.substack.com/feed",
}


@pytest.fixture(scope="module")
def feed():
    return feedparser.parse((FIXTURES / "substack_feed.xml").read_bytes())


@pytest.fixture(scope="module")
def vocab():
    return load_canonical()


# ---------------------------------------------------------------------------
# Post slug + id
# ---------------------------------------------------------------------------


def test_post_slug_from_link_extracts_p_segment():
    assert _post_slug_from_link(
        "https://marcstein.substack.com/p/lakers-summer-dilemma"
    ) == "lakers-summer-dilemma"


def test_post_slug_from_link_lowercases():
    assert _post_slug_from_link(
        "https://x.substack.com/p/MyPost-2026"
    ) == "mypost-2026"


def test_post_slug_from_link_returns_none_for_non_post_url():
    assert _post_slug_from_link("https://x.substack.com/") is None
    assert _post_slug_from_link("") is None


def test_make_item_id_uses_post_slug_when_available(feed):
    item_id = make_item_id("marcstein", feed.entries[0])
    assert item_id == "ss-marcstein-lakers-summer-dilemma"
    # Required prefix per SHARD_FORMAT.md.
    assert item_id.startswith("ss-")


def test_make_item_id_falls_back_to_link_hash():
    fake = {"link": "https://example.substack.com/some-non-standard-path"}
    item_id = make_item_id("example", fake)
    assert item_id is not None
    assert item_id.startswith("ss-example-")
    # 16 hex chars after the prefix.
    hex_part = item_id[len("ss-example-") :]
    assert len(hex_part) == 16
    assert all(c in "0123456789abcdef" for c in hex_part)


def test_make_item_id_stable_for_same_link():
    a = make_item_id("pub", {"link": "https://x.substack.com/some/path"})
    b = make_item_id("pub", {"link": "https://x.substack.com/some/path"})
    assert a == b


def test_make_item_id_returns_none_when_link_missing():
    assert make_item_id("pub", {"link": ""}) is None
    assert make_item_id("pub", {}) is None


# ---------------------------------------------------------------------------
# Excerpt extraction + cap
# ---------------------------------------------------------------------------


def test_excerpt_strips_html_and_decodes_entities(feed):
    """Wemby entry uses &apos; + <strong> tags — both must be cleaned."""
    excerpt = _entry_excerpt(feed.entries[2])  # Wemby post
    assert "<strong>" not in excerpt and "<p>" not in excerpt
    assert "&apos;" not in excerpt and "&ldquo;" not in excerpt
    # The decoded apostrophe should be present.
    assert "Wembanyama's" in excerpt


def test_excerpt_caps_at_max_with_word_boundary(feed):
    """Long-form Celtics post → must cap at 280, no mid-word break."""
    excerpt = _entry_excerpt(feed.entries[1])
    assert len(excerpt) <= EXCERPT_MAX_CHARS + 1  # +1 for ellipsis
    assert excerpt.endswith("…")
    # Sanity: cut at a word boundary.
    body = excerpt.rstrip("…").rstrip()
    last_token = body.rsplit(" ", 1)[-1]
    # Last token shouldn't be a half-word fragment of likely text.
    assert len(last_token) > 1


def test_excerpt_prefers_content_over_summary(feed):
    """`content:encoded` is fuller than `description`; use it when present."""
    # Entry 0's content has "Sources around the league..." which is NOT
    # in the shorter description. If we picked summary we'd miss it.
    excerpt = _entry_excerpt(feed.entries[0])
    assert "Sources around the league" in excerpt


def test_excerpt_empty_when_no_payload():
    fake = {}
    assert _entry_excerpt(fake) == ""


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def test_map_entry_produces_valid_item(feed, vocab):
    players, teams = vocab
    item = map_entry_to_item(feed.entries[0], PUBLICATION, players, teams)
    assert item is not None
    assert validate_item(item) == []
    assert item["source"] == "substack"
    assert item["id"] == "ss-marcstein-lakers-summer-dilemma"
    assert item["url"] == "https://marcstein.substack.com/p/lakers-summer-dilemma"
    assert item["author"]["handle"] == "marcstein"
    assert item["author"]["display_name"] == "The Stein Line"
    assert item["author"]["url"] == "https://marcstein.substack.com"
    assert item["media"]["type"] == "text"
    # No engagement, no extraction block.
    assert "engagement" not in item
    assert "extraction" not in item


def test_map_entry_detects_entities_from_title_and_excerpt(feed, vocab):
    players, teams = vocab
    item = map_entry_to_item(feed.entries[0], PUBLICATION, players, teams)
    # Title mentions Lakers; excerpt mentions LeBron James + Anthony Davis.
    assert "lebron-james" in item["players"]
    assert "anthony-davis" in item["players"]
    assert "los-angeles-lakers" in item["teams"]


def test_map_entry_tags_team_from_excerpt(feed, vocab):
    """Celtics entry mentions Boston Celtics + Cleveland Cavaliers in the body."""
    players, teams = vocab
    item = map_entry_to_item(feed.entries[1], PUBLICATION, players, teams)
    assert "boston-celtics" in item["teams"]
    assert "cleveland-cavaliers" in item["teams"]


def test_map_entry_preserves_unicode_title(feed, vocab):
    players, teams = vocab
    item = map_entry_to_item(feed.entries[2], PUBLICATION, players, teams)
    assert "🦄" in item["title"]
    # Wemby is in canonical; San Antonio in body.
    assert "victor-wembanyama" in item["players"]
    assert "san-antonio-spurs" in item["teams"]


def test_map_entry_returns_none_when_published_missing(vocab):
    """Defensive: an entry without a publish time can't be ordered, so skip."""
    players, teams = vocab
    fake = {
        "title": "x",
        "link": "https://x.substack.com/p/y",
    }
    assert map_entry_to_item(fake, PUBLICATION, players, teams) is None


def test_map_entry_returns_none_when_link_missing(vocab):
    players, teams = vocab
    fake = {
        "title": "x",
        "published": "Thu, 21 May 2026 14:30:00 GMT",
    }
    assert map_entry_to_item(fake, PUBLICATION, players, teams) is None


# ---------------------------------------------------------------------------
# fetch_feed
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status: int, content: bytes = b""):
        self.status_code = status
        self.content = content

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class _FakeSession:
    def __init__(self, response: _FakeResp):
        self.response = response
        self.last_url: str | None = None

    def get(self, url, timeout=None):
        self.last_url = url
        return self.response


def test_fetch_feed_hits_publication_feed_url():
    body = (FIXTURES / "substack_feed.xml").read_bytes()
    session = _FakeSession(_FakeResp(200, body))
    entries = fetch_feed(session, PUBLICATION, limit=30)
    assert session.last_url == PUBLICATION["feed"]
    assert len(entries) > 0


def test_fetch_feed_raises_on_404():
    session = _FakeSession(_FakeResp(404))
    with pytest.raises(requests.HTTPError):
        fetch_feed(session, PUBLICATION, limit=30)


# ---------------------------------------------------------------------------
# collect_items
# ---------------------------------------------------------------------------


def test_collect_items_filters_since(feed, vocab):
    players, teams = vocab

    def fetcher(pub, limit):
        return list(feed.entries[:limit])

    items, stats = collect_items(
        publications=[PUBLICATION],
        feed_fetcher=fetcher,
        players_dict=players,
        teams_dict=teams,
        since_iso="2026-05-21T00:00:00Z",
        max_per_feed=30,
        sleep_sec=0,
    )
    # 5 entries in fixture; 1 is old (May 14) and gets dropped.
    assert stats["entries_seen"] == 5
    assert stats["dropped_since"] == 1
    assert stats["kept"] == 4
    assert stats["feed_errors"] == 0


def test_collect_items_dedupes_on_repeat_call(feed, vocab):
    players, teams = vocab

    def fetcher(pub, limit):
        return list(feed.entries[:limit]) + list(feed.entries[:limit])

    items, stats = collect_items(
        publications=[PUBLICATION],
        feed_fetcher=fetcher,
        players_dict=players,
        teams_dict=teams,
        since_iso="2026-05-21T00:00:00Z",
        max_per_feed=30,
        sleep_sec=0,
    )
    # Each entry appears twice in the feed; deduped to one per id.
    assert stats["deduped"] == 4
    assert stats["kept"] == 4


def test_collect_items_isolates_failing_feed(feed, vocab):
    players, teams = vocab

    def fetcher(pub, limit):
        if pub["slug"] == "broken":
            raise requests.HTTPError("404")
        return list(feed.entries[:limit])

    publications = [
        {"slug": "broken", "name": "broken pub", "feed": "x"},
        PUBLICATION,
    ]
    items, stats = collect_items(
        publications=publications,
        feed_fetcher=fetcher,
        players_dict=players,
        teams_dict=teams,
        since_iso="2026-05-21T00:00:00Z",
        max_per_feed=30,
        sleep_sec=0,
    )
    assert stats["feed_errors"] == 1
    assert len(items) > 0  # broken didn't kill the run


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


def _write_test_config(path: Path):
    path.write_text(
        json.dumps({"publications": [PUBLICATION]})
    )


def test_run_dry_run_writes_nothing(tmp_path, monkeypatch, capsys, feed):
    monkeypatch.setattr(shards, "DATA_DIR", tmp_path)
    config_path = tmp_path / "subs.json"
    _write_test_config(config_path)

    def fetcher(pub, limit):
        return list(feed.entries[:limit])

    rc = poll_substack.run(
        ["--dry-run", "--since-hours", "999"],
        feed_fetcher=fetcher,
        config_path=config_path,
        sleep_sec=0,
    )
    assert rc == 0
    captured = capsys.readouterr().out
    assert "DRY RUN" in captured
    assert not (tmp_path / "substack").exists()


def test_run_writes_shard_and_dedupes_on_rerun(tmp_path, monkeypatch, feed):
    monkeypatch.setattr(shards, "DATA_DIR", tmp_path)
    config_path = tmp_path / "subs.json"
    _write_test_config(config_path)

    def fetcher(pub, limit):
        return list(feed.entries[:limit])

    # First run.
    rc1 = poll_substack.run(
        ["--since-hours", "8760"],
        feed_fetcher=fetcher,
        config_path=config_path,
        sleep_sec=0,
    )
    assert rc1 == 0

    # Second run — same items → 0 appended via shard-level id dedup.
    rc2 = poll_substack.run(
        ["--since-hours", "8760"],
        feed_fetcher=fetcher,
        config_path=config_path,
        sleep_sec=0,
    )
    assert rc2 == 0

    shard_2026_05_21 = load_shard("substack", "2026-05-21")
    ids = [it["id"] for it in shard_2026_05_21["items"]]
    assert all(i.startswith("ss-marcstein-") for i in ids)
    assert len(set(ids)) == len(ids)  # no duplicates


def test_run_all_feeds_fail_returns_1(tmp_path, monkeypatch):
    monkeypatch.setattr(shards, "DATA_DIR", tmp_path)
    config_path = tmp_path / "subs.json"
    _write_test_config(config_path)

    def fetcher(pub, limit):
        raise requests.HTTPError("503")

    rc = poll_substack.run(
        ["--since-hours", "24"],
        feed_fetcher=fetcher,
        config_path=config_path,
        sleep_sec=0,
    )
    assert rc == 1


def test_run_missing_config_returns_1(tmp_path, monkeypatch):
    monkeypatch.setattr(shards, "DATA_DIR", tmp_path)
    rc = poll_substack.run(
        ["--dry-run"],
        feed_fetcher=lambda p, l: [],
        config_path=tmp_path / "missing.json",
        sleep_sec=0,
    )
    assert rc == 1


def test_run_publication_override_filters(tmp_path, monkeypatch, feed):
    monkeypatch.setattr(shards, "DATA_DIR", tmp_path)
    config_path = tmp_path / "subs.json"
    config_path.write_text(
        json.dumps(
            {
                "publications": [
                    PUBLICATION,
                    {"slug": "other", "name": "Other", "feed": "x"},
                ]
            }
        )
    )
    calls: list[str] = []

    def fetcher(pub, limit):
        calls.append(pub["slug"])
        return list(feed.entries[:limit])

    rc = poll_substack.run(
        ["--dry-run", "--publication", "marcstein"],
        feed_fetcher=fetcher,
        config_path=config_path,
        sleep_sec=0,
    )
    assert rc == 0
    assert calls == ["marcstein"]


def test_run_shipped_config_loads():
    """The shipped publications.json must be parseable + have publications."""
    config = load_config(
        Path(__file__).resolve().parent.parent.parent
        / "data"
        / "sources"
        / "substack_publications.json"
    )
    assert len(config["publications"]) >= 1
    for pub in config["publications"]:
        assert pub["slug"] and pub["name"] and pub["feed"]
        assert pub["feed"].startswith("https://")
