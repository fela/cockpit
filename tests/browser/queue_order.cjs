// Queue order (SPEC RANK-18): the user's takes first, then the sweeper's score, with no rules of the code's own.
// On a synthetic database, puts a step at score 5 on a new high-priority item and a finish step on a new
// normal one, then checks that the page's queue lists the steps the user took, then the rest by score, and that
// the high-priority step gets no place of its own. Then puts the sweep's score of 100 on it and checks that it
// leads the steps the user did not take.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs)
// and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/queue_order.cjs
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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "pressing-first", "content-type": "application/json" };

async function main() {
  const work = workFolder("cockpit-queue-order-");
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
    const stamp = Date.now();
    const highKey = `note:queue-order-high-${stamp}`;
    await call(AGENT, "PUT", "/api/items", { key: highKey, title: "Queue order: high", stage: "building", sessions: [SESSION_B] });
    const high = await call(headers, "GET", `/api/state`).then((state) => state.items.find((item) => item.key === highKey));
    await call(headers, "PATCH", `/api/items/${high.id}`, { priority: "high" });
    const highStep = (await call(AGENT, "POST", "/api/steps", { item_key: highKey, need: "decide", ask: "Queue order: a high-priority ask", session_id: SESSION_B, score: 5 })).step;
    const doneKey = `note:queue-order-done-${stamp}`;
    await call(AGENT, "PUT", "/api/items", { key: doneKey, title: "Queue order: done", stage: "building", sessions: [SESSION_B] });
    // The agent's finish step, which an agent's close needs first (CONTROL-19).
    await call(AGENT, "POST", "/api/steps", { item_key: doneKey, need: "complete", ask: "Shipped: close it?", session_id: SESSION_B });

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, colorScheme: "dark" });
    await context.setExtraHTTPHeaders({ cookie: headers.cookie });
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const shown = async () => {
      await page.goto(`${info.origin}/classic`);
      await page.locator("#queue [data-step]").first().waitFor();
      return page.evaluate(() => {
        const ids = [...document.querySelectorAll("#next [data-step], #queue [data-step]")]
          .filter((el) => !el.parentElement.closest("[data-step]"))
          .map((el) => Number(el.dataset.step));
        const byId = new Map(ui.state.queue.map((step) => [step.id, step]));
        return [...new Set(ids)].filter((id) => byId.has(id)).map((id) => {
          const step = byId.get(id);
          const taken = Boolean(step.taken_at) && step.taken_at === step.queued_at;
          // A place `_stronger_first` cannot move: the user's take, a timer run in it, a snooze.
          return { id, taken, held: taken || step.started_in_place || Boolean(step.snoozed_until), score: step.score };
        });
      });
    };
    const waiting = () => page.evaluate(() => ui.state.ready_next.map((step) => ({ id: step.id, score: step.score })));
    const inOrder = (seen) => seen.every((s, i) => i === 0 || (seen[i - 1].taken && !s.taken) || (seen[i - 1].taken === s.taken && (s.taken || seen[i - 1].score >= s.score)));

    let seen = await shown();
    console.error(`  queue as shown: ${seen.map((s) => `${s.id}${s.taken ? "T" : ""}:${s.score}`).join(" ")}`);
    check("own takes first, then the rest by score", inOrder(seen), JSON.stringify(seen));
    // A free place goes to the highest score waiting, so the score-5 step holds one only when nothing waiting
    // scores above it.
    const higher = (await waiting()).filter((s) => s.id !== highStep.id && s.score > 5);
    check("the high-priority step at score 5 gets no place of its own", !seen.some((s) => s.id === highStep.id) || higher.length === 0,
      JSON.stringify({ queue: seen, higher }));
    // Scored 100, it takes the place of a movable step scored under 90, and leads the steps the user did not take.
    const movable = seen.some((s) => s.id !== highStep.id && !s.held && s.score < 90);

    await call(AGENT, "POST", "/api/steps", { item_key: highKey, need: "decide", score: 100 });  // the sweep's score
    seen = await shown();
    console.error(`  after the sweep's score: ${seen.map((s) => `${s.id}${s.taken ? "T" : ""}:${s.score}`).join(" ")}`);
    check("the live queue had a movable place under 90 to test with", movable, JSON.stringify(seen));
    check("scored 100, it takes a place", seen.some((s) => s.id === highStep.id), JSON.stringify(seen));
    check("only steps the user took or scored 100 stand before it",
      seen.slice(0, seen.findIndex((s) => s.id === highStep.id)).every((s) => s.taken || s.score >= 100), JSON.stringify(seen));
    check("the order still holds", inOrder(seen), JSON.stringify(seen));
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
