"""What each step carries about the user's snoozes and time on its task (SPEC RANK-22)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cockpit import cli, db, store
from tests.conftest import ORIGIN, SESSION_A, World

AT = "2026-01-06T08:00:00Z"
NOW = datetime(2026, 1, 6, 20, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "cockpit.db")
    db.migrate(conn)
    conn.execute(
        "INSERT INTO items (id, key, title, created_at, updated_at, updated_by) VALUES"
        " (1, 'note:plan', 'Plan', ?, ?, 'user'), (2, 'note:other', 'Other', ?, ?, 'user')",
        (AT, AT, AT, AT),
    )
    conn.execute(
        "INSERT INTO steps (id, item_id, need, ask, created_at, updated_at, updated_by) VALUES"
        " (1, 1, 'define', 'Define', ?, ?, 's'), (2, 1, 'decide:plan', 'Decide', ?, ?, 's'),"
        " (3, 2, 'define', 'Define', ?, ?, 's')",
        (AT, AT, AT, AT, AT, AT),
    )
    yield conn
    conn.close()


def _snooze(conn: sqlite3.Connection, step_id: int, at: str, aside: bool, actor: str = "user") -> None:
    data = {"aside": True} if aside else {}
    conn.execute(
        "INSERT INTO events (at, actor, kind, item_id, step_id, need, data) VALUES (?, ?, 'step.snooze', 1, ?, '', ?)",
        (at, actor, step_id, json.dumps(data)),
    )


def _span(conn: sqlite3.Connection, step_id: int, started_at: str, ended_at: str | None) -> None:
    conn.execute(
        "INSERT INTO time_spans (step_id, started_at, ended_at, ended_by) VALUES (?, ?, ?, ?)",
        (step_id, started_at, ended_at, ended_at and "pause"),
    )


def _mark(conn: sqlite3.Connection, step_id: int, at: str, progress: int, of_user_time: int) -> None:
    conn.execute(
        "INSERT INTO progress_marks (step_id, item_id, at, progress, of_user_time) VALUES (?, 1, ?, ?, ?)",
        (step_id, at, progress, of_user_time),
    )


def test_snoozes_count_across_the_task_s_steps_leaving_out_an_undo(conn: sqlite3.Connection) -> None:
    _snooze(conn, 1, "2026-01-06T09:00:00Z", aside=True)
    # Woken hours later: the snooze stands.
    _snooze(conn, 1, "2026-01-06T12:00:00Z", aside=False)
    _snooze(conn, 2, "2026-01-06T13:59:45Z", aside=True)
    # Undo a few seconds after: the snooze is taken back.
    _snooze(conn, 2, "2026-01-06T14:00:00Z", aside=True)
    _snooze(conn, 2, "2026-01-06T14:00:05Z", aside=False)
    # A repeated wake takes back nothing more.
    _snooze(conn, 2, "2026-01-06T14:00:10Z", aside=False)
    # An agent's snooze is not the user's.
    _snooze(conn, 1, "2026-01-06T15:00:00Z", aside=True, actor="agent")
    # Only the items asked for are read.
    assert store._item_efforts(conn, {2}, NOW) == {}
    effort = store._item_efforts(conn, {1, 2}, NOW)[1]
    assert effort["snoozes"] == 2
    assert effort["last_snoozed_at"] == "2026-01-06T13:59:45Z"
    assert 2 not in store._item_efforts(conn, {1, 2}, NOW)


def test_snoozes_are_read_through_the_item_first_index(conn: sqlite3.Connection) -> None:
    # One snooze on this task and 10,000 on another: reading this task's effort skips the other task's snoozes.
    _snooze(conn, 1, "2026-01-06T09:00:00Z", aside=True)
    conn.executemany(
        "INSERT INTO events (at, actor, kind, item_id, step_id, need, data) VALUES (?, 'user', 'step.snooze', 2, 3, '', ?)",
        [(f"2026-01-06T10:{n // 600:02d}:{n % 60:02d}Z", json.dumps({"aside": True})) for n in range(10_000)],
    )
    conn.execute("ANALYZE")
    ticks = []
    conn.set_progress_handler(lambda: ticks.append(1), 100)
    assert store._item_efforts(conn, {1}, NOW)[1]["snoozes"] == 1
    conn.set_progress_handler(None, 100)
    # Scanning every snooze took about 1,400 ticks of 100 SQLite instructions.
    assert len(ticks) < 50


def test_minutes_since_progress_count_the_user_s_time_after_the_estimate_last_rose(conn: sqlite3.Connection) -> None:
    # No estimate yet: all of the user's time counts.
    _span(conn, 1, "2026-01-06T09:00:00Z", "2026-01-06T09:30:00Z")
    assert store._item_efforts(conn, {1, 2}, NOW)[1] == store.NO_EFFORT | {"user_minutes": 30, "minutes_since_progress": 30}
    # The first estimate is where the task started; it rose at 09:10, and the time after that counts. An estimate of
    # the earlier kind (how far the work was) at 08:40 is no rise.
    _mark(conn, 1, "2026-01-06T08:30:00Z", 10, 1)
    _mark(conn, 1, "2026-01-06T08:40:00Z", 90, 0)
    _mark(conn, 1, "2026-01-06T09:10:00Z", 40, 1)
    _mark(conn, 2, "2026-01-06T09:20:00Z", 40, 1)
    # A later lower estimate, then 20 minutes on another step, then a running timer since 19:45.
    _mark(conn, 2, "2026-01-06T10:00:00Z", 30, 1)
    _span(conn, 2, "2026-01-06T11:00:00Z", "2026-01-06T11:20:00Z")
    _span(conn, 2, "2026-01-06T19:45:00Z", None)
    effort = store._item_efforts(conn, {1, 2}, NOW)[1]
    assert effort["user_minutes"] == 30 + 20 + 15
    assert effort["progress"] == 30
    assert effort["minutes_since_progress"] == 20 + 20 + 15


def test_each_step_carries_its_task_s_effort_and_the_cli_shows_it(world: World) -> None:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    for key in ("note:plan", "note:quiet"):
        agent.put("/api/items", json={"key": key, "title": key[5:], "sessions": [SESSION_A]}, headers=world.agent())
    plan = agent.post(
        "/api/steps", json={"item_key": "note:plan", "need": "define", "ask": "Define the plan", "progress": 10},
        headers=world.agent(),
    ).json()["step"]["id"]
    agent.post("/api/steps", json={"item_key": "note:quiet", "need": "define", "ask": "Define it"}, headers=world.agent())
    world.client.post(f"/api/steps/{plan}/snooze", json={"until": "2099-01-01T08:00:00Z"}, headers=world.user())
    state = world.client.get("/api/state").json()
    steps = {step["ask"]: step for group in ("queue", "ready_next", "snoozed") for step in state[group]}
    assert steps["Define the plan"]["effort"]["snoozes"] == 1
    assert steps["Define the plan"]["effort"]["progress"] == 10
    assert steps["Define it"]["effort"] == store.NO_EFFORT
    text = cli.summary(state)
    assert "    snoozed once, last " in text.split("Define the plan")[1].split("\n")[1]
    assert "snoozed" not in text.split("Queue (")[1].split("Snoozed:")[0]


@pytest.mark.parametrize(
    ("step_progress", "task_progress", "words"),
    [(40, 70, "progress 40%"), (None, 70, "progress unset (task at 70%)"), (None, 0, "progress unset (task at 0%)"),
     (None, None, "progress unset")],
)
def test_the_cli_shows_the_task_s_estimate_when_the_step_has_none(
    step_progress: int | None, task_progress: int | None, words: str
) -> None:
    step = {"size": "small", "progress": step_progress, "effort": store.NO_EFFORT | {"progress": task_progress}}
    assert cli._bar_words(step) == f"size small, {words}"
