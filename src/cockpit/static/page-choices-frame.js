"use strict";

// Added to every shared page Cockpit frames (SPEC NEXT-48; app.py inlines it after the page's HTML). A page's named
// form controls are choices the user makes on it: radio buttons, checkboxes, selects and text boxes with a `name`.
// When the user changes one, this tells Cockpit's page its name, value and the words it shows, and Cockpit saves
// it; when the page loads, Cockpit sends the choices saved before, and this sets the controls to them.
//
// A page whose choices are not form controls (cards to click, say) calls `cockpit.choose(name, value, label)`, and
// reads the saved ones from `cockpit.choices` or the `cockpitchoices` event, which comes once they arrive.
(() => {
  const post = (message) => parent.postMessage({ cockpitChoices: message }, "*");
  const SKIP_TYPES = new Set(["hidden", "submit", "button", "reset", "image", "file", "password"]);
  const controls = () => [...document.querySelectorAll("input[name], select[name], textarea[name]")]
    .filter((control) => !(control.localName === "input" && SKIP_TYPES.has(control.type)));
  const named = (name) => controls().filter((control) => control.name === name);
  const words = (control) => [...(control.labels || [])].map((label) => label.textContent.replace(/\s+/g, " ").trim()).filter(Boolean).join(" ");

  // A control's choice as Cockpit keeps it: one value, a list for checkboxes and multiple selects, or null when
  // nothing is chosen; with the words the page shows for it.
  function read(name) {
    const group = named(name);
    if (!group.length) return null;
    const first = group[0];
    if (first.type === "radio") {
      const on = group.find((control) => control.checked);
      return { chosen: on ? on.value : null, label: on ? words(on) : "" };
    }
    if (first.type === "checkbox") {
      const on = group.filter((control) => control.checked);
      return { chosen: on.map((control) => control.value), label: on.map(words).filter(Boolean).join(", ") };
    }
    if (first.localName === "select") {
      const options = [...first.selectedOptions];
      const label = options.map((option) => option.textContent.trim()).join(", ");
      return first.multiple ? { chosen: options.map((option) => option.value), label } : { chosen: first.value || null, label };
    }
    return { chosen: first.value || null, label: "" };
  }

  // A cleared choice (null, or an empty list) clears its control, also one the page checked or filled in to start
  // with.
  function write(name, chosen) {
    const values = Array.isArray(chosen) ? chosen : chosen === null ? [] : [chosen];
    for (const control of named(name)) {
      if (control.type === "radio" || control.type === "checkbox") control.checked = values.includes(control.value);
      // A cleared select shows its empty option ("Pick one"), or none: setting the value leaves a select with no such
      // option empty, where deselecting each option would have the browser pick its first again.
      else if (control.localName === "select" && chosen === null) control.value = "";
      else if (control.localName === "select") for (const option of control.options) option.selected = values.includes(option.value);
      else control.value = typeof chosen === "string" ? chosen : "";
    }
  }

  // By name, what the user chose; null or an empty list for a choice the user cleared.
  const choices = {};
  const tell = (name, chosen, label) => {
    choices[name] = chosen === "" ? null : chosen;
    post({ type: "chose", name, chosen: choices[name], label: label || "" });
  };

  // Names the user chose since the page loaded: the saved choices that arrive later leave those as they are.
  const edited = new Set();
  const report = (event) => {
    const control = event.target;
    if (!control || !control.name || !controls().includes(control)) return;
    // Text goes as it is typed, so a close of the page straight after typing keeps it.
    if (event.type === "input" && !(control.localName === "textarea" || control.localName === "input" && !["radio", "checkbox"].includes(control.type))) return;
    edited.add(control.name);
    const { chosen, label } = read(control.name);
    tell(control.name, chosen, label);
  };
  document.addEventListener("change", report, true);
  document.addEventListener("input", report, true);
  // A form has nowhere to go: the page's policy allows no form address.
  document.addEventListener("submit", (event) => event.preventDefault(), true);

  window.cockpit = Object.assign(window.cockpit || {}, {
    choices,
    choose(name, value, label) {
      if (typeof name !== "string" || !name) throw new Error("cockpit.choose needs a name");
      edited.add(name);
      write(name, value);
      tell(name, value ?? null, label || "");
    },
  });

  addEventListener("message", (event) => {
    if (event.source !== parent || !event.data || typeof event.data.cockpitChoices !== "object") return;
    const message = event.data.cockpitChoices;
    if (message.type !== "set" || !message.choices || typeof message.choices !== "object") return;
    for (const [name, chosen] of Object.entries(message.choices)) {
      if (edited.has(name)) continue;
      choices[name] = chosen;
      write(name, chosen);
    }
    dispatchEvent(new CustomEvent("cockpitchoices", { detail: { ...choices } }));
  });

  const start = () => post({ type: "ready" });
  if (document.readyState === "loading") addEventListener("DOMContentLoaded", start);
  else start();
})();
