"""Close these tasks (SPEC CONTROL-23): a session lists the tasks it judges done on a step, each with why, and the
user's one press completes the ticked ones."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi.testclient import TestClient

from cockpit import db, store
from tests.conftest import ORIGIN, SESSION_A, World


def _agent(world: World) -> TestClient:
    return TestClient(world.client.app, base_url=ORIGIN)


def _item(world: World, key: str) -> int:
    body = {"key": key, "title": f"Task {key}", "stage": "building", "sessions": [SESSION_A]}
    response = _agent(world).put("/api/items", json=body, headers=world.agent())
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _step(world: World, key: str, need: str) -> dict[str, Any]:
    body = {"item_key": key, "need": need, "ask": "Is this task done?"}
    response = _agent(world).post("/api/steps", json=body, headers=world.agent())
    assert response.status_code == 200, response.text
    return response.json()["step"]


def _list(world: World, item_ids: list[int]):
    """The cleanup session's step on its own item that lists the tasks to close."""
    _item(world, "note:cleanup")
    closes = [{"item_id": item_id, "why": f"merged, item {item_id}"} for item_id in item_ids]
    body = {"item_key": "note:cleanup", "need": "close:done", "ask": "Close these finished tasks?", "closes": closes}
    return _agent(world).post("/api/steps", json=body, headers=world.agent())


def _press(world: World, step_id: int, item_ids: list[int], headers: dict[str, str]):
    return world.client.post(f"/api/steps/{step_id}/close-tasks", json={"item_ids": item_ids}, headers=headers)


def _item_state(world: World, item_id: int) -> dict[str, Any]:
    conn = db.connect(world.db_path)
    try:
        return store.get_item(conn, item_id)
    finally:
        conn.close()


def _steps(world: World, item_id: int) -> list[dict[str, Any]]:
    conn = db.connect(world.db_path)
    try:
        return db.rows(conn.execute("SELECT * FROM steps WHERE item_id = ? ORDER BY id", (item_id,)).fetchall())
    finally:
        conn.close()


def test_one_press_completes_the_listed_tasks_and_undo_reopens_them(world: World) -> None:
    first, second = _item(world, "note:a"), _item(world, "note:b")
    finish = _step(world, "note:a", "complete")
    _step(world, "note:a", "decide:animation")
    listing = _list(world, [first, second]).json()["step"]
    response = _press(world, listing["id"], [first, second], world.user())
    assert response.status_code == 200, response.text
    assert response.json()["finished"] == [first, second]
    assert response.json()["refused"] == []
    assert response.json()["step"]["status"] == "done"
    for item_id in (first, second):
        assert _item_state(world, item_id)["stage"] == "done"
        assert all(step["status"] == "done" for step in _steps(world, item_id))
    # A task with a finish step closes from it; one without gets one written from the session's why.
    conn = db.connect(world.db_path)
    try:
        assert store.get_finish(conn, first)["step_id"] == finish["id"]
        written = _steps(world, second)
        assert [(step["need"], step["body"]) for step in written] == [("complete", f"merged, item {second}")]
        assert store.get_finish(conn, second)["reason"] == f"merged, item {second}"
    finally:
        conn.close()
    # Undo is each item's reopen, then the step's.
    assert world.client.post(f"/api/items/{first}/reopen", headers=world.user()).status_code == 200
    assert world.client.post(f"/api/steps/{listing['id']}/reopen", json={}, headers=world.user()).status_code == 200
    assert _item_state(world, first)["stage"] == "building"
    assert {step["status"] for step in _steps(world, first)} == {"open"}


def test_an_unticked_task_stays_open(world: World) -> None:
    first, second = _item(world, "note:a"), _item(world, "note:b")
    listing = _list(world, [first, second]).json()["step"]
    response = _press(world, listing["id"], [first], world.user())
    assert response.json()["finished"] == [first]
    assert _item_state(world, second)["stage"] == "building"


