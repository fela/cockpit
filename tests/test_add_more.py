"""More words for a to-do the user added (SPEC CONTROL-18)."""

from __future__ import annotations

from dataclasses import replace

from cockpit import db, store
from tests.conftest import SESSION_A, SESSION_B, World, make_session
from tests.test_api import add_item, agent_client, put_item, run_sql
from tests.test_delivery import message


def add_more(world: World, item_id: int, text: str):
    return world.client.post(f"/api/items/{item_id}/add-more", json={"text": text, "images": []}, headers=world.user())


def first_message(world: World, item_id: int, cwd: str) -> dict:
    conn = db.connect(world.db_path)
    try:
        target = {"item_id": item_id, "step_id": None, "session_id": None, "new_cwd": cwd}
        return store.create_message(conn, "user", "Test list sharing", target, world.checkout.parent)
    finally:
        conn.close()


def test_more_words_join_the_text_and_go_to_the_session(world: World) -> None:
    item = add_item(world, "Test list sharing\non a small sample")
    run_sql(world, "UPDATE items SET sessions = ? WHERE id = ?", (f'["{SESSION_A}"]', item["id"]))
    added = add_more(world, item["id"], "  and include empty lists  ")
    assert added.status_code == 200, added.text
    body = added.json()
    assert body["item"]["summary"] == "Test list sharing\non a small sample\n\nand include empty lists"
    assert body["item"]["title"] == "Test list sharing"
    sent = message(world, body["message"]["id"])
    assert (sent["session_id"], sent["item_id"], sent["author"], sent["text"]) == (
        SESSION_A, item["id"], "user", "More for this to-do:\n\nand include empty lists")


def test_more_words_join_the_first_message_while_the_session_has_not_started(world: World) -> None:
    item = add_item(world, "Test list sharing")
    first = first_message(world, item["id"], str(world.checkout))
    body = add_more(world, item["id"], "and include empty lists").json()
    assert (body["message"]["id"], body["message"]["joined"]) == (first["id"], True)
    assert message(world, first["id"])["text"] == "Test list sharing\n\nand include empty lists"


def test_more_words_with_no_session_yet_stay_on_the_item_for_the_sweeper(world: World) -> None:
    item = add_item(world, "Test list sharing")
    body = add_more(world, item["id"], "and include empty lists").json()
    assert body["message"] is None
    assert body["item"]["summary"] == "Test list sharing\n\nand include empty lists"


def test_more_words_need_words_the_user_and_an_item_the_user_added(world: World) -> None:
    item = add_item(world, "Test list sharing")
    assert add_more(world, item["id"], "  \n ").status_code == 400
    agent = agent_client(world).post(f"/api/items/{item['id']}/add-more", json={"text": "x", "images": []}, headers=world.agent())
    assert agent.status_code == 403
    other = put_item(world)
    refused = add_more(world, other["id"], "and more")
    assert refused.status_code == 400 and "not one you added" in refused.json()["detail"]
    assert add_more(world, 999, "x").status_code == 404


def test_more_words_after_a_failed_first_message_stay_on_the_item(world: World) -> None:
    item = add_item(world, "Test list sharing")
    first = first_message(world, item["id"], str(world.checkout))
    run_sql(world, "UPDATE messages SET state = 'failed', detail = 'no such checkout' WHERE id = ?", (first["id"],))
    added = add_more(world, item["id"], "and include empty lists")
    assert added.status_code == 200, added.text
    assert added.json()["message"] is None
    assert added.json()["item"]["summary"] == "Test list sharing\n\nand include empty lists"


def test_the_page_gets_the_ended_items_of_the_sessions_it_lists(world: World) -> None:
    """A session of a closed task, or one done long ago, on no open item, goes by the task's title. Items the list of work shows are in `items` already."""
    closed = add_item(world, "Test list sharing")
    done = add_item(world, "Renew the passport")
    unlisted = add_item(world, "Book the venue")
    shown = add_item(world, "Draft the reminder guide")
    run_sql(world, "UPDATE items SET sessions = ?, archived = 1 WHERE id = ?", (f'["{SESSION_A}"]', closed["id"]))
    run_sql(world, "UPDATE items SET sessions = ?, stage = 'done', updated_at = '2026-01-01T00:00:00Z' WHERE id = ?",
            (f'["{SESSION_A}"]', done["id"]))
    run_sql(world, "UPDATE items SET sessions = '[\"not-listed\"]', archived = 1 WHERE id = ?", (unlisted["id"],))
    run_sql(world, "UPDATE items SET sessions = ? WHERE id = ?", (f'["{SESSION_A}"]', shown["id"]))
    listed = world.client.get("/api/state", headers=world.user()).json()["ended_items"]
    assert sorted((entry["id"], entry["title"], entry["archived"]) for entry in listed) == [
        (closed["id"], "Test list sharing", 1), (done["id"], "Renew the passport", 0)]


def test_a_finished_session_leaves_the_list_and_each_listed_one_says_who_started_it(world: World) -> None:
    """Mark removed sessions finished, show each session's starting message, and distinguish sweep and briefing sessions."""
    removed = replace(make_session(SESSION_B, pid=None), removed_at="2026-01-15T10:00:00Z")
    world.cache.sessions = [make_session(SESSION_A, pid=None), removed]
    item = add_item(world, "Test list sharing")
    conn = db.connect(world.db_path)
    try:
        target = {"item_id": item["id"], "step_id": None, "session_id": None, "new_cwd": str(world.checkout)}
        started = store.create_message(conn, "user", "Test list sharing", target, world.checkout.parent)
    finally:
        conn.close()
    run_sql(world, "UPDATE messages SET session_id = ?, via = 'new' WHERE id = ?", (SESSION_A, started["id"]))
    listed = world.client.get("/api/state", headers=world.user()).json()["sessions"]
    assert [session["session_id"] for session in listed] == [SESSION_A]
    assert {key: listed[0]["started"][key] for key in ("author", "item_id")} == {"author": "user", "item_id": item["id"]}
    assert listed[0]["is_sweep"] is False
    run_sql(world, "UPDATE messages SET new_name = ? WHERE id = ?", (store.SWEEP_SESSION_NAME, started["id"]))
    assert world.client.get("/api/state", headers=world.user()).json()["sessions"][0]["is_sweep"] is True
