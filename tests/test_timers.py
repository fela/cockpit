from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from cockpit import db, store
from tests.conftest import ORIGIN, SESSION_A, World

ITEM = "gh:example-org/app#10"


def _key(index: int, need: str) -> str:
    """The first step goes on ITEM, and each further one on a task of its own: one task shows one card at a time
    (SPEC NEXT-55)."""
    return ITEM if index == 0 else f"note:{need}"


def _steps(world: World, *needs: str) -> list[int]:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    ids = []
    for index, need in enumerate(needs):
        key = _key(index, need)
        title = "PR 10" if key == ITEM else need
        agent.put("/api/items", json={"key": key, "title": title, "sessions": [SESSION_A]}, headers=world.agent())
        put = agent.post("/api/steps", json={"item_key": key, "need": need, "ask": "Review"}, headers=world.agent())
        assert put.status_code == 200, put.text
        ids.append(put.json()["step"]["id"])
    return ids


def _start(world: World, step_id: int) -> dict:
    response = world.client.post(f"/api/steps/{step_id}/timer/start", headers=world.user())
    assert response.status_code == 200, response.text
    return response.json()


def _spans(world: World) -> list[tuple[int, str | None]]:
    conn = sqlite3.connect(world.db_path)
    try:
        return conn.execute("SELECT step_id, ended_by FROM time_spans ORDER BY id").fetchall()
    finally:
        conn.close()


def _state(world: World) -> dict:
    return world.client.get("/api/state").json()


def test_a_step_starts_paused_until_user_presses_play(world: World) -> None:
    (_step,) = _steps(world, "review@aaaa111")
    assert _spans(world) == []
    assert _state(world)["timer"] is None


def test_play_starts_the_timer_and_the_header_shows_it(world: World) -> None:
    (step,) = _steps(world, "review@aaaa111")
    assert _start(world, step)["started"] is True
    assert _start(world, step) == {"started": False}
    state = _state(world)
    assert state["timer"]["step_id"] == step and state["timer"]["item_title"] == "PR 10"
    shown = next(entry for entry in state["queue"] + state["ready_next"] if entry["id"] == step)
    assert shown["timer"]["started_at"]
    item = next(entry for entry in state["items"] if entry["key"] == ITEM)
    assert item["time_spent"]["started_at"] == state["timer"]["started_at"]


def test_play_puts_user_on_the_step_and_switching_applies_what_waited(world: World) -> None:
    first, second = _steps(world, "review@aaaa111", "reply:b")
    assert _state(world)["queue"][0]["user_on_it"] is False
    _start(world, first)
    assert _state(world)["queue"][0]["user_on_it"] is True
    agent = TestClient(world.client.app, base_url=ORIGIN)
    assert agent.post(f"/api/steps/{first}/done", json={}, headers=world.agent()).json()["held"]
    assert [entry["id"] for entry in _state(world)["queue"]] == [first, second]
    _start(world, second)
    assert [entry["id"] for entry in _state(world)["queue"]] == [second]


def test_one_timer_runs_at_a_time(world: World) -> None:
    first, second = _steps(world, "review@aaaa111", "reply:b")
    _start(world, first)
    _start(world, second)
    assert _spans(world) == [(first, "switch"), (second, None)]


def test_pause_then_play(world: World) -> None:
    (step,) = _steps(world, "review@aaaa111")
    _start(world, step)
    assert world.client.post(f"/api/steps/{step}/timer/pause", headers=world.user()).status_code == 200
    assert _state(world)["timer"] is None
    assert next(entry for entry in _state(world)["queue"] if entry["id"] == step)["timer"]["started_at"] is None
    assert _start(world, step)["started"] is True
    assert world.client.post(f"/api/steps/{step}/timer/pause", headers=world.user()).status_code == 200
    assert world.client.post(f"/api/steps/{step}/timer/pause", headers=world.user()).status_code == 409
    assert _spans(world) == [(step, "pause"), (step, "pause")]


