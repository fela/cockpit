"""Separate Cockpits for one user, such as work and personal (SPEC SCOPE-7): COCKPIT_INSTANCE names the instance,
each instance keeps its own files, port, service, cookies, and Claude and Codex folders, and without a name Cockpit
keeps what a single Cockpit has always used."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cockpit import attach, config, parts, permissions, sidechat
from tests.test_resources import AGENT_SESSION, OTHER_SESSION, FakeProc, Proc, agent_tree, cpu_of, sampler_for, step
from tests.test_setup import write_setup


def test_cockpit_instance_alone_names_the_instance() -> None:
    assert config.find_instance({}) == ""
    assert config.find_instance({"COCKPIT_INSTANCE": ""}) == ""
    assert config.find_instance({"COCKPIT_INSTANCE": "personal"}) == "personal"
    # The Claude or Codex folder a session runs on names no instance.
    assert config.find_instance({"CLAUDE_CONFIG_DIR": "/home/u/.claude-home", "CODEX_HOME": "/home/u/.codex-home"}) == ""
    for wrong in ("work", "Personal", "home-2", "2home", "a/b", "x" * 21):
        with pytest.raises(config.InstanceError):
            config.find_instance({"COCKPIT_INSTANCE": wrong})


def test_a_named_instance_has_claude_folders_of_its_own(tmp_path: Path) -> None:
    unnamed = config.instance_accounts("", tmp_path)
    assert [(a.name, a.folder, a.default, a.claude_default) for a in unnamed] == [
        ("personal", tmp_path / ".claude", True, True), ("work", tmp_path / ".claude-work", False, False)]
    assert unnamed[0].state == tmp_path / ".claude.json"
    first, second = config.instance_accounts("home", tmp_path)
    assert (first.name, first.letter, first.folder, first.state) == (
        "home", "H", tmp_path / ".claude-home", tmp_path / ".claude-home" / ".claude.json")
    assert (second.name, second.folder) == ("home2", tmp_path / ".claude-home-2")
    assert first.default and not second.default
    # Every `claude` a named instance runs names its folder, even on its first account: the probe below.


def test_the_unnamed_instance_keeps_the_single_cockpits_port(tmp_path: Path) -> None:
    assert config.read_setup(write_setup(tmp_path, {}), parts.setup_tables()).port == config.DEFAULT_PORT
    assert config.read_setup(write_setup(tmp_path, {"port": 8790}), parts.setup_tables()).port == 8790
    for google_or_local in ({"port": 8791}, {"auth_mode": "local", "origin": "http://127.0.0.1:8791", "port": 8791}):
        with pytest.raises(config.SetupError, match="port must be 8790 without COCKPIT_INSTANCE"):
            config.read_setup(write_setup(tmp_path, google_or_local), parts.setup_tables())


def test_the_setup_names_the_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "INSTANCE", "home")
    assert config.read_setup(write_setup(tmp_path, {}), parts.setup_tables()).port == config.DEFAULT_PORT
    local = {"auth_mode": "local", "origin": "http://127.0.0.1:8791", "port": 8791}
    assert config.read_setup(write_setup(tmp_path, local), parts.setup_tables()).port == 8791
    with pytest.raises(config.SetupError, match="local origin must be http://127.0.0.1:8791"):
        config.read_setup(write_setup(tmp_path, local | {"origin": "http://127.0.0.1:8790"}), parts.setup_tables())
    for wrong in (80, 70000, "8791", True, 8791.0):
        with pytest.raises(config.SetupError, match="port must be a number"):
            config.read_setup(write_setup(tmp_path, {"port": wrong}), parts.setup_tables())


def test_a_google_install_is_reached_on_its_own_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "INSTANCE", "home")
    setup = config.read_setup(write_setup(tmp_path, {"port": 8791}), parts.setup_tables())
    monkeypatch.setattr(config, "setup", lambda: setup)
    assert config.api_base() == "http://127.0.0.1:8791"


def test_local_setup_writes_the_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cockpit import local

    repo = tmp_path / "cockpit"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "SPEC.md").write_text("spec\n")
    folder = tmp_path / "config"
    monkeypatch.setattr(config, "INSTANCE", "home")
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


# What a fresh process of an instance makes of its environment, as the server and the `cockpit` command see it.
PROBE = """
import json
from cockpit import accounts, auth, config, local, permissions, resources
print(json.dumps({
    "instance": config.INSTANCE, "data": str(config.DATA_DIR), "config": str(config.CONFIG_DIR),
    "settings": str(config.CLAUDE_SETTINGS), "claude": str(config.CLAUDE_HOME),
    "accounts": [str(a.folder) for a in config.ACCOUNTS], "codex": [str(home) for home in config.CODEX_HOMES],
    "first_claude": accounts.claude_env(config.ACCOUNTS[0]), "label": local.LABEL, "unit": local.SYSTEMD_UNIT,
    "restart": local.SYSTEMD_RESTART_SCRIPT, "attach": local.ATTACH, "port": config.setup().port,
    "cookies": [auth.SIGN_IN_COOKIE, auth.SESSION_COOKIE, auth.LOCAL_SESSION_COOKIE, auth.VISITOR_COOKIE,
                auth.VISITOR_SIGN_IN_COOKIE],
    "environment": config.instance_environment(), "lock": str(config.deploy_lock(config.DATA_DIR)),
    "transcripts": resources.FD_SESSION[1].pattern.split("/")[0],
    "secrets": [place for place in permissions.SECRET_PLACES if place.startswith((".config/cockpit", ".codex"))],
}))
"""


def probe(home: Path, environ: dict[str, str]) -> dict:
    """PROBE run with HOME at `home`, the shell's COCKPIT_INSTANCE and CLAUDE_CONFIG_DIR dropped, and CODEX_HOME at a
    folder of no instance, then `environ`."""
    keep = {key: value for key, value in os.environ.items() if key not in {"COCKPIT_INSTANCE", "CLAUDE_CONFIG_DIR"}}
    result = subprocess.run([sys.executable, "-c", PROBE],
                            env=keep | {"HOME": str(home), "CODEX_HOME": str(home / "elsewhere")} | environ,
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_without_a_name_cockpit_uses_what_a_single_cockpit_always_used(tmp_path: Path) -> None:
    """Every folder, port, service, cookie and variable, written out as main had them before instances."""
    (tmp_path / ".config" / "cockpit").mkdir(parents=True)
    write_setup(tmp_path / ".config" / "cockpit", {})
    assert probe(tmp_path, {}) == {
        "instance": "", "data": str(tmp_path / ".local/share/cockpit"), "config": str(tmp_path / ".config/cockpit"),
        "settings": str(tmp_path / ".claude/settings.json"), "claude": str(tmp_path / ".claude"),
        "accounts": [str(tmp_path / ".claude"), str(tmp_path / ".claude-work")],
        "codex": [str(tmp_path / ".codex"), str(tmp_path / ".codex-2")],
        "first_claude": ["env", "-u", "CLAUDE_CONFIG_DIR"], "label": "cockpit.server", "unit": "cockpit",
        "restart": 'sleep "$1"; exec systemctl --user restart cockpit', "attach": "cockpit attach {session}",
        "port": 8790,
        "cookies": ["__Host-cockpit_sign_in", "__Host-cockpit_session", "cockpit_local_session", "__Host-p_visitor",
                    "__Host-p_sign_in"],
        "environment": {}, "lock": str(tmp_path / ".local/share/cockpit/deploy.lock"), "transcripts": r"\.claude",
        "secrets": [".config/cockpit/token", ".config/cockpit/local-login-token", ".config/cockpit/cookie-key",
                    ".config/cockpit/google-oauth-client.json", ".config/cockpit/secrets", ".config/cockpit/signins",
                    ".codex/auth.json", ".codex-2/auth.json"],
    }


def test_a_named_instance_keeps_its_own_files_services_and_codex_folder(tmp_path: Path) -> None:
    (tmp_path / ".config" / "cockpit-home").mkdir(parents=True)
    write_setup(tmp_path / ".config" / "cockpit-home", {"port": 8791})
    assert probe(tmp_path, {"COCKPIT_INSTANCE": "home"}) == {
        "instance": "home", "data": str(tmp_path / ".local/share/cockpit-home"),
        "config": str(tmp_path / ".config/cockpit-home"), "settings": str(tmp_path / ".claude-home/settings.json"),
        "claude": str(tmp_path / ".claude-home"),
        "accounts": [str(tmp_path / ".claude-home"), str(tmp_path / ".claude-home-2")],
        "codex": [str(tmp_path / ".codex-home"), str(tmp_path / ".codex-home-2")],
        "first_claude": ["env", f"CLAUDE_CONFIG_DIR={tmp_path / '.claude-home'}"], "label": "cockpit.server.home",
        "unit": "cockpit-home", "restart": 'sleep "$1"; exec systemctl --user restart cockpit-home',
        "attach": "COCKPIT_INSTANCE=home cockpit attach {session}", "port": 8791,
        "cookies": ["__Host-cockpit_sign_in-home", "__Host-cockpit_session-home", "cockpit_local_session-home",
                    "__Host-p_visitor-home", "__Host-p_sign_in-home"],
        # The server's environment names the instance and its own Codex folder, in place of the shell's CODEX_HOME.
        "environment": {"COCKPIT_INSTANCE": "home", "CODEX_HOME": str(tmp_path / ".codex-home")},
        # Every instance deploys under one lock, since instances may share one virtualenv.
        "lock": str(tmp_path / ".local/share/cockpit/deploy.lock"), "transcripts": r"\.claude\-home",
        "secrets": [".config/cockpit-home/token", ".config/cockpit-home/local-login-token",
                    ".config/cockpit-home/cookie-key", ".config/cockpit-home/google-oauth-client.json",
                    ".config/cockpit-home/secrets", ".config/cockpit-home/signins", ".codex-home/auth.json",
                    ".codex-home-2/auth.json"],
    }


def test_every_instances_credentials_stay_closed(tmp_path: Path) -> None:
    """The credential files of another instance on the machine are as closed as this instance's own."""
    others = [".config/cockpit-home/token", ".config/cockpit-home/cookie-key", ".claude-home/.credentials.json",
              ".claude-home-2/.credentials.json", ".codex-home/auth.json", ".codex-home-2/auth.json"]
    for name in [*others, ".claude-home/settings.json"]:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x")
    assert set(others) <= set(permissions.secret_places(tmp_path))
    assert [permissions.showable(tmp_path / name, tmp_path) for name in others] == [None] * len(others)
    assert permissions.showable(tmp_path / ".claude-home/settings.json", tmp_path) is not None
    denied = sidechat.denied_reads(tmp_path)
    assert {f"/{tmp_path / name}" for name in others} <= set(denied)
    assert "~/.codex*/auth.json" in denied


