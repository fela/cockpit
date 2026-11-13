"""Entry point for `cockpit-server`: wires config to the app, runs backups and sweeper wake-ups, serves."""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Any

import uvicorn
from fastapi import FastAPI

from cockpit import (
    accounts,
    activity,
    auth,
    briefing,
    codex,
    codex_agent,
    codex_login,
    codex_worker,
    commands,
    config,
    db,
    delivery,
    error_stops,
    explain,
    github,
    history,
    limits,
    logins,
    moves,
    parts,
    patterns,
    permissions,
    projects,
    questions,
    removals,
    request_stats,
    resets,
    resources,
    schedules,
    server_logs,
    sessions,
    sidechat,
    spend,
    stages,
    store,
    sweeper,
    updates,
    wakes,
)
from cockpit.app import HidePublicLinks, Settings, create_app
from cockpit.config import user_name

log = logging.getLogger(__name__)

BACKUP_INTERVAL_SECONDS = 24 * 60 * 60
ACTIVITY_FOLD_SECONDS = 60 * 60
REQUEST_LIMIT = 128


def backup_once() -> None:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    conn = db.connect(config.DB_PATH)
    try:
        db.backup(conn, config.BACKUP_DIR / f"cockpit-{today}.db")
    finally:
        conn.close()
    cutoff = (datetime.now(UTC) - timedelta(days=config.BACKUP_KEEP_DAYS)).strftime("%Y-%m-%d")
    for path in config.BACKUP_DIR.glob("cockpit-*.db"):
        if path.stem.removeprefix("cockpit-") < cutoff:
            path.unlink()
    log.info("backed up the database for %s", today)


def backup_forever() -> None:
    while True:
        try:
            backup_once()
        except Exception:
            log.exception("database backup failed")
        time.sleep(BACKUP_INTERVAL_SECONDS)


def fold_activity_forever() -> None:
    """The Mac's minutes past their 30 days become day totals even when no Mac sends (SPEC TRACK-9)."""
    while True:
        try:
            conn = db.connect(config.DB_PATH)
            try:
                with db.transaction(conn):
                    activity.fold(conn, datetime.now(UTC), config.setup().timezone)
            finally:
                conn.close()
        except Exception:
            log.exception("folding the Mac's old minutes into day totals failed")
        time.sleep(ACTIVITY_FOLD_SECONDS)


def flush_requests(recorder: request_stats.Recorder) -> None:
    conn = db.connect(config.DB_PATH)
    try:
        recorder.flush(conn, datetime.now(UTC))
    finally:
        conn.close()


def flush_requests_forever(recorder: request_stats.Recorder) -> None:
    while True:
        time.sleep(request_stats.FLUSH_SECONDS)
        try:
            flush_requests(recorder)
        except Exception:
            log.exception("writing the request counts failed")


class StopNoting(uvicorn.Server):
    """Uvicorn's server, noting on the app and the log capture when a stop began: a page error from the time it stops
    is the restart's."""

    def __init__(self, config: uvicorn.Config, app: FastAPI, capture: server_logs.Capture) -> None:
        super().__init__(config)
        self.app = app
        self.capture = capture

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        if self.app.state.stopping_at is None:
            self.app.state.stopping_at = datetime.now(UTC)
            self.capture.stopping_at = self.app.state.stopping_at
        super().handle_exit(sig, frame)


def record_start(version: str, started_at: str) -> None:
    conn = db.connect(config.DB_PATH)
    try:
        with db.transaction(conn):
            patterns.record(conn, "server.started", "cockpit", None, None, None,
                            {"version": version, "started_at": started_at})
    finally:
        conn.close()


def finish_withdrawals() -> None:
    """Close the withdrawals that waited while the user worked on a call's card."""
    conn = db.connect(config.DB_PATH)
    try:
        now = datetime.now(UTC)
        finished = permissions.finish_withdrawals(conn, now)
    finally:
        conn.close()
    if finished:
        log.info("closed the withdrawn permission requests %s", ", ".join(map(str, finished)))


def launch_briefing_once(deliverer: delivery.Deliverer) -> None:
    """Start a session on the day's briefing task when the briefing is due (briefing.py)."""
    conn = db.connect(config.DB_PATH)
    try:
        message_id = briefing.launch_once(conn, config.setup(), datetime.now(UTC))
    finally:
        conn.close()
    if message_id is not None:
        log.info("asking for the morning briefing with message %s", message_id)
        deliverer.start(f"send-{message_id}", deliverer.send, message_id)


