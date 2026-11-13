// Isolated integration checks: no live data or real agent messages.
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");
const { spawn } = require("node:child_process");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "../..");
const work = workFolder("cockpit-vim-");
const server = spawn(path.join(root, ".venv/bin/python"), [path.join(__dirname, "serve_keyboard.py"), work], {
  cwd: root, stdio: ["ignore", "pipe", "pipe"],
});
let serverLog = "";
server.stdout.on("data", (data) => { serverLog += data; });
server.stderr.on("data", (data) => { serverLog += data; });
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let browser, page;

(async () => {
  const runtime = path.join(work, "runtime.json");
  for (let at = 0; !fs.existsSync(runtime); at++) {
    if (at > 100 || server.exitCode !== null) throw new Error(`Fixture failed: ${serverLog}`);
    await delay(100);
  }
  const { origin, cookie } = JSON.parse(fs.readFileSync(runtime, "utf8"));
  for (let at = 0; ; at++) {
    try { if ((await fetch(origin + "/healthz")).ok) break; } catch {}
    if (at > 50) throw new Error(`Fixture did not listen: ${serverLog}`);
    await delay(100);
  }
  const executablePath = process.env.BROWSER_EXECUTABLE;
  browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  const context = await browser.newContext({ extraHTTPHeaders: { Cookie: cookie }, viewport: { width: 1280, height: 900 } });
  page = await context.newPage();
  page.setDefaultTimeout(8000);
  const errors = [], csp = [], sends = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => { if (/Content Security Policy|violates.*directive/i.test(message.text())) csp.push(message.text()); });
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().endsWith("/api/messages")) sends.push(request.postDataJSON());
  });
  await page.goto(origin + "/classic");
  await page.locator("#next [data-step]").waitFor();
  const mod = await page.evaluate(() => /Mac|iPhone|iPad/.test(navigator.platform)) ? "Meta" : "Control";
  const press = (key) => page.keyboard.press(key);
  const search = page.getByRole("combobox", { name: "Search commands, work, and sessions" });
  async function command(text) {
    await press(`${mod}+k`); await search.fill(text); await press("Enter");
  }
  async function startComposer() {
    await command("Start a new agent session");
    await page.locator('#work textarea[data-key="msg:new-session"]').waitFor({ state: "attached" });
  }
  await startComposer();
  const area = page.locator('#work textarea[data-key="msg:new-session"]');
  const editor = page.locator("#work .cm-content");
  const status = page.locator("#work .cm-vim-panel");
  assert.equal(await page.locator(".cm-editor").count(), 0);
  await area.fill("alpha beta gamma");
  await command("Enable Vim editing");
  await editor.waitFor();
  assert.equal(await area.inputValue(), "alpha beta gamma");
  assert.equal(await area.isVisible(), false);
  assert.match(await status.innerText(), /NORMAL/);
  console.log("PASS Vim is opt-in; enabling preserves an existing draft");

  await editor.focus();
  await press("g"); await press("g"); await press("0");
  await press("d"); await press("w");
  assert.equal(await area.inputValue(), "beta gamma");
  await press("u");
  assert.equal(await area.inputValue(), "alpha beta gamma");
  await press("v"); await press("e"); await press("x");
  assert.equal(await area.inputValue(), " beta gamma");
  await press("u");
  await press("A"); await page.keyboard.type(" delta");
  assert.match(await status.innerText(), /INSERT/);
  await press("Escape");
  assert.match(await status.innerText(), /NORMAL/);
  await press("/"); await page.keyboard.type("beta");
  await page.evaluate(() => render(false));
  assert.equal(await page.locator("#work .cm-vim-panel input").evaluate((node) => node === document.activeElement), true);
  await press("Enter");
  assert.equal(await page.locator(".command-dialog[open]").count(), 0);
  await press("?"); await page.keyboard.type("alpha"); await press("Enter");
  assert.equal(await page.locator("dialog[open]").count(), 0);
  await press("Escape");
  assert.equal(sends.length, 0);
  console.log("PASS Vim motions, operators, Visual mode, undo, Insert/Escape and search stay in the editor");

  const active = await editor.elementHandle();
  await page.evaluate(() => render(false));
  assert.equal(await editor.evaluate((node) => node === document.activeElement), true);
  assert.equal(await editor.evaluate((node, before) => node === before, active), true);
  assert.equal(await area.inputValue(), "alpha beta gamma delta");
  await press("u");
  assert.equal(await area.inputValue(), "alpha beta gamma");
  await press("Control+r");
  assert.equal(await area.inputValue(), "alpha beta gamma delta");
  const changed = await fetch(origin + "/api/items/2", {
    method: "PATCH", headers: { Authorization: `Bearer ${"t".repeat(40)}`, "X-Cockpit-As": "vim-test",
      "Content-Type": "application/json" }, body: JSON.stringify({ summary: "Changed during prompt editing" }),
  });
  assert.equal(changed.ok, true);
  await page.waitForFunction(() => ui.state.items.some((item) => item.summary === "Changed during prompt editing"));
  assert.equal(await editor.evaluate((node, before) => node === before, active), true);
  await press(`${mod}+k`); await press("Escape");
  assert.equal(await editor.evaluate((node) => node === document.activeElement), true);
  await press("Tab");
  assert.equal(await editor.evaluate((node) => node === document.activeElement), false);
  console.log("PASS Cursor, mode, undo and focus survive action/poll redraws; Cmd+K returns focus and Tab exits");

  await page.reload(); await page.locator("#next [data-step]").waitFor({ state: "attached" });
  await startComposer();
  await editor.waitFor();
  assert.equal(await area.inputValue(), "alpha beta gamma delta");
  await page.locator(".brand-menu > summary").click();
  const toggle = page.getByRole("switch", { name: "Vim editing", exact: true });
  assert.equal(await toggle.getAttribute("aria-checked"), "true");
  await toggle.click();
  assert.equal(await toggle.evaluate((node) => node === document.activeElement), true);
  assert.equal(await toggle.getAttribute("aria-checked"), "false");
  assert.equal(await page.locator(".cm-editor").count(), 0);
  assert.equal(await area.inputValue(), "alpha beta gamma delta");
  await page.locator(".brand-menu > summary").click();
  await command("Enable Vim editing");
  console.log("PASS Preference and drafts survive reload; settings switch restores the native field without losing text");

  const second = await context.newPage();
  await second.goto(origin + "/classic"); await second.locator("#next [data-step]").waitFor();
  await command("Disable Vim editing");
  await second.waitForFunction(() => !CockpitEditor.get());
  await command("Enable Vim editing");
  await second.waitForFunction(() => CockpitEditor.get());
  await second.close();
  console.log("PASS Vim preference synchronizes across tabs");

  await editor.focus();
  await press("i"); await page.keyboard.type(" more");
  const text = await area.inputValue();
  const beforeIme = sends.length;
  // A composition that has started but not yet changed the text: only the platform's modifier is held.
  await editor.dispatchEvent("compositionstart", { data: "" });
  await editor.dispatchEvent("keydown", { key: "Enter", code: "Enter", [mod === "Meta" ? "metaKey" : "ctrlKey"]: true });
  await editor.dispatchEvent("compositionend", { data: "" });
  await page.waitForTimeout(100);
  assert.equal(sends.length, beforeIme);
  const response = page.waitForResponse((response) => response.request().method() === "POST" && response.url().endsWith("/api/messages"));
  await press(`${mod}+Enter`);
  assert.equal((await response).ok(), true);
  assert.equal(sends.length, 1);
  assert.equal(sends[0].text, text);
  console.log("PASS Cmd/Ctrl+Enter sends the full edited prompt once; IME composition cannot send");

  await startComposer();
  await editor.focus(); await press("i"); await page.keyboard.type("Test prompt after delivery"); await press("Escape");
  await page.emulateMedia({ reducedMotion: "reduce", colorScheme: "dark" });
  await page.screenshot({ path: path.join(work, "vim-editor-dark.png") });
  await page.emulateMedia({ colorScheme: "light" });
  await page.setViewportSize({ width: 390, height: 844 });
  await editor.scrollIntoViewIfNeeded();
  assert.equal(await editor.evaluate((node) => node.getBoundingClientRect().right <= innerWidth), true);
  await page.screenshot({ path: path.join(work, "vim-editor-mobile.png") });
  await page.route("**/api/messages", (route) => route.fulfill({ status: 409,
    contentType: "application/json", body: JSON.stringify({ detail: "Test delivery refusal" }) }));
  await editor.focus();
  const failedText = await area.inputValue();
  const refused = page.waitForResponse((response) => response.status() === 409 && response.url().endsWith("/api/messages"));
  await press(`${mod}+Enter`);
  await refused;
  await page.waitForFunction((text) => document.querySelector('#work textarea[data-key="msg:new-session"]')?.value === text, failedText);
  assert.equal(await editor.isVisible(), true);
  await page.unroute("**/api/messages");
  console.log("PASS Sending from Normal mode uses the same delivery path; a refused send restores the edited draft");

  const blocked = await context.newPage();
  await blocked.addInitScript(() => {
    Storage.prototype.getItem = () => { throw new Error("Storage is blocked"); };
    Storage.prototype.setItem = () => { throw new Error("Storage is blocked"); };
  });
  await blocked.goto(origin + "/classic");
  await blocked.locator("#next [data-step]").waitFor();
  assert.equal(await blocked.evaluate(() => CockpitEditor.get()), false);
  await blocked.keyboard.press(`${mod}+k`);
  await blocked.getByRole("combobox", { name: "Search commands, work, and sessions" }).fill("Enable Vim editing");
  await blocked.keyboard.press("Enter");
  assert.equal(await blocked.evaluate(() => CockpitEditor.get()), true);
  await blocked.close();
  console.log("PASS Blocked browser storage still allows Vim editing for the current page");

  assert.deepEqual(errors, []);
  assert.deepEqual(csp, []);
  console.log(`PASS Dark/light/mobile layout, no CSP violations or browser errors; screenshots in ${work}`);
})().catch(async (error) => {
  console.error(error);
  if (page) {
    console.error(await page.evaluate(() => ({ active: document.activeElement?.outerHTML.slice(0, 500),
      fields: [...document.querySelectorAll("#work textarea")].map((n) => ({ key: n.dataset.key, value: n.value })),
      status: document.querySelector("#work .cm-vim-panel")?.textContent })));
    await page.screenshot({ path: path.join(work, "failure.png") });
    console.error(`Screenshot: ${path.join(work, "failure.png")}`);
  }
  process.exitCode = 1;
}).finally(async () => {
  await browser?.close(); server.kill();
});
