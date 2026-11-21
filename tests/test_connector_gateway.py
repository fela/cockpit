from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from mcp import types

from cockpit import connector_gateway, gateway_cli, gateway_oauth
from cockpit.gateway_store import PROVIDERS, GatewayError, Store

SCHEMA = {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False}
DISCOVERY = {"issuer": "https://mcp.linear.app", "authorize": "https://mcp.linear.app/authorize",
             "token": "https://mcp.linear.app/token", "resource": PROVIDERS["linear"],
             "registration": "https://mcp.linear.app/register"}


def configure(store, provider="linear", expires=None):
    store.configure(provider, {"client_id": "cockpit-own-client", "client_secret": None,
                               "redirect_uri": "http://localhost:8766/callback", "scope": "read"})
    store.save_identity(provider, "grant", {"access_token": "upstream-private-token", "refresh_token": "refresh-private",
                                             "expires": expires, "discovery": DISCOVERY})


class FakeUpstream:
    def __init__(self):
        self.requests = []
        self.calls = []
        self.before_call = None
        self.before_list = None
        self.schema = SCHEMA
        self.names = ["get_issue", "create_issue", "get_delete_everything", "slack_search_messages", "notion-fetch"]
        self.fail_call = False
        self.invalid_json = False
        self.redirect_call = False

    def answer(self, request):
        self.requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(200)
        message = json.loads(request.content)
        method = message["method"]
        if "id" not in message:
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": message["params"]["protocolVersion"], "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fake connector", "version": "1"}}
        elif method == "tools/list":
            if self.before_list:
                self.before_list()
            result = {"tools": [{"name": name, "description": name, "inputSchema": self.schema,
                                  "annotations": {"readOnlyHint": True}} for name in self.names]}
        elif method == "tools/call":
            if self.before_call:
                self.before_call()
            self.calls.append(message["params"])
            if self.redirect_call:
                return httpx.Response(307, headers={"location": "/mcp/again"})
            if self.invalid_json:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "error": "FAKE_PRIVATE_PROVIDER_CONTENT"})
            if self.fail_call:
                raise httpx.ReadTimeout("upstream secret body must stay private")
            result = {"content": [{"type": "text", "text": "upstream result"}]}
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})


@pytest.fixture
def gateway(world, monkeypatch):
    gateway = world.client.app.state.connector_gateway
    configure(gateway.store)
    identity, token = gateway.store.create_client("Laptop Codex", "read")
    upstream = FakeUpstream()
    original = httpx.AsyncClient

    def client(**kwargs):
        return original(**kwargs, transport=httpx.MockTransport(upstream.answer))

    monkeypatch.setattr(connector_gateway.httpx, "AsyncClient", client)
    with TestClient(world.client.app, base_url="http://127.0.0.1:18790", client=("127.0.0.1", 5000)) as browser:
        yield browser, gateway, identity, token, upstream


def rpc(client, token, method, params=None, **kwargs):
    return client.post(connector_gateway.MCP_PATH, headers={"Authorization": "Bearer " + token,
                       "Accept": "application/json, text/event-stream", **kwargs.pop("headers", {})},
                       json={"jsonrpc": "2.0", "id": 7, "method": method, **({"params": params} if params else {})}, **kwargs)


def call(gateway, name="linear__get_issue", arguments=None):
    client, _, _, token, _ = gateway
    return rpc(client, token, "tools/call", {"name": name, "arguments": arguments if arguments is not None else {"id": "A-12"}})


def test_sdk_initialize_and_only_configured_reviewed_reads_are_listed(gateway):
    client, _, _, token, _ = gateway
    initialized = rpc(client, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                   "clientInfo": {"name": "test", "version": "1"}})
    assert initialized.status_code == 200
    listed = rpc(client, token, "tools/list")
    assert listed.status_code == 200, listed.text
    assert [tool["name"] for tool in listed.json()["result"]["tools"]] == [connector_gateway.STATUS_TOOL, "linear__get_issue"]


def test_client_access_update_preserves_token_and_leaves_other_clients_read_only(gateway):
    client, server, identity, token, _ = gateway
    other, other_token = server.store.create_client("Laptop Claude", "read")
    server.store.update_access(identity, "read_write")
    assert server.store.authenticate("Bearer " + token) == identity
    assert server.store.authenticate("Bearer " + other_token) == other
    assert server.store.access(other) == "read"
    assert call(gateway, connector_gateway.STATUS_TOOL, {}).json()["result"]["structuredContent"]["mode"] == "read_write"
    assert call(gateway, "linear__create_issue").json()["result"]["isError"] is False
    rejected = rpc(client, other_token, "tools/call", {"name": "linear__create_issue", "arguments": {"id": "A"}})
    assert rejected.json()["result"]["isError"] is True
    server.store.update_access(identity, "read")
    assert call(gateway, connector_gateway.STATUS_TOOL, {}).json()["result"]["structuredContent"]["mode"] == "read"
    assert call(gateway, "linear__create_issue").json()["result"]["isError"] is True


@pytest.mark.parametrize(("provider", "tool"), [
    ("linear", "create_issue"), ("linear", "update_issue"), ("linear", "delete_issue"),
    ("slack", "slack_send_message"), ("notion", "notion-create-pages"), ("notion", "notion-publish-page"),
])
def test_write_client_calls_each_published_mutation_without_approval(gateway, provider, tool):
    _, server, identity, _, upstream = gateway
    configure(server.store, provider)
    server.store.update_access(identity, "read_write")
    upstream.names = [tool]
    response = call(gateway, provider + "__" + tool)
    assert response.json()["result"]["isError"] is False
    assert upstream.calls == [{"name": tool, "arguments": {"id": "A-12"}}]
    with server.store.connection() as conn:
        assert [row[0] for row in conn.execute("SELECT status FROM audit WHERE tool=?", (tool,))][-2:] == ["write.started", "write.completed"]


def test_write_catalog_uses_original_schemas_and_conservative_annotations(gateway):
    client, server, identity, token, upstream = gateway
    server.store.update_access(identity, "read_write")
    upstream.names = ["get_issue", "create_issue", "get_delete_everything"]
    tools = rpc(client, token, "tools/list").json()["result"]["tools"]
    by_name = {tool["name"]: tool for tool in tools}
    for name in upstream.names:
        tool = by_name["linear__" + name]
        assert tool["inputSchema"] == SCHEMA
        assert tool["annotations"]["readOnlyHint"] == (name == "get_issue")
        assert tool["annotations"]["idempotentHint"] == (name == "get_issue")
        assert tool["annotations"]["destructiveHint"] == (name != "get_issue")


def test_read_catalog_uses_current_role_after_a_downgrade_during_listing(gateway):
    client, server, identity, token, upstream = gateway
    server.store.update_access(identity, "read_write")
    upstream.before_list = lambda: server.store.update_access(identity, "read")
    tools = rpc(client, token, "tools/list").json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == [connector_gateway.STATUS_TOOL, "linear__get_issue"]


