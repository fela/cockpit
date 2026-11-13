"""SPEC NEXT-61: a card an agent rewrote after the user read it says so, with how long they read it and the agent's
line on what changed, until the user reads the new card, or writes or presses on the task. A glance is no read."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from cockpit import db, store
from tests.conftest import SESSION_A, World
from tests.test_api import run_sql
from tests.test_merge import _put_item, _put_step, _state_item, _step

KEY = "note:weekly-reminder"
NOTE = "The tests finished; the result is now at the top."


def _ago(seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read(world: World, step_id: int, start: str, end: str) -> None:
    run_sql(world, "INSERT INTO time_spans (step_id, started_at, ended_at, ended_by) VALUES (?, ?, ?, 'pause')",
            (step_id, start, end))


def _looked_at(world: World, step_id: int, seconds: int = 60) -> None:
    """The user read the card for `seconds`, ending ten minutes ago."""
    _read(world, step_id, _ago(600 + seconds), _ago(600))


def _card(world: World) -> tuple[dict[str, Any], dict[str, Any]]:
    item = _put_item(world, KEY, "Weekly reminder", sessions=[SESSION_A])
    return item, _step(world, KEY, "review", "send the weekly reminder?")


def _changes(world: World, item_id: int) -> list[tuple[int, str]]:
    return [(change["step_id"], change["note"]) for change in _state_item(world, item_id)["changes"]]


def test_a_rewrite_of_a_card_the_user_looked_at_shows_its_note(world: World) -> None:
    item, card = _card(world)
    _looked_at(world, card["id"])

    rewrite = _put_step(world, KEY, "review", "send the weekly reminder?", body="The tests pass.", changed=NOTE)

    assert rewrite.status_code == 200, rewrite.text
    assert _changes(world, item["id"]) == [(card["id"], NOTE)]
    # The note is about the rewrite, not a field of the step.
    assert "changed" not in rewrite.json()["step"]


def test_a_card_the_user_never_opened_or_a_new_score_shows_no_band(world: World) -> None:
    item, card = _card(world)
    assert _put_step(world, KEY, "review", "send the weekly reminder?", body="The tests pass.", changed=NOTE).status_code == 200
    assert _changes(world, item["id"]) == []

    _looked_at(world, card["id"])
    # The same text again, a new score or a new size is no rewrite the user reads.
    assert _put_step(world, KEY, "review", "send the weekly reminder?", body="The tests pass.", score=80, size="small").status_code == 200
    assert _changes(world, item["id"]) == []


def test_a_rewrite_without_a_note_is_refused_once_the_user_looked(world: World) -> None:
    item, card = _card(world)
    # Before the user opens the card, a rewrite needs no note.
    assert _put_step(world, KEY, "review", "send the weekly reminder soon?").status_code == 200
    _looked_at(world, card["id"])

    for note in (None, "  "):
        extra = {} if note is None else {"changed": note}
        refused = _put_step(world, KEY, "review", "send the weekly reminder now?", **extra)
        assert refused.status_code == 400 and "changed" in refused.text, refused.text
    # A new score alone is no rewrite, so it needs no note either.
    assert _put_step(world, KEY, "review", "send the weekly reminder soon?", score=70).status_code == 200

    assert _changes(world, item["id"]) == []
    assert _put_step(world, KEY, "review", "send the weekly reminder now?", changed=NOTE).status_code == 200
    assert _changes(world, item["id"]) == [(card["id"], NOTE)]


def test_a_rewrite_while_the_user_reads_waits_and_then_shows_the_band(world: World) -> None:
    item, card = _card(world)
    assert world.client.post(f"/api/steps/{card['id']}/timer/start", headers=world.user()).status_code == 200
    run_sql(world, "UPDATE time_spans SET started_at = ? WHERE ended_at IS NULL", (_ago(60),))

    held = _put_step(world, KEY, "review", "send the weekly reminder?", body="The tests pass.", changed=NOTE)

    assert held.status_code == 200 and held.json()["held"], held.text
    assert _changes(world, item["id"]) == []
    assert world.client.post(f"/api/steps/{card['id']}/timer/pause", headers=world.user()).status_code == 200
    assert _changes(world, item["id"]) == [(card["id"], NOTE)]


def test_a_rewrite_needs_no_note_when_no_band_would_show(world: World) -> None:
    item, card = _card(world)
    # A start and pause at once, an 8 s glance, and a long read before the user's last message are no read.
    assert world.client.post(f"/api/steps/{card['id']}/timer/start", headers=world.user()).status_code == 200
    assert world.client.post(f"/api/steps/{card['id']}/timer/pause", headers=world.user()).status_code == 200
    assert _put_step(world, KEY, "review", "send the weekly reminder soon?").status_code == 200
    _read(world, card["id"], _ago(3000), _ago(2000))
    run_sql(world, "INSERT INTO messages (author, item_id, step_id, text, state, created_at, sent_at)"
            " VALUES ('user', ?, ?, 'Try again', 'sent', ?, ?)", (item["id"], card["id"], _ago(1500), _ago(1500)))
    _looked_at(world, card["id"], seconds=8)
    assert _put_step(world, KEY, "review", "send the weekly reminder now?").status_code == 200
    assert _changes(world, item["id"]) == []


def test_a_rewrite_without_a_note_is_refused_while_the_user_reads(world: World) -> None:
    _item, card = _card(world)
    assert world.client.post(f"/api/steps/{card['id']}/timer/start", headers=world.user()).status_code == 200
    run_sql(world, "UPDATE time_spans SET started_at = ? WHERE ended_at IS NULL", (_ago(60),))

    refused = _put_step(world, KEY, "review", "send the weekly reminder now?")

    assert refused.status_code == 400 and "Changed after you read it" in refused.text, refused.text


def test_a_rewrite_held_without_a_note_before_the_rule_still_applies(world: World) -> None:
    # A put that waited behind the user's timer when Cockpit did not yet ask for the line is replayed as it was.
    item, card = _card(world)
    assert world.client.post(f"/api/steps/{card['id']}/timer/start", headers=world.user()).status_code == 200
    run_sql(world, "UPDATE time_spans SET started_at = ? WHERE ended_at IS NULL", (_ago(60),))
    conn = db.connect(world.db_path)
    try:
        held = store.upsert_step(conn, {"item_id": item["id"], "need": "review", "body": "The tests pass."}, "agent:old")
        conn.commit()
    finally:
        conn.close()
    assert held["held"]

    assert world.client.post(f"/api/steps/{card['id']}/timer/pause", headers=world.user()).status_code == 200
    assert _changes(world, item["id"]) == [(card["id"], "")]


def test_a_note_longer_than_a_line_is_refused(world: World) -> None:
    _card(world)

    refused = _put_step(world, KEY, "review", "send the weekly reminder?", changed="x" * 101)

    assert refused.status_code == 400 and "changed" in refused.text


@pytest.mark.parametrize("act", ["message", "press"])
def test_the_band_stays_until_the_user_acts_on_the_task(world: World, act: str) -> None:
    item, card = _card(world)
    _looked_at(world, card["id"])
    assert _put_step(world, KEY, "review", "send the weekly reminder?", body="The tests pass.", changed=NOTE).status_code == 200
    run_sql(world, "UPDATE step_changes SET at = ?", (_ago(300),))
    assert len(_changes(world, item["id"])) == 1

    if act == "message":
        run_sql(world, "INSERT INTO messages (author, item_id, step_id, text, state, created_at, sent_at)"
                " VALUES ('user', ?, ?, 'Send it', 'sent', ?, ?)", (item["id"], card["id"], db.now_iso(), db.now_iso()))
        assert _changes(world, item["id"]) == []
    else:
        # A press that closes the card takes it off the page with its band.
        assert world.client.post(f"/api/steps/{card['id']}/done", json={}, headers=world.user()).status_code == 200
        run_sql(world, "UPDATE steps SET status = 'open' WHERE id = ?", (card["id"],))
        assert _changes(world, item["id"]) == []


def test_a_rewrite_after_a_glance_shows_no_band(world: World) -> None:
    item, card = _card(world)
    _looked_at(world, card["id"], seconds=8)

    assert _put_step(world, KEY, "review", "send the weekly reminder?", body="The tests pass.", changed=NOTE).status_code == 200

    assert _changes(world, item["id"]) == []


def test_the_band_says_how_long_the_user_read_the_card(world: World) -> None:
    item, card = _card(world)
    # Two looks: 40 s and then 6 min, ending ten minutes ago.
    stopped = _ago(600)
    _read(world, card["id"], _ago(1200), _ago(1160))
    _read(world, card["id"], _ago(960), stopped)

    assert _put_step(world, KEY, "review", "send the weekly reminder?", body="The tests pass.", changed=NOTE).status_code == 200

    [change] = _state_item(world, item["id"])["changes"]
    assert (change["read_seconds"], change["read_until"]) == (400, stopped)


def test_reading_before_the_users_last_message_does_not_count(world: World) -> None:
    item, card = _card(world)
    _read(world, card["id"], _ago(3000), _ago(2000))
    run_sql(world, "INSERT INTO messages (author, item_id, step_id, text, state, created_at, sent_at)"
            " VALUES ('user', ?, ?, 'Try again', 'sent', ?, ?)", (item["id"], card["id"], _ago(1500), _ago(1500)))
    _looked_at(world, card["id"], seconds=5)

    assert _put_step(world, KEY, "review", "send the weekly reminder?", body="The tests pass.", changed=NOTE).status_code == 200

    assert _changes(world, item["id"]) == []


def test_reading_the_rewritten_card_takes_the_band_away(world: World) -> None:
    item, card = _card(world)
    _read(world, card["id"], _ago(900), _ago(800))
    assert _put_step(world, KEY, "review", "send the weekly reminder?", body="The tests pass.", changed=NOTE).status_code == 200
    run_sql(world, "UPDATE step_changes SET at = ?", (_ago(700),))

    # A glance at the new card keeps the band; a read takes it away.
    _read(world, card["id"], _ago(600), _ago(590))
    assert len(_changes(world, item["id"])) == 1
    _read(world, card["id"], _ago(500), _ago(470))
    assert _changes(world, item["id"]) == []
