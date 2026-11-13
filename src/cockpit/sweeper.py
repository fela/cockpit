"""Launching sweeps and waking the sweeper session when the board changes.

One long-lived session, named "Cockpit sweep", decides what has priority.
Cockpit wakes it when the board changed after the last sweep started: a step
put, closed, withdrawn or moved, the user's feedback, a change to the user's goals,
item edits, an item the user added, a message that failed, or an agent asking for a
sweep. The sweeper's own writes do not count.
A burst of changes goes out as one wake-up once it has been quiet for a
minute, and a wake-up waits a few minutes after the previous sweep so sweeps
do not run back to back.

A sweep that stopped without finishing gets a wake-up at once, which asks the
sweeper why it stopped and for a full sweep. Cockpit sends that restart once
per stopped sweep and never restarts a restart; the page warns when the
restart did not lead to a finished sweep.

While the user looks at an empty queue, Cockpit sends the same session a quick
sweep for work every FILL_EVERY_MINUTES, with no change needed (SPEC SWEEP-7).

Between sweeps, the same session checks the top of the user's queue: whether the
first one or two steps are still valid against live state. A check is short
(no collecting, no ranking). It runs when the user presses Update, and by itself
while the user has the page open and a top step's last check is older than
CHECK_EVERY_MINUTES. A launch, a sweep or a check, goes to the session the last
launch reached while that session exists, else to a new one; one launch runs
at a time.

While the user is away (SPEC SWEEP-11), sweeps slow down and then stop. The user is
active on input on a page (the page reports it) and on the user's own writes; a
tab left open does not count. After SLOW_AFTER_MINUTES away, wake-ups wait
SLOW_GAP_MINUTES after the last sweep. After PAUSE_AFTER_MINUTES, Cockpit sends
the sweeper one last check, which puts first in the user's queue a step that will
still be right when the user comes back, and then sends no wake-ups or checks. The
page shows a Restart sweeps key. The user writing on Cockpit (a message, a hand-off,
a to-do, a goal, feedback), Update, Full sweep now and Restart start
sweeps again; the restart is a change that wakes the sweeper.

A reorder (SPEC SWEEP-25) is a short sweep that only rescores the user's queue and ready next. It marks no change as seen,
so the wake-up after a restart still lists what changed during the pause, and its scores count all at once when it
finishes, as a sweep's do (`store._swap_places`). `reorder_due` stays until a sweep that started after it finished well.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cockpit import config, db, patterns, rank, sessions, store
from cockpit.config import user_name

log = logging.getLogger(__name__)

# A burst of changes goes out as one wake-up once nothing new happened for this long.
WAKE_QUIET_SECONDS = 60
# The least time from the end of a sweep, or a failed launch, to the next wake-up.
WAKE_GAP_MINUTES = 5
WAKE_POLL_SECONDS = 30
# A reorder the sweeper never finishes is sent this many times, then dropped with an error in the log.
REORDER_TRIES = 3
WAKE_LINES = 30
# A wake-up names a priority message the user dropped by this many of its first characters,
# and quotes this many of an item the user added (`cockpit get` lists the whole text).
DROPPED_QUOTE_CHARS = 60
ADDED_QUOTE_CHARS = 300
# A check covers the first this many steps of the user's queue.
CHECK_TOP = 2
# A top step is checked again once its last check is this old, while the user is looking.
CHECK_EVERY_MINUTES = 30
# The user counts as looking while the page fetched the board this recently.
USER_PRESENT_MINUTES = 5
# A launched check that gives no verdict for this long counts as over, and the session as free; each verdict
# restarts the wait, so a long check that keeps giving them is not launched over.
CHECK_MINUTES = 10
# While the user has the page open and nothing waits for them, a quick sweep looks for work for them this often,
# with no change on the board (SPEC SWEEP-7, SWEEP-16).
FILL_EVERY_MINUTES = 15
# A queue with fewer steps than this gets a quick sweep for work: one step left is a queue about to run dry (SPEC
# SWEEP-7
FILL_BELOW = 2
# The user away: from this long without input or a write from the user, wake-ups wait the slow gap after a sweep;
SLOW_AFTER_MINUTES = 60
SLOW_GAP_MINUTES = 30
# and from this long, sweeps and checks stop until the user writes on Cockpit again or restarts them.
PAUSE_AFTER_MINUTES = 120
# While sweeps are paused Cockpit itself asks a stalled item's session to go on, again after this while it stalls again.
STALL_WAKE_AGAIN = timedelta(hours=2)
# Events that say nothing new about the board: delivery and the dismissal of a
# failed message (and its undo; a failure itself counts, see unseen_changes), sweep bookkeeping (a pause too), an agent rewording a step it
# already put, permissions (tool calls the hooks report, except a call auto mode refused, see unseen_changes; the user answers on the
# page, the rules they change, a temporary sentence expiring, and a session
# withdrawing its own call or proposing a sentence for it), pages on items, and images on items.
# Making a page public or private, or giving it a new link, is not quiet: the sweeper's session reads it, so a page shared behind the user's back
# (a session that can reach the database or the cookie key can do that, SPEC API-5) reaches someone who can ask about it.
# The user's or an agent's. An image added to an item still waiting for the sweeper
# (store.UNTRIAGED) counts all the same: the sweeper decides on that item with
# everything attached to it.
QUIET_KINDS = (
    "message.sent",
    "message.followed_up",
    "session.closed",
    "message.failed",
    "message.dismissed",
    "message.restored",
    "sweep.started",
    "sweep.finished",
    "sweep.paused",
    "step.updated",
    "step.checked",
    "step.command_finished",
    "permission.rule_added",
    "permission.rule_removed",
    "permission.rule_edited",
    "permission.requested",
    "permission.allowed",
    "permission.denied",
    "permission.dismissed",
    "permission.withdrawn",
    "permission.proposed",
    "codex.requested",
    "codex.answered",
    "item.image_added",
    "item.image_removed",
    "item.page_added",
    "item.page_updated",
    "item.page_removed",
)
STEP_WORDS = {
    "created": "put step {id}",
    "done": "marked step {id} done",
    "dismiss": "dismissed step {id}",
    "withdraw": "withdrew step {id}",
    "yield": "gave step {id}'s place to a stronger step",
    "reopen": "reopened step {id}",
    "snooze": "snoozed or woke step {id}",
    "hand_off": "handed step {id} to an agent",
    "ran": "pressed Run on step {id}, which ran its command and handed the step to its session",
    "allowed": "pressed Allow on step {id}, which added its permission and handed the step to its session",
    "return": "returned step {id} to {user}'s queue",
}

SessionExists = Callable[[str], bool]


def _unchecked(conn: sqlite3.Connection, step_ids: list[int], since: str) -> list[int]:
    """The listed steps still in the user's view that no check has passed or closed after `since`. A step
    leaves the view when it closes, is withdrawn or handed off, or its item is archived.

    Times have whole seconds, so a check stamped in the launch's own second came before it.
    """
    unchecked = []
    for step_id in step_ids:
        step = store.get_step(conn, step_id)
        archived = conn.execute("SELECT archived FROM items WHERE id = ?", (step["item_id"],)).fetchone()[0]
        in_view = step["status"] == "open" and not step["withdrawn_at"] and step["handoff_message_id"] is None and not archived
        if in_view and (step["checked_at"] or "") <= since:
            unchecked.append(step_id)
    return unchecked


def _check_silent(conn: sqlite3.Connection, launch_record: dict[str, Any], launched: dict[str, Any], now: datetime) -> bool:
    """Whether a sent check has gone CHECK_MINUTES without a verdict, counting from its delivery. A verdict passes,
    closes or withdraws one of its steps."""
    heard = [launched["sent_at"]]
    for step_id in launch_record["step_ids"]:
        step = store.get_step(conn, step_id)
        heard += [at for at in (step["checked_at"], step["closed_at"], step["withdrawn_at"]) if at and at > launched["created_at"]]
    return now - rank.parse_time(max(heard)) >= timedelta(minutes=CHECK_MINUTES)  # ty: ignore[unsupported-operator]


def checking(conn: sqlite3.Connection, now: datetime) -> dict[str, Any] | None:
    """The check under way: its message and the steps still waiting for a verdict; else None."""
    launch_record = db.kv_get(conn, "sweep_launch")
    if launch_record is None or launch_record["kind"] != "check":
        return None
    launched = store.get_message(conn, launch_record["message_id"])
    if launched["state"] == "failed":
        return None
    if launched["state"] == "sent" and launch_failure(launched):
        return None
    if launched["state"] == "sent" and _check_silent(conn, launch_record, launched, now):
        return None
    waiting = _unchecked(conn, launch_record["step_ids"], launched["created_at"])
    if not waiting:
        return None
    return {"message_id": launched["id"], "by": launched["author"], "since": launched["created_at"], "step_ids": waiting}


def launch_status(conn: sqlite3.Connection, now: datetime) -> dict[str, Any] | None:
    """Where the latest sweep or check launch stands, so the page can say when the one the user started is over
    (SPEC SWEEP-6): its message, kind and steps, and `state`: starting, running, done, failed or timed out, with
    the reason in `detail` for the last two. None before the first launch."""
    launch_record = db.kv_get(conn, "sweep_launch")
    if launch_record is None:
        return None
    launched = store.get_message(conn, launch_record["message_id"])
    status = {"message_id": launched["id"], "kind": launch_record["kind"], "step_ids": launch_record["step_ids"], "detail": ""}
    if launched["state"] == "failed":
        return status | {"state": "failed", "detail": launched["detail"] or "the sweep session could not be reached"}
    if launched["state"] != "sent":
        return status | {"state": "starting"}
    waited = now - rank.parse_time(launched["sent_at"])  # ty: ignore[unsupported-operator]
    if launch_record["kind"] == "check":
        if not _unchecked(conn, launch_record["step_ids"], launched["created_at"]):
            return status | {"state": "done"}
        if failure := launch_failure(launched):
            return status | {"state": "failed", "detail": failure}
        if _check_silent(conn, launch_record, launched, now):
            return status | {"state": "timed out", "detail": f"no step got a verdict for {CHECK_MINUTES} min"}
        return status | {"state": "running"}
    sweep = db.kv_get(conn, "last_sweep")
    if sweep is not None and sweep["started_at"] >= launched["created_at"] and sweep["finished_at"]:
        return status | {"state": "failed", "detail": sweep["error"]} if sweep.get("error") else status | {"state": "done"}
    if failure := launch_failure(launched):
        return status | {"state": "failed", "detail": failure}
    if sweep is None or sweep["started_at"] < launched["created_at"]:
        if waited >= timedelta(minutes=store.CHECK_IN_MINUTES):
            return status | {"state": "timed out", "detail": f"the sweep session did not start within {store.CHECK_IN_MINUTES} min"}
        return status | {"state": "starting"}
    if store.sweep_running(conn, now) is None:
        return status | {"state": "timed out", "detail": "the sweep stopped writing before it finished"}
    return status | {"state": "running"}


def launch_failure(launched: dict[str, Any]) -> str | None:
    """A rejected turn after delivery means the launch failed even though its message arrived."""
    sid = launched["session_id"]
    sent_at = rank.parse_time(launched["sent_at"])
    answer = sessions.last_answer(sid, sent_at) if sid and sent_at is not None else None
    if answer is None or answer[1]:
        return None
    return ("the sweep session stopped on a usage limit" if sessions.limit_stop(sid)
            else "the sweep session stopped with an error")


def busy(conn: sqlite3.Connection, now: datetime) -> str | None:
    """Why no sweep or check should be launched now, or None."""
    running = store.sweep_running(conn, now)
    if running is not None:
        return f"a sweep started at {running['started_at']} by {running['by']} is still running"
    launch_record = db.kv_get(conn, "sweep_launch")
    if launch_record is None:
        return None
    launched = store.get_message(conn, launch_record["message_id"])
    status = launch_status(conn, now)
    if status is not None and status["state"] == "failed":
        return None
    if launched["state"] in ("queued", "sending"):
        return "the sweep session is still starting"
    if launch_record["kind"] == "check":
        check = checking(conn, now)
        if check is None:
            return None
        return f"a check of the top of the queue started at {rank.parse_time(check['since']):%H:%M} UTC"
    last = db.kv_get(conn, "last_sweep")
    checked_in = last is not None and last["started_at"] >= launched["created_at"]
    sent_at = rank.parse_time(launched["sent_at"])
    if launched["state"] == "sent" and not checked_in and now - sent_at < timedelta(minutes=store.CHECK_IN_MINUTES):  # ty: ignore[unsupported-operator]
        return f"a sweep session was sent the sweep at {sent_at:%H:%M} UTC and has not checked in yet"
    return None


def resumable(conn: sqlite3.Connection, exists: SessionExists, latest_model: Callable[[str], str | None]) -> SessionExists:
    """`exists`, narrowed to a session whose latest own reply uses Opus (SPEC SWEEP-28).
    Before its first reply, use the model recorded at launch.
    When a safety check flags a turn, Claude Code moves the session to a fallback model for the rest of its life, and
    resuming it keeps it there; a launch then goes to a new session instead."""

    def usable(session_id: str) -> bool:
        if not exists(session_id):
            return False
        latest = latest_model(session_id) or store.started_models(conn).get(session_id)
        intended = config.MAIN_MODEL
        if latest is None or latest == intended:
            return True
        log.error("sweeper session %s answered on %s instead of %s; the next launch starts a new session",
                  session_id, latest, intended)
        return False

    return usable


def launch(
    conn: sqlite3.Connection,
    author: str,
    text: str,
    cwd: Path,
    exists: SessionExists,
    kind: str,
    step_ids: list[int],
) -> dict[str, Any]:
    """Queue `text` for the sweeper session, or for a new one; Conflict while a sweep or check is under way.

    `kind` is "sweep", "check" or "reorder"; a check names the steps it covers. The text quotes agents' asks, titles and
    reasons, so a line in them that reads like a sender header is marked (store.defused).
    """
    sweeper = _live_sweeper(conn, exists)
    with db.transaction(conn):
        message_id = _queue_launch(conn, author, text, cwd, sweeper, kind, step_ids)
    return store.get_message(conn, message_id)


def _live_sweeper(conn: sqlite3.Connection, exists: SessionExists) -> str | None:
    """The session the last launch reached while it exists; None for a new one. Looked up outside a transaction, as
    `exists` may list sessions."""
    sweeper = store.sweeper_session(conn)
    if sweeper is not None and not exists(sweeper):
        return None
    return sweeper


def _queue_launch(
    conn: sqlite3.Connection,
    author: str,
    text: str,
    cwd: Path,
    sweeper: str | None,
    kind: str,
    step_ids: list[int],
) -> int:
    """`launch`'s writes, inside the caller's transaction; returns the queued message's id."""
    reason = busy(conn, datetime.now(UTC))
    if reason is not None:
        raise store.Conflict(reason)
    text = store.defused(text)
    now = db.now_iso()
    # The sweep prompt goes over the socket at once, also on the user's Update: it is Cockpit's text, not
    # The user's words, and `busy` counts the sweep session as starting until it lands (SPEC MSG-3).
    if sweeper is not None:
        cursor = conn.execute(
            "INSERT INTO messages (author, session_id, text, state, created_at, as_prompt)"
            " VALUES (?, ?, ?, 'queued', ?, 0)",
            (author, sweeper, text, now),
        )
    else:
        cursor = conn.execute(
            "INSERT INTO messages (author, new_cwd, new_name, text, state, created_at, as_prompt)"
            " VALUES (?, ?, ?, ?, 'queued', ?, 0)",
            (author, str(cwd), store.SWEEP_SESSION_NAME, text, now),
        )
    db.kv_set(conn, "sweep_launch", {"message_id": cursor.lastrowid, "kind": kind, "step_ids": step_ids})
    if kind == "sweep":
        # A check's launch replaces `sweep_launch`; wake_due's gap counts sweeps only.
        db.kv_set(conn, "sweep_launched_at", now)
    return cursor.lastrowid  # ty: ignore[invalid-return-type]


