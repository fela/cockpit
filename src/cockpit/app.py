"""Cockpit's HTTP server: the page, the JSON API, and the Google sign-in."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import html
import logging
import re
import secrets
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import AsyncGenerator, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import BackgroundTasks, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from cockpit import (
    accounts,
    actions,
    auth,
    briefing,
    codex,
    codex_agent,
    codex_login,
    commands,
    config,
    control_uses,
    db,
    delivery,
    emergency,
    flashes,
    history,
    inbox_settings,
    logins,
    machine,
    page_errors,
    patterns,
    permissions,
    projects,
    rank,
    render,
    secret_files,
    sidechat,
    snooze_words,
    spec,
    spend,
    stages,
    store,
    sweeper,
    timesheet,
    usage,
)
from cockpit.codex_agent import CodexError, ProviderSessions
from cockpit.config import Account, user_name
from cockpit.sessions import (
    Session,
    SessionCache,
    calls_before,
    last_instruction,
    running,
    session_log,
)

log = logging.getLogger(__name__)

# The page's script and stylesheet hold no data, so /static/ is public; the
# sign-in error pages use the stylesheet before anyone is signed in.
PACKAGE_DIR = Path(__file__).parent
STATIC_DIR = PACKAGE_DIR / "static"
AGENT_NAME = re.compile(r"[a-z0-9-]{1,32}")
# The main page names its stylesheets and scripts with a tag of this server process (`?v=`), so the browser keeps
# them until the next restart and a reload asks Cockpit for no file it already has (SPEC UI-39).
STATIC_REFERENCE = re.compile(r'((?:href|src)="/static/[^"?]+)"')
STATIC_KEPT = "public, max-age=31536000, immutable"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
PUBLIC_PATHS = frozenset({"/healthz", auth.SIGN_IN_PATH, auth.CALLBACK_PATH})
# touched_at and pinged_at may run this far ahead of the server's clock.
CLOCK_SKEW_MINUTES = 5
# The spec and skills pages show read comments this long after the user wrote them; unread ones always.
SPEC_COMMENT_DAYS = 14
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
# A page a session shares (SPEC API-3) is HTML an agent wrote, so it runs sandboxed in an origin of its own:
# its scripts reach neither the user's sign-in cookie nor Cockpit's API, and fetch nothing beyond the script,
# style and font hosts below. A link that opens a new tab opens it outside the sandbox.
PAGE_CSP = (
    "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox allow-downloads; default-src 'none'; "
    "script-src 'unsafe-inline' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://unpkg.com; "
    "style-src 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
    "font-src https://fonts.gstatic.com data:; img-src data: blob:; media-src data: blob:; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
# The same page drawn as a figure in a step (SPEC NEXT-17): the same sandbox, framed by Cockpit's own page only.
FIGURE_CSP = PAGE_CSP.replace("frame-ancestors 'none'", "frame-ancestors 'self'")
# Added to a figure's page: it tells Cockpit's page how tall the figure is, so the frame fits it (app.js).
# It measures the content itself, from the page's top to the bottom of the body's contents plus the body's
# padding, border and margin below them, so a body as tall as the frame (min-height: 100vh) does not grow it.
# Content can grow inside a body of fixed height without resizing it, so it also measures twice a second. While
# Cockpit sets the frame aside during a redraw, nothing in it is laid out and its content box is empty; it is not
# measured then.
FIGURE_SIZE_SCRIPT = (
    "<script>(() => { let sent = 0; const send = () => { const body = document.body; if (!body) return; "
    "const range = document.createRange(); range.selectNodeContents(body); const box = range.getBoundingClientRect(); "
    "if (!box.width && !box.height) return; const style = getComputedStyle(body); "
    "const height = Math.ceil(box.bottom + scrollY + parseFloat(style.paddingBottom) "
    "+ parseFloat(style.borderBottomWidth) + parseFloat(style.marginBottom)); "
    "if (height !== sent) { sent = height; parent.postMessage({ cockpitFigure: height }, '*'); } }; "
    "addEventListener('load', () => { send(); new ResizeObserver(send).observe(document.body); setInterval(send, 500); }); "
    "})();</script>"
)

# Added to a figure's page: a link opens in a new tab, as a link in a step does. In a frame a plain link would load
# its address inside the frame, which most sites refuse, and the page would be gone from the step or the Pages
# view. A link within the page (`#below`) still moves within it, without a history entry, so the browser's Back
# closes a page open over Cockpit (SPEC NEXT-47) in one press; a download still downloads, and a page's own
# script that handles a click first keeps it. A link in an SVG chart (`href` or `xlink:href`) opens too.
FIGURE_LINK_SCRIPT = (
    "<script>addEventListener('click', (event) => { const link = event.target.closest && event.target.closest('a[*|href]'); "
    "if (!link || event.defaultPrevented || link.hasAttribute('download')) return; "
    "const href = link.getAttribute('href') || link.getAttributeNS('http://www.w3.org/1999/xlink', 'href') || ''; "
    "if (/^\\s*javascript:/i.test(href)) return; "
    "if (href.startsWith('#')) { event.preventDefault(); let id = href.slice(1); try { id = decodeURIComponent(id); } catch {} "
    "const target = id && (document.getElementById(id) || document.getElementsByName(id)[0]); "
    "if (target) target.scrollIntoView(); else if (!id || id === 'top') scrollTo(0, 0); return; } "
    "link.setAttribute('target', '_blank'); link.setAttribute('rel', 'noopener'); });</script>"
)

# Added to every page Cockpit frames (SPEC NEXT-48): it reports the user's choices in the page's named form controls
# and sets the controls to the choices saved before. Inline for the same reason as the comment script below.
PAGE_CHOICES_SCRIPT = f"<script>{(STATIC_DIR / 'page-choices-frame.js').read_text()}</script>"

# Added to a page Cockpit shows for comments (SPEC NEXT-38): it reports what the user selects or picks and draws
# the marks. The page's policy allows no script from Cockpit's own address, so it goes inline.
PAGE_FRAME_SCRIPT = f"<script>{(STATIC_DIR / 'page-frame.js').read_text()}</script>"
# The first press and key listeners on a page shown for comments: they hand each press, and each key down and up, to
# page-frame.js's picker, which then runs before any listener the page's own scripts add, so a pick stops the page
# acting on the press or the key. The
# picker registers once through `window.__cockpitPress`, which the page's scripts can neither replace nor reuse.
PAGE_PICK_GUARD_SCRIPT = (
    "<script>(()=>{let call=null;"
    'Object.defineProperty(window,"__cockpitPress",{value:(picker)=>{call=call||picker;}});'
    'for(const type of ["pointerdown","mousedown","pointerup","mouseup","click","dblclick","touchstart","touchend","keydown","keyup"])'
    "addEventListener(type,(event)=>{if(call)call(type,event);},true);})()</script>"
)
# What may come before a page's own content: a byte order mark, comments, white space, the doctype, and the html
# and head tags, whose quoted attribute values may hold a `>`.
_TAG_REST = r"""(?:[^>"']|"[^"]*"|'[^']*')*>"""
_PREAMBLE = re.compile(rf"\A﻿?(?:\s|<!--.*?-->)*(?:<!doctype{_TAG_REST})?(?:\s|<!--.*?-->)*"
                       rf"(?:<html\b{_TAG_REST})?(?:\s|<!--.*?-->)*(?:<head\b{_TAG_REST})?", re.IGNORECASE | re.DOTALL)


def with_pick_guard(page_html: str) -> str:
    """The page with PAGE_PICK_GUARD_SCRIPT at the start of its head: after the doctype, so the page keeps its mode,
    and after the html and head tags, so they keep their attributes."""
    at = _PREAMBLE.match(page_html).end()  # ty: ignore[unresolved-attribute]  # The preamble also matches an empty string.
    return page_html[:at] + PAGE_PICK_GUARD_SCRIPT + page_html[at:]


Exchange = Callable[[auth.GoogleClient, str, str, str], dict[str, Any]]


@dataclass(frozen=True)
class Settings:
    db_path: Path
    token: str
    cookie_key: bytes
    google: auth.GoogleClient | None
    # The one Google account that may sign in on the page.
    email: str
    origin: str
    checkouts_dir: Path
    sweep_cwd: Path
    sweep_prompt: str
    spec_repo: Path
    # The branch of spec_repo a spec edit pulls first and pushes to: the deploy branch; "" commits only.
    spec_branch: str
    # The checkout of the user's skills the skills page edits: a folder each in skills_folder, on skills_branch.
    skills_repo: Path
    skills_folder: str
    skills_branch: str
    # The user's Claude Code settings, whose allow lists the page reads and changes.
    claude_settings: Path
    # The Claude rate limits and account the user's status line saves (usage.py).
    claude_rate_limits: Path
    # The Claude accounts sessions run on (config.ACCOUNTS, accounts.py); usage.read labels records by them.
    claude_accounts: tuple[Account, ...]
    # The command that runs Claude Code, which the page signs an account in again with (logins.py).
    claude: list[str]
    # The command that runs Codex, which the page signs an account in with (codex_login.py).
    codex: str
    # The command the page's terminal link runs to attach to a session, with {session} for its id.
    attach: str
    exchange: Exchange
    # Where the secrets the user hands sessions go, a file each (secret_files.py, SPEC MSG-15).
    secrets_dir: Path
    auth_mode: str = "google"
    local_login_token: str = ""
    providers: tuple[str, ...] = ("claude",)
    default_provider: str = "claude"


# Request bodies. Fields left out of an upsert keep their stored value.
SessionId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]
Url = Annotated[str, StringConstraints(pattern=r"^https?://\S+$", max_length=2000)]
Text = Annotated[str, StringConstraints(max_length=20000)]
Line = Annotated[str, StringConstraints(max_length=300)]
# A model, effort or account name as config.SESSION_OPTIONS and config.ACCOUNTS spell them.
OptionName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9.-]{0,63}$")]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Fields where an explicit null means something. Null anywhere else is an
    # error: a left-out field keeps its value, and a null must not reach a list
    # or text column.
    nullable: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="before")
    @classmethod
    def _no_nulls(cls, data: Any) -> Any:
        if isinstance(data, dict):
            nulls = sorted(name for name, value in data.items() if value is None and name not in cls.nullable)
            if nulls:
                raise ValueError(f"null is not allowed for {', '.join(nulls)}; leave a field out to keep its value")
        return data


class Link(Body):
    label: Line
    url: Url


def _utc_time(value: str | None) -> str | None:
    """An ISO time with an explicit offset, stored as UTC `...Z` so stored times compare as strings."""
    if value is None:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("expected an ISO time like 2026-01-15T10:00:00Z") from exc
    if moment.tzinfo is None:
        raise ValueError("the time needs a UTC offset, e.g. 2026-01-15T10:00:00Z")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _past_time(value: str | None) -> str | None:
    value = _utc_time(value)
    if value is not None and rank.parse_time(value) > datetime.now(UTC) + timedelta(minutes=CLOCK_SKEW_MINUTES):  # ty: ignore[unsupported-operator]
        raise ValueError("the time is in the future")
    return value


class WaitCheck(Body):
    """A check Cockpit runs every wakes.CHECK_SECONDS on a wait; the item's session wakes once it holds (SPEC STATE-8).
    `reached`: every pull request in `pulls` reached `stage` (stages.reached), cherry-picks included."""

    kind: Literal["reached"]
    stage: Literal["dev", "staging", "prod"]
    pulls: Annotated[list[Annotated[str, StringConstraints(pattern=stages.PULL_URL.pattern)]], Field(min_length=1, max_length=20)]


class Waiting(Body):
    kind: Literal["agent", "bot", "person", "release"]
    who: Line
    # On an agent: when its work picks up again by itself (a timer it set, a run that ends then). Until then the work
    # rests rather than stalls (SPEC SWEEP-20). On a bot, person or release, which an agent's put must give: when
    # Cockpit wakes the item's session to look again, whatever `check` says (SPEC STATE-8).
    until: str | None = None
    # On a bot, person or release: what Cockpit checks for the item's session in the meantime (SPEC STATE-8).
    check: WaitCheck | None = None
    # What the work waits for them to do, in a word or two ("review", "answer"); the row's hover line says it
    # before the name (SPEC VIEW-27).
    what: Annotated[str, StringConstraints(max_length=20)] | None = None
    # With `until` on an agent: why the work rests until then, in a few words ("check the reminder after lunch"); the
    # row's hover line in Waiting says it.
    why: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=store.WHY_CHARS, pattern=r"^[^\n]*$")] | None = None
    # With `until` on an agent: the user chose the time, such as a reminder they asked for, so the work sits in
    # Waiting's Snoozed by you rather than under Agent resumes later.
    by_user: bool | None = None

    _until = field_validator("until")(_utc_time)

    @model_validator(mode="after")
    def _until_fits_the_kind(self) -> Waiting:
        agent = self.kind == "agent"
        if self.check is not None and agent:
            raise ValueError("check goes on a bot, person or release; an agent's own session picks its work up again")
        if (self.why is not None or self.by_user is not None) and not (agent and self.until is not None):
            raise ValueError("why and by_user go with until, on an agent whose work rests until then")
        if agent and self.until is not None and self.why is None:
            raise ValueError(
                f"an agent entry with until needs why: in a few words, why the work rests until then, such as"
                f' "check the reminder after lunch" or "reminder you asked for". {user_name()} reads it on the row\'s'
                " hover in Waiting")
        return self


class ItemFields(Body):
    nullable = frozenset({"stage", "goal_id", "pinged_at", "touched_at"})

    title: Annotated[str, StringConstraints(min_length=1, max_length=store.TITLE_CHARS)] | None = None
    summary: Line | None = None
    # What the agent works on now, in a few words ("Build the Busy group"); Busy's hover line (SPEC VIEW-27).
    doing: Annotated[str, StringConstraints(max_length=store.DOING_CHARS)] | None = None
    project: Line | None = None
    stage: Literal["planning", "building", "review", "dev", "staging", "prod", "done"] | None = None
    waiting_on: list[Waiting] | None = None
    priority: Literal["urgent", "high", "normal", "low"] | None = None
    goal_id: int | None = None
    blocks: Line | None = None
    sessions: list[SessionId] | None = None
    links: list[Link] | None = None
    pinged_at: str | None = None
    touched_at: str | None = None
    archived: bool | None = None
    # A plain to-do (SPEC CONTROL-16): the user's own task, with no PR, ticket or thread behind it. Add sets it;
    # an agent sets it on an item that is clearly one.
    todo: bool | None = None
    # Only the user pins an item (SPEC CONTROL-13); an agent's put with it is refused.
    pinned: bool | None = None

    _times = field_validator("pinged_at", "touched_at")(_past_time)


class ItemPut(ItemFields):
    # todo-<id> is an item the user added; a PUT may update it, and only Add makes one (store.upsert_item).
    key: Annotated[str, StringConstraints(pattern=r"^((gh|linear|slack|session|note):\S.{0,200}|todo-[1-9][0-9]{0,9})$")]
    # Why a put that archives the item or moves it to stage done closes it, shown on its row in Finished this week (SPEC VIEW-4, VIEW-20).
    note: Line | None = None


