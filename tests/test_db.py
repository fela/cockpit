from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cockpit import db, store

GUIDANCE = "my priority is finishing list sharing and writing the reminder guide"


def test_row_allocates_fresh_empty_json_containers() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    records = conn.execute("SELECT '[]' AS sessions, '{}' AS details UNION ALL SELECT '[]', '{}'").fetchall()
    conn.close()
    first, second = db.rows(records)
    repeated = db.row(records[0])
    assert first == second == repeated == {"sessions": [], "details": {}}
    first["sessions"].append("changed")
    first["details"]["changed"] = True
    assert second == repeated == {"sessions": [], "details": {}}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("null", None),
        ("true", True),
        ("false", False),
        (" [ ] \n", []),
        (" { } \t", {}),
        (" null \n", None),
        (" true \t", True),
        (" false \n", False),
        ('[1, "two", false]', [1, "two", False]),
        ('{"nested": [1, true]}', {"nested": [1, True]}),
        ('"hello"', "hello"),
        ("42", 42),
    ],
)
def test_row_decodes_json_and_preserves_sql_null(value, expected) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    record = conn.execute("SELECT ? AS data, 'true' AS title", (value,)).fetchone()
    conn.close()
    decoded = db.row(record)
    assert decoded == {"data": expected, "title": "true"}
    assert type(decoded["data"]) is type(expected)


@pytest.mark.parametrize("value", ["", "[", "{broken}", "True", "null trailing"])
def test_row_rejects_malformed_json(value) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    record = conn.execute("SELECT ? AS data", (value,)).fetchone()
    conn.close()
    with pytest.raises(json.JSONDecodeError):
        db.row(record)


@pytest.mark.parametrize("failing_pragma", ["journal_mode", "foreign_keys", "busy_timeout"])
def test_failed_connection_setup_closes_the_database(tmp_path, monkeypatch, failing_pragma):
    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if failing_pragma in sql:
                raise sqlite3.OperationalError("connection setup failed")
            return super().execute(sql, *args, **kwargs)

    conn = sqlite3.connect(tmp_path / "setup.db", factory=FailingConnection)
    monkeypatch.setattr(db.sqlite3, "connect", lambda *args, **kwargs: conn)
    with pytest.raises(sqlite3.OperationalError, match="connection setup failed"):
        db.connect(tmp_path / "setup.db")
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        conn.execute("SELECT 1")


def test_a_connection_waits_30_seconds_for_a_lock_and_a_write_that_waited_it_out_reads_as_locked(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "lock.db")
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    conn.execute("CREATE TABLE t (x)")
    conn.execute("BEGIN IMMEDIATE")
    other = sqlite3.connect(tmp_path / "lock.db", timeout=0, isolation_level=None)
    with pytest.raises(sqlite3.OperationalError) as caught:
        other.execute("INSERT INTO t VALUES (1)")
    conn.execute("ROLLBACK")
    other.close()
    conn.close()
    assert db.locked(caught.value)
    assert not db.locked(sqlite3.OperationalError("disk I/O error"))
    assert not db.locked(RuntimeError("database is locked"))


def database_at(path: Path, version: int) -> sqlite3.Connection:
    """A database built by the first `version` migrations, the way older servers built it."""
    conn = sqlite3.connect(path, isolation_level=None)
    for number, script in enumerate(db.MIGRATIONS[:version], start=1):
        conn.executescript(f"BEGIN; {script}; PRAGMA user_version = {number}; COMMIT;")
    return conn


def fill_version_8(conn: sqlite3.Connection, guidance: str) -> None:
    """Items, steps, messages and feedback that point at each other, as a version-8 server wrote them."""
    at = "2026-01-04T09:00:00Z"
    conn.execute("INSERT INTO goals (id, week, text, position) VALUES (2, '2026-W01', 'Ship A', 1)")
    conn.execute(
        "INSERT INTO items (id, key, title, priority, goal_id, sessions, created_at, updated_at, updated_by,"
        " user_fields) VALUES (3, 'note:a', 'A', 'high', 2, '[\"s\"]', ?, ?, 'user', '[\"priority\"]')",
        (at, at),
    )
    conn.execute(
        "INSERT INTO items (id, key, title, priority, archived, created_at, updated_at, updated_by)"
        " VALUES (7, 'gh:example-org/app#10', 'PR 10', 'low', 1, ?, ?, 'agent:x')",
        (at, at),
    )
    conn.execute(
        "INSERT INTO messages (id, author, item_id, text, state, created_at) VALUES (5, 'user', 3, 'take it', 'sent', ?)",
        (at,),
    )
    for step_id, item_id, need, nudge, handoff in [(11, 3, "merge", 15, 5), (12, 7, "review", 0, None)]:
        conn.execute(
            "INSERT INTO steps (id, item_id, need, ask, nudge, handoff_message_id, queued_at, created_at, updated_at,"
            " updated_by) VALUES (?, ?, ?, 'Ask', ?, ?, ?, ?, ?, 'agent:x')",
            (step_id, item_id, need, nudge, handoff, at, at, at),
        )
    conn.execute("UPDATE messages SET step_id = 11 WHERE id = 5")
    conn.execute(
        "INSERT INTO feedback (id, step_id, item_id, text, created_at) VALUES (4, 12, 7, 'too long', ?)", (at,)
    )
    conn.execute("INSERT INTO events (id, at, actor, kind, step_id) VALUES (9, ?, 'user', 'step.raise', 11)", (at,))
    conn.execute("INSERT INTO kv (key, value) VALUES ('guidance', ?)", (json.dumps(guidance),))


def contents(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    return {
        "goals": [tuple(r) for r in conn.execute("SELECT * FROM goals ORDER BY id")],
        "items": [tuple(r) for r in conn.execute("SELECT * FROM items ORDER BY id")],
        "steps": [
            tuple(r) for r in conn.execute("SELECT id, item_id, need, handoff_message_id, queued_at FROM steps ORDER BY id")
        ],
        "messages": [tuple(r) for r in conn.execute("SELECT id, item_id, step_id, text FROM messages ORDER BY id")],
        "feedback": [tuple(r) for r in conn.execute("SELECT id, step_id, item_id, text FROM feedback ORDER BY id")],
        "events": [tuple(r) for r in conn.execute("SELECT id, kind, step_id FROM events ORDER BY id")],
    }


def test_migration_9_keeps_every_row_and_reference(tmp_path: Path, monkeypatch) -> None:
    old = database_at(tmp_path / "old.db", 8)
    fill_version_8(old, GUIDANCE)
    before = contents(old)
    old.close()

    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:9])
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert contents(conn) == before  # dropping `items` cascaded into nothing
        guidance = [tuple(r) for r in conn.execute("SELECT id, text, dropped_at FROM guidance")]
        assert guidance == [(1, GUIDANCE, None)]
        assert db.kv_get(conn, "guidance") is None
        assert "nudge" not in {r["name"] for r in conn.execute("PRAGMA table_info(steps)")}
        references = {(r["table"], r["from"], r["on_delete"]) for r in conn.execute("PRAGMA foreign_key_list(steps)")}
        assert ("items", "item_id", "CASCADE") in references
        conn.execute("UPDATE items SET priority = 'urgent' WHERE id = 3")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE items SET priority = 'whenever' WHERE id = 3")
        conn.execute("DELETE FROM items WHERE id = 7")  # the references work on the new table
        assert [r["id"] for r in conn.execute("SELECT id FROM steps")] == [11]
        assert tuple(conn.execute("SELECT item_id, step_id FROM feedback WHERE id = 4").fetchone()) == (None, None)
    finally:
        conn.close()


