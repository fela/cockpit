"""Build the public Cockpit demo: one HTML file that runs Cockpit's own pages on invented data, with no server.

It runs the real app in this process against a temporary database seeded with invented work (seed.py), records
every reply the pages ask for, and writes one self-contained file: the pages, their scripts and styles, and a small
stand-in backend in the browser (demo-shell.js, demo-child.js) that answers from those replies and changes them as the
visitor acts. No model runs and nothing leaves the browser.

Usage: .venv/bin/python demo/build.py [--out demo/dist/cockpit-demo.html] [--state PATH]
"""

from __future__ import annotations

import argparse
import base64
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = Path(__file__).resolve().parent
ORIGIN = "https://cockpit.example.test"
HOME_SHOWN = "/home/sam"

# Everything the app reads from the home folder comes from the temporary one, before cockpit is imported.
WORK = Path(tempfile.mkdtemp(prefix="cockpit-demo-")).resolve()
os.environ["HOME"] = str(WORK)
os.environ["TZ"] = "UTC"
for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
    os.environ.pop(name, None)
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEMO))

import json
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

REAL_DATETIME = datetime


class Clock:
    """How far the app's clock runs behind the real one while seeding: records are written at the time they say."""

    offset = timedelta(0)

    @classmethod
    def set(cls, hours: float) -> None:
        cls.offset = timedelta(hours=hours)


class ShiftedDatetime(REAL_DATETIME):
    @classmethod
    def now(cls, tz=None):
        moment = REAL_DATETIME.now(UTC) + Clock.offset
        return cls.fromtimestamp(moment.timestamp(), tz)

    @classmethod
    def utcnow(cls):
        return cls.now(UTC).replace(tzinfo=None)

    @classmethod
    def today(cls):
        return cls.now()


import seed
from fastapi.testclient import TestClient

from cockpit import (
    auth,
    codex,
    commands,
    config,
    db,
    delivery,
    github,
    machine,
    resets,
    sessions,
    sidechat,
    spend,
    store,
)
from cockpit.app import PART_PAGES, Settings, create_app, with_part_tags
from tests.conftest import CLAUDE_SETTINGS_TEXT, COOKIE_KEY, TOKEN, FakeCache, FakeRunner

EMAIL = "sam@example.com"
# Where a press on a step with no session would start one: the defaults. Every demo step with a command has a session.
NO_NEW_SESSION = {"new_cwd": None, "provider": None, "model": None, "effort": None, "account": None}


def patch_clock() -> None:
    for name, module in list(sys.modules.items()):
        if (name.startswith("cockpit") or name == "seed") and getattr(module, "datetime", None) is REAL_DATETIME:
            module.datetime = ShiftedDatetime


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def make_repo(repo: Path, branch: str, write) -> Path:
    repo.mkdir(parents=True)
    write(repo)
    git(repo, "init", "-q", "-b", branch)
    git(repo, "config", "user.name", "Sam")
    git(repo, "config", "user.email", "sam@example.com")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "start")
    return repo


def accounts() -> tuple[config.Account, ...]:
    made = []
    for name, letter, address in (("personal", "P", "sam@example.test"), ("work", "W", "sam@work.example.test")):
        folder = WORK / ".claude-accounts" / name
        folder.mkdir(parents=True)
        oauth = {"emailAddress": address, "refreshTokenExpiresAt": (time.time() + 30 * 86400) * 1000}
        (folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}))
        state = WORK / ".claude-accounts" / f"{name}.json"
        state.write_text(json.dumps({"oauthAccount": oauth}))
        made.append(config.Account(name=name, letter=letter, folder=folder, state=state, default=not made))
    return tuple(made)


def rate_limits(path: Path) -> None:
    now = datetime.now(UTC)
    rates = {}
    for address, five, seven in (("sam@example.test", 35, 60), ("sam@work.example.test", 10, 20)):
        rates[address] = {
            "account": {"emailAddress": address, "displayName": "Sam", "organizationType": "claude_max"},
            "captured_at": now.timestamp(), "session_id": seed.session_id("dark mode"),
            "config_dir": str(WORK / ".claude-accounts"),
            "rate_limits": {
                "five_hour": {"used_percentage": five, "resets_at": (now + timedelta(hours=2, minutes=10)).timestamp()},
                "seven_day": {"used_percentage": seven, "resets_at": (now + timedelta(days=3, hours=5)).timestamp()},
            },
        }
    path.write_text(json.dumps({"accounts": rates}))


def fake_sessions() -> list[sessions.Session]:
    listed = []
    now = datetime.now(UTC)
    for at, (name, folder, state, status, tempo) in enumerate(seed.SESSIONS):
        sid = seed.session_id(name)
        alive = state in ("working", "blocked", "done")
        listed.append(sessions.Session(
            id=sid[:8], session_id=sid, name=name, state=state, pid=4100 + at if alive else None,
            status=status, tempo=tempo, job_state="working" if state == "working" else "",
            in_flight=["local_bash"] if state == "working" else [],
            cwd=seed.cwd(WORK, folder),
            detail="Adding the theme switch to settings" if state == "working" else "",
            needs="", intent="", updated_at=seed.iso(now - timedelta(minutes=7 + at * 23)), links=[],
            effort="", version="2.1.301" if alive else "", account="personal",
        ))
    return listed


