"""What the user's Mac records each minute (SPEC TRACK-9, TRACK-10, TRACK-11).

A small program on the Mac (activity_install.sh) sends, once a minute, the app in front, the site's host when a
browser is in front, and whether the keyboard or mouse went untouched for the minute (away). It sends with the
Mac's own key, which can only send these records: Cockpit keeps the key's SHA-256 in `kv`, never the key. The
installer gets the key by trading the one-time code in the line the user pastes, so no key is ever shown.

Cockpit keeps each minute for `KEEP_DAYS`, then folds it into a daily total per app and site that stays for good.
Each app and site has a category: work, neutral or distraction, from the user's choices on the page, else the
defaults below, else unsorted. A site matches its own host first, then the host without its leading labels, so
gist.github.com takes github.com's category. A minute counts for a task when its middle falls inside one of the
task's timer spans.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from cockpit import db
from cockpit.timesheet import MINUTE_FORMAT, Interval, _merged, _time

KEEP_DAYS = 30
# How far ahead of Cockpit's clock a minute may be: the Mac's clock can run a little fast.
AHEAD = timedelta(minutes=5)
CATEGORIES = ("work", "neutral", "distraction")
UNSORTED = "unsorted"
# How recent the last minute must be to count as what is in front now.
NOW_WINDOW = timedelta(minutes=3)
# Of the last DRIFT_OF minutes, this many on distraction make drift.
DRIFT_AT, DRIFT_OF = 3, 5
KEY_SHA = "activity_key_sha256"
# The newest one-time code's SHA-256 and when it stops working, until the installer trades it for a key.
CODE = "activity_code"
CODE_LASTS = timedelta(minutes=15)
PAUSED = "activity_paused"
# Each pause as [from, to) in UTC minutes, `to` null while it lasts: the Mac may send a pause's minutes late.
PAUSES = "activity_pauses"
LAST_SEEN = "activity_last_seen"
STAMP = "%Y-%m-%dT%H:%M:%SZ"
HALF_MINUTE = timedelta(seconds=30)

DEFAULT_CATEGORIES: dict[str, str] = {
    **dict.fromkeys(
        ("Cursor", "Terminal", "iTerm2", "Code", "Visual Studio Code", "Xcode", "Ghostty", "Warp", "Zed",
         "github.com", "linear.app", "claude.ai", "chatgpt.com", "localhost"),
        "work",
    ),
    **dict.fromkeys(
        ("Slack", "Mail", "Calendar", "Messages", "Finder", "Notes", "System Settings", "Safari", "Google Chrome",
         "Arc", "Brave Browser", "Firefox", "mail.google.com", "calendar.google.com"),
        "neutral",
    ),
    **dict.fromkeys(
        ("youtube.com", "x.com", "twitter.com", "reddit.com", "instagram.com", "facebook.com", "tiktok.com",
         "netflix.com", "news.ycombinator.com"),
        "distraction",
    ),
}


def new_code(conn: sqlite3.Connection, now: datetime) -> str:
    """A one-time code for the line the user pastes on the Mac; the installer trades it for the Mac's key. Only the
    newest code works, once, for CODE_LASTS. The key the Mac has now keeps working until a code is traded."""
    code = secrets.token_urlsafe(16)
    db.kv_set(conn, CODE, {"sha256": _sha(code), "until": (now + CODE_LASTS).strftime(STAMP)})
    return code


def claim(conn: sqlite3.Connection, code: str, now: datetime) -> str | None:
    """Trade a one-time code for a new key for the Mac, so the key before stops working; None when the code is not
    the newest, was used, or is past CODE_LASTS."""
    with db.transaction(conn):
        stored = db.kv_get(conn, CODE)
        if stored is None or not hmac.compare_digest(_sha(code), stored["sha256"]):
            return None
        db.kv_set(conn, CODE, None)
        if now.strftime(STAMP) > stored["until"]:
            return None
        key = secrets.token_urlsafe(32)
        db.kv_set(conn, KEY_SHA, _sha(key))
    return key


def disconnect(conn: sqlite3.Connection) -> None:
    """The Mac was uninstalled: its key stops working, and the page offers Connect a Mac again."""
    db.kv_set(conn, KEY_SHA, None)


def _sha(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def key_matches(conn: sqlite3.Connection, header: str | None) -> bool:
    stored = db.kv_get(conn, KEY_SHA)
    if stored is None or header is None or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(_sha(header.removeprefix("Bearer ").strip()), stored)


def set_paused(conn: sqlite3.Connection, paused: bool, now: datetime) -> None:
    """Pause or resume recording. Each pause stays on record, so minutes the Mac took during one, while it could not
    reach Cockpit and learn of it, are skipped when they arrive later. A pause covers every minute it touches: it starts
    at its minute and ends after the minute Resume was pressed in, so a minute that was partly paused is never kept."""
    start = now.replace(second=0, microsecond=0)
    end = start if now == start else start + timedelta(minutes=1)
    with db.transaction(conn):
        pauses = db.kv_get(conn, PAUSES) or []
        lasting = bool(pauses) and pauses[-1][1] is None
        if paused and not lasting:
            pauses.append([start.strftime(MINUTE_FORMAT), None])
        elif not paused and lasting:
            pauses[-1][1] = end.strftime(MINUTE_FORMAT)
        db.kv_set(conn, PAUSES, pauses)
        db.kv_set(conn, PAUSED, paused)


def mac_state(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "connected": db.kv_get(conn, KEY_SHA) is not None,
        "paused": bool(db.kv_get(conn, PAUSED)),
        "last_seen": db.kv_get(conn, LAST_SEEN),
    }


def record(conn: sqlite3.Connection, minutes: list[dict[str, Any]], now: datetime, zone: ZoneInfo) -> dict[str, Any]:
    """Store the Mac's minutes; a minute sent again replaces the one stored. Skipped: all of them while the user has
    paused recording, and any from an earlier pause or past KEEP_DAYS. Refused, with the reason: a minute ahead of
    Cockpit's clock. One bad minute leaves the others stored, so the Mac's backlog is never lost to it."""
    ahead = (now + AHEAD).strftime(MINUTE_FORMAT)
    refused = [f"minute {each['minute']} is ahead of Cockpit's clock ({now.strftime(MINUTE_FORMAT)} UTC)"
               for each in minutes if each["minute"] > ahead]
    timely = [each for each in minutes if each["minute"] <= ahead]
    with db.transaction(conn):
        db.kv_set(conn, LAST_SEEN, now.strftime(STAMP))
        if db.kv_get(conn, PAUSED):
            return {"stored": 0, "skipped": len(timely), "refused": refused, "paused": True}
        oldest = (now - timedelta(days=KEEP_DAYS)).strftime(MINUTE_FORMAT)
        pauses = db.kv_get(conn, PAUSES) or []
        kept = [each for each in timely if each["minute"] >= oldest
                and not any(start <= each["minute"] < (end or ahead) for start, end in pauses)]
        conn.executemany(
            "INSERT OR REPLACE INTO activity_minutes (minute, app, site, away) VALUES (?, ?, ?, ?)",
            [(each["minute"], each["app"], each["site"], int(each["away"])) for each in kept],
        )
        fold(conn, now, zone)
    return {"stored": len(kept), "skipped": len(timely) - len(kept), "refused": refused, "paused": False}


def fold(conn: sqlite3.Connection, now: datetime, zone: ZoneInfo) -> None:
    """Fold minutes older than KEEP_DAYS into daily totals, in the user's days, and forget pauses that ended before
    then. Runs with each send and on its own every hour (server.py), so a paused or removed Mac's minutes go too."""
    cut = (now - timedelta(days=KEEP_DAYS)).strftime(MINUTE_FORMAT)
    totals: Counter[tuple[str, str, str, int]] = Counter()
    for row in conn.execute("SELECT minute, app, site, away FROM activity_minutes WHERE minute < ?", (cut,)):
        day = datetime.strptime(row["minute"], MINUTE_FORMAT).replace(tzinfo=UTC).astimezone(zone).date().isoformat()
        totals[(day, row["app"], row["site"] or "", row["away"])] += 1
    for (day, app, site, away), count in totals.items():
        conn.execute(
            "INSERT INTO activity_days (day, app, site, away, minutes) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (day, app, site, away) DO UPDATE SET minutes = minutes + excluded.minutes",
            (day, app, site, away, count),
        )
    conn.execute("DELETE FROM activity_minutes WHERE minute < ?", (cut,))
    pauses = db.kv_get(conn, PAUSES) or []
    db.kv_set(conn, PAUSES, [[start, end] for start, end in pauses if end is None or end >= cut])


