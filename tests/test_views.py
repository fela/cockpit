from __future__ import annotations

import logging
import os
import signal
import sqlite3
import struct
import time
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from cockpit import config, db, parts, sessions, views, zones
from cockpit.app import create_app
from tests.conftest import ORIGIN, SESSION_A
from tests.test_api import put_item, put_step
from tests.test_sessions import assistant, text
from tests.test_spend import transcript
from tests.test_zones import own_setup  # noqa: F401  # A fixture.


def test_spawned_page_reads_match_inline_reads_and_observe_later_changes(world):
    item = put_item(world, sessions=[SESSION_A])
    put_step(world)
    world.cache.sessions = [replace(world.cache.sessions[0], pid=123, job_state="working", tempo="blocked")]
    transcript(sessions.PROJECTS_DIR, SESSION_A, [assistant("answer", text("Pick A or B?"))])
    args = (world.db_path, "user", world.cache.sessions, ["a warning"], world.settings.checkouts_dir, set())
    expected = views.read(*args)
    pool = views.Pool(config.SETUP_PATH, parts.PACKAGE, sessions.PROJECTS_DIR)
    try:
        actual = pool.read(*args)
        for state in (expected.state, actual.state):
            state.pop("now")
        assert actual == expected
        assert actual.state["waiting_sessions"][0]["last_message"] == "Pick A or B?"

        # The real HTTP route uses the same worker after a write, with fresh database and transcript data.
        app = create_app(world.settings, world.deliverer, world.cache, world.spend_index, world.codex_index, [], pool.read)
        client = TestClient(app, base_url=ORIGIN)
        client.cookies = world.client.cookies
        put_item(world, title="Changed title")
        transcript(sessions.PROJECTS_DIR, SESSION_A, [assistant("next", text("New answer"))])
        response = client.get("/api/state")
        assert response.status_code == 200, response.text
        assert next(row for row in response.json()["items"] if row["id"] == item["id"])["title"] == "Changed title"
        assert response.json()["waiting_sessions"][0]["last_message"] == "New answer"
    finally:
        pool.stop()


def test_worker_loss_surfaces_and_the_pool_can_still_stop(world):
    pool = views.Pool(config.SETUP_PATH, parts.PACKAGE, sessions.PROJECTS_DIR)
    try:
        worker_pid = pool.executor.submit(os.getpid).result(timeout=10)
        assert worker_pid != os.getpid()
        os.kill(worker_pid, signal.SIGKILL)
        with pytest.raises(BrokenProcessPool):
            pool.read(world.db_path, "user", [], [], world.settings.checkouts_dir, set())
    finally:
        pool.stop()


def test_read_only_worker_failure_reaches_the_caller(world, tmp_path):
    pool = views.Pool(config.SETUP_PATH, parts.PACKAGE, sessions.PROJECTS_DIR)
    try:
        with pytest.raises(sqlite3.OperationalError, match="unable to open database"):
            pool.read(tmp_path / "missing.db", "user", [], [], world.settings.checkouts_dir, set())
        assert not (tmp_path / "missing.db").exists()
    finally:
        pool.stop()


def test_a_worker_log_reaches_the_server_log(world, caplog):
    put_item(world, sessions=[SESSION_A])
    folder = sessions.PROJECTS_DIR / "-loop"
    folder.mkdir(parents=True, exist_ok=True)
    loop = folder / f"{SESSION_A}.jsonl"
    loop.symlink_to(loop)  # Reading its size fails with "too many levels of symbolic links", which sessions logs.
    listing = [replace(world.cache.sessions[0], pid=123, job_state="working")]
    pool = views.Pool(config.SETUP_PATH, parts.PACKAGE, sessions.PROJECTS_DIR)
    with caplog.at_level(logging.ERROR, logger="cockpit.sessions"):
        try:
            pool.read(world.db_path, "user", listing, [], world.settings.checkouts_dir, set())
        finally:
            pool.stop()  # Also waits for the server to log what the workers sent.
    assert [(r.name, r.levelno) for r in caplog.records if "could not read the transcript" in r.getMessage()] == [
        ("cockpit.sessions", logging.ERROR),
    ]

