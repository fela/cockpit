# Cockpit — technical spec

Cockpit is a single-user Python web application for directing Claude Code and
Codex sessions. [SPEC.md](SPEC.md) holds requirements;
[REASONING.md](REASONING.md) explains design choices. See [LOCAL.md](LOCAL.md)
for local authentication and provider details.

## 1. Shape

One Python 3.12+ process runs FastAPI and uvicorn on `127.0.0.1:8790`. A SQLite
file stores the board and delivery state. Browser assets ship with the package.
The optional prompt editor is a checked-in bundle, so installation needs no
frontend build.

The `cockpit` CLI calls the HTTP API. Agents use it to maintain items, prepare
steps and send messages. A sweeper session checks and ranks work; owning sessions
do their items' work and write their steps.

### Files and setup

`~/.config/cockpit/setup.toml` describes the installation. Required values identify
the user, origin, checkout paths, skill location, attach command and timezone.
Optional values select authentication, providers, executable paths and a deployment
branch. [setup.example.toml](setup.example.toml) documents them.

Setup validation rejects missing or unknown keys, malformed paths and invalid
provider or origin choices. `cockpit setup --local` generates a local configuration.
The manual configuration default is Google authentication; generated local setup
explicitly selects local authentication.

The configuration directory is private (0700). Authentication files use 0600:
`token` for agents, `cookie-key` for browser session signatures, `local-login-token`
for local browser sign-in, and `google-oauth-client.json` for Google mode.
The database is `~/.local/share/cockpit/cockpit.db`.

### Modules (`src/cockpit/`)

| Area | Modules |
| --- | --- |
| HTTP, authentication and configuration | `app.py`, `auth.py`, `server.py`, `config.py` |
| Database and board rules | `db.py`, `store.py`, `rank.py` |
| Command line and local installation | `cli.py`, `local.py`, `attach.py` |
| Claude sessions and delivery | `sessions.py`, `delivery.py`, `accounts.py`, `limits.py` |
| Codex threads and protocol | `codex.py`, `codex_agent.py` |
| Permissions and explanations | `permissions.py`, `explain.py` |
| Sweeps and briefings | `sweeper.py`, `briefing.py` |
| History and usage | `history.py`, `patterns.py`, `spend.py`, `timesheet.py`, `usage.py` |
| Spec, skills and rendering | `spec.py`, `render.py` |
| Emergency and side conversations | `emergency.py`, `sidechat.py` |
| Browser interface | `static/` |

## 2. Access

### Browser sign-in (`auth.py`)

Local mode accepts only `http://127.0.0.1:8790` or `http://localhost:8790`.
It validates the socket peer and exact Host. Browser writes also require the exact
configured Origin. `cockpit login` exchanges a private local token for a one-use
code, opens a URL fragment, and the page exchanges the code for a signed HttpOnly
session cookie. The fragment is removed from browser history before the exchange.
See [LOCAL.md](LOCAL.md) for cookie and code details.

Google mode uses OpenID Connect's authorization code flow with PKCE and scopes
`openid email`. The OAuth web client redirects to `<origin>/auth/google/callback`.
The callback checks the sign-in state and admits only the configured email and,
for a Workspace account, its domain. A signed cookie identifies the browser.
Sign-out clears that browser's cookie. Replacing the cookie key invalidates all
browser sessions.

### Who may do what

Agents authenticate with a bearer token. Agent writes require `X-Cockpit-As`, a
short lowercase name recorded as `agent:<name>`. Browser actions use the actor
`user`; automatic server actions use `cockpit`.

The browser alone may answer approvals, send messages as the user, edit permission
rules and change user controls. Agents can maintain items and steps, send messages
as themselves and record sweep results. Server checks preserve user-set fields
when an agent updates an item. Session-only actions also check the sending session's
identity to catch accidental cross-session changes; agents share one API token.

Agent messages that imitate Cockpit's sender headers are rejected. Agent-written
text embedded in a forwarded message is marked as quoted before delivery.

### Headers

Browser writes require an exact Origin match; cookies alone do not establish the
source of a write. Responses use a restrictive Content Security Policy, MIME
sniffing protection and a no-referrer policy. API responses are not cached.
Versioned static assets may be cached immutably. Shared HTML pages use a separate
sandbox policy without same-origin access.

## 3. Data model

### Conventions

SQLite connections use write-ahead logging, foreign keys and a busy timeout.
Multi-statement changes run in transactions. Keep slow reads and external work
outside a write transaction so other writers can proceed. Timestamps are ISO 8601
UTC; API timestamps with offsets are normalized before storage.

### Work: items and steps

An **item** is a piece of work. Its unique source key supports repeated upserts:
`gh:example-org/app#10`, `linear:ENG-123`, `session:<uuid>` or `note:<slug>`.
It holds a title, summary, project, stage, priority, goals, sessions and links.
`waiting_on` describes another agent, bot, person or release holding the next move.
A wait can include a wake-up time and a condition Cockpit can check sooner.