def every_row(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    return {table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in tables}


def fill_version_9(conn: sqlite3.Connection) -> None:
    """Rows in every table a version-9 server had, the guidance and permission requests among them."""
    fill_version_8(conn, GUIDANCE)
    conn.executescript(f"BEGIN; {db.MIGRATIONS[8]}; PRAGMA user_version = 9; COMMIT;")
    conn.execute("INSERT INTO guidance (text, created_at, dropped_at) VALUES ('Old', 'x', 'y')")
    conn.execute(
        "INSERT INTO permission_requests (kind, state, session_id, cwd, tool_name, tool_input, tool_use_id, reason,"
        " count, created_at, last_at, message_id) VALUES ('denied', 'open', 's', '/', 'Bash', '{}', 't', 'r', 1,"
        " 'x', 'x', 5)"
    )


def test_migration_10_adds_an_empty_to_do_list_and_keeps_every_row(tmp_path: Path, monkeypatch) -> None:
    old = database_at(tmp_path / "old.db", 8)
    fill_version_9(old)
    before = every_row(old)
    old.close()

    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:10])
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 10
        after = every_row(conn)
        columns = [(r["name"], r["type"], r["notnull"], r["pk"]) for r in conn.execute("PRAGMA table_info(todos)")]
    finally:
        conn.close()
    assert after.pop("todos") == []
    assert after == before
    assert all(before[table] for table in ("items", "steps", "messages", "feedback", "events", "guidance", "kv"))
    assert columns == [
        ("id", "INTEGER", 0, 1),
        ("text", "TEXT", 1, 0),
        ("created_at", "TEXT", 1, 0),
        ("done_at", "TEXT", 0, 0),
    ]


def test_migration_11_turns_each_open_to_do_into_an_item_user_added_and_keeps_every_row(
    tmp_path: Path, monkeypatch
) -> None:
    old = database_at(tmp_path / "old.db", 8)
    fill_version_9(old)
    old.executescript(f"BEGIN; {db.MIGRATIONS[9]}; PRAGMA user_version = 10; COMMIT;")
    for text, created_at, done_at in [
        ("Call the bank", "2026-01-04T10:00:00Z", None),
        ("Book flights", "2026-01-04T10:01:00Z", "2026-01-04T11:00:00Z"),
        (" Approve the OAuth app \nin the console", "2026-01-04T10:02:00Z", None),
    ]:
        old.execute("INSERT INTO todos (text, created_at, done_at) VALUES (?, ?, ?)", (text, created_at, done_at))
    before = every_row(old)
    old.close()

    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:11])
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
        after = every_row(conn)
        added = [
            dict(r) for r in conn.execute(
                "SELECT id, key, title, summary, todo, user_fields, stage, priority, sessions, created_at, updated_by"
                " FROM items WHERE todo = 1 ORDER BY id"
            )
        ]
        image_columns = [r["name"] for r in conn.execute("PRAGMA table_info(item_images)")]
        image_references = [(r["table"], r["from"], r["on_delete"]) for r in conn.execute(
            "PRAGMA foreign_key_list(item_images)"
        )]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO item_images (item_id, content_type, size, content, created_at)"
                " VALUES (3, 'image/svg+xml', 1, x'00', 'x')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE items SET todo = 2 WHERE id = 3")
    finally:
        conn.close()
    assert [(item["key"], item["title"], item["summary"]) for item in added] == [
        (f"todo-{added[0]['id']}", "Call the bank", "Call the bank"),
        (f"todo-{added[1]['id']}", "Approve the OAuth app", " Approve the OAuth app \nin the console"),
    ]
    assert all(
        (item["todo"], item["user_fields"], item["stage"], item["priority"], item["sessions"], item["updated_by"])
        == (1, '["summary"]', None, "normal", "[]", "user")
        for item in added
    )
    assert [item["created_at"] for item in added] == ["2026-01-04T10:00:00Z", "2026-01-04T10:02:00Z"]
    assert "todos" in before and "todos" not in after  # the done to-do goes with the table
    before.pop("todos")
    old_items = before.pop("items")
    items = after.pop("items")
    assert items[: len(old_items)] == [row + (0,) for row in old_items]  # an item an agent made is not the user's
    assert len(items) == len(old_items) + len(added)
    assert after.pop("item_images") == []
    assert after.pop("sqlite_sequence") == []  # AUTOINCREMENT keeps image ids from being reused
    assert after == before
    assert all(before[table] for table in ("steps", "messages", "guidance", "permission_requests"))
    assert image_columns == ["id", "item_id", "content_type", "size", "content", "created_at"]
    assert image_references == [("items", "item_id", "CASCADE")]


def test_migration_12_lets_a_request_be_withdrawn_and_keeps_every_row(tmp_path: Path, monkeypatch) -> None:
    old = database_at(tmp_path / "old.db", 8)
    fill_version_9(old)
    for number in (10, 11):
        old.executescript(f"BEGIN; {db.MIGRATIONS[number - 1]}; PRAGMA user_version = {number}; COMMIT;")
    old.execute(
        "INSERT INTO permission_requests (kind, state, session_id, cwd, tool_name, tool_input, tool_use_id, reason,"
        " count, created_at, last_at, resolved_at, rule, rule_list) VALUES ('asked', 'allowed', 's', '/', 'Bash',"
        " '{}', '', '', 1, 'x', 'x', 'y', 'Bash(ls *)', 'allow')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        old.execute("UPDATE permission_requests SET state = 'withdrawn' WHERE id = 1")
    before = every_row(old)
    old.close()

    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:12])
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
        after = every_row(conn)
        conn.execute("UPDATE permission_requests SET state = 'withdrawn' WHERE id = 1")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE permission_requests SET state = 'gone' WHERE id = 1")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE permission_requests SET rule_once = 2 WHERE id = 1")
        indexes = {r["name"] for r in conn.execute("PRAGMA index_list(permission_requests)")}
        references = [(r["table"], r["from"]) for r in conn.execute("PRAGMA foreign_key_list(permission_requests)")]
    finally:
        conn.close()
    requests = after.pop("permission_requests")
    # Each old row keeps its values; the new columns start empty and not temporary.
    assert requests == [row + (None, 0, None, None) for row in before.pop("permission_requests")]
    assert after == before
    assert indexes == {"permission_requests_open", "permission_requests_once"}
    assert references == [("messages", "message_id")]


