"""Private connector identities, hashed client credentials, and metadata-only audits.

The OS account that runs Cockpit is trusted. The ordinary Cockpit bearer token cannot read this store through HTTP.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

PROVIDERS = {
    "linear": "https://mcp.linear.app/mcp",
    "slack": "https://mcp.slack.com/mcp",
    "notion": "https://mcp.notion.com/mcp",
}
CLIENT_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9 ._-]{0,79}")
ACCESS = frozenset({"read", "read_write"})


class GatewayError(Exception):
    """A safe error message. Upstream bodies and exceptions may contain credentials and stay out of it."""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _private(path: Path, directory: bool) -> None:
    found = path.lstat()
    expected = stat.S_ISDIR(found.st_mode) if directory else stat.S_ISREG(found.st_mode)
    if not expected or found.st_uid != os.getuid() or stat.S_IMODE(found.st_mode) & 0o077:
        raise GatewayError(f"{path}: requires an owned {'0700 directory' if directory else '0600 file'}")


class Store:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        _private(folder, True)
        self.path = folder / "gateway.sqlite3"
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        _private(self.path, False)
        with self.connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS clients (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
                    created REAL NOT NULL, revoked REAL,
                    access TEXT NOT NULL CHECK (access IN ('read', 'read_write'))
                )
            """)
            # Existing credentials retain read access. Run the migration under the same immediate transaction
            # as the schema check, so concurrent server/administrator startup cannot add the column twice.
            if "access" not in {row["name"] for row in conn.execute("PRAGMA table_info(clients)")}:
                conn.execute("ALTER TABLE clients ADD COLUMN access TEXT NOT NULL DEFAULT 'read' "
                             "CHECK (access IN ('read', 'read_write'))")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS providers (
                    name TEXT PRIMARY KEY, config TEXT NOT NULL, grant TEXT, pending TEXT,
                    generation TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, at REAL NOT NULL, client TEXT, provider TEXT,
                    tool TEXT, status TEXT NOT NULL
                )
            """)

    @contextlib.contextmanager
    def connection(self) -> Generator[sqlite3.Connection]:
        _private(self.path, False)
        conn = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            # An immediate transaction serializes credential changes across server workers and CLI processes.
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextlib.contextmanager
    def provider_lock(self, name: str) -> Generator[None]:
        if name not in PROVIDERS:
            raise GatewayError("Unknown connector")
        path = self.folder / f"{name}.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            _private(path, False)
            deadline = time.monotonic() + 20
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise GatewayError(f"{name} credential operation is busy; try again") from None
                    time.sleep(0.025)
            yield
        finally:
            os.close(fd)

    @staticmethod
    def audit(conn: sqlite3.Connection, status: str, client: str | None, provider: str | None,
              tool: str | None) -> None:
        conn.execute("INSERT INTO audit (at, client, provider, tool, status) VALUES (?, ?, ?, ?, ?)",
                     (time.time(), client, provider, tool, status))

    def create_client(self, name: str, access: str) -> tuple[str, str]:
        if CLIENT_NAME.fullmatch(name) is None:
            raise GatewayError("Client name must use 1–80 letters, numbers, spaces, dots, underscores or hyphens")
        if access not in ACCESS:
            raise GatewayError("Client access must be read or read_write")
        identity, token = secrets.token_hex(12), "cgw_" + secrets.token_urlsafe(40)
        with self.connection() as conn:
            conn.execute("INSERT INTO clients (id, name, token_hash, created, revoked, access) VALUES (?, ?, ?, ?, NULL, ?)",
                         (identity, name, hashlib.sha256(token.encode()).hexdigest(), time.time(), access))
            self.audit(conn, "client.created", identity, None, None)
        return identity, token

    def clients(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("SELECT id, name, created, revoked, access FROM clients ORDER BY created")]

    def access(self, client: str) -> str:
        with self.connection() as conn:
            row = conn.execute("SELECT access FROM clients WHERE id=? AND revoked IS NULL", (client,)).fetchone()
        if row is None:
            raise GatewayError("Gateway client was revoked or does not exist")
        if row["access"] not in ACCESS:
            raise GatewayError("Gateway client has invalid access")
        return row["access"]

    def update_access(self, client: str, access: str) -> None:
        if access not in ACCESS:
            raise GatewayError("Client access must be read or read_write")
        with self.connection() as conn:
            if conn.execute("UPDATE clients SET access=? WHERE id=? AND revoked IS NULL", (access, client)).rowcount != 1:
                raise GatewayError("Active client not found")
            self.audit(conn, "client.access." + access, client, None, None)

    def authenticate(self, header: str | None) -> str | None:
        if not header or not header.startswith("Bearer cgw_") or len(header) > 200:
            return None
        hashed = hashlib.sha256(header.removeprefix("Bearer ").encode()).hexdigest()
        with self.connection() as conn:
            row = conn.execute("SELECT id, token_hash FROM clients WHERE token_hash = ? AND revoked IS NULL",
                               (hashed,)).fetchone()
        return row["id"] if row and hmac.compare_digest(row["token_hash"], hashed) else None

    def revoke(self, client: str) -> None:
        with self.connection() as conn:
            if conn.execute("UPDATE clients SET revoked = ? WHERE id = ? AND revoked IS NULL",
                            (time.time(), client)).rowcount != 1:
                raise GatewayError("Active client not found")
            self.audit(conn, "client.revoked", client, None, None)

    @staticmethod
    def active(conn: sqlite3.Connection, client: str, provider: str, generation: str, required_access: str) -> bool:
        if required_access not in ACCESS:
            raise ValueError("Invalid required access")
        return bool(
            conn.execute("SELECT 1 FROM clients WHERE id = ? AND revoked IS NULL AND (? = 'read' OR access = ?)",
                         (client, required_access, required_access)).fetchone()
            and conn.execute("SELECT 1 FROM providers WHERE name = ? AND generation = ? AND grant IS NOT NULL",
                             (provider, generation)).fetchone()
        )

    def provider(self, name: str) -> dict[str, Any]:
        if name not in PROVIDERS:
            raise GatewayError("Unknown connector")
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM providers WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise GatewayError(f"Configure {name} with cockpit connectors configure first")
        return {**dict(row), **{key: json.loads(row[key]) if row[key] else None for key in ("config", "grant", "pending")}}

    def configure(self, name: str, config: dict[str, Any]) -> None:
        if name not in PROVIDERS:
            raise GatewayError("Unknown connector")
        with self.provider_lock(name), self.connection() as conn:
            conn.execute("INSERT INTO providers VALUES (?, ?, NULL, NULL, ?) ON CONFLICT(name) DO UPDATE SET "
                         "config=excluded.config, grant=NULL, pending=NULL, generation=excluded.generation",
                         (name, canonical(config), secrets.token_hex(16)))
            self.audit(conn, "provider.configured", None, name, None)

    def share_claude(self, name: str, config: dict[str, Any]) -> None:
        if name not in PROVIDERS:
            raise GatewayError("Unknown connector")
        with self.provider_lock(name), self.connection() as conn:
            # This grant is an activation marker only. Source credentials remain in Claude's own file.
            conn.execute("INSERT INTO providers VALUES (?, ?, ?, NULL, ?) ON CONFLICT(name) DO UPDATE SET "
                         "config=excluded.config, grant=excluded.grant, pending=NULL, generation=excluded.generation",
                         (name, canonical(config), canonical({"shared": True}), secrets.token_hex(16)))
            self.audit(conn, "provider.shared_claude", None, name, None)

    def signout(self, name: str) -> None:
        with self.provider_lock(name), self.connection() as conn:
            if conn.execute("UPDATE providers SET grant=NULL, pending=NULL, generation=? WHERE name=?",
                            (secrets.token_hex(16), name)).rowcount != 1:
                raise GatewayError("Connector not configured")
            self.audit(conn, "provider.signed_out", None, name, None)

    def save_identity(self, name: str, field: str, value: dict[str, Any] | None) -> None:
        # Caller holds provider_lock for the whole OAuth exchange, refresh or signout operation.
        if field not in {"grant", "pending"}:
            raise ValueError("Invalid identity field")
        with self.connection() as conn:
            conn.execute(f"UPDATE providers SET {field}=? WHERE name=?",
                         (canonical(value) if value is not None else None, name))
