from __future__ import annotations

import logging
import os
import time

from cockpit import spend, spend_worker
from tests.test_spend import COPY, NOW, SESSION, call, prompt, subagent, transcript, usage


def wait_ready(index):
    until = time.monotonic() + 10
    while index.ready_at is None and time.monotonic() < until:
        time.sleep(0.01)
    assert index.ready_at is not None


def test_child_publishes_the_same_usage_models_and_duplicate_accounting(tmp_path):
    entries = [prompt(0, "Start"), call("reply", 1, usage(fresh=1000, output=200))]
    transcript(tmp_path, SESSION, entries)
    transcript(tmp_path, COPY, entries)
    subagent(tmp_path, SESSION, "helper", [call("helper-reply", 2, usage(fresh=500, output=50))])
    expected = spend.SpendIndex(tmp_path)
    expected.refresh(NOW)
    actual = spend.SpendIndex(tmp_path)
    worker = spend_worker.start(actual)
    try:
        assert worker.process.pid != os.getpid()
        wait_ready(actual)
        assert actual.per_session(NOW) == expected.per_session(NOW)
        assert actual.latest_models() == expected.latest_models()
        actual_summary = actual.summary(NOW.replace(hour=0))
        expected_summary = expected.summary(NOW.replace(hour=0))
        assert actual_summary | {"ready_at": None} == expected_summary | {"ready_at": None}
        assert actual._files == expected._files
    finally:
        worker.stop()
    assert not worker.process.is_alive()
    assert not worker.thread.is_alive()


def test_child_parser_warnings_reach_the_servers_log(tmp_path, caplog):
    path = transcript(tmp_path, SESSION, [])
    path.write_text('{"type":"user","message":[\n')
    actual = spend.SpendIndex(tmp_path)
    with caplog.at_level(logging.WARNING):
        worker = spend_worker.start(actual)
        try:
            wait_ready(actual)
            assert "not one transcript entry" in caplog.text
        finally:
            worker.stop()


def test_an_unexpected_child_exit_is_reported_and_keeps_the_received_usage(tmp_path, caplog):
    transcript(tmp_path, SESSION, [call("reply", 1, usage(fresh=1000, output=200))])
    actual = spend.SpendIndex(tmp_path)
    worker = spend_worker.start(actual)
    try:
        wait_ready(actual)
        before = actual.per_session(NOW)
        with caplog.at_level(logging.ERROR):
            worker.process.terminate()
            worker.process.join(5)
            worker.thread.join(5)
            assert "usage index worker stopped" in caplog.text
        assert actual.per_session(NOW) == before
    finally:
        worker.stop()
