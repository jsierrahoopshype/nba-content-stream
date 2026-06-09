/* NBA Content Stream — homepage feed logic.
 *
 * Reads data/index/feed.json + manifest.json + trending.json (instant
 * first paint), then live-merges Bluesky / Reddit / Google News and
 * re-renders.
 */
(function () {
  "use strict";

  const ncs = window.NCS;
  const config = window.NCS_CONFIG;

  const els = {
    summary: document.getElementById("summary"),
    feed: document.getElementById("feed"),
    empty: document.getElementById("empty"),
    pills: document.getElementById("pills"),
    search: document.getElementById("q"),
    suggest: document.getElementById("suggest"),
    trending: document.getElementById("trending"),
    trendingWrap: document.getElementById("trending-wrap"),
  };

  let MANIFEST = null;
  let MANIFEST_SLUGS = null;
  let ARCHIVE_ITEMS = [];
  let LIVE_ITEMS = [];
  let LIVE_STATUS = null;
  let SOURCE_FILTER = new Set([
    "bluesky",
    "google-news",
    "reddit",
    "substack",
    "youtube",
  ]);

  function applyFilter(items) {
    return items.filter((it) => SOURCE_FILTER.has(it.source));
  }

  function render() {
    const merged = ncs.mergeItems(ARCHIVE_ITEMS, LIVE_ITEMS);
    const filtered = applyFilter(merged);
    els.feed.innerHTML = "";
    if (filtered.length === 0) {
      els.empty.style.display = "block";
    } else {
      els.empty.style.display = "none";
      const frag = document.createDocumentFragment();
      // Cap to keep the DOM light; user can scroll, but 500 cards is
      // plenty for one pageview.
      const maxRender = 500;
      filtered.slice(0, maxRender).forEach((it) => {
        frag.appendChild(
          ncs.renderCard(it, {
            pathPrefix: "",
            manifestSlugs: MANIFEST_SLUGS,
          })
        );
      });
      els.feed.appendChild(frag);
    }
    const liveCount = LIVE_ITEMS.length;
    const dot = liveCount ? '<span class="live-dot"></span>' : "";
    els.summary.innerHTML = `${dot}showing <strong>${filtered.length}</strong> of <strong>${merged.length}</strong> items across <strong>${SOURCE_FILTER.size}</strong> source(s)${liveCount ? ` · <strong>${liveCount}</strong> live` : ""}`;
  }

  function renderTrending(trending) {
    if (!trending || !trending.items || !trending.items.length) {
      els.trendingWrap.style.display = "none";
      return;
    }
    els.trendingWrap.style.display = "block";
    els.trending.innerHTML = "";
    const frag = document.createDocumentFragment();
    trending.items.slice(0, 15).forEach((it) => {
      const card = document.createElement("a");
      card.className = "trend-card";
      card.href = it.url || "#";
      card.target = "_blank";
      card.rel = "noopener";
      card.innerHTML = `
        <div class="meta">
          <span class="src-badge src-${ncs.escapeHtml(it.source)}">
            <span class="dot"></span>${ncs.escapeHtml(it.source)}
          </span>
          <span>${ncs.escapeHtml(ncs.relativeTime(it.published_at))}</span>
        </div>
        <div class="title">${ncs.escapeHtml(it.title || "")}</div>
      `;
      frag.appendChild(card);
    });
    els.trending.appendChild(frag);
  }

  // Perf-1: re-render is throttled to one paint per animation frame so
  // streaming live chunks (and the background full-feed swap) don't
  // thrash the DOM.
  let renderQueued = false;
  function scheduleRender() {
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(() => {
      renderQueued = false;
      render();
    });
  }

  async function loadArchive() {
    // Cluster C: preload canonical alongside the index files so
    // renderCard can resolve player headshots + team logos on the very
    // first render. ncs.loadCanonical resolves to window.NCS_Canonical.
    //
    // Perf-1: load the SMALL recent slice (feed-recent.json, ~100 newest
    // items) first and paint immediately, instead of blocking first paint
    // on the full ~850KB feed.json. The full feed loads in the background
    // below for search depth / pagination and re-renders without a jarring
    // jump (same items at top; older items appended beneath).
    const [manifest, recent, trending] = await Promise.all([
      fetch(window.NCS_dataUrl("data/index/manifest.json")).then((r) => r.json()),
      fetch(window.NCS_dataUrl("data/index/feed-recent.json")).then((r) => r.json()).catch(() => null),
      fetch(window.NCS_dataUrl("data/index/trending.json")).then((r) => r.json()).catch(() => null),
      ncs.loadCanonical(),
    ]);
    MANIFEST = manifest;
    MANIFEST_SLUGS = ncs.manifestSlugSets(manifest);
    const recentItems = recent && recent.items ? recent.items : null;
    ncs.attachSearch(els.search, els.suggest, manifest, "");
    ncs.attachSourcePills(els.pills, (state) => {
      SOURCE_FILTER = state;
      render();
    });
    LIVE_STATUS = ncs.attachLiveStatus(els.pills);
    renderTrending(trending);

    if (recentItems) {
      // Fast path: paint the recent slice now, fetch the full feed next.
      ARCHIVE_ITEMS = recentItems;
      render();
      fetch(window.NCS_dataUrl("data/index/feed.json"))
        .then((r) => r.json())
        .then((full) => {
          ARCHIVE_ITEMS = full.items || ARCHIVE_ITEMS;
          render();
        })
        .catch((e) => console.warn("full feed load failed; keeping recent slice:", e));
    } else {
      // Fallback (older build without feed-recent.json): full feed only.
      const full = await fetch(window.NCS_dataUrl("data/index/feed.json")).then((r) => r.json());
      ARCHIVE_ITEMS = full.items || [];
      render();
    }
  }

  // Build a handle→latest-post map from archive Bluesky items so the live
  // Bluesky fetch can poll the most-recently-active reporters first.
  function buildRecency(items) {
    const m = {};
    for (const it of items || []) {
      if (!it || it.source !== "bluesky") continue;
      const h = (it.author_handle || "").toLowerCase();
      if (!h) continue;
      const p = it.published_at || "";
      if (!m[h] || p > m[h]) m[h] = p;
    }
    return m;
  }

  async function loadLive() {
    if (!config.LIVE_MERGE_ENABLED) return;
    if (LIVE_STATUS) LIVE_STATUS.begin();
    // Perf-1: incremental live merge. Each source (and each Bluesky
    // chunk) splices into LIVE_ITEMS and re-renders as it lands, so the
    // freshest reporter posts appear within ~1-2s instead of after the
    // whole ~10s merge. Dedupe by id; mergeItems handles live↔archive.
    const seenLive = new Set();
    const onPartial = (items) => {
      let added = false;
      for (const it of items) {
        if (!it || !it.id || seenLive.has(it.id)) continue;
        seenLive.add(it.id);
        LIVE_ITEMS.push(it);
        added = true;
      }
      if (added) scheduleRender();
    };
    try {
      const finalItems = await ncs.liveMerge({
        // Polish-10 (Fix 1): forward Bluesky's per-chunk progress to the
        // status badge so the user sees the fetch advancing.
        onBskyProgress: (p) => {
          if (!LIVE_STATUS || !p || !p.total) return;
          LIVE_STATUS.progress((p.done / p.total) * 100);
        },
        onPartial: onPartial,
        // Poll the most-recently-active reporters first (freshest first).
        recency: buildRecency(ARCHIVE_ITEMS),
      });
      // Reconcile to the authoritative, de-duped pool.
      const seen = new Set();
      LIVE_ITEMS = [];
      for (const it of finalItems) {
        if (!it || !it.id || seen.has(it.id)) continue;
        seen.add(it.id);
        LIVE_ITEMS.push(it);
      }
      if (LIVE_STATUS) LIVE_STATUS.end({ count: LIVE_ITEMS.length });
    } catch (e) {
      console.warn("live merge failed:", e);
      LIVE_ITEMS = [];
      if (LIVE_STATUS) LIVE_STATUS.error();
    }
    render();
  }

  // First-paint sequence: instant render from archive, then fire live
  // merge in the background.
  loadArchive()
    .then(loadLive)
    .catch((err) => {
      console.error("feed load failed:", err);
      els.empty.style.display = "block";
      els.empty.textContent = "Could not load content. Check the console.";
    });
})();
