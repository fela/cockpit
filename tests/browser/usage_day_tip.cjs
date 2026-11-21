// Usage's daily figures work by hover, keyboard and touch, with the tooltip inside the viewport.
// Uses synthetic API responses and this checkout's static files; no server or account files.
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const STATIC = path.resolve(__dirname, "../../src/cockpit/static");
const now = new Date();
const stamp = now.toISOString();
const hour = (daysAgo) => new Date(now.getTime() - daysAgo * 86400e3).toISOString();
const use = {
  tokens: { fresh: 1000, cache_write: 2000, cache_read: 3000, output: 4000 },
  usd_by_kind: { fresh: 4, cache_write: 10, cache_read: 0.6, output: 80 },
  calls: 10, usd: 94.6, week_percent: 2.365, plan_usd: 1.1825,
  subagent_usd: 4, woken_usd: 2, misses: 1, miss_tokens: 2000, miss_usd: 1, paused_miss_usd: 1,
};
// How the day's steps closed.
let closed = ["done", "dismissed"];
function report(period) {
  return {
    period, ready_at: stamp, since: stamp, total: use,
    sessions: [{ session_id: "session-1", name: "Parser project", title: "", first_prompt: "Fix parser",
      cwd: "/example/parser", kind: "terminal", first: stamp, last: stamp, turns: 2, use }],
    skills: [], origins: [], session_kinds: [], models: [], top_turns: [], miss_turns: [], unpriced_models: [],
    other_models: { calls: 0, tokens: { fresh: 0, cached: 0, output: 0 }, sessions: [] },
    week_list_usd: 4000, plan_usd_per_percent: 0.5,
    limits: { accounts: [], error: null },
    codex: { calls: 0, sessions: [], accounts: [], total: { fresh: 0, cached: 0, output: 0, reasoning: 0 },
      week_percent: 0, weeks: 0 },
    daily: { since: new Date(now.getTime() - 30 * 86400e3).toISOString(),
      hours: [{ hour: hour(1), use }], codex_hours: [{ hour: hour(1), week_percent: 3.25 }],
      steps: closed.map((status) => ({ closed_at: hour(1), status })) },
  };
}
async function main() {
  const work = workFolder("cockpit-usage-day-tip-");
  const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE || undefined });
  let passed = 0;
  try {
    for (const width of [1280, 390]) for (const scheme of ["light", "dark"]) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme,
        isMobile: width === 390, hasTouch: width === 390, timezoneId: "UTC" });
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.route("http://localhost/**", async (route) => {
        const url = new URL(route.request().url());
        if (url.pathname === "/api/spend") return route.fulfill({ json: report(url.searchParams.get("period")) });
        if (url.pathname.startsWith("/api/")) return route.fulfill({ json: {} });
        const file = path.join(STATIC, url.pathname === "/usage" ? "usage.html" : url.pathname.replace(/^\/static\//, ""));
        if (!fs.existsSync(file)) return route.fulfill({ status: 404, body: "" });
        const type = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".woff2": "font/woff2" }[path.extname(file)];
        return route.fulfill({ contentType: type || "application/octet-stream", body: fs.readFileSync(file) });
      });
      await page.goto("http://localhost/usage");
      const days = page.locator(".daily-chart.use .daily-day");
      const tip = page.locator("#usage-day-tip");
      await days.last().waitFor();
      await page.evaluate(() => document.fonts.ready);
      assert.equal(await days.count(), 30, "the daily chart shows 30 days");
      assert.equal(await page.locator(".daily-y").count(), 0, "repeated vertical labels are removed");
      assert.equal(await page.locator(".daily-scale").count(), 3, "one scale maximum per chart");
      assert.equal(await page.locator(".daily-axis").count(), 1, "the charts share their time axis");
      assert.deepEqual(await page.locator(".daily-title").evaluateAll((titles) => titles.map((el) => [el.textContent, Boolean(el.title || el.dataset.tip)])),
        [["Claude", true], ["Codex", true], ["Steps done", false]], "a chart's name has a hover only where the hover says more");
      assert.deepEqual(await page.locator(".spend-use-head h2").allTextContents(), ["Use"]);
      assert.equal(await page.locator(".spend-use-head .spend-controls").count(), 1);
      assert.equal(await page.locator(".tile-line, .rank-meta, .kinds-figure").count(), 0, "details wait for hover or expansion");
      assert.equal(await page.locator("#spend-where .spend-card").count(), 0, "rank lists have their own layout");
      assert.equal(await page.locator("#spend-where .rank-head button").count(), 4);
      assert.deepEqual(await page.locator("#spend-where .rank-head button").allTextContents(), ["All", "All", "All", "All"]);
      const kindHovers = await page.evaluate(() => {
        const hover = (el) => el.title || el.dataset.tip || "";
        return { segments: [...document.querySelectorAll(".kinds > .kind-bar > span")].map(hover),
          legends: [...document.querySelectorAll(".kind-detail")].map(hover) };
      });
      assert.equal(kindHovers.legends.length, 4);
      assert.deepEqual(kindHovers.segments, kindHovers.legends, "token kinds give the same detail on segment and legend");
      for (const [at, value] of ["0.1 %", "0.25 %", "0.015 %", "2 %"].entries()) {
        assert.ok(kindHovers.legends[at].includes(value), `kind ${at} is missing its exact value`);
        assert.ok(kindHovers.legends[at].includes(`${((at + 1) * 1000).toLocaleString("en-US")} tokens`), `kind ${at} is missing its token count`);
      }
      const longHovers = await page.evaluate(() => [...document.querySelectorAll("[title], [data-tip]")]
        .map((el) => ({ text: el.title || el.dataset.tip || "", selector: `${el.tagName.toLowerCase()}.${[...el.classList].join(".")}` }))
        .filter(({ text }) => text.split("\n").length > 2 || text.split("\n").some((line) => line.length > 80)));
      assert.deepEqual(longHovers, [], "Usage hover text stays within two lines of 80 characters");
      if (width === 390) {
        const smallTargets = await page.locator(".spend-controls button, .kind-detail, .rank-head button, .rank-row, button.spend-readout").evaluateAll((elements) => elements
          .map((el) => ({ name: el.textContent.trim(), width: el.getBoundingClientRect().width, height: el.getBoundingClientRect().height }))
          .filter((box) => box.width < 44 || box.height < 44));
        assert.deepEqual(smallTargets, [], "phone controls have 44px hit targets");
      }
      const layout = await page.evaluate(() => {
        const rect = (selector) => document.querySelector(selector).getBoundingClientRect();
        const row = document.querySelector(".rank-row");
        const [name, bar, figure] = [".rank-name", ".rank-bar", ".rank-figure"].map((selector) => row.querySelector(selector).getBoundingClientRect());
        const whole = row.getBoundingClientRect();
        const readout = getComputedStyle(document.querySelector(".spend-readout"));
        return { ordered: rect("h1").bottom <= rect("#spend-limits").top
          && rect("#spend-limits").bottom <= rect(".spend-use-head").top
          && rect("#spend-totals").bottom <= rect("#spend-daily").top,
        rankRow: Math.max(name.top, figure.top) < Math.min(name.bottom, figure.bottom) && name.right <= figure.left
          && bar.top >= Math.max(name.bottom, figure.bottom) && Math.abs(bar.width - whole.width) < 1,
        overflow: document.documentElement.scrollWidth - window.innerWidth,
        rowTitle: row.title || row.dataset.tip || "",
        readoutBox: readout.boxShadow !== "none" || parseFloat(readout.borderTopWidth) > 0
          || !["transparent", "rgba(0, 0, 0, 0)"].includes(readout.backgroundColor),
        };
      });
      assert.equal(layout.ordered, true, "limits, use controls, totals and daily chart follow the page heading");
      assert.equal(layout.rankRow, true, "a rank's name and figure share a line, and its bar runs under them at the row's width");
      assert.equal(layout.readoutBox, false, "the totals are unboxed figures");
      assert.ok(layout.rowTitle.includes("turns"), "rank metadata is on hover");
      assert.ok(layout.overflow <= 0, `${layout.overflow}px horizontal overflow`);
      // Hover and focus give the same day in all three aligned charts. A scroll takes a hover's tooltip away, so the
      // page comes to rest on the chart before the pointer moves.
      await days.nth(28).scrollIntoViewIfNeeded();
      await page.waitForTimeout(150);
      await days.nth(28).hover();
      await tip.waitFor({ state: "visible" });
      assert.equal(await tip.locator(".tip-when").count(), 1);
      assert.equal(await tip.locator(".tip-row").count(), 7);
      const figures = await tip.locator(".tip-row").allTextContents();
      assert.deepEqual(figures, ["Claude2.365 % of a week", "Claude API price$94.6", "Codex3.25 % of its own week",
        "Steps done1", "Steps dismissed1", "Claude per step2.365 % of a week", "API price per step$94.6"]);
      assert.equal(await page.locator('.daily-day.lit[data-day="28"]').count(), 3);
      assert.equal(await tip.evaluate((el) => el.parentElement === document.body && getComputedStyle(el).position === "fixed"), true);
      const assertInside = async (height) => {
        const box = await tip.boundingBox();
        assert.ok(box && box.x >= 0 && box.y >= 0 && box.x + box.width <= width + 1 && box.y + box.height <= height + 1,
          `tooltip outside ${width}x${height}px viewport: ${JSON.stringify(box)}`);
      };
      const assertClearOf = async (column) => {
        const [box, anchor] = await Promise.all([tip.boundingBox(), column.boundingBox()]);
        assert.ok(box && anchor && (box.x + box.width <= anchor.x || box.x >= anchor.x + anchor.width
          || box.y + box.height <= anchor.y || box.y >= anchor.y + anchor.height),
        `tooltip overlaps selected column: ${JSON.stringify({ box, anchor })}`);
      };
      await assertInside(900);
      await assertClearOf(days.nth(28));
      await days.nth(28).focus();
      await page.keyboard.press("ArrowLeft");
      assert.equal(await page.locator(":focus").getAttribute("data-day"), "27");
      await page.keyboard.press("ArrowRight");
      assert.equal(await page.locator(":focus").getAttribute("data-day"), "28");
      await page.keyboard.press("Home");
      assert.equal(await page.locator(":focus").getAttribute("data-day"), "0");
      await assertInside(900);
      await assertClearOf(days.first());
      await page.keyboard.press("End");
      assert.equal(await page.locator(":focus").getAttribute("data-day"), "29");
      await assertInside(900);
      await assertClearOf(days.last());
      assert.equal(await page.locator(":focus").getAttribute("aria-describedby"), "usage-day-tip");
      await page.keyboard.press("Escape");
      assert.equal(await tip.isVisible(), false, "Escape dismisses the tooltip");
      await page.keyboard.press("ArrowLeft");
      await tip.waitFor({ state: "visible" });
      await page.keyboard.press("End");
      await page.evaluate(() => render());
      assert.equal(await page.locator(":focus").getAttribute("data-focus"), "use:29", "redraw restores the daily focus");
      assert.equal(await tip.isVisible(), true, "redraw restores the focused day tooltip");
      await page.keyboard.press("Enter");
      assert.equal(await page.locator("#spend-daily .breakdown").count(), 1, "keyboard pins the day's details");
      assert.equal(await page.locator('.daily-day[aria-pressed="true"]').count(), 3);
      await page.keyboard.press("Enter");
      assert.equal(await page.locator("#spend-daily .breakdown").count(), 0, "keyboard unpins the day");
      if (width === 390) await days.nth(28).tap();
      else await days.nth(28).click();
      await tip.waitFor({ state: "visible" });
      assert.equal(await page.locator("#spend-daily .breakdown").count(), 1, "tap or click pins details");
      assert.match(await page.locator("#spend-daily .breakdown").innerText(), /3.25 %/);
      await assertInside(900);
      await assertClearOf(days.nth(28));
      await page.screenshot({ path: path.join(work, `usage-${width}-${scheme}.png`), fullPage: true });
      await page.setViewportSize({ width, height: 300 });
      await page.mouse.move(1, 1);
      await days.nth(28).hover();
      await tip.waitFor({ state: "visible" });
      await assertInside(300);
      await page.setViewportSize({ width, height: 900 });
      await page.getByRole("button", { name: "24 hours", exact: true }).click();
      await page.waitForFunction(() => document.querySelector("#spend-lede").textContent.startsWith("The last 24 hours"));
      assert.equal(await days.count(), 30, "period controls leave the daily time range fixed");
      for (const [at, section] of ["spend-sessions", "spend-skills", "spend-origins", "spend-codex"].entries()) {
        await page.locator("#spend-where .rank-head button").nth(at).click();
        assert.equal(await page.locator("#spend-more").evaluate((el) => el.open), true, `All opens ${section}`);
        assert.equal(await page.locator(`#${section}`).isVisible(), true);
        await page.locator("#spend-more > summary").click();
      }
      // Days whose steps were all dismissed still have something to say, so their chart stays.
      closed = ["dismissed"];
      await page.reload();
      await days.last().waitFor();
      assert.equal(await page.locator(".daily-chart.steps .daily-day").count(), 30, "a chart with only dismissed steps keeps its days");
      closed = ["done", "dismissed"];
      assert.deepEqual(errors, []);
      console.log(`pass: ${width}px ${scheme}: layout, hover, keyboard, redraw, pinning${width === 390 ? ", touch" : ""} and tooltip bounds`);
      passed += 1;
      await context.close();
    }
    console.log(`${passed}/4 viewport/theme cases passed; screenshots in ${work}`);
  } finally { await browser.close(); }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
