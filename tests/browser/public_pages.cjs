// Sharing pages (SPEC API-5, API-7, API-8). Shares a page from its Share menu and opens its link as someone who is
// not signed in, at desktop and phone width, in light and dark:
// - a page starts shared with nobody; its Share menu sits only over the page itself, fits the screen and moves by keys;
// - a phone's list row opens an unshared page rather than sharing it;
// - Anyone with the link asks first; cancelling leaves the page unshared, and accepting hands over the link (the
//   clipboard, or a phone's share sheet) and marks the page public in the list;
// - a session sees who can open the page, but not its link or its people;
// - the "1 shared" button narrows the list to shared pages;
// - the link opens at the public address without signing in, sandboxed, with no Cockpit script added;
// - there the page's script runs and can ask OK or Cancel, but loads nothing from another website, opens no window and
//   starts no download;
// - Specific people opens an address box; the first address shares the page with that person alone, and a visitor
//   who isn't signed in then meets a sign-in page that doesn't name the page;
// - removing the last person shares the page with nobody, and its link stops working;
// - sharing again brings back the same link and says so; Only me ends it without asking;
// - New link ends the old link for good and keeps the page shared at a new one;
// - a session can stop the sharing but can't start it;
// - neither the Pages tab nor the public page breaks the page's security policy.
//
// Starts serve_preview.py (this checkout's Cockpit on a synthetic database; README.md says what it needs)
// and drives headless Chromium. Prints one line per check, saves screenshots in SHOTS_DIR (default: a temporary
// directory) and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/public_pages.cjs

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
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "public-pages-test", "content-type": "application/json" };
const SHOTS = process.env.SHOTS_DIR || fs.mkdtempSync(path.join(os.tmpdir(), "cockpit-public-shots-"));

// Reports what the sandbox allows into the page, where the anonymous visitor's browser can read it back.
const REPORT = `<!doctype html><html><head><title>Shared report</title>
<style>body{font:16px/1.5 system-ui;margin:24px}</style></head><body>
<h1>Shared report</h1><p id="ran">The script did not run.</p><p id="saving"></p><p id="dialog"></p><p id="cookie"></p>
<p id="popup"></p><p id="fetch">fetch pending</p><p id="image">image pending</p><button id="download">Download</button>
<script>
document.getElementById("ran").textContent = "The script ran.";
let saving = "saving works";
try { localStorage.setItem("x", "1"); } catch (error) { saving = "saving refused: " + error.name; }
document.getElementById("saving").textContent = saving;
let answer = "not asked";
try { answer = String(confirm("Sure?")); } catch (error) { answer = "threw " + error.name; }
document.getElementById("dialog").textContent = "confirm gave " + answer;
let cookie = "";
try { cookie = "cookie read: " + document.cookie; } catch (error) { cookie = "cookie refused: " + error.name; }
document.getElementById("cookie").textContent = cookie;
const opened = window.open("about:blank");
document.getElementById("popup").textContent = opened ? "popup opened" : "popup refused";
fetch("https://offsite.example.test/x", { mode: "no-cors" }).then(
  () => { document.getElementById("fetch").textContent = "fetch reached it"; },
  () => { document.getElementById("fetch").textContent = "fetch refused"; });
const image = new Image();
image.onload = () => { document.getElementById("image").textContent = "image loaded"; };
image.onerror = () => { document.getElementById("image").textContent = "image refused"; };
image.src = "https://offsite.example.test/x.png";
document.getElementById("download").onclick = () => {
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob(["MZ"]));
  link.download = "invoice.exe";
  document.body.append(link);
  link.click();
};
</script></body></html>`;

