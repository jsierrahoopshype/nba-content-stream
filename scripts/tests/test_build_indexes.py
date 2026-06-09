"""Tests for `scripts.build_indexes`.

We write a small set of fake shard files into a tmp directory
(monkeypatching `shards.DATA_DIR` the same way the poller tests do)
and exercise the full pipeline end-to-end. Assertions focus on the
data contract the frontend will rely on:
  - per-entity files contain only the compact shape (no engagement,
    no ingested_at, no source-specific fields)
  - newest-first ordering
  - window + cap respected
  - trending scoring: recency + source weight
  - manifest sorted by count desc, only slugs with content
  - idempotency: rebuild → byte-identical output
  - dedup: same id across two shards counts once
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import build_indexes
from scripts.lib import shards as shards_module


# A fixed "now" so window math is deterministic. All fixture items use
# timestamps relative to this.
FIXED_NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def _hours_before(h: float) -> str:
    return (FIXED_NOW - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_before(d: float) -> str:
    return (FIXED_NOW - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _date_of(iso: str) -> str:
    return iso[:10]


def _item(
    item_id: str,
    source: str,
    published_at: str,
    *,
    title: str = "Untitled",
    url: str = "https://example.com/x",
    author_handle: str = "x",
    author_display: str = "X",
    body_excerpt: str | None = None,
    thumbnail: str | None = None,
    players: list[str] | None = None,
    teams: list[str] | None = None,
) -> dict:
    item: dict = {
        "id": item_id,
        "source": source,
        "published_at": published_at,
        "ingested_at": published_at,
        "url": url,
        "title": title,
        "author": {"handle": author_handle, "display_name": author_display},
        "media": {"type": "text"},
        "players": players or [],
        "teams": teams or [],
    }
    if body_excerpt is not None:
        item["body_excerpt"] = body_excerpt
    if thumbnail is not None:
        item["thumbnail"] = thumbnail
    return item


def _write_shard(data_dir: Path, source: str, items: list[dict]) -> None:
    """Group by published_at date and write one shard per date.

    Mirrors what append_items would do — items in a real shard are
    sorted ascending by published_at within their day file.
    """
    by_date: dict[str, list[dict]] = {}
    for it in items:
        by_date.setdefault(_date_of(it["published_at"]), []).append(it)
    for date, batch in by_date.items():
        batch.sort(key=lambda x: x["published_at"])
        path = data_dir / source / f"{date}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "date": date,
                    "source": source,
                    "generated_at": "2026-05-25T12:00:00Z",
                    "items": batch,
                },
                indent=2,
            )
        )


@pytest.fixture
def isolated_data(tmp_path, monkeypatch):
    """Redirect shards.DATA_DIR to a tmp tree and yield the path."""
    monkeypatch.setattr(shards_module, "DATA_DIR", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Compact shape
# ---------------------------------------------------------------------------


def test_compact_item_keeps_only_render_fields():
    raw = _item(
        "bs-1",
        "bluesky",
        _hours_before(1),
        title="t",
        url="https://x/1",
        body_excerpt="excerpt",
        thumbnail="https://x/thumb.jpg",
        players=["lebron-james"],
        teams=["los-angeles-lakers"],
    )
    # Add a bunch of fields the frontend doesn't need; they must be stripped.
    raw["engagement"] = {"likes": 5}
    raw["google_url"] = "https://news.google.com/x"
    raw["matched_query"] = "LeBron"
    compact = build_indexes._compact_item(raw)
    assert set(compact.keys()) == {
        "id",
        "source",
        "published_at",
        "title",
        "url",
        "author",
        "thumbnail",
        "body_excerpt",
        "players",
        "teams",
    }
    # Engagement, ingested_at, media, google_url, matched_query — all gone.
    assert "engagement" not in compact
    assert "ingested_at" not in compact
    assert "media" not in compact
    assert "google_url" not in compact
    assert "matched_query" not in compact
    # Author flattened to a display name string.
    assert compact["author"] == "X"


# ---------------------------------------------------------------------------
# Polish-5 / Fix 2: Google News dedup (same title within 24h).
# ---------------------------------------------------------------------------


def test_gn_dedup_collapses_same_title_same_day():
    # All three on the same UTC calendar day (FIXED_NOW is 2026-05-25T12Z).
    items = [
        _item("gn-a", "google-news", _hours_before(10), title="Brunson carries Jay Wright-ism"),
        _item("gn-b", "google-news", _hours_before(5),  title="Brunson carries Jay Wright-ism"),
        _item("gn-c", "google-news", _hours_before(1),  title="Brunson carries Jay Wright-ism"),
    ]
    dropped = build_indexes._dedupe_google_news(items)
    assert dropped == 2
    assert len(items) == 1
    assert items[0]["id"] == "gn-c"  # newest survives


def test_gn_dedup_case_and_whitespace_insensitive():
    items = [
        _item("gn-a", "google-news", _hours_before(5), title="Brunson Carries  Jay Wright-ism"),
        _item("gn-b", "google-news", _hours_before(1), title="brunson carries jay wright-ism"),
    ]
    dropped = build_indexes._dedupe_google_news(items)
    assert dropped == 1
    assert items[0]["id"] == "gn-b"


def test_gn_dedup_does_not_collapse_across_days():
    items = [
        _item("gn-a", "google-news", _days_before(2), title="Brunson story"),
        _item("gn-b", "google-news", _days_before(0), title="Brunson story"),
    ]
    dropped = build_indexes._dedupe_google_news(items)
    assert dropped == 0
    assert len(items) == 2


def test_gn_dedup_does_not_touch_other_sources():
    items = [
        _item("bs-a", "bluesky", _hours_before(5), title="Same headline"),
        _item("bs-b", "bluesky", _hours_before(1), title="Same headline"),
        _item("gn-a", "google-news", _hours_before(3), title="Same headline"),
    ]
    dropped = build_indexes._dedupe_google_news(items)
    assert dropped == 0  # bluesky pair untouched, gn alone has no dup
    assert len(items) == 3


def test_compact_item_handles_missing_optional_fields():
    raw = _item("bs-2", "bluesky", _hours_before(1))
    compact = build_indexes._compact_item(raw)
    assert compact["thumbnail"] is None
    assert compact["body_excerpt"] is None
    assert compact["players"] == []
    assert compact["teams"] == []


# ---------------------------------------------------------------------------
# Shard reading + dedup
# ---------------------------------------------------------------------------


def test_load_all_items_sorts_descending_and_dedupes_by_id(isolated_data):
    _write_shard(
        isolated_data,
        "bluesky",
        [
            _item("bs-1", "bluesky", _hours_before(2)),
            _item("bs-2", "bluesky", _hours_before(1)),
        ],
    )
    # Same id in google-news shard — should dedupe (first occurrence wins).
    _write_shard(
        isolated_data,
        "google-news",
        [_item("bs-1", "google-news", _hours_before(0.5), title="DUP")],
    )
    items = build_indexes.load_all_items()
    ids = [it["id"] for it in items]
    assert ids == ["bs-2", "bs-1"]  # newest first
    assert len(items) == 2


def test_load_all_items_drops_missing_id_or_published(isolated_data):
    (isolated_data / "bluesky").mkdir(parents=True)
    (isolated_data / "bluesky" / "2026-05-25.json").write_text(
        json.dumps(
            {
                "items": [
                    {"id": None, "published_at": _hours_before(1)},
                    {"id": "bs-ok", "published_at": _hours_before(2)},
                    {"id": "bs-no-time"},  # missing published_at
                ]
            }
        )
    )
    items = build_indexes.load_all_items()
    assert [it["id"] for it in items] == ["bs-ok"]


def test_load_all_items_ignores_index_and_sources_dirs(isolated_data):
    """The builder must never recurse into its own output or config dirs."""
    (isolated_data / "index").mkdir()
    (isolated_data / "index" / "trending.json").write_text(
        json.dumps({"items": [_item("idx-X", "bluesky", _hours_before(1))]})
    )
    (isolated_data / "sources").mkdir()
    (isolated_data / "sources" / "bluesky_overrides.json").write_text("{}")
    _write_shard(
        isolated_data,
        "bluesky",
        [_item("bs-real", "bluesky", _hours_before(1))],
    )
    items = build_indexes.load_all_items()
    assert [it["id"] for it in items] == ["bs-real"]


# ---------------------------------------------------------------------------
# Per-entity indexes
# ---------------------------------------------------------------------------


def test_player_index_groups_by_player_newest_first(isolated_data):
    _write_shard(
        isolated_data,
        "bluesky",
        [
            _item(
                "bs-1",
                "bluesky",
                _hours_before(3),
                title="oldest",
                players=["stephen-curry"],
                teams=["golden-state-warriors"],
            ),
            _item(
                "bs-2",
                "bluesky",
                _hours_before(1),
                title="newest",
                players=["stephen-curry"],
                teams=["golden-state-warriors"],
            ),
        ],
    )
    _write_shard(
        isolated_data,
        "youtube",
        [
            _item(
                "yt-1",
                "youtube",
                _hours_before(2),
                title="middle",
                players=["stephen-curry"],
            )
        ],
    )
    players, teams, _, _, _ = build_indexes.build_indexes(now=FIXED_NOW, retag=False)
    curry = players["stephen-curry"]
    assert curry["count"] == 3
    assert [it["title"] for it in curry["items"]] == ["newest", "middle", "oldest"]
    # Items mention the original slug so the frontend can render OTHER tags.
    assert all("stephen-curry" in it["players"] for it in curry["items"])


def test_team_index_built_separately(isolated_data):
    _write_shard(
        isolated_data,
        "bluesky",
        [
            _item(
                "bs-1",
                "bluesky",
                _hours_before(1),
                title="t1",
                teams=["los-angeles-lakers"],
            )
        ],
    )
    _, teams, _, _, _ = build_indexes.build_indexes(now=FIXED_NOW, retag=False)
    assert "los-angeles-lakers" in teams
    assert teams["los-angeles-lakers"]["name"] == "Los Angeles Lakers"
    assert teams["los-angeles-lakers"]["count"] == 1


def test_unknown_slug_skipped(isolated_data, caplog):
    _write_shard(
        isolated_data,
        "bluesky",
        [
            _item(
                "bs-1",
                "bluesky",
                _hours_before(1),
                players=["not-a-real-player"],
            )
        ],
    )
    players, _, _, _, _ = build_indexes.build_indexes(now=FIXED_NOW, retag=False)
    assert "not-a-real-player" not in players


def test_window_filter_excludes_old_items(isolated_data):
    _write_shard(
        isolated_data,
        "bluesky",
        [
            _item(
                "bs-old",
                "bluesky",
                _days_before(60),
                title="old",
                players=["stephen-curry"],
            ),
            _item(
                "bs-new",
                "bluesky",
                _hours_before(1),
                title="new",
                players=["stephen-curry"],
            ),
        ],
    )
    players, _, _, _, _ = build_indexes.build_indexes(
        window_days=30, now=FIXED_NOW
    , retag=False)
    assert players["stephen-curry"]["count"] == 1
    assert players["stephen-curry"]["items"][0]["title"] == "new"


def test_per_entity_items_use_compact_shape(isolated_data):
    """Per-entity items contain only the documented compact fields."""
    _write_shard(
        isolated_data,
        "bluesky",
        [
            _item(
                "bs-1",
                "bluesky",
                _hours_before(1),
                players=["stephen-curry"],
            )
        ],
    )
    players, _, _, _, _ = build_indexes.build_indexes(now=FIXED_NOW, retag=False)
    item = players["stephen-curry"]["items"][0]
    assert "engagement" not in item
    assert "ingested_at" not in item
    assert "media" not in item


def test_max_items_per_entity_cap(isolated_data, monkeypatch):
    """Per-entity files cap at MAX_ITEMS_PER_ENTITY."""
    monkeypatch.setattr(build_indexes, "MAX_ITEMS_PER_ENTITY", 3)
    items = [
        _item(
            f"bs-{i}",
            "bluesky",
            _hours_before(i + 1),
            players=["stephen-curry"],
        )
        for i in range(10)
    ]
    _write_shard(isolated_data, "bluesky", items)
    players, _, _, _, _ = build_indexes.build_indexes(now=FIXED_NOW, retag=False)
    assert players["stephen-curry"]["count"] == 3
    # The 3 newest survive (oldest entries dropped).
    titles = [it["id"] for it in players["stephen-curry"]["items"]]
    assert titles == ["bs-0", "bs-1", "bs-2"]


# ---------------------------------------------------------------------------
# Trending
# ---------------------------------------------------------------------------


def test_trending_newer_outranks_older_same_source(isolated_data):
    _write_shard(
        isolated_data,
        "bluesky",
        [
            _item("bs-old", "bluesky", _hours_before(20), title="old"),
            _item("bs-new", "bluesky", _hours_before(1), title="new"),
        ],
    )
    _, _, trending, _, _ = build_indexes.build_indexes(now=FIXED_NOW, retag=False)
    ids = [it["id"] for it in trending["items"]]
    assert ids.index("bs-new") < ids.index("bs-old")


def test_trending_youtube_outranks_bluesky_same_age(isolated_data):
    """Source weight: youtube/substack 1.3 > bluesky 1.0 at equal recency."""
    _write_shard(
        isolated_data,
        "youtube",
        [_item("yt-1", "youtube", _hours_before(1), title="YT")],
    )
    _write_shard(
        isolated_data,
        "bluesky",
        [_item("bs-1", "bluesky", _hours_before(1), title="BS")],
    )
    _, _, trending, _, _ = build_indexes.build_indexes(now=FIXED_NOW, retag=False)
    ids = [it["id"] for it in trending["items"]]
    assert ids.index("yt-1") < ids.index("bs-1")
    # Both should also have trending_score set.
    assert all("trending_score" in it for it in trending["items"])


def test_trending_window_filters_out_old(isolated_data):
    """Items older than TRENDING_WINDOW_HOURS don't appear in trending."""
    _write_shard(
        isolated_data,
        "bluesky",
        [
            _item("bs-fresh", "bluesky", _hours_before(1), title="fresh"),
            _item("bs-stale", "bluesky", _hours_before(200), title="stale"),
        ],
    )
    _, _, trending, _, _ = build_indexes.build_indexes(now=FIXED_NOW, retag=False)
    ids = [it["id"] for it in trending["items"]]
    assert "bs-fresh" in ids
    assert "bs-stale" not in ids


