"""Provider version warnings (SPEC SESSION-15), with read-only checks for enabled providers."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cockpit import updates

NOW = datetime(2026, 1, 16, 7, 0, tzinfo=UTC)
PUBLISHED = {
    "created": "2025-01-01T00:00:00.000Z",
    "modified": "2026-01-15T20:00:00.000Z",
    "0.10.0": "2026-01-01T10:00:00.000Z",
    "0.20.0": "2026-01-10T10:00:00.000Z",
    "0.21.0": "2026-01-12T10:00:00.000Z",
    "0.30.0-alpha.2": "2026-01-12T12:00:00.000Z",
    "0.30.0": "2026-01-15T10:00:00.000Z",
    # A fix to an older line, published after 0.21.0: publish time decides, not the number.
    "0.20.1": "2026-01-12T11:00:00.000Z",
}


def test_the_newest_release_out_for_three_days_wins_by_publish_time() -> None:
    assert updates.newest_ready(PUBLISHED, NOW) == "0.20.1"
    assert updates.newest_ready({"0.30.0": "2026-01-15T10:00:00.000Z"}, NOW) is None


class FakeViews:
    """`npm view @openai/codex time`, and nothing else."""

    def __init__(self, fails: bool = False) -> None:
        self.fails = fails

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        assert args[:3] == ["npm", "view", updates.PACKAGE]
        if self.fails:
            return subprocess.CompletedProcess(args, 1, "", "npm error network ETIMEDOUT")
        return subprocess.CompletedProcess(args, 0, json.dumps(PUBLISHED), "")


def claude_link(tmp_path: Path, version: str) -> Path:
    versions = tmp_path / "claude" / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    (versions / version).write_text("")
    link = tmp_path / "bin-claude"
    link.unlink(missing_ok=True)
    link.symlink_to(versions / version)
    return link


def channel(newest: str, seen: list[str] | None = None):
    def fetch(url: str) -> str:
        if seen is not None:
            seen.append(url)
        return newest
    return fetch


def test_claude_code_is_behind_when_its_channel_serves_a_newer_release(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    seen: list[str] = []
    installed, problem = updates.claude_behind(channel("2.1.20", seen), claude_link(tmp_path, "2.1.10"), settings)
    assert installed == "2.1.10"
    assert "is 2.1.10, but its latest channel serves 2.1.20" in problem and "run `claude update`" in problem
    assert seen == [f"{updates.CLAUDE_CHANNEL_URL}/latest"]
    assert updates.claude_behind(channel("2.1.10"), claude_link(tmp_path, "2.1.10"), settings) is None
    # Newer than the channel, as after a hand install from npm: not behind. Versions compare as numbers.
    assert updates.claude_behind(channel("2.1.9"), claude_link(tmp_path, "2.1.10"), settings) is None


def test_claude_code_follows_the_channel_its_settings_name(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"autoUpdatesChannel": "stable"}))
    seen: list[str] = []
    assert updates.claude_behind(channel("2.1.1", seen), claude_link(tmp_path, "2.1.10"), settings) is None
    assert seen == [f"{updates.CLAUDE_CHANNEL_URL}/stable"]


def test_a_claude_code_from_another_installer_is_left_alone(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    plain = tmp_path / "plain-claude"
    plain.write_text("")
    assert updates.claude_behind(channel("2.1.20"), plain, settings) is None
    # A link to Homebrew's executable is not the native installer's versions folder.
    (tmp_path / "Cellar" / "bin").mkdir(parents=True)
    (tmp_path / "Cellar" / "bin" / "claude").write_text("")
    brew = tmp_path / "brew-claude"
    brew.symlink_to(tmp_path / "Cellar" / "bin" / "claude")
    assert updates.claude_behind(channel("2.1.20"), brew, settings) is None
    # The native installer's link to a release that is gone is broken, not current.
    link = claude_link(tmp_path, "2.1.20")
    (tmp_path / "claude" / "versions" / "2.1.20").unlink()
    with pytest.raises(RuntimeError, match="missing"):
        updates.claude_behind(channel("2.1.20"), link, settings)


def test_codex_is_behind_when_a_release_out_for_three_days_is_newer(tmp_path: Path) -> None:
    package_json = tmp_path / "package.json"
    package_json.write_text(json.dumps({"version": "0.10.0"}))
    installed, problem = updates.codex_behind(FakeViews(), package_json, NOW)
    assert installed == "0.10.0" and "is 0.10.0, but 0.20.1" in problem
    assert "If `codex --version` still says 0.10.0, run `npm install --global @openai/codex@0.20.1`" in problem
    for current in ("0.20.1", "0.30.0"):
        package_json.write_text(json.dumps({"version": current}))
        assert updates.codex_behind(FakeViews(), package_json, NOW) is None
    assert updates.codex_behind(FakeViews(), tmp_path / "missing.json", NOW) is None
    # A version npm does not list cannot be placed, so no install is advised that might go backwards.
    package_json.write_text(json.dumps({"version": "0.50.0"}))
    with pytest.raises(RuntimeError, match="not the installed 0.50.0"):
        updates.codex_behind(FakeViews(), package_json, NOW)


def test_a_check_that_fails_is_reported_like_a_tool_that_fell_behind(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    package_json = tmp_path / "package.json"
    package_json.write_text(json.dumps({"version": "0.20.1"}))

    def channel_down(url: str) -> str:
        raise OSError("connection refused")

    problems = updates.behind(FakeViews(fails=True), channel_down, claude_link(tmp_path, "2.1.10"), settings,
                              package_json, NOW, ("claude", "codex"))
    assert [(tool, installed) for tool, installed, _ in problems] == [("claude", None), ("codex", None)]
    assert problems[0][2] == ("Cockpit could not check whether Claude Code is up to date for a day (connection refused)."
                              " Check by hand: `claude update` installs a newer release if its channel has one.")
    assert "ETIMEDOUT" in problems[1][2] and "Check by hand" in problems[1][2]
    assert updates.behind(FakeViews(), channel("2.1.10"), claude_link(tmp_path, "2.1.10"), settings, package_json,
                          NOW, ("claude", "codex")) == []


@pytest.mark.parametrize("providers", [("claude",), ("codex",), ("claude", "codex"), ()])
def test_only_enabled_providers_are_checked(tmp_path: Path, monkeypatch, providers):
    seen = []
    monkeypatch.setattr(updates, "claude_behind", lambda *args: seen.append("claude"))
    monkeypatch.setattr(updates, "codex_behind", lambda *args: seen.append("codex"))
    assert updates.behind(FakeViews(), channel("2.1.10"), tmp_path / "claude", tmp_path / "settings.json",
                          tmp_path / "package.json", NOW, providers) == []
    assert seen == list(providers)
