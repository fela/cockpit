from __future__ import annotations

import json

import pytest

from cockpit.state_sync import StateVersions


def body(response):
    return json.loads(response.body)


def test_changes_reconstruct_the_state_including_removed_fields():
    versions = StateVersions()
    before = {"now": "before", "queue": [{"body": "a long card" * 1000}], "old": True}
    first = versions.response(before, None, "user")
    after = {"now": "after", "queue": before["queue"], "timer": {"seconds": 2}}
    second = versions.response(after, first.headers["x-cockpit-state"], "user")
    delta = body(second)
    assert delta == {"changes": {"now": "after", "timer": {"seconds": 2}}, "removed": ["old"]}
    reconstructed = body(first) | delta["changes"]
    for key in delta["removed"]:
        del reconstructed[key]
    assert reconstructed == after
    assert len(second.body) < len(first.body) / 100


def test_unknown_evicted_or_other_callers_checkpoint_returns_full_state():
    versions = StateVersions()
    first = versions.response({"queue": [1]}, None, "user")
    cursor = first.headers["x-cockpit-state"]
    for scope, since in (("agent", cursor), ("user", "unknown")):
        response = versions.response({"queue": [2]}, since, scope)
        assert body(response) == {"queue": [2]}
        assert "x-cockpit-base" not in response.headers
    for i in range(65):
        versions.response({"queue": [i]}, None, "user")
    response = versions.response({"queue": [3]}, cursor, "user")
    assert body(response) == {"queue": [3]}
    assert "x-cockpit-base" not in response.headers


def test_state_api_remains_full_for_cli_and_sends_changes_for_browser(world):
    first = world.client.get("/api/state", headers={"x-cockpit-sync": "1"})
    assert first.status_code == 200
    assert "queue" in first.json()
    assert first.headers["cache-control"] == "no-store"
    cursor = first.headers["x-cockpit-state"]
    second = world.client.get("/api/state", headers={"x-cockpit-since": cursor})
    assert second.status_code == 200
    assert second.headers["x-cockpit-base"] == cursor
    assert "queue" not in second.json()["changes"]
    assert second.json()["removed"] == []
    plain = world.client.get("/api/state")
    assert "queue" in plain.json()
    assert "x-cockpit-state" not in plain.headers


def test_plain_api_reads_skip_checkpoint_work(world, monkeypatch):
    def unexpected(*args):
        raise AssertionError("plain API reads must not hash state")

    monkeypatch.setattr(StateVersions, "response", unexpected)
    response = world.client.get("/api/state")
    assert response.status_code == 200
    assert "queue" in response.json()


def test_encoded_full_and_delta_responses_preserve_json_values():
    versions = StateVersions()
    state = {'quoted"key': ["line\nwith a tab\t", "Tokyo 東京", {"value": None, "enabled": True}], "value": 2.5}
    first = versions.response(state, None, "user")
    assert body(first) == state
    assert first.headers["content-type"] == "application/json"
    changed = {**state, "value": 3.5}
    second = versions.response(changed, first.headers["x-cockpit-state"], "user")
    assert body(second) == {"changes": {"value": 3.5}, "removed": []}
    recovered = versions.response(changed, "expired", "user")
    assert body(recovered) == changed


def test_nonfinite_values_still_fail_to_encode():
    with pytest.raises(ValueError):
        StateVersions().response({"value": float("nan")}, None, "user")
