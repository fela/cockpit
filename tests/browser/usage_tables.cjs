// Usage builds its detailed tables on opening, and keeps period, measure and rank links current.
// Run with PLAYWRIGHT_MODULE and BROWSER_EXECUTABLE as described in tests/browser/README.md.
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const STATIC = path.resolve(__dirname, "../../src/cockpit/static");
const stamp = new Date().toISOString();
const use = {
  tokens: { fresh: 0, cache_write: 0, cache_read: 0, output: 1000 },
  usd_by_kind: { fresh: 0, cache_write: 0, cache_read: 0, output: 0.02 },
  calls: 1, usd: 0.02, week_percent: 0.001, plan_usd: 0.0005,
  subagent_usd: 0, woken_usd: 0, misses: 0, miss_tokens: 0, miss_usd: 0, paused_miss_usd: 0,
};
function report(period) {
  return {
    period, ready_at: stamp, since: stamp, total: use,
    sessions: [{ session_id: "session-1", name: `Session for ${period}`, title: "", first_prompt: "Fix parser",
      cwd: "/example/parser", kind: "terminal", first: stamp, last: stamp, turns: 1, use }],
    skills: [], origins: [], session_kinds: [], models: [], top_turns: [], miss_turns: [], unpriced_models: [],
    week_list_usd: 4000, plan_usd_per_percent: 0.5,
    limits: { accounts: [], error: null },
    codex: { calls: 0, sessions: [], accounts: [], total: { fresh: 0, cached: 0, output: 0, reasoning: 0 },
      week_percent: 0, weeks: 0 },
    daily: { since: new Date(Date.now() - 30 * 86400e3).toISOString(), hours: [], codex_hours: [], steps: [] },
  };
}
async function main() {
  const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE || undefined });
  try {
    for (const width of [1280, 390]) {
      const page = await browser.newPage({ viewport: { width, height: 900 } });
      const errors = [];
      let refreshing = true;
      page.on("pageerror", (error) => { errors.push(error.message); console.error(error.message); });
      await page.route("http://localhost/**", async (route) => {
        const url = new URL(route.request().url());
        if (url.pathname === "/api/spend") return route.fulfill({ json: { ...report(url.searchParams.get("period")), refreshing } });
        if (url.pathname.startsWith("/api/")) return route.fulfill({ json: {} });
        const file = path.join(STATIC, url.pathname === "/usage" ? "usage.html" : path.basename(url.pathname));
        if (!fs.existsSync(file)) return route.fulfill({ status: 404, body: "" });
        const type = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" }[path.extname(file)];
        return route.fulfill({ contentType: type || "application/octet-stream", body: fs.readFileSync(file) });
      });
      await page.goto("http://localhost/usage");
      await page.waitForSelector(".spend-readout");
      assert.match(await page.locator("#spend-lede").textContent(), /Updating usage from session files/);
      refreshing = false;
      await page.waitForFunction(() => !document.querySelector("#spend-lede").textContent.includes("Updating usage"), null,
        { timeout: 12000 });
      assert.equal(await page.locator("#spend-more table").count(), 0, "closed tables wait for opening");
      await page.getByRole("button", { name: "API price", exact: true }).click();
      await page.getByRole("button", { name: "24 hours", exact: true }).click();
      await page.waitForFunction(() => document.querySelector("#spend-lede").textContent.startsWith("The last 24 hours"));
      assert.equal(await page.locator("#spend-more table").count(), 0, "period and measure changes keep tables deferred");
      await page.locator("#spend-more > summary").click();
      await page.waitForSelector(".sessions-table");
      assert.match(await page.locator(".sessions-table").textContent(), /Session for 1/);
      assert.match(await page.locator(".sessions-table").textContent(), /API price/);
      await page.getByRole("button", { name: "Plan price", exact: true }).click();
      assert.match(await page.locator(".sessions-table").textContent(), /Plan price/);
      await page.locator("#spend-more > summary").click();
      await page.getByRole("button", { name: "7 days", exact: true }).click();
      await page.waitForFunction(() => document.querySelector("#spend-lede").textContent.startsWith("The last 7 days"));
      // A rank card opens its table and scrolls there on the same click.
      await page.locator("#spend-where .spend-card").filter({ hasText: "Sessions" }).first().locator(".card-more").click();
      assert.equal(await page.locator("#spend-more").evaluate((el) => el.open), true);
      assert.match(await page.locator(".sessions-table").textContent(), /Session for 7/);
      const numbers = await page.evaluate(() => [dollars(4.2), dollars(138), percent(0.04), percent(4.3), tokens(175000), axisFigure(2000)]);
      assert.deepEqual(numbers, ["$4.20", "$138", "0.04 %", "4.3 %", "175 k", "2,000"]);
      assert.deepEqual(errors, []);
      console.log(`pass: ${width}px deferred tables, current period and measure, rank links and figure text`);
      await page.close();
    }
  } finally { await browser.close(); }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
