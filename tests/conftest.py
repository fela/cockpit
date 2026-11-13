from __future__ import annotations

import contextlib
import dataclasses
import functools
import inspect
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio.from_thread
import pytest
from fastapi.testclient import TestClient

from cockpit import accounts, auth, codex, config, db, delivery, github, parts, permissions, sessions, spend
from cockpit.app import Settings, create_app
from cockpit.sessions import Listing, Session

# The tests' setup file (SPEC SCOPE-5) uses EMAIL for the account that signs in.
config.SETUP_PATH = Path(__file__).parent / "setup.toml"
ORIGIN = "https://cockpit.example.test"
PUBLIC_ORIGIN = "https://pages.example.test"
EMAIL = "sam@example.com"
FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"
TOKEN = "t" * 40
COOKIE_KEY = b"k" * 32
SESSION_A = "11111111-1111-4111-8111-111111111111"
SESSION_B = "22222222-2222-4222-8222-222222222222"
CLAUDE_SETTINGS_TEXT = """{
  "model": "opus",
  "permissions": {
    "defaultMode": "auto",
    "allow": ["Bash(gh pr view *)"],
    "ask": ["Bash(git push *)"]
  },
  "autoMode": {
    "allow": ["$defaults", "Reading files anywhere under the home directory is fine."]
  }
}
"""
SPEC_TEXT = r"""# Spec

Intro paragraph.

## Requirements

| ID | Requirement | Origin | Strength | Status | Evidence | Last change | Comments |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ONE-1 | The tool does one thing. | User | Should | Live | 2026-01-04 09:00:00 "one thing" | 2026-01-04 created (Claude) |  |
| ONE-2 | A pipe \| inside a cell. | User | Could | Not done | "x" | 2026-01-04 created (Claude) |  |

## Notes

- First note.
- Second note.
"""


def make_session(
    session_id: str, pid: int | None, state: str = "blocked", cwd: str = "/", account: str = "personal"
) -> Session:
    return Session(
        id=session_id[:8],
        session_id=session_id,
        name=f"session {session_id[:4]}",
        state=state,
        pid=pid,
        status="idle" if pid else "",
        tempo="",
        job_state="",
        in_flight=[],
        cwd=cwd,
        detail="doing things",
        needs="pick A or B" if state == "blocked" else "",
        intent="",
        updated_at="2026-01-04T09:00:00Z",
        links=[],
        effort="",
        version="2.1.301" if pid else "",
        account=account,
    )


class FakeCache:
    def __init__(self, sessions: list[Session]) -> None:
        self.sessions = sessions
        self.warnings: list[str] = []
        self.account_problems: list[accounts.Problem] = []

    def get(self) -> list[Session]:
        return self.sessions

    def snapshot(self, wait: bool) -> Listing:
        return Listing(self.sessions, self.warnings, self.account_problems, frozenset())

    def invalidate(self) -> None:
        """Nothing to drop: tests set `sessions` directly."""


@dataclass
class FakeRunner:
    output: str = "backgrounded · abcd1234\n"
    returncode: int = 0
    calls: list[tuple[list[str], Path]] = field(default_factory=list)
    # The account each call ran on, by name, in the order of `calls`.
    accounts: list[str] = field(default_factory=list)

    def __call__(self, account: config.Account, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, cwd))
        self.accounts.append(account.name)
        return subprocess.CompletedProcess(args, self.returncode, stdout=self.output, stderr="")


@dataclass
class World:
    settings: Settings
    client: TestClient
    db_path: Path
    cache: FakeCache
    runner: FakeRunner
    deliverer: delivery.Deliverer
    checkout: Path
    exchange_result: dict[str, Any]
    spec_repo: Path
    claude_settings: Path
    claude_rate_limits: Path
    claude_state: Path
    spend_index: spend.SpendIndex
    codex_index: codex.CodexIndex

    def user(self) -> dict[str, str]:
        return {"origin": ORIGIN}

    def agent(self, name: str = "sweep") -> dict[str, str]:
        return {"authorization": f"Bearer {TOKEN}", "x-cockpit-as": name}


