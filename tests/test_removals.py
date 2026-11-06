from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from cockpit import config, db, removals, sessions
from cockpit.sessions import Move, Session
from tests.conftest import ORIGIN, SESSION_A, SESSION_B, World, make_session
from tests.test_api import run_sql
from tests.test_delivery import send

ITEM = "gh:example-org/app#10"
FLAGS = ["--effort", "high", "--name", "Review PR 10", "--model", "opus"]


def _finished(world: World, tmp_path: Path) -> Session:
    """A stopped background session on a closed item, with the job file `claude rm` deletes."""
    session = replace(make_session(SESSION_A, pid=None, state="done", cwd=str(tmp_path)), job_state="done")
    world.cache.sessions = [session]
    agent = TestClient(world.client.app, base_url=ORIGIN)
    agent.put("/api/items", json={"key": ITEM, "title": "PR 10"}, headers=world.agent())
    run_sql(world, "UPDATE items SET sessions = ?, stage = 'done' WHERE key = ?", (json.dumps([SESSION_A]), ITEM))
    job = config.ACCOUNTS[0].jobs / session.id
    job.mkdir(parents=True)
    (job / "state.json").write_text(json.dumps({"respawnFlags": FLAGS}))
    return session


def _due(world: World) -> list[str]:
    conn = db.connect(world.db_path)
    try:
        busy = removals.busy(config.ACCOUNTS, world.cache.sessions)
        return removals.due(conn, world.cache.sessions, removals.pinned(config.ACCOUNTS), busy)
    finally:
        conn.close()


def _rows(world: World) -> list[dict]:
    conn = db.connect(world.db_path)
    try:
        return db.rows(conn.execute("SELECT * FROM session_removals").fetchall())
    finally:
        conn.close()


def test_a_finished_session_whose_items_are_closed_is_removed(world: World, tmp_path: Path) -> None:
    _finished(world, tmp_path)
    assert _due(world) == [SESSION_A]
    world.deliverer.remove(SESSION_A, set)
    assert world.runner.calls == [(["claude", "rm", SESSION_A[:8]], Path.home())]
    [row] = _rows(world)
    assert json.loads(row["flags"]) == FLAGS and row["removed_at"] and row["refusal"] is None
    [listed] = removals.listed(world.db_path)
    assert listed.session_id == SESSION_A and listed.removed_at == row["removed_at"] and not listed.alive
    world.cache.sessions = [listed]
    (config.ACCOUNTS[0].jobs / SESSION_A[:8] / "state.json").unlink()  # as `claude rm` deletes it
    assert _due(world) == []


def test_a_session_on_an_open_item_stays(world: World, tmp_path: Path) -> None:
    _finished(world, tmp_path)
    run_sql(world, "UPDATE items SET stage = 'building' WHERE key = ?", (ITEM,))
    assert _due(world) == []
    world.deliverer.remove(SESSION_A, set)
    assert world.runner.calls == [] and _rows(world) == []


def test_an_archived_item_counts_as_closed(world: World, tmp_path: Path) -> None:
    _finished(world, tmp_path)
    run_sql(world, "UPDATE items SET stage = 'building', archived = 1 WHERE key = ?", (ITEM,))
    assert _due(world) == [SESSION_A]


def test_running_pinned_and_itemless_sessions_stay(world: World, tmp_path: Path) -> None:
    session = _finished(world, tmp_path)
    world.cache.sessions = [replace(session, pid=4242, status="idle")]
    assert _due(world) == []
    world.cache.sessions = [session]
    (config.ACCOUNTS[0].jobs / "pins.json").write_text(json.dumps([session.id]))
    assert _due(world) == []
    (config.ACCOUNTS[0].jobs / "pins.json").write_text("[]")
    run_sql(world, "UPDATE items SET sessions = '[]' WHERE key = ?", (ITEM,))
    assert _due(world) == []


def test_a_waiting_message_keeps_the_session(world: World, tmp_path: Path) -> None:
    _finished(world, tmp_path)
    world.runner.returncode = 1  # the message cannot go out, so it stays queued
    run_sql(
        world,
        "INSERT INTO messages (author, text, session_id, state, created_at) VALUES ('user', 'hi', ?, 'queued', ?)",
        (SESSION_A, "2026-01-27T00:00:00Z"),
    )
    assert _due(world) == []


