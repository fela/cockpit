"use strict";

// Reply boxes work while the optional Vim bundle loads. Keep its version tied to this page's assets.
(() => {
  const key = "cockpit.vim";
  const source = new URL(document.currentScript.src);
  source.pathname = source.pathname.replace(/editor-loader\.js$/, "prompt-editor.js");
  const bindings = new WeakMap();
  let enabled = kept();
  let loading = false;

  function kept() {
    try { return localStorage.getItem(key) === "true"; } catch { return false; }
  }

  function load() {
    if (loading) return;
    loading = true;
    const script = document.createElement("script");
    script.src = source.href;
    script.onload = () => {
      const editor = window.CockpitEditor;
      const active = document.activeElement;
      if (editor.get() !== enabled) editor.set(enabled);
      for (const area of document.querySelectorAll(".prompt-field > textarea")) {
        const binding = bindings.get(area);
        if (!binding || area.closest(".motion-ghost")) continue;
        const holder = area.parentElement;
        // The composer owns the wrapper's quote layer and listeners; upgrade its field in place.
        const upgraded = editor.create(area, binding);
        holder.prepend(...upgraded.childNodes);
        if (area === active) editor.focus(area, { preventScroll: true });
      }
      document.dispatchEvent(new Event("cockpit:editor-setting"));
    };
    script.onerror = () => {
      script.remove();
      loading = false;
      loader.set(false);
      logError("loading Vim editor", new Error("Vim editor could not be loaded"));
      toast("Vim editor did not load. Enable Vim editing to try again.", true);
    };
    document.head.append(script);
  }

  const loader = {
    get: () => enabled,
    set(on) {
      enabled = Boolean(on);
      try { localStorage.setItem(key, String(enabled)); } catch { /* This tab still works without storage. */ }
      document.dispatchEvent(new Event("cockpit:editor-setting"));
      if (enabled) load();
    },
    create(area, binding) {
      const field = document.createElement("div");
      field.className = "prompt-field";
      field.append(area);
      bindings.set(area, binding);
      return field;
    },
    focus: (area, options) => area?.focus(options),
    captureFocus: () => null,
    prepend(area, text) {
      const [start, end, direction] = [area.selectionStart, area.selectionEnd, area.selectionDirection];
      area.setRangeText(text, 0, 0);
      area.setSelectionRange(start + text.length, end + text.length, direction);
      area.dispatchEvent(new Event("input", { bubbles: true }));
    },
    end: (area) => area?.setSelectionRange(area.value.length, area.value.length),
  };
  window.CockpitEditor = loader;
  window.addEventListener("storage", (event) => {
    if (window.CockpitEditor !== loader || (event.key !== key && event.key !== null)) return;
    enabled = kept();
    if (enabled) load();
    document.dispatchEvent(new Event("cockpit:editor-setting"));
  });
  if (enabled) load();
})();
