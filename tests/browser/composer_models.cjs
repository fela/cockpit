// Model/effort choices, capacity warnings and Take back on existing-session composers. Synthetic Tasks records only.
// Every message, step hand-off and session handover is intercepted; no real agent runs.
// PLAYWRIGHT_MODULE=/path/to/playwright PYTHON=/path/to/python bun tests/browser/composer_models.cjs [screenshots] [width:case]
"use strict";

const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { workFolder, fullState } = require("./work.cjs");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = process.env.PYTHON || path.join(ROOT, ".venv", "bin", "python");
const SOURCE = "22222222-2222-4222-8222-222222222222";
const TARGET = "codex:33333333-3333-4333-8333-333333333333";
const SETTINGS = { provider: "claude", model: "claude-sonnet-5-5", effort: "medium", speed: null };
const AGENT = { authorization: `Bearer ${"t".repeat(40)}`, "x-cockpit-as": "composer-models-browser", "content-type": "application/json" };
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function harnessUp(work, server) {
  for (const end = Date.now() + 60e3; Date.now() < end; await sleep(100)) {
    if (server.exitCode !== null) throw new Error(`serve_preview.py exited with ${server.exitCode}`);
    const file = path.join(work, "cookie.json");
    if (!fs.existsSync(file)) continue;
    const info = JSON.parse(fs.readFileSync(file, "utf8"));
    if (await fetch(`${info.origin}/healthz`).then((res) => res.ok, () => false)) return info;
  }
  throw new Error("serve_preview.py did not become ready within 60 s");
}

async function agent(info, method, url, body) {
  const response = await fetch(`${info.origin}${url}`, { method, headers: AGENT, body: JSON.stringify(body) });
  assert(response.ok, `${method} ${url}: ${response.status} ${await response.clone().text()}`);
  return response.json();
}

async function eventually(check, message) {
  for (const end = Date.now() + 10e3; Date.now() < end; await sleep(25)) if (await check()) return;
  assert.fail(message);
}

