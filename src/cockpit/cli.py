"""`cockpit`: the command agents use to read and change Cockpit.

It talks to the server on 127.0.0.1 with the token in ~/.config/cockpit/token.
Every write names the agent with --as; Cockpit records it on the rows it changes.
`cockpit spec` reads and edits docs/SPEC.md through the server, which commits each edit.
`cockpit permission` holds the two hooks that report tool calls to Cockpit (SPEC PERM-1), and lets
the session that made a call list, withdraw it or propose an auto-mode sentence for it (SPEC PERM-6, PERM-7).
`cockpit image` saves an image on an item to a file, and adds or removes one, e.g. a screenshot or a graph.
`cockpit page` shares an HTML page on an item, e.g. a report, and updates, saves or removes one (SPEC API-3).
`cockpit message` dismisses a failed message whose work was done another way, or a waiting one whose text reached
its session another way, or retries one of your own (SPEC MSG-5).
`cockpit deploy` installs the committed deploy_branch into the local service and restarts it (see `cockpit deploy --help`).
Exit codes: 0 done, 1 error, 3 `step put` left a closed step alone.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from string import Template
from typing import Any

from cockpit import attach, config, moves, parts, permissions, rank, removals, sessions, store
from cockpit.auth import read_or_create_secret
from cockpit.config import user_name

# Requests to Cockpit stay on loopback even when the shell configures an HTTP proxy.
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# How long `cockpit send` waits for the delivery outcome. A resume can take the
# command timeout (60 s) plus the wait for its process (180 s) plus a last
# `claude agents` listing (up to 30 s); a new session the command timeout plus the
# UUID lookup window (30 s) plus a last listing. A session moved off a retired effort
# is resumed twice and can take longer on a loaded host; the CLI then says the
# message is still sending, and the page shows how it ends.
SEND_WAIT_SECONDS = 300
SEND_POLL_SECONDS = 1.0
# `cockpit get` lists live sessions idle this long for the sweeper to judge (SPEC SESSION-13). Claude Code closes a
# background session after about an hour idle, but not one that watches an artifact it published or has a queued task,
# so the ones still running past the hour are those.
IDLE_LISTED_SECONDS = 60 * 60
# How often a waiting permission hook checks Cockpit and the transcript, and how much of auto mode's reason it
# sends. The tool input goes whole, since Allow lets the whole call run. The transcript tail covers the
# turn that made the call.
ASK_POLL_SECONDS = 2.0
REASON_MAX = 2000
TRANSCRIPT_TAIL_BYTES = 4 * 1024 * 1024
FAILED_HEAD = (
    "Failed messages.\n"
    "When a failure's work was done another way, such as by a session now working on the item, dismiss it:"
    " `cockpit message dismiss ID --reason TEXT --as NAME`.\n"
    "When the work still needs it, fix the cause first. Retry your own with `cockpit message retry ID --as NAME`;"
    " send another sender's again with `cockpit send`."
)
# `cockpit get` shows this much of a failed message's detail, on one line.
FAILED_DETAIL_CHARS = 300
# `cockpit get` lists stalled agent work (SPEC SWEEP-20) by priority, then the longest stalled first.
STALL_ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
GROUP_LABELS = {"you": "waiting for you", "busy": "busy", "waiting": "waiting", "done": "done"}
# `cockpit get` lists the waiting work unchanged this long for the sweeper's review (SPEC SWEEP-21), at most
# WAITING_REVIEW_MAX of it, the longest unchanged first.
WAITING_REVIEW_AFTER_HOURS = 24
WAITING_REVIEW_MAX = 10


class CockpitError(Exception):
    exit_code = 1


class StepSkipped(CockpitError):
    """The step was closed, so the put changed nothing."""

    exit_code = 3


ITEM_FIELDS = """\
JSON fields (left-out fields keep their value; lists are replaced whole):
  key         required: gh:example-org/app#10, linear:ENG-123, slack:<channel>/<ts>, session:<uuid>, note:<slug>.
              todo-<n> is an item $user added with Add. You may update it but not create one.
  title       required for a new item: what the item is about in 3 to 4 words, such as "Fix recurring reminders".
              Refused past 5 words or 40 characters.
  summary     one line: where it stands. On an item $user added, ${user}'s text stays, and the summary you
              put shows beside it as the session's latest status.
  doing       what you work on now, in a few words, such as "Add list sharing"; at most 60 characters.
              Put it when you start a new piece of work on the item. The page shows it when $user hovers
              the item in Busy.
  project     the project the task is in: the one from `cockpit project list` it fits. A put naming a project
              that does not exist is refused; projects are a few broad areas and big bodies of work, and only work
              that starts a new one gets a new project, created first with `cockpit project put`.
  stage       planning | building | review | dev | staging | prod | done. Only ${user}'s press sets done; see note.
  waiting_on  [{"kind": "agent" | "bot" | "person" | "release", "who": "<session uuid, bot, name or release>"}].
              A person is someone other than $user. What $user does is a step, and a put naming $user is refused.
              Any entry takes "what": what they do, in a word or two, such as "review" or "answer"; the page
              shows "review: Alex" when $user hovers the item in Waiting.
              An agent entry takes "until": "<ISO time>" when its work picks up again then (a timer, a handover).
              Until that time the work rests and does not count as stalled, also while the session is stopped.
              With until goes "why": why the work rests until then, in a few words (at most 60 characters),
              such as "check the reminder after the build"; $user reads it on the row's hover in Waiting. Add
              "by_user": true when $user chose the time, such as a reminder $user asked for: the item then
              sits under Snoozed by you rather than Agent resumes later.
              30 minutes after it the work counts as stalled, and the sweeper wakes its session unless the
              item is low priority.
              A bot, person or release entry needs "until": "<ISO time>", when Cockpit wakes the item's
              session to look again; an agent's put adding one without it is refused. Where Cockpit can check
              the wait itself, add "check": {"kind": "reached", "stage": "staging" | "prod",
              "pulls": ["<PR url>", ...]}: it holds once every PR listed reached that stage, and Cockpit
              checks it every 10 minutes and wakes the session as soon as it holds.
  priority    urgent | high | normal | low
  goal_id     a goal id from `cockpit get --json` .goals
  blocks      whose goal it blocks, in words
  sessions    [session uuid, ...], primary first
  links       [{"label": "...", "url": "https://..."}]
  pinged_at, touched_at   ISO times with an offset, such as 2026-01-15T10:00:00Z, not in the future
  archived    true | false. Only ${user}'s press sets true; see note.
  todo        true for a plain to-do: ${user}'s own task, with no PR, ticket or thread behind it
  recurring   true for work that repeats, such as a review every few days: the sidebar lists it under
              Background. Every run of a scheduled prompt is recurring already.
  note        one line: why the work is finished, such as "merged". It goes with "stage": "done" or
              "archived": true. Only $user closes a task, so such a put needs an open finish step, which
              you put first: a step with need complete, where $user presses Task done or Close. The put
              leaves the item open; without that step it is refused. One exception: "stage": "done" on
              an item whose PR has merged closes it with no finish step, when no other step is open on it.
              So does "stage": "done" with a note on a to-do $user added as fire and forget.
Once $user sets priority, goal_id, blocks or archived on the page, your value for that
field is ignored. The response lists the fields it left alone in "kept".
An item $user completed stays done: a put that moves it to another stage is refused (HTTP 409)."""

STEP_FIELDS = """\
JSON fields (left-out fields keep their value; a done or dismissed step stays closed):
  item_id or item_key   required: the item. An item $user completed or closed takes no new step
              (HTTP 409): put the result on the item $user asked on, or on a new item.
  need        required: a stable name for the ask, unique per item, such as merge, decide:fallback, review:<sha>
  ask         required for a new or withdrawn step. One line, at most 120 characters: the decision as a plain
              question under the item's title, such as "Include completed tasks in exports?"
  why         one line, at most 160 characters: why now
  body        Markdown, at most 80 words and 900 characters, aim for 40 to 60 words: what the user needs to act. Link URLs do not count, but the source
              with them stays under 20,000 characters.
              Commit hashes and session ids go in details, not in ask, why or body.
  details     [{"title": "...", "body": "<Markdown>"}], shown collapsed. At most 3, each body at most
              2,500 characters, counted like the body.
  draft       Markdown text $user posts under their own name. Only the draft opens with the agent
              attribution line; Cockpit refuses card text that opens with it.
  core_links  [{"label": "app#10", "url": "https://..."}, ...]: up to 3 links the step is about, such as the
              PR to merge or the page to read, each label at most 40 characters and each URL once. The card
              shows them as buttons above the answers, and the item's other links under Other beside them.
              [] clears them.
  reply_hint  one line, at most 80 characters: the kind of answer you expect, shown greyed in the empty reply
              box, such as "Say which lists to export" or "Say which of the two fixes to try". Not a draft
              of the answer.
  score       0-100
  reasons     ["chip label", ...], up to 60 characters each
  size        small | medium | large: how big ${user}'s part of the step is
  progress    0-100: the share of ${user}'s own time on the item that is already behind them.
              The agents' work does not count.
              The session that puts the step sets size and progress; the sweeper fills in what is missing.
              The page draws one small bar from them once both are set. Cockpit adjusts the bar for how
              optimistic past estimates were against ${user}'s timer, so give your own estimate.
  session_id  the session that gets ${user}'s answer, when not the item's primary session
  answers     up to 4 one-line replies, at most 80 characters each, such as ["Approve it", "Hold it"].
              $user sends one with one press. Offer them when $user very likely picks one. [] clears them.
              A reply that also completes the item is {"text": "I've sent it myself; close", "closes": true}.
              Its press closes every step of the item, moves it to stage done and sends the reply.
  command     {"run": "<shell command>", "cwd": "/absolute/folder", "explain": "<what it does>"}: a command
              only $user may run. explain (required, Markdown, at most 1200 characters) says in plain words
              what each part of the command does and whether it changes anything; the page shows it above
              the command. Run on the page runs it as $user in bash -lc, for at most 30 minutes, and sends
              its output to the step's session. null clears it.
  permission  {"list": "allow", "rule": "Bash(ls /tmp/tasks-export *)"} or {"list": "auto", "rule": "<sentence>"}.
              Allow on the page adds the rule to ${user}'s settings and tells the session. null clears it.
  closes      [{"item_id": 10, "why": "<what shows it is done>"}, ...]: other tasks you judge done, at most
              50, for $user to close with one press. The step lists them with your why, all ticked; $user
              unticks any to keep open, and the plum key completes the rest, each as its Task done would,
              from its finish step (Cockpit writes one from your why where none is open). Each task's
              session hears it; your session hears what closed. null clears it.
  early       true for an early look: an early result, or a question or request for feedback, that the
              session asks while it keeps working. It takes a place in ${user}'s queue only when no other
              step waits for one, and gives that place up when another step does. false makes it an
              ordinary step.
  expires_at  the time after which the step is of no use to $user, such as when its sign-in link stops
              working, as a time with its UTC offset ("2026-01-15T10:10:00Z"). Until then the step
              leads ${user}'s queue and its card counts the time down. When the time passes with the
              step still open, Cockpit tells the step's session once, so it renews the step (a put with
              a new expires_at), frees it of the time (null) or withdraws it. Give it only to a step
              that stops working at that time, not to work that is due by a date.
  changed     one line, at most 100 characters, when you rewrite a step $user looked at before: what
              changed, such as "The tests finished; the result is now at the top." The card shows it in
              a band when ${user} read the card 30 s or more since their last message or
              press on the item, until they read the new card 30 s or write or press again.
              Cockpit refuses the rewrite without it when the band would show.
