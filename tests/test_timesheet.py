from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cockpit import db, timesheet
from tests.conftest import World

AT = "2026-01-06T08:00:00Z"


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "cockpit.db")
    db.migrate(conn)
    conn.execute(
        "INSERT INTO items (id, key, title, created_at, updated_at, updated_by) VALUES"
        " (1, 'gh:example-org/app#10', 'PR 10', ?, ?, 'user'), (2, 'note:plan', 'Plan', ?, ?, 'user')",
        (AT, AT, AT, AT),
    )
    conn.execute(
        "INSERT INTO steps (id, item_id, need, ask, created_at, updated_at, updated_by) VALUES"
        " (1, 1, 'review@aaaa111', 'Review', ?, ?, 's'), (2, 2, 'decide:plan', 'Decide', ?, ?, 's')",
        (AT, AT, AT, AT),
    )
    yield conn
    conn.close()


def _input(conn: sqlite3.Connection, *minutes: str) -> None:
    for minute in minutes:
        timesheet.note_input(conn, _at(minute))


def _span(conn: sqlite3.Connection, step_id: int, started_at: str, ended_at: str | None) -> None:
    conn.execute(
        "INSERT INTO time_spans (step_id, started_at, ended_at, ended_by) VALUES (?, ?, ?, ?)",
        (step_id, started_at, ended_at, ended_at and "pause"),
    )


def test_input_minutes_at_most_five_minutes_apart_make_one_stretch() -> None:
    minutes = [_at("2026-01-06T10:00"), _at("2026-01-06T10:05"), _at("2026-01-06T10:11")]
    assert timesheet.active_stretches(minutes) == [
        (_at("2026-01-06T10:00"), _at("2026-01-06T10:06")),
        (_at("2026-01-06T10:11"), _at("2026-01-06T10:12")),
    ]


def test_a_step_s_kind_is_the_first_word_of_its_need() -> None:
    assert [timesheet.step_kind(need) for need in ("review@aaaa111", "merge-app-10", "qa:2026-01-06", "connect")] == [
        "review", "merge", "qa", "connect",
    ]


def test_time_on_steps_is_left_out_of_active_time_without_a_timer(conn: sqlite3.Connection) -> None:
    # Input from 10:00 to 10:08 is one stretch until 10:09; the timer runs 10:02 to 10:05 inside it, and
    # again from 11:00 to 11:30 with no input at all.
    _input(conn, "2026-01-06T10:00:20", "2026-01-06T10:00:50", "2026-01-06T10:04:00", "2026-01-06T10:08:00")
    _span(conn, 1, "2026-01-06T10:02:00Z", "2026-01-06T10:05:00Z")
    _span(conn, 2, "2026-01-06T11:00:00Z", "2026-01-06T11:30:00Z")
    report = timesheet.report(conn, 1, _at("2026-01-06T12:00"), ZoneInfo("UTC"))
    assert report["days"] == [
        {
            "date": "2026-01-06", "worked": 9 * 60 + 30 * 60, "on_steps": 3 * 60 + 30 * 60, "on_feedback": 0,
            "active_without_timer": 6 * 60,
        },
    ]
    assert report["totals"] == {"worked": 39 * 60, "on_steps": 33 * 60, "on_feedback": 0, "active_without_timer": 6 * 60}
    assert report["items"] == [
        {"item_id": 2, "title": "Plan", "seconds": 30 * 60},
        {"item_id": 1, "title": "PR 10", "seconds": 3 * 60},
    ]
    assert report["kinds"] == [{"kind": "decide", "seconds": 30 * 60}, {"kind": "review", "seconds": 3 * 60}]


def test_two_timers_at_once_count_once_per_day_and_fully_per_task(conn: sqlite3.Connection) -> None:
    _span(conn, 1, "2026-01-06T10:00:00Z", "2026-01-06T10:10:00Z")
    _span(conn, 2, "2026-01-06T10:05:00Z", "2026-01-06T10:15:00Z")
    report = timesheet.report(conn, 1, _at("2026-01-06T12:00"), ZoneInfo("UTC"))
    assert report["days"][0]["on_steps"] == 15 * 60
    assert [item["seconds"] for item in report["items"]] == [10 * 60, 10 * 60]


