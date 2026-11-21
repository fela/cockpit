"""Reopen on a finished task (SPEC CONTROL-34), with "reopen" in the setup's features: the user takes the task up again
as a message would (CONTROL-28) and its session gets a fixed message from them."""

from __future__ import annotations

import dataclasses
import json

import pytest

from cockpit import config, db, store
from tests.conftest import SESSION_A, SESSION_B, World
from tests.test_api import run_sql
from tests.test_closed_items import _complete, _put_step, _shown
from tests.test_complete_task import CLOSING, ITEM, _agent, _finish, _item, _item_state, _step


@pytest.fixture
def reopen_on(monkeypatch: pytest.MonkeyPatch) -> None:
    setup = config.setup()
    monkeypatch.setattr(config, "setup", lambda: dataclasses.replace(setup, features=("reopen",)))


def _take_up(world: World, item_id: int, seen: str | None) -> tuple[int, dict]:
    response = world.client.post(f"/api/items/{item_id}/take-up", json={"session_id": seen}, headers=world.user())
    return response.status_code, response.json()


def _paused(world: World) -> bool:
    return world.client.get("/api/state").json()["sweeps_paused"] is not None


def _pause_sweeps(world: World) -> None:
    conn = db.connect(world.db_path)
    try:
        with db.transaction(conn):
            db.kv_set(conn, "sweeps_paused", {"at": "2026-01-07T18:00:00Z", "away_since": "2026-01-07T16:00:00Z"})
    finally:
        conn.close()


def test_reopen_is_off_by_default(world: World) -> None:
    item_id = _complete(world)
    assert world.client.get("/api/state").json()["features"] == []
    status, body = _take_up(world, item_id, SESSION_A)
    assert status == 404 and "Reopen is off" in body["detail"]
    assert _item_state(world, item_id)["stage"] == "done"


@pytest.mark.usefixtures("reopen_on")
def test_reopen_takes_a_completed_task_back_to_busy_and_tells_its_session(world: World) -> None:
    item_id = _complete(world)
    assert world.client.get("/api/state").json()["features"] == ["reopen"]
    status, body = _take_up(world, item_id, SESSION_A)
    assert status == 200, body
    item = _item_state(world, item_id)
    assert (item["stage"], item["archived"], item["waiting_on"]) == ("building", 0, [])
    send = body["send"]
    # While the message is on its way, the task shows in Busy, also with its session stopped (the test's fake delivery
    # fails it, so its state is set as delivery would have it).
    run_sql(world, "UPDATE messages SET state = 'queued' WHERE id = ?", (send["id"],))
    assert _shown(world, item_id) == {"group": "busy", "finished": False, "counted": False}
    assert (send["author"], send["session_id"], send["item_id"], send["as_prompt"]) == ("user", SESSION_A, item_id, 1)
    # Fixed words with the numeric ID; the title, which an agent can write, stays out of the prompt.
    assert send["text"].startswith(f"I reopened item {item_id} on Cockpit, so it takes your steps again.")
    assert item["title"] not in send["text"]
    # Its session may put steps on it again.
    assert _put_step(world, "read:answer")[0] == 200
    conn = db.connect(world.db_path)
    try:
        event = conn.execute("SELECT actor, data FROM events WHERE kind = 'item.reopened' ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert event["actor"] == "user" and '"by": "reopen"' in event["data"]


@pytest.mark.usefixtures("reopen_on")
def test_reopen_with_no_session_sends_nothing_and_waits_for_a_next_move(world: World) -> None:
    item_id = _complete(world)
    run_sql(world, "UPDATE items SET sessions = '[]' WHERE id = ?", (item_id,))
    status, body = _take_up(world, item_id, None)
    assert status == 200 and body["send"] is None, body
    assert _item_state(world, item_id)["stage"] == "building"
    # The page's notice says Waiting, where it lands.
    assert _shown(world, item_id) == {"group": "waiting", "finished": False, "counted": False}


@pytest.mark.usefixtures("reopen_on")
@pytest.mark.parametrize(("seen", "now"), [(SESSION_A, [SESSION_B, SESSION_A]), (None, [SESSION_A])])
def test_reopen_is_refused_when_the_task_has_another_session_than_the_page_showed(
    world: World, seen: str | None, now: list[str],
) -> None:
    item_id = _complete(world)
    run_sql(world, "UPDATE items SET sessions = ? WHERE id = ?", (json.dumps(now), item_id))
    _pause_sweeps(world)
    status, body = _take_up(world, item_id, seen)
    assert status == 409 and "another session since your page showed it" in body["detail"]
    assert _item_state(world, item_id)["stage"] == "done"
    assert world.client.get("/api/items", params={"key": ITEM}).json()["messages"] == []
    # A refused press leaves paused sweeps paused.
    assert _paused(world)


@pytest.mark.usefixtures("reopen_on")
def test_reopen_is_refused_when_the_shown_session_handed_its_work_to_another(world: World) -> None:
    item_id = _complete(world)
    conn = db.connect(world.db_path)
    try:
        with db.transaction(conn):
            transfer = store.create_message(conn, "user", "Hand over", {"session_id": SESSION_B}, world.checkout)
            conn.execute(
                "INSERT INTO session_handovers (source_session_id, message_id, target_session_id, created_at)"
                " VALUES (?, ?, ?, ?)", (SESSION_A, transfer["id"], SESSION_B, db.now_iso()),
            )
    finally:
        conn.close()
    status, body = _take_up(world, item_id, SESSION_A)
    assert status == 409 and "handed its work to another session" in body["detail"]
    assert _item_state(world, item_id)["stage"] == "done"
    assert world.client.get("/api/items", params={"key": ITEM}).json()["messages"] == []


@pytest.mark.usefixtures("reopen_on")
def test_reopen_resumes_paused_sweeps(world: World) -> None:
    item_id = _complete(world)
    _pause_sweeps(world)
    assert _take_up(world, item_id, SESSION_A)[0] == 200
    assert not _paused(world)


@pytest.mark.usefixtures("reopen_on")
def test_reopen_on_an_open_task_is_refused(world: World) -> None:
    item_id = _item(world)
    status, body = _take_up(world, item_id, SESSION_A)
    assert status == 409 and "not finished" in body["detail"]


@pytest.mark.usefixtures("reopen_on")
def test_only_the_user_reopens(world: World) -> None:
    item_id = _complete(world)
    response = _agent(world).post(f"/api/items/{item_id}/take-up", json={"session_id": SESSION_A}, headers=world.agent())
    assert response.status_code in (401, 403), response.text
    assert _item_state(world, item_id)["stage"] == "done"


@pytest.mark.usefixtures("reopen_on")
def test_reopen_after_a_close_landed_is_refused_and_the_task_stays_closed(world: World) -> None:
    item_id = _complete(world)
    assert world.client.put("/api/items", json={"key": ITEM, "archived": True}, headers=world.user()).status_code == 200
    status, body = _take_up(world, item_id, SESSION_A)
    assert status == 409 and "closed as not needed" in body["detail"]
    assert _item_state(world, item_id)["archived"]


@pytest.mark.usefixtures("reopen_on")
def test_a_closed_task_s_refusal_keeps_its_sentence(world: World) -> None:
    item_id = _item(world)
    step = _step(world, "reply:1", [CLOSING])
    assert _finish(world, item_id, step["id"], "close", None, "not needed").status_code == 200
    status, detail = _put_step(world, "read:fj-comparison")
    assert status == 409 and detail.endswith("reopens a closed item.")
