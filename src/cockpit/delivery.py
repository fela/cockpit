"""Deliver a message from Cockpit to a Claude Code session.

A live session (its process runs) gets the text over its messaging socket: an
idle session starts a turn, a busy one reads it between tool calls. A stopped
session is resumed in the background with the text as its prompt. A new
session is started in the background in the chosen checkout, on Opus 5.5 at
NEW_SESSION_EFFORT. A session whose options say RETIRED_EFFORT moves to
NEW_SESSION_EFFORT before Cockpit gives it a turn, once nothing runs in it
(Deliverer._lower_effort).

A message the user wrote reaches a live session as the user's own prompt (SPEC MSG-3):
Claude Code treats a socket message as another session's words, so Cockpit
waits until the session's turn is over, stops it and resumes it with the text.
While the turn runs the message waits, queued with `waits_since` set; the
server's wake loop sends it once the turn is over, and the user's Send now stops
the session at once. All of the user's messages that wait for one session go
together, oldest first, in one prompt (SPEC MSG-7), so each new message adds
no turn and no wait of its own. A session whose turn ended while a background
command or a scheduled prompt it started runs on gets them at once as well:
the stop ends that work, and the message says so. Only a turn under way, or a
background subagent, which would lose its work, makes them wait, and they go
as Send now does once the oldest has waited WAIT_LIMIT. Agents' messages and
Cockpit's own go over the socket, except Cockpit's archive message (SPEC
SESSION-4), which waits like the user's, so the session reads it before Cockpit
stops it.

Every `claude` Cockpit runs for a session runs on the Claude account the session started on
(Session.account, SPEC SESSION-17): its resume, its stop, its effort change. A new session goes to
the account the picker names (accounts.pick), and checkouts are marked trusted in that account's state.

A message the user hands a step off with waits first for Take back (SPEC MSG-8): it
stays queued with `take_back_until` set, which no delivery claims, until the
page's notice with Take back is gone, so Take back stops it for certain.

Cockpit never sends a message twice on its own. A failure before anything reached
Claude is recorded as failed with the reason. A failure after Claude may have
the text (a socket write that broke off, a `claude --bg` that timed out, a
resume whose process never showed) is recorded as failed with "Delivery
uncertain", so the user checks the session before pressing Retry. Errors after the
text certainly arrived (looking up a new session's UUID) leave the message sent
and say what is missing; the item left unlinked shows as a warning on the page
and in `cockpit get`, and no longer waits for the sweeper, so no second session
starts for it. Cockpit cannot stop another process from starting the
same session between its lookup and the resume; deliveries Cockpit itself makes
to one session are serialized by a per-session lock and look the session up
again inside it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cockpit import accounts, config, db, limits, moves, parts, patterns, rank, removals
from cockpit.codex_agent import CodexAgent, CodexError
from cockpit.codex_worker import WorkerClient
from cockpit.config import Account, user_name
from cockpit.sessions import (
    COCKPIT_BATCH_SEPARATOR,
    LookupFailed,
    Session,
    claude_command,
    last_model,
    prefixed,
    turn_ended,
)
from cockpit.store import (
    ARCHIVE_PENDING,
    SWEEP_SESSION_NAMES,
    WORKING_LIMIT,
    Conflict,
    Invalid,
    NotFound,
    chosen_effort,
    close_refusal,
    create_message,
    defused,
    end_archive,
    from_ask,
    stop_refusal,
    working_sessions,
)

log = logging.getLogger(__name__)

SOCKET_TIMEOUT_SECONDS = 5
COMMAND_TIMEOUT_SECONDS = 60
# After a resume the session's process shows up in the listing; the lock is held
# until then so a quick second message is written to the socket instead of
# resuming again (which would start a copy). On a loaded host a `claude agents`
# listing can run to its timeout and a resumed session shows late, so the wait
# covers several listings.
RESUME_SETTLE_SECONDS = 180
# A stopped process leaves the listing's pid empty within seconds; a restart
# resumes the session only after that, or Claude would start a copy.
STOP_SETTLE_SECONDS = 30
# Claude's daemon can still count a stopped session as running a moment after the
# listing shows no process, and a resume then starts a copy; the resume waits this long.
STOP_GRACE_SECONDS = 5
# When Cockpit stops, such as for a deploy's restart, it waits this long at most for the deliveries under way, so a
# Run's result or an agent's message is not cut off mid-send (SPEC MSG-5). Below systemd's 90 s stop timeout. A
# resume's wait for its process can outlast it on a loaded host; Claude has the message by then, the restart marks
# it uncertain (mark_interrupted), and store.confirm_uncertain finds it in the session's transcript.
DRAIN_SECONDS = 75
RESUME_POLL_SECONDS = 1.0
# A new session can take a moment to appear in `claude agents`; Cockpit looks for
# it this long. A session not found by
# then is not linked to its item; the item shows as a warning instead
# (store.unlinked_warnings).
NEW_SESSION_LOOKUP_SECONDS = 30
NEW_SESSION_LOOKUP_PAUSE_SECONDS = 2.0
# A session Cockpit starts does the work and checks that it works before it calls a step ready, so it runs the
# strongest model, at high effort.
NEW_SESSION_MODEL = config.MAIN_MODEL
NEW_SESSION_EFFORT = "high"
# A background session keeps the options it started with. A resume cannot pass --effort (Claude then starts a copy), so
# Cockpit resumes the session with the command `/effort NEW_SESSION_EFFORT`, which Claude Code runs without calling the
# model and saves in the session's options. Change the two together.
RETIRED_EFFORT = "xhigh"
WAIT_LIMIT = timedelta(minutes=15)
# How long Cockpit waits for a session's turn on its archive message to end before it leaves the
# session running (SPEC SESSION-4).
ARCHIVE_WAIT_LIMIT = timedelta(hours=1)
# Cockpit stops a live background session whose turn is over, with nothing running or waiting in it (stop_refusal; an
# open step to it needs no process, since the user's answer resumes it), once it has been idle IDLE_STOP_AFTER (SPEC
# SESSION-15). Claude keeps its conversation, and the prompt cache is gone after an hour either way, so the stop costs
# no tokens; it frees process memory, and the next message resumes it on the current Claude Code.
IDLE_STOP_AFTER = timedelta(hours=1)
# While less than LOW_MEMORY_SHARE of the machine's memory is available, an idle session stops after
# IDLE_STOP_AFTER_LOW_MEMORY instead. A session resumed within the hour may then write its prompt cache again; that
# costs less than work the kernel kills.
LOW_MEMORY_SHARE = 0.3
IDLE_STOP_AFTER_LOW_MEMORY = timedelta(minutes=15)
# A delivery that started a session's work holds its place this long after it ends, or until the listing shows the
# session at work: a resumed or new session takes a moment to show as working.
PLACE_SECONDS = 120
# Told to a session Cockpit stopped before its turn was over, after the user's message.
CUT_SHORT = (
    "Cockpit stopped this session before your turn was over, to give you this message. Your background commands,"
    " subagents and scheduled prompts have ended. After handling the message, start again the ones you still need."
)
# Told to a session Cockpit stopped after its turn was over while background work it started ran on.
AFTER_TURN = (
    "Cockpit stopped this session after your turn was over, to give you this message. Your background commands,"
    " subagents and scheduled prompts have ended. Start again the ones you still need."
)
# Added to a delivery's detail when Cockpit moved the session off RETIRED_EFFORT first.
LOWERED = f"; first moved it from {RETIRED_EFFORT} to {NEW_SESSION_EFFORT} effort"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_BACKGROUNDED = re.compile(r"backgrounded · (\S+)")
# Claude's own notes when a resume started a copy instead.
_COPY = re.compile(r"started a (?:restricted )?copy(?: of that conversation)? as ([\w-]+)")

Runner = Callable[[Account, list[str], Path], subprocess.CompletedProcess[str]]
Trust = Callable[[Account, Path], None]
Lookup = Callable[[str], Session | None]
ShortIdLookup = Callable[[Account, str], str | None]
# The account a new session goes to, and why in words (accounts.pick).
Pick = Callable[[], tuple[Account, str]]
# Every session of every provider, as the page lists them; may be a few seconds old.
Listed = Callable[[], list[Session]]


class DeliveryError(Exception):
    """The message did not reach the session, or may have; the text says which and why."""


class ListingFailed(DeliveryError):
    """Listing Claude's sessions failed, so Cockpit cannot tell which account's copy runs; trying later may work."""


class Waiting(Exception):
    """The user's message waits for the live session's turn to end; nothing was sent (SPEC MSG-3)."""


class AtCapacity(Exception):
    """WORKING_LIMIT sessions work already, so the message waits for a place; nothing was sent (SPEC SESSION-23)."""


def uncertain(what: str) -> DeliveryError:
    return DeliveryError(f"Delivery uncertain: {what}. Check the session before retrying.")


def run_command(account: Account, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    # Large prompts travel on stdin so each command argument stays within the operating system's limit.
    prompt = None
    if "--bg" in args and len(args[-1].encode("utf-8")) > 64 * 1024:
        prompt, args = args[-1], args[:-1]
    return subprocess.run(
        claude_command(account, args), cwd=cwd, capture_output=True, text=True, input=prompt,
        timeout=COMMAND_TIMEOUT_SECONDS, check=False,
    )


def reply_line(message: dict[str, Any]) -> str:
    """How to answer an agent's message (SPEC MSG-5): Claude Code's SendMessage only reaches sessions on its own
    account, and the sender's --as name is no session name, so the receiver gets the command that reaches it."""
    about = f" --item {message['item_id']}" if message["item_id"] is not None else ""
    return (f"To reply, run `cockpit send --session {message['from_session']}{about} --as <your name>` with your reply"
            " on stdin. SendMessage does not reach it.")


