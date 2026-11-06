"""One setup file per install (SPEC SCOPE-5): what it must hold, and that its user's name reaches the texts."""

from __future__ import annotations

import dataclasses
import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cockpit import cli, config, delivery
from tests.conftest import ORIGIN, World

ROOT = Path(__file__).resolve().parents[1]
TESTS_SETUP = Path(__file__).parent / "setup.toml"
# What the repo does not publish: git's own folder, local environments and caches, and the lock file.
SKIPPED = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules", "uv.lock"}


def write_setup(folder: Path, changes: dict[str, Any]) -> Path:
    """The tests' setup with `changes` applied (None removes a key), written to `folder`. A list of tables
    goes last, as TOML's [[key]] tables; any other value as `key = <JSON>`, which TOML reads the same."""
    values = {key: value for key, value in (tomllib.loads(TESTS_SETUP.read_text()) | changes).items() if value is not None}
    tables = {key: value for key, value in values.items()
              if isinstance(value, list) and value and all(isinstance(e, dict) for e in value)}
    lines = [f"{key} = {json.dumps(value)}\n" for key, value in values.items() if key not in tables]
    for key, entries in tables.items():
        for entry in entries:
            lines += [f"\n[[{key}]]\n", *(f"{name} = {json.dumps(value)}\n" for name, value in entry.items())]
    path = folder / "setup.toml"
    path.write_text("".join(lines))
    return path


def test_the_example_setup_holds_every_key() -> None:
    example = config.read_setup(ROOT / "docs" / "setup.example.toml")
    assert "{session}" in example.attach


def test_the_timezone_is_read(tmp_path: Path) -> None:
    read = config.read_setup(write_setup(tmp_path, {}))
    assert read.timezone.key == "Etc/GMT-7"


def test_last_use_gives_each_address_its_end_in_seconds(tmp_path: Path) -> None:
    assert config.read_setup(write_setup(tmp_path, {})).last_use == {}
    path = write_setup(tmp_path, {})
    path.write_text(path.read_text() + 'last_use = {"sam@work.example" = 2026-01-15T10:00:00Z}\n')
    assert config.read_setup(path).last_use == {"sam@work.example": datetime(2026, 1, 15, 10, tzinfo=UTC).timestamp()}


@pytest.mark.parametrize("value", ['{"sam@work.example" = 2026-01-15T10:00:00}', '{"sam" = 2026-01-15T10:00:00Z}',
                                   '{"sam@work.example" = "Friday"}', '"Friday"'])
def test_a_wrong_last_use_stops_with_the_key_it_names(tmp_path: Path, value: str) -> None:
    path = write_setup(tmp_path, {})
    path.write_text(path.read_text() + f"last_use = {value}\n")
    with pytest.raises(config.SetupError, match="last_use must give each address a time with its offset"):
        config.read_setup(path)


def test_paths_may_start_with_home(tmp_path: Path) -> None:
    read = config.read_setup(write_setup(tmp_path, {"checkouts": "~/dev"}))
    assert read.checkouts == Path.home() / "dev"


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"email": None}, "missing email"),
        ({"colour": "blue"}, "unknown colour"),
        ({"name": " "}, "name must be text that is not empty"),
        ({"checkouts": "dev/example"}, "checkouts must be absolute paths"),
        ({"email": "user"}, "email must be a Google account's address"),
        ({"origin": "https://cockpit.example.test/"}, "origin must be the page's https:// address"),
        ({"attach": "claude attach"}, "attach must contain {session}"),
        ({"timezone": "UTC+7"}, "timezone must be a zone name such as Europe/Berlin, not 'UTC\\+7'"),
        ({"team": [{"name": "Alex"}]}, "the team is set on Cockpit's page now"),
    ],
)
def test_a_wrong_setup_stops_with_the_key_it_names(tmp_path: Path, changes: dict, error: str) -> None:
    with pytest.raises(config.SetupError, match=error):
        config.read_setup(write_setup(tmp_path, changes))


def test_no_setup_file_says_where_it_goes(tmp_path: Path) -> None:
    with pytest.raises(config.SetupError, match="no setup file at .*docs/setup.example.toml"):
        config.read_setup(tmp_path / "setup.toml")


def test_another_user_s_name_reaches_the_texts(world: World, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(config, "setup", lambda: dataclasses.replace(config.read_setup(TESTS_SETUP), name="Ana"))
    assert delivery.prefixed("user", "hello").startswith("Message from Ana via Cockpit:")
    state = world.client.get("/api/state").json()
    assert state["attach"] == "claude attach {session}"
    summary = cli.summary(state)
    assert "steps waiting on Ana" in summary and "Queue (Ana's order):" in summary
    agent = TestClient(world.client.app, base_url=ORIGIN)
    refused = agent.post("/api/items/add", json={"text": "x", "part": None, "pinned": False}, headers=world.agent())
    assert refused.status_code == 403 and "only Ana, signed in on the page" in refused.text
    with pytest.raises(SystemExit):
        cli.main(["item", "put", "--help"])
    assert "an item Ana added with Add" in capsys.readouterr().out


def test_retired_accounts_name_accounts_other_than_the_default(tmp_path: Path) -> None:
    assert config.read_setup(write_setup(tmp_path, {})).retired_accounts == ()
    path = write_setup(tmp_path, {})
    path.write_text(path.read_text() + 'retired_accounts = ["work"]\n')
    assert config.read_setup(path).retired_accounts == ("work",)


@pytest.mark.parametrize("value", ['["personal"]', '["nobody"]', '"work"', '[["work"]]'])
def test_a_wrong_retired_account_stops_with_the_key_it_names(tmp_path: Path, value: str) -> None:
    path = write_setup(tmp_path, {})
    path.write_text(path.read_text() + f"retired_accounts = {value}\n")
    with pytest.raises(config.SetupError, match="retired_accounts must list Claude accounts"):
        config.read_setup(path)
