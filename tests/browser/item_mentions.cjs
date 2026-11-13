// A mention of another item in a step's text is a link to that item (SPEC UI-74). Puts its own step, whose body
// mentions an item with a project logo, an item of Cockpit's own work and a step on a third item, into the user's queue
// on the sidebar page, and checks at 1440 and 390 px:
// - each mention is a link that names its item on hover (its title attribute) and leads with its project's mark: the
//   project's logo, or Cockpit's key on Cockpit's work;
// - the step's mention names the item the step is on;
// - pressing a mention opens that item on the card;
// - a link to an item closed before this week, opened in a tab of its own, shows that item through Search;
// - no page errors.
// Saves a screenshot of the card per width in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/item_mentions.cjs

"use strict";

const path = require("node:path");
const { startPreview, screenshotRedrawn } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const { chromium } = require(PLAYWRIGHT);

const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "item-mentions-test", "content-type": "application/json" };

async function main() {
  const { work, info } = await startPreview("cockpit-item-mentions-");
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const headers = { cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json" };
    const call = async (method, url, auth, body) => {
      const response = await fetch(info.origin + url, { method, headers: auth, body: body === undefined ? undefined : JSON.stringify(body) });
      if (!response.ok) throw new Error(`${method} ${url}: HTTP ${response.status}: ${await response.text()}`);
      return response.json();
    };
    // Allocate two-digit IDs because one-digit mentions are ordinary prose.
    for (let n = 0; n < 10; n++) await call("PUT", "/api/items", AGENT,
      { key: `note:mention-padding-${n}`, title: `Tasks item ${n}` });
    await call("PUT", "/api/projects", AGENT, { name: "tasks", about: "Tasks",
      logo: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle cx="5" cy="5" r="4"/></svg>' });
    await call("PUT", "/api/projects", AGENT, { name: "cockpit", about: "The work page" });
    const withLogo = await call("PUT", "/api/items", AGENT,
      { key: "note:mention-tasks", title: "Review list reminders", project: "tasks" });
    const ownWork = await call("PUT", "/api/items", AGENT,
      { key: "note:mention-cockpit", title: "Review the work page", project: "cockpit" });
    const steppedItem = await call("PUT", "/api/items", AGENT,
      { key: "note:mention-export", title: "Review the CSV export" });
    for (let n = 0; n < 10; n++) await call("POST", "/api/steps", AGENT,
      { item_key: `note:mention-padding-${n}`, need: "read", ask: "Read the Tasks item" });
    const { step: stepped } = await call("POST", "/api/steps", AGENT,
      { item_key: steppedItem.key, need: "review", ask: "Check the export" });
    const closed = await call("PUT", "/api/items", AGENT, { key: "note:mention-closed", title: "Fix list ordering" });
    await call("PUT", "/api/items", headers, { key: closed.key, stage: "done" });
    // Hide the finished fixture from the current week while retaining it for direct links.
    const { spawnSync } = require("node:child_process");
    const aged = spawnSync(path.join(__dirname, "../../.venv/bin/python"), ["-c",
      "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"UPDATE items SET done_at='2000-01-01T00:00:00Z', updated_at='2000-01-01T00:00:00Z' WHERE id=?\", (int(sys.argv[2]),)); c.commit()",
      path.join(work, "cockpit.db"), String(closed.id)], { encoding: "utf8" });
    if (aged.status !== 0) throw new Error(aged.stderr);
    const closedId = closed.id;
    const initial = await call("GET", "/api/state", headers);
    // The fixture database's open steps are snoozed, so this run's step takes Next up.
    await Promise.all([...initial.queue, ...initial.ready_next].map((one) => call("POST", `/api/steps/${one.id}/snooze`, headers, { until: "2099-01-01T08:00:00Z" })));
    const key = `note:item-mentions-${Date.now()}`;
    await call("PUT", "/api/items", AGENT, { key, title: "Item mentions test" });
    const body = `The coordinator (item ${withLogo.id}) and to-do ${ownWork.id} wait; your answer on step ${stepped.id} settles it. Step 1 stays text.`;
    const { step } = await call("POST", "/api/steps", AGENT, { item_key: key, need: "read:mentions", ask: "mentions test; read", body, score: 100, session_id: SESSION_B });

    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const width of [1440, 390]) {
      const context = await browser.newContext({ viewport: { width, height: 1000 } });
      await context.setExtraHTTPHeaders({ cookie: headers.cookie });
      const page = await context.newPage();
      page.setDefaultTimeout(30000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${info.origin}/`);
      const card = page.locator(`#next [data-step="${step.id}"]`).first();
      await page.waitForFunction((id) => !!document.querySelector(`#next [data-step="${id}"] a.item-ref`), step.id).catch(() => null);
      if (!(await card.count())) {
        // On a phone the card shows once its row is picked.
        await page.evaluate(() => { for (const group of document.querySelectorAll("#sidebar details")) group.open = true; });
        await page.locator(`#sidebar li[data-item]`, { hasText: "Item mentions test" }).first().locator(".side-row").click();
      }
      await card.locator("a.item-ref").first().waitFor();
      const links = await card.locator("a.item-ref").evaluateAll((all) => all.map((link) => ({
        // Cockpit's tooltip takes the title over on hover (motion.js).
        text: link.textContent.trim(), title: link.title || link.dataset.tip || "", href: link.getAttribute("href"),
        logo: link.querySelector("img.ref-mark")?.getAttribute("src") || null,
        key: !!link.querySelector(".cockpit-mark.ref-mark"),
      })));
      const find = (words) => links.find((link) => link.text === words);
      const a = find(`item ${withLogo.id}`);
      const b = find(`to-do ${ownWork.id}`);
      const c = find(`step ${stepped.id}`);
      check(`${width}px: three mentions are links`, links.length === 3 && a && b && c, JSON.stringify(links));
      check(`${width}px: an item's link names it and shows its project's logo`,
        a && a.title === withLogo.title && a.href === `/#item/${withLogo.id}` && a.logo?.startsWith(`/projects/${encodeURIComponent(withLogo.project)}/logo.svg`), JSON.stringify(a));
      check(`${width}px: Cockpit's work leads with Cockpit's key`, b && b.title === ownWork.title && b.key, JSON.stringify(b));
      check(`${width}px: a step's link names the item it is on`, c && c.title === steppedItem.title && c.href === `/#item/${steppedItem.id}`, JSON.stringify(c));
      check(`${width}px: "Step 1" stays text`, (await card.locator(".prose").first().innerText()).includes("Step 1 stays text"), "");
      if (width === 1440) {
        await card.locator("a.item-ref", { hasText: `step ${stepped.id}` }).first().hover();
        const tip = await page.locator(".tip:not([hidden])").first().innerText().catch(() => "");
        check("hovering a mention shows its item's name", tip.includes(steppedItem.title), `tooltip: ${JSON.stringify(tip)}`);
        await page.screenshot({ path: path.join(work, "item-mentions-hover.png"), clip: await card.boundingBox() });
        await page.mouse.move(0, 0);
      }
      await screenshotRedrawn(card, { path: path.join(work, `item-mentions-${width}.png`) });
      await card.locator(`a.item-ref`, { hasText: `item ${withLogo.id}` }).first().click();
      const opened = await page.waitForFunction((title) => (document.querySelector("#next")?.textContent || "").includes(title), withLogo.title)
        .then(() => true, () => false);
      check(`${width}px: pressing a mention opens its item`, opened, "the card did not show the item's title");
      // An item closed before this week, which the page does not list, opens from its link in a tab of its own.
      await page.goto(`${info.origin}/#item/${closedId}`);
      const found = await page.locator(`.row[data-item="${closedId}"]:visible, li[data-item="${closedId}"]:visible`).first()
        .waitFor().then(() => true, () => false);
      check(`${width}px: a link to an item closed earlier opens it`, found, `no row for item ${closedId}`);
      if (width === 1440) await page.screenshot({ path: path.join(work, "item-mentions-closed.png") });
      check(`${width}px: no page errors`, errors.length === 0, errors.join("; "));
      await context.close();
    }
  } finally {
    if (browser) await browser.close();
  }
  console.error(`screenshots in ${work}`);
  process.exitCode = results.every((result) => result.ok) ? 0 : 1;
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
