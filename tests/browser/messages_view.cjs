// Your latest message, with Tasks items and messages created through the fixture API.
"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const DAY_MS = 24 * 3600e3;


const SESSION_A = "11111111-1111-4111-8111-111111111111";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "browser-check", "content-type": "application/json" };

async function call(info, headers, method, url, body) {
  const response = await fetch(`${info.origin}${url}`, { method, headers: { ...headers, connection: "close" },
    body: body === undefined ? undefined : JSON.stringify(body) });
  if (!response.ok) throw new Error(`${method} ${url}: HTTP ${response.status} ${await response.text()}`);
  return response.json();
}

async function main() {
  const work = workFolder("cockpit-messages-view-");
  const checkout = path.join(__dirname, "..", "..");
  // This check needs completed messages, so seed them through its own test server.
  const fixtureServer = path.join(work, "server.py");
  fs.writeFileSync(fixtureServer, `
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
sys.path.insert(0, ${JSON.stringify(__dirname)})
import serve_preview
harness = serve_preview.serve_slow
original_seed = harness.seed_board
def seed(path):
    original_seed(path)
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    with harness.db.connect(path) as conn:
        for key, title, words in (
            ("export", "Export a task list", ["Export as CSV", "Include completed tasks", "Keep reminder dates", "Include the list name", "The preview looks ready"]),
            ("sharing", "Share a task list", ["Let Ana edit the list"]),
        ):
            item = harness.store.upsert_item(conn, {"key": "note:messages-" + key, "title": title, "stage": "building", "sessions": [harness.SESSION_B]}, "agent:fixture")
            for offset, text in enumerate(words):
                stamp = (now - timedelta(minutes=20-offset)).strftime("%Y-%m-%dT%H:%M:%SZ")
                conn.execute("INSERT INTO messages (author, item_id, session_id, text, state, created_at, sent_at) VALUES ('user', ?, ?, ?, 'sent', ?, ?)", (item["id"], harness.SESSION_B, text, stamp, stamp))
harness.seed_board = seed
harness.main(Path(sys.argv[1]))
`);
  const server = spawn(path.join(checkout, ".venv", "bin", "python"), [fixtureServer, work],
    { stdio: ["ignore", "inherit", "inherit"], env: { ...process.env, PYTHONPATH: path.join(checkout, "src") } });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    const cookie = `${info.name}=${info.value}`;
    await call(info, AGENT, "PUT", "/api/items", {
      key: "note:messages-quiet", title: "Add recurring reminders", stage: "building", sessions: [SESSION_A],
    });
    const state = await call(info, { cookie }, "GET", "/api/state");
    const since = new Date(Date.now() - DAY_MS).toISOString().slice(0, 19);
    const mine = state.messages.filter((message) => message.author === "user" && message.created_at >= since).map((message) => message.id);
    if (mine.length < 5) throw new Error(`the fixture database has ${mine.length} messages of the user's from the last day; the check needs 5`);

    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const width of [1280, 390]) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: width > 400 ? "light" : "dark" });
      await context.setExtraHTTPHeaders({ cookie });
      await context.addInitScript(() => localStorage.setItem("cockpit:work-sort", JSON.stringify("messages")));
      const page = await context.newPage();
      page.setDefaultTimeout(45000);
      page.setDefaultNavigationTimeout(60000);
      const errors = [];
      page.on("pageerror", (error) => { errors.push(error.message); console.error(`page error: ${error.message}`); });
      await page.goto(`${info.origin}/classic`);
      await page.locator("#work .row-msgs").first().waitFor().catch(async (error) => {
        console.error(await page.evaluate(() => ({ url: location.href, text: document.body.innerText.slice(0, 2000),
          ready: typeof ui !== "undefined" && Boolean(ui.state), errors: document.querySelector("#error")?.textContent })));
        await page.screenshot({ path: path.join(work, "messages-failed.png") });
        throw error;
      });

      const seen = await page.evaluate(() => {
        const allRows = [...document.querySelectorAll("#work .row")];
        const hasMessages = allRows.map((row) => Boolean(row.querySelector(":scope > .row-msgs")));
        const rows = allRows.filter((row, index) => hasMessages[index]);
        const times = rows.map((row) => row.querySelector(".row-msgs .msg .row-age").dataset.time);
        const sort = document.querySelector('#work select[data-key="work:sort"]');
        const group = document.querySelector('#work select[data-key="work:group"]');
        return {
          panel: Boolean(document.getElementById("messages")),
          sort: sort.value,
          groups: [...group.options].map((option) => option.value),
          times,
          counts: rows.map((row) => row.querySelectorAll(":scope > .row-msgs .msg:not(.failed):not(.waiting)").length),
          hasMessages,
          more: rows.findIndex((row) => row.querySelector(":scope > .row-msgs > .link-button")),
        };
      });
      check(`${width}px: no Messages panel`, !seen.panel, "the #messages panel is on the page");
      check(`${width}px: Sort reads Your latest message`, seen.sort === "messages", seen.sort);
      check(`${width}px: no Who moves next grouping in this view`, !seen.groups.includes("next"), seen.groups.join(", "));
      check(`${width}px: rows by latest message, newest first`,
        seen.times.every((time, at) => at === 0 || seen.times[at - 1] >= time), seen.times.join(" "));
      check(`${width}px: at most three messages per row, besides failed or waiting ones`, seen.counts.every((count) => count <= 3), seen.counts.join(","));
      check(`${width}px: the open work without messages follows`,
        seen.hasMessages.includes(false) && seen.hasMessages.every((hasMessages, index) => hasMessages === (index < seen.times.length)),
        JSON.stringify(seen.hasMessages));
      check(`${width}px: a row offers the remaining messages`, seen.more >= 0, "no Show more button");
      if (seen.more >= 0) {
        const row = page.locator("#work .row").filter({ has: page.locator(":scope > .row-msgs > .link-button") }).first();
        const words = await row.locator(":scope > .row-msgs > .link-button").textContent();
        const before = await row.locator(":scope > .row-msgs .msg").count();
        await row.locator(":scope > .row-msgs > .link-button").click();
        const after = await row.locator(":scope > .row-msgs .msg").count();
        check(`${width}px: "${words}" shows the rest`, after === before + Number(words.split(" ")[0]), `${after} after ${words}`);
      }
      // Every message of the user's from the last day shows once.
      for (const button of await page.locator("#work .row-msgs > .link-button").all()) {
        if ((await button.textContent()).endsWith("more")) await button.click();
      }
      const shownIds = await page.evaluate(() => [...document.querySelectorAll("#work .msg[data-message]")].map((el) => Number(el.dataset.message)));
      const missing = mine.filter((id) => !shownIds.includes(id));
      const twice = shownIds.filter((id, at) => shownIds.indexOf(id) !== at);
      check(`${width}px: each of the user's ${mine.length} messages of the last day shows once`, !missing.length && !twice.length,
        `missing ${missing.join(",")}; twice ${twice.join(",")}`);
      await page.locator("#work").screenshot({ path: path.join(work, `messages-${width}.png`), animations: "disabled", timeout: 20000 });
      check(`${width}px: no page errors`, !errors.length, errors.join("; "));
      await context.close();
    }

    // The keyboard menu's Messages entry sorts the list.
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    await context.setExtraHTTPHeaders({ cookie });
    await context.addInitScript(() => { if (!sessionStorage.getItem("seeded")) { localStorage.setItem("cockpit:work-sort", JSON.stringify("recent")); sessionStorage.setItem("seeded", "1"); } });
    const page = await context.newPage();
    await page.goto(`${info.origin}/classic#board/messages`);
    await page.locator("#work .row-msgs").first().waitFor();
    check("#board/messages sorts the list by Your latest message",
      await page.locator('#work select[data-key="work:sort"]').inputValue() === "messages", "not sorted");
    await context.close();
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  console.error(`screenshots in ${work}`);
  if (results.some((result) => !result.ok)) process.exitCode = 1;
}

async function harnessUp(work, server) {
  const file = path.join(work, "cookie.json");
  const end = Date.now() + 120e3;
  while (Date.now() < end) {
    if (server.exitCode !== null) throw new Error(`serve_preview.py exited with ${server.exitCode}`);
    if (fs.existsSync(file)) {
      const info = JSON.parse(fs.readFileSync(file, "utf8"));
      const up = await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false);
      if (up) return info;
    }
    await sleep(200);
  }
  throw new Error("serve_preview.py did not come up within 120 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
