# NBA Content Stream — HoopsMatic Feature Design Doc

**Status:** Draft v1, awaiting review
**Owner:** Jorge Sierra
**Scope:** HoopsMatic feature aggregating NBA content from YouTube, Substack, Bluesky, Reddit, and Google News, with cross-cutting player/team pages and an automated data-viz social video pipeline.

---

## 1. Purpose

Aggregate the latest NBA content from five free, legally accessible sources into a single browsable HoopsMatic feature. Surface cross-cutting views by player and team. Use the same data backbone to generate short-form data-viz video for HoopsHype social channels.

## 2. Scope

### In scope (v1)

- Tab per source: YouTube, Substack, Bluesky, Reddit, Google News
- Cross-cutting player pages and team pages
- Search by player or team name
- Date range filter and source filter
- "Trending Now" surface
- Maximum backfill from existing archives
- Three social video formats (heatmap, velocity, weekly wave)
- Semi-auto and manual video generation, no autopost

### Out of scope (v1)

- Notifications, autoposts, Slack alerts
- Reporter Rankings tool integration
- Reporter mention war video format
- Quote compilation and player chatter compilation video formats
- Bookmark/save functionality
- Twitter/X ingestion (free API is dead)
- The Athletic, ESPN+, paywalled body content

## 3. Architecture

### Stack

| Layer | Technology |
|---|---|
| Frontend | Vanilla HTML/JS, no framework. Recharts or D3 for any data viz inside the feature. |
| Hosting | GitHub Pages, lives under HoopsMatic at `jsierrahoopshype.github.io/[feature-slug]/` |
| Backend API | Cloudflare Worker, Wrangler-deployed, ES modules |
| Hot storage | JSON shards in the new repo, committed by GH Actions |
| Cold storage | Cloudflare R2 (free tier 10GB) |
| Polling | GitHub Actions, every 15 minutes |
| AI extraction | Gemini 2.5 Flash via existing infra |
| Video generation | Extends existing `bar-chart-race` repo (Pillow/PIL + ffmpeg) |

### Repos involved

- **New repo:** `nba-content-stream` (placeholder name, final TBD). Holds the frontend, the ingestion GH Actions, and the hot-tier JSON shards.
- **Extend:** `bar-chart-race` for the new video formats.
- **Reuse data from:** `nba-podcast-stream` (YouTube channel list), `nba-buzz` HF Space (Bluesky reporter list), `hoopshype-rumors` (player/team canonical), `nba-headshots`.
- **Open question:** existing `reddit-intel` repo. Either fold the Reddit ingestion into the new repo for cohesion, or leave `reddit-intel` as the ingestion source and have it deliver to the new repo. Flag for implementation phase. **DECISION: fold into new repo for cohesion.**

### Why this stack

- Free or negligible cost at projected volume (see Section 11).
- All ingestion is GH Actions on public repo: unlimited free minutes.
- R2 free tier easily holds 10+ years of projected data.
- Vanilla frontend matches existing HoopsMatic tools and avoids framework debt.

## 4. Data sources

### 4.1 YouTube

- **Channel list:** existing `nba-podcast-stream` repo (~30 main channels plus conditional channels like Nate Duncan triggered by keyword match). Live-fetched with `data/sources/youtube_overrides.json` for local add/remove.
- **API:** YouTube Data API v3, free tier 10K units/day.
- **Poll cadence:** every 15 min.
- **Cost:** ~3K units/day for 30 channels via `playlistItems.list` plus batched `videos.list`. Comfortable inside free tier.
- **Transcript:** `youtube-transcript-api` (unofficial but stable, no API cost).
- **Processing:** full Gemini Flash extraction. Existing YouTube extractor pipeline output drops directly into the new stream.
- **Gating:** auto-pass (the curated channel list is the gate).

### 4.2 Substack

- **Source list:** to be finalized by Jorge. Preliminary draft list in Section 12.
- **API:** Public RSS feed per publication at `{pub}.substack.com/feed`. No rate limit issues at 15-min polling.
- **Processing:** full Gemini Flash extraction on free posts (title, headline, summary, key insights). Paid posts: title and headline only (RSS gives a preview, do not extract body).
- **Gating:** auto-pass (the publication list is the gate).
- **Copyright note:** never reproduce full Substack post bodies. Store extracted insights and link out for full read.

### 4.3 Bluesky

- **Reporter list:** existing `nba-buzz` HF Space list. Live-fetched with `data/sources/bluesky_overrides.json` for local add/remove.
- **API:** AT Protocol public read endpoints, no auth required.
- **Poll cadence:** every 15 min.
- **Processing:** raw feed (title equivalent = post text, source link, author handle, repost/like counts).
- **Gating:** auto-pass (the reporter list is the gate). Filter out replies, keep top-level posts and quote posts.

