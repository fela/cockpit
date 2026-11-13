// Signing a signed-out Claude account in again from the page (SPEC UI-37, UI-40), at 1280 px in the light look and 390
// px in the dark: the banner at the top names the signed-out account and explains the effect on its sessions; the
// sign-in key opens Claude's page in a new tab, and the banner itself takes the code (SPEC UI-53), with no dialog over
// the page: its code field has the focus, has it again when the user comes back to the tab, and keeps it through the
// page's refreshes; a wrong code is refused with what Claude said and the form goes; the right one signs the account
// in, says so, and the banner goes. Nothing is wider than the window.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs), whose
// `claude` is tests/fake_claude.py, clears the test account's sign-in the way Claude Code does, and drives a headless
// Chromium. Prints one line per check, saves the banner at each width as claude-sign-in-<width>.png in the work folder,
// the banner with its code field as claude-code-<width>.png, and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/claude_sign_in.cjs

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
// tests/fake_claude.py's code that signs in.
const GOOD_CODE = "good-code#state";
// The code field on the work account's banner.
const FIELD = "sign-in:claude:work";
const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let failures = 0;

function check(ok, words) {
  console.log(`${ok ? "ok  " : "FAIL"} ${words}`);
  if (!ok) failures += 1;
}

// What Claude Code leaves when it fails to renew an account's access for good: blank tokens, the old expiry.
function signOut(work) {
  const oauth = { accessToken: "", refreshToken: "", expiresAt: 0, refreshTokenExpiresAt: Date.now() + 25 * 86400e3 };
  fs.writeFileSync(path.join(work, "accounts", "work", ".credentials.json"), JSON.stringify({ claudeAiOauth: oauth }));
}

