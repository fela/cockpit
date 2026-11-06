// Only rows that came into Busy in the last minute are lit. On the page with the list at
// its left, checks:
// - when the page opens, no Busy row is lit except one whose item was made in the last minute;
// - a Busy item moved to Waiting and back comes in lit, first in Busy, and no other row is lit;
// - a reload within the minute keeps it lit;
// - once its minute is over, the page draws it plain again by itself.
// Saves a screenshot of the lit row in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/busy_arrivals.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "busy-arrivals-test", "content-type": "application/json" };

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-busy-arrivals-");
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
    const handedItems = new Set(state.with_agents.map((step) => step.item_id));
    // A to-do added in the last half hour stays at the top of Busy whatever its group (sidebar.js freshTodos).
    const target = state.items.find((item) => item.group === "busy" && !item.archived && !item.open_steps
      && (!/^todo-/.test(item.key) || Date.now() - Date.parse(item.created_at) > 31 * 60e3)
      && !handedItems.has(item.id) && item.waiting_on.some((entry) => entry.kind === "agent"));
    if (!target) throw new Error("no Busy item without steps that waits on an agent in the copy");
    const put = (waiting_on) => fetch(`${info.origin}/api/items`, { method: "PUT", headers: AGENT,
      body: JSON.stringify({ key: target.key, waiting_on }) }).then(async (response) => {
      if (!response.ok) throw new Error(`put ${response.status}: ${await response.text()}`);
    });

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, colorScheme: "light" });
    await context.setExtraHTTPHeaders({ cookie });
    const page = await context.newPage();
    await page.addLocatorHandler(page.locator("dialog#idle[open]"), (dialog) => dialog.locator("button", { hasText: "Return" }).click()
      .catch((error) => { if (!/has been closed/.test(error.message)) throw error; }));
    page.setDefaultTimeout(5000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const busy = "#sidebar details.sidebar-group.busy";
    const lit = () => page.evaluate((sel) => [...document.querySelectorAll(`${sel} li.side-item.fresh`)].map((li) => li.dataset.item), busy);
    const row = page.locator(`${busy} li.side-item[data-item="${target.id}"]`);

    await page.goto(`${info.origin}/`);
    await page.locator(busy).waitFor({ state: "attached", timeout: 30e3 });
    await page.locator(`${busy} > summary`).click();
    const made = new Map(state.items.map((item) => [String(item.id), Date.parse(item.created_at)]));
    const litAtOpen = await lit();
    check("at open only items made in the last minute are lit", litAtOpen.every((id) => Date.now() - made.get(id) < 70e3),
      litAtOpen.join(", "));

    await put([{ kind: "person", who: "Alex", what: "review", until: "2026-01-15T10:00:00Z" }]);
    await waitFor(() => row.count(), (count) => count === 0, 20e3);
    check("moved to Waiting, it leaves Busy", (await row.count()) === 0, "still in Busy");
    await put(target.waiting_on);
    const cameAt = Date.now();
    await waitFor(() => row.count(), (count) => count === 1, 20e3);
    const seen = await row.evaluate((li) => ({ fresh: li.classList.contains("fresh"), first: li.parentElement.firstElementChild === li,
      pulse: getComputedStyle(li.querySelector(".side-row")).animationName }));
    check("back in Busy, it is lit and first", seen.fresh && seen.first && seen.pulse === "fresh-pulse", JSON.stringify(seen));
    const others = (await lit()).filter((id) => id !== String(target.id));
    check("no other row is lit", others.length === 0, others.join(", "));
    await row.screenshot({ path: path.join(work, "busy-arrival.png") });
    await page.locator(busy).screenshot({ path: path.join(work, "busy-group.png") });

    await page.reload();
    await page.locator(busy).waitFor({ state: "attached", timeout: 30e3 });
    if (!(await page.locator(busy).evaluate((el) => el.open))) await page.locator(`${busy} > summary`).click();
    await row.waitFor({ state: "attached", timeout: 15e3 });
    check("a reload within the minute keeps it lit", await row.evaluate((li) => li.classList.contains("fresh")), "plain after reload");

    await sleep(Math.max(0, cameAt + 66e3 - Date.now()));
    const after = await row.evaluate((li) => li.classList.contains("fresh"));
    check("after its minute it is plain again", !after, "still lit");
    check("no page errors", errors.length === 0, errors.join(" | "));
  } finally {
    if (browser) await browser.close();
    server.kill("SIGTERM");
  }
  const failed = results.filter((result) => !result.ok).length;
  console.error(`${results.length - failed} passed, ${failed} failed`);
  if (failed) process.exitCode = 1;
}

async function waitFor(read, done, ms = 5000) {
  const end = Date.now() + ms;
  let value = await read();
  while (!done(value) && Date.now() < end) {
    await sleep(200);
    value = await read();
  }
  return value;
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
