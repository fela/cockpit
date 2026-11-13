"""Codex use, counted apart from Claude's (SPEC SESSION-10): tokens per Codex session and each Codex account's own
weekly limit.

Each Codex account signed in on the machine has a folder of its own (config.CODEX_HOMES, Codex's CODEX_HOME), and
Codex writes each session to `<folder>/sessions/YYYY/MM/DD/rollout-<time>-<session id>.jsonl`. The folder's
`auth.json` names the account: the address in its sign-in's ID token, which is all that is read of it. Each account
has its own meter, so everything below holds per account, and the summary puts the accounts together the way the
Claude accounts are: the use adds up, in shares of one account's week, and each meter stays apart.

A session file's first entry, `session_meta`, gives the session's folder (`cwd`) and how it started (`source`: `exec` for `codex exec`, `cli` for
the terminal, `subagent` for a subagent). After each model call an `event_msg` of type `token_count` gives the session's tokens so far
(`info.total_token_usage`) and the account's rate limits (`rate_limits.primary` and `.secondary`: the share used, the
window's length in minutes and when it resets). Input counts the cached input within it, and output the reasoning
within it. The same count can come twice, so each event adds what grew since the session's previous count.

Cockpit has no list price for Codex's models, so Codex use is shown in tokens and as a share of Codex's own weekly
limit. That share comes from the meter of the limit named `codex` (another, `codex_bengalfox`, always reads 0 %): the
meter moves in whole percents, and each rise is shared out over the calls since the rise before, by their tokens, so
use of the same account elsewhere counts on the calls here. Codex often resets the week early, to a new `resets_at`;
a week starts from 0 %, except the first one on record, which starts from its first reading. Sessions running at once
can read the meter a percent apart, so a week rises only past its highest reading.
The index reads each session file once and then only what was appended since, with the Claude transcripts.

The session files do not say how many usage-limit resets an account has banked. Codex's own app-server does, in its
answer to `account/rateLimits/read`, so a thread asks one for each signed-in account every RESETS_EVERY_SECONDS (read_resets).
That answer also gives the weekly meter, which, unlike a session file's reading, is known to be the signed-in account's.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cockpit import usage

log = logging.getLogger(__name__)

# Session files last written longer ago than this are not read, as for Claude (spend.HORIZON_DAYS).
HORIZON_DAYS = 35
WEEK_MINUTES = 7 * 24 * 60
# Readings of one week name the time it resets give or take a few seconds; weeks lie hours or days apart.
SAME_WEEK_SECONDS = 3600
KINDS = ("fresh", "cached", "output", "reasoning")
# Only the first line and the token counts are parsed; the lines between can run to megabytes.
TOKEN_COUNT = b'"type":"token_count"'
# When Cockpit starts, the latest weekly meter is read from the end of this many of the newest session files, so the
# usage display shows each account's week at once, not only after the first full count (SPEC UI-57).
LATEST_FILES = 5
LATEST_TAIL_BYTES = 1 << 20
RESETS_EVERY_SECONDS = 15 * 60
RESETS_TIMEOUT_SECONDS = 60
# Codex's words for a sign-in that no longer works, from its 0.166.0 source and a try on a folder with made-up tokens.
REFUSED = re.compile(r"\b401 Unauthorized\b|authentication required|log out and sign in again", re.IGNORECASE)
# A refusal is asked about once more after this, so one bad moment of the server shows no warning.
REFUSED_ASK_AGAIN_SECONDS = 30


def _tokens(total: dict[str, Any]) -> dict[str, int]:
    """Input read fresh and from the cache, output and the reasoning part of it."""
    cached = int(total.get("cached_input_tokens") or 0)
    return {
        "fresh": int(total.get("input_tokens") or 0) - cached, "cached": cached,
        "output": int(total.get("output_tokens") or 0), "reasoning": int(total.get("reasoning_output_tokens") or 0),
    }


@dataclass
class Session:
    session_id: str
    offset: int = 0
    cwd: str = ""
    source: str = ""
    # The tokens counted so far, and what each count added, by its time.
    total: dict[str, int] = field(default_factory=lambda: dict.fromkeys(KINDS, 0))
    added: list[tuple[str, dict[str, int]]] = field(default_factory=list)
    # The week each count's own line reported, by when it resets (epoch seconds); None without a reading.
    weeks: list[int | None] = field(default_factory=list)


def email(home: Path) -> str:
    """The address the account in `home` signed in with, from its ID token; "" without a sign-in. The token is
    not checked: the address only labels the account's meter."""
    try:
        token = json.loads((home / "auth.json").read_text())["tokens"]["id_token"]
        claims = token.split(".")[1]
        return str(json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4))).get("email") or "")
    except FileNotFoundError:
        return ""
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, binascii.Error) as exc:
        # The type only: a decode error's text quotes the file's bytes, which hold the tokens.
        log.error("reading the Codex sign-in in %s failed: %s", home, type(exc).__name__)
        return ""


