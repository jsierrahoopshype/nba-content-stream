// Node-runnable sanity test for the pacedBatchFetch helper defined in
// assets/common.js. Run with:
//
//   node scripts/tests/test_paced_batch.js
//
// We don't want to depend on jsdom or a real browser. The helper is
// pure (no DOM, just Promise + setTimeout + an optional window.NCS_DEBUG
// trace), so we extract it from common.js with a regex and eval it in
// a vm sandbox that exposes Promise, setTimeout, and a stubbed window.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const COMMON_PATH = path.join(__dirname, "..", "..", "assets", "common.js");
const SRC = fs.readFileSync(COMMON_PATH, "utf8");

// Capture the named function declaration. Greedy match between the
// signature and the next top-level `}\n` would be brittle; instead pull
// from the start of `async function pacedBatchFetch` through the
// matching close-brace by counting depth.
function extractFn(src, signature) {
  const start = src.indexOf(signature);
  if (start < 0) throw new Error("signature not found: " + signature);
  // Find the opening brace of the function body.
  const openIdx = src.indexOf("{", start);
  let depth = 0;
  for (let i = openIdx; i < src.length; i++) {
    const c = src[i];
    if (c === "{") depth++;
    else if (c === "}") {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error("unterminated function body for: " + signature);
}

const FN_SRC = extractFn(SRC, "async function pacedBatchFetch");

const sandbox = {
  Promise,
  setTimeout,
  console,
  window: {}, // no NCS_DEBUG, so no trace calls fire
};
vm.createContext(sandbox);
vm.runInContext(FN_SRC + "\nthis.pacedBatchFetch = pacedBatchFetch;", sandbox);

(async () => {
  let failed = 0;

  // Case 1: all succeed.
  {
    const items = [1, 2, 3, 4, 5, 6, 7];
    const seen = [];
    const out = await sandbox.pacedBatchFetch(
      items,
      3,
      10,
      async (x) => { seen.push(x); return x * 2; },
      null
    );
    const ok = JSON.stringify(out) === JSON.stringify([2, 4, 6, 8, 10, 12, 14]) &&
               seen.length === 7;
    console.log(`${ok ? "PASS" : "FAIL"}  all succeed: out=${JSON.stringify(out)}`);
    if (!ok) failed++;
  }

  // Case 2: some throw — failures are swallowed and filtered.
  {
    const out = await sandbox.pacedBatchFetch(
      [1, 2, 3, 4, 5],
      2,
      5,
      async (x) => {
        if (x % 2 === 0) throw new Error("nope");
        return "got-" + x;
      },
      null
    );
    const ok = JSON.stringify(out) === JSON.stringify(["got-1", "got-3", "got-5"]);
    console.log(`${ok ? "PASS" : "FAIL"}  failures swallowed: out=${JSON.stringify(out)}`);
    if (!ok) failed++;
  }

  // Case 3: chunk pacing — total time >= (chunks - 1) * betweenMs.
  {
    const items = Array.from({ length: 10 }, (_, i) => i);
    const t0 = Date.now();
    await sandbox.pacedBatchFetch(items, 4, 80, async (x) => x, null);
    const elapsed = Date.now() - t0;
    // 10 items / 4 per chunk = 3 chunks; 2 gaps of 80ms = ~160ms min.
    // Allow generous upper bound; we just want to verify pacing happens.
    const ok = elapsed >= 150 && elapsed < 1000;
    console.log(`${ok ? "PASS" : "FAIL"}  pacing: elapsed=${elapsed}ms (expected >=150 and <1000)`);
    if (!ok) failed++;
  }

  // Case 4: empty input.
  {
    const out = await sandbox.pacedBatchFetch([], 5, 100, async () => "x", null);
    const ok = Array.isArray(out) && out.length === 0;
    console.log(`${ok ? "PASS" : "FAIL"}  empty input: out=${JSON.stringify(out)}`);
    if (!ok) failed++;
  }

  // Case 5: no gap after the final chunk (single chunk exits fast).
  {
    const t0 = Date.now();
    await sandbox.pacedBatchFetch([1, 2, 3], 10, 500, async (x) => x, null);
    const elapsed = Date.now() - t0;
    // Single chunk; no betweenMs delay should be paid.
    const ok = elapsed < 100;
    console.log(`${ok ? "PASS" : "FAIL"}  single chunk skips final delay: elapsed=${elapsed}ms`);
    if (!ok) failed++;
  }

  // Case 6 (Polish-10 Fix 1): onProgress fires after every chunk
  // with cumulative {done, total, succeeded, failed, chunkIndex,
  // totalChunks}, and exceptions inside the callback do NOT break
  // the batch.
  {
    const items = Array.from({ length: 7 }, (_, i) => i);
    const calls = [];
    const out = await sandbox.pacedBatchFetch(
      items,
      3,
      5,
      async (x) => x * 10,
      null,
      (p) => calls.push(p)
    );
    // 7 items / 3 per chunk = 3 chunks → 3 progress calls.
    const ok =
      calls.length === 3 &&
      calls[0].done === 3 && calls[0].total === 7 &&
      calls[0].chunkIndex === 1 && calls[0].totalChunks === 3 &&
      calls[2].done === 7 && calls[2].succeeded === 7 &&
      out.length === 7;
    console.log(`${ok ? "PASS" : "FAIL"}  onProgress fires per chunk: ${JSON.stringify(calls)}`);
    if (!ok) failed++;
  }

  // Case 7 (Polish-10 Fix 1): a throwing onProgress callback is
  // caught — the batch must still complete and return results.
  {
    const out = await sandbox.pacedBatchFetch(
      [1, 2, 3, 4],
      2,
      5,
      async (x) => x,
      null,
      () => { throw new Error("progress blew up"); }
    );
    const ok = JSON.stringify(out) === JSON.stringify([1, 2, 3, 4]);
    console.log(`${ok ? "PASS" : "FAIL"}  throwing onProgress doesn't break batch: out=${JSON.stringify(out)}`);
    if (!ok) failed++;
  }

  // Case 8 (Perf-1): onChunk streams each chunk's non-null results as it
  // lands (incremental render), and a throwing onChunk doesn't break the
  // batch. onChunk is the 7th argument.
  {
    const items = [1, 2, 3, 4, 5];
    const chunks = [];
    const out = await sandbox.pacedBatchFetch(
      items,
      2,
      5,
      async (x) => (x === 3 ? null : x * 10), // 3 fails → dropped from its chunk
      null,
      null,
      (chunkOut, meta) => chunks.push({ chunkOut, meta }),
    );
    const ok =
      chunks.length === 3 &&
      JSON.stringify(chunks[0].chunkOut) === JSON.stringify([10, 20]) &&
      JSON.stringify(chunks[1].chunkOut) === JSON.stringify([40]) && // 3 dropped
      JSON.stringify(chunks[2].chunkOut) === JSON.stringify([50]) &&
      chunks[0].meta.chunkIndex === 1 && chunks[2].meta.totalChunks === 3 &&
      JSON.stringify(out) === JSON.stringify([10, 20, 40, 50]);
    console.log(`${ok ? "PASS" : "FAIL"}  onChunk streams per-chunk results: ${JSON.stringify(chunks.map(c => c.chunkOut))}`);
    if (!ok) failed++;
  }

  // Case 9 (Perf-1): a throwing onChunk is caught — batch still completes.
  {
    const out = await sandbox.pacedBatchFetch(
      [1, 2, 3, 4],
      2,
      5,
      async (x) => x,
      null,
      null,
      () => { throw new Error("onChunk blew up"); },
    );
    const ok = JSON.stringify(out) === JSON.stringify([1, 2, 3, 4]);
    console.log(`${ok ? "PASS" : "FAIL"}  throwing onChunk doesn't break batch: out=${JSON.stringify(out)}`);
    if (!ok) failed++;
  }

  if (failed > 0) {
    console.error(`\n${failed} assertion(s) failed.`);
    process.exit(1);
  }
  console.log("\nAll 9 assertions passed.");
})();
