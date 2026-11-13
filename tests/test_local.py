from __future__ import annotations

import dataclasses
import fcntl
import plistlib
import subprocess
import time
import urllib.error
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from cockpit import app, cli, config, local, parts
from cockpit.app import create_app
from tests.test_setup import write_setup

ORIGIN = "http://127.0.0.1:8790"
LOGIN_TOKEN = "l" * 40


def login_code(browser, origin):
    response = browser.post("/auth/local/code", headers={"Authorization": f"Bearer {LOGIN_TOKEN}", "Origin": origin})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()["code"]


def sign_in(browser, origin):
    return browser.post("/auth/local", json={"code": login_code(browser, origin)}, headers={"Origin": origin})


def client(world, peer="127.0.0.1", **changes):
    settings = dataclasses.replace(world.settings, **({"auth_mode": "local", "origin": ORIGIN, "google": None,
                                                      "local_login_token": LOGIN_TOKEN} | changes))
    app = create_app(settings, world.deliverer, world.cache, world.spend_index, world.codex_index, [])
    return TestClient(app, base_url=settings.origin, client=(peer, 15432), follow_redirects=False)


def test_local_mode_has_no_public_pages(tmp_path):
    changes = {"auth_mode": "local", "origin": ORIGIN, "public_origin": "https://pages.example.com"}
    with pytest.raises(config.SetupError, match="public_origin"):
        config.read_setup(write_setup(tmp_path, changes), parts.setup_tables())


def test_local_login_needs_its_own_secret_and_cookie(world):
    browser = client(world)
    assert browser.get("/api/state").status_code == 401
    assert browser.get("/").headers["location"] == "/auth/local"
    assert "cockpit login" in browser.get("/auth/local").text
    for secret in ("wrong", world.settings.token, LOGIN_TOKEN):
        assert browser.post("/auth/local", json={"token": secret}, headers={"Origin": ORIGIN}).status_code == 403
        assert browser.post("/auth/local", json={"code": secret}, headers={"Origin": ORIGIN}).status_code == 403
    response = sign_in(browser, ORIGIN)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Secure" not in cookie
    assert "__Host-" not in cookie
    assert browser.get("/api/state").status_code == 401
    assert browser.get("/api/state", headers={"X-Cockpit-Local": response.json()["page_secret"]}).status_code == 200
    assert browser.post("/auth/sign-out", headers={"Origin": ORIGIN}).status_code == 200
    assert browser.get("/api/state").status_code == 401


def test_local_mode_refuses_to_make_a_page_public(world):
    browser = client(world)
    secret = sign_in(browser, ORIGIN).json()["page_secret"]
    response = browser.post("/api/item-pages/1/public", json={"revision": 1},
                            headers={"Origin": ORIGIN, "X-Cockpit-Local": secret})
    assert response.status_code == 409
    assert "local mode" in response.text