A **step** is an action the user can take on an item. `(item_id, need)` identifies
it across updates. It holds the ask, reason, body, collapsed details, draft reply,
answer buttons and optional command or permission request. Ranking fields describe
its score and reasons. Queue, focus, snooze, withdrawal and closure timestamps
record its lifecycle. An early-look step can ask a question while its session keeps
working, yielding its queue place to ordinary steps.

The queue has four places. Further prepared steps wait in Ready next. The user's
active work, drafts and timers affect whether a step may move. Item fields set by
the user survive agent upserts, and closed work requires an explicit user action
to reopen it.

### Messages, permissions, sessions

Messages record sender, target, provider, delivery state and outcome.
Permission requests record the call, explanation, decision and retry information.
Session records and Codex thread registrations connect replies with their items.
History stores versions of steps, user actions and session turns.

### Tracking, sweeper and other state

Other tables hold goals, projects, sweeps, feedback, shared pages, images, timers,
usage events, briefings and writing examples. Actor fields identify who changed
state. JSON-valued columns are decoded by the database helpers.

### Migrations

Schema changes are versioned in `db.py` and applied when the database opens.
Back up the database before changing the installed application.

### Backups

Use SQLite's backup interface to obtain a consistent copy while the server runs.
Keep backups private: a board can contain conversations, links and shared reports.

## 4. Server loops

Background workers deliver queued messages, refresh session state, collect
history, resume sessions after usage limits clear and schedule sweeps and checks.
They share persisted state with HTTP handlers. At startup and daily, a version
check reads installed versions and release metadata for enabled providers. A
problem lasting a day warns the sweeper; the user installs tool updates. Errors surface in server logs;
failed delivery is recorded so the user or sweeper can act on it.

## 5. Sessions, messages and delivery

### Session listing (`sessions.py`)

Claude session listings are cached so page polling does not repeatedly run a slow
command. Refreshes retain the last usable listing when a provider call fails and
surface a warning. Delivery looks up the target again before sending.

### Messages

A message starts as `queued`, becomes `sending`, then ends as `sent` or `failed`.
New sessions start only in allowed checkout paths. Target selection uses the
explicit session, step session or item's primary session. Per-session locks keep
concurrent sends ordered.

### Delivery (`Deliverer._send`)

Claude agent messages can use a running session's socket. User messages preserve
the authority of a user turn through stop-and-resume when needed. Stopped sessions
resume in their working directory. A new background session's returned identity
is linked to the item. Failed sends retain the reason instead of pretending the
work was handed off.

Codex threads use one private `codex app-server` subprocess. Cockpit initializes
its newline-delimited JSON protocol, correlates requests and receives notifications
on a reader thread. It uses the CLI's existing sign-in and managed restrictions,
and explicitly starts and resumes threads with `workspace-write` and `on-request`
approvals. New Claude sessions use Opus 5.5 at high effort; new Codex sessions use
Sol at high effort. `config.MAIN_MODEL` and `config.SESSION_OPTIONS` define these
defaults; `delivery.py` passes the selected model and effort explicitly. The
new-session menu can override them, and saved Codex threads retain those choices.

### Waiting, Interrupt and send, Take back

Messages can wait for the active turn. The user can interrupt and send, or take a
message back while it is still held. Follow-up text joins a held message or queues
after a message already delivered. Delivery state determines which actions remain
available.

### Emergency session (`emergency.py`, SPEC SESSION-21)

The emergency conversation uses a small separate page and its own state file.
It avoids the main page's rendering and database dependencies, so it remains
useful when those parts fail. It still depends on the running server, browser
sign-in and an available provider.

### Ask and Fork (`sidechat.py`, `store.fork`, SPEC MSG-11)

A side question can use a copy of the session's context while the original work
continues. Its answer returns to the same item. A fork creates a separate item for
work that should have its own conversation.

### Failures

Failures are persisted with reasons. A sweep can retry or dismiss them and ask
the owning session to resolve the cause. An uncertain delivery is marked as such
rather than silently resent.

### Dialogs, questions, limits

Claude hooks report interactive permission requests and auto-mode refusals.
Codex command, file, permission and structured-question requests are routed to
user-only endpoints. Approvals apply to the requested call or turn. Completed
turns, resolved requests and disconnects clear stale cards.

### Archive and step commands

Archiving removes completed work from the active view while retaining history.
Messages can resume saved sessions. A step command includes an explanation and
runs only after the user's action; its outcome is recorded on the step.

## 6. Permissions

Claude permission hooks and rule editing are separate from Codex approvals.
The browser can allow or deny a request and add a note for the session.
Explanations describe the operation and effect in plain words, alongside the
actual tool input. Rules and one-time grants are kept distinct.

## 7. Queue, ranking and the sweep

### Places and ready next

Owning sessions write actionable steps; the sweeper scores them. A step remains
identifiable across revisions, handoffs and returns. Focused work stays readable
while changes arrive.

### Ranking (`rank.py`)

The sweeper weighs the factors in the user's inbox settings together: priorities,
recency, urgency, blockers, closeness to completion and the size of the user's part.
The server enforces queue and focus rules around those scores.

### The sweeper session and launches (`sweeper.py`)

