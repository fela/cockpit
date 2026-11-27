from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from cockpit import db, github, store
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


def test_a_pull_link_names_its_pr_and_other_links_name_none() -> None:
    assert github.url_ref("https://github.com/example-org/app/pull/40/files") == "example-org/app#40"
    assert github.url_ref("https://github.com/example-org/app/pull/40#discussion") == "example-org/app#40"
    assert github.url_ref("https://github.com/example-org/app/issues/40") is None
    assert github.url_ref("https://github.com/example-org/app/pull/401x") is None
    # A leading zero would break the batched GraphQL query.
    assert github.url_ref("https://github.com/example-org/app/pull/01") is None


def test_the_open_tasks_pull_links_are_the_ones_whose_state_is_kept(world: World) -> None:
    put(world, "note:linked", links=[PR_LINK, {"label": "ENG-1", "url": "https://linear.app/x/issue/ENG-1"}])
    put(world, "note:written")
    agent = TestClient(world.client.app, base_url=ORIGIN)
    step = {
        "item_key": "note:written", "need": "look", "ask": "look at https://github.com/example-org/ask/pull/2",
        "why": "it follows https://github.com/example-org/why/pull/5",
        "body": "See https://github.com/example-org/web/pull/7 and https://github.com/example-org/web/issues/8.",
        "core_links": [{"label": "api#3", "url": "https://github.com/example-org/api/pull/3"}],
    }
    response = agent.post("/api/steps", json=step, headers=world.agent())
    assert response.status_code == 200, response.text
    put(world, "note:finished", links=[{"label": "old#1", "url": "https://github.com/example-org/old/pull/1"}])
    world.client.put("/api/items", json={"key": "note:finished", "stage": "done"}, headers=world.user())
    conn = db.connect(world.db_path)
    assert store.linked_pulls(conn) == [
        "example-org/api#3", "example-org/app#40", "example-org/ask#2", "example-org/web#7", "example-org/why#5"
    ]


def test_states_are_kept_merged_prs_are_not_asked_again_and_the_page_shows_them(world: World) -> None:
    conn = db.connect(world.db_path)
    refs = [f"example-org/app#{number}" for number in range(1, github.STATE_BATCH + 3)]
    asked: list[list[str]] = []
    backoff = github.StateBackoff()
    answers = {"example-org/app#1": "merged", "example-org/app#2": "draft"}

    def ask(batch: list[str]) -> github.States:
        asked.append(batch)
        return github.States(found={ref: answers.get(ref, "open") for ref in batch}, missing={})

    assert github.check_states(conn, ask, refs, {}, NOW, backoff) == refs
    assert [len(batch) for batch in asked] == [github.STATE_BATCH, 2]
    answers["example-org/app#2"] = "open"
    asked.clear()
    assert github.check_states(conn, ask, refs, {}, NOW, backoff) == ["example-org/app#2"]
    assert "example-org/app#1" not in [ref for batch in asked for ref in batch]
    states = world.client.get("/api/state").json()["pull_states"]
    assert states["example-org/app#1"] == "merged"
    assert states["example-org/app#2"] == "open"


def test_a_pr_github_gives_no_state_for_is_logged_and_asked_again_after_an_hour(world: World, caplog) -> None:
    conn = db.connect(world.db_path)
    failed: dict[str, datetime] = {}
    refs = ["example-org/app#1", "example-org/app#2"]
    backoff = github.StateBackoff()

    def partly(batch: list[str]) -> github.States:
        return github.States(found={"example-org/app#1": "open"}, missing={"example-org/app#2": "it is an issue"})

    assert github.check_states(conn, partly, refs, failed, NOW, backoff) == ["example-org/app#1"]
    assert "asking GitHub the state of example-org/app#2 failed: it is an issue" in caplog.text
    asked: list[list[str]] = []

    def broken(batch: list[str]) -> github.States:
        asked.append(batch)
        raise github.GitHubError("rate limited")

    github.check_states(conn, broken, refs, failed, NOW + timedelta(minutes=59), backoff)
    assert asked == [["example-org/app#1"]]
    assert "asking GitHub the state of example-org/app#1 failed: rate limited" in caplog.text
    asked.clear()
    github.check_states(conn, broken, refs, failed, NOW + github.RETRY_AFTER, backoff)
    assert asked == [["example-org/app#2"]]