def test_time_writing_feedback_counts_apart_from_steps_and_untimed_time(conn: sqlite3.Connection) -> None:
    # Input from 10:00 to 10:20; the step's timer runs 10:00 to 10:05 and again from 10:08. Feedback sent at
    # 10:08 took 3 minutes, written while the timer was paused; one sent at 10:12 took 2 minutes on a page
    # that paused nothing, so the step's timer keeps that time.
    _input(conn, *(f"2026-01-06T10:{minute:02d}" for minute in range(0, 20, 4)), "2026-01-06T10:19")
    _span(conn, 1, "2026-01-06T10:00:00Z", "2026-01-06T10:05:00Z")
    _span(conn, 1, "2026-01-06T10:08:00Z", "2026-01-06T10:15:00Z")
    conn.execute(
        "INSERT INTO feedback (text, target, quote, seconds, created_at) VALUES"
        " ('a', '', '', 180, '2026-01-06T10:08:00Z'), ('b', '', '', 120, '2026-01-06T10:12:00Z'),"
        " ('before timing', '', '', NULL, '2026-01-06T10:18:00Z')"
    )
    (day,) = timesheet.report(conn, 1, _at("2026-01-06T12:00"), ZoneInfo("UTC"))["days"]
    assert day == {
        "date": "2026-01-06", "worked": 20 * 60, "on_steps": 12 * 60, "on_feedback": 3 * 60,
        "active_without_timer": 5 * 60,
    }


def test_a_running_timer_counts_until_now(conn: sqlite3.Connection) -> None:
    _span(conn, 1, "2026-01-06T11:50:00Z", None)
    report = timesheet.report(conn, 1, _at("2026-01-06T12:00"), ZoneInfo("UTC"))
    assert report["days"][0]["on_steps"] == 10 * 60
    assert report["items"] == [{"item_id": 1, "title": "PR 10", "seconds": 10 * 60}]


def test_days_are_user_s_days_in_their_time_zone(conn: sqlite3.Connection) -> None:
    # Johannesburg is two hours ahead: 21:30 UTC is 23:30 on the 5th, 23:30 UTC is 01:30 on the 6th.
    _input(conn, "2026-01-05T21:30", "2026-01-05T23:30")
    _span(conn, 1, "2026-01-05T21:50:00Z", "2026-01-05T22:10:00Z")
    # Older than the two days asked for: left out.
    _input(conn, "2026-01-03T12:00")
    _span(conn, 2, "2026-01-03T12:00:00Z", "2026-01-03T13:00:00Z")
    report = timesheet.report(conn, 2, _at("2026-01-06T10:00"), ZoneInfo("Africa/Johannesburg"))
    assert report["days"] == [
        {"date": "2026-01-06", "worked": 11 * 60, "on_steps": 10 * 60, "on_feedback": 0, "active_without_timer": 60},
        {"date": "2026-01-05", "worked": 11 * 60, "on_steps": 10 * 60, "on_feedback": 0, "active_without_timer": 60},
    ]
    assert report["items"] == [{"item_id": 1, "title": "PR 10", "seconds": 20 * 60}]


def test_user_s_input_on_a_page_counts_as_active_time(world: World) -> None:
    assert world.client.post("/api/presence", headers=world.user()).status_code == 200
    raw = sqlite3.connect(world.db_path)
    try:
        assert raw.execute("SELECT minute FROM input_minutes").fetchall() == [(datetime.now(UTC).strftime("%Y-%m-%dT%H:%M"),)]
    finally:
        raw.close()
    report = world.client.get("/api/time?days=1&tz=Africa/Johannesburg").json()
    # The minute of input counts up to now, which lies inside that minute.
    (today,) = report["days"]
    assert today["worked"] == today["active_without_timer"] <= 60
    assert report["input_since"]


def test_the_time_report_needs_days_and_a_known_time_zone(world: World) -> None:
    assert world.client.get("/api/time?days=1&tz=Mars/Olympus").status_code == 400
    assert world.client.get("/api/time?days=1").status_code == 400
    assert world.client.get("/api/time?days=0&tz=UTC").status_code == 400


