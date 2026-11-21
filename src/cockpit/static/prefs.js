// This browser's preference in the brand menu, above Sign out, on every page: Vim editing for session
// prompts and replies. Above it, when the page carries Cockpit's mark for it, the Settings page's entry (SPEC
// CONTROL-35). Loads deferred; on the Cockpit page after prompt-editor.js.
"use strict";

(() => {
  const list = document.querySelector(".brand-menu .menu-list");
  if (!list) return;
  const span = (cls, text) => Object.assign(document.createElement("span"), { className: cls, textContent: text });

  // The editor (prompt-editor.js) loads only on the Cockpit page; elsewhere the entry reads and writes its
  // key, and a Cockpit tab follows through the storage event.
  const VIM_KEY = "cockpit.vim";
  const vimOn = () => {
    if (window.CockpitEditor) return window.CockpitEditor.get();
    try { return localStorage.getItem(VIM_KEY) === "true"; } catch { return false; }
  };
  const vim = document.createElement("button");
  vim.type = "button";
  vim.className = "vim-setting";
  vim.setAttribute("role", "switch");
  vim.setAttribute("aria-label", "Vim editing");
  vim.title = "Vim keys in reply boxes, in this browser";
  const state = span("when", "");
  vim.append(span("menu-label", "Vim editing"), state);
  const show = () => {
    vim.setAttribute("aria-checked", String(vimOn()));
    state.textContent = vimOn() ? "On" : "Off";
  };
  vim.addEventListener("click", () => {
    if (window.CockpitEditor) {
      window.CockpitEditor.set(!window.CockpitEditor.get());
      return;  // the editor announces cockpit:editor-setting
    }
    try { localStorage.setItem(VIM_KEY, String(!vimOn())); } catch { /* blocked storage: nothing to keep */ }
    show();
  });
  document.addEventListener("cockpit:editor-setting", show);
  window.addEventListener("storage", (event) => {
    if (event.key === VIM_KEY || event.key === null) show();
  });

  show();
  list.querySelector("form").before(vim);

  if (document.querySelector('meta[name="cockpit-settings"]')) {
    const settings = document.createElement("a");
    settings.href = "/settings";
    if (location.pathname === "/settings") settings.setAttribute("aria-current", "page");
    const label = span("menu-label", "Settings");
    label.prepend(Icons.icon("tune"));
    settings.append(label);
    vim.before(settings);
  }
})();
