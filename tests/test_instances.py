"""Separate Cockpits for one user, such as work and personal (SPEC SCOPE-6): each instance keeps its own files,
port, service, and Claude and Codex folders, and a session reaches the instance its folders belong to."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cockpit import accounts, config, parts
from tests.test_setup import write_setup


def test_cockpit_instance_names_the_instance(tmp_path: Path) -> None:
    assert config.find_instance({}, tmp_path) == ""
    assert config.find_instance({"COCKPIT_INSTANCE": "personal"}, tmp_path) == "personal"
    assert config.find_instance({"COCKPIT_INSTANCE": ""}, tmp_path) == ""
    for wrong in ("work", "Personal", "home-2", "2home", "a/b", "x" * 21):
        with pytest.raises(config.InstanceError):
            config.find_instance({"COCKPIT_INSTANCE": wrong}, tmp_path)


def test_a_session_reaches_the_instance_its_folders_belong_to(tmp_path: Path) -> None:
    """A hook or `cockpit` command run by a session that a daemon started without COCKPIT_INSTANCE still finds its
    instance, by the Claude or Codex folder the session runs on, once that instance is set up."""
    claude, second, codex = (str(tmp_path / name) for name in (".claude-home", ".claude-home-2", ".codex-home-2"))
    assert config.find_instance({"CLAUDE_CONFIG_DIR": claude}, tmp_path) == ""  # not set up
    (tmp_path / ".config" / "cockpit-home").mkdir(parents=True)
    assert config.find_instance({"CLAUDE_CONFIG_DIR": claude}, tmp_path) == "home"
    assert config.find_instance({"CLAUDE_CONFIG_DIR": second}, tmp_path) == "home"
    assert config.find_instance({"CODEX_HOME": codex}, tmp_path) == "home"
    # COCKPIT_INSTANCE wins over the folders.
    (tmp_path / ".config" / "cockpit-side").mkdir()
    assert config.find_instance({"COCKPIT_INSTANCE": "side", "CLAUDE_CONFIG_DIR": claude}, tmp_path) == "side"


def test_the_unnamed_instances_folders_name_no_instance(tmp_path: Path) -> None:
    (tmp_path / ".config" / "cockpit-work").mkdir(parents=True)
    for environ in ({"CLAUDE_CONFIG_DIR": str(tmp_path / ".claude")}, {"CLAUDE_CONFIG_DIR": str(tmp_path / ".claude-work")},
                    {"CODEX_HOME": str(tmp_path / ".codex")}, {"CODEX_HOME": str(tmp_path / ".codex-2")},
                    {"CLAUDE_CONFIG_DIR": str(tmp_path / "elsewhere" / ".claude-home")}):
        assert config.find_instance(environ, tmp_path) == "", environ


def test_a_named_instance_has_claude_folders_of_its_own(tmp_path: Path) -> None:
    unnamed = config.instance_accounts("", tmp_path)
    assert [(a.name, a.folder, a.default, a.claude_default) for a in unnamed] == [
        ("personal", tmp_path / ".claude", True, True), ("work", tmp_path / ".claude-work", False, False)]
    assert unnamed[0].state == tmp_path / ".claude.json"
    first, second = config.instance_accounts("home", tmp_path)
    assert (first.name, first.letter, first.folder, first.state) == (
        "home", "H", tmp_path / ".claude-home", tmp_path / ".claude-home" / ".claude.json")
    assert (second.name, second.folder) == ("home2", tmp_path / ".claude-home-2")
    assert first.default and not first.claude_default and not second.default and not second.claude_default
    # Every `claude` a named instance runs names its folder, even on its first account.
    assert accounts.claude_env(first) == ["env", f"CLAUDE_CONFIG_DIR={first.folder}"]
    assert accounts.claude_env(unnamed[0]) == ["env", "-u", "CLAUDE_CONFIG_DIR"]


def test_the_setup_names_the_port(tmp_path: Path) -> None:
    assert config.read_setup(write_setup(tmp_path, {}), parts.setup_tables()).port == config.DEFAULT_PORT
    local = {"auth_mode": "local", "origin": "http://127.0.0.1:8791", "port": 8791}
    assert config.read_setup(write_setup(tmp_path, local), parts.setup_tables()).port == 8791
    with pytest.raises(config.SetupError, match="local origin must be http://127.0.0.1:8791"):
        config.read_setup(write_setup(tmp_path, local | {"origin": "http://127.0.0.1:8790"}), parts.setup_tables())
    for wrong in (80, 70000, "8791", True, 8791.0):
        with pytest.raises(config.SetupError, match="port must be a number"):
            config.read_setup(write_setup(tmp_path, {"port": wrong}), parts.setup_tables())


def test_a_google_install_is_reached_on_its_own_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    setup = config.read_setup(write_setup(tmp_path, {"port": 8791}), parts.setup_tables())
    monkeypatch.setattr(config, "setup", lambda: setup)
    assert config.api_base() == "http://127.0.0.1:8791"


def test_local_setup_writes_the_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cockpit import local

    repo = tmp_path / "cockpit"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "SPEC.md").write_text("spec\n")
    folder = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", folder)
    monkeypatch.setattr(config, "SETUP_PATH", folder / "setup.toml")
    monkeypatch.setattr(config, "LOCAL_LOGIN_PATH", folder / "local-login-token")
    monkeypatch.setattr(local, "codex_command", lambda: "codex")
    monkeypatch.setattr(local.subprocess, "run", lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "main\n"))
    config.setup.cache_clear()
    try:
        local.initialize(repo, "codex", "Sam", "UTC", 8791)
        setup = config.setup()
        assert (setup.origin, setup.port) == ("http://127.0.0.1:8791", 8791)
    finally:
        config.setup.cache_clear()


# What a fresh process of each instance makes of its environment, as the server and the `cockpit` command see it.
PROBE = """
import json
from cockpit import auth, config, local
print(json.dumps({
    "instance": config.INSTANCE, "data": str(config.DATA_DIR), "config": str(config.CONFIG_DIR),
    "settings": str(config.CLAUDE_SETTINGS), "accounts": [str(a.folder) for a in config.ACCOUNTS],
    "codex": [str(home) for home in config.CODEX_HOMES], "label": local.LABEL, "unit": local.SYSTEMD_UNIT,
    "cookie": auth.LOCAL_SESSION_COOKIE, "environment": config.instance_environment(),
}))
"""


def probe(home: Path, **environ: str) -> dict:
    keep = {key: value for key, value in os.environ.items()
            if key not in {"COCKPIT_INSTANCE", "CLAUDE_CONFIG_DIR", "CODEX_HOME"}}
    result = subprocess.run([sys.executable, "-c", PROBE], env=keep | {"HOME": str(home)} | environ,
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_each_instance_keeps_its_own_files_and_services(tmp_path: Path) -> None:
    unnamed = probe(tmp_path)
    assert unnamed == {
        "instance": "", "data": str(tmp_path / ".local/share/cockpit"), "config": str(tmp_path / ".config/cockpit"),
        "settings": str(tmp_path / ".claude/settings.json"),
        "accounts": [str(tmp_path / ".claude"), str(tmp_path / ".claude-work")],
        "codex": [str(tmp_path / ".codex"), str(tmp_path / ".codex-2")],
        "label": "cockpit.server", "unit": "cockpit", "cookie": "cockpit_local_session", "environment": {},
    }
    named = probe(tmp_path, COCKPIT_INSTANCE="home")
    assert named == {
        "instance": "home", "data": str(tmp_path / ".local/share/cockpit-home"),
        "config": str(tmp_path / ".config/cockpit-home"), "settings": str(tmp_path / ".claude-home/settings.json"),
        "accounts": [str(tmp_path / ".claude-home"), str(tmp_path / ".claude-home-2")],
        "codex": [str(tmp_path / ".codex-home"), str(tmp_path / ".codex-home-2")],
        "label": "cockpit.server.home", "unit": "cockpit-home", "cookie": "cockpit_local_session-home",
        "environment": {"COCKPIT_INSTANCE": "home", "CODEX_HOME": str(tmp_path / ".codex-home")},
    }
    # A session on the instance's second Claude account, started without COCKPIT_INSTANCE, reaches the same files.
    (tmp_path / ".config" / "cockpit-home").mkdir(parents=True)
    assert probe(tmp_path, CLAUDE_CONFIG_DIR=str(tmp_path / ".claude-home-2")) == named


def test_the_page_checks_a_local_origin_on_the_port_it_names() -> None:
    """The app's own check (create_app) has no setup at hand: an origin on another port passes only when written
    exactly as the loopback address on that port."""
    config.validate_local_origin("http://127.0.0.1:8791")
    config.validate_local_origin("http://localhost:8791")
    for wrong in ("http://127.0.0.1:8791/", "http://0.0.0.0:8791", "http://localhost:8791@evil.test", "http://127.0.0.1",
                  "https://127.0.0.1:8791", "http://127.0.0.1:port", "http://127.0.0.1.evil.test:8791"):
        with pytest.raises(config.SetupError):
            config.validate_local_origin(wrong)