def test_without_other_instances_the_credential_places_are_mains(tmp_path: Path) -> None:
    assert permissions.secret_places(tmp_path) == permissions.SECRET_PLACES


def attached_environment(command: str, tmp_path: Path) -> list[str]:
    """CLAUDE_CONFIG_DIR and COCKPIT_INSTANCE as `claude` sees them when a tmux shell left from another Cockpit's
    command runs the attach `command`."""
    bin_folder = tmp_path / "bin"
    bin_folder.mkdir(exist_ok=True)
    (bin_folder / "claude").write_text('#!/bin/sh\necho "${CLAUDE_CONFIG_DIR-unset} ${COCKPIT_INSTANCE-unset}"\n')
    (bin_folder / "claude").chmod(0o755)
    stale = {"PATH": f"{bin_folder}:{os.environ['PATH']}", "CLAUDE_CONFIG_DIR": "/stale", "COCKPIT_INSTANCE": "other"}
    return subprocess.run(["sh", "-c", command], env=stale, capture_output=True, text=True, check=True).stdout.split()


def test_an_attached_session_asks_its_own_instance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The session's permission hooks post to the Cockpit COCKPIT_INSTANCE names, so the attach command sets it for
    # a named instance and unsets it for the unnamed one, whatever the tmux shell kept.
    personal, work = config.instance_accounts("", tmp_path)
    assert attached_environment(attach.attach_command(personal, "aaaa111"), tmp_path) == ["unset", "unset"]
    assert attached_environment(attach.attach_command(work, "aaaa111"), tmp_path) == [str(work.folder), "unset"]
    monkeypatch.setattr(config, "INSTANCE", "home")
    for account in config.instance_accounts("home", tmp_path):
        command = attach.attach_command(account, "aaaa111")
        assert attached_environment(command, tmp_path) == [str(account.folder), "home"]
        assert attach._program(command) == "claude"


