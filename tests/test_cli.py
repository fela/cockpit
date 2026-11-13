from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cockpit import cli, config, db, spec
from tests.conftest import ORIGIN, PUBLIC_ORIGIN, SESSION_A, SESSION_B, World


@pytest.mark.parametrize("method,path", [("GET", "/api/state"), ("POST", "/api/permissions/requests")])
def test_loopback_requests_bypass_http_proxy(monkeypatch, method, path):
    from io import BytesIO
    from urllib.response import addinfourl

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.test:15432")
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setattr(cli.urllib.request, "_opener", None)
    monkeypatch.setattr(cli, "read_or_create_secret", lambda path: "private-test-token")
    monkeypatch.setattr(config, "api_base", lambda: "http://127.0.0.1:8790")
    seen = []

    def http_open(handler, request):
        seen.append(request.host)
        assert request.host == "127.0.0.1:8790"
        assert request.get_header("Authorization") == "Bearer private-test-token"
        response = addinfourl(BytesIO(b'{"ok": true}'), {}, request.full_url, 200)
        response.msg = "OK"
        return response

    monkeypatch.setattr(cli.urllib.request.HTTPHandler, "http_open", http_open)
    assert cli.call(method, path, None if method == "GET" else {}, "test") == {"ok": True}
    assert seen == ["127.0.0.1:8790"]


@pytest.mark.parametrize("contents", [None, "name = [", 'name = "Sam"'])
@pytest.mark.parametrize("args", [["--help"], ["item", "put", "--help"], ["step", "put", "--help"]])
def test_help_works_without_a_valid_setup(tmp_path, monkeypatch, capsys, contents, args):
    path = tmp_path / "setup.toml"
    if contents is not None:
        path.write_text(contents)
    monkeypatch.setattr(config, "SETUP_PATH", path)
    config.setup.cache_clear()
    try:
        with pytest.raises(SystemExit) as result:
            cli.main(args)
        assert result.value.code == 0
        assert "the user" in capsys.readouterr().out
        monkeypatch.setattr(cli, "read_or_create_secret", lambda path: "test token")
        with pytest.raises(config.SetupError):
            cli.main(["get"])
    finally:
        config.setup.cache_clear()


def test_summary_and_lean_json(world: World) -> None:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    agent.put("/api/items", json={"key": "note:a", "title": "A", "stage": "building"}, headers=world.agent())
    step = agent.post(
        "/api/steps", json={"item_key": "note:a", "need": "decide", "ask": "Pick **A**", "body": "*context*",
                            "details": [{"title": "More", "body": "x"}],
                            "command": {"run": "true", "cwd": "/tmp", "explain": "Does *nothing*."}},
        headers=world.agent(),
    ).json()["step"]
    world.client.post(f"/api/steps/{step['id']}/timer/start", headers=world.user())
    state = world.client.get("/api/state").json()
    text = cli.summary(state)
    assert f"1. Pick **A** SAM IS ON IT (changes to it wait until Sam pauses)\n     step {step['id']}, score" in text
    lean = cli.lean(state)
    queued = lean["queue"][0]
    assert "item" not in queued and "body_html" not in queued and "ask_html" not in queued
    assert queued["details"] == [{"title": "More", "body": "x"}]
    # The command as put, so a put can send it back unchanged.
    assert queued["command"] == {"run": "true", "cwd": "/tmp", "explain": "Does *nothing*."}
    resent = agent.post("/api/steps", json={"item_key": "note:a", "need": "decide", "command": queued["command"]}, headers=world.agent())
    assert resent.status_code == 200, resent.text
    assert all("last_message_html" not in session for session in lean["waiting_sessions"])


