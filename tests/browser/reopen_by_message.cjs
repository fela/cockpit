// A message on a finished task takes it back to Busy, and the page says so clearly (SPEC CONTROL-28). For each of
// desktop light, desktop dark and a phone width, picks a task in Finished this week and checks:
// - its message box says that sending reopens the task;
// - at Send, before the server answers, the task leaves Finished this week, Busy's folded heading lights up with its
//   count one higher and "Reopened", and a notice names the task;
// - with Busy opened, the row is in Busy, and first;
// - the server agrees (group busy, stage building), and after the next read of the state it is still in Busy;
// - the heading is plain again after its few seconds;
// - a task stays finished when Send is refused; one or two Sends queued behind a pending finish reach the server after
//   it; a reply from a page says the task reopened.
// Saves screenshots in the work dir.
//
// Starts serve_preview.py with this checkout's Cockpit on a synthetic database (README.md lists its
// requirements; nothing reaches a real session) and drives a headless Chromium. Prints one line per check and exits 1
// when one failed.
//
// Usage, from the checkout: node tests/browser/reopen_by_message.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "reopen-test", "content-type": "application/json" };
// The harness's fake session that keeps working, so the task stays in Busy as it does while a real session answers.
const WORKING_SESSION = "22222222-2222-4222-8222-222222222222";
const LOOKS = [
  { name: "desktop-light", width: 1280, height: 900, colorScheme: "light" },
  { name: "desktop-dark", width: 1280, height: 900, colorScheme: "dark" },
  { name: "phone", width: 390, height: 844, colorScheme: "light", phone: true },
];

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-reopen-by-message-");
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
    const readState = () => fetch(`${info.origin}/api/state`, { headers: { cookie } }).then((response) => response.json());
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const busy = "#sidebar details.sidebar-group.busy";
    const done = "#sidebar details.sidebar-group.done";
    // Seed a finished task per viewport and refusal/ordering case, plus an active task for the Busy count.
    const userHeaders = { cookie, origin: info.origin, "content-type": "application/json" };
    for (let n = 0; n < LOOKS.length + 3; n++) {
      const key = `note:reopen-fixture-${n}`;
      await fetch(`${info.origin}/api/items`, { method: "PUT", headers: AGENT,
        body: JSON.stringify({ key, title: `List reminder ${n}` }) });
      await fetch(`${info.origin}/api/items`, { method: "PUT", headers: userHeaders,
        body: JSON.stringify({ key, stage: "done" }) });
    }
    await fetch(`${info.origin}/api/items`, { method: "PUT", headers: AGENT,
      body: JSON.stringify({ key: "note:reopen-busy", title: "Shared list exports", stage: "building", sessions: [WORKING_SESSION] }) });
    const used = new Set();
    for (const look of LOOKS) {
      const state = await readState();
      const target = state.items.find((item) => item.group === "done" && item.stage === "done" && !used.has(item.id));
      if (!target) throw new Error("no finished task left in the fixture");
      used.add(target.id);
      const put = await fetch(`${info.origin}/api/items`, { method: "PUT", headers: AGENT,
        body: JSON.stringify({ key: target.key, sessions: [WORKING_SESSION] }) });
      if (!put.ok) throw new Error(`put ${put.status}: ${await put.text()}`);

      const context = await browser.newContext({ viewport: { width: look.width, height: look.height }, colorScheme: look.colorScheme,
        isMobile: Boolean(look.phone), hasTouch: Boolean(look.phone) });
      await context.setExtraHTTPHeaders({ cookie });
      const page = await context.newPage();
      await page.addLocatorHandler(page.locator("dialog#idle[open]"), (dialog) => dialog.locator("button", { hasText: "Return" }).click()
        .catch((error) => { if (!/has been closed/.test(error.message)) throw error; }));
      page.setDefaultTimeout(30000);
      page.setDefaultNavigationTimeout(60000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      const say = (name, ok, detail) => check(`${look.name}: ${name}`, ok, detail);

      await page.goto(`${info.origin}/`);
      await page.locator(done).waitFor({ state: "attached", timeout: 30e3 });
      const openIt = async (group) => { if (!(await page.locator(group).evaluate((el) => el.open))) await page.locator(`${group} > summary`).click(); };
      const closeIt = async (group) => { if (await page.locator(group).evaluate((el) => el.open)) await page.locator(`${group} > summary`).click(); };
      if (look.phone) await page.locator(".phone-back").first().click();
      await closeIt(busy);
      await openIt(done);
      await page.locator(`${done} li[data-item="${target.id}"] .side-row`).first().click();
      const area = page.locator(`[data-item="${target.id}"] .composer textarea, .item-card .composer textarea`).first();
      await area.waitFor({ state: "visible" });
      const hint = await area.getAttribute("placeholder");
      say("the message box says sending reopens the task", /reopens this task/.test(hint || ""), hint);
      const countOf = (group) => page.locator(`${group} > summary`).evaluate((summary) => Number(summary.querySelector(".count-now, .group-count").textContent));
      const countBefore = await countOf(busy);
      const finishedBefore = await countOf(done);
      await page.screenshot({ path: path.join(work, `${look.name}-before.png`) });

      await area.fill("One more thing on this");
      await area.locator("xpath=ancestor::*[contains(@class,'composer')][1]").locator(".send").first().click();
      await page.locator(`${busy} > summary.holding .busy-reopened`).waitFor({ state: "attached", timeout: 3000 }).catch(() => {});
      const heading = await page.locator(`${busy} > summary`).evaluate((summary) => ({
        holding: summary.classList.contains("holding"),
        word: summary.querySelector(".busy-reopened")?.textContent || "",
        count: Number(summary.querySelector(".count-now")?.textContent),
      }));
      say("Busy's folded heading lights up and says Reopened", heading.holding && heading.word === "Reopened", JSON.stringify(heading));
      say("its count is one higher", heading.count === countBefore + 1, `${countBefore} then ${heading.count}`);
      say("the task left Finished this week", (await page.locator(`${done} li[data-item="${target.id}"]`).count()) === 0, "still listed");
      say("Finished this week counts one fewer", (await countOf(done)) === finishedBefore - 1, `${finishedBefore} then ${await countOf(done)}`);
      const notice = await page.locator("#toast, .toast").allInnerTexts();
      say("a notice names the task", notice.some((text) => text.includes("Reopened") && text.includes(target.title)), notice.join(" | "));
      await sleep(1600);
      await page.screenshot({ path: path.join(work, `${look.name}-sent-card.png`) });
      if (look.phone) await page.locator(".phone-back").first().click();
      await page.screenshot({ path: path.join(work, `${look.name}-sent-folded.png`) });
      await page.locator(`${busy} > summary`).screenshot({ path: path.join(work, `${look.name}-heading.png`) });

      await openIt(busy);
      const row = page.locator(`${busy} li[data-item="${target.id}"]`);
      await row.waitFor({ state: "attached" });
      const place = await row.evaluate((li) => ({ first: li.parentElement.firstElementChild === li, classes: li.className }));
      say("with Busy open, the row is first in it", place.first, JSON.stringify(place));
      await page.screenshot({ path: path.join(work, `${look.name}-sent-open.png`) });

      await waitFor(readState, (now) => now.items.find((item) => item.id === target.id)?.group === "busy", 10e3);
      const after = (await readState()).items.find((item) => item.id === target.id);
      say("the server has it in Busy at stage building", after?.group === "busy" && after?.stage === "building", JSON.stringify({ group: after?.group, stage: after?.stage }));
      await sleep(3000);
      if (look.phone) await page.screenshot({ path: path.join(work, `${look.name}-list.png`) });
      say("after the next read it is still in Busy", (await row.count()) === 1 && (await page.locator(`${done} li[data-item="${target.id}"]`).count()) === 0, "moved");
      await closeIt(busy);
      await waitFor(() => page.locator(`${busy} > summary`).evaluate((summary) => summary.classList.contains("holding")), (lit) => !lit, 10e3);
      say("the heading is plain again after its few seconds",
        !(await page.locator(`${busy} > summary`).evaluate((summary) => summary.classList.contains("holding"))), "still lit");
      if (look === LOOKS[0]) await refusedAndInTurn(page, say, target, state, used, { done, busy, openIt, closeIt, countOf });
      if (look === LOOKS[0]) await pageReply(context, info.origin, { cookie }, say);
      say("no page errors", errors.length === 0, errors.join(" | "));
      await context.close();
    }
  } finally {
    if (browser) await browser.close();
    server.kill("SIGTERM");
  }
  const failed = results.filter((result) => !result.ok).length;
  console.error(`${results.length - failed} passed, ${failed} failed; screenshots in ${work}`);
  if (failed) process.exitCode = 1;
}

