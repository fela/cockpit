"use strict";

// DOM and time helpers shared by usage views.
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

// A section's heading, as on the Cockpit page, with a line or a link on how to read the section beside it.
function head(id, title, hint) {
  return h("div", { class: "section-head" }, h("h2", { id, text: title }), hint && h("span", { class: "hint" }, hint));
}

function moment(iso) {
  return new Date(iso).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
}

function clock(iso) {
  return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
}

// How long until a time: "45 min", "4 h 12 min", "3 d 22 h".
function countdown(iso) {
  const minutes = Math.max(0, Math.round((new Date(iso).getTime() - Date.now()) / 60000));
  if (minutes < 60) return `${minutes} min`;
  const [big, small, unit] = minutes < 1440
    ? [`${Math.floor(minutes / 60)} h`, minutes % 60, "min"]
    : [`${Math.floor(minutes / 1440)} d`, Math.floor((minutes % 1440) / 60), "h"];
  return small ? `${big} ${small} ${unit}` : big;
}