def test_the_sampler_leaves_another_instances_programs_unattributed(tmp_path: Path) -> None:
    """A program whose environment names another instance counts on the host but on none of this instance's
    sessions, even with a session id in its environment."""
    proc = FakeProc(tmp_path / "proc")
    agent_tree(proc)
    own = proc.add(Proc(40, "python3", 1, "python3 job.py", start=400, env=f"CLAUDE_CODE_SESSION_ID={OTHER_SESSION}\0"))
    other = proc.add(Proc(41, "python3", 1, "python3 job.py", start=410,
                          env=f"COCKPIT_INSTANCE=home\0CLAUDE_CODE_SESSION_ID={AGENT_SESSION}\0"))
    sampler = sampler_for(proc)
    step(sampler, proc, 0)
    own.ticks += 10
    other.ticks += 10
    step(sampler, proc, 5)
    assert cpu_of(sampler) == {(OTHER_SESSION, "Other Python"): 0.1, ("", "Other Python"): 0.1}


def test_a_local_origin_is_the_loopback_address_on_the_setups_port() -> None:
    config.validate_local_origin("http://127.0.0.1:8791", 8791)
    config.validate_local_origin("http://localhost:8791", 8791)
    for wrong in ("http://127.0.0.1:8790", "http://127.0.0.1:8791/", "http://0.0.0.0:8791",
                  "http://localhost:8791@evil.test", "http://127.0.0.1", "https://127.0.0.1:8791"):
        with pytest.raises(config.SetupError):
            config.validate_local_origin(wrong, 8791)
