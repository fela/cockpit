"use strict";

// The parts feedback mode offers (SPEC FEEDBACK-7), shared by the Cockpit page (app.js) and the other tabs
// (feedback.js). The user sees boxes, not the page's structure, so every guess and every step is a part whose
// box looks different from the one before: an element and its wrapper that cover the same box count as one.
// Each page passes its own `pickable` selector (what a pick may land on) and `controls` (the picker's own keys
// and boxes, which are never picked).
const Pick = (() => {
  // Two boxes the eye cannot tell apart: every edge within 3 px.
  function sameBox(a, b) {
    const r = a.getBoundingClientRect();
    const s = b.getBoundingClientRect();
    return Math.abs(r.left - s.left) <= 3 && Math.abs(r.top - s.top) <= 3
      && Math.abs(r.right - s.right) <= 3 && Math.abs(r.bottom - s.bottom) <= 3;
  }

  // Whether the user sees an element as a part of its own: one the page names in `pickable`, or a
  // block that holds words of its own or draws its own box (a border, a fill, a shadow). A word set inside a
  // line, such as bold or code, belongs to its line, and what a button or link holds belongs to it unless the page
  // names it: a session's state dot or a chip in a row stays a part of its own, as before.
  function isPart(el, pickable) {
    if (el.matches(pickable)) return true;
    if (el instanceof SVGElement || el.parentElement?.closest("button, a, summary")) return false;
    const style = getComputedStyle(el);
    if (style.display === "inline" || style.display === "contents") return false;
    const ownWords = [...el.childNodes].some((node) => node.nodeType === Node.TEXT_NODE && node.data.trim());
    const border = ["Top", "Right", "Bottom", "Left"].some((side) => parseFloat(style[`border${side}Width`]) > 0);
    const fill = style.backgroundColor !== "rgba(0, 0, 0, 0)" || style.backgroundImage !== "none";
    return ownWords || border || fill || style.boxShadow !== "none";
  }

  // The nearest part around `el`, short of the page itself.
  function outer(el, pickable) {
    for (let part = el.parentElement; part && part !== document.body; part = part.parentElement) {
      if (isPart(part, pickable)) return part;
    }
    return null;
  }

  // The part around `el` with a box of its own; the whole page past the last one.
  function around(el, pickable) {
    let part = el;
    do part = outer(part, pickable) || document.body;
    while (part !== document.body && sameBox(part, el));
    return part;
  }

  // The part at a point: the smallest pickable part under it, an input box included. Every element the user
  // sees can be picked: under a point no pickable part holds, the topmost element there is the
  // part, as `of` gives it.
  function at(x, y, pickable, controls) {
    const stack = document.elementsFromPoint(x, y);
    const passed = stack.length > 0 && passedAt(stack[0], x, y);
    if (passed) return passed.closest(controls) ? null : of(passed, pickable);
    if (stack.some((el) => el.closest(controls))) return null;
    return stack.length > 0 ? of(stack[0], pickable) : null;
  }

  // The part an element is in: the element itself or the nearest part around it, else the element, or the HTML
  // element an icon's drawing sits in. Null for the page itself.
  function of(el, pickable) {
    const part = (isPart(el, pickable) ? el : outer(el, pickable))
      || (el instanceof SVGElement ? (el.ownerSVGElement || el).parentElement : el);
    return part && part !== document.body && part !== document.documentElement ? part : null;
  }

  // Safari and Firefox send no click from a disabled control, such as Update while a check runs, and a click
  // in a framed page never reaches Cockpit, so while picking both let the press through to what lies under
  // them (style.css), and drop out of the elements at the point. Each is still the part picked: the innermost
  // disabled control or frame inside the top element whose box holds the point.
  function passedAt(top, x, y) {
    let found = null;
    for (const el of top.querySelectorAll(":disabled, iframe")) {
      const r = el.getBoundingClientRect();
      if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom && (!found || found.contains(el))) found = el;
    }
    return found;
  }

  // The guesses one spot offers, each click there the next: the part under the pointer, then the parts
  // around it up to its named area (a section, a card), then its neighbours: the parts just before and
  // after it, from the nearest level of the page that has any.
  function guesses(first, pickable) {
    const list = [first];
    const add = (part) => { if (part && part !== document.body && !list.some((seen) => seen === part || sameBox(seen, part))) list.push(part); };
    for (let part = first; part !== document.body && !part.id;) {
      part = around(part, pickable);
      add(part);
    }
    for (let el = first; el && el !== document.body; el = el.parentElement) {
      const before = neighbour(el, "previousElementSibling", pickable);
      const after = neighbour(el, "nextElementSibling", pickable);
      if (!before && !after) continue;
      add(before);
      add(after);
      break;
    }
    return list;
  }

  // The nearest sibling on one side that is or holds a part with a box to see.
  function neighbour(el, side, pickable) {
    for (let next = el[side]; next; next = next[side]) {
      const part = firstPart(next, pickable);
      if (part && part.getClientRects().length) return part;
    }
    return null;
  }

  // The element itself when it is a part, else the first part inside it, in the page's order.
  function firstPart(el, pickable) {
    if (isPart(el, pickable)) return el;
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_ELEMENT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) if (isPart(node, pickable)) return node;
    return null;
  }

  // Narrower: the part inside `el` on the way back to `from`, the part the user clicked; null where the way ends.
  function toward(el, from, pickable) {
    if (!el.isConnected || !from.isConnected || el === from || !el.contains(from)) return null;
    for (let part = from; part !== document.body;) {
      const next = around(part, pickable);
      if (next === el) return part;
      if (next === part) return null;
      part = next;
    }
    return null;
  }

  // Whether the user can see an element: rendered, and neither it nor a part around it transparent or hidden.
  const shown = (el) => el.checkVisibility({ opacityProperty: true, visibilityProperty: true, checkOpacity: true, checkVisibilityCSS: true });

  // Plates that open while the pointer is over their group (the overview's). The pointer leaves for the
  // feedback box right after a pick and the plate closes, so a part around one shows it neither in its
  // words nor in its copy; a pick inside a plate keeps it.
  const PLATES = ".light-list";
  const plateIn = (node, el) => {
    const plate = node.closest(PLATES);
    return Boolean(plate) && plate !== el && el.contains(plate);
  };

  // Whether a box ends at its edges, so what lies past them is cut off.
  const clips = (el) => getComputedStyle(el).overflow !== "visible";
  const overlaps = (a, b) => a.width > 0 && a.height > 0 && a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;

  // The words of a part the user sees on the page: hidden text, a plate's, and text past the edge of a
  // box that cuts it off are left out.
  function seenText(el) {
    const pieces = [];
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    const range = document.createRange();
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const text = node.data.trim();
      if (!text || !shown(node.parentElement) || plateIn(node.parentElement, el)) continue;
      range.selectNodeContents(node);
      const rects = [...range.getClientRects()];
      const inView = rects.some((rect) => {
        for (let box = node.parentElement; box; box = box === el ? null : box.parentElement) {
          if (clips(box) && !overlaps(rect, box.getBoundingClientRect())) return false;
        }
        return rect.width > 0 && rect.height > 0;
      });
      if (inView) pieces.push(text);
    }
    return pieces.join(" ");
  }

  // A password field, such as the secret box's (MSG-15), or a field for a credential shown as text, such as a
  // sign-in code (`data-credential`): the feedback keeps neither its value nor a copy of it,
  // only its placeholder or label.
  const secret = (el) => el.localName === "input" && (el.type === "password" || el.hasAttribute("data-credential"));

  // What the part says: the words the user sees on it, else an input's value or placeholder, else its label.
  function words(el) {
    const text = el.matches("input, textarea, select") ? ((!secret(el) && el.value) || el.placeholder || "") : seenText(el);
    return text || el.getAttribute("aria-label") || el.title || el.dataset.tip || "";
  }

  // A copy of the part to show in the feedback box, as the user sees it: its look frozen into inline styles,
  // scaled down to fit `width` by `height`. A part the page hides, and a plate, keeps its place in the
  // copy, empty and unseen. Null where it would shrink past a quarter of its size (a whole
  // section, the page), so the box shows the part's words instead; `small` marks a copy under half size,
  // too small to read. The copy is inert: no ids, keys or names of its own.
  const SKIP = /^(transition|animation|anchor-name|position-anchor|view-transition|cursor|pointer-events)/;
  function copy(el, width, height) {
    if (el === document.body) return null;
    const box = el.getBoundingClientRect();
    if (!box.width || !box.height) return null;
    const scale = Math.min(1, width / box.width, height / box.height);
    if (scale < 0.25) return null;
    const clone = el.cloneNode(true);
    const all = [el, ...el.querySelectorAll("*")];
    const twins = new Map(all.map((original, i) => [original, i]));
    const clones = [clone, ...clone.querySelectorAll("*")];
    // The copy does not load a framed page again: the frame keeps its box, empty.
    for (const frame of clones) if (frame.localName === "iframe") frame.removeAttribute("src");
    const hidden = new Set();
    const originals = all.filter((original) => {
      if (hidden.has(original.parentElement)) {
        hidden.add(original);
        return false;
      }
      if (original !== el && (!shown(original) || original.matches(PLATES))) {
        hidden.add(original);
        const target = clones[twins.get(original)];
        target.replaceChildren();
        target.style.setProperty("visibility", "hidden");
      }
      return true;
    });
    if (originals.length > 600) return null;
    originals.forEach((original) => {
      const style = getComputedStyle(original);
      const target = clones[twins.get(original)];
      for (const name of style) if (!SKIP.test(name)) target.style.setProperty(name, style.getPropertyValue(name));
      for (const name of ["id", "data-key", "name", "for", "tabindex", "autofocus"]) target.removeAttribute(name);
      if (target.classList) target.classList.remove("pick-hover", "pick-chosen");
      if (secret(original)) {
        target.removeAttribute("value");
        target.value = "";
      } else if ("value" in original && typeof original.value === "string") target.value = original.value;
      if (hidden.has(original)) target.style.setProperty("visibility", "hidden");
    });
    clone.style.setProperty("position", "static");
    clone.style.setProperty("margin", "0");
    clone.style.setProperty("outline", "none");
    // The part keeps its own size: limits in percent would resolve against the copy's small frame.
    clone.style.setProperty("width", `${box.width}px`);
    clone.style.setProperty("height", `${box.height}px`);
    for (const name of ["min-width", "min-height"]) clone.style.setProperty(name, "0");
    for (const name of ["max-width", "max-height"]) clone.style.setProperty(name, "none");
    clone.style.setProperty("transform", `scale(${scale})`);
    clone.style.setProperty("transform-origin", "0 0");
    const frame = document.createElement("div");
    frame.className = `pick-copy${scale < 0.5 ? " small" : ""}`;
    frame.inert = true;
    frame.setAttribute("aria-hidden", "true");
    frame.style.width = `${Math.ceil(box.width * scale)}px`;
    frame.style.height = `${Math.ceil(box.height * scale)}px`;
    frame.append(clone);
    return frame;
  }

  // The colour the part stands on: the nearest opaque background at or behind it. A tint (a well's
  // translucent wash) is passed over for the colour under it, which sets how the part reads.
  function ground(el) {
    for (let part = el; part; part = part.parentElement) {
      const colour = getComputedStyle(part).backgroundColor;
      const alpha = colour.match(/(?:,|\/)\s*([\d.]+)(%?)\)$/);
      if (!alpha || Number(alpha[1]) >= (alpha[2] ? 100 : 1)) return colour;
    }
    return "var(--bg)";
  }

  return { around, at, of, guesses, toward, words, copy, ground };
})();