class Detail(Body):
    title: Line
    body: Text


AnswerText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=store.ANSWER_CHARS, pattern=r"^[^\n]*$")]


class AnswerButton(Body):
    text: AnswerText
    # True: pressing the answer also completes the step's item (SPEC CONTROL-15).
    closes: bool


# A plain string is an answer that does not close.
Answer = AnswerText | AnswerButton
CommandText = Annotated[str, StringConstraints(min_length=1, max_length=store.COMMAND_CHARS)]
# Words, not only spaces: an empty explanation would show Run with nothing above it.
CommandExplain = Annotated[str, StringConstraints(max_length=store.COMMAND_EXPLAIN_CHARS, pattern=r"\S")]
RuleText = Annotated[str, StringConstraints(min_length=1, max_length=permissions.RULE_MAX)]


CommandFolder = Annotated[str, StringConstraints(min_length=2, max_length=1000, pattern=r"^/")]


class StepCommand(Body):
    # The command as the user would type it, and the absolute folder it runs in.
    run: CommandText
    cwd: CommandFolder
    # What each part of the command does, in plain words, and what it changes (SPEC NEXT-40).
    explain: CommandExplain


class StepPermission(Body):
    list: Literal["allow", "auto"]
    rule: RuleText


class StepClose(Body):
    item_id: int
    # What shows the task is done, in the session's words.
    why: Annotated[str, StringConstraints(min_length=1, max_length=300)]


class StepPut(Body):
    nullable = frozenset({"item_id", "item_key", "session_id", "command", "permission", "closes"})

    item_id: int | None = None
    item_key: str | None = None
    need: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    session_id: SessionId | None = None
    ask: Annotated[str, StringConstraints(min_length=1, max_length=300)] | None = None
    why: Line | None = None
    body: Text | None = None
    details: list[Detail] | None = None
    draft: Text | None = None
    # What kind of answer the step expects, shown as the reply box's placeholder (SPEC NEXT-51); "" for "Reply…".
    reply_hint: Annotated[str, StringConstraints(max_length=200)] | None = None
    score: Annotated[int, Field(ge=0, le=100)] | None = None
    reasons: list[Annotated[str, StringConstraints(max_length=60)]] | None = None
    # How big the user's part is and how much of the user's time on the item is behind them, 0 to 100 (SPEC NEXT-35);
    # the page draws one bar from both.
    size: Literal["small", "medium", "large"] | None = None
    progress: Annotated[int, Field(ge=0, le=100)] | None = None
    # One-click buttons (SPEC NEXT-21, NEXT-18); null clears a command or permission.
    answers: Annotated[list[Answer], Field(max_length=store.ANSWERS_MAX)] | None = None
    command: StepCommand | None = None
    permission: StepPermission | None = None
    # The tasks the session proposes the user closes with one press (SPEC CONTROL-23); null clears the list.
    closes: Annotated[list[StepClose], Field(min_length=1, max_length=store.CLOSES_MAX)] | None = None
    # An early result or question while the session works on (SPEC NEXT-42): it takes a place only when no other
    # step waits for one.
    early: bool | None = None


class StepChecked(Body):
    # What the agent checked, e.g. "PR open, CI green on the latest commit, no new comments".
    note: Annotated[str, StringConstraints(min_length=1, max_length=300)]


class StepActionBody(Body):
    nullable = frozenset({"until", "session_id"})

    until: str | None = None  # snooze: required; null wakes the step
    direction: Literal["up", "down"] | None = None  # move: required
    note: Line | None = None  # done or dismiss: why, shown with the step in Finished this week (SPEC VIEW-4, VIEW-20)
    # The user's Done: the step's session as their page showed it, null when it showed none. The page sends it, so
    # Done archives the session the card named (SPEC SESSION-4); an agent's done archives nothing and sends none.
    session_id: SessionId | None = None

    _until = field_validator("until")(_utc_time)


class StepRun(Body):
    # The command, its folder and the session the output goes to, as the user's page showed them.
    command: CommandText
    cwd: CommandFolder
    session_id: SessionId


class StepAllow(StepPermission):
    # The session told, as the user's page showed it.
    session_id: SessionId


class HandOff(Body):
    nullable = frozenset({"session_id", "new_cwd", "provider", "model", "effort", "account"})

    text: Text
    # Both left out: the step's own session.
    session_id: SessionId | None = None
    new_cwd: str | None = None
    # A new session's options (SPEC SESSION-22), each null for the default; store.start_options checks them.
    provider: Literal["claude", "codex"] | None = None
    model: OptionName | None = None
    effort: OptionName | None = None
    account: OptionName | None = None


class SecretIn(HandOff):
    # The secret's file name and its value (SPEC MSG-15); `text` is what the user wrote with it, maybe nothing.
    name: Annotated[str, StringConstraints(pattern=secret_files.NAME_PATTERN)]
    value: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=secret_files.VALUE_LIMIT)]


class FollowUp(Body):
    text: Text


class ItemFinish(Body):
    nullable = frozenset({"answer", "session_id"})

    step_id: int
    how: Literal["complete", "close"]
    # A closing answer's text, as the step's answers hold it; null for the Task done and Close item keys.
    answer: str | None
    # What the user typed in the step's reply box before Task done, sent as their last message; empty for none.
    note: Text
    # Why the user closed the item; empty for complete.
    reason: Annotated[str, StringConstraints(max_length=500)]
    # The step's session as the user's page showed it; null when it showed none.
    session_id: SessionId | None


class StepCloseTasks(Body):
    # The tasks on the step's list that the user left ticked.
    item_ids: Annotated[list[int], Field(min_length=1, max_length=store.CLOSES_MAX)]


class AwayAnswer(Body):
    # The question answered: the `id` of the state's `away`.
    question: str
    # Add: the time without activity counts too; otherwise Return.
    add: bool


class AwayStop(Body):
    # The end of the user's last activity the page saw before the stretch without any.
    since: str
    # How many of the user's inputs the page knew of: the state's `input.count` it measured the stretch by, or a later
    # count its own input made (`input_count` in the answer to a presence report, a start or an answer).
    seen: Annotated[int, Field(ge=0)]

    _since = field_validator("since")(_utc_time)


PRESS_ID = r"^[A-Za-z0-9-]{8,64}$"


class SnoozeWords(Body):
    # When the step should come back, in the user's own words (SPEC VIEW-29), and the page's IANA timezone.
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    tz: Annotated[str, StringConstraints(min_length=1, max_length=64)]


class TimerStart(Body):
    # The page's id for the press, kept on the span it starts (`store.pause_timer_at`).
    press: Annotated[str, Field(pattern=PRESS_ID)]


class TimerPause(Body):
    # How long before this request the user pressed pause, by the browser's clock: a try that lands late stops the
    # timer at the press. A page may stay open for long, so the age goes up to a year. `start_press`: the id of the
    # page's start of the step just before this pause, left out when none came (`store.pause_timer_at`).
    ago_ms: Annotated[int, Field(ge=0, le=366 * 24 * 3600 * 1000)]
    start_press: Annotated[str, Field(pattern=PRESS_ID)] | None = None


class ItemTime(Body):
    # The item's time as the user corrects it, in seconds, lower or higher than the timers counted.
    seconds: Annotated[int, Field(ge=0)]


class AddedTime(Body):
    # Time the timers missed, in seconds, that the user adds to the item (SPEC TRACK-7).
    seconds: Annotated[int, Field(gt=0)]


class GoalIn(Body):
    text: Text


class GoalsIn(Body):
    texts: list[Text]


class GoalMove(Body):
    position: Annotated[int, Field(ge=1)]


class WrongModelDismissal(Body):
    session_id: str
    model: str
    calls: int


class WarningSnooze(Body):
    nullable = frozenset({"until"})

    # An account problem's key holds its whole text.
    key: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    until: str | None  # null wakes the warning

    _until = field_validator("until")(_utc_time)


class AccountPreference(Body):
    # The account folder new work goes to first (accounts.prefer); null clears the choice.
    nullable: ClassVar[frozenset[str]] = frozenset({"account"})
    account: str | None
    minutes: int


class PickedPart(Body):
    # Where on the page the part is, in feedback's form (`page:<section>/step N/<kind>`).
    target: Annotated[str, StringConstraints(pattern=r"^page:", max_length=300)]
    # The text the part showed, "" when it has none.
    quote: Annotated[str, StringConstraints(max_length=2000)]


# Where a page comment sits (SPEC NEXT-38), as the script in the page's frame proposed it: words with up to
# PAGE_COMMENT_CONTEXT characters on each side, so the same words elsewhere on the page do not take the mark, or a
# part picked by its CSS path, with the text it showed.
PAGE_COMMENT_CONTEXT = 64


class TextAnchor(Body):
    kind: Literal["text"]
    quote: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    prefix: Annotated[str, StringConstraints(max_length=PAGE_COMMENT_CONTEXT)]
    suffix: Annotated[str, StringConstraints(max_length=PAGE_COMMENT_CONTEXT)]


class PartAnchor(Body):
    kind: Literal["part"]
    path: Annotated[str, StringConstraints(min_length=1, max_length=1000)]
    text: Annotated[str, StringConstraints(max_length=2000)]


class PageCommentAdd(Body):
    nullable = frozenset({"step_id"})

    # The page version the user's screen shows; Cockpit refuses a comment on one that has changed since.
    revision: Annotated[int, Field(ge=1)]
    # The step whose answer the comment goes with, or null on the Pages tab.
    step_id: int | None
    anchor: Annotated[TextAnchor | PartAnchor, Field(discriminator="kind")]
    text: Annotated[str, StringConstraints(min_length=1, max_length=store.PAGE_COMMENT_CHARS)]


class PageChoiceSet(Body):
    nullable = frozenset({"step_id", "chosen"})

    # The control's name on the page.
    name: Annotated[str, StringConstraints(min_length=1, max_length=store.CHOICE_NAME_MAX)]
    # Its value: one for a radio button, select or text box, a list for checkboxes or a multiple select, or null
    # when the user cleared it.
    chosen: str | list[str] | None
    # The words the control shows for the value, such as its label.
    label: Annotated[str, StringConstraints(max_length=store.CHOICE_VALUE_MAX * 2)]
    # The step the page is open for, whose answer the choice goes with, or null on the Pages tab.
    step_id: int | None


class PageCommentsItem(Body):
    # The unsent comments on the page that go to the new item.
    comment_ids: Annotated[list[int], Field(min_length=1, max_length=500)]


class PageCommentsSend(Body):
    # The unsent comments on the page that go to the item's session, after the user's own words (may be empty).
    comment_ids: Annotated[list[int], Field(min_length=1, max_length=500)]
    text: Text


class ItemAdd(Body):
    nullable = frozenset({"part", "provider", "model", "effort", "account"})

    # What the user wants done, in the user's words; the first line is the title.
    text: Annotated[str, StringConstraints(min_length=1, max_length=store.ADD_MAX_CHARS)]
    # A part of the page the user attached (SPEC CONTROL-9), or null.
    part: PickedPart | None
    # Whether the to-do starts in the Pinned list (SPEC CONTROL-13).
    pinned: bool
    # What its session starts with (SPEC SESSION-22), each null or left out for the default; a page loaded before
    # these options sends none.
    provider: Literal["claude", "codex"] | None = None
    model: OptionName | None = None
    effort: OptionName | None = None
    account: OptionName | None = None
    # Fire and forget (SPEC CONTROL-24): its session works to the end on its own and closes the item itself; a page
    # loaded before this option sends none.
    fire_and_forget: bool = False


class ItemAddMore(Body):
    # More of the user's words for a to-do they added (SPEC CONTROL-18).
    text: Text


class PermissionRule(Body):
    list: Literal["allow", "auto"]
    rule: Annotated[str, StringConstraints(min_length=1, max_length=permissions.RULE_MAX)]


class PermissionEdit(PermissionRule):
    old: Annotated[str, StringConstraints(min_length=1, max_length=permissions.RULE_MAX)]


class PermissionReport(Body):
    kind: Literal["denied", "asked"]
    session_id: SessionId
    cwd: Annotated[str, StringConstraints(min_length=1, max_length=1000)]
    tool_name: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    tool_input: dict[str, Any]
    # Empty when the hook could not tell: a waiting hook finds the id in the transcript.
    tool_use_id: Annotated[str, StringConstraints(max_length=200)]
    reason: Annotated[str, StringConstraints(max_length=2000)]


# The user's short note to the session, sent with the retry of a call auto mode blocked (SPEC PERM-7).
AllowNote = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300, pattern=r"^[^\r\n]*$")]


class PermissionAllow(Body):
    nullable = frozenset({"rule", "list", "note", "replaces"})

    rule: Annotated[str, StringConstraints(min_length=1, max_length=permissions.RULE_MAX)] | None
    list: Literal["allow", "auto"] | None
    note: AllowNote | None = None
    # The rule of the list that `rule` changes in place (SPEC PERM-11).
    replaces: Annotated[str, StringConstraints(min_length=1, max_length=permissions.RULE_MAX)] | None = None


class PermissionSettle(Body):
    state: Literal["answered", "expired"]


class PermissionWithdraw(Body):
    session_id: SessionId


class PermissionTell(Body):
    text: Text


class PermissionPropose(Body):
    session_id: SessionId
    sentence: Annotated[
        str, StringConstraints(min_length=permissions.SENTENCE_MIN, max_length=permissions.SENTENCE_MAX)
    ]


class Target(Body):
    nullable = frozenset({"item_id", "step_id", "session_id", "new_cwd", "provider", "model", "effort", "account"})

    item_id: int | None = None
    step_id: int | None = None
    session_id: SessionId | None = None
    new_cwd: str | None = None
    # A new session's options (SPEC SESSION-22), each null for the default; store.start_options checks them.
    provider: Literal["claude", "codex"] | None = None
    model: OptionName | None = None
    effort: OptionName | None = None
    account: OptionName | None = None


class MessageIn(Body):
    text: Text
    target: Target
    # An agent's own session, when it sends from one (SPEC MSG-12): the receiver is told how to reply to it.
    from_session: SessionId | None = None


class AskIn(Body):
    nullable: ClassVar[frozenset[str]] = frozenset({"step_id"})
    session_id: SessionId
    # The step whose box the user asked from, or null for the item's own box.
    step_id: int | None
    text: Text


class ForkIn(Body):
    session_id: SessionId
    text: Text


class ClaudeCode(Body):
    code: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\S+$", max_length=512)]

    # The sign-in the code belongs to, as its start named it.
    attempt: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class MessageDismiss(Body):
    nullable = frozenset({"reason"})

    # Why the failure no longer matters; an agent gives one line, the user's page sends null.
    reason: Line | None


class SessionMove(Body):
    # The Claude account to move the session to (config.ACCOUNTS).
    account: Annotated[str, StringConstraints(min_length=1, max_length=40)]


