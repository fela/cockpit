from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cockpit import activity, db
from tests.conftest import ORIGIN, World

AT = "2026-01-06T08:00:00Z"
JAKARTA = ZoneInfo("Asia/Jakarta")
NOW = datetime(2026, 11, 11, 9, 30, tzinfo=UTC)


def _minute(at: str, app: str = "Cursor", site: str | None = None, away: bool = False) -> dict:
    return {"minute": at, "app": app, "site": site, "away": away}


def _item_with_step(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO items (id, key, title, created_at, updated_at, updated_by) VALUES (1, 'note:plan', 'Plan', ?, ?, 'user')",
        (AT, AT),
    )
    conn.execute(
        "INSERT INTO steps (id, item_id, need, ask, created_at, updated_at, updated_by)"
        " VALUES (1, 1, 'decide:plan', 'Decide', ?, ?, 's')",
        (AT, AT),
    )


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "cockpit.db")
    db.migrate(conn)
    _item_with_step(conn)
    yield conn
    conn.close()


def test_a_minute_sent_again_replaces_the_one_stored(conn: sqlite3.Connection) -> None:
    activity.record(conn, [_minute("2026-11-11T09:00", "Slack")], NOW, JAKARTA)
    found = activity.record(conn, [_minute("2026-11-11T09:00", "Cursor"), _minute("2026-11-11T09:01")], NOW, JAKARTA)
    assert found == {"stored": 2, "skipped": 0, "refused": [], "paused": False}
    assert [row["app"] for row in conn.execute("SELECT app FROM activity_minutes ORDER BY minute")] == ["Cursor"] * 2


def test_a_minute_ahead_of_the_clock_is_refused_and_the_others_are_stored(conn: sqlite3.Connection) -> None:
    found = activity.record(conn, [_minute("2026-11-11T09:00"), _minute("2026-11-11T09:40")], NOW, JAKARTA)
    assert found["stored"] == 1
    assert found["refused"] == ["minute 2026-11-11T09:40 is ahead of Cockpit's clock (2026-11-11T09:30 UTC)"]
    assert conn.execute("SELECT COUNT(*) FROM activity_minutes").fetchone()[0] == 1


def test_pause_stores_nothing_but_still_notes_the_mac(conn: sqlite3.Connection) -> None:
    activity.set_paused(conn, True, NOW)
    assert activity.record(conn, [_minute("2026-11-11T09:00")], NOW, JAKARTA) == {
        "stored": 0, "skipped": 1, "refused": [], "paused": True,
    }
    assert conn.execute("SELECT COUNT(*) FROM activity_minutes").fetchone()[0] == 0
    assert activity.mac_state(conn)["last_seen"] == "2026-11-11T09:30:00Z"


def test_minutes_from_a_pause_that_arrive_late_are_skipped(conn: sqlite3.Connection) -> None:
    # The Mac was offline from 09:00: it missed the pause from 09:10 to 09:20 and sends it all at 09:30.
    activity.set_paused(conn, True, datetime(2026, 11, 11, 9, 10, 40, tzinfo=UTC))
    activity.set_paused(conn, True, datetime(2026, 11, 11, 9, 12, tzinfo=UTC))
    activity.set_paused(conn, False, datetime(2026, 11, 11, 9, 20, 10, tzinfo=UTC))
    sent = [_minute(f"2026-11-11T09:{minute:02}") for minute in (9, 10, 15, 19, 20, 25)]
    # A pause of seconds within one minute still covers that minute.
    activity.set_paused(conn, True, datetime(2026, 11, 11, 9, 27, 1, tzinfo=UTC))
    activity.set_paused(conn, False, datetime(2026, 11, 11, 9, 27, 50, tzinfo=UTC))
    sent += [_minute("2026-11-11T09:26"), _minute("2026-11-11T09:27"), _minute("2026-11-11T09:28")]
    found = activity.record(conn, sent, NOW, JAKARTA)
    # The pause from 09:10:40 to 09:20:10 covers 09:10 to 09:20, the minute Resume was pressed in included.
    assert found == {"stored": 4, "skipped": 5, "refused": [], "paused": False}
    stored = [row["minute"] for row in conn.execute("SELECT minute FROM activity_minutes ORDER BY minute")]
    assert stored == ["2026-11-11T09:09", "2026-11-11T09:25", "2026-11-11T09:26", "2026-11-11T09:28"]


