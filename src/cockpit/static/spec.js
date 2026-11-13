"use strict";

// The spec page: docs/SPEC.md block by block. The user comments on a block, edits
// one block, or edits the whole file. The same page lists the user's skills and shows
// each one the same way (SPEC SKILLS-1). Text goes in with textContent; only the
// server's *_html fields (Markdown rendered without raw HTML) use innerHTML.

const { icon } = Icons;
const POLL_MS = 15000;
const TOAST_MS = 7000;
// Values offered for these columns; a value already in the row is always kept as an option.
const CHOICES = {
  Origin: ["User", "User (pain)", "Agent"],
  Strength: ["Must", "Should", "Could", "Agent default", "Cut?"],
  Status: ["Live", "Shipped", "In build", "Not done", "Dropped"],
};
const STATUS_ORDER = ["Not done", "In build", "Shipped", "Live", "Dropped"];
const STATUS_TIPS = { Shipped: "Deployed and checked; it turns Live once you have tried it" };
const SHOWN_COLUMNS = new Set(["ID", "Requirement", "Origin", "Strength", "Status", "Comments"]);
// A requirement's editor leaves these out: IDs are never reused, and a save writes Last change itself.
const UNEDITED_COLUMNS = new Set(["ID", "Last change"]);
// A row in the "comments on changed parts" banner names its requirement by the ID in its quote.
const QUOTED_ID = /^\|\s*([A-Z]+-\d+)\s*\|/;

// What the page shows, read from its path: the spec, the list of skills, one skill, or the writing examples.
const DOC = pageDoc(location.pathname);

function pageDoc(path) {
  const skill = path.match(/^\/skills\/([a-z0-9][a-z0-9-]*)\/?$/);
  if (skill) {
    return {
      tab: "/skills", api: `/api/skills/${skill[1]}`, name: `the ${skill[1]} skill`, title: `${skill[1]} skill`, skill: skill[1],
      commentWords: "A session changes the skill to answer your comment",
      commentSent: "Comment sent: a sweep hands it to a session within minutes, which changes the skill",
      saveWords: "Saves and pushes to the skills branch",
      saved: "Saved and pushed to the skills branch",
      published: "committed and pushed",
    };
  }
  if (/^\/skills\/?$/.test(path)) return { tab: "/skills", list: true, name: "the skills", title: "skills" };
  if (/^\/writing\/?$/.test(path)) return { tab: "/skills", writing: true, name: "the writing examples", title: "writing examples" };
  return {
    tab: "/spec", api: "/api/spec", name: "the spec", title: "spec",
    commentWords: "An agent records your comment in the spec",
    commentSent: "Comment sent: a sweep records it in the spec within minutes",
    saveWords: "Saves and pushes to the deploy branch",
    saved: "Saved and pushed to the deploy branch",
    published: "committed and pushed",
  };
}

const ui = {
  spec: null,
  fingerprint: "",
  // What the user has open: one block's editor or comment box, or the whole-spec editor.
  editing: null,
  commenting: null,
  // When the comment box opened; a comment sends how long the user wrote it (TRACK-2).
  commentSince: 0,
  // The block tapped last; on touch screens it shows that block's actions.
  selected: null,
  drafts: new Map(),
  // Unsent writing brought back over Reload (UI-13), until the first spec it can be placed on.
  held: window.PageReload?.take() ?? null,
  // Writing brought back for a part that changed meanwhile. It shows in a banner and never opens over the newer text.
  orphans: [],
  // The version the whole-spec editor opened on, and the newest version seen while something is open.
  allVersion: null,
  latestVersion: null,
  filter: null,
  // The id of the section heading in view, marked in the table of contents.
  section: "",
  busy: false,
  // Chip choices made while a save was on its way, oldest first (saveCell).
  cellsNext: [],
  // Settles the hold on leaving the page (`holdLeave`) once the waiting chip choices all went.
  cellsSent: null,
  // The latest chip choice per row and column ("key:column") while choices save, which the chips show over the
  // spec as the last reload read it.
  cellChoices: new Map(),
  // The spec as the last load read it, also when an open editor kept the page from showing it: waiting chip
  // choices go onto these rows.
  latest: null,
  moved: false,
  toastTimer: null,
  // The writing examples page: the examples last loaded, the ones shown whole, and the one whose Remove was
  // pressed once (a second press removes it).
  examples: [],
  openExamples: new Set(),
  removing: null,
};