def world():
    """The app, its seeded database and a signed-in client, all in WORK."""
    dev = WORK / "dev"
    for folder in ("tasks-app", "website"):
        (dev / folder).mkdir(parents=True)
    setup = WORK / "setup.toml"
    spec = make_repo(WORK / "spec-repo", "main", lambda repo: (repo / "docs").mkdir() or (repo / "docs" / "SPEC.md").write_text(seed.SPEC_TEXT))
    skills = make_repo(WORK / "skills-repo", "skills", seed.write_skills)
    setup.write_text("\n".join([
        'name = "Sam"', f'email = "{EMAIL}"', f'origin = "{ORIGIN}"', f'checkouts = "{dev}"',
        f'spec_repo = "{spec}"', f'skills_repo = "{skills}"', 'skills_folder = "skills"', 'skills_branch = "skills"',
        'attach = "claude attach {session}"', 'timezone = "UTC"', 'providers = ["claude"]', "",
    ]))
    config.SETUP_PATH = setup
    config.setup.cache_clear()
    config.DB_PATH = WORK / "cockpit.db"
    config.CLAUDE_SETTINGS = WORK / "claude-settings.json"
    config.CLAUDE_SETTINGS.write_text(CLAUDE_SETTINGS_TEXT)
    config.CLAUDE_RATE_LIMITS = WORK / "rate-limits.json"
    rate_limits(config.CLAUDE_RATE_LIMITS)
    config.ACCOUNTS = accounts()
    resets.known = lambda address: None
    sessions.PROJECTS_DIR = WORK / ".claude" / "projects"
    sessions.PROJECTS_DIR.mkdir(parents=True)
    config.CODEX_HOMES = (WORK / ".codex",)
    github.pull_merged = lambda ref: False
    machine.read = lambda: machine.measure(
        "0.80 0.70 0.60 2/300 40000\n",
        "MemTotal: 16777216 kB\nMemAvailable: 10485760 kB\nSwapTotal: 2097152 kB\nSwapFree: 2097152 kB\n",
        8, 512 * 1024**3, 300 * 1024**3,
    )
    sidechat.ask_copy = lambda session, text: ("Ask needs a live session; the demo has none.", {"output_tokens": 1})

    with db.connect(config.DB_PATH) as conn:
        db.migrate(conn)
        seeded = seed.seed(conn, Clock, WORK, dev)
        seed.put_briefing(conn, Clock)
        Clock.set(0)
    seed.write_transcripts(sessions.PROJECTS_DIR, WORK, datetime.now(UTC))
    spend_index = spend.SpendIndex(sessions.PROJECTS_DIR)
    spend_index.refresh(datetime.now(UTC))
    codex_index = codex.CodexIndex(config.CODEX_HOMES)

    settings = Settings(
        db_path=config.DB_PATH, token=TOKEN, cookie_key=COOKIE_KEY,
        google=auth.GoogleClient(client_id="client-id", client_secret="s" * 24),
        email=EMAIL, origin=ORIGIN, checkouts_dir=dev, sweep_cwd=WORK, sweep_prompt="run a sweep",
        spec_repo=spec, spec_branch="", skills_repo=skills, skills_folder="skills", skills_branch="skills",
        claude_settings=config.CLAUDE_SETTINGS, claude_rate_limits=config.CLAUDE_RATE_LIMITS,
        claude_accounts=config.ACCOUNTS, codex="/bin/false", claude=["/bin/false"],
        attach="claude attach {session}", exchange=lambda client, code, redirect, verifier: {},
        secrets_dir=WORK / "secrets", signins_dir=WORK / "signins", providers=("claude",),
    )
    cache = FakeCache(fake_sessions())
    cache.account_problems = []
    deliverer = delivery.Deliverer(
        db_path=settings.db_path, sockets_dir=WORK / "socks", run=FakeRunner(), trust=lambda account, cwd: None,
        lookup=lambda sid: next((s for s in cache.sessions if s.session_id == sid), None),
        short_id_to_uuid=lambda account, short_id: next(
            (s.session_id for s in cache.sessions if s.id == short_id), short_id),
        pick=lambda: (config.ACCOUNTS[0], "the only account in use"),
        listed=cache.get,
    )
    with db.connect(config.DB_PATH) as conn:
        seed.put_writing(conn, seeded, cache.sessions)
    # The demo shows Cockpit alone, with no parts (parts.py).
    app = create_app(settings, deliverer, cache, spend_index, codex_index, [])
    client = TestClient(app, base_url=ORIGIN, headers={"origin": ORIGIN})
    client.cookies.set(auth.SESSION_COOKIE, auth.session_cookie_value(COOKIE_KEY, EMAIL, int(time.time()) + 86400))
    return World(client, seeded, cache, dev)


class World:
    def __init__(self, client, seeded, cache, dev):
        self.client = client
        self.seeded = seeded
        self.cache = cache
        self.dev = dev

    def get(self, url: str) -> Any:
        reply = self.client.get(url)
        if reply.status_code != 200:
            raise SystemExit(f"GET {url}: {reply.status_code} {reply.text[:400]}")
        kind = reply.headers.get("content-type", "").split(";")[0]
        return {"j": reply.json()} if kind == "application/json" else {"t": reply.text, "ct": kind}

    def post(self, url: str, body: dict[str, Any]) -> Any:
        reply = self.client.post(url, json=body)
        if reply.status_code != 200:
            raise SystemExit(f"POST {url}: {reply.status_code} {reply.text[:400]}")
        return reply.json()

    def state(self) -> dict[str, Any]:
        return self.get("/api/state")["j"]

    def conn(self) -> sqlite3.Connection:
        return closing(db.connect(config.DB_PATH))

    def session(self, sid: str, working: bool, doing: str = "") -> None:
        """The cached session `sid` turns to working on `doing`, or back to done and idle."""
        def changed(session):
            if session.session_id != sid:
                return session
            if working:
                return replace(session, state="working", status="busy", tempo="active", job_state="working",
                               detail=doing, in_flight=["local_bash"])
            return replace(session, state="done", status="idle", tempo="idle", job_state="", detail="", in_flight=[])
        self.cache.sessions = [changed(session) for session in self.cache.sessions]


