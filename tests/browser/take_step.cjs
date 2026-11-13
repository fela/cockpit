// The user takes a ready-next step to the top of their queue (NEXT-36). Checks, at laptop and phone width:
// - every ready-next row shows "Take it", and the page has no sideways scroll;
// - its tooltip says which step gives up its place;
// - pressing it puts the step first in the queue on Cockpit and as Next up on the page, and the weakest queue step the
//   user did not work on waits in Ready next, while a weaker one the user started and paused stays, on the page before
//   the server answers too;
// - T on a highlighted ready-next row does the same, also while another step's timer runs.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
// Screenshots go to the folder given as the first argument, when one is given.
//
// Usage, from the checkout: node tests/browser/take_step.cjs [screenshot folder]

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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "take-step-test", "content-type": "application/json" };

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const shots = process.argv[2] || null;
  if (shots) fs.mkdirSync(shots, { recursive: true });
  const work = workFolder("cockpit-take-step-");
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
    const queueIds = async () => {
      const state = await user("GET", "/api/state");
      return [state.queue.map((step) => step.id), state.ready_next.map((step) => step.id)];
    };

    // The fixture database's open steps are snoozed and its timer paused, so this run's steps fill the queue.
    const initial = await user("GET", "/api/state");
    if (initial.timer) await user("POST", `/api/steps/${initial.timer.step_id}/timer/pause`);
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    browser = await chromium.launch({ executablePath: CHROMIUM });

    for (const width of [1440, 390]) {
      const key = `note:take-step-${Date.now()}`;
      await agent("PUT", "/api/items", { key, title: "Take step test" });
      const put = (need, score) => agent("POST", "/api/steps", { item_key: key, need, ask: `Take test ${need}`, score, session_id: SESSION_B })
        .then((answer) => answer.step);
      const strong = [];
      for (const n of [0, 1, 2, 3]) strong.push(await put(`strong-${n}-${width}`, 90 - n));
      const weak = await put(`weak-${width}`, 5);
      const weaker = await put(`weaker-${width}`, 4);
      // The user worked on the weakest queue step and paused it, so it keeps its place on a take (NEXT-16, NEXT-36).
      await user("POST", `/api/steps/${strong[3].id}/timer/start`);
      await user("POST", `/api/steps/${strong[3].id}/timer/pause`);

      const context = await browser.newContext({ viewport: { width, height: 900 }, isMobile: width < 600, hasTouch: width < 600 });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.setDefaultTimeout(30000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
      await page.goto(`${info.origin}/classic`);
      const group = page.locator("details.ready-next");
      // The first load of the fixture database, and every action on a busy host, can take well over 5 s.
      await group.locator("summary").click();
      const rows = group.locator(".step-row");
      await rows.first().waitFor();
      const takes = await rows.evaluateAll((list) => list.map((row) => Boolean(row.querySelector("button.take"))));
      check(`${width}: every ready-next row shows Take it`, takes.length === 2 && takes.every(Boolean), JSON.stringify(takes));
      const button = group.locator(`.step-row[data-step="${weak.id}"] button.take`);
      check(`${width}: the button reads "Take it"`, (await button.innerText()).trim() === "Take it", await button.innerText());
      // The page moves a title into its own tooltip (data-tip) once it is drawn.
      const title = await button.evaluate((el) => el.getAttribute("title") || el.dataset.tip || "");
      check(`${width}: its tooltip says a step the user worked on keeps its place`,
        title.includes("the last step you have not worked on since it took its place waits in Waiting next;")
        && title.includes("when you worked on all of them, the last one you are not on waits."), title);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
      check(`${width}: the page has no sideways scroll`, overflow <= 0, `${overflow}px too wide`);
      if (shots) await group.screenshot({ path: path.join(shots, `ready-${width}.png`) });

      // Play and Pause on the page, then Take before a fresh state arrives: the page's own timer change marks the
      // step as worked on in its place, so its Take keeps it, as the server will.
      const simulated = await page.evaluate(([played, taken]) => {
        const at = Date.now();
        const paused = timedState(timedState(ui.state, played, true, at), played, false, at + 1000);
        const step = STEP_LISTS.flatMap((name) => paused[name]).find((other) => other.id === taken);
        return applyAction(paused, step, "take", {}).ready_next.map((other) => other.id);
      }, [strong[2].id, weak.id]);
      check(`${width}: after Play and Pause on the page, its Take keeps that step too`,
        simulated.includes(strong[1].id) && !simulated.includes(strong[2].id), JSON.stringify(simulated));
      // With every queue step worked on, Play on a ready-next step, Take it, Pause, then Take another: the step
      // taken while its timer ran keeps its place, also when the first take is replayed, and the weakest other
      // paused step waits.
      const chained = await page.evaluate(([queued, played, taken]) => {
        const find = (state, id) => STEP_LISTS.flatMap((name) => state[name]).find((other) => other.id === id);
        let at = Date.now();
        let state = ui.state;
        for (const id of queued) state = timedState(timedState(state, id, true, at), id, false, (at += 1000));
        state = timedState(state, played, true, (at += 1000));
        state = applyAction(state, find(state, played), "take", {});
        state = timedState(state, played, false, (at += 1000));
        // The page replays a take still pending on each new state, also one that already has it.
        state = applyAction(state, find(state, played), "take", {});
        return applyAction(state, find(state, taken), "take", {}).ready_next.map((other) => other.id);
      }, [strong.slice(0, 3).map((step) => step.id), weaker.id, weak.id]);
      check(`${width}: a step the page took while its timer ran keeps its place on the next Take`,
        chained.includes(strong[2].id) && !chained.includes(weaker.id), JSON.stringify(chained));
      // While the server has not answered, the page already shows the same step waiting as the server will.
      let release;
      const held = new Promise((resolve) => { release = resolve; });
      await page.route("**/take", async (route) => { await held; await route.continue(); });
      await button.click();
      await page.waitForFunction((id) => !document.querySelector(`details.ready-next .step-row[data-step="${id}"]`), weak.id);
      const readyOnPage = await page.evaluate(() => [...document.querySelectorAll("details.ready-next .step-row[data-step]")]
        .map((row) => Number(row.dataset.step)));
      check(`${width}: before the server answers, the page shows that step in Ready next`,
        readyOnPage.includes(strong[2].id) && !readyOnPage.includes(strong[3].id), JSON.stringify(readyOnPage));
      release();
      await sleep(800);
      const [queue, ready] = await queueIds();
      check(`${width}: Cockpit puts the taken step first`, queue[0] === weak.id && queue.length === 4, JSON.stringify(queue));
      check(`${width}: the weakest queue step the user did not work on waits in Ready next`,
        ready.includes(strong[2].id) && queue.includes(strong[3].id), JSON.stringify([queue, ready]));
      const nextOnPage = await page.evaluate(() => Number(document.querySelector("#next [data-step]")?.dataset.step));
      check(`${width}: the page shows the taken step as Next up`, nextOnPage === weak.id, String(nextOnPage));
      if (shots) await page.screenshot({ path: path.join(shots, `taken-${width}.png`) });

      // T on a highlighted ready-next row, while the taken step's timer runs: the new take still leads.
      await user("POST", `/api/steps/${weak.id}/timer/start`);
      // The page takes the running timer from its next poll (every 5 s); a click to fetch it would pause the timer.
      await page.waitForFunction((id) => document.querySelector(`#next [data-step="${id}"]`) !== null
        && document.querySelector(`#next [data-step="${id}"].on-clock`) !== null, weak.id, { timeout: 15000 });
      let found = false;
      for (let n = 0; n < 60 && !found; n += 1) {
        await page.keyboard.press("j");
        found = await page.evaluate((id) => document.querySelector(".keyboard-current")?.dataset.step === String(id), weaker.id);
      }
      check(`${width}: j reaches the ready-next row`, found, "never highlighted");
      if (found) {
        await page.keyboard.press("t");
        await sleep(800);
        const [after] = await queueIds();
        check(`${width}: T takes the highlighted step to the top`, after[0] === weaker.id && after[1] === weak.id, JSON.stringify(after));
        const running = (await user("GET", "/api/state")).timer;
        check(`${width}: the taken step's timer still runs`, running && running.step_id === weak.id, JSON.stringify(running));
        const next = await page.evaluate(() => Number(document.querySelector("#next [data-step]")?.dataset.step));
        check(`${width}: the page shows it as Next up over the step whose timer runs`, next === weaker.id, String(next));
      }

      check(`${width}: no page errors`, errors.length === 0, errors.join(" | "));
      await user("POST", `/api/steps/${weak.id}/timer/pause`);
      const [left, waiting] = await queueIds();
      await Promise.all([...left, ...waiting].map((id) => user("POST", `/api/steps/${id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
      await context.close();
    }

    // a take over four places sends the weakest step the user did not work on to Ready next, on the page
    // as on Cockpit. The queue goes by the sweeper's score only (RANK-18), so a finish step scored 1 is that step.
    {
      const key = `note:take-finish-${Date.now()}`;
      await agent("PUT", "/api/items", { key, title: "Take finish test" });
      const put = (need, score) => agent("POST", "/api/steps", { item_key: key, need, ask: `Take test ${need}`, score, session_id: SESSION_B })
        .then((answer) => answer.step);
      const strong = [];
      for (const n of [0, 1, 2]) strong.push(await put(`strong-${n}`, 90 - n));
      const finish = await put("complete", 1);
      const weak = await put("weak", 5);
      const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.setDefaultTimeout(30000);
      await page.goto(`${info.origin}/classic`);
      const group = page.locator("details.ready-next");
      await group.locator("summary").click();
      await group.locator(`.step-row[data-step="${weak.id}"] button.take`).waitFor();
      // The take's answer waits, so the lists below are the ones the page draws before Cockpit answers.
      let release;
      const held = new Promise((resolve) => { release = resolve; });
      await page.route(`**/api/steps/${weak.id}/take`, async (route) => { await held; await route.continue(); });
      await group.locator(`.step-row[data-step="${weak.id}"] button.take`).click();
      await sleep(300);
      const shown = await group.locator(".step-row[data-step]").evaluateAll((list) => list.map((row) => Number(row.dataset.step)));
      check("the page sends the weakest step, a finish step, to Ready next on a take", shown.includes(finish.id) && !shown.includes(strong[2].id), JSON.stringify(shown));
      release();
      await sleep(800);
      const [queue, ready] = await queueIds();
      check("Cockpit sends the weakest step, a finish step, to Ready next on a take", ready.includes(finish.id) && queue.includes(strong[2].id), JSON.stringify([queue, ready]));
      await Promise.all([...queue, ...ready].map((id) => user("POST", `/api/steps/${id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
      await context.close();
    }
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

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