// The table of contents: a column beside the spec on wide screens, a row of chips under the
// header on narrow ones. Its links box and marker persist across redraws, so the row keeps its
// scroll position and the marker slides from the old section to the new one.
const toc = {
  nav: document.getElementById("toc"),
  links: null,
  marker: null,
  observer: null,
  lastY: 0,
  // Until then a scroll comes from a jump to a section and leaves the chip row in place.
  quietUntil: 0,
};
const narrow = window.matchMedia("(max-width: 900px)");
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [name, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") el.className = value;
    else if (name === "text") el.textContent = value;
    else if (name === "html") el.innerHTML = value;
    else if (name === "value") el.value = value;
    else if (name.startsWith("on")) el.addEventListener(name.slice(2), value);
    else el.setAttribute(name, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function fill(id, ...nodes) {
  Loading.settle(document.getElementById(id));
  document.getElementById(id).replaceChildren(...nodes.flat(Infinity).filter(Boolean));
}

function slug(text) {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

// "now", "6m", "3h", "2d"; the tooltip on a [data-time] element gives the exact time.
function short(iso) {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 172800) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

function plural(count, noun) {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

// A square button with an icon; its words are the tooltip. `key` is its shortcut, or null.
function iconButton(name, title, onclick, key) {
  return h("button", { class: "button small ghost icon-only", type: "button", title, "aria-label": title, "data-key": key, onclick }, icon(name));
}

function cancelButton() {
  return h("button", { class: "button ghost icon-only", type: "button", title: "Cancel", "aria-label": "Cancel", "data-key": "Esc", onclick: closeAll }, icon("x"));
}

function toast(text, fail) {
  const el = document.getElementById("toast");
  el.replaceChildren(h("span", { text }));
  el.className = fail ? "toast fail" : "toast";
  el.hidden = false;
  clearTimeout(ui.toastTimer);
  ui.toastTimer = setTimeout(() => { el.hidden = true; }, TOAST_MS);
}

async function api(method, path, body) {
  const options = {
    method,
    headers: body === undefined ? {} : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  };
  // A read waits through a restart of Cockpit; an action is sent again until Cockpit has it (CODE-5).
  return replyData(method === "GET" ? await readThroughRestart(path, options) : await sendAction(path, options, TRY_MS, null));
}

function anyOpen() {
  return ui.editing !== null || ui.commenting !== null;
}

async function load() {
  if (DOC.list) { await loadList(); return; }
  if (DOC.writing) { await loadWriting(); return; }
  try {
    const spec = await api("GET", DOC.api);
    const fingerprint = JSON.stringify(spec);
    if (fingerprint === ui.fingerprint) return;
    ui.latest = spec;
    if (anyOpen() && ui.spec) {
      // Leave what the user has open alone; say the file moved and catch up when they close it.
      ui.latestVersion = spec.version;
      ui.moved = spec.version !== ui.spec.version;
      renderBanner();
      return;
    }
    ui.spec = spec;
    ui.fingerprint = fingerprint;
    ui.moved = false;
    if (ui.held) placeHeld(spec);
    render();
  } catch (error) {
    logError(`loading ${DOC.name}`, error);
    // The next good read draws the page again, banners and all, also when the file did not change.
    ui.fingerprint = "";
    fill("banner", h("div", { class: "banner fail", text: `Could not load ${DOC.name}: ${error.message}` }));
    LOADING.remove();
  }
}

// The skills list: each skill's name, what it is for, its length, and the user's comments waiting for a session.
async function loadList() {
  try {
    const listed = await api("GET", "/api/skills");
    const fingerprint = JSON.stringify(listed);
    if (fingerprint === ui.fingerprint) return;
    ui.fingerprint = fingerprint;
    renderList(listed);
  } catch (error) {
    logError("loading the skills", error);
    ui.fingerprint = "";
    fill("banner", h("div", { class: "banner fail", text: `Could not load the skills: ${error.message}` }));
    LOADING.remove();
  }
}

function renderList(listed) {
  const words = listed.skills.reduce((sum, skill) => sum + skill.words, 0);
  fill("summary",
    h("span", { class: "count" }, h("span", { text: "Skills" }), h("b", { text: listed.skills.length })),
    h("span", { class: "count" }, h("span", { text: "Words" }), h("b", { text: words.toLocaleString() })));
  fill("banner");
  // One line per skill, so all of them fit on a screen; the whole description is the row's tooltip.
  fill("doc",
    h("h1", { text: "Skills", title: `${listed.path}, on ${listed.branch}` }),
    h("a", { class: "writing-link", href: "/writing" },
      icon("writing-good"), "Writing examples"),
    h("ul", { class: "skill-list" }, listed.skills.map((skill) => h("li", {},
      h("a", { class: "skill-row", href: `/skills/${skill.name}`, title: skill.description },
        h("span", { class: "skill-name", text: skill.name }),
        h("span", { class: "skill-description", text: skill.description }),
        skill.unread_comments > 0 && h("span", {
          class: "skill-waiting", title: `${plural(skill.unread_comments, "comment")} waiting for a session`,
        }, icon("chat"), h("span", { class: "comment-count", text: skill.unread_comments })),
        h("span", { class: "meta skill-words", text: `${skill.words.toLocaleString()} words` }))))));
}

// The writing examples (SPEC FEEDBACK-8): what the user marked as well written, then what they found hard to
// understand, newest first, each with its text as it was, the user's note, and the session that wrote it.
// Agents read the same list with `cockpit writing list`.
async function loadWriting() {
  try {
    const examples = await api("GET", "/api/writing");
    const fingerprint = JSON.stringify(examples);
    if (fingerprint === ui.fingerprint) return;
    ui.fingerprint = fingerprint;
    ui.examples = examples;
    renderWriting();
  } catch (error) {
    logError("loading the writing examples", error);
    ui.fingerprint = "";
    fill("banner", h("div", { class: "banner fail", text: `Could not load the writing examples: ${error.message}` }));
    LOADING.remove();
  }
}

const KIND_WORDS = { step: "Step", page: "Shared page", part: "Part of a page" };
// Longer than this, an example shows its start until the user opens it.
const LONG_EXAMPLE = 700;

function renderWriting() {
  const examples = ui.examples;
  fill("summary",
    h("a", { class: "count skill-back", href: "/skills" }, icon("back"), h("b", { text: "Skills" })),
    h("span", { class: "count" }, h("span", { text: "Examples" }), h("b", { text: examples.length })));
  fill("banner");
  const good = examples.filter((example) => example.verdict === "good");
  const bad = examples.filter((example) => example.verdict === "bad");
  if (!examples.length) {
    fill("doc",
      h("h1", { text: "Writing examples" }),
      h("p", { class: "skill-lead", text: "Nothing marked yet. Press Feedback (F), pick a step or a page, and press Good writing or Hard to understand." }));
    return;
  }
  fill("doc",
    h("h1", { text: "Writing examples" }),
    h("h2", { class: "writing-part", text: `Good writing (${good.length})` }),
    good.length
      ? h("ol", { class: "writing-list" }, good.map(writingCard))
      : h("p", { class: "writing-empty", text: "Nothing marked yet. The next time a step or a report reads well, press F, "
        + "pick it, and press Good writing." }),
    h("h2", { class: "writing-part", text: `Hard to understand (${bad.length})` }),
    bad.length
      ? h("ol", { class: "writing-list" }, bad.map(writingCard))
      : h("p", { class: "writing-empty", text: "Nothing marked yet. The next time a step or a report is hard to follow, "
        + "press F, pick it, and press Hard to understand." }));
}

function day(iso) {
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

// Who wrote it: the session's name and the start of its id (the whole id on hover), else the writer's name.
function writer(example) {
  if (example.session_id) {
    const id = example.session_id.slice(0, 8);
    return h("span", { title: `Session ${example.session_id}` }, `by ${example.session_name || "session"} (${id})`);
  }
  return example.written_by && h("span", { text: `by ${example.written_by}` });
}

// The skills the writing session had loaded when it wrote the example, each with its version: the skill folder's
// commit then, or the MD5 of the text it loaded (the folder and load time on hover).
function skillsLoaded(example) {
  if (!example.skills.length) return null;
  return h("p", { class: "writing-skills meta" }, h("span", { text: "Skills loaded:" }),
    example.skills.map((skill) => h("span", {
      class: "chip", title: `${skill.folder}, loaded ${day(skill.loaded_at)}`, text: `${skill.name} ${skill.version}`,
    })));
}

function writingText(example, open) {
  if (example.format === "html") {
    return h("p", { class: "writing-page" }, example.page_id
      ? h("a", { href: `/pages/${example.page_id}`, target: "_blank", rel: "noopener" }, icon("doc"), example.title)
      : "The page was removed from its item; its HTML is kept (cockpit writing get " + example.id + ").");
  }
  const clamp = example.text.length > LONG_EXAMPLE && !open ? " clamped" : "";
  if (example.format === "markdown") return h("div", { class: `writing-text prose${clamp}`, html: example.text_html });
  return h("blockquote", { class: `writing-text plain${clamp}`, text: example.text });
}

function writingCard(example) {
  const open = ui.openExamples.has(example.id);
  const long = example.format !== "html" && example.text.length > LONG_EXAMPLE;
  return h("li", { class: "writing-card", "data-example": example.id },
    h("div", { class: "writing-head" },
      h("h2", { text: example.title }),
      removeButton(example)),
    h("p", { class: "writing-meta meta" },
      h("span", { class: "chip", title: example.target, text: KIND_WORDS[example.kind] }),
      h("span", { "data-time": example.created_at, text: day(example.created_at) }),
      writer(example),
      example.item_title && h("span", { text: `on ${example.item_title}` })),
    skillsLoaded(example),
    example.note && h("p", { class: "writing-note" }, h("b", { text: "Your note" }), example.note),
    writingText(example, open),
    long && h("button", {
      class: "button small ghost", type: "button",
      onclick: () => {
        if (open) ui.openExamples.delete(example.id); else ui.openExamples.add(example.id);
        renderWriting();
      },
    }, open ? "Show less" : "Show all"));
}

// The first press asks for a second within a few seconds; the second removes the example for good.
function removeButton(example) {
  if (ui.removing !== example.id) {
    return iconButton("x", "Remove example", () => {
      ui.removing = example.id;
      renderWriting();
      setTimeout(() => { if (ui.removing === example.id) { ui.removing = null; renderWriting(); } }, 4000);
    }, null);
  }
  return h("button", { class: "button small writing-remove", type: "button", onclick: () => removeExample(example) },
    "Press again to remove");
}

async function removeExample(example) {
  ui.removing = null;
  try {
    await api("DELETE", `/api/writing/${example.id}`);
    toast("Removed from the writing examples", false);
  } catch (error) {
    logError("removing a writing example", error);
    toast(`Not removed: ${error.message}`, true);
  }
  ui.fingerprint = "";
  await loadWriting();
}

// Blocks and their fields

function cell(block, name) {
  const index = block.columns.indexOf(name);
  return index < 0 ? "" : block.cells[index] ?? "";
}

function cellHtml(block, name) {
  const index = block.columns.indexOf(name);
  return index < 0 ? "" : block.cells_html[index] ?? "";
}

function isRequirement(block) {
  return block.kind === "row" && block.columns[0] === "ID" && /^[A-Z]+-\d+$/.test(block.cells[0]);
}

function rowText(values) {
  return `| ${values.map((value) => value.trim().split(/\s+/).join(" ").replaceAll("|", "\\|")).join(" | ")} |`;
}

// Saving. The Save buttons say "Saving…" until the server answers, then a toast says what the
// save did; the page reloads the file, so the new text shows in place.

// While a save or a comment is on its way, its editor takes no input, so the text it closes with is the text it sent.
// An editor drawn meanwhile (a filter, another Edit) takes none either (`render`).
function holdEditors() {
  for (const box of document.querySelectorAll("#doc .spec-box, #doc .row-edit")) box.inert = true;
}

function save(work, done) {
  const saving = finishSave(work, done);
  holdLeave(saving);
  return saving;
}

async function finishSave(work, done) {
  if (ui.busy) return;
  ui.busy = true;
  holdEditors();
  for (const button of document.querySelectorAll("[data-save]")) {
    button.disabled = true;
    button.textContent = "Saving…";
  }
  try {
    const result = await work();
    done();
    if (!result.changed) toast("Nothing changed", false);
    else if (!result.committed) toast(`Saved to ${DOC.name}, but not ${DOC.published}: ${result.commit_error}`, true);
    else toast(DOC.saved, false);
  } catch (error) {
    logError("saving", error);
    toast(error.message, true);
  } finally {
    // Busy through the reload, so a choice made meanwhile goes onto the row as the reload shows it.
    ui.fingerprint = "";
    try { await load(); } finally { ui.busy = false; }
    // With no choice waiting, the chips show the spec as read, also after a refusal.
    if (!ui.cellsNext.length) ui.cellChoices.clear();
    render();
    saveNextCell();
  }
}

// The oldest waiting chip choice, onto its row as it now reads; one that changes nothing gives way to the next.
function saveNextCell() {
  while (!ui.busy && ui.cellsNext.length) {
    const next = ui.cellsNext.shift();
    const row = (ui.latest || ui.spec).blocks.find((block) => block.key === next.key);
    if (row) sendCell(row, next.name, next.value);
    else toast(`${next.name} not saved: its row is gone`, true);
  }
  if (!ui.cellsNext.length && ui.cellsSent) {
    ui.cellsSent();
    ui.cellsSent = null;
  }
  // Waiting choices whose rows are gone leave the chips to show the spec again.
  if (!ui.busy && !ui.cellsNext.length && ui.cellChoices.size) {
    ui.cellChoices.clear();
    render();
  }
}

// Saving closes what is open, so the reload that follows shows the new spec; the drafts of other
// blocks stay for when the user opens them again.
function savePart(block, newText) {
  const unchanged = block.kind === "row" ? newText === rowText(block.cells) : newText === block.text;
  if (unchanged) {
    // The server stamps every row save as an edit, so an untouched form is not sent.
    ui.drafts.delete(`edit:${block.key}`);
    ui.drafts.delete(`row:${block.key}`);
    closeAll();
    toast("Nothing changed", false);
    return;
  }
  if (!newText.trim() && !confirm(`Remove this part from ${DOC.name}?`)) return;
  save(() => api("POST", `${DOC.api}/part`, { old: block.text, new: newText }), () => {
    ui.editing = null;
    ui.commenting = null;
    ui.drafts.delete(`edit:${block.key}`);
    ui.drafts.delete(`row:${block.key}`);
  });
}

// One cell of a row, chosen from a chip's menu: saved at once. One chosen while a save is on its way, or while
// earlier choices wait (as during the reload after a save), waits behind them and then goes onto the row as the
// reload shows it. A waiting choice is in no outbox yet, so leaving the page asks first until the last
// of them went.
function saveCell(block, name, value) {
  ui.cellChoices.set(`${block.key}:${name}`, value);
  if (!ui.busy && !ui.cellsNext.length) {
    sendCell(block, name, value);
    return;
  }
  if (!ui.cellsSent) holdLeave(new Promise((resolve) => { ui.cellsSent = resolve; }));
  ui.cellsNext.push({ key: block.key, name, value });
}

function sendCell(block, name, value) {
  const values = [...block.cells];
  values[block.columns.indexOf(name)] = value;
  savePart(block, rowText(values));
}

function sendComment(block) {
  const text = (ui.drafts.get(`comment:${block.key}`) || "").trim();
  if (!text || ui.busy) return;
  ui.busy = true;
  holdEditors();
  const seconds = Math.round((Date.now() - ui.commentSince) / 1000);
  api("POST", "/api/feedback", { text, target: block.key, quote: block.text.slice(0, 4000), detail: "", step_id: null, item_id: null, seconds })
    .then(() => {
      ui.drafts.delete(`comment:${block.key}`);
      ui.commenting = null;
      toast(DOC.commentSent, false);
    }, (error) => {
      logError("sending a comment", error);
      toast(error.message, true);
    })
    .finally(async () => {
      ui.fingerprint = "";
      try { await load(); } finally { ui.busy = false; }
      if (!ui.cellsNext.length) ui.cellChoices.clear();
      render();
      saveNextCell();
    });
}

// Pieces

// A block's Comment and Edit buttons.
function actions(block) {
  const count = block.comments.length;
  const commentWords = count ? "Comments" : "Comment";
  return h("div", { class: count ? "block-actions shown" : "block-actions" },
    h("button", {
      class: count ? "button small ghost comment-button" : "button small ghost icon-only", type: "button",
      title: commentWords, "aria-label": commentWords, onclick: () => openComment(block),
    }, icon("chat"), count > 0 && h("span", { class: "comment-count", text: count })),
    iconButton("pencil", "Edit", () => openEditor(block), null));
}

function openComment(block) {
  ui.commenting = ui.commenting === block.key ? null : block.key;
  ui.commentSince = Date.now();
  ui.editing = null;
  render();
  focusSoon(`comment:${block.key}`);
}

function openEditor(block) {
  ui.editing = ui.editing === block.key ? null : block.key;
  ui.commenting = null;
  render();
  focusSoon(`edit:${block.key}`);
}

function focusSoon(key) {
  const el = document.querySelector(`[data-key="${CSS.escape(key)}"]`);
  if (el) el.focus();
}

function closeAll() {
  ui.editing = null;
  ui.commenting = null;
  if (ui.moved) { ui.fingerprint = ""; load(); }
  render();
}

function closeMenus() {
  for (const menu of document.querySelectorAll("details.menu[open]")) menu.open = false;
}

function statusMark(status) {
  return h("span", { class: `status-mark status-${slug(status)}`, "aria-hidden": "true" });
}

// A chip that opens a menu of a column's values. Choosing one shows it on the chip at once and
// hands it to `choose`.
function choiceMenu(name, value, choose) {
  const options = CHOICES[name].includes(value) ? CHOICES[name] : [value, ...CHOICES[name]];
  const label = (text) => [name === "Status" && statusMark(text), h("span", { text })];
  const summary = h("summary", { class: `chip choice choice-${slug(name)} value-${slug(value)}`, title: name, "aria-label": `${name}: ${value}` },
    label(value), icon("chevron"));
  // The value shown, which a choice waiting to save (`saveCell`) changes before the reload does.
  let shown = value;
  const item = (option) => [h("span", { class: "menu-label" }, label(option)), option === shown && icon("check")];
  const buttons = options.map((option) => h("button", {
    type: "button", role: "menuitemradio", "aria-checked": option === shown ? "true" : "false",
    onclick: () => {
      menu.open = false;
      if (option === shown) return;
      shown = option;
      summary.className = `chip choice choice-${slug(name)} value-${slug(option)}`;
      summary.setAttribute("aria-label", `${name}: ${option}`);
      summary.replaceChildren(...label(option).filter(Boolean), icon("chevron"));
      options.forEach((each, at) => {
        buttons[at].setAttribute("aria-checked", each === shown ? "true" : "false");
        buttons[at].replaceChildren(...item(each).filter(Boolean));
      });
      choose(option);
    },
  }, ...item(option)));
  const menu = h("details", { class: "menu choice-menu" }, summary, h("div", { class: "menu-list", role: "menu" }, buttons));
  return menu;
}

function commentList(comments) {
  if (!comments.length) return null;
  return h("ul", { class: "spec-comments" }, comments.map((comment) => h("li", {},
    h("div", { class: "comment-text", text: comment.text }),
    h("div", { class: "comment-meta" },
      h("span", { class: "meta", "data-time": comment.created_at, text: short(comment.created_at) }),
      comment.read_at
        ? h("span", { class: "comment-state read", title: "Read", "aria-label": "Read" }, icon("check"))
        : h("span", { class: "comment-state waiting", title: "Unread", "aria-label": "Waiting for a sweep" })))));
}

function textBox(key, initial, rows, onSubmit) {
  if (!ui.drafts.has(key)) ui.drafts.set(key, initial);
  const area = h("textarea", { class: "field", rows, "data-key": key, value: ui.drafts.get(key) });
  area.addEventListener("input", () => ui.drafts.set(key, area.value));
  area.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); onSubmit(); }
    if (event.key === "Escape") { event.preventDefault(); closeAll(); }
  });
  return area;
}

function commentBox(block) {
  if (ui.commenting !== block.key) return null;
  const key = `comment:${block.key}`;
  const area = textBox(key, "", 3, () => sendComment(block));
  const send = h("button", {
    class: "button primary", type: "button", "data-key": "Ctrl+Enter",
    title: DOC.commentWords, disabled: !area.value.trim(), onclick: () => sendComment(block),
  }, "Comment");
  area.addEventListener("input", () => { send.disabled = !area.value.trim(); });
  return h("div", { class: "spec-box" }, area, h("div", { class: "edit-foot" }, cancelButton(), send));
}

function proseEditor(block) {
  const key = `edit:${block.key}`;
  const submit = () => savePart(block, ui.drafts.get(key) ?? block.text);
  const area = textBox(key, block.text, Math.min(14, block.text.split("\n").length + 2), submit);
  area.classList.add("grow");
  return h("div", { class: "spec-box" },
    area,
    h("div", { class: "edit-foot" },
      cancelButton(),
      h("button", {
        class: "button primary", type: "button", "data-key": "Ctrl+Enter", "data-save": true,
        title: `${DOC.saveWords}; empty removes this part`, onclick: submit,
      }, "Save")));
}

// The editor takes the row's place. A requirement's text is the first, unlabelled field; its
// Strength, Status and Origin are chip menus; the cells it folds away stay folded here.
function rowEditor(block, requirement) {
  const key = `row:${block.key}`;
  if (!ui.drafts.has(key)) ui.drafts.set(key, [...block.cells]);
  const values = ui.drafts.get(key);
  const submit = () => savePart(block, rowText(values));
  const columns = block.columns
    .map((name, index) => ({ name: name || `Column ${index + 1}`, index }))
    .filter(({ name }) => !(requirement && UNEDITED_COLUMNS.has(name)));
  const texts = columns.filter(({ name }) => !Object.hasOwn(CHOICES, name));
  let focusNext = true;
  const field = ({ name, index }, labelled) => {
    const value = values[index] ?? "";
    const rows = Math.min(6, Math.max(labelled ? 1 : 2, Math.ceil(value.length / 90)));
    const area = h("textarea", { class: "field grow", rows, "data-key": focusNext ? `edit:${block.key}` : `edit:${block.key}:${index}`, "aria-label": name, value });
    focusNext = false;
    area.addEventListener("input", () => { values[index] = area.value; });
    area.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); submit(); }
      if (event.key === "Escape") { event.preventDefault(); closeAll(); }
    });
    return labelled ? h("label", { class: "edit-field" }, h("span", { text: name }), area) : area;
  };
  const lead = requirement ? texts.find(({ name }) => name === "Requirement") : null;
  const folded = requirement ? texts.filter((column) => column !== lead && !SHOWN_COLUMNS.has(column.name)) : [];
  const shown = texts.filter((column) => column !== lead && !folded.includes(column));
  const choices = columns.filter(({ name }) => Object.hasOwn(CHOICES, name));
  const saveWords = requirement
    ? "Saves and commits; the row's Last change notes your edit."
    : DOC.saveWords;
  return h("div", { class: "row-edit" },
    lead && field(lead, false),
    choices.length > 0 && h("div", { class: "req-meta" }, choices.map(({ name, index }) =>
      choiceMenu(name, values[index] ?? "", (option) => { values[index] = option; }))),
    shown.map((column) => field(column, true)),
    folded.length > 0 && h("details", { class: "req-more" },
      h("summary", { text: folded[0].name }),
      folded.map((column) => field(column, true))),
    h("div", { class: "edit-foot" },
      cancelButton(),
      h("button", { class: "button primary", type: "button", "data-key": "Ctrl+Enter", "data-save": true, title: saveWords, onclick: submit }, "Save")));
}

