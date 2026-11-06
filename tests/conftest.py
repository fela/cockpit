from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cockpit import accounts, auth, codex, config, delivery, github, permissions, sessions, spend
from cockpit.app import Settings, create_app
from cockpit.sessions import Listing, Session

# The tests' setup file (SPEC SCOPE-5) uses EMAIL for the account that signs in.
config.SETUP_PATH = Path(__file__).parent / "setup.toml"
ORIGIN = "https://cockpit.example.test"
EMAIL = "sam@example.com"
FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"
TOKEN = "t" * 40
COOKIE_KEY = b"k" * 32
SESSION_A = "11111111-1111-4111-8111-111111111111"
SESSION_B = "22222222-2222-4222-8222-222222222222"
CLAUDE_SETTINGS_TEXT = """{
  "model": "opus",
  "permissions": {
    "defaultMode": "auto",
    "allow": ["Bash(gh pr view *)"],
    "ask": ["Bash(git push *)"]
  },
  "autoMode": {
    "allow": ["$defaults", "Reading files anywhere under the home directory is fine."]
  }
}
"""
SPEC_TEXT = r"""# Spec

Intro paragraph.

## Requirements

| ID | Requirement | Origin | Strength | Status | Evidence | Last change | Comments |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ONE-1 | The tool does one thing. | User | Should | Live | 2026-01-04 09:00:00 "one thing" | 2026-01-04 created (Claude) |  |
| ONE-2 | A pipe \| inside a cell. | User | Could | Not done | "x" | 2026-01-04 created (Claude) |  |

## Notes

- First note.
- Second note.
"""


def make_session(
    session_id: str, pid: int | None, state: str = "blocked", cwd: str = "/", account: str = "personal"
) -> Session:
    return Session(
        id=session_id[:8],
        session_id=session_id,
        name=f"session {session_id[:4]}",
        state=state,
        pid=pid,
        status="idle" if pid else "",
        tempo="",
        job_state="",
        in_flight=[],
        cwd=cwd,
        detail="doing things",
        needs="pick A or B" if state == "blocked" else "",
        intent="",
        updated_at="2026-01-04T09:00:00Z",
        links=[],
        effort="",
        version="2.1.301" if pid else "",
        account=account,
    )


class FakeCache:
    def __init__(self, sessions: list[Session]) -> None:
        self.sessions = sessions
        self.warnings: list[str] = []
        self.account_problems: list[accounts.Problem] = []

    def get(self) -> list[Session]:
        return self.sessions

    def snapshot(self, wait: bool) -> Listing:
        return Listing(self.sessions, self.warnings, self.account_problems, frozenset())

    def invalidate(self) -> None:
        """Nothing to drop: tests set `sessions` directly."""


@dataclass
class FakeRunner:
    output: str = "backgrounded · abcd1234\n"
    returncode: int = 0
    calls: list[tuple[list[str], Path]] = field(default_factory=list)
    # The account each call ran on, by name, in the order of `calls`.
    accounts: list[str] = field(default_factory=list)

    def __call__(self, account: config.Account, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, cwd))
        self.accounts.append(account.name)
        return subprocess.CompletedProcess(args, self.returncode, stdout=self.output, stderr="")


@dataclass
class World:
    settings: Settings
    client: TestClient
    db_path: Path
    cache: FakeCache
    runner: FakeRunner
    deliverer: delivery.Deliverer
    checkout: Path
    exchange_result: dict[str, Any]
    spec_repo: Path
    claude_settings: Path
    claude_rate_limits: Path
    claude_state: Path
    spend_index: spend.SpendIndex
    codex_index: codex.CodexIndex

    def user(self) -> dict[str, str]:
        return {"origin": ORIGIN}

    def agent(self, name: str = "sweep") -> dict[str, str]:
        return {"authorization": f"Bearer {TOKEN}", "x-cockpit-as": name}


@pytest.fixture(autouse=True)
def no_stop_grace(monkeypatch) -> None:
    """The fake `claude` has no daemon to settle after a stop; the test that checks the wait sets it."""
    monkeypatch.setattr(delivery, "STOP_GRACE_SECONDS", 0)


@pytest.fixture(autouse=True)
def no_refusal_hold(monkeypatch) -> None:
    """A refusal shows at once, so a test sees its card right after reporting it; the tests of the hold set it."""
    monkeypatch.setattr(permissions, "HOLD_SECONDS", 0)
    monkeypatch.setattr(permissions, "HOLD_MAX_SECONDS", 0)


@pytest.fixture(autouse=True)
def claude_accounts(monkeypatch, tmp_path: Path) -> tuple[config.Account, config.Account]:
    """The Claude accounts are folders of the test's own, never this machine's: personal (the default one) and work,
    whose folder does not exist until a test makes it."""
    folder = tmp_path / "accounts"
    personal = config.Account(name="personal", letter="P", folder=folder / "claude", state=folder / "claude.json", default=True)
    work = config.Account(
        name="work", letter="W", folder=folder / "claude-work", state=folder / "claude-work" / ".claude.json", default=False
    )
    monkeypatch.setattr(config, "ACCOUNTS", (personal, work))
    return personal, work


