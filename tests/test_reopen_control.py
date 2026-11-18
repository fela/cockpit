"""A finished task reopens without a message (SPEC CONTROL-33): the user's Reopen takes it back to Busy as a message
would (CONTROL-28) and tells its session, and a session that must still post on a completed task reopens it itself."""

from __future__ import annotations

from cockpit import db, store
from tests.conftest import SESSION_A, World
from tests.test_api import run_sql
from tests.test_closed_items import _complete, _put_step, _shown
from tests.test_complete_task import CLOSING, ITEM, _agent, _finish, _item, _item_state, _step


def test_reopen_takes_a_completed_task_back_to_busy_and_tells_its_session(world: World) -> None:
    item_id = _complete(world)
    response = world.client.post(f"/api/items/{item_id}/take-up", headers=world.user())
    assert response.status_code == 200, response.text
    item = _item_state(world, item_id)
    assert (item["stage"], item["archived"], item["waiting_on"]) == ("building", 0, [])
    send = response.json()["send"]
    # While Cockpit's message is on its way, the task shows in Busy, also with its session stopped (the test's fake
    # delivery fails it, so its state is set as delivery would have it).
    run_sql(world, "UPDATE messages SET state = 'queued' WHERE id = ?", (send["id"],))
    assert _shown(world, item_id) == {"group": "busy", "finished": False, "counted": False}
    assert send["author"] == "cockpit" and send["session_id"] == SESSION_A and send["item_id"] == item_id
    assert "reopened item" in send["text"] and "takes your steps again" in send["text"]
    # Its session may put steps on it again.
    assert _put_step(world, "read:answer")[0] == 200
    conn = db.connect(world.db_path)
    try:
        event = conn.execute("SELECT data FROM events WHERE kind = 'item.reopened' ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert '"by": "reopen"' in event["data"]


def test_reopen_on_an_open_task_is_refused(world: World) -> None:
    item_id = _item(world)
    response = world.client.post(f"/api/items/{item_id}/take-up", headers=world.user())
    assert response.status_code == 409
    assert "not finished" in response.json()["detail"]


def test_only_the_user_reopens(world: World) -> None:
    item_id = _complete(world)
    response = _agent(world).post(f"/api/items/{item_id}/take-up", headers=world.agent())
    assert response.status_code in (401, 403), response.text
    assert _item_state(world, item_id)["stage"] == "done"


def _agent_reopen(world: World, item_id: int, why: str) -> tuple[int, str]:
    response = _agent(world).post(f"/api/items/{item_id}/agent-reopen", json={"why": why}, headers=world.agent())
    return response.status_code, response.text


def test_a_session_reopens_a_completed_task_itself_and_posts_there_again(world: World) -> None:
    item_id = _complete(world)
    status, text = _agent_reopen(world, item_id, "the test run still posts here")
    assert status == 200, text
    item = _item_state(world, item_id)
    assert (item["stage"], item["archived"], item["waiting_on"], item["doing"]) == ("building", 0, [], "the test run still posts here")
    # It leaves Finished this week; the test's session is stopped, so the page lists it under Waiting, not Busy.
    assert _shown(world, item_id)["finished"] is False
    assert _put_step(world, "read:answer")[0] == 200
    conn = db.connect(world.db_path)
    try:
        event = conn.execute("SELECT actor, data FROM events WHERE kind = 'item.reopened' ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert event["actor"].startswith("agent:") and '"by": "agent"' in event["data"]


def test_a_session_says_why_it_reopens(world: World) -> None:
    item_id = _complete(world)
    assert _agent_reopen(world, item_id, "  ")[0] == 400
    assert _agent_reopen(world, item_id, "x" * 61)[0] == 400
    assert _item_state(world, item_id)["stage"] == "done"


def test_a_session_reopens_no_task_closed_as_not_needed(world: World) -> None:
    item_id = _item(world)
    step = _step(world, "reply:1", [CLOSING])
    assert _finish(world, item_id, step["id"], "close", None, "not needed").status_code == 200
    status, text = _agent_reopen(world, item_id, "the test run still posts here")
    assert status == 409 and "closed as not needed" in text, text
    assert _item_state(world, item_id)["archived"]


def test_a_session_s_reopen_of_an_open_task_is_refused(world: World) -> None:
    item_id = _item(world)
    status, text = _agent_reopen(world, item_id, "the test run still posts here")
    assert status == 409 and "not finished" in text, text


def test_the_page_uses_its_own_reopen(world: World) -> None:
    item_id = _complete(world)
    response = world.client.post(f"/api/items/{item_id}/agent-reopen", json={"why": "x"}, headers=world.user())
    assert response.status_code in (401, 403), response.text


def test_other_steps_on_a_completed_task_are_still_refused_and_point_to_the_reopen_command(world: World) -> None:
    _complete(world)
    status, detail = _put_step(world, "read:fj-comparison")
    assert status == 409
    assert "cockpit item reopen" in detail


def test_a_closed_task_s_refusal_names_no_reopen_command(world: World) -> None:
    item_id = _item(world)
    step = _step(world, "reply:1", [CLOSING])
    assert _finish(world, item_id, step["id"], "close", None, "not needed").status_code == 200
    status, detail = _put_step(world, "read:fj-comparison")
    assert status == 409 and "cockpit item reopen" not in detail


def test_reopen_plainly_with_no_session_sends_nothing(world: World) -> None:
    item_id = _complete(world)
    conn = db.connect(world.db_path)
    try:
        conn.execute("UPDATE items SET sessions = '[]' WHERE id = ?", (item_id,))
        conn.commit()
        assert store.reopen_plainly(conn, item_id, world.db_path.parent)["send"] is None
    finally:
        conn.close()
    assert _item_state(world, item_id)["stage"] == "building"


def test_reopen_after_a_close_landed_is_refused_and_the_task_stays_closed(world: World) -> None:
    item_id = _complete(world)
    assert world.client.put("/api/items", json={"key": ITEM, "archived": True}, headers=world.user()).status_code == 200
    response = world.client.post(f"/api/items/{item_id}/take-up", headers=world.user())
    assert response.status_code == 409 and "closed as not needed" in response.json()["detail"]
    assert _item_state(world, item_id)["archived"]


def test_a_session_s_reopen_waits_for_the_user_s_timer_on_the_task(world: World) -> None:
    item_id = _item(world)
    step = _step(world, "reply:1", [CLOSING])
    assert world.client.post(f"/api/steps/{step['id']}/timer/start", headers=world.user()).status_code == 200
    run_sql(world, "UPDATE items SET stage = 'done' WHERE id = ?", (item_id,))
    status, text = _agent_reopen(world, item_id, "the test run still posts here")
    assert status == 409 and "timer runs" in text, text
    assert _item_state(world, item_id)["stage"] == "done"
