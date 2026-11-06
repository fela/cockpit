// A page opens over Cockpit's page (SPEC NEXT-47), and its choices go to the step (NEXT-48). From a step card on
// Cockpit's main page (`/` at desktop width in light, `/classic` at phone width in dark) it checks:
// - a click on the step's link to its page opens the page in a layer over the main page, with no header or list,
//   and the address stays the main page's;
// - a radio button on the page is a choice: it is saved for the step, the layer says so, and the Comment button
//   still opens the comment column;
// - the X closes the layer, and the step card then lists the choice above its reply box; a comment being written
//   when the X closed the layer is back in its box when the layer opens again (regression check), also with the tab's
//   sessionStorage blocked (regression check);
// - opened again, the page shows the saved choice; a `cockpit.choose` call saves another; a checkbox the page
//   checks to start with, and a text box it fills in to start with, once cleared, are saved as cleared and stay
//   clear when the page opens again, as does a select with no empty option that `cockpit.choose` clears
//   (regression check);
//   Escape closes it;
// - opened again, the browser's Back closes it and stays on the main page;
// - Send with an empty reply box hands the step off with the choices, and they show as sent, while a choice saved
//   for another step failed (regression check);
// - a click with Ctrl (a new tab) leaves the main page as it is;
// - the page linked from the step's card loads ahead, hidden, and a click shows it with nothing left to load;
// - a page not loaded ahead: the layer takes its place at once, with a loading sheet naming the page where the page
//   will be, and the page's frame laid out at its full width under it; a slow read of Cockpit's state holds the
//   page back no longer, and the X closes the layer while it loads; an Escape the page handles itself leaves the
//   layer open;
// - no page errors and no Content Security Policy errors.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs)
// and drives headless Chromium. Prints one line per check, saves screenshots in SHOTS_DIR (default: a temporary
// directory) and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/page_overlay.cjs

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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "overlay-test", "content-type": "application/json" };
const SHOTS = process.env.SHOTS_DIR || fs.mkdtempSync(path.join(os.tmpdir(), "cockpit-overlay-shots-"));

const OPTIONS = `<!doctype html><html><head><style>
body{font:16px/1.5 system-ui;margin:24px}label{display:block;margin:6px 0}.card{border:1px solid #888;padding:8px;margin:6px 0;cursor:pointer}
</style></head><body>
<h1>Pick a launch plan</h1>
<p>Three ways to ship the exports report.</p>
<fieldset><legend>Plan</legend>
<label><input type="radio" name="plan" value="a"> Plan A: ship now</label>
<label><input type="radio" name="plan" value="b"> Plan B: ship in April</label>
</fieldset>
<label><input type="checkbox" name="notify" value="yes" checked> Tell the team</label>
<label>Report title <input type="text" name="title" value="Exports"></label>
<label>Size <select name="size"><option value="s">Small</option><option value="l">Large</option></select></label>
<p><a href="#extra">To the extras</a></p>
<div class="card" id="extra">Add the weekly chart</div>
<script>document.getElementById("extra").addEventListener("click", () => cockpit.choose("extras", ["chart"], "Weekly chart"));</script>
</body></html>`;