def test_get_lists_items_user_added_first_while_they_wait_for_the_sweeper(world: World) -> None:
    head = (
        "Items Sam added whose session did not start. Add normally starts one. Start one for each"
        " with `cockpit send --item ID --new-in CHECKOUT`, with Sam's words as the message."
    )
    assert head not in cli.summary(world.client.get("/api/state").json())

    def add(text: str) -> dict:
        return world.client.post("/api/items/add", json={"text": text, "part": None, "pinned": False}, headers=world.user()).json()

    first = add("Call the bank")
    second = add("Approve the OAuth app\nin the console")
    handled = add("Renew passport")
    image = world.client.post(f"/api/items/{second['id']}/images", content=b"GIF89a" + b"\x00" * 8,
                              headers=world.user() | {"content-type": "image/gif"}).json()
    agent = TestClient(world.client.app, base_url=ORIGIN)
    agent.put("/api/items", json={"key": handled["key"], "sessions": [SESSION_A]}, headers=world.agent())

    text = cli.summary(world.client.get("/api/state").json())
    block = (
        f"\n\n{head}\n"
        f"  item {first['id']} {first['key']}:\n    Call the bank\n"
        f"  item {second['id']} {second['key']}; images {image['id']}:\n    Approve the OAuth app\n    in the console\n"
        "\nQueue (Sam's order):"
    )
    assert block in text
    assert "Renew passport" not in text.split("Queue (Sam's order):")[0]


def test_get_lists_work_no_one_moves_that_is_not_an_untriaged_to_do(world: World) -> None:
    head = "Work no one has the next move on: nothing in its wait list and no step for Sam."
    agent = TestClient(world.client.app, base_url=ORIGIN)
    agent.put("/api/items", json={"key": "note:left", "title": "Fix the test hang", "stage": "planning"}, headers=world.agent())
    agent.put("/api/items", json={"key": "note:moving", "title": "Build exports", "stage": "building", "sessions": [SESSION_A]},
              headers=world.agent())
    world.client.post("/api/items/add", json={"text": "Call the bank", "part": None, "pinned": False}, headers=world.user())
    state = world.client.get("/api/state").json()
    left = next(item for item in state["items"] if item["key"] == "note:left")
    assert left["wait"] == "stuck" and left["tip"] == "no next move"

    listed = cli.summary(state).split(head)[1].split("\n\n")[0]
    assert f"  item {left['id']} note:left Fix the test hang (normal, stage planning)\n    sessions: none; unchanged" in listed
    # Work with a session is the agents', and the untriaged to-do has its own list above.
    assert "Build exports" not in listed and "Call the bank" not in listed
    # A step the user snoozed is the item's next move.
    agent.put("/api/items", json={"key": "note:later", "title": "Clean up old notes"}, headers=world.agent())
    later = agent.post("/api/steps", json={"item_key": "note:later", "need": "define", "ask": "Which notes go?"},
                       headers=world.agent()).json()["step"]
    world.client.post(f"/api/steps/{later['id']}/snooze", json={"until": "2099-01-01T08:00:00Z"}, headers=world.user())
    state = world.client.get("/api/state").json()
    assert next(item for item in state["items"] if item["key"] == "note:later")["ball"] == "nobody"
    assert "Clean up old notes" not in cli.summary(state).split(head)[1].split("\n\n")[0]
    agent.put("/api/items", json={"key": "note:left", "waiting_on": [{"kind": "bot", "who": "review-bot", "until": "2026-02-05T00:00:00Z"}]},
              headers=world.agent())
    assert head not in cli.summary(world.client.get("/api/state").json())


def test_item_by_key_lists_every_step(world: World) -> None:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    agent.put("/api/items", json={"key": "gh:example-org/app#40", "title": "PR 40"}, headers=world.agent())
    for need in ("merge", "review:abc"):  # put and closed in turn, as one task shows one card (SPEC NEXT-55)
        put = agent.post("/api/steps", json={"item_key": "gh:example-org/app#40", "need": need, "ask": need, "answers": ["Go ahead"]},
                         headers=world.agent())
        assert put.status_code == 200, put.text
        if need == "merge":
            world.client.post(f"/api/steps/{put.json()['step']['id']}/dismiss", json={}, headers=world.user())
    item = agent.get("/api/items", params={"key": "gh:example-org/app#40"}, headers=world.agent()).json()
    assert [(step["need"], step["status"]) for step in item["steps"]] == [("merge", "dismissed"), ("review:abc", "open")]
    assert agent.get("/api/items", params={"key": "note:none"}, headers=world.agent()).status_code == 404


