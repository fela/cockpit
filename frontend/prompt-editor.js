import { EditorState, Prec, Transaction } from "@codemirror/state";
import { EditorView, drawSelection, keymap, placeholder } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { markdown } from "@codemirror/lang-markdown";
import { syntaxHighlighting } from "@codemirror/language";
import { classHighlighter } from "@lezer/highlight";
import { vim } from "@replit/codemirror-vim";

// The textarea remains the draft's backing field. Each rendered composer binds
// its own Send callback; a view follows its draft key across synchronous redraws
// so cursor, Vim mode and undo history survive them.
const KEY = "cockpit.vim";
const entries = new Map();
const bindings = new WeakMap();
const nonce = document.querySelector('meta[name="cockpit-style-nonce"]')?.content || "";
let enabled = kept();

function kept() {
  try { return localStorage.getItem(KEY) === "true"; } catch { return false; }
}

function fields() {
  return [...document.querySelectorAll(".prompt-field > textarea")].filter((area) => !area.closest(".motion-ghost"));
}

function mount(area) {
  const key = area.dataset.key;
  const binding = bindings.get(area);
  if (!binding) return;
  let entry = entries.get(key);
  if (!entry) {
    entry = { area, submit: binding.submit };
    const view = new EditorView({
      state: EditorState.create({
        doc: area.value,
        selection: { anchor: area.selectionStart, head: area.selectionEnd },
        extensions: [
          EditorView.cspNonce.of(nonce),
          // Sending belongs to the composer in every Vim mode. The highest
          // priority keymap also prevents Vim from consuming Cmd/Ctrl+Enter.
          // Shift adds nothing to Send, as in the plain box.
          Prec.highest(keymap.of(["Mod-Enter", "Shift-Mod-Enter"].map((key) => ({ key, run(view) {
            // compositionStarted covers the start of a composition, before composing turns true.
            if (!view.composing && !view.compositionStarted && entry.area.value.trim()) entry.submit();
            return true;
          } })))),
          vim({ status: true }),
          drawSelection(), history(), keymap.of([...defaultKeymap, ...historyKeymap]),
          markdown(), syntaxHighlighting(classHighlighter), EditorView.lineWrapping,
          placeholder(area.placeholder),
          EditorView.contentAttributes.of({
            "aria-label": area.getAttribute("aria-label") || "Message",
            "data-key": key, "aria-describedby": "vim-editor-hint",
            "autocapitalize": "off", "spellcheck": "false",
          }),
          EditorView.updateListener.of((update) => {
            if (!update.docChanged) return;
            entry.area.value = update.state.doc.toString();
            // The ordinary draft/target/Send listeners stay authoritative.
            entry.area.dispatchEvent(new Event("input", { bubbles: true }));
          }),
        ],
      }),
    });
    entry.view = view;
    entries.set(key, entry);
  } else {
    entry.area = area;
    entry.submit = binding.submit;
    if (entry.view.state.doc.toString() !== area.value) {
      entry.view.dispatch({
        changes: { from: 0, to: entry.view.state.doc.length, insert: area.value },
        annotations: Transaction.addToHistory.of(false),
      });
    }
  }
  area.hidden = true;
  area.parentElement.append(entry.view.dom);
}

function unmount(entry) {
  const { area, view } = entry;
  const selection = view.state.selection.main;
  const focused = view.dom.contains(document.activeElement);
  area.value = view.state.doc.toString();
  view.destroy();
  view.dom.remove();
  area.hidden = false;
  area.setSelectionRange(selection.from, selection.to);
  if (focused && area.isConnected) area.focus({ preventScroll: true });
}

function apply(on) {
  enabled = on;
  if (on) {
    const active = document.activeElement;
    for (const area of fields()) {
      mount(area);
      if (area === active) entries.get(area.dataset.key)?.view.focus();
    }
  } else {
    for (const entry of entries.values()) unmount(entry);
    entries.clear();
  }
  document.dispatchEvent(new Event("cockpit:editor-setting"));
}

function set(on) {
  apply(Boolean(on));
  try { localStorage.setItem(KEY, String(enabled)); } catch { /* This tab still works without storage. */ }
}

function focus(area, options) {
  const view = area && entries.get(area.dataset.key)?.view;
  if (view && enabled) view.focus();
  else area?.focus(options);
}

function captureFocus() {
  for (const [key, entry] of entries) {
    const target = document.activeElement;
    // Vim's / and : command fields live in the panel, outside contentDOM.
    if (entry.view.dom.contains(target)) return { key, editor: true, target };
  }
  return null;
}

window.addEventListener("storage", (event) => {
  if (event.key === KEY || event.key === null) apply(kept());
});

// Disconnect after a redraw has finished, never during a view's move to the
// replacement composer. Closed composers release observers and DOM listeners.
new MutationObserver(() => {
  for (const [key, entry] of entries) {
    if (!entry.area.isConnected) {
      entry.view.destroy();
      entries.delete(key);
    }
  }
}).observe(document.body, { subtree: true, childList: true });

// One shared description avoids duplicate IDs when several replies are open.
const hint = document.createElement("span");
hint.id = "vim-editor-hint";
hint.hidden = true;
hint.textContent = "Vim editor. Press i to insert, Escape for Normal mode. Cmd or Ctrl Enter sends. Tab leaves the editor.";
document.body.append(hint);

window.CockpitEditor = {
  get: () => enabled, set, focus, captureFocus,
  create(area, { submit }) {
    const field = document.createElement("div");
    field.className = "prompt-field";
    field.append(area);
    bindings.set(area, { submit });
    if (enabled) mount(area);
    return field;
  },
  // Puts `text` at the top of the box, keeping the user's selection on the text it was on (words another tab
  // put back while the user types here).
  prepend(area, text) {
    const view = entries.get(area.dataset.key)?.view;
    if (view && enabled) {
      // The cursor stays on the text it was on, also at the very start (it moves past the words, not before them).
      const changes = view.state.changes({ from: 0, insert: text });
      view.dispatch({ changes, selection: view.state.selection.map(changes, 1), annotations: Transaction.addToHistory.of(false) });
      return;
    }
    const [start, end, direction] = [area.selectionStart, area.selectionEnd, area.selectionDirection];
    area.setRangeText(text, 0, 0);
    area.setSelectionRange(start + text.length, end + text.length, direction);
    area.dispatchEvent(new Event("input", { bubbles: true }));
  },
  end(area) {
    const view = area && entries.get(area.dataset.key)?.view;
    if (view && enabled) view.dispatch({ selection: { anchor: view.state.doc.length }, scrollIntoView: true });
    else area?.setSelectionRange(area.value.length, area.value.length);
  },
};
