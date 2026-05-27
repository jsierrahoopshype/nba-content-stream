/**
 * NBA Content Stream — CORS proxy Worker
 *
 * Fetches RSS / feed URLs the browser can't reach directly (Reddit,
 * Google News, Substack) and re-emits them with permissive CORS so the
 * frontend's live-merge layer can pull fresh items client-side.
 *
 * SECURITY: this proxy is allowlist-only. It accepts an `?url=` query
 * param, validates the host against ALLOWED_HOSTS, and refuses anything
 * else with a 403. Without the allowlist this would be an open proxy.
 *
 * Routes:
 *   GET /?url=<encoded feed url>   -> proxied body, CORS open
 *   OPTIONS /                      -> preflight 204
 *   GET /health                    -> 200 "ok"
 *
 * Deploy: `wrangler deploy` from worker-cors/. The deployed URL gets
 * pasted into assets/config.js so the frontend uses it.
 */

const ALLOWED_HOSTS = new Set([
  "www.reddit.com",
  "reddit.com",
  "old.reddit.com",
  "news.google.com",
  // huggingface.co is here as belt-and-suspenders for the Bluesky
  // reporter list. The frontend now loads that CSV same-origin from a
  // committed snapshot, so this entry is only used if some future code
  // path proxies an HF request. Keeping it permitted prevents another
  // silent live-fetch death like the one PR #X fixed.
  "huggingface.co",
]);

// substack is *.substack.com — exact match doesn't suffice. Suffix match.
const ALLOWED_SUFFIXES = [".substack.com"];

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "content-type",
  "Access-Control-Max-Age": "86400",
};

function hostAllowed(hostname) {
  if (ALLOWED_HOSTS.has(hostname)) return true;
  for (const sfx of ALLOWED_SUFFIXES) {
    if (hostname.endsWith(sfx)) return true;
  }
  return false;
}

function respond(body, init = {}) {
  const headers = new Headers(init.headers || {});
  for (const [k, v] of Object.entries(CORS_HEADERS)) headers.set(k, v);
  return new Response(body, { ...init, headers });
}

export default {
  async fetch(request) {
    if (request.method === "OPTIONS") {
      return respond(null, { status: 204 });
    }

    const url = new URL(request.url);

    if (url.pathname === "/health") {
      return respond("ok", { status: 200 });
    }

    const target = url.searchParams.get("url");
    if (!target) {
      return respond("missing ?url=", { status: 400 });
    }

    let parsed;
    try {
      parsed = new URL(target);
    } catch {
      return respond("invalid url", { status: 400 });
    }

    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
      return respond("scheme not allowed", { status: 400 });
    }

    if (!hostAllowed(parsed.hostname)) {
      return respond(`host not allowed: ${parsed.hostname}`, { status: 403 });
    }

    // Re-fetch from Cloudflare's edge. The User-Agent matters for Reddit
    // (a generic UA gets 429ed). Same UA shape we use server-side.
    let upstream;
    try {
      upstream = await fetch(parsed.toString(), {
        method: "GET",
        headers: {
          "User-Agent":
            "nba-content-stream-cors/0.1 (HoopsMatic; +https://github.com/jsierrahoopshype/nba-content-stream)",
          Accept: "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        },
        cf: { cacheTtl: 60, cacheEverything: true },
      });
    } catch (err) {
      return respond(`upstream fetch error: ${err.message}`, { status: 502 });
    }

    const contentType = upstream.headers.get("content-type") || "application/xml";
    const body = await upstream.arrayBuffer();
    return respond(body, {
      status: upstream.status,
      headers: { "content-type": contentType },
    });
  },
};