def route_cli_to(world: World, monkeypatch) -> None:
    """Send the CLI's HTTP calls to the test app as an agent."""
    agent = TestClient(world.client.app, base_url=ORIGIN)

    def fetch(method: str, path: str, data: tuple[bytes, str] | None, name: str | None) -> tuple[bytes, str]:
        headers = world.agent(name or "reader")
        if data is not None:
            headers = headers | {"content-type": data[1]}
        response = agent.request(method, path, content=data[0] if data else None, headers=headers)
        if response.status_code >= 400:
            raise cli.CockpitError(f"{method} {path}: HTTP {response.status_code}: {response.json()['detail']}")
        return response.content, response.headers.get("content-type", "")

    monkeypatch.setattr(cli, "fetch", fetch)


def test_feedback_mark_read_by_ids_leaves_the_rest_unread(world: World, monkeypatch, capsys) -> None:
    ids = [
        world.client.post(
            "/api/feedback", json={"text": text, "target": target, "quote": "", "detail": "", "step_id": None, "item_id": None, "seconds": 40},
            headers=world.user(),
        ).json()["id"]
        for text, target in (("general", ""), ("on a row", "spec:ONE-1"))
    ]
    route_cli_to(world, monkeypatch)
    assert cli.main(["feedback", "--mark-read", "spec-example", "--ids", str(ids[1])]) == 0
    unread = world.client.get("/api/feedback").json()
    assert [entry["id"] for entry in unread] == [ids[0]]
    assert cli.main(["feedback", "--mark-read", "spec-example", "--ids", str(ids[1])]) == 1
    assert "not unread feedback" in capsys.readouterr().err
    assert cli.main(["feedback", "--mark-read", "spec-example"]) == 1


def test_spec_get_edit_and_put_go_through_the_server(world: World, tmp_path: Path, monkeypatch) -> None:
    route_cli_to(world, monkeypatch)
    out = tmp_path / "spec.md"
    version = cli.main(["spec", "get", "--out", str(out)]) == 0 and spec.version(out.read_text())
    row = next(line for line in out.read_text().splitlines() if line.startswith("| ONE-1"))
    (tmp_path / "old").write_text(row + "\n")
    (tmp_path / "new").write_text(row.replace("| Should |", "| Must |"))
    assert cli.main(["spec", "edit", "--as", "spec-example", "--old-file", str(tmp_path / "old"),
                     "--new-file", str(tmp_path / "new")]) == 0
    assert "| Must |" in (world.spec_repo / "docs" / "SPEC.md").read_text()
    assert cli.main(["spec", "put", "--as", "spec-example", "--version", version, str(out)]) == 1


def test_spec_edit_says_pushed_only_when_it_pushed(world: World, tmp_path: Path, monkeypatch, capsys) -> None:
    route_cli_to(world, monkeypatch)
    row = next(line for line in (world.spec_repo / "docs" / "SPEC.md").read_text().splitlines() if line.startswith("| ONE-1"))
    (tmp_path / "old").write_text(row + "\n")
    (tmp_path / "new").write_text(row.replace("| Should |", "| Must |"))
    edit = ["spec", "edit", "--as", "spec-example", "--old-file", str(tmp_path / "old"), "--new-file", str(tmp_path / "new")]
    assert cli.main(edit) == 0
    assert "pushed to the deploy branch" in capsys.readouterr().out
    # With deploy turned off the commit stays in the checkout (spec.spec_doc), and the CLI says so.
    (tmp_path / "old").write_text(row.replace("| Should |", "| Must |") + "\n")
    (tmp_path / "new").write_text(row)
    deployed = spec.spec_doc
    monkeypatch.setattr(spec, "spec_doc", lambda repo, branch: deployed(repo, ""))
    assert cli.main(edit) == 0
    printed = capsys.readouterr().out
    assert "nothing was pushed" in printed and "pushed to the deploy branch" not in printed


