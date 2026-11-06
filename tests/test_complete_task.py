"""Complete task and Close item (SPEC CONTROL-15): the user's one action that ends an item from its step."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from cockpit import db, delivery, store
from tests.conftest import ORIGIN, SESSION_A, SESSION_B, World
from tests.test_api import run_sql
from tests.test_delivery import live, message, stops_and_resumes
from tests.test_session_archive import _archives, _due, _turn_over

LOOK_AGAIN = "2026-02-05T00:00:00Z"

ITEM = "gh:example-org/app#20"
CLOSING = {"text": "I've sent it myself; close", "closes": True}


def _agent(world: World) -> TestClient:
    return TestClient(world.client.app, base_url=ORIGIN)


def _item(world: World) -> int:
    body = {
        "key": ITEM, "title": "PR 20", "stage": "review", "sessions": [SESSION_A],
        "waiting_on": [{"kind": "person", "until": LOOK_AGAIN, "who": "Ana"}],
    }
    response = _agent(world).put("/api/items", json=body, headers=world.agent())
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _step(world: World, need: str, answers: list[Any]) -> dict[str, Any]:
    body = {"item_key": ITEM, "need": need, "ask": "Is the reply out?", "answers": answers}
    response = _agent(world).post("/api/steps", json=body, headers=world.agent())
    assert response.status_code == 200, response.text
    return response.json()["step"]


def _finish(world: World, item_id: int, step_id: int, how: str, answer: str | None, reason: str, note: str = ""):
    body = {"step_id": step_id, "how": how, "answer": answer, "note": note, "reason": reason, "session_id": SESSION_A}
    return world.client.post(f"/api/items/{item_id}/finish", json=body, headers=world.user())


def _item_state(world: World, item_id: int) -> dict[str, Any]:
    conn = db.connect(world.db_path)
    try:
        return store.get_item(conn, item_id)
    finally:
        conn.close()


def _step_state(world: World, step_id: int) -> dict[str, Any]:
    conn = db.connect(world.db_path)
    try:
        return store.get_step(conn, step_id)
    finally:
        conn.close()


def test_an_item_keeps_when_it_last_moved_to_stage_done(world: World) -> None:
    item_id = _item(world)
    # Put while the task is open: a finished task takes no new step from an agent (SPEC CONTROL-27).
    step = _step(world, "reply:1", [CLOSING])
    assert _item_state(world, item_id)["done_at"] is None
    old = "2026-01-01T09:00:00Z"
    set_done_at = lambda: run_sql(world, "UPDATE items SET done_at = ? WHERE id = ?", (old, item_id))
    put = lambda client, headers, stage: client.put("/api/items", json={"key": ITEM, "stage": stage}, headers=headers)
    # The user moves it to done; an agent's later put of done leaves the time alone.
    assert put(world.client, world.user(), "done").status_code == 200
    assert _item_state(world, item_id)["done_at"] is not None
    set_done_at()
    put(_agent(world), world.agent(), "done")
    assert _item_state(world, item_id)["done_at"] == old
    # The user takes it back to building and then to done from the stage menu.
    put(world.client, world.user(), "building")
    assert _item_state(world, item_id)["done_at"] == old
    put(world.client, world.user(), "done")
    assert _item_state(world, item_id)["done_at"] > old
    # Complete on an item already at done keeps its time; on one that is not, sets it.
    set_done_at()
    assert _finish(world, item_id, step["id"], "complete", None, "done").status_code == 200
    assert _item_state(world, item_id)["done_at"] == old
    # Moved to building, then that Complete undone: Undo brings it back to done, now.
    put(world.client, world.user(), "building")
    assert world.client.post(f"/api/items/{item_id}/reopen", headers=world.user()).status_code == 200
    assert _item_state(world, item_id)["stage"] == "done"
    assert _item_state(world, item_id)["done_at"] > old
    put(world.client, world.user(), "review")
    set_done_at()
    step = _step(world, "reply:2", [CLOSING])
    assert _finish(world, item_id, step["id"], "complete", None, "done").status_code == 200
    assert _item_state(world, item_id)["done_at"] > old


def test_answers_carry_whether_they_close_the_item(world: World) -> None:
    _item(world)
    step = _step(world, "reply", ["Not yet", CLOSING])
    assert step["answers"] == [{"text": "Not yet", "closes": False}, CLOSING]
    assert _step_state(world, step["id"])["answers"] == [{"text": "Not yet", "closes": False}, CLOSING]


def test_a_closing_answer_completes_the_item_and_tells_the_session(world: World, monkeypatch) -> None:
    stops_and_resumes(world, live("done", "idle"), monkeypatch)
    item_id = _item(world)
    step = _step(world, "reply", ["Not yet", CLOSING])
    other = _step(world, "merge", ["Merge it"])
    response = _finish(world, item_id, step["id"], "complete", CLOSING["text"], "")
    assert response.status_code == 200, response.text
    finished = response.json()
    assert finished["steps"] == [step["id"], other["id"]]
    assert finished["session_end"] == {"name": "session 1111", "how": "archiving", "other_steps": 0}
    assert finished["send"] is None
    item = _item_state(world, item_id)
    assert (item["stage"], item["waiting_on"], item["archived"]) == ("done", [], 0)
    assert {_step_state(world, step["id"])["status"], _step_state(world, other["id"])["status"]} == {"done"}
    # The answer waits for Take back, then reaches the session as the user's message that completed the item.
    answer = message(world, finished["message"]["id"])
    assert (answer["author"], answer["completes"], answer["state"]) == ("user", 1, "queued")
    assert answer["take_back_until"] is not None
    [archive] = _archives(world)
    assert archive["message_id"] == answer["id"]
    conn = db.connect(world.db_path)
    try:
        text = delivery.compose(conn, store.get_message(conn, answer["id"]))
    finally:
        conn.close()
    assert "Sam completed this item with that message" in text
    assert "hands you" not in text
    assert store.release_message(db.connect(world.db_path), answer["id"])
    world.deliverer.send(answer["id"])
    assert message(world, answer["id"])["state"] == "sent"
    # Once the session's turn on the answer is over, Cockpit stops it, as Archive does.
    world.cache.sessions = [_turn_over(world.cache.sessions[0])]
    assert _due(world) == [archive["id"]]


def test_complete_task_without_an_answer_archives_the_session_with_cockpit_s_message(world: World, monkeypatch) -> None:
    stops_and_resumes(world, live("working", "busy"), monkeypatch)
    item_id = _item(world)
    step = _step(world, "complete", [])
    finished = _finish(world, item_id, step["id"], "complete", None, "").json()
    assert finished["message"] is None
    assert "Sam completed item" in finished["send"]["text"]
    assert "Once this turn is over, Cockpit stops and archives this session" in finished["send"]["text"]
    [archive] = _archives(world)
    assert archive["message_id"] == finished["send"]["id"]


def test_a_session_with_other_open_steps_is_told_and_kept(world: World, monkeypatch) -> None:
    stops_and_resumes(world, live("working", "busy"), monkeypatch)
    item_id = _item(world)
    step = _step(world, "complete", [])
    elsewhere = {"key": "gh:example-org/app#10", "title": "PR 10", "sessions": [SESSION_A]}
    _agent(world).put("/api/items", json=elsewhere, headers=world.agent())
    _agent(world).post(
        "/api/steps", json={"item_key": elsewhere["key"], "need": "merge", "ask": "Merge", "answers": ["Merge it"]}, headers=world.agent()
    )
    finished = _finish(world, item_id, step["id"], "complete", None, "").json()
    assert finished["session_end"] == {"name": "session 1111", "how": "kept", "other_steps": 1}
    assert "Cockpit archives" not in finished["send"]["text"]
    assert _archives(world) == []


def test_close_item_needs_a_reason_and_archives_the_item(world: World) -> None:
    item_id = _item(world)
    step = _step(world, "reply", [])
    assert _finish(world, item_id, step["id"], "close", None, " ").status_code == 400
    assert _finish(world, item_id, step["id"], "close", CLOSING["text"], "no").status_code == 400
    finished = _finish(world, item_id, step["id"], "close", None, "Ana answered it herself").json()
    assert "Sam's reason: Ana answered it herself" in finished["send"]["text"]
    item = _item_state(world, item_id)
    assert (item["archived"], item["stage"], "archived" in item["user_fields"]) == (1, "review", True)
    assert _step_state(world, step["id"])["status"] == "dismissed"
    assert _finish(world, item_id, step["id"], "close", None, "again").status_code == 409


def test_finishing_ends_the_running_timer(world: World) -> None:
    item_id = _item(world)
    for how, reason, ended_by in [("close", "not needed", "dismiss"), ("complete", "", "done")]:
        step = _step(world, f"timed-{how}", [])
        assert world.client.post(f"/api/steps/{step['id']}/timer/start", headers=world.user()).status_code == 200
        response = _finish(world, item_id, step["id"], how, None, reason)
        assert response.status_code == 200, response.text
        conn = db.connect(world.db_path)
        try:
            span = conn.execute("SELECT ended_at, ended_by FROM time_spans WHERE step_id = ?", (step["id"],)).fetchone()
        finally:
            conn.close()
        assert span["ended_at"] is not None and span["ended_by"] == ended_by
        assert world.client.post(f"/api/items/{item_id}/reopen", headers=world.user()).status_code == 200


def test_closing_in_all_work_ends_the_running_timer(world: World) -> None:
    item_id = _item(world)
    step = _step(world, "timed-row", [])
    assert world.client.post(f"/api/steps/{step['id']}/timer/start", headers=world.user()).status_code == 200
    assert world.client.patch(f"/api/items/{item_id}", json={"archived": True}, headers=world.user()).status_code == 200
    conn = db.connect(world.db_path)
    try:
        span = conn.execute("SELECT ended_at, ended_by FROM time_spans WHERE step_id = ?", (step["id"],)).fetchone()
    finally:
        conn.close()
    assert span["ended_at"] is not None and span["ended_by"] == "dismiss"


def test_task_done_with_a_last_message_completes_the_item_and_tells_the_session(world: World, monkeypatch) -> None:
    stops_and_resumes(world, live("done", "idle"), monkeypatch)
    item_id = _item(world)
    step = _step(world, "complete", [])
    response = _finish(world, item_id, step["id"], "complete", None, "", note="  Thanks, also warn Alex  ")
    assert response.status_code == 200, response.text
    finished = response.json()
    assert finished["send"] is None
    assert _item_state(world, item_id)["stage"] == "done"
    # The last message waits for Take back like a closing answer, then reaches the session as the user's message.
    sent = message(world, finished["message"]["id"])
    assert (sent["author"], sent["text"], sent["completes"], sent["state"]) == ("user", "Thanks, also warn Alex", 1, "queued")
    [archive] = _archives(world)
    assert archive["message_id"] == sent["id"]


def test_a_later_message_keeps_the_last_message_of_task_done(world: World, monkeypatch) -> None:
    stops_and_resumes(world, live("done", "idle"), monkeypatch)
    item_id = _item(world)
    step = _step(world, "complete", [])
    finished = _finish(world, item_id, step["id"], "complete", None, "", note="Tell Alex the result").json()
    response = world.client.post(
        "/api/messages", json={"text": "One more thing", "target": {"session_id": SESSION_A}}, headers=world.user()
    )
    assert response.status_code == 200, response.text
    # The later message brings the session back; the user's closing words still go, ahead of it.
    [archive] = _archives(world)
    assert archive["revived_at"] is not None
    kept = message(world, finished["message"]["id"])
    assert (kept["state"], kept["dismissed_at"]) == ("queued", None)


def test_a_last_message_goes_only_with_task_done(world: World) -> None:
    item_id = _item(world)
    step = _step(world, "reply", ["Not yet", CLOSING])
    assert _finish(world, item_id, step["id"], "close", None, "not needed", note="bye").status_code == 400
    assert _finish(world, item_id, step["id"], "complete", CLOSING["text"], "", note="and this").status_code == 400
    assert _item_state(world, item_id)["stage"] == "review"


def test_only_a_closing_answer_of_the_step_completes_from_its_text(world: World) -> None:
    item_id = _item(world)
    step = _step(world, "reply", ["Not yet", CLOSING])
    assert _finish(world, item_id, step["id"], "complete", "Not yet", "").status_code == 400
    assert _finish(world, item_id, step["id"], "complete", "Something else", "").status_code == 400
    assert agent_cannot_finish(world, item_id, step["id"])


def test_a_finish_reaches_only_the_session_the_page_showed(world: World) -> None:
    item_id = _item(world)
    step = _step(world, "reply", [CLOSING])
    body = {"step_id": step["id"], "how": "complete", "answer": None, "note": "", "reason": "", "session_id": SESSION_B}
    refused = world.client.post(f"/api/items/{item_id}/finish", json=body, headers=world.user())
    assert refused.status_code == 409 and "another session" in refused.text
    assert _finish(world, item_id, step["id"], "complete", None, "").status_code == 200


def test_a_finish_is_refused_when_the_step_goes_elsewhere_while_it_is_handled(world: World, monkeypatch) -> None:
    item_id = _item(world)
    step = _step(world, "reply", [CLOSING])
    lookup = world.deliverer.lookup

    # The step moves to session B after the route read its session, before the finish writes.
    def lookup_then_move(session_id: str):
        run_sql(world, "UPDATE steps SET session_id = ? WHERE id = ?", (SESSION_B, step["id"]))
        return lookup(session_id)

    monkeypatch.setattr(world.deliverer, "lookup", lookup_then_move)
    refused = _finish(world, item_id, step["id"], "complete", CLOSING["text"], "")
    assert refused.status_code == 409 and "another session" in refused.text
    assert _step_state(world, step["id"])["status"] == "open"


def agent_cannot_finish(world: World, item_id: int, step_id: int) -> bool:
    body = {"step_id": step_id, "how": "complete", "answer": None, "note": "", "reason": "", "session_id": SESSION_A}
    return _agent(world).post(f"/api/items/{item_id}/finish", json=body, headers=world.agent()).status_code == 403


def test_undo_puts_the_item_and_its_steps_back_and_calls_off_the_archive(world: World, monkeypatch) -> None:
    stops_and_resumes(world, live("done", "idle"), monkeypatch)
    item_id = _item(world)
    step = _step(world, "reply", [CLOSING])
    finished = _finish(world, item_id, step["id"], "complete", CLOSING["text"], "").json()
    reopened = world.client.post(f"/api/items/{item_id}/reopen", headers=world.user())
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["send"] is None
    item = _item_state(world, item_id)
    assert (item["stage"], item["waiting_on"]) == ("review", [{"kind": "person", "until": LOOK_AGAIN, "who": "Ana"}])
    reopened_step = _step_state(world, step["id"])
    assert (reopened_step["status"], reopened_step["handoff_message_id"]) == ("open", None)
    # The answer still waited for Take back: it is set aside unsent.
    assert message(world, finished["message"]["id"])["state"] == "failed"
    [archive] = _archives(world)
    assert archive["cancelled_at"] is not None
    assert world.client.post(f"/api/items/{item_id}/reopen", headers=world.user()).status_code == 409
    # It can be completed again afterwards.
    assert _finish(world, item_id, step["id"], "complete", None, "").status_code == 200


def test_undo_keeps_a_field_the_user_set_after_the_finish(world: World, monkeypatch) -> None:
    stops_and_resumes(world, live("done", "idle"), monkeypatch)
    item_id = _item(world)
    step = _step(world, "reply", [CLOSING])
    assert _finish(world, item_id, step["id"], "close", None, "Not needed").status_code == 200
    assert world.client.patch(f"/api/items/{item_id}", json={"priority": "high"}, headers=world.user()).status_code == 200
    assert world.client.post(f"/api/items/{item_id}/reopen", headers=world.user()).status_code == 200
    assert _item_state(world, item_id)["user_fields"] == ["priority"]
    put = _agent(world).put("/api/items", json={"key": ITEM, "priority": "low"}, headers=world.agent()).json()
    assert put["kept"] == ["priority"] and _item_state(world, item_id)["priority"] == "high"


def test_undo_after_the_message_reached_the_session_tells_it(world: World, monkeypatch) -> None:
    stops_and_resumes(world, live("done", "idle"), monkeypatch)
    item_id = _item(world)
    step = _step(world, "complete", [])
    finished = _finish(world, item_id, step["id"], "close", None, "Not needed").json()
    world.deliverer.send(finished["send"]["id"])
    assert message(world, finished["send"]["id"])["state"] == "sent"
    reopened = world.client.post(f"/api/items/{item_id}/reopen", headers=world.user()).json()
    assert "Sam undid the closed item" in reopened["send"]["text"]
    assert _item_state(world, item_id)["archived"] == 0




def test_the_migration_turns_stored_answers_into_buttons(world: World) -> None:
    _item(world)
    step = _step(world, "reply", ["Yes"])
    conn = db.connect(world.db_path)
    try:
        conn.execute("UPDATE steps SET answers = '[\"Yes\", \"No\"]' WHERE id = ?", (step["id"],))
        conn.execute("UPDATE step_versions SET answers = '[\"Yes\"]'")
        convert = next(m for m in db.MIGRATIONS if "'closes', json('false')" in m).split("ALTER TABLE")[0]
        conn.executescript(f"BEGIN; {convert}; COMMIT;")
        assert store.get_step(conn, step["id"])["answers"] == [
            {"text": "Yes", "closes": False}, {"text": "No", "closes": False},
        ]
        versions = {row[0] for row in conn.execute("SELECT answers FROM step_versions").fetchall()}
        assert versions == {'[{"text":"Yes","closes":false}]'}
    finally:
        conn.close()



def test_undo_gives_back_places_withdrawals_and_hand_offs(world: World) -> None:
    item_id = _item(world)
    step = _step(world, "reply", [CLOSING])
    withdrawn = _step(world, "decide", [])
    handed = _step(world, "merge", ["Merge it"])
    others = []
    for n in range(3):
        body = {"key": f"note:other-{n}", "title": f"Other {n}"}
        _agent(world).put("/api/items", json=body, headers=world.agent())
        others.append(_agent(world).post(
            "/api/steps", json={"item_key": body["key"], "need": "look", "ask": "Look"}, headers=world.agent()
        ).json()["step"])
    assert _agent(world).post(f"/api/steps/{withdrawn['id']}/withdraw", json={}, headers=world.agent()).status_code == 200
    hand = world.client.post(f"/api/steps/{handed['id']}/hand-off", json={"text": "Merge it"}, headers=world.user())
    assert hand.status_code == 200, hand.text
    before = {each: _step_state(world, each) for each in (step["id"], withdrawn["id"], handed["id"], others[-1]["id"])}
    assert before[step["id"]]["queued_at"] is not None
    assert _finish(world, item_id, step["id"], "complete", None, "").status_code == 200
    assert world.client.post(f"/api/items/{item_id}/reopen", headers=world.user()).status_code == 200
    after = {each: _step_state(world, each) for each in before}
    assert after[step["id"]]["queued_at"] == before[step["id"]]["queued_at"]
    assert after[withdrawn["id"]]["withdrawn_at"] == before[withdrawn["id"]]["withdrawn_at"] is not None
    assert after[handed["id"]]["handoff_message_id"] == hand.json()["id"]
    assert after[others[-1]["id"]]["queued_at"] == before[others[-1]["id"]]["queued_at"]


def test_an_item_whose_work_went_on_after_it_was_completed_can_be_completed_again(world: World) -> None:
    item_id = _item(world)
    step = _step(world, "complete", [])
    assert _finish(world, item_id, step["id"], "complete", None, "").status_code == 200
    # The user finds more to do: they take the item back to building, and its session puts a new step.
    world.client.put("/api/items", json={"key": ITEM, "stage": "building"}, headers=world.user())
    again = _step(world, "complete:2", [])
    assert _finish(world, item_id, again["id"], "complete", None, "").status_code == 200
    assert _item_state(world, item_id)["stage"] == "done"


def test_undo_while_the_message_is_on_its_way_tells_the_session_after_it(world: World, monkeypatch) -> None:
    stops_and_resumes(world, live("done", "idle"), monkeypatch)
    item_id = _item(world)
    step = _step(world, "complete", [])
    finished = _finish(world, item_id, step["id"], "close", None, "Not needed").json()
    conn = db.connect(world.db_path)
    try:
        conn.execute("UPDATE messages SET state = 'sending' WHERE id = ?", (finished["send"]["id"],))
    finally:
        conn.close()
    reopened = world.client.post(f"/api/items/{item_id}/reopen", headers=world.user()).json()
    assert "Sam undid the closed item" in reopened["send"]["text"]


def test_undo_gives_every_step_its_place_however_their_times_fall(world: World) -> None:
    # Restored one by one, a step whose place began at or after the finish counted as a later place against the next
    # step restored, and lost its own
    item_id = _item(world)
    first = _step(world, "reply", [])
    second = _step(world, "decide", [])
    others = []
    for n in range(4):
        body = {"key": f"note:other-{n}", "title": f"Other {n}"}
        _agent(world).put("/api/items", json=body, headers=world.agent())
        others.append(_agent(world).post(
            "/api/steps", json={"item_key": body["key"], "need": "look", "ask": "Look"}, headers=world.agent()
        ).json()["step"]["id"])
    conn = db.connect(world.db_path)
    try:
        conn.execute("UPDATE steps SET queued_at = '2099-01-01T00:00:00Z' WHERE id = ?", (first["id"],))
    finally:
        conn.close()
    queued = {each for each in [first["id"], second["id"], *others] if _step_state(world, each)["queued_at"]}
    assert queued == {first["id"], second["id"], others[0], others[1]}
    assert _finish(world, item_id, first["id"], "complete", None, "").status_code == 200
    assert all(_step_state(world, each)["queued_at"] for each in others)
    assert world.client.post(f"/api/items/{item_id}/reopen", headers=world.user()).status_code == 200
    after = {each for each in [first["id"], second["id"], *others] if _step_state(world, each)["queued_at"]}
    assert after == queued


def test_finishing_again_takes_over_the_earlier_finish_s_archive(world: World, monkeypatch) -> None:
    # The session is busy, so Cockpit's message about the first finish still waits when the user moves the item
    # back to building; the second finish archives the session itself, and its Undo calls the archive off.
    stops_and_resumes(world, live("working", "busy"), monkeypatch)
    item_id = _item(world)
    step = _step(world, "complete", [])
    first = _finish(world, item_id, step["id"], "complete", None, "").json()
    assert message(world, first["send"]["id"])["state"] == "queued"
    world.client.put("/api/items", json={"key": ITEM, "stage": "building"}, headers=world.user())
    again = _step(world, "complete:2", [])
    second = _finish(world, item_id, again["id"], "complete", None, "").json()
    old = message(world, first["send"]["id"])
    assert (old["state"], old["dismissed_by"]) == ("failed", "user")
    assert old["detail"] == "Not sent: Sam finished the item again, and that finish tells the session."
    assert message(world, second["send"]["id"])["state"] == "queued"
    earlier, latest = _archives(world)
    assert earlier["cancelled_at"] is not None and latest["cancelled_at"] is None
    assert latest["message_id"] == second["send"]["id"]
    assert world.client.post(f"/api/items/{item_id}/reopen", headers=world.user()).status_code == 200
    assert all(archive["cancelled_at"] for archive in _archives(world))
    assert message(world, second["send"]["id"])["state"] == "failed"
    assert _item_state(world, item_id)["stage"] == "building"


def test_the_state_counts_the_items_the_user_closed_in_the_last_day(world: World) -> None:
    def closed() -> dict:
        state = world.client.get("/api/state").json()
        return state["closed_by_user"] | {"count": state["counts"]["closed_by_user"]}

    item_id = _item(world)
    step = _step(world, "reply", [])
    assert closed() == {"finished": [], "archived": [], "count": 0}
    assert _finish(world, item_id, step["id"], "complete", None, "").status_code == 200
    assert closed() == {"finished": [item_id], "archived": [], "count": 1}
    # Close in All work on the completed item: it counts once.
    world.client.patch(f"/api/items/{item_id}", json={"archived": True}, headers=world.user())
    assert closed() == {"finished": [item_id], "archived": [item_id], "count": 1}
    world.client.patch(f"/api/items/{item_id}", json={"archived": False}, headers=world.user())
    assert closed() == {"finished": [item_id], "archived": [], "count": 1}
    assert world.client.post(f"/api/items/{item_id}/reopen", headers=world.user()).status_code == 200
    assert closed()["count"] == 0
    # Close in All work, its Undo, then Close item from the card: only the finish counts, as the user's latest
    # edit of `archived` restored the item.
    world.client.patch(f"/api/items/{item_id}", json={"archived": True}, headers=world.user())
    world.client.patch(f"/api/items/{item_id}", json={"archived": False}, headers=world.user())
    step = _step(world, "again", [])
    assert _finish(world, item_id, step["id"], "close", None, "not needed").status_code == 200
    assert closed() == {"finished": [item_id], "archived": [], "count": 1}
    assert world.client.post(f"/api/items/{item_id}/reopen", headers=world.user()).status_code == 200
    assert closed()["count"] == 0
    # Close in All work archives the item with no finish; an agent's archive is not the user's close, and an
    # agent's later edit leaves the user's close counted.
    other = _agent(world).put("/api/items", json={"key": "note:other", "title": "Other"}, headers=world.agent()).json()["id"]
    agents = _agent(world).put("/api/items", json={"key": "note:agents", "title": "Agents'"}, headers=world.agent()).json()["id"]
    world.client.patch(f"/api/items/{other}", json={"archived": True}, headers=world.user())
    _agent(world).put("/api/items", json={"key": "note:agents", "archived": True, "note": "done"}, headers=world.agent())
    _agent(world).put("/api/items", json={"key": "note:other", "summary": "a later status"}, headers=world.agent())
    assert closed() == {"finished": [], "archived": [other], "count": 1}
    # A day later it no longer counts, and the user's edit of something else on it does not count it again.
    conn = db.connect(world.db_path)
    try:
        conn.execute("UPDATE events SET at = '2026-01-01T00:00:00Z' WHERE item_id IN (?, ?)", (other, agents))
        conn.commit()
    finally:
        conn.close()
    assert closed()["count"] == 0
    world.client.patch(f"/api/items/{other}", json={"priority": "low"}, headers=world.user())
    assert closed()["count"] == 0
    # An archive recorded before the event kept its value still counts while the item stays archived.
    legacy = _agent(world).put("/api/items", json={"key": "note:legacy", "title": "Legacy"}, headers=world.agent()).json()["id"]
    conn = db.connect(world.db_path)
    try:
        conn.execute("UPDATE items SET archived = 1 WHERE id = ?", (legacy,))
        conn.execute(
            "INSERT INTO events (at, actor, kind, item_id, data) VALUES (?, 'user', 'item.edited', ?, ?)",
            (db.now_iso(), legacy, '{"fields": ["archived"], "aside": false}'),
        )
        conn.commit()
    finally:
        conn.close()
    assert closed() == {"finished": [], "archived": [legacy], "count": 1}