"""SPEC NEXT-17: earlier images fold away when a new step or a new reply on the same step starts a round."""

from __future__ import annotations

import pytest

from cockpit import db, store
from tests.conftest import SESSION_A, World
from tests.test_api import agent_client, put_item, put_step


def two_rounds(world: World) -> tuple[int, int]:
    """An item whose first step was answered and whose second is open."""
    put_item(world, key="note:rounds", title="Rounds")
    first = put_step(world, item_key="note:rounds", need="first", ask="Which layout?")["step"]["id"]
    done = agent_client(world).post(f"/api/steps/{first}/done", json={"note": "answered"}, headers=world.agent())
    assert done.status_code == 200, done.text
    second = put_step(world, item_key="note:rounds", need="second", ask="Ship it?")["step"]["id"]
    return first, second


def put_at(world: World, times: dict[int, str]) -> dict[int, str]:
    conn = db.connect(world.db_path)
    with conn:
        for step_id, at in times.items():
            conn.execute("UPDATE steps SET created_at = ? WHERE id = ?", (at, step_id))
    return store._round_starts(conn, list(times))


def test_a_step_s_round_begins_when_the_step_before_it_was_put(world: World) -> None:
    first, second = two_rounds(world)
    assert put_at(world, {first: "2026-11-12T10:00:00Z", second: "2026-11-12T11:00:00Z"}) == {second: "2026-11-12T10:00:00Z"}


def test_a_withdrawn_step_put_again_starts_a_later_round_under_its_old_id(world: World) -> None:
    # A withdrawn step put again keeps its id and takes a new time, so the lower id is the later step.
    first, second = two_rounds(world)
    assert put_at(world, {first: "2026-11-12T12:00:00Z", second: "2026-11-12T11:00:00Z"}) == {first: "2026-11-12T11:00:00Z"}


def test_steps_put_in_the_same_second_take_their_order_from_their_ids(world: World) -> None:
    first, second = two_rounds(world)
    assert put_at(world, {first: "2026-11-12T10:00:00Z", second: "2026-11-12T10:00:00Z"}) == {second: "2026-11-12T10:00:00Z"}


def replied_step(world: World, monkeypatch: pytest.MonkeyPatch) -> int:
    """The item's only step, followed by a user message that captures that step's exact version."""
    monkeypatch.setattr(db, "now_iso", lambda: "2026-11-12T10:00:00Z")
    put_item(world)
    step_id = put_step(world)["step"]["id"]
    monkeypatch.setattr(db, "now_iso", lambda: "2026-11-12T11:00:00Z")
    with db.connect(world.db_path) as conn:
        store.create_message(conn, "user", "Use the original layout", {"step_id": step_id, "session_id": SESSION_A},
                             world.settings.checkouts_dir)
    return step_id


def test_reply_on_the_only_step_starts_a_round_that_survives_further_edits(world: World, monkeypatch) -> None:
    step_id = replied_step(world, monkeypatch)
    with db.connect(world.db_path) as conn:
        assert store._round_starts(conn, [step_id]) == {}
        monkeypatch.setattr(db, "now_iso", lambda: "2026-11-12T12:00:00Z")
        put_step(world, body="The original layout is restored.")
        assert store._round_starts(conn, [step_id]) == {step_id: "2026-11-12T10:00:00Z"}
        monkeypatch.setattr(db, "now_iso", lambda: "2026-11-12T13:00:00Z")
        put_step(world, body="The original layout is restored and checked.")
        put_step(world, score=80, size="small")
        assert store._round_starts(conn, [step_id]) == {step_id: "2026-11-12T10:00:00Z"}


def test_ranking_and_identical_puts_do_not_count_as_a_reply(world: World, monkeypatch) -> None:
    step_id = replied_step(world, monkeypatch)
    put_step(world, score=80)
    put_step(world)
    with db.connect(world.db_path) as conn:
        assert store._round_starts(conn, [step_id]) == {}


def test_a_same_second_reply_uses_the_captured_version_id(world: World, monkeypatch) -> None:
    step_id = replied_step(world, monkeypatch)
    # The reply and new text share a timestamp, but the new version follows the version the message captured.
    put_step(world, body="The original layout is restored.")
    with db.connect(world.db_path) as conn:
        assert store._round_starts(conn, [step_id]) == {step_id: "2026-11-12T10:00:00Z"}


@pytest.mark.parametrize("state,dismissed", [("failed", None), ("sent", "2026-11-12T11:00:01Z")])
def test_failed_or_dismissed_messages_keep_the_existing_round(world: World, monkeypatch, state, dismissed) -> None:
    step_id = replied_step(world, monkeypatch)
    with db.connect(world.db_path) as conn:
        conn.execute("UPDATE messages SET state = ?, dismissed_at = ? WHERE step_id = ?", (state, dismissed, step_id))
    put_step(world, body="A routine update.")
    with db.connect(world.db_path) as conn:
        assert store._round_starts(conn, [step_id]) == {}


def test_another_reply_advances_to_the_version_it_answered(world: World, monkeypatch) -> None:
    step_id = replied_step(world, monkeypatch)
    put_step(world, body="The original layout is restored.")
    monkeypatch.setattr(db, "now_iso", lambda: "2026-11-12T12:00:00Z")
    with db.connect(world.db_path) as conn:
        store.create_message(conn, "user", "Now change the export", {"step_id": step_id, "session_id": SESSION_A},
                             world.settings.checkouts_dir)
        # Until the agent answers, the card retains its current images.
        assert store._round_starts(conn, [step_id]) == {step_id: "2026-11-12T10:00:00Z"}
    put_step(world, body="The export is updated.")
    with db.connect(world.db_path) as conn:
        assert store._round_starts(conn, [step_id]) == {step_id: "2026-11-12T11:00:00Z"}