def test_timer_time_per_size_counts_steps_their_total_and_the_median_step(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO steps (id, item_id, need, ask, size, created_at, updated_at, updated_by) VALUES"
        " (3, 1, 'merge', 'Merge', 'small', ?, ?, 's'), (4, 2, 'qa', 'Try it', 'large', ?, ?, 's')",
        (AT, AT, AT, AT),
    )
    conn.execute("UPDATE steps SET size = 'small' WHERE id = 1")
    _span(conn, 1, "2026-01-06T09:00:00Z", "2026-01-06T09:01:00Z")
    _span(conn, 1, "2026-01-06T09:10:00Z", "2026-01-06T09:13:00Z")
    _span(conn, 3, "2026-01-06T09:20:00Z", "2026-01-06T09:30:00Z")
    _span(conn, 4, "2026-01-06T10:00:00Z", "2026-01-06T10:40:00Z")
    _span(conn, 2, "2026-01-06T11:00:00Z", "2026-01-06T11:05:00Z")
    report = timesheet.report(conn, 1, _at("2026-01-06T12:00"), ZoneInfo("UTC"))
    assert report["sizes"] == [
        {"size": "small", "steps": 2, "seconds": 14 * 60, "median": 7 * 60},
        {"size": "large", "steps": 1, "seconds": 40 * 60, "median": 40 * 60},
    ]


def test_progress_estimates_on_finished_items_are_checked_against_the_users_timer_time(conn: sqlite3.Connection) -> None:
    assert timesheet.progress_check(conn, True) is None
    # Item 1 finished at 18:00. The user's timer ran on it 09:00-10:00, 12:00-13:00 and 17:00-17:30 (150 minutes),
    # and again after the finish. At 10:00 (60 of 150 minutes behind: 40 %) its session said 50 %, at 16:00 (120 of
    # 150: 80 %) 70 %; an estimate after the finish does not count. One estimate of the earlier kind (how far the
    # work was) said 80 % at 10:00. Item 2 is still open.
    conn.execute("UPDATE items SET stage = 'done', done_at = '2026-01-06T18:00:00Z' WHERE id = 1")
    _span(conn, 1, "2026-01-06T09:00:00Z", "2026-01-06T10:00:00Z")
    _span(conn, 1, "2026-01-06T12:00:00Z", "2026-01-06T13:00:00Z")
    _span(conn, 1, "2026-01-06T17:00:00Z", "2026-01-06T17:30:00Z")
    _span(conn, 1, "2026-01-06T18:30:00Z", "2026-01-06T19:00:00Z")
    conn.execute(
        "INSERT INTO progress_marks (step_id, item_id, at, progress, of_user_time) VALUES"
        " (1, 1, '2026-01-06T10:00:00Z', 50, 1), (1, 1, '2026-01-06T16:00:00Z', 70, 1), (1, 1, '2026-01-06T19:00:00Z', 100, 1),"
        " (1, 1, '2026-01-06T10:00:00Z', 80, 0), (2, 2, '2026-01-06T10:00:00Z', 10, 1)"
    )
    assert timesheet.progress_check(conn, True) == {"items": 1, "estimates": 2, "median_gap": 0, "median_off": 10}
    assert timesheet.progress_check(conn, False) == {"items": 1, "estimates": 1, "median_gap": 40, "median_off": 40}
    # Finished at 12:30 instead: of the 90 timer minutes by then, 60 were behind at 10:00, and 16:00 is past it.
    conn.execute("UPDATE items SET done_at = '2026-01-06T12:30:00Z' WHERE id = 1")
    assert timesheet.progress_check(conn, True) == {"items": 1, "estimates": 1, "median_gap": -17, "median_off": 17}
    # An item the user's timer never ran on has nothing to check against.
    conn.execute("DELETE FROM time_spans")
    assert timesheet.progress_check(conn, True) is None


def test_the_bar_is_corrected_only_once_enough_estimates_are_checked(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    conn.execute("UPDATE items SET stage = 'done', done_at = '2026-01-06T18:00:00Z' WHERE id = 1")
    _span(conn, 1, "2026-01-06T09:00:00Z", "2026-01-06T11:00:00Z")
    conn.execute(
        "INSERT INTO progress_marks (step_id, item_id, at, progress, of_user_time) VALUES"
        " (1, 1, '2026-01-06T09:00:00Z', 30, 1), (1, 1, '2026-01-06T10:00:00Z', 80, 1)"
    )
    check = {"items": 1, "estimates": 2, "median_gap": 30, "median_off": 30}
    assert timesheet.progress_check(conn, True) == check
    assert timesheet.progress_correction(conn) is None
    monkeypatch.setattr(timesheet, "CORRECTION_FROM", {"estimates": 2, "items": 1})
    assert timesheet.progress_correction(conn) == check
    monkeypatch.setattr(timesheet, "CORRECTION_FROM", {"estimates": 2, "items": 2})
    assert timesheet.progress_correction(conn) is None
