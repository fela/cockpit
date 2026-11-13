"""Scheduled prompts (SPEC BG-14): a prompt Cockpit starts as a new session in a checkout, daily or weekly at a time
in the user's zone (setup.toml's `timezone`).

A session puts a schedule by its `name` with `cockpit schedule put`. Every CHECK_SECONDS Cockpit looks for a schedule
whose latest time (its slot) is newer than the slot it last ran (`last_slot`). For it, in one transaction, it puts an
item for the run (`note:schedule-<name>-<date>-<HHMM>`, in the schedule's project), queues the prompt as a message that starts a
new session on that item, and records the slot; the message goes out after the transaction commits. The slot recorded
in the same transaction as the message is what keeps a slot from starting twice, also across a restart.

A put of a new schedule, or one whose time changed, records the latest slot already past, so a put never starts a run
at once. After Cockpit was down over a slot, the next check starts that slot's run once, late; older slots it missed
are skipped. Resuming a paused schedule skips the slots that passed while it was paused.

A run that cannot be queued keeps its slot unrecorded, so the next check tries again; its error stays on the schedule
and shows as a warning on the page until a run is queued. A queued run that fails to deliver shows as any failed
message does.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from cockpit import db, projects, store
from cockpit.config import user_name
from cockpit.failures import failure_reason

log = logging.getLogger(__name__)

CHECK_SECONDS = 60
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
EVERY = ("daily", "weekly")
PROMPT_LIMIT = 8000
STAMP = "%Y-%m-%dT%H:%M:%SZ"
# A run's item is titled "<title> <d Mon>"; the longest date this adds, checked against the title limit at put time.
LONGEST_DATE = "30 Sep"


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(STAMP)


def _clock(text: str) -> time:
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", text)
    if match is None:
        raise store.Invalid(f"`at` is a time of day as HH:MM, such as 09:00, not {text!r}")
    return time(int(match[1]), int(match[2]))


def latest_slot(schedule: dict[str, Any], now: datetime, zone: ZoneInfo) -> datetime:
    """The schedule's latest time at or before `now`, in the user's zone."""
    local = now.astimezone(zone)
    day = local.date()
    if schedule["every"] == "weekly":
        day -= timedelta(days=(day.weekday() - schedule["weekday"]) % 7)
    slot = datetime.combine(day, _clock(schedule["at"]), zone)
    if slot.astimezone(UTC) > now.astimezone(UTC):
        slot = datetime.combine(day - timedelta(days=1 if schedule["every"] == "daily" else 7), _clock(schedule["at"]), zone)
    return slot


def next_slot(schedule: dict[str, Any], now: datetime, zone: ZoneInfo) -> datetime:
    """The schedule's first time after `now`, in the user's zone."""
    latest = latest_slot(schedule, now, zone)
    step = timedelta(days=1 if schedule["every"] == "daily" else 7)
    return datetime.combine(latest.date() + step, _clock(schedule["at"]), zone)


def _timing(body: dict[str, Any]) -> tuple[str, int | None, str]:
    every = body.get("every")
    if every not in EVERY:
        raise store.Invalid(f"`every` is daily or weekly, not {every!r}")
    at = str(body.get("at") or "")
    _clock(at)
    day = body.get("day")
    if every == "daily":
        if day is not None:
            raise store.Invalid("a daily schedule runs every day; leave out `day`")
        return every, None, at
    if day not in DAYS:
        raise store.Invalid(f"a weekly schedule names its `day`, one of {', '.join(DAYS)}, not {day!r}")
    return every, DAYS.index(day), at


def _shown(conn: sqlite3.Connection, row: sqlite3.Row, now: datetime, zone: ZoneInfo) -> dict[str, Any]:
    schedule = dict(row)
    schedule["paused"] = bool(schedule["paused"])
    schedule["day"] = None if schedule["weekday"] is None else DAYS[schedule["weekday"]]
    schedule["next_at"] = None if schedule["paused"] else _stamp(next_slot(schedule, now, zone))
    message = None
    if schedule["last_message_id"] is not None:
        message = conn.execute(
            "SELECT state, detail, session_id FROM messages WHERE id = ?", (schedule["last_message_id"],)
        ).fetchone()
    schedule["last_run"] = None if message is None else {
        "message_id": schedule["last_message_id"], "item_id": schedule["last_item_id"], "state": message["state"],
        "failure": failure_reason(message["detail"]) if message["state"] == "failed" else None,
        "session_id": message["session_id"],
    }
    return schedule


def get(conn: sqlite3.Connection, name: str, now: datetime, zone: ZoneInfo) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM schedules WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise store.NotFound(f"no schedule {name}")
    return _shown(conn, row, now, zone)


def listing(conn: sqlite3.Connection, now: datetime, zone: ZoneInfo) -> list[dict[str, Any]]:
    """Every schedule, by name."""
    return [_shown(conn, row, now, zone) for row in conn.execute("SELECT * FROM schedules ORDER BY name")]


def put(
    conn: sqlite3.Connection, body: dict[str, Any], author: str, checkouts_dir: Path, now: datetime, zone: ZoneInfo
) -> dict[str, Any]:
    """Create or change the schedule with this name. A new time skips to the next slot; pausing stays the user's."""
    name = str(body.get("name") or "")
    if NAME.fullmatch(name) is None:
        raise store.Invalid(f"a schedule's `name` is 1 to 40 of a-z, 0-9 and -, starting with a letter or digit, not {name!r}")
    prompt = str(body.get("prompt") or "").strip()
    if not prompt or len(prompt) > PROMPT_LIMIT:
        raise store.Invalid(f"a schedule needs a `prompt` of at most {PROMPT_LIMIT} characters")
    cwd = str(body.get("cwd") or "")
    if cwd not in store.checkouts(conn, checkouts_dir):
        raise store.Invalid(f"`cwd` {cwd!r} is not a checkout under {checkouts_dir}")
    title = str(body.get("title") or "").strip()
    if not title:
        raise store.Invalid("a schedule needs a `title`; each run's item is titled with it and the run's date")
    try:
        store.check_agent_title({"title": f"{title} {LONGEST_DATE}"}, "agent:schedule")
    except store.Invalid as error:
        raise store.Invalid(f"each run's item is titled \"{title} <d Mon>\", which is too long: {error}") from error
    found = projects.resolve(conn, str(body.get("project") or ""))
    if found is None:
        raise projects.unknown(conn, str(body.get("project") or ""))
    every, weekday, at = _timing(body)
    stamp = db.now_iso()
    with db.transaction(conn):
        held = conn.execute("SELECT every, weekday, at FROM schedules WHERE name = ?", (name,)).fetchone()
        timing = {"every": every, "weekday": weekday, "at": at}
        last_slot = _stamp(latest_slot(timing, now, zone))
        if held is None:
            conn.execute(
                "INSERT INTO schedules (name, prompt, cwd, every, weekday, at, title, project, author, created_at,"
                " updated_at, last_slot) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, prompt, cwd, every, weekday, at, title, found, author, stamp, stamp, last_slot),
            )
        else:
            moved = dict(held) != timing
            conn.execute(
                "UPDATE schedules SET prompt = ?, cwd = ?, every = ?, weekday = ?, at = ?, title = ?, project = ?,"
                " author = ?, updated_at = ?, last_slot = CASE WHEN ? THEN ? ELSE last_slot END WHERE name = ?",
                (prompt, cwd, every, weekday, at, title, found, author, stamp, moved, last_slot, name),
            )
    return get(conn, name, now, zone)