@pytest.fixture(autouse=True)
def no_stop_grace(monkeypatch) -> None:
    """The fake `claude` has no daemon to settle after a stop; the test that checks the wait sets it."""
    monkeypatch.setattr(delivery, "STOP_GRACE_SECONDS", 0)


@pytest.fixture(autouse=True)
def no_refusal_hold(monkeypatch) -> None:
    """A refusal shows at once, so a test sees its card right after reporting it; the tests of the hold set it."""
    monkeypatch.setattr(permissions, "HOLD_SECONDS", 0)
    monkeypatch.setattr(permissions, "HOLD_MAX_SECONDS", 0)


@pytest.fixture(autouse=True)
def claude_accounts(monkeypatch, tmp_path: Path) -> tuple[config.Account, config.Account]:
    """The Claude accounts are folders of the test's own, never this machine's: personal (the default one) and work,
    whose folder does not exist until a test makes it."""
    folder = tmp_path / "accounts"
    personal = config.Account(name="personal", letter="P", folder=folder / "claude", state=folder / "claude.json", default=True)
    work = config.Account(
        name="work", letter="W", folder=folder / "claude-work", state=folder / "claude-work" / ".claude.json", default=False
    )
    monkeypatch.setattr(config, "ACCOUNTS", (personal, work))
    return personal, work


@pytest.fixture(autouse=True)
def transcripts(monkeypatch, tmp_path: Path) -> Path:
    """Session transcripts come from a folder of the test's own, never from this machine's sessions."""
    folder = tmp_path / "projects"
    monkeypatch.setattr(sessions, "PROJECTS_DIR", folder)
    monkeypatch.setattr(sessions, "_usages", {})
    return folder


@pytest.fixture(autouse=True)
def merged_pulls(monkeypatch) -> set[str]:
    """Whether a PR merged comes from this set, never from GitHub: a test adds the refs (`owner/repo#n`) it merged."""
    merged: set[str] = set()
    monkeypatch.setattr(github, "pull_merged", lambda ref: ref in merged)
    return merged


@pytest.fixture(autouse=True)
def own_claude_settings(monkeypatch, tmp_path: Path) -> None:
    """Provider detection reads the test's own settings."""
    monkeypatch.setattr(config, "CLAUDE_SETTINGS", tmp_path / "claude" / "settings.json")


@pytest.fixture(autouse=True)
def base_install(monkeypatch):
    """Tests run on the base install; the tests of an install's own part supply that part themselves."""
    monkeypatch.setattr(parts, "PACKAGE", "cockpit.no_such_package")
    setup_reader = config.setup
    setup_reader.cache_clear()
    yield
    setup_reader.cache_clear()


@pytest.fixture
def app_parts():
    """The base app, with installation features supplied by their own tests."""
    return []


@pytest.fixture(autouse=True)
def database(monkeypatch, tmp_path: Path) -> None:
    """Code that opens Cockpit's database by its configured path, such as `cockpit attach`, opens the test's own."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "configured.db")


@pytest.fixture(autouse=True)
def unsynced_databases(monkeypatch) -> None:
    """Test databases skip syncing to disk on each commit. A test's database is thrown away with its folder, so a
    crash losing the last writes costs nothing."""
    connect = db.connect

    def unsynced(path: Path) -> sqlite3.Connection:
        conn = connect(path)
        conn.execute("PRAGMA synchronous = OFF")
        return conn

    monkeypatch.setattr(db, "connect", unsynced)


@pytest.fixture(scope="session")
def migrated_db(tmp_path_factory) -> Path:
    """A database with every migration applied, made once per test run; each `world` starts from a copy of it."""
    path = tmp_path_factory.mktemp("template") / "cockpit.db"
    conn = db.connect(path)
    conn.execute("PRAGMA synchronous = OFF")
    db.migrate(conn)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return path


# `git bisect run` and git hooks export GIT_DIR (and hooks GIT_INDEX_FILE), which outrank `git -C`: under them a
# test's git command acts on the outer checkout instead of the test's repo.
GIT_REDIRECTS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")


@pytest.fixture(autouse=True)
def own_git_repos(monkeypatch) -> None:
    """Git commands in a test act on the test's own repos, whatever GIT_REDIRECTS the test run inherited."""
    for name in GIT_REDIRECTS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def one_event_loop(monkeypatch) -> Generator[None]:
    """A TestClient used outside a `with` block starts a thread and an event loop for every request. Each test's
    requests share one loop instead, started on the test's first request and stopped when the test ends; a client
    given its own `portal` keeps it."""
    loops = contextlib.ExitStack()
    portal: list[anyio.from_thread.BlockingPortal] = []

    @contextlib.contextmanager
    def shared_portal(client: TestClient) -> Generator[anyio.from_thread.BlockingPortal]:
        if client.portal is not None:
            yield client.portal
            return
        if not portal:
            portal.append(loops.enter_context(anyio.from_thread.start_blocking_portal(**client.async_backend)))
        yield portal[0]

    monkeypatch.setattr(TestClient, "_portal_factory", shared_portal)
    with loops:
        yield


