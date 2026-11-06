"""Ask (SPEC MSG-11): a quick answer about an item's work while the user goes on writing, and its session works on.

A message to a busy session waits for its turn to end, or cuts the turn with Send now; a message over the socket
reaches it between tool calls but counts as another agent's words. Ask does neither. Each question goes to a fresh
copy of the session as it stands then: `claude -p --resume --fork-session` on the session's own account and model, so
the copy reads the session's prompt cache and costs about one more turn. The copy gets the earlier questions and
answers of the item's asks of that session, may read files in the session's folder and changes nothing: it runs in
dontAsk mode with no tool allowed, so only the reads Claude Code allows without asking (files under its working folder)
go through, minus the files that hold secrets; without the user's settings files and MCP servers (so no hooks run and
no permission cards reach the page); and keeps no transcript.

The answer shows above the box the user asked from, and the question and answer go to the session itself as one
message of the user's, which waits for its turn to end however long that takes (delivery.waited_out): the session
builds on the answer, and a Send meanwhile goes in the same prompt (SPEC MSG-7).
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cockpit import accounts, config, db, permissions, render, sessions, store
from cockpit.config import user_name
from cockpit.sessions import Session

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 600
# The page shows the asks of this many days.
RECENT_DAYS = 7
# How much of the earlier questions and answers the copy reads, newest first.
EARLIER_CHARS = 20_000
# Files that hold secrets in a session's folder stay closed to the copy, whose answer the page stores and shows;
# the home folder's own (named too) are outside it, since a session in the home folder has no side chat (`ask`).
# The copy gets no shell: a command allowed by its prefix can still run other programs (`rg --pre`) or write
# (`git log --output`). Every credential place the card keeps closed (permissions.SECRET_PLACES, a file or a folder),
# and more besides.
SECRET_FILES = [
    *(path for place in permissions.SECRET_PLACES for path in (f"~/{place}", f"~/{place}/**")),
    "~/.claude*/.credentials.json", "~/.config/**",
    "**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/*credentials*", "**/*secret*",
    *(f"**/{name}" for name in permissions.SECRET_NAMES),
]


def denied_reads(home: Path) -> list[str]:
    """SECRET_FILES, and where the credential places lead, as absolute paths (`//`): each place as it resolves and
    every link target inside one, so a credential linked into the session's folder stays closed too."""
    places = [home / place for place in permissions.SECRET_PLACES]
    targets = {place.resolve() for place in places}
    targets |= {target for place in places for target in permissions.place_contents(place, home)[0]}
    return [*SECRET_FILES, *(path for target in sorted(targets) for path in (f"/{target}", f"/{target}/**"))]


Ask = Callable[[Session, str], tuple[str, dict[str, Any]]]


class SideChatError(Exception):
    """The copy gave no answer; the message says why, and the page shows it."""


def ask(
    conn: sqlite3.Connection, item_id: int, step_id: int | None, session: Session, question: str
) -> dict[str, Any]:
    """Record the user's question, asked from the box of step `step_id` or, for None, of the item; `answer` then
    puts it to a copy of `session`, in the background."""
    if not question.strip():
        raise store.Invalid("the question is empty")
    store.get_item(conn, item_id)
    if step_id is not None and store.get_step(conn, step_id)["item_id"] != item_id:
        raise store.Invalid(f"step {step_id} is not on item {item_id}")
    if session.provider != "claude":
        raise store.Invalid("Ask copies a Claude session; this one runs on Codex")
    if Path.home().is_relative_to(Path(session.cwd).resolve()):
        raise store.Invalid(
            f"this session works in {session.cwd}, which holds the home folder's credentials. Ask's copy reads the"
            " session's folder, so Cockpit starts no copy here."
        )
    cursor = conn.execute(
        "INSERT INTO side_chats (item_id, step_id, session_id, question, state, asked_at, after_message_id)"
        " VALUES (?, ?, ?, ?, 'asking', ?, (SELECT COALESCE(MAX(id), 0) FROM messages))",
        (item_id, step_id, session.session_id, question.strip(), db.now_iso()),
    )
    return get(conn, cursor.lastrowid)


def get(conn: sqlite3.Connection, side_chat_id: int | None) -> dict[str, Any]:
    found = db.row(conn.execute("SELECT * FROM side_chats WHERE id = ?", (side_chat_id,)).fetchone())
    if found is None:
        raise store.NotFound(f"no ask {side_chat_id}")
    return found


