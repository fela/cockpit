from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from cockpit import actions, db
from tests.conftest import ORIGIN, World

FEEDBACK = {"text": "the queue is slow", "seconds": 4, "target": "", "quote": "", "step_id": None, "item_id": None}


def feedback_rows(world: World) -> int:
    conn = sqlite3.connect(world.db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    finally:
        conn.close()


def send(world: World, action_id: str, path: str = "/api/feedback", body: Any = FEEDBACK) -> Any:
    return world.client.post(path, json=body, headers={**world.user(), "x-cockpit-action": action_id})


def test_a_repeated_action_happens_once_and_gets_the_first_answer(world: World) -> None:
    first = send(world, "action-0001")
    assert first.status_code == 200
    again = send(world, "action-0001")
    assert again.status_code == 200
    assert again.json() == first.json()
    assert feedback_rows(world) == 1
    send(world, "action-0002")
    assert feedback_rows(world) == 2


def test_a_refused_action_runs_again_when_repeated(world: World) -> None:
    refused = send(world, "action-0003", body={**FEEDBACK, "text": ""})
    assert refused.status_code == 400
    assert send(world, "action-0003").status_code == 200
    assert feedback_rows(world) == 1


def test_an_action_id_names_one_action(world: World) -> None:
    assert send(world, "action-0004").status_code == 200
    reused = send(
        world, "action-0004", path="/api/writing", body={"kind": "part", "target": "", "quote": "x", "note": ""}
    )
    assert reused.status_code == 400
    assert "another action" in reused.json()["detail"]


def test_agents_and_unsigned_requests_are_left_alone(world: World) -> None:
    anonymous = TestClient(world.client.app, base_url=ORIGIN)
    anonymous.cookies.clear()
    answer = anonymous.post("/api/feedback", json=FEEDBACK, headers={**world.user(), "x-cockpit-action": "action-0005"})
    assert answer.status_code == 401
    # The refusal was not stored: the signed-in user's action under the same id runs.
    assert send(world, "action-0005").status_code == 200
    assert feedback_rows(world) == 1


async def nothing_after() -> None:
    return None


def once_app(tmp_path: Path, calls: list[str], release: asyncio.Event, after: Any) -> actions.Once:
    """Middleware around an app that waits for `release`, answers with its call count, then runs `after`
    (the work of background tasks, which runs once the answer went)."""
    conn = db.connect(tmp_path / "actions.db")
    db.migrate(conn)
    conn.close()

    async def app(scope: Any, receive: Any, send: Any) -> None:
        calls.append(scope["path"])
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"n": %d}' % len(calls)})
        await after()

    return actions.Once(app, tmp_path / "actions.db")


def user_scope(action_id: bytes, path: str = "/api/x") -> dict[str, Any]:
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [(b"x-cockpit-action", action_id)],
        "state": {"me": "user"},
    }


async def call(once: actions.Once, scope: dict[str, Any]) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await once(scope, receive, send)
    return sent


def test_a_repeat_while_the_first_try_runs_waits_for_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        calls: list[str] = []
        release = asyncio.Event()
        once = once_app(tmp_path, calls, release, nothing_after)
        first = asyncio.ensure_future(call(once, user_scope(b"action-0101")))
        await asyncio.sleep(0.05)
        repeat = asyncio.ensure_future(call(once, user_scope(b"action-0101")))
        await asyncio.sleep(0.05)
        release.set()
        answers = await asyncio.gather(first, repeat)
        assert calls == ["/api/x"]
        assert answers[0][1]["body"] == answers[1][1]["body"] == b'{"n": 1}'

    asyncio.run(scenario())


def test_two_tries_that_arrive_together_run_the_action_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        calls: list[str] = []
        release = asyncio.Event()
        release.set()
        once = once_app(tmp_path, calls, release, nothing_after)
        answers = await asyncio.gather(*(call(once, user_scope(b"action-0103")) for _ in range(5)))
        assert calls == ["/api/x"]
        assert {answer[1]["body"] for answer in answers} == {b'{"n": 1}'}

    asyncio.run(scenario())


