from datetime import UTC, datetime

import pytest

from cockpit import db, store


@pytest.fixture
def queue_db(tmp_path):
    conn = db.connect(tmp_path / "queue.db")
    db.migrate(conn)
    yield conn
    conn.close()


def steps_in_queue(conn):
    ranked = store._ranked_open(conn, store._unarchived_items(conn), datetime.now(UTC))
    return tuple([step["id"] for step in ranked[name]] for name in ("queue", "ready_next"))


def make_steps(conn, count):
    steps = []
    for number in range(count):
        item = store.upsert_item(conn, {"key": f"note:{number}", "title": f"Task {number}"}, "user")
        steps.append(store.upsert_step(conn, {
            "item_id": item["id"], "need": "read", "ask": f"Read {number}", "score": 90 - number,
        }, "user")["step"])
    return steps


def test_unarchive_gives_back_the_replacement_place(queue_db):
    steps = make_steps(queue_db, 7)
    original = steps_in_queue(queue_db)
    store.upsert_item(queue_db, {"key": "note:0", "archived": True}, "user")
    assert steps_in_queue(queue_db)[0] == [step["id"] for step in steps[1:5]]
    store.upsert_item(queue_db, {"key": "note:0", "archived": False}, "user")
    assert steps_in_queue(queue_db) == original


@pytest.mark.parametrize("count", [5, 7])
def test_yield_of_an_archived_step_conflicts_and_keeps_the_queue(queue_db, count):
    step = make_steps(queue_db, count)[0]
    store.upsert_item(queue_db, {"key": "note:0", "archived": True}, "user")
    before = steps_in_queue(queue_db)
    with pytest.raises(store.Conflict, match="queue"):
        store.step_action(queue_db, step["id"], "yield", {}, "user")
    assert steps_in_queue(queue_db) == before
    assert store.get_step(queue_db, step["id"])["queued_at"] == step["queued_at"]
