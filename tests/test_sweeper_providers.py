from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from cockpit import config, db, parts, store, sweeper
from tests.conftest import SESSION_B
from tests.test_sweeper import age_everything, iso, put_steps, set_checked, stop_sweep


@pytest.fixture
def fallback(monkeypatch):
    setup = config.setup()
    monkeypatch.setattr(config, "setup", lambda: replace(setup, providers=("claude", "codex")))
    monkeypatch.setattr(parts, "provider_availability", lambda conn, now: {"claude": False, "codex": True})


@pytest.mark.parametrize("reuse", [False, True])
@pytest.mark.parametrize("path", ["normal", "check", "reorder", "pause", "wake", "recovery", "fill"])
def test_every_launch_path_keeps_the_selected_provider(world, fallback, path, reuse):
    if path in ("check", "reorder", "pause"):
        put_steps(world, 1)
    if path == "recovery":
        stop_sweep(world, 60, 50)
    if path != "recovery":
        age_everything(world, 3)
    if path == "check":
        set_checked(world, sweeper.CHECK_EVERY_MINUTES + 1)
    conn = db.connect(world.db_path)
    try:
        now = datetime.now(UTC)
        sweeper.user_seen(conn, now)
        if reuse:
            old = iso(now - timedelta(hours=2))
            conn.execute("INSERT INTO codex_threads (session_id, name, cwd, updated_at, model) VALUES (?, ?, ?, ?, ?)",
                         (SESSION_B, "Cockpit sweep", str(world.checkout), old, "gpt-6.2-sol"))
            cursor = conn.execute("INSERT INTO messages (author, session_id, text, state, created_at, sent_at, provider)"
                                  " VALUES ('cockpit', ?, 'sweep', 'sent', ?, ?, 'codex')", (SESSION_B, old, old))
            db.kv_set(conn, "sweep_launch", {"message_id": cursor.lastrowid, "kind": "sweep", "step_ids": []})
        if path == "pause":
            db.kv_set(conn, "user_input", iso(now - timedelta(minutes=sweeper.PAUSE_AFTER_MINUTES + 1)))
        if path == "reorder":
            sweeper.reorder_soon(conn, "queue changed")
        if path == "wake":
            sweeper.request(conn, "agent:other", "check again")
            conn.execute("UPDATE events SET at = ?", (iso(now - timedelta(minutes=3)),))
        exists = sweeper.resumable(conn, lambda sid: sid == SESSION_B, lambda sid: None)
        if path == "normal":
            message_id = sweeper.launch(conn, "cockpit", "sweep", world.checkout, exists, "sweep", [])["id"]
        else:
            call = {"check": sweeper.check_once, "reorder": sweeper.reorder_once, "pause": sweeper.pause_once,
                    "wake": sweeper.wake_once, "recovery": sweeper.wake_once, "fill": sweeper.fill_once}[path]
            message_id = call(conn, world.checkout, exists)
        assert message_id is not None
        message = store.get_message(conn, message_id)
        assert message["provider"] == "codex"
        assert message["session_id"] == (SESSION_B if reuse else None)
        if not reuse:
            assert message["new_options"] == {"model": "gpt-6.2-sol", "effort": "high"}
        with pytest.raises(store.Conflict):
            sweeper.launch(conn, "cockpit", "again", world.checkout, exists, "sweep", [])
        if path == "recovery":
            assert db.kv_get(conn, "sweep_recovery") is not None
    finally:
        conn.close()


@pytest.mark.parametrize("available", [{}, {"claude": True, "codex": True}, {"claude": False},
                                       {"claude": False, "codex": False}])
def test_unknown_or_unavailable_fallback_keeps_claude(world, fallback, monkeypatch, available):
    monkeypatch.setattr(parts, "provider_availability", lambda conn, now: available)
    conn = db.connect(world.db_path)
    try:
        assert sweeper.provider(conn, datetime.now(UTC)) == "claude"
    finally:
        conn.close()


