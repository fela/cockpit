"""Signing a Claude account in again from the page, and the turns a sign-out cost (SPEC UI-40).

When an account is signed out (accounts.signed_out), every call on it answers "Login expired · Please run /login":
Claude Code ends each session's turn there with that error (sessions.login_stop), and the session waits. The page
then says so at its top and offers the sign-in: Cockpit runs `claude auth login` for that account, under a
pseudo-terminal since it reads the code from a terminal, and hands the page Claude's link (SignIn). The user
approves on Claude's page and pastes the code it shows; Cockpit types it in.

Once the account's sign-in file changed after a session's stop and the account is not signed out, Cockpit sends
the session a message to continue the task it lost (sync), stopped sessions included. The sweeper gets a full sweep
in place of the wake-up it lost. A message that reached the session after its stop, or waits to, ends the wait, so
each stop is answered once, and a session that answers "Login expired" again waits for the next sign-in.
"""

from __future__ import annotations

import fcntl
import logging
import os
import pty
import re
import select
import sqlite3
import struct
import subprocess
import termios
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from cockpit import accounts, config, limits, store
from cockpit.sessions import Session, login_stop

log = logging.getLogger(__name__)

ACTOR = "cockpit"
CONTINUE = (
    "The {account} Claude account works again: it was signed in again at {at:%H:%M} UTC. Your last turn ended on"
    ' "Login expired". Continue the task you were on from where it stopped.'
)
LINK_TIMEOUT_SECONDS = 60
FINISH_TIMEOUT_SECONDS = 60
# How long an interrupted sign-in gets to end by itself before it is killed.
INTERRUPT_SECONDS = 5
# How long a sign-in waits for the code; after that it ends, and the page starts a new one.
WAIT_FOR_CODE_SECONDS = 15 * 60
# How often the reader looks whether its sign-in ended, so that ending one waits at most this long for it.
READ_POLL_SECONDS = 0.2
# The terminal's escape sequences: OSC ones (Claude prints its link as a terminal hyperlink) and CSI ones (colour,
# the cursor).
ESCAPES = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[A-Za-z]")
# The link once it is printed in full: the line break after it ends it, since a read can stop inside it.
LINK = re.compile(r"https://\S+/oauth/authorize\?\S+(?=\s)")
SIGNED_IN = "Login successful"
FAILED = re.compile(r"Login failed: ([^\r\n]*)")