function selectedClass(block) {
  return ui.selected === block.key ? " selected" : "";
}

function requirementCard(block) {
  const id = block.cells[0];
  const status = cell(block, "Status");
  const origin = cell(block, "Origin");
  const folded = block.columns
    .map((name, index) => [name, index])
    .filter(([name, index]) => !SHOWN_COLUMNS.has(name) && block.cells[index]);
  const editing = ui.editing === block.key;
  const saveTo = (name) => (value) => saveCell(block, name, value);
  const shown = (name, value) => ui.cellChoices.get(`${block.key}:${name}`) ?? value;
  return h("div", { class: `req status-${slug(status)}${editing ? " editing" : ""}${selectedClass(block)}`, id, "data-block": block.key },
    h("div", { class: "req-head" },
      h("a", { class: "req-id", href: `#${id}`, text: id, title: origin && origin !== "Agent" ? `Origin: ${origin}` : null }),
      editing ? rowEditor(block, true) : h("div", { class: "req-text", html: cellHtml(block, "Requirement") }),
      actions(block)),
    !editing && h("div", { class: "req-meta" },
      choiceMenu("Strength", shown("Strength", cell(block, "Strength")), saveTo("Strength")),
      choiceMenu("Status", shown("Status", status), saveTo("Status")),
      origin === "Agent" && choiceMenu("Origin", shown("Origin", origin), saveTo("Origin"))),
    !editing && cell(block, "Comments") && h("p", { class: "req-note", html: cellHtml(block, "Comments") }),
    !editing && folded.length > 0 && h("details", { class: "req-more" },
      h("summary", { text: folded[0][0] }),
      folded.map(([name, index]) => h("div", { class: "req-field" },
        h("span", { class: "req-label", text: name }), h("span", { html: block.cells_html[index] })))),
    commentList(block.comments),
    commentBox(block));
}