def set_category(conn: sqlite3.Connection, name: str, category: str) -> None:
    if category not in CATEGORIES:
        raise ValueError(f"category is one of {', '.join(CATEGORIES)}, not {category!r}")
    conn.execute(
        "INSERT INTO activity_categories (name, category) VALUES (?, ?)"
        " ON CONFLICT (name) DO UPDATE SET category = excluded.category",
        (name, category),
    )


def _known(conn: sqlite3.Connection, own_host: str) -> dict[str, str]:
    """Every name with a category: the defaults, Cockpit's own host as work, then the user's choices."""
    chosen = {row["name"]: row["category"] for row in conn.execute("SELECT name, category FROM activity_categories")}
    return DEFAULT_CATEGORIES | {own_host: "work"} | chosen


def category(known: dict[str, str], app: str, site: str | None) -> str:
    if not site:
        return known.get(app, UNSORTED)
    host = site
    while True:
        if host in known:
            return known[host]
        if "." not in host:
            return UNSORTED
        host = host.split(".", 1)[1]


def _minutes(conn: sqlite3.Connection, start: datetime, end: datetime) -> list[dict[str, Any]]:
    return [
        {"at": datetime.strptime(row["minute"], MINUTE_FORMAT).replace(tzinfo=UTC), "app": row["app"],
         "site": row["site"], "away": bool(row["away"])}
        for row in conn.execute(
            "SELECT minute, app, site, away FROM activity_minutes WHERE minute >= ? AND minute < ? ORDER BY minute",
            (start.strftime(MINUTE_FORMAT), end.strftime(MINUTE_FORMAT)),
        )
    ]


