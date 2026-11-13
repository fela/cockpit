// Page recovery and comment races run against an isolated database and fake sessions.
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { workFolder } = require("./work.cjs");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "../..");
const work = workFolder("cockpit-page-bugs-");
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const gate = () => { let release; const promise = new Promise((resolve) => { release = resolve; }); return { promise, release }; };
let server, browser, serverLog = "";

(async () => {
  browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE || undefined });
  server = spawn(path.join(root, ".venv/bin/python"), [path.join(__dirname, "serve_keyboard.py"), work],
    { cwd: root, stdio: ["ignore", "pipe", "pipe"] });
  server.stdout.on("data", (data) => { serverLog += data; });
  server.stderr.on("data", (data) => { serverLog += data; });
  const runtime = path.join(work, "runtime.json");
  for (let n = 0; !fs.existsSync(runtime); n++) {
    if (n > 300 || server.exitCode !== null) throw new Error(`Fixture failed: ${serverLog}`);
    await delay(200);
  }
  const { origin, cookie } = JSON.parse(fs.readFileSync(runtime, "utf8"));
  const call = async (method, url, body, agent = false) => {
    const response = await fetch(origin + url, { method, headers: {
      Cookie: cookie, Origin: origin, "Content-Type": "application/json",
      ...(agent ? { Authorization: `Bearer ${"t".repeat(40)}`, "X-Cockpit-As": "page-test" } : {}),
    }, body: body === undefined ? undefined : JSON.stringify(body) });
    const data = await response.json();
    assert.ok(response.ok, `${method} ${url}: ${JSON.stringify(data)}`);
    return data;
  };
  let sequence = 0;
  const fixture = async (withStep) => {
    const key = `note:tasks-${++sequence}`;
    const item = await call("PUT", "/api/items", { key, title: "Tasks review",
      sessions: ["11111111-1111-4111-8111-111111111111"] }, true);
    const report = await call("POST", `/api/items/${item.id}/pages`, { title: "Tasks plan",
      html: "<!doctype html><html><body><p>Review the tasks list.</p><input name=choice></body></html>" }, true);
    const step = withStep ? (await call("POST", "/api/steps", { item_key: key, need: "review",
      ask: "Review the tasks plan", session_id: item.sessions[0] }, true)).step : null;
    const comment = async (text) => call("POST", `/api/item-pages/${report.id}/comments`, {
      revision: report.revision, step_id: step?.id ?? null, text,
      anchor: {kind:"text",quote:"Review the tasks list.",prefix:"",suffix:""},
    });
    return { item, report, step, comment };
  };
  const failures = [];
  const check = async (id, test) => {
    if (process.argv[2] && process.argv[2] !== id) return;
    const context = await browser.newContext({ extraHTTPHeaders: { Cookie: cookie } });
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    try {
      await test(page, context);
      assert.deepEqual(errors, [], "uncaught page errors");
      console.log(`PASS ${id}`);
    } catch (error) {
      failures.push(id);
      console.error(`FAIL ${id}: ${error.stack}`);
    } finally { await context.close(); }
  };
  const main = async (page) => {
    await page.goto(origin + "/classic");
    await page.waitForFunction(() => typeof ui !== "undefined" && ui.state);
  };

  await check("P1", async (page, context) => {
    for (const kind of ["goals", "inbox-settings"]) {
      await main(page);
      const draft = kind === "goals"
        ? [{ text: "Finish tasks", paused: false }, { text: "Review reminders", paused: true }]
        : await call("GET", "/api/inbox-settings");
      if (kind !== "goals") {
        draft.rules.points = ["Help Ana first", "Review tasks"];
        draft.sources.text = "Check the tasks list";
      }
      await context.setOffline(true);
      await page.evaluate(({kind,draft}) => {
        if (kind === "goals") saveGoals(draft); else saveInboxSettings(draft);
      }, {kind,draft});
      await page.waitForFunction(() => Object.keys(localStorage).some((key) => key.startsWith("cockpit.outbox.")));
      await page.goto("about:blank");
      await page.route(`**/api/${kind}`, (route) => route.request().method() === "PUT"
        ? route.fulfill({status:409,json:{detail:"Edit refused"}}) : route.continue());
      await context.setOffline(false);
      await main(page);
      await page.waitForFunction((kind) => localStorage.getItem(`cockpit.unsaved:/api/${kind}`), kind);
      const notice = await page.locator("#toast").textContent();
      assert.match(notice, /not saved: Edit refused/);
      assert.ok(notice.includes(kind === "goals" ? "Finish tasks" : "Help Ana first"));
      await page.reload();
      await page.waitForFunction(() => ui.state);
      if (kind === "goals") {
        await page.evaluate(() => openSettings("goals"));
        const rows = await page.locator("#settings-view .goal-entry").evaluateAll((items) => items.map((item) => ({
          text: item.querySelector(".goal-entry-text").value, paused: item.classList.contains("paused") })));
        assert.deepEqual(rows, draft);
        await page.locator("#settings-view .goal-entry-text").first().fill("Finish tasks and reminders");
        await page.unroute(`**/api/${kind}`);
        await page.locator("#settings-view button[type=submit]").click();
      } else {
        await page.evaluate(() => openSettings("inbox"));
        assert.deepEqual(await page.locator("#settings-view .inbox-point-text").evaluateAll((boxes) => boxes.map((box) => box.value)), draft.rules.points);
        assert.equal(await page.locator("#settings-view #inbox-sources").inputValue(), draft.sources.text);
        await page.locator("#settings-view #inbox-sources").fill("Check reminders too");
        await page.unroute(`**/api/${kind}`);
        await page.locator("#settings-view button[type=submit]").click();
      }
      await page.waitForFunction((kind) => !localStorage.getItem(`cockpit.unsaved:/api/${kind}`), kind);
    }
  });

  await check("P2", async (page) => {
    for (const action of ["sendToItem", "addAsItem"]) {
      const data = await fixture(false);
      const first = await data.comment("Remove this task note");
      const second = await data.comment("Keep this reminder note");
      await page.goto(`${origin}/pages?id=${data.report.id}&comment=1`);
      await page.locator(`[data-comment="${first.id}"] .pc-delete`).waitFor();
      const held = gate(), deleting = gate();
      await page.route(`**/api/page-comments/${first.id}`, async (route) => {
        deleting.release();
        await held.promise;
        await route.continue();
      });
      await page.locator(`[data-comment="${first.id}"] .pc-delete`).click();
      await deleting.promise;
      await call("POST", `/api/items/${data.item.id}/pages`, { title: "Tasks plan",
        html: "<!doctype html><html><body><p>Review the tasks list.</p><p>Updated plan.</p></body></html>" }, true);
      await page.evaluate(() => load());
      await page.locator(`[data-comment="${second.id}"]`).waitFor();
      await page.evaluate((action) => { window.sendingCheck = window[action](); }, action);
      held.release();
      await page.evaluate(() => window.sendingCheck);
      assert.equal(await page.evaluate(() => replyError), "");
      const remaining = (await call("GET", `/api/item-pages/${data.report.id}/comments`)).comments;
      assert.equal(remaining.some((comment) => comment.id === first.id), false);
      assert.equal(remaining.find((comment) => comment.id === second.id).state, action === "sendToItem" ? "sent" : "added");
      await page.unrouteAll({behavior:"wait"});
    }
  });

  await check("P3", async (page) => {
    const data = await fixture(true);
    const comment = await data.comment("Remove this comment before sending");
    await page.goto(`${origin}/pages?id=${data.report.id}&step=${data.step.id}&comment=1`);
    await page.waitForFunction(() => column?.stepId === stepId && stepTakes(theStep()));
    await page.locator(`[data-comment="${comment.id}"] .pc-delete`).waitFor();
    const choice = gate(), deletion = gate(), choosing = gate(), deleting = gate();
    let sends = 0;
    await page.route(`**/api/item-pages/${data.report.id}/choices`, async (route) => {
      if (route.request().method() === "PUT") { choosing.release(); await choice.promise; }
      await route.continue();
    });
    await page.route(`**/api/page-comments/${comment.id}`, async (route) => {
      deleting.release();
      await deletion.promise;
      await route.continue();
    });
    await page.route(`**/api/steps/${data.step.id}/hand-off`, async (route) => { sends += 1; await route.continue(); });
    await page.evaluate((pageId) => {
      CockpitChoices.save(pageId, {name:"choice",chosen:"Review tasks",label:"Tasks",step_id:stepId}, () => {});
      keepReply(replyKey(true), "Review the plan");
      window.sendingCheck = send();
    }, data.report.id);
    await choosing.promise;
    await page.locator(`[data-comment="${comment.id}"] .pc-delete`).click();
    await deleting.promise;
    choice.release();
    await delay(200);
    const premature = sends;
    deletion.release();
    await page.evaluate(() => window.sendingCheck);
    assert.equal(premature, 0, "Send overtakes deletion while the choice saves");
    assert.equal(sends, 1);
    assert.equal(await page.evaluate(() => replyError), "");
    assert.equal((await call("GET", `/api/item-pages/${data.report.id}/comments`)).comments.length, 0);
    await page.unrouteAll({behavior:"wait"});
  });

  await check("P4", async (page) => {
    const data = await fixture(true);
    await main(page);
    await page.clock.install();
    await page.evaluate((id) => {
      const step = STEP_LISTS.flatMap((name) => ui.state[name]).find((entry) => entry.id === id);
      window.sendingCheck = handOffStep(step, "Review tasks", {session_id:step.target.session_id}, [], "Tasks session", null, null, null);
    }, data.step.id);
    await page.evaluate(() => window.sendingCheck);
    await page.locator("#toast .toast-more").click();
    await page.locator("#toast textarea").fill("More about reminders");
    await page.locator("#toast").hover();
    await page.clock.fastForward(61000);
    assert.equal(await page.locator("#toast .toast-action").isDisabled(), true);
    assert.equal(await page.locator("#toast textarea").inputValue(), "More about reminders");
    assert.equal(await page.locator("#toast").isVisible(), true);
  });
  assert.deepEqual(failures, []);
})().catch((error) => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close();
  if (server) server.kill();
});
