"""Cockpit's HTTP server: the page, the JSON API, and the Google sign-in."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import AsyncGenerator, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import BackgroundTasks, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from starlette.background import BackgroundTask
from starlette.routing import Match

from cockpit import (
    accounts,
    actions,
    activity,
    auth,
    briefing,
    codex,
    codex_login,
    commands,
    config,
    connector_signin,
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
    request_stats,
    resources,
    schedules,
    secret_files,
    server_logs,
    sessions,
    sidechat,
    snooze_words,
    spec,
    spend,
    stages,
    state_sync,
    store,
    sweeper,
    timesheet,
    top_cards,
    usage,
    zones,
)
from cockpit import parts as installed_parts
from cockpit.codex_agent import CodexError, ProviderSessions
from cockpit.config import Account, user_name
from cockpit.parts import Context, Factory, Part, Tags
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
# The places in index.html that take the parts' stylesheets and scripts (parts.py).
PART_STYLES = "<!-- part styles -->"
PART_SCRIPTS = "<!-- part scripts -->"
# The pages a part may add tags to.
PART_PAGES = ("index.html", "usage.html")
STATIC_KEPT = "public, max-age=31536000, immutable"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Browsers fetch an app's manifest without its cookies, so installing needs the manifest open to all.
MANIFEST_PATH = "/manifest.webmanifest"
# The Mac's installer holds no secret: the line the user pastes carries a one-time code, which the installer trades
# for the Mac's key (SPEC TRACK-9).
ACTIVITY_INSTALL_PATH = "/activity/install.sh"
ACTIVITY_MINUTES_PATH = "/api/activity/minutes"
ACTIVITY_CLAIM_PATH = "/api/activity/claim"
ACTIVITY_DISCONNECT_PATH = "/api/activity/disconnect"


def activity_install_line(origin: str, code: str) -> str:
    """The line the user pastes in Terminal on the Mac. Its code works once, for a short while, so the shell's history
    keeps nothing that still works."""
    return f"curl -fsSL {origin}{ACTIVITY_INSTALL_PATH} | bash -s -- {code}"
PUBLIC_PATHS = frozenset({"/healthz", MANIFEST_PATH, auth.SIGN_IN_PATH, auth.CALLBACK_PATH, ACTIVITY_INSTALL_PATH})
# A public page's link (SPEC API-5): exactly the shape `secrets.token_urlsafe(32)` makes, so nothing else is served
# on the public host.
PUBLIC_PAGE_PATH = re.compile(r"/p/[A-Za-z0-9_-]{43}")
# A visitor to a page shared with specific people (SPEC API-7) signs in with Google on the public host, through these.
PUBLIC_SIGN_IN_PATH = re.compile(r"/p/[A-Za-z0-9_-]{43}/sign-in")
PUBLIC_SIGN_OUT_PATH = "/p/sign-out"
# Everything after /p/, query too: a link holds the page's token, and Google's answer to a visitor's sign-in a code.
PUBLIC_PAGE_LINK = re.compile(r"/p/\S*")
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
# Cockpit's helper pages, which open in the layers over its main page: a page or the briefing from a step's link
# (SPEC NEXT-47, `&overlay=1`), and Briefing, Pages, Usage, Spec and Skills from the logo menu (`?center=1`).
# Cockpit's own pages may frame them; no other site may.
FRAMED_BY_COCKPIT = SECURITY_HEADERS["Content-Security-Policy"].replace(
    "frame-ancestors 'none'", "frame-ancestors 'self'"
)
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
# A public page (SPEC API-5) opens on a host of its own, with no sign-in there, so a hostile page can't pose as Cockpit.
# Its visitors never asked for it to reach anywhere, so it loads nothing from elsewhere, opens no other window and starts
# no download. The sandbox stays, so public pages can't read each other's saved data. allow-modals lets it ask OK or
# Cancel. A script can still leave the page, taking what a visitor typed with it; no header stops that.
PUBLIC_PAGE_CSP = (
    "sandbox allow-scripts allow-modals; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "font-src data:; img-src data: blob:; media-src data: blob:; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)
PUBLIC_PAGE_GONE = "This page isn't shared any more, or the link is wrong."
# The page that asks a visitor to sign in, or says their account isn't on the list. It names no page and runs no script.
PUBLIC_DOOR_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)
PUBLIC_DOOR_STYLE = (
    "body{font:16px/1.5 system-ui,sans-serif;margin:0;min-height:100vh;display:grid;place-items:center;"
    "background:#f6f6f4;color:#1d1d1b}main{max-width:26rem;margin:16px;padding:28px;background:#fff;border-radius:14px;"
    "box-shadow:0 1px 3px #0002}h1{font-size:1.2rem;margin:0 0 8px}p{margin:0 0 18px;color:#55554f}"
    ".go{display:inline-block;padding:10px 16px;border-radius:9px;background:#1d1d1b;color:#fff;text-decoration:none;"
    "font-weight:600}form{display:inline}button{font:inherit;background:none;border:0;color:#55554f;"
    "text-decoration:underline;cursor:pointer;margin-left:14px;padding:0}"
    "@media (prefers-color-scheme:dark){body{background:#151514;color:#ececea}main{background:#222220}"
    "p,button{color:#b4b4ae}.go{background:#ececea;color:#151514}}"
)
PAGE_TITLE = r"^[^\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069]+$"


class HidePublicLinks(logging.Filter):
    """Keeps a public page's secret link out of the server's log of requests (uvicorn's access logger)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(PUBLIC_PAGE_LINK.sub("/p/…", arg) if isinstance(arg, str) else arg for arg in record.args)
        return True


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
    # The connector sign-ins waiting for the address the user pastes, a file each (connector_signin.py).
    signins_dir: Path
    auth_mode: str = "google"
    local_login_token: str = ""
    providers: tuple[str, ...] = ("claude",)
    default_provider: str = "claude"
    # Where public pages open (SPEC API-5), a host of their own; "" turns them off.
    public_origin: str = ""


# Request bodies. Fields left out of an upsert keep their stored value.
SessionId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]
Url = Annotated[str, StringConstraints(pattern=r"^https?://\S+$", max_length=2000)]
Text = Annotated[str, StringConstraints(max_length=20000)]
Line = Annotated[str, StringConstraints(max_length=300)]
# A model, effort or account name as config.SESSION_OPTIONS and config.ACCOUNTS spell them.
OptionName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9.-]{0,63}$")]
# Ids of images on the message's task, sent with the words (store.attach_message_images checks them).
MessageImages = Annotated[list[int], Field(max_length=store.ITEM_IMAGES_MAX)]


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


# One of the links a step is about, a chip above its answers; `store._check_core_links` checks the list.
class CoreLink(Body):
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=store.CORE_LINK_CHARS)]
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
    # With `until` on an agent: why the work rests until then, in a few words ("check the reminder after the build"); the
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
                f' "check the reminder after the build" or "reminder you asked for". {user_name()} reads it on the row\'s'
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
    # Work that repeats, such as a review every few days: the sidebar lists it under Background (SPEC BG-3).
    recurring: bool | None = None
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
    nullable = frozenset({"item_id", "item_key", "session_id", "command", "permission", "closes", "expires_at"})

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
    # The links the step is about, shown as chips above its answers; [] clears them.
    core_links: list[CoreLink] | None = None
    # An early result or question while the session works on (SPEC NEXT-42): it takes a place only when no other
    # step waits for one.
    early: bool | None = None
    # The time after which the step is of no use, such as when its sign-in link stops working (SPEC NEXT-60); null
    # clears it.
    expires_at: str | None = None
    # The agent's one line on what a rewrite changed, shown on the card when the user looked at it before (SPEC
    # NEXT-61); not stored on the step.
    changed: Annotated[str, StringConstraints(max_length=100)] | None = None

    _expires_at = field_validator("expires_at")(_utc_time)


class StepChecked(Body):
    # What the agent checked, e.g. "PR open, CI green on the latest commit, no new comments".
    note: Annotated[str, StringConstraints(min_length=1, max_length=300)]


class ItemMerge(Body):
    # The item that joins another, and the item it joins: an id or a key each.
    side: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    into: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    # Why the two are one task, shown with the merge on the joined card (SPEC NEXT-55).
    note: Annotated[str, StringConstraints(min_length=1, max_length=300)]


class SnoozeWords(Body):
    # When the step or warning should come back, in the user's own words (SPEC VIEW-29), and the page's IANA timezone.
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    tz: Annotated[str, StringConstraints(min_length=1, max_length=64)]

    @field_validator("tz")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"unknown time zone {value!r}; send an IANA name like Europe/Amsterdam") from error
        return value


class StepActionBody(Body):
    nullable = frozenset({"until", "session_id"})

    until: str | None = None  # snooze: required; null wakes the step
    direction: Literal["up", "down"] | None = None  # move: required
    note: Line | None = None  # done or dismiss: why, shown with the step in Finished this week (SPEC VIEW-4, VIEW-20)
    # The user's Done: the step's session as their page showed it, null when it showed none. The page sends it, so
    # Done archives the session the card named (SPEC SESSION-4); an agent's done archives nothing and sends none.
    session_id: SessionId | None = None
    # snooze: the user's own words for `until`, which then is a stand-in time while Claude reads them (SPEC VIEW-29).
    words: SnoozeWords | None = None

    _until = field_validator("until")(_utc_time)


class NewSessionStart(Body):
    """Where a press on a step with no session starts its new session (SPEC NEXT-18), as the step's Send does: the
    checkout and the start picks (SPEC SESSION-22), each pick null for the default. The page sends them with every
    press; they count only while the step has no session."""

    nullable = frozenset({"session_id", "new_cwd", "provider", "model", "effort", "account", "speed"})

    new_cwd: str | None
    provider: Literal["claude", "codex"] | None
    model: OptionName | None
    effort: OptionName | None
    account: OptionName | None
    speed: OptionName | None

    def start(self) -> dict[str, Any]:
        return {"new_cwd": self.new_cwd, "provider": self.provider, "model": self.model, "effort": self.effort,
                "account": self.account, "speed": self.speed}


class StepRun(NewSessionStart):
    # The command, its folder and the session the output goes to (null for none), as the user's page showed them.
    command: CommandText
    cwd: CommandFolder
    session_id: SessionId | None


class StepAllow(StepPermission, NewSessionStart):
    # The session told, as the user's page showed it (null for none).
    session_id: SessionId | None


