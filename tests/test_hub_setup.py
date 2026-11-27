"""The hub is a public part that the setup's [hub] table turns on (SPEC UI-81): without the table Cockpit has no hub
routes, tags, state or table; with it the part loads from the public code alone, and the table is checked as read."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cockpit import config, db, parts
from tests.conftest import World, use_hub
from tests.fake_hub import FakeHub
from tests.test_parts import client

HUB_TAGS = ("hub-foot.js", "hub-foot.css", "hub.js", "hub.css", "hub-left.js", "hub-left.css", "hub-accounts.js",
            "hub-accounts.css")


def tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


def migrated(path: Path) -> Path:
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()
    return path


@pytest.mark.parametrize("path", ["/hub", "/api/hub", "/api/hub/left"])
def test_without_a_hub_table_its_routes_answer_404(world: World, path) -> None:
    assert parts.installed() == []
    assert client(world, parts.installed()).get(path, headers=world.user(), follow_redirects=False).status_code == 404


def test_without_a_hub_table_the_pages_carry_nothing_of_the_hub(world: World, tmp_path: Path) -> None:
    browser = client(world, parts.installed())
    for path in ("/", "/usage"):
        page = browser.get(path, headers=world.user()).text
        for tag in HUB_TAGS:
            assert f"/static/{tag}" not in page, (path, tag)
    state = browser.get("/api/state", headers=world.user()).json()
    assert "hub" not in state and "page_words" not in state
    assert "hub_limit_readings" not in tables(migrated(tmp_path / "plain.db"))


@pytest.fixture
def stand_in(monkeypatch, tmp_path: Path):
    """A setup whose [hub] table names a stand-in hub with no accounts, a Codex sign-in, and no restarts or tunnel."""
    found = FakeHub("k" * 32)
    key_file = tmp_path / "hub-management-key"
    key_file.write_text("k" * 32 + "\n")
    use_hub(monkeypatch, config.HubSetup(url=found.url, key_file=key_file, sign_in={"codex": ("true",)}, restarts={},
                                         tunnel_state=None))
    yield found
    found.server.shutdown()


def test_with_a_hub_table_the_part_loads_from_the_public_code(stand_in: FakeHub, world: World, tmp_path: Path) -> None:
    from cockpit import hub_part

    # The base install lacks the private package (tests/conftest.py base_install): the hub is its only part.
    assert parts.setup_tables() == {} and parts.installed() == [hub_part.part]
    # A migration reads no setup file, so the hub's table comes with the part, not with db.migrate.
    assert "hub_limit_readings" not in tables(migrated(tmp_path / "hub.db"))
    assert "hub_limit_readings" in tables(world.db_path)
    browser = world.client
    page = browser.get("/usage", headers=world.user()).text
    for tag in ("hub.js", "hub-left.js", "hub-accounts.js", "page-update.js"):
        assert f'<script src="/static/{tag}' in page, tag
    assert 'name="cockpit-version"' in page
    assert '<script src="/static/hub-foot.js' in browser.get("/", headers=world.user()).text
    found = browser.get("/api/hub", headers=world.user()).json()["hub"]
    # The hub as last read: nothing yet, since the first read runs behind the request.
    assert found["accounts"] is None and found["error"] is None
    # Only what the setup gives: a Codex sign-in, no restarts, no tunnel to check.
    assert set(found["sign_in"]) == {"codex"} and found["restarts"] == [] and found["tunnel_checked"] is False
    assert "hub" in browser.get("/api/state", headers=world.user()).json()
    assert browser.get("/hub", headers=world.user(), follow_redirects=False).status_code == 308


def read(tmp_path: Path, hub: str) -> config.HubSetup | None:
    path = tmp_path / "setup.toml"
    path.write_text(hub)
    return config._read_hub(path, __import__("tomllib").loads(hub).get("hub"))


def test_a_hub_table_reads_as_given(tmp_path: Path) -> None:
    found = read(tmp_path, """
