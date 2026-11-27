// A step holding an unsent draft (SPEC NEXT-39): the step shows plainly that words wait in its box. Checks, at phone
// and desktop width in the light theme and at desktop width in the dark, on the step on the card and a second one
// picked from its row in Your queue:
// - no mark and no pencil on the step's row while the box is empty;
// - typing shows the draft's mark in the card's head at once: a chip in the box's amber, its pencil alone at the end of
//   the item's title, "Unsent draft" in its tooltip and accessible name; the box has no "Not sent yet" line and, as the
//   step has a session, no picker of where the words go, Send going to that session;
// - beside Updated and a running timer the mark overlaps nothing and stays in the card;
// - the whole box holding the draft is amber, clearly unlike the card and an empty box, while typing and away from it,
//   and its words keep the text colour and weight;
// - the step's row in the list shows a pencil in the user's amber;
// - after a reload the mark and the row's pencil are there again;
// - a second step holding a draft keeps the pencil on its row while another step is on the card; back on it, its box
//   is the same amber with the words in it; Send goes to the step's session and takes the pencil away, and when
//   Cockpit refuses the reply the words and the pencil come back;
// - emptying the box takes the mark, the colour and the row's pencil away;
// - no page errors.
// Saves a screenshot of the Next up card per width in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs) and
// drives a headless Chromium. Prints one line per check and exits 1 when one failed.
// A run takes about half a minute.
//
// Usage, from the checkout: node tests/browser/draft_marker.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder, screenshotRedrawn } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
// The harness's agent token and second fake session (tests/conftest.py).
const SESSION_B = "22222222-2222-4222-8222-222222222222";
const TOKEN = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "draft-marker-test" };

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-draft-marker-");
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

    // The fixture database's open steps are snoozed, so this run's two steps lead Your queue.
    const initial = await user("GET", "/api/state");
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    const run = Date.now();
    const put = async (slug, title, score) => {
      const key = `note:${slug}-${run}`;
      await agent("PUT", "/api/items", { key, title, priority: "urgent" });
      const answer = await agent("POST", "/api/steps", {
        item_key: key, need: `${slug}-${run}`, ask: `${title}?`, score, session_id: SESSION_B, body: "Say yes or no.",
      });
      return answer.step.id;
    };
    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const [width, height, scheme] of [[390, 844, "light"], [1280, 900, "light"], [1280, 900, "dark"]]) {
      // Cockpit keeps drafts for every device, so each size gets two steps of its own, with no words kept from the
      // size before.
      const first = await put(`draft-first-${width}-${scheme}`, "Pick the reminder time", 100);
      const second = await put(`draft-second-${width}-${scheme}`, "Merge the fix", 90);
      const context = await browser.newContext({ viewport: { width, height }, colorScheme: scheme });
      const at = `${width} px, ${scheme}`;
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.setDefaultTimeout(5000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${info.origin}/`);
      const card = page.locator(`#next [data-step="${first}"]`).first();
      // The first drawing can take a while on a busy host.
      await card.waitFor({ timeout: 30e3 });
      // The mark ends the task's title: Next up's heading.
      const mark = card.locator(".task-heading > .draft-mark");
      const box = card.locator(`textarea[data-key="msg:step:${first}"]`);
      // The items of the two steps, whose rows in the list carry the pencil.
      const itemOf = async (id) => Number(await page.locator(`#sidebar li[data-step="${id}"]`).first().getAttribute("data-item"));
      const firstItem = await itemOf(first);
      const secondItem = await itemOf(second);

      check(`${at}: no mark while the box is empty, and no pencil on the step's row`,
        (await mark.count()) === 1 && await mark.isHidden() && (await pencil(page, firstItem)) === null, `${await mark.count()} marks`);
      const empty = await boxLook(box);
      await box.click();
      await page.keyboard.type("Keep completed tasks out of the shared list export");
      const shape = await mark.evaluate((el) => ({
        seen: el.checkVisibility(),
        endsTitle: el.previousElementSibling?.matches(".task-heading-text, .chip.cockpit") === true,
        pencilAlone: getComputedStyle(el.querySelector(".label")).display === "none" && Boolean(el.querySelector(".icon")),
        named: el.dataset.tip === "Unsent draft" && el.getAttribute("aria-label") === "Unsent draft",
      }));
      check(`${at}: typing shows the draft's pencil at the end of the title at once, named Unsent draft`,
        shape.seen && shape.endsTitle && shape.pencilAlone && shape.named, JSON.stringify(shape));
      // Clear of the heading's left edge up to its first part, where a label before it would be.
      const clearOfLabel = () => mark.evaluate((el) => {
        const head = el.closest(".task-heading");
        const box = head.getBoundingClientRect();
        const first = [...head.children].find((child) => child.getBoundingClientRect().width).getBoundingClientRect();
        const own = el.getBoundingClientRect();
        return own.right <= box.left || own.left >= first.left || own.bottom <= first.top || own.top >= first.bottom;
      });
      check(`${at}: the mark stays clear of the item's line's start`, await clearOfLabel(), "overlaps it");
      // A crowded head: Updated and a running timer beside the draft (the page's own state, drawn once).
      await page.evaluate((id) => {
        const step = ui.state.queue.find((one) => one.id === id);
        ui.seenText.set(step.id, "older words");
        step.timer = { started_at: new Date().toISOString() };
        render(false);
      }, first);
      const crowded = await mark.evaluate((el) => {
        const own = el.getBoundingClientRect();
        const card = el.closest(".next-card").getBoundingClientRect();
        const others = [...el.closest(".next-card").querySelectorAll(".context-marks > *, .task-heading-text, .prio")]
          // Each line of a wrapped title on its own: on a phone the heading wraps under the floated marks.
          .filter((other) => !other.hidden).flatMap((other) => [...other.getClientRects()])
          .filter((rect) => rect.width && own.left < rect.right && rect.left < own.right && own.top < rect.bottom && rect.top < own.bottom);
        return { overlaps: others.length, inside: own.left >= card.left && own.right <= card.right };
      });
      check(`${at}: beside Updated and a running timer the mark overlaps nothing and stays in the card`,
        crowded.overlaps === 0 && crowded.inside && await clearOfLabel(), JSON.stringify(crowded));
      // The box holds no "Not sent yet" line  and, as the step has a session, no picker of where the
      // words go: Send goes to the step's own session.
      const lines = await card.locator(".composer").evaluate((el) => ({
        notSent: el.textContent.includes("Not sent yet"),
        // Words straight in the box, outside its parts (a stray "false").
        stray: [...el.childNodes].filter((node) => node.nodeType === 3 && node.textContent.trim()).map((node) => node.textContent),
        pickers: el.querySelectorAll("select, .composer-target").length,
        // motion.js moves a title to data-tip.
        send: el.querySelector(".send").dataset.tip || el.querySelector(".send").title,
      }));
      check(`${at}: the box has no Not sent yet line and no picker, and Send goes to the step's session`,
        !lines.notSent && !lines.stray.length && lines.pickers === 0 && /^Send: the session/.test(lines.send), JSON.stringify(lines));
      // The whole box is amber while typing and away from it, unlike the card's and an empty box's fill and with an amber left bar,
      // with no corner mark of its own; its words keep the text's look.
      const plain = (look) => Boolean(look) && distance(look.text, empty.text) < 2 && look.weight === empty.weight && look.same;
      // The amber fill is the pale wash of the user's own messages, so the amber left bar carries the mark with it.
      const marked = (look) => plain(look) && distance(look.bg, look.card) >= 15 && distance(look.bg, empty.bg) >= 15
        && look.bg[0] - look.bg[2] >= 20 && Boolean(look.bar) && look.bar[0] - look.bar[2] >= 60 && !empty.bar && !look.ownCorner;
      const typing = await boxLook(box);
      check(`${at}: while typing, the box is amber and its words keep the text colour`, marked(typing), JSON.stringify({ empty, typing }));
      const typingPencil = await waitFor(() => pencil(page, firstItem), Boolean);
      check(`${at}: while typing, the step's row in the list shows the pencil in amber`, amberPencil(typingPencil), JSON.stringify(typingPencil));
      await page.evaluate(() => document.activeElement && document.activeElement.blur());
      const away = await boxLook(box);
      check(`${at}: away from the box, it stays amber`, marked(away), JSON.stringify({ empty, away }));
      // The Unsent draft chip is the same amber box: the box's fill, a frame, and amber words.
      // The frame shows against the fill, and the words are amber and readable on it (4.5:1).
      const sameAmber = (look) => Boolean(look) && distance(look.bg, away.bg) < 2 && Boolean(look.frame) && look.width >= 1
        && distance(look.frame, look.bg) >= 25 && look.text[0] - look.text[2] >= 60 && contrast(look.text, look.bg) >= 4.5;
      const chip = await chipLook(mark);
      check(`${at}: the Unsent draft chip has the box's amber fill, a frame and amber words`, sameAmber(chip), JSON.stringify({ chip, box: away.bg }));
      await sleep(300);
      const shot = path.join(work, `draft-marker-${width}-${scheme}.png`);
      await screenshotRedrawn(card, { path: shot, timeout: 30e3 });
      console.error(`  screenshot: ${shot}`);

      await page.reload();
      await card.waitFor({ timeout: 30e3 });
      check(`${at}: after a reload the mark is there again`, await waitFor(() => mark.isVisible(), Boolean), "hidden");
      const reloadPencil = await waitFor(() => pencil(page, firstItem), Boolean);
      check(`${at}: after a reload the step's row shows the pencil again`, amberPencil(reloadPencil), JSON.stringify(reloadPencil));

      // The second step, picked from its row, takes a draft; picking the first again leaves the pencil on the
      // second's row, and back on it its box is the same amber with the words in it.
      const secondCard = page.locator(`#next [data-step="${second}"]`).first();
      const secondBox = secondCard.locator(`textarea[data-key="msg:step:${second}"]`);
      await pick(page, second);
      await secondCard.waitFor({ timeout: 30e3 });
      await secondBox.click();
      await page.keyboard.type("Later");
      await pick(page, first);
      await card.waitFor({ timeout: 30e3 });
      const leftPencil = await waitFor(() => pencil(page, secondItem), Boolean);
      check(`${at}: a step holding a draft off the card keeps the pencil on its row`, amberPencil(leftPencil), JSON.stringify(leftPencil));
      await pick(page, second);
      await secondCard.waitFor({ timeout: 30e3 });
      const secondLook = await boxLook(secondBox);
      const secondWords = await page.evaluate((key) => ui.drafts.get(key) || "", `msg:step:${second}`);
      check(`${at}: back on the step, its box holds the words in the same amber`,
        plain(secondLook) && distance(secondLook.bg, away.bg) < 2 && !secondLook.ownCorner && secondWords === "Later",
        JSON.stringify({ secondLook, secondWords, away }));
      // With a new session picked for this box before (a choice kept from the picker), Send still goes to the
      // step's own session. The pencil leaves with the words, and comes back with them when Cockpit refuses the
      // reply.
      // Cockpit holds the reply until the test has looked at the row, then refuses it.
      let handed = null;
      let refuse = null;
      const looked = new Promise((resolve) => { refuse = resolve; });
      await page.route("**/hand-off", async (route) => {
        handed = route.request().postDataJSON();
        await looked;
        return route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: "Test refusal" }) });
      });
      const secondKey = `msg:step:${second}`;
      await page.evaluate((key) => { ui.choices.set(key, `new:${ui.state.checkouts[0]}`); render(false); }, secondKey);
      await secondCard.locator(".composer .send").click();
      await waitFor(async () => handed, Boolean, 30e3);
      const left = await waitFor(() => pencil(page, secondItem), (shown) => !shown);
      refuse();
      const back = await waitFor(() => page.evaluate((key) => ui.drafts.get(key) || "", secondKey), (words) => words === "Later", 30e3);
      const backPencil = await waitFor(() => pencil(page, secondItem), Boolean);
      check(`${at}: Send goes to the step's session and takes the pencil away; refused, the words and pencil come back`,
        !left && handed && handed.session_id === SESSION_B && !handed.new_cwd && back === "Later" && amberPencil(backPencil),
        JSON.stringify({ left, handed, back, backPencil }));
      await page.unroute("**/hand-off");
      // On a phone the refusal's toast lies over the phone bar's back key.
      await page.evaluate(() => closeToast());

      await pick(page, first);
      await card.waitFor({ timeout: 30e3 });
      await box.click();
      await page.keyboard.press("ControlOrMeta+a");
      await page.keyboard.press("Delete");
      const emptied = await boxLook(box);
      const emptiedPencil = await waitFor(() => pencil(page, firstItem), (shown) => !shown);
      check(`${at}: emptying the box takes the mark, the box's colour and the row's pencil away`,
        await mark.isHidden() && (await card.locator(".composer.drafted").count()) === 0
          && plain(emptied) && distance(emptied.bg, empty.bg) < 2 && emptiedPencil === null, JSON.stringify({ emptied, emptiedPencil }));
      check(`${at}: no page errors`, errors.length === 0, errors.join(" | "));
      await context.close();
      // The next size's steps lead Your queue.
      await Promise.all([first, second].map((id) => user("POST", `/api/steps/${id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    }
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

// A reply box's look once it is on the page, or null if it does not settle within 5 s: the fill the eye sees in
// its box and in the card under it (every fill beneath painted first, so a transparent box shows what is under
// it), and the colour and weight of the words as drawn (the editor's lines, and the text box itself when it
// shows). Colours are sRGB from a canvas, so that any CSS colour compares.
// A draft chip's look: its fill as seen over what lies behind it, its words' colour, and whether it has a frame.
function chipLook(mark) {
  return mark.evaluate((el) => {
    const paint = document.createElement("canvas").getContext("2d");
    const over = (colours) => {
      paint.fillStyle = "#fff";
      paint.fillRect(0, 0, 1, 1);
      for (const colour of colours) {
        paint.fillStyle = colour;
        paint.fillRect(0, 0, 1, 1);
      }
      return [...paint.getImageData(0, 0, 1, 1).data].slice(0, 3);
    };
    const fills = [];
    for (let at = el; at; at = at.parentElement) fills.unshift(getComputedStyle(at).backgroundColor);
    const style = getComputedStyle(el);
    // The frame's colour leads the computed shadow ("oklch(…) 0px 0px 0px 1px inset"), painted over the fill.
    const frame = style.boxShadow === "none" ? null : style.boxShadow.match(/^(\w+\([^)]*\)|#\w+|\w+)/)[1];
    // Its width is the shadow's spread, the fourth length.
    const width = frame ? parseFloat((style.boxShadow.slice(frame.length).match(/-?[\d.]+px/g) || [])[3] || "0") : 0;
    return { bg: over(fills), text: over([...fills, style.color]), frame: frame && over([...fills, frame]), width };
  });
}

function boxLook(area) {
  return waitFor(() => area.evaluate((el) => {
    const box = el.isConnected && el.closest(".composer");
    if (!box) return null;
    const paint = document.createElement("canvas").getContext("2d");
    const over = (colours) => {
      paint.fillStyle = "#fff";
      paint.fillRect(0, 0, 1, 1);
      for (const colour of colours) {
        paint.fillStyle = colour;
        paint.fillRect(0, 0, 1, 1);
      }
      return [...paint.getImageData(0, 0, 1, 1).data].slice(0, 3);
    };
    const seen = (node) => {
      const fills = [];
      for (let at = node; at; at = at.parentElement) fills.unshift(getComputedStyle(at).backgroundColor);
      return over(fills);
    };
    const drawn = [...box.querySelectorAll(".cm-line"), el].filter((node) => node.getBoundingClientRect().width > 0)
      .map((node) => getComputedStyle(node));
    if (!drawn.length || !drawn[0].fontWeight) return null;
    return {
      bg: seen(box),
      card: seen(box.closest(".card")),
      text: over([drawn[0].color]),
      weight: drawn[0].fontWeight,
      same: drawn.every((style) => style.color === drawn[0].color && style.fontWeight === drawn[0].fontWeight),
      // The box draws no corner mark of its own. The sidebar's layer that fills the ring behind the box's rounded
      // corners paints only a shadow, so it is no mark.
      ownCorner: ((after) => after.content !== "none"
        && (after.backgroundImage !== "none" || after.backgroundColor !== "rgba(0, 0, 0, 0)"))(getComputedStyle(box, "::after")),
      // The 3 px bar in the user's amber down its left edge, as on the user's own latest message.
      bar: ((shadow) => shadow ? over([shadow[1]]) : null)(getComputedStyle(box).boxShadow.match(/^((?:rgba?|oklch|color)\([^)]*\)) 3px 0px 0px 0px inset/)),
    };
  }), Boolean);
}

// Shows the step on the card from its row in Your queue. On a phone the list and the card are two screens, so the
// phone bar's back key shows the list first.
async function pick(page, id) {
  const back = page.locator(".phone-back:visible");
  if (await back.count()) await back.click();
  await page.locator(`#sidebar .sidebar-group.queue li[data-step="${id}"] > .side-row`).click();
}

// The pencil on the item's row in the list, or null while it is hidden: its colour as drawn, from a canvas so that any
// CSS colour compares. On a phone's item screen the list is off screen, so the row's own display is what counts.
function pencil(page, itemId) {
  return page.evaluate((itemId) => {
    const mark = document.querySelector(`#sidebar li[data-item="${itemId}"] .sidebar-draft`);
    if (!mark || mark.hidden || getComputedStyle(mark).display === "none") return null;
    const paint = document.createElement("canvas").getContext("2d");
    paint.fillStyle = getComputedStyle(mark).color;
    paint.fillRect(0, 0, 1, 1);
    return { colour: [...paint.getImageData(0, 0, 1, 1).data].slice(0, 3), icon: Boolean(mark.querySelector(".icon")) };
  }, itemId);
}

// A pencil that shows, with its icon, in the user's amber.
function amberPencil(look) {
  return Boolean(look) && look.icon && look.colour[0] - look.colour[2] >= 60;
}

// How far apart two sRGB colours are, 0 to 441.
// The WCAG contrast ratio of two sRGB colours.
function contrast(a, b) {
  const light = (rgb) => rgb.map((v) => v / 255).map((v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4))
    .reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
  const [high, low] = [light(a), light(b)].sort((x, y) => y - x);
  return (high + 0.05) / (low + 0.05);
}

function distance(a, b) {
  return Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
}

// The value `read` gives once `ok` holds it, or its last value after `ms` (5 s unless given).
async function waitFor(read, ok, ms = 5000) {
  const end = Date.now() + ms;
  let value = await read();
  while (!ok(value) && Date.now() < end) {
    await sleep(100);
    value = await read();
  }
  return value;
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