// A refused Send leaves the task finished, the lit heading gone and the words back in the box. A Send pressed while a
// finish still waits its turn (`inTurn`) reaches the server after that finish.
async function refusedAndInTurn(page, say, reopened, state, used, { done, busy, openIt, closeIt, countOf }) {
  const pick = () => {
    const next = state.items.find((item) => item.group === "done" && item.stage === "done" && !used.has(item.id));
    if (!next) throw new Error("no finished task left in the fixture");
    used.add(next.id);
    return next;
  };
  const write = async (target, words) => {
    await closeIt(busy);
    await openIt(done);
    await page.locator(`${done} li[data-item="${target.id}"] .side-row`).first().click();
    const area = page.locator(".item-card .composer textarea").first();
    await area.waitFor({ state: "visible" });
    await area.fill(words);
    return area;
  };
  const sendOf = (area) => area.locator("xpath=ancestor::*[contains(@class,'composer')][1]").locator(".send").first();

  const refused = pick();
  const finishedBefore = await countOf(done);
  await page.route("**/api/messages", (route) => (route.request().method() === "POST"
    ? route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: "refused by the test" }) })
    : route.continue()));
  const area = await write(refused, "Refused words");
  await sendOf(area).click();
  await waitFor(() => page.locator(`${done} li[data-item="${refused.id}"]`).count(), (count) => count === 1, 4000);
  say("a refused Send leaves the task in Finished this week", (await page.locator(`${done} li[data-item="${refused.id}"]`).count()) === 1, "not listed");
  say("Finished this week counts as before", (await countOf(done)) === finishedBefore, `${finishedBefore} then ${await countOf(done)}`);
  say("Busy's heading does not say Reopened", (await page.locator(`${busy} > summary .busy-reopened`).count()) === 0, "still says it");
  const back = await page.locator(".item-card .composer textarea").first().inputValue();
  say("the words are back in the box", back === "Refused words", back);
  await page.locator(".item-card .composer textarea").first().fill("");
  await page.unroute("**/api/messages");

  const ordered = pick();
  const area2 = await write(ordered, "After the finish");
  let posted = 0;
  const posting = (request) => { if (request.method() === "POST" && request.url().endsWith("/api/messages")) posted = Date.now(); };
  page.on("request", posting);
  const finishEnds = await page.evaluate(() => { inTurn(() => new Promise((resolve) => setTimeout(resolve, 1500))); return Date.now() + 1500; });
  await sendOf(area2).click();
  await waitFor(() => posted, (at) => at > 0, 5000);
  page.off("request", posting);
  say("a Send reaches the server after a finish still on its way", posted >= finishEnds - 100, `${posted - finishEnds} ms after its end`);

  // A second Send, right behind the first, also waits behind the finish.
  const twice = pick();
  const area3 = await write(twice, "First of two");
  const postedAt = [];
  const counting = (request) => { if (request.method() === "POST" && request.url().endsWith("/api/messages")) postedAt.push(Date.now()); };
  page.on("request", counting);
  const twiceEnds = await page.evaluate(() => { inTurn(() => new Promise((resolve) => setTimeout(resolve, 1500))); return Date.now() + 1500; });
  await sendOf(area3).click();
  await page.waitForTimeout(300);
  const again = page.locator(".item-card .composer textarea").first();
  await again.fill("Second of two");
  await sendOf(again).click();
  await waitFor(() => postedAt.length, (count) => count === 2, 6000);
  page.off("request", counting);
  say("two Sends both reach the server after the finish", postedAt.length === 2 && postedAt[0] >= twiceEnds - 100 && postedAt[1] >= twiceEnds - 100,
    `${postedAt.map((at) => at - twiceEnds).join(", ")} ms after its end`);
}

