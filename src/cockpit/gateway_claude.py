"""Read explicitly shared Claude authorizations; Claude Code owns their renewal and storage."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from cockpit import accounts, config
from cockpit.config import Account
from cockpit.gateway_store import PROVIDERS, GatewayError, Store, canonical

log = logging.getLogger(__name__)
SERVER_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")
RENEW_BEFORE = 300
NATIVE_TIMEOUT = 60
FAILURE_COOLDOWN = 60
MAX_SOURCE_BYTES = 10_000_000


def shared(row: dict[str, Any]) -> bool:
    return row["config"].get("authorization_source") == "claude"


def signin_error(provider: str) -> GatewayError:
    return GatewayError(f"{provider} uses Claude authorization; sign in through the existing Cockpit connector sign-in card")


def _profile(account: Account) -> dict[str, Any]:
    return {"account": account.name, "folder": str(account.folder), "state": str(account.state), "default": account.default}


def _json(path: Path) -> dict[str, Any]:
    # Open the final file without following a link, then validate the descriptor actually read. Claude may
    # atomically replace it; we never modify it or create a backup containing its credentials.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r") as source:
            found = os.fstat(source.fileno())
            if (not stat.S_ISREG(found.st_mode) or found.st_uid != os.getuid()
                    or stat.S_IMODE(found.st_mode) & 0o077 or found.st_size > MAX_SOURCE_BYTES):
                raise GatewayError("Claude source files require owned private regular files")
            document = json.load(source)
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise GatewayError("Claude source file is unavailable or invalid; check the existing Cockpit connector sign-in") from None
    if not isinstance(document, dict):
        raise GatewayError("Claude source file must contain an object")
    return document


def _account(binding: dict[str, Any]) -> Account:
    try:
        account = config.account(binding["account"])
    except (KeyError, LookupError, TypeError):
        raise GatewayError("The shared Claude account is no longer configured; share it again explicitly") from None
    if _profile(account) != {key: binding.get(key) for key in ("account", "folder", "state", "default")}:
        raise GatewayError("The shared Claude account profile changed; share it again explicitly")
    try:
        found = account.folder.lstat()
    except OSError:
        raise GatewayError("The shared Claude account folder is unavailable") from None
    if (not stat.S_ISDIR(found.st_mode) or found.st_uid != os.getuid() or stat.S_IMODE(found.st_mode) & 0o077):
        raise GatewayError("The shared Claude account requires an owned private directory")
    return account


def _server(account: Account, provider: str, name: Any) -> tuple[dict[str, Any], str]:
    if not isinstance(name, str) or SERVER_NAME.fullmatch(name) is None:
        raise GatewayError("Invalid shared Claude server name")
    servers = _json(account.state).get("mcpServers")
    server = servers.get(name) if isinstance(servers, dict) else None
    if not isinstance(server, dict) or server.get("type") != "http" or server.get("url") != PROVIDERS[provider]:
        raise GatewayError(f"The Claude server must use the configured {provider} HTTPS MCP URL exactly")
    headers = server.get("headers")
    if headers is None:
        headers = {}
    if not isinstance(headers, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
        raise GatewayError("Invalid Claude server headers")
    shape = {"type": "http", "url": server["url"], "headers": headers}
    key = name + "|" + hashlib.sha256(json.dumps(shape, separators=(",", ":")).encode()).hexdigest()[:16]
    return server, key


def _fingerprint(value: dict[str, Any]) -> str:
    try:
        return hashlib.sha256(canonical(value).encode()).hexdigest()
    except (ValueError, TypeError, RecursionError):
        raise GatewayError("Invalid Claude source metadata") from None


def source(provider: str, binding: dict[str, Any]) -> tuple[Account, dict[str, Any], str]:
    """Read and validate both source files anew, including the bound profile and server identity."""
    account = _account(binding)
    name = binding.get("server")
    server, key = _server(account, provider, name)
    if key != binding.get("credential_key") or _fingerprint(server) != binding.get("server_fingerprint"):
        raise GatewayError("The shared Claude server configuration changed; share it again explicitly")
    entries = _json(account.folder / ".credentials.json").get("mcpOAuth")
    entry = entries.get(key) if isinstance(entries, dict) else None
    if not isinstance(entry, dict):
        raise signin_error(provider)
    if entry.get("serverName") != name or entry.get("serverUrl") != PROVIDERS[provider]:
        raise GatewayError("The Claude authorization does not match the shared server identity")
    for field in ("clientId", "accessToken"):
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip() or any(ord(char) < 33 or ord(char) == 127 for char in value):
            raise GatewayError("The Claude authorization has invalid token or client metadata")
    for field in ("refreshToken", "clientSecret", "issuer", "redirectUri", "scope"):
        if field in entry and not isinstance(entry[field], str):
            raise GatewayError("The Claude authorization has invalid metadata")
    expires = entry.get("expiresAt")
    try:
        finite = isinstance(expires, (float, int)) and not isinstance(expires, bool) and math.isfinite(expires)
    except OverflowError:
        finite = False
    if not finite or expires <= 0:
        raise GatewayError("The Claude authorization has an invalid expiry")
    return account, entry, _fingerprint(entry)


def bind(store: Store, provider: str, account_name: str, name: str) -> None:
    if provider not in PROVIDERS:
        raise GatewayError("Unknown connector")
    try:
        account = config.account(account_name)
    except LookupError:
        raise GatewayError("Choose an existing configured Claude account") from None
    binding = _profile(account) | {"server": name}
    _account(binding)
    server, key = _server(account, provider, name)
    binding |= {"credential_key": key, "server_fingerprint": _fingerprint(server)}
    source(provider, binding)
    store.share_claude(provider, {"authorization_source": "claude", "source": binding})


def _user_scope(account: Account, server: str, working: Path) -> None:
    """Reject inherited project configuration that could select a different native MCP target."""
    projects = _json(account.state).get("projects", {})
    if not isinstance(projects, dict):
        raise GatewayError("Claude source has invalid project metadata")
    for ancestor in (working, *working.parents):
        try:
            # Claude loads ancestor .mcp.json even outside a Git checkout. Presence is enough to refuse:
            # this check should use only the explicitly validated user server, irrespective of file content.
            for name in (".mcp.json", ".git"):
                try:
                    (ancestor / name).lstat()
                except FileNotFoundError:
                    continue
                raise GatewayError("Claude connector check requires a private data folder outside project MCP configuration")
        except OSError:
            raise GatewayError("Claude connector check could not verify its private working directory") from None
        project = projects.get(str(ancestor))
        if isinstance(project, dict) and server in (project.get("mcpServers") or {}):
            raise GatewayError("Claude connector check found a project server overriding its configured user server")


def native_check(account: Account, server: str, data_folder: Path) -> None:
    """Ask Claude's supported read-only connection check once; native locking/retries belong to Claude."""
    binary = shutil.which("claude") or str(config.HOME / ".local" / "bin" / "claude")
    try:
        resolved = Path(binary).resolve(strict=True)
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise GatewayError("Claude Code is required to renew shared connector authorization")
        binary = str(resolved)
        # A fresh private directory avoids same-name local servers. Reject inherited project configuration
        # too, so the native command selects the user server we validated.
        with tempfile.TemporaryDirectory(prefix="claude-check-", dir=data_folder) as working:
            _user_scope(account, server, Path(working).resolve())
            result = subprocess.run([*accounts.claude_env(account), binary, "mcp", "get", server],
                                    cwd=working, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=NATIVE_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        raise GatewayError("Claude connector check failed or timed out; check the existing Cockpit connector sign-in") from None
    if result.returncode != 0:
        raise GatewayError("Claude connector check failed; check the existing Cockpit connector sign-in")


def access_token(store: Store, provider: str, row: dict[str, Any], require_sharing: bool) -> tuple[str, str]:
    # Caller holds provider_lock across read, native check, reread and return. A cancelled asyncio catalog
    # leaves this worker owning the lock until subprocess.run completes or reaps its bounded child.
    if require_sharing and not row["grant"]:
        raise signin_error(provider)
    binding = row["config"]["source"]
    account, entry, fingerprint = source(provider, binding)
    if entry["expiresAt"] / 1000 <= time.time() + RENEW_BEFORE:
        failure = row["pending"]
        if failure and failure.get("fingerprint") == fingerprint and failure.get("retry_after", 0) > time.time():
            raise GatewayError("Claude connector renewal recently failed; wait before checking the existing sign-in again")
        try:
            native_check(account, binding["server"], store.folder)
            account, entry, fingerprint = source(provider, binding)
            if entry["expiresAt"] / 1000 <= time.time() + RENEW_BEFORE:
                raise GatewayError("Claude connector check returned no usable renewed authorization")
        except GatewayError:
            try:
                _, _, fingerprint = source(provider, binding)
            except GatewayError:
                pass
            # This records no token. An unknown native outcome must not trigger another immediate refresh.
            store.save_identity(provider, "pending", {"fingerprint": fingerprint, "retry_after": time.time() + FAILURE_COOLDOWN})
            log.error("Shared Claude connector renewal failed: provider=%s", provider)
            raise
        store.save_identity(provider, "pending", None)
    # Profile/server changes during the native check or file reads must not yield the earlier token.
    _, current, current_fingerprint = source(provider, binding)
    if current_fingerprint != fingerprint:
        raise GatewayError("Claude authorization changed during acquisition; try again")
    return current["accessToken"], row["generation"]


def legacy_access_token(store: Store, provider: str) -> tuple[str, str]:
    """Trusted local sign-in checks retain native renewal ownership even while gateway sharing is disabled."""
    with store.provider_lock(provider):
        row = store.provider(provider)
        if not shared(row):
            raise GatewayError("The Claude sharing source changed during sign-in")
        return access_token(store, provider, row, require_sharing=False)


def token_fingerprint(store: Store, provider: str, token: str, generation: str) -> str | None:
    row = store.provider(provider)
    if row["generation"] != generation or not row["grant"]:
        raise GatewayError("Connector authorization changed before execution")
    if not shared(row):
        return None
    _, entry, fingerprint = source(provider, row["config"]["source"])
    if entry["accessToken"] != token:
        raise GatewayError("Claude authorization changed during acquisition; try again")
    return fingerprint


def assert_current(store: Store, provider: str, generation: str, fingerprint: str | None) -> None:
    if fingerprint is None:
        return
    row = store.provider(provider)
    if row["generation"] != generation or not row["grant"] or not shared(row):
        raise GatewayError("Connector authorization changed before execution")
    _, entry, current = source(provider, row["config"]["source"])
    if current != fingerprint or entry["expiresAt"] / 1000 <= time.time():
        raise GatewayError("Claude authorization changed or expired before execution; try again")


def status(row: dict[str, Any]) -> dict[str, Any]:
    """Saved source state only; availability still requires the gateway's live tools/list check."""
    is_shared = shared(row)
    result = {"authorization_source": "claude" if is_shared else "cockpit", "grant_stored": bool(row["grant"]) and not is_shared,
              "signin_pending": bool(row["pending"]) and not is_shared}
    if is_shared:
        result["sharing_enabled"] = bool(row["grant"])
    return result


def bound_store(account: Account, name: str) -> tuple[Store, str] | None:
    """Find an exact source binding for the legacy Cockpit sign-in flow without creating a store."""
    folder = config.DB_PATH.parent / "connectors"
    if not (folder / "gateway.sqlite3").exists():
        return None
    store = Store(folder)
    for provider in PROVIDERS:
        try:
            row = store.provider(provider)
        except GatewayError:
            continue
        binding = row["config"].get("source", {})
        if shared(row) and binding.get("account") == account.name and binding.get("server") == name:
            _account(row["config"]["source"])
            return store, provider
    return None
