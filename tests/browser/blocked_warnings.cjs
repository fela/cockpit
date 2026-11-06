// Run with node tests/browser/blocked_warnings.cjs (PLAYWRIGHT_MODULE and BROWSER_EXECUTABLE override the host's).
// Uses the keyboard fixture's isolated database; never touches live work or real agents.  PERM-25: on the page with
// the rail, at 1280 px and 390 px, - no Blocked and waiting panel shows above Next up; - each dialog is a red,
// pulsing row in Inbox (the picked one in the picked colours), named by its heading, as the account's warning is; -
// with nothing picked, Next up shows the newest dialog's full card, with Run; - on a step, the change-order arrows
// turn red and their small card names the next dialog; pressing them opens it; - a row opens its dialog; Run answers
// it, its row goes, and Next up goes on to the next dialog; - nothing is wider than the window, and the page logs no
// error. The classic page keeps its Blocked and waiting panel.
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");
const { spawn } = require("node:child_process");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "../..");
const work = workFolder("cockpit-blocked-warnings-");
const python = path.join(root, ".venv/bin/python");
const server = spawn(python, [path.join(__dirname, "serve_keyboard.py"), work], {
  cwd: root, stdio: ["ignore", "pipe", "pipe"],
});
let serverLog = "";
server.stdout.on("data", (data) => { serverLog += data; });
server.stderr.on("data", (data) => { serverLog += data; });
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
// The fixture's session (tests/conftest.py) and agent token.
const SESSION_A = "11111111-1111-4111-8111-111111111111";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "permission-hook", "content-type": "application/json" };
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
let browser;

