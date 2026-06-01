/* NBA Content Stream — shared rendering + live-merge helpers.
 *
 * Used by feed.js (homepage) and entity.js (per-player / per-team
 * pages). Pure DOM + fetch; no framework.
 */
(function () {
  "use strict";

  const C = window.NCS_CONFIG || {};

  // Fix 4: avatar cache for Bluesky cards.
  //
  // Root cause: archive Bluesky items (the ones in data/index/*.json)
  // don't carry author_avatar — the server-side poll_bluesky.py
  // doesn't capture it and build_indexes._compact_item doesn't pass
  // anything like it through. Live items DO carry the avatar from
  // post.author.avatar. On the main feed the live items mask the
  // archive gap, but on entity pages most cards are archive-filtered
  // and very few live items match the entity, so the gap is visible.
  //
  // The cache is keyed by bluesky handle (the bsky.social-style
  // identifier we can extract from item.url even for archive items).
  // bskyLiveItems writes (handle -> avatar_url) for every reporter
  // seen on each pageload. renderCard reads it as a fallback when
  // item.author_avatar is missing. After live merge completes,
  // entity.js re-renders, and archive cards pick up cached avatars.
  if (!window.NCS_AvatarCache) window.NCS_AvatarCache = new Map();
  // Polish-5 / Fix 3: gated debug traces for the avatar pipeline.
  // This is the 3rd PR attempting to fix avatars on entity pages;
  // previous PRs traced the code on paper but the bug persisted.
  // To diagnose decisively, set `window.NCS_DEBUG = true` in the
  // console, reload, and read [NCS-AVATAR-TRACE] log lines to see
  // exactly which step in the chain breaks.
  function _dbg(stage, info) {
    if (window.NCS_DEBUG) {
      // eslint-disable-next-line no-console
      console.debug("[NCS-AVATAR-TRACE]", stage, info);
    }
  }
  function _cacheBskyAvatar(handle, url) {
    if (handle && url) {
      window.NCS_AvatarCache.set(handle, url);
      _dbg("cache:write", { handle, url, size: window.NCS_AvatarCache.size });
    }
  }
  function _lookupBskyAvatar(handle) {
    const hit = handle ? window.NCS_AvatarCache.get(handle) || null : null;
    _dbg("cache:lookup", {
      handle,
      hit: !!hit,
      cache_size: window.NCS_AvatarCache.size,
    });
    return hit;
  }
  // Extract handle from a Bluesky post URL — works on both live and
  // archive items because both carry the public bsky.app URL.
  const _BSKY_HANDLE_FROM_URL_RE = /https?:\/\/bsky\.app\/profile\/([^/]+)\/post\//;
  function _handleFromBskyUrl(url) {
    if (!url) {
      _dbg("url:extract", { url, handle: "" });
      return "";
    }
    const m = _BSKY_HANDLE_FROM_URL_RE.exec(url);
    const handle = m ? m[1] : "";
    _dbg("url:extract", { url, handle });
    return handle;
  }

  // ---------------------------------------------------------------------
  // Time formatting
  // ---------------------------------------------------------------------

  function relativeTime(iso) {
    if (!iso) return "";
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return "";
    const diffSec = Math.max(0, (Date.now() - t) / 1000);
    if (diffSec < 60) return Math.floor(diffSec) + "s ago";
    if (diffSec < 3600) return Math.floor(diffSec / 60) + "m ago";
    if (diffSec < 86400) return Math.floor(diffSec / 3600) + "h ago";
    if (diffSec < 30 * 86400) return Math.floor(diffSec / 86400) + "d ago";
    const d = new Date(iso);
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  // ---------------------------------------------------------------------
  // Card rendering (compact item -> DOM)
  // ---------------------------------------------------------------------

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function entityTagsHtml(item, pathPrefix, manifestSlugs) {
    const players = (item.players || []).filter((s) => manifestSlugs.players.has(s));
    const teams = (item.teams || []).filter((s) => manifestSlugs.teams.has(s));
    if (!players.length && !teams.length) return "";
    const parts = [];
    for (const slug of players) {
      const name = manifestSlugs.playerName[slug] || slug;
      parts.push(
        `<a class="tag" href="${pathPrefix}players/${escapeHtml(slug)}.html">${escapeHtml(name)}</a>`
      );
    }
    for (const slug of teams) {
      const name = manifestSlugs.teamName[slug] || slug;
      parts.push(
        `<a class="tag team" href="${pathPrefix}teams/${escapeHtml(slug)}.html">${escapeHtml(name)}</a>`
      );
    }
    return `<div class="tags">${parts.join("")}</div>`;
  }

  // --- 48h media window helper ---
  // All non-YouTube media (Bluesky images, Reddit thumb, GN image,
  // Bluesky external-card thumb) is only rendered for items published
  // within the last 48h. Older items are text-only — this keeps the
  // archive lightweight and avoids long-tail link-card thumbnails
  // potentially going stale.
  const MEDIA_WINDOW_MS = 48 * 3600 * 1000;
  function withinMediaWindow(publishedAt) {
    if (!publishedAt) return false;
    const t = new Date(publishedAt).getTime();
    if (Number.isNaN(t)) return false;
    return Date.now() - t < MEDIA_WINDOW_MS;
  }

  function ytVideoIdFromItem(item) {
    // Item id is `yt-{videoId}` per SHARD_FORMAT.md.
    const m = /^yt-([A-Za-z0-9_\-]{6,})$/.exec(item.id || "");
    return m ? m[1] : null;
  }

  function renderYoutubeEmbed(item, fresh) {
    // Lazy iframe: show the thumbnail with a play button overlay;
    // swap in the iframe on first click. Sanctioned official embed
    // — youtube.com/embed/{id} — counts views for the creator. We
    // ALWAYS render the YouTube embed regardless of age (the user
    // owns it, click-to-load is bandwidth-cheap).
    const vid = ytVideoIdFromItem(item);
    if (!vid) return "";
    const poster = item.thumbnail || `https://i.ytimg.com/vi/${vid}/hqdefault.jpg`;
    return `
      <div class="yt-embed" data-vid="${escapeHtml(vid)}">
        <img class="yt-poster" src="${escapeHtml(poster)}" alt="" loading="lazy">
        <button class="yt-play" aria-label="Play video">
          <svg viewBox="0 0 68 48" width="68" height="48"><path d="M66.5 7.7a8.4 8.4 0 0 0-5.9-5.9C55.4 0 34 0 34 0S12.6 0 7.4 1.8a8.4 8.4 0 0 0-5.9 5.9C0 12.9 0 24 0 24s0 11.1 1.5 16.3a8.4 8.4 0 0 0 5.9 5.9C12.6 48 34 48 34 48s21.4 0 26.6-1.8a8.4 8.4 0 0 0 5.9-5.9C68 35.1 68 24 68 24s0-11.1-1.5-16.3z" fill="#f00"/><path d="M27 34 45 24 27 14z" fill="#fff"/></svg>
        </button>
      </div>
    `;
  }

  function renderBlueskyMedia(item) {
    if (!withinMediaWindow(item.published_at)) return "";
    const media = item.media || {};
    if (media.type === "image" && Array.isArray(media.images) && media.images.length) {
      // Show up to 4 thumbnails in a small grid for multi-image posts.
      const shown = media.images.slice(0, 4);
      const cls = shown.length > 1 ? "bsky-images grid" : "bsky-images";
      return (
        `<div class="${cls}">` +
        shown.map((img) =>
          `<a href="${escapeHtml(item.url)}" target="_blank" rel="noopener"><img src="${escapeHtml(img.url)}" alt="${escapeHtml(img.alt || "")}" loading="lazy"></a>`
        ).join("") +
        `</div>`
      );
    }
    if (media.type === "video" && media.thumbnail) {
      // We don't try to play Bluesky video inline (HLS would need a
      // player lib). Show the thumbnail as a poster that opens the
      // bsky.app post on click.
      return `<a class="bsky-video" href="${escapeHtml(item.url)}" target="_blank" rel="noopener"><img src="${escapeHtml(media.thumbnail)}" alt="" loading="lazy"><span class="play-hint">▶ Play on Bluesky</span></a>`;
    }
    if (media.type === "link" && media.uri) {
      const thumbHtml = media.thumb
        ? `<img src="${escapeHtml(media.thumb)}" alt="" loading="lazy">`
        : "";
      const host = (() => {
        try { return new URL(media.uri).hostname.replace(/^www\./, ""); }
        catch { return ""; }
      })();
      return `
        <a class="bsky-extcard" href="${escapeHtml(media.uri)}" target="_blank" rel="noopener">
          ${thumbHtml}
          <div class="ext-meta">
            <div class="ext-title">${escapeHtml(media.title || media.uri)}</div>
            ${media.description ? `<div class="ext-desc">${escapeHtml(media.description)}</div>` : ""}
            <div class="ext-host">${escapeHtml(host)}</div>
          </div>
        </a>
      `;
    }
    return "";
  }

  function renderSmallThumb(item) {
    // Reddit + Google News + Substack: small preview thumb if RSS gave
    // us one and item is within the 48h media window.
    if (!item.thumbnail) return "";
    if (!withinMediaWindow(item.published_at)) return "";
    return `<a class="rss-thumb" href="${escapeHtml(item.url || "#")}" target="_blank" rel="noopener"><img src="${escapeHtml(item.thumbnail)}" alt="" loading="lazy"></a>`;
  }

  // --- Byline icon: Bluesky avatar (Fix 3) or outlet favicon (Fix 4) ---

  function _itemHostname(item) {
    try {
      const u = new URL(item.url || "");
      return u.hostname.replace(/^www\./, "");
    } catch {
      return "";
    }
  }

  // Fix A3: link the outlet name to its homepage. We pick the
  // hostname's apex (e.g. espn.com) and build https://<host>. Returns
  // null if the URL is unparseable; the caller falls back to plain text.
  function _outletHomepageUrl(item) {
    const host = _itemHostname(item);
    if (!host) return null;
    return `https://${host}`;
  }

  // Fix A6: initials-avatar fallback for non-Bluesky cards with no
  // media. Generates a small circular block with one or two letters
  // derived from the outlet name or the first tagged entity. Color is
  // a muted source-tinted background. Skipped entirely if the card
  // already has rich media (a thumbnail, YouTube embed, etc.) to
  // avoid double-imagery.
  function _initialsFromAuthor(s) {
    if (!s) return "·";
    const tokens = s.replace(/[^\w\s]/g, " ").trim().split(/\s+/).filter(Boolean);
    if (!tokens.length) return "·";
    if (tokens.length === 1) return tokens[0].slice(0, 2).toUpperCase();
    return (tokens[0][0] + tokens[tokens.length - 1][0]).toUpperCase();
  }
  function initialsAvatarHtml(item) {
    if (item.source === "bluesky") return "";
    if (item.source === "youtube") return "";
    // Don't add the initials avatar if the card already shows a
    // thumbnail — would compete visually.
    if (item.thumbnail) return "";
    const initials = escapeHtml(_initialsFromAuthor(item.author));
    return `<div class="initials-avatar src-tint-${escapeHtml(item.source)}" aria-hidden="true">${initials}</div>`;
  }

  // Cluster C: headshot + team-logo URL helpers.
  //
  // Player headshots live in the nba-headshots repo, at
  // players/headshots/face/{filename}, where {filename} is the EXACT
  // filename stored by upstream (e.g. "203999-nikola-joki.png"). The
  // file is keyed by NBA-ID + upstream slug, NOT our cleaner slug,
  // because the upstream slugify dropped diacritics; we store the
  // raw filename verbatim under canonical[slug].headshot_filename
  // and look it up here.
  //
  // Team logos come from ESPN's public CDN. Slug→ESPN-abbrev mapping
  // is mostly the team's `abbr` field; a few teams use a different
  // short code on ESPN (sa for San Antonio, no for New Orleans, gs
  // for Golden State, ny for the Knicks, etc.).
  const _ESPN_ABBREV_OVERRIDES = {
    "san-antonio-spurs": "sa",
    "new-orleans-pelicans": "no",
    "golden-state-warriors": "gs",
    "new-york-knicks": "ny",
    "brooklyn-nets": "bkn",
    "los-angeles-clippers": "lac",
    "los-angeles-lakers": "lal",
    "oklahoma-city-thunder": "okc",
    "philadelphia-76ers": "phi",
    "portland-trail-blazers": "por",
    "washington-wizards": "wsh", // ESPN uses wsh, not was
    "utah-jazz": "utah",
    "phoenix-suns": "phx",
    "memphis-grizzlies": "mem",
    "minnesota-timberwolves": "min",
    "milwaukee-bucks": "mil",
    "miami-heat": "mia",
    "houston-rockets": "hou",
    "denver-nuggets": "den",
    "detroit-pistons": "det",
    "dallas-mavericks": "dal",
    "cleveland-cavaliers": "cle",
    "chicago-bulls": "chi",
    "charlotte-hornets": "cha",
    "boston-celtics": "bos",
    "atlanta-hawks": "atl",
    "indiana-pacers": "ind",
    "orlando-magic": "orl",
    "sacramento-kings": "sac",
    "toronto-raptors": "tor",
  };

  // Lazily-loaded canonical (players.json / teams.json) so the helpers
  // can resolve any slug without forcing every caller to pass dicts.
  // After loadCanonical() resolves, window.NCS_Canonical is populated
  // synchronously for renderCard to read.
  let _canonicalLoading = null;
  async function loadCanonical() {
    if (window.NCS_Canonical) return window.NCS_Canonical;
    if (_canonicalLoading) return _canonicalLoading;
    _canonicalLoading = (async () => {
      const fetchJson = async (p) => {
        try {
          const r = await fetch(p);
          if (r.ok) return await r.json();
        } catch {}
        return null;
      };
      const players =
        (await fetchJson(window.NCS_dataUrl("data/canonical/players.json"))) || {};
      const teams =
        (await fetchJson(window.NCS_dataUrl("data/canonical/teams.json"))) || {};
      window.NCS_Canonical = { players, teams };
      return window.NCS_Canonical;
    })();
    return _canonicalLoading;
  }

  const NBA_HEADSHOTS_BASE =
    "https://raw.githubusercontent.com/jsierrahoopshype/nba-headshots/main/players/headshots/face/";

  function headshotUrl(playerSlug, players) {
    if (!playerSlug || !players) return null;
    const entry = players[playerSlug];
    if (!entry || !entry.headshot_filename) return null;
    return NBA_HEADSHOTS_BASE + entry.headshot_filename;
  }

  function teamLogoUrl(teamSlug) {
    if (!teamSlug) return null;
    const abbrev = _ESPN_ABBREV_OVERRIDES[teamSlug];
    if (!abbrev) return null;
    return `https://a.espncdn.com/i/teamlogos/nba/500/${abbrev}.png`;
  }

  // ESPN-hosted NBA league logo. Used as a generic-NBA fallback when
  // a card has no tagged player or team — better than bare initials
  // when we have no idea who the story is about.
  const NBA_LEAGUE_LOGO_URL = "https://a.espncdn.com/i/teamlogos/leagues/500/nba.png";

  // Polish-10 (Fix 3): team → conference map for the leaderboard
  // dashboards. canonical/teams.json carries name/city/abbr/aliases
  // but not conference; rather than re-derive from the league API
  // we hardcode the 30-team split here. East and West haven't
  // changed structure in modern memory, so a static map is fine
  // and keeps the dashboards offline-renderable.
  const _TEAM_CONFERENCE = {
    "atlanta-hawks": "East", "boston-celtics": "East",
    "brooklyn-nets": "East", "charlotte-hornets": "East",
    "chicago-bulls": "East", "cleveland-cavaliers": "East",
    "detroit-pistons": "East", "indiana-pacers": "East",
    "miami-heat": "East", "milwaukee-bucks": "East",
    "new-york-knicks": "East", "orlando-magic": "East",
    "philadelphia-76ers": "East", "toronto-raptors": "East",
    "washington-wizards": "East",
    "dallas-mavericks": "West", "denver-nuggets": "West",
    "golden-state-warriors": "West", "houston-rockets": "West",
    "los-angeles-clippers": "West", "los-angeles-lakers": "West",
    "memphis-grizzlies": "West", "minnesota-timberwolves": "West",
    "new-orleans-pelicans": "West", "oklahoma-city-thunder": "West",
    "phoenix-suns": "West", "portland-trail-blazers": "West",
    "sacramento-kings": "West", "san-antonio-spurs": "West",
    "utah-jazz": "West",
  };

  function teamConference(teamSlug) {
    return _TEAM_CONFERENCE[teamSlug] || "";
  }

  // Polish-10 (Fix 2): render a manifest count with a "+" suffix
  // when it's saturated at the per-entity cap. Cap is read from
  // manifest.max_items_per_entity; callers pass the value (or null
  // if absent on an old manifest).
  function formatCappedCount(count, cap) {
    if (cap != null && count >= cap) return `${cap}+`;
    return String(count);
  }

  // Cluster C: visual integration. For a non-Bluesky non-YouTube card
  // with NO thumbnail, prefer (in order):
  //   1. first tagged player's headshot
  //   2. first tagged team's ESPN logo
  //   3. NBA league logo (generic-NBA fallback)
  //   4. the initials-avatar fallback (existing behavior, only if the
  //      league logo also errors)
  // onerror chains gracefully — if the headshot image 404s, the
  // browser hides the img; we still show the initials underneath
  // (rendered into the same slot via a wrapper).
  function _imgWithFallback(src, fallbackHtml, cls) {
    // The fallback is a sibling that's visually hidden until the
    // image fires onerror. Cleanest: render both and use a small
    // inline-onerror handler to swap.
    return `
      <span class="img-fallback-wrap">
        <img class="${cls}" src="${escapeHtml(src)}" alt="" loading="lazy"
          onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
        <span class="img-fallback-content" style="display:none">${fallbackHtml}</span>
      </span>
    `;
  }

  function visualAvatarHtml(item) {
    // Bluesky has its own avatar from author info.
    if (item.source === "bluesky") return "";
    // YouTube has its own video thumbnail.
    if (item.source === "youtube") return "";
    // If the card already has a media thumbnail, don't compete.
    if (item.thumbnail) return "";

    const initials = escapeHtml(_initialsFromAuthor(item.author));
    const initialsHtml = `<span class="initials-avatar src-tint-${escapeHtml(item.source)}" aria-hidden="true">${initials}</span>`;

    // Read canonical from window if loadCanonical() has resolved.
    // First render before canonical loads uses initials only; the
    // second render after canonical load (triggered by loadLive)
    // upgrades to headshots/logos.
    const canonical = window.NCS_Canonical;
    if (!canonical) return initialsHtml;

    // Prefer a tagged player's headshot.
    const firstPlayer = (item.players || [])[0];
    if (firstPlayer) {
      const url = headshotUrl(firstPlayer, canonical.players);
      if (url) return _imgWithFallback(url, initialsHtml, "visual-avatar visual-headshot");
    }
    // Fall back to a tagged team's logo.
    const firstTeam = (item.teams || [])[0];
    if (firstTeam) {
      const url = teamLogoUrl(firstTeam);
      if (url) return _imgWithFallback(url, initialsHtml, "visual-avatar visual-team-logo");
    }
    // Nothing tagged — show the generic NBA league logo. Initials
    // remain the final fallback if the league logo also fails.
    return _imgWithFallback(NBA_LEAGUE_LOGO_URL, initialsHtml, "visual-avatar visual-league-logo");
  }

  function bylineIconHtml(item) {
    // Bluesky cards: use the post author's avatar (the user's identity).
    // Live items carry author_avatar from the AppView response; archive
    // items don't have it yet (poller doesn't capture). Render an
    // <img> when available; nothing otherwise — no fallback image, the
    // existing text byline is the fallback.
    if (item.source === "bluesky") {
      if (item.author_avatar) {
        return `<img class="byline-avatar" src="${escapeHtml(item.author_avatar)}" alt="" loading="lazy" onerror="this.style.display='none'">`;
      }
      return "";
    }
    // YouTube cards already have a thumbnail in the body; skip the
    // byline icon to avoid double-imagery.
    if (item.source === "youtube") return "";
    // Reddit / Google News / Substack: outlet favicon via Google's
    // free favicon service. Works for any reachable origin, no API
    // key. onerror hides the img element if the favicon 404s, leaving
    // just the text source badge.
    const host = _itemHostname(item);
    if (!host) return "";
    const src = `https://www.google.com/s2/favicons?domain=${encodeURIComponent(host)}&sz=32`;
    return `<img class="byline-favicon" src="${escapeHtml(src)}" alt="" loading="lazy" onerror="this.style.display='none'">`;
  }

  // Fix E (previous PR): linkify bare http(s) URLs in plain text.
  // Conservative regex — must start with http:// or https://. Returns
  // innerHTML-safe markup; callers must NOT also escape the result.
  const _URL_RE = /\bhttps?:\/\/[^\s<>()"']+[^\s<>()"',.;:!?]/g;
  function linkifyEscaped(plain) {
    if (!plain) return "";
    const escaped = escapeHtml(plain);
    return escaped.replace(_URL_RE, (m) =>
      `<a class="inline-url" href="${m}" target="_blank" rel="noopener noreferrer">${m}</a>`
    );
  }

  // Fix B2: regex fallback for @-mentions. The handle pattern matches
  // {label}.bsky.social or any *.{tld} the AppView shows on Bluesky
  // (custom domains like @marc.bsky.team). Restricted to alphanumerics,
  // dots, and dashes so it doesn't slurp punctuation. Only used when
  // record.facets isn't available.
  const _MENTION_RE = /@([a-z0-9](?:[a-z0-9.\-]*[a-z0-9])?\.[a-z][a-z0-9.\-]*[a-z])/gi;
  function linkifyMentionsEscaped(linkedHtml) {
    // linkedHtml is the OUTPUT of linkifyEscaped — it may contain
    // existing <a class="inline-url"> tags for URLs. We only want to
    // wrap @mentions in spans that are NOT already inside an anchor.
    // Cheap and good enough: split on existing anchor tags, transform
    // only the non-anchor segments. This avoids accidentally wrapping
    // an @-sign that's part of a URL's query string.
    return linkedHtml.replace(
      /(<a [^>]*>[\s\S]*?<\/a>)|([^<]+)/g,
      (_, anchor, plain) => {
        if (anchor) return anchor;
        return plain.replace(
          _MENTION_RE,
          (full, handle) =>
            `<a class="inline-mention" href="https://bsky.app/profile/${handle}" target="_blank" rel="noopener noreferrer">@${handle}</a>`
        );
      }
    );
  }

  // Fix B2 (preferred path): use Bluesky's record.facets to render
  // @mentions and #tags with the canonical did/uri the server gives us.
  // Falls through to URL + regex-mention linkify when no usable mention
  // facet is found.
  function renderBlueskyRichText(text, facets) {
    if (!text) return "";
    // Build a unified facet range list. Each entry has {start, end,
    // kind, target} where kind is "mention"|"link" and target is the
    // did (for mentions) or the canonical URI (for links). #link
    // facets are critical for posts where Bluesky displays a truncated
    // URL ("youtu.be/w-U3...") that doesn't match the bare-URL regex —
    // without the facet, those segments stay un-clickable. We process
    // mentions and links in the same byte-range pass.
    const ranges = [];
    for (const f of facets || []) {
      const feats = f.features || [];
      const mention = feats.find((x) => (x.$type || "").includes("richtext.facet#mention"));
      const link = feats.find((x) => (x.$type || "").includes("richtext.facet#link"));
      if (mention && mention.did && f.index && f.index.byteEnd > f.index.byteStart) {
        ranges.push({
          start: f.index.byteStart, end: f.index.byteEnd,
          kind: "mention", target: mention.did,
        });
      } else if (link && link.uri && f.index && f.index.byteEnd > f.index.byteStart) {
        ranges.push({
          start: f.index.byteStart, end: f.index.byteEnd,
          kind: "link", target: link.uri,
        });
      }
    }
    if (!ranges.length) {
      // No usable facets — regex-only path: linkify bare URLs first,
      // then linkify @mentions in the still-plain segments.
      return linkifyMentionsEscaped(linkifyEscaped(text));
    }
    // Sort and walk the byte ranges, slicing the original UTF-8 text
    // with TextEncoder/Decoder so multibyte chars don't break indices.
    ranges.sort((a, b) => a.start - b.start);
    const enc = new TextEncoder();
    const dec = new TextDecoder();
    const bytes = enc.encode(text);
    let out = "";
    let cursor = 0;
    for (const r of ranges) {
      if (r.start < cursor) continue; // skip overlapping/malformed
      // Plain segment before this facet: still pass through the
      // URL + mention regex linkify so any plain URLs not in the
      // facet list also get caught.
      const before = dec.decode(bytes.slice(cursor, r.start));
      out += linkifyMentionsEscaped(linkifyEscaped(before));
      const segment = dec.decode(bytes.slice(r.start, r.end));
      if (r.kind === "mention") {
        out += `<a class="inline-mention" href="https://bsky.app/profile/${escapeHtml(r.target)}" target="_blank" rel="noopener noreferrer">${escapeHtml(segment)}</a>`;
      } else {
        // #link facet — segment is the display text (potentially
        // truncated with an ellipsis); target is the canonical URI.
        out += `<a class="inline-url" href="${escapeHtml(r.target)}" target="_blank" rel="noopener noreferrer">${escapeHtml(segment)}</a>`;
      }
      cursor = r.end;
    }
    const trailing = dec.decode(bytes.slice(cursor));
    out += linkifyMentionsEscaped(linkifyEscaped(trailing));
    return out;
  }

  // Fix D: Bluesky image grid for live items (server media goes through
  // renderBlueskyMedia from item.media). Up to 4 images, 48h-gated.
  function renderBlueskyImagesLive(item) {
    if (!item.images || !item.images.length) return "";
    if (!withinMediaWindow(item.published_at)) return "";
    const shown = item.images.slice(0, 4);
    const cls = shown.length > 1 ? "bsky-images grid" : "bsky-images";
    return (
      `<div class="${cls}">` +
      shown
        .map(
          (img) =>
            `<a href="${escapeHtml(item.url)}" target="_blank" rel="noopener"><img src="${escapeHtml(img.url)}" alt="${escapeHtml(img.alt)}" loading="lazy"></a>`
        )
        .join("") +
      `</div>`
    );
  }

  // Fix 1: Bluesky native video. Render the thumbnail with a play
  // overlay; click opens the post on bsky.app where Bluesky's own
  // HLS player handles playback. We do NOT load an HLS library or
  // try to stream .m3u8 client-side. 48h gated.
  function renderBlueskyVideoLive(item) {
    const v = item.video;
    if (!v || (!v.thumbnail && !v.playlist)) return "";
    if (!withinMediaWindow(item.published_at)) return "";
    // Aspect ratio: prefer Bluesky's reported value; fall back to 16/9.
    let aspect = "16 / 9";
    if (v.aspectRatio && v.aspectRatio.width && v.aspectRatio.height) {
      aspect = `${v.aspectRatio.width} / ${v.aspectRatio.height}`;
    }
    // Thumbnail: prefer the AppView's still; if missing, show a black
    // placeholder rather than failing.
    const thumbAttr = v.thumbnail
      ? `src="${escapeHtml(v.thumbnail)}"`
      : `src=""`;
    return `
      <a class="bsky-video-link" href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer" style="aspect-ratio:${aspect};">
        <img class="bsky-video-poster" ${thumbAttr} alt="${escapeHtml(v.alt || "")}" loading="lazy" onerror="this.style.display='none'">
        <span class="bsky-video-play" aria-hidden="true">
          <svg viewBox="0 0 60 60" width="48" height="48"><circle cx="30" cy="30" r="29" fill="rgba(0,0,0,.55)"/><path d="M24 18 L42 30 L24 42 Z" fill="#fff"/></svg>
        </span>
        <span class="bsky-video-hint">▶ Play on Bluesky</span>
      </a>
    `;
  }

  // Fix B1: render a quoted post inline. Bordered, slightly indented
  // box that looks like a tweet quote — distinct from the linkCard
  // (which is an external article preview). Includes the quoted
  // author, text (rich-text processed), and any images/linkCard inside
  // the quoted post. Whole box is clickable to the quoted post URL.
  function renderQuotedPost(qp) {
    if (!qp) return "";
    if (qp.missing) {
      return `<div class="bsky-quoted bsky-quoted-missing">[quoted post unavailable]</div>`;
    }
    const avatarHtml = qp.author_avatar
      ? `<img class="bsky-quoted-avatar" src="${escapeHtml(qp.author_avatar)}" alt="" loading="lazy" onerror="this.style.display='none'">`
      : `<span class="bsky-quoted-avatar bsky-quoted-avatar-placeholder" aria-hidden="true"></span>`;
    const handleLine = qp.author_handle
      ? `<span class="bsky-quoted-handle">@${escapeHtml(qp.author_handle)}</span>`
      : "";
    // Polish-9 (Fix 1): use the SAME rich-text renderer as the outer
    // post so URL + @mention facets resolve to canonical did→profile
    // links inside the quote box (previously fell back to regex-only
    // linkifying because facets weren't extracted).
    const textHtml = renderBlueskyRichText(qp.text || "", qp.facets);
    // Polish-9 (Fix 1): timestamp in the quoted-post head, mirroring
    // the outer post's meta row. Helps readers tell when the quoted
    // post was originally published vs. when it was quoted.
    const timeHtml = qp.timestamp
      ? `<span class="bsky-quoted-time">${escapeHtml(relativeTime(qp.timestamp))}</span>`
      : "";
    // Quoted images: render a compact grid if present and within window.
    let mediaHtml = "";
    if (qp.images && qp.images.length) {
      const shown = qp.images.slice(0, 4);
      const cls = shown.length > 1 ? "bsky-quoted-images grid" : "bsky-quoted-images";
      mediaHtml += `<div class="${cls}">` + shown.map((img) =>
        `<img src="${escapeHtml(img.url)}" alt="${escapeHtml(img.alt)}" loading="lazy">`
      ).join("") + `</div>`;
    }
    // Polish-9 (Fix 1): quoted-post video. Thumbnail-only with a play
    // overlay; the whole .bsky-quoted box is already a click-through
    // to the quoted post on bsky.app, where the native player runs.
    if (qp.video && qp.video.thumbnail) {
      mediaHtml += `
        <div class="bsky-quoted-video">
          <img src="${escapeHtml(qp.video.thumbnail)}" alt="${escapeHtml(qp.video.alt || "")}" loading="lazy">
          <span class="bsky-quoted-video-hint">VIDEO · click to play on Bluesky</span>
        </div>
      `;
    }
    // Quoted linkCard inside a quote: render a compact one-line preview.
    if (qp.linkCard && qp.linkCard.uri) {
      const lc = qp.linkCard;
      let host = "";
      try { host = new URL(lc.uri).hostname.replace(/^www\./, ""); } catch {}
      mediaHtml += `
        <div class="bsky-quoted-linkcard">
          <span class="qlc-title">${escapeHtml(lc.title || lc.uri)}</span>
          <span class="qlc-host">${escapeHtml(host)}</span>
        </div>
      `;
    }
    const wrapperOpen = qp.url
      ? `<a class="bsky-quoted" href="${escapeHtml(qp.url)}" target="_blank" rel="noopener noreferrer">`
      : `<div class="bsky-quoted">`;
    const wrapperClose = qp.url ? `</a>` : `</div>`;
    return `
      ${wrapperOpen}
        <div class="bsky-quoted-head">
          ${avatarHtml}
          <span class="bsky-quoted-author">${escapeHtml(qp.author)}</span>
          ${handleLine}
          ${timeHtml}
        </div>
        <div class="bsky-quoted-text">${textHtml}</div>
        ${mediaHtml}
      ${wrapperClose}
    `;
  }

  // Fix C: Bluesky external link-card preview (rich box: thumb +
  // title + description + domain). 48h-gated.
  function renderBlueskyLinkCard(item) {
    const lc = item.linkCard;
    if (!lc || !lc.uri) return "";
    if (!withinMediaWindow(item.published_at)) return "";
    const host = (() => {
      try { return new URL(lc.uri).hostname.replace(/^www\./, ""); }
      catch { return ""; }
    })();
    const thumbHtml = lc.thumb
      ? `<img class="lc-thumb" src="${escapeHtml(lc.thumb)}" alt="" loading="lazy" onerror="this.style.display='none'">`
      : "";
    return `
      <a class="bsky-linkcard" href="${escapeHtml(lc.uri)}" target="_blank" rel="noopener noreferrer">
        ${thumbHtml}
        <div class="lc-meta">
          <div class="lc-title">${escapeHtml(lc.title || lc.uri)}</div>
          ${lc.description ? `<div class="lc-desc">${escapeHtml(lc.description)}</div>` : ""}
          <div class="lc-host">${escapeHtml(host)}</div>
        </div>
      </a>
    `;
  }

  function renderCard(item, options) {
    const opts = options || {};
    const pathPrefix = opts.pathPrefix || "";
    const manifestSlugs = opts.manifestSlugs;
    const source = item.source || "";
    const liveFlag = item._live
      ? `<span class="live-flag">LIVE</span>`
      : "";
    const author = item.author || "";
    const titleText = item.title || "(no title)";
    const excerpt = item.body_excerpt
      ? `<div class="excerpt">${escapeHtml(item.body_excerpt)}</div>`
      : "";
    const bylineIcon = bylineIconHtml(item);

    let topRowHtml = "";
    let bodyHtml = "";
    if (source === "bluesky") {
      // Fix B: Bluesky's body reads like a tweet — `Author Name: post
      // body` as one inline block, avatar bigger and to the left of
      // the body. The meta row keeps just the source + LIVE + time;
      // the author moves into the body byline.
      //
      // Use record.text for the body if present; the "title" field is
      // just the first line truncated. Linkify URLs + @mentions.
      const bodyText = item.text || item.title || "";
      // Fix 3: explicit attribution link in the meta row of every
      // Bluesky card. Same target as the timestamp (the post URL) but
      // with literal "View on Bluesky →" text so attribution is
      // unambiguous and discoverable. Other sources don't need the
      // equivalent because their headline IS the click-through.
      // Wrap the timestamp + View-on-Bluesky in a flex-end group so
      // they stay together. Previous attempt used `margin-left: auto`
      // on the timestamp alone, which pushed the timestamp to the
      // right but left View-on-Bluesky orphaned on a wrapped second
      // line (or hidden off the right edge on narrow viewports).
      const viewOnBluesky = item.url
        ? `<a class="view-on-source" href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">View on Bluesky →</a>`
        : "";
      topRowHtml = `
        <div class="top">
          ${sourceBadgeHtml(source)}
          ${liveFlag}
          <span class="meta-right">
            ${timestampHtml(item)}
            ${viewOnBluesky}
          </span>
        </div>
      `;
      // Fix A2: avatar + author both link to bsky.app/profile/{handle}.
      // The handle is captured separately on live items; for archive
      // items we extract it from the public bsky.app URL.
      const bskyHandle = item.author_handle || _handleFromBskyUrl(item.url);
      const profileUrl = bskyHandle
        ? `https://bsky.app/profile/${bskyHandle}`
        : (item.url || "#");
      // Fix 4: archive items don't carry author_avatar. Fall back to
      // the handle-keyed avatar cache populated by bskyLiveItems on
      // this pageload. The cache is empty on first render and fills
      // during liveMerge; entity.js re-renders afterwards so the
      // archive cards pick up the avatars then.
      const avatarUrl = item.author_avatar || _lookupBskyAvatar(bskyHandle);
      _dbg("render:bsky-card", {
        id: item.id,
        live: !!item._live,
        author_avatar_on_item: !!item.author_avatar,
        bskyHandle,
        resolved_avatar: avatarUrl,
      });
      const avatarImg = avatarUrl
        ? `<img class="bsky-avatar" src="${escapeHtml(avatarUrl)}" alt="" loading="lazy" onerror="this.style.display='none'">`
        : `<span class="bsky-avatar bsky-avatar-placeholder" aria-hidden="true"></span>`;
      const linkedText = renderBlueskyRichText(bodyText, item.facets);
      bodyHtml = `
        <div class="bsky-body">
          <a class="bsky-avatar-link" href="${escapeHtml(profileUrl)}" target="_blank" rel="noopener noreferrer">${avatarImg}</a>
          <div class="bsky-body-text">
            <a class="bsky-author" href="${escapeHtml(profileUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(author)}</a><span class="bsky-author-sep">:</span>
            <span class="bsky-text">${linkedText}</span>
          </div>
        </div>
        ${renderBlueskyMedia(item)}
        ${renderBlueskyImagesLive(item)}
        ${renderBlueskyVideoLive(item)}
        ${renderBlueskyLinkCard(item)}
        ${renderQuotedPost(item.quotedPost)}
      `;
    } else if (source === "youtube") {
      // Fix 1 (polish-5): wrap the timestamp in .meta-right so it
      // anchors to the right edge consistently across all card types.
      topRowHtml = `
        <div class="top">
          ${sourceBadgeHtml(source)}
          ${liveFlag}
          ${bylineIcon}
          ${outletAuthorHtml(item)}
          <span class="meta-right">${timestampHtml(item)}</span>
        </div>
      `;
      bodyHtml = `
        <div class="title"><a href="${escapeHtml(item.url || "#")}" target="_blank" rel="noopener noreferrer">${escapeHtml(titleText)}</a></div>
        ${renderYoutubeEmbed(item, withinMediaWindow(item.published_at))}
        ${excerpt}
      `;
    } else {
      // Reddit, Google News, Substack — link-out headline + small thumb.
      // Fix 1 (polish-5): same meta-right wrapper as YouTube/Bluesky.
      topRowHtml = `
        <div class="top">
          ${sourceBadgeHtml(source)}
          ${liveFlag}
          ${bylineIcon}
          ${outletAuthorHtml(item)}
          <span class="meta-right">${timestampHtml(item)}</span>
        </div>
      `;
      // Cluster C: visual avatar in the body — prefer tagged player's
      // headshot, then tagged team's ESPN logo, then initials. Returns
      // empty string when the card already has a thumbnail.
      const visual = visualAvatarHtml(item);
      bodyHtml = visual
        ? `
          <div class="card-body-with-avatar">
            ${visual}
            <div class="card-body-main">
              <div class="title"><a href="${escapeHtml(item.url || "#")}" target="_blank" rel="noopener noreferrer">${escapeHtml(titleText)}</a></div>
              ${excerpt}
            </div>
          </div>
        `
        : `
          <div class="title"><a href="${escapeHtml(item.url || "#")}" target="_blank" rel="noopener noreferrer">${escapeHtml(titleText)}</a></div>
          ${renderSmallThumb(item)}
          ${excerpt}
        `;
    }

    const card = document.createElement("article");
    card.className = "card card-" + source;
    card.dataset.source = source;
    card.dataset.id = item.id;
    card.innerHTML = `
      ${topRowHtml}
      ${bodyHtml}
      ${manifestSlugs ? entityTagsHtml(item, pathPrefix, manifestSlugs) : ""}
    `;

    // Wire the YouTube play button to swap thumb -> iframe.
    const playBtn = card.querySelector(".yt-play");
    if (playBtn) {
      playBtn.addEventListener("click", (e) => {
        e.preventDefault();
        const wrap = playBtn.closest(".yt-embed");
        const vid = wrap.dataset.vid;
        wrap.innerHTML =
          `<iframe src="https://www.youtube.com/embed/${encodeURIComponent(vid)}?autoplay=1&rel=0" allow="accelerometer; autoplay; encrypted-media; gyroscope; picture-in-picture" allowfullscreen loading="lazy"></iframe>`;
      });
    }
    // Fix A1: source badge filters the feed to that source on click.
    const badge = card.querySelector(".src-badge[data-clickable]");
    if (badge) {
      badge.addEventListener("click", (e) => {
        e.preventDefault();
        if (window.NCS && typeof window.NCS._setSourceOnly === "function") {
          window.NCS._setSourceOnly(badge.dataset.source);
        }
      });
    }
    return card;
  }

  // Fix A1: badge is a button-styled span that calls _setSourceOnly.
  function sourceBadgeHtml(source) {
    return `<span class="src-badge src-${escapeHtml(source)}" data-clickable data-source="${escapeHtml(source)}" role="button" tabindex="0" title="Filter feed to ${escapeHtml(source)} only"><span class="dot"></span>${escapeHtml(source)}</span>`;
  }

  // Fix A3: outlet/channel name links to its homepage when derivable.
  function outletAuthorHtml(item) {
    const author = item.author || "";
    if (!author) return "";
    const url = _outletHomepageUrl(item);
    if (!url) return `<span class="author">${escapeHtml(author)}</span>`;
    return `<a class="author" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" title="Open ${escapeHtml(_itemHostname(item))}">${escapeHtml(author)}</a>`;
  }

  // Fix A4: timestamp links to the original post/article.
  function timestampHtml(item) {
    const when = relativeTime(item.published_at);
    if (!item.url) {
      return `<span class="when">${escapeHtml(when)}</span>`;
    }
    return `<a class="when" href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer" title="Open original">${escapeHtml(when)}</a>`;
  }

  // ---------------------------------------------------------------------
  // Manifest helpers
  // ---------------------------------------------------------------------

  function manifestSlugSets(manifest) {
    return {
      players: new Set(manifest.players.map((p) => p.slug)),
      teams: new Set(manifest.teams.map((t) => t.slug)),
      playerName: Object.fromEntries(manifest.players.map((p) => [p.slug, p.name])),
      teamName: Object.fromEntries(manifest.teams.map((t) => [t.slug, t.name])),
    };
  }

  // ---------------------------------------------------------------------
  // Search / autocomplete
  // ---------------------------------------------------------------------

  function attachSearch(inputEl, suggestEl, manifest, pathPrefix) {
    pathPrefix = pathPrefix || "";
    const all = []
      .concat(
        manifest.players.map((p) => ({ ...p, kind: "Player", path: `${pathPrefix}players/${p.slug}.html` }))
      )
      .concat(
        manifest.teams.map((t) => ({ ...t, kind: "Team", path: `${pathPrefix}teams/${t.slug}.html` }))
      );

    function render(q) {
      const ql = q.toLowerCase().trim();
      if (!ql) {
        suggestEl.classList.remove("open");
        suggestEl.innerHTML = "";
        return;
      }
      const hits = all
        .filter((e) => e.name.toLowerCase().includes(ql) || e.slug.includes(ql))
        .slice(0, 10);
      if (!hits.length) {
        suggestEl.classList.remove("open");
        suggestEl.innerHTML = "";
        return;
      }
      suggestEl.innerHTML = hits
        .map(
          (e) =>
            `<a href="${escapeHtml(e.path)}"><span><span class="kind">${escapeHtml(e.kind)}</span> ${escapeHtml(e.name)}</span><span class="count">${e.count}</span></a>`
        )
        .join("");
      suggestEl.classList.add("open");
    }

    inputEl.addEventListener("input", (e) => render(e.target.value));
    inputEl.addEventListener("focus", (e) => render(e.target.value));
    inputEl.addEventListener("blur", () => {
      // Delay so the click on a suggestion lands first.
      setTimeout(() => suggestEl.classList.remove("open"), 150);
    });
  }

  // ---------------------------------------------------------------------
  // Source-filter pills
  // ---------------------------------------------------------------------

  function attachSourcePills(containerEl, onChange) {
    const sources = ["bluesky", "google-news", "reddit", "substack", "youtube"];
    const state = new Set(sources); // start: all on
    const allPill = document.createElement("span");
    allPill.className = "pill on";
    allPill.dataset.kind = "all";
    allPill.textContent = "All";
    allPill.title = "Show every source";
    containerEl.appendChild(allPill);

    const pills = {};
    for (const s of sources) {
      const el = document.createElement("span");
      el.className = "pill on";
      el.dataset.kind = s;
      el.innerHTML = `<span class="dot" style="color:var(--src-${s})"></span>${s}`;
      // Polish-8 (Fix 2): click = make this the only active source.
      // Click the same pill again to return to All. Ctrl/Cmd+Click
      // still combines for power users.
      el.title = "Click: show only this source · Ctrl/Cmd+Click: toggle this source in the current set";
      containerEl.appendChild(el);
      pills[s] = el;
    }

    function sync() {
      for (const s of sources) pills[s].classList.toggle("on", state.has(s));
      const allOn = state.size === sources.length;
      allPill.classList.toggle("on", allOn);
      // Polish-8: mark exactly one source as "solo" so the active-source
      // pill stands out from the muted greyed-out others. The "on" class
      // alone wasn't strong enough to read at a glance when only one was
      // selected.
      const solo = state.size === 1;
      for (const s of sources) pills[s].classList.toggle("solo", solo && state.has(s));
      onChange(state);
    }

    // Fix A1: expose a single-source setter so a card's source badge can
    // narrow the filter to that source on click. Mirrors the effect of
    // clicking the source pill, but as a programmatic call. Multiple
    // attached pill-sets are not expected on one page; if they were,
    // the last one wins, which is fine.
    window.NCS = window.NCS || {};
    window.NCS._setSourceOnly = function (source) {
      state.clear();
      state.add(source);
      sync();
      // Scroll the pill bar into view so the user can see what changed.
      const el = pills[source];
      if (el && el.scrollIntoView) {
        el.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "center" });
      }
    };

    allPill.addEventListener("click", () => {
      // "All" always sets the state to every source on; if everything
      // is already on, do nothing (instead of clearing — that left the
      // user with an empty feed and no obvious way to recover).
      sources.forEach((s) => state.add(s));
      sync();
    });
    for (const s of sources) {
      pills[s].addEventListener("click", (e) => {
        // Polish-8 (Fix 2): single-click selects this source as the
        // only active filter. Click the same active solo pill again
        // and we go back to All. Ctrl/Cmd+Click preserves the old
        // multi-select toggle behavior for users combining sources.
        if (e.ctrlKey || e.metaKey) {
          if (state.has(s)) state.delete(s);
          else state.add(s);
          // Empty state is meaningless (shows no items); if the
          // power user toggled the last source off, snap back to All
          // instead of leaving them stranded.
          if (state.size === 0) sources.forEach((x) => state.add(x));
        } else {
          const isSolo = state.size === 1 && state.has(s);
          if (isSolo) {
            // Already the only one on — toggle back to All.
            sources.forEach((x) => state.add(x));
          } else {
            // Solo this source.
            state.clear();
            state.add(s);
          }
        }
        sync();
      });
    }
    onChange(state);
  }

  // ---------------------------------------------------------------------
  // Live merge
  // ---------------------------------------------------------------------

  function _corsProxyFetch(url) {
    if (!C.CORS_PROXY_URL) {
      return Promise.reject(new Error("CORS proxy not configured"));
    }
    return fetch(C.CORS_PROXY_URL + "?url=" + encodeURIComponent(url));
  }

  // Paced batch helper. Splits `items` into chunks of `batchSize`, runs
  // `fetchFn(item)` in parallel within a chunk, then waits `betweenMs`
  // before starting the next chunk. Individual failures are swallowed
  // (returned as null and filtered out) so one slow/broken handle or
  // feed doesn't poison the whole batch.
  //
  // Why pacing matters even though JS Promises are concurrent: firing
  // 164 fetches at once forces the OS DNS resolver to look up
  // public.api.bsky.app 164 times in parallel; on home connections
  // this overruns the resolver cache and most lookups fail with
  // net::ERR_NAME_NOT_RESOLVED. Likewise, 19 simultaneous Substack
  // feed fetches through the Worker trip 429s either at CF or at
  // Substack. Pacing chunks ~100-500ms apart keeps DNS warm and
  // request rates under the rate limiters.
  //
  // Polish-10 (Fix 1): optional `onProgress` callback invoked after
  // every chunk with {done, total, succeeded, failed, chunkIndex,
  // totalChunks}. Used by the live-status badge to display percent
  // progress so a 3-5s paced fetch feels like progress instead of a
  // hang. Stays optional — every existing call site that doesn't
  // pass it gets identical behavior.
  async function pacedBatchFetch(items, batchSize, betweenMs, fetchFn, traceLabel, onProgress) {
    const out = [];
    let succeeded = 0;
    let failed = 0;
    const totalChunks = Math.ceil(items.length / batchSize);
    for (let i = 0; i < items.length; i += batchSize) {
      const batch = items.slice(i, i + batchSize);
      const results = await Promise.all(
        batch.map((item) =>
          Promise.resolve()
            .then(() => fetchFn(item))
            .catch(() => null)
        )
      );
      for (const r of results) {
        if (r === null || r === undefined) failed++;
        else {
          succeeded++;
          out.push(r);
        }
      }
      const chunkIndex = Math.floor(i / batchSize) + 1;
      const done = Math.min(i + batchSize, items.length);
      if (traceLabel && window.NCS_DEBUG) {
        console.debug(traceLabel, {
          chunk: chunkIndex,
          totalChunks,
          sent: done,
          total: items.length,
          succeeded,
          failed,
        });
      }
      if (typeof onProgress === "function") {
        try {
          onProgress({
            done,
            total: items.length,
            succeeded,
            failed,
            chunkIndex,
            totalChunks,
          });
        } catch (e) {
          // Progress callbacks must never break the batch.
          if (window.NCS_DEBUG) console.warn("pacedBatchFetch onProgress threw:", e);
        }
      }
      if (i + batchSize < items.length) {
        await new Promise((r) => setTimeout(r, betweenMs));
      }
    }
    return out;
  }

  // --- Bluesky (handles same-origin, AppView direct) ---
  async function fetchBlueskyHandles(maxHandles) {
    // Loaded same-origin from the committed snapshot at
    // data/sources/bluesky_handles.csv. No CORS, no Worker, no
    // HuggingFace dependency. This is exactly the file format the
    // poll_bluesky.py poller consumes server-side.
    //
    // `maxHandles` is a soft cap; passing Infinity (or omitting it)
    // returns every row.
    const cap = maxHandles == null ? Infinity : maxHandles;
    try {
      const resp = await fetch(window.NCS_dataUrl(C.BLUESKY_HANDLES_URL));
      if (!resp.ok) return [];
      const text = await resp.text();
      const lines = text.trim().split("\n");
      const handles = [];
      // Skip header. We only read the first column, so display names
      // with quoted commas in column 2 don't affect parsing.
      for (let i = 1; i < lines.length && handles.length < cap; i++) {
        const first = lines[i].split(",")[0].trim();
        if (first) handles.push(first);
      }
      return handles;
    } catch {
      return [];
    }
  }

  async function fetchBlueskyAuthor(actor, limit) {
    const url =
      `${C.BLUESKY_APPVIEW_BASE}/xrpc/app.bsky.feed.getAuthorFeed` +
      `?actor=${encodeURIComponent(actor)}&filter=posts_no_replies&limit=${limit}`;
    try {
      const resp = await fetch(url);
      if (!resp.ok) return [];
      const j = await resp.json();
      return j.feed || [];
    } catch {
      return [];
    }
  }

  function _atUriToId(uri) {
    const path = uri && uri.startsWith("at://") ? uri.slice(5) : uri || "";
    return "bs-" + encodeURIComponent(path);
  }

  // Build a public bsky.app URL for an AT-URI quoted record. The URI
  // shape is at://<did>/app.bsky.feed.post/<rkey>; the public viewer
  // accepts either the did or the handle as the profile segment.
  function _quotedPostUrl(quotedUri, handleFallback) {
    if (!quotedUri || !quotedUri.startsWith("at://")) return "";
    const rest = quotedUri.slice(5); // strip at://
    const parts = rest.split("/");
    if (parts.length < 3) return "";
    const did = parts[0];
    const rkey = parts[parts.length - 1];
    const profile = handleFallback || did;
    return `https://bsky.app/profile/${profile}/post/${rkey}`;
  }

  // Fix B1: extract the quoted post payload from a record#view, in
  // either its standalone or recordWithMedia form. Returns a normalized
  // object the renderer consumes, or a {missing: true} sentinel for the
  // viewNotFound/viewBlocked variants so the renderer can show a
  // placeholder instead of crashing.
  //
  // Polish-9 (Fix 1): extracts FULL nested embeds — images, link card,
  // AND video — plus the quoted record's facets so the renderer can
  // build canonical @mention links the same way as the outer post.
  // Previously video embeds inside a quote were dropped (only images
  // and link cards were captured), and the text was linkified without
  // facets so canonical did→profile links were lost.
  function _extractQuotedPost(recordView) {
    if (!recordView) return null;
    const t = recordView.$type || "";
    if (t.includes("viewNotFound") || t.includes("viewBlocked") || t.includes("viewDetached")) {
      _dbg("bsky:quote:missing", { type: t });
      return { missing: true };
    }
    const author = recordView.author || {};
    const value = recordView.value || {};
    const text = value.text || "";
    const facets = value.facets || null;
    // Quoted-post embeds are nested in recordView.embeds (an array of
    // view objects). Walk it for images / link card / video.
    let images = null;
    let linkCard = null;
    let video = null;
    for (const e of recordView.embeds || []) {
      const et = e.$type || "";
      if (!images && et.includes("app.bsky.embed.images")) {
        images = (e.images || [])
          .map((im) => ({ url: im.fullsize || im.thumb || "", alt: im.alt || "" }))
          .filter((im) => im.url);
      } else if (!linkCard && et.includes("app.bsky.embed.external")) {
        const ext = e.external || {};
        if (ext.uri) {
          linkCard = {
            uri: ext.uri,
            title: ext.title || "",
            description: ext.description || "",
            thumb: ext.thumb || null,
          };
        }
      } else if (!video && et.includes("app.bsky.embed.video")) {
        // Same video#view shape as a top-level embed (cid, playlist,
        // thumbnail, aspectRatio). Render thumbnail-only; click-through
        // opens the post on bsky.app where their native player runs.
        if (e.thumbnail || e.playlist) {
          video = {
            thumbnail: e.thumbnail || null,
            playlist: e.playlist || null,
            aspectRatio: e.aspectRatio || null,
            alt: e.alt || "",
          };
        }
      }
    }
    _dbg("bsky:quote:extracted", {
      handle: author.handle,
      text_len: text.length,
      has_facets: !!facets,
      images: images ? images.length : 0,
      has_linkCard: !!linkCard,
      has_video: !!video,
    });
    return {
      author: author.displayName || author.handle || "",
      author_handle: author.handle || "",
      author_avatar: author.avatar || null,
      text: text,
      facets: facets,
      timestamp: value.createdAt || recordView.indexedAt || null,
      url: _quotedPostUrl(recordView.uri, author.handle),
      images: images,
      linkCard: linkCard,
      video: video,
    };
  }

  async function bskyLiveItems(maxPosts, opts) {
    const tStart = Date.now();
    _dbg("bsky:start", { time: tStart });
    const out = [];
    // Poll EVERY handle in the committed CSV. The CSV is sorted
    // alphabetically (not by activity), so any sub-sample biases the
    // live feed to whatever reporters happen to be near the top of
    // the alphabet rather than whoever just posted.
    //
    // Chunk size 20 keeps the local DNS resolver from being asked
    // to look up public.api.bsky.app 164 times in the same tick —
    // that overran the resolver on home networks and most fetches
    // failed with net::ERR_NAME_NOT_RESOLVED (PR #21 bug). 20 stays.
    //
    // Polish-10 (Fix 1): between_ms 250 → 100. Diagnostic traces
    // showed 100% handle success rate, so the conservative 250ms gap
    // was idle time we didn't need. Halving it cuts ~1s off the
    // total. perHandle bumped 5 → 3 (revert PR #24): smaller payload
    // = faster parse + smaller network footprint, and 3 posts per
    // reporter × 164 handles × the recent-activity filter is plenty
    // of recency. Combined: ~10s → ~3-5s typical wall time.
    const handles = await fetchBlueskyHandles();
    const perHandle = 3;
    const chunkSize = 20;
    const betweenMs = 100;
    console.debug("[NCS-BSKY-LIVE] start", {
      handles_total: handles.length,
      per_handle: perHandle,
      chunk_size: chunkSize,
      between_ms: betweenMs,
    });
    const onProgress = opts && typeof opts.onProgress === "function"
      ? opts.onProgress
      : null;
    const feeds = await pacedBatchFetch(
      handles,
      chunkSize,
      betweenMs,
      (h) => fetchBlueskyAuthor(h, perHandle),
      "[NCS-BLUESKY-BATCH]",
      onProgress
    );
    const tagger = window.NCS_Tagger;
    await tagger.ready();
    // Fix 2 (avatar coverage): walk every fetched feed BEFORE the
    // filter loop and pre-populate the handle→avatar cache from each
    // post's author. Previously the cache only wrote inside the
    // filter loop (after the reply/repost gate), so reporters whose
    // recent posts were all replies / reposts never reached the
    // cache-write line — and entity pages, which show mostly archive
    // items keyed by handle, saw initials avatars instead of real
    // headshots. Pre-population guarantees every reporter with any
    // recent post in their feed gets cached, even if no post passes
    // the filter for inclusion in LIVE_ITEMS.
    for (const feed of feeds) {
      for (const fv of feed) {
        const a = fv && fv.post && fv.post.author;
        if (a && a.handle && a.avatar) _cacheBskyAvatar(a.handle, a.avatar);
      }
    }
    for (const feed of feeds) {
      for (const fv of feed) {
        const post = fv.post;
        if (!post) continue;
        const reason = fv.reason;
        if (reason && reason.$type && reason.$type.includes("reasonRepost")) continue;
        const record = post.record || {};
        if (record.reply) continue;
        const author = post.author || {};
        const handle = author.handle || "";
        const rkey = (post.uri || "").split("/").pop();
        const text = record.text || "";
        // Tag ONLY the post text. Embed metadata (link card title,
        // description, image alt text) describes the LINKED article,
        // not the poster's own words — concatenating it would cause
        // false attributions (e.g. a Pelicans article linked from a
        // post about Game 7 would falsely tag the post as being
        // "about" the Pelicans). The post text is authoritative.
        const tags = tagger.detectEntitiesSync(text);

        // Bluesky embed shapes from the public AppView. We render:
        //   app.bsky.embed.images#view             -> grid of poster's images
        //   app.bsky.embed.external#view           -> link-card preview
        //   app.bsky.embed.record#view             -> quote-post (no media)
        //   app.bsky.embed.recordWithMedia#view    -> quote + media
        //   app.bsky.embed.record#viewNotFound     -> deleted/blocked quoted
        // For recordWithMedia: media lives in embed.media; the quoted
        // record lives in embed.record.record.
        const embed = post.embed || {};
        const embedType = embed.$type || "";
        const isRecordWithMedia = embedType.includes("recordWithMedia");
        const mediaView = isRecordWithMedia ? (embed.media || {}) : embed;
        const mediaViewType = mediaView.$type || "";
        let images = null;
        let linkCard = null;
        let video = null;
        if (mediaViewType.includes("app.bsky.embed.images")) {
          images = (mediaView.images || [])
            .map((im) => ({
              url: im.fullsize || im.thumb || "",
              alt: im.alt || "",
            }))
            .filter((im) => im.url);
        } else if (mediaViewType.includes("app.bsky.embed.external")) {
          const ext = mediaView.external || {};
          if (ext.uri) {
            linkCard = {
              uri: ext.uri,
              title: ext.title || "",
              description: ext.description || "",
              thumb: ext.thumb || null,
            };
          }
        } else if (mediaViewType.includes("app.bsky.embed.video")) {
          // Fix 1: Bluesky native video. The AppView's video#view shape:
          //   { cid, playlist (HLS m3u8), thumbnail, aspectRatio, alt? }
          // We capture the thumbnail + aspect-ratio for rendering, and
          // the playlist URL strictly for reference — we do NOT play
          // HLS inline (no hls.js dependency). Click-through to bsky.app
          // is the legal/sanctioned path; Bluesky's own player handles
          // playback.
          if (mediaView.thumbnail || mediaView.playlist) {
            video = {
              thumbnail: mediaView.thumbnail || null,
              playlist: mediaView.playlist || null,
              aspectRatio: mediaView.aspectRatio || null,
              alt: mediaView.alt || "",
            };
          }
        }

        // Fix B1: quote post. Either a record-only embed (the post is
        // just a quote with no media) or recordWithMedia (quote + media).
        // The shape changes slightly between the two: in record#view
        // the quoted record is in embed.record; in recordWithMedia#view
        // it's in embed.record.record.
        let quotedPost = null;
        const recordView = isRecordWithMedia
          ? (embed.record && embed.record.record) || null
          : (embedType.includes("app.bsky.embed.record") ? embed.record : null);
        if (recordView) {
          quotedPost = _extractQuotedPost(recordView);
        }

        out.push({
          id: _atUriToId(post.uri),
          source: "bluesky",
          published_at: record.createdAt || post.indexedAt,
          title: text.split("\n")[0].slice(0, 280) || "(no text)",
          // Full post text for the body, regardless of length. Bluesky
          // posts max out at 300 graphemes anyway.
          text: text,
          // Fix B2: facets carry the canonical did for each @mention so
          // we can build https://bsky.app/profile/<did> links without
          // guessing. Falls back to regex linkify if facets is absent.
          facets: record.facets || null,
          url: `https://bsky.app/profile/${handle}/post/${rkey}`,
          author: author.displayName || handle,
          // Fix A2: store the actual handle separately from the display
          // name. The body byline links to bsky.app/profile/{handle},
          // and the display name can be anything.
          author_handle: handle,
          // The AppView exposes a CDN URL at author.avatar. Captured
          // here so renderCard's bylineIconHtml shows a small circular
          // avatar next to the byline. Archive items don't carry this
          // yet — future enhancement: extend poll_bluesky.py to store
          // author.avatar so archived Bluesky cards also get the
          // avatar. For now, live cards get avatars, archive cards
          // fall back to the text byline.
          author_avatar: author.avatar || null,
          images: images,
          linkCard: linkCard,
          video: video,
          quotedPost: quotedPost,
          thumbnail: null,
          body_excerpt: text.length > 80 ? text : null,
          players: tags.players,
          teams: tags.teams,
          _live: true,
        });
        // Fix 4: stash this reporter's avatar in the cache so any
        // ARCHIVE Bluesky cards from the same handle (which lack
        // author_avatar) can fall back to it during renderCard.
        _cacheBskyAvatar(handle, author.avatar);
      }
    }
    // Sort newest-first and cap. `maxPosts` is now a soft ceiling on
    // the final pool rather than a per-handle budget; the cap protects
    // against an edge case where every reporter posted a lot at once.
    const final = (maxPosts && out.length > maxPosts)
      ? (out.sort((a, b) => (b.published_at || "").localeCompare(a.published_at || "")),
         out.slice(0, maxPosts))
      : out;
    const elapsed = Date.now() - tStart;
    // Polish-9 (Fix 2): always-on summary trace so a single line in
    // the console answers "did live-fetch work, how long, how many?"
    // — useful for diagnosing the user-perceived "fresh content
    // doesn't load right away" report without enabling NCS_DEBUG.
    console.debug("[NCS-BSKY-LIVE] done", {
      handles_polled: handles.length,
      handles_succeeded: feeds.length,
      handles_failed: handles.length - feeds.length,
      items_returned: final.length,
      cached_handles: window.NCS_AvatarCache.size,
      elapsed_ms: elapsed,
      capped: !!(maxPosts && out.length > maxPosts),
    });
    _dbg("bsky:done", {
      produced: final.length,
      cached_handles: window.NCS_AvatarCache.size,
    });
    return final;
  }

  // --- Reddit (via CORS proxy) ---
  function _parseAtomFeed(xmlText) {
    const doc = new DOMParser().parseFromString(xmlText, "application/xml");
    const entries = doc.getElementsByTagNameNS("http://www.w3.org/2005/Atom", "entry");
    const out = [];
    for (const e of entries) {
      const title = e.getElementsByTagNameNS("http://www.w3.org/2005/Atom", "title")[0]?.textContent || "";
      const id = e.getElementsByTagNameNS("http://www.w3.org/2005/Atom", "id")[0]?.textContent || "";
      const linkEl = e.getElementsByTagNameNS("http://www.w3.org/2005/Atom", "link")[0];
      const link = linkEl?.getAttribute("href") || "";
      const published = e.getElementsByTagNameNS("http://www.w3.org/2005/Atom", "published")[0]?.textContent || "";
      const authorName =
        e.getElementsByTagNameNS("http://www.w3.org/2005/Atom", "author")[0]?.getElementsByTagNameNS("http://www.w3.org/2005/Atom", "name")[0]?.textContent || "";
      const content =
        e.getElementsByTagNameNS("http://www.w3.org/2005/Atom", "content")[0]?.textContent || "";
      out.push({ title, id, link, published, author: authorName, content });
    }
    return out;
  }

  // Mirrors poll_reddit.extract_selftext + cap_excerpt: only the OP's
  // selftext between Reddit's <!-- SC_OFF --> ... <!-- SC_ON --> markers,
  // HTML-stripped, capped at 280 chars at a word boundary. Link posts
  // (no SC markers) get null and the card omits body_excerpt entirely.
  // Without this, the raw <content> from Atom would carry the full post
  // body, comment links, score tables, and "submitted by" boilerplate.
  const _REDDIT_SC_RE = /<!--\s*SC_OFF\s*-->([\s\S]*?)<!--\s*SC_ON\s*-->/;
  const _HTML_TAG_RE = /<[^>]+>/g;
  const _WS_RE_JS = /\s+/g;
  const REDDIT_EXCERPT_MAX = 280;

  function _decodeEntities(s) {
    // The DOMParser already decoded most things, but Reddit's nested
    // content was HTML-encoded inside the Atom <content> CDATA. Decode
    // by routing through a textarea, which is the canonical no-lib way.
    const ta = document.createElement("textarea");
    ta.innerHTML = s;
    return ta.value;
  }

  function _redditExcerpt(contentHtml) {
    if (!contentHtml) return null;
    // Reddit double-encodes the content (entities inside Atom CDATA).
    // Decode once so the SC_OFF marker matches.
    const decoded = _decodeEntities(contentHtml);
    const m = _REDDIT_SC_RE.exec(decoded);
    if (!m) return null;  // link post — no selftext
    const inner = m[1];
    const stripped = _decodeEntities(inner.replace(_HTML_TAG_RE, " "))
      .replace(_WS_RE_JS, " ")
      .trim();
    if (!stripped) return null;
    if (stripped.length <= REDDIT_EXCERPT_MAX) return stripped;
    let cut = stripped.slice(0, REDDIT_EXCERPT_MAX);
    const lastSpace = cut.lastIndexOf(" ");
    if (lastSpace > REDDIT_EXCERPT_MAX * 0.6) cut = cut.slice(0, lastSpace);
    return cut.replace(/\s+$/, "") + "…";
  }

  async function redditLiveItems(maxPosts) {
    try {
      const resp = await _corsProxyFetch("https://www.reddit.com/r/nba/top/.rss?t=day");
      if (!resp.ok) return [];
      const xml = await resp.text();
      const entries = _parseAtomFeed(xml).slice(0, maxPosts || 25);
      const tagger = window.NCS_Tagger;
      await tagger.ready();
      const out = [];
      for (const e of entries) {
        const post_id = e.id.match(/t3_[a-z0-9]+/i)?.[0];
        if (!post_id) continue;
        const handle = e.author.replace(/^\/u\//, "");
        const tags = tagger.detectEntitiesSync(e.title);
        const excerpt = _redditExcerpt(e.content);
        out.push({
          id: `rd-${post_id}`,
          source: "reddit",
          published_at: e.published,
          title: e.title,
          url: e.link, // already a reddit thread URL
          author: handle,
          thumbnail: null,
          body_excerpt: excerpt, // null for link posts; ≤280 for selfposts
          players: tags.players,
          teams: tags.teams,
          _live: true,
        });
      }
      return out;
    } catch {
      return [];
    }
  }

  // --- Google News (via CORS proxy) ---
  function _parseRssFeed(xmlText) {
    const doc = new DOMParser().parseFromString(xmlText, "application/xml");
    const items = doc.getElementsByTagName("item");
    const out = [];
    for (const it of items) {
      const title = it.getElementsByTagName("title")[0]?.textContent || "";
      const link = it.getElementsByTagName("link")[0]?.textContent || "";
      const pubDate = it.getElementsByTagName("pubDate")[0]?.textContent || "";
      const desc = it.getElementsByTagName("description")[0]?.textContent || "";
      const sourceEl = it.getElementsByTagName("source")[0];
      out.push({ title, link, pubDate, description: desc, sourceName: sourceEl?.textContent || "" });
    }
    return out;
  }

  function _splitGNTitle(title) {
    const idx = title.lastIndexOf(" - ");
    if (idx < 0) return [title, ""];
    return [title.slice(0, idx).trim(), title.slice(idx + 3).trim()];
  }

  async function googleNewsLiveItems(maxItems) {
    const queries = ["NBA news", "NBA trade rumors", "NBA injury"];
    const perQuery = Math.max(3, Math.ceil((maxItems || 15) / queries.length));
    const tagger = window.NCS_Tagger;
    await tagger.ready();
    const out = [];
    for (const q of queries) {
      const url = `https://news.google.com/rss/search?q=${encodeURIComponent(q)}&hl=en-US&gl=US&ceid=US:en`;
      try {
        const resp = await _corsProxyFetch(url);
        if (!resp.ok) continue;
        const xml = await resp.text();
        const items = _parseRssFeed(xml).slice(0, perQuery);
        for (const e of items) {
          const [headline, publisher] = _splitGNTitle(e.title);
          if (!headline) continue;
          const tags = tagger.detectEntitiesSync(headline);
          // Stable id: same hash strategy as the server side (sha1 not
          // available client-side without a lib; use a cheap djb2 hash
          // — close enough for cross-cycle dedup on live items).
          let h = 5381;
          const key = headline.toLowerCase() + "|" + (publisher || "").toLowerCase();
          for (let i = 0; i < key.length; i++) h = ((h << 5) + h + key.charCodeAt(i)) >>> 0;
          out.push({
            id: "gn-" + h.toString(16).padStart(8, "0"),
            source: "google-news",
            published_at: e.pubDate,
            title: headline,
            url: e.link, // google redirect; the server tries to extract real, browser can't
            author: publisher || e.sourceName,
            thumbnail: null,
            body_excerpt: null,
            players: tags.players,
            teams: tags.teams,
            _live: true,
          });
        }
      } catch {
        /* keep going */
      }
    }
    return out;
  }

  // --- Substack (via CORS proxy) ---

  const _SUBSTACK_POST_SLUG_RE = /\/p\/([a-z0-9][a-z0-9\-]*)/i;
  const _SUBSTACK_EXCERPT_MAX = 280;

  function _substackPostSlugFromLink(link) {
    if (!link) return null;
    const m = _SUBSTACK_POST_SLUG_RE.exec(link);
    return m ? m[1].toLowerCase() : null;
  }

  function _substackItemId(publicationSlug, link) {
    if (!link) return null;
    const postSlug = _substackPostSlugFromLink(link);
    if (postSlug) return `ss-${publicationSlug}-${postSlug}`;
    // Fallback: djb2 hash of the link (server uses sha1; client can't
    // without a lib, and the collision space is per-publication so
    // 32 bits is plenty for dedup).
    let h = 5381;
    for (let i = 0; i < link.length; i++) h = ((h << 5) + h + link.charCodeAt(i)) >>> 0;
    return `ss-${publicationSlug}-${h.toString(16).padStart(8, "0")}`;
  }

  // Mirrors poll_substack._entry_excerpt: prefer the longer payload
  // (content:encoded -> description), strip HTML, cap at 280 chars at
  // a word boundary with an ellipsis.
  function _substackExcerpt(descriptionHtml) {
    if (!descriptionHtml) return "";
    // Substack RSS encodes the HTML body. DOMParser already gave us
    // the decoded textContent in _parseRssFeed, but `description` is
    // the inner HTML. Strip tags + collapse whitespace + decode entities.
    const stripped = _decodeEntities(descriptionHtml.replace(_HTML_TAG_RE, " "))
      .replace(_WS_RE_JS, " ")
      .trim();
    if (!stripped) return "";
    if (stripped.length <= _SUBSTACK_EXCERPT_MAX) return stripped;
    let cut = stripped.slice(0, _SUBSTACK_EXCERPT_MAX);
    const lastSpace = cut.lastIndexOf(" ");
    if (lastSpace > _SUBSTACK_EXCERPT_MAX * 0.6) cut = cut.slice(0, lastSpace);
    return cut.replace(/\s+$/, "") + "…";
  }

  async function _loadSubstackPublications() {
    try {
      const resp = await fetch(window.NCS_dataUrl("data/sources/substack_publications.json"));
      if (!resp.ok) return [];
      const blob = await resp.json();
      const list = blob.publications || [];
      // Skip _meta-only entries and anything missing slug/feed.
      return list.filter((p) => p && p.slug && p.feed);
    } catch {
      return [];
    }
  }

  async function substackLiveItems(maxItems) {
    if (!C.CORS_PROXY_URL) return [];
    const pubs = await _loadSubstackPublications();
    if (!pubs.length) return [];
    const tagger = window.NCS_Tagger;
    await tagger.ready();
    // 19 simultaneous Worker requests to .substack.com feeds tripped
    // 429 rate limits at either CF or Substack and most feeds came
    // back empty. Chunks of 4 with a 500ms gap keep the burst under
    // the limiters; total wall time at ~5 chunks × 500ms is ~2.5s.
    const fetchOne = async (pub) => {
      const resp = await _corsProxyFetch(pub.feed);
      if (!resp.ok) return [];
      const xml = await resp.text();
      const entries = _parseRssFeed(xml);
      const out = [];
      for (const e of entries) {
        const link = e.link;
        if (!link || !e.title) continue;
        const itemId = _substackItemId(pub.slug, link);
        if (!itemId) continue;
        const excerpt = _substackExcerpt(e.description || "");
        const detectText = excerpt ? `${e.title}\n${excerpt}` : e.title;
        const tags = tagger.detectEntitiesSync(detectText);
        out.push({
          id: itemId,
          source: "substack",
          published_at: e.pubDate,
          title: e.title.trim(),
          url: link,
          author: pub.name,
          thumbnail: null,
          body_excerpt: excerpt || null,
          players: tags.players,
          teams: tags.teams,
          _live: true,
        });
      }
      return out;
    };
    const perPubResults = await pacedBatchFetch(
      pubs,
      4,
      500,
      fetchOne,
      "[NCS-SUBSTACK-BATCH]"
    );
    let out = [];
    for (const items of perPubResults) out.push(...items);
    if (maxItems && out.length > maxItems) {
      out.sort((a, b) => (b.published_at || "").localeCompare(a.published_at || ""));
      out = out.slice(0, maxItems);
    }
    return out;
  }

  // --- Live merge orchestrator ---
  async function liveMerge(opts) {
    if (!C.LIVE_MERGE_ENABLED) return [];
    const limits = C.LIVE_MERGE_LIMITS || {};
    const wanted = opts && opts.sources
      ? opts.sources
      : ["bluesky", "reddit", "google-news", "substack"];
    // Polish-10 (Fix 1): forward an optional per-source progress
    // callback. Currently only Bluesky reports progress (it's the
    // long pole of the live fetch); the other sources finish in
    // sub-second time so percentage updates would just be noise.
    const onBskyProgress = opts && typeof opts.onBskyProgress === "function"
      ? opts.onBskyProgress
      : null;
    const tasks = [];
    if (wanted.indexOf("bluesky") >= 0) {
      tasks.push(bskyLiveItems(limits.bluesky, { onProgress: onBskyProgress }));
    }
    if (wanted.indexOf("reddit") >= 0) tasks.push(redditLiveItems(limits.reddit));
    if (wanted.indexOf("google-news") >= 0) tasks.push(googleNewsLiveItems(limits.googleNews));
    if (wanted.indexOf("substack") >= 0) tasks.push(substackLiveItems(limits.substack));
    const batches = await Promise.allSettled(tasks);
    const out = [];
    for (const r of batches) {
      if (r.status === "fulfilled") out.push(...r.value);
    }
    // Normalize EVERY published_at to the canonical ISO Z form
    // (YYYY-MM-DDTHH:MM:SSZ). Bluesky's record.createdAt has fractional
    // milliseconds like "2026-05-27T14:30:00.123Z"; lexicographic
    // comparison against archive items in "2026-05-27T14:30:00Z" form
    // puts the live item BELOW the archive one (because "." < "Z"),
    // which was the production bug — fresh live items were getting
    // sorted under hours-old archive items. Force one format here so
    // mergeItems' string sort orders them correctly.
    for (const it of out) it.published_at = canonicalIsoZ(it.published_at);
    return out;
  }

  // Single-source-of-truth normalizer used by liveMerge + mergeItems.
  function canonicalIsoZ(value) {
    if (!value) return value;
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(value)) return value;
    const d = new Date(value);
    if (isNaN(d.getTime())) return value;
    return d.toISOString().replace(/\.\d{3}Z$/, "Z");
  }

  // ---------------------------------------------------------------------
  // Merge + dedupe + sort
  // ---------------------------------------------------------------------

  function mergeItems(archiveItems, liveItems) {
    const seen = new Set();
    const out = [];
    // Live first so they win id ties; both arrays' published_at is
    // canonicalized to the same string format before sort so the
    // lexicographic comparison reflects real chronological order.
    for (const it of (liveItems || [])) {
      if (!it || !it.id) continue;
      if (seen.has(it.id)) continue;
      seen.add(it.id);
      out.push({ ...it, published_at: canonicalIsoZ(it.published_at) });
    }
    for (const it of (archiveItems || [])) {
      if (!it || !it.id) continue;
      if (seen.has(it.id)) continue;
      seen.add(it.id);
      out.push({ ...it, published_at: canonicalIsoZ(it.published_at) });
    }
    out.sort((a, b) => (b.published_at || "").localeCompare(a.published_at || ""));
    return out;
  }

  // ---------------------------------------------------------------------
  // Public
  // ---------------------------------------------------------------------

  // Polish-9 (Fix 2): attach an inline live-fetch status badge next
  // to the source pills. Returns a small controller that feed.js and
  // entity.js call begin() / end() / error() around their liveMerge
  // call so the user sees an unmistakable "fresh content on the way"
  // indicator (and a "+N live" confirmation when it lands) instead
  // of wondering whether the page is broken during the ~3-5s paced
  // fetch window. Auto-hides 4s after end() so it doesn't linger.
  //
  // Polish-10 (Fix 1): progress(pct) updates the label with a percent
  // so the badge actively shows the fetch advancing instead of
  // sitting on "fetching live…" the whole time. Callers wire this
  // via the `onBskyProgress` option to liveMerge.
  function attachLiveStatus(containerEl) {
    if (!containerEl) {
      return { begin() {}, progress() {}, end() {}, error() {} };
    }
    const badge = document.createElement("span");
    badge.className = "live-status";
    badge.style.display = "none";
    containerEl.appendChild(badge);
    let fadeTimer = null;
    let inFlight = false;
    function clearFade() {
      if (fadeTimer) { clearTimeout(fadeTimer); fadeTimer = null; }
    }
    function setLoadingLabel(pct) {
      const pctStr = (pct == null || isNaN(pct)) ? "" : ` ${pct}%`;
      badge.innerHTML =
        '<span class="live-status-spinner" aria-hidden="true"></span>' +
        `fetching live…${pctStr}`;
    }
    return {
      begin() {
        clearFade();
        inFlight = true;
        badge.style.display = "";
        badge.className = "live-status loading";
        setLoadingLabel(null);
      },
      progress(pct) {
        if (!inFlight) return;
        const clamped = Math.max(0, Math.min(100, Math.round(pct)));
        setLoadingLabel(clamped);
      },
      end(opts) {
        clearFade();
        inFlight = false;
        const o = opts || {};
        const count = o.count || 0;
        badge.className = "live-status done";
        badge.textContent = count ? `+${count} live` : "live ready";
        fadeTimer = setTimeout(() => { badge.style.display = "none"; }, 4000);
      },
      error() {
        clearFade();
        inFlight = false;
        badge.className = "live-status error";
        badge.textContent = "live unavailable";
        fadeTimer = setTimeout(() => { badge.style.display = "none"; }, 4000);
      },
    };
  }

  window.NCS = Object.assign(window.NCS || {}, {
    relativeTime,
    escapeHtml,
    renderCard,
    manifestSlugSets,
    attachSearch,
    attachSourcePills,
    attachLiveStatus,
    liveMerge,
    mergeItems,
    loadCanonical,
    headshotUrl,
    teamLogoUrl,
    teamConference,
    formatCappedCount,
  });
})();
