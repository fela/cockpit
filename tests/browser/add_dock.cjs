// The Add box with a part attached, reached the way the user reaches it (the Feedback key at the rail's top, then
// a click on a part): the page stays as bright as it was, the part keeps an outline, the panel stands beside the
// part without covering it, and it shows a preview of the part. Wider moves the outline with the panel following;
// removing the attachment, or closing the box, takes the outline away and puts the panel back in its own place.
// At desktop width, the box also shows over a view open in the center layer (Usage). Checks at desktop and phone
// width, and saves a screenshot of each in the work dir.
//
// Usage, from the checkout: node tests/browser/add_dock.cjs   (it starts serve_preview.py)

"use strict";

const path = require("node:path");
const { startPreview } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const { chromium } = require(PLAYWRIGHT);

let failures = 0;
function check(ok, words, detail) {
  console.log(`${ok ? "ok  " : "FAIL"} ${words}${ok ? "" : `: ${detail}`}`);
  if (!ok) failures += 1;
}

const wait = (ms) => new Promise((done) => setTimeout(done, ms));

// What the page shows of the Add box and the attached part's outline, against the attached part itself.
const looks = (page) => page.evaluate(() => {
  const box = document.getElementById("add-box");
  const panel = box.querySelector(".add-panel");
  const mark = document.querySelector(".pick-mark.attached");
  const rect = (el) => { const r = el.getBoundingClientRect(); return { x: r.left, y: r.top, r: r.right, b: r.bottom }; };
  const part = ui.addPart && ui.addPart.el;
  return {
    open: !box.hidden,
    ground: getComputedStyle(box).backgroundColor,
    docked: Boolean(panel && panel.classList.contains("docked")),
    panel: panel && rect(panel),
    part: part && rect(part),
    mark: mark && !mark.hidden && getComputedStyle(mark).display !== "none" ? rect(mark) : null,
    preview: Boolean(panel && panel.querySelector(".add-part .pick-preview, .add-part .pick-quote")),
    target: ui.addPart && ui.addPart.target,
  };
});

const overlaps = (a, b) => a.x < b.r && b.x < a.r && a.y < b.b && b.y < a.b;
// How much of `b` the box `a` covers, as a share of b's area.
const covers = (a, b) => (Math.max(0, Math.min(a.r, b.r) - Math.max(a.x, b.x)) * Math.max(0, Math.min(a.b, b.b) - Math.max(a.y, b.y)))
  / ((b.r - b.x) * (b.b - b.y));
const near = (a, b) => ["x", "y", "r", "b"].every((side) => Math.abs(a[side] - b[side]) < 12);