def test_local_mode_rejects_cross_origin_rebinding_and_proxy_requests(world):
    browser = client(world)
    assert browser.post("/auth/local", json={"token": LOGIN_TOKEN}).status_code == 403
    assert browser.post("/auth/local", json={"token": LOGIN_TOKEN},
                        headers={"Origin": "https://evil.test"}).status_code == 403
    assert browser.get("/auth/local", headers={"Host": "evil.test:8790"}).status_code == 403
    assert client(world, peer="10.0.0.2").get("/auth/local", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
    assert browser.get("/auth/google/callback?code=test").status_code == 404


def test_local_token_is_not_a_user_credential_in_google_mode(world):
    browser = TestClient(world.client.app, base_url=world.settings.origin, follow_redirects=False)
    assert browser.post("/auth/local", json={"token": LOGIN_TOKEN}, headers=world.user()).status_code != 200
    assert browser.post("/auth/local/code", headers=world.user() | {"Authorization": f"Bearer {LOGIN_TOKEN}"}).status_code != 200
    assert not any(route.path.startswith("/auth/local") for route in world.client.app.routes)


def test_local_codes_are_private_single_use_and_expire_after_two_minutes(world, monkeypatch):
    browser = client(world)
    clock = [100.0]
    monkeypatch.setattr(app, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time, perf_counter=time.perf_counter))
    for token in ("wrong", world.settings.token):
        assert browser.post("/auth/local/code", headers={"Origin": ORIGIN,
                            "Authorization": f"Bearer {token}", "X-Cockpit-As": "test"}).status_code == 403
    for origin in (None, "https://evil.test"):
        headers = {"Authorization": f"Bearer {LOGIN_TOKEN}"}
        if origin:
            headers["Origin"] = origin
        assert browser.post("/auth/local/code", headers=headers).status_code == 403
    code = login_code(browser, ORIGIN)
    assert len(code) == 43 and code != LOGIN_TOKEN
    headers = {"Origin": ORIGIN}
    assert browser.post("/auth/local", json={"code": "☃"}, headers=headers).status_code == 403
    assert browser.post("/auth/local", json={"code": code + "wrong"}, headers=headers).status_code == 403
    clock[0] += 119
    assert browser.post("/auth/local", json={"code": code}, headers=headers).status_code == 200
    assert browser.post("/auth/local", json={"code": code}, headers=headers).status_code == 403
    expired = login_code(browser, ORIGIN)
    clock[0] += 120
    response = browser.post("/auth/local", json={"code": expired}, headers=headers)
    assert response.status_code == 403 and "run cockpit login again" in response.text
    fresh = login_code(browser, ORIGIN)
    assert client(world).post("/auth/local", json={"code": fresh}, headers=headers).status_code == 403
    assert browser.post("/auth/local", json={"code": fresh}, headers=headers).status_code == 200