# The page's lists of steps, in the order the page draws them.
STEP_LISTS = ("queue", "ready_next", "with_agents", "snoozed")
# Which table an id field counts in, so the demo can give each new record an id of its own in the browser.
ID_KEYS = {
    "step_id": "steps", "item_id": "items", "message_id": "messages", "handoff_message_id": "messages",
    "result_message_id": "messages", "run_id": "command_runs", "page_id": "item_pages", "image_id": "item_images",
}


def id_table(record: dict[str, Any]) -> str | None:
    """The table a record's own `id` counts in, by the fields it has."""
    if "ask" in record and ("need" in record or "status" in record):
        return "steps"
    if "key" in record and "title" in record:
        return "items"
    if "text" in record and ("via" in record or "take_back_until" in record or "author" in record):
        return "messages"
    if "exit_code" in record:
        return "command_runs"
    return None


def list_paths(state: dict[str, Any]) -> list[str]:
    """The state's lists, top level and one level down (`closed_recently/steps`), apart from the sessions."""
    paths = []
    for key, value in state.items():
        if key in ("sessions", "linked_sessions"):
            continue
        if isinstance(value, list):
            paths.append(key)
        elif isinstance(value, dict):
            paths.extend(f"{key}/{inner}" for inner, listed in value.items() if isinstance(listed, list))
    return paths


def at_path(state: dict[str, Any], path: str) -> list[Any]:
    head, _, tail = path.partition("/")
    return state[head][tail] if tail else state[head]


def belongs(entry: Any, item_id: int) -> bool:
    """Whether a list entry is the item's: one of its steps, messages or rows, or the item itself."""
    if isinstance(entry, int) and not isinstance(entry, bool):
        return entry == item_id
    if not isinstance(entry, dict):
        return False
    if entry.get("item_id") == item_id:
        return True
    return "key" in entry and "title" in entry and entry.get("id") == item_id


