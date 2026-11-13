"use strict";

// The parts feedback mode offers (SPEC FEEDBACK-7), shared by the Cockpit page (app.js) and the other tabs
// (feedback.js). The user sees boxes, not the page's structure, so every guess and every step is a part whose
// box looks different from the one before: an element and its wrapper that cover the same box count as one.
// Each page passes its own `pickable` selector (what a pick may land on) and `controls` (the picker's own keys
// and boxes, which are never picked).
//
// A page framed from Cockpit's own origin, such as Usage in the center layer, is part of the page the user sees:
// a pick goes into it (`at`), Wider steps out of it to its frame, and boxes are compared in this page's
// coordinates (`box`). Each element is measured with its own page's window.
const Pick = (() => {
  const isSvg = (el) => el.namespaceURI === "http://www.w3.org/2000/svg";
  const css = (el) => el.ownerDocument.defaultView.getComputedStyle(el);

  // The frame showing the page `el` is in; null for an element of this page, and for one of a framed page the
  // frame has since left (its page has no window any more).
  const frameOf = (el) => (el.ownerDocument === document ? null : el.ownerDocument.defaultView?.frameElement || null);

  // Whether `el` is still on the page the user sees: in its page, and that page still shown by every frame it is in.
  function alive(el) {
    for (let node = el; node; node = frameOf(node)) {
      if (!node.isConnected || !node.ownerDocument.defaultView) return false;
      if (node.ownerDocument === document) return true;
    }
    return false;
  }

  // The data attributes that name what a part is about (the item, step or session); the others hold passing
  // state, such as a time or the words shown on hover.
  const NAMES = ["item", "step", "session", "focus", "request", "credential", "page"];
  const NAMED = NAMES.map((name) => `[data-${name}]`).join(",");

  // What `node` is about: the naming attributes on it, else on the first element inside it that has any. A row of
  // Usage's table is about the session its name button carries, and so is each of its cells' row.
  function about(node) {
    const named = NAMES.some((name) => name in node.dataset) ? node : node.querySelector(NAMED);
    return JSON.stringify(named ? NAMES.map((name) => named.dataset[name] ?? null) : null);
  }

  // Where a part sits, so a redraw that replaces it with one like it in the same place does not lose it: the
  // nearest element around it with an id, and the way down from there by the kind of element, its place among its
  // parent's children of that kind, and what it is about, so a row that moved is not taken for the one now in its
  // place. Kept for every part the picker meets while the page still shows it.
  const trails = new WeakMap();
  function trail(el) {
    const way = [];
    let node = el;
    for (; node.parentElement && !node.id && node !== node.ownerDocument.body; node = node.parentElement) {
      const kind = node.localName;
      way.unshift([kind, [...node.parentElement.children].filter((child) => child.localName === kind).indexOf(node), about(node)]);
    }
    return { doc: node.ownerDocument, id: node.id || null, way };
  }

  // The part the page shows now for `el`: `el` itself while the page shows it, else the part that sits where it
  // sat (trail) if every element on the way there is the same kind and about the same thing, else null.
  function now(el) {
    if (alive(el)) {
      trails.set(el, trail(el));
      return el;
    }
    const was = trails.get(el);
    if (!was || !was.doc.defaultView) return null;
    let node = was.id ? was.doc.getElementById(was.id) : was.doc.body;
    for (const [kind, index, name] of was.way) {
      node = node && [...node.children].filter((child) => child.localName === kind)[index];
      if (!node || about(node) !== name) return null;
    }
    return alive(node) ? now(node) : null;
  }

  // The element around `el`: its parent, short of its page's body, past which a framed page leads out to its frame.
  function parent(el) {
    const up = el.parentElement;
    return up && up !== el.ownerDocument.body ? up : frameOf(el);
  }

  // Whether `el` holds `inner`, through the frames `inner` is in.
  function holds(el, inner) {
    for (let node = inner; node; node = frameOf(node)) if (el.contains(node)) return true;
    return false;
  }

  // An element's box in this page's coordinates: a framed page's own are shifted by where its frame shows it.
  function box(el) {
    const r = el.getBoundingClientRect();
    let left = r.left;
    let top = r.top;
    for (let frame = frameOf(el); frame; frame = frameOf(frame)) {
      const f = frame.getBoundingClientRect();
      left += f.left + frame.clientLeft;
      top += f.top + frame.clientTop;
    }
    return { left, top, right: left + r.width, bottom: top + r.height };
  }

  // The page a frame shows, when it comes from Cockpit's own origin and has loaded; null for any other frame.
  function framedPage(el) {
    if (el.localName !== "iframe") return null;
    try {
      const page = el.contentDocument;
      return page && page.body ? page : null;
    } catch {
      return null;
    }
  }

  // Two boxes the eye cannot tell apart: every edge within 3 px.
  function sameBox(a, b) {
    const r = box(a);
    const s = box(b);
    return Math.abs(r.left - s.left) <= 3 && Math.abs(r.top - s.top) <= 3
      && Math.abs(r.right - s.right) <= 3 && Math.abs(r.bottom - s.bottom) <= 3;
  }

  // Whether the user sees an element as a part of its own: one the page names in `pickable`, or a
  // block that holds words of its own or draws its own box (a border, a fill, a shadow). A word set inside a
  // line, such as bold or code, belongs to its line, and what a button or link holds belongs to it unless the page
  // names it: a session's state dot or a chip in a row stays a part of its own, as before.
  function isPart(el, pickable) {
    if (el.matches(pickable)) return true;
    if (el.parentElement?.closest("button, a, summary")) return false;
    if (isSvg(el)) return svgPart(el);
    const style = css(el);
    if (style.display === "inline" || style.display === "contents") return false;
    const ownWords = [...el.childNodes].some((node) => node.nodeType === Node.TEXT_NODE && node.data.trim());
    const border = ["Top", "Right", "Bottom", "Left"].some((side) => parseFloat(style[`border${side}Width`]) > 0);
    const fill = style.backgroundColor !== "rgba(0, 0, 0, 0)" || style.backgroundImage !== "none";
    return ownWords || border || fill || style.boxShadow !== "none";
  }

  // A drawing the user sees as a part of its own: a picture over 32 px, such as a chart (an icon is smaller), or a
  // shape in it the page names, with a title of its own or data on it (a bar in a chart).
  function svgPart(el) {
    if (el.localName === "svg") {
      const r = el.getBoundingClientRect();
      return Math.max(r.width, r.height) > 32;
    }
    return [...el.children].some((child) => child.localName === "title") || [...el.attributes].some((attr) => attr.name.startsWith("data-"));
  }

  // The nearest part around `el`, short of the page itself.
  function outer(el, pickable) {
    for (let part = parent(el); part; part = parent(part)) {
      if (isPart(part, pickable)) return part;
    }
    return null;
  }

  // The part at a point: the smallest pickable part under it, an input box included. Every element the user
  // sees can be picked: under a point no pickable part holds, the topmost element there is the
  // part, as `of` gives it. Over a page framed from Cockpit's origin, the part is the one in that page.
  function at(x, y, pickable, controls) {
    return atIn(document, x, y, pickable, controls);
  }

  function atIn(page, x, y, pickable, controls) {
    const stack = page.elementsFromPoint(x, y);
    if (stack.length === 0) return null;
    const under = passedAt(stack[0], x, y) || stack[0];
    if (under.closest(controls)) return null;
    const framed = framedPage(under);
    if (framed) {
      const r = under.getBoundingClientRect();
      const inner = atIn(framed, x - r.left - under.clientLeft, y - r.top - under.clientTop, pickable, controls);
      if (inner) return inner;
    }
    return of(under, pickable);
  }

  // The part an element is in: the element itself or the nearest part around it, else the element, or the HTML
  // element an icon's drawing sits in. Null for the page itself; a framed page's own body is its frame.
  function of(el, pickable) {
    const page = el.ownerDocument;
    if (el === page.body || el === page.documentElement) return page === document ? null : of(frameOf(el), pickable);
    const part = (isPart(el, pickable) ? el : outer(el, pickable))
      || (isSvg(el) ? (el.ownerSVGElement || el).parentElement : el);
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
  // Wider gives around it up to its named area (a section, a card), then its neighbours: the parts just before and
  // after it, from the nearest level of the page that has any.
  function guesses(first, pickable, controls) {
    const list = [first];
    const add = (part) => { if (part && part !== document.body && !list.some((seen) => seen === part || sameBox(seen, part))) list.push(part); };
    for (let part = first, i = 0; part && part !== document.body && !part.id && i < WAY_LIMIT; i++) {
      part = wider(part, pickable, controls);
      add(part);
    }
    for (let el = first; el && el !== document.body; el = parent(el)) {
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
    const walker = el.ownerDocument.createTreeWalker(el, NodeFilter.SHOW_ELEMENT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) if (isPart(node, pickable)) return node;
    return null;
  }

  // Wider, Narrower and Sideways go by what the user sees (SPEC FEEDBACK-7): the boxes on the screen, cut where a
  // box around them cuts off what lies past its edges, rather than by the page's structure alone. Boxes are
  // {left, top, right, bottom} in this page's coordinates.
  const area = (r) => Math.max(0, r.right - r.left) * Math.max(0, r.bottom - r.top);
  const cut = (r, s) => ({ left: Math.max(r.left, s.left), top: Math.max(r.top, s.top), right: Math.min(r.right, s.right), bottom: Math.min(r.bottom, s.bottom) });
  const within = (r, s, slack) => r.left >= s.left - slack && r.top >= s.top - slack && r.right <= s.right + slack && r.bottom <= s.bottom + slack;
  const alike = (r, s) => Math.abs(r.left - s.left) <= 3 && Math.abs(r.top - s.top) <= 3
    && Math.abs(r.right - s.right) <= 3 && Math.abs(r.bottom - s.bottom) <= 3;

  // The part of an element's box the user sees: cut by each box around it that clips (short of a part fixed to
  // the screen, which no box around it clips), by each frame it is shown in, and by the window.
  function seen(el) {
    let r = box(el);
    for (let node = el; node;) {
      let fixed = false;
      for (let up = node; up && up !== up.ownerDocument.body; up = up.parentElement) {
        if (up !== node && !fixed && clips(up)) r = cut(r, box(up));
        if (css(up).position === "fixed") fixed = true;
      }
      node = frameOf(node);
      if (node) r = cut(r, box(node));
    }
    return cut(r, { left: 0, top: 0, right: innerWidth, bottom: innerHeight });
  }

  // The box to compare `el` by: the part the user sees, or its whole box when none of it is in view.
  const view = (el) => {
    const r = seen(el);
    return area(r) > 0 ? r : box(el);
  };

  // A part fixed to the screen, or one that sticks, moves on its own: a part behind it is not around it.
  function layer(el) {
    for (let up = el; up && up !== up.ownerDocument.body; up = up.parentElement) {
      if (["fixed", "sticky"].includes(css(up).position) || up.matches(":popover-open, dialog[open]")) return up;
    }
    return null;
  }

  // The elements directly inside `el`; a frame from Cockpit's own origin holds its page's body.
  function kids(el) {
    const framed = framedPage(el);
    return framed ? [...framed.body.children] : [...el.children];
  }

  // A group the eye sees as one: a block, not inside a control, that lays out two or more things side by side or
  // one under another, such as a row of keys or a heading over its text. A group has no fill or border of its own
  // (that would make it a part), only the room around what it holds.
  function isGroup(el) {
    if (isSvg(el) || el.parentElement?.closest("button, a, summary") || el === el.ownerDocument.body) return false;
    const display = css(el).display;
    if (display === "inline" || display === "contents" || display === "none") return false;
    let things = 0;
    for (const node of el.childNodes) {
      if (node.nodeType === Node.TEXT_NODE ? node.data.trim() : node.nodeType === Node.ELEMENT_NODE && node.getClientRects().length && area(box(node)) > 0) things++;
      if (things > 1) return true;
    }
    return false;
  }

  // Wider: the smallest part or group around `el` that the eye sees as larger: one whose seen box holds `el`'s and
  // differs from it. Around a part in the page's structure, and also a part `el` is drawn over in the same layer
  // (a badge set on a card's corner widens to the card). The whole page past the last; null for the whole page.
  function wider(el, pickable, controls) {
    if (el === document.body) return null;
    const r = view(el);
    const found = [];
    for (let up = parent(el); up; up = parent(up)) if (isPart(up, pickable) || isGroup(up)) found.push(up);
    const page = el.ownerDocument;
    const own = el.getBoundingClientRect();
    const x = Math.min(Math.max(own.left + own.width / 2, 0), page.defaultView.innerWidth - 1);
    const y = Math.min(Math.max(own.top + own.height / 2, 0), page.defaultView.innerHeight - 1);
    const mine = layer(el);
    for (const under of page.elementsFromPoint(x, y)) {
      if (under === page.body || under === page.documentElement || holds(el, under) || under.contains(el)) continue;
      if (under.closest(controls) || layer(under) !== mine || !isPart(under, pickable)) continue;
      found.push(under);
    }
    let best = null;
    let bestArea = Infinity;
    for (const part of found) {
      const s = view(part);
      if (alike(s, r) || !within(r, s, 2)) continue;
      if (area(s) < bestArea) { best = part; bestArea = area(s); }
    }
    return best || document.body;
  }

  // The parts and groups the eye sees directly inside `el`: walking in from `el`, each one met that shows at
  // least half inside `el`'s box and differs from it; a wrapper the same size as `el`, or one mostly outside it, is
  // walked through. The picker's own controls are left out. `clipped` compares by the part of `el` in view
  // (Narrower goes into what the user sees); without it, by `el`'s whole box (Sideways also reaches the rows of a
  // list scrolled out of view).
  const INSIDE_LIMIT = 3000;
  function inside(el, pickable, controls, clipped) {
    const page = { left: 0, top: 0, right: innerWidth, bottom: innerHeight };
    const outerBox = el === document.body ? page : clipped ? view(el) : box(el);
    return walkIn(el, outerBox, controls, clipped, (child) => isPart(child, pickable) || isGroup(child));
  }

  // The smaller pieces inside `el` when no part or group is: any element the eye can tell from `el`, such as a
  // word set in bold, a link in a line or a shape in a drawing, so Narrower always has somewhere to go.
  function pieces(el, controls) {
    return walkIn(el, view(el), controls, true, () => true);
  }

  // A piece taller or wider than a scrolled panel it is in counts as the panel when what shows of it (`clipped`)
  // matches the panel's own seen box: stepping into it would change nothing the user sees.
  function walkIn(el, outerBox, controls, clipped, counts) {
    const found = [];
    let left = INSIDE_LIMIT;
    const walk = (node) => {
      for (const child of kids(node)) {
        if (--left < 0) return;
        if (child.closest(controls)) continue;
        if (!isSvg(child) && css(child).display === "contents") { walk(child); continue; }
        if (!shown(child)) continue;
        const s = box(child);
        const showing = area(cut(s, outerBox));
        if (area(s) > 0 && counts(child) && !alike(clipped ? view(child) : s, outerBox) && showing >= area(s) / 2) found.push(child);
        else walk(child);
      }
    };
    walk(el);
    return found;
  }

  // The gap between two parts' edges, nothing where they touch or overlap.
  function gap(a, b) {
    const r = box(a);
    const s = box(b);
    return Math.hypot(Math.max(0, s.left - r.right, r.left - s.right), Math.max(0, s.top - r.bottom, r.top - s.bottom));
  }

  // Reading order: top to bottom, then left to right, rows within 4 px counting as one.
  function readingOrder(a, b) {
    const r = box(a);
    const s = box(b);
    return Math.abs(r.top - s.top) > 4 ? r.top - s.top : r.left - s.left;
  }

  // Narrower: on the way back to `from`, the part the user clicked, while `el` lies on Wider's way from it; else
  // the part inside it that holds `from`; else the largest part inside it, the first in reading order among ones
  // of about that size. With no part inside, the same among its smaller pieces. Null with nothing inside at all.
  function narrower(el, from, pickable, controls) {
    if (!alive(el)) return null;
    const back = from && from !== el && alive(from) ? toward(el, from, pickable, controls) : null;
    if (back) return back;
    let list = inside(el, pickable, controls, true);
    if (!list.length) list = pieces(el, controls);
    if (!list.length) return null;
    const holding = from && alive(from) ? list.find((part) => part !== from && holds(part, from)) : null;
    if (holding) return holding;
    const largest = Math.max(...list.map((part) => area(view(part))));
    return list.filter((part) => area(view(part)) >= largest * 0.9).sort(readingOrder)[0];
  }

  // The part inside `el` on Wider's way out from `from`; null when that way does not pass `el`.
  const WAY_LIMIT = 100;
  function toward(el, from, pickable, controls) {
    if (!alive(el) || !alive(from) || el === from) return null;
    let part = from;
    for (let i = 0; i < WAY_LIMIT && part !== document.body; i++) {
      const next = wider(part, pickable, controls);
      if (next === el) return part;
      part = next;
    }
    return null;
  }

  // How alike two parts look: the same kind of element, a class in common, and a size within half to twice the
  // other's, one point each.
  function likeness(a, b) {
    const r = box(a);
    const s = box(b);
    const w = (r.right - r.left) / Math.max(1, s.right - s.left);
    const h = (r.bottom - r.top) / Math.max(1, s.bottom - s.top);
    const classes = (el) => [...el.classList].filter((name) => !name.startsWith("pick-"));
    const mine = classes(b);
    return (a.localName === b.localName ? 1 : 0) + (classes(a).some((name) => mine.includes(name)) ? 1 : 0)
      + (w >= 0.5 && w <= 2 && h >= 0.5 && h <= 2 ? 1 : 0);
  }

  // Sideways: the parts beside `el`, in the order Sideways visits them: the other parts directly inside the one
  // Wider gives that look like `el` in two ways, or in one way and stand within NEXT_TO of it (in the left column,
  // Priorities goes to the lines above and below it, not to the column's foot or the whole list of items), or,
  // when none does, any part within NEXT_TO;
  // when none of those looks like `el` in two ways, also from each part next to that one and like
  // it in two ways (the next card in a list, not a column across the page) the part in it most like `el` (the
  // same field in the next card). The most alike first, then the nearest by the gap between their edges (within
  // 4 px counting as the same), then reading order: in a row A B C from B, A then C.
  // Boxes are whole, so a row scrolled out of view still counts; `reveal` brings it in.
  const NEXT_TO = 48;
  function siblings(el, pickable, controls) {
    if (!alive(el)) return [];
    const up = wider(el, pickable, controls);
    if (!up) return [];
    const apart = (part) => part !== el && !holds(part, el) && !holds(el, part);
    const all = inside(up, pickable, controls, false).filter(apart);
    const near = all.filter((part) => likeness(part, el) >= 2 || (likeness(part, el) === 1 && gap(el, part) <= NEXT_TO));
    const list = near.length ? near : all.filter((part) => gap(el, part) <= NEXT_TO);
    if (!list.some((part) => likeness(part, el) >= 2) && up !== document.body) {
      const top = wider(up, pickable, controls);
      const cousins = top ? inside(top, pickable, controls, false)
        .filter((part) => part !== up && apart(part) && likeness(part, up) >= 2 && gap(up, part) <= NEXT_TO) : [];
      for (const part of cousins) {
        const like = likest(el, part, pickable, controls);
        if (like && !list.includes(like)) list.push(like);
      }
    }
    return list.map((part) => ({ part, like: likeness(part, el), far: gap(el, part) }))
      .sort((a, b) => b.like - a.like || (Math.abs(a.far - b.far) > 4 ? a.far - b.far : 0) || readingOrder(a.part, b.part))
      .map(({ part }) => part);
  }

  // The feedback box's steps by name, for a pick {el, from, side}: the next {part, from, side}, or null where there
  // is none. `from` is the part the user clicked, or last went Sideways to; Wider and Narrower keep it, so Narrower
  // can step back toward it. `side` is the list Sideways goes through: the parts beside the pick, then the pick
  // itself, so pressing on comes back round to it. The list lasts while the pick is the part Sideways last went to.
  // The page redraws often; each part stands for the one now in its place (now), so a round outlasts a redraw.
  // When some part of the round has gone, a new round starts from the part it started from, still last, so
  // Sideways still comes back to it; from the pick when that has gone too.
  function step(name, pick, pickable, controls) {
    const el = now(pick.el);
    // A part with nothing in its place any more has nothing to step from.
    if (!el) return null;
    const from = pick.from && now(pick.from);
    if (name === "wider" || name === "narrower") {
      const part = name === "wider" ? wider(el, pickable, controls) : narrower(el, from, pickable, controls);
      return part && { part, from, side: pick.side };
    }
    if (name !== "sideways") throw new Error(`no step named ${name}`);
    let side = null;
    // The round goes on while the pick is the part Sideways last went to; Wider or Narrower since starts a new one.
    if (pick.side && pick.side.list[pick.side.index] === pick.el) {
      const list = pick.side.list.map(now);
      const start = list[list.length - 1];
      if (list.every(Boolean)) side = { list, index: pick.side.index };
      else if (start) {
        const round = [...siblings(start, pickable, controls), start];
        side = { list: round, index: round.indexOf(el) };
      }
    }
    if (!side) side = { list: [...siblings(el, pickable, controls), el], index: -1 };
    side.list.forEach(now);
    if (side.list.length < 2) return null;
    const index = (side.index + 1) % side.list.length;
    return { part: side.list[index], from: side.list[index], side: { list: side.list, index } };
  }

  // Brings the part a step went to into view, when less than half of it shows and it fits the window.
  function reveal(el) {
    if (el === document.body || !alive(el)) return;
    const r = box(el);
    const fits = r.right - r.left <= innerWidth && r.bottom - r.top <= innerHeight;
    if (fits && area(seen(el)) < area(r) / 2) el.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  // The part inside `where`, it included, that looks most like `el`, if any looks alike in two ways or more.
  function likest(el, where, pickable, controls) {
    let best = likeness(where, el) >= 2 ? where : null;
    let bestLike = best ? likeness(where, el) : 1;
    const queue = [where];
    for (let seenParts = 0; queue.length && seenParts < 500; seenParts++) {
      for (const part of inside(queue.shift(), pickable, controls, false)) {
        const like = likeness(part, el);
        if (like > bestLike) { best = part; bestLike = like; }
        queue.push(part);
      }
    }
    return best;
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
  const clips = (el) => css(el).overflow !== "visible";
  const overlaps = (a, b) => a.width > 0 && a.height > 0 && a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;

  // The words of a part the user sees on the page: hidden text, a plate's, and text past the edge of a
  // box that cuts it off are left out.
  function seenText(el) {
    const pieces = [];
    const walker = el.ownerDocument.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    const range = el.ownerDocument.createRange();
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
  // copy, empty and unseen. A part that would shrink past a quarter of its size (a whole section) is shown at a
  // quarter, cut to its top left corner (`cut`), and what lies outside that corner is left empty. Null for the
  // whole page and for a part with too much to copy, so the box shows the part's words instead; `small` marks a
  // copy under half size, too small to read. The copy is inert: no ids, keys or names of its own.
  const SKIP = /^(transition|animation|anchor-name|position-anchor|view-transition|cursor|pointer-events)/;
  const VOID = /^(input|img|br|hr|textarea|select|iframe|video|audio|canvas|svg|embed|object|meter|progress)$/;
  function copy(el, width, height) {
    if (el === document.body) return null;
    const box = el.getBoundingClientRect();
    if (!box.width || !box.height) return null;
    const scale = Math.max(0.25, Math.min(1, width / box.width, height / box.height));
    const shownWidth = Math.min(width, box.width * scale);
    const shownHeight = Math.min(height, box.height * scale);
    const corner = { left: box.left, top: box.top, right: box.left + shownWidth / scale, bottom: box.top + shownHeight / scale };
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
      const rect = original.getBoundingClientRect();
      const outside = rect.width > 0 && rect.height > 0 && !overlaps(rect, corner);
      if (original !== el && (!shown(original) || original.matches(PLATES) || outside)) {
        hidden.add(original);
        const target = clones[twins.get(original)];
        target.replaceChildren();
        target.style.setProperty("visibility", "hidden");
      }
      return true;
    });
    if (originals.filter((original) => !hidden.has(original)).length > 600) return null;
    originals.forEach((original) => {
      const style = css(original);
      const target = clones[twins.get(original)];
      for (const name of style) if (!SKIP.test(name)) target.style.setProperty(name, style.getPropertyValue(name));
      for (const name of ["id", "data-key", "name", "for", "tabindex", "autofocus"]) target.removeAttribute(name);
      if (target.classList) target.classList.remove("pick-hover", "pick-chosen");
      if (secret(original)) {
        target.removeAttribute("value");
        target.value = "";
      } else if ("value" in original && typeof original.value === "string") target.value = original.value;
      if (original.localName === "canvas" && !hidden.has(original)) drawCanvas(original, target);
      if (hidden.has(original)) target.style.setProperty("visibility", "hidden");
    });
    // A dot, a tick or a count the page draws before or after a part's content comes along as an element of its
    // own, styled as drawn. The copy drops the page's classes, so no rule draws them a second time where the copy
    // stands, and its frozen styles alone set its look.
    originals.forEach((original) => {
      const target = clones[twins.get(original)];
      target.removeAttribute("class");
      if (hidden.has(original) || VOID.test(original.localName)) return;
      for (const side of ["::before", "::after"]) {
        const style = original.ownerDocument.defaultView.getComputedStyle(original, side);
        if (style.content === "none" || style.content === "normal" || style.display === "none") continue;
        const drawn = document.createElement("span");
        for (const name of style) if (!SKIP.test(name) && name !== "content") drawn.style.setProperty(name, style.getPropertyValue(name));
        drawn.textContent = [...style.content.matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((match) => match[1].replace(/\\(.)/g, "$1")).join("");
        if (side === "::before") target.prepend(drawn);
        else target.append(drawn);
      }
    });
    // A part set inside a line takes its own box, so the size below holds.
    if (css(el).display === "inline") clone.style.setProperty("display", "inline-block");
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
    const cutRight = shownWidth < box.width * scale - 1;
    const cutFoot = shownHeight < box.height * scale - 1;
    frame.className = `pick-copy${scale < 0.5 ? " small" : ""}${cutRight ? " cut-right" : ""}${cutFoot ? " cut-foot" : ""}`;
    frame.inert = true;
    frame.setAttribute("aria-hidden", "true");
    frame.style.width = `${Math.ceil(shownWidth)}px`;
    frame.style.height = `${Math.ceil(shownHeight)}px`;
    frame.append(clone);
    return frame;
  }

  // A canvas copies empty, so the copy draws what the page's canvas shows; one the page drew from another site
  // cannot be read and stays empty.
  function drawCanvas(original, target) {
    target.width = original.width;
    target.height = original.height;
    try {
      target.getContext("2d").drawImage(original, 0, 0);
    } catch {
      // A canvas in WebGL with nothing kept to draw, or one the browser will not share, stays empty.
    }
  }

  // The colour the part stands on: the nearest opaque background at or behind it. A tint (a well's
  // translucent wash) is passed over for the colour under it, which sets how the part reads.
  function ground(el) {
    for (let part = el; part; part = part.parentElement || frameOf(part)) {
      const colour = css(part).backgroundColor;
      const alpha = colour.match(/(?:,|\/)\s*([\d.]+)(%?)\)$/);
      if (!alpha || Number(alpha[1]) >= (alpha[2] ? 100 : 1)) return colour;
    }
    return "var(--bg)";
  }

  // The path of the framed page an element is in, such as "/usage"; null for an element of this page.
  const framedPath = (el) => (el.ownerDocument === document ? null : el.ownerDocument.location.pathname);

  // While picking, a part that acts on a press or on the pointer coming over it does neither: the press, the
  // double click and the pointer's arrival reach none of the page's own handlers, so a calendar event does not
  // start to move, a grip does not start a drag, and the part stays as the user sees it until the click picks it.
  // These run first (capture on the window) and leave the picker's own controls and its click alone; a page
  // follows the pointer for its hover mark from a handler it adds on the window before this one. The pointer
  // leaving a part still reaches the page, so what opened before picking still closes. A press neither moves the
  // focus (an input box would take it) nor starts a selection, and a middle click opens no link.
  // `picking` and `controls` are functions: the page's state now, and its controls' selector now.
  const HELD = ["pointerdown", "pointerup", "mousedown", "mouseup", "dblclick", "auxclick", "touchstart", "touchend",
    "pointerover", "pointerenter", "mouseover", "mouseenter", "pointermove", "mousemove", "dragstart"];
  //
  // Enter and Space on a focused part reach none of the page's own key handlers either (a row opens on Enter):
  // on a button, a link or a box to tick, the browser's own click follows and picks; on any other part the key
  // picks it with a click from the keyboard. Space still types in a box for text, and Enter there picks the box
  // rather than sending its form.
  const ACTIVATES = "button, a[href], summary, input:is([type=checkbox], [type=radio], [type=submit], [type=button], [type=reset], [type=image], [type=file], [type=color])";
  const TYPING = "textarea, select, input";
  function holdPage(picking, controls) {
    const hold = (event) => {
      if (!picking() || !(event.target instanceof Element) || event.target.closest(controls())) return;
      if (["mousedown", "auxclick", "dragstart"].includes(event.type)) event.preventDefault();
      event.stopImmediatePropagation();
    };
    for (const type of HELD) window.addEventListener(type, hold, true);
    const holdKey = (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      const el = event.target;
      if (!picking() || !(el instanceof Element) || el.closest(controls())) return;
      event.stopImmediatePropagation();
      if (event.type !== "keydown" || el.matches(ACTIVATES)) return;
      if (el.isContentEditable || (el.matches(TYPING) && (event.key === " " || el.localName !== "input"))) return;
      event.preventDefault();
      if (!event.repeat) el.click();
    };
    for (const type of ["keydown", "keypress", "keyup"]) window.addEventListener(type, holdKey, true);
  }

  // How a session finds the part again, beyond its target and words, most telling first: its element, its
  // data and labels, its state, the heading it sits under, its place among parts like it, the page (and the
  // framed page) it is on, where it sits on the screen in which theme, and last its place in the page's structure.
  const DESCRIBE_LIMIT = 2000;
  const STATES = ["aria-selected", "aria-expanded", "aria-checked", "aria-pressed", "aria-current", "aria-disabled",
    "disabled", "checked", "open", "hidden"];
  function describe(el) {
    const theme = document.documentElement.dataset.theme
      || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const shape = `window ${innerWidth}×${innerHeight}, ${theme} theme`;
    const address = `page ${location.pathname}${location.search}${location.hash}`;
    if (el === document.body) return `the whole page · ${address} · ${shape}`;
    const classes = (node, most) => [...node.classList].filter((name) => !name.startsWith("pick-")).slice(0, most)
      .map((name) => `.${CSS.escape(name)}`).join("");
    const pieces = [`${el.localName}${el.id ? `#${CSS.escape(el.id)}` : ""}${classes(el, 4)}`];
    const data = [...el.attributes].filter((attr) => attr.name.startsWith("data-")).slice(0, 8)
      .map((attr) => `${attr.name}="${attr.value.slice(0, 60)}"`);
    if (data.length) pieces.push(data.join(" "));
    for (const name of ["role", "aria-label", "title", "type", "name", "placeholder"]) {
      const value = el.getAttribute(name);
      if (value) pieces.push(`${name}="${value.slice(0, 120)}"`);
    }
    if (el.localName === "a" && el.href) {
      const link = new URL(el.href, el.baseURI);
      pieces.push(`href ${link.origin === location.origin ? link.pathname + link.search + link.hash : link.href}`);
    }
    const states = STATES.filter((name) => el.hasAttribute(name)).map((name) => {
      const value = el.getAttribute(name);
      return value ? `${name}="${value.slice(0, 20)}"` : name;
    });
    if (states.length) pieces.push(`state ${states.join(" ")}`);
    const heading = headingOver(el);
    if (heading) pieces.push(`${heading.how} "${heading.text}"`);
    const place = placeAmong(el);
    if (place) pieces.push(place);
    pieces.push(address);
    const framed = framedPath(el);
    if (framed) pieces.push(`in the framed page ${framed}`);
    const r = box(el);
    pieces.push(`at ${Math.round(r.left)},${Math.round(r.top)} size ${Math.round(r.right - r.left)}×${Math.round(r.bottom - r.top)} in a ${shape}`);
    const steps = [];
    for (let node = el; node && node !== node.ownerDocument.body && steps.length < 10; node = node.parentElement) {
      if (node.id) { steps.unshift(`#${CSS.escape(node.id)}`); break; }
      const same = node.parentElement ? [...node.parentElement.children].filter((other) => other.localName === node.localName) : [node];
      steps.unshift(`${node.localName}${classes(node, 2)}${same.length > 1 ? `:nth-of-type(${same.indexOf(node) + 1})` : ""}`);
    }
    pieces.push(`path ${steps.join(" > ")}`);
    const text = pieces.join(" · ");
    return text.length > DESCRIBE_LIMIT ? `${text.slice(0, DESCRIBE_LIMIT - 1)}…` : text;
  }

  // The heading that names the part: its own heading, the first inside it (a card's title); else the heading it
  // sits under, the last one before it in an element around it, walking out. A heading inside a neighbour (the
  // next card's title) names that neighbour, so it does not count.
  const HEADINGS = "h1, h2, h3, h4, h5, h6, [role=heading], legend, summary";
  function headingOver(el) {
    const words = (heading) => heading.textContent.trim().replace(/\s+/g, " ").slice(0, 80);
    if (!el.matches(HEADINGS)) {
      const own = el.querySelector(HEADINGS);
      if (own && words(own)) return { how: "with the heading", text: words(own) };
    }
    for (let node = el.parentElement; node && node !== node.ownerDocument.body; node = node.parentElement) {
      const over = [...node.querySelectorAll(HEADINGS)].filter((heading) => heading !== el && heading.parentElement.contains(el)
        && heading.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING).pop();
      if (over && words(over)) return { how: "under the heading", text: words(over) };
    }
    return null;
  }

  // Which of the parts like it the part is, such as "row 3 of 7", where its parent holds others of its element
  // and first class.
  function placeAmong(el) {
    const up = el.parentElement;
    if (!up) return null;
    const kind = [...el.classList].find((name) => !name.startsWith("pick-"));
    const like = [...up.children].filter((other) => other.localName === el.localName && (!kind || other.classList.contains(kind)));
    return like.length > 1 ? `${like.indexOf(el) + 1} of ${like.length} ${el.localName}${kind ? `.${CSS.escape(kind)}` : ""}` : null;
  }

  // Marks drawn over the page: a box around a part with its name on a label. They sit outside the body, so
  // no walk over the page meets them, and the pointer passes through them. Each follows its part as the page
  // scrolls or the window changes, and every quarter second for changes on the page and scrolls inside a framed
  // page, and hides while its part is out of view, is the whole page, or has gone.
  // `name` tells marks apart ("hover", "chosen"); a null `el` takes that mark away.
  const marks = new Map();
  let docked = null;
  let following = null;
  function mark(name, el, label) {
    let made = marks.get(name);
    if (!el) {
      if (made) made.node.remove();
      marks.delete(name);
      follow();
      return;
    }
    if (!made) {
      const node = document.createElement("div");
      node.className = `pick-mark ${name}`;
      node.setAttribute("aria-hidden", "true");
      const tag = document.createElement("span");
      tag.className = "pick-mark-label";
      node.append(tag);
      document.documentElement.append(node);
      made = { node, tag };
      marks.set(name, made);
    }
    made.el = el;
    made.tag.textContent = label;
    place(made);
    follow();
  }

  function place(made) {
    made.el = now(made.el) || made.el;
    const r = alive(made.el) && made.el !== document.body ? seen(made.el) : null;
    if (!r || area(r) === 0) {
      made.node.hidden = true;
      return;
    }
    made.node.hidden = false;
    for (const [name, value] of [["left", r.left], ["top", r.top], ["width", r.right - r.left], ["height", r.bottom - r.top]]) {
      made.node.style.setProperty(name, `${value}px`);
    }
    // The label sits above the box, or just inside its top where the window's edge leaves no room.
    made.node.classList.toggle("inside", r.top < 26);
  }

  // The feedback box stands beside the chosen part, so the part stays in plain sight while the user writes: to
  // its right, else its left, else below or above it, else in the window's corner that covers least of it. Once
  // placed it stays put while it still fits and covers none of the part, so a step to the next part or a scroll
  // moves it only when it must.
  // `top` is the highest the box may go (below the header and the pick bar). Below 640 px it keeps its place as a
  // sheet at the foot of the screen (style.css). A null `el` returns the box to its own place. One box stands
  // docked at a time (the feedback box or the Add box); taking one box back leaves the other where it is.
  const GAP = 12;
  const MARGIN = 16;
  const DOCK_WIDTH = 640;
  function dock(panel, el, top) {
    if (!el) {
      undock(panel);
      if (docked && docked.panel === panel) docked = null;
    } else {
      const at = docked && docked.panel === panel ? docked.at : null;
      docked = { panel, el, top, at };
      placeDock();
    }
    follow();
  }

  function undock(panel) {
    for (const name of ["left", "top", "right", "bottom"]) panel.style.removeProperty(name);
    panel.classList.remove("docked");
    if (docked && docked.panel === panel) docked.at = null;
  }

  function placeDock() {
    docked.el = now(docked.el) || docked.el;
    const { panel, el, top, at } = docked;
    const r = alive(el) && el !== document.body && innerWidth >= DOCK_WIDTH ? seen(el) : null;
    if (!r || area(r) === 0 || panel.hidden) {
      undock(panel);
      return;
    }
    const w = panel.offsetWidth;
    const h = panel.offsetHeight;
    const maxX = innerWidth - MARGIN - w;
    const maxY = innerHeight - MARGIN - h;
    if (at && at[0] >= MARGIN && at[0] <= maxX && at[1] >= top && at[1] <= maxY
      && area(cut(r, { left: at[0], top: at[1], right: at[0] + w, bottom: at[1] + h })) === 0) return;
    const clamp = (value, low, high) => Math.max(low, Math.min(value, Math.max(low, high)));
    const spots = [
      [r.right + GAP, clamp(r.top, top, maxY), r.right + GAP <= maxX],
      [r.left - GAP - w, clamp(r.top, top, maxY), r.left - GAP - w >= MARGIN],
      [clamp(r.left, MARGIN, maxX), r.bottom + GAP, r.bottom + GAP <= maxY],
      [clamp(r.left, MARGIN, maxX), r.top - GAP - h, r.top - GAP - h >= top],
    ];
    let spot = spots.find(([, , fits]) => fits);
    if (!spot) {
      const covered = ([x, y]) => area(cut(r, { left: x, top: y, right: x + w, bottom: y + h }));
      spot = [[maxX, maxY], [MARGIN, maxY], [maxX, top], [MARGIN, top]].sort((a, b) => covered(a) - covered(b))[0];
    }
    docked.at = [Math.round(spot[0]), Math.round(spot[1])];
    panel.classList.add("docked");
    panel.style.setProperty("left", `${docked.at[0]}px`);
    panel.style.setProperty("top", `${docked.at[1]}px`);
    panel.style.setProperty("right", "auto");
    panel.style.setProperty("bottom", "auto");
  }

  // Starts following while a mark or the docked box is out, and stops once none is.
  const FOLLOW_MS = 250;
  function follow() {
    const wanted = marks.size > 0 || docked !== null;
    if (wanted === Boolean(following)) return;
    if (!wanted) {
      following();
      following = null;
      return;
    }
    let queued = 0;
    const update = () => {
      queued = 0;
      for (const made of marks.values()) place(made);
      if (docked) placeDock();
    };
    const soon = () => { if (!queued) queued = requestAnimationFrame(update); };
    const timer = setInterval(update, FOLLOW_MS);
    addEventListener("scroll", soon, { capture: true, passive: true });
    addEventListener("resize", soon);
    following = () => {
      clearInterval(timer);
      cancelAnimationFrame(queued);
      removeEventListener("scroll", soon, { capture: true });
      removeEventListener("resize", soon);
    };
  }

  return {
    at, of, guesses, wider, narrower, toward, siblings, step, reveal, alive, now, words, describe, copy, ground, framedPath,
    holdPage, mark, dock,
  };
})();
