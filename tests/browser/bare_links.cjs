// Bare web addresses in a step's text are links (SPEC UI-38). Puts a step with bare and Markdown links into the user's
// queue and checks its body at 1440 and 390 px:
// - every web address in the body is a link: the bare dev link, the Markdown links, and the screenshot, which is not
//   loaded and whose words open it;
// - the dev link stops before the full stop that ends its sentence;
// - pressing each link opens its address in a new tab (outside addresses get an empty stand-in page);
// - no page errors.
// Saves a screenshot of the card per width in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/bare_links.cjs

"use strict";

const path = require("node:path");
const { startPreview } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

// The harness's agent token and second fake session (tests/conftest.py).
const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "bare-links-test", "content-type": "application/json" };
const DEV_LINK = "https://app.example.test/lists?list=10&export=20";
const SCREENSHOT = "https://github.com/example-org/app/blob/aaaa111/list-sharing.png?raw=true";
const SCREENSHOT_WORDS = "A shared task list with an invite button";
// Markdown links, a screenshot, and a bare address that ends its sentence, with sentence punctuation
const BODY = [
  "**List sharing** ([app#10](https://github.com/example-org/app/pull/10)) adds an invite button;"
    + " [app#20](https://github.com/example-org/app/pull/20) adds a shared-list export.",
  `![${SCREENSHOT_WORDS}](${SCREENSHOT})`,
  `**Sample list:** ${DEV_LINK}. Open it to inspect the invite button and export link.`,
].join("\n\n");

async function main() {
  const { work, info } = await startPreview("cockpit-bare-links-");
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const headers = { cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json" };
    const call = async (method, url, auth, body) => {
      const response = await fetch(info.origin + url, { method, headers: auth, body: body === undefined ? undefined : JSON.stringify(body) });
      if (!response.ok) throw new Error(`${method} ${url}: HTTP ${response.status}: ${await response.text()}`);
      return response.json();
    };
    // The fixture database's open steps are snoozed, so this run's step takes Next up.
    const initial = await call("GET", "/api/state", headers);
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((one) => call("POST", `/api/steps/${one.id}/snooze`, headers, { until: "2099-01-01T08:00:00Z" })));
    const key = `note:bare-links-${Date.now()}`;
    await call("PUT", "/api/items", AGENT, { key, title: "Bare links test" });
    const { step } = await call("POST", "/api/steps", AGENT, { item_key: key, need: "read:links", ask: "Bare links test", body: BODY, score: 100, session_id: SESSION_B });
    const STEP = step.id;
    // Each address in the Markdown source, less the punctuation that ends its sentence; & as the page has it.
    const expected = [...new Set((step.body.match(/https?:\/\/[^\s)\]]+/g) || []).map((url) => url.replace(/[.,;:!?]+$/, "")))];

    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const width of [1440, 390]) {
      const context = await browser.newContext({ viewport: { width, height: 1000 } });
      await context.setExtraHTTPHeaders({ cookie: headers.cookie });
      // Outside addresses open an empty stand-in page, so the check needs no network.
      await context.route((url) => url.origin !== info.origin, (route) => route.fulfill({ status: 200, contentType: "text/html", body: "" }));
      const page = await context.newPage();
      page.setDefaultTimeout(5000);
      page.setDefaultNavigationTimeout(60000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${info.origin}/classic`);
      const card = page.locator(`[data-step="${STEP}"]:visible`).first();
      await card.waitFor();
      // Next up shows the body; a queue row opens on its title.
      if (!(await card.locator(".prose.body").count())) await card.locator(".row-title").first().click();
      const body = card.locator(".prose.body").first();
      await body.locator(`a[href="${DEV_LINK}"]`).waitFor();
      const links = await body.evaluate((el) => [...el.querySelectorAll("a[href^='http']")].map((a) => ({
        href: a.getAttribute("href"),
        words: a.textContent,
        after: a.nextSibling ? a.nextSibling.textContent.slice(0, 1) : "",
      })));
      const hrefs = links.map((link) => link.href);
      const missing = expected.filter((url) => !hrefs.includes(url));
      check(`${width} px: every web address in the body is a link (${expected.length})`, missing.length === 0, `not links: ${missing.join(", ")}`);
      const shotLink = links.find((link) => link.href === SCREENSHOT);
      check(`${width} px: the screenshot's words open it`, shotLink && shotLink.words === SCREENSHOT_WORDS, JSON.stringify(shotLink));
      const dev = links.find((link) => link.href === DEV_LINK);
      check(`${width} px: the dev link stops before the full stop`, dev && dev.words === DEV_LINK && dev.after === ".", JSON.stringify(dev));

      const opened = [];
      for (const [index, link] of links.entries()) {
        const anchor = body.locator("a[href^='http']").nth(index);
        const [tab] = await Promise.all([context.waitForEvent("page"), anchor.click()]);
        await tab.waitForLoadState();
        if (tab.url() === new URL(link.href).href) opened.push(link.href);
        await tab.close();
      }
      check(`${width} px: pressing each link opens its address`, opened.length === links.length,
        `opened ${opened.length} of ${links.length}: ${hrefs.filter((href) => !opened.includes(href)).join(", ")}`);

      const shot = path.join(work, `bare-links-${width}.png`);
      await page.screenshot({ path: shot });
      console.error(`  screenshot ${shot}`);
      check(`${width} px: no page errors`, errors.length === 0, errors.join(" | "));
      await context.close();
    }
  } finally {
    if (browser) await browser.close();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
