"""Serve synthetic Cockpit records with delayed writes for browser checks.

Every non-GET /api/ request waits SLOW_SECONDS. All databases, settings, account records and
transcripts live under the temporary work folder. FakeRunner handles delivery; fake_copy answers Ask.
Checks can supply dialog.json, sessions.json, account-problems.json, providers.json, codex-homes.json and
extra-setup.toml (added to the setup as it is) before startup, then edit the generated rate-limits.json,
banked-resets.json or model-limits.json (each address's weekly limits for one model alone, in the shape
resets.model_limits gives). The spec is copied from this checkout into an isolated git repository. cookie.json holds the test cookie and loopback origin.
The server stops with its parent and removes its database on exit.

Usage: .venv/bin/python tests/browser/serve_slow.py <temporary-work-folder>
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from parent_watch import stop_with_parent, stopped

from cockpit import (
    auth,
    briefing,
    codex,
    config,
    db,
    delivery,
    emergency,
    projects,
    resets,
    sessions,
    sidechat,
    snooze_words,
    spend,
    store,
)
from cockpit.accounts import Problem
from cockpit.app import Settings, create_app
from tests.conftest import (
    CLAUDE_SETTINGS_TEXT,
    COOKIE_KEY,
    EMAIL,
    SESSION_A,
    SESSION_B,
    TOKEN,
    FakeCache,
    FakeRunner,
    make_session,
)

SLOW_SECONDS = 2.0
FAKE_COPY_SECONDS = 2.0
# Long enough that a check sees the page while Cockpit is still reading, past a 5 s poll.
FAKE_READ_SECONDS = 6.0


class SlowWrites:
    """ASGI wrapper: holds every non-GET /api/ request for SLOW_SECONDS, then hands it to the app."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/api/") and scope["method"] != "GET":
            await asyncio.sleep(SLOW_SECONDS)
        await self.app(scope, receive, send)


def fake_copy(session: sessions.Session, text: str) -> tuple[str, dict]:
    """Stands in for Ask's copy of a session (sidechat.ask_copy): it answers with the question it got."""
    time.sleep(FAKE_COPY_SECONDS)
    question = text.rsplit("question:\n", 1)[-1]
    return f"A fake copy of **{session.name}** got: {question}", {"output_tokens": 10}


def fake_snooze_read(text: str, zone, now: datetime, events: list) -> datetime:
    """Stands in for Claude reading a typed snooze time (snooze_words.read): "in N minutes" is read after
    FAKE_READ_SECONDS; other words name no time."""
    time.sleep(FAKE_READ_SECONDS)
    words = text.split()
    if len(words) == 3 and words[0] == "in" and words[1].isdigit() and words[2] == "minutes":
        return now + timedelta(minutes=int(words[1]))
    raise snooze_words.Unread("no such event in the calendar")


def preview_accounts(work: Path) -> tuple[config.Account, ...]:
    """An account folder per address in <work>/rate-limits.json: personal (the default) first, then work, spare."""
    addresses = list(json.loads((work / "rate-limits.json").read_text())["accounts"])
    accounts = []
    for name, address in zip(("personal", "work", "spare"), addresses, strict=False):
        folder = work / "accounts" / name
        folder.mkdir(parents=True)
        oauth = {"emailAddress": address, "refreshTokenExpiresAt": (time.time() + 30 * 86400) * 1000}
        (folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}))
        (work / "accounts" / f"{name}.json").write_text(json.dumps({"oauthAccount": oauth}))
        accounts.append(config.Account(name=name, letter={"personal": "P", "work": "W", "spare": "S"}[name], folder=folder, state=work / "accounts" / f"{name}.json", default=not accounts))
    return tuple(accounts)


# `git bisect run` and git hooks export these, and they outrank `git -C`: leaving them set lets the tests' setup write
# its identity into the outer checkout's .git/config and change that checkout's commit author. tests/conftest.py drops
# them for pytest the same way.
OUTER_GIT = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")


def own_git_env() -> dict[str, str]:
    """This process's environment without the variables that point git at another repo."""
    return {name: value for name, value in os.environ.items() if name not in OUTER_GIT}


