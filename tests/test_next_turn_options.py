from __future__ import annotations

import json
from dataclasses import replace
import time
from types import SimpleNamespace

import pytest

from cockpit import config, db, delivery, store
from cockpit.codex_agent import CodexError
from tests.conftest import SESSION_A, SESSION_B, make_session, write_prompt
from tests.test_api import agent_client, put_item, put_step
from tests.test_codex_agent import agent as codex_agent_fixture
from tests.test_codex_agent import finish
from tests.test_delivery import save_default
from tests.test_handover import source

agent = codex_agent_fixture


def post(world, text, target):
    response = world.client.post("/api/messages", json={"text": text, "target": target}, headers=world.user())
    assert response.status_code == 200, response.text
    return world.client.get(f"/api/messages/{response.json()['id']}").json()


def test_codex_settings_wait_for_turn_and_resume_with_old_worker_protocol(world, agent):
    first = post(world, "Start", {"new_cwd": str(world.checkout), "provider": "codex"})
    chosen = post(world, "Use more reasoning", {"session_id": SESSION_B, "provider": "codex",
                                               "model": "gpt-6.2-astra", "effort": "xhigh", "speed": "fast"})
    assert chosen["state"] == "queued" and chosen["waits_since"]
    assert not any(method == "thread/unsubscribe" for method, _ in agent.rpc.calls)
    finish(agent)
    world.deliverer.send(chosen["id"])
    methods = [method for method, _ in agent.rpc.calls]
    assert methods.count("thread/start") == 1
    assert methods.index("thread/unsubscribe") < methods.index("thread/resume")
    resumed = next(params for method, params in agent.rpc.calls if method == "thread/resume")
    assert resumed["model"] == "gpt-6.2-astra"
    assert resumed["config"]["model_reasoning_effort"] == "xhigh"
    assert resumed["serviceTier"] == "priority"
    finish(agent)
    post(world, "Keep those choices", {"session_id": SESSION_B})
    conn = db.connect(world.db_path)
    assert dict(conn.execute("SELECT model, effort, speed FROM codex_threads").fetchone()) == {
        "model": "gpt-6.2-astra", "effort": "xhigh", "speed": "fast"}
    conn.close()
    assert first["session_id"] == chosen["session_id"]


def test_claude_provider_change_has_one_prompt_draft_and_ownership_before_call(world, agent):
    item = source(world)
    step = put_step(world, item_key=item["key"], session_id=SESSION_A)["step"]
    original = agent.rpc.request

    def inspect(method, params):
        if method == "turn/start":
            conn = db.connect(world.db_path)
            assert store.get_item(conn, item["id"])["sessions"][0] == SESSION_B
            assert store.get_step(conn, step["id"])["session_id"] == SESSION_B
            conn.close()
            assert params["input"][0]["text"].count("Unique drafted instruction") == 1
            assert "transcript.md" in params["input"][0]["text"]
        return original(method, params)

    agent.rpc.request = inspect
    sent = post(world, "Unique drafted instruction", {"item_id": item["id"], "session_id": SESSION_A,
                                                     "provider": "codex", "model": "gpt-6.2-astra", "effort": "high"})
    assert sent["state"] == "sent" and sent["session_id"] == SESSION_B
    assert sent["text"] == "Unique drafted instruction" and sent["handover_context"]
    assert sum(method == "turn/start" for method, _ in agent.rpc.calls) == 1
    assert not world.runner.calls


@pytest.mark.parametrize("state,tempo,in_flight", [("done", "", ["local_bash"]),
                                                  ("working", "idle", ["session_cron"])])
def test_provider_change_waits_for_background_work(world, agent, state, tempo, in_flight):
    item = source(world)
    world.cache.sessions = [replace(world.cache.sessions[0], pid=12, state=state, status="busy",
                                    tempo=tempo, in_flight=in_flight)]
    queued = post(world, "Continue on Codex", {"item_id": item["id"], "session_id": SESSION_A,
                                              "provider": "codex", "effort": "high"})
    assert queued["state"] == "queued" and queued["waits_since"]
    assert not world.runner.calls and not agent.rpc.calls


