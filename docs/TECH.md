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
| History and usage | `history.py`, `patterns.py`, `spend.py`, `timesheet.py`, `usage.py`, `resources.py` |
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
sandbox policy without same-origin access. Pages shared outside Cockpit use a stricter
one, described under "Public pages".

### Public pages

The user can share a page outside Cockpit (SPEC API-5, API-7, API-8). Each page has one
link, `<public_origin>/p/<token>`, where the token is 32 random bytes. The page's
**Share menu**, on its heading in the Pages tab and in the layer that opens it over
Cockpit's main page, says who that link lets in:

- **Only me**, the default. The link answers 404.
- **Specific people.** A visitor signs in with Google on the public host, and gets in
  when their address is on the page's list. The user's own address always gets in.
  Gmail spellings of one address count as one person: dots, `+tags` and
  `googlemail.com` are folded together. Other domains are compared as typed, ignoring
  case.
- **Anyone with the link.** No sign-in. The menu asks first, because the page's code
  then runs for strangers.

The menu also has Copy link and New link. Only the user changes the sharing, from the
page being read, and the request carries the version on the screen
(`PUT /api/item-pages/<id>/sharing` with `access`, `revision` and, for Specific people,
`people`). Every change records an event and wakes the sweeper. A session can choose
Only me and nothing else. It sees who can open a page (`access`), but never the link
or the list of people. The older `POST`/`DELETE /api/item-pages/<id>/public` routes
and `cockpit page unpublish` still work and map onto the same choice.

Changing the choice keeps the link, so links already sent work again when the page is
shared again. Only me keeps the list of people for later. **New link** replaces the
token, for a link that reached the wrong person: the old link stops working for good,
and the page keeps its sharing at the new one.

`public_origin` is a host of its own, because shared pages can run a session's
scripts. Cockpit's own host never serves a shared page, and the public host answers
only these requests, with a 404 for everything else:

| Request | What it does |
| --- | --- |
| `GET`/`HEAD /p/<token>` | The page, or a small sign-in page for Specific people |
| `GET /p/<token>/sign-in` | Starts Google sign-in (with PKCE) for a page shared with people |
| `GET /p/google/callback` | Google sends the visitor back here; Cockpit sets the visitor cookie |
| `POST /p/sign-out` | Clears the visitor cookie; the `Origin` header must be the public host |

Cockpit's own sign-in cookie and session tokens count for nothing on the public host.
The **visitor cookie**, `__Host-p_visitor`, holds the visitor's address, an expiry 30
days out and a signature made with a key derived from Cockpit's cookie key. It is
`HttpOnly`, `Secure` and `SameSite=Lax`, and it lets the visitor into pages shared with
that address and nothing else. A visitor who isn't on the list sees "This page isn't
shared with you" with their address, and can switch accounts or sign out. Neither
small page names the page or its task. The OAuth client needs
`<public_origin>/p/google/callback` as an authorized redirect URI, and its consent
screen must be in production for people outside the user's test list to sign in.
Leaving `public_origin` out turns sharing off, and local mode refuses it.

All of this rests on the `Content-Security-Policy` response header, so check after
installing that the reverse proxy keeps it. The policy gives the page a sandbox with
scripts and `confirm()` and nothing else: it can't save, open a window, start a
download, or read cookies, so it can't read the visitor cookie either. It loads
nothing from other websites, and scripts, styles and fonts have to sit inside the
file. Other limits:

- **A page's script can still send what it knows elsewhere.** It can move the visitor's
  tab to another website with data in the address, and WebRTC (a browser's way of calling
  another computer directly) still sends packets. Chromium does not apply the proposed
  `webrtc 'block'` policy. So a shared page can ask a visitor to type
  something and pass it on. A script can also check where or when it runs and act
  differently on the public host than in Cockpit, so the version the user read pins
  the file, not what visitors see. Anyone with the link says so; the user should share
  only pages whose code they trust. A page with no scripts has neither problem.
- **A session with file access can get around "only the user".** It can sign a cookie
  with the key on disk or write to the database. The API check stops a session using the
  API, and each change to sharing wakes the sweeper. Real separation needs sessions to
  run as a different computer user.
- **A shared link stays until the user picks Only me.** Closing or archiving the task
  leaves it working. The `N shared` button on the Pages tab lists every shared page.
  Sharing again revives the old link, so a leaked link needs New link, not Only me.
- **A leaked link can slow Cockpit.** Shared pages have no rate limit, so many
  readers of a large page at once slow every other request.
- **Google's word is taken for some addresses.** Anyone can make a personal Google
  account with an address that isn't Gmail, such as a work address. Cockpit accepts
  that account for its address, as Google does. Someone who once made one with a work
  address keeps it after leaving that job, so a page shared with that work address
  still lets them in. Addresses on the user's own Workspace domain, and the user's own
  address, are safe: they get in only from an account in that Workspace.

Tokens stay out of the table of past presses. The server's request log shows `/p/…`
for every public-host path and drops the query string, which carries Google's sign-in
code on the way back. The reverse proxy's public site should keep no access log, pass
only the four requests above, pass the `Cookie` header, and strip `Authorization`,
`Forwarded` and `X-Forwarded-*`. For example, with Caddy:

