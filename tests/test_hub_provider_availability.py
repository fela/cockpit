"""Provider selection reads a fresh, anonymous summary of the hub's accounts and raw quotas."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from cockpit import config, db, hub, parts, sweeper
from tests.conftest import use_hub

NOW = datetime(2026, 11, 14, 12, tzinfo=UTC)
CLAUDE = {"id": "private@example.com.json", "provider": "claude", "email": "private@example.com", "auth_index": 1,
          "token": "a-secret-token"}
CODEX = {"id": "another@example.com.json", "provider": "codex", "email": "another@example.com", "auth_index": 2}


@pytest.fixture
def availability(tmp_path, monkeypatch):
    key = tmp_path / "management-key"
    key.write_text("secret-management-key")
    use_hub(monkeypatch, config.HubSetup(url="http://unused", key_file=key, sign_in={}, restarts={}, tunnel_state=None))
    path = tmp_path / "cockpit.db"
    conn = db.connect(path)
    db.migrate(conn)
    reader = hub.Hub("http://unused", key, {}, (), {}, path, lambda accounts, now: None)
    # As the hub's part does when the server starts.
    hub.register_availability_reader(path, reader)
    credentials = []
    monkeypatch.setattr(reader, "_tunnel", lambda: "active")
    monkeypatch.setattr(reader, "_get", lambda route, field: credentials)
    monkeypatch.setattr(reader, "_service", lambda now: {"error": None})
    monkeypatch.setattr(reader, "_check", lambda credential, now: ({"windows": {}, "models": {}, "plan": ""}, "", False))
    yield conn, reader, credentials
    conn.close()


def limit(used, reset_after=3600):
    return {"used_percentage": used, "resets_at": NOW.timestamp() + reset_after, "captured_at": NOW.isoformat()}


def keep_limits(conn, credential, *, windows, models):
    kept = db.kv_get(conn, hub.LIMITS_KEY) or {}
    kept[hub.limits_key(credential)] = {"windows": windows, "models": models, "plan": ""}
    db.kv_set(conn, hub.LIMITS_KEY, kept)


def test_a_setup_without_a_hub_has_no_provider_override(tmp_path):
    assert config.setup().hub is None
    conn = db.connect(tmp_path / "cockpit.db")
    db.migrate(conn)
    try:
        assert parts.provider_availability(conn, NOW) is None
    finally:
        conn.close()


def test_no_configured_hub_has_no_override_even_with_a_snapshot(availability):
    conn, reader, _ = availability
    db.kv_set(conn, hub.PROVIDER_AVAILABILITY_KEY, {"observed_at": NOW.timestamp(), "claude": False, "codex": True})
    reader._key_path.unlink()
    assert parts.provider_availability(conn, NOW) is None


@pytest.mark.parametrize("snapshot", [None, {}, [], {"observed_at": "broken"}, {"observed_at": True},
                                      {"observed_at": NOW.timestamp() + 1, "claude": True},
                                      {"observed_at": NOW.timestamp() - 3 * hub.REFRESH_SECONDS - 0.01, "claude": True}])
def test_missing_invalid_stale_or_future_snapshots_are_unknown(availability, monkeypatch, snapshot):
    conn, reader, _ = availability
    # The refresh that follows an unusable snapshot saves nothing new.
    monkeypatch.setattr(reader, "_refresh_safely", lambda now: {})
    db.kv_set(conn, hub.PROVIDER_AVAILABILITY_KEY, snapshot)
    assert parts.provider_availability(conn, NOW) == {}


def test_fresh_snapshot_returns_only_provider_booleans_through_expiry_boundary(availability):
    conn, _, _ = availability
    db.kv_set(conn, hub.PROVIDER_AVAILABILITY_KEY,
              {"observed_at": NOW.timestamp(), "claude": False, "codex": True, "email": "private@example.com"})
    edge = NOW + timedelta(seconds=3 * hub.REFRESH_SECONDS)
    assert parts.provider_availability(conn, edge) == {"claude": False, "codex": True}
    db.kv_set(conn, hub.PROVIDER_AVAILABILITY_KEY, {"observed_at": NOW.timestamp(), "claude": 1, "codex": "yes"})
    assert parts.provider_availability(conn, NOW) == {}


def test_malformed_snapshot_json_is_logged_and_unknown(availability, caplog):
    conn, _, _ = availability
    conn.execute("INSERT INTO kv(key,value) VALUES (?,?)", (hub.PROVIDER_AVAILABILITY_KEY, "broken"))
    assert parts.provider_availability(conn, NOW) == {}
    assert "provider availability failed" in caplog.text


def test_refresh_persists_only_anonymous_boolean_summary_and_time(availability):
    conn, reader, credentials = availability
    credentials.extend([CLAUDE, CODEX | {"disabled": True}])
    reader.refresh(NOW)
    assert db.kv_get(conn, hub.PROVIDER_AVAILABILITY_KEY) == {
        "observed_at": NOW.timestamp(), "claude": True, "codex": False,
    }
    assert parts.provider_availability(conn, NOW) == {"claude": True, "codex": False}


@pytest.mark.parametrize("state", [{"disabled": True}, {"requires_sign_in": True}, {"unavailable": True},
                                   {"quota_blocked": True, "next_retry_after": "2020-01-01T00:00:00Z"}])
def test_nonworking_accounts_are_unavailable_even_when_cooldown_elapsed(availability, state):
    conn, reader, credentials = availability
    credentials.append(CLAUDE | state)
    reader.refresh(NOW)
    assert parts.provider_availability(conn, NOW) == {"claude": False, "codex": False}


@pytest.mark.parametrize("window,used,reset_after,expected", [
    ("five_hour", 100, 3600, False), ("seven_day", 101, 3600, False),
    ("five_hour", 99.9, 3600, True), ("seven_day", 100, -1, True), ("five_hour", 100, 0, True),
])
def test_raw_general_limits_block_only_until_the_reset(availability, window, used, reset_after, expected):
    conn, reader, credentials = availability
    credentials.append(CLAUDE)
    keep_limits(conn, CLAUDE, windows={window: limit(used, reset_after)}, models={})
    reader.refresh(NOW)
    assert parts.provider_availability(conn, NOW)["claude"] is expected


def test_a_model_only_limit_leaves_other_models_available(availability):
    conn, reader, credentials = availability
    credentials.append(CLAUDE)
    keep_limits(conn, CLAUDE, windows={"seven_day": limit(70)}, models={hub.FABLE: limit(100)})
    reader.refresh(NOW)
    assert parts.provider_availability(conn, NOW)["claude"] is True


def test_one_working_account_with_room_makes_its_provider_available(availability):
    conn, reader, credentials = availability
    usable = CLAUDE | {"id": "second@example.com.json", "auth_index": 3}
    credentials.extend([CLAUDE, usable, CODEX | {"requires_sign_in": True}])
    keep_limits(conn, CLAUDE, windows={"five_hour": limit(100)}, models={})
    reader.refresh(NOW)
    assert parts.provider_availability(conn, NOW) == {"claude": True, "codex": False}


@pytest.mark.parametrize("another_usable", [False, True])
def test_failed_limit_checks_are_unknown_unless_another_account_is_usable(availability, monkeypatch, another_usable):
    conn, reader, credentials = availability
    credentials.append(CLAUDE)
    monkeypatch.setattr(reader, "_check", lambda credential, now: (None, "provider unavailable", False))
    if another_usable:
        second = CLAUDE | {"id": "second@example.com.json", "auth_index": 3}
        credentials.append(second)
        keep_limits(conn, second, windows={"seven_day": limit(10)}, models={})
    reader.refresh(NOW)
    expected = {"claude": True, "codex": False} if another_usable else {"codex": False}
    assert parts.provider_availability(conn, NOW) == expected


@pytest.mark.parametrize("listed_before", [False, True])
def test_failed_credentials_read_invalidates_availability_instead_of_using_old_accounts(
        availability, monkeypatch, listed_before):
    conn, reader, credentials = availability
    credentials.extend([CLAUDE, CODEX])
    if listed_before:
        reader.refresh(NOW)
    else:
        db.kv_set(conn, hub.PROVIDER_AVAILABILITY_KEY, {"observed_at": NOW.timestamp(), "claude": True})

    def failed(*args):
        raise hub.HubUnreached("offline")

    monkeypatch.setattr(reader, "_get", failed)
    assert reader.refresh(NOW)["error"] == "offline"
    assert parts.provider_availability(conn, NOW) == {}


def test_unexpected_refresh_failure_invalidates_even_after_accounts_were_saved(availability, monkeypatch):
    conn, reader, credentials = availability
    credentials.extend([CLAUDE, CODEX])

    def broken(now):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(reader, "_service", broken)
    assert "unexpected failure" in reader._refresh_safely(NOW)["error"]
    assert parts.provider_availability(conn, NOW) == {}


def test_empty_hub_has_no_available_providers(availability):
    conn, reader, _ = availability
    reader.refresh(NOW)
    assert parts.provider_availability(conn, NOW) == {"claude": False, "codex": False}


def test_hook_polls_only_the_registered_reader_for_its_database(availability, monkeypatch, tmp_path):
    conn, reader, _ = availability
    calls = []
    monkeypatch.setattr(reader, "read", lambda now, show_code: calls.append((now, show_code)))
    # A database whose server built no hub part has no hub, whatever another database has registered.
    monkeypatch.delitem(hub._availability_readers, str(reader._db_path.resolve()))
    hub.register_availability_reader(tmp_path / "other.db", reader)
    assert parts.provider_availability(conn, NOW) is None
    assert calls == []
    hub.register_availability_reader(reader._db_path, reader)
    db.kv_set(conn, hub.PROVIDER_AVAILABILITY_KEY, {"observed_at": NOW.timestamp()})
    assert parts.provider_availability(conn, NOW) == {}
    assert calls == [(NOW, False)]


def test_expired_snapshot_waits_for_fresh_limits_before_routing(availability):
    conn, reader, credentials = availability
    credentials.extend([CLAUDE | {"quota_blocked": True}, CODEX])
    db.kv_set(conn, hub.PROVIDER_AVAILABILITY_KEY,
              {"observed_at": NOW.timestamp() - 181, "claude": True, "codex": True})
    hub.register_availability_reader(reader._db_path, reader)
    assert parts.provider_availability(conn, NOW) == {"claude": False, "codex": True}


def test_two_cooling_claude_accounts_and_two_working_codex_accounts_allow_codex(availability, monkeypatch):
    conn, reader, credentials = availability
    credentials.extend([
        CLAUDE | {"unavailable": True, "quota_blocked": True},
        CLAUDE | {"id": "second-claude.json", "auth_index": 3, "unavailable": True},
        CODEX,
        CODEX | {"id": "second-codex.json", "auth_index": 4},
    ])
    reader.refresh(NOW)
    assert parts.provider_availability(conn, NOW) == {"claude": False, "codex": True}
    setup = config.setup()
    monkeypatch.setattr(config, "setup", lambda: replace(setup, providers=("claude", "codex")))
    assert sweeper.provider(conn, NOW) == "codex"