def test_goals_prints_user_s_goals_most_important_first(world: World, monkeypatch, capsys) -> None:
    route_cli_to(world, monkeypatch)
    assert cli.main(["goals"]) == 0
    assert capsys.readouterr().out == "(no goals from Sam)\n"
    for text in ("Lists first", "Then the export PRs,\nmost of them"):
        world.client.post("/api/goals", json={"text": text}, headers=world.user())
    world.client.post("/api/goals/1/pause", headers=world.user())
    # The pause lands in the same second as the add in a fast run, and a goal changed when it was added prints no
    # "last changed"; the pause gets a later time of its own.
    conn = db.connect(world.db_path)
    try:
        conn.execute("UPDATE events SET at = '2099-01-01T00:00:00Z' WHERE kind = 'goal.paused'")
    finally:
        conn.close()
    assert cli.main(["goals"]) == 0
    stamp = r"\d{4}-\d\d-\d\d \d\d:\d\d UTC"
    expected = (
        rf"1\. Goal 2, added {stamp}:\nThen the export PRs,\nmost of them\n\n"
        rf"Goal 1 \(paused by Sam: leave it aside until they resume it\), added {stamp},"
        r" last changed 2099-01-01 00:00 UTC:\n"
        "Lists first\n"
    )
    assert re.fullmatch(expected, capsys.readouterr().out)


def test_step_checked_records_the_note(world: World, monkeypatch) -> None:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    agent.put("/api/items", json={"key": "note:a", "title": "A"}, headers=world.agent())
    step = agent.post("/api/steps", json={"item_key": "note:a", "need": "n", "ask": "Ask"}, headers=world.agent()).json()
    route_cli_to(world, monkeypatch)
    assert cli.main(["step", "checked", str(step["step"]["id"]), "--as", "sweep", "--note", "PR open, CI green"]) == 0
    shown = world.client.get("/api/state").json()["queue"][0]
    assert (shown["checked_by"], shown["check_note"]) == ("agent:sweep", "PR open, CI green")


def test_image_saves_an_image_user_attached_to_a_file(world: World, tmp_path: Path, monkeypatch, capsys) -> None:
    item = world.client.post("/api/items/add", json={"text": "Fill in the form", "part": None, "pinned": False}, headers=world.user()).json()
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    image = world.client.post(f"/api/items/{item['id']}/images", content=png,
                              headers=world.user() | {"content-type": "image/png"}).json()
    route_cli_to(world, monkeypatch)
    out = tmp_path / "shot.png"
    assert cli.main(["image", "get", str(image["id"]), "--out", str(out)]) == 0
    assert out.read_bytes() == png
    assert capsys.readouterr().out == f"wrote image {image['id']} (image/png, {len(png)} bytes) to {out}\n"
    assert cli.main(["image", "get", "999", "--out", str(tmp_path / "none.png")]) == 1
    assert capsys.readouterr().err == "cockpit: GET /api/item-images/999: HTTP 404: image 999 does not exist\n"
    assert not (tmp_path / "none.png").exists()
    assert cli.main(["item", "get", item["key"]]) == 0
    assert json.loads(capsys.readouterr().out)["images"] == [{key: image[key] for key in image if key != "item_id"}]


