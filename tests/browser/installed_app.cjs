// Cockpit installed as an app (SPEC UI-13 and UI-79): Chromium accepts its manifest, installs it and
// opens it in a window of its own; the files that installing needs are open to a browser without Cockpit's cookie; and an
// update reaches that window and every other page the way the user expects, with no address bar to fall back on. A real
// restart stands in for a deploy: two copies of this checkout, the second with one changed stylesheet, take turns behind
// one address, as they do behind the reverse proxy. Every page shows Reload and none reloads by itself; pressing Reload
// brings the new files, keeps unsent writing, and takes it out of any part that changed meanwhile rather than over it.
//
// Prints one line per check and exits 1 when one failed. Screenshots of Reload on a page other than the main one stay
// in the work folder it names. A run takes about a minute.
//
// Usage, from the checkout: node tests/browser/installed_app.cjs

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;
const { chromium } = require(PLAYWRIGHT);

const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const PAGES = ["/spec", "/briefing", "/usage", "/pages"];
const MARKER = "--installed-app-new-build";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "installed-app-test", "content-type": "application/json" };
const VERSION_ROUTE = "**/api/version";
// The new build's spec changes one row, so writing begun on the old text has nowhere safe to go back to.
const SPEC_BEFORE = "The collapsed sections on a step are clear:";
const SPEC_AFTER = "The collapsed sections on a step are plain:";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// A copy of this checkout that a server can run from, so a later change to it is a different build.
function buildCopy(prefix, change) {
  const copy = workFolder(prefix);
  const skip = (source) => path.basename(source) !== "__pycache__";
  fs.cpSync(path.join(ROOT, "src"), path.join(copy, "src"), { recursive: true, filter: skip });
  fs.cpSync(path.join(ROOT, "tests"), path.join(copy, "tests"), { recursive: true, filter: skip });
  fs.mkdirSync(path.join(copy, "docs"));
  fs.copyFileSync(path.join(ROOT, "docs", "SPEC.md"), path.join(copy, "docs", "SPEC.md"));
  if (change) change(copy);
  return copy;
}

