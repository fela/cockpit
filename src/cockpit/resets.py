"""Cockpit's own reading of each Claude account's usage endpoint: its banked resets, for the usage display's
hover (SPEC UI-37), and its 5-hour and 7-day windows (SPEC UI-12).

Claude grants some accounts a usage-limit reset now and then; when a session reaches a limit, Claude Code's
limit dialog offers to spend one ("to reset your session limit now"). Claude Code reads them from its usage
endpoint, under the `cedar_ember` key:

    {"eligible": true, "grants": [{"label", "resets_total", "resets_left", "ends_at", "usable_now",
     "use_requires_limit", "paused", ...}], "cooldown_until": null, ...}

Cockpit asks the same endpoint with the access token the account's folder holds, as Claude Code does, and
sends the headers Claude Code sends, with the installed release in the User-Agent (headers): without them the
endpoint answers `"eligible": false, "ineligible_reason": "surface"`, which Cockpit logs as an error. Cockpit leaves token renewal to Claude Code; Claude Code does whenever a session runs on the account. While it is expired the
account keeps its last answer. Answers are kept by the address the folder is signed in with, so a folder
signed in with another address starts with none. Only the token is read from the sign-in file, and it is
neither logged nor shown.

The same answer holds the account's windows (`"five_hour": {"utilization", "resets_at"}`, and `seven_day`).
The status line saves them only while a session runs on the account, so without this reading an account no
session runs on would keep its last session's numbers (usage.latest).
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from cockpit.config import CLAUDE_COMMAND, Account

log = logging.getLogger(__name__)

URL = "https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1"
EVERY_SECONDS = 15 * 60
TIMEOUT_SECONDS = 20

_lock = threading.Lock()
# Signed-in address to its last banked resets: {"read_at", "grants", "cooldown_until"}.
_known: dict[str, dict[str, Any]] = {}
# Signed-in address to its last windows, in the status line's shape (usage.py):
# {"rate_limits": {"five_hour": {"used_percentage", "resets_at"}, "seven_day": {...}}, "captured_at": <unix seconds>}.
_readings: dict[str, dict[str, Any]] = {}


def known(address: str) -> dict[str, Any] | None:
    """The last answer for the account signed in with `address`, or None before the first one."""
    with _lock:
        return _known.get(address)


def reading(address: str) -> dict[str, Any] | None:
    """The windows last read for the account signed in with `address`, or None before the first reading."""
    with _lock:
        return _readings.get(address)


def expiring(address: str, now: datetime) -> float | None:
    """Unix seconds when the soonest-ending usage-limit reset of the account signed in with `address` expires,
    of those it holds that clear its week and are not paused; None when it holds none with an end. A cooldown
    does not count: it ends before the reset does, and the reset is lost only at its end."""
    answer = known(address)
    if answer is None:
        return None
    ends = [
        datetime.fromisoformat(grant["ends_at"]).timestamp() for grant in answer["grants"]
        if grant["left"] and not grant["paused"] and "seven_day" in grant["clears"] and grant["ends_at"] is not None
        and datetime.fromisoformat(grant["ends_at"]) > now
    ]
    return min(ends) if ends else None


def headers() -> dict[str, str]:
    """The headers Claude Code sends to the usage endpoint, its User-Agent naming the installed release."""
    return {"anthropic-beta": "oauth-2025-04-20", "x-app": "cli", "anthropic-client-platform": "cli",
            "User-Agent": f"claude-cli/{CLAUDE_COMMAND.resolve(strict=True).name} (external, cli)"}


def spendable(address: str, now: datetime) -> list[frozenset[str]]:
    """The usage-limit resets the account signed in with `address` can spend at `now`, each as the windows it
    clears: one entry per reset left of a grant that is not paused or expired, none while the account's resets are
    on cooldown. Whether one gets its account past a limit is usage.room's to say."""
    answer = known(address)
    return [] if answer is None else _spendable_in(answer, now)


def _spendable_in(answer: dict[str, Any], now: datetime) -> list[frozenset[str]]:
    cooldown = answer["cooldown_until"]
    if cooldown is not None and datetime.fromisoformat(cooldown) > now:
        return []
    return [
        frozenset(grant["clears"]) for grant in answer["grants"]
        if not grant["paused"] and (grant["ends_at"] is None or datetime.fromisoformat(grant["ends_at"]) > now)
        for _ in range(grant["left"])
    ]


def weeks_banked(answer: dict[str, Any] | None, now: datetime) -> int:
    """How many more full weeks of use an answer's resets hold: those that clear the week and can be spent at `now`.
    `answer` is in the shape `known` gives, from Claude or from Codex (codex.parse_resets); None counts as none."""
    return 0 if answer is None else sum("seven_day" in clears for clears in _spendable_in(answer, now))


