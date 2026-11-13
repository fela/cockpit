# Local hosting and Codex

Start with [the README](../README.md) to install from a fresh clone. A laptop uses
local sign-in by default through `cockpit setup --local`. An HTTPS deployment can
use Google OAuth. Provider selection and browser authentication are independent.

## Local authentication

`auth_mode = "local"` accepts only `http://127.0.0.1:8790` or
`http://localhost:8790`. The server binds IPv4 loopback and disables proxy-header
handling. Each request checks the socket peer and exact Host, including its port.
Browser writes require the configured Origin, including sign-in and sign-out.
Use Google authentication with HTTPS for access through a forwarding proxy.

`cockpit login` sends the private mode-0600 `local-login-token` to
`/auth/local/code` over loopback. The returned code works once and expires after
two minutes. The CLI opens `/auth/local#<code>`; if no browser opens, it prints
that one-time address with the two-minute deadline and exits successfully. The page removes the fragment
before exchanging it for a signed `cockpit_local_session` cookie with HttpOnly,
SameSite=Strict and Path=/. Google mode uses Secure cookies. Neither mode accepts
the agents' bearer token as browser sign-in.

Removing the local login token and restarting rotates it. Replacing `cookie-key`
invalidates existing browser sessions. Keep all generated authentication files
outside the repository.

## Running a service

The README's `.venv/bin/cockpit-server` runs in the foreground on macOS or Linux.
For a macOS service whose installed code is separate from checkout edits, run
these commands from your checkout:

```bash
uv venv --python 3.12 ~/.local/share/cockpit/venv
uv pip install --python ~/.local/share/cockpit/venv/bin/python .
export PATH="$HOME/.local/share/cockpit/venv/bin:$PATH"
cockpit setup --local --provider codex --name Sam --timezone UTC
cockpit service install
cockpit login
```

If you already ran setup, keep the existing setup file and skip that command.
Stop any foreground server before starting the service on the same port.

`cockpit service install` writes a per-user LaunchAgent with an absolute Python
path and `-m cockpit.server`. It starts at login and restarts on failure.
PATH is captured at installation so provider tools remain available. Logs go to
`~/.local/share/cockpit/logs/server.log`. `cockpit service status`, `start` and
`stop` manage it. On Linux, run the server directly or configure a systemd user
service using the installed executable and the PATH your provider tools need.
Name its unit file `~/.config/systemd/user/cockpit.service`, so that `cockpit deploy`
restarts it.

The host must remain awake and online for Cockpit and its agents to work.
On Linux, Claude commands run in their own systemd scope; on macOS they run
directly. `claude_sockets_dir` overrides the platform socket directory if needed.

## Updating the service

`deploy_branch` names the branch of `spec_repo` that `cockpit deploy` installs.
Setup uses the checkout's current branch. Keep the deployment checkout clean and
on that branch. Spec and skill edits use the configured repositories and branches;
they can commit and push changes, so use repositories you intend to edit.

`cockpit deploy` updates the branch from its remote, builds its committed tree in
a scratch environment and checks that `cockpit.server` imports. It then pushes the
branch and reinstalls that tree into the service's Python environment: on macOS
the Python the LaunchAgent runs, and otherwise the Python that runs the command.
On Linux, run the service environment's own `cockpit`, for example
`~/.local/share/cockpit/venv/bin/cockpit deploy`. A push the remote rejects stops
the deploy before anything is installed, with one exception: when the remote
branch only moved ahead of the deployed commit during the build, the deploy
finishes and says to deploy again. Uncommitted changes are excluded; a
divergent branch or a commit that fails to import is refused.

The command schedules a service restart after it returns: the LaunchAgent on
macOS, or on Linux the systemd user service `cockpit` when
`~/.config/systemd/user/cockpit.service` exists. Without either, it says to
restart the server yourself. Open pages offer Reload when the build version
changes. For other service arrangements, reinstall into the service environment
and restart it using your service manager.

## Providers and threads

