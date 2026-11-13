"""Connector sign-ins from a card (SPEC SESSION-25): a session asks with `cockpit signin start`, Cockpit makes the
link, the user pastes back the address they land on, and Cockpit stores the sign-in where Claude Code reads it.

The connector and its sign-in server are a fake (`FakeConnector`) answering `connector_signin._request`: OAuth
discovery, a token endpoint that checks the PKCE verifier against the link's challenge, and an MCP endpoint that
lists three tools for a token it issued."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import stat
import time
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from cockpit import cli, config, connector_signin
from tests.conftest import SESSION_A, SESSION_B, World
from tests.test_api import agent_client, put_item, put_step

MCP_URL = "https://mcp.example.test/mcp"
ISSUER = "https://auth.example.test"
ITEM_KEY = "gh:example-org/app#10"


class FakeConnector:
    """An MCP connector with an OAuth server of its own, as Linear's is."""

    def __init__(self) -> None:
        self.issued: dict[str, str] = {}  # code -> the challenge of the link it answers
        self.live_tokens: set[str] = set()
        self.refreshes: set[str] = set()
        self.counter = 0
        self.calls: list[str] = []
        self.unreachable = False
        self.gives_refresh = True
        self.tools_down = False

    def approve(self, link: str) -> str:
        """What the user's browser lands on after approving the link."""
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(link).query))
        code = f"code-{len(self.issued)}"
        self.issued[code] = query["code_challenge"]
        return f"{query['redirect_uri']}?code={code}&state={query['state']}"

    def _tokens(self) -> dict[str, Any]:
        self.counter += 1
        access, refresh = f"access-{self.counter}", f"refresh-{self.counter}"
        self.live_tokens.add(access)
        tokens = {"access_token": access, "expires_in": 3600, "token_type": "Bearer"}
        if self.gives_refresh:
            self.refreshes.add(refresh)
            tokens["refresh_token"] = refresh
        return tokens

    def request(self, url: str, data: bytes | None, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        if self.unreachable:
            raise OSError("connection refused")
        self.calls.append(url)
        if url == "https://mcp.example.test/.well-known/oauth-protected-resource/mcp":
            return 200, {}, json.dumps({"resource": MCP_URL, "authorization_servers": [ISSUER],
                                        "scopes_supported": ["read"]}).encode()
        if url == f"{ISSUER}/.well-known/oauth-authorization-server":
            return 200, {}, json.dumps({
                "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize", "token_endpoint": f"{ISSUER}/token",
                "client_id_metadata_document_supported": True,
            }).encode()
        if url == f"{ISSUER}/token":
            form = dict(urllib.parse.parse_qsl((data or b"").decode()))
            if form["grant_type"] == "authorization_code":
                challenge = self.issued.pop(form.get("code", ""), None)
                verifier = form.get("code_verifier", "")
                expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                if challenge is None or challenge != expected:
                    # A server that echoes the code it refused, which the card must not repeat.
                    description = f"code {form.get('code')} not valid"
                    return 400, {}, json.dumps({"error": "invalid_grant", "error_description": description}).encode()
                assert form["resource"] == MCP_URL
                return 200, {}, json.dumps(self._tokens()).encode()
            if form["grant_type"] == "refresh_token" and form["refresh_token"] in self.refreshes:
                self.refreshes.discard(form["refresh_token"])
                return 200, {}, json.dumps(self._tokens()).encode()
            return 400, {}, json.dumps({"error": "invalid_grant"}).encode()
        if url == MCP_URL:
            if self.tools_down:
                return 503, {}, b""
            token = headers.get("Authorization", "").removeprefix("Bearer ")
            if token not in self.live_tokens:
                return 401, {}, b""
            message = json.loads(data or b"{}")
            if "id" not in message:
                return 202, {}, b""
            if message["method"] == "initialize":
                result = {"protocolVersion": connector_signin.MCP_PROTOCOL, "capabilities": {}}
                return 200, {"Mcp-Session-Id": "s-1", "Content-Type": "application/json"}, json.dumps(
                    {"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
            assert headers.get("Mcp-Session-Id") == "s-1"
            tools = [{"name": name} for name in ("search", "read", "write")]
            event = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {"tools": tools}})
            return 200, {"Content-Type": "text/event-stream"}, f"event: message\ndata: {event}\n\n".encode()
        return 404, {}, b""


@pytest.fixture
def connector(monkeypatch) -> FakeConnector:
    fake = FakeConnector()
    monkeypatch.setattr(connector_signin, "_request", fake.request)
    return fake


@pytest.fixture
def account(claude_accounts) -> config.Account:
    """The personal account, configured with the connector `linear`, signed out."""
    personal = claude_accounts[0]
    personal.folder.mkdir(parents=True)
    personal.state.write_text(json.dumps({"mcpServers": {
        "linear": {"type": "http", "url": MCP_URL},
        "local": {"type": "stdio", "command": "local-mcp"},
    }}))
    (personal.folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "keep-me"}}))
    (personal.folder / "mcp-needs-auth-cache.json").write_text(json.dumps({"linear": {"timestamp": 1}}))
    return personal


