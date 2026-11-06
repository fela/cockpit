// One view (SPEC VIEW-18) with Matters most (SPEC VIEW-17). Puts a list of three items that wait on someone other than
// the user and hands one step to an agent, then checks, at desktop and phone width:
// - the queue holds only the user's own groups: no Agents on it and no In progress there;
// - the work beyond the queue continues its list: no heading, no line about it, no rule, and its first group sits as
//   close to the queue as the queue's own groups sit to each other;
// - no group repeats the queue: there is no Waiting for you group, and no item with a step on Next up, in the queue,
//   Ready next or Snoozed also has a row below, also when its step is a result to read;
// - only the group whose name does not say what it holds (No one) has a line under its heading, and Finished this week
//   is the last group, with no Closed group beside it (VIEW-20);
// - Matters most is the first group below the queue, with each item's title and the sweep's line, in the order put; a
//   long line wraps instead of being cut off;
// - You handed these shows the handed step at the top of its item's group; its arrow key opens the item's row;
// - every item shows once, a handed step's item too;
// - every group's heading count matches the tasks it shows when open;
// - the Sessions tab scrolls down to the list of work on the same page, which holds the sessions too;
// - opening an item from elsewhere (a status light, the Pinned list) opens its row under Matters most, and opening one
//   that waits for the user opens its step, whose card folds in the task's controls and sessions;
// - an old item link (#board/item/N) opens the item on Cockpit too;
// - an empty list hides Matters most, and its items go back to their groups.
// Saves a screenshot of the work below the queue per width in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a copy of the live database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/one_view.cjs
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

const LINES = [
  "review-bot checks the reminder change",
  "Alex is checking whether shared lists keep their reminder dates when exported",
  "Ana chooses a default reminder time for shared lists",
];
const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "one-view", "content-type": "application/json" };

