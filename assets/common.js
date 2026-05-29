/* NBA Content Stream — shared rendering + live-merge helpers.
 *
 * Used by feed.js (homepage) and entity.js (per-player / per-team
 * pages). Pure DOM + fetch; no framework.
 */
(function () {
  "use strict";

  const C = window.NCS_CONFIG || {};

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

  // Fix E: linkify bare http(s) URLs in plain text. Conservative regex —
  // must start with http:// or https://. Returns innerHTML-safe markup;
  // callers must NOT also escape the result.
  const _URL_RE = /\bhttps?:\/\/[^\s<>()"']+[^\s<>()"',.;:!?]/g;
  function linkifyEscaped(plain) {
    if (!plain) return "";
    const escaped = escapeHtml(plain);
    return escaped.replace(_URL_RE, (m) =>
      `<a class="inline-url" href="${m}" target="_blank" rel="noopener noreferrer">${m}</a>`
    );
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
      // just the first line truncated. Linkify URLs in the body.
      const bodyText = item.text || item.title || "";
      topRowHtml = `
        <div class="top">
          <span class="src-badge src-${escapeHtml(source)}">
            <span class="dot"></span>${escapeHtml(source)}
          </span>
          ${liveFlag}
          <span class="when">${escapeHtml(relativeTime(item.published_at))}</span>
        </div>
      `;
      const avatarHtml = item.author_avatar
        ? `<img class="bsky-avatar" src="${escapeHtml(item.author_avatar)}" alt="" loading="lazy" onerror="this.style.display='none'">`
        : `<span class="bsky-avatar bsky-avatar-placeholder" aria-hidden="true"></span>`;
      const linkedText = linkifyEscaped(bodyText);
      bodyHtml = `
        <div class="bsky-body">
          ${avatarHtml}
          <div class="bsky-body-text">
            <a class="bsky-author" href="${escapeHtml(item.url || "#")}" target="_blank" rel="noopener">${escapeHtml(author)}</a><span class="bsky-author-sep">:</span>
            <span class="bsky-text">${linkedText}</span>
          </div>
        </div>
        ${renderBlueskyMedia(item)}
        ${renderBlueskyImagesLive(item)}
        ${renderBlueskyLinkCard(item)}
      `;
    } else if (source === "youtube") {
      topRowHtml = `
        <div class="top">
          <span class="src-badge src-${escapeHtml(source)}">
            <span class="dot"></span>${escapeHtml(source)}
          </span>
          ${liveFlag}
          ${bylineIcon}
          <span class="author">${escapeHtml(author)}</span>
          <span class="when">${escapeHtml(relativeTime(item.published_at))}</span>
        </div>
      `;
      bodyHtml = `
        <div class="title"><a href="${escapeHtml(item.url || "#")}" target="_blank" rel="noopener">${escapeHtml(titleText)}</a></div>
        ${renderYoutubeEmbed(item, withinMediaWindow(item.published_at))}
        ${excerpt}
      `;
    } else {
      // Reddit, Google News, Substack — link-out headline + small thumb.
      topRowHtml = `
        <div class="top">
          <span class="src-badge src-${escapeHtml(source)}">
            <span class="dot"></span>${escapeHtml(source)}
          </span>
          ${liveFlag}
          ${bylineIcon}
          <span class="author">${escapeHtml(author)}</span>
          <span class="when">${escapeHtml(relativeTime(item.published_at))}</span>
        </div>
      `;
      bodyHtml = `
        <div class="title"><a href="${escapeHtml(item.url || "#")}" target="_blank" rel="noopener">${escapeHtml(titleText)}</a></div>
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
    return card;
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
    containerEl.appendChild(allPill);

    const pills = {};
    for (const s of sources) {
      const el = document.createElement("span");
      el.className = "pill on";
      el.dataset.kind = s;
      el.innerHTML = `<span class="dot" style="color:var(--src-${s})"></span>${s}`;
      containerEl.appendChild(el);
      pills[s] = el;
    }

    function sync() {
      for (const s of sources) pills[s].classList.toggle("on", state.has(s));
      allPill.classList.toggle("on", state.size === sources.length);
      onChange(state);
    }

    allPill.addEventListener("click", () => {
      if (state.size === sources.length) {
        state.clear();
      } else {
        sources.forEach((s) => state.add(s));
      }
      sync();
    });
    for (const s of sources) {
      pills[s].addEventListener("click", () => {
        if (state.has(s)) state.delete(s);
        else state.add(s);
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
      const resp = await fetch(C.BLUESKY_HANDLES_URL);
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

  async function bskyLiveItems(maxPosts) {
    const out = [];
    // Poll EVERY handle in the committed CSV. The CSV is sorted
    // alphabetically (not by activity), so any sub-sample biases the
    // live feed to whatever reporters happen to be near the top of
    // the alphabet rather than whoever just posted. Bluesky's public
    // AppView has no per-IP rate limit at this scale, HTTP/2
    // multiplexes the requests, and at ~165 handles the wall time
    // is a couple of seconds.
    //
    // To stay polite-ish (and avoid creating 165 simultaneous open
    // sockets on slow connections), batch into chunks of 50 and run
    // each batch with Promise.all. The whole run is still well under
    // 5s for the current 164-handle list.
    const handles = await fetchBlueskyHandles();
    const perHandle = 3;  // each reporter contributes up to 3 recent posts
    const feeds = [];
    const BATCH = 50;
    for (let i = 0; i < handles.length; i += BATCH) {
      const slice = handles.slice(i, i + BATCH);
      const batch = await Promise.all(slice.map((h) => fetchBlueskyAuthor(h, perHandle)));
      feeds.push(...batch);
    }
    const tagger = window.NCS_Tagger;
    await tagger.ready();
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
        //   app.bsky.embed.images#view       -> grid of poster's images
        //   app.bsky.embed.external#view     -> link-card preview
        //   app.bsky.embed.recordWithMedia   -> inner .media is one of above
        // Capture whichever is present (the inner shape for
        // recordWithMedia) so renderCard can build the rich preview.
        const embed = post.embed || {};
        const embedType = embed.$type || "";
        const mediaView = embedType.includes("recordWithMedia")
          ? (embed.media || {})
          : embed;
        const mediaViewType = mediaView.$type || "";
        let images = null;
        let linkCard = null;
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
        }

        out.push({
          id: _atUriToId(post.uri),
          source: "bluesky",
          published_at: record.createdAt || post.indexedAt,
          title: text.split("\n")[0].slice(0, 280) || "(no text)",
          // Full post text for the body, regardless of length. Bluesky
          // posts max out at 300 graphemes anyway.
          text: text,
          url: `https://bsky.app/profile/${handle}/post/${rkey}`,
          author: author.displayName || handle,
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
          thumbnail: null,
          body_excerpt: text.length > 80 ? text : null,
          players: tags.players,
          teams: tags.teams,
          _live: true,
        });
      }
    }
    // Sort newest-first and cap. `maxPosts` is now a soft ceiling on
    // the final pool rather than a per-handle budget; the cap protects
    // against an edge case where every reporter posted a lot at once.
    if (maxPosts && out.length > maxPosts) {
      out.sort((a, b) => (b.published_at || "").localeCompare(a.published_at || ""));
      return out.slice(0, maxPosts);
    }
    return out;
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
      const resp = await fetch("data/sources/substack_publications.json");
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
    // Fire publication feeds in parallel — small handful, no need to batch.
    const results = await Promise.allSettled(
      pubs.map(async (pub) => {
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
      })
    );
    let out = [];
    for (const r of results) if (r.status === "fulfilled") out.push(...r.value);
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
    const tasks = [];
    if (wanted.indexOf("bluesky") >= 0) tasks.push(bskyLiveItems(limits.bluesky));
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

  window.NCS = {
    relativeTime,
    escapeHtml,
    renderCard,
    manifestSlugSets,
    attachSearch,
    attachSourcePills,
    liveMerge,
    mergeItems,
  };
})();
