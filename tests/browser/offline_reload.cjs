"use strict";

// A reload waits up to 10 s for a slow server instead of showing the saved page, and still falls back offline or on a proxy error.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

const worker = fs.readFileSync(process.env.OFFLINE_WORKER_SOURCE
  || path.join(__dirname, "../../src/cockpit/static/offline-worker.js"), "utf8");
const owner = "b".repeat(64);
const until = Math.floor(Date.now() / 1000) + 3600;

async function main() {
  let running = "Saved Cockpit";
  let delay = 0;
  let status = 200;
  const server = http.createServer((request, response) => {
    const url = new URL(request.url, "http://fixture");
    if (url.pathname === "/offline-worker.js") {
      response.writeHead(200, { "Content-Type": "text/javascript" }).end(worker);
    } else if (url.pathname === "/") {
      const title = running, failing = status;
      if (failing !== 200) {
        setTimeout(() => response.writeHead(failing, { "Content-Type": "text/html" }).end("<h1>Bad gateway</h1>"), delay);
        return;
      }
      setTimeout(() => response.writeHead(200, {
        "Content-Type": "text/html; charset=utf-8",
        "X-Cockpit-Offline-User": owner, "X-Cockpit-Offline-Until": String(until),
      }).end(`<!doctype html><meta charset=utf-8><h1>${title}</h1>`), delay);
    } else {
      response.writeHead(200, { "Content-Type": "text/html" }).end("<h1>Online fixture</h1>");
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  let browser;
  try {
    browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE });
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.goto(origin + "/client");
    const result = await page.evaluate(async ({ user, until }) => {
      await navigator.serviceWorker.register("/offline-worker.js");
      const registration = await navigator.serviceWorker.ready;
      const channel = new MessageChannel();
      return new Promise((resolve, reject) => {
        const timeout = setTimeout(() => reject(new Error("Offline save did not answer")), 10000);
        channel.port1.onmessage = ({ data }) => {
          if (data.progress) return;
          clearTimeout(timeout);
          channel.port1.close();
          resolve(data);
        };
        registration.active.postMessage({ cockpitOffline: "prepare", user, until, supportsProgress: true }, [channel.port2]);
      });
    }, { user: owner, until });
    assert.deepEqual(result, { ok: true }, "The page must be saved before the checks");

    // A deploy: the server now runs a newer Cockpit and answers more slowly than the worker's 1.5 s wait.
    running = "Newer Cockpit";
    delay = 2500;
    await page.goto(origin + "/");
    assert.equal(await page.locator("h1").textContent(), "Saved Cockpit", "A plain slow open still shows the saved page");
    await page.reload();
    assert.equal(await page.locator("h1").textContent(), "Newer Cockpit", "Reload must wait for the running Cockpit");
    // The page's own Reload key calls location.reload(), as the tabs open before this change do.
    await page.goto(origin + "/");
    await Promise.all([page.waitForNavigation(), page.evaluate(() => location.reload())]);
    assert.equal(await page.locator("h1").textContent(), "Newer Cockpit", "The page's Reload must wait for the running Cockpit");
    // Safari's reload has no mark the worker can read, so the page's Reload key announces it; an open stands in here.
    await page.goto(origin + "/");
    await page.evaluate(() => new Promise((resolve) => {
      const pipe = new MessageChannel();
      pipe.port1.onmessage = resolve;
      navigator.serviceWorker.controller.postMessage({ cockpitOffline: "reloading" }, [pipe.port2]);
    }));
    await page.goto(origin + "/");
    assert.equal(await page.locator("h1").textContent(), "Newer Cockpit", "An announced reload must wait for the running Cockpit");
    await page.goto(origin + "/");
    assert.equal(await page.locator("h1").textContent(), "Saved Cockpit", "The announcement covers only the next open");

    // A server that stalls past the reload's longer wait: the saved page shows rather than an endless wait.
    delay = 12000;
    const stalled = Date.now();
    await page.reload();
    assert.equal(await page.locator("h1").textContent(), "Saved Cockpit", "A stalled reload shows the saved page");
    const waited = Date.now() - stalled;
    assert.ok(waited > 9500 && waited < 11500, `The saved page shows after the reload's 10 s wait (took ${waited} ms)`);
    await new Promise((resolve) => setTimeout(resolve, 2500));

    // The proxy answers 502 while Cockpit restarts: the saved page is shown in place of the error, early or late.
    status = 502;
    delay = 0;
    await page.reload();
    assert.equal(await page.locator("h1").textContent(), "Saved Cockpit", "A fast 502 on reload shows the saved page");
    delay = 2500;
    await page.reload();
    assert.equal(await page.locator("h1").textContent(), "Saved Cockpit", "A late 502 on reload shows the saved page");
    await page.goto(origin + "/");
    assert.equal(await page.locator("h1").textContent(), "Saved Cockpit", "A late 502 on an open shows the saved page");

    status = 200;
    delay = 0;
    await context.setOffline(true);
    await page.reload();
    assert.equal(await page.locator("h1").textContent(), "Saved Cockpit", "Reload offline still shows the saved page");
    console.log("pass: Reload waits for a slow server and falls back to the saved page offline or on a proxy error");
  } finally {
    if (browser) await browser.close();
    server.close();
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