def launch_projects_tidy_once(deliverer: delivery.Deliverer) -> None:
    """Ask the projects session to tidy the projects when it is due (projects.py, SPEC SWEEP-23)."""
    conn = db.connect(config.DB_PATH)
    try:
        message_id = projects.launch_once(
            conn, config.setup(), lambda session_id: deliverer.lookup(session_id) is not None, datetime.now(UTC)
        )
    finally:
        conn.close()
    if message_id is not None:
        log.info("asking for a tidy of the projects with message %s", message_id)
        deliverer.start(f"send-{message_id}", deliverer.send, message_id)


def cut_short(what: str, exc: Exception) -> bool:
    """Whether a loaded host cut a part of a background poll short, logged as a warning; the next poll tries again.
    It does so in two ways, each told elsewhere: a session lookup whose `claude agents` listing timed out
    (list_sessions logged that), and a write that waited out the busy timeout (requests then fail with a 503 that
    is logged). The caller logs anything else as an error."""
    if isinstance(exc, sessions.LookupFailed) or db.locked(exc):
        log.warning("%s stopped short: %s; the next poll tries again", what, exc)
        return True
    return False


def wake_sweeper_forever(
    deliverer: delivery.Deliverer, session_cache: sessions.SessionCache, spend_index: spend.SpendIndex
) -> None:
    """Every poll: close held permission withdrawals; ask for the morning briefing when it
    is due (briefing.py); put each session's question to
    the user as a step and close the ones answered (questions.py); tell the sessions Claude's usage limit
    stopped to continue once usage is back (limits.py), and the ones a sign-out stopped once their account is
    signed in again, the sweeper with a full sweep (logins.py); send the
    messages of the user's that waited for a session's turn to end (delivery.waiting_due) and the hand-offs
    whose wait for Take back reached its limit (delivery.take_back_due); stop the sessions
    the user archived once their turn on Cockpit's message is over (delivery.archives_due); take the finished
    sessions whose items are all closed off `claude agents` (removals.due); fill free places in
    the user's queue; then stop sweeps when the user has been away long enough (with a last check),
    else wake the sweeper if it is due, else have it check the top of the queue if that is due.

    Writes fill the queue themselves; the poll covers startup and a ready-next
    step whose snooze ended while a place was free. The first poll runs at startup.
    """
    while True:
        try:
            finish_withdrawals()
        except Exception:
            log.exception("closing held permission withdrawals failed")
        try:
            launch_briefing_once(deliverer)
        except Exception:
            log.exception("asking for the morning briefing failed")
        try:
            launch_projects_tidy_once(deliverer)
        except Exception:
            log.exception("asking for a tidy of the projects failed")
        try:
            conn = db.connect(config.DB_PATH)
            to_send: list[int] = []
            to_archive: list[int] = []
            to_stop: list[str] = []
            to_remove: list[str] = []
            busy: set[Path] = set()
            idle_after = delivery.IDLE_STOP_AFTER
            to_move: list[dict[str, Any]] = []
            try:
                # The sweeper decides from a listing at most CACHE_SECONDS old, so it waits for one when needed.
                listing = session_cache.snapshot(wait=True)
                listed, failed = listing.sessions, listing.failed
                # A failed account's sessions are only its last good list, so nothing is done for them. Nor for
                # a moved session then: its copy may be the failed account's, and its lookup refuses while one fails.
                moved = moves.moved(config.DB_PATH) if failed else {}
                sure = [s for s in listed if s.account not in failed and s.session_id not in moved]
                claude_sure = [s for s in sure if s.provider == "claude"]
                readings = accounts.readings(accounts.in_use(config.ACCOUNTS, []), config.CLAUDE_RATE_LIMITS)
                # questions, moves, waiting_due and archives_due act on a session missing from the list, which
                # may run on the failed account, so they wait until every listing works.
                if failed:
                    log.warning(
                        "session questions, move recommendations, waiting messages and archives not handled:"
                        " listing the %s account's Claude sessions failed", ", ".join(sorted(failed)),
                    )
                else:
                    questions.sync(conn, listed, datetime.now(UTC))
                    # A move is recommended only to an account that is not signed out (accounts.usable).
                    destinations = accounts.usable(accounts.in_use(config.ACCOUNTS, []), datetime.now(UTC))
                    moves.sync(conn, claude_sure, destinations, readings, datetime.now(UTC))
                to_send += limits.sync(conn, sure, readings, datetime.now(UTC), config.setup().checkouts)
                lost_ids, swept = logins.sync(
                    conn, claude_sure, store.sweeper_session(conn), datetime.now(UTC), config.setup().checkouts
                )
                to_send += lost_ids
                try:
                    # Before the resends' outcomes are judged, so a resend that got through counts as sent.
                    store.confirm_uncertain(conn, datetime.now(UTC))
                except Exception as exc:
                    if not cut_short("checking uncertain deliveries in their sessions' transcripts", exc):
                        log.exception("checking uncertain deliveries in their sessions' transcripts failed")
                try:
                    to_send += error_stops.sync(conn, claude_sure, datetime.now(UTC), config.setup().checkouts)
                except Exception as exc:
                    if not cut_short("resending to sessions stopped on an API error", exc):
                        log.exception("resending to sessions stopped on an API error failed")
                sidechat.expire(conn, datetime.now(UTC))
                if not failed:
                    to_send += delivery.waiting_due(conn, listed, datetime.now(UTC))
                    to_send += deliverer.places_due(conn)
                    to_archive = delivery.archives_due(conn, listed, datetime.now(UTC))
                    to_move = moves.waits_due(listed, config.DB_PATH)
                    busy = removals.busy(config.ACCOUNTS, listed)
                    to_remove = removals.due(conn, listed, removals.pinned(config.ACCOUNTS), busy)
                idle_after = idle_stop_after(Path("/proc/meminfo"))
                to_stop = delivery.stops_due(conn, sure, datetime.now(UTC), idle_after)
                if to_stop and idle_after < delivery.IDLE_STOP_AFTER:
                    log.warning("the host is low on memory: stopping %d idle sessions after %d minutes",
                                len(to_stop), idle_after.total_seconds() // 60)
                to_send += delivery.take_back_due(conn, datetime.now(UTC))
                # The user's pause applies agents' held changes itself; this covers a hand-off by Run or Allow.
                store.release_held(conn)
                with db.transaction(conn):
                    store.fill_queue(conn, datetime.now(UTC))
                to_send += store.tell_expired(conn, config.setup().checkouts, datetime.now(UTC))
                # Only on full listings: a failed account's sessions are its last good list, which may show work stopped.
                if not failed:
                    try:
                        board = store.page_state(conn, "cockpit", listed, [], config.setup().checkouts, set())["items"]
                        sweeper.note_stalls(conn, board)
                        to_send += sweeper.stall_wakes(conn, board, config.setup().checkouts, datetime.now(UTC))
                    except Exception as exc:
                        if not cut_short("noting stalled work", exc):
                            log.exception("noting stalled work failed")

                def found(session_id: str) -> bool:
                    return deliverer.lookup(session_id) is not None

                exists = sweeper.resumable(conn, found, spend_index.latest_model)

                # On its own: a failed lookup here must not keep the messages queued above from going out.
                try:
                    message_id = sweeper.pause_once(conn, config.setup().checkouts, exists)
                    purpose = f"a last check before sweeps stop while {user_name()} is away"
                    if message_id is None and swept is not None and sweeper.paused(conn) is None:
                        try:
                            message_id = sweeper.launch(
                                conn, "cockpit", logins.sweep_text(swept), config.setup().checkouts, exists, "sweep", []
                            )["id"]
                            purpose = f"a full sweep once the {swept.session.account} account was signed in again"
                        except store.Conflict:
                            message_id = None  # a sweep is under way; the next poll tries again
                    if message_id is None:
                        message_id = sweeper.reorder_once(conn, config.setup().checkouts, exists)
                        purpose = f"rescoring {user_name()}'s queue"
                    if message_id is None:
                        message_id = sweeper.wake_once(conn, config.setup().checkouts, exists)
                        purpose = "waking the sweeper"
                    if message_id is None:
                        message_id = sweeper.check_once(conn, config.setup().checkouts, exists)
                        purpose = "checking the top of the queue"
                    if message_id is None:
                        message_id = sweeper.fill_once(conn, config.setup().checkouts, exists)
                        purpose = f"a quick sweep for work while {user_name()}'s queue is about to run dry"
                except Exception as exc:
                    if not cut_short("starting a sweep", exc):
                        log.exception("starting a sweep failed")
                    message_id = None
            finally:
                conn.close()
                # Also when a later part of the poll failed: each list's messages are committed as due, and
                # nothing else sends them (a queued message that waits for nothing is failed only at restart).
                for send_id in to_send:
                    deliverer.start(f"send-{send_id}", deliverer.send, send_id)
                for archive_id in to_archive:
                    threading.Thread(target=deliverer.archive, args=(archive_id,), daemon=True).start()
                for session_id in to_stop:
                    threading.Thread(target=stop_idle, args=(deliverer, session_id, idle_after), daemon=True).start()
                for waiting in to_move:
                    # Counted from the start: the move queues its message to the session before it sends it.
                    deliverer.start(f"move-{waiting['session_id']}", move_waiting, deliverer, session_cache, waiting)
                if to_remove and REMOVING.acquire(blocking=False):
                    threading.Thread(
                        target=remove_finished, args=(deliverer, session_cache, to_remove), daemon=True
                    ).start()
            if message_id is not None:
                log.info("%s with message %s", purpose, message_id)
                deliverer.send(message_id)
        except Exception as exc:
            if not cut_short("waking the sweeper", exc):
                log.exception("waking the sweeper failed")
        time.sleep(sweeper.WAKE_POLL_SECONDS)


def signed_in() -> list[tuple[config.Account, str]]:
    """Each Claude account in use with the address it is signed in with, for the check of their usage and banked
    resets (resets.refresh_forever). None while the sign-ins go unused (accounts.sign_ins_unused), so that check asks
    Anthropic nothing."""
    if accounts.sign_ins_unused():
        return []
    return [(account, address) for account in accounts.in_use(config.ACCOUNTS, []) if (address := accounts.email(account))]


def idle_stop_after(meminfo: Path) -> timedelta:
    """delivery.idle_stop_after from `meminfo`. Without one (macOS has no /proc) or when it does not parse, the
    one-hour limit holds, so the rest of the wake poll goes on."""
    try:
        return delivery.idle_stop_after(meminfo.read_text())
    except FileNotFoundError:
        return delivery.IDLE_STOP_AFTER
    except (OSError, KeyError, ValueError, IndexError):
        log.exception("reading the free memory from %s failed; idle sessions stop after an hour", meminfo)
        return delivery.IDLE_STOP_AFTER


def stop_idle(deliverer: delivery.Deliverer, session_id: str, after: timedelta) -> None:
    """Stop an idle session (delivery.stops_due); one that started work since the listing keeps running."""
    try:
        deliverer.close_idle(session_id, datetime.now(UTC), after)
    except (store.Conflict, store.NotFound) as exc:
        log.info("left idle session %s running: %s", session_id, exc)
    except delivery.DeliveryError as exc:
        log.error("stopping idle session %s failed: %s", session_id, exc)


# Held while finished sessions are taken off `claude agents`. They go one at a time: a removal that deletes a worktree
# other sessions share must be recorded before the next one finds the worktree gone (removals.worktree).
REMOVING = threading.Lock()


def remove_finished(
    deliverer: delivery.Deliverer, session_cache: sessions.SessionCache, session_ids: list[str]
) -> None:
    """Take finished sessions off `claude agents` (removals.due), one after another, each judged on a fresh listing
    of the folders running sessions work in; releases REMOVING."""
    def busy() -> set[Path]:
        session_cache.invalidate()
        listing = session_cache.snapshot(wait=True)
        if listing.failed:
            # A session on that account may work in the worktree, unseen.
            raise RuntimeError(f"listing the {', '.join(sorted(listing.failed))} account's sessions failed")
        return removals.busy(config.ACCOUNTS, listing.sessions)

    try:
        for session_id in session_ids:
            try:
                deliverer.remove(session_id, busy)
            except Exception:
                log.exception("taking finished session %s off `claude agents` failed", session_id)
    finally:
        REMOVING.release()


def move_waiting(deliverer: delivery.Deliverer, session_cache: sessions.SessionCache, waiting: dict[str, Any]) -> None:
    """Carry out a move the user asked for while the session was busy (moves.waits_due). A failed listing leaves it
    for the next poll; a move that fails otherwise ends its wait, logged as an error, rather than try every poll."""
    session_id = waiting["session_id"]
    try:
        moved = deliverer.move(
            session_id, waiting["account"], waiting["actor"], config.setup().checkouts, waiting["asked_at"]
        )
    except delivery.ListingFailed as exc:
        log.warning("the waiting move of session %s is tried again: %s", session_id, exc)
        return
    except (delivery.DeliveryError, store.Conflict, store.NotFound, store.Invalid) as exc:
        log.error("the move of session %s to the %s account that %s asked for failed: %s",
                  session_id, waiting["account"], waiting["actor"], exc)
        conn = db.connect(deliverer.db_path)
        try:
            with db.transaction(conn):
                moves.end_wait(conn, session_id, waiting["asked_at"])
        finally:
            conn.close()
        return
    session_cache.invalidate()
    if moved["message_id"] is not None:
        deliverer.send(moved["message_id"])


def check_codex_forever(index: codex.CodexIndex, command: str) -> None:
    """Note the address each Codex folder holds, then ask Codex about each account, every codex.RESETS_EVERY_SECONDS
    (SPEC UI-77)."""
    while True:
        try:
            conn = db.connect(config.DB_PATH)
            try:
                for account in index.accounts:
                    codex_login.remember(conn, account.home.name, codex.email(account.home))
            finally:
                conn.close()
            index.ask_accounts(command)
        except Exception:
            log.exception("checking the Codex accounts failed")
        time.sleep(codex.RESETS_EVERY_SECONDS)




def check_pulls_forever() -> None:
    """Ask GitHub who opened each open item's PR (SPEC VIEW-5) and the state of each PR the open items link to
    (SPEC NEXT-66), at startup and every github.CHECK_SECONDS."""
    failed: dict[str, datetime] = {}
    failed_states: dict[str, datetime] = {}
    while True:
        try:
            conn = db.connect(config.DB_PATH)
            try:
                recorded = github.check_once(conn, github.ask_github, failed, datetime.now(UTC))
            finally:
                conn.close()
            if recorded:
                log.info("recorded who opened %s", ", ".join(recorded))
        except Exception:
            log.exception("asking GitHub who opened the open items' PRs failed")
        try:
            conn = db.connect(config.DB_PATH)
            try:
                changed = github.check_states(
                    conn, github.ask_states, store.linked_pulls(conn), failed_states, datetime.now(UTC)
                )
            finally:
                conn.close()
            if changed:
                log.info("recorded the state of %s", ", ".join(changed))
        except Exception:
            log.exception("asking GitHub the state of the linked PRs failed")
        time.sleep(github.CHECK_SECONDS)


def check_schedules_forever(deliverer: delivery.Deliverer) -> None:
    """Every schedules.CHECK_SECONDS, start the scheduled prompts whose time came (SPEC BG-14)."""
    while True:
        sent: list[int] = []
        try:
            conn = db.connect(config.DB_PATH)
            try:
                setup = config.setup()
                sent = schedules.check_once(conn, setup.checkouts, datetime.now(UTC), setup.timezone)
            finally:
                conn.close()
        except Exception:
            log.exception("checking the scheduled prompts failed")
        for message_id in sent:
            log.info("started a scheduled prompt, with message %s", message_id)
            try:
                deliverer.start(f"send-{message_id}", deliverer.send, message_id)
            except Exception:
                log.exception("starting the delivery of message %s failed", message_id)
        time.sleep(schedules.CHECK_SECONDS)


def check_wakes_forever(deliverer: delivery.Deliverer) -> None:
    """Every wakes.CHECK_SECONDS, wake the items whose waits came: their time, or their check (SPEC STATE-8)."""
    while True:
        sent: list[int] = []
        try:
            conn = db.connect(config.DB_PATH)
            try:
                reached = wakes.reacher(stages.ask_merge, stages.ask_holds)
                sent = wakes.check_once(conn, reached, config.setup().checkouts, datetime.now(UTC))
            finally:
                conn.close()
        except Exception:
            log.exception("checking the open items' waits failed")
        for message_id in sent:
            log.info("woke an item's session for its wait, with message %s", message_id)
            try:
                deliverer.start(f"send-{message_id}", deliverer.send, message_id)
            except Exception:
                log.exception("starting the delivery of message %s failed", message_id)
        time.sleep(wakes.CHECK_SECONDS)


def explain_forever(session_cache: codex_agent.ProviderSessions) -> None:
    """Have Claude explain each open call on the page once, newest first (SPEC PERM-3), pausing them all while
    Claude's limit refuses them (explain.explain_unless_paused)."""
    paused_until: datetime | None = None
    while True:
        explained = None
        try:
            conn = db.connect(config.DB_PATH)
            try:
                explained, paused_until = explain.explain_unless_paused(
                    lambda now, conn=conn: explain.explain_next(
                        conn, config.HOME, config.CLAUDE_SETTINGS,
                        lambda text: explain_ask(session_cache, text), now,
                        store.open_dialogs(session_cache.last()),
                    ),
                    paused_until, lambda: datetime.now(UTC),
                )
            finally:
                conn.close()
        except Exception:
            log.exception("explaining a call on the page failed")
        if explained is None:
            time.sleep(explain.POLL_SECONDS)


def read_turns_forever() -> None:
    """Read the turns items' sessions gained (SPEC VIEW-10), every history.TURNS_SECONDS."""
    while True:
        try:
            conn = db.connect(config.DB_PATH)
            try:
                added = history.read_all_turns(conn)
                if added:
                    log.info("read %s new turns of items' sessions", added)
            finally:
                conn.close()
        except Exception as exc:
            # The next read picks up where this one stopped.
            if not cut_short("reading items' session turns", exc):
                log.exception("reading items' session turns failed")
        time.sleep(history.TURNS_SECONDS)


def history_forever(session_cache: codex_agent.ProviderSessions) -> None:
    """Keep items' histories current (SPEC VIEW-10): assemble every history once after a start so each long text
    waits for its summary, then have Claude write the waiting summaries and stories one at a
    time, newest first, pausing them all while Claude's limit refuses them (history.summarize_waiting)."""
    assembled = False
    pause: history.Pause | None = None
    while True:
        try:
            conn = db.connect(config.DB_PATH)
            try:
                if not assembled:
                    log.info("assembled the history of %s items", history.backfill(conn, datetime.now(UTC)))
                    assembled = True
                pause = history.summarize_waiting(
                    conn, lambda: pick_account(session_cache)[0], history.ask_claude, pause, lambda: datetime.now(UTC)
                )
            finally:
                conn.close()
        except Exception:
            log.exception("keeping items' histories current failed")
        time.sleep(history.PASS_SECONDS)


def check_tools_forever() -> None:
    """Check enabled providers at startup and every updates.CHECK_SECONDS (SPEC SESSION-15)."""
    while True:
        try:
            now = datetime.now(UTC)
            problems = updates.behind(
                updates.run_npm, updates.fetch_text, updates.CLAUDE_BIN, updates.CLAUDE_SETTINGS,
                updates.PACKAGE_JSON, now, config.setup().providers,
            )
            conn = db.connect(config.DB_PATH)
            try:
                updates.record_behind(conn, problems, now)
            finally:
                conn.close()
        except Exception:
            log.exception("recording whether Claude Code and Codex are behind failed")
        time.sleep(updates.CHECK_SECONDS)


def end_interrupted_runs(deliverer: delivery.Deliverer) -> None:
    """Check commands left unfinished across a restart and tell their sessions what is known."""
    conn = db.connect(config.DB_PATH)
    try:
        message_ids = commands.interrupted(conn, config.setup().checkouts)
    finally:
        conn.close()
    for message_id in message_ids:
        log.warning("a command run survived or has an unknown outcome after restart; telling its session with message %s", message_id)
        deliverer.start(f"send-{message_id}", deliverer.send, message_id)


def find_session(session_id: str) -> sessions.Session | None:
    return sessions.find_session(config.ACCOUNTS, moves.moved(config.DB_PATH), removals.listed(config.DB_PATH), session_id)


def short_id_to_uuid(account: config.Account, short_id: str) -> str | None:
    """The UUID of the session `short_id` in `account`'s list: the account a new session was started on."""
    match = next((session for session in sessions.list_account(account, []) if session.id == short_id), None)
    return match.session_id if match else None


def pick_account(session_cache: codex_agent.ProviderSessions) -> tuple[config.Account, str]:
    """The account new work goes to now (accounts.pick), from the accounts in use that are not signed out
    (accounts.usable), their latest usage, the sessions running on each in the cache's last listing and the account
    the user prefers for now (which waits on a listing only while there is none yet, at Cockpit's start). Raises accounts.SignedOut when every one is signed out."""
    now = datetime.now(UTC)
    usable = accounts.usable(accounts.in_use(config.ACCOUNTS, []), now)
    if not usable:
        raise accounts.SignedOut("every Claude account is signed out; sign one in again first")
    conn = db.connect(config.DB_PATH)
    try:
        preference = accounts.preference(conn, now)
    finally:
        conn.close()
    return accounts.pick(
        usable, accounts.readings(usable, config.CLAUDE_RATE_LIMITS), now, preference, sessions.running(session_cache.last()),
    )


def explain_accounts(session_cache: codex_agent.ProviderSessions) -> list[config.Account]:
    """The accounts a call's explanation tries in turn (SPEC PERM-17): the one new work goes to, then the others in
    use; with one account in use, it is tried twice."""
    first = pick_account(session_cache)[0]
    others = [account for account in accounts.in_use(config.ACCOUNTS, []) if account != first]
    return [first, *others] if others else [first, first]


def explain_ask(session_cache: codex_agent.ProviderSessions, text: str) -> dict[str, str]:
    """Claude's explanation of a call from the accounts in turn; with every account signed out, an ExplainError, so
    the card says so and the call waits its retry like any failed explanation."""
    try:
        chosen = explain_accounts(session_cache)
    except accounts.SignedOut as exc:
        raise explain.ExplainError("every Claude account is signed out", str(exc)) from exc
    return explain.ask_accounts(chosen, text)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    started_at = db.now_iso()
    setup = config.setup()
    config.private_data_dirs()
    settings = Settings(
        db_path=config.DB_PATH,
        token=auth.read_or_create_secret(config.TOKEN_PATH),
        cookie_key=auth.read_or_create_secret(config.COOKIE_KEY_PATH).encode(),
        google=auth.read_google_client(config.GOOGLE_CLIENT_PATH) if setup.auth_mode == "google" else None,
        email=setup.email,
        origin=setup.origin,
        public_origin=setup.public_origin,
        checkouts_dir=setup.checkouts,
        sweep_cwd=setup.checkouts,
        sweep_prompt=config.SWEEP_PROMPT,
        spec_repo=setup.spec_repo,
        spec_branch=setup.deploy_branch,
        skills_repo=setup.skills_repo,
        skills_folder=setup.skills_folder,
        skills_branch=setup.skills_branch,
        claude_settings=config.CLAUDE_SETTINGS,
        claude_rate_limits=config.CLAUDE_RATE_LIMITS,
        claude_accounts=config.ACCOUNTS,
        claude=["claude"],
        codex=setup.codex_command,
        attach=setup.attach,
        exchange=auth.exchange_code,
        secrets_dir=config.SECRETS_DIR,
        signins_dir=config.SIGNINS_DIR,
        auth_mode=setup.auth_mode,
        local_login_token=auth.read_or_create_secret(config.LOCAL_LOGIN_PATH) if setup.auth_mode == "local" else "",
        providers=setup.providers,
        default_provider=setup.default_provider,
    )
    agent = codex_worker.WorkerClient(config.DB_PATH, setup.codex_command) if "codex" in setup.providers else None
    claude_cache = sessions.SessionCache(
        config.ACCOUNTS, lambda: moves.moved(config.DB_PATH), lambda: removals.listed(config.DB_PATH)
    ) if "claude" in setup.providers else None
    session_cache = codex_agent.ProviderSessions(claude_cache, agent)

    def lookup(session_id: str) -> sessions.Session | None:
        if agent is not None and agent.owns(session_id):
            return agent.lookup(session_id)
        return find_session(session_id) if claude_cache is not None else None

    deliverer = delivery.Deliverer(
        db_path=config.DB_PATH,
        sockets_dir=Path(setup.claude_sockets_dir).expanduser() if setup.claude_sockets_dir else config.SOCKETS_DIR,
        run=delivery.run_command,
        trust=lambda account, cwd: delivery.trust_checkout(setup.checkouts, account.state, cwd),
        lookup=lookup,
        short_id_to_uuid=short_id_to_uuid,
        pick=lambda: pick_account(session_cache),
        listed=session_cache.current,
        codex=agent,
        default_provider=setup.default_provider,
        enabled_providers=setup.providers,
    )
    spend_index = spend.SpendIndex(sessions.PROJECTS_DIR)
    codex_index = codex.CodexIndex(config.CODEX_HOMES)
    # Before the app, whose start logs the deliveries and turns a restart cut off (SPEC TRACK-12).
    capture, capture_writer, capture_db = server_logs.install(config.DB_PATH, started_at)
    app = create_app(settings, deliverer, session_cache, spend_index, codex_index, parts.installed())
    end_interrupted_runs(deliverer)
    threading.Thread(target=backup_forever, name="backup", daemon=True).start()
    threading.Thread(target=fold_activity_forever, name="activity-fold", daemon=True).start()
    threading.Thread(
        target=wake_sweeper_forever, args=(deliverer, session_cache, spend_index), name="sweeper-wake", daemon=True
    ).start()
    threading.Thread(target=check_pulls_forever, name="pull-authors", daemon=True).start()
    threading.Thread(target=check_wakes_forever, args=(deliverer,), name="wakes", daemon=True).start()
    threading.Thread(target=check_schedules_forever, args=(deliverer,), name="schedules", daemon=True).start()
    if "claude" in setup.providers:
        threading.Thread(target=explain_forever, args=(session_cache,), name="explain-calls", daemon=True).start()
        threading.Thread(target=read_turns_forever, name="item-turns", daemon=True).start()
        threading.Thread(target=history_forever, args=(session_cache,), name="item-history", daemon=True).start()
        threading.Thread(target=resets.refresh_forever, args=(signed_in,), name="banked-resets", daemon=True).start()
    threading.Thread(target=spend.refresh_forever, args=(spend_index, codex_index), name="claude-spend", daemon=True).start()
    threading.Thread(target=check_codex_forever, args=(codex_index, setup.codex_command), name="codex-accounts", daemon=True).start()
    threading.Thread(target=check_tools_forever, name="tool-versions", daemon=True).start()
    threading.Thread(
        target=flush_requests_forever, args=(app.state.requests,), name="request-counts", daemon=True
    ).start()
    threading.Thread(
        target=resources.track_forever, args=(config.DB_PATH, session_cache.last, lambda: config.setup().timezone),
        name="resources", daemon=True,
    ).start()
    record_start(app.state.version, started_at)
    logging.getLogger("uvicorn.access").addFilter(HidePublicLinks())
    server_config = uvicorn.Config(
        app, host=config.LISTEN_HOST, port=config.LISTEN_PORT, proxy_headers=False, log_level="info",
        # Sync dependencies open their connections before handlers get a worker thread. Bound the requests too,
        # so a burst gets retryable 503s while the server stays below its 1024-descriptor service limit.
        limit_concurrency=REQUEST_LIMIT,
    )
    # Building the Config sets up uvicorn's loggers and drops their handlers, so their capture goes on after it.
    # Uvicorn's loggers keep their lines from the root logger, so "uvicorn" needs its own.
    logging.getLogger("uvicorn").addHandler(capture)

    def finish() -> None:
        """Writes the last minute's request counts and the log lines still queued."""
        flush_requests(app.state.requests)
        capture_writer.stop()

    def finish_on_signal(signum: int, _frame: object) -> None:
        # Once stopped, uvicorn puts back the handler it found and raises the stop signal again, so nothing after
        # `run` runs on a stop. The signal then does what it would have done.
        finish()
        signal.signal(signum, signal.SIG_DFL)
        signal.raise_signal(signum)

    signal.signal(signal.SIGTERM, finish_on_signal)
    signal.signal(signal.SIGINT, finish_on_signal)
    server = StopNoting(server_config, app, capture_db)
    server.run()
    finish()
    if not server.started:
        # uvicorn.run's exit code when the server could not start, such as a port in use.
        sys.exit(3)


if __name__ == "__main__":
    main()
