"""Scheduled prompts (SPEC BG-14): a prompt that starts a new session at a set time, never twice for one slot."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from cockpit import db, schedules, store
from tests.conftest import ORIGIN, World

ZONE = ZoneInfo("Etc/GMT-7")
# Sunday 15 Nov 2026, 12:00 in the user's zone; the next Monday 09:00 there is 02:00 UTC on 16 Nov.
SUNDAY = datetime(2026, 11, 15, 5, 0, tzinfo=UTC)
MONDAY_RUN = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)


def agent(world: World) -> TestClient:
    return TestClient(world.client.app, base_url=ORIGIN)


def weekly(world: World, **fields) -> dict:
    agent(world).put("/api/projects", json={"name": "app", "about": "the app"}, headers=world.agent("projects"))
    return {
        "name": "app-review", "prompt": "Review this week's changes.", "cwd": str(world.checkout), "title": "App review",
        "project": "app", "every": "weekly", "day": "monday", "at": "09:00",
    } | fields


def put(world: World, body: dict, now: datetime = SUNDAY) -> dict:
    conn = db.connect(world.db_path)
    return schedules.put(conn, body, "agent:reviewer", world.settings.checkouts_dir, now, ZONE)


def check(world: World, now: datetime) -> list[int]:
    return schedules.check_once(db.connect(world.db_path), world.settings.checkouts_dir, now, ZONE)


def messages(world: World) -> list[dict]:
    return db.rows(db.connect(world.db_path).execute("SELECT * FROM messages ORDER BY id").fetchall())


def test_slots_are_in_the_users_zone() -> None:
    monday = {"every": "weekly", "weekday": 0, "at": "09:00"}
    assert schedules.next_slot(monday, SUNDAY, ZONE).astimezone(UTC) == MONDAY_RUN
    assert schedules.latest_slot(monday, MONDAY_RUN, ZONE).astimezone(UTC) == MONDAY_RUN
    assert schedules.latest_slot(monday, MONDAY_RUN - timedelta(seconds=1), ZONE).astimezone(UTC) == MONDAY_RUN - timedelta(days=7)
    daily = {"every": "daily", "weekday": None, "at": "23:30"}
    # 23:30 in the user's zone is 16:30 UTC the same day.
    assert schedules.next_slot(daily, SUNDAY, ZONE).astimezone(UTC) == datetime(2026, 11, 15, 16, 30, tzinfo=UTC)


@pytest.mark.parametrize("at, now, expected", [
    ("02:30", datetime(2026, 3, 8, 7, 0, tzinfo=UTC), datetime(2026, 3, 7, 7, 30, tzinfo=UTC)),
    ("02:30", datetime(2026, 3, 8, 7, 30, tzinfo=UTC), datetime(2026, 3, 8, 7, 30, tzinfo=UTC)),
    ("01:30", datetime(2026, 11, 1, 6, 15, tzinfo=UTC), datetime(2026, 11, 1, 5, 30, tzinfo=UTC)),
])
def test_slots_compare_elapsed_time_across_daylight_saving_changes(at: str, now: datetime, expected: datetime) -> None:
    schedule = {"every": "daily", "weekday": None, "at": at}
    zone = ZoneInfo("America/New_York")
    assert schedules.latest_slot(schedule, now, zone).astimezone(UTC) == expected
    assert schedules.next_slot(schedule, now, zone).astimezone(UTC) > now


def test_resuming_in_the_repeated_hour_keeps_a_completed_slot(world: World) -> None:
    zone = ZoneInfo("America/New_York")
    conn = db.connect(world.db_path)
    body = weekly(world, every="daily", at="01:30")
    body.pop("day")
    try:
        schedules.put(conn, body, "agent:reviewer", world.settings.checkouts_dir,
                      datetime(2026, 11, 1, 5, 0, tzinfo=UTC), zone)
        assert len(schedules.check_once(conn, world.settings.checkouts_dir,
                                       datetime(2026, 11, 1, 5, 30, tzinfo=UTC), zone)) == 1
        schedules.pause(conn, body["name"], True, datetime(2026, 11, 1, 5, 45, tzinfo=UTC), zone)
        schedules.pause(conn, body["name"], False, datetime(2026, 11, 1, 6, 15, tzinfo=UTC), zone)
        assert schedules.check_once(conn, world.settings.checkouts_dir,
                                    datetime(2026, 11, 1, 6, 30, tzinfo=UTC), zone) == []
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    finally:
        conn.close()


def test_a_new_schedule_waits_for_its_next_slot_then_starts_one_session(world: World) -> None:
    shown = put(world, weekly(world))
    assert shown["next_at"] == "2026-11-16T02:00:00Z"
    assert check(world, SUNDAY) == []
    assert check(world, MONDAY_RUN - timedelta(minutes=1)) == []
    sent = check(world, MONDAY_RUN)
    assert len(sent) == 1
    message = messages(world)[-1]
    assert message["id"] == sent[0]
    assert message["author"] == "agent:reviewer"
    assert message["new_cwd"] == str(world.checkout)
    assert message["text"].endswith("Review this week's changes.")
    assert "Scheduled prompt app-review, which runs Mondays at 09:00" in message["text"]
    conn = db.connect(world.db_path)
    item = store.get_item(conn, message["item_id"])
    assert (item["key"], item["title"], item["project"]) == ("note:schedule-app-review-2026-11-16-0900","App review 16 Nov", "app")
    # A run is work that repeats, so the sidebar lists it under Background (SPEC BG-3).
    assert item["recurring"] == 1
    # The same slot never starts again, on a later check or after a restart (a new connection reads the same row).
    assert check(world, MONDAY_RUN + timedelta(minutes=1)) == []
    assert check(world, MONDAY_RUN + timedelta(days=6)) == []
    assert len(check(world, MONDAY_RUN + timedelta(days=7))) == 1
    assert len(messages(world)) == 2


def test_a_missed_check_starts_the_latest_slot_once_late(world: World) -> None:
    put(world, {key: value for key, value in weekly(world, every="daily").items() if key != "day"})
    # Down from Sunday until Wednesday noon: Monday's, Tuesday's and Wednesday's slots passed; only Wednesday's runs.
    wednesday_noon = datetime(2026, 11, 18, 5, 0, tzinfo=UTC)
    assert len(check(world, wednesday_noon)) == 1
    item = store.get_item(db.connect(world.db_path), messages(world)[-1]["item_id"])
    assert item["key"] == "note:schedule-app-review-2026-11-18-0900"
    assert check(world, wednesday_noon + timedelta(hours=1)) == []


def test_changing_the_prompt_keeps_the_slot_and_changing_the_time_moves_it(world: World) -> None:
    put(world, weekly(world))
    put(world, weekly(world, prompt="Review it all."), MONDAY_RUN + timedelta(hours=1))
    # Monday's slot had not run; a new prompt leaves it due.
    assert len(check(world, MONDAY_RUN + timedelta(hours=1))) == 1
    put(world, weekly(world, day="tuesday", at="08:00"), MONDAY_RUN + timedelta(days=2))
    # Tuesday 08:00 had passed when the time changed, so it waits for the next Tuesday.
    assert check(world, MONDAY_RUN + timedelta(days=2)) == []


def test_a_later_time_the_same_day_runs_on_its_own_item(world: World) -> None:
    put(world, weekly(world))
    assert len(check(world, MONDAY_RUN)) == 1
    put(world, weekly(world, at="15:00"), MONDAY_RUN + timedelta(hours=1))
    assert len(check(world, MONDAY_RUN + timedelta(hours=6))) == 1
    first, second = messages(world)[-2:]
    assert first["item_id"] != second["item_id"]
    item = store.get_item(db.connect(world.db_path), second["item_id"])
    assert item["key"] == "note:schedule-app-review-2026-11-16-1500"


def test_a_paused_schedule_starts_nothing_and_resuming_skips_what_passed(world: World) -> None:
    put(world, weekly(world))
    conn = db.connect(world.db_path)
    assert schedules.pause(conn, "app-review", True, SUNDAY, ZONE)["next_at"] is None
    assert check(world, MONDAY_RUN) == []
    schedules.pause(conn, "app-review", False, MONDAY_RUN + timedelta(hours=1), ZONE)
    assert check(world, MONDAY_RUN + timedelta(hours=1)) == []
    assert len(check(world, MONDAY_RUN + timedelta(days=7))) == 1


def test_a_run_that_cannot_start_shows_its_error_and_tries_again(world: World, caplog, monkeypatch) -> None:
    put(world, weekly(world))
    real = store.create_message

    def broken(*args, **kwargs):
        raise store.Invalid("/gone is not a checkout")

    monkeypatch.setattr(store, "create_message", broken)
    assert check(world, MONDAY_RUN) == []
    assert check(world, MONDAY_RUN + timedelta(minutes=1)) == []
    assert caplog.text.count("starting the scheduled prompt app-review failed") == 1
    conn = db.connect(world.db_path)
    assert schedules.warnings(conn) == ["The scheduled prompt app-review could not start its run: /gone is not a checkout"]
    # The failed run left no item or message behind.
    assert conn.execute("SELECT COUNT(*) FROM items WHERE key LIKE 'note:schedule-%'").fetchone()[0] == 0
    assert state_warnings(world) == schedules.warnings(conn)
    monkeypatch.setattr(store, "create_message", real)
    assert len(check(world, MONDAY_RUN + timedelta(minutes=2))) == 1
    assert schedules.warnings(conn) == []


def state_warnings(world: World) -> list[str]:
    return [each for each in world.client.get("/api/state").json()["warnings"] if "scheduled prompt" in each]


@pytest.mark.parametrize(("fields", "words"), [
    ({"name": "Has Spaces"}, "`name`"),
    ({"cwd": "/etc"}, "is not a checkout"),
    ({"title": "A long title for every weekly run"}, "too long"),
    ({"project": "nowhere"}, "nowhere"),
    ({"every": "monthly"}, "daily or weekly"),
    ({"day": "mon"}, "names its `day`"),
    ({"at": "9:00"}, "HH:MM"),
    ({"prompt": ""}, "`prompt`"),
])
def test_a_put_refuses_what_it_cannot_run(world: World, fields: dict, words: str) -> None:
    with pytest.raises(store.Invalid, match=words):
        put(world, weekly(world, **fields))


def test_a_daily_schedule_names_no_day(world: World) -> None:
    with pytest.raises(store.Invalid, match="leave out `day`"):
        put(world, weekly(world, every="daily"))


def test_sessions_put_list_and_remove_and_only_the_user_pauses(world: World) -> None:
    body = weekly(world)
    made = agent(world).post("/api/schedules", json=body, headers=world.agent("reviewer"))
    assert made.status_code == 200, made.text
    assert made.json()["author"] == "agent:reviewer"
    listed = agent(world).get("/api/schedules", headers=world.agent("reviewer")).json()
    assert [each["name"] for each in listed] == ["app-review"]
    refused = agent(world).post("/api/schedules/app-review/pause", json={"paused": True}, headers=world.agent("reviewer"))
    assert refused.status_code == 403
    paused = world.client.post("/api/schedules/app-review/pause", json={"paused": True}, headers=world.user())
    assert paused.status_code == 200 and paused.json()["paused"] is True
    # A session's put leaves the user's pause as it is.
    again = agent(world).post("/api/schedules", json=body | {"prompt": "Other words."}, headers=world.agent("reviewer"))
    assert again.json()["paused"] is True
    removed = agent(world).delete("/api/schedules/app-review", headers=world.agent("reviewer"))
    assert removed.status_code == 200
    assert agent(world).delete("/api/schedules/app-review", headers=world.agent("reviewer")).status_code == 404


def test_the_last_run_shows_its_message_state(world: World) -> None:
    put(world, weekly(world))
    message_id = check(world, MONDAY_RUN)[0]
    conn = db.connect(world.db_path)
    with db.transaction(conn):
        conn.execute("UPDATE messages SET state = 'failed', detail = 'Workspace not trusted' WHERE id = ?", (message_id,))
    run = schedules.get(conn, "app-review", MONDAY_RUN, ZONE)["last_run"]
    assert (run["message_id"], run["state"], run["failure"]) == (message_id, "failed", "the workspace was not trusted")
