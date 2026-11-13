from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from cockpit import cli, db, explain
from tests.conftest import ORIGIN, SESSION_A, World
from tests.test_api import put_card


def test_repeated_dismissals_show_as_a_pattern(world: World) -> None:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    agent.put("/api/items", json={"key": "gh:example-org/app#10", "title": "PR 10"}, headers=world.agent())
    for sha in ("aaaa111", "bbbb222", "cccc333"):
        step = agent.post(
            "/api/steps", json={"item_key": "gh:example-org/app#10", "need": f"review@{sha}", "ask": "Review"},
            headers=world.agent(),
        ).json()["step"]
        world.client.post(f"/api/steps/{step['id']}/dismiss", json={}, headers=world.user())
    summary = world.client.get("/api/patterns?days=14").json()
    assert "3 review steps dismissed by Sam" in summary["repeats"]
    review = next(row for row in summary["by_kind"] if row["name"] == "review")
    assert review["created"] == 3 and review["dismissed"] == 3
    assert summary["by_source"][0]["name"] == "gh"


def test_an_agents_dismissals_count_as_closed_by_agents_not_as_user_dismissing(world: World) -> None:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    agent.put("/api/items", json={"key": "gh:example-org/app#10", "title": "PR 10"}, headers=world.agent())
    for name in ("a", "b", "c"):
        step = agent.post(
            "/api/steps", json={"item_key": "gh:example-org/app#10", "need": f"decide:{name}", "ask": "Decide"},
            headers=world.agent(),
        ).json()["step"]
        agent.post(f"/api/steps/{step['id']}/dismiss", json={}, headers=world.agent())
    summary = world.client.get("/api/patterns?days=1").json()
    assert not [line for line in summary["repeats"] if "dismissed" in line]
    decide = next(row for row in summary["by_kind"] if row["name"] == "decide")
    assert (decide["dismissed"], decide["closed by agents"]) == (0, 3)


def test_hand_offs_count_in_the_table_but_are_no_repeat(world: World) -> None:
    # The user hands off steps that asked them something, so many hand-offs are no sign agents could skip asking.
    agent = TestClient(world.client.app, base_url=ORIGIN)
    # Each step is on a task of its own: a task shows one card at a time (SPEC NEXT-55).
    steps = []
    for name in ("a", "b", "c"):
        key = f"note:{name}"
        agent.put("/api/items", json={"key": key, "title": f"Task {name}", "sessions": [SESSION_A]}, headers=world.agent())
        steps.append(agent.post(
            "/api/steps", json={"item_key": key, "need": f"decide:{name}", "ask": "Decide"}, headers=world.agent(),
        ).json()["step"])
    for step in steps:
        sent = world.client.post(f"/api/steps/{step['id']}/hand-off", json={"text": "Do A"}, headers=world.user())
        assert sent.status_code == 200
    summary = world.client.get("/api/patterns?days=1").json()
    assert not [line for line in summary["repeats"] if "steps" in line]
    decide = next(row for row in summary["by_kind"] if row["name"] == "decide")
    assert decide["handed off"] == 3


def test_steps_taken_to_the_top_count_in_the_table(world: World) -> None:
    # Four steps fill the places at the top, so the fifth waits until the user takes it there.
    for name in ("a", "b", "c", "d"):
        put_card(world, need=f"decide:{name}", ask=f"Decide {name}", score=70)
    waiting = put_card(world, need="decide:e", ask="Decide e", score=10)["step"]
    assert world.client.post(f"/api/steps/{waiting['id']}/take", json={}, headers=world.user()).status_code == 200
    summary = world.client.get("/api/patterns?days=1").json()
    decide = next(row for row in summary["by_kind"] if row["name"] == "decide")
    assert (decide["created"], decide["taken to the top"]) == (5, 1)
    assert summary["by_source"][0]["taken to the top"] == 1
    text = cli.patterns_text(summary)
    assert "withdrawn / reopened / snoozed / taken to the top / handed off" in text
    assert "  decide: 5 / 0 / 0 / 0 / 0 / 0 / 0 / 1 / 0" in text


def test_sweeps_carry_their_summary(world: World) -> None:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    sweep = agent.post("/api/sweeps/start", json={"reorder": False}, headers=world.agent("sweep")).json()
    agent.post(
        "/api/sweeps/finish", json={"sweep_id": sweep["id"], "summary": "Full: 2 steps for Sam", "error": ""},
        headers=world.agent("sweep"),
    )
    sweeps = world.client.get("/api/patterns?days=1").json()["sweeps"]
    assert [(entry["id"], entry["summary"]) for entry in sweeps] == [(sweep["id"], "Full: 2 steps for Sam")]


def test_messages_failing_for_one_reason_show_as_one_plain_repeat(world: World) -> None:
    world.runner.returncode = 1
    for checkout in ("app", "dashboard", "scheduling"):
        world.runner.output = f"Workspace not trusted. Run `claude` in /home/sam/dev/{checkout} once, then retry."
        world.client.post(
            "/api/messages", json={"text": "Take this on", "target": {"new_cwd": str(world.checkout)}},
            headers=world.user(),
        )
    repeats = world.client.get("/api/patterns?days=1").json()["repeats"]
    assert repeats == ["3 messages failed: the workspace was not trusted"]