class Recorder:
    """Plays each answer the demo has a prepared reply for through the real app, and keeps what the page then shows.

    Each play starts from the seeded world and records the answered item's part of the state ("slice") at each
    moment: handed to its session, the session at work, and the session's next step. The browser splices these
    slices into its own state when the visitor answers, so one item's story never disturbs another's.
    """

    def __init__(self, w: World) -> None:
        self.w = w
        self.base = w.state()
        self.sessions = list(w.cache.sessions)
        with w.conn() as conn:
            tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
            self.maxes = {name: conn.execute(f'SELECT coalesce(max(rowid), 0) FROM "{name}"').fetchone()[0]
                          for name in tables if not name.startswith("sqlite_")}
            self.snap = sqlite3.connect(":memory:")
            conn.backup(self.snap)

    def restore(self) -> None:
        with self.w.conn() as conn:
            self.snap.backup(conn)
        self.w.cache.sessions = list(self.sessions)

    def slice_of(self, item_id: int) -> dict[str, Any]:
        state = self.w.state()
        item = next((entry for entry in state["items"] if entry["id"] == item_id), None)
        sids = set(item["sessions"]) if item else set()
        return {
            "item_id": item_id, "at": state["now"], "counts": state["counts"],
            "lists": {path: [entry for entry in at_path(state, path) if belongs(entry, item_id)]
                      for path in list_paths(state)},
            "sessions": [entry for entry in state["sessions"] if entry["session_id"] in sids],
            "history": self.w.get(f"/api/items/{item_id}/history")["j"],
        }

    def sent(self, message_id: int) -> None:
        """The message left for its session, as delivery would mark it."""
        with self.w.conn() as conn:
            conn.execute(
                "UPDATE messages SET state = 'sent', sent_at = ?, via = 'socket', take_back_until = NULL WHERE id = ?",
                (seed.iso(datetime.now(UTC)), message_id),
            )

    def next_step(self, item: dict[str, Any], step_id: int | None, plan: dict[str, Any]) -> dict[str, Any]:
        """The session closes the answered step and puts the next one, as `plan` says."""
        sid = item["sessions"][0] if item["sessions"] else None
        with self.w.conn() as conn:
            if step_id is not None:
                store.step_action(conn, step_id, "done", {"note": "answered"}, "agent:demo")
            store.upsert_item(conn, {"key": item["key"], **plan.get("item", {}), "doing": ""}, "agent:demo")
            put = store.upsert_step(conn, {"item_id": item["id"], "session_id": sid, **plan["step"]}, "agent:demo")
        if sid:
            self.w.session(sid, False)
        return put.get("step", put)

    def working(self, item: dict[str, Any], doing: str) -> None:
        with self.w.conn() as conn:
            store.upsert_item(conn, {"key": item["key"], "doing": doing}, "agent:demo")
        if item["sessions"]:
            self.w.session(item["sessions"][0], True, doing)

    def play(self, step: dict[str, Any], plan: dict[str, Any], deeper: bool) -> dict[str, Any] | None:
        """The visitor answers `step`; its session works and puts `plan`'s step. None when the app refuses it."""
        item_id = step["item_id"]
        reply = self.w.client.post(f"/api/steps/{step['id']}/hand-off", json={"text": seed.ANSWER})
        if reply.status_code != 200:
            print(f"  no reply for {step['item']['key']} {step['need']}: {reply.status_code} {reply.text[:200]}")
            return None
        reply = reply.json()
        played = {"match": plan.get("match"), "reply": reply, "handed": self.slice_of(item_id)}
        self.sent(reply["id"])
        item = self.item(item_id)
        self.working(item, plan["doing"])
        played["working"] = self.slice_of(item_id)
        put = self.next_step(self.item(item_id), step["id"], plan)
        played["after"] = self.slice_of(item_id)
        played["deeper"] = {}
        if deeper and put.get("status", "open") == "open":
            put_step = self.open_step(put["id"])
            if put_step is not None:
                again = self.play(put_step, seed.GENERIC, False)
                if again is not None:
                    played["deeper"][put["id"]] = [again]
        return played

    def item(self, item_id: int) -> dict[str, Any]:
        with self.w.conn() as conn:
            return store.get_item(conn, item_id)

    def open_step(self, step_id: int) -> dict[str, Any] | None:
        state = self.w.state()
        return next((entry for name in STEP_LISTS for entry in state[name] if entry["id"] == step_id), None)

    def scenarios(self) -> dict[str, Any]:
        plans = seed.follow_ups(datetime.now(UTC))
        answers: dict[int, list[dict[str, Any]]] = {}
        for name in ("queue", "ready_next", "snoozed"):
            for step in self.base[name]:
                if step["command"]:
                    continue
                mine = [plan for plan in plans if plan["key"] == step["item"]["key"] and plan["need"] == step["need"]]
                played = []
                for plan in [*mine, seed.GENERIC]:
                    self.restore()
                    step_now = self.open_step(step["id"])
                    one = self.play(step_now, plan, True) if step_now else None
                    if one is not None:
                        played.append(one)
                if played:
                    answers[step["id"]] = played
        self.restore()
        return {"answers": answers, "run": self.run_scenarios(), "add": self.add()}

    def run_scenarios(self) -> dict[int, dict[str, Any]]:
        """Run on a step with a command: the command 'runs', its output reaches the session, which reports."""
        runs = {}
        for name in ("queue", "ready_next", "snoozed"):
            for step in self.base[name]:
                if not step["command"]:
                    continue
                self.restore()
                command = step["command"]
                seen_session = step["target"]["session_id"] if step["target"] else None
                with self.w.conn() as conn:
                    run, message = commands.start(
                        conn, step["id"], command["run"], command["cwd"], seen_session, NO_NEW_SESSION, self.w.dev)
                reply = {"run": run, "message": message}
                handed = self.slice_of(step["item_id"])
                with self.w.conn() as conn:
                    result = commands.finish(conn, self.w.dev, run["id"], 0, seed.RUN["output"], 0, "")
                self.sent(message["id"])
                self.sent(result["id"])
                item = self.item(step["item_id"])
                self.working(item, seed.RUN["doing"])
                working = self.slice_of(step["item_id"])
                self.next_step(self.item(step["item_id"]), step["id"], seed.RUN)
                runs[step["id"]] = {"reply": reply, "handed": handed, "working": working,
                                    "after": self.slice_of(step["item_id"]), "deeper": {}}
        self.restore()
        return runs

    def add(self) -> dict[str, Any]:
        """Add: the to-do the visitor writes becomes an item, a session starts on it and asks what done means."""
        self.restore()
        item = self.w.post("/api/items/add", {"text": seed.ANSWER, "part": None, "pinned": False, "fire_and_forget": False})
        item = item.get("item", item)
        added = self.slice_of(item["id"])
        sid = seed.session_id("added to-do")
        template = self.sessions[0]
        self.w.cache.sessions = [*self.w.cache.sessions, replace(
            template, id=sid[:8], session_id=sid, name="added to-do", state="done", status="idle", tempo="idle",
            job_state="", detail="", in_flight=[], cwd=str(self.w.dev), pid=4199,
        )]
        with self.w.conn() as conn:
            store.upsert_item(conn, {"key": item["key"], "sessions": [sid]}, "agent:demo")
        self.working(self.item(item["id"]), seed.ADDED["doing"])
        working = self.slice_of(item["id"])
        # The page gives each later to-do its own session by replacing this one's id, so the stage must name it.
        if [entry["session_id"] for entry in working["sessions"]] != [sid]:
            raise SystemExit("the added to-do's working stage does not show its session")
        self.next_step(self.item(item["id"]), None, seed.ADDED)
        after = self.slice_of(item["id"])
        self.restore()
        return {"item": item, "added": added, "working": working, "after": after}


def capture(w: World, base: dict[str, Any]) -> dict[str, Any]:
    """Every read the pages make, keyed by URL, as the seeded world answers it. A refused read stops the build."""
    got: dict[str, Any] = {}

    def take(url: str) -> Any:
        if url not in got:
            got[url] = w.get(url)
        return got[url].get("j")

    for url in (
        "/api/permissions", "/api/feedback", "/api/goals", "/api/inbox-settings", "/api/timezone",
        "/api/listed", "/api/patterns?days=14", "/api/spec", "/api/time?days=14&tz=UTC",
        *(f"/api/spend?period={period}" for period in ("week", "1", "7", "30")),
    ):
        take(url)
    # Search with Include closed: each closed item with the words it is found by; the browser matches them itself.
    corpus = []
    with w.conn() as conn:
        closed = conn.execute(
            "SELECT id, lower(title || ' ' || summary || ' ' || agent_summary || ' ' || key || ' ' || project)"
            " FROM items WHERE archived = 1 OR stage = 'done'").fetchall()
    for item_id, words in closed:
        corpus += [found | {"words": words} for found in take(f"/api/items/closed?q=%23{item_id}")["items"]]
    got["closed corpus"] = {"j": corpus}
    reply = w.client.get("/api/emergency")
    got["/api/emergency"] = {"j": reply.json(), "status": reply.status_code}
    for found in take("/api/briefings").get("briefings", []):
        take(f"/api/briefings/{found['id']}")
    for found in take("/api/writing"):
        take(f"/api/writing/{found['id']}")
    for found in take("/api/skills")["skills"]:
        take(f"/api/skills/{found['name']}")
    for found in take("/api/projects"):
        take(f"/api/projects/{found['name']}/logo.svg")
    for found in base["items"]:
        take(f"/api/items/{found['id']}/history")
    for found in take("/api/pages")["pages"]:
        for url in (f"/pages/{found['id']}", f"/pages/{found['id']}/figure?comments=1",
                    f"/pages/{found['id']}/figure?comments=0", f"/api/item-pages/{found['id']}/choices",
                    f"/api/item-pages/{found['id']}/comments"):
            take(url)
    for found in base["sessions"]:
        take(f"/api/sessions/{found['session_id']}/log")
    return got