@pytest.mark.parametrize("browser_result", [False, True])
def test_login_prints_a_one_time_address_when_no_browser_opens(monkeypatch, capsys, browser_result):
    setup = dataclasses.replace(config.setup(), auth_mode="local", origin=ORIGIN)
    monkeypatch.setattr(config, "setup", lambda: setup)
    monkeypatch.setattr(local.auth, "read_or_create_secret", lambda path: LOGIN_TOKEN)
    monkeypatch.setattr(cli.LOCAL_OPENER, "open", lambda request, timeout: BytesIO(b'{"code": "one-time-test-code"}'))
    monkeypatch.setattr(local.webbrowser, "open", lambda address: browser_result)

    assert cli.main(["login"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert LOGIN_TOKEN not in captured.out
    if browser_result:
        assert captured.out == "Opened Cockpit in your browser.\n"
    else:
        assert captured.out == f"Open this one-time address within two minutes:\n{ORIGIN}/auth/local#one-time-test-code\n"


@pytest.mark.parametrize("origin", ["http://0.0.0.0:8790", "http://127.0.0.1.evil.test:8790", "http://localhost:8790/",
                                    "https://localhost:8790", "http://localhost:8790@evil.test"])
def test_local_setup_allows_only_exact_loopback_origin(tmp_path, origin):
    with pytest.raises(config.SetupError, match="local origin"):
        config.read_setup(write_setup(tmp_path, {"auth_mode": "local", "origin": origin}), parts.setup_tables())


def test_existing_setups_keep_google_and_claude_defaults(tmp_path):
    setup = config.read_setup(write_setup(tmp_path, {}), parts.setup_tables())
    assert (setup.auth_mode, setup.providers, setup.default_provider) == ("google", ("claude",), "claude")
    setup = config.read_setup(write_setup(tmp_path, {"auth_mode": "local", "origin": ORIGIN,
                                                    "providers": ["codex"], "default_provider": "codex"}),
                              parts.setup_tables())
    assert setup.providers == ("codex",)


def test_bootstrap_and_login_work_without_existing_setup(tmp_path, monkeypatch):
    folder = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", folder)
    monkeypatch.setattr(config, "SETUP_PATH", folder / "setup.toml")
    monkeypatch.setattr(config, "LOCAL_LOGIN_PATH", folder / "local-login-token")
    monkeypatch.setattr(local, "codex_command", lambda: "codex")
    config.setup.cache_clear()
    try:
        assert cli.main(["setup", "--local", "--repo", str(Path(__file__).resolve().parents[1])]) == 0
        assert config.SETUP_PATH.stat().st_mode & 0o077 == 0
        assert config.LOCAL_LOGIN_PATH.stat().st_mode & 0o077 == 0
        assert config.setup().providers == ("codex",)
        assert cli.main(["setup", "--local"]) == 1
        opened = []
        monkeypatch.setattr(local.webbrowser, "open", lambda url: opened.append(url) or True)

        def issue(request, timeout):
            assert request.full_url == ORIGIN + "/auth/local/code"
            assert request.method == "POST"
            assert request.get_header("Authorization").startswith("Bearer ")
            assert request.get_header("Origin") == ORIGIN
            return BytesIO(b'{"code": "one-time-test-code"}')

        monkeypatch.setattr(cli.LOCAL_OPENER, "open", issue)
        assert cli.main(["login"]) == 0
        assert opened == [ORIGIN + "/auth/local#one-time-test-code"]
    finally:
        config.setup.cache_clear()


def test_launch_agent_keeps_paths_with_spaces_as_arguments(tmp_path):
    python = tmp_path / "App Support" / "python"
    plist = plistlib.loads(local.launch_agent(python, tmp_path, "/usr/bin:/bin"))
    assert plist["ProgramArguments"] == [str(python), "-m", "cockpit.server"]
    assert plist["KeepAlive"] and plist["RunAtLoad"]


@pytest.mark.parametrize("refusals", [3, 30, 31])
def test_login_retries_connection_refused_for_fifteen_seconds(monkeypatch, capsys, refusals):
    setup = dataclasses.replace(config.setup(), auth_mode="local", origin=ORIGIN)
    monkeypatch.setattr(config, "setup", lambda: setup)
    monkeypatch.setattr(local.auth, "read_or_create_secret", lambda path: LOGIN_TOKEN)
    clock, attempts, opened = [0.0], [], []

    def sleep(seconds):
        assert seconds == 0.5
        clock[0] += seconds

    monkeypatch.setattr(local, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep), raising=False)
    monkeypatch.setattr(local.webbrowser, "open", lambda url: opened.append(url) or True)

    def issue(request, timeout):
        attempts.append(clock[0])
        if len(attempts) <= refusals:
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        return BytesIO(b'{"code": "one-time-test-code"}')

    monkeypatch.setattr(cli.LOCAL_OPENER, "open", issue)
    assert cli.main(["login"]) == (1 if refusals > 30 else 0)
    assert attempts == [number * 0.5 for number in range(min(refusals + 1, 31))]
    assert opened == ([] if refusals > 30 else [ORIGIN + "/auth/local#one-time-test-code"])
    if refusals > 30:
        assert "Cockpit is not running" in capsys.readouterr().err


@pytest.mark.parametrize("error", [urllib.error.HTTPError(ORIGIN, 403, "Forbidden", {}, None),
                                       urllib.error.URLError(TimeoutError("timed out"))])
def test_login_reports_other_failures_without_retrying(monkeypatch, capsys, error):
    setup = dataclasses.replace(config.setup(), auth_mode="local", origin=ORIGIN)
    monkeypatch.setattr(config, "setup", lambda: setup)
    monkeypatch.setattr(local.auth, "read_or_create_secret", lambda path: LOGIN_TOKEN)
    attempts = []

    def issue(request, timeout):
        attempts.append(request.full_url)
        raise error

    monkeypatch.setattr(cli.LOCAL_OPENER, "open", issue)
    assert cli.main(["login"]) == 1
    assert len(attempts) == 1
    assert ("HTTP 403" if isinstance(error, urllib.error.HTTPError) else "Cockpit is not running") in capsys.readouterr().err


@pytest.mark.parametrize("origin", [ORIGIN, "http://localhost:8790"])
def test_login_exchanges_the_token_on_loopback_without_a_proxy(monkeypatch, origin):
    from urllib.response import addinfourl

    setup = dataclasses.replace(config.setup(), auth_mode="local", origin=origin)
    monkeypatch.setattr(config, "setup", lambda: setup)
    monkeypatch.setattr(local.auth, "read_or_create_secret", lambda path: LOGIN_TOKEN)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.test:15432")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    opened, requests = [], []
    monkeypatch.setattr(local.webbrowser, "open", lambda url: opened.append(url) or True)

    def http_open(handler, request):
        requests.append(request.full_url)
        assert request.host == "127.0.0.1:8790"
        assert request.get_header("Authorization") == f"Bearer {LOGIN_TOKEN}"
        assert request.get_header("Origin") == origin
        assert request.get_header("Host") == origin.removeprefix("http://")
        response = addinfourl(BytesIO(b'{"code": "one-time-test-code"}'), {}, request.full_url, 200)
        response.msg = "OK"
        return response

    monkeypatch.setattr(cli.urllib.request.HTTPHandler, "http_open", http_open)
    assert cli.main(["login"]) == 0
    assert requests == [ORIGIN + "/auth/local/code"]
    assert opened == [origin + "/auth/local#one-time-test-code"]
    assert LOGIN_TOKEN not in opened[0]


def test_login_reports_a_stopped_server_without_opening_the_browser(monkeypatch, capsys):
    setup = dataclasses.replace(config.setup(), auth_mode="local", origin=ORIGIN)
    monkeypatch.setattr(config, "setup", lambda: setup)
    monkeypatch.setattr(local.auth, "read_or_create_secret", lambda path: LOGIN_TOKEN)
    opened = []
    monkeypatch.setattr(local.webbrowser, "open", lambda url: opened.append(url))

    def stopped(*args, **kwargs):
        raise local.urllib.error.URLError("connection refused")

    monkeypatch.setattr(cli.LOCAL_OPENER, "open", stopped)
    assert cli.main(["login"]) == 1
    assert "Cockpit is not running" in capsys.readouterr().err
    assert opened == []


def test_local_setup_preserves_unicode_in_toml(tmp_path, monkeypatch):
    repo = tmp_path / "Projects 🚀" / 'Cockpit "desk"'
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "SPEC.md").write_text("spec\n")
    folder = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", folder)
    monkeypatch.setattr(config, "SETUP_PATH", folder / "setup.toml")
    monkeypatch.setattr(local, "codex_command", lambda: "codex")
    monkeypatch.setattr(local.subprocess, "run", lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "main\n"))
    monkeypatch.setattr(local.auth, "read_or_create_secret", lambda path: LOGIN_TOKEN)
    config.setup.cache_clear()
    try:
        local.initialize(repo, "codex", "Sam", "UTC")
        setup = config.setup()
        assert setup.spec_repo == repo and setup.checkouts == repo.parent
        assert setup.name == "Sam"
    finally:
        config.setup.cache_clear()


