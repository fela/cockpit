// The hub's part of the Usage page (SPEC UI-85): under the page's head a status line, then the Accounts section with
// one aligned row per account with its state, limits, requests and controls (the key its state calls for, the rest in its ⋯
// menu), Add account beside Usage, and near the page's end the closed "Hub service" fold with the hub's service, the restarts and
// the requests the hub answered with an error when there are any. The address /hub opens the Usage page. A change that
// cannot be taken back asks first in the page's own dialog. An open menu or dialog stays through a poll. The page shows
// nothing of a failed request's headers or body, a control changes the hub and the page draws the hub as read after
// it, and nothing is wider than the window on a desktop or a phone. While the hub does not answer, the last read's
// rows show without their controls; on a machine without a hub none of these parts show and the machine's own weekly
// limits take their place.
//
// Starts serve_hub.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) with a
// stand-in hub on a local port, and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
// The page with its Hub service fold open is saved as hub-1280.png, hub-1280-dark.png and hub-390.png in the work
// folder, an open ⋯ menu as hub-menu.png and hub-390-menu.png, the question dialog as hub-confirm.png and
// hub-confirm-dark.png, and the page on a machine without a hub as no-hub.png.
//
// Usage, from the checkout: node tests/browser/hub_page.cjs

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
const WORKING = "sam@example.test";
const WITH_MODEL = "alex@example.test";
const RESTING = "kim@example.test";
const OFF = "lee@example.test";
// What the stand-in hub's log of a failed request holds beside the request's line: none of it may reach the page.
const CALLER_KEY = "caller-key-in-the-log";
const CALLER_BODY = "what the caller asked, which stays in the log";

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// The requests the hub sent through an account in its last ten-minute spans, as the hub names them: by their clock
// times in UTC.
function spans(now, counts) {
  const clock = (ms) => new Date(ms).toISOString().slice(11, 16);
  const start = Math.floor(now / 600e3) * 600e3 - (counts.length - 1) * 600e3;
  return counts.map(([success, failed], index) => (
    { time: `${clock(start + index * 600e3)}-${clock(start + (index + 1) * 600e3)}`, success, failed }));
}

// One request the hub answered with an error, in the form of the hub's own log file of it (tests/fake_hub.py).
function errorLog(method, url, status) {
  return "=== REQUEST INFO ===\nVersion: 8.0.53\n"
    + `URL: ${url}\nMethod: ${method}\nDownstream Transport: http\nTimestamp: 2026-11-09T11:58:00Z\n\n`
    + `=== HEADERS ===\nAuthorization: Bearer ${CALLER_KEY}\nUser-Agent: a-client\n\n`
    + `=== REQUEST BODY ===\n${CALLER_BODY}\n\n`
    + `=== RESPONSE ===\nStatus: ${status}\nContent-Type: text/plain\n\nthe hub's answer\n`;
}

// What the stand-in hub holds (serve_slow.py, hub.json): a Codex account that works, a Claude account with a weekly
// limit for Fable alone, a Claude account the hub rests, and a Codex account turned off that has carried no request.
// Cockpit kept a Fable limit of the resting account from a request three hours before its last.
function standIn(now) {
  const seconds = Math.round(now / 1000);
  const said = new Date(now - 4 * 60e3).toISOString();
  const week = String(seconds + 3.5 * 86400);
  // `fiveHour` null: the provider names no 5-hour limit for the account, as for some accounts.
  const claude = (email, index, fiveHour, sevenDay, more) => ({
    id: `claude-${email}.json`, auth_index: index, provider: "claude", email,
    quota: { observed_at: said, signals: {
      ...(fiveHour === null ? {} : {
        "Anthropic-Ratelimit-Unified-5h-Utilization": fiveHour, "Anthropic-Ratelimit-Unified-5h-Reset": String(seconds + 2 * 3600),
      }),
      "Anthropic-Ratelimit-Unified-7d-Utilization": sevenDay, "Anthropic-Ratelimit-Unified-7d-Reset": week, ...more,
    } },
  });
  const busy = [[4, 0], [9, 0], [14, 1], [6, 0], [0, 0], [3, 0], [18, 0], [22, 2], [11, 0], [7, 0], [5, 0], [12, 0]];
  return {
    routing: "fill-first",
    kept_limits: {
      [`claude/claude-${RESTING}.json`]: {
        windows: {}, plan: "",
        models: { Fable: { used_percentage: 57, resets_at: seconds + 3.5 * 86400, captured_at: new Date(now - 184 * 60e3).toISOString() } },
      },
    },
    credentials: [
      {
        id: `codex-${WORKING}-plus.json`, auth_index: 1, provider: "codex", email: WORKING, success: 1840, failed: 12,
        recent_requests: spans(now, busy), access_token_expires_at: new Date(now + 6 * 86400e3).toISOString(),
        quota: { observed_at: said, signals: {
          "X-Codex-Plan-Type": "plus",
          "X-Codex-Primary-Used-Percent": "31", "X-Codex-Primary-Window-Minutes": "300", "X-Codex-Primary-Reset-At": String(seconds + 3 * 3600),
          "X-Codex-Secondary-Used-Percent": "48", "X-Codex-Secondary-Window-Minutes": "10080", "X-Codex-Secondary-Reset-At": week,
        } },
      },
      {
        ...claude(WITH_MODEL, 2, null, "0.64", { "Anthropic-Ratelimit-Unified-7d_oi-Utilization": "0.22", "Anthropic-Ratelimit-Unified-7d_oi-Reset": week }),
        success: 920, failed: 3, recent_requests: spans(now, busy.map(([success]) => [Math.round(success / 2), 0])),
        access_token_expires_at: new Date(now + 5 * 3600e3).toISOString(),
        // The provider rate-limited Fable alone: the hub holds that model on the account for ten minutes.
        quota_blocked: true, cooldowns: [{ scope: "model", model_key: "claude-fable-5-2", reason: "transient_rate_limit",
          http_status: 429, retry_at: new Date(now + 10 * 60e3).toISOString() }],
      },
      {
        ...claude(RESTING, 3, "1", "0.91", {}), success: 2210, failed: 96,
        // Eight hours of spans with hundreds failed, as long as the hub's window gets: the chart narrows beside the count.
        recent_requests: spans(now, [...busy, ...busy, ...busy, ...busy].map(([success], index) => (index > 8 ? [0, success * 3] : [success, 0]))),
        unavailable: true, quota_blocked: true, next_retry_after: new Date(now + 110 * 60e3).toISOString(),
        status_message: "rate limited: 429 from the provider",
      },
      {
        id: `codex-${OFF}-pro.json`, auth_index: 4, provider: "codex", email: OFF, disabled: true, id_token: { plan_type: "promax" },
        recent_requests: spans(now, busy.map(() => [0, 0])),
      },
    ],
    error_logs: {
      "error-v1-messages-1.log": [seconds - 90, errorLog("POST", "/v1/messages?beta=true", 429)],
      "error-v1-responses-2.log": [seconds - 400, errorLog("POST", "/v1/responses", 502)],
      "error-api-hello-3.log": [seconds - 20, errorLog("GET", "/api/hello", 404)],
      "error-api-hello-4.log": [seconds - 200, errorLog("GET", "/api/hello", 404)],
      "error-api-hello-5.log": [seconds - 600, errorLog("GET", "/api/hello", 404)],
    },
  };
}

