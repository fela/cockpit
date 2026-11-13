"""The browser harness's git stays in its own repos (tests/browser/serve_slow.py)."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "browser"))
import serve_slow


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=serve_slow.own_git_env(), check=False)


def test_the_spec_repo_s_identity_stays_out_of_the_repo_git_points_at(tmp_path: Path, monkeypatch) -> None:
    # Under `git bisect run` or a hook, GIT_DIR names the outer checkout; the harness's spec repo is set up anyway.
    outer = tmp_path / "outer"
    subprocess.run(["git", "init", "-q", str(outer)], check=True)
    monkeypatch.setenv("GIT_DIR", str(outer / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(outer))
    monkeypatch.setenv("GIT_INDEX_FILE", str(outer / ".git" / "index"))
    work = tmp_path / "work"
    work.mkdir()
    repo = serve_slow.spec_repo(work)
    assert git(outer, "config", "--local", "--get", "user.email").stdout == ""
    assert git(repo, "config", "--local", "--get", "user.email").stdout.strip() == "test@example.test"
    assert git(repo, "log", "--format=%s").stdout.split("\n")[0] == "spec"
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(["git", "-C", str(outer), "rev-parse", "HEAD"], check=True, capture_output=True)
