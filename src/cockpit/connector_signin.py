"""Sign a Claude account in to an MCP connector, such as Slack or Linear, from a card on the page (SPEC SESSION-25).

A session that finds a connector signed out runs `cockpit signin start`. Cockpit first checks the sign-in it has:
a token that still works, or one that renews, needs nothing from the user. Otherwise it makes the sign-in link
itself, as Claude Code's own `/mcp` would (OAuth 2.1 with PKCE), and puts a step with it. The user opens the link,
approves, lands on a `http://localhost/callback?code=…` page that does not load, and pastes that address into the
card. Cockpit trades the code for tokens, stores them where Claude Code reads them (`mcpOAuth` in the account's
`.credentials.json`), checks that the connector lists its tools with them, and hands the step to the session.

What stays secret: the PKCE verifier and the client secret wait in a pending file of their own (0600, in a 0700
folder outside every repository), and the pasted address, the code and the tokens go nowhere but the token
endpoint and the credentials file: not the database, a message, a step or a log.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cockpit import db, store
from cockpit.config import Account, user_name

log = logging.getLogger(__name__)

# Claude Code's client id metadata document (draft-ietf-oauth-client-id-metadata-document), which names it and
# allows any localhost port in `http://localhost/callback`; a server that supports such documents takes it as the
# client id with no registration, as Claude Code does.
CLAUDE_CLIENT_METADATA = "https://claude.ai/oauth/claude-code-client-metadata"
MCP_PROTOCOL = "2025-06-18"
TIMEOUT_SECONDS = 20
# A token that ends within this many seconds counts as ended, so a check never reports one about to stop.
EXPIRY_MARGIN_SECONDS = 60
# A sign-in step's need: the prefix, the connector's name and the time it began, so a later sign-in to the same
# connector on the item is a new ask.
NEED_PREFIX = "signin:"


class SigninError(Exception):
    """The sign-in could not go on; the text says why in words the user or the session can act on, and holds no
    secret."""


class StaleAddress(SigninError):
    """The pasted address answers an older link than the card's."""


@dataclass(frozen=True)
class Discovery:
    """Where a connector signs in (RFC 9728 and RFC 8414)."""

    issuer: str
    authorize: str
    token: str
    registration: str | None
    metadata_documents: bool
    resource: str | None
    resource_metadata_url: str | None
    auth_metadata_url: str | None
    scopes: list[str]


def server_config(account: Account, name: str) -> dict[str, Any]:
    """The connector `name` as the account's state file configures it (`mcpServers`); only one reached over HTTP
    signs in this way."""
    servers = json.loads(account.state.read_text()).get("mcpServers") or {}
    if name not in servers:
        raise SigninError(
            f"No MCP server {name!r} in {account.state}; it has {', '.join(sorted(servers)) or 'none'}."
        )
    server = servers[name]
    if server.get("type") != "http" or not server.get("url"):
        raise SigninError(f"MCP server {name!r} is not an HTTP server with a url, so it does not sign in with OAuth.")
    return server


def credential_key(name: str, server: dict[str, Any]) -> str:
    """The key Claude Code files the server's tokens under in `mcpOAuth`: its name, then a hash of its type, url
    and headers as compact JSON."""
    shape = {"type": server["type"], "url": server["url"], "headers": server.get("headers") or {}}
    digest = hashlib.sha256(json.dumps(shape, separators=(",", ":")).encode()).hexdigest()
    return f"{name}|{digest[:16]}"


def read_entry(account: Account, key: str) -> dict[str, Any] | None:
    path = account.folder / ".credentials.json"
    if not path.exists():
        return None
    return (json.loads(path.read_text()).get("mcpOAuth") or {}).get(key)


