// Exercise the real page's incremental reads, full-state recovery and optional editor loading.
"use strict";
const assert = require("node:assert/strict");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { startPreview, fullState } = require("./work.cjs");

(async () => {
  const { info } = await startPreview("cockpit-state-sync-");
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ extraHTTPHeaders: { Cookie: `${info.name}=${info.value}` }, viewport: { width: 1280, height: 900 } });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    let editorRequests = 0;
    let releaseEditor;
    const editorGate = new Promise((resolve) => { releaseEditor = resolve; });
    await page.route("**/static/prompt-editor.js*", async (route) => {
      editorRequests++;
      await editorGate;
      await route.continue();
    });
    await page.goto(info.origin);
    await page.locator("#next [data-step]").waitFor();
    assert.equal(editorRequests, 0, "plain replies should load without the Vim bundle");
    const reads = await page.evaluate(async () => {
      while (ui.refreshing) await new Promise((resolve) => setTimeout(resolve, 20));
      const previous = stateCursor;
      const response = await readThroughRestart("/api/state", { method: "GET", credentials: "same-origin", headers: { "x-cockpit-since": previous } });
      return { base: response.response.headers.get("x-cockpit-base"), previous, changed: Object.keys(response.data.changes) };
    });
    assert.equal(reads.base, reads.previous);
    assert.ok(!reads.changed.includes("queue"), "unchanged cards should stay off the wire");
    await page.evaluate(async () => {
      stateCursor = "expired-checkpoint";
      const state = await readState("/api/state");
      if (!Array.isArray(state.queue)) throw new Error("full state was not restored");
      state.queue.length = 0;
      const next = await readState("/api/state");
      if (!next.queue.length) throw new Error("local edits corrupted the server checkpoint");
    });
    console.log("PASS State changes omit unchanged cards, recover missing checkpoints, and preserve authoritative data");

    await page.route("**/api/state", async (route) => {
      const full = await fullState(route);
      const state = await full.response.json();
      await route.fulfill({ ...full, json: { ...state, team: "Test replacement" } });
    });
    assert.equal(await page.evaluate(async () => (await readState("/api/state")).team), "Test replacement");
    await page.unroute("**/api/state");
    assert.notEqual(await page.evaluate(async () => (await readState("/api/state")).team), "Test replacement");
    console.log("PASS Full-state test overrides clear their checkpoint and recover when interception ends");

    await page.keyboard.press("Control+k");
    await page.getByRole("combobox", { name: "Search commands, work, and sessions" }).fill("Start a new agent session");
    await page.keyboard.press("Enter");
    const area = page.locator('#work textarea[data-key="msg:new-session"]');
    await area.fill("Draft before optional editor");
    await page.evaluate(() => window.CockpitEditor.set(true));
    await area.press("End");
    await area.pressSequentially(" and while loading");
    assert.equal(editorRequests, 1);
    releaseEditor();
    await page.locator("#work .cm-editor").waitFor();
    assert.equal(await area.inputValue(), "Draft before optional editor and while loading");
    assert.match(await page.locator("#work .cm-content").innerText(), /and while loading/);
    await page.evaluate(() => window.CockpitEditor.set(false));
    await area.fill("> Quoted line\nDraft after loading");
    assert.equal(await area.evaluate((node) => node.parentElement.querySelector(".quote-copy")?.textContent), "> Quoted lineDraft after loading");
    console.log("PASS Optional Vim loading preserves a draft typed while the bundle is still loading");
    assert.deepEqual(errors, []);
    await context.close();

    const blocked = await browser.newContext({ extraHTTPHeaders: { Cookie: `${info.name}=${info.value}` } });
    await blocked.addInitScript(() => { Storage.prototype.setItem = () => { throw new Error("Storage blocked for test"); }; });
    const blockedPage = await blocked.newPage();
    await blockedPage.goto(info.origin + "/classic");
    await blockedPage.locator("#next [data-step]").waitFor();
    await blockedPage.keyboard.press("Control+k");
    await blockedPage.getByRole("combobox", { name: "Search commands, work, and sessions" }).fill("Start a new agent session");
    await blockedPage.keyboard.press("Enter");
    await blockedPage.locator('#work textarea[data-key="msg:new-session"]').fill("Draft with storage blocked");
    await blockedPage.evaluate(() => window.CockpitEditor.set(true));
    await blockedPage.locator("#work .cm-editor").waitFor();
    assert.equal(await blockedPage.evaluate(() => window.CockpitEditor.get()), true);
    assert.match(await blockedPage.locator("#work .cm-content").innerText(), /Draft with storage blocked/);
    console.log("PASS First Vim enable works when browser storage is blocked");
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
