"use strict";

// Added to a shared page that Cockpit shows for comments (SPEC NEXT-38; app.py inlines it after the page's HTML).
// It runs in the page's sandbox beside the page's own scripts, which can send Cockpit's page the same messages, so
// all it does is propose and draw: it tells Cockpit's page what the user selected or picked, passes on the P and
// Escape keys, and draws the marks Cockpit's page sends. The comment box, its text, Send and Add stay in Cockpit's
// page (page-comments.js), which checks each proposal before it shows it.
//
// A mark on words is found again by its quote: the one place the words stand on the page, or, where they stand
// more than once, the one place whose words around them match too. A picked part is found by its CSS path, and only
// while it shows the same text. Anything else counts as not found, so a mark never lands on the wrong words.
(() => {
  const CONTEXT = 30;
  const QUOTE_MAX = 2000;
  const SKIP = "script, style, noscript, template, textarea";
  const TONES = ["open", "sent", "active"];
  const post = (message) => parent.postMessage({ cockpitPage: message }, "*");
  // The marks Cockpit's page sent: [{id, anchor, tone}], each with its `range` or `element` once found.
  let marks = [];
  let picking = false;
  let hovered = null;
  // Boxes around picked parts and around the part under the pointer, outside the body, so the page's own
  // scripts that rebuild the body leave them alone and the mutation observer ignores them.
  const layer = document.createElement("div");
  layer.style.cssText = "position:absolute;left:0;top:0;width:0;height:0;pointer-events:none;z-index:2147483647";

  const sheet = new CSSStyleSheet();
  sheet.replaceSync(
    "::highlight(cockpit-open){background:rgba(250,204,21,.42)}"
    + "::highlight(cockpit-sent){background:rgba(140,140,140,.28)}"
    + "::highlight(cockpit-active){background:rgba(245,158,11,.62)}"
    + "html[data-cockpit-picking] *{cursor:crosshair!important}",
  );
  document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];

  // The page's text with each run of white space as one space, and per character its text node and offset.
  function textIndex() {
    const chars = [];
    const where = [];
    let space = true;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode: (node) => (node.parentElement && node.parentElement.closest(SKIP) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
    });
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const value = node.nodeValue;
      for (let offset = 0; offset < value.length; offset++) {
        const blank = /\s/.test(value[offset]);
        if (blank && space) continue;
        space = blank;
        chars.push(blank ? " " : value[offset]);
        where.push([node, offset]);
      }
    }
    return { text: chars.join(""), where };
  }

  // The first index in `where` for which `after` holds, which is false for all before it and true from it on.
  function firstWhere(where, after) {
    let low = 0;
    let high = where.length;
    while (low < high) {
      const middle = (low + high) >> 1;
      if (after(where[middle])) high = middle;
      else low = middle + 1;
    }
    return low;
  }

  function rangeAt(index, start, end) {
    const range = document.createRange();
    range.setStart(index.where[start][0], index.where[start][1]);
    range.setEnd(index.where[end - 1][0], index.where[end - 1][1] + 1);
    return range;
  }

  // The selection as an anchor and its box on the screen, or null when nothing on the page is selected.
  function selected() {
    const selection = getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) return null;
    const range = selection.getRangeAt(0);
    if (!document.body.contains(range.commonAncestorContainer)) return null;
    const index = textIndex();
    let start = firstWhere(index.where, ([node, offset]) => range.comparePoint(node, offset) >= 0);
    let end = firstWhere(index.where, ([node, offset]) => range.comparePoint(node, offset + 1) > 0);
    while (start < end && index.text[start] === " ") start++;
    while (end > start && index.text[end - 1] === " ") end--;
    if (start >= end || end - start > QUOTE_MAX) return null;
    const box = range.getBoundingClientRect();
    return {
      anchor: {
        kind: "text",
        quote: index.text.slice(start, end),
        prefix: index.text.slice(Math.max(0, start - CONTEXT), start),
        suffix: index.text.slice(end, end + CONTEXT),
      },
      box: { top: box.top, bottom: box.bottom, left: box.left, right: box.right },
    };
  }

  // Where the words stand once, the words just before or just after them must match too, so words that left
  // their place for another one that says the same count as gone; where they stand more than once, the whole
  // context on both sides picks the one. No words before (or after) them means they started (or ended) the page.
  const NEAR = 12;
  function findText(index, anchor) {
    const hits = [];
    for (let at = index.text.indexOf(anchor.quote); at !== -1; at = index.text.indexOf(anchor.quote, at + 1)) hits.push(at);
    const end = (at) => at + anchor.quote.length;
    const before = (at, words) => (words ? index.text.slice(0, at).endsWith(words) : at === 0);
    const after = (at, words) => (words ? index.text.slice(end(at)).startsWith(words) : end(at) === index.text.length);
    if (hits.length === 1) {
      const at = hits[0];
      return before(at, anchor.prefix.slice(-NEAR)) || after(at, anchor.suffix.slice(0, NEAR)) ? at : -1;
    }
    const fitting = hits.filter((at) => before(at, anchor.prefix) && after(at, anchor.suffix));
    return fitting.length === 1 ? fitting[0] : -1;
  }

  // The words a part shows, by which it is found again: a chart's label when it has one, an image's alt text or
  // file name, else its text.
  const partText = (element) => {
    let words = element.textContent;
    if (element.localName === "svg") words = element.getAttribute("aria-label") || element.querySelector(":scope > title")?.textContent || words;
    else if (element.localName === "img") words = `Image: ${[element.alt, imageName(element.getAttribute("src") || "")].filter(Boolean).join(", ")}`;
    return words.replace(/\s+/g, " ").trim().slice(0, QUOTE_MAX);
  };
  // An image's address, or for an image inside the page a short fingerprint of its data.
  function imageName(src) {
    if (!src.startsWith("data:")) return src.split(/[?#]/)[0].slice(-200);
    let hash = 0;
    for (let at = 0; at < src.length; at++) hash = (hash * 31 + src.charCodeAt(at)) | 0;
    return `embedded ${(hash >>> 0).toString(16)}`;
  }

  // The part a click picks: a whole chart, an image, a table row, a list entry or the nearest block.
  function partOf(element) {
    let svg = element.closest("svg");
    while (svg && svg.parentElement && svg.parentElement.closest("svg")) svg = svg.parentElement.closest("svg");
    if (svg) return svg;
    const part = element.closest("img, canvas, video, tr, li, p, h1, h2, h3, h4, h5, h6, pre, blockquote, dt, dd, figure, table, div, section, article");
    return part && part !== document.body ? part : null;
  }

  function cssPath(element) {
    const steps = [];
    for (let el = element; el && el !== document.body && el !== document.documentElement; el = el.parentElement) {
      if (el.id && document.querySelectorAll(`#${CSS.escape(el.id)}`).length === 1) {
        steps.unshift(`#${CSS.escape(el.id)}`);
        return steps.join(" > ");
      }
      const tag = el.localName;
      const same = [...el.parentElement.children].filter((child) => child.localName === tag);
      steps.unshift(same.length > 1 ? `${tag}:nth-of-type(${same.indexOf(el) + 1})` : tag);
    }
    return `body > ${steps.join(" > ")}`;
  }

  function findPart(anchor) {
    let element = null;
    try {
      element = document.querySelector(anchor.path);
    } catch {
      return null;
    }
    return element && partText(element) === anchor.text ? element : null;
  }

  function box(element, tone) {
    const rect = element.getBoundingClientRect();
    const el = document.createElement("div");
    const colors = { open: "rgba(234,88,12,.9)", sent: "rgba(140,140,140,.75)", active: "rgba(234,88,12,1)", pick: "rgba(37,99,235,.95)" };
    el.style.cssText = `position:absolute;left:${rect.left + scrollX - 3}px;top:${rect.top + scrollY - 3}px;`
      + `width:${rect.width + 6}px;height:${rect.height + 6}px;border:2px ${tone === "sent" ? "dashed" : "solid"} ${colors[tone]};`
      + `border-radius:4px;box-sizing:border-box;${tone === "active" ? "box-shadow:0 0 0 4px rgba(234,88,12,.25);" : ""}`;
    return el;
  }

  // Finds every mark on the page as it is now and draws it; tells Cockpit's page which ones it found.
  function draw() {
    const index = marks.some((mark) => mark.anchor.kind === "text") ? textIndex() : null;
    const tones = Object.fromEntries(TONES.map((tone) => [tone, []]));
    const boxes = [];
    const found = [];
    for (const mark of marks) {
      mark.range = null;
      mark.element = null;
      if (mark.anchor.kind === "text") {
        const at = findText(index, mark.anchor);
        if (at === -1) continue;
        mark.range = rangeAt(index, at, at + mark.anchor.quote.length);
        tones[mark.tone].push(mark.range);
      } else {
        mark.element = findPart(mark.anchor);
        if (!mark.element) continue;
        boxes.push(box(mark.element, mark.tone));
      }
      found.push(mark.id);
    }
    if (window.CSS && CSS.highlights) {
      for (const tone of TONES) CSS.highlights.set(`cockpit-${tone}`, new Highlight(...tones[tone]));
    }
    if (hovered) boxes.push(box(hovered, "pick"));
    layer.replaceChildren(...boxes);
    if (!layer.isConnected) document.documentElement.append(layer);
    // In the order they sit on the page, which the comment column follows.
    const start = (mark) => {
      if (mark.range) return mark.range;
      const range = document.createRange();
      range.selectNode(mark.element);
      return range;
    };
    const placed = found.map((id) => marks.find((mark) => mark.id === id));
    placed.sort((a, b) => start(a).compareBoundaryPoints(Range.START_TO_START, start(b)));
    post({ type: "found", ids: placed.map((mark) => mark.id), looked: marks.map((mark) => mark.id) });
  }

  let drawTimer = 0;
  const drawSoon = () => {
    clearTimeout(drawTimer);
    drawTimer = setTimeout(draw, 150);
  };

  function setPicking(on) {
    picking = on;
    hovered = null;
    document.documentElement.toggleAttribute("data-cockpit-picking", on);
    draw();
  }

  const inside = (point, rect) => point.clientX >= rect.left && point.clientX <= rect.right && point.clientY >= rect.top && point.clientY <= rect.bottom;

  function markAt(event) {
    for (const mark of marks) {
      if (mark.range && [...mark.range.getClientRects()].some((rect) => inside(event, rect))) return mark;
      if (mark.element && inside(event, mark.element.getBoundingClientRect())) return mark;
    }
    return null;
  }

  // While picking, a press picks the part under it and does nothing else on the page. The pick comes on the
  // press's end rather than its click, as a tap on plain text sends no click in Safari on the iPhone; the rest of
  // that gesture on the same spot is swallowed too (its click, a double click's second press and the double click
  // itself), so a link or a double-click action in the picked part stays put, while a new press elsewhere works.
  // A press that starts a new gesture ends that one, so a later click on the same spot works too. Enter or Space
  // on a focused control picks it on the key's way down, before the page's own key handlers, which never see that
  // key, and the key's release is held back too (`heldKey`); a field being typed in keeps its keys. A click with
  // no press and no key (its `detail` is 0, as a screen reader's) picks its control too; a click the page's own
  // script sends picks nothing. A touch's own events are held back from the
  // page's handlers too, but not cancelled, so the user can still scroll the page while picking.
  // The page's head holds the first listeners (PAGE_PICK_GUARD_SCRIPT, app.py), so they run before any the page's
  // own scripts add; they call `onPress`.
  let picked = null;
  let heldKey = null;
  const onKey = (type, event) => {
    if (type === "keyup") {
      if (event.key !== heldKey) return;
      heldKey = null;
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    // A held key that repeats goes on being held back, also once picking ended with the first press, and while
    // other keys go down beside it; a fresh press of it is a new one, also when its release went to another window
    // (the comment box) and never came here.
    if (event.key === heldKey) {
      if (event.repeat) {
        event.preventDefault();
        event.stopImmediatePropagation();
        return;
      }
      heldKey = null;
    }
    const target = event.target;
    const typing = target.closest && target.closest("input:not([type=button],[type=submit],[type=reset],[type=checkbox],[type=radio],[type=image]), textarea, select") || target.isContentEditable;
    if (!picking || !event.isTrusted || (event.key !== "Enter" && event.key !== " ") || typing
      || target === document.body || target === document.documentElement) return;
    // The activity listener below comes after this one, so a key held back from the page counts here.
    activity();
    event.preventDefault();
    event.stopImmediatePropagation();
    heldKey = event.key;
    pick(target);
  };
  const onPress = (type, event) => {
    if (type === "keydown" || type === "keyup") {
      onKey(type, event);
      return;
    }
    const touch = type === "touchstart" || type === "touchend";
    const point = touch ? event.changedTouches[0] : event;
    const pressed = type === "pointerdown" || type === "mousedown" || type === "touchstart";
    const after = picked && Date.now() - picked.at < (type === "click" || type === "dblclick" ? 1000 : 600)
      && Math.hypot(point.clientX - picked.x, point.clientY - picked.y) <= 10;
    if (!picking && !after) {
      if (pressed) picked = null;
      return;
    }
    if (picking && !event.isTrusted) return;
    // The activity listener below comes after this one, so a press held back from the page counts here.
    if (type === "pointerdown") activity();
    if (!touch) event.preventDefault();
    event.stopImmediatePropagation();
    const byKey = type === "click" && event.detail === 0;
    if (!picking || (type !== "pointerup" && !byKey)) return;
    picked = byKey ? null : { at: Date.now(), x: event.clientX, y: event.clientY };
    pick(byKey ? event.target : partOf(event.target));
  };
  const pick = (part) => {
    setPicking(false);
    // A press on no part still ends the pick, so the column's Pick a part comes up again.
    if (!part) {
      post({ type: "part", anchor: null });
      return;
    }
    const rect = part.getBoundingClientRect();
    post({
      type: "part",
      anchor: { kind: "part", path: cssPath(part), text: partText(part) },
      box: { top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right },
    });
  };
  window.__cockpitPress(onPress);
  // The user's input on the page, the mouse moving, the wheel, a key or a press, at most twice a second: Cockpit
  // counts it as activity for the step that opened the page (SPEC TRACK-5). Events a script made do not count; the
  // page's own scripts can still post the message, which only keeps that step's timer running.
  let activityAt = 0;
  let pointerX = null;
  let pointerY = null;
  const activity = () => {
    const now = Date.now();
    if (now - activityAt < 500) return;
    activityAt = now;
    post({ type: "activity" });
  };
  addEventListener("pointermove", (event) => {
    if (!event.isTrusted || (event.screenX === pointerX && event.screenY === pointerY)) return;
    pointerX = event.screenX;
    pointerY = event.screenY;
    activity();
  }, { capture: true, passive: true });
  for (const type of ["wheel", "keydown", "pointerdown"]) {
    addEventListener(type, (event) => {
      if (event.isTrusted) activity();
    }, { capture: true, passive: true });
  }
  addEventListener("pointermove", (event) => {
    if (!picking) return;
    const part = partOf(event.target);
    if (part === hovered) return;
    hovered = part;
    draw();
  }, true);
  addEventListener("click", (event) => {
    if (picking || !getSelection().isCollapsed) return;
    const mark = markAt(event);
    if (mark) post({ type: "mark", id: mark.id });
  });

  let selectTimer = 0;
  const reportSelection = () => {
    clearTimeout(selectTimer);
    selectTimer = setTimeout(() => post({ type: "select", ...(selected() || { anchor: null, box: null }) }), 120);
  };
  document.addEventListener("selectionchange", reportSelection);
  addEventListener("scroll", () => {
    if (!getSelection().isCollapsed) reportSelection();
  }, { passive: true });

  addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (picking) setPicking(false);
      return;
    }
    const typing = event.target.closest && event.target.closest("input, textarea, select, [contenteditable]");
    if ((event.key === "p" || event.key === "P") && !typing && !event.metaKey && !event.ctrlKey && !event.altKey) post({ type: "key", key: "p" });
  }, true);
  // Escape goes to Cockpit once the page had its turn: a page that closes something of its own with it (and calls
  // preventDefault) keeps it, so the layer over Cockpit stays open (SPEC NEXT-47).
  addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !event.defaultPrevented) post({ type: "key", key: "Escape" });
  });

  addEventListener("message", (event) => {
    if (event.source !== parent || !event.data || typeof event.data.cockpitComments !== "object") return;
    const message = event.data.cockpitComments;
    if (message.type === "marks") {
      marks = message.marks;
      draw();
    } else if (message.type === "pick") {
      setPicking(Boolean(message.on));
    } else if (message.type === "show") {
      const mark = marks.find((entry) => entry.id === message.id);
      const target = mark && (mark.element || (mark.range && mark.range.startContainer.parentElement));
      if (target) target.scrollIntoView({ block: "center", behavior: "smooth" });
    } else if (message.type === "unselect") {
      getSelection().removeAllRanges();
    }
  });

  const start = () => {
    new MutationObserver(drawSoon).observe(document.body, { childList: true, subtree: true, characterData: true });
    addEventListener("resize", drawSoon);
    post({ type: "ready" });
  };
  if (document.body) start();
  else addEventListener("DOMContentLoaded", start);
})();