def test_old_minutes_fold_away_while_paused_and_old_pauses_are_forgotten(conn: sqlite3.Connection) -> None:
    activity.record(conn, [_minute("2026-11-01T01:00")], datetime(2026, 11, 1, 2, tzinfo=UTC), JAKARTA)
    activity.set_paused(conn, True, datetime(2026, 11, 1, 3, tzinfo=UTC))
    activity.set_paused(conn, False, datetime(2026, 11, 1, 4, tzinfo=UTC))
    activity.set_paused(conn, True, datetime(2026, 11, 2, 3, tzinfo=UTC))
    with db.transaction(conn):
        activity.fold(conn, datetime(2026, 12, 2, tzinfo=UTC), JAKARTA)
    assert conn.execute("SELECT COUNT(*) FROM activity_minutes").fetchone()[0] == 0
    assert conn.execute("SELECT SUM(minutes) FROM activity_days").fetchone()[0] == 1
    assert db.kv_get(conn, activity.PAUSES) == [["2026-11-02T03:00", None]]


def test_old_minutes_fold_into_day_totals_in_the_users_days(conn: sqlite3.Connection) -> None:
    # 18:30 UTC on 1 Nov is already 2 Nov in Jakarta.
    activity.record(conn, [
        _minute("2026-11-01T18:30", "Cursor"), _minute("2026-11-01T18:31", "Cursor"),
        _minute("2026-11-01T18:32", "Safari", "youtube.com"), _minute("2026-11-01T18:33", away=True),
    ], datetime(2026, 11, 1, 19, tzinfo=UTC), JAKARTA)
    later = datetime(2026, 12, 3, tzinfo=UTC)
    activity.record(conn, [], later, JAKARTA)
    assert conn.execute("SELECT COUNT(*) FROM activity_minutes").fetchone()[0] == 0
    found = activity.day_summary(conn, date(2026, 11, 2), later, JAKARTA, "cockpit.example.test")
    assert found["minutes"] == 3
    assert found["away"] == 1
    assert found["categories"] == {"work": 2, "neutral": 0, "distraction": 1, "unsorted": 0}
    assert found["outside_tasks"] is None


def test_a_day_the_cut_passes_through_counts_its_folded_and_kept_minutes(conn: sqlite3.Connection) -> None:
    # 08:00 and 19:00 in Jakarta on 1 Nov; 30 days before 1 Dec 09:00 UTC falls between them.
    activity.record(conn, [_minute("2026-11-01T01:00"), _minute("2026-11-01T12:00", "Safari", "youtube.com")],
                    datetime(2026, 11, 1, 13, tzinfo=UTC), JAKARTA)
    later = datetime(2026, 12, 1, 9, tzinfo=UTC)
    activity.record(conn, [], later, JAKARTA)
    assert conn.execute("SELECT COUNT(*) FROM activity_minutes").fetchone()[0] == 1
    found = activity.day_summary(conn, date(2026, 11, 1), later, JAKARTA, "cockpit.example.test")
    assert found["categories"] == {"work": 1, "neutral": 0, "distraction": 1, "unsorted": 0}
    assert found["outside_tasks"] is None


def test_a_subdomain_takes_its_parents_category_and_the_users_choice_wins(conn: sqlite3.Connection) -> None:
    known = activity._known(conn, "cockpit.example.test")
    assert activity.category(known, "Safari", "gist.github.com") == "work"
    assert activity.category(known, "Safari", "cockpit.example.test") == "work"
    assert activity.category(known, "Safari", "example.org") == "unsorted"
    assert activity.category(known, "Spotify", None) == "unsorted"
    activity.set_category(conn, "gist.github.com", "distraction")
    known = activity._known(conn, "cockpit.example.test")
    assert activity.category(known, "Safari", "gist.github.com") == "distraction"
    assert activity.category(known, "Safari", "docs.github.com") == "work"
    with pytest.raises(ValueError, match="category"):
        activity.set_category(conn, "Spotify", "fun")


def test_a_minute_counts_for_a_step_when_its_middle_is_inside_the_timer(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO time_spans (step_id, started_at, ended_at, ended_by) VALUES (1, ?, ?, 'pause')",
        ("2026-11-11T09:00:20Z", "2026-11-11T09:03:00Z"),
    )
    activity.record(conn, [
        _minute("2026-11-11T08:59", "Slack"),
        _minute("2026-11-11T09:00", "Cursor"),
        _minute("2026-11-11T09:01", "Safari", "github.com"),
        _minute("2026-11-11T09:02", away=True),
        _minute("2026-11-11T09:03", "Slack"),
    ], NOW, JAKARTA)
    found = activity.item_breakdown(conn, 1, NOW, "cockpit.example.test")
    assert found == {
        "labels": [{"label": "Cursor", "category": "work", "minutes": 1},
                   {"label": "github.com", "category": "work", "minutes": 1}],
        "away": 1,
    }
    day = activity.day_summary(conn, date(2026, 11, 11), NOW, JAKARTA, "cockpit.example.test")
    assert day["outside_tasks"] == 2
    assert day["minutes"] == 4


