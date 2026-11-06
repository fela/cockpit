"""Failed messages: the plain sentence on what failed, dismiss and restore, and `cockpit get`'s list (SPEC MSG-5)."""

from __future__ import annotations

import json

import pytest

from cockpit import cli, db, store, sweeper
from tests.conftest import SESSION_A, World, make_session
from tests.test_api import agent_client, run_sql
from tests.test_cli import route_cli_to

UNTRUSTED = (
    "Workspace not trusted. Run `claude` in /home/sam/dev/app once and accept the trust prompt, then retry."
)


def gone_once_queued(world: World) -> None:
    """SESSION_A is on this machine when a message to it is queued, and gone by the time it goes out."""
    found = iter([make_session(SESSION_A, pid=None)])
    world.deliverer.lookup = lambda session_id: next(found, None)


def fail_two(world: World) -> tuple[dict, dict]:
    """A new session the user started that hit the trust prompt, and an agent's message to a session that is gone."""
    world.runner.returncode, world.runner.output = 1, UNTRUSTED
    launch = world.client.post(
        "/api/messages", json={"text": "Take this on", "target": {"new_cwd": str(world.checkout)}}, headers=world.user()
    ).json()
    gone_once_queued(world)
    note = agent_client(world).post(
        "/api/messages", json={"text": "CI is green", "target": {"session_id": SESSION_A}},
        headers=world.agent("app-pr"),
    ).json()
    return launch, note


def shown(world: World) -> dict[int, dict]:
    return {message["id"]: message for message in world.client.get("/api/state").json()["messages"]}


def failed_count(world: World) -> int:
    return world.client.get("/api/state").json()["counts"]["failed_messages"]


def test_an_agent_s_message_to_a_session_not_on_this_machine_is_refused_and_names_the_ids_alike(
    world: World,
) -> None:
    """Reject fabricated session IDs before queuing a message, including IDs with a valid short prefix."""
    agent = agent_client(world)
    made_up = SESSION_A[:8] + "-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    refused = agent.post(
        "/api/messages", json={"text": "CI is green", "target": {"session_id": made_up}}, headers=world.agent("app-pr")
    )
    assert refused.status_code == 400
    assert refused.json()["detail"] == (
        f"no session {made_up} on this machine; did you mean {SESSION_A}?"
        " Copy the full id from `cockpit get` or the item's sessions."
    )
    other = "22222222-0000-4000-8000-000000000000"
    refused = agent.post(
        "/api/messages", json={"text": "CI is green", "target": {"session_id": other}}, headers=world.agent("app-pr")
    )
    assert refused.json()["detail"] == (
        f"no session {other} on this machine. Copy the full id from `cockpit get` or the item's sessions."
    )
    assert world.client.get("/api/state").json()["messages"] == []
    assert failed_count(world) == 0
    sent = agent.post(
        "/api/messages", json={"text": "CI is green", "target": {"session_id": SESSION_A}}, headers=world.agent("app-pr")
    )
    assert sent.status_code == 200


def test_each_failed_message_says_what_failed_in_plain_words(world: World) -> None:
    world.cache.sessions = [make_session(SESSION_A, pid=None)]
    launch, note = fail_two(world)
    messages = shown(world)
    assert messages[launch["id"]]["detail"] == f"claude --bg exited 1: {UNTRUSTED}"
    assert messages[launch["id"]]["failure"] == "The new session in app could not start: the workspace was not trusted"
    assert messages[note["id"]]["failure"] == (
        'The message to session "session 1111" was not delivered: Claude has no session with that id on this machine'
    )
    run_sql(world, "UPDATE messages SET state = 'sent' WHERE id = ?", (note["id"],))
    assert shown(world)[note["id"]]["failure"] is None


@pytest.mark.parametrize(
    ("detail", "failure"),
    [
        ("claude --bg --resume exited 1: Workspace not trusted. Run `claude` in /x once",
         "{to} was not delivered: the workspace was not trusted"),
        ("Could not reach the live session's socket (pid 7): [Errno 111] Connection refused",
         "{to} was not delivered: the running session did not take the message"),
        ("The session's directory '/gone' no longer exists, so it cannot be resumed",
         "{to} was not delivered: the session's directory is gone"),
        ("The session was already running, so Claude started a copy (abcd1234) with this message instead of resuming x.",
         "{to} was not delivered: Claude started a copy of the session with it instead"),
        (("Claude started a copy (abcd1234) with this message instead of resuming x (note: session x is already"
          " running in the background, so this started a copy as abcd1234.)."),
         "{to} was not delivered: Claude started a copy of the session with it instead"),
        ("Cockpit restarted before sending this message.", "{to} was not delivered: Cockpit restarted before sending it"),
        ("Delivery uncertain: Cockpit restarted while sending. Check the session before retrying.",
         "{to}: Cockpit could not tell whether it arrived; check the session before retrying"),
        ("claude --bg --resume exited 2: unknown option\nUsage: claude …",
         "{to} was not delivered: claude --bg --resume exited 2: unknown option"),
        ("", "{to} was not delivered: no reason was recorded"),
    ],
)
def test_the_failure_reasons_delivery_records_map_to_plain_words(detail: str, failure: str) -> None:
    message = {"new_cwd": None, "session_id": SESSION_A, "detail": detail}
    assert store.failure_sentence(message, {}) == failure.format(to="The message to session 11111111")
    named = store.failure_sentence(message, {SESSION_A: 'session "copy tweaks"'})
    assert named == failure.format(to='The message to session "copy tweaks"')