def on_own_provider(home: Path) -> bool:
    """Whether Codex in `home` runs on a model provider its config.toml defines, which brings its own key: such a
    folder holds no sign-in (auth.json) of its own and has none to lose."""
    try:
        settings = tomllib.loads((home / "config.toml").read_text())
    except FileNotFoundError:
        return False
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.error("reading Codex's config.toml in %s failed: %s", home, exc)
        return False
    return settings.get("model_provider") in settings.get("model_providers", {})


def _weekly(limits: dict[str, Any]) -> list[dict[str, Any]]:
    """The windows of a token count's rate limits that are the `codex` limit's week."""
    return [
        window for window in (limits.get("primary"), limits.get("secondary"))
        if limits.get("limit_id") == "codex" and window and window.get("window_minutes") == WEEK_MINUTES
    ]


def _week(window: dict[str, Any], limits: dict[str, Any], stamp: str) -> dict[str, Any]:
    """One reading of the weekly meter as AccountIndex keeps it: {used, resets_at, plan, captured_at}."""
    return {
        "used": float(window["used_percent"]),
        "resets_at": datetime.fromtimestamp(window["resets_at"], UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "plan": str(limits.get("plan_type") or ""), "captured_at": stamp,
    }


def _week_answered(answer: dict[str, Any], stamp: str, home: Path) -> dict[str, Any] | None:
    """The weekly meter in Codex's answer to `account/rateLimits/read`, as _week keeps one; None when it holds none."""
    block = (answer.get("result") or {}).get("rateLimits")
    if not block:
        return None
    try:
        limits = {"limit_id": block.get("limitId"), "plan_type": block.get("planType")} | {
            name: block[name] and {
                "used_percent": block[name]["usedPercent"], "window_minutes": block[name]["windowDurationMins"],
                "resets_at": block[name]["resetsAt"],
            } for name in ("primary", "secondary")
        }
        weeks = [_week(window, limits, stamp) for window in _weekly(limits)]
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        log.error("Codex's answer on %s's weekly meter is not in the shape Cockpit reads: %r", home.name, exc)
        return None
    return weeks[-1] if weeks else None


def _heaviest(row: dict[str, Any]) -> tuple[float, int]:
    """Sessions sort by their share of the week, then by their tokens, most first."""
    return -row["week_percent"], -(row["tokens"]["fresh"] + row["tokens"]["cached"] + row["tokens"]["output"])


def ask_limits(home: Path, command: str) -> dict[str, Any] | None:
    """Codex's own answer to `account/rateLimits/read` for the account signed in at `home`, from an app-server started
    for this question: a read of the usage endpoint, which spends none of the account's usage, whose `error` says
    when the sign-in no longer works. None, logged, when it does not answer."""
    questions = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "cockpit", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}},
    ]
    try:
        server = subprocess.Popen(
            [command, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            env=os.environ | {"CODEX_HOME": str(home)}, start_new_session=True,
        )
    except OSError as exc:
        log.error("starting %s app-server to read %s's banked resets failed: %s", command, home.name, exc)
        return None
    # `codex` starts the real server as its child, so stopping it takes the whole process group. That also ends the
    # reading below, which is how the time limit cuts a server that stays silent.
    stop = lambda: os.killpg(server.pid, signal.SIGKILL)
    limit = threading.Timer(RESETS_TIMEOUT_SECONDS, stop)
    limit.start()
    try:
        server.stdin.write("".join(json.dumps(question) + "\n" for question in questions))  # ty: ignore[unresolved-attribute]  # Popen uses stdin=PIPE.
        server.stdin.flush()  # ty: ignore[unresolved-attribute]
        for line in server.stdout:  # ty: ignore[not-iterable]  # Popen uses stdout=PIPE.
            answer = json.loads(line)
            if answer.get("id") == 2:
                return answer
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.error("reading %s's banked resets from Codex failed: %r", home.name, exc)
        return None
    finally:
        limit.cancel()
        limit.join()
        stop()
        server.communicate()
    log.error("Codex's app-server for %s ended or timed out before it gave its banked resets", home.name)
    return None


def read_resets(home: Path, command: str, now: datetime) -> dict[str, Any] | None:
    """The banked resets of the account signed in at `home`, in the shape resets.known gives; None when Codex gives
    none."""
    answer = ask_limits(home, command)
    return answer and _resets_of(answer, home, now)


def _resets_of(answer: dict[str, Any], home: Path, now: datetime) -> dict[str, Any] | None:
    try:
        return parse_resets(answer["result"]["rateLimitResetCredits"], now)
    except (KeyError, TypeError):
        # The error's code only: its message quotes the server's reply, which may echo a credential.
        log.error("Codex gave %s no banked resets: error %s", home.name, (answer.get("error") or {}).get("code"))
        return None


def refused_sign_in(answer: dict[str, Any] | None) -> bool:
    """Whether Codex's answer says the account's sign-in no longer works, as against a network or server failure."""
    error = (answer or {}).get("error")
    return bool(error) and REFUSED.search(str(error.get("message"))) is not None


def parse_resets(block: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """Codex's `rateLimitResetCredits` block in the shape resets.known gives, each credit one full reset of the week
    that can be spent at any time; None, logged, for a shape Cockpit cannot read."""
    try:
        return {
            "read_at": now.isoformat(),
            "grants": [
                {
                    "label": credit["title"], "left": 1, "total": 1,
                    "ends_at": datetime.fromtimestamp(credit["expiresAt"], UTC).isoformat(),
                    "usable_now": True, "needs_limit": False, "paused": False, "clears": ["seven_day"],
                }
                for credit in block["credits"]
                if credit["status"] == "available" and credit["resetType"] == "codexRateLimits"
            ],
            "cooldown_until": None,
        }
    except (KeyError, TypeError, ValueError, OSError) as exc:
        log.error("Codex's banked resets have an unexpected shape: %r", exc)
        return None


def _changed(home: Path) -> float | None:
    """When the folder's sign-in file last changed; None without one."""
    try:
        return (home / "auth.json").stat().st_mtime
    except OSError:
        return None


class AccountIndex:
    """One Codex account's use, from the session files in its folder."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.sessions_dir = home / "sessions"
        # Read again on each refresh; read here too, so the page names every account while the first count runs.
        self.email = email(home)
        # The sign-in file, as _changed gives it, that Codex last refused.
        self._refused_file: float | None = None
        self._lock = threading.Lock()
        self._files: dict[Path, Session] = {}
        # The latest weekly meter: {used, resets_at, plan, captured_at}, and the account it belongs to, which outlives
        # a sign-in that has not finished yet.
        self._week: dict[str, Any] | None = None
        self._week_email = self.email
        self._weeks_path = home / "cockpit-usage.json"
        self._weeks = self._saved_weeks()
        self._week = self._weeks.get(self.email)
        # The last banked resets read for each address (read_resets), so a folder signed in to another account starts
        # without the old account's.
        self._resets: dict[str, dict[str, Any]] = {}
        # Every reading of the weekly meter: (time, share used, resets_at in epoch seconds).
        self._readings: list[tuple[str, float, int]] = []
        # Each call's share of the weekly limit, per session file in the order of its `added`, worked out again
        # whenever a line adds a call or a reading.
        self._shares: dict[Path, list[float]] | None = None
        self._latest_week()

    def _saved_weeks(self) -> dict[str, dict[str, Any]]:
        """Read weekly meters whose account was checked when the endpoint answered."""
        try:
            saved = json.loads(self._weeks_path.read_text())
            if not isinstance(saved, dict) or any(
                not isinstance(week, dict) or not {"used", "resets_at", "plan", "captured_at"} <= week.keys()
                for week in saved.values()
            ):
                raise ValueError("unexpected weekly meter shape")
            return saved
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError) as exc:
            log.error("reading saved Codex meters in %s failed: %s", self.home.name, type(exc).__name__)
            return {}

    def _save_weeks(self) -> None:
        """Replace the private meter cache atomically, preserving each account's reading."""
        with tempfile.NamedTemporaryFile(mode="w", dir=self.home, prefix=".cockpit-usage-", delete=False) as handle:
            temporary = Path(handle.name)
            try:
                json.dump(self._weeks, handle)
                handle.close()
                temporary.replace(self._weeks_path)
            finally:
                temporary.unlink(missing_ok=True)

    def _latest_week(self) -> None:
        """The latest weekly meter, from the end of the newest session files. The first full count reads every
        file and runs after Claude's (spend.refresh_forever):"""
        # Session files carry no account identity; signed-in accounts use the endpoint's saved reading.
        if self.email:
            return
        found = []
        for path in self.sessions_dir.glob("**/rollout-*.jsonl"):
            try:
                found.append((path.stat().st_mtime, path))
            except OSError:
                continue
        for _, path in sorted(found, reverse=True)[:LATEST_FILES]:
            try:
                with path.open("rb") as handle:
                    size = handle.seek(0, 2)
                    handle.seek(max(0, size - LATEST_TAIL_BYTES))
                    lines = handle.read().split(b"\n")
            except OSError as exc:
                log.error("reading the end of %s for Codex's weekly meter failed: %s", path, exc)
                continue
            # The last piece is a line still being written, or empty; the first may start mid-line.
            for raw in reversed(lines[1 if size > LATEST_TAIL_BYTES else 0:-1]):
                if TOKEN_COUNT not in raw[:200]:
                    continue
                try:
                    entry = json.loads(raw)
                    limits = entry["payload"].get("rate_limits") or {}
                    weeks = [_week(window, limits, str(entry["timestamp"])) for window in _weekly(limits)]
                except (ValueError, KeyError, TypeError, AttributeError) as exc:
                    log.warning("%s near its end is not one Codex entry, skipped: %r", path, exc)
                    continue
                if weeks:
                    if self._week is None or weeks[-1]["captured_at"] > self._week["captured_at"]:
                        self._week = weeks[-1]
                    break

    def refresh(self, now: datetime) -> None:
        """Read what was appended to every session file written within HORIZON_DAYS."""
        self._note_address(email(self.home))
        oldest = (now - timedelta(days=HORIZON_DAYS)).timestamp()
        for path in self.sessions_dir.glob("**/rollout-*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            known = self._files.get(path)
            if stat.st_mtime < oldest or (known is not None and stat.st_size == known.offset):
                continue
            try:
                self._read(path, known or Session(session_id=path.stem[-36:]), stat.st_size)
            except OSError as exc:
                log.error("reading %s for Codex use failed: %s", path, exc)

    def _note_address(self, address: str) -> None:
        """The account the folder holds now. A folder signed in to another account starts without the old account's
        meter: a session file names no account, so its readings cannot say whose they are."""
        with self._lock:
            self.email = address
            if address and address != self._week_email:
                self._week, self._week_email = self._weeks.get(address), address

    def _read(self, path: Path, known: Session, size: int) -> None:
        if size < known.offset:
            log.error("%s shrank from %d to %d bytes; Codex use keeps what it read", path, known.offset, size)
            known.offset = size
        with path.open("rb") as handle:
            handle.seek(known.offset)
            for raw in handle:
                # A line still being written is read on the next refresh.
                if not raw.endswith(b"\n"):
                    break
                if known.offset == 0 or TOKEN_COUNT in raw[:200]:
                    try:
                        with self._lock:
                            self._line(known, json.loads(raw))
                    except (ValueError, KeyError, TypeError, AttributeError) as exc:
                        log.warning("%s at byte %d is not one Codex entry, skipped: %r", path, known.offset, exc)
                known.offset += len(raw)
        with self._lock:
            self._files[path] = known
            self._shares = None

    def _line(self, known: Session, entry: dict[str, Any]) -> None:
        payload, stamp = entry["payload"], str(entry["timestamp"])
        if entry.get("type") == "session_meta":
            source = payload.get("source") or ""
            # A subagent's session names its parent: {"subagent": {"thread_spawn": {"parent_thread_id": ...}}}.
            known.cwd, known.source = str(payload.get("cwd") or ""), source if isinstance(source, str) else "subagent"
            return
        if payload.get("type") != "token_count":
            return
        limits = payload.get("rate_limits") or {}
        weekly = _weekly(limits)
        # Parsed before anything changes, so a line that does not parse leaves `added` and `weeks` in step.
        readings = [(stamp, float(window["used_percent"]), int(window["resets_at"])) for window in weekly]
        info = payload.get("info")
        if info:
            total = _tokens(info["total_token_usage"])
            # A count below the last one starts again from zero.
            grew = {kind: total[kind] - known.total[kind] for kind in KINDS}
            if any(value < 0 for value in grew.values()):
                grew = total
            if any(grew.values()):
                known.added.append((stamp, grew))
                known.weeks.append(readings[0][2] if readings else None)
                self._shares = None
            known.total = total
        for window, reading in zip(weekly, readings, strict=True):
            self._readings.append(reading)
            self._shares = None
            if not self.email and (self._week is None or stamp >= self._week["captured_at"]):
                self._week = _week(window, limits, stamp)

    def _week_shares(self) -> dict[Path, list[float]]:
        """Each call's share of the weekly limit, in percent: every rise of a week's meter shared out over that
        week's calls since its rise before, by their tokens. A call belongs to the week its own line reported, or,
        without a reading, to the week read last. Called under the lock."""
        if self._shares is not None:
            return self._shares
        shares = {path: [0.0] * len(session.added) for path, session in self._files.items()}
        # A call sorts before a reading of the same moment, since Codex reads the meter after the call, and of two
        # readings of the same moment the higher comes first, so the lower one adds nothing.
        events = sorted(
            [(stamp, 0, 0.0, path, at) for path, session in self._files.items() for at, (stamp, _) in enumerate(session.added)]
            + [(stamp, 1, -used, None, resets) for stamp, used, resets in self._readings],
            key=lambda event: event[:3])
        # Each week by when it first said it resets, since that jitters by seconds between readings.
        weeks: list[int] = []

        def week_of(resets: int) -> int:
            week = next((week for week in weeks if abs(week - resets) <= SAME_WEEK_SECONDS), None)
            if week is None:
                weeks.append(resets)
                return resets
            return week

        highest: dict[int, float] = {}
        pending: dict[int | None, list[tuple[Path, int]]] = {}
        latest: int | None = None
        for _, kind, minus, path, at in events:
            if path is not None:
                resets = self._files[path].weeks[at]
                pending.setdefault(week_of(resets) if resets is not None else latest, []).append((path, at))
                continue
            week, used = week_of(at), -minus
            latest = week
            if not highest:
                # The first reading on record: the calls before it count toward use from before the record.
                highest[week] = used
                pending = {}
                continue
            # A week first read here started from 0 %.
            rise = used - highest.get(week, 0.0)
            if rise <= 0:
                continue
            highest[week] = used
            calls = pending.pop(week, [])
            tokens = [self._files[path].added[at][1] for path, at in calls]
            weights = [grew["fresh"] + grew["cached"] + grew["output"] for grew in tokens]
            total_weight = sum(weights)
            if total_weight:
                for (path, at), weight in zip(calls, weights, strict=True):
                    shares[path][at] += rise * weight / total_weight
        self._shares = shares
        return shares

    def summary(self, since: datetime, now: datetime) -> dict[str, Any]:
        """The tokens counted since `since`, in total and per session (most first), and the latest weekly meter
        with its pace at `now`, as Claude's (usage.pace)."""
        start = since.strftime("%Y-%m-%dT%H:%M:%S")
        total, rows = dict.fromkeys(KINDS, 0), []
        with self._lock:
            shares = self._week_shares()
            for path, session in self._files.items():
                calls = [at for at, (stamp, _) in enumerate(session.added) if stamp >= start]
                if not calls:
                    continue
                tokens = {kind: sum(session.added[at][1][kind] for at in calls) for kind in KINDS}
                for kind in KINDS:
                    total[kind] += tokens[kind]
                rows.append({
                    "session_id": session.session_id, "account": self.email or self.home.name, "cwd": session.cwd, "source": session.source,
                    "first": session.added[calls[0]][0], "last": session.added[calls[-1]][0], "calls": len(calls),
                    "tokens": tokens, "week_percent": round(sum(shares[path][at] for at in calls), 3),
                })
            # Codex often resets its week early, so a period can span more than one of its weeks.
            weeks: list[int] = []
            for stamp, _, resets in self._readings:
                if stamp >= start and all(abs(each - resets) > SAME_WEEK_SECONDS for each in weeks):
                    weeks.append(resets)
        rows.sort(key=_heaviest)
        return {"total": total, "calls": sum(row["calls"] for row in rows), "sessions": rows, "week": self.week(now),
                "week_percent": round(sum(row["week_percent"] for row in rows), 3), "weeks": len(weeks)}

    def ask(self, command: str, now: datetime) -> None:
        """Note whether Codex's answer refuses this account's sign-in, its banked resets and its weekly meter; a failed
        read keeps what was known. The refusal holds for the sign-in file as it was before the question, so a sign-in made meanwhile,
        or Codex renewing its access, ends it."""
        file = _changed(self.home)
        # Read before the question: a sign-in made meanwhile must not get this account's banked resets.
        address = email(self.home)
        answer = ask_limits(self.home, command)
        if refused_sign_in(answer):
            time.sleep(REFUSED_ASK_AGAIN_SECONDS)
            answer = ask_limits(self.home, command)
        if refused_sign_in(answer):
            if self._refused_file != file:
                log.error("Codex refuses the sign-in in %s: every job on that account fails until it signs in again", self.home.name)
            self._refused_file = file
            return
        if answer is None:
            return
        if "error" not in answer:
            self._refused_file = None
        resets = _resets_of(answer, self.home, now)
        if not address or address != email(self.home):
            return
        self._note_address(address)
        if resets is not None:
            self.set_resets(address, resets)
        stamp = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        week = _week_answered(answer, stamp, self.home)
        with self._lock:
            if address != self.email:
                return
            if week and (self._week is None or stamp >= self._week["captured_at"]):
                self._week = week
                self._weeks[address] = week
                self._save_weeks()

    def refused(self) -> bool:
        """Whether Codex last refused the sign-in the folder holds now."""
        return self._refused_file is not None and self._refused_file == _changed(self.home)

    def set_resets(self, address: str, answer: dict[str, Any]) -> None:
        with self._lock:
            self._resets[address] = answer

    def week(self, now: datetime) -> dict[str, Any] | None:
        """The latest weekly meter {used, resets_at, plan, captured_at} with its share of the week gone (`elapsed`),
        its pace at `now`, the end it counts to and its banked resets (`resets`), as Claude's (usage.week_counted);
        None before any reading."""
        with self._lock:
            week = self._week and {**self._week, "resets": self._resets.get(self.email)}
        if not week:
            return None
        resets_at = datetime.fromisoformat(week["resets_at"]).timestamp()
        # A week that reset since its last count has unknown use.
        if resets_at <= now.timestamp():
            return week | {"used": None, "elapsed": None, "pace": None, "counted_until": None, "span": 100}
        return week | usage.week_counted(self.email, week["used"], resets_at, now.timestamp())

    def hours(self, since: datetime) -> list[dict[str, Any]]:
        """Codex use per hour since `since`: its calls, tokens and share of the weekly limit, for the graph per day."""
        start = since.strftime("%Y-%m-%dT%H:%M:%S")
        per_hour: dict[str, dict[str, Any]] = {}
        with self._lock:
            shares = self._week_shares()
            for path, session in self._files.items():
                for at, (stamp, grew) in enumerate(session.added):
                    if stamp < start:
                        continue
                    hour = per_hour.setdefault(f"{stamp[:13]}:00:00Z", {"calls": 0, "tokens": dict.fromkeys(KINDS, 0), "week_percent": 0.0})
                    hour["calls"] += 1
                    hour["week_percent"] += shares[path][at]
                    for kind in KINDS:
                        hour["tokens"][kind] += grew[kind]
        return [{"hour": hour, **per_hour[hour], "week_percent": round(per_hour[hour]["week_percent"], 3)} for hour in sorted(per_hour)]


class CodexIndex:
    """Every Codex account's use, each counted by its own AccountIndex and put together here."""

    def __init__(self, homes: tuple[Path, ...]) -> None:
        self.accounts = [AccountIndex(home) for home in homes]
        self.ready_at: str | None = None

    def refresh(self, now: datetime) -> None:
        for account in self.accounts:
            account.refresh(now)
        self.ready_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    def ask_accounts(self, command: str) -> None:
        """Ask Codex about every signed-in account: its banked resets, and whether its sign-in still works."""
        for account in self.accounts:
            if email(account.home):
                account.ask(command, datetime.now(UTC))

    def summary(self, since: datetime, now: datetime) -> dict[str, Any]:
        """Every account's tokens and sessions since `since`, and in `accounts` each signed-in account's latest
        weekly meter, in the order of config.CODEX_HOMES. The shares add up in shares of one account's week, as the
        Claude accounts' do, and `weeks` counts each account's weeks in the period, each allowing 100 %."""
        parts = [(account, account.summary(since, now)) for account in self.accounts]
        return {
            "total": {kind: sum(part["total"][kind] for _, part in parts) for kind in KINDS},
            "calls": sum(part["calls"] for _, part in parts),
            "sessions": sorted((row for _, part in parts for row in part["sessions"]), key=_heaviest),
            "accounts": [
                {"email": account.email, "folder": account.home.name, "week": part["week"]}
                for account, part in parts if account.email or part["week"]
            ],
            "week_percent": round(sum(part["week_percent"] for _, part in parts), 3),
            "weeks": sum(part["weeks"] for _, part in parts),
        }

    def weeks(self, now: datetime) -> list[dict[str, Any]]:
        """Each signed-in account's latest weekly meter, in the order of config.CODEX_HOMES, for the usage display
        at the foot of the page (SPEC UI-57): {email, folder, week}, `week` as AccountIndex.week."""
        listed = [(account, account.week(now)) for account in self.accounts]
        return [{"email": account.email, "folder": account.home.name, "week": week} for account, week in listed if account.email or week]

    def hours(self, since: datetime) -> list[dict[str, Any]]:
        """Every account's Codex use per hour since `since`, added up, for the graph per day."""
        per_hour: dict[str, dict[str, Any]] = {}
        for account in self.accounts:
            for hour in account.hours(since):
                merged = per_hour.setdefault(hour["hour"], {"calls": 0, "tokens": dict.fromkeys(KINDS, 0), "week_percent": 0.0})
                merged["calls"] += hour["calls"]
                merged["week_percent"] += hour["week_percent"]
                for kind in KINDS:
                    merged["tokens"][kind] += hour["tokens"][kind]
        return [{"hour": hour, **per_hour[hour], "week_percent": round(per_hour[hour]["week_percent"], 3)} for hour in sorted(per_hour)]
