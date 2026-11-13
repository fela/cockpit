"""Parts (cockpit.parts): what an install adds to Cockpit's public code, through routes, tags on the main page, fields of
the state and tables of the setup file. A stand-in part here, so these hold with or without the install's own."""

from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from cockpit import cli, config, parts
from cockpit.app import PART_SCRIPTS, PART_STYLES, STATIC_DIR, create_app, with_part_tags
from cockpit.parts import Command, Context, Factory, Part, Tags
from tests.conftest import ORIGIN, World
from tests.test_cli import route_cli_to
from tests.test_setup import write_setup


def stand_in(page_tags: dict[str, Tags], fields: dict[str, Any]) -> tuple[Factory, list[Context]]:
    """A part with one route, /api/stand-in, that answers only the user; `page_tags` and `fields` as given, and the
    caller's being the user among the fields. The list gets the Context its factory was handed."""
    handed: list[Context] = []

    def factory(context: Context) -> Part:
        handed.append(context)

        def register(app: FastAPI) -> None:
            def answer(request: Request) -> dict[str, bool]:
                context.user_only(request)
                return {"answered": True}

            app.add_api_route("/api/stand-in", answer, methods=["GET"])
            # Cockpit's own route for the main page goes in first, so it keeps the path.
            app.add_api_route("/", lambda: {"stand_in": True}, methods=["GET"])

        def state(now: datetime, is_user: bool) -> dict[str, Any]:
            assert now.tzinfo is not None
            return fields | {"stand_in_for_user": is_user}

        return Part(register=register, page_tags=page_tags, state=state)

    return factory, handed


def client(world: World, factories: list[Factory]) -> TestClient:
    app = create_app(world.settings, world.deliverer, world.cache, world.spend_index, world.codex_index, factories)
    browser = TestClient(app, base_url=ORIGIN)
    browser.cookies = world.client.cookies
    return browser


TAGS = Tags(styles=("stand-in.css",), scripts=("stand-in.js",))


def test_with_no_parts_the_page_keeps_neither_marker_nor_tag(world: World) -> None:
    page = with_part_tags((STATIC_DIR / "index.html").read_text(), [])
    assert PART_STYLES not in page and PART_SCRIPTS not in page
    for path in ("/", "/classic"):
        sent = client(world, []).get(path, headers=world.user()).text
        assert "<!-- part" not in sent and "stand-in" not in sent
    assert client(world, []).get("/api/stand-in", headers=world.user()).status_code == 404


def test_a_part_adds_its_route_its_tags_and_its_state_fields(world: World) -> None:
    factory, handed = stand_in({"index.html": TAGS}, {"stand_in": 1})
    browser = client(world, [factory])
    assert handed and handed[0].session_cache is world.cache
    assert browser.get("/api/stand-in", headers=world.user()).json() == {"answered": True}
    assert browser.get("/api/stand-in", headers=world.agent()).status_code == 403
    page = browser.get("/", headers=world.user()).text
    # Versioned as Cockpit's own files are, the stylesheet with the others and the script after the views it adds to,
    # before the script that starts the page.
    style = re.search(r'<link rel="stylesheet" href="/static/stand-in\.css\?v=[^"]+">', page)
    script = re.search(r'<script src="/static/stand-in\.js\?v=[^"]+" defer></script>', page)
    assert style and script, page
    assert page.index("/static/center.js") < script.start() < page.index("/static/app.js")
    assert style.start() < page.index("</head>")
    # The main page stays Cockpit's: its route went in first.
    assert '<body data-layout="sidebar"' in page
    state = browser.get("/api/state", headers=world.user()).json()
    assert (state["stand_in"], state["stand_in_for_user"]) == (1, True)
    assert browser.get("/api/state", headers=world.agent()).json()["stand_in_for_user"] is False


def test_tags_for_a_page_that_takes_none_stop_cockpit_at_start(world: World) -> None:
    factory, _ = stand_in({"missing.html": TAGS}, {})
    with pytest.raises(ValueError, match="missing.html"):
        client(world, [factory])


def test_a_parts_fields_participate_in_state_synchronization(world: World) -> None:
    fields: dict[str, Any] = {"stand_in": 1, "temporary": True}
    factory, _ = stand_in({}, fields)
    browser = client(world, [factory])
    first = browser.get("/api/state", headers=world.user() | {"x-cockpit-sync": "1"})
    assert first.json()["stand_in"] == 1
    cursor = first.headers["x-cockpit-state"]
    fields["stand_in"] = 2
    del fields["temporary"]
    second = browser.get("/api/state", headers=world.user() | {"x-cockpit-since": cursor})
    assert second.headers["x-cockpit-base"] == cursor
    assert second.json()["changes"]["stand_in"] == 2
    assert "temporary" in second.json()["removed"]
    assert "stand_in_for_user" not in second.json()["changes"]


def test_a_state_field_cockpit_already_has_stops_the_request(world: World) -> None:
    factory, _ = stand_in({}, {"version": "mine"})
    with pytest.raises(ValueError, match="version"):
        client(world, [factory]).get("/api/state", headers=world.user())


