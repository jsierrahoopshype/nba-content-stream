# NBA Content Stream

HoopsMatic feature that aggregates NBA content from YouTube, Substack, Bluesky, Reddit, and Google News into a single browsable interface, with cross-cutting player and team pages and an automated data-viz social video pipeline.

Live at: [jsierrahoopshype.github.io/nba-content-stream](https://jsierrahoopshype.github.io/nba-content-stream/)

See [DESIGN.md](./DESIGN.md) for the full design doc.

## Architecture

| Layer | Technology |
|---|---|
| Frontend | Vanilla HTML/JS served by GitHub Pages from the repo root |
| Ingestion | GitHub Actions cron, every 15 min per source |
| Hot storage | JSON shards in `data/`, committed by Actions |
| Cold storage | Cloudflare R2 (10GB free tier) |
| API | Cloudflare Worker for archive queries (`worker/`) |
| AI extraction | Gemini 2.5 Flash for YouTube + Substack long-form |
| Video generation | Extends existing `bar-chart-race` repo (Pillow + ffmpeg) |

## Repo structure

```
.
├── index.html                  Frontend entry point (Phase 1 placeholder)
├── data/
│   ├── canonical/              Player and team master lists
│   ├── youtube/                Daily shards (created by Phase 2 ingestion)
│   ├── substack/
│   ├── bluesky/
│   ├── reddit/
│   ├── google-news/
│   └── index/                  Derived player/team/trending indexes
├── docs/
│   └── SHARD_FORMAT.md         JSON schema for content items
├── scripts/                    Ingestion code (Phase 2+)
├── worker/                     Cloudflare Worker for R2 reads
└── .github/workflows/          GitHub Actions cron polling
```

## Status

**Phase 1 (Plumbing)**: complete. Repo structure, workflow stubs, Worker skeleton, shard format spec, stub canonical lists.

Subsequent phases:

| Phase | Focus |
|---|---|
| 2 | Source ingestion (YouTube, Substack, Bluesky, Reddit, Google News) |
| 3 | Frontend (feed, filters, player and team pages, Trending Now) |
| 4 | Backfill from rumors archive + canonical player/team list dump |
| 5 | Social video pipeline (chatter heatmap, rumor velocity, weekly wave) |
| 6 | Polish, threshold tuning, R2 cold-tier rollup, launch |

## Deploy

Frontend deploys automatically via GitHub Pages on push to `main`.

Worker deploys via `wrangler deploy` from `worker/`. See [worker/README.md](./worker/README.md).

## Stub canonical data

`data/canonical/players.json` is a placeholder list of the most-mentioned active players, with common aliases. The full canonical list is extracted from the HoopsHype rumors archive (637K entries since 2010) in Phase 4.

`data/canonical/teams.json` covers all 30 NBA teams. This list is stable.
