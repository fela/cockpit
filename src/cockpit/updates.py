"""Warn when an enabled provider falls behind, leaving installation to the user (SPEC SESSION-15)."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cockpit import db, patterns

log = logging.getLogger(__name__)

CHECK_SECONDS = 24 * 60 * 60
PACKAGE = "@openai/codex"
# The native installer's link to the release it runs, ~/.local/share/claude/versions/<version>.
CLAUDE_BIN = Path.home() / ".local" / "bin" / "claude"
# Under it, each channel names the version Claude Code's own updater installs.
CLAUDE_CHANNEL_URL = "https://downloads.claude.ai/claude-code-releases"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
GRACE = timedelta(days=1)
PACKAGE_JSON = Path.home() / ".local" / "lib" / "node_modules" / "@openai" / "codex" / "package.json"
MIN_AGE = timedelta(days=3)
NPM_TIMEOUT_SECONDS = 600
# A release, not a prerelease such as 0.164.0-alpha.1.
RELEASE = re.compile(r"^\d+\.\d+\.\d+$")

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def run_npm(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=NPM_TIMEOUT_SECONDS, check=False)


def newest_ready(published: dict[str, str], now: datetime) -> str | None:
    """The newest release (by publish time) out for MIN_AGE; `published` is `npm view <package> time`."""
    ready = [
        (datetime.fromisoformat(when), version) for version, when in published.items()
        if RELEASE.match(version) and datetime.fromisoformat(when) <= now - MIN_AGE
    ]
    return max(ready)[1] if ready else None


def installed_version(package_json: Path) -> str:
    return json.loads(package_json.read_text(encoding="utf-8"))["version"]


def _npm(run: Runner, args: list[str]) -> str:
    result = run(["npm", *args])
    if result.returncode != 0:
        raise RuntimeError(f"`npm {' '.join(args)}` exited {result.returncode}: {result.stderr.strip()[-500:]}")
    return result.stdout


def fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read().decode().strip()


def claude_channel(settings: Path) -> str:
    """The release channel Claude Code's updater follows: `autoUpdatesChannel` in its settings, else latest."""
    if not settings.is_file():
        return "latest"
    return json.loads(settings.read_text(encoding="utf-8")).get("autoUpdatesChannel", "latest")


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def claude_behind(fetch: Callable[[str], str], claude_bin: Path, settings: Path) -> tuple[str, str] | None:
    """(installed version, sentence) when Claude Code's release channel serves a newer release than the one installed; None when
    it is current or was not installed with the native installer, whose updater this checks."""
    if not claude_bin.is_symlink():
        return None
    target = claude_bin.resolve()
    if target.parent.name != "versions" or target.parent.parent.name != "claude":
        return None
    if not target.is_file():
        raise RuntimeError(f"{claude_bin} links to {target}, which is missing")
    installed = target.name
    channel = claude_channel(settings)
    newest = fetch(f"{CLAUDE_CHANNEL_URL}/{channel}")
    if not RELEASE.match(newest) or not RELEASE.match(installed):
        raise RuntimeError(f"cannot compare the installed {installed[:80]!r} with the {channel} channel's {newest[:80]!r}")
    if version_key(installed) >= version_key(newest):
        return None
    return (
        installed,
        (f"Claude Code on this machine is {installed}, but its {channel} channel serves {newest} and its own updater"
         f" has not installed it for a day. If `claude --version` still says {installed}, run `claude update`."),
    )


def codex_behind(run: Runner, package_json: Path, now: datetime) -> tuple[str, str] | None:
    """(installed version, sentence) when Codex's CLI is older than the newest release out for MIN_AGE;
    None when it is current or was not installed with npm here."""
    if not package_json.is_file():
        return None
    published = json.loads(_npm(run, ["view", PACKAGE, "time", "--json"]))
    target = newest_ready(published, now)
    installed = installed_version(package_json)
    if target is None or installed not in published:
        raise RuntimeError(f"npm lists no release of {PACKAGE} out for {MIN_AGE.days} days, or not the installed {installed}")
    if installed == target or published[installed] >= published[target]:
        return None
    return (
        installed,
        (f"Codex's CLI on this machine is {installed}, but {target} has been out since {published[target][:10]}"
         f". If `codex --version` still says {installed},"
         f" run `npm install --global {PACKAGE}@{target}`."),
    )


def behind(
    run: Runner, fetch: Callable[[str], str], claude_bin: Path, settings: Path, package_json: Path, now: datetime,
    providers: tuple[str, ...],
) -> list[tuple[str, str | None, str]]:
    """(tool, installed version, sentence) for each tool that is behind or could not be checked, so a check that
    fails is seen too; the version is None when the check failed."""
    problems = []
    for key, tool, check, by_hand in (
        ("claude", "Claude Code", lambda: claude_behind(fetch, claude_bin, settings),
         "`claude update` installs a newer release if its channel has one"),
        ("codex", "Codex's CLI", lambda: codex_behind(run, package_json, now),
         f"compare `codex --version` with `npm view {PACKAGE} version`"),
    ):
        if key not in providers:
            continue
        try:
            found = check()
            problem = None if found is None else (key, *found)
        except Exception as exc:
            log.exception("checking %s's version failed", tool)
            problem = (key, None, (f"Cockpit could not check whether {tool} is up to date for a day ({exc})."
                                        f" Check by hand: {by_hand}."))
        if problem is not None:
            problems.append(problem)
    return problems


def record_behind(conn: sqlite3.Connection, problems: list[tuple[str, str | None, str]], now: datetime) -> None:
    """Record `tools.behind`, which wakes the sweeper (sweeper.unseen_changes), for each tool the checks have found
    behind or unable to check for GRACE on the same installed version: an updater's normal lag, a release the channel
    just started serving or a passing network failure clears by the next day's check, and a tool whose version
    changed was updated, so its day starts again. A failed check says nothing of the version and keeps the day going.
    A problem that lasts is recorded again once a day, since a wake-up lists only the latest changes; the checks at
    each restart in between repeat nothing."""
    noted = db.kv_get(conn, "tools_behind") or {}
    current: dict[str, dict[str, Any]] = {}
    with db.transaction(conn):
        for tool, installed, problem in problems:
            seen: dict[str, Any] | None = noted.get(tool)
            if seen is None or (installed is not None and seen["installed"] not in (None, installed)):
                seen = {"since": now.isoformat(), "recorded": None, "installed": installed}
            seen["installed"] = installed or seen["installed"]
            recorded = seen["recorded"]
            lasted = datetime.fromisoformat(seen["since"]) <= now - GRACE
            if lasted and (recorded is None or datetime.fromisoformat(recorded) <= now - GRACE):
                patterns.record(conn, "tools.behind", "cockpit", None, None, None, {"problem": problem})
                seen["recorded"] = now.isoformat()
            current[tool] = seen
        db.kv_set(conn, "tools_behind", current)
