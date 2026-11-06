"""Cockpit asks for a full sweep when none finished in the last few hours (SPEC SCOPE-3)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from cockpit import cli, db, patterns, sweeper
from tests.conftest import ORIGIN, SESSION_A, World
from tests.test_sweeper import age_everything, iso, run_sql, wake


def finish(world: World, summary: str, error: str = "") -> dict:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    sweep = agent.post("/api/sweeps/start", json={"reorder": False}, headers=world.agent("sweep")).json()
    response = agent.post("/api/sweeps/finish", json={"sweep_id": sweep["id"], "summary": summary, "error": error}, headers=world.agent("sweep"))
    assert response.status_code == 200, response.text
    return response.json()


def test_a_wake_up_asks_for_a_full_sweep_until_one_finishes_and_a_light_finish_warns(world: World, monkeypatch, capsys) -> None:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    agent.post("/api/sweeps/request", json={"reason": "PR 30 is green"}, headers=world.agent("app-1"))
    age_everything(world, 2)
    first = wake(world, lambda session_id: False)
    assert 'run a full sweep (none finished in the last 3 hours) (section "Sweep")' in first["text"]
    world.deliverer.send(first["id"])

    light = finish(world, "Light: 2 steps for Sam")
    assert light["warning"].startswith("no full sweep finished in the last 3 hours")
    assert finish(world, "", "gh is down")["warning"] == ""  # a failed sweep is no light sweep
    assert finish(world, "Fill: GitHub 0")["warning"].startswith("no full sweep finished")
    assert finish(world, "Full: half done", "Slack is down")["warning"] == ""
    assert finish(world, "Light: after it")["warning"]  # a failed full sweep does not count
    assert finish(world, "Full: 3 steps for Sam")["warning"] == ""
    assert finish(world, "Light: nothing new")["warning"] == ""  # a full sweep finished just now

    agent.post("/api/sweeps/request", json={"reason": "PR 40 is green"}, headers=world.agent("app-1"))
    age_everything(world, 10)
    light_wake = wake(world, lambda session_id: True)
    assert 'run a light sweep (section "Sweep")' in light_wake["text"]

    assert 'run a quick sweep (section "Fill")' in sweeper.fill_text(False)

    # The full sweep's finish falls out of the window.
    old = iso(datetime.now(UTC) - timedelta(hours=patterns.FULL_SWEEP_HOURS, minutes=1))
    run_sql(world, "UPDATE events SET at = ? WHERE kind = 'sweep.finished'", (old,))
    assert finish(world, "Light: one more")["warning"].startswith("no full sweep finished")
    conn = db.connect(world.db_path)
    try:
        assert "run a full sweep (none finished" in sweeper.wake_text(conn, [], datetime.now(UTC))
    finally:
        conn.close()
    monkeypatch.setattr(cli, "call", lambda method, path, body, agent: {"warning": "no full sweep finished"})
    cli.main(["sweep", "finish", "abc", "--as", "sweep", "--summary", "Light: one"])
    assert "cockpit: warning: no full sweep finished" in capsys.readouterr().err


def test_a_due_full_sweep_wakes_the_sweeper_on_a_quiet_board_while_the_user_looks(world: World) -> None:
    finish(world, "Light: nothing new")
    old = iso(datetime.now(UTC) - timedelta(minutes=sweeper.SLOW_GAP_MINUTES + 1))
    conn = db.connect(world.db_path)
    try:
        db.kv_set(conn, "last_sweep", db.kv_get(conn, "last_sweep") | {"started_at": old, "finished_at": old})
    finally:
        conn.close()
    assert wake(world, lambda session_id: False) is None  # the user is not looking
    world.client.get("/api/state")
    message = wake(world, lambda session_id: False)
    assert message is not None and message["text"].startswith("Cockpit wake-up: the board is quiet, and Sam has Cockpit open.")
    assert 'run a full sweep (none finished in the last 3 hours) (section "Sweep")' in message["text"]
    # Its session took it but ran no full sweep: the next wake waits the slow gap.
    world.deliverer.send(message["id"])
    sent = iso(datetime.now(UTC) - timedelta(minutes=sweeper.CHECK_IN_MINUTES + 1))
    run_sql(world, "UPDATE messages SET created_at = ?, sent_at = ?", (sent, sent))
    assert wake(world, lambda session_id: True) is None
    run_sql(world, "UPDATE kv SET value = ? WHERE key = 'sweep_launched_at'", (json.dumps(old),))
    # A check launched since, and retried every few minutes when it times out, does not put the full sweep off.
    run_sql(
        world, "INSERT INTO messages (author, session_id, text, state, created_at, sent_at, as_prompt)"
        " VALUES ('cockpit', ?, 'Cockpit check', 'sent', ?, ?, 0)",
        (SESSION_A, iso(datetime.now(UTC)), iso(datetime.now(UTC))),
    )
    run_sql(
        world, "UPDATE kv SET value = json_object('message_id', (SELECT MAX(id) FROM messages), 'kind', 'check',"
        " 'step_ids', json('[]')) WHERE key = 'sweep_launch'",
    )
    assert wake(world, lambda session_id: True) is not None
    # A full sweep that finished in the last hours leaves a quiet board alone.
    run_sql(world, "DELETE FROM kv WHERE key IN ('sweep_launch', 'sweep_launched_at')")
    finish(world, "Full: 3 steps for Sam")
    conn = db.connect(world.db_path)
    try:
        db.kv_set(conn, "last_sweep", db.kv_get(conn, "last_sweep") | {"started_at": old, "finished_at": old})
    finally:
        conn.close()
    assert wake(world, lambda session_id: True) is None
