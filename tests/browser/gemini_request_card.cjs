// A Gemini shell call's permission card (SPEC SESSION-30) says what the call is for in Antigravity's own summary, then
// its command one part per line with its words coloured (PERM-22), then its folder, at 1280 px light and 390 px dark.
// A Gemini file edit's card names the edit and shows its file and coloured diff, with no JSON. A shell call auto mode
// asks to run outside Antigravity's sandbox says so on its folder line. Serves this checkout
// with serve_preview.py and adds the Gemini request to each /api/state and /api/listed answer, so no agent runs. Prints
// one line per check, saves screenshots as gemini-card-, gemini-edit- and gemini-outside-<width>.png in the work folder,
// and exits 1 when one failed.  Usage, from the checkout: node tests/browser/gemini_request_card.cjs

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
const ID = "gemini1";
const COMMAND = "git -C /home/u/dev/tasks status --short && ls /tmp/tasks-export | head -5";

// The request as acp_agent.py puts it on the page for an Antigravity shell call.
const shellRequest = () => ({
  id: ID, request_id: 7, method: "session/request_permission", provider: "gemini",
  params: { threadId: "44444444-4444-4444-8444-444444444444", reason: "Check the tree and the latest exports",
    cwd: "/home/u/dev/tasks", command: COMMAND, shell: true, paths: [], diffs: [], outsideSandbox: false,
    availableDecisions: ["accept", "decline", "cancel"] },
});

// The request for a shell call that Antigravity's auto mode asks to run outside its sandbox (BypassSandbox).
const outsideRequest = () => ({
  id: ID, request_id: 9, method: "session/request_permission", provider: "gemini",
  params: { threadId: "44444444-4444-4444-8444-444444444444", reason: "Delete the old exports",
    cwd: "/home/u/dev/tasks", command: "rm -rf /home/u/old-exports", shell: true, paths: [], diffs: [],
    outsideSandbox: true, availableDecisions: ["accept", "decline", "cancel"] },
});

// The request as acp_agent.py puts it on the page for an Antigravity file edit.
const FILE = "/home/u/dev/tasks/src/tasks/store.py";
const editRequest = () => ({
  id: ID, request_id: 8, method: "session/request_permission", provider: "gemini",
  params: { threadId: "44444444-4444-4444-8444-444444444444", reason: "Edit store.py", cwd: "/home/u/dev/tasks",
    command: "", shell: false, paths: [FILE], availableDecisions: ["accept", "decline", "cancel"],
    diffs: [{ path: FILE, diff: `--- ${FILE}\n+++ ${FILE}\n@@ -40,7 +40,7 @@\n def due(task):\n     if task.done:\n`
      + "         return None\n-    return task.start + task.length\n+    return task.start + task.length + task.slack\n"
      + "\n\n def overdue(tasks, now):\n" }] },
});

let failures = 0;
function check(ok, words) {
  console.log(`${ok ? "ok  " : "FAIL"} ${words}`);
  if (!ok) failures += 1;
}

async function main() {
  const work = workFolder("cockpit-gemini-card-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser;
  try {
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const info = await harnessUp(work, server);
    for (const [kind, request, name] of [["shell", shellRequest, "gemini-card"], ["edit", editRequest, "gemini-edit"],
      ["outside", outsideRequest, "gemini-outside"]])
    for (const [width, scheme] of [[1280, "light"], [390, "dark"]]) {
      const at = `${kind} ${width} px ${scheme}`;
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      await context.route("**/api/state", async (route) => {
        const full = await fullState(route).catch(() => null);
        if (!full) return;
        const state = await full.response.json();
        await route.fulfill({ ...full, json: { ...state, codex_requests: [request()], away: null } }).catch(() => {});
      });
      await context.route("**/api/listed", async (route) => {
        const response = await route.fetch().catch(() => null);
        if (!response) return;
        const listed = await response.json();
        await route.fulfill({ response, json: { ...listed, codex_requests: [ID] } }).catch(() => {});
      });
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(String(error)));
      await page.goto(`${info.origin}/`, { waitUntil: "load", timeout: 120e3 });
      await page.waitForSelector(`#sidebar li.warn-row[data-codex="${ID}"]`, { state: "attached", timeout: 60e3 });
      if (width < 600) await page.evaluate(() => setPhoneScreen(true, false));
      await page.locator(`#sidebar li.warn-row[data-codex="${ID}"] .side-row`).click();
      await page.waitForSelector(`#next [data-codex="${ID}"]`, { timeout: 15e3 });
      await sleep(400);
      const card = await page.evaluate((key) => {
        const el = [...document.querySelectorAll(`#next [data-codex="${key}"]`)].find((each) => !each.closest(".motion-ghost, .close-copy"));
        const calls = [...el.querySelectorAll("pre.call")];
        return { text: el.textContent, calls: calls.length, call: calls[0].textContent,
          lines: calls[0].textContent.split("\n").length, coloured: el.querySelectorAll(".sh").length,
          added: [...el.querySelectorAll(".diff-add")].map((each) => each.textContent),
          removed: [...el.querySelectorAll(".diff-del")].map((each) => each.textContent) };
      }, ID);
      const once = (words) => card.text.split(words).length - 1;
      check(!card.text.includes("{") && !card.text.includes('"command"'), `${at}: no JSON on the card`);
      if (kind === "shell") {
        check(card.text.includes("Check the tree and the latest exports"), `${at}: the card says what the call is for`);
        check(card.lines === 3, `${at}: the command shows one part per line ${JSON.stringify(card.call)}`);
        check(card.coloured > 0, `${at}: the command's words are coloured`);
        check(once("tmp/tasks-export") === 1, `${at}: the command shows once`);
        check(card.text.includes("Runs in /home/u/dev/tasks") && !card.text.includes("sandbox"), `${at}: the folder shows`);
      } else if (kind === "outside") {
        check(card.text.includes("Delete the old exports"), `${at}: the card says what the call is for`);
        check(card.text.includes("Runs outside Gemini's sandbox, in /home/u/dev/tasks"), `${at}: the card says it leaves the sandbox`);
      } else {
        check(card.text.includes("Edit store.py"), `${at}: the card says what the call does`);
        check(card.calls === 1 && card.call.startsWith(FILE) && once(FILE) === 1, `${at}: the diff alone, under its file`);
        check(card.added.length === 1 && card.added[0].includes("task.slack"), `${at}: the added line is marked`);
        check(card.removed.length === 1 && !card.removed[0].includes("slack"), `${at}: the removed line is marked`);
        check(once("@@ -40,7 +40,7 @@") === 1, `${at}: the diff names its lines`);
      }
      await page.screenshot({ path: path.join(work, `${name}-${width}.png`) });
      const fits = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
      check(fits, `${at}: nothing is wider than the window`);
      check(errors.length === 0, `${at}: no page errors ${JSON.stringify(errors)}`);
      await context.close();
    }
    console.log(`screenshots in ${work}`);
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
