// A failed control-use batch passes through the real logger, error API and SQLite store.
"use strict";

const assert = require("node:assert/strict");
const { execFileSync, spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-control-errors-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser;
  try {
    const info = await harnessUp(work, server);
    browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE || undefined });
    const context = await browser.newContext();
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    const page = await context.newPage();
    let failedBatch;
    await page.route("**/api/control-uses", (route) => {
      const batch = route.request().postDataJSON();
      if (!failedBatch && batch.uses.some((use) => use.kind === "press" && use.control === 'key "x"')) {
        failedBatch = batch;
        return route.fulfill({ status: 500, contentType: "application/json", body: "{}" });
      }
      return route.continue();
    });
    await page.goto(`${info.origin}/usage`);
    await page.waitForFunction(() => typeof window.logError === "function" && document.visibilityState === "visible");
    const reported = page.waitForResponse(
      (response) => response.url().endsWith("/api/page-errors") && response.request().method() === "POST",
      { timeout: 25e3 },
    );
    await page.keyboard.press("x");
    const report = await reported;
    assert.ok(failedBatch, "the trusted key press reaches the failed control-use batch");
    assert.equal(report.status(), 200, "the error API accepts the report");
    console.log("pass: the error API accepts the control-use failure report");
    const response = await fetch(`${info.origin}/api/page-errors?all=true`, {
      headers: { authorization: `Bearer ${"t".repeat(40)}` },
      signal: AbortSignal.timeout(10e3),
    });
    assert.equal(response.status, 200);
    const rows = (await response.json()).errors.filter((error) => error.place === "control uses");
    assert.equal(rows.length, 1);
    const [stored] = rows;
    assert.equal(stored.page, "/usage");
    assert.equal(stored.message, "control uses: Cockpit answered 500");
    assert.match(stored.stack, /Error: control uses: Cockpit answered 500\n\s+at .*\/static\/uses\.js:\d+/);
    assert.equal(stored.count, 1);
    console.log("pass: the error API returns the original place, message and stack");
    const persisted = JSON.parse(execFileSync(PYTHON, ["-c", `
import json, sqlite3, sys
from pathlib import Path
with sqlite3.connect(Path(sys.argv[1]).as_uri() + "?mode=ro", uri=True) as conn:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM page_errors WHERE id = ?", (int(sys.argv[2]),)).fetchone()
    print(json.dumps(dict(row)))
`, path.join(work, "cockpit.db"), String(stored.id)], { encoding: "utf8", timeout: 5e3 }));
    assert.deepEqual(persisted, stored, "SQLite stores the same original error returned by the API");
    console.log("pass: control-use failure stores its original place, message and stack through the API and SQLite");
  } finally {
    try {
      if (browser) await browser.close();
    } finally {
      if (server.exitCode === null && server.signalCode === null) {
        const gone = new Promise((resolve) => server.once("exit", resolve));
        server.kill();
        await Promise.race([gone, sleep(3e3)]);
        if (server.exitCode === null && server.signalCode === null) server.kill("SIGKILL");
        await gone;
      }
    }
  }
}

async function harnessUp(work, server) {
  const file = path.join(work, "cookie.json");
  for (const end = Date.now() + 60e3; Date.now() < end; await sleep(200)) {
    if (server.exitCode !== null) throw new Error(`serve_preview.py exited with ${server.exitCode}`);
    if (!fs.existsSync(file)) continue;
    const info = JSON.parse(fs.readFileSync(file, "utf8"));
    if (await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false)) return info;
  }
  throw new Error("serve_preview.py did not come up within 60 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
