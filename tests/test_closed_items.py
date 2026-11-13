"""A closed task takes no new ask from an agent (SPEC CONTROL-27): a step put on an item the user completed or
closed is refused, and so is an item put that would move a completed item back to another stage."""

from __future__ import annotations

import json

import pytest

from cockpit import db, store
from tests.conftest import SESSION_A, World, make_session
from tests.test_add_more import add_more
from tests.test_api import add_item, put_item, run_sql, state_item
from tests.test_complete_task import (
    CLOSING,
    ITEM,
    _agent,
    _finish,
    _item,
    _item_state,
    _step,
    _step_state,
    live,
    message,
    stops_and_resumes,
)
from tests.test_page_comments import WORDS, comment, setup


def _complete(world: World) -> int:
    """The user presses the closing answer on a step: the task is done."""
    item_id = _item(world)
    step = _step(world, "reply:1", [CLOSING])
    assert _finish(world, item_id, step["id"], "complete", None, "done").status_code == 200
    assert _item_state(world, item_id)["stage"] == "done"
    return item_id


def _put_step(world: World, need: str) -> tuple[int, str]:
    body = {"item_key": ITEM, "need": need, "ask": "read the export guide", "size": "small", "changed": "Reworded."}
    response = _agent(world).post("/api/steps", json=body, headers=world.agent())
    return response.status_code, response.json().get("detail", "")


def _open_steps(world: World) -> list[str]:
    item = world.client.get("/api/items", params={"key": ITEM}).json()
    return [step["need"] for step in item["steps"] if step["status"] == "open" and not step["withdrawn_at"]]


def test_an_agent_s_step_on_a_completed_item_is_refused(world: World) -> None:
    item_id = _complete(world)
    status, detail = _put_step(world, "read:export-guide")
    assert status == 409
    assert f"Item {item_id}" in detail and "is closed" in detail and "completed it" in detail
    assert "new item" in detail and "asked on" in detail
    assert _open_steps(world) == []
    assert _item_state(world, item_id)["stage"] == "done"
    # The need it closed answers the same way, not with a quiet skip.
    assert _put_step(world, "reply:1")[0] == 409


def test_an_agent_s_step_on_an_item_the_user_closed_is_refused(world: World) -> None:
    item_id = _item(world)
    step = _step(world, "reply:1", [CLOSING])
    assert _finish(world, item_id, step["id"], "close", None, "not needed").status_code == 200
    assert _item_state(world, item_id)["archived"]
    status, detail = _put_step(world, "read:later")
    assert status == 409 and "closed it" in detail


def test_an_agent_s_stage_put_does_not_reopen_a_completed_item(world: World) -> None:
    item_id = _complete(world)
    response = _agent(world).put("/api/items", json={"key": ITEM, "stage": "building"}, headers=world.agent())
    assert response.status_code == 409, response.text
    assert "back to stage building" in response.json()["detail"]
    assert _item_state(world, item_id)["stage"] == "done"
    # A summary or a link on the finished task still goes in, and leaves it done.
    response = _agent(world).put("/api/items", json={"key": ITEM, "summary": "Merged; live on prod"}, headers=world.agent())
    assert response.status_code == 200, response.text
    assert _item_state(world, item_id)["stage"] == "done"
    # A stage of null clears done as surely as another stage does.
    response = _agent(world).put("/api/items", json={"key": ITEM, "stage": None}, headers=world.agent())
    assert response.status_code == 409, response.text
    response = _agent(world).patch(f"/api/items/{item_id}", json={"stage": "prod"}, headers=world.agent())
    assert response.status_code == 409, response.text
    assert _item_state(world, item_id)["stage"] == "done"


def test_a_step_still_open_on_a_completed_item_is_still_answered_on(world: World) -> None:
    """The user can move an item to done from the stage menu with a step still open: its session and the sweeper
    still update that step, and only a new ask is refused."""
    item_id = _item(world)
    step = _step(world, "reply:1", [CLOSING])
    response = world.client.put("/api/items", json={"key": ITEM, "stage": "done"}, headers=world.user())
    assert response.status_code == 200, response.text
    body = {"item_key": ITEM, "need": "reply:1", "ask": "Is the reply out yet?"}
    response = _agent(world).post("/api/steps", json=body, headers=world.agent())
    assert response.status_code == 200, response.text
    assert response.json()["step"]["id"] == step["id"]
    assert _put_step(world, "read:new")[0] == 409
    assert _item_state(world, item_id)["stage"] == "done"


