"""Signing a Codex account in from the page (SPEC UI-77).

Each Codex account has a folder of its own (config.CODEX_HOMES), which holds the account's sign-in in auth.json. Cockpit
reads the address in that file (codex.email) and nothing else in it; Codex itself writes the file. A folder whose Codex
runs on a provider of its own, as detected by codex.on_own_provider, signs in with that provider.

Pressing Start runs `codex login --device-auth` with CODEX_HOME set to the folder. Per Codex 0.166.0's source
(codex-rs/cli/src/login.rs, run_login_with_device_code calling clear_existing_auth_before_login), it first revokes the
stored refresh token and deletes auth.json, then prints OpenAI's link and a one-time code that lasts 15 minutes. So from
the press on the account is signed out until the user finishes, and also after a code left to run out. The page says so
before the press.

Codex prints the link and code on its standard output, and exits once the user has entered the code on OpenAI's page. The
code stays in this process's memory while it waits and reaches only the user's page: agents read the state without it,
and no log line holds it.

A sign-in can put the wrong account in a folder, since OpenAI's page signs in whichever account the browser holds. So the
database keeps the address each folder first held (`remember`), and a folder holding another address shows as a problem
until the user keeps the new one (`keep`) or signs in again.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import sqlite3
import subprocess
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cockpit import codex, db, store

log = logging.getLogger(__name__)

KV_KEY = "codex_folders"
LINK_TIMEOUT_SECONDS = 60
# Codex ends its own wait after 15 minutes; this ends one that hangs past that.
WAIT_SECONDS = 15 * 60
KILL_AFTER_SECONDS = WAIT_SECONDS + 60
ESCAPES = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
LINK = re.compile(r"https://\S+/codex/device\S*")
# A long unbroken run in Codex's error text may be a credential the server echoed.
LONG_RUN = re.compile(r"[A-Za-z0-9_\-.~+/=]{20,}")
CODE = re.compile(r"one-time code[^\n]*\n\s*(\S+)\s*\n")


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class _Login:
    """One running `codex login --device-auth`. `before` is the address the folder held when it started."""

    process: subprocess.Popen[str]
    timer: threading.Timer
    before: str
    text: str = ""
    url: str = ""
    code: str = ""
    ends_at: str = ""


class SignIn:
    """Per folder, the one sign-in waiting for the user, and how the last one ended. A second press while one waits
    gets the same link and code: that one is already signed out and nothing is lost by waiting on it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._starting: set[str] = set()
        self._logins: dict[str, _Login] = {}
        self._failures: dict[str, str] = {}
        self._finished: dict[str, dict[str, str]] = {}

    def start(self, folder: str, command: list[str], env: dict[str, str], before: str, now: datetime) -> None:
        """Start the folder's sign-in, which ends its present one at once, and wait for Codex's link and code.
        `command` runs `codex login --device-auth` for the folder, with `env` added to Cockpit's environment."""
        with self._lock:
            if folder in self._logins:
                return
            if folder in self._starting:
                raise store.Conflict(f"the sign-in of {folder} is starting: wait for its link")
            self._starting.add(folder)
        try:
            self._run(folder, command, env, before, now)
        finally:
            with self._lock:
                self._starting.discard(folder)

    def _run(self, folder: str, command: list[str], env: dict[str, str], before: str, now: datetime) -> None:
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                errors="replace", env=os.environ | env, start_new_session=True,
            )
        except OSError as exc:
            raise store.Invalid(f"Codex's sign-in of {folder} did not start: {exc}") from exc
        # Stops a login that hangs past Codex's own wait, also when only a child of it holds the output open.
        timer = threading.Timer(KILL_AFTER_SECONDS, self._stop, args=(process,))
        timer.daemon = True
        timer.start()
        login = _Login(process, timer, before, ends_at=_iso(now + timedelta(seconds=WAIT_SECONDS)))
        linked = threading.Event()
        threading.Thread(target=self._follow, args=(folder, login, linked), name=f"codex-sign-in-{folder}", daemon=True).start()
        linked.wait(LINK_TIMEOUT_SECONDS)
        if not (login.url and login.code):
            ended = process.poll()
            self._stop(process)
            raise store.Invalid(f"Codex gave no sign-in link for {folder}: {_said(login, ended)}")
        with self._lock:
            # It may have ended already, in which case _follow found no entry to clear: leave no entry behind.
            if process.poll() is None:
                self._logins[folder] = login
        log.info("started a sign-in of Codex's %s folder from the page", folder)

    def _follow(self, folder: str, login: _Login, linked: threading.Event) -> None:
        """Read Codex's output until it exits, and note how the sign-in ended."""
        assert login.process.stdout is not None
        for line in login.process.stdout:
            login.text += ESCAPES.sub("", line)
            if not (login.url and login.code):
                link, code = LINK.search(login.text), CODE.search(login.text)
                if link and code:
                    login.url, login.code = link[0], code[1]
                    linked.set()
        returncode = login.process.wait()
        login.timer.cancel()
        linked.set()
        if not (login.url and login.code):
            return  # `_run` reports it.
        with self._lock:
            self._logins.pop(folder, None)
            if returncode == 0:
                self._failures.pop(folder, None)
                self._finished[folder] = {"before": login.before, "at": _iso(datetime.now(UTC))}
                log.info("signed Codex's %s folder in from the page", folder)
            else:
                self._failures[folder] = _said(login, returncode)
                # The exit code only: Codex's words stay out of the log.
                log.warning("a sign-in of Codex's %s folder ended without signing in: exit %s", folder, returncode)

    def _stop(self, process: subprocess.Popen[str]) -> None:
        """End the login's whole process group, which may outlive Codex itself."""
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def state(self, folder: str) -> tuple[dict[str, str] | None, str, dict[str, str] | None]:
        """The folder's sign-in at one moment: the one that waits for the user (OpenAI's link, the code and when the
        code ends), the last failed one's words, and the last finished one."""
        with self._lock:
            login = self._logins.get(folder)
            waiting = {"url": login.url, "code": login.code, "ends_at": login.ends_at} if login is not None else None
            return waiting, self._failures.get(folder, ""), self._finished.get(folder)