def test_migration_13_makes_old_images_users_and_leaves_failed_messages_undismissed(
    tmp_path: Path, monkeypatch
) -> None:
    old = database_at(tmp_path / "old.db", 8)
    fill_version_9(old)
    for number in (10, 11, 12):
        old.executescript(f"BEGIN; {db.MIGRATIONS[number - 1]}; PRAGMA user_version = {number}; COMMIT;")
    old.execute(
        "INSERT INTO item_images (item_id, content_type, size, content, created_at) VALUES (3, 'image/png', 1, x'00', 'x')"
    )
    old.execute("INSERT INTO messages (id, author, item_id, text, state, detail, created_at) VALUES"
                " (6, 'user', 3, 'go', 'failed', 'claude --bg exited 1: boom', 'x')")
    before = every_row(old)
    old.close()

    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:13])
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
        after = every_row(conn)
    finally:
        conn.close()
    assert after.pop("item_images") == [row + ("user",) for row in before.pop("item_images")]
    assert after.pop("messages") == [row + (None, None, None) for row in before.pop("messages")]
    assert after == before


def test_migration_14_gives_old_steps_no_buttons_and_adds_command_runs(tmp_path: Path, monkeypatch) -> None:
    old = database_at(tmp_path / "old.db", 8)
    fill_version_9(old)
    for number in (10, 11, 12, 13):
        old.executescript(f"BEGIN; {db.MIGRATIONS[number - 1]}; PRAGMA user_version = {number}; COMMIT;")
    before = every_row(old)
    old.close()

    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:14])
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 14
        after = every_row(conn)
        steps = [store.get_step(conn, record["id"]) for record in conn.execute("SELECT id FROM steps")]
        references = [(r["table"], r["from"], r["on_delete"]) for r in conn.execute("PRAGMA foreign_key_list(command_runs)")]
    finally:
        conn.close()
    assert before["steps"], "the old database has steps to carry over"
    assert after.pop("steps") == [row + ("[]", "null", "null") for row in before.pop("steps")]
    assert after.pop("command_runs") == []
    assert after == before
    assert [(step["answers"], step["command"], step["permission"]) for step in steps] == [([], None, None)] * len(steps)
    assert sorted(references) == [("messages", "result_message_id", "NO ACTION"), ("steps", "step_id", "CASCADE")]

def test_migration_15_adds_empty_time_spans_and_keeps_every_row(tmp_path: Path, monkeypatch) -> None:
    old = database_at(tmp_path / "old.db", 8)
    fill_version_9(old)
    for number in (10, 11, 12, 13, 14):
        old.executescript(f"BEGIN; {db.MIGRATIONS[number - 1]}; PRAGMA user_version = {number}; COMMIT;")
    before = every_row(old)
    old.close()

    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:15])
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 15
        after = every_row(conn)
    finally:
        conn.close()
    assert after.pop("time_spans") == []
    assert after == before


def test_migration_16_dates_old_withdrawals_and_keeps_every_row(tmp_path: Path, monkeypatch) -> None:
    old = database_at(tmp_path / "old.db", 8)
    fill_version_9(old)
    for number in range(10, 16):
        old.executescript(f"BEGIN; {db.MIGRATIONS[number - 1]}; PRAGMA user_version = {number}; COMMIT;")
    old.execute(
        "INSERT INTO permission_requests (kind, state, session_id, cwd, tool_name, tool_input, tool_use_id, reason,"
        " count, created_at, last_at, resolved_at) VALUES ('denied', 'withdrawn', 's', '/', 'Bash', '{}', 't', 'r',"
        " 1, 'x', 'x', '2026-01-06T10:00:00Z')"
    )
    before = every_row(old)
    old.close()

    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:16])
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 16
        after = every_row(conn)
    finally:
        conn.close()
    open_row, withdrawn_row = before.pop("permission_requests")
    # The new columns: when the user last worked on the card, and when the session took the call back.
    assert after.pop("permission_requests") == [open_row + (None, None), withdrawn_row + (None, "2026-01-06T10:00:00Z")]
    assert after == before


def test_migration_9_skips_empty_guidance(tmp_path: Path) -> None:
    old = database_at(tmp_path / "old.db", 8)
    old.execute("INSERT INTO kv (key, value) VALUES ('guidance', ?)", (json.dumps(" \n"),))
    old.close()
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        assert conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 0
        assert db.kv_get(conn, "guidance") is None
    finally:
        conn.close()


def test_a_migration_that_breaks_a_reference_is_rolled_back(tmp_path: Path, monkeypatch) -> None:
    database_at(tmp_path / "old.db", len(db.MIGRATIONS)).close()
    broken = "INSERT INTO steps (item_id, need, ask, created_at, updated_at, updated_by) VALUES (404, 'n', 'Ask', 'x', 'x', 'x')"
    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS, broken])
    conn = db.connect(tmp_path / "old.db")
    try:
        with pytest.raises(db.MigrationError, match="steps"):
            db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS) - 1
        assert conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_open_steps_wait_as_ready_next_after_migration_4_except_pinned(tmp_path: Path) -> None:
    conn = database_at(tmp_path / "old.db", 3)
    at = "2026-01-04T09:00:00Z"
    conn.execute(
        "INSERT INTO items (id, key, title, created_at, updated_at, updated_by) VALUES (1, 'note:a', 'A', ?, ?, 'x')",
        (at, at),
    )
    for step_id, score, pin in [(1, 10, 1), (2, 20, None), (3, 90, None), (4, 80, None), (5, 70, None), (6, 60, None)]:
        conn.execute(
            "INSERT INTO steps (id, item_id, need, ask, score, pin, created_at, updated_at, updated_by)"
            " VALUES (?, 1, ?, 'Ask', ?, ?, ?, ?, 'x')",
            (step_id, f"n{step_id}", score, pin, at, at),
        )
    conn.close()

    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        queued = [row["id"] for row in conn.execute("SELECT id FROM steps WHERE queued_at IS NOT NULL")]
        assert queued == [1]
        with db.transaction(conn):
            store.fill_queue(conn, datetime.now(UTC))
        queued = sorted(row["id"] for row in conn.execute("SELECT id FROM steps WHERE queued_at IS NOT NULL"))
    finally:
        conn.close()
    assert queued == [1, 3, 4, 5]  # the pinned step, then the three strongest


