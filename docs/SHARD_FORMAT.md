# Shard format specification

This document defines the JSON structure for content items stored in NBA Content Stream's hot and cold storage tiers. The shard is the unit of work for every ingestion pipeline, the unit of read for the frontend, and the unit of input for the social video generator.

## File layout

Hot tier (last 30 days), committed to this repo:
```
data/{source}/{YYYY-MM-DD}.json
```

Cold tier (R2 bucket `nba-content-stream-archive`): same path inside the bucket.

Source values: `youtube`, `substack`, `bluesky`, `reddit`, `google-news`.

## Shard envelope

Each shard contains a single source's items for a single UTC day:

```json
{
  "date": "2026-05-21",
  "source": "youtube",
  "generated_at": "2026-05-21T15:30:00Z",
  "items": [ ... ]
}
```

| Field | Type | Notes |
|---|---|---|
| `date` | string | ISO date (UTC) the shard covers |
| `source` | enum | One of the five source values |
| `generated_at` | string | ISO 8601 UTC of last write to this shard |
| `items` | array | Ordered ascending by `published_at` |

## Item schema (full example)

Every item must include the required fields. Optional fields appear where applicable per source (see Source-specific variations).

```json
{
  "id": "yt-abc123XYZ",
  "source": "youtube",
  "published_at": "2026-05-21T14:30:00Z",
  "ingested_at": "2026-05-21T14:31:42Z",
  "url": "https://www.youtube.com/watch?v=abc123XYZ",
  "title": "Lakers panic mode is officially here",
  "author": {
    "handle": "@thezachlowshow",
    "display_name": "The Zach Lowe Show",
    "url": "https://www.youtube.com/@thezachlowshow"
  },
  "body_excerpt": "Optional. First ~500 chars of post body or video description.",
  "thumbnail": "https://img.youtube.com/vi/abc123XYZ/maxresdefault.jpg",
  "media": {
    "type": "video",
    "duration_seconds": 4523
  },
  "engagement": {
    "likes": 1240,
    "reposts": null,
    "comments": 187,
    "score": null,
    "views": 24500
  },
  "players": ["lebron-james", "anthony-davis"],
  "teams": ["los-angeles-lakers"],
  "extraction": {
    "summary": "Zach argues the Lakers' issues are structural, not personnel.",
    "best_quotes": [
      {"timestamp": 423, "text": "The defense isn't broken because of the lineup."},
      {"timestamp": 1247, "text": "This trade deadline isn't going to fix any of it."}
    ],
    "topics": ["lakers", "trade-rumors", "lebron-james"]
  }
}
```

## Required fields (all sources)

| Field | Type | Notes |
|---|---|---|
| `id` | string | Unique within source. Prefix with source code: `yt-`, `ss-`, `bs-`, `rd-`, `gn-`. |
| `source` | enum | One of: `youtube`, `substack`, `bluesky`, `reddit`, `google-news`. |
| `published_at` | ISO 8601 UTC | When the item was published at source. |
| `ingested_at` | ISO 8601 UTC | When the GH Action ingested it. |
| `url` | string | Canonical link to the original item. |
| `title` | string | Headline or video title. |
| `author` | object | At minimum `handle` and `display_name`. |
| `players` | array of strings | Player slugs from `data/canonical/players.json`. Empty array if none detected. |
| `teams` | array of strings | Team slugs from `data/canonical/teams.json`. Empty array if none detected. |

## Optional fields

| Field | Type | Notes |
|---|---|---|
| `body_excerpt` | string | First ~500 chars of body/description. |
| `thumbnail` | string | URL to thumbnail or preview image. |
| `media` | object | `type` and `duration_seconds`. |
| `engagement` | object | Source-specific metrics. |
| `extraction` | object | Present only when Gemini extraction ran. |
| `top_comments` | array | Reddit-only. Up to 3 highest-scored comments. |

## Source-specific variations

### YouTube
- `media.type` = `"video"`, `media.duration_seconds` required.
- `engagement.views`, `.likes`, `.comments` present (null if API doesn't return).
- `extraction` block **required** (full Gemini extraction from transcript).
- `thumbnail` = `https://img.youtube.com/vi/{id}/maxresdefault.jpg` (fall back to `hqdefault.jpg`).

### Substack
- `media.type` = `"text"`.
- `body_excerpt` present (first ~500 chars from RSS).
- `extraction` block **required** for free posts (Gemini extraction on full body).
- Paid posts: `title` and `body_excerpt` only, no `extraction`.
- `author.url` = publication URL (`{pub}.substack.com`).

### Bluesky
- `media.type` = `"text"` (or `"image"` if post has image attachment).
- `body_excerpt` = full post text (Bluesky posts are short).
- `engagement.likes`, `.reposts` present.
- No `extraction` block.
- Filter applied at ingest: top-level posts and quote-posts only. Drop replies.

### Reddit (r/nba)
- `media.type` = `"text"`.
- `body_excerpt` = post body (selftext) if any, else first top comment.
- `engagement.score` (can be negative), `.comments` (count) present.
- Optional `top_comments` array: up to 3 comments as `{author, score, text}`.
- No `extraction` block.

### Google News
- `media.type` = `"text"`.
- `title` only (RSS doesn't reliably give body).
- `author.handle` = publisher domain (e.g. `espn.com`).
- `body_excerpt` if RSS provides a snippet.
- No `engagement` block.
- No `extraction` block.

## Player and team tagging

- Slugs are lowercase, hyphen-separated.
- Player examples: `lebron-james`, `victor-wembanyama`, `shai-gilgeous-alexander`.
- Team examples: `los-angeles-lakers`, `oklahoma-city-thunder`, `philadelphia-76ers`.
- Detection: regex with word boundaries against `data/canonical/players.json` and `data/canonical/teams.json`.
- Aliases supported per canonical files (e.g., `"Wemby"` → `victor-wembanyama`).
- Disambiguation by team context when last names collide (`"Murray"` + `"Hawks"` → `dejounte-murray`; `"Murray"` + `"Nuggets"` → `jamal-murray`).

## ID generation

| Source | ID format |
|---|---|
| YouTube | `yt-{video_id}` |
| Substack | `ss-{publication_slug}-{post_slug}` |
| Bluesky | `bs-{at_uri_path}` (URL-encoded) |
| Reddit | `rd-{post_id}` |
| Google News | `gn-{base64_url_hash}` |

## Versioning

This is schema v1. Breaking changes will bump the version. The shard envelope will gain a `schema_version` field starting with v2.

## Validation

A Phase 2 script will validate every shard against this spec before commit. Items failing validation are dropped from the shard with a warning logged to the Action run.
