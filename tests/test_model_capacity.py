from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cockpit import accounts, config, model_capacity, sessions
from tests.conftest import SESSION_A, make_session

NOW = datetime.now(UTC).replace(microsecond=0)


def reading(used, hours):
    return {"used_percentage": used, "resets_at": (NOW + timedelta(hours=hours)).timestamp()}


def test_model_capacity_distinguishes_scoped_fable_limit_and_unknown_routes():
    windows = [reading(30, 2), reading(70, 24)]
    named = {"Fable": reading(100, 24)}
    fable = model_capacity.account(model_capacity.model_windows("claude-fable-5-2", windows, named), NOW)
    opus = model_capacity.account(model_capacity.model_windows(config.MAIN_MODEL, windows, named), NOW)
    assert fable["state"] == "limited" and opus["state"] == "available"
    assert model_capacity.combine([fable, opus])["state"] == "available"
    assert model_capacity.combine([fable, model_capacity.unknown()])["state"] == "unknown"
    assert model_capacity.combine([fable, fable])["state"] == "limited"


def test_model_capacity_near_limit_uses_existing_ninety_percent_threshold_and_live_windows():
    low = model_capacity.account([reading(90, 2), None], NOW)
    roomy = model_capacity.account([reading(89, 2)], NOW)
    assert low["state"] == "low" and low["remaining_percent"] == 10
    assert model_capacity.combine([low, roomy])["state"] == "available"
    assert model_capacity.combine([low, model_capacity.account([reading(95, 2)], NOW)])["state"] == "low"
    assert model_capacity.account([reading(100, -1)], NOW)["state"] == "unknown"
    blocked = model_capacity.account([reading(100, 2), reading(100, 24)], NOW)
    assert datetime.fromisoformat(blocked["resets_at"]) == NOW + timedelta(hours=24)


def test_source_stop_applies_only_to_its_exact_model_and_expires_at_reset(monkeypatch):
    stop = sessions.LimitStop(cooldown_stops=0, at=(NOW - timedelta(minutes=5)).isoformat(),
                              resets=NOW + timedelta(hours=1))
    observed = sessions.Usage(stop=stop, model=config.MAIN_MODEL, answer=None, effort="")
    capacities = {"gpt-6.2-sol": model_capacity.account([reading(20, 2)], NOW)}
    stopped = model_capacity.for_session(observed, config.MAIN_MODEL, "claude", capacities, NOW)
    assert stopped["state"] == "limited" and stopped["resets_at"] == stop.resets.isoformat()
    assert model_capacity.for_session(observed, "claude-fable-5-2", "claude", capacities, NOW)["state"] == "unknown"
    assert model_capacity.for_session(observed, "gpt-6.2-sol", "codex", capacities, NOW)["state"] == "available"
    assert model_capacity.for_session(observed, config.MAIN_MODEL, "claude", capacities,
                                      NOW + timedelta(hours=2))["state"] == "unknown"


def test_expired_cooldown_yields_to_saved_availability(monkeypatch):
    stop = sessions.LimitStop(cooldown_stops=1, at=(NOW - timedelta(minutes=11)).isoformat(), resets=None)
    observed = sessions.Usage(stop=stop, model=config.MAIN_MODEL, answer=None, effort="")
    capacities = {config.MAIN_MODEL: model_capacity.account([reading(20, 2)], NOW)}
    assert model_capacity.for_session(observed, config.MAIN_MODEL, "claude", capacities, NOW)["state"] == "available"


def test_newer_confirmed_availability_supersedes_old_explicit_reset_stop(monkeypatch):
    stop = sessions.LimitStop(cooldown_stops=0, at=(NOW - timedelta(minutes=5)).isoformat(),
                              resets=NOW + timedelta(days=3))
    observed = sessions.Usage(stop=stop, model=config.MAIN_MODEL, answer=None, effort="")
    old = model_capacity.account([reading(20, 24) | {"captured_at": (NOW - timedelta(minutes=10)).timestamp()}], NOW)
    fresh = model_capacity.account([reading(40, 24) | {"captured_at": NOW.isoformat()}], NOW)
    assert model_capacity.for_session(observed, config.MAIN_MODEL, "claude", {config.MAIN_MODEL: old}, NOW)["state"] == "limited"
    pooled = model_capacity.combine([old, fresh])
    assert model_capacity.for_session(observed, config.MAIN_MODEL, "claude", {config.MAIN_MODEL: pooled}, NOW)["state"] == "available"
    partial = model_capacity.account([reading(20, 2), reading(40, 24) | {"captured_at": NOW.timestamp()}], NOW)
    assert partial["observed_at"] is None
    assert model_capacity.for_session(observed, config.MAIN_MODEL, "claude", {config.MAIN_MODEL: partial}, NOW)["state"] == "limited"


def test_local_bank_reset_reading_clears_old_source_limit(world, monkeypatch):
    world.cache.sessions = [make_session(SESSION_A, pid=None, cwd=str(world.checkout))]
    monkeypatch.setattr(world.spend_index, "latest_models", lambda: {SESSION_A: config.MAIN_MODEL})
    monkeypatch.setattr(accounts, "on_api_server", lambda settings: False)
    stop = sessions.LimitStop(cooldown_stops=0, at=(NOW - timedelta(minutes=5)).isoformat(),
                              resets=NOW + timedelta(days=3))
    observed = sessions.Usage(stop=stop, model=config.MAIN_MODEL, answer=None, effort="")
    monkeypatch.setattr(sessions, "transcript_usages", lambda ids: {sid: observed for sid in ids})
    monkeypatch.setattr(accounts, "readings", lambda listed, path: {
        "personal": {"rate_limits": {"seven_day": reading(20, 24)}, "captured_at": NOW.timestamp()},
    })
    state = world.client.get("/api/state").json()
    row = next(each for each in state["sessions"] if each["session_id"] == SESSION_A)
    assert row["settings"]["capacity"]["state"] == "available"


