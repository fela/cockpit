// A Codex request's card leaves at once when the user answers it (SPEC PERM-30), at 1280 px light and 390 px dark.
// Serves this checkout with serve_preview.py and adds one Codex command request to each /api/state and /api/listed
// answer, so no Codex runs. The answer endpoint is held for two seconds:
// - Run takes the card and its Inbox row off before the server answers, and they stay off once it accepted;
// - a refused Deny brings the card back with the server's reason on it.
// Prints one line per check, saves screenshots as codex-card-<width>-*.png in the work folder, and exits 1 when one
// failed.  Usage, from the checkout: node tests/browser/codex_request_card.cjs

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
const HOLD = 2000;

// The request the user runs and the one whose Deny is refused ask different things, so the screenshots tell them apart.
function codexRequest(id) {
  const run = id.startsWith("run");
  return {
    id, request_id: 7, method: "item/commandExecution/requestApproval",
    params: { threadId: "44444444-4444-4444-8444-444444444444", turnId: "t1", itemId: "i1",
      reason: run ? "May I list the exports to check the weekly reminder file?" : "May I delete last month's exports?",
      command: run ? "/bin/bash -lc 'ls /tmp/tasks-export'" : "/bin/bash -lc 'rm -r /tmp/tasks-export/2026-10'",
      cwd: "/home/u/dev/tasks" },
  };
}

let failures = 0;
function check(ok, words) {
  console.log(`${ok ? "ok  " : "FAIL"} ${words}`);
  if (!ok) failures += 1;
}

async function main() {
  const work = workFolder("cockpit-codex-card-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser;
  try {
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const info = await harnessUp(work, server);
    for (const [width, scheme] of [[1280, "light"], [390, "dark"]]) {
      const phone = width < 600;
      const at = `${width} px ${scheme}`;
      // Each look has its own two requests: one the user runs, one whose Deny the server refuses.
      const accepted = `run${width}`;
      const refused = `deny${width}`;
      const open = new Set([accepted, refused]);
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const requests = () => [...open].map(codexRequest);
      await context.route("**/api/state", async (route) => {
        const full = await fullState(route).catch(() => null);
        if (!full) return;
        const state = await full.response.json();
        await route.fulfill({ ...full, json: { ...state, codex_requests: requests(), away: null } }).catch(() => {});
      });
      await context.route("**/api/listed", async (route) => {
        const response = await route.fetch().catch(() => null);
        if (!response) return;
        const listed = await response.json();
        await route.fulfill({ response, json: { ...listed, codex_requests: [...open] } }).catch(() => {});
      });
      const answers = [];
      const answered = new Map();
      await context.route("**/api/codex/requests/*/answer", async (route) => {
        const id = route.request().url().split("/").at(-2);
        answers.push({ id, body: route.request().postDataJSON(), at: Date.now() });
        await sleep(HOLD);
        answered.set(id, Date.now());
        if (id === refused) {
          await route.fulfill({ status: 409, contentType: "application/json", json: { detail: "Codex already ended that turn" } }).catch(() => {});
          return;
        }
        open.delete(id);
        await route.fulfill({ status: 200, contentType: "application/json", json: { ok: true } }).catch(() => {});
      });
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(String(error)));
      await page.goto(`${info.origin}/`, { waitUntil: "load", timeout: 120e3 });
      await page.waitForSelector(`#sidebar li.warn-row[data-codex="${accepted}"]`, { state: "attached", timeout: 60e3 });

      const openCard = async (id) => {
        if (phone) await page.evaluate(() => setPhoneScreen(true, false));
        await page.locator(`#sidebar li.warn-row[data-codex="${id}"] .side-row`).click();
        await page.waitForSelector(`#next [data-codex="${id}"]`, { timeout: 15e3 });
        await sleep(400);
      };
      const shown = (id) => page.evaluate((key) => {
        const stand = (selector) => [...document.querySelectorAll(selector)].find((el) => !el.closest(".motion-ghost, .close-copy"));
        const card = stand(`#next [data-codex="${key}"]`);
        return { card: Boolean(card), row: Boolean(stand(`#sidebar li.warn-row[data-codex="${key}"]`)),
          error: card?.querySelector(".request-error")?.textContent || "" };
      }, id);

      // Logs in the page each time the card and its row go off or come back, so a slow test driver cannot blur the
      // order; a redraw that replaces the card for a moment does not count as taking it off.
      const watch = (id) => page.evaluate((key) => {
        window.cardLog = [];
        let was = false;
        // A card or row leaving plays out on a copy (motion.js); that copy is not the card.
        const stands = (selector) => [...document.querySelectorAll(selector)].some((el) => !el.closest(".motion-ghost, .close-copy"));
        new MutationObserver(() => queueMicrotask(() => {
          const off = !stands(`#next [data-codex="${key}"]`) && !stands(`#sidebar li.warn-row[data-codex="${key}"]`);
          if (off !== was) window.cardLog.push([Date.now(), off]);
          was = off;
        })).observe(document.body, { childList: true, subtree: true });
      }, id);
      // Whether the card was off, and stayed off, from before the server answered until it did.
      const goneBefore = async (id) => {
        const log = await page.evaluate(() => window.cardLog);
        const answeredAt = answered.get(id) ?? Infinity;
        const before = log.filter(([time]) => time < answeredAt);
        return { log, answeredAt, early: before.length > 0 && before.at(-1)[1] === true };
      };

      await openCard(accepted);
      await page.screenshot({ path: path.join(work, `codex-card-${width}-before.png`) });
      const card = page.locator(`#next [data-codex="${accepted}"]`);
      await watch(accepted);
      await card.getByRole("button", { name: "Run", exact: true }).click();
      await sleep(150);
      const right = await shown(accepted);
      const { early } = await goneBefore(accepted);
      check(answers.length === 1 && answers[0].body.decision === "accept", `${at}: Run sent its answer ${JSON.stringify(answers)}`);
      check(early && !right.card && !right.row, `${at}: Run takes the card and its row off before the server answers ${JSON.stringify({ ...right, early })}`);
      await page.screenshot({ path: path.join(work, `codex-card-${width}-after-run.png`) });
      await sleep(HOLD + 1500);
      const later = await shown(accepted);
      check(!later.card && !later.row, `${at}: the card stays off once the server accepted ${JSON.stringify(later)}`);

      await openCard(refused);
      await watch(refused);
      await page.locator(`#next [data-codex="${refused}"]`).getByRole("button", { name: "Deny", exact: true }).click();
      await sleep(150);
      const gone = await shown(refused);
      const denied = await goneBefore(refused);
      check(denied.early, `${at}: Deny takes the card off at once too ${JSON.stringify({ ...gone, ...denied })}`);
      await sleep(HOLD + 800);
      // The refused card comes back in Inbox; open it to read the reason.
      const back = await shown(refused);
      if (!back.card && back.row) await openCard(refused);
      const reason = await shown(refused);
      check(reason.card && reason.error.includes("Codex already ended that turn"),
        `${at}: a refused answer brings the card back with the reason ${JSON.stringify(reason)}`);
      await page.screenshot({ path: path.join(work, `codex-card-${width}-refused.png`) });

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
