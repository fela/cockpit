"""The page's actions go through a connection that drops (SPEC CODE-5).

The page names each action it sends with a fresh id in the `X-Cockpit-Action` header and sends it again
until an answer arrives. A try can reach Cockpit and do its work while its answer is lost on the way back,
so Cockpit keeps each action's answer under its id: a repeat gets that answer, and the action happens once.
A repeat that arrives while the first try still runs waits for it. A refusal (4xx) or a failure (5xx)
changed nothing, so its record goes and its repeat runs again.

Cockpit records an action's id before its first try runs, so no repeat ever runs it twice: when Cockpit
stopped during that try (a restart, a failed write of the answer), whether the action happened is unknown,
and a repeat gets a refusal that says so rather than a second run.

The work of a try that reached Cockpit runs to its end even when the connection drops, and its answer goes
out as soon as the app sent it whole: work the app does after its answer (background tasks) does not hold
it, and a failure there changes the answer no more.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cockpit import db

log = logging.getLogger(__name__)

HEADER = b"x-cockpit-action"
ACTION_ID = re.compile(rb"[A-Za-z0-9-]{8,64}")
# Any spelling of the page's id, since the route also takes "+1" or " 1" as page 1.
PUBLISH_PATH = re.compile(r"/api/item-pages/[^/]+/(public(/new-link)?|sharing)/?")
# The Mac's new key and the Terminal line that carries it (SPEC TRACK-9).
MAC_KEY_PATH = re.compile(r"/api/activity/key/?")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# A page sends an action again for as long as it stays open; a month covers a page left open on a laptop
# that slept through a holiday.
KEEP = timedelta(days=30)


@dataclass(frozen=True)
class Answer:
    status: int
    content_type: str
    body: bytes
    # The app's own headers on the first try; a repeat gets only the content type.
    headers: tuple[tuple[bytes, bytes], ...] = ()


def refusal(status: int, detail: str) -> Answer:
    return Answer(status, "application/json", b'{"detail": "%s"}' % detail.encode())


ANOTHER_ACTION = refusal(400, "this action id was already used for another action")
# `cut_off`: the page tells this 409 from a refusal it may pass over (a timer's pause that found no timer).
CUT_OFF = Answer(
    409,
    "application/json",
    b'{"detail": "Cockpit stopped while it did this action, so it may or may not have happened: check the page'
    b' before you press again", "cut_off": true}',
)
FAILED = refusal(500, "Cockpit failed on this action; see its journal")
UNANSWERED = refusal(503, "the action did not finish; send it again")
BUSY = refusal(503, "Cockpit's database is busy; send it again")


def begin(conn: sqlite3.Connection, action_id: str, method: str, path: str) -> Answer | None:
    """The answer for an action id Cockpit has seen; otherwise records that its first try starts, and None."""
    now = datetime.now(UTC)
    with db.transaction(conn):
        record = conn.execute("SELECT * FROM page_actions WHERE id = ?", (action_id,)).fetchone()
        if record is None:
            conn.execute(
                "INSERT INTO page_actions (id, method, path, at) VALUES (?, ?, ?, ?)",
                (action_id, method, path, now.strftime("%Y-%m-%dT%H:%M:%SZ")),
            )
            conn.execute("DELETE FROM page_actions WHERE at < ?", ((now - KEEP).strftime("%Y-%m-%dT%H:%M:%SZ"),))
            return None
    if (record["method"], record["path"]) != (method, path):
        return ANOTHER_ACTION
    if record["status"] is None:
        return CUT_OFF
    return Answer(record["status"], record["content_type"], record["body"])


def settle(conn: sqlite3.Connection, action_id: str, answer: Answer) -> None:
    """Keeps a success's answer for its repeats; a refusal or failure changed nothing, so its record goes."""
    if 200 <= answer.status < 300:
        conn.execute(
            "UPDATE page_actions SET status = ?, content_type = ?, body = ?, at = ? WHERE id = ?",
            (answer.status, answer.content_type, answer.body, db.now_iso(), action_id),
        )
    else:
        conn.execute("DELETE FROM page_actions WHERE id = ?", (action_id,))


@dataclass
class Running:
    """An action whose first try runs now: a repeat of it waits for `answer`."""

    method: str
    path: str
    answer: asyncio.Future[Answer]


