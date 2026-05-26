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

  // How many items each live-merge call asks for.
  LIVE_MERGE_LIMITS: {
    bluesky: 30,    // per Bluesky-reporter "trending" handles, search is broad
    reddit: 25,     // top of day
    googleNews: 15, // per topic query
    substack: 10,   // per publication feed
  },

  // The Bluesky reporters list lives in the nba-buzz HF Space; we
  // proxy it through the same CORS Worker for browser fetches. (Same
  // file the poll_bluesky.py poller uses.)
  BLUESKY_HANDLES_URL:
    "https://huggingface.co/spaces/cdechoch/nba-buzz/raw/main/bluesky_handles.csv",

  // Where Bluesky's public AppView lives (CORS-friendly, no proxy needed).
  BLUESKY_APPVIEW_BASE: "https://public.api.bsky.app",
};