async function main() {
  const work = workFolder("cockpit-composer-models-");
  const shots = process.argv[2] || work;
  fs.mkdirSync(shots, { recursive: true });
  fs.writeFileSync(path.join(work, "providers.json"), JSON.stringify(["claude", "codex"]));
  const server = spawn(PYTHON, [path.join(__dirname, "serve_preview.py"), work], {
    env: { ...process.env, PYTHONPATH: "src" }, stdio: ["ignore", "inherit", "inherit"],
  });
  let browser;
  try {
    const info = await harnessUp(work, server);
    browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE || undefined });
    for (const [width, scheme] of [[1280, "light"], [390, "dark"]]) {
      for (const kind of ["same", "codex", "cross", "step", "capacity", "session"]) {
        if (process.argv[3] && process.argv[3] !== `${width}:${kind}`) continue;
        const key = `note:composer-models-${width}-${kind}`;
        const item = await agent(info, "PUT", "/api/items", { key, title: `Review Tasks reminders (${kind})`, priority: "urgent", sessions: [SOURCE] });
        const step = kind === "step" ? (await agent(info, "POST", "/api/steps", { item_key: key, need: "review", ask: "Choose the weekly reminder", body: "The reminder changes are ready for review.", score: 100, session_id: SOURCE })).step : null;
        const context = await browser.newContext({ viewport: { width, height: 1000 }, colorScheme: scheme,
          isMobile: width < 600, hasTouch: width < 600, reducedMotion: "reduce" });
        await context.setExtraHTTPHeaders({ cookie: `${info.name}=${info.value}` });
        const resetAt = new Date(Date.now() + 3 * 3600e3).toISOString();
        const unavailable = { state: "limited", resets_at: resetAt, remaining_percent: 0, reason: "Synthetic usage limit" };
        const capacities = {
          "claude-sonnet-5-5": { state: "available", resets_at: resetAt, remaining_percent: 80, reason: "Saved account has room" },
          "claude-opus-5-5": { state: "available", resets_at: resetAt, remaining_percent: 80, reason: "Another account has room" },
          "claude-haiku-5-5": { state: "available", resets_at: resetAt, remaining_percent: 80, reason: "Saved account has room" },
          "gpt-6.2-sol": { state: "low", resets_at: resetAt, remaining_percent: 5, reason: "Synthetic remaining usage" },
          "gpt-6.1-terra": { state: "unknown", resets_at: null, remaining_percent: null, reason: "Usage reading is old" },
          "gpt-6-luna": { ...unavailable, resets_at: new Date(Date.now() - 3600e3).toISOString() },
        };
        let sourceSettings = kind === "codex"
          ? { provider: "codex", model: "gpt-6.2-sol", effort: "medium", speed: "fast" }
          : kind === "capacity" ? { ...SETTINGS, capacity: unavailable, model_capacity: { "claude-opus-5-5": unavailable } } : SETTINGS;
        const sourceHandover = kind === "session" ? { state: "sent", session_id: TARGET } : null;
        const requests = [];
        let pending;
        await context.route("**/api/state", async (route) => {
          const full = await fullState(route);
          const state = await full.response.json();
          const patch = (session) => session && session.session_id === SOURCE
            ? { ...session, ...sourceSettings, settings: sourceSettings, name: "Reviewing Tasks reminders", handover: sourceHandover } : session;
          state.sessions = state.sessions.map(patch);
          if (kind === "session") state.sessions.push({ ...state.sessions.find((row) => row.session_id === SOURCE),
            session_id: TARGET, name: "Tasks reminders in Codex", provider: "codex", model: "gpt-6.2-sol", handover: null,
            settings: { provider: "codex", model: "gpt-6.2-sol", effort: "high", speed: "standard" } });
          // Account capacity may differ from a source session that stopped on its own limit.
          for (const choices of Object.values(state.session_options || {})) {
            for (const model of choices.models) model.capacity = kind === "capacity"
              ? capacities[model.name] || { state: "unknown", resets_at: null, remaining_percent: null, reason: "Usage not known" }
              : { state: "unknown", resets_at: null, remaining_percent: null, reason: "Outside this test" };
          }
          for (const group of ["queue", "ready_next", "snoozed", "with_agents"]) {
            for (const row of state[group] || []) if (row.target) row.target = patch(row.target);
          }
          await route.fulfill({ ...full, json: { ...state, providers: ["claude", "codex"], away: null, team: "" } });
        });
        // Intercept both composer endpoints and the obsolete action: even a regression cannot start an agent.
        for (const endpoint of ["**/api/messages", "**/api/steps/*/hand-off", "**/api/sessions/**"]) {
          await context.route(endpoint, async (route) => {
            if (route.request().method() !== "POST") return route.continue();
            const request = { path: new URL(route.request().url()).pathname, body: route.request().postDataJSON() };
            requests.push(request);
            if (request.path.startsWith("/api/sessions/")) return route.fulfill({ status: 409, json: { detail: "Picking next-turn options must not mutate or launch a session" } });
            await new Promise((resolve) => { pending = { request, resolve, answer: null }; });
            const answer = pending.answer;
            pending = null;
            await route.fulfill(answer);
          });
        }
        const page = await context.newPage();
        page.setDefaultTimeout(15e3);
        const errors = [];
        page.on("pageerror", (error) => errors.push(error.message));
        try {
          const openItem = async () => {
            await page.waitForFunction((id) => ui.state?.items.some((row) => row.id === id), item.id);
            await page.evaluate((id) => selectItem(id), item.id);
          };
          await page.goto(`${info.origin}/`, { waitUntil: "load", timeout: 60e3 });
          await openItem();
          if (kind === "session") await page.evaluate(([id, itemId]) => openSessionView(id, itemId), [SOURCE, item.id]);
          const draftKey = kind === "session" ? await page.evaluate(() => sessionKey()) : step ? `msg:step:${step.id}` : `msg:item:${item.id}`;
          const area = page.locator(`textarea[data-key="${draftKey}"]`);
          const composer = page.locator(`.composer:has(textarea[data-key="${draftKey}"])`);
          const pickMenu = (field) => composer.locator(`details[data-turn-pick="${field}"]`);
          const pick = async (field, label) => {
            const menu = pickMenu(field);
            if (!await menu.evaluate((el) => el.open)) await menu.locator("summary").click();
            if (width < 600) {
              const bounds = await menu.locator(".menu-list").boundingBox();
              assert(bounds.x >= 0 && bounds.x + bounds.width <= width + 1, `${field} menu fits the phone: ${JSON.stringify(bounds)}`);
            }
            await menu.getByRole("menuitemradio", { name: label, exact: true }).click();
          };
          const shown = async (model, effort) => {
            assert.equal(await pickMenu("model").locator("summary").getAttribute("aria-label"), `Model: ${model}`);
            assert.equal(await pickMenu("effort").locator("summary").getAttribute("aria-label"), `Effort: ${effort}`);
          };
          if (kind === "session") {
            await area.waitFor();
            await area.focus();
            await shown("Sonnet 5.5", "Medium");
            assert.equal(await page.evaluate(() => sessionView.id), SOURCE, "opening old source history does not follow a completed transfer");
            sourceSettings = { ...SETTINGS, model: "claude-opus-5-5", effort: "high", capacity: unavailable };
            await page.evaluate(async () => { await refresh(); });
            await eventually(async () => (await pickMenu("model").locator("summary").getAttribute("aria-label")) === "Model: Opus 5.5", "open conversation updates its empty composer to current settings");
            await shown("Opus 5.5", "High");
            await composer.locator(".turn-capacity").waitFor({ state: "visible" });
            assert.equal(await area.inputValue(), "", "settings refresh keeps the empty input empty");
            await page.screenshot({ path: path.join(shots, `composer-session-refresh-${width}.png`) });
            const freshWords = `Keep these newly typed words (${width})`;
            await area.fill(freshWords);
            await pick("model", "GPT-6.2 Sol");
            await pick("effort", "High");
            await area.focus();
            await area.evaluate((el) => el.setSelectionRange(5, 12));
            sourceSettings = { ...SETTINGS, capacity: capacities["claude-sonnet-5-5"] };
            await page.evaluate(async () => { await refresh(); });
            await shown("GPT-6.2 Sol", "High");
            assert.equal(await area.inputValue(), freshWords, "source settings refresh preserves a typed draft");
            assert.deepEqual(await area.evaluate((el) => [el.selectionStart, el.selectionEnd]), [5, 12], "source settings refresh preserves selection");
            assert.equal(await composer.locator(".turn-capacity").isVisible(), false, "pending model capacity replaces source-model warning");
            const guarded = await page.evaluate((id) => { sessionView.followFrom = id; return followSessionHandover(); }, SOURCE);
            assert.equal(guarded, false, "a completed transfer waits for newly typed words and choices");
            assert.equal(await page.evaluate(() => sessionView.id), SOURCE);
            assert.equal(await area.inputValue(), freshWords, "the follow guard keeps newly typed words visible");
            await page.screenshot({ path: path.join(shots, `composer-session-protected-draft-${width}.png`) });
            // With no unsent work left, the same recorded transfer may move to the new conversation.
            assert.equal(await page.evaluate((key) => { takeDraft(key); return followSessionHandover(); }, draftKey), true);
            await page.waitForFunction((id) => sessionView.id === id, TARGET);
            assert.equal(await page.locator("#session-composer textarea").inputValue(), "", "following starts with the target conversation's own composer");
            assert.deepEqual(requests, [], "state refresh and following an existing transfer do not send another message");
            assert.deepEqual(errors, [], "session view checks raise no page errors");
            console.log(`PASS ${width}px ${scheme} session: live empty-composer settings/capacity, draft and selection preservation, old history and protected handover following`);
            continue;
          }
          if (kind === "capacity") {
            await area.waitFor();
            assert.equal(await area.inputValue(), "", "capacity is visible before typing");
            const warning = composer.locator(".turn-capacity");
            await warning.waitFor({ state: "visible" });
            assert.match(await warning.textContent(), /Sonnet 5\.5 is unavailable now.*(?:Change model to continue now|Choose another model), or wait/);
            const resetWords = await page.evaluate((at) => when(new Date(at)), resetAt);
            assert((await warning.textContent()).includes(`Resets ${resetWords}.`), "warning shows the known local reset time");
            const red = await page.evaluate(() => {
              const probe = document.createElement("span"); probe.style.color = "var(--fail)";
              document.body.append(probe); const color = getComputedStyle(probe).color; probe.remove(); return color;
            });
            const assertRed = async (node, description) => eventually(async () =>
              await node.evaluate((el) => el.isConnected ? getComputedStyle(el).color : null) === red, description);
            await assertRed(pickMenu("model").locator("summary"), "selected limited model is red");
            await assertRed(warning, "capacity warning uses the red error color");
            await page.screenshot({ path: path.join(shots, `composer-capacity-warning-${width}.png`) });
            await pickMenu("model").locator("summary").click();
            const choice = (name) => pickMenu("model").getByRole("menuitemradio", { name: new RegExp(`^${name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?: |$)`) });
            const limited = choice("Sonnet 5.5");
            const low = choice("GPT-6.2 Sol");
            assert.match(await limited.textContent(), /Unavailable/);
            assert.match(await low.textContent(), /5% left/);
            await assertRed(limited, "limited model option is red");
            await assertRed(low, "near-exhausted model option is red");
            assert.equal(await limited.isDisabled(), false, "user can choose to wait on a limited model");
            assert.equal(await low.isDisabled(), false, "near-exhausted model remains selectable");
            assert.equal(await choice("GPT-6.1 Terra").locator(".turn-capacity-state").count(), 0, "unknown capacity has no red badge");
            assert.equal(await choice("GPT-6 Luna").locator(".turn-capacity-state").count(), 0, "expired capacity has no red badge");
            await page.screenshot({ path: path.join(shots, `composer-capacity-menu-${width}.png`) });
            await limited.click();
            assert.equal(await warning.isVisible(), true, "choosing to wait keeps the warning");
            const choose = async (name) => {
              const menu = pickMenu("model");
              if (!await menu.evaluate((el) => el.open)) await menu.locator("summary").click();
              await choice(name).click();
            };
            await choose("GPT-6.2 Sol");
            assert.match(await warning.textContent(), /nearly out of capacity \(5% left\)/);
            await assertRed(pickMenu("model").locator("summary"), "selected near-exhausted model is red");
            await choose("Opus 5.5");
            assert.equal(await warning.isVisible(), true, "same-provider alternative uses this session's account capacity over pooled available capacity");
            await assertRed(pickMenu("model").locator("summary"), "same-account limited alternative remains red");
            await choose("Haiku 5.5");
            assert.equal(await warning.isVisible(), false, "switching to an available model removes the warning");
            assert.equal(await pickMenu("model").locator("summary").evaluate((el) => el.classList.contains("capacity-warning")), false);
            await page.screenshot({ path: path.join(shots, `composer-capacity-switched-${width}.png`) });
            for (const name of ["GPT-6.1 Terra", "GPT-6 Luna"]) {
              await choose(name);
              assert.equal(await warning.isVisible(), false, `${name}'s unknown or expired capacity is not red`);
              assert.equal(await pickMenu("model").locator("summary").evaluate((el) => el.classList.contains("capacity-warning")), false);
            }
            assert.deepEqual(requests, [], "capacity checks and model switches make no agent requests");
            assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), 0, "capacity controls fit the page");
            assert.deepEqual(errors, [], "capacity state raises no page errors");
            console.log(`PASS ${width}px ${scheme} capacity: empty-composer warning, red selectable options, local reset, available switch and unknown/expired state`);
            continue;
          }
          const words = `Keep the weekly reminder context (${width} ${kind})`;
          await area.fill(words);
          await shown(kind === "codex" ? "GPT-6.2 Sol" : "Sonnet 5.5", "Medium");
          assert.equal(await page.getByRole("menuitem", { name: "Hand to Codex", exact: true }).count(), 0);
          if (step) {
            const menu = page.locator(`#next > [data-step="${step.id}"]:not(.motion-ghost) .step-menu`);
            await menu.locator("summary").click();
            assert.equal(await menu.locator(".menu-session .menu-name").first().textContent(), "Sonnet 5.5", "step menu leads with the current model");
            assert.equal(await menu.getByRole("menuitem", { name: "Hand to Codex", exact: true }).count(), 0);
            await menu.locator("summary").click();
          }
          // One combined model picker presents both providers; choosing only stages the next Send.
          await pickMenu("model").locator("summary").click();
          assert(await pickMenu("model").getByText("Claude", { exact: true }).isVisible());
          assert(await pickMenu("model").getByText("Codex", { exact: true }).isVisible());
          assert.equal(await pickMenu("model").getByRole("menuitemradio", { name: kind === "codex" ? "GPT-6.2 Sol" : "Sonnet 5.5", exact: true }).getAttribute("aria-checked"), "true");
          await page.screenshot({ path: path.join(shots, `composer-models-menu-${width}-${kind}.png`) });
          const cross = kind === "cross" || kind === "step";
          const modelLabel = kind === "codex" ? "GPT-6.1 Terra" : cross ? "GPT-6.2 Sol" : "Opus 5.5";
          await pickMenu("model").getByRole("menuitemradio", { name: modelLabel, exact: true }).click();
          await pick("effort", "High");
          await shown(modelLabel, "High");
          assert.deepEqual(requests, [], "model and effort picks cause no message or handover");
          if (cross) assert.match(await composer.locator(".turn-hint").textContent(), /Continues in Codex/);
          else assert.equal(await composer.locator(".turn-hint").isVisible(), false, "same-provider change has no continuation notice");
          if (width < 600) {
            for (const field of ["model", "effort"]) {
              const bounds = await pickMenu(field).locator("summary").boundingBox();
              assert(bounds.height >= 44 && bounds.width >= 44, `${field} has a 44px phone hit area: ${JSON.stringify(bounds)}`);
            }
          }
          await page.evaluate(() => render(false));
          await shown(modelLabel, "High");
          assert.equal(await area.inputValue(), words, "redraw retains draft");
          await page.reload({ waitUntil: "load" });
          await openItem();
          await shown(modelLabel, "High");
          assert.equal(await area.inputValue(), words, "reload retains draft and pending picks");
          assert.equal(requests.length, 0, "redraw and reload do not send staged choices");
          await page.screenshot({ path: path.join(shots, `composer-models-pending-${width}-${kind}.png`) });
          const expected = { session_id: SOURCE, provider: cross || kind === "codex" ? "codex" : "claude", model: kind === "codex" ? "gpt-6.1-terra" : cross ? "gpt-6.2-sol" : "claude-opus-5-5", effort: "high", ...(kind === "codex" ? { speed: "fast" } : cross ? { speed: "standard" } : {}) };
          const send = async () => {
            await composer.getByRole("button", { name: "Send", exact: true }).click();
            await eventually(() => Boolean(pending), "Send reaches the held message endpoint");
            assert.equal(pending.request.path, step ? `/api/steps/${step.id}/hand-off` : "/api/messages");
            const body = pending.request.body;
            assert.equal(body.text, words);
            assert.deepEqual(body.images, []);
            if (step) {
              for (const [name, value] of Object.entries(expected)) assert.equal(body[name], value, `step sends ${name}`);
            } else assert.deepEqual(body.target, { item_id: item.id, ...expected }, "plain message carries item context and explicit target choices");
          };
          await send();
          assert.equal(requests.length, 1, "Send makes one request");
          pending.answer = { status: 409, json: { detail: "Synthetic delivery refused" } };
          pending.resolve();
          await page.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("Synthetic delivery refused"));
          await openItem();
          await eventually(async () => await area.count() && await area.inputValue() === words, "failed Send restores draft");
          await shown(modelLabel, "High");
          await page.screenshot({ path: path.join(shots, `composer-models-failed-${width}-${kind}.png`) });
          assert.equal(requests.length, 1, "failure does not retry automatically");
          await send();
          assert.equal(requests.length, 2, "explicit retry makes one further request");
          pending.answer = { status: 200, json: { id: 900000 + width, state: "queued", text: words, session_id: SOURCE, created_at: new Date().toISOString() } };
          pending.resolve();
          await eventually(() => !pending, "successful response finishes");
          await eventually(() => page.evaluate((key) => !ui.drafts.get(key), draftKey), "successful Send clears draft");
          if (!step) {
            const restored = await page.evaluate(({ key, text, target }) => {
              const { session_id, provider, ...options } = target;
              const takenBack = unsentTarget({ session_id, new_cwd: null, provider, options });
              const accepted = restoreTarget(key, takenBack);
              if (accepted) restoreDraft(key, text);
              render(false);
              return { accepted, takenBack };
            }, { key: draftKey, text: words, target: expected });
            assert.equal(restored.accepted, true, "Take back restores the original conversation target");
            assert.equal(restored.takenBack.choice, `session:${SOURCE}`);
            for (const [name, value] of Object.entries(expected)) assert.equal(restored.takenBack.picks?.[name], value, `Take back restores ${name}`);
            await openItem();
            await shown(modelLabel, "High");
            assert.equal(await area.inputValue(), words, "Take back puts words and model choices in the same composer");
            assert.equal(requests.length, 2, "restoring a taken-back target sends nothing");
            await area.fill("");
            await page.evaluate((key) => { takeDraft(key); render(false); }, draftKey);
          }
          if (kind === "same") {
            await page.evaluate(([id, itemId]) => openSessionView(id, itemId), [SOURCE, item.id]);
            const header = page.locator(".session-head");
            await header.waitFor();
            assert.equal(await header.getByRole("button", { name: "Hand to Codex", exact: true }).count(), 0, "conversation header has no standalone handover action");
          }
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), 0, "no horizontal page overflow");
          assert.deepEqual(errors, [], "page logs no uncaught errors");
          console.log(`PASS ${width}px ${scheme} ${kind}: pending choices, redraw/reload, explicit target, failure recovery${step ? "" : ", Take back choices"} and one request per Send`);
        } catch (error) {
          await page.screenshot({ path: path.join(shots, `composer-models-error-${width}-${kind}.png`) }).catch(() => {});
          throw error;
        } finally {
          if (pending) { pending.answer = { status: 409, json: { detail: "Test finished" } }; pending.resolve(); }
          await context.unrouteAll({ behavior: "ignoreErrors" });
          await context.close();
        }
      }
    }
    console.log(`Screenshots: ${shots}`);
  } finally {
    if (browser) await browser.close();
    server.kill();
    await new Promise((resolve) => server.exitCode !== null || server.signalCode !== null ? resolve() : server.once("exit", resolve));
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