def test_migration_7_marks_the_last_launch_a_sweep(tmp_path: Path) -> None:
    conn = database_at(tmp_path / "old.db", 6)
    conn.execute("INSERT INTO kv (key, value) VALUES ('sweep_launch', '{\"message_id\": 3}')")
    conn.close()

    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        assert db.kv_get(conn, "sweep_launch") == {"message_id": 3, "kind": "sweep", "step_ids": []}
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(steps)")}
    finally:
        conn.close()
    assert {"checked_at", "checked_by", "check_note"} <= columns


def test_migration_17_makes_no_old_message_user_s_prompt(tmp_path: Path, monkeypatch) -> None:
    old = database_at(tmp_path / "old.db", 8)
    fill_version_9(old)
    for number in range(10, 17):
        old.executescript(f"BEGIN; {db.MIGRATIONS[number - 1]}; PRAGMA user_version = {number}; COMMIT;")
    before = every_row(old)
    old.close()

    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:17])
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 17
        after = every_row(conn)
    finally:
        conn.close()
    # Messages from before keep the socket route: none is the user's prompt, none waits.
    assert after.pop("messages") == [row + (0, None) for row in before.pop("messages")]
    assert after == before


def test_migration_18_counts_the_minutes_of_user_s_actions_as_input(tmp_path: Path, monkeypatch) -> None:
    old = database_at(tmp_path / "old.db", 17)
    old.execute(
        "INSERT INTO events (at, actor, kind) VALUES ('2026-01-06T09:00:05Z', 'user', 'a'),"
        " ('2026-01-06T09:00:40Z', 'user', 'b'), ('2026-01-06T09:07:00Z', 'user', 'c'),"
        " ('2026-01-06T09:30:00Z', 'sweep', 'd')"
    )
    before = every_row(old)
    old.close()

    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:18])
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 18
        after = every_row(conn)
        since = db.kv_get(conn, "input_minutes_since")
    finally:
        conn.close()
    # Each minute with an action of the user's, once; the sweep's actions are no input of the user's.
    assert after.pop("input_minutes") == [("2026-01-06T09:00",), ("2026-01-06T09:07",)]
    assert after.pop("kv") == before.pop("kv") + [("input_minutes_since", json.dumps(since))]
    assert datetime.fromisoformat(since) > datetime(2026, 1, 6, tzinfo=UTC)
    assert after == before


def test_migration_20_makes_goals_and_priority_messages_one_ordered_list(tmp_path: Path, monkeypatch) -> None:
    old = database_at(tmp_path / "old.db", 19)
    old.execute(
        "INSERT INTO goals (id, week, text, position) VALUES (2, '2026-W01', 'Old A', 1), (5, '2026-W02', 'Ship B', 2),"
        " (6, '2026-W02', 'Support C', 1)"
    )
    old.execute(
        "INSERT INTO items (id, key, title, goal_id, created_at, updated_at, updated_by)"
        " VALUES (3, 'note:a', 'A', 5, 'x', 'x', 'user')"
    )
    old.execute(
        "INSERT INTO guidance (id, text, created_at, dropped_at) VALUES (1, 'Lists first', '2026-01-05T08:00:00Z', NULL),"
        " (2, 'Gone', '2026-01-05T09:00:00Z', '2026-01-05T10:00:00Z'), (3, 'Reviews', '2026-01-06T11:00:00Z', NULL)"
    )
    old.execute(
        "INSERT INTO events (id, at, actor, kind, data) VALUES"
        " (1, '2026-01-05T07:00:00Z', 'user', 'goals.saved', '{\"week\": \"2026-W02\", \"count\": 2}'),"
        " (2, '2026-01-05T08:00:00Z', 'user', 'guidance.added', '{\"guidance_id\": 1}'),"
        " (3, '2026-01-05T10:00:00Z', 'user', 'guidance.dropped', '{\"guidance_id\": 2}'),"
        " (4, '2026-01-05T10:01:00Z', 'user', 'step.done', '{}')"
    )
    before = every_row(old)
    old.close()

    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:20])
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 20
        after = every_row(conn)
        state_goals = store.goal_list(conn)
    finally:
        conn.close()
    # This week's goals first in their order, then older weeks', then the messages that hold, newest first;
    # a message keeps its time and gets an id after the goals'.
    assert [(goal["id"], goal["text"], goal["position"]) for goal in state_goals] == [
        (6, "Support C", 1), (5, "Ship B", 2), (2, "Old A", 3), (9, "Reviews", 4), (7, "Lists first", 5),
    ]
    assert after.pop("goals")[3:] == [
        (7, "Lists first", 5, "2026-01-05T08:00:00Z", None, None),
        (8, "Gone", 6, "2026-01-05T09:00:00Z", None, "2026-01-05T10:00:00Z"),
        (9, "Reviews", 4, "2026-01-06T11:00:00Z", None, None),
    ]
    assert state_goals[0]["created_at"] == "2026-01-05T07:00:00Z"
    assert after.pop("events") == [
        before["events"][0],
        (2, "2026-01-05T08:00:00Z", "user", "goal.added", None, None, None, '{"goal_id":7}'),
        (3, "2026-01-05T10:00:00Z", "user", "goal.dropped", None, None, None, '{"goal_id":8}'),
        before["events"][3],
    ]
    assert "guidance" not in after
    for table in ("goals", "events", "guidance"):
        before.pop(table)
    assert after == before


def test_migration_31_lets_time_be_taken_back_and_keeps_every_span(tmp_path: Path, monkeypatch) -> None:
    old = database_at(tmp_path / "old.db", 30)
    old.execute("INSERT INTO items (id, key, title, created_at, updated_at, updated_by) VALUES (3, 'note:a', 'A', 'x', 'x', 'user')")
    old.execute("INSERT INTO steps (id, item_id, need, ask, created_at, updated_at, updated_by) VALUES (4, 3, 'review', 'Review', 'x', 'x', 'user')")
    old.execute(
        "INSERT INTO time_spans (id, step_id, started_at, ended_at, ended_by) VALUES"
        " (1, 4, '2026-01-06T07:00:00Z', '2026-01-06T07:10:00Z', 'pause'), (2, 4, '2026-01-06T08:00:00Z', NULL, NULL)"
    )
    before = every_row(old)
    old.close()

    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:31])
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 31
        after = every_row(conn)
        conn.execute("UPDATE time_spans SET ended_at = '2026-01-06T08:30:00Z', ended_by = 'away' WHERE id = 2")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO time_spans (step_id, started_at) VALUES (4, 'x'), (4, 'y')")
    finally:
        conn.close()
    assert after.pop("time_spans") == [span + (None,) for span in before.pop("time_spans")]
    assert after == before