def credentials(account: config.Account) -> dict[str, Any]:
    return json.loads((account.folder / ".credentials.json").read_text())


def linear_key() -> str:
    return connector_signin.credential_key("linear", {"type": "http", "url": MCP_URL})


def start(world: World, server: str = "linear", account_name: str = "personal") -> Any:
    put_item(world, sessions=[SESSION_A])
    return agent_client(world).post("/api/signins", json={
        "account": account_name, "server": server, "item_key": ITEM_KEY, "why": "Reads the ticket ENG-1",
        "session_id": SESSION_A,
    }, headers=world.agent("ticket"))


def finish(world: World, step_id: int, address: str) -> Any:
    return world.client.post(f"/api/steps/{step_id}/signin", json={"address": address}, headers=world.user())


def test_credential_key_is_the_one_claude_code_files_tokens_under() -> None:
    # Claude Code's key: the name, then the first 16 hex digits of sha256 over the compact JSON of type, url and
    # headers.
    shape = '{"type":"http","url":"https://mcp.example.test/mcp","headers":{}}'
    assert linear_key() == f"linear|{hashlib.sha256(shape.encode()).hexdigest()[:16]}"


def test_a_signed_out_connector_gets_a_card_and_the_pasted_address_signs_it_in(
    world: World, connector: FakeConnector, account: config.Account
) -> None:
    response = start(world)
    assert response.status_code == 200, response.text
    step = response.json()["step"]
    signin = step["signin"]
    assert step["need"].startswith("signin:linear:")
    assert step["session_id"] == SESSION_A
    assert signin["account"] == "personal" and signin["host"] == "auth.example.test" and signin["failure"] is None
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(signin["link"]).query))
    assert query["client_id"] == connector_signin.CLAUDE_CLIENT_METADATA
    assert query["code_challenge_method"] == "S256" and query["resource"] == MCP_URL and query["scope"] == "read"
    # The verifier waits in a file only Cockpit reads, and nowhere on the step.
    folder = world.settings.signins_dir
    pending = folder / f"{step['id']}.json"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700 and stat.S_IMODE(pending.stat().st_mode) == 0o600
    assert json.loads(pending.read_text())["verifier"] not in json.dumps(step)

    address = connector.approve(signin["link"])
    done = finish(world, step["id"], address)
    assert done.status_code == 200, done.text
    body = done.json()
    assert "failure" not in body
    # The first token was renewed once, so the stored one is the second; the other sign-in stays.
    stored = credentials(account)
    entry = stored["mcpOAuth"][linear_key()]
    assert entry["accessToken"] == "access-2" and entry["refreshToken"] == "refresh-2"
    assert entry["clientId"] == connector_signin.CLAUDE_CLIENT_METADATA and entry["serverUrl"] == MCP_URL
    assert entry["expiresAt"] > time.time() * 1000
    assert stored["claudeAiOauth"] == {"accessToken": "keep-me"}
    assert stat.S_IMODE((account.folder / ".credentials.json").stat().st_mode) == 0o600
    assert json.loads((account.folder / "mcp-needs-auth-cache.json").read_text()) == {}
    assert not pending.exists()
    # The session hears the result, with no code or token in it, and holds the step.
    text = body["message"]["text"]
    assert "lists 3 tools" in text and "renewed it once" in text
    assert "code-0" not in text and "access-" not in text and "refresh-" not in text
    after = body["step"]
    assert after["handoff_message_id"] == body["message"]["id"]
    assert after["signin"]["finished_at"] and after["signin"]["tools"] == 3
    with sqlite3.connect(world.db_path) as conn:
        dump = "\n".join(conn.iterdump())
    assert "code-0" not in dump and "access-" not in dump