@pytest.mark.parametrize("change", ["downgrade", "revoke", "signout", "reconfigure"])
def test_changed_authority_during_schema_fetch_stops_write(gateway, change):
    client, server, identity, token, upstream = gateway
    server.store.update_access(identity, "read_write")
    assert rpc(client, token, "tools/list").status_code == 200

    def invalidate():
        upstream.before_list = None
        if change == "downgrade":
            server.store.update_access(identity, "read")
        elif change == "revoke":
            server.store.revoke(identity)
        elif change == "signout":
            server.store.signout("linear")
        else:
            configure(server.store)

    upstream.before_list = invalidate
    assert call(gateway, "linear__create_issue").json()["result"]["isError"] is True
    assert not upstream.calls


@pytest.mark.parametrize("arguments", [{}, {"id": 7}, {"id": "A", "extra": True}])
def test_invalid_write_arguments_never_reach_service(gateway, arguments):
    gateway[1].store.update_access(gateway[2], "read_write")
    assert call(gateway, "linear__create_issue", arguments).json()["result"]["isError"] is True
    assert not gateway[-1].calls


@pytest.mark.parametrize("tool", ["linear__unpublished", "linear__create_issue/other", "other__create_issue"])
def test_write_authority_still_requires_a_valid_published_tool(gateway, tool):
    gateway[1].store.update_access(gateway[2], "read_write")
    assert call(gateway, tool).json()["result"]["isError"] is True
    assert not gateway[-1].calls


def test_write_tool_alias_and_local_schema_root_are_forwarded_exactly(gateway):
    client, server, identity, token, upstream = gateway
    server.store.update_access(identity, "read_write")
    upstream.names = ["alias__publish"]
    upstream.schema = {"type": "object", "$defs": {"identifier": {"type": "string"}},
                       "properties": {"id": {"$ref": "#/$defs/identifier"}}, "required": ["id"], "additionalProperties": False}
    listed = rpc(client, token, "tools/list").json()["result"]["tools"]
    assert listed[1]["name"] == "linear__alias__publish"
    assert listed[1]["inputSchema"] == upstream.schema
    assert call(gateway, "linear__alias__publish").json()["result"]["isError"] is False
    assert upstream.calls == [{"name": "alias__publish", "arguments": {"id": "A-12"}}]
    assert call(gateway, "linear__publish").json()["result"]["isError"] is True


def test_write_notion_query_keeps_authentic_modes_and_schema(gateway):
    client, server, identity, token, upstream = gateway
    server.store.update_access(identity, "read_write")
    configure(server.store, "notion")
    upstream.names = ["notion-query-data-sources"]
    upstream.schema = {"type": "object", "$defs": {"mode": {"enum": ["rows", "view", "sql"]}},
                       "properties": {"mode": {"$ref": "#/$defs/mode"}}, "required": ["mode"]}
    listed = rpc(client, token, "tools/list").json()["result"]["tools"]
    notion = next(tool for tool in listed if tool["name"] == "notion__notion-query-data-sources")
    assert notion["inputSchema"] == upstream.schema
    assert notion["annotations"]["readOnlyHint"] is False
    assert call(gateway, notion["name"], {"mode": "sql"}).json()["result"]["isError"] is False
    server.store.update_access(identity, "read")
    assert call(gateway, notion["name"], {"mode": "sql"}).json()["result"]["isError"] is True


def test_gateway_preserves_attributed_authored_text_without_content_rewriting(gateway):
    _, server, identity, _, upstream = gateway
    configure(server.store, "slack")
    server.store.update_access(identity, "read_write")
    upstream.names = ["slack_send_message"]
    upstream.schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    payload = {"text": "**[Codex]** _written and posted by Codex on Sam's behalf_\nThe release is ready."}
    assert call(gateway, "slack__slack_send_message", payload).json()["result"]["isError"] is False
    assert upstream.calls == [{"name": "slack_send_message", "arguments": payload}]


@pytest.mark.parametrize("failure", ["timeout", "redirect", "malformed_response"])
def test_uncertain_write_has_one_post_and_safe_unknown_result(gateway, failure, monkeypatch, caplog):
    _, server, identity, _, upstream = gateway
    server.store.update_access(identity, "read_write")
    monkeypatch.setattr(connector_gateway, "TIMEOUT", 5)
    deadlines = []
    timeout = connector_gateway.asyncio.timeout

    def tracked_timeout(seconds):
        deadline = timeout(seconds)
        deadlines.append(deadline)
        return deadline

    monkeypatch.setattr(connector_gateway.asyncio, "timeout", tracked_timeout)
    upstream.fail_call = failure == "timeout"
    upstream.redirect_call = failure == "redirect"
    upstream.invalid_json = failure == "malformed_response"
    response = call(gateway, "linear__create_issue", {"id": "private-write-input"})
    result = response.json()["result"]
    assert result["isError"] is True
    assert connector_gateway.WRITE_UNKNOWN in result["content"][0]["text"]
    # Invalid replies and transport failures end the call before its deadline.
    assert deadlines and not any(deadline.expired() for deadline in deadlines)
    assert len(upstream.calls) == 1
    assert all(str(request.url) == PROVIDERS["linear"] for request in upstream.requests)
    with server.store.connection() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM audit WHERE tool='create_issue'")]
    assert [row["status"] for row in rows] == ["write.started", "write.unknown"]
    assert "private-write-input" not in json.dumps(rows)
    for marker in ("private-write-input", "upstream secret body", "FAKE_PRIVATE_PROVIDER_CONTENT", "upstream-private-token"):
        assert marker not in caplog.text


def test_completed_write_with_failed_audit_still_reports_unknown_safely(gateway, monkeypatch, caplog):
    _, server, identity, _, upstream = gateway
    server.store.update_access(identity, "read_write")
    audit = server.store.audit

    def unavailable_audit(conn, status, client, provider, tool):
        if status in {"write.completed", "write.unknown"}:
            raise sqlite3.OperationalError("private audit failure detail")
        audit(conn, status, client, provider, tool)

    monkeypatch.setattr(server.store, "audit", unavailable_audit)
    result = call(gateway, "linear__create_issue").json()["result"]
    assert result["isError"] is True
    assert connector_gateway.WRITE_UNKNOWN in result["content"][0]["text"]
    assert len(upstream.calls) == 1
    assert "Connector audit failed" in caplog.text
    assert "Connector write outcome unknown" in caplog.text
    assert "private audit failure detail" not in caplog.text


def test_unavailable_connector_is_visible_without_hiding_healthy_provider(gateway, caplog):
    client, server, _, token, _ = gateway
    configure(server.store, "slack", expires=0)
    grant = server.store.provider("slack")["grant"] | {"refresh_token": None}
    server.store.save_identity("slack", "grant", grant)
    listed = rpc(client, token, "tools/list").json()["result"]["tools"]
    assert [tool["name"] for tool in listed] == [connector_gateway.STATUS_TOOL, "linear__get_issue"]
    status = call(gateway, connector_gateway.STATUS_TOOL, {}).json()["result"]["structuredContent"]
    rows = {row["provider"]: row for row in status["connectors"]}
    assert rows["linear"]["available"] is True
    assert rows["slack"]["available"] is False
    assert "expired" in rows["slack"]["error"]
    assert rows["notion"]["configured"] is False
    assert call(gateway).json()["result"]["isError"] is False
    assert "provider=slack" in caplog.text
    assert "upstream-private-token" not in caplog.text