@pytest.mark.parametrize("fail_at", ["thread/start", "turn/start"])
@pytest.mark.parametrize("retry_by", ["user", "sweep"])
def test_failed_switch_retries_same_draft_and_context_once(world, agent, fail_at, retry_by):
    item = source(world)
    original = agent.rpc.request

    def failing(method, params):
        if method == fail_at:
            raise CodexError("temporarily unavailable")
        return original(method, params)

    agent.rpc.request = failing
    failed = post(world, "Retry exactly this draft", {"item_id": item["id"], "session_id": SESSION_A,
                                                    "provider": "codex", "effort": "high"})
    assert failed["state"] == "failed" and failed["handover_context"]
    if fail_at == "thread/start":
        old_action = world.client.post(f"/api/sessions/{SESSION_A}/handover", json={"item_id": item["id"]},
                                       headers=world.user())
        assert old_action.status_code == 409 and "draft" in old_action.json()["detail"]
    agent.rpc.request = original
    headers = world.user() if retry_by == "user" else world.agent(retry_by)
    client = world.client if retry_by == "user" else agent_client(world)
    retried = client.post(f"/api/messages/{failed['id']}/retry", headers=headers)
    assert retried.status_code == 200, retried.text
    result = world.client.get(f"/api/messages/{failed['id']}").json()
    assert result["state"] == "sent" and result["text"] == "Retry exactly this draft"
    turns = [params for method, params in agent.rpc.calls if method == "turn/start"]
    assert len(turns) == 1 and turns[0]["input"][0]["text"].count("Retry exactly this draft") == 1
    assert sum(method == "thread/start" for method, _ in agent.rpc.calls) == 1


def test_step_switch_waits_for_take_back_and_retries_existing_message(world, agent):
    item = source(world)
    step = put_step(world, item_key=item["key"], session_id=SESSION_A)["step"]
    response = world.client.post(f"/api/steps/{step['id']}/hand-off", json={
        "text": "Do the next part", "session_id": SESSION_A, "provider": "codex", "effort": "medium"
    }, headers=world.user())
    assert response.status_code == 200, response.text
    message = response.json()
    assert message["take_back_until"] and not agent.rpc.calls
    world.deliverer.send(message["id"])
    assert not agent.rpc.calls
    released = world.client.post(f"/api/messages/{message['id']}/release", headers=world.user())
    assert released.status_code == 200
    world.deliverer.send(message["id"])
    sent = world.client.get(f"/api/messages/{message['id']}").json()
    assert sent["state"] == "sent" and sent["text"] == "Do the next part"
    conn = db.connect(world.db_path)
    assert store.get_step(conn, step["id"])["session_id"] == SESSION_B
    conn.close()


def test_claude_changes_use_commands_without_copy_or_model_turn(world, agent):
    item = source(world)
    stopped = replace(world.cache.sessions[0], effort="high")
    world.cache.sessions = [stopped]
    account = config.account(stopped.account)
    path = account.jobs / stopped.id / "state.json"
    path.parent.mkdir(parents=True)
    flags = ["--model", config.MAIN_MODEL, "--effort", "high"]
    path.write_text(json.dumps({"respawnFlags": flags}))

    def run(account, args, cwd):
        current = world.cache.sessions[0]
        if args[:2] == ["claude", "stop"]:
            world.cache.sessions = [replace(current, pid=None, state="stopped", status="")]
        elif args[:3] == ["claude", "--bg", "--resume"]:
            assert args[3] == SESSION_A and len(args) == 5
            effort = current.effort
            if args[-1].startswith("/"):
                save_default(account, args[-1])
            if args[-1].startswith("/model "):
                flags[1] = args[-1].split()[1]
                path.write_text(json.dumps({"respawnFlags": flags}))
            if args[-1].startswith("/effort "):
                effort = args[-1].split()[1]
            world.cache.sessions = [replace(current, pid=32, state="blocked", status="idle", effort=effort)]
        result = world.runner(account, args, cwd)
        return replace_result(result, "backgrounded · " + SESSION_A[:8])

    world.deliverer.run = run
    account.folder.mkdir(parents=True, exist_ok=True)
    (account.folder / "settings.json").write_text(json.dumps({"model": "opus"}))
    sent = post(world, "The actual requested work", {"item_id": item["id"], "session_id": SESSION_A,
                                                   "provider": "claude", "model": "claude-sonnet-5-5", "effort": "max"})
    assert sent["state"] == "sent" and sent["session_id"] == SESSION_A
    # Neither command changed the user's defaults for new sessions.
    assert json.loads((account.folder / "settings.json").read_text()) == {"model": "opus", "theme": "dark"}
    prompts = [args[-1] for args, _ in world.runner.calls if args[:2] == ["claude", "--bg"]]
    assert prompts[:2] == ["/model claude-sonnet-5-5", "/effort max"]
    assert len(prompts) == 3 and prompts[-1].count("The actual requested work") == 1
    conn = db.connect(world.db_path)
    assert store.chosen_effort(conn, SESSION_A) == "max"
    assert store.started_models(conn)[SESSION_A] == "claude-sonnet-5-5"
    conn.close()


