// Early looks (SPEC NEXT-42): a session's early result or question takes a place only while no other step waits.
// On a synthetic database, puts an early look scored 99 and checks that it waits while the queue is full or
// another step waits. Then the user dismisses every other open step: the early look takes a place, and the card
// shows the Early look tag. Last, four ordinary steps scored 10 fill the queue and the early look waits again.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs)
// and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/early_looks.cjs [screenshot.png]
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

const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "early-looks", "content-type": "application/json" };

async function main() {
  const work = workFolder("cockpit-early-looks-");
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
    const headers = { cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json" };
    const call = async (who, method, url, body) => {
      const response = await fetch(`${info.origin}${url}`, { method, headers: who, body: body === undefined ? undefined : JSON.stringify(body) });
      if (!response.ok) throw new Error(`${method} ${url}: HTTP ${response.status} ${await response.text()}`);
      return response.json();
    };
    const key = `note:early-looks-${Date.now()}`;
    await call(AGENT, "PUT", "/api/items", { key, title: "Early looks test", stage: "building", sessions: [SESSION_B] });
    const early = (await call(AGENT, "POST", "/api/steps", { item_key: key, need: "early:export", ask: "The sample export is ready: keep the reminder column?", session_id: SESSION_B, score: 99, size: "small", progress: 0, early: true })).step;
    let state = await call(headers, "GET", "/api/state");
    const others = state.ready_next.filter((step) => !step.early);
    const placed = state.queue.some((step) => step.id === early.id);
    console.error(`  live queue ${state.queue.length}, other steps waiting ${others.length}, early look placed: ${placed}`);
    check("waits while the queue is full or another step waits", !placed || others.length === 0,
      JSON.stringify({ queue: state.queue.length, others: others.length, placed }));
    for (const step of [...state.queue, ...state.ready_next].filter((step) => step.id !== early.id)) {
      await call(headers, "POST", `/api/steps/${step.id}/dismiss`, {});
    }
    state = await call(headers, "GET", "/api/state");
    check("takes a free place once nothing else waits", state.queue.some((step) => step.id === early.id), JSON.stringify(state.queue.map((step) => step.id)));

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, colorScheme: "dark" });
    await context.setExtraHTTPHeaders({ cookie: headers.cookie });
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => { if (message.type() === "error") console.error(`  console: ${message.text()}`); });
    await page.goto(`${info.origin}/`, { timeout: 60000 });
    // The first load of a synthetic database can take a while.
    const tag = page.locator(".chip.early").first();
    await tag.waitFor({ timeout: 30000 });
    if (process.argv[2]) await page.screenshot({ path: process.argv[2], timeout: 30000 });
    check("the card shows the Early look tag", (await tag.textContent()).includes("Early look"), await tag.textContent());

    const ordinary = [];
    for (let n = 0; n < 4; n += 1) {
      ordinary.push((await call(AGENT, "POST", "/api/steps", { item_key: key, need: `decide:${n}`, ask: `Include list ${n} in the export?`, session_id: SESSION_B, score: 10 })).step.id);
    }
    state = await call(headers, "GET", "/api/state");
    const queued = state.queue.map((step) => step.id);
    check("ordinary steps scored 10 take the early look's place", ordinary.every((id) => queued.includes(id)) && state.ready_next.some((step) => step.id === early.id),
      JSON.stringify({ queued, ready: state.ready_next.map((step) => step.id) }));
    check("no page errors", errors.length === 0, errors.join("; "));
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  if (results.some((r) => !r.ok)) process.exitCode = 1;
}

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
