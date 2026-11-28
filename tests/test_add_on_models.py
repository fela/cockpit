"""The same text on several models (SPEC SESSION-31): Add's "Also on" pick makes one to-do per model, each with its
model in the title and the note that tells its session to work alone."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cockpit import config, db, store
from tests.conftest import ORIGIN, World

TEXT = "Make the export faster\nIt takes a minute on big boards"


@pytest.fixture
def all_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    setup = config.setup()
    monkeypatch.setattr(config, "setup", lambda: dataclasses.replace(setup, providers=("claude", "codex", "gemini")))


def _add(world: World, **fields: Any):
    body = {"text": TEXT, "part": None, "pinned": False} | fields
    return world.client.post("/api/items/add", json=body, headers=world.user())


def _start_options(world: World, item_id: int) -> dict[str, Any] | None:
    conn = db.connect(world.db_path)
    try:
        stored = conn.execute("SELECT start_options FROM items WHERE id = ?", (item_id,)).fetchone()[0]
    finally:
        conn.close()
    return json.loads(stored) if stored else None


def test_each_model_gets_its_own_to_do_with_the_note(world: World, all_agents: None) -> None:
    response = _add(world, provider="claude", model="claude-fable-5-2", effort="max",
                    also=[{"provider": "gemini", "model": None}, {"provider": "codex", "model": "gpt-6.2-sol"}])
    assert response.status_code == 200, response.text
    main = response.json()
    gemini, codex = main["also"]
    assert [each["title"] for each in (main, gemini, codex)] == [
        "Fable 5.2 · Make the export faster", "Gemini 4 Neon · Make the export faster", "GPT-6.2 Sol · Make the export faster"
    ]
    for each in (main, gemini, codex):
        assert each["summary"] == f"{TEXT}\n\n{store.COMPARE_NOTE}"
        assert each["key"] == f"todo-{each['id']}" and each["user_fields"] == ["summary"]
    assert len({main["id"], gemini["id"], codex["id"]}) == 3
    # Each keeps its own picks; the main pick keeps its effort, the others start on their agent's defaults.
    assert _start_options(world, main["id"])["model"] == "claude-fable-5-2"
    assert _start_options(world, main["id"])["effort"] == "max"
    assert _start_options(world, gemini["id"]) == {"provider": "gemini", "model": None, "effort": None,
                                                   "account": None, "speed": None}
    assert _start_options(world, codex["id"])["provider"] == "codex"
    assert _start_options(world, codex["id"])["model"] == "gpt-6.2-sol"
    shown = {item["id"] for item in world.client.get("/api/state").json()["items"]}
    assert {main["id"], gemini["id"], codex["id"]} <= shown


def test_the_main_pick_left_to_the_default_is_named_by_the_default_model(world: World, all_agents: None) -> None:
    main = _add(world, also=[{"provider": "claude", "model": "claude-sonnet-5-5"}]).json()
    assert main["title"] == "Opus 5.5 · Make the export faster"
    assert main["also"][0]["title"] == "Sonnet 5.5 · Make the export faster"


def test_a_plain_add_has_no_note_and_no_also(world: World) -> None:
    added = _add(world).json()
    assert (added["title"], added["summary"]) == ("Make the export faster", TEXT)
    assert "also" not in added


def test_the_note_follows_an_attached_part(world: World, all_agents: None) -> None:
    part = {"target": "page:header/icon", "quote": "", "detail": ""}
    main = _add(world, part=part, also=[{"provider": "gemini", "model": None}]).json()
    assert main["summary"] == (f"{TEXT}\n\nAttached part of the page (page:header/icon), which shows no text"
                               f"\n\n{store.COMPARE_NOTE}")


def test_refused_picks_add_nothing(world: World, all_agents: None) -> None:
    before = len(world.client.get("/api/state").json()["items"])
    refused = [
        {"provider": "claude", "also": [{"provider": "claude", "model": "claude-opus-5-5"}]},  # the same model twice
        {"also": [{"provider": "grok", "model": None}]},  # an agent that is off
        {"also": [{"provider": "codex", "model": "gpt-5"}]},  # a model Codex does not offer
        {"also": [{"provider": "gemini", "model": None}] * 4},  # more than ADD_ALSO_MAX
        {"also": [{"provider": "gemini"}]},  # no model field
    ]
    for fields in refused:
        response = _add(world, **fields)
        assert response.status_code in (400, 422), (fields, response.text)
    assert _add(world, also=[{"provider": "claude", "model": "claude-opus-5-5"}]).json()["detail"] == (
        "the same model is picked twice"
    )
    assert len(world.client.get("/api/state").json()["items"]) == before


def test_the_model_name_does_not_count_toward_the_title_limit(world: World, all_agents: None) -> None:
    # "Opus 5.5 · Make the export faster" is seven words, past the five an agent's title may have; without its
    # model's name it is four, so the session's first step goes through and the title keeps the model.
    main = _add(world, also=[{"provider": "gemini", "model": None}]).json()
    agent = TestClient(world.client.app, base_url=ORIGIN)
    step = {"item_key": main["key"], "need": "read:export", "ask": "export now takes a second", "body": "Checks pass."}
    response = agent.post("/api/steps", json=step, headers=world.agent())
    assert response.status_code == 200, response.text
    # A plain to-do with a title that long is still refused until renamed.
    plain = _add(world, text="Make the big board export much faster").json()
    response = agent.post("/api/steps", json=step | {"item_key": plain["key"]}, headers=world.agent())
    assert response.status_code == 400 and "is still titled" in response.text


def test_words_added_later_keep_the_model_name_out_of_the_title_limit(world: World, all_agents: None) -> None:
    # Add more puts the user's words after the note, so the text no longer ends with it; the item still knows its
    # model, and the session's next step goes through.
    main = _add(world, also=[{"provider": "gemini", "model": None}]).json()
    more = world.client.post(f"/api/items/{main['id']}/add-more", json={"text": "Keep CSV compatible", "images": []},
                             headers=world.user())
    assert more.status_code == 200, more.text
    assert not more.json()["item"]["summary"].endswith(store.COMPARE_NOTE)
    assert more.json()["item"]["compared_on"] == "Opus 5.5"
    agent = TestClient(world.client.app, base_url=ORIGIN)
    step = {"item_key": main["key"], "need": "read:export", "ask": "export now takes a second", "body": "Checks pass."}
    response = agent.post("/api/steps", json=step, headers=world.agent())
    assert response.status_code == 200, response.text


def test_the_migration_finds_comparison_to_dos_made_before_it(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "old.db")
    try:
        for number, script in enumerate(db.MIGRATIONS[:-1], start=1):
            conn.executescript(f"BEGIN; {script}; PRAGMA user_version = {number}; COMMIT;")
        now = "2026-11-24T10:00:00Z"
        rows = [(1, "Gemini 4 Neon · Make it faster", f"Make it faster\n\n{store.COMPARE_NOTE}\n\nKeep CSV"),
                (2, "Plain · title with a dot", "Plain text")]
        for item_id, title, summary in rows:
            conn.execute("INSERT INTO items (id, key, title, summary, todo, created_at, updated_at, updated_by)"
                         " VALUES (?, ?, ?, ?, 1, ?, ?, 'user')", (item_id, f"todo-{item_id}", title, summary, now, now))
        conn.commit()
        db.migrate(conn)
        found = dict(conn.execute("SELECT id, compared_on FROM items").fetchall())
    finally:
        conn.close()
    assert found == {1: "Gemini 4 Neon", 2: None}
