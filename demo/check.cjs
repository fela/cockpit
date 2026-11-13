// Click through the built demo in a headless browser and fail on any error, any request that leaves the page,
// or any call the stand-in backend could not answer.
//
// Usage: bun demo/check.cjs [demo/dist/cockpit-demo.html] [--plain | --public]
//   PLAYWRIGHT_CORE  path to a playwright-core package, if it is not installed where require finds it
//   CHROMIUM         path to a Chromium or headless-shell binary, if Playwright's own is not installed
// --plain serves the page without the sandbox policy, as a static host without headers would.
// --public serves it under the stricter policy of Cockpit's public pages: no new windows, no downloads.

const fs = require("fs");
const path = require("path");
const { chromium } = require(process.env.PLAYWRIGHT_CORE || "playwright-core");

const args = process.argv.slice(2);
const plain = args.includes("--plain");
const pub = args.includes("--public");
const file = path.resolve(args.find((a) => !a.startsWith("--")) || path.join(__dirname, "dist", "cockpit-demo.html"));
const HOST = "http://demo.test";

// The policy Cockpit's own page sandbox sends, the strictest place the file is likely to be opened.
const CSP = "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox allow-downloads; default-src 'none'; "
  + "script-src 'unsafe-inline' https://cdnjs.cloudflare.com; style-src 'unsafe-inline'; font-src data:; "
  + "img-src data: blob:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";
// The policy a public page gets (SPEC API-5): a page anyone with the link can open, so it may not open windows.
const PUBLIC_CSP = "sandbox allow-scripts allow-modals; default-src 'none'; script-src 'unsafe-inline'; "
  + "style-src 'unsafe-inline'; font-src data:; img-src data: blob:; media-src data: blob:; base-uri 'none'; "
  + "form-action 'none'; frame-ancestors 'none'";
// Chromium logs these while it tears down the old frame on Start over; the new frame draws every logo.
const TEARDOWN = /Connecting to 'data:image\/svg\+xml/;

let failed = 0;
const pass = (name, ok, more = "") => {
  if (!ok) failed++;
  console.log(`${ok ? "ok  " : "FAIL"} ${name}${more ? "  " + more : ""}`);
};

async function open(browser, opts = {}) {
  const ctx = await browser.newContext({
    viewport: opts.viewport || { width: 1400, height: 900 },
    colorScheme: opts.scheme || "light", isMobile: Boolean(opts.mobile), hasTouch: Boolean(opts.mobile),
  });
  const html = fs.readFileSync(file);
  const errs = [];
  let teardown = 0;
  await ctx.route("**/*", (route) => {
    const url = route.request().url();
    if (url === `${HOST}/cockpit-demo.html`) {
      return route.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: html, headers: plain ? {} : { "Content-Security-Policy": pub ? PUBLIC_CSP : CSP } });
    }
    errs.push("request left the page: " + url);
    return route.abort();
  });
  const p = await ctx.newPage();
  p.on("pageerror", (e) => errs.push("page error: " + e.message));
  p.on("console", (m) => {
    if (m.type() !== "error" && m.type() !== "warning") return;
    if (TEARDOWN.test(m.text())) teardown++;
    else if (!m.text().includes("WebGL")) errs.push(`${m.type()}: ${m.text()}`);
  });
  await p.goto(`${HOST}/cockpit-demo.html`, { waitUntil: "domcontentloaded" });
  await p.waitForFunction(() => Boolean(window.__demoShell && document.querySelector("#demo-view")));
  const view = p.frameLocator("#demo-view");
  // The frame has an opaque sandbox origin; use the browser driver to wait for the user's first card.
  await view.locator(".next-card").first().waitFor({ state: "visible" });
  const frame = async () => (await p.$("#demo-view")).contentFrame();
  const st = () => p.evaluate(() => {
    const s = window.__demoShell.state();
    return JSON.stringify({
      queue: s.queue.map((x) => x.id), ready: s.ready_next.map((x) => x.id), snoozed: s.snoozed.map((x) => x.id),
      agents: s.with_agents.map((x) => x.id), closed: (s.closed_recently?.steps || []).map((x) => `${x.id}:${x.status}`),
      items: (s.items || []).length,
    });
  });
  return {
    ctx, p, view, frame, st, errs,
    teardown: () => teardown,
    wait: (ms) => p.waitForTimeout(ms),
    misses: () => p.evaluate(() => window.__demoMisses || []),
    toast: () => p.evaluate(() => document.getElementById("demo-toast").textContent),
    row: (name) => view.locator("button.side-row", { hasText: name }).first(),
  };
}

