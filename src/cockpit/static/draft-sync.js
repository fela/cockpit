"use strict";

// Unsent words follow the user between devices (SPEC NEXT-67). The page keeps its drafts in this browser's storage
// (app.js `saveDrafts`); this file also keeps them on Cockpit (drafts.py), so a box typed in on the laptop holds the
// same words on the phone. Each stored value of a draft is one entry: the words of a box ("drafts/<box key>"), its
// "To" choice ("choices/…"), a new session's picks ("starts/…"), the rule a rule draft changes ("replacing/…") and an answer
// picked on a card and not sent yet ("picks/…").
//
// The browser's storage holds the latest drafts of all its tabs, since every tab saves on each keystroke, so the
// entries go to Cockpit from there and come from Cockpit into it. The page remembers each entry's value and version
// as Cockpit last had them (SYNCED_KEY, shared by the tabs). An entry that differs in storage goes to Cockpit a
// moment after the last change, named with that version; an entry Cockpit holds at a newer version comes into
// storage, and this tab's drafts, from the state; other tabs take it from storage. When both changed, the words
// join, Cockpit's first; for the other entries this browser's value stays and goes to Cockpit. A box the page leaves
// as drawn, such as the one the user types in, keeps its words, and its entries take Cockpit's once the user leaves
// it, as a box does with another tab's (app.js `takeStoredDrafts`).
//
// Each request to Cockpit waits in the record until its answer comes, and the page sends it again, under the same id,
// before anything new: Cockpit answers it as it did the first time (drafts.py), so a lost answer leaves no doubt about
// which words Cockpit took. Until then Cockpit's newer values of its entries wait too.

