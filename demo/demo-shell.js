// The Cockpit demo's shell: it shows Cockpit's own pages in a sandboxed frame and answers every request they make
// from invented data kept in this browser tab. No server, no AI. Nothing leaves the page.
//
// The pages talk to the shell through demo-child.js (fetch, a lock for the page's own state changes, stored
// settings, navigation). Answers to a few steps were recorded once from a real Cockpit with an invented user, and
// the shell plays them back: the step goes to "You handed these", the session works for a few seconds, then the
// recorded next step appears. Every other answer goes to its session, waits, and comes back to the user.
(() => {
  "use strict";

  const DATA = JSON.parse(document.getElementById("demo-data").textContent);
  let view = document.getElementById("demo-view");
  const toastBox = document.getElementById("demo-toast");
  const resetButton = document.getElementById("demo-reset");
  const backButton = document.getElementById("demo-back");

  const STEP_LISTS = ["queue", "ready_next", "snoozed", "with_agents"];
  const QUEUE_LIMIT = 4;
  const DELAY = 20000;
  const WORK_MS = 7000;
  const SECOND = 1000;
  const DAY = 86400000;
  const LONGEST = 24 * DAY;
  const CAPTURE = Date.parse(DATA.captureNow);
  const SAVED = "Demo: Cockpit would save this; the demo only shows it";
  const ASK_ANSWER = "Demo answer: no AI runs here. In Cockpit, a read-only copy of this session would answer your "
    + "question from what the session knows, while the session keeps working.";
  const QUIET = /^\/api\/(page-errors|presence(\/seen)?|control-uses|flashes|sweeps\/run)$/;
  const DAYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"];
  const MESSAGE_KEYS = [
    "as_prompt", "author", "behind_dialog", "completes", "created_at", "detail", "dismiss_reason", "dismissed_at",
    "dismissed_by", "failure", "fork_of", "from_session", "hands_off", "id", "item_id", "new_cwd", "new_name",
    "new_options", "page_answers", "provider", "sent_at", "session_id", "state", "step_ask", "step_id",
    "step_version", "take_back_until", "text", "via", "waits_since",
  ];

  const isoNow = (ms = Date.now()) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
  const clone = (value) => (typeof structuredClone === "function" ? structuredClone(value) : JSON.parse(JSON.stringify(value)));
  const own = (value) => ({ ...value, __demo_own: true });
  const conflict = (detail, status = 409) => ({ j: { detail }, status });

  // ---------------------------------------------------------------------------------------------------------------
  // Time. The data was captured at CAPTURE; every time in it moves so the demo's "now" is the visitor's now.

  const ISO = /^(\d{4})-(\d{2})-(\d{2})(?:([T ])(\d{2}):(\d{2})(?::(\d{2})(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$/;

  function shiftText(text, deltaFor) {
    if (typeof text !== "string" || text.length < 10 || text.length > 40) return text;
    const m = ISO.exec(text);
    if (!m) return text;
    const [, y, mo, d, sep, h, mi, s, frac, zone] = m;
    if (+mo < 1 || +mo > 12 || +d < 1 || +d > 31) return text;
    const pad = (n) => String(n).padStart(2, "0");
    if (!sep) {
      const ms = Date.UTC(+y, +mo - 1, +d) + (CAPTURE % DAY);
      return new Date(ms + deltaFor(ms)).toISOString().slice(0, 10);
    }
    const ms = Date.UTC(+y, +mo - 1, +d, +h, +mi, s ? +s : 0);
    const t = new Date(ms + deltaFor(ms));
    let out = `${t.getUTCFullYear()}-${pad(t.getUTCMonth() + 1)}-${pad(t.getUTCDate())}${sep}${pad(t.getUTCHours())}:${pad(t.getUTCMinutes())}`;
    if (s !== undefined) out += `:${pad(t.getUTCSeconds())}${frac || ""}`;
    return out + (zone || "");
  }

  function walk(value, text, key = (k) => k, field = null) {
    if (typeof value === "string") return text(value, field);
    if (Array.isArray(value)) return value.map((v) => walk(v, text, key, field));
    if (value && typeof value === "object") {
      const out = {};
      for (const [k, v] of Object.entries(value)) out[key(k)] = walk(v, text, key, k);
      return out;
    }
    return value;
  }

  function shifted(value, deltaFor) {
    const t = (x) => shiftText(x, deltaFor);
    return walk(value, t, t);
  }

  // A recorded stage happened after CAPTURE: its own moments land at the visitor's now, older ones move as the
  // base data does.
  function stageDelta(rawAt) {
    const shift = Math.round((Date.now() - Date.parse(rawAt)) / SECOND) * SECOND;
    return (ms) => (ms < CAPTURE ? world.baseDelta : shift);
  }

  // ---------------------------------------------------------------------------------------------------------------
  // Ids. Recorded rows the demo creates are named @@ID:table:n@@; each play gets fresh ids above the data's own.

  const counters = {};
  function nextId(table) {
    const n = counters[table] ?? Math.max(5000, (DATA.maxes[table] || 0) + 1);
    counters[table] = n + 1;
    return n;
  }

  function idFor(ids, table, n) {
    const k = `${table}:${n}`;
    if (!ids.has(k)) ids.set(k, nextId(table));
    return ids.get(k);
  }

  const EXACT = /^@@ID:([a-z_]+):(\d+)@@$/;
  const EMBEDDED = /@@ID:([a-z_]+):(\d+)@@/g;

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }

  function titleOf(text) {
    const line = String(text || "").split("\n").map((l) => l.trim()).find(Boolean) || "";
    if (line.length <= 40) return line;
    let cut = line.slice(0, 39);
    const space = cut.lastIndexOf(" ");
    if (space > 15) cut = cut.slice(0, space);
    return `${cut}…`;
  }

  // The visitor's own words where the recording had QQANSWERQQ, in the form each field holds.
  function answerAs(answer, field) {
    const text = answer == null ? "" : String(answer);
    if (field === "html" || (typeof field === "string" && field.endsWith("_html"))) return escapeHtml(text).replace(/\n/g, "<br>");
    if (field === "title" || field === "item_title") return titleOf(text);
    return text;
  }

  function instantiate(value, ids, answer) {
    return walk(value, (text, field) => {
      const m = EXACT.exec(text);
      if (m) return idFor(ids, m[1], +m[2]);
      let out = text.includes("@@ID:") ? text.replace(EMBEDDED, (_, t, n) => String(idFor(ids, t, +n))) : text;
      if (out.includes("QQANSWERQQ")) out = out.split("QQANSWERQQ").join(answerAs(answer, field));
      return out;
    }, (key) => {
      const m = EXACT.exec(key);
      return m ? String(idFor(ids, m[1], +m[2])) : key;
    });
  }

  function fakeUuid() {
    const part = (n) => Array.from({ length: n }, () => Math.floor(Math.random() * 16).toString(16)).join("");
    return `${part(8)}-${part(4)}-4${part(3)}-a${part(3)}-${part(12)}`;
  }

  // ---------------------------------------------------------------------------------------------------------------
  // Fixtures: the answers to reads that do not change in the demo.

  function fill(template, boot) {
    return template.replace(/@@DEMO:([\w.-]+)@@/g, (whole, name) => (name === "boot" ? (boot === null ? whole : boot) : (DATA.assets[name] ?? "")));
  }

  function fixture(key) {
    const raw = DATA.fixtures[key];
    if (!raw) return null;
    if (!("j" in raw)) return raw;
    if (!world.fixtureCache.has(key)) world.fixtureCache.set(key, { ...raw, j: shifted(raw.j, () => world.baseDelta) });
    return world.fixtureCache.get(key);
  }

  function lookup(path) {
    const pathname = path.split("?")[0];
    const key = path in DATA.fixtures ? path
      : pathname in DATA.fixtures ? pathname
        : Object.keys(DATA.fixtures).find((k) => k.split("?")[0] === pathname);
    return key ? fixture(key) : null;
  }

  function result(r) {
    return "j" in r
      ? { status: r.status || 200, text: JSON.stringify(r.j), ct: "application/json" }
      : { status: r.status || 200, text: r.t ?? "", ct: r.ct || "text/plain" };
  }

  // Whether a row in one of the state's lists is about the item.
  function belongs(entry, itemId) {
    if (typeof entry === "number") return entry === itemId;
    if (entry && typeof entry === "object") {
      if (entry.item_id === itemId) return true;
      return "key" in entry && "title" in entry && entry.id === itemId;
    }
    return false;
  }

  const FIGURES = Object.fromEntries(Object.entries(DATA.fixtures)
    .filter(([k]) => /^\/pages\/\d+\/figure\?comments=[01]$/.test(k))
    .map(([k, v]) => [k, v.t ?? ""]));

  // ---------------------------------------------------------------------------------------------------------------
  // The world: the state the pages read, and what the demo remembers about the visitor's actions.

  let world = null;
  let epochCounter = 0;
  const storage = { local: new Map(), session: new Map() };
  let holder = null;
  const lockQueue = [];
  let pokeTimer = null;
  let toastTimer = null;

  function freshWorld() {
    const baseDelta = Math.round((Date.now() - CAPTURE) / SECOND) * SECOND;
    const state = shifted(DATA.base, () => baseDelta);
    return {
      epoch: ++epochCounter,
      baseDelta,
      state,
      fixtureCache: new Map(),
      timezone: DATA.fixtures["/api/timezone"].j.zone,
      baseCounts: { ...(state.counts || {}) },
      countDelta: {},
      addCount: 0,
      fates: new Map(),
      stepCache: new Map(),
      runs: new Map(),
      deeper: new Map(),
      historyOver: new Map(),
      logExtra: new Map(),
      comments: new Map(),
      choices: new Map(),
      pendingAdd: new Map(),
      lastLock: new Map(),
      kept: new Set(),
      timers: new Set(),
      // How the demo read each snooze typed in words, by target (`readLater`); normalize puts it in the state, since
      // a page's commit replaces the state.
      snoozeReads: new Map(),
    };
  }

  function seedFates() {
    for (const step of world.state.snoozed || []) {
      if (!step.snoozed_until) continue;
      world.fates.set(step.id, { kind: "snoozed", until: step.snoozed_until, queued: false });
      armWake(Date.parse(step.snoozed_until) - Date.now());
    }
  }

  function armWake(ms) {
    if (ms > 0 && ms < LONGEST) later(ms + 50, () => {});
  }

  function makeMessage(fields) {
    const m = {};
    for (const k of MESSAGE_KEYS) m[k] = null;
    Object.assign(m, {
      author: "user", state: "sent", via: "socket", detail: "", created_at: isoNow(), completes: 0, as_prompt: 0,
      hands_off: 0, failure: null, behind_dialog: false,
    }, fields);
    if (m.id == null) m.id = nextId("messages");
    if (m.state === "sent" && !m.sent_at) m.sent_at = m.created_at;
    return m;
  }

  function findStep(s, id) {
    for (const name of STEP_LISTS) {
      const list = s[name] || [];
      const index = list.findIndex((x) => x.id === id);
      if (index >= 0) return { name, index, step: list[index] };
    }
    return null;
  }

  function takeStep(s, id) {
    let found = null;
    for (const name of STEP_LISTS) {
      if (!Array.isArray(s[name])) continue;
      s[name] = s[name].filter((x) => {
        if (x.id !== id) return true;
        if (!found) found = x;
        return false;
      });
    }
    return found;
  }

  function listAt(s, path, create) {
    const parts = path.split("/");
    let here = s;
    for (let i = 0; i < parts.length; i += 1) {
      const k = parts[i];
      if (here[k] == null || typeof here[k] !== "object") {
        if (!create) return null;
        here[k] = i === parts.length - 1 ? [] : {};
      }
      here = here[k];
    }
    return Array.isArray(here) ? here : null;
  }

  function addPart(sessionId, part) {
    if (!sessionId) return;
    if (!world.logExtra.has(sessionId)) world.logExtra.set(sessionId, []);
    world.logExtra.get(sessionId).push(part);
  }

  function itemOf(id) {
    return (world.state.items || []).find((x) => x.id === id) || null;
  }

  function closedRow(step, status, note) {
    const item = itemOf(step.item_id);
    return {
      id: step.id, item_id: step.item_id, ask: step.ask, status, closed_at: isoNow(), closed_by: "user", note: note || "",
      item_title: item?.title ?? null, item_project: item?.project ?? null, ask_html: step.ask_html ?? null,
    };
  }

  // ---------------------------------------------------------------------------------------------------------------
  // Recorded stages: each one is the item's rows as the real Cockpit showed them at that moment.

  function applyStage(run, name) {
    const raw = run.play[name];
    const stage = instantiate(shifted(raw, stageDelta(raw.at)), run.ids, run.answer);
    const s = world.state;
    run.itemId = stage.item_id;
    for (const [path, entries] of Object.entries(stage.lists || {})) {
      const list = listAt(s, path, entries.length > 0);
      if (!list) continue;
      let at = -1;
      for (let i = list.length - 1; i >= 0; i -= 1) {
        if (belongs(list[i], stage.item_id)) {
          list.splice(i, 1);
          at = i;
        }
      }
      if (at < 0) list.push(...entries);
      else list.splice(Math.min(at, list.length), 0, ...entries);
    }
    for (const session of stage.sessions || []) {
      const list = s.sessions || (s.sessions = []);
      const i = list.findIndex((x) => x.session_id === session.session_id);
      if (i >= 0) list[i] = session;
      else list.push(session);
      const linked = s.linked_sessions || (s.linked_sessions = []);
      if (!linked.includes(session.session_id)) linked.push(session.session_id);
    }
    world.historyOver.set(stage.item_id, stage.history);
    const counts = stage.counts || {};
    const before = run.prevCounts || {};
    for (const k of new Set([...Object.keys(counts), ...Object.keys(before)])) {
      const d = (counts[k] || 0) - (before[k] || 0);
      if (!d) continue;
      world.countDelta[k] = (world.countDelta[k] || 0) + d;
      run.deltas[k] = (run.deltas[k] || 0) + d;
    }
    run.prevCounts = counts;
    run.lastStage = stage;
    return stage;
  }

  // What a play will touch, as it is before the play starts, so Take back can put it back.
  function sliceOf(run) {
    const s = world.state;
    const paths = new Set();
    const sessionIds = new Set();
    let itemId = run.itemId;
    for (const n of ["handed", "added", "working", "after"]) {
      if (!run.play[n]) continue;
      const st = instantiate(run.play[n], run.ids, run.answer);
      if (st.item_id != null) itemId = st.item_id;
      for (const p of Object.keys(st.lists || {})) paths.add(p);
      for (const x of st.sessions || []) sessionIds.add(x.session_id);
    }
    const lists = new Map();
    for (const p of paths) {
      const list = listAt(s, p, false);
      lists.set(p, list ? list.map((e, i) => [i, e]).filter(([, e]) => belongs(e, itemId)).map(([i, e]) => [i, clone(e)]) : null);
    }
    const sessions = new Map();
    for (const id of sessionIds) {
      const x = (s.sessions || []).find((y) => y.session_id === id);
      sessions.set(id, x ? clone(x) : null);
    }
    return {
      itemId,
      lists,
      sessions,
      linked: clone(s.linked_sessions || []),
      history: world.historyOver.has(itemId) ? clone(world.historyOver.get(itemId)) : undefined,
      logs: new Map([...world.logExtra].map(([k, v]) => [k, v.length])),
    };
  }

  function clearTimers(run) {
    for (const t of run.timers) {
      clearTimeout(t.handle);
      world.timers.delete(t);
    }
    run.timers.clear();
  }

  function putSlice(run, keepMessages) {
    const s = world.state;
    const slice = run.slice;
    if (!slice) return;
    for (const [p, pairs] of slice.lists) {
      if (keepMessages && p === "messages") continue;
      const list = listAt(s, p, Boolean(pairs && pairs.length));
      if (!list) continue;
      for (let i = list.length - 1; i >= 0; i -= 1) if (belongs(list[i], slice.itemId)) list.splice(i, 1);
      for (const [i, e] of pairs || []) list.splice(Math.min(i, list.length), 0, clone(e));
    }
    const sessions = s.sessions || (s.sessions = []);
    for (const [id, x] of slice.sessions) {
      const i = sessions.findIndex((y) => y.session_id === id);
      if (x) {
        if (i >= 0) sessions[i] = clone(x);
        else sessions.push(clone(x));
      } else if (i >= 0) sessions.splice(i, 1);
    }
    s.linked_sessions = clone(slice.linked);
    if (slice.history === undefined) world.historyOver.delete(slice.itemId);
    else world.historyOver.set(slice.itemId, clone(slice.history));
    for (const [k, d] of Object.entries(run.deltas)) world.countDelta[k] = (world.countDelta[k] || 0) - d;
    run.deltas = {};
    clearTimers(run);
    if (!keepMessages) {
      for (const [k, n] of slice.logs) {
        const l = world.logExtra.get(k);
        if (l) l.length = n;
      }
    }
  }

  // ---------------------------------------------------------------------------------------------------------------
  // normalize: what the server does after every change. Queue places, snoozes, counts.

  function byRank(a, b) {
    return (b.score || 0) - (a.score || 0)
      || String(a.created_at || "").localeCompare(String(b.created_at || ""))
      || a.id - b.id;
  }

  function normalize() {
    const s = world.state;
    const now = Date.now();
    s.now = isoNow(now);
    s.snooze_reads = Object.fromEntries(world.snoozeReads);
    for (const name of STEP_LISTS) if (!Array.isArray(s[name])) s[name] = [];
    delete s.placed_order;

    const open = new Set();
    for (const name of ["with_agents", "queue", "snoozed", "ready_next"]) {
      s[name] = s[name].filter((x) => {
        if (!x || open.has(x.id)) return false;
        open.add(x.id);
        return true;
      });
    }
    if (s.closed_recently && Array.isArray(s.closed_recently.steps)) {
      const ids = new Set();
      s.closed_recently.steps = s.closed_recently.steps.filter((x) => {
        if (open.has(x.id) || ids.has(x.id)) return false;
        ids.add(x.id);
        return true;
      });
    }
    for (const key of ["messages", "items"]) {
      if (!Array.isArray(s[key])) continue;
      const ids = new Set();
      s[key] = s[key].filter((x) => {
        if (!x || typeof x !== "object" || !("id" in x)) return true;
        if (ids.has(x.id)) return false;
        ids.add(x.id);
        return true;
      });
    }
    if (Array.isArray(s.sessions)) {
      const ids = new Set();
      s.sessions = s.sessions.filter((x) => {
        if (ids.has(x.session_id)) return false;
        ids.add(x.session_id);
        return true;
      });
    }

    for (const [id, fate] of [...world.fates]) {
      if (fate.kind === "done" || fate.kind === "dismissed") {
        takeStep(s, id);
        continue;
      }
      if (fate.kind !== "snoozed") continue;
      let found = findStep(s, id);
      if (!found) {
        // A page's own update after Snooze drops the step from its list; the snooze still holds it.
        const cached = world.stepCache.get(id);
        if (!cached) {
          world.fates.delete(id);
          continue;
        }
        s.snoozed.push(clone(cached));
        found = findStep(s, id);
      }
      if (found.name === "with_agents") {
        world.fates.delete(id);
        continue;
      }
      const step = takeStep(s, id);
      if (Date.parse(fate.until) <= now) {
        world.fates.delete(id);
        step.snoozed_until = null;
        step.taken_at = null;
        if (fate.queued) {
          step.queued_at = s.now;
          world.kept.add(id);
          s.queue.unshift(step);
        } else {
          step.queued_at = null;
          s.ready_next.push(step);
        }
      } else {
        step.snoozed_until = fate.until;
        step.queued_at = null;
        step.taken_at = null;
        s.snoozed.push(step);
      }
    }
    s.snoozed.sort((a, b) => (Date.parse(a.snoozed_until) || 0) - (Date.parse(b.snoozed_until) || 0));

    while (s.queue.length < QUEUE_LIMIT && s.ready_next.length) {
      const ordinary = s.ready_next.filter((x) => !x.early);
      const pick = (ordinary.length ? ordinary : s.ready_next).slice().sort(byRank)[0];
      s.ready_next.splice(s.ready_next.indexOf(pick), 1);
      pick.queued_at = s.now;
      pick.taken_at = null;
      s.queue.push(pick);
    }
    const movable = (x) => !world.kept.has(x.id) && !x.taken_at && !(x.timer && x.timer.started_at) && !x.user_on_it;
    while (s.queue.length > QUEUE_LIMIT) {
      const candidates = s.queue.filter(movable);
      if (!candidates.length) break;
      candidates.sort((a, b) => (b.early ? 1 : 0) - (a.early ? 1 : 0)
        || (a.started_in_place ? 1 : 0) - (b.started_in_place ? 1 : 0)
        || (a.score || 0) - (b.score || 0));
      const out = candidates[0];
      s.queue.splice(s.queue.indexOf(out), 1);
      out.queued_at = null;
      s.ready_next.push(out);
    }
    const taken = (x) => Boolean(x.taken_at && x.taken_at === x.queued_at);
    s.queue.sort((a, b) => (taken(b) ? 1 : 0) - (taken(a) ? 1 : 0)
      || (taken(a) && taken(b) ? String(b.taken_at).localeCompare(String(a.taken_at)) : 0)
      || byRank(a, b));
    s.ready_next.sort(byRank);

    const counts = { ...world.baseCounts };
    for (const [k, d] of Object.entries(world.countDelta)) counts[k] = Math.max(0, (counts[k] || 0) + d);
    counts.waiting_on_user = s.queue.length;
    counts.ready_next = s.ready_next.length;
    counts.snoozed = s.snoozed.length;
    counts.with_agents = s.with_agents.length;
    s.counts = counts;
    for (const name of STEP_LISTS) for (const x of s[name]) world.stepCache.set(x.id, clone(x));
  }

  // ---------------------------------------------------------------------------------------------------------------
  // The lock: a page changes the state itself after a write, as Cockpit's pages do with their own optimistic
  // updates; nothing else touches the state meanwhile.

  function reply(source, id, ok, value) {
    if (id == null) return;
    try {
      source.postMessage({ demo: "reply", id, ok, value }, "*");
    } catch (error) {
      console.error("demo:", error);
    }
  }

  function drain() {
    while (!holder && lockQueue.length) {
      const next = lockQueue.shift();
      if (next.fn) {
        try {
          next.fn();
        } catch (error) {
          console.error("demo:", error);
        }
        continue;
      }
      normalize();
      const h = { source: next.source, epoch: world.epoch };
      h.timer = setTimeout(() => {
        if (holder === h) {
          holder = null;
          drain();
        }
      }, 5000);
      holder = h;
      world.lastLock.set(next.source, Date.now());
      reply(next.source, next.id, true, world.state);
    }
  }

  function whenFree(fn) {
    lockQueue.push({ fn });
    drain();
  }

  function isLocal(x) {
    return Boolean(x && typeof x === "object" && typeof x.id === "string" && x.id.startsWith("local-"));
  }

  // The page's own placeholders (ids "local-…") stay on the page; the demo's rows replace them.
  function withoutLocal(next) {
    const out = {};
    for (const [k, v] of Object.entries(next)) {
      if (Array.isArray(v)) out[k] = v.filter((x) => !isLocal(x));
      else if (v && typeof v === "object") {
        const inner = {};
        for (const [k2, v2] of Object.entries(v)) inner[k2] = Array.isArray(v2) ? v2.filter((x) => !isLocal(x)) : v2;
        out[k] = inner;
      } else out[k] = v;
    }
    return out;
  }

  function unlock(source, id, next) {
    if (!holder || holder.source !== source) {
      reply(source, id, false, "Demo: the lock was not held");
      return;
    }
    clearTimeout(holder.timer);
    const h = holder;
    holder = null;
    if (h.epoch === world.epoch && next && Array.isArray(next.queue)) {
      world.state = withoutLocal(next);
      normalize();
      poke();
    }
    reply(source, id, true, true);
    drain();
  }

  // A timer of the demo's own: it runs when the lock is free, and not at all after Start over.
  function later(ms, fn, run) {
    const epoch = world.epoch;
    const t = { handle: null, run };
    t.handle = setTimeout(() => {
      world.timers.delete(t);
      run?.timers.delete(t);
      whenFree(() => {
        if (epoch !== world.epoch) return;
        fn();
        normalize();
        poke();
      });
    }, ms);
    world.timers.add(t);
    run?.timers.add(t);
    return t;
  }

  function frames() {
    const out = [];
    const visit = (w) => {
      if (!w) return;
      out.push(w);
      let n = 0;
      try {
        n = w.length;
      } catch {
        return;
      }
      for (let i = 0; i < n; i += 1) {
        let c = null;
        try {
          c = w[i];
        } catch {
          c = null;
        }
        visit(c);
      }
    };
    visit(view.contentWindow);
    return out;
  }

  function poke() {
    clearTimeout(pokeTimer);
    pokeTimer = setTimeout(() => {
      for (const w of frames()) {
        try {
          w.postMessage({ demo: "poke" }, "*");
        } catch {
          // A frame that went away.
        }
      }
    }, 150);
  }

  function toast(text) {
    toastBox.textContent = text;
    toastBox.classList.add("shown");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastBox.classList.remove("shown"), Math.max(3000, text.length * 60));
  }

  // ---------------------------------------------------------------------------------------------------------------
  // Requests from the pages.

  window.addEventListener("message", (event) => {
    const data = event.data;
    if (!data || typeof data !== "object" || data.demo !== "rpc" || typeof data.op !== "string") return;
    const source = event.source;
    if (!source || !frames().includes(source)) return;
    try {
      handle(source, data);
    } catch (error) {
      console.error("demo:", error);
      reply(source, data.id, false, `Demo: ${error.message}`);
    }
  });

  function handle(source, data) {
    const { id, op } = data;
    switch (op) {
      case "fetch":
        whenFree(() => answerFetch(source, data));
        return;
      case "lock":
        lockQueue.push({ source, id });
        drain();
        return;
      case "unlock":
        unlock(source, id, data.state);
        return;
      case "view":
        // The pages a frame inside the app can show: see VIEWS in demo-child.js.
        if (["pages", "briefing", "usage", "spec"].includes(data.name)) reply(source, id, true, fill(DATA.views[data.name], null));
        else reply(source, id, false, "Demo: no such view");
        return;
      case "toast":
        toast(String(data.text || ""));
        reply(source, id, true, true);
        return;
      case "storage": {
        const area = storage[data.area];
        if (!area) {
          reply(source, id, false, "Demo: no such storage");
          return;
        }
        if (data.key == null) area.clear();
        else if (data.value == null) area.delete(String(data.key));
        else area.set(String(data.key), String(data.value));
        const heard = { demo: "storage", area: data.area, key: data.key ?? null, value: data.value ?? null, old: data.old ?? null };
        for (const w of frames()) {
          if (w === source) continue;
          try {
            w.postMessage(heard, "*");
          } catch {
            // A frame that went away.
          }
        }
        reply(source, id, true, true);
        return;
      }
      case "nav":
        navigate(data);
        reply(source, id, true, true);
        return;
      case "where":
        if (source === view.contentWindow) {
          stack[pos] = { path: data.path || "/", search: data.search || "", hash: data.hash || "" };
          showBack();
        }
        reply(source, id, true, true);
        return;
      case "back":
        if (source === view.contentWindow) goBack();
        reply(source, id, true, true);
        return;
      default:
        reply(source, id, false, `Demo: unknown request ${op}`);
    }
  }

  function answerFetch(source, data) {
    const method = String(data.method || "GET").toUpperCase();
    let body = data.body;
    if (typeof body === "string") {
      try {
        body = JSON.parse(body);
      } catch {
        // Not JSON: the routes read nothing from it.
      }
    }
    if (body == null || typeof body !== "object") body = {};
    let r;
    try {
      const url = new URL(data.path || "/", DATA.origin);
      r = route(method, url, body, source);
    } catch (error) {
      console.error("demo:", error);
      r = { j: { detail: `Demo: ${error.message}` }, status: 500 };
    }
    if (method !== "GET" && method !== "HEAD") {
      normalize();
      poke();
    }
    reply(source, data.id, true, result(r));
  }

  function route(method, url, body, source) {
    const path = url.pathname;
    let m;
    if (method === "GET" || method === "HEAD") return getRoute(path, url);
    if (path === "/api/timezone" && method === "PUT") {
      world.timezone = String(body.zone);
      return { j: { zone: world.timezone, ask: null } };
    }
    if (path === "/api/timezone/report" || path === "/api/timezone/keep") return { j: { zone: world.timezone, ask: null } };
    if ((m = /^\/api\/steps\/(\d+)\/(.+)$/.exec(path))) return stepRoute(+m[1], m[2], body, source);
    if ((m = /^\/api\/messages\/(\d+)\/([\w-]+)$/.exec(path))) return messageRoute(+m[1], m[2], body, source);
    if (path === "/api/messages" && method === "POST") return sendMessage(body);
    if (path === "/api/items/add" && method === "POST") return addItem(String(body.text ?? ""));
    if ((m = /^\/api\/items\/(\d+)\/(.+)$/.exec(path))) return itemRoute(+m[1], m[2], body, source);
    if ((m = /^\/api\/item-pages\/(\d+)\/(.+)$/.exec(path))) return pageRoute(+m[1], m[2], body, method, source);
    if ((m = /^\/api\/page-comments\/(\d+)$/.exec(path))) return commentRoute(+m[1], body, method);
    if ((m = /^\/api\/briefings\/(\d+)\/read$/.exec(path))) return briefingRead(+m[1]);
    if (path === "/api/update" && method === "POST") return startUpdate();
    if (QUIET.test(path)) return { j: {} };
    return genericWrite(source);
  }

  // A write the demo has no story for: the page shows its own change, and a note says nothing was kept.
  function genericWrite(source) {
    const at = Date.now();
    later(400, () => {
      if (!((world.lastLock.get(source) || 0) >= at)) toast(SAVED);
    });
    return { j: {} };
  }

  // ---------------------------------------------------------------------------------------------------------------
  // Steps.

  function stepRoute(id, action, body, source) {
    const s = world.state;
    if (action === "writing") return { j: {} };
    if (action === "reopen") return reopenStep(id);
    const fate = world.fates.get(id);
    if (fate && (fate.kind === "done" || fate.kind === "dismissed")) {
      return conflict(action === "return" ? "Demo: the agent already finished this step" : "Demo: this step is already closed");
    }
    const found = findStep(s, id);
    if (!found) return conflict("Demo: no such step", 404);
    switch (action) {
      case "done":
        return closeStep(found, "done", body);
      case "dismiss":
        return closeStep(found, "dismissed", body);
      case "snooze":
        return snoozeStep(found, body);
      case "take": {
        world.fates.delete(id);
        const step = takeStep(s, id);
        step.queued_at = step.taken_at = isoNow();
        step.snoozed_until = null;
        s.queue.unshift(step);
        return { j: own(clone(step)) };
      }
      case "hold":
        return { j: { held: true } };
      case "timer/away":
        return { j: { input_count: 0, stopped: true, away: null } };
      case "close-tasks": {
        const ids = (Array.isArray(body.item_ids) ? body.item_ids : []).map(Number).filter(Boolean);
        for (const itemId of ids) closeItemSteps(itemId);
        const step = clone(found.step);
        cancelRun(id);
        world.fates.set(id, { kind: "done" });
        takeStep(s, id);
        listAt(s, "closed_recently/steps", true).unshift(closedRow(step, "done", body.note));
        return { j: { finished: ids, refused: [], step: { ...step, status: "done", closed_at: isoNow() } } };
      }
      case "hand-off":
        return handOff(found, body);
      case "run":
        return runStep(found, body);
      case "allow":
        return fallback(found, String(body.text || "Allow it"), body);
      case "secret":
        return fallback(found, "(a secret, not shown)", body);
      case "return":
        return takeBack(found);
      default:
        if (action.startsWith("timer/")) return { j: { input_count: 0 } };
        return genericWrite(source);
    }
  }

  function closeStep(found, status, body) {
    const s = world.state;
    const step = clone(found.step);
    cancelRun(step.id);
    world.fates.set(step.id, { kind: status });
    takeStep(s, step.id);
    const row = closedRow(step, status, body.note);
    listAt(s, "closed_recently/steps", true).unshift(row);
    return { j: { ...step, status, closed_at: row.closed_at } };
  }

  function snoozeStep(found, body) {
    const s = world.state;
    const id = found.step.id;
    if (!body.words) world.snoozeReads.delete(`step:${id}`);
    if (body.until == null) {
      world.fates.delete(id);
      const step = takeStep(s, id);
      step.snoozed_until = null;
      step.queued_at = step.taken_at = isoNow();
      s.queue.unshift(step);
      return { j: own(clone(step)) };
    }
    const ms = Date.parse(body.until);
    if (Number.isNaN(ms)) return conflict("Demo: that time could not be read", 400);
    const until = isoNow(ms);
    const old = world.fates.get(id);
    world.fates.set(id, { kind: "snoozed", until, queued: found.name === "queue" || Boolean(old && old.queued) });
    world.kept.delete(id);
    armWake(ms - Date.now());
    if (body.words) readLater(id, until, String(body.words.text || ""));
    return { j: { ...clone(found.step), snoozed_until: until } };
  }

  // A snooze in the user's words holds until `standIn` while the demo reads them, as Cockpit does with Claude: then
  // it moves to the time read, or the step comes back with the reason. A read that lands after the user snoozed the
  // step again changes nothing.
  function readLater(id, standIn, text) {
    const reads = world.snoozeReads;
    const target = `step:${id}`;
    const words = text.trim();
    const readId = Math.random().toString(16).slice(2);
    const reading = { id: readId, words, status: "reading", until: null, reason: "", at: isoNow() };
    reads.set(target, reading);
    later(2500, () => {
      const fate = world.fates.get(id);
      if (reads.get(target) !== reading) return;
      if (!fate || fate.kind !== "snoozed" || fate.until !== standIn) {
        reads.delete(target);
        return;
      }
      const read = readSnooze(words);
      if (typeof read === "string") {
        const found = findStep(world.state, id);
        if (found) snoozeStep(found, { until: null });
        reads.set(target, { id: readId, words, status: "unread", until: null, reason: read, at: isoNow() });
        return;
      }
      fate.until = isoNow(read.getTime());
      armWake(read.getTime() - Date.now());
      reads.set(target, { id: readId, words, status: "read", until: fate.until, reason: "", at: isoNow() });
    });
  }

  function reopenStep(id) {
    const s = world.state;
    const open = findStep(s, id);
    if (open) return { j: own(clone(open.step)) };
    const cached = world.stepCache.get(id);
    if (!cached) return conflict("Demo: this step closed before the demo began");
    const now = isoNow();
    const step = { ...clone(cached), status: "open", closed_at: null, handoff_message_id: null, queued_at: now, taken_at: now };
    delete step.handoff;
    world.fates.delete(id);
    const rows = listAt(s, "closed_recently/steps", false);
    if (rows) for (let i = rows.length - 1; i >= 0; i -= 1) if (rows[i].id === id) rows.splice(i, 1);
    s.queue.unshift(step);
    return { j: own(clone(step)) };
  }

  function cancelRun(id) {
    const run = world.runs.get(id);
    if (!run) return;
    clearTimers(run);
    world.runs.delete(id);
  }

  function closeItemSteps(itemId) {
    const s = world.state;
    for (const name of STEP_LISTS) {
      for (const step of [...s[name]]) {
        if (step.item_id !== itemId) continue;
        cancelRun(step.id);
        world.fates.set(step.id, { kind: "done" });
      }
    }
  }

  function pickPlay(plays, text) {
    const t = text.toLowerCase();
    return plays.find((p) => Array.isArray(p.match) && p.match.some((w) => t.includes(String(w).toLowerCase())))
      || plays.find((p) => p.match == null)
      || plays[0];
  }

  function handOff(found, body) {
    const step = found.step;
    const id = step.id;
    const text = String(body.text ?? "");
    if (world.runs.has(id)) return conflict("Demo: this step is already with the agent");
    const deeper = world.deeper.get(id);
    const plays = deeper?.plays ?? DATA.played.answers[String(id)];
    const play = plays && plays.length ? pickPlay(plays, text) : null;
    if (!play || step.status !== "open" || found.name === "with_agents") return fallback(found, text, body);
    const run = {
      kind: "answer", key: id, stepId: id, itemId: step.item_id, answer: text, ids: deeper?.ids ?? new Map(),
      prevCounts: deeper?.counts ?? DATA.base.counts, deltas: {}, phase: "handed", play, epoch: world.epoch,
      timers: new Set(),
    };
    run.sessionId = instantiate(play.working?.sessions?.[0]?.session_id ?? null, run.ids, text)
      ?? step.target?.session_id ?? step.session_id ?? null;
    run.slice = sliceOf(run);
    world.fates.delete(id);
    world.kept.delete(id);
    takeStep(world.state, id);
    world.runs.set(id, run);
    applyStage(run, "handed");
    const message = instantiate(shifted(play.reply, stageDelta(play.handed.at)), run.ids, text);
    run.messageId = message.id;
    const until = setDeadline(run);
    run.taken = dropSentComments(id);
    return { j: own({ ...message, take_back_until: until }) };
  }

  // The take-back window: until it ends, the message waits and Take back stops it.
  function setDeadline(run, ms = DELAY) {
    const now = Date.now();
    const untilMs = Math.ceil((now + ms) / SECOND) * SECOND;
    const until = isoNow(untilMs);
    const s = world.state;
    run.until = until;
    const message = (s.messages || []).find((m) => m.id === run.messageId);
    if (message) message.take_back_until = until;
    const step = s.with_agents.find((x) => x.id === run.stepId);
    if (step && step.handoff) step.handoff.take_back_until = until;
    if (run.deadline) {
      clearTimeout(run.deadline.handle);
      world.timers.delete(run.deadline);
      run.timers.delete(run.deadline);
    }
    run.deadline = later(untilMs - now, () => (run.kind === "fallback" ? sendFallback(run) : startWorking(run)), run);
    return until;
  }

  function startWorking(run) {
    if (world.runs.get(run.key) !== run || run.phase !== "handed") return;
    run.phase = "working";
    applyStage(run, "working");
    addPart(run.sessionId, { author: "user", at: isoNow(), text: run.answer, kind: "instruction" });
    later(WORK_MS, () => finish(run), run);
  }

  function finish(run) {
    if (world.runs.get(run.key) !== run) return;
    const stage = applyStage(run, "after");
    const steps = STEP_LISTS.flatMap((n) => (stage.lists[n] || []).filter((x) => x && typeof x === "object"));
    if (run.stepId != null && !steps.some((x) => x.id === run.stepId)) world.fates.set(run.stepId, { kind: "done" });
    const made = new Set(run.ids.values());
    const fresh = steps.find((x) => made.has(x.id) && x.id !== run.stepId);
    const item = (stage.lists.items || []).find((x) => x && typeof x === "object" && x.id === stage.item_id);
    let text = "Done";
    let html = null;
    if (fresh) {
      text = fresh.body || fresh.ask || "Done";
      html = fresh.body_html || (fresh.body ? null : fresh.ask_html) || null;
    } else if (item && item.summary) text = item.summary;
    addPart(run.sessionId, { at: isoNow(), kind: "says", text, html: html || `<p>${escapeHtml(text)}</p>\n` });
    for (const [key, plays] of Object.entries(run.play.deeper || {})) {
      const m = EXACT.exec(key);
      if (m) world.deeper.set(idFor(run.ids, m[1], +m[2]), { plays, ids: run.ids, counts: run.play.after.counts });
    }
    world.runs.delete(run.key);
    if (run.itemId != null) world.pendingAdd.delete(run.itemId);
  }

  // An answer the demo has no recording for: it goes to the session, which cannot work on it, so it comes back.
  function fallback(found, text, body) {
    const s = world.state;
    const step = found.step;
    const id = step.id;
    if (world.runs.has(id)) return conflict("Demo: this step is already with the agent");
    const sessionId = body.session_id || step.target?.session_id || step.session_id || null;
    const message = makeMessage({
      item_id: step.item_id, session_id: sessionId, step_id: id, text, state: "queued", via: null, hands_off: 1,
      as_prompt: 1, step_ask: step.ask,
    });
    (s.messages || (s.messages = [])).unshift(message);
    const original = clone(step);
    takeStep(s, id);
    world.fates.delete(id);
    world.kept.delete(id);
    s.with_agents.unshift({
      ...clone(step), handoff: clone(message), handoff_message_id: message.id, queued_at: null, taken_at: null,
      user_on_it: false,
    });
    const run = {
      kind: "fallback", key: id, stepId: id, itemId: step.item_id, answer: text, messageId: message.id,
      phase: "handed", epoch: world.epoch, timers: new Set(), deltas: {}, original, sessionId,
    };
    world.runs.set(id, run);
    const until = setDeadline(run);
    run.taken = dropSentComments(id);
    return { j: own({ ...clone(message), take_back_until: until, reopened: false }) };
  }

  function sendFallback(run) {
    if (world.runs.get(run.key) !== run || run.phase !== "handed") return;
    run.phase = "working";
    const s = world.state;
    const now = isoNow();
    const sent = { state: "sent", sent_at: now, take_back_until: null, via: "socket" };
    const message = (s.messages || []).find((m) => m.id === run.messageId);
    if (message) Object.assign(message, sent);
    const handed = s.with_agents.find((x) => x.id === run.stepId);
    if (handed && handed.handoff) Object.assign(handed.handoff, sent);
    addPart(run.sessionId, { author: "user", at: now, text: run.answer, kind: "instruction" });
    later(6000, () => {
      if (world.runs.get(run.key) !== run) return;
      world.runs.delete(run.key);
      const state = world.state;
      const step = takeStep(state, run.stepId);
      if (!step) return;
      delete step.handoff;
      step.handoff_message_id = null;
      step.queued_at = step.taken_at = isoNow();
      state.queue.unshift(step);
      toast("Demo: no agent works on this step, so it came back to you");
    }, run);
  }

  function runStep(found, body) {
    const step = found.step;
    const id = step.id;
    if (world.runs.has(id)) return conflict("Demo: this step is already with the agent");
    const play = DATA.played.run?.[String(id)];
    if (!play || found.name === "with_agents") return fallback(found, `Run: ${step.command?.run ?? ""}`.trim(), body);
    const run = {
      kind: "run", key: id, stepId: id, itemId: step.item_id, answer: "", ids: new Map(), prevCounts: DATA.base.counts,
      deltas: {}, phase: "handed", play, epoch: world.epoch, timers: new Set(),
    };
    run.sessionId = instantiate(play.working?.sessions?.[0]?.session_id ?? null, run.ids, "")
      ?? step.target?.session_id ?? step.session_id ?? null;
    run.slice = sliceOf(run);
    world.fates.delete(id);
    world.kept.delete(id);
    takeStep(world.state, id);
    world.runs.set(id, run);
    applyStage(run, "handed");
    const answer = instantiate(shifted(play.reply, stageDelta(play.handed.at)), run.ids, "");
    run.messageId = answer.message?.id ?? null;
    run.prompt = answer.message?.text || `Run: ${step.command?.run ?? ""}`;
    later(3000, () => runWorking(run), run);
    return { j: own(answer) };
  }

  function runWorking(run) {
    if (world.runs.get(run.key) !== run) return;
    run.phase = "working";
    applyStage(run, "working");
    addPart(run.sessionId, { author: "user", at: isoNow(), text: run.prompt, kind: "instruction" });
    addPart(run.sessionId, {
      at: isoNow(), kind: "call", id: "toolu_demo", tool_name: "Bash", title: "Check the migration on staging",
      text: "./manage.py showmigrations shares --database staging", result: "shares\n [X] 0042_shares", error: false,
    });
    later(WORK_MS, () => finish(run), run);
  }

  function takeBack(found) {
    const s = world.state;
    const id = found.step.id;
    const run = world.runs.get(id);
    const now = isoNow();
    if (!run) {
      if (found.name !== "with_agents") return conflict("Demo: this step is not with an agent");
      const step = takeStep(s, id);
      delete step.handoff;
      step.handoff_message_id = null;
      step.queued_at = step.taken_at = now;
      s.queue.unshift(step);
      world.fates.delete(id);
      return { j: own({ ...clone(step), stopped: false, unsent: null, unsent_to: null }) };
    }
    clearTimers(run);
    world.runs.delete(id);
    const handed = run.phase === "handed";
    let step;
    if (run.kind === "fallback") {
      takeStep(s, id);
      step = clone(run.original);
      if (handed) s.messages = (s.messages || []).filter((m) => m.id !== run.messageId);
    } else {
      putSlice(run, !handed);
      step = takeStep(s, id) || clone(world.stepCache.get(id));
    }
    // Run sends a command and no comments, so it has none to give back.
    if (handed && run.taken) restoreComments(run.taken);
    delete step.handoff;
    Object.assign(step, { status: "open", handoff_message_id: null, queued_at: now, taken_at: now, closed_at: null });
    s.queue.unshift(step);
    world.fates.delete(id);
    return {
      j: own({
        ...clone(step), handoff_message_id: null, stopped: handed, unsent: handed ? run.answer : null,
        unsent_to: handed ? { session_id: run.sessionId, new_cwd: null, provider: null, options: null } : null,
      }),
    };
  }

  // ---------------------------------------------------------------------------------------------------------------
  // Messages.

  function messageRoute(id, action, body, source) {
    const s = world.state;
    const run = [...world.runs.values()].find((r) => r.messageId === id);
    const message = (s.messages || []).find((m) => m.id === id);
    const waiting = run && run.phase === "handed" && (run.kind === "answer" || run.kind === "fallback");
    switch (action) {
      case "hold":
        if (waiting) setDeadline(run);
        return { j: own(clone(message || { id, take_back_until: run?.until ?? null })) };
      case "follow-up": {
        const text = String(body.text ?? "");
        if (waiting) {
          run.answer = `${run.answer}\n\n${text}`;
          if (message) message.text = run.answer;
          const handed = s.with_agents.find((x) => x.id === run.stepId);
          if (handed && handed.handoff) handed.handoff.text = run.answer;
          const until = setDeadline(run);
          return { j: own({ joined: true, take_back_until: until, reopened: false }) };
        }
        const next = makeMessage({ item_id: message?.item_id ?? null, session_id: message?.session_id ?? null, text });
        (s.messages || (s.messages = [])).unshift(next);
        addPart(next.session_id, { author: "user", at: isoNow(), text, kind: "instruction" });
        later(3000, () => toast("Demo: no agent runs here, so no one reads this message"));
        return { j: own({ ...clone(next), joined: false, reopened: false }) };
      }
      case "release":
      case "send-now":
        if (waiting) {
          if (run.deadline) {
            clearTimeout(run.deadline.handle);
            world.timers.delete(run.deadline);
            run.timers.delete(run.deadline);
            run.deadline = null;
          }
          if (run.kind === "fallback") sendFallback(run);
          else startWorking(run);
        }
        return { j: own(clone(message || { id })) };
      case "retry":
      case "dismiss":
      case "restore":
        return { j: {} };
      default:
        return genericWrite(source);
    }
  }

  function sendMessage(body) {
    const s = world.state;
    const target = body.target && typeof body.target === "object" ? body.target : {};
    const text = String(body.text ?? "");
    const pending = target.item_id != null ? world.pendingAdd.get(target.item_id) : null;
    if (pending) {
      const id = idFor(pending.ids, "messages", 1);
      const known = (s.messages || []).find((m) => m.id === id);
      const message = known ? clone(known) : makeMessage({
        id, item_id: target.item_id, session_id: pending.sessionId, text, state: "queued", via: null,
      });
      return { j: own({ ...message, reopened: false }) };
    }
    const item = itemOf(target.item_id);
    const sessionId = target.session_id || item?.sessions?.[0] || null;
    const message = makeMessage({
      item_id: target.item_id ?? null, session_id: sessionId, step_id: target.step_id ?? null, text,
      new_cwd: target.new_cwd ?? null, provider: target.provider ?? null,
    });
    (s.messages || (s.messages = [])).unshift(message);
    addPart(sessionId, { author: "user", at: isoNow(), text, kind: "instruction" });
    later(3000, () => toast("Demo: no agent runs here, so no one reads this message"));
    return { j: own({ ...clone(message), reopened: false }) };
  }

  // ---------------------------------------------------------------------------------------------------------------
  // Items.

  // The recording's to-do gets its session when the agent starts work, so its "working" stage names the session. Each
  // later to-do gets a session of its own in place of the recorded one.
  function addItem(text) {
    let play = DATA.played.add;
    if (world.addCount > 0) {
      const sid = play.working.sessions[0].session_id;
      const fresh = fakeUuid();
      const swap = (t) => (t.includes(sid) ? t.split(sid).join(fresh) : t);
      play = walk(play, swap, swap);
    }
    world.addCount += 1;
    const run = {
      kind: "add", key: null, stepId: null, ids: new Map(), prevCounts: DATA.base.counts, deltas: {}, answer: text,
      phase: "added", play, epoch: world.epoch, timers: new Set(),
    };
    run.itemId = idFor(run.ids, "items", 1);
    run.key = `add:${run.itemId}`;
    applyStage(run, "added");
    run.sessionId = play.working.sessions[0].session_id;
    const item = instantiate(shifted(play.item, stageDelta(play.added.at)), run.ids, text);
    world.pendingAdd.set(run.itemId, run);
    world.runs.set(run.key, run);
    later(2500, () => {
      if (world.runs.get(run.key) !== run) return;
      run.phase = "working";
      applyStage(run, "working");
      addPart(run.sessionId, { author: "user", at: isoNow(), text, kind: "instruction" });
      later(WORK_MS, () => finish(run), run);
    }, run);
    return { j: own(item) };
  }

  function itemRoute(id, action, body, source) {
    switch (action) {
      case "images":
        return conflict("Demo: pictures are not kept in the demo", 400);
      case "ask":
        return askSide(id, body);
      case "fork":
        return conflict("Demo: Fork starts a new session, and no sessions run in the demo");
      case "finish":
        closeItemSteps(id);
        return { j: { message: null, session_end: null } };
      case "reopen":
        for (const [stepId, step] of world.stepCache) if (step.item_id === id) world.fates.delete(stepId);
        return { j: {} };
      case "add-more": {
        const item = itemOf(id);
        addPart(item?.sessions?.[0] ?? null, { author: "user", at: isoNow(), text: String(body.text ?? ""), kind: "instruction" });
        later(1500, () => toast("Demo: no agent runs here, so no one reads what you added"));
        return { j: {} };
      }
      case "time":
      case "time/add":
        return { j: {} };
      default:
        return genericWrite(source);
    }
  }

  function askSide(itemId, body) {
    const s = world.state;
    const item = itemOf(itemId);
    const chat = {
      id: nextId("side_chats"), item_id: itemId, step_id: body.step_id ?? null,
      session_id: body.session_id ?? item?.sessions?.[0] ?? null, question: String(body.text ?? ""), answer: null,
      answer_html: null, error: null, state: "asking", asked_at: isoNow(), answered_at: null, sent_message_id: null,
      sent_state: null, sent_at: null,
    };
    if (!Array.isArray(s.side_chats)) s.side_chats = [];
    s.side_chats.push(chat);
    later(1500, () => {
      const found = (world.state.side_chats || []).find((x) => x.id === chat.id);
      if (!found) return;
      Object.assign(found, {
        state: "answered", answer: ASK_ANSWER, answer_html: `<p>${escapeHtml(ASK_ANSWER)}</p>\n`, answered_at: isoNow(),
        sent_message_id: nextId("messages"), sent_state: "queued",
      });
    });
    return { j: own(clone(chat)) };
  }

  // ---------------------------------------------------------------------------------------------------------------
  // Pages: comments and choices.

  function pageInfo(pageId) {
    return (fixture("/api/pages")?.j?.pages || []).find((p) => p.id === pageId) || null;
  }

  function listComments(pageId) {
    if (!world.comments.has(pageId)) world.comments.set(pageId, clone(fixture(`/api/item-pages/${pageId}/comments`)?.j?.comments || []));
    return world.comments.get(pageId);
  }

  function listChoices(pageId) {
    if (!world.choices.has(pageId)) world.choices.set(pageId, clone(fixture(`/api/item-pages/${pageId}/choices`)?.j?.choices || []));
    return world.choices.get(pageId);
  }

  function stepTakes(stepId) {
    const found = findStep(world.state, stepId);
    return Boolean(found && found.name !== "with_agents" && !world.runs.has(stepId));
  }

  function withStepOpen(comment) {
    return { ...clone(comment), step_open: comment.step_id != null && comment.state === "unsent" && stepTakes(comment.step_id) ? 1 : 0 };
  }

  function anchorText(anchor) {
    if (anchor && anchor.kind === "text") return `On the words "${anchor.quote ?? ""}"`;
    return `On the part ${anchor?.path ?? "of the page"}`;
  }

  function commentLines(page, comments) {
    return `On the page "${page?.title ?? "page"}":\n${comments.map((c, i) => `${i + 1}. ${anchorText(c.anchor)}: ${c.text}`).join("\n")}`;
  }

  function unsentOf(pageId, ids) {
    const wanted = new Set((Array.isArray(ids) ? ids : []).map(Number));
    return listComments(pageId).filter((c) => wanted.has(c.id) && c.state === "unsent");
  }

  function dropFromState(ids) {
    const s = world.state;
    s.page_comments = (s.page_comments || []).filter((c) => !ids.has(c.id));
  }

  function pageRoute(pageId, action, body, method, source) {
    const s = world.state;
    const page = pageInfo(pageId);
    const now = isoNow();
    if (action === "comments" && method === "POST") {
      const text = String(body.text || "").trim();
      if (!text) return conflict("Demo: the comment is empty", 400);
      // As on the host, a comment is on quoted words or on a picked part; the page's comment list reads its anchor.
      const anchor = body.anchor;
      const anchored = anchor?.kind === "text" ? typeof anchor.quote === "string" && anchor.quote !== ""
        : anchor?.kind === "part" && typeof anchor.path === "string" && anchor.path !== "";
      if (!anchored) return conflict("Demo: a comment needs the words or the part it is on", 422);
      const stepId = body.step_id ?? null;
      if (stepId != null && !stepTakes(stepId)) return conflict("Demo: that step no longer takes comments");
      const comment = {
        id: nextId("page_comments"), page_id: pageId, revision: body.revision ?? page?.revision ?? 1, step_id: stepId,
        anchor, text, state: "unsent", message_id: null, item_id: null, created_at: now,
        updated_at: now,
      };
      listComments(pageId).push(comment);
      if (stepId != null) {
        if (!Array.isArray(s.page_comments)) s.page_comments = [];
        s.page_comments.push({ id: comment.id, page_id: pageId, step_id: stepId, anchor: comment.anchor, text, page_title: page?.title ?? "" });
      }
      return { j: own(withStepOpen(comment)) };
    }
    if (action === "comments/send") {
      const chosen = unsentOf(pageId, body.comment_ids);
      if (!chosen.length) return conflict("Demo: these comments were already sent");
      const words = String(body.text || "").trim();
      const item = itemOf(page?.item_id);
      const text = [words, commentLines(page, chosen)].filter(Boolean).join("\n\n");
      const message = makeMessage({ item_id: page?.item_id ?? null, session_id: item?.sessions?.[0] ?? null, text });
      (s.messages || (s.messages = [])).unshift(message);
      for (const c of chosen) Object.assign(c, { state: "sent", message_id: message.id, updated_at: now });
      dropFromState(new Set(chosen.map((c) => c.id)));
      addPart(message.session_id, { author: "user", at: now, text, kind: "instruction" });
      later(3000, () => toast("Demo: no agent runs here, so no one reads these comments"));
      return { j: own({ ...clone(message), reopened: false }) };
    }
    if (action === "comments/item") {
      const chosen = unsentOf(pageId, body.comment_ids);
      if (!chosen.length) return conflict("Demo: these comments were already sent");
      const text = `Comments on the page "${page?.title ?? "page"}"\n\nThese page comments became an item of their own.\n\n${commentLines(page, chosen)}`;
      const added = addItem(text);
      for (const c of chosen) Object.assign(c, { state: "added", item_id: added.j.id, updated_at: now });
      dropFromState(new Set(chosen.map((c) => c.id)));
      return added;
    }
    if (action === "choices" && method === "PUT") {
      const list = listChoices(pageId);
      const name = String(body.name ?? "");
      const chosen = body.chosen ?? null;
      const index = list.findIndex((c) => c.name === name);
      if (index >= 0) list.splice(index, 1);
      const empty = chosen === null || (Array.isArray(chosen) && !chosen.length);
      const stepId = body.step_id ?? null;
      if (!empty) {
        list.push({ page_id: pageId, name, chosen, label: body.label ?? null, step_id: stepId, message_id: null, updated_at: now, sent: false });
        list.sort((a, b) => a.name.localeCompare(b.name));
      }
      s.page_choices = (s.page_choices || []).filter((c) => !(c.page_id === pageId && c.name === name));
      if (!empty && stepId != null) {
        s.page_choices.push({ page_id: pageId, name, chosen, label: body.label ?? null, step_id: stepId, page_title: page?.title ?? "" });
      }
      return { j: own({ choices: clone(list) }) };
    }
    return genericWrite(source);
  }

  function commentRoute(commentId, body, method) {
    let list = null;
    let comment = null;
    for (const l of world.comments.values()) {
      const c = l.find((x) => x.id === commentId);
      if (c) {
        list = l;
        comment = c;
        break;
      }
    }
    if (!comment) return conflict("Demo: no such comment", 404);
    if (comment.state !== "unsent") return conflict(`Demo: this comment was already ${comment.state}; it stays`);
    if (method === "DELETE") {
      list.splice(list.indexOf(comment), 1);
      dropFromState(new Set([commentId]));
      return { j: own(clone(comment)) };
    }
    const text = String(body.text ?? "").trim();
    if (!text) return conflict("Demo: the comment is empty", 400);
    comment.text = text;
    comment.updated_at = isoNow();
    for (const c of world.state.page_comments || []) if (c.id === commentId) c.text = text;
    return { j: own(withStepOpen(comment)) };
  }

  // A handed answer takes the step's comments and choices with it. It returns what it took, for Take back.
  function dropSentComments(stepId) {
    const s = world.state;
    const taken = {
      cards: (s.page_comments || []).filter((c) => c.step_id === stepId),
      cardChoices: (s.page_choices || []).filter((c) => c.step_id === stepId),
      comments: [],
      choices: [],
    };
    s.page_comments = (s.page_comments || []).filter((c) => c.step_id !== stepId);
    s.page_choices = (s.page_choices || []).filter((c) => c.step_id !== stepId);
    for (const list of world.comments.values()) {
      for (const c of list) {
        if (c.step_id !== stepId || c.state !== "unsent") continue;
        Object.assign(c, { state: "sent", updated_at: isoNow() });
        taken.comments.push(c);
      }
    }
    for (const list of world.choices.values()) {
      for (const c of list) {
        if (c.step_id !== stepId || c.sent) continue;
        c.sent = true;
        taken.choices.push(c);
      }
    }
    return taken;
  }

  // Take back stopped the message before it went out, so its comments and choices wait for the next answer again, as
  // the real Cockpit sets them unsent.
  function restoreComments(taken) {
    const s = world.state;
    (s.page_comments || (s.page_comments = [])).push(...taken.cards);
    (s.page_choices || (s.page_choices = [])).push(...taken.cardChoices);
    for (const c of taken.comments) Object.assign(c, { state: "unsent", updated_at: isoNow() });
    for (const c of taken.choices) c.sent = false;
  }

  // ---------------------------------------------------------------------------------------------------------------
  // Everything else.

  function briefingRead(n) {
    const s = world.state;
    if (s.briefing && typeof s.briefing === "object") s.briefing.unread = false;
    const f = fixture("/api/briefings");
    if (f && f.j) f.j.read_id = n;
    return { j: { read_id: n } };
  }

  function startUpdate() {
    const id = nextId("messages");
    world.state.sweep_launch = { message_id: id, kind: "check", state: "running", detail: "" };
    later(3000, () => {
      const s = world.state;
      if (s.sweep_launch && s.sweep_launch.message_id === id) s.sweep_launch = { ...s.sweep_launch, state: "done" };
    });
    return { j: own({ id }) };
  }

  // Cockpit reads snooze times with Claude and the user's calendar; the demo knows a few common ones, in the visitor's
  // own time zone. The time read, or the reason none was.
  function readSnooze(words) {
    const text = words.toLowerCase().replace(/\s+/g, " ");
    const now = new Date();
    const PART = { morning: 9, lunch: 12, noon: 12, afternoon: 14, evening: 19, night: 20 };
    const at = (d, h) => {
      const x = new Date(d);
      x.setHours(h, 0, 0, 0);
      return x;
    };
    const day = (n) => {
      const x = new Date(now);
      x.setDate(x.getDate() + n);
      return x;
    };
    let until = null;
    let m;
    if ((m = /^in (\d+|an?|one) (min(?:ute)?s?|h(?:ou)?rs?|days?|weeks?)$/.exec(text))) {
      const n = /^\d+$/.test(m[1]) ? +m[1] : 1;
      const unit = m[2][0] === "m" ? 60000 : m[2][0] === "h" ? 3600000 : m[2][0] === "d" ? DAY : 7 * DAY;
      until = new Date(now.getTime() + n * unit);
    } else if (text === "tonight") {
      until = at(now, 20);
    } else if ((m = /^tomorrow(?: (morning|lunch|noon|afternoon|evening|night))?$/.exec(text))
      || (m = /^(?:after )?(morning|lunch|noon|afternoon|evening|night) tomorrow$/.exec(text))) {
      until = at(day(1), PART[m[1]] ?? 9);
    } else if ((m = /^(?:next |on )?(sunday|monday|tuesday|wednesday|thursday|friday|saturday)(?: (morning|lunch|noon|afternoon|evening|night))?$/.exec(text))) {
      const n = (DAYS.indexOf(m[1]) - now.getDay() + 7) % 7 || 7;
      until = at(day(n), PART[m[2]] ?? 9);
    } else if (text === "next week") {
      const n = (1 - now.getDay() + 7) % 7 || 7;
      until = at(day(n), 9);
    } else if ((m = /^this (morning|afternoon|evening)$/.exec(text))) {
      until = at(now, PART[m[1]]);
      if (until <= now) return `this ${m[1]} has passed; try "tomorrow ${m[1]}"`;
    } else if (text === "later") {
      until = new Date(now.getTime() + 3 * 3600000);
    }
    return until || 'the demo knows only times like "in 2 hours", "tomorrow morning" or "monday"';
  }

  function listed() {
    const s = world.state;
    return {
      steps: Object.fromEntries(STEP_LISTS.map((n) => [n, s[n].map((x) => x.id)])),
      items: (s.items || []).map((x) => x.id),
      permission_requests: [], settled_requests: [], moved_past: [], held_requests: [], codex_requests: [],
      waiting_sessions: [],
    };
  }

  function closedSearch(q) {
    const corpus = fixture("closed corpus")?.j || [];
    const strip = (x) => {
      const { words, ...rest } = x;
      return rest;
    };
    const m = /^#(\d+)$/.exec(q.trim());
    if (m) {
      const f = fixture(`/api/items/closed?q=%23${m[1]}`);
      if (f) return f.j.items;
      return corpus.filter((x) => x.id === +m[1]).map(strip);
    }
    const words = q.toLowerCase().split(/\s+/).filter(Boolean);
    return corpus.filter((x) => words.every((w) => String(x.words || "").includes(w))).map(strip);
  }

  function getRoute(path, url) {
    const s = world.state;
    let m;
    if (path === "/api/state") {
      normalize();
      return { j: world.state };
    }
    if (path === "/api/timezone") return { j: { zone: world.timezone, ask: null } };
    if (path === "/api/listed") return { j: listed() };
    if (path === "/api/items/closed") return { j: { items: closedSearch(url.searchParams.get("q") || "") } };
    if ((m = /^\/api\/items\/(\d+)\/history$/.exec(path))) {
      const id = +m[1];
      if (world.historyOver.has(id)) return { j: world.historyOver.get(id) };
      return fixture(path) || { j: { item_id: id, now: s.now, start: null, story: null, entries: [], steps: {} } };
    }
    // The invented person records no time on a Mac.
    if (/^\/api\/items\/\d+\/activity$/.test(path)) return { j: { labels: [], away: 0 } };
    if ((m = /^\/api\/sessions\/([^/]+)\/log$/.exec(path))) {
      const f = fixture(path);
      const parts = [...(f?.j?.parts || []), ...(world.logExtra.get(decodeURIComponent(m[1])) || [])];
      return { j: { ...(f?.j || {}), parts, total: parts.length } };
    }
    if ((m = /^\/api\/item-pages\/(\d+)\/comments$/.exec(path))) return { j: { comments: listComments(+m[1]).map(withStepOpen) } };
    if ((m = /^\/api\/item-pages\/(\d+)\/choices$/.exec(path))) return { j: { choices: clone(listChoices(+m[1])) } };
    const f = lookup(path + url.search);
    if (f) return f;
    window.__demoMisses.push(path + url.search);
    return { j: {} };
  }

  // ---------------------------------------------------------------------------------------------------------------
  // Views and navigation: one frame shows one page; links and Back move between them.

  const stack = [{ path: "/", search: "", hash: "" }];
  let pos = 0;
  let historyWorks = false;

  function viewFor(entry) {
    const { search, hash } = entry;
    let path = entry.path || "/";
    let m;
    if (path === "/next") path = "/";
    if (path === "/") return { name: "index-sidebar", path, search, hash };
    if (path === "/classic") return { name: "index-classic", path, search, hash };
    if (path === "/spec" || /^\/(skills|writing)(\/|$)/.test(path)) return { name: "spec", path, search, hash };
    if (["/briefing", "/usage", "/pages", "/emergency"].includes(path)) return { name: path.slice(1), path, search, hash };
    if ((m = /^\/pages\/(\d+)$/.exec(path))) return { name: "pages", path: "/pages", search: `?id=${m[1]}`, hash };
    return { name: "index-sidebar", path: "/", search: "", hash: "" };
  }

  function bootText(v) {
    const boot = {
      origin: DATA.origin, path: v.path, search: v.search, hash: v.hash, depth: 1,
      storage: { local: Object.fromEntries(storage.local), session: Object.fromEntries(storage.session) },
      logos: DATA.logos, figures: FIGURES,
    };
    return `window.__demoBoot = ${JSON.stringify(boot).replace(/</g, "\\u003c")};`;
  }

  function render(entry) {
    const v = viewFor(entry);
    const next = document.createElement("iframe");
    for (const name of ["id", "title", "sandbox", "allow"]) {
      const value = view.getAttribute(name);
      if (value !== null) next.setAttribute(name, value);
    }
    next.srcdoc = fill(DATA.views[v.name], bootText(v));
    const focused = document.activeElement === view;
    view.replaceWith(next);
    view = next;
    if (focused) next.focus();
    showBack();
  }

  // Cockpit opens some of its own pages (the whole briefing, a step's links) in a new tab, and the user closes that
  // tab to come back. The demo has one frame and no tabs, so the band's Back takes the user back instead. It shows
  // whenever the frame holds something other than the start of the demo's board.
  function showBack() {
    backButton.hidden = pos === 0 && viewFor(stack[pos]).name === "index-sidebar";
  }

  function backFromBand() {
    if (pos > 0) goBack();
    else navigate({ path: "/", replace: true });
  }

  function navigate(data) {
    if (data.reload) {
      render(stack[pos]);
      return;
    }
    const entry = { path: data.path || "/", search: data.search || "", hash: data.hash || "" };
    if (data.replace) {
      stack[pos] = entry;
      render(entry);
      return;
    }
    stack.splice(pos + 1);
    stack.push(entry);
    pos = stack.length - 1;
    if (data.tab) toast("Demo: Cockpit opens this in a new tab. Use Back at the top to return.");
    if (historyWorks) {
      try {
        history.pushState({ demoPos: pos }, "");
      } catch {
        historyWorks = false;
      }
    }
    render(entry);
  }

  function goBack() {
    if (pos <= 0) return;
    if (historyWorks) {
      history.back();
      return;
    }
    pos -= 1;
    render(stack[pos]);
  }

  window.addEventListener("popstate", (event) => {
    const p = event.state && event.state.demoPos;
    if (typeof p !== "number" || !stack[p]) return;
    pos = p;
    render(stack[pos]);
  });

  function startOver() {
    for (const t of world.timers) clearTimeout(t.handle);
    if (holder) clearTimeout(holder.timer);
    holder = null;
    lockQueue.length = 0;
    storage.local.clear();
    storage.session.clear();
    world = freshWorld();
    seedFates();
    normalize();
    stack.length = 0;
    stack.push({ path: "/", search: "", hash: "" });
    pos = 0;
    if (historyWorks) {
      try {
        history.replaceState({ demoPos: 0 }, "");
      } catch {
        historyWorks = false;
      }
    }
    render(stack[0]);
    toast("Demo: back to the start");
  }

  window.__demoMisses = [];
  window.__demoShell = { state: () => world.state, world: () => world };

  world = freshWorld();
  seedFates();
  normalize();
  try {
    history.replaceState({ demoPos: 0 }, "");
    historyWorks = true;
  } catch {
    historyWorks = false;
  }
  render(stack[0]);
  resetButton.addEventListener("click", startOver);
  backButton.addEventListener("click", backFromBand);
})();
