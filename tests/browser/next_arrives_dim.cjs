// After a close or a Send on Next up, the step that comes next starts a shade darker under the leaving card's
// animation and lightens into place. On a synthetic database, each round snoozes the steps there, adds three items
// with one step each and, in light and in dark, presses Task done on the first (a close, played as Tear, which builds
// its pieces late and keeps them in sight longest), then a one-press answer on the second (a Send). Checks:
// - the shade comes on at full strength over the whole window, with the leaving copy over it and the next step under it;
// - clicks go through it;
// - it only lightens, eased over at least 400 ms; it lasts 0.65 to 2.5 s, at least 1.2 s for Tear, and is off the
//   page when it ends;
// - it darkens the pixels of the card, the margins beside it and the rail, and a press anywhere lifts it at once;
// - the page threw no errors.
// Saves screenshots of the page about 100 ms and 500 ms into the shade, and after it, in the test's work folder.
//
// Starts serve_slow.py (this checkout's Cockpit on a synthetic database, every non-GET /api/ request held 2 s;
// README.md says what it needs) and drives a headless Chromium at 1280 px on the page at /. Prints one line per check
// and exits 1 when one failed.
//
// Usage, from the checkout: node tests/browser/next_arrives_dim.cjs
"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "next-arrives-dim", "content-type": "application/json" };

