"""The Settings page (SPEC CONTROL-33): the schedule of the morning briefing and the weekly digest, which the user
changes there, and how Cockpit follows it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from cockpit import briefing, config, db, settings, store
from tests.conftest import ORIGIN, World

TEAM = "Alex: lists and sharing\nAna: reminders and exports"


def at(day: int, hour: int, minute: int = 5) -> datetime:
    """`hour`:`minute` on January `day` 2026 in the tests' UTC+7 (tests/setup.toml); the 11th is a Sunday."""
    return datetime(2026, 1, day, hour, minute, tzinfo=timezone(timedelta(hours=7))).astimezone(UTC)


@pytest.fixture
def team(world: World) -> None:
    assert world.client.put("/api/team", json={"team": TEAM}, headers=world.user()).status_code == 200


def put(world: World, **changes: object):
    return world.client.put("/api/settings", json=changes, headers=world.user())


def launch(world: World, now: datetime, digest: bool = False) -> dict | None:
    conn = db.connect(world.db_path)
    try:
        ask = briefing.launch_digest_once if digest else briefing.launch_once
        message_id = ask(conn, config.setup(), now)
        return store.get_message(conn, message_id) if message_id is not None else None
    finally:
        conn.close()


def brief(world: World, day: int, hour: int = 7) -> None:
    assert launch(world, at(day, hour)) is not None
    conn = db.connect(world.db_path)
    try:
        briefing.put(conn, "agent:briefing", f"2026-01-{day:02d}", f"## Executive summary\n\n- News of the {day}th.")
    finally:
        conn.close()


def test_the_settings_start_at_their_defaults_and_keep_what_the_user_changes(world: World) -> None:
    shown = world.client.get("/api/settings", headers=world.user()).json()
    assert shown["schedule"] == shown["defaults"] == {"briefing_hour": 7, "digest_day": 6, "digest_hour": 8}
    assert shown["zone"] == config.setup().timezone.key and shown["team"] is None
    # A change keeps the values it leaves out.
    assert put(world, digest_day=0, digest_hour=9).json()["schedule"] == {"briefing_hour": 7, "digest_day": 0, "digest_hour": 9}
    assert put(world, briefing_hour=6).json()["schedule"] == {"briefing_hour": 6, "digest_day": 0, "digest_hour": 9}
    # Null is no digest; a default chosen again is no longer kept as a change.
    assert put(world, digest_day=None).json()["schedule"]["digest_day"] is None
    assert put(world, briefing_hour=7, digest_hour=8, digest_day=6).json()["schedule"] == settings.DEFAULTS
    conn = db.connect(world.db_path)
    try:
        assert db.kv_get(conn, settings.KEY) == {}
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("changes", "words"),
    [
        ({"briefing_hour": 24}, "briefing hour is an hour from 0 to 23"),
        ({"digest_hour": -1}, "digest hour is an hour from 0 to 23"),
        ({"digest_day": 7}, "the digest's day is 0 (Monday) to 6 (Sunday)"),
        ({"briefing_hour": None}, "null is not allowed for briefing_hour"),
        ({"timezone": "UTC"}, "timezone"),
    ],
)
def test_a_setting_out_of_range_is_refused_and_changes_nothing(world: World, changes: dict, words: str) -> None:
    refused = put(world, **changes)
    assert refused.status_code == 400 and words in refused.text
    assert world.client.get("/api/settings", headers=world.user()).json()["schedule"] == settings.DEFAULTS


def test_only_the_user_changes_the_settings(world: World) -> None:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    assert agent.get("/api/settings", headers=world.agent("briefing")).status_code == 403
    assert agent.put("/api/settings", json={"briefing_hour": 9}, headers=world.agent("briefing")).status_code == 403


@pytest.mark.usefixtures("team")
def test_the_briefing_comes_at_the_hour_the_user_set(world: World) -> None:
    assert put(world, briefing_hour=9).status_code == 200
    assert launch(world, at(8, 7)) is None
    assert launch(world, at(8, 9)) is not None
    assert world.client.get("/api/briefings").json()["hour"] == 9


@pytest.mark.usefixtures("team")
def test_a_monday_digest_comes_on_monday_on_the_week_to_monday(world: World) -> None:
    assert put(world, digest_day=0, digest_hour=9).status_code == 200
    for day in (5, 6, 11, 12):
        brief(world, day)
    assert launch(world, at(11, 9), digest=True) is None  # Sunday
    assert launch(world, at(12, 8), digest=True) is None  # Monday, before nine
    message = launch(world, at(12, 9), digest=True)
    assert message is not None and message["new_name"] == "Weekly digest Mon 12 Jan"
    text = message["text"]
    assert "Write Sam's weekly digest for the week to Monday 12 January. Sam reads it on Monday morning" in text
    # Tuesday 6 to Monday 12: the Monday before belongs to last week's digest.
    assert '<briefing day="Tuesday 6 January">' in text and '<briefing day="Monday 12 January">' in text
    assert "Monday 5 January" not in text


@pytest.mark.usefixtures("team")
def test_the_digest_waits_four_hours_from_its_own_hour_for_that_days_briefing(world: World) -> None:
    assert put(world, digest_hour=13).status_code == 200
    brief(world, 6)
    assert launch(world, at(11, 7)) is not None  # Sunday's briefing asked for, not yet written
    assert launch(world, at(11, 13), digest=True) is None
    assert launch(world, at(11, 16, 55), digest=True) is None
    assert launch(world, at(11, 17), digest=True) is not None


@pytest.mark.usefixtures("team")
def test_no_digest_comes_when_the_user_chose_none(world: World) -> None:
    assert put(world, digest_day=None).status_code == 200
    brief(world, 11)
    assert launch(world, at(11, 13), digest=True) is None


def test_the_settings_page_is_served_and_the_menu_links_to_it(world: World) -> None:
    page = world.client.get("/settings")
    assert page.status_code == 200 and "/static/settings.js" in page.text
    assert "frame-ancestors 'self'" in page.headers["content-security-policy"]
    menu = world.client.get("/static/prefs.js").text
    assert 'link.href = "/settings"' in menu