def test_a_new_time_zone_reaches_running_workers(world, own_setup):  # noqa: F811
    args = (world.db_path, "user", [], [], world.settings.checkouts_dir, set())
    pool = views.Pool(config.SETUP_PATH, parts.PACKAGE, sessions.PROJECTS_DIR)
    try:
        # Both workers start and read the setup before the change.
        for future in [pool.executor.submit(views._read_in_worker, *args) for _ in range(4)]:
            assert future.result(timeout=30).state["local_now"].endswith("Etc/GMT-7")
        conn = db.connect(world.db_path)
        try:
            zones.put(conn, own_setup, "Asia/Tokyo", datetime.now(UTC))
        finally:
            conn.close()
        for _ in range(4):
            assert pool.read(*args).state["local_now"].endswith("Asia/Tokyo")
    finally:
        pool.stop()


def test_a_worker_killed_while_logging_cannot_hold_the_server_stop(world, caplog):
    pool = views.Pool(config.SETUP_PATH, parts.PACKAGE, sessions.PROJECTS_DIR)
    pool.read(world.db_path, "user", [], [], world.settings.checkouts_dir, set())
    records = pool.server_log.records
    # What a worker killed mid-write leaves: the queue's write lock held, and a record's length with no record.
    records._wlock.acquire()
    os.write(records._writer.fileno(), struct.pack("!i", 100))
    started = time.monotonic()
    with caplog.at_level(logging.ERROR, logger="cockpit.views"):
        pool.stop()
    assert time.monotonic() - started < views.STOP_SECONDS + 5
    assert "page worker logs were still arriving" in caplog.text


def test_a_page_read_timeout_leaves_later_reads_working(world, monkeypatch):
    pool = views.Pool(config.SETUP_PATH, parts.PACKAGE, sessions.PROJECTS_DIR)
    args = (world.db_path, "user", [], [], world.settings.checkouts_dir, set())
    try:
        # Keep both workers busy so this request runs out of time waiting for its turn.
        busy = [pool.executor.submit(time.sleep, 0.25) for _ in range(2)]
        with monkeypatch.context() as changed:
            changed.setattr(views, "READ_SECONDS", 0.01)
            with pytest.raises(views.ReadTimeout, match="longer than 0.01 seconds"):
                pool.read(*args)
        for future in busy:
            future.result(timeout=10)
        assert pool.read(*args).state["items"] == []
    finally:
        pool.stop()


def test_a_timeout_error_from_worker_code_keeps_its_original_cause(world, monkeypatch):
    pool = views.Pool(config.SETUP_PATH, parts.PACKAGE, sessions.PROJECTS_DIR)
    failed = Future()
    failure = TimeoutError("transcript read failed")
    failed.set_exception(failure)
    monkeypatch.setattr(pool.executor, "submit", lambda *args: failed)
    try:
        with pytest.raises(TimeoutError) as caught:
            pool.read(world.db_path, "user", [], [], world.settings.checkouts_dir, set())
        assert caught.value is failure
    finally:
        pool.stop()


def test_a_page_read_timeout_returns_retryable_service_unavailable_and_logs(world, caplog):
    reads = []

    def read(*args):
        reads.append(args)
        if len(reads) == 1:
            raise views.ReadTimeout("building the page took longer than 30 seconds; try again")
        return views.read(*args)

    app = create_app(world.settings, world.deliverer, world.cache, world.spend_index, world.codex_index, [], read)
    with TestClient(app, base_url=ORIGIN) as client:
        # The timeout handler leaves the route's sign-in requirement in place.
        assert client.get("/api/state").status_code == 401
        assert reads == []
        client.cookies = world.client.cookies
        with caplog.at_level(logging.ERROR, logger="cockpit.app"):
            response = client.get("/api/state")
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "1"
        assert "longer than 30 seconds" in response.json()["detail"]
        assert "page state read timed out" in caplog.text
        assert client.get("/api/state").status_code == 200
