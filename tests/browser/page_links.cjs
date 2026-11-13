// A step card lists its item's pages under the "Other" chip beside its core links, with its GitHub and Linear links
// (SPEC API-3). A page opens for the step; a link the step's text holds stays out, unless it is a page.

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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "links-test", "content-type": "application/json" };
const GITHUB = "https://github.com/example-org/app/pull/10";

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-links-");
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

    // The fixture database's open steps are snoozed, so this run's two steps take the user's places.
    const initial = await user("GET", "/api/state");
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    const run = Date.now();
    const steps = {};
    const pages = {};
    for (const name of ["next", "queued"]) {
      const key = `note:links-${run}-${name}`;
      const item = await agent("PUT", "/api/items", { key, title: `Links test: ${name}`, priority: "urgent" });
      const page = await agent("POST", `/api/items/${item.id}/pages`, { title: `Plan page ${name}`, html: "<p>plan</p>" });
      pages[name] = page;
      // the item's links name its page too, and the step's text links both.
      await agent("PUT", "/api/items", { key, links: [{ label: `Plan (page ${page.id})`, url: page.url }, { label: "app#10", url: GITHUB }] });
      const answer = await agent("POST", "/api/steps", {
        item_key: key, need: `links-${name}-${run}`, ask: `Links test: ${name}`, score: name === "next" ? 100 : 90, session_id: SESSION_B,
        body: `Read [the plan](${page.url}) and [the PR](${GITHUB}).`,
      });
      steps[name] = answer.step.id;
      await sleep(1100);
    }

    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const [width, height] of [[390, 844], [1280, 900]]) {
      const context = await browser.newContext({ viewport: { width, height } });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.setDefaultTimeout(5000);
      page.setDefaultNavigationTimeout(60000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${info.origin}/classic`);
      const card = (id) => page.locator(`[data-step="${id}"]`).first();
      await card(steps.next).waitFor();
      for (const name of ["next", "queued"]) {
        const folded = card(steps[name]).locator('.row-title[aria-expanded="false"]');
        if (await folded.count()) await folded.click();
        // The links lie under the overlay until the step's timer runs, and in a closed list; they are in the card
        // either way.
        const links = card(steps[name]).locator(".links-other .menu-list a");
        await links.first().waitFor({ state: "attached" });
        const hrefs = await links.evaluateAll((anchors) => anchors.map((a) => a.getAttribute("href")));
        // On a step a page opens in the Pages view for that step, where comments go with the answer (NEXT-38).
        const forStep = `${info.origin}/pages?id=${pages[name].id}&step=${steps[name]}`;
        check(`${width} px, ${name}: the item's page is listed once, for the step`, hrefs.filter((href) => href === forStep).length === 1 && !hrefs.includes(pages[name].url), JSON.stringify(hrefs));
        check(`${width} px, ${name}: the GitHub link the text holds stays out`, !hrefs.includes(GITHUB), JSON.stringify(hrefs));
        const pageIcon = await card(steps[name]).locator(`.links-other .menu-list a[href="${forStep}"] svg.icon-doc`).count();
        check(`${width} px, ${name}: the page link has the page icon`, pageIcon === 1, `${pageIcon} page icons`);
      }
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