def placeholders(played: Any, maxes: dict[str, int]) -> Any:
    """Ids the plays made, past the seeded world's, become `@@ID:table:n@@`: the browser gives each play ids of its own.

    The n-th new record of a table keeps its n through a play and the deeper plays after it, so a step the session
    put is the same step when the visitor answers it in turn.
    """
    def new(table: str | None, value: Any) -> Any:
        if table and isinstance(value, int) and not isinstance(value, bool) and value > maxes.get(table, 0):
            return f"@@ID:{table}:{value - maxes[table]}@@"
        return value

    def walk(value: Any, key: str = "") -> Any:
        if isinstance(value, str):
            return re.sub(r"\btodo-(\d+)\b", lambda m: f"todo-{new('items', int(m[1]))}", value)
        if isinstance(value, list):
            return [walk(entry, key) for entry in value]
        if not isinstance(value, dict):
            return value
        if key == "deeper":
            return {new("steps", int(step)): walk(plays) for step, plays in value.items()}
        # A history's steps, by id.
        if key == "steps" and value and all(isinstance(step, str) and step.isdigit() for step in value):
            return {str(new("steps", int(step))): walk(inner) for step, inner in value.items()}
        own = id_table(value)
        made = {}
        for field, inner in value.items():
            if field == "id":
                made[field] = new(own, inner)
            elif field in ID_KEYS:
                made[field] = new(ID_KEYS[field], inner) if not isinstance(inner, list) else [
                    new(ID_KEYS[field], one) for one in inner]
            else:
                made[field] = walk(inner, field)
        return made

    return walk(played)


def loose_ids(value: Any) -> set[str]:
    """Id fields with numbers in them that `placeholders` has no table for: each may need a place in ID_KEYS."""
    found = set()
    if isinstance(value, dict):
        for field, inner in value.items():
            number = isinstance(inner, int) and not isinstance(inner, bool)
            if number and field.endswith("_id") and field not in ID_KEYS:
                found.add(field)
            if number and field == "id" and id_table(value) is None:
                found.add("id in {" + ", ".join(sorted(value)[:8]) + "}")
            found |= loose_ids(inner)
    elif isinstance(value, list):
        for inner in value:
            found |= loose_ids(inner)
    return found


def scrub(text: str) -> str:
    """The temporary home folder reads as Sam's, in both the plain and the dashed form Claude names folders with."""
    dashed = str(WORK).replace("/", "-").replace(".", "-")
    return text.replace(str(WORK), HOME_SHOWN).replace(dashed, HOME_SHOWN.replace("/", "-"))


STATIC = ROOT / "src" / "cockpit" / "static"

# The pages the demo shows, by the name demo-shell.js asks for: the file, and for the main page its layout.
VIEWS = {
    "index-sidebar": ("index.html", "sidebar"),
    "index-classic": ("index.html", "classic"),
    "spec": ("spec.html", None),
    "briefing": ("briefing.html", None),
    "usage": ("usage.html", None),
    "pages": ("pages.html", None),
    "emergency": ("emergency.html", None),
}

