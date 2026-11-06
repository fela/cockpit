from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cockpit import cli, db, delivery, store
from cockpit.sessions import Session
from tests.conftest import ORIGIN, SESSION_A, SESSION_B, World, make_session
from tests.test_api import run_sql
from tests.test_delivery import live, message, stops_and_resumes

ITEM = "gh:example-org/app#10"


def _step(world: World, need: str, session_id: str | None = SESSION_A) -> int:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    agent.put("/api/items", json={"key": ITEM, "title": "PR 10"}, headers=world.agent())
    body = {"item_key": ITEM, "need": need, "ask": "Review", "session_id": session_id}
    response = agent.post("/api/steps", json=body, headers=world.agent())
    assert response.status_code == 200, response.text
    return response.json()["step"]["id"]


def _done(world: World, step_id: int) -> dict:
    response = world.client.post(f"/api/steps/{step_id}/done", json={}, headers=world.user())
    assert response.status_code == 200, response.text
    return response.json()


def _archives(world: World) -> list[dict]:
    conn = db.connect(world.db_path)
    try:
        return db.rows(conn.execute("SELECT * FROM session_archives ORDER BY id").fetchall())
    finally:
        conn.close()


def _due(world: World) -> list[int]:
    conn = db.connect(world.db_path)
    try:
        return delivery.archives_due(conn, world.cache.sessions, datetime.now(UTC))
    finally:
        conn.close()