def _said(login: _Login, returncode: int | None) -> str:
    """Codex's last error line for the user's page, without the code or any long run that may be a credential."""
    errors = [line.strip() for line in login.text.splitlines() if line.strip().lower().startswith("error")]
    if not errors:
        return "it was still running after a minute" if returncode is None else f"it exited with {returncode}"
    said = errors[-1].replace(login.code, "…") if login.code else errors[-1]
    return LONG_RUN.sub("…", said)[:300]


# remember and keep change one folder in a value shared by all, from several threads of this one process.
_held_lock = threading.Lock()


def held(conn: sqlite3.Connection) -> dict[str, str]:
    """The address each folder should hold, by folder name."""
    return db.kv_get(conn, KV_KEY) or {}


def remember(conn: sqlite3.Connection, folder: str, address: str) -> None:
    """Note the address a folder holds if it has none noted: a later other one shows as a problem (status)."""
    with _held_lock:
        known = held(conn)
        if address and folder not in known:
            db.kv_set(conn, KV_KEY, known | {folder: address})


def keep(conn: sqlite3.Connection, folder: str, address: str) -> None:
    """The user meant the folder to hold this address."""
    with _held_lock:
        db.kv_set(conn, KV_KEY, held(conn) | {folder: address})


def status(conn: sqlite3.Connection, index: codex.CodexIndex, sign_in: SignIn, show_code: bool) -> list[dict[str, Any]]:
    """For each folder: the address it holds and should hold, its problem ("signed_out", "other_account" or None), and
    the sign-in under way or finished. Only the user's page gets the code and Codex's error text. A folder with no
    address counts as signed out once it held an account or Codex ran on it (it has sessions), so an account that
    lost its sign-in before Cockpit first noted it still shows; any other such folder is unused here and left out.
    A folder whose Codex runs on a provider of its own, has no sign-in here and is left out too."""
    known = held(conn)
    accounts = [account for account in index.accounts if not codex.on_own_provider(account.home)]
    # Each sign-in is read before the addresses: one read as finished then comes with the address it ended on.
    states = {account.home.name: sign_in.state(account.home.name) for account in accounts}
    emails = {account.home.name: codex.email(account.home) for account in accounts}
    found = []
    for account in accounts:
        folder = account.home.name
        address = emails[folder]
        should = known.get(folder, "")
        others = [email for name, email in emails.items() if name != folder]
        waiting, failure, finished = states[folder]
        if waiting and not show_code:
            waiting = {"ends_at": waiting["ends_at"]}
        problem = None
        if not waiting:
            if (not address and (should or account.sessions_dir.is_dir())) or (address and account.refused()):
                problem = "signed_out"
            elif address and (address != (should or address) or address in others):
                problem = "other_account"
        if not (address or should or problem or waiting or failure):
            continue
        found.append({
            "folder": folder, "email": address, "held": should, "problem": problem, "doubled": address in others,
            "signing_in": waiting,
            "error": failure if show_code else "",
            "finished": finished and finished | {"after": address},
        })
    return found