const { chromium } = require(PLAYWRIGHT);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const work = workFolder("cockpit-public-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "ignore", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    const agent = (method, url, body) => call(info.origin, AGENT, method, url, body);
    const user = (method, url, body) => call(info.origin, {
      cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json",
    }, method, url, body);
    const listedTo = (who, id) => who("GET", "/api/pages").then(({ pages }) => pages.find((one) => one.id === id));
    browser = await chromium.launch({ executablePath: CHROMIUM });
    const run = Date.now();
    const sizes = [["desktop", 1280, 900, "light"], ["phone", 390, 844, "dark"]];

    for (const [label, width, height, colorScheme] of sizes) {
      const at = `${label} ${colorScheme}`;
      const phone = label === "phone";
      const item = await agent("PUT", "/api/items", { key: `note:public-${label}-${run}`, title: `Public test ${label}` });
      const added = await agent("POST", `/api/items/${item.id}/pages`, { title: `Report ${label}`, html: REPORT });
      const other = await agent("POST", `/api/items/${item.id}/pages`, { title: `Other ${label}`, html: "<!doctype html><p>Other" });

      const context = await browser.newContext({ viewport: { width, height }, colorScheme, hasTouch: phone, isMobile: phone });
      await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: info.origin });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      // A phone's share sheet can't open headless; this records what would have gone to it.
      if (phone) await context.addInitScript(() => {
        window.sharedLinks = [];
        navigator.share = async (data) => { window.sharedLinks.push(data); };
      });
      const page = await context.newPage();
      const problems = [];
      page.on("console", (message) => { if (message.type() === "error") problems.push(message.text()); });
      page.on("pageerror", (error) => problems.push(String(error)));
      let answer = true;
      const asked = [];
      page.on("dialog", async (dialog) => {
        asked.push(dialog.message());
        await (answer ? dialog.accept() : dialog.dismiss());
      });

      // The Share menu sits only over the page itself. A phone's list row opens the page for it.
      const row = page.locator(`li:has(a.pages-row[data-page="${added.id}"])`);
      const inRow = (words) => row.locator(`.pages-row-share button[aria-label="${words}"]`);
      const head = (name) => page.locator(`#pages-preview [data-head="${name}"]`);
      const shareKey = head("share");
      const menu = page.locator("#pages-preview details.share-menu");
      const openMenu = async () => {
        if (!(await menu.evaluate((el) => el.open))) await shareKey.click();
        await head("share-me").waitFor();
      };
      const checked = () => menu.locator('[role="menuitemradio"][aria-checked="true"]').getAttribute("data-head");
      const keyWords = async () => (await shareKey.textContent()).trim();
      const pagesTab = `${info.origin}/pages`;
      const reading = `${info.origin}/pages?id=${added.id}`;
      const room = () => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      if (phone) {
        await page.goto(pagesTab);
        await inRow("Open to share").waitFor();
        const box = await inRow("Open to share").boundingBox();
        check(`${at}: an unshared row's button is large enough to tap`, box.width >= 40 && box.height >= 40, JSON.stringify(box));
        await inRow("Open to share").click();
        await shareKey.waitFor();
        check(`${at}: the row's button opens the page without asking or sharing`,
          asked.length === 0 && !(await listedTo(user, added.id)).public, JSON.stringify(asked));
      } else {
        await page.goto(reading);
        await shareKey.waitFor();
        check(`${at}: an unshared row has no share button on a wide screen`, !(await inRow("Open to share").isVisible()), "visible");
      }
      check(`${at}: a page starts shared with nobody`, (await keyWords()) === "Share" && !(await shareKey.getAttribute("class")).includes(" on"), await keyWords());
      check(`${at}: the heading doesn't widen the page`, (await room()) <= 0, `${await room()}px`);
      await openMenu();
      check(`${at}: the menu says only the user can open it`, (await checked()) === "share-me", await checked());
      check(`${at}: the menu offers the three choices`, await menu.locator('[role="menuitemradio"]').count() === 3, "wrong count");
      check(`${at}: an unshared page's menu has no link to copy`, await head("share-copy").count() === 0, "found one");
      const sheet = await menu.locator(".menu-list").boundingBox();
      check(`${at}: the menu fits the screen`, sheet && sheet.x >= 0 && sheet.x + sheet.width <= width && sheet.y + sheet.height <= height, JSON.stringify(sheet));
      await page.screenshot({ path: path.join(SHOTS, `${label}-menu.png`) });
      if (!phone) {
        await head("share-me").focus();
        await page.keyboard.press("ArrowDown");
        const moved = await page.evaluate(() => document.activeElement.dataset.head);
        check(`${at}: Down moves to the next choice`, moved === "share-people", moved);
        await page.keyboard.press("Escape");
        await sleep(100);
        check(`${at}: Escape closes the menu`, !(await menu.evaluate((el) => el.open)), "still open");
        await openMenu();
        await page.locator("#pages-preview h2").click();
        await sleep(100);
        check(`${at}: a press outside closes the menu`, !(await menu.evaluate((el) => el.open)), "still open");
        await openMenu();
      }

      // Anyone with the link.
      answer = false;
      await head("share-anyone").click();
      await sleep(300);
      const stillPrivate = await listedTo(user, added.id);
      check(`${at}: cancelling leaves the page shared with nobody`, asked.length === 1 && !stillPrivate.public && !stillPrivate.public_url, JSON.stringify(stillPrivate));
      check(`${at}: sharing says who can open the page`, /Anyone with the link can open this page without signing in/.test(asked[0]), asked[0]);
      check(`${at}: sharing warns that the page's code runs`, /own code runs for them/.test(asked[0]) && !/shared before/.test(asked[0]), asked[0]);
      check(`${at}: sharing warns about other websites`, /from other websites won't load/.test(asked[0]), asked[0]);
      check(`${at}: sharing names the page last`, asked[0].endsWith(`Share “Report ${label}”?`), asked[0]);

      answer = true;
      await head("share-anyone").click();
      await head("share-copy").waitFor();
      const listed = await listedTo(user, added.id);
      check(`${at}: the page has a link at the public address`, listed.access === "anyone" && listed.public_url?.startsWith(`${info.public_origin}/p/`) && /\/p\/[A-Za-z0-9_-]{43}$/.test(listed.public_url), listed.public_url);
      const toSession = await listedTo(agent, added.id);
      check(`${at}: a session sees who can open the page but not its link or people`, toSession.access === "anyone" && toSession.public_url === null && toSession.people === null, JSON.stringify(toSession));
      if (phone) {
        const shared = await page.evaluate(() => window.sharedLinks);
        check(`${at}: sharing opens the share sheet with the link`, shared.length === 1 && shared[0].url === listed.public_url, JSON.stringify(shared));
      } else {
        const copied = await page.evaluate(() => navigator.clipboard.readText());
        check(`${at}: sharing copies the link`, copied === listed.public_url, copied);
      }
      check(`${at}: the Share key says Public`, (await keyWords()) === "Public", await keyWords());
      check(`${at}: the menu stays open on its new choice`, (await checked()) === "share-anyone", await checked());
      check(`${at}: the list marks the page public`, (await row.locator(".pages-public").textContent()) === "public", "no chip");
      await sleep(200);
      await page.screenshot({ path: path.join(SHOTS, `${label}-public.png`) });
      check(`${at}: a shared page's heading doesn't widen the page`, (await room()) <= 0, `${await room()}px`);
      const copyWords = phone ? "Share link…" : "Copy link";
      check(`${at}: the menu offers ${copyWords}`, (await head("share-copy").textContent()).includes(copyWords), await head("share-copy").textContent());
      if (phone) {
        await page.goto(pagesTab);
        await inRow("Share link").waitFor();
        const box = await inRow("Share link").boundingBox();
        check(`${at}: a row's button is large enough to tap`, box.width >= 40 && box.height >= 40, JSON.stringify(box));
        await inRow("Share link").click();
        await sleep(100);
        check(`${at}: a row's Share link opens the share sheet again`, (await page.evaluate(() => window.sharedLinks.at(-1)?.url)) === listed.public_url, "no share");
        check(`${at}: the row's buttons don't widen the list`, (await room()) <= 0, `${await room()}px`);
      } else {
        await page.evaluate(() => navigator.clipboard.writeText("something else"));
        await head("share-copy").click();
        await sleep(100);
        check(`${at}: Copy link copies it again`, (await page.evaluate(() => navigator.clipboard.readText())) === listed.public_url, "not copied");
        check(`${at}: Copy link closes the menu`, !(await menu.evaluate((el) => el.open)), "still open");
      }

      // The list's button shows everything someone else can open right now, then everything again.
      await page.goto(pagesTab);
      const filter = page.locator(".pages-public-filter");
      await filter.waitFor();
      check(`${at}: the list counts shared pages`, (await filter.textContent()) === "1 shared", await filter.textContent());
      const otherRow = page.locator(`a.pages-row[data-page="${other.id}"]`);
      check(`${at}: the list starts with every page`, await otherRow.count() === 1 && await row.count() === 1, "a page is missing");
      await filter.click();
      check(`${at}: the button narrows the list to shared pages`, await otherRow.count() === 0 && await row.count() === 1, "wrong rows");
      await filter.click();
      check(`${at}: pressing it again brings every page back`, await otherRow.count() === 1, "other page missing");
      await page.goto(reading);
      await shareKey.waitFor();

      // Someone who has never signed in, in a browser with no Cockpit cookie.
      const visitor = await browser.newContext({ viewport: { width, height }, colorScheme, hasTouch: phone, isMobile: phone });
      const reader = await visitor.newPage();
      const readerProblems = [];
      reader.on("console", (message) => { if (message.type() === "error") readerProblems.push(message.text()); });
      reader.on("pageerror", (error) => readerProblems.push(String(error)));
      const readerAsked = [];
      reader.on("dialog", async (dialog) => {
        readerAsked.push(dialog.message());
        await dialog.accept();
      });
      const downloads = [];
      reader.on("download", (download) => downloads.push(download.suggestedFilename()));
      const opened = await reader.goto(listed.public_url);
      check(`${at}: the link opens without signing in`, opened.status() === 200 && new URL(reader.url()).pathname.startsWith("/p/"), `${opened.status()} ${reader.url()}`);
      const text = (id) => reader.locator(`#${id}`).textContent();
      check(`${at}: the page's own script runs`, (await text("ran")) === "The script ran.", await text("ran"));
      check(`${at}: the shared page can't read Cockpit's cookies`, /refused/.test(await text("cookie")), await text("cookie"));
      check(`${at}: the shared page can ask OK or Cancel`, readerAsked.length === 1 && (await text("dialog")) === "confirm gave true", await text("dialog"));
      check(`${at}: the shared page can't save`, /refused/.test(await text("saving")), await text("saving"));
      check(`${at}: the shared page can't open a window`, (await text("popup")) === "popup refused", await text("popup"));
      await sleep(1500);
      check(`${at}: the shared page can't fetch from another website`, (await text("fetch")) === "fetch refused", await text("fetch"));
      check(`${at}: the shared page can't load an image from another website`, (await text("image")) === "image refused", await text("image"));
      await reader.locator("#download").click();
      await sleep(500);
      check(`${at}: the shared page can't start a download`, downloads.length === 0, downloads.join(","));
      const scripts = await reader.locator("script[src]").count();
      check(`${at}: Cockpit adds no script to a shared page`, scripts === 0, `${scripts} scripts`);
      await reader.screenshot({ path: path.join(SHOTS, `${label}-visitor.png`) });
      const privatePage = await reader.goto(added.url);
      check(`${at}: the visitor still can't open the private address`, !new URL(reader.url()).pathname.startsWith("/pages/") || privatePage.status() >= 400, `${privatePage.status()} ${reader.url()}`);
      const publicHost = await reader.goto(`${info.public_origin}/pages/${added.id}`);
      check(`${at}: the public address doesn't show private pages`, publicHost.status() === 404, String(publicHost.status()));
      const appThere = await reader.goto(`${info.public_origin}/`);
      check(`${at}: the public address doesn't show Cockpit`, appThere.status() === 404, String(appThere.status()));

      // Specific people: the panel opens first, and the first address shares the page with them alone.
      await openMenu();
      const before = asked.length;
      await head("share-people").click();
      await head("share-input").waitFor();
      const focused = await page.evaluate(() => document.activeElement.dataset.head);
      check(`${at}: Specific people opens the address box`, focused === "share-input", focused);
      check(`${at}: opening the box changes nothing yet`, (await listedTo(user, added.id)).access === "anyone", "changed");
      await head("share-input").fill("not an address");
      await head("share-add").click();
      await sleep(200);
      check(`${at}: a mistyped address isn't sent`, (await listedTo(user, added.id)).people.length === 0, "sent");
      await head("share-input").fill("Friend.Name+cockpit@Gmail.com");
      await head("share-input").press("Enter");
      await page.waitForFunction(() => document.querySelector('#pages-preview [data-head="share"]')?.textContent.trim() === "1 person");
      const narrowed = await listedTo(user, added.id);
      check(`${at}: adding someone shares the page with them alone`, narrowed.access === "people" && JSON.stringify(narrowed.people) === '["friend.name+cockpit@gmail.com"]', JSON.stringify(narrowed));
      check(`${at}: narrowing from anyone asks nothing`, asked.length === before, asked.at(-1));
      check(`${at}: narrowing keeps the link`, narrowed.public_url === listed.public_url, narrowed.public_url);
      check(`${at}: the box is empty and ready for the next address`, (await head("share-input").inputValue()) === ""
        && (await page.evaluate(() => document.activeElement.dataset.head)) === "share-input", "not ready");
      check(`${at}: the list says whom it is shared with`, (await row.locator(".pages-public").textContent()) === "1 person", await row.locator(".pages-public").textContent());
      const door = await reader.goto(listed.public_url);
      const doorText = await reader.locator("main").textContent();
      check(`${at}: a visitor who isn't signed in is asked to sign in`, door.status() === 401 && /Sign in with Google/.test(doorText), `${door.status()} ${doorText}`);
      check(`${at}: the sign-in page doesn't name the page`, !doorText.includes(`Report ${label}`) && (await reader.locator("script").count()) === 0, doorText);
      await reader.screenshot({ path: path.join(SHOTS, `${label}-door.png`) });
      // Signed in as someone not on the list: told so, and Sign out really signs out.
      await visitor.addCookies([{ name: info.visitor.name, value: info.visitor.value, url: info.public_origin.replace(/^http:/, "https:"),
        secure: true, httpOnly: true, sameSite: "Lax" }]);
      const notListed = await reader.goto(listed.public_url);
      const refusedText = await reader.locator("main").textContent();
      check(`${at}: a visitor not on the list is told so, with their address`, notListed.status() === 403
        && refusedText.includes(info.visitor.email) && !refusedText.includes(`Report ${label}`), `${notListed.status()} ${refusedText}`);
      await reader.screenshot({ path: path.join(SHOTS, `${label}-not-shared.png`) });
      await Promise.all([reader.waitForNavigation(), reader.getByRole("button", { name: "Sign out" }).click()]);
      const outText = await reader.locator("main").textContent();
      const kept = (await visitor.cookies(info.public_origin)).filter((cookie) => cookie.name === info.visitor.name);
      check(`${at}: Sign out signs the visitor out`, /Signed out/.test(outText) && kept.length === 0, `${outText} ${kept.length}`);
      const signedOut = await reader.goto(listed.public_url);
      check(`${at}: after signing out the link asks to sign in again`, signedOut.status() === 401, String(signedOut.status()));
      await head("share-input").fill("second@example.com");
      await head("share-add").click();
      await page.waitForFunction(() => document.querySelector('#pages-preview [data-head="share"]')?.textContent.trim() === "2 people");
      await page.screenshot({ path: path.join(SHOTS, `${label}-people.png`) });
      check(`${at}: the people list doesn't widen the page`, (await room()) <= 0, `${await room()}px`);
      await menu.locator('button[aria-label="Remove friend.name+cockpit@gmail.com"]').click();
      await page.waitForFunction(() => document.querySelector('#pages-preview [data-head="share"]')?.textContent.trim() === "1 person");
      check(`${at}: removing someone keeps the others`, JSON.stringify((await listedTo(user, added.id)).people) === '["second@example.com"]', "wrong list");
      await menu.locator('button[aria-label="Remove second@example.com"]').click();
      await page.waitForFunction(() => document.querySelector('#pages-preview [data-head="share"]')?.textContent.trim() === "Share");
      const nobody = await listedTo(user, added.id);
      check(`${at}: removing the last person shares the page with nobody`, nobody.access === "me" && !nobody.public && nobody.people.length === 0, JSON.stringify(nobody));
      check(`${at}: then the link stops working`, (await reader.goto(listed.public_url)).status() === 404, "still works");
      check(`${at}: the list stops marking the page`, await row.locator(".pages-public").count() === 0, "chip remains");

      // Sharing again brings back the same link, and says so; Only me ends it again without asking.
      await openMenu();
      await head("share-anyone").click();
      await head("share-copy").waitFor();
      const again = await listedTo(user, added.id);
      check(`${at}: sharing again brings back the same link`, again.public_url === listed.public_url, again.public_url);
      check(`${at}: sharing again warns the link sent before works again`, /shared before and keeps the link you sent then/.test(asked.at(-1)), asked.at(-1));
      check(`${at}: the link sent before works again`, (await reader.goto(listed.public_url)).status() === 200, "link still ended");
      const asks = asked.length;
      await head("share-me").click();
      await page.waitForFunction(() => document.querySelector('#pages-preview [data-head="share"]')?.textContent.trim() === "Share");
      check(`${at}: Only me asks nothing`, asked.length === asks, asked.at(-1));
      check(`${at}: Only me ends the link`, (await reader.goto(listed.public_url)).status() === 404, "still works");
      await head("share-anyone").click();
      await head("share-copy").waitFor();

      // New link, for a link that reached the wrong person: the old one ends for good and the page stays shared.
      answer = false;
      await head("share-new-link").click();
      await sleep(300);
      check(`${at}: cancelling New link keeps the link`, (await listedTo(user, added.id)).public_url === listed.public_url, "link changed");
      answer = true;
      await openMenu();
      await head("share-new-link").click();
      let renewed = await listedTo(user, added.id);
      for (let tries = 0; tries < 20 && renewed.public_url === listed.public_url; tries++) {
        await sleep(250);
        renewed = await listedTo(user, added.id);
      }
      check(`${at}: New link gives the page a different link`, renewed.public && renewed.public_url && renewed.public_url !== listed.public_url, renewed.public_url);
      check(`${at}: New link ends the old link`, (await reader.goto(listed.public_url)).status() === 404, "old link works");
      check(`${at}: the page opens at its new link`, (await reader.goto(renewed.public_url)).status() === 200, "new link fails");
      check(`${at}: New link says the old link ends for good`, /stops working for good/.test(asked.at(-1)), asked.at(-1));
      const handed = phone ? await page.evaluate(() => window.sharedLinks.at(-1)?.url)
        : await page.evaluate(() => navigator.clipboard.readText());
      check(`${at}: New link hands over the new link`, handed === renewed.public_url, handed);

      // A real phone opens the share sheet only within seconds of the press, and refuses it after the confirmation
      // and the save took longer.
      if (phone) {
        const reported = [];
        const note = (request) => { if (request.url().endsWith("/api/page-errors")) reported.push(request.postData()); };
        page.on("request", note);
        await page.evaluate(() => {
          navigator.share = async () => {
            throw new DOMException("Must be handling a user gesture to perform a share request.", "NotAllowedError");
          };
          return navigator.clipboard.writeText("something else");
        });
        await openMenu();
        await head("share-new-link").click();
        let late = await listedTo(user, added.id);
        for (let tries = 0; tries < 20 && late.public_url === renewed.public_url; tries++) {
          await sleep(250);
          late = await listedTo(user, added.id);
        }
        await page.locator("#banner", { hasText: "Copied the link" }).waitFor({ timeout: 5000 }).catch(() => {});
        const copied = await page.evaluate(() => navigator.clipboard.readText());
        check(`${at}: a share sheet the browser refuses copies the link`, late.public_url !== renewed.public_url && copied === late.public_url, copied);
        check(`${at}: the page says it copied the link`, /Copied the link/.test(await page.locator("#banner").textContent()), await page.locator("#banner").textContent());
        await sleep(1500);
        check(`${at}: a refused share sheet is not reported as an error`, reported.length === 0, reported.join(" | "));
        page.off("request", note);
      }

      // The layer that opens a page over Cockpit's main page, from its task's links, has the same Share menu, and
      // Escape there closes only the menu.
      await page.goto(`${info.origin}/#home/item/${item.id}`);
      await page.locator(`a[href$="/pages/${added.id}"]:visible`).first().click();
      const layerHandle = await page.locator("dialog.page-overlay[open]:not(.page-overlay-loading-page) iframe.page-overlay-frame").elementHandle();
      const layer = await layerHandle.contentFrame();
      const layerKey = layer.locator('#pages-preview [data-head="share"]');
      await layerKey.waitFor();
      check(`${at}: the layer has the Share menu`, (await layerKey.textContent()).trim() === "Public", await layerKey.textContent());
      await layerKey.click();
      await layer.locator('#pages-preview [data-head="share-me"]').waitFor();
      const layerChecked = await layer.locator('#pages-preview [role="menuitemradio"][aria-checked="true"]').getAttribute("data-head");
      check(`${at}: the layer's menu says anyone can open it`, layerChecked === "share-anyone", layerChecked);
      const layerSheet = await layer.locator("#pages-preview details.share-menu .menu-list").boundingBox();
      check(`${at}: the layer's menu fits the screen`, layerSheet && layerSheet.x >= 0 && layerSheet.x + layerSheet.width <= width && layerSheet.y + layerSheet.height <= height, JSON.stringify(layerSheet));
      await page.screenshot({ path: path.join(SHOTS, `${label}-layer-menu.png`) });
      await page.keyboard.press("Escape");
      await sleep(500);
      check(`${at}: Escape closes the layer's menu`, !(await layer.locator("#pages-preview details.share-menu").evaluate((el) => el.open)), "still open");
      check(`${at}: Escape on the menu leaves the layer open`, await page.locator("dialog.page-overlay[open]").count() > 0, "the layer closed");
      // A session may stop the sharing, never start it.
      await agent("DELETE", `/api/item-pages/${added.id}/public`);
      check(`${at}: a session can stop the sharing`, !(await listedTo(user, added.id)).public, "still shared");
      const refusedShare = await fetch(`${info.origin}/api/item-pages/${added.id}/sharing`, {
        method: "PUT", headers: AGENT, body: JSON.stringify({ access: "anyone", revision: added.revision }),
      });
      check(`${at}: a session can't share a page`, refusedShare.status === 403, String(refusedShare.status));
      await agent("DELETE", `/api/item-pages/${other.id}`);

      // A browser reports refused requests as console errors, so this also catches a broken security policy. Expected:
      // the links that no longer work, what the report page itself tries and is refused (its window, its download and
      // everything at offsite.example.test), and the site icon. The icon needs sign-in, so the browser's request for it
      // ends at Google and the dead link's own security policy refuses it.
      const refused = (list) => list.filter((one) => !/status of 40[134]|Ignored call to 'confirm|Loading the image 'https:\/\/accounts\.google\.com|offsite\.example\.test|Blocked opening|allow-popups|Download is disallowed|allow-downloads/.test(one));
      check(`${at}: the Pages tab reports no errors`, refused(problems).length === 0, refused(problems).join(" | "));
      check(`${at}: the public page reports no errors`, refused(readerProblems).length === 0, refused(readerProblems).join(" | "));
      await visitor.close();
      await context.close();
    }
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  console.error(`screenshots: ${SHOTS}`);
  const failed = results.filter((one) => !one.ok);
  console.error(`${results.length - failed.length} of ${results.length} checks passed`);
  if (failed.length) process.exitCode = 1;
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