Exits 3 when the (item, need) is closed: the put changed nothing. Use a new need.
A new step takes a place in ${user}'s queue (at most 4) when one is free. Otherwise it
waits as ready next, and the response's step.queued_at is null. The strongest ready-next
step by score takes the next free place."""


def call(method: str, path: str, body: Any, agent: str | None) -> Any:
    """A JSON request to the server (no body when `body` is None); returns the decoded response."""
    data = None if body is None else (json.dumps(body).encode(), "application/json")
    content, _ = fetch(method, path, data, agent)
    return json.loads(content)


def fetch(method: str, path: str, data: tuple[bytes, str] | None, agent: str | None) -> tuple[bytes, str]:
    """A request to the server with `data` as (body, Content-Type), or no body; returns the response's bytes
    and Content-Type."""
    headers = {"Authorization": f"Bearer {read_or_create_secret(config.TOKEN_PATH)}"}
    content = None
    if data is not None:
        content, headers["Content-Type"] = data
    if agent is not None:
        headers["X-Cockpit-As"] = agent
    request = urllib.request.Request(config.api_base() + path, data=content, headers=headers, method=method)
    try:
        with LOCAL_OPENER.open(request, timeout=60) as response:
            return response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("detail", "")
        except ValueError:
            detail = ""
        raise CockpitError(f"{method} {path}: HTTP {exc.code}: {detail}") from exc
    except OSError as exc:
        raise CockpitError(
            f"Cockpit is not reachable at {config.api_base()}: {exc}. Check whether cockpit.service is running."
        ) from exc


def _stdin_json() -> Any:
    try:
        return json.load(sys.stdin)
    except ValueError as exc:
        raise CockpitError(f"stdin is not valid JSON: {exc}") from exc


def _tip(item: dict[str, Any]) -> str:
    """The item's hover line as the page shows it (SPEC VIEW-27), its time in UTC."""
    at = item["tip_at"]
    return f"{item['tip']} {rank.parse_time(at):%d %b %H:%M} UTC" if at else item["tip"]


def _ago(iso: str | None, now: datetime) -> str:
    moment = rank.parse_time(iso)
    return rank.age_words((now - moment).total_seconds()) if moment else "never"


def lean(state: dict[str, Any]) -> dict[str, Any]:
    """The state without what only the page needs: rendered HTML, each step's copy of its item, session prompts."""

    def step(entry: dict[str, Any]) -> dict[str, Any]:
        kept = {key: value for key, value in entry.items() if key != "item" and not key.endswith("_html")}
        kept["details"] = [{"title": detail["title"], "body": detail["body"]} for detail in entry["details"]]
        # The command as put, so an agent can send it back unchanged (its explanation's HTML is the page's).
        if entry["command"]:
            kept["command"] = {key: value for key, value in entry["command"].items() if not key.endswith("_html")}
        return kept

    def session(entry: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in entry.items() if key not in ("intent", "last_message_html")}

    def request(entry: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in entry.items() if key != "tool_input"}

    return state | {
        "queue": [step(entry) for entry in state["queue"]],
        "ready_next": [step(entry) for entry in state["ready_next"]],
        "snoozed": [step(entry) for entry in state["snoozed"]],
        "with_agents": [step(entry) for entry in state["with_agents"]],
        "sessions": [session(entry) for entry in state["sessions"]],
        "waiting_sessions": [session(entry) for entry in state["waiting_sessions"]],
        "permission_requests": [request(entry) for entry in state["permission_requests"]],
        "moved_past": [request(entry) for entry in state["moved_past"]],
        "held_requests": [request(entry) for entry in state["held_requests"]],
        "settled_requests": [request(entry) for entry in state["settled_requests"]],
    }


def _request_words(request: dict[str, Any]) -> str:
    """An open permission request on one line, after its id."""
    what = "waiting for permission" if request["kind"] == "asked" else f"blocked by auto mode ×{request['count']}"
    return f"{what}: {request['summary'].splitlines()[0][:160]}"


def _bar_words(step: dict[str, Any]) -> str:
    """A step's size and progress (SPEC NEXT-35), so the sweeper sees which it has to fill in."""
    progress = "unset" if step["progress"] is None else f"{step['progress']}%"
    return f"size {step['size'] or 'unset'}, progress {progress}"


def _early_words(step: dict[str, Any]) -> str:
    return ", early look (takes a place only when no other step waits)" if step["early"] else ""


def _expiry_words(step: dict[str, Any], now: datetime) -> str:
    if not step["expires_at"]:
        return ""
    if rank.expiring(step, now):
        return f", useful until {step['expires_at']} (leads the queue until then)"
    return f", its time ran out at {step['expires_at']}"


def summary(state: dict[str, Any]) -> str:
    now = datetime.now(UTC)
    counts = state["counts"]
    lines = []
    oldest = state["oldest_waiting_minutes"]
    head = f"{counts['waiting_on_user']} steps waiting on {user_name()}"
    if oldest is not None:
        head += f" (oldest {rank.age_words(oldest * 60).removesuffix(' ago')})"
    head += (
        f" · {counts['sessions_waiting']} sessions at a dialog in their terminal"
        f" · {counts['agents_working']} sessions working · {counts['snoozed']} snoozed"
    )
    if counts["waiting_for_place"]:
        head += (
            f" · {counts['waiting_for_place']} messages wait for a place"
            f" (at most {counts['working_limit']} sessions work at once)"
        )
    lines.append(head)
    sweep = state["last_sweep"]
    if sweep is None:
        lines.append("Last sweep: never")
    elif not sweep["finished_at"]:
        status = "running" if state["sweep_running"] else "abandoned (stopped writing)"
        lines.append(f"Sweep {sweep['id']} {status}, started {_ago(sweep['started_at'], now)} by {sweep['by']}")
    else:
        outcome = f"ERROR {sweep['error']}" if sweep["error"] else sweep["summary"]
        lines.append(f"Last sweep: {_ago(sweep['finished_at'], now)} — {outcome}")
    if state["unread_feedback"]:
        lines.append(f"Unread feedback from {user_name()}: {state['unread_feedback']} (cockpit feedback)")
    for warning in state["warnings"]:
        lines.append(f"WARNING: {warning}")
    for entry in state["claude_sign_ins"]:
        lines.append(
            f"WARNING: {entry['text']} {user_name()} signs it in again with the button at the top of Cockpit's page,"
            f" or with {entry['command']} in a terminal."
        )
    for entry in state["codex_sign_ins"]:
        if entry["problem"] == "signed_out":
            lines.append(
                f"WARNING: Codex's ~/{entry['folder']} ({entry['held'] or entry['email']}) is signed out. {user_name()} signs it"
                " in from its warning in Cockpit's Inbox."
            )
        elif entry["problem"] == "other_account":
            lines.append(
                f"WARNING: Codex's ~/{entry['folder']} now holds {entry['email']}; it held {entry['held']}. {user_name()}"
                " decides on its warning in Cockpit's Inbox."
            )
    for problem in state["account_problems"]:
        lines.append(f"WARNING: {problem['text']}")
        lines += [f"  {line}" for line in problem["detail"].splitlines()]
    for wrong in state["wrong_models"]:
        now_on = f"It is back on {wrong['intended']}" if wrong["back"] else f"`/model {wrong['intended']}` moves it back"
        lines.append(
            f"WARNING: session {wrong['session_id']} ({wrong['name'][:60]}) ran on {wrong['model']} instead of"
            f" {wrong['intended']}. It made {wrong['calls']} call{'' if wrong['calls'] == 1 else 's'} there, the"
            f" last at {wrong['last']}. {now_on}."
        )
    failed = [message for message in state["messages"] if message["state"] == "failed" and not message["dismissed_at"]]
    if failed:
        lines.append(FAILED_HEAD)
        titles = {item["id"]: item["title"] for item in state["items"]}
        for message in sorted(failed, key=lambda message: message["id"]):
            item = (
                f"item {message['item_id']} ({titles.get(message['item_id'], 'no longer on the board')})"
                if message["item_id"] is not None else "no item"
            )
            to = f"session {message['session_id']}" if message["new_cwd"] is None else f"a new session in {message['new_cwd']}"
            at = message["created_at"][:16].replace("T", " ")
            lines.append(f"  message {message['id']} by {message['author']}, {item}, to {to}, {at} UTC:")
            lines.append(f"    {' '.join(message['detail'].split())[:FAILED_DETAIL_CHARS]}")
    if state["permission_requests"]:
        lines.append(
            f"Tool calls blocked or waiting for permission. {user_name()} answers these on the page.\n"
            "The session that made a call lists its own with `cockpit permission list --session UUID`. It takes"
            " one back once it no longer needs it, with `cockpit permission withdraw ID --session UUID --as NAME`."
        )
        for entry in state["permission_requests"]:
            lines.append(f"  request {entry['id']}, session {entry['session_id']}, {_request_words(entry)}")
            if entry["proposed_sentence"] is not None:
                lines.append(f"    the session proposes: {entry['proposed_sentence']}")
    untriaged = [item for item in state["items"] if item["untriaged"]]
    if untriaged:
        lines.append("")
        lines.append(
            f"Items {user_name()} added whose session did not start. Add normally starts one. Start one for each"
            f" with `cockpit send --item ID --new-in CHECKOUT`, with {user_name()}'s words as the message."
        )
        for item in sorted(untriaged, key=lambda item: item["id"]):
            images = f"; images {', '.join(str(image['id']) for image in item['images'])}" if item["images"] else ""
            lines.append(f"  item {item['id']} {item['key']}{images}:")
            lines.extend(f"    {line}" for line in item["summary"].splitlines())
    lines.append("")
    lines.append(f"Queue ({user_name()}'s order):")
    for position, step in enumerate(state["queue"], start=1):
        on_it = f" {user_name().upper()} IS ON IT (changes to it wait until {user_name()} pauses)" if step["user_on_it"] else ""
        lines += [
            f"  {position}. {step['ask']}{on_it}",
            f"     step {step['id']}, score {step['score']}, {_bar_words(step)}{_early_words(step)}{_expiry_words(step, now)}",
            f"     item {step['item_id']} {step['item']['key']}, need {step['need']}",
        ]
    if not state["queue"]:
        lines.append("  (empty)")
    if state["ready_next"]:
        lines.append("Ready next (waiting for a place, highest score first):")
        for step in state["ready_next"]:
            lines += [
                f"  - {step['ask']}",
                f"    step {step['id']}, score {step['score']}, {_bar_words(step)}{_early_words(step)}{_expiry_words(step, now)}",
                f"    item {step['item_id']} {step['item']['key']}, need {step['need']}",
            ]
    if state["snoozed"]:
        lines.append("Snoozed:")
        for step in state["snoozed"]:
            lines.append(f"  step {step['id']} until {step['snoozed_until']}: {step['ask']}")
    if state["with_agents"]:
        lines.append(f"With agents. {user_name()} handed these off; each stays with its agent until the agent closes or returns it:")
        for step in state["with_agents"]:
            handoff = step["handoff"]
            to = handoff["session_id"] or f"a new session in {handoff['new_cwd']}"
            lines += [
                f"  - {step['ask']}",
                f"    step {step['id']} to {to}, handed off {_ago(handoff['created_at'], now)}, message {handoff['state']}",
                f"    item {step['item_id']} {step['item']['key']}, need {step['need']}",
            ]
    if state["recent_archives"]:
        lines.append(
            f"{user_name()}'s archives of sessions, last two days (check each makes sense; a message to the session"
            " brings it back):"
        )
        for archive in state["recent_archives"]:
            lines.append(
                f"  session {archive['session_id']}, {_ago(archive['asked_at'], now)}, after step {archive['step_id']}"
                f" [item {archive['item_id']}]: {archive_words(archive)}"
            )
    if state["waiting_sessions"]:
        lines.append(f"Sessions at a dialog in their terminal (a question to {user_name()} is a step instead):")
        for session in state["waiting_sessions"]:
            lines.append(
                f"  {session['id']} {session['name'] or '(unnamed)'} [{session['session_id']}]: "
                f"{session['needs'] or session['detail']}"
            )
    idle = [
        session for session in state["sessions"]
        if session["alive"] and session["close_refusal"] is None
        and (now - rank.parse_time(session["updated_at"])).total_seconds() >= IDLE_LISTED_SECONDS  # ty: ignore[unsupported-operator]
    ]
    if idle:
        lines.append(
            "Sessions idle over an hour, with nothing running or waiting in Cockpit. Judge each. When its work is"
            ' done, close it with `cockpit session close UUID --as sweep --reason "..."`; a later message resumes it.'
        )
        for session in sorted(idle, key=lambda session: session["updated_at"]):
            items = [f"{item['id']} {item['title']}" for item in state["items"] if session["session_id"] in item["sessions"]]
            lines += [
                f"  {session['id']} {session['name'] or '(unnamed)'} [{session['session_id']}]",
                f"    {session['state']}, last change {_ago(session['updated_at'], now)}",
                f"    background: {', '.join(session['in_flight']) or 'none'}",
                f"    items: {'; '.join(items) or 'none'}",
            ]
    stalled = sorted(
        (item for item in state["items"] if item["stalled_since"]),
        key=lambda item: (STALL_ORDER[item["priority"]], item["stalled_since"]),
    )
    if stalled:
        lines.append("")
        lines.append(
            "Agent work that stalled: no session on it has worked for 30 minutes or more. Ask the sessions of all"
            " but the low-priority items to check their work and record their next move:"
        )
        states = {session["session_id"]: session["state"] for session in state["sessions"]}
        for item in stalled:
            sent = item["last_agent_message_at"]
            lines += [
                f"  item {item['id']} {item['title']} ({item['priority']}, stage {item['stage'] or 'none'})",
                f"    stalled since {item['stalled_since'][11:16]} UTC ({_ago(item['stalled_since'], now)})",
                f"    sessions: {', '.join(f'{sid} ({states.get(sid, 'not listed')})' for sid in item['sessions']) or 'none'}",
                f"    last agent message to it: {_ago(sent, now) if sent else 'never'}",
            ]
    # Open work no one has the next move on that the untriaged list does not already hold, such as an agent's note left
    # with no session, wait or step. With stalled work it fills Waiting's Stuck part (SPEC VIEW-33). An item whose step
    # the user snoozed has its next move: the step comes back to the user when it wakes.
    snoozed = {step["item_id"] for step in state["snoozed"]}
    unmoved = sorted(
        (
            item for item in state["items"]
            if item["ball"] == "nobody" and not item["archived"] and not item["untriaged"] and item["id"] not in snoozed
        ),
        key=lambda item: item["updated_at"],
    )
    if unmoved:
        lines.append("")
        lines.append(
            f"Work no one has the next move on: nothing in its wait list and no step for {user_name()}. Give each a"
            " next move this run: ask its session, or start one in the project's checkout with"
            " `cockpit send --item ID --new-in CHECKOUT --as sweep`, when agent work can move it; put the step only"
            f" {user_name()} can take; or put its finish step when the work is done:"
        )
        for item in unmoved:
            lines += [
                f"  item {item['id']} {item['key']} {item['title']} ({item['priority']}, stage {item['stage'] or 'none'})",
                f"    sessions: {', '.join(item['sessions']) or 'none'}; unchanged {_ago(item['updated_at'], now)}",
            ]
    lines.append("")
    review = sorted(
        (
            item for item in state["items"]
            if item["group"] == "waiting" and not item["archived"]
            and (now - rank.parse_time(item["updated_at"])).total_seconds() >= WAITING_REVIEW_AFTER_HOURS * 3600  # ty: ignore[unsupported-operator]
        ),
        key=lambda item: item["updated_at"],
    )[:WAITING_REVIEW_MAX]
    if review:
        lines.append(
            f"Waiting work unchanged for {WAITING_REVIEW_AFTER_HOURS} hours or more, longest first. Review each as the"
            " sweep procedure says under 'Waiting work':"
        )
        for item in review:
            lines.append(
                f"  item {item['id']} {item['title']} ({item['priority']}, stage {item['stage'] or 'none'}):"
                f" {_tip(item)}; unchanged {_ago(item['updated_at'], now)}"
            )
        lines.append("")
    lines.append("Items by group:")
    for group, label in GROUP_LABELS.items():
        items = [item for item in state["items"] if item["group"] == group]
        if items:
            lines.append(f"  {label}: " + "; ".join(
                f"{item['id']} {item['title']}" + (f" ({_tip(item)})" if item["tip"] else "") for item in items
            ))
    places = rank.goal_places(state["goals"])
    lines.append("")
    lines.append(
        "Goals, most important first: "
        + (
            "; ".join(
                f"{places[goal['id']]}. #{goal['id']} {goal['text']}" if goal["id"] in places
                else f"(paused) #{goal['id']} {goal['text']}"
                for goal in state["goals"]
            )
            or "(none)"
        )
    )
    return "\n".join(lines)


