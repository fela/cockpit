from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cockpit import config, db, hub

NOW = datetime.now(UTC).replace(microsecond=0)

# Each test runs with a [hub] table in the setup whose key file is missing, until it supplies a stand-in hub.
pytestmark = pytest.mark.usefixtures("hub_on")


def reading(used):
    return {"used_percentage": used, "resets_at": (NOW + timedelta(days=1)).timestamp(), "captured_at": NOW.timestamp()}


def test_hub_capacity_uses_saved_per_model_limits_and_every_route_without_requests(world, tmp_path, monkeypatch):
    key = tmp_path / "key"
    key.write_text("test-key")
    reader = hub.Hub("http://unused", key, {}, (), {}, world.db_path, lambda accounts, now: None)
    reader._listed = ([{"id": "a", "provider": "claude"}, {"id": "b", "provider": "codex"}], NOW.isoformat())
    hub.register_availability_reader(world.db_path, reader)
    monkeypatch.setattr(reader, "_send", lambda *args: (_ for _ in ()).throw(AssertionError("quota request")))
    conn = db.connect(world.db_path)
    db.kv_set(conn, hub.LIMITS_KEY, {
        "claude/a": {"windows": {"seven_day": reading(70)}, "models": {"Fable": reading(100)}},
        "codex/b": {"windows": {"seven_day": reading(95)}, "models": {}},
    })
    capacity = hub.model_capacity(conn, NOW)
    assert capacity[config.MAIN_MODEL]["state"] == "available"
    assert datetime.fromisoformat(capacity[config.MAIN_MODEL]["observed_at"]) == NOW
    assert capacity["claude-fable-5-2"]["state"] == "limited"
    assert capacity["gpt-6.2-sol"]["state"] == "low"
    reader._listed[0].append({"id": "c", "provider": "claude"})
    assert hub.model_capacity(conn, NOW)["claude-fable-5-2"]["state"] == "unknown"
    reader._listed = (reader._listed[0], (NOW - timedelta(minutes=10)).isoformat())
    assert hub.model_capacity(conn, NOW) == {}
    conn.close()


def test_a_held_model_is_limited_on_its_account_while_the_others_still_use_it(world, tmp_path):
    key = tmp_path / "key"
    key.write_text("test-key")
    reader = hub.Hub("http://unused", key, {}, (), {}, world.db_path, lambda accounts, now: None)
    until = (NOW + timedelta(minutes=10)).isoformat()
    reader._listed = ([{"id": "b", "provider": "codex", "quota_blocked": True, "cooldowns": [
        {"scope": "model", "model_key": "gpt-6.2-astra", "http_status": 429, "retry_at": until}]}], NOW.isoformat())
    hub.register_availability_reader(world.db_path, reader)
    conn = db.connect(world.db_path)
    db.kv_set(conn, hub.LIMITS_KEY, {"codex/b": {"windows": {"seven_day": reading(2)}, "models": {}}})
    capacity = hub.model_capacity(conn, NOW)
    assert capacity["gpt-6.2-astra"]["state"] == "limited"
    assert capacity["gpt-6.2-sol"]["state"] == "available"
    conn.close()

