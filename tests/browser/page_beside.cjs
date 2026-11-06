// A page opened from a step's card sits beside the card. It checks, on `/` at 1440 px in
// light and on `/classic` at 1280 px in dark:
// - the page slides in from the right, and the card slides to a column at the left;
// - only the card shows at the left (no rail, list, queue or header), with its reply box on screen, and the page
//   takes the rest of the window, not modal: the card takes clicks and typing;
// - the Pages view in the layer leaves out its own reply box and the step's ask;
// - a comment left on the page shows at once, open, above the card's reply box, while the page stays;
// - a comment deleted from the card leaves the page's comment column;
// - Send on the card sends the answer with the comment and closes the page, sliding out, with the page laid out
//   as before;
// - the X closes it, sliding out, with the rail back and the page scrolled as before; Escape on the card closes it;
// - another page link on the card replaces the page beside it, keeping the layout;
// and that at 1000 px the page slides over the whole window from the right, at 390 px from the bottom; no page
// errors and no Content Security Policy errors.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs)
// and drives headless Chromium. Prints one line per check, saves screenshots in SHOTS_DIR (default: a temporary
// directory) and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/page_beside.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "beside-test", "content-type": "application/json" };
const SHOTS = process.env.SHOTS_DIR || fs.mkdtempSync(path.join(os.tmpdir(), "cockpit-beside-shots-"));

const REPORT = (title) => `<!doctype html><html><head><style>
body{font:16px/1.5 system-ui;margin:24px}</style></head><body>
<h1>${title}</h1><p>The grocery list export includes open tasks and their reminder dates.</p>
${Array.from({ length: 30 }, (_, n) => `<p>Line ${n + 1} of the report.</p>`).join("")}
</body></html>`;

const { chromium } = require(PLAYWRIGHT);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Records each animation Cockpit's page starts: on what, and its first keyframe's transform.
const RECORD_ANIMATIONS = () => {
  window.__animations = [];
  const animate = Element.prototype.animate;
  Element.prototype.animate = function (frames, options) {
    const first = Array.isArray(frames) ? frames[0] : null;
    const last = Array.isArray(frames) ? frames[frames.length - 1] : null;
    window.__animations.push({
      on: this.tagName === "DIALOG" ? "dialog" : this.classList.contains("beside-card") ? "card" : this.className,
      from: first && first.transform, to: last && last.transform,
    });
    return animate.call(this, frames, options);
  };
};