def archive_words(archive: dict[str, Any]) -> str:
    if archive["cancelled_at"]:
        return f"{user_name()} reopened the step, so the session was not archived"
    if archive["revived_at"]:
        return "a later message brought the session back"
    if archive["failure"]:
        return f"not archived: {archive['failure']}"
    if archive["stopped_at"]:
        return "archived: stopped, out of the main view"
    return "archiving: Cockpit stops the session once its turn on the archive message is over"


def patterns_text(report: dict[str, Any]) -> str:
    lines = [f"Patterns over the last {report['days']} days (since {report['since']})"]
    lines.append("Repeats:" if report["repeats"] else "Repeats: none")
    lines += [f"  - {repeat}" for repeat in report["repeats"]]
    lines.append(
        f"Steps by kind of need. Columns: created / done by {user_name()} / closed by agents / dismissed by"
        f" {user_name()} / withdrawn / reopened / snoozed / taken to the top / handed off"
    )
    for row in report["by_kind"]:
        lines.append(
            f"  {row['name']}: {row['created']} / {row['done']} / {row['closed by agents']} / {row['dismissed']}"
            f" / {row['withdrawn']} / {row['reopened']} / {row['snoozed']} / {row['taken to the top']}"
            f" / {row['handed off']}"
        )
    if report["median_minutes_to_done"] is not None:
        lines.append(f"Median time from step to {user_name()}'s done: {report['median_minutes_to_done']} min")
    messages = report["messages"]
    lines.append(f"Messages: {messages['sent']} sent, {messages['failed']} failed")
    lines += [f"  failed {failure['at']}: {failure['detail']}" for failure in messages["recent_failures"]]
    for kind, times in report["sweep_times"].items():
        if times is not None:
            lines.append(
                f"{kind.capitalize()} sweeps: {times['count']}, median {times['median_seconds']} s,"
                f" slowest {times['slowest_seconds']} s"
            )
    for sweep in report["sweeps"]:
        lines.append(
            f"Sweep {sweep['id']} by {sweep['by']} at {sweep['started_at']}"
            f"\n  {sweep['minutes']} min, {sweep['steps_put']} steps put"
            f"\n  first step after {sweep['minutes_to_first_step']} min, third after {sweep['minutes_to_third_step']} min"
            f"\n  error: {sweep['error'] or 'none'}"
            + (f"\n  {sweep['summary']}" if sweep["summary"] else "")
        )
    if report["feedback"]:
        lines.append("Recent feedback:")
        lines += [f"  {entry['created_at']}: {entry['text']}" for entry in report["feedback"]]
    lines += permission_review_lines(report["permissions"])
    return "\n".join(lines)


def permission_review_lines(review: dict[str, Any]) -> list[str]:
    """The permission requests of the report's window: totals, then one line per request, with ×N on a call refused
    N times."""
    ends = ", ".join(f"{count} {state}" for state, count in review["by_state"].items())
    lines = [f"Permission requests: {len(review['requests'])}" + (f" ({ends})" if ends else "")]
    lines.append(
        f"  {user_name()} answered {review['user_answers']} permission cards and pressed Run or Allow on"
        f" {review['step_presses']} steps; Codex asked {review['codex_requests']} times"
    )
    if review["explanation_words"] is not None:
        words = review["explanation_words"]
        lines.append(f"  Explanations: median {words['median']} words, longest {words['longest']}")
    lines += [f"  rule added {rule['at']} to {rule['list']}: {rule['rule']}" for rule in review["rules_added"]]
    for request in review["requests"]:
        end = request["state"] + (f" by {request['answered_by']}" if request["answered_by"] else "")
        waited = "" if request["minutes_open"] is None else f" after {request['minutes_open']} min"
        read = "" if request["explanation_words"] is None else f", {request['explanation_words']} words to read"
        call = request["call"].replace("\n", " ")
        times = f" ×{request['times']}" if request["times"] > 1 else ""
        lines.append(f"  {request['id']} {request['tool']} [{request['stopped_as']}]{times} {end}{waited}{read}: {call}")
    return lines


def control_uses_text(report: dict[str, Any]) -> str:
    """One line per control, most pressed first: presses, hover stops, days on the page, then where and its label."""
    if not report["controls"]:
        return f"No control uses recorded since {report['since']}."
    lines = [
        f"Since {report['since']} (tracked since {report['tracked_since']}):",
        f"{'press':>6} {'hover':>6} {'days':>5}  page  control  [last label]",
    ]
    for c in report["controls"]:
        lines.append(f"{c['press']:>6} {c['hover']:>6} {c['seen']:>5}  {c['page']}  {c['control']}")
    return "\n".join(lines)


def page_errors_text(listing: dict[str, Any]) -> str:
    """Each error with its count and where it happened, then its stack; the listing holds the most recent few."""
    if not listing["total"]:
        return "No errors on Cockpit's pages."
    blocks = []
    for error in listing["errors"]:
        state = f"taken {error['taken_at']}: {error['taken_note']}" if error["taken_at"] else "open"
        if not error["taken_at"] and error["taken_note"]:
            state = f"open again; taken before: {error['taken_note']}"
        stack = "\n".join(f"    {line.strip()}" for line in error["stack"].splitlines()[:8])
        blocks.append(
            f"Error {error['id']}: {error['message']}\n  page {error['page']}, place {error['place']}"
            f"\n  seen {error['count']} times, from {error['first_at']} to {error['last_at']}\n  state: {state}"
            + (f"\n{stack}" if stack else "")
        )
    left = listing["total"] - len(listing["errors"])
    if left:
        blocks.append(f"and {left} more seen earlier")
    return "\n\n".join(blocks)


def server_logs_text(report: dict[str, Any]) -> str:
    """Totals per level, then one line per kind of line, most frequent first, ids and numbers shown as N."""
    totals = report["totals"]
    lines = [
        f"Server log since {report['since']} (tracked since {report['tracked_since']}): {totals['WARNING']} warnings,"
        f" {totals['ERROR']} errors, {totals['CRITICAL']} critical, in {report['kinds']} kinds of line",
    ]
    if report["lines"]:
        lines.append(f"{'count':>6} {'days':>4}  last                  level    logger: message")
    for line in report["lines"]:
        lines.append(
            f"{line['count']:>6} {line['days']:>4}  {line['last_at']}  {line['level']:<8} {line['logger']}: {line['message']}"
        )
    left = report["kinds"] - len(report["lines"])
    if left:
        lines.append(f"and {left} rarer kinds")
    return "\n".join(lines)


def request_stats_text(report: dict[str, Any]) -> str:
    """Totals, then one line per route, busiest first, with the failing and slow routes after them."""
    lines = [
        f"Requests since {report['since']} (tracked since {report['tracked_since']}): {report['requests']},"
        f" {report['failed']} failed with a server error, {report['slow']} slow",
    ]
    if report["routes"]:
        lines.append(f"{'count':>8} {'5xx':>5} {'4xx':>5} {'mean':>6} {'max':>7} {'slow':>5}  route  [callers]")
    for route in report["routes"]:
        callers = ", ".join(
            f"{caller} {count}" for caller, count in sorted(route["by_caller"].items(), key=lambda pair: -pair[1])[:4]
        )
        lines.append(
            f"{route['count']:>8} {route['failed']:>5} {route['refused']:>5} {route['mean_ms']:>4}ms {route['max_ms']:>5}ms"
            f" {route['slow']:>5}  {route['method']} {route['route']}  [{callers}]"
        )
    return "\n".join(lines)


def presses_text(report: dict[str, Any]) -> str:
    """One line per press, oldest first: when, page, control, and the task and step it was on."""
    if not report["total"]:
        return f"No presses since {report['since']}."
    lines = [f"{report['total']} presses since {report['since']}" + (
        f"; the last {len(report['presses'])} shown" if report["total"] > len(report["presses"]) else ""
    )]
    for press in report["presses"]:
        on = "".join(
            [f" item {press['item_id']}" if press["item_id"] is not None else "",
             f" step {press['step_id']}" if press["step_id"] is not None else ""]
        )
        lines.append(f"{press['at']}  {press['page']}  {press['control']}{on}")
    return "\n".join(lines)


