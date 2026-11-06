// An item holding an unsent draft is marked in the list at the left of / (SPEC NEXT-39): an amber pencil before the
// row's age. Checks, at desktop width:
// - no pencil while nothing is typed;
// - typing in the item's step's reply box shows the pencil on that item's row at once, and on no other row;
// - it is there again after a reload, and while another item shows;
// - Send takes it away, and a refused reply brings it back with the words;
// - emptying the box takes it away;
// - words left in the item's close reason, or in the conversation view of its own session, show it too, and not on
//   another item whose step answers to that session;
// - the pencil is in the user's amber;
// - no page errors.
//
// Starts serve_preview.py (this checkout's Cockpit on a copy of the live database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/sidebar_draft.cjs

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
const TOKEN = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "sidebar-draft-test" };

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-sidebar-draft-");
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

    // The fixture database's open steps are snoozed, so this run's steps lead the queue.
    const copied = await user("GET", "/api/state");
    await Promise.all([...copied.queue, ...copied.ready_next, ...copied.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    const run = Date.now();
    // Both steps answer to the fake session, but only the first item has it as its own (primary) session, so a draft
    // in that session's conversation view is the first item's alone.
    const put = async (slug, title, score, sessions) => {
      const key = `note:${slug}-${run}`;
      await agent("PUT", "/api/items", { key, title, priority: "urgent", sessions });
      const answer = await agent("POST", "/api/steps", {
        item_key: key, need: `${slug}-${run}`, ask: `${title}?`, score, body: "Say yes or no.", session_id: SESSION_B,
      });
      return answer.step;
    };
    const first = await put("pencil-first", "Pick the reminder time", 100, [SESSION_B]);
    const second = await put("pencil-second", "Merge the fix", 90, []);

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const pencils = () => page.evaluate((ids) => ids.map((id) =>
      [...document.querySelectorAll(`.sidebar-list [data-draft-item="${id}"]`)].some((el) => !el.hidden && el.getBoundingClientRect().width > 0)),
    [first.item_id, second.item_id]);
    const settle = (want) => waitFor(pencils, (shown) => JSON.stringify(shown) === JSON.stringify(want));
    const open = async (itemId) => {
      await page.goto(`${info.origin}/#board/item/${itemId}`);
      await page.locator(`.sidebar-list [data-draft-item="${itemId}"]`).first().waitFor({ state: "attached", timeout: 30e3 });
    };
    const box = page.locator(`#next textarea[data-key="msg:step:${first.id}"]`).first();

    await open(first.item_id);
    await box.waitFor({ timeout: 30e3 });
    check("no pencil while nothing is typed", JSON.stringify(await pencils()) === "[false,false]", JSON.stringify(await pencils()));

    await box.click();
    await page.keyboard.type("Friday at ten");
    const typed = await settle([true, false]);
    check("typing shows the pencil on its item's row only", JSON.stringify(typed) === "[true,false]", JSON.stringify(typed));
    const colour = await page.evaluate((id) => {
      const paint = document.createElement("canvas").getContext("2d");
      paint.fillStyle = getComputedStyle(document.querySelector(`.sidebar-list [data-draft-item="${id}"]`)).color;
      paint.fillRect(0, 0, 1, 1);
      return [...paint.getImageData(0, 0, 1, 1).data].slice(0, 3);
    }, first.item_id);
    check("the pencil is amber", colour[0] - colour[2] >= 60, JSON.stringify(colour));

    await page.reload();
    await box.waitFor({ timeout: 30e3 });
    const reloaded = await settle([true, false]);
    check("after a reload the pencil is there again", JSON.stringify(reloaded) === "[true,false]", JSON.stringify(reloaded));
    await open(second.item_id);
    const elsewhere = await settle([true, false]);
    check("while another item shows, the pencil stays", JSON.stringify(elsewhere) === "[true,false]", JSON.stringify(elsewhere));

    // Cockpit holds the reply until the test has looked at the row, then refuses it.
    await open(first.item_id);
    await box.waitFor({ timeout: 30e3 });
    let handed = null;
    let refuse = null;
    const looked = new Promise((resolve) => { refuse = resolve; });
    await page.route("**/hand-off", async (route) => {
      handed = route.request().postDataJSON();
      await looked;
      return route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: "Test refusal" }) });
    });
    await page.locator(`#next .composer:has(textarea[data-key="msg:step:${first.id}"]) .send`).first().click();
    await waitFor(async () => handed, Boolean, 30e3);
    const sent = await settle([false, false]);
    refuse();
    const back = await settle([true, false]);
    const words = await waitFor(() => page.evaluate((key) => ui.drafts.get(key) || "", `msg:step:${first.id}`), (text) => text === "Friday at ten", 30e3);
    check("Send takes the pencil away; refused, it comes back with the words",
      JSON.stringify(sent) === "[false,false]" && JSON.stringify(back) === "[true,false]" && words === "Friday at ten", JSON.stringify({ sent, back, words }));
    await page.unroute("**/hand-off");

    await page.reload();
    await box.waitFor({ timeout: 30e3 });
    await box.click();
    await page.keyboard.press("Control+A");
    await page.keyboard.press("Backspace");
    const cleared = await settle([false, false]);
    check("emptying the box takes the pencil away", JSON.stringify(cleared) === "[false,false]", JSON.stringify(cleared));

    // Words kept from before, in the close reason and in the session's conversation view.
    // The conversation view keeps one box per item it was opened for (regression check): its words mark that item alone.
    for (const [what, key, want] of [
      ["a close reason", `close:${first.id}`, [true, false]],
      ["the session's conversation view opened for the first item", `msg:view:${SESSION_B}:${first.item_id}`, [true, false]],
      ["the session's conversation view opened for the second item", `msg:view:${SESSION_B}:${second.item_id}`, [false, true]],
    ]) {
      await page.evaluate((key) => localStorage.setItem("cockpit.drafts", JSON.stringify([[key, "Half a thought"]])), key);
      await page.reload();
      await box.waitFor({ timeout: 30e3 });
      const shown = await settle(want);
      check(`words left in ${what} show the pencil on that item`, JSON.stringify(shown) === JSON.stringify(want), JSON.stringify(shown));
    }
    // Words kept under the key from before the view's box was per item (regression check) still mark the item the session
    // leads, and the view opened for that item takes them over.
    await page.evaluate((key) => localStorage.setItem("cockpit.drafts", JSON.stringify([[key, "Older thought"]])), `msg:view:${SESSION_B}`);
    await page.reload();
    await box.waitFor({ timeout: 30e3 });
    const old = await settle([true, false]);
    check("words under the old key show the pencil on the item the session leads", JSON.stringify(old) === "[true,false]", JSON.stringify(old));
    await page.evaluate(([session, itemId]) => window.openSessionView(session, itemId), [SESSION_B, first.item_id]);
    const adopted = await waitFor(() => page.evaluate(() => {
      const area = document.querySelector("#session-composer textarea");
      return { box: area ? area.value : null, stored: JSON.parse(localStorage.getItem("cockpit.drafts") || "[]") };
    }), (seen) => seen.box === "Older thought", 30e3);
    check("the view opened for that item takes the old words over",
      adopted.box === "Older thought" && JSON.stringify(adopted.stored.map(([key]) => key)) === JSON.stringify([`msg:view:${SESSION_B}:${first.item_id}`]),
      JSON.stringify(adopted));
    await page.evaluate(() => localStorage.setItem("cockpit.drafts", "[]"));

    check("no page errors", errors.length === 0, errors.join(" | "));
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

// The value `read` gives once `ok` holds it, or its last value after `ms` (5 s unless given).
async function waitFor(read, ok, ms = 5000) {
  const end = Date.now() + ms;
  let value = await read();
  while (!ok(value) && Date.now() < end) {
    await sleep(100);
    value = await read();
  }
  return value;
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
