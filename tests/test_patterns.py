from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import ORIGIN, SESSION_A, World


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
    agent.put(
        "/api/items", json={"key": "gh:example-org/app#10", "title": "PR 10", "sessions": [SESSION_A]},
        headers=world.agent(),
    )
    # All three are put first: a new step waits while one on its item is with an agent (SPEC NEXT-37).
    steps = [
        agent.post(
            "/api/steps", json={"item_key": "gh:example-org/app#10", "need": f"decide:{name}", "ask": "Decide"},
            headers=world.agent(),
        ).json()["step"]
        for name in ("a", "b", "c")
    ]
    for step in steps:
        sent = world.client.post(f"/api/steps/{step['id']}/hand-off", json={"text": "Do A"}, headers=world.user())
        assert sent.status_code == 200
    summary = world.client.get("/api/patterns?days=1").json()
    assert not [line for line in summary["repeats"] if "steps" in line]
    decide = next(row for row in summary["by_kind"] if row["name"] == "decide")
    assert decide["handed off"] == 3


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


def test_patterns_need_a_window(world: World) -> None:
    assert world.client.get("/api/patterns").status_code == 400
    assert world.client.get("/api/patterns?days=91").status_code == 400
