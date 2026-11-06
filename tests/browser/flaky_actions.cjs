// The user's actions go through a connection that drops (SPEC CODE-5).
// Checks, on the main page at phone width:
// - an answer pressed offline shows at once, the header says it waits to send, and nothing reaches Cockpit;
// - back online, it reaches Cockpit once, the header note goes, and no failure shows;
// - an answer whose first answer from Cockpit is lost on the way back is sent again and still makes one message;
// - an action whose try stalls (no answer at all) shows as waiting after a few seconds, and is dropped after its try
//   time and sent again;
// - presses on one step or item made offline reach Cockpit in the order made: Done, Undo, Done leaves the step done,
//   and of two priorities the later one holds;
// - a reply still waiting when the page reloads is kept in the browser: the reload asks nothing, and the reloaded page
//   sends it once Cockpit is reachable, once;
// - a reply the reloaded page sends but Cockpit refuses puts its words back in the step's reply box, with the "To"
//   choice it was sent with, and the page says so;
// - another open tab leaves a reply waiting in the first tab alone, and sends it once that tab is closed.
//
// Starts serve_preview.py (this checkout's Cockpit on a copy of the live database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
// A run takes about three minutes.
//
// Usage, from the checkout: node tests/browser/flaky_actions.cjs

"use strict";