def test_a_new_session_that_may_have_started_says_so() -> None:
    message = {
        "new_cwd": "/home/sam/dev/app", "session_id": None,
        "detail": "Delivery uncertain: claude --bg did not finish within 60 s. Check the session before retrying.",
    }
    assert store.failure_sentence(message, {}) == (
        "The new session in app: Cockpit could not tell whether it started; check the session before retrying"
    )


def test_user_and_agents_dismiss_a_failed_message_and_user_restores_it(world: World) -> None:
    launch, note = fail_two(world)
    assert failed_count(world) == 2
    agent = agent_client(world)

    def dismiss(message_id: int, reason, client=agent, headers=None):
        return client.post(f"/api/messages/{message_id}/dismiss", json={"reason": reason},
                           headers=headers if headers is not None else world.agent())

    # An agent says why, in one line; the user's page sends null.
    for reason in (None, "", "  ", "the item has a session\nsince 10:00", "x" * 301):
        assert dismiss(launch["id"], reason).status_code == 400, reason
    assert agent.post(f"/api/messages/{launch['id']}/dismiss", json={}, headers=world.agent()).status_code == 400
    by_agent = dismiss(launch["id"], " the item has a working session since 10:00 ")
    assert by_agent.status_code == 200, by_agent.text
    assert by_agent.json()["state"] == "failed"
    assert (by_agent.json()["dismissed_by"], by_agent.json()["dismiss_reason"]) == (
        "agent:sweep", "the item has a working session since 10:00",
    )
    assert by_agent.json()["dismissed_at"] is not None
    by_user = dismiss(note["id"], None, world.client, world.user())
    assert by_user.status_code == 200
    assert (by_user.json()["dismissed_by"], by_user.json()["dismiss_reason"]) == ("user", None)

    # Both leave the failure count; the state still carries them with their dismissal.
    assert failed_count(world) == 0
    messages = shown(world)
    assert [(messages[m]["state"], messages[m]["dismissed_by"]) for m in (launch["id"], note["id"])] == [
        ("failed", "agent:sweep"), ("failed", "user"),
    ]

    again = dismiss(launch["id"], "still done")
    assert again.status_code == 409 and "already dismissed by agent:sweep" in again.json()["detail"]
    assert dismiss(999, "gone").status_code == 404
    retried = world.client.post(f"/api/messages/{launch['id']}/retry", headers=world.user())
    assert retried.status_code == 409 and "Sam restores it before a retry" in retried.json()["detail"]

    assert agent.post(f"/api/messages/{launch['id']}/restore", headers=world.agent()).status_code == 403
    restored = world.client.post(f"/api/messages/{launch['id']}/restore", headers=world.user())
    assert restored.status_code == 200
    assert [restored.json()[key] for key in ("dismissed_at", "dismissed_by", "dismiss_reason")] == [None, None, None]
    assert world.client.post(f"/api/messages/{launch['id']}/restore", headers=world.user()).status_code == 409
    assert world.client.post("/api/messages/999/restore", headers=world.user()).status_code == 404
    assert failed_count(world) == 1

    # Only a failed message can be dismissed.
    run_sql(world, "UPDATE messages SET state = 'sent' WHERE id = ?", (launch["id"],))
    sent = dismiss(launch["id"], "fine")
    assert sent.status_code == 409
    assert "Only a failed message, or one that waits for its session's turn, can be dismissed." in sent.json()["detail"]

    conn = db.connect(world.db_path)
    try:
        events = [(row["kind"], row["actor"], row["item_id"], json.loads(row["data"])) for row in conn.execute(
            "SELECT * FROM events WHERE kind IN ('message.dismissed', 'message.restored') ORDER BY id"
        )]
    finally:
        conn.close()
    assert events == [
        ("message.dismissed", "agent:sweep", None,
         {"message_id": launch["id"], "reason": "the item has a working session since 10:00"}),
        ("message.dismissed", "user", None, {"message_id": note["id"], "reason": None}),
        ("message.restored", "user", None, {"message_id": launch["id"]}),
    ]
    assert {"message.dismissed", "message.restored"} <= set(sweeper.QUIET_KINDS)


