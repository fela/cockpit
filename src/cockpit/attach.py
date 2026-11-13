"""Show a session in the user's tmux on this machine (SPEC SESSION-6).

The page's iTerm2 link runs the setup file's `attach` command, which runs `cockpit attach SESSION` here,
over SSH when Cockpit runs on another machine. It shows the session in a window of the tmux session the
user attached to last: the window it opened for that session before, or a new window that starts in the
checkouts folder and types `claude attach SESSION` into its shell, with CLAUDE_CONFIG_DIR set before it for a
session on an account other than the default one (SPEC SESSION-17). Each window it opens carries the
session's id in a window option, so the next press goes back to that window instead of adding one. It
starts a tmux session only when no tmux server runs.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from cockpit.config import Account

# The window option that names the session a window was opened for.
WINDOW_OPTION = "@cockpit_session"
# The tmux session started when no tmux server runs.
NEW_TMUX_SESSION = "main"
# A window's name is the session's name, cut to this many characters so the status line keeps room.
NAME_CHARS = 30


class AttachError(Exception):
    """tmux answered with an error."""


def _tmux(*args: str) -> str:
    result = subprocess.run(["tmux", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AttachError(f"tmux {args[0]} failed: {result.stderr.strip() or f'exit code {result.returncode}'}")
    return result.stdout.strip()


def last_attached() -> str | None:
    """The tmux session the user attached to last; None when no tmux server runs."""
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_last_attached}\t#{session_name}"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        if "no server running" in result.stderr or "error connecting" in result.stderr:
            return None
        raise AttachError(f"tmux list-sessions failed: {result.stderr.strip()}")
    rows = [line.split("\t", 1) for line in result.stdout.splitlines()]
    return max(rows, key=lambda row: int(row[0] or 0))[1]


def open_window(session: str, name: str, folder: Path, command: str) -> tuple[str, str]:
    """(tmux session, window id) of the window that shows `session`. A window opened for it before is
    reused while its pane runs the attach program. Any other program, including a shell that may be reading
    input, keeps its window and a new window gets `command`."""
    program = _program(command)
    target = last_attached()
    if target is None:
        window = _tmux("new-session", "-d", "-P", "-F", "#{window_id}", "-s", NEW_TMUX_SESSION, "-n", name,
                       "-c", str(folder))
        target = NEW_TMUX_SESSION
    else:
        rows = _tmux("list-windows", "-a", "-F",
                     f"#{{session_name}}\t#{{window_id}}\t#{{{WINDOW_OPTION}}}\t#{{pane_current_command}}")
        found = next((row for row in (line.split("\t") for line in rows.splitlines()) if row[2] == session), None)
        if found is not None:
            target, window, running = found[0], found[1], found[3]
            if running == program:
                return target, window
            _tmux("set-option", "-w", "-u", "-t", window, WINDOW_OPTION)
            found = None
        if found is None:
            window = _tmux("new-window", "-d", "-P", "-F", "#{window_id}", "-t", f"{target}:", "-n", name,
                           "-c", str(folder))
    _tmux("set-option", "-w", "-t", window, WINDOW_OPTION, session)
    _tmux("send-keys", "-t", window, command, "Enter")
    return target, window


def _program(command: str) -> str:
    """The program `command` runs, past `env -u NAME` and NAME=value settings: what the pane shows running it."""
    words = shlex.split(command)
    if words[0] == "env":
        words = words[1:]
        while words[0] == "-u":
            words = words[2:]
    return Path(next(word for word in words if "=" not in word)).name


def attach_command(account: Account, session: str) -> str:
    """What the window's shell runs to attach to `session` on `account`. The default account unsets
    CLAUDE_CONFIG_DIR, which the tmux shell may have from a command run there for the other account."""
    command = f"claude attach {shlex.quote(session)}"
    if account.default:
        return f"env -u CLAUDE_CONFIG_DIR {command}"
    return f"CLAUDE_CONFIG_DIR={shlex.quote(str(account.folder))} {command}"


def attach(account: Account, session: str, name: str, folder: Path) -> None:
    """Show `session` in its tmux window and attach this terminal to that tmux session; does not return.
    Inside tmux already, it switches this client to the window."""
    target, window = open_window(session, name[:NAME_CHARS], folder, attach_command(account, session))
    if os.environ.get("TMUX"):
        os.execvp("tmux", ["tmux", "switch-client", "-t", window])
    _tmux("select-window", "-t", window)
    os.execvp("tmux", ["tmux", "attach-session", "-t", target])
