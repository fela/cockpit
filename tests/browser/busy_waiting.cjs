// Busy and Waiting below the queue on a synthetic board (SPEC VIEW-27).

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const OLD_GROUPS = ["people", "bots", "merge", "agents", "nobody", "handed"];


const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "browser-check", "content-type": "application/json" };

async function call(info, headers, method, url, body) {
  const response = await fetch(`${info.origin}${url}`, { method, headers: { ...headers, connection: "close" },
    body: body === undefined ? undefined : JSON.stringify(body) });
  if (!response.ok) throw new Error(`${method} ${url}: HTTP ${response.status} ${await response.text()}`);
  return response.json();
}

async function main() {
  const work = workFolder("cockpit-busy-waiting-");
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
    const until = new Date(Date.now() + 24 * 3600e3).toISOString();
    await call(info, AGENT, "PUT", "/api/items", {
      key: "note:busy-reminders", title: "Build weekly reminders", stage: "building", sessions: [SESSION_B],
      doing: "Prepare a reminder preview",
    });
    await call(info, AGENT, "PUT", "/api/items", {
      key: "note:waiting-sharing", title: "Review list sharing", stage: "review",
      waiting_on: [{ kind: "person", who: "Alex", what: "review", until }],
    });
    const state = await fetch(`${info.origin}/api/state`, { headers: { cookie } }).then((response) => response.json());
    const items = new Map(state.items.map((item) => [String(item.id), item]));

    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const [page_path, groupSel, rowSel, hoverSel] of [
      ["/", (group) => `#sidebar details.sidebar-group.${group}`, "li[data-item]", ".side-row"],
      ["/classic", (group) => `#work details.group.${group}`, ".row[data-item]", ".row-title"],
    ]) {
      const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, colorScheme: "light" });
      await context.setExtraHTTPHeaders({ cookie });
      const page = await context.newPage();
      // The fixture can open the "Back after" dialog over the page while the user's timer is idle; answer it
      // with Return whenever it shows, so it never takes the test's clicks.
      await page.addLocatorHandler(page.locator("dialog#idle[open]"), (dialog) => dialog.locator("button", { hasText: "Return" }).click()
        // The test may close its page while an answer is under way.
        .catch((error) => { if (!/has been closed/.test(error.message)) throw error; }));
      page.setDefaultTimeout(45000);
      page.setDefaultNavigationTimeout(60000);
      const errors = [];
      page.on("pageerror", (error) => { errors.push(error.message); console.error(`page error: ${error.message}`); });
      await page.goto(`${info.origin}${page_path}`);
      await page.locator(groupSel("waiting")).waitFor({ state: "attached" });
      for (const group of ["busy", "waiting"]) {
        const open = await page.locator(groupSel(group)).evaluate((el) => el.open);
        check(`${page_path}: ${group} starts folded`, !open, "open");
      }
      const old = await page.evaluate((names) => names.filter((name) => document.querySelector(`details.${name}`)), OLD_GROUPS);
      check(`${page_path}: no group by who holds the ball`, old.length === 0, old.join(", "));
      for (const group of ["waiting", "busy"]) {
        await page.locator(`${groupSel(group)} > summary`).click();
        // A snoozed step's row in Waiting says when it wakes instead (snoozed_waiting.cjs).
        const row = page.locator(`${groupSel(group)} ${rowSel}:not(.snoozed)`).first();
        const item = items.get(await row.getAttribute("data-item"));
        // The page adds the time in the user's zone to a line with tip_at; the check reads the line's start.
        const expected = (one) => one.tip;
        await row.locator(hoverSel).first().hover();
        const tip = page.locator(".tip");
        await waitFor(() => tip.evaluate((el) => !el.hidden), (shown) => shown);
        const seen = await tip.evaluate((el) => ({ text: el.textContent, hidden: el.hidden, bg: getComputedStyle(el).backgroundColor }));
        check(`${page_path}: a ${group} row's hover shows "${expected(item)}"`, !seen.hidden && seen.text.includes(expected(item)), JSON.stringify(seen));
        const [r, g, b] = seen.bg.match(/[\d.]+/g).map(Number);
        check(`${page_path}: the ${group} tooltip is dark`, r + g + b < 200, seen.bg);
        // Both groups are open by this point; one image per layout records them together.
        if (group === "busy") await page.screenshot({
          path: path.join(work, `${page_path === "/" ? "sidebar" : "classic"}-groups.png`), animations: "disabled",
        });
        await page.mouse.move(0, 0);
        await sleep(300);
      }
      check(`${page_path}: no page errors`, errors.length === 0, errors.join(" | "));
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
  const end = Date.now() + 120e3;
  while (Date.now() < end) {
    if (server.exitCode !== null) throw new Error(`serve_preview.py exited with ${server.exitCode}`);
    if (fs.existsSync(file)) {
      const info = JSON.parse(fs.readFileSync(file, "utf8"));
      const up = await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false);
      if (up) return info;
    }
    await sleep(200);
  }
  throw new Error("serve_preview.py did not come up within 120 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