// A reply from a page, on a step still open on a finished task: it goes, and the page says the task reopened.
async function pageReply(browserContext, origin, user, say) {
  const run = Date.now();
  const write = (method, url, body, headers) => fetch(`${origin}${url}`, { method, headers, body: JSON.stringify(body) }).then(async (response) => {
    if (!response.ok) throw new Error(`${url} ${response.status}: ${await response.text()}`);
    return response.json();
  });
  const key = `note:reopen-page-${run}`;
  const item = await write("PUT", "/api/items", { key, title: "Page reply test", project: "cockpit", sessions: [WORKING_SESSION] }, AGENT);
  const report = await write("POST", `/api/items/${item.id}/pages`, { title: "A report", html: "<!doctype html><p>Words</p>" }, AGENT);
  const { step } = await write("POST", "/api/steps", { item_key: key, need: "reply:page", ask: "Ship the report?" }, AGENT);
  await write("PUT", "/api/items", { key, stage: "done" }, { ...user, origin, "content-type": "application/json" });
  const page = await browserContext.newPage();
  await page.goto(`${origin}/pages?id=${report.id}&step=${step.id}`);
  await page.locator(".pages-preview-buttons button", { hasText: "Comment" }).click();
  await page.locator("#pages-reply-text").fill("One more thing");
  const hint = await page.locator("#pages-reply-text").getAttribute("placeholder");
  say("a page's reply box says sending reopens the task", /reopens this task/.test(hint || ""), hint);
  await page.locator(".pages-reply-buttons .button.primary").click();
  await page.locator(".pages-reply-sent").waitFor();
  const banner = await page.locator("#banner").innerText();
  say("a page's reply says the task reopened", banner === "Reopened “Page reply test”. It is back in Busy.", banner);
  const state = await fetch(`${origin}/api/state`, { headers: user }).then((response) => response.json());
  say("the server has it in Busy", state.items.find((one) => one.id === item.id)?.stage === "building", "not reopened");
  await page.close();
}

async function waitFor(read, done, ms = 5000) {
  const end = Date.now() + ms;
  let value = await read();
  while (!done(value) && Date.now() < end) {
    await sleep(200);
    value = await read();
  }
  return value;
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