class HandOff(Body):
    nullable = frozenset({"session_id", "new_cwd", "provider", "model", "effort", "account", "speed"})

    text: Text
    # Images on the step's item that go with the words (SPEC MSG-18).
    images: MessageImages = []
    # Both left out: the step's own session.
    session_id: SessionId | None = None
    new_cwd: str | None = None
    # A new session's options (SPEC SESSION-22), each null for the default; store.start_options checks them.
    provider: Literal["claude", "codex"] | None = None
    model: OptionName | None = None
    effort: OptionName | None = None
    account: OptionName | None = None
    speed: OptionName | None = None


class SecretIn(HandOff):
    # The secret's file name and its value (SPEC MSG-15); `text` is what the user wrote with it, maybe nothing.
    name: Annotated[str, StringConstraints(pattern=secret_files.NAME_PATTERN)]
    value: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=secret_files.VALUE_LIMIT)]


class SigninStart(Body):
    # A session's `cockpit signin start` (SPEC SESSION-25): the account it runs on, the connector and the item.
    account: OptionName
    server: Annotated[str, StringConstraints(pattern=connector_signin.SERVER_NAME.pattern)]
    item_key: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    why: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
    session_id: SessionId


class SigninFinish(Body):
    # The address the user's browser landed on after approving; it goes to the sign-in server and nowhere else.
    address: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]


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
    # Images on the item that go with the last message (SPEC MSG-18).
    images: MessageImages = []
    # Why the user closed the item; empty for complete.
    reason: Annotated[str, StringConstraints(max_length=500)]
    # The step's session as the user's page showed it; null when it showed none.
    session_id: SessionId | None


class StepCloseTasks(Body):
    # The tasks on the step's list that the user left ticked: none only on a finish step, whose own task closes.
    item_ids: Annotated[list[int], Field(max_length=store.CLOSES_MAX)]


class AwayAnswer(Body):
    # The question answered: the `id` of the state's `away`.
    question: str
    # Add: the time without activity counts too; otherwise Return.
    add: bool


class AwayReturned(Body):
    # The question the user answered with Return: the `id` of the state's `away` then.
    question: str


class AwayStop(Body):
    # The end of the user's last activity the page saw before the stretch without any.
    since: str
    # How many of the user's inputs the page knew of: the state's `input.count` it measured the stretch by, or a later
    # count its own input made (`input_count` in the answer to a presence report, a start or an answer).
    seen: Annotated[int, Field(ge=0)]

    _since = field_validator("since")(_utc_time)


PRESS_ID = r"^[A-Za-z0-9-]{8,64}$"


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


class GoalEntry(Body):
    # The goal the entry is; null for a new goal.
    nullable: ClassVar[frozenset[str]] = frozenset({"id"})
    id: int | None
    text: Text
    paused: bool


class GoalsIn(Body):
    goals: list[GoalEntry]


class GoalMove(Body):
    position: Annotated[int, Field(ge=1)]


class WrongModelDismissal(Body):
    session_id: str
    model: str


class WrongModelInvestigation(Body):
    session_id: str
    model: str


class WarningSnooze(Body):
    nullable = frozenset({"until"})

    # An account problem's key holds its whole text.
    key: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    until: str | None  # null wakes the warning
    # The user's own words for `until`, which then is a stand-in time while Claude reads them (SPEC VIEW-29).
    words: SnoozeWords | None = None

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
    # How a session finds the part again (pick.js describe), "" when the page has nothing more to say.
    detail: Annotated[str, StringConstraints(max_length=2000)]


class WarningMessage(Body):
    # The warning's key, as a snooze names it (WarningSnooze).
    key: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    # What the user wrote on the warning, in the user's words.
    text: Annotated[str, StringConstraints(min_length=1, max_length=store.ADD_MAX_CHARS)]
    # The warning as the card showed it: its place on the page and its words.
    part: PickedPart


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
    nullable = frozenset({"part", "provider", "model", "effort", "account", "speed", "priority"})

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
    speed: OptionName | None = None
    # Fire and forget (SPEC CONTROL-24): its session works to the end on its own and closes the item itself; a page
    # loaded before this option sends none.
    fire_and_forget: bool = False
    # The priority the user picked in the form (SPEC CONTROL-10), or null for none: the words of the text then set
    # it; a page loaded before this option sends none.
    priority: Literal["urgent", "high", "normal", "low"] | None = None


class ItemAddMore(Body):
    # More of the user's words for a to-do they added (SPEC CONTROL-18), and the images they put on it with them.
    text: Text
    images: MessageImages


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
    nullable = frozenset({"item_id", "step_id", "session_id", "new_cwd", "provider", "model", "effort", "account", "speed"})

    item_id: int | None = None
    step_id: int | None = None
    session_id: SessionId | None = None
    new_cwd: str | None = None
    # A new session's options (SPEC SESSION-22), each null for the default; store.start_options checks them.
    provider: Literal["claude", "codex"] | None = None
    model: OptionName | None = None
    effort: OptionName | None = None
    account: OptionName | None = None
    speed: OptionName | None = None


class MessageIn(Body):
    text: Text
    target: Target
    # Images the user put on the message's task to go with the words (SPEC MSG-18); agents send none.
    images: MessageImages = []
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
    # How a session finds a picked part again (pick.js describe), "" for feedback on no part.
    detail: Annotated[str, StringConstraints(max_length=2000)]
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


class ControlPress(Body):
    at: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$")]
    control: Line
    item_id: int | None
    step_id: int | None

    # A header control has neither a task nor a step, and a control on a step can lack its task.
    nullable = frozenset({"item_id", "step_id"})


class ControlUsesIn(Body):
    batch: Line
    page: Line
    uses: list[ControlUse] = Field(max_length=500)
    # A page loaded before presses were sent sends none until it reloads.
    presses: list[ControlPress] = Field(default=[], max_length=500)


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
    nullable = frozenset({"away_ms"})
    page: Line
    place: Line
    message: Text
    stack: Text
    count: int = Field(ge=1, le=100000)
    # When the page that met the error loaded (ISO time): a page loaded before a fix runs the old code.
    loaded_at: Line
    # For an error from Cockpit being away, how long before this try the page first met it away; null otherwise.
    # Pages loaded before it was added send none.
    away_ms: int | None = Field(default=None, ge=0)

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


class ZoneIn(Body):
    # An IANA zone name, such as Asia/Jakarta: the one the browser reports, or the one the user picked.
    zone: str


class TeamIn(Body):
    # The people the briefing follows, in the user's words; "" for no one.
    team: str


class TopCardIn(Body):
    # A top card (SPEC NEXT-65): top_cards.put checks the fields.
    need: str
    title: str
    body: str = ""
    answers: list[str] = []
    ends_at: str | None = None
    session_id: str | None = None


class ScheduleIn(Body):
    # A scheduled prompt (SPEC BG-14): schedules.put checks the fields. `day` is for a weekly one only.
    name: str
    prompt: str
    cwd: str
    title: str
    project: str
    every: str
    at: str
    day: str | None = None


class SchedulePause(Body):
    paused: bool


class TopCardDone(Body):
    answer: str | None = None


class ProjectIn(Body):
    # A project (SPEC VIEW-34): created by its name, or changed; a field left out stays, a null checkout clears it
    # (SESSION-24). projects.put checks them.
    nullable = frozenset({"checkout"})
    name: str
    about: str | None = None
    logo: str | None = None
    checkout: str | None = None


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
    speed: OptionName = "standard"


class EmergencySeen(Body):
    """The emergency session as the page that asks last read it: Stop and Close act only on that one."""
    nullable: ClassVar[frozenset[str]] = frozenset({"session_id"})
    session_id: str | None
    count: Annotated[int, Field(ge=0)]


class EmergencyClose(EmergencySeen):
    # Whether the user agreed to end an answer that is still running.
    end_answer: bool


class PageIn(Body):
    # No line breaks or text-direction marks: the title appears in the question that makes a page public, and those could
    # push the question's own words out of view.
    title: Annotated[str, StringConstraints(min_length=1, max_length=300, pattern=PAGE_TITLE)]
    # A whole HTML document; store.PAGE_MAX_BYTES bounds it.
    html: Annotated[str, StringConstraints(min_length=1)]


class PublishIn(Body):
    # The version the user is looking at: a page that changed since then is not made public.
    revision: int


class SharingIn(Body):
    # Who can open the page at its link (SPEC API-8).
    access: Literal["me", "people", "anyone"]
    # The version the user is looking at: a page that changed since then is not shared.
    revision: int | None = None
    # When given, the whole list of people (SPEC API-7) in place of the old one.
    people: Annotated[list[Annotated[str, StringConstraints(max_length=254)]], Field(max_length=50)] | None = None


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


class ActivityMinute(Body):
    # One minute on the Mac (SPEC TRACK-9): its UTC start, the app in front, the browser's host or null, and away.
    nullable: ClassVar[frozenset[str]] = frozenset({"site"})
    minute: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d\d-\d\dT\d\d:\d\d$")]
    app: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    site: Annotated[str, StringConstraints(pattern=r"^[a-z0-9.-]{1,253}$")] | None
    away: StrictBool

    @field_validator("minute")
    @classmethod
    def _real_minute(cls, value: str) -> str:
        # A minute like 25:00 would be stored, then break folding it into its day once it is old.
        datetime.strptime(value, "%Y-%m-%dT%H:%M")
        return value


class ActivityMinutes(Body):
    # A day's worth at most: the Mac keeps what it could not send and sends it with the next minute. Each is checked
    # as an ActivityMinute on its own, so one bad minute is refused and the rest are stored.
    minutes: Annotated[list[Any], Field(max_length=1440)]



class ActivityPause(Body):
    paused: StrictBool


class ActivityClaim(Body):
    code: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class ActivityCategory(Body):
    name: Annotated[str, StringConstraints(min_length=1, max_length=253)]
    category: Literal["work", "neutral", "distraction"]


async def _image_body(request: Request) -> bytes:
    """The request body, read no further than one byte past the image limit, so the store refuses an oversized one."""
    content = bytearray()
    async for chunk in request.stream():
        content += chunk
        if len(content) > store.IMAGE_MAX_BYTES:
            break
    return bytes(content)


