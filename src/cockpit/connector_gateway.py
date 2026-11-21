"""Loopback MCP gateway with revocable read or read/write client permissions."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import sqlite3
from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import Any, NamedTuple

import httpx
from jsonschema import Draft202012Validator, SchemaError, ValidationError
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from referencing import Registry
from referencing.exceptions import Unresolvable
from starlette.requests import Request
from starlette.types import Receive, Scope, Send

from cockpit import gateway_claude, gateway_oauth
from cockpit.gateway_logs import private_upstream
from cockpit.gateway_store import PROVIDERS, GatewayError, Store, canonical

log = logging.getLogger(__name__)
MCP_PATH = "/api/connectors/mcp"
# Both ends of the dedicated SSH tunnel use a literal loopback host. The ordinary reverse proxy uses the
# configured Cockpit hostname and also blocks this route. Forwarded request headers never establish a role.
HOSTS = frozenset({"127.0.0.1:8790", "127.0.0.1:18790"})
LOOPBACK = frozenset({"127.0.0.1", "::1"})
TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,110}")
TIMEOUT = 60
CATALOG_SECONDS = 15
STATUS_TOOL = "cockpit__connector_status"
WRITE_UNKNOWN = "This write may have happened. Inspect the service before retrying; the gateway did not replay it."
# Each name was reviewed as a read operation. Other names (even with readOnlyHint) require write access.
# Sources and review scope are recorded in docs/CONNECTOR-GATEWAY.md.
READS = {
    "linear": frozenset({
        "list_comments", "get_document", "list_documents", "get_issue", "list_issues", "list_issue_statuses",
        "get_issue_status", "list_issue_labels", "list_projects", "get_project", "list_project_labels",
        "list_teams", "get_team", "list_users", "get_user", "list_cycles", "get_cycle", "search_documentation",
    }),
    "slack": frozenset({
        "slack_search_all", "slack_search_messages", "slack_search_files", "slack_search_channels",
        "slack_search_users", "slack_search_emoji", "slack_read_channel", "slack_read_thread",
        "slack_read_file", "slack_read_canvas", "slack_get_user_info", "slack_get_user_profile",
        "slack_get_channel_info", "slack_get_conversation_info", "slack_get_conversation_history",
        "slack_get_thread_replies", "slack_get_channel_members", "slack_get_reactions", "slack_list_user_channels",
        "slack_read_user_profile", "slack_search_emojis",
    }),
    "notion": frozenset({
        "notion-search", "notion-fetch", "notion-get-comments", "notion-get-teams", "notion-get-users",
        "notion-get-self", "notion-connector-search", "notion-list-private-pages", "notion-list-shared-pages",
        "notion-list-favorite-pages", "notion-list-recent-pages", "notion-list-page-backlinks",
        "notion-query-meeting-notes", "notion-get-groups", "notion-get-tool-access",
        "notion-query-data-sources", "notion-query-multiple-data-sources", "notion-download-attachment",
        "notion-get-file-download-urls", "notion-get-suggested-edit", "notion-view-version", "notion-get-version-changes",
        "notion-get-async-task", "notion-download-skill", "notion-get-enabled-skills", "notion-memory-search",
        "notion-search-agents", "notion-query-sessions", "notion-search-sessions", "notion-get-session-status",
        "notion-wait-session", "notion-list-session-events", "notion-read-session-event",
        "notion-calendar-list-calendars", "notion-calendar-list-events", "notion-calendar-list-coworkers-events",
        "notion-calendar-list-calendar-resources", "notion-calendar-list-contacts",
    }),
}


def reviewed_read(provider: str, name: str, arguments: dict[str, Any]) -> bool:
    return name in READS[provider] and (
        provider != "notion" or name != "notion-query-data-sources" or arguments.get("mode") in {"rows", "view"}
    )


def input_schema(provider: str, tool: types.Tool, access: str) -> dict[str, Any]:
    if access == "read" and provider == "notion" and tool.name == "notion-query-data-sources":
        # The reviewed rows/view modes read data. Future or SQL modes need their own contract review.
        restriction = {"type": "object", "properties": {"mode": {"enum": ["rows", "view"]}}, "required": ["mode"]}
        return tool.inputSchema | {"allOf": [*tool.inputSchema.get("allOf", []), restriction]}
    return tool.inputSchema


async def refuse_tool_redirect(response: httpx.Response) -> None:
    # The SDK follows same-origin redirects independently of httpx.follow_redirects. A mutation's POST must
    # be sent once, even when a provider answers with a redirect after possibly applying the operation.
    if 300 <= response.status_code < 400 and response.request.method == "POST":
        message = json.loads(response.request.content)
        if message.get("method") == "tools/call":
            raise GatewayError("Connector tool calls do not follow redirects")


async def upstream_message(message: object) -> None:
    """End pending SDK requests when the transport reports an error instead of a usable response."""
    if isinstance(message, Exception):
        raise GatewayError("Connector returned an invalid response") from None


def safe_schema(schema: dict[str, Any]) -> None:
    """Validation must resolve only local schema references, never fetch an upstream-provided URL."""
    if len(canonical(schema).encode()) > 100_000:
        raise GatewayError("Connector tool schema is too large")

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (not isinstance(item, str) or not item.startswith("#")):
                    raise GatewayError("Connector tool schema references an external resource")
                if key == "$id":
                    raise GatewayError("Connector tool schema sets an external schema identity")
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise GatewayError("Connector published an invalid input schema") from None


class CatalogLookup(NamedTuple):
    tools: list[types.Tool]
    status: dict[str, Any]
    authorization: tuple[str, str | None] | None = None


class Gateway:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.catalog_checks: dict[tuple[str, str], asyncio.Task[CatalogLookup]] = {}
        self.server: Server = Server("Cockpit connectors")
        self.manager = StreamableHTTPSessionManager(
            self.server, json_response=True, stateless=True,
            security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                        allowed_hosts=list(HOSTS), allowed_origins=[]),
        )
        @self.server.list_tools()
        async def list_tools() -> list[types.Tool]:
            tools, _, _ = await self.catalog(self.principal())
            return tools

        @self.server.call_tool(validate_input=False)
        async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
            try:
                return await self.call(self.principal(), name, arguments)
            except GatewayError as exc:
                return types.CallToolResult(content=[types.TextContent(type="text", text=str(exc))], isError=True)

    async def catalog(self, client: str) -> tuple[list[types.Tool], list[dict[str, Any]], str]:
        tools = [types.Tool(name=STATUS_TOOL,
                            description="Check this client's read/write permissions and which connectors need sign-in.",
                            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
                            annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True))]
        statuses: list[dict[str, Any]] = []
        catalogs = await asyncio.gather(*(self.provider_catalog(provider) for provider in PROVIDERS))
        # Recheck grants and client permissions after all upstream waits.
        catalogs = await asyncio.gather(*(self.confirm_catalog(provider, lookup)
                                          for provider, lookup in zip(PROVIDERS, catalogs, strict=True)))
        access = self.store.access(client)
        for lookup, provider in zip(catalogs, PROVIDERS, strict=True):
            status = lookup.status
            visible: list[types.Tool] = []
            try:
                for tool in lookup.tools:
                    if access == "read" and tool.name not in READS[provider]:
                        continue
                    if TOOL_NAME.fullmatch(tool.name) is None:
                        raise GatewayError("Connector published an invalid tool name")
                    schema = input_schema(provider, tool, access)
                    safe_schema(schema)
                    readonly = tool.name in READS[provider] and not (
                        access == "read_write" and provider == "notion" and tool.name == "notion-query-data-sources"
                    )
                    visible.append(types.Tool(
                        name=f"{provider}__{tool.name}", title=tool.title, description=tool.description,
                        inputSchema=schema,
                        annotations=types.ToolAnnotations(readOnlyHint=readonly, destructiveHint=not readonly,
                                                          idempotentHint=readonly, openWorldHint=True),
                    ))
                tools.extend(visible)
                if status["available"]:
                    status["tools"] = len(visible)
            except GatewayError as exc:
                log.error("Connector schema unavailable: provider=%s", provider)
                status.update(available=False, error=str(exc))
            statuses.append(status)
        return tools, statuses, access

    async def provider_catalog(self, provider: str) -> CatalogLookup:
        try:
            row = self.store.provider(provider)
        except GatewayError:
            return CatalogLookup([], {"provider": provider, "configured": False, "available": False})
        status: dict[str, Any] = {"provider": provider, "configured": True, "available": False,
                                  **gateway_claude.status(row)}
        if not row["grant"]:
            error = (str(gateway_claude.signin_error(provider)) if gateway_claude.shared(row)
                     else "Signed out; run cockpit connectors signin " + provider)
            return CatalogLookup([], status | {"error": error})
        # Reconnecting clients can all ask for the same tool list. Share only the in-flight upstream lookup
        # for this grant. catalog() still checks and filters by each client's current permissions.
        key = (provider, row["generation"])
        check = self.catalog_checks.get(key)
        if check is None:
            check = asyncio.create_task(self.check_catalog(key))
            self.catalog_checks[key] = check
        result = await asyncio.shield(check)
        return CatalogLookup(result.tools, status | result.status, result.authorization)

    async def confirm_catalog(self, provider: str, lookup: CatalogLookup) -> CatalogLookup:
        if lookup.authorization is None:
            return lookup
        try:
            async with asyncio.timeout(CATALOG_SECONDS):
                return await asyncio.to_thread(self.current_catalog, provider, lookup, *lookup.authorization)
        except (sqlite3.OperationalError, TimeoutError) as exc:
            log.error("Connector authorization check failed: provider=%s exception=%s", provider, type(exc).__name__)
            return CatalogLookup([], lookup.status | {"available": False, "error": "Could not check connector authorization"})

    def current_catalog(self, provider: str, lookup: CatalogLookup, generation: str,
                        fingerprint: str | None) -> CatalogLookup:
        status = lookup.status
        try:
            try:
                gateway_claude.assert_current(self.store, provider, generation, fingerprint)
            finally:
                current = self.store.provider(provider)
                status = status | gateway_claude.status(current)
            if current["generation"] != generation or not current["grant"]:
                raise GatewayError("Connector authorization changed while loading tools")
        except GatewayError as exc:
            log.error("Connector unavailable: provider=%s exception=%s", provider, type(exc).__name__)
            return CatalogLookup([], status | {"available": False, "error": str(exc)})
        return CatalogLookup(lookup.tools, status)

    async def check_catalog(self, key: tuple[str, str]) -> CatalogLookup:
        provider, expected_generation = key
        try:
            async with asyncio.timeout(CATALOG_SECONDS), self.upstream(
                provider, catalog_generation=expected_generation,
            ) as (session, generation, fingerprint):
                tools = await self.tools(session)
            return CatalogLookup(tools, {"available": True}, (generation, fingerprint))
        except (GatewayError, TimeoutError) as exc:
            log.error("Connector unavailable: provider=%s exception=%s", provider, type(exc).__name__)
            error = str(exc) if isinstance(exc, GatewayError) else "Connector catalog timed out"
            return CatalogLookup([], {"error": error})
        finally:
            self.catalog_checks.pop(key, None)

    def principal(self) -> str:
        request = self.server.request_context.request
        if not isinstance(request, Request):
            raise GatewayError("Missing gateway request context")
        client = request.scope.get("state", {}).get("gateway_client")
        if not client:
            raise GatewayError("Missing gateway client identity")
        # Recheck after any wait, rather than trusting the lifetime of an MCP session.
        header = request.headers.get("authorization")
        if self.store.authenticate(header) != client:
            raise GatewayError("Gateway client was revoked")
        return client

    @contextlib.asynccontextmanager
    async def upstream(self, provider: str, *, catalog_generation: str | None) -> AsyncGenerator[tuple[ClientSession, str, str | None]]:
        # Context covers credential refresh and all SDK child tasks; headers are constructed from this grant only.
        try:
            with private_upstream(provider, catalog=catalog_generation is not None):
                async with asyncio.timeout(TIMEOUT):
                    token, generation = await asyncio.to_thread(gateway_oauth.access_token, self.store, provider)
                    if catalog_generation is not None and generation != catalog_generation:
                        raise GatewayError("Connector authorization changed while loading tools")
                    fingerprint = await asyncio.to_thread(gateway_claude.token_fingerprint, self.store, provider, token, generation)
                    async with (
                        httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, timeout=TIMEOUT,
                                          follow_redirects=False, trust_env=False,
                                          event_hooks={"response": [refuse_tool_redirect]}) as client,
                        streamable_http_client(PROVIDERS[provider], http_client=client) as (read, write, _),
                        ClientSession(read, write, read_timeout_seconds=timedelta(seconds=TIMEOUT),
                                      message_handler=upstream_message) as session,
                    ):
                        await session.initialize()
                        yield session, generation, fingerprint
        except GatewayError:
            raise
        except Exception as exc:  # noqa: BLE001 - network/SDK exceptions can echo credentials; sanitize this boundary
            cause = exc
            while isinstance(cause, ExceptionGroup) and len(cause.exceptions) == 1:
                cause = cause.exceptions[0]
            if isinstance(cause, GatewayError):
                raise cause from None
            if catalog_generation is None:
                log.error("Connector request failed (%s); upstream details withheld", type(exc).__name__)
            raise GatewayError(f"{provider} request failed; check its sign-in and try again") from None

    @staticmethod
    async def tools(session: ClientSession) -> list[types.Tool]:
        result = await session.list_tools()
        tools = list(result.tools)
        cursors: set[str] = set()
        while result.nextCursor:
            if result.nextCursor in cursors or len(tools) > 2000:
                raise GatewayError("Connector returned an invalid tools page sequence")
            cursors.add(result.nextCursor)
            result = await session.list_tools(params=types.PaginatedRequestParams(cursor=result.nextCursor))
            tools.extend(result.tools)
        if len({tool.name for tool in tools}) != len(tools):
            raise GatewayError("Connector published duplicate tool names")
        return tools

    async def call(self, client: str, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        access = self.store.access(client)
        if name == STATUS_TOOL:
            if arguments:
                raise GatewayError("Connector status takes no arguments")
            _, statuses, access = await self.catalog(client)
            payload = {"mode": access, "connectors": statuses}
            return types.CallToolResult(content=[types.TextContent(type="text", text=canonical(payload))],
                                        structuredContent=payload)
        provider, separator, tool_name = name.partition("__")
        if not separator or provider not in PROVIDERS or not TOOL_NAME.fullmatch(tool_name):
            raise GatewayError("Unknown connector tool")
        if len(canonical(arguments).encode()) > 64_000:
            raise GatewayError("Tool arguments are too large")
        readonly = reviewed_read(provider, tool_name, arguments)
        if access == "read" and not readonly:
            with self.store.connection() as conn:
                self.store.audit(conn, "denied", client, provider, tool_name)
            raise GatewayError("This connector gateway allows reviewed read tools only")
        required_access, operation = ("read", "read") if readonly else ("read_write", "write")
        dispatched = False
        try:
            async with self.upstream(provider, catalog_generation=None) as (session, generation, fingerprint):
                tools = await self.tools(session)
                tool = next((tool for tool in tools if tool.name == tool_name), None)
                if tool is None:
                    raise GatewayError("Connector no longer lists this tool")
                schema = input_schema(provider, tool, self.store.access(client))
                safe_schema(schema)
                try:
                    Draft202012Validator(schema, registry=Registry()).validate(arguments)
                except (ValidationError, Unresolvable):
                    raise GatewayError("Arguments do not match the connector's current input schema") from None
                await asyncio.to_thread(gateway_claude.assert_current, self.store, provider, generation, fingerprint)
                with self.store.connection() as conn:
                    if not self.store.active(conn, client, provider, generation, required_access):
                        raise GatewayError("Client access changed or the client or connector was revoked before execution")
                    self.store.audit(conn, operation + ".started", client, provider, tool_name)
                dispatched = True
                result = await session.call_tool(tool_name, arguments)
            with self.store.connection() as conn:
                self.store.audit(conn, operation + (".error" if result.isError else ".completed"), client, provider, tool_name)
            return result
        except BaseException as exc:
            if dispatched:
                try:
                    with self.store.connection() as conn:
                        self.store.audit(conn, "read.failed" if readonly else "write.unknown", client, provider, tool_name)
                except Exception as audit_error:  # noqa: BLE001 - failed audit storage must preserve the unknown-write warning
                    log.error("Connector audit failed: client=%s provider=%s exception=%s", client, provider, type(audit_error).__name__)
                if not readonly:
                    log.error("Connector write outcome unknown: client=%s provider=%s tool=%s", client, provider, tool_name)
                    if isinstance(exc, Exception):
                        raise GatewayError(WRITE_UNKNOWN) from None
            raise

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # The SDK also logs malformed client envelopes before a tool handler runs.
        with private_upstream("gateway"):
            await self.manager.handle_request(scope, receive, send)
