// Rendered marks on Search's groups, the responsive session slots on its rows, and Usage heading marks.
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
  // A task with a step for the user, so Search has its Waiting for you group.
  const queued = await item("queued", {});
  await call("POST", "/api/steps", { item_id: queued.id, need: "review", ask: "Review this task?", score: 90, session_id: SESSION }, agent);
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
      // Search for the words every task here holds: its groups, the finished task among them, and their rows.
      await page.goto(`${info.origin}/#find/tasks`);
      await page.locator("#work .group.you > summary").waitFor();
      for (const [selector, mark] of [["#work .group.you > summary", "person"], ["#work .group.done > summary", "flag"]]) {
        await glyph(page, selector, "::after", mark, 16, scheme);
      }
      for (const id of [linked.id, empty.id]) await page.locator(`#work [data-item="${id}"] > .row-head`).click();
      for (const width of [641, 640]) {
        await page.setViewportSize({ width, height: 900 });
        const where = `${scheme}, ${width}px`;
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
            // Chromium 158 reads `justify-content: flex-end` back as "flow-end".
            check(`${where}: task ${id} reserves a 26px dot slot beside the stage`, slot.display === "flex" && slot.width === 26
              && slot.flex === "0" && slot.shrink === "0" && ["flex-end", "flow-end"].includes(slot.justify)
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
