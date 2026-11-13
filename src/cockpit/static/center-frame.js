"use strict";
// A helper page (Briefing, Pages, Usage, Spec, Skills, or one an install's part serves) shown in the center layer
// over Cockpit's main page (center.js): `?center=1` drops the page's own header, since the layer has its
// own, and Escape asks the layer to close. A link to another of Cockpit's helper pages stays in the layer; a link to the
// rest of Cockpit opens in the whole window. Loaded without defer, so the header is gone before the page first paints.
if (new URLSearchParams(location.search).get("center") === "1" && window.parent !== window) {
  document.documentElement.classList.add("center-frame");
  const helper = /^\/(briefing|pages|usage|spec|skills|writing)(\/|$)/;
  addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || event.defaultPrevented) return;
    if (event.target.closest?.("input, textarea, select, [contenteditable]")) return;
    parent.postMessage({ cockpitCenter: "close" }, location.origin);
  });
  addEventListener("click", (event) => {
    const link = event.target.closest?.("a[href]");
    if (!link || link.target || event.defaultPrevented) return;
    const url = new URL(link.href, location.href);
    if (url.origin !== location.origin) return;
    if (helper.test(url.pathname) || parent.cockpitHelperPage?.(url.pathname)) {
      url.searchParams.set("center", "1");
      link.href = url.href;
    } else {
      link.target = "_top";
    }
  }, true);
}
