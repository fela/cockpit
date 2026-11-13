"""Order of the user's queue.

The steps the user took to the top lead, newest take first (SPEC NEXT-36); every other open, unsnoozed step
follows by the sweeper's score, its judgement using the configured inbox factors.
Ties go to the oldest step. The code adds no ranking rules of its own (SPEC RANK-18), but for one the user asked
for: a step that is useful only until a time leads the queue while that time runs (SPEC NEXT-60).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

# The needs of a step that asks the user to finish its item (SPEC CONTROL-15, CONTROL-17).
FINISH_NEED = re.compile(r"complete(:.+)?")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(UTC)


def age_words(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h ago"
    return f"{hours // 24} d ago"


def goal_places(goals: list[dict[str, Any]]) -> dict[int, int]:
    """Each goal that holds by its place among them, 1 for the most important; a paused goal has none.

    `goals` is the user's list in order, without the dropped ones. The page and the CLI number goals the same way.
    """
    holding = [goal["id"] for goal in goals if goal["paused_at"] is None]
    return {goal_id: place for place, goal_id in enumerate(holding, start=1)}


def finishing(step: dict[str, Any]) -> bool:
    """Whether the step asks the user to finish its item."""
    return FINISH_NEED.fullmatch(step["need"]) is not None


def taken(step: dict[str, Any]) -> bool:
    """Whether the user took the step to the top of their queue and it still holds that place (SPEC NEXT-36)."""
    return step.get("taken_at") is not None and step["taken_at"] == step.get("queued_at")


def expiring(step: dict[str, Any], now: datetime) -> bool:
    """Whether the step is useful only until a time that has not passed yet (SPEC NEXT-60)."""
    until = parse_time(step.get("expires_at"))
    return until is not None and until > now


def expiry_rank(step: dict[str, Any], now: datetime) -> tuple[int, str]:
    """A stable sort key that leads with the steps whose time runs, the soonest to end first, and leaves the rest
    in their order."""
    return (0, step["expires_at"]) if expiring(step, now) else (1, "")


def order(steps: list[dict[str, Any]], items: dict[int, dict[str, Any]], now: datetime) -> dict[str, list]:
    """Split open steps into the ordered queue and the snoozed group, each step with its item attached."""
    queue, snoozed = [], []
    for step in steps:
        shown = step | {"item": items[step["item_id"]]}
        until = parse_time(step.get("snoozed_until"))
        (snoozed if until is not None and until > now else queue).append(shown)
    queue.sort(key=lambda step: (-step["score"], step["created_at"], step["id"]))
    # The sort is stable, so the steps the user did not take keep their score order.
    queue.sort(key=lambda step: step["taken_at"] if taken(step) else "", reverse=True)
    queue.sort(key=lambda step: expiry_rank(step, now))
    snoozed.sort(key=lambda step: step["snoozed_until"])
    return {"queue": queue, "snoozed": snoozed}
