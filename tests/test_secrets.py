"""A secret the user hands a session from a step (SPEC MSG-15)."""

from __future__ import annotations

import stat

import pytest

from cockpit import db, delivery, secret_files, store
from tests.conftest import SESSION_A, World
from tests.test_api import agent_client, put_card, put_item, put_step

VALUE = "tasks-test-token-aaaa111"


def hand_secret(world: World, step_id: int, **body) -> dict:
    response = world.client.post(f"/api/steps/{step_id}/secret", headers=world.user(),
                                 json={"name": "tasks-export-key", "value": VALUE, "text": ""} | body)
    assert response.status_code == 200, response.text
    return response.json()


def database_text(world: World) -> str:
    conn = db.connect(world.db_path)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


def test_a_secret_goes_to_a_file_only_the_user_reads_and_the_session_learns_where(world: World) -> None:
    put_item(world, sessions=[SESSION_A])
    step = put_step(world)["step"]
    message = hand_secret(world, step["id"], value=f"  {VALUE}\n", text="Try the Tasks export service")

    folder = world.settings.secrets_dir
    path = folder / "tasks-export-key"
    assert path.read_text() == VALUE
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [entry.name for entry in folder.iterdir()] == ["tasks-export-key"]

    assert message["text"].startswith(f"Sam stored secret tasks-export-key at {path}.")
    assert message["text"].endswith("\n\nTry the Tasks export service")
    assert VALUE not in message["text"]
    # The step went to its session at once: no wait for Take back, since the file cannot be taken back.
    state = world.client.get("/api/state").json()
    assert [entry["id"] for entry in state["with_agents"]] == [step["id"]]
    assert message["take_back_until"] is None
    assert world.runner.calls, "the message was not delivered"
    assert all(VALUE not in " ".join(args) for args, _ in world.runner.calls)
    conn = db.connect(world.db_path)
    try:
        delivered = delivery.compose(conn, store.get_message(conn, message["id"]))
    finally:
        conn.close()
    assert f"Sam stored secret tasks-export-key at {path}" in delivered and VALUE not in delivered
    assert VALUE not in database_text(world)


def test_a_secret_of_the_same_name_replaces_the_file(world: World) -> None:
    put_item(world, sessions=[SESSION_A])
    first = put_step(world, need="first")["step"]
    second = put_card(world, "second", ask="Approve PR 10")["step"]
    hand_secret(world, first["id"])
    message = hand_secret(world, second["id"], value="sk-new")
    assert (world.settings.secrets_dir / "tasks-export-key").read_text() == "sk-new"
    assert message["text"] == store.get_message(db.connect(world.db_path), message["id"])["text"]
    assert [entry.name for entry in world.settings.secrets_dir.iterdir()] == ["tasks-export-key"]


def test_the_folder_is_closed_to_others_also_when_it_was_there_before(world: World) -> None:
    put_item(world, sessions=[SESSION_A])
    step = put_step(world)["step"]
    world.settings.secrets_dir.mkdir(mode=0o755)
    world.settings.secrets_dir.chmod(0o755)
    hand_secret(world, step["id"])
    assert stat.S_IMODE(world.settings.secrets_dir.stat().st_mode) == 0o700


def test_names_are_letters_digits_dashes_and_underscores(world: World) -> None:
    put_item(world, sessions=[SESSION_A])
    step = put_step(world)["step"]
    for name in ("../escape", ".hidden", "a/b", "has space", "", "x" * 65, "dot.ted"):
        refused = world.client.post(f"/api/steps/{step['id']}/secret", headers=world.user(),
                                    json={"name": name, "value": VALUE, "text": ""})
        assert refused.status_code == 400, name
        # The refusal names what is wrong without repeating the value.
        assert VALUE not in refused.text
    assert not world.settings.secrets_dir.exists()
    hand_secret(world, step["id"], name="Tasks_Export-2")
    assert (world.settings.secrets_dir / "Tasks_Export-2").read_text() == VALUE