def _turn_over(session: Session) -> Session:
    """The session's turn on Cockpit's message ended, a moment after the message reached it."""
    later = (datetime.now(UTC) + timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return replace(session, state="done", status="idle", updated_at=later)


def _sessions_on_page(world: World) -> dict[str, dict]:
    return {entry["session_id"]: entry for entry in world.client.get("/api/state").json()["sessions"]}


def _told(world: World, monkeypatch) -> dict:
    """The user archives the last open step of a running session in the middle of a turn; the session's turn ends,
    and Cockpit's message reaches it as its prompt. Returns the archive."""
    stops_and_resumes(world, live("working", "busy"), monkeypatch)
    step = _done(world, _step(world, "review"))
    assert step["session_end"] == {"name": "session 1111", "how": "archiving", "other_steps": 0}
    [archive] = _archives(world)
    assert message(world, archive["message_id"])["state"] == "queued"
    assert world.runner.calls == []
    stops_and_resumes(world, live("done", "idle"), monkeypatch)
    conn = db.connect(world.db_path)
    try:
        assert delivery.waiting_due(conn, world.cache.sessions, datetime.now(UTC)) == [archive["message_id"]]
    finally:
        conn.close()
    world.deliverer.send(archive["message_id"])
    assert message(world, archive["message_id"])["state"] == "sent"
    return archive


def test_done_on_a_step_that_is_not_the_last_closes_only_the_step(world: World) -> None:
    first = _step(world, "review")
    _step(world, "decide")
    step = _done(world, first)
    assert step["status"] == "done"
    assert step["session_end"] == {"name": "session 1111", "how": "kept", "other_steps": 1}
    assert world.runner.calls == []
    assert _archives(world) == []


def test_done_on_the_last_step_when_it_is_a_result_to_read_keeps_the_session(world: World) -> None:
    step = _done(world, _step(world, "read:results"))
    assert step["status"] == "done"
    assert "session_end" not in step
    assert world.runner.calls == []
    assert _archives(world) == []


def test_a_step_that_targets_the_session_through_its_item_keeps_it(world: World) -> None:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    other = {"key": "gh:example-org/app#20", "title": "PR 20", "sessions": [SESSION_A]}
    agent.put("/api/items", json=other, headers=world.agent())
    agent.post("/api/steps", json={"item_key": other["key"], "need": "merge", "ask": "Merge", "answers": ["Merge it"]}, headers=world.agent())
    step = _done(world, _step(world, "review"))
    assert step["session_end"]["how"] == "kept"
    assert _archives(world) == []


def test_archive_tells_the_session_then_stops_it_once_that_turn_is_over(world: World, monkeypatch) -> None:
    archive = _told(world, monkeypatch)
    resume = world.runner.calls[-1][0]
    assert resume[:3] == ["claude", "--bg", "--resume"]
    assert resume[-1].startswith("Message from Cockpit itself (automatic, not from Sam or an agent):")
    assert "The sweep checks every archived session and sends it a message when its work needs it" in resume[-1]
    assert "cockpit session keep" not in resume[-1]
    assert _sessions_on_page(world)[SESSION_A]["archive"]["state"] == "archiving"
    # The session works on the message: nothing to stop yet.
    assert _due(world) == []
    world.cache.sessions = [_turn_over(world.cache.sessions[0])]
    assert _due(world) == [archive["id"]]
    world.runner.calls.clear()
    world.deliverer.archive(archive["id"])
    assert world.runner.calls == [(["claude", "stop", SESSION_A[:8]], Path.home())]
    [stopped] = _archives(world)
    assert stopped["stopped_at"] is not None and stopped["failure"] == ""
    # Out of Sessions on the page, and listed for the sweep.
    assert SESSION_A not in _sessions_on_page(world)
    state = world.client.get("/api/state").json()
    assert [entry["session_id"] for entry in state["recent_archives"]] == [SESSION_A]
    assert "archived: stopped, out of the main view" in cli.summary(state)


def test_a_session_that_started_another_turn_since_the_poll_is_not_stopped(world: World, monkeypatch) -> None:
    archive = _told(world, monkeypatch)
    world.cache.sessions = [_turn_over(world.cache.sessions[0])]
    assert _due(world) == [archive["id"]]
    # Before the archive's thread runs, a message starts another turn: the archive waits for that one to end.
    world.cache.sessions = [replace(world.cache.sessions[0], state="working", status="busy")]
    world.runner.calls.clear()
    world.deliverer.archive(archive["id"])
    assert world.runner.calls == []
    [pending] = _archives(world)
    assert pending["stopped_at"] is None
    world.cache.sessions = [_turn_over(world.cache.sessions[0])]
    world.deliverer.archive(archive["id"])
    assert world.runner.calls == [(["claude", "stop", SESSION_A[:8]], Path.home())]


def test_a_session_has_no_keep_after_an_archive(world: World, monkeypatch) -> None:
    """SPEC SESSION-4: the sweep's check of each archive is the override; a session cannot keep itself running."""
    _told(world, monkeypatch)
    agent = TestClient(world.client.app, base_url=ORIGIN)
    kept = agent.post(f"/api/sessions/{SESSION_A}/keep", json={"reason": "PR 10 waits"}, headers=world.agent("app-10"))
    assert kept.status_code in (404, 405)


def test_a_stopped_session_whose_turn_ended_done_is_archived_as_it_is(world: World) -> None:
    world.cache.sessions = [replace(world.cache.sessions[0], state="done")]
    step = _done(world, _step(world, "review"))
    assert step["session_end"]["how"] == "archiving"
    assert world.runner.calls == []
    [archive] = _archives(world)
    assert archive["message_id"] is None and archive["stopped_at"] is not None


def test_reopen_before_the_message_went_out_sets_it_aside(world: World, monkeypatch) -> None:
    stops_and_resumes(world, live("working", "busy"), monkeypatch)
    step_id = _step(world, "review")
    _done(world, step_id)
    response = world.client.post(f"/api/steps/{step_id}/reopen", json={}, headers=world.user())
    assert response.status_code == 200, response.text
    [archive] = _archives(world)
    assert archive["cancelled_at"] is not None
    unsent = message(world, archive["message_id"])
    assert unsent["state"] == "failed" and unsent["dismissed_at"] is not None
    assert world.runner.calls == []
    assert _sessions_on_page(world)[SESSION_A]["archive"] is None


def test_reopen_after_the_message_reached_the_session_tells_it(world: World, monkeypatch) -> None:
    archive = _told(world, monkeypatch)
    world.client.post(f"/api/steps/{archive['step_id']}/reopen", json={}, headers=world.user())
    conn = db.connect(world.db_path)
    try:
        follow_up = db.row(conn.execute("SELECT * FROM messages WHERE id > ?", (archive["message_id"],)).fetchone())
    finally:
        conn.close()
    assert follow_up["text"].startswith("Sam reopened step 1, so Cockpit no longer archives this session.")
    # It waits behind the session's turn on the first message.
    assert follow_up["as_prompt"] == 1 and follow_up["state"] == "queued"
    world.cache.sessions = [_turn_over(world.cache.sessions[0])]
    assert _due(world) == []


def test_a_later_message_brings_an_archived_session_back(world: World, monkeypatch) -> None:
    archive = _told(world, monkeypatch)
    world.cache.sessions = [_turn_over(world.cache.sessions[0])]
    world.deliverer.archive(archive["id"])
    assert SESSION_A not in _sessions_on_page(world)
    response = world.client.post(
        "/api/messages", json={"text": "One more thing", "target": {"session_id": SESSION_A}}, headers=world.user()
    )
    assert response.status_code == 200, response.text
    [revived] = _archives(world)
    assert revived["revived_at"] is not None
    assert _sessions_on_page(world)[SESSION_A]["archive"] is None


def test_a_message_while_the_archive_waits_keeps_the_session(world: World, monkeypatch) -> None:
    stops_and_resumes(world, live("working", "busy"), monkeypatch)
    _done(world, _step(world, "review"))
    world.client.post(
        "/api/messages", json={"text": "Before you go", "target": {"session_id": SESSION_A}}, headers=world.user()
    )
    [archive] = _archives(world)
    assert archive["revived_at"] is not None
    assert message(world, archive["message_id"])["detail"] == (
        "Not sent: a later message to the session brought it back first."
    )


def test_a_failed_archive_message_leaves_the_session_and_says_why(world: World) -> None:
    world.runner.returncode = 1
    world.runner.output = "no such session"
    _done(world, _step(world, "review"))
    assert _due(world) == []
    [archive] = _archives(world)
    assert archive["failure"].startswith("Cockpit's message did not reach the session:")
    entry = _sessions_on_page(world)[SESSION_A]
    assert entry["archive"]["state"] == "failed"


def test_a_session_still_at_work_an_hour_later_is_left_running(world: World, monkeypatch) -> None:
    archive = _told(world, monkeypatch)
    an_hour_ago = (datetime.now(UTC) - timedelta(minutes=61)).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_sql(world, "UPDATE messages SET sent_at = ? WHERE id = ?", (an_hour_ago, archive["message_id"]))
    assert _due(world) == []
    [left] = _archives(world)
    assert left["failure"] == (
        "the session still worked 1 h after Cockpit's message reached it, so Cockpit left it running"
    )


def test_a_session_that_stopped_by_itself_counts_as_archived(world: World, monkeypatch) -> None:
    _told(world, monkeypatch)
    world.cache.sessions = [replace(world.cache.sessions[0], pid=None, status="", state="done")]
    assert _due(world) == []
    [archive] = _archives(world)
    assert archive["stopped_at"] is not None and archive["failure"] == ""


def test_a_failed_stop_is_recorded(world: World, monkeypatch) -> None:
    archive = _told(world, monkeypatch)
    world.cache.sessions = [_turn_over(world.cache.sessions[0])]
    world.deliverer.run = world.runner
    world.runner.returncode = 1
    world.runner.output = "stop refused"
    world.deliverer.archive(archive["id"])
    [failed] = _archives(world)
    assert "claude stop exited 1" in failed["failure"]
    assert _sessions_on_page(world)[SESSION_A]["archive"]["state"] == "failed"


def test_an_agents_done_leaves_the_session_running(world: World) -> None:
    step_id = _step(world, "review")
    agent = TestClient(world.client.app, base_url=ORIGIN)
    response = agent.post(f"/api/steps/{step_id}/done", json={}, headers=world.agent())
    assert response.status_code == 200, response.text
    assert "session_end" not in response.json()
    assert _archives(world) == []


def test_dismiss_leaves_the_session_running(world: World) -> None:
    step_id = _step(world, "review")
    response = world.client.post(f"/api/steps/{step_id}/dismiss", json={}, headers=world.user())
    assert response.status_code == 200, response.text
    assert _archives(world) == []


def test_done_without_a_session_archives_nothing(world: World) -> None:
    step = _done(world, _step(world, "review", session_id=None))
    assert step["session_end"] is None
    assert _archives(world) == []


def test_done_never_archives_the_sweep(world: World) -> None:
    world.cache.sessions = [replace(world.cache.sessions[0], name=store.SWEEP_SESSION_NAME)]
    step = _done(world, _step(world, "review"))
    assert step["session_end"] is None
    assert _archives(world) == []


def test_archive_events_are_recorded(world: World, monkeypatch) -> None:
    archive = _told(world, monkeypatch)
    world.cache.sessions = [_turn_over(world.cache.sessions[0])]
    world.deliverer.archive(archive["id"])
    conn = db.connect(world.db_path)
    try:
        rows = conn.execute(
            "SELECT kind, data FROM events WHERE kind LIKE 'session.%' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert [(kind, json.loads(data)) for kind, data in rows] == [
        ("session.archived", {"session_id": SESSION_A, "told": True}),
        ("session.stopped", {"session_id": SESSION_A, "failure": ""}),
    ]


def test_the_archive_is_claimed_before_the_stop(world: World, monkeypatch) -> None:
    # A cancel that comes while `claude stop` runs finds the archive stopped, not still pending: the worker no longer
    # checks and then stops while a cancel lands in between
    archive = _told(world, monkeypatch)
    world.cache.sessions = [_turn_over(world.cache.sessions[0])]
    seen = []
    run = world.deliverer.run

    def stop_sees_the_claim(account, args, cwd):
        if args[1] == "stop":
            [row] = _archives(world)
            seen.append(row["stopped_at"])
        return run(account, args, cwd)

    world.deliverer.run = stop_sees_the_claim
    world.deliverer.archive(archive["id"])
    assert len(seen) == 1 and seen[0] is not None
    # A cancel committed before the worker's claim stops nothing.
    conn = db.connect(world.db_path)
    try:
        conn.execute("UPDATE session_archives SET stopped_at = NULL, cancelled_at = ? WHERE id = ?",
                     (db.now_iso(), archive["id"]))
    finally:
        conn.close()
    world.runner.calls.clear()
    world.deliverer.archive(archive["id"])
    assert world.runner.calls == []


def test_a_lookup_that_fails_leaves_the_archive_for_the_next_poll(world: World, monkeypatch) -> None:
    # The session listing timed out: nothing is claimed, so the next poll tries again.
    archive = _told(world, monkeypatch)
    world.cache.sessions = [_turn_over(world.cache.sessions[0])]
    lookup = world.deliverer.lookup

    def times_out(session_id):
        raise TimeoutError("claude agents timed out")

    world.deliverer.lookup = times_out
    with pytest.raises(TimeoutError):
        world.deliverer.archive(archive["id"])
    [pending] = _archives(world)
    assert (pending["stopped_at"], pending["failure"]) == (None, "")
    assert _due(world) == [archive["id"]]
    world.deliverer.lookup = lookup
    world.runner.calls.clear()
    world.deliverer.archive(archive["id"])
    assert world.runner.calls == [(["claude", "stop", SESSION_A[:8]], Path.home())]


def test_done_archives_only_the_session_the_page_showed(world: World) -> None:
    # The page showed session A on the card; the step goes to B now. Done is refused, and B keeps running.
    world.cache.sessions = [*world.cache.sessions, make_session(SESSION_B, pid=None)]
    step_id = _step(world, "review", SESSION_A)
    run_sql(world, "UPDATE steps SET session_id = ? WHERE id = ?", (SESSION_B, step_id))
    refused = world.client.post(f"/api/steps/{step_id}/done", json={"session_id": SESSION_A}, headers=world.user())
    assert refused.status_code == 409, refused.text
    assert "another session" in refused.json()["detail"]
    assert _archives(world) == []
    done = world.client.post(f"/api/steps/{step_id}/done", json={"session_id": SESSION_B}, headers=world.user())
    assert done.status_code == 200, done.text
    assert [archive["session_id"] for archive in _archives(world)] == [SESSION_B]


def test_a_put_that_moves_the_step_cannot_land_between_done_s_check_and_its_close(world: World, monkeypatch) -> None:
    # Done checked the step's session against the page's, then a put moved the step to B before the close: Done
    # would close B's step and archive A. The check and the close are one transaction, so the put cannot land
    # between them (regression check).
    world.cache.sessions = [*world.cache.sessions, make_session(SESSION_B, pid=None)]
    step_id = _step(world, "review", SESSION_A)
    moved: list[str] = []
    check = store.check_seen_target

    def check_then_move(step: int, session_id: str | None, seen: str | None) -> None:
        check(step, session_id, seen)
        other = sqlite3.connect(world.db_path, timeout=0)
        try:
            other.execute("UPDATE steps SET session_id = ? WHERE id = ?", (SESSION_B, step))
            other.commit()
            moved.append("moved")
        except sqlite3.OperationalError as error:
            moved.append(str(error))
        finally:
            other.close()

    monkeypatch.setattr(store, "check_seen_target", check_then_move)
    done = world.client.post(f"/api/steps/{step_id}/done", json={"session_id": SESSION_A}, headers=world.user())
    assert done.status_code == 200, done.text
    assert moved == ["database is locked"]
    assert [archive["session_id"] for archive in _archives(world)] == [SESSION_A]