def test_fable_stop_needs_newer_scoped_observation_to_clear(monkeypatch):
    model = "claude-fable-5-2"
    stop = sessions.LimitStop(cooldown_stops=0, at=(NOW - timedelta(minutes=5)).isoformat(),
                              resets=NOW + timedelta(days=3))
    observed = sessions.Usage(stop=stop, model=model, answer=None, effort="")
    general = [reading(20, 24) | {"captured_at": NOW.isoformat()}]
    unscoped = model_capacity.account(model_capacity.model_windows(model, general, {}), NOW)
    assert unscoped["state"] == "available"
    assert model_capacity.for_session(observed, model, "claude", {model: unscoped}, NOW)["state"] == "limited"
    scoped = model_capacity.account(model_capacity.model_windows(model, general, {
        "Fable": reading(20, 24) | {"captured_at": NOW.isoformat()},
    }), NOW)
    pooled = model_capacity.combine([unscoped, scoped])
    assert model_capacity.for_session(observed, model, "claude", {model: pooled}, NOW)["state"] == "available"
    stale_shared = model_capacity.account(model_capacity.model_windows(model, [
        reading(20, 24) | {"captured_at": (NOW - timedelta(minutes=10)).isoformat()},
    ], {"Fable": reading(20, 24) | {"captured_at": NOW.isoformat()}}), NOW)
    mixed = model_capacity.combine([unscoped, stale_shared])
    assert model_capacity.for_session(observed, model, "claude", {model: mixed}, NOW)["state"] == "limited"
    shared_block = model_capacity.account(model_capacity.model_windows(model, [reading(100, 24)], {}), NOW)
    assert shared_block["state"] == "limited"


def test_state_exposes_model_and_source_capacity_from_saved_records(world, monkeypatch):
    world.cache.sessions = [make_session(SESSION_A, pid=None, cwd=str(world.checkout))]
    monkeypatch.setattr(world.spend_index, "latest_models", lambda: {SESSION_A: config.MAIN_MODEL})
    monkeypatch.setattr(accounts, "on_api_server", lambda settings: False)
    monkeypatch.setattr(accounts, "readings", lambda listed, path: {
        "personal": {"rate_limits": {"seven_day": reading(95, 24)}}})
    state = world.client.get("/api/state").json()
    assert state["session_options"]["claude"]["models"][0]["capacity"]["state"] == "low"
    row = next(each for each in state["sessions"] if each["session_id"] == SESSION_A)
    assert row["settings"]["capacity"]["remaining_percent"] == 5


def test_existing_local_session_keeps_its_own_accounts_capacity(world, monkeypatch):
    world.cache.sessions = [make_session(SESSION_A, pid=None, cwd=str(world.checkout))]
    monkeypatch.setattr(world.spend_index, "latest_models", lambda: {SESSION_A: config.MAIN_MODEL})
    monkeypatch.setattr(accounts, "on_api_server", lambda settings: False)
    monkeypatch.setattr(accounts, "in_use", lambda configured, warnings: list(configured))
    monkeypatch.setattr(accounts, "readings", lambda listed, path: {
        "personal": {"rate_limits": {"seven_day": reading(100, 24)}},
        "work": {"rate_limits": {"seven_day": reading(20, 24)}},
    })
    state = world.client.get("/api/state").json()
    assert state["session_options"]["claude"]["models"][0]["capacity"]["state"] == "available"
    row = next(each for each in state["sessions"] if each["session_id"] == SESSION_A)
    assert row["settings"]["capacity"]["state"] == "limited"
    assert row["settings"]["model_capacity"]["claude-fable-5-2"]["state"] == "limited"


def test_state_reuses_one_observation_for_model_effort_and_capacity(world, monkeypatch):
    world.cache.sessions = [make_session(SESSION_A, pid=None, cwd=str(world.checkout))]
    monkeypatch.setattr(world.spend_index, "latest_models", dict)
    stop = sessions.LimitStop(cooldown_stops=0, at=NOW.isoformat(), resets=NOW + timedelta(hours=1))
    observed = sessions.Usage(stop=stop, answer=None, model=config.MAIN_MODEL, effort="xhigh")
    batches = []

    def batch(ids):
        batches.append(ids)
        return {sid: observed for sid in ids}

    def single(sid):
        raise AssertionError("The state request must share its transcript lookup")

    monkeypatch.setattr(sessions, "transcript_usages", batch)
    monkeypatch.setattr(sessions, "transcript_usage", single)
    response = world.client.get("/api/state")
    assert response.status_code == 200
    row = next(each for each in response.json()["sessions"] if each["session_id"] == SESSION_A)
    assert batches == [[SESSION_A]]
    assert row["settings"]["model"] == config.MAIN_MODEL
    assert row["settings"]["effort"] == "xhigh"
    assert row["settings"]["capacity"]["state"] == "limited"

