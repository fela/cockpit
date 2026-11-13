"""The build version the page compares to offer Reload (SPEC UI-13)."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from cockpit import app
from tests.conftest import World


def test_the_version_changes_with_any_file_of_the_package_but_not_with_bytecode(tmp_path: Path) -> None:
    package = tmp_path / "cockpit"
    shutil.copytree(app.PACKAGE_DIR, package, ignore=shutil.ignore_patterns("__pycache__"))
    before = app.build_version(package)
    assert re.fullmatch(r"[0-9a-f]{12}", before)
    assert app.build_version(package) == before

    script = package / "static" / "app.js"
    script.write_bytes(script.read_bytes() + b"\n// changed\n")
    after_static = app.build_version(package)
    assert after_static != before

    (package / "store.py").write_text((package / "store.py").read_text() + "\n")
    after_python = app.build_version(package)
    assert after_python not in (before, after_static)

    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "store.cpython-312.pyc").write_bytes(b"\x00" * 16)
    (package / "static" / "__pycache__").mkdir()
    (package / "static" / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    assert app.build_version(package) == after_python

    (package / "static" / "app.js").rename(package / "static" / "main.js")  # a rename counts too
    assert app.build_version(package) != after_python


def test_the_state_carries_the_version_computed_once_at_start_up(world: World, monkeypatch) -> None:
    version = world.client.get("/api/state").json()["version"]
    assert re.fullmatch(r"[0-9a-f]{12}", version)
    monkeypatch.setattr(app, "build_version", lambda package_dir: pytest.fail("the version is computed again"))
    assert world.client.get("/api/state").json()["version"] == version
