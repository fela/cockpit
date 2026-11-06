from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from cockpit import db, github
from tests.conftest import ORIGIN, World

NOW = datetime(2026, 1, 8, 10, 0, tzinfo=UTC)
PR_LINK = {"label": "app#40", "url": "https://github.com/example-org/app/pull/40/files"}


def put(world: World, key: str, **fields) -> None:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    response = agent.put("/api/items", json={"key": key, "title": key} | fields, headers=world.agent())
    assert response.status_code == 200, response.text


def pulls_on_the_page(world: World) -> dict[str, dict | None]:
    return {item["key"]: item["pull"] for item in world.client.get("/api/state").json()["items"]}


def test_an_items_pr_is_its_gh_key_or_else_its_first_pull_link() -> None:
    assert github.pull_ref({"key": "gh:example-org/app#20", "links": [PR_LINK]}) == "example-org/app#20"
    ticket_links = [{"label": "ENG-123", "url": "https://linear.app/x/issue/ENG-123"}, PR_LINK]
    assert github.pull_ref({"key": "linear:ENG-123", "links": ticket_links}) == "example-org/app#40"
    issue_link = [{"label": "an issue", "url": "https://github.com/example-org/app/issues/40"}]
    assert github.pull_ref({"key": "note:x", "links": issue_link}) is None


def test_github_is_asked_once_per_open_pr_and_the_page_tags_users_own(world: World) -> None:
    put(world, "gh:example-org/app#10")
    put(world, "linear:ENG-123", links=[PR_LINK])
    put(world, "gh:example-org/app#20")
    put(world, "gh:example-org/app#30")
    world.client.put("/api/items", json={"key": "gh:example-org/app#30", "stage": "done"}, headers=world.user())
    put(world, "note:plain")
    answers = {
        "example-org/app#10": github.Pull(pull=True, author="user", mine=True),
        "example-org/app#40": github.Pull(pull=True, author="alex-example", mine=False),
        "example-org/app#20": github.Pull(pull=False, author="user", mine=True),
    }
    asked: list[str] = []

    def ask(ref: str) -> github.Pull:
        asked.append(ref)
        return answers[ref]

    conn = db.connect(world.db_path)
    assert pulls_on_the_page(world)["gh:example-org/app#10"] is None
    assert github.check_once(conn, ask, {}, NOW) == sorted(answers)
    assert github.check_once(conn, ask, {}, NOW) == []
    assert sorted(asked) == sorted(answers)
    assert pulls_on_the_page(world) == {
        "gh:example-org/app#10": {"ref": "example-org/app#10", "mine": True},
        "linear:ENG-123": {"ref": "example-org/app#40", "mine": False},
        # An issue is no PR, and the page tags it by its step alone.
        "gh:example-org/app#20": None,
        "gh:example-org/app#30": None,
        "note:plain": None,
    }


def test_a_failed_lookup_is_logged_and_asked_again_after_an_hour(world: World, caplog) -> None:
    put(world, "gh:example-org/app#10")
    calls: list[datetime] = []

    def broken(ref: str) -> github.Pull:
        calls.append(ref)
        raise github.GitHubError("Could not resolve to a Repository")

    conn = db.connect(world.db_path)
    failed: dict[str, datetime] = {}
    assert github.check_once(conn, broken, failed, NOW) == []
    assert "asking GitHub who opened example-org/app#10 failed: Could not resolve to a Repository" in caplog.text
    github.check_once(conn, broken, failed, NOW + timedelta(minutes=59))
    assert len(calls) == 1
    assert github.check_once(conn, lambda ref: github.Pull(pull=True, author="user", mine=True), failed, NOW + github.RETRY_AFTER) == [
        "example-org/app#10"
    ]
    assert failed == {}