// Account rows retain their data and controls when their layout changes. Read limits by their shared column key,
// and read working state and request counts from their accessible names and hover text.
function accountsNow() {
  const tip = (el) => el?.getAttribute("data-tip") || el?.getAttribute("title") || "";
  return [...document.querySelectorAll("#hub-accounts .hub-account")].map((el) => ({
    email: el.querySelector(".hub-account-who").dataset.tip,
    name: el.querySelector(".hub-account-who").textContent,
    kind: el.querySelector(".hub-account-kind").textContent,
    planTip: tip(el.querySelector(".hub-account-kind")),
    state: ["working", "cooling", "disabled", "signed_out"].find((state) => el.classList.contains(state)),
    words: el.querySelector(".hub-state-words")?.textContent || el.querySelector(".hub-lamp")?.getAttribute("aria-label") || tip(el.querySelector(".hub-lamp")),
    visibleState: el.querySelector(".hub-account-state")?.innerText || "",
    said: el.querySelector(".hub-said")?.textContent || "",
    stateTip: tip(el.querySelector(".hub-account-state")),
    limits: [...el.querySelectorAll(".hub-limit")].filter((row) => row.querySelector(".hub-limit-used")).map((row) => ({
      key: row.dataset.limit,
      name: row.querySelector(".hub-limit-name")?.textContent || "",
      used: row.querySelector(".hub-limit-used").textContent,
      line: row.querySelector(".hub-limit-line")?.textContent || "",
      detail: tip(row) + " " + tip(row.querySelector(".hub-limit-line")),
      stale: row.classList.contains("stale"),
    })),
    limitsAt: el.querySelector(".hub-limits-at")?.textContent || "",
    spans: el.querySelectorAll(".hub-span").length,
    traffic: el.querySelector(".hub-spans")?.getAttribute("aria-label") || el.querySelector(".hub-traffic")?.getAttribute("aria-label") || tip(el.querySelector(".hub-traffic")),
    trafficTip: tip(el.querySelector(".hub-spans")) || tip(el.querySelector(".hub-traffic")),
    trafficText: el.querySelector(".hub-traffic")?.innerText || "",
    keys: [...el.querySelectorAll(".hub-keys > button")].map((button) => button.textContent),
    more: el.querySelector(".hub-more > summary")?.getAttribute("aria-label") || "",
    entries: [...el.querySelectorAll(".hub-more .menu-list button")].map((button) => button.textContent.trim()),
    text: el.innerText,
  }));
}

function statusNow() {
  const line = document.querySelector("#hub-health .hub-status");
  return line ? { look: line.className.replace("hub-status ", ""), text: line.textContent } : { look: "", text: document.getElementById("hub-health").innerText };
}

function dialogNow() {
  const dialog = document.getElementById("hub-confirm");
  return {
    open: dialog.open,
    title: dialog.querySelector("h2")?.textContent || "",
    keys: [...dialog.querySelectorAll("button")].map((button) => button.textContent),
    focus: document.activeElement.textContent,
  };
}

const overflow = () => document.documentElement.scrollWidth - window.innerWidth;
const cardOf = (email) => `.hub-account:has(.hub-account-who[data-tip="${email}"])`;