@dataclass
class _Login:
    """One running `claude auth login`: what it printed so far, escapes removed, and whether it ended. `attempt`
    names it to the page, so that a code goes only to the sign-in whose link the user opened; `finishing` is set
    once it has a code, and nothing replaces or ends it until that code's answer is in."""

    process: subprocess.Popen[bytes]
    terminal: int
    interrupt: bool
    attempt: str = field(default_factory=lambda: uuid.uuid4().hex)
    text: str = ""
    ended: bool = False
    finishing: bool = False
    stopping: bool = False
    changed: threading.Condition = field(default_factory=threading.Condition)
    reader: threading.Thread | None = None

    def read(self) -> None:
        while not self.stopping:
            try:
                ready, _, _ = select.select([self.terminal], [], [], READ_POLL_SECONDS)
                chunk = os.read(self.terminal, 65536) if ready else None
            except OSError:
                chunk = b""  # the terminal closes when the process ends
            if chunk is None:
                continue
            if not chunk:
                break
            with self.changed:
                self.text = ESCAPES.sub("", self.text + chunk.decode(errors="replace"))
                self.changed.notify_all()
        with self.changed:
            self.ended = True
            self.changed.notify_all()

    def wait_for(self, done: Callable[[str], bool], seconds: float) -> str:
        """The text once `done` holds for it, the process ended or `seconds` passed."""
        with self.changed:
            self.changed.wait_for(lambda: self.ended or done(self.text), seconds)
            return self.text

    def end(self) -> None:
        """Stop the process and the reader, then close the terminal, so no reader is left on a reused descriptor. An
        `interrupt` sign-in first gets Ctrl-C on its terminal: one run over ssh would outlive a killed ssh there."""
        self.stopping = True
        if self.interrupt and self.process.poll() is None:
            os.write(self.terminal, b"\x03")
            try:
                self.process.wait(INTERRUPT_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        if self.reader is not None:
            self.reader.join()
        os.close(self.terminal)


class SignIn:
    """Per account, the one `claude auth login` waiting for the code Claude's page shows the user; starting another
    for that account ends it, unless it is finishing. `interrupt` sends a sign-in Ctrl-C before ending it (_Login.end).
    The lock guards the bookkeeping only: the waits on Claude happen outside it, so the page's state reads never wait
    on them."""

    def __init__(self, interrupt: bool) -> None:
        self._interrupt = interrupt
        self._lock = threading.Lock()
        self._logins: dict[str, _Login] = {}

    def start(self, name: str, command: list[str]) -> dict[str, str]:
        """Start a sign-in of the account `name` with `command`, which runs `claude auth login --claudeai` for it;
        returns Claude's link for the user to open and the attempt it belongs to."""
        with self._lock:
            self._refuse_finishing(name)
        login = self._run(name, command)
        text = login.wait_for(lambda text: LINK.search(text) is not None, LINK_TIMEOUT_SECONDS)
        link = LINK.search(text)
        if link is None:
            login.end()
            said = " ".join(text.split())[-300:]
            raise store.Invalid(f"Claude gave no sign-in link for the {name} account: {said or 'it printed nothing'}")
        with self._lock:
            previous = self._logins.get(name)
            if previous is not None and previous.finishing:
                replaced = login
            else:
                self._logins[name] = login
                replaced = previous
        if replaced is not None:
            replaced.end()
        if replaced is login:
            raise store.Conflict(f"a sign-in of the {name} account is finishing: wait for its answer")
        timer = threading.Timer(WAIT_FOR_CODE_SECONDS, self._expire, args=(name, login))
        timer.daemon = True
        timer.start()
        log.info("started a sign-in of the %s Claude account from the page", name)
        return {"url": link[0], "attempt": login.attempt}

    def finish(self, name: str, attempt: str, code: str) -> None:
        """Type the code Claude's page showed into the account's sign-in `attempt`; the sign-in then ends."""
        with self._lock:
            login = self._logins.get(name)
            if login is None or login.attempt != attempt:
                raise store.Invalid(f"no sign-in of the {name} account waits for this code: press Sign in again")
            if login.ended:
                del self._logins[name]
            elif login.finishing:
                raise store.Conflict(f"this sign-in of the {name} account already has a code: wait for its answer")
            else:
                login.finishing = True
        if not login.finishing:
            login.end()
            raise store.Invalid(f"the sign-in of the {name} account ended before the code came: press Sign in again")
        try:
            before = len(login.text)
            os.write(login.terminal, (code + "\r").encode())
            text = login.wait_for(lambda text: SIGNED_IN in text[before:] or FAILED.search(text[before:]) is not None,
                                  FINISH_TIMEOUT_SECONDS)[before:]
            if login.interrupt and SIGNED_IN in text:
                # It ends by itself once signed in: Ctrl-C (end) is for one that does not.
                try:
                    login.process.wait(INTERRUPT_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            with self._lock:
                if self._logins.get(name) is login:
                    del self._logins[name]
            login.end()
        if SIGNED_IN in text:
            log.info("signed the %s Claude account in again from the page", name)
            return
        failed = FAILED.search(text)
        if failed is None:
            raise store.Invalid(f"Claude did not finish the sign-in within {FINISH_TIMEOUT_SECONDS} s of getting the code")
        raise store.Invalid(f"Claude did not sign the {name} account in: {failed[1].replace(code, '…')}. Press Sign in to try again")

    def waiting(self) -> list[str]:
        """The accounts whose sign-in waits for a code."""
        with self._lock:
            return [name for name, login in self._logins.items() if not login.ended]

    def link(self, name: str) -> dict[str, str] | None:
        """Claude's link and the attempt of the account's sign-in that waits for a code."""
        with self._lock:
            login = self._logins.get(name)
        if login is None or login.ended:
            return None
        link = LINK.search(login.text)
        assert link is not None, "a sign-in is kept only once its link is in"
        return {"url": link[0], "attempt": login.attempt}

    def _run(self, name: str, command: list[str]) -> _Login:
        terminal, inner = pty.openpty()
        # A wide terminal keeps the link on one line.
        fcntl.ioctl(inner, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 4000, 0, 0))
        try:
            process = subprocess.Popen(command, stdin=inner, stdout=inner, stderr=inner, start_new_session=True)
        except OSError:
            os.close(terminal)
            raise
        finally:
            os.close(inner)
        login = _Login(process, terminal, self._interrupt)
        login.reader = threading.Thread(target=login.read, name=f"claude-sign-in-{name}", daemon=True)
        login.reader.start()
        return login

    def _refuse_finishing(self, name: str) -> None:
        current = self._logins.get(name)
        if current is not None and current.finishing:
            raise store.Conflict(f"a sign-in of the {name} account is finishing: wait for its answer")

    def _expire(self, name: str, login: _Login) -> None:
        with self._lock:
            if self._logins.get(name) is not login or login.finishing:
                return
            del self._logins[name]
        log.info("a sign-in of the %s Claude account waited %d min for a code and ended", name, WAIT_FOR_CODE_SECONDS // 60)
        login.end()


@dataclass(frozen=True)
class Lost:
    """A session whose turn a sign-out ended, now that its account was signed in again at `signed_in`."""

    session: Session
    signed_in: datetime


def lost(conn: sqlite3.Connection, sessions: list[Session], now: datetime) -> list[Lost]:
    """Each session whose last turn ended on "Login expired" on an account signed in again since, that no message
    reached after its stop. A session under way or archived is left alone."""
    found = []
    for session in sessions:
        if session.state == "working":
            continue
        stopped = login_stop(session.session_id)
        if stopped is None:
            continue
        account = config.account(session.account)
        changed = accounts.sign_in_changed(account)
        if changed is None or changed <= stopped or accounts.signed_out(account, now):
            continue
        stamp = stopped.strftime("%Y-%m-%dT%H:%M:%SZ")
        # Cockpit's own message since the stop answers it, whether or not it went out (a failed one is not tried
        # again every poll); anyone else's answers it once it went out after the stop, or while it waits to go.
        if conn.execute(
            "SELECT 1 FROM messages WHERE session_id = ? AND ((author = ? AND created_at > ?)"
            " OR state IN ('queued', 'sending') OR (state = 'sent' AND sent_at > ?))",
            (session.session_id, ACTOR, stamp, stamp),
        ).fetchone():
            continue
        if conn.execute(
            "SELECT 1 FROM session_archives WHERE session_id = ? AND cancelled_at IS NULL AND revived_at IS NULL"
            " AND failure = ''",
            (session.session_id,),
        ).fetchone():
            continue
        # Closed by an agent, such as the sweep, after the stop: it ended on purpose, as after a usage limit.
        if limits.ended_on_purpose(conn, session.session_id, stopped):
            continue
        found.append(Lost(session, changed))
    return found


def sync(conn: sqlite3.Connection, sessions: list[Session], sweeper: str | None, now: datetime, checkouts_dir: Path) -> tuple[list[int], Lost | None]:
    """Queue a message to continue to each session that lost its turn to a sign-out (lost), except the sweeper's
    session `sweeper`, which runs a full sweep instead (sweep_text). Returns the queued messages, for the caller to
    deliver, and the sweeper's lost turn, for the caller to launch the sweep."""
    message_ids = []
    swept = None
    for turn in lost(conn, sessions, now):
        if turn.session.session_id == sweeper:
            swept = turn
            continue
        text = CONTINUE.format(account=turn.session.account, at=turn.signed_in.astimezone(now.tzinfo))
        message_ids.append(store.create_message(conn, ACTOR, text, {"session_id": turn.session.session_id}, checkouts_dir)["id"])
        log.info(
            "the %s account was signed in again: session %s continues the turn it lost; queued message %s",
            turn.session.account, turn.session.session_id, message_ids[-1],
        )
    return message_ids, swept


def sweep_text(turn: Lost) -> str:
    skill = config.cockpit_skill_folder()
    return (
        f"Cockpit wake-up: the {turn.session.account} Claude account was signed out, so your last turn answered"
        f' "Login expired" and no sweep ran until it was signed in again at {turn.signed_in:%H:%M} UTC. Read'
        f" {skill}/SKILL.md and {skill}/references/sweep.md, since they change between sweeps, and run a full sweep"
        ' (section "Sweep").'
    )
