# nba-content-stream-cors

A tiny Cloudflare Worker that proxies RSS / feed URLs so the browser
can pull them client-side for the live-merge layer of the NBA Content
Stream frontend. Reddit, Google News, and Substack feeds don't emit
CORS headers; this Worker fetches them from Cloudflare's edge and
re-emits with `Access-Control-Allow-Origin: *`.

## Security

**Allowlist only.** The Worker validates the requested host against:

- `www.reddit.com`, `reddit.com`, `old.reddit.com`
- `news.google.com`
- `*.substack.com`
- `huggingface.co`

Any other host gets a 403. Without this allowlist the Worker would be
an open proxy.

`huggingface.co` is here as belt-and-suspenders for the Bluesky
reporter list. The frontend loads that CSV same-origin from a committed
snapshot (`data/sources/bluesky_handles.csv`), so the Worker entry is
only exercised by potential future code paths that proxy HF.

## Routes

| Path                | Behavior |
|---------------------|----------|
| `GET /?url=<feed>`  | Proxied body, CORS open. 60s edge cache. |
| `OPTIONS /`         | 204 preflight. |
| `GET /health`       | `200 "ok"` for uptime checks. |

## Deploy

```bash
cd worker-cors
wrangler deploy
```

The deployed URL will be something like
`https://nba-content-stream-cors.thejorgesierra.workers.dev`. Paste it
into `assets/config.js` on the frontend (`CORS_PROXY_URL`).

## Local testing

```bash
wrangler dev
# In another terminal:
curl 'http://localhost:8787/?url=https://www.reddit.com/r/nba/top/.rss?t=day' | head
```

## Cost

Free tier: 100K requests/day. The frontend live-merges every ~60s on
page open; with the 60s edge cache, most pageloads hit cache instead
of upstream. Well inside the free tier.
