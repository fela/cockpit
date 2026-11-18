// The brand menu's Settings entry, above Sign out, on every page: it opens the Settings page (SPEC CONTROL-33),
// which holds the briefing schedule, the time zone and this browser's Vim editing. Loads deferred.
"use strict";

(() => {
  const list = document.querySelector(".brand-menu .menu-list");
  if (!list || list.querySelector('a[href="/settings"]')) return;
  const link = document.createElement("a");
  link.href = "/settings";
  link.className = "settings-link";
  if (location.pathname === "/settings") link.setAttribute("aria-current", "page");
  const label = document.createElement("span");
  label.className = "menu-label";
  label.append(Icons.icon("tune"), "Settings");
  link.append(label);
  list.querySelector("form").before(link);
})();
