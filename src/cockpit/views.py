"""Read-only page assembly, isolated from the server's background transcript parsers."""

from __future__ import annotations

import logging
import multiprocessing
import queue
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from logging.handlers import QueueHandler
from pathlib import Path
from typing import Any

from cockpit import config, parts, sessions, store

log = logging.getLogger(__name__)

STOP_SECONDS = 2
READ_SECONDS = 30


@dataclass
class View:
    state: dict[str, Any]
    usages: dict[str, sessions.Usage]


class ReadTimeout(Exception):
    """A page worker did not finish within the request's time limit."""


Reader = Callable[[Path, str, list[sessions.Session], list[str], Path, set[int]], View]


def read(
    db_path: Path, me: str, listing: list[sessions.Session], warnings: list[str], checkouts: Path, opened: set[int],
) -> View:
    # Requests retain their own write connection. Workers can only read the database.
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        state = store.page_state(conn, me, listing, warnings, checkouts, opened)
    finally:
        conn.close()
    usages = sessions.transcript_usages([
        row["session_id"] for row in state["sessions"] if row.get("provider") != "codex"
    ])
    return View(state, usages)


def _read_in_worker(
    db_path: Path, me: str, listing: list[sessions.Session], warnings: list[str], checkouts: Path, opened: set[int],
) -> View:
    # The server rewrites the setup when the user changes their time zone; reading it again costs about a millisecond.
    config.setup.cache_clear()
    return read(db_path, me, listing, warnings, checkouts, opened)


def _configure(setup_path: Path, package: str, projects: Path, logs: multiprocessing.Queue) -> None:
    config.SETUP_PATH = setup_path
    parts.PACKAGE = package
    sessions.PROJECTS_DIR = projects
    root = logging.getLogger()
    root.addHandler(QueueHandler(logs))
    root.setLevel(logging.INFO)


class _ServerLog:
    """Logs each record the workers send through the server's logger of the same name, on a thread of its own.

    Stopping never writes to the queue, so a worker killed while writing to it cannot hold the server's shutdown."""

    def __init__(self, records: multiprocessing.Queue) -> None:
        self.records = records
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name="page worker logs", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while True:
            try:
                record = self.records.get(timeout=0.5)
            except queue.Empty:
                if self.stopping.is_set():
                    return
                continue
            logging.getLogger(record.name).handle(record)

    def stop(self) -> None:
        self.stopping.set()
        self.thread.join(timeout=STOP_SECONDS)
        if self.thread.is_alive():
            log.error("page worker logs were still arriving %s s after the server began to stop; the rest are lost",
                      STOP_SECONDS)


class Pool:
    def __init__(self, setup_path: Path, package: str, projects: Path) -> None:
        context = multiprocessing.get_context("spawn")
        records = context.Queue()
        # This also logs what a worker records after its read timed out or failed.
        self.server_log = _ServerLog(records)
        self.executor = ProcessPoolExecutor(
            max_workers=2, mp_context=context, initializer=_configure,
            initargs=(setup_path, package, projects, records),
        )

    def read(
        self, db_path: Path, me: str, listing: list[sessions.Session], warnings: list[str], checkouts: Path,
        opened: set[int],
    ) -> View:
        future = self.executor.submit(_read_in_worker, db_path, me, listing, warnings, checkouts, opened)
        try:
            return future.result(timeout=READ_SECONDS)
        except TimeoutError as exc:
            # A worker can also raise TimeoutError itself; preserve that failure rather than calling it a slow read.
            if future.done():
                return future.result()
            future.cancel()
            raise ReadTimeout(f"building the page took longer than {READ_SECONDS} seconds; try again") from exc

    def stop(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.server_log.stop()
