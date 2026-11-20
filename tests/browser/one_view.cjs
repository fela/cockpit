// One view (SPEC VIEW-18): every item shows once in the list on the left, in its first group. Puts five steps for the
// user, three items that wait on someone other than the user, and a step handed to an agent, then checks, at desktop
// and phone width:
// - Inbox is the first group, Finished this week the last, and there is no Waiting for you group;
// - every item shows once, a handed step's item too, and an item with a step in Inbox, Ready next or Snoozed shows as
//   that step;
// - the handed step shows in its item's group, Busy or Waiting;
// - every group's heading count matches the rows it shows when open;
// - opening an item that waits for the user shows its step on the card, whose Task details fold holds the task's
//   controls and where it stands;
// - an old item link (#board/item/N) opens the item on Cockpit too, and leaves the address once followed, so a reload
//   after the user moved on to another task shows that task (regression check).
// Saves a screenshot of the list per width in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and
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
    const initial = await call(headers, "GET", "/api/state");
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((step) => call(headers, "POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    const run = Date.now();
    for (let n = 1; n <= 5; n += 1) {
      const key = `note:one-view-mine-${run}-${n}`;
      await call(AGENT, "PUT", "/api/items", { key, title: `One view: yours ${n}`, stage: "review", summary: `Waits for your look at part ${n}` });
      await call(AGENT, "POST", "/api/steps", { item_key: key, need: "decide", ask: `One view: decide part ${n}?`, score: 100 - n, session_id: SESSION_B });
    }
    // Three items in progress that wait on someone other than the user, in Waiting.
    const waiting = [];
    for (const [n, who] of ["review-bot", "Alex", "Alex"].entries()) {
      const key = `note:one-view-waiting-${run}-${n}`;
      await call(AGENT, "PUT", "/api/items", { key, title: `One view: waits on ${who}`, stage: "building", waiting_on: [{ kind: who === "review-bot" ? "bot" : "person", who, until: "2099-01-01T08:00:00Z" }] });
      waiting.push(key);
    }
    const state = await call(headers, "GET", "/api/state");
    const picked = waiting.map((key) => state.items.find((item) => item.key === key));
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
      await page.goto(`${info.origin}/`);
      await page.locator("#sidebar li.side-item[data-item]").first().waitFor({ state: "attached", timeout: 20e3 });

      // Every group opened, with Inbox's ready-next steps shown too.
      await page.evaluate(() => { for (const each of document.querySelectorAll("#sidebar details.sidebar-group")) each.open = true; });
      const more = page.locator("#sidebar .sidebar-more .side-row");
      if (await more.count() && (await more.getAttribute("aria-expanded")) === "false") await more.evaluate((el) => el.click());
      await page.waitForFunction(() => !document.querySelector("#sidebar .sidebar-more .side-row[aria-expanded='false']"));

      const seen = await page.evaluate((handedStepId) => {
        const groups = [...document.querySelectorAll("#sidebar details.sidebar-group")];
        const keyOf = (group) => [...group.classList].find((cls) => cls !== "sidebar-group");
        const ids = [...document.querySelectorAll("#sidebar li[data-item]")].map((row) => Number(row.dataset.item));
        // The steps the list shows as their item's row.
        const steps = [...ui.state.queue, ...ui.state.ready_next, ...ui.state.snoozed].filter((step) => !step.left);
        const handed = document.querySelector(`#sidebar li[data-step="${handedStepId}"]`);
        return {
          groups: groups.map(keyOf),
          twice: ids.filter((id, at) => ids.indexOf(id) !== at),
          notAsStep: steps.filter((step) => !document.querySelector(`#sidebar li[data-item="${step.item_id}"][data-step="${step.id}"]`)).map((step) => step.id),
          handed: handed ? { item: Number(handed.dataset.item), group: keyOf(handed.closest("details.sidebar-group")) } : null,
          counts: groups.map((group) => ({
            group: keyOf(group),
            count: Number(group.querySelector(":scope > summary .group-count").textContent),
            shown: group.querySelectorAll(":scope > ul > li:not(.sidebar-sub):not(.sidebar-more)").length,
          })),
        };
      }, handedStep.id);
      check(`${width} px: Inbox is the first group`, seen.groups[0] === "queue", JSON.stringify(seen.groups));
      check(`${width} px: Finished this week is the last group`, seen.groups.at(-1) === "done", JSON.stringify(seen.groups));
      check(`${width} px: there is no Waiting for you group`, !seen.groups.includes("you"), JSON.stringify(seen.groups));
      check(`${width} px: every item shows once`, seen.twice.length === 0, JSON.stringify(seen.twice));
      check(`${width} px: an item with a step in Inbox, Ready next or Snoozed shows as that step`, seen.notAsStep.length === 0, JSON.stringify(seen.notAsStep));
      check(`${width} px: the handed step shows in its item's group`,
        seen.handed && seen.handed.item === handedItem.id && ["busy", "waiting"].includes(seen.handed.group), JSON.stringify(seen.handed));
      const off = seen.counts.filter((entry) => entry.count !== entry.shown);
      check(`${width} px: each of ${seen.counts.length} groups shows as many rows as its count`, seen.counts.length >= 3 && off.length === 0, JSON.stringify(off));

      // The list is the phone's list screen.
      if (width <= 1000) await page.locator(".phone-bar .phone-back").click();
      const shot = path.join(work, `list-${width}.png`);
      await page.locator("#sidebar").screenshot({ path: shot });
      console.error(`  screenshot ${shot}`);

      // An item that waits for the user shows as its step on the card, with Task details in the card's menu (VIEW-29),
      // which draws the fold open.
      const mine = await page.evaluate(() => {
        const step = [...ui.state.queue, ...ui.state.ready_next].find((one) => one.ask === "One view: decide part 3?");
        return step && { item: step.item_id, step: step.id };
      });
      if (!mine) throw new Error("the run's third step is not in Inbox or Ready next; the check needs it");
      await page.locator(`#sidebar li[data-step="${mine.step}"] .side-row`).click();
      const card = page.locator(`#next [data-step="${mine.step}"]`).first();
      await card.waitFor();
      check(`${width} px: opening an item that waits for you shows its step on the card`, await card.isVisible(), "not shown");
      await card.locator(".step-menu > summary").click();
      await card.locator(".step-menu").getByRole("menuitem", { name: "Task details" }).click();
      const fold = card.locator(".task-details");
      await fold.waitFor();
      const task = await fold.evaluate((el, id) => {
        const item = ui.state.items.find((one) => one.id === id);
        const note = item.summary && item.summary !== item.title ? item.summary : "";
        return {
          pin: !!el.querySelector(".item-controls .pin-key"), stage: !!el.querySelector(".item-controls .stage-menu"),
          close: !!el.querySelector(".close-item"),
          // Where it stands: the stage chip (naming the review), the summary, the latest status.
          stands: !!el.querySelector(".task-stands .chip"),
          note: !note || (el.querySelector(".item-note")?.textContent === note),
          status: !item.agent_summary || (el.querySelector(".item-status")?.textContent || "").includes(item.agent_summary),
        };
      }, mine.item);
      check(`${width} px: the step's card folds in the task's pin and stage, and no second Close`, task.pin && task.stage && !task.close, JSON.stringify(task));
      check(`${width} px: the step's card folds in where the task stands`, task.stands && task.note && task.status, JSON.stringify(task));

      await page.goto(`${info.origin}/#board/item/${picked[2].id}`);
      const linked = await waitFor(() => page.evaluate((id) => ({
        picked: !!document.querySelector(`#sidebar li.side-item.sel[data-item="${id}"]`), shown: ui.shownItem,
      }), picked[2].id), (now) => now.picked);
      check(`${width} px: an old #board/item link opens the item on Cockpit`, linked.picked && linked.shown === picked[2].id, JSON.stringify(linked));
      check(`${width} px: the link's task route leaves the address once followed`, new URL(page.url()).hash === "", page.url());
      // The user moves on to another task, then reloads (Reload after a deploy): the page shows that task, not the one
      // the link once named (regression check).
      if (width <= 1000) await page.locator(".phone-bar .phone-back").click();
      await page.locator(`#sidebar li[data-step="${mine.step}"] .side-row`).click();
      await page.locator(`#next [data-step="${mine.step}"]`).first().waitFor();
      await page.reload({ timeout: 20e3 });
      await page.waitForFunction(() => ui.state && ui.shownItem, null, { timeout: 20e3 });
      await sleep(1500);
      const reloaded = await page.evaluate(() => ui.shownItem);
      check(`${width} px: a reload after moving on shows the task moved to, not the linked one`, reloaded === mine.item, JSON.stringify({ reloaded, linked: picked[2].id, mine: mine.item }));
      if (width <= 1000) {
        // A phone that opens the link in a new tab puts the list's entry before the task's. Back reaches the list, and
        // no entry keeps the link's address, so Back does not follow the link again (regression check).
        const fresh = await context.newPage();
        fresh.setDefaultTimeout(5000);
        fresh.on("pageerror", (error) => errors.push(error.message));
        await fresh.goto(`${info.origin}/#home/item/${picked[2].id}`, { timeout: 20e3 });
        await fresh.waitForFunction((id) => ui.shownItem === id && !location.hash, picked[2].id, { timeout: 20e3 });
        // Back steps through the task's entry, then the item screen's, to the list's. An entry that kept the link's
        // address would bring it back as a hashchange, which the page then follows again.
        await fresh.evaluate(() => { window.replayed = []; addEventListener("hashchange", (event) => replayed.push(new URL(event.newURL).hash)); });
        for (let step = 0; step < 2; step += 1) {
          await fresh.goBack();
          await sleep(500);
        }
        const back = await fresh.evaluate(() => ({ replayed, list: document.body.classList.contains("phone-list") }));
        check(`${width} px: Back from a link opened in a new tab reaches the list without the link's address`, back.replayed.length === 0 && back.list, JSON.stringify(back));
        await fresh.close();
      }
      check(`${width} px: no page errors`, errors.length === 0, errors.join(" | "));
      await context.close();
    }
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