def test_githubs_answer_is_read_even_when_gh_exits_nonzero(monkeypatch) -> None:
    answer = {
        "data": {
            "p0": {"issueOrPullRequest": {"__typename": "PullRequest", "state": "OPEN", "isDraft": True}},
            "p1": {"issueOrPullRequest": {"__typename": "PullRequest", "state": "MERGED", "isDraft": False}},
            "p2": {"issueOrPullRequest": {"__typename": "Issue"}},
            "p3": None,
            "p4": {"issueOrPullRequest": {"__typename": "PullRequest", "state": "CLOSED", "isDraft": False}},
        },
        "errors": [{"path": ["p3"], "type": "NOT_FOUND", "message": "Could not resolve the repo gone/RATE_LIMITED"}],
    }
    queries: list[str] = []

    def run(args, **kwargs):
        queries.append(args[-1])
        return subprocess.CompletedProcess(
            args, 1, stdout="HTTP/2.0 200 OK\nContent-Type: application/json\n\n" + json.dumps(answer),
            stderr="gh: some errors",
        )

    monkeypatch.setattr("cockpit.github.subprocess.run", run)
    refs = ["a/b#1", "a/b#2", "a/b#3", "gone/repo#4", "a/b#5"]
    assert github.ask_states(refs) == github.States(
        found={"a/b#1": "draft", "a/b#2": "merged", "a/b#5": "closed"},
        missing={"a/b#3": "it is an issue", "gone/repo#4": "Could not resolve the repo gone/RATE_LIMITED"},
    )
    assert len(queries) == 1
    assert 'p3: repository(owner: "gone", name: "repo") { issueOrPullRequest(number: 4)' in queries[0]
    monkeypatch.setattr(
        "cockpit.github.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, stdout="", stderr="gh: not logged in"),
    )
    with pytest.raises(github.GitHubError, match="not logged in"):
        github.ask_states(refs)


@pytest.mark.parametrize(("failures", "error"), [
    (["timeout"], None),
    (["timeout", "timeout"], "gh took over 30 s twice"),
    ([504], None),
    ([504, 504], "gh: HTTP 504"),
    ([503], None),
    ([504, "timeout"], "gh took over 30 s on the retry"),
    ([403], "gh: HTTP 403"),
    ([400], "gh answered no JSON"),
])
def test_a_pr_state_batch_retries_temporary_failures_once(
    world: World, monkeypatch, caplog, failures: list[int | str], error: str | None,
) -> None:
    conn = db.connect(world.db_path)
    refs = ["example-org/app#1", "example-org/app#2"]
    failed: dict[str, datetime] = {}
    queries: list[list[str]] = []
    backoff = github.StateBackoff()
    pull = {"issueOrPullRequest": {"__typename": "PullRequest", "state": "OPEN", "isDraft": False}}

    def run(args, *, timeout, **kwargs):
        queries.append(args)
        if len(queries) <= len(failures):
            failure = failures[len(queries) - 1]
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, timeout)
            body = '{"message": "Service unavailable"}' if failure == 503 else ""
            # The response status is authoritative even if the CLI diagnostic is absent or misleading.
            diagnostic = {403: "gh: HTTP 403\ngh: HTTP 504", 504: "gh: HTTP 504"}.get(failure, "")
            response = f"HTTP/2.0 {failure}\nSet-Cookie: private-header-marker\n\n{body}"
            return subprocess.CompletedProcess(args, 1, stdout=response, stderr=diagnostic)
        return subprocess.CompletedProcess(
            args, 0, stdout="HTTP/2.0 200 OK\n\n" + json.dumps({"data": {"p0": pull, "p1": pull}}),
        )

    monkeypatch.setattr("cockpit.github.subprocess.run", run)
    assert github.check_states(conn, github.ask_states, refs, failed, NOW, backoff) == ([] if error else refs)
    attempts = len(failures) + (error is None)
    assert queries == [queries[0]] * attempts
    assert "--include" in queries[0]
    if error is None:
        assert failed == {}
        assert github.pull_states(conn) == dict.fromkeys(refs, "open")
        assert "asking GitHub the state" not in caplog.text
    else:
        assert failed == dict.fromkeys(refs, NOW)
        assert error in caplog.text
        assert "private-header-marker" not in caplog.text
        assert github.check_states(conn, github.ask_states, refs, failed, NOW + timedelta(minutes=1), backoff) == []
        assert len(queries) == attempts