def test_trending_limit_respected(isolated_data, monkeypatch):
    monkeypatch.setattr(build_indexes, "TRENDING_LIMIT", 3)
    _write_shard(
        isolated_data,
        "bluesky",
        [
            _item(f"bs-{i}", "bluesky", _hours_before(i + 1), title=f"t{i}")
            for i in range(10)
        ],
    )
    _, _, trending, _, _ = build_indexes.build_indexes(now=FIXED_NOW, retag=False)
    assert trending["count"] == 3


# ---------------------------------------------------------------------------
# Feed
# ---------------------------------------------------------------------------


def test_feed_includes_source_and_is_newest_first(isolated_data):
    _write_shard(
        isolated_data,
        "bluesky",
        [_item("bs-1", "bluesky", _hours_before(2))],
    )
    _write_shard(
        isolated_data,
        "google-news",
        [_item("gn-1", "google-news", _hours_before(1))],
    )
    _, _, _, feed, _ = build_indexes.build_indexes(now=FIXED_NOW, retag=False)
    ids = [it["id"] for it in feed["items"]]
    assert ids == ["gn-1", "bs-1"]
    sources_seen = {it["source"] for it in feed["items"]}
    assert sources_seen == {"bluesky", "google-news"}


def test_feed_window_drops_old(isolated_data):
    _write_shard(
        isolated_data,
        "bluesky",
        [
            _item("bs-old", "bluesky", _days_before(20), title="old"),
            _item("bs-new", "bluesky", _hours_before(1), title="new"),
        ],
    )
    _, _, _, feed, _ = build_indexes.build_indexes(now=FIXED_NOW, retag=False)
    # FEED_WINDOW_DAYS defaults to 7; the 20-day-old item must be dropped.
    ids = [it["id"] for it in feed["items"]]
    assert ids == ["bs-new"]


