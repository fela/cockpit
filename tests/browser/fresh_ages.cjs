// A history entry's age shows how fresh it is (SPEC VIEW-19). Checks, on a step the user handed off and its session
// gave back, in the light and dark looks at 1440 and 390 px:
// - the current entry's age, a moment old, sits on strong green, as does one a minute ahead of the browser's clock;
// - ages of 1, 5, 20 and 59 minutes fade step by step, and one of 61 minutes has no green;
// - every age reads on its green at 4.5:1 or more, and each green stays inside the history;
// - as time passes the age's words and green follow without a redraw (an age moved to 90 minutes ago loses its green
//   within two seconds);
// - there is no sideways scroll, and there are no page errors.
//
// Starts serve_preview.py (this checkout's Cockpit on a copy of the live database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
// Screenshots go to the folder given as the first argument, when one is given.
//
// Usage, from the checkout: node tests/browser/fresh_ages.cjs [screenshot folder]

"use strict";

const { spawn, spawnSync } = require("node:child_process");
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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "fresh-ages-test", "content-type": "application/json" };
// Ages in minutes; -1 is a minute ahead of the browser's clock.
const MINUTES = [-1, 1, 5, 20, 59, 61];
const MARK_SENT = `
import sqlite3, sys
from datetime import datetime, timezone
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
conn = sqlite3.connect(sys.argv[1])
conn.execute("UPDATE messages SET state = 'sent', sent_at = ?, take_back_until = NULL WHERE id = ?", (now, sys.argv[2]))
conn.commit()
`;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const shots = process.argv[2] || null;
  if (shots) fs.mkdirSync(shots, { recursive: true });
  const work = workFolder("cockpit-fresh-ages-");
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

    // The fixture database's open steps are snoozed and its timer paused, so this run's step is the one on the page.
    const copied = await user("GET", "/api/state");
    if (copied.timer) await user("POST", `/api/steps/${copied.timer.step_id}/timer/pause`);
    await Promise.all([...copied.queue, ...copied.ready_next, ...copied.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));

    const key = `note:fresh-ages-${Date.now()}`;
    await agent("PUT", "/api/items", { key, title: "Fresh ages test", priority: "urgent", sessions: [SESSION_B] });
    const answer = await agent("POST", "/api/steps", {
      item_key: key, need: "decide:fresh-ages", ask: "Fresh ages test", score: 100, session_id: SESSION_B,
      body: "The step the session gave back.",
    });
    const id = answer.step.id;
    const handed = await user("POST", `/api/steps/${id}/hand-off`, { text: "How fresh is this?" });
    // The history counts the message once it went to the session; the harness's fake sessions never confirm one.
    const sent = spawnSync(PYTHON, ["-c", MARK_SENT, path.join(work, "cockpit.db"), String(handed.id)], { encoding: "utf8" });
    check("the hand-off message is marked sent", sent.status === 0, sent.stderr);
    // A handed step comes back with an answer on it (NEXT-37).
    await agent("POST", `/api/steps/${id}/return`, { note: "It was checked a moment ago." });
    // A step card shows its history once its timer ran.
    await user("POST", `/api/steps/${id}/timer/start`);

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
    await page.goto(`${info.origin}/classic`);
    const card = page.locator(`[data-step="${id}"]`).first();
    // The history sits folded under the step (NEXT-52); open it.
    await card.locator(".fold-all > summary").click();
    await card.locator("section.history .hist-entry.current").waitFor();

    for (const [width, look] of [[1440, "light"], [1440, "dark"], [390, "light"], [390, "dark"]]) {
      await page.setViewportSize({ width, height: 900 });
      await page.emulateMedia({ colorScheme: look });
      await sleep(300);
      const where = `${look}, ${width} px`;
      // Fixtures are added and measured in one go: a history redraw would drop them.
      const seen = await page.evaluate(measure, { stepId: id, minutes: MINUTES });
      check(`${where}: the current entry's age sits on strong green`, seen.current.fresh && seen.current.alpha >= 0.5, JSON.stringify(seen.current));
      const [ahead, ...rest] = seen.ages;
      check(`${where}: an age a minute ahead of the clock sits on strong green`, ahead.alpha >= 0.5, JSON.stringify(ahead));
      const fading = rest.slice(0, 4).every((age, index) => index === 0 || age.alpha < rest[index - 1].alpha);
      check(`${where}: ages of 1, 5, 20 and 59 minutes fade step by step`, fading && rest[3].alpha > 0, JSON.stringify(rest));
      check(`${where}: an age of 61 minutes has no green`, rest[4].alpha === 0, JSON.stringify(rest[4]));
      const faint = [seen.current, ...seen.ages].filter((age) => age.contrast < 4.5);
      check(`${where}: every age reads on its green at 4.5:1 or more`, faint.length === 0, JSON.stringify(faint));
      const out = seen.ages.filter((age) => age.out > 0);
      check(`${where}: each green stays inside the history`, out.length === 0, JSON.stringify(out));
      const sideways = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      check(`${where}: no sideways scroll`, sideways <= 0, `${sideways}px wider`);
      if (shots) await card.locator("section.history").screenshot({ path: path.join(shots, `fresh-${look}-${width}.png`) });
    }

    // An age outside the history, which a redraw of the history leaves alone.
    await page.evaluate(() => {
      const el = histWhen(new Date(Date.now() - 5 * 60000).toISOString());
      el.id = "test-tick";
      document.body.append(el);
      el.dataset.ago = new Date(Date.now() - 90 * 60000).toISOString();
    });
    await sleep(2000);
    const moved = await page.evaluate(() => {
      const el = document.getElementById("test-tick");
      return { fresh: el.classList.contains("fresh"), text: el.textContent };
    });
    check("as time passes the age loses its green without a redraw", !moved.fresh && moved.text === "1 h ago", JSON.stringify(moved));

    check("no page errors", errors.length === 0, errors.join(" | "));
    await user("POST", `/api/steps/${id}/timer/pause`);
    await context.close();
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