def pause(conn: sqlite3.Connection, name: str, paused: bool, now: datetime, zone: ZoneInfo) -> dict[str, Any]:
    """Pause or resume. Resuming skips the slots that passed while paused."""
    schedule = get(conn, name, now, zone)
    with db.transaction(conn):
        if paused:
            conn.execute("UPDATE schedules SET paused = 1 WHERE name = ?", (name,))
        elif schedule["paused"]:
            conn.execute(
                "UPDATE schedules SET paused = 0, last_slot = ? WHERE name = ?",
                (_stamp(latest_slot(schedule, now, zone)), name),
            )
    return get(conn, name, now, zone)


def remove(conn: sqlite3.Connection, name: str) -> None:
    with db.transaction(conn):
        if conn.execute("DELETE FROM schedules WHERE name = ?", (name,)).rowcount == 0:
            raise store.NotFound(f"no schedule {name}")


def _when_words(schedule: dict[str, Any]) -> str:
    if schedule["every"] == "daily":
        return f"daily at {schedule['at']}"
    return f"{DAYS[schedule['weekday']].capitalize()}s at {schedule['at']}"


def start_run(conn: sqlite3.Connection, name: str, checkouts_dir: Path, now: datetime, zone: ZoneInfo) -> int | None:
    """Queue the run of the schedule's latest slot, unless it ran or was paused meanwhile; returns the message's id."""
    with db.transaction(conn):
        row = conn.execute("SELECT * FROM schedules WHERE name = ?", (name,)).fetchone()
        if row is None or row["paused"]:
            return None
        schedule = dict(row)
        slot = latest_slot(schedule, now, zone)
        if _stamp(slot) <= schedule["last_slot"]:
            return None
        # The slot's time is in the key, so a schedule moved to a later time the same day runs on an item of its own.
        item = store.upsert_item(conn, {
            "key": f"note:schedule-{name}-{slot:%Y-%m-%d-%H%M}",
            "title": f"{schedule['title']} {slot.day} {slot:%b}",
            "project": schedule["project"],
            "recurring": True,
            "summary": f"Started by the scheduled prompt {name} for {slot:%a %d %b %H:%M}",
        }, schedule["author"])
        text = (
            f"Scheduled prompt {name}, which runs {_when_words(schedule)} {user_name()}'s time, for"
            f" {slot:%a %d %b %Y %H:%M}. It was set with `cockpit schedule put`.\n\n{schedule['prompt']}"
        )
        message = store.create_message(
            conn, schedule["author"], text, {"item_id": item["id"], "new_cwd": schedule["cwd"]}, checkouts_dir,
        )
        conn.execute(
            "UPDATE schedules SET last_slot = ?, last_message_id = ?, last_item_id = ?, error = NULL WHERE name = ?",
            (_stamp(slot), message["id"], item["id"], name),
        )
    return message["id"]