def test_feed_cap_respected(isolated_data, monkeypatch):
    monkeypatch.setattr(build_indexes, "MAX_FEED_ITEMS", 5)
    _write_shard(
        isolated_data,
        "bluesky",
        [
            _item(f"bs-{i}", "bluesky", _hours_before(i + 1))
            for i in range(20)
        ],
    )
    _, _, _, feed, _ = build_indexes.build_indexes(now=FIXED_NOW, retag=False)
    assert feed["count"] == 5


# ---------------------------------------------------------------------------
# Perf-1: feed-recent.json (small newest-first slice for instant paint)
# ---------------------------------------------------------------------------


def _fake_feed(n):
    return {
        "generated_at": "2026-06-08T00:00:00Z",
        "window_days": 7,
        "count": n,
        "items": [
            {"id": f"x{i}", "source": "bluesky", "published_at": "2026-06-08T00:00:00Z"}
            for i in range(n)
        ],
    }


def test_build_feed_recent_slices_head_and_preserves_schema():
    feed = _fake_feed(250)
    recent = build_indexes.build_feed_recent(feed, cap=100)
    assert recent["count"] == 100
    assert len(recent["items"]) == 100
    assert recent["items"] == feed["items"][:100]   # newest-first head
    assert recent["recent_slice"] is True
    assert recent["generated_at"] == feed["generated_at"]
    assert recent["window_days"] == feed["window_days"]
    # source feed is untouched (purely additive)
    assert feed["count"] == 250 and len(feed["items"]) == 250
    assert "recent_slice" not in feed


