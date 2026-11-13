// Projects and Inbox settings (SPEC VIEW-34, UI-70). Checks, at 390 and 1280 px in the light and the dark theme:
// - each task's row in the list at the left leads with its project's logo: Cockpit's tag for Cockpit's work, the
//   logo the project has (loaded, 14 px), else the project's first letter;
// - the steps past the four places fold into Inbox's "Show N more" row, which shows them all, and the page's Reload folds them again;
// - the Inbox heading's settings key opens Priorities and inbox in the card's place, whose inbox part holds How your
//   inbox is ranked as numbered points and What to check as a text box, each holding Cockpit's own at first;
// - a point dragged by its grip, a point moved with the arrow keys, an edited point and one added with Enter, and words
//   added to the text box, all reach the server on Save in that order; the view shows them when it opens again,
//   and the default key puts Cockpit's points back;
// - two presses on the key while the settings load open one view, which keeps the words typed in it (regression check);
// - a Save made while the last is on its way makes leaving the page ask, and goes once that one is done (regression check);
// - the page does not scroll sideways, and has no page errors.
// Saves screenshots of the page and the view per width and theme in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs)
// and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/projects.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const SESSION_B = "22222222-2222-4222-8222-222222222222";
const TOKEN = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "projects-test" };
const LOGO = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect width="24" height="24" rx="5" fill="#2f7d5b"/>'
  + '<path d="M6 17l6-11 6 11z" fill="#fff"/></svg>';

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// What read() gives once done() holds of it, or after 20 s, so a slow machine fails no check that only took longer.
async function settled(read, done) {
  const end = Date.now() + 20e3;
  for (;;) {
    const got = await read();
    if (done(got) || Date.now() > end) return got;
    await sleep(250);
  }
}

