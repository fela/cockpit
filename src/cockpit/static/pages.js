"use strict";

// The Pages tab (SPEC API-4): every HTML page sessions shared on an item, open or closed, last changed
// first and grouped by the day of that change. The box finds a page by its title or its item's title.
// On a wide screen a click shows the page across the whole width, sandboxed as a step's figure is (its own
// origin, no access to Cockpit), with All pages to go back to the list; a phone, or a click with a modifier
// key, opens the page itself. `?id=N` names the page shown, so a link can point at one.
//
// Comments (SPEC NEXT-38): reading is the main use, so a page shows without them until the user presses Comment.
// Then a comment column (page-comments.js) shows beside the page, and the reply box under it. A step's link to a
// page opens `?id=N&step=M`: the step's ask on top and the page; with Comment on, the reply box's Send takes the
// comments with the answer. Without a step, or once the step is with its session, the comments go only with Add
// as new item. Commenting starts on by itself for `&comment=1` (the step's "Comment on this page" link) and when
// the step already has comments on the page.
//
// Overlay (SPEC NEXT-47): `&overlay=1` is this view inside the layer that opens a page over Cockpit's main page
// (page-overlay.js). It shows the page alone, with Comment, Share and Open in a new tab floating over its corner; the
// layer draws the X, and Escape here asks it to close. Choices the user makes on the page (SPEC NEXT-48,
// page-choices.js) go with the step's answer, as comments do.
//
// Sharing (SPEC API-5, API-7, API-8): a page starts shared with nobody. Its Share menu, on the heading of a page being
// read here or in the layer, chooses who can open it at its link: only the user, specific people who sign in with
// Google, or anyone with the link. It sends the version on the screen, so nobody shares a page they haven't seen. A
// phone's list rows open the page for that, and on a shared page carry its link. Only me keeps the link, so sharing again brings back links
// already sent; New link ends the old one for good.