def due(conn: sqlite3.Connection, now: datetime, zone: ZoneInfo) -> list[str]:
    """The names of the schedules whose latest slot has not run."""
    return [
        row["name"] for row in conn.execute("SELECT * FROM schedules WHERE paused = 0 ORDER BY name")
        if _stamp(latest_slot(dict(row), now, zone)) > row["last_slot"]
    ]


def check_once(conn: sqlite3.Connection, checkouts_dir: Path, now: datetime, zone: ZoneInfo) -> list[int]:
    """Queue every due run; returns the messages to deliver. A run that fails keeps its error on the schedule."""
    sent = []
    for name in due(conn, now, zone):
        try:
            message_id = start_run(conn, name, checkouts_dir, now, zone)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}" if not isinstance(exc, store.Invalid | store.NotFound) else str(exc)
            held = conn.execute("SELECT error FROM schedules WHERE name = ?", (name,)).fetchone()
            if held is not None and held["error"] != error:
                log.error("starting the scheduled prompt %s failed: %s", name, error, exc_info=exc)
                with db.transaction(conn):
                    conn.execute("UPDATE schedules SET error = ? WHERE name = ?", (error, name))
            continue
        if message_id is not None:
            sent.append(message_id)
    return sent


def warnings(conn: sqlite3.Connection) -> list[str]:
    """A warning on the page for each schedule whose run could not start."""
    return [
        f"The scheduled prompt {row['name']} could not start its run: {row['error']}"
        for row in conn.execute("SELECT name, error FROM schedules WHERE error IS NOT NULL AND paused = 0 ORDER BY name")
    ]