async function main() {
  const work = workFolder("cockpit-one-view-");
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
    // The fixture database's open steps are snoozed, and five steps of this run's own fill the user's four places and
    // wait in Waiting next: the queue then has the groups Waiting next and Snoozed.
    const copied = await call(headers, "GET", "/api/state");
    await Promise.all([...copied.queue, ...copied.ready_next, ...copied.snoozed].map((step) => call(headers, "POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    const run = Date.now();
    for (let n = 1; n <= 5; n += 1) {
      const key = `note:one-view-mine-${run}-${n}`;
      await call(AGENT, "PUT", "/api/items", { key, title: `One view: yours ${n}`, stage: "review", summary: `Waits for your look at part ${n}` });
      await call(AGENT, "POST", "/api/steps", { item_key: key, need: "decide", ask: `One view: decide part ${n}?`, score: 100 - n, session_id: SESSION_B });
    }
    // Three items in progress that wait on someone other than the user, for Matters most.
    const waiting = [];
    for (const [n, who] of ["review-bot", "Alex", "Alex"].entries()) {
      const key = `note:one-view-waiting-${run}-${n}`;
      await call(AGENT, "PUT", "/api/items", { key, title: `One view: waits on ${who}`, stage: "building", waiting_on: [{ kind: who === "review-bot" ? "bot" : "person", who, until: "2099-01-01T08:00:00Z" }] });
      waiting.push(key);
    }
    const state = await call(headers, "GET", "/api/state");
    const picked = waiting.map((key) => state.items.find((item) => item.key === key));
    const put = (entries) => fetch(`${info.origin}/api/in-progress`, { method: "PUT", headers, body: JSON.stringify({ entries }) });
    const stored = await put(picked.map((item, index) => ({ item_id: item.id, line: LINES[index] })));
    check("the list is stored", stored.ok, `HTTP ${stored.status}: ${await stored.text()}`);
    // A step the user hands to an agent.
    const handedKey = `note:one-view-${Date.now()}`;
    const handedItem = await call(AGENT, "PUT", "/api/items", { key: handedKey, title: "One view: handed", stage: "building", sessions: [SESSION_B] });
    const handedStep = (await call(AGENT, "POST", "/api/steps", { item_key: handedKey, need: "decide", ask: "One view: hand this over?", session_id: SESSION_B })).step;
    await call(headers, "POST", `/api/steps/${handedStep.id}/hand-off`, { text: "Go ahead" });

    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const width of [1280, 390]) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: "dark" });
      await context.setExtraHTTPHeaders({ cookie: headers.cookie });
      const page = await context.newPage();
      page.setDefaultTimeout(5000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${info.origin}/classic`);
      const group = page.locator("#work details.group.in-progress");
      await group.locator(".row").first().waitFor();

      const seen = await page.evaluate((handedItemId) => {
        const group = document.querySelector("#work details.group.in-progress");
        const groups = [...document.querySelectorAll("#work details.group")];
        const handed = document.querySelector("#work .with-agents");
        const rows = [...document.querySelectorAll("#work .row[data-item]")].map((row) => Number(row.dataset.item));
        const work = document.getElementById("work");
        const queue = document.getElementById("queue");
        const queueGroups = [...queue.querySelectorAll(":scope > details.group")];
        // Items with a step on Next up, in the queue, Ready next or Snoozed: they show as that step.
        const stepItems = new Set([...ui.state.queue, ...ui.state.ready_next, ...ui.state.snoozed].map((step) => step.item_id));
        return {
          queueGroups: queueGroups.map((each) => each.className),
          heading: [...work.querySelectorAll(":scope > .section-head, :scope > .group-caption, :scope > h2")].map((el) => el.textContent),
          rule: getComputedStyle(work).borderTopStyle,
          // The list starts with its Sort, Group and New session row (VIEW-21).
          gap: Math.round(work.firstElementChild.getBoundingClientRect().top - queue.getBoundingClientRect().bottom),
          queueGap: queueGroups.length > 1 ? Math.round(queueGroups[1].getBoundingClientRect().top - queueGroups[0].getBoundingClientRect().bottom) : null,
          youGroup: document.querySelectorAll("details.group.you").length,
          repeated: rows.filter((id) => stepItems.has(id)),
          lastGroup: groups.at(-1).className,
          closed: document.querySelectorAll("details.group.closed").length,
          captioned: groups.filter((each) => each.querySelector(":scope > .group-caption")?.textContent).map((each) => each.className),
          first: groups[0] === group,
          head: [group.querySelector(".group-name").firstChild.textContent, group.querySelector(".group-count").textContent].join(" "),
          entries: [...group.querySelectorAll(":scope > .list > .row")].map((row) => ({
            id: Number(row.dataset.item), title: row.querySelector(".row-title").textContent, line: row.querySelector(".row-sub").textContent,
          })),
          handedShown: Boolean(handed && handed.querySelector(`li[data-step]`) && handed.querySelector(".handed-head").textContent.startsWith("You handed these")),
          handedRow: rows.includes(handedItemId),
          twice: rows.filter((id, at) => rows.indexOf(id) !== at),
        };
      }, handedItem.id);
      check(`${width} px: the queue holds only the user's groups`,
        seen.queueGroups.every((cls) => !/with-agents|in-progress/.test(cls)), JSON.stringify(seen.queueGroups));
      check(`${width} px: no heading or line sets the work below the queue apart`, seen.heading.length === 0 && seen.rule === "none", JSON.stringify([seen.heading, seen.rule]));
      check(`${width} px: the list below starts as close to the queue as the queue's groups to each other`,
        seen.queueGap !== null && Math.abs(seen.gap - seen.queueGap) <= 4, JSON.stringify([seen.gap, seen.queueGap]));
      check(`${width} px: there is no Waiting for you group`, seen.youGroup === 0, String(seen.youGroup));
      check(`${width} px: no item with a step in the queue, Ready next or Snoozed has a row below`, seen.repeated.length === 0, JSON.stringify(seen.repeated));
      check(`${width} px: Finished this week is the last group, and there is no Closed group`, /\bdone\b/.test(seen.lastGroup) && seen.closed === 0, JSON.stringify([seen.lastGroup, seen.closed]));
      check(`${width} px: only the No one group has a line under its heading`, seen.captioned.every((cls) => /\bnobody\b/.test(cls)), JSON.stringify(seen.captioned));
      check(`${width} px: Matters most is the first group below the queue`, seen.first, "another group first");
      check(`${width} px: it is headed Matters most 3`, seen.head === "Matters most 3", seen.head);
      const expected = picked.map((item, index) => ({ id: item.id, title: item.title, line: LINES[index] }));
      check(`${width} px: titles and lines show in the order put`, JSON.stringify(seen.entries) === JSON.stringify(expected),
        JSON.stringify(seen.entries));
      check(`${width} px: the handed step shows under You handed these`, seen.handedShown, "not there");
      check(`${width} px: the handed step's item has its own row`, seen.handedRow, "no row for it");
      check(`${width} px: every item shows once`, seen.twice.length === 0, JSON.stringify(seen.twice));
      const long = group.locator(".row").nth(1).locator(".row-sub");
      const wraps = await long.evaluate((el) => ({ cut: el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1 }));
      check(`${width} px: a long line is not cut off`, !wraps.cut, JSON.stringify(wraps));

      // Every group opened: its heading count is the rows under it.
      const counts = await page.evaluate(async () => {
        const groups = [...document.querySelectorAll("#queue details.group, #work details.group")];
        for (const each of groups) each.open = true;
        await new Promise((resolve) => setTimeout(resolve, 50));
        return groups.map((each) => {
          const rows = each.querySelectorAll(":scope > .list > [data-step], :scope > .list > [data-item], :scope > ul > li:not(.more-row), :scope > .with-agents > ul > li");
          // A group that lists only its newest names the rest on its "Show N more" button. Finished this week counts
          // its tasks; the closed steps folded at its end have their own count.
          const more = Number((each.querySelector(":scope > ul > li.more-row")?.textContent.match(/Show (\d+) more/) || [0, 0])[1]);
          const shown = [...rows].filter((row) => row.getBoundingClientRect().height > 0).length + more;
          return { group: each.className, count: Number(each.querySelector(".group-count").textContent), shown };
        });
      });
      const off = counts.filter((entry) => entry.count !== entry.shown);
      check(`${width} px: each of ${counts.length} groups shows as many rows as its count`, counts.length >= 3 && off.length === 0, JSON.stringify(off));
      await page.reload();
      await group.locator(".row").first().waitFor();

      const views = await page.evaluate(() => ({ work: getComputedStyle(document.getElementById("work")).display }));
      check(`${width} px: the work shows on Cockpit`, views.work !== "none", JSON.stringify(views));
      const shot = path.join(work, `work-${width}.png`);
      await page.locator("#work").screenshot({ path: shot });
      console.error(`  screenshot ${shot}`);

      // The handed step's arrow key opens its item.
      // A handed step shows in its item's group: Busy, or Waiting once its agent stopped (VIEW-27).
      await page.evaluate(() => { for (const g of document.querySelectorAll("#work details.group.busy, #work details.group.waiting")) g.open = true; });
      await page.locator(`#work .with-agents li[data-step="${handedStep.id}"] .handed-open`).click();
      const handedOpen = await waitFor(() => page.evaluate((id) => !!document.querySelector(`#work .row.open[data-item="${id}"]`), handedItem.id), (now) => now);
      check(`${width} px: the handed step's arrow key opens its item`, handedOpen, "not opened");

      // A status light or the Pinned list opens the item through openInWork with the item's own group.
      await page.evaluate(() => { document.querySelector("#work details.group.in-progress").open = false; });
      await page.evaluate(([id, group]) => window.openInWork(id, group), [picked[0].id, picked[0].group]);
      const opened = await waitFor(() => page.evaluate((id) => ({
        open: !!document.querySelector(`#work details.group.in-progress[open] .row.open[data-item="${id}"]`),
      }), picked[0].id), (now) => now.open);
      check(`${width} px: opening an item from elsewhere opens its row under Matters most`, opened.open, JSON.stringify(opened));

      // An item that waits for the user opens as its step: Next up's card, or its row in the queue or Ready next,
      // opened, with Task details folded into the card.
      const mine = await page.evaluate(() => {
        const step = [...ui.state.queue, ...ui.state.ready_next].find((one) => !ui.next || one.item_id !== ui.next.item_id);
        return step && { item: step.item_id, step: step.id };
      });
      if (!mine) throw new Error("the fixture database has no step past Next up; the check needs one");
      const stepShown = await page.evaluate(async (id) => {
        const shown = await window.openInWork(id, "you");
        return shown ? { step: Number(shown.dataset.step), open: shown.classList.contains("open") } : null;
      }, mine.item);
      check(`${width} px: opening an item that waits for you opens its step`, stepShown && stepShown.step === mine.step && stepShown.open, JSON.stringify(stepShown));
      // Task details is an entry of the card's ⋯ menu (VIEW-29), which draws the fold open.
      const card = page.locator(`#queue .row[data-step="${mine.step}"]`);
      await card.locator(".step-menu > summary").click();
      await card.locator(".step-menu").getByRole("menuitem", { name: "Task details" }).click();
      const fold = page.locator(`#queue .row[data-step="${mine.step}"] .task-details`);
      await fold.waitFor();
      const task = await fold.evaluate((el, id) => {
        const item = ui.state.items.find((one) => one.id === id);
        const note = item.summary && item.summary !== item.title ? item.summary : "";
        return {
          pin: !!el.querySelector(".item-controls .pin-key"), stage: !!el.querySelector(".item-controls .stage-menu"),
          close: !!el.querySelector(".close-item"),
          // Where it stands, as the row showed it: the stage chip (naming the review), the summary, the latest status.
          stands: !!el.querySelector(".task-stands .chip"),
          note: !note || (el.querySelector(".item-note")?.textContent === note),
          status: !item.agent_summary || (el.querySelector(".item-status")?.textContent || "").includes(item.agent_summary),
        };
      }, mine.item);
      check(`${width} px: the step's card folds in the task's pin and stage, and no second Close`, task.pin && task.stage && !task.close, JSON.stringify(task));
      check(`${width} px: the step's card folds in where the task stands`, task.stands && task.note && task.status, JSON.stringify(task));

      // Sessions is the list of work lower on the same page (VIEW-3): its tab scrolls down to it and reads as
      // current there. The list rises to just under the header, or as far as the page's end lets it.
      await page.locator(".page-tabs a[data-view='sessions']").click();
      const board = await waitFor(() => page.evaluate(() => {
        const header = document.querySelector("header").getBoundingClientRect().bottom;
        const top = document.getElementById("work").getBoundingClientRect().top;
        const atEnd = window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2;
        return {
          hash: location.hash, top: Math.round(top), header: Math.round(header), atEnd,
          work: getComputedStyle(document.getElementById("work")).display,
          current: document.querySelector(".page-tabs [aria-current]")?.dataset.view,
          sessionsPanel: !!document.getElementById("sessions"),
        };
      }), (now) => (now.top < now.header + 40 || now.atEnd) && now.current === "sessions");
      check(`${width} px: the Sessions tab scrolls down to the list of work on the same page`,
        board.hash === "#sessions" && (board.top < board.header + 40 || board.atEnd) && board.work !== "none" && board.current === "sessions" && !board.sessionsPanel,
        JSON.stringify(board));

      await page.goto(`${info.origin}/classic#board/item/${picked[2].id}`);
      const linked = await waitFor(() => page.evaluate((id) => ({
        open: !!document.querySelector(`#work .row.open[data-item="${id}"]`),
      }), picked[2].id), (now) => now.open);
      check(`${width} px: an old #board/item link opens the item on Cockpit`, linked.open, JSON.stringify(linked));
      check(`${width} px: no page errors`, errors.length === 0, errors.join(" | "));
      await context.close();
    }

    const cleared = await put([]);
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    await context.setExtraHTTPHeaders({ cookie: headers.cookie });
    const page = await context.newPage();
    await page.goto(`${info.origin}/classic`);
    await page.locator("#work details.group").first().waitFor();
    await sleep(500);
    const after = await page.evaluate((ids) => ({
      groups: document.querySelectorAll("#work details.group.in-progress").length,
      back: ids.every((id) => document.querySelector(`#work .row[data-item="${id}"]`)),
    }), picked.map((item) => item.id));
    check("an empty list hides Matters most, and its items go back to their groups", cleared.ok && after.groups === 0 && after.back, JSON.stringify(after));
    await context.close();
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
