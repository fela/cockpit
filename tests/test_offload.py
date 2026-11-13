from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tempfile
import time
import types
from pathlib import Path

import pytest

from cockpit import config, db, offload


@pytest.fixture
def checkouts(tmp_path: Path) -> tuple[Path, Path]:
    """A main checkout with one worktree, as Cockpit's own checkouts are laid out."""
    main = tmp_path / "repo"
    (main / "tests").mkdir(parents=True)
    (main / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.test"]
    subprocess.run([*git, "init", "-q", str(main)], check=True)
    subprocess.run([*git, "-C", str(main), "add", "."], check=True)
    subprocess.run([*git, "-C", str(main), "commit", "-qm", "x"], check=True)
    worktree = main / ".claude" / "worktrees" / "wt"
    subprocess.run([*git, "-C", str(main), "worktree", "add", "-q", "-b", "wt", str(worktree)], check=True)
    return main, worktree


def _plan(command: str, cwd: Path, main: Path, env: dict[str, str] | None = None) -> offload.Plan | None:
    return offload.plan(command, cwd, env or {"PATH": "/usr/bin"}, config.Offload(host="box", repos=(main,)))


def test_a_test_run_in_a_main_checkout_goes_to_the_host(checkouts: tuple[Path, Path]) -> None:
    main, _ = checkouts
    work = _plan("uv run pytest tests/test_x.py -q 2>&1 | tail -20", main, main)
    assert work == offload.Plan(host="box", roots=(main,), ran_in=(main,), extras=(), mains=(main,))


def test_a_test_run_in_a_worktree_copies_its_main_checkout_too(checkouts: tuple[Path, Path]) -> None:
    main, worktree = checkouts
    work = _plan("timeout 600 .venv/bin/python -m pytest -x", worktree, main)
    assert work is not None
    assert (work.roots, work.ran_in, work.mains) == ((main, worktree), (worktree,), (main,))


def test_a_cd_into_a_worktree_follows_it(checkouts: tuple[Path, Path]) -> None:
    main, worktree = checkouts
    for command in (f"cd {worktree} && pytest", f"cd {worktree}\npytest -q", "cd .claude/worktrees/wt; pytest"):
        work = _plan(command, main, main)
        assert work is not None, command
        assert work.ran_in == (main, worktree), command


def test_output_under_tmp_and_the_job_folder_comes_back(checkouts: tuple[Path, Path]) -> None:
    main, _ = checkouts
    job = config.HOME / ".claude" / "jobs" / "j"
    env = {"PATH": "/usr/bin", "CLAUDE_JOB_DIR": str(job)}
    work = _plan("pytest --basetemp=/tmp/run-1 > $CLAUDE_JOB_DIR/tmp/out.txt 2>/dev/null", main, main, env)
    assert work is not None
    assert work.extras == (job / "tmp" / "out.txt", Path("/tmp/run-1"))


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "pytest -q # local",
        "pytest $(git diff --name-only)",
        "pytest `ls`",
        "(pytest)",
        "pytest <<EOF\nx\nEOF",
        "pytest; rm -rf build",
        "git commit -qam wip && pytest",
        "pytest && cockpit step done 1",
        "cd frontend && bun install",
        'bun -e \'require("fs").writeFileSync("/tmp/x", "")\'',
        "bun",
        "python .claude/worktrees/wt/tests/test_x.py",
        "pytest .claude/worktrees/wt/tests",
        "pytest -k $PATTERN",
        "pytest > ~/out.txt",
        "pytest --basetemp=/tmp",
        "cd /tmp && pytest",
        "cd .. && pytest",
        "uv run --directory other pytest",
        "pytest 'unclosed",
        "echo pytest",
        "pytest # tests\nrm -f notes.txt",
        "pytest > /tmp/results.log 2>&1 &",
        "env '-Srm -rf build' pytest",
        "env -C /tmp pytest",
        "pytest --junitxml=/var/tmp/results.xml",
        "pytest --junitxml=/dev/shm/results.xml",
        'python -c \'-__import__("os").system("rm -rf build")\' -m pytest',
    ],
)
def test_other_commands_run_here(checkouts: tuple[Path, Path], command: str) -> None:
    main, _ = checkouts
    assert _plan(command, main, main) is None


def test_python_options_before_m_are_read_past(checkouts: tuple[Path, Path]) -> None:
    main, _ = checkouts
    assert _plan("python3 -X dev -W error -B -m pytest -q", main, main) is not None


@pytest.mark.parametrize("command", ["bun test frontend", "uv run python scripts/render_app_icons.py | tail -5"])
def test_bun_and_python_scripts_go_too(checkouts: tuple[Path, Path], command: str) -> None:
    main, _ = checkouts
    assert _plan(command, main, main) is not None


def test_output_through_a_symlink_runs_here(checkouts: tuple[Path, Path]) -> None:
    main, _ = checkouts
    with tempfile.TemporaryDirectory(dir="/tmp") as folder:
        (Path(folder) / "link").symlink_to(main.parent)
        assert _plan(f"pytest --basetemp={folder}/link/run", main, main) is None


def test_a_checkout_the_setup_does_not_name_runs_here(checkouts: tuple[Path, Path], tmp_path: Path) -> None:
    main, _ = checkouts
    assert offload.plan("pytest", main, {}, config.Offload(host="box", repos=(tmp_path / "other",))) is None


