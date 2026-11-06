// Rendered group marks, responsive session slots, the classic Add key and Usage heading marks.
// Run with the browser dependencies selected through PLAYWRIGHT_MODULE and BROWSER_EXECUTABLE.
"use strict";

const { startPreview } = require("./work.cjs");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const SESSION = "22222222-2222-4222-8222-222222222222";

async function main() {
  const { info } = await startPreview("cockpit-marks-");
  const headers = { cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json" };
  const agent = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "marks-test", "content-type": "application/json" };
  const call = async (method, url, body, who) => {
    const response = await fetch(info.origin + url, { method, headers: who, body: JSON.stringify(body) });
    if (!response.ok) throw new Error(`${method} ${url}: ${response.status} ${await response.text()}`);
    return response.json();
  };
  const item = (key, extra) => call("PUT", "/api/items", { key: `note:marks-${key}`, title: `Tasks ${key}`, project: "tasks", ...extra }, agent);
  const linked = await item("linked", { stage: "building", sessions: [SESSION], waiting_on: [{ kind: "agent", who: SESSION }] });
  const empty = await item("empty", { stage: "building" });
  const finished = await item("finished", { stage: "building" });
  await call("PATCH", `/api/items/${finished.id}`, { archived: true }, headers);
  await call("PUT", "/api/in-progress", { entries: [{ item_id: linked.id, line: "Prepare the next task" }] }, agent);
  for (let at = 0; at < 7; at++) {
    const task = await item(`queued-${at}`, {});
    const { step } = await call("POST", "/api/steps", {
      item_id: task.id, need: "review", ask: "Review this task?", score: 90 - at, session_id: SESSION,
    }, agent);
    if (at === 6) await call("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" }, headers);
  }
  const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE });
  const failures = [];
  const check = (name, ok, detail) => {
    console.log(`${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${JSON.stringify(detail)}`}`);
    if (!ok) failures.push(name);
  };
  // The mask comes from the page's loaded stylesheet and its icon token, on the actual rendered descendant.
  const glyph = async (page, selector, pseudo, mark, width, where) => {
    const node = page.locator(selector);
    if (await node.count() !== 1) return check(`${where}: ${selector} has its ${mark} mark`, false, `found ${await node.count()} elements`);
    const style = await node.evaluate((el, { pseudo, mark }) => {
      const s = getComputedStyle(el, pseudo);
      return { visible: el.checkVisibility(), content: s.content, width: s.width, height: s.height,
        mask: s.maskImage, token: s.getPropertyValue(`--mark-${mark}`).trim(), background: s.backgroundColor, order: s.order };
    }, { pseudo, mark });
    check(`${where}: ${selector} has its ${mark} mark`, style.visible && style.content === '""'
      && style.width === `${width}px` && style.height === "16px" && style.token.startsWith("url(")
      && style.mask === style.token && style.background !== "rgba(0, 0, 0, 0)"
      && (pseudo !== "::after" || style.order === "-1"), style);
  };
  try {
    for (const scheme of ["light", "dark"]) {
      const context = await browser.newContext({ colorScheme: scheme, reducedMotion: "reduce",
        viewport: { width: 641, height: 900 }, extraHTTPHeaders: headers });
      const page = await context.newPage();
      page.setDefaultTimeout(30e3);
      page.setDefaultNavigationTimeout(60e3);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${info.origin}/classic`);
      await page.locator("#work .group.in-progress > summary").waitFor();
      for (const [selector, mark] of [
        ["#queue .group.ready-next > summary", "layers"], ["#queue .group.snoozed > summary", "clock"],
        ["#work .group.in-progress > summary", "radar"], ["#work .group.done > summary", "flag"],
      ]) await glyph(page, selector, "::after", mark, 16, scheme);
      await page.locator("#work .group.waiting > summary").click();
      for (const id of [linked.id, empty.id]) await page.locator(`#work [data-item="${id}"] > .row-head`).click();
      for (const width of [641, 640]) {
        await page.setViewportSize({ width, height: 900 });
        const where = `${scheme}, ${width}px`;
        const add = await page.locator("#add").evaluate((el) => {
          const s = getComputedStyle(el), icon = el.querySelector(".icon"), box = el.getBoundingClientRect(), ib = icon.getBoundingClientRect();
          const probe = document.createElement("span");
          probe.style.color = "var(--accent)";
          el.append(probe);
          const accent = getComputedStyle(probe).color;
          probe.remove();
          return { visible: el.checkVisibility(), width: box.width, height: box.height, iconWidth: ib.width, iconHeight: ib.height,
            centered: Math.abs((ib.left + ib.right) - (box.left + box.right)) < 1,
            padding: s.padding, justify: s.justifyContent, background: s.backgroundColor, accent };
        });
        check(`${where}: Add keeps its size, centered plus and accent background`, add.visible && add.width === 32
          && add.height === (width <= 640 ? 32 : 28) && add.iconWidth === 16 && add.iconHeight === 16
          && add.centered && add.padding === "0px" && add.justify === "center" && add.background === add.accent, add);
        for (const id of [linked.id, empty.id]) {
          await page.locator(`#work [data-item="${id}"] > .row-head .stage-slot .stage`).waitFor();
          const slot = await page.locator(`#work [data-item="${id}"] > .row-head .row-side`).evaluate((el) => {
            const dots = el.querySelector(".dots"), age = el.querySelector(".row-age"), stage = el.querySelector(".stage-slot .stage");
            const ds = getComputedStyle(dots), as = getComputedStyle(age), rs = getComputedStyle(el), br = getComputedStyle(el, "::before");
            return { visible: el.checkVisibility(), dots: dots.children.length, display: ds.display, width: dots.getBoundingClientRect().width,
              flex: ds.flexGrow, shrink: ds.flexShrink, justify: ds.justifyContent, order: ds.order, ageBasis: as.flexBasis,
              rowGap: rs.rowGap, breakContent: br.content, breakOrder: br.order, breakBasis: br.flexBasis, breakHeight: br.height,
              dotsBox: dots.getBoundingClientRect().toJSON(), ageBox: age.getBoundingClientRect().toJSON(),
              slotWidth: el.querySelector(".stage-slot").getBoundingClientRect().width, stageBox: stage?.getBoundingClientRect().toJSON() };
          });
          check(`${where}: task ${id} renders the expected session count`, slot.visible && slot.dots === (id === linked.id ? 1 : 0), slot);
          if (width > 640) {
            check(`${where}: task ${id} reserves a 26px dot slot beside the stage`, slot.display === "flex" && slot.width === 26
              && slot.flex === "0" && slot.shrink === "0" && slot.justify === "flex-end"
              && slot.slotWidth === 132 && slot.stageBox?.right <= slot.dotsBox.left, slot);
          } else {
            check(`${where}: task ${id} puts dots and age above the stage`, slot.order === "-3" && slot.ageBasis === "auto"
              && slot.rowGap === "0px" && slot.breakContent === '""' && slot.breakOrder === "-1"
              && slot.breakBasis === "100%" && slot.breakHeight === "4px"
              && slot.stageBox?.top >= slot.ageBox.bottom
              && (id === empty.id || slot.dotsBox.bottom <= slot.stageBox.top), slot);
          }
        }
      }
      await page.goto(`${info.origin}/classic#find/tasks`);
      await page.locator("#work .group.you > summary").waitFor();
      await glyph(page, "#work .group.you > summary", "::after", "person", 16, scheme);
      await page.goto(`${info.origin}/usage`);
      await page.locator("#spend-limits .section-head h2").waitFor();
      await page.locator("#spend-more > summary").click();
      for (const [section, mark] of Object.entries({ limits: "clock", daily: "flag", where: "chart", misses: "hourglass",
        models: "layers", sessions: "bot", skills: "key", origins: "mail", "session-kinds": "people", top: "radar", codex: "bot" })) {
        await glyph(page, `#spend-${section} .section-head h2`, "::before", mark, section === "sessions" ? 8 : 16, scheme);
      }
      check(`${scheme}: no page errors`, errors.length === 0, errors);
      await context.close();
    }
  } finally {
    await browser.close();
  }
  console.log(`${failures.length} check(s) failed`);
  process.exitCode = failures.length ? 1 : 0;
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
