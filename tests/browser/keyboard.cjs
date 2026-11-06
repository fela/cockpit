// Run with PLAYWRIGHT_MODULE=/path/to/node_modules/playwright node tests/browser/keyboard.cjs.
// Uses an isolated database and browser profile; never touches live work or real agents.
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");
const { spawn } = require("node:child_process");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "../..");
const work = workFolder("cockpit-keys-");
const server = spawn(path.join(root, ".venv/bin/python"), [path.join(__dirname, "serve_keyboard.py"), work], {
  cwd: root, stdio: ["ignore", "pipe", "pipe"],
});
let serverLog = "";
server.stdout.on("data", (data) => { serverLog += data; });
server.stderr.on("data", (data) => { serverLog += data; });
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let browser;
let page;

(async () => {
  const runtime = path.join(work, "runtime.json");
  for (let at = 0; !fs.existsSync(runtime); at++) {
    if (at > 600 || server.exitCode !== null) throw new Error(`Fixture failed: ${serverLog}`);
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
  page = await context.newPage();
  page.setDefaultTimeout(30e3);
  page.setDefaultNavigationTimeout(60000);
  const errors = [];
  const writes = [];
  page.on("pageerror", (error) => errors.push(error.message));
  // Opening a step's row holds its place (NEXT-16, `/hold`); every other write to the work counts, once: a write the
  // page sends again after a try that got no answer carries the same action id, which Cockpit does once (CODE-5).
  const actions = new Set();
  page.on("request", (request) => {
    if (request.method() === "GET" || !/\/api\/(steps|messages|items)/.test(request.url()) || /\/hold$/.test(request.url())) return;
    const action = request.headers()["x-cockpit-action"];
    if (action && actions.has(action)) return;
    if (action) actions.add(action);
    // Home (/) starts its card's timer too (UI-19). The test leaves / for /classic right
    // away, so /classic finds step 1 on the clock or, while Home's start is still on its way, starts it as well. The
    // checks count /classic's writes.
    if (/\/timer\/start$/.test(request.url()) && new URL(request.frame().url()).pathname === "/") return;
    writes.push(request.url());
  });
  // The page's own writes (the timer's start on its one-second tick) can take seconds on a busy host.
  const wrote = async (pathname) => {
    for (let at = 0; !writes.some((url) => new URL(url).pathname === pathname); at++) {
      if (at > 150) throw new Error(`The page did not write ${pathname}: ${JSON.stringify(writes)}`);
      await delay(100);
    }
  };
  const timerStart = await fetch(origin + "/api/steps/1/timer/start", { method: "POST", headers: { Cookie: cookie, Origin: origin } });
  assert.equal(timerStart.ok, true);
  await page.goto(origin + "/classic");
  await page.locator("#next [data-step]").waitFor();
  const mod = await page.evaluate(() => /Mac|iPhone|iPad/.test(navigator.platform)) ? "Meta" : "Control";
  const press = (key) => page.keyboard.press(key);
  const palette = page.getByRole("dialog", { name: "Command menu", exact: true });
  const search = page.getByRole("combobox", { name: "Search commands, work, and sessions" });

  await press(`${mod}+k`);
  await palette.waitFor();
  await press("Escape");
  await page.evaluate(() => document.activeElement?.blur());
  await press("?");
  await page.getByRole("dialog", { name: "Keyboard shortcuts", exact: true }).waitFor();
  await press("Escape");
  const timed = await (await fetch(origin + "/api/state", { headers: { Cookie: cookie } })).json();
  assert.equal(timed.timer?.step_id, 1);
  assert.equal(writes.length, 0);
  await fetch(origin + "/api/steps/1/timer/pause", { method: "POST", headers: { Cookie: cookie, Origin: origin } });
  await page.reload();
  await page.locator("#next [data-step]").waitFor();
  console.log("PASS Opening commands and help preserves the active work timer");

  await press(`${mod}+k`);
  await palette.waitFor();
  assert.equal(await search.evaluate((node) => node === document.activeElement), true);
  await search.fill("alpha");
  // The one item the words find, the session that works on it right under it, then showing the
  // matches on the page (VIEW-21).
  assert.equal(await palette.getByRole("option").count(), 3);
  assert.match(await palette.getByRole("option").first().innerText(), /Alpha list reminders/);
  assert.match(await palette.getByRole("option").nth(1).innerText(), /session 1111/);
  assert.match(await palette.getByRole("option").nth(2).innerText(), /Show matches on the page/);
  await press("Enter");
  // An item with a step for the user opens as that step: Next up's card or its row in the queue (VIEW-18).
  await page.locator("#next .next-card, #queue .row.open").filter({ hasText: "Alpha list reminders" }).waitFor();
  assert.equal(await palette.isVisible(), false);
  console.log("PASS Cmd+K searches live work and opens its step");

  // A word only in the to-do's summary finds the item, then the session that works on it (SESSION-3); the session stays
  // after the item even when the session's own name matches too.
  const options = async (words) => {
    await press(`${mod}+k`);
    await search.fill(words);
    await palette.getByRole("option").first().waitFor();
    // Each result is one line: its class says what it is, so the text leads with that class.
    const found = await palette.getByRole("option").evaluateAll((nodes) => nodes.map((node) => `${node.className} ${node.innerText}`));
    if (words === "skill" || words === "skill session") {
      const row = palette.getByRole("option").filter({ hasText: /^session 1111$/ });
      assert.equal(await row.count(), 1, `${words}: the work has one session row`);
      const style = await row.evaluate((node) => {
        const computed = getComputedStyle(node);
        return { marginLeft: computed.marginLeft, minHeight: computed.minHeight, height: node.getBoundingClientRect().height };
      });
      assert.equal(style.marginLeft, "18px", `${words}: session indentation ${JSON.stringify(style)}`);
      assert.equal(style.minHeight, "30px", `${words}: session minimum height ${JSON.stringify(style)}`);
      assert.ok(style.height >= 30, `${words}: session row height ${JSON.stringify(style)}`);
    }
    await press("Escape");
    await palette.waitFor({ state: "hidden" });
    return found.map((text) => text.replace(/\s+/g, " "));
  };
  for (const words of ["skill", "skill session"]) {
    const found = await options(words);
    const at = found.findIndex((text) => /^command-work .*Alpha list reminders/.test(text));
    assert.ok(at >= 0, `${words}: ${found.join(" | ")}`);
    // The session sits indented under the work it is on, as the rail lists it.
    assert.match(found[at + 1] || "", /^command-session under session 1111$/, `${words}: ${found.join(" | ")}`);
  }
  const zebra = await options("zebra");
  assert.ok(zebra.some((text) => /Prepare a grocery list/.test(text)), zebra.join(" | "));
  const walrus = await options("walrus");
  assert.ok(walrus.some((text) => /Delta list archive/.test(text)), walrus.join(" | "));
  assert.ok(!walrus.some((text) => /^command-session/.test(text)), walrus.join(" | "));
  console.log("PASS The to-do's words, its agent's summary and its open work find the item and its session");

  // Two found items, one with two sessions: every session lists indented under the best-ranked found item it is
  // on, not after both items, and showing the matches on the page comes right after the Skills page
  // the word names, as two tasks were found. The state gains a session on Beta, one more on Alpha, and words on Beta; with `many`, 60 more on Alpha,
  // which push Beta past the 60 results listed while the matches on the page keep their place.
  let many = false;
  await page.route("**/api/state**", async (route) => {
    const response = await route.fetch();
    const state = await response.json();
    const first = state.sessions.find((session) => session.session_id.startsWith("11111111"));
    const copy = (digits) => ({ ...first, session_id: `${digits}${first.session_id.slice(digits.length)}`, name: `session ${digits}` });
    const extra = many ? Array.from({ length: 60 }, (_, at) => copy(String(5000 + at))) : [];
    state.sessions.push(copy("2222"), copy("3333"), ...extra);
    for (const item of state.items) {
      if (item.title === "Alpha list reminders") item.priority = "urgent";
      if (item.title === "Alpha list reminders") item.sessions = [...item.sessions, copy("3333").session_id, ...extra.map((session) => session.session_id)];
      if (item.title === "Beta CSV export") Object.assign(item, { summary: "Check the export skill instructions", sessions: [copy("2222").session_id] });
    }
    await route.fulfill({ response, json: state });
  });
  await page.reload();
  await page.locator("#next [data-step]").waitFor();
  const grouped = await options("skill");
  assert.deepEqual(grouped.slice(0, 2).map((text) => text.split(" ").slice(0, 3).join(" ")), ["command-other Skills G", "command-other Show matches"]);
  assert.deepEqual(grouped.filter((text) => /^command-(work|session)/.test(text)), [
    "command-work Alpha list reminders", "command-session under session 1111", "command-session under session 3333",
    "command-work Beta CSV export", "command-session under session 2222",
  ]);
  // Found work looks as its row in the list at the left does: neither has a coloured dot;
  // other work than Cockpit's leads with a plain grey mark in the menu and nothing in the list, and urgent work
  // (Alpha) has a red title in the list.
  await page.goto(origin + "/");
  await page.locator("#sidebar .side-item").first().waitFor();
  const rows = await page.locator("#sidebar .side-item").evaluateAll((items) => Object.fromEntries(items.map((row) => [
    row.querySelector(".sidebar-title").textContent,
    { lead: row.querySelector(".sidebar-lead").childElementCount, color: getComputedStyle(row.querySelector(".sidebar-title")).color },
  ])));
  const headingIcons = await page.locator("#sidebar .sidebar-group > summary > svg.icon").count();
  assert.equal(headingIcons, await page.locator("#sidebar .sidebar-group").count(), "every group heading has its icon");
  await press(`${mod}+k`);
  await search.fill("skill");
  await palette.getByRole("option").first().waitFor();
  const menuMarks = await palette.locator(".command-work").evaluateAll((options) => Object.fromEntries(options.map((option) => [
    option.querySelector(".command-title").textContent, option.querySelector(".command-label > :first-child").className,
  ])));
  await press("Escape");
  for (const title of ["Alpha list reminders", "Beta CSV export"]) {
    assert.ok(rows[title], `${title} is in the list`);
    assert.equal(rows[title].lead, 0, `${title} has nothing at its start`);
    assert.equal(menuMarks[title], "command-mark", title);
  }
  assert.notEqual(rows["Alpha list reminders"].color, rows["Beta CSV export"].color, "urgent Alpha has a red title");
  await page.goto(origin + "/classic");
  many = true;
  await page.reload();
  await page.locator("#next [data-step]").waitFor();
  const crowded = await options("skill");
  assert.match(crowded[1], /^command-other Show matches on the page/, crowded.slice(0, 3).join(" | "));
  assert.ok(!crowded.some((text) => /Beta CSV export/.test(text)), "Beta is past the first 60");
  await page.unrouteAll({ behavior: "wait" });
  await page.reload();
  await page.locator("#next [data-step]").waitFor();
  console.log("PASS Each found session lists under the found item it is on");

  await press("g");
  await page.locator(".go-hint").waitFor();
  await press("h");
  // Home is the page with the list of items at its left (VIEW-26); the rest of this test uses /classic.
  await page.waitForURL(origin + "/");
  await page.locator("#sidebar").waitFor();
  await page.goto(origin + "/classic");
  await page.locator("#next [data-step]").waitFor();
  await page.waitForFunction(() => location.hash === "" && window.scrollY === 0);
  await press("j");
  assert.equal(await page.locator("#next .keyboard-current").count(), 1);
  // The user is active on the page with step 1 open on Next up, so its timer starts (TRACK-2, UI-19), here or already
  // on Home.
  await page.locator('#next [data-step="1"].on-clock').waitFor({ timeout: 15e3 });
  await press("j");
  assert.equal(await page.locator("#queue .keyboard-current").count(), 1);
  const selected = await page.locator(".keyboard-current").getAttribute("data-step");
  await press("k");
  assert.equal(await page.locator("#next .keyboard-current").count(), 1);
  await press("ArrowDown");
  assert.equal(await page.locator(".keyboard-current").getAttribute("data-step"), selected);
  const original = await page.locator(".keyboard-current").elementHandle();
  const changed = await fetch(origin + "/api/items/2", {
    method: "PATCH", headers: { Authorization: `Bearer ${"t".repeat(40)}`, "X-Cockpit-As": "keyboard-test",
      "Content-Type": "application/json" }, body: JSON.stringify({ summary: "Updated while navigating" }),
  });
  assert.equal(changed.ok, true);
  // Polled through the handle: a page-side wait on it compiles its check in the page, which the page's content
  // policy refuses once the first check comes back false.
  for (let at = 0; await original.evaluate((node) => node.isConnected); at++) {
    if (at > 80) throw new Error("The current row was not redrawn after the item changed");
    await delay(100);
  }
  await page.locator(".keyboard-current").waitFor();
  assert.equal(await page.locator(".keyboard-current").getAttribute("data-step"), selected);
  await press("Enter");
  await page.locator(`#queue [data-step="${selected}"].open`).waitFor();
  await press("r");
  // Writing in another step's reply box leaves the step's timer running (TRACK-6): at most one start is all /classic
  // wrote so far; the checks below count what typing and IME input write.
  await delay(500);
  const timerWrites = writes.map((url) => new URL(url).pathname);
  assert.deepEqual(timerWrites.filter((pathname) => pathname !== "/api/steps/1/timer/start"), []);
  assert.ok(timerWrites.filter((pathname) => pathname === "/api/steps/1/timer/start").length <= 1, JSON.stringify(timerWrites));
  writes.length = 0;
  // Focus can reach the reply box a moment after the key: 1 run in 8 read it too soon, and found it there right after.
  const reply = page.locator(`#queue [data-step="${selected}"] .composer textarea`);
  for (let at = 0; !(await reply.evaluate((node) => node === document.activeElement)); at++) {
    if (at > 20) throw new Error("R did not focus the highlighted row's reply box");
    await delay(100);
  }
  console.log("PASS J/K, arrows, Enter and R navigate, open and reply to a highlighted row");
  console.log("PASS Row identity and focus survive a real polling redraw");

  await page.keyboard.type("ghexyzajk?");
  assert.equal(await page.locator("dialog[open]").count(), 0);
  assert.equal(await page.locator(".go-hint").isVisible(), false);
  assert.equal(writes.length, 0);
  await page.keyboard.press(`${mod}+a`);
  await press("Backspace");
  const field = page.locator(`#queue [data-step="${selected}"] .composer textarea`);
  await press(`${mod}+k`);
  await palette.waitFor();
  await press("Escape");
  assert.equal(await field.evaluate((node) => node === document.activeElement), true);
  await page.evaluate(() => document.activeElement?.blur());
  await page.evaluate(() => document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "e", bubbles: true, isComposing: true })));
  assert.equal(writes.length, 0);
  console.log("PASS Typing and IME input trigger no actions; closing the palette restores focus");

  await press("?");
  const help = page.getByRole("dialog", { name: "Keyboard shortcuts", exact: true });
  await help.waitFor();
  await press("e"); await press("x"); await press("z");
  assert.equal(writes.length, 0);
  await press("Escape");
  await page.evaluate(() => document.activeElement?.blur());
  await press("g"); await press("x");
  assert.equal(writes.length, 0);
  assert.equal(await page.locator(".go-hint").isVisible(), false);
  console.log("PASS Help and invalid G chords do not leak destructive shortcuts");

  await press("g"); await press("s");
  await page.waitForFunction(() => location.hash === "#sessions");
  await press(`${mod}+k`);
  await search.fill("session 1111");
  await press("Enter");
  // Alpha waits on the user, so its session's own composer is under Task details on Alpha's step on Next up (VIEW-3),
  // and the step's reply stays the step's.
  await page.waitForFunction(() => document.activeElement?.matches('#next .task-details textarea[data-key^="msg:side:"]'));
  await page.evaluate(() => document.activeElement?.blur());
  console.log("PASS G S jumps to Sessions, the list of work; palette opens a session and focuses its composer");

  // G R opens Briefing from Sessions, and Back returns to them.
  await press("g"); await press("r");
  await page.waitForURL(origin + "/briefing");
  await page.goBack();
  await page.waitForFunction(() => location.hash === "#sessions");
  console.log("PASS G R from Sessions opens Briefing");

  await press("g"); await press("u");
  await page.waitForURL(origin + "/usage");
  await press(`${mod}+k`);
  await search.fill("skill");
  await palette.getByRole("option").filter({ hasText: "session 1111" }).waitFor();
  // Gamma lists no session of its own; its step goes to session 1111, which works on it too.
  await search.fill("gamma");
  await palette.getByRole("option").filter({ hasText: "session 1111" }).waitFor();
  await search.fill("beta");
  await palette.getByRole("option").filter({ hasText: "Beta CSV export" }).waitFor();
  await press("Enter");
  await page.waitForURL(/#home\/item\//);
  await page.locator("#next .next-card, #queue .row.open").filter({ hasText: "Beta CSV export" }).waitFor();
  console.log("PASS The switcher searches work from another page and follows its destination");

  for (const route of ["/spec", "/skills", "/briefing", "/usage"]) {
    await page.goto(origin + route);
    await page.getByRole("button", { name: "Search commands", exact: true }).waitFor();
    await press("?");
    await help.waitFor();
    await press("Escape");
    await press("g"); await press("h");
    await page.waitForURL(origin + "/");
  }
  console.log("PASS Shared keyboard navigation and help work on Spec, Skills, Briefing and Usage");

  await press(`${mod}+k`);
  await search.fill("no-matching-command-zzzzzz");
  await page.getByText("No matching commands, work, or sessions.", { exact: true }).waitFor();
  await press("Enter");
  assert.equal(await palette.isVisible(), true);
  await press("Escape");
  assert.deepEqual(errors, []);
  // Each page load above may start the timer of the step it shows open, and leaving it pauses it (TRACK-2, TRACK-6),
  // as the page's tick happens to fall; nothing else writes.
  assert.deepEqual(writes.filter((url) => !/^\/api\/steps\/\d+\/timer\/(start|pause)$/.test(new URL(url).pathname)), []);
  console.log("PASS Empty search is safe, no browser errors, no work mutations");

  await press(`${mod}+k`);
  await search.fill("");
  await press("ArrowDown");
  assert.equal(await search.getAttribute("aria-activedescendant"), "cockpit-command-1");
  await press("Tab");
  assert.equal(await page.evaluate(() => document.querySelector("dialog[open]").contains(document.activeElement)), true);
  await page.emulateMedia({ colorScheme: "dark" });
  await page.screenshot({ path: path.join(work, "command-menu.png") });
  await press("Escape");
  await page.setViewportSize({ width: 390, height: 844 });
  // On a phone the item and the list are two screens; Search sits in the rail, the list screen, which the item
  // screen's bar opens (UI-56).
  await page.locator(".phone-back").click();
  await page.getByRole("button", { name: "Search commands", exact: true }).click();
  assert.equal(await page.locator(".command-dialog").evaluate((node) => node.getBoundingClientRect().right <= innerWidth), true);
  await page.screenshot({ path: path.join(work, "command-menu-mobile.png") });
  await press("Escape");
  await page.evaluate(() => document.activeElement?.blur());
  await press("?");
  await help.waitFor();
  await page.screenshot({ path: path.join(work, "shortcut-help-mobile.png") });
  console.log(`PASS Palette navigation, native focus trap, and mobile layout; screenshots in ${work}`);
})().catch(async (error) => {
  console.error(error);
  if (page) {
    console.error(await page.evaluate(() => ({ active: document.activeElement?.outerHTML.slice(0, 500),
      selection: window.getSelection()?.toString(), modal: document.querySelector("dialog[open]")?.outerHTML.slice(0, 200),
      highlighted: [...document.querySelectorAll(".keyboard-current")].map((n) => n.getAttribute("data-step")) })));
    await page.screenshot({ path: path.join(work, "failure.png") });
    console.error(`Screenshot: ${path.join(work, "failure.png")}`);
  }
  console.error(serverLog);
  process.exitCode = 1;
})
  .finally(async () => { if (browser) await browser.close(); server.kill("SIGTERM"); });