class SessionClose(Body):
    # Why the sweeper judged the session done, one line.
    reason: Annotated[str, StringConstraints(min_length=1, max_length=300)]


class FeedbackIn(Body):
    nullable = frozenset({"step_id", "item_id"})

    text: Text
    step_id: int | None = None
    item_id: int | None = None
    # What the feedback is about when it is not a whole step or item: `spec:<block>` or `page:<element>`, else "".
    target: Line
    # The text the user saw there, so the reader knows what the user meant even after it changes.
    quote: Text
    # How long the user wrote it: from opening the feedback box (or the first word) to Send (SPEC TRACK-2).
    seconds: Annotated[int, Field(ge=0, le=86_400)]


class SpecPart(Body):
    old: Annotated[str, StringConstraints(min_length=1, max_length=20000)]
    new: Text


class SpecWhole(Body):
    version: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]
    text: Annotated[str, StringConstraints(max_length=1_000_000)]


class ControlUse(Body):
    control: Line
    kind: Literal["press", "hover", "seen"]
    count: int = Field(ge=1, le=10000)


class ControlUsesIn(Body):
    batch: Line
    page: Line
    uses: list[ControlUse] = Field(max_length=500)


class FlashIn(Body):
    kind: Literal["request", "session", "codex", "step"]
    ref: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")]
    label: Line
    page: Line
    shown_at: Line
    gone_at: Line

    _times = field_validator("shown_at", "gone_at")(_past_time)

    @model_validator(mode="after")
    def _numbered(self) -> FlashIn:
        if self.kind in ("request", "step") and not self.ref.isdigit():
            raise ValueError(f"a {self.kind} is named by its number")
        return self


class PageErrorIn(Body):
    page: Line
    place: Line
    message: Text
    stack: Text
    count: int = Field(ge=1, le=100000)
    # When the page that met the error loaded (ISO time): a page loaded before a fix runs the old code.
    loaded_at: Line

    _loaded = field_validator("loaded_at")(_utc_time)


class PageErrorsTake(Body):
    ids: list[int] = Field(min_length=1)
    note: Line


class FeedbackRead(Body):
    ids: list[int]


class WritingIn(Body):
    nullable = frozenset({"step_id", "item_id", "page_id", "session_id"})

    # good: writing to reproduce; bad: writing the user found hard to understand (SPEC FEEDBACK-8).
    verdict: Literal["good", "bad"]
    # step: the whole step; page: a shared page; part: the part of a tab the user picked (SPEC FEEDBACK-8).
    kind: Literal["step", "page", "part"]
    # Where the part is, in feedback's form (`page:<section>/step N/<kind>`); "" for a whole step or page.
    target: Line
    # The words the user saw in the part; "" for a whole step or page, whose text the server copies.
    quote: Text
    # Why the user marked it, in their words; "" when they gave none.
    note: Text
    step_id: int | None
    item_id: int | None
    page_id: int | None
    # The session the picked part names, such as a message in a step's history; else the step's or item's.
    session_id: str | None


class SweepStart(Body):
    reorder: bool


class SweepFinish(Body):
    sweep_id: str
    summary: Line
    error: Line


class SweepRequest(Body):
    reason: Annotated[str, StringConstraints(min_length=1, max_length=300)]


class InProgressEntry(Body):
    # Strict, so JSON true is not item 1; bounded to what SQLite stores.
    item_id: Annotated[StrictInt, Field(ge=1, le=2**63 - 1)]
    line: str


class InProgressIn(Body):
    # store.put_in_progress names what is wrong with an entry.
    entries: list[InProgressEntry]


class BriefingIn(Body):
    # The day Cockpit's request named (YYYY-MM-DD); briefing.put refuses an earlier one.
    day: str
    # Markdown; briefing.put names the limit when it is too long.
    body: str


class TeamIn(Body):
    # The people the briefing follows, in the user's words; "" for no one.
    team: str


class ProjectIn(Body):
    # A project (SPEC VIEW-34): created by its name, or changed; a field left out stays. projects.put checks them.
    name: str
    about: str | None = None
    logo: str | None = None


class ProjectMerge(Body):
    into: str


class ProjectAssign(Body):
    items: list[int] = Field(min_length=1, max_length=500)


class InboxSettingsIn(Body):
    rules: list[str]
    sources: str


class EmergencyIn(Body):
    # A Codex turn names no account (SPEC SESSION-22). An emergency page loaded before SESSION-22 sends no provider,
    # model or effort: its turn runs Claude at the defaults, so a tab left open across a deploy keeps working.
    # `session_id` and `count` are the session as the page showed it: the message goes only to that one.
    nullable: ClassVar[frozenset[str]] = frozenset({"account", "session_id"})
    session_id: str | None
    count: Annotated[int, Field(ge=0)]
    text: Annotated[str, StringConstraints(min_length=1, max_length=100_000)]
    account: str | None
    provider: OptionName | None = None
    model: OptionName | None = None
    effort: OptionName | None = None


class EmergencySeen(Body):
    """The emergency session as the page that asks last read it: Stop and Close act only on that one."""
    nullable: ClassVar[frozenset[str]] = frozenset({"session_id"})
    session_id: str | None
    count: Annotated[int, Field(ge=0)]


class EmergencyClose(EmergencySeen):
    # Whether the user agreed to end an answer that is still running.
    end_answer: bool


class PageIn(Body):
    title: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    # A whole HTML document; store.PAGE_MAX_BYTES bounds it.
    html: Annotated[str, StringConstraints(min_length=1)]


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def _validation_detail(exc: RequestValidationError) -> str:
    parts = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error["loc"] if part != "body")
        parts.append(f"{where}: {error['msg']}" if where else error["msg"])
    return "; ".join(parts)


def _page(title: str, body_html: str, status: int) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<script src='/static/local-auth.js'></script><title>{html.escape(title)}</title><link rel=stylesheet href='/static/style.css'>"
        f"<link rel=stylesheet href='/static/look.css'>"
        f"<body class=plain><main>{body_html}</main>",
        status_code=status,
    )


async def _image_body(request: Request) -> bytes:
    """The request body, read no further than one byte past the image limit, so the store refuses an oversized one."""
    content = bytearray()
    async for chunk in request.stream():
        content += chunk
        if len(content) > store.IMAGE_MAX_BYTES:
            break
    return bytes(content)


def build_version(package_dir: Path) -> str:
    """12 hex characters over every file of the installed package, the page's static files among them
    (bytecode caches left out): a deploy that changes any of them changes it."""
    digest = hashlib.sha256()
    for path in sorted(package_dir.rglob("*")):
        relative = path.relative_to(package_dir)
        if path.is_file() and "__pycache__" not in relative.parts:
            digest.update(relative.as_posix().encode() + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()[:12]


def _connection(request: Request) -> Iterator[sqlite3.Connection]:
    """One connection per request (module level so FastAPI can resolve the annotation)."""
    conn = db.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(_connection)]


