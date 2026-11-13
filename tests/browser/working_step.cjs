// A step the user handed to an agent stays on the card while the agent works on it: the step as it was, then what the
// agent is doing. On a synthetic database, with the sidebar layout, at 1440 px and at phone width, light and dark:
// - picking the item in Busy draws the handed step's card, with its title, ask, body, details and core links;
// - the card shows none of the step's answers, keys or menu, as the agent holds the step;
// - the words the user sent with the step head the card, once: as their latest message, not again in the panel;
// - it ends in the working panel: the agent's name, what it is doing and its latest status;
// - the card starts no timer;
// - Take back on the panel brings the step back to the user's queue;
// - no page errors.
// Saves screenshots of the card in the work dir.
//
// Starts serve_preview.py (README.md says what it needs) and drives a headless Chromium. Prints one line per check and
// exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/working_step.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// The harness's agent token (tests/conftest.py TOKEN) and its second fake session, which the step goes to.
const AGENT_TOKEN = "t".repeat(40);
const SESSION = "22222222-2222-4222-8222-222222222222";

const SENT = [
  "Looks good overall. A few changes before you go on:",
  "1. The trips page should open on the upcoming trips, not the past ones.",
  "2. Drop the second map; keep the list.",
  "3. Make the photos larger on the phone.",
  "4. Keep the old plans for a week, then remove them.",
  "5. Send me the five new plans on the page when they are ready.",
].join("\n");

async function main() {
  const work = workFolder("cockpit-working-step-");
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
    const agent = { authorization: `Bearer ${AGENT_TOKEN}`, "x-cockpit-as": "working-step-test", "content-type": "application/json" };
    const call = async (method, url, headers, body) => {
      const response = await fetch(`${info.origin}${url}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
      const text = await response.text();
      if (!response.ok) throw new Error(`${method} ${url}: HTTP ${response.status}: ${text}`);
      return JSON.parse(text);
    };
    // The fixture database's open steps are snoozed, so nothing else takes the card.
    const initial = await call("GET", "/api/state", user);
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((step) => call("POST", `/api/steps/${step.id}/snooze`, user, { until: "2099-01-01T08:00:00Z" })));
    const key = `note:working-step-${Date.now()}`;
    const title = "Trip planner";
    const itemId = (await call("PUT", "/api/items", agent, {
      key, title, stage: "building", priority: "normal", sessions: [SESSION],
      doing: "Writing the five new plans",
    })).id;
    const step = (await call("POST", "/api/steps", agent, {
      item_key: key, need: "review:trips", session_id: SESSION, score: 80, size: "medium",
      ask: "Approve the new trips flow?",
      body: "The trips page now opens on **upcoming trips**. Each trip has its plan beside it.",
      details: [{ title: "What changed", body: "The map picker moved into the trip's menu." }],
      core_links: [{ label: "Trips page", url: "https://example.com/trips" }],
      answers: [{ text: "Approve", closes: false }],
    })).step;
    await call("PUT", "/api/items", agent, { key, summary: "Five plans are being written; the trips page waits on them" });
    await call("POST", `/api/steps/${step.id}/hand-off`, user, { text: SENT, session_id: SESSION });

    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const [width, height, scheme] of [[1440, 1000, "light"], [1440, 1000, "dark"], [390, 844, "light"]]) {
      const label = `${width} ${scheme}`;
      const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 2, colorScheme: scheme });
      await context.setExtraHTTPHeaders({ cookie: user.cookie });
      const page = await context.newPage();
      page.setDefaultTimeout(8000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      const timers = [];
      page.on("request", (request) => { if (/\/api\/steps\/\d+\/timer/.test(request.url())) timers.push(request.url()); });
      await page.goto(`${info.origin}/#item/${itemId}`, { timeout: 60000 });
      await page.waitForFunction((id) => ui.state && ui.state.with_agents.some((one) => one.id === id), step.id, { timeout: 60000 });
      await page.evaluate((id) => selectItem(id), itemId);
      const card = page.locator(`#next article.working-card[data-step="${step.id}"]`);
      const shown = await card.waitFor().then(() => true, () => false);
      check(`${label}: the handed step's card shows`, shown, await page.locator("#next").innerHTML().catch(() => ""));
      if (!shown) { await context.close(); continue; }
      const text = await card.innerText();
      check(`${label}: it heads with the task's title and the ask`,
        (await card.locator("h1.task-heading").innerText()).includes(title) && text.includes("Approve the new trips flow"), text.slice(0, 300));
      check(`${label}: it keeps the body, details and core links`,
        text.includes("upcoming trips") && text.includes("What changed") && (await card.locator("a", { hasText: "Trips page" }).count()) > 0, text);
      check(`${label}: it shows no answers, keys or menu`,
        (await card.locator(".actions, .step-menu").count()) === 0, await card.locator(".actions, .step-menu").count());
      const note = card.locator(".working-note");
      const noteText = await note.innerText();
      check(`${label}: the panel names the agent and what it is doing`,
        /Working…|Agent idle|With the agent/.test(noteText) && noteText.includes("Writing the five new plans"), noteText);
      check(`${label}: the panel shows the agent's latest status`, noteText.includes("Five plans are being written"), noteText);
      check(`${label}: the user's words head the card once, not again in the panel`,
        (await card.locator(".fold-said").innerText()).includes("Looks good overall") && !noteText.includes("Looks good overall"), noteText);
      check(`${label}: the panel comes after the step's text and before the reply box`,
        await card.evaluate((el) => {
          const kids = [...el.children];
          const at = (selector) => kids.findIndex((kid) => kid.matches(selector));
          return at(".details") < at(".working-note") && at(".working-note") < at(".composer");
        }), "out of order");
      await card.screenshot({ path: path.join(work, `card-${width}-${scheme}.png`) });
      await page.screenshot({ path: path.join(work, `page-${width}-${scheme}.png`) });
      await sleep(1500);
      check(`${label}: the card starts no timer`, timers.length === 0, timers.join(", "));
      check(`${label}: no page errors`, errors.length === 0, errors.join(" | "));
      await context.close();
    }

    // Take back brings the step to the user's queue.
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.setExtraHTTPHeaders({ cookie: user.cookie });
    const page = await context.newPage();
    await page.goto(`${info.origin}/`, { timeout: 60000 });
    await page.waitForFunction((id) => ui.state && ui.state.with_agents.some((one) => one.id === id), step.id, { timeout: 60000 });
    await page.evaluate((id) => selectItem(id), itemId);
    await page.locator(`#next .working-note .working-back`).click();
    let state = null;
    for (let tries = 0; tries < 40; tries += 1) {
      state = await call("GET", "/api/state", user);
      if (state.queue.some((one) => one.id === step.id)) break;
      await sleep(250);
    }
    check("Take back brings the step back to the queue", state.queue.some((one) => one.id === step.id), "not back");
    const back = await page.locator(`#next article.next-card[data-step="${step.id}"]:not(.working-card) .actions`).waitFor().then(() => true, () => false);
    check("the card shows the step's answers again", back, "no answers");
    await context.close();
    console.error(`  screenshots in ${work}`);
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
  const end = Date.now() + 120e3;
  while (Date.now() < end) {
    if (server.exitCode !== null) throw new Error(`serve_preview.py exited with ${server.exitCode}`);
    if (fs.existsSync(file)) {
      const info = JSON.parse(fs.readFileSync(file, "utf8"));
      const up = await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false);
      if (up) return info;
    }
    await sleep(200);
  }
  throw new Error("serve_preview.py did not come up within 120 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