async function main() {
  const work = workFolder("projects-");
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

    const run = Date.now();
    // The checks start from empty inbox settings.
    await user("PUT", "/api/inbox-settings", { rules: [], sources: "" });
    // Projects stay at most ten (VIEW-34): the fixture's projects past eight merge into one, so the two below fit.
    const initial = (await user("GET", "/api/projects")).filter((project) => project.name !== "cockpit");
    for (const extra of initial.slice(7)) {
      await agent("POST", `/api/projects/${encodeURIComponent(extra.name)}/merge`, { into: initial[0].name });
    }
    await agent("PUT", "/api/projects", { name: "logo-test", about: "A project with a logo", logo: LOGO });
    await agent("PUT", "/api/projects", { name: "plain-test", about: "A project without one" });
    const put = async (slug, title, project) => {
      const key = `note:${slug}-${run}`;
      const item = await agent("PUT", "/api/items", { key, title, project, priority: "urgent" });
      await agent("POST", "/api/steps", { item_key: key, need: `${slug}-${run}`, ask: `${title}?`, score: 100, session_id: SESSION_B, body: "Yes or no." });
      return item;
    };
    const withLogo = await put("logo", "Ship the logo work", "logo-test");
    const plain = await put("plain", "Ship the plain work", "Plain-Test");
    const tool = await put("tool", "Ship the tool work", "cockpit");
    check("a put in another case stores the project's own name", plain.project === "plain-test", plain.project);
    let refused = "";
    try { await agent("PUT", "/api/items", { key: `note:none-${run}`, title: "Nowhere", project: "no-such-project" }); }
    catch (error) { refused = error.message; }
    check("a put naming no project that exists is refused", refused.includes("there is no project 'no-such-project'"), refused);

    browser = await chromium.launch({ executablePath: CHROMIUM });
    let saved = false;
    let raced = false;
    for (const [width, height, scheme] of [[390, 844, "light"], [1280, 900, "light"], [1280, 900, "dark"], [390, 844, "dark"]]) {
      const context = await browser.newContext({ viewport: { width, height }, colorScheme: scheme });
      const at = `${width} px, ${scheme}`;
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      // The fixture can open the "Back after" dialog over the page while the user's timer is idle; answer it
      // with Return whenever it shows, so it never takes the test's clicks.
      await page.addLocatorHandler(page.locator("dialog#idle[open]"), (dialog) => dialog.locator("button", { hasText: "Return" }).click()
        // The test may close its page while an answer is under way.
        .catch((error) => { if (!/has been closed/.test(error.message)) throw error; }));
      // A minute without the mouse fades the idle veil in over the page; a move clears it.
      let moves = 0;
      await page.addLocatorHandler(page.locator("#idle-veil"), () => page.mouse.move(5 + (moves++ % 2), 5)
        .catch((error) => { if (!/has been closed/.test(error.message)) throw error; }));
      page.setDefaultTimeout(90000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => { if (message.type() === "error") console.error("console:", message.text().slice(0, 300)); });
      await page.goto(`${info.origin}/`);
      // Steps past the four places fold into Inbox's "Show N more" row (VIEW-30); the test's own steps can be among them.
      await page.waitForFunction(() => document.querySelector("#sidebar .sidebar-group.queue li"), null, { timeout: 90e3 });
      const more = page.locator("#sidebar .sidebar-group.queue li.sidebar-more > button");
      if (await more.count()) {
        const words = (await more.textContent()).trim();
        check(`${at}: the steps past the four places fold into one "Show N more" row`, /^Show \d+ more$/.test(words)
          && !(await page.locator("#sidebar .sidebar-group.ready").count()), words);
        await more.evaluate((el) => el.click());
        await page.waitForFunction(() => document.querySelector("#sidebar li.sidebar-more")?.textContent.trim() === "Show fewer");
        const shown = await page.evaluate(() => {
          const list = [...document.querySelectorAll("#sidebar .sidebar-group.queue .sidebar-list > li")];
          return { last: list.at(-1).classList.contains("sidebar-more"), ready: list.filter((li) => li.dataset.step).length };
        });
        check(`${at}: pressing it shows them in Inbox, with Show fewer at their end`, shown.last && shown.ready > 0, JSON.stringify(shown));
        // Folded is the default: the page's Reload keeps other open rows but folds these again.
        await page.evaluate(() => reloadKeepingPlace());
        await page.waitForFunction(() => /^Show \d+ more$/.test(document.querySelector("#sidebar li.sidebar-more")?.textContent.trim() || ""));
        check(`${at}: the page's Reload folds them again`, true);
        await page.screenshot({ path: path.join(work, `inbox-folded-${width}-${scheme}.png`) });
        await page.locator("#sidebar .sidebar-group.queue li.sidebar-more > button").evaluate((el) => el.click());
        await page.waitForFunction(() => document.querySelector("#sidebar li.sidebar-more")?.textContent.trim() === "Show fewer");
      } else console.log(`note ${at}: no step waits past the four places, so the "Show N more" row is not checked`);
      const row = (item) => page.locator(`#sidebar li.side-item[data-item="${item.id}"]`).first();
      await row(withLogo).waitFor({ state: "attached", timeout: 90e3 });
      // On a phone the item and the list are two screens (UI-56): the item screen's bar opens the list.
      // On a slow machine the item screen can come up after the row is in the list, so wait for one of the two screens.
      await page.waitForFunction((id) => [document.querySelector(`#sidebar li.side-item[data-item="${id}"]`),
        document.querySelector(".phone-back")].some((el) => el && el.checkVisibility()), withLogo.id);
      if (await page.locator(".phone-back").isVisible()) await page.locator(".phone-back").click();
      await row(withLogo).waitFor({ state: "visible" });
      const logo = row(withLogo).locator(".sidebar-lead img.project-logo");
      await logo.waitFor({ state: "visible" }).catch(() => {});
      await logo.evaluate((el) => el.complete || new Promise((resolve) => { el.onload = resolve; el.onerror = resolve; })).catch(() => {});
      const loaded = await logo.evaluate((el) => ({ ok: el.naturalWidth > 0, w: el.getBoundingClientRect().width })).catch((error) => ({ ok: false, w: error.message }));
      check(`${at}: a project's logo leads its task's row, loaded at 14 px`, loaded.ok && loaded.w === 14, JSON.stringify(loaded));
      const letter = await row(plain).locator(".sidebar-lead .project-letter").textContent().catch(() => null);
      check(`${at}: a project without a logo shows its first letter`, letter === "P", letter);
      check(`${at}: Cockpit's work keeps the Cockpit tag`, await row(tool).locator(".sidebar-lead .cockpit-mark").count() === 1, "no tag");
      await page.screenshot({ path: path.join(work, `projects-${width}-${scheme}.png`) });

      const key = page.locator(".sidebar-group.queue > summary .inbox-settings-open");
      const view = page.locator("#settings-view");
      // Waits until the view is open, or closed and empty, and its slide is over.
      const viewSettled = (shown) => page.waitForFunction((shown) => settingsView.open === shown && sliding === null
        && (shown || document.getElementById("settings-view").childElementCount === 0), shown);
      // The key is in the list, which on a phone is the other screen: Back from the view lands on the item screen.
      const toList = async () => { if (await page.locator(".phone-back").isVisible()) await page.locator(".phone-back").click(); };
      const openKey = async () => { await viewSettled(false); await toList(); await key.click(); await viewSettled(true); };
      const open = await page.locator(".sidebar-group.queue").evaluate((el) => el.open);
      await openKey();
      await view.locator(".inbox-point-text").first().waitFor();
      check(`${at}: the key leaves Inbox as it was`, await page.locator(".sidebar-group.queue").evaluate((el) => el.open) === open, "toggled");
      const points = () => view.locator(".inbox-point-text").evaluateAll((all) => all.map((box) => box.value));
      const titles = await view.locator(".inbox-part-title").allTextContents();
      const sources = view.locator("#inbox-sources");
      // The first pass sees Cockpit's own; later passes see what the checks before them saved.
      const kept = await user("GET", "/api/inbox-settings");
      const shownPoints = await points();
      const ranks = await view.locator(".inbox-rank").allTextContents();
      check(`${at}: the view shows the stored points, numbered, and the stored text`,
        titles.join("|") === "Priorities|How your inbox is ranked|What to check for new work"
        && JSON.stringify(shownPoints) === JSON.stringify(kept.rules.points) && ranks[0] === "1."
        && await sources.inputValue() === kept.sources.text
        && (saved || JSON.stringify(shownPoints) === JSON.stringify(kept.rules.default_points)), JSON.stringify({ titles, shownPoints, ranks }));
      const sideways = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
      check(`${at}: no sideways scroll`, !sideways, "scrolls");

      if (!saved) {
        const ours = kept.rules.default_points;
        const grip = (n) => view.locator(".inbox-point .inbox-grip").nth(n);
        // Drag the third point above the first.
        const from = await grip(2).boundingBox();
        const top = await view.locator(".inbox-point").nth(0).boundingBox();
        await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
        await page.mouse.down();
        for (let step = 1; step <= 8; step += 1) {
          await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2 + (top.y + 4 - from.y - from.height / 2) * step / 8);
        }
        await page.mouse.up();
        // Move the last point up one with the keyboard.
        await grip(ours.length - 1).focus();
        await page.keyboard.press("ArrowUp");
        // Rewrite the new second point, then start a point below it with Enter.
        const second = view.locator(".inbox-point-text").nth(1);
        await second.fill("Urgency, edited.");
        await second.press("End");
        await second.press("Enter");
        await page.keyboard.type("Shared list requests soon.");
        const theirs = `${await sources.inputValue()}\n- Jira: tickets assigned to me.`;
        await sources.fill(theirs);
        const want = [ours[2], "Urgency, edited.", "Shared list requests soon.", ours[1], ...ours.slice(3, -2), ours.at(-1), ours.at(-2)];
        const drawn = await points();
        check(`${at}: drag, arrow keys, edit and Enter put the points in the order wanted`,
          JSON.stringify(drawn) === JSON.stringify(want), JSON.stringify(drawn));
        await page.screenshot({ path: path.join(work, `inbox-settings-edit-${width}-${scheme}.png`) });
        await view.locator("button[type=submit]").click();
        await viewSettled(false);
        const stored = await settled(() => user("GET", "/api/inbox-settings"), (got) => got.sources.text === theirs);
        check(`${at}: Save stores the points in their new order and the words written`,
          JSON.stringify(stored.rules.points) === JSON.stringify(want) && stored.sources.text === theirs,
          JSON.stringify(stored));
        await openKey();
        await view.locator(".inbox-point-text").first().waitFor();
        const again = await points();
        const resets = await view.locator(".inbox-reset").evaluateAll((all) => all.map((button) => button.disabled));
        await view.locator(".inbox-reset").first().click();
        const reloaded = await points();
        check(`${at}: the view shows them again, and the default key reloads Cockpit's points`,
          JSON.stringify(again) === JSON.stringify(want) && resets.join() === "false,false"
          && JSON.stringify(reloaded) === JSON.stringify(ours), JSON.stringify({ again, resets, reloaded }));
        saved = true;
      }
      await page.screenshot({ path: path.join(work, `inbox-settings-${width}-${scheme}.png`) });
      await page.keyboard.press("Escape");
      await viewSettled(false);

      if (!raced) {
        raced = true;
        // A save still on its way opens the view at the first press, with no request to hold.
        await page.waitForFunction(() => !ui.inboxUnsaved && !ui.inboxNext && !ui.inboxSaving);
        const first = () => view.locator(".inbox-point-text").first();
        // The first answer waits until both presses are in; a second request would wait until the words are typed.
        let gets = 0;
        let letFirst = null;
        let letGet = null;
        await page.route("**/api/inbox-settings", async (route) => {
          if (route.request().method() !== "GET") return route.fallback();
          gets += 1;
          if (gets === 1) await new Promise((resolve) => { letFirst = resolve; });
          else await new Promise((resolve) => { letGet = resolve; });
          return route.fallback();
        });
        await toList();
        await key.click();
        await key.click();
        await settled(async () => letFirst, Boolean);
        letFirst();
        await first().fill("Typed while it loads.");
        if (letGet) letGet();
        await sleep(800);
        const typed = await first().inputValue();
        check(`${at}: two presses while the settings load open one view, which keeps the words typed`,
          gets === 1 && typed === "Typed while it loads.", JSON.stringify({ gets, typed }));
        await page.keyboard.press("Escape");
        await viewSettled(false);
        await page.unroute("**/api/inbox-settings");

        let letPut = null;
        await page.route("**/api/inbox-settings", async (route) => {
          if (route.request().method() !== "PUT" || letPut) return route.fallback();
          await new Promise((resolve) => { letPut = resolve; });
          return route.fallback();
        });
        const asks = () => page.evaluate(() => {
          const leaving = new Event("beforeunload", { cancelable: true });
          dispatchEvent(leaving);
          return leaving.defaultPrevented;
        });
        await openKey();
        await first().fill("First save.");
        await view.locator("button[type=submit]").click();
        await openKey();
        await first().fill("Second save.");
        await view.locator("button[type=submit]").click();
        await viewSettled(false);
        const asked = await asks();
        await settled(async () => letPut, Boolean);
        letPut();
        const stored = await settled(() => user("GET", "/api/inbox-settings"), (got) => got.rules.points[0] === "Second save.");
        await page.waitForFunction(() => !ui.busy.has("inbox-settings:set"));
        const after = await asks();
        check(`${at}: a Save behind another makes leaving ask, then goes`,
          asked && !after && stored.rules.points[0] === "Second save.", JSON.stringify({ asked, after, first: stored.rules.points[0] }));
        await page.unroute("**/api/inbox-settings");
      }
      check(`${at}: no page errors`, errors.length === 0, errors.join(" | "));
      await context.close();
    }
    console.error(`screenshots in ${work}`);
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

async function call(origin, headers, method, url, body) {
  const response = await fetch(`${origin}${url}`, { method, headers, body });
  const text = await response.text();
  if (!response.ok) throw new Error(`${method} ${url}: ${response.status} ${text}`);
  return text ? JSON.parse(text) : null;
}

async function harnessUp(work, server) {
  const file = path.join(work, "cookie.json");
  const end = Date.now() + 180e3;
  while (Date.now() < end) {
    if (server.exitCode !== null) throw new Error(`serve_preview.py exited with ${server.exitCode}`);
    if (fs.existsSync(file)) {
      const info = JSON.parse(fs.readFileSync(file, "utf8"));
      const up = await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false);
      if (up) return info;
    }
    await sleep(200);
  }
  throw new Error("serve_preview.py did not come up within 180 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