def test_refusals_leave_no_file_and_no_message(world: World) -> None:
    put_item(world, sessions=[SESSION_A])
    step = put_step(world)["step"]
    url = f"/api/steps/{step['id']}/secret"
    blank = world.client.post(url, headers=world.user(), json={"name": "key", "value": "  \n", "text": ""})
    assert blank.status_code == 400
    # Only the user, on the page, hands a secret.
    agent = agent_client(world).post(url, headers=world.agent(), json={"name": "key", "value": VALUE, "text": ""})
    assert agent.status_code == 403
    hand_secret(world, step["id"])
    # The step is with its session now: a second hand-off is refused, and the secret of that name stays as it was.
    again = world.client.post(url, headers=world.user(), json={"name": "tasks-export-key", "value": "sk-new", "text": ""})
    assert again.status_code == 409
    assert [entry.name for entry in world.settings.secrets_dir.iterdir()] == ["tasks-export-key"]
    assert (world.settings.secrets_dir / "tasks-export-key").read_text() == VALUE
    assert database_text(world).count("stored secret") == 1


def test_a_hand_off_that_fails_after_the_value_took_the_name_puts_the_secret_back(world: World, monkeypatch) -> None:
    put_item(world, sessions=[SESSION_A])
    first = put_step(world, need="first")["step"]
    second = put_card(world, "second", ask="Approve PR 10")["step"]
    hand_secret(world, first["id"])
    publish = secret_files.Staged.publish

    def publish_then_fail(secret: secret_files.Staged) -> None:
        publish(secret)
        raise RuntimeError("the commit failed")

    monkeypatch.setattr(secret_files.Staged, "publish", publish_then_fail)
    for name in ("tasks-export-key", "other"):
        with pytest.raises(RuntimeError):
            world.client.post(f"/api/steps/{second['id']}/secret", headers=world.user(),
                              json={"name": name, "value": "sk-new", "text": ""})
    assert [entry.name for entry in world.settings.secrets_dir.iterdir()] == ["tasks-export-key"]
    assert (world.settings.secrets_dir / "tasks-export-key").read_text() == VALUE
    assert database_text(world).count("stored secret") == 1


def test_a_link_in_the_folders_place_is_refused(world: World, tmp_path) -> None:
    put_item(world, sessions=[SESSION_A])
    step = put_step(world)["step"]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o755)
    elsewhere.chmod(0o755)
    world.settings.secrets_dir.symlink_to(elsewhere)
    with pytest.raises(OSError):
        world.client.post(f"/api/steps/{step['id']}/secret", headers=world.user(),
                          json={"name": "tasks-export-key", "value": VALUE, "text": ""})
    assert list(elsewhere.iterdir()) == [] and stat.S_IMODE(elsewhere.stat().st_mode) == 0o755
    assert "stored secret" not in database_text(world)


def test_a_partial_file_a_stopped_server_left_goes_with_the_next_write(world: World) -> None:
    put_item(world, sessions=[SESSION_A])
    step = put_step(world)["step"]
    world.settings.secrets_dir.mkdir(mode=0o700)
    (world.settings.secrets_dir / ".tasks-export-key.0123.partial").write_text("sk-old")
    hand_secret(world, step["id"])
    assert [entry.name for entry in world.settings.secrets_dir.iterdir()] == ["tasks-export-key"]


def test_a_file_that_cannot_be_written_leaves_the_step_with_the_user(world: World) -> None:
    put_item(world, sessions=[SESSION_A])
    step = put_step(world)["step"]
    # A folder in the file's place: the write fails, and the hand-off's transaction rolls back.
    (world.settings.secrets_dir / "tasks-export-key").mkdir(parents=True)
    with pytest.raises(IsADirectoryError):
        world.client.post(f"/api/steps/{step['id']}/secret", headers=world.user(),
                          json={"name": "tasks-export-key", "value": VALUE, "text": ""})
    state = world.client.get("/api/state").json()
    assert state["with_agents"] == [] and step["id"] in [entry["id"] for entry in state["queue"]]
    assert "stored secret" not in database_text(world)
    assert [entry.name for entry in world.settings.secrets_dir.iterdir()] == ["tasks-export-key"]