def test_the_user_s_own_writes_on_a_completed_item_go_through(world: World) -> None:
    item_id = _complete(world)
    response = world.client.put("/api/items", json={"key": ITEM, "stage": "building"}, headers=world.user())
    assert response.status_code == 200, response.text
    # Reopened by the user, the task takes the agent's step again.
    assert _put_step(world, "read:export-guide")[0] == 200
    assert _item_state(world, item_id)["stage"] == "building"



def _message(world: World, item_id: int, headers: dict[str, str]):
    body = {"text": "One more thing", "target": {"item_id": item_id, "session_id": SESSION_A}}
    client = world.client if "authorization" not in headers else _agent(world)
    return client.post("/api/messages", json=body, headers=headers)


def _shown(world: World, item_id: int) -> dict:
    state = world.client.get("/api/state").json()
    return {
        "group": next(item["group"] for item in state["items"] if item["id"] == item_id),
        "finished": item_id in [row["id"] for row in state["closed_recently"]["items"]],
        "counted": item_id in state["closed_by_user"]["finished"] + state["closed_by_user"]["archived"],
    }


def _message_is(world: World, item_id: int, state: str, sent_at: str | None) -> None:
    """Delivery's part, which the test's fake session does not play."""
    run_sql(world, "UPDATE messages SET state = ?, sent_at = ? WHERE item_id = ? AND author = 'user'", (state, sent_at, item_id))


def test_the_user_s_message_on_a_completed_task_takes_it_back_to_busy(world: World) -> None:
    """SPEC CONTROL-28. Its session may then put steps on it again, and Undo no longer puts the old finish back."""
    item_id = _complete(world)
    assert _message(world, item_id, world.user()).status_code == 200
    _message_is(world, item_id, "queued", None)
    item = _item_state(world, item_id)
    assert (item["stage"], item["archived"], item["waiting_on"]) == ("building", 0, [])
    assert _shown(world, item_id) == {"group": "busy", "finished": False, "counted": False}
    assert _put_step(world, "read:answer")[0] == 200
    assert world.client.post(f"/api/items/{item_id}/reopen", headers=world.user()).status_code == 409


def test_the_user_s_message_on_a_task_closed_as_not_needed_takes_it_back_to_busy(world: World) -> None:
    item_id = _item(world)
    step = _step(world, "reply:1", [CLOSING])
    assert _finish(world, item_id, step["id"], "close", None, "not needed").status_code == 200
    assert _message(world, item_id, world.user()).status_code == 200
    _message_is(world, item_id, "queued", None)
    item = _item_state(world, item_id)
    assert (item["stage"], item["archived"]) == ("building", 0)
    assert "archived" not in item["user_fields"]
    assert _shown(world, item_id) == {"group": "busy", "finished": False, "counted": False}
    assert _put_step(world, "read:answer")[0] == 200


def test_it_shows_in_busy_from_send_on_though_its_session_stopped_long_ago(world: World) -> None:
    """Without the message on its way, the session's old last change would put the task in Waiting as idle."""
    item_id = _complete(world)
    world.cache.sessions = [make_session(SESSION_A, pid=None, state="stopped")]
    assert _message(world, item_id, world.user()).status_code == 200
    _message_is(world, item_id, "queued", None)
    assert _shown(world, item_id)["group"] == "busy"
    _message_is(world, item_id, "sending", None)
    assert _shown(world, item_id)["group"] == "busy"
    # Sent just now: the session has yet to show that it started.
    _message_is(world, item_id, "sent", db.now_iso())
    assert _shown(world, item_id)["group"] == "busy"
    # Sent a while ago, or not sent, with the session still stopped: the task shows as idle, as any other would.
    _message_is(world, item_id, "sent", "2026-01-04T09:00:00Z")
    assert _shown(world, item_id)["group"] == "waiting"
    _message_is(world, item_id, "failed", None)
    assert _shown(world, item_id)["group"] == "waiting"


def test_a_new_session_on_its_way_to_a_task_with_no_session_shows_in_busy(world: World) -> None:
    item_id = _complete(world)
    run_sql(world, "UPDATE items SET sessions = '[]' WHERE id = ?", (item_id,))
    assert _message(world, item_id, world.user()).status_code == 200
    _message_is(world, item_id, "queued", None)
    assert _shown(world, item_id)["group"] == "busy"


def test_page_comments_sent_to_the_session_take_a_finished_task_back_to_busy(world: World) -> None:
    page, step = setup(world)
    item_id = page["item_id"]
    loose = comment(world, page, None, WORDS, "Check March against February").json()
    assert _finish(world, item_id, step["id"], "complete", None, "done").status_code == 200
    assert _item_state(world, item_id)["stage"] == "done"
    sent = world.client.post(f"/api/item-pages/{page['id']}/comments/send",
                             json={"comment_ids": [loose["id"]], "text": ""}, headers=world.user())
    assert sent.status_code == 200, sent.text
    assert _item_state(world, item_id)["stage"] == "building"