def test_build_feed_recent_handles_short_feed():
    feed = _fake_feed(3)
    recent = build_indexes.build_feed_recent(feed, cap=100)
    assert recent["count"] == 3 and len(recent["items"]) == 3


def test_write_all_indexes_emits_feed_recent(tmp_path):
    feed = _fake_feed(150)
    build_indexes.write_all_indexes(
        {}, {}, {"items": []}, feed, {"players": [], "teams": []}, index_root=tmp_path
    )
    rec = tmp_path / "feed-recent.json"
    full = tmp_path / "feed.json"
    assert rec.exists() and full.exists()
    rec_data = json.loads(rec.read_text())
    full_data = json.loads(full.read_text())
    assert rec_data.get("recent_slice") is True
    assert rec_data["count"] == build_indexes.RECENT_FEED_ITEMS
    # full feed unchanged (no recent_slice marker, full item count)
    assert full_data["count"] == 150 and "recent_slice" not in full_data


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_manifest_sorts_players_and_teams_by_count_desc(isolated_data):
    # Curry has 3, LeBron has 1.
    _write_shard(
        isolated_data,
        "bluesky",
        [
            _item("bs-c1", "bluesky", _hours_before(1), players=["stephen-curry"]),
            _item("bs-c2", "bluesky", _hours_before(2), players=["stephen-curry"]),
            _item("bs-c3", "bluesky", _hours_before(3), players=["stephen-curry"]),
            _item("bs-l1", "bluesky", _hours_before(4), players=["lebron-james"]),
        ],
    )
    _, _, _, _, manifest = build_indexes.build_indexes(now=FIXED_NOW, retag=False)
    assert manifest["players"][0]["slug"] == "stephen-curry"
    assert manifest["players"][0]["count"] == 3
    assert manifest["players"][1]["slug"] == "lebron-james"


