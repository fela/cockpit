// Compare this checkout with a supplied baseline on the same synthetic board.
// node tests/browser/performance.cjs BASELINE_CHECKOUT OUTPUT_JSON
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const zlib = require("node:zlib");
const { spawnSync } = require("node:child_process");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const current = path.resolve(__dirname, "../..");
const baseline = path.resolve(process.argv[2]);
const output = process.argv[3];
const seed = `
import sqlite3,sys
from pathlib import Path
c=sqlite3.connect(Path(sys.argv[1])/'cockpit.db');c.row_factory=sqlite3.Row
item=dict(c.execute('SELECT * FROM items WHERE archived=0 AND stage="building" LIMIT 1').fetchone())
step=dict(c.execute('SELECT * FROM steps WHERE status="open" LIMIT 1').fetchone())
def put(table,values):
 keys=list(values);return c.execute('INSERT INTO '+table+' ('+','.join(keys)+') VALUES ('+','.join('?' for _ in keys)+')',list(values.values())).lastrowid
for n in range(250):
 i={k:v for k,v in item.items() if k!='id'};i.update(key=f'note:perf-{n}',title=f'Performance example {n}',pinned_at=None)
 iid=put('items',i)
 s={k:v for k,v in step.items() if k!='id'};s.update(item_id=iid,need=f'perf-{n}',ask=f'Review example {n}',score=10,queued_at=None,body=('Example task content with **formatting**, [a link](https://example.com), and a decision.\\n\\n'*5))
 put('steps',s)
c.commit()
`;
function read(info, cursor) {
  return new Promise((resolve, reject) => {
    const started = performance.now();
    const headers = { Cookie: `${info.name}=${info.value}`, "Accept-Encoding": "gzip", "x-cockpit-sync": "1", ...(cursor ? { "x-cockpit-since": cursor } : {}) };
    http.get(info.origin + "/api/state", { headers }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => {
        const wire = Buffer.concat(chunks);
        const decoded = response.headers["content-encoding"] === "gzip" ? zlib.gunzipSync(wire) : wire;
        resolve({ ms: performance.now() - started, bytes: wire.length, decodedBytes: decoded.length,
          cursor: response.headers["x-cockpit-state"], data: JSON.parse(decoded) });
      });
    }).on("error", reject);
  });
}
(async () => {
  const cases = {};
  for (const [name, root] of [["before", baseline], ["after", current]]) {
    const { work, info } = await require(path.join(root, "tests/browser/work.cjs")).startPreview(`cockpit-perf-${name}-`);
    const seeded = spawnSync(path.join(root, ".venv/bin/python"), ["-c", seed, work], { encoding: "utf8" });
    assert.equal(seeded.status, 0, seeded.stderr);
    cases[name] = { info, work, polls: [], renders: [], loads: [] };
  }
  const browser = await chromium.launch({ headless: true });
  try {
    // Alternate versions to reduce bias from changes in host load.
    for (let trial = 0; trial < 5; trial++) {
      for (const name of trial % 2 ? ["after", "before"] : ["before", "after"]) {
        const test = cases[name];
        const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, extraHTTPHeaders: { Cookie: `${test.info.name}=${test.info.value}` } });
        const page = await context.newPage();
        const errors = [];
        page.on("pageerror", (error) => errors.push(error.message));
        const cdp = await context.newCDPSession(page);
        await cdp.send("Network.enable");
        await cdp.send("Network.emulateNetworkConditions", { offline: false, latency: 40, downloadThroughput: 500000, uploadThroughput: 250000 });
        await cdp.send("Emulation.setCPUThrottlingRate", { rate: 4 });
        await page.goto(test.info.origin, { timeout: 120000, waitUntil: "domcontentloaded" });
        await page.locator("#next [data-step]").waitFor({ timeout: 120000 });
        const load = await page.evaluate(() => ({ readyMs: performance.now(), jsBytes: performance.getEntriesByType("resource").filter((r) => /\.js(?:\?|$)/.test(r.name)).reduce((n, r) => n + r.encodedBodySize, 0), items: ui.state.items.length }));
        test.loads.push(load);
        await cdp.send("Emulation.setCPUThrottlingRate", { rate: 1 });
        await page.waitForTimeout(500);
        test.renders.push(...await page.evaluate(() => {
          const times = [];
          for (let i = 0; i < 4; i++) { const at = performance.now(); render(false); times.push(performance.now() - at); }
          return times;
        }));
        if (trial === 0) await page.screenshot({ path: path.join(test.work, `${name}.png`), fullPage: false });
        assert.deepEqual(errors, [], `${name} browser errors`);
        await context.close();
        const first = await read(test.info, null);
        test.fullBytes = first.bytes;
        let cursor = first.cursor;
        for (let i = 0; i < 4; i++) {
          const result = await read(test.info, cursor);
          cursor = result.cursor;
          test.polls.push({ ms: result.ms, bytes: result.bytes, decodedBytes: result.decodedBytes });
        }
        console.log(`${name} trial ${trial + 1}: ready ${Math.round(load.readyMs)} ms, JS ${load.jsBytes} bytes`);
      }
    }
    for (const test of Object.values(cases)) { delete test.info; }
    fs.writeFileSync(output, JSON.stringify({ workload: "261 items including 250 ready-next steps; cold browser; 40 ms latency, 4 Mbit/s download, 4x CPU slowdown for load; unthrottled redraws and loopback polls", cases }, null, 2));
    console.log(output);
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