def compose(conn: Any, message: dict[str, Any]) -> str:
    """The delivered text: the sender line, the message, and the step it answers.

    A new session knows nothing about the item, so it also gets the step's body and links; a new
    session for an item without a step (a to-do the user added, SPEC CONTROL-8) gets the item's place.
    The context carries agents' words (the step's ask and body, the item's title and links), so a line in
    it that looks like a sender header is marked (`defused`): it cannot pass for the user's or Cockpit's.
    """
    text = prefixed(message["author"], message["text"])
    if message["images"]:
        # The images the user sent with the words (SPEC MSG-18), on the message's task.
        ids = ", ".join(map(str, message["images"]))
        text += (f"\n\n{user_name()} sent {'this image' if len(message['images']) == 1 else 'these images'} with the"
                 f" message: {ids}. Save each with `cockpit image get ID --out FILE`, then open the file to see it.")
    if message["from_session"] is not None:
        text += "\n\n" + reply_line(message)
    # A message that starts a session (`new_cwd`) is its first prompt until delivered, also on a retry after
    # Codex created the thread but failed to start its first turn (the thread is then its `session_id`).
    first = message["session_id"] is None or message["new_cwd"] is not None
    if message["step_id"] is None:
        if first and message["item_id"] is not None:
            lines = _new_item_session(conn, message["item_id"])
            if message["fork_of"] is not None:
                lines += _forked(message)
            return text + "\n" + defused("\n".join(lines))
        return text
    step = db.row(conn.execute("SELECT * FROM steps WHERE id = ?", (message["step_id"],)).fetchone())
    if step is None:
        return text
    item = db.row(conn.execute("SELECT key, title, links, fire_and_forget FROM items WHERE id = ?", (step["item_id"],)).fetchone())
    handed = step["handoff_message_id"] == message["id"]
    # A later message to the session that holds the step, such as the output of a command the user ran from it,
    # leaves the step with that session.
    holder = (
        conn.execute("SELECT session_id FROM messages WHERE id = ?", (step["handoff_message_id"],)).fetchone()
        if step["handoff_message_id"] is not None and not handed
        else None
    )
    held = holder is not None and message["session_id"] is not None and holder[0] == message["session_id"]
    relation = f"{user_name()} hands you" if handed else "this answers"
    lines = [
        "",
        "---",
        f"Cockpit context: {relation} step {step['id']}.",
        f"Item {step['item_id']}: {item['title']}",  # ty: ignore[not-subscriptable]
        f"Key: {item['key']}",  # ty: ignore[not-subscriptable]
        f"The step asked: {message['step_ask']}",
    ]
    if step["ask"] != message["step_ask"]:
        lines.append(f"The step has changed since; it now asks: {step['ask']}")
    if first:
        if step["body"].strip():
            lines += ["", step["body"]]
        if item["links"]:  # ty: ignore[not-subscriptable]
            lines += ["", "Links: " + ", ".join(f"[{link['label']}]({link['url']})" for link in item["links"])]  # ty: ignore[not-subscriptable]
        # A new session on a fire-and-forget to-do (SPEC CONTROL-24), such as one started from its blocker step,
        # works the same way as the first.
        if item["fire_and_forget"] and not message["completes"]:  # ty: ignore[not-subscriptable]
            lines += ["", *_fire_and_forget(step["item_id"])]
    if message["completes"]:
        # A closing answer or a last message sent with Task done (SPEC CONTROL-15): the user's press completed the
        # item, so its steps are closed already.
        lines += [
            "",
            f"{user_name()} completed this item with that message. Cockpit closed its steps and moved it to stage done.",
            f"Do what the message says in this turn. Then commit, push and end without a question for {user_name()}.",
            "Cockpit archives this session after the turn, unless the session has other open steps.",
            (
                "If the last part fails or work is left after all, put the item back at the stage the work is in. Say"
                f" what is left in its summary. Put a step if {user_name()} has to act on it."
            ),
        ]
    elif handed or held:
        lines += [
            "",
            (
                f"The step is out of {user_name()}'s queue until you have dealt with it."
                if handed
                else f"{user_name()} handed you this step earlier. You still hold it, and it stays out of their queue"
                " until you have dealt with it."
            ),
        ]
        if rank.finishing(step):
            lines += [
                f"This is the item's finish step: only {user_name()} closes the task.",
                (
                    "First do everything the message asks. Then put the item's summary as where it stands now, and"
                    f' return the step with your reply: `cockpit step return {step["id"]} --as <your name> --note'
                    ' "<your reply>"`.'
                ),
                (
                    "Cockpit then rewrites the step. Its ask becomes your reply's first sentence. Below it come"
                    f" {user_name()}'s message and your reply, then the task and where it stands."
                ),
                (
                    "Keep the step while part of what the message asks is still pending, such as a PR to ship, a"
                    " review or a reply. Rewrite its ask as what is left and who holds it, and put the item's"
                    " waiting_on."
                ),
            ]
        else:
            lines += [
                f"When nothing is left for {user_name()}, close it: `cockpit step done {step['id']} --as <your name>`.",
                (
                    f"When {user_name()} asked a question or still has to decide something, rewrite the step with"
                    " `cockpit step put` on the same item and need. Its first lines answer each question plainly."
                    f" Its ask is what {user_name()} decides now. Then return it to their queue:"
                    f" `cockpit step return {step['id']} --as <your name>`."
                ),
            ]
            if message["author"] == "user":
                lines.append(
                    f"Before you put a rewrite, check it against {user_name()}'s message and the step as"
                    f" {user_name()} saw it. Make sure the revised step answers the message and explains the next action."
                )
    elif step["status"] != "open":
        lines += ["", f"The step is {step['status']} now."]
    elif message["hands_off"] or step["handoff_message_id"] is not None:
        lines += [
            "",
            (
                f"This message no longer hands you the step. It went back to {user_name()}, or a later message holds"
                " it now. Leave the step as it is."
            ),
        ]
    else:
        lines += [
            "",
            f"When the message settles that step, act on it, then close it: `cockpit step done {step['id']} --as <your name>`.",
        ]
    return text + "\n" + defused("\n".join(lines))


class SocketUnreachable(OSError):
    """The connection failed, so nothing was written."""


def write_to_socket(path: Path, text: str) -> None:
    line = json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(SOCKET_TIMEOUT_SECONDS)
        try:
            connection.connect(str(path))
        except OSError as exc:
            raise SocketUnreachable(str(exc)) from exc
        connection.sendall(line.encode())


# The entry Claude Code itself writes for a folder whose trust prompt was accepted.
TRUSTED_PROJECT = {
    "allowedTools": [],
    "mcpContextUris": [],
    "mcpServers": {},
    "enabledMcpjsonServers": [],
    "disabledMcpjsonServers": [],
    "hasTrustDialogAccepted": True,
    "hasClaudeMdExternalIncludesApproved": False,
    "hasClaudeMdExternalIncludesWarningShown": False,
}
# Deliveries to different sessions run at once; each trust is a read, a change and a replace of the account's state.
TRUST_LOCK = threading.Lock()


