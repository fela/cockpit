"use strict";

// Every error a page meets reaches Cockpit's error log, where the sweep reads it (SPEC CODE-2); none
// stays only in the browser console. Uncaught errors and rejections arrive here by themselves; a catch
// that handles an error on its own (a toast, a banner) passes it to `logError` as well.
//
// A loop of errors costs little: the page sends an error the first time it meets it, and its repeats as
// one count at most once a minute, with at most 30 reports an hour in all. The server adds repeats to
// one row, so the sweep reads one line however often an error comes.
//
// A request that is safe to repeat goes through `fetchThroughRestart`, which waits and tries again while
// a reverse proxy answers 502 or the connection fails during a restart. The page sees a failure only when
// Cockpit is still away after about 30 s of tries made while the page is visible and the browser online.
// Tries with no answer while no other request of the page got one either are not logged: the browser stays online
// on a wifi with no way out, so only an answer shows that the page's connection worked.
// An error from Cockpit being away (a 502, 503 or 504, or no connection) reports how long ago the page first met it
// away: when Cockpit stopped or started in the minutes after that, the server takes the error for a restart and does
// not log it, however long the restart took.
// Counts of control uses (uses.js) do not use it: nobody waits on them and they go again every 15 s anyway, so they
// log such an answer, or a request with no answer at all, only once it has lasted two minutes of the page visible
// and online, and, for no answer, while Cockpit answered the page's other requests.
// A read goes through `readThroughRestart`, which reads the
// answer's body within the same tries: a connection that breaks off mid-body (a reload, a laptop waking)
// is tried again like one that never came up. A timer's start goes through `readWithin`, which tries the
// same way but gives up a set time after the press: a start Cockpit gets much later would count time the
// user never meant. A timer's pause goes through `sendPressed`, which tries until Cockpit answers and tells
// Cockpit each time how long ago the user pressed it, so Cockpit stops the timer at the press. An action (a send, a hand-off, a done) goes through `sendAction`, which carries an id
// Cockpit recognises and tries again until Cockpit answers, so a flaky connection delays an action and never
// loses it (SPEC CODE-5). The browser keeps each action until then, so one still waiting when its page closes
// or reloads goes from the next Cockpit page; when Cockpit does not take it after all, the words written for
// it go back into their box.
//
// Cockpit's success answers are JSON: `answerJson` throws on a success whose body is not, so a page never
// works on with an empty answer in place of Cockpit's.
//
// A page left open across a deploy runs the code it loaded against the newer Cockpit's answers, and may
// read a field Cockpit no longer sends. A page that knows it is older sets
// `window.pageOutdated`; its errors then say so in their place, so the sweep reads them apart from the
// running code's errors and a reload may be all they need.

