// Answers come back first on the step the user asked on, finish steps too (SPEC NEXT-37). On a copy of the live data,
// per width (1440 and 390 px), on a new item:
// - the user hands its finish step to the agent with a message, and the agent's done with a note returns it;
// - the returned card leads the queue, its ask is the reply's first sentence, and its text shows the user's message and
//   the reply before the task, with where it stands as the agent's summary now and without the stale last step;
// - an agent's return of another handed step it left as it was, without a note, is refused;
// - no page errors.
// Saves a screenshot of the returned card per width in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a copy of the live database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/finish_answer.cjs

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

// The harness's agent token (tests/conftest.py TOKEN).
const AGENT_TOKEN = "t".repeat(40);
const AGENT = "finish-answer";
// The harness's fake session that reports its work done (tests/conftest.py SESSION_A).
const SESSION = "11111111-1111-4111-8111-111111111111";

async function main() {
  const work = workFolder("cockpit-finish-answer-");
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
    const agent = { authorization: `Bearer ${AGENT_TOKEN}`, "x-cockpit-as": AGENT, "content-type": "application/json" };
    const call = async (method, url, headers, body) => {
      const response = await fetch(`${info.origin}${url}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
      const text = await response.text();
      return { status: response.status, json: response.ok ? JSON.parse(text) : null, text };
    };
    const must = async (...args) => {
      const response = await call(...args);
      if (response.json === null) throw new Error(`${args[0]} ${args[1]}: HTTP ${response.status}: ${response.text}`);
      return response.json;
    };

    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const width of [1440, 390]) {
      const key = `note:finish-answer-${width}`;
      await must("PUT", "/api/items", agent, { key, title: `Tasks export on ${width}`, stage: "review", sessions: [SESSION] });
      const columns = (await must("POST", "/api/steps", agent, { item_key: key, need: "decide:columns", ask: "Include completed tasks in the CSV export?" })).step;
      await must("POST", `/api/steps/${columns.id}/done`, agent, { note: "Completed tasks are included in the export." });
      // The agent's finish step (CONTROL-19: an agent's close needs the finish step its session wrote).
      const finish = (await must("POST", "/api/steps", agent, {
        item_key: key, need: "complete", ask: "Is this task done?", body: "The sample CSV contains completed and open tasks.", session_id: SESSION,
      })).step.id;
      await must("PUT", "/api/items", agent, { key, summary: "Sample CSV is ready" });
      await must("POST", `/api/steps/${finish}/hand-off`, user, { text: "Please keep only open tasks in the sample export" });
      await must("PUT", "/api/items", agent, { key, summary: "The sample export contains only open tasks" });
      const reply = "The sample CSV now contains only open tasks. Completed tasks remain in the list.";
      const returned = await must("POST", `/api/steps/${finish}/done`, agent, { note: reply });
      check(`${width} px: the agent's done returns the handed finish step`, returned.status === "open" && Boolean(returned.returned), JSON.stringify(returned.status));
      // The returned step takes a place by its score (SPEC RANK-18): the sweep's score for it.
      await must("POST", "/api/steps", agent, { item_key: key, need: returned.need, score: 100 });

      // Another handed step, left as it was: its return without a note is refused with what to do.
      const memo = (await must("POST", "/api/steps", agent, { item_key: key, need: "decide:memo", ask: "Send version B?" })).step;
      await must("POST", `/api/steps/${memo.id}/hand-off`, user, { text: "Which version preserves reminder dates?" });
      const refused = await call("POST", `/api/steps/${memo.id}/return`, agent, {});
      check(`${width} px: a handed step left as it was comes back only with an answer`,
        refused.status === 400 && refused.text.includes("Which version preserves reminder dates?"), `${refused.status} ${refused.text}`);

      const context = await browser.newContext({ viewport: { width, height: 1000 } });
      await context.setExtraHTTPHeaders({ cookie: user.cookie });
      const page = await context.newPage();
      page.setDefaultTimeout(5000);
      page.setDefaultNavigationTimeout(60000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${info.origin}/classic`);

      const card = page.locator(`[data-step="${finish}"]:visible`).first();
      await card.waitFor({ timeout: 20000 });
      const queue = await page.locator("[data-step]:visible").evaluateAll((cards) => cards.map((el) => Number(el.dataset.step)));
      check(`${width} px: the returned finish step, scored 100, has a place in the queue`, queue.slice(0, 4).includes(finish), JSON.stringify(queue.slice(0, 4)));
      // A step the user took may lead the queue, which shows the returned one as a row: open it.
      if (await card.locator(".row-head .row-title").count()) {
        if (!(await card.evaluate((el) => el.classList.contains("open")))) await card.locator(".row-head .row-title").click();
      }
      await sleep(500);
      const ask = await card.locator(".ask > .ask-text, .row-head .row-title").innerText();
      check(`${width} px: its ask is the reply's first sentence`, ask === reply.split(". ")[0], ask);
      const text = await card.innerText();
      const body = text.slice(text.indexOf("You wrote on"));
      const wrote = body.indexOf("Please keep only open tasks in the sample export");
      const stands = body.indexOf("Where it stands: The sample export contains only open tasks");
      check(`${width} px: the user's message comes before where it stands`,
        text.includes("You wrote on") && wrote >= 0 && stands > wrote, JSON.stringify({ wrote, stands, text }));
      // The reply fits in the ask and why, so the body does not say it again, nor the task's title.
      check(`${width} px: the body leaves out the reply, the title and the stale last step`,
        !body.includes(`${AGENT}'s reply:`) && !body.includes("The task:") && !body.includes(columns.ask), body);
      const shot = path.join(work, `finish-answer-${width}.png`);
      await card.screenshot({ path: shot }).catch(() => card.screenshot({ path: shot }));
      console.error(`  screenshot ${shot}`);
      check(`${width} px: no page errors`, errors.length === 0, errors.join(" | "));
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