(async () => {
  const { work, info } = await startPreview("cockpit-add-dock-");
  const browser = await chromium.launch({ executablePath: CHROMIUM });
  for (const [width, height] of [[1280, 900], [390, 844]]) {
    const context = await browser.newContext({ viewport: { width, height } });
    await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
    const page = await context.newPage();
    // The machines these run on can be slow; a step waits for what it needs rather than a fixed short time.
    page.setDefaultTimeout(60e3);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`${info.origin}/`, { timeout: 120e3 });
    await page.locator(".rail-actions #feedback-mode").waitFor({ state: "attached" });
    const phone = width < 640;
    // On a phone the rail is the list screen; the part to pick is a row there.
    if (phone) await page.evaluate(() => setPhoneScreen(true, false));
    const target = phone ? "#sidebar .side-row" : "#next h1";
    await page.locator(target).first().waitFor();

    await page.locator("#feedback-mode").click();
    await page.locator(target).first().click();
    await page.locator('#add-box textarea[data-key="add:new"]').waitFor();
    await wait(600);
    let now = await looks(page);
    check(now.open && now.target, `${width} px: a pick opens Add with the part attached`, JSON.stringify(now));
    check(now.ground === "rgba(0, 0, 0, 0)", `${width} px: the page is not darkened behind the box`, now.ground);
    check(now.mark && now.part && near(now.mark, now.part), `${width} px: the attached part keeps its outline`, JSON.stringify(now));
    check(now.preview, `${width} px: the box shows a preview of the part`, JSON.stringify(now));
    if (!phone) {
      check(now.docked && !overlaps(now.panel, now.part), `${width} px: the panel stands beside the part, not over it`, JSON.stringify(now));
    }
    await page.screenshot({ path: path.join(work, `add-dock-${width}.png`) });

    // Wider: the outline goes to the larger part and the panel follows.
    const before = now.target;
    await page.locator("#add-box .add-part button", { hasText: "Wider" }).click();
    await wait(600);
    now = await looks(page);
    check(now.target && now.target !== before && now.mark && near(now.mark, now.part),
      `${width} px: Wider moves the outline to the larger part (${before} → ${now.target})`, JSON.stringify(now));
    // The whole card leaves no room beside it for the panel, which then takes the window's corner that covers least.
    if (!phone) check(now.docked && covers(now.panel, now.part) < 0.1, `${width} px: after Wider the panel stands beside the part or covers little of it`, JSON.stringify(now));
    // Where the panel does lie over the part, the outline and its label pass under the panel, never across it.
    const layers = await page.evaluate(() => [document.getElementById("add-box"), document.querySelector(".pick-mark.attached")]
      .map((el) => Number(getComputedStyle(el).zIndex)));
    check(layers[0] > layers[1], `${width} px: the panel shows over the part's outline`, JSON.stringify(layers));
    await page.screenshot({ path: path.join(work, `add-dock-wider-${width}.png`) });

    // Removing the attachment takes the outline away and puts the panel back in the middle, the page darkened again.
    await page.locator("#add-box").getByRole("button", { name: "Remove attachment" }).click();
    await wait(400);
    now = await looks(page);
    check(now.open && !now.target && !now.mark && !now.docked && now.ground !== "rgba(0, 0, 0, 0)",
      `${width} px: removing the attachment takes the outline away and puts the panel back`, JSON.stringify(now));

    // Picking again, then closing the box, leaves no outline behind.
    await page.keyboard.press("Escape");
    await wait(300);
    await page.locator("#feedback-mode").click();
    await page.locator(target).first().click();
    await page.locator('#add-box textarea[data-key="add:new"]').waitFor();
    await page.keyboard.press("Escape");
    await wait(400);
    now = await looks(page);
    const marks = await page.evaluate(() => [...document.querySelectorAll(".pick-mark")].filter((el) => !el.hidden && getComputedStyle(el).display !== "none").length);
    check(!now.open && marks === 0, `${width} px: closing the box leaves no outline`, JSON.stringify({ now, marks }));

    // With a view open in the center layer (Usage here), the box shows over it: the point at the panel's middle
    // belongs to the box, not to the view.
    if (!phone) {
      await page.evaluate(() => CockpitCenter.openHelper("/usage"));
      const heading = page.frameLocator("iframe.center-page").locator("#limits-title");
      await heading.waitFor();
      await page.locator("#feedback-mode").click();
      // While picking, a press on a framed page reaches Cockpit's page, which finds the part under it (pick.js).
      const spot = await heading.boundingBox();
      await page.mouse.click(spot.x + 20, spot.y + spot.height / 2);
      await page.locator('#add-box textarea[data-key="add:new"]').waitFor();
      await wait(600);
      const onTop = await page.evaluate(() => {
        const r = document.querySelector("#add-box .add-panel").getBoundingClientRect();
        const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        return top ? (top.closest("#add-box") ? "add-box" : top.className || top.tagName) : null;
      });
      check(onTop === "add-box", `${width} px: the box shows over a view in the center layer`, onTop);
      await page.screenshot({ path: path.join(work, `add-dock-center-${width}.png`) });
      await page.keyboard.press("Escape");
    }
    check(errors.length === 0, `${width} px: no page errors`, errors.join("; "));
    await context.close();
  }
  await browser.close();
  console.log(`screenshots in ${work}`);
  process.exit(failures ? 1 : 0);
})().catch((error) => { console.error(error); process.exit(1); });
