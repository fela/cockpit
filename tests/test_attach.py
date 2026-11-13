"""`cockpit attach` against a tmux server of the test's own: TMUX_TMPDIR puts its socket in a temporary
folder, so the user's tmux is never touched."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from cockpit import attach, config

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")

COMMAND = "sleep 300"


@pytest.fixture(autouse=True)
def own_tmux(tmp_path, monkeypatch):
    socket_folder = tmp_path / "tmux"
    socket_folder.mkdir()
    monkeypatch.setenv("TMUX_TMPDIR", str(socket_folder))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("ENV", "")
    yield
    subprocess.run(["tmux", "kill-server"], capture_output=True, check=False)


def tmux(*args: str) -> str:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=True).stdout.strip()


def windows() -> list[list[str]]:
    rows = tmux("list-windows", "-a", "-F", "#{session_name}\t#{window_id}\t#{window_name}\t#{pane_current_path}")
    return [line.split("\t") for line in rows.splitlines()]


def wait_for(window: str, program: str) -> None:
    deadline = time.monotonic() + 10
    # User tmux configuration can select a different login shell on macOS.
    expected = {"sh", "bash", "zsh", "fish"} if program == "sh" else {program}
    while tmux("display-message", "-p", "-t", window, "#{pane_current_command}") not in expected:
        assert time.monotonic() < deadline, f"{window} never ran {program}"
        time.sleep(0.05)


def test_starts_a_tmux_session_only_when_no_server_runs(tmp_path):
    folder = tmp_path / "example"
    folder.mkdir()
    target, window = attach.open_window("aaaa111", "Cockpit work", folder, COMMAND)
    assert target == "main"
    assert windows() == [["main", window, "Cockpit work", str(folder)]]
    wait_for(window, "sleep")


def test_opens_a_window_in_the_tmux_session_attached_last_and_reuses_it(tmp_path):
    folder = tmp_path / "example"
    folder.mkdir()
    tmux("new-session", "-d", "-s", "work", "-c", str(tmp_path))
    target, window = attach.open_window("aaaa111", "Cockpit work", folder, COMMAND)
    assert target == "work"
    assert [row for row in windows() if row[1] == window] == [["work", window, "Cockpit work", str(folder)]]
    wait_for(window, "sleep")

    assert attach.open_window("aaaa111", "Cockpit work", folder, COMMAND) == ("work", window)
    assert len(windows()) == 2
    other = attach.open_window("bbbb222", "Exports", folder, COMMAND)[1]
    assert other != window
    assert len(windows()) == 3


def test_opens_a_new_window_once_the_session_was_left(tmp_path):
    tmux("new-session", "-d", "-s", "work", "-c", str(tmp_path))
    window = attach.open_window("aaaa111", "Cockpit work", tmp_path, COMMAND)[1]
    wait_for(window, "sleep")
    tmux("send-keys", "-t", window, "C-c")
    wait_for(window, "sh")

    opened = attach.open_window("aaaa111", "Cockpit work", tmp_path, COMMAND)[1]
    assert opened != window
    wait_for(opened, "sleep")
    assert tmux("display-message", "-p", "-t", window, f"#{{{attach.WINDOW_OPTION}}}") == ""
    assert len(windows()) == 3


def test_a_shell_reading_input_keeps_its_input_when_attaching_again(tmp_path):
    window = attach.open_window("aaaa111", "Cockpit work", tmp_path, COMMAND)[1]
    wait_for(window, "sleep")
    tmux("send-keys", "-t", window, "C-c")
    wait_for(window, "sh")
    captured = tmp_path / "input"
    ready = tmp_path / "ready"
    tmux("send-keys", "-t", window, f"touch {ready}; read line; printf '%s' \"$line\" > {captured}", "Enter")
    deadline = time.monotonic() + 10
    while not ready.exists():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    wait_for(window, "sh")
    opened = attach.open_window("aaaa111", "Cockpit work", tmp_path, COMMAND)[1]
    assert opened != window
    wait_for(opened, "sleep")
    tmux("send-keys", "-t", window, "kept input", "Enter")
    while not captured.exists():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert captured.read_text() == "kept input"


def test_last_attached_is_none_without_a_server():
    assert attach.last_attached() is None


def test_opens_a_new_window_when_the_old_one_runs_another_program(tmp_path):
    window = attach.open_window("aaaa111", "Cockpit work", tmp_path, COMMAND)[1]
    wait_for(window, "sleep")
    tmux("send-keys", "-t", window, "C-c")
    wait_for(window, "sh")
    captured = tmp_path / "input"
    tmux("send-keys", "-t", window, f"cat > {captured}", "Enter")
    wait_for(window, "cat")

    target, opened = attach.open_window("aaaa111", "Cockpit work", tmp_path, COMMAND)
    assert opened != window
    wait_for(opened, "sleep")
    assert captured.read_text() == ""
    assert attach.open_window("aaaa111", "Cockpit work", tmp_path, COMMAND) == (target, opened)
    assert len(windows()) == 2


def test_a_window_name_keeps_the_status_line_short(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(attach, "open_window", lambda *args: opened.append(args) or ("main", "@1"))
    monkeypatch.setattr(attach.os, "execvp", lambda program, args: opened.append(args))
    monkeypatch.setattr(attach, "_tmux", lambda *args: "")
    personal, work = config.ACCOUNTS
    attach.attach(personal, "aaaa111", "Cockpit: the session-management web page", Path("/home/sam/dev/app"))
    attach.attach(work, "aaaa111", "Cockpit work", Path("/home/sam/dev/app"))
    assert opened == [
        # Unset, so a CLAUDE_CONFIG_DIR the tmux shell kept from a work command does not apply.
        ("aaaa111", "Cockpit: the session-managemen", Path("/home/sam/dev/app"), "env -u CLAUDE_CONFIG_DIR claude attach aaaa111"),
        ["tmux", "attach-session", "-t", "main"],
        # A session on the work account attaches through the work folder's daemon.
        ("aaaa111", "Cockpit work", Path("/home/sam/dev/app"), f"CLAUDE_CONFIG_DIR={work.folder} claude attach aaaa111"),
        ["tmux", "attach-session", "-t", "main"],
    ]
