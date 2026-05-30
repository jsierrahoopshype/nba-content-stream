// Node-runnable sanity test for the SITE_BASE derivation + dataUrl
// helper defined in assets/config.js. Run with:
//
//   node scripts/tests/test_site_base.js
//
// Exits 0 on success, non-zero on first failed assertion. Loads
// assets/config.js inside a tiny sandbox that mocks `window` with a
// pathname-only location so the derivation logic runs as it would in
// a real browser. We assert SITE_BASE and dataUrl outputs for every
// pathname shape the production site actually serves.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const CONFIG_PATH = path.join(__dirname, "..", "..", "assets", "config.js");
const CONFIG_SRC = fs.readFileSync(CONFIG_PATH, "utf8");

function deriveFor(pathname) {
  const sandbox = { window: { location: { pathname } } };
  vm.createContext(sandbox);
  vm.runInContext(CONFIG_SRC, sandbox);
  return {
    base: sandbox.window.NCS_CONFIG.SITE_BASE,
    dataUrl: sandbox.window.NCS_dataUrl,
  };
}

const CASES = [
  // pathname,                                       expected SITE_BASE
  ["/",                                              ""],
  ["/index.html",                                    ""],
  ["/players/lebron-james.html",                     ""],
  ["/teams/los-angeles-lakers.html",                 ""],
  ["/nba-content-stream/",                           "/nba-content-stream"],
  ["/nba-content-stream/index.html",                 "/nba-content-stream"],
  ["/nba-content-stream/players/lebron-james.html",  "/nba-content-stream"],
  ["/nba-content-stream/teams/los-angeles-lakers.html", "/nba-content-stream"],
];

let failed = 0;
for (const [pathname, expectedBase] of CASES) {
  const { base, dataUrl } = deriveFor(pathname);
  const expectedDataUrl = (expectedBase || "") + "/data/canonical/players.json";
  const actualDataUrl = dataUrl("data/canonical/players.json");
  const ok = base === expectedBase && actualDataUrl === expectedDataUrl;
  const status = ok ? "PASS" : "FAIL";
  console.log(
    `${status}  pathname=${JSON.stringify(pathname)}  SITE_BASE=${JSON.stringify(base)}  ` +
    `dataUrl(...)=${JSON.stringify(actualDataUrl)}`
  );
  if (!ok) {
    failed++;
    console.log(
      `       expected SITE_BASE=${JSON.stringify(expectedBase)}  ` +
      `dataUrl=${JSON.stringify(expectedDataUrl)}`
    );
  }
}

// Sanity-check that dataUrl also strips a leading slash on its input
// (defensive: callers should pass "data/foo" but "/data/foo" should
// not double-slash).
{
  const { dataUrl } = deriveFor("/nba-content-stream/players/x.html");
  const out = dataUrl("/data/foo.json");
  const expected = "/nba-content-stream/data/foo.json";
  const ok = out === expected;
  console.log(`${ok ? "PASS" : "FAIL"}  leading-slash input is normalised  got=${JSON.stringify(out)}`);
  if (!ok) failed++;
}

if (failed > 0) {
  console.error(`\n${failed} assertion(s) failed.`);
  process.exit(1);
}
console.log(`\nAll ${CASES.length + 1} assertions passed.`);