def test_migration_33_drops_the_seen_and_keep_columns_and_keeps_every_row(tmp_path: Path, monkeypatch) -> None:
    old = database_at(tmp_path / "old.db", 32)
    old.execute("INSERT INTO items (id, key, title, created_at, updated_at, updated_by) VALUES (3, 'note:a', 'A', 'x', 'x', 'user')")
    old.execute("INSERT INTO steps (id, item_id, need, ask, created_at, updated_at, updated_by) VALUES (4, 3, 'review', 'Review', 'x', 'x', 'user')")
    old.execute(
        "INSERT INTO closures (id, kind, ref_id, item_id, action, closed_by, closed_at, note, seen_at)"
        " VALUES (1, 'step', 4, 3, 'done', 'agent:a', 'x', 'merged', 'y')"
    )
    old.execute(
        "INSERT INTO session_archives (id, session_id, step_id, asked_at, kept_at, kept_by, keep_reason, stopped_at)"
        " VALUES (2, 's', 4, 'x', NULL, NULL, '', 'y')"
    )
    before = every_row(old)
    old.close()

    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:33])
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 33
        after = every_row(conn)
        columns = {
            table: [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            for table in ("closures", "session_archives")
        }
    finally:
        conn.close()
    assert "seen_at" not in columns["closures"]
    assert not {"kept_at", "kept_by", "keep_reason"} & set(columns["session_archives"])
    assert after.pop("closures") == [(1, "step", 4, 3, "done", "agent:a", "x", "merged")]
    assert after.pop("session_archives") == [(2, "s", 4, None, "x", "y", "", None, None)]
    before.pop("closures")
    before.pop("session_archives")
    assert after == before


def test_migration_34_adds_held_changes_and_drops_pins_and_the_focus_window(tmp_path: Path, monkeypatch) -> None:
    old = database_at(tmp_path / "old.db", 33)
    old.execute("INSERT INTO items (id, key, title, created_at, updated_at, updated_by) VALUES (3, 'note:a', 'A', 'x', 'x', 'user')")
    old.execute(
        "INSERT INTO steps (id, item_id, need, ask, pin, user_active_at, created_at, updated_at, updated_by)"
        " VALUES (4, 3, 'review', 'Review', 1, 'y', 'x', 'x', 'user')"
    )
    before = every_row(old)
    old.close()

    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:34])
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 34
        after = every_row(conn)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(steps)")]
        held = "INSERT INTO held_changes (step_id, kind, actor, data, created_at) VALUES (4, ?, 'agent:a', '{}', 'x')"
        conn.execute(held, ("step_put",))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(held, ("pin",))
    finally:
        conn.close()
    assert not {"pin", "user_active_at"} & set(columns)
    assert [row[:4] for row in after.pop("steps")] == [row[:4] for row in before.pop("steps")]
    assert after.pop("held_changes") == []
    assert after == before


def test_the_progress_marks_migration_starts_each_step_with_its_progress_at_its_last_put(tmp_path: Path) -> None:
    version = 44
    old = database_at(tmp_path / "old.db", version)
    old.execute("INSERT INTO items (id, key, title, created_at, updated_at, updated_by) VALUES (3, 'note:a', 'A', 'x', 'x', 'user')")
    old.execute(
        "INSERT INTO steps (id, item_id, need, ask, progress, prepared_at, created_at, updated_at, updated_by) VALUES"
        " (4, 3, 'a', 'A', 40, '2026-01-10T09:00:00Z', 'x', '2026-01-11T09:00:00Z', 's'),"
        " (5, 3, 'b', 'B', 70, '', 'x', '2026-01-12T09:00:00Z', 's'),"
        " (6, 3, 'c', 'C', NULL, '', 'x', '2026-01-12T09:00:00Z', 's')"
    )
    old.execute(
        "INSERT INTO held_changes (step_id, kind, actor, data, created_at) VALUES"
        " (4, 'step_put', 's', '{\"item_id\": 3, \"need\": \"a\", \"why\": \"W\", \"progress\": 85}', 'x'),"
        " (4, 'step_action', 's', '{\"action\": \"done\", \"body\": {}}', 'x')"
    )
    # Item 7 was completed at 10:00, undone, moved to done by an agent at 12:00 and completed again at 15:00, when it
    # was already done. Item 8 was moved to done by an agent at 10:00, then by the user's edits of its stage, the
    # last at 16:00. Item 9 has none of these. Item 11 had no stage when it was completed at 10:00.
    old.execute(
        "INSERT INTO items (id, key, title, stage, created_at, updated_at, updated_by) VALUES"
        " (7, 'note:b', 'B', 'done', 'x', '2026-01-13T09:00:00Z', 'user'),"
        " (8, 'note:c', 'C', 'done', 'x', '2026-01-14T09:00:00Z', 'user'),"
        " (9, 'note:d', 'D', 'done', 'x', '2026-01-14T09:00:00Z', 'user'),"
        " (10, 'note:e', 'E', 'review', 'x', '2026-01-14T09:00:00Z', 'user'),"
        " (11, 'note:f', 'F', 'done', 'x', '2026-01-14T09:00:00Z', 'user')"
    )
    old.execute(
        "INSERT INTO item_finishes (item_id, how, reason, before_fields, closed_steps, at, reopened_at) VALUES"
        " (7, 'complete', '', '{\"stage\": \"review\"}', '[]', '2026-01-12T10:00:00Z', '2026-01-12T11:00:00Z'),"
        " (7, 'complete', '', '{\"stage\": \"done\"}', '[]', '2026-01-12T15:00:00Z', NULL),"
        " (11, 'complete', '', '{\"stage\": null}', '[]', '2026-01-12T10:00:00Z', NULL)"
    )
    old.execute(
        "INSERT INTO closures (kind, ref_id, item_id, action, closed_by, closed_at) VALUES"
        " ('item', 7, 7, 'stage done', 'agent:a', '2026-01-12T12:00:00Z'),"
        " ('item', 8, 8, 'stage done', 'agent:a', '2026-01-12T10:00:00Z')"
    )
    old.execute(
        "INSERT INTO events (at, actor, kind, item_id, data) VALUES"
        " ('2026-01-12T14:00:00Z', 'user', 'item.edited', 8, '{\"fields\": [\"stage\"]}'),"
        " ('2026-01-12T16:00:00Z', 'user', 'item.edited', 8, '{\"fields\": [\"priority\", \"stage\"]}'),"
        " ('2026-01-12T17:00:00Z', 'user', 'item.edited', 8, '{\"fields\": [\"priority\"]}')"
    )
    old.close()
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        marks = conn.execute(
            "SELECT step_id, item_id, at, progress, of_user_time FROM progress_marks ORDER BY step_id"
        ).fetchall()
        done = conn.execute("SELECT id, done_at FROM items WHERE id > 3 ORDER BY id").fetchall()
        progress = conn.execute("SELECT progress FROM steps ORDER BY id").fetchall()
        held = conn.execute("SELECT data FROM held_changes ORDER BY id").fetchall()
    finally:
        conn.close()
    # The estimates of how far the work was stay as marks of that kind, and leave the steps.
    assert [tuple(mark) for mark in marks] == [(4, 3, "2026-01-10T09:00:00Z", 40, 0), (5, 3, "2026-01-12T09:00:00Z", 70, 0)]
    assert [row[0] for row in progress] == [None, None, None]
    assert [json.loads(row[0]) for row in held] == [{"item_id": 3, "need": "a", "why": "W"}, {"action": "done", "body": {}}]
    assert [tuple(row) for row in done] == [
        (7, "2026-01-12T12:00:00Z"), (8, "2026-01-12T16:00:00Z"), (9, "2026-01-14T09:00:00Z"), (10, None),
        (11, "2026-01-12T10:00:00Z"),
    ]


