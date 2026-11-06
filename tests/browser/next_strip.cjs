// Next up's marks on a phone (SPEC UI-32, UI-16): the step's marks have the card's first row to themselves, at its
// right, above the item's line, so a crowded set (Updated and a timer running over an hour; the check and the info
// mark sit under More) never covers that line. The card has no "Next up" label. Checks, for the step in Next up: -
// at 390 and 360 px, with only its usual marks, they sit at the card's right above the item's line, with no   label
// before it; - at 390 and 360 px, with every mark showing and the timer running, paused or held, the marks overlap
// neither the label nor the item's line (priority, chips, title), stay in the card, their 44 px tap areas (read

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
const TOKEN = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "next-strip-test" };

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-next-strip-");
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

    // The fixture database's open steps are snoozed, so this run's step is Next up.
    const copied = await user("GET", "/api/state");
    await Promise.all([...copied.queue, ...copied.ready_next, ...copied.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    const run = Date.now();
    const key = `note:next-strip-${run}`;
    await agent("PUT", "/api/items", { key, title: "Strip test", priority: "urgent" });
    const answer = await agent("POST", "/api/steps", {
      item_key: key, need: `strip-${run}`, ask: "Ship it?", score: 100, session_id: SESSION_B, body: "Say yes or no.",
    });
    const stepId = answer.step.id;

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const context = await browser.newContext({ viewport: { width: 390, height: 800 } });
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    page.setDefaultNavigationTimeout(60000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`${info.origin}/classic`);
    const card = page.locator(`#next .next-card[data-step="${stepId}"]`).first();
    // The first drawing can take a while on a busy host.
    await card.waitFor({ timeout: 30e3 });

    // The step's marks and timer, set in the page's own state and drawn once. `timer` is "running", "paused"
    // or "held" (the feedback box holds the timer); `crowded` adds Updated, a size bar and a fresh check.
    const show = (crowded, timer) => page.evaluate(([id, crowdedOn, state]) => {
      const step = ui.state.queue.find((one) => one.id === id);
      const item = ui.state.items.find((one) => one.id === step.item_id);
      const started = new Date(Date.now() - 83 * 60e3).toISOString();
      step.timer = { started_at: state === "running" ? started : null };
      item.time_spent = state === "running" ? { seconds: 0, started_at: started } : { seconds: 83 * 60, started_at: null };
      ui.feedbackHold = state === "held" ? { stepId: id } : null;
      if (crowdedOn) ui.seenText.set(id, "older words");
      else ui.seenText.delete(id);
      step.size = crowdedOn ? "large" : null;
      step.progress = crowdedOn ? 40 : null;
      step.checked_at = crowdedOn ? new Date().toISOString() : null;
      render(false);
    }, [stepId, crowded, timer]);

    // A label would be the head's ::before, leading the item's line: the room from the head's left edge to
    // the line's first part, as tall as that part. A tap area is the mark's box grown to 44 px.
    const measure = () => card.evaluate((el) => {
      const head = el.querySelector(".step-context");
      const box = head.getBoundingClientRect();
      const style = getComputedStyle(head, "::before");
      const shown = (node) => !node.hidden && node.getBoundingClientRect().width > 0;
      // With the task name as the card's heading, the line can hold the marks alone: the label then has no room.
      const lead = [...head.children].find((child) => !child.matches(".context-marks") && shown(child));
      const first = lead ? lead.getBoundingClientRect() : { left: box.left, top: box.top, bottom: box.top };
      const label = { left: box.left, right: first.left, top: first.top, bottom: first.bottom };
      const strip = head.querySelector(".context-marks").getBoundingClientRect();
      const marks = [...head.querySelectorAll(".context-marks > *")].filter(shown);
      const rects = marks.map((mark) => mark.getBoundingClientRect());
      // A tap area is the element's box joined with the box its ::after draws (the page's 44 px areas). The
      // running timer is no key itself: its pause key is.
      const tapArea = (node) => {
        const rect = node.getBoundingClientRect();
        const after = getComputedStyle(node, "::after");
        if (after.content === "none" || after.position !== "absolute") return rect;
        const shift = new DOMMatrixReadOnly(after.transform === "none" ? undefined : after.transform);
        const left = rect.left + node.clientLeft + parseFloat(after.left) + shift.e;
        const top = rect.top + node.clientTop + parseFloat(after.top) + shift.f;
        return {
          left: Math.min(left, rect.left), top: Math.min(top, rect.top),
          right: Math.max(left + parseFloat(after.width), rect.right), bottom: Math.max(top + parseFloat(after.height), rect.bottom),
        };
      };
      // The ⋯ menu (VIEW-29) takes the tap on its key.
      const taps = [...head.querySelectorAll(".context-marks > :not(.step-menu), .context-marks > .step-menu > summary")]
        .filter(shown).map(tapArea);
      // The item's line is the head's own parts and, on a card headed by the task's name, that heading with its marks.
      const heading = [...el.querySelectorAll(".task-heading")].filter(shown);
      const lineTaps = [...head.querySelectorAll(":scope > .prio, :scope > .context-tags > *"),
        ...heading.flatMap((node) => [...node.querySelectorAll(".chip.cockpit, .draft-mark, .quick-mark")])].filter(shown).map(tapArea);
      const lineParts = [...head.querySelectorAll(":scope > :not(.context-marks), :scope > .context-tags > *"), ...heading]
        .filter(shown).map((part) => part.getBoundingClientRect());
      const cardBox = el.getBoundingClientRect();
      const hits = (a, b) => a.left < b.right - 0.5 && b.left < a.right - 0.5 && a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5;
      const tapHits = taps.flatMap((a, i) => taps.slice(i + 1).filter((b) => hits(a, b))).length
        + taps.filter((a) => lineTaps.some((b) => hits(a, b))).length;
      return {
        count: rects.length,
        onLabel: rects.filter((rect) => hits(rect, label)).length,
        onLine: rects.filter((rect) => lineParts.some((part) => hits(rect, part))).length,
        tapHits,
        inside: rects.every((rect) => rect.left >= cardBox.left && rect.right <= cardBox.right && rect.top >= cardBox.top)
          && taps.every((tap) => tap.top >= cardBox.top - 0.5),
        noLabel: ["none", "normal", '""'].includes(style.content),
        atRight: rects.length > 0 && Math.abs(Math.max(...rects.map((rect) => rect.right)) - strip.right) <= 1,
        lineBelow: lineParts.every((part) => part.top >= strip.bottom - 1),
        headed: heading.length,
      };
    });
    // The ask's top once the page has settled after a resize or a redraw, read from the card on the page now.
    const askTop = async () => {
      await sleep(100);
      return page.evaluate((id) => document.querySelector(`#next .next-card[data-step="${id}"] .ask`).getBoundingClientRect().top, stepId);
    };

    for (const width of [390, 360]) {
      await page.setViewportSize({ width, height: 800 });
      await show(false, "running");
      const plain = await measure();
      check(`${width} px: with its usual marks, they sit at the card's right above the item's line, with no label`,
        plain.count >= 1 && plain.headed === 1 && plain.noLabel && plain.atRight && plain.lineBelow && plain.onLabel === 0, JSON.stringify(plain));

      await show(true, "running");
      const crowded = await measure();
      check(`${width} px: with every mark showing, none covers the label or the item's line`,
        crowded.count >= 2 && crowded.onLabel === 0 && crowded.onLine === 0, JSON.stringify(crowded));
      check(`${width} px: the marks stay in the card, their tap areas no higher than its top`, crowded.inside, JSON.stringify(crowded));
      check(`${width} px: the marks' tap areas stay apart and clear of the item's line's`,
        crowded.tapHits === 0, JSON.stringify(crowded));
      check(`${width} px: the item's line starts below the marks`, crowded.lineBelow, JSON.stringify(crowded));
      for (const timer of ["paused", "held"]) {
        await show(true, timer);
        const other = await measure();
        check(`${width} px: with every mark showing and the timer ${timer}, none covers the label or the item's line, and its tap areas stay apart and clear of the item's line's`,
          other.onLabel === 0 && other.onLine === 0 && other.tapHits === 0, JSON.stringify(other));
      }

      await show(true, "running");
      await sleep(300);
      const shot = path.join(work, `next-strip-${width}.png`);
      await card.waitFor({ timeout: 30e3 });
      const box = await card.boundingBox();
      await page.screenshot({ path: shot, clip: { x: box.x, y: box.y, width: box.width, height: 160 }, timeout: 30e3 });
      console.error(`  screenshot: ${shot}`);
    }

    // Play, pause, hold and Updated coming or going (UI-16): at each width the ask's place is the same in all
    // six, and every mark stays clear of the item's line and in the card, its tap areas apart.
    const moved = [];
    const crossed = [];
    for (let width = 320; width <= 640; width += 8) {
      await page.setViewportSize({ width, height: 800 });
      const tops = [];
      for (const crowded of [false, true]) {
        for (const timer of ["running", "paused", "held"]) {
          await show(crowded, timer);
          tops.push(await askTop());
          const seen = await measure();
          if (seen.onLabel || seen.onLine || seen.tapHits || !seen.inside || !seen.lineBelow) {
            crossed.push(`${width} px ${crowded ? "crowded" : "plain"} ${timer}: ${JSON.stringify(seen)}`);
          }
        }
      }
      if (Math.max(...tops) - Math.min(...tops) > 0.5) moved.push(`${width} px: ${tops.map(Math.round).join("/")}`);
    }
    check("320 to 640 px: the ask stays put on play, pause, hold and when Updated, size and check come or go",
      moved.length === 0, moved.join("; "));
    check("320 to 640 px, in all six states: the marks cover nothing, stay in the card, and their tap areas stay apart and clear of the item's line's",
      crossed.length === 0, crossed.slice(0, 4).join("; "));
    check("no page errors", errors.length === 0, errors.join(" | "));
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
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
