"""Cockpit installed as an app: what installing needs, and an update check on every page (SPEC UI-13 and UI-79)."""

import json
import struct
import zlib

import pytest
from fastapi.testclient import TestClient

from cockpit.app import MANIFEST_PATH, STATIC_DIR
from tests.conftest import World

OTHER_PAGES = ("spec.html", "briefing.html", "usage.html", "server.html", "pages.html")


@pytest.mark.parametrize("path", ["/briefing", "/pages", "/usage", "/server", "/spec", "/skills"])
def test_the_helper_pages_open_in_the_center_layer(world: World, path: str) -> None:
    page = world.client.get(path)
    assert "frame-ancestors 'self'" in page.headers["content-security-policy"]
    assert '<script src="/static/center-frame.js' in page.text


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def first_pixel(data: bytes) -> tuple[int, int, int]:
    # The top-left pixel's colour. Every PNG row filter leaves the first pixel of the first row as stored.
    color_type = data[25]
    assert color_type in (2, 6) and data[28] == 0, "expected non-interlaced RGB or RGBA"
    chunks, at = b"", 8
    while at < len(data):
        length, kind = struct.unpack(">I4s", data[at:at + 8])
        if kind == b"IDAT":
            chunks += data[at + 8:at + 8 + length]
        at += 12 + length
    row = zlib.decompress(chunks)
    return tuple(row[1:4])


def test_manifest_is_served_to_a_browser_without_the_cookie(anonymous: TestClient) -> None:
    # Browsers ask for the manifest without credentials, and a stale one would keep an old icon or name.
    response = anonymous.get(MANIFEST_PATH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    assert response.headers["cache-control"] == "no-cache"
    manifest = response.json()
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    assert manifest["id"] == "/"


def test_manifest_icons_exist_and_are_the_sizes_it_says(anonymous: TestClient) -> None:
    manifest = json.loads((STATIC_DIR / "manifest.webmanifest").read_text())
    purposes: set[tuple[str, str]] = set()
    for icon in manifest["icons"]:
        response = anonymous.get(icon["src"])
        assert response.status_code == 200, icon["src"]
        width, height = png_size(response.content)
        assert f"{width}x{height}" == icon["sizes"], icon["src"]
        purposes.add((icon["sizes"], icon["purpose"]))
    assert purposes == {("192x192", "any"), ("512x512", "any"), ("192x192", "maskable"), ("512x512", "maskable")}


def test_apple_touch_icon_is_opaque_and_the_size_ios_asks_for(anonymous: TestClient) -> None:
    response = anonymous.get("/static/apple-touch-icon.png")
    assert response.status_code == 200
    assert png_size(response.content) == (180, 180)
    # Colour type 2 is RGB with no alpha; iOS draws transparency as black.
    assert response.content[25] == 2


def test_every_page_links_what_installing_needs() -> None:
    # A person may start the install from any page, and an installed window can land on any of them. The parts' pages
    # sit in folders under static/.
    pages = sorted(STATIC_DIR.rglob("*.html"))
    assert pages
    for page in pages:
        text = page.read_text()
        assert f'<link rel="manifest" href="{MANIFEST_PATH}">' in text, page.name
        assert '<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">' in text, page.name
        assert '<meta name="mobile-web-app-capable" content="yes">' in text, page.name
        assert '<meta name="apple-mobile-web-app-capable" content="yes">' in text, page.name
        assert 'name="theme-color"' in text, page.name


def test_other_pages_check_for_a_new_version_and_the_special_ones_do_not() -> None:
    # An installed window has no address bar, so these need their own Reload (UI-13).
    for name in OTHER_PAGES:
        assert '<script src="/static/page-update.js" defer></script>' in (STATIC_DIR / name).read_text(), name
    # The main page has its own check, and the emergency page keeps to its own files (SESSION-21).
    for name in ("index.html", "emergency.html"):
        assert "page-update.js" not in (STATIC_DIR / name).read_text(), name


def test_running_version_needs_sign_in(anonymous: TestClient) -> None:
    response = anonymous.get("/api/version")
    assert response.status_code == 401
    assert response.json() == {"detail": "sign in first"}


def test_running_version_matches_the_one_the_main_page_gets(world: World) -> None:
    response = world.client.get("/api/version")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"version": world.client.get("/api/state").json()["version"]}


def test_every_page_says_which_cockpit_sent_it_and_cannot_be_kept_by_a_304(world: World) -> None:
    # A page's Reload compares against this. A file's ETag could answer 304 and keep an old version on a new page.
    running = world.client.get("/api/version").json()["version"]
    assert running
    for place in ("/", "/spec", "/briefing", "/usage", "/server", "/pages"):
        response = world.client.get(place)
        assert f'<meta name="cockpit-version" content="{running}">' in response.text, place
        assert response.headers["cache-control"] == "no-cache", place
        assert "etag" not in response.headers, place


def test_a_service_worker_does_not_exist() -> None:
    # Updates reach the page by normal HTTP caching and the Reload key alone (UI-13), so nothing may keep old files.
    assert not list(STATIC_DIR.rglob("*worker*"))
    for script in STATIC_DIR.rglob("*.js"):
        assert "serviceWorker.register" not in script.read_text(), script.name


def test_the_phone_launch_screen_is_graphite_like_the_page_that_follows_it() -> None:
    # Android paints its own launch screen in background_color; the main page's launch picks up from it (UI-80).
    manifest = json.loads((STATIC_DIR / "manifest.webmanifest").read_text())
    assert manifest["background_color"] == "#141210"
    assert 'GRAPHITE = "#141210"' in (STATIC_DIR / "launch.js").read_text()
    assert "--launch-ground: #141210;" in (STATIC_DIR / "launch.css").read_text()


def test_the_main_page_decides_on_its_launch_before_it_paints() -> None:
    # The script runs in the head without defer, so a cold start never flashes the page before the launch.
    text = (STATIC_DIR / "index.html").read_text()
    head, body = text.split("<body>", 1)
    assert '<script src="/static/launch.js"></script>' in head
    assert '<link rel="stylesheet" href="/static/launch.css">' in head
    assert body.lstrip().startswith('<div id="launch" class="launch" aria-hidden="true">')
    # The page tells it when its first state has drawn, whether or not that worked.
    app = (STATIC_DIR / "app.js").read_text()
    finally_block = app.split("window.CockpitBoot = null;", 1)[1][:200]
    assert "window.CockpitLaunch?.ready();" in finally_block


def test_the_launch_shows_once_per_app_window_not_on_every_move_to_the_main_page() -> None:
    # Cockpit sends no referrer (Referrer-Policy: no-referrer), so a move from another of its pages has none either;
    # the window's session storage is what tells a fresh open from a move.
    script = (STATIC_DIR / "launch.js").read_text()
    assert "document.referrer" not in script
    assert "sessionStorage.getItem(LAUNCHED)" in script
    assert "sessionStorage.setItem(LAUNCHED" in script


def test_the_maskable_icon_tile_is_the_launch_screens_graphite() -> None:
    # The phone shows the maskable icon's middle circle on background_color; a tile of another colour would show as a
    # disc that vanishes when the page's launch takes over.
    for name in ("icon-maskable-192.png", "icon-maskable-512.png"):
        assert first_pixel((STATIC_DIR / name).read_bytes()) == (0x14, 0x12, 0x10), name
