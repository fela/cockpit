"""The server's log lines, its requests, refused writes, the press trail and stage moves (SPEC TRACK-12 to TRACK-15)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient

from cockpit import cli, db, page_errors, request_stats, server_logs, store
from tests.conftest import ORIGIN, World
from tests.test_complete_task import CLOSING, _finish, _item, _step

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
STARTED = "2026-11-11T11:00:00Z"


def agent_client(world: World) -> TestClient:
    return TestClient(world.client.app, base_url=ORIGIN)


def put_item(world: World, key: str, **fields: Any) -> dict[str, Any]:
    response = agent_client(world).put("/api/items", json={"key": key, "title": "Some task"} | fields,
                                       headers=world.agent())
    assert response.status_code == 200, response.text
    return response.json()


def events(world: World, kind: str) -> list[dict[str, Any]]:
    conn = db.connect(world.db_path)
    try:
        return db.rows(conn.execute("SELECT * FROM events WHERE kind = ? ORDER BY id", (kind,)).fetchall())
    finally:
        conn.close()


def flush(world: World) -> None:
    conn = db.connect(world.db_path)
    try:
        world.client.app.state.requests.flush(conn, datetime.now(UTC))
    finally:
        conn.close()


def log_line(name: str, level: int, message: str) -> logging.LogRecord:
    return logging.LogRecord(name, level, "/src/cockpit/delivery.py", 120, message, None, None, "deliver")


def test_ids_and_numbers_do_not_split_a_kind_of_line() -> None:
    one = server_logs.shape("message 12 to 0b5e6f1c-1d2a-4c3b-9e8f-7a6b5c4d3e2f failed at 1a2b3c4")
    two = server_logs.shape("message 300 to 11111111-2222-4333-8444-555555555555 failed at 5d6e7f8")
    assert one == two == "message N to N failed at N"


def test_different_exceptions_on_one_line_stay_apart(world: World) -> None:
    traceback = "Exception in ASGI application\nTraceback (most recent call last):\n  File \"app.py\", line {}\n{}\n"
    conn = db.connect(world.db_path)
    try:
        for line, error in ((10, "ValueError: bad stage"), (20, "KeyError: 'item'"), (30, "ValueError: bad stage")):
            server_logs.record_line(conn, logging.ERROR, "uvicorn.error", "uvicorn.error run:1",
                               traceback.format(line, error), NOW, STARTED, False)
        errors = page_errors.listing(conn, False)["errors"]
    finally:
        conn.close()
    assert sorted((error["message"], error["count"]) for error in errors) == [
        ("Exception in ASGI application | KeyError: 'item'", 1),
        ("Exception in ASGI application | ValueError: bad stage", 2),
    ]


def test_warnings_count_per_kind_and_errors_go_to_the_page_errors(world: World) -> None:
    conn = db.connect(world.db_path)
    try:
        for number in (1, 2, 3):
            server_logs.record_line(conn, logging.WARNING, "cockpit.delivery", "x", f"slow delivery {number}", NOW, STARTED, False)
        server_logs.record_line(conn, logging.ERROR, "uvicorn.error", "uvicorn.error run:1",
                           "Exception in ASGI application\nTraceback (most recent call last):\n  boom", NOW, STARTED, False)
        report = server_logs.report(conn, 7, NOW)
        errors = page_errors.listing(conn, False)
    finally:
        conn.close()
    assert report["totals"] == {"WARNING": 3, "ERROR": 1, "CRITICAL": 0}
    assert report["lines"][0] | {"last_at": None} == {
        "level": "WARNING", "logger": "cockpit.delivery", "message": "slow delivery N", "count": 3, "days": 1,
        "last_at": None,
    }
    assert errors["total"] == 1
    assert errors["errors"][0]["page"] == "server"
    assert errors["errors"][0]["message"] == "Exception in ASGI application | boom"
    assert "boom" in errors["errors"][0]["stack"]
    text = cli.server_logs_text(report)
    assert "3 warnings, 1 errors, 0 critical, in 2 kinds of line" in text
    assert "WARNING  cockpit.delivery: slow delivery N" in text


def test_the_capture_skips_the_page_errors_own_lines_and_info(world: World) -> None:
    capture = server_logs.Capture(world.db_path, STARTED)
    for name, level, message in [(page_errors.__name__, logging.ERROR, "page error on / (x): y"),
                                 ("cockpit.delivery", logging.INFO, "delivered"),
                                 ("cockpit.delivery", logging.WARNING, "no live session for 4")]:
        logger = logging.getLogger(f"tests.capture.{name}")
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.addHandler(capture)
        try:
            logger.handle(log_line(name, level, message))
        finally:
            logger.removeHandler(capture)
    conn = db.connect(world.db_path)
    try:
        report = server_logs.report(conn, 1, datetime.now(UTC))
    finally:
        conn.close()
    assert [(line["logger"], line["message"]) for line in report["lines"]] == [("cockpit.delivery", "no live session for N")]


def test_an_error_logged_as_the_server_stops_is_counted_but_not_a_page_error(world: World) -> None:
    capture = server_logs.Capture(world.db_path, STARTED)
    logger = logging.getLogger("tests.capture.stopping")
    logger.propagate = False
    logger.addHandler(capture)
    stopped = datetime.now(UTC)
    capture.stopping_at = stopped
    try:
        for seconds, message in [(-10, "a read failed before the stop"), (-1, "the answer was cut off"),
                                 (1, "the other machine does not answer")]:
            line = log_line("cockpit.reader", logging.ERROR, message)
            line.created = (stopped + timedelta(seconds=seconds)).timestamp()
            logger.handle(line)
    finally:
        logger.removeHandler(capture)
    conn = db.connect(world.db_path)
    try:
        assert server_logs.report(conn, 1, datetime.now(UTC))["totals"]["ERROR"] == 3
        assert [error["message"] for error in page_errors.listing(conn, True)["errors"]] == ["a read failed before the stop"]
    finally:
        conn.close()


def test_a_line_written_after_a_later_one_keeps_the_latest_time(world: World) -> None:
    conn = db.connect(world.db_path)
    try:
        for minutes in (0, -1):
            server_logs.record_line(conn, logging.WARNING, "cockpit.delivery", "x", "slow delivery",
                                    NOW + timedelta(minutes=minutes), STARTED, False)
        assert server_logs.report(conn, 1, NOW)["lines"][0]["last_at"] == "2026-11-11T12:00:00Z"
    finally:
        conn.close()


def test_an_error_taken_for_fixing_opens_again_after_a_restart(world: World) -> None:
    conn = db.connect(world.db_path)
    try:
        server_logs.record_line(conn, logging.ERROR, "cockpit.app", "cockpit.app f:1", "it broke", NOW, STARTED, False)
        error = page_errors.listing(conn, False)["errors"][0]
        page_errors.take(conn, [error["id"]], "fix session")
        server_logs.record_line(conn, logging.ERROR, "cockpit.app", "cockpit.app f:1", "it broke", NOW, STARTED, False)
        assert page_errors.open_count(conn) == 0
        restarted = (datetime.now(UTC) + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        server_logs.record_line(conn, logging.ERROR, "cockpit.app", "cockpit.app f:1", "it broke", NOW, restarted, False)
        assert page_errors.open_count(conn) == 1
    finally:
        conn.close()


def test_requests_count_per_route_and_caller(world: World) -> None:
    for _ in range(3):
        assert world.client.get("/api/server-logs?days=1").status_code == 200
    assert agent_client(world).get("/api/server-logs?days=1", headers=world.agent()).status_code == 200
    world.client.get("/static/nothing-here.js")
    world.client.get("/api/no-such-route")
    flush(world)
    report = world.client.get("/api/request-stats?days=1").json()
    by_route = {(route["method"], route["route"]): route for route in report["routes"]}
    logs = by_route[("GET", "/api/server-logs")]
    assert logs["count"] == 4
    assert sum(logs["by_caller"].values()) == 4
    assert len(logs["by_caller"]) == 2
    assert logs["statuses"] == {"200": 4}
    assert ("GET", "/static") in by_route
    assert by_route[("GET", "(none)")]["refused"] == 1
    assert "GET /api/server-logs" in cli.request_stats_text(report)


def test_requests_the_guard_refuses_keep_their_route(world: World) -> None:
    signed_out = TestClient(world.client.app, base_url=ORIGIN)
    assert signed_out.get("/api/items/7/history").status_code == 401
    unnamed = {"authorization": world.agent()["authorization"]}
    assert signed_out.put("/api/items", json={"key": "note:x"}, headers=unnamed).status_code == 400
    flush(world)
    by_route = {(route["method"], route["route"]): route
                for route in world.client.get("/api/request-stats?days=1").json()["routes"]}
    assert by_route[("GET", "/api/items/{item_id}/history")]["by_caller"] == {"anonymous": 1}
    assert by_route[("PUT", "/api/items")]["refused"] == 1
    assert ("GET", "(none)") not in by_route
    # The unnamed agent's write counts above; only a refusal with a named caller is an event.
    assert events(world, "request.refused") == []


def test_a_user_write_the_guard_refuses_is_an_event_with_its_route(world: World) -> None:
    elsewhere = {"origin": "https://elsewhere.example"}
    assert world.client.put("/api/items", json={"key": "note:x"}, headers=elsewhere).status_code == 403
    refused = events(world, "request.refused")
    assert [(event["actor"], event["data"]["route"], event["data"]["status"]) for event in refused] == [
        ("user", "/api/items", 403)]


def test_a_flush_that_fails_keeps_its_counts() -> None:
    recorder = request_stats.Recorder()
    recorder.add(NOW, "GET", "/api/state", 200, "user", 30)

    class Broken:
        in_transaction = False

        def execute(self, *_: Any) -> None:
            raise RuntimeError("database is locked")

    try:
        recorder.flush(Broken(), NOW)  # type: ignore[arg-type]
    except RuntimeError:
        pass
    recorder.add(NOW, "GET", "/api/state", 200, "user", 1500)
    tally = recorder.tallies[("2026-11-11", "GET", "/api/state", 200, "user")]
    assert (tally.count, tally.total_ms, tally.max_ms, tally.slow) == (2, 1530, 1500, 1)


def test_a_refused_write_is_an_event_with_its_reason(world: World) -> None:
    response = agent_client(world).put("/api/items", json={"key": "gh:example-org/app#1"}, headers=world.agent("fixer"))
    assert response.status_code == 400
    assert response.json()["detail"] == "a new item needs a title"
    refused = events(world, "request.refused")
    assert len(refused) == 1
    assert refused[0]["actor"] == "agent:fixer"
    assert refused[0]["data"] == {
        "method": "PUT", "route": "/api/items", "status": 400, "detail": "a new item needs a title",
    }


def test_repeated_refusals_show_in_the_patterns(world: World) -> None:
    for _ in range(3):
        agent_client(world).put("/api/items", json={"key": "gh:example-org/app#1"}, headers=world.agent())
    repeats = world.client.get("/api/patterns?days=7").json()["repeats"]
    assert "3 writes by agents to PUT /api/items refused: a new item needs a title" in repeats


def test_presses_keep_their_order_task_and_step(world: World) -> None:
    item = put_item(world, "note:press-trail", stage="building")
    step = agent_client(world).post(
        "/api/steps", json={"item_id": item["id"], "need": "decide", "ask": "Which one?"}, headers=world.agent()
    ).json()["step"]
    presses = [
        {"at": "2026-11-11T10:00:00.120Z", "control": "card answer", "item_id": None, "step_id": step["id"]},
        {"at": "2026-11-11T10:00:05Z", "control": "header button", "item_id": None, "step_id": None},
    ]
    body = {"batch": "b1", "page": "/", "uses": [{"control": "card answer", "kind": "press", "count": 1}],
            "presses": presses}
    assert world.client.post("/api/control-uses", json=body, headers=world.user()).status_code == 200
    report = world.client.get("/api/page-presses?days=90").json()
    assert [(press["control"], press["item_id"], press["step_id"]) for press in report["presses"]] == [
        ("card answer", item["id"], step["id"]), ("header button", None, None),
    ]
    on_item = world.client.get(f"/api/page-presses?days=90&item={item['id']}").json()
    assert on_item["total"] == 1
    assert f"card answer item {item['id']} step {step['id']}" in cli.presses_text(report)


def test_presses_older_than_the_window_go(world: World) -> None:
    old = (datetime.now(UTC) - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {"batch": "b1", "page": "/", "uses": [],
            "presses": [{"at": old, "control": "x", "item_id": None, "step_id": None}]}
    world.client.post("/api/control-uses", json=body, headers=world.user())
    body = {"batch": "b2", "page": "/", "uses": [], "presses": []}
    world.client.post("/api/control-uses", json=body, headers=world.user())
    conn = db.connect(world.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM page_presses").fetchone()[0] == 0
    finally:
        conn.close()


def test_each_stage_move_is_an_event(world: World) -> None:
    item = put_item(world, "note:stages", stage="planning")
    put_item(world, "note:stages", stage="planning", summary="still planning")
    put_item(world, "note:stages", stage="building")
    moves = [json.dumps(event["data"]) for event in events(world, "item.stage") if event["item_id"] == item["id"]]
    assert moves == ['{"from": null, "to": "planning"}', '{"from": "planning", "to": "building"}']


def test_completing_undoing_and_reopening_by_message_are_stage_moves(world: World) -> None:
    item_id = _item(world)
    step = _step(world, "reply:1", [CLOSING])
    assert _finish(world, item_id, step["id"], "complete", None, "done").status_code == 200
    assert world.client.post(f"/api/items/{item_id}/reopen", headers=world.user()).status_code == 200
    assert _finish(world, item_id, step["id"], "complete", None, "done").status_code == 200
    conn = db.connect(world.db_path)
    try:
        assert store.reopen_for_message(conn, item_id) is True
    finally:
        conn.close()
    moves = [(event["actor"], event["data"]["from"], event["data"]["to"])
             for event in events(world, "item.stage") if event["item_id"] == item_id]
    assert moves == [
        ("agent:sweep", None, "review"), ("user", "review", "done"), ("user", "done", "review"),
        ("user", "review", "done"), ("user", "done", "building"),
    ]