def flashes_text(found: list[dict[str, Any]]) -> str:
    """One line per card that left the page soon after showing, newest first."""
    if not found:
        return f"No card left {user_name()}'s page within a minute of showing."
    lines = []
    for flash in found:
        seconds = (datetime.fromisoformat(flash["gone_at"]) - datetime.fromisoformat(flash["shown_at"])).total_seconds()
        lines.append(
            f"{flash['gone_at']}  {flash['kind']} {flash['ref']}, gone after {seconds:.0f} s on {flash['page']}:"
            f" {flash['outcome']}\n    {flash['label']}"
        )
    return "\n".join(lines)


def transcript_path(session_id: str) -> str:
    """The session's Claude Code transcript on this machine, or "" when none is here."""
    found = sorted(Path.home().glob(f".claude/projects/*/{session_id}.jsonl"))
    return str(found[0]) if found else ""


WRITING_HEADS = {
    "good": "Good writing: reproduce what the note praises",
    "bad": "Hard to understand: avoid what made these hard to follow",
}


def writing_text(examples: list[dict[str, Any]]) -> str:
    """Each writing example (SPEC FEEDBACK-8), the good ones first, then those the user found hard to understand,
    with where it came from, who wrote it and the transcript that shows how, the skills it had loaded, the user's
    note, and the start of its text; `cockpit writing get ID` prints all of it."""
    if not examples:
        return f"{user_name()} has not marked any writing yet."
    blocks = []
    for verdict, head in WRITING_HEADS.items():
        marked = [example for example in examples if example["verdict"] == verdict]
        blocks.append(f"== {head} ({len(marked)}) ==")
        blocks += [_writing_block(example) for example in marked]
    return "\n\n".join(blocks)


def _writing_block(example: dict[str, Any]) -> str:
    lines = [f"Example {example['id']}, {example['kind']}, marked {example['created_at']}: {example['title']}"]
    if example["item_title"]:
        lines.append(f"  item {example['item_id']}: {example['item_title']}")
    lines.append(f"  where: {example['target']}")
    if example["session_id"]:
        transcript = transcript_path(example["session_id"])
        lines.append(f"  written by session {example['session_id']} ({example['session_name'] or 'no name'})")
        lines.append(f"  transcript: {transcript or 'not on this machine'}")
    elif example["written_by"]:
        lines.append(f"  written by {example['written_by']}")
    if example["skills"]:
        skills = ", ".join(f"{skill['name']} {skill['version']} ({skill['folder']})" for skill in example["skills"])
        lines.append(f"  skills loaded: {skills}")
    if example["note"]:
        lines.append(f"  {user_name()}'s note: {example['note']}")
    start = " ".join(example["text"].split())
    lines.append(f"  text ({example['format']}): {start[:300]}{'…' if len(start) > 300 else ''}")
    return "\n".join(lines)


def goals_text(goals: list[dict[str, Any]]) -> str:
    """The user's goals, most important first, one block each with its place, id and time.

    A paused goal keeps its spot in the list but has no place: the ranking counts it for nothing.
    """
    if not goals:
        return f"(no goals from {user_name()})"
    places = rank.goal_places(goals)
    blocks = [
        (
            f"{places[goal['id']]}. Goal {goal['id']}" if goal["id"] in places
            else f"Goal {goal['id']} (paused by {user_name()}: leave it aside until they resume it)"
        )
        + f", added {rank.parse_time(goal['created_at']):%Y-%m-%d %H:%M} UTC"
        + (
            f", last changed {rank.parse_time(goal['changed_at']):%Y-%m-%d %H:%M} UTC"
            if goal["changed_at"] != goal["created_at"] else ""
        )
        + f":\n{goal['text']}"
        for goal in goals
    ]
    return "\n\n".join(blocks)


def wait_for_delivery(message: dict[str, Any]) -> dict[str, Any] | str:
    """The message once it is delivered; or, in words, that it waits for a place among the working sessions
    (SPEC SESSION-23), which is no failure: Cockpit sends it when a session finishes."""
    deadline = time.monotonic() + SEND_WAIT_SECONDS
    while message["state"] in ("queued", "sending"):
        if message["state"] == "queued" and message["capacity_since"] is not None:
            return (
                f"waits: at most {store.WORKING_LIMIT} sessions work at once, and every place is taken."
                f" Cockpit sends message {message['id']} when a place is free."
            )
        if time.monotonic() > deadline:
            raise CockpitError(
                f"message {message['id']} is still {message['state']} after {SEND_WAIT_SECONDS} s."
                f" Check it later on the page or with GET /api/messages/{message['id']}."
            )
        time.sleep(SEND_POLL_SECONDS)
        message = call("GET", f"/api/messages/{message['id']}", None, None)
    if message["state"] == "failed":
        raise CockpitError(f"message {message['id']} failed: {message['detail']}")
    return message


def _id_list(text: str) -> set[int]:
    try:
        ids = {int(part) for part in text.split(",") if part.strip()}
    except ValueError as exc:
        raise CockpitError(f"--ids takes comma-separated numbers, not {text!r}") from exc
    if not ids:
        raise CockpitError("--ids names no entries")
    return ids


def _read_file(path: str) -> str:
    try:
        return Path(path).read_text()
    except OSError as exc:
        raise CockpitError(f"cannot read {path}: {exc}") from exc


def _edit_outcome(result: dict[str, Any]) -> str:
    if not result["changed"]:
        return f"nothing changed; version {result['version']}"
    if not result["committed"]:
        raise CockpitError(
            f"the spec is saved (version {result['version']}) but not committed and pushed: {result['commit_error']}"
        )
    if not result["pushed"]:
        return f"saved and committed; no deploy branch is set, so nothing was pushed; version {result['version']}"
    return f"saved, committed and pushed to the deploy branch; version {result['version']}"


def spec_command(args: argparse.Namespace) -> str:
    if args.action == "get":
        page = call("GET", "/api/spec", None, None)
        Path(args.out).write_text(page["text"])
        return page["version"]
    if args.action == "edit":
        body = {"old": _read_file(args.old_file).strip("\n"), "new": _read_file(args.new_file).strip("\n")}
        return _edit_outcome(call("POST", "/api/spec/part", body, args.agent))
    body = {"version": args.version, "text": _read_file(args.path)}
    return _edit_outcome(call("PUT", "/api/spec", body, args.agent))


def image_command(args: argparse.Namespace) -> str:
    if args.action == "get":
        content, content_type = fetch("GET", f"/api/item-images/{args.image_id}", None, None)
        args.out.write_bytes(content)
        return f"wrote image {args.image_id} ({content_type}, {len(content)} bytes) to {args.out}"
    if args.action == "remove":
        image = call("DELETE", f"/api/item-images/{args.image_id}", None, args.agent)
        return f"removed image {image['id']} from item {image['item_id']}"
    try:
        content = args.file.read_bytes()
    except OSError as exc:
        raise CockpitError(f"cannot read {args.file}: {exc.strerror}") from exc
    content_type = store.image_type(content)
    if content_type is None:
        raise CockpitError(f"{args.file} is not a PNG, JPEG, GIF or WebP file, the image types Cockpit keeps")
    item_id = (
        int(args.item) if args.item.isdigit()
        else call("GET", "/api/items?" + urllib.parse.urlencode({"key": args.item}), None, None)["id"]
    )
    response, _ = fetch("POST", f"/api/items/{item_id}/images", (content, content_type), args.agent)
    return str(json.loads(response)["id"])


def page_command(args: argparse.Namespace) -> str:
    if args.action == "get":
        content, _ = fetch("GET", f"/pages/{args.page_id}", None, None)
        args.out.write_bytes(content)
        return f"wrote page {args.page_id} ({len(content)} bytes) to {args.out}"
    if args.action == "choices":
        return json.dumps(call("GET", f"/api/item-pages/{args.page_id}/choices", None, None)["choices"], indent=2)
    if args.action == "remove":
        page = call("DELETE", f"/api/item-pages/{args.page_id}", None, args.agent)
        return f"removed page {page['id']} from item {page['item_id']}"
    if args.action == "unpublish":
        page = call("DELETE", f"/api/item-pages/{args.page_id}/public", None, args.agent)
        return f"page {page['id']} is shared with nobody now; its link stops working until it is shared again"
    try:
        body = {"title": args.title, "html": args.file.read_text()}
    except (OSError, UnicodeDecodeError) as exc:
        raise CockpitError(f"cannot read {args.file} as text: {exc}") from exc
    if args.action == "update":
        page = call("PUT", f"/api/item-pages/{args.page_id}", body, args.agent)
        return f"updated page {page['id']}: {page['url']}"
    item_id = (
        int(args.item) if args.item.isdigit()
        else call("GET", "/api/items?" + urllib.parse.urlencode({"key": args.item}), None, None)["id"]
    )
    page = call("POST", f"/api/items/{item_id}/pages", body, args.agent)
    return f"page {page['id']}: {page['url']}"


def clip(text: str) -> str:
    """`text`, or its start and its length in REASON_MAX characters, the most the API takes."""
    if len(text) <= REASON_MAX:
        return text
    length = f"… ({len(text)} characters)"
    return text[: REASON_MAX - len(length)] + length


def _blocks(entry: Any) -> list[dict[str, Any]]:
    message = entry.get("message") if isinstance(entry, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _transcript_blocks(data: bytes) -> list[dict[str, Any]]:
    blocks = []
    for line in data.split(b"\n"):
        try:
            blocks += _blocks(json.loads(line))
        except ValueError:
            continue  # the first line of a tail read is usually cut short
    return blocks


def _complete_lines(transcript: Path, offset: int) -> tuple[bytes, int]:
    """The transcript's whole lines from `offset`, and the offset after them."""
    with transcript.open("rb") as file:
        file.seek(offset)
        data = file.read()
    end = data.rfind(b"\n") + 1  # a line still being written is read next time
    return data[:end], offset + end


def matching_calls(transcript: Path, tool_name: str, tool_input: Any) -> tuple[dict[str, bool], int]:
    """The calls with this tool and input in the transcript's tail, oldest first, each
    with whether its result is written; and how far the transcript was read."""
    data, end = _complete_lines(transcript, max(0, transcript.stat().st_size - TRANSCRIPT_TAIL_BYTES))
    calls: dict[str, bool] = {}
    for block in _transcript_blocks(data):
        if block.get("type") == "tool_use" and block.get("name") == tool_name and block.get("input") == tool_input:
            calls[block.get("id", "")] = False
        elif block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
            calls[block["tool_use_id"]] = True
    return calls, end


def result_written(transcript: Path, tool_use_id: str, offset: int) -> tuple[bool, int]:
    """Whether the call's result was written after `offset`, and where to read from next."""
    data, end = _complete_lines(transcript, offset)
    found = any(
        block.get("type") == "tool_result" and block.get("tool_use_id") == tool_use_id
        for block in _transcript_blocks(data)
    )
    return found, end


def _hook_report(kind: str, payload: Any, tool_use_id: str) -> dict[str, Any] | None:
    """The call to report, whole; None for one too long for Cockpit to show whole, which stays with the session:
    its dialog waits in the session, and auto mode's refusal stands."""
    if not isinstance(payload, dict):
        raise CockpitError("the hook input is not a JSON object")
    missing = [key for key in ("session_id", "cwd", "tool_name", "tool_input") if key not in payload]
    if missing:
        raise CockpitError(f"the hook input has no {', '.join(missing)}")
    size = len(json.dumps(payload["tool_input"], sort_keys=True, ensure_ascii=False))
    if size > permissions.TOOL_INPUT_MAX:
        print(
            f"cockpit: this {payload['tool_name']} call is {size:,} characters as JSON. Cockpit shows calls whole up"
            f" to {permissions.TOOL_INPUT_MAX:,}, so this one stays with the session.",
            file=sys.stderr,
        )
        return None
    return {
        "kind": kind,
        "session_id": payload["session_id"],
        "cwd": payload["cwd"],
        "tool_name": payload["tool_name"],
        "tool_input": payload["tool_input"],
        "tool_use_id": tool_use_id,
        "reason": clip(str(payload.get("reason", ""))),
    }


# Permission hooks (SPEC PERM-1). Both read the hook's JSON on stdin.
# `permission report` is the PermissionDenied hook: it records a call auto mode
# blocked and returns at once. `permission ask` is the PermissionRequest hook:
# it records the call while the session shows its permission dialog, then waits
# for the user's answer on Cockpit. The dialog stays usable meanwhile and the first
# answer wins: an answer on Cockpit is printed as the hook's decision, which
# closes the dialog; an answer in the session appears in the transcript as the
# call's result, which ends the wait. A failure exits 1, which Claude Code shows
# as a hook error and otherwise ignores.
def _hook_decision(decision: dict[str, str]) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}})