def test_a_working_sign_in_needs_no_card(world: World, connector: FakeConnector, account: config.Account) -> None:
    connector.live_tokens.add("access-live")
    connector_signin.write_entry(account, linear_key(), {
        "accessToken": "access-live", "clientId": "c", "expiresAt": int((time.time() + 3600) * 1000),
    })
    response = start(world)
    assert response.status_code == 200, response.text
    assert response.json() == {"working": {"tools": 3, "expires_at": pytest.approx(time.time() * 1000 + 3600_000, abs=60_000),
                                           "renews": False}}
    with sqlite3.connect(world.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM steps WHERE need GLOB 'signin:*'").fetchone() == (0,)


def test_an_ended_token_renews_without_a_card(world: World, connector: FakeConnector, account: config.Account) -> None:
    connector.refreshes.add("refresh-old")
    connector_signin.write_entry(account, linear_key(), {
        "accessToken": "access-old", "refreshToken": "refresh-old", "clientId": "c",
        "expiresAt": int((time.time() - 10) * 1000),
    })
    response = start(world)
    assert response.json()["working"]["renews"] is True
    entry = credentials(account)["mcpOAuth"][linear_key()]
    assert entry["accessToken"] == "access-1" and entry["refreshToken"] == "refresh-1"


def test_a_renewed_token_is_kept_when_the_check_after_it_fails(
    world: World, connector: FakeConnector, account: config.Account
) -> None:
    # The server replaces the refresh token on renewal, so losing the new one would end the sign-in.
    connector.refreshes.add("refresh-old")
    connector.tools_down = True
    connector_signin.write_entry(account, linear_key(), {
        "accessToken": "access-old", "refreshToken": "refresh-old", "clientId": "c",
        "expiresAt": int((time.time() - 10) * 1000),
    })
    assert "step" in start(world).json()
    assert credentials(account)["mcpOAuth"][linear_key()]["refreshToken"] == "refresh-1"


def test_a_new_sign_in_keeps_no_refresh_token_from_before(
    world: World, connector: FakeConnector, account: config.Account
) -> None:
    # An earlier refresh token may be another account's; renewing with it would switch the connector back.
    connector.gives_refresh = False
    step = start(world).json()["step"]
    connector.refreshes.add("refresh-other-account")
    connector_signin.write_entry(account, linear_key(), {
        "accessToken": "access-gone", "refreshToken": "refresh-other-account", "clientId": "c",
    })
    assert "message" in finish(world, step["id"], connector.approve(step["signin"]["link"])).json()
    entry = credentials(account)["mcpOAuth"][linear_key()]
    assert entry["accessToken"] == "access-1" and "refreshToken" not in entry


def test_asking_again_shows_the_same_card(world: World, connector: FakeConnector, account: config.Account) -> None:
    first = start(world).json()["step"]
    again = start(world).json()
    assert again["reused"] is True and again["step"]["id"] == first["id"]
    assert again["step"]["signin"]["link"] == first["signin"]["link"]


def test_each_account_gets_its_own_card_and_its_session_hears_back(
    world: World, connector: FakeConnector, account: config.Account, claude_accounts
) -> None:
    # A second account's ask on the same item and connector used to take over the first card, which still told the
    # first session.
    work = claude_accounts[1]
    work.folder.mkdir(parents=True)
    work.state.write_text(account.state.read_text())
    personal = start(world).json()["step"]
    put_item(world, sessions=[SESSION_A, SESSION_B])
    response = agent_client(world).post("/api/signins", json={
        "account": "work", "server": "linear", "item_key": ITEM_KEY, "why": "Reads the ticket ENG-2",
        "session_id": SESSION_B,
    }, headers=world.agent("ticket"))
    assert response.status_code == 200, response.text
    second = response.json()["step"]
    assert second["id"] != personal["id"] and second["need"] != personal["need"]
    assert second["signin"]["account"] == "work" and second["session_id"] == SESSION_B
    assert start(world).json()["step"]["signin"] == personal["signin"]

    body = finish(world, second["id"], connector.approve(second["signin"]["link"])).json()
    assert body["message"]["session_id"] == SESSION_B and "work Claude account" in body["message"]["text"]
    assert linear_key() in credentials(work)["mcpOAuth"]
    assert linear_key() not in credentials(account).get("mcpOAuth", {})
    with sqlite3.connect(world.db_path) as conn:
        assert conn.execute("SELECT handoff_message_id FROM steps WHERE id = ?", (personal["id"],)).fetchone() == (None,)


def test_an_address_from_an_older_link_keeps_the_card_link(
    world: World, connector: FakeConnector, account: config.Account
) -> None:
    step = start(world).json()["step"]
    address = connector.approve(step["signin"]["link"]).replace("state=", "state=old")
    body = finish(world, step["id"], address).json()
    assert "older sign-in link" in body["failure"]
    assert body["step"]["signin"]["link"] == step["signin"]["link"]
    assert body["step"]["signin"]["failure"] == body["failure"]
    assert (world.settings.signins_dir / f"{step['id']}.json").exists()
    # The right address still works after it.
    done = finish(world, step["id"], connector.approve(step["signin"]["link"])).json()
    assert "message" in done


def test_a_refused_code_puts_a_new_link_on_the_card(world: World, connector: FakeConnector, account: config.Account) -> None:
    step = start(world).json()["step"]
    connector.approve(step["signin"]["link"])
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(step["signin"]["link"]).query))["state"]
    body = finish(world, step["id"], f"http://localhost:5000/callback?code=forged&state={state}").json()
    assert body["failure"] == "The sign-in server refused the code: invalid_grant. Open the new link and approve again."
    assert "forged" not in json.dumps(body)
    assert body["step"]["signin"]["link"] != step["signin"]["link"]
    assert body["step"]["signin"]["failure"] == body["failure"]
    assert linear_key() not in credentials(account).get("mcpOAuth", {})
    # The new link signs in.
    assert "message" in finish(world, step["id"], connector.approve(body["step"]["signin"]["link"])).json()


