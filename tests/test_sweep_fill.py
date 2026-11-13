from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from cockpit import cli, db, patterns, store, sweeper
from tests.conftest import ORIGIN, SESSION_B, World
from tests.test_sweeper import iso, put_steps, run_sql


def fill(world: World, exists: sweeper.SessionExists) -> dict | None:
    conn = db.connect(world.db_path)
    try:
        message_id = sweeper.fill_once(conn, world.checkout, exists)
        return store.get_message(conn, message_id) if message_id is not None else None
    finally:
        conn.close()


def age_launches(world: World, minutes: int) -> None:
    """Move every message, and the last sweep's start and end, `minutes` into the past."""
    past = iso(datetime.now(UTC) - timedelta(minutes=minutes))
    run_sql(world, "UPDATE messages SET created_at = ?, sent_at = ?", (past, past))
    conn = db.connect(world.db_path)
    try:
        sweep = db.kv_get(conn, "last_sweep")
        if sweep is not None:
            db.kv_set(conn, "last_sweep", sweep | {"started_at": past, "finished_at": past})
        if db.kv_get(conn, "sweep_launched_at") is not None:
            db.kv_set(conn, "sweep_launched_at", past)
    finally:
        conn.close()


def test_a_queue_about_to_run_dry_the_user_looks_at_gets_a_quick_sweep_every_fill_interval(world: World) -> None:
    assert fill(world, lambda session_id: False) is None  # The user has not opened the page

    world.client.get("/api/state")
    message = fill(world, lambda session_id: False)
    assert message is not None
    assert (message["author"], message["new_name"]) == ("cockpit", "Cockpit sweep")
    assert message["text"].startswith("Cockpit fill: Sam's queue is about to run dry: it holds at most one step besides early looks")
    assert "prepare new work as a step when little else is coming" in message["text"]
    assert 'run a full sweep (section "Sweep"; none finished in the last 3 hours)' in message["text"]
    assert fill(world, lambda session_id: False) is None  # the session is still starting

    world.deliverer.send(message["id"])
    agent = TestClient(world.client.app, base_url=ORIGIN)
    sweep = agent.post("/api/sweeps/start", json={"reorder": False}, headers=world.agent("sweep")).json()
    agent.post("/api/sweeps/finish", json={"sweep_id": sweep["id"], "summary": "Fill: nothing new", "error": ""}, headers=world.agent("sweep"))
    assert fill(world, lambda session_id: True) is None  # a sweep started within the interval

    age_launches(world, sweeper.FILL_EVERY_MINUTES + 1)
    again = fill(world, lambda session_id: session_id == SESSION_B)
    assert again is not None and again["session_id"] == SESSION_B  # the same sweeper session
    world.deliverer.send(again["id"])

    age_launches(world, sweeper.FILL_EVERY_MINUTES + 1)
    steps = put_steps(world, 2)
    assert fill(world, lambda session_id: True) is None  # two steps wait for the user
    world.client.post(f"/api/steps/{steps[0]['id']}/done", json={}, headers=world.user())
    assert fill(world, lambda session_id: True) is not None  # one step left: the queue is about to run dry

    age_launches(world, sweeper.FILL_EVERY_MINUTES + 1)
    conn = db.connect(world.db_path)
    try:
        db.kv_set(conn, "sweeps_paused", {"at": iso(datetime.now(UTC)), "away_since": iso(datetime.now(UTC))})
    finally:
        conn.close()
    assert fill(world, lambda session_id: True) is None  # sweeps are stopped


def test_sweep_times_show_the_median_and_slowest_per_kind_for_the_last_week(world: World) -> None:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    assert world.client.get("/api/state").json()["sweep_times"] == {"full": None, "light": None, "fill": None, "reorder": None}
    sweeps = (("Full: ranked 9", ""), ("Light: one step", ""), ("Light: two steps", ""), ("Light: three", ""), ("", "gh is down"), ("Fill: GitHub 1", ""))
    for summary, error in sweeps:
        sweep = agent.post("/api/sweeps/start", json={"reorder": False}, headers=world.agent("sweep")).json()
        agent.post("/api/sweeps/finish", json={"sweep_id": sweep["id"], "summary": summary, "error": error}, headers=world.agent("sweep"))
    # The sweeps take 10, 20, 30, 40, 50 and 60 s.
    now = datetime.now(UTC)
    conn = db.connect(world.db_path)
    try:
        rows = conn.execute("SELECT id FROM events WHERE kind IN ('sweep.started', 'sweep.finished') ORDER BY id").fetchall()
    finally:
        conn.close()
    for pair in range(0, len(rows), 2):
        start = now - timedelta(minutes=60 - pair)
        run_sql(world, "UPDATE events SET at = ? WHERE id = ?", (iso(start), rows[pair]["id"]))
        run_sql(world, "UPDATE events SET at = ? WHERE id = ?", (iso(start + timedelta(seconds=5 * (pair + 2))), rows[pair + 1]["id"]))
    # A light sweep older than the window is left out.
    run_sql(world, "UPDATE events SET at = ? WHERE id IN (?, ?)", (iso(now - timedelta(days=patterns.SWEEP_TIME_DAYS, minutes=1)), rows[6]["id"], rows[7]["id"]))

    state = world.client.get("/api/state").json()
    assert state["sweep_time_days"] == patterns.SWEEP_TIME_DAYS
    assert state["sweep_times"] == {
        "full": {"count": 1, "median_seconds": 10, "slowest_seconds": 10},
        "light": {"count": 2, "median_seconds": 25, "slowest_seconds": 30},
        "fill": {"count": 1, "median_seconds": 60, "slowest_seconds": 60},
        "reorder": None,
    }
    report = world.client.get("/api/patterns", params={"days": 7}).json()
    assert "Light sweeps: 2, median 25 s, slowest 30 s" in cli.patterns_text(report)


def test_a_check_that_gives_no_verdict_does_not_put_off_a_fill(world: World) -> None:
    world.client.get("/api/state")
    step = put_steps(world, 1)[0]
    conn = db.connect(world.db_path)
    try:
        check = sweeper.launch(conn, "cockpit", "Cockpit check", world.checkout, lambda session_id: False, "check", [step["id"]])
    finally:
        conn.close()
    world.deliverer.send(check["id"])
    assert fill(world, lambda session_id: True) is None  # the check is under way
    # The check gave no verdict and timed out; a check is retried every few minutes, which must not hold off the fill.
    past = iso(datetime.now(UTC) - timedelta(minutes=sweeper.CHECK_MINUTES + 1))
    run_sql(world, "UPDATE messages SET created_at = ?, sent_at = ? WHERE id = ?", (past, past, check["id"]))
    assert fill(world, lambda session_id: True) is not None
