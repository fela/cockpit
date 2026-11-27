"""The tightness reading of each provider's hub allowance (SPEC SESSION-28)."""

import threading
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from cockpit import db, hub
from cockpit import hub_tightness as tightness
from cockpit.hub_left import Reading
from cockpit.hub_tightness import Account

# A Monday, 00:00 UTC, in a user's timezone of UTC: hour h of the test is hour h of the week.
NOW = datetime(2026, 11, 16, tzinfo=UTC).timestamp()
ZONE = ZoneInfo("UTC")
HOUR = 3600
DAY = 86400
A = "claude/a.json"
B = "claude/b.json"

# Each test runs with a [hub] table in the setup whose key file is missing, until it supplies a stand-in hub.
pytestmark = pytest.mark.usefixtures("hub_on")


def week(account: str, left: float, per_hour: float, resets_at: float) -> list[Reading]:
    """Readings of an account's week that leave it `left` percent now, falling `per_hour` points an hour over the
    last two days."""
    return [
        Reading(account, "seven_day", NOW - 2 * DAY, 100 - left - per_hour * 48, resets_at),
        Reading(account, "seven_day", NOW, 100 - left, resets_at),
    ]


def five_hour(account: str, used: float, resets_at: float) -> Reading:
    return Reading(account, "five_hour", NOW - HOUR, used, resets_at)


