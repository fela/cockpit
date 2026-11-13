// Core links (SPEC NEXT-58): the links a step is about show as chips under its text, above its answers, and the task's
// other links sit behind a quiet "Other N" chip beside them. Puts three tasks in the user's queue:
// - "main": its step names two core links (a GitHub PR and the task's shared page) and links the page in a sentence;
//   the task links the same PR and a Linear issue, an earlier step (closed) links a design doc and a Slack thread, and
//   the user's message to the task's session links a notes page;
// - "other only": a step without core links, on a task with one link;
// - "bare": a step and task with no links.
// Checks at 1440 and 390 px, light and dark:
// - the chips come after the step's text and above its answers, with the core links' addresses (the shared page opening
//   for the step);
// - "Other 4" lists the Linear issue under "On this task" and the doc, the thread and the notes under "Mentioned
//   earlier", and no core link;
// - the step's ⋯ menu holds no link;
// - the step without core links shows only Other, and the step without links shows no row;
// - the shared page's link in a sentence of the text has the page icon;
// - Tab from the last chip reaches Other, Enter opens it, a redraw keeps it open and Escape closes it;
// - the open list stays inside the window;
// - the chips' colours change with the scheme;
// - no page errors.
// Saves a screenshot of the main card, with Other open, per width and scheme in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and drives a
// headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/core_links.cjs

"use strict";

const path = require("node:path");
const { startPreview } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

