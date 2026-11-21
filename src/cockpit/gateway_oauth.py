"""Cockpit-owned OAuth clients. This module never opens any coding agent's credential files."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from cockpit import gateway_claude
from cockpit.gateway_logs import private_upstream
from cockpit.gateway_store import PROVIDERS, GatewayError, Store, canonical

HOSTS = {
    "linear": frozenset({"mcp.linear.app"}),
    "slack": frozenset({"mcp.slack.com", "slack.com"}),
    "notion": frozenset({"mcp.notion.com"}),
}
RESOURCES = dict(PROVIDERS) | {"slack": "https://mcp.slack.com"}
SIGNIN_SECONDS = 10 * 60
log = logging.getLogger(__name__)


def endpoint(provider: str, url: str) -> str:
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in HOSTS[provider] or parts.port not in {None, 443}
            or parts.username or parts.password or parts.fragment):
        raise GatewayError(f"{provider} published an OAuth endpoint outside its allowed hosts")
    return url


def redirect_uri(uri: str) -> str:
    parts = urlsplit(uri)
    if (parts.scheme != "http" or parts.hostname not in {"localhost", "127.0.0.1", "::1"}
            or not parts.port or parts.path != "/callback" or parts.query or parts.fragment
            or parts.username or parts.password):
        raise GatewayError("Use http://localhost:PORT/callback as the redirect URI")
    return uri


def request_json(provider: str, method: str, url: str, payload: dict[str, Any] | None,
                 form: bool) -> dict[str, Any]:
    endpoint(provider, url)
    # Discovery is safe to retry. A code/refresh/registration POST with a lost answer must not be replayed.
    attempts = 3 if method == "GET" else 1
    for attempt in range(attempts):
        try:
            with private_upstream(provider), httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
                response = client.request(method, url, headers={"Accept": "application/json", "User-Agent": "cockpit-connectors"},
                                          data=payload if form else None, json=payload if not form else None)
            document = response.json()
            break
        except (httpx.HTTPError, ValueError):
            if attempt == attempts - 1:
                raise GatewayError(f"{provider} OAuth request failed; sign in again") from None
            time.sleep(0.25 * 2**attempt)
    if not isinstance(document, dict) or response.status_code not in {200, 201}:
        raise GatewayError(f"{provider} OAuth request refused (HTTP {response.status_code})")
    if document.get("ok") is False or document.get("error"):
        raise GatewayError(f"{provider} refused OAuth authorization; check the app configuration and sign in again")
    return document


def discover(provider: str) -> dict[str, Any]:
    root = PROVIDERS[provider].removesuffix("/mcp")
    suffix = "" if provider == "slack" else "/mcp"
    resource = request_json(provider, "GET", root + "/.well-known/oauth-protected-resource" + suffix, None, False)
    if resource.get("resource") != RESOURCES[provider]:
        raise GatewayError(f"{provider} published an unexpected OAuth resource")
    issuers = resource.get("authorization_servers")
    if not isinstance(issuers, list) or not issuers or not isinstance(issuers[0], str):
        raise GatewayError(f"{provider} publishes no OAuth authorization server")
    issuer = endpoint(provider, issuers[0]).rstrip("/")
    parts = urlsplit(issuer)
    if parts.query:
        raise GatewayError("OAuth issuer must have no query")
    auth_url = f"https://{parts.netloc}/.well-known/oauth-authorization-server{parts.path}"
    auth = request_json(provider, "GET", auth_url, None, False)
    if auth.get("issuer", "").rstrip("/") != issuer:
        raise GatewayError(f"{provider} OAuth issuer does not match discovery")
    if "S256" not in auth.get("code_challenge_methods_supported", []):
        raise GatewayError(f"{provider} does not advertise PKCE S256")
    return {
        "issuer": issuer,
        "authorize": endpoint(provider, auth["authorization_endpoint"]),
        "token": endpoint(provider, auth["token_endpoint"]),
        "registration": endpoint(provider, auth["registration_endpoint"]) if auth.get("registration_endpoint") else None,
        "resource": resource["resource"],
    }


def begin(store: Store, provider: str) -> str:
    with store.provider_lock(provider):
        row = store.provider(provider)
        config = row["config"]
        if gateway_claude.shared(row):
            raise gateway_claude.signin_error(provider)
        found = discover(provider)
        client_id, client_secret = config.get("client_id"), config.get("client_secret")
        if not client_id:
            if provider == "slack" or not found["registration"]:
                raise GatewayError(f"Configure a dedicated {provider} OAuth client ID first")
            registered = request_json(provider, "POST", found["registration"], {
                "client_name": "Cockpit connectors", "redirect_uris": [config["redirect_uri"]],
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            }, False)
            client_id, client_secret = registered.get("client_id"), registered.get("client_secret")
            if not isinstance(client_id, str) or not client_id:
                raise GatewayError(f"{provider} registration returned no client ID")
            # Register once and retain the identity even if this authorization attempt is abandoned.
            config = config | {"client_id": client_id, "client_secret": client_secret}
            with store.connection() as conn:
                conn.execute("UPDATE providers SET config=? WHERE name=?", (canonical(config), provider))
        if provider == "slack" and client_secret:
            raise GatewayError("Slack's localhost PKCE app uses a client ID without a client secret")
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        query = {
            "response_type": "code", "client_id": client_id, "redirect_uri": config["redirect_uri"],
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
            "resource": found["resource"],
        }
        if config.get("scope"):
            query["scope"] = config["scope"]
        pending = {"state": state, "verifier": verifier, "started": time.time(), "discovery": found,
                   "generation": row["generation"]}
        store.save_identity(provider, "pending", pending)
        return found["authorize"] + ("&" if "?" in found["authorize"] else "?") + urlencode(query)


def _tokens(provider: str, found: dict[str, Any], form: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    answer = request_json(provider, "POST", found["token"], {key: value for key, value in form.items() if value}, True)
    tokens = answer.get("authed_user", answer) if not answer.get("access_token") else answer
    if not isinstance(tokens, dict) or not isinstance(tokens.get("access_token"), str) or not tokens["access_token"]:
        raise GatewayError(f"{provider} returned no access token")
    kind = tokens.get("token_type", "Bearer")
    if not isinstance(kind, str) or kind.lower() not in ({"bearer", "user"} if provider == "slack" else {"bearer"}):
        raise GatewayError(f"{provider} returned an unsupported token type")
    expires = tokens.get("expires_in")
    if expires is not None:
        try:
            expires = int(expires)
        except (ValueError, TypeError, OverflowError):
            raise GatewayError(f"{provider} returned an invalid token expiry") from None
        if expires <= 0:
            raise GatewayError(f"{provider} returned an expired token")
    grant = {"access_token": tokens["access_token"], "refresh_token": tokens.get("refresh_token")
             or (previous or {}).get("refresh_token"), "discovery": found,
             "expires": time.time() + expires if expires is not None else None}
    return grant


def finish(store: Store, provider: str, address: str) -> None:
    parts = urlsplit(address.strip())
    with store.provider_lock(provider):
        row = store.provider(provider)
        if gateway_claude.shared(row):
            raise gateway_claude.signin_error(provider)
        pending, config = row["pending"], row["config"]
        if not pending or pending["started"] + SIGNIN_SECONDS <= time.time():
            raise GatewayError("This sign-in expired; start a new sign-in")
        expected = urlsplit(config["redirect_uri"])
        if (parts.scheme, parts.netloc, parts.path, parts.fragment) != (expected.scheme, expected.netloc, expected.path, ""):
            raise GatewayError("The callback address does not match this connector's redirect URI")
        parsed = parse_qs(parts.query, keep_blank_values=True)
        if any(len(values) != 1 for values in parsed.values()):
            raise GatewayError("The callback has duplicate parameters")
        query = {key: values[0] for key, values in parsed.items()}
        if not hmac.compare_digest(query.get("state", ""), pending["state"]):
            raise GatewayError("The callback state does not match; use the newest sign-in link")
        if query.get("iss") and query["iss"].rstrip("/") != pending["discovery"]["issuer"]:
            raise GatewayError("The callback issuer does not match")
        if query.get("error") or not query.get("code"):
            store.save_identity(provider, "pending", None)
            raise GatewayError("The connector declined sign-in; start a new sign-in")
        # Consume before the exchange. An unknown network outcome cannot spend the authorization code again.
        store.save_identity(provider, "pending", None)
        found = pending["discovery"]
        grant = _tokens(provider, found, {
            "grant_type": "authorization_code", "code": query["code"], "code_verifier": pending["verifier"],
            "redirect_uri": config["redirect_uri"], "client_id": config["client_id"],
            "client_secret": config.get("client_secret"), "resource": found["resource"],
        }, None)
        with store.connection() as conn:
            conn.execute("UPDATE providers SET grant=?, generation=? WHERE name=?",
                         (canonical(grant), secrets.token_hex(16), provider))
            store.audit(conn, "provider.signed_in", None, provider, None)


def access_token(store: Store, provider: str) -> tuple[str, str]:
    # This process/file lock covers the read, refresh exchange and durable token rotation across all workers.
    with store.provider_lock(provider):
        row = store.provider(provider)
        if gateway_claude.shared(row):
            return gateway_claude.access_token(store, provider, row, require_sharing=True)
        grant = row["grant"]
        if not grant:
            raise GatewayError(f"{provider} is signed out; run cockpit connectors signin {provider}")
        if grant["expires"] is not None and grant["expires"] <= time.time() + 60:
            if not grant.get("refresh_token"):
                raise GatewayError(f"{provider} authorization expired; sign in again")
            config, found = row["config"], grant["discovery"]
            # Commit before the POST: a process death after the server rotates must not replay the old token.
            store.save_identity(provider, "grant", None)
            try:
                renewed = _tokens(provider, found, {
                    "grant_type": "refresh_token", "refresh_token": grant["refresh_token"],
                    "client_id": config["client_id"], "client_secret": config.get("client_secret"),
                    "resource": found["resource"],
                }, grant)
            except Exception as exc:  # noqa: BLE001 - every failed exchange invalidates a possibly rotated refresh token
                # A lost refresh response may have rotated the token. Require a new grant rather than replay it.
                log.error("Connector refresh failed: provider=%s exception=%s", provider, type(exc).__name__)
                raise GatewayError(f"{provider} refresh failed; sign in again") from None
            store.save_identity(provider, "grant", renewed)
            grant = renewed
        return grant["access_token"], row["generation"]
