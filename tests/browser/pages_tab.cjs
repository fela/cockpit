// The Pages tab lists every page sessions shared on an item, open or closed, and shows a chosen one across the whole
// width (SPEC API-4

"use strict";

const { execFileSync } = require("node:child_process");
const path = require("node:path");
const { startPreview } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const { work, info } = await startPreview("cockpit-pages-");
  // The copy's own pages go (their comments and choices with them), so the rows are this run's pages only and no
  // page the run did not write loads a resource the test cannot vouch for.
  execFileSync(PYTHON, ["-c",
    "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute('PRAGMA foreign_keys = ON'); c.execute('DELETE FROM item_pages'); c.commit()",
    path.join(work, "cockpit.db")]);
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const agent = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "pages-test", "content-type": "application/json" };
    const own = await (await fetch(`${info.origin}/api/items`, {
      method: "PUT", headers: agent, body: JSON.stringify({ key: `note:pages-test-${Date.now()}`, title: "Pages test" }),
    })).json();
    const addPage = async (title, html) => {
      const response = await fetch(`${info.origin}/api/items/${own.id}/pages`, { method: "POST", headers: agent, body: JSON.stringify({ title, html }) });
      if (!response.ok) throw new Error(`adding page ${title}: HTTP ${response.status}: ${await response.text()}`);
      return response.json();
    };
    // Four pages to start; the list puts the last added first.
    for (const [title, words] of [["Lists guide", "Create a grocery list in Tasks"], ["Reminders guide", "Choose a weekly reminder"],
      ["Sharing guide", "Invite Alex to a list"], ["Exports guide", "Save a list as CSV"]]) {
      await addPage(title, `<h1>${title}</h1><p>${words}</p>`);
    }
    const listed = (await (await fetch(`${info.origin}/api/pages`, { headers: { cookie: `${info.name}=${info.value}` } })).json()).pages;
    check("the list holds this run's four pages", listed.length === 4, `${listed.length} pages`);

    browser = await chromium.launch({ executablePath: CHROMIUM });
    const problems = [];
    const open = async (width, height, scheme) => {
      const context = await browser.newContext({ viewport: { width, height }, colorScheme: scheme });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.on("pageerror", (error) => problems.push(`${width} px: ${error.message}`));
      page.on("console", (message) => {
        if (message.type() === "error") problems.push(`${width} px, ${page.url()}: ${message.text()} ${message.location().url || ""}`);
      });
      return page;
    };

    // Desktop: the list alone, then a page across the whole width.
    const wide = await open(1280, 860, "light");
    await wide.goto(`${info.origin}/pages`);
    await wide.waitForSelector(".pages-row");
    check("one row per page", (await wide.locator(".pages-row").count()) === listed.length,
      `${await wide.locator(".pages-row").count()} rows for ${listed.length} pages`);
    const first = await wide.locator(".pages-row").first().getAttribute("data-page");
    check("the last changed page leads the list", Number(first) === listed[0].id, `row ${first}, newest ${listed[0].id}`);
    check("without a page named, the list shows alone", !(await wide.locator(".pages-preview").isVisible())
      && (await wide.locator(".pages-row[aria-current]").count()) === 0, "a page shows");
    await wide.screenshot({ path: path.join(work, "pages-1280-list.png") });

    // A poll that finds a new page keeps the keyboard on its row.
    await wide.locator(".pages-row").nth(2).focus();
    const focusedId = await wide.locator(".pages-row").nth(2).getAttribute("data-page");
    const added = await addPage("Pages test addition", "<p>new</p>");
    await wide.evaluate(() => refresh());
    await wide.waitForSelector(`.pages-row[data-page="${added.id}"]`);
    check("a poll keeps the keyboard on its row", await wide.evaluate(() => document.activeElement?.dataset.page) === focusedId, "focus moved");
    listed.unshift({ ...added, item_title: own.title });

    const firstRow = wide.locator(".pages-row").first();
    const firstId = await firstRow.getAttribute("data-page");
    await firstRow.click();
    const frameSrc = await wide.locator(".pages-frame").getAttribute("src");
    check("a click draws that page", frameSrc.startsWith(`/pages/${firstId}/figure`), frameSrc);
    check("the address names it", new URL(wide.url()).search === `?id=${firstId}`, wide.url());
    check("the list makes way for the page", !(await wide.locator(".pages-index").isVisible()), "list visible");
    const box = await wide.locator(".pages-frame").boundingBox();
    check("the page takes most of the screen", box.width >= 1280 * 0.95 && box.height >= 860 * 0.8,
      `${Math.round(box.width)} × ${Math.round(box.height)} of 1280 × 860`);
    check("no comment column until Comment is pressed", (await wide.locator(".pc-column").count()) === 0
      && (await wide.locator("button[aria-pressed]", { hasText: "Comment" }).getAttribute("aria-pressed")) === "false", "a column shows");
    const frame = wide.frameLocator(".pages-frame");
    await frame.locator("body").waitFor();
    check("the drawn page has content", (await frame.locator("body").innerText()).trim().length > 0, "empty body");
    await wide.screenshot({ path: path.join(work, "pages-1280-light.png") });

    // Comment shows the column beside the page; Hide comments takes it away.
    await wide.locator("button[aria-pressed]", { hasText: "Comment" }).click();
    await wide.waitForSelector(".pc-column .pc-head");
    const narrowed = await wide.locator(".pages-frame").boundingBox();
    check("Comment shows the column beside the page", narrowed.width < box.width - 200, `${Math.round(narrowed.width)} px wide`);
    check("the keyboard stays on the Comment button", await wide.evaluate(() => document.activeElement?.dataset.head) === "comment",
      await wide.evaluate(() => document.activeElement?.outerHTML.slice(0, 80)));
    await wide.locator("button[aria-pressed]", { hasText: "Hide comments" }).click();
    check("Hide comments takes the column away", (await wide.locator(".pc-column").count()) === 0, "column stays");

    // A poll that finds a new page keeps the frame being read.
    await wide.evaluate(() => { document.querySelector(".pages-frame").kept = true; });
    const later = await addPage("Pages test later", "<p>later</p>");
    await wide.evaluate(() => refresh());
    await wide.waitForSelector(`.pages-row[data-page="${later.id}"]`, { state: "attached" });
    check("a poll keeps the frame being read", await wide.evaluate(() => document.querySelector(".pages-frame").kept === true), "the frame was redrawn");
    listed.unshift({ ...later, item_title: own.title });

    await wide.locator(".pages-back").click();
    check("All pages goes back to the list", new URL(wide.url()).search === "" && await wide.locator(".pages-index").isVisible(), wide.url());
    check("the list has the keyboard on the page just read", await wide.evaluate(() => document.activeElement?.dataset.page) === firstId, "focus elsewhere");

    const second = wide.locator(".pages-row").nth(3);
    const secondId = await second.getAttribute("data-page");
    const secondPage = listed.find((entry) => String(entry.id) === secondId);
    await second.click();
    check("another row draws its page", (await wide.locator(".pages-frame").getAttribute("src")).startsWith(`/pages/${secondId}/figure`), "");
    check("the preview names its item", (await wide.locator(".pages-preview-head .note").innerText()).includes(secondPage.item_title), "");
    // All work opens only items still on it, so an item it no longer shows has no link.
    const itemLinks = await wide.locator(".pages-preview-head .note a").count();
    check("the item links to All work only while All work shows it", !secondPage.item_on_board ? itemLinks === 0
      : (await wide.locator(".pages-preview-head .note a").getAttribute("href")) === `/#home/item/${secondPage.item_id}`, "");

    await wide.goBack();
    check("Back shows the list", await wide.locator(".pages-index").isVisible(), "");
    check("Back puts the keyboard on the page just read", await wide.evaluate(() => document.activeElement?.dataset.page) === secondId, "focus elsewhere");
    await wide.goBack();
    check("Back again draws the page shown before", (await wide.locator(".pages-frame").getAttribute("src")).startsWith(`/pages/${firstId}/figure`), "");
    await wide.locator(".pages-back").click();

    // J picks a row and Enter shows it, staying on Pages.
    await wide.mouse.click(5, 600);
    await wide.keyboard.press("j");
    const picked = await wide.locator(".pages-row.keyboard-current").getAttribute("data-page");
    await wide.keyboard.press("Enter");
    check("J then Enter shows the picked page", new URL(wide.url()).pathname === "/pages"
      && (await wide.locator(".pages-frame").getAttribute("src")).startsWith(`/pages/${picked}/figure`), wide.url());
    await wide.locator(".pages-back").click();

    const word = listed[3].title.split(/\s+/)[0];
    await wide.fill("#pages-find", word);
    const found = listed.filter((entry) => `${entry.title} ${entry.item_title}`.toLowerCase().includes(word.toLowerCase())).length;
    check(`the box finds pages by "${word}"`, (await wide.locator(".pages-row").count()) === found, `${await wide.locator(".pages-row").count()} rows, ${found} expected`);
    await wide.fill("#pages-find", "zzqqxx no such page");
    check("a search with no match says so", (await wide.locator(".pages-empty").innerText()).includes("zzqqxx"), "");
    await wide.fill("#pages-find", "");

    // Links on a page work as on the page itself: a link to another address opens a new tab and leaves the page in
    // place, a link within the page moves within it.
    const linked = await addPage("Pages test links", '<p><a id="out" href="https://example.com/">out</a></p>'
      + '<svg width="200" height="30"><a xlink:href="https://example.com/svg"><text id="chart" x="4" y="20">chart link</text></a></svg>'
      + '<p><a id="down" href="#end">down</a></p><div style="height:3000px"></div><p id="end">end</p>');
    listed.unshift({ ...linked, item_title: own.title });
    await wide.goto(`${info.origin}/pages?id=${linked.id}`);
    const linkFrame = wide.frameLocator(".pages-frame");
    await linkFrame.locator("#out").waitFor();
    const [tab] = await Promise.all([wide.context().waitForEvent("page", { timeout: 5000 }).catch(() => null), linkFrame.locator("#out").click()]);
    check("a link on the page opens a new tab", Boolean(tab) && tab.url().startsWith("https://example.com"), tab ? tab.url() : "no tab");
    if (tab) await tab.close();
    const [chartTab] = await Promise.all([wide.context().waitForEvent("page", { timeout: 5000 }).catch(() => null), linkFrame.locator("#chart").click()]);
    check("a link in a chart opens a new tab", Boolean(chartTab) && chartTab.url().startsWith("https://example.com/svg"), chartTab ? chartTab.url() : "no tab");
    if (chartTab) await chartTab.close();
    const inner = wide.frames().find((entry) => entry.url().includes(`/pages/${linked.id}/figure`));
    check("the page stays in place after the link", Boolean(inner) && !inner.url().includes("example.com"), inner ? inner.url() : "frame gone");
    await linkFrame.locator("#down").click();
    await sleep(300);
    check("a link within the page moves within it", (await inner.evaluate(() => scrollY)) > 1000, `${await inner.evaluate(() => scrollY)} px`);

    // Feedback mode picks the page itself.
    await wide.locator("#feedback-mode").click();
    const frameBox = await wide.locator(".pages-frame").boundingBox();
    await wide.mouse.click(frameBox.x + frameBox.width / 2, frameBox.y + frameBox.height / 2);
    await sleep(300);
    check("feedback mode picks the page in the frame", await wide.evaluate(() => document.querySelector(".pick-chosen")?.classList.contains("pages-frame")),
      await wide.evaluate(() => document.querySelector(".pick-chosen")?.className || "nothing picked"));
    await wide.keyboard.press("Escape");

    const dark = await open(1280, 860, "dark");
    await dark.goto(`${info.origin}/pages?id=${firstId}`);
    await dark.waitForSelector(".pages-frame");
    await sleep(500);
    await dark.screenshot({ path: path.join(work, "pages-1280-dark.png") });

    // Phone.
    const phone = await open(390, 844, "light");
    await phone.goto(`${info.origin}/pages`);
    await phone.waitForSelector(".pages-row");
    check("the phone hides the preview", !(await phone.locator(".pages-preview").isVisible()), "preview visible");
    const href = await phone.locator(".pages-row").first().getAttribute("href");
    check("a row on the phone links to the page", href === listed[0].url, href);
    const scroll = await phone.evaluate(() => document.documentElement.scrollWidth - innerWidth);
    check("the phone page does not scroll sideways", scroll <= 0, `${scroll} px too wide`);
    await phone.screenshot({ path: path.join(work, "pages-390-light.png"), fullPage: false });
    await phone.goto(`${info.origin}/pages?id=${firstId}`);
    await phone.waitForSelector(".pages-frame");
    const phoneFrame = await phone.locator(".pages-frame").boundingBox();
    check("a page named on the phone shows most of the screen high", phoneFrame.height >= 844 * 0.7, `${Math.round(phoneFrame.height)} px high`);
    const phoneScroll = await phone.evaluate(() => document.documentElement.scrollWidth - innerWidth);
    check("the phone page view does not scroll sideways", phoneScroll <= 0, `${phoneScroll} px too wide`);
    await phone.screenshot({ path: path.join(work, "pages-390-page.png"), fullPage: false });
    const phoneDark = await open(390, 844, "dark");
    await phoneDark.goto(`${info.origin}/pages`);
    await phoneDark.waitForSelector(".pages-row");
    await phoneDark.screenshot({ path: path.join(work, "pages-390-dark.png") });

    // Every page's header leads here: a tab on a wide screen, the brand menu on a phone.
    for (const at of ["/", "/briefing", "/usage", "/spec", "/skills"]) {
      const tab = await open(1440, 900, "light");
      await tab.goto(`${info.origin}${at}`);
      check(`${at} has the Pages tab`, (await tab.locator('.page-tabs a[href="/pages"]').count()) === 1, "");
      check(`${at} has Pages in the brand menu`, (await tab.locator('.brand-menu a.phone-only[href="/pages"]').count()) === 1, "");
    }

    // The harness has no skills checkout, so the Skills page's own load fails there whatever this tab does.
    const ours = problems.filter((problem) => !problem.includes("/skills:"));
    check("no page error or blocked resource", ours.length === 0, ours.join(" | "));
    console.error(`  screenshots in ${work}`);
  } finally {
    if (browser) await browser.close();
  }
  if (results.some((result) => !result.ok)) process.exitCode = 1;
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
