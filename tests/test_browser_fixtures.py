"""Browser fixture startup and page reads stay within synthetic data and checkout sources."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient

from cockpit import auth, config, emergency, machine, resets, sessions, sidechat
from tests.conftest import COOKIE_KEY, EMAIL


def test_preview_reads_only_temporary_records_and_checkout_sources(tmp_path, monkeypatch, request):
    request.addfinalizer(config.setup.cache_clear)
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "tests" / "browser"))
    spec = importlib.util.spec_from_file_location("browser_fixture", root / "tests/browser/serve_slow.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for target, names in (
        (config, ("SETUP_PATH", "DB_PATH", "CLAUDE_SETTINGS", "CLAUDE_RATE_LIMITS", "ACCOUNTS", "CODEX_HOMES")),
        (sessions, ("PROJECTS_DIR",)), (emergency, ("STATE_PATH", "OUTPUT_DIR")),
        (resets, ("known",)), (sidechat, ("ask_copy",)),
    ):
        for name in names:
            monkeypatch.setattr(target, name, getattr(target, name))
    monkeypatch.setattr(module.spend, "refresh_forever", lambda *args: None)
    monkeypatch.setattr(machine, "read", dict)
    apps = []

    def run(server, sockets):
        apps.append(server.config.app)
        for sock in sockets:
            sock.close()

    monkeypatch.setattr(module.uvicorn.Server, "run", run)
    original_open = Path.open
    reads = []

    def isolated_open(path, mode="r", *args, **kwargs):
        if "r" in mode:
            resolved = path.resolve()
            assert resolved.is_relative_to(tmp_path) or resolved.is_relative_to(root), resolved
            reads.append(resolved)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", isolated_open)
    module.serve(tmp_path)
    assert reads
    assert config.DB_PATH == tmp_path / "cockpit.db"
    assert all(account.folder.is_relative_to(tmp_path) for account in config.ACCOUNTS)
    assert all(home.is_relative_to(tmp_path) for home in config.CODEX_HOMES)
    client = TestClient(apps[0])
    client.cookies.set(auth.SESSION_COOKIE, auth.session_cookie_value(COOKIE_KEY, EMAIL, 4102444800))
    state = client.get("/api/state").json()
    assert len(state["items"]) >= 2 and all(item["title"] for item in state["items"])
    assert len(state["queue"]) == 4
    assert {entry["name"] for entry in state["session_accounts"]} == {"personal", "work"}
