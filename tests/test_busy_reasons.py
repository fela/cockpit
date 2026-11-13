from dataclasses import replace
from datetime import UTC, datetime

from tests.conftest import SESSION_A, SESSION_B, make_session
from tests.test_api import put_item, state_item


def test_a_rate_limited_agent_waits_with_a_reason_instead_of_looking_busy(world):
    item = put_item(world, stage="building", sessions=[SESSION_A])
    world.cache.sessions = [replace(make_session(SESSION_A, 99), detail="API Error 429: rate_limit_error",
                                    updated_at=datetime.now(UTC).isoformat())]
    shown = state_item(world, item["id"])
    assert (shown["group"], shown["wait"], shown["activity"]) == ("waiting", "blocked", "Claude rate limit")
    worker = replace(make_session(SESSION_B, 100, state="working"), tempo="active")
    world.cache.sessions.append(worker)
    put_item(world, sessions=[SESSION_A, SESSION_B])
    shown = state_item(world, item["id"])
    assert (shown["group"], shown["activity"]) == ("busy", "Working now")


def test_a_terminal_dialog_has_an_approval_reason(world):
    item = put_item(world, stage="building", sessions=[SESSION_A])
    world.cache.sessions = [replace(make_session(SESSION_A, 99, state="working"), tempo="blocked", job_state="working")]
    shown = state_item(world, item["id"])
    assert (shown["group"], shown["activity"]) == ("waiting", "Waiting for approval")