def test_a_page_without_both_markers_takes_no_tags() -> None:
    with pytest.raises(ValueError, match="part scripts"):
        with_part_tags(f"<head>{PART_STYLES}</head>", [TAGS])


def test_a_part_reads_its_own_setup_table(tmp_path: Path) -> None:
    def read(path: Path, table: Any) -> Any:
        if table is not None and set(table) != {"host"}:
            raise config.SetupError(f"{path}: [stand_in] takes host")
        return table

    path = write_setup(tmp_path, {})
    assert config.read_setup(path, {"stand_in": read}).parts == {"stand_in": None}
    path.write_text(path.read_text() + '\n[stand_in]\nhost = "example.test"\n')
    assert config.read_setup(path, {"stand_in": read}).parts == {"stand_in": {"host": "example.test"}}
    # Without the part that reads it, the table is unknown.
    with pytest.raises(config.SetupError, match="unknown stand_in"):
        config.read_setup(path, {})
    path.write_text(path.read_text() + "port = 22\n")
    with pytest.raises(config.SetupError, match="takes host"):
        config.read_setup(path, {"stand_in": read})


def test_a_part_table_may_not_take_a_setup_key_s_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="email"):
        config.read_setup(write_setup(tmp_path, {}), {"email": lambda path, table: table})


def test_a_copy_without_the_private_package_has_no_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parts, "PACKAGE", "cockpit.no_such_package")
    assert parts.installed() == []
    assert parts.setup_tables() == {}
    conn = sqlite3.connect(":memory:")
    assert parts.hidden_items(conn) == set()
    parts.create_tables(conn)
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
    conn.close()
    assert parts.commands() == []
    assert parts.model_routes() == {}
    assert parts.standing() is None
    assert not parts.is_standing("standing-session")
    assert not parts.is_standing(None)
    assert parts.standing_fields("standing-session", True) == {"standing": False}
    assert parts.page_options() is None
    assert parts.process_labels() == {}


def test_a_part_s_route_with_its_own_key_takes_a_post_from_nobody_signed_in(world: World) -> None:
    def factory(context: Context) -> Part:
        def register(app: FastAPI) -> None:
            context.own_key_posts["/api/stand-in/report"] = "stand-in"

            def report(request: Request) -> dict[str, Any]:
                return {"actor": request.state.actor, "me": request.state.me, "origin": context.origin}

            app.add_api_route("/api/stand-in/report", report, methods=["POST"])
            app.add_api_route("/api/stand-in/other", report, methods=["POST"])

        return Part(register=register, page_tags={}, state=lambda now, is_user: {})

    app = create_app(world.settings, world.deliverer, world.cache, world.spend_index, world.codex_index, [factory])
    nobody = TestClient(app, base_url=ORIGIN, follow_redirects=False)
    assert nobody.post("/api/stand-in/report").json() == {"actor": "stand-in", "me": None, "origin": ORIGIN}
    # The user's cookie counts for nothing there, and the path opens no other route of the part.
    assert client(world, [factory]).post("/api/stand-in/report", headers=world.user()).json()["me"] is None
    assert nobody.post("/api/stand-in/other").status_code in {401, 403}
    assert nobody.post("/api/stand-in/report/").status_code in {401, 403}
    assert nobody.get("/api/stand-in/report").status_code in {401, 403, 405}