def test_manifest_only_lists_slugs_with_content(isolated_data):
    _write_shard(
        isolated_data,
        "bluesky",
        [_item("bs-1", "bluesky", _hours_before(1), players=["stephen-curry"])],
    )
    _, _, _, _, manifest = build_indexes.build_indexes(now=FIXED_NOW, retag=False)
    slugs = {p["slug"] for p in manifest["players"]}
    assert slugs == {"stephen-curry"}  # no entries for canonical players with 0 items


def test_manifest_source_histogram(isolated_data):
    _write_shard(
        isolated_data,
        "bluesky",
        [
            _item("bs-1", "bluesky", _hours_before(1)),
            _item("bs-2", "bluesky", _hours_before(2)),
        ],
    )
    _write_shard(
        isolated_data,
        "youtube",
        [_item("yt-1", "youtube", _hours_before(1))],
    )
    _, _, _, _, manifest = build_indexes.build_indexes(now=FIXED_NOW, retag=False)
    assert manifest["sources"] == {"bluesky": 2, "youtube": 1}
    assert manifest["total_items"] == 3


def test_manifest_exposes_per_entity_cap(isolated_data):
    """Polish-10 (Fix 2): the manifest publishes max_items_per_entity
    so the leaderboards + entity pages can render "1000+" when a
    bucket has saturated, instead of silently displaying the cap as
    if it were the real count."""
    _write_shard(
        isolated_data,
        "bluesky",
        [_item("bs-1", "bluesky", _hours_before(1), players=["stephen-curry"])],
    )
    _, _, _, _, manifest = build_indexes.build_indexes(now=FIXED_NOW, retag=False)
    assert manifest["max_items_per_entity"] == build_indexes.MAX_ITEMS_PER_ENTITY