def spec_repo(work: Path) -> Path:
    """A git repo of its own in the work folder holding this checkout's spec, so a spec edit through the harness
    commits there and never into the checkout."""
    repo = work / "spec-repo"
    (repo / "docs").mkdir(parents=True)
    shutil.copy(ROOT / "docs" / "SPEC.md", repo / "docs" / "SPEC.md")
    for command in (
        ["init", "-q", "-b", "main"],
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.test"],
        ["add", "docs/SPEC.md"],
        ["commit", "-q", "-m", "spec"],
    ):
        subprocess.run(["git", "-C", str(repo), *command], check=True, capture_output=True, env=own_git_env())
    return repo


def remove_copy(work: Path) -> None:
    """Remove the fixture database and its journal files on exit."""
    for name in ("cockpit.db", "cockpit.db-wal", "cockpit.db-shm"):
        (work / name).unlink(missing_ok=True)


def seed_board(path: Path) -> None:
    """Invent Tasks work for the queue, Busy, Waiting, messages and finished-work views."""
    with db.connect(path) as conn:
        db.migrate(conn)
        projects.put(conn, "agent:fixture", "tasks", "Tasks", None, False, None, Path("/"))
        for at, title in enumerate(("Review reminder settings", "Approve the CSV export", "Check list sharing",
                                    "Choose the weekly reminder", "Check completed task exports")):
            item = store.upsert_item(conn, {
                "key": f"note:fixture-{at}", "title": title, "project": "tasks", "stage": "building",
                "summary": "Synthetic application work for browser checks.", "sessions": [SESSION_B],
            }, "agent:fixture")
            store.upsert_step(conn, {
                "item_id": item["id"], "need": "review", "ask": title, "score": 80 - at,
                "session_id": SESSION_B, "body": "Check the change and reply to the session.",
            }, "agent:fixture")
        store.upsert_item(conn, {"key": "note:fixture-done", "title": "Fix list ordering", "stage": "done"}, "user")
        busy = []
        for at, title in enumerate(("Build reminder selector", "Add shared list export")):
            busy.append(store.upsert_item(conn, {
                "key": f"note:fixture-busy-{at}", "title": title, "project": "tasks", "stage": "building",
                "summary": "Preparing a sample list for the next browser preview.", "sessions": [SESSION_B],
            }, "agent:fixture"))
        until = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        store.upsert_item(conn, {
            "key": "note:fixture-review", "title": "Review shared grocery list", "stage": "review",
            "waiting_on": [{"kind": "person", "who": "Alex", "what": "review", "until": until}],
        }, "agent:fixture")
        for by_user in (False, True):
            store.upsert_item(conn, {
                "key": f"note:fixture-reminder-{by_user}", "title": "Check weekly reminder" if by_user else "Check export preview",
                "stage": "building", "sessions": [SESSION_A],
                "waiting_on": [{"kind": "agent", "who": SESSION_A, "until": until, "by_user": by_user,
                                "why": "Check the sample list tomorrow"}],
            }, "agent:fixture")
        for at, text in enumerate(("Include the grocery list", "Keep reminder dates", "Use CSV format",
                                    "Include completed tasks", "The export preview looks ready")):
            stamp = (datetime.now(UTC) - timedelta(minutes=10 - at)).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                "INSERT INTO messages (author, item_id, session_id, text, state, created_at, sent_at)"
                " VALUES ('user', ?, ?, ?, 'sent', ?, ?)",
                (busy[0]["id"], SESSION_B, text, stamp, stamp),
            )


def ask_for_briefing(path: Path, day: str) -> None:
    """Record that Cockpit asked a session for the briefing for `day` (YYYY-MM-DD), as briefing.launch_once does
    each morning, with the message already sent, so an agent may post that day's briefing."""
    with db.connect(path) as conn, db.transaction(conn):
        created = db.now_iso()
        cursor = conn.execute(
            "INSERT INTO messages (author, new_cwd, new_name, text, state, created_at, as_prompt)"
            " VALUES ('cockpit', ?, 'Briefing', ?, 'sent', ?, 0)",
            (str(path.parent / "checkouts"), "Write the morning briefing.", created),
        )
        db.kv_set(conn, "briefing_launch", {
            "day": day, "since": f"{day}T00:00:00Z", "until": f"{day}T07:00:00Z", "message_id": cursor.lastrowid,
        })


