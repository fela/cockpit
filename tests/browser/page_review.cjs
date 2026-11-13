// Page action ordering and recovery checks (P1–P9). Uses the isolated keyboard fixture: no live data
// or agents. Requests are held at named boundaries, so races do not depend on network timing.
// PLAYWRIGHT_MODULE=/path/to/playwright node tests/browser/page_review.cjs [P1..P9]
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { workFolder } = require("./work.cjs");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "../..");
const work = workFolder("cockpit-page-review-");
let server;
let serverLog = "";
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const gate = () => { let release; const promise = new Promise((resolve) => { release = resolve; }); return { promise, release }; };
const until = async (test) => {
  for (let n = 0; n < 100; n++) { if (await test()) return; await delay(50); }
  throw new Error("request boundary was not reached");
};
const asks = (page) => page.evaluate(() => {
  const event = new Event("beforeunload", { cancelable: true });
  dispatchEvent(event);
  return event.defaultPrevented;
});
let browser;

(async () => {
  browser = await chromium.launch({ headless: true,
    executablePath: process.env.BROWSER_EXECUTABLE || undefined });
  server = spawn(path.join(root, ".venv/bin/python"), [path.join(__dirname, "serve_keyboard.py"), work], {
    cwd: root, stdio: ["ignore", "pipe", "pipe"],
  });
  server.stdout.on("data", (data) => { serverLog += data; });
  server.stderr.on("data", (data) => { serverLog += data; });
  const runtime = path.join(work, "runtime.json");
  for (let n = 0; !fs.existsSync(runtime); n++) {
    if (n > 300 || server.exitCode !== null) throw new Error(`Fixture failed: ${serverLog}`);
    await delay(200);
  }
  const { origin, cookie } = JSON.parse(fs.readFileSync(runtime, "utf8"));
  await until(() => fetch(origin + "/healthz").then((r) => r.ok, () => false));
  const user = async (method, url, body, agent = false) => {
    const response = await fetch(origin + url, { method, headers: {
      Cookie: cookie, Origin: origin, "Content-Type": "application/json",
      ...(agent ? { Authorization: `Bearer ${"t".repeat(40)}`, "X-Cockpit-As": "page-review-test" } : {}),
    }, body: body === undefined ? undefined : JSON.stringify(body) });
    const data = await response.json();
    assert.ok(response.ok, `${method} ${url}: ${JSON.stringify(data)}`);
    return data;
  };
  let sequence = 0;
  const fixture = async () => {
    const key = `note:page-review-${++sequence}`;
    const item = await user("PUT", "/api/items", { key, title: `Page review ${sequence}`,
      sessions: ["11111111-1111-4111-8111-111111111111"] }, true);
    const { step } = await user("POST", "/api/steps", { item_key: key, need: "review", ask: "Check this page",
      session_id: item.sessions[0] }, true);
    const report = await user("POST", `/api/items/${item.id}/pages`, { title: "Review report",
      html: '<!doctype html><html><body><h1>Review report</h1><input name="choice"></body></html>' }, true);
    return { item, step, report };
  };
  const main = async (page) => {
    await page.goto(origin + "/classic");
    await page.waitForFunction(() => typeof ui !== "undefined" && ui.state);
  };
  const pagesView = async (page, data) => {
    await page.goto(`${origin}/pages?id=${data.report.id}&step=${data.step.id}&comment=1`);
    await page.locator("#pages-reply-text").waitFor();
  };
  const failures = [];
  const check = async (id, name, test) => {
    if (process.argv[2] && process.argv[2] !== id) return;
    const context = await browser.newContext({ extraHTTPHeaders: { Cookie: cookie }, viewport: { width: 1280, height: 900 } });
    const page = await context.newPage();
    page.setDefaultTimeout(30000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    try {
      await test(page, context);
      assert.deepEqual(errors, [], "uncaught page errors");
      console.log(`PASS ${id}: ${name}`);
    } catch (error) {
      failures.push(id);
      console.error(`FAIL ${id}: ${name}: ${error.stack}`);
    } finally {
      await context.close();
    }
  };

  await check("P1", "a finish waiting behind another holds leaving and sends its last words", async (page) => {
    const a = await fixture();
    const b = await fixture();
    const held = gate();
    let first = false;
    let second = false;
    await page.route("**/api/items/*/finish", async (route) => {
      if (route.request().url().includes(`/items/${a.item.id}/`)) { first = true; await held.promise; }
      else second = true;
      await route.continue();
    });
    await main(page);
    await page.evaluate(([a, b]) => {
      const steps = STEP_LISTS.flatMap((name) => ui.state[name]);
      window.finishingA = taskDone(steps.find((s) => s.id === a), `msg:step:${a}`, null);
      ui.drafts.set(`msg:step:${b}`, "Last words for B");
      saveDrafts();
      window.finishingB = taskDone(steps.find((s) => s.id === b), `msg:step:${b}`, null);
    }, [a.step.id, b.step.id]);
    try {
      await until(() => first);
      assert.equal(second, false);
      assert.equal(await asks(page), true, "no leave guard while the second finish has no outbox entry");
    } finally { held.release(); }
    await page.evaluate(() => Promise.all([window.finishingA, window.finishingB]));
    assert.equal(second, true);
    assert.equal(await asks(page), false, "finish guard did not drain");
    const state = await user("GET", "/api/state");
    assert.ok(state.messages.some((m) => m.item_id === b.item.id && m.text.includes("Last words for B")));
  });

  await check("P2", "answers wait for comment deletions and stop on a refused deletion", async (page) => {
    for (const mode of ["send", "done", "answer", "secret", "run", "allow", "refused"]) {
      const data = await fixture();
      const comment = await user("POST", `/api/item-pages/${data.report.id}/comments`, {
        revision: data.report.revision, step_id: data.step.id,
        anchor: { kind: "part", path: "h1", text: "Review report" }, text: "Remove before answering",
      });
      const held = gate();
      let deleting = false;
      let answers = 0;
      const answerPath = mode === "done" || mode === "answer" ? `/api/items/${data.item.id}/finish`
        : `/api/steps/${data.step.id}/${["secret", "run", "allow"].includes(mode) ? mode : "hand-off"}`;
      await page.route(origin + answerPath, async (route) => {
        answers += 1;
        // Secret storage is not needed to test its wait; the refusal also checks its draft comes back.
        if (mode === "secret") return route.fulfill({ status: 409, json: { detail: "test secret refused" } });
        if (mode === "run" || mode === "allow") return route.fulfill({ json: {} });
        await route.continue();
      });
      await page.route(`**/api/page-comments/${comment.id}`, async (route) => {
        deleting = true;
        await held.promise;
        if (mode === "refused") return route.fulfill({ status: 409, json: { detail: "test deletion refused" } });
        await route.continue();
      });
      await main(page);
      await page.evaluate(({ id, comment, mode }) => {
        const step = STEP_LISTS.flatMap((name) => ui.state[name]).find((s) => s.id === id);
        window.deleting = deletePageComment(comment);
        const key = `msg:step:${id}`;
        ui.drafts.set(key, "Answer after removal");
        if (mode === "done") window.answering = taskDone(step, key, null);
        else if (mode === "answer") window.answering = finishItem(step, "complete", "Close it", "", [], "", null, null);
        else if (mode === "secret") window.answering = sendSecret(step, key, `secret:${id}`, "test", "value", "test session");
        else if (mode === "run" || mode === "allow") window.answering = pressStepButton(step, mode, {}, "Go ahead", "Refused", null, null);
        else window.answering = handOffDraft(key, step, { session_id: step.target.session_id }, "test session");
      }, { id: data.step.id, comment, mode });
      try {
        await until(() => deleting);
        await delay(150);
        assert.equal(answers, 0, `${mode}: answer overtook the deletion`);
        assert.equal(await asks(page), true, `${mode}: waiting answer had no leave guard`);
      } finally { held.release(); }
      await page.evaluate(() => Promise.all([window.answering, window.deleting]));
      assert.equal(answers, mode === "refused" ? 0 : 1, `${mode}: wrong number of answers`);
      if (mode === "refused") {
        assert.match(await page.locator("#toast").textContent(), /not deleted/i);
        assert.equal(await page.evaluate((id) => ui.drafts.get(`msg:step:${id}`), data.step.id), "Answer after removal");
      } else {
        const comments = await user("GET", `/api/item-pages/${data.report.id}/comments`);
        assert.ok(!comments.comments.some((c) => c.id === comment.id), "deleted comment was sent");
      }
      await page.unrouteAll({ behavior: "wait" });
    }
  });

  // P3: persistent warning snoozes are covered by warning_snooze.cjs.

  await check("P4", "Add more typing guards leaving until cancel or the send settles", async (page) => {
    await main(page);
    await page.evaluate(() => {
      const shown = toast("Sent to test session", false, { label: "Take back", run: () => {} });
      openFollowUp(shown, Promise.resolve(true), { id: "test-message", text: "first" }, "test session", 1);
    });
    assert.equal(await asks(page), false, "empty follow-up guards leaving");
    await page.locator("#toast textarea").fill("Follow-up still being written");
    assert.equal(await asks(page), true, "typing a follow-up has no leave guard");
    await page.locator(".toast-follow-cancel").click();
    assert.equal(await asks(page), false, "cancelled follow-up still guards leaving");
    await page.evaluate(() => {
      window.following = new Promise((resolve) => { window.settleFollow = resolve; });
      const shown = toast("Sending to test session", false, { label: "Take back", run: () => {} });
      openFollowUp(shown, window.following, { id: "test-message", text: "first" }, "test session", 1);
    });
    await page.locator("#toast textarea").fill("More words to send");
    await page.locator(".toast-follow-send").click();
    assert.equal(await asks(page), true, "sent follow-up lost its guard while waiting for the first message");
    await page.evaluate(() => window.settleFollow(false));
    // run() clears ui.busy before its redraw, and the guard lifts once that redraw is done too.
    await page.waitForFunction(() => {
      const event = new Event("beforeunload", { cancelable: true });
      dispatchEvent(event);
      return !event.defaultPrevented;
    }, null, { timeout: 5000 }).catch(() => { throw new Error("settled follow-up still guards leaving"); });
  });

  await check("P5", "coalesced page choices hold leaving until the latest value saves", async (page) => {
    const data = await fixture();
    await main(page);
    const held = [gate(), gate()];
    const values = [];
    await page.route(`**/api/item-pages/${data.report.id}/choices`, async (route) => {
      const at = values.length;
      values.push(route.request().postDataJSON().chosen);
      await held[at].promise;
      await route.continue();
    });
    await page.evaluate(({ report, step }) => {
      const body = { name: "choice", chosen: "first", label: "", step_id: step.id };
      CockpitChoices.save(report.id, body, () => {});
      CockpitChoices.save(report.id, { ...body, chosen: "middle" }, () => {});
      CockpitChoices.save(report.id, { ...body, chosen: "latest" }, () => {});
    }, data);
    try {
      await until(() => values.length === 1);
      assert.equal(await asks(page), true, "queued value has no leave guard");
      held[0].release();
      await until(() => values.length === 2);
      assert.equal(await asks(page), true, "guard ended before the queue drained");
    } finally { held.forEach((g) => g.release()); }
    await page.evaluate((id) => CockpitChoices.settled(id), data.step.id);
    assert.deepEqual(values, ["first", "latest"]);
    assert.equal(await asks(page), false);
    assert.equal((await user("GET", `/api/item-pages/${data.report.id}/choices`)).choices[0].chosen, "latest");
  });

  await check("P6", "Take back replay after reload restores server words into the Pages reply", async (page, context) => {
    const data = await fixture();
    await pagesView(page, data);
    await page.locator("#pages-reply-text").fill("Answer to recover");
    await page.evaluate(() => send());
    const held = gate();
    const recover = gate();
    let returns = 0;
    await context.route(`**/api/steps/${data.step.id}/return`, async (route) => {
      returns += 1;
      if (returns === 1) { await held.promise; await route.abort().catch(() => {}); }
      else { await recover.promise; await route.continue(); }
    });
    await page.evaluate(() => { window.taking = takeBack(); });
    await until(() => returns === 1);
    try {
      await page.reload();
      await until(() => returns === 2);
      // Make the reply cache read the empty draft before recovery answers.
      await page.evaluate(() => savedReply(replyKey(true)));
      recover.release();
      await page.waitForFunction(() => !Object.keys(localStorage).some((k) => k.startsWith("cockpit.outbox.")));
      await page.evaluate(() => reload());
      assert.equal(await page.locator("#pages-reply-text").inputValue(), "Answer to recover");
      assert.equal(await page.evaluate(() => localStorage.getItem(replyKey(true))), "Answer to recover");
    } finally { held.release(); recover.release(); }
  });

  await check("P7", "Take back uses server unsent words and explains an already delivered answer", async (page) => {
    for (const stopped of [true, false]) {
      const data = await fixture();
      await pagesView(page, data);
      await page.locator("#pages-reply-text").fill("Original answer");
      await page.evaluate(() => send());
      await page.route(`**/api/steps/${data.step.id}/return`, async (route) => {
        const response = await route.fetch();
        const result = await response.json();
        await route.fulfill({ response, json: { ...result, stopped, unsent: stopped ? "Server's stopped words" : null } });
      });
      await page.evaluate(() => takeBack());
      assert.equal(await page.locator("#pages-reply-text").inputValue(), stopped ? "Server's stopped words" : "");
      if (!stopped) assert.match(await page.locator("#banner").textContent(), /already.*(sent|reached|delivered)/i);
      await page.unrouteAll({ behavior: "wait" });
    }
  });

  await check("P8", "Narrower terminates for detached picks and an ancestor skipped by equal boxes", async (page, context) => {
    await page.goto(origin + "/usage");
    const cdp = await context.newCDPSession(page);
    // Interrupt the old synchronous loop so this test fails without freezing the entire test process.
    const timer = setTimeout(() => cdp.send("Runtime.terminateExecution").catch(() => {}), 1500);
    try {
      const result = await page.evaluate(() => {
        const wrapper = document.createElement("div");
        const inner = wrapper.appendChild(document.createElement("div"));
        const leaf = inner.appendChild(document.createElement("div"));
        wrapper.style.cssText = "width:100px;height:100px";
        inner.style.cssText = leaf.style.cssText = "width:100%;height:100%";
        document.body.append(wrapper);
        const skipped = Pick.toward(inner, leaf, "div", "#pick-bar, #pick-box");
        const normal = Pick.toward(wrapper, leaf, "div", "#pick-bar, #pick-box");
        wrapper.remove();
        const detached = Pick.toward(wrapper, leaf, "div", "#pick-bar, #pick-box");
        return [skipped === null, normal === null, detached === null];
      });
      assert.deepEqual(result, [true, true, true]);
    } finally { clearTimeout(timer); await cdp.detach(); }
  });

  await check("P9", "an older briefing load cannot replace a newer article or navigation or mark it read", async (page) => {
    const held = gate();
    let listings = 0;
    let oldBody = false;
    const read = [];
    const entry = (id) => ({ id, day: id === 10 ? "2026-01-14" : "2026-01-15" });
    await page.route("**/api/briefings", (route) => {
      const id = ++listings === 1 ? 10 : 20;
      return route.fulfill({ json: { briefings: id === 10 ? [entry(10)] : [entry(20), entry(10)],
        read_id: 0, team: "Sam, Alex and Ana work on Tasks", hour: 10, launch: null } });
    });
    await page.route(/\/api\/briefings\/(?:10|20)$/, async (route) => {
      const id = Number(route.request().url().split("/").pop());
      if (id === 10) { oldBody = true; await held.promise; }
      await route.fulfill({ json: { ...entry(id), since: "2026-01-14T10:00:00Z", until: "2026-01-15T10:00:00Z",
        created_at: "2026-01-15T10:00:00Z", updated_at: "2026-01-15T10:00:00Z", body_html: `<p>Briefing ${id}</p>` } });
    });
    await page.route("**/api/briefings/*/read", (route) => {
      read.push(Number(route.request().url().split("/").at(-2)));
      return route.fulfill({ json: {} });
    });
    await page.goto(origin + "/briefing");
    await until(() => oldBody);
    await page.evaluate(() => { window.newLoad = load(); });
    await page.evaluate(() => window.newLoad);
    assert.match(await page.locator(".briefing-body").textContent(), /20/);
    const response = page.waitForResponse((r) => r.url().endsWith("/api/briefings/10"));
    held.release();
    await response;
    await delay(150);
    assert.match(await page.locator(".briefing-body").textContent(), /20/, "older article replaced the newer one");
    assert.equal(await page.locator('#days a[aria-current="page"]').getAttribute("href"), "/briefing?id=20");
    assert.deepEqual(read, [20], "superseded load marked its briefing read");
  });

  assert.deepEqual(failures, []);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
}).finally(async () => {
  if (browser) await browser.close();
  if (server) server.kill();
});
