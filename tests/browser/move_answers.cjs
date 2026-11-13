// A move step Cockpit recommends offers "Yes, move it" and "No, let it wait" as one-press answers. On a synthetic
// database, on /classic at 1440 and 390 px:
// - the card shows both answers in its answer row, and no separate Move button;
// - "Yes, move it" asks Cockpit to move the session to the step's account (the request is caught, so no session
//   moves) and hands nothing to a session;
// - "No, let it wait" dismisses the step;
// - no page errors.
// Saves a screenshot of the card per width in the work folder.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and drives
// a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/move_answers.cjs

"use strict";

const { spawn, execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const AGENT_TOKEN = "t".repeat(40);
const AGENT = "check-move";
const SESSION = "11111111-1111-4111-8111-111111111111";
const PYTHON = path.join(__dirname, "..", "..", ".venv", "bin", "python");

async function main() {
  const work = workFolder("cockpit-move-answers-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
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
      const key = `note:check-move-${width}`;
      await must("PUT", "/api/items", agent, { key, title: `Move check ${width}`, stage: "building", sessions: [SESSION] });
      const step = (await must("POST", "/api/steps", agent, {
        item_key: key, need: `move:${SESSION}:work:20260115T100000`, score: 100,
        ask: "Move this item's session to the work Claude account, so it continues now?",
      })).step;
      // The answers as moves.sync puts them: agents may not give an answer an action, so set them in the fixture database.
      execFileSync(PYTHON, ["-c", [
        "import json, sqlite3, sys",
        "sys.path.insert(0, 'src')",
        "from cockpit import moves",
        "conn = sqlite3.connect(sys.argv[1])",
        "conn.execute('UPDATE steps SET answers = ? WHERE id = ?', (json.dumps(moves.ANSWERS), int(sys.argv[2])))",
        "conn.commit()",
      ].join("\n"), path.join(work, "cockpit.db"), String(step.id)], { cwd: path.join(__dirname, "..", "..") });

      const context = await browser.newContext({ viewport: { width, height: 1000 } });
      await context.setExtraHTTPHeaders({ cookie: user.cookie });
      const page = await context.newPage();
      page.setDefaultTimeout(5000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      const moves = [];
      await page.route("**/api/sessions/*/move", async (route) => {
        moves.push({ url: route.request().url(), body: route.request().postDataJSON() });
        await route.fulfill({ json: { session_id: SESSION, moved: true, waits: false, message_id: null, detail: "moved (test)" } });
      });
      const handOffs = [];
      page.on("request", (request) => { if (request.url().includes("/hand-off")) handOffs.push(request.url()); });
      await page.goto(`${info.origin}/classic`, { timeout: 60000 });

      const card = page.locator(`[data-step="${step.id}"]:visible`).first();
      await card.waitFor({ timeout: 20000 });
      if (await card.locator(".row-head .row-title").count()) {
        if (!(await card.evaluate((el) => el.classList.contains("open")))) await card.locator(".row-head .row-title").click();
      }
      await sleep(500);
      const answers = await card.locator(".answers .answer:not(.task-done)").allInnerTexts();
      check(`${width} px: the card offers "Yes, move it" and "No, let it wait"`,
        JSON.stringify(answers.map((text) => text.trim())) === JSON.stringify(["Yes, move it", "No, let it wait"]), JSON.stringify(answers));
      check(`${width} px: no separate Move button`, (await card.locator('[aria-label="Move"]').count()) === 0, "a Move block is still there");
      const shot = path.join(work, `move-answers-${width}.png`);
      await card.screenshot({ path: shot }).catch(() => card.screenshot({ path: shot }));
      console.error(`  screenshot ${shot}`);

      await card.locator(".answers .answer", { hasText: "Yes, move it" }).evaluate((el) => el.click());
      await sleep(800);
      check(`${width} px: "Yes, move it" asks Cockpit to move the session to the work account`,
        moves.length === 1 && moves[0].url.includes(`/api/sessions/${SESSION}/move`) && moves[0].body.account === "work", JSON.stringify(moves));
      check(`${width} px: "Yes, move it" hands nothing to a session`, handOffs.length === 0, JSON.stringify(handOffs));

      // The caught move left the step open; load the page again so the card shows as at first.
      await page.goto(`${info.origin}/classic`, { timeout: 60000 });
      await card.waitFor({ timeout: 20000 });
      await sleep(500);
      if (!(await card.locator(".answers .answer").count())) {
        await page.screenshot({ path: path.join(work, `after-yes-${width}.png`) });
        await card.locator(".row-head .row-title").click();
        await sleep(500);
      }
      await card.locator(".answers .answer", { hasText: "No, let it wait" }).evaluate((el) => el.click());
      let status = null;
      for (let i = 0; i < 20 && status !== "dismissed"; i += 1) {
        await sleep(300);
        const item = await must("GET", `/api/items?key=${encodeURIComponent(key)}`, agent);
        status = item.steps.find((each) => each.id === step.id).status;
      }
      check(`${width} px: "No, let it wait" dismisses the step`, status === "dismissed", String(status));
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
