// The morning briefing on its task's card, and a link to it opening it over Cockpit's page, as a shared page opens
// (SPEC NEXT-47, VIEW-31). On Cockpit's main page (`/` at desktop width in light, `/classic` at phone width in dark)
// it checks:
// - at desktop width, the briefing's read step holds the whole briefing, with no link away to it; selecting part of
//   it shows Comment, which quotes that part into the card's reply box; the briefing never loads ahead, so it is
//   still unread a while after the card shows;
// - a click on a link to the briefing on another step's card opens it in a layer over the whole window, with no new
//   tab, the main page's address kept, and the briefing alone in the layer: no header with Cockpit's tabs;
// - opening it marks the briefing read and leaves its read step open; the X closes the layer;
// - Escape in the layer closes it, and so does the browser's Back, which stays on the main page;
// - the list of earlier briefings in the layer keeps to the layer, and a click on another day leaves one Back to
//   close it;
// - a click with Ctrl (a new tab) leaves the main page as it is;
// - a click on a task the briefing mentions closes the layer and opens the task on the main page; a mention of a
//   task closed long ago, which the page lists nowhere, opens the main page's search of closed work for it;
// - no page errors and no Content Security Policy errors.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs), with
// yesterday's briefing stored and Cockpit having asked for today's (briefing.json in its work folder), and drives
// headless Chromium. Prints one line per check, saves screenshots in SHOTS_DIR (default: a temporary directory) and
// exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/briefing_overlay.cjs

"use strict";

const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "briefing-overlay-test", "content-type": "application/json" };
// Dates a task's archive a month back, so Finished this week no longer lists it.
const AGE_ARCHIVE = `
import sqlite3, sys
from datetime import datetime, timedelta, timezone
then = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
conn = sqlite3.connect(sys.argv[1])
conn.execute("UPDATE items SET archived_at = ? WHERE id = ?", (then, int(sys.argv[2])))
conn.commit()
`;
const SHOTS = process.env.SHOTS_DIR || fs.mkdtempSync(path.join(os.tmpdir(), "cockpit-briefing-overlay-shots-"));
const BODY = [
  "## Executive summary", "", "Sam shipped the CSV export, and Alex is halfway through the reminder settings.", "",
  "## Exports", "", "The export now names each column. Two customers asked for a weekly file.", "",
  "## Reminders", "", "The settings page saves each reminder on its own.",
].join("\n");

const EARLIER = "## Executive summary\n\nAlex opened the reminders ticket.";

