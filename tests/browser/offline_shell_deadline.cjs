"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { gzipSync } = require("node:zlib");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

const worker = fs.readFileSync(process.env.OFFLINE_WORKER_SOURCE
  || path.join(__dirname, "../../src/cockpit/static/offline-worker.js"), "utf8");
const clock = `
const originalTimeout = AbortSignal.timeout;
let shellDeadline;
let downloads = 0;
AbortSignal.timeout = (ms) => {
  downloads++;
  if (downloads === 1) {
    shellDeadline = new AbortController();
    return shellDeadline.signal;
  }
  if (downloads === 2) shellDeadline.abort(new DOMException("The download deadline elapsed", "TimeoutError"));
  return originalTimeout(ms);
};
`;
const owner = "a".repeat(64);
const until = Math.floor(Date.now() / 1000) + 3600;
const title = "✓ Offline page";
const policy = "default-src 'self'";
const shell = gzipSync(`<!doctype html><meta charset=utf-8><h1>${title}</h1><script src="/static/held.js?v=test"></script>`);

async function main() {
  const server = http.createServer((request, response) => {
    if (request.url === "/offline-worker.js") {
      response.writeHead(200, { "Content-Type": "text/javascript" }).end(worker + "\n" + clock);
    } else if (request.url === "/static/held.js?v=test") {
      response.writeHead(200, { "Content-Type": "text/javascript", "Cache-Control": "public, immutable" })
        .end('document.documentElement.dataset.assetLoaded = "yes";');
    } else if (request.url === "/") {
      response.writeHead(200, {
        "Content-Type": "text/html; charset=utf-8", "Content-Encoding": "gzip", "Content-Length": shell.length,
        "Content-Security-Policy": policy, "X-Cockpit-Offline-User": owner, "X-Cockpit-Offline-Until": String(until),
      }).end(shell);
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
    assert.deepEqual(result, { ok: true }, "The complete page must stay cacheable after its download signal is aborted");
    const saved = await page.evaluate(async ({ owner, until }) => {
      const cache = await caches.open(`cockpit-shell-v1-${owner}-${until}`);
      const response = await cache.match("/");
      return { encoding: response.headers.get("Content-Encoding"), length: response.headers.get("Content-Length"),
        policy: response.headers.get("Content-Security-Policy"), owner: response.headers.get("X-Cockpit-Offline-User") };
    }, { owner, until });
    assert.deepEqual(saved, { encoding: null, length: null, policy, owner });
    await context.setOffline(true);
    await page.goto(origin + "/");
    assert.equal(await page.locator("h1").textContent(), title);
    assert.equal(await page.locator("html").getAttribute("data-asset-loaded"), "yes");
    console.log("pass: decoded HTML and its script load offline after the original download deadline");
  } finally {
    if (browser) await browser.close();
    server.close();
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