def test_the_page_revision_migration_starts_every_page_at_revision_one(tmp_path: Path) -> None:
    old = database_at(tmp_path / "old.db", 45)
    old.execute("INSERT INTO items (id, key, title, created_at, updated_at, updated_by) VALUES (3, 'note:a', 'A', 'x', 'x', 'user')")
    old.execute(
        "INSERT INTO item_pages (id, item_id, title, html, size, created_at, updated_at, added_by)"
        " VALUES (5, 3, 'P', '<p>x</p>', 8, 'x', 'x', 'user')"
    )
    old.close()
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        assert [tuple(row) for row in conn.execute("SELECT id, revision FROM item_pages")] == [(5, 1)]
    finally:
        conn.close()


def test_the_codex_migration_runs_after_mains_and_a_database_that_ran_it_first_catches_up(tmp_path: Path) -> None:
    """main's databases stop at 46 and take the Codex migration as 47. A database that recorded the Codex
    migration as 41 gets main's 41-46 and keeps its Codex threads, with no second run of the Codex migration."""
    assert "codex_threads" in db.MIGRATIONS[db.CODEX_MIGRATION - 1]
    early = database_at(tmp_path / "early.db", db.CODEX_AHEAD - 1)
    early.executescript(f"BEGIN; {db.MIGRATIONS[db.CODEX_MIGRATION - 1]}; PRAGMA user_version = {db.CODEX_AHEAD}; COMMIT;")
    early.execute("INSERT INTO codex_threads (session_id, name, cwd, updated_at) VALUES ('t1', 'n', '/c', 'x')")
    early.close()
    database_at(tmp_path / "main.db", db.CODEX_MIGRATION - 1).close()
    for name in ("early.db", "main.db"):
        conn = db.connect(tmp_path / name)
        try:
            db.migrate(conn)
            assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS), name
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
            assert {"provider", "completes"} <= columns, name
            assert db._has_table(conn, "item_finishes") and db._has_table(conn, "progress_marks"), name
            threads = [row["session_id"] for row in conn.execute("SELECT session_id FROM codex_threads")]
            assert threads == (["t1"] if name == "early.db" else []), name
        finally:
            conn.close()


@pytest.mark.parametrize("stop", range(db.CODEX_AHEAD, db.CODEX_MIGRATION))
def test_a_database_that_ran_the_codex_migration_first_resumes_a_catch_up_cut_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: int
) -> None:
    early = database_at(tmp_path / "early.db", db.CODEX_AHEAD - 1)
    early.executescript(f"BEGIN; {db.MIGRATIONS[db.CODEX_MIGRATION - 1]}; PRAGMA user_version = {db.CODEX_AHEAD}; COMMIT;")
    early.close()
    conn = db.connect(tmp_path / "early.db")
    try:
        with monkeypatch.context() as cut:
            cut.setattr(db, "MIGRATIONS", db.MIGRATIONS[:stop])
            db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == stop
        db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    finally:
        conn.close()


def test_the_step_version_migration_gives_each_message_its_step_s_last_version_from_an_earlier_second(
    tmp_path: Path,
) -> None:
    """A version in the message's own second may come after it, so it is not the baseline (SPEC NEXT-37)."""
    step_version = next(n for n, migration in enumerate(db.MIGRATIONS) if "ADD COLUMN step_version" in migration)
    old = database_at(tmp_path / "old.db", step_version)
    old.execute("INSERT INTO items (id, key, title, created_at, updated_at, updated_by) VALUES (3, 'note:a', 'A', 'x', 'x', 'user')")
    old.execute(
        "INSERT INTO steps (id, item_id, need, ask, created_at, updated_at, updated_by)"
        " VALUES (4, 3, 'decide:a', 'Ask', 'x', 'x', 'agent:a')"
    )
    for version, at in ((1, "2026-01-26T10:00:00Z"), (2, "2026-01-26T10:00:05Z"), (3, "2026-01-26T10:10:00Z")):
        old.execute(
            "INSERT INTO step_versions (id, step_id, at, actor, ask, why, body, details, draft, answers, command,"
            " permission) VALUES (?, 4, ?, 'agent:a', 'Ask', '', '', '[]', '', '[]', 'null', 'null')",
            (version, at),
        )
    old.execute(
        "INSERT INTO messages (id, author, item_id, step_id, text, state, created_at)"
        " VALUES (5, 'user', 3, 4, 'Which one?', 'sent', '2026-01-26T10:00:05Z')"
    )
    old.execute(
        "INSERT INTO messages (id, author, item_id, text, state, created_at)"
        " VALUES (6, 'user', 3, 'take it', 'sent', '2026-01-26T10:00:05Z')"
    )
    old.close()
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        assert [tuple(row) for row in conn.execute("SELECT id, step_version FROM messages ORDER BY id")] == [(5, 1), (6, None)]
    finally:
        conn.close()


def test_the_migration_files_cockpit_work_under_one_project(tmp_path: Path) -> None:
    # The page tags an item Cockpit by its project (SPEC UI-43): older spellings, the tool's old name among them, move.
    version = next(n for n, script in enumerate(db.MIGRATIONS) if "UPDATE items SET project = 'cockpit'" in script)
    conn = database_at(tmp_path / "old.db", version)
    at = "2026-01-27T09:00:00Z"
    projects = {1: "cockpit", 2: "Cockpit", 3: "desk", 4: "\tdesk\n", 5: "app", 6: "", 7: "desktop", 8: "\u00a0Cockpit\u3000"}
    for item_id, project in projects.items():
        conn.execute(
            "INSERT INTO items (id, key, title, project, created_at, updated_at, updated_by) VALUES (?, ?, 'T', ?, ?, ?, 'agent:x')",
            (item_id, f"note:{item_id}", project, at, at),
        )
    conn.close()
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        moved = {row["id"]: row["project"] for row in conn.execute("SELECT id, project FROM items")}
    finally:
        conn.close()
    assert moved == {1: "cockpit", 2: "cockpit", 3: "cockpit", 4: "cockpit", 5: "app", 6: "", 7: "desktop", 8: "cockpit"}
    # It trims what a put's str.strip() trims.
    trimmed = re.search(r"trim\(project, char\(([\d,\s]+)\)\)", db.MIGRATIONS[version]).group(1)
    assert {chr(int(code)) for code in trimmed.split(",")} == {chr(code) for code in range(0x110000) if chr(code).isspace()}