const POLL_MS = 60e3;
// From this width a row shows its page here in place of the list; below it a row opens the page itself.
const WIDE = matchMedia("(min-width: 901px)");
const icon = (name) => Icons.icon(name);
let pages = [];
let shown = Number(new URLSearchParams(location.search).get("id")) || null;
// The step the page was opened from, for good: a step link opens its own tab.
const stepId = Number(new URLSearchParams(location.search).get("step")) || null;
let drawnPrint = "";
// Cockpit's state, for the step, its unsent page comments on other pages, and the checkouts Add starts in.
let state = null;
// The comment column on the shown frame, or null while the user is not commenting.
let column = null;
// Whether the user is commenting on the shown page: they pressed Comment, or it started on by itself.
let commenting = false;
// The page `&comment=1` asks to comment on, until the user leaves it.
let commentOn = new URLSearchParams(location.search).get("comment") === "1" ? shown : null;
// The page the frame shows, {id, revision}: a new frame for the same page (a new version) keeps commenting on.
let framedPage = null;
// Whether this install has an address for shared pages, and whether the list shows only those pages.
let publicOn = false;
let publicOnly = false;
// The Share menu over a redraw: the page whose menu is open, whether its people show, and the address being typed.
const share = { open: null, people: false, typed: "" };
// The comments the user was writing when the column went, by page id: {revision, writing}.
const unfinished = new Map();
// The answer just sent from here, while Take back can still stop it: {message, timer}.
let sent = null;
let busy = false;
let replyError = "";
// A comment Cockpit refused because the page changed, for the next version's column to show.
let carried = "";
// Unsent comments brought back over Reload (UI-13). Only these open the Pages tab's column by themselves.
const fromReload = window.PageReload?.take();
const restored = new Set();
for (const [id, kept] of fromReload?.unfinished ?? []) {
  unfinished.set(id, kept);
  restored.add(id);
}
carried = fromReload?.carried || "";
window.PageReload?.hold(() => {
  keepWriting();
  const kept = [...unfinished];
  return kept.length || carried ? { unfinished: kept, carried } : null;
});
if (stepId) document.body.classList.add("pages-step-mode");
// In the layer over Cockpit's main page, for good.
const overlay = new URLSearchParams(location.search).get("overlay") === "1";
if (overlay) document.body.classList.add("pages-overlay-mode");
const closeOverlay = () => parent.postMessage({ cockpitOverlay: "close" }, location.origin);
// The task a link names on Cockpit's page: `/#home/item/N` (keyboard.js), or a mention's `/#item/N`; else null.
// briefing.js has the same.
function taskLinkId(link) {
  let url;
  try {
    url = new URL(link.href, location.href);
  } catch {
    return null;
  }
  if (url.origin !== location.origin || !["/", "/classic"].includes(url.pathname)) return null;
  const match = url.hash.match(/^#(?:(?:home|board)\/)?item\/(\d+)$/);
  return match ? Number(match[1]) : null;
}
// Loading ahead of a click, hidden (page-overlay.js): the view draws its page and waits to be shown before it reads
// Cockpit's state, which is slow, or the comment the last layer kept, which belongs to the next layer shown.
let held = false;
try {
  held = overlay && Boolean(frameElement?.closest(".page-overlay-preload"));
} catch {
  // A frame Cockpit's page cannot reach is never one it loads ahead.
}
// While held, what would write for the user waits here: a choice the page's own script makes as it starts
// (page-choices.js) runs once the page is shown, and the controls it shows count as seen only then (uses.js).
window.cockpitHold = held ? [] : null;
// Beside the step: the layer shows this view next to the step's card on Cockpit's page, whose reply box lists the
// comments and sends them with the answer. The view leaves out its own reply box, the step's ask and the comment
// list: commenting is always on, Pick a part takes Comment's place in the head, and the words the user selects or the
// part they pick go into the card's reply box as a quote, with the cursor under it for the comment
const beside = overlay && new URLSearchParams(location.search).get("beside") === "1";
if (beside) document.body.classList.add("pages-beside-mode");
let toldComments = "";
function tellCard(comments) {
  if (!beside) return;
  const print = JSON.stringify(comments.map((comment) => [comment.id, comment.text]));
  if (print === toldComments) return;
  // The first list is what the card already shows; only a change after it is news.
  const first = toldComments === "";
  toldComments = print;
  if (!first) parent.postMessage({ cockpitOverlay: "changed" }, location.origin);
}
// In the layer, a comment being written outlives the layer, which goes with its frame on the X, Escape or Back:
// the layer asks this view to keep it as it closes, Cockpit's page holds it (`cockpitUnfinished`, page-overlay.js),
// and the next layer on that page opens with the comments shown and the comment back in its box.
if (overlay) {
  const kept = parent.cockpitUnfinished;
  // A view loading ahead leaves a kept comment where it is; Cockpit's page shows a fresh layer while one waits.
  if (!held) {
    for (const [id, left] of kept.get()) unfinished.set(id, left);
    kept.set([]);
  }
  window.cockpitPagesLeaving = () => {
    keepWriting();
    kept.set([...unfinished]);
  };
  // This view replaces another page's view beside the step, which kept its comments again as it went: what the
  // user wrote there while this one loaded wins over what this view read at its start, except for its own page.
  window.cockpitPagesTake = () => {
    const left = new Map(kept.get());
    kept.set([]);
    const own = framedPage?.id;
    for (const id of [...unfinished.keys()]) if (id !== own) unfinished.delete(id);
    for (const [id, comment] of left) if (id !== own) unfinished.set(id, comment);
  };
}
// The layer stays hidden, with Cockpit's page showing, until this view says its page has drawn (or what stops it).
let readySent = false;
function overlayReady() {
  if (!overlay || readySent) return;
  readySent = true;
  parent.postMessage({ cockpitOverlay: "ready" }, location.origin);
}
// The last choice the user made on the shown page, as a short note by its buttons: {text, failed}, or null.
let choiceNote = null;
let choiceTimer = 0;

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [name, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") el.className = value;
    else if (name === "text") el.textContent = value;
    else if (name.startsWith("on")) el.addEventListener(name.slice(2), value);
    else el.setAttribute(name, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

async function api(path) {
  // A read waits through a restart of Cockpit (CODE-5).
  return replyData(await readThroughRestart(path, { credentials: "same-origin" }));
}

// A write: tried again through a restart while it is fresh, and done once (the action key, CODE-5).
async function write(path, body, words) {
  return replyData(await sendAction(path, {
    method: "POST", credentials: "same-origin", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  }, TRY_MS, words));
}

// The same for a removal, which has no body.
async function remove(path) {
  return replyData(await sendAction(path, { method: "DELETE", credentials: "same-origin" }, TRY_MS, null));
}

// "Today", "Yesterday", or "Thursday 15 October", in this browser's time.
function dayWords(iso) {
  const day = new Date(iso);
  const today = new Date();
  const start = (date) => new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const daysAgo = Math.round((start(today) - start(day)) / 864e5);
  if (daysAgo === 0) return "Today";
  if (daysAgo === 1) return "Yesterday";
  return day.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
}

function clock(iso) {
  return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
}

// "today at 14:37", "yesterday at 14:37" or "on Thursday 15 October at 14:37".
function when(iso) {
  const day = dayWords(iso);
  const words = day === "Today" || day === "Yesterday" ? day.toLowerCase() : `on ${day}`;
  return `${words} at ${clock(iso)}`;
}

// Who shared the page: a session by its name, the user as "you".
function sharedBy(addedBy) {
  if (addedBy === "user") return "you";
  if (addedBy === "cockpit") return "Cockpit";
  return addedBy.replace(/^agent:/, "");
}

const closed = (page) => page.item_archived || page.item_stage === "done";
const itemHref = (page) => `/#home/item/${page.item_id}`;

function matching(term) {
  const words = term.toLowerCase().split(/\s+/).filter(Boolean);
  return pages.filter((page) => {
    const text = `${page.title} ${page.item_title}`.toLowerCase();
    return words.every((word) => text.includes(word));
  });
}

function renderCount() {
  const items = new Set(pages.map((page) => page.item_id)).size;
  const count = document.getElementById("pages-count");
  const words = pages.length
    ? `${pages.length} ${pages.length === 1 ? "page" : "pages"} on ${items} ${items === 1 ? "item" : "items"}`
    : "Ask a session to share a page";
  const refocus = count.contains(document.activeElement);
  const shared = pages.filter((page) => page.public).length;
  if (!shared) publicOnly = false;
  // The way to see everything anyone can open right now.
  const filter = shared > 0 && h("button", {
    class: `button small pages-public-filter${publicOnly ? " on" : ""}`, type: "button", "aria-pressed": String(publicOnly),
    onclick: () => {
      publicOnly = !publicOnly;
      renderCount();
      renderList();
    },
  }, `${shared} shared`);
  count.replaceChildren(words, filter || "");
  if (refocus && filter) filter.focus();
}

// A page's title in a question to the user. A session writes the title, so it comes after the question's own words, on
// one short line, and can't push them out of view.
function titleInQuestion(page) {
  const line = page.title.replace(/[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069]+/g, " ").trim();
  return line.length > 80 ? `${line.slice(0, 80)}…` : line;
}

// Hands over a shared page's link: the phone's share sheet where there is one, otherwise the clipboard. A browser
// opens the share sheet only within seconds of the user's press, so a link handed over after a confirmation and a
// save is refused with NotAllowedError and goes to the clipboard. A browser can refuse the clipboard after the wait
// for Share, so the last resort shows the link to copy by hand.
async function handOver(page) {
  if (!WIDE.matches && navigator.share) {
    try {
      await navigator.share({ title: page.title, url: page.public_url });
      return;
    } catch (error) {
      if (error.name === "AbortError") return;
      if (error.name !== "NotAllowedError") logError("sharing a page's link", error);
    }
  }
  try {
    await navigator.clipboard.writeText(page.public_url);
    notice(`Copied the link to “${page.title}”.`);
  } catch (error) {
    logError("copying a page's link", error);
    window.prompt("Copy this link:", page.public_url);
  }
}

// The same shape of address Cockpit takes (store.PAGE_PERSON), so a typo is caught before it is sent.
const PERSON = /^[^@\s,;<>"']{1,64}@[a-z0-9-]{1,63}(\.[a-z0-9-]{1,63}){1,8}$/;
const peopleWords = (count) => `${count} ${count === 1 ? "person" : "people"}`;

// Who besides the user can open the page, in a word for its Share key and its list row; null while nobody can.
function sharedWords(page) {
  if (!page.public) return null;
  return page.access === "anyone" ? "Public" : peopleWords(page.people?.length ?? 0);
}

async function put(path, body) {
  return replyData(await sendAction(path, {
    method: "PUT", credentials: "same-origin", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  }, TRY_MS, null));
}

// Sharing a page nobody else could open, or opening one to anyone, asks first: the page's code runs for whoever opens
// it. Choosing who among people it is already shared with, or narrowing it, does not.
function confirmShare(page, access) {
  if (page.public && !(access === "anyone" && page.access !== "anyone")) return true;
  const before = page.link_kept
    ? "This page was shared before and keeps the link you sent then. If that link reached someone it shouldn't have, choose New link once it's shared.\n\n"
    : "";
  const who = access === "anyone"
    ? "Anyone with the link can open this page without signing in."
    : "The people you add can open this page after they sign in with Google.";
  return window.confirm(`${before}${who} It isn't listed anywhere. The page's own code runs for them, and it can act differently there than here, so share only pages whose code you trust. Scripts and fonts from other websites won't load for them.\n\nShare “${titleInQuestion(page)}”?`);
}

// Saves who can open the page, at the version on the screen, and redraws its Share menu with the answer. Without
// people, the page keeps its list.
async function setSharing(page, access, people) {
  if (framedPage?.id !== page.id) return null;
  try {
    const saved = await put(`/api/item-pages/${page.id}/sharing`, { access, revision: framedPage.revision, ...(people ? { people } : {}) });
    Object.assign(page, saved);
    redrawHead(page);
    if (!overlay) {
      renderCount();
      renderList();
    }
    load().catch((error) => logError("loading the pages", error));
    return saved;
  } catch (error) {
    logError("sharing a page", error);
    notice(`Who can open “${page.title}” didn't change: ${error.message}`);
    return null;
  }
}

async function chooseAccess(page, access) {
  const wasShared = page.public;
  if (access === "people") {
    share.people = true;
    // With nobody on the list yet, Specific people only opens it; the first address added shares the page.
    if (!page.people?.length) {
      redrawHead(page);
      document.querySelector('#pages-preview [data-head="share-input"]')?.focus();
      return;
    }
  } else share.people = false;
  if (access === (page.public ? page.access : "me")) {
    redrawHead(page);
    return;
  }
  if (access !== "me" && !confirmShare(page, access)) return;
  const saved = await setSharing(page, access, null);
  if (!saved) return;
  if (access === "me") notice(`Only you can open “${page.title}” now. Its link stops working until you share it again.`);
  else if (!wasShared) await handOver(saved);
}

async function addPerson(page, input) {
  const email = input.value.trim().toLowerCase();
  if (!PERSON.test(email)) {
    input.setCustomValidity("Type an email address, like name@gmail.com");
    input.reportValidity();
    return;
  }
  const wasShared = page.public;
  if (page.access !== "people" && !confirmShare(page, "people")) return;
  share.typed = "";
  const saved = await setSharing(page, "people", [...(page.people ?? []), email]);
  document.querySelector('#pages-preview [data-head="share-input"]')?.focus();
  if (saved && !wasShared) await handOver(saved);
}

// Taking the last person off a page shared only with people shares it with nobody.
async function removePerson(page, email) {
  const rest = (page.people ?? []).filter((person) => person !== email);
  const access = page.access === "people" && rest.length === 0 ? "me" : page.access;
  const saved = await setSharing(page, access, rest);
  if (saved && access === "me" && page.access === "me") notice(`Only you can open “${page.title}” now.`);
  document.querySelector('#pages-preview [data-head="share-input"]')?.focus();
}

// Replaces a shared page's link, for one that reached the wrong person: the old link stops working for good.
async function newLink(page) {
  if (!window.confirm(`The current link stops working for good, for everyone who has it. The page stays shared at a new link, which you can then send again.\n\nNew link for: “${titleInQuestion(page)}”?`)) return;
  try {
    const renewed = await write(`/api/item-pages/${page.id}/public/new-link`, {}, null);
    load().catch((error) => logError("loading the pages", error));
    await handOver(renewed);
  } catch (error) {
    logError("giving a page a new link", error);
    notice(`“${page.title}” keeps its old link: ${error.message}`);
  }
}

// The Share menu on a page's heading (SPEC API-8): who can open it, the people it is shared with, and its link. It
// looks and moves as a step's ⋯ menu does, and stays open over a redraw.
function shareMenu(page) {
  if (!publicOn && !page.public) return null;
  const el = h("details", { class: "menu step-menu share-menu" });
  el.open = share.open === page.id;
  el.addEventListener("toggle", () => {
    if (el.open) share.open = page.id;
    else if (share.open === page.id && el.isConnected) {
      share.open = null;
      share.people = false;
    }
  });
  // Escape closes the menu, and only the menu: in the layer it would otherwise close the layer. Up and Down move
  // through the menu's entries, Home and End go to its ends; the address box keeps its own keys.
  el.addEventListener("keydown", (event) => {
    if (el.open && event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }
    if (!el.open || event.target.tagName === "INPUT" || !["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    const entries = [...el.querySelectorAll("[role^=menuitem]")];
    if (entries.length === 0) return;
    event.preventDefault();
    event.stopPropagation();
    const at = entries.indexOf(document.activeElement);
    const next = event.key === "Home" ? 0 : event.key === "End" ? entries.length - 1
      : event.key === "ArrowDown" ? (at + 1) % entries.length : (at <= 0 ? entries.length : at) - 1;
    entries[next].focus();
  });
  const close = () => {
    el.open = false;
    el.querySelector(":scope > summary")?.focus({ preventScroll: true });
  };
  const current = page.public ? page.access : "me";
  const radio = (access, iconName, words) => {
    const on = current === access;
    return h("button", {
      type: "button", role: "menuitemradio", "aria-checked": String(on), "data-head": `share-${access}`,
      onclick: () => chooseAccess(page, access),
    }, h("span", { class: "menu-label" }, icon(iconName), words), on && h("span", { class: "when" }, icon("check")));
  };
  const people = page.people ?? [];
  const listed = current === "people" || share.people;
  const input = h("input", {
    class: "field", type: "email", "aria-label": "Add a Gmail address", placeholder: "name@gmail.com",
    autocomplete: "off", spellcheck: "false", "data-head": "share-input",
    oninput: (event) => { share.typed = event.target.value; event.target.setCustomValidity(""); },
  });
  input.value = share.typed;
  const panel = listed && h("div", { class: "share-people", role: "group", "aria-label": "People it is shared with" },
    people.map((email) => h("div", { class: "share-person" }, icon("person"), h("span", { class: "share-email", text: email, title: email }),
      h("button", { type: "button", class: "share-remove", "aria-label": `Remove ${email}`, title: "Remove", onclick: () => removePerson(page, email) }, icon("x")))),
    h("form", { class: "share-add", novalidate: true, onsubmit: (event) => { event.preventDefault(); addPerson(page, input); } },
      input, h("button", { type: "submit", class: "button small", "data-head": "share-add" }, "Add")),
    h("p", { class: "menu-note", text: "They sign in with Google to open it." }));
  const links = page.public && page.public_url && [
    h("button", { type: "button", role: "menuitem", "data-head": "share-copy", onclick: () => { close(); handOver(page); } },
      h("span", { class: "menu-label" }, icon("copy"), !WIDE.matches && navigator.share ? "Share link…" : "Copy link")),
    h("button", { type: "button", role: "menuitem", "data-head": "share-new-link", onclick: () => { close(); newLink(page); } },
      h("span", { class: "menu-label" }, icon("retry"), "New link…")),
  ];
  const words = sharedWords(page);
  el.append(
    h("summary", { class: `button small${page.public ? " on" : ""}`, "data-head": "share",
      "aria-label": words ? `Shared: ${page.access === "anyone" ? "anyone with the link" : words}` : "Share" },
    icon(page.public && page.access === "anyone" ? "link" : "people"), words || "Share"),
    h("div", { class: "menu-list" },
      h("div", { class: "menu-group", role: "menu", "aria-label": "Who can open it" },
        radio("me", "person", "Only me"),
        radio("people", "people", "Specific people"),
        radio("anyone", "link", "Anyone with the link")),
      panel,
      links && h("div", { class: "menu-group", role: "menu", "aria-label": "The link" }, links)));
  return el;
}

// A press outside the Share menu closes it, as a press outside any menu does.
addEventListener("pointerdown", (event) => {
  const open = document.querySelector(".share-menu[open]");
  if (open && !open.contains(event.target)) open.open = false;
}, true);

// A phone's list row: a shared page's link, or the way to the page's heading, where its Share menu is.
function rowShare(page) {
  const button = (name, words, onclick) => h("button", {
    class: "button small icon-only", type: "button", "aria-label": words, title: words, onclick,
  }, icon(name));
  if (page.public && page.public_url) return button("link", navigator.share ? "Share link" : "Copy link", () => handOver(page));
  return publicOn ? button("people", "Open to share", () => show(page.id, true)) : null;
}

function row(page) {
  const link = h("a", { class: "pages-row", href: page.url, target: "_blank", rel: "noopener", "data-page": page.id,
    "aria-current": page.id === shown ? "true" : null },
    icon("doc"),
    h("span", { class: "pages-row-text" },
      h("span", { class: "pages-row-title", text: page.title }),
      h("span", { class: "pages-row-item" },
        Icons.cockpitTag(page.item_project),
        h("span", { text: page.item_title }),
        closed(page) && h("span", { class: "pages-closed", text: "closed" }),
        page.public && h("span", { class: "pages-public", text: sharedWords(page).toLowerCase() }))),
    h("time", { class: "pages-row-time", datetime: page.updated_at, text: clock(page.updated_at) }));
  link.addEventListener("click", (event) => {
    if (!WIDE.matches || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    show(page.id, true);
  });
  return link;
}

function renderList() {
  const term = document.getElementById("pages-find").value.trim();
  const found = matching(term).filter((page) => !publicOnly || page.public);
  const list = document.getElementById("pages-list");
  Loading.settle(list);
  // A redraw keeps the keyboard on the row it was on.
  const focused = list.contains(document.activeElement) ? document.activeElement.dataset.page : null;
  if (!pages.length) {
    list.replaceChildren(h("p", { class: "pages-empty" },
      "Sessions share reports, plans and charts on an item with ", h("code", { text: "cockpit page add" }),
      "; each one shows here."));
    return;
  }
  if (!found.length) {
    list.replaceChildren(h("p", { class: "pages-empty", text: `No ${publicOnly ? "shared " : ""}page or item title holds “${term}”.` }));
    return;
  }
  const days = new Map();
  for (const page of found) {
    const day = dayWords(page.updated_at);
    if (!days.has(day)) days.set(day, []);
    days.get(day).push(page);
  }
  list.replaceChildren(...[...days].map(([day, entries]) => h("section", { class: "pages-day" },
    h("h2", { text: day }),
    h("ul", null, entries.map((page) => h("li", null, row(page), h("span", { class: "pages-row-share" }, rowShare(page))))))));
  if (focused) list.querySelector(`[data-page="${focused}"]`)?.focus({ preventScroll: true });
}

// One line over the page: back to the list, what the page is, and its buttons.
function previewHead(page) {
  const picking = Boolean(column && column.picking());
  const comment = beside
    ? h("button", {
      class: `button small${picking ? " on" : ""}`, type: "button", "aria-pressed": String(picking), "data-head": "pick",
      "aria-label": "Pick a part to comment on; or select words", disabled: !column, onclick: () => column?.togglePick(),
    }, icon("pick"), picking ? "Click a part…" : "Pick a part")
    : h("button", {
      class: `button small${commenting ? " on" : ""}`, type: "button", "aria-pressed": String(commenting), "data-head": "comment", onclick: toggleCommenting,
    }, icon("chat"), commenting ? "Hide comments" : "Comment");
  const note = choiceNote && h("span", { class: `pages-choice-note${choiceNote.failed ? " failed" : ""}`, role: "status", text: choiceNote.text });
  if (overlay) {
    return h("header", { class: "pages-preview-head pages-overlay-head" },
      h("div", { class: "pages-preview-buttons" }, note, comment, shareMenu(page),
        h("a", { class: "button small ghost", href: page.url, target: "_blank", rel: "noopener", "data-head": "open", title: "Open in a new tab", "aria-label": "Open in a new tab" }, icon("arrow"))));
  }
  const version = page.revision > 1 ? `version ${page.revision}, ` : "";
  const changed = page.updated_at === page.created_at ? "shared" : "changed";
  // All work opens only items still on it; an archived item, or one done long enough ago to have left it, is named
  // without a link.
  const item = page.item_on_board ? h("a", { href: itemHref(page), text: page.item_title }) : h("span", { text: page.item_title });
  const back = !stepId && h("a", {
    class: "button small ghost pages-back", href: "/pages", "data-head": "back",
    onclick: (event) => {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      history.pushState(null, "", "/pages");
      show(null, false);
    },
  }, icon("arrow"), "All pages");
  return h("header", { class: "pages-preview-head" },
    back,
    h("div", { class: "pages-preview-words" },
      h("h2", { text: page.title }),
      h("p", { class: "note" },
        Icons.cockpitTag(page.item_project), "On ", item, closed(page) ? " (closed)" : "",
        `; ${version}${changed} by ${sharedBy(page.added_by)} ${when(page.updated_at)}.`)),
    h("div", { class: "pages-preview-buttons" }, note, comment, shareMenu(page),
      h("a", { class: "button small", href: page.url, target: "_blank", rel: "noopener", "data-head": "open" }, icon("arrow"), "Open the page")));
}

// The new heading keeps the keyboard on its button (Comment, Pick a part, a poll's redraw).
function redrawHead(page) {
  const head = document.querySelector("#pages-preview .pages-preview-head");
  if (!head) return;
  const focused = head.contains(document.activeElement) ? document.activeElement.dataset.head : null;
  head.replaceWith(previewHead(page));
  if (focused) document.querySelector(`#pages-preview .pages-preview-head [data-head="${focused}"]`)?.focus();
}

function toggleCommenting() {
  commenting = !commenting;
  renderPreview();
}

// Whether the step already has the user's comments on this page, which open the column by themselves.
function stepCommentsHere(page) {
  return Boolean(stepId && state && state.page_comments.some((entry) => entry.step_id === stepId && entry.page_id === page.id));
}

// The step the page was opened from, as Cockpit's state has it, or null when it closed.
function theStep() {
  if (!stepId || !state) return null;
  return [...state.queue, ...state.ready_next, ...state.snoozed, ...state.with_agents].find((step) => step.id === stepId) || null;
}

// Whether the user's answer to the step can take comments now: open, and not with its session.
const stepTakes = (step) => Boolean(step && !step.handoff && !step.left);

// The step whose answer comments and choices go with now, or null. Before Cockpit's state arrives, the step the
// address names, which almost always still takes them.
const takesNow = (step) => (state ? (stepTakes(step) ? step.id : null) : stepId);

// Where comments go when the step takes none is said once, under the page, by renderReply.
function stepHead(step) {
  if (!step) {
    return h("div", { class: "pages-step" },
      h("p", { class: "pages-step-item", text: state ? "This step is closed" : "Loading the step…" }));
  }
  return h("div", { class: "pages-step" },
    h("p", { class: "pages-step-item" }, Icons.cockpitTag(step.item.project), h("a", { href: `/#home/item/${step.item_id}`, text: step.item.title })),
    h("p", { class: "pages-step-ask", text: step.ask }),
    commenting && !stepTakes(step) && !sent && h("p", { class: "note", text: "This step is with its session now, so comments here no longer go with your answer." }));
}

function renderPreview() {
  const pane = document.getElementById("pages-preview");
  const page = pages.find((entry) => entry.id === shown);
  // A shown page takes the whole width; without one the list does. The layer has no list: it shows that its page
  // is gone.
  const loaded = drawnPrint !== "";
  document.body.classList.toggle("pages-reading", Boolean(page) || (overlay && loaded));
  if (!page) {
    keepWriting();
    detachColumn();
    framedPage = null;
    // Until the pages have come there is nothing to say yet; then a page asked for by a step or the layer is gone.
    pane.replaceChildren(...((stepId || overlay) && loaded ? [h("p", { class: "pages-empty", text: "This page no longer exists." })] : []));
    if (loaded) overlayReady();
    return;
  }
  // A kept comment opens the column in the layer, which brings it back as it opens, and after Reload; otherwise
  // in the Pages tab the page shows without comments, and the comment waits in its box for Comment.
  if (page.id !== framedPage?.id) commenting = beside || page.id === commentOn || stepCommentsHere(page) || ((overlay || restored.delete(page.id)) && unfinished.has(page.id));
  // The frame stays in place while the page is the same version, since moving a frame reloads it and
  // would lose the reader's place; a new version or another page gets a new frame. It always carries the
  // comment script, so pressing Comment does not reload it either.
  const src = `/pages/${page.id}/figure?comments=1&v=${page.revision}`;
  let frame = pane.querySelector("iframe");
  const step = theStep();
  if (frame && frame.dataset.src === src) {
    redrawHead(page);
    pane.querySelector(".pages-step")?.replaceWith(stepHead(step));
  } else {
    // A comment being written on this page goes on to the new version's column, which shows it to place again;
    // one on another page waits for that page.
    if (page.id === framedPage?.id) {
      if (column && column.draftText()) carried = column.draftText();
    } else keepWriting();
    detachColumn();
    frame = h("iframe", {
      class: "pages-frame", src, "data-src": src, "aria-label": page.title,
      sandbox: "allow-scripts allow-popups allow-popups-to-escape-sandbox allow-downloads", allow: "fullscreen",
    });
    Loading.frame(frame);
    frame.addEventListener("load", overlayReady, { once: true });
    pane.replaceChildren(...[
      previewHead(page),
      h("div", { class: "pages-reader" }, frame),
      h("div", { class: "pages-reply", id: "pages-reply" }),
    ].filter(Boolean));
  }
  framedPage = { id: page.id, revision: page.revision };
  // Comments and choices on the page go with the answer to the step while it takes them (page-choices.js). When the
  // address's step takes none after all, Cockpit refuses a comment, and saves a choice without the step.
  const takes = takesNow(step);
  frame.dataset.step = takes ? String(takes) : "";
  placeColumn(frame, page, takes);
}

// The sidebar beside the frame while the user is commenting: the step's ask, when a step opened the page, over
// the comment column, for where comments go now: with the step's answer while the step takes them, else with Add
// as new item. While the user reads, neither takes room from the page.
function placeColumn(frame, page, takes) {
  const reader = frame.parentElement;
  reader.classList.toggle("commenting", commenting && !beside);
  if (!commenting) {
    keepWriting();
    detachColumn();
    reader.querySelector(".pages-side")?.remove();
    renderReply();
    return;
  }
  // Where comments go depends on the step, so the column waits for Cockpit's state to say whether it takes answers.
  if (stepId && !state) {
    detachColumn();
    reader.querySelector(".pages-side")?.remove();
    reader.querySelector(".pc-column")?.remove();
    if (!beside) reader.append(h("div", { class: "pages-side" }, stepHead(null)));
    return;
  }
  if (column && column.stepId === takes && reader.querySelector(".pc-column")) {
    column.refresh();
    renderReply();
    return;
  }
  if (column && column.draftText()) carried = column.draftText();
  detachColumn();
  const aside = h("aside", { class: "pc-column", "aria-label": beside ? "Comments on the page not loaded" : "Comments on the page", hidden: beside });
  reader.querySelector(".pages-side")?.remove();
  reader.querySelector(".pc-column")?.remove();
  reader.append(beside ? aside : h("div", { class: "pages-side" }, stepId && stepHead(theStep()), aside));
  // A comment this page's column held when it went comes back: placed as it was on the same version, else to
  // place again. Beside a step the reply box takes comments, so one left from the page on the whole window waits
  // for the page shown that way again.
  const left = beside ? null : unfinished.get(page.id);
  if (!beside) unfinished.delete(page.id);
  const kept = left && left.revision === page.revision && left.writing.anchor ? left.writing : null;
  if (left && !kept && !carried) carried = left.writing.text;
  column = PageComments.attach({
    frame, page: { id: page.id, revision: page.revision }, stepId: takes, column: aside, onPlace: beside ? (anchor) => quoteOnCard(page, anchor) : null,
    onChange: () => { renderReply(); tellCard(column ? column.mine() : []); }, onStale: (text) => { carried = text; refresh(); }, carried, kept,
    onUnsaved: (writing) => unfinished.set(page.id, { revision: page.revision, writing, unsaved: true }),
    onPick: () => { if (beside) redrawHead(page); },
  });
  carried = "";
  if (beside) redrawHead(page);
  renderReply();
}

// What the user selected or picked on a page beside its step, as a quote for the card's reply box: the page's name
// and number, as the session reads page comments, then the words.
function quoteOnCard(page, anchor) {
  const words = anchor.kind === "text" ? anchor.quote : anchor.text || "(a chart or image with no text)";
  const on = `the page “${page.title}” (page ${page.id})`;
  const head = anchor.kind === "text" ? `On ${on}:` : `On a part of ${on}:`;
  parent.postMessage({ cockpitOverlay: "quote", text: `${head}\n${words}` }, location.origin);
}

// A comment being written when the column goes (Hide comments, All pages, another page) waits for the comments
// to show again on that page.
// Beside a step no comment is written here, so what an earlier keep held stays for the page on the whole window.
// A column with no comment being written drops what an earlier keep from it held for its page, which the user has
// since saved or cancelled; a comment whose save failed after its column went (`unsaved`) stays.
function keepWriting() {
  if (!column || !framedPage || beside) return;
  const writing = column.writing();
  if (writing) unfinished.set(framedPage.id, { revision: framedPage.revision, writing });
  else if (!unfinished.get(framedPage.id)?.unsaved) unfinished.delete(framedPage.id);
}

function detachColumn() {
  if (column) column.detach();
  column = null;
}

// The words in the reply box: the answer to the step, or, when no step takes them, the message to the item's session
// that goes with the comments on the page shown; each has its own draft, so an answer never goes to another session.
// The page's own copy is the draft; localStorage carries it across a reload, and to a tab that opens the box later,
// where the browser allows it. A draft read once stays the page's: another tab writing the same box leaves the words
// here as they are.
const replies = new Map();

function replyKey(toStep) {
  return toStep ? `cockpit.pages.reply:${stepId}` : `cockpit.pages.reply:page-${shown}`;
}

function savedReply(key) {
  if (!replies.has(key)) {
    let text = "";
    try {
      text = localStorage.getItem(key) || "";
    } catch {
      // Storage blocked: the box starts empty.
    }
    replies.set(key, text);
  }
  return replies.get(key);
}

function keepReply(key, text) {
  replies.set(key, text);
  try {
    if (text) localStorage.setItem(key, text); else localStorage.removeItem(key);
  } catch {
    // A private window or a full storage keeps no draft across a reload; the box still works.
  }
}

// A return replayed after a reload can finish after this page read its draft. Put its unsent words into the
// page's copy too, ahead of any newer writing, so the next redraw keeps them (errors.js, CODE-5).
addEventListener("cockpit:left-answered", (event) => {
  const { words, back } = event.detail;
  if (words.key !== null || !words.store.startsWith("cockpit.pages.reply:")) return;
  event.preventDefault();
  keepReply(words.store, [back, savedReply(words.store)].filter(Boolean).join("\n\n"));
  if (words.store === replyKey(true)) commenting = true;
  reload();
});

// Under the page: the step's reply box, whose Send takes the user's words and their page comments on the step,
// and Add as new item, which makes a to-do of the comments on this page. Without a step that takes them, Send takes
// the words and the comments on this page to the session working on the page's item, when the item has one.
function renderReply() {
  const box = document.getElementById("pages-reply");
  if (!box) return;
  if (sent) {
    box.replaceChildren(h("div", { class: "pages-reply-sent", role: "status" },
      h("p", null, h("b", { text: "Sent to the step's session." }), " It goes once this notice is gone; until then Take back stops it."),
      h("button", { class: "button small", type: "button", disabled: busy, onclick: takeBack }, icon("back"), "Take back")));
    return;
  }
  // Without the comment column there is nothing to send from here: the page has the screen. Beside the step, the
  // step's own reply box sends.
  if (!column || beside) {
    box.replaceChildren(...(replyError ? [h("p", { class: "pc-error", role: "alert", text: replyError })] : []));
    return;
  }
  const step = theStep();
  const takes = stepTakes(step);
  const page = pages.find((entry) => entry.id === shown);
  const toItem = !takes && Boolean(page && page.item_has_session);
  const here = column.mine();
  // This step's unsent comments on its item's other pages go with the answer too, and its choices on its pages.
  const elsewhere = takes && state ? state.page_comments.filter((entry) => entry.step_id === step.id && entry.page_id !== shown) : [];
  const choices = takes && state ? state.page_choices.filter((entry) => entry.step_id === step.id).length : 0;
  const count = here.length + elsewhere.length;
  // A redraw (a comment refresh) puts the box the user types in back with its caret, selection and scroll.
  const typing = document.activeElement && document.activeElement.id === "pages-reply-text" ? document.activeElement : null;
  // A box taken out of the page scrolls back to its top, so its scroll is read now.
  const scrolled = typing ? typing.scrollTop : 0;
  const pending = takes
    ? (count > 0 || choices > 0) && h("p", { class: "pages-pending" },
      h("b", { text: [count && `${count} comment${count === 1 ? "" : "s"}`, choices && `${choices} choice${choices === 1 ? "" : "s"}`].filter(Boolean).join(" and ") }),
      " with this answer", elsewhere.length ? `, including ${elsewhere.length} comment${elsewhere.length === 1 ? "" : "s"} on another page` : "")
    : h("p", { class: "pages-pending" + (here.length ? "" : " quiet"), text: here.length
      ? toItem
        ? `${here.length} comment${here.length === 1 ? "" : "s"} ready to send to this item's session`
        : `${here.length} unsent comment${here.length === 1 ? "" : "s"} on this page. Its item has no session, so Add as new item makes a to-do of ${here.length === 1 ? "it" : "them"}.`
      : toItem ? "Comments you leave here go to the session working on this page's item with Send, or to a new item with Add as new item."
        : "Comments you leave here go to a new item with Add as new item." });
  const sending = takes ? send : sendToItem;
  const key = replyKey(takes);
  // Without a step, Send needs comments to send: the words alone are a message, which the item's own view sends.
  const ready = (text) => !busy && (takes ? count > 0 || choices > 0 || Boolean(text.trim()) : here.length > 0);
  const area = (takes || toItem) && h("textarea", {
    class: "field", id: "pages-reply-text", rows: 2, disabled: busy,
    "aria-label": takes ? "Your answer to the step" : "Your message to the item's session",
    placeholder: `${takes ? "Your answer to the step (optional with page comments)" : "A message to go with the comments (optional)"}${page && (page.item_stage === "done" || page.item_archived) ? ". Sending reopens this task" : ""}`,
  });
  if (area) area.value = savedReply(key);
  const sendButton = (takes || toItem) && h("button", { class: "button primary", type: "button", disabled: !ready(savedReply(key)), onclick: sending },
    icon("send"), "Send");
  const addButton = h("button", {
    class: "button ghost", type: "button", disabled: busy || !here.length, onclick: addAsItem,
    title: "A new to-do from your unsent comments; the step stays",
  }, icon("plus"), "Add as new item");
  if (area) {
    area.addEventListener("input", () => {
      keepReply(key, area.value);
      sendButton.disabled = !ready(area.value);
    });
    area.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); sending(); }
    });
  }
  box.replaceChildren(...[
    pending,
    area,
    h("div", { class: "pages-reply-buttons" }, addButton, sendButton),
    replyError && h("p", { class: "pc-error", role: "alert", text: replyError }),
  ].filter(Boolean));
  if (typing && area) {
    area.focus();
    area.setSelectionRange(typing.selectionStart, typing.selectionEnd, typing.selectionDirection);
    area.scrollTop = scrolled;
  }
}

async function send() {
  const step = theStep();
  if (busy || !stepTakes(step)) return;
  busy = true;
  replyError = "";
  renderReply();
  try {
    const text = savedReply(replyKey(true));
    await PageComments.settled(step.id);
    // Choices still saving from the page go with this answer (SPEC NEXT-48).
    await CockpitChoices.settled(step.id);
    // Deletions started while a choice saves finish before the answer takes its comments.
    await PageComments.settled(step.id);
    const message = await write(`/api/steps/${step.id}/hand-off`, { text }, null);
    if (message.reopened) notice(`Reopened “${step.item.title}”. It is back in Busy.`);
    if (savedReply(replyKey(true)) === text) keepReply(replyKey(true), "");
    sent = { message };
    // The message waits for Take back (SPEC MSG-8); once the notice goes, it goes at once.
    sent.releaseAt = Date.now() + TAKE_BACK_SHOWN_MS;
    sent.timer = setTimeout(release, TAKE_BACK_SHOWN_MS);
  } catch (error) {
    replyError = `Not sent: ${error.message}`;
    logError("sending page comments with an answer", error);
  } finally {
    busy = false;
  }
  await reload();
}

async function release() {
  const message = sent && sent.message;
  sent = null;
  renderReply();
  if (!message) return;
  try {
    await write(`/api/messages/${message.id}/release`, {}, null);
  } catch (error) {
    // Cockpit's own loop sends it once Take back's minute is over.
    logError("releasing an answer from a page", error);
  }
}

async function takeBack() {
  if (!sent || busy) return;
  busy = true;
  // The notice's timer would release the message while Take back is on its way.
  clearTimeout(sent.timer);
  renderReply();
  try {
    const result = await write(`/api/steps/${stepId}/return`, {}, { store: replyKey(true), key: null, text: "" });
    // The server returns only the words it stopped, including follow-ups joined since Send.
    if (result.unsent) keepReply(replyKey(true), [result.unsent, savedReply(replyKey(true))].filter(Boolean).join("\n\n"));
    if (!result.stopped) notice("Your answer already reached the session; it was not taken back.");
    sent = null;
    commenting = true;
  } catch (error) {
    replyError = `Not taken back: ${error.message}`;
    logError("taking back an answer from a page", error);
    // Take back stays for a second try until the notice's time is up.
    if (sent) sent.timer = setTimeout(release, Math.max(0, sent.releaseAt - Date.now()));
  } finally {
    busy = false;
  }
  await reload();
}

// Without a step that takes them: the words in the box and the user's unsent comments on this page go to the
// session working on the page's item, at once.
async function sendToItem() {
  const page = pages.find((entry) => entry.id === shown);
  const comments = column;
  if (busy || !page || !comments) return;
  const key = replyKey(false);
  busy = true;
  replyError = "";
  renderReply();
  try {
    await PageComments.settled(comments.stepId);
    const ids = column.mine().map((comment) => comment.id);
    if (!ids.length) return;
    const message = await write(`/api/item-pages/${page.id}/comments/send`, { comment_ids: ids, text: savedReply(key) }, null);
    keepReply(key, "");
    notice(message.reopened ? `Reopened “${page.item_title}”. It is back in Busy.` : `Sent to the session working on ${page.item_title}.`);
  } catch (error) {
    replyError = `Not sent: ${error.message}`;
    logError("sending page comments to the item's session", error);
  } finally {
    busy = false;
    renderReply();
  }
  await reload();
}

// Add as new item: a to-do of the user's unsent comments on this page, whose session starts as Add starts one.
async function addAsItem() {
  const comments = column;
  const pageId = shown;
  if (busy || !comments) return;
  busy = true;
  replyError = "";
  renderReply();
  try {
    await PageComments.settled(comments.stepId);
    const ids = column.mine().map((comment) => comment.id);
    if (!ids.length) return;
    // The new item's session starts in Cockpit's first checkout, which Cockpit's state names.
    if (!state) await loadState();
    if (!state) throw new Error("Cockpit could not read its state, so it cannot start the new item's session");
    const item = await write(`/api/item-pages/${pageId}/comments/item`, { comment_ids: ids }, null);
    try {
      await write("/api/messages", {
        text: item.summary, target: { item_id: item.id, step_id: null, session_id: null, new_cwd: state.checkouts[0] },
      }, null);
      replyError = "";
      notice(`Added as item ${item.id}. A session starts on it.`);
    } catch (error) {
      replyError = `Added as item ${item.id}, but its session did not start; the sweeper starts it. (${error.message})`;
      logError("starting the session of a page comments item", error);
    }
  } catch (error) {
    replyError = `Not added: ${error.message}`;
    logError("adding page comments as an item", error);
  } finally {
    busy = false;
    renderReply();
  }
  await reload();
}

function notice(text) {
  const banner = document.getElementById("banner");
  banner.replaceChildren(h("div", { class: "banner", role: "status", text }));
  setTimeout(() => { if (banner.textContent === text) banner.replaceChildren(); }, 8000);
}

async function reload() {
  await loadState();
  renderPreview();
}

// The page the address names, else none: the list.
function chosen() {
  const named = Number(new URLSearchParams(location.search).get("id")) || null;
  return pages.some((page) => page.id === named) ? named : null;
}

function show(id, push) {
  const left = shown;
  shown = id;
  if (push) history.pushState(null, "", `/pages?id=${id}`);
  if (id !== commentOn) commentOn = null;
  for (const link of document.querySelectorAll(".pages-row")) {
    if (Number(link.dataset.page) === id) link.setAttribute("aria-current", "true");
    else link.removeAttribute("aria-current");
  }
  renderPreview();
  // Back on the list, the keyboard and the eye are on the page just read.
  if (id === null && left !== null) {
    document.querySelector(`.pages-row[data-page="${left}"]`)?.focus({ preventScroll: true });
    document.querySelector(`.pages-row[data-page="${left}"]`)?.scrollIntoView({ block: "center" });
  }
}

async function load() {
  const data = await api("/api/pages");
  // The day is in the print, so "Today" becomes "Yesterday" after midnight.
  const print = JSON.stringify([data.pages, data.public_pages, new Date().toDateString()]);
  if (print === drawnPrint) {
    // The step may have gone to its session or come back meanwhile, which moves where comments go.
    const step = theStep();
    // A column still waiting for the step (Cockpit's state failed to come before) is placed once it comes.
    if (column ? column.stepId !== takesNow(step) : commenting && state) renderPreview();
    else {
      // A session may have asked the step again meanwhile: the heading shows the question the answer goes to.
      document.querySelector("#pages-preview .pages-step")?.replaceWith(stepHead(step));
      column?.refresh();
    }
    return;
  }
  drawnPrint = print;
  pages = data.pages;
  publicOn = data.public_pages;
  // A page the user chose stays while it exists, whatever the address says.
  if (!pages.some((page) => page.id === shown)) shown = chosen();
  // The layer shows one page and never the list.
  if (!overlay) {
    renderCount();
    renderList();
  }
  renderPreview();
}

async function loadState() {
  try {
    state = await api("/api/state");
  } catch (error) {
    logError("reading Cockpit's state", error);
  }
}

function refresh() {
  load().then(() => document.getElementById("banner").replaceChildren()).catch((error) => {
    logError("loading the pages", error);
    overlayReady();
    Loading.failed(document);
    document.getElementById("banner").replaceChildren(h("div", { class: "banner fail", text: `Cockpit could not load the pages: ${error.message}` }));
  });
}

// Back and Forward: the page the address names, with its comments when the address asks for them.
function showChosen() {
  const id = chosen();
  if (new URLSearchParams(location.search).get("comment") === "1") commentOn = id;
  show(id, false);
}

// How long the notice with Take back shows; the server holds the message 60 s (store.TAKE_BACK_LIMIT).
const TAKE_BACK_SHOWN_MS = 55e3;

// P picks a part of the page and Escape stops, also with the keyboard outside the page. The G chord's P
// (Priorities) is the keyboard layer's, which takes the key first.
document.addEventListener("keydown", (event) => {
  if (!column || event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.target.closest && event.target.closest("input, textarea, select, [contenteditable]")) return;
  if (event.key === "p" || event.key === "P") { event.preventDefault(); column.togglePick(); }
  // Escape that stops a pick or a comment stops there; the layer's own Escape closes it only when nothing is.
  else if (event.key === "Escape" && !column.quiet()) { event.preventDefault(); column.cancel(); }
});
// In the layer, Escape with nothing to stop closes it: pressed here, or in the page, whose comment script sends it.
if (overlay) {
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || event.defaultPrevented || (column && !column.quiet())) return;
    if (event.target.closest && event.target.closest("textarea, input, select, [contenteditable]") && event.target.value) return;
    closeOverlay();
  });
  addEventListener("message", (event) => {
    const frame = document.querySelector("#pages-preview iframe");
    const message = event.data && event.data.cockpitPage;
    if (!frame || event.source !== frame.contentWindow || !message || message.type !== "key" || message.key !== "Escape") return;
    if (!column || column.quiet()) closeOverlay();
  });
  // A link to a task, the step's above the page or a comment's once added as a task, opens it on Cockpit's page
  // under the layer, which closes (page-overlay.js), rather than the whole of Cockpit loading inside the layer.
  document.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest && event.target.closest("a[href]");
    const id = link && taskLinkId(link);
    if (!id) return;
    event.preventDefault();
    parent.postMessage({ cockpitOverlay: "item", id }, location.origin);
  });
  // The user's input on the layer (the mouse moving, the wheel, a key or a press), the page in it included, is
  // activity for the timer of the step that opened it (SPEC TRACK-5): Cockpit's page hears none of it, so the layer passes it on, at most twice a second.
  let activitySent = 0;
  const sendActivity = () => {
    const now = Date.now();
    if (now - activitySent < 500) return;
    activitySent = now;
    parent.postMessage({ cockpitOverlay: "activity" }, location.origin);
  };
  let pointerX = null;
  let pointerY = null;
  addEventListener("pointermove", (event) => {
    if (!event.isTrusted || (event.screenX === pointerX && event.screenY === pointerY)) return;
    pointerX = event.screenX;
    pointerY = event.screenY;
    sendActivity();
  }, { capture: true, passive: true });
  for (const type of ["wheel", "keydown", "pointerdown"]) {
    addEventListener(type, (event) => {
      if (event.isTrusted) sendActivity();
    }, { capture: true, passive: true });
  }
  addEventListener("message", (event) => {
    const frame = document.querySelector("#pages-preview iframe");
    const message = event.data && event.data.cockpitPage;
    if (frame && event.source === frame.contentWindow && message && message.type === "activity") sendActivity();
  });
  // Cockpit's page says a comment was deleted from the step's card: the column lists the comments again.
  addEventListener("message", (event) => {
    if (event.origin !== location.origin || event.source !== parent || !event.data || event.data.cockpitOverlay !== "reload") return;
    reload();
  });
}
// A choice saved on the shown page: a short note by its buttons, and the reply box counts it.
addEventListener("cockpit:choice", (event) => {
  const { pageId, stepId: to, ok, error } = event.detail;
  if (pageId !== shown) return;
  choiceNote = ok
    ? { text: to ? "Choice saved; it goes with your answer" : "Choice saved", failed: false }
    : { text: `Choice not saved: ${error}`, failed: true };
  clearTimeout(choiceTimer);
  if (ok) choiceTimer = setTimeout(() => { choiceNote = null; renderPreview(); }, 4000);
  loadState().then(renderPreview);
  if (ok && beside) parent.postMessage({ cockpitOverlay: "changed" }, location.origin);
});
document.getElementById("pages-find").addEventListener("input", renderList);
addEventListener("popstate", showChosen);
// The page itself first: Cockpit's state (the step, where comments go) can take seconds on a busy host, and the
// page waits for nothing in it.
refresh();
function readState() {
  loadState().then(() => {
    const page = pages.find((entry) => entry.id === shown);
    if (page && page.id === framedPage?.id && !commenting && stepCommentsHere(page)) commenting = true;
    renderPreview();
  });
}
if (held) {
  // Shown at last: the view reads the state, and the page again in case it changed while it waited.
  addEventListener("message", (event) => {
    if (!held || event.origin !== location.origin || event.source !== parent || event.data?.cockpitOverlay !== "shown") return;
    held = false;
    const later = window.cockpitHold || [];
    window.cockpitHold = null;
    for (const run of later) run();
    readState();
    refresh();
  });
} else readState();
setInterval(() => { if (!document.hidden && !held) loadState().then(refresh); }, POLL_MS);
addEventListener("online", () => { if (!held) refresh(); });
document.addEventListener("visibilitychange", () => {
  if (document.hidden || held) return;
  loadState().then(refresh);
});
