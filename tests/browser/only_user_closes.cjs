// Only the user closes a task (SPEC CONTROL-17). On a copy of the live data, per width (1440 and 390 px), on a new
// item:
// - an agent's put of stage done is refused while the item has no finish step open (CONTROL-19); with the finish step
//   its session wrote, the put leaves the item open and points at that step;
// - the user hands another step of the item to the agent, whose done closes it: the closed steps folded in Finished
//   this week (VIEW-20) show that close as handled, in grey, not with the user's plum check;
// - an agent's done on the handed finish step returns it to the user's queue instead of closing it;
// - the user's Done on the finish step opens the finish panel, also while another step of the item is open, and Task
//   done closes the task (stage done);
// - no page errors.
// Saves a screenshot of the closed steps per width in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a copy of the live database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/only_user_closes.cjs

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
  const work = workFolder("cockpit-only-user-closes-");
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
      if (!response.ok) throw new Error(`${method} ${url}: HTTP ${response.status}: ${text}`);
      return JSON.parse(text);
    };

    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const width of [1440, 390]) {
      const key = `note:finish-answer-${width}`;
      const item = await call("PUT", "/api/items", agent, { key, title: `Check closes ${width}`, stage: "review", sessions: [SESSION] });
      const bare = await call("PUT", "/api/items", agent, { key, stage: "done", note: "merged and released" }).then(() => "taken", (error) => error.message);
      check(`${width} px: an agent's stage done without a finish step is refused`, bare.includes("HTTP 400") && bare.includes("finish step"), bare);
      const own = (await call("POST", "/api/steps", agent, { item_key: key, need: "complete", ask: "Merged and released: close it?", session_id: SESSION })).step;
      const asked = await call("PUT", "/api/items", agent, { key, stage: "done", note: "merged and released" });
      check(`${width} px: an agent's stage done leaves the item open`, asked.stage === "review" && !asked.archived, JSON.stringify(asked.stage));
      const finish = (await call("GET", `/api/items?key=${encodeURIComponent(key)}`, agent)).steps.find((step) => step.id === asked.finish_step);
      check(`${width} px: and asks on the finish step its session wrote`, finish && finish.id === own.id && finish.status === "open", JSON.stringify(finish));

      const memo = (await call("POST", "/api/steps", agent, { item_key: key, need: "decide:memo", ask: "Write the memo?" })).step;
      await call("POST", `/api/steps/${memo.id}/hand-off`, user, { text: "I will write it myself tomorrow" });
      await call("POST", `/api/steps/${memo.id}/done`, agent, { note: "the user writes it" });
      await call("POST", `/api/steps/${finish.id}/hand-off`, user, { text: "Looks good" });
      const returned = await call("POST", `/api/steps/${finish.id}/done`, agent, { note: "the user confirmed it" });
      check(`${width} px: an agent's done on the handed finish step returns it`, returned.status === "open" && Boolean(returned.returned), JSON.stringify(returned.status));
      // The returned step takes a place by its score (SPEC RANK-18): the sweep's score for it.
      await call("POST", "/api/steps", agent, { item_key: key, need: "complete", score: 100 });
      // Another open step on the item: the finish step still has Task done, which closes both.
      await call("POST", "/api/steps", agent, { item_key: key, need: "review", ask: "Review the follow-up?" });

      const context = await browser.newContext({ viewport: { width, height: 1000 } });
      await context.setExtraHTTPHeaders({ cookie: user.cookie });
      const page = await context.newPage();
      page.setDefaultTimeout(5000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${info.origin}/classic`);

      const finishedGroup = page.locator("#work details.group.done").first();
      await finishedGroup.waitFor({ timeout: 20000 });
      if (!(await finishedGroup.evaluate((el) => el.open))) await finishedGroup.locator(":scope > summary").click();
      const closed = finishedGroup.locator("details.closed-steps").first();
      if (!(await closed.evaluate((el) => el.open))) await closed.locator(":scope > summary").click();
      const row = closed.locator("li").filter({ hasText: "Write the memo?" }).first();
      await row.waitFor();
      const look = await row.locator(".closed-icon").evaluate((el) => ({
        cls: el.className, label: el.getAttribute("aria-label"), title: el.dataset.tip || el.getAttribute("title") || "", color: getComputedStyle(el).color,
      }));
      const plum = await page.evaluate(() => {
        const probe = document.createElement("span");
        probe.className = "closed-icon done";
        document.body.append(probe);
        const color = getComputedStyle(probe).color;
        probe.remove();
        return color;
      });
      check(`${width} px: the agent's close shows as handled, not the user's plum done`,
        look.cls.includes("handled") && look.label === "handled" && look.title.startsWith("Handled by") && look.color !== plum, JSON.stringify(look));
      const shot = path.join(work, `only-user-closes-${width}.png`);
      // A redraw can replace the group mid-shot; the screenshot is for the eye, so a second try is enough.
      await closed.screenshot({ path: shot }).catch(() => closed.screenshot({ path: shot }));
      console.error(`  screenshot ${shot}`);

      // The returned finish step, scored 100, has a place in the queue; the user's one press on Task done closes the
      // task.
      const card = page.locator(`[data-step="${finish.id}"]:visible`).first();
      await card.waitFor();
      // The close animation draws in software here, so the press can take a few seconds to return.
      await card.locator(".button.task-done").first().click({ timeout: 20000 });
      let after = null;
      for (let tries = 0; tries < 25; tries += 1) {
        after = await call("GET", `/api/items?key=${encodeURIComponent(key)}`, agent);
        if (after.stage === "done") break;
        await sleep(200);
      }
      check(`${width} px: the user's Task done closes the task`, after.stage === "done" && after.id === item.id, JSON.stringify(after.stage));
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