def test_the_migration_gives_a_saved_account_choice_the_shortest_length_that_covers_its_time_left(tmp_path: Path) -> None:
    # The usage card marks the length chosen (SPEC SESSION-17); a choice saved before it was kept gets one.
    version = next(n for n, script in enumerate(db.MIGRATIONS) if "'account_preference'" in script)
    conn = database_at(tmp_path / "old.db", version)
    until = datetime.now(UTC).timestamp() + 90 * 60
    conn.execute(
        "INSERT INTO kv (key, value) VALUES ('account_preference', ?)",
        (json.dumps({"account": "personal", "until": datetime.fromtimestamp(until, UTC).isoformat()}),),
    )
    conn.close()
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        assert db.kv_get(conn, "account_preference")["minutes"] == 120
    finally:
        conn.close()


def test_the_writing_migration_puts_each_step_example_on_the_version_whose_text_it_holds(tmp_path: Path) -> None:
    """A rewrite can land between the copy of a step's text and the example's insert :
    the example is on the version its text is, not the last one before it."""
    migration = next(n for n, script in enumerate(db.MIGRATIONS) if "ADD COLUMN step_version_id" in script)
    old = database_at(tmp_path / "old.db", migration)
    old.execute("INSERT INTO items (id, key, title, created_at, updated_at, updated_by) VALUES (3, 'note:a', 'A', 'x', 'x', 'user')")
    old.execute(
        "INSERT INTO steps (id, item_id, need, ask, created_at, updated_at, updated_by)"
        " VALUES (4, 3, 'decide:a', 'Ask', 'x', 'x', 'agent:a')"
    )
    versions = (
        (1, "2026-01-26T10:00:00Z", "Old text", [{"title": "Numbers", "body": "7 in 100"}], "A draft"),
        (2, "2026-01-26T10:00:01Z", "New unread text", [], ""),
    )
    for version, at, ask, details, draft in versions:
        old.execute(
            "INSERT INTO step_versions (id, step_id, at, actor, ask, why, body, details, draft, answers, command,"
            " permission) VALUES (?, 4, ?, 'agent:a', ?, 'Why', '', ?, ?, '[]', 'null', 'null')",
            (version, at, ask, json.dumps(details), draft),
        )
    old_text = store._step_markdown(
        {"ask": "Old text", "why": "Why", "body": "", "details": versions[0][3], "draft": "A draft", "closes": None, "reply_hint": "", "core_links": []}
    )
    for example, text in ((5, old_text), (6, "# Marked before step history")):
        old.execute(
            "INSERT INTO writing_examples (id, kind, title, target, text, format, note, step_id, session_name, written_by,"
            " created_at, verdict) VALUES (?, 'step', 'Ask', 'step:4', ?, 'markdown', '', 4, '', 'agent:a',"
            " '2026-01-26T10:00:02Z', 'bad')",
            (example, text),
        )
    old.close()
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        assert [tuple(row) for row in conn.execute("SELECT id, step_version_id FROM writing_examples ORDER BY id")] == [
            (5, 1), (6, None),
        ]
    finally:
        conn.close()


def test_the_closes_migration_gives_each_steps_latest_version_its_list(tmp_path: Path) -> None:
    """a step's list of tasks to close became part of its text; the latest version takes the step's list,
    so the next put that leaves it as it is adds no version."""
    migration = next(n for n, script in enumerate(db.MIGRATIONS) if "step_versions ADD COLUMN closes" in script)
    old = database_at(tmp_path / "old.db", migration)
    old.execute("INSERT INTO items (id, key, title, created_at, updated_at, updated_by) VALUES (3, 'note:a', 'A', 'x', 'x', 'user')")
    closes = json.dumps([{"item_id": 9, "why": "merged"}])
    old.execute(
        "INSERT INTO steps (id, item_id, need, ask, closes, created_at, updated_at, updated_by)"
        " VALUES (4, 3, 'close:done', 'Close these?', ?, 'x', 'x', 'agent:a')",
        (closes,),
    )
    for version in (1, 2):
        old.execute(
            "INSERT INTO step_versions (id, step_id, at, actor, ask, why, body, details, draft, answers, command,"
            " permission) VALUES (?, 4, 'x', 'agent:a', 'Close these?', '', '', '[]', '', '[]', 'null', 'null')",
            (version,),
        )
    old.close()
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        assert [row[0] for row in conn.execute("SELECT closes FROM step_versions ORDER BY id")] == [None, closes]
    finally:
        conn.close()


def test_database_and_sidecars_are_private_without_changing_parent_mode(tmp_path):
    import os

    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    path = parent / "private.db"
    mask = os.umask(0o022)
    try:
        for existing in (False, True):
            if existing:
                path.chmod(0o644)
            conn = db.connect(path)
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS private (text TEXT)")
                conn.execute("INSERT INTO private VALUES ('private message')")
                for name in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
                    assert name.stat().st_mode & 0o777 == 0o600
            finally:
                conn.close()
        assert parent.stat().st_mode & 0o777 == 0o755
    finally:
        os.umask(mask)


def test_a_new_connection_keeps_the_others_locks_so_another_process_loses_no_write(tmp_path):
    # The server holds connections open while each request opens another; `cockpit attach` and the browser tests'
    # fixtures write from their own process. Opening the file outside SQLite and closing it dropped the held
    # connections' locks, so the other process deleted the write-ahead log on closing and a write was lost.
    import subprocess
    import sys

    path = tmp_path / "shared.db"
    held = db.connect(path)
    try:
        held.execute("CREATE TABLE t (n INTEGER)")
        outside = ("import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); "
                   "c.execute('INSERT INTO t VALUES (0)'); c.commit()")
        for n in range(1, 4):
            db.connect(path).close()
            subprocess.run([sys.executable, "-c", outside, str(path)], check=True)
            held.execute("INSERT INTO t VALUES (?)", (n,))
            assert Path(str(path) + "-wal").exists(), "another process deleted the write-ahead log under an open connection"
        assert sorted(row[0] for row in held.execute("SELECT n FROM t")) == [0, 0, 0, 1, 2, 3]
        assert held.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        held.close()