def test_an_agent_s_message_leaves_the_task_finished(world: World) -> None:
    item_id = _complete(world)
    assert _message(world, item_id, world.agent()).status_code == 200
    assert _item_state(world, item_id)["stage"] == "done"


def test_a_message_not_written_to_the_task_leaves_it_finished(world: World) -> None:
    """Ask's answer handed on to the session (sidechat) goes under the user's name through `create_message`."""
    item_id = _complete(world)
    conn = db.connect(world.db_path)
    try:
        with db.transaction(conn):
            target = {"item_id": item_id, "step_id": None, "session_id": SESSION_A, "new_cwd": None}
            store.create_message(conn, "user", "From Ask", target, world.settings.checkouts_dir)
    finally:
        conn.close()
    assert _item_state(world, item_id)["stage"] == "done"


def test_handing_on_a_step_still_open_on_a_completed_task_takes_it_back_to_busy(world: World) -> None:
    item_id = _item(world)
    step = _step(world, "reply:1", [CLOSING])
    assert world.client.put("/api/items", json={"key": ITEM, "stage": "done"}, headers=world.user()).status_code == 200
    body = {"text": "Rebase it", "session_id": SESSION_A}
    response = world.client.post(f"/api/steps/{step['id']}/hand-off", json=body, headers=world.user())
    assert response.status_code == 200, response.text
    assert _item_state(world, item_id)["stage"] == "building"
    assert _shown(world, item_id)["group"] == "busy"


def test_a_message_on_an_open_task_leaves_its_stage(world: World) -> None:
    item_id = _item(world)
    assert _message(world, item_id, world.user()).status_code == 200
    item = _item_state(world, item_id)
    assert item["stage"] == "review" and item["waiting_on"]


def _on_a_finished_task(world: World, item_id: int, state: str) -> int:
    """A message of the user's that went before the task finished, in `state`."""
    conn = db.connect(world.db_path)
    try:
        target = {"item_id": item_id, "step_id": None, "session_id": SESSION_A, "new_cwd": None}
        message = store.create_message(conn, "user", "Earlier words", target, world.checkout.parent)
    finally:
        conn.close()
    run_sql(world, "UPDATE messages SET state = ? WHERE id = ?", (state, message["id"]))
    return message["id"]


def test_retrying_the_user_s_failed_message_takes_a_finished_task_back_to_busy(world: World) -> None:
    item_id = _complete(world)
    message_id = _on_a_finished_task(world, item_id, "failed")
    retried = world.client.post(f"/api/messages/{message_id}/retry", headers=world.user())
    assert retried.status_code == 200, retried.text
    assert retried.json()["reopened"] is True
    assert _item_state(world, item_id)["stage"] == "building"


def test_retrying_an_ask_s_answer_handed_on_leaves_the_task_finished(world: World) -> None:
    """CONTROL-28: an Ask's answer handed on to the session is no new words of the user's."""
    item_id = _complete(world)
    message_id = _on_a_finished_task(world, item_id, "failed")
    run_sql(
        world,
        "INSERT INTO side_chats (item_id, session_id, question, state, asked_at, sent_message_id)"
        " VALUES (?, ?, 'q', 'answered', '2026-02-04T00:00:00Z', ?)",
        (item_id, SESSION_A, message_id),
    )
    retried = world.client.post(f"/api/messages/{message_id}/retry", headers=world.user())
    assert retried.status_code == 200, retried.text
    assert retried.json()["reopened"] is False
    assert _item_state(world, item_id)["stage"] == "done"


def test_retrying_the_finish_s_own_last_message_leaves_the_task_finished(world: World) -> None:
    item_id = _item(world)
    step = _step(world, "reply:1", [CLOSING])
    finished = _finish(world, item_id, step["id"], "complete", None, "done", note="Thanks, that is all")
    assert finished.status_code == 200, finished.text
    conn = db.connect(world.db_path)
    try:
        message_id = store.get_finish(conn, item_id)["message_id"]
    finally:
        conn.close()
    run_sql(world, "UPDATE messages SET state = 'failed', take_back_until = NULL WHERE id = ?", (message_id,))
    retried = world.client.post(f"/api/messages/{message_id}/retry", headers=world.user())
    assert retried.status_code == 200, retried.text
    assert retried.json()["reopened"] is False
    assert _item_state(world, item_id)["stage"] == "done"


