# Cockpit — reasoning

Cockpit gives one person a way to direct many agent sessions without repeatedly
reconstructing what each needs. [SPEC.md](SPEC.md) holds requirements;
[TECH.md](TECH.md) describes the implementation. This document explains design
choices without retaining personal conversations or work records.

## Access

Local sign-in makes a laptop installation possible without an OAuth client or a
public hostname. It is restricted to loopback and checks the request's host and
origin. HTTPS deployments use Google OAuth to admit one configured account.

Browser sessions and agent tokens have separate roles. Agents can prepare work
and identify their writes; only the user can answer approvals and send messages
with the user's authority. Exact Origin checks protect browser writes beyond
what cookie attributes alone establish. A reverse proxy strips authorization
headers so remote requests cannot use the loopback agent credential.

Signed cookies keep browser sessions simple. Replacing the cookie key signs out
all browsers; signing out one browser clears its own cookie.

## Scope

An item represents work from any source, and a step represents the next action
that needs the user. Pull requests, tickets, messages and agent sessions fit this
model, so the user has one queue instead of a queue per service.

Installation-specific names, identities and paths belong in setup. Validation
makes a missing or invalid value visible at startup. The bundled skill provides
agent instructions alongside the application, so a fresh clone can work without
a separate private repository.

## State

Stage and next move answer different questions. A task can be in review while
waiting on a person, a bot or an agent. Recording both makes the board useful
without reopening every linked conversation.

A task that needs the user has an actionable step. Naming the user in a wait list
would leave the board claiming it needs attention without saying what to do.
A wait on someone else carries a time or condition for checking again, so waiting
work can advance when its blocker clears.

## Agent API and sweep

Owning sessions know the details of their work. They prepare evidence and write
their own steps. The sweeper sees the whole board and ranks across items. Keeping
these roles separate makes scores comparable without asking one session to learn
every task's history.

The API enforces user controls as well as the CLI. Otherwise an agent calling an
endpoint directly could overwrite a priority or reopen a dismissed step. Agent
writes preserve fields the user set.

A long-lived sweeper retains context. Combining wake-ups after a burst of changes
reduces repeated work, and excluding the sweeper's own writes prevents a loop.
Light sweeps inspect local state; full sweeps also check configured connected
sources. Unavailable sources remain explicit gaps in the result.

## Next steps

A small queue limits context switching. Preparing a few spare steps keeps it
useful when the user clears work faster than sessions prepare it. Preparing too
far ahead spends effort on decisions that may change.

A ready step supplies context, evidence and an action. It answers what happened,
why the user is needed, what each option changes and what follows. Extra detail
can be folded or linked, while information needed for the decision stays visible.
An independent reader can catch assumptions the owning session no longer notices.

A stable step survives questions and rewrites. Returning an answer on the same
step lets the user find it where they asked. Focus, drafts and timers protect
work the user is reading or answering from surprise changes.

## Ranking

The user's priorities compete with urgency, blockers, quick actions, recency and
closeness to completion. A sweeper can weigh these together in context rather
than forcing every task through a long fixed points table. Server queue rules
protect ongoing work while the scores change.

Waiting and unseen work still need review. Otherwise items without recent
activity can remain hidden indefinitely. A short reorder on return restores a
useful first action before a slower source refresh finishes.

## Goals

One priority list expresses what matters across tasks. Separate feedback controls
keep instructions about the work distinct from requests to improve Cockpit.

## Focus, control and overview

The next step is the main reading surface. A compact work list provides context
without competing with the decision. Folding inactive work leaves it available
without making the user scan it on every visit.

Actions show their result immediately and undo it with an error when refused.
This makes slow connections tolerable. Stable placement, preserved drafts and
explicit reloads protect work from background updates.

## Messages to agents

User messages retain their authority when delivered to an agent. Delivery tracks
queued, sending, sent and failed states so the board can distinguish a real
handoff from a failed attempt. Per-session ordering keeps follow-ups in sequence.

Take back holds a message briefly before delivery. That gives undo a defined
effect. After delivery, a correction is another message rather than a claim that
the earlier message disappeared.

Codex threads started by Cockpit have a known owner and a saved registry.
Controlling only those threads avoids competing with another active controller
for terminal or desktop conversations.

## Pages

HTML reports, charts and images give complex decisions enough room. Shared pages
belong to items, so they remain findable after a step closes. Sandboxed rendering
keeps report content separate from the signed-in application. Comments return to
the conversation that can act on them.

## Emergency session

A separate, minimal conversation page can keep working when the main interface
breaks. Its small asset set and separate state reduce shared failure points.
The server and provider must still be available.

## Permissions

An approval should show what the operation changes alongside its actual input.
A plain explanation helps the user decide without searching a transcript.
One-time grants remain distinct from standing rules. Claude hooks and Codex
protocol requests use their respective permission models.

## Tracking

Timers distinguish active work from an unattended page and allow correction.
Usage views connect provider limits and token use to the work that consumed them.
These records help identify repeated friction and costly checks.

## Item history

History connects the user's words, the session's answer and the resulting step.
Keeping exchanges together lets the user reconstruct why a task is back without
reading an entire transcript. Earlier detail can stay folded until needed.

## Feedback tool

Feedback points to the part of the page concerned and keeps its text for context.
That gives the agent a concrete target. Writing examples retain the marked text
and its source so later revisions do not erase what the user judged.

## This spec

Markdown keeps requirements readable beside the code and versioned in git.
Stable row IDs support links and comments even when requirements are removed.
Exact-text and version checks catch overlapping edits instead of overwriting them.
The evidence and change columns remain available for an installation's own notes;
the public copy omits private conversation history.
