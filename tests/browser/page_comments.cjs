// Comments on shared pages (SPEC NEXT-38). Opens a page from a step (`/pages?id=N&step=M`) and checks, at desktop
// and phone width, in light and dark:
// - the page shows without comments until Comment is pressed;
// - selected words get a Comment button; the saved comment shows in the column and its words are marked;
// - Pick a part (the P key) and a click on the chart comments on the chart, outlined on the page; a comment read
//   that answers after the save leaves the saved comment shown (regression check); Enter or Space on a focused button picks
//   it (regression check);
// - the reply box keeps its words, caret and selection when another tab writes the same reply and the page redraws
//   (regression check, regression check);
// - the reply box counts the comments; Send hands the step off with the words and the comments, and Take back
//   brings both back;
// - a reload keeps the comments and their marks; a new version of the page keeps the marks whose words stay, and
//   lists an unsent comment whose words left under "Their words are no longer on the page";
// - a page whose own script rebuilds its content keeps its marks;
// - the step card's reply box lists the comments that go with its answer;
// - from the Pages tab, Add as new item makes a to-do of the comments, and Send takes them to the session working on
//   the page's item.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs)
// and drives headless Chromium; at phone width with touch, picking the chart by a tap.
// Prints one line per check, saves screenshots in SHOTS_DIR (default: a temporary directory) and exits 1 when one
// failed.
//
// Usage, from the checkout: node tests/browser/page_comments.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "comments-test", "content-type": "application/json" };
const SHOTS = process.env.SHOTS_DIR || fs.mkdtempSync(path.join(os.tmpdir(), "cockpit-comment-shots-"));

const REPORT = (extra) => `<!doctype html><html><head><style>body{font:16px/1.5 system-ui;margin:24px}td{padding:4px 12px}</style></head><body>
<h1>Exports report</h1>
<p>Exports doubled in March after the list filter changed.</p>
<svg width="320" height="120" role="img" aria-label="Exports by week"><title>Exports by week</title>
<rect x="10" y="60" width="40" height="50" fill="#69c"/><rect x="70" y="30" width="40" height="80" fill="#69c"/><text x="10" y="20">Exports by week</text></svg>
<table><tr><td>February</td><td>10</td></tr><tr><td>March</td><td>20</td></tr></table>
<button id="rerun">Run again</button>
<script>for (const type of ["keydown", "keyup"]) document.getElementById("rerun").addEventListener(type, (event) => {
  if (event.key === "Enter" || event.key === " ") { event.preventDefault(); window.reran = (window.reran || 0) + 1; } });</script>
${extra}</body></html>`;