### 4.4 Reddit

- **Scope:** r/nba only.
- **API:** Public RSS feeds — `https://www.reddit.com/r/nba/top/.rss?t=day` and `https://www.reddit.com/r/nba/hot/.rss`. No OAuth, no auth, no API keys.
- **Rationale:** Reddit's API terms have ambiguous "share" language; RSS is purpose-built for syndication and is the cleaner legal path. Tradeoff: thinner data (no live score numbers in feed, no comment access).
- **Poll cadence:** every 15 min.
- **Processing:** raw feed (post title, short body excerpt, link).
- **Gating:** the `top/.rss?t=day` feed IS the quality gate. Reddit's algorithm pre-filters; nothing to score manually.
- **Design constraint:** never store/display post bodies or comment bodies beyond a short excerpt (~280 chars). Every Reddit item displayed must link back to the original thread.

### 4.5 Google News

- **Source:** Google News RSS, query-based, no API key needed.
- **Query strategy:** per-player and per-team queries, plus topic queries ("NBA trade rumors", "NBA injuries").
- **Poll cadence:** every 15 min for global queries, less frequent for per-player.
- **Processing:** raw feed (headline + snippet + source domain + link).
- **Gating:** publisher whitelist. See Section 12 for proposed whitelist.

### 4.6 Source matrix summary

| Source | Cadence | AI processing | Gating |
|---|---|---|---|
| YouTube | 15 min | Full Gemini extraction | Channel list |
| Substack | 15 min | Full Gemini extraction (free posts) | Publication list |
| Bluesky | 15 min | Raw feed | Reporter list |
| Reddit r/nba | 15 min | Raw feed | RSS top-of-day pre-filter |
| Google News | 15 min | Raw feed | Publisher whitelist |

## 5. Processing pipeline

### 5.1 Ingestion flow

```
Source (poll every 15 min)
  -> GH Actions runner
  -> Source-specific parser (one Python module per source)
  -> Quality gate (per source rules)
  -> Player/team tagger (rumors archive vocabulary)
  -> AI extraction (Gemini Flash, only for YouTube + Substack)
  -> Shard writer (append to today's JSON shard)
  -> Git commit + push to repo (triggers Pages rebuild)
  -> Daily rollup at 23:55 UTC: shard moves from hot tier to R2 cold tier
```

### 5.2 Player and team tagging

- Canonical source: rumors archive (R2). One-off dump script extracts unique player and team tags with aliases ("KAT", "PG", "Wemby", "Cade", "SGA").
- Output: `players.json` and `teams.json` files committed to the new repo.
- Refresh: monthly via GH Actions cron, in case new players or aliases appear in the archive.
- Detection: regex with word boundaries against canonical list. Disambiguation by team context where last names collide. If still ambiguous, skip rather than guess.
- Bonus (post-v1): AI fallback. If regex misses or is ambiguous, send the snippet to Gemini Flash for resolution. Adds ~$0.0001 per ambiguous item.

### 5.3 Shard format

See `docs/SHARD_FORMAT.md` for the full schema. One JSON file per day per source.

## 6. Storage model

### 6.1 Hot tier (last 30 days)

- Lives in the new repo at `data/{source}/{YYYY-MM-DD}.json`.
- Committed by GH Actions every 15 min.
- Served directly via GitHub Pages, no Worker call needed for live browsing.
- Frontend fetches today's shard + recent shards as needed.

### 6.2 Cold tier (full archive)

- R2 bucket `nba-content-stream-archive`.
- Same JSON shard format as hot tier.
- Daily rollup at 23:55 UTC moves shards from repo to R2, then prunes the repo to last 30 days.
- Queried via Cloudflare Worker for player pages, team pages, and date range filter.

### 6.3 Indexes

Derived files, regenerated on every commit:

- `index/players/{slug}.json`: ordered list of item IDs mentioning this player, with date and source.
- `index/teams/{slug}.json`: same for teams.
- `index/trending.json`: items with highest engagement/recency score across all sources in the last 24h.

### 6.4 Estimated volume

| Source | Items/day | Items/year | Size/year (~3KB/item) |
|---|---|---|---|
| YouTube | 90-150 | ~50K | 150 MB |
| Substack | 5-15 | ~3.6K | 10 MB |
| Bluesky | 300-500 | ~150K | 450 MB |
| Reddit | 30-50 (after gating) | ~15K | 45 MB |
| Google News | 50-100 (after gating) | ~30K | 90 MB |
| **Total** | **475-815** | **~250K** | **~750 MB/year** |

R2 free tier holds 10+ years of this volume.

