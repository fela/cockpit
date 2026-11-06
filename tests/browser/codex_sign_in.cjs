// The Codex sign-in on the page (SPEC UI-77), at 1280 px light and 390 px dark, on scratch account folders with the
// stand-in tests/fake_codex.py: no real `codex login` runs, as one deletes the real folders' sign-ins.
//
// Checks that the hover card's Sign in again only opens a warning that says what starting does, with the sign-in file
// still there; Start shows OpenAI's link and the code in a credential field; the card closes when Codex finishes and
// says what the folder held before and after; a folder that lost its file shows a red warning; a code that ran out says
// so; another account in the folder shows both addresses and offers Keep. Saves a screenshot per state in the work folder.
//
// Usage, from the checkout: node tests/browser/codex_sign_in.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const FIRST = "first@example.com";
const SECOND = "second@example.com";
const OTHER = "other@example.com";
const CODE = "ABCD-EFGH";

let failures = 0;
function check(ok, words) {
  console.log(`${ok ? "ok  " : "FAIL"} ${words}`);
  if (!ok) failures += 1;
}

function signIn(home, address) {
  const part = (value) => Buffer.from(JSON.stringify(value)).toString("base64url");
  fs.writeFileSync(path.join(home, "auth.json"), JSON.stringify({ tokens: { id_token: `${part({ alg: "none" })}.${part({ email: address })}.x` } }));
}