def test_a_refused_removal_is_recorded_and_not_asked_again_until_the_session_changes(
    world: World, tmp_path: Path
) -> None:
    session = _finished(world, tmp_path)
    world.runner.returncode = 1
    world.runner.output = "The worktree has uncommitted changes. Deleting it would lose them."
    world.deliverer.remove(SESSION_A, set)
    [row] = _rows(world)
    assert row["removed_at"] is None and "uncommitted changes" in row["refusal"]
    # Listed in case `claude rm` deleted it after all; the copy `claude agents` still lists wins.
    [record] = removals.listed(world.db_path)
    assert sessions.with_removed([session], [record], {}) == [session]
    assert _due(world) == []
    world.cache.sessions = [replace(session, updated_at="2099-01-01T00:00:00.000Z")]
    assert _due(world) == [SESSION_A]


def test_a_message_resumes_a_removed_session_with_its_options(world: World, tmp_path: Path) -> None:
    _finished(world, tmp_path)
    world.deliverer.remove(SESSION_A, set)
    [removed] = removals.listed(world.db_path)
    world.cache.sessions = [removed]
    world.runner.calls.clear()

    def runner(account, args, cwd):
        world.cache.sessions = [replace(removed, pid=99, removed_at="")]  # listed again, running
        return world.runner(account, args, cwd)

    world.deliverer.run = runner
    world.runner.output = f"backgrounded · {SESSION_A[:8]}\n"
    sent = send(world, {"session_id": SESSION_A})
    assert sent["state"] == "sent" and sent["via"] == "resume"
    args, cwd = world.runner.calls[0]
    assert args[:4] == ["claude", "--bg", "--resume", SESSION_A] and args[4:-1] == FLAGS
    assert cwd == tmp_path


def test_a_removal_whose_answer_was_lost_still_lists(world: World, tmp_path: Path) -> None:
    session = _finished(world, tmp_path)
    conn = db.connect(world.db_path)
    try:
        with db.transaction(conn):
            removals.ask(conn, session, FLAGS, None)
    finally:
        conn.close()
    [record] = removals.listed(world.db_path)
    assert record.session_id == SESSION_A and record.removed_at
    assert sessions.with_removed([], [record], {}) == [record]


def test_the_removal_record_wins_over_an_older_copy_on_another_account_and_follows_a_move(
    world: World, tmp_path: Path
) -> None:
    session = _finished(world, tmp_path)
    work = config.ACCOUNTS[1]
    older = replace(session, account=work.name, updated_at="2026-01-01T00:00:00.000Z")
    (work.jobs / session.id).mkdir(parents=True)
    (work.jobs / session.id / "state.json").write_text(json.dumps({"respawnFlags": ["--name", "old"]}))
    world.deliverer.remove(SESSION_A, set)
    assert [call[0] for call in world.runner.calls] == [["claude", "rm", session.id]] * 2
    assert json.loads(_rows(world)[0]["flags"]) == FLAGS
    [record] = removals.listed(world.db_path)
    assert sessions.with_removed([older], [record], {}) == [record]
    moved = {SESSION_A: Move(work.name, "2099-01-01T00:00:00.000Z")}
    assert [found.account for found in sessions.with_removed([older], [record], moved)] == [work.name]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(cwd), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _worktree_session(world: World, tmp_path: Path) -> tuple[Path, Path, str, Session]:
    """A finished session that entered a worktree of its own, as the job file says, with a commit there."""
    repo, tree = tmp_path / "repo", tmp_path / "tree"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "start")
    _git(repo, "worktree", "add", "-q", "-b", "worktree-task", str(tree))
    _git(tree, "commit", "-q", "--allow-empty", "-m", "work")
    session = replace(_finished(world, tmp_path), cwd=str(tree))
    world.cache.sessions = [session]
    job = config.ACCOUNTS[0].jobs / session.id / "state.json"
    job.write_text(json.dumps({"respawnFlags": FLAGS, "worktreePath": str(tree)}))
    return repo, tree, _git(tree, "rev-parse", "HEAD"), session


def _claude_rm_deletes(repo: Path, tree: Path) -> None:
    """What `claude rm` does to a clean worktree it made: the folder and its branch go."""
    _git(repo, "worktree", "remove", str(tree))
    _git(repo, "branch", "-q", "-D", "worktree-task")


def _resumes(world: World, session: Session) -> None:
    def runner(account, args, cwd):
        world.cache.sessions = [replace(session, pid=99, removed_at="")]
        return world.runner(account, args, cwd)

    world.runner.calls.clear()
    world.deliverer.run = runner
    world.runner.output = f"backgrounded · {session.id}\n"
    assert send(world, {"session_id": session.session_id})["state"] == "sent"