## 7. Frontend (HoopsMatic feature)

### 7.1 URL structure

- Feature root: `jsierrahoopshype.github.io/nba-content-stream/`
- Source tabs: filter pills, not separate routes
- Player pages: `/players/lebron-james`
- Team pages: `/teams/lakers`
- Feature-scoped for v1. Cross-tool player profiles can be a later refactor.

### 7.2 Default view

Chronological mixed feed, most recent items at top, across all sources. Discovery elements (Trending Now, search, filters) live in the same view.

### 7.3 Layout (mobile-first)

```
[Header: feature name + brief description]
[Trending Now strip: 3-5 hot items, horizontal scroll on mobile]
[Filter bar: source pills | date range | search box]
[Feed: chronological mixed items, infinite scroll]
[Footer: link back to HoopsMatic main, data source credits]
```

### 7.4 Item card

Per item:
- Source badge (color-coded)
- Author/channel
- Title (linked to source URL, external)
- Player/team tags (clickable, route to /players/[slug] or /teams/[slug])
- Timestamp (relative: "2h ago")
- For AI-extracted items: 1-2 best-quote pull-outs as a collapsible section

### 7.5 Player and team pages

Layout per page:
- Header: player/team name, headshot (from `nba-headshots`), summary
- Chatter volume chart: last 30 days, stacked by source
- Recent mentions: chronological feed filtered to this player/team
- Top reporters covering this player (derived from item authors)
- Link to the canonical HoopsMatic player profile if one exists

### 7.6 Search

- Client-side fuzzy search on the last 30 days hot tier (instant).
- Deeper search via Worker that scans R2 (paginated, slower).
- Autocomplete on player/team names using `players.json` and `teams.json`.

### 7.7 Trending Now

Scoring formula (initial, tunable):
```
score = (engagement * source_weight) / (hours_since_publish + 2) ^ 1.5
```

Where `source_weight` favors YouTube and Substack over raw Bluesky/Reddit since long-form has more signal per item. Top N items by score, refreshed every 15 min with the polling cycle.

## 8. Backfill

Maximum from existing archives at launch.

| Source | Backfill depth | Mechanism |
|---|---|---|
| YouTube | All extractor history | Import from existing YouTube quote extractor archive |
| Substack | ~20-30 recent posts per pub | RSS feed reads, no historical access |
| Bluesky | ~500-1000 posts per reporter | AT Proto per-feed pagination |
| Reddit | None (launch day forward) | RSS gives current only |
| Google News | None (launch day forward) | RSS gives current only |
| Rumors archive cross-reference | All player/team mentions | One-off dump script reads R2 |

**Important note on archive privacy:** the rumors archive backfill happens via a script Jorge runs locally or via GH Actions. The script reads R2 and writes player/team mention indexes directly to the new repo. Rumor contents and reporter quotes never flow through Claude chat.

## 9. Social video pipeline

### 9.1 Three concepts

| Concept | Length | Audio | Trigger | Output |
|---|---|---|---|---|
| Player chatter heatmap | 45-60s | TTS over music bed | Spike detection (Z-score on player mention volume > threshold) | Single chart, one player, source-stacked bars over 4 weeks |
| Rumor velocity tracker | 60-90s | TTS-heavy narrative | Cross-source threshold (story appears in N≥3 sources within Z days) | Forensic timeline, beat per day, headshots and source citations |
| Story of the week wave | 45-60s | TTS opening + on-screen captions | Scheduled weekly (Friday) | Stacked area chart, top story across all platforms |

### 9.2 Generation workflow

- **Semi-auto path:** GH Actions runs candidate detection daily. Candidates land in a Slack channel (or in-repo queue file) with preview thumbnails. Jorge approves; system generates and queues for posting.
- **Manual path:** Jorge picks topic via a simple UI inside the feature. System generates immediately and returns video file for download.
- **Posting:** out of v1 scope. Videos saved to a queue folder; Jorge posts manually for now.

### 9.3 Format spec

- Resolution: 1080x1920 (9:16 vertical).
- Frame rate: 30 fps.
- Audio: AAC, 128kbps. TTS via ElevenLabs (already used) or browser TTS for cheaper option.
- Music bed: royalty-free loops, library shared with bar chart race generator.
- Output: MP4, H.264.
- Brand: pipeline is brand-agnostic for now. Templates accept a `brand` config (logo, color palette, font).

### 9.4 Extension of bar-chart-race

The existing `bar-chart-race` repo provides Pillow/PIL frame compositing, ffmpeg encoding, 50 themes, headshot integration. New work: heatmap renderer, velocity timeline renderer, wave/area chart renderer, TTS audio mixer. Extends additively; no changes to existing bar chart race functionality.

