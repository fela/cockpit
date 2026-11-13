"""The user's time zone (SPEC CONTROL-32): one setting, kept in setup.toml, that the page can change.

Cockpit uses the zone for the morning briefing's hour, scheduled prompts, the sweeper's clock and the days of the
time report. Each open page reports its browser's zone. A browser takes its zone from the device's settings, so a
VPN leaves it alone; a trip changes it. Cockpit asks to switch only once a browser has shown a zone with a different
clock for ASK_AFTER: a layover or a day across a border passes without a question. A zone the user chose to keep is
not asked about again while it keeps being reported; once it has gone unreported for FORGET_AFTER, its next report
starts a new count, so the next trip there asks again.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, available_timezones

from . import config, db, parts, patterns, store

ASK_AFTER = timedelta(hours=6)
FORGET_AFTER = timedelta(days=2)
# The kv key holding each reported zone whose clock differs from Cockpit's: {zone: {first, last, kept}}.
SEEN_KEY = "browser_zones"
TIMEZONE_LINE = re.compile(r'^timezone\s*=\s*"[^"\n]*"\s*$', re.MULTILINE)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def zone(name: str) -> ZoneInfo:
    """The zone called `name`, refused unless it is an IANA zone name."""
    if name not in available_timezones():
        raise store.Invalid(f"{name!r} is not a time zone name such as Asia/Jakarta")
    return ZoneInfo(name)


def same_clock(a: ZoneInfo, b: ZoneInfo, now: datetime) -> bool:
    """Whether two zones show the same time now and on the first of each month for a year, so through any
    daylight-saving change: Asia/Jakarta and Asia/Ho_Chi_Minh do; Europe/London and Africa/Abidjan do not."""
    moments = [now] + [datetime(now.year + (now.month + step - 1) // 12, (now.month + step - 1) % 12 + 1, 1, 12, tzinfo=UTC)
                       for step in range(1, 13)]
    return all(moment.astimezone(a).utcoffset() == moment.astimezone(b).utcoffset() for moment in moments)


def unreliable(name: str) -> bool:
    """A browser that hides its zone, as private modes and fingerprint blockers do, reports UTC; devices in a
    place report the place's zone. So a reported UTC or Etc/ zone never starts a question."""
    return name == "UTC" or name.startswith("Etc/")


def state(conn: sqlite3.Connection, browser: str | None, now: datetime) -> dict[str, Any]:
    """Cockpit's zone, and the question to put to a browser in zone `browser`, if any: {zone, since}."""
    ask = None
    if browser is not None:
        seen = (db.kv_get(conn, SEEN_KEY) or {}).get(browser)
        if seen and not seen["kept"] and now - datetime.fromisoformat(seen["first"]) >= ASK_AFTER:
            ask = {"zone": browser, "since": seen["first"]}
    return {"zone": config.setup().timezone.key, "ask": ask}


def report(conn: sqlite3.Connection, browser: str, now: datetime) -> dict[str, Any]:
    """Note that an open page's browser is in zone `browser`, and answer as `state` does."""
    reported = zone(browser)
    with db.transaction(conn):
        seen = {name: entry for name, entry in (db.kv_get(conn, SEEN_KEY) or {}).items()
                if now - datetime.fromisoformat(entry["last"]) < FORGET_AFTER}
        if not unreliable(browser) and not same_clock(reported, config.setup().timezone, now):
            entry = seen.get(browser) or {"first": iso(now), "kept": False}
            seen[browser] = {**entry, "last": iso(now)}
        db.kv_set(conn, SEEN_KEY, seen)
    return state(conn, browser if browser in seen else None, now)


def keep(conn: sqlite3.Connection, browser: str, now: datetime) -> dict[str, Any]:
    """The user keeps Cockpit's zone while their browser shows `browser`: no question about it on this trip."""
    zone(browser)
    with db.transaction(conn):
        seen = db.kv_get(conn, SEEN_KEY) or {}
        if browser in seen:
            seen[browser] = {**seen[browser], "kept": True}
            db.kv_set(conn, SEEN_KEY, seen)
        patterns.record(conn, "timezone.kept", "user", None, None, None, {"zone": config.setup().timezone.key, "browser": browser})
    return state(conn, browser, now)


def put(conn: sqlite3.Connection, path: Path, name: str, now: datetime) -> dict[str, Any]:
    """Set the user's zone to `name` in the setup file at `path`, for this process from now on."""
    chosen = zone(name)
    before = config.setup().timezone.key
    text = path.read_text()
    if len(TIMEZONE_LINE.findall(text)) != 1:
        raise RuntimeError(f"{path} has no single timezone line to change")
    written = TIMEZONE_LINE.sub(f'timezone = "{chosen.key}"', text)
    # Written whole beside the file, then moved over it, so a reader never sees half a setup.
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".setup-", suffix=".toml")
    try:
        with os.fdopen(handle, "w") as out:
            out.write(written)
        os.chmod(temporary, path.stat().st_mode & 0o777)
        config.read_setup(Path(temporary), parts.setup_tables())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    config.setup.cache_clear()
    with db.transaction(conn):
        # A new zone starts every count again: what differed from the old zone may match the new one.
        db.kv_set(conn, SEEN_KEY, {})
        patterns.record(conn, "timezone.set", "user", None, None, None, {"from": before, "to": chosen.key})
    return state(conn, None, now)