def test_backups_are_private_even_when_replacing_a_public_file(tmp_path):
    import os

    conn = db.connect(tmp_path / "source.db")
    conn.execute("CREATE TABLE private (text TEXT)")
    conn.execute("INSERT INTO private VALUES ('private message')")
    target = tmp_path / "backups" / "daily.db"
    mask = os.umask(0o022)
    try:
        for existing in (False, True):
            if existing:
                target.chmod(0o644)
            db.backup(conn, target)
            assert target.stat().st_mode & 0o777 == 0o600
    finally:
        conn.close()
        os.umask(mask)


def test_migration_of_archived_at_takes_the_close_not_the_last_put(tmp_path: Path, monkeypatch) -> None:
    number = next(index for index, script in enumerate(db.MIGRATIONS, 1) if "ADD COLUMN archived_at" in script)
    # A patch of its own, so that ending it leaves the fixtures' patches (the test database, no network) in place.
    with monkeypatch.context() as patch:
        patch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:number - 1])
        conn = db.connect(tmp_path / "old.db")
        db.migrate(conn)
    insert = (
        "INSERT INTO items (id, key, title, archived, created_at, updated_at, updated_by) VALUES"
        " (?, ?, 'Work', ?, '2026-01-01T00:00:00Z', '2026-02-01T00:00:00Z', 'agent:late')"
    )
    conn.execute(insert, (1, "note:closed", 1))
    conn.execute(insert, (2, "note:edited", 1))
    conn.execute(insert, (3, "note:bare", 1))
    conn.execute(insert, (4, "note:open", 0))
    conn.execute(insert, (5, "note:reclosed", 1))
    conn.execute(insert, (6, "note:legacy", 1))
    conn.execute(
        "INSERT INTO events (at, actor, kind, item_id, data) VALUES"
        " ('2026-01-02T00:00:00Z', 'user', 'item.closed', 1, '{}'),"
        " ('2026-01-03T00:00:00Z', 'user', 'item.edited', 1, '{\"fields\": [\"summary\"]}'),"
        " ('2026-01-04T00:00:00Z', 'user', 'item.edited', 2, '{\"archived\": true}'),"
        " ('2026-01-05T00:00:00Z', 'user', 'item.closed', 5, '{}'),"
        " ('2026-01-06T00:00:00Z', 'user', 'item.closed', 6, '{}'),"
        " ('2026-01-07T00:00:00Z', 'user', 'item.edited', 6, '{\"fields\": [\"archived\"]}')"
    )
    # Closed by the user, reopened, then archived by an agent: the agent's close is the one that holds.
    conn.execute(
        "INSERT INTO closures (kind, ref_id, item_id, action, closed_by, closed_at) VALUES"
        " ('item', 5, 5, 'archived', 'agent:later', '2026-01-08T00:00:00Z')"
    )
    conn.close()
    conn = db.connect(tmp_path / "old.db")
    db.migrate(conn)
    rows = [tuple(row) for row in conn.execute("SELECT id, archived_at, archived_by FROM items ORDER BY id")]
    conn.close()
    assert rows == [
        (1, "2026-01-02T00:00:00Z", "user"),
        (2, "2026-01-04T00:00:00Z", "user"),
        (3, "2026-02-01T00:00:00Z", "agent:late"),
        (4, None, None),
        (5, "2026-01-08T00:00:00Z", "agent:later"),
        (6, "2026-01-07T00:00:00Z", "user"),
    ]


@pytest.mark.parametrize("names", [
    ["id", "title", "sessions", "links"],
    ["id", "id", "title"],
    ["id", "ID", "title"],
    ["sessions", "SESSIONS", "sessions"],
    ["é", "É", "title"],
    ["İ", "i", "title"],
    ["", "", "title"],
])
def test_row_copy_preserves_sqlite_column_lookup(names: list[str]) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        query = "SELECT " + ", ".join('? AS "' + name + '"' for name in names)
        values = ["[]" if name in db.JSON_COLUMNS else str(index) for index, name in enumerate(names)]
        record = conn.execute(query, values).fetchone()
        expected = dict(record)
        for name in db.JSON_COLUMNS & expected.keys():
            expected[name] = json.loads(expected[name])
        assert db.row(record) == expected
    finally:
        conn.close()


def test_row_batches_preserve_mixed_layouts_and_independent_json() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        same = conn.execute("SELECT 1 AS id, '[]' AS sessions UNION ALL SELECT 2, '[]'").fetchall()
        other = conn.execute("SELECT '{\"nested\":[1]}' AS data, NULL AS title").fetchone()
        duplicate = conn.execute("SELECT 4 AS id, 5 AS ID, '[]' AS sessions").fetchone()
        records = [*same, other, duplicate]
        expected = [db.row(record) for record in records]
        assert db.rows(records) == expected
        assert db.rows(iter(records)) == expected
        assert db.rows(conn.execute("SELECT 1 AS id, '[]' AS sessions UNION ALL SELECT 2, '[]'")) == expected[:2]
        assert db.rows([duplicate, *same]) == [db.row(duplicate), *expected[:2]]
        assert db.rows([]) == []
        copied = db.rows(same)
        copied[0]["sessions"].append("local edit")
        assert copied[1]["sessions"] == []
        assert db.rows(same)[0]["sessions"] == []
        bad = conn.execute("SELECT 'not JSON' AS data").fetchone()
        with pytest.raises(json.JSONDecodeError):
            db.rows([other, bad])
    finally:
        conn.close()


def test_migration_97_names_opus_5_5_for_claude_sessions_started_on_the_default(tmp_path: Path) -> None:
    conn = database_at(tmp_path / "old.db", 96)
    insert = ("INSERT INTO messages (id, author, text, state, created_at, via, session_id, new_options)"
              " VALUES (?, 'user', 'go', 'sent', '2026-11-09T00:00:00Z', ?, ?, ?)")
    picked = json.dumps({"model": "claude-sonnet-5-5", "effort": None, "account": None})
    conn.execute(insert, (1, "new", "s-default", None))
    conn.execute(insert, (2, "new", "s-effort", json.dumps({"model": None, "effort": "low", "account": "work"})))
    conn.execute(insert, (3, "new", "s-picked", picked))
    conn.execute(insert, (4, "new", "s-codex", None))
    conn.execute(insert, (5, "resume", "s-default", None))
    conn.execute("INSERT INTO codex_threads (session_id, name, cwd, updated_at) VALUES ('s-codex', 'c', '/', 'now')")
    conn.close()
    conn = db.connect(tmp_path / "old.db")
    db.migrate(conn)
    rows = {row[0]: row[1] and json.loads(row[1]) for row in conn.execute("SELECT id, new_options FROM messages")}
    conn.close()
    assert rows == {
        1: {"model": "claude-opus-5-5", "effort": None, "account": None},
        2: {"model": "claude-opus-5-5", "effort": "low", "account": "work"},
        3: json.loads(picked),
        4: None,
        5: None,
    }