def test_image_add_sends_a_file_by_its_signature_and_remove_takes_back_your_own(
    world: World, tmp_path: Path, monkeypatch, capsys
) -> None:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    item = agent.put("/api/items", json={"key": "gh:example-org/app#40", "title": "PR 40"},
                     headers=world.agent()).json()
    route_cli_to(world, monkeypatch)
    graph = tmp_path / "graph"  # no extension: the first bytes decide the type
    graph.write_bytes(b"RIFF\x18\x00\x00\x00WEBPVP8 " + b"\x00" * 16)
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 24)  # a JPEG, whatever its name says

    assert cli.main(["image", "add", item["key"], "--file", str(graph), "--as", "app-pr"]) == 0
    by_key = int(capsys.readouterr().out)
    assert cli.main(["image", "add", str(item["id"]), "--file", str(shot), "--as", "app-pr"]) == 0
    by_id = int(capsys.readouterr().out)
    images = agent.get("/api/items", params={"key": item["key"]}, headers=world.agent()).json()["images"]
    assert [(image["id"], image["content_type"], image["added_by"]) for image in images] == [
        (by_key, "image/webp", "agent:app-pr"), (by_id, "image/jpeg", "agent:app-pr"),
    ]

    # Anything else is refused before it is sent.
    svg = tmp_path / "chart.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    sent = []
    monkeypatch.setattr(cli, "fetch", lambda *args: sent.append(args))
    for path in (svg, tmp_path / "missing.png"):
        assert cli.main(["image", "add", item["key"], "--file", str(path), "--as", "app-pr"]) == 1
    assert capsys.readouterr().err == (
        f"cockpit: {svg} is not a PNG, JPEG, GIF or WebP file, the image types Cockpit keeps\n"
        f"cockpit: cannot read {tmp_path / 'missing.png'}: No such file or directory\n"
    )
    assert sent == []

    route_cli_to(world, monkeypatch)
    assert cli.main(["image", "add", "note:none", "--file", str(graph), "--as", "app-pr"]) == 1
    assert "HTTP 404: no item with key note:none" in capsys.readouterr().err
    assert cli.main(["image", "remove", str(by_key), "--as", "sweep"]) == 1
    assert "HTTP 403" in capsys.readouterr().err
    assert cli.main(["image", "remove", str(by_key), "--as", "app-pr"]) == 0
    assert capsys.readouterr().out == f"removed image {by_key} from item {item['id']}\n"
    images = agent.get("/api/items", params={"key": item["key"]}, headers=world.agent()).json()["images"]
    assert [image["id"] for image in images] == [by_id]


def test_page_add_update_get_and_remove(world: World, tmp_path: Path, monkeypatch, capsys) -> None:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    item = agent.put("/api/items", json={"key": "gh:example-org/app#40", "title": "PR 40"},
                     headers=world.agent()).json()
    route_cli_to(world, monkeypatch)
    report = tmp_path / "report.html"
    report.write_text("<!doctype html><title>R</title><p>Größe ✓</p>")

    assert cli.main(["page", "add", item["key"], "--file", str(report), "--title", "Plain words", "--as", "app-pr"]) == 0
    page_id = page_id_in(capsys.readouterr().out)
    report.write_text("<!doctype html><p>second version</p>")
    assert cli.main(["page", "update", str(page_id), "--file", str(report), "--title", "Plain words, v2",
                     "--as", "app-pr"]) == 0
    assert capsys.readouterr().out == f"updated page {page_id}: {ORIGIN}/pages/{page_id}\n"
    out = tmp_path / "copy.html"
    assert cli.main(["page", "get", str(page_id), "--out", str(out)]) == 0
    assert out.read_text() == "<!doctype html><p>second version</p>"
    pages = agent.get("/api/items", params={"key": item["key"]}, headers=world.agent()).json()["pages"]
    assert [(page["id"], page["title"], page["added_by"]) for page in pages] == [(page_id, "Plain words, v2", "agent:app-pr")]

    assert cli.main(["page", "add", "note:none", "--file", str(report), "--title", "x", "--as", "app-pr"]) == 1
    assert "HTTP 404: no item with key note:none" in capsys.readouterr().err
    assert cli.main(["page", "add", item["key"], "--file", str(tmp_path / "missing.html"), "--title", "x",
                     "--as", "app-pr"]) == 1
    assert "cannot read" in capsys.readouterr().err
    assert cli.main(["page", "remove", str(page_id), "--as", "app-pr"]) == 0
    assert capsys.readouterr().out == f"removed page {page_id} from item {item['id']}\n"