`providers` enables Claude, Codex or both. `default_provider` chooses new sessions
unless a request selects one. Existing sessions keep their provider. Explicit
provider selection persists across message retries.

An independent local worker runs `codex_agent.py` and owns its private
`codex app-server` subprocess. The web server calls that worker over a private
Unix socket at `~/.local/share/cockpit/codex.sock`, restricted to the current user.
Routine web-server restarts reconnect to the same worker, active turns and pending
approval requests. The worker communicates with app-server using
newline-delimited JSON over standard input and output. It correlates request IDs,
reads notifications on a dedicated thread and routes interactive requests to
user-only endpoints. The worker opens no network listener. The child inherits the CLI's
login and managed restrictions, with the installed environment on PATH so agents
can invoke `cockpit`. Cockpit explicitly starts and resumes threads with
`workspace-write` and `on-request` approvals.

New Claude sessions default to Opus 5.5 at high effort; new Codex sessions default
to Sol at high effort. These defaults come from `MAIN_MODEL` and `SESSION_OPTIONS`
in `config.py`, and `delivery.py` passes the selected model and effort explicitly.
The new-session menu can override them. Registered Codex threads retain their
model and effort when resumed.

Command and file approvals apply to the current call. Permission grants cover at
most the requested permissions for a turn. Structured answers return against
their original request ID. Unknown interactive requests receive a protocol error.
Completion, resolution and disconnects clear stale prompts. Replies enter item
history with their turn timestamps.

The worker starts on demand. On Linux it runs as a separate user service managed
by systemd, named `cockpit-codex-<database-path-hash>`, so a systemd user session
is required even when running the web server in the foreground. The caller's
environment is forwarded when the worker starts, including PATH and CODEX_HOME
when set. Changes to that environment take effect when the worker next starts.
The CLI keeps its configured login and model provider, including a configured
custom API server.
On macOS the worker starts as a separate launchd job, named `cockpit.codex.<id>`.
Its log is `~/.local/share/cockpit/logs/codex-worker.log`; Linux logs go to the journal.
Worker warnings and errors also appear in Cockpit's server logs.

The worker continues using a private copy of its Cockpit package across deploys.
Copies live under `~/.local/share/cockpit/codex-worker-code`; remove old copies
only after stopping their workers. Worker code updates take effect when that worker
next starts. Before explicitly stopping or updating the worker, check that no
Codex turn or request is active. On Linux, find its unit with
`systemctl --user list-units 'cockpit-codex-*'` and stop that unit. The next request
requiring Codex starts the worker with the newly installed code. On macOS, stop the worker
with `launchctl bootout gui/$(id -u)/cockpit.codex.<id>`, using its job label from
`launchctl list`. Stopping only the web service leaves Codex work running. A machine reboot or worker loss interrupts
turns and clears pending approvals. Messages whose delivery status cannot be
determined stay failed until an explicit retry. Database changes and the protocol between the web server and
worker must remain compatible with a surviving worker.

The Linux restart test uses disposable systemd services and a fake Codex process.
It verifies the same process and approval request survive, with one prompt
execution. The macOS launchd path still needs an equivalent live check.

Cockpit resumes only threads in its registry. Other desktop and terminal threads
can appear in usage views, but Cockpit does not control them. The SQLite database
retains thread IDs for later continuation after worker loss.
Disconnects and timeouts record failures or uncertain delivery. A new thread is
attached to its originating message and item before its first prompt is sent,
so the conversation can still be found if the web response is lost. A message left
mid-delivery by a restart stays failed with an explanation that Cockpit cannot
tell whether Codex received it; inspect the
thread before explicitly retrying.

Provider tests use fake processes and protocol requests. A live smoke check, when
wanted, should use a disposable thread: send a short prompt, verify completion
and archive it.

## Public pages

Local mode has no public pages. Cockpit answers only this computer, so a public link would reach nobody. The Pages
tab leaves out Make public, and the server refuses a setup file that sets `public_origin`.
