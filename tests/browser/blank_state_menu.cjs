// A session that Claude Code lists with no state shows on the classic page's All work with a dot that says its state is
// not known, and no error. A Next up menu that a redraw takes off the page before its toggle event arrives throws
// nothing.
//
// Starts serve_preview.py (this checkout's Cockpit on a copy of the live database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/blank_state_menu.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-blank-state-");
  fs.mkdirSync(path.join(work, "checkouts"), { recursive: true });
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });

    // Every session reads as Claude Code listing it with no state, so the loose ones in All work have none.
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/state*", async (route) => {
      const response = await route.fetch();
      const state = await response.json();
      for (const session of state.sessions) session.state = "";
      await route.fulfill({ response, json: state });
    });
    await page.goto(`${info.origin}/classic`);
    await page.waitForLoadState("networkidle");
    await sleep(1500);
    const rows = await page.locator(".session-row .dots .dot").evaluateAll((dots) => dots.map((dot) => [dot.className, dot.dataset.tip || dot.title]));
    check("the classic page shows sessions with no state", rows.length > 0 && errors.length === 0,
      JSON.stringify({ rows: rows.length, errors }));
    check("a session with no state has a dot that says its state is not known",
      rows.length > 0 && rows.every(([name, title]) => /\bunknown\b/.test(name) && title === "State not known"),
      JSON.stringify(rows.slice(0, 5)));
    // Grouped by state, they sit under a named group rather than one with no name.
    const groups = await page.evaluate(() => (typeof SESSION_STATE_GROUPS === "object" ? SESSION_STATE_GROUPS[""] : null));
    check("the state grouping names sessions with no state", groups === "State not known", JSON.stringify(groups));
    await page.close();

    // A redraw replaces Next up's card between the menu opening and its toggle event.
    const next = await context.newPage();
    const nextErrors = [];
    next.on("pageerror", (error) => nextErrors.push(error.message));
    await next.goto(`${info.origin}/`);
    await next.waitForLoadState("networkidle");
    await sleep(1500);
    const found = await next.evaluate(() => {
      const menu = document.querySelector(".next-card .step-context details.step-menu");
      if (!menu) return false;
      menu.open = true;
      menu.closest(".next-card").remove();
      return true;
    });
    await sleep(500);
    check("Next up has a menu to open", found, "no ⋯ menu in Next up's card (.step-context, VIEW-29)");
    check("a menu taken off the page before its toggle throws nothing", found && nextErrors.length === 0, JSON.stringify(nextErrors));
  } finally {
    if (browser) await browser.close();
    server.kill("SIGTERM");
  }
  const failed = results.filter((result) => !result.ok);
  console.error(`${results.length - failed.length}/${results.length} passed`);
  if (failed.length) process.exitCode = 1;
}

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
