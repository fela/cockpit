"""The Settings page (SPEC CONTROL-35): off unless the setup turns it on; the briefing's hour and the weekly digest's
day and hour it sets, and how the scheduler, its retries and the Briefing page follow them."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from cockpit import briefing, config, db, store
from tests.conftest import World

# The tests' setup is in UTC+7 (tests/setup.toml). January 2026: Sunday 11, Monday 12.
TEAM = "Alex: lists and sharing\nAna: reminders and exports"
DEFAULTS = {"briefing_hour": 7, "digest_day": 6, "digest_hour": 8}

# The tests' own setup, which leaves the Settings page and the digest off.
SETUP = config.setup


def setup_with_settings() -> config.Setup:
    return dataclasses.replace(SETUP(), features=(briefing.DIGEST_FEATURE, briefing.SETTINGS_FEATURE))


setup_with_settings.cache_clear = SETUP.cache_clear  # type: ignore[attr-defined]


@pytest.fixture
def settings_on(monkeypatch) -> None:
    monkeypatch.setattr(config, "setup", setup_with_settings)


@pytest.fixture
def team(world: World) -> None:
    assert world.client.put("/api/team", json={"team": TEAM}, headers=world.user()).status_code == 200


def at(day: int, hour: int, minute: int = 5) -> datetime:
    """`hour`:`minute` on January `day` 2026 in the tests' UTC+7."""
    return datetime(2026, 1, day, hour, minute, tzinfo=timezone(timedelta(hours=7))).astimezone(UTC)


def put(world: World, **changes: int | None) -> dict:
    """Save the schedule as the page does: the whole of it, with `changes`."""
    response = world.client.put("/api/settings", json=DEFAULTS | changes, headers=world.user())
    assert response.status_code == 200, response.text
    return response.json()["schedule"]


def brief(world: World, day: int, hour: int, written: bool) -> None:
    """Have Cockpit ask for January `day`'s morning briefing at `hour`:05, and store it when `written`."""
    conn = db.connect(world.db_path)
    try:
        assert briefing.launch_once(conn, config.setup(), at(day, hour)) is not None
        if written:
            briefing.put(conn, "agent:briefing", f"2026-01-{day:02d}", f"## Executive summary\n\n- News of the {day}th.")
    finally:
        conn.close()


def digest(world: World, now: datetime) -> dict | None:
    conn = db.connect(world.db_path)
    try:
        message_id = briefing.launch_digest_once(conn, config.setup(), now)
        return store.get_message(conn, message_id) if message_id is not None else None
    finally:
        conn.close()


def listing(world: World, now: datetime) -> dict:
    conn = db.connect(world.db_path)
    try:
        return briefing.listing(conn, config.setup(), now)
    finally:
        conn.close()


def fail(world: World, message: dict) -> None:
    """The message does not reach a session."""
    world.runner.returncode = 1
    world.deliverer.send(message["id"])


@pytest.mark.usefixtures("team")
def test_the_settings_page_is_off_by_default_and_a_stored_schedule_then_counts_for_nothing(world: World) -> None:
    assert world.client.get("/settings").status_code == 404
    assert world.client.get("/api/settings", headers=world.user()).status_code == 404
    assert world.client.put("/api/settings", json=DEFAULTS, headers=world.user()).status_code == 404
    for path in ("/", "/briefing"):
        assert 'name="cockpit-settings"' not in world.client.get(path).text
    conn = db.connect(world.db_path)
    try:
        with db.transaction(conn):
            db.kv_set(conn, "schedule", {"briefing_hour": 10})
    finally:
        conn.close()
    brief(world, 12, 7, False)  # still at seven
    assert listing(world, at(12, 7))["hour"] == 7


