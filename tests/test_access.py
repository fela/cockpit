from __future__ import annotations

import base64
import json
import re
import time

from fastapi.testclient import TestClient

from cockpit import auth
from cockpit.app import SECURITY_HEADERS, STATIC_DIR
from tests.conftest import COOKIE_KEY, EMAIL, ORIGIN, TOKEN, World

GOOD_CLAIMS = {
    "iss": "https://accounts.google.com",
    "aud": "client-id",
    "exp": time.time() + 600,
    "email_verified": True,
    "hd": "example.com",
    "email": "sam@example.com",
}


def test_editor_styles_have_a_fresh_nonce_without_allowing_inline_scripts(world: World) -> None:
    nonces = []
    for _ in range(2):
        response = world.client.get("/")
        assert response.status_code == 200
        match = re.search(r'name="cockpit-style-nonce" content="([\w-]+)"', response.text)
        assert match
        nonce = match[1]
        nonces.append(nonce)
        policy = response.headers["content-security-policy"]
        assert policy == SECURITY_HEADERS["Content-Security-Policy"] + f"; style-src 'self' 'nonce-{nonce}'"
        assert "unsafe-inline" not in policy
        assert "script-src" not in policy  # scripts retain default-src 'self'
        assert response.headers["cache-control"] == "no-cache"
    assert nonces[0] != nonces[1]


def test_the_main_page_names_its_files_with_the_build_so_a_reload_asks_for_none_it_has(world: World) -> None:
    # UI-39: a reload revalidated every stylesheet and script; now each carries the build version and a tag of
    # this server process, and is kept.
    page = world.client.get("/").text
    version = world.client.get("/api/state").json()["version"]
    files = re.findall(r'(?:href|src)="(/static/[^"]+)"', page)
    tag = files[0].split("?v=")[1]
    assert tag.startswith(f"{version}-") and all(file.endswith(f"?v={tag}") for file in files)
    assert files.index(f"/static/boot.js?v={tag}") < files.index(f"/static/style.css?v={tag}")
    kept = world.client.get(f"/static/app.js?v={tag}")
    assert kept.status_code == 200 and kept.headers["cache-control"] == "public, max-age=31536000, immutable"
    # Any other name, this build's from an earlier process among them, is asked again each time.
    assert world.client.get(f"/static/app.js?v={version}").headers["cache-control"] == "no-cache"
    assert world.client.get("/static/app.js").headers["cache-control"] == "no-cache"