function plainRow(block) {
  const editing = ui.editing === block.key;
  return h("div", { class: `req${editing ? " editing" : ""}${selectedClass(block)}`, "data-block": block.key },
    h("div", { class: "req-head" },
      editing
        ? rowEditor(block, false)
        : h("div", { class: "req-text" }, block.cells_html.map((html, index) =>
          h("span", { class: "plain-cell", title: block.columns[index] || "", html }))),
      actions(block)),
    commentList(block.comments),
    commentBox(block));
}

// A skill's frontmatter shows as its description; Edit opens the whole frontmatter.
function isFrontmatter(block) {
  return Boolean(DOC.skill) && block.kind === "code" && block.start === 0 && block.text.startsWith("---");
}

// The description line of the frontmatter, with any continuation lines; quotes and a YAML ">" dropped.
function frontmatterDescription(text) {
  const found = text.match(/^description:[ \t]*(.*(?:\n[ \t]+.*)*)/m);
  if (!found) return "";
  return found[1].replace(/^[>|][-+]?/, "").split(/\s+/).filter(Boolean).join(" ").replace(/^["']|["']$/g, "");
}

function proseBlock(block) {
  const editing = ui.editing === block.key;
  const content = block.kind === "heading"
    ? h(`h${Math.min(block.level, 3)}`, { id: slug(block.title), text: block.title })
    : isFrontmatter(block)
      ? h("p", { class: "skill-lead", text: frontmatterDescription(block.text) })
      : h("div", { class: "prose", html: block.html });
  return h("div", { class: `block block-${block.kind}${editing ? " editing" : ""}${selectedClass(block)}`, "data-block": block.key },
    h("div", { class: "block-line" }, editing ? null : content, actions(block)),
    editing && proseEditor(block),
    commentList(block.comments),
    commentBox(block));
}

// Page

function statusCounts(blocks) {
  const counts = new Map();
  for (const block of blocks) {
    if (!isRequirement(block)) continue;
    const status = cell(block, "Status");
    counts.set(status, (counts.get(status) || 0) + 1);
  }
  return counts;
}

function renderSummary() {
  if (DOC.skill) {
    const words = ui.spec.text.split(/\s+/).filter(Boolean).length;
    fill("summary",
      h("a", { class: "count skill-back", href: "/skills" }, icon("back"), h("b", { text: DOC.skill })),
      h("span", { class: "count" }, h("span", { text: "Words" }), h("b", { text: words.toLocaleString() })));
    return;
  }
  const counts = statusCounts(ui.spec.blocks);
  const total = [...counts.values()].reduce((sum, count) => sum + count, 0);
  const chip = (label, value, filter) => h("button", {
    class: `count filter${ui.filter === filter ? " on" : ""}`, type: "button", "aria-pressed": ui.filter === filter ? "true" : "false",
    title: STATUS_TIPS[filter] || null,
    onclick: () => { ui.filter = ui.filter === filter ? null : filter; render(); window.scrollTo({ top: 0 }); },
  }, filter !== null && statusMark(filter), h("span", { text: label }), h("b", { text: value }));
  const statuses = [...counts.keys()].sort((a, b) => STATUS_ORDER.indexOf(a) - STATUS_ORDER.indexOf(b));
  fill("summary", h("div", { class: "filters", role: "group", "aria-label": "Show requirements by status" },
    chip("All", total, null), statuses.map((status) => chip(status, counts.get(status), status))));
}

// Runs after renderDoc: a section the doc does not show (a status filter is on, or the whole
// file is open in the editor) stays listed but cannot be picked.
function renderToc() {
  const links = [];
  let open = null;
  for (const block of ui.spec.blocks) {
    if (block.kind === "heading" && block.level >= 2) {
      const id = slug(block.title);
      open = { count: 0, badge: h("span", { class: "open-count" }) };
      links.push(h("a", {
        class: block.level > 2 ? "sub" : "", href: `#${id}`, "data-section": id,
        "aria-disabled": document.getElementById(id) ? null : "true",
      }, h("span", { class: "toc-title", text: block.title }), open.badge));
    } else if (open && isRequirement(block) && !["Live", "Dropped"].includes(cell(block, "Status"))) {
      open.count += 1;
      open.badge.textContent = String(open.count);
      open.badge.title = `${plural(open.count, "requirement")} not live yet`;
    }
  }
  if (!toc.links) {
    toc.links = h("div", { class: "toc-links" });
    toc.marker = h("span", { class: "toc-marker", "aria-hidden": "true" });
    toc.links.addEventListener("scroll", fadeTocEdges, { passive: true });
    toc.links.addEventListener("click", (event) => {
      if (event.target.closest("a[aria-disabled]")) event.preventDefault();
      else if (event.target.closest("a")) toc.quietUntil = performance.now() + 300;
    });
    toc.nav.replaceChildren(toc.links);
  }
  const left = toc.links.scrollLeft;
  toc.links.replaceChildren(toc.marker, ...links);
  toc.links.scrollLeft = left;
  fadeTocEdges();
  showSection(false);
}

// The chip row fades at an edge only where more chips wait beyond it.
function fadeTocEdges() {
  const row = toc.links;
  if (!row) return;
  row.classList.toggle("fade-start", row.scrollLeft > 1);
  row.classList.toggle("fade-end", row.scrollLeft + row.clientWidth < row.scrollWidth - 1);
}

// Marks the section in view: aria-current on its link, the marker beside it in the column,
// and on narrow screens its chip scrolled into the middle of the row.
function showSection(follow) {
  if (!toc.links) return;
  let current = null;
  for (const link of toc.links.querySelectorAll("a")) {
    if (ui.section && link.dataset.section === ui.section) {
      link.setAttribute("aria-current", "location");
      current = link;
    } else {
      link.removeAttribute("aria-current");
    }
  }
  const marker = toc.marker;
  if (!current) { marker.classList.remove("on"); return; }
  const place = `translateY(${current.offsetTop}px) scaleY(${current.offsetHeight})`;
  if (!marker.classList.contains("on")) {
    // Appearing: in place at once, then it fades in.
    marker.style.transition = "none";
    marker.style.transform = place;
    void marker.offsetWidth;
    marker.style.transition = "";
    marker.classList.add("on");
  } else {
    marker.style.transform = place;
  }
  if (!follow) return;
  const behavior = reducedMotion.matches ? "auto" : "smooth";
  if (narrow.matches) {
    const row = toc.links;
    row.scrollTo({ left: current.offsetLeft - (row.clientWidth - current.offsetWidth) / 2, behavior });
  } else {
    const nav = toc.nav;
    const top = current.offsetTop;
    if (top < nav.scrollTop || top + current.offsetHeight > nav.scrollTop + nav.clientHeight) {
      nav.scrollTo({ top: top - nav.clientHeight / 3, behavior });
    }
  }
}

// How much of the window's top the sticky header (and on narrow screens the chip row) covers; the rail a wide
// window holds the header in (page-rail.css) sits at the left and covers none of it.
// The CSS reads it for the chip row's place and for where a jump to a section lands.
function measureCover() {
  const top = document.querySelector(".top");
  const header = getComputedStyle(top).position === "fixed" ? 0 : top.offsetHeight;
  const strip = narrow.matches ? toc.nav.offsetHeight : 0;
  document.documentElement.style.setProperty("--spec-top", `${header}px`);
  document.documentElement.style.setProperty("--spec-cover", `${header + strip}px`);
  return header + strip;
}

// The section in view is the last heading above a line a third of the way down below the header.
// Any block entering or leaving the window re-checks it, however fast the page scrolled.
function watchSections() {
  if (toc.observer) toc.observer.disconnect();
  toc.observer = new IntersectionObserver(findSection);
  for (const el of document.querySelectorAll("#doc [data-block]")) toc.observer.observe(el);
}

function findSection() {
  const cover = measureCover();
  const line = cover + (window.innerHeight - cover) / 3;
  let current = "";
  for (const heading of document.querySelectorAll("#doc h2[id], #doc h3[id]")) {
    if (heading.getBoundingClientRect().top > line) break;
    current = heading.id;
  }
  if (current === ui.section) return;
  ui.section = current;
  showSection(true);
}

// On narrow screens the chip row tucks under the header while the user reads down the page and
// comes back on the way up. A jump to a section leaves it where it is.
function tuckToc() {
  const y = window.scrollY;
  if (!narrow.matches || performance.now() < toc.quietUntil) { toc.lastY = y; return; }
  if (Math.abs(y - toc.lastY) < 12) return;
  toc.nav.classList.toggle("tucked", y > toc.lastY && y > 160);
  toc.lastY = y;
}

// What Reload carries over (UI-13). An editor opened and left untouched comes back open, with the newest text.
function writingToKeep() {
  if (ui.held) return ui.held;
  if (!ui.spec) return null;
  const blocks = new Map(ui.spec.blocks.map((block) => [block.key, block]));
  const drafts = [];
  const bases = {};
  for (const [key, value] of ui.drafts) {
    if (key === "edit:all") {
      if (value !== ui.spec.text) drafts.push([key, value]);
      continue;
    }
    const [kind, blockKey] = draftParts(key);
    const block = blocks.get(blockKey);
    if (kind === "comment") {
      if (value.trim()) drafts.push([key, value]);
      continue;
    }
    const changed = !block || (kind === "row" ? rowText(value) !== rowText(block.cells) : value !== block.text);
    if (!changed) continue;
    drafts.push([key, value]);
    // While something is open ui.spec stays the version it opened on, so this is the text the writing started from.
    if (block) bases[blockKey] = block.text;
  }
  if (!drafts.length && !ui.orphans.length && !ui.editing && !ui.commenting) return null;
  return {
    drafts,
    bases,
    editing: ui.editing,
    commenting: ui.commenting,
    commentSince: ui.commentSince,
    allVersion: ui.allVersion,
    orphans: ui.orphans,
  };
}

function draftParts(key) {
  const colon = key.indexOf(":");
  return [key.slice(0, colon), key.slice(colon + 1)];
}

// Saving a part replaces the text it opened on, so writing started on older text must not reopen on newer text.
function placeHeld(spec) {
  const { drafts, bases, editing, commenting, commentSince, allVersion, orphans } = ui.held;
  ui.held = null;
  ui.orphans = [...orphans];
  const blocks = new Map(spec.blocks.map((block) => [block.key, block]));
  const lost = new Set();
  for (const [key, value] of drafts) {
    if (key === "edit:all") {
      if (editing === "all" || spec.version === allVersion) ui.drafts.set(key, value);
      else ui.orphans.push(value);
      continue;
    }
    const [kind, blockKey] = draftParts(key);
    const block = blocks.get(blockKey);
    if (block && (kind === "comment" || block.text === bases[blockKey])) {
      ui.drafts.set(key, value);
    } else {
      ui.orphans.push(kind === "row" ? rowText(value) : value);
      lost.add(blockKey);
    }
  }
  if (editing === "all") {
    ui.editing = "all";
    ui.allVersion = ui.drafts.has("edit:all") ? allVersion : spec.version;
    if (ui.allVersion !== spec.version) {
      ui.moved = true;
      ui.latestVersion = spec.version;
    }
  } else if (editing && blocks.has(editing) && !lost.has(editing)) {
    ui.editing = editing;
  }
  if (commenting && blocks.has(commenting)) {
    ui.commenting = commenting;
    ui.commentSince = commentSince;
  }
}

function renderBanner() {
  const banners = [];
  if (ui.orphans.length) {
    banners.push(h("div", { class: "banner neutral" },
      h("div", { class: "banner-head", text: "Your writing on parts that changed" }),
      h("ul", { class: "unplaced-list" }, ui.orphans.map((text, index) => h("li", {},
        h("span", { class: "unplaced-text kept-writing", text }),
        iconButton("x", "Drop this text", () => { ui.orphans.splice(index, 1); renderBanner(); }, null))))));
  }
  if (ui.moved) {
    banners.push(h("div", { class: "banner banner-line" },
      h("span", { title: "Your edit still saves if its part did not change", text: `${DOC.name[0].toUpperCase()}${DOC.name.slice(1)} changed since you opened it.` }),
      iconButton("retry", "Close what is open, drop its text and reload", () => { ui.drafts.clear(); closeAll(); }, null)));
  }
  if (ui.spec && ui.spec.unplaced_comments.length) {
    banners.push(h("div", { class: "banner neutral" },
      h("div", { class: "banner-head", text: "Comments on changed parts" }),
      h("ul", { class: "unplaced-list" }, ui.spec.unplaced_comments.map(unplacedComment))));
  }
  fill("banner", banners);
}

// A comment whose part has changed since: the requirement's ID when the quote is a requirement
// row, else the start of the quote; the quote without its table pipes is the tooltip.
function unplacedComment(comment) {
  const id = comment.quote.match(QUOTED_ID);
  const words = comment.quote.split("|").map((part) => part.trim()).filter(Boolean);
  const where = id
    ? h("a", { class: "unplaced-where req-id", href: `#${id[1]}`, text: id[1] })
    : h("span", { class: "unplaced-where", text: words.join(" ").slice(0, 40) });
  return h("li", { title: id ? `${id[1]}: ${words[1] || ""}` : words.join(" ").slice(0, 300) },
    where,
    h("span", { class: "unplaced-text", text: comment.text }),
    h("span", { class: "meta", "data-time": comment.created_at, text: short(comment.created_at) }));
}

function renderDoc() {
  const blocks = ui.spec.blocks;
  const nodes = [];
  let group = null;
  const groupFor = (cls) => {
    if (!group || group.className !== cls) {
      group = h({ "spec-list": "ul", "spec-list numbered": "ol" }[cls] || "div", { class: cls });
      nodes.push(group);
    }
    return group;
  };
  const visible = ui.filter === null ? blocks : filtered(blocks);
  for (const block of visible) {
    if (block.kind === "row") {
      groupFor("req-list").append(isRequirement(block) ? requirementCard(block) : plainRow(block));
    } else if (block.kind === "item") {
      // A numbered step keeps its own number, also where the list skips one.
      const numbered = block.number !== null;
      groupFor(numbered ? "spec-list numbered" : "spec-list").append(h("li", { value: numbered ? block.number : null }, proseBlock(block)));
    } else {
      group = null;
      nodes.push(proseBlock(block));
    }
  }
  if (ui.filter !== null && !visible.some(isRequirement)) nodes.push(h("p", { class: "note", text: "Choose another status to see requirements" }));
  fill("doc", nodes);
}

// With a status filter: the requirements with that status under their section headings.
function filtered(blocks) {
  const shown = [];
  let heading = null;
  for (const block of blocks) {
    if (block.kind === "heading") heading = block;
    if (isRequirement(block) && cell(block, "Status") === ui.filter) {
      if (heading && !shown.includes(heading)) shown.push(heading);
      shown.push(block);
    }
  }
  return shown;
}

function renderEditAll() {
  const key = "edit:all";
  const submit = (force) => {
    const text = ui.drafts.get(key);
    const version = force ? ui.latestVersion : ui.allVersion;
    save(() => api("PUT", DOC.api, { version, text }), () => {
      ui.editing = null;
      ui.drafts.delete(key);
    });
  };
  fill("doc",
    h("div", { class: "spec-box edit-all" },
      textBox(key, ui.spec.text, 30, () => submit(false)),
      h("div", { class: "edit-foot" },
        ui.moved && h("button", { class: "button", type: "button", "data-save": true, onclick: () => submit(true) }, "Save over newer changes"),
        cancelButton(),
        h("button", {
          class: "button primary", type: "button", "data-key": "Ctrl+Enter", "data-save": true,
          title: DOC.saveWords, onclick: () => submit(false),
        }, "Save"))));
}

function render() {
  if (!ui.spec) return;
  renderSummary();
  renderBanner();
  if (ui.editing === "all") renderEditAll();
  else renderDoc();
  if (ui.busy) holdEditors();
  renderToc();
  watchSections();
  const button = document.getElementById("edit-all");
  button.disabled = ui.editing === "all";
}

document.title = `Cockpit ${DOC.title}`;
for (const link of document.querySelectorAll(".brand-menu a")) {
  if (link.getAttribute("href") === DOC.tab) link.setAttribute("aria-current", "page");
  else link.removeAttribute("aria-current");
}
// Ghosts stand in the page until its file arrives; a failed load takes them away, and the banner says why (UI-48).
const LOADING = Loading.ghost("tall w-50|gap|w-90|w-80|w-90|w-60|gap|gap|tall w-30|w-90|w-70|w-80|w-90|w-50", `Loading ${DOC.name}`, "ghost-doc");
fill("doc", LOADING);
document.getElementById("spec-more").hidden = Boolean(DOC.list || DOC.writing);
document.querySelector("main").classList.toggle("skill-index", Boolean(DOC.list || DOC.writing));

// The header's "more" menu holds the rare action: editing the whole file.
document.querySelector("#spec-more > summary").append(icon("more"));
document.querySelector("#edit-all .menu-label").prepend(icon("pencil"));

document.getElementById("edit-all").addEventListener("click", () => {
  if (!ui.spec) return;
  closeMenus();
  ui.editing = "all";
  ui.commenting = null;
  ui.allVersion = ui.spec.version;
  render();
  focusSoon("edit:all");
});

document.getElementById("doc").addEventListener("click", (event) => {
  if (event.target.closest("a, button, input, textarea, select, summary, .spec-box, .row-edit")) return;
  const el = event.target.closest("[data-block]");
  ui.selected = el ? el.dataset.block : null;
  for (const node of document.querySelectorAll("#doc .selected")) node.classList.remove("selected");
  if (el) el.classList.add("selected");
});

// A menu closes on a press outside it, when another opens, or on Escape. Escape with no menu
// open closes the open editor or comment box, as the Cancel button's keycap says.
document.addEventListener("click", (event) => {
  for (const menu of document.querySelectorAll("details.menu[open]")) {
    if (!menu.contains(event.target)) menu.open = false;
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || event.defaultPrevented) return;
  const open = document.querySelector("details.menu[open]");
  if (open) {
    open.open = false;
    open.querySelector("summary").focus();
  } else if (anyOpen()) {
    closeAll();
  }
});

window.addEventListener("scroll", tuckToc, { passive: true });

// The header is solid; once the page scrolls under it, a hairline sets it off.
const specHeader = document.querySelector(".top");
function markScrolled() {
  specHeader.classList.toggle("scrolled", window.scrollY > 0);
}
window.addEventListener("scroll", markScrolled, { passive: true });
markScrolled();

const resized = new ResizeObserver(() => { measureCover(); fadeTocEdges(); showSection(false); });
resized.observe(document.querySelector(".top"));
resized.observe(toc.nav);
narrow.addEventListener("change", () => {
  toc.nav.classList.remove("tucked");
  measureCover();
  showSection(true);
});

// The Briefing light, as on the Cockpit page. This page reads the state when it opens and when it comes back
// into view, not with every poll: a read of the state counts as the user looking at the board, which keeps the
// automatic checks going (TECH section 7, Checks).
async function loadBriefingLight() {
  try {
    const state = await api("GET", "/api/state");
    // The Briefing tab's light: a morning briefing the user has not opened yet (SPEC VIEW-8). A phone
    // shows it on the brand key from the body's attribute (style.css).
    const unread = Boolean(state.briefing && state.briefing.unread);
    document.body.toggleAttribute("data-briefing-unread", unread);
    for (const link of document.querySelectorAll('a[href="/briefing"]')) link.classList.toggle("unread", unread);
  } catch (error) {
    logError("reading whether a briefing is unread", error);
  }
}

load();
loadBriefingLight();
window.PageReload?.hold(writingToKeep);
setInterval(() => { if (!document.hidden) load(); }, POLL_MS);
// A connection that comes back shows the latest spec at once, rather than at the next poll (CODE-5).
addEventListener("online", () => load());
// Actions a closed or reloaded page left behind went from this page (errors.js, CODE-5).
addEventListener("cockpit:sent-left", () => load());
document.addEventListener("visibilitychange", () => { if (!document.hidden) loadBriefingLight(); });
