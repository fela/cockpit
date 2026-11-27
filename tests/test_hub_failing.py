"""Cockpit's own investigation when a provider's requests through the hub fail in numbers (SPEC UI-52, UI-90)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cockpit import db, hub, hub_failing, store
from tests.conftest import World
from tests.fake_hub import FakeHub
from tests.test_hub import CODEX, KEY, TUNNEL_ACTIVE, database

NOW = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
# The test setup enables Claude alone.
ENABLED = ("claude",)
HUB_FOOT_JS = Path(hub_failing.__file__).parent / "static" / "hub-foot.js"

# Each test runs with a [hub] table in the setup whose key file is missing, until it supplies a stand-in hub.
pytestmark = pytest.mark.usefixtures("hub_on")


def span(minutes_ago: int, succeeded: int, failed: int, now: datetime = NOW) -> dict[str, Any]:
    return {"at": (now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "succeeded": succeeded, "failed": failed}


def account(provider: str, *spans: dict[str, Any]) -> dict[str, Any]:
    return {"provider": provider, "traffic": list(spans)}


def failing_codex(now: datetime) -> list[dict[str, Any]]:
    """A read at `now` in which 55 of 155 Codex requests failed in the last 20 minutes, over two spans."""
    return [account("codex", span(15, 50, 30, now), span(5, 50, 25, now)), account("claude", span(5, 100, 0, now))]


FAILING_CODEX = failing_codex(NOW)


def test_the_page_and_cockpit_use_the_same_numbers():
    script = HUB_FOOT_JS.read_text()
    numbers = {name: re.search(rf"const {name} = (.+);", script).group(1)
               for name in ("HUB_FAILING_MS", "HUB_FAILING_COUNT", "HUB_FAILING_SHARE")}
    assert numbers == {"HUB_FAILING_MS": "20 * 60 * 1000", "HUB_FAILING_COUNT": str(hub_failing.FAILING_COUNT),
                       "HUB_FAILING_SHARE": str(hub_failing.FAILING_SHARE)}
    assert hub_failing.FAILING_SECONDS == 20 * 60


@pytest.mark.parametrize(("accounts", "found"), [
    # 55 of 155 in the last 20 minutes, over two accounts.
    (FAILING_CODEX, {"codex": (55, 155)}),
    ([account("codex", span(15, 20, 10)), account("codex", span(5, 20, 10))], {"codex": (20, 60)}),
    # Fewer than 20 failures, or fewer than a quarter of the requests.
    ([account("codex", span(5, 10, 19))], {}),
    ([account("codex", span(5, 61, 20))], {}),
    # A span that started before the last 20 minutes does not count.
    ([account("codex", span(25, 0, 40), span(5, 10, 10))], {}),
    # A provider the page has no warning for.
    ([account("gemini", span(5, 0, 40))], {}),
])
def test_a_provider_is_failing_when_the_page_warns(accounts, found):
    assert hub_failing.failing(accounts, NOW) == found


def test_the_last_20_minutes_count_from_the_whole_second_the_page_gets():
    accounts = [account("codex", span(20, 60, 20))]
    assert hub_failing.failing(accounts, NOW + timedelta(milliseconds=500)) == {"codex": (20, 80)}
    assert hub_failing.failing(accounts, NOW + timedelta(seconds=1)) == {}


@pytest.mark.parametrize(("enabled", "failing_provider", "runs_on"), [
    (("codex", "claude"), "codex", "claude"),
    (("claude",), "claude", "claude"),
])
def test_the_investigation_runs_on_a_provider_other_than_the_failing_one(world: World, enabled, failing_provider,
                                                                         runs_on):
    conn = db.connect(world.db_path)
    accounts = [account(failing_provider, span(5, 0, 30))]
    sent = hub_failing.investigate(conn, accounts, NOW, world.checkout, world.settings.checkouts_dir, enabled)
    assert conn.execute("SELECT provider FROM messages WHERE id = ?", (sent[0],)).fetchone()[0] == runs_on


def test_failures_start_an_item_and_a_session_in_cockpits_checkout_that_hold_the_warning(world: World):
    conn = db.connect(world.db_path)
    sent = hub_failing.investigate(conn, FAILING_CODEX, NOW, world.checkout, world.settings.checkouts_dir, ENABLED)
    assert len(sent) == 1
    item_id = store.handed_warnings(conn)["hub-failing-codex"]
    item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    assert item["title"] == "Codex hub requests failing"
    assert item["summary"].startswith("Cockpit started this on its own: 55 of 155 Codex requests")
    assert item["priority"] == "high"
    world.deliverer.send(sent[0])
    args, cwd = world.runner.calls[-1]
    assert cwd == world.checkout
    assert any("Cockpit started this investigation on its own" in arg and "55 of 155 Codex requests" in arg
               for arg in args)

    # The next reads while the failures go on start nothing more.
    assert hub_failing.investigate(conn, FAILING_CODEX, NOW + timedelta(minutes=1), world.checkout,
                                   world.settings.checkouts_dir, ENABLED) == []
    # Nor once the item is done, until six hours have passed.
    conn.execute("UPDATE items SET stage = 'done' WHERE id = ?", (item_id,))
    conn.commit()
    assert store.handed_warnings(conn) == {}
    later = NOW + timedelta(hours=5)
    assert hub_failing.investigate(conn, failing_codex(later), later, world.checkout, world.settings.checkouts_dir, ENABLED) == []
    later = NOW + timedelta(hours=6)
    again = hub_failing.investigate(conn, failing_codex(later), later, world.checkout, world.settings.checkouts_dir, ENABLED)
    assert len(again) == 1
    assert store.handed_warnings(conn)["hub-failing-codex"] != item_id


def test_a_warning_the_user_handed_or_snoozed_starts_nothing(world: World):
    conn = db.connect(world.db_path)
    mine = store.add_item(conn, "The Codex failures", None, False,
                          dict.fromkeys(("provider", "model", "effort", "account")), False, None)
    store.hand_warning(conn, "hub-failing-codex", mine["id"])
    assert hub_failing.investigate(conn, FAILING_CODEX, NOW, world.checkout, world.settings.checkouts_dir, ENABLED) == []
    conn.execute("UPDATE items SET stage = 'done' WHERE id = ?", (mine["id"],))
    conn.commit()
    store.snooze_warning(conn, "hub-failing-codex", (NOW + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), NOW)
    assert hub_failing.investigate(conn, FAILING_CODEX, NOW, world.checkout, world.settings.checkouts_dir, ENABLED) == []
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_each_read_the_hub_answers_is_handed_on_and_a_failure_acting_on_it_is_logged(tmp_path, caplog):
    fake_hub = FakeHub(KEY)
    fake_hub.credentials = [CODEX]
    reads = []

    def after_read(accounts: list[dict[str, Any]], now: datetime) -> None:
        reads.append((accounts, now))
        raise RuntimeError("broken")

    path = tmp_path / "management-key"
    path.write_text(KEY + "\n")
    try:
        found = hub.Hub(fake_hub.url, path, {}, TUNNEL_ACTIVE, {}, database(tmp_path), after_read).refresh(NOW)
    finally:
        fake_hub.server.shutdown()
    assert found["error"] is None
    assert reads == [(found["accounts"], NOW)]
    assert "acting on a read of the CLIProxyAPI hub failed" in caplog.text


def test_an_investigation_whose_session_cannot_start_gives_the_warning_back(world: World, caplog):
    conn = db.connect(world.db_path)
    [message_id] = hub_failing.investigate(conn, FAILING_CODEX, NOW, world.checkout, world.settings.checkouts_dir, ENABLED)
    item_id = store.handed_warnings(conn)["hub-failing-codex"]
    # Still on its way, the message keeps the warning with the item.
    later = NOW + timedelta(minutes=1)
    assert hub_failing.investigate(conn, failing_codex(later), later, world.checkout, world.settings.checkouts_dir, ENABLED) == []
    assert store.handed_warnings(conn) == {"hub-failing-codex": item_id}
    conn.execute("UPDATE messages SET state = 'failed', detail = 'Cannot start independent Codex worker' WHERE id = ?",
                 (message_id,))
    conn.commit()
    later = NOW + timedelta(minutes=2)
    # The next read gives the warning back to Inbox and logs why; the six hours before another investigation hold.
    assert hub_failing.investigate(conn, failing_codex(later), later, world.checkout, world.settings.checkouts_dir, ENABLED) == []
    assert store.handed_warnings(conn) == {}
    assert "could not start (Cannot start independent Codex worker)" in caplog.text
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