def test_overlapping_catalogs_share_only_the_pending_lookup_and_filter_by_current_role(gateway, monkeypatch):
    _, server, identity, _, upstream = gateway
    writer, _ = server.store.create_client("Writer", "read_write")
    original = server.tools

    async def exercise():
        pending = asyncio.Event()
        release = asyncio.Event()

        async def hold_tools(session):
            pending.set()
            await release.wait()
            return await original(session)

        monkeypatch.setattr(server, "tools", hold_tools)
        abandoned = asyncio.create_task(server.catalog(identity))
        await pending.wait()
        reader_check = asyncio.create_task(server.catalog(identity))
        writer_check = asyncio.create_task(server.catalog(writer))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        abandoned.cancel()
        with pytest.raises(asyncio.CancelledError):
            await abandoned
        release.set()
        (reader_tools, _, _), (writer_tools, _, _) = await asyncio.gather(reader_check, writer_check)
        assert [tool.name for tool in reader_tools] == [connector_gateway.STATUS_TOOL, "linear__get_issue"]
        assert "linear__create_issue" in [tool.name for tool in writer_tools]
        assert len([request for request in upstream.requests if b'"method":"tools/list"' in request.content]) == 1

        await server.catalog(identity)
        assert len([request for request in upstream.requests if b'"method":"tools/list"' in request.content]) == 2

    asyncio.run(exercise())


def test_catalog_rechecks_signout_while_waiting_for_other_providers(gateway, caplog, monkeypatch):
    _, server, *_ = gateway
    linear_finished = asyncio.Event()
    original = server.provider_catalog

    async def check(provider):
        result = await original(provider)
        if provider == "linear":
            linear_finished.set()
        elif provider == "slack":
            await linear_finished.wait()
            if server.store.provider("linear")["grant"]:
                server.store.signout("linear")
        return result

    monkeypatch.setattr(server, "provider_catalog", check)
    result = call(gateway, connector_gateway.STATUS_TOOL, {}).json()["result"]["structuredContent"]
    linear = next(row for row in result["connectors"] if row["provider"] == "linear")

    assert linear["available"] is False
    assert linear["grant_stored"] is False
    assert "tools" not in linear
    assert "provider=linear exception=GatewayError" in caplog.text


def test_catalog_teardown_downgrades_only_known_redacted_sdk_duplicates(caplog):
    from anyio import ClosedResourceError

    logger = logging.getLogger("mcp.client.streamable_http")

    def _handle_sse_event(exc):
        try:
            raise exc
        except Exception:
            logger.exception("private provider payload")

    with caplog.at_level(logging.DEBUG):
        with connector_gateway.private_upstream("notion", catalog=True):
            _handle_sse_event(ClosedResourceError())
            _handle_sse_event(ValueError())
        with connector_gateway.private_upstream("notion", catalog=False):
            _handle_sse_event(ClosedResourceError())

    assert [record.levelno for record in caplog.records] == [logging.DEBUG, logging.ERROR, logging.ERROR]
    assert "private provider payload" not in caplog.text


def test_status_tool_remains_available_before_any_grants(gateway):
    client, server, _, token, _ = gateway
    server.store.signout("linear")
    listed = rpc(client, token, "tools/list").json()["result"]["tools"]
    assert [tool["name"] for tool in listed] == [connector_gateway.STATUS_TOOL]
    status = call(gateway, connector_gateway.STATUS_TOOL, {}).json()["result"]["structuredContent"]
    assert status["mode"] == "read"
    assert all(not row["available"] for row in status["connectors"])


def test_catalog_deadline_includes_waiting_for_another_credential_operation(gateway, monkeypatch):
    client, server, _, token, _ = gateway
    monkeypatch.setattr(connector_gateway, "CATALOG_SECONDS", 0.025)
    with server.store.provider_lock("linear"):
        listed = rpc(client, token, "tools/list").json()["result"]["tools"]
        assert [tool["name"] for tool in listed] == [connector_gateway.STATUS_TOOL]
        status = call(gateway, connector_gateway.STATUS_TOOL, {}).json()["result"]["structuredContent"]
        linear = next(row for row in status["connectors"] if row["provider"] == "linear")
        assert linear["available"] is False
        assert "timed out" in linear["error"]


def test_sdk_malformed_client_payload_is_redacted_before_log_handlers(gateway, caplog):
    client, _, _, token, _ = gateway
    marker = "FAKE_PRIVATE_INBOUND_QUERY_9927"
    response = rpc(client, token, "tools/call", {"name": "linear__get_issue", "arguments": marker})
    assert response.status_code == 200
    assert marker not in caplog.text
    assert "provider=gateway" in caplog.text


def test_sdk_malformed_upstream_body_is_redacted_before_log_handlers(gateway, caplog, monkeypatch):
    monkeypatch.setattr(connector_gateway, "TIMEOUT", 5)
    gateway[-1].invalid_json = True
    assert call(gateway).json()["result"]["isError"] is True
    assert "FAKE_PRIVATE_PROVIDER_CONTENT" not in caplog.text
    records = [record for record in caplog.records if record.name == "mcp.client.streamable_http"]
    assert records
    assert all(record.exc_info is None and record.exc_text is None and record.stack_info is None for record in records)
    assert "Connector upstream event" in caplog.text
    logging.getLogger("mcp.client.streamable_http").warning("ordinary log stays")
    assert "ordinary log stays" in caplog.text


@pytest.mark.parametrize("mode", ["sql", "write", None])
def test_notion_mixed_query_tool_enforces_reviewed_read_modes(gateway, mode):
    response = call(gateway, "notion__notion-query-data-sources", {"mode": mode})
    assert response.json()["result"]["isError"] is True
    assert not gateway[-1].calls


def test_query_mode_restriction_preserves_rooted_local_schema_references():
    schema = {"type": "object", "$defs": {"mode": {"type": "string"}},
              "properties": {"mode": {"$ref": "#/$defs/mode"}}, "required": ["mode"]}
    tool = types.Tool(name="notion-query-data-sources", inputSchema=schema)
    advertised = connector_gateway.input_schema("notion", tool, "read")
    Draft202012Validator(advertised).validate({"mode": "rows"})
    Draft202012Validator(advertised).validate({"mode": "view"})


def test_sdk_read_uses_only_upstream_identity_and_audits_no_payloads(gateway):
    _, server, _, token, upstream = gateway
    response = call(gateway)
    assert response.status_code == 200, response.text
    assert response.json()["result"]["content"][0]["text"] == "upstream result"
    assert upstream.calls == [{"name": "get_issue", "arguments": {"id": "A-12"}}]
    for request in upstream.requests:
        assert request.headers["authorization"] == "Bearer upstream-private-token"
        for header in ("cookie", "x-cockpit-as", "x-cockpit-local", "x-forwarded-for"):
            assert header not in request.headers
        assert token not in str(request.headers)
        assert str(request.url) == PROVIDERS["linear"]
    with server.store.connection() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM audit")]
        assert [row["status"] for row in rows][-2:] == ["read.started", "read.completed"]
        assert "A-12" not in json.dumps(rows)
        assert "upstream result" not in json.dumps(rows)
        assert "private-token" not in json.dumps(rows)


