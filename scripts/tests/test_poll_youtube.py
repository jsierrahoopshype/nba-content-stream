"""Tests for `scripts.poll_youtube`.

Fixtures:
  - `fixtures/youtube_channels_list.json` — a `channels.list` response
    for 2 fake channels with `contentDetails.relatedPlaylists.uploads`
    populated. Used to test the resolution + cache path.
  - `fixtures/youtube_playlist_items.json` — a `playlistItems.list`
    response with 5 entries covering: normal NBA episode (entity-
    tagged), long description (excerpt cap at 280), empty description
    (body_excerpt omitted), old episode (>24h, dropped by since), and
    an emoji-in-title episode with a known player.

The two API callers (`channels_fetcher`, `playlist_items_fetcher`)
are dependency-injected so tests never hit the network.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from scripts import poll_youtube
from scripts.lib import shards
from scripts.lib.canonical import load_canonical
from scripts.lib.shards import load_shard, validate_item
from scripts.poll_youtube import (
    EXCERPT_MAX_CHARS,
    _best_thumbnail,
    _chunked,
    _excerpt,
    collect_items,
    load_channel_cache,
    load_channel_list,
    map_playlist_item_to_item,
    resolve_channels,
    save_channel_cache,
)

FIXTURES = Path(__file__).parent / "fixtures"
CHANNEL_ID = "UCfakeOGsShow0000000001"
UPLOADS_ID = "UUfakeOGsShow0000000001"


@pytest.fixture(scope="module")
def channels_response():
    return json.loads((FIXTURES / "youtube_channels_list.json").read_text())


@pytest.fixture(scope="module")
def playlist_items_response():
    return json.loads((FIXTURES / "youtube_playlist_items.json").read_text())


@pytest.fixture(scope="module")
def vocab():
    return load_canonical()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_chunked_batches_at_size():
    assert _chunked(list("abcdef"), 2) == [["a", "b"], ["c", "d"], ["e", "f"]]
    assert _chunked(list("abc"), 5) == [["a", "b", "c"]]
    assert _chunked([], 50) == []


def test_best_thumbnail_prefers_maxres(playlist_items_response):
    snippet = playlist_items_response["items"][0]["snippet"]
    assert _best_thumbnail(snippet["thumbnails"]) == (
        "https://i.ytimg.com/vi/abc111/maxresdefault.jpg"
    )


def test_best_thumbnail_falls_back_to_high(playlist_items_response):
    """The second fixture entry only has `high` — that should be picked."""
    snippet = playlist_items_response["items"][1]["snippet"]
    assert _best_thumbnail(snippet["thumbnails"]) == (
        "https://i.ytimg.com/vi/abc222/hqdefault.jpg"
    )


def test_best_thumbnail_returns_none_for_empty():
    assert _best_thumbnail({}) is None
    assert _best_thumbnail(None) is None  # type: ignore[arg-type]


def test_excerpt_collapses_and_caps():
    long_desc = "word " * 200  # 1000 chars after collapse
    out = _excerpt(long_desc)
    assert len(out) <= EXCERPT_MAX_CHARS + 1  # +1 for ellipsis
    assert out.endswith("…")


def test_excerpt_short_unchanged():
    assert _excerpt("Hello world") == "Hello world"


def test_excerpt_empty_string():
    assert _excerpt("") == ""


# ---------------------------------------------------------------------------
# Channel cache + resolution
# ---------------------------------------------------------------------------


def test_load_save_cache_round_trip(tmp_path):
    path = tmp_path / "cache.json"
    cache = {CHANNEL_ID: {"uploads_playlist_id": UPLOADS_ID, "channel_title": "X", "resolved_at": "2026-05-24T00:00:00Z"}}
    save_channel_cache(path, cache)
    assert load_channel_cache(path) == cache


def test_load_cache_missing_file_returns_empty(tmp_path):
    assert load_channel_cache(tmp_path / "missing.json") == {}


def test_save_cache_preserves_meta(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"_meta": {"x": 1}, "cache": {}}))
    save_channel_cache(path, {CHANNEL_ID: {"uploads_playlist_id": UPLOADS_ID}})
    blob = json.loads(path.read_text())
    assert blob["_meta"] == {"x": 1}
    assert blob["cache"][CHANNEL_ID]["uploads_playlist_id"] == UPLOADS_ID


def test_resolve_channels_populates_cache_from_api(channels_response):
    cache = {}
    calls = {"n": 0}

    def fetcher(ids):
        calls["n"] += 1
        return channels_response

    resolved, units = resolve_channels(
        [CHANNEL_ID, "UCfakeRingerNBA0000000002"], cache, fetcher
    )
    assert units == 1
    assert resolved[CHANNEL_ID]["uploads_playlist_id"] == UPLOADS_ID
    assert resolved[CHANNEL_ID]["channel_title"] == "TheOGsShow"
    # Cache was mutated in-place.
    assert cache[CHANNEL_ID]["uploads_playlist_id"] == UPLOADS_ID


def test_resolve_channels_skips_already_cached(channels_response):
    """Cached entries must NOT trigger a second channels.list call."""
    cache = {
        CHANNEL_ID: {
            "uploads_playlist_id": UPLOADS_ID,
            "channel_title": "TheOGsShow",
            "resolved_at": "2026-05-01T00:00:00Z",
        }
    }
    calls = {"n": 0}

    def fetcher(ids):
        calls["n"] += 1
        return channels_response

    resolved, units = resolve_channels([CHANNEL_ID], cache, fetcher)
    assert units == 0
    assert calls["n"] == 0
    assert resolved[CHANNEL_ID]["uploads_playlist_id"] == UPLOADS_ID


def test_resolve_channels_batches_at_50():
    """Many channels → multiple batched calls, each ≤50 IDs."""
    cache = {}
    seen_batches = []

    def fetcher(ids):
        seen_batches.append(list(ids))
        return {
            "items": [
                {
                    "id": cid,
                    "snippet": {"title": cid},
                    "contentDetails": {
                        "relatedPlaylists": {"uploads": "UU" + cid[2:]}
                    },
                }
                for cid in ids
            ]
        }

    ids = [f"UC{i:022d}" for i in range(120)]
    resolve_channels(ids, cache, fetcher)
    # 120 -> batches of 50, 50, 20.
    assert [len(b) for b in seen_batches] == [50, 50, 20]


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def test_map_playlist_item_produces_valid_item(playlist_items_response, vocab):
    players, teams = vocab
    item = map_playlist_item_to_item(
        playlist_items_response["items"][0], CHANNEL_ID, players, teams
    )
    assert item is not None
    assert validate_item(item) == []
    assert item["source"] == "youtube"
    assert item["id"] == "yt-abc111"
    assert item["url"] == "https://www.youtube.com/watch?v=abc111"
    assert item["author"]["handle"] == CHANNEL_ID
    assert item["author"]["display_name"] == "TheOGsShow"
    assert item["author"]["url"] == f"https://www.youtube.com/channel/{CHANNEL_ID}"
    assert item["media"]["type"] == "video"
    # Episodes-only — no engagement, no extraction.
    assert "engagement" not in item
    assert "extraction" not in item
    # Thumbnail picked the maxres URL.
    assert item["thumbnail"].endswith("maxresdefault.jpg")


def test_map_detects_entities_in_title_and_description(playlist_items_response, vocab):
    players, teams = vocab
    item = map_playlist_item_to_item(
        playlist_items_response["items"][0], CHANNEL_ID, players, teams
    )
    # Title: "LeBron James and the Lakers' offseason plan"
    # Description mentions Anthony Davis.
    assert "lebron-james" in item["players"]
    assert "anthony-davis" in item["players"]
    assert "los-angeles-lakers" in item["teams"]


def test_map_excerpt_caps_long_description(playlist_items_response, vocab):
    players, teams = vocab
    item = map_playlist_item_to_item(
        playlist_items_response["items"][1], CHANNEL_ID, players, teams
    )
    excerpt = item["body_excerpt"]
    assert len(excerpt) <= EXCERPT_MAX_CHARS + 1
    assert excerpt.endswith("…")


def test_map_omits_body_excerpt_when_description_empty(playlist_items_response, vocab):
    players, teams = vocab
    item = map_playlist_item_to_item(
        playlist_items_response["items"][2], CHANNEL_ID, players, teams
    )
    assert "body_excerpt" not in item


def test_map_preserves_unicode_title(playlist_items_response, vocab):
    players, teams = vocab
    item = map_playlist_item_to_item(
        playlist_items_response["items"][4], CHANNEL_ID, players, teams
    )
    assert "🦄" in item["title"]
    # Wemby + San Antonio in title/description.
    assert "victor-wembanyama" in item["players"]
    assert "san-antonio-spurs" in item["teams"]


def test_map_returns_none_when_video_id_missing(vocab):
    players, teams = vocab
    fake = {"snippet": {"title": "x", "publishedAt": "2026-05-21T14:30:00Z"}}
    assert map_playlist_item_to_item(fake, CHANNEL_ID, players, teams) is None


def test_map_returns_none_when_published_missing(vocab):
    players, teams = vocab
    fake = {
        "snippet": {
            "title": "x",
            "resourceId": {"videoId": "abc"},
        }
    }
    assert map_playlist_item_to_item(fake, CHANNEL_ID, players, teams) is None


# ---------------------------------------------------------------------------
# collect_items: filter, dedup, error isolation
# ---------------------------------------------------------------------------


def test_collect_items_filters_since_and_counts_quota(
    channels_response, playlist_items_response, vocab
):
    players, teams = vocab
    cache = {}

    def chan_fetcher(ids):
        return channels_response

    def pi_fetcher(playlist_id, max_results):
        return playlist_items_response

    items, stats = collect_items(
        [CHANNEL_ID],
        cache,
        chan_fetcher,
        pi_fetcher,
        players,
        teams,
        since_iso="2026-05-21T00:00:00Z",
        max_per_channel=10,
        sleep_sec=0,
    )
    # 5 entries in fixture, 1 dated 2026-05-14 → dropped by since.
    assert stats["videos_seen"] == 5
    assert stats["dropped_since"] == 1
    assert stats["kept"] == 4
    # Quota: 1 (channels.list batch) + 1 (playlistItems.list).
    assert stats["quota_units_estimate"] == 2
    assert stats["channel_errors"] == 0


def test_collect_items_uses_cache_no_second_channels_call(
    playlist_items_response, vocab
):
    """Pre-populated cache → channels_fetcher must NOT be invoked."""
    players, teams = vocab
    cache = {
        CHANNEL_ID: {
            "uploads_playlist_id": UPLOADS_ID,
            "channel_title": "TheOGsShow",
            "resolved_at": "2026-05-01T00:00:00Z",
        }
    }
    chan_calls = {"n": 0}

    def chan_fetcher(ids):
        chan_calls["n"] += 1
        return {"items": []}

    def pi_fetcher(playlist_id, max_results):
        return playlist_items_response

    items, stats = collect_items(
        [CHANNEL_ID],
        cache,
        chan_fetcher,
        pi_fetcher,
        players,
        teams,
        since_iso="2026-05-21T00:00:00Z",
        max_per_channel=10,
        sleep_sec=0,
    )
    assert chan_calls["n"] == 0
    # Quota: 0 (cached) + 1 (playlistItems) = 1.
    assert stats["quota_units_estimate"] == 1
    assert stats["kept"] == 4


def test_collect_items_isolates_failing_channel(
    channels_response, playlist_items_response, vocab
):
    players, teams = vocab
    cache = {}

    def chan_fetcher(ids):
        return channels_response

    def pi_fetcher(playlist_id, max_results):
        # The Ringer NBA channel's uploads playlist will error.
        if playlist_id == "UUfakeRingerNBA0000000002":
            raise requests.HTTPError("503")
        return playlist_items_response

    items, stats = collect_items(
        [CHANNEL_ID, "UCfakeRingerNBA0000000002"],
        cache,
        chan_fetcher,
        pi_fetcher,
        players,
        teams,
        since_iso="2026-05-21T00:00:00Z",
        max_per_channel=10,
        sleep_sec=0,
    )
    assert stats["channel_errors"] == 1
    assert stats["kept"] > 0


def test_collect_items_dedupes_repeat_videos(playlist_items_response, vocab):
    """Same video id surfaced twice (across channels or repeated calls) → one item."""
    players, teams = vocab
    cache = {
        CHANNEL_ID: {
            "uploads_playlist_id": UPLOADS_ID,
            "channel_title": "TheOGsShow",
            "resolved_at": "2026-05-01T00:00:00Z",
        },
        "UCfakeOther0000000000003": {
            "uploads_playlist_id": "UUfakeOther0000000000003",
            "channel_title": "Other",
            "resolved_at": "2026-05-01T00:00:00Z",
        },
    }

    def pi_fetcher(playlist_id, max_results):
        return playlist_items_response  # same items for both

    items, stats = collect_items(
        [CHANNEL_ID, "UCfakeOther0000000000003"],
        cache,
        lambda ids: {"items": []},
        pi_fetcher,
        players,
        teams,
        since_iso="2026-05-21T00:00:00Z",
        max_per_channel=10,
        sleep_sec=0,
    )
    assert stats["deduped"] == 4  # 4 fresh items repeated across 2 channels


# ---------------------------------------------------------------------------
# Channel list discovery (live + overrides)
# ---------------------------------------------------------------------------


def test_load_channel_list_parses_python_literal(tmp_path):
    """Confirms the AST-based parser handles the nba-podcast-stream app.py shape."""
    from scripts.lib import sources

    overrides_path = tmp_path / "youtube_overrides.json"
    overrides_path.write_text('{"add": [], "remove": []}')
    fake_app_py = """\