const { chromium } = require(PLAYWRIGHT);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-briefing-overlay-");
  const day = new Date().toISOString().slice(0, 10);
  const yesterday = new Date(Date.now() - 86400e3).toISOString().slice(0, 10);
  fs.writeFileSync(path.join(work, "briefing.json"), JSON.stringify({ day, earlier: { [yesterday]: EARLIER } }));
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "ignore", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    const cookie = { cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json" };
    const agent = (method, url, body) => call(info.origin, AGENT, method, url, body);
    const user = (method, url, body) => call(info.origin, cookie, method, url, body);
    const initial = await user("GET", "/api/state");
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));

    // A task the briefing mentions by its number, which links to it.
    const target = await agent("PUT", "/api/items", { key: `note:briefing-overlay-mention-${Date.now()}`, title: "Weekly export file", sessions: [SESSION_B] });
    // And a task closed a month ago, which the page lists nowhere: its mention opens Cockpit's search for it.
    const offPage = await agent("PUT", "/api/items", { key: `note:briefing-overlay-old-${Date.now()}`, title: "Old export file", sessions: [SESSION_B] });
    await user("PATCH", `/api/items/${offPage.id}`, { archived: true });
    const aged = spawnSync(PYTHON, ["-c", AGE_ARCHIVE, path.join(work, "cockpit.db"), String(offPage.id)], { encoding: "utf8" });
    if (aged.status !== 0) throw new Error(`could not date the archive back: ${aged.stderr}`);
    const briefing = await agent("POST", "/api/briefings", { day, body: `${BODY}\n\nThe weekly file is item ${target.id}. The old export was item ${offPage.id}.` });
    const earlier = (await user("GET", "/api/briefings")).briefings.find((each) => each.day === yesterday);
    const readKey = `note:briefing-${day}`;
    const readItem = await agent("GET", `/api/items?key=${encodeURIComponent(readKey)}`);
    // The read step goes to the top of the queue, as the sweeper would put it in the morning.
    const readStep = await agent("POST", "/api/steps", { item_key: readKey, need: "read:briefing", score: 100 });
    check("Cockpit puts a step to read the briefing", readItem && readStep.step.id, JSON.stringify(readStep));

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const run = Date.now();
    const sizes = [["desktop", 1280, 900, "light", "/"], ["phone", 390, 844, "dark", "/classic"]];
    for (const [label, width, height, scheme, home] of sizes) {
      const key = `note:briefing-overlay-${run}-${label}`;
      const item = await agent("PUT", "/api/items", { key, title: `Briefing link ${label}`, priority: "urgent", sessions: [SESSION_B] });
      const answer = await agent("POST", "/api/steps", {
        item_key: key, need: `briefing-link-${label}`, ask: "Does the briefing say what Sam shipped?", score: 100, session_id: SESSION_B,
        body: `It is in [the morning briefing](${info.origin}/briefing?id=${briefing.id}).`,
      });
      const touch = width <= 900;
      const context = await browser.newContext({ viewport: { width, height }, colorScheme: scheme, hasTouch: touch, isMobile: touch });
      await context.addCookies([{ name: info.name, value: info.value, domain: new URL(info.origin).hostname, path: "/", secure: true, httpOnly: true, sameSite: "Lax" }]);
      const page = await context.newPage();
      // The fixture can open the "Back after" dialog over the page while the user's timer is idle;
      // answer it with Return whenever it shows, so it never takes the test's clicks.
      await page.addLocatorHandler(page.locator("dialog#idle[open]"), (dialog) => dialog.locator("button", { hasText: "Return" }).click()
        .catch((error) => { if (!/has been closed/.test(error.message)) throw error; }));
      page.setDefaultTimeout(20000);
      const errors = [];
      const watch = (target) => {
        target.on("pageerror", (error) => errors.push(error.message));
        target.on("console", (message) => { if (message.type() === "error" && /Content Security Policy/.test(message.text())) errors.push(message.text()); });
      };
      watch(page);
      const shot = (name) => page.screenshot({ path: path.join(SHOTS, `${label}-${scheme}-${name}.png`) });
      const at = `${label} (${scheme}, ${home})`;
      const dialog = page.locator("dialog.page-overlay[open]");
      const drawn = page.locator("dialog.page-overlay[open]:not(.page-overlay-loading-page)");
      const closed = async () => (await waitFor(() => dialog.count(), (count) => count === 0)) === 0;
      const tabs = () => context.pages().length;

      // Checks the layer the briefing opened in, once it has drawn.
      const inLayer = async (what) => {
        await drawn.waitFor();
        const layer = await openLayer(page);
        check(`${at}: ${what} opens the briefing in the layer`, layer.url().includes(`/briefing?id=${briefing.id}&overlay=1`), layer.url());
        await layer.locator(".briefing-doc h1").waitFor();
        const bare = await layer.evaluate(() => ({
          top: getComputedStyle(document.querySelector(".top")).display,
          heading: document.querySelector(".briefing-doc h1").getBoundingClientRect().top,
          text: document.querySelector(".briefing-body").textContent.includes("Sam shipped the CSV export"),
        }));
        check(`${at}: ${what}: the layer shows the briefing alone, without Cockpit's header and tabs`, bare.top === "none" && bare.text, JSON.stringify(bare));
        // The X sits in the layer's top right corner, over the briefing's margin, clear of its heading.
        const shut = await page.locator("dialog.page-overlay[open] .page-overlay-close").boundingBox();
        check(`${at}: ${what}: the briefing's heading starts under the layer's X`, shut && bare.heading >= shut.y + shut.height, JSON.stringify({ shut, heading: bare.heading }));
        await page.waitForFunction(() => document.querySelector("dialog.page-overlay[open]").getAnimations().length === 0);
        const box = await page.locator("dialog.page-overlay[open] iframe.page-overlay-frame").boundingBox();
        check(`${at}: ${what}: the layer fills the window`, box && box.x <= 1 && box.width >= width - 1 && box.height >= height - 1, JSON.stringify(box));
        check(`${at}: ${what}: no new tab, and the main page keeps its address`, tabs() === 1 && page.url() === before, `${tabs()} tabs, ${page.url()}`);
        return layer;
      };

      const openCard = async (card) => {
        await card.waitFor();
        if (await card.evaluate((el) => el.classList.contains("step-row") && !el.classList.contains("open"))) await card.locator(".row-head").click();
      };
      const linkIn = (card) => card.locator(`a[href*="/briefing?id=${briefing.id}"]:visible`).first();
      let before;

      if (label === "desktop") {
        // The read step holds the whole briefing, so the user reads and comments on all of it on the card.
        await page.goto(`${info.origin}${home}#home/item/${readItem.id}`);
        const readCard = page.locator(`#next [data-step="${readStep.step.id}"]`).first();
        await openCard(readCard);
        const words = await readCard.innerText();
        check(`${at}: the read step shows the whole briefing`, ["Sam shipped the CSV export", "Two customers asked for a weekly file",
          "saves each reminder on its own"].every((part) => words.includes(part)), words.slice(0, 400));
        const away = await readCard.locator('a[href*="/briefing"]').count();
        check(`${at}: with no link away to the briefing`, away === 0, `${away} links`);
        await sleep(4000);
        const ahead = await page.locator(`dialog.page-overlay-preload iframe[src*="/briefing"]`).count();
        const still = await user("GET", "/api/briefings");
        check(`${at}: the briefing does not load ahead, so it stays unread`, ahead === 0 && still.read_id < briefing.id, JSON.stringify({ ahead, read: still.read_id }));
        await shot("1-read-step");
        // Selecting part of it shows Comment, which quotes that part into the card's reply box.
        await readCard.locator("p", { hasText: "Two customers asked for a weekly file" }).first().click({ clickCount: 3 });
        const comment = page.locator(".quote-button");
        const offered = await comment.waitFor({ state: "visible", timeout: 5000 }).then(() => true, () => false);
        check(`${at}: selecting part of the briefing offers Comment`, offered, "no Comment button");
        await shot("1b-selected");
        if (offered) await comment.click();
        const reply = readCard.locator(`textarea[data-key="msg:step:${readStep.step.id}"]`);
        const quoted = await waitFor(() => reply.inputValue().catch(() => ""), (value) => value.includes("> "));
        check(`${at}: Comment quotes that part into the card's reply box`,
          quoted.includes("> The export now names each column. Two customers asked for a weekly file."), JSON.stringify(quoted));
        await shot("1c-comment");
        await reply.fill("");
      }

      // Another step's link to the briefing.
      await page.goto(`${info.origin}${home}${home === "/" ? `#home/item/${item.id}` : ""}`);
      const card = page.locator(`#next [data-step="${answer.step.id}"]`).first();
      await openCard(card);
      before = page.url();
      await linkIn(card).click();
      // The layer takes its place at once, named for the briefing, with a loading sheet until it has drawn.
      await dialog.waitFor();
      const title = await dialog.getAttribute("aria-label");
      check(`${at}: the layer is named for the briefing`, title === "Morning briefing", JSON.stringify(title));
      const layer = await inLayer("a step's link");
      await shot("2-open");
      if (label === "desktop") {
        const read = await waitFor(() => user("GET", "/api/briefings"), (data) => data.read_id === briefing.id);
        check(`${at}: opening the briefing marks it read`, read.read_id === briefing.id, `read ${read.read_id}`);
        const state = await user("GET", "/api/state");
        const open = [...state.queue, ...state.ready_next].some((step) => step.id === readStep.step.id);
        check(`${at}: and leaves its read step open, for Task done on the card`, open, "the read step closed");
        // The read step stays open, ahead of the other step; snoozed, it leaves /classic's single Next up to that step.
        await user("POST", `/api/steps/${readStep.step.id}/snooze`, { until: "2099-01-01T08:00:00Z" });
      }
      // Escape in the layer closes it.
      await layer.locator("body").click({ position: { x: 5, y: 200 } });
      await page.keyboard.press("Escape");
      check(`${at}: Escape closes the layer`, await closed(), "the layer stayed open");
      check(`${at}: the main page keeps its address after Escape`, page.url() === before, page.url());

      // The browser's Back closes it and stays on the main page.
      await openCard(card);
      await linkIn(card).click();
      const again = await inLayer("the link again");
      // The earlier briefings' list keeps to the layer; a click on a day replaces the briefing shown in it.
      const day1 = again.locator(`#days a[href^="/briefing?id=${earlier.id}"]`);
      await day1.waitFor();
      check(`${at}: the list of briefings links within the layer`, (await day1.getAttribute("href")).endsWith("&overlay=1"), await day1.getAttribute("href"));
      await day1.click();
      await page.waitForFunction((id) => document.querySelector("dialog.page-overlay[open] iframe.page-overlay-frame")
        ?.contentWindow?.location.search.includes(`id=${id}&`), earlier.id);
      const frame = await openLayer(page);
      await frame.locator(".briefing-body", { hasText: "Alex opened the reminders ticket" }).waitFor();
      check(`${at}: a day in the list stays in the layer, alone`, frame.url().includes(`id=${earlier.id}&overlay=1`)
        && (await frame.evaluate(() => getComputedStyle(document.querySelector(".top")).display)) === "none", frame.url());
      await shot("3-earlier-day");
      await page.goBack();
      check(`${at}: one Back closes the layer`, await closed(), "the layer stayed open");
      await sleep(300);
      check(`${at}: Back stays on the main page`, page.url() === before && tabs() === 1, `${page.url()}, ${tabs()} tabs`);
      await shot("4-closed");

      // A click with Ctrl opens a tab of its own and leaves the main page as it is.
      await openCard(card);
      const popup = context.waitForEvent("page", { timeout: 5000 }).catch(() => null);
      await linkIn(card).click({ modifiers: ["Control"] });
      const opened = await popup;
      await sleep(500);
      check(`${at}: a Ctrl click leaves the main page as it is`, (await dialog.count()) === 0 && page.url() === before, `${await dialog.count()} layers, ${page.url()}`);
      // A tab that opens after the wait above (a slow machine) is closed too, so later checks count one tab.
      if (!opened) await context.waitForEvent("page", { timeout: 20000 }).catch(() => null);
      for (const other of context.pages()) if (other !== page) await other.close();

      // A task the briefing mentions opens on the main page, and the layer closes, rather than the whole of
      // Cockpit loading inside the layer.
      await openCard(card);
      await linkIn(card).click();
      const mentioning = await inLayer("the link once more");
      const mention = mentioning.locator(`.briefing-body a.item-ref[href="/#item/${target.id}"]`);
      await mention.click();
      check(`${at}: a click on a task the briefing mentions closes the layer`, await closed(), "the layer stayed open");
      const shown = await waitFor(() => page.evaluate(() => document.querySelector(".keyboard-current")?.closest("[data-item]")?.dataset.item || null),
        (id) => id === String(target.id));
      check(`${at}: and opens that task on the main page, in the same tab`, shown === String(target.id) && tabs() === 1, JSON.stringify({ shown, tabs: tabs() }));
      check(`${at}: the layer's history entry is gone, and the main page keeps its address`,
        !(await page.evaluate(() => Boolean(history.state?.cockpitPageOverlay))) && new URL(page.url()).pathname === home, page.url());
      await shot("5-mention");

      // A mention of a task the page lists nowhere opens Cockpit's search for it, and that search's address stays
      // once the layer's history entry is gone. The page loads afresh on the step first: the mention above showed
      // another task in its place and left the address as it was, so going to that address again would do nothing.
      await page.goto("about:blank");
      await page.goto(`${info.origin}${home}${home === "/" ? `#home/item/${item.id}` : ""}`);
      await openCard(card);
      before = page.url();
      await linkIn(card).click();
      const searching = await inLayer("the link for the closed task");
      await searching.locator(`.briefing-body a.item-ref[href="/#item/${offPage.id}"]`).click();
      check(`${at}: a click on a closed task the briefing mentions closes the layer`, await closed(), "the layer stayed open");
      const found = `#find-closed/${encodeURIComponent(`#${offPage.id}`)}`;
      const address = await waitFor(() => new URL(page.url()).hash, (hash) => hash === found);
      check(`${at}: and the main page searches closed work for it, in the same tab`, address === found && tabs() === 1
        && !(await page.evaluate(() => Boolean(history.state?.cockpitPageOverlay))) && new URL(page.url()).pathname === home,
        JSON.stringify({ address, tabs: tabs(), url: page.url() }));
      await shot("6-closed-mention");

      check(`${at}: no page or policy errors`, errors.length === 0, errors.join(" | "));
      await context.close();
      // `/classic` shows the first queued step alone; this round's step moves out of the next round's way.
      await user("POST", `/api/steps/${answer.step.id}/snooze`, { until: "2099-01-01T08:00:00Z" });
    }
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failed = results.filter((result) => !result.ok);
  console.error(`${results.length - failed.length}/${results.length} checks passed; screenshots in ${SHOTS}`);
  if (failed.length) process.exitCode = 1;
}

// The frame of the open layer (a closed one stays hidden a few seconds).
async function openLayer(page) {
  const handle = await page.locator("dialog.page-overlay[open]:not(.page-overlay-loading-page) iframe.page-overlay-frame").elementHandle();
  const frame = await handle.contentFrame();
  await frame.waitForLoadState("load");
  return frame;
}

async function waitFor(read, done) {
  let value;
  for (let tries = 0; tries < 60; tries++) {
    value = await read();
    if (done(value)) return value;
    await sleep(100);
  }
  return value;
}

async function call(origin, headers, method, url, body) {
  const response = await fetch(`${origin}${url}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await response.text();
  if (!response.ok) throw new Error(`${method} ${url}: ${response.status} ${text}`);
  return text ? JSON.parse(text) : null;
}

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