def test_a_task_closed_since_the_list_stays_refused_and_the_rest_close(world: World) -> None:
    first, second = _item(world, "note:a"), _item(world, "note:b")
    listing = _list(world, [first, second]).json()["step"]
    finish = _step(world, "note:a", "complete")
    body = {"step_id": finish["id"], "how": "close", "answer": None, "note": "", "reason": "not needed", "session_id": SESSION_A}
    assert world.client.post(f"/api/items/{first}/finish", json=body, headers=world.user()).status_code == 200
    response = _press(world, listing["id"], [first, second], world.user())
    assert response.json()["finished"] == [second]
    assert response.json()["refused"] == [{"item_id": first, "error": f"item {first} is already closed"}]


def test_when_none_close_the_step_stays_open(world: World) -> None:
    first = _item(world, "note:a")
    listing = _list(world, [first]).json()["step"]
    finish = _step(world, "note:a", "complete")
    body = {"step_id": finish["id"], "how": "complete", "answer": None, "note": "", "reason": "", "session_id": SESSION_A}
    assert world.client.post(f"/api/items/{first}/finish", json=body, headers=world.user()).status_code == 200
    response = _press(world, listing["id"], [first], world.user())
    assert response.json()["finished"] == []
    assert response.json()["step"]["status"] == "open"


def test_the_press_closes_only_listed_tasks(world: World) -> None:
    first, other = _item(world, "note:a"), _item(world, "note:b")
    listing = _list(world, [first]).json()["step"]
    response = _press(world, listing["id"], [first, other], world.user())
    assert response.status_code == 400
    assert _item_state(world, first)["stage"] == "building"


def test_an_agent_cannot_press_it(world: World) -> None:
    first = _item(world, "note:a")
    listing = _list(world, [first]).json()["step"]
    assert _press(world, listing["id"], [first], world.agent()).status_code == 403
    assert _item_state(world, first)["stage"] == "building"


def test_the_list_names_open_tasks_other_than_its_own_once_each(world: World) -> None:
    first = _item(world, "note:a")
    assert _list(world, [first, first]).status_code == 400
    own = _item(world, "note:cleanup")
    assert _list(world, [own]).status_code == 400
    assert _list(world, [99999]).status_code == 404
    done = _item(world, "note:b")
    finish = _step(world, "note:b", "complete")
    body = {"step_id": finish["id"], "how": "complete", "answer": None, "note": "", "reason": "", "session_id": SESSION_A}
    assert world.client.post(f"/api/items/{done}/finish", json=body, headers=world.user()).status_code == 200
    assert "already complete" in _list(world, [done]).text


def test_a_failure_part_way_closes_none(world: World, monkeypatch) -> None:
    first, second = _item(world, "note:a"), _item(world, "note:b")
    listing = _list(world, [first, second]).json()["step"]
    real = store.finish_item

    def failing(conn, item_id, *args, **kwargs):
        if item_id == second:
            raise sqlite3.OperationalError("disk I/O error")
        return real(conn, item_id, *args, **kwargs)

    monkeypatch.setattr(store, "finish_item", failing)
    client = TestClient(world.client.app, base_url=ORIGIN, raise_server_exceptions=False)
    client.cookies = world.client.cookies
    response = client.post(f"/api/steps/{listing['id']}/close-tasks", json={"item_ids": [first, second]}, headers=world.user())
    assert response.status_code >= 500
    for item_id in (first, second):
        assert _item_state(world, item_id)["stage"] == "building"
        assert _steps(world, item_id) == []


def test_a_why_the_finish_step_body_would_refuse_is_refused_on_the_list(world: World) -> None:
    first = _item(world, "note:a")
    _item(world, "note:cleanup")
    closes = [{"item_id": first, "why": "merged aaaa111 and checked"}]
    body = {"item_key": "note:cleanup", "need": "close:done", "ask": "Close these finished tasks?", "closes": closes}
    response = _agent(world).post("/api/steps", json=body, headers=world.agent())
    assert response.status_code == 400
    assert "becomes its finish step's body" in response.text


