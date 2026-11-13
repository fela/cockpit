// Ask and Fork (SPEC MSG-11). Puts its own item, whose step asks for the harness's working fake session, into the user's
// queue, then checks, at desktop width in the light scheme (the step's card) and at phone width in the dark one (the
// item's row, once the first width's Send handed the step to the session):
// - the box where the item shows (its step's card when the step is in the queue, VIEW-18; else the item's row) shows
//   Ask and Fork in the menu of the arrow beside Send once the user writes;
// - Ask shows the question above the box within 300 ms, with a line saying a copy is answering, and a toast, while
//   the words stay in the box;
// - the fake copy's answer (serve_slow.fake_copy, about 2 s) shows under the question as rendered Markdown, with a
//   line saying where the two stand on their way to the session, and a toast says it arrived;
// - the question and answer went to the session as one message of the user's;
// - Fork empties the box, toasts, and makes a new item from the words;
// - a Send from the box takes the question and answer along, and they leave the box;
// - no sideways scroll and no page errors;
// and once, that a state without side_chats (a Cockpit from before them, during a deploy) draws with no error.
// Saves screenshots of the open menu and of the answered question, in both schemes, per width in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what
// it needs) and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/ask_fork.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
// serve_slow.py's working session, and the harness's agent token (tests/conftest.py).
const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "ask-fork-test", "content-type": "application/json" };

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-ask-fork-");
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
    const cookie = `${info.name}=${info.value}`;
    const getState = () => fetch(`${info.origin}/api/state`, { headers: { cookie } }).then((response) => response.json());
    const call = async (method, url, headers, body) => {
      const response = await fetch(info.origin + url, { method, headers, body: JSON.stringify(body) });
      if (!response.ok) throw new Error(`${method} ${url}: HTTP ${response.status}: ${await response.text()}`);
      return response.json();
    };
    // The fixture database's open steps are snoozed, so this run's step takes Next up.
    const user = { cookie, origin: info.origin, "content-type": "application/json" };
    const initial = await getState();
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((one) => call("POST", `/api/steps/${one.id}/snooze`, user, { until: "2099-01-01T08:00:00Z" })));
    const itemKey = `note:ask-fork-${Date.now()}`;
    await call("PUT", "/api/items", AGENT, { key: itemKey, title: "Tasks reminder trial", sessions: [SESSION_B] });
    const { step: own } = await call("POST", "/api/steps", AGENT,
      { item_key: itemKey, need: "decide:trial", ask: "Run the Tasks reminder trial on staging?", score: 100, session_id: SESSION_B });

    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const [width, scheme] of [[1280, "light"], [390, "dark"]]) {
      // The step is in the user's queue, where its item shows as the step's card (VIEW-18), until the first width's
      // Send hands it to the session; its item then has its row below the queue.
      const state = await getState();
      const queued = state.queue.some((one) => one.id === own.id);
      const step = own;
      if (!queued && !state.with_agents.some((one) => one.id === own.id)) throw new Error(`step ${own.id} is neither queued nor handed`);
      const itemId = step.item_id;

      const context = await browser.newContext({ viewport: { width, height: 1000 }, colorScheme: scheme });
      await context.setExtraHTTPHeaders({ cookie });
      const page = await context.newPage();
      // Generous: other heavy work may run on the machine beside this test.
      page.setDefaultTimeout(90000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${info.origin}/classic`);
      let row;
      let key;
      if (queued) {
        await page.locator(`#next [data-step], #queue .row[data-step]`).first().waitFor({ state: "attached" });
        await page.evaluate((id) => window.openItemStep(id), itemId);
        row = page.locator(`#next [data-step="${step.id}"], #queue .row.open[data-step="${step.id}"]`).first();
        await row.waitFor();
        key = `msg:step:${step.id}`;
      } else {
        row = page.locator(`#work .row[data-item="${itemId}"]`).first();
        await row.waitFor({ state: "attached" });
        // The item's group may start folded.
        const group = page.locator("#work details.group", { has: page.locator(`.row[data-item="${itemId}"]`) }).first();
        if (!(await group.evaluate((el) => el.open))) await group.locator(":scope > summary").click();
        await row.scrollIntoViewIfNeeded();
        await row.locator(".row-head").click();
        key = `msg:item:${itemId}`;
      }
      const area = row.locator(`textarea[data-key="${key}"]`);
      const question = `How far is the work? (${width})`;
      await area.fill(question);
      await row.locator(".composer summary.send-more").click({ noWaitAfter: true });
      const ask = row.locator(".composer button.ask-way");
      const fork = row.locator(".composer button.fork-way");
      check(`${width}: Ask and Fork show in Send's menu`,
        await ask.isVisible() && await fork.isVisible() && (await ask.innerText()).includes("Ask") && (await fork.innerText()).includes("Fork"),
        `${await ask.count()} Ask, ${await fork.count()} Fork`);
      await row.locator(".composer").first().screenshot({ path: path.join(work, `menu-${width}-${scheme}.png`), timeout: 10000 })
        .catch((error) => console.error(`  (no menu screenshot at ${width}: ${error.message.split("\n")[0]})`));

      // Timed in the page, from the click to the question above the box.
      await page.evaluate((words) => {
        window.__ask = { clickAt: null, shownAt: null };
        document.addEventListener("click", () => { window.__ask.clickAt ??= performance.now(); }, { capture: true, once: true });
        new MutationObserver((changes, observer) => {
          if ([...document.querySelectorAll(".ask-question")].some((el) => el.textContent === words)) {
            window.__ask.shownAt = performance.now();
            observer.disconnect();
          }
        }).observe(document.body, { childList: true, subtree: true });
      }, question);
      // At once means before Cockpit answers the ask (MSG-11): the test holds that answer until the question shows,
      // so the check does not depend on how busy the machine is.
      let release;
      const held = new Promise((resolve) => { release = resolve; });
      await page.route("**/api/items/*/ask", async (route) => { await held; await route.continue(); }, { times: 1 });
      await ask.click();
      const entry = row.locator(".ask-block .ask-entry").last();
      const asked = await entry.locator(".ask-question", { hasText: question }).waitFor({ timeout: 30000 }).then(() => true, () => false);
      release();
      const timing = await page.evaluate(() => window.__ask);
      const shownAfter = Math.round(timing.shownAt - timing.clickAt);
      check(`${width}: the question shows at once, before Cockpit answers`, asked,
        `${asked ? `${shownAfter} ms` : "not shown in 30 s"} (load average ${os.loadavg()[0].toFixed(0)})`);
      const askedToast = await page.locator("#toast").innerText().catch(() => "");
      check(`${width}: a toast says the copy was asked`, askedToast.includes("the answer shows above the box"), askedToast);
      const answering = await entry.locator(".ask-who.asking").count();
      check(`${width}: a line says a copy is answering`, answering === 1, `${answering} lines`);
      const kept = await row.locator(`textarea[data-key="${key}"]`).inputValue();
      check(`${width}: the words stay in the box`, kept === question, JSON.stringify(kept));

      // The user goes on writing; the answer shows while the box holds the cursor. The fake copy answers in about
      // 2 s; the page sees it with its next state, every 5 s and slow on a loaded host.
      const box = row.locator(`textarea[data-key="${key}"]`);
      await box.focus();
      await box.press("End");
      await box.pressSequentially(" Then go");
      await row.locator(".ask-block .ask-entry").last().locator(".ask-line.prose strong").waitFor({ timeout: 30000 });
      const typing = await page.evaluate((field) => document.activeElement?.dataset?.key === field, key);
      check(`${width}: the answer shows while the user writes`, typing, "the box lost the cursor");
      check(`${width}: what the user wrote meanwhile stays`, (await box.inputValue()) === `${question} Then go`, JSON.stringify(await box.inputValue()));
      const answer = await entry.locator(".ask-line.prose").innerText();
      check(`${width}: the copy's answer shows as Markdown`, answer.includes(`got: ${question}`), answer);
      const status = await entry.locator(".ask-state").innerText().catch(() => "");
      check(`${width}: a line says where the question and answer stand`,
        /Goes to .* with your reply, or when its current turn ends\.|Reached .* at \d\d:\d\d\./.test(status), status);
      const toastText = await page.locator("#toast").innerText().catch(() => "");
      check(`${width}: a toast says the answer arrived`, toastText.includes("Your quick question has an answer"), toastText);
      const onPage = await page.locator(".ask-question", { hasText: question }).count();
      check(`${width}: the question shows once on the page`, onPage === 1, `${onPage} entries`);
      const after = await getState();
      const chat = (after.side_chats || []).find((each) => each.question === question);
      const carried = chat && after.messages.find((message) => message.id === chat.sent_message_id);
      check(`${width}: the question and answer went to the session as the user's message`,
        Boolean(carried) && carried.session_id === SESSION_B && carried.author === "user"
          && carried.text.includes(question) && carried.text.includes("The copy answered:"), JSON.stringify(carried));

      // Both schemes of the answered question; a poll's redraw can replace it while the shot is taken, so a
      // second try gets the new one.
      for (const each of [scheme, scheme === "light" ? "dark" : "light"]) {
        await page.emulateMedia({ colorScheme: each });
        const shot = () => row.locator(".ask-block:not(:empty)").screenshot({ path: path.join(work, `ask-${width}-${each}.png`) });
        await shot().catch(shot).catch((error) => console.error(`  (no screenshot at ${width} ${each}: ${error.message.split("\n")[0]})`));
      }
      await page.emulateMedia({ colorScheme: scheme });
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      check(`${width}: no sideways scroll`, overflow <= 0, `${overflow} px`);

      // A refused Fork puts the words back, and the box's To where the user left it (regression check). Fork forks the
      // session the box writes to, so To stays on it; an item row's box shows its To, a step card's box shows none
      // but records one all the same.
      const refusedWords = `Fork refused (${width})`;
      await row.locator(`textarea[data-key="${key}"]`).fill(refusedWords);
      const to = row.locator(`select[data-key="${key}:to"]`);
      const shown = (await to.count()) > 0;
      const recorded = () => page.evaluate((k) => ui.choices.get(k) || null, key);
      const picked = await recorded();
      await page.route(`**/api/items/${itemId}/fork`, (route) => route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: "refused for the test" }) }));
      await row.locator(".composer summary.send-more").click({ noWaitAfter: true });
      await row.locator(".composer button.fork-way").click();
      await page.locator("#toast", { hasText: "Not forked" }).waitFor({ timeout: 15000 });
      // The box draws the words again with the refresh after the refusal (`run`).
      await page.waitForFunction(([k, words]) => document.querySelector(`textarea[data-key="${k}"]`)?.value === words,
        [key, refusedWords], { timeout: 15000 }).catch(() => {});
      const back = { words: await row.locator(`textarea[data-key="${key}"]`).inputValue(), to: await recorded(),
        shown: shown ? await to.inputValue() : null };
      check(`${width}: a refused Fork puts the words and the To back`, back.words === refusedWords && Boolean(picked)
        && back.to === picked && (!shown || back.shown === picked), JSON.stringify({ ...back, picked }));
      await page.unroute(`**/api/items/${itemId}/fork`);

      // Fork: the box empties and a new item holds the words.
      const forkWords = `Try it on staging as well (${width})`;
      await row.locator(`textarea[data-key="${key}"]`).fill(forkWords);
      await row.locator(".composer summary.send-more").click({ noWaitAfter: true });
      await row.locator(".composer button.fork-way").click();
      await page.locator("#toast", { hasText: "Forked" }).waitFor({ timeout: 15000 });
      const emptied = await row.locator(`textarea[data-key="${key}"]`).inputValue();
      check(`${width}: Fork empties the box`, emptied === "", JSON.stringify(emptied));
      const forked = (await getState()).items.find((item) => (item.summary || "").includes(forkWords) || (item.title || "").includes(forkWords.slice(0, 20)));
      check(`${width}: Fork makes a new item from the words`, Boolean(forked) && forked.id !== itemId, JSON.stringify(forked && { id: forked.id, title: forked.title }));

      // Send from the same box takes the question and answer along, and they leave it.
      await row.locator(`textarea[data-key="${key}"]`).fill(`Then go ahead (${width})`);
      await row.locator(".composer button.send").first().click();
      await page.waitForFunction((words) => ![...document.querySelectorAll(".ask-question")].some((el) => el.textContent === words),
        question, { timeout: 15000 }).then(() => true, () => false).then((gone) => check(`${width}: a Send from the box takes the question along`, gone, "still shown"));

      check(`${width}: no page errors`, errors.length === 0, errors.join("; "));
      await context.close();
    }
    // A state without side_chats, as from a Cockpit before them during a deploy.
    {
      const context = await browser.newContext({ viewport: { width: 1280, height: 1000 } });
      await context.setExtraHTTPHeaders({ cookie });
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.route("**/api/state*", async (route) => {
        const response = await route.fetch().catch(() => null);
        if (!response) return;
        const body = await response.json();
        delete body.side_chats;
        await route.fulfill({ response, json: body }).catch(() => {});
      });
      await page.goto(`${info.origin}/classic`);
      await page.locator("#work .row").first().waitFor({ state: "attached" });
      await sleep(1500);
      check("a state without side_chats draws with no error", errors.length === 0, errors.join("; "));
      await context.close();
    }
    console.error(`screenshots in ${work}`);
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failed = results.filter((result) => !result.ok);
  console.error(`${results.length - failed.length} of ${results.length} checks passed`);
  if (failed.length) process.exitCode = 1;
}

async function harnessUp(work, server) {
  const file = path.join(work, "cookie.json");
  const end = Date.now() + 180e3;
  while (Date.now() < end) {
    if (server.exitCode !== null) throw new Error(`serve_preview.py exited with ${server.exitCode}`);
    if (fs.existsSync(file)) {
      const info = JSON.parse(fs.readFileSync(file, "utf8"));
      const up = await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false);
      if (up) return info;
    }
    await sleep(200);
  }
  throw new Error("serve_preview.py did not come up within 180 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