const { chromium } = require(PLAYWRIGHT);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-comments-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "ignore", "inherit"] });
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
    const initial = await user("GET", "/api/state");
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const run = Date.now();
    const sizes = [["desktop", 1280, 900, "light"], ["phone", 390, 844, "dark"]];
    for (const [label, width, height, scheme] of sizes) {
      const key = `note:comments-${run}-${label}`;
      const item = await agent("PUT", "/api/items", { key, title: `Comments test ${label}`, priority: "urgent", sessions: [SESSION_B] });
      const report = await agent("POST", `/api/items/${item.id}/pages`, { title: "Exports report", html: REPORT("") });
      const answer = await agent("POST", "/api/steps", {
        item_key: key, need: `comments-${label}`, ask: "Ship the exports report?", score: 100, session_id: SESSION_B,
        body: `Read [the report](${report.url}).`,
      });
      const stepId = answer.step.id;
      const touch = width <= 900;
      const context = await browser.newContext({ viewport: { width, height }, colorScheme: scheme, hasTouch: touch, isMobile: touch });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.setDefaultTimeout(8000);
      page.setDefaultNavigationTimeout(60000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => { if (message.type() === "error" && /Content Security Policy/.test(message.text())) errors.push(message.text()); });
      const shot = (name) => page.screenshot({ path: path.join(SHOTS, `${label}-${scheme}-${name}.png`) });
      const at = `${label} (${scheme})`;

      await page.goto(`${info.origin}/pages?id=${report.id}&step=${stepId}`);
      const frame = await frameOf(page, report.id);
      // Reading comes first: the step's ask and the column show once the user presses Comment.
      await sleep(500);
      check(`${at}: the page shows without comments or the step's ask at first`, (await page.locator(".pc-column").count()) === 0
        && (await page.locator(".pages-step-ask").count()) === 0 && (await page.locator("#pages-reply").innerText()) === "",
        "a column, the ask or a reply box shows");
      await page.locator(".pages-preview-buttons button", { hasText: "Comment" }).click();
      await page.locator(".pc-hint").waitFor();
      check(`${at}: the step's ask heads the comments`, (await page.locator(".pages-side > .pages-step .pages-step-ask").textContent()) === "Ship the exports report?", "");

      // Words: select, Comment, write, save.
      await frame.evaluate(() => {
        const node = [...document.querySelectorAll("p")][0].firstChild;
        const at = node.nodeValue.indexOf("doubled in March");
        const range = document.createRange();
        range.setStart(node, at);
        range.setEnd(node, at + "doubled in March".length);
        getSelection().removeAllRanges();
        getSelection().addRange(range);
      });
      await page.locator(".pc-float:not([hidden])").waitFor();
      await shot("1-selected");
      await page.locator(".pc-float").click();
      await page.locator(".pc-draft textarea").fill("Say which month it compares with");
      // The page's own script proposes a part and presses Escape while the user writes: the comment stays.
      await frame.evaluate(() => {
        parent.postMessage({ cockpitPage: { type: "part", anchor: { kind: "part", path: "h1", text: "Exports report" }, box: { top: 0, bottom: 9, left: 0, right: 9 } } }, "*");
        parent.postMessage({ cockpitPage: { type: "key", key: "Escape" } }, "*");
      });
      await sleep(300);
      const writing = await page.locator(".pc-draft textarea").inputValue().catch(() => "");
      check(`${at}: the page's own messages leave the comment being written`, writing === "Say which month it compares with", writing);
      // A poll while the user writes keeps the comment's focus and caret (regression check), and shows the step's ask as
      // the session put it since (regression check).
      await agent("POST", "/api/steps", { item_key: key, need: `comments-${label}`, ask: "Ship the exports report today?" });
      await page.locator(".pc-draft textarea").evaluate((area) => { area.focus(); area.setSelectionRange(4, 9); });
      await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
      const asked = await waitFor(() => page.locator(".pages-step-ask").textContent(), (text) => text === "Ship the exports report today?");
      check(`${at}: a poll shows the step's new ask`, asked === "Ship the exports report today?", asked);
      const caret = await page.evaluate(() => {
        const area = document.querySelector(".pc-draft textarea");
        return { focused: document.activeElement === area, start: area.selectionStart, end: area.selectionEnd };
      });
      check(`${at}: a poll keeps the comment's focus and caret`, caret.focused && caret.start === 4 && caret.end === 9, JSON.stringify(caret));
      await agent("POST", "/api/steps", { item_key: key, need: `comments-${label}`, ask: "Ship the exports report?" });
      await page.locator(".pc-draft .button.primary").click();
      await page.locator(".pc-card.own").first().waitFor();
      check(`${at}: the comment shows on its words`, (await page.locator(".pc-card.own .pc-on").first().textContent()) === "“doubled in March”", "");
      const marked = await waitFor(() => frame.evaluate(() => CSS.highlights.get("cockpit-open")?.size || 0), (size) => size === 1);
      check(`${at}: the words are marked on the page`, marked === 1, `${marked} marks`);

      // A part: P, then a click on the chart.
      await page.locator(".pages-step-ask").click();
      await page.keyboard.press("p");
      await page.locator('.pc-head .button[aria-pressed="true"]').waitFor();
      // On a phone the ask sits under the page, so the chart comes back into view first.
      await frame.locator("svg").scrollIntoViewIfNeeded();
      const box = await page.locator(".pages-frame").boundingBox();
      const chart = await frame.evaluate(() => { const r = document.querySelector("svg").getBoundingClientRect(); return { x: r.left + 30, y: r.top + 80 }; });
      if (touch) await page.touchscreen.tap(box.x + chart.x, box.y + chart.y);
      else await page.mouse.click(box.x + chart.x, box.y + chart.y);
      await page.locator(".pc-draft textarea").fill("Start the axis at zero");
      // A read of the comments that starts before this save and answers after it leaves the saved comment shown
      // (regression check): the first read from here on answers with the list from before, 1.5 s late.
      let late = false;
      await page.route("**/api/item-pages/*/comments", async (route) => {
        if (late || route.request().method() !== "GET") return route.fallback();
        late = true;
        const response = await route.fetch();
        await sleep(1500);
        await route.fulfill({ response });
      });
      await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
      await waitFor(async () => late, (on) => on);
      await page.keyboard.press("Enter");
      await page.waitForFunction(() => document.querySelectorAll(".pc-card.own").length === 2);
      await sleep(2500);
      const ownAfter = await page.locator(".pc-card.own").count();
      check(`${at}: a comment read started before the save leaves the saved comment shown`, ownAfter === 2, `${ownAfter} own comments`);
      await page.unroute("**/api/item-pages/*/comments");
      const outlined = await waitFor(() => frame.evaluate(() => document.documentElement.lastElementChild.children.length), (count) => count === 1);
      check(`${at}: the chart has an outline`, outlined === 1, `${outlined} outlines`);
      const order = await page.locator(".pc-card.own .pc-on").allTextContents();
      check(`${at}: the column follows the page's order`, order[0] === "“doubled in March”" && order[1].startsWith("Part:"), JSON.stringify(order));
      const pending = await page.locator(".pages-pending").textContent();
      check(`${at}: the reply box counts the comments`, pending.startsWith("2 comments with this answer"), pending);
      // A focused button whose own Enter and Space handlers act: either key picks it while picking, and its handlers
      // never see the key (regression check).
      if (!touch) {
        for (const key of ["Enter", " "]) {
          await page.locator(".pages-step-ask").click();
          await page.keyboard.press("p");
          await page.locator('.pc-head .button[aria-pressed="true"]').waitFor();
          await frame.locator("#rerun").focus();
          await page.keyboard.press(key);
          const draft = await page.locator(".pc-draft .pc-on").textContent().catch(() => "");
          const reran = await frame.evaluate(() => window.reran || 0);
          check(`${at}: ${key === " " ? "Space" : key} on a focused button picks it, and the page's handler is left out`,
            draft === "Part: “Run again”" && reran === 0, `${draft}, the handler ran ${reran} times`);
          await page.locator(".pc-draft-buttons .button", { hasText: "Cancel" }).click();
        }
      }
      await shot("2-two-comments");

      // The step card lists them above its reply box.
      const home = await context.newPage();
      await home.goto(`${info.origin}/classic`);
      const card = home.locator(`[data-step="${stepId}"]`).first();
      await card.waitFor();
      // A step that is not the top one shows as a row; opening it shows its card.
      if (await card.evaluate((el) => el.classList.contains("step-row") && !el.classList.contains("open"))) await card.locator(".row-head").click();
      const listed = await card.locator(".page-comments-pending summary").textContent().catch(() => "");
      check(`${at}: the step card lists the comments`, listed === "2 page comments go with this answer", listed);
      const link = await card.locator(".core-links a[href*='/pages?id=']").first().getAttribute("href").catch(() => "");
      check(`${at}: the step card's page link opens the page for the step`, link === `${info.origin}/pages?id=${report.id}&step=${stepId}`, link);
      await home.close();

      // With the browser's storage blocked, the reply's words stay through a redraw (regression check).
      await page.evaluate(() => {
        window.storageMethods = { get: Storage.prototype.getItem, set: Storage.prototype.setItem };
        Storage.prototype.getItem = () => { throw new Error("storage blocked"); };
        Storage.prototype.setItem = () => { throw new Error("storage blocked"); };
      });
      await page.locator("#pages-reply-text").fill("Yes, after these fixes");
      await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
      await sleep(500);
      const replyKept = await page.locator("#pages-reply-text").inputValue();
      check(`${at}: without storage the reply keeps its words through a redraw`, replyKept === "Yes, after these fixes", replyKept);
      await page.evaluate(() => { Storage.prototype.getItem = window.storageMethods.get; Storage.prototype.setItem = window.storageMethods.set; });

      // Another tab writing the same reply leaves this tab's words as they are, and a redraw keeps the caret where
      // the user put it (regression check, regression check).
      // Long words, scrolled down in a short box, keep their scroll through the redraw too.
      await page.locator("#pages-reply-text").fill(`Yes, after these fixes${"\nand one line more".repeat(40)}`);
      const typed = await page.evaluate(() => {
        const area = document.getElementById("pages-reply-text");
        area.focus();
        area.setSelectionRange(4, 9);
        area.scrollTop = 100;
        const key = Object.keys(localStorage).find((name) => name.startsWith("cockpit.pages.reply:"));
        localStorage.setItem(key, "Written in another tab");
        document.dispatchEvent(new Event("visibilitychange"));
        return key;
      });
      await sleep(500);
      const afterOther = await page.evaluate(() => {
        const area = document.getElementById("pages-reply-text");
        return { text: area.value, start: area.selectionStart, end: area.selectionEnd, focused: document.activeElement === area, scroll: area.scrollTop };
      });
      check(`${at}: another tab's words leave this tab's reply as typed, with its selection (${typed})`,
        afterOther.text.startsWith("Yes, after these fixes\nand one line more") && afterOther.start === 4 && afterOther.end === 9 && afterOther.focused,
        JSON.stringify({ ...afterOther, text: afterOther.text.slice(0, 40) }));
      check(`${at}: the redraw keeps the reply box's scroll`, afterOther.scroll === 100, `scrollTop ${afterOther.scroll}`);

      // Send, then Take back.
      await page.locator("#pages-reply-text").fill("Yes, after these fixes");
      await page.locator(".pages-reply-buttons .button.primary").click();
      await page.locator(".pages-reply-sent").waitFor();
      await shot("3-sent");
      const sentState = await user("GET", `/api/item-pages/${report.id}/comments`);
      check(`${at}: Send marks both comments sent`, sentState.comments.every((comment) => comment.state === "sent"), JSON.stringify(sentState.comments.map((c) => c.state)));
      const greyed = await waitFor(() => frame.evaluate(() => CSS.highlights.get("cockpit-sent")?.size || 0), (size) => size === 1);
      check(`${at}: the sent words turn grey`, greyed === 1, `${greyed}`);
      // Take back after hiding the comments shows them again, with the answer's words.
      await page.locator(".pages-preview-buttons button", { hasText: "Hide comments" }).click();
      await page.locator(".pages-reply-sent .button").click();
      await page.locator("#pages-reply-text").waitFor();
      check(`${at}: Take back shows the comments again`, (await page.locator(".pc-column").count()) === 1, "no column");
      // Until the reload shows the step taking answers again, the box is the page's own message to the item's session.
      const words = await waitFor(() => page.locator("#pages-reply-text").inputValue(), (text) => text === "Yes, after these fixes");
      check(`${at}: Take back puts the words back`, words === "Yes, after these fixes", words);
      const back = await user("GET", `/api/item-pages/${report.id}/comments`);
      check(`${at}: Take back makes the comments unsent`, back.comments.every((comment) => comment.state === "unsent"), JSON.stringify(back.comments.map((c) => c.state)));

      // Reload: the comments and marks stay.
      await page.reload();
      const again = await frameOf(page, report.id);
      // The step's comments on the page open the column by themselves.
      await page.waitForFunction(() => document.querySelectorAll(".pc-card.own").length === 2);
      const kept = await waitFor(() => again.evaluate(() => CSS.highlights.get("cockpit-open")?.size || 0), (size) => size === 1);
      check(`${at}: a reload keeps comments and marks`, kept === 1, `${kept}`);

      // A new version: the words of the first comment leave, the chart stays; and the page rebuilds itself.
      const rebuilt = REPORT(`<script>setTimeout(() => { document.body.innerHTML = document.body.innerHTML; }, 300)</script>`)
        .replace("Exports doubled in March", "Exports grew a lot in March");
      await agent("PUT", `/api/item-pages/${report.id}`, { title: "Exports report", html: rebuilt });
      await page.reload();
      const third = await frameOf(page, report.id);
      await page.locator(".pc-lost .pc-card").waitFor();
      check(`${at}: a comment whose words left is listed apart`, (await page.locator(".pc-lost h3").textContent()) === "Their words are no longer on the page", "");
      await sleep(900);
      const partKept = await third.evaluate(() => document.documentElement.lastElementChild.children.length);
      check(`${at}: the chart keeps its outline after the page rebuilt itself`, partKept === 1, `${partKept}`);
      await shot("4-new-version");

      // Add as new item from the Pages tab.
      const tab = await context.newPage();
      await tab.goto(`${info.origin}/pages?id=${report.id}`);
      if (width > 900) {
        const loose = await frameOf(tab, report.id);
        await sleep(500);
        check(`${at}: the Pages tab shows the page without comments at first`, (await tab.locator(".pc-column").count()) === 0, "a column shows");
        await tab.locator(".pages-preview-buttons button", { hasText: "Comment" }).click();
        await tab.locator(".pc-head").waitFor();
        await loose.evaluate(() => {
          const cell = [...document.querySelectorAll("td")].find((td) => td.textContent === "February").firstChild;
          const range = document.createRange();
          range.selectNodeContents(cell);
          getSelection().removeAllRanges();
          getSelection().addRange(range);
        });
        await tab.locator(".pc-float:not([hidden])").click();
        await tab.locator(".pc-draft textarea").fill("Check February against last year");
        // Hide comments keeps the comment being written for when they show again.
        await tab.locator(".pages-preview-buttons button", { hasText: "Hide comments" }).click();
        await tab.locator(".pages-preview-buttons button", { hasText: "Comment" }).click();
        await tab.locator(".pc-draft textarea").waitFor();
        check(`${at}: Hide comments keeps the comment being written`, (await tab.locator(".pc-draft textarea").inputValue()) === "Check February against last year",
          await tab.locator(".pc-draft textarea").inputValue());
        await tab.locator(".pc-draft .button.primary").click();
        await tab.locator(".pages-reply-buttons .button:not([disabled])").first().waitFor();
        const offered = await tab.locator(".pages-reply-buttons .button").allTextContents();
        check(`${at}: the Pages tab offers Add as new item and Send to the item's session`,
          offered.join("|") === "Add as new item|Send" && (await tab.locator("#pages-reply-text").count()) === 1, offered.join("|"));
        await tab.locator(".pages-reply-buttons .button", { hasText: "Add as new item" }).click();
        await tab.locator("#banner .banner").waitFor();
        const added = await tab.locator("#banner .banner").textContent();
        check(`${at}: Add as new item makes a to-do`, /^Added as item \d+/.test(added), added);
        await tab.screenshot({ path: path.join(SHOTS, `${label}-${scheme}-5-added.png`) });
        // The step card's "Comment on this page" link opens the column at once.
        await tab.goto(`${info.origin}/pages?id=${report.id}&comment=1`);
        await tab.locator(".pc-head").waitFor();
        check(`${at}: &comment=1 opens the column`, (await tab.locator(".pc-column").count()) === 1, "");

        // Without a step that takes them, Send takes the comments and the words with them to the item's session.
        const sending = await frameOf(tab, report.id);
        await sleep(900);
        await sending.evaluate(() => {
          const cell = [...document.querySelectorAll("td")].find((td) => td.textContent === "March").firstChild;
          const range = document.createRange();
          range.selectNodeContents(cell);
          getSelection().removeAllRanges();
          getSelection().addRange(range);
        });
        await tab.locator(".pc-float:not([hidden])").click();
        await tab.locator(".pc-draft textarea").fill("Is March a full month?");
        await tab.locator(".pc-draft .button.primary").click();
        await tab.locator(".pc-draft").waitFor({ state: "detached" });
        await tab.locator("#pages-reply-text").fill("From the Pages tab");
        await tab.screenshot({ path: path.join(SHOTS, `${label}-${scheme}-6-send-to-item.png`) });
        await tab.locator(".pages-reply-buttons .button.primary:not([disabled])").click();
        await tab.waitForFunction(() => /^Sent to the session working on /.test(document.querySelector("#banner .banner")?.textContent || ""));
        const sentNow = (await user("GET", `/api/item-pages/${report.id}/comments`)).comments.find((entry) => entry.text === "Is March a full month?");
        const message = sentNow && sentNow.message_id ? await user("GET", `/api/messages/${sentNow.message_id}`) : null;
        check(`${at}: Send takes comments no step takes to the item's session`,
          Boolean(message) && message.session_id === SESSION_B && /^From the Pages tab\n\n.+'s 1 page comment:\n\n/.test(message.text),
          JSON.stringify(message && { session: message.session_id, text: message.text.slice(0, 80) }));
        const emptied = await waitFor(() => tab.locator("#pages-reply-text").inputValue(), (text) => text === "");
        check(`${at}: the box is empty after Send`, emptied === "", emptied);

        // A comment being written waits while another page is read, and through a new version of its own page.
        const other = await agent("POST", `/api/items/${item.id}/pages`, { title: "Other report", html: REPORT("") });
        const writing = await frameOf(tab, report.id);
        await sleep(900); // the version rebuilds itself after 300 ms, which would drop a selection made before
        await writing.evaluate(() => {
          const cell = [...document.querySelectorAll("td")].find((td) => td.textContent === "February").firstChild;
          const range = document.createRange();
          range.selectNodeContents(cell);
          getSelection().removeAllRanges();
          getSelection().addRange(range);
        });
        await tab.locator(".pc-float:not([hidden])").click();
        await tab.locator(".pc-draft textarea").fill("Look at February again");
        await tab.locator(".pages-back").click();
        await tab.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
        await tab.locator(`.pages-row[data-page="${other.id}"]`).click();
        await tab.locator(".pages-preview-buttons button", { hasText: "Comment" }).click();
        await tab.locator(".pc-head").waitFor();
        await tab.locator(".pages-back").click();
        await tab.locator(`.pages-row[data-page="${report.id}"]`).click();
        await tab.locator(".pages-preview-buttons button", { hasText: "Comment" }).click();
        await tab.locator(".pc-draft textarea").waitFor();
        check(`${at}: a comment being written waits while another page is read`, (await tab.locator(".pc-draft textarea").inputValue()) === "Look at February again",
          await tab.locator(".pc-draft textarea").inputValue());
        await tab.locator(".pages-preview-buttons button", { hasText: "Hide comments" }).click();
        const before = await tab.locator(".pages-frame").getAttribute("data-src");
        await agent("PUT", `/api/item-pages/${report.id}`, { title: "Exports report", html: rebuilt.replace("Exports include all tasks", "Exports include tasks") });
        await tab.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
        await tab.waitForFunction((src) => document.querySelector(".pages-frame")?.dataset.src !== src, before);
        await tab.locator(".pages-preview-buttons button", { hasText: "Comment" }).click();
        await tab.locator(".pc-head").waitFor();
        const waiting = (await tab.locator(".pc-column .pc-error").textContent().catch(() => "")) || "";
        check(`${at}: a new version keeps a hidden comment's words to place again`, waiting.includes("Look at February again"), waiting);
        // Hidden while it is saved, a comment whose save fails comes back.
        const placing = await frameOf(tab, report.id);
        await sleep(900);
        await placing.evaluate(() => {
          const cell = [...document.querySelectorAll("td")].find((td) => td.textContent === "February").firstChild;
          const range = document.createRange();
          range.selectNodeContents(cell);
          getSelection().removeAllRanges();
          getSelection().addRange(range);
        });
        await tab.locator(".pc-float:not([hidden])").click();
        await tab.route("**/api/item-pages/*/comments", async (route) => {
          if (route.request().method() !== "POST") return route.continue();
          await sleep(1000);
          await route.fulfill({ status: 422, contentType: "application/json", body: JSON.stringify({ detail: "refused for the test" }) });
        });
        await tab.locator(".pc-draft .button.primary").click();
        await tab.locator(".pages-preview-buttons button", { hasText: "Hide comments" }).click();
        await sleep(1500);
        await tab.unroute("**/api/item-pages/*/comments");
        await tab.locator(".pages-preview-buttons button", { hasText: "Comment" }).click();
        await tab.locator(".pc-draft textarea").waitFor();
        check(`${at}: a comment hidden during a failed save comes back`, (await tab.locator(".pc-draft textarea").inputValue()) === "Look at February again",
          await tab.locator(".pc-draft textarea").inputValue());
        // Refused as stale after the user went on to another page, it stays with its own page (regression check).
        await tab.route("**/api/item-pages/*/comments", async (route) => {
          if (route.request().method() !== "POST") return route.continue();
          await sleep(1000);
          await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: "the page changed since you opened it" }) });
        });
        await tab.locator(".pc-draft .button.primary").click();
        await tab.locator(".pages-back").click();
        await tab.locator(`.pages-row[data-page="${other.id}"]`).click();
        await tab.locator(".pages-preview-buttons button", { hasText: "Comment" }).click();
        await tab.locator(".pc-head").waitFor();
        await sleep(1500);
        await tab.unroute("**/api/item-pages/*/comments");
        // The next column built takes what a refusal carried: hide and show this page's comments.
        await tab.locator(".pages-preview-buttons button", { hasText: "Hide comments" }).click();
        await tab.locator(".pages-preview-buttons button", { hasText: "Comment" }).click();
        await tab.locator(".pc-head").waitFor();
        const elsewhere = `${(await tab.locator(".pc-column .pc-error").textContent().catch(() => "")) || ""}`
          + `${await tab.locator(".pc-draft textarea").inputValue().catch(() => "")}`;
        check(`${at}: a comment refused as stale after the user moved on stays off the other page`, !elsewhere.includes("Look at February again"), elsewhere);
        await tab.locator(".pages-back").click();
        await tab.locator(`.pages-row[data-page="${report.id}"]`).click();
        await tab.locator(".pages-preview-buttons button", { hasText: "Comment" }).click();
        await tab.locator(".pc-head").waitFor();
        await sleep(500);
        const home = `${(await tab.locator(".pc-column .pc-error").textContent().catch(() => "")) || ""}`
          + `${await tab.locator(".pc-draft textarea").inputValue().catch(() => "")}`;
        check(`${at}: it comes back on its own page`, home.includes("Look at February again"), home);
      }
      await tab.close();
      check(`${at}: no page errors`, errors.length === 0, errors.join(" | "));
      await context.close();
    }
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  console.error(`screenshots in ${SHOTS}`);
  const failed = results.filter((result) => !result.ok);
  console.error(failed.length ? `${failed.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failed.length ? 1 : 0;
}

// The frame of the page with comments, once its script said it is ready.
async function frameOf(page, pageId) {
  const end = Date.now() + 8000;
  while (Date.now() < end) {
    const frame = page.frames().find((each) => each.url().includes(`/pages/${pageId}/figure?comments=1`));
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
  throw new Error(`no comment frame for page ${pageId}`);
}

async function waitFor(read, done) {
  let value;
  for (let tries = 0; tries < 40; tries++) {
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