def create_app(
    settings: Settings,
    deliverer: delivery.Deliverer,
    session_cache: SessionCache | ProviderSessions,
    spend_index: spend.SpendIndex,
    codex_index: codex.CodexIndex,
) -> FastAPI:
    if settings.auth_mode == "local":
        config.validate_local_origin(settings.origin)
        if len(settings.local_login_token) < 32:
            raise ValueError("local mode requires a private local-login-token")
    elif settings.auth_mode != "google" or settings.google is None:
        raise ValueError("Google mode requires a Google OAuth client")
    setup = db.connect(settings.db_path)
    try:
        db.migrate(setup)
        interrupted = delivery.mark_interrupted(setup)
        if interrupted:
            log.error("%d message(s) were left mid-delivery by a restart and are now marked failed", interrupted)
        lost = sidechat.mark_interrupted(setup)
        if lost:
            log.error("%d side chat question(s) lost their answer to a restart and are now marked failed", lost)
        stopped = codex_agent.mark_interrupted(setup)
        if stopped:
            log.error("%d Codex turn(s) were left running by a restart and are now marked failed", stopped)
    finally:
        setup.close()

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        yield
        # Stopping, such as for a deploy's restart: the deliveries under way finish first (SPEC MSG-5). Here, since
        # uvicorn raises the stop signal again once it has stopped, so nothing after `uvicorn.run` runs.
        cut = await asyncio.to_thread(deliverer.drain, delivery.DRAIN_SECONDS)
        if cut:
            log.error("%d delivery(ies) still ran after %d s; the restart cuts them off", cut, delivery.DRAIN_SECONDS)

    app = FastAPI(title="Cockpit", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.db_path = settings.db_path
    key = settings.cookie_key
    # The page compares this with the version it loaded with and offers Reload when they differ.
    version = build_version(PACKAGE_DIR)
    # The build version alone would not do: a deploy installs the new files before it restarts Cockpit, so the old
    # process can serve them under its own version, and a rollback to that build would then use them.
    static_tag = f"{version}-{secrets.token_hex(4)}"
    claude_sign_in = logins.SignIn(settings.claude)
    codex_sign_in = codex_login.SignIn(settings.codex)
    local = settings.auth_mode == "local"
    session_cookie = auth.LOCAL_SESSION_COOKIE if local else auth.SESSION_COOKIE
    cookie_attributes = auth.LOCAL_COOKIE_ATTRIBUTES if local else auth.COOKIE_ATTRIBUTES
    sign_in_path = auth.LOCAL_SIGN_IN_PATH if local else auth.SIGN_IN_PATH

    def caller(request: Request) -> str | None:
        if auth.bearer_matches(request.headers.get("authorization"), settings.token):
            return "agent"
        value = request.cookies.get(session_cookie)
        valid = auth.local_session_cookie_valid if local else auth.session_cookie_valid
        if value is not None and valid(value, key, settings.email, time.time()):
            if local and request.url.path.startswith("/api/"):
                expected = auth.local_page_secret(key, value)
                if not hmac.compare_digest(request.headers.get("x-cockpit-local", "").encode(), expected.encode()):
                    return None
            return "user"
        return None

    # Added before the guard, so it runs inside it and sees who signed in.
    app.add_middleware(actions.Once, db_path=settings.db_path)

    @app.middleware("http")
    async def guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        # When the request arrived, before any wait for the database (a timer's pause counts back from it).
        request.state.arrived = datetime.now(UTC)
        path = request.url.path
        if local and (
            request.url.netloc != settings.origin.removeprefix("http://")
            or request.client is None or request.client.host not in {"127.0.0.1", "::1"}
        ):
            return _error(403, "local mode accepts only direct loopback requests to its configured host")
        me = caller(request)
        request.state.me = me
        if local and path.startswith("/api/") and me is None:
            return JSONResponse({"detail": "sign in first"}, status_code=401,
                                headers={"X-Cockpit-Auth": "local", "Cache-Control": "no-store"})
        # Cookie-carrying writes (sign-out included) must come from Cockpit's own page.
        if request.method not in SAFE_METHODS and me != "agent" and request.headers.get("origin") != settings.origin:
            return _error(403, "writes from the browser must come from Cockpit's own page")
        public = (path in PUBLIC_PATHS or (local and path in {auth.LOCAL_SIGN_IN_PATH, auth.LOCAL_SIGN_IN_PATH + "/code"})
                  or path == "/auth/sign-out" or path.startswith("/static/"))
        if not public and me is None:
            if path.startswith("/api/"):
                return _error(401, "sign in first")
            return RedirectResponse(sign_in_path, status_code=302)
        if me == "agent" and request.method not in SAFE_METHODS:
            name = request.headers.get("x-cockpit-as", "")
            if not AGENT_NAME.fullmatch(name):
                return _error(400, "agent writes need an X-Cockpit-As header naming the agent ([a-z0-9-], up to 32)")
            request.state.actor = f"agent:{name}"
        else:
            request.state.actor = me
        response = await call_next(request)
        # A shared page sets its own, stricter Content-Security-Policy.
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        # An item image never changes, so its route sets its own caching.
        if path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        elif path.startswith("/static/"):
            # Only this process's name is kept: a page from before a restart asks with its old tag.
            response.headers["Cache-Control"] = STATIC_KEPT if request.query_params.get("v") == static_tag else "no-cache"
        return response

    @app.exception_handler(store.NotFound)
    async def not_found(_: Request, exc: store.NotFound) -> JSONResponse:
        return _error(404, str(exc))

    @app.exception_handler(store.Conflict)
    async def conflict(_: Request, exc: store.Conflict) -> JSONResponse:
        return _error(409, str(exc))

    @app.exception_handler(store.Invalid)
    async def invalid(_: Request, exc: store.Invalid) -> JSONResponse:
        return _error(400, str(exc))

    @app.exception_handler(store.Forbidden)
    async def forbidden(_: Request, exc: store.Forbidden) -> JSONResponse:
        return _error(403, str(exc))

    @app.exception_handler(store.TooLarge)
    async def too_large(_: Request, exc: store.TooLarge) -> JSONResponse:
        return _error(413, str(exc))

    @app.exception_handler(RequestValidationError)
    async def bad_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(400, _validation_detail(exc))

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity(_: Request, exc: sqlite3.IntegrityError) -> JSONResponse:
        return _error(400, f"the write breaks a database rule: {exc}")

    @app.exception_handler(sqlite3.OperationalError)
    async def busy(_: Request, exc: sqlite3.OperationalError) -> JSONResponse:
        log.error("database error: %s", exc)
        return _error(503, f"database busy or failing: {exc}")

    def user_only(request: Request) -> None:
        if request.state.me != "user":
            raise store.Forbidden(f"only {user_name()}, signed in on the page, can do this")

    def session_only(request: Request) -> None:
        if request.state.me != "agent":
            raise store.Forbidden(f"only the session that made the call does this; {user_name()} dismisses or answers it")

    # Sign-in

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get(auth.SIGN_IN_PATH)
    def sign_in() -> Response:
        if local:
            return RedirectResponse(auth.LOCAL_SIGN_IN_PATH, status_code=302)
        assert settings.google is not None
        start = auth.start_sign_in(settings.google, settings.origin, settings.email)
        response = RedirectResponse(start.redirect_url, status_code=302)
        response.headers["Set-Cookie"] = (
            f"{auth.SIGN_IN_COOKIE}={start.cookie_value}; Max-Age={auth.SIGN_IN_MAX_AGE}; {auth.COOKIE_ATTRIBUTES}"
        )
        return response

    @app.get(auth.CALLBACK_PATH)
    def callback(request: Request) -> Response:
        if local:
            return _error(404, "Google sign-in is disabled in local mode")
        assert settings.google is not None
        expected = auth.parse_sign_in_cookie(request.cookies.get(auth.SIGN_IN_COOKIE))
        state = request.query_params.get("state", "")
        code = request.query_params.get("code", "")
        clear = f"{auth.SIGN_IN_COOKIE}=; Max-Age=0; {auth.COOKIE_ATTRIBUTES}"
        retry = f"<p><a href='{auth.SIGN_IN_PATH}'>Try again</a>"
        if request.query_params.get("error"):
            response = _page("Sign-in cancelled", f"<h1>Sign-in cancelled</h1>{retry}", 400)
        elif expected is None or not state or not hmac.compare_digest(expected[0], state) or not code:
            response = _page("Sign-in expired", f"<h1>Sign-in expired or was not started here</h1>{retry}", 400)
        else:
            try:
                claims = auth.id_token_claims(
                    settings.exchange(settings.google, code, settings.origin + auth.CALLBACK_PATH, expected[1])
                )
            except auth.SignInError as exc:
                log.error("sign-in failed: %s", exc)
                response = _page("Sign-in failed", f"<h1>Sign-in failed</h1><p>{html.escape(str(exc))}{retry}", 502)
            else:
                if auth.admitted(claims, settings.google.client_id, time.time(), settings.email):
                    expires = int(time.time()) + auth.SESSION_MAX_AGE
                    response = RedirectResponse("/", status_code=302)
                    response.headers["Set-Cookie"] = (
                        f"{auth.SESSION_COOKIE}={auth.session_cookie_value(key, settings.email, expires)}; "
                        f"Max-Age={auth.SESSION_MAX_AGE}; {auth.COOKIE_ATTRIBUTES}"
                    )
                else:
                    account = html.escape(str(claims.get("email", "this account")))
                    response = _page(
                        "Not admitted",
                        f"<h1>Cockpit is private</h1><p>{account} cannot use Cockpit."
                        f" Sign in as {html.escape(settings.email)}."
                        f"{retry}",
                        403,
                    )
        response.headers.append("Set-Cookie", clear)
        return response

    @app.post("/auth/sign-out")
    def sign_out() -> Response:
        response = _page(
            "Signed out", f"<h1>Signed out</h1><p><a href='{sign_in_path}'>Sign in again</a>", 200
        )
        response.headers["Set-Cookie"] = f"{session_cookie}=; Max-Age=0; {cookie_attributes}"
        return response

    if local:
        # Codes live only in this process. The async routes consume them without yielding between checks.
        local_codes: dict[str, float] = {}

        @app.get(auth.LOCAL_SIGN_IN_PATH)
        def local_sign_in() -> Response:
            return _page("Local sign-in", "<h1>Sign in on this Mac</h1>"
                         "<p id='status'>Run <code>cockpit login</code> in your terminal to open Cockpit.</p>"
                         "<script src='/static/local-login.js'></script>", 200,
                         )

        @app.post(auth.LOCAL_SIGN_IN_PATH + "/code")
        async def local_sign_in_code(request: Request) -> Response:
            if not auth.bearer_matches(request.headers.get("authorization"), settings.local_login_token):
                return _error(403, "invalid local sign-in token; run cockpit login again")
            now = time.monotonic()
            for code, expires in list(local_codes.items()):
                if expires <= now:
                    del local_codes[code]
            code = secrets.token_urlsafe(32)
            local_codes[code] = now + 120
            return JSONResponse({"code": code}, headers={"Cache-Control": "no-store"})

        @app.post(auth.LOCAL_SIGN_IN_PATH)
        async def local_sign_in_finish(body: dict[str, str]) -> Response:
            supplied = body.get("code", "").encode()
            code = next((code for code in local_codes if hmac.compare_digest(code.encode(), supplied)), None)
            if code is None or local_codes.pop(code) <= time.monotonic():
                return _error(403, "local sign-in code used or expired; run cockpit login again")
            expires = int(time.time()) + auth.SESSION_MAX_AGE
            session = auth.local_session_cookie_value(key, settings.email, expires)
            response = JSONResponse({"ok": True, "page_secret": auth.local_page_secret(key, session)},
                                    headers={"Cache-Control": "no-store"})
            response.headers["Set-Cookie"] = (
                f"{session_cookie}={session}; "
                f"Max-Age={auth.SESSION_MAX_AGE}; {cookie_attributes}"
            )
            return response

    # Page

    def cockpit_page(layout: str) -> HTMLResponse:
        # CodeMirror creates its own styles. A fresh style-only nonce permits
        # those without allowing inline scripts or arbitrary inline styles.
        nonce = secrets.token_urlsafe(24)
        page = STATIC_REFERENCE.sub(rf'\1?v={static_tag}"', (STATIC_DIR / "index.html").read_text())
        page = page.replace("__COCKPIT_STYLE_NONCE__", nonce).replace("<body>", f'<body data-layout="{layout}">', 1)
        policy = SECURITY_HEADERS["Content-Security-Policy"] + f"; style-src 'self' 'nonce-{nonce}'"
        return HTMLResponse(page, headers={"Cache-Control": "no-cache", "Content-Security-Policy": policy})

    # The Cockpit page with the list of items at its left (SPEC VIEW-26). The page from before it stays at /classic
    # while the list grows to hold what only that page has, such as the list of sessions.
    @app.get("/")
    def index() -> HTMLResponse:
        return cockpit_page("sidebar")

    @app.get("/classic")
    def classic_page() -> HTMLResponse:
        return cockpit_page("classic")

    # Where the sidebar page was tried; old links keep working.
    @app.get("/next")
    def next_page() -> RedirectResponse:
        return RedirectResponse("/", status_code=308)

    # The spec, the list of skills, each skill and the writing examples share one page; its script reads
    # which from the path.
    @app.get("/spec")
    @app.get("/skills")
    @app.get("/skills/{name}")
    @app.get("/writing")
    def spec_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "spec.html", headers={"Cache-Control": "no-cache"})

    @app.get("/briefing")
    def briefing_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "briefing.html", headers={"Cache-Control": "no-cache"})

    @app.get("/usage")
    def usage_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "usage.html", headers={"Cache-Control": "no-cache"})

    # The Pages view also opens in the layer over Cockpit's main page (SPEC NEXT-47, `&overlay=1`), so Cockpit's own
    # pages may frame it. It names its files with the main page's tag, so the layer takes them from the browser's
    # cache, which already has most of them from the main page, and asks Cockpit only for the page itself.
    @app.get("/pages")
    def pages_page() -> HTMLResponse:
        policy = SECURITY_HEADERS["Content-Security-Policy"].replace("frame-ancestors 'none'", "frame-ancestors 'self'")
        page = STATIC_REFERENCE.sub(rf'\1?v={static_tag}"', (STATIC_DIR / "pages.html").read_text())
        return HTMLResponse(page, headers={"Cache-Control": "no-cache", "Content-Security-Policy": policy})

    # The emergency session (SPEC SESSION-21): a page and three calls that use no database, so they keep working
    # while the rest of the page is broken.
    @app.get("/emergency")
    def emergency_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "emergency.html", headers={"Cache-Control": "no-cache"})

    def emergency_unreachable(exc: Exception) -> Response:
        # The turn stays recorded, so the page can try again.
        log.error("emergency session: %s", exc)
        return _error(503, f"Cockpit could not reach systemd for the emergency session: {exc}. Try again.")

    @app.get("/api/emergency", dependencies=[Depends(user_only)])
    def emergency_view() -> Response:
        try:
            return JSONResponse(emergency.view())
        except emergency.Unreachable as exc:
            return emergency_unreachable(exc)

    @app.post("/api/emergency/send", dependencies=[Depends(user_only)])
    def emergency_send(body: EmergencyIn) -> Response:
        picks = (body.provider, body.model, body.effort)
        if picks == (None, None, None):
            claude = config.SESSION_OPTIONS["claude"]
            picks = ("claude", claude["models"][0][0], claude["effort"])
        elif None in picks:
            return _error(400, "provider, model and effort go together")
        provider, model, effort = picks
        try:
            emergency.send(body.session_id, body.count, body.text, provider, body.account, model, effort, settings.checkouts_dir)  # ty: ignore[invalid-argument-type]  # All three picks are set above.
            return JSONResponse(emergency.view())
        except (LookupError, ValueError) as exc:
            return _error(400, str(exc))
        except (emergency.Busy, emergency.Changed) as exc:
            return _error(409, str(exc))
        except emergency.Unreachable as exc:
            return emergency_unreachable(exc)
        except OSError as exc:
            log.exception("emergency session: could not start a turn")
            return _error(500, f"Cockpit could not start the turn: {exc}")

    @app.post("/api/emergency/stop", dependencies=[Depends(user_only)])
    def emergency_stop(body: EmergencySeen) -> Response:
        try:
            emergency.stop(body.session_id, body.count)
            return JSONResponse(emergency.view())
        except emergency.Changed as exc:
            return _error(412, str(exc))
        except emergency.Unreachable as exc:
            return emergency_unreachable(exc)

    @app.post("/api/emergency/close", dependencies=[Depends(user_only)])
    def emergency_close(body: EmergencyClose) -> Response:
        try:
            emergency.close(body.session_id, body.count, body.end_answer)
            return JSONResponse(emergency.view())
        except emergency.Changed as exc:
            return _error(412, str(exc))
        except emergency.Busy as exc:
            return _error(409, str(exc))
        except emergency.Unreachable as exc:
            return emergency_unreachable(exc)
        except OSError as exc:
            log.exception("emergency session: could not close it")
            return _error(500, f"Cockpit could not close the session: {exc}")

    @app.get("/static/local-auth.js")
    def local_auth_script() -> Response:
        # Google pages load the same HTML; their fetch and sign-out behaviour stays native.
        script = (STATIC_DIR / "local-auth.js").read_text() if local else ""
        return Response(script, media_type="text/javascript")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # API

    def blocked_lists(conn: sqlite3.Connection, now: datetime, dialogs: dict[str, datetime]) -> dict[str, list[dict]]:
        """The permission cards by where the page shows them. A refusal waits a while before the page shows it, and one
        the session got past folds away under its own line (SPEC PERM-18). The session's own list (`cockpit permission
        list`) shows all three. A dialog that closed in its session stays a minute, marked closed, out of the Blocked
        count (PERM-23)."""
        cards = permissions.open_requests(
            conn, now, Path.home(), permissions.auto_sentences(settings.claude_settings), dialogs
        )
        return {
            "permission_requests": [
                card for card in cards if not card["held"] and not card["moved_past"] and not card["settled"]
            ],
            "settled_requests": [card for card in cards if card["settled"]],
            "moved_past": [card for card in cards if not card["held"] and card["moved_past"]],
            "held_requests": [card for card in cards if card["held"]],
        }

    def dialog_waiters(
        conn: sqlite3.Connection, now: datetime, dialogs: dict[str, datetime], waiting: list[str],
        permission_requests: list[dict],
    ) -> list[str]:
        """Of the sessions `waiting` at a dialog, those Blocked and waiting lists: a session at a dialog that came to
        Cockpit shows once, as that request with its answers; once the user answered it, not at all while the answer
        reaches the dialog."""
        asking = {request["session_id"] for request in permission_requests if request["kind"] == "asked"}
        asking |= permissions.just_answered(conn, now, dialogs)
        return [session_id for session_id in waiting if session_id not in asking]

    @app.get("/api/listed")
    def listed(conn: Conn) -> dict[str, Any]:
        """The ids of what the page lists in Next up, Blocked and waiting and the queue, in a fraction of the state's
        time: a page back from the background shows its cards again once none of them has gone, rather than
        "Updating…" until the whole state came (SPEC UI-60)."""
        now = datetime.now(UTC)
        sessions = session_cache.snapshot(wait=False).sessions
        dialogs = store.open_dialogs(sessions)
        blocked = blocked_lists(conn, now, dialogs)
        waiting = [session.session_id for session in store.at_dialogs(sessions)]
        return {
            "steps": store.listed_steps(conn, now),
            # The items the state lists: All work's (store.on_all_work).
            "items": [
                row["id"] for row in conn.execute("SELECT id, archived, stage, updated_at, done_at, pinned_at FROM items WHERE archived = 0")
                if store.on_all_work(row, now)
            ],
            **{key: [card["id"] for card in cards] for key, cards in blocked.items()},
            "codex_requests": [request["id"] for request in deliverer.codex.requests()] if deliverer.codex is not None else [],
            "waiting_sessions": dialog_waiters(conn, now, dialogs, waiting, blocked["permission_requests"]),
        }

    @app.get("/api/state")
    def state(request: Request, conn: Conn) -> dict[str, Any]:
        now = datetime.now(UTC)
        # A tab in the background asks with ?background=1: it keeps its cards current, but the user is not looking.
        if request.state.me == "user" and "background" not in request.query_params:
            sweeper.user_seen(conn, now)
        listing = session_cache.snapshot(wait=False)
        sessions = listing.sessions
        dialogs = store.open_dialogs(sessions)
        result = store.page_state(conn, request.state.me, sessions, listing.warnings, settings.checkouts_dir)
        result |= blocked_lists(conn, now, dialogs)
        result["account_problems"] = [
            {"text": problem.text, "detail": problem.detail} for problem in listing.account_problems
        ]
        result["attach"] = settings.attach
        result["goals_summary"] = history.goals_summary(conn, [goal["text"] for goal in result["goals"]])
        result["projects"] = projects.listing(conn)
        result["providers"] = list(settings.providers)
        result["default_provider"] = settings.default_provider
        # The user's home folder, which a blocked call's folder shows as `~` (SPEC PERM-26).
        result["home"] = str(Path.home())
        # What a new session may start with (SPEC SESSION-22): each enabled provider's models and efforts, its
        # defaults, and the Claude accounts to pick from.
        result["session_options"] = {
            provider: {
                "models": [{"name": name, "label": label} for name, label in config.SESSION_OPTIONS[provider]["models"]],
                "efforts": config.SESSION_OPTIONS[provider]["efforts"],
                "effort": config.SESSION_OPTIONS[provider]["effort"],
            }
            for provider in settings.providers
        }
        result["session_accounts"] = [
            {"name": account.name, "letter": account.letter} for account in accounts.in_use(settings.claude_accounts, [])
        ]
        result["codex_requests"] = deliverer.codex.requests() if deliverer.codex is not None else []
        # Blocked counts Claude permission requests only: the header names them as calls auto mode blocked.
        # Codex approvals and questions show under Blocked from codex_requests.
        result["counts"]["blocked"] = len(result["permission_requests"])
        shown = set(dialog_waiters(
            conn, now, dialogs, [session["session_id"] for session in result["waiting_sessions"]],
            result["permission_requests"],
        ))
        result["waiting_sessions"] = [session for session in result["waiting_sessions"] if session["session_id"] in shown]
        result["counts"]["sessions_waiting"] = len(result["waiting_sessions"])
        result["checking"] = sweeper.checking(conn, now)
        result["sweep_launch"] = sweeper.launch_status(conn, now)
        result["check_top"] = sweeper.CHECK_TOP
        result["check_every_minutes"] = sweeper.CHECK_EVERY_MINUTES
        result["sweeps_paused"] = sweeper.paused(conn)
        result["usage"] = usage.read(
            settings.claude_rate_limits, now, accounts.in_use(settings.claude_accounts, []), accounts.preference(conn, now),
            running(sessions),
        ) | {"codex": codex_index.weeks(now), "machine": machine.read()}
        spent = spend_index.per_session(now)
        for row in result["sessions"]:
            row["spend"] = spent.get(row["session_id"])
        names = store.shown_session_names(conn, sessions)
        result["wrong_models"] = [
            row | {"name": names.get(row["session_id"]) or row["title"] or row["first_prompt"]}
            for row in store.not_dismissed(conn, spend_index.wrong_models(now, store.started_models(conn)))
        ]
        result["snoozed_warnings"] = store.snoozed_warnings(conn, now)
        waiting = claude_sign_in.waiting()
        result["claude_sign_ins"] = [
            entry | {"waiting": entry["account"] in waiting} for entry in accounts.sign_ins(settings.claude_accounts, now)
        ] if "claude" in settings.providers else []
        result["codex_sign_ins"] = codex_login.status(conn, codex_index, codex_sign_in, request.state.me == "user")
        result["briefing"] = briefing.status(conn)
        result["team"] = briefing.team(conn)
        result["side_chats"] = sidechat.recent(conn, now)
        try:
            result["emergency"] = emergency.summary()
        except Exception:
            # The main page keeps working without the emergency session's row.
            log.exception("emergency session: could not read it for the main page")
            result["emergency"] = None
        result["version"] = version
        return result

    @app.get("/api/items")
    def get_item(conn: Conn, key: Annotated[str, Query(min_length=1)]) -> dict[str, Any]:
        return store.item_by_key(conn, key)

    # Search with Include closed (SPEC VIEW-21): the closed items that match, beside the open ones the page has.
    @app.get("/api/items/closed")
    def find_closed(conn: Conn, q: Annotated[str, Query(min_length=1, max_length=200)]) -> dict[str, Any]:
        return {"items": store.find_closed(conn, q, datetime.now(UTC))}

    @app.put("/api/items")
    def put_item(body: ItemPut, request: Request, conn: Conn) -> dict[str, Any]:
        fields = body.model_dump(exclude_unset=True)
        store.check_agent_title(fields, request.state.actor)
        store.check_agent_waiting(fields, request.state.actor)
        return store.upsert_item(conn, fields, request.state.actor)

    @app.patch("/api/items/{item_id}")
    def patch_item(item_id: int, body: ItemFields, request: Request, conn: Conn) -> dict[str, Any]:
        fields = body.model_dump(exclude_unset=True)
        store.check_agent_title(fields, request.state.actor)
        store.check_agent_waiting(fields, request.state.actor)
        return store.patch_item(conn, item_id, fields, request.state.actor)

    @app.get("/api/items/{item_id}/history")
    def item_history(item_id: int, conn: Conn) -> dict[str, Any]:
        store.get_item(conn, item_id)
        working = {session.session_id for session in session_cache.get() if session.working_now}
        return history.item_history(conn, item_id, datetime.now(UTC), working)

    # The user's Add: a to-do that becomes an item for the sweeper to pick up.
    @app.post("/api/items/add", dependencies=[Depends(user_only)])
    def add_item(body: ItemAdd, conn: Conn) -> dict[str, Any]:
        sweeper.resume(conn, "added a to-do")
        start = {"provider": body.provider, "model": body.model, "effort": body.effort, "account": body.account}
        return store.add_item(
            conn, body.text, body.part.model_dump() if body.part else None, body.pinned, start, body.fire_and_forget
        )

    # More words for a to-do the user added (SPEC CONTROL-18): they join its text and go to its session. A session
    # still starting gets them once it is known, as a follow-up does.
    @app.post("/api/items/{item_id}/add-more", dependencies=[Depends(user_only)])
    def add_more(item_id: int, body: ItemAddMore, background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        try:
            added = store.add_more(conn, item_id, body.text, settings.checkouts_dir)
        except store.Conflict:
            first = conn.execute(
                "SELECT new_cwd FROM messages WHERE item_id = ? AND author = 'user' AND new_cwd IS NOT NULL"
                " AND state = 'sending' ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
            if first is None:
                raise
            deliverer.wait_for_start(first[0])
            added = store.add_more(conn, item_id, body.text, settings.checkouts_dir)
        message = added["message"]
        if message is not None and not message.get("joined"):
            background.add_task(deliverer.send, message["id"])
        return added

    # The body is the raw image, read only after the guard has checked the caller
    # (and an agent's X-Cockpit-As name).
    @app.post("/api/items/{item_id}/images")
    def add_item_image(
        item_id: int, request: Request, content: Annotated[bytes, Depends(_image_body)], conn: Conn
    ) -> dict[str, Any]:
        return store.add_item_image(
            conn, item_id, request.headers.get("content-type", ""), content, request.state.actor
        )

    @app.get("/item-images/{image_id}")
    @app.get("/api/item-images/{image_id}")
    def get_item_image(image_id: int, conn: Conn) -> Response:
        image = store.item_image(conn, image_id)
        # An image id is never reused, so the browser may keep the image for good.
        return Response(
            image["content"],
            media_type=image["content_type"],
            headers={"Cache-Control": "private, max-age=31536000, immutable"},
        )

    @app.delete("/api/item-images/{image_id}")
    def remove_item_image(image_id: int, request: Request, conn: Conn) -> dict[str, Any]:
        return store.remove_item_image(conn, image_id, request.state.actor)

    def page_out(page: dict[str, Any]) -> dict[str, Any]:
        return page | {"url": f"{settings.origin}/pages/{page['id']}"}

    # Every page on every item, open or closed, for the Pages tab (SPEC API-4).
    @app.get("/api/pages")
    def list_pages(conn: Conn) -> dict[str, Any]:
        return {"pages": [page_out(page) for page in store.all_pages(conn, datetime.now(UTC))]}

    @app.post("/api/items/{item_id}/pages")
    def add_item_page(item_id: int, body: PageIn, request: Request, conn: Conn) -> dict[str, Any]:
        return page_out(store.add_item_page(conn, item_id, body.title, body.html, request.state.actor))

    @app.put("/api/item-pages/{page_id}")
    def update_item_page(page_id: int, body: PageIn, request: Request, conn: Conn) -> dict[str, Any]:
        return page_out(store.update_item_page(conn, page_id, body.title, body.html, request.state.actor))

    @app.delete("/api/item-pages/{page_id}")
    def remove_item_page(page_id: int, request: Request, conn: Conn) -> dict[str, Any]:
        return page_out(store.remove_item_page(conn, page_id, request.state.actor))

    @app.get("/pages/{page_id}")
    def show_item_page(page_id: int, conn: Conn) -> HTMLResponse:
        page = store.item_page(conn, page_id)
        return HTMLResponse(page["html"], headers={"Content-Security-Policy": PAGE_CSP, "Cache-Control": "no-cache"})

    # `comments=1`: the page takes comments (SPEC NEXT-38).
    @app.get("/pages/{page_id}/figure")
    def show_item_figure(page_id: int, conn: Conn, comments: bool = False) -> HTMLResponse:
        page = store.item_page(conn, page_id)
        return HTMLResponse(
            (with_pick_guard(page["html"]) if comments else page["html"])
            + FIGURE_SIZE_SCRIPT + FIGURE_LINK_SCRIPT + PAGE_CHOICES_SCRIPT + (PAGE_FRAME_SCRIPT if comments else ""),
            headers={"Content-Security-Policy": FIGURE_CSP, "Cache-Control": "no-cache"},
        )

    # Choices on a page (SPEC NEXT-48): the page sets its controls to them, and a session reads them.
    @app.get("/api/item-pages/{page_id}/choices")
    def list_page_choices(page_id: int, conn: Conn) -> dict[str, Any]:
        return {"choices": store.page_choices(conn, page_id)}

    @app.put("/api/item-pages/{page_id}/choices", dependencies=[Depends(user_only)])
    def set_page_choice(page_id: int, body: PageChoiceSet, conn: Conn) -> dict[str, Any]:
        return {"choices": store.set_page_choice(conn, page_id, body.name, body.chosen, body.label, body.step_id)}

    @app.get("/api/item-pages/{page_id}/comments")
    def list_page_comments(page_id: int, conn: Conn) -> dict[str, Any]:
        return {"comments": store.page_comments(conn, page_id)}

    @app.post("/api/item-pages/{page_id}/comments", dependencies=[Depends(user_only)])
    def add_page_comment(page_id: int, body: PageCommentAdd, conn: Conn) -> dict[str, Any]:
        return store.add_page_comment(conn, page_id, body.revision, body.step_id, body.anchor.model_dump(), body.text)

    @app.delete("/api/page-comments/{comment_id}", dependencies=[Depends(user_only)])
    def remove_page_comment(comment_id: int, conn: Conn) -> dict[str, Any]:
        return store.remove_page_comment(conn, comment_id)

    # Add as new item (SPEC NEXT-38): the page starts the item's session afterwards, as Add does.
    @app.post("/api/item-pages/{page_id}/comments/item", dependencies=[Depends(user_only)])
    def add_page_comments_item(page_id: int, body: PageCommentsItem, conn: Conn) -> dict[str, Any]:
        sweeper.resume(conn, "added a to-do")
        return store.add_page_comments_item(conn, page_id, body.comment_ids)

    # Send to the item's session (SPEC NEXT-38): comments no open step takes go to the page's item's session.
    @app.post("/api/item-pages/{page_id}/comments/send", dependencies=[Depends(user_only)])
    def send_page_comments(page_id: int, body: PageCommentsSend, background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        sweeper.resume(conn, "wrote a message")
        message = store.send_page_comments(conn, page_id, body.comment_ids, body.text, settings.checkouts_dir)
        background.add_task(deliverer.send, message["id"])
        return message

    @app.post("/api/steps")
    def put_step(body: StepPut, request: Request, conn: Conn) -> dict[str, Any]:
        fields = body.model_dump(exclude_unset=True)
        item_key = fields.pop("item_key", None)
        if (fields.get("item_id") is None) == (item_key is None):
            raise store.Invalid("give exactly one of item_id and item_key")
        if item_key is not None:
            record = conn.execute("SELECT id FROM items WHERE key = ?", (item_key,)).fetchone()
            if record is None:
                raise store.NotFound(f"no item with key {item_key}")
            fields["item_id"] = record["id"]
        if "answers" in fields:
            fields["answers"] = [
                {"text": answer, "closes": False} if isinstance(answer, str) else answer for answer in fields["answers"]
            ]
        if fields.get("permission") is not None:
            wanted = fields["permission"]
            wanted["rule"] = permissions.check_rule(wanted["list"], wanted["rule"])
        # One transaction, so a hand-off cannot land between the check and the put.
        with db.transaction(conn):
            actor = request.state.actor
            store.refuse_answer_elsewhere(conn, fields, actor)
            closes = store.last_handed_step(conn, fields, actor)
            put = store.upsert_step(conn, fields, actor)
            if closes is None:
                return put
            # The finish step is open by now, so the close that STATE-5 refused on its own goes through.
            closed = store.step_action(conn, closes, "done", {"note": f"finish step on need {fields['need']} took over"}, actor)
            if closed.get("held"):
                return put | {"held": closed["held"]}
            return put | {"closed_handed": closes, "step": store.get_step(conn, put["step"]["id"])}

    # Before the generic action route, which would otherwise take "hand-off", "run" and "allow" as actions.
    # The message waits for Take back (SPEC MSG-8): it goes on the page's release, or on the wake loop's.
    @app.post("/api/steps/{step_id}/hand-off", dependencies=[Depends(user_only)])
    def hand_off(step_id: int, body: HandOff, conn: Conn) -> dict[str, Any]:
        sweeper.resume(conn, "handed off a step")
        target = {"session_id": body.session_id, "new_cwd": body.new_cwd, "provider": body.provider,
                  "model": body.model, "effort": body.effort, "account": body.account}
        return store.hand_off_step(conn, step_id, body.text, target, settings.checkouts_dir)

    # A secret for the step's session (SPEC MSG-15): its value goes to a file of its own, and the step goes to the
    # session with a message that says only where the file is. It goes at once: Take back could not unwrite the file.
    @app.post("/api/steps/{step_id}/secret", dependencies=[Depends(user_only)])
    def hand_secret(step_id: int, body: SecretIn, background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        sweeper.resume(conn, "handed off a step")
        target = {"session_id": body.session_id, "new_cwd": body.new_cwd, "provider": body.provider,
                  "model": body.model, "effort": body.effort, "account": body.account}
        # The value is written before the hand-off and takes the secret's name inside its transaction: a write that
        # fails leaves no message, and a hand-off or commit that fails leaves the secret of that name as it was.
        with secret_files.staged(settings.secrets_dir, body.name, body.value) as secret, db.transaction(conn):
            message = store.hand_off(conn, step_id, "user", secret_files.message(body.name, secret.path, body.text),
                                     target, settings.checkouts_dir, "hand_off")
            store.reopen_for_message(conn, message["item_id"])
            secret.publish()
        store.release_held(conn)
        background.add_task(deliverer.send, message["id"])
        return store.get_message(conn, message["id"])

    @app.post("/api/steps/{step_id}/run", dependencies=[Depends(user_only)])
    def run_command(step_id: int, body: StepRun, conn: Conn) -> dict[str, Any]:
        sweeper.resume(conn, "ran a step's command")
        run, message = commands.start(conn, step_id, body.command, body.cwd, body.session_id, settings.checkouts_dir)
        deliverer.start(
            f"command-run-{run['id']}",
            commands.execute, settings.db_path, settings.checkouts_dir, run["id"], message["id"], deliverer.send,
        )
        return {"run": run, "message": message}

    @app.post("/api/steps/{step_id}/allow", dependencies=[Depends(user_only)])
    def allow_step_permission(step_id: int, body: StepAllow, background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        sweeper.resume(conn, "allowed a step's permission")
        message = permissions.allow_step(
            conn, settings.claude_settings, settings.checkouts_dir, step_id, body.model_dump(exclude={"session_id"}),
            body.session_id,
        )
        background.add_task(deliverer.send, message["id"])
        return message

    # Time on a step (SPEC TRACK-2): the user starts and pauses a step's timer.
    @app.post("/api/steps/{step_id}/timer/start", dependencies=[Depends(user_only)])
    def start_timer(step_id: int, conn: Conn, body: TimerStart | None = None) -> dict[str, Any]:
        # Older pages and callers may omit the press id.
        return store.start_timer(conn, step_id, body.press if body else None)

    @app.post("/api/steps/{step_id}/timer/pause", dependencies=[Depends(user_only)])
    def pause_timer(step_id: int, request: Request, conn: Conn, body: TimerPause | None = None) -> dict[str, Any]:
        if body is None:
            return store.pause_timer(conn, step_id)
        pressed = request.state.arrived - timedelta(milliseconds=body.ago_ms)
        return store.pause_timer_at(conn, step_id, pressed, body.start_press)

    # A snooze time the user typed (SPEC VIEW-29): Claude reads the words as a time, which the page then snoozes the
    # step until. Reading changes nothing.
    @app.post("/api/snooze/read", dependencies=[Depends(user_only)])
    def read_snooze_words(body: SnoozeWords) -> dict[str, str]:
        try:
            zone = ZoneInfo(body.tz)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise store.Invalid(f"unknown time zone {body.tz!r}; send an IANA name like Europe/Amsterdam") from error
        try:
            at = snooze_words.read(body.text, zone, datetime.now(UTC))
        except snooze_words.Unread as exc:
            raise store.Invalid(str(exc)) from exc
        return {"until": at.strftime("%Y-%m-%dT%H:%M:%SZ")}

    # The user opened, pressed or started answering a queued step: it keeps its place (SPEC NEXT-16).
    @app.post("/api/steps/{step_id}/hold", dependencies=[Depends(user_only)])
    def hold_step(step_id: int, conn: Conn) -> dict[str, Any]:
        return store.hold_step(conn, step_id)

    # The page saw no activity while the step's timer ran: it stops at the last activity (SPEC TRACK-5).
    @app.post("/api/steps/{step_id}/timer/away", dependencies=[Depends(user_only)])
    def stop_away(step_id: int, body: AwayStop, conn: Conn) -> dict[str, Any]:
        return store.stop_away(conn, step_id, body.since, body.seen)

    @app.post("/api/steps/{step_id}/timer/answer", dependencies=[Depends(user_only)])
    def answer_away(step_id: int, body: AwayAnswer, conn: Conn) -> dict[str, Any]:
        return store.answer_away(conn, step_id, body.question, body.add)

    @app.post("/api/items/{item_id}/time", dependencies=[Depends(user_only)])
    def set_item_time(item_id: int, body: ItemTime, conn: Conn) -> dict[str, Any]:
        return store.set_item_time(conn, item_id, body.seconds)

    @app.post("/api/items/{item_id}/time/add", dependencies=[Depends(user_only)])
    def add_item_time(item_id: int, body: AddedTime, conn: Conn) -> dict[str, Any]:
        return store.add_item_time(conn, item_id, body.seconds)

    # Close these tasks (SPEC CONTROL-23): a session lists the tasks it judges done, each with why, on a step of its
    # own, and the user's one press completes the ones left ticked, each as its Task done would, from its finish
    # step (`store.finish_step_for`). One transaction holds the press: a task that changed since the session listed
    # it (closed, or gone) is refused in its own savepoint and the others still close, and any other failure closes
    # none. Once one closes, the step is done, and its session hears what closed and what stayed open. Undo is each
    # item's reopen route, then the step's.
    @app.post("/api/steps/{step_id}/close-tasks", dependencies=[Depends(user_only)])
    def close_tasks(step_id: int, body: StepCloseTasks, background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        sweeper.resume(conn, "completed tasks a session listed")
        finished: list[int] = []
        refused: list[dict[str, Any]] = []
        with db.transaction(conn):
            step = store.get_step(conn, step_id)
            if step["status"] != "open" or step["withdrawn_at"]:
                raise store.Conflict(f"step {step_id} is not open")
            whys = {entry["item_id"]: entry["why"] for entry in step["closes"] or []}
            for item_id in body.item_ids:
                if item_id not in whys:
                    raise store.Invalid(f"step {step_id} does not list item {item_id}")
                try:
                    with db.transaction(conn):
                        finishing = store.finish_step_for(conn, item_id, whys[item_id], step["prepared_by"])
                        seen = store.target_session(conn, item_id, finishing)
                        finish(conn, item_id, finishing, "complete", None, "", whys[item_id], seen, background)
                except (store.Conflict, store.Invalid, store.NotFound) as error:
                    refused.append({"item_id": item_id, "error": str(error)})
                else:
                    finished.append(item_id)
            if finished:
                step = store.step_action(conn, step_id, "done", {}, "user")
                session_id = store.target_session(conn, step["item_id"], step_id)
                if session_id is not None:
                    told = store.cockpit_prompt(
                        conn, session_id, step_id, store.closed_tasks_text(conn, step, finished, refused), settings.checkouts_dir
                    )
                    background.add_task(deliverer.send, told["id"])
        return {"finished": finished, "refused": refused, "step": step}

    @app.post("/api/steps/{step_id}/checked")
    def checked_step(step_id: int, body: StepChecked, request: Request, conn: Conn) -> dict[str, Any]:
        return store.check_step(conn, step_id, body.note, request.state.actor)

    @app.post("/api/steps/{step_id}/{action}")
    def act_on_step(
        step_id: int, action: str, body: StepActionBody, request: Request, background: BackgroundTasks, conn: Conn
    ) -> dict[str, Any]:
        fields = body.model_dump(exclude_unset=True)
        seen = fields.pop("session_id", None)
        user_done = action == "done" and request.state.actor == "user"
        session_id = None
        # One transaction: Done resolves the step's session, refuses when it is no longer the one the page showed,
        # and closes the step, so a put that moves the step to another session goes before or after all three.
        with db.transaction(conn):
            if user_done:
                current = store.get_step(conn, step_id)
                session_id = store.target_session(conn, current["item_id"], step_id)
                if "session_id" in body.model_fields_set and not store.is_read(current):
                    store.check_seen_target(step_id, session_id, seen)
            step = store.step_action(conn, step_id, action, fields, request.state.actor)
        # Done on a result to read (need `read:…`, VIEW-16) closes only that step: the item's work goes on.
        if user_done and not store.is_read(step):
            step["session_end"] = archive_after_done(conn, step, session_id, background)
        if action == "reopen":
            message = store.cancel_archive(conn, step_id, settings.checkouts_dir)
            if message is not None:
                background.add_task(deliverer.send, message["id"])
            if step["closes"] and request.state.actor == "user":
                with db.transaction(conn):
                    undone = store.undo_closed_tasks(conn, step_id, settings.checkouts_dir)
                if undone is not None:
                    background.add_task(deliverer.send, undone["id"])
        return step

    # The user's Archive (SPEC SESSION-4). While the step's session, the one its card names, has other open
    # steps, it closes only the step. On its last open step it also archives the session: Cockpit tells it
    # and stops it once that turn is over (delivery.archives_due), and the sweep checks the archive. The
    # sweep's own session is never archived this way.
    def archive_after_done(
        conn: sqlite3.Connection, step: dict[str, Any], session_id: str | None, background: BackgroundTasks
    ) -> dict[str, Any] | None:
        session = deliverer.lookup(session_id) if session_id else None
        if session is None or session.name in store.SWEEP_SESSION_NAMES:
            return None
        name = store.shown_session_names(conn, [session])[session.session_id]
        # The count and the archive in one transaction, so a step put between them keeps the session.
        with db.transaction(conn):
            # Counts this step too: an Undo that reopened it before this transaction keeps the session.
            others = store.other_open_steps(conn, session.session_id, 0)
            if others:
                return {"name": name, "how": "kept", "other_steps": others}
            message = store.archive_session(
                conn, session, step, settings.checkouts_dir, store.archive_text(step["id"]), None
            )
        if message is not None:
            background.add_task(deliverer.send, message["id"])
        return {"name": name, "how": "archiving", "other_steps": 0}

    # Complete task and Close item (SPEC CONTROL-15): the user's one action that ends an item, from its step.
    # A closing answer (`answer`) or the last message typed before Task done (`note`) goes to the session as the
    # user's message and waits for Take back like a hand-off; the page releases it. Undo is the reopen route.
    def finish(
        conn: sqlite3.Connection, item_id: int, step_id: int, how: str, answer: str | None, note: str, reason: str,
        seen_session: str | None, background: BackgroundTasks,
    ) -> dict[str, Any]:
        session_id = store.target_session(conn, item_id, step_id)
        store.check_seen_target(step_id, session_id, seen_session)
        session = deliverer.lookup(session_id) if session_id else None
        if session is not None and session.name in store.SWEEP_SESSION_NAMES:
            session = None
        finished = store.finish_item(
            conn, item_id, step_id, how, answer, note, reason, seen_session, session, settings.checkouts_dir,
        )
        if finished["send"] is not None:
            background.add_task(deliverer.send, finished["send"]["id"])
        briefing.read_on_finish(conn, finished["item"])
        return finished

    @app.post("/api/items/{item_id}/finish", dependencies=[Depends(user_only)])
    def finish_item(item_id: int, body: ItemFinish, background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        sweeper.resume(conn, "completed an item" if body.how == "complete" else "closed an item")
        return finish(conn, item_id, body.step_id, body.how, body.answer, body.note, body.reason, body.session_id, background)

    @app.post("/api/items/{item_id}/reopen", dependencies=[Depends(user_only)])
    def reopen_item(item_id: int, background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        reopened = store.reopen_item(conn, item_id, settings.checkouts_dir)
        if reopened["send"] is not None:
            background.add_task(deliverer.send, reopened["send"]["id"])
        return reopened

    # The user's goals (SPEC CONTROL-12): one ordered list, written only on the page.
    @app.get("/api/goals")
    def get_goals(conn: Conn) -> list[dict[str, Any]]:
        return store.goal_list(conn)

    @app.post("/api/goals", dependencies=[Depends(user_only)])
    def add_goal(body: GoalIn, conn: Conn) -> dict[str, Any]:
        sweeper.resume(conn, "added a goal")
        return store.add_goal(conn, body.text)

    # The whole list as the user wrote it in the Priorities box, one goal per line (UI-65).
    @app.put("/api/goals", dependencies=[Depends(user_only)])
    def set_goals(body: GoalsIn, conn: Conn) -> list[dict[str, Any]]:
        sweeper.resume(conn, "changed their goals")
        return store.set_goals(conn, body.texts)

    @app.put("/api/goals/{goal_id}", dependencies=[Depends(user_only)])
    def reword_goal(goal_id: int, body: GoalIn, conn: Conn) -> dict[str, Any]:
        return store.reword_goal(conn, goal_id, body.text)

    @app.post("/api/goals/{goal_id}/move", dependencies=[Depends(user_only)])
    def move_goal(goal_id: int, body: GoalMove, conn: Conn) -> dict[str, Any]:
        return store.move_goal(conn, goal_id, body.position)

    @app.post("/api/goals/{goal_id}/{action}", dependencies=[Depends(user_only)])
    def goal_action(goal_id: int, action: Literal["pause", "resume", "drop", "restore"], conn: Conn) -> dict[str, Any]:
        act = {
            "pause": store.pause_goal, "resume": store.resume_goal, "drop": store.drop_goal, "restore": store.restore_goal,
        }[action]
        return act(conn, goal_id)

    @app.get("/api/permissions")
    def get_permissions() -> dict[str, Any]:
        return permissions.rules(settings.claude_settings)

    @app.post("/api/permissions/rules", dependencies=[Depends(user_only)])
    def add_permission(body: PermissionRule, conn: Conn) -> dict[str, Any]:
        rule, added = permissions.add_rule(conn, settings.claude_settings, body.list, body.rule)
        return {"rule": rule, "added": added, **permissions.rules(settings.claude_settings)}

    @app.post("/api/permissions/rules/remove", dependencies=[Depends(user_only)])
    def remove_permission(body: PermissionRule, conn: Conn) -> dict[str, Any]:
        permissions.remove_rule(conn, settings.claude_settings, body.list, body.rule)
        return permissions.rules(settings.claude_settings)

    # The Undo of a rule changed from a blocked call's card changes it back (SPEC PERM-11).
    @app.post("/api/permissions/rules/edit", dependencies=[Depends(user_only)])
    def edit_permission(body: PermissionEdit, conn: Conn) -> dict[str, Any]:
        rule = permissions.edit_rule(conn, settings.claude_settings, body.list, body.old, body.rule)
        return {"rule": rule, **permissions.rules(settings.claude_settings)}

    @app.post("/api/permissions/requests")
    def report_permission(body: PermissionReport, request: Request, conn: Conn) -> dict[str, Any]:
        # What the session was asked to do explains a block (SPEC PERM-13); a dialog needs no explanation.
        asked_by = last_instruction(body.session_id) if body.kind == "denied" else None
        # Auto mode judges a call with the session's calls before it: a block can come from an earlier call, such as
        # one that reached for credentials or posted to GitHub.
        earlier = None
        label = permissions.label_of(body.reason)
        if body.kind == "denied" and label is not None:
            since = datetime.now(UTC) - timedelta(minutes=permissions.EARLIER_CALL_MINUTES)
            calls = calls_before(body.session_id, body.tool_use_id, since.strftime("%Y-%m-%dT%H:%M:%SZ"))
            earlier = permissions.earlier_call(calls, label, Path.home())
        return permissions.report(
            conn, request.state.actor, body.kind, body.session_id, body.cwd, body.tool_name, body.tool_input,
            body.tool_use_id, body.reason, asked_by, earlier, settings.checkouts_dir,
        )

    @app.get("/api/permissions/requests/{request_id}")
    def get_permission_request(request_id: int, conn: Conn) -> dict[str, Any]:
        return permissions.get_request(conn, request_id)

    @app.post("/api/permissions/requests/withdraw-all", dependencies=[Depends(session_only)])
    def withdraw_all_permissions(body: PermissionWithdraw, request: Request, conn: Conn) -> dict[str, Any]:
        return {"withdrawn": permissions.withdraw_all(conn, request.state.actor, body.session_id, datetime.now(UTC))}

    def dialog_to_restart(conn: sqlite3.Connection, request_id: int) -> Session | None:
        """The live session still at the dialog of a request whose hook stopped waiting (SPEC PERM-8): the user's
        answer restarts it. None while the hook waits, as it answers the dialog itself."""
        request = permissions.get_request(conn, request_id)
        if request["kind"] != "asked" or request["state"] not in ("open", "expired"):
            return None
        if permissions.hook_waits(request, datetime.now(UTC)):
            return None
        session = deliverer.lookup(request["session_id"])
        if session is None or not permissions.at_dialog(conn, request, store.open_dialogs([session])):
            raise store.Conflict("the session no longer waits at this dialog: it was answered in its terminal, or the session moved on")
        return session

    def deliver(message: dict[str, Any] | None, restart: Session | None, background: BackgroundTasks) -> None:
        if message is not None:
            background.add_task(deliverer.restart if restart is not None else deliverer.send, message["id"])

    @app.post("/api/permissions/requests/{request_id}/allow", dependencies=[Depends(user_only)])
    def allow_permission(
        request_id: int, body: PermissionAllow, background: BackgroundTasks, conn: Conn
    ) -> dict[str, Any]:
        restart = dialog_to_restart(conn, request_id)
        answered, message = permissions.allow(
            conn, settings.claude_settings, settings.checkouts_dir, request_id, body.rule, body.list, restart, body.note,
            body.replaces,
        )
        deliver(message, restart, background)
        return answered

    # The user's own words to a blocked call's session, from its card (SPEC PERM-13).
    @app.post("/api/permissions/requests/{request_id}/tell", dependencies=[Depends(user_only)])
    def tell_permission(request_id: int, body: PermissionTell, background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        sweeper.resume(conn, "wrote a message")
        answered, message = permissions.tell(conn, settings.checkouts_dir, request_id, body.text)
        background.add_task(deliverer.send, message["id"])
        return answered

    @app.post("/api/permissions/requests/{request_id}/withdraw", dependencies=[Depends(session_only)])
    def withdraw_permission(request_id: int, body: PermissionWithdraw, request: Request, conn: Conn) -> dict[str, Any]:
        return permissions.withdraw(conn, request.state.actor, request_id, body.session_id, datetime.now(UTC))

    @app.post("/api/permissions/requests/{request_id}/active", dependencies=[Depends(user_only)])
    def user_active_on_request(request_id: int, conn: Conn) -> dict[str, Any]:
        return permissions.mark_user_active(conn, request_id)

    @app.post("/api/permissions/requests/{request_id}/propose", dependencies=[Depends(session_only)])
    def propose_permission(request_id: int, body: PermissionPropose, request: Request, conn: Conn) -> dict[str, Any]:
        return permissions.propose(conn, request.state.actor, request_id, body.session_id, body.sentence)

    @app.post("/api/permissions/requests/{request_id}/deny", dependencies=[Depends(user_only)])
    def deny_permission(request_id: int, background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        restart = dialog_to_restart(conn, request_id)
        answered, message = permissions.close(conn, settings.checkouts_dir, request_id, "denied", restart)
        deliver(message, restart, background)
        return answered

    @app.post("/api/permissions/requests/{request_id}/dismiss", dependencies=[Depends(user_only)])
    def dismiss_permission(request_id: int, conn: Conn) -> dict[str, Any]:
        return permissions.close(conn, settings.checkouts_dir, request_id, "dismissed", None)[0]

    @app.post("/api/permissions/requests/{request_id}/settle")
    def settle_permission(request_id: int, body: PermissionSettle, conn: Conn) -> dict[str, Any]:
        return permissions.settle(conn, request_id, body.state)

    @app.post("/api/messages")
    def post_message(body: MessageIn, request: Request, background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        if request.state.me == "user":
            sweeper.resume(conn, "wrote a message")
        if body.from_session is not None and request.state.me == "user":
            raise store.Invalid("from_session is for an agent's message; the user's messages come from the page")
        # An agent types the session id, and a mistyped one would only fail on delivery, as a failed message on the
        # user's page (SPEC MSG-5). The page offers only sessions that exist.
        session_id = body.target.session_id
        if request.state.me != "user" and session_id is not None and not deliverer.has_session(session_id):
            alike = sorted({each.session_id for each in session_cache.get() if each.session_id[:8] == session_id[:8]})
            hint = f"; did you mean {' or '.join(alike)}?" if alike else "."
            raise store.Invalid(
                f"no session {session_id} on this machine{hint} Copy the full id from `cockpit get` or the item's sessions."
            )
        with db.transaction(conn):
            message = store.create_message(
                conn, request.state.actor, body.text, body.target.model_dump(), settings.checkouts_dir
            )
            if body.from_session is not None:
                message = store.mark_sender(conn, message["id"], body.from_session)
            if request.state.me == "user" and message["item_id"] is not None:
                store.reopen_for_message(conn, message["item_id"])
        background.add_task(deliverer.send, message["id"])
        return message

    def listed_session(session_id: str) -> Session:
        found = next((entry for entry in session_cache.snapshot(wait=True).sessions if entry.session_id == session_id), None)
        if found is None:
            raise store.NotFound(f"session {session_id[:8]} is not on this machine")
        return found

    # Ask: a quick question about an item's work to a read-only copy of its session, which keeps working; the
    # question and answer then go to the session (SPEC MSG-11).
    @app.post("/api/items/{item_id}/ask", dependencies=[Depends(user_only)])
    def ask(item_id: int, body: AskIn, conn: Conn) -> dict[str, Any]:
        session = listed_session(body.session_id)
        chat = sidechat.ask(conn, item_id, body.step_id, session, body.text)
        threading.Thread(
            target=sidechat.answer,
            args=(settings.db_path, chat["id"], session, sidechat.ask_copy, settings.checkouts_dir, deliverer.send),
            name=f"ask-{chat['id']}",
            daemon=True,
        ).start()
        return chat

    # Fork: a new line of work as a new item, its session forked from this item's session (SPEC MSG-11).
    @app.post("/api/items/{item_id}/fork", dependencies=[Depends(user_only)])
    def fork(item_id: int, body: ForkIn, background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        sweeper.resume(conn, "forked a session")
        session = listed_session(body.session_id)
        if session.provider != "claude":
            raise store.Invalid("Fork copies a Claude session; this one runs on Codex")
        forked = store.fork(conn, item_id, session.session_id, session.cwd, body.text, settings.checkouts_dir)
        background.add_task(deliverer.send, forked["message"]["id"])
        return forked

    @app.post("/api/codex/requests/{request_id}/answer", dependencies=[Depends(user_only)])
    def answer_codex(request_id: str, body: dict[str, Any]) -> dict[str, bool]:
        if deliverer.codex is None:
            raise store.NotFound("Codex is not enabled")
        try:
            deliverer.codex.answer(request_id, body)
        except CodexError as exc:
            raise store.Conflict(str(exc)) from exc
        return {"ok": True}

    @app.get("/api/messages/{message_id}")
    def get_message(message_id: int, conn: Conn) -> dict[str, Any]:
        return store.get_message(conn, message_id)

    @app.post("/api/messages/{message_id}/retry")
    def retry(message_id: int, request: Request, background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        message = store.retry_message(conn, message_id, request.state.actor)
        background.add_task(deliverer.send, message_id)
        return message

    # A message that waits for its session's turn to end goes now: the session stops and resumes with it (SPEC MSG-3).
    @app.post("/api/messages/{message_id}/send-now", dependencies=[Depends(user_only)])
    def send_now(message_id: int, background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        message = store.send_now_message(conn, message_id)
        background.add_task(deliverer.restart, message_id)
        return message

    # The page's notice with Take back after a hand-off is gone (SPEC MSG-8): the message stops waiting for Take
    # back and goes. A message that no longer waited for it (taken back, released or sent now) stays as it is.
    @app.post("/api/messages/{message_id}/release", dependencies=[Depends(user_only)])
    def release(message_id: int, background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        if store.release_message(conn, message_id):
            background.add_task(deliverer.send, message_id)
        return store.get_message(conn, message_id)

    # While the user writes more in the notice with Take back (SPEC MSG-10), the message keeps waiting.
    @app.post("/api/messages/{message_id}/hold", dependencies=[Depends(user_only)])
    def hold(message_id: int, conn: Conn) -> dict[str, Any]:
        store.hold_message(conn, message_id)
        return store.get_message(conn, message_id)

    # A follow-up from the notice with Take back (SPEC MSG-10): it joins the first message while that has not gone,
    # else goes after it to the same session.
    @app.post("/api/messages/{message_id}/follow-up", dependencies=[Depends(user_only)])
    def follow_up(message_id: int, body: FollowUp, background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        try:
            message = store.add_follow_up(conn, message_id, body.text, settings.checkouts_dir)
        except store.Conflict:
            first = store.get_message(conn, message_id)
            if first["state"] == "sending" and first["new_cwd"] is not None:
                # The first message is starting a new session, which the follow-up goes to once it is known.
                deliverer.wait_for_start(first["new_cwd"])
            elif first["session_id"] is None:
                raise
            message = store.add_follow_up(conn, message_id, body.text, settings.checkouts_dir)
        if not message["joined"]:
            background.add_task(deliverer.send, message["id"])
        return message

    @app.post("/api/messages/{message_id}/dismiss")
    def dismiss_message(message_id: int, body: MessageDismiss, request: Request, conn: Conn) -> dict[str, Any]:
        return store.dismiss_message(conn, message_id, body.reason, request.state.actor)

    @app.post("/api/messages/{message_id}/restore", dependencies=[Depends(user_only)])
    def restore_message(message_id: int, conn: Conn) -> dict[str, Any]:
        return store.restore_message(conn, message_id)

    # A dialog in a session's terminal that Cockpit cannot answer (SPEC PERM-8): the session stops and resumes,
    # and the call it makes again comes to Cockpit as a permission request.
    @app.post("/api/sessions/{session_id}/restart", dependencies=[Depends(user_only)])
    def restart_session(session_id: str, background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        session = deliverer.lookup(session_id)
        if session is None:
            raise store.NotFound(f"no Claude session {session_id} on this machine")
        message = store.restart_message(conn, session, settings.checkouts_dir, store.restart_text(session))
        background.add_task(deliverer.restart, message["id"])
        return message

    # The session's conversation, as shown in the sidebar (SPEC VIEW-26).
    @app.get("/api/sessions/{session_id}/log", dependencies=[Depends(user_only)])
    def read_session_log(session_id: SessionId, conn: Conn) -> Any:
        # A Codex thread's conversation is Codex's to give; a Claude session's is its transcript.
        if deliverer.codex is not None and deliverer.codex.owns(session_id):
            try:
                log = deliverer.codex.log(session_id)
            except CodexError as exc:
                return _error(502, str(exc))
        else:
            log = session_log(session_id)
        refs = store.mention_refs(conn)
        log["parts"] = [part | {"html": render.markdown_html(part["text"], refs)} if part["kind"] == "says" else part for part in log["parts"]]
        return log

    @app.post("/api/sessions/{session_id}/close")
    def close_session(session_id: str, body: SessionClose, request: Request) -> Any:
        if request.state.actor != "agent:sweep":
            raise store.Forbidden("only the sweeper (--as sweep) closes sessions")
        try:
            closed = deliverer.close(session_id, body.reason, request.state.actor)
        except delivery.DeliveryError as exc:
            return _error(502, str(exc))
        session_cache.invalidate()
        return closed

    # Moving a session to another Claude account (SPEC SESSION-18): the user's Move, or an agent the user asked.
    # Cockpit never does it by itself, so the sweeper may not.
    @app.post("/api/sessions/{session_id}/move")
    def move_session(session_id: str, body: SessionMove, request: Request, background: BackgroundTasks) -> Any:
        if request.state.actor == "agent:sweep":
            raise store.Forbidden(f"the sweeper does not move sessions; only {user_name()} decides a move")
        try:
            moved = deliverer.move(session_id, body.account, request.state.actor, settings.checkouts_dir, None)
        except delivery.DeliveryError as exc:
            return _error(502, str(exc))
        session_cache.invalidate()
        if moved["message_id"] is not None:
            background.add_task(deliverer.send, moved["message_id"])
        return moved

    # Signing a Claude account in again from the page (SPEC UI-40): Claude's link, then the code it shows.
    @app.post("/api/claude/{name}/sign-in", dependencies=[Depends(user_only)])
    def claude_start(name: str) -> dict[str, str]:
        return claude_sign_in.start(claude_account(name))

    @app.post("/api/claude/{name}/code", dependencies=[Depends(user_only)])
    def claude_code(name: str, body: ClaudeCode) -> dict[str, Any]:
        account = claude_account(name)
        claude_sign_in.finish(account, body.attempt, body.code)
        return {"account": account.name, "signed_out": accounts.signed_out(account, datetime.now(UTC))}

    def claude_account(name: str) -> Account:
        found = next((account for account in settings.claude_accounts if account.name == name), None)
        if "claude" not in settings.providers or found is None:
            raise store.NotFound(f"no Claude account {name!r}")
        return found

    # Signing a Codex account in from the page (SPEC UI-77): Codex's link and code, which the state then shows.
    @app.post("/api/codex/{folder}/sign-in", dependencies=[Depends(user_only)])
    def codex_start(folder: str, conn: Conn) -> dict[str, bool]:
        home = codex_home(folder)
        address = codex.email(home)
        codex_login.remember(conn, folder, address)
        codex_sign_in.start(home, address or codex_login.held(conn).get(folder, ""), datetime.now(UTC))
        return {"ok": True}

    @app.post("/api/codex/{folder}/keep", dependencies=[Depends(user_only)])
    def codex_keep(folder: str, conn: Conn) -> dict[str, bool]:
        address = codex.email(codex_home(folder))
        if not address:
            raise store.Invalid(f"{folder} holds no account to keep")
        codex_login.keep(conn, folder, address)
        return {"ok": True}

    def codex_home(folder: str) -> Path:
        found = next((account.home for account in codex_index.accounts if account.home.name == folder), None)
        if found is None:
            raise store.NotFound(f"no Codex folder {folder!r}")
        return found

    @app.get("/api/feedback")
    def get_feedback(conn: Conn, unread: bool = True) -> list[dict[str, Any]]:
        return store.feedback_list(conn, unread_only=unread)

    # The user's choice that new sessions go to one account for a while, ahead of the usual order (SPEC SESSION-17).
    @app.post("/api/accounts/prefer", dependencies=[Depends(user_only)])
    def prefer_account(body: AccountPreference, conn: Conn) -> dict[str, Any]:
        now = datetime.now(UTC)
        try:
            accounts.prefer(conn, settings.claude_accounts, body.account, body.minutes, now)
        except ValueError as exc:
            raise store.Invalid(str(exc)) from exc
        preference = accounts.preference(conn, now)
        return {"account": preference and preference.account, "until": preference and preference.until.isoformat()}

    # The user's close on a wrong-model banner: it stays away until the session answers on that model again (SESSION-20).
    @app.post("/api/wrong-models/dismiss", dependencies=[Depends(user_only)])
    def dismiss_wrong_model(body: WrongModelDismissal, conn: Conn) -> dict[str, Any]:
        store.dismiss_wrong_model(
            conn, body.session_id, body.model, body.calls, datetime.now(UTC), spend.WRONG_MODEL_HOURS
        )
        return {"ok": True}

    # The user's snooze on a warning in Inbox (SPEC UI-72): it stays away until its time, on every page.
    @app.post("/api/warnings/snooze", dependencies=[Depends(user_only)])
    def snooze_warning(body: WarningSnooze, conn: Conn) -> dict[str, Any]:
        now = datetime.now(UTC)
        if body.until is not None and datetime.fromisoformat(body.until) <= now:
            raise store.Invalid("a warning snoozes until a time still to come")
        store.snooze_warning(conn, body.key, body.until, now)
        return {"snoozed_warnings": store.snoozed_warnings(conn, now)}

    @app.post("/api/feedback", dependencies=[Depends(user_only)])
    def post_feedback(body: FeedbackIn, conn: Conn) -> dict[str, Any]:
        sweeper.resume(conn, "gave feedback")
        return store.add_feedback(conn, body.text, body.step_id, body.item_id, body.target, body.quote, body.seconds)

    @app.post("/api/feedback/read")
    def read_feedback(body: FeedbackRead, conn: Conn) -> dict[str, int]:
        store.mark_feedback_read(conn, body.ids)
        return {"marked": len(body.ids)}

    # Writing examples (SPEC FEEDBACK-8), good or hard to understand: the user marks them on the page; the page and
    # agents list them.
    @app.get("/api/writing")
    def get_writing(conn: Conn) -> list[dict[str, Any]]:
        return store.writing_examples(conn)

    @app.get("/api/writing/{example_id}")
    def get_writing_example(example_id: int, conn: Conn) -> dict[str, Any]:
        return store.writing_example(conn, example_id)

    @app.post("/api/writing", dependencies=[Depends(user_only)])
    def post_writing(body: WritingIn, conn: Conn) -> dict[str, Any]:
        return store.add_writing_example(
            conn, body.verdict, body.kind, body.target, body.quote, body.note, body.step_id, body.item_id, body.page_id,
            body.session_id, session_cache.get(),
        )

    @app.delete("/api/writing/{example_id}", dependencies=[Depends(user_only)])
    def delete_writing(example_id: int, conn: Conn) -> dict[str, Any]:
        return store.remove_writing_example(conn, example_id)

    @app.delete("/api/steps/{step_id}/writing", dependencies=[Depends(user_only)])
    def delete_step_writing(step_id: int, conn: Conn) -> dict[str, Any]:
        return store.clear_step_writing(conn, step_id)

    # The morning briefing on the user's team (SPEC VIEW-8).

    @app.get("/api/briefings")
    def briefings(conn: Conn) -> dict[str, Any]:
        return briefing.listing(conn)

    @app.get("/api/briefings/{briefing_id}")
    def get_briefing(briefing_id: int, conn: Conn) -> dict[str, Any]:
        return briefing.get(conn, briefing_id)

    @app.post("/api/briefings")
    def put_briefing(body: BriefingIn, request: Request, conn: Conn) -> dict[str, Any]:
        return briefing.put(conn, request.state.actor, body.day, body.body)

    # The people the briefing follows: the page asks the user, and an agent may set them on the user's word.
    @app.put("/api/team")
    def put_team(body: TeamIn, conn: Conn) -> dict[str, str]:
        return {"team": briefing.put_team(conn, body.team)}

    @app.post("/api/briefings/{briefing_id}/read", dependencies=[Depends(user_only)])
    def read_briefing(briefing_id: int, conn: Conn) -> dict[str, int]:
        briefing.mark_read(conn, briefing_id)
        return {"read": briefing_id}

    # Projects (SPEC VIEW-34, CONTROL-26, SWEEP-23): every task is in one, and each has a logo Cockpit designs.
    @app.get("/api/projects")
    def get_projects(conn: Conn) -> list[dict[str, Any]]:
        return projects.listing(conn)

    @app.put("/api/projects")
    def put_project(body: ProjectIn, request: Request, conn: Conn) -> dict[str, Any]:
        return projects.put(conn, request.state.actor, body.name, body.about, body.logo)

    @app.post("/api/projects/{name}/merge")
    def merge_project(name: str, body: ProjectMerge, request: Request, conn: Conn) -> dict[str, Any]:
        return projects.merge(conn, request.state.actor, name, body.into)

    @app.post("/api/projects/{name}/assign")
    def assign_project(name: str, body: ProjectAssign, conn: Conn) -> dict[str, Any]:
        return projects.assign(conn, name, body.items)

    @app.delete("/api/projects/{name}")
    def remove_project(name: str, conn: Conn) -> dict[str, Any]:
        return projects.remove(conn, name)

    # The page shows a logo as an <img>, which runs nothing; the policy keeps it from loading anything either.
    @app.get("/projects/{name}/logo.svg")
    @app.get("/api/projects/{name}/logo.svg")
    def project_logo(name: str, conn: Conn) -> Response:
        return Response(
            projects.logo(conn, name), media_type="image/svg+xml",
            headers={
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
                "Cache-Control": "private, max-age=86400",
            },
        )

    # Inbox settings (SPEC UI-70): how the sweeper ranks the user's work, and what it checks for new work. The user
    # changes them on the page; the sweeper reads them.
    @app.get("/api/inbox-settings")
    def get_inbox_settings(conn: Conn) -> dict[str, Any]:
        return inbox_settings.get(conn)

    @app.get("/api/inbox-settings/text")
    def inbox_settings_text(conn: Conn) -> dict[str, str]:
        return {"text": inbox_settings.as_text(conn, user_name())}

    @app.put("/api/inbox-settings", dependencies=[Depends(user_only)])
    def put_inbox_settings(body: InboxSettingsIn, conn: Conn) -> dict[str, Any]:
        saved = inbox_settings.put(conn, body.rules, body.sources)
        sweeper.request(conn, "user", "changed the Inbox settings")
        return saved

    # The sweeper's pick of the work in progress for the sidebar (SPEC VIEW-17); a put replaces the list.
    @app.put("/api/in-progress")
    def put_in_progress(body: InProgressIn, request: Request, conn: Conn) -> dict[str, Any]:
        entries = [entry.model_dump() for entry in body.entries]
        return {"entries": store.put_in_progress(conn, request.state.actor, entries)}

    @app.post("/api/sweeps/start")
    def sweep_start(body: SweepStart, request: Request, conn: Conn) -> dict[str, Any]:
        return store.start_sweep(conn, request.state.actor, body.reorder)

    @app.post("/api/sweeps/finish")
    def sweep_finish(body: SweepFinish, request: Request, conn: Conn) -> dict[str, Any]:
        return store.finish_sweep(conn, body.sweep_id, body.summary, body.error, request.state.actor)

    @app.post("/api/sweeps/request")
    def sweep_request(body: SweepRequest, request: Request, conn: Conn) -> dict[str, str]:
        sweeper.request(conn, request.state.actor, body.reason)
        return {"reason": body.reason}

    @app.post("/api/sweeps/run", dependencies=[Depends(user_only)])
    def sweep_run(background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        """Send the sweeper session a full sweep, unless a sweep or check is under way."""
        sweeper.resume(conn, "pressed Full sweep now")
        # Whether the sweeper's session still exists decides between resuming it and starting a new one.
        known = {session.session_id for session in session_cache.snapshot(wait=True).sessions}
        message = sweeper.launch(
            conn, "user", settings.sweep_prompt, settings.sweep_cwd, known.__contains__, "sweep", []
        )
        background.add_task(deliverer.send, message["id"])
        return message

    @app.post("/api/update", dependencies=[Depends(user_only)])
    def update(background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        """The user's Update: the sweeper checks the top of the queue, or sweeps when the queue is empty."""
        sweeper.resume(conn, "pressed Update")
        # Whether the sweeper's session still exists decides between resuming it and starting a new one.
        known = {session.session_id for session in session_cache.snapshot(wait=True).sessions}
        message = sweeper.update(conn, settings.sweep_cwd, known.__contains__, settings.sweep_prompt)
        background.add_task(deliverer.send, message["id"])
        return message

    # The user's input on a page: sweeps slow down and stop while there is none (SPEC SWEEP-11), and
    # each minute with input counts as active time (SPEC TRACK-3).
    @app.post("/api/presence", dependencies=[Depends(user_only)])
    def presence(conn: Conn) -> dict[str, Any]:
        now = datetime.now(UTC)
        counted = sweeper.user_input(conn, now)
        timesheet.note_input(conn, now)
        return {"sweeps_paused": sweeper.paused(conn), "input_count": counted}

    # A page with the running timer on screen (SPEC TRACK-5): activity for the timer, not input (`store.note_seen`).
    @app.post("/api/presence/seen", dependencies=[Depends(user_only)])
    def presence_seen(conn: Conn) -> dict[str, Any]:
        return {"input_count": store.note_seen(conn, datetime.now(UTC))}

    @app.post("/api/sweeps/resume", dependencies=[Depends(user_only)])
    def sweep_resume(conn: Conn) -> dict[str, bool]:
        return {"resumed": sweeper.resume(conn, "pressed Restart on the page")}

    def comment_since() -> str:
        return (datetime.now(UTC) - timedelta(days=SPEC_COMMENT_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def doc_view(doc: spec.Doc, conn: sqlite3.Connection) -> dict[str, Any]:
        text = spec.read(doc)
        blocks = spec.page_blocks(text, doc.key_prefix)
        comments = store.doc_comments(conn, doc.key_prefix, comment_since())
        for block in blocks:
            block["comments"] = [comment for comment in comments if comment["target"] == block["key"]]
        placed = {block["key"] for block in blocks}
        return {
            "version": spec.version(text),
            "text": text,
            "path": str(doc.repo / doc.file),
            "blocks": blocks,
            "unplaced_comments": [comment for comment in comments if comment["target"] not in placed],
        }

    @app.get("/api/spec")
    def get_spec(conn: Conn) -> dict[str, Any]:
        return doc_view(spec.spec_doc(settings.spec_repo, settings.spec_branch), conn)

    @app.post("/api/spec/part")
    def edit_spec_part(body: SpecPart, request: Request) -> dict[str, Any]:
        doc = spec.spec_doc(settings.spec_repo, settings.spec_branch)
        return spec.edit_part(doc, body.old, body.new, request.state.actor, datetime.now(UTC))

    @app.put("/api/spec")
    def edit_spec(body: SpecWhole, request: Request) -> dict[str, Any]:
        doc = spec.spec_doc(settings.spec_repo, settings.spec_branch)
        return spec.edit_all(doc, body.version, body.text, request.state.actor)

    # The user's skills (SPEC SKILLS-1) live in the configured skills repository and branch.
    @app.get("/api/skills")
    def get_skills(conn: Conn) -> dict[str, Any]:
        unread = Counter(
            comment["target"].split(":")[1]
            for comment in store.doc_comments(conn, "skill:", comment_since())
            if comment["read_at"] is None
        )
        found = spec.skills(settings.skills_repo, settings.skills_folder)
        for skill in found:
            skill["unread_comments"] = unread[skill["name"]]
        return {"skills": found, "path": str(settings.skills_repo / settings.skills_folder), "branch": settings.skills_branch}

    def skill_doc(name: str) -> spec.Doc:
        return spec.skill_doc(settings.skills_repo, settings.skills_folder, settings.skills_branch, name)

    @app.get("/api/skills/{name}")
    def get_skill(name: str, conn: Conn) -> dict[str, Any]:
        return doc_view(skill_doc(name), conn)

    @app.post("/api/skills/{name}/part", dependencies=[Depends(user_only)])
    def edit_skill_part(name: str, body: SpecPart) -> dict[str, Any]:
        doc = skill_doc(name)
        return spec.edit_part(doc, body.old, body.new, "user", datetime.now(UTC))

    @app.put("/api/skills/{name}", dependencies=[Depends(user_only)])
    def edit_skill(name: str, body: SpecWhole) -> dict[str, Any]:
        doc = skill_doc(name)
        return spec.edit_all(doc, body.version, body.text, "user")

    # Which controls the user presses and which hover texts they stop on (SPEC UI-45).

    @app.post("/api/control-uses", dependencies=[Depends(user_only)])
    def post_control_uses(body: ControlUsesIn, conn: Conn) -> dict[str, int]:
        uses = [use.model_dump() for use in body.uses]
        return {"recorded": control_uses.record(conn, body.batch, body.page, uses, datetime.now(UTC))}

    @app.get("/api/control-uses")
    def get_control_uses(conn: Conn, days: Annotated[int, Query(ge=1, le=365)]) -> dict[str, Any]:
        return control_uses.report(conn, days, datetime.now(UTC))

    # Cards that left the user's page within a minute of showing (SPEC PERM-24).

    @app.post("/api/flashes", dependencies=[Depends(user_only)])
    def post_flash(body: FlashIn, conn: Conn) -> dict[str, Any]:
        stored = flashes.record(
            conn, body.kind, body.ref, body.label, body.page, body.shown_at, body.gone_at, datetime.now(UTC)
        )
        return {"id": stored["id"] if stored else None}

    @app.get("/api/flashes")
    def get_flashes(conn: Conn, days: Annotated[int, Query(ge=1, le=365)]) -> dict[str, Any]:
        return {"flashes": flashes.listing(conn, days, datetime.now(UTC))}

    # Errors Cockpit's own pages met (SPEC CODE-2).

    @app.post("/api/page-errors", dependencies=[Depends(user_only)])
    def post_page_error(body: PageErrorIn, conn: Conn) -> dict[str, Any]:
        recorded = page_errors.record(
            conn, body.page, body.place, body.message[:2000], body.stack[:8000], body.count, body.loaded_at
        )
        return {"id": recorded["id"], "count": recorded["count"]}

    @app.get("/api/page-errors")
    def get_page_errors(conn: Conn, include_taken: Annotated[bool, Query(alias="all")]) -> dict[str, Any]:
        return page_errors.listing(conn, include_taken)

    @app.post("/api/page-errors/take")
    def take_page_errors(body: PageErrorsTake, conn: Conn) -> dict[str, int]:
        return {"taken": page_errors.take(conn, sorted(set(body.ids)), body.note)}

    @app.get("/api/patterns")
    def get_patterns(conn: Conn, days: Annotated[int, Query(ge=1, le=90)]) -> dict[str, Any]:
        return patterns.summary(conn, days, datetime.now(UTC))

    # Claude use per session and per skill at API list prices and as a share of a weekly limit (SPEC SESSION-9),
    # the accounts' own weekly meters, Codex use (SPEC SESSION-10), and the last DAILY_DAYS of use and closed steps. `period` is "week", since the earliest start
    # of the accounts' current weeks, or a number of days.
    @app.get("/api/spend")
    def get_spend(conn: Conn, period: Annotated[str, Query(pattern=r"^(week|[0-9]{1,2})$")]) -> dict[str, Any]:
        now = datetime.now(UTC)
        limits = usage.read(
            settings.claude_rate_limits, now, accounts.in_use(settings.claude_accounts, []), accounts.preference(conn, now),
            running(session_cache.get()),
        )
        if period == "week":
            starts = [datetime.fromisoformat(account["week_start"]) for account in limits["accounts"] if account["week_start"]]
            since = min(starts, default=now - timedelta(days=7))
        elif 1 <= int(period) <= spend.HORIZON_DAYS:
            since = now - timedelta(days=int(period))
        else:
            raise store.Invalid(f"period is week or 1 to {spend.HORIZON_DAYS} days, not {period}")
        result = spend_index.summary(since)
        names = store.shown_session_names(conn, session_cache.get())
        for row in result["sessions"]:
            row["name"] = names.get(row["session_id"]) or row["title"] or row["first_prompt"]
        # The graph per day (SPEC SESSION-14) covers its own days: Claude use per hour and the steps closed.
        daily_since = now - timedelta(days=spend.DAILY_DAYS)
        daily = {"since": daily_since.strftime("%Y-%m-%dT%H:%M:%SZ"), "hours": spend_index.hours(daily_since),
                 "codex_hours": codex_index.hours(daily_since), "steps": store.steps_closed(conn, daily_since)}
        return result | {"period": period, "limits": limits, "codex": codex_index.summary(since, now), "daily": daily}

    # The user's time per day, task and kind of step (SPEC TRACK-3); the days are the user's, in `tz`.
    @app.get("/api/time")
    def get_time(conn: Conn, days: Annotated[int, Query(ge=1, le=90)], tz: str) -> dict[str, Any]:
        try:
            zone = ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise store.Invalid(f"unknown time zone {tz!r}; send an IANA name like Europe/Amsterdam") from error
        return timesheet.report(conn, days, datetime.now(UTC), zone)

    return app