@pytest.mark.parametrize(("status", "body", "stderr"), [
    (403, "{}", "gh: You have exceeded a secondary rate limit. (HTTP 403)"),
    (200, '{"errors":[{"message":"You have exceeded a secondary rate limit"}]}', ""),
    (200, '{"errors":[{"type":"RATE_LIMITED","message":"API rate limit exceeded"}]}', ""),
    (429, "", ""),
])
def test_pr_state_throttling_pauses_all_batches_and_new_refs(
    world: World, monkeypatch, caplog, status: int, body: str, stderr: str,
) -> None:
    conn = db.connect(world.db_path)
    first = "example-org/app#1"
    refs = [f"example-org/app#{number}" for number in range(1, github.STATE_BATCH + 3)]
    conn.execute("INSERT INTO pull_states (ref, state, checked_at) VALUES (?, 'draft', 'earlier')", (first,))
    failed: dict[str, datetime] = {}
    backoff = github.StateBackoff()
    clock = 100
    monkeypatch.setattr(github, "monotonic", lambda: clock)
    responses = [
        subprocess.CompletedProcess([], 1, f"HTTP/2.0 {status}\nrEtRy-AfTeR: 90\n\n{body}", stderr),
        subprocess.CompletedProcess([], 1, f"HTTP/2.0 {status}\n\n{body}", stderr),
        subprocess.CompletedProcess([], 0, "HTTP/2.0 200 OK\n\n" + json.dumps({"data": {
            "p0": {"issueOrPullRequest": {"__typename": "PullRequest", "state": "OPEN", "isDraft": False}},
        }}), ""),
    ]
    queries = []

    def run(args, **kwargs):
        queries.append(args)
        return responses.pop(0)

    monkeypatch.setattr(github.subprocess, "run", run)

    assert github.check_states(conn, github.ask_states, refs, failed, NOW, backoff) == []
    assert len(queries) == 1
    assert failed == {}
    assert github.pull_states(conn) == {first: "draft"}
    assert [record.levelname for record in caplog.records] == ["WARNING"]
    assert "retrying after 90 s" in caplog.text

    for clock in (160, 189):
        assert github.check_states(conn, github.ask_states, ["new/repo#1"], failed, NOW, backoff) == []
    assert len(queries) == 1

    clock = 190
    assert github.check_states(conn, github.ask_states, refs, failed, NOW, backoff) == []
    assert len(queries) == 2
    assert "retrying after 120 s" in caplog.text

    clock = 309
    assert github.check_states(conn, github.ask_states, [first], failed, NOW, backoff) == []
    clock = 310
    assert github.check_states(conn, github.ask_states, [first], failed, NOW, backoff) == [first]
    assert github.pull_states(conn) == {first: "open"}
    assert backoff.delay == github.RATE_LIMIT_DELAY


@pytest.mark.parametrize(("headers", "wait"), [
    ("Retry-After: 0\n", 0),
    ("X-RateLimit-Remaining: 0\nX-RateLimit-Reset: {reset}\n", 300),
    ("Retry-After: 90\nX-RateLimit-Remaining: 0\nX-RateLimit-Reset: {reset}\n", 300),
])
def test_pr_state_throttling_uses_retry_evidence_in_a_403(monkeypatch, headers: str, wait: int) -> None:
    monkeypatch.setattr(github, "time", lambda: NOW.timestamp())
    reset = int(NOW.timestamp()) + 300
    monkeypatch.setattr(github.subprocess, "run", lambda args, **kwargs: subprocess.CompletedProcess(
        args, 1, "HTTP/2.0 403 Forbidden\n" + headers.format(reset=reset) + "\n", "",
    ))
    with pytest.raises(github.GitHubRateLimit) as error:
        github.ask_states(["example-org/app#1"])
    assert error.value.retry_after == wait
