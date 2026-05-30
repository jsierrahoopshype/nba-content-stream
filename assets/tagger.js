/* NBA Content Stream — lite client-side entity tagger.
 *
 * Ports the easy 95% of scripts/lib/canonical.py for the browser.
 * Loads data/canonical/{players,teams}.json once, then exposes
 *   window.NCS_Tagger.detectEntities(text) -> {players: [...], teams: [...]}
 *
 * Matching rules (mirror the Python tagger):
 *   - Case-insensitive, word-boundary regex match.
 *   - Matches canonical names + aliases + each player's last name.
 *   - An ambiguous phrase that maps to >1 player or >1 team is SKIPPED
 *     (no team-context disambiguation on the client edge — that would
 *     require building the disambiguator and the live-merge volume is
 *     low enough that "Murray" without context just doesn't tag).
 */
(function () {
  "use strict";

  // Cached caller-resolved on first use.
  let _ready = null;
  let _players = null; // {slug: {name, aliases, team}}
  let _teams = null;   // {slug: {name, aliases, city, abbr}}
  let _playerIndex = null; // {lowercased_phrase: [slug, ...]}
  let _teamIndex = null;
  let _pattern = null;     // single concatenated regex of all phrases

  function _stripMeta(blob) {
    const out = {};
    for (const k of Object.keys(blob)) {
      if (!k.startsWith("_")) out[k] = blob[k];
    }
    return out;
  }

  function _lastName(displayName) {
    const parts = (displayName || "").split(/\s+/);
    return parts[parts.length - 1] || "";
  }

  function _buildIndex(dict, includeLastName) {
    const idx = Object.create(null);
    for (const slug of Object.keys(dict)) {
      const info = dict[slug];
      const phrases = [String(info.name)];
      for (const a of info.aliases || []) phrases.push(String(a));
      if (includeLastName) phrases.push(_lastName(String(info.name)));
      for (const p of phrases) {
        if (!p) continue;
        const key = p.toLowerCase();
        const slugs = idx[key] || (idx[key] = []);
        if (slugs.indexOf(slug) < 0) slugs.push(slug);
      }
    }
    return idx;
  }

  function _escapeRegex(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function _buildCompiledPattern(idx) {
    // Sort longest-first so "LeBron James" is tried before "James".
    const phrases = Object.keys(idx).sort((a, b) => b.length - a.length);
    if (phrases.length === 0) return null;
    return new RegExp(
      "\\b(" + phrases.map(_escapeRegex).join("|") + ")\\b",
      "gi"
    );
  }

  async function _load() {
    if (_ready) return _ready;
    _ready = (async () => {
      const [playersBlob, teamsBlob] = await Promise.all([
        fetch("data/canonical/players.json").then((r) => r.json()),
        fetch("data/canonical/teams.json").then((r) => r.json()),
      ]).catch(async () => {
        // Try one level up in case we're on /players/{slug}.html.
        return Promise.all([
          fetch("../data/canonical/players.json").then((r) => r.json()),
          fetch("../data/canonical/teams.json").then((r) => r.json()),
        ]);
      });
      _players = _stripMeta(playersBlob);
      _teams = _stripMeta(teamsBlob);
      // Drop bare last names from the player candidate pool. With ~530
      // active players many surnames collide (Mitchell, Murray,
      // Williams, Thompson, ...); matching only full names + curated
      // short-form aliases avoids the false-positive trap that the
      // older smaller canonical didn't expose.
      _playerIndex = _buildIndex(_players, false);
      _teamIndex = _buildIndex(_teams, false);
      // We build ONE combined pattern; for each match we look up in
      // both indexes to resolve to slugs.
      const merged = Object.create(null);
      for (const k of Object.keys(_playerIndex)) merged[k] = true;
      for (const k of Object.keys(_teamIndex)) merged[k] = true;
      _pattern = _buildCompiledPattern(merged);
    })();
    return _ready;
  }

  function detectEntitiesSync(text) {
    if (!text || !_pattern) return { players: [], teams: [] };
    const found = { players: new Set(), teams: new Set() };
    let m;
    _pattern.lastIndex = 0;
    while ((m = _pattern.exec(text)) !== null) {
      const key = m[1].toLowerCase();
      const teamSlugs = _teamIndex[key];
      if (teamSlugs && teamSlugs.length === 1) {
        found.teams.add(teamSlugs[0]);
      } // ambiguous team match dropped
      const playerSlugs = _playerIndex[key];
      if (playerSlugs && playerSlugs.length === 1) {
        found.players.add(playerSlugs[0]);
      } // ambiguous player match dropped (e.g. "Murray", "JB")
    }
    return {
      players: Array.from(found.players).sort(),
      teams: Array.from(found.teams).sort(),
    };
  }

  async function detectEntities(text) {
    await _load();
    return detectEntitiesSync(text);
  }

  // Inline sanity check: when the tagger loads on a page that includes
  // <meta name="ncs-tagger-test" content="1">, run a tiny self-check
  // and log to console. Cheap, opt-in, never breaks production.
  async function _selfCheck() {
    if (!document.querySelector('meta[name="ncs-tagger-test"]')) return;
    await _load();
    const cases = [
      ["LeBron James and the Lakers", ["lebron-james"], ["los-angeles-lakers"]],
      ["Wemby blocks everything", ["victor-wembanyama"], []],
      ["Murray scored 30", [], []], // ambiguous — drops
      ["", [], []],
    ];
    let pass = 0, fail = 0;
    for (const [text, expP, expT] of cases) {
      const got = detectEntitiesSync(text);
      const ok =
        JSON.stringify(got.players) === JSON.stringify(expP) &&
        JSON.stringify(got.teams) === JSON.stringify(expT);
      if (ok) pass++;
      else {
        fail++;
        console.warn("tagger self-check FAIL", { text, got, expP, expT });
      }
    }
    console.log(`NCS_Tagger self-check: ${pass} pass, ${fail} fail`);
  }

  window.NCS_Tagger = {
    detectEntities,
    detectEntitiesSync,
    ready: _load,
  };

  if (typeof document !== "undefined") {
    if (document.readyState !== "loading") _selfCheck();
    else document.addEventListener("DOMContentLoaded", _selfCheck);
  }
})();
