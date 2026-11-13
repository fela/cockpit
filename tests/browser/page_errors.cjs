// Errors on Cockpit's pages reach the server's error log (SPEC CODE-2), and a loop of errors stays cheap.
// Each page loads without an error. On the main page, an uncaught error and an unhandled rejection
// each reach the log; a loop of 500 throws of one error sends one report at once and the rest as one
// count a minute later; 40 different errors send at most 30 reports; Cockpit's refusals (a 409) stay
// off the log and its failures (a 500) go on it. A poll during a restart of Cockpit (502s, or refused
// connections) waits it out with no error, also 40 s offline or hidden, and an outage past about 30 s is
// logged once. A poll whose
// answer breaks off mid-body tries again with no error, and a success that is not JSON shows and is logged.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what
// it needs) and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
// A run takes about five minutes: the repeats of an error go a minute after the first report, and the
// offline, hidden and outage checks wait 40 s each.
//
// Usage, from the checkout: node tests/browser/page_errors.cjs

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
// The harness's agent token (tests/conftest.py).
const TOKEN = "t".repeat(40);

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-errors-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    const logged = async () => {
      const response = await fetch(`${info.origin}/api/page-errors?all=true`, { headers: { authorization: `Bearer ${TOKEN}` } });
      return response.json();
    };
    const before = new Map((await logged()).errors.map((error) => [error.id, error.count]));
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    const page = await context.newPage();
    let reports = [];
    page.on("request", (request) => {
      if (request.url().endsWith("/api/page-errors") && request.method() === "POST") reports.push(JSON.parse(request.postData()));
    });

    for (const url of ["/", "/spec", "/briefing", "/usage", "/skills"]) {
      await page.goto(`${info.origin}${url}`);
      await page.waitForLoadState("networkidle");
      await sleep(1500);
    }
    // Only errors produced after this check starts count toward its assertions.
    const added = (await logged()).errors.filter((error) => (before.get(error.id) ?? 0) < error.count);
    check("the pages load without an error", added.length === 0 && reports.length === 0,
      JSON.stringify(added.map((error) => [error.page, error.place, error.message])));

    await page.goto(`${info.origin}/classic`);
    await page.waitForLoadState("networkidle");
    reports = [];
    await page.evaluate(() => {
      setTimeout(() => { throw new Error("probe: uncaught"); });
      Promise.reject(new Error("probe: rejected"));
    });
    await sleep(1000);
    let errors = (await logged()).errors;
    check("an uncaught error reaches the log",
      errors.some((error) => error.message === "probe: uncaught" && error.place.startsWith("uncaught in") && error.page === "/classic"),
      JSON.stringify(errors));
    check("an unhandled rejection reaches the log",
      errors.some((error) => error.message === "probe: rejected" && error.place === "unhandled rejection" && error.stack.includes("Error")),
      JSON.stringify(errors));

    reports = [];
    await page.evaluate(() => { for (let n = 0; n < 500; n += 1) logError("probe loop", new Error(`loop ${n}`)); });
    await sleep(1000);
    check("a loop of 500 errors sends one report at once", reports.length === 1 && reports[0].count === 1, JSON.stringify(reports));
    console.error("  waiting a minute for the loop's repeats");
    await sleep(62e3);
    const loop = (await logged()).errors.find((error) => error.place === "probe loop");
    check("the loop's 499 repeats arrive a minute later as one report", reports.length === 2 && loop && loop.count === 500,
      JSON.stringify({ count: loop && loop.count, reports: reports.length }));

    await page.evaluate(() => {
      const refusal = Object.assign(new Error("step 3 is done"), { status: 409 });
      logError("probe refusal", new Error("Not sent: the step was already done", { cause: refusal }));
      logError("probe failure", Object.assign(new Error("Cockpit answered 500"), { status: 500 }));
    });
    await sleep(1000);
    errors = (await logged()).errors;
    check("Cockpit's refusals stay off the log", !errors.some((error) => error.place === "probe refusal"), JSON.stringify(errors));
    check("Cockpit's failures go on it", errors.some((error) => error.place === "probe failure"), JSON.stringify(errors));

    // A restart of Cockpit: Caddy answers 502 for about a second, and without Caddy the connection fails.
    // The page's poll waits it out with no error.
    for (const [name, away] of [["answers 502", (route) => route.fulfill({ status: 502, body: "" })],
      ["refuses the connection", (route) => route.abort("connectionrefused")]]) {
      reports = [];
      let refused = 0;
      await page.route("**/api/state", (route) => (refused++ < 3 ? away(route) : route.continue()));
      await sleep(12e3);
      await page.unroute("**/api/state");
      const state = await page.evaluate(() => ({ error: ui.error, ok: Date.now() - ui.lastOk.getTime() }));
      check(`a poll while Cockpit restarts and ${name} waits it out with no error`,
        refused >= 3 && reports.length === 0 && state.error === "" && state.ok < 10e3,
        JSON.stringify({ refused, reports, state }));
    }
    // Cockpit answers, then the connection breaks off while the page reads the body (a reload, a laptop
    // waking): the poll tries again with no error. Read as {}, such a body would throw on the
    // missing permission_requests.
    reports = [];
    await page.evaluate(() => {
      const real = window.fetch;
      let broken = 0;
      window.probe = { real, broken: () => broken };
      window.fetch = (input, init) => {
        if (!String(input).endsWith("/api/state") || broken >= 2) return real(input, init);
        broken += 1;
        const body = new ReadableStream({
          start(controller) {
            controller.enqueue(new TextEncoder().encode('{"queue": ['));
            controller.error(new TypeError("network error"));
          },
        });
        return Promise.resolve(new Response(body, { status: 200, headers: { "content-type": "application/json" } }));
      };
    });
    await sleep(12e3);
    const brokeOff = await page.evaluate(() => {
      window.fetch = window.probe.real;
      return { broken: window.probe.broken(), error: ui.error, ok: Date.now() - ui.lastOk.getTime(),
        drawn: Array.isArray(ui.state.permission_requests) };
    });
    check("a poll whose answer breaks off mid-body tries again with no error",
      brokeOff.broken === 2 && reports.length === 0 && brokeOff.error === "" && brokeOff.ok < 10e3 && brokeOff.drawn,
      JSON.stringify({ brokeOff, reports }));

    // A success that is not JSON is Cockpit's failure: it shows and is logged, never read as an empty state.
    // One poll gets it: a second one's report would wait out the repeat limit and land in a later check.
    reports = [];
    let served = 0;
    await page.route("**/api/state", (route) => {
      served += 1;
      return route.fulfill({ status: 200, contentType: "text/html", body: "<html>not Cockpit</html>" });
    });
    const pollBy = Date.now() + 15e3;
    while (served === 0) {
      if (Date.now() > pollBy) throw new Error("the page sent no /api/state poll within 15 s");
      await sleep(100);
    }
    await page.unroute("**/api/state");
    await sleep(1e3);
    const notJson = await page.evaluate(() => ui.error);
    check("a success with no JSON shows and is logged",
      notJson.startsWith("Cockpit answered 200 with no JSON") && reports.length === 1 && reports[0].place === "refreshing the page",
      JSON.stringify({ notJson, reports }));

    // A laptop asleep or a network dropped on the user's side says nothing about Cockpit: while the browser
    // is offline, or the page hidden, failed tries do not count, and the poll goes on once it is back.
    reports = [];
    await context.setOffline(true);
    console.error("  waiting 40 s with the browser offline");
    await sleep(40e3);
    await context.setOffline(false);
    await sleep(3e3);
    const offline = await page.evaluate(() => ({ error: ui.error, ok: Date.now() - ui.lastOk.getTime() }));
    check("40 s with the browser offline is no error, and the poll goes on once it is online",
      reports.length === 0 && offline.error === "" && offline.ok < 3e3, JSON.stringify({ offline, reports }));

    reports = [];
    await page.route("**/api/state", (route) => route.abort("connectionrefused"));
    await sleep(6e3);
    await page.evaluate(() => {
      Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    console.error("  waiting 40 s with the page hidden and Cockpit out of reach");
    await sleep(40e3);
    await page.unroute("**/api/state");
    await page.evaluate(() => {
      delete document.hidden;
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await sleep(3e3);
    const hidden = await page.evaluate(() => ({ error: ui.error, ok: Date.now() - ui.lastOk.getTime() }));
    check("40 s out of reach while the page is hidden is no error, and the poll goes on once it shows",
      reports.length === 0 && hidden.error === "" && hidden.ok < 3e3, JSON.stringify({ hidden, reports }));

    // Cockpit away for good, with the page visible and online: after about 30 s of tries it shows and is
    // logged once, as Caddy's 502 or as no connection.
    for (const [name, away, words] of [
      ["answers 502", (route) => route.fulfill({ status: 502, body: "" }), /^Cockpit answered 502$/],
      ["refuses the connection", (route) => route.abort("connectionrefused"),
        /^Failed to fetch: no connection to Cockpit in \d+ s of tries$/]]) {
      reports = [];
      await page.route("**/api/state", away);
      console.error(`  waiting 40 s while Cockpit ${name}`);
      await sleep(40e3);
      const outage = await page.evaluate(() => ui.error);
      await page.unroute("**/api/state");
      check(`an outage past 30 s while Cockpit ${name} shows and is logged once`,
        words.test(outage) && reports.length === 1 && reports[0].place === "refreshing the page",
        JSON.stringify({ outage, reports }));
    }

    // Eight reports went this hour already: the uncaught error, the rejection, the loop's two, the failure,
    // the success with no JSON and the two outages.
    reports = [];
    await page.evaluate(() => { for (let n = 0; n < 40; n += 1) logError(`probe many ${String.fromCharCode(65 + n)}`, new Error("different")); });
    await sleep(1500);
    check("40 different errors send only what is left of 30 reports an hour", reports.length === 22, `${reports.length} reports`);
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failed = results.filter((result) => !result.ok);
  console.error(`${results.length - failed.length} of ${results.length} checks passed`);
  process.exitCode = failed.length ? 1 : 0;
}

// The harness's cookie file, once it is written and the server answers.
async function harnessUp(work, server) {
  const file = path.join(work, "cookie.json");
  const end = Date.now() + 60e3;
  while (Date.now() < end) {
    if (server.exitCode !== null) throw new Error(`serve_preview.py exited with ${server.exitCode}`);
    if (fs.existsSync(file)) {
      const info = JSON.parse(fs.readFileSync(file, "utf8"));
      const up = await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false);
      if (up) return info;
    }
    await sleep(200);
  }
  throw new Error("serve_preview.py did not come up within 60 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
