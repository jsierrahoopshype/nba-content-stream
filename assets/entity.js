/* NBA Content Stream — per-entity page logic.
 *
 * Each pre-rendered players/{slug}.html or teams/{slug}.html includes
 * a <meta name="ncs-entity"> tag with `kind` (player|team) and `slug`.
 * This script reads it, fetches data/index/{players|teams}/{slug}.json,
 * renders the cards + mini-chart, then live-merges fresh items filtered
 * to this entity.
 */
(function () {
  "use strict";

  const ncs = window.NCS;
  const config = window.NCS_CONFIG;

  const meta = document.querySelector('meta[name="ncs-entity"]');
  if (!meta) return;
  const KIND = meta.dataset.kind; // "player" or "team"
  const SLUG = meta.dataset.slug;

  const els = {
    summary: document.getElementById("summary"),
    feed: document.getElementById("feed"),
    empty: document.getElementById("empty"),
    chart: document.getElementById("chart"),
    pills: document.getElementById("pills"),
    search: document.getElementById("q"),
    suggest: document.getElementById("suggest"),
  };

  let MANIFEST_SLUGS = null;
  let ARCHIVE_ITEMS = [];
  let LIVE_ITEMS = [];
  let SOURCE_FILTER = new Set([
    "bluesky",
    "google-news",
    "reddit",
    "substack",
    "youtube",
  ]);

  function matchesEntity(item) {
    const arr = item[KIND + "s"] || []; // players / teams
    return arr.indexOf(SLUG) >= 0;
  }

  function applyFilter(items) {
    return items.filter((it) => SOURCE_FILTER.has(it.source));
  }

  function render() {
    const merged = ncs.mergeItems(ARCHIVE_ITEMS, LIVE_ITEMS.filter(matchesEntity));
    const filtered = applyFilter(merged);
    els.feed.innerHTML = "";
    if (filtered.length === 0) {
      els.empty.style.display = "block";
    } else {
      els.empty.style.display = "none";
      const frag = document.createDocumentFragment();
      filtered.slice(0, 500).forEach((it) => {
        frag.appendChild(
          ncs.renderCard(it, {
            pathPrefix: "../",
            manifestSlugs: MANIFEST_SLUGS,
          })
        );
      });
      els.feed.appendChild(frag);
    }
    const liveCount = LIVE_ITEMS.filter(matchesEntity).length;
    const dot = liveCount ? '<span class="live-dot"></span>' : "";
    els.summary.innerHTML = `${dot}showing <strong>${filtered.length}</strong> of <strong>${merged.length}</strong> mentions${liveCount ? ` · <strong>${liveCount}</strong> live` : ""}`;
  }

  // ---------------------------------------------------------------------
  // Mini-chart: bar chart of mentions per day, last 14 days
  // ---------------------------------------------------------------------

  function renderChart(items) {
    if (!els.chart) return;
    const days = 14;
    // Bucket counts by UTC date string.
    const counts = new Map();
    for (let i = 0; i < days; i++) {
      const d = new Date(Date.now() - i * 86400 * 1000);
      counts.set(d.toISOString().slice(0, 10), 0);
    }
    for (const it of items) {
      const key = (it.published_at || "").slice(0, 10);
      if (counts.has(key)) counts.set(key, counts.get(key) + 1);
    }
    const dates = Array.from(counts.keys()).reverse(); // oldest -> newest
    const values = dates.map((d) => counts.get(d));
    const maxV = Math.max(1, ...values);

    const W = 600,
      H = 80,
      pad = 4;
    const barW = (W - pad * 2) / dates.length;

    let svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">`;
    values.forEach((v, i) => {
      const x = pad + i * barW + 1;
      const h = (v / maxV) * (H - 18);
      const y = H - 12 - h;
      svg += `<rect class="bar" x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${(barW - 2).toFixed(2)}" height="${Math.max(1, h).toFixed(2)}"><title>${dates[i]}: ${v}</title></rect>`;
    });
    // Date labels: first + middle + last
    const labelIdx = [0, Math.floor(dates.length / 2), dates.length - 1];
    for (const i of labelIdx) {
      const x = pad + i * barW + barW / 2;
      svg += `<text x="${x.toFixed(2)}" y="${H - 2}" text-anchor="middle">${dates[i].slice(5)}</text>`;
    }
    svg += "</svg>";
    els.chart.innerHTML = svg;
  }

  // ---------------------------------------------------------------------
  // Bootstrap
  // ---------------------------------------------------------------------

  // Fix 5: trending players + teams nav rail. Rendered client-side
  // from manifest.json so we don't have to re-bake all 68 entity HTML
  // files every cycle. Each pill is a link to that entity's page.
  function renderNavRail(manifest) {
    const railEl = document.getElementById("nav-rail");
    if (!railEl) return;
    // Manifest already lists players + teams sorted by content count
    // desc. Take the top N of each excluding the current entity so the
    // user always sees other places to jump to.
    const TOP_N = 8;
    const players = (manifest.players || [])
      .filter((p) => !(KIND === "player" && p.slug === SLUG))
      .slice(0, TOP_N);
    const teams = (manifest.teams || [])
      .filter((t) => !(KIND === "team" && t.slug === SLUG))
      .slice(0, TOP_N);
    const renderPill = (e, kind) =>
      `<a class="rail-pill rail-pill-${kind}" href="../${kind}s/${ncs.escapeHtml(e.slug)}.html">${ncs.escapeHtml(e.name)}<span class="rail-count">${e.count}</span></a>`;
    railEl.innerHTML = `
      <div class="rail-section">
        <div class="rail-label">Trending players</div>
        <div class="rail-pills">${players.map((p) => renderPill(p, "player")).join("")}</div>
      </div>
      <div class="rail-section">
        <div class="rail-label">Trending teams</div>
        <div class="rail-pills">${teams.map((t) => renderPill(t, "team")).join("")}</div>
      </div>
    `;
  }

  async function loadArchive() {
    const idxKey = KIND === "player" ? "players" : "teams";
    // Cluster C: preload canonical alongside index files. Sets
    // window.NCS_Canonical synchronously available to visualAvatarHtml.
    const [manifest, entityIdx] = await Promise.all([
      fetch("../data/index/manifest.json").then((r) => r.json()),
      fetch(`../data/index/${idxKey}/${SLUG}.json`).then((r) => r.json()),
      ncs.loadCanonical(),
    ]);
    MANIFEST_SLUGS = ncs.manifestSlugSets(manifest);
    ARCHIVE_ITEMS = entityIdx.items || [];
    ncs.attachSearch(els.search, els.suggest, manifest, "../");
    ncs.attachSourcePills(els.pills, (state) => {
      SOURCE_FILTER = state;
      render();
    });
    renderChart(ARCHIVE_ITEMS);
    renderNavRail(manifest);
    render();
  }

  async function loadLive() {
    if (!config.LIVE_MERGE_ENABLED) return;
    try {
      LIVE_ITEMS = await ncs.liveMerge();
    } catch (e) {
      console.warn("live merge failed:", e);
      LIVE_ITEMS = [];
    }
    render();
  }

  loadArchive()
    .then(loadLive)
    .catch((err) => {
      console.error("entity load failed:", err);
      if (els.empty) {
        els.empty.style.display = "block";
        els.empty.textContent = "Could not load content. Check the console.";
      }
    });
})();
