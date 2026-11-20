// Finished this week (SPEC VIEW-20). On a synthetic database, per width (1440 and 390 px) and colour scheme (light
// and dark):
// - the list on the left has one group of finished work, Finished this week, last, and no Closed or Done this week group;
// - it lists a task the user closed as not needed, whose card says "Closed as not needed by you" with the reason, and
//   has Unarchive;
// - each done task's card says how it ended and who ended it;
// - its heading count is the tasks it lists;
// - the page has no horizontal scroll and no page errors.
// Then Unarchive takes the closed task out of the group and back to open work.
// Saves screenshots of the group and of the closed task's card per width and scheme in the work dir.
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
        await page.goto(`${info.origin}/`);
        // A phone page opens on the item screen; the list is on the other screen.
        if (width < 600) await page.evaluate(() => setPhoneScreen(true, false));
        const group = page.locator("#sidebar details.sidebar-group.done");
        await group.waitFor({ timeout: 20000 });
        if (!(await group.evaluate((el) => el.open))) await group.locator(":scope > summary").click();
        await sleep(100);
        const seen = await page.evaluate(({ title }) => {
          const group = document.querySelector("#sidebar details.sidebar-group.done");
          const names = [...document.querySelectorAll("#sidebar details.sidebar-group > summary .sidebar-group-name")].map((name) => name.textContent.trim());
          const rows = [...group.querySelectorAll("li.side-item[data-item]")];
          const closed = rows.find((row) => row.querySelector(".sidebar-title")?.textContent.includes(title));
          return {
            names,
            count: Number(group.querySelector(":scope > summary .group-count").textContent),
            rows: rows.length,
            closed: closed ? Number(closed.dataset.item) : null,
            done: rows.filter((row) => row !== closed).map((row) => Number(row.dataset.item)),
            overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
            // Rows whose content reaches past the screen's right edge.
            past: rows.filter((row) => [...row.querySelectorAll("*")].some((el) => el.getBoundingClientRect().right > window.innerWidth + 1))
              .map((row) => row.querySelector(".sidebar-title")?.textContent).slice(0, 3),
          };
        }, { title: TITLE });
        check(`${tag}: one group of finished work, last, and no Closed or Done this week`,
          seen.names.at(-1) === "Finished this week" && !seen.names.includes("Closed") && !seen.names.includes("Done this week"), JSON.stringify(seen.names));
        check(`${tag}: the heading counts the tasks listed`, seen.count === seen.rows, JSON.stringify([seen.count, seen.rows]));
        check(`${tag}: no horizontal scroll`, seen.overflow <= 0, String(seen.overflow));
        check(`${tag}: every row fits on the screen`, seen.past.length === 0, JSON.stringify(seen.past));
        const shot = path.join(work, `finished-${width}-${scheme}.png`);
        await screenshotRedrawn(group, { path: shot });
        console.error(`  screenshot ${shot}`);

        // Each done task's card, picked as its row picks it, says how it ended.
        const doneLines = [];
        for (const id of seen.done) {
          await page.evaluate((item) => selectItem(item), id);
          const line = page.locator(`#next .item-card[data-item="${id}"] .ended-line`);
          doneLines.push(await line.first().textContent({ timeout: 3000 }).catch(() => ""));
        }
        const bare = doneLines.filter((line) => !/^(Done by |Moved to done)/.test(line));
        check(`${tag}: each of ${doneLines.length} done tasks says how it ended`, doneLines.length > 0 && bare.length === 0, JSON.stringify(bare.slice(0, 3)));

        // The closed task's card.
        check(`${tag}: the group lists the closed task`, seen.closed !== null, "not listed");
        if (seen.closed !== null) {
          await page.evaluate((item) => selectItem(item), seen.closed);
          const card = page.locator(`#next .item-card[data-item="${seen.closed}"] .row.closed-item`);
          await card.waitFor();
          const closedLine = await card.locator(".ended-line").textContent().catch(() => null);
          check(`${tag}: the closed task says it was closed as not needed by you, with the reason`,
            closedLine === `Closed as not needed by you: ${REASON}`, String(closedLine));
          const unarchive = card.locator('button[aria-label^="Unarchive"]');
          check(`${tag}: the closed task has Unarchive`, (await unarchive.count()) === 1, "no Unarchive");
          const cardShot = path.join(work, `closed-card-${width}-${scheme}.png`);
          await screenshotRedrawn(page.locator("#next"), { path: cardShot });
          console.error(`  screenshot ${cardShot}`);

          if (scheme === "dark" && width === 390) {
            await unarchive.click();
            await page.waitForFunction((id) => ![...document.querySelectorAll(`#sidebar li.side-item[data-item="${id}"]`)].some((el) => el.closest(".sidebar-group.done")), seen.closed);
            await sleep(1500);
            const back = await call(headers, "GET", `/api/items?key=${encodeURIComponent(key)}`);
            check(`${tag}: Unarchive takes it out of the group and back to open work`, back.archived === false || back.archived === 0, JSON.stringify(back.archived));
          }
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
