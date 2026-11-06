"""When the user last saw an item, and how that moves its steps (SPEC RANK-14)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from cockpit import db
from tests.conftest import ORIGIN, SESSION_A, World
from tests.test_db import database_at

ITEM = "gh:example-org/app#10"


def _step(world: World, need: str) -> tuple[int, int]:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    item = agent.put("/api/items", json={"key": ITEM, "title": "PR 10", "sessions": [SESSION_A]}, headers=world.agent()).json()
    step = agent.post("/api/steps", json={"item_key": ITEM, "need": need, "ask": "Review"}, headers=world.agent()).json()["step"]
    return item["id"], step["id"]


def _seen(world: World, item_id: int) -> tuple[str | None, int]:
    conn = sqlite3.connect(world.db_path)
    try:
        return conn.execute("SELECT seen_at, set_aside FROM items WHERE id = ?", (item_id,)).fetchone()
    finally:
        conn.close()


def _sql(world: World, sql: str, *args: object) -> None:
    conn = sqlite3.connect(world.db_path)
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_playing_a_step_or_acting_on_it_marks_its_item_seen_and_agents_do_not(world: World) -> None:
    item_id, step_id = _step(world, "review@aaaa111")
    assert _seen(world, item_id) == (None, 0)
    world.client.post(f"/api/steps/{step_id}/done", json={"note": "merged"}, headers=world.agent())
    assert _seen(world, item_id) == (None, 0)
    world.client.post(f"/api/steps/{step_id}/reopen", json={}, headers=world.user())
    seen, aside = _seen(world, item_id)
    assert seen is not None and aside == 0
    _sql(world, "UPDATE items SET seen_at = NULL WHERE id = ?", item_id)
    assert world.client.post(f"/api/steps/{step_id}/timer/start", headers=world.user()).status_code == 200
    assert _seen(world, item_id)[0] is not None


def test_a_snooze_or_a_lowered_priority_sets_the_item_aside(world: World) -> None:
    item_id, step_id = _step(world, "review@aaaa111")
    later = _iso(datetime.now(UTC) + timedelta(hours=1))
    world.client.post(f"/api/steps/{step_id}/snooze", json={"until": later}, headers=world.user())
    assert _seen(world, item_id)[1] == 1
    world.client.post(f"/api/steps/{step_id}/snooze", json={"until": None}, headers=world.user())
    assert _seen(world, item_id)[1] == 0
    world.client.patch(f"/api/items/{item_id}", json={"priority": "low"}, headers=world.user())
    assert _seen(world, item_id)[1] == 1
    world.client.patch(f"/api/items/{item_id}", json={"priority": "high"}, headers=world.user())
    assert _seen(world, item_id)[1] == 0


def test_the_migration_fills_seen_from_the_event_log_and_the_timers(tmp_path: Path) -> None:
    # The database just before the migration that added seen_at.
    version = next(n for n, script in enumerate(db.MIGRATIONS) if "ADD COLUMN seen_at" in script)
    conn = database_at(tmp_path / "old.db", version)
    at = "2026-01-04T09:00:00Z"
    for item_id, priority in [(1, "normal"), (2, "normal"), (3, "low"), (4, "normal"), (5, "high"), (6, "urgent")]:
        conn.execute(
            "INSERT INTO items (id, key, title, priority, created_at, updated_at, updated_by) VALUES (?, ?, 'T', ?, ?, ?, 'agent:x')",
            (item_id, f"note:{item_id}", priority, at, at),
        )
    for step_id, item_id, snoozed in [(11, 1, None), (12, 2, "2026-01-09T00:00:00Z"), (14, 4, None)]:
        conn.execute(
            "INSERT INTO steps (id, item_id, need, ask, snoozed_until, created_at, updated_at, updated_by) VALUES (?, ?, 'n', 'a', ?, ?, ?, 'agent:x')",
            (step_id, item_id, snoozed, at, at),
        )
    events = [
        ("2026-01-05T10:00:00Z", "step.hand_off", 1, 11, "{}"),
        ("2026-01-06T10:00:00Z", "step.snooze", 2, 12, "{}"),
        ("2026-01-07T10:00:00Z", "item.edited", 3, None, '{"fields": ["priority"]}'),
        ("2026-01-05T10:00:00Z", "step.snooze", 4, 14, "{}"),
        ("2026-01-07T10:00:00Z", "item.edited", 5, None, '{"fields": ["priority"]}'),
        ("2026-01-07T10:00:00Z", "item.edited", 6, None, '{"fields": ["priority"]}'),
    ]
    for when, kind, item_id, step_id, data in events:
        conn.execute("INSERT INTO events (at, actor, kind, item_id, step_id, data) VALUES (?, 'user', ?, ?, ?, ?)", (when, kind, item_id, step_id, data))
    conn.execute("INSERT INTO time_spans (step_id, started_at, ended_at, ended_by) VALUES (11, '2026-01-08T10:00:00Z', '2026-01-08T10:05:00Z', 'pause')")
    conn.close()
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        seen = {row["id"]: (row["seen_at"], row["set_aside"]) for row in conn.execute("SELECT id, seen_at, set_aside FROM items")}
    finally:
        conn.close()
    assert seen == {
        1: ("2026-01-08T10:00:00Z", 0),  # the timer is the latest look
        2: ("2026-01-06T10:00:00Z", 1),  # snoozed
        3: ("2026-01-07T10:00:00Z", 1),  # lowered to low
        4: ("2026-01-05T10:00:00Z", 0),  # a snooze undone since
        5: ("2026-01-07T10:00:00Z", 1),  # may have been lowered from urgent: set aside
        6: ("2026-01-07T10:00:00Z", 0),  # raised to urgent
    }
