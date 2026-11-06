// A Codex session's conversation in the session view of the Cockpit page (SPEC VIEW-26, regression check). Marks the
// harness's second fake session as a Codex one in the page's state responses and answers its log with a Codex
// thread's parts, then checks, at desktop width and at phone width:
// - opening the session's view asks for its conversation and shows it: the user's message, the command folded into
//   a line of calls, and Codex's words as Markdown;
// - no note says the conversation of a Codex session does not show;
// - when more was read than shown, the first line says Codex keeps the earlier entries;
// - no sideways scroll and no page errors.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs)
// and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/codex_log.cjs

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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "codex-log-test", "content-type": "application/json" };

// The parts CodexAgent.log gives for a short thread, oldest first.
const LOG = {
  total: 80,
  parts: [
    { at: "2026-01-15T10:00:00Z", kind: "instruction", author: "user", text: "Run the tests (Codex log check)" },
    { at: "2026-01-15T10:00:02Z", kind: "call", id: "call-1", tool_name: "Bash", title: "", text: "pytest -q", result: "1 failed", error: true },
    { at: "2026-01-15T10:00:02Z", kind: "call", id: "call-2", tool_name: "Bash", title: "", text: "git status", result: "clean", error: false },
    { at: "2026-01-15T10:00:04Z", kind: "says", text: "One test **fails**", html: "<p>One test <strong>fails</strong></p>" },
  ],
};

const { chromium } = require(PLAYWRIGHT);

async function main() {
  const work = workFolder("cockpit-codex-log-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    const agent = (method, url, body) => call(info.origin, AGENT, method, url, body);
    const key = `note:codex-log-${Date.now()}`;
    const item = await agent("PUT", "/api/items", { key, title: "Codex log test", priority: "urgent", sessions: [SESSION_B] });
    browser = await chromium.launch({ executablePath: CHROMIUM });

    for (const width of [1280, 390]) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, isMobile: width < 600, hasTouch: width < 600 });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.setDefaultTimeout(90000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.route("**/api/state*", async (route) => {
        const response = await route.fetch().catch(() => null);
        if (!response) return;
        const body = await response.json();
        const known = body.sessions.find((session) => session.session_id === SESSION_B);
        if (known) known.provider = "codex";
        else body.sessions.push({ session_id: SESSION_B, id: SESSION_B.slice(0, 8), name: "codex test", provider: "codex", state: "done" });
        await route.fulfill({ response, json: body }).catch(() => {});
      });
      let asked = 0;
      await page.route(`**/api/sessions/${SESSION_B}/log`, (route) => {
        asked += 1;
        // Each later read has one more line, so the view draws it anew (it skips an unchanged log).
        const more = Array.from({ length: asked - 1 }, (_, n) => ({ at: "2026-01-15T10:01:00Z", kind: "says", text: `Still working (${n})` }));
        const body = { total: LOG.total + more.length, parts: [...LOG.parts, ...more] };
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
      });

      await page.goto(`${info.origin}/`);
      await page.locator(`#sidebar li[data-item="${item.id}"]`).first().waitFor({ state: "attached" });
      await page.evaluate(([id, itemId]) => window.openSessionView(id, itemId), [SESSION_B, item.id]);
      const log = page.locator("#session-log");
      await log.locator(".log-says").first().waitFor({ timeout: 15000 }).catch(() => {});
      check(`${width}: the view asks for the Codex session's conversation`, asked > 0, `${asked} requests`);
      const shown = await log.evaluate((el) => ({
        text: el.innerText,
        says: el.querySelector(".log-says strong")?.textContent || "",
        you: el.querySelector(".log-instruction .log-text")?.textContent || "",
        calls: el.querySelectorAll("details[data-call]").length,
      }));
      check(`${width}: it shows the user's message`, shown.you === LOG.parts[0].text, JSON.stringify(shown));
      check(`${width}: it shows the command among the calls`, shown.calls > 0, JSON.stringify(shown));
      check(`${width}: it shows Codex's words as Markdown`, shown.says === "fails", JSON.stringify(shown));
      check(`${width}: no note says the conversation does not show`, !shown.text.includes("does not show here"), shown.text);
      check(`${width}: the first line says Codex keeps the earlier entries`, shown.text.includes("Codex keeps the earlier ones"), shown.text);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
      check(`${width}: no sideways scroll`, overflow <= 0, `${overflow} px`);
      await page.screenshot({ path: path.join(work, `codex-log-${width}.png`) });

      // Two commands in the same second, both without a title: opening one keeps the other closed across the next
      // poll's redraw, as each is kept open by its own id.
      await log.locator("details.log-calls > summary").first().click();
      await log.locator("details.log-call > summary").first().click();
      const before = asked;
      const end = Date.now() + 30000;
      while (asked === before && Date.now() < end) await new Promise((resolve) => setTimeout(resolve, 200));
      await new Promise((resolve) => setTimeout(resolve, 500));
      const open = await log.locator("details.log-call").evaluateAll((all) => all.map((el) => el.open));
      check(`${width}: after a redraw only the opened command is open`, asked > before && JSON.stringify(open) === "[true,false]",
        `${asked - before} polls, open ${JSON.stringify(open)}`);
      check(`${width}: no page errors`, errors.length === 0, errors.join(" | "));
      await context.close();
    }
    console.error(`  screenshots in ${work}`);
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
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error("serve_preview.py did not come up within 60 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
