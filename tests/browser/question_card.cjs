// An agent's question card shows a pick-one question's options as answer keys (SPEC SESSION-30), at 1280 px light
// and 390 px dark. Serves this checkout with serve_preview.py and adds two question requests to each /api/state and
// /api/listed answer, so no agent runs:
// - Gemini's one question: a press on an option sends that option at once, with no Send answer key;
// - Codex's two questions: a press picks an option, a second press on another moves the pick, and Send answer sends
//   it with the written answer to the other question.
// Prints one line per check, saves screenshots as question-card-<width>-*.png in the work folder, and exits 1 when
// one failed.  Usage, from the checkout: node tests/browser/question_card.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder, fullState } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const THREAD = "44444444-4444-4444-8444-444444444444";

function questionRequest(id) {
  if (id.startsWith("gemini")) {
    return { id, request_id: 7, method: "item/tool/requestUserInput", provider: "gemini", params: {
      threadId: THREAD, reason: "", cwd: "/home/u/dev/tasks", questions: [{
        id: "interaction_q1", header: "", question: "Which list should the weekly reminder go to?", isOther: false, isSecret: false,
        options: [{ label: "Groceries", description: "" }, { label: "Errands", description: "" },
          { label: "A new list called Weekly", description: "" }] }] } };
  }
  return { id, request_id: 8, method: "item/tool/requestUserInput", provider: "codex", params: {
    threadId: THREAD, turnId: "t1", itemId: "i1", questions: [
      { id: "format", header: "Format", question: "Which format should the export use?", isOther: false, isSecret: false,
        options: [{ label: "CSV", description: "opens in any spreadsheet" }, { label: "JSON", description: "keeps the reminders' times" }] },
      { id: "name", header: "Name", question: "What should the file be called?", isOther: true, isSecret: false, options: [] },
    ] } };
}

let failures = 0;
function check(ok, words) {
  console.log(`${ok ? "ok  " : "FAIL"} ${words}`);
  if (!ok) failures += 1;
}

async function main() {
  const work = workFolder("cockpit-question-card-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser;
  try {
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const info = await harnessUp(work, server);
    for (const [width, scheme] of [[1280, "light"], [390, "dark"]]) {
      const phone = width < 600;
      const at = `${width} px ${scheme}`;
      const gemini = `gemini${width}`;
      const codex = `codex${width}`;
      const open = new Set([gemini, codex]);
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      await context.route("**/api/state", async (route) => {
        const full = await fullState(route).catch(() => null);
        if (!full) return;
        const state = await full.response.json();
        await route.fulfill({ ...full, json: { ...state, codex_requests: [...open].map(questionRequest), away: null } }).catch(() => {});
      });
      await context.route("**/api/listed", async (route) => {
        const response = await route.fetch().catch(() => null);
        if (!response) return;
        const listed = await response.json();
        await route.fulfill({ response, json: { ...listed, codex_requests: [...open] } }).catch(() => {});
      });
      const answers = [];
      await context.route("**/api/codex/requests/*/answer", async (route) => {
        const id = route.request().url().split("/").at(-2);
        answers.push({ id, body: route.request().postDataJSON() });
        open.delete(id);
        await route.fulfill({ status: 200, contentType: "application/json", json: { ok: true } }).catch(() => {});
      });
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(String(error)));
      await page.goto(`${info.origin}/`, { waitUntil: "load", timeout: 120e3 });
      await page.waitForSelector(`#sidebar li.warn-row[data-codex="${gemini}"]`, { state: "attached", timeout: 60e3 });
      const openCard = async (id) => {
        if (phone) await page.evaluate(() => setPhoneScreen(true, false));
        await page.locator(`#sidebar li.warn-row[data-codex="${id}"] .side-row`).click();
        await page.waitForSelector(`#next [data-codex="${id}"]`, { timeout: 15e3 });
        await sleep(400);
      };
      const card = (id) => page.locator(`#next [data-codex="${id}"]`).filter({ hasNot: page.locator(".motion-ghost") }).first();

      await openCard(gemini);
      await page.screenshot({ path: path.join(work, `question-card-${width}-gemini.png`) });
      const keys = await card(gemini).locator(".answers .button").allTextContents();
      check(JSON.stringify(keys) === JSON.stringify(["Groceries", "Errands", "A new list called Weekly"]),
        `${at}: Gemini's options show as answer keys ${JSON.stringify(keys)}`);
      check(await card(gemini).getByRole("button", { name: "Send answer" }).count() === 0, `${at}: a one-question card has no Send answer`);
      check(await card(gemini).locator("textarea").count() === 0, `${at}: a pick-one question has no text box`);
      await card(gemini).getByRole("button", { name: "Errands", exact: true }).click();
      await sleep(300);
      check(answers.length === 1 && JSON.stringify(answers[0].body) === JSON.stringify({ answers: { interaction_q1: { answers: ["Errands"] } } }),
        `${at}: a press sends that option ${JSON.stringify(answers)}`);

      await openCard(codex);
      const format = card(codex).getByRole("group", { name: "Which format should the export use?" });
      await format.getByRole("button", { name: /^CSV/ }).click();
      await sleep(150);
      await format.getByRole("button", { name: /^JSON/ }).click();
      await sleep(150);
      const pressed = await format.locator(".button").evaluateAll((els) => els.map((el) => [el.textContent, el.getAttribute("aria-pressed"), el.classList.contains("on")]));
      check(JSON.stringify(pressed) === JSON.stringify([["CSV: opens in any spreadsheet", "false", false], ["JSON: keeps the reminders' times", "true", true]]),
        `${at}: a second press moves the pick ${JSON.stringify(pressed)}`);
      check(answers.length === 1, `${at}: a pick on a two-question card sends nothing yet`);
      await card(codex).getByRole("textbox", { name: "What should the file be called?" }).fill("reminders-week-48");
      await page.mouse.move(0, 0);
      await sleep(300);
      const kept = await format.locator(".button.on").allTextContents();
      check(JSON.stringify(kept) === JSON.stringify(["JSON: keeps the reminders' times"]), `${at}: the pick stays while the other answer is written ${JSON.stringify(kept)}`);
      const looks = await format.locator(".button").evaluateAll((els) => els.map((el) => ({ background: getComputedStyle(el).backgroundColor })));
      check(looks[0].background !== looks[1].background, `${at}: the picked key looks picked ${JSON.stringify(looks)}`);
      await page.screenshot({ path: path.join(work, `question-card-${width}-codex.png`) });
      await card(codex).getByRole("button", { name: "Send answer" }).click();
      await sleep(300);
      check(answers.length === 2 && JSON.stringify(answers[1].body) === JSON.stringify({ answers: { format: { answers: ["JSON"] }, name: { answers: ["reminders-week-48"] } } }),
        `${at}: Send answer sends the pick and the written answer ${JSON.stringify(answers.slice(1))}`);

      const fits = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
      check(fits, `${at}: nothing is wider than the window`);
      check(errors.length === 0, `${at}: no page errors ${JSON.stringify(errors)}`);
      await context.close();
    }
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  console.log(failures ? `${failures} failed` : "all passed");
  process.exitCode = failures ? 1 : 0;
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
