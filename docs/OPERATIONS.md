# Operations

Quick reference for running and monitoring the ingestion workflows
after Phase 2c. For the architecture see `DESIGN.md`; for the data
contract see `SHARD_FORMAT.md`.

## Workflows

| Workflow file | Cadence | What it polls |
|---|---|---|
| `.github/workflows/poll-sources.yml` | every 15 min (`*/15 * * * *`) | Bluesky reporters + Google News RSS + Substack publications |
| `.github/workflows/poll-reddit.yml`  | hourly, 5 min past (`5 * * * *`) | r/nba top/.rss?t=day + hot/.rss |
| `.github/workflows/poll-youtube.yml` | hourly, 10 min past (`10 * * * *`) | YouTube uploads from the nba-podcast-stream channel list |
| `.github/workflows/daily-rollup.yml` | 23:55 UTC daily | Hot → cold tier rollup (Phase 6) |

Both poll workflows share the `shard-commit` concurrency group, so
they never push to `data/` simultaneously. Each appends to the per-day
shard file and commits as `github-actions[bot]`. `poll-sources.yml`
also rebuilds `data/index/` and regenerates the per-entity HTML pages
under `players/` and `teams/` after each poll cycle.

## Frontend (Phase 3)

Static site under the repo root, served from GitHub Pages:

| File / dir | Role |
|---|---|
| `index.html` | Unified feed (reads `data/index/feed.json`, lives merges Bluesky / Reddit / Google News). |
| `players.html`, `teams.html` | Directory grids from `manifest.json`. |
| `players/{slug}.html`, `teams/{slug}.html` | Pre-rendered per-entity pages with SEO baked in. Regenerated each cycle by `scripts/prerender_pages.py`. |
| `sitemap.xml` | All pages, regenerated each cycle. |
| `assets/styles.css`, `assets/*.js` | Vanilla HTML/CSS/JS, no framework. |
| `worker-cors/` | Cloudflare Worker that proxies Reddit / Google News / Substack RSS so the browser can live-merge them. Allowlist-only. Deploy with `wrangler deploy` from `worker-cors/`. |

After deploying the Worker, paste the deployed URL into
`assets/config.js` (`CORS_PROXY_URL`). Until then the Reddit / Google
News / Substack live-merge silently fails — the archived items from
the index files still render fine.

**Secrets**: only one — `YOUTUBE_API_KEY`, used by `poll-youtube.yml`.
Bluesky / Google News / Reddit / Substack are all keyless public reads.
If any other workflow asks for a secret, that's a bug — file an issue.

The YouTube key is stored as a repo secret (`Settings → Secrets and
variables → Actions → YOUTUBE_API_KEY`). To rotate: paste the new
value into the same secret; no code change needed. To verify the key
is working, run the YouTube workflow via `Run workflow` and check the
`stats:` line — `channel_errors == 0` and `quota_units_estimate > 0`
means the key is live.

## Going live

1. Open the **Actions** tab on GitHub.
2. Find `Poll sources` in the sidebar. If the banner says "This
   scheduled workflow is disabled", click **Enable workflow**.
3. Repeat for `Poll Reddit` and `Poll YouTube`.
4. Confirm `YOUTUBE_API_KEY` is set in `Settings → Secrets and variables → Actions` (required by `Poll YouTube` only).
5. Watch the next two cycles run end-to-end (≤ 30 min wait).

## Manually triggering a run

Each workflow has `workflow_dispatch` enabled.

- **Actions tab → Poll sources → Run workflow** runs all sources at
  once, or pick `bluesky` / `google-news` from the dropdown for a
  single source.
- **Actions tab → Poll Reddit → Run workflow** runs Reddit
  immediately (no source picker; it only has one source).

Manual runs respect the same concurrency group as cron runs, so a
manual trigger during a cron run just queues.

## Reading the logs

Each poller emits an INFO `stats:` line at the end of its run.

**Bluesky** (`scripts.poll_bluesky`):
```
stats: {'reporters': 375, 'posts_seen': 412, 'dropped_filter': 86,
        'dropped_since': 240, 'kept': 86, 'fetch_errors': 0}
```
- `fetch_errors` should be a small fraction of `reporters` (renamed
  handles, suspended accounts). Anywhere near 100% means we're hitting
  `bsky.social` instead of `public.api.bsky.app` — check the URL in
  the warnings.

**Google News** (`scripts.poll_google_news`):
```
stats: {'queries': 40, 'entries_seen': 1500, 'dropped_whitelist': 900,
        'dropped_since': 400, 'deduped': 80, 'kept': 120,
        'query_errors': 0}
```
- `dropped_whitelist` being large is expected and good — that's the
  publisher whitelist working.
- `query_errors` should be 0 or 1 (transient Google blip). Persistently
  high counts suggest Google is rate-limiting; consider raising
  `INTER_QUERY_SLEEP_SEC` in the poller.

