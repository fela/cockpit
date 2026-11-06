// After a Send or a close on the card, the next step shows from its top (SPEC UI-64). On a synthetic data, on
// the page with the list at its left (1440 x 560 px) and on a phone (390 x 700 px):
// - the user scrolls down a long step to its reply box, writes and presses Send; the page is at the next step's top
//   at once (within one frame of the step changing) and stays there;
// - the same for Task done on a finish step (the close animation), on the wide page;
// - no page errors.
// Records the page's scroll position every frame from the press; a failed check prints where the page went.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/scroll_next.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const AGENT_TOKEN = "t".repeat(40);
const SESSION = "11111111-1111-4111-8111-111111111111";
const LONG = Array.from({ length: 22 }, (_, i) => `Paragraph ${i + 1}: the step runs long so the reply box is far below its top.`).join("\n\n");

async function main() {
  const work = workFolder("cockpit-scroll-next-");
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
    const user = { cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json" };
    const as = (who) => ({ authorization: `Bearer ${AGENT_TOKEN}`, "x-cockpit-as": who, "content-type": "application/json" });
    const call = async (method, url, headers, body) => {
      const response = await fetch(`${info.origin}${url}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
      const text = await response.text();
      if (!response.ok) throw new Error(`${method} ${url}: HTTP ${response.status}: ${text}`);
      return JSON.parse(text);
    };
    // Each round: a long step to answer, and a long step that leads the queue to show next.
    const round = async (name, need) => {
      const key = `note:check-scroll-${name}`;
      const item = await call("PUT", "/api/items", as("check-scroll"), { key, title: `Check scroll ${name}`, stage: "building", sessions: [SESSION] });
      const step = (await call("POST", "/api/steps", as("check-scroll"), { item_key: key, need, ask: `Scroll check ${name}: answer this long step?`, body: LONG })).step;
      const nextKey = `note:check-scroll-${name}-next`;
      await call("PUT", "/api/items", as("check-scroll"), { key: nextKey, title: `Next after ${name}`, stage: "building", sessions: [SESSION] });
      const next = (await call("POST", "/api/steps", as("check-scroll"), { item_key: nextKey, need: "decide:next", ask: `Next after ${name}: the step shown next?`, body: LONG })).step;
      await call("POST", "/api/steps", as("sweep"), { item_key: key, need, score: 100, reasons: ["scroll check"] });
      await call("POST", "/api/steps", as("sweep"), { item_key: nextKey, need: "decide:next", score: 99, reasons: ["scroll check"] });
      return { item, step, next };
    };

    // The synthetic data may hold the user's running timer, or its "Back after …" question; the page shows
    // that timer's step instead of the queue's first. Return answers the question, and the timer stops.
    const copied = await call("GET", "/api/state", user);
    if (copied.away) await call("POST", `/api/steps/${copied.away.step_id}/timer/answer`, user, { question: copied.away.id, add: false });
    const timer = (await call("GET", "/api/state", user)).timer;
    if (timer) await call("POST", `/api/steps/${timer.step_id}/timer/pause`, user);

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const errors = [];

    const trial = async (width, height, how) => {
      const variant = `${width}px`;
      const r = await round(`${variant}-${how}`, how === "send" ? "decide:scroll" : "complete");
      const context = await browser.newContext({ viewport: { width, height } });
      await context.setExtraHTTPHeaders({ cookie: user.cookie });
      const page = await context.newPage();
      page.setDefaultTimeout(10000);
      page.on("pageerror", (error) => errors.push(`${variant}/${how}: ${error.message}`));
      await page.goto(`${info.origin}/`);
      await page.locator("#next").waitFor({ timeout: 20000 });
      const card = page.locator(`#next [data-step="${r.step.id}"]`);
      await card.waitFor();
      await sleep(400);
      await page.evaluate(() => window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "instant" }));
      await sleep(300);
      const before = await page.evaluate(() => window.scrollY);
      const bottom = await page.evaluate(() => document.documentElement.scrollHeight - window.innerHeight);
      let button;
      if (how === "send") {
        await card.locator(`textarea[data-key="msg:step:${r.step.id}"]`).fill("Scroll check reply");
        button = card.locator(".composer:not(.feedback-box) button.send").first();
      } else {
        button = card.locator(".actions button.finish, .actions button", { hasText: "Task done" }).first();
      }
      // A trace of the scroll position and the next card's top on screen, every frame for 2.5 s from the press.
      await page.evaluate(() => {
        window.__trace = [];
        const start = performance.now();
        // The bottom of the phone bar while it stays over the page's top; 0 on the wide page, which has none.
        const barBottom = () => {
          const bar = document.querySelector(".phone-bar");
          return bar && getComputedStyle(bar).position === "sticky" ? Math.max(0, bar.getBoundingClientRect().bottom) : 0;
        };
        const tick = (now) => {
          const card = [...document.querySelectorAll("#next > [data-step]")].find((el) => !el.classList.contains("motion-ghost"));
          window.__trace.push({ t: Math.round(now - start), y: Math.round(window.scrollY), step: card ? Number(card.dataset.step) : null,
            top: card ? Math.round(card.getBoundingClientRect().top) : null, bar: Math.round(barBottom()) });
          if (now - start < 2500) requestAnimationFrame(tick);
        };
        requestAnimationFrame(tick);
      });
      await button.evaluate((el) => el.click());
      await sleep(2700);
      await page.screenshot({ path: path.join(work, `after-${variant}-${how}.png`) });
      const trace = await page.evaluate(() => window.__trace);
      // The frames where the scroll position or the step changed, as "ms:scrollY:step:card top".
      const moves = trace.filter((frame, i) => i === 0 || frame.y !== trace[i - 1].y || frame.step !== trace[i - 1].step).map((frame) => `${frame.t}:${frame.y}:${frame.step}:${frame.top}`).join(" ");
      const swapped = trace.find((frame) => frame.step !== r.step.id && frame.step !== null);
      // The next step's card, with its top in view: on a phone below the phone bar that stays over the page's top.
      const shownTop = (frame) => Boolean(frame) && frame.step === r.next.id && frame.top >= frame.bar - 8 && frame.top < frame.bar + 60;
      const label = `${variant}, ${how}`;
      // Down at the reply box, well past the next step's top, however tall the sidebar page draws the step.
      check(`${label}: the page was scrolled down before the press`, before > 300 && before >= bottom - 2, `scrollY ${before} of ${bottom}`);
      check(`${label}: the next step came`, Boolean(swapped), JSON.stringify(trace.slice(0, 5)));
      if (swapped) {
        const first = trace.find((frame) => frame.t >= swapped.t + 1) || swapped;
        check(`${label}: the next step's top shows at once`, shownTop(swapped) || shownTop(first), `moves ${moves}`);
        check(`${label}: the next step's top still shows at the end`, shownTop(trace.at(-1)), `moves ${moves}`);
      }
      await context.close();
      // The next step leaves too, so it does not hold the top of the queue in the next round.
      await call("POST", `/api/steps/${r.next.id}/dismiss`, user, {});
    };

    await trial(1440, 560, "send");
    await trial(1440, 560, "finish");
    await trial(390, 700, "send");
    check("no page errors", errors.length === 0, errors.join(" | "));
  } finally {
    if (browser) await browser.close();
    server.kill("SIGTERM");
  }
  const failed = results.filter((result) => !result.ok);
  console.error(failed.length ? `${failed.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failed.length ? 1 : 0;
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