async function main() {
  const work = workFolder("cockpit-hub-page-");
  fs.writeFileSync(path.join(work, "hub.json"), JSON.stringify(standIn(Date.now())));
  const server = spawn(PYTHON, [path.join(__dirname, "serve_hub.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const pageErrors = [];
    const open = async (width, scheme, address) => {
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme, isMobile: width < 600 });
      // In the browser's own cookies, so the cookie also goes with the request a redirect makes (/hub to /usage).
      await context.addCookies([{ name: info.name, value: info.value, domain: new URL(info.origin).hostname, path: "/", secure: true, httpOnly: true, sameSite: "Lax" }]);
      const page = await context.newPage();
      page.on("pageerror", (error) => pageErrors.push(error.message));
      await page.goto(`${info.origin}${address}`);
      // The hub's first read finishes a moment after the page's first state, and the hub's own state (its version, the
      // sharing, the failed requests) arrives with a later read than the accounts.
      try {
        await page.waitForSelector("#hub-accounts .hub-account", { timeout: 30e3 });
        await page.waitForFunction(() => document.getElementById("hub-failed").childElementCount > 0, null, { timeout: 30e3 });
      } catch (error) {
        const seen = await page.evaluate(() => document.body.innerText.slice(0, 600)).catch(() => "");
        throw new Error(`${address} at ${width} px did not draw the hub: ${error.message}\nat ${page.url()}\npage errors: ${JSON.stringify(pageErrors)}\npage: ${seen}`);
      }
      await page.evaluate(() => document.fonts.ready);
      return { context, page };
    };
    // Opens the Hub service fold, which the page starts with closed.
    const openFold = async (page) => {
      await page.click(".hub-fold > summary");
      await page.waitForFunction(() => document.querySelector(".hub-fold").open, null, { timeout: 5e3 });
    };
    // Opens an account's ⋯ menu and presses one of its entries.
    const fromMenu = async (page, email, entry) => {
      await page.click(`${cardOf(email)} .hub-more > summary`);
      await page.click(`${cardOf(email)} .hub-more .menu-list button:has-text("${entry}")`);
    };

    // The page as it loads, at each width.
    for (const [width, scheme, shot] of [[1280, "light", "hub-1280.png"], [1280, "dark", "hub-1280-dark.png"], [390, "light", "hub-390.png"]]) {
      const { context, page } = await open(width, scheme, "/usage");
      if (width === 1280 && scheme === "light") {
        const order = await page.evaluate(() => {
          const kids = [...document.querySelector("main").children];
          const at = (selector) => kids.indexOf(document.querySelector(selector));
          const fold = document.querySelector(".hub-fold");
          return { head: at(".spend-head"), top: at(".hub-top"), signIns: at("#hub-sign-ins"), left: at("#hub-left"), accounts: at("#hub-accounts"), daily: at("#spend-daily"),
            tables: at("#spend-more"), fold: at(".hub-fold"), open: fold.open, summary: fold.querySelector("summary").textContent };
        });
        check("the hub's status line, allowance graph and Accounts follow the page's head, and Hub service is a closed fold after the usage's tables",
          order.top === order.head + 1 && order.signIns === order.top + 1 && order.left === order.signIns + 1 && order.accounts === order.left + 1 && order.daily > order.accounts && order.fold > order.tables
            && !order.open && order.summary === "Hub service", JSON.stringify(order));
      }
      // The primary action keeps its colours when its open menu takes the pointer.
      await page.mouse.move(0, 0);
      const addColours = () => page.locator(".hub-add > summary").evaluate((el) => {
        const style = getComputedStyle(el);
        return [style.backgroundColor, style.color];
      });
      const closedColours = await addColours();
      await page.click(".hub-add > summary");
      await page.hover(".hub-add .menu-list button:first-child");
      const openColours = await addColours();
      check(`${width} px ${scheme}: Add account keeps its primary colours while its menu is open`,
        String(closedColours) === String(openColours) && openColours[0] !== openColours[1], JSON.stringify({ closedColours, openColours }));
      await page.screenshot({ path: path.join(work, `add-account-${width}-${scheme}.png`) });
      await page.click("main h1");
      await openFold(page);
      await page.waitForSelector(".hub-service-row", { timeout: 30e3 });
      const over = await page.evaluate(overflow);
      const text = await page.evaluate(() => document.body.innerText);
      await page.screenshot({ path: path.join(work, shot), fullPage: true });
      check(`${width} px ${scheme}: nothing wider than the window`, over <= 0, `${over} px over`);
      check(`${width} px ${scheme}: every part of the page is drawn`,
        ["Accounts", "Add account", "Failed requests", "Hub service", "Restart the hub"].every((title) => text.includes(title)), text);
      const rows = await page.evaluate(() => [...document.querySelectorAll("#hub-accounts .hub-account")].map((el) => {
        const style = getComputedStyle(el);
        return { sides: [style.borderRightWidth, style.borderLeftWidth].map(parseFloat),
          top: parseFloat(style.borderTopWidth), bottom: parseFloat(style.borderBottomWidth), shadow: style.boxShadow, radius: style.borderRadius };
      }));
      check(`${width} px ${scheme}: account rows have no enclosing border, rounded corners or shadow`,
        rows.every((row) => row.sides.every((value) => value === 0) && row.top + row.bottom <= 1 && row.shadow === "none" && parseFloat(row.radius) === 0), JSON.stringify(rows));
      const longTips = await page.evaluate(() => [...document.querySelectorAll("#hub-accounts [title], #hub-accounts [data-tip]")]
        .map((el) => el.getAttribute("data-tip") || el.getAttribute("title"))
        .filter((tip) => tip.split("\n").length > 2 || tip.split("\n").some((line) => line.length > 80)));
      const charts = await page.evaluate(() => [...document.querySelectorAll("#hub-accounts .hub-traffic")].map((el) => {
        const count = el.querySelector(".hub-traffic-failed").getBoundingClientRect();
        const right = Math.max(...[...el.querySelectorAll(".hub-span")].map((span) => span.getBoundingClientRect().right));
        return count.width ? Math.round(count.left - right) : null;
      }).filter((space) => space !== null));
      check(`${width} px ${scheme}: a long request chart ends before its failed count`, charts.length >= 2 && charts.every((space) => space >= 6), JSON.stringify(charts));
      check(`${width} px ${scheme}: account hover text fits two lines of 80 characters`, longTips.length === 0, JSON.stringify(longTips));
      if (width === 1280) {
        const columns = await page.evaluate(() => {
          const heads = [...document.querySelectorAll(".hub-account-columns [data-limit]")];
          return heads.map((head) => {
            const cells = [...document.querySelectorAll(`.hub-account .hub-limit[data-limit="${head.dataset.limit}"]`)];
            const expected = head.getBoundingClientRect();
            return { key: head.dataset.limit, left: expected.left, width: expected.width,
              cells: cells.map((cell) => {
                const box = cell.getBoundingClientRect();
                return { left: box.left, width: box.width, named: cell.querySelector(".hub-limit-name")?.checkVisibility() || false };
              }) };
          });
        });
        check(`${width} px ${scheme}: shared limit headers and cells align across accounts`,
          String(columns.map((column) => column.key)) === "five_hour,week,fable,opus"
            && columns.every((column) => column.cells.length > 0 && column.cells.every((cell) => Math.abs(cell.left - column.left) <= 1 && Math.abs(cell.width - column.width) <= 1 && !cell.named)), JSON.stringify(columns));
        const unknown = await page.locator(`${cardOf(OFF)} .hub-limits-at`).boundingBox();
        check(`${width} px ${scheme}: an unknown account's limit message spans the shared limit columns`,
          unknown && columns.length > 0 && Math.abs(unknown.x - columns[0].left) <= 1
            && Math.abs(unknown.x + unknown.width - columns.at(-1).left - columns.at(-1).width) <= 1, JSON.stringify({ unknown, columns }));
      }
      if (scheme === "dark") {
        await fromMenu(page, OFF, "Sign out");
        await page.waitForSelector("#hub-confirm[open]", { timeout: 5e3 });
        await page.screenshot({ path: path.join(work, "hub-confirm-dark.png") });
        await page.keyboard.press("Escape");
      }
      if (width === 390) {
        // A menu opens under its key's right edge and stays inside the window.
        const inWindow = async (selector) => {
          await page.click(`${selector} > summary`);
          const box = await page.evaluate((found) => {
            const list = document.querySelector(`${found} > .menu-list`).getBoundingClientRect();
            return { left: list.left, right: list.right, width: window.innerWidth, over: document.documentElement.scrollWidth - window.innerWidth };
          }, selector);
          return box;
        };
        const add = await inWindow(".hub-add");
        await page.click("main h1");
        const more = await inWindow(`${cardOf(WORKING)} .hub-more`);
        await page.screenshot({ path: path.join(work, "hub-390-menu.png") });
        await page.click("main h1");
        check("390 px: Add account's and a card's ⋯ menus open inside the window",
          [add, more].every((box) => box.left >= 0 && box.right <= box.width && box.over <= 0), JSON.stringify([add, more]));
      }
      await context.close();
    }

    const { context, page } = await open(1280, "light", "/hub");
    check("the address /hub opens the Usage page", new URL(page.url()).pathname === "/usage", page.url());
    await openFold(page);
    // At 1280 px the header is the rail at the left (page-rail.css), with the page beside it.
    const held = await page.evaluate(() => ({ header: document.querySelector("header.top").getBoundingClientRect().width,
      main: document.querySelector("main").getBoundingClientRect().x, pages: document.querySelector('.brand-menu a[href="/pages"]').checkVisibility() }));
    check("1280 px: the header is a 280 px rail listing the pages, the page beside it", held.header === 280 && held.main >= 280 && held.pages, JSON.stringify(held));
    const menuPages = await page.evaluate(() => [...document.querySelectorAll(".brand-menu a")].map((link) => `${link.getAttribute("href")}${link.getAttribute("aria-current") ? "*" : ""}`));
    check("the menu has no Hub entry, and marks Usage as the page shown",
      menuPages.includes("/usage*") && !menuPages.some((href) => href.startsWith("/hub")), String(menuPages));

    const status = await page.evaluate(statusNow);
    check("the status line says the hub is up and how the accounts stand",
      status.look === "ok" && /^Up, 2 of 4 accounts working, 1 resting until .+, 1 turned off$/.test(status.text), JSON.stringify(status));

    const cards = await page.evaluate(accountsNow);
    const card = (email) => cards.find((each) => each.email === email);
    check("one row per account, Claude's before Codex's, each named by its address's first part with the address on hover",
      String(cards.map((each) => each.email)) === String([WITH_MODEL, RESTING, OFF, WORKING])
        && String(cards.map((each) => each.name)) === "alex,kim,lee,sam", String(cards.map((each) => each.name)));
    const limit = (account, key) => account?.limits.find((row) => row.key === key);
    const working = card(WORKING);
    check("a working account's lamp names its state, with its plan, its week and five hours, and its requests on hover",
      working && working.state === "working" && working.words === "Working" && working.visibleState === "" && working.said === "" && working.kind === "Codex Plus"
        && working.limits.length === 2 && limit(working, "week")?.used === "48 %" && limit(working, "five_hour")?.used === "31 %"
        && working.spans === 12 && /114 requests since \d\d:\d\d, 3 failed/.test(working.traffic)
        && /114 requests since \d\d:\d\d, 3 failed/.test(working.trafficTip)
        && working.trafficText.trim() === "3 failed", JSON.stringify(working));
    check("limits below 80 percent show no countdown, and a token that still runs says nothing",
      working && working.limitsAt === "" && working.limits.every((row) => !row.stale && row.line === "") && !working.text.includes("token"), JSON.stringify(working));
    check("a working account has no key of its own, and its ⋯ menu turns it off or signs it out",
      working && working.keys.length === 0 && working.more === "More for sam" && String(working.entries) === "Turn off,Sign out", JSON.stringify(working));
    const model = card(WITH_MODEL);
    const opusHelp = await page.locator('.hub-account-columns [data-limit="opus"]').evaluate((el) => el.getAttribute("data-tip") || el.getAttribute("title") || "");
    check("a Claude account shows its week, Fable's week and the week after Fable, with a countdown for a limit at 80 percent or more",
      model && model.limits.length === 3 && limit(model, "fable")?.used === "22 %"
        && limit(model, "opus")?.used === "100 %" && /^3 d \d+ h$/.test(limit(model, "opus")?.line)
        && limit(model, "week")?.used === "64 %" && limit(model, "week")?.line === ""
        && opusHelp.includes("Fable") && !await page.locator(".hub-opus-note").count(), JSON.stringify(model) + " | " + opusHelp);
    check("an account whose provider rate-limited one model works, and names that model's hold with its reason on hover",
      model && model.state === "working" && /^Fable 5\.2 held until \d\d:\d\d$/.test(model.visibleState)
        && /rate-limited/.test(model.stateTip) && model.keys.length === 0, JSON.stringify(model));
    check("a limit of the last request does not say when it was said", model && model.limits.every((row) => !row.stale && !/said /.test(row.detail + row.line)), JSON.stringify(model));
    const resting = card(RESTING);
    check("limits kept from an earlier request are dimmed and identify when they were said on hover",
      resting && resting.limits.length === 4 && String(resting.limits.filter((row) => row.stale).map((row) => row.key)) === "fable,opus"
        && limit(resting, "fable")?.used === "57 %" && /said |as of /i.test(limit(resting, "fable")?.detail)
        && /said |as of /i.test(limit(resting, "opus")?.detail)
        && limit(resting, "week")?.used === "91 %" && limit(resting, "five_hour")?.used === "100 %", JSON.stringify(resting));
    check("a resting account's limits say their countdown at 80 percent or more, as a working account's do",
      resting && String(resting.limits.map((row) => [row.key, /\d/.test(row.line)])) === String(resting.limits.map((row) => [row.key, parseFloat(row.used) >= 80])),
      JSON.stringify(resting));
    check("a resting account says until when, with the hub's reason on hover, and its key ends the rest",
      resting && resting.state === "cooling" && /^Resting until /.test(resting.words) && resting.said === ""
        && /^Until .+\nrate limited: 429 from the provider$/.test(resting.stateTip)
        && String(resting.keys) === "End the rest" && String(resting.entries) === "Turn off,Sign out", JSON.stringify(resting));
    const off = card(OFF);
    check("the $500 account names its price and puts Ultrafast access in its plan's hover text",
      off.kind === "Codex Pro $500" && off.planTip.includes("Ultrafast") && !off.text.includes("Ultrafast")
        && !await page.locator(".hub-plan-500, .hub-plan-note").count(), JSON.stringify(off));
    check("an account turned off that carried no request says both, without limit rows, and its key turns it on",
      off && off.state === "disabled" && off.words === "Turned off" && off.limitsAt === "Its limits show after its next request through the hub"
        && off.limits.length === 0 && String(off.keys) === "Turn on" && String(off.entries) === "Sign out", JSON.stringify(off));
    check("spans without a request draw no chart, and keep their count on hover",
      off && off.spans === 0 && /0 requests since \d\d:\d\d/.test(off.traffic) && /0 requests since \d\d:\d\d/.test(off.trafficTip)
        && off.trafficText.trim() === "", JSON.stringify(off));
    const ends = await page.evaluate(() => [...document.querySelectorAll(".hub-account")].map((el) => ({
      controls: Boolean(el.querySelector(".hub-account-end .hub-more")) && (!el.querySelector(".hub-keys") || Boolean(el.querySelector(".hub-account-end .hub-keys"))),
      chart: el.querySelector(".hub-spans")?.getBoundingClientRect().height,
      lines: el.querySelectorAll(".hub-traffic-line").length,
      countdowns: [...el.querySelectorAll(".hub-limit-line")].map((line) => [line.textContent, line.getBoundingClientRect().height]),
      height: el.getBoundingClientRect().height,
      words: [".hub-account-who", ".hub-limit-used", ".hub-spans", ".hub-account-end"].map((part) => el.querySelector(part)).filter(Boolean)
        .map((part) => { const box = part.getBoundingClientRect(); return Math.round(box.top + box.height / 2 - el.getBoundingClientRect().top); }),
      state: el.querySelector(".hub-account-state")?.textContent || "",
    })));
    check("state keys and menus share the row's end, request charts are 24 pixels tall, and only a countdown with words takes a line",
      ends.every((end) => end.controls && (end.chart === undefined || end.chart === 24) && end.lines === 0
        && end.countdowns.every(([words, height]) => (words ? height === 16 : height === 0))), JSON.stringify(ends));
    check("a row is as tall as its lines, 48 pixels with one line plus the rule above it, and its name, figures, chart and keys share a middle",
      ends.every((end, at) => new Set(end.words).size === 1
        && (end.state || end.countdowns.some(([words]) => words) || end.height === (at ? 49 : 48))), JSON.stringify(ends));

    const quiet = await page.evaluate(() => ({
      hints: [...document.querySelectorAll(".hub-part .section-head .hint")].map((el) => `${el.closest("section").id}: ${el.textContent}`),
      add: document.querySelector(".hub-add > summary")?.textContent || "",
      addInHead: Boolean(document.querySelector(".spend-head .hub-add")),
      addCount: document.querySelectorAll(".hub-add").length,
      addEntries: [...document.querySelectorAll(".hub-add .menu-list button")].map((button) => `${button.textContent.trim()}${button.disabled ? " (off)" : ""}`),
      service: [...document.querySelectorAll(".hub-service-row")].map((row) => `${row.querySelector("dt").textContent}: ${row.querySelector("dd").innerText.replace(/\s+/g, " ").trim()}`),
      version: document.querySelector(".hub-service-row:nth-child(2) dd").firstChild.textContent,
      newer: document.querySelector(".hub-version-new")?.textContent || "",
    }));
    check("of the hub's parts, only the failed requests' head says more than its title", String(quiet.hints) === "hub-failed: 5 in the hub's log", String(quiet.hints));
    check("Add account sits once beside Usage and signs in a Claude or a Codex account",
      quiet.add === "Add account" && quiet.addInHead && quiet.addCount === 1 && String(quiet.addEntries) === "Claude account,Codex account", JSON.stringify(quiet));
    check("the hub service says how requests are shared, the version with the newer release, the client keys, and has the restarts",
      quiet.service.length === 4 && quiet.service[0].startsWith("Sharing: Round robin Weighted round robin Fill first")
        && quiet.version === "8.0.53" && quiet.newer === "8.0.63 is out" && quiet.service[2] === "Client keys: 2"
        && quiet.service[3] === "Restart: Restart the hub Restart the tunnel", JSON.stringify(quiet));

    const failed = await page.evaluate(() => [...document.querySelectorAll("table.hub-failed tbody tr")].map((row) => [...row.cells].slice(1).map((cell) => cell.textContent).join(" ")));
    check("the failed requests show how many times, the answer, the method and the path, the newest first and a repeated one once",
      String(failed) === String(["3 404 GET /api/hello", "1 429 POST /v1/messages", "1 502 POST /v1/responses"]), failed.join(" | "));
    const body = await page.evaluate(() => document.documentElement.outerHTML);
    const state = await page.evaluate(() => fetch("/api/hub").then((response) => response.text()));
    check("nothing of a failed request's headers or body reaches the page",
      ![body, state].some((text) => text.includes(CALLER_KEY) || text.includes(CALLER_BODY) || text.includes("stand-in-management-key")), "the page or its state holds the log's text or a key");

    // The controls: each changes the stand-in hub, and the page draws the hub as read after it.
    const routingNow = () => page.evaluate(() => [...document.querySelectorAll(".hub-routing button")].filter((button) => button.getAttribute("aria-pressed") === "true").map((button) => button.textContent));
    check("the way requests are shared is the pressed one", String(await routingNow()) === "Fill first", String(await routingNow()));
    await page.click(".hub-routing button:has-text('Round robin'):not(:has-text('Weighted'))");
    await page.waitForFunction(() => document.querySelector(".hub-routing button[aria-pressed='true']")?.textContent === "Round robin", null, { timeout: 15e3 }).catch(() => {});
    check("choosing another way changes the hub", String(await routingNow()) === "Round robin", String(await routingNow()));

    // Waits until the account is in `state`, or gone for null, with no change on its way.
    const settled = (email, want) => page.waitForFunction(([who, state]) => {
      const found = [...document.querySelectorAll(".hub-account")].find((el) => el.querySelector(".hub-account-who").dataset.tip === who);
      return !document.querySelector("#hub-read.busy") && (state === null ? !found : Boolean(found) && found.classList.contains(state));
    }, [email, want], { timeout: 15e3 }).catch(() => {});
    const press = async (email, key, state) => {
      await page.click(`${cardOf(email)} .hub-keys > button:text-is("${key}")`);
      await settled(email, state);
      return page.evaluate(accountsNow);
    };

    // A control's answer leaves the sign-ins out, so a sign-in that waits keeps its code on its card through one.
    const codeNow = () => page.evaluate(() => document.querySelector(".codex-code")?.value || "");
    await page.click(".hub-add > summary");
    await page.click(".hub-add .menu-list button:has-text('Codex account')");
    await page.waitForFunction(() => document.querySelector(".codex-code")?.value, null, { timeout: 15e3 }).catch(() => {});
    const codeShown = await codeNow();
    const adding = await page.evaluate(() => ({ open: document.querySelector(".hub-add").open,
      aboveGraph: document.querySelector("#hub-sign-ins").getBoundingClientRect().top < document.querySelector("#hub-left").getBoundingClientRect().top,
      heading: [document.querySelector(".hub-sign-in .hub-account-who")].map((el) => `${el.textContent}${el.scrollWidth > el.clientWidth ? " (cut)" : ""}`)[0],
      spills: [...document.querySelectorAll(".hub-sign-in > *")].some((el) => el.getBoundingClientRect().right > el.closest(".hub-sign-in").getBoundingClientRect().right),
      entries: [...document.querySelectorAll(".hub-add .menu-list button")].map((button) => `${button.textContent.trim()}${button.disabled ? " (off)" : ""}`) }));
    check("Add account, Codex account starts a sign-in with its code on a card under its heading, and the Codex entry waits for it",
      codeShown === "WXYZ-1234" && !adding.open && adding.aboveGraph && !adding.spills && adding.heading === "Signing in a Codex account" && String(adding.entries) === "Claude account,Codex account (off)", `${codeShown} ${JSON.stringify(adding)}`);
    for (const width of [1280, 390]) {
      await page.setViewportSize({ width, height: 900 });
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: path.join(work, `add-account-sign-in-${width}.png`) });
    }
    await page.setViewportSize({ width: 1280, height: 900 });

    await fromMenu(page, WORKING, "Turn off");
    await settled(WORKING, "disabled");
    let after = await page.evaluate(accountsNow);
    let now = after.find((each) => each.email === WORKING);
    check("Turn off in the ⋯ menu turns the account off, and its key then turns it on",
      now && now.state === "disabled" && String(now.keys) === "Turn on" && String(now.entries) === "Sign out", JSON.stringify(now));
    const troubled = await page.evaluate(statusNow);
    check("with no Codex account working the status line says so first, in red",
      troubled.look === "trouble" && /^No Codex account is working, 1 of 4 accounts working, .+, 2 turned off$/.test(troubled.text), JSON.stringify(troubled));
    check("a Codex sign-in's code stays on its card through a control", codeShown === "WXYZ-1234" && await codeNow() === codeShown, `${codeShown} | ${await codeNow()}`);
    // The stand-in login ends as a finished sign-in.
    fs.writeFileSync(path.join(work, "hub", "finish"), "");
    after = await press(WORKING, "Turn on", "working");
    now = after.find((each) => each.email === WORKING);
    check("Turn on turns it on again, and the status line is well again", now && now.state === "working" && (await page.evaluate(statusNow)).look === "ok", JSON.stringify(now));
    after = await press(RESTING, "End the rest", "working");
    now = after.find((each) => each.email === RESTING);
    check("End the rest has the account take requests again", now && now.state === "working" && now.words === "Working" && now.keys.length === 0, JSON.stringify(now));

    // Signing out asks first in the page's dialog: Cancel and Escape leave the account, its Sign out key signs it out.
    await fromMenu(page, OFF, "Sign out");
    await page.waitForSelector("#hub-confirm[open]", { timeout: 5e3 });
    const asked = await page.evaluate(dialogNow);
    await page.screenshot({ path: path.join(work, "hub-confirm.png") });
    check("Sign out asks in the page's dialog, with the focus on Cancel",
      asked.open && asked.title === "Sign lee out of the hub?" && String(asked.keys) === "Sign out,Cancel" && asked.focus === "Cancel", JSON.stringify(asked));
    await page.click("#hub-confirm button:text-is('Cancel')");
    await sleep(500);
    const cancelled = await page.evaluate(() => ({ open: document.getElementById("hub-confirm").open, focus: document.activeElement.getAttribute("aria-label") }));
    const kept = await page.evaluate(accountsNow);
    check("Cancel leaves the account and gives the focus back to its ⋯ key",
      !cancelled.open && cancelled.focus === "More for lee" && kept.some((each) => each.email === OFF), JSON.stringify(cancelled));
    await fromMenu(page, OFF, "Sign out");
    await page.waitForSelector("#hub-confirm[open]", { timeout: 5e3 });
    await page.keyboard.press("Escape");
    await sleep(500);
    const escaped = await page.evaluate(() => document.getElementById("hub-confirm").open);
    check("Escape closes the question and leaves the account", !escaped && (await page.evaluate(accountsNow)).some((each) => each.email === OFF), String(escaped));
    await fromMenu(page, OFF, "Sign out");
    await page.waitForSelector("#hub-confirm[open]", { timeout: 5e3 });
    await page.click("#hub-confirm button[type=submit]");
    await settled(OFF, null);
    after = await page.evaluate(accountsNow);
    check("the dialog's Sign out removes the account", after.length === 3 && !after.some((each) => each.email === OFF), String(after.map((each) => each.name)));

    await page.click("[data-restart='tunnel']");
    await page.waitForSelector("#hub-confirm[open]", { timeout: 5e3 });
    const restartAsked = await page.evaluate(dialogNow);
    await page.click("#hub-confirm button[type=submit]");
    // The restart's answer comes before the next part, so a poll it would redraw is not part of that part's check.
    await page.waitForFunction(() => document.getElementById("hub-read").textContent.startsWith("Restarting"), null, { timeout: 5e3 }).catch(() => {});
    await page.waitForFunction(() => !document.querySelector("#hub-read.busy"), null, { timeout: 60e3 }).catch(() => {});
    const banner = await page.evaluate(() => `${document.getElementById("banner").innerText}${document.getElementById("hub-banner").innerText}`);
    const read = await page.evaluate(() => document.getElementById("hub-read").innerText);
    check("a restart asks in the dialog and ends with the hub read again",
      restartAsked.title === "Restart this machine's tunnel to the hub?" && String(restartAsked.keys) === "Restart the tunnel,Cancel"
        && banner === "" && /^Read \d\d:\d\d/.test(read), `${JSON.stringify(restartAsked)} | ${banner} | ${read}`);

    // A poll that brings a change waits while a menu or the dialog is open, and the change shows once it closes. The
    // change is the working account's plan, in Cockpit's answer as the page gets it.
    let plan = "";
    let served = 0;
    const planned = async (route) => {
      const response = await route.fetch();
      const data = await response.json();
      for (const each of (data.hub && data.hub.accounts) || []) if (each.email === WORKING) Object.assign(each, { plan, plan_label: plan });
      served += 1;
      await route.fulfill({ response, json: data });
    };
    const nextPoll = async () => {
      const before = served;
      const end = Date.now() + 30e3;
      while (served === before && Date.now() < end) await sleep(200);
      await sleep(500);
      return served > before;
    };
    const kindNow = () => page.evaluate((selector) => document.querySelector(`${selector} .hub-account-kind`).textContent, cardOf(WORKING));
    await page.route("**/api/hub", planned);
    plan = "team";
    await page.click(`${cardOf(WORKING)} .hub-more > summary`);
    await page.evaluate((selector) => { document.querySelector(`${selector} .hub-more`).dataset.kept = "yes"; }, cardOf(WORKING));
    await page.screenshot({ path: path.join(work, "hub-menu.png") });
    const polled = await nextPoll();
    const menuHeld = await page.evaluate((selector) => {
      const menu = document.querySelector(`${selector} .hub-more`);
      return { open: menu.open, kept: menu.dataset.kept || "", kind: document.querySelector(`${selector} .hub-account-kind`).textContent };
    }, cardOf(WORKING));
    await page.click("main h1");
    await page.waitForFunction((selector) => document.querySelector(`${selector} .hub-account-kind`).textContent === "Codex Team", cardOf(WORKING), { timeout: 5e3 }).catch(() => {});
    const menuAfter = await kindNow();
    check("an open ⋯ menu stays open through a poll that brings a change, and the change shows once it closes",
      polled && menuHeld.open && menuHeld.kept === "yes" && menuHeld.kind === "Codex Plus" && menuAfter === "Codex Team", `${polled} ${JSON.stringify(menuHeld)} ${menuAfter}`);
    plan = "enterprise";
    await page.click("[data-restart='hub']");
    await page.waitForSelector("#hub-confirm[open]", { timeout: 5e3 });
    const dialogPolled = await nextPoll();
    const dialogHeld = await page.evaluate(dialogNow);
    const kindHeld = await kindNow();
    await page.click("#hub-confirm button:text-is('Cancel')");
    await page.waitForFunction((selector) => document.querySelector(`${selector} .hub-account-kind`).textContent === "Codex Enterprise", cardOf(WORKING), { timeout: 5e3 }).catch(() => {});
    const dialogAfter = await kindNow();
    const back = await page.evaluate(() => document.activeElement.dataset.restart || "");
    const restarting = await page.evaluate(() => document.getElementById("hub-read").textContent);
    check("an open dialog stays through a poll that brings a change; Cancel draws the change, restarts nothing and gives the focus back to its key",
      dialogPolled && dialogHeld.open && dialogHeld.title === "Restart the hub's service?" && kindHeld === "Codex Team" && dialogAfter === "Codex Enterprise"
        && back === "hub" && !restarting.includes("Restarting"), `${dialogPolled} ${JSON.stringify(dialogHeld)} ${kindHeld} ${dialogAfter} ${back} ${restarting}`);
    // A closed menu's key can keep keyboard focus while a changed account row is drawn by the next poll.
    plan = "team";
    await page.locator(`${cardOf(WORKING)} .hub-more > summary`).focus();
    const focusPolled = await nextPoll();
    await page.waitForFunction((selector) => document.querySelector(`${selector} .hub-account-kind`).textContent === "Codex Team", cardOf(WORKING), { timeout: 5e3 }).catch(() => {});
    const focusAfter = await page.evaluate(() => ({
      name: document.activeElement.getAttribute("aria-label"),
      account: document.activeElement.closest(".hub-account")?.querySelector(".hub-account-who").dataset.tip,
      open: document.activeElement.closest(".hub-more")?.open,
    }));
    check("a changed account row preserves keyboard focus on its closed menu key through a poll",
      focusPolled && await kindNow() === "Codex Team" && focusAfter.name === "More for sam" && focusAfter.account === WORKING && focusAfter.open === false,
      `${focusPolled} ${JSON.stringify(focusAfter)}`);
    await page.unrouteAll({ behavior: "wait" });
    await context.close();

    // Cockpit's answer as the page gets it, changed: the tunnel down with no failed request, then the hub not answering,
    // first with the accounts of Cockpit's last read and then with none.
    const troubledContext = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    await troubledContext.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    const sick = await troubledContext.newPage();
    sick.on("pageerror", (error) => pageErrors.push(error.message));
    let changeAnswer = null;
    await sick.route("**/api/hub", async (route) => {
      const response = await route.fetch();
      const data = await response.json();
      if (data.hub && data.hub.accounts) changeAnswer(data.hub);
      await route.fulfill({ response, json: data });
    });
    changeAnswer = (hub) => {
      hub.tunnel = "failed";
      if (hub.service) hub.service.failed_requests = { count: 0, shown: [] };
    };
    await sick.goto(`${info.origin}/usage`);
    await sick.waitForSelector("#hub-health .hub-status.trouble", { timeout: 30e3 });
    await openFold(sick);
    await sick.waitForSelector(".hub-service-row", { timeout: 30e3 });
    const tunnelDown = await sick.evaluate(() => ({ status: document.querySelector("#hub-health .hub-status").textContent,
      failed: document.getElementById("hub-failed").childElementCount, shown: document.getElementById("hub-failed").checkVisibility(),
      text: document.querySelector("main").innerText }));
    check("a tunnel that is down leads the status line, in red",
      tunnelDown.status.startsWith("This machine's tunnel to the hub is down: systemd calls it failed, 3 of 3 accounts working"), tunnelDown.status);
    check("without failed requests the page has no part for them",
      tunnelDown.failed === 0 && !tunnelDown.shown && !tunnelDown.text.includes("Failed requests"), JSON.stringify({ failed: tunnelDown.failed, shown: tunnelDown.shown }));
    changeAnswer = (hub) => {
      hub.error = "the test's failure";
    };
    await sick.goto(`${info.origin}/usage`);
    await sick.waitForSelector("#hub-account-list .hub-old", { timeout: 30e3 });
    const old = await sick.evaluate(() => ({ note: document.querySelector("#hub-account-list .hub-old").textContent,
      cards: document.querySelectorAll("#hub-accounts .hub-account").length,
      controls: document.querySelectorAll("#hub-accounts :is(.hub-add, .hub-more, .hub-keys, button)").length }));
    check("with the hub not answering, the cards of Cockpit's last read show without their controls, under a line saying from when",
      old.note.startsWith("The hub does not answer, so these are the figures from ") && old.cards === 3 && old.controls === 0, JSON.stringify(old));
    changeAnswer = (hub) => {
      hub.error = "the test's failure";
      hub.accounts = [];
    };
    await sick.goto(`${info.origin}/usage`);
    await sick.waitForSelector("#hub-health .hub-status.trouble", { timeout: 30e3 });
    const down = await sick.evaluate(() => ({ status: document.querySelector("#hub-health .hub-status").textContent,
      list: document.getElementById("hub-account-list").innerText, add: document.querySelectorAll(".hub-add").length,
      service: [...document.querySelectorAll(".hub-service dt")].map((dt) => dt.textContent) }));
    check("with the hub not answering the status line says why, the accounts are unknown, and only the restarts stay",
      down.status === "Cockpit could not read the hub: the test's failure" && down.list === "Unknown while the hub does not answer."
        && down.add === 0 && String(down.service) === "Restart", JSON.stringify(down));
    await sick.unrouteAll({ behavior: "wait" });
    await troubledContext.close();

    // A machine without a hub: Cockpit answers that it has none, and the machine's own weekly limits show instead.
    const bare = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    await bare.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    const none = await bare.newPage();
    none.on("pageerror", (error) => pageErrors.push(error.message));
    await none.route("**/api/hub", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ hub: null }) }));
    await none.route("**/api/hub/left", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ left: null }) }));
    await none.goto(`${info.origin}/usage`);
    await none.waitForSelector("#spend-limits #limits-title", { timeout: 60e3 });
    await none.screenshot({ path: path.join(work, "no-hub.png"), fullPage: true });
    const without = await none.evaluate(() => ({
      shown: [...document.querySelectorAll(".hub-part")].filter((el) => el.checkVisibility()).map((el) => el.id || el.className),
      limits: document.getElementById("spend-limits").checkVisibility(),
      title: document.getElementById("limits-title").textContent,
      banner: document.getElementById("hub-banner").innerText,
    }));
    check("on a machine without a hub no part of the hub shows, and the machine's own weekly limits do",
      without.shown.length === 0 && without.limits && without.title === "Weekly limits" && without.banner === "", JSON.stringify(without));
    await none.unrouteAll({ behavior: "wait" });
    await bare.close();
    check("no page errors", pageErrors.length === 0, pageErrors.join(" | "));
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(`${results.length - failedChecks.length}/${results.length} checks passed; work dir ${work}`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

// The harness's cookie file, once it is written and the server answers.
async function harnessUp(work, server) {
  const file = path.join(work, "cookie.json");
  const end = Date.now() + 180e3;
  while (Date.now() < end) {
    if (server.exitCode !== null) throw new Error(`serve_preview.py exited with ${server.exitCode}`);
    if (fs.existsSync(file)) {
      const info = JSON.parse(fs.readFileSync(file, "utf8"));
      const up = await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false);
      if (up) return info;
    }
    await sleep(200);
  }
  throw new Error("serve_preview.py did not come up within 180 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