def test_hook_errors_keep_claude_and_are_logged(world, fallback, monkeypatch, caplog):
    def broken(conn, now):
        raise RuntimeError("availability failed")

    monkeypatch.setattr(parts, "provider_availability", broken)
    conn = db.connect(world.db_path)
    try:
        assert sweeper.provider(conn, datetime.now(UTC)) == "claude"
        assert "keeping Claude" in caplog.text
    finally:
        conn.close()


def test_disabled_codex_keeps_claude(world, monkeypatch):
    monkeypatch.setattr(parts, "provider_availability", lambda conn, now: {"claude": False, "codex": True})
    conn = db.connect(world.db_path)
    try:
        message = sweeper.launch(conn, "cockpit", "sweep", world.checkout, lambda sid: False, "sweep", [])
        assert message["provider"] == "claude"
    finally:
        conn.close()


@pytest.mark.parametrize("used,reset_seconds,expected", [(100, 3600, "codex"), (99.9, 3600, "claude"),
                                                      (100, -1, "claude"), (None, 3600, "claude")])
def test_native_claude_limits_respect_reset_time_and_unknown_usage(world, fallback, monkeypatch, used, reset_seconds, expected):
    now = datetime.now(UTC)
    monkeypatch.setattr(parts, "provider_availability", lambda conn, now: None)
    monkeypatch.setattr(sweeper.accounts, "on_api_server", lambda path: False)
    monkeypatch.setattr(sweeper.accounts, "usable", lambda found, now: found)
    record = None if used is None else {"rate_limits": {
        "seven_day": {"used_percentage": used, "resets_at": now.timestamp() + reset_seconds}}}
    monkeypatch.setattr(sweeper.accounts, "readings", lambda found, path: {"personal": record})
    conn = db.connect(world.db_path)
    try:
        assert sweeper.provider(conn, now) == expected
    finally:
        conn.close()


def test_codex_identity_can_come_from_launch_messages(world):
    conn = db.connect(world.db_path)
    try:
        conn.execute("INSERT INTO messages (author, session_id, text, state, created_at, provider)"
                     " VALUES ('cockpit', ?, 'sweep', 'sent', ?, 'codex')", (SESSION_B, db.now_iso()))
        assert sweeper.resumable(conn, lambda sid: True, lambda sid: config.MAIN_MODEL)(SESSION_B)
    finally:
        conn.close()


@pytest.mark.parametrize("kind", ["sweep", "check"])
@pytest.mark.parametrize("before_acknowledgement", [False, True])
def test_failed_codex_turn_reports_the_error_and_releases_the_launch(world, fallback, kind, before_acknowledgement):
    step_ids = [step["id"] for step in put_steps(world, 1)]
    conn = db.connect(world.db_path)
    try:
        now = db.now_iso()
        failed_at = iso(datetime.now(UTC) - timedelta(seconds=1)) if before_acknowledgement else now
        created_at = iso(datetime.now(UTC) - timedelta(seconds=2))
        conn.executemany("UPDATE steps SET checked_at = ? WHERE id = ?", [(created_at, step_id) for step_id in step_ids])
        conn.execute("INSERT INTO codex_threads (session_id, name, cwd, updated_at, state, detail, model)"
                     " VALUES (?, 'Cockpit sweep', ?, ?, 'failed', 'Codex usage limit reached', 'gpt-6.2-sol')",
                     (SESSION_B, str(world.checkout), failed_at))
        cursor = conn.execute("INSERT INTO messages (author, session_id, text, state, created_at, sent_at, provider)"
                              " VALUES ('cockpit', ?, 'sweep', 'sent', ?, ?, 'codex')", (SESSION_B, created_at, now))
        db.kv_set(conn, "sweep_launch", {"message_id": cursor.lastrowid, "kind": kind, "step_ids": step_ids})
        assert sweeper.launch_status(conn, datetime.now(UTC))["state"] == "failed"
        assert sweeper.launch_status(conn, datetime.now(UTC))["detail"] == "Codex usage limit reached"
        assert sweeper.busy(conn, datetime.now(UTC)) is None
    finally:
        conn.close()