def deploy_world(tmp_path, monkeypatch, *, imports=True):
    """A Cockpit checkout on `release` with a committed file and an uncommitted edit, a LaunchAgent naming the
    service's Python, and fakes for uv, the scratch import and the restart; returns what they were asked to do."""
    repo = tmp_path / "cockpit"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "SPEC.md").write_text("spec\n")
    (repo / "app.txt").write_text("committed\n")
    for args in (["init", "-q", "-b", "release"], ["add", "."],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "first"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "app.txt").write_text("not committed\n")
    monkeypatch.setattr(config, "SETUP_PATH", write_setup(tmp_path, {"spec_repo": str(repo), "deploy_branch": "release"}))
    config.setup.cache_clear()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(local.sys, "platform", "darwin")
    plist = tmp_path / "agent.plist"
    plist.write_bytes(local.launch_agent(Path("/service/venv/bin/python"), tmp_path, "/usr/bin:/bin"))
    monkeypatch.setattr(local, "_plist", lambda: plist)
    monkeypatch.setattr(local, "_uv", lambda: "uv")
    calls, real_run = [], subprocess.run

    def run(args, **kwargs):
        if args[0] == "git":
            return real_run(args, **kwargs)
        if args[:3] == ["uv", "pip", "install"]:
            calls.append(("install", args[args.index("--python") + 1], (Path(args[-1]) / "app.txt").read_text()))
        elif args[0] == "uv":
            calls.append(("venv",))
        else:
            calls.append(("import",))
            return subprocess.CompletedProcess(args, 0 if imports else 1, b"", b"" if imports else b"SyntaxError: bad")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(local.subprocess, "run", run)
    monkeypatch.setattr(local, "_detach", lambda args, lock: calls.append(("restart", args)))
    return calls