def _inside(at: datetime, spans: list[Interval]) -> bool:
    middle = at + HALF_MINUTE
    return any(start <= middle < end for start, end in spans)


def _rows(counts: Counter[tuple[str, str | None]], known: dict[str, str]) -> list[dict[str, Any]]:
    """Per app, or per site when a browser showed one: a site seen in two browsers is one row, and its category
    follows from the label alone."""
    per_label: Counter[str] = Counter()
    kinds: dict[str, str] = {}
    for (app, site), count in counts.items():
        per_label[site or app] += count
        kinds[site or app] = category(known, app, site)
    return [{"label": label, "category": kinds[label], "minutes": count} for label, count in per_label.most_common()]


def day_summary(conn: sqlite3.Connection, day: date, now: datetime, zone: ZoneInfo, own_host: str) -> dict[str, Any]:
    """One of the user's days on the Mac: minutes per category, away, per app or site with its category, and the
    minutes outside any task's timer. A day past KEEP_DAYS reads from the daily totals, which hold no timer split."""
    known = _known(conn, own_host)
    counts: Counter[tuple[str, str | None]] = Counter()
    away = 0
    outside: int | None = 0
    start = datetime.combine(day, time(), zone).astimezone(UTC)
    end = min(datetime.combine(day + timedelta(days=1), time(), zone).astimezone(UTC), now)
    minutes = _minutes(conn, start, end)
    if minutes:
        spans = _merged([
            (_time(row["started_at"]), _time(row["ended_at"]) if row["ended_at"] else now)
            for row in conn.execute(
                "SELECT started_at, ended_at FROM time_spans WHERE started_at < ? AND (ended_at IS NULL OR ended_at > ?)",
                (end.strftime(STAMP), start.strftime(STAMP)),
            )
        ])
        for minute in minutes:
            if minute["away"]:
                away += 1
                continue
            counts[(minute["app"], minute["site"])] += 1
            if not _inside(minute["at"], spans):
                outside += 1
    # The 30-day cut can fall inside the day, leaving its earlier minutes only in the daily totals.
    for row in conn.execute("SELECT app, site, away, minutes FROM activity_days WHERE day = ?", (day.isoformat(),)):
        outside = None
        if row["away"]:
            away += row["minutes"]
        else:
            counts[(row["app"], row["site"] or None)] += row["minutes"]
    rows = _rows(counts, known)
    per_category = dict.fromkeys((*CATEGORIES, UNSORTED), 0)
    for row in rows:
        per_category[row["category"]] += row["minutes"]
    return {
        "day": day.isoformat(),
        "minutes": sum(counts.values()),
        "away": away,
        "categories": per_category,
        "labels": rows,
        "outside_tasks": outside,
        "mac": mac_state(conn),
    }


def item_breakdown(conn: sqlite3.Connection, item_id: int, now: datetime, own_host: str) -> dict[str, Any]:
    """Where the item's timer time went on the Mac, per app or site, over the minutes still kept (SPEC TRACK-10)."""
    spans = _merged([
        (_time(row["started_at"]), _time(row["ended_at"]) if row["ended_at"] else now)
        for row in conn.execute(
            "SELECT t.started_at, t.ended_at FROM time_spans t JOIN steps s ON s.id = t.step_id WHERE s.item_id = ?",
            (item_id,),
        )
    ])
    counts: Counter[tuple[str, str | None]] = Counter()
    away = 0
    if spans:
        for minute in _minutes(conn, spans[0][0] - HALF_MINUTE, spans[-1][1] + HALF_MINUTE):
            if not _inside(minute["at"], spans):
                continue
            if minute["away"]:
                away += 1
            else:
                counts[(minute["app"], minute["site"])] += 1
    return {"labels": _rows(counts, _known(conn, own_host)), "away": away}


def now_summary(conn: sqlite3.Connection, now: datetime, own_host: str) -> dict[str, Any]:
    """What is in front on the Mac now, for a session that asks (SPEC TRACK-9): the latest minute
    if it is recent, its category, the last minutes' categories and whether they drifted to distraction."""
    known = _known(conn, own_host)
    recent = _minutes(conn, now - timedelta(minutes=DRIFT_OF), now + AHEAD)
    kinds = ["away" if minute["away"] else category(known, minute["app"], minute["site"]) for minute in recent]
    latest = recent[-1] if recent and now - recent[-1]["at"] <= NOW_WINDOW else None
    return {
        "current": latest and {
            "minute": latest["at"].strftime(STAMP),
            "app": latest["app"],
            "site": latest["site"],
            "away": latest["away"],
            "category": "away" if latest["away"] else category(known, latest["app"], latest["site"]),
        },
        "last_minutes": dict(Counter(kinds)),
        "drifting": kinds.count("distraction") >= DRIFT_AT,
        "mac": mac_state(conn),
    }
