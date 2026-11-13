"""Fire and forget (SPEC CONTROL-24): a to-do added with Add's Fire and forget gets a session that works to the end on
its own, asks only what blocks it, and closes the item itself with a note, which the user's next step names."""

from __future__ import annotations

from typing import Any

from cockpit import db, sweeper
from tests.conftest import World
from tests.test_complete_task import _agent
from tests.test_delivery import send


def _add(world: World, fire_and_forget: bool) -> dict[str, Any]:
    body = {"text": "Fix the CSV exporter\nIt drops the last row", "part": None, "pinned": False, "fire_and_forget": fire_and_forget}
    response = world.client.post("/api/items/add", json=body, headers=world.user())
    assert response.status_code == 200, response.text
    return response.json()


def _put(world: World, key: str, **fields: Any):
    return _agent(world).put("/api/items", json={"key": key} | fields, headers=world.agent())


def _item(world: World, item_id: int) -> dict[str, Any]:
    return next(entry for entry in world.client.get("/api/state").json()["items"] if entry["id"] == item_id)


def test_its_session_learns_to_work_alone_and_close_the_item_itself(world: World) -> None:
    added = _add(world, True)
    assert added["fire_and_forget"] == 1
    send(world, {"item_id": added["id"], "new_cwd": str(world.checkout)}, added["summary"])
    prompt = world.runner.calls[0][0][-1]
    assert "Sam added this as fire and forget: do the work to the end on your own." in prompt
    assert "Put a step only when the work cannot go on without Sam" in prompt
    assert '"stage": "done" and a one-line "note"' in prompt and f"(key todo-{added['id']})" in prompt


def test_a_plain_to_do_s_session_hears_nothing_of_it(world: World) -> None:
    added = _add(world, False)
    assert added["fire_and_forget"] == 0
    send(world, {"item_id": added["id"], "new_cwd": str(world.checkout)}, added["summary"])
    assert "fire and forget" not in world.runner.calls[0][0][-1]
    # Its close still needs a finish step (CONTROL-19).
    refused = _put(world, added["key"], stage="done", note="Fixed the CSV exporter")
    assert refused.status_code == 400 and "need complete" in refused.json()["detail"]


def test_its_session_closes_it_with_a_note_that_the_next_step_names(world: World) -> None:
    added = _add(world, True)
    key = added["key"]
    refused = _put(world, key, stage="done")
    assert refused.status_code == 400 and "fire and forget" in refused.json()["detail"]
    assert _item(world, added["id"])["stage"] is None
    put = _put(world, key, stage="done", note="CSV exports include the final task")
    assert put.status_code == 200, put.text
    body = put.json()
    assert (body["stage"], body["finish_step"], body["merged"], body["alone"]) == ("done", None, None, True)
    state = world.client.get("/api/state").json()
    assert [(entry["item_id"], entry["pull"], entry["note"]) for entry in state["merged_closes"]] == [
        (added["id"], None, "CSV exports include the final task"),
    ]
    assert [(entry["id"], entry["note"]) for entry in state["closed_recently"]["items"]] == [
        (added["id"], "CSV exports include the final task"),
    ]


def test_a_new_session_from_its_blocker_step_learns_it_too(world: World) -> None:
    added = _add(world, True)
    step = _agent(world).post("/api/steps", json={"item_key": added["key"], "need": "decide:format", "ask": "Include a header row?"},
                              headers=world.agent()).json()["step"]
    send(world, {"item_id": added["id"], "step_id": step["id"], "new_cwd": str(world.checkout)}, "Include it")
    assert "Sam added this as fire and forget" in world.runner.calls[0][0][-1]


def test_its_stalled_work_wakes_the_sweeper_at_normal_priority(world: World) -> None:
    added = _add(world, True)
    conn = db.connect(world.db_path)
    try:
        since = "2026-01-31T04:00:00Z"
        sweeper.note_stalls(conn, [{"id": added["id"], "priority": "normal", "fire_and_forget": 1, "stalled_since": since}])
        events = db.rows(conn.execute("SELECT item_id FROM events WHERE kind = 'item.stalled'").fetchall())
    finally:
        conn.close()
    assert [event["item_id"] for event in events] == [added["id"]]


def test_an_open_question_keeps_it_for_the_user(world: World) -> None:
    """A step still open on it is the user's: the close goes the usual way, through a finish step."""
    added = _add(world, True)
    step = _agent(world).post("/api/steps", json={"item_key": added["key"], "need": "decide:format", "ask": "Include a header row?"},
                              headers=world.agent())
    assert step.status_code == 200, step.text
    refused = _put(world, added["key"], stage="done", note="Fixed the CSV exporter")
    assert refused.status_code == 400 and "need complete" in refused.json()["detail"]
    assert world.client.post(f"/api/steps/{step.json()['step']['id']}/dismiss", json={}, headers=world.user()).status_code == 200
    assert _put(world, added["key"], stage="done", note="Fixed the CSV exporter").json()["alone"] is True