@pytest.fixture(scope="session")
def spec_template(tmp_path_factory) -> Path:
    """The spec's checkout on the deploy branch and the bare remote that spec edits push to (SPEC CODE-7), made once
    per test run; each `world` copies both, so a test pays for no git commands."""
    folder = tmp_path_factory.mktemp("spec-template")
    make_spec_template(folder)
    return folder


def make_spec_template(folder: Path) -> None:
    """Builds the spec template in `folder`. It runs before any test's own_git_repos, so its git commands get an
    environment without GIT_REDIRECTS themselves."""
    env = {name: value for name, value in os.environ.items() if name not in GIT_REDIRECTS}
    spec_repo = folder / "spec-repo"
    (spec_repo / "docs").mkdir(parents=True)
    (spec_repo / "docs" / "SPEC.md").write_text(SPEC_TEXT)
    spec_remote = folder / "spec-remote.git"
    # An empty --template leaves out git's sample hooks, which every test would otherwise copy and delete.
    subprocess.run(["git", "init", "-q", "--template=", "--bare", "-b", "main", str(spec_remote)], check=True,
                   capture_output=True, env=env)
    for command in (
        ["init", "-q", "--template=", "-b", "main"],
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.test"],
        ["add", "docs/SPEC.md"],
        ["commit", "-q", "-m", "spec"],
        ["remote", "add", "origin", str(spec_remote)],
        ["push", "-q", "-u", "origin", "main"],
    ):
        subprocess.run(["git", "-C", str(spec_repo), *command], check=True, capture_output=True, env=env)