```
{
	log default {
		format filter {
			wrap json
			fields {
				request>uri regexp ^/p/.* /p/…
				request>headers>Referer delete
			}
		}
	}
}

(cockpit_public) {
	reverse_proxy 127.0.0.1:8790 {
		header_up Host p.example.com
		header_up -Authorization
		header_up -Forwarded
		header_up -X-Forwarded-For
		header_up -X-Forwarded-Proto
		header_up -X-Forwarded-Host
	}
}

p.example.com {
	header Strict-Transport-Security "max-age=31536000"
	@read {
		method GET HEAD
		path_regexp ^/p/([A-Za-z0-9_-]{43}(/sign-in)?|google/callback)$
	}
	@signout {
		method POST
		path /p/sign-out
	}
	handle @read {
		import cockpit_public
	}
	handle @signout {
		import cockpit_public
	}
	handle {
		respond 404
	}
}
```

Caddy keeps no access log for a site without a `log` directive. Its error log still
records a request that fails, say while Cockpit restarts. The global filter above
replaces the whole path and query string, and drops the `Referer` header, which holds
the link when a visitor presses Sign in. Caddy already hides the `Cookie` header in
logs while `log_credentials` stays off, its default.

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
it across updates. It holds the ask, reason, body, collapsed details, core links,
draft reply, answer buttons and optional command or permission request. Core links
are at most three `{label, url}` links the step is about, each label 1–40 characters
and each address distinct; omitting them on an update keeps them. Ranking fields describe
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
A backup also holds the secret links of pages that were shared when it was taken, and
restoring it makes those links work again. After restoring one, end them all with
`UPDATE item_pages SET public_token = NULL, access = 'me'`, then share again whatever
should be. Each then gets a new link.

## 4. Server loops

Background workers deliver queued messages, refresh session state, collect
history, resume sessions after usage limits clear and schedule sweeps and checks.
They share persisted state with HTTP handlers. At startup and daily, a version
check reads installed versions and release metadata for enabled providers. A
problem lasting a day warns the sweeper; the user installs tool updates. Errors surface in server logs;
failed delivery is recorded so the user or sweeper can act on it.

### Processor and memory tracking (`resources.py`, SPEC TRACK-16)

A thread in the server reads `/proc` every 5 seconds and credits each program's
processor time, and every minute its proportional memory (Pss), to a session and
a part (the kind of program). A program finds its session through Cockpit's
session listing, then its parent chain, then `CLAUDE_CODE_SESSION_ID` in its
environment, which a child takes from its parent when it starts, so a
background program keeps its session after its parent ends. Programs with none
are the machine's own. A program that runs another in its place is classified
again. Another user's program, and one that shuts out its owner too (ssh-agent
does, to guard its keys), is a system service measured by its resident size;
the refusal is expected and logs nothing. The module docstring gives the rules.

