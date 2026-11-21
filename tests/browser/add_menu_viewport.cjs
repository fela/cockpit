// Add menus follow the available viewport, keeping their details ancestry and final options reachable.
// Uses synthetic records only. Pass an old static directory to prove the regression without replacing source.
// PLAYWRIGHT_MODULE=/tmp/cockpit-browser-tests/node_modules/playwright node tests/browser/add_menu_viewport.cjs [old-static-dir]
"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");
const { startPreview, fullState } = require("./work.cjs");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const oldStatic = process.argv[2];
const fields = ["priority", "provider", "model", "effort", "speed", "account", "ways"];
const select = (field) => field === "ways" ? "#add-box .add-ways" : `#add-box .start-pick:has(> summary[data-key="add:new:${field}"])`;
const settle = (page) => page.evaluate(async () => {
  await Promise.all(document.getElementById("add-box").getAnimations({ subtree: true }).map((animation) => animation.finished.catch(() => {})));
  await new Promise(requestAnimationFrame);
  await new Promise(requestAnimationFrame);
});

// Plenty of real entries and a long label, offered through the synthetic state response on every poll.
function offers(state) {
  const speeds = [{ name: "standard", label: "Standard", hint: "default" }, { name: "fast", label: "Fast", hint: "faster" }];
  const models = Array.from({ length: 36 }, (_, i) => ({ name: `fixture-${i}`, label: i === 35 ? "Final model with a deliberately long descriptive name that must wrap within the viewport" : `Model ${i + 1}`, speeds }));
  const choices = { models, efforts: ["low", "medium", "high", "xhigh", "max"], effort: "high" };
  state.default_provider = "claude";
  state.session_options = { claude: choices, codex: choices };
  state.session_accounts = Array.from({ length: 36 }, (_, i) => ({ name: `Account ${i + 1}`, letter: "A" }));
  return state;
}

async function fits(page, field, direction) {
  const menu = page.locator(select(field));
  const geometry = await menu.evaluate((el) => {
    const list = el.querySelector(":scope > .menu-list");
    const r = list.getBoundingClientRect();
    const a = el.querySelector("summary").getBoundingClientRect();
    const p = el.closest(".add-panel").getBoundingClientRect();
    const v = visualViewport;
    const box = ({ left, top, right, bottom, width, height }) => ({ left, top, right, bottom, width, height });
    return { list: box(r), anchor: box(a), panel: box(p), view: { left: v.offsetLeft, top: v.offsetTop, right: v.offsetLeft + v.width, bottom: v.offsetTop + v.height },
      scrollHeight: list.scrollHeight, clientHeight: list.clientHeight, parent: list.parentElement === el };
  });
  const { list: r, view: v, anchor: a } = geometry;
  assert(geometry.parent, "menu keeps its details ancestry");
  assert(r.width > 0 && r.height > 0 && r.left >= v.left + 7 && r.right <= v.right - 7 && r.top >= v.top + 7 && r.bottom <= v.bottom - 7,
    `${field} fits viewport: ${JSON.stringify(geometry)}`);
  if (direction === "up") assert(r.bottom <= a.top - 5, `${field} opens up: ${JSON.stringify(geometry)}`);
  if (direction === "down") assert(r.top >= a.bottom + 5, `${field} opens down: ${JSON.stringify(geometry)}`);
  // Hit testing catches overflow clipping that a bounding rectangle alone would miss.
  const last = menu.locator(".menu-list button").last();
  await last.scrollIntoViewIfNeeded();
  await settle(page);
  assert(await last.evaluate((el) => {
    const r = el.getBoundingClientRect();
    const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return el === top || el.contains(top);
  }), `${field}: final option receives pointer events`);
  return geometry;
}

async function open(page, field) {
  // Close any previous list using its own keyboard handler.
  const existing = page.locator("#add-box details.menu[open] > summary");
  for (const summary of await existing.all()) { await summary.focus(); await page.keyboard.press("Escape"); }
  await page.evaluate(() => {
    const panel = document.querySelector("#add-box .add-panel");
    if (!panel.dataset.testPosition) return;
    Pick.dock(panel, null, 0);
    panel.style.position = "fixed";
    panel.style.left = `${innerWidth - panel.offsetWidth - 16}px`;
    panel.style.top = panel.dataset.testPosition === "bottom" ? `${innerHeight - panel.offsetHeight - 16}px` : "16px";
    panel.style.overflow = "auto";
  });
  await page.locator(`${select(field)} > summary`).click();
  await settle(page);
}