@pytest.fixture(autouse=True)
def transcripts(monkeypatch, tmp_path: Path) -> Path:
    """Session transcripts come from a folder of the test's own, never from this machine's sessions."""
    folder = tmp_path / "projects"
    monkeypatch.setattr(sessions, "PROJECTS_DIR", folder)
    monkeypatch.setattr(sessions, "_usages", {})
    return folder


@pytest.fixture(autouse=True)
def merged_pulls(monkeypatch) -> set[str]:
    """Whether a PR merged comes from this set, never from GitHub: a test adds the refs (`owner/repo#n`) it merged."""
    merged: set[str] = set()
    monkeypatch.setattr(github, "pull_merged", lambda ref: ref in merged)
    return merged


@pytest.fixture(autouse=True)
def database(monkeypatch, tmp_path: Path) -> None:
    """Code that opens Cockpit's database by its configured path, such as `cockpit attach`, opens the test's own."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "configured.db")


@pytest.fixture(autouse=True)
def own_git_repos(monkeypatch) -> None:
    """Git commands in a test act on the test's own repos. `git bisect run` and git hooks export GIT_DIR (and hooks
    GIT_INDEX_FILE), which outrank `git -C`: under them the spec repo's first commit landed in the outer checkout."""
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def world(tmp_path: Path) -> World:
    checkouts = tmp_path / "checkouts"
    checkout = checkouts / "app"
    (checkout / ".git").mkdir(parents=True)
    google = auth.GoogleClient(client_id="client-id", client_secret="s" * 24)
    spec_repo = tmp_path / "spec-repo"
    (spec_repo / "docs").mkdir(parents=True)
    (spec_repo / "docs" / "SPEC.md").write_text(SPEC_TEXT)
    # The spec's checkout on the deploy branch, which spec edits push to a bare remote (SPEC CODE-7).
    spec_remote = tmp_path / "spec-remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(spec_remote)], check=True, capture_output=True)
    for command in (
        ["init", "-q", "-b", "main"],
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.test"],
        ["add", "docs/SPEC.md"],
        ["commit", "-q", "-m", "spec"],
        ["remote", "add", "origin", str(spec_remote)],
        ["push", "-q", "-u", "origin", "main"],
    ):
        subprocess.run(["git", "-C", str(spec_repo), *command], check=True, capture_output=True)
    claude_settings = tmp_path / "claude" / "settings.json"
    claude_settings.parent.mkdir()
    claude_settings.write_text(CLAUDE_SETTINGS_TEXT)
    claude_settings.chmod(0o600)
    claude_state = tmp_path / "claude.json"
    claude_state.write_text(json.dumps({"projects": {}}))
    claude_state.chmod(0o600)
    exchange_result: dict[str, Any] = {}
    settings = Settings(
        db_path=tmp_path / "cockpit.db",
        token=TOKEN,
        cookie_key=COOKIE_KEY,
        google=google,
        email=EMAIL,
        origin=ORIGIN,
        checkouts_dir=checkouts,
        sweep_cwd=tmp_path,
        sweep_prompt="run a sweep",
        spec_repo=spec_repo,
        spec_branch="main",
        # Created by the skills tests that need it (test_skills.py).
        skills_repo=tmp_path / "skills-repo",
        skills_folder="skills",
        skills_branch="skills",
        claude_settings=claude_settings,
        claude_rate_limits=tmp_path / "claude" / "rate-limits.json",
        claude_accounts=config.ACCOUNTS,

        codex=str(tmp_path / "codex-command"),
        claude=[sys.executable, str(FAKE_CLAUDE), str(tmp_path / "accounts" / "claude")],
        attach="claude attach {session}",
        exchange=lambda client, code, redirect, verifier: exchange_result,
        secrets_dir=tmp_path / "secrets",
    )
    cache = FakeCache([make_session(SESSION_A, pid=None, cwd=str(tmp_path))])
    runner = FakeRunner()
    deliverer = delivery.Deliverer(
        db_path=settings.db_path,
        sockets_dir=tmp_path / "socks",
        run=runner,
        trust=lambda account, cwd: delivery.trust_checkout(checkouts, claude_state, cwd),
        lookup=lambda session_id: next((s for s in cache.sessions if s.session_id == session_id), None),
        short_id_to_uuid=lambda account, short_id: SESSION_B,
        pick=lambda: (config.ACCOUNTS[0], "the only account in use"),
    )
    spend_index = spend.SpendIndex(tmp_path / "projects")
    codex_index = codex.CodexIndex((tmp_path / "codex",))
    client = TestClient(create_app(settings, deliverer, cache, spend_index, codex_index), base_url=ORIGIN)
    expires = int(time.time()) + 3600
    client.cookies.set(auth.SESSION_COOKIE, auth.session_cookie_value(COOKIE_KEY, EMAIL, expires))
    return World(
        settings, client, settings.db_path, cache, runner, deliverer, checkout, exchange_result, spec_repo, claude_settings,
        settings.claude_rate_limits,
        claude_state,
        spend_index,
        codex_index,
    )


@pytest.fixture
def anonymous(world: World) -> TestClient:
    client = TestClient(world.client.app, base_url=ORIGIN, follow_redirects=False)
    return client
