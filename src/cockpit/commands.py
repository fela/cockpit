"""Commands the user runs from a step with one press (SPEC NEXT-18).

A step can carry a command that only the user may run, such as starting a local preview.
Run starts it on the host as the user's user, in the step's folder, through a
login shell, and hands the step to its session with a message that says so.
When the command ends, its session gets the exit code and the output.
"""

from __future__ import annotations

import codecs
import contextlib
import io
import logging
import os
import re
import select
import signal
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cockpit import db, patterns, rank, store
from cockpit.config import user_name

log = logging.getLogger(__name__)

# A login shell, so the command finds the same tools as in the user's terminal.
SHELL = ("bash", "-lc")
TIMEOUT_MINUTES = 30
# After the timeout the command gets SIGTERM, then SIGKILL this much later.
KILL_GRACE_SECONDS = 10
# The output kept and sent to the session: its end, where errors and results are.
OUTPUT_CHARS = 20_000
BACKTICKS = re.compile(r"`+")
RESTARTED = "Cockpit restarted while the command ran. Its outcome is unknown; nothing says how far it got."
STILL_RUNNING = "Cockpit restarted while the command ran. Its process group is still running; its outcome is unknown."


def _fenced(text: str) -> str:
    """A code block whose fence is longer than any run of backticks inside it."""
    longest = max((len(match.group()) for match in BACKTICKS.finditer(text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def start(
    conn: sqlite3.Connection, step_id: int, seen: str, seen_cwd: str, seen_session: str | None, new_session: dict[str, Any],
    checkouts_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The user pressed Run: record the run and hand the step to its session with a message saying it runs.

    `seen`, `seen_cwd` and `seen_session` are the command, its folder and the session the output goes to as the
    user's page showed them (None for no session); when any changed since, the press is refused, so the user runs only
    what they read. A step with no session starts one with the message (`new_session`: new_cwd and the start picks,
    store.create_message), and the run's session is the one that message starts (`execute`).
    Returns the run and the message; the caller then starts `execute`.
    """
    with db.transaction(conn):
        step = store.get_step(conn, step_id)
        store.check_open_for_hand_off(step)
        command = step["command"]
        if command is None:
            raise store.Conflict(f"step {step_id} has no command to run")
        if (command["run"], command["cwd"]) != (seen, seen_cwd):
            raise store.Conflict(f"step {step_id}'s command changed since your page showed it; read it again")
        session_id = store.target_session(conn, step["item_id"], step_id)
        store.check_seen_target(step_id, session_id, seen_session)
        if not Path(command["cwd"]).is_dir():
            raise store.Invalid(f"{command['cwd']} is not a folder on the host")
        text = (
            f"{user_name()} pressed Run on step {step_id}. Cockpit is running this command as {user_name()}, in"
            f" `{command['cwd']}`, and"
            f" sends you its exit code and output when it ends; it stops the command after {TIMEOUT_MINUTES} minutes."
            " Wait for that message before you act on the result.\n\n" + _fenced(command["run"])
        )
        target = {"session_id": session_id} if session_id is not None else {"session_id": None, **new_session}
        message = store.hand_off(conn, step_id, "cockpit", text, target, checkouts_dir, "ran")
        run_id = conn.execute(
            "INSERT INTO command_runs (step_id, session_id, command_text, cwd, started_at) VALUES (?, ?, ?, ?, ?)",
            (step_id, session_id, command["run"], command["cwd"], db.now_iso()),
        ).lastrowid
    return store.get_run(conn, run_id), message  # ty: ignore[invalid-argument-type]


def execute(
    db_path: Path, checkouts_dir: Path, run_id: int, start_message_id: int, deliver: Callable[[int], None]
) -> None:
    """Run the command, deliver the start message meanwhile, then record the result and deliver it.

    The command starts first, so it runs at once; the result message always
    reaches the session after the start message. A start message that fails to go
    leaves the command running: its result is still recorded and sent.
    """
    conn = db.connect(db_path)
    try:
        run = store.get_run(conn, run_id)
        exit_code: int | None = None
        note = ""
        output = ""
        began = time.monotonic()
        try:
            process = subprocess.Popen(
                [*SHELL, run["command_text"]],
                cwd=run["cwd"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            conn.execute("UPDATE command_runs SET process_id = ? WHERE id = ?", (process.pid, run_id))
        except Exception as exc:
            log.exception("command run %s did not start", run_id)
            process = None
            note = f"Cockpit could not run it: {exc}"
        _deliver_logged(deliver, start_message_id)
        # A run that started a new session sends its output there, once the start message names it.
        conn.execute(
            "UPDATE command_runs SET session_id = (SELECT session_id FROM messages WHERE id = ?)"
            " WHERE id = ? AND session_id IS NULL",
            (start_message_id, run_id),
        )
        cut = 0
        if process is not None:
            tail = _Tail(process)
            deadline = began + TIMEOUT_MINUTES * 60
            try:
                # Done once its output closed and it exited, as before it was read as it came.
                tail.thread.join(timeout=max(0.0, deadline - time.monotonic()))
                if tail.thread.is_alive():
                    raise subprocess.TimeoutExpired(run["command_text"], TIMEOUT_MINUTES * 60)
                exit_code = process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                failed = _stop_group(process)
                note = (
                    f"It ran over {TIMEOUT_MINUTES} minutes, and Cockpit could not stop it: {failed}." if failed
                    else f"Cockpit stopped it after {TIMEOUT_MINUTES} minutes."
                )
            except Exception as exc:
                # The run still ends with a result for its session, never left unfinished.
                log.exception("command run %s: waiting for it failed", run_id)
                exit_code = None
                failed = _stop_group(process)
                note = f"Cockpit lost track of it: {exc}." + (f" Stopping it failed: {failed}." if failed else " Cockpit stopped it.")
            output, cut, failure = tail.result()
            if failure:
                note = f"{note} Cockpit could not read all of its output: {failure.rstrip('.')}.".lstrip()
        message = finish(conn, checkouts_dir, run_id, exit_code, output, cut, note)
    finally:
        conn.close()
    if message is not None:
        deliver(message["id"])


class _Tail:
    """Reads a running command's output as it comes and keeps only its end, so a command that prints without end
    cannot fill Cockpit's memory over its 30 minutes. What it keeps gives the same end and count as cutting the
    whole output after its trailing blanks are dropped (`finish`)."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.kept = ""
        self.cut = 0
        # Blanks dropped from a run of them at the end: they count as cut once text follows them.
        self.blanks = 0
        self.failure = ""
        self.stopped = False
        self.stdout = process.stdout
        self.thread = threading.Thread(target=self._read, args=(self.stdout.fileno(),), daemon=True)  # ty: ignore[unresolved-attribute]  # Popen uses stdout=PIPE.
        self.thread.start()

    def _read(self, fd: int) -> None:
        decoder = io.IncrementalNewlineDecoder(codecs.getincrementaldecoder("utf-8")(errors="replace"), translate=True)
        try:
            # os.read returns what has arrived, so output printed before a pause is kept even if the pipe stays open;
            # poll lets `result` stop the read when a process outside the command's group keeps the pipe open.
            poller = select.poll()
            poller.register(fd, select.POLLIN)
            while not self.stopped:
                if not poller.poll(200):
                    continue
                data = os.read(fd, 65_536)
                if not data:
                    break
                self.add(decoder.decode(data))
            self.add(decoder.decode(b"", final=True))
        except Exception as exc:
            log.exception("reading a command run's output failed")
            self.failure = str(exc)

    def add(self, text: str) -> None:
        if self.blanks and text.strip():
            self.cut += self.blanks
            self.blanks = 0
        self.kept += text
        body = len(self.kept.rstrip())
        if body > 2 * OUTPUT_CHARS:
            self.cut += body - OUTPUT_CHARS
            self.kept = self.kept[body - OUTPUT_CHARS:]
            body = OUTPUT_CHARS
        blanks = len(self.kept) - body
        if blanks > 2 * OUTPUT_CHARS:
            self.blanks += blanks - OUTPUT_CHARS
            self.kept = self.kept[:body] + self.kept[len(self.kept) - OUTPUT_CHARS:]

    def result(self) -> tuple[str, int, str]:
        """The output's end, how many characters before it were dropped, and why reading stopped early, if it did.
        A process that left the command's process group may hold the output open; the read stops waiting then."""
        self.thread.join(timeout=KILL_GRACE_SECONDS)
        failure = self.failure
        if self.thread.is_alive():
            failure = "a process it started still holds its output open"
            self.stopped = True
            self.thread.join()
        self.stdout.close()  # ty: ignore[unresolved-attribute]
        return self.kept, self.cut, failure


def _signal_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    # The group is gone when the command ended on its own just now.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, sig)


def _group_alive(process: subprocess.Popen[bytes]) -> bool:
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    return True


def _stop_group(process: subprocess.Popen[bytes]) -> str:
    """Stop the command and every process in its group: SIGTERM, KILL_GRACE_SECONDS for all of them to end, then
    SIGKILL. Returns '' once stopped, else what went wrong."""
    try:
        _signal_group(process, signal.SIGTERM)
        end = time.monotonic() + KILL_GRACE_SECONDS
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=KILL_GRACE_SECONDS)
        # The command may have ended on SIGTERM while what it started still cleans up.
        while _group_alive(process) and time.monotonic() < end:
            time.sleep(0.05)
        _signal_group(process, signal.SIGKILL)
        process.wait()
    except Exception as exc:
        log.exception("could not stop command run process group %s", process.pid)
        return str(exc).rstrip(".")
    return ""


def _deliver_logged(deliver: Callable[[int], None], message_id: int) -> None:
    # Delivery records its own failures on the message; one that escapes is logged, and the run goes on.
    try:
        deliver(message_id)
    except Exception:
        log.exception("could not deliver the start message %s of a command run", message_id)


def finish(
    conn: sqlite3.Connection, checkouts_dir: Path, run_id: int, exit_code: int | None, output: str, cut: int,
    note: str,
) -> dict[str, Any] | None:
    """Record how a run ended and queue the message that tells its session; returns the message.

    A run whose new session never started, or was never named, has no session to tell: the result stays on the run,
    shown on the step's card, and the error log says so; returns None then.

    `output` is the end of what the command printed, after `cut` characters already dropped. `exit_code` is None
    when the command did not end by itself; `note` then says why, and the stored output ends with it.
    """
    output = output.rstrip()
    cut += max(0, len(output) - OUTPUT_CHARS)
    # Cut first, then mark sender lines: a cut after the marking could drop a mark and leave a sender line.
    output = store.defused(output[-OUTPUT_CHARS:] if output else "")
    if cut:
        output = f"[the first {cut:,} characters are cut]\n" + output
    with db.transaction(conn):
        run = store.get_run(conn, run_id)
        now = db.now_iso()
        seconds = round((rank.parse_time(now) - rank.parse_time(run["started_at"])).total_seconds())  # ty: ignore[unsupported-operator]
        ended = f"ended with exit code {exit_code} after {seconds} s" if exit_code is not None else f"did not finish: {note.rstrip('.')}"
        if exit_code is not None and note:
            ended += f"; {note.rstrip('.')}"
        shown = f"Its output (stdout and stderr together):\n\n{_fenced(output)}" if output else "It printed nothing."
        text = (
            f"The command {user_name()} ran from step {run['step_id']} {ended}.\n\n{_fenced(run['command_text'])}"
            f"\n\n{shown}"
        )
        session_id = _run_session(conn, run)
        message = None
        if session_id is None:
            log.error("command run %s on step %s ended with no session to tell: its new session never started", run_id, run["step_id"])
        else:
            message = store.create_message(
                conn, "cockpit", text, {"session_id": session_id, "step_id": run["step_id"]}, checkouts_dir
            )
        conn.execute(
            "UPDATE command_runs SET session_id = ?, finished_at = ?, exit_code = ?, output = ?, result_message_id = ?"
            " WHERE id = ?",
            (session_id, now, exit_code, "\n\n".join(part for part in (output, note) if part),
             message["id"] if message else None, run_id),
        )
        patterns.record_step(conn, "command_finished", "cockpit", store.get_step(conn, run["step_id"]))
    return message


def _run_session(conn: sqlite3.Connection, run: dict[str, Any]) -> str | None:
    """The session a run's output goes to: the one it was handed to, else, when the run started a new session whose
    name came only after the start message went, the step's session by now (store.target_session)."""
    if run["session_id"] is not None:
        return run["session_id"]
    step = store.get_step(conn, run["step_id"])
    return store.target_session(conn, step["item_id"], step["id"])


def interrupted(conn: sqlite3.Connection, checkouts_dir: Path) -> list[int]:
    """Check unfinished runs at startup and tell their sessions what is known about them."""
    messages = []
    unfinished = db.rows(conn.execute("SELECT * FROM command_runs WHERE finished_at IS NULL").fetchall())
    for run in unfinished:
        pid = run["process_id"]
        # Each command leads its own process group (start_new_session), whose id is the recorded process id.
        alive = False
        if pid is not None:
            try:
                os.killpg(pid, 0)
                alive = True
            except ProcessLookupError:
                pass
            except PermissionError:
                alive = True
        if not alive:
            message = finish(conn, checkouts_dir, run["id"], None, "", 0, RESTARTED)
            if message is not None:
                messages.append(message["id"])
        elif run["output"] != STILL_RUNNING:
            session_id = _run_session(conn, run)
            if session_id is None:
                log.error("command run %s on step %s still runs with no session to tell: its new session never started", run["id"], run["step_id"])
                continue
            with db.transaction(conn):
                message = store.create_message(
                    conn, "cockpit", f"The command from step {run['step_id']} (process group {pid}): {STILL_RUNNING}",
                    {"session_id": session_id, "step_id": run["step_id"]}, checkouts_dir,
                )
                conn.execute("UPDATE command_runs SET output = ? WHERE id = ?", (STILL_RUNNING, run["id"]))
                messages.append(message["id"])
    return messages