(async () => {
  const runtime = path.join(work, "runtime.json");
  for (let at = 0; !fs.existsSync(runtime); at++) {
    if (at > 1200 || server.exitCode !== null) throw new Error(`Fixture failed: ${serverLog}`);
    await delay(100);
  }
  const { origin, cookie } = JSON.parse(fs.readFileSync(runtime, "utf8"));
  for (let at = 0; ; at++) {
    try { if ((await fetch(origin + "/healthz")).ok) break; } catch {}
    if (at > 600) throw new Error(`Fixture did not listen: ${serverLog}`);
    await delay(100);
  }
  const executablePath = process.env.BROWSER_EXECUTABLE || (fs.existsSync(CHROMIUM) ? CHROMIUM : undefined);
  browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  for (const [width, height] of [[1280, 900], [390, 844]]) {
    const phone = width < 600;
    const ask = async (command, description) => {
      const response = await fetch(origin + "/api/permissions/requests", {
        method: "POST", headers: AGENT,
        body: JSON.stringify({ kind: "asked", session_id: SESSION_A, cwd: work, tool_name: "Bash",
          tool_input: { command, description }, tool_use_id: "", reason: "" }),
      });
      assert.equal(response.ok, true, await response.clone().text());
      return (await response.json()).id;
    };
    const older = await ask(`rm exports/preview-${width}.csv`, "Remove the Tasks export preview");
    await delay(1100);
    const newer = await ask(`cat reminders/weekly-${width}.txt`, "Read the Tasks reminder draft");

    const context = await browser.newContext({ extraHTTPHeaders: { Cookie: cookie }, viewport: { width, height } });
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
    await page.goto(origin + "/");
    await page.locator(`#sidebar li.warn-row[data-request="${newer}"]`).waitFor({ state: "attached" });
    await page.evaluate(() => { ui.placed.clear(); selectItem(null); });
    await delay(400);

    assert.equal(await page.locator("#blocked > *").count(), 0, `${width}: the Blocked and waiting panel still shows`);
    if (phone) await page.evaluate(() => setPhoneScreen(true, false));
    const rows = await page.evaluate(() => [...document.querySelectorAll("#sidebar .sidebar-group.queue li.warn-row")].map((row) => ({
      request: Number(row.dataset.request), words: row.textContent.trim(), picked: row.classList.contains("sel"), pulse: row.querySelector(".side-row").getAnimations().map((one) => one.animationName).join(","),
    })));
    assert.deepEqual(rows.map((row) => row.request), [newer, older], `${width}: rows ${JSON.stringify(rows)}`);
    assert.deepEqual(rows.map((row) => row.words), ["Read the Tasks reminder draft", "Remove the Tasks export preview"]);
    assert.ok(rows.every((row) => (row.picked ? row.pulse === "" : row.pulse === "warn-row-pulse")), `${width}: the rows do not pulse ${JSON.stringify(rows)}`);
    if (phone) {
      await page.screenshot({ path: path.join(work, `rows-${width}.png`) });
      await page.evaluate(() => setPhoneScreen(false, false));
    }

    // Nothing picked: the newest dialog is Next up, with its full card.
    const card = page.locator(`#next > .blocked-card[data-request="${newer}"]`);
    await card.waitFor();
    assert.equal(await card.locator("button", { hasText: "Run" }).count() > 0, true, `${width}: Next up's dialog has no Run`);
    await page.screenshot({ path: path.join(work, `next-${width}.png`) });

    // On a step: the arrows are red, their small card names the next dialog, and pressing them opens it.
    const stepId = await page.evaluate(() => Number(document.querySelector("#sidebar .sidebar-group.queue li[data-step]")?.dataset.step || 0));
    assert.ok(stepId, `${width}: the fixture's queue has no step`);
    await page.evaluate((id) => selectItem(ui.state.queue.find((step) => step.id === id).item_id), stepId);
    await page.locator("#next .warn-key").waitFor();
    assert.equal((await page.locator("#next .warn-peek-title").textContent()).trim(), "Read the Tasks reminder draft");
    await page.locator("#next .warn-key").click();
    await card.waitFor();

    // The older dialog's row opens it, and a press on the row counts as working on it, so it stays while the session
    // withdraws it. Run answers it, its row goes and Next up goes on to the other dialog.
    if (phone) await page.evaluate(() => setPhoneScreen(true, false));
    // The press tells the server the user is on the call; the withdrawal waits for that, or it races the press.
    const marked = page.waitForResponse((response) => response.url().endsWith(`/api/permissions/requests/${older}/active`));
    await page.locator(`#sidebar li.warn-row[data-request="${older}"] .side-row`).click();
    await marked;
    const olderCard = page.locator(`#next > .blocked-card[data-request="${older}"]`);
    await olderCard.waitFor();
    const withdrawn = await fetch(`${origin}/api/permissions/requests/${older}/withdraw`, { method: "POST", headers: AGENT, body: JSON.stringify({ session_id: SESSION_A }) });
    assert.equal(withdrawn.ok, true, await withdrawn.clone().text());
    await page.evaluate(async () => { ui.pendingRender = true; await refresh(); });
    await olderCard.locator(".request-note", { hasText: "no longer needs this call" }).waitFor();
    assert.equal(await page.locator(`#sidebar li.warn-row.sel[data-request="${older}"]`).count(), 1, `${width}: the opened dialog's row is not picked`);
    await olderCard.locator("button.primary", { hasText: "Run" }).click();
    await page.locator(`#sidebar li.warn-row[data-request="${older}"]`).waitFor({ state: "detached" });
    await card.waitFor();
    const fits = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
    assert.equal(fits, true, `${width}: the page is wider than the window`);
    await page.screenshot({ path: path.join(work, `after-allow-${width}.png`) });

    // A long command on Next up is clamped and opens on a press.
    const long = await ask(Array.from({ length: 12 }, (_, n) => `echo line-${n}-${width}`).join("\n"), "Print twelve lines");
    // From its row, which on a phone sits on the list's screen while the card is drawn hidden.
    if (phone) await page.evaluate(() => setPhoneScreen(true, false));
    await page.locator(`#sidebar li.warn-row[data-request="${long}"] .side-row`).click();
    const longCall = page.locator(`#next > .blocked-card[data-request="${long}"] pre.call`);
    await longCall.waitFor();
    await page.waitForFunction((id) => document.querySelector(`#next [data-request="${id}"] pre.call`)?.classList.contains("clipped"), long);
    await longCall.click();
    await page.waitForFunction((id) => document.querySelector(`#next [data-request="${id}"] pre.call`)?.classList.contains("expanded"), long);
    await page.locator(`#next > .blocked-card[data-request="${long}"]`).locator("button.primary", { hasText: "Run" }).click();
    await page.locator(`#sidebar li.warn-row[data-request="${long}"]`).waitFor({ state: "detached" });

    // A dialog that came up on Next up by itself, then closed in its session, stays there marked closed (PERM-23).
    await page.evaluate(() => { ui.placed.clear(); selectItem(null); });
    await card.waitFor();
    const settled = await fetch(`${origin}/api/permissions/requests/${newer}/settle`, { method: "POST", headers: AGENT, body: JSON.stringify({ state: "answered" }) });
    assert.equal(settled.ok, true, await settled.clone().text());
    await page.locator(`#next > .blocked-card.settled[data-request="${newer}"]`).waitFor({ timeout: 20000 });

    // The classic page keeps the panel.
    const third = await ask(`ls "$CLAUDE_JOB_DIR/tmp" # ${width}`, "List the job folder");
    await page.goto(origin + "/classic");
    await page.locator(`#blocked .blocked-card[data-request="${third}"]`).waitFor();
    await page.goto(origin + "/");
    const thirdCard = page.locator(`#next > .blocked-card[data-request="${third}"]`);
    await thirdCard.waitFor();
    // The answer reaches the server before the page closes, or the next width finds this dialog still open.
    const answered = page.waitForResponse((response) => response.url().includes(`/api/permissions/requests/${third}/`) && !response.url().endsWith("/active")
      && response.request().method() === "POST");
    await thirdCard.locator("button.primary", { hasText: "Run" }).click();
    await page.locator(`#sidebar li.warn-row[data-request="${third}"]`).waitFor({ state: "detached" });
    await answered;
    assert.deepEqual(errors, []);
    console.log(`PASS ${width}: dialogs are red rows in Your queue and open on Next up`);
    await context.close();
  }
  console.log(`Screenshots in ${work}`);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
}).finally(async () => {
  await browser?.close();
  server.kill();
});
