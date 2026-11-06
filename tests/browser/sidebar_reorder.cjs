// On the page with the list at the left, the list follows the queue's ranked order while the card holds the step the
// user opened: a newer, stronger step moves to the top of Inbox at once, the rows glide to
// their new places, and the card stays on the user's step with Resorted. A press on Resorted shows the queue's first
// step on the card, also when the user had picked another item in the list.
//
// On a synthetic database, with the seeded steps snoozed, puts one step on each of three items (scores 60, 50,
// 40), presses the first on the card, then puts a step scored 95 on a fourth item. Then picks the third item in the
// list, puts a step scored 99 on a fifth item, and presses Resorted. Saves screenshots to the work folder.
//
// Starts serve_preview.py (README.md says what it needs) and drives a headless Chromium. Prints one line per check
// and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/sidebar_reorder.cjs
"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "sidebar-reorder", "content-type": "application/json" };
const RESORTED = "#next > .resorted-pin > .reorder.resorted";

async function main() {
  const work = workFolder("cockpit-sidebar-reorder-");
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
    const user = { cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json" };
    const call = async (who, method, url, body) => {
      const response = await fetch(`${info.origin}${url}`, { method, headers: who, body: body === undefined ? undefined : JSON.stringify(body) });
      if (!response.ok) throw new Error(`${method} ${url}: HTTP ${response.status} ${await response.text()}`);
      return response.json();
    };
    const copied = await call(user, "GET", "/api/state");
    await Promise.all([...copied.queue, ...copied.ready_next, ...copied.snoozed].map((step) => call(user, "POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    const stamp = Date.now();
    const putStep = async (title, ask, score) => {
      const key = `note:sidebar-reorder-${stamp}-${score}`;
      await call(AGENT, "PUT", "/api/items", { key, title, stage: "building", sessions: [SESSION_B] });
      return (await call(AGENT, "POST", "/api/steps", { item_key: key, need: "n", ask, session_id: SESSION_B, score })).step;
    };
    const steps = [
      await putStep("Add recurring reminders", "Change the batch job order?", 60),
      await putStep("Export fix", "Merge the export fix?", 50),
      await putStep("Export result", "Read the export result", 40),
    ];

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: "light" });
    await context.setExtraHTTPHeaders({ cookie: user.cookie });
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`${info.origin}/`);
    await page.locator(`#next [data-step="${steps[0].id}"]`).waitFor({ timeout: 20e3 });
    const inbox = () => page.evaluate(() => [...document.querySelectorAll("#sidebar .sidebar-group.queue li[data-step]")].map((li) => Number(li.dataset.step)));
    check("Inbox starts in score order", JSON.stringify(await inbox()) === JSON.stringify(steps.map((s) => s.id)), (await inbox()).join(","));

    // The user opens the first step on the card.
    await page.locator("#next .ask").click();
    await sleep(300);
    check("pressing the card holds its step", await page.evaluate((id) => ui.placed.has(id), steps[0].id), "not placed");

    // Rows that move while the list redraws are recorded with their animations.
    await page.evaluate(() => {
      window.glided = new Set();
      const look = () => {
        for (const li of document.querySelectorAll("#sidebar li[data-step]")) {
          if (li.getAnimations().some((a) => a.effect && a.effect.getKeyframes().some((k) => (k.translate && k.translate !== "0 0" && k.translate !== "0px 0px") || (k.transform && k.transform !== "none")))) window.glided.add(Number(li.dataset.step));
        }
      };
      new MutationObserver(() => { look(); requestAnimationFrame(look); setTimeout(look, 30); }).observe(document.getElementById("sidebar"), { childList: true, subtree: true });
    });
    const urgent = await putStep("Reminder dates missing", "Restore the reminder date?", 95);
    const moved = await page.waitForFunction((id) => Number(document.querySelector("#sidebar .sidebar-group.queue li[data-step]")?.dataset.step) === id,
      urgent.id, { timeout: 20e3 }).then(() => true, () => false);
    check("the stronger new step goes to the top of Inbox at once", moved, (await inbox()).join(","));
    check("the user's step moves down to its ranked place in Inbox", (await inbox())[1] === steps[0].id, (await inbox()).join(","));
    await sleep(400);
    const glided = await page.evaluate(() => [...window.glided]);
    check("the rows glide to their new places", glided.includes(steps[0].id) || glided.includes(urgent.id), `glided ${glided.join(",")}`);
    const card = await page.evaluate(() => ui.next && ui.next.id);
    check("the card stays on the user's step", card === steps[0].id, `card shows ${card}`);
    const marked = await page.evaluate((id) => document.querySelector(`#sidebar li[data-step="${id}"]`)?.classList.contains("sel")
      || Boolean(document.querySelector(`#sidebar li[data-step="${id}"] [aria-current="true"]`)), steps[0].id);
    check("the user's row stays marked as the one on the card", marked, "not marked");
    const shown = await page.waitForSelector(RESORTED, { timeout: 10e3 }).then(() => true, () => false);
    check("the card shows Resorted", shown, "no Resorted key");
    await page.screenshot({ path: path.join(work, "reordered-light.png") });
    await page.emulateMedia({ colorScheme: "dark" });
    await sleep(300);
    await page.screenshot({ path: path.join(work, "reordered-dark.png") });
    await page.emulateMedia({ colorScheme: "light" });

    // The user picks the third item in the list; a step stronger than every other arrives; Resorted shows the top.
    await page.locator(`#sidebar li[data-step="${steps[1].id}"] .side-row`).click();
    await page.locator(`#next [data-step="${steps[1].id}"]`).waitFor();
    const top = await putStep("Payments down", "Roll back the payments release?", 99);
    const reached = await page.waitForFunction((id) => ui.state.queue.some((s) => s.id === id), top.id, { timeout: 20e3 }).then(() => true, () => false);
    check("the strongest step reaches the queue", reached, await page.evaluate(() => ui.state.queue.map((s) => s.id).join(",")));
    const keyed = await page.waitForSelector(RESORTED, { timeout: 10e3 }).then(() => true, () => false);
    const stillPicked = await page.evaluate(() => ui.next && ui.next.id);
    check("the picked item's step stays on the card with Resorted", keyed && stillPicked === steps[1].id, `key ${keyed}, card ${stillPicked}`);
    if (keyed) {
      const first = await page.evaluate(() => ui.state.queue[0].id);
      await page.locator(RESORTED).click();
      const went = await page.waitForFunction((id) => ui.next && ui.next.id === id && document.querySelector(`#next [data-step="${id}"]`), first, { timeout: 3000 })
        .then(() => true, () => false);
      check("a press on Resorted shows the queue's first step on the card", went && first === top.id,
        `first ${first}, top ${top.id}, card ${await page.evaluate(() => ui.next && ui.next.id)}`);
      const marks = await page.evaluate(() => ({ sel: ui.selItem,
        row: Number(document.querySelector("#sidebar .sidebar-group.queue li.sel, #sidebar .sidebar-group.queue li:has([aria-current='true'])")?.dataset.step) }));
      check("the card follows the queue again and the list marks the top step's row", marks.sel === null && marks.row === top.id, JSON.stringify(marks));
      await page.screenshot({ path: path.join(work, "after-resorted.png") });
    }
    check("no page errors", errors.length === 0, errors.join("; "));
    console.error(`  screenshots in ${work}`);
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  if (results.some((r) => !r.ok)) process.exitCode = 1;
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