def refresh_forever(signed_in: Callable[[], list[tuple[Account, str]]]) -> None:
    """Ask for every signed-in account's resets and windows every EVERY_SECONDS; `signed_in` gives each account's
    folder with the address it is signed in with."""
    while True:
        try:
            listed = signed_in()
        except Exception:
            log.exception("listing the signed-in Claude accounts for their usage failed")
            listed = []
        for account, address in listed:
            now = datetime.now(UTC)
            try:
                body = fetch(account, now)
            except Exception:
                log.exception("reading the %s Claude account's usage failed", account.name)
                continue
            if body is None:
                continue
            banked, windows_now = parse(body.get("cedar_ember"), now), windows(body, now)
            with _lock:
                if banked is not None:
                    _known[address] = banked
                if windows_now is not None:
                    _readings[address] = windows_now
        time.sleep(EVERY_SECONDS)


def fetch(account: Account, now: datetime) -> dict[str, Any] | None:
    """The account's answer from the usage endpoint now, or None, logged, while its access token has expired or
    the endpoint fails."""
    try:
        try:
            credentials = (account.folder / ".credentials.json").read_text()
        except FileNotFoundError:
            # Claude Code keeps the default folder's login in the macOS Keychain under this name; another folder's
            # login sits under a name of its own, so it stays unknown here.
            if sys.platform != "darwin" or not account.default:
                raise
            credentials = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True, timeout=3,
            ).stdout
        oauth = json.loads(credentials)["claudeAiOauth"]
        token, expires = oauth["accessToken"], oauth["expiresAt"]
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        # Only the error type is logged because file and command errors can contain tokens.
        log.error("reading the %s Claude account's access token failed: %s", account.name, type(exc).__name__)
        return None
    if expires / 1000 <= now.timestamp():
        log.warning("the %s Claude account's banked resets and windows stay as last read: its access token has expired, and"
                    " Claude Code renews it once a session runs on the account", account.name)
        return None
    try:
        request = urllib.request.Request(URL, headers=headers() | {"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = json.load(response)
    except (OSError, ValueError) as exc:
        detail = f"HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
        log.error("reading the %s Claude account's usage failed: %s", account.name, detail)
        return None
    if not isinstance(body, dict):
        log.error("the %s Claude account's usage is a %s, not an object", account.name, type(body).__name__)
        return None
    return body


def parse(block: Any, now: datetime) -> dict[str, Any] | None:
    """The page's shape of the endpoint's `cedar_ember` block; None, logged, for a shape Cockpit cannot read."""
    try:
        grants = [
            {
                "label": grant["label"],
                "left": int(grant["resets_left"]),
                "total": int(grant["resets_total"]),
                "ends_at": grant.get("ends_at"),
                "usable_now": bool(grant.get("usable_now")),
                # Some offers can only be used once the account is at a limit.
                "needs_limit": bool(grant.get("use_requires_limit")),
                "paused": bool(grant.get("paused")),
                # The windows a reset clears: "five_hour", "seven_day", "seven_day_overage_included".
                "clears": [str(window) for window in grant.get("clears") or []],
            }
            for grant in ((block.get("grants") or []) if block.get("eligible") else [])
        ]
        if block.get("ineligible_reason") == "surface":
            log.error("Claude's usage endpoint did not take Cockpit's request for Claude Code's (ineligible_reason"
                      " surface), so it lists no banked resets: compare resets.headers with what Claude Code sends")
        return {"read_at": now.isoformat(), "grants": grants, "cooldown_until": block.get("cooldown_until")}
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        log.error("Claude's banked resets have an unexpected shape: %r", exc)
        return None


def windows(body: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """The answer's 5-hour and 7-day windows in the status line's shape, read at `now`. A window the answer gives
    no use or reset time for is left out, as the status line leaves out a window that has reset. None, logged, for
    a shape Cockpit cannot read."""
    try:
        rate_limits = {}
        for key in ("five_hour", "seven_day"):
            window = body.get(key)
            if window is None or window.get("utilization") is None or window.get("resets_at") is None:
                continue
            resets_at = datetime.fromisoformat(window["resets_at"]).timestamp()
            rate_limits[key] = {"used_percentage": float(window["utilization"]), "resets_at": resets_at}
        return {"rate_limits": rate_limits, "captured_at": now.timestamp()}
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        log.error("Claude's usage windows have an unexpected shape: %r", exc)
        return None