def test_a_part_s_command_joins_the_cli(world: World, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def add_parser(commands: Any, agent: argparse.ArgumentParser, help_user: str) -> None:
        parser = commands.add_parser("stand-in", parents=[agent], help=f"{help_user}'s stand-in")
        parser.add_argument("word")

    def run(args: argparse.Namespace, call: Any) -> Any:
        return f"{args.word} from {args.agent}: {len(call('GET', '/api/state', None, None)['items'])} items"

    monkeypatch.setattr(parts, "commands", lambda: [Command(name="stand-in", add_parser=add_parser, run=run)])
    route_cli_to(world, monkeypatch)
    assert cli.main(["stand-in", "hello", "--as", "helper"]) == 0
    assert capsys.readouterr().out == "hello from helper: 0 items\n"
    monkeypatch.setattr(parts, "commands", list)
    with pytest.raises(SystemExit):
        cli.main(["stand-in", "hello", "--as", "helper"])


def test_a_part_adds_tags_to_the_usage_page(world: World) -> None:
    factory, _ = stand_in({"usage.html": TAGS}, {})
    page = client(world, [factory]).get("/usage").text
    assert "/static/stand-in.css?v=" in page
    assert page.index("/static/stand-in.js?v=") < page.index("/static/usage.js?v=")


def test_the_base_app_has_only_its_own_state_fields(world: World) -> None:
    state = client(world, []).get("/api/state").json()
    assert set(state["usage"]) == {"accounts", "error", "codex", "machine"}


def test_base_install_sends_native_model_names() -> None:
    assert config.codex_model("gpt-6.1-sol", "ultrafast") == "gpt-6.1-sol"
    assert config.codex_model("gpt-6-astra", "ultrafast") == "gpt-6-astra"


def test_a_part_hides_items_until_opened_by_id(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    hidden: set[int] = set()
    monkeypatch.setattr(parts, "hidden_items", lambda conn: hidden.copy())
    browser = client(world, [])
    browser.put("/api/projects", json={"name": "Stand-in"}, headers=world.agent("helper"))
    item_ids, step_ids = [], []
    for key in ("note:hidden", "note:visible"):
        response = browser.put("/api/items", json={"key": key, "title": "Work", "project": "Stand-in"},
                               headers=world.agent("helper"))
        assert response.status_code == 200, response.text
        item_ids.append(response.json()["id"])
        step = browser.post("/api/steps", json={"item_key": key, "need": "pick", "ask": "Pick one"},
                            headers=world.agent("helper"))
        assert step.status_code == 200, step.text
        step_ids.append(step.json()["step"]["id"])
    hidden.add(item_ids[0])
    for query, expected_items, expected_steps in (("", item_ids[1:], step_ids[1:]),
                                                  (f"?opened={item_ids[0]}", item_ids, step_ids)):
        state = browser.get("/api/state" + query).json()
        assert {item["id"] for item in state["items"]} == set(expected_items)
        shown_steps = {step["id"] for group in ("queue", "ready_next", "snoozed") for step in state[group]}
        assert shown_steps == set(expected_steps)
        listed = browser.get("/api/listed" + query).json()
        assert set(listed["items"]) == set(expected_items)
        assert {step for group in listed["steps"].values() for step in group} == set(expected_steps)
    [project] = [project for project in browser.get("/api/projects").json() if project["name"] == "Stand-in"]
    assert project["open_items"] == 1


def test_a_part_supplies_its_tables_and_hidden_items(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    def create_tables(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE hidden_items (item_id INTEGER)")
        conn.execute("INSERT INTO hidden_items VALUES (12)")

    def hidden(conn: sqlite3.Connection) -> set[int]:
        return {row[0] for row in conn.execute("SELECT item_id FROM hidden_items")}

    monkeypatch.setattr(parts.importlib.util, "find_spec", lambda package: True)
    package = SimpleNamespace(create_tables=create_tables, HIDDEN_ITEMS=hidden)
    monkeypatch.setattr(parts.importlib, "import_module", lambda name: package)
    conn = sqlite3.connect(":memory:")
    try:
        parts.create_tables(conn)
        assert parts.hidden_items(conn) == {12}
    finally:
        conn.close()


def standing_session() -> parts.Standing:
    return parts.Standing(
        session_id="standing-session", name="Stand-in", logo="/api/stand-in/logo.svg", mark="Stand-in",
        description="Kept running by its part.", running="Running.", stopped="Its supervisor restarts it.",
        keeps="its supervisor manages it", copy_refusal="Write to the standing session instead",
        stop_refusal="Its supervisor stops it", halt_refusal="Its supervisor stops it",
        not_running="{name} is down", unnamed="the stand-in", socket_unreachable="Socket unavailable: {error}",
        written="Written (pid {pid})",
    )


def test_a_part_supplies_a_standing_session_and_page_options(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    from importlib.machinery import ModuleSpec
    from types import ModuleType

    package = ModuleType("cockpit.stand_in")
    package.__spec__ = ModuleSpec(package.__name__, loader=None)
    session = standing_session()
    options = parts.PageOptions(
        marks={"data-stand-in": None}, frame_origins=("https://stand-in.example.test",),
        unrecorded_prefixes=("/api/stand-in/",),
    )
    package.STANDING = lambda: session
    package.PAGE_OPTIONS = lambda: options
    package.PROCESS_LABELS = {r"stand-in server": "Stand-in server"}
    monkeypatch.setitem(sys.modules, package.__name__, package)
    monkeypatch.setattr(parts, "PACKAGE", package.__name__)
    assert parts.standing() is session
    assert parts.is_standing(session.session_id)
    assert not parts.is_standing(None)
    assert not parts.is_standing("another-session")
    assert parts.page_options() is options
    assert parts.process_labels() == {r"stand-in server": "Stand-in server"}
    assert parts.standing_fields(session.session_id, True) == {
        "standing": True, "standing_name": "Stand-in",
        "standing_description": "Kept running by its part. Running.",
    }
    assert parts.standing_fields(session.session_id, False)["standing_description"] == (
        "Kept running by its part. Its supervisor restarts it."
    )
    assert parts.standing_fields("another-session", True) == {"standing": False}


def test_a_parts_page_attributes_and_frame_policy_are_escaped(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    import html
    import json

    monkeypatch.setattr(parts, "page_options", lambda: parts.PageOptions(
        marks={"data-stand-in": None, "data-label": 'A "quoted" name'},
        frame_origins=("https://stand-in.example.test",), unrecorded_prefixes=("/api/stand-in/",),
    ))
    page = client(world, []).get("/", headers=world.user())
    assert 'data-stand-in data-label="A &quot;quoted&quot; name"' in page.text
    prefixes = re.search(r'data-unrecorded-prefixes="([^"]+)"', page.text)
    assert prefixes and json.loads(html.unescape(prefixes[1])) == ["/api/stand-in/"]
    assert "; frame-src 'self' https://stand-in.example.test" in page.headers["content-security-policy"]
