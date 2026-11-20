// An item picked in the list shows its history on the card compactly. Checks, on an item of its own whose five steps
// the user closed one by one (four exchanges, no step waiting for the user): - the item's card shows Earlier folded,
// with at most two exchanges outside it; - opening Earlier shows the rest; - at phone width the page has no sideways
// scroll.
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed. Screenshots go to the folder
// given as the first argument, when one is given.  Usage, from the checkout: node
// tests/browser/item_history_compact.cjs [screenshot folder]

"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { startPreview } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

// The harness's agent token (tests/conftest.py).
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "history-test", "content-type": "application/json" };

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const shots = process.argv[2] || null;
  if (shots) fs.mkdirSync(shots, { recursive: true });
  const { info } = await startPreview("cockpit-item-history-");
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const cookie = { cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json" };
    const user = (method, url, body) => call(info.origin, cookie, method, url, body);
    const agent = (method, url, body) => call(info.origin, AGENT, method, url, body);

    // An item in progress whose steps the agent put and the user marked done, a second apart so each is an exchange
    // of its own. The steps name no session, so Done archives none. Each Done's answer is the next step; the last
    // Done has none and folds into the entry before it as a note (history.py, _fold_presses): four exchanges.
    const key = `note:item-history-${Date.now()}`;
    await agent("PUT", "/api/items", { key, title: "History test", stage: "building", summary: "The agent works on part six" });
    for (let n = 1; n <= 5; n += 1) {
      const { step } = await agent("POST", "/api/steps", { item_key: key, need: `decide:part-${n}`, ask: `Take part ${n} as it is?` });
      await user("POST", `/api/steps/${step.id}/done`, {});
      await sleep(1100);
    }
    const state = await user("GET", "/api/state");
    const item = state.items.find((entry) => entry.key === key);
    const history = await user("GET", `/api/items/${item.id}/history`);
    const pick = { item, entries: history.entries.length, start: history.start ? 1 : 0 };
    if (pick.entries !== 4) throw new Error(`the item's history has ${pick.entries} exchanges, not the four its steps made`);

    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const [label, viewport] of [["desktop", { width: 1440, height: 1000 }], ["phone", { width: 390, height: 844 }]]) {
      const context = await browser.newContext({ viewport });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      await page.goto(`${info.origin}/`, { waitUntil: "networkidle" });
      // On a phone the list and the card are two screens, and the page opens on the card's.
      const back = page.locator(".phone-back:visible");
      if (await back.count()) await back.click();
      const listed = page.locator(`#sidebar li[data-item="${pick.item.id}"] > .side-row`);
      // The item's group can be folded; showInSidebar opens it, as search and the keys do.
      await page.waitForFunction((id) => typeof window.showInSidebar === "function" && document.querySelector(`#sidebar li[data-item="${id}"]`), pick.item.id);
      if (!(await listed.isVisible())) await page.evaluate((id) => window.showInSidebar(id), pick.item.id);
      await listed.click();
      const card = page.locator("#next .card").filter({ has: page.locator(`section.history[data-item="${pick.item.id}"]`) }).first();
      await card.locator("section.history").waitFor({ timeout: 60e3 });
      await sleep(300);
      const outside = await card.locator("section.history > ol > li.hist-entry").count();
      const fold = card.locator("section.history > ol > li.hist-earlier");
      check(`${label}: two exchanges or fewer outside Earlier`, outside <= 2, `${outside} shown`);
      check(`${label}: Earlier folded`, await fold.count() === 1 && !(await fold.locator("details").first().evaluate((el) => el.open)), "no closed Earlier");
      if (shots) await card.screenshot({ path: path.join(shots, `item-history-${label}.png`) });
      await fold.locator("summary").first().click();
      const inside = await fold.locator("li.hist-entry").count();
      check(`${label}: Earlier holds the rest`, outside + inside === pick.entries + pick.start, `${outside} + ${inside} of ${pick.entries} and ${pick.start} start line`);
      const wide = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      check(`${label}: no sideways scroll`, wide <= 0, `${wide} px too wide`);
      await context.close();
    }
  } finally {
    if (browser) await browser.close();
  }
  const failed = results.filter((result) => !result.ok);
  console.error(`${results.length - failed.length} of ${results.length} checks passed`);
  if (failed.length) process.exitCode = 1;
}

async function call(origin, headers, method, url, body) {
  const response = await fetch(`${origin}${url}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await response.text();
  if (!response.ok) throw new Error(`${method} ${url}: ${response.status} ${text}`);
  return text ? JSON.parse(text) : null;
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
