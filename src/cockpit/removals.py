"""Taking finished sessions off `claude agents` (SPEC SESSION-4).

Cockpit stops a session when the user archives it, closes its task, or leaves it idle, and a stopped session stays
in `claude agents` until it is removed."""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from cockpit import config, db, store
from cockpit.accounts import retired
from cockpit.config import Account
from cockpit.sessions import Session

log = logging.getLogger(__name__)

# Held while a removal may delete a worktree and while a resume adds one again, so the two never overlap.
WORKTREES = threading.Lock()
# A resume starts its process only after restore: until then no listing shows it running in its folder, so for this
# long the folder and a worktree added again count as worked in (keeps).
RESTORED_SECONDS = 600
_restored: dict[str, float] = {}  # folder → time.monotonic() a resume into it began


def _restored_lately(tree: Path) -> bool:
    now = time.monotonic()
    # A copy: restore adds to it from other threads.
    return any(now - at < RESTORED_SECONDS and Path(path).is_relative_to(tree) for path, at in _restored.copy().items())


def due(conn: sqlite3.Connection, listed: list[Session], pinned: set[str], busy: set[Path]) -> list[str]:
    """The sessions Cockpit removes now (keeps); Deliverer.remove judges each again before `claude rm`. `pinned`:
    the short ids of the sessions the user pinned in an account's `claude agents`; `busy`: the folders running
    sessions work in (busy)."""
    return [session.session_id for session in listed if keeps(conn, session, pinned, busy) is None]


def keeps(conn: sqlite3.Connection, session: Session, pinned: set[str], busy: set[Path]) -> str | None:
    """Why Cockpit keeps `session` in `claude agents`, or None when it is finished and every Cockpit item it belongs
    to is closed. A removed session that another account still lists is due again, for that copy."""
    if session.provider != "claude":
        return "it is not a Claude session"
    tree = entered(session)
    if tree is not None and (any(path.is_relative_to(tree) for path in busy) or _restored_lately(tree)):
        return "a running session works in its worktree"
    refused = conn.execute(
        "SELECT asked_at FROM session_removals WHERE session_id = ? AND refusal IS NOT NULL", (session.session_id,)
    ).fetchone()
    if session.removed_at:
        if not holders(config.ACCOUNTS, session):
            return "Cockpit removed it already"
        return "`claude rm` refused its copy on another account" if refused is not None else None
    if session.alive:
        return "its process runs"
    if not session.job_state:
        return "it is not a background session"
    if session.name in store.SWEEP_SESSION_NAMES:
        return "it is the sweeper"
    if session.id in pinned:
        return f"{store.user_name()} pinned it in `claude agents`"
    items = conn.execute(
        "SELECT i.archived, i.stage FROM items i, json_each(i.sessions) s WHERE s.value = ?", (session.session_id,)
    ).fetchall()
    if not items:
        return "it belongs to no Cockpit item"
    if any(not archived and stage != "done" for archived, stage in items):
        return "a Cockpit item it belongs to is open"
    if store.other_open_steps(conn, session.session_id, 0):
        return "an open step goes to it"
    if conn.execute(
        "SELECT 1 FROM messages WHERE session_id = ? AND state IN ('queued', 'sending')", (session.session_id,)
    ).fetchone():
        return "a message to it waits or is being sent"
    if refused is not None and refused[0] >= session.updated_at:
        return "`claude rm` refused it, and it has not changed since"
    return None


def pinned(accounts: tuple[Account, ...]) -> set[str]:
    """The short ids of the sessions the user pinned in an account's `claude agents`."""
    found: set[str] = set()
    for account in accounts:
        path = account.jobs / "pins.json"
        if path.exists():
            found |= set(json.loads(path.read_text()))
    return found


def holders(accounts: tuple[Account, ...], session: Session) -> list[Account]:
    """The accounts that list `session` in `claude agents`, its own first: each holds its job folder. A session the
    user moved to another account is listed on both once it ran on the new one. A session held by a retired account
    is tagged with the default one whichever it last ran on (SPEC UI-78), so there the newest job file comes first."""
    held = [account for account in accounts if (account.jobs / session.id / "state.json").exists()]
    if any(retired(account) for account in held):
        return sorted(held, key=lambda account: (account.jobs / session.id / "state.json").stat().st_mtime, reverse=True)
    return sorted(held, key=lambda account: account.name != session.account)


def _job(account: Account, short_id: str) -> dict[str, Any]:
    path = account.jobs / short_id / "state.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _entered(job: dict[str, Any]) -> Path | None:
    path = job.get("worktreePath")
    return Path(path) if isinstance(path, str) and path else None


def entered(session: Session) -> Path | None:
    """The worktree the session entered (its job file's `worktreePath`), which `claude rm` deletes when clean."""
    return next(
        (path for account in holders(config.ACCOUNTS, session) if (path := _entered(_job(account, session.id)))), None
    )


def busy(accounts: tuple[Account, ...], listed: list[Session]) -> set[Path]:
    """The folders running sessions work in, Claude's and Codex's: each one's directory and the worktree it entered."""
    found: set[Path] = set()
    for session in listed:
        if session.alive:
            if session.cwd:
                found.add(Path(session.cwd))
            found |= {path for account in accounts if (path := _entered(_job(account, session.id)))}
    return found