# The few places the pages need to differ in the demo, each checked to match as often as it did when this was
# written: a change in Cockpit that moves one stops the build instead of leaving a page that talks to no one.
#   - Messages between the pages and their frames name the receiver's origin. Frames from srcdoc have none.
#   - A page and its Pages layer share a comment left unfinished by reaching into each other's window, which frames
#     without an origin cannot do: in the demo such a comment goes with its layer.
#   - Frames load through __demo.frame, which writes the page into them; the browser would ask a server.
#   - A project logo comes from the replies the build recorded.
#   - Signing in opens no tab; the demo refuses it with a message.
#   - An optimistic change, once its request went through, also changes the demo's stored state the way the page
#     changed its own drawing of it. demo-child.js `commit` explains.
REWRITES = [
    ("pages.js", "}, location.origin)", '}, "*")', 7),
    ("page-overlay.js", "}, location.origin)", '}, "*")', 3),
    ("pages.js", "event.origin !== location.origin", "!__demo.ours(event.origin)", 2),
    ("page-overlay.js", "event.origin !== location.origin", "!__demo.ours(event.origin)", 1),
    ("page-overlay.js",
     'frame.src = params.has("briefing") ? `/briefing?id=${params.get("briefing")}&overlay=1` : `/pages?${params}`;',
     '__demo.frame(frame, params.has("briefing") ? `/briefing?id=${params.get("briefing")}&overlay=1` : `/pages?${params}`);', 1),
    ("briefing.js", "}, location.origin)", '}, "*")', 4),
    # Another day's briefing takes the place of the one in the layer: the frame that holds it shows the new one.
    ("briefing.js", "    location.replace(link.href);", "    __demo.replaceFrame(link.href);", 1),
    ("pages.js", "const kept = parent.cockpitUnfinished;", "const kept = __demo.unfinished;", 1),
    ("page-overlay.js", "frame.contentWindow?.cockpitPagesTake?.();", '__demo.reach(frame, "cockpitPagesTake");', 1),
    ("page-overlay.js", "frame?.contentWindow?.cockpitPagesLeaving?.();", '__demo.reach(frame, "cockpitPagesLeaving");', 1),
    ("app.js", "frame.src = frame.dataset.src;", "__demo.frame(frame, frame.dataset.src);", 1),
    # The center layer shows a helper page (Briefing, Pages, Usage, Spec) in a frame, and that page's Escape asks the
    # layer to close.
    ("center.js",
     '  const frame = h("iframe", { class: "center-page", src: url.pathname + url.search + url.hash, title: name });',
     '  const frame = h("iframe", { class: "center-page", title: name });\n'
     "  __demo.frame(frame, url.pathname + url.search + url.hash);", 1),
    ("center.js", "CENTER_HELPERS[frame.contentWindow.location.pathname]", "CENTER_HELPERS[__demo.path(frame)]", 1),
    ("center.js", "event.origin === location.origin && data?.cockpitCenter",
     "__demo.ours(event.origin) && data?.cockpitCenter", 1),
    ("center-frame.js", 'parent.postMessage({ cockpitCenter: "close" }, location.origin);',
     'parent.postMessage({ cockpitCenter: "close" }, "*");', 1),
    ("pages.js", '    Loading.frame(frame);\n    frame.addEventListener("load", overlayReady',
     '    __demo.frame(frame, src);\n    Loading.frame(frame);\n    frame.addEventListener("load", overlayReady', 1),
    ("icons.js",
     "logo.src = `/projects/${encodeURIComponent(project.name)}/logo.svg?v=${encodeURIComponent(project.logo_at)}`;",
     "logo.src = __demo.asset(`/projects/${encodeURIComponent(project.name)}/logo.svg`);", 1),
    ("app.js", '    figureWarm.append(h("p", {}, frame));',
     '    __demo.frame(frame, frame.getAttribute("src"));\n    figureWarm.append(h("p", {}, frame));', 1),
    ("app.js", 'const tab = window.open("", "_blank");', "const tab = null;", 1),
    ("app.js", "    const answer = await work();\n    change.confirmed",
     "    const answer = await work();\n"
     "    await __demo.commit((state) => resorted(state, refilled(givenBack(state, apply(state)), 0), 0), answer);\n"
     "    change.confirmed", 1),
]

# The page's address and history belong to the demo's own page, which the frames cannot reach: demo-child.js keeps
# one for each page instead.
LOCATION = re.compile(r"(?<![\w$.])(?:window\.|document\.)?location\.")
HISTORY = re.compile(r"(?<![\w$.])(?:window\.)?history\.(pushState|replaceState|back|state|forward|go|length)\b")
# Vendored and left as it is.
UNTOUCHED = {"prompt-editor.js"}
LINK = re.compile(r'\s*<link rel="stylesheet" href="/static/([\w.-]+\.css)">')
SCRIPT = re.compile(r'\s*<script src="/static/([\w.-]+\.js)"( defer)?></script>')
FONT = re.compile(r'url\("/static/fonts/([\w.-]+\.woff2)"\)')


def data_uri(kind: str, raw: bytes) -> str:
    return f"data:{kind};base64," + base64.b64encode(raw).decode()


def script_text(name: str, text: str) -> str:
    """Text that may sit inside a <script> element: no end tag, and nothing an HTML parser takes for a comment."""
    # An HTML parser that meets "<!--" inside a script stops trusting "</script>" to end it. Inside a JavaScript
    # string or regular expression, "<\x21--" means the same text, so those are rewritten; anywhere else is refused.
    # The bundled editor holds three: the regex /^\s*<!--/ and the string "<!--" twice.
    safe = re.compile(r"""(?<=")<!--(?=")|(?<=\\s\*)<!--(?=/)""")
    text = safe.sub(lambda _: "<\\x21--", text)
    if "<!--" in text:
        raise SystemExit(f"{name} holds '<!--' outside a string or regex, which changes how a <script> element ends")
    return re.sub(r"</(script)", r"<\\/\1", text, flags=re.IGNORECASE)


def assets() -> dict[str, str]:
    """Every script and stylesheet the pages load, rewritten for the demo, by file name."""
    made: dict[str, str] = {}
    for path in sorted(STATIC.glob("*.js")) + sorted(STATIC.glob("*.css")):
        if path.name == "local-auth.js":
            continue
        made[path.name] = path.read_text()
    for name, old, new, times in REWRITES:
        found = made[name].count(old)
        if found != times:
            raise SystemExit(f"{name}: expected {old!r} {times} times, found {found}")
        made[name] = made[name].replace(old, new)
    for name, text in made.items():
        if name.endswith(".js") and name not in UNTOUCHED:
            text = LOCATION.sub("__demoLoc.", text)
            made[name] = HISTORY.sub(r"__demoHist.\1", text)
    fonts = {path.name: data_uri("font/woff2", path.read_bytes()) for path in (STATIC / "fonts").glob("*.woff2")}
    for name, text in made.items():
        if name.endswith(".css"):
            text = FONT.sub(lambda match: f'url("{fonts[match.group(1)]}")', text)
            if "/static/" in text or re.search(r"</style", text, re.IGNORECASE):
                raise SystemExit(f"{name} still points at /static/ or ends its <style> element early")
            made[name] = text
        else:
            made[name] = script_text(name, made[name])
    return made


