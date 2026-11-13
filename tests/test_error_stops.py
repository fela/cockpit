"""Sessions whose turn ended on an API error (SPEC SWEEP-30): Cockpit resends once the cause looks gone, reports
what stays broken on one high-priority item, and hands what it fixed to the morning briefing."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cockpit import db, error_stops, sessions, store
from tests.conftest import SESSION_A, SESSION_B, World, make_session

SESSION_C = "33333333-3333-4333-8333-333333333333"
NOW = datetime.now(UTC).replace(microsecond=0)
FABLE = "claude-fable-5-2"
OPUS = "claude-opus-5-5"
# A session's last entry when the API refuses an old Claude Code version.
OLD_CLIENT = (
    "API Error: 400 Claude Code 2.1.280 does not support this model; version 2.1.290 or newer is required. Run"
    " 'claude update', or update the Claude desktop app, then try again."
)


def api_error(at: datetime, text: str = OLD_CLIENT, kind: str = "unknown") -> dict[str, Any]:
    return {
        "type": "assistant", "timestamp": at.strftime("%Y-%m-%dT%H:%M:%S.500Z"), "error": kind,
        "isApiErrorMessage": True, "apiErrorStatus": 400, "failedResponseHadNoToolCall": True,
        "message": {"model": "<synthetic>", "role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def ok(at: datetime, model: str = FABLE) -> dict[str, Any]:
    return {
        "type": "assistant", "timestamp": at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "message": {"model": model, "role": "assistant", "content": [{"type": "text", "text": "Done."}]},
    }


def prompt(at: datetime) -> dict[str, Any]:
    return {"type": "user", "timestamp": at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "message": {"role": "user", "content": "Go on."}}


def transcript(folder: Path, session_id: str, *entries: dict[str, Any]) -> None:
    (folder / "-home-x").mkdir(parents=True, exist_ok=True)
    (folder / "-home-x" / f"{session_id}.jsonl").write_text("".join(json.dumps(entry) + "\n" for entry in entries))


def append(folder: Path, session_id: str, *entries: dict[str, Any]) -> None:
    with (folder / "-home-x" / f"{session_id}.jsonl").open("a") as file:
        file.write("".join(json.dumps(entry) + "\n" for entry in entries))


def sync(world: World, now: datetime) -> list[int]:
    conn = db.connect(world.db_path)
    try:
        return error_stops.sync(conn, world.cache.sessions, now, world.checkout.parent)
    finally:
        conn.close()


def query(world: World, sql: str, *args: Any) -> list[dict[str, Any]]:
    conn = db.connect(world.db_path)
    try:
        return db.rows(conn.execute(sql, args).fetchall())
    finally:
        conn.close()


def deliver(world: World, message_id: int, at: datetime, state: str = "sent", detail: str = "") -> None:
    conn = db.connect(world.db_path)
    try:
        with db.transaction(conn):
            conn.execute("UPDATE messages SET state = ?, sent_at = ?, detail = ? WHERE id = ?",
                         (state, at.strftime("%Y-%m-%dT%H:%M:%SZ"), detail, message_id))
    finally:
        conn.close()


# Finding the stop


def test_the_old_client_stop_is_an_error_stop_and_its_last_good_answer_is_kept(transcripts: Path) -> None:
    stopped = NOW - timedelta(minutes=10)
    transcript(transcripts, SESSION_A, ok(stopped - timedelta(minutes=3)), prompt(stopped - timedelta(minutes=1)),
               api_error(stopped), {"type": "system"})
    stop = sessions.error_stop(SESSION_A)
    assert stop is not None
    assert (stop.at.replace(microsecond=0), stop.kind, stop.text) == (stopped, "unknown", OLD_CLIENT)
    assert stop.answered is not None and stop.answered.replace(microsecond=0) == stopped - timedelta(minutes=3)
    # The model the session runs on is still read through the synthetic entry.
    assert sessions.transcript_usage(SESSION_A).model == FABLE


@pytest.mark.parametrize("last", [
    ok(NOW),
    api_error(NOW, "You've hit your weekly limit · resets Jan 15, 10am (UTC)", "rate_limit"),
    api_error(NOW, "Login expired · Please run /login", "authentication_failed"),
], ids=["answered", "usage limit", "signed out"])
def test_an_answer_the_usage_limit_and_a_sign_out_are_no_error_stop(transcripts: Path, last: dict[str, Any]) -> None:
    transcript(transcripts, SESSION_A, api_error(NOW - timedelta(minutes=5)), last)
    assert sessions.error_stop(SESSION_A) is None


def test_a_refused_key_is_an_error_stop(transcripts: Path) -> None:
    text = 'API Error: 401 {"error":"Invalid API key"}'
    transcript(transcripts, SESSION_A, api_error(NOW, text, "authentication_failed"))
    stop = sessions.error_stop(SESSION_A)
    assert stop is not None and stop.text == text and stop.answered is None


# The resend


def test_a_session_stopped_by_the_old_client_gets_one_resend_once_its_account_and_model_work_again(
    world: World, transcripts: Path,
) -> None:
    stopped = NOW - timedelta(minutes=10)
    world.cache.sessions = [
        make_session(SESSION_A, pid=101, state="blocked", account="work"),
        make_session(SESSION_B, pid=102, state="blocked", account="work"),
        make_session(SESSION_C, pid=103, state="blocked", account="personal"),
    ]
    transcript(transcripts, SESSION_A, ok(stopped - timedelta(minutes=3)), api_error(stopped))
    transcript(transcripts, SESSION_B, ok(stopped - timedelta(minutes=20)))
    transcript(transcripts, SESSION_C, ok(stopped + timedelta(minutes=2)))
    # Nothing shows the error is gone: another account's answer says nothing of this one.
    assert sync(world, NOW) == []
    # Another model on the account went through: that says nothing of Fable.
    transcript(transcripts, SESSION_B, ok(stopped + timedelta(minutes=4), OPUS))
    assert sync(world, NOW) == []
    # Fable on the same account went through.
    transcript(transcripts, SESSION_B, ok(stopped + timedelta(minutes=4), OPUS), ok(stopped + timedelta(minutes=6)))
    [message_id] = sync(world, NOW)
    [message] = query(world, "SELECT * FROM messages WHERE id = ?", message_id)
    assert message["session_id"] == SESSION_A and message["author"] == "cockpit"
    assert f'ended on an API error at {stopped:%H:%M} UTC: "{OLD_CLIENT}"' in message["text"]
    assert f"A call on the same account and model went through at {stopped + timedelta(minutes=6):%H:%M} UTC" in message["text"]
    [row] = query(world, "SELECT * FROM error_stops")
    assert (row["session_id"], row["account"], row["model"], row["kind"], row["message_id"], row["outcome"]) == (
        SESSION_A, "work", FABLE, "unknown", message_id, "")
    # One message per stop, also once the message went out and the session has not answered yet.
    assert sync(world, NOW) == []
    deliver(world, message_id, NOW)
    assert sync(world, NOW + timedelta(minutes=1)) == []


def test_with_no_other_session_to_show_it_the_resend_goes_after_an_hour(world: World, transcripts: Path) -> None:
    stopped = NOW - timedelta(minutes=50)
    world.cache.sessions = [make_session(SESSION_A, pid=None, state="stopped", account="work")]
    transcript(transcripts, SESSION_A, ok(stopped - timedelta(minutes=3)), api_error(stopped))
    assert sync(world, NOW) == []
    [message_id] = sync(world, NOW + timedelta(minutes=11))
    [message] = query(world, "SELECT text FROM messages WHERE id = ?", message_id)
    assert "An hour has passed since, and no other session showed whether the error is gone" in message["text"]


def test_a_stop_older_than_twelve_hours_is_left_alone(world: World, transcripts: Path) -> None:
    world.cache.sessions = [make_session(SESSION_A, pid=None, state="stopped", account="work")]
    transcript(transcripts, SESSION_A, api_error(NOW - error_stops.LOOKBACK - timedelta(minutes=1)))
    assert sync(world, NOW) == []


def test_a_working_messaged_or_archived_session_gets_no_resend(world: World, transcripts: Path) -> None:
    stopped = NOW - timedelta(hours=2)
    world.cache.sessions = [
        make_session(SESSION_A, pid=101, state="working", account="work"),
        make_session(SESSION_B, pid=None, state="stopped", account="work"),
        make_session(SESSION_C, pid=None, state="stopped", account="work"),
    ]
    for session_id in (SESSION_A, SESSION_B, SESSION_C):
        transcript(transcripts, session_id, api_error(stopped))
    conn = db.connect(world.db_path)
    try:
        with db.transaction(conn):
            store.create_message(conn, "user", "Carry on.", {"session_id": SESSION_B}, world.checkout.parent)
            item_id = store.upsert_item(conn, {"key": "note:c", "title": "C"}, "agent:c")["id"]
            step_id = store.upsert_step(conn, {"item_id": item_id, "need": "n", "ask": "C?"}, "agent:c")["step"]["id"]
            conn.execute("INSERT INTO session_archives (session_id, step_id, asked_at) VALUES (?, ?, ?)",
                         (SESSION_C, step_id, (stopped - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")))
    finally:
        conn.close()
    assert sync(world, NOW) == []


# What the resend did


def test_a_resend_the_session_answers_is_fixed_and_goes_to_the_briefing(world: World, transcripts: Path) -> None:
    stopped = NOW - timedelta(hours=2)
    world.cache.sessions = [make_session(SESSION_A, pid=None, state="stopped", account="work")]
    transcript(transcripts, SESSION_A, ok(stopped - timedelta(minutes=3)), api_error(stopped))
    [message_id] = sync(world, NOW)
    deliver(world, message_id, NOW + timedelta(seconds=30))
    append(transcripts, SESSION_A, prompt(NOW + timedelta(seconds=30)), ok(NOW + timedelta(minutes=2)))
    assert sync(world, NOW + timedelta(minutes=3)) == []
    [row] = query(world, "SELECT outcome, item_id FROM error_stops")
    assert row == {"outcome": "fixed", "item_id": None}
    conn = db.connect(world.db_path)
    try:
        lines = error_stops.fixed(conn, NOW, NOW + timedelta(hours=1))
        assert error_stops.fixed(conn, NOW + timedelta(hours=1), NOW + timedelta(hours=2)) == []
    finally:
        conn.close()
    assert lines == [f'session 1111 ({SESSION_A[:8]}) stopped at {stopped:%H:%M} UTC on "{OLD_CLIENT}"; Cockpit'
                     " resent its last message and it went on"]


def test_resends_that_fail_go_on_one_high_priority_item_until_the_user_closes_it(
    world: World, transcripts: Path,
) -> None:
    stopped = NOW - timedelta(hours=2)
    world.cache.sessions = [
        make_session(SESSION_A, pid=None, state="stopped", account="work"),
        make_session(SESSION_B, pid=None, state="stopped", account="work"),
        make_session(SESSION_C, pid=None, state="stopped", account="work"),
    ]
    for session_id in (SESSION_A, SESSION_B):
        transcript(transcripts, session_id, ok(stopped - timedelta(minutes=3)), api_error(stopped))
    first, second = sync(world, NOW)
    # The first comes back on the same error.
    deliver(world, first, NOW + timedelta(seconds=30))
    append(transcripts, SESSION_A, prompt(NOW + timedelta(seconds=30)), api_error(NOW + timedelta(minutes=1)))
    later = NOW + timedelta(minutes=2)
    assert sync(world, later) == []  # the repeat gets no second message
    rows = query(world, "SELECT session_id, outcome, tried, item_id, message_id FROM error_stops ORDER BY id")
    assert [(row["session_id"], row["outcome"]) for row in rows] == [
        (SESSION_A, "failed"), (SESSION_B, ""), (SESSION_A, "repeat")]
    assert rows[0]["tried"] == f'Cockpit resent its last message; it ended on "{OLD_CLIENT}" again'
    item_id = rows[0]["item_id"]
    [item] = query(world, "SELECT * FROM items WHERE id = ?", item_id)
    assert (item["title"], item["priority"]) == (error_stops.ITEM_TITLE, "high")
    [step] = query(world, f"SELECT * FROM steps s WHERE s.item_id = ? AND {store.OPEN}", item_id)
    assert (step["need"], step["ask"]) == ("look", "1 session still stopped on API errors; look?")
    assert step["details"][0] == {"title": "The errors", "body": f'- "{OLD_CLIENT}"'}
    sessions_detail = step["details"][1]["body"]
    assert sessions_detail == (
        f"- session 1111 ({SESSION_A}), on no task: stopped at {stopped:%H:%M} UTC; Cockpit resent its last message;"
        f' it ended on "{OLD_CLIENT}" again')

    # The second cannot be delivered: it joins the same item.
    deliver(world, second, later, "failed", "the session's folder is gone")
    assert sync(world, later) == []
    rows = query(world, "SELECT session_id, outcome, tried, item_id FROM error_stops WHERE session_id = ?", SESSION_B)
    assert rows == [{"session_id": SESSION_B, "outcome": "failed", "item_id": item_id,
                     "tried": "Cockpit's message could not be delivered: the session's folder is gone"}]
    [step] = query(world, f"SELECT * FROM steps s WHERE s.item_id = ? AND {store.OPEN}", item_id)
    assert step["ask"] == "2 sessions still stopped on API errors; look?"
    assert step["details"][0]["body"] == f'- "{OLD_CLIENT}" (2 sessions)'

    # Once the user closed the step, the next failure makes a new item, also within the same minute.
    conn = db.connect(world.db_path)
    try:
        with db.transaction(conn):
            conn.execute("UPDATE steps SET status = 'done' WHERE id = ?", (step["id"],))
    finally:
        conn.close()
    transcript(transcripts, SESSION_C, ok(later - timedelta(minutes=70)), api_error(later - timedelta(minutes=61)))
    [third] = sync(world, later)
    deliver(world, third, later)
    append(transcripts, SESSION_C, api_error(later + timedelta(minutes=1)))
    sync(world, later)
    [row] = query(world, "SELECT item_id FROM error_stops WHERE session_id = ? AND outcome = 'failed'", SESSION_C)
    assert row["item_id"] not in (None, item_id)


def test_a_different_error_after_an_answer_is_fixed_and_a_usage_limit_after_it_is_left_to_its_own_recovery(
    world: World, transcripts: Path,
) -> None:
    stopped = NOW - timedelta(hours=2)
    world.cache.sessions = [
        make_session(SESSION_A, pid=None, state="stopped", account="work"),
        make_session(SESSION_B, pid=None, state="stopped", account="work"),
    ]
    for session_id in (SESSION_A, SESSION_B):
        transcript(transcripts, session_id, ok(stopped - timedelta(minutes=3)), api_error(stopped))
    first, second = sync(world, NOW)
    for message_id in (first, second):
        deliver(world, message_id, NOW)
    append(transcripts, SESSION_A, ok(NOW + timedelta(minutes=1)),
           api_error(NOW + timedelta(minutes=2), "API Error: Autocompact is thrashing"))
    append(transcripts, SESSION_B,
           api_error(NOW + timedelta(minutes=1), "You've hit your weekly limit · resets Jan 15, 10am (UTC)", "rate_limit"))
    assert sync(world, NOW + timedelta(minutes=3)) == []
    rows = query(world, "SELECT session_id, outcome FROM error_stops ORDER BY id")
    assert rows == [{"session_id": SESSION_A, "outcome": "fixed"}, {"session_id": SESSION_B, "outcome": "other"}]
    assert query(world, "SELECT id FROM items WHERE key LIKE 'note:error-stops-%'") == []


def test_a_failure_whose_error_holds_a_request_id_still_reaches_the_user(world: World, transcripts: Path) -> None:
    text = "API Error: 500 Internal server error; request id 0a1b2c3d4e5f"
    stopped = NOW - timedelta(hours=2)
    world.cache.sessions = [make_session(SESSION_A, pid=None, state="stopped", account="work")]
    transcript(transcripts, SESSION_A, ok(stopped - timedelta(minutes=3)), api_error(stopped, text))
    [message_id] = sync(world, NOW)
    deliver(world, message_id, NOW)
    append(transcripts, SESSION_A, api_error(NOW + timedelta(seconds=20), text))
    sync(world, NOW + timedelta(minutes=1))
    [row] = query(world, "SELECT item_id FROM error_stops WHERE outcome = 'failed'")
    [step] = query(world, f"SELECT * FROM steps s WHERE s.item_id = ? AND {store.OPEN}", row["item_id"])
    assert step["details"][0]["body"] == f'- "{text}"'


def test_a_session_that_fails_before_delivery_records_its_send_time_still_fails(
    world: World, transcripts: Path,
) -> None:
    stopped = NOW - timedelta(hours=2)
    world.cache.sessions = [make_session(SESSION_A, pid=None, state="stopped", account="work")]
    transcript(transcripts, SESSION_A, ok(stopped - timedelta(minutes=3)), api_error(stopped))
    [message_id] = sync(world, NOW)
    # The session resumed and failed at once; delivery stamped its send time a minute later.
    append(transcripts, SESSION_A, prompt(NOW + timedelta(seconds=5)), api_error(NOW + timedelta(seconds=10)))
    deliver(world, message_id, NOW + timedelta(minutes=1))
    sync(world, NOW + timedelta(minutes=2))
    assert [row["outcome"] for row in query(world, "SELECT outcome FROM error_stops ORDER BY id")] == [
        "failed", "repeat"]


def test_an_answer_from_before_another_session_moved_account_says_nothing_of_its_new_account(
    world: World, transcripts: Path,
) -> None:
    stopped = NOW - timedelta(minutes=10)
    world.cache.sessions = [
        make_session(SESSION_A, pid=101, state="blocked", account="work"),
        make_session(SESSION_B, pid=102, state="blocked", account="work"),
    ]
    transcript(transcripts, SESSION_A, ok(stopped - timedelta(minutes=3)), api_error(stopped))
    transcript(transcripts, SESSION_B, ok(stopped + timedelta(minutes=4)))
    conn = db.connect(world.db_path)
    try:
        with db.transaction(conn):
            conn.execute("INSERT INTO session_moves (session_id, account, at) VALUES (?, 'work', ?)",
                         (SESSION_B, (stopped + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")))
    finally:
        conn.close()
    assert sync(world, NOW) == []
    transcript(transcripts, SESSION_B, ok(stopped + timedelta(minutes=4)), ok(stopped + timedelta(minutes=6)))
    assert len(sync(world, NOW)) == 1


def test_a_terminal_session_that_is_busy_gets_no_resend(world: World, transcripts: Path) -> None:
    stopped = NOW - timedelta(hours=2)
    # A terminal session lists no state; its status says it is busy, as while a subagent it started still runs.
    world.cache.sessions = [dataclasses.replace(make_session(SESSION_A, pid=101, state="", account="work"),
                                                status="busy")]
    transcript(transcripts, SESSION_A, api_error(stopped))
    assert sync(world, NOW) == []


def test_an_error_entry_without_a_time_zone_is_no_error_stop(transcripts: Path) -> None:
    entry = api_error(NOW) | {"timestamp": NOW.strftime("%Y-%m-%dT%H:%M:%S")}
    transcript(transcripts, SESSION_A, entry)
    assert sessions.error_stop(SESSION_A) is None
