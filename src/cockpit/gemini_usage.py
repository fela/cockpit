"""Antigravity's usage limits, for Gemini's lane at the foot of the page (SPEC UI-57, UI-73).

Antigravity (`agy`, the "gemini" provider) reports its limits for the signed-in Google account with `agy -p /quota`:
one tab-separated line per limit, such as

    Gemini Models	Weekly Limit Remaining	88%	2026-12-02T19:40:00Z
    Gemini Models	Five Hour Limit Remaining	64%	2026-11-26T11:50:00Z
    Claude and GPT models	Weekly Limit Remaining	97%	2026-12-03T05:15:00Z

Each group of models has a weekly and a five-hour limit. A Gemini session can run on either group's models
(db.session_models), so the lane draws the week of each group with use, the first group's while none has any, and the
hover card lists every group. The command takes several seconds and leaves a log file
in Antigravity's folder, so Quota runs it on a thread of its own, at most every REFRESH_SECONDS and only while the
page reads the usage, and the page gets the latest result.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cockpit import acp_agent, usage

log = logging.getLogger(__name__)

REFRESH_SECONDS = 10 * 60
TIMEOUT_SECONDS = 60
# The limit names Antigravity prints, as usage.WINDOWS keys.
LIMITS = {"Weekly Limit Remaining": "seven_day", "Five Hour Limit Remaining": "five_hour"}


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(text: str) -> list[dict[str, Any]]:
    """The groups in `agy -p /quota`'s output, in its order: {name, limits: {window key: {used_percentage,
    resets_at}}}, `resets_at` in seconds. Raises ValueError on a line it cannot read, or on none."""
    groups: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 4 or fields[1] not in LIMITS or not fields[2].endswith("%"):
            raise ValueError(f"unexpected line {line.strip()[:120]!r}")
        name, limit, remaining, resets = fields
        left = float(remaining[:-1])
        # float() also reads "nan" and "inf", which no share can be.
        if not 0 <= left <= 100:
            raise ValueError(f"unexpected share {remaining!r} in {line.strip()[:120]!r}")
        resets_at = datetime.fromisoformat(resets).timestamp()
        group = groups.setdefault(name, {"name": name, "limits": {}})
        group["limits"][LIMITS[limit]] = {"used_percentage": 100 - left, "resets_at": resets_at}
    if not groups:
        raise ValueError("it printed no limits")
    return list(groups.values())


def run_quota() -> str:
    """`agy -p /quota`'s output, with the environment Cockpit's Antigravity sessions start with. Raises RuntimeError
    when it fails."""
    try:
        done = subprocess.run(
            ["agy", "-p", "/quota"], capture_output=True, text=True, timeout=TIMEOUT_SECONDS, cwd=Path.home(),
            env=acp_agent.agent_env("gemini", dict(os.environ)), check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"agy -p /quota did not run: {error}") from error
    if done.returncode != 0:
        said = (done.stderr.strip() or done.stdout.strip()).splitlines()
        raise RuntimeError(f"agy -p /quota exited {done.returncode}: {said[-1][:200] if said else 'no output'}")
    return done.stdout


class Quota:
    """The latest reading of Antigravity's limits, refreshed on a thread by `run` (run_quota in service)."""

    def __init__(self, run: Callable[[], str]) -> None:
        self._run = run
        self._lock = threading.Lock()
        self._groups: list[dict[str, Any]] = []
        self._captured_at: str | None = None
        self._error = ""
        self._asked_at: float | None = None
        self._thread: threading.Thread | None = None

    def _refresh(self) -> None:
        try:
            groups = parse(self._run())
        except (RuntimeError, ValueError) as error:
            log.error("Antigravity's usage limits could not be read: %s", error)
            with self._lock:
                self._error = str(error)
            return
        with self._lock:
            self._groups, self._captured_at, self._error = groups, _stamp(datetime.now(UTC)), ""

    def read(self, now: datetime) -> dict[str, Any]:
        """{groups, captured_at, error} at `now`: each group {name, week, five_hour}, each window in usage.window's
        shape; `captured_at` is when the groups were read (None before the first reading) and `error` why the latest
        reading failed ("" when it worked). Starts a reading when the latest is REFRESH_SECONDS old."""
        moment = now.timestamp()
        with self._lock:
            if (self._thread is None or not self._thread.is_alive()) and (
                    # A clock set back since the last reading makes it due too.
                    self._asked_at is None or not 0 <= moment - self._asked_at < REFRESH_SECONDS):
                self._asked_at = moment
                self._thread = threading.Thread(target=self._refresh, name="gemini-quota", daemon=True)
                self._thread.start()
            groups, captured_at, error = self._groups, self._captured_at, self._error
        # Antigravity's account has no last_use (config.Setup), so its weeks count to their resets.
        return {
            "groups": [{"name": group["name"],
                        "week": usage.window("seven_day", group["limits"].get("seven_day"), moment, ""),
                        "five_hour": usage.window("five_hour", group["limits"].get("five_hour"), moment, "")}
                       for group in groups],
            "captured_at": captured_at,
            "error": error,
        }
