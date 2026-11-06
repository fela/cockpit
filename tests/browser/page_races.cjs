// Words and answers that arrive while a request waits: - a refused send puts its words back only into a box that
// still writes to the same session; - a notice that comes while the Add more box is open waits for the box; - the
// priorities saved twice while the first save waits save both, in order, also when the first one fails; - a spec
// editor takes no input while its save waits; - two chip choices on a spec row, the second made while the first one's
// save waits, are both saved (regression check);   one made during the reload after a save is saved too, and a waiting one
// can go back to the row's value; - a briefing whose text failed to load is asked again on the next poll; - on Usage,
// a period's late answer gives way to the period picked after it. Run with
// PLAYWRIGHT_MODULE=/path/to/node_modules/playwright node tests/browser/page_races.cjs. Uses the isolated fixture of
// keyboard.cjs; never touches live work or real agents.
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");
const { spawn } = require("node:child_process");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "../..");
const work = workFolder("cockpit-races-");
const server = spawn(path.join(root, ".venv/bin/python"), [path.join(__dirname, "serve_keyboard.py"), work], {
  cwd: root, stdio: ["ignore", "pipe", "pipe"],
});
let serverLog = "";
server.stdout.on("data", (data) => { serverLog += data; });
server.stderr.on("data", (data) => { serverLog += data; });
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let browser;

