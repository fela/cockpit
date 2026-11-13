// A link to a pull request draws the PR's state (SPEC NEXT-66): open green, draft grey, merged purple, closed red, and
// the open drawing in grey while the state is not known yet, in place of the GitHub logo. Puts one task in the user's
// queue that links five PRs (#11 open, #12 draft, #13 merged, #14 closed, #15 not known), writes the four known states
// into the fixture database as the server's GitHub check would, and shows a step whose core links are #11, #13 and #15
// and whose text links #12 and #13. Hover
// text is read from `data-tip`, where motion.js moves a `title`.
// Checks at 1440 and 390 px, light and dark:
// - each core link's chip has its state's icon and colour, and its hover text names the state (none for #15);
// - no PR link shows the GitHub logo;
// - the links in the step's text carry their state's class, mark colour and hover text;
// - "Other" lists #14, the one link not on the step, with its state's icon;
// - the four colours differ from each other, and merged differs between the schemes;
// - no page errors.
// Saves a screenshot of the card, with Other open, per width and scheme in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and drives a
// headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/pr_states.cjs

"use strict";

const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { startPreview } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

const PYTHON = path.join(__dirname, "..", "..", ".venv", "bin", "python");
// The harness's agent token and second fake session (tests/conftest.py).
const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "pr-states-test", "content-type": "application/json" };
const PR = (number) => `https://github.com/example-org/app/pull/${number}`;
const STATES = { 11: "open", 12: "draft", 13: "merged", 14: "closed" };

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Runs one SQL statement with text parameters on the fixture database.
function sql(work, statement, ...params) {
  const script = "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute(sys.argv[2], sys.argv[3:]); c.commit()";
  const result = spawnSync(PYTHON, ["-c", script, path.join(work, "cockpit.db"), statement, ...params.map(String)]);
  if (result.status !== 0) throw new Error(`${statement}: ${result.stderr}`);
}

async function main() {
  const { work, info } = await startPreview("cockpit-pr-states-");
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
    // The fixture database's open steps are snoozed, so this run's step takes the user's first place.
    const initial = await call("GET", "/api/state", headers);
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((one) => call("POST", `/api/steps/${one.id}/snooze`, headers, { until: "2099-01-01T08:00:00Z" })));
    const run = Date.now();
    const key = `note:pr-states-${run}`;
    await call("PUT", "/api/items", AGENT, {
      key, title: "PR states test", priority: "urgent",
      links: [11, 12, 13, 14, 15].map((number) => ({ label: `app#${number}`, url: PR(number) })),
    });
    for (const [number, state] of Object.entries(STATES)) {
      sql(work, "INSERT INTO pull_states (ref, state, checked_at) VALUES (?, ?, ?)", `example-org/app#${number}`, state, "2026-01-08T10:00:00Z");
    }
    const { step } = await call("POST", "/api/steps", AGENT, {
      session_id: SESSION_B, item_key: key, need: `look-${run}`, ask: "PR states test", score: 100, answers: ["Fine"],
      body: `The [draft](${PR(12)}) waits and the [merged one](${PR(13)}) is in.`,
      core_links: [{ label: "app#11", url: PR(11) }, { label: "app#13", url: PR(13) }, { label: "app#15", url: PR(15) }],
    });

    const merged = {};
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
        const card = page.locator(`[data-step="${step.id}"]:visible`).first();
        await card.waitFor();
        if (!(await card.locator(".prose.body").count())) await card.locator(".row-title").first().click();
        await card.locator(".core-links").waitFor();

        // Opens Other and reads the card in one go, as a poll's redraw (every five seconds) can replace the card
        // between two reads; a second try reads the new one.
        const read = () => page.evaluate((id) => {
          const shown = [...document.querySelectorAll(`[data-step="${id}"]`)].find((el) => el.offsetParent !== null);
          const other = shown.querySelector(".links-other");
          other.open = true;
          const tip = (a) => a.dataset.tip || a.title;
          return {
            chips: [...shown.querySelectorAll(".core-links a.link-chip")].map((a) => {
              const svg = a.querySelector(".icon");
              return { href: a.getAttribute("href"), icon: svg.getAttribute("class"), colour: getComputedStyle(svg).color, title: tip(a) };
            }),
            inText: [...shown.querySelectorAll(".prose.body a.src-github")].map((a) => ({
              href: a.getAttribute("href"), classes: a.className, title: tip(a), mark: getComputedStyle(a, "::before").backgroundColor,
            })),
            other: [...other.querySelectorAll(".menu-list a")].map((a) => ({
              href: a.getAttribute("href"), icon: a.querySelector(".icon").getAttribute("class"), colour: getComputedStyle(a.querySelector(".icon")).color,
            })),
            github: shown.querySelectorAll(".icon-github").length,
          };
        }, step.id);
        const seen = await read().catch(read);
        const find = (list, number) => list.find((one) => one.href === PR(number)) || {};
        const chip = (number) => find(seen.chips, number);
        const text = (number) => find(seen.inText, number);
        const listed = (number) => find(seen.other, number);
        for (const [number, want] of [[11, "pr-open"], [13, "pr-merged"], [15, "pr"]]) {
          check(`${at}: #${number}'s chip draws ${want}`, chip(number).icon === `icon icon-${want}`, JSON.stringify(chip(number)));
        }
        check(`${at}: the chips' hover text names a known state`,
          chip(11).title === "app#11 · open" && chip(13).title === "app#13 · merged" && !chip(15).title.includes("·"),
          JSON.stringify(seen.chips.map((one) => one.title)));
        check(`${at}: the links in the text carry their state`,
          / pull /.test(` ${text(12).classes} `) && /pr-draft/.test(text(12).classes) && /pr-merged/.test(text(13).classes)
            && text(12).title === "draft · draft" && text(13).title === "merged one · merged",
          JSON.stringify(seen.inText));
        check(`${at}: Other lists #14 by its state`,
          seen.other.length === 1 && listed(14).icon === "icon icon-pr-closed", JSON.stringify(seen.other));
        check(`${at}: no PR link shows the GitHub logo`, seen.github === 0, `${seen.github} GitHub logos`);

        const draft = text(12).mark;
        const colours = [chip(11).colour, draft, chip(13).colour, listed(14).colour];
        check(`${at}: the four states' colours differ`, new Set(colours).size === 4, colours.join(" | "));
        check(`${at}: a state not known yet is grey, as a draft`, chip(15).colour === draft, `${chip(15).colour} vs ${draft}`);
        check(`${at}: a merged link's mark in the text is purple, as its chip`, text(13).mark === chip(13).colour, `${text(13).mark} vs ${chip(13).colour}`);
        merged[at] = chip(13).colour;

        const shot = path.join(work, `pr-states-${width}-${scheme}.png`);
        await page.evaluate((id) => {
          const shown = [...document.querySelectorAll(`[data-step="${id}"]`)].find((el) => el.offsetParent !== null);
          shown.querySelector(".links-other").open = true;
        }, step.id);
        const take = () => card.screenshot({ path: shot });
        await take().catch(take);
        console.error(`  screenshot ${shot}`);
        await sleep(100);
        check(`${at}: no page errors`, errors.length === 0, errors.join(" | "));
        await context.close();
      }
    }
    for (const width of [1440, 390]) {
      check(`${width} px: merged's purple changes with the scheme`, merged[`${width} px light`] !== merged[`${width} px dark`],
        `${merged[`${width} px light`]} in both`);
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