(() => {
  const SYNCED_KEY = "cockpit.drafts-synced";
  const PUSH_MS = 800;
  // drafts.CHANGES: at most this many entries go in one request; the rest go in the next.
  const CHANGES = 200;
  // drafts.VALUE_CHARS: a longer entry stays in this browser. A function, as are the kinds, since app.js, which
  // declares what they name, runs after this file.
  const valueChars = () => 6 * ADD_MAX + 2;
  // Each kind of entry: where storage keeps it and this tab's map of it.
  const kinds = () => ({
    drafts: [DRAFTS_KEY, ui.drafts],
    choices: [TARGETS_KEY, ui.choices],
    starts: [STARTS_KEY, ui.starts],
    replacing: [REPLACING_KEY, ui.replacing],
    picks: [PICKS_KEY, ui.picks],
  });
  // Private windows and blocked storage keep drafts in the one tab (app.js `writeStored`), and here on this device.
  const STORAGE = (() => {
    try {
      localStorage.setItem(`${SYNCED_KEY}.probe`, "1");
      localStorage.removeItem(`${SYNCED_KEY}.probe`);
      return true;
    } catch {
      return false;
    }
  })();

  let timer = null;
  // The entries on their way to Cockpit (key → value), and whether a change came while they were.
  let sending = null;
  let again = false;
  // Entries Cockpit refused while their box was held: they wait for the user to leave the box, which joins them.
  const refused = new Set();
  // Entries too long for Cockpit, each reported once.
  const tooLong = new Set();

  // Feedback words belong to the part of the page picked in this tab, so they stay in this browser.
  function local(entry) {
    const key = entry.slice(entry.indexOf("/") + 1);
    return key === PICK_KEY || key.startsWith(`${PICK_KEY}:`);
  }

  // What Cockpit last had: entries as key → [value, version], when this browser last heard Cockpit take a change, and
  // the requests sent whose answer has not come ({id, follows, changes}), oldest first.
  function readSynced() {
    const stored = readStored(SYNCED_KEY, null);
    return { entries: new Map(stored?.entries || []), answeredAt: stored?.answeredAt || 0, unanswered: stored?.unanswered || [] };
  }

  function writeSynced(record) {
    writeStored(SYNCED_KEY, { entries: [...record.entries], answeredAt: record.answeredAt, unanswered: record.unanswered });
  }

  // The drafts this browser's storage keeps: kind → Map.
  function readStore() {
    return Object.fromEntries(Object.entries(kinds()).map(([kind, [key]]) => [kind, new Map(readStored(key, []))]));
  }

  // The stored drafts as entries: key → JSON text.
  function entriesOf(store) {
    const entries = new Map();
    for (const [kind, pairs] of Object.entries(store)) {
      for (const [key, value] of pairs) {
        const entry = `${kind}/${key}`;
        if (!local(entry)) entries.set(entry, JSON.stringify(value));
      }
    }
    return entries;
  }

  // Whether an entry is of a kind this page keeps. One of another kind a newer page wrote, and this page leaves it to
  // that page: it neither records the entry's version nor sends a change of it, since sending what this page does not
  // hold would empty it on Cockpit.
  function keeps(entry) {
    return Object.hasOwn(kinds(), entry.slice(0, entry.indexOf("/")));
  }

  // Puts one entry, of a kind this page keeps, in storage and in this tab's drafts.
  function setEntry(store, entry, value) {
    const cut = entry.indexOf("/");
    const key = entry.slice(cut + 1);
    for (const map of [store[entry.slice(0, cut)], kinds()[entry.slice(0, cut)][1]]) {
      if (value) map.set(key, JSON.parse(value));
      else map.delete(key);
    }
  }

  function writeStore(store) {
    for (const [kind, [key]] of Object.entries(kinds())) writeStored(key, [...store[kind]]);
    if (typeof showItemDraftMarks === "function") showItemDraftMarks();
    drawStartLines();
  }

  // The boxes the page leaves as drawn for now: those of a section holding the field the user is in, which a poll
  // does not draw again (app.js `render`), and the open Add box, new or adding to a to-do.
  function heldBoxes() {
    const boxes = new Set(SECTIONS.filter(([ids]) => holdsField(ids)).flatMap(([ids]) =>
      ids.flatMap((id) => [...document.querySelectorAll(`#${id} [data-key]`)].map((field) => field.dataset.key))));
    const typing = typingKey();
    if (typing) boxes.add(typing);
    if (ui.adding) {
      boxes.add(ADD_KEY);
      if (ui.addTo) boxes.add(addMoreKey(ui.addTo.id));
    }
    return boxes;
  }

  // Whether an entry belongs to a held box: its words, choice, picks or rule. A card's "To" choice is keyed by the
  // card, and its drafts by the card and the list chosen.
  function held(entry, boxes) {
    const key = entry.slice(entry.indexOf("/") + 1);
    for (const box of boxes) if (box === key || box.startsWith(`${key}:`)) return true;
    return false;
  }

  // Both sides changed one entry: words join, Cockpit's first, unless one already holds the other.
  function joined(entry, theirs, ours) {
    if (!entry.startsWith("drafts/") || !theirs || !ours) return ours || theirs;
    const [a, b] = [JSON.parse(theirs).trim(), JSON.parse(ours).trim()];
    if (b.includes(a)) return ours;
    if (a.includes(b)) return theirs;
    return JSON.stringify(`${a}\n\n${b}`);
  }

  // One entry against Cockpit's [value, version]. Returns whether the stored drafts changed.
  function settle(record, store, entries, entry, theirs) {
    const [base, version] = record.entries.get(entry) || ["", 0];
    if (theirs[1] <= version || !keeps(entry)) return false;
    const ours = entries.get(entry) || "";
    const next = ours === base ? theirs[0] : joined(entry, theirs[0], ours);
    if (theirs[0]) record.entries.set(entry, theirs);
    else record.entries.delete(entry);
    if (next === ours) return false;
    setEntry(store, entry, next);
    return true;
  }

  // The drafts in a state Cockpit sent (`drafts`: {at, entries}); `askedAt` is when the page asked for it. Returns
  // whether this tab's drafts changed, so the page draws them.
  function take(drafts, askedAt) {
    if (!drafts || !STORAGE) return false;
    const record = readSynced();
    // A state asked before Cockpit took this browser's last change may not hold it yet.
    if (askedAt < record.answeredAt) return false;
    const store = readStore();
    const entries = entriesOf(store);
    const boxes = heldBoxes();
    const waiting = new Set(record.unanswered.flatMap((sent) => sent.changes.map((change) => change.key)));
    let changed = false;
    // A refused entry Cockpit has emptied since is in neither: it joins nothing and goes to Cockpit again.
    for (const entry of new Set([...record.entries.keys(), ...Object.keys(drafts.entries), ...refused])) {
      if (sending?.has(entry) || waiting.has(entry) || held(entry, boxes) || local(entry)) continue;
      refused.delete(entry);
      // An entry missing from a state newer than this browser's version of it was emptied on Cockpit since.
      const version = record.entries.get(entry)?.[1] || 0;
      const theirs = drafts.entries[entry] || ["", drafts.at > version ? drafts.at : 0];
      if (settle(record, store, entries, entry, theirs)) changed = true;
    }
    writeSynced(record);
    if (changed) writeStore(store);
    // Changes and requests that met no network, and words joined here, go now.
    if (timer === null) schedule(0);
    return changed;
  }

  // Sends the entries that differ from Cockpit's a moment after the last change, or after `wait` ms.
  function schedule(wait) {
    if (!STORAGE) return;
    clearTimeout(timer);
    timer = setTimeout(push, wait);
  }

  // The entries whose value differs from the one last sent: the newest request waiting for its answer, else Cockpit's.
  function changes(record) {
    const entries = entriesOf(readStore());
    const sentValues = new Map(record.unanswered.flatMap((sent) => sent.changes.map((change) => [change.key, change.value])));
    const list = [];
    for (const entry of new Set([...entries.keys(), ...record.entries.keys(), ...sentValues.keys()])) {
      const value = entries.get(entry) || "";
      const [base, version] = record.entries.get(entry) || ["", 0];
      if (!keeps(entry) || value === (sentValues.has(entry) ? sentValues.get(entry) : base) || refused.has(entry)) continue;
      if (value.length > valueChars()) {
        if (!tooLong.has(entry)) {
          tooLong.add(entry);
          logError("keeping drafts on Cockpit", new Error(`${entry} holds ${value.length} characters, more than Cockpit keeps; it stays in this browser`));
        }
        continue;
      }
      list.push({ key: entry, value, base: version });
    }
    return list.slice(0, CHANGES);
  }

  // drafts.FOLLOWS: a request names at most this many of the requests before it.
  const FOLLOWS = 100;

  // A new request of the entries that differ from Cockpit's, kept in the record until its answer comes; null when
  // none differs. It names the requests sent before it whose answer has not come (`follows`), so Cockpit knows its
  // words were typed over theirs.
  function request(record) {
    const list = changes(record);
    if (!list.length) return null;
    const sent = { id: crypto.randomUUID(), follows: record.unanswered.slice(-FOLLOWS).map((other) => other.id), changes: list };
    record.unanswered.push(sent);
    writeSynced(record);
    return sent;
  }

  // Sends the oldest request whose answer has not come, else a new one, and takes in the answer.
  async function push() {
    timer = null;
    if (sending) { again = true; return; }
    if (!navigator.onLine) return;
    const record = readSynced();
    const sent = record.unanswered[0] || request(record);
    if (!sent) return;
    const list = sent.changes;
    sending = new Map(list.map((change) => [change.key, change.value]));
    try {
      let response;
      try {
        response = await fetch("/api/drafts", jsonRequest("POST", sent));
      } catch {
        // A request that met no network goes again after the next state: the drafts stay in this browser.
        return;
      }
      const data = await answerJson(response);
      if (response.status === 401) return;
      const failed = new Error(`Cockpit did not keep the drafts: it answered ${response.status} ${JSON.stringify(data.detail || "")}`);
      // Cockpit or a proxy failing may have kept the words, so the request goes again as it is after the next state.
      if (response.status >= 500) throw failed;
      const record = readSynced();
      record.unanswered = record.unanswered.filter((other) => other.id !== sent.id);
      // A refusal here is a fault in the page or Cockpit, so it is reported (logError leaves out refusals with a
      // status), and the request goes no more: its entries go again in a new one.
      if (!response.ok) {
        writeSynced(record);
        throw failed;
      }
      const waiting = new Set(record.unanswered.flatMap((other) => other.changes.map((change) => change.key)));
      const taken = new Set(data.taken);
      const store = readStore();
      const entries = entriesOf(store);
      const boxes = heldBoxes();
      let changed = false;
      for (const change of list) {
        const theirs = data.entries[change.key];
        if (taken.has(change.key)) {
          if (theirs[0]) record.entries.set(change.key, theirs);
          else record.entries.delete(change.key);
        } else if (waiting.has(change.key)) continue;
        // A later request of this browser holds newer words of the entry: its answer settles it.
        else if (held(change.key, boxes)) refused.add(change.key);
        else if (settle(record, store, entries, change.key, theirs)) changed = true;
      }
      record.answeredAt = Date.now();
      writeSynced(record);
      if (changed) {
        writeStore(store);
        if (ui.state) safeRender();
      }
      // The joined words, a longer list's rest, and the next request waiting or the changes made meanwhile go next.
      again = true;
    } catch (error) {
      logError("keeping drafts on Cockpit", error);
    } finally {
      sending = null;
      if (again) {
        again = false;
        schedule(PUSH_MS);
      }
    }
  }

  // The page is going away: the requests whose answer has not come go again, under their ids, and the words typed
  // since the last request, if any, go in a new one that follows them, all in requests that outlive the page. Their
  // answers are read when this browser sends them again.
  function leave() {
    clearTimeout(timer);
    timer = null;
    // Before app.js has run, the page holds no drafts to send.
    if (!navigator.onLine || typeof readStored !== "function") return;
    const record = readSynced();
    request(record);
    for (const sent of record.unanswered) fetch("/api/drafts", { ...jsonRequest("POST", sent), keepalive: true }).catch(() => {});
  }

  // The words typed just before the page goes away still reach Cockpit.
  addEventListener("pagehide", leave);
  document.addEventListener("visibilitychange", () => { if (document.hidden) leave(); });

  window.CockpitDraftSync = { changed: () => schedule(PUSH_MS), take };
})();