def views(known: dict[str, str]) -> dict[str, str]:
    """Each page's HTML as the server sends it, with its stylesheets and scripts as placeholders the shell fills.

    Deferred scripts run, in order, after the page is read: at the end of its body they do the same."""
    icon = data_uri("image/svg+xml", (STATIC / "icon.svg").read_bytes())
    made = {}
    for view, (file, layout) in VIEWS.items():
        html = (STATIC / file).read_text()
        if file in PART_PAGES:
            html = with_part_tags(html, [])
        if layout:
            html = html.replace("<body>", f'<body data-layout="{layout}">', 1)
        html = html.replace("__COCKPIT_STYLE_NONCE__", "demo")
        html = html.replace('href="/static/icon.svg"', f'href="{icon}"')
        # The demo is no app to install: its pages name no app manifest or home-screen icon, which the browser
        # would ask the demo's host for.
        html = re.sub(r'\n\s*<link rel="(?:manifest|apple-touch-icon)" href="[^"]*">', "", html)
        deferred = []

        def script(match: re.Match[str]) -> str:
            name = match.group(1)
            if name == "local-auth.js":
                return ""
            # The standalone page already carries the editor bundle; inline it rather than loading another script URL.
            if name == "editor-loader.js":
                name = "prompt-editor.js"
            if name not in known:
                raise SystemExit(f"{file} loads {name}, which the build has no copy of")  # noqa: B023 (runs within this pass of the loop)
            if match.group(2):
                deferred.append(f"<script>@@DEMO:{name}@@</script>")  # noqa: B023 (runs within this pass of the loop)
                return ""
            return f"\n  <script>@@DEMO:{name}@@</script>"

        html = LINK.sub(lambda match: f"\n  <style>@@DEMO:{match.group(1)}@@</style>", html)
        html = SCRIPT.sub(script, html)
        html = html.replace("<head>", "<head>\n  <script>@@DEMO:boot@@</script><script>@@DEMO:demo-child.js@@</script>", 1)
        html = html.replace("</body>", "\n".join(deferred) + "\n</body>", 1)
        if "/static/" in html:
            raise SystemExit(f"{file} still points at /static/ after the build replaced its files")
        made[view] = html
    return made


def logos(fixtures: dict[str, Any]) -> dict[str, str]:
    found = {}
    for path, reply in fixtures.items():
        match = re.fullmatch(r"/api/projects/([^/]+)/logo\.svg", path)
        if match:
            found[match.group(1)] = data_uri(reply["ct"], reply["t"].encode())
    return found


def notices() -> str:
    """The licence notices of the third-party code and fonts the page carries, one after another.

    The editor's libraries (MIT) and the fonts (SIL Open Font License) ask that every copy carry their notices.
    Cockpit serves these files beside the code; the demo is one file, so it carries them inside.
    """
    fonts = STATIC / "fonts"
    files = sorted(STATIC.glob("*.LICENSE.txt")) + sorted(fonts.glob("LICENSE-*.txt"))
    for name in UNTOUCHED:
        if STATIC / name.replace(".js", ".LICENSE.txt") not in files:
            raise SystemExit(f"{name} is embedded but has no notices file beside it")
    for font in fonts.glob("*.woff2"):
        if fonts / f"LICENSE-{font.name.split('-latin')[0]}.txt" not in files:
            raise SystemExit(f"{font.name} is embedded but its family has no LICENSE file in {fonts}")
    return script_text("notices", "\n\n".join(f"===== {path.name}\n\n{path.read_text().strip()}" for path in files))


REPO_URL = "https://github.com/fela/cockpit"
GITHUB_MARK = '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.27.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z"/></svg>'
RESTART_ICON = '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" d="M2.5 8a5.5 5.5 0 1 0 1.7-3.97M2.5 2.5v3h3"/></svg>'