def test_a_repeat_for_another_request_is_refused_while_the_first_runs(tmp_path: Path) -> None:
    async def scenario() -> None:
        calls: list[str] = []
        release = asyncio.Event()
        once = once_app(tmp_path, calls, release, nothing_after)
        first = asyncio.ensure_future(call(once, user_scope(b"action-0104")))
        await asyncio.sleep(0.05)
        other = await call(once, user_scope(b"action-0104", path="/api/y"))
        assert other[0]["status"] == 400
        release.set()
        await first
        assert calls == ["/api/x"]

    asyncio.run(scenario())


def test_a_dropped_connection_still_stores_the_answer(tmp_path: Path) -> None:
    async def scenario() -> None:
        calls: list[str] = []
        release = asyncio.Event()
        once = once_app(tmp_path, calls, release, nothing_after)
        first = asyncio.ensure_future(call(once, user_scope(b"action-0102")))
        await asyncio.sleep(0.05)
        first.cancel()
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.sleep(0.2)
        repeat = await call(once, user_scope(b"action-0102"))
        assert calls == ["/api/x"]
        assert repeat[1]["body"] == b'{"n": 1}'

    asyncio.run(scenario())


def test_the_answer_goes_before_background_work_and_survives_its_failure(tmp_path: Path) -> None:
    async def scenario() -> None:
        calls: list[str] = []
        release = asyncio.Event()
        release.set()
        background = asyncio.Event()

        async def after() -> None:
            await background.wait()
            raise RuntimeError("delivery failed")

        once = once_app(tmp_path, calls, release, after)
        first = await asyncio.wait_for(call(once, user_scope(b"action-0105")), 2)
        assert first[0]["status"] == 200
        background.set()
        await asyncio.sleep(0.1)
        repeat = await call(once, user_scope(b"action-0105"))
        assert repeat[0]["status"] == 200
        assert repeat[1]["body"] == b'{"n": 1}'
        assert calls == ["/api/x"]

    asyncio.run(scenario())


def test_an_action_cockpit_did_not_settle_is_refused_rather_run_twice(tmp_path: Path, monkeypatch: Any) -> None:
    def broken(conn: sqlite3.Connection, action_id: str, answer: actions.Answer) -> None:
        raise sqlite3.OperationalError("database is locked")

    async def scenario() -> None:
        calls: list[str] = []
        release = asyncio.Event()
        release.set()
        once = once_app(tmp_path, calls, release, nothing_after)
        monkeypatch.setattr(actions, "settle", broken)
        first = await call(once, user_scope(b"action-0106"))
        assert first[0]["status"] == 200
        monkeypatch.undo()
        repeat = await call(once, user_scope(b"action-0106"))
        assert repeat[0]["status"] == 409
        assert b"may or may not have happened" in repeat[1]["body"]
        assert json.loads(repeat[1]["body"])["cut_off"] is True
        assert calls == ["/api/x"]

    asyncio.run(scenario())


def test_a_busy_database_answers_503_so_the_page_sends_again(tmp_path: Path, monkeypatch: Any) -> None:
    def locked(conn: sqlite3.Connection, action_id: str, method: str, path: str) -> actions.Answer | None:
        raise sqlite3.OperationalError("database is locked")

    async def scenario() -> None:
        calls: list[str] = []
        release = asyncio.Event()
        release.set()
        once = once_app(tmp_path, calls, release, nothing_after)
        monkeypatch.setattr(actions, "begin", locked)
        busy = await call(once, user_scope(b"action-0107"))
        assert busy[0]["status"] == 503
        assert calls == []
        monkeypatch.undo()
        again = await call(once, user_scope(b"action-0107"))
        assert again[0]["status"] == 200
        assert calls == ["/api/x"]

    asyncio.run(scenario())
