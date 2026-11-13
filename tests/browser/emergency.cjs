// The emergency session (SPEC SESSION-21): one option in the menu at the top left opens a minimal page that keeps
// working when the main page breaks. Checks: - the top-left menu on the Cockpit page offers "Emergency session",
// which opens /emergency; - with the main page's script failing to load, the menu still opens and the option still
// leads there; - the page lists both accounts with their letters (P, W), draws the effort as lit bars, sends a
// message on the   account picked, and shows Claude's reply, its call and the refused call; the account picked stays
// picked   after a reload; - the list at the left of the Cockpit page then shows the session, and its row opens
// /emergency; - the × on that row closes the session and takes it out of the list, and so does Close on the page;
// when Cockpit   answers that Claude is still answering after all, the × asks before it ends that answer; - at 1440
// px light and 390 px dark there is no sideways scroll, and there are no page errors.  Starts serve_preview.py (this
// checkout's Cockpit on a synthetic database; README.md says what it needs) with a fake `claude` first on
// PATH, and drives a headless Chromium. Prints one line per check and exits 1 when one failed. Screenshots go to the
// folder given as the first argument, when one is given.  Usage, from the checkout: node tests/browser/emergency.cjs
// [screenshot folder]

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
// Answers the way `claude -p --output-format stream-json --verbose` does, after a second, naming its account.
const FAKE_CLAUDE = `#!/bin/sh
cat > /dev/null
sleep 1
echo '{"type":"system","subtype":"init"}'
echo '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"systemctl --user restart cockpit"}}]}}'
echo '{"type":"user","message":{"content":[{"type":"tool_result","is_error":true,"content":"auto mode blocked this call"}]}}'
echo "{\\"type\\":\\"assistant\\",\\"message\\":{\\"content\\":[{\\"type\\":\\"text\\",\\"text\\":\\"Answered from \${CLAUDE_CONFIG_DIR:-the default folder}.\\"}]}}"
echo '{"type":"result","subtype":"success","is_error":false,"result":"done"}'
`;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const shots = process.argv[2] || null;
  if (shots) fs.mkdirSync(shots, { recursive: true });
  const work = workFolder("cockpit-emergency-");
  const bin = workFolder("cockpit-emergency-bin-");
  fs.writeFileSync(path.join(bin, "claude"), FAKE_CLAUDE, { mode: 0o755 });
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], {
    stdio: ["ignore", "inherit", "inherit"],
    env: { ...process.env, PATH: `${bin}:${process.env.PATH}` },
  });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const errors = [];
    const open = async (options) => {
      const context = await browser.newContext(options);
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
      return { context, page };
    };

    // The menu at the top left, on the working page.
    let { context, page } = await open({ viewport: { width: 1440, height: 900 } });
    await page.goto(`${info.origin}/`);
    await page.click(".brand-menu > summary");
    const option = page.locator(".brand-menu .menu-list a[href='/emergency']");
    check("the top-left menu offers Emergency session", await option.isVisible(), "the option is hidden");
    check("its words say what it is", (await option.innerText()).trim() === "Emergency session", await option.innerText());
    if (shots) await page.screenshot({ path: path.join(shots, "menu-1440-light.png") });
    await option.click();
    await page.waitForURL(`${info.origin}/emergency`);
    check("the option opens the emergency page", page.url().endsWith("/emergency"), page.url());
    await context.close();

    // The same with the main page's script broken.
    ({ context, page } = await open({ viewport: { width: 1440, height: 900 } }));
    await page.route("**/static/app.js*", (route) => route.abort());
    await page.goto(`${info.origin}/`);
    await page.click(".brand-menu > summary");
    const brokenOption = page.locator(".brand-menu .menu-list a[href='/emergency']");
    check("with app.js failing, the menu still offers it", await brokenOption.isVisible(), "the option is hidden");
    await brokenOption.click();
    await page.waitForURL(`${info.origin}/emergency`);
    check("and it still opens the emergency page", page.url().endsWith("/emergency"), page.url());
    await context.close();
    errors.length = 0;  // the aborted script's own load error

    // The page itself, light at 1440 px. Send before Cockpit's first answer keeps the words and says why.
    ({ context, page } = await open({ viewport: { width: 1440, height: 900 }, colorScheme: "light" }));
    const held = [];
    await page.route("**/api/emergency", (route) => { held.push(route); });
    await page.goto(`${info.origin}/emergency`);
    await page.fill("#text", "Too early");
    await page.press("#text", "Control+Enter");
    const early = { kept: await page.inputValue("#text"), said: await page.innerText("#status").catch(() => "") };
    check("Send before the first read keeps the words and says why", early.kept === "Too early" && /not answered yet/.test(early.said),
      JSON.stringify(early));
    await page.unroute("**/api/emergency");
    for (const route of held) await route.continue().catch(() => {});
    await page.fill("#text", "");
    await page.reload();
    await page.waitForSelector("#accounts input");
    const names = await page.$$eval("#accounts input", (inputs) => inputs.map((input) => input.value));
    check("both accounts are offered", JSON.stringify(names) === '["personal","work"]', JSON.stringify(names));
    await page.check("#accounts input[value='work']");
    check("with Codex off, no agent choice shows", await page.isHidden("#agents"), "the agent choice shows");
    const models = await page.$$eval("#model option", (options) => options.map((option) => option.textContent));
    check("the Claude models are offered, Opus first as the default",
      models[0] === "Opus 5.5 (default)" && models.includes("Sonnet 5.5"), JSON.stringify(models));
    await page.selectOption("#model", "claude-sonnet-5-5");
    const letters = await page.$$eval("#accounts label", (labels) => labels.map((label) => label.querySelector(".account-letter")?.textContent));
    check("each account shows its letter, P for personal and W for work", JSON.stringify(letters) === '["P","W"]', JSON.stringify(letters));
    const lit = () => page.$$eval("#effort-bars rect:not([opacity])", (bars) => bars.length);
    check("the effort's bars show high, the default, as three lit", await lit() === 3, String(await lit()));
    await page.selectOption("#effort", "low");
    check("picking low lights one bar", await lit() === 1, String(await lit()));
    await page.fill("#text", "The Cockpit page shows nothing");
    await page.click("#send");
    await page.waitForSelector(".working", { timeout: 5000 });
    check("while Claude answers, Stop shows and Send waits",
      await page.isVisible("#stop") && await page.isDisabled("#send"), "the controls did not change");
    await page.waitForSelector(".says", { timeout: 15000 });
    const reply = await page.innerText(".says");
    check("the reply comes from the picked account", reply.includes(".claude-work") || reply.includes("work"), reply);
    check("the call shows", (await page.innerText(".call")).includes("systemctl --user restart cockpit"), await page.innerText(".call"));
    check("the refused call shows in red", (await page.innerText(".refused")).includes("auto mode blocked"), "no refused line");
    const red = await page.$eval(".refused", (node) => getComputedStyle(node).color);
    check("refused is red", /rgb\((1[5-9]\d|2\d\d), \d{1,2}, \d{1,2}\)/.test(red), red);
    check("the turn names the account", (await page.innerText(".who")).includes("work account"), await page.innerText(".who"));
    check("the turn names the model and effort picked",
      (await page.innerText(".who")).includes("claude-sonnet-5-5 at low effort"), await page.innerText(".who"));
    check("the message box emptied", (await page.inputValue("#text")) === "", await page.inputValue("#text"));
    await page.waitForSelector("#send:not([disabled])");
    check("the session is named", (await page.innerText("#where")).startsWith("Claude session "),await page.innerText("#where"));
    if (shots) await page.screenshot({ path: path.join(shots, "emergency-1440-light.png"), fullPage: true });
    await page.reload();
    await page.waitForSelector("#accounts input");
    check("the picked account stays picked after a reload",
      await page.isChecked("#accounts input[value='work']"), "personal is picked");
    check("the picked model and effort stay picked after a reload",
      await page.inputValue("#model") === "claude-sonnet-5-5" && await page.inputValue("#effort") === "low",
      `${await page.inputValue("#model")} ${await page.inputValue("#effort")}`);
    // A model Cockpit stops offering leaves the list, and the pick goes back to the default.
    const removed = await page.evaluate(() => {
      const view = JSON.parse(JSON.stringify(lastView));
      view.options.claude.models = view.options.claude.models.filter((model) => model.name !== "claude-sonnet-5-5");
      render(view);
      return { listed: [...document.querySelectorAll("#model option")].map((option) => option.value), picked: document.getElementById("model").value };
    });
    check("a model no longer offered leaves the list and the pick goes back to the default",
      !removed.listed.includes("claude-sonnet-5-5") && removed.picked === "claude-opus-5-5", JSON.stringify(removed));
    check("no sideways scroll at 1440 px",
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), "it scrolls sideways");
    await context.close();

    // The main page's list at the left, once the session has a message.
    ({ context, page } = await open({ viewport: { width: 1440, height: 900 } }));
    await page.goto(`${info.origin}/`);
    const row = page.locator("#sidebar .sidebar-group.emergency a.side-row[href='/emergency']");
    await row.waitFor({ timeout: 15000 }).catch(() => {});
    check("the list at the left shows the emergency session", await row.isVisible(), "no row");
    // The row shows only its name, as every row in the list does; its account is in its title.
    const rowText = await row.innerText().catch(() => "");
    const rowTitle = (await row.getAttribute("data-tip").catch(() => "")) || "";
    check("its row shows the last message, and its title names its account",
      rowText.includes("The Cockpit page shows nothing") && rowTitle.includes("Work account"), `${rowText} | ${rowTitle}`);
    const rowGround = await row.evaluate((node) => getComputedStyle(node).backgroundColor);
    const head = await page.$eval("#sidebar .sidebar-group.emergency > summary", (node) => getComputedStyle(node).color);
    const [failColor, failGround] = await page.evaluate(() => {
      const probe = document.body.appendChild(document.createElement("span"));
      probe.style.color = "var(--fail)";
      probe.style.backgroundColor = "var(--fail-bg)";
      const found = [getComputedStyle(probe).color, getComputedStyle(probe).backgroundColor];
      probe.remove();
      return found;
    });
    check("the emergency group stands out in red", head === failColor && rowGround === failGround, `${head} on ${rowGround}`);
    if (shots) await page.screenshot({ path: path.join(shots, "sidebar-1440-light.png") });
    await row.click();
    await page.waitForURL(`${info.origin}/emergency`);
    check("the row opens the emergency page", page.url().endsWith("/emergency"), page.url());
    await context.close();

    // Dark at 390 px.
    ({ context, page } = await open({ viewport: { width: 390, height: 844 }, colorScheme: "dark" }));
    await page.goto(`${info.origin}/emergency`);
    await page.waitForSelector(".says");
    check("no sideways scroll at 390 px",
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), "it scrolls sideways");
    const ground = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    check("the dark look has a dark ground", ground === "rgb(20, 18, 16)", ground);
    if (shots) await page.screenshot({ path: path.join(shots, "emergency-390-dark.png"), fullPage: true });
    await context.close();

    // Close: the × on the list's row, then the page's own Close.
    ({ context, page } = await open({ viewport: { width: 1440, height: 900 } }));
    let asked = 0;
    page.on("dialog", (dialog) => { asked += 1; dialog.accept(); });
    // Cockpit refuses the first close as if an answer had started since the list last read the session.
    const closes = [];
    await page.route("**/api/emergency/close", (route) => {
      closes.push(route.request().postDataJSON());
      if (closes.length > 1) return route.continue();
      return route.fulfill({ status: 409, contentType: "application/json",
        body: JSON.stringify({ detail: "Claude is still answering in the emergency session" }) });
    });
    await page.goto(`${info.origin}/`);
    const closeKey = page.locator("#sidebar .emergency-row .emergency-close");
    await closeKey.waitFor({ timeout: 15000 }).catch(() => {});
    check("the row in the list has a close key", await closeKey.isVisible(), "no close key");
    await closeKey.click();
    const gone = await page.locator("#sidebar .sidebar-group.emergency").waitFor({ state: "detached", timeout: 15000 })
      .then(() => true, () => false);
    check("closing takes the emergency session out of the list", gone, "the group is still there");
    check("an answer the list did not know of is ended only after asking",
      asked === 1 && closes.length === 2 && closes[0].end_answer === false && closes[1].end_answer === true,
      `asked ${asked} times; sent ${JSON.stringify(closes)}`);
    await page.unroute("**/api/emergency/close");
    // The browser logs the refusal it was handed as a failed load; that one is expected.
    const refusal = errors.findIndex((text) => text.includes("status of 409"));
    if (refusal >= 0) errors.splice(refusal, 1);
    await page.goto(`${info.origin}/emergency`);
    await page.waitForSelector("#accounts input");
    check("the closed session is gone from its page too",
      (await page.innerText("#where")).startsWith("No session is open"), await page.innerText("#where"));
    check("Close waits for a session to close", await page.isDisabled("#close"), "Close is enabled");
    await page.fill("#text", "Second try");
    await page.click("#send");
    await page.waitForSelector(".says", { timeout: 15000 });
    await page.waitForSelector("#close:not([disabled])");
    await page.click("#close");
    await page.waitForFunction(() => document.getElementById("where").textContent.startsWith("No session is open"), null, { timeout: 15000 })
      .catch(() => {});
    check("the page's Close closes the session", (await page.innerText("#where")).startsWith("No session is open")
      && (await page.locator(".turn").count()) === 0, await page.innerText("#where"));
    await context.close();

    check("no page errors", errors.length === 0, errors.join(" | "));
  } finally {
    if (browser) await browser.close();
    server.kill();
    await sleep(200);
  }
  const failed = results.filter((result) => !result.ok);
  console.error(`${results.length - failed.length} of ${results.length} checks passed`);
  if (failed.length) process.exitCode = 1;
}

// The harness's cookie file, once it is written and the server answers.
async function harnessUp(work, server) {
  const file = path.join(work, "cookie.json");
  const end = Date.now() + 120e3;
  while (Date.now() < end) {
    if (server.exitCode !== null) throw new Error(`serve_preview.py exited with ${server.exitCode}`);
    if (fs.existsSync(file)) {
      const info = JSON.parse(fs.readFileSync(file, "utf8"));
      const up = await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false);
      if (up) return info;
    }
    await sleep(200);
  }
  throw new Error("serve_preview.py did not come up within 120 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