@pytest.fixture(scope="session", autouse=True)
def shared_app_building() -> Generator[None]:
    """Each test builds its own app from the same code, and most of that work comes out the same every time, so it
    is done once per test run and reused:

    - FastAPI builds a pydantic validator for every parameter and response of every route. Validators are kept by
      their full description. Models made on the fly (a route's combined body) are rebuilt every time, because
      each app has its own class under the same name.
    - FastAPI evaluates every route's parameter annotations, which `from __future__ import annotations` leaves as
      text; the same text in the same module always means the same type. A function's annotations are kept by its
      code and module.
    - FastAPI's parameter fields are kept by their full description, as validators are.
    - FastAPI works out each route's tree of dependencies and parameters. The tree depends on the endpoint's code,
      defaults and route dependencies, so a copy of it serves the same route in the next app, with that app's own
      endpoint and route dependencies put in. A tree that reaches a function made inside create_app below the route
      dependencies, or a field that is not kept, is built fresh every time.
    - Whether an endpoint is a generator, its return type and its path pattern come from its code and path alone.
    - The version hashes every file of the package; the package does not change during a run."""
    from fastapi import params
    from fastapi import routing as fastapi_routing
    from fastapi._compat import v2
    from fastapi.datastructures import DefaultPlaceholder
    from fastapi.dependencies import utils as fastapi_utils
    from pydantic.fields import FieldInfo
    from pydantic_core import PydanticUndefined as Undefined

    from cockpit import app

    build_adapter = v2.TypeAdapter
    adapters: dict[str, Any] = {}

    def shared_adapter(annotation: Any, config: Any = None) -> Any:
        key = f"{annotation!r} {config!r}"
        if "pydantic.main." in key or " at 0x" in key:
            return build_adapter(annotation, config=config)
        if key not in adapters:
            adapters[key] = build_adapter(annotation, config=config)
        return adapters[key]

    evaluate = fastapi_utils.get_typed_annotation
    annotations: dict[tuple[str, int], Any] = {}

    def shared_annotation(annotation: Any, globalns: dict[str, Any]) -> Any:
        if not isinstance(annotation, str):
            return evaluate(annotation, globalns)
        key = (annotation, id(globalns))
        if key not in annotations:
            annotations[key] = evaluate(annotation, globalns)
        return annotations[key]

    read_signature = fastapi_utils._get_signature
    function_annotations: dict[tuple[Any, int], dict[str, Any]] = {}

    def shared_signature(call: Any) -> inspect.Signature:
        if not inspect.isfunction(call) or hasattr(call, "__wrapped__"):
            return read_signature(call)
        key = (call.__code__, id(call.__globals__))
        if key not in function_annotations:
            try:
                function_annotations[key] = inspect.get_annotations(call, eval_str=True)
            except NameError:
                return read_signature(call)
        typed = function_annotations[key]
        signature = inspect.signature(call)
        return signature.replace(
            parameters=[
                parameter.replace(annotation=typed.get(parameter.name, parameter.empty))
                for parameter in signature.parameters.values()
            ],
            return_annotation=typed.get("return", signature.empty),
        )

    build_field = fastapi_routing.create_model_field
    fields: dict[str, Any] = {}

    def plain(value: Any) -> Any:
        # FastAPI marks unset options with DefaultPlaceholder objects, whose repr is their address.
        return ("DefaultPlaceholder", value.value) if isinstance(value, DefaultPlaceholder) else value

    def describe(info: Any) -> str:
        slots = [slot for cls in type(info).__mro__ for slot in getattr(cls, "__slots__", ())]
        attributes = {name: plain(value) for name, value in getattr(info, "__dict__", {}).items()}
        return repr((type(info), [(slot, plain(getattr(info, slot, None))) for slot in slots], attributes))

    def shared_field(name: str, type_: Any, default: Any = Undefined, field_info: Any = None, alias: Any = None,
                     mode: str = "validation") -> Any:
        key = f"{name} {mode} {alias} {type_!r} {default!r} {describe(field_info)}"
        if "pydantic.main." in key or " at 0x" in key:
            return build_field(name, type_, default, field_info, alias, mode)
        if key not in fields:
            fields[key] = build_field(name, type_, default, field_info, alias, mode)
        return fields[key]

    build_dependant = fastapi_routing._build_dependant_with_parameterless_dependencies
    dependants: dict[tuple[Any, ...], Any] = {}
    unshared: set[tuple[Any, ...]] = set()

    def made_once(call: Any) -> bool:
        # A function defined at module level, or a class: the same object in every app.
        return not inspect.isfunction(call) or "<locals>" not in call.__qualname__

    def call_key(call: Any) -> Any:
        return (call.__code__, id(call.__globals__)) if inspect.isfunction(call) else id(call)

    def value_key(value: Any) -> Any:
        if isinstance(value, params.Depends):
            return (type(value), call_key(value.dependency), value.use_cache, value.scope, getattr(value, "scopes", None))
        return describe(value) if isinstance(value, FieldInfo) else repr(plain(value))

    def nodes(dependant: Any) -> Iterator[Any]:
        yield dependant
        for child in dependant.dependencies:
            yield from nodes(child)

    def copy(dependant: Any, call: Any) -> Any:
        return dataclasses.replace(
            dependant, call=call, dependencies=[copy(child, child.call) for child in dependant.dependencies],
            path_params=list(dependant.path_params), query_params=list(dependant.query_params),
            header_params=list(dependant.header_params), cookie_params=list(dependant.cookie_params),
            body_params=list(dependant.body_params),
        )

    def shared_dependant(*, path: str, call: Any, dependencies: Any) -> Any:
        if not inspect.isfunction(call) or hasattr(call, "__wrapped__"):
            return build_dependant(path=path, call=call, dependencies=dependencies)
        defaults = [*(call.__defaults__ or ()), *sorted((call.__kwdefaults__ or {}).items())]
        key = (path, call_key(call), tuple(value_key(each) for each in defaults),
               tuple(value_key(each) for each in dependencies))
        # A described value showing its address differs in every app.
        addressed = any(isinstance(part, str) and " at 0x" in part for part in (*key[2], *key[3]))
        if addressed or key in unshared:
            return build_dependant(path=path, call=call, dependencies=dependencies)
        if key not in dependants:
            dependant, body_params, embed = build_dependant(path=path, call=call, dependencies=dependencies)
            fixed = {id(dependant), *(id(child) for child in dependant.dependencies[:len(dependencies)])}
            shared_fields = {id(each) for each in fields.values()}
            own_fields = [each for node in nodes(dependant) for each in (
                *node.path_params, *node.query_params, *node.header_params, *node.cookie_params, *node.body_params)]
            if all(id(node) in fixed or made_once(node.call) for node in nodes(dependant)) and all(
                    id(each) in shared_fields for each in own_fields):
                dependants[key] = dependant
            else:
                unshared.add(key)
            return dependant, body_params, embed
        dependant = copy(dependants[key], call)
        for child, depends in zip(dependant.dependencies, dependencies, strict=False):
            child.call = depends.dependency
        body_params = fastapi_routing._get_flat_body_params(dependant)
        return dependant, body_params, fastapi_routing._should_embed_body_fields(body_params)

    def plain_function(call: Any) -> bool:
        return inspect.isfunction(call) and not hasattr(call, "__wrapped__")

    # FastAPI remembers these answers per function object, and every app's endpoints are new objects; for a plain
    # function each answer comes from its code alone.
    is_gen, is_async_gen = fastapi_routing._is_gen_callable, fastapi_routing._is_async_gen_callable

    def shared_is_gen(call: Any) -> bool:
        return inspect.isgeneratorfunction(call) if plain_function(call) else is_gen(call)

    def shared_is_async_gen(call: Any) -> bool:
        return inspect.isasyncgenfunction(call) if plain_function(call) else is_async_gen(call)

    return_annotation = fastapi_routing.get_typed_return_annotation

    def shared_return_annotation(call: Any) -> Any:
        if not plain_function(call):
            return return_annotation(call)
        key = (call.__code__, id(call.__globals__))
        if key not in function_annotations:
            return return_annotation(call)
        return function_annotations[key].get("return")

    compile_path = functools.cache(fastapi_routing.compile_path)

    build_version = app.build_version
    package_version = build_version(app.PACKAGE_DIR)

    def shared_version(package_dir: Path) -> str:
        return package_version if package_dir == app.PACKAGE_DIR else build_version(package_dir)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(v2, "TypeAdapter", shared_adapter)
        patch.setattr(fastapi_utils, "get_typed_annotation", shared_annotation)
        patch.setattr(fastapi_utils, "_get_signature", shared_signature)
        patch.setattr(fastapi_utils, "create_model_field", shared_field)
        patch.setattr(fastapi_routing, "create_model_field", shared_field)
        patch.setattr(fastapi_routing, "_build_dependant_with_parameterless_dependencies", shared_dependant)
        patch.setattr(fastapi_routing, "_is_gen_callable", shared_is_gen)
        patch.setattr(fastapi_routing, "_is_async_gen_callable", shared_is_async_gen)
        patch.setattr(fastapi_routing, "get_typed_return_annotation", shared_return_annotation)
        patch.setattr(fastapi_routing, "compile_path", compile_path)
        patch.setattr(app, "build_version", shared_version)
        yield


