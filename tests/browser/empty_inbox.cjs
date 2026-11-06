// The empty Inbox (SPEC UI-63). Once the last step is gone, the list at the left still shows the Inbox with its count 0
// and one "Nothing waiting" row, picked while the card shows the empty Inbox. The sweeper's ideas for a next step come
// as ordinary steps (NEXT-45), so the empty card lists nothing under its notice. Checks, at 1280 px dark and 390 px
// light:
// - Inbox shows with count 0 and its "Nothing waiting" row is picked (sel, aria-current);
// - the card shows the empty notice and nothing under it;
// - picking another item and then the "Nothing waiting" row brings the empty Inbox back, picked;
// - no page errors.
// Screenshots go to the work folder.
//
// Starts serve_preview.py (this checkout's Cockpit on a copy of the live database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/empty_inbox.cjs

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
  const work = workFolder("cockpit-empty-inbox-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    const user = { cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json" };
    const call = async (who, method, url, body) => {
      const response = await fetch(`${info.origin}${url}`, { method, headers: who, body: body === undefined ? undefined : JSON.stringify(body) });
      if (!response.ok) throw new Error(`${method} ${url}: HTTP ${response.status} ${await response.text()}`);
      return response.json();
    };
    // Empty the queue and ready next on the copy, so the Inbox is empty.
    let state = await call(user, "GET", "/api/state");
    for (const step of [...state.queue, ...state.ready_next]) await call(user, "POST", `/api/steps/${step.id}/dismiss`, {});
    state = await call(user, "GET", "/api/state");
    console.error(`  queue ${state.queue.length}, ready next ${state.ready_next.length} after dismissing`);

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const views = [{ name: "desktop-dark", width: 1280, height: 900, scheme: "dark" }, { name: "phone-light", width: 390, height: 844, scheme: "light" }];
    for (const view of views) {
      const context = await browser.newContext({ viewport: { width: view.width, height: view.height }, colorScheme: view.scheme });
      await context.setExtraHTTPHeaders({ cookie: user.cookie });
      const page = await context.newPage();
      page.setDefaultTimeout(30e3);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${info.origin}/`, { timeout: 60e3 });
      await page.locator(".sidebar-list li.inbox-empty").waitFor({ state: "attached" });
      await sleep(500);

      const sidebar = () => page.evaluate(() => {
        const empty = document.querySelector(".sidebar-list li.inbox-empty");
        const group = empty && empty.closest(".sidebar-group");
        const count = group && group.querySelector(".group-count");
        const row = empty && empty.querySelector(".side-row");
        return {
          shown: Boolean(empty),
          picked: Boolean(empty && empty.classList.contains("sel")),
          current: row ? row.getAttribute("aria-current") : null,
          count: count ? count.textContent.trim() : null,
          otherPicked: document.querySelectorAll(".sidebar-list li.sel:not(.inbox-empty)").length,
        };
      });
      // On a phone the list sits behind the menu; its contents are in the DOM all the same.
      const start = await sidebar();
      check(`${view.name}: Inbox shows with count 0 and its "Nothing waiting" row is picked`,
        start.shown && start.picked && start.count === "0" && start.otherPicked === 0, JSON.stringify(start));
      const card = await page.evaluate(() => {
        const next = document.querySelector("#next");
        return { notice: Boolean(next && next.querySelector(".empty-card")), children: next ? next.children.length : null, ideas: document.querySelectorAll(".ideas, .idea").length };
      });
      check(`${view.name}: the card shows the empty notice and nothing under it`, card.notice && card.children === 1 && card.ideas === 0, JSON.stringify(card));
      await page.screenshot({ path: path.join(work, `empty-inbox-${view.name}.png`), fullPage: true });

      if (view.width >= 600) {
        // Any item's row; its group may be folded.
        const other = await page.evaluate(() => {
          const row = document.querySelector(".sidebar-list li[data-item]");
          if (!row) return null;
          const details = row.closest("details");
          if (details) details.open = true;
          return row.dataset.item;
        });
        if (!other) throw new Error("no item row in the list");
        await page.locator(`.sidebar-list li[data-item="${other}"] > .side-row`).first().click();
        await sleep(500);
        const away = await sidebar();
        check(`${view.name}: picking an item unpicks the "Nothing waiting" row`, away.shown && !away.picked && away.current === null, JSON.stringify(away));
        await page.locator(".sidebar-list li.inbox-empty > .side-row").click();
        await sleep(500);
        const back = await sidebar();
        check(`${view.name}: the "Nothing waiting" row brings the empty Inbox back, picked`,
          back.picked && back.current === "true" && (await page.locator("#next .empty-card").count()) === 1, JSON.stringify(back));
        await page.locator(".sidebar").screenshot({ path: path.join(work, `empty-inbox-${view.name}-sidebar.png`) });
      }
      check(`${view.name}: no page errors`, errors.length === 0, errors.join(" | "));
      await context.close();
    }
    console.error(`  screenshots in ${work}`);
  } finally {
    if (browser) await browser.close();
    server.kill("SIGTERM");
  }
  const failed = results.filter((result) => !result.ok);
  console.error(failed.length ? `${failed.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failed.length ? 1 : 0;
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
