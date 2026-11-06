from pathlib import Path

import pytest

from cockpit import store


@pytest.mark.parametrize("probe", ["is_dir", "exists"])
def test_checkouts_skips_a_child_whose_probe_fails(tmp_path, monkeypatch, caplog, probe):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    for name in ("z-repo", "a-repo"):
        (tmp_path / name / ".git").mkdir(parents=True)
    (tmp_path / "ordinary").mkdir()
    original = getattr(Path, probe)

    def inspect(path):
        if path == (blocked if probe == "is_dir" else blocked / ".git"):
            raise PermissionError("unreadable child")
        return original(path)

    monkeypatch.setattr(Path, probe, inspect)
    assert store.checkouts(tmp_path) == [str(tmp_path), str(tmp_path / "a-repo"), str(tmp_path / "z-repo")]
    assert str(blocked) in caplog.text and "unreadable child" in caplog.text