def test_now_notices_drift_to_distraction(conn: sqlite3.Connection) -> None:
    activity.record(conn, [
        _minute("2026-11-11T09:25", "Cursor"),
        _minute("2026-11-11T09:26", "Safari", "youtube.com"),
        _minute("2026-11-11T09:27", "Safari", "m.youtube.com"),
        _minute("2026-11-11T09:28", "Safari", "x.com"),
    ], NOW, JAKARTA)
    found = activity.now_summary(conn, NOW, "cockpit.example.test")
    assert found["current"] == {"minute": "2026-11-11T09:28:00Z", "app": "Safari", "site": "x.com", "away": False,
                                "category": "distraction"}
    assert found["drifting"] is True
    assert found["last_minutes"] == {"work": 1, "distraction": 3}
    later = activity.now_summary(conn, NOW + timedelta(minutes=10), "cockpit.example.test")
    assert later["current"] is None
    assert later["drifting"] is False


def _mac_code(world: World) -> str:
    response = world.client.post("/api/activity/key", headers=world.user())
    assert response.status_code == 200, response.text
    found = response.json()
    # The line holds a one-time code and no key.
    assert list(found) == ["install"]
    start = f"curl -fsSL {ORIGIN}/activity/install.sh | bash -s -- "
    assert found["install"].startswith(start)
    return found["install"].removeprefix(start)


def _claim(world: World, code: str):
    return world.client.post("/api/activity/claim", json={"code": code})


def _mac_key(world: World) -> str:
    claimed = _claim(world, _mac_code(world))
    assert claimed.status_code == 200, claimed.text
    assert claimed.headers["cache-control"] == "no-store"
    return claimed.json()["key"]


