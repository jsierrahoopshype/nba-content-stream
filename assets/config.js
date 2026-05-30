// NBA Content Stream — frontend config.
// Single place for env-ish constants. No build step; just edit this
// file and reload.

window.NCS_CONFIG = {
  // Deployed CORS proxy URL. Until the Worker is deployed, the live-
  // merge for Reddit / Google News / Substack will fail gracefully —
  // the archived items from the index files still render fine. Update
  // this after `wrangler deploy` in worker-cors/.
  //
  // To DISABLE the proxy entirely (archive-only mode), set to null.
  CORS_PROXY_URL: "https://nba-content-stream-cors.thejorgesierra.workers.dev",

  // Live merges run once on page open. We do NOT poll on a timer; the
  // committed index files refresh every 15 min and a user can reload.
  // Set to false to skip live merges entirely.
  LIVE_MERGE_ENABLED: true,

  // How many items each live-merge call asks for. These are SOFT
  // ceilings on the final pool per source, not per-feed budgets —
  // Bluesky polls every reporter in the CSV (~164 handles × ~3
  // recent posts each) and then this cap is the upper bound on what
  // lands in the unified feed.
  LIVE_MERGE_LIMITS: {
    bluesky: 250,   // ~164 reporters × ~3 posts = ~500 pre-filter; keep top 250
    reddit: 25,     // top of day
    googleNews: 15, // per topic query
    substack: 30,   // per publication feed × N pubs
  },

  // The Bluesky reporters list, committed in this repo and served by
  // GitHub Pages alongside the rest of the site. Loaded same-origin —
  // no CORS, no Worker, no HuggingFace dependency. Path is relative to
  // the site root; resolved through dataUrl() so it works on every
  // page (root, /players/{slug}.html, project-pages subpath, etc).
  BLUESKY_HANDLES_URL: "data/sources/bluesky_handles.csv",

  // Where Bluesky's public AppView lives (CORS-friendly, no proxy needed).
  BLUESKY_APPVIEW_BASE: "https://public.api.bsky.app",
};

// Derive a site base path so the same JS works on:
//   - root deploy:      https://example.com/
//   - subpath deploy:   https://example.com/nba-content-stream/
//   - any nested page:  /players/{slug}.html, /teams/{slug}.html
// Without this, a fetch of "data/foo.json" from /players/lebron.html
// resolves as /players/data/foo.json → 404. Storing an absolute base
// and prefixing every data path makes fetches work regardless of which
// page the user is on.
window.NCS_CONFIG.SITE_BASE = (function () {
  const path =
    (typeof window !== "undefined" && window.location && window.location.pathname) || "/";
  const match = path.match(/^\/([^\/]+)\//);
  if (match && match[1] !== "players" && match[1] !== "teams") {
    return "/" + match[1];
  }
  return "";
})();

// Build an absolute-from-root URL for a repo-relative data path.
// `relativePath` should NOT have a leading slash ("data/foo.json").
window.NCS_dataUrl = function (relativePath) {
  const rel = String(relativePath || "").replace(/^\/+/, "");
  return window.NCS_CONFIG.SITE_BASE + "/" + rel;
};