async function pickLast(page, field) {
  const menu = page.locator(select(field));
  const last = menu.locator(".menu-list button").last();
  if (field === "ways") {
    // Leave feedback instead is a local state transition; no item is sent.
    await last.click();
    assert(await page.evaluate(() => !ui.adding && ui.picking === "feedback"));
    await page.keyboard.press("Escape");
    return;
  }
  const value = await last.getAttribute(field === "priority" ? "data-priority" : "data-value");
  await last.click();
  assert(await menu.locator(".menu-list").isHidden(), `${field}: selection closes its list`);
  assert(await page.evaluate(({ field, value }) => field === "priority" ? ui.addPriority === value : ui.starts.get("add:new")[field] === value, { field, value }), `${field}: clicked final option stages selection`);
  assert(await page.locator(`${select(field)} > summary`).evaluate((el) => document.activeElement === el), `${field}: selection restores focus`);
}

async function main() {
  const { work, info } = await startPreview("cockpit-add-menu-viewport-");
  console.log(`work: ${work}`);
  const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE || undefined });
  let checks = 0;
  try {
    for (const scenario of [
      { name: "desktop-top", width: 1280, height: 900, position: "top", direction: "down" },
      { name: "desktop-bottom-right", width: 1280, height: 900, position: "bottom", direction: "up" },
      { name: "desktop-short", width: 1000, height: 420, position: "bottom", direction: "up" },
      { name: "phone", width: 390, height: 844 },
      { name: "phone-short", width: 390, height: 420 },
      { name: "modal", width: 1280, height: 900, modal: true },
    ].filter((scenario) => !process.env.ADD_MENU_SCENARIO || scenario.name === process.env.ADD_MENU_SCENARIO)) {
      const context = await browser.newContext({ viewport: { width: scenario.width, height: scenario.height },
        isMobile: scenario.width < 640, hasTouch: scenario.width < 640, colorScheme: scenario.width < 640 ? "light" : "dark" });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.setDefaultTimeout(30000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      // All requests stay on the local fixture; no live writes or external sends.
      await page.route("**/*", async (route) => {
        assert.equal(new URL(route.request().url()).origin, info.origin, "fixture requests stay local");
        assert(!/\/api\/(items\/add|messages)$/.test(new URL(route.request().url()).pathname), "menu choices stage without sending an item or message");
        await route.continue();
      });
      if (oldStatic) {
        for (const file of ["app.js", "style.css"]) await page.route(`**/static/${file}*`, (route) => route.fulfill({ path: path.join(oldStatic, file), contentType: file.endsWith("js") ? "application/javascript" : "text/css" }));
      }
      await page.route("**/api/state*", async (route) => {
        const { response, headers } = await fullState(route);
        await route.fulfill({ response, headers, json: offers(await response.json()) });
      });
      await page.addLocatorHandler(page.locator("dialog#idle[open]"), (dialog) => dialog.getByRole("button", { name: "Return", exact: true }).click());
      await page.goto(info.origin);
      await page.waitForFunction(() => ui.state?.session_options?.claude?.models.length === 36);
      if (scenario.modal) await page.locator(".sidebar-add:visible").click();
      else {
        if (scenario.width < 640) await page.evaluate(() => setPhoneScreen(true, false));
        if (await page.locator("#feedback-mode").isVisible()) await page.locator("#feedback-mode").click();
        else await page.keyboard.press("f");
        await page.locator(scenario.width < 640 ? "#sidebar .side-row" : "#next h1").first().click();
      }
      await page.locator("#add-box textarea").fill("Menu regression draft");
      await settle(page);
      if (scenario.position) {
        // The production docking path is exercised above. Pin its resulting panel to deterministic edges
        // for all seven menus, independent of fixture item height or surrounding machine's fonts.
        await page.evaluate((position) => {
          const panel = document.querySelector("#add-box .add-panel");
          panel.dataset.testPosition = position;
        }, scenario.position);
      }
      // Keep Claude for the account test; provider is picked separately at the end.
      for (const field of fields.filter((field) => field !== "provider" && field !== "ways")) {
        await open(page, field);
        const before = await fits(page, field, scenario.direction);
        if (field === "model" || field === "account") assert(before.scrollHeight > before.clientHeight, `${field}: long list scrolls`);
        // Sample across frames: opening/scrolling the list must not resize or move its panel.
        const stable = await page.locator("#add-box .add-panel").evaluate(async (panel) => {
          const boxes = [];
          for (let i = 0; i < 12; i++) { await new Promise(requestAnimationFrame); const r = panel.getBoundingClientRect(); boxes.push([r.x, r.y, r.width, r.height]); }
          return boxes.every((r) => r.every((n, i) => Math.abs(n - boxes[0][i]) < 1));
        });
        assert(stable, `${field}: panel stays still while menu is open`);
        if (field === "account") await page.screenshot({ path: path.join(work, `${scenario.name}.png`) });
        // Keyboard focus stays within Add, and Escape closes only the menu.
        await page.keyboard.press("Tab");
        assert(await page.evaluate(() => document.getElementById("add-box").contains(document.activeElement)), "Tab stays in Add");
        await page.locator(`${select(field)} .menu-list button`).last().focus();
        await page.keyboard.press("Escape");
        assert(await page.locator(`${select(field)} .menu-list`).isHidden(), "Escape closes menu");
        assert(await page.locator("#add-box textarea").isVisible(), "Escape leaves Add open");
        assert(await page.locator(`${select(field)} > summary`).evaluate((el) => el === document.activeElement), "Escape restores summary focus");
        await open(page, field);
        await fits(page, field, scenario.direction);
        await pickLast(page, field);
        checks++;
      }
      const staged = await page.evaluate(() => JSON.stringify([ui.addPriority, ui.starts.get("add:new"), ui.drafts.get("add:new")]));
      await page.locator("#add-box .pin-key").click();
      assert.equal(await page.evaluate(() => JSON.stringify([ui.addPriority, ui.starts.get("add:new"), ui.drafts.get("add:new")])), staged,
        "redrawing Add preserves staged picks and draft");
      await open(page, "provider");
      await fits(page, "provider", scenario.direction);
      await pickLast(page, "provider");
      assert(await page.evaluate(() => { const p = ui.starts.get("add:new"); return p.provider === "codex" && p.model === null && p.account === null; }), "provider resets dependent selections");
      checks++;

      // The same open menu follows actual scrolling, resize, and a docking move without becoming detached.
      await open(page, "model");
      await page.evaluate(() => renderAddBox());
      await settle(page);
      await fits(page, "model");
      await page.evaluate(() => { document.querySelector("#add-box .add-panel").style.top = "40px"; });
      await page.setViewportSize({ width: scenario.width, height: scenario.height - 70 });
      await settle(page);
      await fits(page, "model");
      await page.evaluate(() => {
        const box = document.getElementById("add-box");
        const spacer = document.createElement("div");
        spacer.style.cssText = "position:absolute;top:1800px;width:1px;height:1px";
        box.append(spacer);
        box.scrollTop = 35;
        const panel = box.querySelector(".add-panel");
        panel.style.maxHeight = "230px";
        panel.style.overflow = "auto";
        panel.scrollTop = 30;
      });
      await settle(page);
      await fits(page, "model");
      checks++;
      await open(page, "ways");
      await fits(page, "ways");
      await pickLast(page, "ways");
      checks++;
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "no sideways page scroll");
      assert.equal(errors.length, 0, errors.join("\n"));
      console.log(`PASS ${scenario.name}: all seven menus, final-option clicks, focus, Escape, scroll/resize, steady panel`);
      await page.unrouteAll({ behavior: "ignoreErrors" });
      await context.close();
    }
    console.log(`PASS ${checks} menu/scenario checks; screenshots in ${work}`);
  } catch (error) {
    console.error(error);
    throw error;
  } finally {
    for (const context of browser.contexts()) for (const page of context.pages()) await page.unrouteAll({ behavior: "ignoreErrors" });
    await browser.close();
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