def with_part_tags(page: str, tags: list[Tags]) -> str:
    """The page with the parts' stylesheets and scripts in place of its two markers; with no parts, nothing there."""
    for marker in (PART_STYLES, PART_SCRIPTS):
        if page.count(marker) != 1:
            raise ValueError(f"a page that takes part tags needs {marker} exactly once")
    styles = "\n  ".join(f'<link rel="stylesheet" href="/static/{path}">' for one in tags for path in one.styles)
    scripts = "\n  ".join(f'<script src="/static/{path}" defer></script>' for one in tags for path in one.scripts)
    return page.replace(PART_STYLES, styles).replace(PART_SCRIPTS, scripts)


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
    parts: list[Factory],
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
    app.state.requests = request_stats.Recorder()
    # When this process started, and began to stop (server.StopNoting), so a page error from the restart is told apart.
    app.state.started_at = datetime.now(UTC)
    app.state.stopping_at = None
    key = settings.cookie_key
    # The page compares this with the version it loaded with and offers Reload when they differ.
    version = build_version(PACKAGE_DIR)
    app.state.version = version
    # The build version alone would not do: a deploy installs the new files before it restarts Cockpit, so the old
    # process can serve them under its own version, and a rollback to that build would then use them.
    static_tag = f"{version}-{secrets.token_hex(4)}"
    claude_sign_in = logins.SignIn(False)
    codex_sign_in = codex_login.SignIn()
    local = settings.auth_mode == "local"
    public_host = urlsplit(settings.public_origin).netloc.lower()
    session_cookie = auth.LOCAL_SESSION_COOKIE if local else auth.SESSION_COOKIE
    cookie_attributes = auth.LOCAL_COOKIE_ATTRIBUTES if local else auth.COOKIE_ATTRIBUTES
    sign_in_path = auth.LOCAL_SIGN_IN_PATH if local else auth.SIGN_IN_PATH

    def caller(request: Request) -> str | None:
        if auth.bearer_matches(request.headers.get("authorization"), settings.token):
            return "agent"
        value = request.cookies.get(session_cookie)
        valid = auth.local_session_cookie_valid if local else auth.session_cookie_valid
        if value is not None and valid(value, key, settings.email, time.time()):
            if local and request.scope["path"].startswith("/api/"):
                expected = auth.local_page_secret(key, value)
                if not hmac.compare_digest(request.headers.get("x-cockpit-local", "").encode(), expected.encode()):
                    return None
            return "user"
        return None

    # Added before the guard, so it runs inside it and sees who signed in.
    unrecorded_paths: list[re.Pattern[str]] = []
    app.add_middleware(actions.Once, db_path=settings.db_path, unrecorded_paths=unrecorded_paths)
    # POST routes that check a key of their own, with who sends there; the parts add theirs (parts.Context).
    own_key_posts = {ACTIVITY_MINUTES_PATH: "mac", ACTIVITY_CLAIM_PATH: "mac", ACTIVITY_DISCONNECT_PATH: "mac"}

    @app.middleware("http")
    async def guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        # When the request arrived, before any wait for the database (a timer's pause counts back from it).
        request.state.arrived = datetime.now(UTC)
        # What the router matches, decoded, so the gate and the router can't read a path differently.
        path = request.scope["path"]
        if local and (
            request.url.netloc != settings.origin.removeprefix("http://")
            or request.client is None or request.client.host not in {"127.0.0.1", "::1"}
        ):
            return _error(403, "local mode accepts only direct loopback requests to its configured host")
        # The public host (SPEC API-5, API-7) answers shared pages and a visitor's Google sign-in, nothing else. Cockpit's
        # own cookie and tokens count for nothing here; only the visitor cookie, which names an address and nothing more.
        if public_host and request.headers.get("host", "").lower() == public_host:
            request.state.me = None
            request.state.actor = None
            reading = request.method in {"GET", "HEAD"}
            if (
                (reading and PUBLIC_PAGE_PATH.fullmatch(path) is not None)
                or (request.method == "GET" and PUBLIC_SIGN_IN_PATH.fullmatch(path) is not None)
                or (request.method == "GET" and path == auth.VISITOR_CALLBACK_PATH)
                or (request.method == "POST" and path == PUBLIC_SIGN_OUT_PATH
                    and request.headers.get("origin") == settings.public_origin)
            ):
                response = await call_next(request)
            else:
                response = PlainTextResponse("Not found", status_code=404, headers={"Cache-Control": "no-store"})
            for header, value in SECURITY_HEADERS.items():
                response.headers.setdefault(header, value)
            return response
        # The Mac sends its minutes with its own key, which the route checks; no cookie or other token counts there,
        # so it needs no Origin either (SPEC TRACK-9). A part's route with a key of its own is the same.
        if request.method == "POST" and path in own_key_posts:
            request.state.me = None
            request.state.actor = own_key_posts[path]
            response = await call_next(request)
            # The route checks the key only once the body is read, so a refused request may come from anyone: its
            # sender is then nobody, and its refusal stays in the request counts and adds no event.
            if response.status_code >= 400:
                request.state.actor = None
            for header, value in SECURITY_HEADERS.items():
                response.headers.setdefault(header, value)
            response.headers.setdefault("Cache-Control", "no-store")
            return response
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

    def route_template(request: Request) -> str | None:
        """The template of the route that answers this request, or None when no route matches its path."""
        route = request.scope.get("route")
        if route is not None:
            return route.path
        # The guard refuses before the router runs, and static files are a mount, which sets no route: both are
        # named by the route or mount whose path matches. A method the route lacks still names it. A mount moves the
        # scope's root to its own path and keeps the app's in app_root_path, so the match starts from the app's.
        scope = dict(request.scope) | {"root_path": request.scope.get("app_root_path", request.scope.get("root_path", ""))}
        for candidate in app.router.routes:
            if candidate.matches(scope)[0] is not Match.NONE:
                return getattr(candidate, "path", None)
        return None

    def count_request(request: Request, status: int, started: float) -> None:
        name = route_template(request) or "(none)"
        caller = getattr(request.state, "me", None) or "anonymous"
        ms = round((time.perf_counter() - started) * 1000)
        app.state.requests.add(datetime.now(UTC), request.method, name, status, caller, ms)

    def record_refusal_logged(request: Request, actor: str, status: int, body: bytes) -> None:
        try:
            record_refusal(request, actor, status, body)
        except Exception:
            log.exception("could not record a refused %s %s", request.method, request.scope["path"])

    def record_refusal(request: Request, actor: str, status: int, body: bytes) -> None:
        try:
            detail = json.loads(body).get("detail", "")
        except ValueError:
            detail = body.decode(errors="replace")
        # The route's template, not its path: a path can hold a public page's secret link.
        data = {"method": request.method, "route": route_template(request),
                "status": status, "detail": str(detail)[:500]}
        conn = db.connect(settings.db_path)
        try:
            patterns.record(conn, "request.refused", actor, None, None, None, data)
        finally:
            conn.close()

    # Inside the compression and outside the guard, so it counts and times every answer, the guard's refusals included
    # (SPEC TRACK-13). A refused write is an event too: an agent's reason shows where the skill reads unclear, the
    # user's where the page offered what Cockpit then refused.
    @app.middleware("http")
    async def measure(request: Request, call_next):  # type: ignore[no-untyped-def]
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except BaseException:
            count_request(request, 500, started)
            raise
        count_request(request, response.status_code, started)
        # The guard names an agent's write once it has the agent's name, and the user's once signed in. A write by the
        # agent sign-in without a name can be the page itself opened under that sign-in, whose own presence and press
        # reports the guard refuses: the request counts keep those, under the caller "agent".
        actor = getattr(request.state, "actor", None) or ("user" if getattr(request.state, "me", None) == "user" else None)
        if request.method in SAFE_METHODS or not 400 <= response.status_code < 500 or actor is None:
            return response
        body = b"".join([chunk async for chunk in response.body_iterator])
        # Recorded once the answer has gone, so a busy database delays only the event, not the caller.
        answer = Response(body, status_code=response.status_code,
                          background=BackgroundTask(record_refusal_logged, request, actor, response.status_code, body))
        answer.raw_headers = list(response.raw_headers)
        return answer

    # Added last, so it wraps every answer. The state is a large JSON document, which the page asks for at each poll
    # and each return; the lightest level cuts most of its size quickly, where the heaviest takes several times as
    # long for little more. Images and event streams pass as they are.
    app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=1)

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

    @app.exception_handler(sessions.LookupFailed)
    async def lookup_failed(_: Request, exc: sessions.LookupFailed) -> JSONResponse:
        # list_sessions logged the listing's failure, as an error unless it timed out.
        log.warning("a request could not look up a session: %s", exc)
        return _error(503, f"listing Claude sessions failed, so Cockpit cannot tell whether the session runs: {exc}")

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

    @app.get(MANIFEST_PATH)
    def app_manifest() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "manifest.webmanifest",
            media_type="application/manifest+json",
            headers={"Cache-Control": "no-cache"},
        )

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
        elif expected is None or not state or not hmac.compare_digest(expected[0].encode(), state.encode()) or not code:
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

    # Each page says which Cockpit sent it. Its Reload (UI-13) compares against this, not against the first answer it
    # happens to get, which may already come from a newer Cockpit.
    def with_version(page: str) -> str:
        return page.replace("<head>", f'<head>\n  <meta name="cockpit-version" content="{version}">', 1)

    # Built for each request rather than sent as a file: a file's ETag could answer 304 and keep an old version.
    def plain_page(name: str) -> HTMLResponse:
        return HTMLResponse(with_version((STATIC_DIR / name).read_text()), headers={"Cache-Control": "no-cache"})

    # The helper pages also open in the layers over Cockpit's main page, so Cockpit's own pages may frame them.
    def framed_page(name: str) -> HTMLResponse:
        text = (STATIC_DIR / name).read_text()
        if name in PART_PAGES:
            tags = [part.page_tags[name] for part in built if name in part.page_tags]
            text = with_part_tags(text, tags)
            text = STATIC_REFERENCE.sub(rf'\1?v={static_tag}"', text)
        return HTMLResponse(with_version(text), headers={
            "Cache-Control": "no-cache", "Content-Security-Policy": FRAMED_BY_COCKPIT,
        })

    # The install's parts (parts.py). Their routes go in after Cockpit's own, at the end.
    context = Context(
        user_only=user_only,
        framed_page=framed_page,
        session_cache=session_cache,
        claude_settings=settings.claude_settings,
        db_path=settings.db_path,
        unrecorded_paths=unrecorded_paths,
        own_key_posts=own_key_posts,
        origin=settings.origin,
    )
    built: list[Part] = [factory(context) for factory in parts]
    for part in built:
        unknown = sorted(set(part.page_tags) - set(PART_PAGES))
        if unknown:
            raise ValueError(f"a part has tags for pages that take none: {', '.join(unknown)}")
    index_tags = [part.page_tags["index.html"] for part in built if "index.html" in part.page_tags]

    def cockpit_page(layout: str) -> HTMLResponse:
        # CodeMirror creates its own styles. A fresh style-only nonce permits
        # those without allowing inline scripts or arbitrary inline styles.
        nonce = secrets.token_urlsafe(24)
        page = with_part_tags((STATIC_DIR / "index.html").read_text(), index_tags)
        page = with_version(STATIC_REFERENCE.sub(rf'\1?v={static_tag}"', page))
        options = installed_parts.page_options()
        marks = f'data-layout="{layout}"'
        if options:
            for name, value in options.marks.items():
                marks += f" {name}" if value is None else f' {name}="{html.escape(value, quote=True)}"'
            prefixes = html.escape(json.dumps(options.unrecorded_prefixes), quote=True)
            marks += f' data-unrecorded-prefixes="{prefixes}"'
        page = page.replace("__COCKPIT_STYLE_NONCE__", nonce).replace("<body>", f"<body {marks}>", 1)
        policy = SECURITY_HEADERS["Content-Security-Policy"] + f"; style-src 'self' 'nonce-{nonce}'"
        if options and options.frame_origins:
            policy += "; frame-src 'self' " + " ".join(options.frame_origins)
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
    def spec_page() -> HTMLResponse:
        return framed_page("spec.html")

    @app.get("/briefing")
    def briefing_page() -> HTMLResponse:
        return framed_page("briefing.html")

    @app.get("/usage")
    def usage_page() -> HTMLResponse:
        return framed_page("usage.html")

    @app.get("/server")
    def server_page() -> HTMLResponse:
        return framed_page("server.html")

    # The Pages view also opens in the layer over Cockpit's main page (SPEC NEXT-47, `&overlay=1`), so Cockpit's own
    # pages may frame it. It names its files with the main page's tag, so the layer takes them from the browser's
    # cache, which already has most of them from the main page, and asks Cockpit only for the page itself.
    @app.get("/pages")
    def pages_page() -> HTMLResponse:
        page = with_version(STATIC_REFERENCE.sub(rf'\1?v={static_tag}"', (STATIC_DIR / "pages.html").read_text()))
        return HTMLResponse(page, headers={"Cache-Control": "no-cache", "Content-Security-Policy": FRAMED_BY_COCKPIT})

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
            emergency.send(body.session_id, body.count, body.text, provider, body.account, model, effort, body.speed, settings.checkouts_dir)  # ty: ignore[invalid-argument-type]  # All three picks are set above.
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
        return blocked_groups(permissions.open_requests(
            conn, now, Path.home(), permissions.auto_sentences(settings.claude_settings), dialogs
        ))

    def blocked_groups(cards: list[dict]) -> dict[str, list[dict]]:
        """The open requests by the list the page shows each in."""
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

    def opened_items(request: Request) -> set[int]:
        """?opened=12,40: the items a part keeps off the main page that the page opened by their id."""
        opened = request.query_params.get("opened", "")
        if not re.fullmatch(r"(\d+(,\d+)*)?", opened):
            raise store.Invalid(f"opened is a comma-separated list of item ids, not {opened!r}")
        return {int(item_id) for item_id in opened.split(",") if item_id}

    @app.get("/api/listed")
    def listed(request: Request, conn: Conn) -> dict[str, Any]:
        """The ids of what the page lists in Next up, Blocked and waiting and the queue, in a fraction of the state's
        time: a page back from the background shows its cards again once none of them has gone, rather than
        "Updating…" until the whole state came (SPEC UI-60)."""
        now = datetime.now(UTC)
        opened = opened_items(request)
        hidden = installed_parts.hidden_items(conn)
        sessions = session_cache.snapshot(wait=False).sessions
        dialogs = store.open_dialogs(sessions)
        # The requests without their cards' text, which takes most of the state's time to work out.
        blocked = blocked_groups(permissions.open_rows(conn, now, dialogs))
        waiting = [session.session_id for session in store.at_dialogs(sessions)]
        return {
            "steps": store.listed_steps(conn, now, opened),
            # All work, including items a part keeps off the main page only when opened by their id.
            "items": [
                row["id"] for row in db.rows(conn.execute(
                    "SELECT id, archived, stage, updated_at, done_at, pinned_at FROM items WHERE archived = 0"
                ).fetchall())
                if store.on_all_work(row, now) and (row["id"] not in hidden or row["id"] in opened)
            ],
            **{key: [card["id"] for card in cards] for key, cards in blocked.items()},
            "codex_requests": [request["id"] for request in deliverer.codex.requests()] if deliverer.codex is not None else [],
            "waiting_sessions": dialog_waiters(conn, now, dialogs, waiting, blocked["permission_requests"]),
        }

    # The pages other than the main one ask only this, to say when a newer Cockpit is running (SPEC UI-13).
    @app.get("/api/version")
    def running_version() -> dict[str, str]:
        return {"version": version}

    state_versions = state_sync.StateVersions()

    @app.get("/api/state")
    def state(request: Request, conn: Conn) -> Response:
        now = datetime.now(UTC)
        # A tab in the background asks with ?background=1: it keeps its cards current, but the user is not looking.
        if request.state.me == "user" and "background" not in request.query_params:
            sweeper.user_seen(conn, now)
        listing = session_cache.snapshot(wait=False)
        sessions = listing.sessions
        dialogs = store.open_dialogs(sessions)
        warnings = listing.warnings + schedules.warnings(conn)
        result = store.page_state(conn, request.state.me, sessions, warnings, settings.checkouts_dir, opened_items(request))
        result |= blocked_lists(conn, now, dialogs)
        result["account_problems"] = [
            {"text": problem.text, "detail": problem.detail} for problem in listing.account_problems
        ]
        result["attach"] = settings.attach
        result["goals_summary"] = history.goals_summary(
            conn, [goal["text"] for goal in result["goals"] if goal["paused_at"] is None]
        )
        result["projects"] = projects.listing(conn)
        result["providers"] = list(settings.providers)
        result["default_provider"] = settings.default_provider
        # The user's home folder, which a blocked call's folder shows as `~` (SPEC PERM-26).
        result["home"] = str(Path.home())
        # What a new session may start with (SPEC SESSION-22): each enabled provider's models and efforts, its
        # defaults, and the Claude accounts to pick from.
        result["session_options"] = {
            provider: {
                "models": [{"name": name, "label": label, "speeds": config.session_speeds(provider, name)}
                           for name, label in config.SESSION_OPTIONS[provider]["models"]],
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
        # Sessions using a custom API server have no usage on the local subscription accounts.
        claude_usage = {"accounts": [], "error": ""} if accounts.on_api_server(settings.claude_settings) else usage.read(
            settings.claude_rate_limits, now, accounts.in_use(settings.claude_accounts, []), accounts.preference(conn, now),
            running(sessions),
        )
        result["usage"] = claude_usage | {
            "codex": codex_index.weeks(now),
            "machine": machine.read() | {"tasks_now": resources.now_tasks(conn)},
        }
        spent = spend_index.per_session(now)
        for row in result["sessions"]:
            row["spend"] = spent.get(row["session_id"])
        names = store.shown_session_names(conn, sessions)
        result["wrong_models"] = [
            row | {"name": names.get(row["session_id"]) or row["title"] or row["first_prompt"]}
            for row in store.not_dismissed(conn, spend_index.wrong_models(now, store.started_models(conn)))
        ]
        result["snoozed_warnings"] = store.snoozed_warnings(conn, now)
        result["snooze_reads"] = store.snooze_reads(conn, now)
        result["handed_warnings"] = store.handed_warnings(conn)
        result["top_cards"] = top_cards.showing(conn, now)
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
        for part in built:
            fields = part.state(now, request.state.me == "user")
            taken = sorted(fields.keys() & result.keys())
            if taken:
                raise ValueError(f"a part's state fields are already in the state: {', '.join(taken)}")
            result |= fields
        since = request.headers.get("x-cockpit-since")
        if since or request.headers.get("x-cockpit-sync") == "1":
            return state_versions.response(result, since, request.state.me)
        return JSONResponse(result)

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
        stored = conn.execute("SELECT id, key, title FROM items WHERE key = ?", (fields["key"],)).fetchone()
        store.refuse_long_stored_title(stored, fields, request.state.actor)
        return store.upsert_item(conn, fields, request.state.actor)

    # Two items that are one task become one (SPEC NEXT-55); held while the user is on a step of either.
    @app.post("/api/items/merge")
    def merge_items(body: ItemMerge, request: Request, conn: Conn) -> dict[str, Any]:
        return store.merge_item(conn, body.side, body.into, body.note, request.state.actor)

    @app.patch("/api/items/{item_id}")
    def patch_item(item_id: int, body: ItemFields, request: Request, conn: Conn) -> dict[str, Any]:
        fields = body.model_dump(exclude_unset=True)
        store.check_agent_title(fields, request.state.actor)
        store.check_agent_waiting(fields, request.state.actor)
        stored = conn.execute("SELECT id, key, title FROM items WHERE id = ?", (item_id,)).fetchone()
        store.refuse_long_stored_title(stored, fields, request.state.actor)
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
        start = {"provider": body.provider, "model": body.model, "effort": body.effort, "account": body.account, "speed": body.speed}
        return store.add_item(
            conn, body.text, body.part.model_dump() if body.part else None, body.pinned, start, body.fire_and_forget,
            body.priority,
        )

    # More words for a to-do the user added (SPEC CONTROL-18): they join its text and go to its session. A session
    # still starting gets them once it is known, as a follow-up does.
    @app.post("/api/items/{item_id}/add-more", dependencies=[Depends(user_only)])
    def add_more(item_id: int, body: ItemAddMore, background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        try:
            added = store.add_more(conn, item_id, body.text, body.images, settings.checkouts_dir)
        except store.Conflict:
            first = conn.execute(
                "SELECT new_cwd FROM messages WHERE item_id = ? AND author = 'user' AND new_cwd IS NOT NULL"
                " AND state = 'sending' ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
            if first is None:
                raise
            deliverer.wait_for_start(first[0])
            added = store.add_more(conn, item_id, body.text, body.images, settings.checkouts_dir)
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

    # A session sees that a page is public, never its link, so a session can't pass the link on. The token leaves only
    # inside the link, for the user, who asks for it with `link`.
    def page_out(page: dict[str, Any], link: bool = False) -> dict[str, Any]:
        token = page["public_token"]
        # `public`: someone besides the user can open the page at its link (SPEC API-8).
        public = page["access"] != "me" and token is not None
        fields = {key: value for key, value in page.items() if key != "public_token"}
        return fields | {
            "url": f"{settings.origin}/pages/{page['id']}",
            "public": public,
            # Sharing a page again brings back a link sent before, so Share says so.
            "link_kept": token is not None and not public,
            "public_url": f"{settings.public_origin}/p/{token}" if link and public and settings.public_origin else None,
            # Whom the user shared it with stays between Cockpit and the user.
            "people": page["people"] if link else None,
        }

    # Every page on every item, open or closed, for the Pages tab (SPEC API-4).
    @app.get("/api/pages")
    def list_pages(request: Request, conn: Conn) -> dict[str, Any]:
        link = request.state.me == "user"
        return {
            "pages": [page_out(page, link) for page in store.all_pages(conn, datetime.now(UTC))],
            "public_pages": bool(settings.public_origin),
        }

    @app.post("/api/items/{item_id}/pages")
    def add_item_page(item_id: int, body: PageIn, request: Request, conn: Conn) -> dict[str, Any]:
        return page_out(store.add_item_page(conn, item_id, body.title, body.html, request.state.actor))

    @app.put("/api/item-pages/{page_id}")
    def update_item_page(page_id: int, body: PageIn, request: Request, conn: Conn) -> dict[str, Any]:
        return page_out(store.update_item_page(conn, page_id, body.title, body.html, request.state.actor))

    @app.delete("/api/item-pages/{page_id}")
    def remove_item_page(page_id: int, request: Request, conn: Conn) -> dict[str, Any]:
        return page_out(store.remove_item_page(conn, page_id, request.state.actor))

    def sharing_allowed() -> None:
        if local:
            raise store.Conflict("local mode answers only this computer, so a shared link would reach nobody")
        if not settings.public_origin:
            raise store.Conflict("sharing is off: set public_origin in the setup file to turn it on")

    # Who can open a page at its link (SPEC API-5, API-7, API-8). Only the user shares, on the page, at the version they
    # are reading; an agent may only choose 'me', which stops the sharing.
    @app.put("/api/item-pages/{page_id}/sharing")
    def set_page_sharing(page_id: int, body: SharingIn, request: Request, conn: Conn) -> dict[str, Any]:
        if body.access != "me" or body.people is not None:
            user_only(request)
            sharing_allowed()
        page = store.set_page_sharing(conn, page_id, body.access, body.revision, body.people, request.state.actor)
        return page_out(page, link=request.state.me == "user")

    # Before the Share menu, the page made itself public here; it stays for anything that still asks this way.
    @app.post("/api/item-pages/{page_id}/public")
    def publish_item_page(page_id: int, body: PublishIn, request: Request, conn: Conn) -> dict[str, Any]:
        user_only(request)
        sharing_allowed()
        return page_out(store.set_page_sharing(conn, page_id, "anyone", body.revision, None, request.state.actor),
                        link=True)

    # Stopping is the safe direction, so an agent can do it too (`cockpit page unpublish`).
    @app.delete("/api/item-pages/{page_id}/public")
    def unpublish_item_page(page_id: int, request: Request, conn: Conn) -> dict[str, Any]:
        page = store.set_page_sharing(conn, page_id, "me", None, None, request.state.actor)
        return page_out(page, link=request.state.me == "user")

    # New link ends the old link for good. Only the user asks, since the answer holds the new link.
    @app.post("/api/item-pages/{page_id}/public/new-link")
    def renew_public_link(page_id: int, request: Request, conn: Conn) -> dict[str, Any]:
        user_only(request)
        return page_out(store.renew_public_link(conn, page_id, request.state.actor), link=True)

    def on_public_host(request: Request) -> bool:
        return bool(public_host) and request.headers.get("host", "").lower() == public_host

    visitor_key = auth.visitor_key(key)
    # Lax, so the cookie comes along when Google sends the visitor back and when they open a link from an email. A page's
    # own requests carry none: the page runs sandboxed in an origin of its own.
    visitor_cookie_attributes = "Path=/; Secure; HttpOnly; SameSite=Lax"

    def gone() -> Response:
        return PlainTextResponse(PUBLIC_PAGE_GONE, status_code=404, headers={"Cache-Control": "no-store"})

    def door(title: str, body_html: str, status: int) -> HTMLResponse:
        """The public host's own small page: sign in, or not on the list. It names no page."""
        return HTMLResponse(
            f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<meta name=robots content='noindex'><title>{html.escape(title)}</title><style>{PUBLIC_DOOR_STYLE}</style>"
            f"<main>{body_html}</main>",
            status_code=status,
            # same-origin rather than no-referrer, so the Sign out form sends its real Origin, which the public
            # host checks; a no-referrer form post sends "Origin: null".
            headers={"Content-Security-Policy": PUBLIC_DOOR_CSP, "Cache-Control": "no-store",
                     "X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "same-origin"},
        )

    def admits(people: list[str], email: str) -> bool:
        listed = {auth.canonical_email(person) for person in [*people, settings.email]}
        return auth.canonical_email(email) in listed

    # Open to whoever the page is shared with, on the public host only. It answers with the page's HTML and nothing
    # about the item it sits on. A page shared with specific people first asks the visitor to sign in with Google.
    @app.api_route("/p/{token}", methods=["GET", "HEAD"])
    def show_public_page(token: str, request: Request, conn: Conn) -> Response:
        page = store.shared_page(conn, token) if on_public_host(request) else None
        if page is None:
            return gone()
        if page["access"] == "people":
            email = auth.visitor_cookie_email(request.cookies.get(auth.VISITOR_COOKIE), visitor_key, time.time())
            sign_in = f"/p/{token}/sign-in"
            if email is None:
                return door("Sign in to open this page", (
                    "<h1>This page is shared with specific people</h1>"
                    "<p>Sign in with your Google account to open it.</p>"
                    f"<a class=go href='{sign_in}'>Sign in with Google</a>"
                ), 401)
            if not admits(page["people"], email):
                return door("Not shared with this account", (
                    "<h1>This page isn't shared with you</h1>"
                    f"<p>You're signed in as {html.escape(email)}. Ask whoever sent the link to add this address,"
                    " or sign in with another account.</p>"
                    f"<a class=go href='{sign_in}'>Use another account</a>"
                    f"<form method=post action='{PUBLIC_SIGN_OUT_PATH}'><button>Sign out</button></form>"
                ), 403)
        return HTMLResponse(page["html"], headers={
            "Content-Security-Policy": PUBLIC_PAGE_CSP, "Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow",
        })

    @app.get("/p/{token}/sign-in")
    def visitor_sign_in(token: str, request: Request, conn: Conn) -> Response:
        page = store.shared_page(conn, token) if on_public_host(request) and settings.google else None
        if page is None or page["access"] != "people":
            return gone()
        assert settings.google is not None
        start = auth.start_visitor_sign_in(settings.google, settings.public_origin, token)
        response = RedirectResponse(start.redirect_url, status_code=303)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Set-Cookie"] = (
            f"{auth.VISITOR_SIGN_IN_COOKIE}={start.cookie_value}; Max-Age={auth.SIGN_IN_MAX_AGE}; "
            f"{visitor_cookie_attributes}"
        )
        return response

    @app.get(auth.VISITOR_CALLBACK_PATH)
    def visitor_callback(request: Request) -> Response:
        if not on_public_host(request) or settings.google is None:
            return gone()
        expected = auth.parse_visitor_sign_in_cookie(request.cookies.get(auth.VISITOR_SIGN_IN_COOKIE))
        state = request.query_params.get("state", "")
        code = request.query_params.get("code", "")
        clear = f"{auth.VISITOR_SIGN_IN_COOKIE}=; Max-Age=0; {visitor_cookie_attributes}"
        again = "<p>Open the link you were sent again to try once more.</p>"
        if request.query_params.get("error"):
            response = door("Sign-in cancelled", f"<h1>Sign-in cancelled</h1>{again}", 400)
        elif expected is None or not state or not hmac.compare_digest(expected[0].encode(), state.encode()) or not code:
            response = door("Sign-in expired", f"<h1>Sign-in expired or was not started here</h1>{again}", 400)
        else:
            email = None
            try:
                claims = auth.id_token_claims(settings.exchange(
                    settings.google, code, settings.public_origin + auth.VISITOR_CALLBACK_PATH, expected[1]
                ))
            except auth.SignInError as exc:
                log.error("a visitor's sign-in failed: %s", exc)
            else:
                email = auth.visitor_email(claims, settings.google.client_id, time.time(), settings.email)
            if email is None:
                response = door("Sign-in failed", f"<h1>Google couldn't confirm your address</h1>{again}", 502)
            else:
                expires = int(time.time()) + auth.VISITOR_MAX_AGE
                response = RedirectResponse(f"/p/{expected[2]}", status_code=303)
                response.headers["Cache-Control"] = "no-store"
                response.headers["Set-Cookie"] = (
                    f"{auth.VISITOR_COOKIE}={auth.visitor_cookie_value(visitor_key, email, expires)}; "
                    f"Max-Age={auth.VISITOR_MAX_AGE}; {visitor_cookie_attributes}"
                )
        response.headers.append("Set-Cookie", clear)
        return response

    @app.post(PUBLIC_SIGN_OUT_PATH)
    def visitor_sign_out(request: Request) -> Response:
        if not on_public_host(request):
            return gone()
        response = door("Signed out", "<h1>Signed out</h1><p>Open the link you were sent to sign in again.</p>", 200)
        response.headers["Set-Cookie"] = f"{auth.VISITOR_COOKIE}=; Max-Age=0; {visitor_cookie_attributes}"
        return response

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
        if fields["need"].startswith(connector_signin.NEED_PREFIX):
            raise store.Invalid("a sign-in step comes from `cockpit signin start`, which makes its link")
        if fields.get("permission") is not None:
            wanted = fields["permission"]
            wanted["rule"] = permissions.check_rule(wanted["list"], wanted["rule"])
        # One transaction, so a hand-off cannot land between the check and the put.
        with db.transaction(conn):
            actor = request.state.actor
            stored = conn.execute("SELECT id, key, title FROM items WHERE id = ?", (fields["item_id"],)).fetchone()
            store.refuse_long_stored_title(stored, {}, actor)
            store.refuse_answer_elsewhere(conn, fields, actor)
            store.refuse_second_card(conn, fields, actor)
            store.refuse_unexplained_rewrite(conn, fields, actor)
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
                  "model": body.model, "effort": body.effort, "account": body.account, "speed": body.speed}
        return store.hand_off_step(conn, step_id, body.text, target, body.images, settings.checkouts_dir)

    # A secret for the step's session (SPEC MSG-15): its value goes to a file of its own, and the step goes to the
    # session with a message that says only where the file is. It goes at once: Take back could not unwrite the file.
    @app.post("/api/steps/{step_id}/secret", dependencies=[Depends(user_only)])
    def hand_secret(step_id: int, body: SecretIn, background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        sweeper.resume(conn, "handed off a step")
        target = {"session_id": body.session_id, "new_cwd": body.new_cwd, "provider": body.provider,
                  "model": body.model, "effort": body.effort, "account": body.account, "speed": body.speed}
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

    # A connector sign-in (SPEC SESSION-25): a session asks for one, and the user finishes it on the card.
    @app.post("/api/signins")
    def start_signin(body: SigninStart, request: Request, conn: Conn) -> dict[str, Any]:
        session_only(request)
        account = next((each for each in settings.claude_accounts if each.name == body.account), None)
        if account is None:
            names = ", ".join(each.name for each in settings.claude_accounts)
            raise store.Invalid(f"no Claude account {body.account!r}; Cockpit has {names}")
        item = conn.execute("SELECT id FROM items WHERE key = ?", (body.item_key,)).fetchone()
        if item is None:
            raise store.NotFound(f"no item with key {body.item_key}")
        return connector_signin.start(
            conn, settings.signins_dir, account, body.server, item["id"], body.why, body.session_id, request.state.actor
        )

    @app.post("/api/steps/{step_id}/signin", dependencies=[Depends(user_only)])
    def finish_signin(step_id: int, body: SigninFinish, background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        sweeper.resume(conn, "handed off a step")
        done = connector_signin.finish_step(
            conn, settings.signins_dir, settings.claude_accounts, settings.checkouts_dir, step_id, body.address
        )
        if "message" in done:
            store.release_held(conn)
            background.add_task(deliverer.send, done["message"]["id"])
        return done

    @app.post("/api/steps/{step_id}/run", dependencies=[Depends(user_only)])
    def run_command(step_id: int, body: StepRun, conn: Conn) -> dict[str, Any]:
        sweeper.resume(conn, "ran a step's command")
        run, message = commands.start(
            conn, step_id, body.command, body.cwd, body.session_id, body.start(), settings.checkouts_dir
        )
        deliverer.start(
            f"command-run-{run['id']}",
            commands.execute, settings.db_path, settings.checkouts_dir, run["id"], message["id"], deliverer.send,
        )
        return {"run": run, "message": message}

    @app.post("/api/steps/{step_id}/allow", dependencies=[Depends(user_only)])
    def allow_step_permission(step_id: int, body: StepAllow, background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        sweeper.resume(conn, "allowed a step's permission")
        message = permissions.allow_step(
            conn, settings.claude_settings, settings.checkouts_dir, step_id, {"list": body.list, "rule": body.rule},
            body.session_id, body.start(),
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

    # A snooze time the user typed (SPEC VIEW-29). The snooze holds a stand-in time at once; this reads the words with
    # the user's coming week of events, after the response, and moves the snooze to that time or wakes the target.
    def snooze_by_words(
        conn: sqlite3.Connection, target: str, words: SnoozeWords, until: str, background: BackgroundTasks
    ) -> None:
        started = datetime.now(UTC)
        read_id = store.start_snooze_read(conn, target, words.text, until, started)
        background.add_task(read_snooze_words, target, read_id, words, started)

    def read_snooze_words(target: str, read_id: str, words: SnoozeWords, started: datetime) -> None:
        reason = ""
        try:
            at = snooze_words.read(words.text, ZoneInfo(words.tz), started, installed_parts.calendar(started))
            until = at.strftime("%Y-%m-%dT%H:%M:%SZ")
        except snooze_words.Unread as exc:
            until, reason = None, str(exc)
        except Exception:
            log.exception("reading the snooze words %r failed", words.text)
            until, reason = None, "Cockpit could not read the time"
        conn = db.connect(settings.db_path)
        try:
            store.settle_snooze_read(conn, target, read_id, until, reason, datetime.now(UTC))
        finally:
            conn.close()

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

    # Undo of Return: the time without activity counts after all, as Add would have counted it.
    @app.post("/api/steps/{step_id}/timer/answer/undo", dependencies=[Depends(user_only)])
    def undo_away_return(step_id: int, body: AwayReturned, conn: Conn) -> dict[str, Any]:
        return store.undo_away_return(conn, step_id, body.question)

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
    # none. Once one closes, the step is done, and its session hears what closed and what stayed open. On a finish
    # step (need `complete…`) the list is the card's one close key: the press also completes the step's own task,
    # last, even with nothing ticked, and that finish closes the step and tells its session; when it fails, nothing
    # closes. Undo is each item's reopen route, then the step's; on a finish step the own task's reopen restores it.
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
            if not body.item_ids and not rank.finishing(step):
                raise store.Invalid(f"step {step_id} closes only the tasks it lists; tick at least one")
            for item_id in body.item_ids:
                if item_id not in whys:
                    raise store.Invalid(f"step {step_id} does not list item {item_id}")
                try:
                    with db.transaction(conn):
                        finishing = store.finish_step_for(conn, item_id, whys[item_id], step["prepared_by"])
                        seen = store.target_session(conn, item_id, finishing)
                        finish(conn, item_id, finishing, "complete", None, "", [], whys[item_id], seen, background)
                except (store.Conflict, store.Invalid, store.NotFound) as error:
                    refused.append({"item_id": item_id, "error": str(error)})
                else:
                    finished.append(item_id)
            if rank.finishing(step):
                # The own task's finish closes the step and tells its session (`finish_text`), and its reopen is
                # the Undo; each listed task's session heard of its own close.
                seen = store.target_session(conn, step["item_id"], step_id)
                finish(conn, step["item_id"], step_id, "complete", None, "", [], "", seen, background)
                finished.append(step["item_id"])
                step = store.get_step(conn, step_id)
            elif finished:
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
        fields.pop("words", None)
        if body.words is not None and (action != "snooze" or body.until is None or request.state.actor != "user"):
            raise store.Invalid("words come only with the user's snooze to a stand-in time")
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
            if request.state.actor == "user":
                if body.words is not None:
                    snooze_by_words(conn, f"step:{step_id}", body.words, step["snoozed_until"], background)
                else:
                    store.forget_snooze_read(conn, f"step:{step_id}", datetime.now(UTC))
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
        if (session is None or session.name in store.SWEEP_SESSION_NAMES
                or installed_parts.is_standing(session.session_id)):
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
        conn: sqlite3.Connection, item_id: int, step_id: int, how: str, answer: str | None, note: str,
        images: list[int], reason: str, seen_session: str | None, background: BackgroundTasks,
    ) -> dict[str, Any]:
        session_id = store.target_session(conn, item_id, step_id)
        store.check_seen_target(step_id, session_id, seen_session)
        session = deliverer.lookup(session_id) if session_id else None
        if session is not None and (
            session.name in store.SWEEP_SESSION_NAMES or installed_parts.is_standing(session.session_id)
        ):
            session = None
        finished = store.finish_item(
            conn, item_id, step_id, how, answer, note, images, reason, seen_session, session, settings.checkouts_dir,
        )
        if finished["send"] is not None:
            background.add_task(deliverer.send, finished["send"]["id"])
        briefing.read_on_finish(conn, finished["item"])
        return finished

    @app.post("/api/items/{item_id}/finish", dependencies=[Depends(user_only)])
    def finish_item(item_id: int, body: ItemFinish, background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        sweeper.resume(conn, "completed an item" if body.how == "complete" else "closed an item")
        return finish(
            conn, item_id, body.step_id, body.how, body.answer, body.note, body.images, body.reason, body.session_id,
            background,
        )

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

    # The whole list as the user left it in the Priorities editor, in order, each goal holding or paused (UI-65).
    @app.put("/api/goals", dependencies=[Depends(user_only)])
    def set_goals(body: GoalsIn, conn: Conn) -> list[dict[str, Any]]:
        sweeper.resume(conn, "changed their goals")
        return store.set_goals(conn, [(goal.id, goal.text, goal.paused) for goal in body.goals])

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
            body.tool_use_id, body.reason, asked_by, earlier, Path.home(),
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
            message = store.attach_message_images(conn, message, body.images)
            if request.state.me == "user" and message["item_id"] is not None:
                store.reopen_for_message(conn, message["item_id"])
        background.add_task(deliverer.send, message["id"])
        return message

    def copyable_session(session_id: str) -> Session:
        """A session Ask or Fork may copy; standing sessions keep their conversation in their own terminal."""
        if installed_parts.is_standing(session_id):
            raise store.Invalid(installed_parts.standing().copy_refusal)
        return listed_session(session_id)

    def listed_session(session_id: str) -> Session:
        found = next((entry for entry in session_cache.snapshot(wait=True).sessions if entry.session_id == session_id), None)
        if found is None:
            raise store.NotFound(f"session {session_id[:8]} is not on this machine")
        return found

    # Ask: a quick question about an item's work to a read-only copy of its session, which keeps working; the
    # question and answer then go to the session (SPEC MSG-11).
    @app.post("/api/items/{item_id}/ask", dependencies=[Depends(user_only)])
    def ask(item_id: int, body: AskIn, conn: Conn) -> dict[str, Any]:
        session = copyable_session(body.session_id)
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
        session = copyable_session(body.session_id)
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
    # One that waits for a place among the working sessions goes past the limit, and no session stops (SPEC SESSION-23).
    @app.post("/api/messages/{message_id}/send-now", dependencies=[Depends(user_only)])
    def send_now(message_id: int, background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        message = store.send_now_message(conn, message_id)
        background.add_task(
            deliverer.restart if message["capacity_since"] is None else deliverer.send_past_limit, message_id
        )
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
        if installed_parts.is_standing(session.session_id):
            raise store.Conflict(installed_parts.standing().keeps)
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
        account = claude_account(name)
        return claude_sign_in.start(account.name, [*accounts.claude_env(account), *settings.claude, "auth", "login", "--claudeai"])

    @app.post("/api/claude/{name}/code", dependencies=[Depends(user_only)])
    def claude_code(name: str, body: ClaudeCode) -> dict[str, Any]:
        account = claude_account(name)
        claude_sign_in.finish(account.name, body.attempt, body.code)
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
        if codex.on_own_provider(home):
            raise store.Invalid(f"Codex in {folder} uses a custom model provider; sign in with that provider")
        address = codex.email(home)
        codex_login.remember(conn, folder, address)
        codex_sign_in.start(home.name, [settings.codex, "login", "--device-auth"], {"CODEX_HOME": str(home)},
                            address or codex_login.held(conn).get(folder, ""), datetime.now(UTC))
        return {"ok": True}

    # The Server page (SPEC TRACK-16): processor time and memory per task and part over the last day, week or month,
    # what runs now, and every recorded day's totals.
    @app.get("/api/resources")
    def get_resources(conn: Conn, span: Annotated[Literal["day", "week", "month"], Query(alias="range")]) -> dict[str, Any]:
        now = datetime.now(UTC)
        zone = config.setup().timezone
        return {
            **resources.report(conn, span, now, zone, os.cpu_count() or 1, resources.memory_total_mb()),
            "now": resources.now_tasks(conn),
            "daily": resources.daily(conn, now, zone),
        }

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

    # The user's close on a wrong-model banner: it stays away while the session stays on that model (SESSION-20).
    @app.post("/api/wrong-models/dismiss", dependencies=[Depends(user_only)])
    def dismiss_wrong_model(body: WrongModelDismissal, conn: Conn) -> dict[str, Any]:
        now = datetime.now(UTC)
        rows = spend_index.wrong_models(now, store.started_models(conn))
        store.dismiss_wrong_model(conn, body.session_id, body.model, rows, now)
        return {"ok": True}

    # The user's Investigate on a wrong-model warning (SPEC SESSION-20): a to-do, as Add makes one, whose new session
    # in Cockpit's checkout finds out why the session changed model. The to-do now tracks it, so the warning closes,
    # as with the close button. Returns the to-do.
    @app.post("/api/wrong-models/investigate", dependencies=[Depends(user_only)])
    def investigate_wrong_model(body: WrongModelInvestigation, background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        now = datetime.now(UTC)
        rows = spend_index.wrong_models(now, store.started_models(conn))
        if rows is None:
            raise store.Invalid("Cockpit is still counting the sessions' calls after a restart; try again in a minute")
        row = next((row for row in rows if row["session_id"] == body.session_id and row["model"] == body.model), None)
        if row is None:
            raise store.NotFound(f"session {body.session_id[:8]} has no recent calls on {body.model}")
        sessions = session_cache.snapshot(wait=False).sessions
        name = store.shown_session_names(conn, sessions).get(row["session_id"]) or row["title"] or row["first_prompt"]
        sweeper.resume(conn, "investigates a session on the wrong model")
        item = store.add_item(conn, store.wrong_model_investigation(row, name), None, False, dict.fromkeys(
            ("provider", "model", "effort", "account")), False, None)
        with db.transaction(conn):
            message = store.create_message(
                conn, "user", item["summary"],
                {"item_id": item["id"], "step_id": None, "session_id": None, "new_cwd": str(settings.spec_repo)},
                settings.checkouts_dir,
            )
        store.dismiss_wrong_model(conn, row["session_id"], row["model"], rows, now)
        background.add_task(deliverer.send, message["id"])
        return item

    # The user's message on a warning in Inbox (SPEC UI-82): a to-do, as Add makes one with the warning attached,
    # whose new session in Cockpit's checkout acts on it. The warning is handed to that to-do and stays out of Inbox
    # while the to-do is open. Returns the to-do and the warnings handed now.
    @app.post("/api/warnings/message", dependencies=[Depends(user_only)])
    def message_on_warning(body: WarningMessage, background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        sweeper.resume(conn, "left a message on a warning")
        item = store.add_item(conn, body.text, body.part.model_dump(), False, dict.fromkeys(
            ("provider", "model", "effort", "account")), False, None)
        with db.transaction(conn):
            message = store.create_message(
                conn, "user", item["summary"],
                {"item_id": item["id"], "step_id": None, "session_id": None, "new_cwd": str(settings.spec_repo)},
                settings.checkouts_dir,
            )
        store.hand_warning(conn, body.key, item["id"])
        background.add_task(deliverer.send, message["id"])
        return {"item": item, "handed_warnings": store.handed_warnings(conn)}

    # The user's snooze on a warning in Inbox (SPEC UI-72): it stays away until its time, on every page.
    @app.post("/api/warnings/snooze", dependencies=[Depends(user_only)])
    def snooze_warning(body: WarningSnooze, background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        now = datetime.now(UTC)
        if body.until is not None and datetime.fromisoformat(body.until) <= now:
            raise store.Invalid("a warning snoozes until a time still to come")
        if body.words is not None and body.until is None:
            raise store.Invalid("words come only with a snooze to a stand-in time")
        with db.transaction(conn):
            store.snooze_warning(conn, body.key, body.until, now)
            if body.words is not None and body.until is not None:
                snooze_by_words(conn, f"warning:{body.key}", body.words, body.until, background)
            else:
                store.forget_snooze_read(conn, f"warning:{body.key}", now)
        return {"snoozed_warnings": store.snoozed_warnings(conn, now), "snooze_reads": store.snooze_reads(conn, now)}

    @app.post("/api/feedback", dependencies=[Depends(user_only)])
    def post_feedback(body: FeedbackIn, conn: Conn) -> dict[str, Any]:
        sweeper.resume(conn, "gave feedback")
        return store.add_feedback(
            conn, body.text, body.step_id, body.item_id, body.target, body.quote, body.detail, body.seconds
        )

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

    # The user's time zone (SPEC CONTROL-32): each page reports its browser's zone and learns whether to ask about
    # switching; the user switches, keeps the zone, or picks one in the menu.
    @app.get("/api/timezone", dependencies=[Depends(user_only)])
    def get_zone(conn: Conn) -> dict[str, Any]:
        return zones.state(conn, None, datetime.now(UTC))

    @app.post("/api/timezone/report", dependencies=[Depends(user_only)])
    def report_zone(body: ZoneIn, conn: Conn) -> dict[str, Any]:
        return zones.report(conn, body.zone, datetime.now(UTC))

    @app.post("/api/timezone/keep", dependencies=[Depends(user_only)])
    def keep_zone(body: ZoneIn, conn: Conn) -> dict[str, Any]:
        return zones.keep(conn, body.zone, datetime.now(UTC))

    @app.put("/api/timezone", dependencies=[Depends(user_only)])
    def put_zone(body: ZoneIn, conn: Conn) -> dict[str, Any]:
        return zones.put(conn, config.SETUP_PATH, body.zone, datetime.now(UTC))

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
        return projects.put(
            conn,
            request.state.actor,
            body.name,
            body.about,
            body.logo,
            "checkout" in body.model_fields_set,
            body.checkout,
            settings.checkouts_dir,
        )

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
        # Whether the sweeper's session still exists on its model decides between resuming it and starting a new one.
        known = {session.session_id for session in session_cache.snapshot(wait=True).sessions}
        exists = sweeper.resumable(conn, known.__contains__, spend_index.latest_model)
        message = sweeper.launch(conn, "user", settings.sweep_prompt, settings.sweep_cwd, exists, "sweep", [])
        background.add_task(deliverer.send, message["id"])
        return message

    @app.post("/api/update", dependencies=[Depends(user_only)])
    def update(background: BackgroundTasks, conn: Conn) -> dict[str, Any]:
        """The user's Update: the sweeper checks the top of the queue, or sweeps when the queue is empty."""
        sweeper.resume(conn, "pressed Update")
        # Whether the sweeper's session still exists on its model decides between resuming it and starting a new one.
        known = {session.session_id for session in session_cache.snapshot(wait=True).sessions}
        exists = sweeper.resumable(conn, known.__contains__, spend_index.latest_model)
        message = sweeper.update(conn, settings.sweep_cwd, exists, settings.sweep_prompt)
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
        presses = [press.model_dump() for press in body.presses]
        return {"recorded": control_uses.record(conn, body.batch, body.page, uses, presses, datetime.now(UTC))}

    @app.get("/api/control-uses")
    def get_control_uses(conn: Conn, days: Annotated[int, Query(ge=1, le=365)]) -> dict[str, Any]:
        return control_uses.report(conn, days, datetime.now(UTC))

    @app.get("/api/page-presses")
    def get_page_presses(
        conn: Conn, days: Annotated[int, Query(ge=1, le=90)], item: Annotated[int | None, Query()] = None
    ) -> dict[str, Any]:
        return control_uses.presses(conn, days, item, datetime.now(UTC))

    # What the server logged at warning and above (SPEC TRACK-12), and how its routes answered (SPEC TRACK-13).

    @app.get("/api/server-logs")
    def get_server_logs(conn: Conn, days: Annotated[int, Query(ge=1, le=90)]) -> dict[str, Any]:
        return server_logs.report(conn, days, datetime.now(UTC))

    @app.get("/api/request-stats")
    def get_request_stats(conn: Conn, days: Annotated[int, Query(ge=1, le=90)]) -> dict[str, Any]:
        return request_stats.report(conn, days, datetime.now(UTC))

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
        restart = body.away_ms is not None and page_errors.met_restart(
            datetime.now(UTC) - timedelta(milliseconds=body.away_ms), app.state.started_at, app.state.stopping_at
        )
        recorded = page_errors.record(
            conn, body.page, body.place, body.message[:2000], body.stack[:8000], body.count, body.loaded_at, restart
        )
        if recorded is None:
            return {"restart": True}
        return {"id": recorded["id"], "count": recorded["count"]}

    @app.get("/api/page-errors")
    def get_page_errors(conn: Conn, include_taken: Annotated[bool, Query(alias="all")]) -> dict[str, Any]:
        return page_errors.listing(conn, include_taken)

    @app.post("/api/page-errors/take")
    def take_page_errors(body: PageErrorsTake, conn: Conn) -> dict[str, int]:
        return {"taken": page_errors.take(conn, sorted(set(body.ids)), body.note)}

    @app.get("/api/patterns")
    def get_patterns(conn: Conn, days: Annotated[int, Query(ge=1, le=90)]) -> dict[str, Any]:
        now = datetime.now(UTC)
        report = patterns.summary(conn, days, now)
        requests = permissions.review(conn, days, now)
        return report | {"repeats": report["repeats"] + requests.pop("repeats"), "permissions": requests}

    # Claude use per session and per skill at API list prices and as a share of a weekly limit (SPEC SESSION-9),
    # the accounts' own weekly meters, Codex use (SPEC SESSION-10), and the last DAILY_DAYS of use and closed steps. `period` is "week", since the earliest start
    # of the accounts' current weeks, or a number of days.
    @app.get("/api/spend")
    def get_spend(conn: Conn, period: Annotated[str, Query(pattern=r"^(week|[0-9]{1,2})$")]) -> dict[str, Any]:
        now = datetime.now(UTC)
        # Keep the page's last completed reading across restarts while both indexes rebuild.
        cache_key = f"spend-report-v1:{period}" if period in ("week", "1", "7", "30") else None
        refreshing = spend_index.ready_at is None or codex_index.ready_at is None
        if refreshing and cache_key:
            saved = db.kv_get(conn, cache_key)
            if saved is not None:
                return saved | {"refreshing": True}
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
        report = result | {"period": period, "limits": limits, "codex": codex_index.summary(since, now), "daily": daily,
                           "refreshing": refreshing}
        if cache_key and not refreshing:
            db.kv_set(conn, cache_key, report)
        return report

    # The user's time per day, task and kind of step (SPEC TRACK-3); the days are the user's, in `tz`.
    @app.get("/api/time")
    def get_time(conn: Conn, days: Annotated[int, Query(ge=1, le=90)], tz: str) -> dict[str, Any]:
        try:
            zone = ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise store.Invalid(f"unknown time zone {tz!r}; send an IANA name like Europe/Amsterdam") from error
        return timesheet.report(conn, days, datetime.now(UTC), zone)

    # One day of the user's time as stretches, in the setup's time zone unless asked otherwise.
    @app.get("/api/time/stretches")
    def get_time_stretches(conn: Conn, day: str | None = None, tz: str | None = None) -> dict[str, Any]:
        try:
            zone = ZoneInfo(tz) if tz else config.setup().timezone
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise store.Invalid(f"unknown time zone {tz!r}; send an IANA name like Europe/Amsterdam") from error
        now = datetime.now(UTC)
        try:
            wanted = date.fromisoformat(day) if day else now.astimezone(zone).date()
        except ValueError as error:
            raise store.Invalid(f"day must look like 2026-01-15, not {day!r}") from error
        return timesheet.stretches(conn, wanted, now, zone)

    # What the user's Mac records each minute (SPEC TRACK-9, TRACK-10, TRACK-11).
    own_host = urlsplit(settings.origin).hostname or ""

    @app.post(ACTIVITY_MINUTES_PATH)
    def post_activity_minutes(request: Request, conn: Conn, body: ActivityMinutes) -> Response:
        if not activity.key_matches(conn, request.headers.get("authorization")):
            return _error(401, "this Mac's key does not match; press Connect a Mac in Cockpit for a new one")
        minutes, refused = [], []
        for each in body.minutes:
            try:
                minutes.append(ActivityMinute.model_validate(each).model_dump())
            except ValidationError as error:
                refused.append(f"{str(each)[:200]}: {error.errors()[0]['msg']}")
        found = activity.record(conn, minutes, datetime.now(UTC), config.setup().timezone)
        refused += found["refused"]
        if refused:
            # The recorder should send only good minutes; a refused one means it or this check is wrong.
            log.error("refused %d of the Mac's %d minutes; the first: %s", len(refused), len(body.minutes), refused[0])
        return JSONResponse(found | {"refused": len(refused), "why": refused[0] if refused else None})

    @app.get("/api/activity/day")
    def get_activity_day(conn: Conn, day: str | None = None) -> dict[str, Any]:
        # Always the setup's zone: minutes older than KEEP_DAYS are kept only as that zone's day totals.
        zone = config.setup().timezone
        now = datetime.now(UTC)
        try:
            wanted = date.fromisoformat(day) if day else now.astimezone(zone).date()
        except ValueError as error:
            raise store.Invalid(f"day must look like 2026-01-15, not {day!r}") from error
        return activity.day_summary(conn, wanted, now, zone, own_host)

    @app.get("/api/activity/now")
    def get_activity_now(conn: Conn) -> dict[str, Any]:
        return activity.now_summary(conn, datetime.now(UTC), own_host)

    @app.get("/api/items/{item_id}/activity")
    def get_item_activity(conn: Conn, item_id: int) -> dict[str, Any]:
        store.get_item(conn, item_id)
        return activity.item_breakdown(conn, item_id, datetime.now(UTC), own_host)

    @app.post("/api/activity/pause", dependencies=[Depends(user_only)])
    def post_activity_pause(conn: Conn, body: ActivityPause) -> dict[str, Any]:
        activity.set_paused(conn, body.paused, datetime.now(UTC))
        return activity.mac_state(conn)

    # The line the user pastes in Terminal, with a new one-time code; the Mac's key changes when the installer uses it.
    @app.post("/api/activity/key", dependencies=[Depends(user_only)])
    def post_activity_key(conn: Conn) -> dict[str, str]:
        return {"install": activity_install_line(settings.origin, activity.new_code(conn, datetime.now(UTC)))}

    # The installer trades the line's one-time code for the Mac's key; the code is the only thing checked here.
    @app.post(ACTIVITY_CLAIM_PATH)
    def post_activity_claim(conn: Conn, body: ActivityClaim) -> Response:
        key = activity.claim(conn, body.code, datetime.now(UTC))
        if key is None:
            return _error(401, "this line was used already or is over 15 minutes old; in Cockpit's Your time, press "
                               "New key and paste the new line")
        return JSONResponse({"key": key}, headers={"Cache-Control": "no-store"})

    # The uninstaller, with the Mac's own key, tells Cockpit to stop taking it.
    @app.post(ACTIVITY_DISCONNECT_PATH)
    def post_activity_disconnect(request: Request, conn: Conn) -> Response:
        if not activity.key_matches(conn, request.headers.get("authorization")):
            return _error(401, "this Mac's key does not match; it no longer works anyway")
        activity.disconnect(conn)
        return JSONResponse({"connected": False})

    @app.put("/api/activity/categories", dependencies=[Depends(user_only)])
    def put_activity_category(conn: Conn, body: ActivityCategory) -> dict[str, str]:
        activity.set_category(conn, body.name, body.category)
        return {"name": body.name, "category": body.category}

    @app.get(ACTIVITY_INSTALL_PATH)
    def get_activity_install() -> PlainTextResponse:
        script = (PACKAGE_DIR / "activity_install.sh").read_text().replace("__ORIGIN__", settings.origin)
        return PlainTextResponse(script, headers={"Cache-Control": "no-store"})

    # Scheduled prompts (SPEC BG-14): sessions put and remove them; only the user may pause one.
    @app.get("/api/schedules")
    def get_schedules(conn: Conn) -> list[dict[str, Any]]:
        return schedules.listing(conn, datetime.now(UTC), config.setup().timezone)

    @app.post("/api/schedules")
    def post_schedule(body: ScheduleIn, request: Request, conn: Conn) -> dict[str, Any]:
        return schedules.put(
            conn, body.model_dump(), request.state.actor, settings.checkouts_dir, datetime.now(UTC), config.setup().timezone,
        )

    @app.post("/api/schedules/{name}/pause", dependencies=[Depends(user_only)])
    def pause_schedule(name: str, body: SchedulePause, conn: Conn) -> dict[str, Any]:
        return schedules.pause(conn, name, body.paused, datetime.now(UTC), config.setup().timezone)

    @app.delete("/api/schedules/{name}")
    def delete_schedule(name: str, conn: Conn) -> dict[str, bool]:
        schedules.remove(conn, name)
        return {"removed": True}

    # Top cards: what the user should do now that is not a step on a task (SPEC NEXT-65).
    @app.get("/api/top-cards")
    def get_top_cards(conn: Conn) -> list[dict[str, Any]]:
        return top_cards.recent(conn, datetime.now(UTC))

    @app.post("/api/top-cards")
    def post_top_card(body: TopCardIn, request: Request, conn: Conn) -> dict[str, Any]:
        return top_cards.put(conn, body.model_dump(), request.state.actor)

    @app.post("/api/top-cards/{card_id}/done")
    def close_top_card(
        card_id: int, body: TopCardDone, request: Request, background: BackgroundTasks, conn: Conn,
    ) -> dict[str, Any]:
        message = None
        # One transaction, so of two closes at the same moment only the first finds the card open and writes the message.
        with db.transaction(conn):
            was_open = conn.execute("SELECT 1 FROM top_cards WHERE id = ? AND done_at IS NULL", (card_id,)).fetchone()
            card = top_cards.close(conn, card_id, request.state.actor, body.answer)
            # The user's answer goes to the session that asked. If that session is down, it waits like any other message.
            if was_open and request.state.me == "user" and card["session_id"]:
                words = f"answered {body.answer!r}" if body.answer else "tapped Done"
                message = store.create_message(
                    conn, "cockpit", f"{user_name()} {words} on your top card {card['need']}: {card['title']}",
                    {"session_id": card["session_id"]}, settings.checkouts_dir,
                )
        if message:
            background.add_task(deliverer.send, message["id"])
        return card

    # Only the user's page marks a card seen; a session reading the list must not count as the user seeing it.
    @app.post("/api/top-cards/{card_id}/seen", dependencies=[Depends(user_only)])
    def see_top_card(card_id: int, conn: Conn) -> dict[str, Any]:
        return top_cards.seen(conn, card_id)

    # After Cockpit's own routes, which so win a path both have.
    for part in built:
        part.register(app)

    return app
