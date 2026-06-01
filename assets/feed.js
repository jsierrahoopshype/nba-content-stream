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

  async function loadArchive() {
    // Cluster C: preload canonical alongside the other index files so
    // renderCard can resolve player headshots + team logos on the
    // very first render. ncs.loadCanonical resolves to the same
    // window.NCS_Canonical that visualAvatarHtml reads.
    const [manifest, feed, trending] = await Promise.all([
      fetch(window.NCS_dataUrl("data/index/manifest.json")).then((r) => r.json()),
      fetch(window.NCS_dataUrl("data/index/feed.json")).then((r) => r.json()),
      fetch(window.NCS_dataUrl("data/index/trending.json")).then((r) => r.json()).catch(() => null),
      ncs.loadCanonical(),
    ]);
    MANIFEST = manifest;
    MANIFEST_SLUGS = ncs.manifestSlugSets(manifest);
    ARCHIVE_ITEMS = feed.items || [];
    ncs.attachSearch(els.search, els.suggest, manifest, "");
    ncs.attachSourcePills(els.pills, (state) => {
      SOURCE_FILTER = state;
      render();
    });
    // Polish-9 (Fix 2): inline live-fetch status badge next to the
    // source pills. begin() runs when liveMerge starts; end() flips
    // to "+N live" once items arrive. User no longer has to guess
    // whether the ~2s paced Bluesky fetch is in flight.
    LIVE_STATUS = ncs.attachLiveStatus(els.pills);
    renderTrending(trending);
    render();
  }

  async function loadLive() {
    if (!config.LIVE_MERGE_ENABLED) return;
    if (LIVE_STATUS) LIVE_STATUS.begin();
    try {
      LIVE_ITEMS = await ncs.liveMerge();
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
