
"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder } = require("./work.cjs");

// Browser dependencies can be selected through the environment (README.md).
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE || "playwright";
const CHROMIUM = process.env.BROWSER_EXECUTABLE || undefined;

const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
// The harness's agent token and its two fake sessions (tests/conftest.py).
const SESSION_A = "11111111-1111-4111-8111-111111111111";
const SESSION_B = "22222222-2222-4222-8222-222222222222";
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "sidebar-fit-test", "content-type": "application/json" };

const { chromium } = require(PLAYWRIGHT);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const shots = process.argv[2] || null;
  if (shots) fs.mkdirSync(shots, { recursive: true });
  const work = workFolder("cockpit-sidebar-fit-");
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], { stdio: ["ignore", "inherit", "inherit"] });
  let browser = null;
  const results = [];
  const check = (name, ok, detail) => {
    results.push({ name, ok });
    console.error(`  ${ok ? "pass" : "FAIL"} ${name}${ok ? "" : `: ${detail}`}`);
  };
  try {
    const info = await harnessUp(work, server);
    // A second checkout, so the checkout picked under To is not the box's default.
    fs.mkdirSync(path.join(work, "checkouts", "other-repo", ".git"), { recursive: true });
    const cookie = { cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json" };
    const agent = (method, url, body) => call(info.origin, AGENT, method, url, body);
    const user = (method, url, body) => call(info.origin, cookie, method, url, body);

    // The fixture database's open steps are snoozed and its timer paused, so this run's steps lead.
    const copied = await user("GET", "/api/state");
    if (copied.timer) await user("POST", `/api/steps/${copied.timer.step_id}/timer/pause`);
    await Promise.all([...copied.queue, ...copied.ready_next, ...copied.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    browser = await chromium.launch({ executablePath: CHROMIUM });

    for (const width of [1440, 390]) {
      const stamp = `${width}-${Date.now()}`;
      // A step with no session: its reply box picks where a new session starts.
      const key = `note:sidebar-fit-${stamp}`;
      await agent("PUT", "/api/items", { key, title: "Sidebar fit test", priority: "urgent", project: "cockpit" });
      const answer = await agent("POST", "/api/steps", { item_key: key, need: `decide:fit-${stamp}`, ask: "Sidebar fit test", score: 100 });
      const id = answer.step.id;
      const draftKey = `msg:step:${id}`;

      const context = await browser.newContext({ viewport: { width, height: 900 }, isMobile: width < 600, hasTouch: width < 600 });
      await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
      const page = await context.newPage();
      page.setDefaultTimeout(20000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });

      // Take back keeps the picked checkout and start picks (on the classic page, where Next up shows the step):
      // from the notice after Send; from You handed these after a reload, where the page holds nothing of the send;
      // and a Take back the page left on its way at a reload, which the next page sends (errors.js).
      await page.goto(`${info.origin}/classic`, { timeout: 90000 });
      for (const [round, how] of ["notice", "reload", "left"].entries()) {
        const card = page.locator(`#next article[data-step="${id}"]`).first();
        await card.waitFor({ timeout: 90000 });
        const box = card.locator(`textarea[data-key="${draftKey}"]`);
        await box.fill(`Start on it ${width}`);
        const to = card.locator(`select[data-key="${draftKey}:to"]`);
        const options = await to.locator("option").evaluateAll((all) => all.map((option) => option.value));
        // Another checkout than the box's default, and another effort than the last round's.
        const picked = options[options.length - 1];
        await to.selectOption(picked);
        const effort = card.locator(`.start-pick:has(> .start-button[data-key="${draftKey}:effort"])`);
        await effort.locator(".start-button").click();
        const efforts = await effort.locator(".menu-list button").evaluateAll((all) => all.map((button) => button.dataset.value));
        const pickedEffort = efforts[round % efforts.length];
        await effort.locator(`.menu-list button[data-value="${pickedEffort}"]`).click();
        await card.locator(".composer:not(.feedback-box) button.send").first().click();
        const notice = page.locator("#toast button", { hasText: "Take back" });
        await notice.waitFor();
        if (how === "notice") {
          await notice.click();
        } else if (how === "left") {
          // The answer never comes to this page: it reloads with the Take back in its outbox.
          const held = `**/api/steps/${id}/return`;
          let pressed = null;
          const sent = new Promise((resolve) => { pressed = resolve; });
          // Hold only this page's request through navigation; removing the route would release it.
          await page.route(held, () => pressed(), { times: 1 });
          await notice.click();
          await sent;
          await page.reload({ timeout: 90000 });
          await page.unroute(held);
        } else {
          await page.reload({ timeout: 90000 });
          const handed = page.locator(`.with-agents li[data-step="${id}"] button[aria-label^="Take back"]`).first();
          await handed.waitFor({ state: "attached", timeout: 90000 });
          // Busy and Waiting start folded (VIEW-27): open the group that holds the handed step.
          await handed.evaluate((el) => { for (let at = el.closest("details"); at; at = at.parentElement.closest("details")) at.open = true; });
          await handed.click();
        }
        await page.waitForFunction((key) => document.querySelector(`#next textarea[data-key="${key}"]`)?.value, draftKey, { timeout: 20000 }).catch(() => {});
        const back = await page.evaluate((key) => ({
          to: document.querySelector(`#next select[data-key="${key}:to"]`)?.value || null,
          choice: ui.choices.get(key) || null,
          starts: ui.starts.get(key) || null,
          line: document.querySelector(`#next .start-button[data-key="${key}:effort"]`)?.textContent.trim() || "",
        }), draftKey);
        check(`${width}, Take back from the ${how}: puts back the checkout picked under To`,
          options.length > 1 && picked.endsWith("other-repo") && back.to === picked && back.choice === picked, JSON.stringify({ options, ...back }));
        check(`${width}, Take back from the ${how}: puts back the start line's picks`,
          back.starts && back.starts.effort === pickedEffort && back.line === pickedEffort, JSON.stringify({ pickedEffort, ...back }));
        const toastText = await page.locator("#toast .toast-text").textContent();
        const said = how === "left" ? "Taken back from the page you left" : "Taken back. Your message was not sent";
        check(`${width}, Take back from the ${how}: the message was not sent`, toastText.startsWith(said), toastText);
      }

      // A box the user sets to another To while Take back is on its way keeps it; the words stay in the notice.
      {
        const card = page.locator(`#next article[data-step="${id}"]`).first();
        const to = card.locator(`select[data-key="${draftKey}:to"]`);
        const options = await to.locator("option").evaluateAll((all) => all.map((option) => option.value));
        await card.locator(`textarea[data-key="${draftKey}"]`).fill(`Meant for other-repo ${width}`);
        await to.selectOption(options[options.length - 1]);
        await card.locator(".composer:not(.feedback-box) button.send").first().click();
        const notice = page.locator("#toast button", { hasText: "Take back" });
        await notice.waitFor();
        const held = `**/api/steps/${id}/return`;
        let caught = null;
        const sent = new Promise((resolve) => { caught = resolve; });
        await page.route(held, (route) => caught(route));
        await notice.click();
        const route = await sent;
        // The card is under You handed these meanwhile, so the box's To changes as another view of the box sets it.
        await page.evaluate(([key, choice]) => { ui.choices.set(key, choice); }, [draftKey, options[0]]);
        await route.continue();
        await page.waitForFunction(() => (document.querySelector("#toast .toast-text")?.textContent || "").includes("writes to another"), null, { timeout: 20000 }).catch(() => {});
        await page.unroute(held);
        const kept = await page.evaluate((key) => ({
          choice: ui.choices.get(key) || null, draft: ui.drafts.get(key) || "", starts: ui.starts.get(key) || null,
          toast: document.querySelector("#toast .toast-text")?.textContent || "",
        }), draftKey);
        check(`${width}: a box set to another To while Take back waits keeps that To, and not the words or picks`,
          kept.choice === options[0] && !kept.draft.includes("Meant for other-repo") && !kept.starts && kept.toast.includes("writes to another"),
          JSON.stringify(kept));
      }

      // The same, with the Take back left on its way and answered on Usage, which has no reply boxes (errors.js).
      {
        const card = page.locator(`#next article[data-step="${id}"]`).first();
        await card.waitFor({ timeout: 90000 });
        const to = card.locator(`select[data-key="${draftKey}:to"]`);
        const options = await to.locator("option").evaluateAll((all) => all.map((option) => option.value));
        await card.locator(`textarea[data-key="${draftKey}"]`).fill(`Meant for other-repo on Usage ${width}`);
        await to.selectOption(options[options.length - 1]);
        await card.locator(".composer:not(.feedback-box) button.send").first().click();
        const notice = page.locator("#toast button", { hasText: "Take back" });
        await notice.waitFor();
        const held = `**/api/steps/${id}/return`;
        let pressed = null;
        const sent = new Promise((resolve) => { pressed = resolve; });
        // The next page sends the retry, while this page's request stays held until navigation.
        await page.route(held, () => pressed(), { times: 1 });
        await notice.click();
        await sent;
        // Meanwhile the box writes to the default checkout and holds other words.
        await page.evaluate(([key, choice]) => {
          ui.choices.set(key, choice);
          ui.drafts.set(key, "Words for the default checkout");
          saveDrafts();
        }, [draftKey, options[0]]);
        await page.goto(`${info.origin}/usage`, { timeout: 90000 });
        await page.unroute(held);
        await page.waitForFunction(() => (document.querySelector("#toast")?.textContent || "").includes("writes to another"), null, { timeout: 20000 }).catch(() => {});
        const stored = await page.evaluate((key) => {
          const read = (name) => new Map(JSON.parse(localStorage.getItem(name) || "[]")).get(key) || null;
          return {
            draft: read("cockpit.drafts"), choice: read("cockpit.draft-targets"), starts: read("cockpit.draft-starts"),
            toast: document.querySelector("#toast")?.textContent || "",
          };
        }, draftKey);
        check(`${width}: a Take back answered on Usage leaves a box set to another To as it is, and says so`,
          stored.draft === "Words for the default checkout" && stored.choice === options[0] && !stored.starts
            && stored.toast.includes("writes to another") && stored.toast.includes(`Meant for other-repo on Usage ${width}`),
          JSON.stringify(stored));
        // The next part starts from an empty box.
        await page.evaluate((key) => {
          for (const name of ["cockpit.drafts", "cockpit.draft-targets"]) {
            const all = new Map(JSON.parse(localStorage.getItem(name) || "[]"));
            all.delete(key);
            localStorage.setItem(name, JSON.stringify([...all]));
          }
        }, draftKey);
      }

      // The sidebar page: the tag on the item's row, the rail's keys, and the names usage counts give.
      const batches = [];
      await page.route("**/api/control-uses", async (route) => {
        batches.push(route.request().postData() || "");
        await route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
      });
      await agent("PUT", "/api/items", { key, sessions: [SESSION_B, SESSION_A] });
      await page.goto(`${info.origin}/`, { timeout: 90000 });
      const row = page.locator(`#sidebar li[data-item="${answer.step.item_id}"] > .side-row`).first();
      await row.waitFor({ state: "attached", timeout: 90000 });
      // On a phone the item and the list are two screens (UI-56): the item screen's bar opens the list.
      if (await page.locator(".phone-back").isVisible()) await page.locator(".phone-back").click();
      // The sidebar's Busy and Waiting start folded (sidebar.js): open the group that holds the row.
      await row.evaluate((el) => { for (let at = el.closest("details"); at; at = at.parentElement.closest("details")) at.open = true; });
      await row.waitFor({ timeout: 15000 });
      // The first draws of a synthetic board replace the row; it is measured once it stays in the page.
      await row.locator(".chip.cockpit").waitFor({ timeout: 15000 }).catch(() => {});
      await sleep(1000);
      const tag = await row.evaluate((el) => {
        const chip = el.querySelector(".chip.cockpit");
        if (!chip || !el.isConnected) return null;
        const box = el.getBoundingClientRect();
        const key = chip.getBoundingClientRect();
        const age = el.querySelector(".sidebar-age").getBoundingClientRect();
        return {
          words: getComputedStyle(chip.querySelector(".chip-words")).display,
          inside: key.left >= box.left && key.right <= box.right + 0.5 && age.right <= box.right + 0.5,
          // A phone's row is 44 px tall, its tap height; one line of words still.
          oneLine: Math.abs(key.top - age.top) < 12 && box.height < (innerWidth <= 640 ? 52 : 40),
        };
      });
      check(`${width}: the Cockpit tag shows on the item's sidebar row, its key alone`,
        tag && tag.words === "none" && tag.inside && tag.oneLine, JSON.stringify(tag));

      const rail = page.locator(".rail");
      if (await rail.isVisible()) {
        const keys = await page.evaluate(() => {
          const reload = document.getElementById("reload");
          reload.hidden = false;
          const holder = document.querySelector(".rail-keys");
          const bounds = document.querySelector(".rail").getBoundingClientRect();
          const out = [...holder.children].filter((el) => el.getClientRects().length)
            .filter((el) => { const r = el.getBoundingClientRect(); return r.right > bounds.right + 0.5 || r.left < bounds.left - 0.5; })
            .map((el) => el.id || el.className);
          const style = getComputedStyle(reload.querySelector(".label"));
          const labelHidden = style.display === "none" || (style.clipPath === "inset(50%)" && style.width === "1px" && style.height === "1px");
          reload.hidden = true;
          return { out, labelHidden, name: reload.getAttribute("aria-label") };
        });
        check(`${width}: with Reload showing, the rail's keys stay inside the rail`, keys.out.length === 0, JSON.stringify(keys.out));
        check(`${width}: Reload shows as its icon in the rail, named Reload`, keys.labelHidden && keys.name === "Reload", JSON.stringify(keys));
        const foot = await page.evaluate(() => {
          const line = document.querySelector(".rail-line");
          const tops = [...line.querySelectorAll("#usage, .rail-keys > *")].filter((el) => el.getClientRects().length)
            .map((el) => el.getBoundingClientRect()).map((r) => r.top + r.height / 2);
          const feedback = document.getElementById("feedback-mode");
          return { height: line.getBoundingClientRect().height, spread: Math.max(...tops) - Math.min(...tops),
            feedback: feedback.parentElement.className, shown: Boolean(feedback.getClientRects().length) };
        });
        check(`${width}: the rail's foot is one line`, foot.height <= 48 && foot.spread < 12, JSON.stringify(foot));
        check(`${width}: Feedback sits beside Add at the rail's top`, foot.feedback === "rail-actions" && foot.shown, JSON.stringify(foot));
      }

      // The card's ⋯ menu lists the step's session first, then the task's other sessions, each opening its
      // conversation (VIEW-29); the sidebar lists no sessions under the row.
      const openMenu = async () => {
        // On a phone the first press slides the item's screen in over the list (UI-56), and the item stays picked.
        if (await row.isVisible()) await row.click();
        const key = page.locator("main .step-context .step-menu > summary").first();
        await key.waitFor({ timeout: 15000 }).catch(() => {});
        if (await key.isVisible().catch(() => false)) await key.click();
      };
      await openMenu();
      const entries = page.locator("main .step-context .step-menu .menu-list .menu-session[data-opens]");
      const viewKey = entries.first();
      const firstId = await viewKey.count() ? await viewKey.getAttribute("data-opens") : "";
      const keyName = firstId ? (await viewKey.getAttribute("aria-label")) : "";
      const sessionName = keyName ? (await viewKey.locator(".menu-name").textContent()).trim() : "";
      const sidebarRows = await page.locator("#sidebar .sidebar-sessions, #sidebar [data-opens]").count();
      check(`${width}: the card's menu names the step's session first, and the sidebar lists no sessions`,
        firstId === SESSION_B && Boolean(sessionName) && sidebarRows === 0, JSON.stringify({ firstId, keyName, sessionName, sidebarRows }));
      if (keyName) await viewKey.click();
      const opened = await page.waitForFunction((id) => sessionView.id === id, SESSION_B, { timeout: 10000 }).then(() => true, () => false);
      check(`${width}: the session's entry opens its conversation in the page`, opened, "the conversation did not open");
      await page.evaluate(() => closeSessionView(false));
      await openMenu();
      const other = page.locator(`main .step-context .step-menu .menu-list .menu-session[data-opens="${SESSION_A}"]`).first();
      const otherShown = await other.isVisible().catch(() => false);
      if (otherShown) await other.click();
      const openedOther = await page.waitForFunction((id) => sessionView.id === id, SESSION_A, { timeout: 10000 }).then(() => true, () => false);
      check(`${width}: the task's other session is in the same menu and opens its conversation`,
        otherShown && openedOther, JSON.stringify({ otherShown, openedOther }));
      await sleep(300);
      await page.evaluate(() => {
        Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
        document.dispatchEvent(new Event("visibilitychange"));
      });
      await sleep(1000);
      const pressed = batches.flatMap((body) => { try { return JSON.parse(body).uses; } catch { return []; } })
        .filter((use) => use.kind === "press").map((use) => use.control);
      const sessionWords = (keyName || "").replace(/^Show the conversation of /, "");
      console.error(`    pressed: ${JSON.stringify(pressed)}`);
      const leaks = pressed.filter((control) => (sessionName && control.includes(sessionName)) || (sessionWords && control.includes(sessionWords)));
      // Named by place alone, so every session's button counts as one control.
      check(`${width}: a session's button counts without the session's name (${sessionName || "no button"}; ${sessionWords || "no key"})`,
        pressed.some((control) => control.endsWith("session-key")) && leaks.length === 0
          && Boolean(sessionName) && Boolean(sessionWords), JSON.stringify({ pressed, leaks }));
      if (shots) await page.screenshot({ path: path.join(shots, `sidebar-fit-${width}.png`) });

      check(`${width}: no page errors`, errors.length === 0, errors.join(" | "));
      await context.close();
      // The next width's step leads Next up.
      await user("POST", `/api/steps/${id}/snooze`, { until: "2099-01-01T08:00:00Z" });
    }
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
  const failedChecks = results.filter((result) => !result.ok);
  console.error(failedChecks.length ? `${failedChecks.length} of ${results.length} checks failed` : `all ${results.length} checks passed`);
  process.exitCode = failedChecks.length ? 1 : 0;
}

async function call(origin, headers, method, url, body) {
  const response = await fetch(`${origin}${url}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await response.text();
  if (!response.ok) throw new Error(`${method} ${url}: ${response.status} ${text}`);
  return text ? JSON.parse(text) : null;
}

// The harness's cookie file, once it is written and the server answers.
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
