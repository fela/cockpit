"use strict";

// How Cockpit's pages move: the board, Briefing, Pages, Spec and Usage load it
// before their own script. It gives every `title` one custom tooltip (with the exact
// local time for elements that carry `data-time`, and keycaps for a control's
// `data-key` shortcut), presses buttons, opens menus, toasts, the Add box and the
// lightbox with a short entrance, fades images in as they load, animates steps and
// rows when a redraw adds, removes or reorders them, and tints for a moment what a
// poll changed: app.js wraps its redraw in Motion.redraw().
// Transform and opacity move, plus the height of a row that opens or closes and
// of the sections, groups and lists a redraw changes. Durations mirror the tokens
// in motion.css.

(() => {
  const EASE_OUT = "cubic-bezier(0.23, 1, 0.32, 1)";
  const PRESS_MS = 120;
  const POP_MS = 180;
  const MOVE_MS = 240;
  const EXIT_MS = 120;
  // A new card or row fades in over ENTER_MS; it waits ENTER_DELAY_MS when something leaves or moves first.
  const ENTER_MS = 180;
  const ENTER_DELAY_MS = 40;
  const TIP_DELAY_MS = 400;
  // Within this long after a tooltip closes, the next one opens at once and without animation.
  const TIP_SKIP_MS = 1000;
  const TIP_TOUCH_MS = 5000;
  // A card with controls (the Claude usage's, SESSION-17) stays this long after the pointer leaves its element or
  // the card, so the pointer can cross the gap between them; entering either keeps it open.
  const CARD_LEAVE_MS = 300;
  // A touch held this long on a control shows its tooltip; moving more than PRESS_SLOP px cancels it.
  const LONG_PRESS_MS = 500;
  const PRESS_SLOP = 10;
  // How far (px) the pointer moves after a click before tooltips show again.
  const POINTER_SLOP = 3;
  // A redraw this soon after a key press (d/x/z, Enter, typing) happens without animation.
  const KEY_QUIET_MS = 2000;
  // More moves than this in one redraw: the redraw shows at once.
  const MOVE_LIMIT = 80;

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const reduced = () => reducedMotion.matches;
  const picking = () => document.body.classList.contains("picking");

  // Tooltip

  const TIPPED = "[data-tip], [data-time], [data-tip-card]";
  // The most a hover text shows: lines, and characters per line.
  const TIP_LINES = 6;
  const TIP_LINE = 120;
  // On a touch screen a tap on a tipped element outside these shows its tooltip.
  // Controls act instead; a row head opens its row, whose body repeats the rank
  // and times with their own tooltips.
  const ACTING = "button, a, summary, input, select, textarea, label, .row-head";
  const GAP = 6;
  const EDGE = 8;

  const tip = document.createElement("div");
  tip.className = "tip";
  tip.setAttribute("aria-hidden", "true");
  tip.hidden = true;
  document.body.append(tip);

  let tipFor = null;
  let tipVia = "";
  let tipTimer = 0;
  let leaveTimer = 0;
  let tipPending = null;
  let tipHiddenAt = -Infinity;
  let pointerAt = null;
  let pointerType = "mouse";
  let tapHadTip = null;

  // The native tooltip never shows: its text moves to data-tip, and screen readers keep it as a description.
  function moveTitle(el) {
    const title = el.getAttribute("title");
    if (title === null) return;
    el.removeAttribute("title");
    if (title) {
      el.dataset.tip = title;
      el.setAttribute("aria-description", title);
    } else {
      delete el.dataset.tip;
      el.removeAttribute("aria-description");
    }
    if (el === tipFor) showTip(el, tipVia, true);
  }

  function tipTarget(node) {
    // The controls on a card answer the pointer themselves and show no tooltip over the card.
    if (!(node instanceof Element) || tip.contains(node)) return null;
    const titled = node.closest("[title]");
    if (titled) moveTitle(titled);
    return node.closest(TIPPED);
  }

  function exactTime(iso) {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "";
    const options = { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" };
    if (date.getFullYear() !== new Date().getFullYear()) options.year = "numeric";
    return date.toLocaleString([], options);
  }

  // A control's shortcut, from its data-key ("D", "Ctrl+Enter"), as keycaps after the tooltip's
  // text; on touch screens there are no keys to show. Fields use data-key as their draft key
  // ("msg:step:5", "add-task"), so fields are skipped and only short key names count.
  const KEY_NAMES = /^[^\s:+]{1,6}(?:\+[^\s:+]{1,6})*$/;
  const KEYLESS = "input, textarea, select";
  const hovers = window.matchMedia("(hover: hover)");
  const MAC = /Mac|iPhone|iPad/.test(navigator.platform);

  function keycaps(el) {
    const keys = el.dataset.key;
    if (!keys || !hovers.matches || el.matches(KEYLESS) || !KEY_NAMES.test(keys)) return null;
    const caps = Object.assign(document.createElement("span"), { className: "tip-keys" });
    for (const name of keys.split("+")) caps.append(Object.assign(document.createElement("kbd"), { textContent: MAC && name === "Ctrl" ? "⌘" : name }));
    return caps;
  }

  // A tooltip of several lines is a small card (UI-27): its first line is the header, the main
  // message; each later line is a muted detail under it.
  function fillTip(el) {
    // An element with a card of its own (the Claude usage's, UI-37) builds the tooltip's content.
    // A card can hold controls, so it takes the pointer and screen readers read it (CARD_LEAVE_MS).
    const card = el.dataset.tipCard !== undefined && typeof el.tipCard === "function";
    tip.classList.toggle("tip-card", card);
    if (card) tip.removeAttribute("aria-hidden");
    else tip.setAttribute("aria-hidden", "true");
    if (card) {
      // Focus on one of its controls moves to the new control with the same `data-focus` (a control whose name
      // changes as it is pressed carries one), else of the same name, or to the card's element.
      const at = tip.contains(document.activeElement) ? document.activeElement : null;
      const same = at?.dataset.focus ? `[data-focus="${CSS.escape(at.dataset.focus)}"]`
        : at?.hasAttribute("aria-label") ? `[aria-label="${CSS.escape(at.getAttribute("aria-label"))}"]` : null;
      tip.replaceChildren(el.tipCard());
      if (at) ((same && tip.querySelector(same)) ?? el).focus();
      return true;
    }
    const parts = [];
    if (el.dataset.tip) {
      // A hover text is a name or a short result (SPEC UI-46); a value that runs long, such as a to-do's whole
      // message, shows its start, and the card or page it opens holds the rest. An exact time takes one of the lines.
      const [head, ...details] = el.dataset.tip.split("\n").filter((line) => line.trim()).slice(0, el.dataset.time ? TIP_LINES - 1 : TIP_LINES)
        .map((line) => (line.length > TIP_LINE ? `${line.slice(0, TIP_LINE - 1)}…` : line));
      const text = Object.assign(document.createElement("span"), { className: details.length ? "tip-text tip-head" : "tip-text", textContent: head || "" });
      const caps = keycaps(el);
      if (caps) text.append(caps);
      parts.push(text);
      for (const line of details) parts.push(Object.assign(document.createElement("span"), { className: "tip-line", textContent: line }));
    }
    const time = el.dataset.time ? exactTime(el.dataset.time) : "";
    if (time) parts.push(Object.assign(document.createElement("span"), { className: "tip-time", textContent: time }));
    tip.replaceChildren(...parts);
    return parts.length > 0;
  }

  // Above the element, or below it when there is no room; kept inside the window.
  function placeTip(el) {
    tip.style.left = "0px";
    tip.style.top = "0px";
    const box = tip.getBoundingClientRect();
    const target = el.getBoundingClientRect();
    const width = document.documentElement.clientWidth;
    const height = document.documentElement.clientHeight;
    let top = target.top - GAP - box.height;
    const below = top < EDGE;
    if (below) top = target.bottom + GAP;
    top = Math.max(EDGE, Math.min(top, height - EDGE - box.height));
    const center = target.left + target.width / 2;
    const left = Math.max(EDGE, Math.min(center - box.width / 2, width - EDGE - box.width));
    tip.style.left = `${Math.round(left)}px`;
    tip.style.top = `${Math.round(top)}px`;
    tip.style.transformOrigin = `${Math.round(center - left)}px ${below ? "0" : "100%"}`;
    return below;
  }

  // uses.js counts the hover texts the user stops on (SPEC UI-45).
  function tipEvent(el, open) {
    document.dispatchEvent(new CustomEvent("cockpit:tip", { detail: { el, open } }));
  }

  function showTip(el, via, quick) {
    clearTimeout(tipTimer);
    clearTimeout(leaveTimer);
    leaveTimer = 0;
    tipPending = null;
    const instant = quick || tipFor !== null || performance.now() - tipHiddenAt < TIP_SKIP_MS;
    if (!fillTip(el)) { hideTip(); return; }
    if (tipFor !== el) {
      if (tipFor) tipEvent(tipFor, false);
      tipEvent(el, true);
    }
    tipFor = el;
    tipVia = via;
    for (const animation of tip.getAnimations()) animation.cancel();
    tip.hidden = false;
    const below = placeTip(el);
    if (!instant) {
      tip.animate(reduced()
        ? [{ opacity: 0 }, { opacity: 1 }]
        : [{ opacity: 0, transform: `translateY(${below ? -3 : 3}px) scale(.97)` }, { opacity: 1, transform: "none" }],
      { duration: POP_MS - 40, easing: EASE_OUT });
    }
    if (via === "touch") tipTimer = setTimeout(hideTip, TIP_TOUCH_MS);
  }

  function hideTip() {
    clearTimeout(tipTimer);
    clearTimeout(leaveTimer);
    leaveTimer = 0;
    tipPending = null;
    if (!tipFor) return;
    tipEvent(tipFor, false);
    tipFor = null;
    tipVia = "";
    tip.hidden = true;
    tipHiddenAt = performance.now();
  }

  // A redraw replaced the element under the tooltip. A card, however it opened, follows its element's replacement,
  // which carries the same data-tip-card name; a plain tooltip follows the element now under the pointer.
  function reanchorTip() {
    const name = tipFor.dataset.tipCard;
    if (name === undefined) {
      const under = tipVia === "hover" && pointerAt && document.elementFromPoint(pointerAt.x, pointerAt.y);
      const el = under && tipTarget(under);
      if (el) showTip(el, "hover", true);
      else hideTip();
      return;
    }
    const el = document.querySelector(`[data-tip-card="${CSS.escape(name)}"]`);
    if (!el) { hideTip(); return; }
    tipFor = el;
    // A control held down keeps its card as it is, so the press ends on the control it started on.
    if (tip.querySelector(":active")) return;
    // The card is filled again with its timers left as they are: a card leaving stays leaving, a touch one keeps
    // its TIP_TOUCH_MS. Focus that was on the replaced element goes to its replacement.
    fillTip(el);
    placeTip(el);
    if (tipVia === "focus" && (document.activeElement === null || document.activeElement === document.body)) el.focus();
  }

  // A card waits CARD_LEAVE_MS before it closes, and the pointer back on it or its element keeps it (cardHover);
  // a plain tooltip closes at once.
  function leaveTip() {
    if (!tip.classList.contains("tip-card")) { hideTip(); return; }
    if (!leaveTimer) leaveTimer = setTimeout(hideTip, CARD_LEAVE_MS);
  }

  // Where the pointer went while a card shows by hover. Every element it enters counts, so a card closes even when
  // its refill took the control under the pointer away, which leaves the card no pointerleave of its own.
  function cardHover(target) {
    if (tipVia !== "hover" || !tip.classList.contains("tip-card") || !(target instanceof Node)) return;
    if (tip.contains(target) || tipFor?.contains(target)) { clearTimeout(leaveTimer); leaveTimer = 0; } else leaveTip();
  }

  function hoverTip(target) {
    const el = tipTarget(target);
    if (!el || el === tipFor || el === tipPending) return;
    // A card with focus on one of its controls stays: the pointer passing over the page does not take it away.
    if (tip.contains(document.activeElement)) return;
    clearTimeout(tipTimer);
    if (tipFor || performance.now() - tipHiddenAt < TIP_SKIP_MS) { showTip(el, "hover", false); return; }
    tipPending = el;
    tipTimer = setTimeout(() => { if (tipPending === el && el.isConnected) showTip(el, "hover", false); }, TIP_DELAY_MS);
  }

  // After a click the pointer rests where it pressed, and a redraw may put a new
  // tipped element under it. Tooltips wait until the pointer moves again.
  let pressedPoint = null;
  const stillPressed = (event) => pressedPoint !== null
    && Math.hypot(event.clientX - pressedPoint.x, event.clientY - pressedPoint.y) <= POINTER_SLOP;

  document.addEventListener("pointerover", (event) => {
    if (event.pointerType === "touch") return;
    pointerAt = { x: event.clientX, y: event.clientY };
    cardHover(event.target);
    if (picking() || stillPressed(event) || event.buttons !== 0) return;
    hoverTip(event.target);
  });

  document.addEventListener("pointerout", (event) => {
    if (event.pointerType === "touch") return;
    // Out of the window: the pointer no longer marks what the user looks at, and a card it leaves from starts closing.
    if (event.relatedTarget === null) {
      pointerAt = null;
      if (tipVia === "hover" && tip.contains(event.target)) leaveTip();
    }
    const el = tipTarget(event.target);
    if (!el || (event.relatedTarget instanceof Node && el.contains(event.relatedTarget))) return;
    if (el === tipPending) { clearTimeout(tipTimer); tipPending = null; }
    if (el === tipFor && tipVia === "hover") leaveTip();
  });

  document.addEventListener("pointermove", (event) => {
    if (event.pointerType === "touch") return;
    pointerAt = { x: event.clientX, y: event.clientY };
    // A held button is a drag (a goal by its place, text being selected): tooltips wait until it is let go.
    if (pressedPoint === null || stillPressed(event) || event.buttons !== 0) return;
    pressedPoint = null;
    if (!picking()) hoverTip(event.target);
  }, { passive: true });

  document.addEventListener("pointerdown", (event) => {
    pointerType = event.pointerType;
    // A press on a card's control keeps the card open.
    if (event.target instanceof Node && tip.contains(event.target)) return;
    tapHadTip = tipFor;
    hideTip();
    // A click is no quick move between tooltips: the next one waits its full delay.
    tipHiddenAt = -Infinity;
    pressedPoint = { x: event.clientX, y: event.clientY };
  }, true);

  // On a touch screen a tap on an informing element shows its tooltip and a second
  // tap closes it. The tap still reaches the page (the spec page selects the block).
  document.addEventListener("click", (event) => {
    if (pointerType !== "touch" || picking()) return;
    const el = tipTarget(event.target);
    if (el && !el.closest(ACTING) && el !== tapHadTip) showTip(el, "touch", false);
  });

  // A control acts on a tap, so on a touch screen a long press shows its tooltip (UI-20). That press
  // does not act: the context menu it opens and the click it ends in are swallowed. Moving the finger
  // (a scroll) cancels it.
  let press = null;
  let pressShown = false;

  function cancelPress() {
    if (press) clearTimeout(press.timer);
    press = null;
  }

  function longPress() {
    const el = press.el;
    cancelPress();
    pressShown = true;
    if (el.isConnected) showTip(el, "touch", false);
  }

  document.addEventListener("pointerdown", (event) => {
    cancelPress();
    pressShown = false;
    if (event.pointerType !== "touch" || picking()) return;
    const el = tipTarget(event.target);
    if (!el || !el.closest(ACTING)) return;
    press = { el, x: event.clientX, y: event.clientY, timer: setTimeout(longPress, LONG_PRESS_MS) };
  }, true);
  document.addEventListener("pointermove", (event) => {
    if (press && Math.hypot(event.clientX - press.x, event.clientY - press.y) > PRESS_SLOP) cancelPress();
  }, { capture: true, passive: true });
  document.addEventListener("pointerup", cancelPress, true);
  document.addEventListener("pointercancel", cancelPress, true);
  // A browser that opens its context menu before the press time is up shows the tooltip right then.
  document.addEventListener("contextmenu", (event) => {
    if (!press && !pressShown) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (press) longPress();
  }, true);
  document.addEventListener("click", (event) => {
    if (!pressShown) return;
    pressShown = false;
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);

  document.addEventListener("focusin", (event) => {
    const el = event.target;
    if (!(el instanceof Element)) return;
    moveTitle(el);
    // A card's controls show no tooltip of their own over the card.
    if (tip.contains(el)) return;
    if (el.matches(TIPPED) && el.matches(":focus-visible")) showTip(el, "focus", false);
  });

  // A card opened by focus stays while focus is on its element or one of its controls. Whether focus left is
  // decided once it has settled: a redraw that removed the focused element or control moves focus to its replacement
  // (reanchorTip, fillTip) first.
  document.addEventListener("focusout", (event) => {
    if (tipVia !== "focus" || !(event.target === tipFor || tip.contains(event.target))) return;
    setTimeout(() => {
      const at = document.activeElement;
      if (tipVia === "focus" && tipFor !== null && !(tipFor.contains(at) || tip.contains(at))) hideTip();
    }, 0);
  });

  // A card's controls sit at the page's end, so Tab steps into them from the card's element: Tab on the element
  // goes to the first control, Shift+Tab on the first control goes back, and Tab on the last control returns to
  // the element, from where the default Tab goes on to the page's next control. Escape on a control returns to
  // the element; Escape there closes the card.
  function cardTab(event) {
    if (tipFor === null || !tip.classList.contains("tip-card")) return;
    const controls = [...tip.querySelectorAll("button:not(:disabled)")];
    const at = document.activeElement;
    if (at === tipFor && !event.shiftKey && controls.length > 0) {
      event.preventDefault();
      controls[0].focus();
    } else if (at === controls[0] && event.shiftKey) {
      event.preventDefault();
      tipFor.focus();
    } else if (at === controls.at(-1) && !event.shiftKey) {
      tipFor.focus();
    }
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "Tab") cardTab(event);
    if (event.key !== "Escape") return;
    if (tipFor !== null && tip.contains(document.activeElement)) tipFor.focus();
    else hideTip();
  });
  // A scroll moves the tip's element away from it; the card's own scroll does not.
  window.addEventListener("scroll", (event) => {
    if (!(event.target instanceof Node && tip.contains(event.target))) hideTip();
  }, { capture: true, passive: true });
  window.addEventListener("resize", hideTip);
  window.addEventListener("blur", hideTip);
  // A page opening beside its step moves the card, and the link's tip would stay where the link was.
  window.addEventListener("cockpit:layout-moved", hideTip);

  new MutationObserver((records) => {
    for (const record of records) {
      if (record.type === "attributes") { moveTitle(record.target); continue; }
      for (const node of record.addedNodes) {
        if (!(node instanceof Element)) continue;
        moveTitle(node);
        for (const el of node.querySelectorAll("[title]")) moveTitle(el);
      }
    }
    if (tipFor && !tipFor.isConnected) reanchorTip();
  }).observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ["title"] });
  for (const el of document.body.querySelectorAll("[title]")) moveTitle(el);

  // Press: buttons sink a little while held.

  const PRESSABLE = ".button, .prio, .menu-list button, .toast button";

  // In feedback mode a click picks the part instead of acting, so nothing sinks.
  document.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || reduced() || picking() || !(event.target instanceof Element)) return;
    const el = event.target.closest(PRESSABLE);
    if (!el || el.matches(":disabled")) return;
    const down = el.animate([{ scale: "1" }, { scale: ".97" }], { duration: PRESS_MS, easing: EASE_OUT, fill: "forwards" });
    const release = () => {
      for (const type of ["pointerup", "pointercancel", "pointerleave"]) el.removeEventListener(type, release);
      const from = getComputedStyle(el).scale;
      down.cancel();
      el.animate([{ scale: from === "none" ? "1" : from }, { scale: "1" }], { duration: PRESS_MS, easing: EASE_OUT });
    };
    for (const type of ["pointerup", "pointercancel", "pointerleave"]) el.addEventListener(type, release);
  }, { passive: true });

  // Menus and <details>

  // The entrance plays only when the user opens a menu, not when a redraw rebuilds one open.
  const opening = new WeakSet();

  document.addEventListener("click", (event) => {
    const summary = event.target instanceof Element && event.target.closest("summary");
    const details = summary && summary.parentElement;
    if (!(details instanceof HTMLDetailsElement) || picking()) return;
    opening.add(details);
    // motion.css clips a section's content while it grows.
    if (!details.open && !details.classList.contains("menu")) {
      details.classList.add("motion-opening");
      setTimeout(() => details.classList.remove("motion-opening"), MOVE_MS + 60);
    }
  }, true);

  document.addEventListener("toggle", (event) => {
    const details = event.target;
    if (!opening.has(details)) return;
    opening.delete(details);
    if (details.open && details.classList.contains("menu")) popMenu(details);
  }, true);

  function popMenu(details) {
    const summary = details.querySelector(":scope > summary");
    const list = summary && summary.nextElementSibling;
    if (!list) return;
    const box = list.getBoundingClientRect();
    const anchor = summary.getBoundingClientRect();
    const x = Math.abs(box.right - anchor.right) < Math.abs(box.left - anchor.left) ? "right" : "left";
    const y = box.top >= anchor.top ? "top" : "bottom";
    pop(list, x, y);
  }

  // Grows out of its `y` `x` corner, sliding 4 px away from that edge.
  function pop(el, x, y) {
    el.style.transformOrigin = `${y} ${x}`;
    el.animate(reduced()
      ? [{ opacity: 0 }, { opacity: 1 }]
      : [{ opacity: 0, scale: ".97", translate: `0 ${y === "top" ? -4 : 4}px` }, { opacity: 1, scale: "1", translate: "0 0" }],
    { duration: POP_MS, easing: EASE_OUT });
  }

  // Runs `entrance` each time the element with this id goes from hidden to shown. The board
  // has these boxes; the spec page has none of them.
  function whenShown(id, entrance) {
    const el = document.getElementById(id);
    if (!el) return;
    let shown = !el.hidden;
    new MutationObserver(() => {
      const now = !el.hidden;
      if (now && !shown) entrance(el);
      shown = now;
    }).observe(el, { attributes: true, attributeFilter: ["hidden"] });
  }

  // Feedback mode's box (what the user picked, and a note on it) pops in like a menu.
  whenShown("pick-box", (box) => {
    const rect = box.getBoundingClientRect();
    pop(box, rect.left + rect.width / 2 > innerWidth / 2 ? "right" : "left", rect.top + rect.height / 2 > innerHeight / 2 ? "bottom" : "top");
  });

  // The Add dialog: the backdrop fades in and the panel settles into place under it.
  whenShown("add-box", (box) => {
    box.animate([{ opacity: 0 }, { opacity: 1 }], { duration: POP_MS, easing: EASE_OUT });
    const panel = box.querySelector(".add-panel");
    if (panel && !reduced()) panel.animate([{ translate: "0 -6px", scale: ".99" }, { translate: "0 0", scale: "1" }], { duration: POP_MS, easing: EASE_OUT });
  });

  // The lightbox: the backdrop fades in and the image grows out of it.
  whenShown("lightbox", (box) => {
    box.animate([{ opacity: 0 }, { opacity: 1 }], { duration: POP_MS, easing: EASE_OUT });
    const image = box.querySelector("img");
    if (image && !reduced()) image.animate([{ scale: ".97" }, { scale: "1" }], { duration: POP_MS, easing: EASE_OUT });
  });

  // An image still loading when it is drawn fades in as it arrives instead of popping in; one
  // the browser already holds (as on every redraw after the first) shows at once.
  new MutationObserver((records) => {
    for (const record of records) {
      for (const node of record.addedNodes) {
        if (!(node instanceof Element) || node.closest(".motion-ghost")) continue;
        for (const image of node.matches("img") ? [node] : node.querySelectorAll("img")) {
          if (image.complete) continue;
          image.addEventListener("load", () => image.animate([{ opacity: 0 }, { opacity: 1 }], { duration: POP_MS, easing: EASE_OUT }), { once: true });
        }
      }
    }
  }).observe(document.body, { subtree: true, childList: true });

  // Toast: enters when shown, settles again when its text changes, fades when hidden.

  // A vertical offset added to whatever `translate` the element already has.
  function lifted(el, dy) {
    const base = getComputedStyle(el).translate;
    const [x = "0px", y = "0px"] = base === "none" ? [] : base.split(" ");
    return [`${x} calc(${y} + ${dy}px)`, `${x} ${y}`];
  }

  const toastEl = document.getElementById("toast");
  if (toastEl) {
    let shown = !toastEl.hidden;
    new MutationObserver(() => {
      const now = !toastEl.hidden;
      if (now) enterToast(shown);
      else if (shown) leaveToast();
      shown = now;
    }).observe(toastEl, { attributes: true, attributeFilter: ["hidden", "class"], childList: true });
  }

  // A toast moves away from the screen's edge it sits at: one at the foot lifts up as it comes and sinks as it
  // goes, one at the top (a phone's item screen) drops down and rises. 1 for the foot, -1 for the top.
  const edgeSign = (el) => (el.getBoundingClientRect().top < innerHeight / 2 ? -1 : 1);

  // A new toast is opaque within the first third of its entrance, so the page under it does not
  // show through for long; its lift and scale run the whole time. A toast that replaces a shown
  // one stays opaque and only settles.
  function enterToast(again) {
    for (const animation of toastEl.getAnimations()) animation.cancel();
    if (reduced()) { if (!again) toastEl.animate([{ opacity: 0 }, { opacity: 1 }], { duration: POP_MS }); return; }
    const [from, to] = lifted(toastEl, again ? 0 : 8 * edgeSign(toastEl));
    toastEl.animate(again
      ? [{ scale: ".985" }, { scale: "1" }]
      : [{ opacity: 0, scale: ".97", translate: from }, { opacity: 1, offset: 0.3 }, { opacity: 1, scale: "1", translate: to }],
    { duration: POP_MS, easing: EASE_OUT });
  }

  function leaveToast() {
    const ghost = toastEl.cloneNode(true);
    ghost.removeAttribute("id");
    ghost.removeAttribute("role");
    ghost.setAttribute("aria-hidden", "true");
    ghost.inert = true;
    ghost.hidden = false;
    ghost.classList.add("motion-ghost");
    document.body.append(ghost);
    const [at, to] = lifted(ghost, 0);
    const fade = ghost.animate(reduced()
      ? [{ opacity: 1 }, { opacity: 0 }]
      : [{ opacity: 1, translate: at }, { opacity: 0, translate: to.replace(/\S+$/, (y) => `calc(${y} + ${6 * edgeSign(ghost)}px)`) }],
    { duration: EXIT_MS, easing: EASE_OUT, fill: "forwards" });
    fade.finished.catch(() => {}).then(() => ghost.remove());
  }

  // Redraws. app.js rebuilds whole sections; motion.js makes the change read as one motion:
  // - Frames (a page section, a group, a list card) grow or shrink from the height they
  //   had to their new one, clipping what they hold, so everything below follows in flow.
  // - Steps, items and sessions (keyed elements) start where they were inside their frame
  //   and slide to their new place; one that passes more than one neighbour, changes frame
  //   or travels most of a screen fades out where it was and in where it is.
  // - New ones fade in a few px; removed ones fade out in their frame where they were.
  // - A row that closes shrinks its old body away, the reverse of a row that opens.
  // - Inside one that stays, a draft that unfolds or a detail that opens changes height in
  //   one glide, and a new thumbnail fades in.
  // - What the user added keeps its place when the server's answer gives it its real id.
  // - The scroll position holds still what the user was looking at.
  // A redraw that lands while these run carries them on from where they are.

  // `data-glide` keys an element that has no step, item or session of its own but moves with them: a heading of the
  // sidebar's groups.
  const KEYED = "[data-step], [data-item], [data-request], [data-session], [data-glide]";
  // The sidebar's groups are one frame, so a row glides from one group to another.
  const FRAMES = "main section, details.group, .list, .closed-list, .sidebar-groups";
  const ACROSS = ".sidebar-groups";
  const FIELDS = "input, textarea, select, [contenteditable]";
  const LOOSE = ["id", "data-step", "data-item", "data-request", "data-session", "data-glide", "data-key", "data-tip", "data-time"];
  const SCROLL_KEYS = new Set(["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "]);
  // A pointerdown this recent marks the element it hit as the one acted on.
  const ACTED_MS = 1500;
  // Longest the scroll position follows a redraw's motion.
  const HOLD_MS = 2000;
  let painted = false;
  // Set by coverNext(): the next redraw the user's action brings swaps Next up's card in place, when it comes
  // within COVER_MS of the call (an action that drew nothing leaves no cover behind for a later one).
  let coveredAt = -Infinity;
  const COVER_MS = 1000;
  let wasHidden = document.hidden;
  let keyAt = -Infinity;
  let pressed = null;
  let pressedAt = -Infinity;

  document.addEventListener("keydown", () => { keyAt = performance.now(); }, true);
  document.addEventListener("pointerdown", (event) => {
    keyAt = -Infinity;
    pressed = event.target instanceof Element ? event.target : null;
    pressedAt = performance.now();
  }, true);
  document.addEventListener("visibilitychange", () => { if (document.hidden) wasHidden = true; });

  // What each animation motion.js starts here does, so a redraw can start it again on the replacement.
  const motions = new WeakMap();

  function track(el, kind, keyframes, options, data) {
    const animation = el.animate(keyframes, options);
    motions.set(animation, { kind, keyframes, options, data });
    return animation;
  }

  // A running one keeps its start time, so it does not stand still for the frame a new
  // animation waits before it starts; a paused or pending one keeps its current time.
  function resume(el, motion) {
    const animation = track(el, motion.kind, motion.keyframes, motion.options, motion.data);
    if (motion.startTime !== null) animation.startTime = motion.startTime;
    else animation.currentTime = motion.currentTime;
    return animation;
  }

  function taken(animation) {
    return { ...motions.get(animation), currentTime: animation.currentTime, startTime: animation.playState === "running" ? animation.startTime : null };
  }

  const kindOf = (animation) => motions.get(animation)?.kind;
  const live = (animation) => animation.playState !== "finished" && animation.playState !== "idle";

  // A class stays on an element while any animation it was added for runs.
  const holds = new WeakMap();

  function holdClass(el, name, animations) {
    if (!animations.length) return;
    let byName = holds.get(el);
    if (!byName) holds.set(el, byName = new Map());
    let running = byName.get(name);
    if (!running) byName.set(name, running = new Set());
    el.classList.add(name);
    for (const animation of animations) {
      running.add(animation);
      animation.finished.catch(() => {}).then(() => {
        running.delete(animation);
        if (running.size || byName.get(name) !== running) return;
        byName.delete(name);
        el.classList.remove(name);
      });
    }
  }

  // The classes motion.js adds come and go while an element moves, so they stay out of its key.
  const OWN_CLASS = /^(motion-|changed$)/;

  // One key per element: the section it is in, what it is (a card or a row) and its id.
  // A data-glide name is the element's whole identity, so its classes stay out of the key: Busy's heading that
  // lights up during an Add's Undo (`holding`) is the same heading, and stays in place rather than fading out
  // while a copy slides in from below.
  function keyOf(el) {
    const section = el.closest("section");
    const glide = el.dataset.step === undefined && el.dataset.item === undefined && el.dataset.request === undefined
      && el.dataset.session === undefined;
    const id = el.dataset.step !== undefined ? `step ${el.dataset.step}`
      : el.dataset.item !== undefined ? `item ${el.dataset.item}`
      : el.dataset.request !== undefined ? `request ${el.dataset.request}`
      : el.dataset.session !== undefined ? `session ${el.dataset.session}`
      : `glide ${el.dataset.glide}`;
    const kind = glide ? "" : [...el.classList].find((name) => !OWN_CLASS.test(name)) || "";
    return `${section ? section.id : ""} ${el.tagName}.${kind} ${id}`;
  }

  function firstText(node) {
    while (node && node.nodeType === Node.ELEMENT_NODE) node = node.firstChild;
    return node ? node.textContent.trim() : "";
  }

  // The page's frames and keyed elements. A section is keyed by its id; a group or list
  // by its section, the group it sits in and its place among its likes. With `measure`,
  // each gets its rect as seen (moving elements where they are on screen).
  function snapshot(measure) {
    const frames = new Map();
    const frameKeys = new Map();
    const count = new Map();
    for (const el of document.querySelectorAll(FRAMES)) {
      if (el.closest(".motion-ghost")) continue;
      const section = el.closest("section");
      let key = section ? section.id : "";
      if (el !== section) {
        const details = el.closest("details");
        const summary = details && details.querySelector(":scope > summary");
        const base = `${key} ${el.tagName}.${el.classList[0] || ""} ${summary ? firstText(summary) : ""}`;
        const n = count.get(base) || 0;
        count.set(base, n + 1);
        key = `${base} ${n}`;
      }
      frames.set(key, { el, rect: measure ? el.getBoundingClientRect() : null });
      frameKeys.set(el, key);
    }
    // The frame each sits in, and the frame right after it there, if it is one.
    for (const frame of frames.values()) {
      const up = frame.el.parentElement && frame.el.parentElement.closest(FRAMES);
      frame.parent = up ? frameKeys.get(up) : undefined;
      let next = frame.el.nextElementSibling;
      while (next && next.classList.contains("motion-ghost")) next = next.nextElementSibling;
      frame.next = next ? frameKeys.get(next) : undefined;
    }
    const items = new Map();
    const twice = new Set();
    for (const el of document.querySelectorAll(KEYED)) {
      // One inside another moves with it.
      if (el.parentElement.closest(KEYED)) continue;
      const key = keyOf(el);
      if (items.has(key)) { twice.add(key); continue; }
      const frame = el.parentElement.closest(FRAMES);
      const item = { el, rect: null, parent: el.parentElement, section: el.closest("section"), frame: frame ? frameKeys.get(frame) : undefined };
      if (measure) {
        item.rect = el.getBoundingClientRect();
        // Inside a closed <details>: not on screen.
        if (!item.rect.width && !item.rect.height) continue;
        item.animations = el.getAnimations();
        item.opacity = item.animations.length ? Number(getComputedStyle(el).opacity) : 1;
        item.body = el.classList.contains("open") ? el.querySelector(":scope > .row-body:not(.motion-collapse)") : null;
        item.bodyHeight = item.body ? item.body.getBoundingClientRect().height : 0;
        item.parts = new Map(RESIZED.map((part) => {
          const found = el.querySelector(part);
          return [part, found ? found.getBoundingClientRect().height : 0];
        }));
      }
      items.set(key, item);
    }
    for (const key of twice) items.delete(key);
    return { frames, frameKeys, items };
  }

  function measureAfter(after) {
    for (const frame of after.frames.values()) frame.rect = frame.el.getBoundingClientRect();
    for (const [key, item] of after.items) {
      item.rect = item.el.getBoundingClientRect();
      if (!item.rect.width && !item.rect.height) after.items.delete(key);
    }
  }

  // Takes the running motions off the page before a redraw, to start them again after it:
  // frame heights and slides are cancelled (the snapshot holds where they were seen);
  // a body that is opening or closing is noted.
  function takeOver(before) {
    const carried = { frames: new Map(), items: new Map(), bodies: new Map(), closing: new Map() };
    const take = (animation) => {
      const motion = taken(animation);
      animation.cancel();
      return motion;
    };
    for (const [key, frame] of before.frames) {
      for (const animation of frame.el.getAnimations()) {
        if (live(animation) && kindOf(animation) === "frame") carried.frames.set(key, take(animation));
      }
    }
    for (const [key, item] of before.items) {
      const moving = item.animations.filter((animation) => live(animation) && ["move", "enter"].includes(kindOf(animation))).map(take);
      if (moving.length) carried.items.set(key, moving);
      const growing = item.body && item.body.getAnimations().find((animation) => live(animation) && kindOf(animation) === "expand");
      if (growing) carried.bodies.set(key, taken(growing));
      const closing = item.el.querySelector(":scope > .motion-collapse");
      if (closing) carried.closing.set(key, closing);
    }
    return carried;
  }

  // Copies of removed elements, and the frame each sits in; and the spots of removed
  // frames, which know their own place.
  function liveGhosts(before) {
    const ghosts = [];
    for (const box of document.querySelectorAll(".motion-leaving")) {
      const key = before.frameKeys.get(box.parentElement);
      if (key !== undefined) ghosts.push({ box, key });
    }
    for (const box of document.querySelectorAll(".motion-spot")) ghosts.push({ box, key: undefined });
    return ghosts;
  }

  // The height a closing spot adds to the frames around it, margins included.
  function spotHeight(spot) {
    return spot.getBoundingClientRect().height + parseFloat(getComputedStyle(spot).marginBottom) + spot.motionPlace.induced;
  }

  // A frame the redraw took away (a list whose last row left) leaves an empty spot where
  // it was, in front of the frame that followed it; the spot closes over a move, so what
  // came after slides up instead of jumping under the copies fading there, which the
  // spot holds and clips. Frames taken away together share one spot.
  function openSpots(before, after) {
    const spots = new Map();
    const byAnchor = new Map();
    for (const [key, was] of before.frames) {
      if (after.frames.has(key) || was.el.matches("section") || !was.rect.height || !nearScreen(was.rect)) continue;
      let next = was.next;
      while (next !== undefined && !after.frames.has(next)) next = before.frames.get(next).next;
      const parent = after.frames.get(was.parent);
      const parentWas = before.frames.get(was.parent);
      const anchor = next !== undefined ? after.frames.get(next) : null;
      const anchorWas = next !== undefined ? before.frames.get(next) : null;
      if (!parent || !parentWas || !anchor || !anchorWas || anchor.el.parentElement !== parent.el) continue;
      const shared = byAnchor.get(next);
      if (shared) { spots.set(key, shared); continue; }
      const without = anchor.el.getBoundingClientRect().top - parent.el.getBoundingClientRect().top;
      const el = document.createElement("div");
      el.className = "motion-ghost motion-spot";
      el.setAttribute("aria-hidden", "true");
      el.inert = true;
      // Measured 1px tall, so no margins collapse through it.
      el.style.height = "1px";
      parent.el.insertBefore(el, anchor.el);
      const parentRect = parent.el.getBoundingClientRect();
      const tall = anchor.el.getBoundingClientRect().top - parentRect.top;
      const height = anchorWas.rect.top - parentWas.rect.top - tall + 1;
      const induced = tall - without - 1;
      if (height < 1) { el.remove(); continue; }
      el.motionPlace = { parent: was.parent, next, induced };
      const close = track(el, "collapse", [
        { height: `${height}px`, marginBottom: "0px" },
        { height: "0px", marginBottom: `${-induced}px` },
      ], { duration: MOVE_MS, easing: EASE_OUT, fill: "forwards" }, {});
      el.style.height = "";
      close.finished.catch(() => {}).then(() => el.remove());
      const rect = el.getBoundingClientRect();
      const spot = {
        host: { el, rect },
        hostWas: { rect: { left: parentWas.rect.left + rect.left - parentRect.left, top: parentWas.rect.top + rect.top - parentRect.top } },
        animation: close,
      };
      spots.set(key, spot);
      byAnchor.set(next, spot);
    }
    return spots;
  }

  // A redraw without motion ends the running ones at once.
  function settle() {
    for (const animation of document.getAnimations()) {
      if (["frame", "leave", "collapse"].includes(kindOf(animation))) animation.cancel();
    }
  }

  function devicePixel(px) {
    return Math.round(px * window.devicePixelRatio) / window.devicePixelRatio;
  }

  function nearScreen(rect) {
    const height = window.innerHeight;
    return rect.bottom > -height / 2 && rect.top < height * 1.5;
  }

  function strip(node) {
    for (const el of [node, ...node.querySelectorAll("*")]) {
      for (const name of LOOSE) el.removeAttribute(name);
    }
  }

  function quiet(node) {
    for (const animation of node.getAnimations({ subtree: true })) animation.cancel();
    node.classList.remove("motion-moving", "motion-acted", "changed");
    strip(node);
    node.setAttribute("aria-hidden", "true");
    node.inert = true;
  }

  // The removed element fades out where it was, in its frame (or, when that frame is
  // gone, in a bare copy of its old container inside the frame's spot or else its
  // section, so styles that depend on the container still apply), clipped by the frame
  // and kept through later redraws.
  function leave(was, before, after, spots) {
    if (was.opacity < 0.05) return null;
    let host = was.frame !== undefined ? after.frames.get(was.frame) : null;
    let hostWas = host && before.frames.get(was.frame);
    let node = was.el.isConnected ? was.el.cloneNode(true) : was.el;
    quiet(node);
    let box = node;
    if (!host || !hostWas || !host.rect.height) {
      const spot = spots.get(was.frame);
      const id = was.section && was.section.id;
      host = spot ? spot.host : id ? after.frames.get(id) : null;
      hostWas = spot ? spot.hostWas : id ? before.frames.get(id) : null;
      if (!host || !hostWas) return null;
      if (was.parent && was.parent !== was.section) {
        box = was.parent.cloneNode(false);
        strip(box);
        box.style.padding = "0";
        box.style.border = "0";
        box.append(node);
      }
    }
    node.style.margin = "0";
    box.classList.add("motion-ghost", "motion-leaving");
    box.setAttribute("aria-hidden", "true");
    box.inert = true;
    Object.assign(box.style, {
      position: "absolute", left: "0px", top: "0px", margin: "0", zIndex: "0",
      width: `${was.rect.width}px`, height: `${was.rect.height}px`,
    });
    host.el.classList.add("motion-frame");
    host.el.append(box);
    const at = box.getBoundingClientRect();
    const frame = host.el.getBoundingClientRect();
    box.style.left = `${was.rect.left - hostWas.rect.left - (at.left - frame.left)}px`;
    box.style.top = `${was.rect.top - hostWas.rect.top - (at.top - frame.top)}px`;
    const fade = track(box, "leave", reduced()
      ? [{ opacity: was.opacity }, { opacity: 0 }]
      : [{ opacity: was.opacity }, { opacity: 0, translate: "0 -4px", scale: ".98" }],
    { duration: EXIT_MS, easing: EASE_OUT, fill: "forwards" }, {});
    holdClass(host.el, "motion-frame", [fade]);
    fade.finished.catch(() => {}).then(() => box.remove());
    return fade;
  }

  function enter(el, delayed) {
    return track(el, "enter", reduced()
      ? [{ opacity: 0 }, { opacity: 1 }]
      : [{ opacity: 0, translate: "0 6px" }, { opacity: 1, translate: "0 0" }],
    { duration: ENTER_MS, easing: EASE_OUT, delay: delayed ? ENTER_DELAY_MS : 0, fill: "backwards" }, {});
  }

  function bodyOf(row) {
    return row.querySelector(":scope > .row-body:not(.motion-collapse)");
  }

  // A row that opened grows its body to its height; the rows below follow.
  function expand(row) {
    const body = bodyOf(row);
    if (!body) return null;
    if (reduced()) return track(body, "expand", [{ opacity: 0 }, { opacity: 1 }], { duration: POP_MS }, {});
    const style = getComputedStyle(body);
    const height = body.getBoundingClientRect().height;
    const grow = track(body, "expand", [
      { height: "0px", paddingTop: "0px", paddingBottom: "0px", opacity: 0 },
      { opacity: 1, offset: 0.6 },
      { height: `${height}px`, paddingTop: style.paddingTop, paddingBottom: style.paddingBottom, opacity: 1 },
    ], { duration: MOVE_MS, easing: EASE_OUT }, {});
    holdClass(body, "motion-clip", [grow]);
    return grow;
  }

  // A row that closed keeps its old body for a moment inside the new row and shrinks it away.
  function adoptBody(row, body) {
    quiet(body);
    body.classList.add("motion-ghost", "motion-collapse");
    row.append(body);
  }

  // The keyframes are expand()'s in reverse: the body stays readable while its bottom edge rises.
  function collapse(body, height) {
    const style = getComputedStyle(body);
    const shrink = track(body, "collapse", [
      { height: `${height}px`, paddingTop: style.paddingTop, paddingBottom: style.paddingBottom, opacity: 1 },
      { opacity: 1, offset: 0.4 },
      { height: "0px", paddingTop: "0px", paddingBottom: "0px", opacity: 0 },
    ], { duration: MOVE_MS, easing: EASE_OUT, fill: "forwards" }, {});
    shrink.finished.catch(() => {}).then(() => body.remove());
    return shrink;
  }

  // The flex gap after a page section, which goes when the section empties.
  function gapAfter(el) {
    const parent = el.parentElement;
    if (!(el.matches("section") && parent && parent.matches(".primary"))) return 0;
    return parseFloat(getComputedStyle(parent).rowGap) || 0;
  }

  // Each kept element's place among the kept elements of its frame.
  function places(snap, kept) {
    const place = new Map();
    const count = new Map();
    for (const [key, item] of snap.items) {
      if (!kept.has(key)) continue;
      const n = count.get(item.frame) || 0;
      count.set(item.frame, n + 1);
      place.set(key, n);
    }
    return place;
  }

  // What the user adds shows at once under a stand-in id ("local-<n>") until the server answers;
  // the redraw with the answer draws it again under its real id. The new element takes the
  // stand-in's key (and its running motion), so it stays put instead of fading out and in.
  const STAND_IN = / local-\S+$/;

  function adoptStandIns(before, after, carried) {
    const names = new Map();
    const taken = new Set();
    for (const key of before.items.keys()) {
      if (!STAND_IN.test(key) || after.items.has(key)) continue;
      const kind = key.replace(STAND_IN, " ");
      const real = [...after.items.keys()].find((other) => other.startsWith(kind) && !STAND_IN.test(other) && !before.items.has(other) && !taken.has(other));
      if (!real) continue;
      names.set(key, real);
      taken.add(real);
    }
    if (!names.size) return;
    // Same order as before, so each keeps its place among its neighbours.
    before.items = new Map([...before.items].map(([key, item]) => [names.get(key) || key, item]));
    for (const map of [carried.items, carried.bodies, carried.closing]) {
      for (const [key, real] of names) {
        if (map.has(key)) { map.set(real, map.get(key)); map.delete(key); }
      }
    }
  }

  // Inside a step, item or session that stays, image thumbnails beyond those it had fade in
  // where they appear.
  const PARTS = [".thumb"];

  // A draft that unfolds or folds changes height inside a step that stays: it goes from the
  // height it had to its new one, clipping its text, and what is below it follows. One that was
  // not there grows open like a row's body.
  const RESIZED = [".draft"];

  function resize(el, from, to) {
    const style = getComputedStyle(el);
    const animation = track(el, "part", from < 1
      ? [
        { height: "0px", paddingTop: "0px", paddingBottom: "0px", opacity: 0 },
        { opacity: 1, offset: 0.6 },
        { height: `${to}px`, paddingTop: style.paddingTop, paddingBottom: style.paddingBottom, opacity: 1 },
      ]
      : [{ height: `${from}px` }, { height: `${to}px` }],
    { duration: MOVE_MS, easing: EASE_OUT }, {});
    holdClass(el, "motion-clip", [animation]);
    return animation;
  }

  function appear(el) {
    return el.animate(reduced()
      ? [{ opacity: 0 }, { opacity: 1 }]
      : [{ opacity: 0, translate: "0 4px" }, { opacity: 1, translate: "0 0" }],
    { duration: POP_MS, easing: EASE_OUT, fill: "backwards" });
  }

  // Next up drops out of both snapshots when `covered`: the card that left and the card that came stay as drawn,
  // with no fade, glide or change of height, since the action's own animation plays over them.
  function uncover(snap) {
    for (const [key, item] of snap.items) if (item.section && item.section.id === "next") snap.items.delete(key);
    for (const [key, frame] of snap.frames) if (frame.el.closest("#next")) snap.frames.delete(key);
  }

  function play(before, carried, ghosts, covered) {
    const motion = !reduced();
    const after = snapshot(false);
    if (covered) { uncover(before); uncover(after); }
    const started = [];
    adoptStandIns(before, after, carried);

    // Copies of removed elements that the draw took off the page go back into their
    // frame, and spots back to their place; one whose place is gone goes.
    for (const { box, key } of ghosts) {
      if (box.isConnected) continue;
      if (key === undefined) {
        const { parent, next } = box.motionPlace;
        const host = after.frames.get(parent);
        const anchor = after.frames.get(next);
        if (motion && host && anchor && anchor.el.parentElement === host.el) host.el.insertBefore(box, anchor.el);
        else for (const animation of box.getAnimations({ subtree: true })) animation.cancel();
        continue;
      }
      const frame = after.frames.get(key);
      if (!frame) continue;
      frame.el.append(box);
      holdClass(frame.el, "motion-frame", box.getAnimations().filter(live));
    }

    // Rows that closed get their old body back, to shrink it away.
    const closing = [];
    for (const [key, now] of after.items) {
      const was = before.items.get(key);
      if (!was || now.el === was.el || was.frame !== now.frame || now.el.classList.contains("open")) continue;
      const shrinking = carried.closing.get(key);
      if (shrinking) { now.el.append(shrinking); continue; }
      if (motion && was.body && was.bodyHeight && nearScreen(was.rect)) {
        adoptBody(now.el, was.body);
        closing.push([was.body, was.bodyHeight]);
      }
    }

    // Each frame's new height, without the bodies and spots that are about to shrink away.
    const shrinking = [
      ...[...document.querySelectorAll(".motion-collapse")].map((body) => [body, body.getBoundingClientRect().height]),
      ...[...document.querySelectorAll(".motion-spot")].map((spot) => [spot, spotHeight(spot)]),
    ];
    const natural = new Map();
    for (const [key, frame] of after.frames) {
      const rect = frame.el.getBoundingClientRect();
      let height = rect.height;
      for (const [body, bodyHeight] of shrinking) if (frame.el.contains(body)) height -= bodyHeight;
      natural.set(key, { rect, height });
    }

    // Frames go from the height they were seen at to the new one.
    if (motion) {
      for (const [key, frame] of after.frames) {
        const was = before.frames.get(key);
        const { rect, height } = natural.get(key);
        let seen;
        if (was) seen = was.rect.height;
        else if (frame.el.matches("details, .list, .closed-list") && before.frames.get(frame.el.closest("section")?.id)?.rect.height) seen = 0;
        else continue;
        if (Math.abs(height - seen) < 1) continue;
        if (!nearScreen(was ? was.rect : rect) && !nearScreen(rect)) continue;
        const old = carried.frames.get(key);
        let animation;
        if (old && Math.abs(old.data.to - height) < 1) {
          animation = resume(frame.el, old);
        } else {
          const gap = gapAfter(frame.el);
          const from = { height: `${seen}px` };
          const to = { height: `${height}px` };
          if (gap && seen < 1) { from.marginBottom = `${-gap}px`; to.marginBottom = "0px"; }
          if (gap && height < 1) { from.marginBottom = "0px"; to.marginBottom = `${-gap}px`; }
          // A list that appears grows its own margins with its height, so what is below moves
          // in one glide instead of jumping by the margins first.
          if (!gap && seen < 1) {
            const style = getComputedStyle(frame.el);
            from.marginTop = "0px";
            from.marginBottom = "0px";
            to.marginTop = style.marginTop;
            to.marginBottom = style.marginBottom;
          }
          animation = track(frame.el, "frame", [from, to], { duration: MOVE_MS, easing: EASE_OUT }, { to: height });
        }
        holdClass(frame.el, "motion-frame", [animation]);
        started.push(animation);
      }
    }

    // Rows that opened grow; rows that closed shrink; both carry on through a redraw.
    for (const [body, height] of closing) started.push(collapse(body, height));
    for (const [key, now] of after.items) {
      const was = before.items.get(key);
      if (!was || now.el === was.el || !now.el.classList.contains("open")) continue;
      if (!was.el.classList.contains("open")) {
        if (nearScreen(was.rect)) started.push(expand(now.el));
        continue;
      }
      const growing = carried.bodies.get(key);
      const body = bodyOf(now.el);
      if (growing && body) {
        const animation = resume(body, growing);
        if (motion) holdClass(body, "motion-clip", [animation]);
        started.push(animation);
      }
    }

    if (motion) {
      for (const [key, now] of after.items) {
        const was = before.items.get(key);
        // A row that opened or closed grows or shrinks as a whole.
        if (!was || now.el === was.el || !nearScreen(was.rect) || was.el.classList.contains("open") !== now.el.classList.contains("open")) continue;
        for (const [part, seen] of was.parts) {
          const el = now.el.querySelector(part);
          const height = el ? el.getBoundingClientRect().height : 0;
          if (el && Math.abs(height - seen) >= 1) started.push(resize(el, seen, height));
        }
      }
    }

    const spots = motion ? openSpots(before, after) : new Map();
    for (const spot of spots.values()) if (!started.includes(spot.animation)) started.push(spot.animation);

    // Where everything is now, with frames at the height they were seen at.
    measureAfter(after);
    const keptKeys = new Set([...after.items.keys()].filter((key) => before.items.has(key)));
    const placeBefore = places(before, keptKeys);
    const placeAfter = places(after, keptKeys);
    const acted = performance.now() - pressedAt < ACTED_MS ? pressed : null;
    const moves = [];
    const swaps = [];
    for (const key of keptKeys) {
      const was = before.items.get(key);
      const now = after.items.get(key);
      const near = nearScreen(was.rect) || nearScreen(now.rect);
      const carry = carried.items.get(key) || [];
      const wasFrame = before.frames.get(was.frame);
      const nowFrame = after.frames.get(now.frame);
      if (was.frame !== now.frame || !wasFrame || !nowFrame) {
        if (near) swaps.push([was, now]);
        continue;
      }
      const x = now.rect.left - nowFrame.rect.left;
      const y = now.rect.top - nowFrame.rect.top;
      // Whole device pixels: a slide that starts between pixels can show a sliver of what it clips.
      const dx = devicePixel(was.rect.left - wasFrame.rect.left - x);
      const dy = devicePixel(was.rect.top - wasFrame.rect.top - y);
      for (const old of carry) if (old.kind === "enter") started.push(resume(now.el, old));
      if (Math.abs(dx) < 1 && Math.abs(dy) < 1) continue;
      if (!near) continue;
      // A row that passes others fades out and in where it lands, except in a frame whose rows glide past each other.
      const passed = Math.abs(placeAfter.get(key) - placeBefore.get(key));
      if ((passed > 1 && !nowFrame.el.matches(ACROSS)) || Math.abs(dy) > window.innerHeight * 0.75) { swaps.push([was, now]); continue; }
      const sliding = carry.find((old) => old.kind === "move");
      const same = sliding && Math.abs(sliding.data.x - x) < 1 && Math.abs(sliding.data.y - y) < 1;
      // A row that lands in another group of the sidebar lights up there, as a change a poll brought does.
      const crossed = nowFrame.el.matches(ACROSS) && was.el.closest("details")?.className !== now.el.closest("details")?.className;
      moves.push({ el: now.el, dx, dy, x, y, sliding: same ? sliding : null, acted: Boolean(acted && was.el.contains(acted)), crossed });
    }
    if (moves.length > MOVE_LIMIT) {
      for (const animation of started) animation.cancel();
      return [];
    }
    if (motion) {
      for (const move of moves) {
        const animation = move.sliding
          ? resume(move.el, move.sliding)
          : track(move.el, "move", [{ translate: `${move.dx}px ${move.dy}px` }, { translate: "0 0" }],
            { duration: MOVE_MS, easing: EASE_OUT }, { x: move.x, y: move.y });
        holdClass(move.el, "motion-moving", [animation]);
        if (move.acted) holdClass(move.el, "motion-acted", [animation]);
        if (move.crossed) move.el.classList.add("changed");
        const list = move.el.parentElement;
        if (list && list.matches(".list, .closed-list")) holdClass(list, "motion-frame", [animation]);
        started.push(animation);
      }
    }
    for (const key of keptKeys) {
      const was = before.items.get(key);
      const now = after.items.get(key);
      if (now.el === was.el || !nearScreen(now.rect)) continue;
      for (const part of PARTS) {
        const had = was.el.querySelectorAll(part).length;
        for (const el of [...now.el.querySelectorAll(part)].slice(had)) started.push(appear(el));
      }
    }
    const removed = [...before.items.keys()].filter((key) => !after.items.has(key));
    const added = [...after.items.keys()].filter((key) => !before.items.has(key));
    let left = 0;
    for (const [was] of swaps) { const fade = leave(was, before, after, spots); if (fade) { started.push(fade); left += 1; } }
    for (const key of removed) {
      const was = before.items.get(key);
      if (!nearScreen(was.rect)) continue;
      const fade = leave(was, before, after, spots);
      if (fade) { started.push(fade); left += 1; }
    }
    const delayed = left > 0 || moves.length > 0;
    for (const [, now] of swaps) started.push(enter(now.el, delayed));
    for (const key of added) {
      const now = after.items.get(key);
      if (nearScreen(now.rect)) started.push(enter(now.el, delayed));
    }
    return started;
  }

  // Scroll anchoring. The browser's own anchoring loses its anchor when app.js replaces
  // a section's children, so motion.js holds one: the field the user is typing in; else the
  // innermost keyed element under the pointer; else the topmost section top or keyed
  // element on screen below the sticky header that is still there after the draw, those
  // in the pointer's column first (the primary column when the pointer is off the board);
  // else the section the user just clicked in; else any section on screen. The two columns
  // change apart, so only one can hold still, and the pointer marks which (SPEC UI-16).
  // At the top of the page only the field and the pointed element hold; else the top stays
  // in view. The anchor's top on screen stays where it was while the redraw's motion runs,
  // until the user scrolls or anything else moves the page (a script, focus, the scrollbar).
  // When the anchor slides within its list (rows arrived above it), the page scrolls with
  // the slide, so the anchor and the rows sliding with it stay still on screen. The
  // browser's own anchoring rests meanwhile, so every scroll it sees is someone else's.
  let holding = null;

  function stopHold() {
    if (!holding) return;
    cancelAnimationFrame(holding.frame);
    holding = null;
    document.documentElement.classList.remove("motion-holding");
  }

  // The page moved since the hold last set it, other than by shrinking below it.
  function scrolledAway(y) {
    const max = document.documentElement.scrollHeight - window.innerHeight;
    if (y > max + 0.5 && Math.abs(window.scrollY - max) <= 1) return false;
    return Math.abs(window.scrollY - y) > 1;
  }

  for (const type of ["wheel", "touchmove", "pointerdown"]) window.addEventListener(type, stopHold, { capture: true, passive: true });
  window.addEventListener("keydown", (event) => { if (SCROLL_KEYS.has(event.key)) stopHold(); }, true);

  // Room kept at the top of a column (UI-16). When a poll's redraw takes away something above
  // what the user reads and the page is too near its top to scroll back by that much, the column's top
  // margin takes the height, so the text stays put. Something that grows above it later takes the
  // room first. The room goes while the tab is hidden, and once the user scrolls it out of view above,
  // by scrolling back the same height, so that column holds still.
  const COLUMN_ROOM = ".layout > .primary, .layout > .side";
  let userScrollAt = -Infinity;
  let roomTimer = 0;

  function roomOf(column) {
    return parseFloat(column.style.marginTop) || 0;
  }

  function setRoom(column, px) {
    column.style.marginTop = px > 0.5 ? `${px}px` : "";
  }

  function dropRooms() {
    for (const column of document.querySelectorAll(COLUMN_ROOM)) setRoom(column, 0);
  }

  function dropRoomsAbove() {
    const line = headerLine();
    for (const column of document.querySelectorAll(COLUMN_ROOM)) {
      const room = roomOf(column);
      if (!room || column.getBoundingClientRect().top > line || window.scrollY < room) continue;
      setRoom(column, 0);
      window.scrollBy({ top: -room, behavior: "instant" });
    }
  }

  for (const type of ["wheel", "touchmove"]) window.addEventListener(type, () => { userScrollAt = performance.now(); }, { capture: true, passive: true });
  window.addEventListener("keydown", (event) => { if (SCROLL_KEYS.has(event.key)) userScrollAt = performance.now(); }, true);
  window.addEventListener("scroll", () => {
    if (performance.now() - userScrollAt > 1000) return;
    clearTimeout(roomTimer);
    roomTimer = setTimeout(dropRoomsAbove, 200);
  }, { passive: true });
  document.addEventListener("visibilitychange", () => { if (document.hidden) dropRooms(); });

  function headerLine() {
    const header = document.getElementById("header");
    return header ? Math.max(0, header.getBoundingClientRect().bottom) : 0;
  }

  // The board's column under the pointer, the element right under it, and the innermost keyed element there.
  function pointed() {
    const under = pointerAt && document.elementFromPoint(pointerAt.x, pointerAt.y);
    const column = under && under.closest(COLUMN_ROOM);
    if (!column || under.closest(".motion-ghost")) return { column, at: null, el: null };
    return { column, at: under, el: under.closest(KEYED) };
  }

  // The blocks of the Next up card (data-parts) the user reads one at a time: a part anchor holds the
  // paragraph the user reads, found again by its card and its words, so an agent's rewrite above it moves
  // nothing in view.
  const BLOCKS = "p, li, pre, blockquote, h1, h2, h3, h4, tr, summary, .why, .chips, .answers, .actions, .step-context, .left-note";

  function blockWords(block) {
    return wordsOf(block).slice(0, 160);
  }

  function partCandidate(block) {
    const card = block && block.closest("[data-parts]");
    if (!card || card.closest(".motion-ghost")) return null;
    const words = blockWords(block);
    return words ? { part: { card: keyOf(card), tag: block.tagName, words }, top: block.getBoundingClientRect().top } : null;
  }

  function nearAnchor(el) {
    return { key: keyOf(el), nested: Boolean(el.parentElement.closest(KEYED)), top: el.getBoundingClientRect().top };
  }

  // What the page holds still, best first: the field the user types in; what the pointer rests on in the
  // main column, the Next up block under it first; Next up itself while it shows (its first block
  // below the header when the pointer is elsewhere); what the pointer rests on in the side column;
  // then, scrolled down, what shows from the top.
  function pickAnchor() {
    const candidates = [];
    const focused = document.activeElement;
    if (focused instanceof Element && focused.matches(FIELDS)) {
      const rect = focused.getBoundingClientRect();
      if (rect.height && rect.bottom > 0 && rect.top < window.innerHeight) candidates.push({ field: focused, fieldKey: focused.dataset.key, top: rect.top });
    }
    const line = headerLine();
    const { column, at, el: under } = pointed();
    const inMain = Boolean(column && column.matches(".layout > .primary"));
    const pointedPart = inMain && at ? partCandidate(at.closest(BLOCKS)) : null;
    if (pointedPart) candidates.push(pointedPart);
    if (under && inMain) candidates.push(nearAnchor(under));
    const next = [...document.querySelectorAll("#next > [data-step]")].find((el) => !el.classList.contains("motion-ghost"));
    const nextRect = next && next.getBoundingClientRect();
    if (nextRect && nextRect.height && nextRect.bottom > line && nextRect.top < window.innerHeight) {
      if (!pointedPart) {
        const first = [...next.querySelectorAll(BLOCKS)].find((block) => {
          const top = block.getBoundingClientRect().top;
          return top >= line && top < window.innerHeight;
        });
        const part = first && partCandidate(first);
        if (part) candidates.push(part);
      }
      candidates.push(nearAnchor(next));
    }
    if (under && !inMain) candidates.push(nearAnchor(under));
    if (window.scrollY <= 0) return candidates;
    const first = column || document.querySelector(".layout > .primary");
    const shown = [];
    for (const el of document.querySelectorAll(`main section, ${KEYED}`)) {
      const { top, height } = el.getBoundingClientRect();
      if (!height || top < line || top >= window.innerHeight) continue;
      const own = Boolean(first && first.contains(el));
      if (el.matches("section")) shown.push({ id: el.id, top, own });
      else if (!el.parentElement.closest(KEYED) && !el.closest(".motion-ghost")) shown.push({ key: keyOf(el), nested: false, top, own });
    }
    shown.sort((a, b) => (b.own - a.own) || (a.top - b.top));
    candidates.push(...shown);
    const clicked = performance.now() - pressedAt < ACTED_MS && pressed && pressed.isConnected ? pressed.closest("main section") : null;
    if (clicked) candidates.push({ id: clicked.id, top: clicked.getBoundingClientRect().top });
    for (const section of document.querySelectorAll("main section")) {
      const rect = section.getBoundingClientRect();
      if (rect.height && rect.bottom > line && rect.top < window.innerHeight) candidates.push({ id: section.id, top: rect.top });
    }
    return candidates;
  }

  function resolve(candidate) {
    if (candidate.field) {
      const el = candidate.fieldKey ? document.querySelector(`[data-key="${CSS.escape(candidate.fieldKey)}"]`) : null;
      if (el) return el;
      return candidate.field.isConnected && !candidate.field.closest(".motion-ghost") ? candidate.field : null;
    }
    if (candidate.id) return document.getElementById(candidate.id);
    if (candidate.part) {
      const { card, tag, words } = candidate.part;
      for (const el of document.querySelectorAll("[data-parts]")) {
        if (keyOf(el) !== card || el.closest(".motion-ghost")) continue;
        for (const block of el.querySelectorAll(BLOCKS)) if (block.tagName === tag && blockWords(block) === words) return block;
      }
      return null;
    }
    for (const el of document.querySelectorAll(KEYED)) {
      if (keyOf(el) !== candidate.key || el.closest(".motion-ghost")) continue;
      if (candidate.nested === Boolean(el.parentElement.closest(KEYED))) return el;
    }
    return null;
  }

  function holdAnchor(candidates, animations, cause) {
    let el = null;
    let top = 0;
    for (const candidate of candidates) {
      const found = resolve(candidate);
      if (found && found.getBoundingClientRect().height) { el = found; top = candidate.top; break; }
    }
    if (!el) return;
    const column = el.closest(COLUMN_ROOM);
    const correct = () => {
      if (!el.isConnected) return false;
      let off = el.getBoundingClientRect().top - top;
      // Something grew above: the column's kept room gives way first.
      if (off > 0.5 && column && roomOf(column)) {
        const taken = Math.min(off, roomOf(column));
        setRoom(column, roomOf(column) - taken);
        off -= taken;
      }
      if (Math.abs(off) >= 0.5) window.scrollBy({ top: off, behavior: "instant" });
      // Something went above, more than the page can scroll back: the column keeps its room.
      const short = top - el.getBoundingClientRect().top;
      if (cause === "poll" && column && short > 0.5) setRoom(column, roomOf(column) + short);
      return true;
    };
    correct();
    const running = animations.filter(live);
    if (!running.length) return;
    const hold = { frame: 0, until: performance.now() + HOLD_MS, y: window.scrollY };
    holding = hold;
    document.documentElement.classList.add("motion-holding");
    const tick = () => {
      if (holding !== hold) return;
      if (scrolledAway(hold.y)) { stopHold(); return; }
      const done = !running.some(live) || performance.now() > hold.until;
      if (!correct() || done) { stopHold(); return; }
      hold.y = window.scrollY;
      hold.frame = requestAnimationFrame(tick);
    };
    hold.frame = requestAnimationFrame(tick);
  }

  // What a poll changed. After a poll's redraw, each step, item and session whose words or
  // state dots differ from before it, or that is new, gets the class `changed`: a grey wash that
  // fades (motion.css). Next up is left out: nothing in it moves or flashes while the user reads it (UI-16), and its "Updated" mark
  // says what an agent changed. Times ("just now", "3 min ago", "08:15"), a running timer's
  // clock ("1:02:09"), the rank and the fields leave the comparison, so a clock ticking, a rank
  // drifting or the user's own typing tints nothing. It moves nothing, so it also runs with reduced
  // motion.
  const UNCOMPARED = "input, textarea, select, .prompt-field, [data-time], [data-timer-start], .rank";
  const COMPARED_MARKS = ".dot, .state-dot";
  const TIMES = /\bjust now\b|\b\d+\s?(?:s|sec|min|m|h|d|w)\b(?:\s?ago)?|\b\d{1,2}:\d{2}(?:\s?[ap]\.?m\.?)?/gi;

  function wordsOf(el) {
    const parts = [];
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        if (node.nodeType === Node.TEXT_NODE) return NodeFilter.FILTER_ACCEPT;
        if (node.matches(UNCOMPARED)) return NodeFilter.FILTER_REJECT;
        return node.matches(COMPARED_MARKS) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
      },
    });
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      parts.push(node.nodeType === Node.TEXT_NODE ? node.data : ` [${node.getAttribute("class")}] `);
    }
    return parts.join("").replace(TIMES, "#").replace(/\s+/g, " ").trim();
  }

  // Each keyed element's words, by key; a key drawn twice is left out.
  function contents() {
    const seen = new Map();
    const twice = new Set();
    const add = (key, el, words) => {
      if (seen.has(key)) twice.add(key);
      else seen.set(key, { el, words });
    };
    for (const el of document.querySelectorAll(KEYED)) {
      if (el.parentElement.closest(KEYED) || el.closest(".motion-ghost, #next")) continue;
      add(keyOf(el), el, wordsOf(el));
    }
    for (const key of twice) seen.delete(key);
    return seen;
  }

  function markChanged(before) {
    const tinted = new Set();
    for (const [key, now] of contents()) {
      const was = before.get(key);
      if (was && (was.el === now.el || was.words === now.words)) continue;
      // A part of a card that is tinted whole already adds nothing.
      if (now.el.parentElement && tinted.has(now.el.parentElement)) continue;
      tinted.add(now.el);
      // Restarts the wash on an element that still shows one.
      if (now.el.classList.contains("changed")) {
        now.el.classList.remove("changed");
        void now.el.offsetWidth;
      }
      now.el.classList.add("changed");
    }
  }

  for (const type of ["animationend", "animationcancel"]) {
    document.addEventListener(type, (event) => {
      if (event.animationName === "changed" && event.target instanceof Element) event.target.classList.remove("changed");
    });
  }

  // Runs `draw` (which redraws the board's sections) and animates the change. It shows
  // the redraw at once on the first paint, on the first one after the tab was hidden,
  // and right after a key press, so keyboard actions stay instant; the scroll position
  // holds still in all but the first. `draw` leaves the page in its final layout: a class
  // that changes an element's size is set inside it, because the "after" measurement runs
  // as soon as it returns. `cause` is "poll" for a redraw the 5-second poll brought, whose
  // changes get tinted (not on the first paint or the first after the tab was hidden), and
  // "action" for one the user's own click, key or optimistic change brought.
  function redraw(draw, cause) {
    if (cause !== "poll" && cause !== "action") logError("motion", new Error(`Motion.redraw: cause must be "poll" or "action", got ${cause}`));
    const first = !painted;
    // A page catching up after the tab came back (app.js, UI-60) draws what changed meanwhile as the tab's return does.
    const returned = wasHidden || document.body.classList.contains("catching-up");
    const covered = cause === "action" && performance.now() - coveredAt < COVER_MS;
    if (cause === "action") coveredAt = -Infinity;
    const still = first || returned || document.hidden || performance.now() - keyAt < KEY_QUIET_MS;
    painted = true;
    if (!document.hidden) wasHidden = false;
    stopHold();
    let anchor = [];
    let before = null;
    let carried = null;
    let ghosts = [];
    let words = null;
    try {
      if (cause === "poll" && !first && !returned && !document.hidden) words = contents();
    } catch (error) {
      logError("motion: reading the page before the redraw failed", error);
    }
    try {
      // A step's card sliding to or from its column beside a page (page-overlay.js) lands first: measured
      // mid-slide, it would hold the page's scroll to a place the card is only passing through.
      for (const animation of document.getAnimations()) if (animation.id === "beside-slide") animation.finish();
      if (!first) anchor = pickAnchor();
      if (still) settle();
      else {
        // Measured mid-flight, an element that is still moving starts its next move from where it is seen.
        before = snapshot(true);
        carried = takeOver(before);
        ghosts = liveGhosts(before);
      }
    } catch (error) {
      logError("motion: measuring before the redraw failed", error);
      before = null;
    }
    draw();
    try {
      const started = before ? play(before, carried, ghosts, covered) : [];
      // After a Send or close on the card, the next step shows from its top (SPEC UI-64), not where the old one was read.
      const revealed = covered && revealNext();
      if (anchor.length && !revealed) holdAnchor(anchor, started, cause);
    } catch (error) {
      logError("motion: the redraw animation failed", error);
    }
    try {
      if (words) markChanged(words);
    } catch (error) {
      logError("motion: marking what the poll changed failed", error);
    }
  }

  // A close or a Send on Next up plays its own animation over a copy of the card (app.js's closeAnimation and
  // sendAway), so the step after it is drawn under that copy at once: the next redraw the user's action
  // brings shows the new card in place, with no fade out of the old card and no fade in of the new one.
  function coverNext() {
    coveredAt = performance.now();
    dimNext();
  }

  // The page starts a shade darker under the leaving card and lightens into place as the next step comes in, so the
  // close or the Send stands out against it. The shade covers the whole window, margins and header
  // included: a fixed layer on the body (.arrive-shade in motion.css) under the leaving card's copies. It
  // holds while the leaving card covers most of the step, starts to lift ARRIVE_LEAD_MS before the card is out of
  // sight and is gone ARRIVE_TAIL_MS after. The card is taken to be out of sight ARRIVE_GONE_MS after the press (a
  // Send, the GPU closes), unless the close says how to measure it (arrive): then every frame until the shade starts
  // to lift, since some closes build their pieces a frame or more later. The shade lifts at once when Next up shows
  // no card (the Inbox is empty) or the user presses anything (Undo, another item, the next step itself), since Next
  // up may then show another thing.
  const ARRIVE_GONE_MS = 600;
  const ARRIVE_LEAD_MS = 200;
  const ARRIVE_TAIL_MS = 500;
  const ARRIVE_MAX_MS = 2000;
  const ARRIVE_LIFT_MS = 150;
  let arriving = null;

  function arriveTiming(gone) {
    const duration = gone + ARRIVE_TAIL_MS;
    const hold = Math.max(0, gone - ARRIVE_LEAD_MS);
    return {
      keyframes: [{ opacity: 1 }, { opacity: 1, offset: hold / duration, easing: "cubic-bezier(0.45, 0, 0.55, 1)" }, { opacity: 0 }],
      duration,
      gone,
      hold,
    };
  }

  function dimNext() {
    const next = document.getElementById("next");
    if (!next || !next.querySelector(":scope > .next-card")) return;
    if (arriving) arriving.stop();
    const shade = document.createElement("div");
    shade.className = "arrive-shade";
    shade.setAttribute("aria-hidden", "true");
    document.body.append(shade);
    let timing = arriveTiming(ARRIVE_GONE_MS);
    let animation = null;
    let measure = null;
    let frame = 0;
    const pressed = () => lift();
    const stop = () => {
      cancelAnimationFrame(frame);
      removeEventListener("pointerdown", pressed, true);
      removeEventListener("keydown", pressed, true);
      if (animation) animation.cancel();
      shade.remove();
    };
    const arrival = { measure: (how) => { measure = how; }, stop };
    arriving = arrival;
    const play = (keyframes, options) => {
      const old = animation;
      animation = shade.animate(keyframes, options);
      if (old) old.cancel();
      const mine = animation;
      mine.finished.then(() => {
        if (mine !== animation || arriving !== arrival) return;
        stop();
        arriving = null;
      }, () => {});
    };
    // From where the shade stands, out in ARRIVE_LIFT_MS.
    const lift = () => {
      if (arriving !== arrival) return;
      cancelAnimationFrame(frame);
      timing = { hold: 0 };
      play([{ opacity: Number(getComputedStyle(shade).opacity) || 0 }, { opacity: 0 }], { duration: ARRIVE_LIFT_MS, easing: "ease-out" });
    };
    // Lifts the shade when the section lost its card, and until the shade starts to lift, retimes it to the close's
    // measure.
    const follow = () => {
      if (arriving !== arrival) return;
      if (!next.querySelector(":scope > .next-card")) { lift(); return; }
      const now = animation.currentTime || 0;
      const left = measure && now < timing.hold ? measure() : NaN;
      if (Number.isFinite(left)) {
        const gone = Math.min(Math.max(now + left, now + ARRIVE_LEAD_MS), ARRIVE_MAX_MS);
        if (Math.abs(gone - timing.gone) > 8) {
          timing = arriveTiming(gone);
          animation.effect.setKeyframes(timing.keyframes);
          animation.effect.updateTiming({ duration: timing.duration });
        }
      }
      frame = requestAnimationFrame(follow);
    };
    play(timing.keyframes, { duration: timing.duration });
    addEventListener("pointerdown", pressed, true);
    addEventListener("keydown", pressed, true);
    frame = requestAnimationFrame(follow);
  }

  // The close that coverNext() announced says how long until its card is out of sight: `left()` gives the ms from
  // now, or NaN while it cannot tell. The shade asks every frame until it starts to lift.
  function arrive(left) {
    if (arriving) arriving.measure(left);
  }

  // The height of the bar that stays at the window's top over the page: the header, or on a phone the phone bar.
  function topBar() {
    const bar = document.querySelector(".phone-bar");
    const phone = bar && getComputedStyle(bar).position === "sticky" ? Math.max(0, bar.getBoundingClientRect().bottom) : 0;
    return Math.max(headerLine(), phone);
  }

  // The scroll position that shows the top of the card column: the page's top when the card is near it.
  function nextTop() {
    const next = document.getElementById("next");
    if (!next || !next.firstElementChild) return null;
    const column = next.closest(".layout > .primary") || next;
    const y = window.scrollY + column.getBoundingClientRect().top - headerLine();
    const at = window.scrollY + next.getBoundingClientRect().top - topBar() - 12;
    return Math.max(0, at - y < 240 ? y : at);
  }

  // With the page scrolled down the card a Send or close took away, the page goes at once to the next step's top,
  // under the leaving card's animation. Returns whether it moved the page, so the redraw does not also hold it still.
  function revealNext() {
    const y = nextTop();
    if (y === null || window.scrollY <= y + 1) return false;
    window.scrollTo({ top: y, behavior: "instant" });
    return true;
  }

  window.Motion = Object.freeze({ redraw, coverNext, arrive });
})();
