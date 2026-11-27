"""The hub accounts' limits kept as rows and the Usage page's graph of the allowance left drawn from them (SPEC UI-87)."""

import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cockpit import codex, db, hub, hub_left
from cockpit.hub_left import Reading
from tests.conftest import lose_the_hub_key

NOW = 1_794_460_000.0
DAY = 86400
END = NOW + 7 * DAY
A = "claude/a.json"
B = "claude/b.json"

# Each test runs with a [hub] table in the setup whose key file is missing, until it supplies a stand-in hub.
pytestmark = pytest.mark.usefixtures("hub_on")


def week(account: str, at: float, used: float, resets_at: float) -> Reading:
    return Reading(account, "seven_day", at, used, resets_at)


def said(at: float, used: float, resets_at: float) -> dict[str, Any]:
    """A reading as hub.limits_now keeps it."""
    return {
        "used_percentage": used,
        "resets_at": resets_at,
        "captured_at": datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def limits(windows: dict[str, dict[str, Any]], models: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {"windows": windows, "models": models, "plan": ""}


def stored(conn: sqlite3.Connection) -> list[tuple[str, str, float, float, float]]:
    found = conn.execute(
        "SELECT account, limit_key, at, used, resets_at FROM hub_limit_readings ORDER BY at, limit_key"
    )
    return [tuple(each) for each in found]


@pytest.fixture
def conn(tmp_path: Path):
    found = db.connect(tmp_path / "cockpit.db")
    db.migrate(found)
    hub_left.create_table(found)
    yield found
    found.close()


def only_group(readings: list[Reading], accounts: list[str]) -> dict[str, Any]:
    graph = hub_left.graph(readings, accounts, NOW)
    assert len(graph["groups"]) == 1
    return graph["groups"][0]


# record


def test_record_keeps_the_first_reading_of_each_limit(conn):
    hub_left.record(conn, {A: limits({"seven_day": said(NOW - 100, 10, NOW + 2 * DAY)}, {})}, NOW)
    assert stored(conn) == [(A, "seven_day", NOW - 100, 10, NOW + 2 * DAY)]


def test_record_writes_nothing_for_an_unchanged_reading(conn):
    hub_left.record(conn, {A: limits({"seven_day": said(NOW - 100, 10, NOW + 2 * DAY)}, {})}, NOW)
    changes = conn.total_changes
    hub_left.record(conn, {A: limits({"seven_day": said(NOW - 50, 10.4, NOW + 2 * DAY)}, {})}, NOW)
    assert conn.total_changes == changes
    assert len(stored(conn)) == 1


def test_record_skips_a_reset_moved_by_seconds(conn):
    hub_left.record(conn, {A: limits({"seven_day": said(NOW - 100, 10, NOW + 2 * DAY)}, {})}, NOW)
    hub_left.record(conn, {A: limits({"seven_day": said(NOW - 50, 10, NOW + 2 * DAY + 30)}, {})}, NOW)
    assert len(stored(conn)) == 1


def test_record_keeps_a_change_of_one_point(conn):
    hub_left.record(conn, {A: limits({"seven_day": said(NOW - 100, 10, NOW + 2 * DAY)}, {})}, NOW)
    hub_left.record(conn, {A: limits({"seven_day": said(NOW - 50, 11, NOW + 2 * DAY)}, {})}, NOW)
    assert stored(conn)[-1] == (A, "seven_day", NOW - 50, 11, NOW + 2 * DAY)


def test_record_skips_a_change_said_no_later_than_the_last_row(conn):
    hub_left.record(conn, {A: limits({"seven_day": said(NOW - 100, 10, NOW + 2 * DAY)}, {})}, NOW)
    hub_left.record(conn, {A: limits({"seven_day": said(NOW - 100, 20, NOW + 2 * DAY)}, {})}, NOW)
    assert len(stored(conn)) == 1


def test_record_keeps_a_new_window(conn):
    hub_left.record(conn, {A: limits({"seven_day": said(NOW - 100, 10, NOW + 2 * DAY)}, {})}, NOW)
    hub_left.record(conn, {A: limits({"seven_day": said(NOW - 50, 10, NOW + 9 * DAY)}, {})}, NOW)
    assert stored(conn)[-1] == (A, "seven_day", NOW - 50, 10, NOW + 9 * DAY)


def test_record_keeps_the_five_hour_window_and_each_model(conn):
    windows = {"five_hour": said(NOW - 100, 40, NOW + 3600), "seven_day": said(NOW - 100, 10, NOW + 2 * DAY)}
    hub_left.record(conn, {A: limits(windows, {"Fable": said(NOW - 100, 30, NOW + 2 * DAY)})}, NOW)
    assert [(each[0], each[1]) for each in stored(conn)] == [(A, "five_hour"), (A, "model:Fable"), (A, "seven_day")]


def test_record_drops_rows_older_than_thirty_days_when_it_writes(conn):
    conn.execute("INSERT INTO hub_limit_readings VALUES (?, 'seven_day', ?, 5, ?)", (B, NOW - 31 * DAY, NOW - 25 * DAY))
    conn.execute("INSERT INTO hub_limit_readings VALUES (?, 'seven_day', ?, 5, ?)", (B, NOW - 29 * DAY, NOW - 25 * DAY))
    hub_left.record(conn, {B: limits({"seven_day": said(NOW - 29 * DAY, 5, NOW - 25 * DAY)}, {})}, NOW)
    assert len(stored(conn)) == 2
    hub_left.record(conn, {A: limits({"seven_day": said(NOW - 100, 10, NOW + 2 * DAY)}, {})}, NOW)
    assert [each[2] for each in stored(conn)] == [NOW - 29 * DAY, NOW - 100]


def test_codex_weeks_reads_the_codex_accounts_weeks_oldest_first(conn):
    """The Codex use of a folder on the hub takes its shares from these rows (codex.AccountIndex._hub_shares)."""
    codex_account = "codex/c.json"
    hub_left.record(conn, {codex_account: limits({"seven_day": said(NOW - 50, 20, NOW + 2 * DAY),
                                                  "five_hour": said(NOW - 50, 40, NOW + 3600)}, {}),
                           A: limits({"seven_day": said(NOW - 100, 30, NOW + 2 * DAY)}, {})}, NOW)
    assert hub_left.codex_weeks(conn) == [codex.HubReading(codex_account, NOW - 50, 20, NOW + 2 * DAY)]
    hub_left.record(conn, {codex_account: limits({"seven_day": said(NOW - 10, 25, NOW + 2 * DAY)}, {})}, NOW)
    assert [each.used for each in hub_left.codex_weeks(conn)] == [20, 25]


def test_rows_reads_the_last_two_weeks_oldest_first(conn):
    for at in (NOW - 15 * DAY, NOW - DAY, NOW - 13 * DAY):
        conn.execute("INSERT INTO hub_limit_readings VALUES (?, 'seven_day', ?, 5, ?)", (A, at, NOW + DAY))
    assert hub_left.rows(conn, NOW) == [week(A, NOW - 13 * DAY, 5, NOW + DAY), week(A, NOW - DAY, 5, NOW + DAY)]


# One account's lines


def test_an_account_in_the_middle_of_its_window():
    readings = [
        week(A, NOW - 4 * DAY, 10, NOW + 2 * DAY),
        week(A, NOW - 2 * DAY, 30, NOW + 2 * DAY),
        week(A, NOW - DAY, 50, NOW + 2 * DAY),
    ]
    group = only_group(readings, [A])
    account = group["accounts"][0]
    assert (account["id"], account["now"], account["resets_at"]) == ("a.json", 50.0, NOW + 2 * DAY)
    assert account["past"] == [
        [NOW - 5 * DAY, 100.0],
        [NOW - 4 * DAY, 90.0],
        [NOW - 2 * DAY, 70.0],
        [NOW - DAY, 50.0],
        [NOW, 50.0],
    ]
    assert account["idle"] == [[NOW, 50.0], [NOW + 2 * DAY, 50.0], [NOW + 2 * DAY, 100.0], [END, 100.0]]
    # Since the first reading the line fell 40 points in 4 days: 10 a day.
    assert account["pace"] == [[NOW, 50.0], [NOW + 2 * DAY, 30.0], [NOW + 2 * DAY, 100.0], [END, 50.0]]
    assert (group["now"], group["past"], group["idle"], group["pace"]) == (
        50.0,
        account["past"],
        account["idle"],
        account["pace"],
    )


def test_a_reset_moved_by_seconds_stays_in_the_same_window():
    readings = [week(A, NOW - 4 * DAY, 10, NOW + 2 * DAY), week(A, NOW - 2 * DAY, 30, NOW + 2 * DAY + 60)]
    account = only_group(readings, [A])["accounts"][0]
    assert account["past"] == [[NOW - 5 * DAY, 100.0], [NOW - 4 * DAY, 90.0], [NOW - 2 * DAY, 70.0], [NOW, 70.0]]


def test_the_pace_stays_at_zero_once_it_gets_there():
    # 20 points in 1.25 days: 16 a day, so the 20 left last 1.25 days.
    readings = [week(A, NOW - 108000, 60, NOW + 2 * DAY), week(A, NOW, 80, NOW + 2 * DAY)]
    account = only_group(readings, [A])["accounts"][0]
    assert account["pace"] == [
        [NOW, 20.0],
        [NOW + 108000, 0.0],
        [NOW + 2 * DAY, 0.0],
        [NOW + 2 * DAY, 100.0],
        [END, 20.0],
    ]


def test_the_pace_is_measured_over_a_day_at_the_least():
    resets_at = NOW + 7 * DAY - 3600
    readings = [week(A, NOW - 1800, 10, resets_at), week(A, NOW - 900, 20, resets_at)]
    account = only_group(readings, [A])["accounts"][0]
    assert account["past"] == [[NOW - 3600, 100.0], [NOW - 1800, 90.0], [NOW - 900, 80.0], [NOW, 80.0]]
    # 10 points over the half hour since the first reading count as 10 a day.
    assert account["pace"] == [[NOW, 80.0], [resets_at, 10.4], [resets_at, 100.0], [END, 99.6]]


@pytest.mark.parametrize("resets_at", [NOW + 3600, NOW + 3 * DAY, NOW + 6 * DAY - 3600])
def test_the_pace_leaves_out_the_line_before_the_first_reading(resets_at):
    # The line from 100 at the window's start to the first reading is a guess from the reset time: the same two
    # readings give the same pace, 10 points in a day, wherever the reset falls.
    readings = [week(A, NOW - DAY, 10, resets_at), week(A, NOW, 20, resets_at)]
    account = only_group(readings, [A])["accounts"][0]
    assert account["pace"][:2] == [[NOW, 80.0], [resets_at, round(80 - 10 * (resets_at - NOW) / DAY, 1)]]


def test_one_reading_has_no_pace():
    account = only_group([week(A, NOW - DAY, 80, NOW + 2 * DAY)], [A])["accounts"][0]
    assert account["past"][0] == [NOW - 5 * DAY, 100.0]
    assert account["pace"] == account["idle"]


def test_a_window_that_ended_before_now_without_a_later_reading():
    account = only_group([week(A, NOW - 6 * DAY, 30, NOW - 2 * DAY)], [A])["accounts"][0]
    assert (account["now"], account["resets_at"]) == (100.0, None)
    # The window started 9 days ago; the line is cut at the graph's left edge, a week ago, where it stood at 80.
    assert account["past"] == [
        [NOW - 7 * DAY, 80.0],
        [NOW - 6 * DAY, 70.0],
        [NOW - 2 * DAY, 70.0],
        [NOW - 2 * DAY, 100.0],
        [NOW, 100.0],
    ]
    assert account["idle"] == [[NOW, 100.0], [END, 100.0]]
    # Nothing was used after the one reading.
    assert account["pace"] == [[NOW, 100.0], [END, 100.0]]


def test_two_windows_with_a_reset_between():
    readings = [week(A, NOW - 5 * DAY, 60, NOW - 3 * DAY), week(A, NOW - DAY, 20, NOW + 5 * DAY)]
    account = only_group(readings, [A])["accounts"][0]
    assert account["past"] == [
        [NOW - 7 * DAY, 64.0],
        [NOW - 5 * DAY, 40.0],
        [NOW - 3 * DAY, 40.0],
        [NOW - 3 * DAY, 100.0],
        [NOW - 2 * DAY, 100.0],
        [NOW - DAY, 80.0],
        [NOW, 80.0],
    ]
    assert (account["now"], account["resets_at"]) == (80.0, NOW + 5 * DAY)


def test_a_window_read_before_the_last_one_reset_goes_on_from_the_last_reading():
    readings = [week(A, NOW - 3 * DAY, 30, NOW + 2 * DAY), week(A, NOW - DAY, 40, NOW + 3 * DAY)]
    account = only_group(readings, [A])["accounts"][0]
    assert account["past"] == [[NOW - 5 * DAY, 100.0], [NOW - 3 * DAY, 70.0], [NOW - DAY, 60.0], [NOW, 60.0]]


# Groups


def test_a_group_is_the_mean_of_its_accounts():
    readings = [week(A, NOW - 3 * DAY, 40, NOW + DAY), week(B, NOW - 2 * DAY, 20, NOW + 3 * DAY)]
    group = only_group(readings, [A, B])
    assert [each["id"] for each in group["accounts"]] == ["a.json", "b.json"]
    assert group["now"] == 70.0
    # From the time both are known, B's window start 4 days ago.
    assert group["past"] == [[NOW - 4 * DAY, 86.7], [NOW - 3 * DAY, 75.0], [NOW - 2 * DAY, 70.0], [NOW, 70.0]]
    assert group["idle"] == [
        [NOW, 70.0],
        [NOW + DAY, 70.0],
        [NOW + DAY, 90.0],
        [NOW + 3 * DAY, 90.0],
        [NOW + 3 * DAY, 100.0],
        [END, 100.0],
    ]


@pytest.mark.parametrize("a_resets_at", [NOW + 3 * DAY, NOW + 4 * DAY])
def test_a_group_pace_falls_at_one_rate_while_an_account_has_some_left(a_resets_at):
    # A has used 20 points a day and has nothing left; B has used 10 a day and has 60 left. B takes A's use too, so the
    # group falls 15 a day, from 30 to 0 in two days, wherever A's reset falls.
    readings = [
        week(A, NOW - DAY, 80, a_resets_at),
        week(A, NOW, 100, a_resets_at),
        week(B, NOW - DAY, 30, NOW + 5 * DAY),
        week(B, NOW, 40, NOW + 5 * DAY),
    ]
    group = only_group(readings, [A, B])
    assert group["pace"][:4] == [[NOW, 30.0], [NOW + 2 * DAY, 0.0], [a_resets_at, 0.0], [a_resets_at, 50.0]]
    a, b = group["accounts"]
    assert a["pace"][:3] == [[NOW, 0.0], [a_resets_at, 0.0], [a_resets_at, 100.0]]
    assert b["pace"][:2] == [[NOW, 60.0], [NOW + 2 * DAY, 0.0]]


def test_an_account_without_readings_is_left_out():
    group = only_group([week(A, NOW - DAY, 40, NOW + DAY)], [A, B])
    assert [each["id"] for each in group["accounts"]] == ["a.json"]


def test_an_account_not_asked_for_is_left_out():
    group = only_group([week(A, NOW - DAY, 40, NOW + DAY), week(B, NOW - DAY, 20, NOW + DAY)], [B])
    assert [each["id"] for each in group["accounts"]] == ["b.json"]


def test_the_groups_come_in_order_with_fable_alone():
    readings = [
        week("codex/c.json", NOW - DAY, 10, NOW + DAY),
        Reading(A, "model:Fable", NOW - DAY, 30, NOW + DAY),
        week(A, NOW - DAY, 20, NOW + DAY),
    ]
    groups = hub_left.graph(readings, ["codex/c.json", A], NOW)["groups"]
    assert [(each["provider"], each["model"], each["now"]) for each in groups] == [
        ("claude", None, 80.0),
        ("claude", "Fable", 70.0),
        ("codex", None, 90.0),
    ]
    assert groups[2]["accounts"][0]["id"] == "c.json"


def test_the_five_hour_window_is_not_drawn():
    readings = [Reading(A, "five_hour", NOW - 600, 30, NOW + 3600)]
    assert hub_left.graph(readings, [A], NOW)["groups"] == []


def test_no_readings_give_no_groups():
    assert hub_left.graph([], [], NOW) == {"now": NOW, "start": NOW - 7 * DAY, "end": END, "groups": []}


# The route


def test_the_graph_is_none_without_a_hub(world):
    lose_the_hub_key()
    assert world.client.get("/api/hub/left", headers=world.user()).json() == {"left": None}


def test_the_graph_is_none_before_the_hubs_first_read(world, monkeypatch):
    read = {"accounts": None, "error": None, "read_at": None, "service": None, "tunnel": None}
    monkeypatch.setattr(hub.Hub, "read", lambda self, now, show_code: read)
    assert world.client.get("/api/hub/left", headers=world.user()).json() == {"left": None}


def test_the_graph_leaves_out_signed_out_accounts(world, monkeypatch):
    accounts = [
        {"provider": "claude", "id": "a.json", "state": "working"},
        {"provider": "claude", "id": "b.json", "state": "signed_out"},
    ]
    monkeypatch.setattr(hub.Hub, "read", lambda self, now, show_code: {"accounts": accounts})
    # The route reads the real clock, so the rows are an hour old by it.
    at = time.time() - 3600
    conn = db.connect(world.db_path)
    db.migrate(conn)
    for account in (A, B):
        conn.execute("INSERT INTO hub_limit_readings VALUES (?, 'seven_day', ?, 10, ?)", (account, at, at + 3 * DAY))
    conn.close()
    left = world.client.get("/api/hub/left", headers=world.user()).json()["left"]
    assert [each["id"] for group in left["groups"] for each in group["accounts"]] == ["a.json"]