async function desktop(browser) {
  const d = await open(browser);
  const start = await d.st();
  const text = await (await d.frame()).evaluate(() => document.body.innerText);
  pass("loads with steps", JSON.parse(start).queue.length > 0 && text.length > 200);

  // Answer the first step: it goes to an invented agent.
  await d.view.locator("button.answer", { hasText: "Merge it" }).click();
  await d.wait(800);
  pass("answer hands the step over", JSON.parse(await d.st()).agents.length > JSON.parse(start).agents.length);

  // Reply on another step, then Start over.
  await d.row("Reminder default time").click();
  await d.wait(1000);
  await d.view.getByPlaceholder(/^Reply/).fill("same time tomorrow");
  await d.view.getByRole("button", { name: "Send", exact: true }).click();
  await d.wait(1200);
  await d.p.click("#demo-reset");
  await d.wait(2000);
  pass("Start over returns to the start", (await d.st()) === start);

  // Pick a design in the figure page and ship it.
  await d.row("New pricing page").click();
  await d.wait(1800);
  const fig = (await d.frame()).childFrames()[0];
  pass("figure page opens", Boolean(fig));
  if (fig) {
    await fig.getByLabel("Three columns").check();
    await d.wait(800);
    await d.view.getByRole("button", { name: "Ship the one I picked" }).click();
    await d.wait(1500);
    pass("pick and ship", (await d.st()) !== start);
  }
  await d.p.click("#demo-reset");
  await d.wait(2000);

  // An invented link shows a note instead of opening anything.
  await d.view.locator("details:has(a[href*='pull/41']) > summary").first().click();
  await d.wait(400);
  await d.view.locator("a", { hasText: "PR #41" }).first().click();
  await d.wait(800);
  pass("invented link stays put", /Demo link/.test(await d.toast()) && d.ctx.pages().length === 1, `"${await d.toast()}"`);

  // Sign out goes nowhere.
  await d.view.locator("summary.brand").first().click();
  await d.wait(400);
  await d.view.locator("form[action='/auth/sign-out'] button").first().click();
  await d.wait(1000);
  pass("sign out stays put", /nobody signs in/.test(await d.toast()), `"${await d.toast()}"`);
  await d.p.click("#demo-reset");
  await d.wait(1500);

  // The briefing's link to the whole briefing opens it in a layer over the board, as in Cockpit. The layer has no
  // header of its own, and its X and Escape each close it with the board unchanged underneath.
  const back = d.p.locator("#demo-back");
  pass("Back hidden on the board", !(await back.isVisible()));
  const layer = d.view.locator("dialog.page-overlay[open]");
  const inLayer = d.view.frameLocator("iframe.page-overlay-frame:not(.page-overlay-loading-page)");
  const briefingLink = d.view.locator("a", { hasText: "Open the whole briefing" }).first();
  await d.view.getByText("Show 5 more").first().click().catch(() => {});
  await d.wait(600);
  await d.row("Briefing").click();
  await d.wait(1200);
  const toastBefore = await d.toast();
  await briefingLink.click();
  await d.wait(1800);
  const where = await (await d.frame()).evaluate(() => window.__demoBoot.path);
  pass("whole briefing opens in a layer", (await layer.count()) === 1 && where === "/" && d.ctx.pages().length === 1, where);
  const heading = await inLayer.locator(".briefing-doc h1").first().textContent({ timeout: 3000 }).catch(() => "");
  const header = await inLayer.locator(".top").first().isVisible().catch(() => false);
  pass("the layer shows the briefing without a header", heading.trim().length > 0 && !header, `"${heading.trim()}"`);
  pass("no new-tab note", (await d.toast()) === toastBefore && !(await back.isVisible()), `"${await d.toast()}"`);
  await d.view.locator(".page-overlay-close").first().click();
  await d.wait(800);
  pass("the X closes the layer", (await layer.count()) === 0);
  await briefingLink.click();
  await d.wait(1800);
  await inLayer.locator(".briefing-doc").first().click({ position: { x: 5, y: 5 } }).catch(() => {});
  await d.p.keyboard.press("Escape");
  await d.wait(800);
  pass("Escape inside the briefing closes the layer", (await layer.count()) === 0);
  await briefingLink.click();
  await d.wait(1800);
  await d.p.keyboard.press("Escape");
  await d.wait(800);
  const after = await (await d.frame()).evaluate(() => window.__demoBoot.path);
  pass("Escape on the board closes the layer", (await layer.count()) === 0 && after === "/", after);
  // A task the briefing mentions opens on the board, and the layer closes, rather than Cockpit loading in the layer.
  await briefingLink.click();
  await d.wait(1800);
  const mention = inLayer.locator(".briefing-body a.item-ref", { hasText: /^item \d+$/ }).first();
  await mention.waitFor({ state: "visible" });
  const mentioned = (await mention.getAttribute("href")).replace("/#item/", "");
  await mention.click();
  await d.wait(2500);
  const current = await (await d.frame()).evaluate(() =>
    document.querySelector(".keyboard-current")?.closest("[data-item]")?.dataset.item || null);
  pass("a task the briefing mentions closes the layer and opens on the board",
    Boolean(mentioned) && (await layer.count()) === 0 && current === mentioned && d.ctx.pages().length === 1,
    JSON.stringify({ mentioned, current, open: await layer.count() }));
  await d.p.click("#demo-reset");
  await d.wait(1500);

  // Every view in the command palette opens. As in Cockpit, the helper pages open in the center layer over the board,
  // and Escape inside one closes the layer; the rest open as their own view.
  const centered = d.view.locator("section.center-layer:not([hidden])");
  for (const name of ["Briefing", "Pages", "Usage", "Spec", "Skills", "Sessions", "Messages"]) {
    await d.view.getByRole("button", { name: "Search commands" }).click();
    const search = d.view.getByRole("combobox", { name: "Search commands, work, and sessions", exact: true });
    await search.fill(name);
    await d.view.getByRole("option", { name: `Go to: ${name}`, exact: true }).waitFor({ state: "visible" });
    await search.press("Enter");
    await d.wait(2000);
    // The page runs from srcdoc, so the path it was opened at is in its boot data.
    const where = await (await d.frame()).evaluate(() => window.__demoBoot?.path ?? location.href);
    if (["Briefing", "Pages", "Usage", "Spec", "Skills"].includes(name)) {
      const inner = d.view.frameLocator("iframe.center-page");
      await inner.locator("main").waitFor({ state: "visible" });
      const [path, text] = await inner.locator("body").evaluate((body) => [window.__demoBoot?.path ?? location.href, body.innerText.length]);
      pass(`${name} opens in the center layer`,
        where === "/" && (await centered.count()) === 1 && path === `/${name.toLowerCase()}` && text > 100,
        JSON.stringify({ where, path, text }));
      if (name === "Usage") {
        await inner.locator("body").click({ position: { x: 5, y: 5 } }).catch(() => {});
        await d.p.keyboard.press("Escape");
        await d.wait(800);
        pass("Escape inside a helper page closes the center layer", (await centered.count()) === 0);
      }
    } else {
      const text = await (await d.frame()).evaluate(() => document.body.innerText.length);
      pass(`${name} opens`, where !== "/" && text > 100 && (await d.p.locator("#demo-back").isVisible()), where);
    }
    await d.p.click("#demo-reset");
    await d.wait(1500);
  }

  // The checks below call the stand-in backend as the page's own scripts do, through the page's fetch.
  const call = async (method, url, body) => (await d.frame()).evaluate(async ([method, url, body]) => {
    const init = { method, headers: { "content-type": "application/json" } };
    if (body !== null) init.body = JSON.stringify(body);
    const response = await fetch(url, init);
    return { status: response.status, j: await response.json().catch(() => null) };
  }, [method, url, body]);

  // Each added to-do gets a session of its own, and that session's log shows the to-do's words.
  const words = ["Water the plants", "Book the venue"];
  const added = [];
  for (const text of words) added.push((await call("POST", "/api/items/add", { text })).j?.id ?? null);
  await d.wait(3500);
  const sids = await d.p.evaluate((ids) =>
    ids.map((id) => (window.__demoShell.state().items || []).find((x) => x.id === id)?.sessions?.[0] ?? null), added);
  const logged = [];
  for (const sid of sids) {
    const log = sid ? (await call("GET", `/api/sessions/${encodeURIComponent(sid)}/log`, null)).j : null;
    logged.push((log?.parts || []).filter((part) => part.kind === "instruction").map((part) => part.text));
  }
  pass("each added to-do gets its own session", sids.every(Boolean) && sids[0] !== sids[1], JSON.stringify({ added, sids }));
  pass("each session's log shows its own to-do's words",
    words.every((text, i) => logged[i].includes(text)) && !logged[1].includes(words[0]), JSON.stringify(logged));
  await d.p.click("#demo-reset");
  await d.wait(1500);

  // A comment and a choice on a step's page go with the answer. Take back stops the answer before it leaves, so the
  // step has both back, unsent, as in Cockpit.
  const pricing = await d.p.evaluate(() => {
    const s = window.__demoShell.state();
    const item = (s.items || []).find((x) => x.title === "New pricing page");
    const step = [...s.queue, ...s.ready_next].find((x) => x.item_id === item?.id);
    return item && step ? { item: item.id, step: step.id } : null;
  });
  const page = ((await call("GET", "/api/pages", null)).j?.pages || []).find((x) => x.item_id === pricing?.item);
  if (!pricing || !page) pass("the pricing step has a page", false, JSON.stringify({ pricing, page: page?.id ?? null }));
  else {
    const comments = `/api/item-pages/${page.id}/comments`;
    const choices = `/api/item-pages/${page.id}/choices`;
    const bare = await call("POST", comments, { text: "On nothing", step_id: pricing.step });
    pass("a comment that names no words or part is refused, as on the host", bare.status === 422, String(bare.status));
    const comment = (await call("POST", comments, {
      text: "Make the middle column wider", step_id: pricing.step,
      anchor: { kind: "text", quote: "Three columns", prefix: "", suffix: "" },
    })).j;
    await call("PUT", choices, { name: "layout", chosen: "Three columns", label: "Layout", step_id: pricing.step });
    const states = async () => {
      const own = (await call("GET", comments, null)).j?.comments?.find((x) => x.id === comment?.id);
      const choice = (await call("GET", choices, null)).j?.choices?.find((x) => x.name === "layout");
      const s = await d.p.evaluate(() => window.__demoShell.state());
      return {
        comment: own?.state ?? null, open: own?.step_open ?? null, choiceSent: choice?.sent ?? null,
        onStep: (s.page_comments || []).some((x) => x.id === comment?.id) && (s.page_choices || []).some((x) => x.name === "layout"),
      };
    };
    const handed = await call("POST", `/api/steps/${pricing.step}/hand-off`, { text: "Ship the one I picked" });
    const whileHanded = await states();
    const back = await call("POST", `/api/steps/${pricing.step}/return`, {});
    const after = await states();
    pass("an answer takes the step's comment and choice with it",
      handed.status === 200 && whileHanded.comment === "sent" && whileHanded.choiceSent === true && !whileHanded.onStep,
      JSON.stringify(whileHanded));
    pass("Take back gives the step its comment and choice back, unsent",
      back.status === 200 && after.comment === "unsent" && after.open === 1 && after.choiceSent === false && after.onStep,
      JSON.stringify(after));
  }
  await d.p.click("#demo-reset");
  await d.wait(1500);

  const misses = await d.misses();
  pass("every call answered", misses.length === 0, misses.length ? JSON.stringify(misses) : "");
  pass("no errors or outside requests", d.errs.length === 0, d.errs.slice(0, 5).join(" ;; "));
  if (d.teardown()) console.log(`     (${d.teardown()} teardown messages from Start over, known and harmless)`);
  await d.ctx.close();
}

