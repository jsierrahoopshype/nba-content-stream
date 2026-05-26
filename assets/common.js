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

  function renderCard(item, options) {
    const opts = options || {};
    const pathPrefix = opts.pathPrefix || "";
    const manifestSlugs = opts.manifestSlugs;
    const source = item.source || "";
    const liveFlag = item._live
      ? `<span class="live-flag">LIVE</span>`
      : "";
    const thumb = item.thumbnail
      ? `<img class="thumb" src="${escapeHtml(item.thumbnail)}" alt="" loading="lazy">`
      : "";
    const excerpt = item.body_excerpt
      ? `<div class="excerpt">${escapeHtml(item.body_excerpt)}</div>`
      : "";
    const author = item.author || "";

    const card = document.createElement("article");
    card.className = "card";
    card.dataset.source = source;
    card.dataset.id = item.id;
    card.innerHTML = `
      <div class="top">
        <span class="src-badge src-${escapeHtml(source)}">
          <span class="dot"></span>${escapeHtml(source)}
        </span>
        ${liveFlag}
        <span class="author">${escapeHtml(author)}</span>
        <span class="when">${escapeHtml(relativeTime(item.published_at))}</span>
      </div>
      ${thumb}
      <div class="title"><a href="${escapeHtml(item.url || "#")}" target="_blank" rel="noopener">${escapeHtml(item.title || "(no title)")}</a></div>
      ${excerpt}
      ${manifestSlugs ? entityTagsHtml(item, pathPrefix, manifestSlugs) : ""}
    `;
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

  // --- Bluesky (direct, no proxy needed) ---
  async function fetchBlueskyHandles(maxHandles) {
    // The reporter list CSV needs the CORS proxy because Hugging Face
    // doesn't emit CORS headers either. If the proxy is down or not
    // deployed, we silently skip Bluesky live-merge.
    if (!C.CORS_PROXY_URL) return [];
    try {
      const resp = await _corsProxyFetch(C.BLUESKY_HANDLES_URL);
      if (!resp.ok) return [];
      const text = await resp.text();
      const lines = text.trim().split("\n");
      const handles = [];
      // Skip header.
      for (let i = 1; i < lines.length && handles.length < maxHandles; i++) {
        // Crude CSV — handle is the first field. Display names with
        // quoted commas are fine because we only read column 0.
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
    // Pull a small subset of reporters for the live edge. We don't
    // want to fire 375 author-feed requests on every pageload. Pick
    // the first N handles in the CSV (which is roughly the priority
    // order Jorge curates).
    const handlesLimit = 12; // tuneable; 12 handles * a few posts each
    const handles = await fetchBlueskyHandles(handlesLimit);
    const perHandle = Math.max(2, Math.ceil((maxPosts || 30) / Math.max(1, handles.length)));
    // Fire them in parallel.
    const feeds = await Promise.all(handles.map((h) => fetchBlueskyAuthor(h, perHandle)));
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
        const tags = tagger.detectEntitiesSync(text);
        out.push({
          id: _atUriToId(post.uri),
          source: "bluesky",
          published_at: record.createdAt || post.indexedAt,
          title: text.split("\n")[0].slice(0, 280) || "(no text)",
          url: `https://bsky.app/profile/${handle}/post/${rkey}`,
          author: author.displayName || handle,
          thumbnail: null,
          body_excerpt: text.length > 80 ? text : null,
          players: tags.players,
          teams: tags.teams,
          _live: true,
        });
      }
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
      out.push({ title, id, link, published, author: authorName });
    }
    return out;
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
        out.push({
          id: `rd-${post_id}`,
          source: "reddit",
          published_at: e.published,
          title: e.title,
          url: e.link, // already a reddit thread URL
          author: handle,
          thumbnail: null,
          body_excerpt: null,
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

  // --- Live merge orchestrator ---
  async function liveMerge(opts) {
    if (!C.LIVE_MERGE_ENABLED) return [];
    const limits = C.LIVE_MERGE_LIMITS || {};
    const wanted = opts && opts.sources ? opts.sources : ["bluesky", "reddit", "google-news"];
    const tasks = [];
    if (wanted.indexOf("bluesky") >= 0) tasks.push(bskyLiveItems(limits.bluesky));
    if (wanted.indexOf("reddit") >= 0) tasks.push(redditLiveItems(limits.reddit));
    if (wanted.indexOf("google-news") >= 0) tasks.push(googleNewsLiveItems(limits.googleNews));
    const batches = await Promise.allSettled(tasks);
    const out = [];
    for (const r of batches) {
      if (r.status === "fulfilled") out.push(...r.value);
    }
    // Normalize published_at to ISO Z form best-effort.
    for (const it of out) {
      if (it.published_at && !/Z$/.test(it.published_at)) {
        const d = new Date(it.published_at);
        if (!isNaN(d.getTime())) it.published_at = d.toISOString().replace(/\.\d{3}Z$/, "Z");
      }
    }
    return out;
  }

  // ---------------------------------------------------------------------
  // Merge + dedupe + sort
  // ---------------------------------------------------------------------

  function mergeItems(archiveItems, liveItems) {
    const seen = new Set();
    const out = [];
    for (const it of (liveItems || [])) {
      if (!it || !it.id) continue;
      if (seen.has(it.id)) continue;
      seen.add(it.id);
      out.push(it);
    }
    for (const it of (archiveItems || [])) {
      if (!it || !it.id) continue;
      if (seen.has(it.id)) continue;
      seen.add(it.id);
      out.push(it);
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
