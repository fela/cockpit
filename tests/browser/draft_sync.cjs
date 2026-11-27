// Drafts follow the user between devices (drafts.py, draft-sync.js). Two browser contexts stand for a laptop
// (1280 px, light) and a phone (390 px, dark): each keeps its own storage, so only Cockpit carries words between them.
// Checks:
// - words typed in a step's reply box on the laptop show in the same box on the phone, and Cockpit holds them;
// - the phone's box, while the user types in it, keeps its own words; when both devices changed the box, both hold
//   both texts once the user leaves it, the words that reached Cockpit first coming first;
// - emptying the box on one device empties it on the other;
// - words typed on the phone while it was offline join the laptop's words when it comes back, the laptop's first,
//   and the joined words reach the laptop too;
// - a phone opened afresh shows the draft;
// - a second laptop tab resting in the box leaves the words the first tab adds;
// - when Cockpit takes the laptop's words but the laptop misses the answer, and the phone then empties the box, the
//   box stays empty on both devices and on Cockpit;
// - words typed on the laptop while its last words are on their way, then the page goes away before their answer
//   comes, reach Cockpit and the phone;
// - words Cockpit failed to take (a 503) reach Cockpit and the phone when the page goes away straight after;
// - no page errors.
// Saves screenshots of both devices holding the same draft in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/draft_sync.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
// The harness's agent token and second fake session (tests/conftest.py).
const SESSION_B = "22222222-2222-4222-8222-222222222222";
const TOKEN = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "draft-sync-test" };
// A poll every 5 s and a push 0.8 s after the last change: words cross within two polls.
const CROSS_MS = 15e3;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-draft-sync-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    const cookie = { cookie: `${info.name}=${info.value}`, origin: info.origin };
    const agent = (method, url, body) => call(info.origin, { ...TOKEN, "content-type": "application/json" }, method, url, JSON.stringify(body));
    const user = (method, url, body) => call(info.origin, { ...cookie, "content-type": "application/json" }, method, url,
      body === undefined ? undefined : JSON.stringify(body));

    // The fixture database's open steps are snoozed, so this run's step is on the card.
    const initial = await user("GET", "/api/state");
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    const run = Date.now();
    const key = `note:draft-sync-${run}`;
    await agent("PUT", "/api/items", { key, title: "Pick the reminder time", priority: "urgent" });
    const step = (await agent("POST", "/api/steps", {
      item_key: key, need: `draft-sync-${run}`, ask: "Which time suits the reminder?", score: 100, session_id: SESSION_B,
      body: "Say a time.",
    })).step.id;
    const entry = `drafts/msg:step:${step}`;

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const errors = [];
    const device = async (width, height, scheme) => {
      const context = await browser.newContext({ viewport: { width, height }, colorScheme: scheme });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.setDefaultTimeout(5000);
      page.on("pageerror", (error) => errors.push(`${width} px: ${error.message}`));
      await page.goto(`${info.origin}/`, { timeout: 30e3 });
      const box = page.locator(`#next [data-step="${step}"] textarea[data-key="msg:step:${step}"]`).first();
      // The first drawing can take a while on a busy host.
      await box.waitFor({ timeout: 30e3 });
      return { context, page, box };
    };
    const laptop = await device(1280, 900, "light");
    const phone = await device(390, 844, "dark");
    // Waits until the box holds `words`, then says whether it did.
    const holds = async ({ page }, words, wait = CROSS_MS) => page.waitForFunction(
      ([selector, expected]) => document.querySelector(selector)?.value === expected,
      [`#next [data-step="${step}"] textarea[data-key="msg:step:${step}"]`, words], { timeout: wait, polling: 200 },
    ).then(() => true, () => false);
    const leave = async ({ page }) => page.evaluate(() => document.activeElement?.blur());

    const first = "Nine in the morning works";
    await laptop.box.click();
    await laptop.page.keyboard.type(first);
    check("words typed on the laptop show in the same box on the phone", await holds(phone, first), await phone.box.inputValue());
    const kept = (await user("GET", "/api/state")).drafts.entries[entry];
    check("Cockpit holds the words", kept?.[0] === JSON.stringify(first), JSON.stringify(kept));
    await laptop.page.screenshot({ path: path.join(work, "draft-sync-laptop.png") });
    await phone.page.screenshot({ path: path.join(work, "draft-sync-phone.png") });

    // The phone's user types in the box while the laptop's words change: the box keeps the phone's words.
    await phone.box.click();
    await phone.page.keyboard.press("End");
    await phone.page.keyboard.type(", or ten");
    await laptop.box.fill("");
    await laptop.box.click();
    await laptop.page.keyboard.type("Noon instead");
    await sleep(7e3);
    check("the box the phone's user types in keeps their words", await phone.box.inputValue() === `${first}, or ten`,
      await phone.box.inputValue());
    await leave(laptop);
    await leave(phone);
    // Both changed the box from the same words. Which device's words reached Cockpit first depends on timing, and
    // those come first.
    const joinings = [`${first}, or ten\n\nNoon instead`, `Noon instead\n\n${first}, or ten`];
    const joinedBoth = await waitFor(async () => joinings.includes(await phone.box.inputValue())
      && await laptop.box.inputValue() === await phone.box.inputValue());
    check("after both leave the box, both devices hold both texts, the first to reach Cockpit first", joinedBoth,
      `${JSON.stringify(await phone.box.inputValue())} / ${JSON.stringify(await laptop.box.inputValue())}`);

    await laptop.box.fill("");
    await leave(laptop);
    check("emptying the box on the laptop empties it on the phone", await holds(phone, ""), await phone.box.inputValue());
    const gone = (await user("GET", "/api/state")).drafts.entries[entry];
    check("Cockpit's state holds no emptied box", gone === undefined, JSON.stringify(gone));

    // Offline, the phone keeps its words in the browser; back online they join the laptop's.
    await phone.context.setOffline(true);
    await phone.box.click();
    await phone.page.keyboard.type("Phone: after lunch");
    await leave(phone);
    await laptop.box.click();
    await laptop.page.keyboard.type("Laptop: half past eight");
    await leave(laptop);
    await sleep(3e3);
    await phone.context.setOffline(false);
    const offlineJoined = "Laptop: half past eight\n\nPhone: after lunch";
    check("words typed offline on the phone join the laptop's when it comes back, on both devices",
      await holds(phone, offlineJoined) && await holds(laptop, offlineJoined),
      `${JSON.stringify(await phone.box.inputValue())} / ${JSON.stringify(await laptop.box.inputValue())}`);

    const fresh = await device(390, 844, "light");
    check("a device opened afresh shows the draft", await holds(fresh, offlineJoined), await fresh.box.inputValue());

    // A second laptop tab rests in the box, holding the words it had, while the first tab adds to them: what
    // Cockpit keeps and the phone shows are the first tab's words.
    const tab = await laptop.context.newPage();
    tab.on("pageerror", (error) => errors.push(`second tab: ${error.message}`));
    await tab.goto(`${info.origin}/`, { timeout: 30e3 });
    const tabBox = tab.locator(`#next [data-step="${step}"] textarea[data-key="msg:step:${step}"]`).first();
    await tabBox.waitFor({ timeout: 30e3 });
    await tabBox.click();
    await laptop.box.click();
    await laptop.page.keyboard.press("Control+End");
    await laptop.page.keyboard.type(", or nine");
    await leave(laptop);
    await sleep(12e3);
    const added = `${offlineJoined}, or nine`;
    const held = (await user("GET", "/api/state")).drafts.entries[entry];
    check("a tab resting in the box leaves the words another tab added on Cockpit and the phone",
      held?.[0] === JSON.stringify(added) && await holds(phone, added),
      `${JSON.stringify(held)} / ${JSON.stringify(await phone.box.inputValue())}`);
    await tab.close();

    // Cockpit takes the laptop's words, but their answer stays away until the phone has emptied the box, then is lost.
    let release;
    const released = new Promise((resolve) => { release = resolve; });
    await laptop.page.route("**/api/drafts", async (route) => {
      await route.fetch();
      await released;
      await route.abort();
    }, { times: 1 });
    const lost = "Eight sharp";
    await laptop.box.fill(lost);
    await leave(laptop);
    const phoneSaw = await holds(phone, lost);
    await phone.box.fill("");
    await leave(phone);
    const emptied = await waitFor(async () => (await user("GET", "/api/state")).drafts.entries[entry] === undefined);
    release();
    await sleep(12e3);
    const after = (await user("GET", "/api/state")).drafts.entries[entry];
    check("an emptied box stays empty after the laptop missed the answer to its words",
      phoneSaw && emptied && after === undefined && await laptop.box.inputValue() === "" && await holds(phone, ""),
      `${phoneSaw} ${emptied} ${JSON.stringify(after)} / ${JSON.stringify(await laptop.box.inputValue())} / ${JSON.stringify(await phone.box.inputValue())}`);

    // The laptop's request for "Ten" is on its way when the user types more and the page goes away.
    let reached;
    const onItsWay = new Promise((resolve) => { reached = resolve; });
    await laptop.page.route("**/api/drafts", async (route) => {
      const response = await route.fetch();
      reached();
      await sleep(5e3);
      await route.fulfill({ response }).catch(() => {});
    }, { times: 1 });
    await laptop.box.click();
    await laptop.page.keyboard.type("Ten");
    await onItsWay;
    await laptop.page.keyboard.type(" o'clock");
    // Past the push's wait, so the page has no push left to make, only words waiting behind the slow one.
    await sleep(2e3);
    await laptop.page.evaluate(() => dispatchEvent(new PageTransitionEvent("pagehide")));
    await laptop.page.close();
    const last = "Ten o'clock";
    const reachedCockpit = await waitFor(async () => (await user("GET", "/api/state")).drafts.entries[entry]?.[0] === JSON.stringify(last));
    check("words typed while the last ones were on their way reach Cockpit and the phone as the page goes away",
      reachedCockpit && await holds(phone, last),
      `${JSON.stringify((await user("GET", "/api/state")).drafts.entries[entry])} / ${JSON.stringify(await phone.box.inputValue())}`);

    // Cockpit fails on the laptop's next words, and the page goes away before the next state would send them again.
    const failing = await laptop.context.newPage();
    failing.on("pageerror", (error) => errors.push(`failing tab: ${error.message}`));
    await failing.goto(`${info.origin}/`, { timeout: 30e3 });
    const failingBox = failing.locator(`#next [data-step="${step}"] textarea[data-key="msg:step:${step}"]`).first();
    await failingBox.waitFor({ timeout: 30e3 });
    // The closed tab's last request goes again from this one first; the failure is for the words typed next.
    await failing.waitForFunction(() => !JSON.parse(localStorage.getItem("cockpit.drafts-synced")).unanswered.length,
      null, { timeout: CROSS_MS });
    await failing.route("**/api/drafts", (route) => route.fulfill({ status: 503, body: "{}" }), { times: 1 });
    const failed = failing.waitForResponse((response) => response.url().endsWith("/api/drafts") && response.status() === 503);
    const retried = "Eleven, after all";
    await failingBox.fill(retried);
    await failed;
    await failing.evaluate(() => dispatchEvent(new PageTransitionEvent("pagehide")));
    await failing.close();
    const retriedCockpit = await waitFor(async () => (await user("GET", "/api/state")).drafts.entries[entry]?.[0] === JSON.stringify(retried));
    check("words Cockpit failed to take reach Cockpit and the phone when the page goes away straight after",
      retriedCockpit && await holds(phone, retried),
      `${JSON.stringify((await user("GET", "/api/state")).drafts.entries[entry])} / ${JSON.stringify(await phone.box.inputValue())}`);

    // An entry of a kind only a newer page keeps stays on Cockpit while this page sends its own changes, also once a
    // newer tab in the same browser recorded it: this page holds no value of it, so a change it sent would empty it.
    const future = "future-kind/poll";
    const futureValue = JSON.stringify({ value: 3 });
    const stored = await user("POST", "/api/drafts", { id: `future-${run}`, follows: [], changes: [{ key: future, value: futureValue, base: 0 }] });
    await phone.page.evaluate(([name, held]) => {
      const record = JSON.parse(localStorage.getItem("cockpit.drafts-synced"));
      record.entries.push([name, held]);
      localStorage.setItem("cockpit.drafts-synced", JSON.stringify(record));
    }, [future, stored.entries[future]]);
    const later = "Ten works as well";
    await phone.box.fill(later);
    await leave(phone);
    const sentLater = await waitFor(async () => (await user("GET", "/api/state")).drafts.entries[entry]?.[0] === JSON.stringify(later));
    const futureKept = (await user("GET", "/api/state")).drafts.entries[future];
    check("an entry of a kind the page does not keep stays on Cockpit while the page sends its own",
      sentLater && futureKept?.[0] === futureValue, `${sentLater} / ${JSON.stringify(futureKept)}`);
    check("no page errors", errors.length === 0, errors.join(" | "));
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

// Waits up to CROSS_MS until `test` holds, then says whether it did.
async function waitFor(test) {
  const end = Date.now() + CROSS_MS;
  while (Date.now() < end) {
    if (await test()) return true;
    await sleep(300);
  }
  return false;
}

async function call(origin, headers, method, url, body) {
  const response = await fetch(`${origin}${url}`, { method, headers, body });
  const text = await response.text();
  if (!response.ok) throw new Error(`${method} ${url}: ${response.status} ${text}`);
  return text ? JSON.parse(text) : null;
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