const { chromium } = require(PLAYWRIGHT);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-overlay-");
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
    const copied = await user("GET", "/api/state");
    await Promise.all([...copied.queue, ...copied.ready_next, ...copied.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const run = Date.now();
    const sizes = [["desktop", 1280, 900, "light", "/"], ["phone", 390, 844, "dark", "/classic"]];
    for (const [label, width, height, scheme, home] of sizes) {
      const key = `note:overlay-${run}-${label}`;
      const item = await agent("PUT", "/api/items", { key, title: `Overlay test ${label}`, priority: "urgent", sessions: [SESSION_B] });
      const options = await agent("POST", `/api/items/${item.id}/pages`, { title: "Launch plans", html: OPTIONS });
      const answer = await agent("POST", "/api/steps", {
        item_key: key, need: `overlay-${label}`, ask: "Which launch plan?", score: 100, session_id: SESSION_B,
        body: `Pick one on [the plans page](${options.url}).`,
      });
      const stepId = answer.step.id;
      const touch = width <= 900;
      const beside = width >= 1100;
      const context = await browser.newContext({ viewport: { width, height }, colorScheme: scheme, hasTouch: touch, isMobile: touch });
      // A cookie the browser keeps, so a new tab is signed in too.
      await context.addCookies([{ name: info.name, value: info.value, domain: new URL(info.origin).hostname, path: "/", secure: true, httpOnly: true, sameSite: "Lax" }]);
      const page = await context.newPage();
      // The fixture can open the "Back after" dialog over the page while the user's timer is idle;
      // answer it with Return whenever it shows, so it never takes the test's clicks.
      await page.addLocatorHandler(page.locator("dialog#idle[open]"), (dialog) => dialog.locator("button", { hasText: "Return" }).click()
        .catch((error) => { if (!/has been closed/.test(error.message)) throw error; }));
      page.setDefaultTimeout(20000);
      const errors = [];
      const watch = (target) => {
        target.on("pageerror", (error) => errors.push(error.message));
        target.on("console", (message) => { if (message.type() === "error" && /Content Security Policy/.test(message.text())) errors.push(message.text()); });
      };
      watch(page);
      const shot = (name) => page.screenshot({ path: path.join(SHOTS, `${label}-${scheme}-${name}.png`) });
      const at = `${label} (${scheme}, ${home})`;

      // On `/` the main page shows one inbox entry at a time; the test's own item is the one it opens, whatever else
      // the fixture has waiting (a permission request comes first).
      await page.goto(`${info.origin}${home}${home === "/" ? `#home/item/${item.id}` : ""}`);
      const card = page.locator(`#next [data-step="${stepId}"]`).first();
      await card.waitFor();
      const openCard = async () => {
        if (await card.evaluate((el) => el.classList.contains("step-row") && !el.classList.contains("open"))) await card.locator(".row-head").click();
      };
      await openCard();
      // The link in the step's text; the folded ⋯ menu holds another to the same page (VIEW-29).
      const link = card.locator(`a[href*="/pages?id=${options.id}&step=${stepId}"]:visible`).first();
      const before = page.url();
      const depth = await page.evaluate(() => history.length);

      // The page the step's card links to loads ahead; a click on the link then shows it drawn at once.
      const ahead = await page.waitForFunction((id) => [...document.querySelectorAll("dialog.page-overlay-preload")]
        .some((each) => !each.open && each.querySelector(`iframe[src*="id=${id}&"]`) && !each.classList.contains("page-overlay-loading-page")),
      options.id, { timeout: 30000 }).then(() => true, () => false);
      check(`${at}: the step's page loads ahead, hidden`, ahead, "no drawn layer loading ahead");
      // Open: the layer shows the page alone, over the main page.
      await link.click();
      // The layer once its page has drawn; while it loads, a loading sheet is in the page's place.
      const dialog = page.locator("dialog.page-overlay[open]:not(.page-overlay-loading-page)");
      const atOnce = await dialog.count();
      check(`${at}: the page loaded ahead shows at once, with no loading sheet`, atOnce === 1
        && (await page.locator("dialog.page-overlay[open] .page-overlay-loading").count()) === 0, `${atOnce} drawn layers`);
      await dialog.waitFor();
      const layer = await openLayer(page);
      check(`${at}: the layer shows the step's page`, layer.url().includes(`/pages?id=${options.id}&step=${stepId}`) && layer.url().includes("overlay=1"), layer.url());
      const figure = await figureIn(layer, options.id);
      await figure.locator("input[value=b]").waitFor();
      const bare = await layer.evaluate(() => ({
        top: getComputedStyle(document.querySelector(".top") || document.body).display,
        list: getComputedStyle(document.querySelector(".pages-index") || document.body).display,
      }));
      check(`${at}: the page opens in a layer over the main page`, page.url() === before, page.url());
      check(`${at}: the layer shows the page alone`, bare.top === "none" && bare.list === "none", JSON.stringify(bare));
      // The layer slides in; it is measured where it lands.
      await page.waitForFunction(() => document.querySelector("dialog.page-overlay[open]").getAnimations().length === 0);
      const box = await page.locator("dialog.page-overlay[open] iframe.page-overlay-frame").boundingBox();
      // At desktop width the page opens beside the step's card: the window's right part.
      const besideW = await page.evaluate(() => document.querySelector("main.layout").getBoundingClientRect().width);
      if (beside) check(`${at}: the layer takes the window's right part, beside the card`, box && Math.abs(box.x - besideW) <= 2 && box.x + box.width >= width - 1 && box.height >= height - 1, JSON.stringify({ box, besideW }));
      else check(`${at}: the layer fills the window`, box && box.width >= width - 1 && box.height >= height - 1, JSON.stringify(box));
      await shot("1-open");

      // A radio button is a choice for the step.
      await figure.locator("input[value=b]").check();
      const note = await waitFor(() => layer.locator(".pages-choice-note").textContent().catch(() => ""), (text) => text.startsWith("Choice saved"));
      check(`${at}: the layer says the choice is saved for the answer`, note === "Choice saved; it goes with your answer", note);
      const saved = await user("GET", `/api/item-pages/${options.id}/choices`);
      check(`${at}: the choice is saved for the step`, saved.choices.length === 1 && saved.choices[0].chosen === "b"
        && saved.choices[0].step_id === stepId && saved.choices[0].label === "Plan B: ship in April", JSON.stringify(saved.choices));
      await shot("2-chosen");
      // The page's buttons sit left of the layer's X, in the window's terms (beside the card, the frame starts right
      // of the card's column).
      const at0 = await page.locator("dialog.page-overlay[open] iframe.page-overlay-frame:not(.page-overlay-next)").boundingBox();
      const head = await layer.locator(".pages-overlay-head").evaluate((el, origin) => [...el.querySelectorAll(".button, .pages-choice-note")].map((part) => {
        const box = part.getBoundingClientRect();
        return { left: box.left + origin.x, right: box.right + origin.x, top: box.top + origin.y, bottom: box.bottom + origin.y };
      }), { x: at0.x, y: at0.y });
      const shut = await page.locator("dialog.page-overlay[open] .page-overlay-close").boundingBox();
      const clear = head.every((part) => part.right <= shut.x || part.bottom <= shut.y || part.top >= shut.y + shut.height) && head.every((part) => part.left >= at0.x);
      check(`${at}: the page's buttons leave the X clear`, clear, JSON.stringify({ head, shut }));
      if (beside) {
        // Beside the card, which lists the comments, the page has no comment column: commenting is on from the start.
        await layer.locator('.pages-overlay-head [data-head="pick"]:not([disabled])').waitFor();
        const parts = await layer.evaluate(() => ({
          comment: document.querySelectorAll('.pages-overlay-head [data-head="comment"]').length,
          column: [...document.querySelectorAll(".pc-column, .pages-side")].filter((el) => el.getClientRects().length > 0).length,
        }));
        check(`${at}: beside the card the page has Pick a part and no comment column`, parts.comment === 0 && parts.column === 0, JSON.stringify(parts));
      } else {
        await layer.locator(".pages-overlay-head button", { hasText: "Comment" }).click();
        await layer.locator(".pc-column").waitFor();
        check(`${at}: Comment opens the comment column in the layer`, (await layer.locator(".pc-column").count()) === 1, "no column");
      }
      await shot("3-commenting");
      // A comment being written as the X closes the layer.
      await figure.evaluate(() => {
        const range = document.createRange();
        range.selectNodeContents(document.querySelector("h1").firstChild);
        getSelection().removeAllRanges();
        getSelection().addRange(range);
      });
      await layer.locator(".pc-float:not([hidden])").click();
      // Beside the card the words go into its reply box as a quote; on the whole window a box on the page takes the comment.
      const reply = card.locator(".composer textarea").first();
      if (beside) {
        const quoted = await waitFor(() => reply.inputValue().catch(() => ""), (text) => text.includes("> "));
        check(`${at}: beside the card, Comment quotes the words into the card's reply box`, /^> On the page “.+” \(page \d+\):\n> \S/.test(quoted), JSON.stringify(quoted));
        await page.keyboard.type("Say when April starts");
      } else {
        await layer.locator(".pc-draft textarea").fill("Say when April starts");
        // With the tab's sessionStorage blocked, in Cockpit's page and in the layer, the comment still comes back.
        for (const realm of [page, layer]) {
          await realm.evaluate(() => {
            window.storageMethods = {};
            for (const name of ["getItem", "setItem", "removeItem"]) {
              const own = Storage.prototype[name];
              window.storageMethods[name] = own;
              Storage.prototype[name] = function (...args) {
                if (this === sessionStorage) throw new DOMException("blocked for the test", "SecurityError");
                return own.apply(this, args);
              };
            }
          });
        }
      }

      // X closes; the step card lists the choice.
      await page.locator("dialog.page-overlay[open] .page-overlay-close").click();
      await waitFor(() => dialog.count(), (count) => count === 0);
      check(`${at}: the X closes the layer`, (await dialog.count()) === 0, "still open");
      const onOverlayEntry = () => page.evaluate(() => Boolean(history.state && history.state.cockpitPageOverlay));
      check(`${at}: closing takes its history entry back`, !(await waitFor(onOverlayEntry, (on) => !on)) && page.url() === before, page.url());
      await openCard();
      const listed = await waitFor(() => card.locator(".page-comments-pending summary").allTextContents(), (texts) => texts.some((text) => text.includes("choice")));
      check(`${at}: the step card lists the choice`, listed.includes("1 choice on pages go with this answer"), JSON.stringify(listed));

      // Again: the choice shows; cockpit.choose saves another; Escape closes.
      await link.click();
      await dialog.waitFor();
      const figure2 = await figureIn(await openLayer(page), options.id);
      const restored = await waitFor(() => figure2.locator("input[value=b]").isChecked().catch(() => false), (on) => on);
      check(`${at}: the page shows the saved choice`, restored, "Plan B not checked");
      const layer2 = await openLayer(page);
      if (beside) {
        const kept = await reply.inputValue();
        check(`${at}: the comment written in the card's reply box stays there through the X`, kept.endsWith("Say when April starts"), JSON.stringify(kept));
        await reply.fill("");
      } else {
        const kept = await waitFor(() => layer2.locator(".pc-draft textarea").inputValue().catch(() => ""), (text) => text !== "");
        check(`${at}: the comment being written as the X closed the layer is back, with sessionStorage blocked`, kept === "Say when April starts", JSON.stringify(kept));
        await page.evaluate(() => { for (const [name, own] of Object.entries(window.storageMethods)) Storage.prototype[name] = own; });
        await layer2.locator(".pc-draft button", { hasText: "Cancel" }).click();
      }
      // Clearing the checkbox the page checked to start with is a choice too.
      await figure2.locator("input[name=notify]").uncheck();
      const cleared = await waitFor(() => user("GET", `/api/item-pages/${options.id}/choices`), (data) => data.choices.some((c) => c.name === "notify"));
      check(`${at}: a cleared checkbox is saved as cleared`, cleared.choices.some((c) => c.name === "notify" && Array.isArray(c.chosen) && c.chosen.length === 0),
        JSON.stringify(cleared.choices));
      await figure2.locator("input[name=title]").fill("");
      const emptied = await waitFor(() => user("GET", `/api/item-pages/${options.id}/choices`), (data) => data.choices.some((c) => c.name === "title"));
      check(`${at}: a cleared text box is saved as cleared`, emptied.choices.some((c) => c.name === "title" && c.chosen === null), JSON.stringify(emptied.choices));
      await figure2.evaluate(() => cockpit.choose("size", null));
      const unsized = await figure2.locator("select[name=size]").evaluate((select) => select.selectedIndex);
      check(`${at}: cockpit.choose clears a select with no empty option`, unsized === -1, `selected index ${unsized}`);
      await figure2.locator("#extra").click();
      const two = await waitFor(() => user("GET", `/api/item-pages/${options.id}/choices`), (data) => data.choices.length === 5);
      check(`${at}: cockpit.choose saves a choice`, two.choices.some((c) => c.name === "extras" && c.chosen[0] === "chart" && c.label === "Weekly chart"), JSON.stringify(two.choices));
      // A link within the page moves within it and adds no history entry.
      await figure2.locator("a[href='#extra']").click();
      const entries = await page.evaluate(() => history.length);
      check(`${at}: a link within the page adds no history entry`, entries === depth + 1, `${entries} entries, ${depth} before opening`);
      await figure2.locator("h1").click();
      // A page that handles an Escape itself keeps it: the layer stays open.
      await figure2.evaluate(() => document.addEventListener("keydown", (event) => event.preventDefault(), { once: true }));
      await page.keyboard.press("Escape");
      await sleep(800);
      check(`${at}: an Escape the page handles itself leaves the layer open`, (await dialog.count()) === 1, "the layer closed");
      await page.keyboard.press("Escape");
      await waitFor(() => dialog.count(), (count) => count === 0);
      check(`${at}: Escape closes the layer`, (await dialog.count()) === 0, "still open");
      check(`${at}: Escape takes the history entry back`, !(await waitFor(onOverlayEntry, (on) => !on)) && page.url() === before, page.url());
      // A close with no comment being written leaves none kept, so the page loads ahead again.
      const again = await page.waitForFunction((id) => [...document.querySelectorAll("dialog.page-overlay-preload")]
        .some((each) => each.querySelector(`iframe[src*="id=${id}&"]`) && !each.classList.contains("page-overlay-loading-page")),
      options.id, { timeout: 30000 }).then(() => true, () => false);
      check(`${at}: after a close, the page loads ahead again`, again, "no drawn layer loading ahead");

      // Again: Back closes and stays on the main page.
      await openCard();
      await link.click();
      await dialog.waitFor();
      await page.goBack();
      await waitFor(() => dialog.count(), (count) => count === 0);
      check(`${at}: Back closes the layer and stays on the main page`, (await dialog.count()) === 0 && page.url() === before, page.url());

      // A Ctrl click leaves the main page as it is.
      const opened = context.waitForEvent("page", { timeout: 5000 }).catch(() => null);
      await openCard();
      await link.click({ modifiers: ["Control"] });
      const tab = await opened;
      check(`${at}: a Ctrl click opens no layer`, (await dialog.count()) === 0, "a layer opened");
      if (tab) await tab.close();

      // A page not loaded ahead (the test drops the layers loading ahead): the layer takes its place at once, with
      // the loading sheet, and a slow read of Cockpit's state in it holds the page back no longer. The layer's reads
      // of Cockpit's state wait 20 s; `released` is when the first one goes on.
      const dropAhead = () => page.evaluate(() => document.querySelectorAll("dialog.page-overlay-preload").forEach((each) => each.remove()));
      let released = Infinity;
      const slowState = async (route) => {
        if (route.request().frame().url().includes("overlay=1")) {
          await sleep(20000);
          released = Math.min(released, Date.now());
        }
        await route.continue().catch(() => {});
      };
      // The page itself waits until the sheet has been looked at, so a fast load cannot skip it.
      let letFigure;
      const figureHeld = new Promise((resolve) => { letFigure = resolve; });
      const heldFigure = async (route) => {
        await figureHeld;
        await route.continue().catch(() => {});
      };
      await page.route("**/api/state", slowState);
      await page.route(`**/pages/${options.id}/figure*`, heldFigure);
      await openCard();
      await dropAhead();
      const started = Date.now();
      await link.click();
      const loading = page.locator("dialog.page-overlay[open].page-overlay-loading-page");
      const sheet = loading.locator(".page-overlay-loading");
      const placed = await loading.count();
      check(`${at}: the layer takes its place at once on the click`, placed === 1
        && (!beside || await page.evaluate(() => document.documentElement.classList.contains("page-beside"))), `${placed} loading layers`);
      const sheetText = await sheet.textContent().catch(() => "");
      check(`${at}: while the page loads, the layer shows a loading sheet naming the page`,
        // A layer the step's ⋯ menu link started loading ahead names the page by that link's words, its title.
        (await sheet.isVisible()) && sheetText.includes("Loading the page")
        && [(await link.textContent()).trim(), "Launch plans"].some((name) => sheetText.includes(name)), sheetText);
      await sleep(400);
      await shot("4-loading");
      const frameWidth = await loading.locator("iframe.page-overlay-frame").evaluate((frame) => frame.getBoundingClientRect().width).catch(() => 0);
      // Beside the card, the layer has the window's width less the card's column.
      const fullWidth = page.viewportSize().width - (beside ? await loading.evaluate((layer) => parseFloat(getComputedStyle(layer).left)) : 0);
      check(`${at}: the page's frame has its full width while it loads`, frameWidth >= fullWidth - 1, `width ${frameWidth} of ${fullWidth}`);
      letFigure();
      await page.unroute(`**/pages/${options.id}/figure*`, heldFigure);
      await dialog.waitFor({ timeout: 30000 });
      const shownAt = Date.now();
      check(`${at}: the layer shows its page before Cockpit's state arrives`, shownAt < released,
        `shown ${shownAt - started} ms after the click, state released ${released - started} ms after it`);
      await waitFor(() => page.locator("dialog.page-overlay[open] .page-overlay-loading").count(), (count) => count === 0);
      check(`${at}: the loading sheet goes once the page has drawn`, (await page.locator("dialog.page-overlay[open] .page-overlay-loading").count()) === 0, "the sheet stays");
      const figure4 = await figureIn(await openLayer(page), options.id);
      check(`${at}: the layer shows its page once drawn`, (await figure4.locator("input[value=b]").count()) === 1, "no page in the layer");
      await page.locator("dialog.page-overlay[open] .page-overlay-close").click();
      await waitFor(() => page.locator("dialog.page-overlay[open]").count(), (count) => count === 0);
      await page.unroute("**/api/state", slowState);

      // The X while the page loads closes the layer, gives the keyboard back to the link and leaves the main page
      // where it was in its history.
      const slowFigure = async (route) => {
        await sleep(4000);
        await route.continue().catch(() => {});
      };
      await page.route(`**/pages/${options.id}/figure*`, slowFigure);
      await openCard();
      await dropAhead();
      await link.focus();
      await link.press("Enter");
      await sheet.waitFor();
      await page.locator("dialog.page-overlay[open] .page-overlay-close").click();
      await waitFor(() => page.locator("dialog.page-overlay[open]").count(), (count) => count === 0);
      check(`${at}: the X while the page loads gives the keyboard back to the link`, await link.evaluate((node) => node === document.activeElement), "focus is elsewhere");
      await sleep(4500);
      check(`${at}: the X while the page loads closes the layer`, (await page.locator("dialog.page-overlay:not(.page-overlay-preload)").count()) === 0, "a layer is there");
      const where = await page.evaluate(() => ({ url: location.href, layer: Boolean(history.state?.cockpitPageOverlay) }));
      check(`${at}: the X while the page loads leaves the main page where it was`, where.url === before && !where.layer, JSON.stringify(where));
      await page.unroute(`**/pages/${options.id}/figure*`, slowFigure);

      // A slow save goes on after the X, and Send waits for it: the answer carries the choice just made.
      await openCard();
      await link.click();
      await dialog.waitFor();
      const figure3 = await figureIn(await openLayer(page), options.id);
      await waitFor(() => figure3.locator("input[value=b]").isChecked().catch(() => false), (on) => on);
      const notify = await waitFor(() => figure3.locator("input[name=notify]").isChecked().catch(() => true), (on) => !on);
      check(`${at}: the cleared checkbox stays clear when the page opens again`, !notify, "checked again");
      const title = await waitFor(() => figure3.locator("input[name=title]").inputValue().catch(() => "?"), (text) => text === "");
      check(`${at}: the cleared text box stays clear when the page opens again`, title === "", JSON.stringify(title));
      const size = await waitFor(() => figure3.locator("select[name=size]").evaluate((select) => select.selectedIndex).catch(() => 0), (index) => index === -1);
      check(`${at}: the cleared select stays empty when the page opens again`, size === -1, `selected index ${size}`);
      // A choice saved for another step that fails holds up no answer to this one, nor does a choice for this step on
      // a page since removed (Cockpit answers 404), which has nowhere to go.
      const refuse = (route) => route.fulfill({ status: 400, contentType: "application/json", body: JSON.stringify({ detail: "refused" }) });
      await page.route("**/api/item-pages/999999999/choices", refuse);
      await page.evaluate((id) => {
        CockpitChoices.save(999999999, { name: "other", chosen: "x", label: "", step_id: 999999999 }, () => {});
        CockpitChoices.save(999999998, { name: "gone", chosen: "x", label: "", step_id: id }, () => {});
      }, stepId);
      const slow = async (route) => {
        // The saves for the other step and the removed page above keep their own answers.
        if (route.request().url().includes("/item-pages/99999999")) return route.fallback();
        if (route.request().method() === "PUT") await sleep(3000);
        await route.continue();
      };
      await page.route("**/api/item-pages/*/choices", slow);
      await figure3.locator("input[value=a]").check();
      await page.locator("dialog.page-overlay[open] .page-overlay-close").click();
      await waitFor(() => dialog.count(), (count) => count === 0);
      await openCard();
      const send = card.locator(".button.send");
      check(`${at}: Send works with only choices`, !(await send.isDisabled()), "Send disabled");
      await shot("5-card");
      await send.click();
      const sent = await waitFor(() => user("GET", `/api/item-pages/${options.id}/choices`), (data) => data.choices.every((c) => c.sent));
      check(`${at}: Send carries the choices`, sent.choices.length === 5 && sent.choices.every((c) => c.sent), JSON.stringify(sent.choices));
      check(`${at}: the answer carries the choice saved after the X`, sent.choices.some((c) => c.name === "plan" && c.chosen === "a"), JSON.stringify(sent.choices));
      await page.unroute("**/api/item-pages/*/choices", slow);
      await page.unroute("**/api/item-pages/999999999/choices", refuse);

      check(`${at}: no page or policy errors`, errors.length === 0, errors.join(" | "));
      await context.close();
    }
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failed = results.filter((result) => !result.ok);
  console.error(`${results.length - failed.length}/${results.length} checks passed; screenshots in ${SHOTS}`);
  if (failed.length) process.exitCode = 1;
}

// A frame anywhere in the page whose address passes `match`, once loaded.
// The frame of the open layer (a closed one stays hidden a few seconds), and the shared page's frame inside it.
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
  throw new Error(`no frame of page ${pageId} in the layer`);
}

async function waitFor(read, done) {
  let value;
  for (let tries = 0; tries < 60; tries++) {
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
