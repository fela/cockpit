"""The wake poll (server.wake_sweeper_forever) sends what it found due even when a later part of the poll fails, and
the background polls log what a loaded host cut short as a warning."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

import pytest

from cockpit import config, delivery, history, moves, server, sessions, store
from tests.conftest import World


class Stop(BaseException):
    """Ends the poll's endless loop after one round."""


class Recorder:
    """A deliverer that runs a counted thread's work when started, so the test sees what the poll dispatched."""

    def __init__(self) -> None:
        self.sent: list[int] = []
        self.started: list[str] = []

    def send(self, message_id: int) -> None:
        self.sent.append(message_id)

    def start(self, name: str, target: Any, *args: Any) -> None:
        self.started.append(name)
        target(*args)

    def lookup(self, session_id: str) -> None:
        return None

    def places_due(self, conn: Any) -> list[int]:
        return []


def test_messages_found_due_go_out_when_a_later_part_of_the_poll_fails(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Take back wait that ended is cleared in its own commit; were its send dropped with the poll, nothing would
    send the message again until a restart failed it."""
    monkeypatch.setattr(config, "DB_PATH", world.db_path)
    monkeypatch.setattr(server, "finish_withdrawals", lambda: None)
    monkeypatch.setattr(server, "launch_briefing_once", lambda deliverer: None)
    monkeypatch.setattr(server, "launch_projects_tidy_once", lambda deliverer: None)
    monkeypatch.setattr(delivery, "take_back_due", lambda conn, now: [42])

    def broken(conn: Any) -> None:
        raise RuntimeError("release failed")

    def sleep(seconds: float) -> None:
        raise Stop

    monkeypatch.setattr(store, "release_held", broken)
    monkeypatch.setattr(server.time, "sleep", sleep)
    recorder = Recorder()
    with pytest.raises(Stop):
        server.wake_sweeper_forever(recorder, world.cache, world.spend_index)  # ty: ignore[invalid-argument-type]
    assert recorder.sent == [42] and recorder.started == ["send-42"]


def test_a_waiting_move_counts_as_a_delivery_under_way(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """A move queues its message to the session before it sends it; a restart between the two failed the message
    at the next start, unless the stop waits for the move too (SPEC MSG-5)."""
    monkeypatch.setattr(config, "DB_PATH", world.db_path)
    monkeypatch.setattr(server, "finish_withdrawals", lambda: None)
    monkeypatch.setattr(server, "launch_briefing_once", lambda deliverer: None)
    monkeypatch.setattr(server, "launch_projects_tidy_once", lambda deliverer: None)
    waiting = {"session_id": "s-1", "account": "work", "actor": "user", "asked_at": "2026-01-30T16:00:00Z"}
    monkeypatch.setattr(moves, "waits_due", lambda listed, db_path: [waiting])
    moved: list[dict[str, Any]] = []
    monkeypatch.setattr(server, "move_waiting", lambda deliverer, session_cache, due: moved.append(due))

    def sleep(seconds: float) -> None:
        raise Stop

    monkeypatch.setattr(server.time, "sleep", sleep)
    recorder = Recorder()
    with pytest.raises(Stop):
        server.wake_sweeper_forever(recorder, world.cache, world.spend_index)  # ty: ignore[invalid-argument-type]
    assert moved == [waiting] and "move-s-1" in recorder.started


def a_poll_that_logs(world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, deliverer: Any) -> list:
    """The (level, words before the colon) of what one wake poll logged."""
    monkeypatch.setattr(config, "DB_PATH", world.db_path)
    monkeypatch.setattr(server, "finish_withdrawals", lambda: None)
    monkeypatch.setattr(server, "launch_briefing_once", lambda deliverer: None)
    monkeypatch.setattr(server, "launch_projects_tidy_once", lambda deliverer: None)

    def sleep(seconds: float) -> None:
        raise Stop

    monkeypatch.setattr(server.time, "sleep", sleep)
    with pytest.raises(Stop):
        server.wake_sweeper_forever(deliverer, world.cache, world.spend_index)
    return [(r.levelno, r.message.split(":")[0]) for r in caplog.records if r.name == "cockpit.server"]


@pytest.mark.parametrize(
    ("failure", "logged"),
    [
        (sqlite3.OperationalError("database is locked"), (logging.WARNING, "waking the sweeper stopped short")),
        (sqlite3.OperationalError("disk I/O error"), (logging.ERROR, "waking the sweeper failed")),
    ],
)
def test_a_poll_a_locked_database_cut_short_is_a_warning(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: Exception, logged: tuple
) -> None:
    """A locked database fails requests with a 503 that is logged; the next poll tries again. A broken one is an
    error."""

    def broken(conn: Any) -> None:
        raise failure

    monkeypatch.setattr(store, "release_held", broken)
    assert a_poll_that_logs(world, monkeypatch, caplog, Recorder()) == [logged]


def test_a_sweep_launch_a_timed_out_session_listing_cut_short_is_a_warning(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The sweep launch asks whether its session still runs; list_sessions logged the listing that timed out."""

    class TimedOut(Recorder):
        def lookup(self, session_id: str) -> None:
            raise sessions.LookupFailed("Listing the work account's Claude sessions failed: timed out after 30 seconds")

    def pause_once(conn: Any, cwd: Any, exists: Any) -> None:
        exists("the sweeper's session")

    monkeypatch.setattr(server.sweeper, "pause_once", pause_once)
    logged = a_poll_that_logs(world, monkeypatch, caplog, TimedOut())
    assert [each for each in logged if each[1].startswith("starting a sweep")] == [
        (logging.WARNING, "starting a sweep stopped short")
    ]


@pytest.mark.parametrize(
    ("failure", "level"),
    [(sqlite3.OperationalError("database is locked"), logging.WARNING), (OSError("no such folder"), logging.ERROR)],
)
def test_a_turn_read_a_locked_database_cut_short_is_a_warning(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: Exception, level: int
) -> None:
    monkeypatch.setattr(config, "DB_PATH", world.db_path)

    def broken(conn: Any) -> int:
        raise failure

    def sleep(seconds: float) -> None:
        raise Stop

    monkeypatch.setattr(history, "read_all_turns", broken)
    monkeypatch.setattr(server.time, "sleep", sleep)
    with pytest.raises(Stop):
        server.read_turns_forever()
    assert [r.levelno for r in caplog.records if r.name == "cockpit.server"] == [level]