@pytest.mark.usefixtures("settings_on")
def test_the_page_opens_from_the_menu_mark_and_stores_only_what_the_user_changed(world: World) -> None:
    page = world.client.get("/settings")
    assert page.status_code == 200
    assert "frame-ancestors 'self'" in page.headers["content-security-policy"]
    assert 'name="cockpit-settings"' in world.client.get("/").text
    shown = world.client.get("/api/settings", headers=world.user()).json()
    assert shown == {"schedule": DEFAULTS, "digest": True, "zone": config.setup().timezone.key}
    assert put(world, briefing_hour=9, digest_day=None) == {"briefing_hour": 9, "digest_day": None, "digest_hour": 8}
    conn = db.connect(world.db_path)
    try:
        assert db.kv_get(conn, "schedule") == {"briefing_hour": 9, "digest_day": None}
        assert put(world) == DEFAULTS
        assert db.kv_get(conn, "schedule") == {}
    finally:
        conn.close()


@pytest.mark.usefixtures("settings_on")
@pytest.mark.parametrize("changes", [
    {"briefing_hour": True}, {"briefing_hour": "5"}, {"briefing_hour": 5.0}, {"briefing_hour": 24},
    {"briefing_hour": -1}, {"briefing_hour": None}, {"digest_day": 7}, {"digest_day": "0"}, {"digest_hour": None},
    {"digest_hour": 24}, {"zone": "UTC"},
])
def test_the_schedule_takes_only_whole_hours_and_weekdays(world: World, changes: dict) -> None:
    response = world.client.put("/api/settings", json=DEFAULTS | changes, headers=world.user())
    assert response.status_code == 400, response.text
    assert world.client.get("/api/settings", headers=world.user()).json()["schedule"] == DEFAULTS


@pytest.mark.usefixtures("settings_on")
def test_only_the_user_changes_the_schedule(world: World) -> None:
    refused = world.client.put("/api/settings", json=DEFAULTS | {"briefing_hour": 9}, headers=world.agent())
    assert refused.status_code == 403
    assert world.client.get("/api/settings", headers=world.agent()).status_code == 403


@pytest.mark.usefixtures("settings_on", "team")
def test_the_briefing_comes_at_the_users_hour(world: World) -> None:
    put(world, briefing_hour=10)
    conn = db.connect(world.db_path)
    try:
        assert briefing.launch_once(conn, config.setup(), at(12, 9, 55)) is None
    finally:
        conn.close()
    brief(world, 12, 10, False)
    assert listing(world, at(12, 10))["hour"] == 10


@pytest.mark.usefixtures("settings_on", "team")
def test_a_monday_digest_covers_tuesday_to_monday(world: World) -> None:
    put(world, digest_day=0, digest_hour=9)
    brief(world, 5, 7, True)
    assert digest(world, at(5, 9))["new_name"] == "Weekly digest Mon 5 Jan"
    brief(world, 6, 7, True)
    brief(world, 11, 7, True)
    assert digest(world, at(11, 12)) is None  # Sunday no longer brings one
    brief(world, 12, 7, True)
    assert digest(world, at(12, 8, 55)) is None
    message = digest(world, at(12, 9))
    assert message is not None and message["new_name"] == "Weekly digest Mon 12 Jan"
    assert '<briefing day="Tuesday 6 January">' in message["text"]
    assert '<briefing day="Monday 12 January">' in message["text"]
    assert "Monday 5 January" not in message["text"]
    assert "reads it on Monday or Tuesday morning" in message["text"]


@pytest.mark.usefixtures("settings_on", "team")
def test_a_late_evening_digest_waits_past_midnight_for_a_late_briefing_and_keeps_its_week(world: World) -> None:
    put(world, briefing_hour=20, digest_hour=21)
    brief(world, 6, 20, True)
    brief(world, 11, 20, False)  # Sunday's briefing, still being written
    assert digest(world, at(11, 21)) is None
    assert digest(world, at(12, 0, 55)) is None
    message = digest(world, at(12, 1))  # four hours after 21:00
    assert message is not None and message["new_name"] == "Weekly digest Sun 11 Jan"
    assert '<briefing day="Tuesday 6 January">' in message["text"]


