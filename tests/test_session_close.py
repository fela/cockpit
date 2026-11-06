"""The sweeper closes a live session it judged done ."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cockpit import cli, db, delivery, server, sessions, store
from cockpit.sessions import Session
from tests.conftest import ORIGIN, SESSION_A, SESSION_B, World, make_session

REASON = {"reason": "The reminder text is approved; nothing remains on its item"}


def idle(cwd: Path, **changes) -> Session:
    """A live background session whose turn ended long ago, with nothing running in it."""
    session = replace(
        make_session(SESSION_A, pid=4242, state="done", cwd=str(cwd)),
        status="idle", tempo="idle", job_state="done", updated_at="2026-01-06T10:00:00.000Z",
    )
    return replace(session, **changes)


def close(world: World, as_: str = "sweep", session_id: str = SESSION_A):
    agent = TestClient(world.client.app, base_url=ORIGIN)
    return agent.post(f"/api/sessions/{session_id}/close", json=REASON, headers=world.agent(as_))


def stops(world: World, session: Session) -> None:
    """The fake `claude`: a stop leaves the session without a process."""
    world.cache.sessions = [session]

    def runner(account, args, cwd):
        if args[1] == "stop":
            world.cache.sessions = [replace(session, pid=None, status="")]
        return world.runner(account, args, cwd)

    world.deliverer.run = runner


def events(world: World) -> list[dict]:
    conn = db.connect(world.db_path)
    try:
        return db.rows(conn.execute("SELECT * FROM events WHERE kind = 'session.closed'").fetchall())
    finally:
        conn.close()


def test_the_sweeper_closes_an_idle_session_and_records_why(world: World, tmp_path: Path) -> None:
    stops(world, idle(tmp_path, in_flight=["artifact_watch"]))
    response = close(world)
    assert response.status_code == 200, response.text
    assert response.json() == {"session_id": SESSION_A, "closed": True, "detail": f"stopped session {SESSION_A[:8]}"}
    assert [args for args, _ in world.runner.calls] == [["claude", "stop", SESSION_A[:8]]]
    [event] = events(world)
    assert event["actor"] == "agent:sweep"
    assert event["data"] == {
        "session_id": SESSION_A, "reason": REASON["reason"], "idle_since": "2026-01-06T10:00:00.000Z",
        "in_flight": ["artifact_watch"],
    }


def test_only_the_sweeper_closes_sessions(world: World, tmp_path: Path) -> None:
    stops(world, idle(tmp_path))
    assert close(world, as_="app-fix").status_code == 403
    page = world.client.post(f"/api/sessions/{SESSION_A}/close", json=REASON, headers=world.user())
    assert page.status_code == 403
    assert world.runner.calls == []


def test_a_stopped_session_is_left_as_it_is_and_its_close_recorded(world: World, tmp_path: Path) -> None:
    """The recorded close keeps the usage limit's reset from waking a session the sweeper judged done
    (limits.ended_on_purpose)."""
    stops(world, idle(tmp_path, pid=None, status=""))
    response = close(world)
    assert response.json() == {"session_id": SESSION_A, "closed": False, "detail": f"session {SESSION_A[:8]} was already stopped"}
    assert world.runner.calls == []
    [event] = events(world)
    assert (event["actor"], event["data"]["session_id"], event["data"]["reason"]) == (
        "agent:sweep", SESSION_A, REASON["reason"]
    )


def test_a_stopped_session_is_closed_whatever_its_listing_last_said_ran_in_it(world: World, tmp_path: Path) -> None:
    """A session the usage limit stopped can still list an old subagent, or no background job: neither runs any
    more, so the close is recorded and the reset leaves the session stopped."""
    for changes in ({"in_flight": ["local_agent"]}, {"job_state": ""}):
        stops(world, idle(tmp_path, pid=None, status="", **changes))
        assert close(world).json()["closed"] is False
    assert len(events(world)) == 2


def test_a_stopped_session_with_an_open_step_keeps_its_wake_up(world: World, tmp_path: Path) -> None:
    """The user's answer resumes it, and the usage limit's reset still wakes it (SESSION-13)."""
    stops(world, idle(tmp_path, pid=None, status=""))
    agent = TestClient(world.client.app, base_url=ORIGIN)
    agent.put("/api/items", json={"key": "note:x", "title": "X", "sessions": [SESSION_A]}, headers=world.agent())
    agent.post("/api/steps", json={"item_key": "note:x", "need": "decide", "ask": "A or B?"}, headers=world.agent())
    response = close(world)
    assert response.status_code == 409
    assert response.json()["detail"] == f"Cockpit keeps session {SESSION_A[:8]} open: 1 open step on Cockpit goes to it"
    assert events(world) == []


def test_cockpit_keeps_a_session_running_while_something_in_it_runs_or_waits(world: World, tmp_path: Path) -> None:
    for changes, why in (
        ({"name": "Cockpit sweep"}, "the sweeper closes other sessions, not itself"),
        ({"job_state": ""}, "it is not a background session"),
        ({"state": "working", "status": "busy", "tempo": "active", "job_state": "working"}, "its turn is under way"),
        ({"job_state": "working", "tempo": "blocked"}, "a dialog in its terminal waits for an answer"),
        ({"status": "busy", "in_flight": ["local_agent"]}, "a subagent it started still runs"),
    ):
        stops(world, idle(tmp_path, **changes))
        response = close(world)
        assert response.status_code == 409, changes
        assert response.json()["detail"] == f"Cockpit keeps session {SESSION_A[:8]} running: {why}"
    assert world.runner.calls == []


def test_an_open_step_or_a_waiting_message_keeps_the_session_running(world: World, tmp_path: Path) -> None:
    stops(world, idle(tmp_path))
    agent = TestClient(world.client.app, base_url=ORIGIN)
    agent.put("/api/items", json={"key": "note:x", "title": "X", "sessions": [SESSION_A]}, headers=world.agent())
    step = agent.post("/api/steps", json={"item_key": "note:x", "need": "decide", "ask": "A or B?"}, headers=world.agent())
    assert close(world).json()["detail"] == f"Cockpit keeps session {SESSION_A[:8]} running: 1 open step on Cockpit goes to it"
    agent.post(f"/api/steps/{step.json()['step']['id']}/withdraw", json={}, headers=world.agent())
    conn = db.connect(world.db_path)
    try:
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO messages (author, text, session_id, state, as_prompt, waits_since, created_at)"
                " VALUES ('user', 'Go with A', ?, 'queued', 1, '2026-01-13T09:00:00Z', '2026-01-13T09:00:00Z')",
                (SESSION_A,),
            )
    finally:
        conn.close()
    assert close(world).json()["detail"] == f"Cockpit keeps session {SESSION_A[:8]} running: a message to it waits or is being sent"
    assert world.runner.calls == []


def test_cockpit_get_lists_the_idle_sessions_the_sweeper_may_close(world: World, tmp_path: Path) -> None:
    working = replace(
        make_session(SESSION_B, pid=7, state="working", cwd=str(tmp_path)),
        status="busy", tempo="active", job_state="working", updated_at="2026-01-06T10:00:00.000Z",
    )
    world.cache.sessions = [idle(tmp_path, name="Alex asks", in_flight=["artifact_watch"]), working]
    agent = TestClient(world.client.app, base_url=ORIGIN)
    agent.put("/api/items", json={"key": "note:x", "title": "export help", "sessions": [SESSION_A]}, headers=world.agent())
    state = world.client.get("/api/state").json()
    text = cli.summary(state)
    item_id = state["items"][0]["id"]
    assert f"  {SESSION_A[:8]} Alex asks [{SESSION_A}]\n    done, last change " in text
    assert f"\n    background: artifact_watch\n    items: {item_id} export help" in text
    assert SESSION_B not in text.split("Sessions idle over an hour")[1].split("Items by who holds the ball")[0]
    entries = {entry["session_id"]: entry for entry in state["sessions"]}
    assert entries[SESSION_A]["close_refusal"] is None
    assert entries[SESSION_B]["close_refusal"] == "its turn is under way"


# SPEC SESSION-15: Cockpit stops a session itself once it has been idle an hour.
NOW = datetime.fromisoformat("2026-01-06T12:00:00Z")


def stops_due(world: World, listed: list[Session]) -> list[str]:
    conn = db.connect(world.db_path)
    try:
        return delivery.stops_due(conn, listed, NOW, delivery.IDLE_STOP_AFTER)
    finally:
        conn.close()


def idle_stop(world: World, session: Session, after: timedelta) -> str | None:
    conn = db.connect(world.db_path)
    try:
        return delivery.idle_stop(conn, session, NOW, after)
    finally:
        conn.close()


MEMINFO = "MemTotal:       33554432 kB\nMemFree:         4194304 kB\nMemAvailable:   {available} kB\nBuffers:  0 kB\n"


def test_while_memory_is_low_an_idle_session_stops_after_15_minutes(world: World, tmp_path: Path) -> None:
    assert delivery.idle_stop_after(MEMINFO.format(available=12_000_000)) == delivery.IDLE_STOP_AFTER
    low = delivery.idle_stop_after(MEMINFO.format(available=8_100_000))
    assert low == timedelta(minutes=15)
    quarter = idle(tmp_path, updated_at="2026-01-06T11:40:00Z")
    assert idle_stop(world, quarter, delivery.IDLE_STOP_AFTER) is None
    assert idle_stop(world, quarter, low) == (
        "idle for 20 minutes with nothing running or waiting in it, while the host is low on memory"
    )
    assert idle_stop(world, replace(quarter, updated_at="2026-01-06T11:50:00Z"), low) is None
    # Without /proc (macOS), or when it does not parse, the hour holds and the wake poll goes on.
    assert server.idle_stop_after(tmp_path / "meminfo") == delivery.IDLE_STOP_AFTER
    (tmp_path / "meminfo").write_text("MemTotal:       33554432 kB\n")
    assert server.idle_stop_after(tmp_path / "meminfo") == delivery.IDLE_STOP_AFTER
    (tmp_path / "meminfo").write_text(MEMINFO.format(available=8_100_000))
    assert server.idle_stop_after(tmp_path / "meminfo") == low
    # What keeps a session running still does: here a scheduled prompt.
    assert idle_stop(world, replace(quarter, state="working", tempo="idle", in_flight=["session_cron"]), low) is None


def test_an_idle_session_stops_after_an_hour(world: World, tmp_path: Path) -> None:
    hour = idle(tmp_path, updated_at="2026-01-06T10:59:00Z")
    assert stops_due(world, [hour]) == [SESSION_A]
    assert idle_stop(world, hour, delivery.IDLE_STOP_AFTER) == "idle for 1 hour with nothing running or waiting in it"
    assert stops_due(world, [replace(hour, updated_at="2026-01-06T11:01:00Z")]) == []
    days = idle(tmp_path, updated_at="2026-01-04T11:00:00Z")
    assert idle_stop(world, days, delivery.IDLE_STOP_AFTER) == "idle for 49 hours with nothing running or waiting in it"


def test_an_idle_stop_leaves_alone_what_the_sweepers_close_would(world: World, tmp_path: Path) -> None:
    old = idle(tmp_path)
    for changes in (
        {"name": "Cockpit sweep"},
        {"job_state": ""},
        {"pid": None, "status": "", "version": ""},
        {"state": "working", "status": "busy", "tempo": "active", "job_state": "working"},
        # Background work after its turn shows as status busy, a scheduled prompt as state working.
        {"status": "busy", "in_flight": ["local_bash"]},
        {"state": "working", "tempo": "idle", "in_flight": ["session_cron"]},
        {"job_state": "working", "tempo": "blocked"},
    ):
        assert stops_due(world, [replace(old, **changes)]) == [], changes
    agent = TestClient(world.client.app, base_url=ORIGIN)
    agent.put("/api/items", json={"key": "note:x", "title": "X", "sessions": [SESSION_A]}, headers=world.agent())
    agent.post("/api/steps", json={"item_key": "note:x", "need": "decide", "ask": "A or B?"}, headers=world.agent())
    # An open step to it keeps it from the sweeper's close, but needs no process: the user's answer resumes it.
    conn = db.connect(world.db_path)
    try:
        assert store.close_refusal(conn, old) == "1 open step on Cockpit goes to it"
    finally:
        conn.close()
    assert stops_due(world, [old]) == [SESSION_A]


def test_the_wake_loop_stops_it_and_records_why(world: World, tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="cockpit.server")
    session = idle(tmp_path)
    stops(world, session)
    server.stop_idle(world.deliverer, SESSION_A, delivery.IDLE_STOP_AFTER)
    assert [args for args, _ in world.runner.calls] == [["claude", "stop", SESSION_A[:8]]]
    [event] = events(world)
    assert event["actor"] == "cockpit"
    assert event["data"]["reason"].startswith("idle for ") and event["data"]["reason"].endswith(" hours with nothing running or waiting in it")
    # A session that since the listing started a turn, a background command or a scheduled prompt, or woke and
    # ended a turn just now, keeps running, and the server says so in its log.
    just_now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    for changes in (
        {"state": "working", "status": "busy", "tempo": "active", "job_state": "working"},
        {"status": "busy", "in_flight": ["local_bash"]},
        {"state": "working", "tempo": "idle", "in_flight": ["session_cron"]},
        {"updated_at": just_now},
    ):
        stops(world, replace(session, **changes))
        server.stop_idle(world.deliverer, SESSION_A, delivery.IDLE_STOP_AFTER)
        assert len(world.runner.calls) == 1, changes
    assert "left idle session" in caplog.text and "no longer idle" in caplog.text


@pytest.mark.skipif(not Path("/proc/self/exe").exists(), reason="reads a process's release from Linux's /proc")
def test_the_release_a_process_runs() -> None:
    assert sessions.running_version(os.getpid()) == Path(os.readlink("/proc/self/exe")).name
    gone = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True, check=True)
    assert sessions.running_version(int(gone.stdout)) == ""