@dataclass
class Once:
    """ASGI middleware, inside the sign-in guard: each of the signed-in user's actions happens once."""

    app: ASGIApp
    db_path: Path
    # Extra routes whose responses an install keeps out of the action history.
    unrecorded_paths: list[re.Pattern[str]]
    running: dict[str, Running] = field(default_factory=dict)
    # The app's runs of first tries, held until they end, background tasks and all.
    runs: set[asyncio.Task[None]] = field(default_factory=set)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        action_id = self._action_id(scope)
        if action_id is None:
            await self.app(scope, receive, send)
            return
        method, path = scope["method"], scope["path"]
        entry = self.running.get(action_id)
        if entry is None:
            # Own the complete submission before waiting on the database or detaching work from this request.
            # The browser's timeout can close its receive channel while the database still waits.
            try:
                body = await Request(scope, receive).body()
            except ClientDisconnect:
                await send({"type": "http.response.start", "status": UNANSWERED.status,
                            "headers": [(b"content-type", UNANSWERED.content_type.encode())]})
                await send({"type": "http.response.body", "body": UNANSWERED.body})
                return

            consumed = False

            async def submission() -> Message:
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": body, "more_body": False}
                # A response may listen for disconnects. The detached action lives through the client's disconnect;
                # its response cancels that listener when it finishes.
                return await asyncio.Future()

            # Another complete upload may have registered this id while this request read its body.
            # Lookup and registration have no await between, so only one try starts the action.
            entry = self.running.get(action_id)
            if entry is None:
                entry = Running(method, path, asyncio.ensure_future(self._answer(scope, submission, action_id, method, path)))
                self.running[action_id] = entry
                entry.answer.add_done_callback(lambda _: self._forget(action_id, entry))
        if (entry.method, entry.path) != (method, path):
            answer = ANOTHER_ACTION
        else:
            # A dropped connection cancels this request; the shielded try still finishes and records its answer.
            answer = await asyncio.shield(entry.answer)
        headers = list(answer.headers) or [(b"content-type", answer.content_type.encode())]
        await send({"type": "http.response.start", "status": answer.status, "headers": headers})
        await send({"type": "http.response.body", "body": answer.body})

    def _action_id(self, scope: Scope) -> str | None:
        if scope["type"] != "http" or scope["method"] in SAFE_METHODS:
            return None
        if scope.get("state", {}).get("me") != "user":
            return None
        # Making a page public, or giving it a new link, answers with its secret link, which this table would keep for
        # KEEP. Making it public again is safe, because a public page keeps its link; a New link sent twice just
        # replaces the link twice, and the second answer holds the one that works.
        if PUBLISH_PATH.fullmatch(scope["path"]):
            return None
        if any(pattern.fullmatch(scope["path"]) for pattern in self.unrecorded_paths):
            return None
        # Sending a new Mac key twice replaces it twice; the second answer holds the one that works.
        if MAC_KEY_PATH.fullmatch(scope["path"]):
            return None
        value = dict(scope["headers"]).get(HEADER)
        if value is None or not ACTION_ID.fullmatch(value):
            return None
        return value.decode()

    def _forget(self, action_id: str, entry: Running) -> None:
        if self.running.get(action_id) is entry:
            del self.running[action_id]

    async def _answer(self, scope: Scope, receive: Receive, action_id: str, method: str, path: str) -> Answer:
        """The answer for the action: the recorded one, or its first try's once the app sent it whole."""
        try:
            earlier = await run_in_threadpool(self._begin, action_id, method, path)
        except sqlite3.OperationalError:
            # The database stayed locked past its busy timeout; nothing ran, so the page sends it again.
            log.warning("action %s %s (%s) waits: the database is busy", method, path, action_id, exc_info=True)
            return BUSY
        if earlier is not None:
            return earlier
        answered: asyncio.Future[Answer] = asyncio.get_running_loop().create_future()
        start: Message | None = None
        body = bytearray()

        async def finish(answer: Answer) -> None:
            try:
                await run_in_threadpool(self._settle, action_id, answer)
            finally:
                if not answered.done():
                    answered.set_result(answer)

        async def capture(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
            elif message["type"] == "http.response.body" and start is not None:
                body.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    headers = tuple(start.get("headers", []))
                    content_type = dict(headers).get(b"content-type", b"application/json").decode()
                    await finish(Answer(start["status"], content_type, bytes(body), headers))

        async def run() -> None:
            try:
                await self.app(scope, receive, capture)
            except Exception:
                # After the answer went whole (a background task failed), the action and its answer stand.
                log.exception("action %s %s (%s) failed", method, path, action_id)
                if not answered.done():
                    await finish(FAILED)
            else:
                if not answered.done():
                    await finish(UNANSWERED)

        task = asyncio.ensure_future(run())
        self.runs.add(task)
        task.add_done_callback(self.runs.discard)
        return await answered

    def _begin(self, action_id: str, method: str, path: str) -> Answer | None:
        conn = db.connect(self.db_path)
        try:
            return begin(conn, action_id, method, path)
        finally:
            conn.close()

    def _settle(self, action_id: str, answer: Answer) -> None:
        # The action happened (or failed) whatever becomes of its record: a failed write is logged, and a repeat
        # then finds the action unsettled and is refused rather than run twice.
        conn = db.connect(self.db_path)
        try:
            settle(conn, action_id, answer)
        except Exception:
            log.exception("could not record the answer to action %s", action_id)
        finally:
            conn.close()