@pytest.mark.parametrize("tool", ["create_issue", "get_delete_everything", "delete_issue", "new_read_tool"])
def test_mutating_unknown_and_misleading_readonly_hints_are_server_denied(gateway, tool):
    response = call(gateway, "linear__" + tool)
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is True
    assert "read tools only" in result["content"][0]["text"]
    assert not gateway[-1].calls


@pytest.mark.parametrize("args", [{}, {"id": 7}, {"id": "A", "extra": True}])
def test_invalid_arguments_never_reach_upstream(gateway, args):
    assert call(gateway, arguments=args).json()["result"]["isError"] is True
    assert not gateway[-1].calls


@pytest.mark.parametrize("schema", [
    {"$ref": "http://127.0.0.1:1/secret"},
    {"properties": {"id": {"$dynamicRef": "https://evil.example/secret"}}},
    {"$id": "https://evil.example/schema", "$ref": "#/x"},
])
def test_schema_cannot_fetch_external_resources(gateway, schema):
    gateway[-1].schema = schema
    response = call(gateway)
    assert response.json()["result"]["isError"] is True
    assert not gateway[-1].calls


@pytest.mark.parametrize("token", ["", "invalid", "t" * 40])
def test_missing_invalid_and_ordinary_cockpit_agent_credentials_are_refused(gateway, token):
    assert rpc(gateway[0], token, "tools/list").status_code == 401


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_every_transport_method_requires_its_own_authentication(gateway, method):
    response = gateway[0].request(method, connector_gateway.MCP_PATH)
    assert response.status_code == 401


@pytest.mark.parametrize(("provider", "read", "write"), [
    ("linear", "get_issue", "create_issue"),
    ("slack", "slack_search_messages", "slack_send_message"),
    ("notion", "notion-fetch", "notion-create-pages"),
])
def test_each_provider_permits_reviewed_reads_and_refuses_its_write_tools(gateway, provider, read, write):
    configure(gateway[1].store, provider)
    assert call(gateway, provider + "__" + read).json()["result"]["isError"] is False
    assert call(gateway, provider + "__" + write).json()["result"]["isError"] is True
    assert [entry["name"] for entry in gateway[-1].calls] == [read]


def test_gateway_token_cannot_access_ordinary_api_or_accept_browser_cookie(gateway, world):
    client, server, identity, token, _ = gateway
    # The ordinary browser cookie is present here: the gateway's bearer role still confers no ordinary API access.
    assert world.client.get("/api/state", headers={"authorization": "Bearer " + token}).status_code == 401
    assert client.post(connector_gateway.MCP_PATH, headers={"Accept": "application/json"}, json={}).status_code == 401
    server.store.revoke(identity)
    assert rpc(client, token, "tools/list").status_code == 401


@pytest.mark.parametrize("headers", [{"host": "cockpit.example.test"}, {"host": "evil.example"},
                                    {"origin": "http://127.0.0.1:18790"}, {"x-forwarded-for": "127.0.0.1"},
                                    {"forwarded": "for=127.0.0.1"}])
def test_gateway_rejects_browser_proxy_and_unexpected_host_requests(gateway, headers):
    assert rpc(gateway[0], gateway[3], "tools/list", headers=headers).status_code == 403


def test_non_loopback_gateway_request_is_refused(gateway):
    client, _, _, token, _ = gateway
    client._transport.client = ("203.0.113.1", 1234)
    assert rpc(client, token, "tools/list").status_code == 403


@pytest.mark.parametrize("action", ["revoke", "signout", "reconfigure"])
def test_revocation_while_listing_tools_stops_read_before_execution(gateway, action):
    _, server, identity, _, upstream = gateway
    # Populate the SDK's advertised-tool cache, then revoke while our call fetches the current schema.
    assert rpc(gateway[0], gateway[3], "tools/list").status_code == 200

    def invalidate():
        upstream.before_list = None
        if action == "revoke":
            server.store.revoke(identity)
        elif action == "signout":
            server.store.signout("linear")
        else:
            configure(server.store)

    upstream.before_list = invalidate
    assert call(gateway).json()["result"]["isError"] is True
    assert not upstream.calls


def test_private_store_rejects_symlink_or_open_permissions_and_hashes_tokens(tmp_path):
    store = Store(tmp_path / "private")
    identity, token = store.create_client("Laptop Claude", "read")
    assert store.authenticate("Bearer " + token) == identity
    assert token.encode() not in store.path.read_bytes()
    assert stat.S_IMODE(store.folder.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    store.path.chmod(0o644)
    with pytest.raises(GatewayError, match="0600"):
        store.clients()
    store.path.chmod(0o600)
    other = tmp_path / "symlink"
    other.symlink_to(store.folder, target_is_directory=True)
    with pytest.raises(GatewayError, match="0700"):
        Store(other)


def test_existing_client_migration_keeps_hash_and_read_access_across_concurrent_startup(tmp_path):
    folder = tmp_path / "legacy"
    folder.mkdir(mode=0o700)
    path = folder / "gateway.sqlite3"
    path.touch(mode=0o600)
    token = "cgw_legacy-test-credential"
    hashed = hashlib.sha256(token.encode()).hexdigest()
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE clients (id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, "
                     "created REAL NOT NULL, revoked REAL)")
        conn.execute("INSERT INTO clients VALUES ('legacy', 'Existing client', ?, 1, NULL)", (hashed,))
    with ThreadPoolExecutor(max_workers=4) as pool:
        stores = list(pool.map(lambda _: Store(folder), range(4)))
    for store in stores:
        assert store.authenticate("Bearer " + token) == "legacy"
        assert store.access("legacy") == "read"
        assert store.clients()[0]["access"] == "read"
    with stores[0].connection() as conn:
        assert conn.execute("SELECT token_hash FROM clients").fetchone()[0] == hashed
    assert token.encode() not in path.read_bytes()


@pytest.fixture
def oauth(tmp_path, monkeypatch):
    store = Store(tmp_path / "oauth")
    configure(store)
    calls = []
    monkeypatch.setattr(gateway_oauth, "discover", lambda provider: DISCOVERY)

    def answer(provider, method, url, payload, form):
        calls.append((provider, method, url, payload, form))
        return {"access_token": "new-private-access", "refresh_token": "new-private-refresh", "expires_in": 3600}

    monkeypatch.setattr(gateway_oauth, "request_json", answer)
    return store, calls


def callback(store, state=None):
    pending = store.provider("linear")["pending"]
    return "http://localhost:8766/callback?" + urlencode({"code": "private-code", "state": state or pending["state"]})


def test_oauth_pkce_state_callback_binding_and_single_use(oauth):
    store, calls = oauth
    link = gateway_oauth.begin(store, "linear")
    pending = store.provider("linear")["pending"]
    params = parse_qs(urlsplit(link).query)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(pending["verifier"].encode()).digest()).decode().rstrip("=")
    assert params["code_challenge"] == [challenge]
    assert params["code_challenge_method"] == ["S256"]
    assert params["client_id"] == ["cockpit-own-client"]
    assert params["resource"] == [PROVIDERS["linear"]]
    for address in (callback(store, "wrong-state"), callback(store).replace(":8766/", ":8767/"),
                    callback(store) + "&state=duplicate", callback(store) + "&iss=https://evil.example"):
        with pytest.raises(GatewayError):
            gateway_oauth.finish(store, "linear", address)
    assert not calls
    address = callback(store)
    gateway_oauth.finish(store, "linear", address)
    assert calls[-1][3]["code_verifier"] == pending["verifier"]
    assert calls[-1][3]["code"] == "private-code"
    assert store.provider("linear")["pending"] is None
    assert store.provider("linear")["grant"]["access_token"] == "new-private-access"
    with pytest.raises(GatewayError):
        gateway_oauth.finish(store, "linear", address)
    assert len(calls) == 1