def job_flags(account: Account, short_id: str) -> list[str]:
    """The options the session's job file says it runs with (`respawnFlags`)."""
    path = account.jobs / short_id / "state.json"
    flags = json.loads(path.read_text()).get("respawnFlags") or []
    if not all(isinstance(flag, str) for flag in flags):
        raise ValueError(f"{path} has respawnFlags that are not all text: {flags!r}")
    return flags


def worktree(path: Path) -> dict[str, str]:
    """The worktree at `path`, which `claude rm` may delete: its folder, its repository's main checkout, its branch
    (empty when detached) and commit. Only its folder when it is gone already, as when another session's removal
    deleted it; restore then takes the rest from that session's record."""
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    if not path.is_dir():
        return {"path": str(path)}
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    repo = Path(common).parent if Path(common).name == ".git" else Path(common)
    return {"path": str(path), "repo": str(repo), "branch": "" if branch == "HEAD" else branch,
            "commit": git("rev-parse", "HEAD")}


def restore(conn: sqlite3.Connection, session_id: str, cwd: str) -> None:
    """Add the worktrees a session needs again when a removal deleted them: the one its own record names and the one
    its directory `cwd` lies in. Each goes back on its branch, else on a branch of that name made at its commit,
    else detached at its commit, from the newest record that knows them. `claude rm` deletes only a worktree whose
    commits were pushed, so the repository still has the commit. Raises when git cannot add one. Its folders then
    count as worked in for RESTORED_SECONDS, until the resume's process shows; so does `cwd` whether or not a
    worktree came back."""
    with WORKTREES:
        # Read under the lock: a removal that holds it may be recording the worktree it is about to delete.
        trees = [json.loads(row[0]) for row in conn.execute(
            "SELECT worktree FROM session_removals WHERE worktree IS NOT NULL ORDER BY asked_at DESC"
        )]
        own = conn.execute("SELECT worktree FROM session_removals WHERE session_id = ?", (session_id,)).fetchone()
        wanted = {json.loads(own[0])["path"]} if own is not None and own[0] is not None else set()
        wanted |= {tree["path"] for tree in trees if cwd and Path(cwd).is_relative_to(tree["path"])}
        if cwd:
            _restored[cwd] = time.monotonic()
        for path in sorted(wanted):
            if not Path(path).is_dir():
                tree = next((tree for tree in trees if tree["path"] == path and "commit" in tree), None)
                if tree is None:
                    raise RuntimeError(f"no record says which branch and commit its worktree {path} held")
                _add_worktree(tree)
                log.info("added the worktree %s again for session %s", path, session_id)
            _restored[path] = time.monotonic()


def _add_worktree(tree: dict[str, str]) -> None:
    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", tree["repo"], *args], capture_output=True, text=True, check=False)

    git("worktree", "prune")
    if not tree["branch"]:
        added = git("worktree", "add", "--detach", tree["path"], tree["commit"])
    elif git("rev-parse", "--verify", "--quiet", f"refs/heads/{tree['branch']}").returncode == 0:
        added = git("worktree", "add", tree["path"], tree["branch"])
    else:
        added = git("worktree", "add", "-b", tree["branch"], tree["path"], tree["commit"])
    if added.returncode != 0:
        raise RuntimeError(f"adding its worktree {tree['path']} again failed: {added.stderr.strip()}")


def ask(conn: sqlite3.Connection, session: Session, flags: list[str], tree: dict[str, str] | None) -> None:
    """Record the session, its options and its worktree before `claude rm` runs, so a removal whose answer is lost
    still keeps them. A session removed before, resumed and finished again replaces its record."""
    conn.execute(
        "INSERT OR REPLACE INTO session_removals (session_id, session, flags, worktree, asked_at)"
        " VALUES (?, ?, ?, ?, ?)",
        # The time takes the form of a job file's `updatedAt`, so keeps compares the two as text.
        (session.session_id, json.dumps(asdict(session)), json.dumps(flags), tree and json.dumps(tree), db.now_iso()),
    )


def removed(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("UPDATE session_removals SET removed_at = ? WHERE session_id = ?", (db.now_iso(), session_id))


def refused(conn: sqlite3.Connection, session_id: str, why: str) -> None:
    conn.execute("UPDATE session_removals SET refusal = ? WHERE session_id = ?", (why, session_id))


def listed(db_path: Path) -> list[Session]:
    """Each session Cockpit asked `claude rm` to remove, as it was listed just before, stopped. A refused or
    unanswered removal lists too, since `claude rm` may have deleted it; a copy `claude agents` still lists wins
    (sessions.with_removed)."""
    conn = db.connect(db_path)
    try:
        rows = conn.execute("SELECT session, COALESCE(removed_at, asked_at) FROM session_removals").fetchall()
    finally:
        conn.close()
    return [Session(**json.loads(session) | {"removed_at": removed_at}) for session, removed_at in rows]


def flags(conn: sqlite3.Connection, session_id: str) -> list[str]:
    """The options a removed session ran with, which its resume passes again."""
    row = conn.execute("SELECT flags FROM session_removals WHERE session_id = ?", (session_id,)).fetchone()
    if row is None:
        raise LookupError(f"Cockpit has no record of removing session {session_id}")
    return json.loads(row[0])
