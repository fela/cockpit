// Startup reports use a task-named card with truthful recovery, a Snooze menu and a message box that hands the warning
// to a new session (SPEC UI-72, UI-82), on desktop and touch in both themes.
// All session actions are intercepted; the preview has only synthetic records.
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { startPreview } = require("./work.cjs");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

const ID = "11111111-1111-4111-8111-111111111111";
const BASH_ID = "22222222-2222-4222-8222-222222222222";
const NAME = "Example landing page";
const UPDATED = new Date().toISOString();
const COMMAND = "bash ./setup.sh";

async function main() {
  const { work, info } = await startPreview("cockpit-startup-dialog-");
  const response = await fetch(`${info.origin}/api/state`, { headers: { cookie: `${info.name}=${info.value}` } });
  assert.equal(response.ok, true, "synthetic state loads");
  const snapshot = await response.json();
  const screenshots = process.env.SCREENSHOTS || work;
  fs.mkdirSync(screenshots, { recursive: true });
  const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE || undefined });
  try {
    for (const phone of [false, true]) for (const scheme of ["light", "dark"]) {
      const width = phone ? 390 : 1280;
      const label = `${width}-${scheme}`;
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme,
        isMobile: phone, hasTouch: phone, extraHTTPHeaders: { cookie: `${info.name}=${info.value}` } });
      try {
      let waiting = true;
      let startupFields = { detail: "stuck on a startup dialog", needs: "open this session to continue setup" };
      let attach;
      const restarts = [];
      let restartResponse = { status: 400, json: { detail: "Startup retry failed" } };
      // The warning endpoints answer as the server does; nothing reaches a real session.
      const KEY = `session:${ID}`;
      let handed = {};
      let snoozed = {};
      const sent = [];
      const snoozes = [];
      await context.route("**/api/warnings/message", async (route) => {
        sent.push(route.request().postDataJSON());
        handed = { [KEY]: 999999 };
        await route.fulfill({ json: { item: { id: 999999, key: "todo-999999", summary: sent.at(-1).text }, handed_warnings: handed } });
      });
      await context.route("**/api/warnings/snooze", async (route) => {
        const body = route.request().postDataJSON();
        snoozes.push(body);
        snoozed = body.until ? { [body.key]: body.until } : {};
        await route.fulfill({ json: { snoozed_warnings: snoozed, snooze_reads: {} } });
      });
      await context.route("**/api/state", async (route) => {
        const state = snapshot;
        attach = state.attach;
        const base = { id: "preview-startup", session_id: ID, provider: "claude", name: NAME,
          cwd: "/tmp/example", alive: true, job_state: "working", tempo: "blocked", waits_on_dialog: true,
          updated_at: UPDATED, links: [], last_message_html: "<p>Previous message</p>" };
        const startup = { ...base, ...startupFields };
        const bash = { ...base, id: "preview-bash", session_id: BASH_ID, name: "Export preview",
          detail: "waiting for permission", needs: `approve Bash: ${COMMAND}` };
        await route.fulfill({ json: { ...state, away: null, team: null,
          permission_requests: [], settled_requests: [], checker_refusals: [], codex_requests: [],
          handed_warnings: handed, snoozed_warnings: snoozed,
          waiting_sessions: waiting ? [startup, bash] : [], messages: [] } });
      });
      // A safety net also catches any accidental session action beyond the expected restart.
      await context.route("**/api/sessions/**", async (route) => {
        assert.equal(route.request().method(), "POST");
        assert.equal(new URL(route.request().url()).pathname, `/api/sessions/${ID}/restart`);
        restarts.push(route.request().url());
        await route.fulfill(restartResponse);
      });
      const page = await context.newPage();
      page.setDefaultTimeout(20000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => {
        if (message.type() === "error" && message.text().includes("motion:")) errors.push(message.text());
      });
      await page.goto(info.origin, { timeout: 120000 });
      // Redraws and the phone's screen transition take up to 300 ms; let those finish between actions.
      const settled = () => page.waitForTimeout(400);
      const row = page.locator(`#sidebar li.warn-row:not(.motion-ghost)[data-session="${ID}"]`);
      await row.waitFor({ state: "attached" });
      assert.match(await row.textContent(), /Setup: Example landing page/);
      const openCard = async (id) => {
        if (phone) await page.evaluate(() => setPhoneScreen(true, false));
        await settled();
        await page.locator(`#sidebar li.warn-row:not(.motion-ghost)[data-session="${id}"] .side-row`).click();
        await settled();
      };
      await openCard(ID);
      const card = page.locator(`#next > .startup-dialog:not(.motion-ghost)[data-session="${ID}"]`);
      await card.waitFor();
      assert.equal(await card.getByRole("heading", { name: "Claude reported a setup problem" }).count(), 1);
      assert.equal(await card.locator(".startup-task").textContent(), NAME);
      assert.match(await card.textContent(), /Claude reported a startup block without sharing the setup prompt/);
      assert.match(await card.textContent(), /This status may be out of date/);
      assert.doesNotMatch(await card.textContent(), /asks again here|answer it here|makes the call again/);
      // Cockpit cannot answer the setup prompt, so the only box is the one that hands the warning to a new session.
      assert.equal(await card.locator("textarea").count(), 1);
      assert.equal(await card.locator(".warn-message textarea").getAttribute("placeholder"), "Message… a new session acts on it");
      assert.equal(await card.getByRole("button", { name: "Reply", exact: true }).count(), 0);
      const menu = card.locator(".blocked-head .warn-menu > summary");
      assert.equal(await menu.isVisible(), true, `${label}: the card has a ⋯ menu`);
      const place = await card.evaluate((node) => {
        const outer = node.getBoundingClientRect();
        const key = node.querySelector(".warn-menu > summary").getBoundingClientRect();
        return { right: outer.right - key.right, top: key.top - outer.top };
      });
      assert.ok(place.right >= 0 && place.right < 40 && place.top >= 0 && place.top < 40, `${label}: ⋯ at the top right ${JSON.stringify(place)}`);
      const terminal = card.getByRole("link", { name: "Open terminal", exact: true });
      assert.equal(await terminal.count(), phone ? 0 : 1);
      if (phone) assert.match(await card.textContent(), /If setup still waits, open this session on a computer/);
      else assert.equal(await terminal.getAttribute("href"), `iterm2:/command?c=${encodeURIComponent(attach.replaceAll("{session}", "preview-startup"))}`);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      const layout = await card.evaluate((node) => {
        const bounds = node.getBoundingClientRect();
        const buttons = [...node.querySelectorAll(".actions > :not(.motion-ghost)")]
          .map((button) => { const r = button.getBoundingClientRect(); return { x: r.x, width: r.width, height: r.height }; });
        return { x: bounds.x, width: bounds.width, buttons };
      });
      assert.equal(layout.buttons.length, phone ? 1 : 2);
      for (const box of layout.buttons) {
        assert.ok(box.x >= layout.x && box.x + box.width <= layout.x + layout.width + 1, `${label}: action overflow`);
        if (phone) assert.ok(box.height >= 44, `${label}: touch action too small`);
      }
      await page.screenshot({ path: path.join(screenshots, `startup-dialog-${label}.png`), animations: "disabled" });

      // Snooze: the warning leaves Inbox for Waiting's Snoozed by you; the toast's Undo brings it back.
      const snoozedRow = page.locator(`#sidebar li.warn-snoozed:not(.motion-ghost)[data-warning="${KEY}"]`);
      await menu.click();
      await card.locator(".warn-menu .menu-opens").click();
      await card.locator(".warn-menu .menu-choices button:nth-child(2)").click();
      await card.locator(".warn-snooze-confirm button.primary").click();
      await page.waitForFunction(() => document.getElementById("toast")?.textContent.includes("Snoozed"));
      await row.waitFor({ state: "detached" });
      await snoozedRow.waitFor({ state: "attached" });
      assert.equal(snoozes.length, 1, `${label}: one snooze sent`);
      assert.equal(snoozes[0].key, KEY);
      assert.ok(Date.parse(snoozes[0].until) > Date.now() + 3600e3, `${label}: snoozed for hours`);
      await page.evaluate(() => document.querySelector("#toast button").click());
      await row.waitFor({ state: "attached" });
      await snoozedRow.waitFor({ state: "detached" });
      assert.equal(snoozes.length, 2, `${label}: Undo unsnoozes`);
      assert.equal(snoozes[1].until, null);

      // A snoozed setup warning whose session moves on to another terminal dialog keeps Unsnooze on its card.
      snoozed = { [KEY]: new Date(Date.now() + 3 * 3600e3).toISOString() };
      startupFields = { detail: "waiting for permission", needs: "approve Write: /tmp/example/index.html" };
      await page.evaluate(() => { ui.pendingRender = true; return refresh(); });
      await snoozedRow.waitFor({ state: "attached" });
      if (phone) await page.evaluate(() => setPhoneScreen(true, false));
      await settled();
      await page.evaluate((key) => document.querySelector(`#sidebar li.warn-snoozed[data-warning="${key}"] .side-row`).click(), KEY);
      await settled();
      const moved = page.locator(`#next > .waiting-card:not(.startup-dialog):not(.motion-ghost)[data-session="${ID}"]`);
      await moved.waitFor();
      assert.match(await moved.locator(".warn-snoozed-note").textContent(), /Snoozed until/);
      await moved.locator(".blocked-head .warn-menu > summary").click();
      await page.screenshot({ path: path.join(screenshots, `moved-on-${label}.png`), animations: "disabled" });
      assert.equal(await moved.locator("textarea").count(), 0, `${label}: the menu opens without opening Reply`);
      await moved.locator(".warn-menu .menu-list button", { hasText: "Unsnooze" }).click();
      await row.waitFor({ state: "attached" });
      assert.equal(snoozes.length, 3, `${label}: Unsnooze on the moved-on dialog's card`);
      assert.equal(snoozes[2].until, null);
      startupFields = { detail: "stuck on a startup dialog", needs: "open this session to continue setup" };
      await page.evaluate(() => { ui.pendingRender = true; return refresh(); });
      await settled();

      // Message: the words go to a new session with the warning and where to find the reporting session.
      await openCard(ID);
      await card.waitFor();
      const words = "Check whether this session still needs setup, then clear the warning";
      await card.locator(".warn-message textarea").fill(words);
      await card.locator(".warn-message button.send").click();
      await page.waitForFunction(() => document.getElementById("toast")?.textContent.includes("A new session has your message"));
      assert.equal(sent.length, 1, `${label}: one message sent`);
      assert.equal(sent[0].key, KEY);
      assert.equal(sent[0].text, words);
      assert.equal(sent[0].part.target, "page:next/warning");
      assert.ok(sent[0].part.detail.includes(ID) && sent[0].part.detail.includes("/tmp/example"),
        `${label}: the detail names the reporting session ${JSON.stringify(sent[0].part.detail)}`);
      await row.waitFor({ state: "detached" });
      await page.evaluate(() => { ui.pendingRender = true; return refresh(); });
      await settled();
      assert.equal(await row.count(), 0, `${label}: a handed warning stays out of Inbox after a poll`);
      // The to-do closes while the session still reports: the warning is back.
      handed = {};
      await page.evaluate(() => { ui.pendingRender = true; return refresh(); });
      await row.waitFor({ state: "attached" });
      await openCard(ID);
      await card.waitFor();

      // Each known startup field independently classifies; generic setup commands do not.
      for (const fields of [{ detail: "stuck on a startup dialog", needs: "" },
        { detail: "", needs: "open this session to continue setup" }]) {
        startupFields = fields;
        await page.evaluate(() => refresh());
        await card.waitFor();
        await settled();
      }
      await card.getByRole("button", { name: "Restart session", exact: true }).click();
      await page.locator(".toast").filter({ hasText: "Startup retry failed" }).waitFor();
      await card.getByRole("button", { name: "Restart session", exact: true }).waitFor();
      assert.equal(restarts.length, 1, `${label}: refused restart restores retry`);
      restartResponse = { status: 200, json: { id: 987654, state: "pending" } };
      await card.getByRole("button", { name: "Restart session", exact: true }).click();
      await page.waitForFunction(() => document.querySelector(".startup-dialog .restarting") !== null);
      await settled();
      assert.equal(restarts.length, 2, `${label}: refused retry and successful retry both mocked`);
      assert.doesNotMatch(await page.locator("body").textContent(), /when it makes the call again, it comes here/);

      await openCard(BASH_ID);
      const bash = page.locator(`#next > .waiting-card:not(.motion-ghost)[data-session="${BASH_ID}"]`);
      assert.equal(await bash.locator(".what").textContent(), "The session asks to run a command");
      assert.equal((await bash.locator("pre.call").textContent()).trim(), COMMAND);
      assert.equal(await bash.locator(".startup-task").count(), 0);
      assert.equal(await bash.getByRole("button", { name: "Restart", exact: true }).count(), 1);
      await bash.getByRole("button", { name: "Reply", exact: true }).click();
      await bash.locator(".composer").waitFor();

      await openCard(ID);
      waiting = false;
      await page.evaluate(() => refresh());
      await row.waitFor({ state: "detached" });
      await card.waitFor({ state: "detached" });
      // The session can clear the dialog just before Restart arrives.
      waiting = true;
      restartResponse = { status: 409, json: { detail: "the session no longer waits at a dialog in its terminal" } };
      await page.reload();
      await row.waitFor({ state: "attached" });
      await openCard(ID);
      await card.getByRole("button", { name: "Restart session", exact: true }).click();
      await row.waitFor({ state: "detached" });
      await card.waitFor({ state: "detached" });
      await page.locator(".toast").filter({ hasText: `Nothing to restart: ${NAME} no longer waits at a dialog` }).waitFor();
      assert.equal(restarts.length, 3, `${label}: resolved-dialog race handled`);
      assert.deepEqual(errors, [], label);
      console.log(`PASS ${label}: startup, snooze, message, failed/successful/resolved restart, Bash control, cleared warning`);
      } finally {
        await context.unrouteAll({ behavior: "ignoreErrors" });
        await context.close();
      }
    }
  } finally {
    await browser.close();
  }
  console.log(`Screenshots: ${screenshots}`);
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
