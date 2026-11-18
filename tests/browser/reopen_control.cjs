// A finished task has a Reopen control (SPEC CONTROL-33). For each of desktop light, desktop dark and a phone width,
// picks a task in Finished this week and checks:
// - its card shows Reopen beside Close, and an open task's card shows no Reopen;
// - at Reopen, the task leaves Finished this week and a notice names it;
// - the server has it in Busy at stage building, and Cockpit's message to its session says it was reopened;
// - the page logs no errors.
// Saves screenshots in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a copy of the live database; README.md says what it needs; nothing
// reaches a real session) and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/reopen_control.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = path.join(os.homedir(), ".npm/_npx/abc93a61346c2031/node_modules/playwright");
const CHROMIUM = path.join(os.homedir(), ".cache/ms-playwright/chromium-1194/chrome-linux/chrome");
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
  const work = workFolder("cockpit-reopen-control-");
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
    const done = "#sidebar details.sidebar-group.done";
    const used = new Set();
    for (const look of LOOKS) {
      const state = await readState();
      const target = state.items.find((item) => item.group === "done" && item.stage === "done" && !item.archived && !used.has(item.id));
      if (!target) throw new Error("no finished task left in the copy");
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
      page.setDefaultTimeout(8000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      const say = (name, ok, detail) => check(`${look.name}: ${name}`, ok, detail);

      await page.goto(`${info.origin}/`);
      await page.locator(done).waitFor({ state: "attached", timeout: 30e3 });
      if (look.phone) await page.locator(".phone-back").first().click();
      if (!(await page.locator(done).evaluate((el) => el.open))) await page.locator(`${done} > summary`).click();
      await page.locator(`${done} li[data-item="${target.id}"] .side-row`).first().click();
      const card = page.locator(`.item-card[data-item="${target.id}"]`);
      const reopen = card.locator(".button.reopen-task");
      await reopen.waitFor({ state: "visible" });
      const beside = await reopen.evaluate((button) => {
        const close = button.nextElementSibling;
        if (!close?.classList.contains("close-item")) return "not next to Close";
        const [a, b] = [button.getBoundingClientRect(), close.getBoundingClientRect()];
        return Math.abs(a.top - b.top) < 2 && b.left > a.right ? true : "not on one line";
      });
      say("the finished task's card shows Reopen just before Close, on one line", beside === true, beside);
      await reopen.scrollIntoViewIfNeeded();
      await page.screenshot({ path: path.join(work, `${look.name}-before.png`) });
      await card.locator(".item-actions").screenshot({ path: path.join(work, `${look.name}-controls.png`) });

      const answered = page.waitForResponse((response) => response.url().endsWith(`/api/items/${target.id}/take-up`));
      await reopen.click();
      await sleep(300);
      say("the task left Finished this week", (await page.locator(`${done} li[data-item="${target.id}"]`).count()) === 0, "still listed");
      const notice = await page.locator("#toast, .toast").allInnerTexts();
      say("a notice names the task", notice.some((text) => text.includes("Reopened") && text.includes(target.title)), notice.join(" | "));
      await page.screenshot({ path: path.join(work, `${look.name}-after.png`) });

      const after = (await waitFor(readState, (now) => now.items.find((item) => item.id === target.id)?.group === "busy", 10e3))
        .items.find((item) => item.id === target.id);
      say("the server has it in Busy at stage building", after?.group === "busy" && after?.stage === "building",
        JSON.stringify({ group: after?.group, stage: after?.stage }));
      const { send } = await (await answered).json();
      say("Cockpit told its session it was reopened",
        send?.author === "cockpit" && send?.session_id === WORKING_SESSION && /reopened item/.test(send?.text || ""), JSON.stringify(send));
      await sleep(1500);
      const stillReopen = await page.locator(`.item-card[data-item="${target.id}"] .button.reopen-task`).count();
      say("the open task's card shows no Reopen", stillReopen === 0, `${stillReopen} Reopen buttons`);
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