def request(conn: sqlite3.Connection, actor: str, reason: str) -> None:
    """An agent, or the user, asks the sweeper to decide again."""
    with db.transaction(conn):
        patterns.record(conn, "sweep.requested", actor, None, None, None, {"reason": reason})


def unseen_changes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Events after the last sweep started that the sweeper did not write itself, oldest first.

    Quiet kinds are left out, except an image added to an item that still waits for the sweeper; a message that
    failed, which the sweeper resends or dismisses (SPEC MSG-5); and a call auto mode refused, which the sweeper gets
    the session past or turns into a step (MSG-5). The user sees neither on their own, so these count whoever wrote
    them: the sweeper's own messages can fail after its sweep ended, and Cockpit's own on an item, such as a Run's
    result. Cockpit's messages on no item are left out: a failed sweep launch waits its gap in wake_due.
    """
    sweep = db.kv_get(conn, "last_sweep")
    marks = ", ".join("?" for _ in QUIET_KINDS)
    untriaged = f"SELECT i.id FROM items i WHERE {store.UNTRIAGED}"
    return db.rows(
        conn.execute(
            f"SELECT * FROM events WHERE id > ? AND ((actor != ? AND (kind NOT IN ({marks})"
            f" OR (kind = 'item.image_added' AND item_id IN ({untriaged}))))"
            " OR (kind = 'message.failed' AND (actor != 'cockpit' OR item_id IS NOT NULL))"
            " OR (kind = 'permission.requested' AND json_extract(data, '$.kind') = 'denied')) ORDER BY id",
            (db.kv_get(conn, "sweep_seen_event"), sweep["by"] if sweep else "", *QUIET_KINDS),
        ).fetchall()
    )


def note_stalls(conn: sqlite3.Connection, items: list[dict[str, Any]]) -> None:
    """Record `item.stalled` once when urgent or high agent work stalls (`stalled_since` on the page's items), so a
    stall wakes the sweeper like a change on the board: a session going quiet writes nothing (SPEC SWEEP-20). A
    fire-and-forget to-do's work counts at any priority, since nobody else watches it (SPEC CONTROL-24). The same
    item stalling again later, after its work picked up, is a new stall."""
    stalls = {
        str(item["id"]): item["stalled_since"]
        for item in items
        if item["stalled_since"] and (item["priority"] in ("urgent", "high") or item["fire_and_forget"])
    }
    noted = db.kv_get(conn, "stalls_noted") or {}
    if stalls == noted:
        return
    with db.transaction(conn):
        for item_id, since in stalls.items():
            if noted.get(item_id) != since:
                patterns.record(conn, "item.stalled", "cockpit", int(item_id), None, None, {"stalled_since": since})
        db.kv_set(conn, "stalls_noted", stalls)


def stall_wake_text(item: dict[str, Any], away_since: str) -> str:
    """What Cockpit tells a stalled item's session while sweeps are paused."""
    name = user_name()
    return (
        f"Item {item['id']} ({item['title']}) stalled: none of its sessions has worked since"
        f" {item['stalled_since'][11:16]} UTC. {name} has been away since {away_since[11:16]} UTC and sweeps are paused,"
        f" so this message is the only thing that picks the work up until {name} is back.\n\n"
        f"Pick the work up again now and carry it as far as you can, making its choices yourself. Where you want {name}'s"
        " view, put a step with your pick and go on with that pick while it can be undone. Work that rests until a known"
        " time gets `until` and `why` on its agent entry in `waiting_on`. Work that only"
        f" {name} can unblock gets a step that says what, and the same in the item's `summary`. Cockpit asks again in"
        f" {STALL_WAKE_AGAIN.seconds // 3600} hours if the work stalls again."
    )


def stall_wakes(conn: sqlite3.Connection, items: list[dict[str, Any]], cwd: Path, now: datetime) -> list[int]:
    """While sweeps are paused, wake the session of each stalled item that is not low priority, at most once in
    STALL_WAKE_AGAIN, since no sweep does it then (SPEC SWEEP-26). Returns the messages to deliver."""
    pause = paused(conn)
    if pause is None:
        return []
    woken = db.kv_get(conn, "stall_wakes") or {}
    woken = {
        item_id: at for item_id, at in woken.items()
        if now - rank.parse_time(at) < STALL_WAKE_AGAIN  # ty: ignore[unsupported-operator]
    }
    sent = []
    for item in items:
        if not item["stalled_since"] or item["priority"] == "low" or str(item["id"]) in woken:
            continue
        # One item's failure, such as a locked database, leaves the other items' messages to be delivered.
        try:
            with db.transaction(conn):
                # `items` is a snapshot: the user back, or a rest put, a finish or a close since then, keeps the session asleep.
                fresh = store.get_item(conn, item["id"])
                if (
                    paused(conn) is None or fresh["archived"] or fresh["stage"] == "done" or fresh["priority"] == "low"
                    or fresh["waiting_on"] != item["waiting_on"] or store.wake_waits_for_place(conn, item["id"])
                ):
                    continue
                if store.target_session(conn, item["id"], None) is None:
                    continue
                message = store.create_message(
                    conn, "cockpit", stall_wake_text(item, pause["away_since"]), {"item_id": item["id"]}, cwd
                )
                woken[str(item["id"])] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                db.kv_set(conn, "stall_wakes", woken)
        except Exception:
            log.exception("waking stalled item %s during the sweep pause failed", item["id"])
            continue
        sent.append(message["id"])
    return sent


@dataclass(frozen=True)
class Wake:
    """A wake-up that is due: the changes the sweeper has not seen, oldest first, and the sweep that stopped without
    finishing, which the wake-up restarts; None when it is no restart."""

    changes: list[dict[str, Any]]
    stopped: dict[str, Any] | None


def wake_due(conn: sqlite3.Connection, now: datetime) -> Wake | None:
    """The wake-up to send the sweeper, when it is time; else None. With no changes, a full sweep that is due
    while the user is on Cockpit wakes it too (SPEC SCOPE-3), at most once in SLOW_GAP_MINUTES, so a sweeper that
    keeps running light sweeps is not woken every few minutes.

    A sweep that stopped without finishing is restarted at once, with or without changes or the user. Cockpit restarts
    it once (`sweep_recovery`, which `store.finish_sweep` clears), so a restart that stops too is not restarted again;
    the page warns about it instead (`store.stopped_sweep_warnings`). After the restart, changes on the board wake
    the sweeper as they do after any sweep."""
    if paused(conn) is not None or busy(conn, now) is not None:
        return None
    last = user_last_active(conn)
    away = last is not None and now - last >= timedelta(minutes=SLOW_AFTER_MINUTES)
    gap = timedelta(minutes=SLOW_GAP_MINUTES if away else WAKE_GAP_MINUTES)
    stopped = _stopped_sweep(conn, now)
    if stopped is not None:
        if _launch_failed_within(conn, now, gap):
            return None
        return Wake(unseen_changes(conn), stopped)
    sweep = db.kv_get(conn, "last_sweep")
    changes = unseen_changes(conn)
    if changes:
        if now - rank.parse_time(changes[-1]["at"]) < timedelta(seconds=WAKE_QUIET_SECONDS):  # ty: ignore[unsupported-operator]
            return None
    elif not (
        user_present(conn, now) and patterns.full_sweep_due(conn, now)
        and not sweep_launched_since(conn, now - timedelta(minutes=SLOW_GAP_MINUTES))
    ):
        return None
    # A reorder read none of the changes, so the wake-up it precedes does not wait out the gap after it.
    if sweep is not None and sweep["finished_at"] and not sweep.get("reorder") and now - rank.parse_time(sweep["finished_at"]) < gap:  # ty: ignore[unsupported-operator]
        return None
    if _launch_failed_within(conn, now, gap):
        return None
    return Wake(changes, None)


def _stopped_sweep(conn: sqlite3.Connection, now: datetime) -> dict[str, Any] | None:
    """The last sweep, when it stopped without finishing and Cockpit has not restarted it yet; else None."""
    sweep = db.kv_get(conn, "last_sweep")
    if sweep is None or sweep["finished_at"] or store.sweep_running(conn, now) is not None:
        return None
    return sweep if db.kv_get(conn, "sweep_recovery") is None else None


def _launch_failed_within(conn: sqlite3.Connection, now: datetime, gap: timedelta) -> bool:
    launch_record = db.kv_get(conn, "sweep_launch")
    if launch_record is None:
        return False
    launched = store.get_message(conn, launch_record["message_id"])
    return launched["state"] == "failed" and now - rank.parse_time(launched["created_at"]) < gap  # ty: ignore[unsupported-operator]


def describe(conn: sqlite3.Connection, event: dict[str, Any]) -> str:
    user = user_name()
    who = {"user": user, "cockpit": "Cockpit"}.get(event["actor"], f"agent {event['actor'].removeprefix('agent:')}")
    at = rank.parse_time(event["at"]).strftime("%H:%M")  # ty: ignore[unresolved-attribute]
    kind, data = event["kind"], event["data"]
    if kind.startswith("step."):
        action = kind.removeprefix("step.")
        words = STEP_WORDS.get(action, f"{action} step {{id}}").format(id=event["step_id"], user=user)
        step = conn.execute(
            "SELECT s.ask, i.title FROM steps s JOIN items i ON i.id = s.item_id WHERE s.id = ?", (event["step_id"],)
        ).fetchone()
        about = f' ("{step["ask"]}", on {step["title"]})' if step else ""
        return f"{at} {who} {words}{about}"
    if kind == "feedback.added":
        target = f" on {data['target']}" if data["target"] else ""
        return f"{at} {user} gave feedback{target} (feedback {data['feedback_id']})"
    if kind == "sweep.requested":
        return f"{at} {who} asked for a sweep: {data['reason']}"
    if kind == "message.failed":
        on = f" on item {event['item_id']}" if event["item_id"] is not None else ""
        return (
            f"{at} message {data['message_id']} from {who}{on} failed: {_clipped(data['detail'], ADDED_QUOTE_CHARS).rstrip('.')}."
            ' Fix the delivery problem and retry it, or dismiss it when the work was completed another way.'
        )
    if kind == "permission.requested":
        # Older refusal events may name no request or session; `cockpit get` lists the open ones.
        which = f" (request {data['request_id']}, session {data['session_id']})" if "request_id" in data else ""
        return (
            f"{at} auto mode refused a {data['tool']} call{which}. Help the session continue within its permissions,"
            " or have it put a step for the user to decide."
        )
    if kind == "sweep.resumed":
        stopped = rank.parse_time(data["paused_at"]).strftime("%H:%M")  # ty: ignore[unresolved-attribute]
        return f"{at} {user} {data['how']}, which started sweeps again (they stopped at {stopped} while {user} was away)"
    if kind == "item.edited":
        item = conn.execute("SELECT title FROM items WHERE id = ?", (event["item_id"],)).fetchone()
        title = f" ({item['title']})" if item else ""
        return f"{at} {user} edited item {event['item_id']}{title}: {', '.join(data['fields'])}"
    if kind == "item.stalled":
        item = conn.execute("SELECT title, priority FROM items WHERE id = ?", (event["item_id"],)).fetchone()
        since = rank.parse_time(data["stalled_since"]).strftime("%H:%M")  # ty: ignore[unresolved-attribute]
        about = f" ({item['title']}, {item['priority']})" if item else ""
        return (
            f"{at} item {event['item_id']}{about} stalled: none of its sessions has worked since {since}."
            ' Ask its session to check the work and prepare a step or record what it waits for.'
        )
    if kind == "tools.behind":
        return (
            f"{at} Cockpit: {data['problem']} Do that yourself. If the tool stays behind or cannot be checked, put a"
            f" step on item note:tool-updates saying what failed and what {user} can do."
        )
    if kind == "item.added":
        item = conn.execute("SELECT key, summary FROM items WHERE id = ?", (event["item_id"],)).fetchone()
        return (
            f'{at} {user} added item {event["item_id"]} ({item["key"]}) with Add: "{_clipped(item["summary"], ADDED_QUOTE_CHARS)}".'
            " Cockpit starts a session for it; rank its steps like any other."
        )
    if kind == "item.image_added":
        return (
            f"{at} {who} added image {data['image_id']} to item {event['item_id']}, which still waits for you"
            f" (cockpit image get {data['image_id']} --out FILE)"
        )
    # A goal that holds (new, reworded, resumed, restored) goes out whole; the others name the goal briefly.
    if kind.startswith("goal."):
        goal = store.get_goal(conn, data["goal_id"])
        quoted = f'goal {goal["id"]} ("{_clipped(goal["text"], DROPPED_QUOTE_CHARS)}")'
        if kind == "goal.added":
            # Goals added before the whole list was written at once (UI-65) carry no place: they went on top.
            where = f"at place {data['place']}" if "place" in data else "at the top"
            return f"{at} {user} added goal {goal['id']} {where} of their goals: {goal['text']}"
        if kind == "goal.edited":
            return f"{at} {user} reworded goal {goal['id']}: {goal['text']}"
        if kind == "goal.moved":
            return f"{at} {user} moved {quoted} from place {data['from']} to place {data['to']} of their goals"
        if kind == "goal.paused":
            return f"{at} {user} paused {quoted}: leave it aside until they resume it"
        if kind == "goal.resumed":
            return f"{at} {user} resumed goal {goal['id']}, which holds again: {goal['text']}"
        if kind == "goal.dropped":
            return f"{at} {user} dropped {quoted}"
        if kind == "goal.restored":
            return f"{at} {user} took back the drop of goal {goal['id']}, which holds again: {goal['text']}"
    return f"{at} {who}: {kind}"


def _clipped(text: str, chars: int) -> str:
    """`text` on one line, cut after its first `chars` characters."""
    line = " ".join(text.split())
    return line[:chars] + ("…" if len(line) > chars else "")


def user_clock(now: datetime) -> str:
    """The time and day where the user is, for the sweeper's prompts: goals tied to hours of the day ("work on weekdays,
    9 to 18") hold or not by this clock, so the sweeper does not convert UTC itself (SPEC RANK-20)."""
    zone = config.setup().timezone
    local = now.astimezone(zone)
    return f"It is {local:%H:%M} on {local:%A %-d %B} for {user_name()} ({zone.key})."


def wake_text(conn: sqlite3.Connection, wake: Wake, now: datetime) -> str:
    """The wake-up message. A restart asks first for why the sweep stopped, then for a full sweep."""
    skill = config.cockpit_skill_folder()
    read = f"{skill}/SKILL.md and {skill}/references/sweep.md, since they change between sweeps,"
    if wake.stopped is not None:
        started = rank.parse_time(wake.stopped["started_at"])
        last = store.sweep_last_active(conn, wake.stopped)
        header = (
            f"Cockpit wake-up: the sweep that started at {started:%H:%M} UTC last wrote at {last:%H:%M} UTC and stopped"
            " without finishing. First find out why it stopped: read the end of that turn in your transcript, the"
            f' kernel log (`journalctl _TRANSPORT=kernel --since "{started:%Y-%m-%d %H:%M} UTC"`) for an'
            " out-of-memory kill around that time, and `~/.claude/daemon.log` for a restart of Claude's background"
            " daemon. Then read"
            f' {read} and run a full sweep (section "Sweep"). Say in the sweep\'s finish summary why the earlier sweep'
            f" stopped. When the cause will repeat or needs {user_name()}, also put a step that states the cause and"
            " what you did about it on a `note:` item in the Cockpit project."
        )
    else:
        if patterns.full_sweep_due(conn, now):
            kind = f"a full sweep (none finished in the last {patterns.FULL_SWEEP_HOURS} hours)"
        else:
            kind = "a light sweep"
        if not wake.changes:
            return (
                f"Cockpit wake-up: the board is quiet, and {user_name()} has Cockpit open. Read {read} and run {kind}"
                f' (section "Sweep"). {user_clock(now)}'
            )
        header = (
            f"Cockpit wake-up: the board changed after the last sweep started. Read {read} and run {kind}"
            ' (section "Sweep"), starting from these changes.'
        )
    header += " " + user_clock(now)
    if not wake.changes:
        return header
    shown = wake.changes[-WAKE_LINES:]
    lines = [header, "", "Changes (UTC, oldest first):"]
    if len(wake.changes) > len(shown):
        lines.append(f"- {len(wake.changes) - len(shown)} earlier changes not listed")
    return "\n".join(lines + [f"- {describe(conn, event)}" for event in shown])


def wake_once(conn: sqlite3.Connection, cwd: Path, exists: SessionExists) -> int | None:
    """Queue a wake-up for the sweeper when one is due; returns the message to deliver. A restart of a stopped sweep
    is recorded in `sweep_recovery` with its launch."""
    now = datetime.now(UTC)
    wake = wake_due(conn, now)
    if wake is None:
        return None
    text = wake_text(conn, wake, now)
    sweeper = _live_sweeper(conn, exists)
    try:
        with db.transaction(conn):
            if wake.stopped is not None:
                # The lookup of the sweeper's session took time: the sweep may have finished or been restarted since.
                still = _stopped_sweep(conn, now)
                if still is None or still["id"] != wake.stopped["id"]:
                    return None
            message_id = _queue_launch(conn, "cockpit", text, cwd, sweeper, "sweep", [])
            if wake.stopped is not None:
                db.kv_set(conn, "sweep_recovery", {"stopped": wake.stopped["id"], "at": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
    except store.Conflict:
        return None  # a sweep started between the check and the launch
    return message_id


def user_input(conn: sqlite3.Connection, now: datetime) -> int | None:
    """Note input from the user on a page (a key, a click or tap, the wheel, the pointer moving); at most once a minute.
    Returns the count of the user's inputs it made (`store.note_input`); None when it noted none."""
    noted = db.kv_get(conn, "user_input")
    if noted is None or now - rank.parse_time(noted) >= timedelta(minutes=1):  # ty: ignore[unsupported-operator]
        return store.note_input(conn, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return None


def user_last_active(conn: sqlite3.Connection) -> datetime | None:
    """When the user last did something on Cockpit: input on a page or a write; None before either."""
    written = conn.execute("SELECT MAX(at) AS at FROM events WHERE actor = 'user'").fetchone()["at"]
    times = [time for time in (db.kv_get(conn, "user_input"), written) if time]
    return rank.parse_time(max(times)) if times else None


def paused(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Since when sweeps are stopped, and since when the user was away then; None while they run."""
    return db.kv_get(conn, "sweeps_paused")


def pause_text(conn: sqlite3.Connection, steps: list[dict[str, Any]], away_since: datetime, now: datetime) -> str:
    lines = [
        (
            f"Cockpit pause: {user_name()} has done nothing on Cockpit since {away_since:%H:%M} UTC, so after this"
            f" message Cockpit sends no wake-ups or checks until {user_name()} writes there again or restarts sweeps."
            f" Read {config.cockpit_skill_folder()}/SKILL.md and {config.cockpit_skill_folder()}/references/sweep.md with a file"
            ' tool and follow the section "Pause": check these steps at'
            f" the top of {user_name()}'s queue, and make the first place hold a quick step, one {user_name()}"
            f" finishes in a minute or two, that will still be right when {user_name()} comes back: starting with a"
            " small step gets them going."
            f" No sweep. {user_clock(now)}"
        ),
        "",
    ]
    return "\n".join(lines + _step_lines(conn, steps))


def pause_once(conn: sqlite3.Connection, cwd: Path, exists: SessionExists) -> int | None:
    """Stop sweeps once the user has been away PAUSE_AFTER_MINUTES, after one last check of the top of
    The user's queue when it has steps; returns that check's message to deliver."""
    now = datetime.now(UTC)
    last = user_last_active(conn)
    if paused(conn) is not None or last is None or now - last < timedelta(minutes=PAUSE_AFTER_MINUTES):
        return None
    if busy(conn, now) is not None:
        return None
    steps = store.queue_top(conn, now, CHECK_TOP)
    message_id = None
    if steps:
        try:
            text = pause_text(conn, steps, last, now)
            message_id = launch(conn, "cockpit", text, cwd, exists, "check", [step["id"] for step in steps])["id"]
        except store.Conflict:
            return None  # a sweep or check started meanwhile
    stamp = {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "away_since": last.strftime("%Y-%m-%dT%H:%M:%SZ")}
    with db.transaction(conn):
        # The user may have come back while the check above launched; their `resume` found nothing to resume.
        if user_last_active(conn) != last:
            return message_id
        db.kv_set(conn, "sweeps_paused", stamp)
        patterns.record(conn, "sweep.paused", "cockpit", None, None, None, stamp | {"message_id": message_id})
    return message_id


def resume(conn: sqlite3.Connection, how: str) -> bool:
    """The user is back: sweeps start again, and the restart wakes the sweeper. `how`: what the user did, as in
    "wrote a message". False when sweeps were running."""
    with db.transaction(conn):
        pause = paused(conn)
        if pause is None:
            return False
        db.kv_set(conn, "sweeps_paused", None)
        patterns.record(conn, "sweep.resumed", "user", None, None, None, {"how": how, "paused_at": pause["at"]})
        reorder_soon(conn, f"sweeps restarted because {user_name()} {how}")
    return True


def reorder_soon(conn: sqlite3.Connection, why: str) -> None:
    """Have the next poll send the sweeper a reorder (`reorder_once`), saying `why`."""
    db.kv_set(conn, "reorder_due", {"at": db.now_iso(), "why": why, "sent": 0})


def reorder_text(why: str, away: bool, now: datetime) -> str:
    name = user_name()
    still_away = f" {name} is still away, so sweeps stay stopped after it." if away else ""
    return (
        f"Cockpit reorder: {why}.{still_away} Read {config.cockpit_skill_folder()}/references/sweep.md with a file tool,"
        f' since it changes between sweeps, and follow the section "Reorder": a short sweep that rescores {name}\'s queue'
        f" and ready next so the order is right now, and does nothing else. It starts with"
        f" `cockpit sweep start --as sweep --reorder`, and its finish summary starts with 'Reorder:'. {user_clock(now)}"
    )


def _send_reorder(conn: sqlite3.Connection, author: str, due: dict[str, Any], cwd: Path, exists: SessionExists) -> dict[str, Any]:
    text = reorder_text(due["why"], paused(conn) is not None, datetime.now(UTC))
    message = launch(conn, author, text, cwd, exists, "reorder", [])
    db.kv_set(conn, "reorder_due", due | {"sent": due["sent"] + 1})
    return message


def _reorder_left(conn: sqlite3.Connection, now: datetime) -> dict[str, Any] | None:
    """The reorder still to send, dropping one with nothing left to do. An empty queue has nothing to order, as a
    ready-next step takes a free place at once. A sweep that started after the reorder fell due and finished well has
    rescored the steps; a fill looks for work and need not rescore. The times have whole seconds, so a sweep in the same
    second does not count."""
    due = db.kv_get(conn, "reorder_due")
    if due is None:
        return None
    sweep = db.kv_get(conn, "last_sweep")
    covered = (
        sweep is not None and bool(sweep["finished_at"]) and not sweep["error"]
        and patterns.sweep_kind(sweep["summary"]) != "fill" and sweep["started_at"] > due["at"]
    )
    if covered or not store.queue_top(conn, now, 1):
        db.kv_set(conn, "reorder_due", None)
        return None
    return due


def reorder_once(conn: sqlite3.Connection, cwd: Path, exists: SessionExists) -> int | None:
    """Queue the reorder that is due; returns the message to deliver. It stays due until a sweep covers it, so a message
    that failed or a sweep that stopped halfway is sent again, up to REORDER_TRIES times. A stopped sweep's restart
    (wake_due) goes first: its full sweep covers the reorder, and a reorder in its place would leave the stop
    unexplained."""
    now = datetime.now(UTC)
    due = _reorder_left(conn, now)
    if due is None or busy(conn, now) is not None or _launch_failed_within(conn, now, timedelta(minutes=WAKE_GAP_MINUTES)):
        return None
    if _stopped_sweep(conn, now) is not None and paused(conn) is None:
        return None
    if due["sent"] >= REORDER_TRIES:
        log.error("dropped a reorder after %d tries: %s", due["sent"], due["why"])
        db.kv_set(conn, "reorder_due", None)
        return None
    try:
        return _send_reorder(conn, "cockpit", due, cwd, exists)["id"]
    except store.Conflict:
        return None  # a sweep or check started meanwhile


def user_seen(conn: sqlite3.Connection, now: datetime) -> None:
    """Note that the user's page fetched the board; written at most once a minute."""
    seen = db.kv_get(conn, "user_seen")
    if seen is None or now - rank.parse_time(seen) >= timedelta(minutes=1):  # ty: ignore[unsupported-operator]
        db.kv_set(conn, "user_seen", now.strftime("%Y-%m-%dT%H:%M:%SZ"))


def user_present(conn: sqlite3.Connection, now: datetime) -> bool:
    """The user's page fetched the board in the last USER_PRESENT_MINUTES."""
    seen = db.kv_get(conn, "user_seen")
    return seen is not None and now - rank.parse_time(seen) <= timedelta(minutes=USER_PRESENT_MINUTES)  # ty: ignore[unsupported-operator]


def sweep_launched_since(conn: sqlite3.Connection, since: datetime) -> bool:
    """A sweep started, or Cockpit launched a sweep, after `since`. Reorders do not count, as they look for no work. Checks do not count: a stale step's check that
    times out is retried every few minutes, which would put off a due full sweep for good."""
    sweep = db.kv_get(conn, "last_sweep")
    if sweep is not None and not sweep.get("reorder") and rank.parse_time(sweep["started_at"]) > since:  # ty: ignore[unsupported-operator]
        return True
    launched_at = db.kv_get(conn, "sweep_launched_at")
    return launched_at is not None and rank.parse_time(launched_at) > since  # ty: ignore[unsupported-operator]


def check_due(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]] | None:
    """The top steps to check by themselves, when the user is looking and one is unchecked or stale; else None."""
    if not user_present(conn, now):
        return None
    if paused(conn) is not None or busy(conn, now) is not None:
        return None
    launch_record = db.kv_get(conn, "sweep_launch")
    if launch_record is not None and launch_record["kind"] == "check":
        launched = store.get_message(conn, launch_record["message_id"])
        if now - rank.parse_time(launched["created_at"]) < timedelta(minutes=WAKE_GAP_MINUTES):  # ty: ignore[unsupported-operator]
            return None
    stale = (now - timedelta(minutes=CHECK_EVERY_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
    due = [step for step in store.queue_top(conn, now, CHECK_TOP) if (step["checked_at"] or "") < stale]
    return due or None


def fill_due(conn: sqlite3.Connection, now: datetime) -> bool:
    """Whether to send a quick sweep for work: the user is looking at a queue with fewer than FILL_BELOW steps, and
    no sweep started or was launched for FILL_EVERY_MINUTES (SPEC SWEEP-7). Checks do not count, as for a full sweep
    (`sweep_launched_since`): a check that gives no verdict is retried every few minutes. Such a queue means nothing
    waits as ready next either, since a ready-next step takes a free place at once. Early looks count as empty
    places: they show only while the user has room no other step wants (SPEC NEXT-42)."""
    if not user_present(conn, now):
        return False
    if paused(conn) is not None or busy(conn, now) is not None:
        return False
    if sum(not step["early"] for step in store.queue_top(conn, now, store.QUEUE_LIMIT)) >= FILL_BELOW:
        return False
    return not sweep_launched_since(conn, now - timedelta(minutes=FILL_EVERY_MINUTES))


def fill_text(full_due: bool, now: datetime) -> str:
    name = user_name()
    if full_due:
        kind = f'a full sweep (section "Sweep"; none finished in the last {patterns.FULL_SWEEP_HOURS} hours)'
    else:
        kind = 'a quick sweep (section "Fill")'
    return (
        f"Cockpit fill: {name}'s queue is about to run dry: it holds at most one step besides early looks, and"
        f" {name} has Cockpit open. Read"
        f" {config.cockpit_skill_folder()}/SKILL.md and {config.cockpit_skill_folder()}/references/sweep.md,"
        f" since they change between sweeps, and run {kind}: find work {name} can do now and get it ready, and"
        f" prepare new work as a step when little else is coming. {user_clock(now)}"
    )


def fill_once(conn: sqlite3.Connection, cwd: Path, exists: SessionExists) -> int | None:
    """Queue a quick sweep for work when the user looks at a queue about to run dry; returns the message to deliver."""
    now = datetime.now(UTC)
    if not fill_due(conn, now):
        return None
    try:
        return launch(conn, "cockpit", fill_text(patterns.full_sweep_due(conn, now), now), cwd, exists, "sweep", [])["id"]
    except store.Conflict:
        return None  # a sweep or check started meanwhile


def check_text(conn: sqlite3.Connection, steps: list[dict[str, Any]], why: str) -> str:
    lines = [
        (
            f"Cockpit check: {why}. Read {config.cockpit_skill_folder()}/SKILL.md and"
            f" {config.cockpit_skill_folder()}/references/sweep.md, since they change between sweeps,"
            f" and check whether these steps at the top of {user_name()}'s queue are still valid (section"
            ' "Check"). Check only these; no sweep.'
        ),
        "",
    ]
    return "\n".join(lines + _step_lines(conn, steps))


def _step_lines(conn: sqlite3.Connection, steps: list[dict[str, Any]]) -> list[str]:
    lines = []
    for position, step in enumerate(steps, start=1):
        title = conn.execute("SELECT title FROM items WHERE id = ?", (step["item_id"],)).fetchone()["title"]
        lines.append(f'- step {step["id"]} (place {position}): "{step["ask"]}", on {title}')
    return lines


def check_once(conn: sqlite3.Connection, cwd: Path, exists: SessionExists) -> int | None:
    """Queue a check of the top of the user's queue when one is due; returns the message to deliver."""
    steps = check_due(conn, datetime.now(UTC))
    if steps is None:
        return None
    why = f"{user_name()} has Cockpit open, and these were last checked over {CHECK_EVERY_MINUTES} minutes ago or never"
    step_ids = [step["id"] for step in steps]
    try:
        return launch(conn, "cockpit", check_text(conn, steps, why), cwd, exists, "check", step_ids)["id"]
    except store.Conflict:
        return None  # a sweep or check started meanwhile


def update(conn: sqlite3.Connection, cwd: Path, exists: SessionExists, sweep_prompt: str) -> dict[str, Any]:
    """The user pressed Update: check the top of the queue now, or run a full sweep when the queue is empty. A reorder
    that is due goes first, and `check_due` then checks what ended up on top."""
    steps = store.queue_top(conn, datetime.now(UTC), CHECK_TOP)
    if not steps:
        return launch(conn, "user", sweep_prompt, cwd, exists, "sweep", [])
    due = _reorder_left(conn, datetime.now(UTC))
    if due is not None:
        return _send_reorder(conn, "user", due, cwd, exists)
    text = check_text(conn, steps, f"{user_name()} pressed Update")
    return launch(conn, "user", text, cwd, exists, "check", [step["id"] for step in steps])