// Runs in the page: adds an entry per age under the step card's history, as the page draws them, and returns
// for the current entry's age and each added one its green's opacity, its text's contrast on what is behind it
// (every background from the page down, painted in order), and how many px it reaches left of the history.
function measure({ stepId, minutes }) {
  const history = document.querySelector(`[data-step="${stepId}"] section.history`);
  const list = history.querySelector(".hist-list");
  const left = history.getBoundingClientRect().left;
  const probe = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  const paint = (colours) => {
    probe.clearRect(0, 0, 1, 1);
    probe.fillStyle = "#fff";
    probe.fillRect(0, 0, 1, 1);
    for (const colour of colours) {
      probe.fillStyle = colour;
      probe.fillRect(0, 0, 1, 1);
    }
    return [...probe.getImageData(0, 0, 1, 1).data.slice(0, 3)];
  };
  const luminance = (rgb) => {
    const [r, g, b] = rgb.map((v) => { const c = v / 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const alpha = (colour) => {
    const match = colour.match(/\/\s*([\d.]+)\)|rgba\([^)]*,\s*([\d.]+)\)/);
    return match ? Number(match[1] ?? match[2]) : 1;
  };
  const look = (el) => {
    const layers = [];
    for (let node = el; node; node = node.parentElement) layers.unshift(getComputedStyle(node).backgroundColor);
    const behind = paint(layers);
    const text = paint([...layers, getComputedStyle(el).color]);
    const [light, dark] = [luminance(behind), luminance(text)].sort((a, b) => b - a);
    const box = el.getBoundingClientRect();
    return {
      text: el.textContent, fresh: el.classList.contains("fresh"), alpha: alpha(getComputedStyle(el).backgroundColor),
      contrast: Number(((light + 0.05) / (dark + 0.05)).toFixed(2)), out: left - box.left,
    };
  };
  const ages = minutes.map((age) => {
    const el = histWhen(new Date(Date.now() - age * 60000).toISOString());
    list.append(h("li", { class: "hist-entry compact" }, el, h("div", { class: "hist-body" }, h("span", { class: "hist-text", text: `${age} minutes old` }))));
    return { age, ...look(el) };
  });
  return { current: look(history.querySelector(".hist-entry.current .hist-when")), ages };
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
