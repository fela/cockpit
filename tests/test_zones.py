"""The user's time zone and the question about the browser's (SPEC CONTROL-32). The tests' setup runs on Etc/GMT-7,
the clock of Asia/Jakarta."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cockpit import config, db, store, zones

from .conftest import World

START = datetime(2026, 11, 11, 3, 0, tzinfo=UTC)


@pytest.fixture
def conn(world: World) -> Iterator:
    connection = db.connect(world.db_path)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def own_setup(tmp_path: Path, monkeypatch) -> Iterator[Path]:
    """A copy of the tests' setup that a test may change; the next test reads the checked-in one again."""
    path = tmp_path / "setup.toml"
    shutil.copy(config.SETUP_PATH, path)
    monkeypatch.setattr(config, "SETUP_PATH", path)
    config.setup.cache_clear()
    yield path
    monkeypatch.undo()
    config.setup.cache_clear()


def test_a_zone_with_another_clock_asks_only_after_six_hours(conn) -> None:
    assert zones.report(conn, "Asia/Tokyo", START) == {"zone": "Etc/GMT-7", "ask": None}
    assert zones.report(conn, "Asia/Tokyo", START + timedelta(hours=5, minutes=59))["ask"] is None
    assert zones.report(conn, "Asia/Tokyo", START + timedelta(hours=6))["ask"] == {"zone": "Asia/Tokyo", "since": "2026-11-11T03:00:00Z"}


def test_a_zone_with_the_same_clock_never_asks(conn) -> None:
    # Jakarta and Ho Chi Minh City keep UTC+7 all year, as the setup's Etc/GMT-7 does.
    for zone in ("Asia/Jakarta", "Asia/Ho_Chi_Minh"):
        zones.report(conn, zone, START)
        assert zones.report(conn, zone, START + timedelta(days=1))["ask"] is None
    assert db.kv_get(conn, zones.SEEN_KEY) == {}


def test_utc_and_etc_zones_never_ask(conn) -> None:
    # A private window or a fingerprint blocker reports UTC wherever the user is.
    for zone in ("UTC", "Etc/GMT", "Etc/GMT+5"):
        zones.report(conn, zone, START)
        assert zones.report(conn, zone, START + timedelta(days=1))["ask"] is None


def test_a_legacy_name_a_browser_reports_is_understood(conn) -> None:
    # Chrome reports India as Asia/Calcutta, a name only the tzdata package carries on some systems.
    zones.report(conn, "Asia/Calcutta", START)
    assert zones.report(conn, "Asia/Calcutta", START + timedelta(hours=7))["ask"]["zone"] == "Asia/Calcutta"


def test_a_name_that_is_no_zone_is_refused(conn) -> None:
    for name in ("Mars/Olympus", "../setup", ""):
        with pytest.raises(store.Invalid):
            zones.report(conn, name, START)


def test_the_question_goes_only_to_the_browser_showing_the_zone(conn) -> None:
    zones.report(conn, "Asia/Tokyo", START)
    zones.report(conn, "Asia/Tokyo", START + timedelta(hours=7))
    # The phone left behind still shows Jakarta time: it gets no question, and the laptop's count goes on.
    assert zones.report(conn, "Asia/Jakarta", START + timedelta(hours=7))["ask"] is None
    assert zones.report(conn, "Asia/Tokyo", START + timedelta(hours=8))["ask"]["zone"] == "Asia/Tokyo"


def test_keep_silences_the_zone_until_it_goes_unreported_for_two_days(conn) -> None:
    zones.report(conn, "Asia/Tokyo", START)
    zones.report(conn, "Asia/Tokyo", START + timedelta(hours=7))
    assert zones.keep(conn, "Asia/Tokyo", START + timedelta(hours=7))["ask"] is None
    assert zones.report(conn, "Asia/Tokyo", START + timedelta(days=3))["ask"] is None
    # Back for two days, then a new trip: it counts again from the trip's first report.
    back = START + timedelta(days=5, hours=1)
    assert zones.report(conn, "Asia/Tokyo", back)["ask"] is None
    assert zones.report(conn, "Asia/Tokyo", back + timedelta(hours=6))["ask"]["since"] == zones.iso(back)


def test_same_clock_looks_a_year_ahead(conn) -> None:
    # London matches Abidjan in November but not in July.
    assert not zones.same_clock(ZoneInfo("Europe/London"), ZoneInfo("Africa/Abidjan"), START)
    assert zones.same_clock(ZoneInfo("Europe/London"), ZoneInfo("Europe/Lisbon"), START)
    assert zones.same_clock(ZoneInfo("Asia/Jakarta"), ZoneInfo("Etc/GMT-7"), START)


def test_put_rewrites_the_setup_and_starts_every_count_again(conn, own_setup: Path) -> None:
    zones.report(conn, "Asia/Tokyo", START)
    assert zones.put(conn, own_setup, "Asia/Tokyo", START + timedelta(hours=7)) == {"zone": "Asia/Tokyo", "ask": None}
    assert 'timezone = "Asia/Tokyo"' in own_setup.read_text()
    assert config.setup().timezone == ZoneInfo("Asia/Tokyo")
    assert db.kv_get(conn, zones.SEEN_KEY) == {}
    # Back again, Jakarta's clock now differs from Cockpit's and counts from its first report.
    assert zones.report(conn, "Asia/Jakarta", START + timedelta(days=1))["ask"] is None
    assert list(db.kv_get(conn, zones.SEEN_KEY)) == ["Asia/Jakarta"]


def test_put_refuses_a_setup_without_one_timezone_line(conn, own_setup: Path) -> None:
    config.setup()  # A running Cockpit has read its setup before anyone edits the file.
    own_setup.write_text(own_setup.read_text().replace('timezone = "Etc/GMT-7"\n', ""))
    with pytest.raises(RuntimeError, match="no single timezone line"):
        zones.put(conn, own_setup, "Asia/Tokyo", START)


def test_the_page_reports_keeps_and_switches(world: World, own_setup: Path) -> None:
    assert world.client.get("/api/timezone", headers=world.user()).json() == {"zone": "Etc/GMT-7", "ask": None}
    answer = world.client.post("/api/timezone/report", json={"zone": "Asia/Tokyo"}, headers=world.user())
    assert answer.json() == {"zone": "Etc/GMT-7", "ask": None}
    assert world.client.post("/api/timezone/report", json={"zone": "Nowhere/City"}, headers=world.user()).status_code == 400
    assert world.client.post("/api/timezone/keep", json={"zone": "Asia/Tokyo"}, headers=world.user()).json()["ask"] is None
    answer = world.client.put("/api/timezone", json={"zone": "Asia/Tokyo"}, headers=world.user())
    assert answer.json() == {"zone": "Asia/Tokyo", "ask": None}
    assert 'timezone = "Asia/Tokyo"' in own_setup.read_text()


def test_only_the_user_changes_the_zone(world: World, own_setup: Path) -> None:
    for method, path in (("get", "/api/timezone"), ("post", "/api/timezone/report"), ("post", "/api/timezone/keep"), ("put", "/api/timezone")):
        kwargs = {} if method == "get" else {"json": {"zone": "Asia/Tokyo"}}
        assert getattr(world.client, method)(path, headers=world.agent(), **kwargs).status_code == 403
    assert 'timezone = "Etc/GMT-7"' in own_setup.read_text()