def page(data: dict[str, Any]) -> str:
    """The demo: its data, then the shell that shows the pages and answers them."""
    blob = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")
    shell = script_text("demo-shell.js", (DEMO / "demo-shell.js").read_text())
    return f"""<!doctype html>
<!-- The code editor's libraries and the fonts in this page are other people's work. Their licences are in the
     element with id "third-party-notices" at the end of this file. -->
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Cockpit demo</title>
<link rel="icon" href="{data["icon"]}" type="image/svg+xml">
<style>
  :root {{ color-scheme: light dark; --demo-bg: #f8f7f4; --demo-ink: #24211d; --demo-band: #ece6d8; --demo-edge: #d8d0bd; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --demo-bg: #141210; --demo-ink: #ece8e1; --demo-band: #2a251d; --demo-edge: #40382c; }} }}
  html, body {{ margin: 0; height: 100%; background: var(--demo-bg); color: var(--demo-ink); }}
  body {{ display: flex; flex-direction: column; font: 14px/1.4 system-ui, sans-serif; }}
  #demo-band {{ display: flex; align-items: center; gap: 12px; padding: 0 16px; height: 52px; flex: none; box-sizing: border-box;
    background: #1b1916; color: #f3efe8; border-bottom: 1px solid #000;
    font: 14px/1.3 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }}
  #demo-band .brand {{ display: flex; align-items: center; gap: 9px; color: inherit; text-decoration: none; flex: none; }}
  #demo-band .brand img {{ display: block; border-radius: 7px; box-shadow: 0 1px 2px rgb(0 0 0 / .18); }}
  #demo-band .name {{ font-weight: 650; font-size: 16px; letter-spacing: -0.01em; }}
  #demo-band .pill {{ flex: none; font-size: 11px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase;
    padding: 3px 8px; border-radius: 999px; background: linear-gradient(#ffbe5c, #ef9f2f); color: #2a1a04; }}
  #demo-band .note {{ margin: 0 0 0 4px; flex: 1; min-width: 0; color: #c9c1b4; font-size: 13px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  #demo-band .note span {{ color: #8f877b; }}
  #demo-band .gh, #demo-band button {{ flex: none; display: inline-flex; align-items: center; gap: 7px; height: 32px;
    box-sizing: border-box; padding: 0 12px; border-radius: 8px; font-family: inherit; font-size: 13px; font-weight: 500;
    line-height: 1; cursor: pointer; text-decoration: none; transition: background .15s, border-color .15s, color .15s; }}
  #demo-band .gh {{ color: #f3efe8; background: transparent; border: 1px solid #4a443b; }}
  #demo-band .gh:hover {{ background: #2c2925; }}
  #demo-band button {{ color: #2a1a04; background: #ffb44e; border: 1px solid #ffb44e; }}
  #demo-band button:hover {{ background: #ffc574; }}
  #demo-band :is(a, button):focus-visible {{ outline: 2px solid #ef9f2f; outline-offset: 2px; }}
  #demo-band button[hidden] {{ display: none; }}
  #demo-back {{ font-weight: 600; }}
  @media (max-width: 1180px) {{ #demo-band .note span {{ display: none; }} }}
  @media (max-width: 600px) {{
    #demo-band {{ gap: 8px; padding: 0 12px; height: 50px; }}
    #demo-band .note {{ display: none; }}
    #demo-band .gh span {{ display: none; }}
    #demo-band .gh {{ padding: 0 9px; margin-left: auto; }}
  }}
  @media (max-width: 360px) {{ #demo-band .pill {{ display: none; }} }}
  #demo-band:has(#demo-back:not([hidden])) .pill {{ display: none; }}
  @media (max-width: 600px) {{ #demo-band:has(#demo-back:not([hidden])) .name {{ display: none; }} }}
  #demo-view {{ flex: 1; width: 100%; border: 0; display: block; background: var(--demo-bg); }}
  #demo-toast {{ position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%); max-width: min(520px, calc(100% - 32px));
    padding: 10px 14px; border-radius: 8px; background: var(--demo-ink); color: var(--demo-bg); font-size: 13px;
    box-shadow: 0 4px 16px rgb(0 0 0 / 0.2); opacity: 0; pointer-events: none; transition: opacity 0.2s; z-index: 10; }}
  #demo-toast.shown {{ opacity: 1; }}
</style>
</head>
<body>
<div id="demo-band" role="banner">
  <button type="button" id="demo-back" hidden>&larr; Back</button>
  <a class="brand" href="{REPO_URL}"><img src="{data["icon"]}" alt="" width="26" height="26"><span class="name">Cockpit</span></a>
  <span class="pill">Demo</span>
  <p class="note">One inbox for directing many AI coding agents. <span>Invented work, no AI runs, nothing leaves your browser.</span></p>
  <a class="gh" href="{REPO_URL}">{GITHUB_MARK}<span>View on GitHub</span></a>
  <button type="button" id="demo-reset">{RESTART_ICON}<span>Start over</span></button>
</div>
<iframe id="demo-view" title="Cockpit" sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox allow-downloads"
  allow="clipboard-write"></iframe>
<div id="demo-toast" role="status" aria-live="polite"></div>
<script type="application/json" id="demo-data">{blob}</script>
<script>{shell}</script>
<script type="text/plain" id="third-party-notices">
{notices()}
</script>
</body>
</html>
"""


def assemble(played: Any, fixtures: dict[str, Any], base: dict[str, Any], maxes: dict[str, int]) -> str:
    known = assets()
    known["demo-child.js"] = script_text("demo-child.js", (DEMO / "demo-child.js").read_text())
    data = {
        "origin": ORIGIN,
        "captureNow": base["now"],
        "icon": data_uri("image/svg+xml", (STATIC / "icon.svg").read_bytes()),
        "assets": known,
        "views": views(known),
        "base": base,
        "fixtures": fixtures,
        "played": played,
        "maxes": maxes,
        "logos": logos(fixtures),
    }
    return page(json.loads(scrub(json.dumps(data))))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(DEMO / "dist" / "cockpit-demo.html"))
    parser.add_argument("--state", help="also write the captured /api/state here, for a look")
    args = parser.parse_args()
    patch_clock()
    try:
        w = world()
        recorder = Recorder(w)
        played = placeholders(recorder.scenarios(), recorder.maxes)
        print("  base ids:", {table: top for table, top in recorder.maxes.items() if top})
        print("  id fields without a table:", sorted(loose_ids(played)))
        if recorder.w.state() != recorder.base | {"now": recorder.w.state()["now"]}:
            after = recorder.w.state()
            differ = [key for key in recorder.base if key != "now" and recorder.base[key] != after.get(key)]
            print("  restore differs from the seeded world in:", differ)
        fixtures = capture(w, recorder.base)
        print("  captured", len(fixtures), "replies;", sum(len(json.dumps(one)) for one in fixtures.values()) // 1024, "KB;",
              "emergency", fixtures["/api/emergency"]["status"])
        if args.state:
            Path(args.state).with_name("demo-fixtures.json").write_text(scrub(json.dumps(fixtures, indent=1)))
            Path(args.state).write_text(scrub(json.dumps(recorder.base, indent=1)))
            Path(args.state).with_name("demo-played.json").write_text(scrub(json.dumps(played, indent=1)))
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        html = assemble(played, fixtures, recorder.base, recorder.maxes)
        out.write_text(html)
        print(f"  wrote {out} ({len(html.encode()) // 1024} KB)")
    finally:
        shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    main()
