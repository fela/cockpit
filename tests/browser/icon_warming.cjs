// Fixture-free Chromium checks for icon isolation and upcoming-step figure warming.
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "../../src/cockpit/static");
const app = fs.readFileSync(path.join(root, "app.js"), "utf8");
function part(from, to) {
  const start = app.indexOf(from), end = app.indexOf(to, start + from.length);
  assert.ok(start >= 0 && end > start, `missing app.js boundary: ${from}`);
  return app.slice(start, end);
}

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.BROWSER_EXECUTABLE });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("http://cockpit.test/**", (route) => route.fulfill({
      contentType: "text/html",
      body: '<div id="next" style="width:640px"><div class="prose" data-step="1">Shown card</div></div>',
    }));
    await page.goto("http://cockpit.test/");
    await page.addScriptTag({ content: "window.iconErrors = []; window.logError = (label, error) => iconErrors.push([label, error.message]);" });
    await page.addScriptTag({ path: path.join(root, "icons.js") });
    const glyphs = await page.evaluate(() => {
      function check(ok, message) { if (!ok) throw new Error(message); }
      const style = document.documentElement.style;
      const names = [...style].filter((name) => name.startsWith("--mark-")).map((name) => name.slice(7));
      check(names.length > 50, "known glyphs were not loaded");
      for (const name of names) {
        // CSS masks expose the same static drawings, without relying on icon()'s cache.
        const mask = style.getPropertyValue(`--mark-${name}`);
        const markup = decodeURIComponent(mask.slice('url("data:image/svg+xml,'.length, -2));
        const holder = document.createElement("template");
        holder.innerHTML = markup.replace('<svg xmlns="http://www.w3.org/2000/svg"', `<svg class="icon icon-${name}"`)
          .replace("><", ' aria-hidden="true"><');
        const expected = holder.content.firstChild.outerHTML;
        const first = Icons.icon(name), second = Icons.icon(name);
        check(first.outerHTML === expected && second.outerHTML === expected, `${name}: serialized markup changed`);
        check(first !== second && first.parentNode === null && second.parentNode === null, `${name}: icons must be fresh and detached`);
        first.setAttribute("aria-hidden", "false");
        first.classList.add("caller");
        first.firstElementChild.setAttribute("data-caller", "changed");
        first.append(document.createElementNS("http://www.w3.org/2000/svg", "title"));
        document.body.append(first);
        second.replaceChildren();
        const third = Icons.icon(name);
        check(third.parentNode === null && third.outerHTML === expected, `${name}: caller mutation escaped into another icon`);
        first.remove();
      }
      for (const name of ["missing-glyph", "missing-glyph", "toString", "__proto__"]) {
        const missing = Icons.icon(name);
        check(missing.parentNode === null && missing.childNodes.length === 0
          && missing.getAttribute("aria-hidden") === "true" && missing.classList.contains(`icon-${name}`), "unknown icon output changed");
      }
      check(JSON.stringify(iconErrors) === JSON.stringify(["missing-glyph", "missing-glyph", "toString", "__proto__"]
        .map((name) => ["icons", `no icon named ${name}`])), "unknown icons must log on every call");
      return names.length;
    });
    console.log(`PASS ${glyphs} glyphs match mask markup and remain independent; unknown icons log per call`);

    await page.addScriptTag({ content: `
      const ui = { order: [999, 1, 2, 3, 4, 5], figureLinked: new Set() };
      const histories = [];
      function warmHistory(id) { histories.push(id); }
      ${part("function h(", "// Icons: icons.js")}
      ${part("function figureAddress(", "function fitFigure(")}
      ${part("function figureImages(", "// The item images a step's card")}
      ${part("const WARM_STEPS =", "// Server Markdown in a block.")}
    ` });
    await page.evaluate(() => {
      function check(ok, message) { if (!ok) throw new Error(message); }
      const step = (id, body_html, details = []) => ({ id, item_id: id * 10, body_html, details });
      const state = { queue: [step(1, "Shown"), step(2, "<p>Plain body</p>"),
        step(3, "", [{ body_html: "<p>Text-only detail</p>" }]),
        step(4, '<img class="figure-image" src="/item-images/42">'), step(5, "Outside warming limit")] };
      // Plain text and image-only steps must warm successfully without constructing a template.
      const create = document.createElement;
      document.createElement = function (tag, ...args) {
        check(tag.toLowerCase() !== "template", "figure-free text created a template");
        return create.call(this, tag, ...args);
      };
      try { warmNext(state); } finally { document.createElement = create; }
      check(figureWarm.isConnected && !figureWarm.children.length, "plain steps created frames");
      check(figureWarm.style.width === "640px", "warming width changed");
      check(JSON.stringify(histories) === "[20,30,40]", "history selection/order changed");
      const image = warmImages.get(42);
      check(image instanceof Image && image.getAttribute("src") === "/item-images/42", "image warming was skipped");
      warmNext(state);
      check(warmImages.get(42) === image, "unchanged image was replaced");

      const figure = (src) => `<figure class="figure"><iframe class="figure-frame" src="${src}" loading="lazy" sandbox="allow-scripts" title="Chart"></iframe></figure>`;
      state.queue[1].body_html = figure("/body?r=1");
      state.queue[2].details = [{ body_html: figure("/detail?r=1") }];
      warmNext(state);
      const frames = () => [...figureWarm.querySelectorAll("iframe.figure-frame")];
      check(JSON.stringify(frames().map(figureAddress)) === '["/body?r=1","/detail?r=1"]', "body/detail figures did not warm");
      const [body, detail] = frames();
      check(frames().every((frame) => !frame.hasAttribute("loading") && frame.title === "Chart"
        && frame.getAttribute("sandbox") === "allow-scripts" && frame.parentNode.tagName === "P"), "warmed frame attributes changed");
      warmNext(state);
      check(frames()[0] === body && frames()[1] === detail, "unchanged warmed frames were replaced");
      state.queue[1].body_html = figure("/body?r=2");
      state.queue[2].details = [{ body_html: figure("/detail?r=2") }];
      warmNext(state);
      check(!body.isConnected && !detail.isConnected, "obsolete revisions remained connected");
      check(JSON.stringify(frames().map(figureAddress)) === '["/body?r=2","/detail?r=2"]', "new revisions did not warm");

      state.queue[1].body_html = "<p>Body figure removed</p>";
      state.queue[2].details = [{ body_html: "<p>Detail figure removed</p>" }];
      state.queue[3].body_html = "<p>A figure-frame mentioned in text</p>";
      warmNext(state);
      check(!frames().length && !figureWarm.children.length, "figure removal or substring false positive left frames behind");
      check(warmImages.size === 0, "obsolete warmed image remained");
      state.queue = [];
      warmNext(state);
      check(!figureWarm.children.length && warmImages.size === 0, "empty queue retained warmed content");
    });
    console.log("PASS Plain bodies/details, image/history warming, body/detail frames, retained nodes, revisions and removal");
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
