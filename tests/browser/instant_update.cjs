// Every control the user presses on the main page changes the page at once, before the server answers.
//
// Starts serve_slow.py (this checkout's Cockpit on a synthetic database, every non-GET /api/
// request held 2 s), seeds fresh items, steps and permission requests through the agent API, signs
// in with the harness's cookie, and presses each control. A control passes when the change it should
// make (the step leaves, Next up changes, a toast or the row's Undo says what happened, Update shows busy, ...) is on
// the page within LIMIT_MS of the click. With the server held for 2 s, a change that waits for the
// server shows up after about 2000 ms and fails. A change after SLOW_MS passes and is marked slow: the page's
// redraw on the synthetic data takes about 300 ms on a loaded host.
//
// Usage, from the checkout: node tests/browser/instant_update.cjs
// See tests/browser/README.md for what it needs.

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
const LIMIT_MS = 1000;
const SLOW_MS = 300;
// How long to watch for a late change, so a failure says how late it came.
const WATCH_MS = 3500;
// The fake sessions serve_slow.py lists (tests/conftest.py): SESSION_B is working.
const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "instant-test", "content-type": "application/json" };

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// In the page: where things are, leaving out the copies motion.js keeps while a row animates out.
function pageHelpers() {
  const live = (selector) => [...document.querySelectorAll(selector)].filter((el) => !el.closest(".motion-ghost"));
  window.__live = live;
  // Where a step's row shows in the list: "queue" (Inbox), "snoozed" (in Waiting), "agents" (handed, in Busy or
  // Waiting), "other", or null.
  window.__row = (id) => {
    const row = live(`#sidebar li[data-step="${id}"]`)[0];
    if (!row) return null;
    if (row.classList.contains("snoozed")) return "snoozed";
    if (row.closest(".sidebar-group.queue")) return "queue";
    if (row.closest(".sidebar-group:is(.busy, .waiting)")) return "agents";
    return "other";
  };
  // Where a step shows: "next" (its card on Next up), "agents" (its working card, or its row in Busy or Waiting), or
  // where its row is.
  window.__place = (id) => {
    if (live(`#next .working-card[data-step="${id}"]`).length) return "agents";
    if (live(`#next .next-card[data-step="${id}"]`).length) return "next";
    return window.__row(id);
  };
  // The step on Next up's card, or null.
  window.__card = () => {
    const el = live("#next .next-card[data-step]")[0];
    return el ? Number(el.dataset.step) : null;
  };
  window.__toast = () => !document.getElementById("toast").hidden;
  // The page said what happened to the task: a toast, or the task's row turned into its Undo (a hand-off's or a
  // close's), which stands in for the toast.
  window.__said = (itemId) => window.__toast() || live(`#sidebar li:is(.held-hand, .held-close)[data-item="${itemId}"]`).length > 0;
  // Whether Update is out of reach: it is starting, or a sweep or check runs (app.js).
  window.__busy = () => updateBlocked(ui.state);
  // Watch for `effect` (an expression over `arg`) from the next click on. Returns true when it holds already.
  // `hideToast`: a toast left from an earlier press goes first, so only a new one counts.
  window.__arm = (effect, arg, hideToast) => {
    if (hideToast) document.getElementById("toast").hidden = true;
    const holds = new Function("arg", `return Boolean(${effect});`);
    if (holds(arg)) return true;
    const probe = { clickAt: null, effectAt: null, mutationAt: null, error: null };
    const test = () => {
      if (probe.clickAt === null || probe.effectAt !== null) return;
      try {
        if (holds(arg)) probe.effectAt = performance.now();
      } catch (error) {
        probe.error = String(error);
      }
    };
    const observer = new MutationObserver(() => {
      if (probe.clickAt !== null && probe.mutationAt === null) probe.mutationAt = performance.now();
      test();
    });
    observer.observe(document.body, { subtree: true, childList: true, attributes: true, characterData: true });
    const onClick = () => {
      probe.clickAt = performance.now();
      // After the click's own handlers have run.
      setTimeout(test, 0);
    };
    window.addEventListener("click", onClick, { capture: true, once: true });
    const tick = () => {
      test();
      if (probe.effectAt === null && !probe.stopped) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
    probe.stop = () => {
      probe.stopped = true;
      observer.disconnect();
      window.removeEventListener("click", onClick, { capture: true });
    };
    window.__probe = probe;
    return false;
  };
}

async function main() {
  const work = workFolder("cockpit-instant-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_slow.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  try {
    const info = await harnessUp(work, server);
    const seeded = await seed(info, work);
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const results = await pressAll(browser, info, seeded);
    report(results, work);
    process.exitCode = results.some((result) => result.verdict !== "pass") ? 1 : 0;
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
}

// The harness's cookie file, once it is written and the server answers.
async function harnessUp(work, server) {
  const file = path.join(work, "cookie.json");
  const end = Date.now() + 60e3;
  while (Date.now() < end) {
    if (server.exitCode !== null) throw new Error(`serve_slow.py exited with ${server.exitCode}`);
    if (fs.existsSync(file)) {
      const info = JSON.parse(fs.readFileSync(file, "utf8"));
      const up = await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false);
      if (up) return info;
    }
    await sleep(200);
  }
  throw new Error("serve_slow.py did not come up within 60 s");
}

async function call(origin, headers, method, url, body) {
  const response = await fetch(`${origin}${url}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await response.text();
  if (!response.ok) throw new Error(`${method} ${url}: ${response.status} ${text}`);
  return text ? JSON.parse(text) : null;
}

// Fresh items, steps and permission requests for this run; each request waits 2 s, so they go in parallel.
async function seed(info, work) {
  const agent = (method, url, body) => call(info.origin, AGENT, method, url, body);
  const user = (method, url, body) => call(info.origin,
    { cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json" }, method, url, body);
  const run = Date.now();
  const names = ["reorder", "timer", "answer", "send", "archive", "undo", "dismiss", "snooze", "feedback", "run", "allow",
    "complete", "redo", "closing", "close"];
  // The fixture database's open steps are snoozed, so this run's steps take the user's places.
  const initial = await user("GET", "/api/state");
  await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
  // The rival's item gets its step only in the Resorted press.
  await Promise.all([...names, "rival"].map((name) => agent("PUT", "/api/items", { key: `note:instant-${run}-${name}`, title: `Instant test: ${name}`, priority: "urgent" })));
  const extra = {
    timer: { body: "Instant test body: its card's clock has the timer's play." },
    run: { command: { run: "true", cwd: work, explain: "Does nothing." } },
    allow: { permission: { list: "allow", rule: `Bash(echo instant-${run}:*)` } },
    closing: { answers: [{ text: "I've sent it myself; close", closes: true }, "Change it"] },
  };
  const steps = {};
  const items = {};
  const put = async (name) => {
    const answer = await agent("POST", "/api/steps", {
      item_key: `note:instant-${run}-${name}`, need: `instant-${name}-${run}`, ask: `Instant test: ${name}`, score: name === "reorder" ? 99 : 50,
      answers: ["Works", "Change it"], session_id: SESSION_B, ...(extra[name] || {}),
    });
    steps[name] = answer.step.id;
    items[name] = answer.step.item_id;
  };
  // "reorder" ranks first and is Next up, with its timer running as on a step the user is on (NEXT-16).
  await put("reorder");
  await user("POST", `/api/steps/${steps.reorder}/timer/start`);
  await Promise.all(names.slice(1).map(put));
  // A second open step on these items, so their key is Done or Archive, not the last step's finish panel (CONTROL-15).
  // It is a permission ask: an item shows one card at a time (SPEC NEXT-55), and a permission ask is not a card.
  await Promise.all(["archive", "undo"].map((name) => agent("POST", "/api/steps", {
    item_key: `note:instant-${run}-${name}`, need: `instant-${name}-other-${run}`, ask: `Instant test: ${name}, other step`, score: 1,
    session_id: SESSION_B, permission: { list: "allow", rule: `Bash(echo instant-${name}-other-${run}:*)` },
  })));
  const requests = {};
  await Promise.all(["allow", "deny"].map(async (name) => {
    const answer = await agent("POST", "/api/permissions/requests", {
      kind: "asked", session_id: SESSION_B, cwd: work, tool_name: "Bash", tool_input: { command: `echo instant-${name}-${run}` },
      tool_use_id: "", reason: "instant test",
    });
    requests[name] = answer.id;
  }));
  return { run, steps, items, requests };
}

async function pressAll(browser, info, { run, steps, items, requests }) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, bypassCSP: true });
  await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
  await context.addInitScript(pageHelpers);
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  // The main page's first load takes longer than a press on the synthetic database.
  page.setDefaultNavigationTimeout(30000);
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  const inflight = new Set();
  page.on("request", (request) => { if (request.method() !== "GET" && request.url().includes("/api/")) inflight.add(request); });
  // The server has answered once its response comes in; a fetch that never reads the body (presence.js)
  // gets no requestfinished.
  page.on("response", (response) => inflight.delete(response.request()));
  page.on("requestfinished", (request) => inflight.delete(request));
  page.on("requestfailed", (request) => inflight.delete(request));
  // The page's API traffic, for an error that needs to say what the page had asked and heard.
  const traffic = [];
  const note = (what, request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/") && url.pathname !== "/api/presence") traffic.push({ at: Date.now(), line: `${what} ${request.method()} ${url.pathname}` });
  };
  page.on("request", (request) => note("ask", request));
  page.on("response", (response) => note("answer", response.request()));
  const trafficTail = () => traffic.slice(-8).map(({ at, line }) => `${at - Date.now()} ms ${line}`).join(" | ");
  await page.goto(`${info.origin}/`);
  await page.waitForSelector("#next .next-card, #sidebar li[data-step]", { timeout: 15e3 });

  const results = [];
  // Each result as it comes, so a slow run shows where it is.
  const record = (result) => {
    results.push(result);
    console.error(`  ${result.verdict} ${result.name}${result.ms === undefined ? "" : ` (${result.ms} ms)`}${result.note ? `: ${result.note}` : ""}`);
  };

  // Waits until the page's writes have answered and the redraw after them is in.
  async function settle() {
    const end = Date.now() + 20e3;
    while (inflight.size && Date.now() < end) await sleep(50);
    if (inflight.size) console.error(`  still waiting on ${[...inflight].map((request) => `${request.method()} ${new URL(request.url()).pathname}`).join(", ")}`);
    await sleep(800);
  }

  // Records whether the server, too, has the step in the user's queue or ready next within 8 s. An undo's
  // request goes only once the request it undoes was answered, so it can still be on its way.
  async function serverClosed(name, id) {
    const start = Date.now();
    let where = null;
    while (Date.now() - start < 8e3) {
      where = await page.evaluate(async (stepId) => {
        const state = await (await fetch("/api/state")).json();
        return ["queue", "ready_next", "snoozed", "with_agents"].find((list) => state[list].some((step) => step.id === stepId)) || "closed";
      }, id);
      if (where === "closed") {
        record({ name, verdict: "pass", note: `${Date.now() - start} ms after the writes had settled` });
        return;
      }
      await sleep(250);
    }
    record({ name, verdict: "fail", note: `the step is still in ${where}` });
  }

  async function serverHasBack(name, id) {
    const start = Date.now();
    let where = null;
    while (Date.now() - start < 8e3) {
      where = await page.evaluate(async (stepId) => {
        const state = await (await fetch("/api/state")).json();
        return ["queue", "ready_next", "snoozed", "with_agents"].find((list) => state[list].some((step) => step.id === stepId)) || "closed";
      }, id);
      if (where === "queue" || where === "ready_next") {
        record({ name, verdict: "pass", note: `${Date.now() - start} ms after the writes had settled` });
        return;
      }
      await sleep(250);
    }
    record({ name, verdict: "error", note: `the server has the step under ${where} 8 s after the writes had settled` });
  }

  // Arms the watch, clicks `target`, and records how long until `effect` held. A press on the
  // toast's own button keeps the toast.
  async function press(name, target, effect, arg) {
    const hideToast = !(await target.evaluate((el) => Boolean(el.closest("#toast")), null, { timeout: 5000 }));
    if (await page.evaluate(([source, value, hide]) => window.__arm(source, value, hide), [effect, arg, hideToast])) {
      record({ name, verdict: "error", note: "the expected change was on the page before the press" });
      return;
    }
    await target.click({ timeout: 5000 });
    await page.waitForFunction(() => window.__probe.effectAt !== null || window.__probe.error !== null, null, { timeout: WATCH_MS, polling: 16 })
      .catch(() => {});
    const probe = await page.evaluate(() => {
      const { clickAt, effectAt, mutationAt, error } = window.__probe;
      window.__probe.stop();
      return { clickAt, effectAt, mutationAt, error };
    });
    if (probe.clickAt === null) { record({ name, verdict: "error", note: "the click did not reach the page" }); return; }
    const ms = probe.effectAt === null ? null : Math.round(probe.effectAt - probe.clickAt);
    const firstChange = probe.mutationAt === null ? null : Math.round(probe.mutationAt - probe.clickAt);
    const slow = ms !== null && ms > SLOW_MS && ms <= LIMIT_MS ? `slow: over ${SLOW_MS} ms` : "";
    record({ name, verdict: ms !== null && ms <= LIMIT_MS ? "pass" : "fail", ms, firstChange, note: probe.error || slow });
  }

  // Runs one control's preparation and press; a control that cannot be reached is an error, not a stop.
  async function control(name, work) {
    try {
      await work();
    } catch (error) {
      record({ name, verdict: "error", note: error.message.split("\n")[0] });
    }
  }

  // Opens the list's group that holds `row` when it is folded.
  async function openGroupOf(row) {
    await row.evaluate((el) => {
      for (let group = el.closest("details"); group; group = group.parentElement.closest("details")) group.open = true;
    });
  }

  // The step on Next up's card: its row in the list opens it there (behind Inbox's "Show N more" if need be).
  async function card(id) {
    const shown = page.locator(`#next .next-card:not(.working-card)[data-step="${id}"]`).first();
    for (let attempt = 0; attempt < 4; attempt++) {
      const where = await page.evaluate((step) => window.__place(step), id);
      if (where === "next") return shown;
      if (where === null) {
        if (await showMore()) continue;
        throw new Error(`step ${id} is not on the page (${await whereIs(id)}; traffic: ${trafficTail()})`);
      }
      if (where === "agents") throw new Error(`step ${id} is handed to an agent`);
      const row = page.locator(`#sidebar li[data-step="${id}"]`).first();
      await openGroupOf(row);
      await row.locator(".side-row").click();
      await shown.waitFor({ timeout: 5000 });
      return shown;
    }
    throw new Error(`step ${id} did not show (${await whereIs(id)}; traffic: ${trafficTail()})`);
  }

  // Opens Inbox's ranked steps behind "Show N more"; false when none are hidden.
  async function showMore() {
    const more = page.locator("#sidebar li.sidebar-more > .side-row");
    if (!(await more.count()) || !/^Show \d+ more/.test(await more.textContent())) return false;
    await more.click();
    return true;
  }

  // The Undo of a task's held row (a hand-off's or a close's): the row itself while its group is open, otherwise
  // the Undo on the folded group's heading.
  async function heldUndo(itemId, groups) {
    const row = page.locator(`#sidebar li:is(.held-hand, .held-close)[data-item="${itemId}"] > .side-row`).first();
    if (await row.isVisible()) return row;
    return page.locator(`#sidebar .sidebar-group:is(${groups}) > summary .busy-undo`).first();
  }

  // For an error: the step's elements on the page and the lists of the page's state that hold it.
  function whereIs(id) {
    return page.evaluate((step) => {
      const els = [...document.querySelectorAll(`[data-step="${step}"]`)].map((el) => {
        const group = el.closest(".sidebar-group");
        const name = group && group.querySelector(":scope > summary .sidebar-group-name");
        return `${el.className}${el.closest(".motion-ghost") ? " (ghost)" : ""} in ${name ? name.textContent : el.closest("[id]").id}`;
      });
      /* global ui */
      const lists = typeof ui === "undefined" || !ui.state ? "no state"
        : ["queue", "ready_next", "snoozed", "with_agents"].filter((list) => ui.state[list].some((other) => other.id === step)).join(", ") || "in no list";
      const pending = typeof ui === "undefined" ? ""
        : `, ${ui.pending.size} pending changes, last redraw ${Date.now() - ui.renderedAt} ms ago`;
      const active = document.activeElement;
      const focus = active && active !== document.body
        ? `${active.tagName.toLowerCase()}${active.dataset.key ? `[data-key=${active.dataset.key}]` : ""} in #${active.closest("[id]") && active.closest("[id]").id}`
        : "none";
      return `on the page: ${els.join("; ") || "nothing"}; state: ${lists}${pending}; focus: ${focus}`;
    }, id);
  }

  // An entry of the card's ⋯ menu (VIEW-29), which the press opens first.
  async function menuEntry(cardEl, words) {
    await cardEl.locator("details.step-menu > summary").first().click();
    return cardEl.locator("details.step-menu .menu-list button").filter({ hasText: words }).first();
  }

  // A permission request's card on Next up, which its red row in Inbox opens.
  async function openRequest(id) {
    const el = page.locator(`#next .blocked-card[data-request="${id}"]`).first();
    if (await el.isVisible()) return el;
    const row = page.locator(`#sidebar li.warn-row[data-request="${id}"]`).first();
    if (!(await row.count())) throw new Error(`request ${id} is not on the page`);
    await openGroupOf(row);
    await row.locator(".side-row").click();
    await el.waitFor({ timeout: 5000 });
    return el;
  }

  const gone = (id) => `window.__place(${id}) === null`;
  // The request's row and card are off the page.
  const answered = (id) => `!window.__live('#sidebar li.warn-row[data-request="${id}"], #next [data-request="${id}"]').length`;

  // Resorted (NEXT-16): the step the user is on keeps its place when another step ranks above it, and shows
  // Resorted; a press puts it in its ranked place and Next up shows the highest-ranked step. The user's press on its
  // card holds its place. The queue goes by the sweeper's score (RANK-18): a new step scores above it, and enough above
  // the others to take a place. The new step is on an item of its own, as an item shows one card at a time (SPEC
  // NEXT-55).
  await control("Resorted", async () => {
    const el = await card(steps.reorder);
    await el.click({ position: { x: 8, y: 8 } });
    await call(info.origin, AGENT, "POST", "/api/steps", {
      item_key: `note:instant-${run}-rival`, need: `instant-rival-${run}`, ask: "Instant test: rival", score: 100, session_id: SESSION_B,
    });
    // The Resorted key sits on the card's top edge, before the card in #next.
    const button = "#next .resorted-pin .reorder.resorted";
    await page.waitForSelector(button, { timeout: 15e3 });
    await press("Resorted", page.locator(button),
      `window.__card() !== ${steps.reorder} && window.__row(${steps.reorder}) === "queue"`, {});
  });
  await settle();

  // Timer: Start the timer in the card's ⋯ menu. The timer has no Pause (VIEW-29); the page stops it itself afterwards.
  // Opening the card starts its timer (UI-19), so the page stops that start first, as it would for the user's pause.
  await control("Timer: play (step card)", async () => {
    const el = await card(steps.timer);
    await settle();
    /* global setTimer */
    await page.evaluate((id) => setTimer(id, false), steps.timer);
    await settle();
    await el.locator(".step-menu > summary").first().click();
    await press("Timer: play (step card)", el.locator('.step-menu [data-timer="start"]').first(),
      `window.__live('[data-step="${steps.timer}"].on-clock').length > 0`, {});
  });
  await settle();
  await page.evaluate((id) => setTimer(id, false), steps.timer);
  await settle();

  // The priority sits in the step's ⋯ menu.
  await control("Priority (menu)", async () => {
    const el = await card(steps.timer);
    await el.locator(".step-menu > summary").first().click();
    const opener = el.locator(".step-menu .menu-opens[data-priority]").first();
    const before = await opener.getAttribute("data-priority");
    await opener.click();
    await press("Priority (menu)", el.locator(".step-menu [role=menuitemradio][aria-checked=false]").first(),
      `window.__live('[data-step="${steps.timer}"] .step-menu [data-priority]:not([data-priority="${before}"])').length > 0`, {});
  });
  await settle();

  await control("One-click answer", async () => {
    const el = await card(steps.answer);
    await press("One-click answer", el.locator(".answers button.answer").first(),
      `window.__place(${steps.answer}) === "agents" && window.__said(${items.answer})`, {});
  });
  await settle();
  // Once the hand-off's Undo time is over, the handed step's row in Busy opens its working card, whose Take back
  // brings the step back to the user.
  await control("Take back (working card)", async () => {
    await page.locator(`#sidebar li.held-hand[data-step="${steps.answer}"]`).waitFor({ state: "detached", timeout: 20e3 });
    const row = page.locator(`#sidebar li[data-step="${steps.answer}"]`).first();
    await openGroupOf(row);
    await row.locator(".side-row").click();
    const button = page.locator(`#next .working-card[data-step="${steps.answer}"] .working-back`).first();
    await button.waitFor({ timeout: 5000 });
    await press("Take back (working card)", button,
      `window.__place(${steps.answer}) !== "agents" && window.__place(${steps.answer}) !== null`, {});
  });
  await settle();

  await control("Send (step composer)", async () => {
    const el = await card(steps.send);
    await el.locator(`textarea[data-key="msg:step:${steps.send}"]`).fill(`Instant test reply ${run}`);
    await press("Send (step composer)", el.locator(".composer button.send").first(),
      `window.__place(${steps.send}) === "agents" && window.__said(${items.send})`, {});
    // The user presses the hand-off's Undo while it is still on its way.
    await press("Undo (right after Send)", await heldUndo(items.send, ".busy, .waiting"),
      `window.__place(${steps.send}) !== "agents" && window.__place(${steps.send}) !== null`, {});
  });
  await settle();
  await serverHasBack("Undo of Send reaches the server", steps.send);

  // The step's close key: Done, or Archive session on a session's last step.
  const doneKey = '.actions button:is([aria-label^="Done"], .archive-key)';
  await control("Archive", async () => {
    const el = await card(steps.archive);
    await press("Archive", el.locator(doneKey).first(), `${gone(steps.archive)} && window.__toast()`, {});
  });
  await settle();
  await control("Undo (toast, right after Archive)", async () => {
    const el = await card(steps.undo);
    await press("Archive (second step)", el.locator(doneKey).first(), `${gone(steps.undo)} && window.__toast()`, {});
    await press("Undo (toast, right after Archive)", page.locator("#toast button", { hasText: "Undo" }),
      `window.__place(${steps.undo}) !== null`, {});
  });
  await settle();
  await serverHasBack("Undo reaches the server", steps.undo);

  await control("Dismiss (step menu)", async () => {
    const el = await card(steps.dismiss);
    const entry = await menuEntry(el, "Dismiss");
    await press("Dismiss (step menu)", entry, `${gone(steps.dismiss)} && window.__toast()`, {});
  });
  await settle();

  // Task done on the item's last step completes the item in one press (CONTROL-15); the task's row in Finished this
  // week holds its Undo.
  await control("Task done", async () => {
    const el = await card(steps.complete);
    await press("Task done", el.locator(".actions button.task-done").first(), `${gone(steps.complete)} && window.__said(${items.complete})`, {});
  });
  await settle();
  await control("Undo (right after Task done)", async () => {
    const el = await card(steps.redo);
    await press("Task done (second item)", el.locator(".actions button.task-done").first(), `${gone(steps.redo)} && window.__said(${items.redo})`, {});
    await press("Undo (right after Task done)", await heldUndo(items.redo, ".done"), `window.__place(${steps.redo}) !== null`, {});
  });
  await settle();
  await serverHasBack("Undo of Task done reaches the server", steps.redo);
  // Task done takes what the reply box holds as the user's last message: its key says so, one press completes the
  // task, and the server takes the close.
  await control("Task done + send (last message)", async () => {
    const el = await card(steps.redo);
    await el.locator(`textarea[data-key="msg:step:${steps.redo}"]`).first().fill("Instant test: one last message");
    const words = await el.locator(".actions button.task-done .label").first().textContent();
    if (words !== "Task done + send") throw new Error(`the key reads ${JSON.stringify(words)}`);
    await press("Task done + send (last message)", el.locator(".actions button.task-done").first(), `${gone(steps.redo)} && window.__said(${items.redo})`, {});
  });
  await settle();
  await serverClosed("Task done + send reaches the server", steps.redo);

  await control("Closing answer", async () => {
    const el = await card(steps.closing);
    await press("Closing answer", el.locator(".answers button.answer.closes:not(.task-done)").first(), `${gone(steps.closing)} && window.__said(${items.closing})`, {});
  });
  await settle();

  await control("Close task (step menu)", async () => {
    const el = await card(steps.close);
    const entry = await menuEntry(el, "Close task");
    await press("Close item: open box", entry, `window.__live('[data-step="${steps.close}"] .close-box').length > 0`, {});
    const box = page.locator(`[data-step="${steps.close}"] .close-box`).first();
    await box.locator("textarea").fill("Instant test: not needed any more");
    await press("Close item: Close item", box.locator("button.close-item"), `${gone(steps.close)} && window.__said(${items.close})`, {});
  });
  await settle();

  await control("Snooze (menu time)", async () => {
    const el = await card(steps.snooze);
    await (await menuEntry(el, "Snooze")).click();
    const entry = el.locator("details.step-menu .menu-choices button").first();
    await press("Snooze (menu time)", entry, `window.__card() !== ${steps.snooze} && window.__row(${steps.snooze}) !== "queue" && window.__toast()`, {});
  });
  await settle();
  // Once the server answered and its state is in, the step's row shows snoozed in Waiting. A poll (5 s) may
  // bring it; when it never shows, a redraw puts it there so Unsnooze can still be pressed.
  await control("Snoozed row shows", async () => {
    const start = Date.now();
    const shown = await page.waitForFunction((id) => window.__row(id) === "snoozed", steps.snooze, { timeout: 6000, polling: 50 })
      .then(() => true, () => false);
    if (shown) { record({ name: "Snoozed row shows", verdict: "pass", note: `${Date.now() - start} ms after the server's answer had settled` }); return; }
    const where = await whereIs(steps.snooze);
    /* global render */
    await page.evaluate(() => render(false));
    throw new Error(`no snoozed row 6 s after the server's answer (${where})`);
  });
  await control("Unsnooze", async () => {
    const el = await card(steps.snooze);
    await press("Unsnooze", await menuEntry(el, "Unsnooze"),
      `window.__row(${steps.snooze}) !== "snoozed"`, {});
  });
  await settle();

  await control("Run (step command)", async () => {
    const el = await card(steps.run);
    await press("Run (step command)", el.locator(".step-run button.primary", { hasText: "Run" }),
      `window.__place(${steps.run}) === "agents" && window.__said(${items.run})`, {});
  });
  await settle();
  await control("Allow (step permission)", async () => {
    const el = await card(steps.allow);
    await press("Allow (step permission)", el.locator(".step-run button.primary", { hasText: "Allow" }),
      `window.__place(${steps.allow}) === "agents" && window.__said(${items.allow})`, {});
  });
  await settle();

  await control("Run (permission card)", async () => {
    const el = await openRequest(requests.allow);
    await press("Run (permission card)", el.locator(".actions button.primary", { hasText: "Run" }).first(), answered(requests.allow), {});
  });
  await settle();
  await control("Deny (permission card)", async () => {
    const el = await openRequest(requests.deny);
    await press("Deny (permission card)", el.locator('.actions button[aria-label^="Deny"]').first(), answered(requests.deny), {});
  });
  await settle();

  await control("Add: submit", async () => {
    await press("Add: open box", page.locator(".sidebar-add"), "!document.getElementById('add-box').hidden", {});
    await page.locator('#add-box textarea[data-key="add:new"]').fill(`Instant test to-do ${run}`);
    // The new to-do's row in Busy, or Busy's heading when it is folded, offers its Undo.
    await press("Add: submit", page.locator("#add-box .add-foot button.primary"),
      `window.__live('#sidebar [aria-label^="Undo adding"]').length > 0`, {});
  });
  await settle();

  await control("Feedback mode: Send", async () => {
    await page.evaluate(() => startPicking("feedback"));
    await page.waitForFunction(() => document.body.classList.contains("picking") && !document.getElementById("pick-bar").hidden);
    await press("Feedback mode: pick a part", page.locator("#goals .goals-label").first(), "!document.getElementById('pick-box').hidden", {});
    await page.locator('#pick-box textarea[data-key="fb:pick"]').fill(`Instant test pick feedback ${run}`);
    await press("Feedback mode: Send", page.locator("#pick-box .actions button", { hasText: "Send" }),
      "document.getElementById('pick-box').hidden && window.__toast()", {});
  });
  await settle();

  // Priorities are one line; Save in Priorities and inbox, which opens in the card's place, writes the list.
  const goal = `Instant test goal ${run}`;
  await control("Priorities: Save", async () => {
    await page.locator("#settings-view .settings-card").waitFor({ state: "detached" });
    await page.locator("#goals .goals-box").click();
    await page.locator("#settings-view .goal-entry-text").first().waitFor();
    await page.locator("#settings-view .goal-entry").evaluateAll((items) => items.slice(1).forEach((item) => item.querySelector(".inbox-remove").click()));
    await page.locator("#settings-view .goal-entry-text").first().fill(goal);
    await press("Priorities: Save", page.locator("#settings-view button[type=submit]"),
      "window.__live('#goals .goals-text')[0]?.innerText.trim() === arg.goal", { goal });
  });
  await settle();
  await control("Priorities: Save empty", async () => {
    await page.locator("#settings-view .settings-card").waitFor({ state: "detached" });
    await page.locator("#goals .goals-box").click();
    await page.locator("#settings-view .goal-entry-text").first().waitFor();
    await page.locator("#settings-view .goal-entry").evaluateAll((items) => items.slice(1).forEach((item) => item.querySelector(".inbox-remove").click()));
    await page.locator("#settings-view .goal-entry-text").first().fill("");
    await press("Priorities: Save empty", page.locator("#settings-view button[type=submit]"),
      "window.__live('#goals .goals-text')[0]?.innerText.trim() === '[empty]'", {});
  });
  await settle();

  await control("Update", async () => {
    if (await page.evaluate(() => window.__busy())) throw new Error("Update was busy before the press: a sweep or check was running");
    // Update board in the command menu, as the U key runs it.
    await page.locator(".rail-search .command-trigger").click();
    const dialog = page.locator("dialog.command-dialog[open]");
    await dialog.locator('.command-search input[role="combobox"]').fill("Update board");
    await press("Update", dialog.locator('li[role="option"]', { hasText: "Update board" }).first(), "window.__busy()", {});
  });
  await settle();

  for (const message of pageErrors) record({ name: "page error", verdict: "error", note: message });
  await context.close();
  return results;
}

function report(results, work) {
  const pad = (text, width) => String(text).padEnd(width);
  console.log(`\n${pad("control", 38)}${pad("result", 8)}${pad("changed after", 15)}${pad("first DOM change", 18)}note`);
  for (const result of results) {
    const ms = result.ms === undefined ? "" : result.ms === null ? `> ${WATCH_MS} ms` : `${result.ms} ms`;
    const first = result.firstChange === undefined || result.firstChange === null ? "" : `${result.firstChange} ms`;
    console.log(`${pad(result.name, 38)}${pad(result.verdict, 8)}${pad(ms, 15)}${pad(first, 18)}${result.note || ""}`);
  }
  const failed = results.filter((result) => result.verdict !== "pass").length;
  console.log(`\n${results.length - failed} of ${results.length} passed (limit ${LIMIT_MS} ms, slow over ${SLOW_MS} ms: ${results.filter((result) => result.note?.startsWith("slow")).length}; the server holds every write 2 s). Work dir: ${work}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 2;
});