async function main() {
  const work = workFolder("cockpit-next-dim-");
  const server = spawn(path.join(__dirname, "..", "..", ".venv", "bin", "python"), [path.join(__dirname, "serve_slow.py"), work],
    { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    const user = { cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json" };
    const call = async (who, method, url, body) => {
      const response = await fetch(`${info.origin}${url}`, { method, headers: who, body: body === undefined ? undefined : JSON.stringify(body) });
      if (!response.ok) throw new Error(`${method} ${url}: HTTP ${response.status} ${await response.text()}`);
      return response.json();
    };
    browser = await chromium.launch({ executablePath: CHROMIUM });
    for (const scheme of ["light", "dark"]) await round({ browser, info, user, call, check, work, scheme });
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failed = results.filter((result) => !result.ok).length;
  console.error(`${results.length - failed}/${results.length} checks pass`);
  console.error(`screenshots: ${work}`);
  process.exitCode = failed ? 1 : 0;
}

async function round({ browser, info, user, call, check, work, scheme }) {
  const named = (words) => `${scheme}: ${words}`;
  const initial = await call(user, "GET", "/api/state");
  await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((step) => call(user, "POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
  // A blocked call seeded in the fixture database would show on Next up above the queue.
  await Promise.all(initial.permission_requests.map((request) =>
    call(AGENT, "POST", `/api/permissions/requests/${request.id}/withdraw`, { session_id: request.session_id })));
  const stamp = `${scheme}-${Date.now()}`;
  // One item per step: an item shows one card at a time (SPEC NEXT-55).
  const keys = { finish: `note:next-dim-finish-${stamp}`, lists: `note:next-dim-lists-${stamp}`, completed: `note:next-dim-completed-${stamp}` };
  // A new item goes on prod only with its next move open (SPEC STATE-5), so the finish item waits at building.
  await call(AGENT, "PUT", "/api/items", { key: keys.finish, title: "Weekly reminder selector", stage: "building", sessions: [SESSION_B] });
  await call(AGENT, "PUT", "/api/items", { key: keys.lists, title: "Shared list exports", stage: "building", sessions: [SESSION_B] });
  await call(AGENT, "PUT", "/api/items", { key: keys.completed, title: "Completed tasks in exports", stage: "building", sessions: [SESSION_B] });
  const put = async (body) => (await call(AGENT, "POST", "/api/steps", { session_id: SESSION_B, ...body })).step;
  const finish = await put({ item_key: keys.finish, need: "complete", ask: "Weekly reminders are ready: confirm it is done", score: 90,
    body: "Tasks now lets you choose a weekly reminder on each list." });
  const send = await put({ item_key: keys.lists, need: "n1", ask: "Show shared lists in exports?", score: 70,
    body: "The export selector now includes shared lists. Choose which lists to include in the CSV.",
    answers: [{ text: "Show it", closes: false }] });
  const third = await put({ item_key: keys.completed, need: "n2", ask: "Export completed tasks too?", score: 68,
    body: "Completed tasks can be included or left out of the CSV export." });

  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, colorScheme: scheme });
  await context.setExtraHTTPHeaders({ cookie: user.cookie });
  const page = await context.newPage();
  page.setDefaultTimeout(8000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  try {
    await page.goto(`${info.origin}/`, { timeout: 60000 });
    await page.locator(`#next [data-step="${finish.id}"]`).waitFor({ timeout: 30000 });
    await page.mouse.move(640, 300);
    await sleep(1500);
    // Tear builds its pieces late, so its measure comes late.
    await press(page, check, work, named, `close`, page.locator(`#next [data-step="${finish.id}"] .actions button.task-done`).first(), send.id,
      "tear");
    // The close's answer comes after 2 s and the page polls; then the Send.
    await sleep(6000);
    await page.locator(`#next [data-step="${send.id}"]`).waitFor({ timeout: 20000 });
    await sleep(1000);
    await press(page, check, work, named, `send`, page.locator(`#next [data-step="${send.id}"] .answers button`, { hasText: "Show it" }).first(), third.id);
    await sleep(4000);
    await shades(page, check, named);
    await lifts(page, check, named);
    check(named("the page threw no errors"), errors.length === 0, errors.join(" | "));
  } finally {
    await context.close();
  }
}

// Presses `key` on Next up. From the press, the page notes the shade over #next on every frame for 8 s, with the
// shade animation's own clock: a loaded host draws the redraw after the press late, so the test's clock says little.
// Screenshots go at about 100 ms and 500 ms on the shade's clock, and after it ends.
async function press(page, check, work, named, kind, key, next, force) {
  // A close picks one of its animations at random: `force` names the one to play (not a GPU one), by answering its
  // pick. For the press's task only: the GPU closes stay out of the pick, and every random number is that answer.
  if (force) {
    await page.evaluate((name) => {
      const names = Object.keys(CLOSES).filter((n) => n !== lastClose && !GPU_CLOSES.includes(n));
      const random = Math.random;
      const ready = GpuCloses.ready;
      GpuCloses.ready = () => false;
      Math.random = () => {
        setTimeout(() => { Math.random = random; GpuCloses.ready = ready; });
        return (names.indexOf(name) + 0.5) / names.length;
      };
    }, force);
  }
  await page.evaluate(() => {
    window.__shade = []; window.__shadeDone = false;
    const samples = window.__shade;
    const t0 = performance.now();
    const tick = () => {
      const section = document.getElementById("next");
      const layer = document.querySelector(".arrive-shade");
      const anim = layer ? layer.getAnimations()[0] : null;
      const shade = layer ? getComputedStyle(layer) : { opacity: "0", pointerEvents: "none" };
      const box = layer ? layer.getBoundingClientRect() : null;
      const card = section.querySelector(":scope > .next-card");
      samples.push({
        at: Math.round(performance.now() - t0),
        on: Boolean(layer),
        time: anim ? Math.round(anim.currentTime) : null,
        duration: anim ? Math.round(anim.effect.getComputedTiming().duration) : null,
        fade: anim ? (() => {
          const frames = anim.effect.getKeyframes();
          const hold = frames.at(-2);
          return { from: Number(hold.opacity), to: Number(frames.at(-1).opacity), easing: hold.easing,
            ms: Math.round(anim.effect.getComputedTiming().duration * (1 - hold.computedOffset)) };
        })() : null,
        opacity: Number(shade.opacity),
        events: shade.pointerEvents,
        window: box ? box.left <= 0 && box.top <= 0 && box.right >= innerWidth && box.bottom >= innerHeight : null,
        step: card ? Number(card.dataset.step) : null,
        copies: document.querySelectorAll(".close-fx, .tear-piece").length,
        copiesOver: layer ? [...document.querySelectorAll(".close-fx, .tear-piece")]
          .every((copy) => Number(getComputedStyle(copy).zIndex) > Number(shade.zIndex)) : null,
      });
      if (performance.now() - t0 < 8000) requestAnimationFrame(tick); else window.__shadeDone = true;
    };
    requestAnimationFrame(tick);
  });
  await key.click();
  const shot = (name) => page.screenshot({ path: path.join(work, `${named(kind).replace(/[^a-z]+/g, "-")}-${name}.png`) });
  const atShade = (ms) => page.waitForFunction((t) => window.__shade.some((s) => s.time !== null && s.time >= t), ms, { polling: "raf", timeout: 10000 });
  await atShade(100).then(() => shot("100ms"), () => {});
  await atShade(500).then(() => shot("500ms"), () => {});
  await page.waitForFunction(() => window.__shadeDone, null, { timeout: 30000 });
  await shot("after");
  const samples = await page.evaluate(() => window.__shade);
  const on = samples.filter((s) => s.on);
  const first = on[0];
  const shown = (s) => s && JSON.stringify(s);
  console.error(`    ${named(kind)}: ${samples.length} frames, shade on in ${on.length}; first ${shown(first)}; last on ${shown(on.at(-1))}`);
  check(named(`${kind}: the shade is on at full strength, over the whole window`), Boolean(first) && first.opacity > 0.99
    && on.every((s) => s.window), shown(first));
  check(named(`${kind}: the leaving card's copy plays over it, the next step under it`),
    on.some((s) => s.copies > 0 && s.opacity > 0.99 && s.step === next) && on.every((s) => s.copiesOver),
    shown(on.find((s) => s.copies > 0 && !s.copiesOver) || on.find((s) => s.copies > 0)) || "no frame with a copy");
  check(named(`${kind}: clicks go through the shade`), on.every((s) => s.events === "none"), shown(on.find((s) => s.events !== "none")));
  const rising = on.findIndex((s, i) => i > 0 && s.opacity > on[i - 1].opacity + 0.001);
  check(named(`${kind}: the shade only lightens`), rising === -1, shown(on[rising]));
  // A loaded host can draw no frame for a second while a close builds its pieces, so the fade is read from the
  // shade's own keyframes: full strength, then an eased fall to nothing over at least 400 ms.
  const fade = on.at(-1) && on.at(-1).fade;
  check(named(`${kind}: it lightens smoothly, over at least 400 ms`), Boolean(fade) && fade.from === 1 && fade.to === 0 && fade.ms >= 400
    && fade.easing !== "linear", JSON.stringify(fade));
  if (force) {
    const played = await page.evaluate(() => lastClose);
    // Tear's pieces fly for over a second: the shade holds past the 0.6 s it takes when it cannot measure.
    const longest = Math.max(...on.map((s) => s.duration || 0));
    check(named(`${kind}: ${force} plays, and the shade waits for its card to leave`), played === force && longest >= 1200,
      `${played}; longest ${longest}`);
  }
  const last = samples.at(-1);
  check(named(`${kind}: the shade is off the page when it ends`), !last.on, shown(last));
  // The shade lifts 500 ms after the leaving card is out of sight, which the close measures (Coin's is gone after
  // 0.2 s); 0.6 s after the press when it cannot (a Send, the GPU closes), at most 2 s.
  check(named(`${kind}: it lasts 0.65 to 2.5 s on its own clock, and no longer than it says`),
    on.every((s) => s.duration >= 650 && s.duration <= 2500 && s.time <= s.duration + 50), shown(on.at(-1)));
}

// The shade darkens the whole page: a pixel of the card's empty part, of the margins left and right of the
// card, and of the rail at the left (the page at / has no header; the rail holds what the header held), each with the
// shade held at full strength over it and with the shade gone. Starts the shade on the card shown, with no close over
// it.
async function shades(page, check, named) {
  const spots = await page.evaluate(() => {
    const card = document.querySelector("#next > .next-card").getBoundingClientRect();
    const rail = document.querySelector(".rail").getBoundingClientRect();
    const pixel = (x, y) => ({ x: Math.round(x), y: Math.round(y), width: 1, height: 1 });
    const y = card.top + card.height * 0.6;
    return {
      card: pixel(card.right - 40, y),
      "left margin": card.left - rail.right > 8 ? pixel((rail.right + card.left) / 2, y) : null,
      "right margin": innerWidth - card.right > 8 ? pixel((card.right + innerWidth) / 2, y) : null,
      rail: rail.width > 0 && rail.height > 0 ? pixel(rail.left + rail.width / 2, rail.bottom - 120) : null,
    };
  });
  for (const [name, spot] of Object.entries(spots)) {
    if (!spot) check(named(`the page shows the ${name} to test`), false, JSON.stringify(spots));
  }
  for (const name of Object.keys(spots)) if (!spots[name]) delete spots[name];
  const held = await page.evaluate(() => {
    Motion.coverNext();
    const layer = document.querySelector(".arrive-shade");
    const anim = layer && layer.getAnimations()[0];
    if (!anim) return false;
    anim.pause();
    anim.currentTime = 0;
    return true;
  });
  const dim = {};
  for (const [name, spot] of Object.entries(spots)) dim[name] = luminance(await page.screenshot({ clip: spot }));
  await page.evaluate(() => {
    const layer = document.querySelector(".arrive-shade");
    if (layer) for (const anim of layer.getAnimations()) anim.finish();
  });
  await page.waitForFunction(() => !document.querySelector(".arrive-shade"), null, { timeout: 5000 }).catch(() => {});
  for (const [name, spot] of Object.entries(spots)) {
    const lit = luminance(await page.screenshot({ clip: spot }));
    check(named(`the shade darkens the ${name}`), held && lit - dim[name] >= 8, JSON.stringify({ held, dim: dim[name], lit, spot }));
  }
}

// A press anywhere while the shade is on lifts it within a moment (Undo, another item: the section may show another
// thing), and it leaves no listener behind.
async function lifts(page, check, named) {
  // Held at full strength, so only the press can end it within the wait: on its own it would end after 1.1 s.
  await page.evaluate(() => {
    Motion.coverNext();
    const layer = document.querySelector(".arrive-shade");
    if (layer) for (const anim of layer.getAnimations()) anim.pause();
  });
  await page.mouse.click(1100, 600);
  const gone = await page.waitForFunction(() => !document.querySelector(".arrive-shade"), null, { timeout: 3000 })
    .then(() => true, () => false);
  check(named("a press lifts the shade at once"), gone, JSON.stringify({ gone }));
}

// The mean of a one-pixel PNG's red, green and blue, 0 to 255. Every PNG filter predicts 0 for a lone pixel, so the
// inflated scanline is its filter byte and then the pixel as it is.
function luminance(png) {
  const zlib = require("node:zlib");
  const chunks = [];
  for (let at = 8; at < png.length;) {
    const size = png.readUInt32BE(at);
    const type = png.toString("ascii", at + 4, at + 8);
    if (type === "IDAT") chunks.push(png.subarray(at + 8, at + 8 + size));
    at += size + 12;
  }
  const row = zlib.inflateSync(Buffer.concat(chunks));
  return (row[1] + row[2] + row[3]) / 3;
}

// The harness's cookie file, once it is written and the server answers.
async function harnessUp(work, server) {
  const file = path.join(work, "cookie.json");
  const end = Date.now() + 90e3;
  while (Date.now() < end) {
    if (server.exitCode !== null) throw new Error(`serve_slow.py exited with ${server.exitCode}`);
    if (fs.existsSync(file)) {
      const info = JSON.parse(fs.readFileSync(file, "utf8"));
      const up = await fetch(`${info.origin}/healthz`).then((response) => response.ok, () => false);
      if (up) return info;
    }
    await sleep(200);
  }
  throw new Error("serve_slow.py did not come up within 90 s");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