(async () => {
  const runtime = path.join(work, "runtime.json");
  for (let at = 0; !fs.existsSync(runtime); at++) {
    if (at > 100 || server.exitCode !== null) throw new Error(`Fixture failed: ${serverLog}`);
    await delay(100);
  }
  const { origin, cookie } = JSON.parse(fs.readFileSync(runtime, "utf8"));
  for (let at = 0; ; at++) {
    try { if ((await fetch(origin + "/healthz")).ok) break; } catch {}
    if (at > 50) throw new Error(`Fixture did not listen: ${serverLog}`);
    await delay(100);
  }
  const executablePath = process.env.BROWSER_EXECUTABLE;
  browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  const context = await browser.newContext({ extraHTTPHeaders: { Cookie: cookie }, viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  page.setDefaultTimeout(8000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(origin + "/classic");
  await page.locator("#next [data-step]").waitFor();
  const failed = [];
  // Each check runs on its own: a failure is printed and the next check still runs.
  const check = async (name, body) => {
    try {
      await body();
      console.log(`PASS ${name}`);
    } catch (error) {
      failed.push(name);
      console.log(`FAIL ${name}: ${error.message.split("\n").slice(0, 12).join("\n")}`);
      await page.unrouteAll({ behavior: "ignoreErrors" });
    }
  };

  // A send refused after the user wrote in the same box to another session.
  await check("A refused send keeps a newer draft to another session; the notice holds its words", async () => {
    await page.route("**/api/messages", async (route) => {
      if (route.request().method() !== "POST") return route.continue();
      await delay(800);
      await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: "the session is gone" }) });
    });
    const composer = await page.evaluate(async () => {
      const key = "test:composer";
      ui.drafts.set(key, "First words for A");
      ui.choices.set(key, "session:a");
      const sending = sendMessage(key, { item_id: null }, { session_id: "a" }, "session A", null);
      await new Promise((resolve) => setTimeout(resolve, 100));
      ui.choices.set(key, "session:b");
      ui.drafts.set(key, "Newer words for B");
      await sending;
      return { draft: ui.drafts.get(key), choice: ui.choices.get(key), notice: document.querySelector("#toast .toast-text").textContent,
        tip: document.querySelector("#toast .toast-text").dataset.tip };
    });
    assert.deepEqual([composer.draft, composer.choice], ["Newer words for B", "session:b"]);
    assert.match(composer.notice, /^Not sent to session A: the session is gone\. The box now writes to another session/);
    assert.match(composer.tip, /Your words, not sent: First words for A/);
    const sameBox = await page.evaluate(async () => {
      const key = "test:composer-same";
      ui.drafts.set(key, "Only words");
      ui.choices.set(key, "session:a");
      await sendMessage(key, { item_id: null }, { session_id: "a" }, "session A", null);
      return { draft: ui.drafts.get(key), choice: ui.choices.get(key) };
    });
    assert.deepEqual([sameBox.draft, sameBox.choice], ["Only words", "session:a"]);
    // The user picked another To and emptied the box again: Send would take the words there.
    const emptied = await page.evaluate(async () => {
      const key = "test:composer-emptied";
      ui.drafts.set(key, "Words for A");
      ui.choices.set(key, "session:a");
      const sending = sendMessage(key, { item_id: null }, { session_id: "a" }, "session A", null);
      await new Promise((resolve) => setTimeout(resolve, 100));
      ui.choices.set(key, "session:b");
      await sending;
      return { draft: ui.drafts.get(key) || "", choice: ui.choices.get(key) };
    });
    assert.deepEqual([emptied.draft, emptied.choice], ["", "session:b"]);
    await page.unroute("**/api/messages");

    // A send a closed page left, refused on another page (Spec) after another tab gave its box another To.
    const left = await page.evaluate(() => {
      const key = "msg:step:left-test";
      localStorage.setItem("cockpit.draft-targets", JSON.stringify([[key, "session:b"]]));
      localStorage.setItem("cockpit.drafts", JSON.stringify([[key, "Newer words for B"]]));
      localStorage.setItem("cockpit.outbox.left-test", JSON.stringify({
        path: "/api/messages", method: "POST", headers: {}, body: "{}", tryMs: 30000,
        words: { store: "cockpit.drafts", targets: "cockpit.draft-targets", key, text: "Words for A", choice: "session:a" },
        // Older than a day: the page that takes it reports it as not sent without sending it.
        at: Date.now() - 2 * 24 * 3600e3, n: 0, page: "closed-page",
      }));
      return key;
    });
    await page.goto(origin + "/spec");
    await page.locator("#toast", { hasText: "not put back" }).waitFor();
    const stored = await page.evaluate((key) => ({
      draft: new Map(JSON.parse(localStorage.getItem("cockpit.drafts"))).get(key),
      to: new Map(JSON.parse(localStorage.getItem("cockpit.draft-targets"))).get(key),
      notice: document.querySelector("#toast").textContent,
    }), left);
    assert.deepEqual([stored.draft, stored.to], ["Newer words for B", "session:b"]);
    assert.match(stored.notice, /your words were not put back: Words for A/);
    await page.evaluate((key) => {
      const drafts = new Map(JSON.parse(localStorage.getItem("cockpit.drafts")));
      drafts.delete(key);
      localStorage.setItem("cockpit.drafts", JSON.stringify([...drafts]));
      localStorage.setItem("cockpit.draft-targets", "[]");
    }, left);
    await page.goto(origin + "/classic");
    await page.locator("#next [data-step]").waitFor();
  });

  // A notice that comes while the Add more box is open.
  await check("A notice waits while the Add more box is open, and shows once it closes", async () => {
    const following = await page.evaluate(async () => {
      window.testGone = 0;
      const shown = toast("Sent to session A", false, { label: "Take back", run: () => {}, gone: () => { window.testGone += 1; } });
      openFollowUp(shown, Promise.resolve(true), { id: "local-test", text: "x" }, "session A", 1);
      const box = document.querySelector("#toast .toast-follow textarea");
      box.value = "More words";
      // A second hand-off's notice: its Add more and its completed words apply once it shows.
      const waited = toast("Sent to session B", false, { label: "Take back", run: () => {} });
      offerFollowUp(waited, Promise.resolve(true), { id: "local-test-b", text: "y" }, "session B", 2);
      retoast(waited, "Sent to session B, which reads it now", "");
      const kept = { box: document.querySelector("#toast .toast-follow textarea")?.value, gone: window.testGone,
        text: document.querySelector("#toast .toast-text").textContent };
      document.querySelector("#toast .toast-follow-cancel").click();
      return { kept, after: document.querySelector("#toast .toast-text").textContent, gone: window.testGone,
        more: Boolean(document.querySelector("#toast .toast-more")) };
    });
    assert.deepEqual(following.kept, { box: "More words", gone: 0, text: "Sent to session A" });
    assert.deepEqual([following.after, following.gone, following.more], ["Sent to session B, which reads it now", 1, true]);
    // Take back pressed while a notice waits reads the Add more box's words, as `act` does, before that notice shows.
    const takenBack = await page.evaluate(async () => {
      let read = null;
      const shown = toast("Sent to session C", false, { label: "Take back",
        run: () => { read = document.querySelector("#toast .toast-follow textarea")?.value ?? null; } });
      openFollowUp(shown, Promise.resolve(true), { id: "local-test-c", text: "z" }, "session C", 3);
      document.querySelector("#toast .toast-follow textarea").value = "Words to keep";
      toast("Sent to session D");
      [...document.querySelectorAll("#toast button")].find((button) => button.textContent === "Take back").click();
      return { read, after: document.querySelector("#toast .toast-text").textContent };
    });
    assert.deepEqual(takenBack, { read: "Words to keep", after: "Sent to session D" });
  });

  // The priorities saved twice while the first save waits.
  await check("Priorities saved twice while the first save waits save both, the newer last", async () => {
    const listed = async () => (await (await fetch(origin + "/api/goals", { headers: { Cookie: cookie } })).json()).map((goal) => goal.text);
    const sent = [];
    let failFirst = false;
    await page.route("**/api/goals", async (route) => {
      if (route.request().method() !== "PUT") return route.continue();
      sent.push(JSON.parse(route.request().postData()).texts);
      if (sent.length > 1) return route.continue();
      await delay(800);
      if (failFirst) return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "busy" }) });
      await route.continue();
    });
    await page.evaluate(() => { saveGoals("Ship the race fixes"); saveGoals("Ship the race fixes\nReviews"); });
    for (let at = 0; sent.length < 2 && at < 40; at++) await delay(100);
    assert.deepEqual(sent, [["Ship the race fixes"], ["Ship the race fixes", "Reviews"]]);
    await page.waitForFunction(() => !ui.busy.has("goals:set") && ui.goalsNext === null);
    assert.deepEqual(await listed(), ["Ship the race fixes", "Reviews"]);

    // A first save that fails while a newer one waits: the newer one is saved, and the editor opens on the saved list.
    sent.length = 0;
    failFirst = true;
    await page.evaluate(() => { saveGoals("Failed words"); saveGoals("Kept words"); });
    for (let at = 0; sent.length < 2 && at < 40; at++) await delay(100);
    assert.deepEqual(sent, [["Failed words"], ["Kept words"]]);
    await page.waitForFunction(() => !ui.busy.has("goals:set") && ui.goalsNext === null);
    assert.deepEqual(await listed(), ["Kept words"]);
    assert.equal(await page.evaluate(() => ui.goalsUnsaved), null);
    await page.unroute("**/api/goals");
    await page.evaluate(() => fetch("/api/goals", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ texts: [] }) }));
  });

  // A spec editor while its save waits.
  await check("A spec editor takes no input while its save waits", async () => {
    await page.goto(origin + "/spec");
    let release;
    await page.route("**/api/spec/part", async (route) => {
      await new Promise((resolve) => { release = resolve; });
      await route.continue();
    });
    await page.locator('#doc button[aria-label="Edit"]').first().evaluate((button) => button.click());
    const area = page.locator("#doc .spec-box textarea, #doc .row-edit textarea").first();
    await area.waitFor();
    await area.fill(`${await area.inputValue()} Added before Save.`);
    await page.locator("#doc [data-save]").first().click();
    for (let at = 0; !release && at < 40; at++) await delay(100);
    assert.equal(await page.locator("#doc .spec-box, #doc .row-edit").first().evaluate((box) => box.inert), true);
    // A redraw meanwhile (a filter, another Edit) draws the editor anew, and it takes no input either.
    await page.evaluate(() => render());
    assert.equal(await page.locator("#doc .spec-box, #doc .row-edit").first().evaluate((box) => box.inert), true);
    release();
    await page.locator("#doc .spec-box, #doc .row-edit").first().waitFor({ state: "detached" });
    await page.unroute("**/api/spec/part");
  });

  // Two chip choices on one row, the second while the first one's save waits: both are saved (regression check).
  await check("Two chip choices on a row while the first save waits save both", async () => {
    await page.goto(origin + "/spec");
    const bodies = [];
    let release;
    await page.route("**/api/spec/part", async (route) => {
      bodies.push(JSON.parse(route.request().postData()));
      if (bodies.length === 1) await new Promise((resolve) => { release = resolve; });
      await route.continue();
    });
    const key = await page.locator("#doc .req").first().getAttribute("data-block");
    const pick = async (name) => {
      const menu = page.locator(`#doc .req[data-block="${key}"] .choice-menu`).filter({ has: page.locator(`summary[aria-label^="${name}:"]`) });
      await menu.locator("summary").click();
      const option = menu.locator('button[aria-checked="false"]').first();
      const text = (await option.textContent()).trim();
      await option.click();
      return text;
    };
    const strength = await pick("Strength");
    for (let at = 0; !release && at < 40; at++) await delay(100);
    const status = await pick("Status");
    const asks = await page.evaluate(() => {
      const leaving = new Event("beforeunload", { cancelable: true });
      dispatchEvent(leaving);
      return leaving.defaultPrevented;
    });
    assert.ok(asks, "leaving did not ask while the second choice waited");
    release();
    for (let at = 0; bodies.length < 2 && at < 50; at++) await delay(100);
    assert.equal(bodies.length, 2, "the second choice was not sent");
    assert.ok(bodies[1].new.includes(`| ${strength} |`) && bodies[1].new.includes(status), bodies[1].new);
    await page.waitForFunction(() => !ui.busy);
    await page.unroute("**/api/spec/part");
  });

  // A chip choice made while the reload after a save waits goes onto the row as the reload shows it, and a waiting
  // choice can go back to the row's value (CODE-3).
  await check("A chip choice during the reload after a save is saved, and a waiting one can go back", async () => {
    await page.goto(origin + "/spec");
    const key = await page.locator("#doc .req").first().getAttribute("data-block");
    const menuOf = (name) => page.locator(`#doc .req[data-block="${key}"] .choice-menu`).filter({ has: page.locator(`summary[aria-label^="${name}:"]`) });
    const valueOf = async (name) => (await menuOf(name).locator("summary").getAttribute("aria-label")).split(": ")[1];
    const choose = async (name, text) => {
      await menuOf(name).locator("summary").click();
      const option = text ? menuOf(name).locator("button", { hasText: text }) : menuOf(name).locator('button[aria-checked="false"]').first();
      const chosen = (await option.textContent()).trim();
      await option.click();
      return chosen;
    };
    const answers = [];
    page.on("response", (response) => { if (response.url().endsWith("/api/spec/part")) answers.push(response.status()); });
    // The reload after the first save waits until released.
    let releaseLoad;
    let saves = 0;
    await page.route("**/api/spec/part", async (route) => { saves += 1; await route.continue(); });
    await page.route(/\/api\/spec$/, async (route) => {
      if (saves === 1 && !releaseLoad) await new Promise((resolve) => { releaseLoad = resolve; });
      await route.continue();
    });
    await choose("Strength");
    for (let at = 0; !releaseLoad && at < 50; at++) await delay(100);
    assert.ok(releaseLoad, "the reload after the save did not start");
    const original = await valueOf("Status");
    const other = await choose("Status");
    // Back to the row's value while that choice waits: the waiting choice changes the menu, so this one goes too.
    await choose("Status", original);
    releaseLoad();
    for (let at = 0; answers.length < 3 && at < 80; at++) await delay(100);
    await page.waitForFunction(() => !ui.busy && !ui.cellsNext.length);
    assert.deepEqual(answers, [200, 200, 200], `answers ${JSON.stringify(answers)} (Status ${original}, then ${other})`);
    await page.unroute("**/api/spec/part");
    await page.unroute(/\/api\/spec$/);
    await page.reload();
    assert.equal(await valueOf("Status"), original);
  });

  // A briefing whose text failed to load once.
  await check("A briefing whose text failed to load is asked again", async () => {
    const listing = { briefings: [{ id: 7, day: "2026-01-15" }], read_id: 7, team: "Sam, Alex and Ana build Tasks", hour: 10, launch: null };
    const briefing = { id: 7, day: "2026-01-15", since: "2026-01-14T10:00:00Z", until: "2026-01-15T10:00:00Z",
      created_at: "2026-01-15T10:05:00Z", updated_at: "2026-01-15T10:05:00Z", body_html: "<p>Race fixes landed.</p>" };
    let bodyAsks = 0;
    await page.route("**/api/briefings", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify(listing) }));
    await page.route("**/api/briefings/7", (route) => {
      bodyAsks += 1;
      return bodyAsks === 1
        ? route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "busy" }) })
        : route.fulfill({ contentType: "application/json", body: JSON.stringify(briefing) });
    });
    await page.goto(origin + "/briefing");
    for (let at = 0; bodyAsks < 1 && at < 40; at++) await delay(100);
    await page.evaluate(() => window.dispatchEvent(new Event("online")));
    await page.locator("#briefing .briefing-body").filter({ hasText: "Race fixes landed." }).waitFor();
    assert.equal(bodyAsks, 2);
    await page.unroute("**/api/briefings");
    await page.unroute("**/api/briefings/7");
  });

  // On Usage, the week's answer comes after the 24 hours picked since.
  await check("On Usage, a period's late answer gives way to the period picked after it", async () => {
    await page.route("**/api/spend?period=week", async (route) => {
      await delay(1500);
      await route.continue();
    });
    await page.goto(origin + "/usage?period=week");
    await page.locator('.spend-periods button[data-period="1"]').click();
    for (let at = 0; at < 60; at++) {
      if (await page.evaluate(() => ui.data && ui.data.period === "1")) break;
      await delay(100);
    }
    await delay(2000);
    assert.deepEqual(await page.evaluate(() => [ui.period, ui.data && ui.data.period]), ["1", "1"]);
    await page.unroute("**/api/spend?period=week");
  });

  assert.deepEqual(errors, []);
  assert.deepEqual(failed, []);
  console.log("all page race checks passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
}).finally(async () => {
  if (browser) await browser.close();
  server.kill();
});
