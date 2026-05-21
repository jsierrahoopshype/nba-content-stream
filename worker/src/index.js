/**
 * NBA Content Stream API
 *
 * Reads cold-tier archive shards from R2 for player/team pages
 * and date-range queries. The hot tier (last 30 days) is served
 * directly from GitHub Pages, so the Worker is only hit for
 * historical and aggregated queries.
 *
 * Phase 1: skeleton. Endpoint logic implemented in Phase 3+.
 */

const JSON_HEADERS = {
  "Content-Type": "application/json",
  "Cache-Control": "public, max-age=300, s-maxage=300",
};

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(env) });
    }

    if (request.method !== "GET") {
      return jsonError(env, 405, "Method not allowed");
    }

    try {
      // Routing
      if (url.pathname === "/" || url.pathname === "/health") {
        return json(env, { status: "ok", service: "nba-content-stream-api", phase: 1 });
      }

      // GET /players/:slug?limit=50&before=ISO_DATE
      const playerMatch = url.pathname.match(/^\/players\/([a-z0-9-]+)$/);
      if (playerMatch) {
        return await getPlayerArchive(playerMatch[1], url.searchParams, env);
      }

      // GET /teams/:slug?limit=50&before=ISO_DATE
      const teamMatch = url.pathname.match(/^\/teams\/([a-z0-9-]+)$/);
      if (teamMatch) {
        return await getTeamArchive(teamMatch[1], url.searchParams, env);
      }

      // GET /archive/:source/:date  (e.g., /archive/youtube/2026-01-15)
      const archiveMatch = url.pathname.match(/^\/archive\/([a-z-]+)\/(\d{4}-\d{2}-\d{2})$/);
      if (archiveMatch) {
        return await getArchiveShard(archiveMatch[1], archiveMatch[2], env);
      }

      // GET /search?q=...&from=ISO&to=ISO
      if (url.pathname === "/search") {
        return await search(url.searchParams, env);
      }

      return jsonError(env, 404, "Not found");
    } catch (err) {
      console.error("Worker error:", err);
      return jsonError(env, 500, "Internal error");
    }
  },
};

// -----------------------------------------------------------------------------
// Route handlers (Phase 3+ will flesh these out)
// -----------------------------------------------------------------------------

async function getPlayerArchive(slug, params, env) {
  // Phase 3: read player index from R2, return paginated items
  return json(env, {
    player: slug,
    items: [],
    phase: 1,
    note: "Endpoint stub. Real implementation in Phase 3.",
  });
}

async function getTeamArchive(slug, params, env) {
  // Phase 3
  return json(env, {
    team: slug,
    items: [],
    phase: 1,
    note: "Endpoint stub. Real implementation in Phase 3.",
  });
}

async function getArchiveShard(source, date, env) {
  // Phase 3
  if (!env.ARCHIVE) {
    return jsonError(env, 503, "R2 binding not configured yet");
  }
  const key = `${source}/${date}.json`;
  try {
    const obj = await env.ARCHIVE.get(key);
    if (!obj) {
      return jsonError(env, 404, `Shard not found: ${key}`);
    }
    const body = await obj.text();
    return new Response(body, {
      headers: { ...JSON_HEADERS, ...corsHeaders(env) },
    });
  } catch (err) {
    return jsonError(env, 500, `R2 read failed: ${err.message}`);
  }
}

async function search(params, env) {
  // Phase 3: paginated search across cold-tier shards
  return json(env, {
    query: params.get("q") || "",
    results: [],
    phase: 1,
    note: "Endpoint stub. Real implementation in Phase 3.",
  });
}

// -----------------------------------------------------------------------------
// Helpers
// -----------------------------------------------------------------------------

function corsHeaders(env) {
  const origin = (env && env.CORS_ORIGIN) || "*";
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-API-Key",
    "Access-Control-Max-Age": "86400",
  };
}

function json(env, body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...JSON_HEADERS, ...corsHeaders(env) },
  });
}

function jsonError(env, status, message) {
  return json(env, { error: message, status }, status);
}