A long-lived session keeps context between runs. Wake-ups collect relevant board
changes rather than launching a new sweep for each write. The sweeper's own writes
avoid waking another sweep. Launch and activity markers distinguish active work
from an abandoned run.

### Wake-ups

Changes to steps, priorities, feedback and items can request a sweep. Wait
conditions and times can wake an item's session. A full sweep reads configured
connected sources; a light sweep focuses on the board and sessions.

### Checks

A check verifies specified steps against current state. It records freshness
without consuming unrelated pending sweep changes. A stale step is revised or
withdrawn, preserving the user's work.

### The user away (SWEEP-11)

Sweeps slow and pause while the user is inactive. On return, a short reorder can
restore the queue before the next full update. A paused queue should leave a useful
first action that is likely to remain valid.

### Sweeps

The bundled [sweep reference](../src/cockpit/skills/cockpit/references/sweep.md)
describes full, light, fill, check, reorder and pause runs. Each run records what
it checked and which sources were unavailable.

### Patterns (`patterns.py`)

Events record actions and recurring problems. Summaries let the sweeper inspect
patterns without rereading repeated errors individually.

### Morning briefing (`briefing.py`, VIEW-8)

Briefings summarize activity from collaborators the user selects. The configured
timezone determines the morning schedule. Collection depends on the selected
agent's available connectors. The summary comes first; detail remains available
on the briefing page.

## 8. Tracking and usage

Timers record active time on steps and allow corrections. Presence signals and
idle handling distinguish activity from an unattended browser. Usage views show
provider limits, token use, cache use and work by session or skill. Account
selection considers availability and reset times; changing an existing session's
account requires the user's action.

## 9. API

The route definitions in `app.py` describe the request models. Main route groups
cover board state, items and steps, messages and sessions, permissions, sweeps,
feedback, pages, spec and skills. Sign-in routes are separate from agent API
access. Requests that fail validation return errors; user-only and session-only
routes enforce their caller restrictions on the server.

## 10. CLI

Run `cockpit --help` and each subcommand's `--help` for current arguments and JSON
contracts. Writes require `--as NAME`. Typical operations include `get`, `item put`,
`step put`, `send`, `page add`, `feedback` and `sweep`. `spec` commands edit the
configured repository's spec through the server.

## 11. The page

The main page lists items beside the selected step or conversation. Other pages
show sessions, usage, briefings, shared pages, the spec and skills. The browser
polls state and applies action results immediately, restoring the previous state
and reporting errors when an action fails.

A server build version identifies changed assets. Open pages offer Reload when it
changes and preserve drafts and open rows. Cmd+K opens search and actions; `?`
opens shortcut help. The optional Vim prompt editor is bundled locally.

Shared HTML pages render in sandboxed frames. Page comments and choices return
to the owning conversation. Images and inline figures help explain a decision
without requiring a separate visit.

### Spec and skills pages (`spec.js`, `spec.py`)

Markdown headings, paragraphs, list items and table rows become editable blocks.
Requirement row IDs give stable block keys. The eight spec columns are ID,
Requirement, Origin, Strength, Status, Evidence, Last change and Comments.

Part edits match the exact previous text once; whole-document edits compare a
version hash. Conflicts return an error rather than overwriting newer content.
Writes are serialized, saved through a temporary file and committed individually.
Configured repository and branch checks protect spec and skill edits. A tracked
dirty deployment checkout refuses spec edits and names the affected files.

## 12. Tests

```bash
uv sync --extra test
.venv/bin/python -m pytest -q -p no:warnings
uvx ruff@0.17.3 check src tests
uvx ty@0.0.89 check
```

The Python fixtures create temporary databases, repositories, provider settings
and fake agent processes. `tests/test_spec.py` checks Markdown block parsing and
spec editing. Provider tests use fake protocol requests rather than live model
calls. See [browser test instructions](../tests/browser/README.md) for separate
browser checks and their environment requirements.

## 13. Deployment

Start with the local installation in [the README](../README.md). For a persistent
service, install into a dedicated environment so checkout edits leave the running
package unchanged. macOS uses `cockpit service install`; Linux can use a systemd
user service whose `ExecStart` points to the installed `cockpit-server`. Give the
service the PATH needed by Git and the selected agent tools.

For remote access:

1. Set `auth_mode = "google"`, the allowed email, and an HTTPS origin in setup.
2. Create a Google OAuth web application client with redirect URI
   `<origin>/auth/google/callback`. Save its JSON privately as
   `~/.config/cockpit/google-oauth-client.json`, mode 0600.
3. Put a TLS reverse proxy in front of `127.0.0.1:8790`. Strip incoming
   `Authorization`, `Forwarded` and `X-Forwarded-*` headers. The server disables
   proxy-header interpretation. Agents use the loopback API directly.
4. Start the server and sign in as the configured Google account.
5. For Claude permission cards, install the two hooks shown in the README.

For a manual service update, reinstall the package into the service environment
and restart that service. The page offers Reload when it sees the new build.
For the macOS `cockpit deploy` workflow, see [LOCAL.md](LOCAL.md). Configure your
own repository branch and update policy; no organization-specific branch is needed.
