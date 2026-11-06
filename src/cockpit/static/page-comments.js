"use strict";

// Comments on a shared page (SPEC NEXT-38): the column beside a page framed with `?comments=1`. Beside a step there
// is no column: what the user selects or picks goes to the step's reply box as a quote, and only errors show. The script in the frame (page-frame.js) proposes what the user
// selected or picked; this column checks each proposal, takes the comment, saves it on the page at once, and sends
// the frame the marks to draw. A comment saved with a step waits to go with the user's answer to that step (the
// server adds it to the hand-off); one saved without a step (the Pages tab) goes only with Add as new item.
//
// The frame's page runs scripts Cockpit does not know, which can post the same messages as the frame script, so
// a message counts only from this frame, and only in the shapes below. A forged proposal can at most offer a
// quote to comment on; the comment's text is always typed here.
window.PageComments = (() => {
  const QUOTE_MAX = 2000;
  const CONTEXT_MAX = 64;
  const PATH_MAX = 1000;
  const SHOWN_QUOTE = 160;
  // Keep deletions across column redraws, including refusals, until the comment is successfully deleted.
  const deletions = new Map();
  // Columns rebuilt for a new revision share successful deletions by comment id.
  const deletedComments = new Set();

  async function settled(stepId) {
    while (true) {
      const pending = [...deletions.values()].filter((entry) => entry.stepId === stepId);
      if (!pending.length) return;
      const failures = await Promise.all(pending.map((entry) => entry.result));
      const failure = failures.find(Boolean);
      if (failure) throw failure;
    }
  }

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

  const clip = (text, length) => (text.length > length ? `${text.slice(0, length - 1)}…` : text);
  const isText = (value, min, max) => typeof value === "string" && value.length >= min && value.length <= max;

  function validAnchor(anchor) {
    if (!anchor || typeof anchor !== "object") return null;
    if (anchor.kind === "text" && isText(anchor.quote, 1, QUOTE_MAX) && isText(anchor.prefix, 0, CONTEXT_MAX) && isText(anchor.suffix, 0, CONTEXT_MAX)) {
      return { kind: "text", quote: anchor.quote, prefix: anchor.prefix, suffix: anchor.suffix };
    }
    if (anchor.kind === "part" && isText(anchor.path, 1, PATH_MAX) && isText(anchor.text, 0, QUOTE_MAX)) {
      return { kind: "part", path: anchor.path, text: anchor.text };
    }
    return null;
  }

  function validBox(box) {
    if (!box || typeof box !== "object") return null;
    const sides = ["top", "bottom", "left", "right"];
    return sides.every((side) => Number.isFinite(box[side])) ? Object.fromEntries(sides.map((side) => [side, box[side]])) : null;
  }

  // What a comment is on, in a line: its words, or the part and the text it shows.
  function onWords(anchor) {
    if (anchor.kind === "text") return `“${clip(anchor.quote, SHOWN_QUOTE)}”`;
    return anchor.text ? `Part: “${clip(anchor.text, SHOWN_QUOTE)}”` : "Part: a chart or image with no text";
  }

  async function write(method, path, body) {
    const options = {
      method, credentials: "same-origin",
      headers: body === undefined ? {} : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    };
    return answer(await sendAction(path, options, TRY_MS, null));
  }

  function answer({ response, data }) {
    if (response.status === 401) {
      location.href = "/auth/google";
      throw Object.assign(new Error("Signed out"), { status: 401 });
    }
    if (!response.ok) throw Object.assign(new Error(data.detail || `Cockpit answered ${response.status}`), { status: response.status });
    return data;
  }

  // The column for one frame. `frame`: the page's iframe, loaded with `?comments=1`. `page`: {id, revision, title}.
  // `stepId`: the open step whose answer the comments go with, or null. `column`: the element the column draws in.
  // `onChange(comments)`: after the comments load or change. `onStale(text)`: Cockpit refused a comment because the
  // page has a new version, which the host then loads; `carried` is the refused comment's text, which the new
  // column shows so the user can comment again, or "". `kept`: the placed comment the user was writing when they
  // hid the column ({anchor, text}, from `writing()`), which comes back as it was, or null. `onUnsaved` gets that
  // comment back when its save fails after the column went. `onPlace(anchor)`: for a page beside its step, takes
  // what the user selected or picked in place of a comment box here, and the column shows only errors, hiding while
  // there are none; null for the column that lists the comments, holds Pick a part and writes them. `onPick(on)`:
  // after picking starts or stops.
  function attach({ frame, page, stepId, column, onPlace, onChange, onStale, onUnsaved, onPick, carried, kept }) {
    let comments = [];
    // How many of this column's saves and deletes Cockpit took: a read started before one of them is out of date.
    let writes = 0;
    // What the frame last reported: `ids`, the comments it found on the page as it is now, in page order, out of
    // `looked`, the ones it looked for. A comment it has not looked for yet (just loaded or saved) counts as found.
    let found = { ids: [], looked: new Set() };
    let selection = null;
    let draft = kept ? { anchor: kept.anchor, text: kept.text } : null;
    // Once the column goes, a reply still on its way changes nothing: the page keeps no marks.
    let detached = false;
    let activeId = null;
    let picking = false;
    let error = carried ? `The page has a new version, so your comment was not saved. Select its words or part again on this version, and your comment comes back to place: “${carried}”` : "";
    // The refused comment's text, until a new comment takes it up; another new version carries it on again.
    let waiting = carried || "";
    let saving = false;
    const float = h("button", { class: "button small primary pc-float", type: "button", hidden: true, onmousedown: (event) => event.preventDefault(), onclick: startDraft },
      Icons.icon("chat"), "Comment");
    document.body.append(float);

    // The user's own comments here: unsent, and going with this step's answer (or, without a step, with no
    // step that still takes them).
    const mine = (comment) => comment.state === "unsent" && (stepId === null ? comment.step_id === null || !comment.step_open : comment.step_id === stepId);

    function tell(message) {
      if (frame.contentWindow) frame.contentWindow.postMessage({ cockpitComments: message }, "*");
    }

    function sendMarks() {
      const marks = comments.map((comment) => ({
        id: comment.id, anchor: comment.anchor, tone: comment.id === activeId ? "active" : mine(comment) ? "open" : "sent",
      }));
      if (draft) marks.push({ id: "draft", anchor: draft.anchor, tone: "active" });
      tell({ type: "marks", marks });
    }

    function placeFloat() {
      const box = selection && !picking && !draft ? selection.box : null;
      float.hidden = !box;
      if (!box) return;
      const rect = frame.getBoundingClientRect();
      const top = rect.top + box.bottom + 6;
      const visible = box.bottom > 0 && box.top < rect.height;
      float.hidden = !visible;
      float.style.top = `${Math.min(top, rect.bottom - 34)}px`;
      float.style.left = `${Math.max(rect.left + 4, Math.min(rect.left + box.left, rect.right - 110))}px`;
    }

    function startDraft() {
      if (!selection) return;
      if (onPlace) {
        onPlace(selection.anchor);
        selection = null;
        tell({ type: "unselect" });
        draw(false);
        return;
      }
      draft = { anchor: selection.anchor, text: waiting };
      waiting = "";
      selection = null;
      tell({ type: "unselect" });
      draw(true);
    }

    function setPicking(on) {
      picking = on;
      tell({ type: "pick", on });
      onPick(on);
      draw(false);
    }

    function cancelDraft() {
      draft = null;
      error = "";
      draw(false);
    }

    async function save() {
      const text = draft && draft.text.trim();
      if (!text || saving) return;
      saving = true;
      error = "";
      draw(false);
      try {
        const saved = await write("POST", `/api/item-pages/${page.id}/comments`, { revision: page.revision, step_id: stepId, anchor: draft.anchor, text });
        writes += 1;
        comments = [...comments, saved];
        draft = null;
        activeId = null;
        onChange(comments);
      } catch (failure) {
        error = `Not saved: ${failure.message}`;
        logError("saving a page comment", failure);
        // A column that went keeps the comment for its own page, which brings it back there (stale or not); only
        // the column on view loads the new version with it.
        if (detached) onUnsaved({ anchor: draft.anchor, text: draft.text });
        else if (failure.status === 409 && /changed since/.test(failure.message)) onStale(text);
      } finally {
        saving = false;
        draw(false);
      }
    }

    async function remove(comment) {
      let entry = deletions.get(comment.id);
      if (!entry || entry.failed) {
        const deletion = write("DELETE", `/api/page-comments/${comment.id}`).catch((failure) => {
          if (failure.status !== 404) throw failure;
        });
        entry = { stepId, failed: false, result: deletion.then(() => null, (failure) => failure) };
        deletions.set(comment.id, entry);
      }
      try {
        const failure = await entry.result;
        if (failure) throw failure;
        if (deletions.get(comment.id) === entry) deletions.delete(comment.id);
        deletedComments.add(comment.id);
        writes += 1;
        comments = comments.filter((entry) => entry.id !== comment.id);
        onChange(comments);
      } catch (failure) {
        entry.failed = true;
        error = `Not deleted: ${failure.message}`;
        logError("deleting a page comment", failure);
      }
      draw(false);
    }

    function show(comment) {
      activeId = comment.id;
      tell({ type: "show", id: comment.id });
      draw(false);
    }

    function status(comment) {
      if (comment.state === "sent") return "Sent with your answer";
      if (comment.state === "added") {
        return h("span", null, "Added as ", h("a", { href: `/#home/item/${comment.item_id}`, text: `item ${comment.item_id}` }));
      }
      if (!comment.step_open) return "Not sent; its step closed. Open the page from the Pages tab to add it as an item.";
      return comment.step_id === stepId ? null : "Goes with your answer to another step";
    }

    function card(comment) {
      const own = mine(comment);
      const said = status(comment);
      return h("article", {
        class: `pc-card${own ? " own" : ""}${comment.id === activeId ? " active" : ""}`, "data-comment": comment.id,
        tabindex: "0", onclick: (event) => { if (!event.target.closest("button, a")) show(comment); },
        onkeydown: (event) => { if (event.key === "Enter" && event.target === event.currentTarget) show(comment); },
      },
      h("p", { class: "pc-on", text: onWords(comment.anchor) }),
      h("p", { class: "pc-text", text: comment.text }),
      own ? h("button", { class: "button small ghost pc-delete", type: "button", title: "Delete comment", "aria-label": "Delete comment", onclick: () => remove(comment) }, Icons.icon("x"))
        : said && h("p", { class: "pc-status" }, said));
    }

    function draftCard() {
      const area = h("textarea", { class: "field", rows: 3, placeholder: "Your comment", "aria-label": "Your comment", disabled: saving });
      area.value = draft.text;
      area.addEventListener("input", () => { draft.text = area.value; saveButton.disabled = !area.value.trim(); });
      area.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); save(); }
        if (event.key === "Escape") { event.preventDefault(); cancelDraft(); }
      });
      const saveButton = h("button", { class: "button small primary", type: "button", disabled: saving || !draft.text.trim(), onclick: save }, saving ? "Saving…" : "Save comment");
      return h("div", { class: "pc-card pc-draft" },
        h("p", { class: "pc-on", text: onWords(draft.anchor) }),
        area,
        h("div", { class: "pc-draft-buttons" },
          h("button", { class: "button small ghost", type: "button", onclick: cancelDraft }, "Cancel"),
          saveButton));
    }

    function draw(focusDraft) {
      if (detached) return;
      sendMarks();
      placeFloat();
      // A comment found on the page shows in page order; the user's own ones that are not found go in a list of
      // their own, and anyone else's that are not found leave the column (their words left the page).
      const byId = new Map(comments.map((comment) => [comment.id, comment]));
      const placed = [
        ...found.ids.filter((id) => byId.has(id)).map((id) => byId.get(id)),
        ...comments.filter((comment) => !found.looked.has(comment.id)),
      ];
      const lost = comments.filter((comment) => mine(comment) && found.looked.has(comment.id) && !found.ids.includes(comment.id));
      const pick = h("button", {
        class: `button small${picking ? " on" : ""}`, type: "button", "aria-pressed": String(picking), onclick: () => setPicking(!picking),
      }, Icons.icon("pick"), picking ? "Click a part…" : "Pick a part", h("kbd", { class: "pc-key", text: "P" }));
      const empty = !placed.length && !lost.length && !draft;
      // A redraw while the user types in the comment box (the minute's poll) keeps them typing where they were.
      const typing = column.querySelector(".pc-draft textarea");
      const kept = typing && document.activeElement === typing
        ? { start: typing.selectionStart, end: typing.selectionEnd, direction: typing.selectionDirection, scroll: typing.scrollTop } : null;
      if (onPlace) {
        column.hidden = !error;
        column.replaceChildren(...[error && h("p", { class: "pc-error", role: "alert", text: error })].filter(Boolean));
      } else column.replaceChildren(...[
        h("header", { class: "pc-head" }, h("h2", { text: "Comments" }), pick),
        empty && h("p", { class: "pc-hint", text: "Select words or pick a part to comment" }),
        draft && draftCard(),
        lost.length > 0 && h("section", { class: "pc-lost" },
          h("h3", { text: "Their words are no longer on the page" }),
          lost.map(card)),
        placed.map(card),
        error && h("p", { class: "pc-error", role: "alert", text: error }),
      ].flat().filter(Boolean));
      const area = column.querySelector(".pc-draft textarea");
      if (area && kept && !area.disabled) {
        area.focus({ preventScroll: true });
        area.setSelectionRange(kept.start, kept.end, kept.direction);
        area.scrollTop = kept.scroll;
      } else if (focusDraft) area?.focus();
      if (activeId !== null) column.querySelector(`[data-comment="${activeId}"]`)?.scrollIntoView({ block: "nearest" });
    }

    function onMessage(event) {
      if (event.source !== frame.contentWindow || !event.data || typeof event.data.cockpitPage !== "object") return;
      const message = event.data.cockpitPage;
      if (message.type === "ready") {
        // A frame that loaded after the user started picking picks too.
        if (picking) tell({ type: "pick", on: true });
        draw(false);
      } else if (message.type === "found" && Array.isArray(message.ids) && Array.isArray(message.looked)) {
        const next = { ids: message.ids.filter(Number.isInteger), looked: new Set(message.looked.filter(Number.isInteger)) };
        if (JSON.stringify(next.ids) === JSON.stringify(found.ids) && JSON.stringify([...next.looked]) === JSON.stringify([...found.looked])) return;
        found = next;
        draw(false);
      } else if (message.type === "select") {
        const anchor = validAnchor(message.anchor);
        const box = validBox(message.box);
        selection = anchor && anchor.kind === "text" && box ? { anchor, box } : null;
        placeFloat();
      } else if (message.type === "part") {
        // Only the pick the user started; the page's own scripts can post this message at any time.
        if (!picking) return;
        const anchor = validAnchor(message.anchor);
        picking = false;
        onPick(false);
        if (onPlace) {
          if (anchor && anchor.kind === "part") onPlace(anchor);
          draw(false);
          return;
        }
        if (anchor && anchor.kind === "part" && !(draft && draft.text.trim())) {
          draft = { anchor, text: waiting };
          waiting = "";
        }
        draw(Boolean(draft));
      } else if (message.type === "mark" && Number.isInteger(message.id)) {
        if (comments.some((comment) => comment.id === message.id)) {
          activeId = message.id;
          draw(false);
        }
      } else if (message.type === "key") {
        // The page's own scripts can post this key too. Beside a step a picked part goes into the reply box as it is,
        // with no box where the user types first, so there only Pick a part on Cockpit's side starts picking.
        if (message.key === "p" && !onPlace) setPicking(!picking);
        // Escape in the page stops picking; a draft is the comment box's, which its own Escape and Cancel close.
        else if (message.key === "Escape" && picking) setPicking(false);
      }
    }

    // A read that a save or delete overtook holds the list from before it, so it reads again.
    async function refresh() {
      try {
        const from = writes;
        const { response, data } = await readThroughRestart(`/api/item-pages/${page.id}/comments`, { credentials: "same-origin" });
        if (from !== writes) return refresh();
        comments = answer({ response, data }).comments.filter((comment) => !deletedComments.has(comment.id));
        onChange(comments);
      } catch (failure) {
        error = `Comments not loaded: ${failure.message}`;
        logError("loading page comments", failure);
      }
      draw(false);
    }

    const reposition = () => placeFloat();
    addEventListener("message", onMessage);
    addEventListener("scroll", reposition, { passive: true, capture: true });
    addEventListener("resize", reposition);
    // A comment brought back shows at once, before the comments load.
    draw(false);
    refresh();

    return {
      refresh,
      togglePick: () => setPicking(!picking),
      picking: () => picking,
      cancel: () => {
        if (picking) setPicking(false);
        else if (draft) cancelDraft();
      },
      // Whether Escape has nothing here to stop: no pick and no comment being written.
      quiet: () => !picking && !draft,
      // The user's unsent comments here, which Send or Add as new item take.
      mine: () => comments.filter((comment) => mine(comment) && !deletedComments.has(comment.id)),
      stepId,
      // The words of a comment being written, which a new column for the page carries.
      draftText: () => (draft ? draft.text.trim() : waiting),
      // The comment being written, for a column that comes back when the user shows the comments again: its
      // anchor is null while its words wait to be placed. A comment being saved is the save's: none, and
      // `onUnsaved` brings it back if the save fails.
      writing: () => (saving ? null : draft ? { anchor: draft.anchor, text: draft.text } : waiting ? { anchor: null, text: waiting } : null),
      // The column goes: the page stops picking and drops its marks, unless the frame went with it.
      detach() {
        detached = true;
        tell({ type: "pick", on: false });
        tell({ type: "marks", marks: [] });
        removeEventListener("message", onMessage);
        removeEventListener("scroll", reposition, { capture: true });
        removeEventListener("resize", reposition);
        float.remove();
      },
    };
  }

  return { attach, onWords, settled };
})();
