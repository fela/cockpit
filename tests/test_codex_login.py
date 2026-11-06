"""Signing a Codex account in from the page (SPEC UI-77), on scratch folders with a stand-in for `codex`."""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cockpit import codex, codex_login, db, store

FAKE = Path(__file__).parent / "fake_codex.py"
CODE = "ABCD-EFGH"
NOW = datetime(2026, 2, 4, 12, tzinfo=UTC)


def part(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


def sign_in_file(home: Path, address: str) -> None:
    home.mkdir(exist_ok=True)
    token = f"{part({'alg': 'none'})}.{part({'email': address})}.x"
    (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": token, "refresh_token": "secret-refresh"}}))


def until(check, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(0.02)
    raise AssertionError("it did not happen in time")


@pytest.fixture
def world(world):
    """The app's Codex folder is `<tmp>/codex`, and its command `<tmp>/codex-command`: the stand-in."""
    root = world.codex_index.accounts[0].home.parent
    shutil.copy(FAKE, root / "codex-command")
    world.home = root / "codex"
    world.home.mkdir()
    return world


def entry(world) -> dict:
    [found] = world.client.get("/api/state").json()["codex_sign_ins"]
    return found


def test_a_folder_that_never_held_an_account_is_not_listed(world):
    assert world.client.get("/api/state").json()["codex_sign_ins"] == []


def test_a_used_folder_signed_out_before_cockpit_noted_it_shows(world):
    (world.home / "sessions").mkdir()
    [found] = world.client.get("/api/state").json()["codex_sign_ins"]
    assert found["problem"] == "signed_out" and found["held"] == ""


def test_error_text_masks_a_long_run():
    secret = "ac_" + "x" * 40
    login = codex_login._Login(None, None, "", text=f"waiting\nError: exchange failed code={secret}\n", code="ABCD-EFGH")
    said = codex_login._said(login, 1)
    assert secret not in said and said.startswith("Error: exchange failed")


def test_no_error_line_gives_only_the_exit():
    login = codex_login._Login(None, None, "", text="one-time code\nABCD-EFGH\nanything else\n")
    assert codex_login._said(login, 3) == "it exited with 3"
    assert codex_login._said(login, None) == "it was still running after a minute"


def test_a_login_whose_child_holds_the_output_is_stopped(tmp_path):
    """The login exits and a child keeps its output open: the stop still ends the child, so the reader ends."""
    process = subprocess.Popen(["bash", "-c", "sleep 60 & exit 0"], stdout=subprocess.PIPE, text=True, start_new_session=True)
    process.wait()
    codex_login.SignIn("/bin/false")._stop(process)
    assert process.stdout.read() == ""
    codex_login.SignIn("/bin/false")._stop(process)  # An empty group is no error.


def test_a_sign_in_made_during_the_question_gets_no_banked_resets(tmp_path, monkeypatch):
    home = tmp_path / "account"
    sign_in_file(home, "first@example.com")
    answer = {"id": 2, "result": {"rateLimitResetCredits": []}}
    def asked(_home, _command):
        sign_in_file(home, "second@example.com")
        return answer
    monkeypatch.setattr(codex, "ask_limits", asked)
    monkeypatch.setattr(codex, "parse_resets", lambda block, now: {"grants": []})
    account = codex.AccountIndex(home)
    account.ask("codex", NOW)
    assert account._resets == {}


def test_signing_in_shows_openais_link_and_code_and_sees_it_finish(world, caplog):
    caplog.set_level(logging.INFO)
    sign_in_file(world.home, "me@example.com")
    assert entry(world)["email"] == "me@example.com"
    assert world.client.post("/api/codex/codex/sign-in", headers=world.user()).status_code == 200
    # Codex deleted the sign-in at the press; the card shows the code, with no warning that it is signed out.
    assert not (world.home / "auth.json").exists()
    waiting = entry(world)
    assert waiting["signing_in"]["url"] == "https://auth.openai.com/codex/device"
    assert waiting["signing_in"]["code"] == CODE
    assert waiting["held"] == "me@example.com"
    assert waiting["problem"] is None
    # A second press gets the same one: no second login runs.
    assert world.client.post("/api/codex/codex/sign-in", headers=world.user()).status_code == 200

    (world.home / "finish").write_text("me@example.com")
    done = until(lambda: (found := entry(world))["finished"] and found)
    assert done["finished"]["before"] == done["finished"]["after"] == "me@example.com"
    assert done["problem"] is None and done["signing_in"] is None
    assert CODE not in caplog.text
    assert "secret-refresh" not in caplog.text


def test_agents_get_no_code_and_cannot_start_or_keep(world):
    sign_in_file(world.home, "me@example.com")
    world.client.post("/api/codex/codex/sign-in", headers=world.user())
    agent = world.agent()
    waiting = world.client.get("/api/state", headers=agent).json()["codex_sign_ins"][0]["signing_in"]
    assert set(waiting) == {"ends_at"}
    assert world.client.post("/api/codex/codex/sign-in", headers=agent).status_code == 403
    assert world.client.post("/api/codex/codex/keep", headers=agent).status_code == 403
    (world.home / "finish").write_text("me@example.com")
    until(lambda: entry(world)["finished"])


def test_a_code_that_runs_out_leaves_the_folder_signed_out_with_codexs_words(world, caplog):
    caplog.set_level(logging.INFO)
    sign_in_file(world.home, "me@example.com")
    world.client.post("/api/codex/codex/sign-in", headers=world.user())
    (world.home / "finish").write_text("fail")
    found = until(lambda: (now := entry(world))["error"] and now)
    assert found["problem"] == "signed_out"
    assert "timed out" in found["error"]
    assert CODE not in found["error"]
    assert found["held"] == "me@example.com"
    # Codex's words reach the user's page alone.
    [for_agents] = world.client.get("/api/state", headers=world.agent()).json()["codex_sign_ins"]
    assert for_agents["error"] == "" and for_agents["problem"] == "signed_out"
    assert "timed out" not in caplog.text


def test_no_link_reports_codexs_words_and_the_folder_shows_signed_out(world):
    sign_in_file(world.home, "me@example.com")
    (world.home / "no-link").touch()
    answer = world.client.post("/api/codex/codex/sign-in", headers=world.user())
    assert answer.status_code == 400
    assert "could not reach OpenAI" in answer.text
    assert entry(world)["problem"] == "signed_out"


def test_another_account_in_the_folder_shows_until_kept(world):
    sign_in_file(world.home, "me@example.com")
    world.client.post("/api/codex/codex/sign-in", headers=world.user())
    (world.home / "finish").write_text("other@example.com")
    found = until(lambda: (now := entry(world))["finished"] and now)
    assert found["problem"] == "other_account"
    assert found["finished"]["before"] == "me@example.com"
    assert found["finished"]["after"] == "other@example.com"
    assert world.client.post("/api/codex/codex/keep", headers=world.user()).status_code == 200
    kept = entry(world)
    assert kept["problem"] is None and kept["held"] == "other@example.com"


def test_an_unknown_folder_is_refused(world):
    assert world.client.post("/api/codex/..%2Fx/sign-in", headers=world.user()).status_code in (404, 405)
    assert world.client.post("/api/codex/secrets/sign-in", headers=world.user()).status_code == 404


def test_the_same_account_in_two_folders_shows(tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    sign_in_file(first, "me@example.com")
    sign_in_file(second, "me@example.com")
    conn = db.connect(tmp_path / "db.sqlite")
    db.migrate(conn)
    index = codex.CodexIndex((first, second))
    found = codex_login.status(conn, index, codex_login.SignIn("/bin/false"), True)
    assert [row["problem"] for row in found] == ["other_account", "other_account"]
    assert all(row["doubled"] for row in found)


REFUSALS = [
    {"code": -32600, "message": "codex account authentication required to read rate limits"},
    {"code": -32603, "message": "failed to fetch codex rate limits: GET https://chatgpt.com/backend-api/wham/usage failed: 401 Unauthorized; body=x"},
    {"code": -32603, "message": "Your access token could not be refreshed because your refresh token was revoked. Please log out and sign in again."},
]


@pytest.mark.parametrize("error", REFUSALS)
def test_codexs_refusals_read_as_signed_out(error):
    assert codex.refused_sign_in({"id": 2, "error": error})


@pytest.mark.parametrize("answer", [
    None,
    {"id": 2, "result": {}},
    {"id": 2, "error": {"code": -32603, "message": "failed to fetch codex rate limits: GET x failed: 503 Service Unavailable"}},
    {"id": 2, "error": {"code": -32603, "message": "failed to fetch codex rate limits: error sending request: timed out"}},
    {"id": 2, "error": {"code": -32603, "message": "failed to fetch codex rate limits: GET x failed: 503 Service Unavailable; body={\"request_id\":401}"}},
])
def test_other_failures_do_not_read_as_signed_out(answer):
    assert not codex.refused_sign_in(answer)


def account_answering(tmp_path, monkeypatch, answers):
    home = tmp_path / "account"
    sign_in_file(home, "me@example.com")
    account = codex.AccountIndex(home)
    asked = iter(answers)
    monkeypatch.setattr(codex, "ask_limits", lambda *_: next(asked))
    monkeypatch.setattr(codex, "REFUSED_ASK_AGAIN_SECONDS", 0)
    return account, home


def test_a_refusal_counts_after_a_second_answer_and_ends_with_a_new_sign_in(tmp_path, monkeypatch, caplog):
    refused = {"id": 2, "error": REFUSALS[1]}
    account, home = account_answering(tmp_path, monkeypatch, [refused, refused])
    account.ask("codex", NOW)
    assert account.refused()
    # A refused message quotes the server: nothing of it reaches the log.
    assert "Unauthorized" not in caplog.text
    sign_in_file(home, "me@example.com")
    os.utime(home / "auth.json", (1, 1))
    assert not account.refused()


def test_one_refusal_then_an_answer_is_no_refusal(tmp_path, monkeypatch):
    refused = {"id": 2, "error": REFUSALS[1]}
    good = {"id": 2, "result": {}}
    account, _ = account_answering(tmp_path, monkeypatch, [refused, good])
    account.ask("codex", NOW)
    assert not account.refused()


@pytest.mark.parametrize("name", [".codex", ".codex-2"])
def test_the_stand_in_reserves_absent_default_folders(tmp_path, name):
    user_home = tmp_path / "home"
    user_home.mkdir()
    home = user_home / name
    done = subprocess.run(
        [sys.executable, str(FAKE), "login", "--device-auth"],
        env={"HOME": str(user_home), "CODEX_HOME": str(home)},
        capture_output=True, text=True, check=False, timeout=5,
    )
    assert done.returncode == 2 and "refuses" in done.stdout
    assert not home.exists()


@pytest.mark.parametrize("name", [".codex", ".codex-2", ".codex-3", ".codex-linked", "outside-alias"])
def test_the_stand_in_refuses_discovered_folders_and_aliases(tmp_path, name):
    user_home = tmp_path / "home"
    user_home.mkdir()
    target = user_home / (".codex-3" if name == "outside-alias" else name)
    if name == ".codex-linked":
        target = tmp_path / "shared"
        target.mkdir()
        (user_home / name).symlink_to(target, target_is_directory=True)
    else:
        target.mkdir()
    home = user_home / name
    if name == "outside-alias":
        home = tmp_path / "alias"
        home.symlink_to(target, target_is_directory=True)
    sentinel = target / "auth.json"
    sentinel.write_text("synthetic sign-in stays in place")
    done = subprocess.run(
        [sys.executable, str(FAKE), "login", "--device-auth"],
        env={"HOME": str(user_home), "CODEX_HOME": str(home)},
        capture_output=True, text=True, check=False, timeout=5,
    )
    assert done.returncode == 2 and "refuses" in done.stdout
    assert sentinel.read_text() == "synthetic sign-in stays in place"


def test_the_stand_in_accepts_a_scratch_folder(tmp_path):
    user_home = tmp_path / "home"
    user_home.mkdir()
    home = tmp_path / "scratch"
    home.mkdir()
    (home / "no-link").touch()
    done = subprocess.run(
        [sys.executable, str(FAKE), "login", "--device-auth"],
        env={"HOME": str(user_home), "CODEX_HOME": str(home)},
        capture_output=True, text=True, check=False, timeout=5,
    )
    assert done.returncode == 1 and "could not reach" in done.stdout


def test_a_start_that_fails_to_run_names_the_folder(tmp_path):
    with pytest.raises(store.Invalid, match="did not start"):
        codex_login.SignIn(str(tmp_path / "missing")).start(tmp_path, "", NOW)