def _hook_allow() -> str:
    return _hook_decision({"behavior": "allow"})


def _hook_deny(message: str) -> str:
    """Deny the call; the session reads `message` as the reason."""
    return _hook_decision({"behavior": "deny", "message": message})


def _user_answer(state: str) -> str | None:
    """The hook's decision for the user's answer on Cockpit; None for a request that ended any other way."""
    if state == "allowed":
        return _hook_allow()
    return _hook_deny(f"{user_name()} denied this call from Cockpit.") if state == "denied" else None


def permission_report(payload: Any, agent: str) -> None:
    tool_use_id = payload.get("tool_use_id", "") if isinstance(payload, dict) else ""
    body = _hook_report("denied", payload, tool_use_id)
    if body is not None:
        call("POST", "/api/permissions/requests", body, agent)


def permission_ask(payload: Any, agent: str) -> str | None:
    """Wait for the user's answer: the hook's decision, or None to leave the dialog to the session.

    The session's dialog stays open meanwhile. Once the call's result is in the
    transcript, the session answered first. Claude Code can write the call to the
    transcript just after the hook starts, so the hook looks for it every round:
    the newest call with the same tool and input, leaving out the ones that already
    had a result when the hook started, which are older calls.
    """
    # The emergency page cannot show a dialog's card, so its turns leave every dialog to Claude Code (emergency.py).
    if os.environ.get("COCKPIT_EMERGENCY"):
        return None
    body = _hook_report("asked", payload, "")
    if body is None:
        return None
    transcript =Path(payload.get("transcript_path") or "/nonexistent")
    tool = (payload["tool_name"], payload["tool_input"])
    older = set()
    if transcript.is_file():
        older = {tool_use_id for tool_use_id, done in matching_calls(transcript, *tool)[0].items() if done}
    reported = call("POST", "/api/permissions/requests", body, agent)
    # Cockpit answers some dialogs as it records them (permissions.report): it allows entering a worktree of the
    # user's checkouts, and denies a call the session should split, with advice for the session.
    if reported["state"] == "allowed":
        return _hook_allow()
    if reported["state"] == "denied":
        return _hook_deny(reported["advice"])
    path = f"/api/permissions/requests/{reported['id']}"
    tool_use_id, offset = "", 0
    deadline = time.monotonic() + permissions.ASK_WAIT_SECONDS
    unreachable = False
    while time.monotonic() < deadline:
        time.sleep(ASK_POLL_SECONDS)
        answered = False
        if tool_use_id:
            answered, offset = result_written(transcript, tool_use_id, offset)
        elif transcript.is_file():
            calls, offset = matching_calls(transcript, *tool)
            fresh = [(call_id, done) for call_id, done in calls.items() if call_id not in older]
            if fresh:
                tool_use_id, answered = fresh[-1]
        if answered:
            call("POST", f"{path}/settle", {"state": "answered"}, agent)
            return None
        try:
            state = call("GET", path, None, None)["state"]
        except CockpitError as exc:
            # A Cockpit restart takes seconds; keep waiting through it.
            if not unreachable:
                print(f"cockpit: still waiting; {exc}", file=sys.stderr)
            unreachable = True
            continue
        unreachable = False
        if state != "open":
            return _user_answer(state)
    # The user's answer may land between the last look and the end of the wait; after it, the user's answer restarts
    # the session instead (SPEC PERM-8).
    settled = call("POST", f"{path}/settle", {"state": "expired"}, agent)["state"]
    return _user_answer(settled)


# A session's own say on the calls it made (SPEC PERM-6, PERM-7): it lists them, takes
# back a call it no longer needs, and proposes the auto-mode sentence that would allow one.


def permission_list(state: dict[str, Any], session_id: str) -> str:
    """The session's open calls as the page shows them."""
    # A call still held back from the page, or one the session got past, is open too (SPEC PERM-18).
    waiting = [
        entry
        for entry in state["permission_requests"] + state["moved_past"] + state["held_requests"]
        if entry["session_id"] == session_id
    ]
    if not waiting:
        lines = [f"Session {session_id} has no open calls on Cockpit."]
    else:
        lines = [
            (
                f"Open calls of session {session_id}.\n"
                "Take one back with `cockpit permission withdraw ID --session UUID --as NAME`.\n"
                "Propose the sentence that would allow one with `cockpit permission propose ID --session UUID --as"
                " NAME --sentence TEXT`."
            )
        ]
        for entry in waiting:
            lines.append(f"  request {entry['id']}, {_request_words(entry)}")
            if entry["proposed_sentence"] is not None:
                lines.append(f"    you proposed: {entry['proposed_sentence']}")
    return "\n".join(lines)


def permission_withdraw(request_ids: list[int], every: bool, session_id: str, agent: str) -> str:
    """Take back the session's open calls: the ones named, or with `every` all of them."""
    if every == bool(request_ids):
        raise CockpitError("name the request ids, or give --all")
    body = {"session_id": session_id}
    if every:
        withdrawn = call("POST", "/api/permissions/requests/withdraw-all", body, agent)["withdrawn"]
    else:
        withdrawn = [call("POST", f"/api/permissions/requests/{rid}/withdraw", body, agent) for rid in request_ids]
    if not withdrawn:
        return f"session {session_id} has no open calls"
    lines = []
    for entry in withdrawn:
        summary = permissions.call_summary(entry["tool_name"], entry["tool_input"]).splitlines()[0][:160]
        if entry["state"] == "open":
            lines.append(
                f"request {entry['id']} waits: {user_name()} is answering it on Cockpit. It is withdrawn once"
                f" {user_name()} answers, or {permissions.USER_ON_CARD_MINUTES} minutes after {user_name()} last worked"
                f" on it. The call: {summary}"
            )
        else:
            lines.append(f"withdrew request {entry['id']}: {summary}")
    return "\n".join(lines)