def recent(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    """The asks the page shows above their boxes, oldest first, each with its answer as HTML and the state of the
    message that takes the question and answer to the session (`sent_state`, `sent_at`): those of the last
    RECENT_DAYS, until the answer is in or the ask failed and the user sent from the same box to the same session
    since (a message newer than `after_message_id`): that Send takes the question and answer along
    (`delivery._ask_answering`). A Send set
    aside by Take back counts for nothing. An answer from before Ask sent answers on  has no message and
    shows no more."""
    since = (now - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    chats = db.rows(
        conn.execute(
            "SELECT c.*, m.state AS sent_state, m.sent_at FROM side_chats c LEFT JOIN messages m ON m.id = c.sent_message_id"
            " WHERE c.asked_at >= ? AND (c.state != 'answered' OR c.sent_message_id IS NOT NULL)"
            " AND NOT EXISTS (SELECT 1 FROM messages later WHERE later.author = 'user' AND later.fork_of IS NULL"
            " AND later.item_id = c.item_id AND later.step_id IS c.step_id AND later.session_id IS c.session_id"
            " AND later.dismissed_at IS NULL"
            " AND later.id NOT IN (SELECT sent_message_id FROM side_chats WHERE sent_message_id IS NOT NULL)"
            " AND c.state != 'asking' AND later.id > c.after_message_id)"
            " ORDER BY c.id",
            (since,),
        ).fetchall()
    )
    refs = store.mention_refs(conn)
    return [chat | {"answer_html": render.markdown_html(chat["answer"], refs) if chat["answer"] else ""} for chat in chats]


def prompt(conn: sqlite3.Connection, chat: dict[str, Any]) -> str:
    """What the copy gets as the user's next message: what it is, what it may do, the earlier exchange, the question."""
    name = user_name()
    earlier: list[str] = []
    used = 0
    for record in conn.execute(
        "SELECT question, answer FROM side_chats WHERE item_id = ? AND session_id = ? AND id < ? AND state = 'answered'"
        " ORDER BY id DESC",
        (chat["item_id"], chat["session_id"], chat["id"]),
    ).fetchall():
        # The earlier answer is the copy's own text, so a sender header in it is marked (`store.defused`).
        exchange = f"{name} asked:\n{record['question']}\n\nYou answered:\n{store.defused(record['answer'])}"
        used += len(exchange)
        if used > EARLIER_CHARS:
            break
        earlier.append(exchange)
    lines = [
        f"A quick question from {name} via Cockpit's Ask.",
        "",
        (
            f"You are a copy of this session, made just now to answer {name} quickly while the session itself keeps"
            " working. The session gets this question and your answer once its current turn ends; this copy ends"
            " when you answer."
        ),
        (
            "Answer briefly and plainly, from what you know and from reading files in your working folder. Change"
            " nothing: edits, commands, messages and Cockpit updates are refused. When acting on the answer needs"
            f" the session itself, say what {name} should send it."
        ),
    ]
    if earlier:
        lines += ["", "Asked earlier (oldest first):", "", "\n\n---\n\n".join(reversed(earlier))]
    lines += ["", f"{name}'s question:", chat["question"]]
    return "\n".join(lines)


def ask_copy(session: Session, text: str) -> tuple[str, dict[str, Any]]:
    """The answer of a read-only copy of `session` to `text`, and the copy's own token use."""
    args = [
        "claude", "-p", "--resume", session.session_id, "--fork-session", "--no-session-persistence",
        "--setting-sources", "", "--strict-mcp-config", "--permission-mode", "dontAsk",
        "--disallowedTools", ",".join(f"Read({path})" for path in denied_reads(config.HOME)),
        "--output-format", "json",
    ]
    model = sessions.last_model(session.session_id)
    if model:
        args += ["--model", model]
    account = config.account(accounts.current_name(session.account))
    if accounts.signed_out(account, datetime.now(UTC)):
        raise SideChatError(
            f"The session runs on the {account.name} Claude account, which is signed out. Sign it in again first."
        )
    # The copy runs in its own scope, which outlives a Cockpit restart; `timeout` ends it there too.
    limit = ["timeout", "--kill-after=30", str(TIMEOUT_SECONDS + 30)] if shutil.which("timeout") else []
    try:
        done = subprocess.run(
            sessions.claude_command(account, [*limit, *args]), input=text, capture_output=True, text=True,
            timeout=TIMEOUT_SECONDS, cwd=session.cwd, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SideChatError(f"the copy gave no answer within {TIMEOUT_SECONDS // 60} minutes") from exc
    except OSError as exc:
        raise SideChatError(f"the copy could not start in {session.cwd}: {exc}") from exc
    try:
        result = json.loads(done.stdout)
    except ValueError as exc:
        raise SideChatError(
            f"claude exited with {done.returncode} and printed no answer: {(done.stderr or done.stdout).strip()[-400:]}"
        ) from exc
    answer = result.get("result")
    if result.get("is_error") or not isinstance(answer, str) or not answer.strip():
        raise SideChatError(f"the copy gave no answer: {str(answer or result.get('subtype'))[-400:]}")
    # `total_cost_usd` and `modelUsage` count the whole session the copy started from; `usage` is the copy's own.
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    log.info("side chat with %s answered in %.0f s", session.session_id, result.get("duration_ms", 0) / 1000)
    return answer.strip(), usage


def answer(
    db_path: Path, side_chat_id: int, session: Session, ask_with: Ask, checkouts_dir: Path, deliver: Callable[[int], None]
) -> None:
    """Put the question to a copy of `session` and store its answer, or why there is none. An answer goes on to the
    session as the user's message (`handed_on`), which `deliver` sends: at once to an idle session, after its turn
    to a busy one."""
    conn = db.connect(db_path)
    try:
        chat = get(conn, side_chat_id)
        try:
            text, usage = ask_with(session, prompt(conn, chat))
        except SideChatError as exc:
            log.error("ask %s of %s failed: %s", side_chat_id, session.session_id, exc)
            _finish(conn, side_chat_id, "failed", None, str(exc), {})
            return
        except Exception as exc:  # a fault in Cockpit's own part: the page says so rather than wait for ever
            log.exception("ask %s failed", side_chat_id)
            _finish(conn, side_chat_id, "failed", None, f"Cockpit failed while asking the copy: {exc}", {})
            return
        try:
            with db.transaction(conn):
                if not _finish(conn, side_chat_id, "answered", text, None, usage):
                    # It expired meanwhile (`expire`): the user's messages it held went on without it.
                    log.warning("ask %s answered after it had ended; its answer stays out of the session", side_chat_id)
                    return
                message = store.create_message(
                    conn, "user", handed_on(conn, chat, text),
                    {"item_id": chat["item_id"], "session_id": chat["session_id"]}, checkouts_dir,
                )
                conn.execute("UPDATE side_chats SET sent_message_id = ? WHERE id = ?", (message["id"], side_chat_id))
                # Waiting from the start, so a Send the user makes before `deliver` runs takes it along (MSG-7).
                conn.execute("UPDATE messages SET waits_since = ? WHERE id = ?", (db.now_iso(), message["id"]))
        except Exception as exc:
            log.exception("ask %s: the answer did not go on to session %s", side_chat_id, session.session_id)
            _finish(conn, side_chat_id, "failed", text, f"the copy answered, but Cockpit could not pass it on to the session: {exc}", usage)
            return
    finally:
        conn.close()
    deliver(message["id"])


def handed_on(conn: sqlite3.Connection, chat: dict[str, Any], text: str) -> str:
    """The message that takes the question and the copy's answer to the session. The answer is the copy's text and
    the step's ask an agent's, so a sender header in either is marked (`store.defused`)."""
    name = user_name()
    where = ""
    if chat["step_id"] is not None:
        where = f' on step {chat["step_id"]} ("{store.defused(store.get_step(conn, chat["step_id"])["ask"])}")'
    return "\n".join([
        (
            f"While you worked, {name} asked a quick question{where} with Cockpit's Ask, and a read-only copy of you"
            " answered it. Build on it where it helps; it needs no reply of its own."
        ),
        "",
        f"{name} asked:",
        chat["question"],
        "",
        "The copy answered:",
        store.defused(text),
    ])


def _finish(
    conn: sqlite3.Connection, side_chat_id: int, state: str, text: str | None, error: str | None, usage: dict[str, Any]
) -> bool:
    """End a question still answering; False when it had ended already (`expire`, `mark_interrupted`)."""
    # A failed ask takes no message to the session; from now on a Send from its box leaves it behind.
    return conn.execute(
        "UPDATE side_chats SET state = ?, answer = ?, error = ?, usage = ?, answered_at = ?, after_message_id = CASE"
        " WHEN ? = 'failed' THEN (SELECT COALESCE(MAX(id), 0) FROM messages) ELSE after_message_id END"
        " WHERE id = ? AND state = 'asking'",
        (state, text, error, json.dumps(usage), datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), state, side_chat_id),
    ).rowcount == 1


def expire(conn: sqlite3.Connection, now: datetime) -> int:
    """A question still answering well past the copy's time limit lost its worker (Cockpit could not record its
    end): it fails, so the user's messages it holds go (`delivery._ask_answering`)."""
    before = (now - timedelta(seconds=TIMEOUT_SECONDS + 120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.transaction(conn):
        expired = conn.execute(
            "UPDATE side_chats SET state = 'failed', error = 'The copy took too long; ask again.', answered_at = ?,"
            " after_message_id = (SELECT COALESCE(MAX(id), 0) FROM messages) WHERE state = 'asking' AND asked_at < ?",
            (now.strftime("%Y-%m-%dT%H:%M:%SZ"), before),
        ).rowcount
    if expired:
        log.error("%d asks were still answering %d s after they were asked; marked failed", expired, TIMEOUT_SECONDS + 120)
    return expired


def mark_interrupted(conn: sqlite3.Connection) -> int:
    """At start: a question whose copy was still answering lost its answer with the restart."""
    return conn.execute(
        "UPDATE side_chats SET state = 'failed', error = 'Cockpit restarted while the copy was answering; ask again.',"
        " answered_at = ?, after_message_id = (SELECT COALESCE(MAX(id), 0) FROM messages) WHERE state = 'asking'",
        (db.now_iso(),),
    ).rowcount