## 10. Implementation phases

### Phase 1: Plumbing — DONE
Repo, workflows, Worker skeleton, shard format spec, stub canonical lists.

### Phase 2: Source ingestion — IN PROGRESS
2a: shared library (canonical loading, shard read/write, tagging)
2b: Bluesky + Google News + Reddit RSS pollers
2c: YouTube + Substack pollers (need AI extraction wiring)

### Phase 3: Frontend
Static HTML/JS shell, mixed feed, player/team pages, search, Trending Now.

### Phase 4: Backfill
YouTube extractor archive import, Substack RSS sweep, Bluesky reporter feed sweep, rumors archive cross-reference index build.

### Phase 5: Video pipeline
Spike detection + candidate queueing, three renderers, TTS integration, manual-trigger UI.

### Phase 6: Polish + launch
Quality threshold tuning, performance tuning, R2 cold-tier rollup, cross-tool consistency check, soft launch then public launch.

## 11. Cost estimate

| Item | Cost |
|---|---|
| GitHub Actions (public repo) | $0 (unlimited minutes) |
| Cloudflare Worker (free tier 100K req/day) | $0 at projected volume |
| Cloudflare R2 (free tier 10GB) | $0 for ~10 years of data |
| YouTube Data API (10K units/day free) | $0 at ~3K daily usage |
| Reddit RSS | $0 |
| Bluesky AT Proto | $0 |
| RSS feeds (Substack, Google News) | $0 |
| Gemini 2.5 Flash extraction | < $1/day at full volume |
| TTS (ElevenLabs or alternative) | $5-15/month at 2-3 videos/day |
| **Total** | **< $20/month at full scale** |

## 12. Open items

### 12.1 Feature name — DECIDED
NBA Content Stream. Repo slug: `nba-content-stream`.

### 12.2 Substack list — TBD
Jorge to provide. Preliminary draft below, confidence low:
- Marc Stein + Jake Fischer (The Stein Line)
- Tom Haberstroh (TomTheFinder)
- Howard Beck (if Substack)
- Marc Spears (if Substack)
- Bobby Marks (if/when leaves ESPN)
- Mike Vorkunov (if Substack)
- Sam Vecenie (if independent)
- Yossi Gozlan (cap analysis)
- Eric Pincus (cap analysis)

### 12.3 Google News publisher whitelist (proposed)

**Tier 1 (always include):** ESPN, The Athletic, Yahoo Sports, HoopsHype, Bleacher Report, Sports Illustrated, NBA.com, Forbes.

**Tier 2 (include selectively):** The Ringer, USA Today, AP, Reuters, top team beats (Cleveland.com Cavs, Boston Globe Celtics, Chicago Tribune Bulls, NJ.com Nets, etc.)

**Reject:** SEO content farms, AI-generated sports sites, ad-heavy aggregators.

### 12.4 Quality thresholds (Reddit) — DECIDED
RSS `top/.rss?t=day` is the gate. No manual scoring needed.

### 12.5 Reddit ingestion location — DECIDED
Folded into new repo for cohesion.

### 12.6 Video branding — DEFERRED
Pipeline brand-agnostic for v1. Templates accept `brand` config.

### 12.7 Gannett editorial approval — DECIDED
Free hand on HoopsHype socials, no per-post approval needed.

### 12.8 Reporter Rankings integration — DECIDED
No integration in v1. Ship independently. Revisit in v2.

### 12.9 Player/team page scope — DECIDED
Feature-scoped for v1. HoopsMatic-wide profiles can be a later refactor.

---

## Appendix A: Source list inventory

- YouTube channel list: `nba-podcast-stream` repo
- Bluesky reporter list: `nba-buzz` HF Space
- Substack publication list: TBD, Jorge to provide
- Reddit subs: r/nba only (RSS)
- Google News publisher whitelist: Section 12.3

## Appendix B: Existing tools this feature touches

- `nba-podcast-stream`: source of YouTube channel list
- `nba-buzz` (HF Space): source of Bluesky reporter list
- `hoopshype-rumors`: source of player/team canonical vocabulary
- `nba-headshots`: source of player headshots for cards and videos
- `bar-chart-race`: extended for video generation
- `reddit-intel`: folded into new repo (no longer separate)

## Appendix C: Things explicitly deferred

- Autoposting to social channels (v2)
- Reporter Rankings integration (v2)
- Reporter mention war video format (v2)
- Quote compilation videos (deprioritized)
- Player chatter compilation videos (deprioritized)
- Bookmark/save functionality (v2 if needed)
- Cross-tool HoopsMatic player profiles (separate refactor)

---

*End of design doc v1.*