async function scenario(browser, info, work, homes, width, scheme) {
  const at = `${width} px ${scheme}`;
  const [firstHome, secondHome] = homes;
  const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme });
  await context.grantPermissions(["clipboard-read", "clipboard-write"]).catch(() => {});
  await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  await page.goto(info.origin, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => typeof ui !== "undefined" && ui.state, null, { timeout: 60e3 });
  await page.waitForFunction(() => ui.state.codex_sign_ins.length === 2, null, { timeout: 30e3 });
  const refresh = () => page.evaluate(() => { ui.pendingRender = true; return refresh(); });
  const card = (folder) => page.locator(`article[data-warning="codex-sign-in:${folder}"]`);
  const shot = (name) => page.screenshot({ path: path.join(work, `${name}-${width}.png`) });
  const state = (folder) => page.evaluate((name) => ui.state.codex_sign_ins.find((entry) => entry.folder === name), folder);
  const until = async (test, words) => {
    const end = Date.now() + 20e3;
    while (Date.now() < end) {
      await refresh();
      if (await test()) return check(true, `${at}: ${words}`);
      await sleep(250);
    }
    check(false, `${at}: ${words}`);
  };

  check(await card(".codex").count() === 0, `${at}: no warning while both accounts work`);

  // The hover card's control, there with no warning. The phone has no hover, so its row presses by the same function.
  if (width > 700) {
    const key = page.locator(".usage-card .uc-codex-sign-in button").first();
    // A redraw of the pill as the first states arrive can end a hover, so it moves away and back until the card shows.
    for (let tries = 0; tries < 20 && !(await key.isVisible()); tries += 1) {
      await page.mouse.move(700, 450);
      await page.locator(".usage.caps").hover();
      await sleep(1000);
    }
    await page.screenshot({ path: path.join(work, `hover-${width}.png`) });
    check(await page.locator(".usage-card .uc-codex-sign-in button").count() === 2, `${at}: the hover card offers a sign-in per Codex account`);
    await key.click();
  } else {
    await page.evaluate(() => prepareCodexSignIn(".codex"));
  }
  await card(".codex").waitFor({ timeout: 10e3 });
  let words = await card(".codex").textContent();
  check(words.includes(FIRST) && words.includes("signs this account out at once") && words.includes("fails its turn") && words.includes("15 minutes"),
    `${at}: the warning names the account and says what starting does`);
  check(fs.existsSync(path.join(firstHome, "auth.json")), `${at}: opening the warning leaves the sign-in file alone`);
  await shot("ready");
  await card(".codex").getByRole("button", { name: "Not now" }).click();
  await until(async () => await card(".codex").count() === 0, "Not now closes it");
  check(fs.existsSync(path.join(firstHome, "auth.json")), `${at}: Not now leaves the sign-in file alone`);

  // Start: the link and code, and the file gone.
  await page.evaluate(() => prepareCodexSignIn(".codex"));
  // The click runs on the current button before a poll replaces it.
  const [started] = await Promise.all([
    page.waitForResponse((response) => response.url() === `${info.origin}/api/codex/.codex/sign-in` && response.request().method() === "POST", { timeout: 90e3 }),
    card(".codex").getByRole("button", { name: "Start sign-in" }).evaluate((button) => button.click()),
  ]);
  check(started.ok(), `${at}: Start accepts the sign-in request (${started.status()})`);
  await card(".codex").locator(".codex-code").waitFor({ timeout: 30e3 });
  check(await card(".codex").locator(".codex-code").inputValue() === CODE, `${at}: the card shows the code`);
  check(await card(".codex").locator(".codex-code").getAttribute("data-credential") !== null, `${at}: the code is marked as a credential`);
  check(await card(".codex").getByRole("link", { name: "Open OpenAI's page" }).getAttribute("href") === "https://auth.openai.com/codex/device", `${at}: the link is OpenAI's`);
  check((await card(".codex").textContent()).includes(`Sign in with ${FIRST}`), `${at}: it says which account to sign in with`);
  check(!fs.existsSync(path.join(firstHome, "auth.json")), `${at}: Start deleted the sign-in file, as Codex does`);
  await sleep(300);
  await shot("code");
  const wide = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  check(wide <= 0, `${at}: nothing wider than the window (${wide} px over)`);

  // It finishes with the same account.
  fs.writeFileSync(path.join(firstHome, "finish"), FIRST);
  await until(async () => await card(".codex").count() === 0, "the card closes when Codex finishes");
  await page.locator("#toast", { hasText: `holds ${FIRST}, as before` }).first().waitFor({ state: "attached", timeout: 10e3 }).then(
    () => check(true, `${at}: a notice says the folder holds the same account`), () => check(false, `${at}: a notice says the folder holds the same account`));
  await shot("finished");

  // The file goes without the page: a red warning.
  fs.rmSync(path.join(firstHome, "auth.json"));
  await until(async () => (await state(".codex")).problem === "signed_out" && await page.evaluate(() => allWarnings(ui.state).find((one) => one.key === "codex-sign-in:.codex")?.tone === "fail"), "a lost sign-in file raises a red warning");
  await page.evaluate(() => { if (SIDEBAR) openWarning("codex-sign-in:.codex"); });
  await card(".codex").waitFor({ timeout: 10e3 });
  check((await card(".codex").textContent()).includes(`(${FIRST}) is signed out`), `${at}: the warning names the account it held`);
  await shot("signed-out");
  await card(".codex").getByRole("button", { name: "Sign in to ~/.codex" }).click();
  await card(".codex").locator(".codex-code").waitFor({ timeout: 30e3 });
  fs.writeFileSync(path.join(firstHome, "finish"), FIRST);
  await until(async () => await card(".codex").count() === 0, "signing in ends the red warning");

  // A code left to run out on the second folder.
  await page.evaluate(() => prepareCodexSignIn(".codex-2"));
  await card(".codex-2").getByRole("button", { name: "Start sign-in" }).click();
  await card(".codex-2").locator(".codex-code").waitFor({ timeout: 30e3 });
  fs.writeFileSync(path.join(secondHome, "finish"), "fail");
  await until(async () => (await state(".codex-2")).problem === "signed_out", "a code that runs out leaves the folder signed out");
  await page.evaluate(() => { if (SIDEBAR) openWarning("codex-sign-in:.codex-2"); });
  await card(".codex-2").waitFor({ timeout: 10e3 });
  check((await card(".codex-2").textContent()).includes("timed out"), `${at}: the warning has Codex's words`);
  check(!(await card(".codex-2").textContent()).includes(CODE), `${at}: the words hold no code`);
  await shot("ran-out");

  // The wrong account.
  await card(".codex-2").getByRole("button", { name: "Sign in to ~/.codex-2" }).click();
  await card(".codex-2").locator(".codex-code").waitFor({ timeout: 30e3 });
  fs.writeFileSync(path.join(secondHome, "finish"), OTHER);
  await until(async () => (await state(".codex-2")).problem === "other_account", "another account in the folder is a problem");
  await page.evaluate(() => { if (SIDEBAR) openWarning("codex-sign-in:.codex-2"); });
  await card(".codex-2").waitFor({ timeout: 10e3 });
  words = await card(".codex-2").textContent();
  check(words.includes(OTHER) && words.includes(SECOND), `${at}: the warning shows the account before and after`);
  await shot("other-account");
  await card(".codex-2").getByRole("button", { name: `Keep ${OTHER}` }).click();
  await until(async () => (await state(".codex-2")).problem === null, "Keep takes the new account");
  check((await state(".codex-2")).held === OTHER, `${at}: the folder now counts as holding it`);

  check(errors.length === 0, `${at}: no page errors ${errors.join("; ")}`);
  await context.close();
}

async function main() {
  const work = workFolder("cockpit-codex-sign-in-");
  const scratch = workFolder("cockpit-codex-folders-");
  const browser = await chromium.launch({ executablePath: CHROMIUM });
  try {
    for (const [width, scheme] of [[1280, "light"], [390, "dark"]]) {
      const run = path.join(scratch, String(width));
      const homes = [".codex", ".codex-2"].map((name) => path.join(run, name));
      for (const home of homes) fs.mkdirSync(home, { recursive: true });
      // Before the server starts: it reads each folder's address for the usage display then.
      signIn(homes[0], FIRST);
      signIn(homes[1], SECOND);
      const folder = path.join(work, String(width));
      fs.mkdirSync(folder);
      fs.writeFileSync(path.join(folder, "codex-homes.json"), JSON.stringify(homes));
      const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), folder], { stdio: ["ignore", "inherit", "inherit"] });
      try {
        const info = await harnessUp(folder, server);
        await scenario(browser, info, work, homes, width, scheme);
      } finally {
        server.kill();
        await new Promise((resolve) => (server.exitCode !== null ? resolve() : server.once("exit", resolve)));
      }
    }
  } finally {
    await browser.close();
  }
  console.error(`${failures} failed; work dir ${work}`);
  process.exitCode = failures ? 1 : 0;
}

// The harness's cookie file, once it is written and the server answers.
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