def test_deploy_installs_the_committed_branch_checked_first_then_restarts_apart(tmp_path, monkeypatch):
    calls = deploy_world(tmp_path, monkeypatch)
    try:
        said = local.deploy()
    finally:
        config.setup.cache_clear()
    assert said.startswith("Deployed release at ") and "Reload" in said
    assert [call[0] for call in calls] == ["venv", "install", "import", "install", "restart"]
    scratch, live, restart = calls[1], calls[3], calls[4]
    assert scratch[1].endswith("/venv/bin/python") and scratch[2] == "committed\n"
    assert live[1] == "/service/venv/bin/python" and live[2] == "committed\n"
    assert restart[1][2] == local.RESTART_SCRIPT and restart[1][-1] == str(tmp_path / "agent.plist")


def test_deploy_on_linux_restarts_the_systemd_service_apart(tmp_path, monkeypatch):
    """on Linux deploy installed the commit and left the old code running, while its help and the sweep
    skill promised the restart."""
    calls = deploy_world(tmp_path, monkeypatch)
    monkeypatch.setattr(local.sys, "platform", "linux")
    unit = tmp_path / "cockpit.service"
    monkeypatch.setattr(local, "_systemd_unit", lambda: unit)
    try:
        assert "Restart cockpit-server" in local.deploy()
        unit.write_text("[Service]\n")
        said = local.deploy()
    finally:
        config.setup.cache_clear()
    assert "restarts in" in said and "Reload" in said
    assert calls[-1] == ("restart", ["/bin/sh", "-c", local.SYSTEMD_RESTART_SCRIPT, "sh", str(local.RESTART_DELAY_SECONDS)])
    assert [call[0] for call in calls].count("restart") == 1


def test_deploy_leaves_the_service_alone_when_the_commit_does_not_import(tmp_path, monkeypatch):
    calls = deploy_world(tmp_path, monkeypatch, imports=False)
    try:
        with pytest.raises(ValueError, match="nothing was deployed.*SyntaxError"):
            local.deploy()
    finally:
        config.setup.cache_clear()
    assert [call[0] for call in calls] == ["venv", "install", "import"]


COMMIT = ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m"]


def git_in(folder, *args):
    return subprocess.run(["git", "-C", str(folder), *args], check=True, capture_output=True, text=True).stdout.strip()


def with_origin(tmp_path, *, merged=True, own=False, pushed=True):
    """Give deploy_world's checkout, made clean, an origin. `merged`: origin has a commit from another worktree that
    changes app.txt. `own`: the checkout has a commit of its own. `pushed`: origin has the branch at all.
    Returns origin's folder."""
    repo, origin, other = tmp_path / "cockpit", tmp_path / "origin.git", tmp_path / "other"
    git_in(repo, "checkout", "--", "app.txt")
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True)
    git_in(repo, "remote", "add", "origin", str(origin))
    if pushed:
        git_in(repo, "push", "-q", "origin", "release")
    if merged:
        subprocess.run(["git", "clone", "-q", "-b", "release", str(origin), str(other)], check=True, capture_output=True)
        (other / "app.txt").write_text("merged from a worktree\n")
        git_in(other, *COMMIT, "merged", "-a")
        git_in(other, "push", "-q", "origin", "release")
    if own:
        (repo / "own.txt").write_text("only here\n")
        git_in(repo, "add", "own.txt")
        git_in(repo, *COMMIT, "own")
    (repo / "tmp").mkdir()
    (repo / "tmp" / "old-worktree.txt").write_text("untracked\n")  # untracked files block nothing (CODE-8)
    return origin