def hour_key(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%dT%H:00:00Z")


# The shape


def test_the_shape_is_flat_under_a_week_of_history():
    use = {hour_key(NOW - each * HOUR): 5.0 for each in range(1, 6 * 24)}
    assert tightness.shape(use, NOW, ZONE) == [1.0] * 48


def test_the_shape_is_flat_without_use():
    assert tightness.shape({}, NOW, ZONE) == [1.0] * 48
    assert tightness.shape({hour_key(NOW - 20 * DAY): 0.0}, NOW, ZONE) == [1.0] * 48


def test_each_hour_of_the_day_is_measured_against_the_average_hour_of_a_week():
    # Two weeks of use: 3 an hour on weekdays' working hours (8 to 18), 1 an hour at other times.
    use = {}
    for each in range(1, 14 * 24 + 1):
        at = NOW - each * HOUR
        moment = datetime.fromtimestamp(at, UTC)
        use[hour_key(at)] = 3.0 if moment.weekday() < 5 and 8 <= moment.hour < 18 else 1.0
    shape = tightness.shape(use, NOW, ZONE)
    busy, quiet = 5 * 10, 168 - 5 * 10
    average = (3 * busy + quiet) / 168
    assert shape[10] == pytest.approx(3 / average)  # a weekday's 10:00
    assert shape[2] == pytest.approx(1 / average)  # a weekday's 02:00
    assert shape[24 + 10] == pytest.approx(1 / average)  # a weekend day's 10:00
    # A week at the shape uses as much as a week of average hours.
    assert sum(each * days for each, days in zip(shape, tightness.BIN_DAYS, strict=True)) == pytest.approx(168)


def test_the_shape_counts_the_hours_without_use_since_the_first_one():
    # Use at Monday 10:00 alone, over two weeks: the weekdays' 10:00 holds all of it, 6 over 10 weekdays.
    use = {hour_key(NOW - 14 * DAY + 10 * HOUR): 2.0, hour_key(NOW - 7 * DAY + 10 * HOUR): 4.0}
    shape = tightness.shape(use, NOW, ZONE)
    assert shape[10] == pytest.approx(0.6 / (0.6 * 5 / 168))
    assert shape[11] == 0


def test_the_shape_is_in_the_users_timezone():
    # Two weeks of use at 09:00 UTC on weekdays: 10:00 in Berlin.
    use = {}
    for each in range(1, 14 * 24 + 1):
        at = NOW - each * HOUR
        moment = datetime.fromtimestamp(at, UTC)
        use[hour_key(at)] = 1.0 if moment.weekday() < 5 and moment.hour == 9 else 0.0
    shape = tightness.shape(use, NOW, ZoneInfo("Europe/Berlin"))
    assert shape[10] > 0
    assert [i for i, each in enumerate(shape) if each > 0] == [10]


def test_the_shape_reads_only_the_last_four_weeks():
    use = {hour_key(NOW - 40 * DAY): 100.0, **{hour_key(NOW - each * HOUR): 1.0 for each in range(1, 10 * 24)}}
    assert tightness.shape(use, NOW, ZONE) == pytest.approx([1.0] * 48)


def test_negative_use_fails():
    with pytest.raises(ValueError, match="use of -1.0"):
        tightness.shape({hour_key(NOW - 8 * DAY): -1.0}, NOW, ZONE)


# Stepping ahead


def test_use_runs_out_at_the_shape_of_the_hours_ahead():
    accounts = [Account(50.0, None, 1.0)]
    assert tightness.run_out(accounts, [1.0] * 48, ZONE, 1.0, NOW, NOW + 7 * DAY) == pytest.approx(NOW + 50 * HOUR)
    assert tightness.run_out(accounts, [2.0] * 48, ZONE, 1.0, NOW, NOW + 7 * DAY) == pytest.approx(NOW + 25 * HOUR)
    # No use in a weekday's first 10 hours: 14 an hour each weekday from 10:00, so the 50 last to Thursday 18:00.
    shape = [0.0] * 10 + [1.0] * 38
    assert tightness.run_out(accounts, shape, ZONE, 1.0, NOW, NOW + 7 * DAY) == pytest.approx(NOW + 90 * HOUR)


def test_use_that_lasts_to_the_end_does_not_run_out():
    assert tightness.run_out([Account(50.0, None, 1.0)], [1.0] * 48, ZONE, 1.0, NOW, NOW + 40 * HOUR) is None


def test_an_empty_accounts_use_moves_to_the_others():
    accounts = [Account(0.0, None, 1.0), Account(30.0, None, 1.0)]
    assert tightness.run_out(accounts, [1.0] * 48, ZONE, 1.0, NOW, NOW + 7 * DAY) == pytest.approx(NOW + 15 * HOUR)


def test_an_account_has_its_whole_week_again_at_its_reset():
    # A has 3 left and resets in an hour; B has nothing left until long after. Both use 1 an hour, which goes to A.
    accounts = [Account(3.0, NOW + HOUR, 1.0), Account(0.0, NOW + 60 * HOUR, 1.0)]
    # After the reset A has 100 at 2 an hour: 50 hours more.
    assert tightness.run_out(accounts, [1.0] * 48, ZONE, 1.0, NOW, NOW + 60 * HOUR) == pytest.approx(NOW + 51 * HOUR)


# The reading


@pytest.mark.parametrize(("resets_in", "expected"), [(80, "tight"), (45, "normal"), (30, "room")])
def test_the_reading_compares_the_week_left_with_the_expected_use(resets_in, expected):
    # 50 left at 1 an hour lasts 50 hours, and 40 at a quarter more.
    reading = tightness.read([A], week(A, 50, 1, NOW + resets_in * HOUR), {}, ZONE, NOW)
    assert reading.reading == expected
    if expected == "tight":
        assert reading.hours_left == pytest.approx(50)
    else:
        assert reading.hours_left is None
    assert reading.next_reset == NOW + resets_in * HOUR


def test_tight_names_the_reset_after_it_runs_out():
    readings = [*week(A, 3, 1, NOW + HOUR), *week(B, 0, 1, NOW + 60 * HOUR)]
    reading = tightness.read([A, B], readings, {}, ZONE, NOW)
    assert (reading.reading, reading.next_reset) == ("tight", NOW + 60 * HOUR)
    assert reading.hours_left == pytest.approx(51)
    assert reading.why == "At the expected use the week left runs out before a reset frees more."


def test_every_five_hour_window_near_full_is_tight_until_the_first_resets():
    readings = [
        *week(A, 80, 0.1, NOW + 5 * DAY), five_hour(A, 95, NOW + 2 * HOUR),
        *week(B, 80, 0.1, NOW + 5 * DAY), five_hour(B, tightness.usage.FIVE_HOUR_NEAR, NOW + 3 * HOUR),
    ]
    reading = tightness.read([A, B], readings, {}, ZONE, NOW)
    assert (reading.reading, reading.hours_left, reading.next_reset) == ("tight", 0.0, NOW + 2 * HOUR)


@pytest.mark.parametrize("b_five_hour", [[five_hour(B, 50, NOW + 3 * HOUR)], [five_hour(B, 95, NOW - 1)], []])
def test_one_account_with_room_in_its_five_hour_window_is_not_tight(b_five_hour):
    readings = [*week(A, 80, 0.1, NOW + 5 * DAY), five_hour(A, 95, NOW + 2 * HOUR), *week(B, 80, 0.1, NOW + 5 * DAY),
                *b_five_hour]
    assert tightness.read([A, B], readings, {}, ZONE, NOW).reading == "room"


def test_an_account_without_week_left_leaves_its_five_hour_window_out():
    readings = [*week(A, 80, 0.1, NOW + 5 * DAY), five_hour(A, 95, NOW + 2 * HOUR), *week(B, 0, 0.1, NOW + 4 * DAY),
                five_hour(B, 10, NOW + 3 * HOUR)]
    assert tightness.read([A, B], readings, {}, ZONE, NOW).reading == "tight"
    assert tightness.read([A, B], readings, {}, ZONE, NOW).hours_left == 0.0


def test_accounts_that_used_their_whole_week_are_tight_until_the_first_reset():
    # No use since the last readings, so the pace is 0: the empty week alone makes it tight.
    readings = [*week(A, 0, 0, NOW + DAY), *week(B, 0, 0, NOW + 2 * DAY)]
    assert tightness.read([A, B], readings, {}, ZONE, NOW) == tightness.Tightness(
        "tight", 0.0, NOW + DAY, "Every account has used its whole week."
    )


def test_an_empty_accounts_earlier_weekly_reset_ends_the_five_hour_tightness():
    readings = [*week(A, 80, 0.1, NOW + 5 * DAY), five_hour(A, 95, NOW + 2 * HOUR), *week(B, 0, 0.1, NOW + HOUR),
                five_hour(B, 10, NOW + 3 * HOUR)]
    assert tightness.read([A, B], readings, {}, ZONE, NOW).next_reset == NOW + HOUR


def test_the_hours_ahead_follow_a_half_hour_timezone():
    # In Kolkata (UTC+5:30), from 05:30 there: use is 10 times the average in the hour from 06:00, so 2 left at 1 an
    # hour lasts the half hour to 06:00 and 1.5 / 10 of an hour after it.
    zone = ZoneInfo("Asia/Kolkata")
    start = datetime(2026, 11, 16, 5, 30, tzinfo=zone).timestamp()
    shape = [1.0] * 48
    shape[6] = 10.0
    out = tightness.run_out([Account(2.0, None, 1.0)], shape, zone, 1.0, start, start + DAY)
    assert out == pytest.approx(start + 0.5 * HOUR + 0.15 * HOUR)


def test_no_usable_account_is_tight():
    assert tightness.read([], [], {}, ZONE, NOW) == tightness.Tightness(
        "tight", 0.0, None, "No account is signed in and enabled."
    )


def test_no_known_week_gives_no_reading():
    assert tightness.read([A], [five_hour(A, 10, NOW + HOUR)], {}, ZONE, NOW) is None


def test_the_readings_take_the_usable_accounts_by_provider():
    accounts = [
        {"provider": "claude", "id": "a.json", "state": "cooling"},
        {"provider": "claude", "id": "b.json", "state": "disabled"},
        {"provider": "codex", "id": "c.json", "state": "signed_out"},
    ]
    # B, turned off, would run out at once; A lasts to its reset.
    rows = [*week(A, 50, 1, NOW + 30 * HOUR), *week(B, 0, 1, NOW + 90 * HOUR)]
    found = tightness.readings(accounts, rows, {"claude": {}, "codex": {}}, ZONE, NOW)
    assert found == {
        "claude": {
            "reading": "room", "hours_left": None, "next_reset": "2026-11-17T06:00:00Z",
            "why": "The week left lasts until the resets even at a quarter more than the expected use.",
        },
        "codex": {
            "reading": "tight", "hours_left": 0.0, "next_reset": None, "why": "No account is signed in and enabled.",
        },
    }


# The routes


def state_worked_out(world) -> dict:
    """The state once the thread that works out the reading, which the first ask starts, has ended."""
    first = world.client.get("/api/state", headers=world.user()).json()
    for each in threading.enumerate():
        if each.name == "hub-tightness":
            each.join(10)
    assert first["hub"]["tightness"] is None
    return world.client.get("/api/state", headers=world.user()).json()


def test_the_hub_state_and_the_graph_carry_the_reading(world, monkeypatch):
    accounts = [{"provider": "claude", "id": "a.json", "state": "working"}]
    monkeypatch.setattr(hub.Hub, "read", lambda self, now, show_code: {"accounts": accounts})
    # The routes read the real clock.
    now = time.time()
    conn = db.connect(world.db_path)
    db.migrate(conn)
    conn.execute("INSERT INTO hub_limit_readings VALUES (?, 'seven_day', ?, 2, ?)", (A, now - 2 * DAY, now + 3 * DAY))
    conn.execute("INSERT INTO hub_limit_readings VALUES (?, 'seven_day', ?, 50, ?)", (A, now - 60, now + 3 * DAY))
    conn.commit()
    state = state_worked_out(world)
    reading = state["hub"]["tightness"]["claude"]
    assert reading["reading"] == "tight"
    assert reading["hours_left"] == pytest.approx(50, abs=0.1)
    assert reading["next_reset"] == datetime.fromtimestamp(now + 3 * DAY, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Worked out once per five minutes: a new reading of the limits shows in the next.
    conn.execute("INSERT INTO hub_limit_readings VALUES (?, 'seven_day', ?, 5, ?)", (A, now - 30, now + 7 * DAY))
    conn.close()
    left = world.client.get("/api/hub/left", headers=world.user()).json()
    assert left["tightness"] == state["hub"]["tightness"]


def test_a_failed_reading_leaves_the_state_working(world, monkeypatch, caplog):
    accounts = [{"provider": "claude", "id": "a.json", "state": "working"}]
    monkeypatch.setattr(hub.Hub, "read", lambda self, now, show_code: {"accounts": accounts})

    def broken(*args):
        raise ValueError("bad data")

    monkeypatch.setattr(tightness, "readings", broken)
    state = state_worked_out(world)
    assert state["hub"]["tightness"] is None
    assert "hub tightness: could not work out the reading" in caplog.text


def test_no_reading_before_the_hubs_first_read(world, monkeypatch):
    read = {"accounts": None, "error": None, "read_at": None, "service": None, "tunnel": None}
    monkeypatch.setattr(hub.Hub, "read", lambda self, now, show_code: dict(read))
    assert world.client.get("/api/state", headers=world.user()).json()["hub"]["tightness"] is None