def test_page_unpublish_stops_sharing_a_page(world: World, tmp_path: Path, monkeypatch, capsys) -> None:
    agent = TestClient(world.client.app, base_url=ORIGIN)
    item = agent.put("/api/items", json={"key": "gh:example-org/app#41", "title": "PR 41"},
                     headers=world.agent()).json()
    route_cli_to(world, monkeypatch)
    report = tmp_path / "report.html"
    report.write_text("<!doctype html><p>first</p>")
    assert cli.main(["page", "add", item["key"], "--file", str(report), "--title", "Plain", "--as", "app-pr"]) == 0
    page_id = page_id_in(capsys.readouterr().out)
    public = world.client.post(f"/api/item-pages/{page_id}/public", json={"revision": 1},
                               headers=world.user()).json()["public_url"]

    report.write_text("<!doctype html><p>second</p>")
    assert cli.main(["page", "update", str(page_id), "--file", str(report), "--title", "Plain", "--as", "app-pr"]) == 1
    assert "page unpublish" in capsys.readouterr().err
    assert cli.main(["page", "unpublish", str(page_id), "--as", "app-pr"]) == 0
    assert capsys.readouterr().out == f"page {page_id} is shared with nobody now; its link stops working until it is shared again\n"
    visitor = TestClient(world.client.app, base_url=PUBLIC_ORIGIN)
    assert visitor.get(public.removeprefix(PUBLIC_ORIGIN)).status_code == 404
    assert cli.main(["page", "update", str(page_id), "--file", str(report), "--title", "Plain", "--as", "app-pr"]) == 0


def page_id_in(out: str) -> int:
    """The id in `page <id>: <origin>/pages/<id>`, checking the link names the same id."""
    match = re.fullmatch(rf"page (\d+): {re.escape(ORIGIN)}/pages/(\d+)\n", out)
    assert match and match[1] == match[2], out
    return int(match[1])


def test_attach_says_why_listing_the_sessions_failed(monkeypatch, capsys) -> None:
    # What systemd-run prints over an SSH command without the user's login PATH (SPEC SESSION-6).
    failing = ["sh", "-c", "echo 'Failed to find executable claude: No such file or directory' >&2; exit 1"]
    monkeypatch.setattr(cli.sessions, "claude_command", lambda account, args: failing)
    assert cli.main(["attach", "aaaaaaaa"]) == 1
    assert capsys.readouterr().err == (
        "cockpit: listing Claude sessions failed: `claude agents --json --all` exited with code 1. Its output:"
        " Failed to find executable claude: No such file or directory\n"
    )


def test_send_names_the_session_it_comes_from(monkeypatch) -> None:
    # The receiver is told how to reply to that session (SPEC MSG-12).
    posted = []
    monkeypatch.setattr(cli, "call", lambda method, path, body, agent: posted.append(body) or {"state": "sent"})
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_A)
    assert cli.main(["send", "--session", SESSION_A, "--as", "todo-10", "Is it live?"]) == 0
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    assert cli.main(["send", "--session", SESSION_A, "--as", "todo-10", "Is it live?"]) == 0
    monkeypatch.setenv("CODEX_THREAD_ID", SESSION_B)
    assert cli.main(["send", "--session", SESSION_A, "--as", "todo-10", "Is it live?"]) == 0
    assert [body.get("from_session") for body in posted] == [SESSION_A, None, SESSION_B]


def test_readme_example_creates_an_item_and_step_and_reads_them(world, monkeypatch, capsys):
    from io import StringIO

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    bodies = re.findall(r"<<'JSON'\n(.*?)\nJSON", readme, re.DOTALL)
    assert len(bodies) == 2

    def call(method, path, body, agent):
        response = world.client.request(method, path, json=body, headers=world.agent(agent or "example"))
        response.raise_for_status()
        return response.json()

    monkeypatch.setattr(cli, "call", call)
    for kind, body in zip(("item", "step"), bodies, strict=True):
        monkeypatch.setattr(cli.sys, "stdin", StringIO(body))
        assert cli.main([kind, "put", "--as", "helper"]) == 0
    capsys.readouterr()
    assert cli.main(["item", "get", json.loads(bodies[0])["key"]]) == 0
    stored = json.loads(capsys.readouterr().out)
    assert stored["title"] == json.loads(bodies[0])["title"]
    assert stored["steps"][0]["ask"] == json.loads(bodies[1])["ask"]