def test_oauth_expired_or_signed_out_pending_cannot_exchange(oauth):
    store, calls = oauth
    gateway_oauth.begin(store, "linear")
    address = callback(store)
    pending = store.provider("linear")["pending"] | {"started": 0}
    store.save_identity("linear", "pending", pending)
    with pytest.raises(GatewayError, match="expired"):
        gateway_oauth.finish(store, "linear", address)
    gateway_oauth.begin(store, "linear")
    address = callback(store)
    store.signout("linear")
    with pytest.raises(GatewayError):
        gateway_oauth.finish(store, "linear", address)
    assert not calls


def test_refresh_is_serialized_and_atomic_across_threads(oauth):
    store, calls = oauth
    configure(store, expires=0)
    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(lambda _: gateway_oauth.access_token(store, "linear"), range(12)))
    assert len(calls) == 1
    assert {token for token, _ in tokens} == {"new-private-access"}
    assert store.provider("linear")["grant"]["refresh_token"] == "new-private-refresh"


def test_refresh_unknown_outcome_removes_local_grant(oauth, monkeypatch):
    store, _ = oauth
    configure(store, expires=0)

    def fail(*_):
        raise GatewayError("refresh failed")

    monkeypatch.setattr(gateway_oauth, "request_json", fail)
    with pytest.raises(GatewayError, match="refresh failed"):
        gateway_oauth.access_token(store, "linear")
    assert store.provider("linear")["grant"] is None
    with pytest.raises(GatewayError, match="signed out"):
        gateway_oauth.access_token(store, "linear")


def test_malformed_refresh_response_cannot_replay_rotating_token(oauth, monkeypatch):
    store, calls = oauth
    configure(store, "slack", expires=0)

    def malformed(*args):
        calls.append(args)
        return {"ok": True, "authed_user": None}

    monkeypatch.setattr(gateway_oauth, "request_json", malformed)
    for _ in range(2):
        with pytest.raises(GatewayError):
            gateway_oauth.access_token(store, "slack")
    assert len(calls) == 1
    assert store.provider("slack")["grant"] is None


def test_oauth_registration_retains_a_cockpit_identity_and_never_uses_claude_metadata(oauth, monkeypatch):
    store, calls = oauth
    store.configure("linear", {"client_id": None, "client_secret": None,
                                "redirect_uri": "http://localhost:8766/callback", "scope": "read"})

    def register(provider, method, url, payload, form):
        calls.append((url, payload))
        return {"client_id": "new-cockpit-identity"}

    monkeypatch.setattr(gateway_oauth, "request_json", register)
    gateway_oauth.begin(store, "linear")
    gateway_oauth.begin(store, "linear")
    assert len(calls) == 1
    assert calls[0][1]["client_name"] == "Cockpit connectors"
    assert store.provider("linear")["config"]["client_id"] == "new-cockpit-identity"
    assert "claude" not in json.dumps(calls)


def test_token_exchange_checks_slack_error_body_and_user_token_type(monkeypatch):
    responses = [{"ok": False, "error": "secret upstream body"},
                 {"ok": True, "token_type": "user", "access_token": "slack-private", "expires_in": 3600}]
    original = httpx.Client
    observed = []

    def answer(request):
        observed.append(request)
        return httpx.Response(200, json=responses.pop(0))

    monkeypatch.setattr(gateway_oauth.httpx, "Client", lambda **kw: original(**kw, transport=httpx.MockTransport(answer)))
    found = DISCOVERY | {"token": "https://slack.com/api/oauth.v2.user.access"}
    with pytest.raises(GatewayError, match="refused OAuth") as error:
        gateway_oauth._tokens("slack", found, {"grant_type": "authorization_code", "code": "private-code"}, None)
    assert "secret upstream body" not in str(error.value)
    token = gateway_oauth._tokens("slack", found, {"grant_type": "authorization_code", "code": "private-code"}, None)
    assert token["access_token"] == "slack-private"
    assert all("authorization" not in request.headers and "cookie" not in request.headers for request in observed)


def test_slack_public_pkce_uses_own_client_id_no_secret_and_accepts_user_token(oauth):
    store, calls = oauth
    configure(store, "slack")
    gateway_oauth.begin(store, "slack")
    pending = store.provider("slack")["pending"]
    gateway_oauth.finish(store, "slack", "http://localhost:8766/callback?" + urlencode({"state": pending["state"], "code": "slack-code"}))
    assert calls[-1][3].get("client_secret") is None
    assert calls[-1][3]["client_id"] == "cockpit-own-client"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_discovery_resource_metadata_path_and_endpoint_hosts(provider, monkeypatch):
    urls = []
    resource = gateway_oauth.RESOURCES[provider]
    issuer = "https://slack.com" if provider == "slack" else PROVIDERS[provider].removesuffix("/mcp")

    def answer(name, method, url, payload, form):
        urls.append(url)
        if "oauth-protected-resource" in url:
            return {"resource": resource, "authorization_servers": [issuer]}
        return {"issuer": issuer, "authorization_endpoint": issuer + "/authorize", "token_endpoint": issuer + "/token",
                "code_challenge_methods_supported": ["S256"]}

    monkeypatch.setattr(gateway_oauth, "request_json", answer)
    found = gateway_oauth.discover(provider)
    assert found["resource"] == resource
    assert urls[0].endswith("/.well-known/oauth-protected-resource" + ("" if provider == "slack" else "/mcp"))
    assert urls[1] == issuer + "/.well-known/oauth-authorization-server"


@pytest.mark.parametrize("url", ["http://mcp.linear.app/token", "https://127.0.0.1/token",
                                  "https://mcp.linear.app.evil.example/token", "https://mcp.linear.app:444/token",
                                  "https://user:pass" + "@mcp.linear.app/token"])
def test_oauth_endpoints_are_restricted_to_provider_hosts(url):
    with pytest.raises(GatewayError):
        gateway_oauth.endpoint("linear", url)