@pytest.fixture
def world(tmp_path: Path, migrated_db: Path, spec_template: Path, app_parts) -> World:
    checkouts = tmp_path / "checkouts"
    checkout = checkouts / "app"
    (checkout / ".git").mkdir(parents=True)
    google = auth.GoogleClient(client_id="client-id", client_secret="s" * 24)
    spec_repo = tmp_path / "spec-repo"
    shutil.copytree(spec_template / "spec-repo", spec_repo, symlinks=True)
    shutil.copytree(spec_template / "spec-remote.git", tmp_path / "spec-remote.git", symlinks=True)
    git_config = spec_repo / ".git" / "config"
    git_config.write_text(git_config.read_text().replace(str(spec_template), str(tmp_path)))
    claude_settings = tmp_path / "claude" / "settings.json"
    claude_settings.parent.mkdir()
    claude_settings.write_text(CLAUDE_SETTINGS_TEXT)
    claude_settings.chmod(0o600)
    claude_state = tmp_path / "claude.json"
    claude_state.write_text(json.dumps({"projects": {}}))
    claude_state.chmod(0o600)
    exchange_result: dict[str, Any] = {}
    shutil.copyfile(migrated_db, tmp_path / "cockpit.db")
    (tmp_path / "cockpit.db").chmod(0o600)
    settings = Settings(
        db_path=tmp_path / "cockpit.db",
        token=TOKEN,
        cookie_key=COOKIE_KEY,
        google=google,
        email=EMAIL,
        origin=ORIGIN,
        public_origin=PUBLIC_ORIGIN,
        checkouts_dir=checkouts,
        sweep_cwd=tmp_path,
        sweep_prompt="run a sweep",
        spec_repo=spec_repo,
        spec_branch="main",
        # Created by the skills tests that need it (test_skills.py).
        skills_repo=tmp_path / "skills-repo",
        skills_folder="skills",
        skills_branch="skills",
        claude_settings=claude_settings,
        claude_rate_limits=tmp_path / "claude" / "rate-limits.json",
        claude_accounts=config.ACCOUNTS,

        codex=str(tmp_path / "codex-command"),
        claude=[sys.executable, str(FAKE_CLAUDE), str(tmp_path / "accounts" / "claude")],
        attach="claude attach {session}",
        exchange=lambda client, code, redirect, verifier: exchange_result,
        secrets_dir=tmp_path / "secrets",
        signins_dir=tmp_path / "signins",
    )
    cache = FakeCache([make_session(SESSION_A, pid=None, cwd=str(tmp_path))])
    runner = FakeRunner()
    deliverer = delivery.Deliverer(
        db_path=settings.db_path,
        sockets_dir=tmp_path / "socks",
        run=runner,
        trust=lambda account, cwd: delivery.trust_checkout(checkouts, claude_state, cwd),
        lookup=lambda session_id: next((s for s in cache.sessions if s.session_id == session_id), None),
        short_id_to_uuid=lambda account, short_id: SESSION_B,
        pick=lambda: (config.ACCOUNTS[0], "the only account in use"),
        listed=cache.get,
    )
    spend_index = spend.SpendIndex(tmp_path / "projects")
    codex_index = codex.CodexIndex((tmp_path / "codex",))
    expires = int(time.time()) + 3600

    # The app is built before the test starts, so its startup (opening the database, failing messages a restart left
    # mid-delivery) runs on the fixture's state and not on what the test then writes.
    client = TestClient(create_app(settings, deliverer, cache, spend_index, codex_index, app_parts), base_url=ORIGIN)
    client.cookies.set(auth.SESSION_COOKIE, auth.session_cookie_value(COOKIE_KEY, EMAIL, expires))
    return World(
        settings, client, settings.db_path, cache, runner, deliverer, checkout, exchange_result, spec_repo, claude_settings,
        settings.claude_rate_limits,
        claude_state,
        spend_index,
        codex_index,
    )


@pytest.fixture
def anonymous(world: World) -> TestClient:
    client = TestClient(world.client.app, base_url=ORIGIN, follow_redirects=False)
    return client


@pytest.fixture
def visitor(world: World) -> TestClient:
    """Someone with a public page's link, at the address public pages open on."""
    return TestClient(world.client.app, base_url=PUBLIC_ORIGIN, follow_redirects=False)
