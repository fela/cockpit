// Cockpit's own work carries a clear tag (SPEC UI-43): an item whose project is Cockpit, and each of its steps, shows
// the Cockpit tag (the header's key in small, the jet on amber, and the word). Checks, at 320, 390 and 1280 px in the
// light and the dark theme:
// - a session's put of "Cockpit" stores the project as "cockpit";
// - the tag shows, visible and not clipped, in Next up's head, on a collapsed queue row, on the item's row in the work
//   list, in Pinned, under You handed these, in the steps closed in the last day, in Finished this week, in the status
//   lights' entry, in the search menu, and in the Pages tab's list and (wide) reader; the words show too except on a
//   phone's one-line rows (handed and closed rows, the lights), which keep the key alone;
// - a step that left the queue while open keeps its tag and its whole close button, open and collapsed;
// - beside it, a handed step's ask loses at most the key's room;
// - a step and an item of other work carry none;
// - its key is a dark tile with an amber jet that stands out from the tile;
// - the page does not scroll sideways, and has no page errors.
// Saves a screenshot of the top of the page per width and theme in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed. A run takes about a minute.
//
// Usage, from the checkout: node tests/browser/cockpit_tag.cjs

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
const TOKEN = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "cockpit-tag-test" };

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-tag-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    const cookie = { cookie: `${info.name}=${info.value}`, origin: info.origin };
    const agent = (method, url, body) => call(info.origin, { ...TOKEN, "content-type": "application/json" }, method, url, JSON.stringify(body));
    const user = (method, url, body) => call(info.origin, { ...cookie, "content-type": "application/json" }, method, url,
      body === undefined ? undefined : JSON.stringify(body));

    // The fixture database's open steps are snoozed, so this run's steps are Next up and the queue's first.
    const initial = await user("GET", "/api/state");
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    const run = Date.now();
    const put = async (slug, title, project, score) => {
      const key = `note:${slug}-${run}`;
      const item = await agent("PUT", "/api/items", { key, title, project, priority: "urgent" });
      const answer = await agent("POST", "/api/steps", {
        item_key: key, need: `${slug}-${run}`, ask: `${title}?`, score, session_id: SESSION_B, body: "Say yes or no.",
      });
      return { item, step: answer.step.id };
    };
    const tool = await put("tag-tool", "Tag the tool's work", "Cockpit", 100);
    const toolQueued = await put("tag-tool-two", "Tighter card spacing", "cockpit", 95);
    const other = await put("tag-other", "Merge the reminder fix", "tasks", 90);
    // A step the user hands to its session, one an agent closes, and an item the user closes.
    const handed = await put("tag-hand", "Explain the Run key", "cockpit", 50);
    await user("POST", `/api/steps/${handed.step}/hand-off`, { text: "Go ahead with the shorter wording" });
    const handedOther = await put("tag-hand-other", "Explain the API key", "tasks", 45);
    await user("POST", `/api/steps/${handedOther.step}/hand-off`, { text: "Go ahead with the shorter wording" });
    const closed = await put("tag-closed", "Shorter finish steps", "cockpit", 40);
    await agent("POST", `/api/steps/${closed.step}/done`, { note: "Built and deployed" });
    const archived = await put("tag-archived", "Drop the old strip", "cockpit", 30);
    await user("PATCH", `/api/items/${archived.item.id}`, { archived: true });
    // Items with no step: the work list shows them as rows, with agents. One is pinned and has a page.
    const resting = (slug, title, project) => agent("PUT", "/api/items", {
      key: `note:${slug}-${run}`, title, project, stage: "building", waiting_on: [{ kind: "agent", who: SESSION_B }], sessions: [SESSION_B],
    });
    const toolWork = await resting("tag-tool-work", "Quieter sweep notes", "cockpit");
    const otherWork = await resting("tag-other-work", "Add weekly reminders", "tasks");
    await user("PATCH", `/api/items/${toolWork.id}`, { pinned: true });
    await agent("POST", `/api/items/${toolWork.id}/pages`, { title: "Sweep notes plan", html: "<!doctype html><title>Plan</title><p>Plan</p>" });
    check("a put of \"Cockpit\" stores the project as cockpit", tool.item.project === "cockpit", tool.item.project);

    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const [width, height, scheme] of [[320, 740, "light"], [320, 740, "dark"], [390, 844, "light"], [390, 844, "dark"], [1280, 900, "light"], [1280, 900, "dark"]]) {
      const context = await browser.newContext({ viewport: { width, height }, colorScheme: scheme });
      const at = `${width} px, ${scheme}`;
      const phone = width <= 640;
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.setDefaultTimeout(30e3);
      page.setDefaultNavigationTimeout(60e3);
      // The fixture's idle timer can open a return dialog during the width and theme matrix.
      await page.addLocatorHandler(page.locator("dialog#idle[open]"), (dialog) => dialog.locator("button", { hasText: "Return" }).click()
        .catch((error) => { if (!/has been closed/.test(error.message)) throw error; }));
      // Moving the pointer clears the timer's idle veil before a click lands.
      await page.addLocatorHandler(page.locator("#idle-veil:popover-open"), async () => {
        await page.mouse.move(10, 10);
        await page.mouse.move(20, 20);
      }, { noWaitAfter: true });
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${info.origin}/classic`);
      const card = page.locator(`#next [data-step="${tool.step}"]`).first();
      // The first drawing can take a while on a busy host.
      await card.waitFor({ timeout: 30e3 });
      await page.screenshot({ path: path.join(work, `cockpit-tag-${width}-${scheme}.png`) });

      // The tag is the key alone at the end of the task's title, Next up's heading.
      const headTag = card.locator(".task-heading > .chip.cockpit");
      check(`${at}: Next up's heading ends in the Cockpit key`, await shows(headTag, false)
        && await headTag.evaluate((el) => el.previousElementSibling?.classList.contains("task-heading-text")
          && getComputedStyle(el.querySelector(".chip-words")).display === "none"), await describe(headTag));
      const toolRow = page.locator(`#queue .step-row[data-step="${toolQueued.step}"]`).first();
      const otherRow = page.locator(`#queue .step-row[data-step="${other.step}"]`).first();
      check(`${at}: a collapsed queue row of Cockpit work shows the tag`, await shows(toolRow.locator(".chip.cockpit"), true), await describe(toolRow.locator(".chip.cockpit")));
      check(`${at}: a queue row of other work shows none`, await otherRow.count() === 1 && await otherRow.locator(".chip.cockpit").count() === 0,
        `${await otherRow.count()} rows`);
      const workTag = page.locator(`#work .row[data-item="${toolWork.id}"] .chip.cockpit`);
      const otherRow2 = page.locator(`#work .row[data-item="${otherWork.id}"]`).first();
      const handedRow = page.locator(`#work .with-agents li[data-step="${handed.step}"]`).first();
      // Busy and Waiting start folded (VIEW-27): open the groups that hold the rows checked below.
      for (const row of [workTag.first(), handedRow]) {
        await row.evaluate((el) => { for (let at = el.closest("details"); at; at = at.parentElement.closest("details")) at.open = true; }).catch(() => {});
      }
      check(`${at}: the item's row in the work list shows the tag`, await shows(workTag, true), await describe(workTag));
      check(`${at}: other work's row in the work list shows none`, await otherRow2.count() === 1 && await otherRow2.locator(".chip.cockpit").count() === 0,
        `${await otherRow2.count()} rows`);
      const pinTag = page.locator(`#pinned [data-pinned="${toolWork.id}"] .chip.cockpit`);
      check(`${at}: Pinned shows the tag`, await shows(pinTag, true), await describe(pinTag));
      check(`${at}: You handed these shows the tag${phone ? ", the key alone" : ""}`, await shows(handedRow.locator(".chip.cockpit"), !phone),
        await describe(handedRow.locator(".chip.cockpit")));
      const askWidth = (step) => page.locator(`#work .with-agents li[data-step="${step}"] .row-title`).first()
        .evaluate((el) => el.getBoundingClientRect().width).catch(() => 0);
      const [withTag, without] = [await askWidth(handed.step), await askWidth(handedOther.step)];
      check(`${at}: beside the tag a handed step's ask loses at most the key's room to other work's`,
        withTag > 0 && withTag >= without - (phone ? 26 : 90), `${Math.round(withTag)} px against ${Math.round(without)} px`);
      // Folded lists, opened: the closed step and the closed item show the tag.
      const closedStep = page.locator(`.closed-list li[data-at]`).filter({ hasText: "Shorter finish steps" });
      const closedItem = page.locator(`#work .closed-item[data-item="${archived.item.id}"]`);
      for (const folded of [closedStep, closedItem]) {
        await folded.first().evaluate((el) => { for (let at = el.closest("details"); at; at = at.parentElement.closest("details")) at.open = true; }).catch(() => {});
      }
      check(`${at}: the steps closed in the last day show the tag${phone ? ", the key alone" : ""}`,
        await shows(closedStep.locator(".chip.cockpit"), !phone), await describe(closedStep.locator(".chip.cockpit")));
      check(`${at}: Finished this week's closed item shows the tag${phone ? ", the key alone" : ""}`,
        await shows(closedItem.locator(".chip.cockpit"), !phone), await describe(closedItem.locator(".chip.cockpit")));
      const paint = await headTag.evaluate((el) => {
        const mark = el.querySelector(".cockpit-mark");
        const jet = getComputedStyle(mark, "::after");
        const ground = getComputedStyle(el.closest(".next-card")).backgroundColor;
        const canvas = document.createElement("canvas").getContext("2d");
        const rgb = (colour) => { canvas.fillStyle = colour; canvas.fillRect(0, 0, 1, 1); return [...canvas.getImageData(0, 0, 1, 1).data].slice(0, 3); };
        const image = getComputedStyle(mark).backgroundImage;
        const stops = image.startsWith("linear-gradient") ? (image.match(/rgba?\([^)]*\)|#[0-9a-f]{3,8}/gi) || []).map(rgb) : [];
        return { jet: rgb(jet.backgroundColor), ground: rgb(ground), masked: jet.maskImage !== "none", tile: stops };
      });
      // The jet is drawn on the tile, so it has to stand apart from every colour of the tile's gradient.
      check(`${at}: the tag's key is a dark tile with an amber jet that stands out from the tile`,
        paint.tile.length >= 2 && paint.tile.every((stop) => Math.max(...stop) <= 80 && distance(paint.jet, stop) >= 160)
          && paint.masked && paint.jet[0] - paint.jet[2] >= 80, JSON.stringify(paint));
      // The stage counts at the top: Cockpit work has a stage of its own, so its plate entry needs no tag, and
      // other work's carries none either.
      const lights = await page.evaluate(([mine, theirs]) => {
        const entries = [...document.querySelectorAll(".light-item")];
        const where = (title) => entries.filter((entry) => entry.querySelector(".light-title")?.textContent === title)
          .map((entry) => ({ tag: Boolean(entry.querySelector(".chip.cockpit")), cockpit: Boolean(entry.closest(".stage-cockpit")) }));
        return { mine: where(mine), theirs: where(theirs) };
      }, ["Quieter sweep notes", "Add weekly reminders"]);
      check(`${at}: the stage counts hold Cockpit work under Cockpit, untagged, and other work elsewhere`,
        lights.mine.length === 1 && lights.mine[0].cockpit && !lights.mine[0].tag && lights.theirs.length > 0
          && !lights.theirs.some((entry) => entry.tag || entry.cockpit), JSON.stringify(lights));

      // A step an agent closes while the user has it open stays as "Left your queue", with its tag and its close.
      const leaving = await put(`tag-left-${width}-${scheme}`, "Trim the sweep log", "cockpit", 20);
      const leavingRow = page.locator(`#queue .step-row[data-step="${leaving.step}"]`).first();
      await leavingRow.waitFor();
      await leavingRow.locator(".row-age").click();
      await page.locator(`#queue .step-row.open[data-step="${leaving.step}"]`).waitFor();
      await agent("POST", `/api/steps/${leaving.step}/done`, { note: "Done elsewhere" });
      const leftRow = page.locator(`#queue .step-row.left[data-step="${leaving.step}"]`).first();
      await leftRow.waitFor({ timeout: 15e3 }).catch(() => {});
      check(`${at}: an open step that left the queue keeps its close button`,
        await leftRow.count() === 1 && await whole(leftRow.locator(".left-note button")), await leftRow.count() + " rows");
      await leftRow.locator(".row-age").click().catch(() => {});
      check(`${at}: collapsed, it shows the tag`, await shows(leftRow.locator(".row-line2 .chip.cockpit"), true), await describe(leftRow.locator(".chip.cockpit")));
      check(`${at}: collapsed, its close button is whole`,
        !await leftRow.evaluate((el) => el.classList.contains("open")).catch(() => true) && await whole(leftRow.locator(".left-note button")), await leftRow.count() + " rows");
      await leftRow.locator(".left-note button").click().catch(() => {});
      const wide = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      check(`${at}: the page does not scroll sideways`, wide <= 0, `${wide} px too wide`);

      // The search menu: the item's entry carries the tag, other work's none.
      await page.keyboard.press("Control+k");
      await page.locator(".command-dialog input[type=search]").fill("Quieter sweep notes");
      const command = page.locator(".command-dialog li[role=option]").filter({ hasText: "Quieter sweep notes" }).first();
      await command.waitFor().catch(() => {});
      // Search rows read like the sidebar's rows: the key alone.
      check(`${at}: the search menu's entry shows the tag's key`, await shows(command.locator(".chip.cockpit"), false), await describe(command.locator(".chip.cockpit")));
      await page.locator(".command-dialog input[type=search]").fill("Add weekly reminders");
      const otherCommand = page.locator(".command-dialog li[role=option]").filter({ hasText: "Add weekly reminders" }).first();
      await otherCommand.waitFor().catch(() => {});
      check(`${at}: other work's entry in the search menu shows none`, await otherCommand.count() === 1 && await otherCommand.locator(".chip.cockpit").count() === 0,
        `${await otherCommand.count()} entries`);
      await page.keyboard.press("Escape");
      check(`${at}: no page errors`, errors.length === 0, errors.join(" | "));

      // The Pages tab lists the item's page with the tag.
      await page.goto(`${info.origin}/pages`);
      const pageRow = page.locator(".pages-row").filter({ hasText: "Sweep notes plan" }).first();
      await pageRow.waitFor({ timeout: 15e3 }).catch(() => {});
      check(`${at}: the Pages tab's row shows the tag`, await shows(pageRow.locator(".chip.cockpit"), true), await describe(pageRow.locator(".chip.cockpit")));
      // Its reader, on a screen wide enough for one (a narrower one opens the page itself), names the item with the tag.
      if (width > 900) {
        await pageRow.click();
        const readerTag = page.locator(".pages-preview-head .chip.cockpit");
        await readerTag.first().waitFor({ timeout: 15e3 }).catch(() => {});
        check(`${at}: the Pages reader shows the tag`, await shows(readerTag, true), await describe(readerTag));
      }
      check(`${at}: no page errors on Pages`, errors.length === 0, errors.join(" | "));
      await context.close();
    }
    console.error(`screenshots in ${work}`);
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

// Whether exactly one tag is there and shows: visible (not hidden, transparent or folded away), inside every
// ancestor that clips it, its key 14 px, and, when `words`, its word "Cockpit" whole (not cut by an ellipsis).
async function shows(tag, words) {
  // A poll redraw or entrance animation can hide the tag for a moment.
  const end = Date.now() + 5000;
  do {
    if (await showsNow(tag, words)) return true;
    await sleep(100);
  } while (Date.now() < end);
  return false;
}

async function showsNow(tag, words) {
  if (await tag.count() !== 1) return false;
  return tag.evaluate((el, words) => {
    const own = el.getBoundingClientRect();
    const key = el.querySelector(".cockpit-mark").getBoundingClientRect();
    const seen = el.checkVisibility({ visibilityProperty: true, opacityProperty: true });
    let inside = true;
    for (let at = el.parentElement; at; at = at.parentElement) {
      const style = getComputedStyle(at);
      if (style.overflowX === "visible" && style.overflowY === "visible") continue;
      const box = at.getBoundingClientRect();
      inside &&= own.left >= box.left - 0.5 && own.right <= box.right + 0.5 && own.top >= box.top - 0.5 && own.bottom <= box.bottom + 0.5;
    }
    const mark = el.querySelector(".cockpit-mark");
    const label = el.querySelector(".chip-words");
    const keySeen = mark.checkVisibility({ visibilityProperty: true, opacityProperty: true });
    const whole = label.checkVisibility({ visibilityProperty: true, opacityProperty: true }) && label.scrollWidth <= label.clientWidth + 1 && label.textContent === "Cockpit";
    return seen && keySeen && inside && key.width === 14 && key.height === 14 && (!words || whole);
  }, words);
}

// Whether exactly one element is there, visible and inside every ancestor that clips it, its tap area too: a
// phone grows it with an ::after of its own size or more, centred on it (style.css).
async function whole(target) {
  if (await target.count() !== 1) return false;
  return target.evaluate((el) => {
    if (!el.checkVisibility({ visibilityProperty: true, opacityProperty: true })) return false;
    const own = el.getBoundingClientRect();
    const after = getComputedStyle(el, "::after");
    const grown = after.content !== "none" && after.position === "absolute";
    const half = (size, pseudo) => Math.max(size, grown ? parseFloat(pseudo) || 0 : 0) / 2;
    const [x, y] = [(own.left + own.right) / 2, (own.top + own.bottom) / 2];
    const area = { left: x - half(own.width, after.width), right: x + half(own.width, after.width),
      top: y - half(own.height, after.height), bottom: y + half(own.height, after.height) };
    for (let at = el.parentElement; at; at = at.parentElement) {
      const style = getComputedStyle(at);
      if (style.overflowX === "visible" && style.overflowY === "visible") continue;
      const box = at.getBoundingClientRect();
      if (area.left < box.left - 0.5 || area.right > box.right + 0.5 || area.top < box.top - 0.5 || area.bottom > box.bottom + 0.5) return false;
    }
    return true;
  });
}

async function describe(tag) {
  if (await tag.count() === 0) return "no tag";
  return tag.first().evaluate((el) => JSON.stringify({
    box: el.getBoundingClientRect(), scroll: el.scrollWidth, client: el.clientWidth, seen: el.checkVisibility({ visibilityProperty: true, opacityProperty: true }),
  }));
}

// How far apart two sRGB colours are, 0 to 441.
function distance(a, b) {
  return Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
}

async function call(origin, headers, method, url, body) {
  const response = await fetch(`${origin}${url}`, { method, headers, body });
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