def test_cli_prints_token_once_only_with_explicit_flag(tmp_path, monkeypatch, capsys):
    from cockpit import config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "cockpit.db")
    with pytest.raises(SystemExit):
        gateway_cli.main(["client", "create", "Laptop Codex"])
    capsys.readouterr()
    assert gateway_cli.main(["client", "create", "Laptop Codex", "--access", "read", "--show-token"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["token"].startswith("cgw_")
    assert gateway_cli.main(["client", "list"]) == 0
    listed = capsys.readouterr().out
    assert created["token"] not in listed
    assert json.loads(listed)[0]["access"] == "read"
    assert gateway_cli.main(["client", "access-update", created["id"], "--access", "read_write"]) == 0
    assert "Client access updated to read_write" in capsys.readouterr().out
    assert Store(tmp_path / "connectors").authenticate("Bearer " + created["token"]) == created["id"]
    assert Store(tmp_path / "connectors").access(created["id"]) == "read_write"
    assert gateway_cli.main(["client", "revoke", created["id"]]) == 0
    assert Store(tmp_path / "connectors").authenticate("Bearer " + created["token"]) is None
    assert gateway_cli.main(["client", "access-update", created["id"], "--access", "read_write"]) == 1


def test_cli_creation_requires_explicit_access_even_when_printing_token(capsys):
    with pytest.raises(SystemExit):
        gateway_cli.main(["client", "create", "Laptop Codex", "--show-token"])
    assert "--access" in capsys.readouterr().err


@pytest.fixture
def shared_claude(tmp_path, monkeypatch, claude_accounts):
    import sys
    import time

    from cockpit import config, connector_signin, gateway_claude

    account = claude_accounts[0]
    account.folder.mkdir(parents=True, mode=0o700)
    server = {"type": "http", "url": PROVIDERS["linear"]}
    account.state.write_text(json.dumps({"mcpServers": {"linear": server}}))
    account.state.chmod(0o600)
    key = connector_signin.credential_key("linear", server)
    path = account.folder / ".credentials.json"
    entry = {"serverName": "linear", "serverUrl": PROVIDERS["linear"], "clientId": "native-client",
             "accessToken": "shared-private-access", "refreshToken": "shared-private-refresh",
             "expiresAt": (time.time() + 3600) * 1000}
    path.write_text(json.dumps({"mcpOAuth": {key: entry}, "claudeAiOauth": {"accessToken": "keep-native-session"}}))
    path.chmod(0o600)
    record = tmp_path / "native-check.jsonl"
    binary_folder = tmp_path / "native-bin"
    binary_folder.mkdir(mode=0o700)
    script = binary_folder / "claude"
    script.write_text(f'''#!{sys.executable}
import json, os, sys, time
from pathlib import Path
path = Path({str(path)!r})
with Path({str(record)!r}).open("a") as output:
    output.write(json.dumps({{"argv": sys.argv[1:], "account": os.environ.get("CLAUDE_CONFIG_DIR"), "cwd": os.getcwd()}}) + "\\n")
stored = json.loads(path.read_text())
entry = stored["mcpOAuth"][{key!r}]
entry.update(accessToken="native-renewed-access", refreshToken="native-renewed-refresh", expiresAt=(time.time()+7200)*1000)
path.write_text(json.dumps(stored))
print("FAKE_PRIVATE_NATIVE_OUTPUT")
print("FAKE_PRIVATE_NATIVE_ERROR", file=sys.stderr)
''')
    script.chmod(0o700)
    monkeypatch.setenv("PATH", str(binary_folder) + ":" + os.environ["PATH"])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/unrelated-profile")
    store = Store(tmp_path / "shared-store")
    gateway_claude.bind(store, "linear", account.name, "linear")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "cockpit.db")
    return store, account, path, key, script, record


def shared_entry(fixture, **values):
    _, _, path, key, _, _ = fixture
    document = json.loads(path.read_text())
    document["mcpOAuth"][key].update(values)
    path.write_text(json.dumps(document))


def test_shared_claude_reuses_source_without_copying_or_refresh_post(shared_claude, monkeypatch):
    from cockpit import gateway_claude

    store, _, path, _, _, record = shared_claude
    monkeypatch.setattr(gateway_oauth, "request_json", lambda *_: pytest.fail("shared source must not post OAuth"))
    assert gateway_oauth.access_token(store, "linear")[0] == "shared-private-access"
    shared_entry(shared_claude, accessToken="external-native-renewal")
    assert gateway_oauth.access_token(store, "linear")[0] == "external-native-renewal"
    assert not record.exists()
    row = store.provider("linear")
    assert row["grant"] == {"shared": True}
    assert gateway_claude.status(row) == {"authorization_source": "claude", "grant_stored": False,
                                         "signin_pending": False, "sharing_enabled": True}
    assert b"shared-private" not in store.path.read_bytes()
    assert b"external-native-renewal" not in store.path.read_bytes()
    assert "keep-native-session" in path.read_text()
    for operation in (lambda: gateway_oauth.begin(store, "linear"),
                      lambda: gateway_oauth.finish(store, "linear", "http://localhost/callback?code=private")):
        with pytest.raises(GatewayError, match="existing Cockpit connector sign-in card"):
            operation()


def test_shared_native_renewal_is_serialized_and_has_explicit_safe_cli_arguments(shared_claude, monkeypatch, caplog):
    store, _, path, _, script, record = shared_claude
    shared_entry(shared_claude, expiresAt=1)
    monkeypatch.setattr(gateway_oauth, "request_json", lambda *_: pytest.fail("native owns refresh"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: gateway_oauth.access_token(store, "linear")[0], range(8)))
    assert results == ["native-renewed-access"] * 8
    observed = [json.loads(line) for line in record.read_text().splitlines()]
    assert len(observed) == 1
    assert observed[0]["argv"] == ["mcp", "get", "linear"] and observed[0]["account"] is None
    working = Path(observed[0]["cwd"])
    assert working.parent == store.folder.resolve() and working.name.startswith("claude-check-")
    assert not working.exists()
    assert script.name == "claude"
    assert "native-renewed-refresh" in path.read_text()
    assert "keep-native-session" in path.read_text()
    assert "FAKE_PRIVATE_NATIVE" not in caplog.text
    assert b"native-renewed" not in store.path.read_bytes()


def test_shared_nondefault_native_check_uses_the_configured_account(shared_claude, claude_accounts, monkeypatch):
    from dataclasses import replace

    from cockpit import config, gateway_claude

    store, account, _, _, _, record = shared_claude
    work = replace(account, name="work", default=False)
    monkeypatch.setattr(config, "ACCOUNTS", (work,))
    gateway_claude.bind(store, "linear", "work", "linear")
    shared_entry(shared_claude, expiresAt=1)
    gateway_oauth.access_token(store, "linear")
    assert json.loads(record.read_text())["account"] == str(work.folder)


@pytest.mark.parametrize("expires", [None, "123", True, float("nan"), float("inf"), -1, 10**500])
def test_shared_rejects_invalid_expiry_without_native_check(shared_claude, expires):
    shared_entry(shared_claude, expiresAt=expires)
    with pytest.raises(GatewayError, match="expiry"):
        gateway_oauth.access_token(shared_claude[0], "linear")
    assert not shared_claude[-1].exists()


@pytest.mark.parametrize("field,value", [("serverName", "other"), ("serverUrl", "https://evil.example/mcp"),
                                         ("accessToken", ""), ("accessToken", "invalid\ntoken"),
                                         ("clientId", None), ("refreshToken", {}), ("scope", [])])
def test_shared_rejects_invalid_entry_identity_and_metadata(shared_claude, field, value):
    shared_entry(shared_claude, **{field: value})
    with pytest.raises(GatewayError):
        gateway_oauth.access_token(shared_claude[0], "linear")
    assert not shared_claude[-1].exists()


@pytest.mark.parametrize("target", ["state", "credentials"])
@pytest.mark.parametrize("problem", ["symlink", "permissions", "parse", "ownership"])
def test_shared_source_files_are_private_and_errors_hide_contents(shared_claude, target, problem, monkeypatch, caplog):
    store, account, path, _, _, _ = shared_claude
    target_path = account.state if target == "state" else path
    if problem == "symlink":
        renamed = target_path.with_suffix(".original")
        target_path.rename(renamed)
        target_path.symlink_to(renamed)
    elif problem == "permissions":
        target_path.chmod(0o644)
    elif problem == "parse":
        target_path.write_text('{"FAKE_PRIVATE_PARSE_BODY":')
    else:
        monkeypatch.setattr(os, "getuid", lambda: -1)
    with pytest.raises(GatewayError) as error:
        gateway_oauth.access_token(store, "linear")
    assert "FAKE_PRIVATE_PARSE_BODY" not in str(error.value) + caplog.text


@pytest.mark.parametrize("change", ["url", "headers", "profile", "missing-entry"])
def test_shared_profile_and_server_changes_fail_closed(shared_claude, monkeypatch, change):
    from dataclasses import replace

    from cockpit import config

    store, account, path, key, _, _ = shared_claude
    if change in {"url", "headers"}:
        document = json.loads(account.state.read_text())
        document["mcpServers"]["linear"][change] = "https://evil.example/mcp" if change == "url" else {"X-New": "value"}
        account.state.write_text(json.dumps(document))
    elif change == "profile":
        monkeypatch.setattr(config, "ACCOUNTS", (replace(account, default=False),))
    else:
        document = json.loads(path.read_text())
        del document["mcpOAuth"][key]
        path.write_text(json.dumps(document))
    with pytest.raises(GatewayError):
        gateway_oauth.access_token(store, "linear")


@pytest.mark.parametrize("mode", ["exit", "timeout", "no-renewal", "malformed"])
def test_shared_native_failure_is_safe_and_not_immediately_retried(shared_claude, monkeypatch, caplog, mode):
    import sys

    from cockpit import gateway_claude

    store, _, _, _, script, record = shared_claude
    shared_entry(shared_claude, expiresAt=1)
    behaviors = {"exit": "raise SystemExit(1)", "timeout": "import time; time.sleep(5)",
                 "no-renewal": "print('Connected')", "malformed": f"from pathlib import Path; Path({str(shared_claude[2])!r}).write_text('FAKE_PRIVATE_BAD_NATIVE')"}
    script.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(record)!r}).open('a').write('called\\n')\n" + behaviors[mode] + "\n")
    script.chmod(0o700)
    monkeypatch.setattr(gateway_claude, "NATIVE_TIMEOUT", 0.5 if mode == "timeout" else 2)
    for _ in range(2):
        with pytest.raises(GatewayError) as error:
            gateway_oauth.access_token(store, "linear")
        assert "FAKE_PRIVATE" not in str(error.value) + caplog.text
    assert record.read_text().splitlines() == ["called"]
    assert store.provider("linear")["grant"] == {"shared": True}


