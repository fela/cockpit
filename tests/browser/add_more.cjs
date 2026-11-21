// Add more (SPEC CONTROL-18). Checks, on the main page at 1280 and 390 px:
// - pressing + after an Add opens the bare box, without the to-do just added; only a to-do whose Add more words came
//   back unsent shows there, with Open and Add more;
// - its Add more opens the Add box on that item, and Enter adds the words to the end of its text;
// - New item instead goes back to a new item, and Open shows the item on the card, its row picked in the list;
// - among a search's matches, a session on no item whose task is done or closed goes by the task's title, marked so,
//   and every session on no item says where it came from.
// Saves screenshots in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database, fake sessions; README.md says what
// it needs) and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/add_more.cjs

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

// The harness's agent token (tests/conftest.py), and a session of the ended task alone: no open item of the
// harness lists it, so a search shows it under Sessions on no item.
const ENDED_SESSION = "44444444-4444-4444-8444-444444444444";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "add-more-test", "content-type": "application/json" };
const ENDED_TITLE = "Tasks export review";

async function main() {
  const work = workFolder("cockpit-add-more-");
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
    // A task the user completed, whose session is on no open item. The session joins the item after the finish,
    // which would archive a session it finished with (SESSION-4) and take it off the list.
    const call = async (method, url, auth, body) => {
      const response = await fetch(info.origin + url, { method, headers: auth, body: JSON.stringify(body) });
      if (!response.ok) throw new Error(`${method} ${url}: HTTP ${response.status}: ${await response.text()}`);
      return response.json();
    };
    const user = { cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json" };
    const endedKey = `note:add-more-ended-${Date.now()}`;
    await call("PUT", "/api/items", AGENT, { key: endedKey, title: ENDED_TITLE });
    const { step: last } = await call("POST", "/api/steps", AGENT, { item_key: endedKey, need: "decide:keep", ask: "Keep the shared-list preview?" });
    await call("POST", `/api/items/${last.item_id}/finish`, user,
      { step_id: last.id, how: "complete", answer: null, note: "", reason: "", session_id: null });
    await call("PUT", "/api/items", AGENT, { key: endedKey, sessions: [ENDED_SESSION] });
    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const [width, scheme] of [[1280, "dark"], [390, "light"]]) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      // The fixture can open the "Back after" dialog over the page while the user's timer is idle; answer it
      // with Return whenever it shows, so it never takes the test's clicks.
      await page.addLocatorHandler(page.locator("dialog#idle[open]"), (dialog) => dialog.locator("button", { hasText: "Return" }).click()
        // The test may close its page while an answer is under way.
        .catch((error) => { if (!/has been closed/.test(error.message)) throw error; }));
      page.setDefaultTimeout(15000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${info.origin}/`);
      await page.locator(".layout > .primary").waitFor();
      const dialog = page.locator("#add-box");
      // The rail's + at laptop width; the phone bar's + on a phone's item screen.
      const add = page.locator(width < 1000 ? ".phone-add" : ".sidebar-add");
      const title = `Tasks reminder trial ${width}`;
      const item = () => page.evaluate(async (title) => (await (await fetch("/api/state")).json()).items.find((one) => one.title === title) || null, title);

      // The Add sent too soon. The server makes the item once its Undo time has run out.
      await add.click();
      await page.locator('#add-box textarea[data-key="add:new"]').fill(`${title}\nwith completed tasks and`);
      await page.keyboard.press("Enter");
      const made = await waitFor(item, Boolean, 15000);
      check(`${width} px: the item exists`, made !== null, "no item");

      // Later: the Add box opens bare; the to-do just added is not repeated above the text box.
      await add.click();
      await page.locator('#add-box textarea[data-key="add:new"]').waitFor();
      check(`${width} px: the Add box does not repeat the to-do just added`, (await page.locator("#add-box .add-recent").count()) === 0, "line shown");
      await page.keyboard.press("Escape");
      // Add more words that came back unsent keep their to-do on offer, with Open and Add more.
      const unsent = () => page.evaluate((id) => ui.drafts.set(addMoreKey(id), "words that did not go"), made.id);
      await unsent();
      await add.click();
      const recent = page.locator("#add-box .add-recent");
      await recent.waitFor();
      const line = await recent.innerText();
      check(`${width} px: the Add box offers the to-do with unsent words`,
        line.includes("Your unsent words for") && line.includes(title) && line.includes("Open") && line.includes("Add more"), line);
      await page.screenshot({ path: path.join(work, `add-recent-${width}-${scheme}.png`) });
      // Its Add more opens the box on the item, and the words typed there join the end of its text.
      await recent.locator("button", { hasText: "Add more" }).click();
      const more = page.locator('#add-box textarea[aria-label="More words"]');
      await more.waitFor();
      const heading = await page.locator("#add-box h2").innerText();
      check(`${width} px: Add more opens the box on the item`, heading === title && (await page.locator("#add-box .pin-key").count()) === 0, heading);
      const fits = await page.evaluate(() => {
        const panel = document.querySelector("#add-box .add-panel").getBoundingClientRect();
        return panel.left >= 0 && panel.right <= window.innerWidth && document.documentElement.scrollWidth <= window.innerWidth;
      });
      check(`${width} px: the Add more box fits, no sideways scroll`, fits, "overflows");
      await page.screenshot({ path: path.join(work, `add-more-${width}-${scheme}.png`) });
      await more.fill("a list selector");
      await more.press("Enter");
      check(`${width} px: Enter adds the words and closes the box`, await dialog.isHidden(), "still open");
      const joined = await waitFor(item, (one) => one && one.summary.endsWith("\n\na list selector"), 3000);
      check(`${width} px: the words join the end of the item's text`, joined && joined.summary === `${title}\nwith completed tasks and\n\na list selector`,
        joined && JSON.stringify(joined.summary));

      // New item instead goes back to a new item, which offers the to-do again; its Open shows the item.
      await unsent();
      await add.click();
      await recent.waitFor();
      await recent.locator("button", { hasText: "Add more" }).click();
      await page.locator("#add-box .add-head .link-button").click();
      check(`${width} px: New item instead goes back to a new item`, await page.locator('#add-box textarea[data-key="add:new"]').isVisible(), "no new-item box");
      await page.locator("#add-box .add-recent button", { hasText: "Open" }).click();
      check(`${width} px: Open closes the box`, await dialog.isHidden(), "still open");
      // The card shows the item, and at laptop width its row in the list at the left is picked and on screen.
      const shown = await waitFor(() => page.evaluate(({ id, title }) => {
        const row = document.querySelector(`#sidebar li.side-item[data-item="${id}"]`);
        const box = row ? row.getBoundingClientRect() : null;
        return { picked: Boolean(row && row.classList.contains("sel")), card: document.getElementById("next").textContent.includes(title),
          inView: Boolean(box) && box.height > 0 && box.top >= 0 && box.bottom <= window.innerHeight };
      }, { id: made.id, title }), (value) => value.card && (width < 1000 || (value.picked && value.inView)), 3000);
      check(`${width} px: Open shows the item on the card${width < 1000 ? "" : ", its row picked and on screen"}`,
        shown.card && (width < 1000 || (shown.picked && shown.inView)), JSON.stringify(shown));

      // Sessions on no item show among a search's matches below the card. The page gets the ended task's session as a
      // stopped one the user started today, and the sweep's; both names hold "check", which the search looks for.
      const ended = await page.evaluate(({ sessionId, endedTitle }) => {
        const done = [...ui.state.items, ...ui.state.ended_items].find((one) => one.title === endedTitle);
        const base = { ...ui.state.sessions[0], alive: false, pid: null, is_sweep: false, started: null };
        ui.state = { ...ui.state, sessions: [...ui.state.sessions.filter((one) => one.session_id !== sessionId),
          { ...base, session_id: sessionId, name: "Please check whether Tasks exports preserve weekly reminders",
            started: { author: "user", new_name: "Tasks export reminders", item_id: done.id, created_at: new Date().toISOString() } },
          { ...base, session_id: "sweep-example", name: "cockpit check sweep", is_sweep: true }] };
        setFind("check", false, null, true);
        document.querySelector("#work .group.loose")?.scrollIntoView({ block: "start" });
        const rows = [...document.querySelectorAll("#work .session-row")].filter((row) => row.querySelector(".ended-chip"));
        const titles = new Set(ui.state.items.map((one) => one.title));
        const origins = [...document.querySelectorAll("#work .group.loose .session-row")].map((row) => ({
          title: row.querySelector(".row-title").textContent, origin: (row.querySelector(".session-origin") || {}).textContent || "" }));
        return { done: done.title, count: rows.length, named: rows.every((row) => titles.has(row.querySelector(".row-title").textContent)),
          chips: [...new Set(rows.map((row) => row.querySelector(".ended-chip").textContent))], origins };
      }, { sessionId: ENDED_SESSION, endedTitle: ENDED_TITLE });
      // A session whose task ended goes by the task's title, marked done or closed.
      check(`${width} px: sessions of ended tasks show the task's title and a done or closed mark`,
        ended.count > 0 && ended.named && ended.chips.every((chip) => chip === "Task done" || chip === "Task closed"), JSON.stringify(ended));
      // Each session on no item says who started it and what is left to do; the sweep goes by what it is.
      const origins = ended.origins;
      const titled = (title) => origins.find((one) => one.title === title);
      check(`${width} px: every session on no item says where it came from`,
        origins.length > 0 && origins.every((one) => /^(Started|Cockpit's own session)/.test(one.origin)), JSON.stringify(origins));
      check(`${width} px: the sweep goes by what it is`,
        Boolean(titled("Cockpit sweep")) && /need not make an item/.test(titled("Cockpit sweep").origin),
        JSON.stringify(origins));
      check(`${width} px: a stopped session of a done task says there is nothing to close`,
        /^Started by you today\. Stopped, with its task finished: nothing to close\.$/.test(titled(ended.done)?.origin || ""), JSON.stringify(titled(ended.done)));
      // The next refresh of the page's state drops the faked sessions, so the image is taken at once.
      await page.screenshot({ path: path.join(work, `loose-${width}-${scheme}.png`) });
      check(`${width} px: no page errors`, errors.length === 0, errors.join(" | "));
      await context.close();
    }
    console.error(`  screenshots in ${work}`);
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

// The value `read` gives once `ok` holds it, or its last value after `ms`.
async function waitFor(read, ok, ms) {
  const end = Date.now() + ms;
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
