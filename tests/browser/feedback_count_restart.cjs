// The feedback count on Cockpit's other tabs (SPEC FEEDBACK-9) waits through a restart of Cockpit (CODE-5). Here the
// count's read meets a 502, then a broken connection, then Cockpit's answer: the key shows the count and no error is
// logged. A real failure, a 500, is still logged at once. A read that waits through a restart while the user comes back
// to the tab never lands after the newer read.
//
// Uses the isolated fixture serve_keyboard.py (temporary database, fake sessions, no live data).
// Usage, from the checkout: node tests/browser/feedback_count_restart.cjs
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const { chromium } = require(PLAYWRIGHT);

const root = path.resolve(__dirname, "../..");
const work = workFolder("cockpit-feedback-count-");
const server = spawn(path.join(root, ".venv/bin/python"), [path.join(__dirname, "serve_keyboard.py"), work], {
  cwd: root, stdio: ["ignore", "pipe", "pipe"],
});
let serverLog = "";
server.stdout.on("data", (data) => { serverLog += data; });
server.stderr.on("data", (data) => { serverLog += data; });
const wait = (ms) => new Promise((done) => setTimeout(done, ms));

let failures = 0;
function check(ok, words, detail) {
  console.log(`${ok ? "ok  " : "FAIL"} ${words}${ok ? "" : `: ${detail}`}`);
  if (!ok) failures += 1;
}

// Opens /pages with the count's reads answered in turn by `answers` (502, "abort" for a broken connection, or a
// list to send as Cockpit's answer; the last one repeats; {held: list} answers with the list once `release()` is
// called). Gives back the page, the reads made, the errors logged and `release`.
async function open(context, origin, answers) {
  const page = await context.newPage();
  const reads = [];
  const logged = [];
  let release = () => {};
  const held = new Promise((done) => { release = done; });
  await page.route("**/api/feedback", async (route) => {
    if (route.request().method() !== "GET") { await route.continue(); return; }
    let answer = answers[Math.min(reads.length, answers.length - 1)];
    reads.push(answer);
    if (answer && answer.held) {
      await held;
      answer = answer.held;
    }
    if (answer === "abort") await route.abort("connectionreset");
    else if (typeof answer === "number") await route.fulfill({ status: answer, contentType: "text/html", body: "<h1>Bad gateway</h1>" });
    else await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(answer) });
  });
  await page.route("**/api/page-errors", async (route) => {
    logged.push(JSON.parse(route.request().postData() || "{}"));
    await route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
  });
  await page.goto(`${origin}/pages`);
  return { page, reads, logged, release };
}

const countShown = (page) => page.evaluate(() => {
  const el = document.querySelector("#feedback-mode .feedback-count");
  return el && !el.hidden ? el.textContent : null;
});

(async () => {
  let browser;
  try {
    const runtime = path.join(work, "runtime.json");
    for (let at = 0; !fs.existsSync(runtime); at++) {
      if (at > 300 || server.exitCode !== null) throw new Error(`Fixture failed: ${serverLog}`);
      await wait(100);
    }
    const { origin, cookie } = JSON.parse(fs.readFileSync(runtime, "utf8"));
    for (let at = 0; ; at++) {
      try { if ((await fetch(`${origin}/healthz`)).ok) break; } catch { /* not up yet */ }
      if (at > 100) throw new Error(`Fixture did not listen: ${serverLog}`);
      await wait(100);
    }
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ extraHTTPHeaders: { Cookie: cookie }, viewport: { width: 1280, height: 900 } });

    // A deploy: Caddy answers 502, then the connection breaks as Cockpit comes up, then Cockpit answers.
    const restart = await open(context, origin, [502, "abort", [{ id: 1 }, { id: 2 }]]);
    let shown = null;
    for (const end = Date.now() + 10e3; Date.now() < end && shown === null; await wait(100)) shown = await countShown(restart.page);
    check(shown === "2", "after a 502 and a broken connection, the key shows the count Cockpit answered", `shown ${shown}, reads ${JSON.stringify(restart.reads)}`);
    check(restart.reads.length === 3, "the count was read three times", JSON.stringify(restart.reads));
    // An error goes to the log at once (errors.js, `flush`); give one a moment to arrive.
    await wait(500);
    const restartLogged = restart.logged.filter((report) => /unread feedback/.test(report.place));
    check(restartLogged.length === 0, "no error is logged for the restart", JSON.stringify(restartLogged));
    await restart.page.close();

    // A real failure: Cockpit answers 500, which is no restart.
    const broken = await open(context, origin, [500]);
    let brokenLogged = [];
    for (const end = Date.now() + 10e3; Date.now() < end && !brokenLogged.length; await wait(100)) {
      brokenLogged = broken.logged.filter((report) => /unread feedback/.test(report.place));
    }
    check(brokenLogged.length === 1 && /Cockpit answered 500/.test(brokenLogged[0].message),
      "a 500 is logged as an error on reading the unread feedback", JSON.stringify(broken.logged));
    check(broken.reads.length === 1, "a 500 is read once, not tried again", JSON.stringify(broken.reads));
    check(await countShown(broken.page) === null, "after a 500 the key shows no count", await countShown(broken.page));
    await broken.page.close();

    // Overlapping reads: the first read meets a 502 and its next try waits; meanwhile the user comes back to the tab,
    // whose read finds one note. The older answer (2) must not land after it: the key ends on 1.
    const overlap = await open(context, origin, [502, { held: [{ id: 1 }, { id: 2 }] }, [{ id: 1 }]]);
    await overlap.page.evaluate(() => {
      window.countsShown = [];
      const el = document.querySelector("#feedback-mode .feedback-count");
      new MutationObserver(() => window.countsShown.push(el.textContent)).observe(el, { childList: true, characterData: true, subtree: true });
    });
    for (const end = Date.now() + 10e3; Date.now() < end && overlap.reads.length < 2; await wait(100)) { /* the held try */ }
    await overlap.page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
    await wait(500);
    overlap.release();
    let counts = [];
    for (const end = Date.now() + 15e3; Date.now() < end && !(counts.includes("1") && counts.includes("2")); await wait(100)) {
      counts = await overlap.page.evaluate(() => window.countsShown);
    }
    check(counts.at(-1) === "1" && await countShown(overlap.page) === "1",
      "a read held through a restart does not bring back an older count", `counts shown ${JSON.stringify(counts)}, reads ${JSON.stringify(overlap.reads)}`);
    check(overlap.reads.length === 3, "coming back to the tab during a read reads once more after it", JSON.stringify(overlap.reads));
  } catch (error) {
    console.log(`FAIL ${error.stack || error}`);
    failures += 1;
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  console.log(failures ? `${failures} failed` : "all passed");
  process.exit(failures ? 1 : 0);
})();
