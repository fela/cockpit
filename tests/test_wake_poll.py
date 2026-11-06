"""The wake poll (server.wake_sweeper_forever) sends what it found due even when a later part of the poll fails."""

from __future__ import annotations

from typing import Any

import pytest

from cockpit import config, delivery, moves, server, store
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
        server.wake_sweeper_forever(recorder, world.cache)  # ty: ignore[invalid-argument-type]
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
        server.wake_sweeper_forever(recorder, world.cache)  # ty: ignore[invalid-argument-type]
    assert moved == [waiting] and "move-s-1" in recorder.started
