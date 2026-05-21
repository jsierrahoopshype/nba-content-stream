# NBA Content Stream Worker

Cloudflare Worker that reads cold-tier archive shards from R2 for player/team pages and historical queries. Hot tier (last 30 days) is served directly by GitHub Pages so the Worker is only hit for historical and aggregated reads.

## Routes

| Route | Purpose | Phase |
|---|---|---|
| `GET /health` | Liveness check | 1 |
| `GET /players/:slug` | Player archive (paginated) | 3 |
| `GET /teams/:slug` | Team archive (paginated) | 3 |
| `GET /archive/:source/:date` | Raw shard fetch from R2 | 3 |
| `GET /search?q=...` | Cross-source historical search | 3 |

## First-time setup

```bash
# Install wrangler if you don't have it
npm install -g wrangler

# Login
wrangler login

# Create the R2 bucket (one time)
wrangler r2 bucket create nba-content-stream-archive

# Deploy
cd worker
wrangler deploy
```

After first deploy, the Worker is available at:
`https://nba-content-stream-api.thejorgesierra.workers.dev`

## Local dev

```bash
cd worker
wrangler dev
```

This runs the Worker locally on `http://localhost:8787`. R2 reads use a local emulation by default.

## Secrets

None required in Phase 1. When the frontend later needs an API key for rate limiting:

```bash
wrangler secret put API_KEY_HASH
```

## CORS

The Worker allows requests from `https://jsierrahoopshype.github.io` by default. Override via the `CORS_ORIGIN` env var in `wrangler.toml`.

## Deploy on push (optional)

Wire up a `.github/workflows/deploy-worker.yml` later that calls `wrangler deploy` when `worker/` changes. Skipped in Phase 1 to keep things simple.