def test_a_message_adds_the_worktree_claude_rm_deleted_again(world: World, tmp_path: Path) -> None:
    repo, tree, commit, _ = _worktree_session(world, tmp_path)
    world.deliverer.remove(SESSION_A, set)
    _claude_rm_deletes(repo, tree)
    [removed] = removals.listed(world.db_path)
    world.cache.sessions = [removed]
    _resumes(world, removed)
    assert world.runner.calls[0][1] == tree
    assert _git(tree, "rev-parse", "--abbrev-ref", "HEAD") == "worktree-task"
    assert _git(tree, "rev-parse", "HEAD") == commit


def test_a_removed_worktree_of_a_bare_repository_can_be_restored(world: World, tmp_path: Path) -> None:
    repo, tree, commit, _ = _worktree_session(world, tmp_path)
    bare = tmp_path / "app.git"
    _git(tmp_path, "clone", "--bare", str(repo), str(bare))
    _git(repo, "worktree", "remove", str(tree))
    _git(bare, "worktree", "add", str(tree), "worktree-task")
    world.deliverer.remove(SESSION_A, set)
    assert json.loads(_rows(world)[0]["worktree"])["repo"] == str(bare)
    _claude_rm_deletes(bare, tree)
    [removed] = removals.listed(world.db_path)
    world.cache.sessions = [removed]
    _resumes(world, removed)
    assert _git(tree, "rev-parse", "HEAD") == commit


def test_a_shared_worktree_stays_while_a_session_runs_in_it_and_comes_back_for_the_others(
    world: World, tmp_path: Path
) -> None:
    repo, tree, _, session = _worktree_session(world, tmp_path)
    (tree / "src").mkdir()
    (tree / "src" / "main.py").write_text("")
    _git(tree, "add", "src")
    _git(tree, "commit", "-q", "-m", "src")
    commit = _git(tree, "rev-parse", "HEAD")
    other = make_session(SESSION_B, pid=None, state="done", cwd=str(tree / "src"))
    running = replace(other, pid=99, status="busy")
    world.cache.sessions = [session, running]
    assert _due(world) == []
    world.cache.sessions = [session, other]
    assert _due(world) == [SESSION_A]
    world.deliverer.remove(SESSION_A, set)
    _claude_rm_deletes(repo, tree)
    # The other session was not removed, and its folder lies in the deleted worktree.
    _resumes(world, other)
    assert world.runner.calls[0][1] == tree / "src"
    assert _git(tree, "rev-parse", "HEAD") == commit


def test_a_copy_left_on_another_account_is_removed_on_a_later_poll(world: World, tmp_path: Path) -> None:
    session = _finished(world, tmp_path)
    world.deliverer.remove(SESSION_A, set)
    (config.ACCOUNTS[0].jobs / session.id).joinpath("state.json").unlink()
    work = config.ACCOUNTS[1]
    (work.jobs / session.id).mkdir(parents=True)
    (work.jobs / session.id / "state.json").write_text(json.dumps({"respawnFlags": ["--name", "old"]}))
    [record] = removals.listed(world.db_path)
    world.cache.sessions = [record]
    assert _due(world) == [SESSION_A]
    world.runner.calls.clear()
    world.deliverer.remove(SESSION_A, set)
    assert world.runner.calls == [(["claude", "rm", session.id], Path.home())]
    assert json.loads(_rows(world)[0]["flags"]) == FLAGS


def test_a_running_codex_session_or_a_fresh_restore_keeps_the_worktree_also_for_a_left_over_copy(
    world: World, tmp_path: Path
) -> None:
    _, tree, _, session = _worktree_session(world, tmp_path)
    codex = replace(make_session(SESSION_B, pid=7, cwd=str(tree)), provider="codex")
    assert removals.busy(config.ACCOUNTS, [codex]) == {tree}
    world.cache.sessions = [session, codex]
    assert _due(world) == []
    world.cache.sessions = [session]
    world.deliverer.remove(SESSION_A, set)
    [record] = removals.listed(world.db_path)
    world.cache.sessions = [record, codex]
    assert _due(world) == []
    conn = db.connect(world.db_path)
    try:
        removals.restore(conn, SESSION_A, str(tree))
    finally:
        conn.close()
    world.cache.sessions = [record]
    assert _due(world) == []