def test_shared_signout_and_client_revocation_do_not_reactivate_from_native_source(shared_claude):
    store = shared_claude[0]
    identity, token = store.create_client("shared reader", "read")
    _, generation = gateway_oauth.access_token(store, "linear")
    store.revoke(identity)
    assert store.authenticate("Bearer " + token) is None
    with store.connection() as conn:
        assert not store.active(conn, identity, "linear", generation, "read")
    store.signout("linear")
    for _ in range(2):
        with pytest.raises(GatewayError, match="existing Cockpit"):
            gateway_oauth.access_token(store, "linear")
    assert store.provider("linear")["grant"] is None
    assert not shared_claude[-1].exists()


def test_shared_gateway_rechecks_source_identity_before_tool_dispatch(gateway, shared_claude):
    from cockpit import gateway_claude

    _, server, _, _, upstream = gateway
    gateway_claude.bind(server.store, "linear", "personal", "linear")
    assert rpc(gateway[0], gateway[3], "tools/list").status_code == 200
    upstream.before_list = lambda: shared_entry(shared_claude, accessToken="switched-account-token")
    assert call(gateway).json()["result"]["isError"] is True
    assert not upstream.calls


def test_shared_gateway_status_reports_live_availability_without_db_grant(gateway, shared_claude):
    from cockpit import gateway_claude

    gateway_claude.bind(gateway[1].store, "linear", "personal", "linear")
    result = call(gateway, connector_gateway.STATUS_TOOL, {}).json()["result"]["structuredContent"]
    linear = next(row for row in result["connectors"] if row["provider"] == "linear")
    assert linear["authorization_source"] == "claude" and linear["available"]
    assert not linear["grant_stored"] and linear["sharing_enabled"]
    assert gateway[-1].requests[-1].headers["authorization"] == "Bearer shared-private-access"