def deployed(calls):
    """The app.txt installed into the scratch venv and into the service's venv."""
    return [call[2] for call in calls if call[0] == "install"]


def test_deploy_first_brings_the_branch_up_to_origin(tmp_path, monkeypatch):
    """A session in a worktree of its own may run no git in the deploy checkout, so deploy pulls itself."""
    calls = deploy_world(tmp_path, monkeypatch)
    origin = with_origin(tmp_path)
    try:
        said = local.deploy()
    finally:
        config.setup.cache_clear()
    merged = git_in(origin, "rev-parse", "release")
    assert said.startswith(f"Deployed release at {merged[:12]}.")
    assert deployed(calls) == ["merged from a worktree\n"] * 2
    assert git_in(tmp_path / "cockpit", "rev-parse", "HEAD") == merged
    assert [call[0] for call in calls] == ["venv", "install", "import", "install", "restart"]


def test_deploy_refuses_a_branch_that_split_from_origin(tmp_path, monkeypatch):
    calls = deploy_world(tmp_path, monkeypatch)
    with_origin(tmp_path, own=True)
    try:
        with pytest.raises(ValueError, match=r"bringing release up to origin/release \(nothing was deployed\) failed"):
            local.deploy()
    finally:
        config.setup.cache_clear()
    assert calls == []


def test_deploy_refuses_uncommitted_changes_in_the_deploy_checkout(tmp_path, monkeypatch):
    """CODE-8: the deploy checkout holds only its branch; a pull there would carry half-done work along."""
    calls = deploy_world(tmp_path, monkeypatch)
    with_origin(tmp_path)
    (tmp_path / "cockpit" / "docs" / "SPEC.md").write_text("half done\n")
    try:
        with pytest.raises(ValueError, match=r"uncommitted changes \(docs/SPEC.md\)"):
            local.deploy()
    finally:
        config.setup.cache_clear()
    assert calls == []


def test_deploy_refuses_uncommitted_changes_when_origin_lacks_the_branch(tmp_path, monkeypatch):
    calls = deploy_world(tmp_path, monkeypatch)
    with_origin(tmp_path, merged=False, pushed=False)
    (tmp_path / "cockpit" / "docs" / "SPEC.md").write_text("half done\n")
    try:
        with pytest.raises(ValueError, match=r"uncommitted changes \(docs/SPEC.md\)"):
            local.deploy()
    finally:
        config.setup.cache_clear()
    assert calls == []


def test_deploy_refuses_an_off_branch_checkout_behind_origin(tmp_path, monkeypatch):
    """Off its branch the checkout is not caught up, so a branch behind origin's would install old code."""
    calls = deploy_world(tmp_path, monkeypatch)
    with_origin(tmp_path)
    git_in(tmp_path / "cockpit", "checkout", "-q", "-b", "elsewhere")
    try:
        with pytest.raises(ValueError, match=r"pushing release at \w+ to origin \(nothing was deployed\) failed"):
            local.deploy()
    finally:
        config.setup.cache_clear()
    assert [call[0] for call in calls] == ["venv", "install", "import"]