def test_a_code_works_once_only_while_newest_and_fresh(world: World, anonymous) -> None:
    body = {"minutes": []}
    key = _mac_key(world)
    # A new line leaves the Mac's key working until the Mac uses it.
    older, newer = _mac_code(world), _mac_code(world)
    assert anonymous.post("/api/activity/minutes", json=body, headers={"authorization": f"Bearer {key}"}).status_code == 200
    assert _claim(world, older).status_code == 401
    claimed = _claim(world, newer)
    assert claimed.status_code == 200
    assert _claim(world, newer).status_code == 401
    assert anonymous.post("/api/activity/minutes", json=body, headers={"authorization": f"Bearer {key}"}).status_code == 401
    new_key = claimed.json()["key"]
    assert anonymous.post("/api/activity/minutes", json=body, headers={"authorization": f"Bearer {new_key}"}).status_code == 200
    for bad in ({"code": ""}, {"code": "a b"}, {"code": "x" * 65}, {}):
        assert world.client.post("/api/activity/claim", json=bad).status_code in {400, 422}, bad
    conn = db.connect(world.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'request.refused'").fetchone()[0] == 0
    finally:
        conn.close()


def test_an_uninstalled_mac_stops_its_key(world: World, anonymous) -> None:
    mac = {"authorization": f"Bearer {_mac_key(world)}"}
    assert anonymous.post("/api/activity/disconnect").status_code == 401
    assert world.client.post("/api/activity/disconnect", headers=world.user()).status_code == 401
    assert anonymous.post("/api/activity/disconnect", headers=mac).status_code == 200
    assert anonymous.post("/api/activity/minutes", json={"minutes": []}, headers=mac).status_code == 401
    assert world.client.get("/api/activity/now").json()["mac"]["connected"] is False


def test_a_code_stops_after_fifteen_minutes() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    code = activity.new_code(conn, NOW)
    assert activity.claim(conn, code, NOW + activity.CODE_LASTS + timedelta(seconds=1)) is None
    # A late try uses the code up.
    assert activity.claim(conn, code, NOW) is None
    code = activity.new_code(conn, NOW)
    assert activity.claim(conn, code, NOW + activity.CODE_LASTS) is not None


def test_the_mac_sends_with_its_own_key_and_no_origin(world: World, anonymous) -> None:
    key = _mac_key(world)
    minute = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")
    sent = anonymous.post("/api/activity/minutes", json={"minutes": [_minute(minute, "Cursor", "github.com")]},
                          headers={"authorization": f"Bearer {key}"})
    assert sent.status_code == 200, sent.text
    assert sent.json() == {"stored": 1, "skipped": 0, "refused": 0, "why": None, "paused": False}
    now = world.client.get("/api/activity/now").json()
    assert now["current"]["site"] == "github.com"
    assert now["mac"]["connected"] is True


def test_the_pages_retry_table_keeps_no_mac_code(world: World) -> None:
    pressed = {**world.user(), "x-cockpit-action": "connect-mac-press-1"}
    first = world.client.post("/api/activity/key", headers=pressed).json()["install"].split()[-1]
    second = world.client.post("/api/activity/key", headers=pressed).json()["install"].split()[-1]
    assert first != second
    conn = db.connect(world.db_path)
    try:
        kept = json.dumps([[str(value) for value in row] for row in conn.execute("SELECT * FROM page_actions")])
    finally:
        conn.close()
    assert first not in kept and second not in kept


def test_the_minutes_route_takes_no_other_credential(world: World, anonymous) -> None:
    key = _mac_key(world)
    body = {"minutes": []}
    assert anonymous.post("/api/activity/minutes", json=body).status_code == 401
    assert anonymous.post("/api/activity/minutes", json=body, headers={"authorization": "Bearer nope"}).status_code == 401
    assert world.client.post("/api/activity/minutes", json=body, headers=world.user()).status_code == 401
    assert world.client.post("/api/activity/minutes", json=body, headers=world.agent()).status_code == 401
    # A new key replaces the old one.
    _mac_key(world)
    assert anonymous.post("/api/activity/minutes", json=body, headers={"authorization": f"Bearer {key}"}).status_code == 401
    # A sender without the key is nobody: its refusals add no event that would grow with each try.
    conn = db.connect(world.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'request.refused'").fetchone()[0] == 0
    finally:
        conn.close()


def test_the_mac_learns_it_is_paused_and_bad_minutes_are_refused(world: World, anonymous) -> None:
    key = _mac_key(world)
    mac = {"authorization": f"Bearer {key}"}
    # The pause below covers the minute it happens in, so the good minute is the one before it.
    good = _minute((datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M"))
    assert world.client.post("/api/activity/pause", json={"paused": True}, headers=world.user()).json()["paused"] is True
    assert anonymous.post("/api/activity/minutes", json={"minutes": []}, headers=mac).json()["paused"] is True
    assert world.client.post("/api/activity/pause", json={"paused": False}, headers=world.user()).json()["paused"] is False
    # Each bad minute is refused on its own; the good one sent with it is stored.
    bad = [
        _minute("tomorrow"),
        _minute("2026-11-11T09:00", site="https://github.com/x"),
        _minute("2026-11-11T09:00", app=""),
        _minute("2026-11-11T09:00", app="x" * 101),
        {"minute": "2026-11-11T09:00", "app": "Cursor", "away": False},
        "a minute",
        _minute("2099-01-01T00:00"),
        _minute("2026-11-10T25:00"),
    ]
    for each in bad:
        sent = anonymous.post("/api/activity/minutes", json={"minutes": [good, each]}, headers=mac)
        assert sent.status_code == 200, sent.text
        assert (sent.json()["stored"], sent.json()["refused"]) == (1, 1), each
        assert sent.json()["why"]
    for body in ({"minutes": "all"}, {"minutes": [good] * 1441}, {}):
        assert anonymous.post("/api/activity/minutes", json=body, headers=mac).status_code in {400, 422}, body
        # Refused before the key is checked: it may come from anyone, so it adds no event.
        assert anonymous.post("/api/activity/minutes", json=body).status_code in {400, 422}, body
    conn = db.connect(world.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'request.refused'").fetchone()[0] == 0
    finally:
        conn.close()


def test_only_the_user_connects_pauses_and_sorts(world: World) -> None:
    assert world.client.post("/api/activity/key", headers=world.agent()).status_code == 403
    assert world.client.post("/api/activity/pause", json={"paused": True}, headers=world.agent()).status_code == 403
    put = {"name": "Spotify", "category": "distraction"}
    assert world.client.put("/api/activity/categories", json=put, headers=world.agent()).status_code == 403
    assert world.client.put("/api/activity/categories", json=put, headers=world.user()).status_code == 200
    assert world.client.put("/api/activity/categories", json=put | {"category": "fun"},
                            headers=world.user()).status_code in {400, 422}


def test_agents_read_the_day_and_an_items_breakdown(world: World) -> None:
    conn = db.connect(world.db_path)
    try:
        _item_with_step(conn)
    finally:
        conn.close()
    day = world.client.get("/api/activity/day", params={"day": "2026-11-11"}, headers=world.agent())
    assert day.status_code == 200, day.text
    assert day.json()["minutes"] == 0
    assert world.client.get("/api/activity/day", params={"day": "soon"}).status_code == 400
    assert world.client.get("/api/items/1/activity", headers=world.agent()).json() == {"labels": [], "away": 0}
    assert world.client.get("/api/items/99/activity").status_code == 404


def test_the_installer_is_public_and_names_this_cockpit(anonymous) -> None:
    found = anonymous.get("/activity/install.sh")
    assert found.status_code == 200
    assert found.text.startswith("#!/bin/bash")
    assert "__ORIGIN__" not in found.text
    assert f'ORIGIN="{ORIGIN}"' in found.text
    assert anonymous.get("/api/activity/now").status_code == 401
