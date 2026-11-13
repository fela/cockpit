// A failed control-use batch passes through the real logger, error API and SQLite store.
"use strict";

const assert = require("node:assert/strict");
const { execFileSync, spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-control-errors-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser;
  try {
    const info = await harnessUp(work, server);
    browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE || undefined });
    const context = await browser.newContext();
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    const page = await context.newPage();
    let failedBatch;
    await page.route("**/api/control-uses", (route) => {
      const batch = route.request().postDataJSON();
      if (!failedBatch && batch.uses.some((use) => use.kind === "press" && use.control === 'key "x"')) {
        failedBatch = batch;
        return route.fulfill({ status: 500, contentType: "application/json", body: "{}" });
      }
      return route.continue();
    });
    await page.goto(`${info.origin}/usage`);
    await page.waitForFunction(() => typeof window.logError === "function" && document.visibilityState === "visible");
    const reported = page.waitForResponse(
      (response) => response.url().endsWith("/api/page-errors") && response.request().method() === "POST",
      { timeout: 25e3 },
    );
    await page.keyboard.press("x");
    const report = await reported;
    assert.ok(failedBatch, "the trusted key press reaches the failed control-use batch");
    assert.equal(report.status(), 200, "the error API accepts the report");
    console.log("pass: the error API accepts the control-use failure report");
    const response = await fetch(`${info.origin}/api/page-errors?all=true`, {
      headers: { authorization: `Bearer ${"t".repeat(40)}` },
      signal: AbortSignal.timeout(10e3),
    });
    assert.equal(response.status, 200);
    const rows = (await response.json()).errors.filter((error) => error.place === "control uses");
    assert.equal(rows.length, 1);
    const [stored] = rows;
    assert.equal(stored.page, "/usage");
    assert.equal(stored.message, "control uses: Cockpit answered 500");
    assert.match(stored.stack, /Error: control uses: Cockpit answered 500\n\s+at .*\/static\/uses\.js(?:\?[^\s:]*)?:\d+/);
    assert.equal(stored.count, 1);
    console.log("pass: the error API returns the original place, message and stack");
    const persisted = JSON.parse(execFileSync(PYTHON, ["-c", `
import json, sqlite3, sys
from pathlib import Path
with sqlite3.connect(Path(sys.argv[1]).as_uri() + "?mode=ro", uri=True) as conn:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM page_errors WHERE id = ?", (int(sys.argv[2]),)).fetchone()
    print(json.dumps(dict(row)))
`, path.join(work, "cockpit.db"), String(stored.id)], { encoding: "utf8", timeout: 5e3 }));
    assert.deepEqual(persisted, stored, "SQLite stores the same original error returned by the API");
    console.log("pass: control-use failure stores its original place, message and stack through the API and SQLite");

    // A restart: the proxy answers 502 while Cockpit is away. The batch goes again under the same id and no error is
    // logged. Only a batch still refused after two minutes is an error.
    const storedRows = async () => {
      const answer = await fetch(`${info.origin}/api/page-errors?all=true`, {
        headers: { authorization: `Bearer ${"t".repeat(40)}` },
        signal: AbortSignal.timeout(10e3),
      });
      assert.equal(answer.status, 200);
      return (await answer.json()).errors.filter((error) => /^control uses: Cockpit answered 50[234]$/.test(error.message));
    };
    const restarting = await context.newPage();
    await restarting.clock.install();
    const tries = [];
    let reports = 0;
    let away = true;
    let awayStatus = 502;
    await restarting.route("**/api/control-uses", async (route) => {
      const batch = route.request().postDataJSON();
      if (!batch.uses.some((use) => use.kind === "press" && use.control === 'key "y"')) return route.continue();
      tries.push(batch.batch);
      if (away) return route.fulfill({ status: awayStatus, body: "" });
      return route.continue();
    });
    restarting.on("request", (request) => {
      if (request.url().endsWith("/api/page-errors") && request.method() === "POST") reports += 1;
    });
    await restarting.goto(`${info.origin}/usage`);
    await restarting.waitForFunction(() => typeof window.answerIsAway === "function" && document.visibilityState === "visible");
    await restarting.keyboard.press("y");
    const triedAfter = async (count) => {
      for (const end = Date.now() + 15e3; Date.now() < end && tries.length < count; await sleep(50)) {
        await restarting.clock.runFor(15e3);
      }
      assert.ok(tries.length >= count, `the batch was tried ${tries.length} times, expected ${count}`);
    };
    await triedAfter(3);
    await sleep(500);
    assert.equal(reports, 0, "no error report goes out while the proxy answers 502");
    assert.deepEqual((await storedRows()), [], "a restart stores no error");
    console.log("pass: a 502 during a restart stays quiet");
    away = false;
    const accepted = restarting.waitForResponse((response) => response.url().endsWith("/api/control-uses") && response.status() === 200);
    const before = tries.length;
    await triedAfter(before + 1);
    assert.equal((await accepted).status(), 200);
    assert.equal(new Set(tries).size, 1, "every try carries the same batch id");
    assert.deepEqual((await storedRows()), [], "the batch went through with no error stored");
    console.log("pass: the batch goes again under the same id and Cockpit takes it");

    // The two minutes count one stretch away: a good answer starts them over for the next restart.
    await restarting.clock.runFor(135e3);
    away = true;
    awayStatus = 503;
    const againBefore = tries.length;
    await restarting.keyboard.press("y");
    await triedAfter(againBefore + 2);
    await sleep(500);
    assert.equal(reports, 0, "a later restart stays quiet, however long ago the first one was");
    assert.deepEqual((await storedRows()), [], "a later restart stores no error");
    await restarting.close();
    console.log("pass: a good answer starts the quiet time over, and a 503 is quiet too");

    const lasting = await context.newPage();
    await lasting.clock.install();
    await lasting.route("**/api/control-uses", (route) => route.fulfill({ status: 502, body: "" }));
    await lasting.goto(`${info.origin}/usage`);
    await lasting.waitForFunction(() => typeof window.answerIsAway === "function" && document.visibilityState === "visible");
    const lastingReport = lasting.waitForResponse(
      (response) => response.url().endsWith("/api/page-errors") && response.request().method() === "POST"
        && response.request().postDataJSON().message === "control uses: Cockpit answered 502",
      { timeout: 25e3 },
    );
    await lasting.keyboard.press("z");
    await lasting.clock.runFor(15e3);
    await sleep(500);
    assert.deepEqual((await storedRows()), [], "the first refusal stays quiet");
    let reportedSeen = false;
    lastingReport.then(() => { reportedSeen = true; }, () => {});
    let ranMs = 15e3;
    for (const end = Date.now() + 20e3; Date.now() < end && !reportedSeen; await sleep(100)) {
      await lasting.clock.runFor(15e3);
      ranMs += 15e3;
    }
    assert.equal((await lastingReport).status(), 200);
    assert.ok(ranMs >= 120e3, `reported after ${ranMs} ms, before two minutes`);
    const [lastingRow] = await storedRows();
    assert.ok(lastingRow, "a lasting 502 is stored");
    assert.equal(lastingRow.place, "control uses");
    console.log("pass: a batch still refused after two minutes is logged");
    await lasting.close();

    // A request with no answer at all, as when a phone wakes before its network is back: quiet like a 502, and the
    // batch goes again under the same id. Time the browser spends offline does not count towards the two minutes.
    const unreachedRows = async () => {
      const answer = await fetch(`${info.origin}/api/page-errors?all=true`, {
        headers: { authorization: `Bearer ${"t".repeat(40)}` },
        signal: AbortSignal.timeout(10e3),
      });
      assert.equal(answer.status, 200);
      return (await answer.json()).errors.filter((error) => error.message.startsWith("control uses: Failed to fetch"));
    };
    const waking = await context.newPage();
    await waking.clock.install();
    const wakeTries = [];
    let wakeReports = 0;
    let unreached = true;
    await waking.route("**/api/control-uses", (route) => {
      const batch = route.request().postDataJSON();
      if (!batch.uses.some((use) => use.kind === "press" && use.control === 'key "w"')) return route.continue();
      wakeTries.push(batch.batch);
      return unreached ? route.abort("internetdisconnected") : route.continue();
    });
    waking.on("request", (request) => {
      if (request.url().endsWith("/api/page-errors") && request.method() === "POST") wakeReports += 1;
    });
    await waking.goto(`${info.origin}/usage`);
    await waking.waitForFunction(() => typeof window.awayClock === "function" && document.visibilityState === "visible");
    await waking.keyboard.press("w");
    const wakeTriedAfter = async (count) => {
      for (const end = Date.now() + 15e3; Date.now() < end && wakeTries.length < count; await sleep(50)) {
        await waking.clock.runFor(15e3);
      }
      assert.ok(wakeTries.length >= count, `the batch was tried ${wakeTries.length} times, expected ${count}`);
    };
    await wakeTriedAfter(3);
    await sleep(500);
    assert.equal(wakeReports, 0, "no error report goes out for a request with no answer");
    assert.deepEqual(await unreachedRows(), [], "a failed request stores no error at first");
    console.log("pass: a request with no answer stays quiet");

    // The phone sleeps offline for ten minutes, then wakes and the first send still fails: still quiet.
    // The page must see each change before the clock moves, or the ten minutes would count as online.
    await context.setOffline(true);
    await waking.waitForFunction(() => !navigator.onLine);
    await waking.evaluate(() => window.awayClock());
    await waking.clock.runFor(600e3);
    await context.setOffline(false);
    await waking.waitForFunction(() => navigator.onLine);
    await waking.evaluate(() => window.awayClock());
    const afterSleep = wakeTries.length;
    await wakeTriedAfter(afterSleep + 1);
    await sleep(500);
    assert.equal(wakeReports, 0, "time offline does not count towards the two minutes");
    assert.deepEqual(await unreachedRows(), []);
    console.log("pass: ten minutes offline do not make a failed send an error");

    unreached = false;
    const wakeAccepted = waking.waitForResponse((response) => response.url().endsWith("/api/control-uses") && response.status() === 200);
    await wakeTriedAfter(wakeTries.length + 1);
    assert.equal((await wakeAccepted).status(), 200);
    assert.equal(new Set(wakeTries).size, 1, "every try carries the same batch id");
    assert.equal(wakeReports, 0);
    assert.deepEqual(await unreachedRows(), [], "the batch went through with no error stored");
    await waking.close();
    console.log("pass: once the network is back the same batch goes through");

    const cutOff = await context.newPage();
    await cutOff.clock.install();
    await cutOff.route("**/api/control-uses", (route) => route.abort("internetdisconnected"));
    await cutOff.goto(`${info.origin}/usage`);
    await cutOff.waitForFunction(() => typeof window.awayClock === "function" && document.visibilityState === "visible");
    const cutOffReport = cutOff.waitForResponse(
      (response) => response.url().endsWith("/api/page-errors") && response.request().method() === "POST"
        && response.request().postDataJSON().message.startsWith("control uses: Failed to fetch"),
      { timeout: 25e3 },
    );
    await cutOff.keyboard.press("v");
    let cutOffSeen = false;
    cutOffReport.then(() => { cutOffSeen = true; }, () => {});
    let cutOffMs = 0;
    for (const end = Date.now() + 20e3; Date.now() < end && !cutOffSeen; await sleep(100)) {
      await cutOff.clock.runFor(15e3);
      cutOffMs += 15e3;
    }
    assert.equal((await cutOffReport).status(), 200);
    assert.ok(cutOffMs >= 120e3, `reported after ${cutOffMs} ms, before two minutes`);
    const [cutOffRow] = await unreachedRows();
    assert.ok(cutOffRow, "a lasting failure is stored");
    assert.equal(cutOffRow.place, "control uses");
    const [, waited] = cutOffRow.message.match(/^control uses: Failed to fetch, with no answer for (\d+) s$/) || [];
    assert.ok(waited, `stored message: ${cutOffRow.message}`);
    // Sends go every 15 s, and the two minutes count only up to Cockpit's last answer to the page's other requests
    // (here its once-a-minute reads) before a send went: the first send after such an answer, two minutes on, is logged.
    assert.ok(Number(waited) >= 120 && Number(waited) < 200, `logged after ${waited} s of failing sends`);
    await cutOff.close();
    console.log("pass: sends with no answer for two minutes, while Cockpit answers the page's other requests, are logged");

    // A network that is dead while the browser still says it is online and the page is on screen (a wifi with no way
    // out): no request of the page gets an answer for five and a half minutes, then the network is back and the
    // requests left hanging break off. Neither the counts nor the page's refresh are an error, and the counts arrive.
    const dead = await context.newPage();
    await dead.clock.install();
    const deadReports = [];
    const held = [];
    let cut = false;
    let deadAccepted = false;
    await dead.route("**/api/**", (route) => {
      const request = route.request();
      if (request.url().endsWith("/api/page-errors") && request.method() === "POST") deadReports.push(request.postDataJSON().message);
      if (cut) {
        held.push(route);
        return undefined;
      }
      return route.continue();
    });
    // The refresh gives up while the network is dead; it is shown in the console, not logged.
    let refreshGaveUp = false;
    dead.on("console", (message) => {
      if (cut && message.type() === "error" && /no connection to Cockpit in \d+ s of tries/.test(message.text())) refreshGaveUp = true;
    });
    dead.on("response", (response) => {
      const request = response.request();
      if (response.url().endsWith("/api/control-uses") && response.status() === 200
        && request.postDataJSON().uses.some((use) => use.control === 'key "q"')) deadAccepted = true;
    });
    // Requests of the page still waiting for their answer: one answered after the cut began would show the page a
    // working connection, so the network dies only once none waits.
    const waiting = new Set();
    dead.on("request", (request) => waiting.add(request));
    for (const done of ["requestfinished", "requestfailed"]) dead.on(done, (request) => waiting.delete(request));
    await dead.goto(`${info.origin}/`);
    await dead.waitForFunction(() => typeof window.awayClock === "function" && document.visibilityState === "visible");
    await dead.clock.runFor(10e3);
    await dead.clock.pauseAt(await dead.evaluate(() => Date.now() + 1000));
    for (const end = Date.now() + 10e3; !cut; await sleep(100)) {
      assert.ok(Date.now() < end, "the page's requests before the cut got their answers");
      const idle = await dead.evaluate(() => !ui.refreshing && !ui.refreshAgain);
      if (!waiting.size && idle) cut = true;
    }
    const answeredBeforeCut = await dead.evaluate(() => awayClock().answered);
    // Pressed once the network is dead, so its count has to wait for it.
    await dead.keyboard.press("q");
    for (let ran = 0; ran < 330e3; ran += 15e3) {
      await dead.clock.runFor(15e3);
      await sleep(50);
      assert.deepEqual(await dead.evaluate(() => awayClock().answered), answeredBeforeCut,
        "the page received no answer during the network cut");
    }
    assert.ok(await dead.evaluate(() => navigator.onLine && document.visibilityState === "visible"), "the page stayed online and visible");
    assert.ok(held.some((route) => route.request().url().endsWith("/api/control-uses")), "the count sends hung");
    assert.ok(held.some((route) => route.request().url().endsWith("/api/state")), "the page's refresh hung");
    assert.ok(refreshGaveUp, "the page's refresh gave up during the cut");
    cut = false;
    for (const route of held.splice(0)) await route.abort("connectionreset").catch(() => {});
    for (let ran = 0; ran < 90e3 && !deadAccepted; ran += 15e3) {
      await dead.clock.runFor(15e3);
      await sleep(300);
    }
    // Repeats of an error go a minute after its first report.
    await dead.clock.runFor(65e3);
    await sleep(1000);
    assert.ok(deadAccepted, "the counts arrive once the network is back");
    assert.deepEqual(deadReports, [], "a dead network is no error of the page");
    assert.deepEqual((await unreachedRows()).map((row) => row.id), [cutOffRow.id], "no new count error is stored");
    await dead.close();
    console.log("pass: five and a half minutes on a dead network while online log nothing, and the counts arrive");

    // The spec page shows well over a thousand controls, so its seen controls go in batches of about 32 KB. A page's
    // keepalive requests share 64 KiB, and Chromium keeps a request's body counted there until the page reads the
    // answer: two batches sent with keepalive and answers left unread made the browser refuse every batch after them.
    const specRows = () => JSON.parse(execFileSync(PYTHON, ["-c", `
import json, sqlite3, sys
from pathlib import Path
with sqlite3.connect(Path(sys.argv[1]).as_uri() + "?mode=ro", uri=True) as conn:
    print(json.dumps({
        "seen": sorted(row[0] for row in conn.execute("SELECT control FROM control_uses WHERE page = '/spec' AND kind = 'seen'")),
        "batches": sorted(row[0] for row in conn.execute("SELECT id FROM control_batches")),
    }))
`, path.join(work, "cockpit.db")], { encoding: "utf8", timeout: 5e3 }));
    // Whether each count send asks to outlive the page, in the order sent.
    const keptAlive = () => {
      const fetchAs = window.fetch;
      window.keptAlive = [];
      window.fetch = (url, init) => {
        if (String(url).endsWith("/api/control-uses")) window.keptAlive.push(Boolean(init && init.keepalive));
        return fetchAs(url, init);
      };
    };
    const isSend = (request) => request.url().endsWith("/api/control-uses") && request.method() === "POST";
    const watchBatches = (spec) => {
      const sent = [];
      const errors = [];
      const watch = { sent, errors, answered: 0 };
      spec.on("request", (request) => { if (isSend(request)) sent.push(request.postDataJSON()); });
      spec.on("requestfinished", (request) => { if (isSend(request)) watch.answered += 1; });
      spec.on("console", (message) => { if (message.type() === "error" && /control uses/.test(message.text())) errors.push(message.text()); });
      return watch;
    };
    // Cockpit stored every batch sent and the page has its answers. A send while one is still on its way goes under
    // the same id, so on a slow machine a test step waits for both.
    const settled = async (watch, timeoutMs) => {
      for (const end = Date.now() + timeoutMs; Date.now() < end; await sleep(200)) {
        if (watch.answered === watch.sent.length && await batchesIn(watch.sent.map((batch) => batch.batch), 0)) return true;
      }
      return false;
    };
    const batchesIn = async (ids, timeoutMs) => {
      for (const end = Date.now() + timeoutMs; ; await sleep(200)) {
        if (ids.every((id) => specRows().batches.includes(id))) return true;
        if (Date.now() >= end) return false;
      }
    };
    // Over a thousand controls: too many for two batches. Once drawn, the page's clock stands still but for the
    // test's steps; a slow draw may let one batch go before.
    const specDrawn = async (spec) => {
      await spec.waitForFunction(() => typeof window.awayClock === "function" && document.visibilityState === "visible");
      await spec.waitForFunction(() => document.querySelectorAll("button, a[href], summary, [data-tip]").length > 1000, null, { polling: 200, timeout: 30e3 });
      await spec.clock.pauseAt(Date.now() + 2e3);
      await spec.clock.runFor(6e3);
    };
    // While the page shows, a batch goes every 15 s.
    const spec = await context.newPage();
    await spec.addInitScript(keptAlive);
    await spec.clock.install();
    const shown = watchBatches(spec);
    await spec.goto(`${info.origin}/spec`);
    await specDrawn(spec);
    assert.ok(await settled(shown, 10e3), "Cockpit took and answered any batch sent while the page drew");
    for (let sends = 0; sends < 6; sends++) {
      await spec.clock.runFor(15e3);
      await sleep(300);
      assert.ok(await settled(shown, 10e3), `Cockpit took and answered the ${shown.sent.length}. batch`);
    }
    const shownIds = shown.sent.map((batch) => batch.batch);
    assert.ok(new Set(shownIds).size >= 3, `the seen controls went in ${new Set(shownIds).size} batches`);
    assert.deepEqual(shownIds.filter((id, at) => shownIds.indexOf(id) !== at), [], "no batch went twice");
    const sentSeen = [...new Set(shown.sent.flatMap((batch) => batch.uses.filter((use) => use.kind === "seen").map((use) => use.control)))].sort();
    assert.deepEqual(specRows().seen, sentSeen, "every seen control of the spec page is stored");
    assert.deepEqual(shown.errors, [], "no count error on the spec page");
    assert.deepEqual(await spec.evaluate(() => window.keptAlive), shownIds.map(() => false), "a send while the page shows is not kept alive");
    await spec.close();
    console.log(`pass: the spec page's ${shownIds.length} batches of seen controls all arrive while it shows`);

    // Each time the page is hidden it sends a batch that may outlive it (keepalive); the answers it reads leave room
    // for the next.
    const hiding = await context.newPage();
    await hiding.addInitScript(keptAlive);
    await hiding.clock.install();
    const hidden = watchBatches(hiding);
    await hiding.goto(`${info.origin}/spec`);
    await hiding.evaluate(() => {
      let state = "visible";
      Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
      Object.defineProperty(document, "hidden", { configurable: true, get: () => state === "hidden" });
      window.showAs = (next) => { state = next; document.dispatchEvent(new Event("visibilitychange")); };
    });
    await specDrawn(hiding);
    await settled(hidden, 10e3);
    const early = hidden.sent.length;
    for (let hides = 0; hides < 3; hides++) {
      const before = hidden.sent.length;
      await hiding.evaluate(() => window.showAs("hidden"));
      await sleep(300);
      assert.equal(hidden.sent.length, before + 1, `hiding the page the ${hides + 1}. time sends a batch`);
      assert.ok(await settled(hidden, 10e3), `Cockpit took and answered the batch sent on hiding the ${hides + 1}. time`);
      await hiding.evaluate(() => window.showAs("visible"));
    }
    assert.equal(new Set(hidden.sent.slice(early).map((batch) => batch.batch)).size, 3, "each hidden send was a new batch");
    assert.deepEqual((await hiding.evaluate(() => window.keptAlive)).slice(early), [true, true, true], "each send as the page hides is kept alive");
    assert.deepEqual(hidden.errors, [], "no count error on hiding the page");
    await hiding.close();
    console.log("pass: three batches sent as the spec page hides, each kept alive, all arrive");
  } finally {
    try {
      if (browser) await browser.close();
    } finally {
      if (server.exitCode === null && server.signalCode === null) {
        const gone = new Promise((resolve) => server.once("exit", resolve));
        server.kill();
        await Promise.race([gone, sleep(3e3)]);
        if (server.exitCode === null && server.signalCode === null) server.kill("SIGKILL");
        await gone;
      }
    }
  }
}

async function harnessUp(work, server) {
  const file = path.join(work, "cookie.json");
  for (const end = Date.now() + 60e3; Date.now() < end; await sleep(200)) {
    if (server.exitCode !== null) throw new Error(`serve_preview.py exited with ${server.exitCode}`);
    if (!fs.existsSync(file)) continue;
    const info = JSON.parse(fs.readFileSync(file, "utf8"));
    if (await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false)) return info;
  }
  throw new Error("serve_preview.py did not come up within 60 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
