"""A scheduled run's session closes the run's own item itself (SPEC CONTROL-33): a run with nothing to report puts stage
done with a one-line note, on the terms a fire-and-forget to-do's close has (CONTROL-24), and the user gets no card."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from cockpit import db
from tests.conftest import SESSION_A, World
from tests.test_api import hand_off
from tests.test_complete_task import _agent
from tests.test_schedules import MONDAY_RUN, check, weekly
from tests.test_schedules import put as put_schedule

# The item `schedules.start_run` makes for the weekly schedule's Monday 16 Nov run.
KEY = "note:schedule-app-review-2026-11-16-0900"


def _put(world: World, key: str = KEY, **fields: Any):
    return _agent(world).put("/api/items", json={"key": key} | fields, headers=world.agent())


def _run(world: World) -> int:
    """The scheduler starts the run, and the run's session joins its item."""
    put_schedule(world, weekly(world))
    assert len(check(world, MONDAY_RUN)) == 1
    joined = _put(world, stage="building", sessions=[SESSION_A])
    assert joined.status_code == 200, joined.text
    return joined.json()["id"]


def _refused(response) -> bool:
    return response.status_code == 400 and "need complete" in response.json()["detail"]


def test_its_session_closes_it_with_a_note_that_the_next_step_names(world: World) -> None:
    item_id = _run(world)
    refused = _put(world, stage="done")
    assert refused.status_code == 400 and "recurring run" in refused.json()["detail"]
    put = _put(world, stage="done", note="Nothing to report today")
    assert put.status_code == 200, put.text
    body = put.json()
    assert (body["stage"], body["finish_step"], body["merged"], body["alone"]) == ("done", None, None, True)
    state = world.client.get("/api/state").json()
    assert [(entry["item_id"], entry["pull"], entry["recurring"], entry["note"]) for entry in state["merged_closes"]] == [
        (item_id, None, True, "Nothing to report today"),
    ]


def test_an_open_step_keeps_it_for_the_user(world: World) -> None:
    item_id = _run(world)
    step = _agent(world).post("/api/steps", json={"item_key": KEY, "need": "read:review", "ask": "two hooks changed; read them"},
                              headers=world.agent())
    assert step.status_code == 200, step.text
    assert _refused(_put(world, stage="done", note="Nothing to report today"))
    assert world.client.post(f"/api/steps/{step.json()['step']['id']}/dismiss", json={}, headers=world.user()).status_code == 200
    closed = _put(world, stage="done", note="Nothing to report today").json()
    assert (closed["id"], closed["stage"], closed["alone"]) == (item_id, "done", True)


def test_an_ordinary_item_still_needs_a_finish_step(world: World) -> None:
    made = _put(world, "note:fix-forms", title="Fix forms", stage="building", sessions=[SESSION_A])
    assert made.status_code == 200, made.text
    assert _refused(_put(world, "note:fix-forms", stage="done", note="Nothing to report today"))


def test_a_recurring_item_that_several_runs_share_still_needs_a_finish_step(world: World) -> None:
    key = "note:security-review"
    made = _put(world, key, title="Security review", stage="building", recurring=True, sessions=[SESSION_A])
    assert made.status_code == 200, made.text
    assert _refused(_put(world, key, stage="done", note="Nothing to report today"))


def test_only_the_scheduler_makes_a_run_s_item(world: World) -> None:
    # An item made under a run's future key would become that run's when it starts, so an agent cannot make one.
    for key in (KEY, "note:schedule-security-review"):
        made = _put(world, key, title="Security review", stage="building", recurring=True, sessions=[SESSION_A])
        assert made.status_code == 404 and "only a scheduled prompt" in made.json()["detail"]
    item_id = _run(world)
    assert _put(world, title="App review today").json()["id"] == item_id


def test_a_run_s_item_needs_a_finish_step_once_the_next_run_started(world: World) -> None:
    _run(world)
    assert len(check(world, MONDAY_RUN + timedelta(days=7))) == 1
    assert _refused(_put(world, stage="done", note="Nothing to report today"))


def _finish_step(world: World) -> int:
    body = {"item_key": KEY, "need": "complete", "ask": "Review found nothing; close it?",
            "body": "No changes since yesterday.", "size": "small", "answers": [{"text": "Task done", "closes": True}]}
    response = _agent(world).post("/api/steps", json=body, headers=world.agent())
    assert response.status_code == 200, response.text
    return response.json()["step"]["id"]


def _status(world: World, step_id: int) -> str:
    item = world.client.get("/api/items", params={"key": KEY}).json()
    return next(step["status"] for step in item["steps"] if step["id"] == step_id)


def test_an_idle_finish_step_closes_with_it(world: World) -> None:
    _run(world)
    finish = _finish_step(world)
    closed = _put(world, stage="done", note="Nothing to report today").json()
    assert (closed["stage"], closed["alone"], _status(world, finish)) == ("done", True, "done")


def test_a_finish_step_the_user_handed_back_or_is_timing_keeps_it_for_the_user(world: World) -> None:
    _run(world)
    finish = _finish_step(world)
    assert world.client.post(f"/api/steps/{finish}/timer/start", headers=world.user()).status_code == 200
    timed = _put(world, stage="done", note="Nothing to report today").json()
    assert (timed["stage"], timed["alone"]) == ("building", False)
    assert world.client.post(f"/api/steps/{finish}/timer/pause", headers=world.user()).status_code == 200
    hand_off(world, finish, text="Also check the weekly totals")
    handed = _put(world, stage="done", note="Nothing to report today").json()
    assert (handed["stage"], handed["alone"], _status(world, finish)) == ("building", False, "open")


def test_the_close_line_keeps_its_label_when_the_item_s_flag_changes_later(world: World) -> None:
    item_id = _run(world)
    assert _put(world, stage="done", note="Nothing to report today").status_code == 200
    conn = db.connect(world.db_path)
    try:
        conn.execute("UPDATE items SET recurring = 0, fire_and_forget = 1 WHERE id = ?", (item_id,))
        conn.commit()
    finally:
        conn.close()
    closes = world.client.get("/api/state").json()["merged_closes"]
    assert [(entry["item_id"], entry["recurring"]) for entry in closes] == [(item_id, True)]