def permission_propose(request_id: int, session_id: str, sentence: str, agent: str) -> str:
    body = {"session_id": session_id, "sentence": sentence.strip()}
    proposed = call("POST", f"/api/permissions/requests/{request_id}/propose", body, agent)
    return f"request {proposed['id']}: proposed the auto-mode sentence \"{proposed['proposed_sentence']}\""


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in {"setup", "login", "service", "deploy"}:
        from cockpit import local

        return local.main(argv)
    try:
        help_user = user_name()
    except config.SetupError:
        help_user = "the user"
    agent = argparse.ArgumentParser(add_help=False)
    agent.add_argument("--as", dest="agent", required=True, metavar="NAME", help="who is writing, e.g. sweep")
    raw = argparse.RawTextHelpFormatter
    parser = argparse.ArgumentParser(prog="cockpit", description=__doc__, formatter_class=raw)
    commands = parser.add_subparsers(dest="command", required=True)

    get = commands.add_parser("get", help="what is on Cockpit now")
    get.add_argument("--json", action="store_true", help="the state as JSON, without the page's rendered HTML")

    item = commands.add_parser("item", help="read, create or update an item")
    item_commands = item.add_subparsers(dest="action", required=True)
    item_commands.add_parser(
        "put", parents=[agent], help="create or update an item by its key; reads JSON on stdin",
        epilog=Template(ITEM_FIELDS).substitute(user=help_user), formatter_class=raw,
    )
    item_get = item_commands.add_parser("get", help=f"one item with all its steps, open and closed, and {help_user}'s messages on it, as JSON")
    item_get.add_argument("key")
    item_merge = item_commands.add_parser(
        "merge", parents=[agent],
        help=f"make two items that are one task into one: SIDE's steps, messages and sessions move to MAIN, SIDE's open"
        f" cards are withdrawn and SIDE is archived; {help_user}'s card on MAIN shows the merge. Waits while"
        f" {help_user}'s timer runs on a step of either item.",
    )
    item_merge.add_argument("side", metavar="SIDE", help="the item that joins the other: its id or key")
    item_merge.add_argument("--into", required=True, metavar="MAIN", help="the item it joins: its id or key")
    item_merge.add_argument("--note", required=True, help=f"why the two are one task, shown to {help_user} with the merge")

    step = commands.add_parser("step", help="create, update or close a step")
    step_commands = step.add_subparsers(dest="action", required=True)
    step_commands.add_parser(
        "put", parents=[agent], help="create or update a step by its (item, need); reads JSON on stdin",
        epilog=Template(STEP_FIELDS).substitute(user=help_user), formatter_class=raw,
    )
    step_actions = {
        "done": "the need is gone; the (item, need) stays closed",
        "dismiss": "drop it; the (item, need) stays closed",
        "withdraw": "take it out of the queue until it is ready again; a later put brings it back as new",
        "yield": (
            f"give the step's place in {help_user}'s queue to the strongest ready-next step. The step keeps its"
            " text and waits as ready next"
        ),
        "return": (
            f"give a step {help_user} handed you back to their queue. First rewrite it with your answer at the"
            f" top, or give your reply with --note, which goes first on the step under {help_user}'s message"
        ),
    }
    for action, help_text in step_actions.items():
        step_action = step_commands.add_parser(action, parents=[agent], help=help_text)
        step_action.add_argument("step_id", type=int)
        if action in ("done", "dismiss"):
            step_action.add_argument(
                "--note", help=f'one line: why, e.g. "merged"; {help_user} sees it in the list of what agents closed'
            )
        if action == "return":
            step_action.add_argument(
                "--note",
                help=f"your reply to {help_user}'s message, shown first on the step under it. A finish step needs it"
                " once everything the message asked is done. Cockpit then writes the step again from it, with its"
                ' first sentence as the ask, such as "The Tasks CSV export now includes completed tasks. The export check passed."',
            )
    checked = step_commands.add_parser(
        "checked", parents=[agent], help="record that you checked the step against live state and it still holds"
    )
    checked.add_argument("step_id", type=int)
    checked.add_argument("--note", required=True, help="one line: what you checked")

    commands.add_parser(
        "goals", help=f"print {help_user}'s goals, most important first: what matters and in which order"
    )

    image = commands.add_parser(
        "image", help="add, save or remove an item's images; their ids are in the item's `images`"
    )
    image_commands = image.add_subparsers(dest="action", required=True)
    image_get = image_commands.add_parser("get", help="write an image to a file; prints its type and size")
    image_get.add_argument("image_id", type=int, metavar="ID")
    image_get.add_argument("--out", required=True, type=Path, metavar="PATH", help="the file to write")
    image_add = image_commands.add_parser(
        "add", parents=[agent],
        help="attach a PNG, JPEG, GIF or WebP file to an item, e.g. a screenshot or a graph; prints the image's id",
    )
    image_add.add_argument("item", metavar="ITEM", help="the item's key or id")
    image_add.add_argument("--file", required=True, type=Path, metavar="PATH")
    image_remove = image_commands.add_parser(
        "remove", parents=[agent], help=f"remove an image you added ({help_user} can remove any)"
    )
    image_remove.add_argument("image_id", type=int, metavar="ID")

    page = commands.add_parser(
        "page",
        help=f"add, replace, save, remove or stop sharing an item's HTML pages, which {help_user} opens from the item;"
        " their ids are in the item's `pages`",
    )
    page_commands = page.add_subparsers(dest="action", required=True)
    page_add = page_commands.add_parser(
        "add", parents=[agent], help="share an HTML file on an item, e.g. a report; prints the page's id and link"
    )
    page_add.add_argument("item", metavar="ITEM", help="the item's key or id")
    page_add.add_argument("--file", required=True, type=Path, metavar="PATH", help="a whole HTML document")
    page_add.add_argument("--title", required=True, help="the page's name as the item lists it")
    page_update = page_commands.add_parser(
        "update", parents=[agent], help="replace a page you added; its link stays the same"
    )
    page_update.add_argument("page_id", type=int, metavar="ID")
    page_update.add_argument("--file", required=True, type=Path, metavar="PATH", help="a whole HTML document")
    page_update.add_argument("--title", required=True, help="the page's name as the item lists it")
    page_get = page_commands.add_parser("get", help="write a page's HTML to a file")
    page_get.add_argument("page_id", type=int, metavar="ID")
    page_get.add_argument("--out", required=True, type=Path, metavar="PATH", help="the file to write")
    page_remove = page_commands.add_parser(
        "remove", parents=[agent], help=f"remove a page you added ({help_user} can remove any)"
    )
    page_remove.add_argument("page_id", type=int, metavar="ID")
    page_unpublish = page_commands.add_parser(
        "unpublish", parents=[agent],
        help=f"stop sharing a page, so its link stops working; only {help_user} can share it again, on the page",
    )
    page_unpublish.add_argument("page_id", type=int, metavar="ID")
    page_choices = page_commands.add_parser(
        "choices",
        help=f"print what {help_user} chose on a page (its named form controls or `cockpit.choose`) as JSON: each"
        " choice's name, value, the words the page shows for it, and whether it went with an answer yet",
    )
    page_choices.add_argument("page_id", type=int, metavar="ID")

    message = commands.add_parser(
        "message", help="a failed or waiting message (ids in `cockpit get`): dismiss it, or retry one of your own"
    )
    message_commands = message.add_subparsers(dest="action", required=True)
    dismiss = message_commands.add_parser(
        "dismiss", parents=[agent],
        help=(
            "set a message aside: a failed one whose work was done another way, or one that waits for its session's"
            f" turn whose text reached the session another way. It leaves {help_user}'s failure count"
        ),
    )
    dismiss.add_argument("message_id", type=int, metavar="ID")
    dismiss.add_argument(
        "--reason", required=True, metavar="TEXT",
        help="one line: why it needs nothing more, e.g. 'the item has a working session since 10:00'",
    )
    retry = message_commands.add_parser(
        "retry", parents=[agent], help="send one of your failed messages again and wait until it is delivered"
    )
    retry.add_argument("message_id", type=int, metavar="ID")

    session = commands.add_parser("session", help="close a live session the sweeper judged done, or move one")
    session_commands = session.add_subparsers(dest="action", required=True)
    close = session_commands.add_parser(
        "close", parents=[agent],
        help=(
            "stop a live session whose work is done. Only the sweeper does this. Cockpit refuses while the"
            " session's turn runs, a dialog or subagent waits, or a step or message goes to it. A later message"
            " resumes the session"
        ),
    )
    close.add_argument("session_id", metavar="UUID")
    close.add_argument(
        "--reason", required=True, metavar="TEXT",
        help="one line: why its work is done, e.g. 'export checked, nothing left on its item'",
    )

    move = session_commands.add_parser(
        "move", parents=[agent],
        help=(
            f"move a Claude session to another Claude account, only when {help_user} asked for that move. The"
            " sweeper never moves one. A move asked while the session's turn runs waits, exits 0 and says so;"
            " Cockpit moves the session once its turn is over. A session the usage limit stopped continues on the"
            " new account"
        ),
    )
    move.add_argument("session_id", metavar="UUID")
    move.add_argument("--to", required=True, metavar="ACCOUNT", help="the account's name, e.g. work or personal")

    send = commands.add_parser(
        "send", parents=[agent], formatter_class=raw,
        help="send a message to a session and wait until it is delivered",
        epilog="--item alone goes to the step's or the item's session. With --session or --new-in, the message\n"
               "is also recorded on the item, and a new session joins the item. Exits 1 if delivery fails.\n"
               "Sent from a session, the message names that session ($CLAUDE_CODE_SESSION_ID or $CODEX_THREAD_ID)\n"
               "and gives the receiver the command that replies to it. Use this command to message another session\n"
               "on either account; SendMessage reaches only sessions on your own account.",
    )
    send.add_argument("--item", type=int, help="item id the message is about")
    send.add_argument("--step", type=int, help="with --item: the step it answers; its target session is used")
    send.add_argument("--provider", choices=("claude", "codex"), help="provider for --new-in (defaults to setup.toml)")
    where = send.add_mutually_exclusive_group()
    where.add_argument("--session", help="session UUID")
    where.add_argument("--new-in", help="start a new session in this checkout")
    send.add_argument("text", nargs="?", default="-", help="the message; '-' or left out reads stdin")

    shown = commands.add_parser(
        "attach",
        help="show a session in your tmux, in the window this opened for it before or a new one in the checkouts"
             " folder. The page's iTerm2 link runs this",
    )
    shown.add_argument("session", help="the session's short id, as `claude agents` prints it")

    feedback = commands.add_parser("feedback", help=f"{help_user}'s unread feedback")
    feedback_mode = feedback.add_mutually_exclusive_group()
    feedback_mode.add_argument(
        "--mark-read", metavar="NAME", help="mark feedback read, writing as NAME (needs --through or --ids)"
    )
    feedback_mode.add_argument("--all", action="store_true", help="all feedback, read and unread, oldest first")
    which = feedback.add_mutually_exclusive_group()
    which.add_argument("--through", type=int, metavar="ID", help="with --mark-read: every unread entry up to this id")
    which.add_argument("--ids", metavar="ID,ID", help="with --mark-read: exactly these entries")

    spec = commands.add_parser("spec", help="read or edit the spec (docs/SPEC.md); the server commits each edit")
    spec_commands = spec.add_subparsers(dest="action", required=True)
    spec_get = spec_commands.add_parser("get", help="write the spec to a file and print its version")
    spec_get.add_argument("--out", required=True, metavar="PATH")
    spec_edit = spec_commands.add_parser(
        "edit", parents=[agent], formatter_class=raw,
        help="replace one part: a heading, paragraph, list item or table row",
        epilog="The old file holds the part exactly as the spec has it now; it must appear once.\n"
               "An empty new file removes the part. Fails if the part changed meanwhile: get the spec again.",
    )
    spec_edit.add_argument("--old-file", required=True, metavar="PATH")
    spec_edit.add_argument("--new-file", required=True, metavar="PATH")
    spec_put = spec_commands.add_parser(
        "put", parents=[agent], help="replace the whole spec; fails if it changed since the version you read"
    )
    spec_put.add_argument("--version", required=True, help="the version `cockpit spec get` printed")
    spec_put.add_argument("path")

    pattern = commands.add_parser("patterns", help="what keeps happening: repeats, step outcomes, sweeps")
    pattern.add_argument("--days", type=int, required=True, help="how many days back, 1-90")
    pattern.add_argument("--json", action="store_true")

    uses = commands.add_parser("ui-usage", help="which page controls the user presses and which hover texts they stop on")
    uses.add_argument("--days", type=int, required=True, help="how many days back, 1-365")
    uses.add_argument("--json", action="store_true")

    flashed = commands.add_parser(
        "flashes", help=f"cards that left {help_user}'s page within a minute of showing, and what took them away"
    )
    flashed.add_argument("--days", type=int, required=True, help="how many days back, 1-365")

    logged = commands.add_parser(
        "logs", help="what the server logged at warning and above, counted by kind of line (its errors are also in `errors`)"
    )
    logged.add_argument("--days", type=int, required=True, help="how many days back, 1-90")
    logged.add_argument("--json", action="store_true")

    answered = commands.add_parser(
        "requests", help="how each route answered: counts by caller, server errors, refusals, time taken"
    )
    answered.add_argument("--days", type=int, required=True, help="how many days back, 1-90")
    answered.add_argument("--json", action="store_true")

    pressed = commands.add_parser("presses", help=f"each press on {help_user}'s page, in order, with its task and step")
    pressed.add_argument("--days", type=int, required=True, help="how many days back, 1-90")
    pressed.add_argument("--item", type=int, help="only the presses on this item")
    pressed.add_argument("--json", action="store_true")

    errors = commands.add_parser("errors", help="errors Cockpit's own pages met, for a session to fix")
    errors_commands = errors.add_subparsers(dest="action", required=True)
    errors_list = errors_commands.add_parser("list", help="the open errors, most recent first, with their stacks")
    errors_list.add_argument("--all", action="store_true", help="also the errors a session already took")
    errors_take = errors_commands.add_parser(
        "take", parents=[agent], help="mark errors as taken by the session that fixes them"
    )
    errors_take.add_argument("--ids", required=True, metavar="ID,ID")
    errors_take.add_argument("--note", required=True, help="who fixes them, e.g. the session's name")

    writing = commands.add_parser(
        "writing", help=f"writing {help_user} marked as good or as hard to understand, to learn from"
    )
    writing_commands = writing.add_subparsers(dest="action", required=True)
    writing_list = writing_commands.add_parser(
        "list", help="the good examples first, then the hard to understand ones, each newest first. Each shows who"
        " wrote it, their transcript and the note on why"
    )
    writing_list.add_argument("--json", action="store_true")
    writing_get = writing_commands.add_parser("get", help="print one example's whole text")
    writing_get.add_argument("example_id", type=int)

    project = commands.add_parser("project", help="the projects every task is in, each with its logo")
    project_commands = project.add_subparsers(dest="action", required=True)
    project_list = project_commands.add_parser("list", help="each project, what it covers and its open items")
    project_list.add_argument("--json", action="store_true")
    project_commands.add_parser(
        "put", parents=[agent],
        help='create a project or change it, JSON on stdin: {"name": ..., "about": "one line on what it covers",'
             ' "logo": "<svg ...>", "checkout": "/absolute/path/to/a/git/checkout"}; a field left out stays, a null checkout clears it',
    )
    project_merge = project_commands.add_parser(
        "merge", parents=[agent], help="move every item of one project into another, which keeps its name as an alias"
    )
    project_merge.add_argument("name")
    project_merge.add_argument("--into", required=True)
    project_assign = project_commands.add_parser("assign", parents=[agent], help="put items in a project")
    project_assign.add_argument("name")
    project_assign.add_argument("--items", required=True, metavar="ID,ID")
    project_remove = project_commands.add_parser("remove", parents=[agent], help="remove a project no item is in")
    project_remove.add_argument("name")

    signin = commands.add_parser(
        "signin", help=f"connector sign-ins that {help_user} finishes on a card (SPEC SESSION-25)",
    )
    signin_commands = signin.add_subparsers(dest="action", required=True)
    signin_start = signin_commands.add_parser(
        "start", parents=[agent],
        help=f"sign this session's Claude account in to an MCP connector such as slack or linear: prints the"
             f" connector's tool count when its sign-in already works, else puts a card where {help_user} opens the"
             f" link, approves and pastes back the address; Cockpit finishes the sign-in and messages this session",
    )
    signin_start.add_argument("--server", required=True, metavar="NAME", help="the MCP server's name, as in /mcp")
    signin_start.add_argument("--item", required=True, metavar="KEY", help="the item the sign-in is for")
    signin_start.add_argument("--why", required=True, help=f"one line telling {help_user} what the sign-in is for")

    top = commands.add_parser(
        "top", help=f"cards above {help_user}'s Inbox for what is not a step on a task, such as a meeting that starts soon"
    )
    top_commands = top.add_subparsers(dest="action", required=True)
    top_commands.add_parser(
        "put", parents=[agent],
        help='create or change a card, JSON on stdin: {"need": "standup:2026-01-15", "title": ..., "body": ...,'
             ' "answers": ["Joining now"], "ends_at": "<ISO time>", "session_id": "<who gets the answer>"}',
    )
    top_done = top_commands.add_parser("done", parents=[agent], help="take a card down")
    top_done.add_argument("id", type=int)
    top_list = top_commands.add_parser("list", help="the cards still up, and those closed in the last two days")
    top_list.add_argument("--json", action="store_true")

    schedule = commands.add_parser(
        "schedule", help=f"prompts Cockpit starts as a new session daily or weekly, at a time in {help_user}'s time zone",
    )
    schedule_commands = schedule.add_subparsers(dest="action", required=True)
    schedule_put_help = (
        'create or change a schedule by its name, JSON on stdin: {"name": "weekly-review", "prompt": ...,'
        ' "cwd": "<checkout>", "title": "<item title; each run adds its date>", "project": "<existing project>",'
        ' "every": "weekly", "day": "monday", "at": "09:00"}; a daily one leaves out "day". A new or changed'
        " time first runs at its next slot, never at once"
    )
    schedule_commands.add_parser("put", parents=[agent], help=schedule_put_help, description=schedule_put_help)
    schedule_remove = schedule_commands.add_parser("remove", parents=[agent], help="delete a schedule")
    schedule_remove.add_argument("name")
    schedule_list = schedule_commands.add_parser("list", help="every schedule, its next run and its last run")
    schedule_list.add_argument("--json", action="store_true")

    time_parser = commands.add_parser(
        "time", help=f"one day of {help_user}'s time on Cockpit as stretches, and when they last used the page",
    )
    time_parser.add_argument("--day", metavar="YYYY-MM-DD", help="the day in the setup's time zone; today by default")
    time_parser.add_argument("--json", action="store_true")

    activity_parser = commands.add_parser(
        "activity", help=f"which apps and sites were in front on {help_user}'s Mac: one day, or now",
    )
    activity_parser.add_argument(
        "when", nargs="?", choices=["now"], help="now: what is in front this minute and whether it drifted",
    )
    activity_parser.add_argument("--day", metavar="YYYY-MM-DD", help="the day in the setup's time zone; today by default")
    activity_parser.add_argument("--json", action="store_true")

    # The commands this install's parts add (cockpit.parts).
    for command in parts.commands():
        command.add_parser(commands, agent, help_user)

    commands.add_parser(
        "inbox-settings",
        help=f"{help_user}'s Inbox settings: how their inbox is ranked, and what to check for new work",
    )

    briefing = commands.add_parser("briefing", help=f"{help_user}'s morning briefing on their team")
    briefing_commands = briefing.add_subparsers(dest="action", required=True)
    briefing_put = briefing_commands.add_parser(
        "put", parents=[agent],
        help="store the briefing Cockpit asked for today, Markdown on stdin; a second put that day replaces it",
    )
    briefing_put.add_argument("--day", required=True, help="the day Cockpit's request names, YYYY-MM-DD")
    briefing_commands.add_parser("get", help="print the newest briefing's Markdown")

    progress = commands.add_parser(
        "progress", help=f"the sweeper's list of the work in progress that matters most, beside {help_user}'s queue",
    )
    progress_commands = progress.add_subparsers(dest="action", required=True)
    progress_commands.add_parser(
        "put", parents=[agent],
        help='replace the list with a JSON list on stdin: up to 5 {"item_id": N, "line": "what it waits for"},'
             " most important first. [] clears it",
    )
    progress_commands.add_parser("get", help="print the list as the page shows it")

    sweep = commands.add_parser("sweep", help="start or finish a sweep, or ask for one")
    sweep_commands = sweep.add_subparsers(dest="action", required=True)
    sweep_request = sweep_commands.add_parser(
        "request", parents=[agent],
        help="ask the sweeper to decide priorities again; Cockpit wakes it within minutes, or after a pause of sweeps",
    )
    sweep_request.add_argument("--reason", required=True, help="one line: what changed")
    sweep_start = sweep_commands.add_parser("start", parents=[agent], help="claim the sweep; prints its id")
    sweep_start.add_argument("--reorder", action="store_true", help="a reorder: rescores only, and leaves the changes"
                             " since the last sweep for the next wake-up")
    finish = sweep_commands.add_parser("finish", parents=[agent])
    finish.add_argument("sweep_id")
    outcome = finish.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--summary", help="one line that starts with the sweep's kind: 'Full:' after a full sweep,"
                         " which read every source; 'Fill:' after a fill; 'Reorder:' after a reorder; 'Light:' otherwise. Only a 'Full:' summary"
                         " counts as the full sweep that falls due every few hours")
    outcome.add_argument("--error", help="what failed, when the sweep could not finish")

    permission = commands.add_parser(
        "permission", formatter_class=raw,
        help=f"tool calls waiting for {help_user}: hooks report them, and a session lists, withdraws or proposes"
             " rules for its own",
        epilog="report and ask read the hook's JSON on stdin. They are installed in ~/.claude/settings.json as\n"
               "the PermissionDenied and PermissionRequest hooks. list, withdraw and propose are for the session\n"
               "that made the call. `cockpit permission list --session UUID` shows its request ids.",
    )
    permission_commands = permission.add_subparsers(dest="action", required=True)
    permission_commands.add_parser(
        "report", parents=[agent], help="PermissionDenied hook: record a call auto mode blocked"
    )
    permission_commands.add_parser(
        "ask", parents=[agent],
        help=f"PermissionRequest hook: record the call and wait up to {permissions.ASK_WAIT_SECONDS} s for"
             f" {help_user}'s answer on Cockpit. Give the hook a longer timeout",
    )
    listed = permission_commands.add_parser(
        "list", help="your session's open calls with their request ids"
    )
    listed.add_argument("--session", required=True, metavar="UUID", help="your session's UUID")
    withdraw = permission_commands.add_parser(
        "withdraw", parents=[agent],
        help="take back your session's open calls once it got past them another way or no longer needs them",
    )
    withdraw.add_argument(
        "request_ids", nargs="*", type=int, metavar="ID", help="request ids, from `cockpit permission list`"
    )
    withdraw.add_argument("--session", required=True, metavar="UUID", help="your session's UUID")
    withdraw.add_argument("--all", action="store_true", help="every open call of the session, in place of IDs")
    propose = permission_commands.add_parser(
        "propose", parents=[agent],
        help="propose the auto-mode sentence that would allow your session's call. Make it firm and general:"
             f" \"Sessions may <the kind of work> in <where>.\" {help_user} sees it first among the suggestions",
    )
    propose.add_argument("request_id", type=int, metavar="ID")
    propose.add_argument("--session", required=True, metavar="UUID", help="your session's UUID")
    # An option, not a positional: argparse leaves a positional after `ID --session UUID` unparsed.
    propose.add_argument("--sentence", metavar="TEXT", help="the sentence, one line; left out, it is read from stdin")

    args = parser.parse_args(argv)
    try:
        output = run(args)
    except CockpitError as exc:
        print(f"cockpit: {exc}", file=sys.stderr)
        return exc.exit_code
    if output is not None:
        print(output if isinstance(output, str) else json.dumps(output, indent=2))
    return 0