def _start_pressed(world: World, step: int, press: str) -> dict:
    headers = {**world.user(), "x-cockpit-action": press}
    response = world.client.post(f"/api/steps/{step}/timer/start", json={"press": press}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _pause(world: World, step: int, ago_ms: int, start_press: str | None, action: str) -> int:
    body = {"ago_ms": ago_ms} if start_press is None else {"ago_ms": ago_ms, "start_press": start_press}
    headers = {**world.user(), "x-cockpit-action": action}
    response = world.client.post(f"/api/steps/{step}/timer/pause", json=body, headers=headers)
    return response.status_code


def _span_times(world: World) -> list[tuple[str, str | None, str | None]]:
    conn = sqlite3.connect(world.db_path)
    try:
        return conn.execute("SELECT started_at, ended_at, ended_by FROM time_spans ORDER BY id").fetchall()
    finally:
        conn.close()


def _near(when: str, minutes_ago: float) -> bool:
    return abs(datetime.fromisoformat(when) - datetime.fromisoformat(_ago(minutes_ago))) <= timedelta(seconds=2)


def test_a_late_pause_stops_the_timer_when_the_user_pressed_it(world: World) -> None:
    (step,) = _steps(world, "review@aaaa111")
    _start(world, step)
    _running_since(world, 40)
    ((started, _, _),) = _span_times(world)
    assert _pause(world, step, 36 * 60 * 1000, None, "late-pause-1") == 200
    ((_, ended, ended_by),) = _span_times(world)
    assert _near(ended, 36) and ended_by == "pause"
    _start(world, step)
    assert _pause(world, step, 36 * 60 * 1000, None, "late-pause-1") == 200
    assert _span_times(world)[0] == (started, ended, "pause")
    assert _span_times(world)[1][1] is None


def test_a_pause_behind_its_own_late_start_leaves_an_empty_span(world: World) -> None:
    # Play and pause both pressed while the connection was down: the play lands after the pause's press, and
    # the pause that names it ends its span where it started, also when the play's answer was lost on the way.
    (step,) = _steps(world, "review@aaaa111")
    _start_pressed(world, step, "play-press-1")
    assert _pause(world, step, 50 * 1000, "play-press-1", "late-pause-2") == 200
    ((started, ended, ended_by),) = _span_times(world)
    assert ended == started and ended_by == "pause"


def test_a_late_pause_leaves_a_timer_started_since_elsewhere_running(world: World) -> None:
    # A pause pressed on a laptop that then slept; the timer was started after the press on a phone.
    (step,) = _steps(world, "review@aaaa111")
    _start(world, step)
    assert _pause(world, step, 50 * 1000, None, "late-pause-3") == 409
    assert _state(world)["timer"]["step_id"] == step


def test_a_late_pause_moves_a_later_stop_back_to_the_press(world: World) -> None:
    # The laptop started the step and paused it offline; on a phone the user paused and started it again, and
    # then marked it done. The laptop's span ends at the press, and the phone's span, started after the press,
    # keeps its end: the laptop's start began no span after the press.
    (step,) = _steps(world, "review@aaaa111")
    _start_pressed(world, step, "play-press-2")
    _running_since(world, 30)
    assert world.client.post(f"/api/steps/{step}/timer/pause", headers=world.user()).status_code == 200
    _start(world, step)
    assert world.client.post(f"/api/steps/{step}/done", json={}, headers=world.user()).status_code == 200
    (_, phone) = _span_times(world)
    assert _pause(world, step, 20 * 60 * 1000, "play-press-2", "late-pause-4") == 200
    (laptop, after) = _span_times(world)
    assert _near(laptop[1], 20) and laptop[2] == "pause" and after == phone


def _set_spans(world: World, step: int, spans: list[tuple[str, str | None, str | None]]) -> None:
    conn = sqlite3.connect(world.db_path)
    with conn:
        conn.execute("DELETE FROM time_spans")
        conn.executemany(
            "INSERT INTO time_spans (step_id, started_at, ended_at, ended_by) VALUES (?, ?, ?, ?)",
            [(step, *span) for span in spans],
        )
    conn.close()


def _ago_ms(when: str, after_ms: int) -> int:
    """The age of the moment `after_ms` after `when`."""
    return int((datetime.now(UTC) - datetime.fromisoformat(when)).total_seconds() * 1000) - after_ms


def test_a_late_pause_leaves_a_restart_within_its_second_running(world: World) -> None:
    # Times are kept to the second. The phone paused and started the step again in the second the laptop's
    # pause was pressed: the span that ended then is the one that ran, and the new one runs on.
    (step,) = _steps(world, "review@aaaa111")
    first, second = _ago(10), _ago(0.5)
    _set_spans(world, step, [(first, second, "pause"), (second, None, None)])
    assert _pause(world, step, _ago_ms(second, 200), None, "late-pause-5") == 200
    assert _span_times(world) == [(first, second, "pause"), (second, None, None)]


def test_a_pause_right_after_a_start_elsewhere_stops_it(world: World) -> None:
    # The phone shows a timer the laptop started under a second before the phone's pause.
    (step,) = _steps(world, "review@aaaa111")
    second = _ago(0.5)
    _set_spans(world, step, [(second, None, None)])
    assert _pause(world, step, _ago_ms(second, 500), None, "late-pause-9") == 200
    assert _span_times(world) == [(second, second, "pause")]


def test_a_repeated_start_begins_no_second_span(world: World) -> None:
    # The play's answer was lost; the phone paused the timer before the laptop sent the play again. The repeat
    # gets the first answer and starts nothing.
    (step,) = _steps(world, "review@aaaa111")
    assert _start_pressed(world, step, "play-press-4")["started"] is True
    assert world.client.post(f"/api/steps/{step}/timer/pause", headers=world.user()).status_code == 200
    assert _start_pressed(world, step, "play-press-4")["started"] is True
    assert len(_span_times(world)) == 1
    assert _state(world)["timer"] is None
    # A repeat Cockpit kept no answer for (its first try failed after the span began) starts nothing either.
    response = world.client.post(f"/api/steps/{step}/timer/start", json={"press": "play-press-4"}, headers=world.user())
    assert response.json() == {"started": False}
    assert len(_span_times(world)) == 1


def test_a_pause_stops_only_its_own_step(world: World) -> None:
    first, second = _steps(world, "review@aaaa111", "review@bbbb222")
    _start_pressed(world, first, "play-press-3")
    assert _pause(world, second, 0, None, "late-pause-6") == 409
    assert _pause(world, second, 0, "play-press-3", "late-pause-7") == 409
    assert _state(world)["timer"]["step_id"] == first


def test_a_snoozed_step_starts_no_timer(world: World) -> None:
    # A play the page still sends again after the step was snoozed, here or on another device, starts nothing.
    (step,) = _steps(world, "review@aaaa111")
    world.client.post(f"/api/steps/{step}/snooze", json={"until": "2099-01-01T08:00:00Z"}, headers=world.user())
    refused = world.client.post(f"/api/steps/{step}/timer/start", headers=world.user())
    assert refused.status_code == 409 and refused.json()["detail"] == f"step {step} is snoozed"
    assert _spans(world) == []


def test_a_held_change_whose_apply_fails_waits_for_the_next_release(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = _steps(world, "review@aaaa111", "reply:b")
    _start(world, first)
    agent = TestClient(world.client.app, base_url=ORIGIN)
    assert agent.post(f"/api/steps/{first}/done", json={}, headers=world.agent()).json()["held"]
    conn = db.connect(world.db_path)
    conn.execute("UPDATE time_spans SET ended_at = started_at, ended_by = 'pause'")

    def broken(*args: object) -> None:
        raise RuntimeError("disk full")

    # A patch of its own, so that ending it leaves the fixtures' patches (the test database, no network) in place.
    with monkeypatch.context() as patch:
        patch.setattr(store, "step_action", broken)
        with pytest.raises(RuntimeError):
            store.release_held(conn)
        assert conn.execute("SELECT COUNT(*) FROM held_changes").fetchone()[0] == 1
    assert store.release_held(conn) == 1
    conn.close()
    assert [entry["id"] for entry in _state(world)["queue"]] == [second]


def test_held_changes_apply_in_order_when_the_timer_starts_again_before_they_apply(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _second = _steps(world, "review@aaaa111", "reply:b")
    _start(world, first)
    agent = TestClient(world.client.app, base_url=ORIGIN)
    for ask in ("Review A", "Review B"):
        put = agent.post("/api/steps", json={"item_key": ITEM, "need": "review@aaaa111", "ask": ask, "changed": "Reworded."}, headers=world.agent())
        assert put.json()["held"]
    conn = db.connect(world.db_path)
    conn.execute("UPDATE time_spans SET ended_at = started_at, ended_by = 'pause'")
    transaction = db.transaction
    presses = iter([
        # The user presses play between release_held's read and the first change's apply, and pause before the next.
        "INSERT INTO time_spans (step_id, started_at) VALUES (?, datetime('now'))",
        "UPDATE time_spans SET ended_at = started_at, ended_by = 'pause' WHERE step_id = ? AND ended_at IS NULL",
    ])

    def pressed_between(conn: sqlite3.Connection):
        press = next(presses, None)
        if press is not None:
            conn.execute(press, (first,))
        return transaction(conn)

    monkeypatch.setattr(db, "transaction", pressed_between)
    assert store.release_held(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM held_changes").fetchone()[0] == 2
    monkeypatch.setattr(db, "transaction", transaction)
    conn.execute("UPDATE time_spans SET ended_at = started_at, ended_by = 'pause' WHERE ended_at IS NULL")
    assert store.release_held(conn) == 2
    assert conn.execute("SELECT ask FROM steps WHERE id = ?", (first,)).fetchone()[0] == "Review B"
    conn.close()


def test_a_release_reads_a_held_change_again_when_another_release_applied_it_and_its_id_came_back(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _steps(world, "review@aaaa111", "reply:b")
    _start(world, first)
    agent = TestClient(world.client.app, base_url=ORIGIN)
    put = agent.post("/api/steps", json={"item_key": ITEM, "need": "review@aaaa111", "ask": "Review A", "changed": "Reworded."}, headers=world.agent())
    assert put.json()["held"]
    conn = db.connect(world.db_path)
    conn.execute("UPDATE time_spans SET ended_at = started_at, ended_by = 'pause'")
    transaction = db.transaction
    raced = []

    def other_release_first(conn: sqlite3.Connection):
        if not raced:
            # The wake loop's release applies the change; the user starts the other step, whose rewrite is held
            # under the id the applied change freed.
            raced.append(True)
            monkeypatch.setattr(db, "transaction", transaction)
            assert store.release_held(conn) == 1
            _start(world, second)
            put = agent.post("/api/steps", json={"item_key": _key(1, "reply:b"), "need": "reply:b", "ask": "Reply C", "changed": "Reworded."},
                             headers=world.agent())
            assert put.json()["held"]
        return transaction(conn)

    monkeypatch.setattr(db, "transaction", other_release_first)
    assert store.release_held(conn) == 0
    assert [row[0] for row in conn.execute("SELECT step_id FROM held_changes")] == [second]
    assert conn.execute("SELECT ask FROM steps WHERE id = ?", (first,)).fetchone()[0] == "Review A"
    conn.close()


def test_a_put_between_a_pause_and_its_release_waits_behind_the_held_change(world: World) -> None:
    # the pause has committed and its release has not run yet when the agent puts again. The newer put
    # waits behind the held one, so the release applies both in the order they were made.
    first, _second = _steps(world, "review@aaaa111", "reply:b")
    _start(world, first)
    agent = TestClient(world.client.app, base_url=ORIGIN)
    put = agent.post("/api/steps", json={"item_key": ITEM, "need": "review@aaaa111", "ask": "Review A", "changed": "Reworded."}, headers=world.agent())
    assert put.json()["held"]
    conn = db.connect(world.db_path)
    conn.execute("UPDATE time_spans SET ended_at = started_at, ended_by = 'pause'")
    conn.commit()
    put = agent.post("/api/steps", json={"item_key": ITEM, "need": "review@aaaa111", "ask": "Review B", "changed": "Reworded."}, headers=world.agent())
    assert put.json()["held"] == f"An earlier change to step {first} waits to apply. This change applies right after it."
    done = agent.post(f"/api/steps/{first}/withdraw", json={}, headers=world.agent())
    assert done.json()["held"]
    assert store.release_held(conn) == 3
    step = db.row(conn.execute("SELECT ask, withdrawn_at FROM steps WHERE id = ?", (first,)).fetchone())
    assert step["ask"] == "Review B" and step["withdrawn_at"] is not None
    assert conn.execute("SELECT COUNT(*) FROM held_changes").fetchone()[0] == 0
    conn.close()


def test_play_on_a_step_of_a_closed_item_is_refused(world: World) -> None:
    (step,) = _steps(world, "review@aaaa111")
    item = next(entry for entry in _state(world)["items"] if entry["key"] == ITEM)
    assert world.client.patch(f"/api/items/{item['id']}", json={"archived": True}, headers=world.user()).status_code == 200
    refused = world.client.post(f"/api/steps/{step}/timer/start", headers=world.user())
    assert refused.status_code == 409 and refused.json()["detail"] == f"step {step}'s item {item['id']} is closed"
    assert _spans(world) == []


def test_done_and_hand_off_stop_the_timer(world: World) -> None:
    done, handed = _steps(world, "review@aaaa111", "reply:b")
    _start(world, done)
    world.client.post(f"/api/steps/{done}/done", json={}, headers=world.user())
    _start(world, handed)
    sent = world.client.post(f"/api/steps/{handed}/hand-off", json={"text": "Do A"}, headers=world.user())
    assert sent.status_code == 200
    assert _spans(world) == [(done, "done"), (handed, "hand_off")]
    assert _state(world)["timer"] is None
    assert world.client.post(f"/api/steps/{handed}/timer/start", headers=world.user()).status_code == 409


def test_the_item_total_adds_its_steps_time(world: World) -> None:
    (first,) = _steps(world, "review@aaaa111")
    _start(world, first)
    assert world.client.post(f"/api/steps/{first}/done", json={}, headers=world.user()).status_code == 200
    (second,) = _steps(world, "reply:b")  # the item's next card
    _start(world, second)
    conn = sqlite3.connect(world.db_path)
    with conn:
        conn.execute("UPDATE time_spans SET started_at = '2026-01-06T07:00:00Z', ended_at = '2026-01-06T07:10:00Z' WHERE step_id = ?", (first,))
        conn.execute("UPDATE time_spans SET started_at = '2026-01-06T07:10:00Z' WHERE step_id = ?", (second,))
    conn.close()
    state = _state(world)
    item = next(entry for entry in state["items"] if entry["key"] == ITEM)
    assert item["time_spent"] == {"seconds": 600, "started_at": "2026-01-06T07:10:00Z"}
    assert state["timer"]["seconds"] == 600 and state["timer"]["started_at"] == "2026-01-06T07:10:00Z"


def test_only_user_starts_a_timer(world: World) -> None:
    (step,) = _steps(world, "review@aaaa111")
    agent = TestClient(world.client.app, base_url=ORIGIN)
    refused = agent.post(f"/api/steps/{step}/timer/start", headers=world.agent())
    assert refused.status_code == 403


# Taking back time (SPEC TRACK-4).


def _ago(minutes: float, format: str = "%Y-%m-%dT%H:%M:%SZ") -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).strftime(format)


def _running_since(world: World, minutes_ago: float) -> None:
    """The running span started `minutes_ago`."""
    conn = sqlite3.connect(world.db_path)
    with conn:
        conn.execute("UPDATE time_spans SET started_at = ? WHERE ended_at IS NULL", (_ago(minutes_ago),))
    conn.close()


def _item_steps(world: World, *needs: str) -> list[int]:
    """Steps one after another on ITEM, the user closing each before the next comes: one task shows one card at a time
    (SPEC NEXT-55), and the task's time counts its closed steps too."""
    ids = []
    for need in needs:
        if ids:
            assert world.client.post(f"/api/steps/{ids[-1]}/done", json={}, headers=world.user()).status_code == 200
        (step,) = _steps(world, need)
        ids.append(step)
    return ids


def _span_rows(world: World) -> list[tuple[int, str, str | None, str | None]]:
    conn = sqlite3.connect(world.db_path)
    try:
        return conn.execute("SELECT step_id, started_at, ended_at, ended_by FROM time_spans ORDER BY id").fetchall()
    finally:
        conn.close()


def _put_spans(world: World, *spans: tuple[int, str, str | None]) -> None:
    conn = sqlite3.connect(world.db_path)
    with conn:
        conn.execute("DELETE FROM time_spans")
        conn.executemany(
            "INSERT INTO time_spans (step_id, started_at, ended_at, ended_by) VALUES (?, ?, ?, ?)",
            [(step_id, started, ended, "pause" if ended else None) for step_id, started, ended in spans],
        )
    conn.close()


def _item(world: World) -> dict:
    return next(entry for entry in _state(world)["items"] if entry["key"] == ITEM)


def test_the_state_carries_the_last_input_any_page_reported(world: World) -> None:
    """The page counts input on other pages and devices as activity (TRACK-5): the presence report's time."""
    (step,) = _steps(world, "review@aaaa111")
    _start(world, step)
    assert _state(world)["input"] == {"last": None, "count": 1}
    before = _ago(0)
    assert world.client.post("/api/presence", headers=world.user()).status_code == 200
    reported = _state(world)["input"]
    assert before <= reported["last"] <= _ago(0) and reported["count"] == 2


def _seen(world: World) -> int:
    """How many of the user's inputs Cockpit counted, as a page that just read the state knows it."""
    return _state(world)["input"]["count"]


def _stop(world: World, step: int, since: str) -> Response:
    """A page's stop of the step's timer for no activity from `since`, going by the state it just read."""
    return world.client.post(f"/api/steps/{step}/timer/away", json={"since": since, "seen": _seen(world)}, headers=world.user())


def test_away_ends_the_timer_at_the_last_activity(world: World) -> None:
    (step,) = _steps(world, "review@aaaa111")
    _start(world, step)
    _running_since(world, 120)
    since = _ago(100)
    answer = _stop(world, step, since)
    assert answer.status_code == 200, answer.text
    assert _state(world)["timer"] is None
    ((_, started, ended, ended_by),) = _span_rows(world)
    assert (ended, ended_by) == (since, "away")
    spent = datetime.fromisoformat(ended) - datetime.fromisoformat(started)
    assert _item(world)["time_spent"] == {"seconds": spent.total_seconds(), "started_at": None}


def test_away_takes_the_time_back_from_a_step_closed_since(world: World) -> None:
    # Task done ended the timer, and the stop a second later answered "no timer runs".
    (step,) = _steps(world, "review@aaaa111")
    _start(world, step)
    _running_since(world, 120)
    since = _ago(100)
    finish = {"step_id": step, "how": "complete", "answer": None, "note": "", "reason": "", "session_id": SESSION_A}
    done = world.client.post(f"/api/items/{_item(world)['id']}/finish", json=finish, headers=world.user())
    assert done.status_code == 200, done.text
    answer = _stop(world, step, since)
    assert answer.status_code == 200, answer.text
    assert answer.json()["away"] is None
    ((_, _, ended, ended_by),) = _span_rows(world)
    assert (ended, ended_by) == (since, "away")


def test_away_ends_the_newest_span_when_two_start_in_the_same_second(world: World) -> None:
    # Play, pause and play within one second leave an empty span beside the running one.
    (step,) = _steps(world, "review@aaaa111")
    started = _ago(60)
    _put_spans(world, (step, started, started), (step, started, None))
    answer = _stop(world, step, started)
    assert answer.status_code == 200, answer.text
    assert _state(world)["timer"] is None
    assert [row[2:] for row in _span_rows(world)] == [(started, "pause"), (started, "away")]


def test_away_at_a_time_still_to_come_stops_the_timer_now(world: World) -> None:
    """A page whose clock runs ahead of the server's."""
    (step,) = _steps(world, "review@aaaa111")
    _start(world, step)
    _running_since(world, 10)
    answer = _stop(world, step, _ago(-5))
    assert answer.status_code == 200, answer.text
    ((_, _, ended, ended_by),) = _span_rows(world)
    assert _near(ended, 0) and ended_by == "away"


def test_away_at_a_time_the_timer_did_not_run_is_refused(world: World) -> None:
    (step,) = _steps(world, "review@aaaa111")
    assert _stop(world, step, _ago(5)).status_code == 409
    _start(world, step)
    assert _stop(world, step, _ago(60)).status_code == 409
    assert _span_rows(world)[0][2:] == (None, None)


def _stopped_away(world: World, step: int) -> dict:
    """The step's timer ran three minutes and stopped for no activity a minute after it started; the question."""
    _start(world, step)
    _running_since(world, 3)
    answer = _stop(world, step, _ago(2))
    assert answer.status_code == 200, answer.text
    away = _state(world)["away"]
    assert away["id"] == answer.json()["away"]
    return away


def _answer(world: World, step: int, question: str, add: bool) -> dict:
    response = world.client.post(f"/api/steps/{step}/timer/answer", json={"question": question, "add": add}, headers=world.user())
    assert response.status_code == 200, response.text
    return response.json()


def test_every_page_asks_after_a_stop_for_no_activity_until_answered(world: World) -> None:
    (step,) = _steps(world, "review@aaaa111")
    assert _state(world)["away"] is None
    away = _stopped_away(world, step)
    assert {key: away[key] for key in ("step_id", "item_id", "item_title")} == {
        "step_id": step, "item_id": _item(world)["id"], "item_title": "PR 10",
    }
    assert _near(away["since"], 2) and 55 <= away["seconds"] <= 65
    assert _answer(world, step, away["id"], False)["answered"] is True
    assert _state(world)["away"] is None


def test_return_runs_the_timer_on_from_now(world: World) -> None:
    (step,) = _steps(world, "review@aaaa111")
    away = _stopped_away(world, step)
    assert _answer(world, step, away["id"], False)["answered"] is True
    (stopped, running) = _span_rows(world)
    assert stopped[2:] == (away["since"], "away")
    assert _near(running[1], 0) and running[2:] == (None, None)


def _undo_return(world: World, step: int, question: str) -> dict:
    response = world.client.post(f"/api/steps/{step}/timer/answer/undo", json={"question": question}, headers=world.user())
    assert response.status_code == 200, response.text
    return response.json()


def test_undo_of_return_counts_the_time_away_once(world: World) -> None:
    (step,) = _steps(world, "review@aaaa111")
    away = _stopped_away(world, step)
    returned = _answer(world, step, away["id"], False)
    assert returned["answered"] is True
    # Return says how long it left out: the two minutes away, which its toast offers to count.
    assert 115 <= returned["left_out"] <= 125
    ((_, _, stopped_at, _), (_, returned_at, _, _)) = _span_rows(world)
    assert _undo_return(world, step, away["id"]) == {"undone": True}
    assert _undo_return(world, step, away["id"]) == {"undone": False}
    spans = _span_rows(world)
    assert (step, stopped_at, returned_at, "corrected") in spans and len(spans) == 3
    # Three minutes ran, as after Add: one before the stop, two away.
    assert 175 <= _item(world)["time_spent"]["seconds"] <= 185
    assert _state(world)["timer"]["step_id"] == step


def test_undo_needs_the_return_it_undoes(world: World) -> None:
    (step,) = _steps(world, "review@aaaa111")
    first = _stopped_away(world, step)
    assert _undo_return(world, step, first["id"]) == {"undone": False}
    assert _answer(world, step, first["id"], True)["answered"] is True
    assert _undo_return(world, step, first["id"]) == {"undone": False}
    assert _stop(world, step, _ago(0)).status_code == 200
    second = _state(world)["away"]
    assert _answer(world, step, second["id"], False)["answered"] is True
    assert _undo_return(world, step, first["id"]) == {"undone": False}
    assert _undo_return(world, step, second["id"]) == {"undone": True}


def test_add_runs_the_stopped_timer_on_once_however_many_pages_answer(world: World) -> None:
    (step,) = _steps(world, "review@aaaa111")
    away = _stopped_away(world, step)
    ((_, started, _, _),) = _span_rows(world)
    assert _answer(world, step, away["id"], True)["answered"] is True
    assert _answer(world, step, away["id"], True) == {"answered": False}
    assert _answer(world, step, away["id"], False) == {"answered": False}
    assert _span_rows(world) == [(step, started, None, None)]
    assert _state(world)["timer"]["started_at"] == started


def test_a_stop_from_before_the_answer_does_nothing(world: World) -> None:
    # Two pages saw the same stretch; one's stop arrives after the user answered Add on the other, its page's clock
    # ahead of the server's, so its time would be clamped to now. The input count tells them apart even when the
    # answer comes in the second of the last presence report.
    (step,) = _steps(world, "review@aaaa111")
    assert world.client.post("/api/presence", headers=world.user()).status_code == 200
    away = _stopped_away(world, step)
    seen = _seen(world)
    assert _answer(world, step, away["id"], True)["answered"] is True
    late = world.client.post(f"/api/steps/{step}/timer/away", json={"since": _ago(-60), "seen": seen}, headers=world.user())
    assert late.json() == {"stopped": False, "away": None}
    assert _state(world)["away"] is None and _state(world)["timer"]["step_id"] == step


def test_a_stop_from_before_input_on_another_device_does_nothing(world: World) -> None:
    (step,) = _steps(world, "review@aaaa111")
    _start(world, step)
    _running_since(world, 5)
    seen = _seen(world)
    assert world.client.post("/api/presence", headers=world.user()).status_code == 200
    late = world.client.post(f"/api/steps/{step}/timer/away", json={"since": _ago(4), "seen": seen}, headers=world.user())
    assert late.json() == {"stopped": False, "away": None}
    assert _state(world)["timer"]["step_id"] == step and _state(world)["away"] is None


def test_a_stop_from_before_a_start_does_nothing(world: World) -> None:
    # A page whose clock runs ahead worked out its stop before the user paused and played on another device.
    (step,) = _steps(world, "review@aaaa111")
    _start(world, step)
    _running_since(world, 5)
    seen = _seen(world)
    assert world.client.post(f"/api/steps/{step}/timer/pause", headers=world.user()).status_code == 200
    _start(world, step)
    late = world.client.post(f"/api/steps/{step}/timer/away", json={"since": _ago(-60), "seen": seen}, headers=world.user())
    assert late.json() == {"stopped": False, "away": None}
    assert _state(world)["timer"]["step_id"] == step and _state(world)["away"] is None


def test_a_second_page_stopping_the_same_stretch_gets_the_first_question(world: World) -> None:
    (step,) = _steps(world, "review@aaaa111")
    away = _stopped_away(world, step)
    ((_, _, ended, _),) = _span_rows(world)
    again = _stop(world, step, _ago(1))
    assert again.json() == {"stopped": True, "away": away["id"]}
    assert _span_rows(world)[0][2] == ended and _state(world)["away"]["id"] == away["id"]


def test_an_answer_names_its_question(world: World) -> None:
    # An answer to an earlier question, delayed, does not answer a later one on the same step.
    (step,) = _steps(world, "review@aaaa111")
    first = _stopped_away(world, step)
    assert _answer(world, step, first["id"], False)["answered"] is True
    assert _stop(world, step, _ago(0)).status_code == 200
    second = _state(world)["away"]
    assert second["id"] != first["id"]
    assert _answer(world, step, first["id"], True) == {"answered": False}
    assert _answer(world, step, second["id"], True)["answered"] is True
    assert _state(world)["timer"]["step_id"] == step


def test_correcting_the_time_settles_the_question(world: World) -> None:
    (step,) = _steps(world, "review@aaaa111")
    _stopped_away(world, step)
    world.client.post(f"/api/items/{_item(world)['id']}/time", json={"seconds": 30}, headers=world.user())
    assert _state(world)["away"] is None


def test_closing_the_item_settles_the_question(world: World) -> None:
    (step,) = _steps(world, "review@aaaa111")
    _stopped_away(world, step)
    item = _item(world)
    assert world.client.patch(f"/api/items/{item['id']}", json={"archived": True}, headers=world.user()).status_code == 200
    assert _state(world)["away"] is None
    assert world.client.patch(f"/api/items/{item['id']}", json={"archived": False}, headers=world.user()).status_code == 200
    assert _state(world)["away"] is None


def test_a_start_answers_the_question(world: World) -> None:
    first, second = _steps(world, "review@aaaa111", "reply:b")
    away = _stopped_away(world, first)
    _start(world, second)
    assert _state(world)["away"] is None
    world.client.post(f"/api/steps/{second}/timer/pause", headers=world.user())
    assert _state(world)["away"] is None
    assert _answer(world, first, away["id"], True) == {"answered": False}


def test_no_question_on_a_step_that_left_the_user(world: World) -> None:
    (step,) = _steps(world, "review@aaaa111")
    away = _stopped_away(world, step)
    world.client.post(f"/api/steps/{step}/snooze", json={"until": "2099-01-01T08:00:00Z"}, headers=world.user())
    assert _state(world)["away"] is None
    world.client.post(f"/api/steps/{step}/snooze", json={"until": None}, headers=world.user())
    assert _state(world)["away"] is None
    assert _answer(world, step, away["id"], False) == {"answered": False}
    assert _state(world)["timer"] is None


def test_add_after_more_than_a_day_is_refused_and_the_question_stays(world: World) -> None:
    (step,) = _steps(world, "review@aaaa111")
    _start(world, step)
    _running_since(world, 25 * 60 + 1)
    _stop(world, step, _ago(25 * 60))
    away = _state(world)["away"]
    refused = world.client.post(f"/api/steps/{step}/timer/answer", json={"question": away["id"], "add": True}, headers=world.user())
    assert refused.status_code == 400 and "24 hours" in refused.text
    assert _state(world)["away"]["id"] == away["id"]
    assert _answer(world, step, away["id"], False)["answered"] is True


def test_correcting_the_time_takes_the_newest_time_first(world: World) -> None:
    first, second = _item_steps(world, "review@aaaa111", "reply:b")
    _put_spans(
        world,
        (first, "2026-01-06T07:00:00Z", "2026-01-06T07:10:00Z"),
        (second, "2026-01-06T08:00:00Z", "2026-01-06T08:30:00Z"),
        (first, "2026-01-06T09:00:00Z", "2026-01-06T09:05:00Z"),
    )
    answer = world.client.post(f"/api/items/{_item(world)['id']}/time", json={"seconds": 15 * 60}, headers=world.user())
    assert answer.status_code == 200, answer.text
    assert _span_rows(world) == [
        (first, "2026-01-06T07:00:00Z", "2026-01-06T07:10:00Z", "pause"),
        (second, "2026-01-06T08:00:00Z", "2026-01-06T08:05:00Z", "pause"),
    ]
    assert _item(world)["time_spent"]["seconds"] == 15 * 60


def test_correcting_a_running_timer_keeps_it_running(world: World) -> None:
    (step,) = _steps(world, "review@aaaa111")
    _put_spans(world, (step, "2026-01-06T07:00:00Z", "2026-01-06T07:10:00Z"), (step, _ago(60), None))
    world.client.post(f"/api/items/{_item(world)['id']}/time", json={"seconds": 30 * 60}, headers=world.user())
    old, cut, running = _span_rows(world)
    assert old == (step, "2026-01-06T07:00:00Z", "2026-01-06T07:10:00Z", "pause")
    assert cut[3] == "corrected" and running[2:] == (None, None)
    spent = _item(world)["time_spent"]
    assert spent["started_at"] == running[1]
    assert spent["seconds"] + (datetime.now(UTC) - datetime.fromisoformat(running[1])).total_seconds() == pytest.approx(30 * 60, abs=2)
    assert _state(world)["timer"]["step_id"] == step


def test_correcting_to_nothing_runs_the_timer_from_now(world: World) -> None:
    (step,) = _steps(world, "review@aaaa111")
    _put_spans(world, (step, "2026-01-06T07:00:00Z", "2026-01-06T07:10:00Z"), (step, _ago(60), None))
    world.client.post(f"/api/items/{_item(world)['id']}/time", json={"seconds": 0}, headers=world.user())
    ((_, started, ended, _),) = _span_rows(world)
    assert ended is None and started >= _ago(0.1)


def test_raising_the_time_adds_a_span_before_the_newest(world: World) -> None:
    first, second = _item_steps(world, "review@aaaa111", "reply:b")
    _put_spans(
        world,
        (first, "2026-01-06T07:00:00Z", "2026-01-06T07:10:00Z"),
        (second, "2026-01-06T08:00:00Z", "2026-01-06T08:05:00Z"),
    )
    answer = world.client.post(f"/api/items/{_item(world)['id']}/time", json={"seconds": 35 * 60}, headers=world.user())
    assert answer.status_code == 200, answer.text
    assert _span_rows(world)[2] == (second, "2026-01-06T07:40:00Z", "2026-01-06T08:00:00Z", "corrected")
    assert _item(world)["time_spent"]["seconds"] == 35 * 60


def test_raising_a_running_timer_keeps_it_running_and_asks_nothing(world: World) -> None:
    """The timer showed 0:01 after the user pressed play late; the time added counts as work before play, and the
    running span keeps its start, so a stop at the last activity does not count the added hour."""
    (step,) = _steps(world, "review@aaaa111")
    _start(world, step)
    ((_, started, _, _),) = _span_rows(world)
    world.client.post(f"/api/items/{_item(world)['id']}/time", json={"seconds": 60 * 60}, headers=world.user())
    running, added = _span_rows(world)
    assert running == (step, started, None, None)
    assert added[2:] == (started, "corrected")
    assert (datetime.fromisoformat(added[2]) - datetime.fromisoformat(added[1])).total_seconds() == pytest.approx(60 * 60, abs=2)
    assert _state(world)["timer"]["step_id"] == step and _state(world)["away"] is None


def test_added_time_overlaps_no_timer_so_the_timesheet_counts_it_all(world: World) -> None:
    """Two additions and another item's timer: each added span goes at the latest time no span covers."""
    first, other = _steps(world, "review@aaaa111", "reply:b")
    _put_spans(
        world,
        (other, "2026-01-06T07:50:00Z", "2026-01-06T07:55:00Z"),
        (first, "2026-01-06T08:00:00Z", "2026-01-06T08:05:00Z"),
    )
    item_id = _item(world)["id"]
    for _ in range(2):
        added = world.client.post(f"/api/items/{item_id}/time/add", json={"seconds": 10 * 60}, headers=world.user())
        assert added.status_code == 200, added.text
    assert _span_rows(world)[2:] == [
        (first, "2026-01-06T07:40:00Z", "2026-01-06T07:50:00Z", "corrected"),
        (first, "2026-01-06T07:30:00Z", "2026-01-06T07:40:00Z", "corrected"),
    ]
    assert _item(world)["time_spent"]["seconds"] == (5 + 10 + 10) * 60  # the other item's five minutes count there


def test_added_time_leaves_feedback_writing_counted_and_passes_empty_spans(world: World) -> None:
    """Feedback written 07:50-08:00 is taken time; a span that started and ended in one second is not."""
    (step,) = _steps(world, "review@aaaa111")
    _put_spans(
        world,
        (step, "2026-01-06T07:45:00Z", "2026-01-06T07:45:00Z"),
        (step, "2026-01-06T08:00:00Z", "2026-01-06T08:05:00Z"),
    )
    conn = sqlite3.connect(world.db_path)
    with conn:
        conn.execute(
            "INSERT INTO feedback (text, target, quote, seconds, created_at) VALUES ('x', '', '', 600, '2026-01-06T08:00:00Z')"
        )
    conn.close()
    added = world.client.post(f"/api/items/{_item(world)['id']}/time/add", json={"seconds": 15 * 60}, headers=world.user())
    assert added.status_code == 200, added.text
    assert _span_rows(world)[2] == (step, "2026-01-06T07:35:00Z", "2026-01-06T07:50:00Z", "corrected")


def test_adding_more_than_a_day_at_once_is_refused(world: World) -> None:
    (step,) = _steps(world, "review@aaaa111")
    _put_spans(world, (step, "2026-01-06T07:00:00Z", "2026-01-06T07:10:00Z"))
    item_id = _item(world)["id"]
    for path, seconds in (("time/add", 10**15), ("time/add", 24 * 3600 + 1), ("time", 10**15)):
        refused = world.client.post(f"/api/items/{item_id}/{path}", json={"seconds": seconds}, headers=world.user())
        assert refused.status_code == 400 and "at most 24 hours" in refused.text, (path, refused.text)
    assert len(_span_rows(world)) == 1


def test_adding_time_keeps_a_running_timer_running_through_a_second_play(world: World) -> None:
    (step,) = _steps(world, "review@aaaa111")
    _start(world, step)
    world.client.post(f"/api/items/{_item(world)['id']}/time/add", json={"seconds": 5 * 60}, headers=world.user())
    running = _span_rows(world)[0]
    again = world.client.post(f"/api/steps/{step}/timer/start", headers=world.user())
    assert again.json() == {"started": False}
    assert _span_rows(world)[0] == running and running[2] is None


def test_time_goes_up_only_on_a_timed_item_and_only_user_sets_it(world: World) -> None:
    (step,) = _steps(world, "review@aaaa111")
    item_id = _item(world)["id"]
    untimed = world.client.post(f"/api/items/{item_id}/time", json={"seconds": 10 * 60}, headers=world.user())
    assert untimed.status_code == 400 and "start a step's timer first" in untimed.text
    _put_spans(world, (step, "2026-01-06T07:00:00Z", "2026-01-06T07:10:00Z"))
    agent = TestClient(world.client.app, base_url=ORIGIN)
    assert agent.post(f"/api/items/{item_id}/time", json={"seconds": 0}, headers=world.agent()).status_code == 403
    assert agent.post(f"/api/items/{item_id}/time/add", json={"seconds": 60}, headers=world.agent()).status_code == 403
    assert world.client.post(f"/api/items/{item_id}/time/add", json={"seconds": 0}, headers=world.user()).status_code == 400
    away = {"since": _ago(1), "seen": 0}
    assert agent.post(f"/api/steps/{step}/timer/away", json=away, headers=world.agent()).status_code == 403
    answer = {"question": "0" * 32, "add": True}
    assert agent.post(f"/api/steps/{step}/timer/answer", json=answer, headers=world.agent()).status_code == 403
    assert _span_rows(world) == [(step, "2026-01-06T07:00:00Z", "2026-01-06T07:10:00Z", "pause")]


def test_a_page_on_screen_counts_for_the_timer_but_not_as_input(world: World) -> None:
    """A page with the running timer on screen tells Cockpit at most once a minute (TRACK-5, the state's
    `input` carries it for other devices, while sweeps and the time sheet go by input only (SWEEP-11)."""
    (step,) = _steps(world, "review@aaaa111")
    _start(world, step)
    before = _ago(0)
    first = world.client.post("/api/presence/seen", headers=world.user()).json()
    assert first == {"input_count": 2}
    shown = _state(world)["input"]
    assert before <= shown["last"] <= _ago(0) and shown["count"] == 2
    assert world.client.post("/api/presence/seen", headers=world.user()).json() == {"input_count": None}
    conn = sqlite3.connect(world.db_path)
    try:
        assert conn.execute("SELECT value FROM kv WHERE key = 'user_input'").fetchone() is None
        assert conn.execute("SELECT COUNT(*) FROM input_minutes").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_stop_from_before_a_page_on_screen_on_another_device_does_nothing(world: World) -> None:
    (step,) = _steps(world, "review@aaaa111")
    _start(world, step)
    _running_since(world, 5)
    seen = _seen(world)
    assert world.client.post("/api/presence/seen", headers=world.user()).status_code == 200
    late = world.client.post(f"/api/steps/{step}/timer/away", json={"since": _ago(4), "seen": seen}, headers=world.user())
    assert late.json() == {"stopped": False, "away": None}
    assert _state(world)["timer"]["step_id"] == step and _state(world)["away"] is None


def test_only_the_user_reports_a_page_on_screen(world: World) -> None:
    assert world.client.post("/api/presence/seen", headers=world.agent()).status_code == 403