async function main() {
  const work = workFolder("cockpit-beside-");
  const server = spawn(path.join(__dirname, "..", "..", ".venv", "bin", "python"), [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "ignore", "inherit"] });
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
    // Each window size starts with the queue to itself: the copy's steps and the last size's leftovers sleep.
    const clearQueue = async () => {
      const state = await user("GET", "/api/state");
      await Promise.all([...state.queue, ...state.ready_next].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    };
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const run = Date.now();

    // A step whose text links two pages on its item; returns the ids.
    const makeStep = async (label, long) => {
      const key = `note:beside-${run}-${label}`;
      const item = await agent("PUT", "/api/items", { key, title: `Beside test ${label}`, priority: "urgent", sessions: [SESSION_B] });
      const report = await agent("POST", `/api/items/${item.id}/pages`, { title: "Exports report", html: REPORT("Exports in January") });
      const other = await agent("POST", `/api/items/${item.id}/pages`, { title: "Reminders report", html: REPORT("Reminders in January") });
      // A third page, for three quick clicks.
      const third = await agent("POST", `/api/items/${item.id}/pages`, { title: "Sharing report", html: REPORT("Sharing in January") });
      const notes = long ? Array.from({ length: 40 }, (_, n) => `\n\nNote ${n + 1} on the reports.`).join("") : "";
      const answer = await agent("POST", "/api/steps", {
        item_key: key, need: `beside-${label}`, ask: "Ship the exports report?", score: 100, session_id: SESSION_B,
        body: `Read [the exports report](${report.url}), [the reminder report](${other.url}) and [the sharing report](${third.url}).${notes}`,
      });
      return { item: item.id, report: report.id, revision: report.revision, other: other.id, third: third.id, step: answer.step.id };
    };

    const sizes = [["wide", 1440, 900, "light", "/"], ["classic", 1280, 800, "dark", "/classic"]];
    for (const [label, width, height, scheme, home] of sizes) {
      const at = `${label} (${width} px ${scheme}, ${home})`;
      await clearQueue();
      const { page, errors, shot } = await newPage(browser, info, { width, height }, scheme, label);
      const first = await makeStep(`${label}-1`);
      const second = await makeStep(`${label}-2`);
      const goTo = async (ids) => {
        // A change of hash alone would keep the page drawn as it was.
        await page.goto("about:blank");
        await page.goto(`${info.origin}${home}${home === "/" ? `#home/item/${ids.item}` : ""}`);
        const card = page.locator(`#next [data-step="${ids.step}"]`).first();
        await card.waitFor().catch(async (error) => {
          await page.screenshot({ path: path.join(SHOTS, `${label}-missing-${ids.step}.png`) });
          const state = await user("GET", "/api/state");
          const where = ["queue", "ready_next", "with_agents"].map((name) => `${name}: ${(state[name] || []).map((step) => step.id).join(",")}`).join("; ");
          throw new Error(`step ${ids.step} not shown (${where}): ${error.message}`);
        });
        if (await card.evaluate((el) => el.classList.contains("step-row") && !el.classList.contains("open"))) await card.locator(".row-head").click();
        return card;
      };
      const layerShown = page.locator("dialog.page-overlay[open]:not(.page-overlay-loading-page)");

      // Open beside the card.
      let card = await goTo(first);
      await page.evaluate(() => { window.__animations.length = 0; });
      await card.locator(`a[href*="/pages?id=${first.report}&step=${first.step}"]:visible`).first().click();
      await layerShown.waitFor();
      const layer = await openLayer(page);
      await figureIn(layer, first.report);
      await sleep(500);
      const layout = await page.evaluate((stepId) => {
        const visible = (el) => Boolean(el) && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
        const card = document.querySelector(`#next [data-step="${stepId}"]`);
        const frame = document.querySelector("dialog.page-overlay[open] iframe").getBoundingClientRect();
        const reply = card.querySelector(".composer textarea, .composer [contenteditable]");
        const replyBox = reply && reply.getBoundingClientRect();
        return {
          beside: document.documentElement.classList.contains("page-beside"),
          card: card.getBoundingClientRect().toJSON(),
          frame: frame.toJSON(),
          replyOnScreen: Boolean(replyBox) && replyBox.bottom <= innerHeight && replyBox.top >= 0,
          shownElsewhere: ["#sidebar", ".rail", "#queue", "#work", "#overview", "#header", ".side"]
            .filter((selector) => [...document.querySelectorAll(selector)].some((el) => visible(el) && el.getBoundingClientRect().width > 0)),
          modal: document.querySelector("dialog.page-overlay[open]").matches(":modal"),
          tipShown: [...document.querySelectorAll("body > div.tip")].some((el) => !el.hidden),
          animations: window.__animations,
        };
      }, first.step);
      check(`${at}: the page opens beside the card`, layout.beside && layout.frame.left >= layout.card.right - 1 && layout.frame.right >= width - 1
        && layout.frame.height >= height - 1, JSON.stringify(layout));
      check(`${at}: the card is the column at the left`, layout.card.left >= 0 && layout.card.right <= layout.frame.left + 1 && layout.card.width >= 300, JSON.stringify(layout.card));
      check(`${at}: nothing but the card shows at the left`, layout.shownElsewhere.length === 0, layout.shownElsewhere.join(", "));
      check(`${at}: the card's reply box is on screen`, layout.replyOnScreen, "off screen");
      check(`${at}: the page is not modal, so the card takes clicks`, !layout.modal, "modal");
      check(`${at}: the clicked link's hover label goes with the move`, !layout.tipShown, "a hover label stays");
      check(`${at}: the page slides in from the right`, layout.animations.some((entry) => entry.on === "dialog" && entry.from === "translateX(100%)"), JSON.stringify(layout.animations));
      check(`${at}: the card slides to the left`, layout.animations.some((entry) => entry.on === "card" && /^translate\(/.test(entry.from || "")), JSON.stringify(layout.animations));
      const own = await layer.evaluate(() => ({ step: document.querySelectorAll(".pages-step").length, reply: (document.getElementById("pages-reply")?.children.length) || 0 }));
      await shot("1-beside");

      // The page has no comment column (the card lists the comments): Pick a part stands where Comment was.
      await layer.locator('.pages-overlay-head [data-head="pick"]:not([disabled])').waitFor();
      const bare = await layer.evaluate(() => ({
        comment: document.querySelectorAll('.pages-overlay-head [data-head="comment"]').length,
        column: [...document.querySelectorAll(".pc-column, .pages-side")].filter((el) => el.getClientRects().length > 0).length,
        frame: document.querySelector(".pages-frame").getBoundingClientRect().width,
        width: innerWidth,
      }));
      check(`${at}: the page has no comment column and keeps its whole width`, bare.comment === 0 && bare.column === 0 && bare.frame >= bare.width - 1, JSON.stringify(bare));

      // Pick a part: the button shows picking, Escape on it stops picking and leaves the page open, and a part
      // picked on the page goes into the card's reply box as a quote, with the cursor in that box.
      const figure = await figureIn(layer, first.report);
      const pickButton = layer.locator('.pages-overlay-head [data-head="pick"]');
      await pickButton.click();
      const pressed = await waitFor(() => pickButton.getAttribute("aria-pressed").catch(() => ""), (value) => value === "true", 50);
      const framePicks = await waitFor(() => figure.evaluate(() => document.documentElement.hasAttribute("data-cockpit-picking")).catch(() => false), Boolean, 50);
      await layer.locator('.pages-overlay-head [data-head="pick"]').focus();
      await page.keyboard.press("Escape");
      await sleep(400);
      const stopped = {
        pressed: await pickButton.getAttribute("aria-pressed").catch(() => ""),
        frame: await figure.evaluate(() => document.documentElement.hasAttribute("data-cockpit-picking")).catch(() => null),
        open: await layerShown.count(),
      };
      check(`${at}: Pick a part shows picking, and the page picks`, pressed === "true" && framePicks, JSON.stringify({ pressed, framePicks }));
      check(`${at}: Escape on Pick a part stops picking and keeps the page open`, stopped.pressed === "false" && stopped.frame === false && stopped.open === 1, JSON.stringify(stopped));
      await pickButton.click();
      await waitFor(() => figure.evaluate(() => document.documentElement.hasAttribute("data-cockpit-picking")).catch(() => false), Boolean, 50);
      await figure.locator("p").first().click();
      card = page.locator(`#next [data-step="${first.step}"]`).first();
      const reply = card.locator(".composer textarea").first();
      // The reply box in focus: the textarea itself, or the editor drawn in its place.
      const replyFocused = () => page.evaluate((stepId) => {
        const active = document.activeElement;
        const box = document.querySelector(`#next [data-step="${stepId}"] .composer`);
        return Boolean(active && box && box.contains(active) && active.matches(`textarea, [contenteditable]`));
      }, first.step);
      const onPart = `> On a part of the page “Exports report” (page ${first.report}):\n> The grocery list export includes open tasks and their reminder dates.`;
      const quotedPart = await waitFor(() => reply.inputValue().catch(() => ""), (text) => text.includes(onPart), 60);
      const partState = {
        quoted: quotedPart, pressed: await pickButton.getAttribute("aria-pressed"), focused: await replyFocused(),
        box: await layer.locator(".pc-draft").count(),
      };
      check(`${at}: a picked part goes into the card's reply box as a quote, in focus`, quotedPart.startsWith(onPart) && partState.pressed === "false"
        && partState.focused && partState.box === 0, JSON.stringify(partState));

      // The page's own script cannot put words in the reply box: its P starts no pick, so a part it reports counts for nothing.
      await figure.evaluate(() => {
        parent.postMessage({ cockpitPage: { type: "key", key: "p" } }, "*");
        parent.postMessage({ cockpitPage: { type: "part", anchor: { kind: "part", path: "body > h1", text: "Words the page chose" } } }, "*");
      });
      await sleep(800);
      const forged = { reply: await reply.inputValue(), pressed: await pickButton.getAttribute("aria-pressed") };
      check(`${at}: the page's own script starts no pick and adds nothing to the reply box`, !forged.reply.includes("Words the page chose") && forged.pressed === "false", JSON.stringify(forged));

      // Words selected on the page and Comment: their quote follows in the reply box, the cursor under it, and the
      // user types the comment there. The page shows no box of its own.
      await figure.evaluate(() => {
        const range = document.createRange();
        range.selectNodeContents(document.querySelector("h1").firstChild);
        getSelection().removeAllRanges();
        getSelection().addRange(range);
      });
      await layer.locator(".pc-float:not([hidden])").click();
      const onWords = `> On the page “Exports report” (page ${first.report}):\n> Exports in January\n\n`;
      await waitFor(() => reply.inputValue().catch(() => ""), (text) => text.endsWith(onWords), 60);
      const wordsFocused = await replyFocused();
      await page.keyboard.type("Say which month it compares with");
      const written = await reply.inputValue();
      const shownOnPage = await layer.evaluate(() => [...document.querySelectorAll(".pc-column, .pc-draft")].filter((el) => el.getClientRects().length > 0).length);
      check(`${at}: selected words go into the card's reply box after the part, and the comment is typed under them`,
        wordsFocused && written === `${onPart}\n\n${onWords}Say which month it compares with` && shownOnPage === 0,
        JSON.stringify({ wordsFocused, written, shownOnPage }));
      await shot("2a-writing");
      check(`${at}: the page stays beside the card`, (await layerShown.count()) === 1, "closed");
      const ownNow = await layer.evaluate(() => ({ step: document.querySelectorAll(".pages-step").length, reply: (document.getElementById("pages-reply")?.textContent || "").trim() }));
      check(`${at}: the page leaves out its own reply box and the step's ask`, own.step === 0 && ownNow.step === 0 && ownNow.reply === "", JSON.stringify({ own, ownNow }));
      // The pointer resting on the page shows no hover label over it.
      await page.mouse.move(900, 500);
      await page.mouse.move(960, 600, { steps: 4 });
      await sleep(1200);
      const tipOver = await page.evaluate(() => [...document.querySelectorAll(".tip")].some((tip) => !tip.hidden));
      check(`${at}: the pointer on the page shows no hover label`, !tipOver, "a hover label shows over the page");
      await shot("2-comment");

      // Comments left on the page from the Pages tab show open above the card's reply box; one deleted there leaves the
      // page's marks.
      // The page finds a comment's words by the words just after them too.
      const anchorOn = (line) => ({ kind: "text", quote: `Line ${line} of the report.`, prefix: "", suffix: `Line ${line + 1} of the` });
      for (const [line, text] of [[1, "Keep this one"], [2, "Drop this one"]]) {
        await user("POST", `/api/item-pages/${first.report}/comments`, { revision: first.revision, step_id: first.step, anchor: anchorOn(line), text });
      }
      await page.evaluate(() => {
        dispatchEvent(new CustomEvent("cockpit:page-changed"));
        dispatchEvent(new CustomEvent("cockpit:page-comments-changed"));
      });
      const pending = () => page.locator(`#next [data-step="${first.step}"] details.page-comments-pending`).first();
      const listed = await waitFor(() => pending().evaluate((el) => ({ open: el.open, text: el.textContent })).catch(() => null),
        (value) => value && value.text.includes("Drop this one"), 120);
      check(`${at}: comments from the Pages tab show open above the card's reply box`, Boolean(listed && listed.open && listed.text.includes("2 page comments")), JSON.stringify(listed));
      const twoMarks = await waitFor(() => figure.evaluate(() => CSS.highlights.get("cockpit-open")?.size ?? 0).catch(() => -1), (count) => count === 2, 120);
      card = page.locator(`#next [data-step="${first.step}"]`).first();
      await card.locator("details.page-comments-pending li", { hasText: "Drop this one" }).locator("button").click();
      const marked = await waitFor(() => figure.evaluate(() => CSS.highlights.get("cockpit-open")?.size ?? 0).catch(() => -1), (count) => count === 1, 120);
      check(`${at}: a comment deleted on the card leaves the page's marks`, twoMarks === 2 && marked === 1, `${twoMarks} then ${marked} marks`);
      check(`${at}: the reply box keeps what was written`, (await card.locator(".composer textarea").first().inputValue()).endsWith("Say which month it compares with"), "changed");

      // Another page from the card replaces the one beside it.
      await card.locator(`a[href*="/pages?id=${first.other}&step=${first.step}"]:visible`).first().click();
      // The shown frame, not the one loading hidden in its place, is the other page's, and no swap is under way.
      const shownOther = `dialog.page-overlay[open]:not(.page-overlay-loading-page, .page-overlay-swapping) iframe.page-overlay-frame:not(.page-overlay-next)[src*="id=${first.other}&"]`;
      await waitFor(() => page.locator(shownOther).count(), (count) => count === 1, 120);
      const swapped = await page.evaluate((shown) => ({
        beside: document.documentElement.classList.contains("page-beside"), shown: document.querySelectorAll(shown).length,
        layers: document.querySelectorAll("dialog.page-overlay:not(.page-overlay-preload)").length,
        frames: document.querySelectorAll("dialog.page-overlay[open] iframe").length,
      }), shownOther);
      check(`${at}: another page from the card takes the place of the one beside it`, swapped.beside && swapped.shown === 1 && swapped.layers === 1 && swapped.frames === 1, JSON.stringify(swapped));

      // Send on the card answers with the comment and closes the page.
      await page.evaluate(() => { window.__animations.length = 0; });
      await card.locator(".composer button.send").click();
      await waitFor(() => page.locator("dialog.page-overlay:not(.page-overlay-preload)").count(), (count) => count === 0, 60);
      const after = await page.evaluate(() => ({
        beside: document.documentElement.classList.contains("page-beside"), layers: document.querySelectorAll("dialog.page-overlay:not(.page-overlay-preload)").length,
        marked: document.querySelectorAll(".beside-path, .beside-card").length, animations: window.__animations,
      }));
      check(`${at}: Send on the card closes the page`, !after.beside && after.layers === 0 && after.marked === 0, JSON.stringify(after));
      check(`${at}: the page slides out`, after.animations.some((entry) => entry.on === "dialog" && entry.to === "translateX(100%)"), JSON.stringify(after.animations));
      const sent = await waitFor(async () => (await user("GET", `/api/item-pages/${first.report}/comments`)).comments, (comments) => comments.every((comment) => comment.state !== "unsent"), 80);
      check(`${at}: the answer took the comment`, sent.length === 1 && sent[0].state !== "unsent", JSON.stringify(sent.map((comment) => comment.state)));
      const handed = (await user("GET", "/api/state")).with_agents.find((step) => step.id === first.step);
      check(`${at}: the answer took the quotes and the comment under them`, Boolean(handed && handed.handoff.text.includes(`On the page “Exports report” (page ${first.report}):`)
        && handed.handoff.text.includes("Say which month it compares with")), JSON.stringify(handed && handed.handoff.text));
      await shot("3-sent");

      // The X: the page slides out, and Cockpit's page is as it was.
      card = await goTo(second);
      await sleep(400);
      const secondLink = card.locator(`a[href*="/pages?id=${second.report}&step=${second.step}"]:visible`).first();
      // A poll may draw the card anew mid-scroll; the locator finds the new one on the next try.
      for (let tries = 0; ; tries += 1) {
        try {
          await secondLink.scrollIntoViewIfNeeded({ timeout: 5000 });
          break;
        } catch (error) {
          if (tries >= 4) throw error;
          await sleep(300);
        }
      }
      const scrolledBefore = await page.evaluate(() => scrollY);
      await secondLink.click();
      await layerShown.waitFor();
      await openLayer(page);
      await sleep(400);
      await page.evaluate(() => { window.__animations.length = 0; });
      await page.locator("dialog.page-overlay[open] .page-overlay-close").click();
      await waitFor(() => page.locator("dialog.page-overlay:not(.page-overlay-preload)").count(), (count) => count === 0, 60);
      const closed = await page.evaluate((stepId) => ({
        beside: document.documentElement.classList.contains("page-beside"),
        card: Boolean(document.querySelector(`#next [data-step="${stepId}"]`)),
        rail: [...document.querySelectorAll("#sidebar, #queue")].some((el) => el.getClientRects().length > 0),
        animations: window.__animations, scrolled: scrollY,
      }), second.step);
      check(`${at}: the X closes the page and lays Cockpit's page out as before`, !closed.beside && closed.card && closed.rail, JSON.stringify(closed));
      check(`${at}: the card slides back`, closed.animations.some((entry) => /^translate\(/.test(entry.from || "") && entry.on !== "dialog"), JSON.stringify(closed.animations));
      check(`${at}: the page is scrolled as before`, Math.abs(closed.scrolled - scrolledBefore) <= 1, `${closed.scrolled} vs ${scrolledBefore}`);

      // Escape on the card closes it.
      await card.locator(`a[href*="/pages?id=${second.report}&step=${second.step}"]:visible`).first().click();
      await layerShown.waitFor();
      await openLayer(page);
      await card.locator("h2, .step-ask, .ask").first().click({ position: { x: 4, y: 4 } }).catch(() => page.mouse.click(40, 300));
      await page.keyboard.press("Escape");
      await waitFor(() => page.locator("dialog.page-overlay:not(.page-overlay-preload)").count(), (count) => count === 0, 60);
      check(`${at}: Escape on the card closes the page`, (await page.locator("dialog.page-overlay:not(.page-overlay-preload)").count()) === 0, "still open");

      if (label === "wide") {
        await clearQueue();
        await swapsAndRedraws({ page, at, makeStep, goTo, layerShown, check });
        await askLinkAndPick({ page, at, agent, run, goTo, layerShown, check, clearQueue, shot });
      }

      check(`${at}: no page or policy errors`, errors.length === 0, errors.join(" | "));
      await page.context().close();
    }

    // Narrower windows: the page slides over the whole window, from the right at 1000 px, from the bottom on a phone.
    for (const [label, width, height, scheme, from] of [["narrow", 1000, 800, "light", "translateX(100%)"], ["phone", 390, 844, "dark", "translateY(100%)"]]) {
      const at = `${label} (${width} px ${scheme}, /classic)`;
      await clearQueue();
      const { page, errors, shot } = await newPage(browser, info, { width, height }, scheme, label);
      const ids = await makeStep(label);
      await page.goto(`${info.origin}/classic#home/item/${ids.item}`);
      const card = page.locator(`#next [data-step="${ids.step}"]`).first();
      await card.waitFor().catch(async (error) => {
        await page.screenshot({ path: path.join(SHOTS, `${label}-missing-${ids.step}.png`) });
        const state = await user("GET", "/api/state");
        const where = ["queue", "ready_next", "with_agents"].map((name) => `${name}: ${(state[name] || []).map((step) => step.id).join(",")}`).join("; ");
        throw new Error(`step ${ids.step} not shown (${where}): ${error.message}`);
      });
      if (await card.evaluate((el) => el.classList.contains("step-row") && !el.classList.contains("open"))) await card.locator(".row-head").click();
      await page.evaluate(() => { window.__animations.length = 0; });
      await card.locator(`a[href*="/pages?id=${ids.report}&step=${ids.step}"]:visible`).first().click();
      await page.locator("dialog.page-overlay[open]:not(.page-overlay-loading-page)").waitFor();
      await openLayer(page);
      await sleep(400);
      const shown = await page.evaluate(() => ({
        beside: document.documentElement.classList.contains("page-beside"),
        frame: document.querySelector("dialog.page-overlay[open] iframe").getBoundingClientRect().toJSON(),
        animations: window.__animations,
      }));
      check(`${at}: the page covers the whole window`, !shown.beside && shown.frame.left <= 1 && shown.frame.width >= width - 1, JSON.stringify(shown.frame));
      check(`${at}: the page slides in (${from})`, shown.animations.some((entry) => entry.on === "dialog" && entry.from === from), JSON.stringify(shown.animations));
      await shot("1-open");
      await page.locator("dialog.page-overlay[open] .page-overlay-close").click();
      await waitFor(() => page.locator("dialog.page-overlay:not(.page-overlay-preload)").count(), (count) => count === 0, 60);
      check(`${at}: the X closes it`, (await page.locator("dialog.page-overlay:not(.page-overlay-preload)").count()) === 0, "still open");
      check(`${at}: no page or policy errors`, errors.length === 0, errors.join(" | "));
      await page.context().close();
    }
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failed = results.filter((result) => !result.ok).length;
  console.error(`${results.length - failed}/${results.length} checks passed; screenshots in ${SHOTS}`);
  if (failed) process.exitCode = 1;
}

// Pages swapped beside one card, Back while a swap loads, three quick clicks, and a redraw of the card while a page
// shows beside it.
async function swapsAndRedraws({ page, at, makeStep, goTo, layerShown, check }) {
  const ids = await makeStep("swaps", true);
  const card = await goTo(ids);
  const linkTo = (id) => page.locator(`#next [data-step="${ids.step}"] a[href*="/pages?id=${id}&step=${ids.step}"]:visible`).first();
  const shownId = () => page.evaluate(() => {
    const frame = document.querySelector("dialog.page-overlay[open]:not(.page-overlay-loading-page) iframe.page-overlay-frame:not(.page-overlay-next)");
    return frame ? Number(new URL(frame.src).searchParams.get("id")) : null;
  });
  const state = () => page.evaluate(() => ({
    layers: document.querySelectorAll("dialog.page-overlay:not(.page-overlay-preload)").length,
    beside: document.documentElement.classList.contains("page-beside"),
    marked: document.querySelectorAll(".beside-path, .beside-card").length,
    open: document.documentElement.classList.contains("page-overlay-open"),
  }));
  const allClosed = (value) => value.layers === 0 && !value.beside && value.marked === 0 && !value.open;

  // A quote and a comment written in the card's reply box stay there through a swap to another page, also what was
  // typed while the other page loaded.
  await linkTo(ids.report).click();
  await layerShown.waitFor();
  let layer = await openLayer(page);
  await layer.locator('.pages-overlay-head [data-head="pick"]:not([disabled])').waitFor();
  const figure = await figureIn(layer, ids.report);
  await figure.evaluate(() => {
    const range = document.createRange();
    range.selectNodeContents(document.querySelector("h1").firstChild);
    getSelection().removeAllRanges();
    getSelection().addRange(range);
  });
  await layer.locator(".pc-float:not([hidden])").click();
  const reply = () => page.locator(`#next [data-step="${ids.step}"] .composer textarea`).first();
  await waitFor(() => reply().inputValue().catch(() => ""), (text) => text.includes("> Exports in January"), 60);
  await page.keyboard.type("Half a thought");
  let releaseOther;
  const heldOther = new Promise((resolve) => { releaseOther = resolve; });
  const holdOther = async (route) => { await heldOther; await route.continue().catch(() => {}); };
  await page.route(`**/pages/${ids.other}/figure*`, holdOther);
  await linkTo(ids.other).click();
  await page.locator("iframe.page-overlay-next").waitFor({ state: "attached" });
  await sleep(600);
  await reply().focus();
  await page.keyboard.press("End");
  await page.keyboard.type(", finished");
  releaseOther();
  await page.unroute(`**/pages/${ids.other}/figure*`, holdOther);
  await waitFor(shownId, (id) => id === ids.other, 150);
  await sleep(400);
  const carried = await reply().inputValue();
  check(`${at}: the quote and comment in the reply box stay through a swap to another page, with what was typed while it loaded`,
    carried.includes("> Exports in January") && carried.endsWith("Half a thought, finished"), JSON.stringify(carried));
  await reply().fill("");
  await page.locator("dialog.page-overlay[open] .page-overlay-close").click();
  await waitFor(state, allClosed, 60);
  const depth = await page.evaluate(() => history.length);
  await linkTo(ids.report).click();
  await layerShown.waitFor();
  layer = await openLayer(page);

  // Back while another page loads in its place closes the whole layer.
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  const hold = async (route) => { await held; await route.continue().catch(() => {}); };
  await page.route(`**/pages/${ids.other}/figure*`, hold);
  await linkTo(ids.other).click();
  await page.locator("iframe.page-overlay-next").waitFor({ state: "attached" });
  await page.goBack();
  const afterBack = await waitFor(state, allClosed, 60);
  const depthBack = await page.evaluate(() => history.length);
  check(`${at}: Back while a swap loads closes the page and lays Cockpit's page out as before`, allClosed(afterBack), JSON.stringify(afterBack));
  release();
  await page.unroute(`**/pages/${ids.other}/figure*`, hold);
  check(`${at}: and leaves no history entry behind`, depthBack <= depth + 1 && (await page.evaluate(() => location.hash === "" || location.pathname === "/")), `${depthBack} vs ${depth}`);

  // Three quick clicks: the last page shows, and the X closes it all.
  await linkTo(ids.report).click();
  await layerShown.waitFor();
  await openLayer(page);
  let release2;
  const held2 = new Promise((resolve) => { release2 = resolve; });
  const hold2 = async (route) => { await held2; await route.continue().catch(() => {}); };
  await page.route(`**/pages/${ids.other}/figure*`, hold2);
  await linkTo(ids.other).click();
  await page.locator("iframe.page-overlay-next").waitFor({ state: "attached" });
  await linkTo(ids.third).click();
  await waitFor(shownId, (id) => id === ids.third, 150);
  release2();
  await page.unroute(`**/pages/${ids.other}/figure*`, hold2);
  await sleep(500);
  const frames = await page.evaluate(() => [...document.querySelectorAll("dialog.page-overlay iframe")].map((frame) => frame.src));
  check(`${at}: three quick clicks show the last page alone`, (await shownId()) === ids.third && frames.length === 1, JSON.stringify(frames));
  await page.locator("dialog.page-overlay[open] .page-overlay-close").click();
  const afterThree = await waitFor(state, allClosed, 60);
  check(`${at}: the X then closes it all`, allClosed(afterThree), JSON.stringify(afterThree));

  // A redraw of the card keeps where the column was scrolled, and the X gives the keyboard back to the link.
  await linkTo(ids.report).click();
  await layerShown.waitFor();
  await openLayer(page);
  await sleep(500);
  const room = await page.evaluate(() => {
    const column = document.querySelector("main.layout");
    column.scrollTop = 300;
    return { top: column.scrollTop, height: column.scrollHeight, view: column.clientHeight };
  });
  await sleep(200);
  await page.evaluate(() => dispatchEvent(new CustomEvent("cockpit:page-changed")));
  await page.waitForResponse((response) => response.url().includes("/api/state")).catch(() => {});
  await sleep(800);
  const kept = await page.evaluate(() => document.querySelector("main.layout").scrollTop);
  check(`${at}: a redraw of the card keeps the column's scroll`, room.top > 0 && Math.abs(kept - room.top) <= 1, JSON.stringify({ room, kept }));
  await page.locator("dialog.page-overlay[open] .page-overlay-close").click();
  await waitFor(state, allClosed, 60);
  const focused = await page.evaluate(() => document.activeElement && document.activeElement.getAttribute("href"));
  check(`${at}: closing gives the keyboard back to the link after a redraw`, Boolean(focused && focused.includes(`id=${ids.report}`)), JSON.stringify(focused));
  void card;
}

// A page linked from the step's ask opens beside the card like one in its body; and while the user picks
// a part of Cockpit's page for a to-do, a click on that link picks the link and opens no page.
async function askLinkAndPick({ page, at, agent, run, goTo, layerShown, check, clearQueue, shot }) {
  await clearQueue();
  const key = `note:beside-${run}-ask`;
  const item = await agent("PUT", "/api/items", { key, title: "Beside test ask", priority: "urgent", sessions: [SESSION_B] });
  const report = await agent("POST", `/api/items/${item.id}/pages`, { title: "Exports report", html: REPORT("Exports in January") });
  const answer = await agent("POST", "/api/steps", {
    item_key: key, need: "beside-ask", ask: `Ship the exports report? ([plan](${report.url}))`, score: 100, session_id: SESSION_B,
    body: `The [exports report](${report.url}) is ready.`,
  });
  const ids = { item: item.id, step: answer.step.id };
  let card = await goTo(ids);
  const askLink = () => card.locator(`.ask a[href*="/pages?id=${report.id}&step=${ids.step}"]`).first();
  const bodyLink = () => card.locator(`a[href*="/pages?id=${report.id}&step=${ids.step}"]:visible`).last();
  const href = await askLink().getAttribute("href", { timeout: 5000 }).catch(() => null);
  check(`${at}: a page link in the ask opens the page for the step`, Boolean(href), "the ask's link has no step");
  if (href) await askLink().click();
  else await card.locator(`.ask a[href*="/pages/${report.id}"]`).first().click();
  await layerShown.waitFor();
  await openLayer(page);
  const beside = await page.evaluate(() => document.documentElement.classList.contains("page-beside"));
  check(`${at}: a page link in the ask opens beside the card`, beside, "opened over the whole window");
  await shot("6-ask-link-beside");
  await page.locator("dialog.page-overlay[open] .page-overlay-close").click();
  await waitFor(() => page.locator("dialog.page-overlay:not(.page-overlay-preload)").count(), (count) => count === 0, 60);

  card = await goTo(ids);
  await page.locator("#feedback-mode").click();
  await waitFor(() => page.evaluate(() => Boolean(ui.picking)), Boolean, 50);
  await page.evaluate((id) => { ui.holdAsks.delete(id); ui.held.delete(id); }, ids.step);
  await bodyLink().click();
  await sleep(600);
  const picked = await page.evaluate((id) => ({
    held: ui.holdAsks.has(id),
    layers: document.querySelectorAll("dialog.page-overlay:not(.page-overlay-preload)").length,
    part: document.querySelector("#add-box .add-part")?.innerText || "",
    target: ui.addPart ? ui.addPart.target : "",
  }), ids.step);
  check(`${at}: picking a page link picks the link and opens no page`,
    picked.layers === 0 && picked.part.includes("Attached") && !picked.target.includes("page-overlay"), JSON.stringify(picked));
  check(`${at}: a pick on a step's card does not hold its place in the queue`, !picked.held, JSON.stringify(picked));
  await shot("7-link-picked");
  await page.keyboard.press("Escape");
  await page.evaluate(() => { if (ui.adding) closeAddBox(); });
}

async function newPage(browser, info, viewport, scheme, label) {
  const touch = viewport.width <= 900;
  const context = await browser.newContext({ viewport, colorScheme: scheme, hasTouch: touch, isMobile: touch });
  await context.addCookies([{ name: info.name, value: info.value, domain: new URL(info.origin).hostname, path: "/", secure: true, httpOnly: true, sameSite: "Lax" }]);
  await context.addInitScript(RECORD_ANIMATIONS);
  const page = await context.newPage();
  await page.addLocatorHandler(page.locator("dialog#idle[open]"), (dialog) => dialog.locator("button", { hasText: "Return" }).click()
    .catch((error) => { if (!/has been closed/.test(error.message)) throw error; }));
  page.setDefaultTimeout(20000);
  page.setDefaultNavigationTimeout(60000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error" && /Content Security Policy/.test(message.text())) errors.push(message.text()); });
  const shot = (name) => page.screenshot({ path: path.join(SHOTS, `${label}-${scheme}-${name}.png`) });
  return { page, errors, shot };
}

async function openLayer(page) {
  const handle = await page.locator("dialog.page-overlay[open]:not(.page-overlay-loading-page) iframe.page-overlay-frame").elementHandle();
  const frame = await handle.contentFrame();
  await frame.waitForLoadState("load");
  return frame;
}

async function figureIn(layer, pageId) {
  const end = Date.now() + 30000;
  while (Date.now() < end) {
    const frame = layer.childFrames().find((each) => each.url().includes(`/pages/${pageId}/figure`));
    if (frame) {
      try {
        await frame.waitForLoadState("load");
        return frame;
      } catch {
        // Replaced meanwhile; look again.
      }
    }
    await sleep(100);
  }
  throw new Error(`page ${pageId} did not load in the layer`);
}

async function waitFor(read, done, tries = 60) {
  let value;
  for (let attempt = 0; attempt < tries; attempt++) {
    value = await read();
    if (done(value)) return value;
    await sleep(100);
  }
  return value;
}

async function call(origin, headers, method, url, body) {
  const response = await fetch(`${origin}${url}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await response.text();
  if (!response.ok) throw new Error(`${method} ${url}: ${response.status} ${text}`);
  return text ? JSON.parse(text) : null;
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
