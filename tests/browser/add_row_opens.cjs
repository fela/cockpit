// Run with PLAYWRIGHT_MODULE=/path/to/node_modules/playwright node tests/browser/add_row_opens.cjs.
// Uses the keyboard fixture's isolated database; never touches live work or real agents.
//
// CONTROL-18: a to-do just added opens from its row in Busy as soon as the server made it, also while the
// state the page asks every few seconds is slow to come, as it is on a loaded host. At 1280 px, with every state
// held from the Add on:
// - a click on the row while the Add is on its way to the server changes nothing yet;
// - once the server answers the Add, the row carries the server's item, with no new state, and that click opens it
//   on the card;
// - words sent from that card before the to-do's session started go as Add more's do, not to a new session;
// - a click on a second to-do's row on its way, followed by a click on another item, leaves the card on that item
//   when the server answers;
// - the page logs no error.
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");
const { spawn } = require("node:child_process");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "../..");
const work = workFolder("cockpit-add-row-opens-");
const server = spawn(path.join(root, ".venv/bin/python"), [path.join(__dirname, "serve_keyboard.py"), work], {
  cwd: root, stdio: ["ignore", "pipe", "pipe"],
});
let serverLog = "";
server.stdout.on("data", (data) => { serverLog += data; });
server.stderr.on("data", (data) => { serverLog += data; });
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let browser;

// A gate that holds the requests routed through it until it opens.
function gate() {
  let open;
  const opened = new Promise((resolve) => { open = resolve; });
  return { opened, open };
}

(async () => {
  const runtime = path.join(work, "runtime.json");
  for (let at = 0; !fs.existsSync(runtime); at++) {
    if (at > 1200 || server.exitCode !== null) throw new Error(`Fixture failed: ${serverLog}`);
    await delay(100);
  }
  const { origin, cookie } = JSON.parse(fs.readFileSync(runtime, "utf8"));
  for (let at = 0; ; at++) {
    try { if ((await fetch(origin + "/healthz")).ok) break; } catch {}
    if (at > 600) throw new Error(`Fixture did not listen: ${serverLog}`);
    await delay(100);
  }
  browser = await chromium.launch(process.env.BROWSER_EXECUTABLE ? { executablePath: process.env.BROWSER_EXECUTABLE } : {});
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await context.setExtraHTTPHeaders({ cookie });
  const page = await context.newPage();
  page.setDefaultTimeout(60000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let states = null;
  let adds = null;
  await page.route("**/api/state*", async (route) => { if (states) await states.opened; await route.continue(); });
  await page.route("**/api/items/add", async (route) => { if (adds) await adds.opened; await route.continue(); });
  await page.goto(origin + "/");
  await page.locator("#sidebar .sidebar-list li[data-item] > .side-row").first().waitFor({ state: "attached" });
  await page.evaluate(() => { for (const group of document.querySelectorAll("#sidebar details")) group.open = true; });

  // The row of the to-do titled `title`: its id as the list shows it, and whether it is the Undo still.
  const rowOf = (title) => page.evaluate((title) => {
    const row = [...document.querySelectorAll("#sidebar li[data-item]")].find((one) => one.textContent.includes(title));
    return row ? { id: row.dataset.item, held: row.classList.contains("held") } : null;
  }, title);
  // Presses the row's button inside the page: the list redraws often, and a click from outside can meet a button
  // that was just replaced.
  const press = (title) => page.evaluate((title) => {
    const row = [...document.querySelectorAll("#sidebar li")].find((one) => one.querySelector(".side-row") && one.textContent.includes(title));
    row.querySelector(".side-row").click();
  }, title);
  // Adds a to-do and waits until its Undo time is over: its row is then on its way, under the page's own id.
  const add = async (title) => {
    states = gate();
    adds = gate();
    await page.keyboard.press("a");
    const area = page.locator('#add-box textarea[data-key="add:new"]');
    await area.waitFor();
    await area.fill(title);
    await area.press("Enter");
    await page.waitForFunction((title) => {
      const row = [...document.querySelectorAll("#sidebar li[data-item]")].find((one) => one.textContent.includes(title));
      return row && !row.classList.contains("held");
    }, title, { timeout: 20000 });
    const row = await rowOf(title);
    assert.match(row.id, /^local-/, `the row on its way shows under the page's id, not ${row.id}`);
  };

  const first = "Follow up on the offsite budget";
  await add(first);
  await press(first);
  await delay(500);
  assert.equal((await page.locator("#next").innerText()).includes(first), false, "the card showed the to-do before the server made it");
  adds.open();
  // No state comes meanwhile: the row and the card follow the server's answer alone.
  await page.waitForFunction((title) => {
    const row = [...document.querySelectorAll("#sidebar li[data-item]")].find((one) => one.textContent.includes(title));
    return row && /^\d+$/.test(row.dataset.item) && ui.shownItem === Number(row.dataset.item);
  }, first, { timeout: 10000 });
  await page.locator("#next").getByText(first).first().waitFor({ timeout: 10000 });
  const made = Number((await rowOf(first)).id);
  await page.screenshot({ path: path.join(work, "opened.png") });
  // A message from the card before the to-do's session started joins its first message, as Add more's words do,
  // rather than start a second session without the to-do's text.
  const words = "Also check the venue deposit";
  const sends = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && (request.postData() || "").includes(words)) sends.push(new URL(request.url()).pathname);
  });
  const box = page.locator('#next textarea[aria-label="Message"]').first();
  await box.fill(words);
  const joined = page.waitForResponse((response) => response.url().endsWith(`/api/items/${made}/add-more`), { timeout: 20000 });
  await box.press("Control+Enter");
  assert.equal((await joined).ok(), true, "Add more refused the card's words");
  await delay(1000);
  assert.deepEqual(sends, [`/api/items/${made}/add-more`], "the card's words went elsewhere than Add more");
  states.open();

  // A click elsewhere after the click on the row keeps the card where the user went.
  const second = "Book the train tickets";
  await add(second);
  await press(second);
  await press("Alpha list reminders");
  const other = await page.evaluate(() => ui.shownItem);
  assert.notEqual(other, made, "the click on Alpha did not open it");
  adds.open();
  await page.waitForFunction((title) => {
    const row = [...document.querySelectorAll("#sidebar li[data-item]")].find((one) => one.textContent.includes(title));
    return row && /^\d+$/.test(row.dataset.item);
  }, second, { timeout: 10000 });
  await delay(1000);
  assert.equal(await page.evaluate(() => ui.shownItem), other, "the card left the item the user opened after the click");
  states.open();

  assert.deepEqual(errors, []);
  console.log("PASS 1280: a just-added row on its way opens once the server made it, with no new state; the card's words join Add more; a later click elsewhere wins; no page errors");
  await context.close();
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
}).finally(async () => {
  await browser?.close();
  server.kill();
});
