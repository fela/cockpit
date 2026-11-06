"""Isolated browser fixture: temporary database, fake sessions, no live user data or agents.

It stops within a second of the test that started it ending (parent_watch.py).
"""

from __future__ import annotations

import json
import socket
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from parent_watch import stop_with_parent, stopped

from cockpit import auth, config, db, sessions
from cockpit.app import create_app
from tests.conftest import COOKIE_KEY, EMAIL, SESSION_A, world


def main(work: Path) -> None:
    parent = stop_with_parent(work)
    try:
        serve(work)
    finally:
        stopped(work, parent)


def serve(work: Path) -> None:
    # Calling the fixture directly skips pytest's isolation fixtures.
    config.ACCOUNTS = (config.Account(name="personal", letter="P", folder=work / "accounts" / "claude",
                                     state=work / "accounts" / "claude.json", default=True),)
    config.CODEX_HOMES = (work / "codex",)
    config.DB_PATH = work / "configured.db"
    sessions.PROJECTS_DIR = work / "projects"
    fixture = world.__wrapped__(work)
    for at, title in enumerate(("Alpha list reminders", "Beta CSV export", "Gamma list sharing")):
        key = f"note:keyboard-{at}"
        # Alpha's summary holds a to-do's second ask, which the switcher finds on the item and its session.
        extra = {"summary": "Review it and make a skill for the session", "sessions": [SESSION_A]} if at == 0 else {}
        response = fixture.client.put("/api/items", json={"key": key, "title": title, **extra},
                                      headers=fixture.agent())
        response.raise_for_status()
        response = fixture.client.post("/api/steps", json={"item_key": key, "need": "review", "ask": f"Review {title}",
                                                          "session_id": SESSION_A, "score": 90 - at},
                                       headers=fixture.agent())
        response.raise_for_status()
    # A to-do whose agent's summary (agent_summary) holds a word nothing else has.
    todo = fixture.client.post("/api/items/add", json={"text": "Prepare a grocery list", "part": None, "pinned": False},
                               headers=fixture.user())
    todo.raise_for_status()
    fixture.client.put("/api/items", json={"key": todo.json()["key"], "summary": "Added zebra pasta to the grocery list"},
                       headers=fixture.agent()).raise_for_status()
    # Finished work on the same session: listed for a while, but it lends the session no words.
    fixture.client.put("/api/items", json={"key": "note:keyboard-done", "title": "Delta list archive",
                                           "summary": "The archived walrus supplies list", "sessions": [SESSION_A]},
                       headers=fixture.agent()).raise_for_status()
    with db.connect(fixture.db_path) as conn:
        conn.execute("UPDATE items SET stage = 'done', done_at = updated_at WHERE key = 'note:keyboard-done'")
        # The state lists items by `updated_at`, to the second: Alpha, Beta, Gamma in that order, however the clock
        # fell while they were made, as the switcher's tests expect.
        for at in range(3):
            conn.execute("UPDATE items SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?) WHERE key = ?",
                         (f"-{at} seconds", f"note:keyboard-{at}"))
    # Usage shows its figures once the transcripts were counted: none here, so at once.
    fixture.spend_index.refresh(datetime.now(UTC))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    settings = replace(fixture.settings, origin=origin)
    app = create_app(settings, fixture.deliverer, fixture.cache, fixture.spend_index, fixture.codex_index)
    cookie = auth.session_cookie_value(COOKIE_KEY, EMAIL, int(time.time()) + 3600)
    (work / "runtime.json").write_text(json.dumps({"origin": origin, "cookie": f"{auth.SESSION_COOKIE}={cookie}"}))
    uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[sock])


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