const { spawn, spawnSync } = require("node:child_process");
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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "flaky-test", "content-type": "application/json" };

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-flaky-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    const cookie = { cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json" };
    const agent = (method, url, body) => call(info.origin, AGENT, method, url, body);
    const user = (method, url, body) => call(info.origin, cookie, method, url, body);

    // The fixture database's open steps are snoozed, so this run's steps take the user's places.
    const copied = await user("GET", "/api/state");
    await Promise.all([...copied.queue, ...copied.ready_next, ...copied.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    const run = Date.now();
    const steps = {};
    for (const name of ["offline", "lost", "stalled", "order", "reload", "refused", "closed", "typing"]) {
      await agent("PUT", "/api/items", { key: `note:flaky-${run}-${name}`, title: `Flaky test: ${name}`, priority: "urgent" });
      const answer = await agent("POST", "/api/steps", {
        item_key: `note:flaky-${run}-${name}`, need: `flaky-${name}-${run}`, ask: `Flaky test: ${name}`, score: 100,
        body: "Flaky test body.", session_id: SESSION_B, answers: [`Go ahead ${name}`],
      });
      steps[name] = answer.step.id;
    }

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    await context.addInitScript(() => {
      window.__toasts = [];
      addEventListener("DOMContentLoaded", () => {
        const el = document.getElementById("toast");
        new MutationObserver(() => { if (!el.hidden) window.__toasts.push(el.textContent); }).observe(el, { childList: true, subtree: true, attributes: true });
      });
    });
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    await page.goto(`${info.origin}/classic`);
    const card = (id) => page.locator(`[data-step="${id}"]`).first();
    const press = async (id, text) => {
      const folded = card(id).locator('.row-title[aria-expanded="false"]');
      if (await folded.count()) await folded.click();
      await card(id).locator(".answers .answer", { hasText: text }).click();
    };
    const unsent = () => page.locator("#unsent").evaluate((el) => (el.hidden ? "" : el.textContent));
    // Whether the note reads `text` within 25 s: it shows 15 s after the press, and a loaded machine makes tries late.
    const unsentReads = async (text) => {
      for (const end = Date.now() + 25000; Date.now() < end; await sleep(200)) if ((await unsent()) === text) return true;
      return false;
    };
    const dialogs = [];
    page.on("dialog", (dialog) => { dialogs.push(dialog.type()); dialog.dismiss(); });
    const failures = async () => (await page.evaluate(() => window.__toasts.slice())).filter((text) => /Not sent|Failed to fetch/.test(text));
    const handOffs = [];
    page.on("request", (request) => { if (/\/hand-off$/.test(request.url())) handOffs.push(request.headers()["x-cockpit-action"]); });
    // A hand-off's message goes once its notice closes: the page then sends a release, which Cockpit answers.
    const released = new Set();
    page.on("response", (response) => {
      const id = /\/api\/messages\/(\d+)\/release$/.exec(response.url())?.[1];
      if (id && response.ok()) released.add(Number(id));
    });

    // 1. Offline: the press shows at once, waits, and goes once the connection is back.
    await card(steps.offline).waitFor();
    // The user opened the step while online: the press on it holds its place (NEXT-16), so offline only the
    // answer waits to send.
    const holding = page.waitForResponse((response) => response.url().endsWith(`/api/steps/${steps.offline}/hold`));
    await card(steps.offline).locator(".ask, .row-title").first().click();
    await holding;
    await context.setOffline(true);
    await press(steps.offline, "Go ahead offline");
    await sleep(1500);
    check("an answer pressed offline leaves the queue at once", await card(steps.offline).count() === 0 || !(await card(steps.offline).isVisible()),
      "the step still shows in the queue");
    check("the header says it waits to send", await unsentReads("1 waiting to send"), `the header says "${await unsent()}"`);
    await sleep(5000);
    check("nothing reaches Cockpit while offline", messagesFor(work, steps.offline) === 0, `${messagesFor(work, steps.offline)} messages`);
    await context.setOffline(false);
    const reached = await until(() => messagesFor(work, steps.offline) === 1, 15e3);
    check("back online, the answer reaches Cockpit once", reached, `${messagesFor(work, steps.offline)} messages`);
    await sleep(1000);
    check("the header note goes", (await unsent()) === "", `the header says "${await unsent()}"`);
    check("no failure shows", (await failures()).length === 0, (await failures()).join(" | "));

    // 2. The first try reaches Cockpit, but its answer is lost on the way back: the page sends it again.
    let lost = 0;
    await page.route("**/hand-off", async (route) => {
      if (lost === 0) {
        lost += 1;
        await route.fetch();
        return route.abort("connectionreset");
      }
      return route.continue();
    });
    const before = handOffs.length;
    await press(steps.lost, "Go ahead lost");
    const settled = await until(async () => (await unsent()) === "" && handOffs.length - before >= 2, 15e3);
    const ids = new Set(handOffs.slice(before));
    check("a lost answer is sent again under the same id", settled && ids.size === 1, `${handOffs.length - before} tries, ids ${[...ids].join(", ")}`);
    await sleep(1000);
    check("the repeat makes no second message", messagesFor(work, steps.lost) === 1, `${messagesFor(work, steps.lost)} messages`);
    check("no failure shows after a lost answer", (await failures()).length === 0, (await failures()).join(" | "));
    await page.unroute("**/hand-off");

    // 3. The first try stalls with no answer at all (a connection that hangs): it is dropped and sent again.
    let stalled = 0;
    await page.route("**/hand-off", (route) => {
      if (stalled === 0) { stalled += 1; return; }
      return route.continue();
    });
    await press(steps.stalled, "Go ahead stalled");
    check("a stalled try shows as waiting after 15 s", await unsentReads("1 waiting to send"), `the header says "${await unsent()}"`);
    const recovered = await until(() => messagesFor(work, steps.stalled) === 1, 45e3);
    check("a stalled try is sent again and reaches Cockpit", recovered, `${messagesFor(work, steps.stalled)} messages`);
    check("no failure shows after a stall", (await failures()).length === 0, (await failures()).join(" | "));
    await page.unroute("**/hand-off");

    // 4. Offline, Done, Undo, Done on one step: back online, all three reach Cockpit in order and the step is done.
    // First the stalled hand-off's notice closes and its release goes, so only these presses wait.
    const stalledMessage = Number(readOne(work, "SELECT max(id) FROM messages WHERE step_id = ?", steps.stalled));
    await until(() => released.has(stalledMessage), 60e3);
    await context.setOffline(true);
    await page.evaluate((id) => {
      const find = () => [...ui.state.queue, ...ui.state.ready_next].find((step) => step.id === id);
      const step = find();
      act(step, "done");
      act(step, "reopen");
      act(step, "done");
    }, steps.order);
    // The Undo and the second Done wait in the page behind the first Done, so one is on its way.
    check("Done, Undo, Done offline wait to send", await unsentReads("1 waiting to send"), `the header says "${await unsent()}"`);
    await context.setOffline(false);
    const closed = await until(async () => (await unsent()) === "" && stepStatus(work, steps.order) === "done", 20e3);
    check("back online, the last Done holds", closed, `the step is ${stepStatus(work, steps.order)}`);

    // 5. Offline, two priorities on one item: back online, the later one holds.
    await context.setOffline(true);
    await page.evaluate((key) => {
      const item = ui.state.items.find((candidate) => candidate.key === key);
      setPriority(item, "high");
      setPriority(item, "low");
    }, `note:flaky-${run}-order`);
    await sleep(1000);
    await context.setOffline(false);
    const latest = await until(async () => (await unsent()) === "" && itemPriority(work, `note:flaky-${run}-order`) === "low", 20e3);
    check("back online, the later priority holds", latest, `the priority is ${itemPriority(work, `note:flaky-${run}-order`)}`);
    check("no failure shows after the ordered presses", (await failures()).length === 0, (await failures()).join(" | "));

    // 6. A reply waits for a Cockpit out of reach (the page itself still loads), and the page reloads: the
    // reloaded page sends it once Cockpit answers again.
    // `choice`: the box's "To" choice when the user picked one (a new session in the first checkout), else none.
    const reply = async (id, words, choice) => {
      await card(id).waitFor();
      await page.evaluate(({ id, words, choice }) => {
        const step = [...ui.state.queue, ...ui.state.ready_next].find((other) => other.id === id);
        const key = `msg:step:${id}`;
        ui.drafts.set(key, words);
        if (choice) ui.choices.set(key, `new:${ui.state.checkouts[0]}`);
        handOffDraft(key, step, { session_id: step.target.session_id }, "the test session");
      }, { id, words, choice });
    };
    const kept = () => page.evaluate(() => Object.keys(localStorage).filter((key) => key.startsWith("cockpit.outbox.")).length);
    const draft = (id) => page.evaluate((key) => ui.drafts.get(key) || "", `msg:step:${id}`);
    // Hand-offs meet a Cockpit out of reach, one that refuses them, or Cockpit itself.
    let reach = "down";
    await page.route("**/hand-off", (route) => {
      if (reach === "down") return route.abort("internetdisconnected");
      if (reach === "refusing") return route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: "Test refusal" }) });
      return route.continue();
    });
    await reply(steps.reload, "Words from before the reload", false);
    check("a reply on its way is kept in the browser", await until(async () => (await kept()) === 1, 5e3), `${await kept()} kept`);
    await page.reload();
    check("the reload asks nothing", dialogs.length === 0, dialogs.join(", "));
    check("the reloaded page shows the reply waiting to send", await unsentReads("1 waiting to send"), `the header says "${await unsent()}"`);
    check("nothing reaches Cockpit while it is out of reach", messagesFor(work, steps.reload) === 0, `${messagesFor(work, steps.reload)} messages`);
    reach = "up";
    const replayed = await until(async () => messagesFor(work, steps.reload) === 1 && (await unsent()) === "" && (await kept()) === 0, 30e3);
    check("Cockpit back, the reloaded page sends the reply once", replayed, `${messagesFor(work, steps.reload)} messages, ${await kept()} kept`);
    check("its words stay out of the box", (await draft(steps.reload)) === "", `the box holds "${await draft(steps.reload)}"`);

    // 7. A reply left by a reloaded page that Cockpit refuses: its words go back into the step's reply box.
    reach = "down";
    await reply(steps.refused, "Words that come back", true);
    await until(async () => (await kept()) === 1, 5e3);
    await page.reload();
    reach = "refusing";
    const back = await until(async () => (await draft(steps.refused)) === "Words that come back", 30e3);
    check("a refused reply from the page before puts its words back in the box", back, `the box holds "${await draft(steps.refused)}"`);
    const said = (await page.evaluate(() => window.__toasts.slice())).some((text) => text.includes("Not sent from the page you left: Test refusal"));
    check("the page says the reply was not sent", said, (await page.evaluate(() => window.__toasts.slice())).join(" | "));
    const shown = await until(async () => (await page.locator(`textarea[data-key="msg:step:${steps.refused}"]`).count()) === 1
      && (await page.locator(`textarea[data-key="msg:step:${steps.refused}"]`).inputValue()) === "Words that come back", 10e3);
    check("the step's reply box shows the words", shown, "no reply box with the words");
    // The reply was sent with a new session picked, but a step with a session shows no To picker and its box
    // writes to that session: the words come back addressed to it.
    const to = await page.evaluate((key) => ui.choices.get(key), `msg:step:${steps.refused}`);
    check("its words come back addressed to the step's session", to === `session:${SESSION_B}`, `the box writes to "${to}"`);
    check("nothing is left kept", (await kept()) === 0, `${await kept()} kept`);

    // 8. A reply waits in this tab while a second tab opens: the second leaves it to this one, and sends it once
    // this tab is closed (the second tab looks every minute).
    reach = "down";
    await reply(steps.closed, "Words from a closed tab", false);
    await until(async () => (await kept()) === 1, 5e3);
    const owner = () => page.evaluate(() => Object.entries(localStorage).find(([key]) => key.startsWith("cockpit.outbox."))?.[1]);
    const first = JSON.parse(await owner()).page;
    const other = await context.newPage();
    await other.goto(`${info.origin}/classic`);
    await sleep(3000);
    check("a second tab leaves the first tab's reply alone", JSON.parse(await owner()).page === first && messagesFor(work, steps.closed) === 0,
      `${messagesFor(work, steps.closed)} messages`);
    await page.close({ runBeforeUnload: false });
    const taken = await until(() => messagesFor(work, steps.closed) === 1, 90e3);
    check("once the first tab is closed, the second sends its reply once", taken, `${messagesFor(work, steps.closed)} messages`);

    // 9. Words another tab puts back into a reply box the user types in join it at its top, in the plain box and
    // in the Vim editor, and Send takes them.
    const key = `msg:step:${steps.typing}`;
    await other.evaluate((id) => {
      ui.expanded.add(`step:${id}`);
      ui.expanded.add(`msg:step:${id}`);
      safeRender();
    }, steps.typing);
    const box = other.locator(`textarea[data-key="${key}"]`);
    await box.waitFor();
    await box.click();
    await other.keyboard.type("still typing ");
    const third = await context.newPage();
    await third.goto(`${info.origin}/classic`);
    // Another tab sees a page's writes a moment later: the third tab puts words back once it sees the box's latest text.
    const putBack = async (text) => {
      const latest = await box.inputValue();
      await until(() => third.evaluate(({ key, latest }) => (new Map(JSON.parse(localStorage.getItem("cockpit.drafts") || "[]")).get(key) || "") === latest,
        { key, latest }), 5e3);
      await putBackNow(text);
    };
    const putBackNow = (text) => third.evaluate(({ key, text }) => {
      const drafts = new Map(JSON.parse(localStorage.getItem("cockpit.drafts") || "[]"));
      const now = (drafts.get(key) || "").trim();
      drafts.set(key, now ? `${text}\n\n${now}` : text);
      localStorage.setItem("cockpit.drafts", JSON.stringify([...drafts]));
      tellPutBack(key, text, drafts.get(key));
    }, { key, text });
    await putBack("Words put back");
    const joined = await until(async () => (await box.inputValue()) === "Words put back\n\nstill typing ", 5e3);
    check("words put back by another tab join the box the user types in", joined, `the box holds "${await box.inputValue()}"`);
    await other.keyboard.type("more");
    const kept9 = await other.evaluate((key) => JSON.parse(localStorage.getItem("cockpit.drafts")).find(([name]) => name === key)?.[1], key);
    check("the next keystroke keeps them", kept9 === "Words put back\n\nstill typing more", `stored "${kept9}"`);
    // Typing in the same box in another tab is no put-back: the box the user types in here keeps its text.
    await until(() => third.evaluate((key) => (new Map(JSON.parse(localStorage.getItem("cockpit.drafts") || "[]")).get(key) || "").endsWith("more"), key), 5e3);
    await third.evaluate((key) => {
      const drafts = new Map(JSON.parse(localStorage.getItem("cockpit.drafts") || "[]"));
      drafts.set(key, `${drafts.get(key)} more`);
      localStorage.setItem("cockpit.drafts", JSON.stringify([...drafts]));
    }, key);
    await sleep(1000);
    check("typing in the same box in another tab adds nothing here", (await box.inputValue()) === "Words put back\n\nstill typing more",
      `the box holds "${await box.inputValue()}"`);
    await other.evaluate(() => CockpitEditor.set(true));
    const editor = other.locator(`.cm-content[data-key="${key}"]`);
    await editor.waitFor();
    await editor.focus();
    await other.keyboard.press("A");
    await other.keyboard.type(" in vim");
    await putBack("Vim words");
    const vimJoined = await until(async () => (await box.inputValue()) === "Vim words\n\nWords put back\n\nstill typing more in vim"
      && (await editor.innerText()).startsWith("Vim words"), 5e3);
    check("in the Vim editor too", vimJoined, `the box holds "${await box.inputValue()}"`);
    await other.evaluate(() => CockpitEditor.set(false));
    await box.fill("");
    await box.click();
    await putBack("Into an empty box");
    const send = other.locator(`[data-step="${steps.typing}"] button.send`);
    const sendable = await until(async () => (await box.inputValue()) === "Into an empty box" && !(await send.isDisabled()), 5e3);
    check("an empty box takes them with Send ready", sendable, `the box holds "${await box.inputValue()}"`);
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

