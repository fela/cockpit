"""The preview fixture (serve_preview.py) with a [hub] table in the setup. The hub is a stand-in a check supplies in
hub.json; with none, the hub's key file is a missing one in the scratch folder, so Cockpit runs without the hub and
nothing reads a real hub. hub.json's `sign_in` names the providers the setup gives a sign-in command, Codex and Claude
when it is left out."""
import dataclasses
import functools
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import serve_slow

from cockpit import config, db, hub, hub_failing, parts
from tests.fake_hub import FakeHub

TESTS = Path(__file__).resolve().parents[1]


def parts_fixture(work: Path):
    hub_stand_in = json.loads((work / "hub.json").read_text()) if (work / "hub.json").exists() else None

    def build():
        (work / "hub").mkdir()
        key_file = work / "hub" / "management-key"
        url = "http://127.0.0.1:9"
        signs_in = ("codex", "claude")
        # A stand-in's failing requests start no investigation, which would start a real session.
        hub_failing.investigate = lambda conn, accounts, read_at, spec_repo, checkouts_dir, providers: []
        if hub_stand_in is not None:
            # `kept_limits` is what Cockpit's earlier reads of the hub kept: each account's limits, as hub.limits_now
            # gives them.
            kept = hub_stand_in.pop("kept_limits", None)
            signs_in = tuple(hub_stand_in.pop("sign_in", signs_in))
            if kept is not None:
                with db.connect(config.DB_PATH) as conn:
                    db.kv_set(conn, hub.LIMITS_KEY, kept)
            stand_in = FakeHub("stand-in-management-key")
            for name, value in hub_stand_in.items():
                setattr(stand_in, name, value)
            url = stand_in.url
            key_file.write_text("stand-in-management-key")
        hub_setup = config.HubSetup(
            url=url,
            key_file=key_file,
            sign_in={provider: command for provider, command in {
                "codex": (sys.executable, str(TESTS / "fake_hub_login.py"), str(work / "hub")),
                "claude": (sys.executable, str(TESTS / "fake_hub_claude_login.py"), str(work / "hub")),
            }.items() if provider in signs_in},
            # A restart does nothing and the tunnel reads as up, so a check never touches the machine's own services.
            restarts={"hub": ("true",), "tunnel": ("true",)},
            tunnel_state=("echo", "active"),
        )
        setup = config.setup()
        config.setup = functools.cache(lambda: dataclasses.replace(setup, hub=hub_setup))
        return parts.installed()

    return build


serve_slow.parts_fixture = parts_fixture
serve_slow.SLOW_SECONDS = 0

if __name__ == "__main__":
    serve_slow.main(Path(sys.argv[1]).resolve())