def test_a_dismissed_failure_leaves_the_state_once_30_newer_messages_came(world: World) -> None:
    launch, note = fail_two(world)
    dismissed = agent_client(world).post(
        f"/api/messages/{launch['id']}/dismiss", json={"reason": "the item has a working session"}, headers=world.agent()
    )
    assert dismissed.status_code == 200
    # The user's own messages of the last day all stay (the work list's Your latest message view); one older stays
    # only while it is among the newest.
    run_sql(world, "UPDATE messages SET created_at = '2026-01-04T10:00:00Z' WHERE id = ?", (launch["id"],))
    run_sql(
        world,
        "INSERT INTO messages (author, session_id, text, state, created_at)"
        " WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < ?)"
        " SELECT 'user', ?, 'later', 'sent', '2026-01-05T10:00:00Z' FROM n",
        (store.RECENT_MESSAGES, SESSION_A),
    )
    messages = shown(world)
    assert launch["id"] not in messages
    assert messages[note["id"]]["state"] == "failed" and messages[note["id"]]["dismissed_at"] is None
    assert len(messages) == store.RECENT_MESSAGES + 1


def test_the_user_s_messages_of_the_last_day_all_stay_however_many_came_after(world: World) -> None:
    """Keep every user message from the last day in state so sorting by latest message includes messages beyond the newest thirty."""
    first = world.client.post(
        "/api/messages", json={"text": "Is the export done?", "target": {"session_id": SESSION_A}}, headers=world.user()
    ).json()
    run_sql(
        world,
        "INSERT INTO messages (author, session_id, text, state, created_at)"
        " WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < ?)"
        " SELECT 'agent:sweep', ?, 'a note', 'sent', '2026-01-05T10:00:00Z' FROM n",
        (store.RECENT_MESSAGES, SESSION_A),
    )
    messages = shown(world)
    assert first["id"] in messages
    assert len(messages) == store.RECENT_MESSAGES + 1


def test_cockpit_get_lists_failed_messages_for_the_sweeper_to_dismiss_or_retry(
    world: World, monkeypatch, capsys
) -> None:
    item = world.client.post("/api/items/add", json={"text": "Fix the export", "part": None, "pinned": False}, headers=world.user()).json()
    world.runner.returncode, world.runner.output = 1, UNTRUSTED
    launch = world.client.post(
        "/api/messages", json={"text": "Take this on", "target": {"item_id": item["id"], "new_cwd": str(world.checkout)}},
        headers=world.user(),
    ).json()
    gone_once_queued(world)
    note = agent_client(world).post(
        "/api/messages", json={"text": "CI is green", "target": {"session_id": SESSION_A}},
        headers=world.agent("app-pr"),
    ).json()
    run_sql(world, "UPDATE messages SET created_at = '2026-01-15T10:00:00Z' WHERE id = ?", (launch["id"],))

    text = cli.summary(world.client.get("/api/state").json())
    assert "dismiss it: `cockpit message dismiss ID --reason TEXT --as NAME`" in cli.FAILED_HEAD
    assert "Retry your own with `cockpit message retry ID --as NAME`" in cli.FAILED_HEAD
    lines = text.splitlines()
    head = lines.index(cli.FAILED_HEAD.splitlines()[-1])
    assert lines[head + 1: head + 5] == [
        (
            f"  message {launch['id']} by user, item {item['id']} (Fix the export), to a new session in"
            f" {world.checkout}, 2026-01-15 10:00 UTC:"
        ),
        f"    claude --bg exited 1: {UNTRUSTED}",
        (
            f"  message {note['id']} by agent:app-pr, no item, to session {SESSION_A},"
            f" {note['created_at'][:16].replace('T', ' ')} UTC:"
        ),
        f"    No Claude session {SESSION_A} on this machine",
    ]

    route_cli_to(world, monkeypatch)
    assert cli.main(["message", "dismiss", str(launch["id"]), "--reason", "item has a session since 10:00",
                     "--as", "sweep"]) == 0
    assert capsys.readouterr().out == f"dismissed message {launch['id']}; it no longer counts as failed\n"
    text = cli.summary(world.client.get("/api/state").json())
    assert f"message {launch['id']} " not in text and f"message {note['id']} by agent:app-pr" in text
    assert cli.main(["message", "dismiss", str(launch["id"]), "--reason", "again", "--as", "sweep"]) == 1
    assert "HTTP 409" in capsys.readouterr().err

    # A retry goes out again and waits for the outcome; an agent retries only its own.
    assert cli.main(["message", "retry", str(launch["id"]), "--as", "sweep"]) == 1
    assert "HTTP 403" in capsys.readouterr().err
    assert cli.main(["message", "retry", str(note["id"]), "--as", "app-pr"]) == 1
    assert capsys.readouterr().err == f"cockpit: message {note['id']} failed: No Claude session {SESSION_A} on this machine\n"
    world.client.post(f"/api/messages/{note['id']}/dismiss", json={"reason": None}, headers=world.user())
    assert cli.FAILED_HEAD not in cli.summary(world.client.get("/api/state").json())