(() => {
  const RESTART_WAITS_MS = [500, 1000, 2000, 4000, 8000, 15000];
  const AWAY = [502, 503, 504];
  const REPEAT_MS = 60e3;
  const HOUR_MS = 3600e3;
  const PER_HOUR = 30;
  // Past this many different errors, the page counts the new ones as one.
  const DISTINCT = 200;
  const TOO_MANY = `over ${DISTINCT} different errors on this page; the later ones are only in the browser console`;
  const LOADED_AT = new Date(performance.timeOrigin).toISOString();
  // Per error, by place and message (numbers aside, as the server counts them): its report, its sightings
  // since the last report, when that went, and the timer of the next.
  const seen = new Map();
  let sends = [];

  // A try with no answer by then meets a stalled connection (a train's wifi, a phone between masts): it is
  // dropped and tried again, rather than holding the page still for minutes.
  const TRY_MS = 30e3;
  // The page shows an action as sent at once; only one still without an answer after SLOW_MS counts as waiting and
  // shows in "N waiting to send"
  const SLOW_MS = 15e3;
  // Actions on their way, by id: leaving the page asks first while any is. Those with no answer after
  // SLOW_MS wait for a connection, and the page shows their count.
  const sending = new Set();
  const unsent = new Set();

  // How long this page has been hidden or offline since it loaded, and how much of that offline, in ms: the time a
  // laptop slept, a phone lay in a pocket or the user was in another tab. `awayClock()` reads both, with `at`, the
  // `performance.now()` then; the difference between two readings is the time away between them.
  let awayMs = 0;
  let offlineMs = 0;
  let awayFrom = document.hidden || !navigator.onLine ? performance.now() : null;
  let offlineFrom = navigator.onLine ? null : performance.now();
  function noteAway() {
    const now = performance.now();
    if (awayFrom !== null) awayMs += now - awayFrom;
    if (offlineFrom !== null) offlineMs += now - offlineFrom;
    awayFrom = document.hidden || !navigator.onLine ? now : null;
    offlineFrom = navigator.onLine ? null : now;
  }
  addEventListener("online", noteAway);
  addEventListener("offline", noteAway);
  document.addEventListener("visibilitychange", noteAway);
  function reading() {
    noteAway();
    return { at: performance.now(), away: awayMs, offline: offlineMs };
  }
  // The reading when Cockpit, or the proxy in front of it, last answered a request of this page, or null: the proof
  // that the page's connection worked then. The browser's online flag is no such proof, as it stays on while a wifi
  // has no way out. `noteAnswered` takes it, from `throughRestart` here and from the pages' own requests.
  let lastAnswer = null;
  function noteAnswered() {
    lastAnswer = reading();
  }
  function awayClock() {
    return { ...reading(), answered: lastAnswer };
  }
  // The `performance.now()` when Cockpit first answered 502, 503 or 504, or a try met no connection, since it last
  // answered otherwise, or null: the start of the time Cockpit was away, which the page's error reports carry.
  let awaySince = null;
  // The start of the last time Cockpit was away, and when it answered again: a request whose last try met it away
  // can be logged just after another request got an answer, up to a try's length later. Past AWAY_ENDED_MS an
  // error from Cockpit away is a new time away, whatever the start of the last one.
  let lastAwaySince = null;
  let awayEndedAt = -Infinity;
  const AWAY_ENDED_MS = 2 * TRY_MS;
  // A page's keepalive requests share 64 KiB, and Chromium keeps counting a request's body there until the page
  // reads Cockpit's answer: with enough answers unread, the browser refuses every later keepalive request of the
  // page (SPEC CODE-2). An answer no caller reads is read here, and returned.
  function drainAnswer(response) {
    if (!response.bodyUsed) response.arrayBuffer().catch(() => {});
    return response;
  }

  // Each action on its way is kept in the browser's storage under OUTBOX and its id until Cockpit answers it:
  // {path, method, headers, body, tryMs, words, at, n, page}. `words`: {store, key, text}, the words the user
  // wrote for it and the box they came from (`key` within the drafts map stored under `store`, or null where
  // `store` holds the box's text itself), or null. Each page holds a lock named after it for as long as it is
  // open (Web Locks end with their page, also when the browser kills it), so an entry whose page holds none any
  // more is left behind: the next page takes it over and sends it with the same id, which Cockpit does once.
  const OUTBOX = "cockpit.outbox.";
  const PUT_BACK = "cockpit.put-back";
  const PAGE_LOCK = "cockpit-page:";
  const PAGE_ID = crypto.randomUUID();
  // An action left behind longer than this is not sent: what the user meant a day ago may no longer hold.
  const KEEP_MS = 24 * 3600e3;
  const RESCAN_MS = 60e3;
  // Without Web Locks a page cannot tell entries left behind from another tab's, so it keeps none.
  const keeping = Boolean(navigator.locks);
  // Actions of this page kept in storage, which leaving the page does not drop.
  const stored = new Set();
  // Presses this page holds back until Cockpit answered the one before them (`holdLeave`): not sent yet, so
  // not kept, and leaving the page would drop them.
  const holding = new Set();
  let kept = 0;
  if (keeping) navigator.locks.request(PAGE_LOCK + PAGE_ID, () => new Promise(() => {}));

  // Callers pass only requests that do the same however often they arrive: reads, the presence report,
  // an error report, a timer's start or pause, and actions sent with an id of their own (`sendAction`),
  // which Cockpit does once however often the id arrives.
  // `read` takes Cockpit's answer; a connection that breaks off while it reads is tried again too.
  // A read's failure while the page is hidden or the browser offline (a laptop asleep, a network dropped on
  // the user's side) says nothing about Cockpit: the tries start over once the page is visible and online.
  // `giveUpAt`: the `performance.now()` after which no new try starts, or Infinity; a try already on its
  // way runs to its end. `action`: an action's id, whose tries never give up while the page is open, or null.
  // `tryMs`: how long one try waits for its whole answer. `options.body` may be a function, which makes each
  // try's body when it goes.
  async function throughRestart(path, options, read, giveUpAt, action, tryMs) {
    let tries = 0;
    let since = performance.now();
    // The last answer to any request of the page when these tries began, and when the latest of them went. A
    // give-up with no answer in between met a connection that did not work, the user's network, not Cockpit:
    // it carries `unreached`, which `logError` does not log. A give-up at `giveUpAt` also counts the last try's
    // own answer, such as a 502 that came after `giveUpAt`.
    let heardAtStart = lastAnswer;
    let heardAtTry = lastAnswer;
    // Whether any try went, and how long the tries waited for the page to be visible and online again.
    let tried = false;
    let away = 0;
    // An action with no answer after SLOW_MS shows as waiting, also while its first try still hangs.
    const slow = action === null ? null : setTimeout(() => markUnsent(action, true), SLOW_MS);
    if (action !== null) sending.add(action);
    try {
      for (;;) {
        if (performance.now() > giveUpAt) {
          const seconds = Math.round(away / 1000);
          // `gaveUp` marks the give-up for `readWithin`.
          if (!tried) throw Object.assign(new TypeError("not sent: it could not go within a minute of the press, as this page was offline or in the background or an earlier press was still on its way"), { gaveUp: true });
          throw Object.assign(new TypeError(seconds
            ? `not sent in time: this page was offline or in the background for ${seconds} s after the press`
            : "not sent: Cockpit was out of reach for all the time this request is tried"), { gaveUp: true, unreached: lastAnswer === heardAtStart, cockpitAway: !seconds });
        }
        // The last try's failure counts only when the page is still visible and online as it fails; otherwise
        // the tries start over once it is back. An action has no last try.
        const last = () => action === null && tries >= RESTART_WAITS_MS.length && !document.hidden && navigator.onLine;
        const attempt = tryOnce(tryMs);
        try {
          const body = typeof options.body === "function" ? options.body() : options.body;
          tried = true;
          heardAtTry = lastAnswer;
          const response = await fetch(path, { ...options, body, signal: attempt.signal });
          noteAnswered();
          if (!AWAY.includes(response.status)) {
            if (awaySince !== null) {
              lastAwaySince = awaySince;
              awayEndedAt = performance.now();
            }
            awaySince = null;
          } else if (awaySince === null) awaySince = performance.now();
          if (!AWAY.includes(response.status) || last()) return await read(response);
          drainAnswer(response);
        } catch (failure) {
          if (!lostConnection(failure, attempt.signal)) throw failure;
          if (awaySince === null) awaySince = performance.now();
          if (last() && !attempt.backOnline()) {
            const seconds = Math.round((performance.now() - since) / 1000);
            throw Object.assign(new TypeError(`${failure.message}: no connection to Cockpit in ${seconds} s of tries`, { cause: failure }),
              { unreached: heardAtTry === heardAtStart, cockpitAway: true });
          }
        } finally {
          attempt.end();
        }
        // An action is sent from a hidden page too: the user pressed it and may have moved to another tab.
        if (!navigator.onLine || (action === null && document.hidden)) {
          const left = performance.now();
          await pageBack(action === null);
          away += performance.now() - left;
          tries = 0;
          since = performance.now();
          heardAtStart = lastAnswer;
          continue;
        }
        // The browser came back online during the try: the next try goes at once.
        if (attempt.backOnline()) continue;
        await pause(RESTART_WAITS_MS[Math.min(tries, RESTART_WAITS_MS.length - 1)]);
        tries += 1;
      }
    } finally {
      if (action !== null) {
        clearTimeout(slow);
        sending.delete(action);
        markUnsent(action, false);
      }
    }
  }

  // One try's signal: it ends the try when it outlasts `ms` (a stalled connection), or when the browser comes
  // back online, since a try that hangs across a dropped network is on a dead connection; the next try goes
  // at once. `end` clears its timer once the try is over; `backOnline` says whether the browser came back.
  function tryOnce(ms) {
    const controller = new AbortController();
    let online = false;
    const timer = setTimeout(() => controller.abort(), ms);
    const drop = () => {
      online = true;
      controller.abort();
    };
    addEventListener("online", drop);
    return {
      signal: controller.signal,
      backOnline: () => online,
      end: () => {
        clearTimeout(timer);
        removeEventListener("online", drop);
      },
    };
  }

  // fetch, and a body read, reject with a TypeError when the connection to Cockpit fails, and with an
  // AbortError when the try's own signal ended it.
  function lostConnection(failure, signal) {
    return failure instanceof TypeError || signal.aborted;
  }

  // Resolves once the browser is online and, when `visible`, the page visible.
  function pageBack(visible) {
    return new Promise((resolve) => {
      const check = () => {
        if ((visible && document.hidden) || !navigator.onLine) return;
        removeEventListener("online", check);
        document.removeEventListener("visibilitychange", check);
        resolve();
      };
      addEventListener("online", check);
      document.addEventListener("visibilitychange", check);
    });
  }

  // Waits `ms`, or less when the browser comes back online: the next try goes at once.
  function pause(ms) {
    return new Promise((resolve) => {
      const done = () => {
        clearTimeout(timer);
        removeEventListener("online", done);
        resolve();
      };
      const timer = setTimeout(done, ms);
      addEventListener("online", done);
    });
  }

  function markUnsent(action, waiting) {
    const before = unsent.size;
    if (waiting) unsent.add(action);
    else unsent.delete(action);
    if (unsent.size !== before) showUnsent();
  }

  // The count of actions waiting for a connection, on every page: in the header's `#unsent` where the page
  // has one, otherwise in a note of its own at the bottom of the page.
  function showUnsent() {
    let note = document.getElementById("unsent");
    if (!note) {
      if (!unsent.size) return;
      note = Object.assign(document.createElement("span"), { id: "unsent", className: "unsent unsent-floating" });
      note.setAttribute("role", "status");
      document.body.append(note);
    }
    note.hidden = unsent.size === 0;
    note.textContent = `${unsent.size} waiting to send`;
    const kept = [...unsent].every((action) => stored.has(action));
    note.title = kept ? "No connection; they go once Cockpit answers, also after a reload"
      : "No connection; they go once Cockpit answers";
  }

  // Leaving the page drops the actions on their way that the browser could not keep (storage blocked or full)
  // and the presses held back behind another, so the browser asks first while any is.
  addEventListener("beforeunload", (event) => {
    if (holding.size || [...sending].some((action) => !stored.has(action))) event.preventDefault();
  });

  // A press the page holds back until Cockpit answered the one before it: leaving the page asks first until
  // `sent` settles.
  function holdLeave(sent) {
    holding.add(sent);
    const drop = () => holding.delete(sent);
    sent.then(drop, drop);
  }

  function fetchThroughRestart(path, options) {
    return throughRestart(path, options, async (response) => response, Infinity, null, TRY_MS);
  }

  async function withJson(response) {
    return { response, data: await answerJson(response) };
  }

  // A read: Cockpit's answer and its body as JSON.
  function readThroughRestart(path, options) {
    return throughRestart(path, options, withJson, Infinity, null, TRY_MS);
  }

  // A repeatable action that counts only soon after the user made it: tried like a read until `giveUpAt`.
  // `pressed`: `awayClock()` at the press. When the page was hidden or offline for at least half the time from the
  // press to `giveUpAt` (a laptop asleep, a phone in a pocket), the give-up is the user's device's doing, not
  // Cockpit's: it carries `pageAway`, which `logError` does not log, `awaySeconds`, the time away since the press,
  // and `offline`, whether any of that time was offline.
  async function readWithin(path, options, giveUpAt, pressed) {
    try {
      return await throughRestart(path, options, withJson, giveUpAt, null, TRY_MS);
    } catch (error) {
      if (!error.gaveUp || !pressed) throw error;
      const now = awayClock();
      const away = now.away - pressed.away;
      if (away * 2 < giveUpAt - pressed.at) throw error;
      throw Object.assign(error, { pageAway: true, awaySeconds: Math.round(away / 1000), offline: now.offline > pressed.offline });
    }
  }

  // A timer's pause, which Cockpit applies at the moment the user made it however late it arrives (TRACK-2): an
  // action with an id of its own, which Cockpit does once, tried until Cockpit answers, also from a hidden page, and
  // counted as waiting meanwhile, so leaving the page asks first. It waits in this page only. Each try's JSON body
  // carries `ago_ms`, the time since the press at `pressedAt` (a Date.now()) by the browser's clock, which runs on
  // while a laptop sleeps; a change of that clock meanwhile moves the stop by as much, within the span.
  function sendPressed(path, pressedAt, startPress) {
    const action = crypto.randomUUID();
    const body = () => JSON.stringify({ ago_ms: Math.max(0, Date.now() - pressedAt), start_press: startPress ?? undefined });
    const headers = { "content-type": "application/json", "x-cockpit-action": action };
    return throughRestart(path, { method: "POST", credentials: "same-origin", headers, body }, withJson, Infinity, action, TRY_MS);
  }

  // An action (SPEC CODE-5): Cockpit's answer and its body as JSON. It carries an id of its own and is sent
  // again until Cockpit answers; Cockpit keeps the answer to each id, so a repeat after a lost answer does
  // nothing twice. A refusal (4xx) is Cockpit's answer and comes back like any other. Until an answer comes,
  // the browser keeps the action, so a page closed or reloaded meanwhile leaves it to the next page (`sendLeft`).
  // `tryMs`: how long one try may take, longer for an upload. `words`: what the user wrote for it and the box
  // it came from ({store, key, text}, under OUTBOX), or null. Actions left behind by a closed page that this
  // page is sending go first, so an older press never lands after a newer one.
  function sendAction(path, options, tryMs, words) {
    const action = crypto.randomUUID();
    const headers = { ...options.headers, "x-cockpit-action": action };
    const entry = { path, method: options.method, headers, body: options.body, tryMs, words };
    if (editName(entry)) {
      try {
        entry.unsavedEdit = localStorage.getItem(`cockpit.unsaved:${path}`);
      } catch (failure) {
        logError("reading an edit kept after a reload", failure);
      }
    }
    keep(action, entry);
    const read = async (response) => {
      try {
        const answer = await withJson(response);
        if (answer.response.ok) clearSavedEdit(entry);
        forget(action);
        return answer;
      } catch (failure) {
        // A connection that breaks off mid-answer, or a try ended by its own timer or a reconnect, is tried
        // again; any other failure (an answer that is no JSON) ends the action.
        if (!(failure instanceof TypeError) && failure.name !== "AbortError") forget(action);
        throw failure;
      }
    };
    // On its way from the press on, also while it waits behind the ones left behind (an upload asks before leaving).
    sending.add(action);
    return leftSent.then(() => throughRestart(path, { ...options, headers }, read, Infinity, action, tryMs));
  }

  // An upload's body (a Blob) does not fit in storage, and a request to a path the install keeps unrecorded carries
  // text Cockpit never stores: either waits in its page only, and leaving the page asks first.
  function keep(action, entry) {
    if (!keeping || (entry.body !== undefined && typeof entry.body !== "string")
      || JSON.parse(document.body.dataset.unrecordedPrefixes || "[]")
        .some((prefix) => entry.path.startsWith(prefix))) return;
    try {
      localStorage.setItem(OUTBOX + action, JSON.stringify({ ...entry, at: Date.now(), n: ++kept, page: PAGE_ID }));
      stored.add(action);
    } catch {
      // Storage blocked or full: the action waits in this page only, and leaving it asks first.
    }
  }

  function forget(action) {
    stored.delete(action);
    try {
      localStorage.removeItem(OUTBOX + action);
    } catch {
      // Storage blocked: nothing was kept.
    }
  }

  // Sends the actions that closed or reloaded pages left behind, oldest first, each once Cockpit answered the one
  // before, then tells the page to fetch the latest state ("cockpit:sent-left"). It runs as the page loads, when the
  // browser comes back online, and every minute for pages closed meanwhile; one run at a time, which this page's
  // own actions wait for (`leftSent`).
  let sendingLeft = null;
  let leftSent = Promise.resolve();
  function sendLeft() {
    if (!keeping || sendingLeft) return;
    sendingLeft = (async () => {
      try {
        const left = await navigator.locks.request("cockpit-outbox", takeLeft);
        for (const [index, [action, entry]] of left.entries()) {
          if (await sendOne(action, entry)) continue;
          giveBack(left.slice(index));
          break;
        }
        if (left.length) dispatchEvent(new CustomEvent("cockpit:sent-left"));
      } catch (failure) {
        logError("sending actions left by a closed page", failure);
      } finally {
        sendingLeft = null;
      }
    })();
    leftSent = sendingLeft;
  }

  // Entries this page took but cannot send now (signed out): any page may take them again, this one at its next look.
  function giveBack(entries) {
    for (const [action, entry] of entries) {
      stored.delete(action);
      try {
        localStorage.setItem(OUTBOX + action, JSON.stringify({ ...entry, page: null }));
      } catch (failure) {
        logError("giving back an action left by a closed page", failure);
      }
    }
  }

  // Under the outbox lock, so two pages never take the same entry: the entries whose page is gone become this page's.
  // An entry kept after the look at the locks may belong to a page that opened since: it waits for the next look.
  async function takeLeft() {
    const lookedAt = Date.now();
    const locks = await navigator.locks.query();
    const open = new Set([...locks.held, ...locks.pending].map((lock) => lock.name));
    const left = [];
    try {
      const keys = Array.from({ length: localStorage.length }, (_, i) => localStorage.key(i)).filter((key) => key && key.startsWith(OUTBOX));
      for (const key of keys) {
        const entry = JSON.parse(localStorage.getItem(key));
        if (entry.page === PAGE_ID || open.has(PAGE_LOCK + entry.page) || entry.at >= lookedAt) continue;
        entry.page = PAGE_ID;
        localStorage.setItem(key, JSON.stringify(entry));
        left.push([key.slice(OUTBOX.length), entry]);
      }
    } catch (failure) {
      logError("reading actions left by a closed page", failure);
    }
    return left.sort(([, a], [, b]) => a.at - b.at || a.n - b.n);
  }

  // Sends one action left behind; resolves to whether the next may go. A 401 (signed out) keeps it for the page
  // after the sign-in. A taken answer puts back the words it gave back: those Cockpit stopped (`unsent`, Take
  // back) and words kept with a press that does not use them up (`words.back`), on whichever page sends it. The
  // page may show them itself: it gets "cockpit:left-answered" ({path, data, words, back}) and cancels it.
  async function sendOne(action, entry) {
    stored.add(action);
    if (Date.now() - entry.at > KEEP_MS) {
      forget(action);
      notSent(entry, "it waited over a day for a connection");
      return true;
    }
    const options = { method: entry.method, headers: entry.headers, body: entry.body, credentials: "same-origin" };
    let answer;
    try {
      answer = await throughRestart(entry.path, options, withJson, Infinity, action, entry.tryMs);
    } catch (failure) {
      forget(action);
      logError("sending an action left by a closed page", failure);
      notSent(entry, failure.message);
      return true;
    }
    if (answer.response.status === 401) return false;
    forget(action);
    if (!answer.response.ok) notSent(entry, answer.data.detail || `Cockpit answered ${answer.response.status}`);
    else answered(entry, answer.data);
    return true;
  }

  // An action left behind that Cockpit did not take: the page says so ("cockpit:not-sent", whose listener shows
  // `message`), and the words written for it go back into their box. A page that holds that box in memory puts
  // them back itself, says so (from `reason`) and cancels the event; otherwise they go into the box's storage,
  // where its page reads them. Words whose box writes to another session since stay in the notice
  // (`writesElsewhere`).
  function notSent(entry, reason) {
    const name = editName(entry);
    if (name) {
      const body = JSON.parse(entry.body);
      // A Priorities save sends {goals}; one queued by a page from before priorities could pause sent {texts}.
      const goals = (body.goals ?? body.texts?.map((text) => ({ text, paused: false }))) || [];
      const text = entry.path === "/api/goals"
        ? goals.map((goal) => (goal.paused ? `(paused) ${goal.text}` : goal.text)).join("\n")
        : [...body.rules, body.sources].join("\n");
      try {
        localStorage.setItem(`cockpit.unsaved:${entry.path}`, entry.body);
      } catch (failure) {
        logError("keeping an edit refused after a reload", failure);
      }
      const message = `${name} not saved: ${reason}. Open ${name} to retry. Your edit: ${text || "(empty)"}`;
      dispatchEvent(new CustomEvent("cockpit:not-sent", { detail: { words: null, message, reason } }));
      return;
    }
    // A Take back keeps its reply box even with no words in it.
    const words = entry.words && entry.words.text.trim() ? entry.words : null;
    const kept = words !== null && writesElsewhere(words);
    const message = !words
      ? `An action from the page you left did not go through: ${reason}`
      : kept
        ? `Not sent from the page you left: ${reason}. Its box now writes to another session, so your words were not put back: ${words.text}`
        : `Not sent from the page you left: ${reason}. Your words are back in their box.`;
    const event = new CustomEvent("cockpit:not-sent", { cancelable: true, detail: { words, message, reason } });
    if (dispatchEvent(event) && words && !kept) putBack(words, words.text);
  }

  function editName(entry) {
    if (entry.method !== "PUT") return null;
    if (entry.path === "/api/goals") return "Priorities";
    if (entry.path === "/api/inbox-settings") return "Inbox settings";
    return null;
  }

  // A successful retry clears the recovered edit while a different recovered edit stays available.
  function clearSavedEdit(entry) {
    if (!editName(entry)) return;
    try {
      const key = `cockpit.unsaved:${entry.path}`;
      const current = localStorage.getItem(key);
      if (current === entry.body || current === entry.unsavedEdit) localStorage.removeItem(key);
    } catch (failure) {
      logError("clearing a saved edit", failure);
    }
  }

  // Whether the box `words` names writes to another "To" than the one they were sent to: Send there would take
  // them to that one.
  function writesElsewhere(words) {
    if (words.key === null || !words.choice) return false;
    try {
      const targets = new Map(JSON.parse(localStorage.getItem(words.targets) || "[]"));
      return targets.has(words.key) && targets.get(words.key) !== words.choice;
    } catch (failure) {
      logError("reading the To of a box left by a closed page", failure);
      return false;
    }
  }

  function answered(entry, data) {
    clearSavedEdit(entry);
    // A Take back's box writes where its unsent message was going, also when only page comments went with it.
    const words = entry.words && { ...entry.words, ...(data.unsent_to ? unsentTarget(data.unsent_to) : {}) };
    const back = [data.unsent, words && words.back && words.text].filter(Boolean).join("\n\n");
    if (!words || !(back || data.unsent_to)) return;
    const event = new CustomEvent("cockpit:left-answered", { cancelable: true, detail: { path: entry.path, data, words, back } });
    if (!dispatchEvent(event)) return;
    if (!writesElsewhere(words)) {
      putBack(words, back);
      return;
    }
    // A box that writes to another To since keeps its words, To and picks; the words taken back stay in the
    // notice, as those of an action not sent do (`notSent`).
    if (back) {
      const message = `Taken back from the page you left. Its box now writes to another session, so your words were not put back: ${back}`;
      dispatchEvent(new CustomEvent("cockpit:not-sent", { detail: { words: null, message, reason: "taken back" } }));
    }
  }

  // Where a message Take back stopped was going (the server's `unsent_to`), as its box's "To" choice and the picks
  // a new session was to start with.
  function unsentTarget(to) {
    return {
      choice: to.session_id ? `session:${to.session_id}` : `new:${to.new_cwd}`,
      picks: to.new_cwd ? { provider: to.provider, model: null, effort: null, account: null, ...to.options } : null,
    };
  }

  // Puts `text` at the top of the box `words` names, with its "To" choice and picks, in storage, where its page
  // finds it.
  function putBack(words, text) {
    try {
      const add = (now) => ((now || "").trim() ? `${text.trim()}\n\n${now}` : text);
      if (words.key === null) {
        localStorage.setItem(words.store, add(localStorage.getItem(words.store)));
      } else {
        // The box's "To" choice goes back with the words, unless the user chose again since. It goes first, so an
        // open tab that takes the words already has their choice (app.js's storage listener).
        if (words.choice) {
          const targets = new Map(JSON.parse(localStorage.getItem(words.targets) || "[]"));
          if (!targets.has(words.key)) localStorage.setItem(words.targets, JSON.stringify([...targets, [words.key, words.choice]]));
        }
        // So do the picks a new session from the box was to start with (SPEC SESSION-22).
        if (words.picks && words.starts) {
          const starts = new Map(JSON.parse(localStorage.getItem(words.starts) || "[]"));
          if (!starts.has(words.key)) localStorage.setItem(words.starts, JSON.stringify([...starts, [words.key, words.picks]]));
        }
        if (!text) return;
        const drafts = new Map(JSON.parse(localStorage.getItem(words.store) || "[]"));
        drafts.set(words.key, add(drafts.get(words.key)));
        localStorage.setItem(words.store, JSON.stringify([...drafts]));
        tellPutBack(words.key, text, drafts.get(words.key));
      }
    } catch (failure) {
      logError("putting back the words of an action left by a closed page", failure);
    }
  }

  // Tells the other tabs that words went back into the draft `key` (PUT_BACK), so one where the user types in that
  // box adds them to it (app.js); a tab that only reads the drafts would take the box's stored text instead.
  // `after`: the draft with the words, which a box that took the stored drafts already holds.
  function tellPutBack(key, text, after) {
    try {
      localStorage.setItem(PUT_BACK, JSON.stringify({ key, text, after, id: crypto.randomUUID() }));
    } catch (failure) {
      logError("telling other tabs about words put back", failure);
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => sendLeft());
  else sendLeft();
  addEventListener("online", () => sendLeft());
  setInterval(() => sendLeft(), RESCAN_MS);

  // The body of Cockpit's answer as JSON. An error answer may carry none (Caddy's 502 has an empty body):
  // it reads as {}, and the caller reports the status.
  async function answerJson(response) {
    const text = await response.text();
    try {
      return JSON.parse(text);
    } catch (failure) {
      if (!response.ok) return {};
      throw new Error(`Cockpit answered ${response.status} with no JSON: ${JSON.stringify(text.slice(0, 80))}`, { cause: failure });
    }
  }

  // The error's root: a refusal wraps Cockpit's answer as its cause.
  function root(error) {
    let inner = error;
    while (inner && inner.cause) inner = inner.cause;
    return inner;
  }

  // Whether the error, or one it wraps, carries `mark`.
  function carries(error, mark) {
    for (let inner = error, depth = 0; inner && depth < 20; inner = inner.cause, depth += 1) {
      if (inner[mark]) return true;
    }
    return false;
  }

  // Sends the error's sightings since its last report as one count. With the hour's reports spent, it
  // tries again once the oldest of them is an hour old.
  function flush(entry) {
    entry.timer = null;
    const now = Date.now();
    sends = sends.filter((at) => now - at < HOUR_MS);
    if (sends.length >= PER_HOUR) {
      entry.timer = setTimeout(() => flush(entry), sends[0] + HOUR_MS - now + 1000);
      return;
    }
    sends.push(now);
    entry.sentAt = now;
    const count = entry.unsent;
    const away = entry.awaySince;
    entry.unsent = 0;
    entry.awaySince = null;
    // `away_ms` goes with each try, so a report that waited out a restart itself still says when Cockpit went away.
    fetchThroughRestart("/api/page-errors", {
      method: "POST",
      credentials: "same-origin",
      keepalive: true,
      headers: { "content-type": "application/json" },
      body: () => JSON.stringify({ ...entry.report, count, loaded_at: LOADED_AT,
        away_ms: away === null ? null : Math.round(performance.now() - away) }),
    }).then((response) => {
      drainAnswer(response);
      if (!response.ok) console.warn(`Cockpit did not log an error on this page: it answered ${response.status}`);
    }, (failure) => console.warn("Cockpit did not log an error on this page:", failure));
  }

  function logError(spot, error) {
    console.error(`Cockpit, ${spot}:`, error);
    try {
      const place = window.pageOutdated && window.pageOutdated() ? `${spot}, on a page older than the running Cockpit` : spot;
      const inner = root(error);
      // Cockpit's own answers: a refusal is the server's word, shown to the user; a 401 signs in again.
      // The server's failures (5xx) are errors all the same.
      if (inner && inner.status && inner.status < 500) return;
      // A press given up because the page was asleep, offline or in the background (`readWithin`): the user's
      // device, not a fault in Cockpit, and the page shows it.
      if (inner && inner.pageAway) return;
      // Tries that met no answer while no other request of the page got one either (`throughRestart`): the user's
      // network was down, though the browser may have said online, and the page shows it.
      if (carries(error, "unreached")) return;
      const message = String((error && error.message) || error || "unknown error").slice(0, 2000);
      const stack = String((inner && inner.stack) || (error && error.stack) || "").slice(0, 8000);
      let key = `${place}\n${message}`.replace(/\d+/g, "N");
      let report = { page: location.pathname, place, message, stack };
      // Cockpit away: its proxy's 502, its own 503 when full, a 504, or no connection.
      const away = (inner && AWAY.includes(inner.status)) || carries(error, "cockpitAway");
      if (!seen.has(key) && seen.size >= DISTINCT) {
        key = "too many";
        report = { page: location.pathname, place: "errors.js", message: TOO_MANY, stack: "" };
      }
      let entry = seen.get(key);
      if (!entry) {
        entry = { report, unsent: 0, sentAt: -Infinity, timer: null, awaySince: null };
        seen.set(key, entry);
      }
      entry.report.stack = report.stack;
      // The sightings sent as one count report when Cockpit went away for the latest of them, and only while every
      // one of them met Cockpit away: a restart never covers a sighting met with Cockpit there, such as a 500 that
      // counts as one error with a 503.
      if (!away) entry.awaySince = null;
      else if (entry.unsent === 0 || entry.awaySince !== null) {
        const ended = performance.now() - awayEndedAt <= AWAY_ENDED_MS;
        entry.awaySince = awaySince ?? (ended ? lastAwaySince : null) ?? performance.now();
      }
      entry.unsent += 1;
      if (entry.timer !== null) return;
      const wait = entry.sentAt + REPEAT_MS - Date.now();
      if (wait <= 0) flush(entry);
      else entry.timer = setTimeout(() => flush(entry), wait);
    } catch (failure) {
      console.warn("Cockpit could not log the error above:", failure);
    }
  }

  addEventListener("error", (event) => {
    const where = event.filename ? `${event.filename.replace(location.origin, "")}:${event.lineno}` : "script";
    logError(`uncaught in ${where}`, event.error || event.message);
  });
  addEventListener("unhandledrejection", (event) => logError("unhandled rejection", event.reason));

  window.logError = logError;
  window.answerIsAway = (status) => AWAY.includes(status);
  window.fetchThroughRestart = fetchThroughRestart;
  window.readThroughRestart = readThroughRestart;
  window.readWithin = readWithin;
  window.awayClock = awayClock;
  window.noteAnswered = noteAnswered;
  window.drainAnswer = drainAnswer;
  window.sendPressed = sendPressed;
  window.sendAction = sendAction;
  window.holdLeave = holdLeave;
  window.tellPutBack = tellPutBack;
  window.unsentTarget = unsentTarget;
  window.TRY_MS = TRY_MS;
  window.answerJson = answerJson;
})();