async function phone(browser) {
  const d = await open(browser, { mobile: true, viewport: { width: 390, height: 844 } });
  const widths = await (await d.frame()).evaluate(() => [document.documentElement.scrollWidth, innerWidth]);
  pass("phone has no sideways scroll", widths[0] <= widths[1], widths.join(" vs "));
  await d.view.getByRole("button", { name: /^Your items/ }).first().tap();
  await d.wait(800);
  await d.row("New pricing page").tap();
  await d.wait(1800);
  const fig = (await d.frame()).childFrames()[0];
  if (fig) {
    await fig.getByLabel("Three columns").tap();
    await d.wait(800);
    const ship = d.view.getByRole("button", { name: "Ship the one I picked" });
    await ship.scrollIntoViewIfNeeded();
    await ship.tap();
    await d.wait(1500);
  }
  pass("phone pick and ship by touch", Boolean(fig) && JSON.parse(await d.st()).agents.length > 0);
  pass("phone has no errors", d.errs.length === 0 && (await d.misses()).length === 0, d.errs.slice(0, 3).join(" ;; "));
  await d.ctx.close();
}

(async () => {
  const how = plain ? " without the sandbox policy" : pub ? " under the public page policy" : " under the sandbox policy";
  console.log(`checking ${path.relative(process.cwd(), file)}${how}`);
  // The page carries other people's code and fonts, whose licences ask that their notices go with every copy.
  const notices = /<script type="text\/plain" id="third-party-notices">([\s\S]*?)<\/script>/.exec(fs.readFileSync(file, "utf8"));
  pass("carries the editor's and the fonts' licence notices", Boolean(notices)
    && ["===== prompt-editor.LICENSE.txt", "@codemirror/view", "MIT License", "===== LICENSE-inter.txt",
      "===== LICENSE-source-serif-4.txt", "SIL Open Font License"].every((t) => notices[1].includes(t)));
  const browser = await chromium.launch(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
  try {
    await desktop(browser);
    await phone(browser);
  } catch (e) {
    failed++;
    console.log("FAIL stopped early:", e.stack || e.message);
  }
  await browser.close();
  console.log(failed ? `${failed} failed` : "all passed");
  process.exit(failed ? 1 : 0);
})();
