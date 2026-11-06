"""A session Cockpit started on an item goes by the item's title now (SPEC SESSION-3)."""

from __future__ import annotations

from dataclasses import replace

from cockpit import db, store
from tests.conftest import SESSION_A, SESSION_B, World, make_session
from tests.test_api import add_item, run_sql

FIRST_WORDS = "Please add a way to export every list as a text file, including its completed tasks"


def test_a_session_started_on_a_to_do_takes_the_title_an_agent_gives_it(world: World) -> None:
    item = add_item(world, FIRST_WORDS)
    conn = db.connect(world.db_path)
    try:
        target = {"item_id": item["id"], "step_id": None, "session_id": None, "new_cwd": str(world.checkout)}
        started = store.create_message(conn, "user", FIRST_WORDS, target, world.checkout.parent)
    finally:
        conn.close()
    assert started["new_name"] == FIRST_WORDS[:store.NEW_SESSION_NAME_CHARS]
    run_sql(world, "UPDATE messages SET session_id = ?, via = 'new' WHERE id = ?", (SESSION_A, started["id"]))
    run_sql(world, "UPDATE items SET title = ? WHERE id = ?", ("Export completed tasks", item["id"]))
    named = replace(make_session(SESSION_A, pid=None), name=started["new_name"])
    # A session someone renamed in the terminal keeps its own name; one Cockpit did not start keeps `claude agents`'.
    renamed = replace(make_session(SESSION_B, pid=None), name="Reminder copy")
    world.cache.sessions = [named, renamed]
    run_sql(world, "INSERT INTO messages (author, item_id, session_id, new_cwd, new_name, text, state, created_at)"
            " VALUES ('user', ?, ?, ?, ?, 'x', 'sent', '2026-02-01T00:00:00Z')",
            (item["id"], SESSION_B, str(world.checkout), "Reminder copy and tests"))
    listed = world.client.get("/api/state", headers=world.user()).json()["sessions"]
    assert {session["session_id"]: session["name"] for session in listed} == {
        SESSION_A: "Export completed tasks", SESSION_B: "Reminder copy"}
    # The shown name is only shown: a to-do titled like the sweep does not make its session the sweep.
    run_sql(world, "UPDATE items SET title = ? WHERE id = ?", (store.SWEEP_SESSION_NAME, item["id"]))
    listed = world.client.get("/api/state", headers=world.user()).json()["sessions"]
    first = next(session for session in listed if session["session_id"] == SESSION_A)
    assert (first["name"], first["is_sweep"]) == (store.SWEEP_SESSION_NAME, False)