**Substack** (`scripts.poll_substack`):
```
stats: {'publications': 3, 'entries_seen': 8, 'dropped_since': 2,
        'deduped': 0, 'kept': 6, 'feed_errors': 0}
```
- `feed_errors` should be 0 unless a publication moved or shut down.
  If a specific feed errors repeatedly, prune it from
  `data/sources/substack_publications.json`. Substack is raw RSS with
  no AI processing — there's no "extraction" line to watch for here.

**YouTube** (`scripts.poll_youtube`):
```
stats: {'channels': 57, 'videos_seen': 120, 'dropped_since': 60,
        'deduped': 0, 'kept': 60, 'channel_errors': 0,
        'quota_units_estimate': 58}
```
- `quota_units_estimate` is the rough YouTube Data API quota cost of
  the cycle. Steady state is `channels + uncached_channels / 50`
  (channels.list batches up to 50 IDs at a time, cached after first
  resolution). 24h ≈ `57 * 24 = 1,368 units` — well under the 10K
  default quota. If this number creeps up unexpectedly, something is
  busting the cache (`data/sources/youtube_channel_cache.json`).
- `channel_errors > 0` for a few channels is fine (a private/deleted
  channel surfaces here). All channels erroring with the same error
  suggests an API key issue — check the secret.

**Reddit** (`scripts.poll_reddit`):
```
stats: {'feeds': 2, 'entries_seen': 50, 'dropped_since': 25,
        'deduped': 5, 'kept': 20, 'feed_errors': 0}
```
- `feed_errors == 2` means all feeds 429'd — see "Reddit 429 risk"
  below.

The commit step prints either `No data changes this cycle.` (normal
when nothing new) or the commit it pushed.

## Reddit 429 risk

Reddit rate-limits datacenter IPs (including GitHub Actions runners)
more aggressively than residential IPs. The poller has three layers
of defense already:

1. Descriptive User-Agent (Reddit returns 429 without one).
2. One retry on 429/403 with a 5-second backoff in `fetch_feed`.
3. 1-second polite sleep between feeds.

If `feed_errors` is persistently 2 across multiple cycles (i.e. both
top and hot are 429'ing), options in order of effort:

1. **Lower the cadence further**: edit `poll-reddit.yml` cron to
   `5 */2 * * *` (every 2 hours) or `5 */4 * * *` (every 4 hours).
   The `top/day` feed doesn't update faster than that anyway.
2. **Raise the backoff**: bump `RATE_LIMIT_BACKOFF_SEC` in
   `scripts/poll_reddit.py` from 5 to 30 or 60 seconds.
3. **Self-hosted runner**: move `poll-reddit.yml` to a self-hosted
   runner on a residential IP. Reddit treats residential IPs much
   more leniently.

Bluesky and Google News are unaffected by Reddit rate-limit issues —
they run in a different workflow.

## What to watch in the first few cycles after going live

- The commit log on `main` should show `data: poll cycle <ts>` and/or
  `data: reddit poll <ts>` commits at the expected cadence.
- A few `data/{source}/{date}.json` files should appear and grow as
  items land. Spot-check one or two: valid JSON, items sorted by
  `published_at`, ids prefixed with `bs-`/`gn-`/`rd-`, `players`/
  `teams` populated where applicable.
- Action runtime should stay under 2 minutes per cycle. Longer than
  that suggests one of the sources is timing out — check the per-step
  duration in the Actions UI.

## Common operations

**Pause ingestion**: Actions tab → workflow → ⋯ menu → Disable
workflow. The cron stops; in-flight runs finish normally.

**Re-run a failed cycle**: Actions tab → click the failed run → **Re-run all jobs**. Concurrency group still applies, so this queues if
another run is already going.

**Inspect a shard locally**:
```bash
jq '.items | length' data/bluesky/$(date -u +%Y-%m-%d).json
jq '.items[0]' data/google-news/$(date -u +%Y-%m-%d).json
```

**Reset Google News rotation** (rare): edit
`data/sources/google_news_state.json`, set `player_cursor` and
`team_cursor` to 0, commit. Next cycle starts from the top of the
canonical lists.

**Refresh the player canonical** (do this when the NBA roster turns
over, ~once per season + at the trade deadline): the canonical is
generated from the `jsierrahoopshype/nba-headshots` repo's
`players/metadata/active.json`. To pull the latest:

```bash
curl -L -o /tmp/nbah-active.json \
  https://raw.githubusercontent.com/jsierrahoopshype/nba-headshots/main/players/metadata/active.json
python -m scripts.build_players_canonical
# Re-tag the archive against the refreshed canonical:
python -m scripts.build_indexes
python -m scripts.prerender_pages
git add data/ players/ teams/ sitemap.xml
git commit -m "data: refresh player canonical from nba-headshots"
```

The build_players_canonical script preserves curated short-form
aliases (Wemby, SGA, KD, JB, ...) from the previous canonical when
the slug carries over. New rookies start with just `full_name` as
the alias; add nicknames manually after they show up in posts.