def test_an_unreachable_server_keeps_the_link(world: World, connector: FakeConnector, account: config.Account) -> None:
    step = start(world).json()["step"]
    address = connector.approve(step["signin"]["link"])
    connector.unreachable = True
    body = finish(world, step["id"], address).json()
    assert body["failure"] == "Cockpit could not reach auth.example.test. Paste the address again in a moment."
    assert body["step"]["signin"]["link"] == step["signin"]["link"]
    connector.unreachable = False
    assert "message" in finish(world, step["id"], address).json()


def test_a_declined_sign_in_shows_only_the_error_code(
    world: World, connector: FakeConnector, account: config.Account
) -> None:
    step = start(world).json()["step"]
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(step["signin"]["link"]).query))["state"]
    address = f"http://localhost:5000/callback?error=access_denied&error_description=secret-words&state={state}"
    body = finish(world, step["id"], address).json()
    assert body["failure"].startswith("The sign-in page answered: access_denied.")
    assert "secret-words" not in json.dumps(body)


def test_an_address_without_a_code_says_what_to_paste(world: World, connector: FakeConnector, account: config.Account) -> None:
    step = start(world).json()["step"]
    body = finish(world, step["id"], "http://localhost:5000/").json()
    assert body["failure"].startswith("That address holds no code.")


def test_only_the_user_finishes_and_only_a_session_starts(
    world: World, connector: FakeConnector, account: config.Account
) -> None:
    step = start(world).json()["step"]
    address = connector.approve(step["signin"]["link"])
    as_agent = agent_client(world).post(f"/api/steps/{step['id']}/signin", json={"address": address},
                                        headers=world.agent())
    assert as_agent.status_code == 403
    as_user = world.client.post("/api/signins", json={
        "account": "personal", "server": "linear", "item_key": ITEM_KEY, "why": "x", "session_id": SESSION_A,
    }, headers=world.user())
    assert as_user.status_code == 403


def test_unknown_or_local_servers_are_refused(world: World, connector: FakeConnector, account: config.Account) -> None:
    missing = start(world, server="notion")
    assert missing.status_code == 400 and "it has linear, local" in missing.text
    local = start(world, server="local")
    assert local.status_code == 400 and "not an HTTP server" in local.text
    assert start(world, account_name="team").status_code == 400


def test_a_sign_in_card_sits_beside_the_items_card(world: World, connector: FakeConnector, account: config.Account) -> None:
    put_item(world, sessions=[SESSION_A])
    put_step(world)
    assert start(world).status_code == 200
    put_step(world, ask="Approve PR 10 now")


def test_a_session_cannot_put_a_sign_in_step_itself(world: World) -> None:
    put_item(world, sessions=[SESSION_A])
    body = {"item_key": ITEM_KEY, "need": "signin:linear:1", "ask": "Sign in to Linear"}
    response = agent_client(world).post("/api/steps", json=body, headers=world.agent())
    assert response.status_code == 400 and "cockpit signin start" in response.text


def test_the_message_box_refuses_the_sign_in_address(world: World, connector: FakeConnector, account: config.Account) -> None:
    step = start(world).json()["step"]
    address = connector.approve(step["signin"]["link"])
    response = world.client.post(f"/api/steps/{step['id']}/hand-off", json={"text": f"here: {address}"},
                                 headers=world.user())
    assert response.status_code == 400 and "sign-in box" in response.text
    # Other text on the step goes to the session as usual.
    assert world.client.post(f"/api/steps/{step['id']}/hand-off", json={"text": "Use the work workspace"},
                             headers=world.user()).status_code == 200


def test_a_configured_client_signs_in_on_its_own_port(account: config.Account, connector: FakeConnector) -> None:
    # Slack's server config names its client and callback port; the link uses them.
    found = connector_signin.discover(MCP_URL)
    client = connector_signin._client({"type": "http", "url": MCP_URL, "oauth": {"clientId": "slack-client", "callbackPort": 3118}},
                                      None, found)
    assert client == {"client_id": "slack-client", "client_secret": None, "redirect_uri": "http://localhost:3118/callback"}


def test_the_session_account_follows_claude_config_dir(monkeypatch, claude_accounts, tmp_path: Path) -> None:
    work = claude_accounts[1]
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert cli._session_account() == "personal"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(work.folder))
    assert cli._session_account() == "work"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    with pytest.raises(cli.CockpitError):
        cli._session_account()