def write_entry(account: Account, key: str, entry: dict[str, Any]) -> None:
    """Store the entry, read again just before the write so a sign-in Claude Code wrote meanwhile stays: a 0600
    file beside the credentials, renamed over them. A lock file keeps two of Cockpit's own writes from crossing."""
    path = account.folder / ".credentials.json"
    lock = os.open(path.with_name(".credentials.json.cockpit-lock"), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        stored = json.loads(path.read_text()) if path.exists() else {}
        stored.setdefault("mcpOAuth", {})[key] = entry
        partial = path.with_name(f".credentials.json.{secrets.token_hex(4)}.partial")
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as file:
            json.dump(stored, file)
        os.replace(partial, path)
    finally:
        os.close(lock)


def clear_needs_auth(account: Account, name: str) -> None:
    """Take the server off Claude Code's list of servers that need a sign-in, so a new session tries it at once."""
    path = account.folder / "mcp-needs-auth-cache.json"
    if not path.exists():
        return
    marks = json.loads(path.read_text())
    if name in marks:
        del marks[name]
        path.write_text(json.dumps(marks))


def _request(url: str, data: bytes | None, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=data, headers={"User-Agent": "cockpit"} | headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers.items()), error.read()


def _json_at(url: str) -> dict[str, Any] | None:
    status, _, body = _request(url, None, {"Accept": "application/json"})
    if status != 200:
        return None
    try:
        found = json.loads(body)
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def _well_known(base: str, suffix: str) -> list[str]:
    """RFC 8414 and RFC 9728 put the path after the well-known name; servers also answer at the bare name."""
    parts = urllib.parse.urlsplit(base)
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    return [f"{origin}/.well-known/{suffix}{path}", f"{origin}/.well-known/{suffix}"] if path else [f"{origin}/.well-known/{suffix}"]


def discover(server_url: str) -> Discovery:
    resource_url = None
    resource = None
    for url in _well_known(server_url, "oauth-protected-resource"):
        resource = _json_at(url)
        if resource is not None:
            resource_url = url
            break
    issuer = (resource or {}).get("authorization_servers", [None])[0] or server_url
    auth = None
    auth_url = None
    for url in _well_known(issuer, "oauth-authorization-server") + _well_known(issuer, "openid-configuration"):
        auth = _json_at(url)
        if auth is not None:
            auth_url = url
            break
    if auth is None or not auth.get("authorization_endpoint") or not auth.get("token_endpoint"):
        raise SigninError(f"{issuer} publishes no OAuth authorization server metadata, so Cockpit cannot sign in there.")
    return Discovery(
        issuer=auth.get("issuer") or issuer,
        authorize=auth["authorization_endpoint"],
        token=auth["token_endpoint"],
        registration=auth.get("registration_endpoint"),
        metadata_documents=bool(auth.get("client_id_metadata_document_supported")),
        resource=(resource or {}).get("resource"),
        resource_metadata_url=resource_url,
        auth_metadata_url=auth_url,
        scopes=(resource or {}).get("scopes_supported") or [],
    )


def _redirect(port: int) -> str:
    return f"http://localhost:{port}/callback"


def _client(server: dict[str, Any], entry: dict[str, Any] | None, found: Discovery) -> dict[str, Any]:
    """The client the sign-in runs as, the way Claude Code picks it: the one the server's config names, then the
    one its last sign-in used, then Claude Code's metadata document, then a new registration."""
    configured = server.get("oauth") or {}
    if configured.get("clientId"):
        same = entry is not None and entry.get("clientId") == configured["clientId"]
        port = configured.get("callbackPort") or secrets.choice(range(49152, 65536))
        return {"client_id": configured["clientId"], "client_secret": same and entry.get("clientSecret") or None,
                "redirect_uri": _redirect(port)}
    if entry is not None and entry.get("clientId") and entry.get("redirectUri"):
        return {"client_id": entry["clientId"], "client_secret": entry.get("clientSecret"), "redirect_uri": entry["redirectUri"]}
    redirect = _redirect(secrets.choice(range(49152, 65536)))
    if found.metadata_documents:
        return {"client_id": CLAUDE_CLIENT_METADATA, "client_secret": None, "redirect_uri": redirect}
    if found.registration:
        body = json.dumps({
            "client_name": "Claude Code", "redirect_uris": [redirect], "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none",
        }).encode()
        status, _, answer = _request(found.registration, body, {"Content-Type": "application/json", "Accept": "application/json"})
        if status not in (200, 201):
            raise SigninError(f"{found.issuer} refused to register a client (HTTP {status}).")
        registered = json.loads(answer)
        return {"client_id": registered["client_id"], "client_secret": registered.get("client_secret"), "redirect_uri": redirect}
    raise SigninError(
        f"{found.issuer} needs a client id Cockpit does not have: set `oauth.clientId` on the server in the account's"
        " .claude.json, as Slack's is."
    )


def begin(account: Account, name: str) -> dict[str, Any]:
    """A new sign-in for the connector: the pending record (`pending`, kept secret) and what the card shows
    (`card`: the link and the host it opens)."""
    server = server_config(account, name)
    key = credential_key(name, server)
    entry = read_entry(account, key)
    found = discover(server["url"])
    client = _client(server, entry, found)
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(24)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    scope = " ".join(found.scopes) or (entry or {}).get("scope", "").replace(",", " ")
    query = {
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": client["redirect_uri"],
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    } | ({"scope": scope} if scope else {}) | ({"resource": found.resource} if found.resource else {})
    link = f"{found.authorize}{'&' if '?' in found.authorize else '?'}{urllib.parse.urlencode(query)}"
    pending = {
        "account": account.name, "server": name, "server_url": server["url"], "key": key, "state": state,
        "verifier": verifier, "client": client, "scope": scope, "discovery": found.__dict__, "started_at": time.time(),
    }
    return {"pending": pending, "card": {"link": link, "host": urllib.parse.urlsplit(found.authorize).netloc}}


def save_pending(folder: Path, step_id: int, pending: dict[str, Any]) -> None:
    """The pending sign-in of the step, replacing an earlier one: the folder 0700, the file 0600 from its first
    byte."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    partial = folder / f".{step_id}.{secrets.token_hex(4)}.partial"
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as file:
        json.dump(pending, file)
    os.replace(partial, folder / f"{step_id}.json")


def load_pending(folder: Path, step_id: int) -> dict[str, Any] | None:
    path = folder / f"{step_id}.json"
    return json.loads(path.read_text()) if path.exists() else None


def drop_pending(folder: Path, step_id: int) -> None:
    (folder / f"{step_id}.json").unlink(missing_ok=True)


def callback_query(address: str) -> dict[str, str]:
    """The query of the pasted address: the whole address, or only what follows its `?`."""
    text = address.strip()
    query = urllib.parse.urlsplit(text).query if "://" in text else text.lstrip("?")
    return {name: values[0] for name, values in urllib.parse.parse_qs(query).items()}


def _reason(error: object, status: int) -> str:
    """An OAuth error code such as invalid_grant, which the card can show: a server's free-text description, or a
    code that is not a plain word, might echo the pasted address back, so neither is kept."""
    if isinstance(error, str) and re.fullmatch(r"[a-z_]{1,40}", error):
        return error
    return f"HTTP {status}"


def _token_call(found: dict[str, Any], form: dict[str, str]) -> dict[str, Any]:
    """POST the form to the token endpoint; the answer's tokens, or SigninError with the server's own reason."""
    body = urllib.parse.urlencode({name: value for name, value in form.items() if value}).encode()
    status, _, answer = _request(found["token"], body, {"Content-Type": "application/x-www-form-urlencoded",
                                                        "Accept": "application/json"})
    try:
        tokens = json.loads(answer)
    except ValueError:
        raise SigninError(f"The token endpoint answered HTTP {status} with no JSON.") from None
    # Slack answers 200 with ok false; a user-token flow may nest the tokens under authed_user.
    if tokens.get("ok") is False or tokens.get("error") or status != 200:
        raise SigninError(f"The sign-in server refused the code: {_reason(tokens.get('error'), status)}.")
    tokens = tokens.get("authed_user") if not tokens.get("access_token") and tokens.get("authed_user") else tokens
    if not tokens.get("access_token"):
        raise SigninError("The sign-in server's answer holds no access token.")
    return tokens


def _entry(pending: dict[str, Any], tokens: dict[str, Any]) -> dict[str, Any]:
    """The `mcpOAuth` entry Claude Code reads, with the fields its own sign-in writes. Only this sign-in's tokens go
    in: an earlier refresh token may belong to another account."""
    found = pending["discovery"]
    client = pending["client"]
    entry = {
        "serverName": pending["server"], "serverUrl": pending["server_url"], "accessToken": tokens["access_token"],
        "issuer": found["issuer"], "redirectUri": client["redirect_uri"], "clientId": client["client_id"],
        "scope": tokens.get("scope") or pending["scope"],
        "discoveryState": {
            "authorizationServerUrl": found["issuer"], "oauthMetadataFound": True,
            "authServerMetadataUrl": found["auth_metadata_url"], "resourceMetadataUrl": found["resource_metadata_url"],
        },
    }
    if tokens.get("refresh_token"):
        entry["refreshToken"] = tokens["refresh_token"]
    if tokens.get("expires_in"):
        entry["expiresAt"] = int((time.time() + int(tokens["expires_in"])) * 1000)
    if client.get("client_secret"):
        entry["clientSecret"] = client["client_secret"]
    return entry


def renew(entry: dict[str, Any], found: dict[str, Any]) -> dict[str, Any]:
    """The entry with fresh tokens from its refresh token."""
    tokens = _token_call(found, {
        "grant_type": "refresh_token", "refresh_token": entry["refreshToken"], "client_id": entry["clientId"],
        "client_secret": entry.get("clientSecret") or "",
    })
    renewed = entry | {"accessToken": tokens["access_token"]}
    if tokens.get("refresh_token"):
        renewed["refreshToken"] = tokens["refresh_token"]
    if tokens.get("expires_in"):
        renewed["expiresAt"] = int((time.time() + int(tokens["expires_in"])) * 1000)
    return renewed


def _rpc(url: str, token: str, session: str | None, message: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """One JSON-RPC message over MCP's streamable HTTP; the answer (None for a notification) and the session id."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": MCP_PROTOCOL}
    if session:
        headers["Mcp-Session-Id"] = session
    status, answer_headers, body = _request(url, json.dumps(message).encode(), headers)
    session = next((value for name, value in answer_headers.items() if name.lower() == "mcp-session-id"), session)
    if status in (401, 403):
        raise SigninError(f"The connector refused the token (HTTP {status}).")
    if "id" not in message:
        return None, session
    if status != 200:
        raise SigninError(f"The connector answered {message['method']} with HTTP {status}.")
    text = body.decode()
    kind = next((value for name, value in answer_headers.items() if name.lower() == "content-type"), "")
    if "text/event-stream" in kind:
        datas = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        answers = [json.loads(data) for data in datas if data]
        answer = next((each for each in answers if each.get("id") == message["id"]), None)
    else:
        answer = json.loads(text)
    if not answer or "error" in answer:
        raise SigninError(f"The connector answered {message['method']} with an error.")
    return answer["result"], session


def tool_count(url: str, token: str) -> int:
    """Initialize an MCP session with the token and count the tools it lists: the check that the sign-in works."""
    _, session = _rpc(url, token, None, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": MCP_PROTOCOL, "capabilities": {}, "clientInfo": {"name": "cockpit", "version": "1"}},
    })
    _rpc(url, token, session, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    result, _ = _rpc(url, token, session, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    return len((result or {}).get("tools") or [])


def _live(entry: dict[str, Any]) -> bool:
    expires = entry.get("expiresAt")
    return bool(entry.get("accessToken")) and (expires is None or expires / 1000 > time.time() + EXPIRY_MARGIN_SECONDS)


def state_of(entry: dict[str, Any], tools: int) -> dict[str, Any]:
    """What a session and the user hear about a working sign-in."""
    return {"tools": tools, "expires_at": entry.get("expiresAt"), "renews": bool(entry.get("refreshToken"))}


def check(account: Account, name: str) -> dict[str, Any] | None:
    """The working sign-in the account has for the connector (`state_of`), renewing an ended token first; None
    when the user has to sign in."""
    server = server_config(account, name)
    key = credential_key(name, server)
    entry = read_entry(account, key)
    if entry is None:
        return None
    if _live(entry):
        try:
            return state_of(entry, tool_count(server["url"], entry["accessToken"]))
        except SigninError:
            pass
    if not entry.get("refreshToken"):
        return None
    try:
        renewed = renew(entry, discover(server["url"]).__dict__)
    except SigninError:
        return None
    # Stored before the check: the server may have replaced the refresh token, which ends the old one.
    write_entry(account, key, renewed)
    try:
        tools = tool_count(server["url"], renewed["accessToken"])
    except SigninError:
        return None
    clear_needs_auth(account, name)
    return state_of(renewed, tools)


def finish(account: Account, pending: dict[str, Any], address: str) -> dict[str, Any]:
    """Trade the pasted address's code for tokens, store them, renew them once and confirm the connector lists its
    tools; returns `state_of`. A SigninError says what went wrong; `stale` on it means the address is from an
    older link, and the pending sign-in stays."""
    query = callback_query(address)
    if query.get("error"):
        raise SigninError(f"The sign-in page answered: {_reason(query['error'], 200)}.")
    if "code" not in query:
        raise SigninError("That address holds no code. Paste the whole address of the page you landed on after you approved.")
    if query.get("state") != pending["state"]:
        raise StaleAddress("That address comes from an older sign-in link. Open the link on this card and approve again.")
    found = pending["discovery"]
    client = pending["client"]
    tokens = _token_call(found, {
        "grant_type": "authorization_code", "code": query["code"], "redirect_uri": client["redirect_uri"],
        "client_id": client["client_id"], "code_verifier": pending["verifier"],
        "client_secret": client.get("client_secret") or "", "resource": found["resource"] or "",
    })
    entry = _entry(pending, tokens)
    write_entry(account, pending["key"], entry)
    # One renewal now shows whether the sign-in lasts past its first token; a refusal leaves that token working.
    renews = False
    if entry.get("refreshToken"):
        try:
            entry = renew(entry, found)
            write_entry(account, pending["key"], entry)
            renews = True
        except SigninError:
            pass
    tools = tool_count(pending["server_url"], entry["accessToken"])
    clear_needs_auth(account, pending["server"])
    return state_of(entry, tools) | {"renews": renews}


# The step: Cockpit puts it when `cockpit signin start` finds no working sign-in, and the card's sign-in box finishes
# it. A session's other asks on the item go on as before: a sign-in step is not the item's card (store._cards).

SERVER_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def _label(name: str) -> str:
    return name[:1].upper() + name[1:]


def step_text(account: Account, name: str, why: str) -> dict[str, Any]:
    """The card's words: what to do, what the page after approving looks like, and where the sign-in goes."""
    label = _label(name)
    return {
        "ask": f"Sign Claude in to {label}: open the link, approve, and paste back the address you land on",
        "why": why,
        "body": (
            "After you approve, your browser lands on a localhost page that does not load, such as \"This site"
            " can't be reached\". That page's address carries the sign-in: copy it whole from the address bar into"
            f" the sign-in box. Cockpit finishes the sign-in, checks that {label} works, and tells the session."
        ),
        "details": [{
            "title": "Where the sign-in goes",
            "body": (
                f"Cockpit stores the sign-in in the {account.name} Claude account's credentials file, where Claude"
                " Code reads it, and keeps it out of this page, messages and logs. Sessions that start from then on"
                f" have {label}. A session already running may need `/mcp` to reconnect it. The pasted address works"
                " for a few minutes after you approve; when it has ended, the card shows a new link."
            ),
        }],
    }


def _open_step(conn: sqlite3.Connection, item_id: int, name: str, account: Account) -> dict[str, Any] | None:
    """The item's open sign-in step for the connector and account that is still on the page, not with the session.
    Each account has its own card, so a sign-in reaches the account and session that asked for it."""
    found = conn.execute(
        "SELECT id FROM steps WHERE item_id = ? AND need GLOB ? AND json_extract(signin, '$.account') = ?"
        " AND status = 'open' AND withdrawn_at IS NULL AND handoff_message_id IS NULL ORDER BY id DESC LIMIT 1",
        (item_id, f"{NEED_PREFIX}{name}:*", account.name),
    ).fetchone()
    return store.get_step(conn, found["id"]) if found is not None else None


def _set_signin(conn: sqlite3.Connection, step_id: int, signin: dict[str, Any]) -> None:
    conn.execute("UPDATE steps SET signin = ?, updated_at = ? WHERE id = ?", (json.dumps(signin), db.now_iso(), step_id))


def _relink(
    conn: sqlite3.Connection, folder: Path, step: dict[str, Any], account: Account, failure: str | None,
) -> dict[str, Any]:
    """A new link on the step's card, with what went wrong with the last one; the old link stops working."""
    began = begin(account, step["signin"]["server"])
    with db.transaction(conn):
        current = store.get_step(conn, step["id"])
        store.check_open_for_hand_off(current)
        _set_signin(conn, step["id"], current["signin"] | began["card"] | {
            "account": account.name, "started_at": began["pending"]["started_at"], "failure": failure,
        })
        save_pending(folder, step["id"], began["pending"])
    return store.get_step(conn, step["id"])


def start(
    conn: sqlite3.Connection, folder: Path, account: Account, name: str, item_id: int, why: str, session_id: str,
    actor: str,
) -> dict[str, Any]:
    """`cockpit signin start`: the working sign-in the account has (`working`), else the sign-in step (`step`),
    reusing one already open for the connector and account on the item."""
    if not SERVER_NAME.fullmatch(name):
        raise store.Invalid(f"{name!r} is not an MCP server name")
    try:
        state = check(account, name)
    except SigninError as error:
        raise store.Invalid(str(error)) from None
    if state is not None:
        return {"working": state}
    open_step = _open_step(conn, item_id, name, account)
    try:
        if open_step is not None:
            if load_pending(folder, open_step["id"]) is not None:
                return {"step": open_step, "reused": True}
            return {"step": _relink(conn, folder, open_step, account, None), "reused": True}
        began = begin(account, name)
    except SigninError as error:
        raise store.Invalid(str(error)) from None
    with db.transaction(conn):
        item = conn.execute("SELECT id, key, title FROM items WHERE id = ?", (item_id,)).fetchone()
        if item is None:
            raise store.NotFound(f"no item {item_id}")
        store.refuse_long_stored_title(item, {}, actor)
        signin = {"server": name, "account": account.name, "started_at": began["pending"]["started_at"],
                  "failure": None} | began["card"]
        put = store.upsert_step(conn, step_text(account, name, why) | {
            "item_id": item_id, "need": f"{NEED_PREFIX}{name}:{account.name}:{int(time.time())}", "session_id": session_id,
            "size": "small", "progress": 0, "signin": signin,
        }, actor)
        if put.get("skipped"):
            raise store.Conflict(put["skipped"])
        save_pending(folder, put["step"]["id"], began["pending"])
    return {"step": put["step"]}


def _finished_text(step: dict[str, Any], account: Account, state: dict[str, Any]) -> str:
    """What the session hears once the sign-in works; it names no token."""
    name = step["signin"]["server"]
    label = _label(name)
    expires = state["expires_at"]
    until = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(expires / 1000)) if expires else None
    if until is None:
        lasting = "Its token has no end time."
    elif state["renews"]:
        lasting = f"Its token works until {until}; Cockpit renewed it once, so Claude Code renews it from there."
    else:
        lasting = f"Its token works until {until} and does not renew, so a new sign-in is needed then."
    return (
        f"{user_name()} signed the {account.name} Claude account in to {label} on step {step['id']}. With the new"
        f" sign-in, {label} lists {state['tools']} tools. {lasting}\n\n"
        f"Sessions that start from now on have {label}. A session already running loaded its connectors when it"
        f" started: if its {label} tools still answer that they need a sign-in, `/mcp` in that session reconnects"
        f" {name}, or a new session has it.\n\n"
        f"Close step {step['id']} with `cockpit step done {step['id']}` once you have what you needed it for."
    )


def finish_step(
    conn: sqlite3.Connection, folder: Path, accounts: tuple[Account, ...], checkouts_dir: Path, step_id: int,
    address: str,
) -> dict[str, Any]:
    """The card's sign-in box: finish the step's sign-in from the pasted address and hand the step to its session
    with the result (`message`), or say what went wrong (`failure`), with a new link when the old one is spent."""
    step = store.get_step(conn, step_id)
    if step["signin"] is None:
        raise store.Invalid(f"step {step_id} is not a sign-in")
    store.check_open_for_hand_off(step)
    account = next((each for each in accounts if each.name == step["signin"]["account"]), None)
    if account is None:
        raise store.Invalid(f"no Claude account {step['signin']['account']!r} on this machine")
    pending = load_pending(folder, step_id)
    if pending is None:
        failure = "That sign-in link has ended. Open the new link and approve again."
        return {"failure": failure, "step": _relink(conn, folder, step, account, failure)}
    try:
        state = finish(account, pending, address)
    except StaleAddress as error:
        with db.transaction(conn):
            _set_signin(conn, step_id, step["signin"] | {"failure": str(error)})
        return {"failure": str(error), "step": store.get_step(conn, step_id)}
    except SigninError as error:
        failure = f"{error} Open the new link and approve again."
        return {"failure": failure, "step": _relink(conn, folder, step, account, failure)}
    except OSError as error:
        # The connector or its sign-in server could not be reached; the code may still work, so the link stays.
        log.error("connector sign-in for step %s could not reach the server: %s", step_id, error)
        failure = f"Cockpit could not reach {step['signin']['host']}. Paste the address again in a moment."
        with db.transaction(conn):
            _set_signin(conn, step_id, step["signin"] | {"failure": failure})
        return {"failure": failure, "step": store.get_step(conn, step_id)}
    with db.transaction(conn):
        message = store.hand_off(
            conn, step_id, "cockpit", _finished_text(step, account, state), {}, checkouts_dir, "hand_off"
        )
        _set_signin(conn, step_id, step["signin"] | {"failure": None, "finished_at": db.now_iso(), "tools": state["tools"]})
    drop_pending(folder, step_id)
    return {"message": message, "step": store.get_step(conn, step_id)}