async function startServer(copy) {
  const work = workFolder("cockpit-installed-");
  const server = spawn(PYTHON, [path.join(copy, "tests", "browser", "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  server.unref();
  const file = path.join(work, "cookie.json");
  for (const end = Date.now() + 60e3; Date.now() < end; await sleep(200)) {
    if (server.exitCode !== null) throw new Error(`serve_preview.py exited with ${server.exitCode}`);
    if (!fs.existsSync(file)) continue;
    const info = JSON.parse(fs.readFileSync(file, "utf8"));
    if (await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false)) return { server, info };
  }
  throw new Error("serve_preview.py did not come up within 60 s");
}

const stopServer = (server) => new Promise((resolve) => {
  server.once("exit", resolve);
  server.kill();
});

// One address in front of whichever server runs now, and a bad gateway while none does: the reverse proxy's part.
function proxy() {
  let target = null;
  let signedIn = null;
  const front = http.createServer((request, response) => {
    if (!target) {
      response.writeHead(502, { "content-type": "text/plain" });
      response.end("no server");
      return;
    }
    const { host, port } = new URL(target);
    const headers = { ...request.headers, host: `${host}` };
    if (headers.origin) headers.origin = target;
    if (headers.referer) delete headers.referer;
    // The installed window opens with no test header on it, so its sign-in comes from here, as a saved cookie would.
    if (signedIn) headers.cookie = signedIn;
    const onward = http.request({ host: host.split(":")[0], port, path: request.url, method: request.method, headers }, (answer) => {
      response.writeHead(answer.statusCode, answer.headers);
      answer.pipe(response);
    });
    onward.on("error", () => {
      if (!response.headersSent) response.writeHead(502);
      response.end();
    });
    request.pipe(onward);
  });
  return new Promise((resolve) => {
    front.listen(0, "127.0.0.1", () => {
      front.unref();
      resolve({ origin: `http://127.0.0.1:${front.address().port}`, point: (origin) => { target = origin; }, signIn: (cookie) => { signedIn = cookie; } });
    });
  });
}

function pngSize(bytes) {
  const signature = bytes.subarray(0, 8).toString("hex");
  if (signature !== "89504e470d0a1a0a") return null;
  return [bytes.readUInt32BE(16), bytes.readUInt32BE(20)];
}

// The check a page makes when it comes back to the screen.
const wake = (page) => page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
const reloadShown = (page) => page.evaluate(() => {
  const key = document.querySelector("#reload");
  return Boolean(key) && !key.hidden && key.getClientRects().length > 0;
});
const newFiles = (page) => page.evaluate((name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim(), MARKER);

async function main() {
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  const before = buildCopy("cockpit-build-old-");
  const after = buildCopy("cockpit-build-new-", (copy) => {
    fs.appendFileSync(path.join(copy, "src", "cockpit", "static", "look.css"), `\n:root { ${MARKER}: yes; }\n`);
    const specFile = path.join(copy, "docs", "SPEC.md");
    const specText = fs.readFileSync(specFile, "utf8");
    if (!specText.includes(SPEC_BEFORE)) throw new Error(`the spec no longer says: ${SPEC_BEFORE}`);
    fs.writeFileSync(specFile, specText.replace(SPEC_BEFORE, SPEC_AFTER));
  });
  const shots = workFolder("cockpit-installed-shots-");
  const front = await proxy();
  let running = await startServer(before);
  front.point(running.info.origin);
  let browser = null;
  try {
    // A saved profile, as a private window cannot install anything.
    const context = await chromium.launchPersistentContext(workFolder("cockpit-installed-profile-"), {
      executablePath: CHROMIUM, viewport: { width: 1440, height: 900 },
    });
    browser = context;
    await context.setExtraHTTPHeaders({ cookie: `${running.info.name}=${running.info.value}` });
    const crashes = [];

    // What the browser takes from the running server.
    const main = await context.newPage();
    main.on("pageerror", (error) => crashes.push(`/: ${error.message}`));
    await main.goto(`${front.origin}/`);
    await main.waitForFunction(() => typeof ui !== "undefined" && ui.version !== null);
    const cdp = await context.newCDPSession(main);
    const manifest = await cdp.send("Page.getAppManifest");
    check("Chromium finds the manifest and has no complaint about it",
      manifest.url === `${front.origin}/manifest.webmanifest` && manifest.errors.length === 0, JSON.stringify(manifest.errors));
    const installability = await cdp.send("Page.getInstallabilityErrors");
    check("Chromium finds nothing stopping an install", installability.installabilityErrors.length === 0,
      JSON.stringify(installability.installabilityErrors));
    const icons = await cdp.send("Page.getManifestIcons");
    check("Chromium loads an icon from the manifest", Boolean(icons.primaryIcon), JSON.stringify(icons));

    // Browsers ask for the manifest without cookies; this is that request.
    const open = await fetch(`${front.origin}/manifest.webmanifest`);
    const data = await open.json();
    check("the manifest is open without a cookie and has the app type",
      open.status === 200 && open.headers.get("content-type").startsWith("application/manifest+json"), open.status);
    for (const icon of data.icons) {
      const response = await fetch(`${front.origin}${icon.src}`);
      const size = response.ok ? pngSize(Buffer.from(await response.arrayBuffer())) : null;
      check(`${icon.src} is open without a cookie and is ${icon.sizes}`, size && `${size[0]}x${size[1]}` === icon.sizes, `${response.status} ${size}`);
    }
    const signedOut = await fetch(`${front.origin}/`, { redirect: "manual" });
    check("the page itself still asks for sign-in", signedOut.status === 302, signedOut.status);
    const version = await fetch(`${front.origin}/api/version`);
    check("the running version still asks for sign-in", version.status === 401, version.status);

    // The real install, with nothing signed in behind the address, and the app's own window.
    const manifestId = data.id ? new URL(data.id, front.origin).href : `${front.origin}/`;
    await cdp.send("PWA.install", { manifestId });
    // Installing by hand opens the app in its own window; this debug route leaves it in a tab until told otherwise.
    await cdp.send("PWA.changeAppUserSettings", { manifestId, displayMode: "standalone" });
    const known = new Set(context.pages());
    const opening = context.waitForEvent("page", { timeout: 15e3 });
    front.signIn(`${running.info.name}=${running.info.value}`);
    await cdp.send("PWA.launch", { manifestId });
    const installed = await opening;
    check("the install opens a new window", !known.has(installed), "no new window");
    installed.on("pageerror", (error) => crashes.push(`the installed window: ${error.message}`));
    await installed.waitForFunction(() => typeof ui !== "undefined" && ui.version !== null);
    const standalone = () => installed.evaluate(() => matchMedia("(display-mode: standalone)").matches);
    check("that window opens at the start address", installed.url() === `${front.origin}/`, installed.url());
    check("that window runs as an app, with no address bar", await standalone(), "display mode is not standalone");
    await installed.screenshot({ path: path.join(shots, "installed-window.png") });

    // The other pages, open across the update as an installed app leaves them.
    const others = [];
    for (const place of PAGES) {
      const page = await context.newPage();
      page.on("pageerror", (error) => crashes.push(`${place}: ${error.message}`));
      await page.goto(`${front.origin}${place}`);
      await page.waitForLoadState("networkidle");
      others.push({ place, page });
    }
    const open_pages = [{ place: "/", page: main }, { place: "the installed window", page: installed }, ...others];
    for (const { page } of open_pages) await page.evaluate(() => { window.sameLoad = true; });
    for (const { place, page } of open_pages) {
      await wake(page);
      await sleep(300);
      check(`${place} shows no Reload while it is current`, !(await reloadShown(page)), "it shows");
    }

    // In the layer over the main page, the main page's Reload is the one to show.
    const asked = [];
    const counting = (request) => { if (request.url().endsWith("/api/version")) asked.push(request.url()); };
    main.on("request", counting);
    await main.evaluate(() => new Promise((resolve) => {
      const frame = document.createElement("iframe");
      frame.id = "layer-check";
      frame.src = "/pages?overlay=1";
      frame.onload = resolve;
      document.body.append(frame);
    }));
    await sleep(500);
    await main.evaluate(() => document.querySelector("#layer-check").remove());
    main.off("request", counting);
    check("a page in a layer does not check for itself", asked.length === 0, `${asked.length} requests`);

    // A refused or failed check says nothing and leaves the page alone.
    const probe = others[0].page;
    await probe.route("**/api/version", (route) => route.fulfill({ status: 401, json: { detail: "sign in first", version: "other" } }));
    await wake(probe);
    await sleep(300);
    check("a refused check shows no Reload", !(await reloadShown(probe)) && probe.url().endsWith("/spec"), probe.url());
    await probe.unroute("**/api/version");
    await probe.route("**/api/version", (route) => route.abort());
    await wake(probe);
    await sleep(300);
    check("a failed check shows no Reload", !(await reloadShown(probe)), "it shows");
    await probe.unroute("**/api/version");

    // Pages whose checks all fail until the new build runs: their first answer comes from it, and they are still older.
    const late = [];
    for (const place of ["/usage", "/"]) {
      const page = await context.newPage();
      page.on("pageerror", (error) => crashes.push(`${place} with no early check: ${error.message}`));
      const route = place === "/" ? "**/api/state*" : VERSION_ROUTE;
      await page.route(route, (request) => request.abort());
      await page.goto(`${front.origin}${place}`);
      await sleep(500);
      late.push({ place, page, route });
    }

    // Writing nobody has sent, left on the pages across the update.
    const spec = others[0].page;
    await spec.bringToFront();
    const openOn = async (row, button) => {
      const part = spec.locator(`[id="${row}"]`);
      await part.scrollIntoViewIfNeeded();
      await part.hover();
      await part.locator(`.req-head button[aria-label="${button}"]`).click();
      await part.locator("textarea").first().waitFor();
      return part.locator("textarea").first();
    };
    await (await openOn("UI-16", "Comment")).fill("A comment nobody has sent");
    await (await openOn("UI-14", "Edit")).fill("An edit of a part that changes meanwhile");
    await (await openOn("UI-15", "Edit")).fill("An edit of a part that stays the same");
    const briefing = others[1].page;
    await briefing.bringToFront();
    await briefing.locator("#team-field").fill("Team words nobody has saved");

    // The deploy: this server stops, a moment with nothing behind the address, the new build starts.
    const was = await main.evaluate(() => ui.version);
    await stopServer(running.server);
    front.point(null);
    for (const { page } of open_pages) await wake(page);
    await sleep(1000);
    for (const { place, page } of open_pages) {
      check(`${place} stays on screen while Cockpit restarts`, await page.evaluate(() => window.sameLoad === true), "it reloaded");
    }
    running = await startServer(after);
    front.point(running.info.origin);

    for (const { place, page } of open_pages) {
      await page.bringToFront();
      let shown = false;
      for (const end = Date.now() + 30e3; !shown && Date.now() < end; await sleep(500)) {
        await wake(page);
        shown = await reloadShown(page);
      }
      check(`${place} shows Reload for the new build`, shown, "no Reload after 30 s");
      const drawn = await page.evaluate(() => {
        const key = document.querySelector("#reload");
        return { arrow: Boolean(key.querySelector("svg")), words: key.textContent.trim() };
      });
      check(`${place} draws Reload as its arrow and names it`, drawn.arrow && /Reload/.test(drawn.words) && !drawn.words.includes("[object"), JSON.stringify(drawn));
      check(`${place} did not reload by itself`, await page.evaluate(() => window.sameLoad === true), "it reloaded");
    }
    for (const { place, page, route } of late) {
      await page.unroute(route);
      await page.bringToFront();
      let shown = false;
      for (const end = Date.now() + 30e3; !shown && Date.now() < end; await sleep(500)) {
        await wake(page);
        shown = await reloadShown(page);
      }
      check(`${place} shows Reload when its first answer comes from the new build`, shown, "it takes that build for its own");
      await page.close();
    }
    const reported = await others[0].page.evaluate(() => window.pageOutdated && window.pageOutdated());
    check("the other pages know they are older, for their error reports", reported === true, reported);

    // A Cockpit that goes back to the version that sent the page leaves that page current again.
    const rolled = others[2].page;
    await rolled.bringToFront();
    await rolled.route(VERSION_ROUTE, (route) => route.fulfill({ json: { version: was } }));
    await wake(rolled);
    await sleep(300);
    check("Reload goes away if Cockpit goes back to the page's version", !(await reloadShown(rolled)), "it still shows");
    await rolled.unroute(VERSION_ROUTE);
    await wake(rolled);
    await sleep(300);
    check("Reload comes back when the new version runs again", await reloadShown(rolled), "it stays away");

    // How the key looks where it is new, on a phone and a desktop, light and dark.
    await spec.bringToFront();
    for (const [name, size] of [["phone", { width: 390, height: 780 }], ["desktop", { width: 1280, height: 800 }]]) {
      for (const scheme of ["light", "dark"]) {
        await spec.setViewportSize(size);
        await spec.emulateMedia({ colorScheme: scheme });
        await sleep(150);
        await spec.screenshot({ path: path.join(shots, `reload-${name}-${scheme}.png`), clip: { x: 0, y: 0, width: size.width, height: 160 } });
        const fits = await spec.evaluate(() => {
          const box = document.querySelector("#reload").getBoundingClientRect();
          return box.right <= innerWidth && box.left >= 0 && document.documentElement.scrollWidth <= innerWidth;
        });
        check(`Reload fits on screen, ${name} ${scheme}`, fits, "it overflows");
      }
    }
    await spec.setViewportSize({ width: 1440, height: 900 });
    await spec.emulateMedia({ colorScheme: "light" });
    await spec.evaluate(() => document.getElementById("UI-15").scrollIntoView({ block: "center" }));
    // The usage page's sections are there from the start and fill in later, so the place there is an empty container.
    await rolled.setViewportSize({ width: 1440, height: 400 });
    await rolled.evaluate(() => scrollTo(0, document.documentElement.scrollHeight));
    const usageWas = await rolled.evaluate(() => scrollY);

    for (const { place, page } of open_pages) {
      await page.bringToFront();
      check(`${place} still has the old files before Reload`, (await newFiles(page)) === "", await newFiles(page));
      await Promise.all([page.waitForEvent("load"), page.click("#reload")]);
      await page.waitForLoadState("networkidle");
      check(`Reload on ${place} brings the new files`, (await newFiles(page)) === "yes", await newFiles(page));
      await wake(page);
      await sleep(300);
      check(`${place} shows no Reload after that`, !(await reloadShown(page)), "it still shows");
    }
    check("the installed window is still an app after Reload", await standalone(), "display mode is not standalone");

    await rolled.bringToFront();
    await sleep(1500);
    const usageIs = await rolled.evaluate(() => scrollY);
    check("Reload on the usage page comes back to the same place", usageWas > 0 && Math.abs(usageIs - usageWas) < 80, JSON.stringify({ usageWas, usageIs }));

    // What the writing came back as.
    await spec.bringToFront();
    const same = spec.locator('[id="UI-15"] textarea').first();
    await same.waitFor({ timeout: 5e3 }).catch(() => {});
    check("an edit of a part that did not change comes back open with its writing", (await same.count()) === 1
      && (await same.inputValue()) === "An edit of a part that stays the same", await same.count());
    const place = await spec.evaluate(() => {
      const box = document.getElementById("UI-15").getBoundingClientRect();
      return { top: box.top, bottom: box.bottom, height: innerHeight, scrolled: scrollY };
    });
    check("Reload on the spec keeps the part in view", place.top >= 0 && place.bottom <= place.height, JSON.stringify(place));
    const kept = await spec.locator(".kept-writing").allTextContents();
    check("an edit of a part that changed comes back in a banner", kept.length === 1 && kept[0].includes("An edit of a part that changes meanwhile"), JSON.stringify(kept));
    const changed = spec.locator('[id="UI-14"]');
    check("that part shows its newer text and no editor", (await changed.locator(".req-text").textContent()).includes(SPEC_AFTER.split(":")[0])
      && (await changed.locator("textarea").count()) === 0, await changed.innerText());
    await spec.locator('[id="UI-16"]').hover();
    await spec.locator('[id="UI-16"] .req-head button[aria-label="Comment"]').click();
    check("a comment nobody sent comes back in its box", (await spec.locator('[id="UI-16"] textarea').first().inputValue()) === "A comment nobody has sent", "it is gone");
    const leftOver = await spec.evaluate(() => Object.keys(sessionStorage).filter((key) => key.startsWith("cockpit.reload-held")));
    check("what Reload kept is not kept twice", leftOver.length === 0, JSON.stringify(leftOver));
    await spec.screenshot({ path: path.join(shots, "reload-kept-writing.png") });
    await spec.evaluate(() => scrollTo(0, 0));
    await spec.screenshot({ path: path.join(shots, "reload-kept-banner.png") });
    await briefing.bringToFront();
    await briefing.locator("#team-field").waitFor();
    await briefing.waitForFunction(() => document.querySelector("#team-field").value !== "");
    check("team words nobody saved come back", (await briefing.locator("#team-field").inputValue()) === "Team words nobody has saved", await briefing.locator("#team-field").inputValue());

    // A comment placed on a shared page's words, which lives only in the page's memory. It runs on the new build, with
    // the answer about the version standing in for a second deploy.
    const agent = async (method, where, body) => {
      const answer = await fetch(`${running.info.origin}${where}`, { method, headers: AGENT, body: JSON.stringify(body) });
      if (!answer.ok) throw new Error(`${method} ${where}: ${answer.status}`);
      return answer.json();
    };
    const item = await agent("PUT", "/api/items", { key: "note:installed-app", title: "Installed app check", priority: "urgent", sessions: ["22222222-2222-4222-8222-222222222222"] });
    const shared = await agent("POST", `/api/items/${item.id}/pages`, { title: "A report", html: "<!doctype html><title>A report</title><h1>A report</h1><p>Exports doubled in March.</p>" });
    const pages = others[3].page;
    await pages.bringToFront();
    await pages.goto(`${front.origin}/pages?id=${shared.id}`);
    const frameOf = async () => {
      for (const end = Date.now() + 8e3; Date.now() < end; await sleep(100)) {
        const frame = pages.frames().find((each) => each.url().includes(`/pages/${shared.id}/figure?comments=1`));
        if (frame) {
          await frame.waitForLoadState("load");
          return frame;
        }
      }
      throw new Error("the shared page never opened");
    };
    const frame = await frameOf();
    await pages.locator(".pages-preview-buttons button", { hasText: "Comment" }).click();
    await pages.locator(".pc-hint").waitFor();
    await frame.evaluate(() => {
      const node = document.querySelector("p").firstChild;
      const range = document.createRange();
      range.setStart(node, node.nodeValue.indexOf("doubled in March"));
      range.setEnd(node, node.nodeValue.indexOf("doubled in March") + "doubled in March".length);
      getSelection().removeAllRanges();
      getSelection().addRange(range);
    });
    await pages.locator(".pc-float:not([hidden])").waitFor();
    await pages.locator(".pc-float").click();
    await pages.locator(".pc-draft textarea").fill("A comment on these words");
    await pages.route(VERSION_ROUTE, (route) => route.fulfill({ json: { version: "another build" } }));
    await wake(pages);
    await pages.locator("#reload").waitFor();
    await Promise.all([pages.waitForEvent("load"), pages.click("#reload")]);
    await pages.unroute(VERSION_ROUTE);
    await pages.locator(".pc-draft textarea").waitFor({ timeout: 8e3 }).catch(() => {});
    const placed = pages.locator(".pc-draft textarea");
    check("a comment placed on a page's words comes back open", (await placed.count()) === 1 && (await placed.inputValue()) === "A comment on these words", await placed.count());
    if (await placed.count()) {
      await pages.locator(".pc-draft .button.primary").click();
      await pages.locator(".pc-card.own").first().waitFor();
      const words = await pages.locator(".pc-card.own .pc-on").first().textContent();
      check("sent after Reload, that comment is still on its words", words.includes("doubled in March"), words);
    }

    const now = await main.evaluate(() => ui.version);
    check("the main page runs the new version", now !== was, `${was} then ${now}`);
    check("no page threw an error throughout", crashes.length === 0, crashes.join("; "));
  } finally {
    if (browser) await browser.close();
    running.server.kill();
  }
  const failed = results.filter((result) => !result.ok);
  console.error(`${results.length - failed.length} of ${results.length} checks passed`);
  process.exitCode = failed.length ? 1 : 0;
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
