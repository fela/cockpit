// A session linked to an item only through its open step lights the item's lamp. Gives one agents' item no sessions
// of its own and a working session through its step (`step_sessions`) in the page's state responses, then checks, at
// desktop and phone width, that the item's lamp on its stage's plate at the top of the classic page is lit and that
// its entry says it uses Claude now.  Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and drives a headless Chromium. Prints one line per check and exits 1 when
// one failed.  Usage, from the checkout: node tests/browser/step_lamp.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

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
        const response = await route.fetch().catch(() => null);
        if (!response) return;
        const body = await response.json();
        for (const each of body.items) {
          if (each.id === item.id) Object.assign(each, { sessions: [], step_sessions: [linked], stalled_since: null });
        }
        for (const session of body.sessions) {
          if (session.session_id === linked) {
            Object.assign(session, { state: "working", working_now: true, alive: true, waits_on_dialog: false });
          }
        }
        await route.fulfill({ response, json: body }).catch(() => {});
      });

      await page.goto(`${info.origin}/classic`);
      const group = page.locator("#overview .light-group").filter({ has: page.locator(".light-item", { hasText: item.title }) }).first();
      await group.waitFor({ state: "attached" });
      const entry = group.locator(".light-item", { hasText: item.title }).first();
      const classes = await waitFor(() => entry.getAttribute("class"), (value) => !/\bdim\b/.test(value));
      check(`${width} px: the plate lamp of an item worked on through its step is lit`, !/\bdim\b/.test(classes), classes);
      await group.hover();
      if (width < 641) await group.click();
      await entry.waitFor();
      const words = await entry.getAttribute("aria-label");
      check(`${width} px: its entry on the plate says it uses Claude now`, words.includes("using Claude now"), words);
      await page.screenshot({ path: path.join(work, `step-lamp-${width}.png`) });
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