async function until(test, ms) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (await test()) return true;
    await sleep(250);
  }
  return Boolean(await test());
}

// The messages written for the step in the harness's synthetic database.
function messagesFor(work, stepId) {
  const script = "import sqlite3, sys; conn = sqlite3.connect(sys.argv[1]); "
    + "print(conn.execute('SELECT COUNT(*) FROM messages WHERE step_id = ?', (int(sys.argv[2]),)).fetchone()[0])";
  const out = spawnSync(PYTHON, ["-c", script, path.join(work, "cockpit.db"), String(stepId)], { encoding: "utf8" });
  if (out.status !== 0) throw new Error(`reading the messages failed: ${out.stderr}`);
  return Number(out.stdout.trim());
}

function readOne(work, sql, value) {
  const script = "import sqlite3, sys; conn = sqlite3.connect(sys.argv[1]); "
    + "print(conn.execute(sys.argv[2], (sys.argv[3],)).fetchone()[0])";
  const out = spawnSync(PYTHON, ["-c", script, path.join(work, "cockpit.db"), sql, String(value)], { encoding: "utf8" });
  if (out.status !== 0) throw new Error(`reading the database failed: ${out.stderr}`);
  return out.stdout.trim();
}

const stepStatus = (work, stepId) => readOne(work, "SELECT status FROM steps WHERE id = ?", stepId);
const itemPriority = (work, key) => readOne(work, "SELECT priority FROM items WHERE key = ?", key);

async function call(origin, headers, method, url, body) {
  const response = await fetch(`${origin}${url}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
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