def test_the_restart_holds_the_deploy_lock_until_it_ends(tmp_path, monkeypatch):
    """A second deploy waits for the first one's restart, which runs after the first deploy returns."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    tmp_path.joinpath("deploy.lock").touch()
    with open(tmp_path / "deploy.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        local._detach(["/bin/sh", "-c", "sleep 1"], lock)
    with open(tmp_path / "deploy.lock", "a") as other:
        with pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(other, fcntl.LOCK_EX)


def test_deploy_pushes_local_commits_origin_lacks(tmp_path, monkeypatch):
    """A commit only in the checkout (a spec edit whose push failed) still deploys, and reaches origin first."""
    calls = deploy_world(tmp_path, monkeypatch)
    origin = with_origin(tmp_path, merged=False, own=True)
    try:
        local.deploy()
    finally:
        config.setup.cache_clear()
    assert git_in(origin, "rev-parse", "release") == git_in(tmp_path / "cockpit", "rev-parse", "HEAD")
    assert [call[0] for call in calls] == ["venv", "install", "import", "install", "restart"]


def test_deploy_installs_the_commit_when_origin_moves_on_during_the_install(tmp_path, monkeypatch):
    """Other sessions' spec edits reach origin while a slow install runs; the push then fails, yet origin holds the
    commit, so it deploys."""
    calls = deploy_world(tmp_path, monkeypatch)
    origin = with_origin(tmp_path, merged=False)
    started = git_in(origin, "rev-parse", "release")
    other, fake = tmp_path / "other", local.subprocess.run

    def run(args, **kwargs):
        if args[:3] == ["uv", "pip", "install"] and not other.exists():
            subprocess.run(["git", "clone", "-q", "-b", "release", str(origin), str(other)], check=True, capture_output=True)
            (other / "docs" / "SPEC.md").write_text("edited meanwhile\n")
            git_in(other, *COMMIT, "spec edit", "-a")
            git_in(other, "push", "-q", "origin", "release")
        return fake(args, **kwargs)

    monkeypatch.setattr(local.subprocess, "run", run)
    try:
        said = local.deploy()
    finally:
        config.setup.cache_clear()
    assert said.startswith(f"Deployed release at {started[:12]}.") and "moved past" in said
    assert [call[0] for call in calls] == ["venv", "install", "import", "install", "restart"]


def test_deploy_publishes_a_branch_origin_does_not_have(tmp_path, monkeypatch):
    calls = deploy_world(tmp_path, monkeypatch)
    origin = with_origin(tmp_path, merged=False, pushed=False)
    try:
        local.deploy()
    finally:
        config.setup.cache_clear()
    assert git_in(origin, "rev-parse", "release") == git_in(tmp_path / "cockpit", "rev-parse", "HEAD")
    assert deployed(calls) == ["committed\n"] * 2


def test_deploy_needs_a_deploy_branch(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETUP_PATH", write_setup(tmp_path, {}))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    config.setup.cache_clear()
    try:
        assert config.setup().deploy_branch == ""
        assert cli.main(["deploy"]) == 1
    finally:
        config.setup.cache_clear()


@pytest.mark.parametrize("origin", [ORIGIN, "http://localhost:8790"])
def test_local_api_requires_the_origin_secret_for_reads_and_writes(world, origin):
    browser = client(world, origin=origin)
    response = sign_in(browser, origin)
    secret = response.json()["page_secret"]
    write = {"text": "A private task", "part": None, "pinned": False}
    assert browser.get("/api/state").status_code == 401
    assert browser.post("/api/items/add", json=write, headers={"Origin": origin}).status_code == 401
    headers = {"Origin": origin, "X-Cockpit-Local": secret}
    assert browser.get("/api/state", headers=headers).status_code == 200
    assert browser.post("/api/items/add", json=write, headers=headers).status_code == 200
    # Another sign-in creates a separate session even when both sign in within the same second.
    other = client(world, origin=origin)
    other_secret = sign_in(other, origin).json()["page_secret"]
    assert other_secret != secret
    for path, method in (("/api/state", "GET"), ("/api/items/add", "POST")):
        assert browser.request(method, path, json=write if method == "POST" else None,
                               headers={"Origin": origin, "X-Cockpit-Local": other_secret}).status_code == 401
    # An agent uses its bearer credential, with no cookie or page secret.
    agent = client(world, origin=origin)
    bearer = {"Authorization": f"Bearer {world.settings.token}", "X-Cockpit-As": "test"}
    assert agent.get("/api/state", headers=bearer).status_code == 200
    assert agent.put("/api/items", json={"key": "note:agent", "title": "Agent task"}, headers=bearer).status_code == 200
    assert browser.get("/").status_code == 200
    assert browser.get("/auth/local").status_code == 200


def test_local_subresources_work_with_only_a_cookie_and_api_aliases_require_secret(world):
    browser = client(world)
    response = sign_in(browser, ORIGIN)
    headers = {"Origin": ORIGIN, "X-Cockpit-Local": response.json()["page_secret"]}
    item = browser.post("/api/items/add", json={"text": "An image", "part": None, "pinned": False}, headers=headers).json()
    image = browser.post(f"/api/items/{item['id']}/images", content=b"GIF89a" + b"\x00" * 8,
                         headers=headers | {"Content-Type": "image/gif"}).json()
    assert browser.get(f"/item-images/{image['id']}").status_code == 200
    assert browser.get(f"/api/item-images/{image['id']}").status_code == 401
    page = browser.post(f"/api/items/{item['id']}/pages", json={"title": "Report", "html": "<h1>Private report</h1>"},
                        headers=headers).json()
    assert browser.get(f"/pages/{page['id']}").status_code == 200
    assert browser.get(f"/pages/{page['id']}/figure").status_code == 200
    for path in ("/", "/classic", "/spec", "/skills", "/writing", "/usage", "/server", "/briefing", "/pages", "/emergency"):
        response = browser.get(path)
        assert response.status_code == 200
        assert "local-auth.js" in response.text.split("<script", 1)[1].split("</script>", 1)[0]


@pytest.mark.parametrize("origin", [ORIGIN, "http://localhost:8790"])
def test_cli_uses_the_configured_local_host(world, monkeypatch, origin):
    from io import BytesIO

    browser = client(world, origin=origin)
    setup = dataclasses.replace(config.setup(), auth_mode="local", origin=origin)
    monkeypatch.setattr(config, "setup", lambda: setup)
    monkeypatch.setattr(cli, "read_or_create_secret", lambda path: world.settings.token)

    def urlopen(request, timeout):
        assert request.full_url.startswith(origin + "/")
        response = browser.request(request.method, request.full_url, headers=dict(request.header_items()))
        assert response.status_code == 200
        result = BytesIO(response.content)
        result.headers = response.headers
        return result

    monkeypatch.setattr(cli.LOCAL_OPENER, "open", urlopen)
    assert "items" in cli.call("GET", "/api/state", None, None)


def test_server_start_tightens_only_its_private_data_directories(tmp_path, monkeypatch):
    from cockpit import server

    data = tmp_path / "shared" / "data"
    backups = data / "backups"
    backups.mkdir(parents=True)
    for folder in (data.parent, data, backups):
        folder.chmod(0o755)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "BACKUP_DIR", backups)

    def stop_before_secrets(path):
        assert data.stat().st_mode & 0o777 == 0o700
        assert backups.stat().st_mode & 0o777 == 0o700
        assert data.parent.stat().st_mode & 0o777 == 0o755
        raise RuntimeError("stopped before reading sign-in files")

    monkeypatch.setattr(server.auth, "read_or_create_secret", stop_before_secrets)
    with pytest.raises(RuntimeError, match="stopped before"):
        server.main()
    backups.rmdir()
    data.rmdir()
    config.private_data_dirs()
    assert data.stat().st_mode & 0o777 == backups.stat().st_mode & 0o777 == 0o700


def test_google_mode_keeps_its_browser_requests_unchanged(world):
    assert world.client.get("/static/local-auth.js").text == ""
    assert world.client.get("/api/state").status_code == 200