Memory counts at every sample: each program's latest reading times the seconds
the sample covers. Two peaks are kept per row, both the most held at one sample:
`session_peak_mb` (the row's session, all its parts) and `part_peak_mb` (the
row's part, all sessions). A task's peak adds its sessions' peaks per row, so
parts that ran one after the other are not added up; two sessions of one task
that ran one after the other within ten minutes are, so a task's peak is at
most that much high. Time of children that end
between samples comes from the parent's child counters; when a parent collects a
child while a sample is read, the shortfall is carried in `owed_ticks` until the
counters include it, so nothing counts twice.

Every minute the thread adds its totals into ten-minute rows in `resource_use`
(per session and part) and `resource_host` (the machine's busy time, load, free
memory and pressure stalls), and writes each session's current figures to
`kv.resources_now`; `now_tasks` adds them up by task for the machine lamp and
"Right now". A failed write gives the totals back to the sampler for the next
one, and sampling, writing and folding fail and log separately. Hourly, rows
older than 30 days fold into `resource_days` and `resource_host_days`, kept for
good. `/api/resources` reads them for the Server page. Slots and days use each
row's own offset from UTC in the user's zone, so a clock change moves nothing.
A gap longer than 30 seconds between samples, such as a suspended host or a
restart, counts nothing.

### Scheduled prompts (`schedules.py`)

A schedule is a prompt that Cockpit starts as a new session in a checkout, daily or
weekly at a time in setup.toml's `timezone`. Sessions put and remove them with
`cockpit schedule`, which the standing rules allow. The server lets only the user
pause one, and the page has no control for it. Every minute a loop finds schedules whose latest slot is newer than
the `last_slot` they ran. For each one, a single transaction puts the run's item
(`note:schedule-<name>-<date>-<HHMM>`), queues the prompt as a message that starts a new
session on it, and records the slot. Recording the slot with the message is what
keeps a slot from starting twice, across restarts too. A new schedule, a changed
time, or a resume waits for the next slot. After downtime, only the latest missed
slot runs, once. A run that cannot be queued keeps its error on the schedule, logs
it, shows it as a page warning, and is tried again on the next check.

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

The browser opts into synchronization with `X-Cockpit-Sync: 1`. Its first
`/api/state` response is complete and carries an `X-Cockpit-State`
checkpoint, an identifier for the response's field hashes. The page sends that
checkpoint as `X-Cockpit-Since` on its next read.
When it is known, `X-Cockpit-Base` echoes that checkpoint, which the browser
compares with its current checkpoint before applying a partial response. The JSON object contains
`changes` (replaced top-level fields) and `removed` (deleted field names). The
server keeps hashes for the last 64 sync responses overall, keyed separately for
the signed-in user and token-authenticated agents, and still builds every state from current
data. An unknown checkpoint returns a full state, including after a restart.
A nonempty `X-Cockpit-Since` also opts in, preserving compatibility with earlier
pages that send a checkpoint without `X-Cockpit-Sync`.
Ordinary API and CLI reads return full JSON without computing checkpoint hashes.
The page keeps a separate copy of the checkpoint's state in memory
so local optimistic changes cannot alter it.

Sync responses encode each top-level field once and hash those bytes. A partial
response retains serialized bytes only for changed fields while building its body.
Full bootstrap and recovery responses reuse all the
encoded fields too, avoiding a second serialization of the complete state.

Markdown HTML is reused in a 512-entry process cache. Keys contain the source,
rendering mode, and only the item, step and figure references used by that source.
Renamed mentions and updated page figures therefore render again. Documents over
32,768 characters render directly. Empty or whitespace-only Markdown returns an
empty string before cache-key construction.

Folded ready-next steps count toward Inbox and keep their items out of other groups
before their rows are built; only visible rows create DOM nodes. Database rows bypass
the JSON parser for the common `[]` and `null` literals; each empty list is a fresh object, and
other JSON values retain the normal parser and its validation.

A step card shows its links under its text, above its answers (`app.js coreLinks`):
a chip for each core link, then "Other N", which opens the task's other links. Its
"On this task" group holds the item's pages and links; "Mentioned earlier" holds the
links of the task's other steps (their core links, then those written in their body
and details; closed steps too) and those in the user's messages on it. The server gathers those as each state item's
`mentioned_links` (`store._mentioned_links`, two queries for all items in the state):
`{label, url, step_id}`, with `step_id` null for a message, labelled by the markdown
link's words or, for a bare address, the address without its scheme. Code spans are
skipped. The page shows each link once, leaves out one the step's text already holds
(except a shared page) and draws a page icon before a link to a shared page within a
sentence. The step's ⋯ menu holds no links.

A server build version identifies changed assets. Each page carries the version that
sent it in a `cockpit-version` meta tag, and is built per request with no ETag so a
304 cannot keep an old version on a new page. Open pages offer Reload while the
running version differs from their own, and take the key away if it goes back. The
main page reads the running version from state; the spec, briefing, usage and shared
pages list check `/api/version` through `page-update.js`, each minute and when they
come back into view.

Reload keeps drafts, open rows and the place on the page. Pages holding writing only
in memory give it to `PageReload.hold`, which carries it across the reload in
sessionStorage, and read it back once with `PageReload.take`. On the spec, writing on
a part that changed meanwhile lands in a banner instead of an editor over the newer
text. These pages draw after their first fetch, so `page-update.js` also puts the part
in the middle of the window back where it was, since the browser's own scroll restoring
finds nothing to land on.

Cmd+K opens search and actions; `?` opens shortcut help. The optional Vim prompt editor
is bundled locally and loads when Vim is enabled. Native reply boxes work while
it loads; the upgrade keeps their drafts, quote display elements and focus.

### Installing as an app

Every page links `manifest.webmanifest`, which browsers fetch without the cookie, so
that route and the icons are public. `scripts/render_app_icons.py` renders the PNG
icons from `icon.svg`. An installed window has no address bar, which is why the
pages other than the main one need their own Reload.

There is deliberately no service worker. Files reach the installed window through the
same HTTP cache rules as a browser tab, and Reload stays the only way a page changes
version. An installed window therefore needs the server: with no connection, or during
a restart, it shows the system's or the proxy's error page until it is reopened.

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
user service whose `ExecStart` points to the installed `cockpit-server`, in
`~/.config/systemd/user/cockpit.service` so that `cockpit deploy` restarts it. Give
the service the PATH needed by Git and the selected agent tools.

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
6. For public pages, give them a host of their own and set `public_origin`. Point its
   site at the same `127.0.0.1:8790` with the proxy setup under "Public pages" in
   section 2: it passes the four public requests and the `Cookie` header, which a
   visitor's sign-in needs, and strips the headers above. Keep no access log for it,
   and keep the link out of the proxy's error log, which records a request that fails
   while Cockpit restarts.

For a manual service update, reinstall the package into the service environment
and restart that service. The page offers Reload when it sees the new build, in an
installed window as in a browser tab.
For the `cockpit deploy` workflow on macOS and Linux, see [LOCAL.md](LOCAL.md). Configure your
own repository branch and update policy; no organization-specific branch is needed.
