// A to-do added with + goes into Busy in the list at the left, as an Undo button with a timer, then stays at the top of
// Busy, pulsing.
// Checks, on the main page at 1280 px dark and 390 px light, from the press of Enter in the Add box on:
// - no Just added group shows; the new row is in Busy, which stays folded, and Busy's heading is in view;
// - for its first seconds Busy's heading lights up and keeps its name; its count rolls from the number before to the
//   new one, which is bold in the accent and pulses (no roll or pulse with reduced motion at 390); an Undo at its right
//   shows its remaining seconds over a shrinking bar inside Undo; the Add still waits to reach the server; at 1280 the
//   Add box flies onto the heading (none with reduced motion at 390); afterwards the heading shows Busy and its count
//   again, and Busy is still folded;
// - after the Undo time the Add goes out once, and the row turns into the to-do's own row, first in Busy and pulsing,
//   still so after a few seconds (a still tint with reduced motion);
// - Undo on a second to-do, with Busy now opened by the user, is its row in the list and not the heading, and takes it
//   back into the Add box with its words, and nothing reaches the server; on a phone's item screen, which hides the
//   list, the notice's Undo does;
// - at 1280, with Busy folded again, the heading's Undo takes a third to-do back the same way and leaves Busy folded;
// - it is never listed twice, also when a poll brings the server's item before the server answers the Add (held here
//   for 3 s);
// - two Adds of the same text, the second's answer held while polls bring both, show as two rows (1280 px);
// - on a phone, with the page scrolled down to read a card, the page scrolls up to the new row and stays there;
// - a click on it shows it on the card.
// Watches every drawing of the list, so it holds when the machine is slow. Saves screenshots in the work dir.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database, fake sessions; README.md says what
// it needs) and drives a headless Chromium. Prints one line per check and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/add_arrives.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs"); const os = require("node:os"); const path = require("node:path");
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const { chromium } = require(PLAYWRIGHT);
const ROOT = path.join(__dirname, "..", "..");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// Presses the button that matches `selector` and holds `text`, inside the page: the list redraws on every poll, and on
// a loaded host a click from outside often meets a button that was just replaced.
const pressIn = (page, selector, text) => page.evaluate(([selector, text]) => {
  const button = [...document.querySelectorAll(selector)].find((one) => (one.textContent + (one.getAttribute("aria-label") || "")).includes(text));
  if (!button) throw new Error(`no ${selector} with ${text}`);
  button.click();
}, [selector, text]);
async function up(work, server) {
  const file = path.join(work, "cookie.json"); const end = Date.now() + 90e3;
  while (Date.now() < end) {
    if (server.exitCode !== null) throw new Error("server exited");
    if (fs.existsSync(file)) { const info = JSON.parse(fs.readFileSync(file, "utf8"));
      if (await fetch(`${info.origin}/healthz`).then((r) => r.ok, () => false)) return info; }
    await sleep(200);
  }
  throw new Error("no server");
}
(async () => {
  const work = fs.mkdtempSync(path.join(os.tmpdir(), "cockpit-add-arrives-"));
  const server = spawn(path.join(ROOT, ".venv/bin/python"), [path.join(ROOT, "tests/browser/serve_preview.py"), work], { stdio: ["ignore", "ignore", "inherit"] });
  let browser; const fails = [];
  const check = (n, ok, d) => { console.log(`${ok ? "pass" : "FAIL"} ${n}${ok ? "" : ": " + d}`); if (!ok) fails.push(n); };
  try {
    const info = await up(work, server);
    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const [width, scheme] of [[1280, "dark"], [390, "light"]]) {
      const ctx = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme, reducedMotion: width === 390 ? "reduce" : "no-preference" });
      await ctx.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await ctx.newPage(); page.setDefaultTimeout(120000);
      const errors = []; page.on("pageerror", (e) => errors.push(e.message));
      await page.goto(`${info.origin}/`);
      // A phone opens on the item screen; the list is the screen behind its bar's key.
      if (width === 390) await page.locator(".phone-back").click();
      await page.locator("#sidebar .sidebar-groups").waitFor({ timeout: 120000 });
      const busyOpen = () => page.evaluate(() => Boolean(document.querySelector("#sidebar .sidebar-group.busy")?.open));
      check(`${width}: Busy starts folded`, !(await busyOpen()), "open");
      const title = `Renew passport ${width}`;
      // At 1280 the server's answer to the Add is held while polls bring the new item; at 390 the reader is far down.
      if (width === 1280) {
        await page.route("**/api/items/add", async (route) => {
          noteAdd(route.request());
          const response = await route.fetch();
          await sleep(3000);
          await route.fulfill({ response });
        });
      } else {
        await page.evaluate(() => window.scrollTo(0, 1100));
      }
      // The fixture can hold the user's open "Back after" question, whose dialog covers the page.
      if (await page.locator("dialog#idle[open]").count()) await page.locator("dialog#idle button", { hasText: "Return" }).click();
      // When each Add reached the network; a request the route below holds counts once.
      const sentAt = new Map();
      const posts = { get length() { return sentAt.size; }, map: (f) => [...sentAt.values()].map(f), at: (i) => [...sentAt.values()][i] };
      const noteAdd = (request) => { if (request.method() === "POST" && new URL(request.url()).pathname === "/api/items/add" && !sentAt.has(request)) sentAt.set(request, Date.now()); };
      page.on("request", noteAdd);
      await page.locator(".sidebar-add").click();
      await page.locator('#add-box textarea[data-key="add:new"]').fill(title);
      // Watch every drawing of the list from the press on.
      await page.evaluate((title) => {
        const seen = window.__seen = { lit: false, litReal: false, maxRows: 0, firstGroup: new Set(), inView: false, group: new Set(), justAdded: false, flight: false, held: false, opened: false };
        new MutationObserver(() => { if (document.querySelector(".add-flight")) seen.flight = true; }).observe(document.body, { childList: true });
        // In view: below the top bar and above the screen's bottom edge, in the list's own scroll box too.
        const inView = window.__inView = (box) => {
          const list = document.querySelector("#sidebar").getBoundingClientRect();
          const top = Math.max(document.querySelector(".top").getBoundingClientRect().bottom, list.top);
          return box.top >= top - 1 && box.bottom <= Math.min(innerHeight, list.bottom) + 1;
        };
        const look = () => {
          const rows = [...document.querySelectorAll("#sidebar li.side-item")].filter((li) => li.textContent.includes(title));
          seen.maxRows = Math.max(seen.maxRows, rows.length);
          if (document.querySelector("#sidebar .sidebar-group.added")) seen.justAdded = true;
          for (const li of rows) {
            if (li.querySelector(".undo-add")) seen.held = true;
            seen.group.add(li.closest("details")?.className || "");
            if (li.classList.contains("fresh")) { seen.lit = true; if (/^\d+$/.test(li.dataset.item)) seen.litReal = true; }
            const box = li.getBoundingClientRect();
            if (inView(box)) seen.inView = true;
          }
          const first = document.querySelector("#sidebar .sidebar-group");
          if (rows.length) seen.firstGroup.add(first ? first.className : "");
          const busy = document.querySelector("#sidebar .sidebar-group.busy");
          if (busy?.open) seen.opened = true;
          if (busy && rows.length && inView(busy.querySelector(":scope > summary").getBoundingClientRect())) seen.inView = true;
        };
        new MutationObserver(look).observe(document.getElementById("sidebar"), { childList: true, subtree: true });
        addEventListener("scroll", look, { capture: true, passive: true });
      }, title);
      const pressed = Date.now();
      await page.keyboard.press("Enter");
      await page.locator("#sidebar li.side-item", { hasText: title }).first().waitFor({ state: "attached" });
      // Busy's heading is lit with its name and new count, its Undo at the right, with the bar running down and the
      // seconds left, and nothing sent yet.
      // The bar's width (its scale along the row) and the seconds, sampled twice 1.2 s apart inside the page, so a
      // stopped timer fails and a slow test run does not stretch the gap. On a loaded machine the browser draws fewer
      // frames and the bar's animation clock lags the wall clock, so the bar only has to shrink by a twentieth (1.2 s is a
      // fifth of the 6 s).
      const [heldNow, heldLater] = await page.evaluate(async ([title, pressed]) => {
        // Each sample finds the row again, as a redraw replaces it.
        const sample = () => {
          const button = [...document.querySelectorAll("#sidebar .sidebar-group > summary > button.busy-undo")].find((one) => one.getAttribute("aria-label").includes(title));
          if (!button) return { gone: true };
          const summary = button.parentElement;
          const fill = button.querySelector(".busy-undo-fill");
          const transform = getComputedStyle(fill).transform;
          const box = button.getBoundingClientRect(); const head = summary.getBoundingClientRect();
          const name = summary.querySelector(".sidebar-group-name"); const was = summary.querySelector(".count-was"); const now = summary.querySelector(".count-now");
          const nowStyle = getComputedStyle(now);
          return { text: button.textContent, animation: getComputedStyle(fill).animationName, scale: transform === "none" ? 1 : new DOMMatrix(transform).a,
            secs: button.querySelector(".undo-add-secs").textContent, group: button.closest("details").className, open: button.closest("details").open,
            name: name && name.getBoundingClientRect().width > 0 ? name.textContent : "", lit: getComputedStyle(summary).backgroundColor,
            was: was.textContent, now: now.textContent, nowWeight: nowStyle.fontWeight, nowAnim: nowStyle.animationName,
            wasOpacity: getComputedStyle(was).display === "none" ? 0 : Number(getComputedStyle(was).opacity),
            // The Undo sits at the heading's right, after the count, with the bar inside it; only the fold arrow and the
            // heading's padding follow it, in under 48 px.
            right: getComputedStyle(button).display !== "none" && box.left > now.getBoundingClientRect().right
              && box.right >= head.right - 48,
            // ...and visible there: opaque, in a tint other than the button's own.
            barInUndo: Boolean(fill) && fill.parentElement === button && !summary.querySelector(":scope > .undo-add-fill")
              && getComputedStyle(fill).opacity === "1"
              && getComputedStyle(fill).backgroundColor !== getComputedStyle(button).backgroundColor };
        };
        const first = { ...sample(), at: Date.now() - pressed };
        await new Promise((resolve) => setTimeout(resolve, 1200));
        return [first, sample()];
      }, [title, pressed]).catch((error) => [{ error: error.message }, {}]);
      await page.screenshot({ path: path.join(work, `add-held-${width}.png`) });
      const secs = (one) => Number((/^(\d) s$/.exec(one.secs || "") || [])[1]);
      check(`${width}: Busy's heading is lit, keeps its name and has the Undo at its right with the bar inside it, Busy folded`, /Undo/.test(heldNow.text || "") && /\bbusy\b/.test(heldNow.group || "")
        && !heldNow.open && heldNow.right && heldNow.barInUndo && heldNow.name === "Busy" && !/rgba\(0, 0, 0, 0\)|transparent/.test(heldNow.lit), JSON.stringify(heldNow));
      check(`${width}: the count goes up by one and the new number is bold${width === 390 ? "" : ", rolls in and pulses"}`,
        Number(heldNow.now) === Number(heldNow.was) + 1 && Number(heldNow.nowWeight) >= 700
          // The old number rises out over 1.3 s from 0.45 s after the Add; with reduced motion it does not show.
          && (width === 390 ? heldNow.wasOpacity === 0 && heldNow.nowAnim === "none"
            : (heldLater.wasOpacity < heldNow.wasOpacity || heldLater.wasOpacity === 0) && /count-pulse/.test(heldLater.nowAnim)), JSON.stringify([heldNow, heldLater]));
      check(`${width}: its bar runs down`, heldNow.animation === "undo-run-down" && heldLater.scale < heldNow.scale - 0.05 && heldLater.scale > 0,
        `${heldNow.scale} then ${heldLater.scale}, first read ${heldNow.at} ms after Enter`);
      check(`${width}: its seconds count down`, secs(heldNow) >= 2 && secs(heldNow) <= 6 && secs(heldLater) < secs(heldNow) && secs(heldLater) >= 1,
        `${heldNow.secs} then ${heldLater.secs}`);
      check(`${width}: nothing sent while Undo is on offer`, posts.length === 0, posts.length);
      await page.waitForFunction(() => !document.querySelector("#sidebar .undo-add"), null, { timeout: 15000 });
      const head = await page.locator("#sidebar .sidebar-group.busy > summary").innerText();
      check(`${width}: then the heading reads Busy and its count, still folded`, /^Busy\s*\d+$/.test(head.trim()) && !(await busyOpen()), JSON.stringify(head));
      // Playwright reports the request a moment after the page sent it.
      for (let i = 0; i < 50 && posts.length === 0; i++) await sleep(100);
      check(`${width}: the Add goes out after the Undo time`, posts.length === 1 && posts.at(0) - pressed >= 5500, JSON.stringify(posts.map((at) => at - pressed)));
      if (width === 1280) {
        for (let i = 0; i < 3; i++) { await page.evaluate(() => refresh()); await sleep(700); }
      }
      await page.screenshot({ path: path.join(work, `add-arrives-${width}.png`) });
      // Wait until the server's item is drawn, then a poll or two more.
      await page.waitForFunction((title) => [...document.querySelectorAll("#sidebar li.side-item")].some((li) => li.textContent.includes(title) && /^\d+$/.test(li.dataset.item)), title, { timeout: 120000 });
      await page.evaluate(() => refresh());
      await sleep(3000);
      console.log("  item", JSON.stringify(await page.evaluate((title) => drawn(ui.state).items.filter((one) => one.title === title)
        .map((one) => ({ id: one.id, group: one.group, ball: one.ball, tip: one.tip, sessions: one.sessions, open_steps: one.open_steps })), title)));
      const seen = await page.evaluate(() => ({ ...window.__seen, group: [...window.__seen.group], firstGroup: [...window.__seen.firstGroup], arrived: ui.arrived }));
      console.log("  seen", JSON.stringify(seen));
      check(`${width}: pulsing once it is sent`, seen.litReal, "never marked fresh");
      check(`${width}: never listed twice`, seen.maxRows === 1, seen.maxRows);
      check(`${width}: always in Busy`, seen.group.length > 0 && seen.group.every((g) => /\bbusy\b/.test(g)), seen.group.join(","));
      check(`${width}: no Just added group`, !seen.justAdded, "Just added shown");
      check(`${width}: held row seen by the watcher`, seen.held, "never held");
      check(`${width}: ${width === 390 ? "no flight with reduced motion" : "the Add box flies into the row"}`, seen.flight === (width !== 390), String(seen.flight));
      check(`${width}: Busy's heading in view`, seen.inView, "never in view");
      check(`${width}: Busy never opened by the Add`, !seen.opened && !(await busyOpen()), "opened");
      const end = await page.locator("#sidebar .sidebar-group.busy > summary")
        .evaluate((el) => { const box = el.getBoundingClientRect(); return { top: box.top, bottom: box.bottom, inView: window.__inView(box) }; });
      check(`${width}: Busy's heading still in view at the end`, end.inView, JSON.stringify(end));
      // The user opens Busy: the new to-do leads it.
      await page.locator("#sidebar .sidebar-group.busy > summary").click();
      const fresh = await page.locator("#sidebar li.side-item", { hasText: title }).first().evaluate((li) => ({
        fresh: li.classList.contains("fresh"), first: li.parentElement.firstElementChild === li, group: li.closest("details").className, open: li.closest("details").open,
        pulse: getComputedStyle(li.querySelector(".side-row")).animationName }));
      check(`${width}: still first in Busy, pulsing, seconds later`, fresh.fresh && fresh.first && /\bbusy\b/.test(fresh.group)
        && fresh.pulse === (width === 390 ? "none" : "fresh-pulse"), JSON.stringify(fresh));
      await page.locator("#sidebar li.side-item.fresh", { hasText: title }).first().screenshot({ path: path.join(work, `add-fresh-${width}.png`) });
      const row = page.locator("#sidebar li.side-item", { hasText: title });
      await row.locator(".side-row").click();
      // The card heads with the to-do's text until its session puts a step, which then heads it.
      await page.waitForFunction((title) => (document.querySelector("#next")?.textContent || "").includes(title), title, { timeout: 60000 }).catch(() => {});
      const card = await page.locator("#next").innerText().catch(() => "");
      check(`${width}: click opens it on the card`, card.includes(title), card);
      // Undo takes a to-do back into the Add box, with its words, and nothing reaches the server.
      const undone = `Undo me ${width}`;
      const before = posts.length;
      // On a phone the click above showed the item screen, which hides the list: the bar's Your items lights up and
      // offers the Undo at its right (UI-24), with no notice.
      await page.locator(width === 390 ? ".phone-add" : ".sidebar-add").click();
      await page.locator('#add-box textarea[data-key="add:new"]').fill(undone);
      await page.keyboard.press("Enter");
      if (width === 390) {
        const barUndo = page.locator(".phone-items.holding > .busy-undo");
        await barUndo.waitFor();
        const bar = await page.evaluate(() => ({ name: document.querySelector(".phone-items > .phone-back").innerText.trim(),
          close: document.querySelector(".phone-items").classList.contains("close"), toast: !document.getElementById("toast").hidden }));
        check(`${width}: Your items keeps its name and holds the Undo, with no notice`, /^Your items/.test(bar.name) && !bar.close && !bar.toast, JSON.stringify(bar));
        await barUndo.click();
      } else {
        // Busy is open now: the Undo is the row in the list, and the heading shows its name.
        await page.locator("#sidebar li.side-item.held", { hasText: undone }).waitFor();
        const shown = await page.evaluate(() => ({ open: document.querySelector("#sidebar .sidebar-group.busy").open,
          headingUndo: [...document.querySelectorAll("#sidebar .busy-undo")].some((b) => getComputedStyle(b).display !== "none"),
          // The heading reads Busy and one count, as always.
          head: document.querySelector("#sidebar .sidebar-group.busy > summary").innerText.trim(),
          oldCount: [...document.querySelectorAll("#sidebar .count-was")].some((one) => getComputedStyle(one).display !== "none") }));
        check(`${width}: with Busy open the row is the Undo, not the heading`, shown.open && !shown.headingUndo && !shown.oldCount && /^Busy\s*\d+$/.test(shown.head), JSON.stringify(shown));
        await pressIn(page, "#sidebar li.side-item.held button.undo-add", undone);
      }
      const back = await page.locator('#add-box textarea[data-key="add:new"]').inputValue().catch(() => "");
      check(`${width}: Undo opens the Add box with the words`, await page.locator("#add-box").isVisible() && back === undone, back);
      // A removed row fades out where it was (motion.js), so the row may show for that moment.
      const rowGone = await page.waitForFunction((undone) => ![...document.querySelectorAll("#sidebar li.side-item")].some((li) => li.textContent.includes(undone)),
        undone, { timeout: 2000 }).then(() => true, () => false);
      check(`${width}: Undo removes the row`, rowGone, "row stays");
      await sleep(7000);
      check(`${width}: Undo sends nothing`, posts.length === before, posts.length - before);
      await page.keyboard.press("Escape");
      if (width === 390) {
        // Undo while the Add box is open on another to-do's Add more: the words go back to a new to-do.
        const moved = `Undo into a new to-do ${width}`;
        await page.locator(".phone-add").click();
        await page.locator('#add-box textarea[data-key="add:new"]').fill(moved);
        await page.keyboard.press("Enter");
        const into = await page.evaluate(() => {
          const other = ui.state.items.find((item) => /^todo-/.test(item.key) && typeof item.id === "number");
          openAddMore(other);
          document.querySelector(".phone-items.holding > .busy-undo").click();
          return { addTo: ui.addTo, words: document.querySelector('#add-box textarea[data-key="add:new"]')?.value };
        });
        check(`${width}: Undo during another to-do's Add more goes back to a new to-do`, into.addTo === null && into.words === moved, JSON.stringify(into));
        await page.keyboard.press("Escape");
        await page.evaluate(() => { ui.drafts.delete(ADD_KEY); ui.addImages = []; });
        // The page goes to the background: the Add goes out at once, and Your items no longer offers its Undo.
        const hidden = `Sent on hiding ${width}`;
        const sentBefore = posts.length;
        await page.locator(".phone-add").click();
        await page.locator('#add-box textarea[data-key="add:new"]').fill(hidden);
        await page.keyboard.press("Enter");
        await page.locator(".phone-items.holding > .busy-undo").waitFor();
        await page.evaluate(() => {
          Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
          document.dispatchEvent(new Event("visibilitychange"));
          delete document.hidden;
        });
        await sleep(1000);
        const held = await page.evaluate(() => ({ holding: document.querySelector(".phone-items").classList.contains("holding"),
          undo: Boolean(document.querySelector(".phone-items > .busy-undo")) }));
        check(`${width}: hiding the page sends the Add at once`, posts.length === sentBefore + 1, posts.length - sentBefore);
        check(`${width}: Your items then drops the Undo`, !held.holding && !held.undo, JSON.stringify(held));
      }
      if (width === 1280) {
        // Busy folded again: the heading's Undo takes the to-do back and Busy stays folded.
        await page.locator("#sidebar .sidebar-group.busy > summary").click();
        const third = `Undo from the heading ${width}`;
        const sentBefore = posts.length;
        await page.locator(".sidebar-add").click();
        await page.locator('#add-box textarea[data-key="add:new"]').fill(third);
        await page.keyboard.press("Enter");
        await page.locator(`#sidebar .busy-undo[aria-label*="${third}"]`).waitFor();
        // The fold arrow after the heading's Undo, at the heading's right end, still opens Busy, and its row is the Undo
        // there; folded again, the heading is the Undo again.
        const arrow = await page.locator("#sidebar .sidebar-group.busy > summary").evaluate((el) => { const box = el.getBoundingClientRect(); return { x: box.right - 23, y: box.top + box.height / 2 }; });
        await page.mouse.click(arrow.x, arrow.y);
        const opened = await page.evaluate((third) => ({ open: document.querySelector("#sidebar .sidebar-group.busy").open,
          row: [...document.querySelectorAll("#sidebar li.side-item.held button.undo-add")].some((b) => b.textContent.includes(third) && b.getBoundingClientRect().height > 0) }), third);
        check(`${width}: the fold arrow opens Busy while the heading is the Undo`, opened.open && opened.row, JSON.stringify(opened));
        await page.mouse.click(arrow.x, arrow.y);
        await pressIn(page, "#sidebar .busy-undo", third);
        const words = await page.locator('#add-box textarea[data-key="add:new"]').inputValue().catch(() => "");
        await sleep(7000);
        check(`${width}: the heading's Undo takes it back, Busy stays folded, nothing sent`,
          words === third && !(await busyOpen()) && posts.length === sentBefore && !(await page.locator("#sidebar .busy-undo").count()), JSON.stringify({ words, sent: posts.length - sentBefore }));
        await page.keyboard.press("Escape");
        await page.evaluate(() => { ui.drafts.delete(ADD_KEY); });
        // Two Adds of the same text: the first answers at once, the second's answer is held while polls bring both.
        const twin = "Book venue 1280";
        let adds = 0;
        await page.unroute("**/api/items/add");
        await page.route("**/api/items/add", async (route) => {
          const response = await route.fetch();
          if (++adds === 2) await sleep(4000);
          await route.fulfill({ response });
        });
        await page.evaluate((twin) => {
          window.__twins = { fewer: 0 };
          new MutationObserver(() => {
            const rows = [...document.querySelectorAll("#sidebar li.side-item")].filter((li) => li.textContent.includes(twin));
            if (window.__twins.both && rows.length < 2) window.__twins.fewer++;
            if (rows.length === 2) window.__twins.both = true;
          }).observe(document.getElementById("sidebar"), { childList: true, subtree: true });
        }, twin);
        for (let i = 0; i < 2; i++) {
          await page.locator(".sidebar-add").click();
          await page.locator('#add-box textarea[data-key="add:new"]').fill(twin);
          await page.keyboard.press("Enter");
        }
        for (let i = 0; i < 12; i++) { await sleep(800); await page.evaluate(() => refresh()); }
        await page.waitForFunction((twin) => [...document.querySelectorAll("#sidebar li.side-item")]
          .filter((li) => li.textContent.includes(twin) && /^\d+$/.test(li.dataset.item)).length === 2, twin, { timeout: 90000 }).catch(() => {});
        await page.evaluate(() => refresh());
        await sleep(2000);
        const twins = await page.evaluate((twin) => ({ ...window.__twins,
          end: [...document.querySelectorAll("#sidebar li.side-item")].filter((li) => li.textContent.includes(twin)).map((li) => li.dataset.item) }), twin);
        check(`${width}: two same Adds, two rows throughout`, twins.both && twins.fewer === 0 && twins.end.length === 2 && twins.end.every((id) => /^\d+$/.test(id)), JSON.stringify(twins));
      }
      check(`${width}: no page errors`, errors.length === 0, errors.join(" | "));
      await ctx.close();
    }
    console.error(`screenshots in ${work}`);
  } finally {
    if (browser) await browser.close();
    server.kill();
    await new Promise((done) => (server.exitCode !== null || server.signalCode !== null ? done() : server.once("exit", done)));
    // Clear temporary records while keeping the screenshots.
    for (const name of fs.readdirSync(work)) if (!name.endsWith(".png")) fs.rmSync(path.join(work, name), { recursive: true, force: true });
  }
  process.exitCode = fails.length ? 1 : 0;
})().catch((e) => { console.error(e); process.exitCode = 1; });