def blocked(world: World, command: str, reason: str) -> dict:
    body = {
        "kind": "denied", "session_id": SESSION_A, "cwd": "/home/sam/dev/app", "tool_name": "Bash",
        "tool_input": {"command": command}, "tool_use_id": "toolu_1", "reason": reason,
    }
    response = TestClient(world.client.app, base_url=ORIGIN).post(
        "/api/permissions/requests", json=body, headers=world.agent("permission-hook")
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_permission_requests_show_how_each_ended_and_which_stops_repeat(world: World) -> None:
    pushes = [blocked(world, f"git push --force origin user/{name}", "[Git Destructive]") for name in "abc"]
    deploy = blocked(world, "cockpit deploy", "[Production Deploy]")
    withdrawn = TestClient(world.client.app, base_url=ORIGIN).post(
        f"/api/permissions/requests/{pushes[0]['id']}/withdraw", json={"session_id": SESSION_A}, headers=world.agent("fixer")
    )
    assert withdrawn.status_code == 200, withdrawn.text
    dismissed = world.client.post(f"/api/permissions/requests/{pushes[1]['id']}/dismiss", json={}, headers=world.user())
    assert dismissed.status_code == 200, dismissed.text
    told = world.client.post(
        f"/api/permissions/requests/{deploy['id']}/tell", json={"text": "Go ahead with the deploy."}, headers=world.user()
    )
    assert told.status_code == 200, told.text

    report = world.client.get("/api/patterns?days=7").json()
    review = report["permissions"]
    assert [(r["id"], r["stopped_as"], r["state"], r["answered_by"]) for r in review["requests"]] == [
        (pushes[0]["id"], "Git Destructive", "withdrawn", "agent:fixer"),
        (pushes[1]["id"], "Git Destructive", "dismissed", "user"),
        (pushes[2]["id"], "Git Destructive", "open", None),
        (deploy["id"], "Production Deploy", "answered", "user"),
    ]
    assert review["requests"][3]["call"] == "cockpit deploy" and review["requests"][3]["minutes_open"] == 0
    assert review["requests"][2]["minutes_open"] is None and review["requests"][2]["explanation_words"] is None
    assert review["by_state"] == {"withdrawn": 1, "dismissed": 1, "open": 1, "answered": 1}
    assert (review["user_answers"], review["step_presses"], review["codex_requests"]) == (2, 0, 0)
    ids = ", ".join(str(push["id"]) for push in pushes)
    assert (
        f"3 Bash calls stopped by auto mode as “Git Destructive” (1 withdrawn, 1 dismissed, 1 open): requests {ids}"
        in report["repeats"]
    )
    assert not [line for line in report["repeats"] if "Production Deploy" in line]
    text = cli.patterns_text(report)
    assert "Permission requests: 4 (1 withdrawn, 1 dismissed, 1 open, 1 answered)" in text
    assert "Sam answered 2 permission cards and pressed Run or Allow on 0 steps; Codex asked 0 times" in text
    assert f"  {deploy['id']} Bash [Production Deploy] answered by user after 0 min: cockpit deploy" in text


def test_retries_of_the_same_refused_call_count_as_repeats(world: World) -> None:
    tries = [blocked(world, "git push --force origin user/a", "[Git Destructive]") for _ in range(3)]
    other = blocked(world, "git push --force origin user/b", "[Git Destructive]")
    deploys = [blocked(world, "cockpit deploy", "[Production Deploy]") for _ in range(2)]
    assert {attempt["id"] for attempt in tries} == {tries[0]["id"]}

    report = world.client.get("/api/patterns?days=7").json()
    assert [(r["id"], r["times"]) for r in report["permissions"]["requests"]] == [
        (tries[0]["id"], 3), (other["id"], 1), (deploys[0]["id"], 2),
    ]
    assert (
        f"4 Bash calls stopped by auto mode as “Git Destructive” (4 open): requests {tries[0]['id']} ×3, {other['id']}"
        in report["repeats"]
    )
    assert not [line for line in report["repeats"] if "Production Deploy" in line]
    text = cli.patterns_text(report)
    assert f"  {tries[0]['id']} Bash [Git Destructive] ×3 open: git push --force origin user/a" in text
    assert f"  {other['id']} Bash [Git Destructive] open: git push --force origin user/b" in text


def test_a_request_whose_explanation_failed_shows_with_nothing_to_read(world: World) -> None:
    request = blocked(world, "cockpit deploy", "[Production Deploy]")
    conn = db.connect(world.db_path)
    explain.save(conn, request["id"], explain._failure("No explanation yet: Claude's run failed", datetime.now(UTC)))
    conn.commit()
    conn.close()

    report = world.client.get("/api/patterns?days=7")
    assert report.status_code == 200, report.text
    shown = report.json()["permissions"]["requests"][0]
    assert (shown["id"], shown["heading"], shown["explanation_words"]) == (request["id"], None, None)
    assert f"  {request['id']} Bash [Production Deploy] open: cockpit deploy" in cli.patterns_text(report.json())


def test_patterns_need_a_window(world: World) -> None:
    assert world.client.get("/api/patterns").status_code == 400
    assert world.client.get("/api/patterns?days=91").status_code == 400