@pytest.mark.usefixtures("settings_on", "team")
def test_no_digest_stops_the_request_and_the_retry_of_a_failed_one(world: World) -> None:
    brief(world, 11, 7, True)
    first = digest(world, at(11, 8))
    fail(world, first)
    assert listing(world, at(11, 8, 10))["digest"]["retry_at"] == "2026-01-11T02:05:00Z"
    put(world, digest_day=None)
    assert listing(world, at(11, 8, 10))["digest"]["retry_at"] is None
    assert digest(world, at(11, 9, 10)) is None
    assert digest(world, at(18, 12)) is None


@pytest.mark.usefixtures("settings_on", "team")
def test_a_failure_too_close_to_the_users_next_digest_promises_no_retry(world: World) -> None:
    put(world, digest_day=0, digest_hour=9)
    brief(world, 12, 7, True)
    first = digest(world, at(19, 8, 30))  # the next Monday, before its digest hour
    assert first is not None and first["new_name"] == "Weekly digest Mon 12 Jan"
    fail(world, first)
    assert listing(world, at(19, 8, 40))["digest"]["retry_at"] is None
    assert digest(world, at(19, 9, 35)) is None


@pytest.mark.usefixtures("settings_on", "team")
def test_a_failed_digest_keeps_its_retry_when_the_user_moves_the_digest_later(world: World) -> None:
    brief(world, 11, 7, True)
    first = digest(world, at(11, 8))
    fail(world, first)
    put(world, digest_hour=11)
    assert digest(world, at(11, 9, 10)) is None  # the week starts at 11:00 now
    assert listing(world, at(11, 11, 0))["digest"]["retry_at"] == "2026-01-11T02:05:00Z"
    again = digest(world, at(11, 11, 0))
    assert again is not None and again["id"] != first["id"] and again["new_name"] == "Weekly digest Sun 11 Jan"


@pytest.mark.usefixtures("team")
def test_a_failed_digest_keeps_its_retry_after_a_time_zone_change(world: World) -> None:
    utc = dataclasses.replace(SETUP(), features=(briefing.DIGEST_FEATURE,), timezone=ZoneInfo("UTC"))
    new_york = dataclasses.replace(utc, timezone=ZoneInfo("America/New_York"))
    sunday = datetime(2026, 1, 11, tzinfo=UTC)
    conn = db.connect(world.db_path)
    try:
        assert briefing.launch_once(conn, utc, sunday.replace(hour=7, minute=5)) is not None
        briefing.put(conn, "agent:briefing", "2026-01-11", "## Executive summary\n\n- News of the 11th.")
        first = briefing.launch_digest_once(conn, utc, sunday.replace(hour=8, minute=5))
        assert first is not None
    finally:
        conn.close()
    fail(world, {"id": first})
    conn = db.connect(world.db_path)
    try:
        # 04:05 in New York: the week of 11 January starts at 08:00 there, and the retry waits for it.
        assert briefing.launch_digest_once(conn, new_york, sunday.replace(hour=9, minute=10)) is None
        again = briefing.launch_digest_once(conn, new_york, sunday.replace(hour=13, minute=0))
        assert again is not None and again != first
        assert store.get_message(conn, again)["new_name"] == "Weekly digest Sun 11 Jan"
    finally:
        conn.close()


@pytest.mark.usefixtures("settings_on", "team")
def test_the_digest_reads_the_schedule_in_the_transaction_that_queues_it(world: World, monkeypatch) -> None:
    brief(world, 11, 7, True)
    read = briefing.schedule
    inside: list[bool] = []

    def watched(conn, setup):
        inside.append(conn.in_transaction)
        return read(conn, setup)

    monkeypatch.setattr(briefing, "schedule", watched)
    assert digest(world, at(11, 8)) is not None
    assert inside and all(inside)
