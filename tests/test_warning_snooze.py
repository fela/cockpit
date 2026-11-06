from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cockpit import db, store
from tests.conftest import World


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_snoozed_warning_stays_snoozed_until_its_time_and_only_the_user_snoozes(world: World) -> None:
    later = iso(datetime.now(UTC) + timedelta(hours=2))
    assert world.client.get("/api/state").json()["snoozed_warnings"] == {}
    snoozed = world.client.post("/api/warnings/snooze", headers=world.user(), json={"key": "claude:work", "until": later})
    assert snoozed.status_code == 200, snoozed.text
    assert snoozed.json()["snoozed_warnings"] == {"claude:work": later}
    assert world.client.get("/api/state").json()["snoozed_warnings"] == {"claude:work": later}
    # An agent cannot hide a warning from the user.
    refused = world.client.post("/api/warnings/snooze", headers=world.agent(), json={"key": "claude:work", "until": later})
    assert refused.status_code == 403
    # A time already past is refused; null wakes it.
    past = iso(datetime.now(UTC) - timedelta(minutes=1))
    assert world.client.post("/api/warnings/snooze", headers=world.user(), json={"key": "claude:work", "until": past}).status_code == 400
    # An account problem's key holds its whole text, which can be long.
    long_key = "account-problem:" + "x" * 400
    assert world.client.post("/api/warnings/snooze", headers=world.user(), json={"key": long_key, "until": later}).status_code == 200
    woken = world.client.post("/api/warnings/snooze", headers=world.user(), json={"key": "claude:work", "until": None})
    assert woken.json()["snoozed_warnings"] == {long_key: later}


def test_a_snooze_that_ended_drops_out(world: World) -> None:
    now = datetime.now(UTC)
    conn = db.connect(world.db_path)
    store.snooze_warning(conn, "claude:work", iso(now + timedelta(minutes=5)), now)
    assert store.snoozed_warnings(conn, now) == {"claude:work": iso(now + timedelta(minutes=5))}
    assert store.snoozed_warnings(conn, now + timedelta(minutes=6)) == {}
