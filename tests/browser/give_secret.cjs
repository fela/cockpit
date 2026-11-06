// A secret for a step's session (SPEC MSG-15): ⋯ → Give a secret… on a step opens a box with a name and a masked
// value; Give secret writes the value to <work>/secrets/<name> (0600 in a 0700 folder), the step leaves the queue at
// once, and the notice names the secret. The value never reaches the page's storage, nor the message the session gets.
// A bad name is refused on the page before anything is sent; a refusal brings the box back with its fields.
// Screenshots of the box at desktop width in light and at phone width in dark go to the work folder, for the eye.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/give_secret.cjs

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
// The harness's agent token and second fake session (tests/conftest.py).
const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "secret-test", "content-type": "application/json" };
const VALUE = "example-export-key-aaaa111";

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-secret-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    const cookie = { cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json" };
    const agent = (method, url, body) => call(info.origin, AGENT, method, url, body);
    const user = (method, url, body) => call(info.origin, cookie, method, url, body);

    // The fixture database's open steps are snoozed, so this run's step is Next up.
    const copied = await user("GET", "/api/state");
    await Promise.all([...copied.queue, ...copied.ready_next, ...copied.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    const key = `note:secret-${Date.now()}`;
    await agent("PUT", "/api/items", { key, title: "Secret test", priority: "urgent" });
    const step = (await agent("POST", "/api/steps", { item_key: key, need: "secret", ask: "Give the local export preview a test key", score: 100, session_id: SESSION_B })).step;
    await user("POST", `/api/steps/${step.id}/timer/start`);

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: "light" });
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    const page = await context.newPage();
    page.setDefaultTimeout(20000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`${info.origin}/classic`);
    const card = page.locator(`#next [data-step="${step.id}"]`).first();
    // The first load reads the fixture database; on a busy host that takes a while.
    await card.waitFor({ timeout: 90000 });

    // ⋯ → Give a secret… opens the box with the cursor in its name.
    await card.locator("details.step-menu > summary").first().click();
    await card.locator("details.step-menu .menu-list button", { hasText: "Give a secret" }).click();
    const box = card.locator(".secret-box");
    await box.waitFor();
    check("the box opens with the cursor in the name", await page.evaluate(() => document.activeElement?.classList.contains("secret-name")), "focus elsewhere");
    check("the value field is masked", (await box.locator(".secret-value").getAttribute("type")) === "password", "not a password field");
    await box.screenshot({ path: path.join(work, "desktop-light.png") });

    // A bad name is refused on the page, with nothing sent.
    let posted = 0;
    let sent = null;
    page.on("request", (request) => {
      if (!request.url().endsWith(`/api/steps/${step.id}/secret`)) return;
      posted += 1;
      sent = request.postDataJSON();
    });
    await box.locator(".secret-name").fill("bad name");
    await box.locator(".secret-value").fill(VALUE);
    await box.getByRole("button", { name: "Give secret" }).click();
    await sleep(300);
    check("a bad name is refused on the page", posted === 0 && (await page.locator("#toast").textContent()).includes("letters, digits"), `posted ${posted}`);

    // Picked for feedback, the masked value field shows neither its value nor a copy of it (regression check): the
    // feedback box's words and copy are what the feedback stores.
    await page.evaluate(() => document.activeElement && document.activeElement.blur());
    await page.evaluate(() => startPicking("feedback"));
    await box.locator(".secret-value").click();
    await page.waitForSelector("#pick-box:not([hidden]) textarea");
    const picked = await page.evaluate(() => {
      const shown = document.getElementById("pick-box");
      const fields = [...shown.querySelectorAll("input")].map((input) => `${input.value} ${input.getAttribute("value") || ""}`);
      return [shown.innerText, shown.innerHTML, ...fields].join("\n");
    });
    check("picked for feedback, the value field gives no value", !picked.includes(VALUE), "the value shows in the feedback box");
    // Picking goes on after a pick (a click guesses again): Escape ends it, then Close closes the box. Closing starts
    // the step's timer again, and the card draws anew with the server's answer.
    await page.keyboard.press("Escape");
    const timer = page.waitForResponse((response) => response.url().includes(`/api/steps/${step.id}/timer`));
    await page.locator("#pick-box").getByRole("button", { name: "Close" }).click();
    await timer;
    await sleep(1000);
    await box.waitFor();
    check("the box keeps its value after the pick", (await box.locator(".secret-value").inputValue()) === VALUE, "the value is gone");

    // A GPU close of the card (close-gpu.js) paints the value field as the page shows it, masked, never its value
    // (regression check): the close copies the card, the copy's field holds the value, and the painter draws its words.
    const gpu = await page.waitForFunction(() => window.GpuCloses.ready(), null, { timeout: 30000 }).then(() => true, () => false);
    if (gpu) {
      const painted = await card.evaluate(async (el, value) => {
        const words = [];
        const fillText = CanvasRenderingContext2D.prototype.fillText;
        CanvasRenderingContext2D.prototype.fillText = function (text, ...rest) { words.push(String(text)); return fillText.call(this, text, ...rest); };
        const random = Math.random;
        lastClose = null;
        Math.random = () => (Object.keys(CLOSES).indexOf("burn") + 0.5) / Object.keys(CLOSES).length;
        try { closeAnimation(el, null, false); } finally { Math.random = random; }
        await new Promise((resolve) => setTimeout(resolve, 600));
        CanvasRenderingContext2D.prototype.fillText = fillText;
        return { leaked: words.some((word) => word.includes(value)), masked: words.some((word) => /^•+$/.test(word)), count: words.length };
      }, VALUE);
      check("a GPU close paints the value field masked", !painted.leaked && painted.masked, JSON.stringify(painted));
    } else {
      console.error("  skip a GPU close paints the value field masked: no WebGL2 in this Chromium");
    }

    // Phone width, dark.
    await page.setViewportSize({ width: 390, height: 844 });
    await page.emulateMedia({ colorScheme: "dark" });
    await box.scrollIntoViewIfNeeded();
    await box.screenshot({ path: path.join(work, "phone-dark.png") });
    const fits = await box.evaluate((el) => el.scrollWidth <= el.clientWidth + 1 && el.getBoundingClientRect().right <= innerWidth);
    check("at phone width the box fits the screen", fits, "it overflows");
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.emulateMedia({ colorScheme: "light" });

    // A refusal brings the step back with the box open, its name and value, and the reply box's words.
    await page.route(`**/api/steps/${step.id}/secret`, (route) => route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: "refused for the test" }) }));
    await box.locator(".secret-name").fill("refused-key");
    await page.locator(`#next [data-step="${step.id}"] textarea`).first().fill("words to keep");
    await box.getByRole("button", { name: "Give secret" }).click();
    await page.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("Secret not given"));
    await box.waitFor();
    const back = { name: await box.locator(".secret-name").inputValue(), value: await box.locator(".secret-value").inputValue(),
      words: await page.locator(`#next [data-step="${step.id}"] textarea`).first().inputValue() };
    check("a refusal brings back the box, its fields and the reply's words", back.name === "refused-key" && back.value === VALUE
      && back.words === "words to keep", JSON.stringify({ ...back, value: back.value === VALUE }));
    await page.unroute(`**/api/steps/${step.id}/secret`);

    // Give it: the step leaves the queue at once, the notice names the secret, and the file holds the value. The
    // request is held HELD_MS on its way, so a step that leaves only with the server's answer fails.
    const HELD_MS = 4000;
    await page.route(`**/api/steps/${step.id}/secret`, async (route) => { await sleep(HELD_MS); await route.continue(); });
    await box.locator(".secret-name").fill("tasks-preview-key");
    await page.locator(`#next [data-step="${step.id}"] textarea`).first().fill("Use it for the local export preview");
    const pressed = Date.now();
    await box.getByRole("button", { name: "Give secret" }).click();
    await page.waitForFunction((id) => !document.querySelector(`#next [data-step="${id}"]:not(.motion-ghost)`), step.id);
    const left = Date.now() - pressed;
    check("the step leaves Next up before the server answers", left < HELD_MS - 1500, `after ${left} ms`);
    await page.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("tasks-preview-key"));
    check("the notice names the secret", true, "");
    const file = path.join(work, "secrets", "tasks-preview-key");
    const end = Date.now() + 5000;
    while (!fs.existsSync(file) && Date.now() < end) await sleep(100);
    check("the file holds the value", fs.existsSync(file) && fs.readFileSync(file, "utf8") === VALUE, "missing or wrong");
    check("the file is 0600", fs.existsSync(file) && (fs.statSync(file).mode & 0o777) === 0o600, fs.existsSync(file) ? (fs.statSync(file).mode & 0o777).toString(8) : "missing");
    check("the folder is 0700", (fs.statSync(path.dirname(file)).mode & 0o777) === 0o700, (fs.statSync(path.dirname(file)).mode & 0o777).toString(8));
    const stored = await page.evaluate(() => Object.keys(localStorage).map((k) => localStorage.getItem(k)).join("\n"));
    check("the value is nowhere in the page's storage", !stored.includes(VALUE), "found in localStorage");
    check("the reply box's words went with it", sent?.text === "Use it for the local export preview", JSON.stringify(sent?.text));
    const handed = (await user("GET", "/api/state")).with_agents.find((entry) => entry.id === step.id);
    const words = JSON.stringify(handed || null);
    check("the session's message says where the secret is, not what it is", words.includes("stored secret tasks-preview-key")
      && words.includes("Use it for the local export preview") && !words.includes(VALUE), words.slice(0, 400));
    check("no page errors", errors.length === 0, errors.join(" | "));
    console.error(`  screenshots in ${work}`);
    await context.close();
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

async function call(origin, headers, method, url, body) {
  const response = await fetch(`${origin}${url}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await response.text();
  if (!response.ok) throw new Error(`${method} ${url}: ${response.status} ${text}`);
  return text ? JSON.parse(text) : null;
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