async function main() {
  const work = workFolder("cockpit-claude-sign-in-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser;
  try {
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const info = await harnessUp(work, server);
    for (const [width, scheme] of [[1280, "light"], [390, "dark"]]) {
      signOut(work);
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      // Claude's page is not loaded for real: the new tab gets an empty answer.
      await context.route("https://claude.com/**", (route) => route.fulfill({ body: "Claude's page" }));
      context.setDefaultTimeout(90e3);
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(String(error)));
      await page.goto(`${info.origin}/classic`, { waitUntil: "networkidle" });
      const at = `${width} px ${scheme}`;
      const banner = page.locator("#banner .warn-card").first();
      await page.waitForSelector("#banner .warn-card.fail", { timeout: 90e3 });
      // The fixture may hold the inactivity question, which opens over the page: Return answers it.
      // The page asks it on a one-second timer, so the check waits past a few of its rounds.
      const idle = page.locator("dialog#idle[open]");
      await page.waitForTimeout(3000);
      if (await idle.count()) {
        await idle.locator("button", { hasText: "Return" }).click();
        await page.waitForSelector("dialog#idle:not([open])", { state: "attached" });
      }
      const words = await banner.textContent();
      check(words.startsWith("The work Claude account is signed out: its sessions answer \"Login expired\""), `${at}: the top banner says the work account is signed out (${words})`);
      check(words.includes("new sessions go to the other account"), `${at}: it says where new sessions go`);
      await page.locator("#banner").screenshot({ path: path.join(work, `claude-sign-in-${width}.png`) });
      const [tab] = await Promise.all([context.waitForEvent("page"), banner.locator("button", { hasText: "Sign in to work" }).click()]);
      await page.waitForSelector(`[data-key="${FIELD}"]`, { timeout: 90e3 });
      await tab.waitForLoadState();
      check(tab.url().startsWith("https://claude.com/cai/oauth/authorize"), `${at}: the new tab opens Claude's page (${tab.url()})`);
      check(await page.evaluate((field) => Boolean(document.querySelector(`#banner .warn-card [data-key="${field}"]`)) && !document.querySelector("dialog[open]"), FIELD),
        `${at}: the banner asks for the code, with no dialog over the page`);
      check(await codeFocused(page, FIELD), `${at}: the code field has the focus`);
      await page.evaluate(() => { document.activeElement.blur(); window.dispatchEvent(new Event("focus")); });
      check(await codeFocused(page, FIELD), `${at}: back on Cockpit's tab, the code field has the focus again`);
      await page.waitForTimeout(6000);
      check(await codeFocused(page, FIELD), `${at}: the page's refreshes leave the focus in the code field`);
      await page.screenshot({ path: path.join(work, `claude-code-${width}.png`) });
      let wide = await overflow(page);
      check(wide.px <= 0, `${at}: with the code box, nothing wider than the window (${wide.px} px over${wide.by})`);
      await page.fill(`[data-key="${FIELD}"]`, "wrong-code");
      await page.keyboard.press("Enter");
      await page.waitForFunction(() => [...document.querySelectorAll(".toast")].some((el) => el.textContent.includes("did not sign")), null, { timeout: 90e3 }).catch(() => {});
      const refusal = await page.evaluate(() => [...document.querySelectorAll(".toast")].map((el) => el.textContent).find((words) => words.includes("did not sign")) || "no refusal shown");
      check(refusal.includes("Request failed with status code 400"), `${at}: a wrong code says what Claude said (${refusal})`);
      await page.waitForFunction((field) => !document.querySelector(`[data-key="${field}"]`), FIELD).then(
        () => check(true, `${at}: after a refusal the next try starts over`),
        () => check(false, `${at}: after a refusal the next try starts over`),
      );
      await tab.close();
      const [again] = await Promise.all([context.waitForEvent("page"), page.locator("#banner button", { hasText: "Sign in to work" }).click()]);
      await page.waitForSelector(`[data-key="${FIELD}"]`, { timeout: 90e3 });
      await again.close();
      await page.fill(`[data-key="${FIELD}"]`, GOOD_CODE);
      await page.locator("#banner .sign-in-form button", { hasText: /^Sign in$/ }).click();
      await page.waitForFunction(() => [...document.querySelectorAll(".toast")].some((el) => el.textContent.startsWith("Signed in to the work account")), null, { timeout: 90e3 });
      check(true, `${at}: the right code signs the account in and says so`);
      await page.waitForFunction(() => !document.querySelector("#banner").textContent.includes("Claude account is signed out"), null, { timeout: 90e3 }).then(
        () => check(true, `${at}: the banner goes once the account is signed in`),
        () => check(false, `${at}: the banner goes once the account is signed in`),
      );
      wide = await overflow(page);
      check(wide.px <= 0, `${at}: nothing wider than the window (${wide.px} px over${wide.by})`);
      check(errors.length === 0, `${at}: no page errors ${errors.join("; ")}`);
      await context.close();
    }
  } finally {
    try {
      if (browser) await browser.close();
    } finally {
      server.kill();
      await new Promise((resolve) => (server.exitCode !== null || server.signalCode !== null ? resolve() : server.once("exit", resolve)));
    }
  }
  console.error(`${failures} failed; work dir ${work}`);
  process.exitCode = failures ? 1 : 0;
}

async function codeFocused(page, key) {
  return page.evaluate((key) => document.activeElement && document.activeElement.dataset.key === key, key);
}

// How far the page reaches past the window's right edge, and the innermost element that reaches furthest.
async function overflow(page) {
  return page.evaluate(() => {
    const px = document.documentElement.scrollWidth - window.innerWidth;
    if (px <= 0) return { px, by: "" };
    let worst = null;
    for (const el of document.body.querySelectorAll("*")) {
      const right = el.getBoundingClientRect().right;
      if (right > window.innerWidth && (!worst || right >= worst.right)) worst = { el, right };
    }
    const el = worst && worst.el;
    return { px, by: el ? `, by ${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""}.${[...el.classList].join(".")}` : "" };
  });
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
