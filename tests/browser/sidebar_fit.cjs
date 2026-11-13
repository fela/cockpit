// Checks, at laptop and phone width: a step's reply box with no session (no checkout or start picks, Send starts a
// session in the project's checkout, Take back), the Cockpit tag on the item's sidebar row, the rail's keys and foot,
// the group headings' bands, the phone's list screen, and the card's menu of the task's sessions.

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
    // Two checkouts, the project's own second: a step's reply box starts a new session there, and not in the first.
    for (const name of ["alpha-repo", "cockpit"]) fs.mkdirSync(path.join(work, "checkouts", name, ".git"), { recursive: true });
    const cookie = { cookie: `${info.name}=${info.value}`, origin: info.origin, "content-type": "application/json" };
    const agent = (method, url, body) => call(info.origin, AGENT, method, url, body);
    const user = (method, url, body) => call(info.origin, cookie, method, url, body);

    // The fixture database's open steps are snoozed and its timer paused, so this run's steps lead.
    const initial = await user("GET", "/api/state");
    if (initial.timer) await user("POST", `/api/steps/${initial.timer.step_id}/timer/pause`);
    await Promise.all([...initial.queue, ...initial.ready_next, ...initial.snoozed].map((step) => user("POST", `/api/steps/${step.id}/snooze`, { until: "2099-01-01T08:00:00Z" })));
    browser = await chromium.launch({ executablePath: CHROMIUM });

    for (const width of [1440, 390]) {
      const stamp = `${width}-${Date.now()}`;
      // A step with no session: its reply box starts a new session in the project's checkout, at the defaults.
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

      // The box offers no checkout and no start picks (SPEC SESSION-22), and Take back puts the words back into it
      // (on the classic page, where Next up shows the step): from the notice after Send; from You handed these
      // after a reload, where the page holds nothing of the send; and a Take back the page left on its way at a
      // reload, which the next page sends (errors.js).
      await page.goto(`${info.origin}/classic`, { timeout: 90000 });
      for (const how of ["notice", "reload", "left"]) {
        const card = page.locator(`#next article[data-step="${id}"]`).first();
        await card.waitFor({ timeout: 90000 });
        const box = card.locator(`textarea[data-key="${draftKey}"]`);
        // A checkout and picks the page kept for this box from before give way to the defaults.
        if (how === "notice") {
          await page.evaluate(([key, cwd]) => {
            ui.choices.set(key, `new:${cwd}`);
            ui.starts.set(key, { provider: null, model: null, effort: "low", account: null });
            saveDrafts();
            render(false);
          }, [draftKey, path.join(work, "checkouts", "alpha-repo")]);
        }
        await box.fill(`Start on it ${width}`);
        const shown = await card.evaluate((el) => {
          const reply = el.querySelector(".composer:not(.feedback-box)");
          return {
            pickers: reply.querySelectorAll("select, .composer-target, .start-button").length,
            // motion.js moves a title to data-tip.
            send: reply.querySelector("button.send").dataset.tip || reply.querySelector("button.send").title,
            done: el.querySelector("[data-done-step] .label")?.textContent || null,
          };
        });
        check(`${width}, ${how}: a step's reply box without a session shows no checkout and no start picks`,
          shown.pickers === 0 && shown.send === "Send: a new session takes the step until it needs you", JSON.stringify(shown));
        check(`${width}, ${how}: with words typed and no session to send them to, the key stays Task done`,
          shown.done === "Task done", JSON.stringify(shown));
        const handed = page.waitForRequest((request) => request.method() === "POST" && request.url().endsWith(`/api/steps/${id}/hand-off`));
        await card.locator(".composer:not(.feedback-box) button.send").first().click();
        const sentTo = JSON.parse((await handed).postData());
        check(`${width}, ${how}: Send starts the new session in the project's checkout, at the defaults`,
          sentTo.new_cwd === path.join(work, "checkouts", "cockpit") && sentTo.model == null && sentTo.effort == null && sentTo.account == null,
          JSON.stringify(sentTo));
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
          words: document.querySelector(`#next textarea[data-key="${key}"]`)?.value || "",
          pickers: document.querySelectorAll(`#next [data-key="${key}:to"], #next .start-button[data-key^="${key}:"]`).length,
          choice: ui.choices.get(key) || null,
          starts: ui.starts.get(key) || null,
        }), draftKey);
        check(`${width}, Take back from the ${how}: puts the words back into a box that still offers no picks`,
          back.words.includes(`Start on it ${width}`) && back.pickers === 0 && !back.starts && String(back.choice).endsWith("/cockpit"),
          JSON.stringify(back));
        const toastText = await page.locator("#toast .toast-text").textContent();
        const said = how === "left" ? "Taken back from the page you left" : "Taken back. Your message was not sent";
        check(`${width}, Take back from the ${how}: the message was not sent`, toastText.startsWith(said), toastText);
      }

      // A Take back left on its way and answered on Usage, which has no reply boxes (errors.js): a box that holds
      // another To by then keeps it, and the words stay in the notice.
      const elsewhere = `session:${SESSION_A}`;
      {
        const card = page.locator(`#next article[data-step="${id}"]`).first();
        await card.waitFor({ timeout: 90000 });
        await card.locator(`textarea[data-key="${draftKey}"]`).fill(`Meant for a new session on Usage ${width}`);
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
        // Meanwhile the box writes to a session and holds other words.
        await page.evaluate(([key, choice]) => {
          ui.choices.set(key, choice);
          ui.drafts.set(key, "Words for the session");
          saveDrafts();
        }, [draftKey, elsewhere]);
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
          stored.draft === "Words for the session" && stored.choice === elsewhere && !stored.starts
            && stored.toast.includes("writes to another") && stored.toast.includes(`Meant for a new session on Usage ${width}`),
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
          // A row on the phone's list screen is 40 px tall; one line of words still.
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
          const bounds = document.querySelector(".rail").getBoundingClientRect();
          const out = [...document.querySelectorAll(".rail-actions > *, .rail-keys > *")].filter((el) => el.getClientRects().length)
            .filter((el) => { const r = el.getBoundingClientRect(); return r.right > bounds.right + 0.5 || r.left < bounds.left - 0.5; })
            .map((el) => el.id || el.className);
          const style = getComputedStyle(reload.querySelector(".label"));
          const labelHidden = style.display === "none" || (style.clipPath === "inset(50%)" && style.width === "1px" && style.height === "1px");
          const top = Boolean(reload.closest(".rail-top"));
          reload.hidden = true;
          return { out, labelHidden, top, name: reload.getAttribute("aria-label") };
        });
        check(`${width}: with Reload showing, the rail's keys stay inside the rail`, keys.out.length === 0, JSON.stringify(keys.out));
        check(`${width}: Reload shows as its icon in the rail, named Reload`, keys.labelHidden && keys.name === "Reload", JSON.stringify(keys));
        check(`${width}: Reload sits at the rail's top, so the foot's usage keeps one line (UI-13)`, keys.top, JSON.stringify(keys));
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

        // At every width a group's heading is a band across the list in the band's colour, with its fold arrow at the
        // band's right end (SPEC UI-86): 28 px tall in the rail, 36 px on the phone's list screen.
        const bands = await page.evaluate(() => {
          const list = document.getElementById("sidebar").getBoundingClientRect();
          const probe = document.body.appendChild(document.createElement("i"));
          probe.style.background = "var(--group-band)";
          const colour = getComputedStyle(probe).backgroundColor;
          probe.remove();
          return {
            colour, left: list.left, right: list.right,
            headings: [...document.querySelectorAll(".sidebar-group > summary:not(.holding)")].map((el) => {
              const r = el.getBoundingClientRect();
              const name = el.querySelector(".sidebar-group-name").getBoundingClientRect();
              const arrow = getComputedStyle(el, "::before");
              return { height: r.height, left: r.left, right: r.right, colour: getComputedStyle(el).backgroundColor,
                position: getComputedStyle(el).position, nameRight: name.right, arrowOrder: arrow.order, arrowWidth: arrow.width };
            }),
          };
        });
        const bandHeight = width <= 1000 ? 36 : 28;
        check(`${width}: a group's heading is a ${bandHeight} px band across the list that stays in view, its arrow at the right`,
          bands.headings.length >= 2 && bands.colour !== "rgba(0, 0, 0, 0)" && bands.headings.every((r) => r.colour === bands.colour
            && Math.abs(r.height - bandHeight) <= 0.5 && Math.abs(r.left - bands.left) <= 0.5 && Math.abs(r.right - bands.right) <= 0.5
            && r.position === "sticky" && r.arrowOrder === "10" && r.arrowWidth === "6px"), JSON.stringify(bands));
      }

      // The phone's list screen (SPEC UI-86): the menu key, Search and the keys at the right on one line, Priorities
      // and the list under it, rows 40 px tall that touch, and each group's heading a 36 px band across the screen.
      if (width === 390) {
        await page.waitForFunction(() => document.body.classList.contains("phone-list"));
        const screen = await page.evaluate(() => {
          const box = (el) => el.getBoundingClientRect();
          const brand = box(document.querySelector(".rail .brand-menu"));
          const search = box(document.querySelector(".rail-search"));
          const actions = box(document.querySelector(".rail-actions"));
          // Every group opens while the rows are measured, as the groups open by default hold one row here; each goes
          // back to how it was after.
          const groups = [...document.querySelectorAll(".sidebar-group")].map((el) => [el, el.open]);
          for (const [el] of groups) el.open = true;
          const rows = [...document.querySelectorAll(".sidebar-list > li.side-item > .side-row")];
          const seen = {
            middles: [brand, search, actions].map((r) => r.top + r.height / 2),
            order: [brand.left, brand.right, search.left, search.right, actions.left, actions.right],
            lineBottom: Math.max(brand.bottom, search.bottom, actions.bottom),
            goals: document.getElementById("goals").getClientRects().length ? box(document.getElementById("goals")).top : null,
            list: box(document.getElementById("sidebar")).top,
            rows: rows.map((el) => box(el).height),
            // The gap from each row to the next row in its list.
            gaps: rows.filter((el) => el.parentElement.nextElementSibling?.matches("li.side-item"))
              .map((el) => box(el.parentElement.nextElementSibling.querySelector(":scope > .side-row")).top - box(el).bottom),
            headings: [...document.querySelectorAll(".sidebar-group > summary")].map((el) => box(el))
              .map((r) => ({ height: r.height, left: r.left, right: r.right })),
            width: innerWidth,
            scrollWidth: document.documentElement.scrollWidth,
          };
          for (const [el, open] of groups) el.open = open;
          return seen;
        });
        const [brandLeft, brandRight, searchLeft, searchRight, actionsLeft, actionsRight] = screen.order;
        check(`${width}: on the list screen the menu key, Search and the keys at the right share one line, Search between`,
          Math.max(...screen.middles) - Math.min(...screen.middles) <= 2 && brandRight <= searchLeft && searchRight <= actionsLeft
            && brandLeft >= 0 && actionsRight <= screen.width, JSON.stringify(screen));
        check(`${width}: on the list screen Priorities and the list start below that line`,
          screen.goals !== null && screen.goals >= screen.lineBottom - 0.5 && screen.list >= screen.lineBottom - 0.5, JSON.stringify(screen));
        check(`${width}: on the list screen a row is 40 px tall and the next row starts where it ends`,
          screen.rows.length >= 2 && screen.rows.every((height) => Math.abs(height - 40) <= 0.5)
            && screen.gaps.length >= 1 && screen.gaps.every((gap) => Math.abs(gap) <= 0.5), JSON.stringify({ rows: screen.rows, gaps: screen.gaps }));
        check(`${width}: on the list screen a group's heading is 36 px tall and spans the screen`,
          screen.headings.length >= 1 && screen.headings.every((r) => Math.abs(r.height - 36) <= 0.5 && Math.abs(r.left) <= 0.5
            && Math.abs(r.right - screen.width) <= 0.5), JSON.stringify(screen.headings));
        check(`${width}: the list screen does not scroll sideways`, screen.scrollWidth <= screen.width,
          JSON.stringify({ scrollWidth: screen.scrollWidth, width: screen.width }));

        const scrolled = await page.evaluate(() => {
          // The fixture's groups are too short to scroll the screen, so the first open group gets 40 copies of its
          // first row, and the page scrolls 400 px past that group's heading; the copies go and the page scrolls back
          // to its top before the page draws again.
          const group = document.querySelector(".sidebar-group[open]");
          const first = group.querySelector(".sidebar-list > li.side-item");
          const copies = Array.from({ length: 40 }, () => first.cloneNode(true));
          first.parentElement.append(...copies);
          const heading = group.querySelector(":scope > summary");
          const to = scrollY + heading.getBoundingClientRect().top + 400;
          window.scrollTo({ top: to, behavior: "instant" });
          const seen = { to, at: scrollY, heading: heading.getBoundingClientRect().top,
            foot: document.querySelector(".rail-foot").getBoundingClientRect().bottom, height: innerHeight };
          for (const copy of copies) copy.remove();
          window.scrollTo({ top: 0, behavior: "instant" });
          return seen;
        });
        check(`${width}: scrolled down a long open group, its heading stays at the screen's top and the foot at its bottom`,
          Math.abs(scrolled.at - scrolled.to) <= 1 && Math.abs(scrolled.heading) <= 1 && Math.abs(scrolled.foot - scrolled.height) <= 1,
          JSON.stringify(scrolled));
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
