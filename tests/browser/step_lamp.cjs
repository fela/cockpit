// A session linked to an item only through its open step marks the item as worked on. Gives one agents' item no
// sessions of its own and a working session through its step (`step_sessions`) in the page's state responses, then
// checks, at desktop and phone width, that the item's row in the list ends in the pulsing working dot and that the dot
// says a session is working on it.  Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and drives a headless Chromium. Prints one line per check and exits 1 when
// one failed.  Usage, from the checkout: node tests/browser/step_lamp.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder, fullState } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-step-lamp-");
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
    const cookie = `${info.name}=${info.value}`;
    const state = await fetch(`${info.origin}/api/state`, { headers: { cookie } }).then((response) => response.json());
    const item = state.items.find((each) => each.ball === "agents" && !each.archived);
    if (!item) throw new Error("the fixture database has no item whose next move is the agents'");
    if (!state.sessions.length) throw new Error("the fixture database lists no sessions");
    const linked = state.sessions[0].session_id;

    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const width of [1280, 390]) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: "dark" });
      await context.setExtraHTTPHeaders({ cookie });
      const page = await context.newPage();
      page.setDefaultTimeout(5000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.route("**/api/state*", async (route) => {
        // A poll still under way when the context closes has nothing left to answer.
        const full = await fullState(route).catch(() => null);
        if (!full) return;
        const body = await full.response.json();
        for (const each of body.items) {
          if (each.id === item.id) Object.assign(each, { sessions: [], step_sessions: [linked], stalled_since: null });
        }
        for (const session of body.sessions) {
          if (session.session_id === linked) {
            Object.assign(session, { state: "working", working_now: true, alive: true, waits_on_dialog: false });
          }
        }
        await route.fulfill({ ...full, json: body }).catch(() => {});
      });

      await page.goto(`${info.origin}/`);
      const row = page.locator(`#sidebar li[data-item="${item.id}"]`).first();
      await row.waitFor({ state: "attached" });
      // On a phone the list and the item are two screens, and the page opens on the item's.
      const back = page.locator(".phone-back:visible");
      if (await back.count()) await back.click();
      // The item's group, Busy, starts folded; its heading opens it.
      await row.evaluate((li) => {
        const group = li.closest("details");
        if (group && !group.open) group.querySelector(":scope > summary").click();
      });
      const dot = row.locator(".sidebar-working");
      const seen = await waitFor(() => dot.isVisible(), Boolean);
      check(`${width} px: the row of an item worked on through its step shows the working dot`, seen, `${await dot.count()} dots`);
      const words = seen ? await dot.getAttribute("aria-label") : "";
      check(`${width} px: the dot says a session is working on it`, words === "A session is working on this", words);
      await row.screenshot({ path: path.join(work, `step-lamp-${width}.png`) });
      check(`${width} px: no page errors`, errors.length === 0, errors.join(" | "));
      await page.unrouteAll({ behavior: "ignoreErrors" });
      await context.close();
    }
    console.error(`  screenshots in ${work}`);
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
