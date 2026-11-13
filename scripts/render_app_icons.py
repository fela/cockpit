#!/usr/bin/env python3
"""Render the installed app's PNG icons from src/cockpit/static/icon.svg.

Run again when icon.svg changes, then commit the PNG files:

    uv run --with playwright python scripts/render_app_icons.py

Set BROWSER_EXECUTABLE to use a Chromium that Playwright did not download.
"""

import os
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

STATIC = Path(__file__).resolve().parent.parent / "src" / "cockpit" / "static"
ROUNDED = {"icon-192.png": 192, "icon-512.png": 512}
# The system masks these itself, so they fill the square.
FULL_BLEED = {"icon-maskable-192.png": 192, "icon-maskable-512.png": 512, "apple-touch-icon.png": 180}
# Android shows a maskable icon as an adaptive icon: only its middle two thirds, in a circle, both on the home
# screen and on the launch screen, where that circle is 160 dp across. At full size the mark's wings (25.2 of the
# drawing's 32 units) run past that circle and the mark fills it; at this scale they stay inside with room around
# them, and the launch screen's mark is the size launch.css draws it (UI-80). The iPhone shows the whole square,
# so its icon keeps the full mark.
MASKABLE_MARK = 0.62


# The launch screen draws the maskable icon's circle on the manifest's background_color, and the page's launch then
# shows that graphite flat. So the maskable tile is flat graphite too, not the gradient: otherwise the launch screen
# shows a lighter disc that vanishes when the page takes over.
MASKABLE_TILE = "#141210"


def maskable(svg: str) -> str:
    shrunk, count = re.subn(r"<g ", f'<g transform="translate(16 16) scale({MASKABLE_MARK}) translate(-16 -16)" ', svg)
    if count != 1:
        raise SystemExit("icon.svg should draw its mark in exactly one group")
    flat, count = re.subn(r'(<rect [^>]*)fill="url\(#t\)"', rf'\1fill="{MASKABLE_TILE}"', shrunk)
    if count != 1:
        raise SystemExit("icon.svg should fill its tile with gradient #t")
    return flat


def page_for(svg: str, size: int) -> str:
    return (
        "<!doctype html><html><body style='margin:0;background:transparent'>"
        f"<div style='width:{size}px;height:{size}px'>{svg}</div></body></html>"
    )


def main() -> None:
    svg = (STATIC / "icon.svg").read_text()
    square, count = re.subn(r'\s+rx="[^"]*"', "", svg)
    if count != 1:
        raise SystemExit("icon.svg should have exactly one rounded rectangle")
    executable = os.environ.get("BROWSER_EXECUTABLE") or None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=executable)
        for names, source, transparent in ((ROUNDED, svg, True), (FULL_BLEED, square, False)):
            for name, size in names.items():
                drawing = maskable(source) if "maskable" in name else source
                page = browser.new_page(viewport={"width": size, "height": size})
                page.set_content(page_for(drawing, size))
                page.screenshot(path=STATIC / name, omit_background=transparent)
                page.close()
        browser.close()


if __name__ == "__main__":
    main()
