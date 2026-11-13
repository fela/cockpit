"""Top cards and time estimates let an agent reach the user without taking a place in the queue."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from cockpit import db, timesheet, top_cards
from tests.conftest import SESSION_A, World, make_session

STAMP = "%Y-%m-%dT%H:%M:%SZ"


def _put(world: World, **fields):
    body = {"need": "standup:2026-01-15", "title": "Standup", **fields}
    return world.client.post("/api/top-cards", json=body, headers=world.agent("reviewer"))


def _soon(minutes: int) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).strftime(STAMP)


def test_a_card_shows_on_the_page_and_takes_no_place_in_the_queue(world: World) -> None:
    made = _put(world, body="The call starts at noon", answers=["Joining now"])
    assert made.status_code == 200, made.text
    state = world.client.get("/api/state").json()
    assert [card["need"] for card in state["top_cards"]] == ["standup:2026-01-15"]
    assert state["top_cards"][0]["answers"] == ["Joining now"]
    assert state["queue"] == []


def test_putting_the_same_need_changes_the_card(world: World) -> None:
    first = _put(world).json()
    second = _put(world, title="Standup now").json()
    assert second["id"] == first["id"] and second["title"] == "Standup now"


def test_a_closed_need_stays_closed(world: World) -> None:
    card = _put(world).json()
    assert world.client.post(
        f"/api/top-cards/{card['id']}/done", json={}, headers=world.agent("reviewer")
    ).status_code == 200
    again = _put(world, title="Standup again")
    assert again.status_code == 409 and "stays closed" in again.text
    assert world.client.get("/api/state").json()["top_cards"] == []


def test_a_card_leaves_when_it_ends(world: World) -> None:
    _put(world, ends_at=_soon(-1))
    _put(world, need="handover:2026-01-15", title="Handover", ends_at=_soon(60))
    assert [card["need"] for card in world.client.get("/api/state").json()["top_cards"]] == ["handover:2026-01-15"]


def test_a_card_refuses_what_it_cannot_show(world: World) -> None:
    assert _put(world, need=" ").status_code == 400
    assert _put(world, title="x" * 121).status_code == 400
    assert _put(world, body="x" * 801).status_code == 400
    assert _put(world, answers=["a", "b", "c", "d", "e"]).status_code == 400
    assert _put(world, ends_at="tonight").status_code == 400
    # A time without a zone could mean anything to a session in another one.
    assert _put(world, ends_at="2026-01-15T12:00:00").status_code == 400


def test_the_users_answer_goes_to_the_session_that_asked(world: World) -> None:
    world.cache.sessions = [make_session(SESSION_A, pid=None, state="done", cwd="/")]
    card = _put(world, answers=["Joining now"], session_id=SESSION_A).json()
    answered = world.client.post(f"/api/top-cards/{card['id']}/done", json={"answer": "Joining now"}, headers=world.user())
    assert answered.status_code == 200, answered.text
    assert answered.json()["answer"] == "Joining now"
    conn = db.connect(world.db_path)
    try:
        sent = conn.execute("SELECT text, session_id FROM messages").fetchall()
    finally:
        conn.close()
    assert len(sent) == 1 and sent[0]["session_id"] == SESSION_A
    assert "'Joining now'" in sent[0]["text"] and "standup:2026-01-15" in sent[0]["text"]


def test_a_second_tap_sends_nothing_more(world: World) -> None:
    world.cache.sessions = [make_session(SESSION_A, pid=None, state="done", cwd="/")]
    card = _put(world, session_id=SESSION_A).json()
    for _ in range(2):
        assert world.client.post(f"/api/top-cards/{card['id']}/done", json={}, headers=world.user()).status_code == 200
    conn = db.connect(world.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == 1
    finally:
        conn.close()


def test_an_agent_closing_its_own_card_sends_nothing(world: World) -> None:
    world.cache.sessions = [make_session(SESSION_A, pid=None, state="done", cwd="/")]
    card = _put(world, session_id=SESSION_A).json()
    assert world.client.post(
        f"/api/top-cards/{card['id']}/done", json={}, headers=world.agent("reviewer")
    ).status_code == 200
    conn = db.connect(world.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == 0
    finally:
        conn.close()


def test_an_answer_the_card_does_not_offer_is_refused(world: World) -> None:
    card = _put(world, answers=["Joining now"]).json()
    refused = world.client.post(f"/api/top-cards/{card['id']}/done", json={"answer": "No"}, headers=world.user())
    assert refused.status_code == 400
    assert world.client.post("/api/top-cards/999/done", json={}, headers=world.user()).status_code == 404


def test_a_session_can_list_what_became_of_its_cards(world: World) -> None:
    card = _put(world, answers=["Joining now"]).json()
    world.client.post(f"/api/top-cards/{card['id']}/done", json={"answer": "Joining now"}, headers=world.user())
    listed = world.client.get("/api/top-cards").json()
    assert [(row["need"], row["answer"]) for row in listed] == [("standup:2026-01-15", "Joining now")]
    assert listed[0]["done_by"]


def test_list_forgets_a_card_closed_long_ago(world: World) -> None:
    conn = db.connect(world.db_path)
    try:
        card = top_cards.put(conn, {"need": "old", "title": "Old"}, "reviewer")
        top_cards.close(conn, card["id"], "reviewer", None)
        assert [row["need"] for row in top_cards.recent(conn, datetime.now(UTC))] == ["old"]
        assert top_cards.recent(conn, datetime.now(UTC) + timedelta(days=3)) == []
    finally:
        conn.close()



def test_the_users_page_marks_a_card_seen_once(world: World) -> None:
    card = _put(world, answers=["Joining now"]).json()
    assert card["seen_at"] is None
    first = world.client.post(f"/api/top-cards/{card['id']}/seen", json={}, headers=world.user())
    assert first.status_code == 200, first.text
    seen_at = first.json()["seen_at"]
    assert seen_at
    conn = db.connect(world.db_path)
    try:
        conn.execute("UPDATE top_cards SET seen_at = '2026-01-01T00:00:00Z' WHERE id = ?", (card["id"],))
    finally:
        conn.close()
    # A later sighting keeps the first time, so the session reads when the user first had the card in front of them.
    again = world.client.post(f"/api/top-cards/{card['id']}/seen", json={}, headers=world.user()).json()
    assert again["seen_at"] == "2026-01-01T00:00:00Z"
    assert world.client.get("/api/top-cards").json()[0]["seen_at"] == "2026-01-01T00:00:00Z"


def test_a_session_cannot_mark_a_card_seen(world: World) -> None:
    card = _put(world).json()
    refused = world.client.post(f"/api/top-cards/{card['id']}/seen", json={}, headers=world.agent("reviewer"))
    assert refused.status_code == 403
    assert world.client.get("/api/top-cards").json()[0]["seen_at"] is None
    assert world.client.post("/api/top-cards/999/seen", json={}, headers=world.user()).status_code == 404


def _stretches(conn, now: datetime):
    return timesheet.stretches(conn, date(2026, 1, 15), now, ZoneInfo("UTC"))


def _input_minutes(conn, *minutes: str) -> None:
    with db.transaction(conn):
        for minute in minutes:
            conn.execute("INSERT INTO input_minutes (minute) VALUES (?)", (f"2026-01-15T{minute}",))


def test_a_day_with_no_input_has_no_stretches(world: World) -> None:
    conn = db.connect(world.db_path)
    try:
        found = _stretches(conn, datetime(2026, 1, 15, 12, tzinfo=UTC))
    finally:
        conn.close()
    assert found == {"day": "2026-01-15", "stretches": [], "minutes": 0, "last_input": None}


def test_input_counts_as_active_and_gives_the_last_input_time(world: World) -> None:
    conn = db.connect(world.db_path)
    try:
        _input_minutes(conn, "09:00", "09:01", "09:02")
        found = _stretches(conn, datetime(2026, 1, 15, 12, tzinfo=UTC))
    finally:
        conn.close()
    assert [row["source"] for row in found["stretches"]] == ["active"]
    assert found["minutes"] == found["stretches"][0]["minutes"] > 0
    assert found["last_input"] == "2026-01-15T09:02:00Z"


def test_a_step_timer_names_its_task_and_active_time_under_it_counts_once(world: World) -> None:
    conn = db.connect(world.db_path)
    try:
        with db.transaction(conn):
            when = "2026-01-15T00:00:00Z"
            item = conn.execute(
                "INSERT INTO items (key, title, project, created_at, updated_at, updated_by)"
                " VALUES ('k', 'Fix it', 'Cockpit', ?, ?, 'test')", (when, when),
            ).lastrowid
            step = conn.execute(
                "INSERT INTO steps (item_id, need, ask, created_at, updated_at, updated_by)"
                " VALUES (?, 'merge', 'Merge?', ?, ?, 'test')", (item, when, when),
            ).lastrowid
            conn.execute(
                "INSERT INTO time_spans (step_id, started_at, ended_at, ended_by) VALUES (?, ?, ?, 'pause')",
                (step, "2026-01-15T09:00:00Z", "2026-01-15T09:30:00Z"),
            )
        _input_minutes(conn, "09:10", "09:11")
        found = _stretches(conn, datetime(2026, 1, 15, 12, tzinfo=UTC))
    finally:
        conn.close()
    assert [(row["source"], row["item"], row["project"], row["minutes"]) for row in found["stretches"]] == [
        ("step_timer", "Fix it", "Cockpit", 30.0)
    ]
    assert found["minutes"] == 30.0


def test_the_time_route_reads_the_setups_zone_and_refuses_a_bad_day(world: World) -> None:
    found = world.client.get("/api/time/stretches", params={"day": "2026-01-15"})
    assert found.status_code == 200, found.text
    assert found.json()["day"] == "2026-01-15"
    assert world.client.get("/api/time/stretches", params={"day": "tonight"}).status_code == 400
    assert world.client.get("/api/time/stretches", params={"tz": "Mars/Base"}).status_code == 400
