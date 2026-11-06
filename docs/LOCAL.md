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

The host must remain awake and online for Cockpit and its agents to work.
On Linux, Claude commands run in their own systemd scope; on macOS they run
directly. `claude_sockets_dir` overrides the platform socket directory if needed.

## Updating a macOS service

`deploy_branch` names the branch of `spec_repo` that `cockpit deploy` installs.
Setup uses the checkout's current branch. Keep the deployment checkout clean and
on that branch. Spec and skill edits use the configured repositories and branches;
they can commit and push changes, so use repositories you intend to edit.

`cockpit deploy` updates the branch from its remote, builds its committed tree in
a scratch environment and checks that `cockpit.server` imports. It then reinstalls
that tree into the LaunchAgent's Python environment and pushes the branch. A
failed push is reported. Uncommitted changes are excluded; a divergent branch or
a commit that fails to import is refused.

The command schedules a service restart after it returns. Open pages offer Reload
when the build version changes. For other service arrangements, reinstall into
the service environment and restart it using your service manager.

## Providers and threads

`providers` enables Claude, Codex or both. `default_provider` chooses new sessions
unless a request selects one. Existing sessions keep their provider. Explicit
provider selection persists across message retries.

`codex_agent.py` communicates with a private `codex app-server` subprocess using
newline-delimited JSON over standard input and output. It correlates request IDs,
reads notifications on a dedicated thread and routes interactive requests to
user-only endpoints. It opens no network listener. The child inherits the CLI's
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

Cockpit resumes only threads in its registry. Other desktop and terminal threads
can appear in usage views, but Cockpit does not control them. Shutdown interrupts
the app-server; saved thread IDs remain available for later continuation.
Disconnects and timeouts record failures or uncertain delivery.

Provider tests use fake processes and protocol requests. A live smoke check, when
wanted, should use a disposable thread: send a short prompt, verify completion
and archive it.