def trust_checkout(checkouts_dir: Path, claude_state: Path, cwd: Path) -> None:
    """Mark the checkout that holds `cwd` trusted in Claude Code's state, before a `claude --bg` there.

    `claude --bg` refuses ("Workspace not trusted") in a git checkout without a trusted entry of its own:
    Claude Code looks for trust from the start folder up to the nearest git checkout and in the main
    checkout of a worktree, never above a checkout. So a checkout (the folder with a `.git` nearest above
    `cwd` inside the checkouts directory, at any depth, as a project's own checkout may be) gets an entry
    when it has none, and its entry set trusted when it says untrusted. A folder there inside no git
    checkout, and the checkouts directory itself, take the directory's trust, which is recorded separately in
    each account's state. A directory outside the checkouts folder stays unchanged."""
    try:
        parts = cwd.resolve().relative_to(checkouts_dir.resolve()).parts
    except ValueError:
        return
    folders = [checkouts_dir.joinpath(*parts[:depth]) for depth in range(len(parts), 0, -1)]
    checkout = str(next((folder for folder in folders if (folder / ".git").exists()), checkouts_dir))
    with TRUST_LOCK:
        state = json.loads(claude_state.read_text())
        projects = state.setdefault("projects", {})
        project = projects.get(checkout)
        if project is None:
            projects[checkout] = dict(TRUSTED_PROJECT)
        elif project.get("hasTrustDialogAccepted"):
            return
        else:
            project["hasTrustDialogAccepted"] = True
        # Claude Code rewrites this file often; replacing it whole keeps any reader from seeing half of it.
        descriptor, name = tempfile.mkstemp(prefix=".claude.json.cockpit-", dir=claude_state.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w") as handle:
                handle.write(json.dumps(state, indent=2))
                os.fchmod(handle.fileno(), claude_state.stat().st_mode & 0o777)
            temporary.replace(claude_state)
        finally:
            temporary.unlink(missing_ok=True)
    log.warning("marked %s trusted in %s so claude --bg can start there", checkout, claude_state)


def _run_claude(run: Runner, account: Account, args: list[str], cwd: Path, what: str) -> str:
    """Run a `claude --bg` command on `account`; a timeout or signal leaves it unknown whether it started."""
    try:
        result = run(account, args, cwd)
    except subprocess.TimeoutExpired as exc:
        raise uncertain(f"{what} did not finish within {COMMAND_TIMEOUT_SECONDS} s") from exc
    output = _ANSI.sub("", (result.stdout or "") + (result.stderr or ""))
    if result.returncode < 0:
        raise uncertain(f"{what} was stopped by signal {-result.returncode}")
    if result.returncode != 0:
        raise DeliveryError(f"{what} exited {result.returncode}: {output.strip()[-500:]}")
    return output


def _refuse_copy(output: str, session: Session, what: str) -> None:
    """An in-place resume keeps the short id; a copy gets a new one."""
    resumed_as = {found.group(1) for pattern in (_BACKGROUNDED, _COPY) for found in pattern.finditer(output)}
    copies = sorted(resumed_as - {session.id})
    if copies:
        notes = [line.strip().removeprefix("note:").strip(" .") for line in output.splitlines() if line.strip().startswith("note:")]
        said = " ".join(f"{note}." for note in notes if note) or "none."
        raise DeliveryError(
            f"Claude started a copy, {', '.join(copies)}, with {what} instead of resuming {session.id}."
            f" Claude's note: {said} Check both sessions before retrying. Stop the copy, or answer"
            " there."
        )


@dataclass(frozen=True)
class Sent:
    via: str  # socket | resume | new
    session_id: str | None  # for a new session: its UUID once known
    detail: str


@dataclass(eq=False)
class Place:
    """One of the WORKING_LIMIT places, held by a delivery that starts a session's work until the listing shows that
    session at work (SPEC SESSION-23)."""

    session_id: str | None  # None for a new session whose UUID is not known
    sent_at: float | None  # time.monotonic() when the delivery ended; None while it is under way


@dataclass
class Deliverer:
    db_path: Path
    sockets_dir: Path
    run: Runner
    trust: Trust
    lookup: Lookup
    short_id_to_uuid: ShortIdLookup
    pick: Pick
    listed: Listed
    codex: CodexAgent | WorkerClient | None = None
    default_provider: str = "claude"
    enabled_providers: tuple[str, ...] = ("claude",)

    def __post_init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._sending = 0
        self._sends_done = threading.Condition()
        self._places: list[Place] = []
        self._places_guard = threading.Lock()

    def _lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def has_session(self, session_id: str) -> bool:
        """Whether a message to `session_id` has a session to go to: a Codex thread Cockpit runs, or a Claude
        session on this machine."""
        if self.codex is not None and self.codex.owns(session_id):
            return True
        return self.lookup(session_id) is not None

    def wait_for_start(self, cwd: str) -> None:
        """Return once no delivery is starting a new session in `cwd`: its message then names the session."""
        with self._lock(f"new:{cwd}"):
            return

    def send(self, message_id: int) -> None:
        """Deliver one queued message and record the outcome; safe to run in a thread."""
        self._send(message_id, restart=False, past_limit=False)

    def start(self, name: str, target: Callable[..., None], *args: Any) -> None:
        """Run `target(*args)` on a thread of its own that `drain` waits for: a send, or work that ends in one, such
        as a Run, whose thread delivers its start message, waits for the command and then delivers its result. The
        thread counts from before it starts, so no send slips past a drain between two counted parts."""
        with self._sends_done:
            self._sending += 1

        def counted() -> None:
            try:
                target(*args)
            finally:
                self._sent()

        try:
            threading.Thread(target=counted, name=name, daemon=True).start()
        except BaseException:
            self._sent()
            raise

    def drain(self, seconds: float) -> int:
        """Wait until no delivery is under way, `seconds` at most; returns how many still were. Cockpit calls it when
        it stops, so a restart does not cut a message off mid-send (SPEC MSG-5): `cockpit deploy` restarts Cockpit
        3 s after it ends, while the Run that started it is still delivering its start message or its result."""
        deadline = time.monotonic() + seconds
        with self._sends_done:
            while self._sending and (left := deadline - time.monotonic()) > 0:
                self._sends_done.wait(left)
            return self._sending

    def restart(self, message_id: int) -> None:
        """Stop the live session the message goes to, then deliver the message, which resumes it. For a
        session held at a dialog in its terminal that Cockpit cannot answer (SPEC PERM-8)."""
        self._send(message_id, restart=True, past_limit=True)

    def send_past_limit(self, message_id: int) -> None:
        """Deliver a message that waits for a place among the working sessions without one: the user's Send now on
        it (SPEC SESSION-23)."""
        self._send(message_id, restart=False, past_limit=True)

    def places_due(self, conn) -> list[int]:
        """The messages that waited for a place and go now (SPEC SESSION-23), longest wait first: one for each free
        place, and each whose delivery starts no session's work any more, since its session works by now or is gone
        (that delivery then fails and says so)."""
        with self._places_guard:
            free = WORKING_LIMIT - self._taken()
        due: list[int] = []
        placed: set[str] = set()
        for message in db.rows(conn.execute(
            "SELECT * FROM messages WHERE state = 'queued' AND capacity_since IS NOT NULL ORDER BY capacity_since, id"
        ).fetchall()):
            session_id = message["session_id"]
            if session_id in placed or not starts_work(message, self._session(session_id)):
                due.append(message["id"])
            elif free > 0:
                free -= 1
                due.append(message["id"])
                if session_id is not None:
                    placed.add(session_id)
        return due

    def _session(self, session_id: str | None) -> Session | None:
        """The session a message goes to, of either provider; None for a message that starts one."""
        if session_id is None:
            return None
        if self.codex is not None and self.codex.owns(session_id):
            return self.codex.lookup(session_id)
        return self.lookup(session_id)

    def _taken(self) -> int:
        """How many of the WORKING_LIMIT places are taken: the sessions the listing shows at work, and the deliveries
        that started a session's work which it does not show yet. A new session whose UUID Claude did not report
        counts twice until its place ends after PLACE_SECONDS. The caller holds `_places_guard`."""
        working = {session.session_id for session in working_sessions(self.listed())}
        now = time.monotonic()
        self._places = [
            place for place in self._places
            if place.sent_at is None or (now - place.sent_at < PLACE_SECONDS and place.session_id not in working)
        ]
        return len(working) + sum(1 for place in self._places if place.session_id not in working)

    def _take_place(self, conn, message: dict, past_limit: bool) -> Place | None:
        """A place for the delivery of `message`; None when it starts no session's work. A session that still has
        the place of an earlier delivery keeps that one, counted as under way again. Raises AtCapacity for a message
        that may wait (`may_wait`) while every place is taken or promised to a message that waits longer."""
        if not starts_work(message, self._session(message["session_id"])):
            return None
        with self._places_guard:
            taken = self._taken()
            # The session's lock lets one delivery to it run at a time, so a place it has is of one that ended.
            session_id = message["session_id"]
            kept = next((place for place in self._places if place.session_id == session_id), None)
            if session_id is not None and kept is not None:
                kept.sent_at = None
                return kept
            if not past_limit and may_wait(conn, message) and taken + held_ahead(conn, message) >= WORKING_LIMIT:
                raise AtCapacity(f"{taken} sessions work, and at most {WORKING_LIMIT} work at once")
            place = Place(message["session_id"], None)
            self._places.append(place)
            return place

    def _deliver_in_place(self, conn, message: dict, text: str, restart: bool, waited: bool, past_limit: bool) -> Sent:
        """`_deliver`, counted among the working sessions from before it starts (SPEC SESSION-23). A delivery that
        fails keeps its place for PLACE_SECONDS too: the session may have started all the same."""
        place = self._take_place(conn, message, past_limit)
        session_id = None
        try:
            sent = self._deliver(message, text, restart, waited)
            session_id = sent.session_id
            return sent
        finally:
            if place is not None:
                with self._places_guard:
                    place.session_id = place.session_id or session_id
                    place.sent_at = time.monotonic()

    def archive(self, archive_id: int) -> None:
        """Stop the session of one of the user's archives (SPEC SESSION-4) whose turn on Cockpit's message is
        over (archives_due), and record the outcome; safe to run in a thread. Once every item the stopped session
        belongs to is closed, Cockpit takes it off `claude agents` (removals.py); Claude keeps its conversation."""
        conn = db.connect(self.db_path)
        try:
            archive = db.row(conn.execute("SELECT * FROM session_archives WHERE id = ?", (archive_id,)).fetchone())
            if archive is None:
                raise LookupError(f"session archive {archive_id} does not exist")
            with self._lock(archive["session_id"]):
                # The session may have kept itself, or the user reopened the step, since the poll listed it. The
                # archive is claimed in one write before the stop, so a cancel lands either before it (no stop) or
                # after the stop, as a cancel of a stopped session; end_archive records the outcome. The lookup
                # comes first: when it fails, the archive stays pending for the next poll, as it does when the
                # session started another turn since the poll found its turn over.
                session = self.lookup(archive["session_id"])
                if session is not None and parts.is_standing(session.session_id):
                    end_archive(conn, archive_id, parts.standing().stop_refusal)
                    return
                if session is not None and session.alive and not session.turn_over:
                    return
                claimed = conn.execute(
                    f"UPDATE session_archives AS a SET stopped_at = ? WHERE a.id = ? AND {ARCHIVE_PENDING}",
                    (db.now_iso(), archive_id),
                )
                if claimed.rowcount == 0:
                    return
                failure = ""
                if session is not None and session.pid is not None:
                    try:
                        if session.provider == "codex" and self.codex is not None:
                            self.codex.stop(session.session_id)
                        else:
                            _run_claude(
                                self.run, config.account(session.account), ["claude", "stop", session.id],
                                Path.home(), "claude stop",
                            )
                    except Exception as exc:
                        known = isinstance(exc, (DeliveryError, CodexError))
                        failure = str(exc) if known else f"{type(exc).__name__}: {exc}"
                        log.error("archiving session %s failed: %s", archive["session_id"], failure, exc_info=not known)
                end_archive(conn, archive_id, failure)
        finally:
            conn.close()

    def close(self, session_id: str, reason: str, actor: str) -> dict[str, Any]:
        """Stop a live session the sweeper judged done, unless something in it still runs or waits
        (close_refusal), and record why. Claude keeps its conversation, so a later message resumes it.
        A session already stopped stays so, and the close is recorded all the same: it
        ended on purpose, so the usage limit's reset does not wake it (limits.ended_on_purpose). One with an open
        step is refused, as a live one is."""
        conn = db.connect(self.db_path)
        try:
            with self._lock(session_id):
                session = self.lookup(session_id)
                if session is None:
                    raise NotFound(f"no Claude session {session_id} on this machine")
                refusal = close_refusal(conn, session)
                if refusal is not None:
                    raise Conflict(f"Cockpit keeps session {session.id} {'running' if session.alive else 'open'}: {refusal}")
                if not session.alive:
                    self._record_closed(conn, session, reason, actor)
                    return {"session_id": session_id, "closed": False, "detail": f"session {session.id} was already stopped"}
                return self._stop_closed(conn, session, reason, actor)
        finally:
            conn.close()

    def close_idle(self, session_id: str, now: datetime, after: timedelta) -> dict[str, Any]:
        """Stop a session idle_stop found idle, judged again on a fresh listing under the session's lock: one that
        since started a turn, a background command or a scheduled prompt, or woke and ended a turn, keeps running."""
        conn = db.connect(self.db_path)
        try:
            with self._lock(session_id):
                session = self.lookup(session_id)
                if session is None:
                    raise NotFound(f"no Claude session {session_id} on this machine")
                reason = idle_stop(conn, session, now, after)
                if reason is None:
                    raise Conflict(f"session {session.id} is no longer idle with nothing running or waiting in it")
                return self._stop_closed(conn, session, reason, "cockpit")
        finally:
            conn.close()

    def remove(self, session_id: str, busy: Callable[[], set[Path]]) -> None:
        """Take a finished session off `claude agents` (removals.due), judged again on a fresh listing under the
        session's lock, on every account that lists it. Its options and worktree are recorded first, for a later
        resume. When `claude rm` refuses, as for a worktree with unpushed work, the session stays listed and the
        refusal is recorded. `busy` lists afresh the folders running sessions work in (removals.busy); no worktree
        is added again meanwhile (removals.WORKTREES)."""
        conn = db.connect(self.db_path)
        try:
            with self._lock(session_id), removals.WORKTREES:
                session = self.lookup(session_id)
                if session is None:
                    return
                reason = removals.keeps(conn, session, removals.pinned(config.ACCOUNTS), busy())
                if reason is not None:
                    log.info("kept session %s in `claude agents`: %s", session.id, reason)
                    return
                holders = removals.holders(config.ACCOUNTS, session)
                if not session.removed_at:
                    own = holders.pop(0)
                    tree = removals.entered(session)
                    with db.transaction(conn):
                        removals.ask(
                            conn, session, removals.job_flags(own, session.id), removals.worktree(tree) if tree else None
                        )
                    try:
                        _run_claude(self.run, own, ["claude", "rm", session.id], Path.home(), "claude rm")
                    except DeliveryError as exc:
                        with db.transaction(conn):
                            removals.refused(conn, session_id, str(exc))
                        log.warning("left finished session %s in `claude agents`: %s", session.id, exc)
                        return
                    with db.transaction(conn):
                        removals.removed(conn, session_id)
                    log.info("took finished session %s off `claude agents`", session.id)
                # A copy another account still lists, as after a move; one left behind is due again next poll.
                for account in holders:
                    try:
                        _run_claude(self.run, account, ["claude", "rm", session.id], Path.home(), "claude rm")
                    except DeliveryError as exc:
                        with db.transaction(conn):
                            removals.refused(conn, session_id, f"on the {account.name} account: {exc}")
                        log.error("left the %s account's copy of removed session %s in `claude agents`: %s",
                                  account.name, session.id, exc)
        finally:
            conn.close()

    def _stop_closed(self, conn, session: Session, reason: str, actor: str) -> dict[str, Any]:
        """Stop `session`, which the caller judged done under its lock, and record why."""
        # No resume follows, so the stop runs where it can even when the session's directory is gone.
        self._halt(session, Path.home())
        self._record_closed(conn, session, reason, actor)
        return {"session_id": session.session_id, "closed": True, "detail": f"stopped session {session.id}"}

    def _record_closed(self, conn, session: Session, reason: str, actor: str) -> None:
        with db.transaction(conn):
            patterns.record(
                conn, "session.closed", actor, None, None, None,
                {"session_id": session.session_id, "reason": reason, "idle_since": session.updated_at,
                 "in_flight": session.in_flight},
            )
        log.info("%s closed session %s: %s", actor, session.id, reason)

    def move(
        self, session_id: str, account_name: str, actor: str, checkouts_dir: Path, waited_since: str | None
    ) -> dict[str, Any]:
        """Move a Claude session to another account, as the user decided (moves.py, SPEC SESSION-18). A live session whose
        turn is over is stopped on its old account first; for one that is busy the move waits (`waits`), and
        Cockpit carries it out once its turn is over (moves.waits_due). A session the usage limit stopped gets a
        message to continue, which resumes it on the new account: returned as `message_id` for the caller to
        deliver. `waited_since`: for a waiting move Cockpit carries out, the wait's `asked_at`; the move happens
        only while that wait still stands, so one the user replaced or ended meanwhile does nothing. None for an
        ask."""
        if parts.is_standing(session_id):
            raise Conflict(parts.standing().keeps)
        try:
            account = config.account(account_name)
        except LookupError as exc:
            raise Invalid(str(exc)) from exc
        if accounts.retired(account):
            raise Conflict(f"the {account.name} Claude account is retired; sessions no longer run on it")
        if account not in accounts.in_use(config.ACCOUNTS, []):
            raise Conflict(f"the {account.name} Claude account has no sign-in; sign it in first")
        if accounts.signed_out(account, datetime.now(UTC)):
            raise Conflict(f"the {account.name} Claude account is signed out; sign it in again first")
        conn = db.connect(self.db_path)
        try:
            with self._lock(session_id):
                try:
                    session = self.lookup(session_id)
                except (OSError, subprocess.SubprocessError, ValueError) as exc:
                    # A failed listing leaves open which account's copy runs; moving then could leave it running.
                    raise ListingFailed(f"not moved: listing Claude sessions failed: {exc}") from exc
                if waited_since is not None and moves.standing_wait(conn, session_id, account.name, waited_since) is None:
                    return {"session_id": session_id, "moved": False, "waits": False, "message_id": None,
                            "detail": f"the move of session {session_id[:8]} to {account.name} no longer waits"}
                if session is None:
                    raise NotFound(f"no Claude session {session_id} on this machine")
                if session.provider != "claude":
                    raise Conflict(f"session {session.id} is a {session.provider} session; only Claude sessions move")
                if session.account == account.name:
                    with db.transaction(conn):
                        moves.end_wait(conn, session_id, None)
                    return {"session_id": session_id, "moved": False, "waits": False, "message_id": None,
                            "detail": f"session {session.id} is already on the {account.name} account"}
                if session.alive:
                    # The stop on the old account would end whatever still runs in it.
                    if not session.job_state:
                        raise Conflict(f"session {session.id} is not a background session; move it from its terminal")
                    if not session.turn_over:
                        with db.transaction(conn):
                            moves.wait(conn, session_id, account.name, actor)
                        held = " and ".join(session.busy_with)
                        log.info("%s's move of session %s to %s waits: it has %s", actor, session.id, account.name, held)
                        return {"session_id": session_id, "moved": False, "waits": True, "message_id": None,
                                "detail": f"session {session.id} still has {held}; Cockpit moves it to the"
                                          f" {account.name} account once that is over"}
                    self._halt(session, Path.home())
                with db.transaction(conn):
                    # Read in the move's transaction, so a continue limits.sync queued meanwhile is the only one.
                    stopped_on_limit = bool(limits.waiting(conn, [session], datetime.now(UTC)))
                    moves.record(conn, session_id, account.name)
                    moves.end_wait(conn, session_id, None)
                    patterns.record(
                        conn, "session.moved", actor, None, None, None,
                        {"session_id": session_id, "from": session.account, "to": account.name},
                    )
                    message_id = None
                    if stopped_on_limit:
                        text = limits.CONTINUE.format(why=f"{user_name()} moved you to the {account.name} account")
                        message_id = create_message(conn, "cockpit", text, {"session_id": session_id}, checkouts_dir)["id"]
                moves.close_steps(conn, session_id)
                log.info("%s moved session %s from %s to %s", actor, session.id, session.account, account.name)
                return {"session_id": session_id, "moved": True, "waits": False, "message_id": message_id,
                        "detail": f"moved session {session.id} to the {account.name} account"}
        finally:
            conn.close()

    def _send(self, message_id: int, restart: bool, past_limit: bool) -> None:
        with self._sends_done:
            self._sending += 1
        try:
            self._send_counted(message_id, restart, past_limit)
        finally:
            self._sent()

    def _sent(self) -> None:
        with self._sends_done:
            self._sending -= 1
            self._sends_done.notify_all()

    def _send_counted(self, message_id: int, restart: bool, past_limit: bool) -> None:
        conn = db.connect(self.db_path)
        try:
            message = db.row(conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone())
            if message is None:
                raise LookupError(f"message {message_id} does not exist")
            key = message["session_id"] or f"new:{message['new_cwd']}"
            with self._lock(key):
                claimed = conn.execute(
                    "UPDATE messages SET state = 'sending' WHERE id = ? AND state = 'queued' AND take_back_until IS NULL",
                    (message_id,),
                )
                if claimed.rowcount == 0:
                    log.warning("message %s is no longer queued, or still waits for Take back; not sent", message_id)
                    return
                # From the claim on, a failure records the claimed messages as failed, so none stays 'sending'.
                batch, ids = [message], [message_id]
                try:
                    # Read again once claimed: a follow-up (MSG-10) may have joined its text since the read above.
                    message = db.row(conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone())
                    assert message is not None  # the claim above just updated its row
                    # Before the claim: an answer that comes in after this check already waits, so the claim takes
                    # it along.
                    if message["author"] == "user" and not restart and _ask_answering(conn, message["session_id"]):
                        raise Waiting("a copy of the session still answers a quick question, which goes with it")
                    batch = _in_order(conn, [message, *_claim_waiting(conn, message)])
                    ids = [each["id"] for each in batch]
                    if message["as_prompt"] and not restart and _older_waits(conn, message):
                        raise Waiting(f"an older message of {user_name()}'s to this session waits")
                    if not restart and all(from_ask(conn, each["id"]) for each in batch) and _holds_asks(self.lookup(message["session_id"])):
                        raise Waiting("an Ask's question and answer wait for the session's turn to end for good")
                    now = datetime.now(UTC)
                    waited = message["as_prompt"] and any(waited_out(conn, each, now) for each in batch)
                    sent = self._deliver_in_place(conn, message, compose_batch(conn, batch), restart, waited, past_limit)
                except Waiting as wait:
                    with db.transaction(conn):
                        conn.executemany(
                            "UPDATE messages SET state = 'queued', capacity_since = NULL,"
                            " waits_since = COALESCE(waits_since, ?) WHERE id = ?",
                            [(db.now_iso(), each) for each in ids],
                        )
                    log.info("message(s) %s wait: %s", ids, wait)
                    return
                except AtCapacity as wait:
                    with db.transaction(conn):
                        conn.executemany(
                            "UPDATE messages SET state = 'queued', waits_since = NULL,"
                            " capacity_since = COALESCE(capacity_since, ?) WHERE id = ?",
                            [(db.now_iso(), each) for each in ids],
                        )
                    log.info("message(s) %s wait for a place: %s", ids, wait)
                    return
                except Exception as exc:
                    detail = str(exc) if isinstance(exc, DeliveryError) else f"{type(exc).__name__}: {exc}"
                    log.error("message(s) %s failed: %s", ids, detail, exc_info=not isinstance(exc, DeliveryError))
                    with db.transaction(conn):
                        for each in batch:
                            conn.execute("UPDATE messages SET state = 'failed', detail = ? WHERE id = ?", (detail, each["id"]))
                            patterns.record(
                                conn, "message.failed", each["author"], each["item_id"], each["step_id"], None,
                                {"message_id": each["id"], "detail": detail},
                            )
                    return
                with db.transaction(conn):
                    for each in batch:
                        conn.execute(
                            "UPDATE messages SET state = 'sent', via = ?, detail = ?, sent_at = ?,"
                            " session_id = COALESCE(session_id, ?) WHERE id = ?",
                            (sent.via, sent.detail + _together(ids, each["id"]), db.now_iso(), sent.session_id, each["id"]),
                        )
                        patterns.record(
                            conn, "message.sent", each["author"], each["item_id"], each["step_id"], None,
                            {"message_id": each["id"], "via": sent.via},
                        )
                        if each["item_id"] is not None:
                            _after_send(conn, each, sent.via == "new", sent.session_id if sent.via == "new" else None)
        finally:
            conn.close()

    def _deliver(self, message: dict, text: str, restart: bool, waited: bool) -> Sent:
        if message["fork_of"] is not None and message["session_id"] is None:
            return self._fork(message["id"], message["fork_of"], message["new_name"], text)
        if message["session_id"] is None:
            provider = message.get("provider") or self.default_provider
            if provider not in self.enabled_providers:
                raise DeliveryError(f"{provider} is not enabled in setup.toml")
            if provider == "codex":
                if self.codex is None:
                    raise DeliveryError("Codex is not enabled in setup.toml")
                options = message["new_options"] or {}
                offered = config.SESSION_OPTIONS["codex"]
                try:
                    sid = self.codex.start_for_message(
                        message["id"], Path(message["new_cwd"]), message["new_name"], text,
                        options.get("model") or offered["models"][0][0], options.get("effort") or offered["effort"],
                        options.get("speed") or "standard",
                    )
                except CodexError as exc:
                    if exc.session_id is not None:
                        conn = db.connect(self.db_path)
                        try:
                            with db.transaction(conn):
                                conn.execute("UPDATE messages SET session_id = ? WHERE id = ?", (exc.session_id, message["id"]))
                                if message["item_id"] is not None:
                                    _after_send(conn, message, True, exc.session_id)
                        finally:
                            conn.close()
                    raise DeliveryError(str(exc)) from exc
                return Sent("new", sid, "started a Codex thread")
            return self._start_new(message["id"], Path(message["new_cwd"]), message["new_name"], text, message["new_options"] or {})
        conn = db.connect(self.db_path)
        try:
            is_codex = conn.execute("SELECT 1 FROM codex_threads WHERE session_id = ?", (message["session_id"],)).fetchone()
        finally:
            conn.close()
        if is_codex and self.codex is None:
            raise DeliveryError(
                "This message goes to a Codex thread, and Codex is not enabled in setup.toml. Cockpit cannot deliver"
                " it to Claude instead."
            )
        if self.codex is not None and self.codex.owns(message["session_id"]):
            session = self.codex.lookup(message["session_id"])
            if session is not None and session.working_now and not restart and not waited:
                raise Waiting("Codex is in the middle of a turn")
            if session is not None and session.waits_on_dialog and not restart:
                raise Waiting("answer the pending Codex request before sending another message")
            try:
                self.codex.send(message["session_id"], text, restart=restart or waited)
            except CodexError as exc:
                raise DeliveryError(str(exc)) from exc
            return Sent("resume", message["session_id"], "sent a prompt to Codex")
        if "claude" not in self.enabled_providers:
            raise DeliveryError("Claude is not enabled in setup.toml")
        session = self.lookup(message["session_id"])
        if session is None:
            raise DeliveryError(f"No Claude session {message['session_id']} on this machine")
        if parts.is_standing(session.session_id):
            return self._to_standing(session, text, parts.standing())
        resumed = "resumed the stopped session in the background"
        lowered = ""
        if restart and session.pid is not None:
            if session.turn_over:
                note = ""
            elif session.asks_user or turn_ended(session.session_id):
                note = "\n\n" + AFTER_TURN
            else:
                note = "\n\n" + CUT_SHORT
            session = self._stop(session)
            resumed = "stopped the session at once and resumed it with the message as its prompt"
            text += note
        elif message["as_prompt"] and session.pid is not None:
            if session.turn_over:
                session = self._stop(session)
                resumed = (
                    f"stopped the session once its turn was over and resumed it with {user_name()}'s message as its prompt"
                )
            elif session.asks_user:
                resumed = (
                    f"stopped the session, whose turn had ended on a question for {user_name()} while its background work"
                    " ran on, and resumed it with the message as its prompt"
                )
                session = self._stop(session)
                text += "\n\n" + AFTER_TURN
            elif background_only(session):
                resumed = (
                    "stopped the session, whose turn had ended while its background work ran on, and resumed it with"
                    " the message as its prompt"
                )
                session = self._stop(session)
                text += "\n\n" + AFTER_TURN
            elif waited:
                resumed = (
                    f"stopped the session after {user_name()}'s message waited {WAIT_LIMIT.seconds // 60} min for its"
                    " turn to end, and resumed it with the message as its prompt"
                )
                session = self._stop(session)
                text += "\n\n" + CUT_SHORT
            elif session.runs_subagent:
                raise Waiting(f"a subagent of session {session.id} runs in the background")
            else:
                raise Waiting(f"session {session.id} is in the middle of a turn")
        if session.pid is not None and self._retired_effort(session) and session.turn_over:
            # Its turn is over and nothing runs in it, so the stop cuts nothing off; the message then goes over the
            # socket as it would have.
            session = self._lower_effort(self._stop(session))
            lowered = LOWERED
        if session.pid is not None:
            try:
                write_to_socket(self.sockets_dir / f"{session.pid}.sock", text)
            except SocketUnreachable as exc:
                raise DeliveryError(f"Could not reach the live session's socket (pid {session.pid}): {exc}") from exc
            except OSError as exc:
                raise uncertain(f"writing to the live session (pid {session.pid}) failed: {exc}") from exc
            return Sent("socket", session.session_id, f"written to the live session (pid {session.pid}){lowered}")
        # A removal may have deleted the worktree this session works in: its own, or one it shared.
        conn = db.connect(self.db_path)
        try:
            removals.restore(conn, session.session_id, session.cwd)
        except RuntimeError as exc:
            raise DeliveryError(f"The session's worktree was removed with a finished session, and {exc}") from exc
        finally:
            conn.close()
        if not session.cwd or not Path(session.cwd).is_dir():
            raise DeliveryError(f"The session's directory {session.cwd!r} no longer exists, so it cannot be resumed")
        if self._retired_effort(session):
            session = self._stop(self._lower_effort(session))
            lowered = LOWERED
        account = config.account(session.account)
        self.trust(account, Path(session.cwd))
        # No other flags for a listed session: it keeps the options it started with, and a resume that passes any
        # makes Claude start a copy instead of continuing it. A session Cockpit removed has no job file left, so its
        # resume passes the options it had and continues under its own id (_resume).
        output = _run_claude(self.run, account, self._resume(session, text), Path(session.cwd), "claude --bg --resume")
        _refuse_copy(output, session, "this message")
        if not self._wait_for_process(session.session_id):
            raise uncertain(
                f"Claude accepted the resume, but the session's process did not show within {RESUME_SETTLE_SECONDS} s"
            )
        return Sent("resume", session.session_id, resumed + lowered)

    def _to_standing(self, session: Session, text: str, standing: parts.Standing) -> Sent:
        """Write every message over the standing session's socket at once; its supervisor brings it back if down."""
        if session.pid is None:
            raise Waiting(standing.not_running.format(name=session.name or standing.unnamed))
        try:
            write_to_socket(self.sockets_dir / f"{session.pid}.sock", text)
        except SocketUnreachable as exc:
            raise Waiting(standing.socket_unreachable.format(error=exc)) from exc
        except OSError as exc:
            raise uncertain(f"writing to the live session (pid {session.pid}) failed: {exc}") from exc
        return Sent("socket", session.session_id, standing.written.format(pid=session.pid))

    def _retired_effort(self, session: Session) -> bool:
        """Whether the session runs at RETIRED_EFFORT without the user having picked it when it started (SPEC
        SESSION-22): a picked effort stays."""
        if session.effort != RETIRED_EFFORT:
            return False
        conn = db.connect(self.db_path)
        try:
            return chosen_effort(conn, session.session_id) != RETIRED_EFFORT
        finally:
            conn.close()

    def _lower_effort(self, session: Session) -> Session:
        """Resume the stopped session with the command `/effort NEW_SESSION_EFFORT` and return it running at that
        effort. Claude Code runs the command without calling the model and saves the effort in the session's
        options, so every later turn and resume keeps it. The session then shows as working, with no turn under
        way, until its next message."""
        command = f"/effort {NEW_SESSION_EFFORT}"
        account = config.account(session.account)
        self.trust(account, Path(session.cwd))
        output = _run_claude(
            self.run, account, self._resume(session, command), Path(session.cwd), f"claude --bg --resume with {command}"
        )
        _refuse_copy(output, session, command)
        deadline = time.monotonic() + RESUME_SETTLE_SECONDS
        while time.monotonic() < deadline:
            resumed = self.lookup(session.session_id)
            if resumed is not None and resumed.pid is not None and resumed.effort == NEW_SESSION_EFFORT:
                log.info("moved session %s from %s to %s effort", session.id, session.effort, NEW_SESSION_EFFORT)
                return resumed
            time.sleep(RESUME_POLL_SECONDS)
        raise DeliveryError(
            f"Cockpit resumed the session with {command} to move it from {session.effort} to {NEW_SESSION_EFFORT}"
            f" effort. After {RESUME_SETTLE_SECONDS} s its process still did not show the new effort. The message"
            " was not sent."
        )

    def _resume(self, session: Session, prompt: str) -> list[str]:
        """The command that resumes the stopped `session` with `prompt`. One Cockpit removed from `claude agents` gets
        the options its job file held (removals.py); a listed one keeps its own."""
        if not session.removed_at:
            return ["claude", "--bg", "--resume", session.session_id, prompt]
        conn = db.connect(self.db_path)
        try:
            flags = removals.flags(conn, session.session_id)
        finally:
            conn.close()
        return ["claude", "--bg", "--resume", session.session_id, *flags, prompt]

    def _stop(self, session: Session) -> Session:
        """Stop the session's process and return it stopped. Claude keeps its conversation, so the resume
        that follows continues it."""
        if not session.cwd or not Path(session.cwd).is_dir():
            raise DeliveryError(
                f"The session's directory {session.cwd!r} no longer exists, so Cockpit could not resume the session"
                " there. It left the session running."
            )
        return self._halt(session, Path(session.cwd))

    def _halt(self, session: Session, cwd: Path) -> Session:
        """Run `claude stop` in `cwd` and wait until the session's process is gone; returns it stopped."""
        if parts.is_standing(session.session_id):
            raise DeliveryError(parts.standing().halt_refusal)
        if session.provider == "codex" and self.codex is not None:
            try:
                self.codex.stop(session.session_id)
            except CodexError as exc:
                raise DeliveryError(str(exc)) from exc
            # A thread Cockpit started stays in its registry, so this finds it; the fallback keeps a Session.
            return self.codex.lookup(session.session_id) or session
        _run_claude(self.run, config.account(session.account), ["claude", "stop", session.id], cwd, "claude stop")
        deadline = time.monotonic() + STOP_SETTLE_SECONDS
        while time.monotonic() < deadline:
            stopped = self.lookup(session.session_id)
            if stopped is not None and stopped.pid is None:
                time.sleep(STOP_GRACE_SECONDS)
                return stopped
            time.sleep(RESUME_POLL_SECONDS)
        raise DeliveryError(
            f"claude stop finished, but the session's process (pid {session.pid}) was still running after"
            f" {STOP_SETTLE_SECONDS} s. Cockpit did not resume the session."
        )

    def _wait_for_process(self, session_id: str) -> bool:
        deadline = time.monotonic() + RESUME_SETTLE_SECONDS
        while time.monotonic() < deadline:
            try:
                session = self.lookup(session_id)
            except LookupFailed as exc:
                log.warning("looking up session %s after a resume failed: %s", session_id, exc)
                session = None
            except Exception:
                log.exception("looking up session %s after a resume failed", session_id)
                session = None
            if session is not None and session.pid is not None:
                return True
            time.sleep(RESUME_POLL_SECONDS)
        return False

    def _fork(self, message_id: int, parent_id: str, name: str, text: str) -> Sent:
        """Start a writable background session forked from session `parent_id` (SPEC MSG-11): `claude --bg --resume
        --fork-session` with `text` as its first prompt, in the parent's folder, on its account, model and effort.
        The parent runs on untouched. The fork's UUID is looked up in that account's list, as for a new session:
        `claude --bg` names its own and ignores `--session-id`. Message `message_id` records the model, effort and
        account the fork starts on as its `new_options`, so the wrong-model warning and `chosen_effort` count the
        fork as started on them, also for a parent Cockpit did not start."""
        parent = self.lookup(parent_id)
        if parent is None:
            raise DeliveryError(f"No Claude session {parent_id} on this machine to fork")
        if not parent.cwd or not Path(parent.cwd).is_dir():
            raise DeliveryError(f"The session's directory {parent.cwd!r} no longer exists, so Cockpit cannot fork it there")
        account = config.account(accounts.current_name(parent.account))
        if accounts.signed_out(account, datetime.now(UTC)):
            raise DeliveryError(f"The session runs on the {account.name} Claude account, which is signed out; sign it in first")
        args = ["claude", "--bg", "--resume", parent.session_id, "--fork-session", "--name", name]
        model = last_model(parent.session_id)
        args += [*(["--model", model] if model else []), *(["--effort", parent.effort] if parent.effort else [])]
        conn = db.connect(self.db_path)
        try:
            conn.execute("UPDATE messages SET new_options = ? WHERE id = ?",
                         (json.dumps({"model": model or None, "effort": parent.effort or None, "account": account.name}), message_id))
        finally:
            conn.close()
        self.trust(account, Path(parent.cwd))
        output = _run_claude(self.run, account, [*args, text], Path(parent.cwd), "claude --bg --resume --fork-session")
        log.info("forked session %s on the %s account", parent.session_id, account.name)
        return self._backgrounded(
            account, output, lambda short_id: f"forked session {parent.id} as {short_id} in {parent.cwd} on the {account.name} account"
        )

    def _start_new(self, message_id: int, cwd: Path, name: str, text: str, options: dict[str, str | None]) -> Sent:
        """Start a background session with the model, effort and account the user picked (`options`, SPEC
        SESSION-22), each left out for the default: NEW_SESSION_MODEL, NEW_SESSION_EFFORT and the account the picker
        names. The session stays on that account. Its UUID is looked up in that account's list. Message `message_id`
        records the model the session starts on, so the wrong-model warning judges the session by it also after the
        default changes (SPEC SESSION-20)."""
        account_name = options.get("account")
        if account_name:
            account = config.account(account_name)
            if accounts.retired(account):
                raise DeliveryError(f"the {account.name} Claude account you picked is retired; pick another")
            if not accounts.signed_in(account) or accounts.signed_out(account, datetime.now(UTC)):
                raise DeliveryError(f"the {account.name} Claude account you picked is signed out; sign it in or pick another")
            why = "you picked it"
        else:
            account, why = self.pick()
        model = options.get("model") or NEW_SESSION_MODEL
        effort = options.get("effort") or NEW_SESSION_EFFORT
        args = ["claude", "--bg", "--model", model, "--effort", effort, "--name", name, text]
        conn = db.connect(self.db_path)
        try:
            conn.execute("UPDATE messages SET new_options = ? WHERE id = ?",
                         (json.dumps({"effort": None, "account": None, **options, "model": model}), message_id))
        finally:
            conn.close()
        self.trust(account, cwd)
        output = _run_claude(self.run, account, args, cwd, "claude --bg")
        log.info("started a session on the %s account (%s, %s effort): %s", account.name, model, effort, why)
        return self._backgrounded(
            account, output,
            lambda short_id: (f"started background session {short_id} in {cwd} on the {account.name} account ({why}),"
                              f" {model} at {effort} effort"),
        )

    def _backgrounded(self, account: config.Account, output: str, started: Callable[[str], str]) -> Sent:
        """The session that `claude --bg` printed it started (`output`), found by its short id in `account`'s list;
        `started` says, given the short id, what started it. One not found in time is still sent, but not linked to
        its item."""
        match = _BACKGROUNDED.search(output)
        if match is None:
            raise uncertain(f"claude --bg exited 0 but named no session: {output.strip()[-300:]}")
        short_id = match.group(1)
        what = started(short_id)
        problem = f"it is not in the {account.name} account's `claude agents`"
        deadline = time.monotonic() + NEW_SESSION_LOOKUP_SECONDS
        while True:
            try:
                uuid = self.short_id_to_uuid(account, short_id)
            except Exception as exc:
                log.exception("looking up the UUID of new session %s failed", short_id)
                problem = f"the lookup failed: {exc}"
                uuid = None
            if uuid is not None:
                return Sent("new", uuid, what)
            if time.monotonic() >= deadline:
                break
            time.sleep(NEW_SESSION_LOOKUP_PAUSE_SECONDS)
        log.error("new session %s started but its UUID was not found: %s", short_id, problem)
        return Sent(
            "new",
            None,
            f"{what}. Its UUID was not found within {NEW_SESSION_LOOKUP_SECONDS} s ({problem}), so the session"
            " is not linked to the item.",
        )


def _new_item_session(conn: Any, item_id: int) -> list[str]:
    """What a new session for an item needs besides the user's words: which item it is, its links and images,
    and that it reaches the user through steps on that item."""
    item = db.row(conn.execute("SELECT key, title, links, fire_and_forget, project FROM items WHERE id = ?", (item_id,)).fetchone())
    images = [row["id"] for row in conn.execute("SELECT id FROM item_images WHERE item_id = ? ORDER BY id", (item_id,))]
    lines = ["", "---", f"Cockpit context: you are the session for item {item_id}, {item['title']} ({item['key']})."]  # ty: ignore[not-subscriptable]
    if item["links"]:  # ty: ignore[not-subscriptable]
        lines += ["", "Links: " + ", ".join(f"[{link['label']}]({link['url']})" for link in item["links"])]  # ty: ignore[not-subscriptable]
    if images:
        lines += ["", f"Images on this item: {', '.join(map(str, images))}. Read one with `cockpit image get ID --out FILE`."]
    lines += [
        "",
        (
            f"When you need {user_name()}, put a step on item {item_id} with `cockpit step put`. A step is a card"
            f" that asks {user_name()} to act; the sweeper ranks it in their queue."
        ),
    ]
    if not item["project"].strip():  # ty: ignore[not-subscriptable]
        # Every task is in a project (SPEC CONTROL-26): its session decides which, or that it needs a new one.
        lines += [
            "",
            (
                "Put this item in a project first: `cockpit project list` shows the projects, a few broad areas and"
                ' big bodies of work. Set the one it belongs to with `cockpit item put` and "project". Only work that'
                " starts a new broad area, or a big body of work that will span many tasks, gets a new project,"
                " created first with `cockpit project put`."
            ),
        ]
    if item["fire_and_forget"]:  # ty: ignore[not-subscriptable]
        lines += ["", *_fire_and_forget(item_id)]
    return lines


def _fire_and_forget(item_id: int) -> list[str]:
    """What a fire-and-forget to-do's session (SPEC CONTROL-24) does: the work to the end on its own, a step only for
    what blocks it, and the close itself."""
    user = user_name()
    return [
        (
            f"{user} added this as fire and forget: do the work to the end on your own. Make the choices it needs"
            " yourself, the simplest that fits, and test the result where it runs."
        ),
        (
            f"Put a step only when the work cannot go on without {user}: an open question only {user} can answer, an"
            " unforeseen issue that needs their decision, or an action only they may take. Put no early looks, no"
            " read steps and no finish step."
        ),
        (
            'When the work is done, close the item yourself: `cockpit item put` with "stage": "done" and a one-line'
            f' "note" saying what was done and where it is (key todo-{item_id}). {user} sees only that line, on their'
            " next step. Close every other open step of yours on it first."
        ),
    ]


def _forked(message: dict) -> list[str]:
    """What a fork (SPEC MSG-11) needs to know besides its item: it is not the session whose conversation it carries,
    which goes on with its own work, and it shares that session's folder."""
    return [
        "",
        (
            f"You are a fork: a new session started just now from the conversation of session {message['fork_of']}."
            f" {user_name()} started this new line of work from it with the message above. That session goes on with"
            " its own work, items and steps; leave them to it. Put your steps on your own item, named above, and give"
            " it a title of your own with `cockpit item put`."
        ),
        (
            f"You work in the same folder as that session, {message['new_cwd']}. Before you change files there, make a"
            " worktree of your own, so the two sessions' changes stay apart."
        ),
    ]


def _after_send(conn, message: dict, started: bool, new_session_id: str | None) -> None:
    """The user writing about an item means they worked on it. A message that `started` a new session uses up the
    item's picks from Add (SPEC SESSION-22), also when its UUID was not found; the session joins the item first in
    line once its UUID is known (`new_session_id`)."""
    item = db.row(conn.execute("SELECT sessions FROM items WHERE id = ?", (message["item_id"],)).fetchone())
    if item is None:
        return
    now = db.now_iso()
    if message["author"] == "user":
        conn.execute("UPDATE items SET touched_at = ? WHERE id = ?", (now, message["item_id"]))
    if started:
        conn.execute("UPDATE items SET start_options = NULL WHERE id = ?", (message["item_id"],))
    if new_session_id and new_session_id not in item["sessions"]:
        conn.execute(
            "UPDATE items SET sessions = ?, updated_at = ?, updated_by = ? WHERE id = ?",
            (json.dumps([new_session_id, *item["sessions"]]), now, message["author"], message["item_id"]),
        )


def _older_waits(conn, message: dict) -> bool:
    """An older prompt to the same session is still queued, after `_claim_waiting` took the user's ones it could: it
    goes alone or waits for Take back, and its own worker may not have run yet (another send held the session's lock,
    and the newer message's worker took it first). Later prompts go after it. An Ask's question and answer is no such
    prompt: it waits for the turn to end however long it runs, goes with the user's next message, and takes its place
    by when it was asked (`_in_order`)."""
    return (
        conn.execute(
            "SELECT 1 FROM messages WHERE session_id = ? AND id < ? AND state = 'queued' AND as_prompt = 1"
            " AND id NOT IN (SELECT sent_message_id FROM side_chats WHERE sent_message_id IS NOT NULL)",
            (message["session_id"], message["id"]),
        ).fetchone()
        is not None
    )


def _ask_answering(conn, session_id: str | None) -> bool:
    """A copy of the session still answers an Ask (SPEC MSG-11): the user's messages to the session wait for the
    answer, so it reaches the session with them."""
    return session_id is not None and conn.execute(
        "SELECT 1 FROM side_chats WHERE session_id = ? AND state = 'asking'", (session_id,)
    ).fetchone() is not None


def _holds_asks(session: Session | None) -> bool:
    """An Ask's question and answer alone do not cut the session's turn short, background work included (SPEC
    MSG-11). They go once its turn is over, also when it ended on a question for the user, whose step stays open
    (`questions._only_asks_since`), or with the user's next message."""
    return session is not None and session.alive and not session.turn_over


def _in_order(conn, batch: list[dict]) -> list[dict]:
    """Messages that go in one prompt, oldest first, an Ask's question and answer placed where it was asked, right
    after the newest message then (`after_message_id`): a reply the user sent while the copy answered comes after the
    question it builds on."""
    asks = {
        row["sent_message_id"]: (row["after_message_id"], 1, row["id"]) for row in conn.execute(
            f"SELECT id, sent_message_id, after_message_id FROM side_chats WHERE sent_message_id IN ({','.join('?' * len(batch))})",
            [each["id"] for each in batch],
        )
    }
    return sorted(batch, key=lambda each: asks.get(each["id"], (each["id"], 0, 0)))


def _claim_waiting(conn, message: dict) -> list[dict]:
    """The user's other queued messages to the same session, claimed to go in one prompt with this one (SPEC MSG-7),
    also one whose own worker has not run yet: that worker then finds it claimed. Cockpit's own and the agents' prompts
    go alone, and so does a hand-off held for Take back: the claim stops at the first of them, so no message overtakes
    it (`_older_waits` then holds this one if it is later)."""
    if message["author"] != "user" or not message["as_prompt"] or message["session_id"] is None:
        return []
    claimed: list[dict] = []
    # One transaction: a failure part way claims none of them.
    with db.transaction(conn):
        rows = [
            dict(row) for row in conn.execute(
                "SELECT id, author, take_back_until FROM messages WHERE session_id = ? AND id != ? AND as_prompt = 1"
                " AND state = 'queued'",
                (message["session_id"], message["id"]),
            )
        ]
        # In the prompt's order, so an Ask's answer that came in later but was asked earlier goes too.
        for row in _in_order(conn, rows) if rows else []:
            if row["author"] != "user" or row["take_back_until"] is not None:
                break
            if conn.execute("UPDATE messages SET state = 'sending' WHERE id = ? AND state = 'queued'", (row["id"],)).rowcount:
                # Read once claimed, so a follow-up (MSG-10) joined before the claim goes with it.
                waiting = db.row(conn.execute("SELECT * FROM messages WHERE id = ?", (row["id"],)).fetchone())
                if waiting is not None:
                    claimed.append(waiting)
    return claimed


def compose_batch(conn, batch: list[dict]) -> str:
    """One prompt for messages that waited for the same turn: each with its sender line and step, oldest first."""
    texts = [compose(conn, message) for message in batch]
    if len(texts) > 1:
        texts.append(
            f"These {len(texts)} messages from {user_name()} waited for your turn to end. They come together, oldest"
            " first. Act on each of them."
        )
    return COCKPIT_BATCH_SEPARATOR.join(texts)


def _together(ids: list[int], message_id: int) -> str:
    others = [str(each) for each in ids if each != message_id]
    if not others:
        return ""
    named = others[0] if len(others) == 1 else ", ".join(others[:-1]) + " and " + others[-1]
    return f"; message{'s' if len(others) > 1 else ''} {named} went in the same prompt"


def waited_out(conn, message: dict, now: datetime) -> bool:
    """The message waited WAIT_LIMIT for its session's turn to end (SPEC MSG-7), counted from when it came to
    the front: the later of when it started waiting and when the message of the user's before it reached the
    session, whose turn on that one is left to run. An Ask's question and answer (SPEC MSG-11) never wait out:
    they wait for the turn to end however long it runs."""
    if message["waits_since"] is None or from_ask(conn, message["id"]):
        return False
    before = conn.execute(
        "SELECT MAX(sent_at) FROM messages WHERE session_id = ? AND id < ? AND as_prompt = 1 AND state = 'sent'",
        (message["session_id"], message["id"]),
    ).fetchone()[0]
    front = max(message["waits_since"], before or "")
    return now - datetime.fromisoformat(front) >= WAIT_LIMIT


def background_only(session: Session) -> bool:
    """A live session whose turn is over while background work it started runs on (SPEC MSG-7): a command such
    as a CI watch, or a scheduled prompt. Stopping it ends that work, and waiting gains nothing, since the work
    would end at the stop after WAIT_LIMIT all the same. A running subagent would lose its work, so a session
    with one waits."""
    return session.provider == "claude" and session.alive and not session.runs_subagent and turn_ended(session.session_id)


def waiting_due(conn, listed: list[Session], now: datetime) -> list[int]:
    """The waiting messages to send now (SPEC MSG-3, MSG-7): per session the oldest prompt, which takes the user's
    others along, else the oldest agent message. They go once the session's turn is over (with or without background
    work running on, but no subagent), its turn ended on a question for the user, it stopped, it left the listing
    (the delivery then fails and says so), or
    one of its messages waited WAIT_LIMIT (an Ask's question and answer waits behind none, `waited_out`). None goes
    while a copy of the session answers an Ask, and an Ask's question and answer alone stop no session
    (`_holds_asks`, SPEC MSG-11)."""
    sessions = {session.session_id: session for session in listed}
    waiting: dict[str, list[dict]] = {}
    for row in conn.execute(
        "SELECT id, session_id, waits_since FROM messages WHERE state = 'queued' AND waits_since IS NOT NULL"
        " ORDER BY as_prompt DESC, id"
    ):
        waiting.setdefault(row["session_id"], []).append(dict(row))
    due: list[int] = []
    for session_id, rows in waiting.items():
        session = sessions.get(session_id)
        if _ask_answering(conn, session_id):
            continue
        asks_only = all(from_ask(conn, row["id"]) for row in rows)
        if asks_only and _holds_asks(session):
            continue
        if (
            session is None
            or not session.alive
            or session.turn_over
            or session.asks_user
            or background_only(session)
            or any(waited_out(conn, row, now) for row in rows)
        ):
            # While the turn runs, an Ask's question and answer would not go alone: the first other message goes.
            first = next(row for row in rows if not (_holds_asks(session) and from_ask(conn, row["id"])))
            due.append(first["id"])
    return due


def starts_work(message: dict, session: Session | None) -> bool:
    """The delivery of `message` puts one more session to work (SPEC SESSION-23): it starts a session, or reaches
    one (`session`, None for a new one) that does not work now. The sweeper's session takes no place, as
    `store.working_sessions` counts none for it. A session that is gone starts none: the delivery fails."""
    if message["session_id"] is None:
        return message["new_name"] not in SWEEP_SESSION_NAMES
    return session is not None and not session.working_now and session.name not in SWEEP_SESSION_NAMES


def may_wait(conn, message: dict) -> bool:
    """Whether the message waits for a place while WORKING_LIMIT sessions work (SPEC SESSION-23). The work agents and
    Cockpit start by themselves waits: an agent's message, a wake-up, a scheduled run, whoever set the schedule. What
    the user does goes at once: a message, an answer or a hand-off, the prompt Cockpit sends on one, and the result
    of a command they ran or a permission they answered. So does every message to a standing session."""
    if message["session_id"] is not None and parts.is_standing(message["session_id"]):
        return False
    if conn.execute("SELECT 1 FROM schedules WHERE last_message_id = ?", (message["id"],)).fetchone() is not None:
        return True
    if message["author"] == "user" or message["as_prompt"] or message["hands_off"]:
        return False
    return conn.execute(
        "SELECT 1 FROM command_runs WHERE result_message_id = ?1"
        " UNION ALL SELECT 1 FROM permission_requests WHERE message_id = ?1",
        (message["id"],),
    ).fetchone() is None


def held_ahead(conn, message: dict) -> int:
    """How many messages wait for a place ahead of `message`: all of them for one that has not waited yet, and the
    ones that wait longer for one that has. A free place is theirs first."""
    if message["capacity_since"] is None:
        return conn.execute("SELECT COUNT(*) FROM messages WHERE state = 'queued' AND capacity_since IS NOT NULL").fetchone()[0]
    return conn.execute(
        "SELECT COUNT(*) FROM messages WHERE state = 'queued' AND capacity_since IS NOT NULL"
        " AND (capacity_since < ?1 OR (capacity_since = ?1 AND id < ?2))",
        (message["capacity_since"], message["id"]),
    ).fetchone()[0]


def take_back_due(conn, now: datetime) -> list[int]:
    """Hand-offs whose wait for Take back reached its limit without the page releasing them (SPEC MSG-8): the
    page closed while its notice with Take back showed. Released here, to send now, unless the user's hold
    started the wait again since this read (SPEC MSG-10). A released hand-off waits for its turn from then on
    (`store.release_message`)."""
    due = [
        (row["id"], row["take_back_until"])
        for row in conn.execute(
            "SELECT id, take_back_until FROM messages WHERE state = 'queued' AND take_back_until IS NOT NULL ORDER BY id"
        )
        if datetime.fromisoformat(row["take_back_until"]) <= now
    ]
    return [
        message_id for message_id, until in due
        if conn.execute(
            "UPDATE messages SET take_back_until = NULL, waits_since = COALESCE(waits_since, ?)"
            " WHERE id = ? AND state = 'queued' AND take_back_until = ?",
            (db.now_iso(), message_id, until),
        ).rowcount == 1
    ]


def archives_due(conn, listed: list[Session], now: datetime) -> list[int]:
    """The user's archives whose session Cockpit stops now (SPEC SESSION-4): the session's turn on Cockpit's
    message is over. Records the archives that end otherwise: the session stopped by itself, the message
    did not reach it, or it still works ARCHIVE_WAIT_LIMIT after the message reached it."""
    sessions = {session.session_id: session for session in listed}
    due: list[int] = []
    for archive in db.rows(
        conn.execute(
            "SELECT a.id, a.session_id, m.state, m.detail, m.sent_at FROM session_archives a"
            f" JOIN messages m ON m.id = a.message_id WHERE {ARCHIVE_PENDING} AND m.state IN ('sent', 'failed')"
        ).fetchall()
    ):
        session = sessions.get(archive["session_id"])
        if archive["state"] == "failed":
            end_archive(conn, archive["id"], f"Cockpit's message did not reach the session: {archive['detail']}")
        elif session is None or not session.alive:
            end_archive(conn, archive["id"], "")
        elif session.turn_over and datetime.fromisoformat(session.updated_at) > datetime.fromisoformat(archive["sent_at"]):
            due.append(archive["id"])
        elif now - datetime.fromisoformat(archive["sent_at"]) >= ARCHIVE_WAIT_LIMIT:
            end_archive(
                conn, archive["id"],
                f"the session still worked {ARCHIVE_WAIT_LIMIT.seconds // 3600} h after Cockpit's message reached it,"
                " so Cockpit left it running",
            )
    return due


def idle_stop_after(meminfo: str) -> timedelta:
    """How long a session waits idle before Cockpit stops it, from the text of /proc/meminfo."""
    kilobytes = {line.split(":")[0]: int(line.split()[1]) for line in meminfo.splitlines() if line.strip()}
    if kilobytes["MemAvailable"] < LOW_MEMORY_SHARE * kilobytes["MemTotal"]:
        return IDLE_STOP_AFTER_LOW_MEMORY
    return IDLE_STOP_AFTER


def idle_stop(conn, session: Session, now: datetime, after: timedelta) -> str | None:
    """Why Cockpit stops `session` now, idle `after` (idle_stop_after) with its turn over and nothing running or
    waiting in it; None while it is not due."""
    if not session.turn_over or not session.job_state or not session.updated_at:
        return None
    idle = now - datetime.fromisoformat(session.updated_at)
    if idle < after or stop_refusal(conn, session) is not None:
        return None
    if after < IDLE_STOP_AFTER:
        minutes = int(idle.total_seconds() // 60)
        return f"idle for {minutes} minutes with nothing running or waiting in it, while the host is low on memory"
    hours = int(idle.total_seconds() // 3600)
    return f"idle for {hours} hour{'s' if hours > 1 else ''} with nothing running or waiting in it"


def stops_due(conn, listed: list[Session], now: datetime, after: timedelta) -> list[str]:
    """The sessions Cockpit stops now as idle (idle_stop); Deliverer.close_idle judges each again before the stop."""
    return [session.session_id for session in listed if idle_stop(conn, session, now, after) is not None]


def mark_interrupted(conn) -> int:
    """At start: messages left mid-delivery by a restart are failed, never resent blindly. A message that
    waits for a session's turn, for Take back or for a place among the working sessions keeps waiting: nothing of it
    reached Claude. Each failure is recorded as any other, so it wakes the sweeper (SPEC MSG-5)."""
    with db.transaction(conn):
        interrupted = db.rows(conn.execute(
            "SELECT * FROM messages WHERE state = 'sending'"
            " OR (state = 'queued' AND waits_since IS NULL AND take_back_until IS NULL AND capacity_since IS NULL)"
        ).fetchall())
        for each in interrupted:
            detail = (
                "Delivery uncertain: Cockpit restarted while sending. Check the session before retrying."
                if each["state"] == "sending" else "Cockpit restarted before sending this message."
            )
            conn.execute("UPDATE messages SET state = 'failed', detail = ? WHERE id = ?", (detail, each["id"]))
            patterns.record(
                conn, "message.failed", each["author"], each["item_id"], each["step_id"], None,
                {"message_id": each["id"], "detail": detail},
            )
    return len(interrupted)