// The harness's agent token and second fake session (tests/conftest.py).
const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "core-links-test", "content-type": "application/json" };
const GITHUB = "https://github.com/example-org/app/pull/10";
const LINEAR = "https://linear.app/example/issue/ENG-1";
const DESIGN = "https://docs.example.test/design";
const SLACK = "https://example.slack.com/archives/C1/p1";
const NOTES = "https://notes.example.test/n1";
const SPEC_DOC = "https://docs.example.test/spec";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const { work, info } = await startPreview("cockpit-core-links-");
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
    // The fixture database's open steps are snoozed, so this run's steps take the user's places.
    const initial = await call("GET", "/api/state", headers);
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((one) => call("POST", `/api/steps/${one.id}/snooze`, headers, { until: "2099-01-01T08:00:00Z" })));
    const run = Date.now();
    const step = (fields) => call("POST", "/api/steps", AGENT, { session_id: SESSION_B, ...fields }).then((answer) => answer.step);

    const mainKey = `note:core-links-${run}-main`;
    const item = await call("PUT", "/api/items", AGENT, { key: mainKey, title: "Core links test: main", priority: "urgent" });
    const shared = await call("POST", `/api/items/${item.id}/pages`, AGENT, { title: "Plan page", html: "<p>plan</p>" });
    await call("PUT", "/api/items", AGENT, { key: mainKey, links: [{ label: "GitHub: app#10", url: GITHUB }, { label: "Linear: ENG-1", url: LINEAR }] });
    const earlier = await step({
      item_key: mainKey, need: `earlier-${run}`, ask: "Core links test: earlier", score: 10,
      body: `Read [the design doc](${DESIGN}).`, details: [{ title: "Where it was said", body: `[Slack: thread](${SLACK})` }],
    });
    await call("POST", `/api/steps/${earlier.id}/done`, headers, {});
    await call("POST", "/api/messages", headers, { text: `See ${NOTES} please`, target: { item_id: item.id, session_id: SESSION_B } });
    const main = await step({
      item_key: mainKey, need: `main-${run}`, ask: "Core links test: main", score: 100, answers: ["Merge it"],
      body: `Merge it once [the plan](${shared.url}) reads well.`, details: [{ title: "Checks", body: "All green." }],
      core_links: [{ label: "app#10", url: GITHUB }, { label: "Plan page", url: shared.url }],
    });
    await sleep(1100);
    const otherKey = `note:core-links-${run}-other`;
    await call("PUT", "/api/items", AGENT, { key: otherKey, title: "Core links test: other only", priority: "urgent", links: [{ label: "Docs: spec", url: SPEC_DOC }] });
    const otherOnly = await step({ item_key: otherKey, need: `other-${run}`, ask: "Core links test: other only", score: 90, body: "Read the spec." });
    await sleep(1100);
    const bareKey = `note:core-links-${run}-bare`;
    await call("PUT", "/api/items", AGENT, { key: bareKey, title: "Core links test: bare", priority: "urgent" });
    const bare = await step({ item_key: bareKey, need: `bare-${run}`, ask: "Core links test: bare", score: 80, body: "Nothing linked." });

    const forStep = `${info.origin}/pages?id=${shared.id}&step=${main.id}`;
    const colours = {};
    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const scheme of ["light", "dark"]) {
      for (const width of [1440, 390]) {
        const at = `${width} px ${scheme}`;
        const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme });
        await context.setExtraHTTPHeaders({ cookie: headers.cookie });
        const page = await context.newPage();
        page.setDefaultTimeout(5000);
        page.setDefaultNavigationTimeout(60000);
        const errors = [];
        page.on("pageerror", (error) => errors.push(error.message));
        await page.goto(`${info.origin}/classic`);
        const card = (id) => page.locator(`[data-step="${id}"]:visible`).first();
        // Next up shows its body; a queue row opens on its title.
        const opened = async (id) => {
          await card(id).waitFor();
          if (!(await card(id).locator(".prose.body").count())) await card(id).locator(".row-title").first().click();
          await card(id).locator(".prose.body").first().waitFor();
          return card(id);
        };

        const mainCard = await opened(main.id);
        const row = mainCard.locator(".core-links");
        await row.waitFor();
        const chips = await row.locator("a.link-chip").evaluateAll((anchors) => anchors.map((a) => ({ href: a.getAttribute("href"), words: a.textContent })));
        check(`${at}: the core links show as chips, the page opening for the step`,
          JSON.stringify(chips) === JSON.stringify([{ href: GITHUB, words: "app#10" }, { href: forStep, words: "Plan page" }]), JSON.stringify(chips));
        const order = await mainCard.evaluate((el) => {
          const [body, links, actions] = [".prose.body", ".core-links", ".actions"].map((selector) => el.querySelector(selector));
          const before = (a, b) => Boolean(a && b && a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
          return { afterText: before(body, links), aboveAnswers: before(links, actions) && links.getBoundingClientRect().bottom <= actions.getBoundingClientRect().top };
        });
        check(`${at}: the chips come after the text and above the answers`, order.afterText && order.aboveAnswers, JSON.stringify(order));

        const summary = row.locator(".links-other > summary");
        const count = (await summary.textContent()).replace(/\s+/g, " ").trim();
        check(`${at}: Other counts the task's other links`, count === "Other 4", count);
        const groups = await row.locator(".links-other .menu-list").evaluate((list) => {
          const found = {};
          let head = null;
          for (const child of list.children) {
            if (child.matches(".links-head")) { head = child.textContent; found[head] = []; } else if (child.matches("a")) found[head].push(child.getAttribute("href"));
          }
          return found;
        });
        check(`${at}: Other holds the task's link and the earlier mentions, in their groups`,
          JSON.stringify(groups) === JSON.stringify({ "On this task": [LINEAR], "Mentioned earlier": [DESIGN, SLACK, NOTES] }), JSON.stringify(groups));
        const inOther = [...groups["On this task"] || [], ...groups["Mentioned earlier"] || []];
        check(`${at}: Other leaves out the core links`, !inOther.includes(GITHUB) && !inOther.some((href) => href.includes(`/pages`)), JSON.stringify(inOther));
        // The menu's session entry opens the terminal (an iterm2: link); no web address is in it.
        const menuLinks = await mainCard.locator(".step-menu .menu-list a[href^='http']").evaluateAll((anchors) => anchors.map((a) => `${a.textContent} ${a.getAttribute("href")}`));
        check(`${at}: the ⋯ menu holds no web link`, menuLinks.length === 0, JSON.stringify(menuLinks));
        const inline = await mainCard.locator(`.prose.body a.page-link[href="${forStep}"] svg.icon-doc`).count();
        check(`${at}: the shared page's link in a sentence has the page icon`, inline === 1, `${inline} page icons`);

        // Tab from the last chip reaches Other; Enter opens it, a redraw keeps it open, and Escape closes it.
        await row.locator("a.link-chip").last().focus();
        await page.keyboard.press("Tab");
        // A poll can redraw the card in between, so the check names the focused element by what it is.
        const onOther = await page.evaluate((id) => {
          const focused = document.activeElement;
          return (focused.matches(".links-other > summary") && focused.closest("[data-step]")?.dataset.step === String(id)) || focused.outerHTML.slice(0, 160);
        }, main.id);
        check(`${at}: Tab from the last chip reaches Other`, onOther === true, `focus on ${onOther}`);
        await page.keyboard.press("Enter");
        const isOpen = () => page.locator(`[data-step="${main.id}"]:visible .links-other`).first().evaluate((el) => el.open);
        check(`${at}: Enter opens Other`, await isOpen(), "closed");
        await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
        await sleep(800);
        check(`${at}: a redraw keeps Other open`, await isOpen(), "closed");
        const list = page.locator(`[data-step="${main.id}"]:visible .links-other > .menu-list`).first();
        // On a phone the list is a sheet inside the window; on a desktop it drops from its chip, as the ⋯ menu does, and
        // may reach below the window's bottom, which the page scrolls to.
        const box = await list.boundingBox();
        const across = box && box.x >= 0 && box.x + box.width <= width;
        const inside = width > 640 ? across : across && box.y >= 0 && box.y + box.height <= 900;
        check(`${at}: the open list stays inside the window${width > 640 ? "'s width" : ""}`, inside, JSON.stringify(box));
        const shot = path.join(work, `core-links-${width}-${scheme}.png`);
        await page.screenshot({ path: shot });
        console.error(`  screenshot ${shot}`);
        await page.keyboard.press("Escape");
        check(`${at}: Escape closes Other`, !(await isOpen()), "open");

        const lines = await mainCard.locator(".core-links").evaluate((row) => ({
          chips: [...row.querySelectorAll("a.link-chip")].map((a) => getComputedStyle(a).textDecorationLine),
          other: getComputedStyle(row.querySelector(".links-other > summary")).textDecorationLine,
        }));
        check(`${at}: link chips are underlined and Other is not`,
          lines.chips.length > 0 && lines.chips.every((line) => line === "underline") && lines.other === "none", JSON.stringify(lines));

        colours[at] = await mainCard.locator("a.link-chip").first().evaluate((el) => {
          const style = getComputedStyle(el);
          return `${style.color} on ${style.backgroundColor}`;
        });

        const otherCard = await opened(otherOnly.id);
        const otherRow = await otherCard.locator(".core-links").evaluate((el) => ({
          chips: el.querySelectorAll("a.link-chip").length,
          other: el.querySelector(".links-other > summary")?.textContent.replace(/\s+/g, " ").trim(),
          links: [...el.querySelectorAll(".links-other .menu-list a")].map((a) => a.getAttribute("href")),
        })).catch((error) => ({ error: error.message }));
        check(`${at}: a step without core links shows only Other`,
          otherRow.chips === 0 && otherRow.other === "Other 1" && JSON.stringify(otherRow.links) === JSON.stringify([SPEC_DOC]), JSON.stringify(otherRow));
        const bareCard = await opened(bare.id);
        const bareRows = await bareCard.locator(".core-links").count();
        check(`${at}: a step without links shows no row`, bareRows === 0, `${bareRows} rows`);

        check(`${at}: no page errors`, errors.length === 0, errors.join(" | "));
        await context.close();
      }
    }
    for (const width of [1440, 390]) {
      check(`${width} px: the chips' colours change with the scheme`, colours[`${width} px light`] !== colours[`${width} px dark`],
        `${colours[`${width} px light`]} in both`);
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