# ---------------------------------------------------------------------------
# Idempotency + write/dry-run
# ---------------------------------------------------------------------------


def test_build_indexes_is_idempotent(isolated_data):
    """Running build_indexes twice produces byte-identical output."""
    _write_shard(
        isolated_data,
        "bluesky",
        [
            _item(
                "bs-1",
                "bluesky",
                _hours_before(1),
                players=["stephen-curry"],
                teams=["golden-state-warriors"],
            )
        ],
    )
    a = build_indexes.build_indexes(now=FIXED_NOW, retag=False)
    b = build_indexes.build_indexes(now=FIXED_NOW, retag=False)
    # Tuple of (players, teams, trending, feed, manifest).
    assert a == b


def test_write_all_indexes_creates_files(isolated_data):
    _write_shard(
        isolated_data,
        "bluesky",
        [
            _item(
                "bs-1",
                "bluesky",
                _hours_before(1),
                players=["stephen-curry"],
                teams=["golden-state-warriors"],
            )
        ],
    )
    players, teams, trending, feed, manifest = build_indexes.build_indexes(
        now=FIXED_NOW
    , retag=False)
    build_indexes.write_all_indexes(players, teams, trending, feed, manifest)
    index_root = isolated_data / "index"
    assert (index_root / "players" / "stephen-curry.json").exists()
    assert (index_root / "teams" / "golden-state-warriors.json").exists()
    assert (index_root / "trending.json").exists()
    assert (index_root / "feed.json").exists()
    assert (index_root / "manifest.json").exists()


def test_write_all_indexes_clears_stale_entity_files(isolated_data):
    """Stale players/{slug}.json from a previous build must not linger."""
    # Seed a stale file.
    stale_dir = isolated_data / "index" / "players"
    stale_dir.mkdir(parents=True)
    (stale_dir / "old-player.json").write_text("{}")

    _write_shard(
        isolated_data,
        "bluesky",
        [_item("bs-1", "bluesky", _hours_before(1), players=["stephen-curry"])],
    )
    players, teams, trending, feed, manifest = build_indexes.build_indexes(
        now=FIXED_NOW
    , retag=False)
    build_indexes.write_all_indexes(players, teams, trending, feed, manifest)
    assert not (stale_dir / "old-player.json").exists()
    assert (stale_dir / "stephen-curry.json").exists()


def test_run_dry_run_writes_nothing(isolated_data, capsys):
    _write_shard(
        isolated_data,
        "bluesky",
        [_item("bs-1", "bluesky", _hours_before(1), players=["stephen-curry"])],
    )
    rc = build_indexes.run(["--dry-run"])
    assert rc == 0
    captured = capsys.readouterr().out
    assert "DRY RUN" in captured
    assert not (isolated_data / "index" / "players").exists()


def test_run_no_shards_returns_1(isolated_data, caplog):
    with caplog.at_level("ERROR"):
        rc = build_indexes.run(["--dry-run"])
    assert rc == 1
    assert any("no shard items" in r.message for r in caplog.records)