def _held(result: dict[str, Any]) -> Any:
    """Say in words when a write did not apply as sent: a write to the step the user is on waits for its timer
    to stop; an agent's close of an item became a finish step; an agent's close of a handed finish step returned
    it to the user (SPEC CONTROL-17)."""
    if result.get("held"):
        return f"waits: {result['held']}"
    if result.get("merged"):
        return (
            f"closed: item {result['id']} is at stage done because {result['merged']} merged. {user_name()} gets no"
            " finish step, only a line on it above their next step."
        )
    if result.get("alone"):
        return (
            f"closed: item {result['id']} is a fire-and-forget to-do, now at stage done. {user_name()} gets no finish"
            " step, only your note in a line above their next step."
        )
    if result.get("finish_step"):
        return (
            f"asks: {user_name()} finishes item {result['id']} on step {result['finish_step']}. The item stays open"
            f" until {user_name()} presses Task done or Close there."
        )
    if result.get("returned"):
        return f"returned: {result['returned']}"
    return result


def _merged(result: dict[str, Any]) -> str:
    """What an item merge did, and the next move it leaves: one card on the joined item that covers both."""
    if result["held"]:
        return f"waits: {result['held']} The merge applies then."
    merged, main = result["merged"], result["item"]
    lines = [f"merged: item {merged['from']} into item {main['id']} ({main['title']}); {len(merged['steps'])} steps moved."]
    for step in result["open_steps"]:
        state = "handed to an agent" if step["handoff_message_id"] is not None else "open"
        lines.append(f"  step {step['id']} ({step['need']}, {state}): {step['ask']}")
    lines.append(
        f"Next: one card on item {main['id']} covers both tasks. Rewrite its open card, or put one if it has none,"
        f" when {user_name()} is not on it."
    )
    return "\n".join(lines)


def _session_account() -> str:
    """The Claude account this session runs on: the one whose folder CLAUDE_CONFIG_DIR names, else the default."""
    folder = os.environ.get("CLAUDE_CONFIG_DIR")
    if not folder:
        return next(account.name for account in config.ACCOUNTS if account.default)
    found = next((each for each in config.ACCOUNTS if each.folder.resolve() == Path(folder).expanduser().resolve()), None)
    if found is None:
        raise CockpitError(f"CLAUDE_CONFIG_DIR {folder} is none of Cockpit's Claude account folders")
    return found.name


def signin_start(args: argparse.Namespace) -> str:
    session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not session:
        raise CockpitError("cockpit signin start runs from a Claude Code session: CLAUDE_CODE_SESSION_ID is not set")
    account = _session_account()
    result = call("POST", "/api/signins", {
        "account": account, "server": args.server, "item_key": args.item, "why": args.why, "session_id": session,
    }, args.agent)
    if "working" in result:
        state = result["working"]
        return (
            f"working: the {account} account's {args.server} sign-in lists {state['tools']} tools. If this session's"
            f" {args.server} tools still say they need a sign-in, `/mcp` reconnects them."
        )
    step = result["step"]
    again = " (the card was already up)" if result.get("reused") else ""
    return (
        f"asks: {user_name()} signs in on step {step['id']}{again}, through {step['signin']['host']}. Cockpit"
        f" messages this session when {args.server} works; end your turn until then."
    )