def test_share_cli_requires_explicit_account_and_server_and_exposes_no_secrets(shared_claude, monkeypatch, capsys):
    from cockpit import config

    store = shared_claude[0]
    monkeypatch.setattr(config, "DB_PATH", store.folder.parent / "cockpit.db")
    for options in ([], ["--account", "personal"], ["--server", "linear"]):
        with pytest.raises(SystemExit):
            gateway_cli.main(["share-claude", "linear", *options])
        capsys.readouterr()
    assert gateway_cli.main(["share-claude", "linear", "--account", "personal", "--server", "linear"]) == 0
    assert "Claude Code owns renewal" in capsys.readouterr().out
    assert gateway_cli.main(["status"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["authorization_source"] == "claude" and rows[0]["source_available"]
    assert not rows[0]["grant_stored"]
    assert gateway_cli.main(["share-claude", "linear", "--account", "missing", "--server", "linear"]) == 1
    assert "shared-private" not in capsys.readouterr().err


@pytest.mark.parametrize("sharing_enabled", [True, False])
def test_bound_legacy_signin_checks_and_finish_leave_renewal_to_claude(shared_claude, monkeypatch, sharing_enabled):
    import time

    from cockpit import config, connector_signin, gateway_claude

    _, account, path, key, _, record = shared_claude
    gateway = Store(config.DB_PATH.parent / "connectors")
    gateway_claude.bind(gateway, "linear", account.name, "linear")
    if not sharing_enabled:
        gateway.signout("linear")
    monkeypatch.setattr(connector_signin, "tool_count", lambda *_: 3)
    monkeypatch.setattr(connector_signin, "clear_needs_auth", lambda *_: None)
    monkeypatch.setattr(connector_signin, "renew", lambda *_: pytest.fail("Claude owns shared renewal"))
    shared_entry(shared_claude, expiresAt=1)
    assert connector_signin.check(account, "linear")["renewal_owner"] == "claude"
    assert len(record.read_text().splitlines()) == 1
    pending = {"state": "state", "server": "linear", "server_url": PROVIDERS["linear"], "key": key,
               "discovery": {"resource": PROVIDERS["linear"], "issuer": "https://mcp.linear.app",
                             "auth_metadata_url": "https://mcp.linear.app/auth", "resource_metadata_url": "https://mcp.linear.app/resource"},
               "client": {"client_id": "native-client", "redirect_uri": "http://localhost/callback"},
               "scope": "read", "verifier": "verifier"}
    monkeypatch.setattr(connector_signin, "_token_call", lambda *_: {"access_token": "new-consent", "refresh_token": "new-consent-refresh", "expires_in": 3600})
    finished = connector_signin.finish(account, pending, "http://localhost/callback?code=private&state=state")
    assert finished["renewal_owner"] == "claude" and finished["renews"]
    assert json.loads(path.read_text())["mcpOAuth"][key]["accessToken"] == "new-consent"
    assert len(record.read_text().splitlines()) == 1
    text = connector_signin._finished_text({"id": 1, "signin": {"server": "linear"}}, account, finished)
    assert "Claude Code owns renewal" in text and "renewed it once" not in text
    assert finished["expires_at"] > time.time() * 1000
    assert bool(gateway.provider("linear")["grant"]) == sharing_enabled
    if not sharing_enabled:
        with pytest.raises(GatewayError, match="existing Cockpit"):
            gateway_oauth.access_token(gateway, "linear")


def test_catalog_cancellation_keeps_native_worker_lock_until_completion(shared_claude, monkeypatch):
    import asyncio
    import threading

    from cockpit import gateway_claude

    entered, release = threading.Event(), threading.Event()
    checks = []
    shared_entry(shared_claude, expiresAt=1)

    def native_check(*_):
        checks.append(True)
        entered.set()
        assert release.wait(3)
        shared_entry(shared_claude, expiresAt=9_999_999_999_999, accessToken="completed-native-refresh")

    monkeypatch.setattr(gateway_claude, "native_check", native_check)

    async def exercise():
        task = asyncio.create_task(asyncio.to_thread(gateway_oauth.access_token, shared_claude[0], "linear"))
        assert await asyncio.to_thread(entered.wait, 1)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(task, 0.01)
        second = asyncio.create_task(asyncio.to_thread(gateway_oauth.access_token, shared_claude[0], "linear"))
        await asyncio.sleep(0.05)
        assert not second.done()
        assert checks == [True]
        release.set()
        assert (await second)[0] == "completed-native-refresh"

    try:
        asyncio.run(exercise())
    finally:
        release.set()
    assert checks == [True]


def test_native_installer_location_works_without_path_entry(shared_claude, monkeypatch):
    from cockpit import config, gateway_claude

    store, _, _, _, script, record = shared_claude
    monkeypatch.setattr(gateway_claude.shutil, "which", lambda _: None)
    native_home = script.parent.parent / "native-home"
    destination = native_home / ".local" / "bin" / "claude"
    destination.parent.mkdir(parents=True)
    destination.symlink_to(script)
    monkeypatch.setattr(config, "HOME", native_home)
    shared_entry(shared_claude, expiresAt=1)
    assert gateway_oauth.access_token(store, "linear")[0] == "native-renewed-access"
    assert record.exists()


def test_shared_catalog_hides_tools_if_source_disappears_during_check(gateway, shared_claude):
    from cockpit import gateway_claude

    gateway_claude.bind(gateway[1].store, "linear", "personal", "linear")
    gateway[-1].before_list = lambda: shared_claude[2].write_text(json.dumps({"mcpOAuth": {}}))
    tools = rpc(gateway[0], gateway[3], "tools/list").json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == [connector_gateway.STATUS_TOOL]
    result = call(gateway, connector_gateway.STATUS_TOOL, {}).json()["result"]["structuredContent"]
    linear = next(row for row in result["connectors"] if row["provider"] == "linear")
    assert not linear["available"] and "existing Cockpit" in linear["error"]


def test_shared_endpoint_preserves_read_and_read_write_roles(gateway, shared_claude):
    from cockpit import gateway_claude

    browser, server, identity, token, upstream = gateway
    gateway_claude.bind(server.store, "linear", "personal", "linear")
    listed = rpc(browser, token, "tools/list").json()["result"]["tools"]
    assert "linear__create_issue" not in {tool["name"] for tool in listed}
    assert call(gateway, "linear__create_issue").json()["result"]["isError"]
    assert not upstream.calls
    server.store.update_access(identity, "read_write")
    listed = rpc(browser, token, "tools/list").json()["result"]["tools"]
    assert "linear__create_issue" in {tool["name"] for tool in listed}
    assert not call(gateway, "linear__create_issue").json()["result"]["isError"]
    assert upstream.calls == [{"name": "create_issue", "arguments": {"id": "A-12"}}]
    assert upstream.requests[-1].headers["authorization"] == "Bearer shared-private-access"


@pytest.mark.parametrize("override", ["parent-mcp", "parent-git", "project-state"])
def test_native_check_rejects_inherited_project_configuration(shared_claude, override):
    store, account, _, _, _, record = shared_claude
    if override == "project-state":
        document = json.loads(account.state.read_text())
        document["projects"] = {str(store.folder.resolve()): {"mcpServers": {"linear": {"type": "stdio", "command": "unrelated-server"}}}}
        account.state.write_text(json.dumps(document))
    else:
        (store.folder / (".mcp.json" if override == "parent-mcp" else ".git")).write_text("unrelated-project-config")
    shared_entry(shared_claude, expiresAt=1)
    with pytest.raises(GatewayError, match="project"):
        gateway_oauth.access_token(store, "linear")
    assert not record.exists()


@pytest.mark.parametrize("tool", ["slack_read_user_profile", "slack_search_emojis"])
def test_current_slack_profile_and_emoji_reads_are_available_without_write_authority(gateway, tool):
    browser, server, _, token, upstream = gateway
    configure(server.store, "slack")
    upstream.names = [tool]
    listed = rpc(browser, token, "tools/list").json()["result"]["tools"]
    read = next(t for t in listed if t["name"] == "slack__" + tool)
    assert read["annotations"]["readOnlyHint"] is True
    assert read["annotations"]["destructiveHint"] is False
    result = call(gateway, "slack__" + tool).json()["result"]
    assert not result["isError"]
    assert upstream.calls == [{"name": tool, "arguments": {"id": "A-12"}}]