def test_a_command_that_cannot_go_runs_here_and_says_why(
    checkouts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    main, _ = checkouts
    ran: list[str] = []
    monkeypatch.setattr(offload, "_here", lambda command, cwd: ran.append(command) or 0)
    monkeypatch.setattr(offload, "_there", lambda *args: pytest.fail("nothing should go to the host"))
    for setup_offload, command, reason in (
        (None, "pytest", "names no offload host"),
        (config.Offload(host="box", repos=(main,)), "git status", "This runs here: it is not a test run"),
    ):
        monkeypatch.setattr(offload.config, "setup", lambda found=setup_offload: types.SimpleNamespace(offload=found))
        assert offload.run(command, main, {"PATH": "/usr/bin"}) == 0
        assert reason in capsys.readouterr().err
    assert ran == ["pytest", "git status"]


def _script(command: str, cwd: Path) -> str:
    work = offload.Plan(host="box", roots=(cwd,), ran_in=(cwd,), extras=(), mains=(cwd,))
    return offload._remote_script(command, cwd, {"PATH": "/usr/bin:/bin"}, work, str(cwd / "marker" / "run-1"))


def test_the_host_script_removes_a_named_file_that_is_gone_here(tmp_path: Path) -> None:
    # One machine plays both: the earlier run's copy is made after the plan, as if it were still on the host.
    kept, gone = tmp_path / "out" / "kept.log", tmp_path / "out" / "gone.log"
    kept.parent.mkdir()
    kept.write_text("here\n")
    work = offload.Plan(host="box", roots=(tmp_path,), ran_in=(tmp_path,), extras=(kept, gone), mains=(tmp_path,))
    script = offload._remote_script(f"cat {gone} 2>/dev/null; cat {kept}", tmp_path, {}, work, str(tmp_path / "m" / "r"))
    gone.write_text("an earlier run's\n")
    with subprocess.Popen(["bash", "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as running:
        assert running.stdout is not None
        output = running.stdout.read()
        code = running.wait(timeout=10)
    assert (code, output, gone.exists()) == (0, "here\n", False)


def test_a_failed_offload_joins_the_sweeps_errors(migrated_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    shutil.copyfile(migrated_db, config.DB_PATH)
    offload._record("push", "rsync to box failed")
    assert "Could not record" not in capsys.readouterr().err
    conn = db.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT page, place, message FROM page_errors").fetchall()
    conn.close()
    assert [tuple(row) for row in rows] == [("offload", "push", "rsync to box failed")]


def test_the_host_script_passes_the_exit_code_and_output(tmp_path: Path) -> None:
    # stdin stays open, as ssh's does while the session waits for the command.
    script = _script("pwd; exit 3", tmp_path)
    with subprocess.Popen(["bash", "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as running:
        assert running.stdout is not None
        output = running.stdout.read()
        code = running.wait(timeout=10)
    assert (code, output) == (3, f"{tmp_path}\n")


def test_the_host_script_stops_the_command_when_its_connection_ends(tmp_path: Path) -> None:
    marker = tmp_path / "finished"
    script = _script(f"sleep 20; touch {marker}", tmp_path)
    with subprocess.Popen(["bash", "-c", script], stdin=subprocess.PIPE) as running:
        time.sleep(0.5)
        started = time.monotonic()
        assert running.stdin is not None
        running.stdin.close()
        running.wait(timeout=10)
    assert time.monotonic() - started < 8
    assert not marker.exists()
    assert subprocess.run(["pgrep", "-f", f"touch {marker}"], capture_output=True, check=False).returncode == 1


def test_the_host_script_stops_a_command_whose_connection_ends_at_once(tmp_path: Path) -> None:
    # The connection can end before setsid has given the command its own process group.
    marker = tmp_path / "finished"
    script = _script(f"sleep 20; touch {marker}", tmp_path)
    started = time.monotonic()
    subprocess.run(["bash", "-c", script], stdin=subprocess.DEVNULL, timeout=10, check=False)
    assert time.monotonic() - started < 4
    assert not marker.exists()


def test_the_host_script_ends_what_the_command_left_running(tmp_path: Path) -> None:
    marker = tmp_path / "finished"
    # The child ignores TERM, so only the KILL after the command ends stops it.
    script = _script(f"(trap '' TERM; sleep 20; touch {marker}) & wait", tmp_path)
    with subprocess.Popen(["bash", "-c", script], stdin=subprocess.PIPE) as running:
        time.sleep(0.5)
        assert running.stdin is not None
        running.stdin.close()
        running.wait(timeout=10)
    time.sleep(0.5)
    assert subprocess.run(["pgrep", "-f", f"touch {marker}"], capture_output=True, check=False).returncode == 1
    assert (tmp_path / "marker" / "run-1").exists()


def test_a_venv_linked_outside_the_copy_runs_here_and_says_so(
    checkouts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    main, worktree = checkouts
    other = main / ".claude" / "worktrees" / "other"
    (other / ".venv").mkdir(parents=True)
    (main / ".venv").mkdir()
    sent: list[str] = []
    monkeypatch.setattr(offload, "_here", lambda command, cwd: sent.append(f"here {cwd.name}") or 0)
    monkeypatch.setattr(offload, "_there", lambda command, cwd, env, work: sent.append(f"there {cwd.name}") or 0)
    monkeypatch.setattr(offload.config, "setup", lambda: types.SimpleNamespace(offload=config.Offload("box", (main,))))
    # The main checkout's .venv is copied with it; another worktree's is not.
    (worktree / ".venv").symlink_to(main / ".venv")
    assert offload.run(".venv/bin/python -m pytest", worktree, {"PATH": "/usr/bin"}) == 0
    (worktree / ".venv").unlink()
    (worktree / ".venv").symlink_to(other / ".venv")
    assert offload.run(".venv/bin/python -m pytest", worktree, {"PATH": "/usr/bin"}) == 0
    assert sent == ["there wt", "here wt"]
    assert f"{worktree / '.venv'} links to {other / '.venv'}" in capsys.readouterr().err