def run(args: argparse.Namespace) -> Any:
    if args.command == "get":
        state = call("GET", "/api/state", None, None)
        return lean(state) if args.json else summary(state)
    if args.command == "item":
        if args.action == "get":
            return call("GET", "/api/items?" + urllib.parse.urlencode({"key": args.key}), None, None)
        if args.action == "merge":
            return _merged(call("POST", "/api/items/merge", {"side": args.side, "into": args.into, "note": args.note}, args.agent))
        return _held(call("PUT", "/api/items", _stdin_json(), args.agent))
    if args.command == "step":
        if args.action == "put":
            result = call("POST", "/api/steps", _stdin_json(), args.agent)
            if result["skipped"]:
                print(json.dumps(result, indent=2))
                raise StepSkipped(result["skipped"])
            if result["hint"]:
                print(f"hint: {result['hint']}", file=sys.stderr)
            return _held(result)
        if args.action == "checked":
            return call("POST", f"/api/steps/{args.step_id}/checked", {"note": args.note}, args.agent)
        note = getattr(args, "note", None)
        return _held(call("POST", f"/api/steps/{args.step_id}/{args.action}", {"note": note} if note else {}, args.agent))
    if args.command == "signin":
        return signin_start(args)
    if args.command == "goals":
        return goals_text(call("GET", "/api/goals", None, None))
    if args.command == "image":
        return image_command(args)
    if args.command == "page":
        return page_command(args)
    if args.command == "message":
        if args.action == "dismiss":
            message = call("POST", f"/api/messages/{args.message_id}/dismiss", {"reason": args.reason}, args.agent)
            if message["detail"].startswith("Not sent:"):
                return f"dismissed message {message['id']}; it will not be sent"
            return f"dismissed message {message['id']}; it no longer counts as failed"
        return wait_for_delivery(call("POST", f"/api/messages/{args.message_id}/retry", None, args.agent))
    if args.command == "session" and args.action == "move":
        return call("POST", f"/api/sessions/{args.session_id}/move", {"account": args.to}, args.agent)["detail"]
    if args.command == "session":
        return call("POST", f"/api/sessions/{args.session_id}/close", {"reason": args.reason}, args.agent)["detail"]
    if args.command == "send":
        if args.provider and not args.new_in:
            raise CockpitError("--provider needs --new-in")
        if args.step is not None and args.item is None:
            raise CockpitError("--step needs --item")
        if args.item is None and args.session is None and args.new_in is None:
            raise CockpitError("say where the message goes: --item, --session or --new-in")
        text = sys.stdin.read() if args.text == "-" else args.text
        target = {"session_id": args.session, "item_id": args.item, "step_id": args.step, "new_cwd": args.new_in}
        if args.provider:
            target["provider"] = args.provider
        body: dict[str, Any] = {"text": text, "target": target}
        # The receiver is told how to reply to this session (SPEC MSG-12): a Claude Code session, or a Codex
        # thread, whose id is its session id in Cockpit (codex.py).
        sender = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID")
        if sender:
            body["from_session"] = sender
        message = call("POST", "/api/messages", body, args.agent)
        return wait_for_delivery(message)
    if args.command == "feedback":
        if (args.mark_read is None) != (args.through is None and args.ids is None):
            raise CockpitError("--mark-read goes with --through or --ids")
        entries = call("GET", f"/api/feedback?unread={'false' if args.all else 'true'}", None, None)
        if args.mark_read and args.ids is not None:
            wanted = _id_list(args.ids)
            unread = {entry["id"] for entry in entries}
            if not wanted <= unread:
                raise CockpitError(f"not unread feedback: {', '.join(map(str, sorted(wanted - unread)))}")
            call("POST", "/api/feedback/read", {"ids": sorted(wanted)}, args.mark_read)
            return f"marked {len(wanted)} read; {len(unread - wanted)} others stay unread"
        if args.mark_read:
            ids = [entry["id"] for entry in entries if entry["id"] <= args.through]
            if ids:
                call("POST", "/api/feedback/read", {"ids": ids}, args.mark_read)
            return f"marked {len(ids)} read; {len(entries) - len(ids)} newer stay unread"
        return entries
    if args.command == "attach":
        return attach_command(args.session)
    if args.command == "spec":
        return spec_command(args)
    if args.command == "patterns":
        report = call("GET", f"/api/patterns?days={args.days}", None, None)
        return report if args.json else patterns_text(report)
    if args.command == "ui-usage":
        report = call("GET", f"/api/control-uses?days={args.days}", None, None)
        return report if args.json else control_uses_text(report)
    if args.command == "logs":
        report = call("GET", f"/api/server-logs?days={args.days}", None, None)
        return report if args.json else server_logs_text(report)
    if args.command == "requests":
        report = call("GET", f"/api/request-stats?days={args.days}", None, None)
        return report if args.json else request_stats_text(report)
    if args.command == "presses":
        query = f"days={args.days}" + (f"&item={args.item}" if args.item is not None else "")
        report = call("GET", f"/api/page-presses?{query}", None, None)
        return report if args.json else presses_text(report)
    if args.command == "flashes":
        return flashes_text(call("GET", f"/api/flashes?days={args.days}", None, None)["flashes"])
    if args.command == "errors":
        if args.action == "take":
            taken = call("POST", "/api/page-errors/take", {"ids": sorted(_id_list(args.ids)), "note": args.note}, args.agent)
            return f"took {taken['taken']} errors"
        return page_errors_text(call("GET", f"/api/page-errors?all={'true' if args.all else 'false'}", None, None))
    if args.command == "writing":
        if args.action == "get":
            return call("GET", f"/api/writing/{args.example_id}", None, None)["text"]
        examples = call("GET", "/api/writing", None, None)
        if args.json:
            return [example | {"transcript": transcript_path(example["session_id"]) if example["session_id"] else ""}
                    for example in examples]
        return writing_text(examples)
    if args.command == "briefing":
        return briefing_command(args)
    if args.command == "project":
        return project_command(args)
    if args.command == "top":
        return top_command(args)
    if args.command == "time":
        return time_command(args)
    if args.command == "activity":
        return activity_command(args)
    for command in parts.commands():
        if args.command == command.name:
            return command.run(args, call)
    if args.command == "schedule":
        return schedule_command(args)
    if args.command == "inbox-settings":
        return call("GET", "/api/inbox-settings/text", None, None)["text"]
    if args.command == "progress":
        if args.action == "put":
            entries = call("PUT", "/api/in-progress", {"entries": _stdin_json()}, args.agent)["entries"]
            return f"stored the list ({len(entries)} of at most {store.IN_PROGRESS_MAX}); the page shows it under Matters most, at the top of All work"
        state = call("GET", "/api/state", None, None)
        titles = {item["id"]: item["title"] for item in state["items"]}
        return "\n".join(f"{entry['item_id']} {titles[entry['item_id']]}: {entry['line']}" for entry in state["in_progress"]) or "empty"
    if args.command == "sweep":
        if args.action == "request":
            call("POST", "/api/sweeps/request", {"reason": args.reason}, args.agent)
            return "requested; Cockpit wakes the sweeper once changes have been quiet for a minute, and waits while sweeps are paused"
        if args.action == "start":
            return call("POST", "/api/sweeps/start", {"reorder": args.reorder}, args.agent)["id"]
        body = {"sweep_id": args.sweep_id, "summary": args.summary or "", "error": args.error or ""}
        finished = call("POST", "/api/sweeps/finish", body, args.agent)
        if finished["warning"]:
            print(f"cockpit: warning: {finished['warning']}", file=sys.stderr)
        return finished
    if args.command == "permission":
        if args.action == "report":
            return permission_report(_stdin_json(), args.agent)
        if args.action == "ask":
            return permission_ask(_stdin_json(), args.agent)
        if args.action == "list":
            return permission_list(call("GET", "/api/state", None, None), args.session)
        if args.action == "withdraw":
            return permission_withdraw(args.request_ids, args.all, args.session, args.agent)
        sentence = sys.stdin.read() if args.sentence is None else args.sentence
        return permission_propose(args.request_id, args.session, sentence, args.agent)
    raise CockpitError(f"unknown command {args.command}")


def top_command(args: argparse.Namespace) -> Any:
    if args.action == "put":
        return call("POST", "/api/top-cards", _stdin_json(), args.agent)
    if args.action == "done":
        return call("POST", f"/api/top-cards/{args.id}/done", {}, args.agent)
    cards = call("GET", "/api/top-cards", None, None)
    if args.json:
        return cards
    return "\n".join(
        f"{card['id']} {card['need']}: {card['title']}"
        + (f" (closed {card['done_at']} by {card['done_by']}" + (f", answered {card['answer']}" if card["answer"] else "") + ")"
           if card["done_at"] else "")
        for card in cards
    ) or "no top cards"


def schedule_command(args: argparse.Namespace) -> Any:
    if args.action == "put":
        return call("POST", "/api/schedules", _stdin_json(), args.agent)
    if args.action == "remove":
        return call("DELETE", f"/api/schedules/{urllib.parse.quote(args.name)}", None, args.agent)
    found = call("GET", "/api/schedules", None, None)
    if args.json:
        return found
    lines = []
    for each in found:
        when = f"daily {each['at']}" if each["every"] == "daily" else f"{each['day']} {each['at']}"
        lines.append(
            f"{each['id']} {each['name']}: {when} in {each['cwd']}, "
            + ("paused" if each["paused"] else f"next {each['next_at']}")
        )
        run = each["last_run"]
        if run:
            lines.append(
                f"  last run: item {run['item_id']}, message {run['message_id']} {run['state']}"
                + (f" ({run['failure']})" if run["failure"] else "")
            )
        if each["error"]:
            lines.append(f"  could not start: {each['error']}")
    return "\n".join(lines) or "no schedules"


def time_command(args: argparse.Namespace) -> Any:
    query = urllib.parse.urlencode({"day": args.day} if args.day else {})
    found = call("GET", f"/api/time/stretches?{query}", None, None)
    if args.json:
        return found
    lines = [f"{found['day']}: {found['minutes']:g} minutes, last input {found['last_input'] or 'never'}"]
    lines += [
        f"  {row['start']} {row['minutes']:g} min {row['source']}" + (f": {row['item']}" if row["item"] else "")
        for row in found["stretches"]
    ]
    return "\n".join(lines)


def _mac_line(mac: dict[str, Any]) -> str:
    if not mac["connected"]:
        return f"no Mac connected; {user_name()} connects one under Your time on Cockpit"
    return ("paused, " if mac["paused"] else "") + f"last heard from {mac['last_seen'] or 'never'}"


def activity_command(args: argparse.Namespace) -> Any:
    if args.when == "now":
        if args.day:
            raise CockpitError("--day is for a day's summary; `cockpit activity now` is this minute")
        found = call("GET", "/api/activity/now", None, None)
        if args.json:
            return found
        current = found["current"]
        lines = [_mac_line(found["mac"])]
        if current is None:
            lines.append("nothing recorded in the last few minutes")
        else:
            label = "away" if current["away"] else current["site"] or current["app"]
            lines.append(f"now ({current['minute']}): {label}, {current['category']}")
        recent = ", ".join(f"{count} {kind}" for kind, count in found["last_minutes"].items())
        lines.append(f"last minutes: {recent or 'none'}" + ("; drifting to distraction" if found["drifting"] else ""))
        return "\n".join(lines)
    query = urllib.parse.urlencode({"day": args.day} if args.day else {})
    found = call("GET", f"/api/activity/day?{query}", None, None)
    if args.json:
        return found
    categories = ", ".join(f"{minutes} {kind}" for kind, minutes in found["categories"].items() if minutes)
    lines = [
        f"{found['day']}: {found['minutes']} minutes at the Mac ({categories or 'none'}), {found['away']} away; "
        + _mac_line(found["mac"]),
    ]
    if found["outside_tasks"] is not None:
        lines.append(f"  {found['outside_tasks']} of those minutes outside any task's timer")
    lines += [f"  {row['minutes']} min {row['label']} ({row['category']})" for row in found["labels"]]
    return "\n".join(lines)


def project_command(args: argparse.Namespace) -> Any:
    if args.action == "put":
        return call("PUT", "/api/projects", _stdin_json(), args.agent)
    if args.action == "merge":
        return call("POST", f"/api/projects/{urllib.parse.quote(args.name, safe='')}/merge", {"into": args.into}, args.agent)
    if args.action == "assign":
        body = {"items": sorted(_id_list(args.items))}
        return call("POST", f"/api/projects/{urllib.parse.quote(args.name, safe='')}/assign", body, args.agent)
    if args.action == "remove":
        return call("DELETE", f"/api/projects/{urllib.parse.quote(args.name, safe='')}", None, args.agent)
    found = call("GET", "/api/projects", None, None)
    if args.json:
        return found
    return "\n".join(
        f"{project['name']}: {project['open_items']} open"
        + ("" if project["has_logo"] or project["name"] == store.COCKPIT_PROJECT else ", no logo yet")
        + (f" (also called {', '.join(project['aliases'])})" if project["aliases"] else "")
        + (f"\n  {project['about']}" if project["about"] else "")
        + (f"\n  new sessions start in {project['checkout']}" if project["checkout"] else "")
        for project in found
    ) or "no projects yet"


def briefing_command(args: argparse.Namespace) -> str:
    if args.action == "put":
        stored = call("POST", "/api/briefings", {"day": args.day, "body": sys.stdin.read()}, args.agent)
        return (
            f"stored the briefing for {stored['day']} ({len(stored['body'])} characters);"
            f" {user_name()} reads it on Cockpit's Briefing tab"
        )
    newest = call("GET", "/api/briefings", None, None)["briefings"]
    if not newest:
        raise CockpitError("no briefing yet")
    shown = call("GET", f"/api/briefings/{newest[0]['id']}", None, None)
    return f"Briefing for {shown['day']}, covering {shown['since']} to {shown['until']}:\n\n{shown['body']}"


def attach_command(session_id: str) -> None:
    if "codex" in config.setup().providers:
        state = call("GET", "/api/state", None, None)
        codex_session = next((s for s in state["sessions"] if s.get("provider") == "codex"
                              and session_id in {s["id"], s["session_id"]}), None)
        if codex_session is not None:
            raise CockpitError("This Codex thread is controlled on Cockpit. Use its message box and approval cards."
                               " A second live controller can duplicate work.")
    found, failed = sessions.list_sessions(config.ACCOUNTS, moves.moved(config.DB_PATH), [])
    listed = next((session for session in found if session.id == session_id), None)
    if listed is None and failed:
        raise CockpitError(f"listing Claude sessions failed: {'; '.join(failed)}")
    if listed is None and any(session.id == session_id for session in removals.listed(config.DB_PATH)):
        raise CockpitError(f"Cockpit took session {session_id} off `claude agents` once its tasks closed; send it a"
                           " message on Cockpit to resume it, then open it")
    if listed is None:
        raise CockpitError(f"no session {session_id} on this machine; `claude agents --all` lists them")
    try:
        attach.attach(config.account(listed.account), listed.id, listed.name or listed.id, config.setup().checkouts)
    except attach.AttachError as exc:
        raise CockpitError(str(exc)) from exc


if __name__ == "__main__":
    sys.exit(main())
