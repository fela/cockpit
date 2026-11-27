"""The offline partition identifies a signed-in user and lease without exposing a usable credential."""
import re

import pytest
from fastapi.testclient import TestClient

from cockpit import auth
from tests.conftest import World


def test_offline_identity_is_opaque_stable_and_not_a_credential(world: World) -> None:
    first = world.client.get("/")
    second = world.client.get("/")
    user = first.headers["x-cockpit-offline-user"]
    until = int(first.headers["x-cockpit-offline-until"])
    assert re.fullmatch(r"[0-9a-f]{64}", user)
    assert second.headers["x-cockpit-offline-user"] == user
    assert f'data-offline-user="{user}"' in first.text
    assert f'data-offline-until="{until}"' in first.text
    assert world.client.cookies.get(auth.SESSION_COOKIE) not in first.text
    session = world.client.get("/api/offline/session")
    assert session.json() == {"user": user, "until": until}
    assert f'<meta name="cockpit-version" content="{session.headers["x-cockpit-version"]}">' in first.text
    assert session.headers["cache-control"] == "no-store"


def test_agents_and_signed_out_browsers_cannot_obtain_offline_identity(world: World, anonymous: TestClient) -> None:
    assert anonymous.get("/api/offline/session").status_code == 401
    agent = world.client.get("/", headers=world.agent("reviewer"))
    assert "data-offline-user" not in agent.text
    assert "x-cockpit-offline-user" not in agent.headers
    assert world.client.get("/api/offline/session", headers=world.agent("reviewer")).status_code == 403
    # The opaque partition is not accepted in place of the HttpOnly sign-in cookie.
    user = world.client.get("/").headers["x-cockpit-offline-user"]
    assert anonymous.get("/api/state", headers={"Authorization": f"Bearer {user}"}).status_code == 401


def test_sign_out_and_invalid_cookie_never_receive_offline_shell_identity(world: World, anonymous: TestClient) -> None:
    signed_out = world.client.post("/auth/sign-out", headers=world.user())
    assert signed_out.status_code == 200
    assert "data-offline-user" not in signed_out.text
    assert "x-cockpit-offline-user" not in signed_out.headers
    assert "Max-Age=0" in signed_out.headers["set-cookie"]
    assert anonymous.get("/").status_code == 302
    anonymous.cookies.set(auth.SESSION_COOKIE, "expired.not-a-signature")
    assert anonymous.get("/api/offline/session").status_code == 401


@pytest.mark.parametrize("path", ["/pages", "/spec", "/skills", "/skills/writing", "/writing", "/briefing", "/usage", "/server"])
def test_helper_pages_receive_only_the_authenticated_users_outbox_scope(world: World, anonymous: TestClient, path: str) -> None:
    identity = world.client.get("/api/offline/session").json()
    page = world.client.get(path, headers={"Accept-Encoding": "identity"})
    assert page.status_code == 200
    assert f'data-offline-user="{identity["user"]}"' in page.text
    assert f'data-offline-until="{identity["until"]}"' in page.text
    assert 'data-offline-helper="1"' in page.text
    assert int(page.headers["content-length"]) == len(page.content)
    assert world.client.cookies.get(auth.SESSION_COOKIE) not in page.text
    assert "frame-ancestors 'self'" in page.headers["content-security-policy"]
    agent = world.client.get(path, headers=world.agent("reviewer"))
    assert agent.status_code == 200 and "data-offline-user" not in agent.text
    unsigned = anonymous.get(path)
    assert unsigned.status_code == 302 and "data-offline-user" not in unsigned.text


@pytest.mark.parametrize("markup", [
    '<!doctype html><!-- literal <html lang="en"> --><h1>Figure</h1>',
    '<!doctype html><script>const example = "<html lang=\\"en\\">";</script><h1>Figure</h1>',
    '<!doctype html><!-- lead --><HTML lang="en"><h1>Figure</h1></HTML>',
])
def test_private_figures_receive_scope_without_changing_scripts_or_public_pages(world: World, anonymous: TestClient,
                                                                              visitor: TestClient, markup: str) -> None:
    from tests.test_page_comments import setup, agent
    page, _ = setup(world)
    updated = agent(world).put(f'/api/item-pages/{page["id"]}', json={"title": "Figure", "html": markup},
                               headers=world.agent("app")).json()
    identity = world.client.get("/api/offline/session").json()
    private = world.client.get(f'/pages/{page["id"]}/figure')
    assert private.status_code == 200
    assert private.text.startswith("<!doctype html>")
    assert f'data-offline-user="{identity["user"]}"' in private.text
    assert f'data-offline-until="{identity["until"]}"' in private.text
    assert private.text.count('data-offline-user="') == 1
    if "<script>" in markup:
        assert '<script>const example = "<html lang=\\"en\\">";</script>' in private.text
    assert "data-offline-user" not in world.client.get(f'/pages/{page["id"]}/figure', headers=world.agent("reviewer")).text
    assert anonymous.get(f'/pages/{page["id"]}/figure').status_code == 302
    shared = world.client.post(f'/api/item-pages/{page["id"]}/public', json={"revision": updated["revision"]}, headers=world.user()).json()
    public = visitor.get(shared["public_url"])
    assert public.status_code == 200 and public.text == markup
    assert "data-offline-user" not in public.text
