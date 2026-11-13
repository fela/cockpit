// Finished this week (SPEC VIEW-20). On a synthetic database, per width (1440 and 390 px) and colour scheme (light
// and dark):
// - All work has one group of finished work, Finished this week, and no Closed or Done this week group;
// - it lists a task the user closed as not needed, with "Closed as not needed by you" and the reason, and Unarchive;
// - each done task's second line says how it ended and who ended it;
// - the steps closed in the last day sit folded at its end, each with Reopen (VIEW-4);
// - its heading count is the tasks it lists;
// - the page has no horizontal scroll and no page errors.
// Then Unarchive takes the closed task out of the group and back to open work.
// Saves a screenshot of the group per width and scheme in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/finished_this_week.cjs
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

const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "finished-week", "content-type": "application/json" };
const TITLE = "Finished week: closed";
const REASON = "Alex took it over";

async function main() {
  const work = workFolder("cockpit-finished-week-");
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
    const call = async (who, method, url, body) => {
      const response = await fetch(`${info.origin}${url}`, { method, headers: who, body: body === undefined ? undefined : JSON.stringify(body) });
      if (!response.ok) throw new Error(`${method} ${url}: HTTP ${response.status} ${await response.text()}`);
      return response.json();
    };
    // A task the user closes as not needed from its finish step, with a reason.
    const key = `note:finished-week-${Date.now()}`;
    const item = await call(AGENT, "PUT", "/api/items", { key, title: TITLE, stage: "building", sessions: [SESSION_B] });
    const step = (await call(AGENT, "POST", "/api/steps", { item_key: key, need: "complete", ask: "Close it?", session_id: SESSION_B })).step;
    await call(headers, "POST", `/api/items/${item.id}/finish`, { step_id: step.id, how: "close", answer: null, note: "", reason: REASON, session_id: SESSION_B });

    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const scheme of ["light", "dark"]) {
      for (const width of [1440, 390]) {
        const tag = `${width} px ${scheme}`;
        const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme });
        await context.setExtraHTTPHeaders({ cookie: headers.cookie });
        const page = await context.newPage();
        page.setDefaultTimeout(8000);
        const errors = [];
        page.on("pageerror", (error) => errors.push(error.message));
        await page.goto(`${info.origin}/classic`);
        const group = page.locator("#work details.group.done");
        await group.waitFor({ timeout: 20000 });
        if (!(await group.evaluate((el) => el.open))) await group.locator(":scope > summary").click();
        const fold = group.locator(":scope > details.closed-steps");
        if (await fold.count()) await fold.locator(":scope > summary").click();
        await sleep(100);
        const seen = await page.evaluate(({ title }) => {
          const group = document.querySelector("#work details.group.done");
          const names = [...document.querySelectorAll("#work details.group")].map((each) => {
            const summary = each.querySelector(":scope > summary");
            const name = summary.querySelector(".group-name") || summary;
            return [...name.childNodes].filter((node) => node.nodeType === Node.TEXT_NODE).map((node) => node.textContent).join("").trim();
          });
          const rows = [...group.querySelectorAll(":scope > .list > .row")];
          const closed = rows.find((row) => row.querySelector(".row-title")?.textContent.includes(title));
          const done = rows.filter((row) => !row.classList.contains("closed-item"));
          return {
            names,
            count: Number(group.querySelector(":scope > summary .group-count").textContent),
            rows: rows.length,
            closedLine: closed?.querySelector(".ended-line")?.textContent || null,
            unarchive: Boolean(closed?.querySelector('button[aria-label^="Unarchive"]')),
            doneLines: done.map((row) => row.querySelector(".ended-line")?.textContent || ""),
            stepRows: group.querySelectorAll(":scope > details.closed-steps .closed-list li:not(.more-row)").length,
            reopen: group.querySelectorAll(':scope > details.closed-steps .closed-list button[aria-label^="Reopen"]').length,
            overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
            // Rows whose content reaches past the screen's right edge (rows run full-bleed past the group's box).
            past: rows.filter((row) => [...row.querySelectorAll("*")].some((el) => el.getBoundingClientRect().right > window.innerWidth + 1))
              .map((row) => row.querySelector(".row-title")?.textContent).slice(0, 3),
          };
        }, { title: TITLE });
        check(`${tag}: one group of finished work, last, and no Closed or Done this week`,
          seen.names.at(-1) === "Finished this week" && !seen.names.includes("Closed") && !seen.names.includes("Done this week"), JSON.stringify(seen.names));
        check(`${tag}: the closed task says it was closed as not needed by you, with the reason`,
          seen.closedLine === `Closed as not needed by you: ${REASON}`, String(seen.closedLine));
        check(`${tag}: the closed task has Unarchive`, seen.unarchive, "no Unarchive");
        const bare = seen.doneLines.filter((line) => !/^(Done by |Moved to done)/.test(line));
        check(`${tag}: each of ${seen.doneLines.length} done tasks says how it ended`, seen.doneLines.length > 0 && bare.length === 0, JSON.stringify(bare.slice(0, 3)));
        check(`${tag}: the heading counts the tasks listed`, seen.count === seen.rows, JSON.stringify([seen.count, seen.rows]));
        check(`${tag}: the day's closed steps are folded at its end, each with Reopen`, seen.stepRows > 0 && seen.reopen === seen.stepRows, JSON.stringify([seen.stepRows, seen.reopen]));
        check(`${tag}: no horizontal scroll`, seen.overflow <= 0, String(seen.overflow));
        check(`${tag}: every row fits on the screen`, seen.past.length === 0, JSON.stringify(seen.past));
        // The group's top (its heading, the closed task and the first done tasks) and the folded steps at its end.
        await group.evaluate((el) => window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - 120));
        // The page redraws on every state update; measure the group once it is back.
        await group.waitFor({ state: "visible" });
        const box = await group.boundingBox();
        const shot = path.join(work, `finished-${width}-${scheme}.png`);
        // A redraw after the scroll can leave the group's top a little above the window: the clip starts at the window's top.
        const top = Math.max(box.y, 0);
        await page.screenshot({ path: shot, clip: { x: 0, y: top, width, height: Math.min(box.y + box.height - top, page.viewportSize().height - top, 560) } });
        console.error(`  screenshot ${shot}`);
        const stepsShot = path.join(work, `closed-steps-${width}-${scheme}.png`);
        await screenshotRedrawn(fold, { path: stepsShot });
        console.error(`  screenshot ${stepsShot}`);

        if (scheme === "dark" && width === 390) {
          const row = group.locator(".row.closed-item").filter({ hasText: TITLE }).first();
          await row.locator('button[aria-label^="Unarchive"]').click();
          await page.waitForFunction((title) => ![...document.querySelectorAll("#work details.group.done .row-title")].some((el) => el.textContent.includes(title)), TITLE);
          await sleep(1500);
          const back = await call(headers, "GET", `/api/items?key=${encodeURIComponent(key)}`);
          check(`${tag}: Unarchive takes it out of the group and back to open work`, back.archived === false || back.archived === 0, JSON.stringify(back.archived));
        }
        check(`${tag}: no page errors`, errors.length === 0, errors.join("; "));
        await context.close();
      }
    }
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failed = results.filter((each) => !each.ok);
  console.error(`${failed.length ? `${failed.length} of ${results.length} checks failed` : `all ${results.length} checks passed`}`);
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
