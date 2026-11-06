"""SPEC RANK-18: the queue order is the user's own takes, then the sweeper's score, with no ranking rules of the code's own."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cockpit import rank

NOW = datetime(2026, 1, 4, 12, 0, tzinfo=UTC)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def step(step_id: int, score: int, **fields) -> dict:
    return {"id": step_id, "item_id": 1, "need": f"need-{step_id}", "score": score, "snoozed_until": None,
            "created_at": iso(NOW - timedelta(hours=step_id))} | fields


ITEM = {1: {"id": 1, "key": "note:work", "touched_at": None, "pinged_at": None, "priority": "normal", "goal_id": None,
            "created_at": iso(NOW - timedelta(hours=1)), "seen_at": None, "set_aside": 0}}


def ids(steps: list[dict]) -> list[int]:
    return [entry["id"] for entry in steps]


def test_score_then_oldest() -> None:
    ordered = rank.order([step(1, 50), step(2, 50), step(3, 10), step(4, 90)], ITEM, NOW)["queue"]
    assert ids(ordered) == [4, 2, 1, 3]


def test_the_user_s_takes_lead_newest_first() -> None:
    first, second = iso(NOW - timedelta(minutes=10)), iso(NOW - timedelta(minutes=5))
    steps = [step(1, 90), step(2, 10, taken_at=first, queued_at=first), step(3, 5, taken_at=second, queued_at=second)]
    assert ids(rank.order(steps, ITEM, NOW)["queue"]) == [3, 2, 1]


def test_no_kind_of_step_goes_first_by_rule() -> None:
    items = {
        1: ITEM[1] | {"priority": "urgent", "goal_id": 1, "touched_at": iso(NOW), "pinged_at": iso(NOW)},
        2: ITEM[1] | {"id": 2, "key": "todo-2", "priority": "low"},
    }
    steps = [step(1, 40, need="complete"), step(2, 60, item_id=2, need="read:answer"), step(3, 50, size="small")]
    assert ids(rank.order(steps, items, NOW)["queue"]) == [2, 3, 1]


def test_snoozed_steps_leave_the_queue_until_due() -> None:
    later = iso(NOW + timedelta(hours=1))
    earlier = iso(NOW - timedelta(minutes=1))
    result = rank.order([step(1, 50, snoozed_until=later), step(2, 50, snoozed_until=earlier)], ITEM, NOW)
    assert ids(result["queue"]) == [2]
    assert ids(result["snoozed"]) == [1]