def main(work: Path) -> None:
    # The server's own git, such as a spec edit's commit, stays in the harness's repos too.
    for name in OUTER_GIT:
        os.environ.pop(name, None)
    parent = stop_with_parent(work)
    try:
        serve(work)
    finally:
        stopped(work, parent)
        remove_copy(work)


def parts_fixture(work: Path):
    """Read installation fixtures before the scratch folder is emptied; build them after the database is seeded."""
    return list


def serve(work: Path) -> None:
    codex_usage = json.loads((work / "codex-usage.json").read_text()) if (work / "codex-usage.json").exists() else []
    dialog = json.loads((work / "dialog.json").read_text()) if (work / "dialog.json").exists() else None
    problems_file = work / "account-problems.json"
    problems = json.loads(problems_file.read_text()) if problems_file.exists() else []
    listed = json.loads((work / "sessions.json").read_text()) if (work / "sessions.json").exists() else []
    providers = tuple(json.loads((work / "providers.json").read_text())) if (work / "providers.json").exists() else ("claude",)
    extra_setup = (work / "extra-setup.toml").read_text() if (work / "extra-setup.toml").exists() else ""
    # A check that posts a briefing names its day: Cockpit then has asked for that day's briefing (briefing.put).
    # It may also give earlier days' briefings, by day, which the database then holds.
    briefings = json.loads((work / "briefing.json").read_text()) if (work / "briefing.json").exists() else None
    # Sign-in checks supply scratch account folders and use a stand-in executable.
    scratch_codex = json.loads((work / "codex-homes.json").read_text()) if (work / "codex-homes.json").exists() else None
    build_parts = parts_fixture(work)
    # Emptied, not removed and made again: a folder its test removed meanwhile stays gone (parent_watch.py).
    for entry in work.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    (work / "checkouts").mkdir()
    # Configuration and every data reader use only this run's temporary files.
    setup_copy = work / "setup.toml"
    setup_copy.write_text(
        (ROOT / "tests" / "setup.toml").read_text()
        + f"providers = {json.dumps(list(providers))}\n"
        + extra_setup
    )
    config.SETUP_PATH = setup_copy
    config.setup.cache_clear()
    config.DB_PATH = work / "cockpit.db"
    config.CLAUDE_SETTINGS = work / "claude-settings.json"
    config.CLAUDE_RATE_LIMITS = work / "rate-limits.json"
    config.CLAUDE_SETTINGS.write_text(CLAUDE_SETTINGS_TEXT)
    now = datetime.now(UTC)
    rates = {address: {
        "account": {"emailAddress": address, "displayName": "Test account", "organizationType": "claude_max"},
        "captured_at": now.timestamp(), "session_id": SESSION_A, "config_dir": str(work / "accounts"),
        "rate_limits": {
            "five_hour": {"used_percentage": 20, "resets_at": (now + timedelta(hours=3)).timestamp()},
            "seven_day": {"used_percentage": 40, "resets_at": (now + timedelta(days=4)).timestamp()},
        },
    } for address in ("sam@example.test", "alex@example.test")}
    config.CLAUDE_RATE_LIMITS.write_text(json.dumps({"accounts": rates}))
    seed_board(config.DB_PATH)
    if briefings:
        for day, body in sorted(briefings.get("earlier", {}).items()):
            ask_for_briefing(config.DB_PATH, day)
            with db.connect(config.DB_PATH) as conn:
                briefing.put(conn, "agent:briefing", day, body)
        ask_for_briefing(config.DB_PATH, briefings["day"])
    accounts = preview_accounts(work)
    config.ACCOUNTS = accounts
    banked = work / "banked-resets.json"
    resets.known = lambda address: json.loads(banked.read_text()).get(address) if banked.exists() else None
    by_model = work / "model-limits.json"
    resets.model_limits_of = lambda address: json.loads(by_model.read_text()).get(address, []) if by_model.exists() else []
    sessions.PROJECTS_DIR = work / "projects"
    spend_index = spend.SpendIndex(sessions.PROJECTS_DIR)
    config.CODEX_HOMES = tuple(Path(home) for home in scratch_codex) if scratch_codex else (work / "codex", work / "codex-2")
    for home, entries in zip(config.CODEX_HOMES, codex_usage, strict=False):
        folder = home / "sessions"
        folder.mkdir(parents=True)
        session_id = entries[0]["payload"]["id"]
        (folder / f"rollout-{session_id}.jsonl").write_text(
            "".join(json.dumps(entry, separators=(",", ":")) + "\n" for entry in entries)
        )
    codex_index = codex.CodexIndex(config.CODEX_HOMES)
    # The emergency page's session and turns stay in the work folder; a check puts its own `claude` on PATH.
    emergency.STATE_PATH = work / "emergency.json"
    emergency.OUTPUT_DIR = work / "emergency"

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"

    settings = Settings(
        db_path=work / "cockpit.db",
        token=TOKEN,
        cookie_key=COOKIE_KEY,
        google=auth.GoogleClient(client_id="client-id", client_secret="s" * 24),
        email=EMAIL,
        origin=origin,
        public_origin=f"http://localhost:{sock.getsockname()[1]}",
        checkouts_dir=work / "checkouts",
        sweep_cwd=work,
        sweep_prompt="run a sweep",
        spec_repo=spec_repo(work),
        # Commit only: the repo has no remote.
        spec_branch="",
        skills_repo=work / "skills-repo",
        skills_folder="skills",
        skills_branch="skills",
        claude_settings=work / "claude-settings.json",
        claude_rate_limits=work / "rate-limits.json",
        claude_accounts=accounts,

        codex=str(ROOT / "tests" / "fake_codex.py") if scratch_codex else "/bin/false",
        claude=[sys.executable, str(ROOT / "tests" / "fake_claude.py"), str(work / "accounts" / "personal")],
        attach="claude attach {session}",
        exchange=lambda client, code, redirect, verifier: {},
        secrets_dir=work / "secrets",
        signins_dir=work / "signins",
        providers=providers,
    )
    working = replace(make_session(SESSION_B, pid=102, state="working", cwd=str(work)), name="instant test, working")
    if dialog is not None:
        working = replace(working, job_state="working", tempo="blocked", **dialog)
    # The state leaves out a session's first prompt (`intent`), so a listed session has none.
    names = [field.name for field in dataclasses.fields(sessions.Session)]
    more = [sessions.Session(**{name: entry.get(name, "") for name in names}) for entry in listed]
    cache = FakeCache([replace(make_session(SESSION_A, pid=None, state="done"), name="instant test, finished"), working, *more])
    cache.account_problems = [Problem(**problem) for problem in problems]
    deliverer = delivery.Deliverer(
        db_path=settings.db_path,
        sockets_dir=work / "socks",
        run=FakeRunner(),
        trust=lambda account, cwd: None,
        lookup=lambda session_id: next((s for s in cache.sessions if s.session_id == session_id), None),
        short_id_to_uuid=lambda account, short_id: SESSION_A if short_id == SESSION_A[:8] else SESSION_B,
        pick=lambda: (config.ACCOUNTS[0], "the only account in use"),
        listed=cache.get,
    )
    sidechat.ask_copy = fake_copy
    snooze_words.read = fake_snooze_read
    threading.Thread(target=spend.refresh_forever, args=(spend_index, codex_index), daemon=True).start()
    app = SlowWrites(create_app(settings, deliverer, cache, spend_index, codex_index, build_parts()))
    cookie = auth.session_cookie_value(COOKIE_KEY, EMAIL, int(time.time()) + 86400)
    # Written whole and then renamed, so a test that polls for the file never reads it half written.
    # And a visitor cookie for someone a page isn't shared with, as Google sign-in on the public address would set it.
    stranger = auth.visitor_cookie_value(auth.visitor_key(COOKIE_KEY), "stranger@example.net", int(time.time()) + 86400)
    (work / "cookie.json.part").write_text(json.dumps({
        "name": auth.SESSION_COOKIE, "value": cookie, "origin": origin, "public_origin": settings.public_origin,
        "visitor": {"name": auth.VISITOR_COOKIE, "value": stranger, "email": "stranger@example.net"},
    }))
    (work / "cookie.json.part").replace(work / "cookie.json")
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    server.run(sockets=[sock])


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
