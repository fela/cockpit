from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from cockpit import cli, page_errors
from tests.conftest import ORIGIN, World

LOADED = "2026-01-14T10:00:00.000Z"


def report(world: World, message: str, count: int = 1, loaded_at: str = LOADED, place: str = "action step:1") -> Any:
    body = {"page": "/", "place": place, "message": message, "stack": "TypeError\n  at render (app.js:10:5)",
            "count": count, "loaded_at": loaded_at}
    return world.client.post("/api/page-errors", json=body, headers=world.user())


def listing(world: World, include_taken: bool) -> dict[str, Any]:
    return world.client.get(f"/api/page-errors?all={'true' if include_taken else 'false'}").json()


def test_repeats_of_an_error_add_to_one_row(world: World) -> None:
    first = report(world, "Cannot read properties of undefined (reading 'ask') in step 10")
    assert first.status_code == 200
    report(world, "Cannot read properties of undefined (reading 'ask') in step 20", count=40)
    shown = listing(world, False)
    assert shown["total"] == 1
    assert shown["errors"][0]["count"] == 41
    assert "1 errors on Cockpit's pages that no session has taken: `cockpit errors list`" in (
        world.client.get("/api/patterns?days=1").json()["repeats"]
    )


def test_only_the_users_page_reports_errors(world: World) -> None:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    body = {"page": "/", "place": "x", "message": "m", "stack": "", "count": 1, "loaded_at": LOADED}
    assert agent.post("/api/page-errors", json=body, headers=world.agent()).status_code == 403


def test_a_taken_error_opens_again_only_from_a_page_loaded_after_it_was_taken(world: World) -> None:
    error_id = report(world, "boom").json()["id"]
    agent = TestClient(world.client.app, base_url=ORIGIN)
    taken = agent.post("/api/page-errors/take", json={"ids": [error_id], "note": "fix-boom"}, headers=world.agent())
    assert taken.json() == {"taken": 1}
    # A page still open from before runs the old code: its repeats count, and the error stays taken.
    report(world, "boom", loaded_at=LOADED)
    assert listing(world, False)["total"] == 0
    assert listing(world, True)["errors"][0]["count"] == 2
    report(world, "boom", loaded_at="2999-01-01T00:00:00Z")
    reopened = listing(world, False)["errors"][0]
    assert reopened["taken_at"] is None and reopened["taken_note"] == "fix-boom"
    assert "open again; taken before: fix-boom" in cli.page_errors_text(listing(world, False))


def test_taking_an_error_that_is_not_open_is_refused(world: World) -> None:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    refused = agent.post("/api/page-errors/take", json={"ids": [99], "note": "x"}, headers=world.agent())
    assert refused.status_code == 404


def test_the_table_keeps_the_most_recent_errors_and_the_listing_a_few(world: World, monkeypatch: Any) -> None:
    monkeypatch.setattr(page_errors, "KEEP", 5)
    for letter in "abcdefgh":
        report(world, f"error {letter}", place=f"place {letter}")
    shown = listing(world, False)
    assert shown["total"] == 5
    assert shown["errors"][0]["message"] == "error h"
    monkeypatch.setattr(page_errors, "SHOWN", 2)
    text = cli.page_errors_text(listing(world, False))
    assert text.count("Error ") == 2 and text.endswith("and 3 more seen earlier")


def test_the_journal_gets_an_error_once_and_then_at_round_counts(world: World, caplog: Any) -> None:
    caplog.set_level("ERROR", logger="cockpit.page_errors")
    for _ in range(12):
        report(world, "loop")
    lines = [record.getMessage() for record in caplog.records if record.name == "cockpit.page_errors"]
    assert len(lines) == 2
    assert lines[0].startswith("page error on / (action step:1): loop")
    assert "seen 10 times" in lines[1]


def test_a_load_time_without_a_utc_offset_is_refused(world: World) -> None:
    refused = report(world, "boom", loaded_at="2026-01-14T10:00:00")
    assert refused.status_code == 400
    assert "UTC offset" in refused.text
    assert listing(world, True)["total"] == 0
