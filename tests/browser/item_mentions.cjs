// A mention of another item in a step's text is a link to that item (SPEC UI-74). Puts its own step, whose body
// mentions an item with a project logo, an item of Cockpit's own work and a step on a third item, into the user's queue
// on the sidebar page, and checks at 1440 and 390 px:
// - each mention is a link that names its item on hover (its title attribute) and leads with its project's mark: the
//   project's logo, or Cockpit's key on Cockpit's work;
// - the step's mention names the item the step is on;
// - pressing a mention opens that item on the card;
// - a link to a task done before this week, opened in a tab of its own, shows that task on the card with no Search
//   filter; so does a link to a task archived before this week, which the page does not hold, and a reload keeps it;
// - at 1440 px, a pick in the sidebar or Your time made while the closed work answers a link wins; two quick presses
//   on a task the page does not hold yet open it without "not on Cockpit"; Unarchive on a task done and archived
//   before this week takes Unarchive off its card;
// - at 1440 px, a search with closed work (its address) survives a reload, picking another task in the sidebar then
//   takes its filter away, and Back returns to the task before without it; R on another task's line in the sidebar
//   takes it away too;
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
    const archived = await call("PUT", "/api/items", AGENT, { key: "note:mention-archived", title: "Drop the old export" });
    await call("PUT", "/api/items", headers, { key: archived.key, archived: true, note: "not needed" });
    const archivedAged = spawnSync(path.join(__dirname, "../../.venv/bin/python"), ["-c",
      "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"UPDATE items SET archived_at='2000-01-01T00:00:00Z', updated_at='2000-01-01T00:00:00Z' WHERE id=?\", (int(sys.argv[2]),)); c.commit()",
      path.join(work, "cockpit.db"), String(archived.id)], { encoding: "utf8" });
    if (archivedAged.status !== 0) throw new Error(archivedAged.stderr);
    const retired = await call("PUT", "/api/items", AGENT, { key: "note:mention-retired", title: "Retire the old report" });
    await call("PUT", "/api/items", headers, { key: retired.key, stage: "done" });
    await call("PUT", "/api/items", headers, { key: retired.key, archived: true, note: "not needed" });
    const retiredAged = spawnSync(path.join(__dirname, "../../.venv/bin/python"), ["-c",
      "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"UPDATE items SET done_at='2000-01-01T00:00:00Z', archived_at='2000-01-01T00:00:00Z', updated_at='2000-01-01T00:00:00Z' WHERE id=?\", (int(sys.argv[2]),)); c.commit()",
      path.join(work, "cockpit.db"), String(retired.id)], { encoding: "utf8" });
    if (retiredAged.status !== 0) throw new Error(retiredAged.stderr);
    const shelved = await call("PUT", "/api/items", AGENT, { key: "note:mention-shelved", title: "Shelve the old draft" });
    await call("PUT", "/api/items", headers, { key: shelved.key, archived: true, note: "not needed" });
    const shelvedAged = spawnSync(path.join(__dirname, "../../.venv/bin/python"), ["-c",
      "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"UPDATE items SET archived_at='2000-01-01T00:00:00Z', updated_at='2000-01-01T00:00:00Z' WHERE id=?\", (int(sys.argv[2]),)); c.commit()",
      path.join(work, "cockpit.db"), String(shelved.id)], { encoding: "utf8" });
    if (shelvedAged.status !== 0) throw new Error(shelvedAged.stderr);
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
        await screenshotRedrawn(card, { path: path.join(work, "item-mentions-hover.png") });
        await page.mouse.move(0, 0);
      }
      await screenshotRedrawn(card, { path: path.join(work, `item-mentions-${width}.png`) });
      await card.locator(`a.item-ref`, { hasText: `item ${withLogo.id}` }).first().click();
      const opened = await page.waitForFunction((title) => (document.querySelector("#next")?.textContent || "").includes(title), withLogo.title)
        .then(() => true, () => false);
      check(`${width}px: pressing a mention opens its item`, opened, "the card did not show the item's title");
      // A task closed before this week, which the sidebar does not list, opens on the card from its link in a tab of
      // its own, with no Search filter; so does one archived then, which the page does not hold.
      const onCard = async (id, title) => {
        await page.goto(`${info.origin}/#item/${id}`);
        const shown = await page.waitForFunction(({ id, title }) => ui.shownItem === id
          && (document.querySelector("#next")?.textContent || "").includes(title), { id, title }).then(() => true, () => false);
        return page.evaluate((shown) => ({ shown, bar: Boolean(document.querySelector(".find-bar")), hash: location.hash }), shown);
      };
      const done = await onCard(closedId, "Fix list ordering");
      check(`${width}px: a link to a task done earlier shows it on the card`, done.shown && !done.bar && !done.hash.startsWith("#find"), JSON.stringify(done));
      await page.screenshot({ path: path.join(work, `item-mentions-done-${width}.png`) });
      const gone = await onCard(archived.id, "Drop the old export");
      check(`${width}px: a link to a task archived earlier shows it on the card`, gone.shown && !gone.bar && !gone.hash.startsWith("#find"), JSON.stringify(gone));
      await page.screenshot({ path: path.join(work, `item-mentions-archived-${width}.png`) });
      // A reload on that card keeps the task on it (VIEW-36): the page asks the closed work for it again.
      await page.reload();
      const reloaded = await page.waitForFunction(({ id, title }) => ui.state && ui.closedLookups.get(id) === null
        && ui.shownItem === id && (document.querySelector("#next")?.textContent || "").includes(title),
      { id: archived.id, title: "Drop the old export" }).then(() => true, () => false);
      check(`${width}px: a reload on that card keeps the task on it`, reloaded,
        JSON.stringify(await page.evaluate(() => ({ shown: ui.shownItem, next: document.querySelector("#next")?.textContent.slice(0, 80) }))));
      if (width === 1440) {
        // A navigation made while the closed work answers a link wins: the late answer leaves the card and the view.
        const toasts = [];
        await page.exposeFunction("recordToast", (text) => toasts.push(text));
        let release;
        let held = new Promise((resolve) => { release = resolve; });
        await page.route("**/api/items/closed?**", async (route) => { await held; await route.continue(); });
        await page.goto(`${info.origin}/`);
        await page.waitForFunction(() => ui.state && ui.shownItem !== null);
        await page.evaluate(() => { const shown = toast; window.toast = (...args) => { window.recordToast(args[0]); return shown(...args); }; });
        const asked = page.waitForRequest("**/api/items/closed?**");
        await page.evaluate((id) => { openMention(id); }, retired.id);
        await asked;
        await page.evaluate(() => { for (const group of document.querySelectorAll("#sidebar details")) group.open = true; });
        const pick = page.locator("#sidebar li.side-item[data-item]:not(.sel) > .side-row:visible").first();
        const picked = Number(await pick.evaluate((row) => row.parentElement.dataset.item));
        await pick.click();
        await page.waitForFunction((id) => ui.shownItem === id, picked);
        const firstAnswer = page.waitForResponse("**/api/items/closed?**");
        release();
        await firstAnswer;
        await page.waitForTimeout(500);
        const afterPick = await page.evaluate(() => ui.shownItem);
        check("a pick in the sidebar made while the closed work answers wins", afterPick === picked, JSON.stringify({ picked, afterPick }));
        held = new Promise((resolve) => { release = resolve; });
        const second = page.waitForRequest("**/api/items/closed?**");
        await page.evaluate((id) => { openMention(id); }, retired.id);
        await second;
        await page.evaluate(() => openCenter("time"));
        const secondAnswer = page.waitForResponse("**/api/items/closed?**");
        release();
        await secondAnswer;
        await page.waitForTimeout(500);
        const after = await page.evaluate(() => ({ shown: ui.shownItem, view: center.view }));
        check("a view opened while the closed work answers wins", after.shown === picked && after.view === "time", JSON.stringify({ picked, ...after }));
        await page.unroute("**/api/items/closed?**");
        await page.evaluate(() => closeCenter(true));

        // Two quick presses on a task the page does not hold yet both wait for the state that holds it: neither
        // says the task is not on Cockpit.
        let hold;
        const stateHeld = new Promise((resolve) => { hold = resolve; });
        await page.route("**/api/state?**", async (route) => { await stateHeld; await route.continue(); });
        await page.route("**/api/state", async (route) => { await stateHeld; await route.continue(); });
        const fresh = await call("PUT", "/api/items", AGENT, { key: "note:mention-fresh", title: "Sort the new invoices" });
        const unseen = await page.evaluate((id) => {
          const absent = !ui.state.items.some((item) => item.id === id);
          openMention(id); openMention(id);
          return absent;
        }, fresh.id);
        hold();
        const twice = await page.waitForFunction((id) => ui.shownItem === id, fresh.id).then(() => true, () => false);
        await page.waitForTimeout(500);
        check("two quick presses on a task the page does not hold open it", unseen && twice && !toasts.some((text) => text.includes("is not on Cockpit")),
          JSON.stringify({ unseen, twice, toasts }));
        await page.unroute("**/api/state?**");
        await page.unroute("**/api/state");

        // Unarchive on a task done and archived before this week shows it as it is now, done, with no Unarchive.
        const back = await onCard(retired.id, "Retire the old report");
        check("a link to a task done and archived earlier shows it on the card", back.shown, JSON.stringify(back));
        await page.locator('#next button[aria-label="Unarchive"]').first().click();
        const restored = await page.waitForFunction(({ id, title }) => ui.shownItem === id
          && !document.querySelector('#next button[aria-label="Unarchive"]')
          && (document.querySelector("#next")?.textContent || "").includes(title), { id: retired.id, title: "Retire the old report" })
          .then(() => true, () => false);
        check("Unarchive on it takes Unarchive off the card", restored,
          JSON.stringify(await page.evaluate(() => ({ shown: ui.shownItem, unarchive: Boolean(document.querySelector('#next button[aria-label="Unarchive"]')) }))));
        await page.screenshot({ path: path.join(work, "item-mentions-unarchived.png") });

        // Unarchive on a task archived while open keeps it on the card while the state that holds it is on its way.
        await onCard(shelved.id, "Shelve the old draft");
        let free;
        const stateLate = new Promise((resolve) => { free = resolve; });
        await page.route("**/api/state?**", async (route) => { await stateLate; await route.continue(); });
        await page.route("**/api/state", async (route) => { await stateLate; await route.continue(); });
        // While that state is on its way the card asks the closed work nothing: the closed work no longer has the task.
        let askedClosed = 0;
        const countClosed = (request) => { if (request.url().includes("/api/items/closed")) askedClosed++; };
        page.on("request", countClosed);
        await page.locator('#next button[aria-label="Unarchive"]').first().click();
        await page.waitForTimeout(1000);
        const waiting = await page.evaluate(() => ui.shownItem);
        page.off("request", countClosed);
        free();
        const reopened = await page.waitForFunction(({ id, title }) => ui.shownItem === id && ui.state.items.some((item) => item.id === id)
          && (document.querySelector("#next")?.textContent || "").includes(title), { id: shelved.id, title: "Shelve the old draft" })
          .then(() => true, () => false);
        check("Unarchive on a task archived while open keeps it on the card", waiting === shelved.id && reopened && !askedClosed,
          JSON.stringify({ askedClosed, ...(await page.evaluate(() => ({ shown: ui.shownItem }))) }));
        await page.unroute("**/api/state?**");
        await page.unroute("**/api/state");
      }
      if (width === 1440) {
        // A search with closed work keeps its filter through a reload: its words live in the address.
        await page.goto(`${info.origin}/#find-closed/${encodeURIComponent(`#${closedId}`)}/${closedId}`);
        await page.locator("#work .find-bar").first().waitFor();
        await page.reload();
        const kept = await page.locator("#work .find-bar").first().waitFor().then(() => true, () => false);
        check("a reload keeps the filter", kept, `hash ${await page.evaluate(() => location.hash)}`);
        // Search's filter belongs to the search that made it: picking another task in the sidebar ends it.
        await page.evaluate(() => { for (const group of document.querySelectorAll("#sidebar details")) group.open = true; });
        const other = page.locator("#sidebar li.side-item[data-item]:not(.sel) > .side-row:visible").first();
        const otherId = await other.evaluate((row) => row.parentElement.dataset.item);
        await other.click();
        const cleared = await page.waitForFunction((id) => !document.querySelector("#work .find-bar") && !location.hash.startsWith("#find")
          && document.querySelector(`#sidebar li.sel[data-item="${id}"]`), otherId).then(() => true, () => false);
        check("picking another task takes the filter away", cleared, `hash ${await page.evaluate(() => location.hash)}`);
        await page.screenshot({ path: path.join(work, "item-mentions-after-pick.png") });
        await page.goBack();
        const back = await page.waitForFunction((id) => !document.querySelector(`#sidebar li.sel[data-item="${id}"]`), otherId)
          .then(() => page.evaluate(() => ({ bar: Boolean(document.querySelector("#work .find-bar")), hash: location.hash })), () => null);
        check("Back returns to the task without the filter", back && !back.bar && !back.hash.startsWith("#find"), JSON.stringify(back));
        // R on another task's line in the sidebar picks that task too, and ends the filter the same way.
        await page.goto(`${info.origin}/#find-closed/${encodeURIComponent(`#${closedId}`)}/${closedId}`);
        await page.locator("#work .find-bar").first().waitFor();
        // The closed task's match takes the keyboard's highlight when it arrives, so R waits until it has.
        await page.locator(`#work .row[data-item="${closedId}"].keyboard-current`).first().waitFor();
        const keyed = await page.evaluate(() => {
          const line = [...document.querySelectorAll("#sidebar li.side-item[data-item]:not(.sel)")]
            .find((li) => li.checkVisibility() && /^\d+$/.test(li.dataset.item));
          window.CockpitKeys.highlight(line);
          return line.dataset.item;
        });
        await page.keyboard.press("r");
        const keyCleared = await page.waitForFunction((id) => !document.querySelector("#work .find-bar") && !location.hash.startsWith("#find")
          && document.querySelector(`#sidebar li.sel[data-item="${id}"]`), keyed).then(() => true, () => false);
        check("R on another task's line takes the filter away", keyCleared, `hash ${await page.evaluate(() => location.hash)}`);
      }
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