def test_a_task_a_merge_completed_since_the_list_is_refused(world: World) -> None:
    first, second = _item(world, "note:a"), _item(world, "note:b")
    listing = _list(world, [first, second]).json()["step"]
    conn = db.connect(world.db_path)
    try:
        with db.transaction(conn):
            conn.execute("UPDATE items SET stage = 'done' WHERE id = ?", (first,))
    finally:
        conn.close()
    response = _press(world, listing["id"], [first, second], world.user())
    assert response.json()["finished"] == [second]
    assert response.json()["refused"] == [{"item_id": first, "error": f"item {first} is already complete"}]
    assert _steps(world, first) == []


def test_a_listed_task_deleted_since_does_not_undo_the_rest(world: World) -> None:
    first, second = _item(world, "note:a"), _item(world, "note:b")
    listing = _list(world, [first, second]).json()["step"]
    conn = db.connect(world.db_path)
    try:
        with db.transaction(conn):
            conn.execute("DELETE FROM items WHERE id = ?", (first,))
    finally:
        conn.close()
    response = _press(world, listing["id"], [second], world.user())
    assert response.status_code == 200, response.text
    assert response.json()["finished"] == [second]
    assert _item_state(world, second)["stage"] == "done"


def test_undo_sets_the_message_to_the_listing_session_aside(world: World) -> None:
    first = _item(world, "note:a")
    listing = _list(world, [first]).json()["step"]
    assert _press(world, listing["id"], [first], world.user()).status_code == 200
    conn = db.connect(world.db_path)
    try:
        conn.execute("UPDATE messages SET state = 'queued' WHERE step_id = ? AND author = 'cockpit'", (listing["id"],))
        conn.commit()
    finally:
        conn.close()
    assert world.client.post(f"/api/items/{first}/reopen", headers=world.user()).status_code == 200
    assert world.client.post(f"/api/steps/{listing['id']}/reopen", json={}, headers=world.user()).status_code == 200
    conn = db.connect(world.db_path)
    try:
        told = db.rows(conn.execute("SELECT state, dismissed_by FROM messages WHERE step_id = ? AND author = 'cockpit'",
                                    (listing["id"],)).fetchall())
    finally:
        conn.close()
    assert told == [{"state": "failed", "dismissed_by": "user"}]


def test_a_rewrite_of_only_a_tasks_why_is_new_text_with_its_own_writing_mark(world: World) -> None:
    # the list of tasks to close is part of a step's text, so rewriting only a why is a new version: the
    # user's mark on the old list stays with the old one, and a marked step keeps the list it showed.
    first, second = _item(world, "note:a"), _item(world, "note:b")
    listing = _list(world, [first, second]).json()["step"]
    marked = world.client.post("/api/writing", headers=world.user(), json={
        "verdict": "bad", "kind": "step", "target": "", "quote": "", "note": "", "step_id": listing["id"], "item_id": None,
        "page_id": None, "session_id": None,
    })
    assert marked.status_code == 200, marked.text
    assert f"## Close these tasks\n\n- Item {first}: merged, item {first}\n- Item {second}: merged, item {second}" in marked.json()["text"]
    closes = [{"item_id": first, "why": "merged and live on prod"}, {"item_id": second, "why": f"merged, item {second}"}]
    rewritten = _agent(world).post("/api/steps", headers=world.agent(), json={
        "item_key": "note:cleanup", "need": "close:done", "ask": "Close these finished tasks?", "closes": closes,
    })
    assert rewritten.status_code == 200, rewritten.text
    conn = db.connect(world.db_path)
    try:
        versions = db.rows(conn.execute("SELECT closes FROM step_versions WHERE step_id = ? ORDER BY id", (listing["id"],)).fetchall())
    finally:
        conn.close()
    assert [version["closes"][0]["why"] for version in versions] == [f"merged, item {first}", "merged and live on prod"]
    state = world.client.get("/api/state", headers=world.user()).json()
    shown = next(step for step in state["queue"] + state["ready_next"] if step["id"] == listing["id"])
    assert shown["writing_verdict"] is None