CHANNELS = [
    'UCxxx0000000000000000001',  # Channel One
    'UCxxx0000000000000000002',  # Channel Two
]
CONDITIONAL_CHANNELS = {'UCxxx0000000000000000099': {}}
"""
    with patch.object(sources, "_fetch_with_retry", return_value=fake_app_py):
        channels = load_channel_list(overrides_path)
    assert channels == [
        "UCxxx0000000000000000001",
        "UCxxx0000000000000000002",
    ]


def test_load_channel_list_applies_overrides(tmp_path):
    from scripts.lib import sources

    overrides_path = tmp_path / "youtube_overrides.json"
    overrides_path.write_text(
        json.dumps(
            {
                "add": ["UCextra0000000000000000099"],
                "remove": ["UCxxx0000000000000000001"],
            }
        )
    )
    fake_app_py = (
        "CHANNELS = ['UCxxx0000000000000000001', 'UCxxx0000000000000000002']"
    )
    with patch.object(sources, "_fetch_with_retry", return_value=fake_app_py):
        channels = load_channel_list(overrides_path)
    assert channels == [
        "UCxxx0000000000000000002",
        "UCextra0000000000000000099",
    ]


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


def _setup_run_env(tmp_path, monkeypatch):
    """Wire DATA_DIR, empty overrides, and an env API key."""
    monkeypatch.setattr(shards, "DATA_DIR", tmp_path)
    overrides_path = tmp_path / "youtube_overrides.json"
    overrides_path.write_text('{"add": [], "remove": []}')
    cache_path = tmp_path / "youtube_channel_cache.json"
    cache_path.write_text(json.dumps({"_meta": {}, "cache": {}}))
    return overrides_path, cache_path


def test_run_missing_api_key_returns_1(tmp_path, monkeypatch, caplog):
    """No injected fetchers + no env key → exit 1 with clear message."""
    _setup_run_env(tmp_path, monkeypatch)
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    with caplog.at_level("ERROR"):
        rc = poll_youtube.run(
            ["--dry-run"],
            overrides_path=tmp_path / "youtube_overrides.json",
            cache_path=tmp_path / "youtube_channel_cache.json",
        )
    assert rc == 1
    assert any("YOUTUBE_API_KEY" in rec.message for rec in caplog.records)


def test_run_dry_run_does_not_write_shard(
    tmp_path, monkeypatch, capsys, channels_response, playlist_items_response
):
    overrides_path, cache_path = _setup_run_env(tmp_path, monkeypatch)
    from scripts.lib import sources

    fake_app_py = f"CHANNELS = ['{CHANNEL_ID}']"

    chan_fetcher = lambda ids: channels_response  # noqa: E731
    pi_fetcher = lambda pl, n: playlist_items_response  # noqa: E731

    with patch.object(sources, "_fetch_with_retry", return_value=fake_app_py):
        rc = poll_youtube.run(
            ["--dry-run", "--since-hours", "999"],
            channels_fetcher=chan_fetcher,
            playlist_items_fetcher=pi_fetcher,
            overrides_path=overrides_path,
            cache_path=cache_path,
            sleep_sec=0,
        )

    assert rc == 0
    captured = capsys.readouterr().out
    assert "DRY RUN" in captured
    # No shard written.
    assert not (tmp_path / "youtube").exists()
    # Cache WAS persisted (newly-resolved entry).
    persisted = json.loads(cache_path.read_text())
    assert CHANNEL_ID in persisted["cache"]


def test_run_writes_shard_and_dedupes_on_rerun(
    tmp_path, monkeypatch, channels_response, playlist_items_response
):
    overrides_path, cache_path = _setup_run_env(tmp_path, monkeypatch)
    from scripts.lib import sources

    fake_app_py = f"CHANNELS = ['{CHANNEL_ID}']"

    def chan_fetcher(ids):
        return channels_response

    def pi_fetcher(playlist_id, max_results):
        return playlist_items_response

    with patch.object(sources, "_fetch_with_retry", return_value=fake_app_py):
        rc1 = poll_youtube.run(
            ["--since-hours", "8760"],
            channels_fetcher=chan_fetcher,
            playlist_items_fetcher=pi_fetcher,
            overrides_path=overrides_path,
            cache_path=cache_path,
            sleep_sec=0,
        )
        rc2 = poll_youtube.run(
            ["--since-hours", "8760"],
            channels_fetcher=chan_fetcher,
            playlist_items_fetcher=pi_fetcher,
            overrides_path=overrides_path,
            cache_path=cache_path,
            sleep_sec=0,
        )

    assert rc1 == 0 and rc2 == 0
    shard = load_shard("youtube", "2026-05-21")
    ids = [it["id"] for it in shard["items"]]
    assert len(set(ids)) == len(ids)
    assert all(i.startswith("yt-") for i in ids)
    assert "yt-abc111" in ids


def test_run_all_channels_fail_returns_1(
    tmp_path, monkeypatch, channels_response
):
    overrides_path, cache_path = _setup_run_env(tmp_path, monkeypatch)
    from scripts.lib import sources

    fake_app_py = f"CHANNELS = ['{CHANNEL_ID}']"

    def chan_fetcher(ids):
        return channels_response

    def pi_fetcher(playlist_id, max_results):
        raise requests.HTTPError("403")

    with patch.object(sources, "_fetch_with_retry", return_value=fake_app_py):
        rc = poll_youtube.run(
            ["--since-hours", "24"],
            channels_fetcher=chan_fetcher,
            playlist_items_fetcher=pi_fetcher,
            overrides_path=overrides_path,
            cache_path=cache_path,
            sleep_sec=0,
        )
    assert rc == 1


def test_run_channel_override_filters(
    tmp_path, monkeypatch, channels_response, playlist_items_response
):
    overrides_path, cache_path = _setup_run_env(tmp_path, monkeypatch)
    from scripts.lib import sources

    fake_app_py = (
        f"CHANNELS = ['{CHANNEL_ID}', 'UCfakeRingerNBA0000000002']"
    )

    seen_playlists: list[str] = []

    def chan_fetcher(ids):
        return channels_response

    def pi_fetcher(playlist_id, max_results):
        seen_playlists.append(playlist_id)
        return playlist_items_response

    with patch.object(sources, "_fetch_with_retry", return_value=fake_app_py):
        rc = poll_youtube.run(
            ["--dry-run", "--channel", CHANNEL_ID, "--since-hours", "999"],
            channels_fetcher=chan_fetcher,
            playlist_items_fetcher=pi_fetcher,
            overrides_path=overrides_path,
            cache_path=cache_path,
            sleep_sec=0,
        )
    assert rc == 0
    # Only one playlistItems.list call, for the requested channel.
    assert seen_playlists == [UPLOADS_ID]
