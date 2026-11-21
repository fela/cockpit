from dataclasses import replace
from datetime import UTC, datetime, timedelta

from tests.conftest import SESSION_A, SESSION_B, make_session
from tests.test_api import put_item, put_step, state_item


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


def test_a_provider_limit_stays_visible_with_a_background_command(world):
    item = put_item(world, stage="building", sessions=[SESSION_A])
    world.cache.sessions = [replace(make_session(SESSION_A, 99), status="busy", tempo="blocked",
                                    job_state="blocked", in_flight=["local_bash"],
                                    detail="API Error 429: rate_limit_error")]
    shown = state_item(world, item["id"])
    assert (shown["group"], shown["wait"], shown["activity"]) == ("waiting", "blocked", "Claude rate limit")
    # A separate agent's command still making progress keeps the task busy.
    world.cache.sessions.append(replace(make_session(SESSION_B, 100, state="done"), status="busy",
                                        in_flight=["local_bash"]))
    put_item(world, sessions=[SESSION_A, SESSION_B])
    assert state_item(world, item["id"])["activity"] == "Background work"
    assert state_item(world, item["id"])["group"] == "busy"


def test_a_just_finished_agent_waits_before_the_stall_threshold(world):
    item = put_item(world, stage="building", sessions=[SESSION_A])
    world.cache.sessions = [replace(make_session(SESSION_A, 99, state="done"),
                                    updated_at=datetime.now(UTC).isoformat())]
    shown = state_item(world, item["id"])
    assert (shown["group"], shown["wait"], shown["activity"], shown["stalled_since"]) == (
        "waiting", "stuck", "Agent idle", None)
    assert "is idle" in shown["tip"]


def test_snoozed_decisions_and_results_keep_idle_work_out_of_busy(world):
    item = put_item(world, stage="building", sessions=[SESSION_A])
    world.cache.sessions = [make_session(SESSION_A, None, state="stopped")]
    for need in ("decide", "read:result"):
        step = put_step(world, item_key=item["key"], need=need)["step"]
        until = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        response = world.client.post(f"/api/steps/{step['id']}/snooze", json={"until": until}, headers=world.user())
        assert response.status_code == 200
        shown = state_item(world, item["id"])
        assert (shown["group"], shown["wait"], shown["tip_at"]) == ("waiting", "snoozed", until)
        assert shown["stalled_since"] is None
        world.client.post(f"/api/steps/{step['id']}/dismiss", json={}, headers=world.user())


def test_a_timer_preserves_its_process_but_waits_until_work_starts(world):
    item = put_item(world, stage="building", sessions=[SESSION_A])
    until = (datetime.now(UTC) + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    put_item(world, waiting_on=[{"kind": "agent", "who": SESSION_A, "until": until, "why": "check the upload"}])
    resting = replace(make_session(SESSION_A, 99, state="working"), tempo="idle", job_state="working",
                      status="idle", in_flight=["session_cron"])
    world.cache.sessions = [resting]
    shown = state_item(world, item["id"])
    assert (shown["group"], shown["wait"], shown["tip_at"], shown["activity"]) == (
        "waiting", "resumes", until, "Agent idle")
    assert shown["stalled_since"] is None
    assert not resting.turn_over
    world.cache.sessions = [replace(resting, status="busy", tempo="active")]
    assert state_item(world, item["id"])["group"] == "busy"
    # A real command running alongside its timer is still busy.
    world.cache.sessions = [replace(resting, status="busy", in_flight=["session_cron", "local_bash"])]
    assert state_item(world, item["id"])["group"] == "busy"
