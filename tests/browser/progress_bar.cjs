// The step bar's fill: how much of the user's time on the item is behind them (SPEC NEXT-35). Puts a size and progress
// on a step in the queue, then checks, at desktop and phone width:
// - before enough estimates are checked, the fill and its words show the session's estimate as given;
// - once /api/state carries a correction, the fill takes its gap off, and the words name the session's estimate;
// - a correction that would go below 0 stops at 0;
// - Your time on Board explains the check and the correction in words.
// Saves a screenshot of the bar per width in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a copy of the live database; README.md says what it needs) and
// drives a headless Chromium. The correction is added to the page's state responses in the browser, so the check does
// not depend on how many finished items the copied database holds. Prints one line per check and exits 1 when one
// failed.
//
// Usage, from the checkout: node tests/browser/progress_bar.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder, screenshotRedrawn } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-progress-bar-");
  const server = spawn(path.join(__dirname, "..", "..", ".venv", "bin", "python"), [path.join(__dirname, "serve_preview.py"), work],
    { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    const headers = { cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json" };
    const state = await fetch(`${info.origin}/api/state`, { headers }).then((response) => response.json());
    // The bar shows on queue rows; Next up's card, the first step, has none.
    const step = state.queue[1];
    if (!step) throw new Error("the fixture database has fewer than two steps in the queue");
    const put = await fetch(`${info.origin}/api/steps`, { method: "POST", headers,
      body: JSON.stringify({ item_id: step.item_id, need: step.need, size: "medium", progress: 60 }) });
    check("the step takes a size and progress", put.ok, `HTTP ${put.status}: ${await put.text()}`);

    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const width of [1280, 390]) {
      for (const [name, correction, fill, words] of [
        ["as given", null, 60, "Your part: medium, 60% behind you"],
        ["corrected", { items: 4, estimates: 11, median_gap: 25, median_off: 30 }, 35,
          "Your part: medium, 35% behind you"],
        ["corrected, stopping at 0", { items: 3, estimates: 9, median_gap: 80, median_off: 80 }, 0,
          "Your part: medium, 0% behind you"],
      ]) {
        const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: "dark" });
        await context.setExtraHTTPHeaders({ cookie: headers.cookie });
        const page = await context.newPage();
        page.setDefaultTimeout(5000);
      page.setDefaultNavigationTimeout(60000);
        const errors = [];
        page.on("pageerror", (error) => errors.push(error.message));
        await page.route("**/api/state*", async (route) => {
          const response = await route.fetch();
          const body = await response.json();
          body.progress_correction = correction;
          await route.fulfill({ response, json: body });
        });
        await page.goto(`${info.origin}/classic`);
        const bar = page.locator(`[data-step="${step.id}"] a.size-bar:visible`).first();
        await bar.waitFor();
        // A poll may redraw the bar while it is measured; the measure is taken again until the bar is drawn.
        const seen = await waitFor(() => bar.evaluate((el) => ({
          // The page moves a title into its own tooltip (data-tip) once it is drawn.
          title: el.getAttribute("title") || el.dataset.tip || "",
          fill: Math.round(el.querySelector(".size-fill").getBoundingClientRect().width / el.querySelector(".size-track").getBoundingClientRect().width * 100),
        })), (now) => Number.isFinite(now.fill));
        check(`${width} px, ${name}: the fill is ${fill} %`, Math.abs(seen.fill - fill) <= 2, seen.fill);
        check(`${width} px, ${name}: the words say so`, seen.title.split("\n")[0] === words, seen.title);
        if (name === "corrected") {
          const shot = path.join(work, `progress-bar-${width}.png`);
          await screenshotRedrawn(page.locator(`[data-step="${step.id}"]`).first(), { path: shot });
          console.error(`  screenshot ${shot}`);
          await bar.click();
          const note = await waitFor(() => page.evaluate(() => document.querySelector("#estimates .note")?.textContent || ""),
            (text) => text.length > 0);
          check(`${width} px: Your time explains the check against your timer`, /your timer time on the task/.test(note), note);
        }
        check(`${width} px, ${name}: no page errors`, errors.length === 0, errors.join(" | "));
        // The state route may still be fetching; it lets go before the context closes.
        await page.unrouteAll({ behavior: "ignoreErrors" });
        await context.close();
      }
    }
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

// The value `read` gives once `ok` holds it, or its last value after 5 s.
async function waitFor(read, ok) {
  const end = Date.now() + 5000;
  let value = await read();
  while (!ok(value) && Date.now() < end) {
    await sleep(100);
    value = await read();
  }
  return value;
}

// The harness's cookie file, once it is written and the server answers.
async function harnessUp(work, server) {
  const file = path.join(work, "cookie.json");
  const end = Date.now() + 60e3;
  while (Date.now() < end) {
    if (server.exitCode !== null) throw new Error(`serve_preview.py exited with ${server.exitCode}`);
    if (fs.existsSync(file)) {
      const info = JSON.parse(fs.readFileSync(file, "utf8"));
      const up = await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false);
      if (up) return info;
    }
    await sleep(200);
  }
  throw new Error("serve_preview.py did not come up within 60 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
