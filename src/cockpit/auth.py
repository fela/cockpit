"""Google sign-in for the browser, bearer token for agents.

The browser signs in with Google (OpenID Connect authorization-code flow with
PKCE) and then holds a signed session cookie. Only one account is admitted:
the setup's email (config.py).
Agents on the host use a bearer token from a file only the host user can
read.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import stat
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

# A personal Google account's address ends in one of these; its ID token has no `hd` claim.
PERSONAL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})
GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
SIGN_IN_PATH = "/auth/google"
CALLBACK_PATH = "/auth/google/callback"
SIGN_IN_COOKIE = "__Host-cockpit_sign_in"
SIGN_IN_MAX_AGE = 10 * 60
SESSION_COOKIE = "__Host-cockpit_session"
SESSION_MAX_AGE = 30 * 24 * 60 * 60
LOCAL_SESSION_COOKIE = "cockpit_local_session"
LOCAL_COOKIE_ATTRIBUTES = "Path=/; HttpOnly; SameSite=Strict"
LOCAL_SIGN_IN_PATH = "/auth/local"
# SameSite=Lax, not Strict: the browser reaches "/" through a redirect from
# Google right after signing in, and a Strict cookie is withheld on that
# navigation. A host under a shared domain that is not a public suffix
# shares its site with other hosts under that domain. The exact Origin
# check on every cookie-authenticated write is the guard.
COOKIE_ATTRIBUTES = "Path=/; Secure; HttpOnly; SameSite=Lax"
_SESSION_PATTERN = re.compile(r"([0-9]{1,12})\.([0-9a-f]{64})")


class SignInError(Exception):
    """Google did not complete the sign-in."""


@dataclass(frozen=True)
class GoogleClient:
    client_id: str
    client_secret: str


def require_private_file(path: Path) -> None:
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError(f"{path}: permissions must be 0600 or stricter")


def read_google_client(path: Path) -> GoogleClient:
    """Read the "Web application" client JSON as the Google Cloud console downloads it."""
    require_private_file(path)
    document = json.loads(path.read_text(encoding="utf-8"))
    web = document.get("web") if isinstance(document, dict) else None
    if not isinstance(web, dict):
        raise ValueError(f'{path}: expected a top-level "web" object')  # noqa: TRY004 - bad file contents
    client_id = web.get("client_id")
    client_secret = web.get("client_secret")
    if not isinstance(client_id, str) or not client_id.strip():
        raise ValueError(f'{path}: "web.client_id" must be a non-empty string')
    if not isinstance(client_secret, str) or len(client_secret.strip()) < 16:
        raise ValueError(f'{path}: "web.client_secret" must be at least 16 characters')
    return GoogleClient(client_id=client_id.strip(), client_secret=client_secret.strip())


def read_or_create_secret(path: Path) -> str:
    """A random secret kept in a mode-0600 file; created on first start.

    Cockpit keeps two: the agents' bearer token, and the key that signs the
    browser's session cookie. The key is separate so that holding the token
    (every agent does) does not let anyone forge the user's cookie. Replacing the
    key file signs every browser out.
    """
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.touch(mode=0o600)
        path.write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
    require_private_file(path)
    secret = path.read_text(encoding="utf-8").strip()
    if len(secret) < 32:
        raise ValueError(f"{path}: secret must be at least 32 characters")
    return secret


def bearer_matches(header: str | None, token: str) -> bool:
    if header is None or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header.removeprefix("Bearer ").strip(), token)


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@dataclass(frozen=True)
class SignInStart:
    redirect_url: str
    cookie_value: str


def hosted_domain(email: str) -> str | None:
    """The Google Workspace domain an account with this address signs in under: the `hd` claim of its ID token.
    None for a personal Google account."""
    domain = email.rsplit("@", 1)[1].lower()
    return None if domain in PERSONAL_DOMAINS else domain


def start_sign_in(client: GoogleClient, origin: str, email: str) -> SignInStart:
    state = secrets.token_urlsafe(24)
    code_verifier = secrets.token_urlsafe(48)
    code_challenge = _base64url(hashlib.sha256(code_verifier.encode()).digest())
    # `hd` and `login_hint` only steer Google's account chooser; the callback checks the signed-in account itself.
    domain = hosted_domain(email)
    query = urlencode(
        {
            "client_id": client.client_id,
            "redirect_uri": origin + CALLBACK_PATH,
            "response_type": "code",
            "scope": "openid email",
            "state": state,
            **({"hd": domain} if domain else {}),
            "login_hint": email,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return SignInStart(f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{query}", f"{state}.{code_verifier}")


def parse_sign_in_cookie(value: str | None) -> tuple[str, str] | None:
    if not value:
        return None
    state, separator, code_verifier = value.partition(".")
    if separator != "." or not state or not code_verifier:
        return None
    return state, code_verifier


def exchange_code(client: GoogleClient, code: str, redirect_uri: str, code_verifier: str) -> dict[str, Any]:
    form = urlencode(
        {
            "code": code,
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        }
    ).encode()
    request = urllib.request.Request(
        GOOGLE_TOKEN_ENDPOINT,
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise SignInError(f"Google's token endpoint answered {exc.code}") from exc
    except (OSError, ValueError) as exc:
        raise SignInError("Google's token endpoint could not be reached") from exc
    if not isinstance(payload, dict):
        raise SignInError("Google's token endpoint answered with something other than an object")
    return payload


def id_token_claims(token_response: dict[str, Any]) -> dict[str, Any]:
    # The ID token comes straight from Google's token endpoint over TLS, so its
    # signature is not checked (Google's OpenID Connect docs allow this); the
    # claims are.
    id_token = token_response.get("id_token")
    if not isinstance(id_token, str) or id_token.count(".") != 2:
        raise SignInError("Google sent no ID token")
    payload = id_token.split(".")[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SignInError("the ID token could not be decoded") from exc
    if not isinstance(claims, dict):
        raise SignInError("the ID token claims are not an object")
    return claims


def admitted(claims: dict[str, Any], client_id: str, now: float, email: str) -> bool:
    """True when the token was issued to Cockpit for the verified account `email`. A Workspace account must
    come from its own Workspace (`hd`), so a personal Google account made with the same address is refused."""
    expires = claims.get("exp")
    signed_in = claims.get("email")
    return (
        claims.get("iss") in GOOGLE_ISSUERS
        and claims.get("aud") == client_id
        and isinstance(expires, int | float)
        and expires > now
        and claims.get("email_verified") is True
        and claims.get("hd") == hosted_domain(email)
        and isinstance(signed_in, str)
        and signed_in.lower() == email.lower()
    )


def session_cookie_value(key: bytes, email: str, expires: int) -> str:
    signature = hmac.new(key, f"cockpit-session\0{email.lower()}\0{expires}".encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{signature}"


def session_cookie_valid(value: str | None, key: bytes, email: str, now: float) -> bool:
    if not value:
        return False
    match = _SESSION_PATTERN.fullmatch(value)
    if match is None:
        return False
    expires = int(match.group(1))
    if expires <= now:
        return False
    return hmac.compare_digest(session_cookie_value(key, email, expires), value)


def local_session_cookie_value(key: bytes, email: str, expires: int) -> str:
    """A signed local session with a fresh nonce on each sign-in."""
    nonce = secrets.token_hex(16)
    return nonce + "." + session_cookie_value(key, f"local:{email}:{nonce}", expires)


def local_session_cookie_valid(value: str | None, key: bytes, email: str, now: float) -> bool:
    if not value or not re.fullmatch(r"[0-9a-f]{32}\.[0-9]{1,12}\.[0-9a-f]{64}", value):
        return False
    nonce, signed = value.split(".", 1)
    return session_cookie_valid(signed, key, f"local:{email}:{nonce}", now)


def local_page_secret(key: bytes, session: str) -> str:
    """The origin-scoped browser credential, derived separately from the cookie signature."""
    return hmac.new(key, b"cockpit-local-page\0" + session.encode(), hashlib.sha256).hexdigest()