def _reopened_events(world: World, item_id: int) -> int:
    conn = db.connect(world.db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'item.reopened' AND item_id = ?", (item_id,)).fetchone()[0]
    finally:
        conn.close()


def test_a_finish_s_own_last_message_is_no_reopening(world: World) -> None:
    """Moved to done from the stage menu with a step still open, then completed with a last message: it stays done since
    the first time, with no reopening in its history."""
    item_id = _item(world)
    step = _step(world, "reply:1", [CLOSING])
    assert world.client.put("/api/items", json={"key": ITEM, "stage": "done"}, headers=world.user()).status_code == 200
    done_at = _item_state(world, item_id)["done_at"]
    finished = _finish(world, item_id, step["id"], "complete", None, "done", note="Thanks, that is all")
    assert finished.status_code == 200, finished.text
    after = _item_state(world, item_id)
    assert (after["stage"], after["done_at"]) == ("done", done_at)
    assert _reopened_events(world, item_id) == 0


def test_adding_more_to_a_finished_to_do_takes_it_back_to_busy(world: World) -> None:
    item = add_item(world, "Test list sharing")
    world.cache.sessions = [make_session(SESSION_A, pid=None, state="stopped")]
    run_sql(world, "UPDATE items SET sessions = ?, stage = 'done' WHERE id = ?", (f'["{SESSION_A}"]', item["id"]))
    added = add_more(world, item["id"], "and on a small sample")
    assert added.status_code == 200, added.text
    assert _item_state(world, item["id"])["stage"] == "building"
    _message_is(world, item["id"], "queued", None)
    assert _shown(world, item["id"])["group"] == "busy"


@pytest.mark.parametrize("joined", [True, False])
def test_a_follow_up_takes_a_task_finished_since_the_hand_off_back_to_busy(world: World, joined: bool) -> None:
    item_id = _item(world)
    step = _step(world, "reply:1", [CLOSING])
    handed = world.client.post(f"/api/steps/{step['id']}/hand-off", json={"text": "Merge it"}, headers=world.user()).json()
    if not joined:
        run_sql(world, "UPDATE messages SET state = 'sent', take_back_until = NULL WHERE id = ?", (handed["id"],))
    run_sql(world, "UPDATE items SET stage = 'done' WHERE id = ?", (item_id,))
    added = world.client.post(f"/api/messages/{handed['id']}/follow-up", json={"text": "and delete it"}, headers=world.user())
    assert added.status_code == 200, added.text
    assert added.json()["joined"] is joined
    assert added.json()["reopened"] is True
    assert _item_state(world, item_id)["stage"] == "building"


def test_a_follow_up_on_an_open_task_says_it_reopened_nothing(world: World) -> None:
    """The page may still draw the task as finished when the server finds it open, and then says nothing about Busy."""
    _item(world)
    step = _step(world, "reply:1", [CLOSING])
    handed = world.client.post(f"/api/steps/{step['id']}/hand-off", json={"text": "Merge it"}, headers=world.user()).json()
    added = world.client.post(f"/api/messages/{handed['id']}/follow-up", json={"text": "and delete it"}, headers=world.user())
    assert added.status_code == 200, added.text
    assert added.json()["reopened"] is False


def test_a_reopening_records_why_it_set_the_finish_s_message_aside(world: World, monkeypatch) -> None:
    """A message to the finish's own session sets Cockpit's message aside first, when it brings the session back; a
    reopening by any other session's message does so here, and the history says why."""
    stops_and_resumes(world, live("working", "busy"), monkeypatch)
    item_id = _item(world)
    step = _step(world, "complete", [])
    told = _finish(world, item_id, step["id"], "complete", None, "").json()["send"]["id"]
    assert message(world, told)["state"] == "queued"
    conn = db.connect(world.db_path)
    try:
        assert store.reopen_for_message(conn, item_id) is True
        event = conn.execute("SELECT data FROM events WHERE kind = 'message.dismissed' ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert message(world, told)["dismiss_reason"] == "Sam took the item up again with a message"
    assert json.loads(event["data"])["reason"] == "Sam took the item up again with a message"


def test_an_ask_a_close_left_open_goes_so_the_task_is_in_busy(world: World) -> None:
    """Close item on the row leaves its steps open. The message is the new ask; the session can put the old one again."""
    item_id = _item(world)
    step = _step(world, "reply:1", [CLOSING])
    assert world.client.put("/api/items", json={"key": ITEM, "archived": True}, headers=world.user()).status_code == 200
    assert _message(world, item_id, world.user()).status_code == 200
    _message_is(world, item_id, "queued", None)
    assert _shown(world, item_id)["group"] == "busy"
    assert _step_state(world, step["id"])["withdrawn_at"] is not None
    assert _put_step(world, "reply:1")[0] == 200


def test_a_message_stuck_on_its_way_for_long_shows_its_session_s_stall_again(world: World) -> None:
    item_id = put_item(world, stage="building", sessions=[SESSION_A])["id"]
    world.cache.sessions = [make_session(SESSION_A, pid=None, state="stopped")]
    assert state_item(world, item_id)["stalled_since"] is not None
    message_id = _on_a_finished_task(world, item_id, "queued")
    assert state_item(world, item_id)["stalled_since"] is None
    run_sql(world, "UPDATE messages SET created_at = '2026-01-04T09:00:00Z' WHERE id = ?", (message_id,))
    assert state_item(world, item_id)["stalled_since"] is None, "its item changed just now"
    run_sql(world, "UPDATE items SET updated_at = '2026-01-04T09:00:00Z' WHERE id = ?", (item_id,))
    assert state_item(world, item_id)["stalled_since"] is not None


def test_a_retried_old_message_keeps_the_reopened_task_in_busy_until_it_goes(world: World) -> None:
    item_id = put_item(world, stage="building", sessions=[SESSION_A])["id"]
    world.cache.sessions = [make_session(SESSION_A, pid=None, state="stopped")]
    message_id = _on_a_finished_task(world, item_id, "failed")
    run_sql(world, "UPDATE messages SET created_at = '2026-01-04T09:00:00Z' WHERE id = ?", (message_id,))
    run_sql(world, "UPDATE items SET stage = 'done', updated_at = '2026-01-04T09:00:00Z' WHERE id = ?", (item_id,))
    assert world.client.post(f"/api/messages/{message_id}/retry", headers=world.user()).status_code == 200
    run_sql(world, "UPDATE messages SET state = 'queued' WHERE id = ?", (message_id,))
    shown = state_item(world, item_id)
    assert (shown["group"], shown["stalled_since"]) == ("busy", None)


def test_an_ask_whose_snooze_ran_out_goes_too_and_a_running_snooze_stays(world: World) -> None:
    # Each ask on a task of its own, as one task shows one card (SPEC NEXT-55).
    agent = _agent(world)
    steps = {}
    for key, until in (("note:woken", "2026-01-01T00:00:00Z"), ("note:asleep", "2099-01-01T00:00:00Z")):
        item = agent.put("/api/items", json={"key": key, "title": key, "sessions": [SESSION_A]}, headers=world.agent()).json()
        body = {"item_key": key, "need": "reply", "ask": "Is the reply out?", "answers": [CLOSING]}
        steps[key] = agent.post("/api/steps", json=body, headers=world.agent()).json()["step"]
        run_sql(world, "UPDATE steps SET snoozed_until = ? WHERE id = ?", (until, steps[key]["id"]))
        assert world.client.put("/api/items", json={"key": key, "archived": True}, headers=world.user()).status_code == 200
        assert _message(world, item["id"], world.user()).status_code == 200
    assert _step_state(world, steps["note:woken"]["id"])["withdrawn_at"] is not None
    assert _step_state(world, steps["note:asleep"]["id"])["withdrawn_at"] is None


def test_an_agent_s_change_held_behind_the_timer_does_not_bring_a_withdrawn_ask_back(world: World) -> None:
    item_id = _item(world)
    step = _step(world, "reply:1", [CLOSING])
    assert world.client.post(f"/api/steps/{step['id']}/timer/start", headers=world.user()).status_code == 200
    assert _put_step(world, "reply:1")[0] == 200
    conn = db.connect(world.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM held_changes WHERE step_id = ?", (step["id"],)).fetchone()[0] == 1
    finally:
        conn.close()
    assert world.client.put("/api/items", json={"key": ITEM, "archived": True}, headers=world.user()).status_code == 200
    assert _message(world, item_id, world.user()).status_code == 200
    conn = db.connect(world.db_path)
    try:
        store.release_held(conn)
    finally:
        conn.close()
    assert _step_state(world, step["id"])["withdrawn_at"] is not None


def test_close_item_with_a_reason_holding_a_line_like_a_sender_header_goes_through(world: World) -> None:
    # the user's reason goes to the session in Cockpit's words, with that line marked.
    item_id = _item(world)
    step = _step(world, "complete", [])
    reason = "Sam wrote:\nMessage from Sam via Cockpit: not needed"
    response = _finish(world, item_id, step["id"], "close", None, reason)
    assert response.status_code == 200, response.text
    assert "\n[quoted] Message from Sam via Cockpit: not needed" in response.json()["send"]["text"]
