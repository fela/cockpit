// A page left open across an update can receive state in a different shape. This fixture omits the steps'
// `details` field to exercise that case. The page shows Reload, says it is older than the running Cockpit,
// and marks the error as coming from an older page (SPEC CODE-2, UI-13). The same incomplete state under
// the page's own version is logged as an error of the running code.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what
// it needs), loads the main page in a headless Chromium, then answers its polls with the fixture state less
// the queue steps' `details`, first under the page's own version and then under a newer one. Prints one
// line per check and exits 1 when one failed. A run takes about two minutes.
//
// Usage, from the checkout: node tests/browser/stale_page.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
// The harness's agent token (tests/conftest.py).
const TOKEN = "t".repeat(40);
const OUTDATED = ", on a page older than the running Cockpit";

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-stale-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    const logged = async () => {
      const response = await fetch(`${info.origin}/api/page-errors?all=true`, { headers: { authorization: `Bearer ${TOKEN}` } });
      return (await response.json()).errors;
    };
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    const page = await context.newPage();
    let reports = [];
    page.on("request", (request) => {
      if (request.url().endsWith("/api/page-errors") && request.method() === "POST") reports.push(JSON.parse(request.postData()));
    });
    await page.goto(`${info.origin}/classic`);
    await page.waitForLoadState("networkidle");
    await page.waitForFunction(() => ui.version !== null);
    const queued = await page.evaluate(() => ui.state.queue.length);
    check("the fixture state has steps in the queue", queued > 0, `${queued} steps`);
    check("the page loads without an error", reports.length === 0, JSON.stringify(reports));

    // The fixture state as the page read it, with every queue step's details gone, under `version` (null keeps
    // the server's). Each poll gets it at once.
    const live = await page.evaluate(() => fetch("/api/state").then((response) => response.json()));
    const broken = (version) => (route) => {
      const state = structuredClone(live);
      for (const step of state.queue) delete step.details;
      if (version) state.version = version;
      return route.fulfill({ json: state });
    };
    const shown = () => page.evaluate(() => ({
      // motion.js moves a title into data-tip, its own tooltip.
      error: ui.error, reload: !document.getElementById("reload").hidden,
      live: document.getElementById("live").title || document.getElementById("live").dataset.tip || "",
    }));
    const placed = (place) => reports.filter((report) => report.place === place);

    reports = [];
    await page.route("**/api/state", broken(null));
    await page.waitForFunction(() => ui.error !== "", null, { timeout: 30e3 });
    await sleep(500);
    const same = await shown();
    check("a state the running code cannot draw is its error, unmarked",
      placed("refreshing the page").length === 1 && placed(`refreshing the page${OUTDATED}`).length === 0
        && !same.reload && !same.error.includes("older"),
      JSON.stringify({ same, reports }));
    await page.unroute("**/api/state");
    await page.waitForFunction(() => ui.error === "", null, { timeout: 30e3 });

    reports = [];
    await page.route("**/api/state", broken("0123456789ab"));
    await page.waitForFunction(() => ui.error.includes("older"), null, { timeout: 30e3 });
    await sleep(500);
    const stale = await shown();
    check("an older page shows Reload", stale.reload, JSON.stringify(stale));
    check("its status says it is older and to press Reload",
      stale.error.startsWith("this page is older than the running Cockpit; press Reload (") && stale.live.includes("press Reload"),
      JSON.stringify(stale));
    const marked = placed(`refreshing the page${OUTDATED}`);
    check("its error is sent marked as from an older page",
      marked.length === 1 && marked[0].message.startsWith("Cannot read properties of undefined")
        && placed("refreshing the page").length === 0,
      JSON.stringify(reports));
    // Each poll draws the state again and fails again: the status keeps saying so, and a failure past a minute
    // shows in the banner. Repeated errors stay visible until a valid state clears them.
    console.error("  waiting 70 s of identical polls");
    await sleep(70e3);
    const lasting = await page.evaluate(() => ({
      error: ui.error, banner: document.querySelector("#banner .warn-card.fail")?.textContent || "",
    }));
    check("identical polls keep the status, and after a minute the banner says to press Reload",
      lasting.error.includes("older") && lasting.banner.includes("press Reload"), JSON.stringify(lasting));
    const errors = await logged();
    check("the log holds it apart from the running code's error",
      errors.some((error) => error.place === `refreshing the page${OUTDATED}`) && errors.some((error) => error.place === "refreshing the page"),
      JSON.stringify(errors.slice(0, 3).map((error) => [error.place, error.message])));
    await page.unroute("**/api/state");

    // The live state again, under the page's own version: the page draws it and the status clears.
    await page.waitForFunction(() => ui.error === "", null, { timeout: 30e3 });
    const back = await shown();
    check("the page draws a state it knows again", !back.reload && !back.live.includes("older"), JSON.stringify(back));
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failed = results.filter((result) => !result.ok);
  console.error(`${results.length - failed.length} of ${results.length} checks passed`);
  process.exitCode = failed.length ? 1 : 0;
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
