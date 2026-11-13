"""A burst of requests holds at most server.REQUEST_LIMIT database connections.

FastAPI opens a request's connection on one worker thread and runs the endpoint on another. While every worker is
busy, a new request still opens its connection and then waits, holding two files (the database and its write-ahead
log). Uvicorn's limit_concurrency answers requests past REQUEST_LIMIT with 503, so the waiting ones hold at most
that many connections and the server keeps free file descriptors.
"""

from __future__ import annotations

import socket
import sqlite3
import threading
import time
from urllib.parse import urlsplit

import anyio
import httpx
import uvicorn

from cockpit import auth, db, server
from cockpit.app import Conn
from tests.conftest import ORIGIN, World

# More than REQUEST_LIMIT, so some must be turned away.
REQUESTS = server.REQUEST_LIMIT + 72


def test_a_burst_holds_at_most_the_request_limit(world: World, monkeypatch) -> None:
    opened: list[sqlite3.Connection] = []
    real_connect = db.connect

    def connect(path):  # type: ignore[no-untyped-def]
        conn = real_connect(path)
        opened.append(conn)
        return conn

    monkeypatch.setattr(db, "connect", connect)

    def still_open() -> int:
        count = 0
        for conn in opened.copy():
            try:
                conn.total_changes  # noqa: B018 - raises once the connection is closed
            except sqlite3.ProgrammingError:
                continue
            count += 1
        return count

    release = threading.Event()
    running = 0
    running_lock = threading.Lock()

    def held(conn: Conn) -> dict[str, int]:
        nonlocal running
        with running_lock:
            running += 1
        conn.execute("SELECT 1").fetchone()
        release.wait(30)
        return {"ok": 1}

    app = world.client.app
    app.add_api_route("/api/held", held, methods=["GET"])
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    # As server.serve configures it, on a free local port.
    uvicorn_server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, proxy_headers=False, log_level="warning",
        limit_concurrency=server.REQUEST_LIMIT,
    ))
    thread = threading.Thread(target=uvicorn_server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not uvicorn_server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert uvicorn_server.started

    statuses: list[int] = []
    peak = 0

    async def main() -> None:
        nonlocal peak
        headers = {
            "host": urlsplit(ORIGIN).netloc,
            "cookie": f"{auth.SESSION_COOKIE}={world.client.cookies.get(auth.SESSION_COOKIE)}",
        }
        limits = httpx.Limits(max_connections=REQUESTS, max_keepalive_connections=0)
        client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=headers, limits=limits, timeout=60)

        async def get() -> None:
            statuses.append((await client.get("/api/held")).status_code)

        async with client, anyio.create_task_group() as group:
            # Uvicorn counts open connections against the limit when it reads a request. Started all at once, every
            # connection opens before any request is read, and all of them are turned away; a short gap lets the
            # first REQUEST_LIMIT in.
            for _ in range(REQUESTS):
                group.start_soon(get)
                await anyio.sleep(0.005)
            # The turned-away requests come back at once; the others wait on the busy workers.
            deadline = time.monotonic() + 20
            while len(statuses) < REQUESTS - server.REQUEST_LIMIT and time.monotonic() < deadline:
                await anyio.sleep(0.01)
            await anyio.sleep(0.5)
            peak = still_open()
            release.set()

    try:
        anyio.run(main)
    finally:
        release.set()
        uvicorn_server.should_exit = True
        thread.join(10)
    assert still_open() == 0
    assert 0 < peak <= server.REQUEST_LIMIT
    assert statuses.count(503) >= REQUESTS - server.REQUEST_LIMIT
    assert statuses.count(200) == running