def id_token(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


def test_admitted_only_for_the_setup_account() -> None:
    now = time.time()
    assert auth.admitted(GOOD_CLAIMS, "client-id", now, EMAIL)
    assert auth.admitted(GOOD_CLAIMS | {"email": "Sam@Example.com"}, "client-id", now, EMAIL)
    for change in (
        {"email": "alex@example.com"},
        {"email_verified": False},
        {"aud": "someone-else"},
        {"exp": now - 1},
        {"hd": "gmail.com"},
        {"iss": "https://evil.example"},
    ):
        assert not auth.admitted(GOOD_CLAIMS | change, "client-id", now, EMAIL), change
    workspace_claims_without_hd = {key: value for key, value in GOOD_CLAIMS.items() if key != "hd"}
    assert not auth.admitted(workspace_claims_without_hd, "client-id", now, EMAIL)


def test_a_personal_google_account_signs_in_without_a_workspace_domain() -> None:
    now = time.time()
    personal = {key: value for key, value in GOOD_CLAIMS.items() if key != "hd"} | {"email": "ana@gmail.com"}
    assert auth.admitted(personal, "client-id", now, "ana@gmail.com")
    assert not auth.admitted(personal | {"hd": "gmail.com"}, "client-id", now, "ana@gmail.com")
    assert not auth.admitted(personal, "client-id", now, EMAIL)
    start = auth.start_sign_in(auth.GoogleClient("client-id", "s" * 24), ORIGIN, "ana@gmail.com")
    assert "hd=" not in start.redirect_url and "login_hint=ana%40gmail.com" in start.redirect_url
    workspace = auth.start_sign_in(auth.GoogleClient("client-id", "s" * 24), ORIGIN, EMAIL)
    assert "hd=example.com" in workspace.redirect_url


def test_session_cookie_signature_and_expiry() -> None:
    key = COOKIE_KEY
    value = auth.session_cookie_value(key, EMAIL, 2_000_000_000)
    assert auth.session_cookie_valid(value, key, EMAIL, 1_999_999_999)
    assert not auth.session_cookie_valid(value, key, EMAIL, 2_000_000_001)
    assert not auth.session_cookie_valid(value, b"x" * 32, EMAIL, 1_999_999_999)
    assert not auth.session_cookie_valid(value[:-1] + "0", key, EMAIL, 1_999_999_999)
    # A new account in the setup file signs the old one out.
    assert not auth.session_cookie_valid(value, key, "ana@gmail.com", 1_999_999_999)


def test_unauthenticated_page_redirects_and_api_401(anonymous: TestClient) -> None:
    assert anonymous.get("/").status_code == 302
    assert anonymous.get("/").headers["location"] == auth.SIGN_IN_PATH
    response = anonymous.get("/api/state")
    assert response.status_code == 401
    assert response.json() == {"detail": "sign in first"}
    assert anonymous.get("/healthz").json() == {"ok": True}




def test_every_page_has_the_look_and_its_marks() -> None:
    # A page without these shows the bare base rules, or blank marks (UI-29, UI-34).
    # The emergency page keeps a look of its own, so it still works when these shared files break (SESSION-21).
    pages = sorted(page for page in STATIC_DIR.glob("*.html") if page.name != "emergency.html")
    assert pages
    for page in pages:
        text = page.read_text()
        assert '<link rel="stylesheet" href="/static/marks.css">' in text, page.name
        assert '<link rel="stylesheet" href="/static/look.css">' in text, page.name
        assert '<script src="/static/icons.js"></script>' in text, page.name


def test_sign_in_round_trip(anonymous: TestClient, world: World) -> None:
    start = anonymous.get(auth.SIGN_IN_PATH)
    assert start.status_code == 302
    assert start.headers["location"].startswith(auth.GOOGLE_AUTHORIZATION_ENDPOINT)
    cookie = start.cookies[auth.SIGN_IN_COOKIE]
    state = cookie.split(".")[0]
    world.exchange_result.update({"id_token": id_token(GOOD_CLAIMS)})
    anonymous.cookies.set(auth.SIGN_IN_COOKIE, cookie)
    done = anonymous.get(auth.CALLBACK_PATH, params={"state": state, "code": "c"})
    assert done.status_code == 302
    assert auth.SESSION_COOKIE in done.headers["set-cookie"]


def test_callback_rejects_mismatched_state(anonymous: TestClient, world: World) -> None:
    start = anonymous.get(auth.SIGN_IN_PATH)
    anonymous.cookies.set(auth.SIGN_IN_COOKIE, start.cookies[auth.SIGN_IN_COOKIE])
    world.exchange_result.update({"id_token": id_token(GOOD_CLAIMS)})
    response = anonymous.get(auth.CALLBACK_PATH, params={"state": "forged", "code": "c"})
    assert response.status_code == 400
    # The error page has the page's look (UI-29).
    assert "<link rel=stylesheet href='/static/look.css'>" in response.text
    assert auth.SESSION_COOKIE not in response.headers.get("set-cookie", "").split("=")[0]


def test_callback_rejects_other_account(anonymous: TestClient, world: World) -> None:
    start = anonymous.get(auth.SIGN_IN_PATH)
    cookie = start.cookies[auth.SIGN_IN_COOKIE]
    anonymous.cookies.set(auth.SIGN_IN_COOKIE, cookie)
    world.exchange_result.update({"id_token": id_token(GOOD_CLAIMS | {"email": "x@example.com"})})
    response = anonymous.get(auth.CALLBACK_PATH, params={"state": cookie.split(".")[0], "code": "c"})
    assert response.status_code == 403
    assert "x@example.com" in response.text


def test_browser_writes_need_cockpit_origin(world: World) -> None:
    body = {"key": "note:a", "title": "A"}
    assert world.client.put("/api/items", json=body).status_code == 403
    assert world.client.put("/api/items", json=body, headers={"origin": "https://evil.example.test"}).status_code == 403
    assert world.client.put("/api/items", json=body, headers=world.user()).status_code == 200
    assert world.client.post("/auth/sign-out").status_code == 403


def test_agent_writes_need_a_name(world: World) -> None:
    client = TestClient(world.client.app, base_url=ORIGIN)
    body = {"key": "note:a", "title": "A"}
    headers = {"authorization": f"Bearer {TOKEN}"}
    response = client.put("/api/items", json=body, headers=headers)
    assert response.status_code == 400
    response = client.put("/api/items", json=body, headers=world.agent("sweep"))
    assert response.status_code == 200
    assert response.json()["updated_by"] == "agent:sweep"
    assert client.put("/api/items", json=body, headers=world.agent("Sam Bot")).status_code == 400


def test_security_headers(world: World) -> None:
    response = world.client.get("/api/state")
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