[hub]
url = "http://127.0.0.1:8317"
key_file = "/srv/hub/management-key"
tunnel_state = ["systemctl", "--user", "is-active", "hub-tunnel.service"]
[hub.sign_in]
codex = ["cli-proxy-api", "--codex-device-login"]
[hub.restarts]
hub = ["systemctl", "--user", "restart", "cliproxyapi.service"]
""")
    assert found == config.HubSetup(
        url="http://127.0.0.1:8317", key_file=Path("/srv/hub/management-key"),
        sign_in={"codex": ("cli-proxy-api", "--codex-device-login")},
        restarts={"hub": ("systemctl", "--user", "restart", "cliproxyapi.service")},
        tunnel_state=("systemctl", "--user", "is-active", "hub-tunnel.service"),
    )
    assert read(tmp_path, 'x = 1\n') is None


@pytest.mark.parametrize(("table", "names"), [
    ('[hub]\nurl = "http://localhost:8317?x=1"\nkey_file = "/k"\n', r"\[hub\]\.url"),
    ('[hub]\nurl = "http://localhost:8317#x"\nkey_file = "/k"\n', r"\[hub\]\.url"),
    ('[hub]\nurl = "http://127.0.0.1:1"\nkey_file = "~no-such-user-here/k"\n', r"\[hub\]\.key_file"),
])
def test_a_wrong_hub_field_is_named(tmp_path: Path, table: str, names: str) -> None:
    with pytest.raises(config.SetupError, match=names):
        read(tmp_path, table)


@pytest.mark.parametrize("table", [
    '[hub]\nkey_file = "/k"\n',
    '[hub]\nurl = "http://127.0.0.1:1"\n',
    '[hub]\nurl = "http://127.0.0.1:1"\nkey_file = "/k"\nport = 1\n',
    '[hub]\nurl = "http://127.0.0.1:1/"\nkey_file = "/k"\n',
    '[hub]\nurl = "127.0.0.1:1"\nkey_file = "/k"\n',
    '[hub]\nurl = "http://127.0.0.1:1"\nkey_file = "k"\n',
    '[hub]\nurl = "http://127.0.0.1:1"\nkey_file = "/k"\n[hub.sign_in]\ngemini = ["x"]\n',
    '[hub]\nurl = "http://127.0.0.1:1"\nkey_file = "/k"\n[hub.restarts]\nmachine = ["x"]\n',
    '[hub]\nurl = "http://127.0.0.1:1"\nkey_file = "/k"\n[hub.restarts]\nhub = []\n',
    '[hub]\nurl = "http://127.0.0.1:1"\nkey_file = "/k"\ntunnel_state = "systemctl is-active x"\n',
])
def test_a_wrong_hub_table_stops_the_setup(tmp_path: Path, table: str) -> None:
    with pytest.raises(config.SetupError, match=r"\[hub"):
        read(tmp_path, table)


def test_a_hub_table_added_after_start_reaches_no_hub_reader(world: World, tmp_path: Path, monkeypatch) -> None:
    # The server was built without the hub's part; a setup reload (a new time zone, say) then reads a [hub] table.
    # Until a restart builds the part, every reader of the hub's data answers as without a hub.
    key_file = tmp_path / "hub-management-key"
    key_file.write_text("k" * 32 + "\n")
    use_hub(monkeypatch, config.HubSetup(url="http://127.0.0.1:9", key_file=key_file, sign_in={}, restarts={},
                                         tunnel_state=None))
    assert world.client.get("/api/spend", params={"period": "7"}, headers=world.user()).status_code == 200
    conn = db.connect(world.db_path)
    try:
        assert parts.codex_weeks(conn) == []
        assert parts.provider_availability(conn, datetime.now(UTC)) is None
        assert parts.model_capacity(conn, datetime.now(UTC)) is None
    finally:
        conn.close()


def test_a_hub_table_whose_key_file_is_missing_is_no_hub(tmp_path: Path, monkeypatch, caplog) -> None:
    key_file = tmp_path / "missing-key"
    use_hub(monkeypatch, config.HubSetup(url="http://127.0.0.1:9", key_file=key_file, sign_in={}, restarts={},
                                         tunnel_state=None))
    assert parts.installed() == []
    assert [record.getMessage() for record in caplog.records if record.levelname == "WARNING"] == [
        f"the setup's [hub] key_file {key_file} is missing, so Cockpit runs without the hub"]