def replace_result(result, stdout):
    result.stdout = stdout
    return result


@pytest.mark.parametrize("startup", [
    "ready", "delayed", "busy", "dialog", "absent", "resume-failure", "stop-timeout",
    "exits-before-ready", "stays-busy", "stopped-then-missing",
])
def test_codex_to_claude_transfers_full_context_before_first_model_prompt(world, agent, monkeypatch, tmp_path, startup):
    started = post(world, "Original Codex work", {"new_cwd": str(world.checkout), "provider": "codex"})
    finish(agent)
    item = put_item(world, sessions=[SESSION_B], stage="building")
    home = tmp_path / "codex-home"
    monkeypatch.setattr(config, "CODEX_HOMES", (home,))
    transcript = home / "sessions" / f"rollout-test-{SESSION_B}.jsonl"
    transcript.parent.mkdir(parents=True)
    original_text = "Full original Codex request\u2028with a line separator\u2029and a paragraph separator\u0085and next line"
    records = [{"type": "response_item", "payload": {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": original_text}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "Continue the unfinished work"}]}}]
    transcript.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n")
    successor = make_session(SESSION_A, pid=42, cwd=str(world.checkout))
    world.deliverer.short_id_to_uuid = lambda account, short_id: SESSION_A
    starting = replace(successor, pid=None, state="stopped", status="")
    busy = replace(successor, state="working", status="busy", tempo="active", job_state="working")
    dialog = replace(busy, state="blocked", status="idle", tempo="blocked")
    stages = {
        "delayed": starting, "busy": busy, "dialog": dialog, "absent": starting,
        "exits-before-ready": busy, "stays-busy": busy, "stopped-then-missing": starting,
    }
    elapsed = 0
    failed_once = False

    def monotonic():
        nonlocal elapsed
        elapsed += 0.01
        return elapsed

    def settle(seconds):
        if seconds == 0:
            return
        if startup == "delayed" and world.cache.sessions == [starting]:
            world.cache.sessions = [busy]
        elif startup in ("delayed", "busy") and world.cache.sessions == [busy]:
            world.cache.sessions = [successor]
        elif startup == "exits-before-ready":
            world.cache.sessions = [starting]
        elif startup == "stopped-then-missing":
            world.cache.sessions = []

    monkeypatch.setattr(delivery, "time", SimpleNamespace(monotonic=monotonic, sleep=settle, time=time.time))
    monkeypatch.setattr(delivery, "RESUME_SETTLE_SECONDS", 0.2)
    monkeypatch.setattr(delivery, "STOP_SETTLE_SECONDS", 0.1)

    def run(account, args, cwd):
        nonlocal failed_once
        if args[:2] == ["claude", "stop"]:
            assert world.cache.sessions[0].turn_over
            if startup == "stop-timeout" and not failed_once:
                failed_once = True
            else:
                world.cache.sessions = [replace(successor, pid=None, state="stopped", status="")]
        elif args[:3] == ["claude", "--bg", "--resume"]:
            assert any(call[:2] == ["claude", "stop"] for call, _ in world.runner.calls)
            conn = db.connect(world.db_path)
            assert store.get_item(conn, item["id"])["sessions"] == [SESSION_A, SESSION_B]
            conn.close()
            assert args[-1].count("Switch back with this message") == 1
            if startup == "resume-failure" and not failed_once:
                failed_once = True
                world.runner.returncode = 1
                result = world.runner(account, args, cwd)
                world.runner.returncode = 0
                return result
            world.cache.sessions = [successor]
        else:
            assert args[-1] == "/effort medium"
            world.cache.sessions = [stages.get(startup, successor)]
            if startup not in ("dialog", "absent"):
                save_default(account, args[-1])
                write_prompt(SESSION_A, args[-1])
        return replace_result(world.runner(account, args, cwd), "backgrounded · " + SESSION_A[:8])

    world.deliverer.run = run
    sent = post(world, "Switch back with this message", {"item_id": item["id"], "session_id": SESSION_B,
                                                      "provider": "claude", "model": config.MAIN_MODEL, "effort": "medium"})
    if startup in ("absent", "exits-before-ready"):
        assert sent["state"] == "failed" and sent["session_id"] == SESSION_A
        assert "has no running process, and Cockpit could not confirm its startup command finished" in sent["detail"]
        assert "The transferred message was not sent" in sent["detail"]
        assert len(world.runner.calls) == 1
        conn = db.connect(world.db_path)
        assert db.kv_get(conn, f"handover_bootstrap:{sent['id']}") is None
        assert store.get_message(conn, sent["id"])["state"] == "failed"
        conn.close()
        later = post(world, "A later reply", {"item_id": item["id"], "session_id": SESSION_A})
        assert later["state"] == "queued"
        assert len(world.runner.calls) == 1
        return
    if startup in ("dialog", "stays-busy", "stopped-then-missing"):
        assert sent["state"] == "queued" and sent["session_id"] == SESSION_A
        assert len(world.runner.calls) == 1
        conn = db.connect(world.db_path)
        assert store.get_item(conn, item["id"])["sessions"] == [SESSION_A, SESSION_B]
        conn.close()
        world.cache.sessions = [successor]
        world.deliverer.send(sent["id"])
        sent = world.client.get(f"/api/messages/{sent['id']}").json()
    elif startup in ("resume-failure", "stop-timeout"):
        assert sent["state"] == "failed" and sent["session_id"] == SESSION_A
        conn = db.connect(world.db_path)
        assert db.kv_get(conn, f"handover_bootstrap:{sent['id']}") == SESSION_A
        conn.close()
        world.cache.sessions = [replace(successor, pid=None, state="stopped", status="")]
        retried = agent_client(world).post(f"/api/messages/{sent['id']}/retry", headers=world.agent("sweep"))
        assert retried.status_code == 200, retried.text
        sent = world.client.get(f"/api/messages/{sent['id']}").json()
    assert sent["state"] == "sent" and sent["session_id"] == SESSION_A, sent
    # The startup command's effort did not become the user's default for new sessions.
    settings = config.ACCOUNTS[0].folder / "settings.json"
    if startup in ("dialog", "absent"):
        assert not settings.exists()
    else:
        assert json.loads(settings.read_text()) == {"theme": "dark"}
    saved = world.db_path.parent / "handovers" / SESSION_B / "transcript.jsonl"
    assert saved.read_text() == transcript.read_text()
    readable = saved.with_suffix(".md").read_text()
    assert original_text in readable and "Continue the unfinished work" in readable
    prompts = [args for args, _ in world.runner.calls if args[:2] == ["claude", "--bg"]]
    assert len(prompts) == 2 + (startup == "resume-failure")
    note = prompts[-1][-1].split("The Codex agent's latest answers (excerpts, oldest first):")
    assert "Full original Codex request" in note[0] and "Continue the unfinished work" in note[1]
    assert started["session_id"] == SESSION_B


def test_explicit_picks_keep_queue_boundaries(world, agent):
    post(world, "Start", {"new_cwd": str(world.checkout), "provider": "codex"})
    older = post(world, "Use the current choices", {"session_id": SESSION_B})
    picked = post(world, "Then use low", {"session_id": SESSION_B, "effort": "low"})
    latest = post(world, "Then continue", {"session_id": SESSION_B})
    finish(agent)
    world.deliverer.send(latest["id"])
    assert world.client.get(f"/api/messages/{latest['id']}").json()["state"] == "queued"
    world.deliverer.send(older["id"])
    assert world.client.get(f"/api/messages/{picked['id']}").json()["state"] == "queued"
    finish(agent)
    world.deliverer.send(picked["id"])
    assert world.client.get(f"/api/messages/{latest['id']}").json()["state"] == "queued"
    finish(agent)
    world.deliverer.send(latest["id"])
    prompts = [params["input"][0]["text"] for method, params in agent.rpc.calls if method == "turn/start"]
    assert len(prompts) == 4

