"""A snooze time the user typed in their own words (SPEC VIEW-29): "Friday morning", "after lunch", "in 40 minutes".
One short run with an enabled provider reads the words as a time in the user's timezone; the page then snoozes the
step until that time as it does for the fixed choices. Reading the words changes nothing, so a reading the user does
not want costs only the run."""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from cockpit import accounts, codex_agent, config, sessions
from cockpit.config import Account

log = logging.getLogger(__name__)

# A quick model: the answer is one time, and the user waits for it in the dialog.
MODEL = "claude-haiku-5-5"
TIMEOUT_SECONDS = 60
# The furthest a snooze may reach: further than this is more likely a misreading than a wish.
LONGEST = timedelta(days=366)
WORK_DIR = config.DATA_DIR / "snooze-words"

SCHEMA = {
    "type": "object",
    "properties": {
        "at": {
            "type": "string",
            "description": "The time the words name, as YYYY-MM-DDTHH:MM in the user's timezone; empty when the words name no time.",
        },
        "unclear": {
            "type": "string",
            "description": "When `at` is empty: in at most ten plain words, what the words lack, such as \"no day or time named\".",
        },
    },
    "required": ["at", "unclear"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You turn a few words into one point in time. The user is snoozing a task on their work page and \
typed when it should come back. Read the words as a person would: "tomorrow" alone means 9:00 tomorrow; "morning" \
9:00, "lunch" 12:00, "afternoon" 14:00, "evening" 18:00, "tonight" 20:00; a weekday alone means its next \
occurrence at 9:00, and today's weekday means a week from today; "next week" means next Monday 9:00; "in 40 \
minutes" counts from now. The time must lie after now. When the words name no time, leave `at` empty and say \
briefly in `unclear` what is missing."""


class Unread(Exception):
    """The words gave no time: the run failed, or the words name none. Its text says why in a few words, for the
    dialog."""


def read(text: str, zone: ZoneInfo, now: datetime) -> datetime:
    """The moment `text` names, in UTC, read by an enabled provider."""
    local = now.astimezone(zone)
    prompt = f"Now: {local:%A %d %B %Y, %H:%M} ({zone.key}).\nThe words: {text}"
    if "claude" not in config.setup().providers:
        return moment(ask_codex(prompt), zone, now)
    chosen = accounts.usable(accounts.in_use(config.ACCOUNTS, []), now)
    if not chosen:
        raise Unread("every Claude account is signed out")
    failures: list[str] = []
    for account in chosen:
        try:
            answer = ask(account, prompt)
        except Unread as exc:
            failures.append(str(exc))
            continue
        return moment(answer, zone, now)
    raise Unread(failures[-1])


def moment(answer: dict[str, str], zone: ZoneInfo, now: datetime) -> datetime:
    """The model's answer as a UTC time after `now` and within LONGEST of it."""
    if not answer["at"]:
        raise Unread(answer["unclear"] or "the words name no time")
    try:
        wall = datetime.strptime(answer["at"], "%Y-%m-%dT%H:%M")
        # In the hour the clocks go back, the wall time comes twice: the first that is still ahead counts.
        times = sorted({wall.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1)})
    except (ValueError, OverflowError) as exc:
        raise Unread("the answer was not a time") from exc
    # In the hour the clocks go forward, the wall time never comes.
    if all(at.astimezone(zone).replace(tzinfo=None) != wall for at in times):
        raise Unread("the clocks skip that time that day")
    at = next((one for one in times if one > now), times[-1])
    if at <= now:
        raise Unread("that time has passed")
    if at - now > LONGEST:
        raise Unread("that is more than a year away")
    return at


def ask(account: Account, prompt: str) -> dict[str, str]:
    """One `claude -p` run on `account` with no tools, no settings files and no MCP servers."""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    args = [
        "claude", "-p", "--model", MODEL, "--tools", "", "--setting-sources", "", "--strict-mcp-config",
        "--no-session-persistence", "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
        "--system-prompt", SYSTEM_PROMPT,
    ]
    try:
        done = subprocess.run(
            sessions.claude_command(account, args), input=prompt, capture_output=True, text=True,
            timeout=TIMEOUT_SECONDS, cwd=WORK_DIR, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        log.error("%s: reading snooze words gave no answer within %d s", account.name, TIMEOUT_SECONDS)
        raise Unread("Claude gave no answer in time") from exc
    try:
        result = json.loads(done.stdout)
    except ValueError:
        result = None
    answer = result.get("structured_output") if isinstance(result, dict) else None
    if done.returncode != 0 or not isinstance(answer, dict) or not all(isinstance(answer.get(f), str) for f in ("at", "unclear")):
        log.error("%s: reading snooze words failed (exit %d): %s", account.name, done.returncode, (done.stderr or done.stdout)[-400:])
        raise Unread(f"Claude's run on the {account.name} account failed")
    return {"at": answer["at"].strip(), "unclear": answer["unclear"].strip()}


def ask_codex(prompt: str) -> dict[str, str]:
    """One ephemeral, read-only app-server thread using the existing Codex sign-in and model."""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    events: queue.Queue = queue.Queue()
    rpc = codex_agent.AppServer(config.setup().codex_command, events.put)
    try:
        thread = rpc.request("thread/start", {
            "cwd": str(WORK_DIR), "ephemeral": True, "sandbox": "read-only", "approvalPolicy": "never",
            "baseInstructions": SYSTEM_PROMPT + " Answer directly from the supplied words. Use no tools.",
            "config": {"web_search": "disabled"},
        })["thread"]["id"]
        rpc.request("turn/start", {"threadId": thread, "input": [{"type": "text", "text": prompt}],
                                   "outputSchema": SCHEMA})
        answer = ""
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while True:
            event = events.get(timeout=max(0, deadline - time.monotonic()))
            method, params = event.get("method"), event.get("params", {})
            if method == "cockpit/disconnected":
                raise codex_agent.CodexError("Codex disconnected")
            if params.get("threadId") != thread:
                continue
            item = params.get("item", {})
            if method == "item/completed" and item.get("type") == "agentMessage":
                answer = item.get("text", "")
            if method == "turn/completed":
                turn = params.get("turn", {})
                if turn.get("status") != "completed":
                    raise codex_agent.CodexError(str(turn.get("error") or "Codex did not complete the answer"))
                result = json.loads(answer)
                if not isinstance(result, dict) or not all(isinstance(result.get(f), str) for f in ("at", "unclear")):
                    raise ValueError("Codex returned no time fields")
                return {f: result[f].strip() for f in ("at", "unclear")}
    except (codex_agent.CodexError, queue.Empty, ValueError, KeyError) as exc:
        log.error("reading snooze words with Codex failed: %s", exc)
        raise Unread("Codex could not read that time; try again") from exc
    finally:
        rpc.close()
