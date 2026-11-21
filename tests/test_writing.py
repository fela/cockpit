"""Writing examples (SPEC FEEDBACK-8): the user marks a step, a page or a part as well written or as hard to
understand; Cockpit keeps a copy with who wrote it, and the page and agents list them."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from cockpit import cli, store
from tests.conftest import ORIGIN, SESSION_A, SESSION_B, World
from tests.test_cli import route_cli_to

ITEM = "note:writing"


def _agent(world: World) -> TestClient:
    return TestClient(world.client.app, base_url=ORIGIN)


def _item_and_step(world: World) -> tuple[int, int]:
    item = _agent(world).put("/api/items", json={"key": ITEM, "title": "Export format", "sessions": [SESSION_A]},
                             headers=world.agent("spend"))
    assert item.status_code == 200, item.text
    body = {"item_key": ITEM, "need": "decide", "ask": "Keep the **text** export?", "why": "It opens in a text editor.",
            "body": "Both include completed tasks.", "details": [{"title": "Numbers", "body": "10 tasks in each file"}], "draft": ""}
    step = _agent(world).post("/api/steps", json=body, headers=world.agent("spend"))
    assert step.status_code == 200, step.text
    return item.json()["id"], step.json()["step"]["id"]


def _mark(world: World, **fields: Any):
    body = {"verdict": "good", "kind": "part", "target": "", "quote": "", "note": "", "step_id": None, "item_id": None,
            "page_id": None, "session_id": None} | fields
    return world.client.post("/api/writing", json=body, headers=world.user())


def test_a_marked_step_keeps_its_markdown_and_the_session_that_wrote_it(world: World) -> None:
    item_id, step_id = _item_and_step(world)
    marked = _mark(world, kind="step", step_id=step_id, note="  Short and plain  ")
    assert marked.status_code == 200, marked.text
    example = marked.json()
    assert example["text"] == (
        "# Keep the **text** export?\n\nIt opens in a text editor.\n\nBoth include completed tasks.\n\n## Numbers\n\n10 tasks in each file"
    )
    assert (example["verdict"], example["kind"], example["format"], example["target"], example["note"]) == (
        "good", "step", "markdown", f"step:{step_id}", "Short and plain")
    assert (example["item_id"], example["item_title"], example["session_id"], example["session_name"]) == (
        item_id, "Export format", SESSION_A, "session 1111")
    assert example["written_by"] == "agent:spend"
    assert "<strong>text</strong>" in example["text_html"]
    assert world.client.get("/api/writing").json() == [example]


def test_a_part_keeps_the_words_the_user_saw_and_the_session_its_message_names(world: World) -> None:
    _, step_id = _item_and_step(world)
    example = _mark(world, target=f"page:next/step {step_id}/session {SESSION_B}/hist-text", quote="Merged; CI green.",
                    step_id=step_id, session_id=SESSION_B).json()
    assert (example["kind"], example["format"], example["text"], example["title"]) == (
        "part", "text", "Merged; CI green.", "Keep the **text** export?")
    # The session the message names wrote it, not the step's last writer.
    assert (example["session_id"], example["written_by"]) == (SESSION_B, "")


def test_a_marked_page_keeps_its_html_as_it_was(world: World) -> None:
    item_id, _ = _item_and_step(world)
    page = _agent(world).post(f"/api/items/{item_id}/pages", json={"title": "Completed tasks in January", "html": "<h1>Completed tasks</h1>"},
                              headers=world.agent("spend")).json()
    example = _mark(world, kind="page", page_id=page["id"], note="Clear charts").json()
    assert (example["kind"], example["format"], example["text"], example["title"], example["target"]) == (
        "page", "html", "<h1>Completed tasks</h1>", "Completed tasks in January", f"page:/pages/{page['id']}")
    assert (example["written_by"], example["session_id"], example["text_html"]) == ("agent:spend", SESSION_A, "")
    updated = _agent(world).put(f"/api/item-pages/{page['id']}", json={"title": "Completed tasks in January", "html": "<h1>New</h1>"},
                                headers=world.agent("spend"))
    assert updated.status_code == 200, updated.text
    assert world.client.get(f"/api/writing/{example['id']}").json()["text"] == "<h1>Completed tasks</h1>"


def test_a_marked_step_credits_who_wrote_its_text_not_who_ranked_it(world: World) -> None:
    _, step_id = _item_and_step(world)
    ranked = _agent(world).post("/api/steps", json={"item_key": ITEM, "need": "decide", "score": 70, "reasons": ["clear"]},
                                headers=world.agent("sweep"))
    assert ranked.status_code == 200, ranked.text
    sized = _agent(world).post("/api/steps", json={"item_key": ITEM, "need": "decide", "size": "small", "progress": 40},
                               headers=world.agent("sizer"))
    assert sized.status_code == 200, sized.text
    # The sweeper's ranking and a put of only the size and progress leave the text as the session wrote it, so the
    # skills counted are the ones loaded by then.
    assert _mark(world, kind="step", step_id=step_id).json()["written_by"] == "agent:spend"


def test_a_marked_step_keeps_the_text_and_writer_of_one_version(world: World, monkeypatch) -> None:
    _, step_id = _item_and_step(world)
    read_step = store.get_step

    # Another agent rewrites the step right after the mark first reads it.
    def rewritten_after(conn: sqlite3.Connection, step: int) -> dict[str, Any]:
        monkeypatch.setattr(store, "get_step", read_step)
        found = read_step(conn, step)
        conn.execute("UPDATE steps SET ask = 'Keep the new export?' WHERE id = ?", (step,))
        conn.execute(
            "INSERT INTO step_versions (step_id, at, actor, ask, why, body, details, draft, answers, command, permission)"
            " SELECT step_id, at, 'agent:rewriter', 'Keep the new export?', why, body, details, draft, answers, command,"
            " permission FROM step_versions WHERE step_id = ? ORDER BY id DESC LIMIT 1", (step,))
        return found

    monkeypatch.setattr(store, "get_step", rewritten_after)
    example = _mark(world, kind="step", step_id=step_id).json()
    assert (example["title"], example["text"].splitlines()[0], example["written_by"]) == (
        "Keep the new export?", "# Keep the new export?", "agent:rewriter")


def _loaded(folder: Path, text: str) -> str:
    return f"Base directory for this skill: {folder}\n\n{text}"


def _md5(folder: Path, text: str) -> str:
    return "md5:" + hashlib.md5(_loaded(folder, text).encode()).hexdigest()[:12]


def _skill_load(at: str, folder: Path, text: str) -> dict[str, Any]:
    return {"type": "user", "isMeta": True, "timestamp": at, "sourceToolUseID": "toolu_1",
            "message": {"role": "user", "content": [{"type": "text", "text": _loaded(folder, text)}]}}


def test_an_example_keeps_the_skills_its_session_had_loaded_when_it_wrote_it(
    world: World, transcripts: Path, tmp_path: Path
) -> None:
    #: an example says which skills, and which version of each, made the writing.
    repo = tmp_path / "skills"
    style = repo / "writing-style"
    style.mkdir(parents=True)
    (style / "SKILL.md").write_text("# Writing style\n")
    old = {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "v1"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=os.environ | old)
    first = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%h"], check=True, capture_output=True, text=True)
    plugin = tmp_path / "plugins" / "frontend-design"  # not in git: its version is the loaded text's MD5
    plugin.mkdir(parents=True)
    carried = {"type": "attachment", "timestamp": "2026-02-01T00:00:00Z", "attachment": {"type": "invoked_skills", "skills": [
        {"name": "writing-style", "path": "userSettings:writing-style", "content": _loaded(style, "# Writing style")},
        {"name": "cockpit", "path": "userSettings:cockpit", "content": _loaded(tmp_path / "cockpit", "# Cockpit")},
    ]}}
    project = transcripts / "-home-sam-dev"
    project.mkdir(parents=True)
    entries = [
        _skill_load("2026-01-15T00:00:00Z", style, "# Writing style"),
        _skill_load("2026-01-16T00:00:00Z", plugin, "# Frontend design"),
        carried,  # after a compaction: writing-style keeps its own load; cockpit was loaded before this transcript
        _skill_load("2999-01-01T00:00:00Z", tmp_path / "later", "# Loaded after the step was written"),
    ]
    (project / f"{SESSION_A}.jsonl").write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    _, step_id = _item_and_step(world)
    example = _mark(world, kind="step", step_id=step_id).json()
    assert example["skills"] == [
        {"name": "writing-style", "folder": str(style), "version": first.stdout.strip(), "loaded_at": "2026-01-15T00:00:00Z"},
        {"name": "frontend-design", "folder": str(plugin), "version": _md5(plugin, "# Frontend design"),
         "loaded_at": "2026-01-16T00:00:00Z"},
        {"name": "cockpit", "folder": str(tmp_path / "cockpit"), "version": _md5(tmp_path / "cockpit", "# Cockpit"),
         "loaded_at": "2026-02-01T00:00:00Z"},
    ]
    assert f"  skills loaded: writing-style {first.stdout.strip()} ({style}), frontend-design" in cli.writing_text([example])
    # A part on a tab names no session, so no skills.
    assert _mark(world, target="page:/usage/spend/p", quote="words").json()["skills"] == []


def test_writing_marked_hard_to_understand_is_kept_the_same_way_with_its_verdict(world: World) -> None:
    item_id, step_id = _item_and_step(world)
    good = _mark(world, kind="step", step_id=step_id).json()
    bad = _mark(world, verdict="bad", kind="step", step_id=step_id, note="Too many terms I do not know").json()
    assert (bad["verdict"], bad["kind"], bad["note"], bad["item_id"], bad["session_id"], bad["written_by"]) == (
        "bad", "step", "Too many terms I do not know", item_id, SESSION_A, "agent:spend")
    assert bad["text"] == good["text"]
    assert world.client.get(f"/api/writing/{bad['id']}").json()["verdict"] == "bad"
    assert _mark(world, verdict="unclear", target="page:/usage/spend/p", quote="words").status_code == 400


def _shown_verdict(world: World, step_id: int) -> str | None:
    state = world.client.get("/api/state").json()
    return next(step for step in state["queue"] + state["ready_next"] if step["id"] == step_id)["writing_verdict"]


def test_a_step_marked_again_on_the_same_text_keeps_only_the_last_verdict(world: World) -> None:
    """Keep the latest writing verdict for unchanged text, including repeated presses."""
    _, step_id = _item_and_step(world)
    assert _shown_verdict(world, step_id) is None
    _mark(world, kind="step", step_id=step_id)
    bad = _mark(world, verdict="bad", kind="step", step_id=step_id).json()
    assert [example["id"] for example in world.client.get("/api/writing").json()] == [bad["id"]]
    assert _shown_verdict(world, step_id) == "bad"
    # A rewrite is new text: it shows no verdict, and a mark on it is kept beside the one on the old text.
    rewritten = _agent(world).post("/api/steps", json={"item_key": ITEM, "need": "decide", "ask": "Keep the new export?"},
                                   headers=world.agent("spend"))
    assert rewritten.status_code == 200, rewritten.text
    assert _shown_verdict(world, step_id) is None
    _mark(world, kind="step", step_id=step_id)
    assert [example["verdict"] for example in world.client.get("/api/writing").json()] == ["good", "bad"]
    assert _shown_verdict(world, step_id) == "good"


def test_pressing_the_pressed_key_again_clears_the_mark_on_the_current_text(world: World) -> None:
    """Pressing the selected writing mark again clears it for the current text."""
    _, step_id = _item_and_step(world)
    _mark(world, kind="step", step_id=step_id)
    rewritten = _agent(world).post("/api/steps", json={"item_key": ITEM, "need": "decide", "ask": "Keep the new export?"},
                                   headers=world.agent("spend"))
    assert rewritten.status_code == 200, rewritten.text
    _mark(world, verdict="bad", kind="step", step_id=step_id)
    assert _agent(world).delete(f"/api/steps/{step_id}/writing", headers=world.agent("sweep")).status_code == 403
    cleared = world.client.delete(f"/api/steps/{step_id}/writing", headers=world.user())
    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == {"removed": 1}
    assert _shown_verdict(world, step_id) is None
    # The mark on the earlier text stays.
    assert [example["verdict"] for example in world.client.get("/api/writing").json()] == ["good"]


def test_a_part_of_another_tab_needs_words_and_only_the_user_marks(world: World) -> None:
    empty = _mark(world, target="page:/usage/whole page", quote="  ")
    assert empty.status_code == 400
    assert "no text" in empty.json()["detail"]
    agent = _agent(world).post("/api/writing", headers=world.agent("sweep"), json={
        "verdict": "good", "kind": "part", "target": "page:/usage/spend", "quote": "words", "note": "", "step_id": None, "item_id": None,
        "page_id": None, "session_id": None})
    assert agent.status_code == 403
    example = _mark(world, target="page:/briefing/summary/p", quote="Three things moved.").json()
    assert (example["title"], example["item_id"], example["session_id"]) == ("Briefing page", None, None)


def test_the_user_removes_an_example(world: World) -> None:
    example = _mark(world, target="page:/usage/spend/p", quote="words").json()
    assert world.client.delete(f"/api/writing/{example['id']}", headers=world.user()).status_code == 200
    assert world.client.get("/api/writing").json() == []
    assert world.client.get(f"/api/writing/{example['id']}").status_code == 404


def test_the_page_serves_the_writing_list(world: World) -> None:
    page = world.client.get("/writing")
    assert page.status_code == 200
    assert "spec.js" in page.text


def test_cli_lists_examples_with_the_transcript_that_shows_how(
    world: World, monkeypatch, capsys, tmp_path: Path
) -> None:
    _, step_id = _item_and_step(world)
    example = _mark(world, kind="step", step_id=step_id, note="Short").json()
    bad = _mark(world, verdict="bad", target="page:/usage/spend/p", quote="Dense words.", note="Lost me").json()
    transcript = tmp_path / "home" / ".claude" / "projects" / "-home-dev" / f"{SESSION_A}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    route_cli_to(world, monkeypatch)
    assert cli.main(["writing", "list"]) == 0
    text = capsys.readouterr().out
    assert f"Example {example['id']}, step, marked {example['created_at']}: Keep the **text** export?" in text
    assert f"  written by session {SESSION_A} (session 1111)\n  transcript: {transcript}\n  Sam's note: Short" in text
    # The good examples come first, then the ones to avoid, each under its heading.
    good_head = text.index("== Good writing: reproduce what the note praises (1) ==")
    bad_head = text.index("== Hard to understand: avoid what made these hard to follow (1) ==")
    assert good_head < text.index(f"Example {example['id']},") < bad_head < text.index(f"Example {bad['id']},")
    assert "  Sam's note: Lost me\n  text (text): Dense words." in text
    assert cli.main(["writing", "get", str(example["id"])]) == 0
    assert capsys.readouterr().out.startswith("# Keep the **text** export?\n\nIt opens in a text editor.")
