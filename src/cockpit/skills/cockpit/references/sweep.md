# The sweeper's procedure

The sweeper decides what comes first on Cockpit. Read this file at the start of every sweep, check and pause.
The parent `SKILL.md` applies too, including readiness, completion and step writing.
Owners prepare work, write steps and run reader checks. Check their facts against live state and leave substantive rewrites to them.
When writing a step yourself, use the same writing and reader checks.
Use only available services and connectors. Name unavailable sources in the result.

## Sweeper commands

```bash
cockpit sweep start --as sweep
cockpit sweep finish SWEEP_ID --as sweep --summary "Full: what changed"
cockpit sweep finish SWEEP_ID --as sweep --error "what failed"
cockpit feedback
cockpit feedback --all
cockpit feedback --mark-read sweep --through ID
cockpit feedback --mark-read sweep --ids ID,ID
cockpit patterns --days 14
cockpit errors list
cockpit errors list --all
cockpit errors take --ids ID,ID --note "fix-page-errors" --as sweep
cockpit step yield STEP_ID --as sweep
cockpit message dismiss MESSAGE_ID --as sweep --reason "later delivery succeeded"
cockpit session close UUID --as sweep --reason "work finished; nothing runs or waits"
cockpit step put --as sweep <<'JSON'
{"item_key":"gh:example-org/app#10","need":"merge","score":75,"reasons":["one quick action unblocks it"]}
JSON
cockpit progress put --as sweep <<'JSON'
[{"item_id":10,"line":"The code-review bot reviews the form fix"}]
JSON
```

## Who moves next

Open work appears below the queue in two groups, folded when the page opens:

- **Busy:** an agent works now or resumes within an hour. The hover shows its goal, handed ask or summary.
- **Waiting:** other open work. The hover names the person, bot, release or planned agent rest.

Agent work moves to Waiting after sessions stop for an hour, or when resumption is more than an hour away.
Cockpit determines the next move in this order:

| First condition that holds | Next move |
| --- | --- |
| Open, unsnoozed action step | The user |
| Handed step | Agents |
| Agents, bots or people in `waiting_on` | Agents, then bots, then people |
| Release wait, or stage `dev` or `staging` | Progress toward the next stage |
| Finished work | Done |
| Session at planning, building, review or no stage | Agents |
| None of these | Find who moves next |

A `read:` step reports a result. Ongoing agent work still belongs to agents.
With no agent work or wait, the user reads it; that item does not stall.
For finished deployed work without a wait, put its finish step. Keep any unfinished obligation explicit.
The sweeper follows the same closing rules as owners. Leave fire-and-forget completion to its owning session.

## How each kind of work becomes ready

### Agent work

Ask the owner to finish conflicts, CI fixes, answerable threads, drafts, descriptions, research and review preparation.
Find its UUID in the item first, then available provider history, searching recent work before older archives.
Start a session only when none exists, using `cockpit send --item ID --new-in /path/to/checkout --as sweep`.
Choose the checkout associated with the project. Combine several asks for one session in one message.
Send at most one preparation request per session per six hours, using `last_agent_message_at` where available.
Leave active work and a session the user used within 30 minutes to continue.
Record failed sends and retry on a later sweep. Set a new wait only after delivery succeeds.
For timed work, put its due time in the summary and send it on the first sweep after that time.

### The user's own PRs

| State | Action |
| --- | --- |
| Approved, CI green, conflict-free, threads resolved | Owner puts a merge step. |
| Finished agent work on a draft | Owner marks it ready. |
| Settled PR missing required bot review | Owner requests the team's code-review bot with attribution. |
| Thread needs the user's judgment | Owner prepares analysis and a recommended reply in a decision step. |
| Bot review exceeds its normal turnaround | Owner uses an available independent review process under repository policy. |
| Bot, person or release still working | Keep a timed wait and its supported check. |
| Idle for more than three weeks | Prepare a grouped keep-or-close step with a verdict for each PR. |

Request reviews after the branch settles. Keep the reviewed head stable and retry a stopped run once.
Use generic repository policy for further reviews and shared actions.

### PRs the user reviews

Prepare a brief and draft review after CI passes, conflicts are resolved and required bot reviews finish.
The brief covers behavior, size, risk, other reviews and links. Fold bot findings into it.
For more than about 2,000 changed lines, offer a review session through the composer's controls.
Prepare base conflict fixes within repository permissions. Leave failing CI and requested substantive changes with the author.
Set `waiting_on` to that author. Preserve the distinction between an assignee and a requested reviewer.

### To-dos

Add starts an ordinary session for each user to-do. Its message contains the user's words, links and images.
That session does agent work and writes the steps. Rank them with the rest of the queue.
An `untriaged: true` item has no session, open step or wait. Read it and decide the next move:

- A session failed to start: send the user's words with `--item ID --new-in /path/to/checkout`.
  If that fails again, put a step explaining the error.
- Only the user can act: put the reminder, call, errand, decision or personal reply directly as a step.
  Include useful links and deadlines. Maintain the item yourself.
- Agent work can advance it: start a session in the relevant checkout and let it prepare the step.
- The intent is unclear: ask which outcome the user wants, with your best guess first.

Check duplicates first. Give the item a three- or four-word subject while preserving every requested part in its summary.
Shorten old titles beyond five words or 40 characters, starting with queue and ready-next items.
Treat tasks found elsewhere without a PR, ticket or thread as plain to-dos, using `todo: true` and need `define`.
Deliver answers to quick Add questions promptly. Ask the owner for an answer when one is missing after about ten minutes.

### Blocked sessions

Cockpit turns unanswered session questions into steps within about 30 seconds.
They use `updated_by: cockpit`, need `question:<uuid>:<time>`, the session's needs and quoted last message.
Rewrite on the same item and need, adding context, useful answers, score and reasons.
If it belongs elsewhere, put it there with the blocked session as `session_id`. Cockpit withdraws its automatic step.
Give any leftover session item a plain title, summary and project. Finish an empty duplicate through a finish step linking the destination.
For a status-only last message, dismiss the automatic question with a reason and update the summary.

### Messages and shared decisions

An unanswered request from Slack, Linear, GitHub or a document becomes a `reply:` step with a checked draft.
Use the source key and link. Put shared decisions on their most relevant item and link the others.
An explanation or agreement needs no reply step when nobody waits on the user. Report that fact to the requesting session.

### Questions people wait on

Rank prepared questions high, including blocked agents and people awaiting a reply, personal action or requested draft.
Use reason "question for you" and preserve user-set priority. A short answer unblocking someone can outrank a long priority review.

### Only what will hold

Apply the parent skill's readiness rules for moving CI, active reviews, recent replies and dependent decisions.
State what held work waits for in its summary. Keep it without an ordinary step until that action is ready.
A blocking user question can still have a step. Respect the handed-step return protocol.

## Sweep

### Choose the run

A requested full sweep checks every configured available source. A wake-up specifies full or light and lists changes.
Start with new steps, replies, feedback, goals, to-dos and agent requests in that message.
A light sweep skips broad source collection, but still checks assigned PRs and review requests through available GitHub access.
When queue plus ready next has fewer than four steps, also collect available GitHub and Linear work.
Collect the same way when no queued step serves the top goal that holds now, from the sources that goal's work comes from.
Exclude handed steps from that count. Repair wake conditions in every sweep, including light ones.
Cockpit requests a full sweep after roughly three hours without one. Only summaries beginning `Full:` count as full.
After an hour of inactivity, wake-ups slow. After two hours, Pause stops ordinary wake-ups and checks.

### 1. Start the sweep

Run `cockpit sweep start --as sweep` and retain its ID. A 409 means another sweep owns the run; stop and report it.
Write as `sweep` throughout. Twenty minutes without a sweep write allows Cockpit to abandon the run.
Your own sweep writes do not wake you again.

### 2. Read goals, feedback and health

Read `cockpit get --json`, `cockpit goals`, `cockpit inbox-settings`, feedback and `cockpit patterns --days 14`.
Inbox settings define ranking factors and sources to check. Follow the user's wording and order ahead of defaults.
Goals hold in priority order; set paused goals aside. Preserve lasting feedback after marking it read.
A goal's own words say what work it covers and when it holds: a goal that names hours or days holds only then.
Read the user's time from `local_now` in `cockpit get --json` or the clock line in Cockpit's message; leave UTC conversion to Cockpit.
A goal the user changed recently (`last changed` in `cockpit goals`) is their freshest instruction: rescore every step against it.
Treat comments about Cockpit's behavior as feedback, including ones entered as goals. The user removes misplaced goals.
Treat feedback about the user's work priorities as ranking guidance. Apply step-specific feedback to that step now.
Record Cockpit feedback and spec comments as requirements. Handle skill comments in the configured skills repository under its policy.
Group comments on one skill into one owning session, including the user's words and quoted passage.

Use `note:cockpit-improvements` for repeated problems. Check existing open and closed needs before proposing `improve:<subject>`.
Say what repeats, what it suggests and the proposed fix. Write and reader-check these steps yourself.
Read `cockpit errors list` for unclaimed page errors. Send them to one fix session, then mark their IDs taken.
Errors on page `server` are the server's own logged errors; the fix session reads their stack as a Python traceback.
For a problem a pattern names, `cockpit logs`, `cockpit requests` and `cockpit presses` show the server's warnings, how each route answered, and the user's presses in order.
Send a repeated error to its previous session when still active. Keep one error-fixing session per sweep.
Full sweeps also inspect two weeks of feedback and replies for recurring confusion, ineffective controls and recurring requirements.
Check failed sends, queued messages over 30 minutes, sessions without progress and unclear handed steps.
The user's queued messages deserve attention within about 15 minutes. Prepare fixes or concrete questions for unresolved causes.

### 3. Re-check the user's steps

Check queue and ready-next facts: latest CI, conflicts, threads, owner updates and newer source replies.
Check every relied-on screenshot is attached to the item and placed in the step text. Attach missing figures when available.

| Verdict | Action |
| --- | --- |
| Valid | Record `cockpit step checked ID --as sweep --note "what was checked"`. |
| Need gone | Close the ordinary need with a reason. Apply finish-step rules to completion asks. |
| Ready with a stale fact | Record the change and ask the owner to update the same need in place. |
| Fails readiness | Withdraw it and ask the owner to prepare it. |
| User snoozed it | Preserve it and request any necessary update. |

Handed steps belong to their agents. Exclude them from re-checking and ranking.
Ask an idle owner to finish its hand-off. If that session is gone, inspect the delivered message and latest work.
Answer on the same step and return with a note explaining the outcome or missing answer, under the parent's return protocol.
A handed finish step stays handed while its requested work remains. Use a replacement owner when needed.

### 4. Collect work that may need the user

Follow Inbox's configured sources. Check sources through connectors or tools actually available.
Where authorized independent workers are available, split slow collection by source and collect short lists with links and state.

- **Sessions:** read `.sessions` and `.waiting_sessions`, excluding the sweeper itself.
  Inspect unanswered questions and recent assistant conclusions. Use provider history for older linked sessions when available.
- **GitHub:** check open PRs assigned to the signed-in user and PRs requesting their review, including light sweeps.
  In full collection, also check their drafts, requested changes, re-reviews and assigned open issues.
  Read latest checks, reviews and threads. Ignore cancelled runs replaced by newer checks.
  Refresh changed PRs and open steps after the previous sweep. Verify bot waits and sessions quiet for three hours against live GitHub.
- **Linear:** check available notifications and linked tickets for questions, mentions, priority and due dates from the last three days.
  Full sweeps and low queues also collect unfinished assigned work in the current cycle, at high priority or serving a goal.
- **Slack:** check available mentions, direct messages and saved messages in their threads from the last three days.
  A request needs the user while unanswered. Include document review requests until the user replies or comments.
  Saved messages become items once, preserving existing open or closed items. The saved mark can remain after completion.
- **Notion:** in full sweeps, check available comments, mentions and review requests from the last three days.
  Read linked pages and comments. Use `note:notion-<page id>` for unanswered page requests, with a link and prepared questions.
- **Archived sessions:** inspect the last two days for unfinished work, uncommitted or unpushed changes, CI and open threads.
  Resume unfinished work with `cockpit send --session UUID --as sweep`, stating what remains.
- **Idle sessions:** inspect live background sessions idle over an hour with nothing running or waiting.
  Close only finished sessions with `cockpit session close`. Keep uncertain waits; Cockpit can refuse closure while work remains.
- **Agent closes:** inspect `.closed_recently` since the previous full sweep. Verify merged PRs, posted replies and answered questions.
  Send unresolved work to its owner for a new need under the closing rules.

Live source state takes precedence over an old summary. A PR assigned elsewhere retains that owner despite a review request.
Prepare the user's actual part and record ownership correctly. Claim only sources and facts you checked.

### 5. Look for duplicate and related work first

Before creating an item or starting or messaging a session, search keys, links, UUIDs and recent available history.
Try source URLs, short identifiers, titles and branches. Search recent history before older transcripts.
For identical work, keep the existing tracker and send it the user's request. Update its summary.
Put a finish step on the duplicate, quoting the request and linking the answer or continuing work.
Tell the duplicate's session where work moved. Deliver any awaited answer on the asking item too.
For related separate work, keep both items, link them both ways and reuse the session that knows the context.

Update stage, waits, links and goal links from live state. Refresh links for changed or removed goals.
Set priority from the user's instructions and source urgency while preserving fields listed in `kept`.
A dependency names the other item and its next owner. The user's action belongs in a step, rather than a person wait.
Keep release follow-up on an open item, with every relevant PR linked and a wake condition.

### 6. Rank steps and preserve places

Score ready steps under "Score and reasons". Preserve user controls and update ready steps in place.
Withdraw only work that is no longer ready. Restore a withdrawn step with its full text and fields.

### 7. Get the top items ready

Prepare four queue places and one or two spares fully. Ask owners to finish agent work and verify readiness.
Send task, links and this brief: "Finish the work and check it. Put the step yourself with your session UUID.
Follow Asked to get ready in the cockpit skill. Leave score and reasons to the sweeper."
Start a session in the project's checkout only when no owner exists. Preserve active user conversations.

**Prepare in two depths.** Up to four further items get a quick step from existing knowledge, in about ten minutes.
Ask owners to use "Asked for a quick step". The body opens "Quickly prepared: <what remains unchecked>".
Cockpit gives a quick step a queue place as soon as one frees, with no separate preparation state. Ask for quick steps only where the ask stays right from what is already known, and never a quick `merge` or `approve` step. Upgrade quick steps when they enter the four places or spares, or when fewer than four fully prepared steps remain.
Ask these owners before searching for more work. Remove the quick-preparation line after verification.

Bring work forward in full sweeps and low queues: unfinished idle work, resolvable blocks, PR preparation, delayed reviews and assigned tasks.
Ask owners to resolve resource limits within authorization. Put a user decision when cost, timing or permissions require it.
Give each active goal a working session, prepared step or owner asked this run. Split work from a busy session when needed.

#### Stalled work

Read `stalled_since` every sweep and fill. Wake every non-low-priority stalled owner in this run, including fire-and-forget work.
State when work stopped and ask it to resume, identify a blocker, or put the question only the user can answer.
For planned rest, require agent `until` and `why`, plus `by_user` when appropriate. Revisit unjustified or excessive rests.
If a recent wake stalled again, read the last message and act on the blocker yourself.
Replace a gone session in its checkout. Bring low-priority stalled work forward while the queue is low.

#### Work no one moves

`cockpit get` lists open work no one has the next move on beyond untriaged to-dos, such as an agent's note left with no session, wait or step.
Give each a next move in this run, as for an untriaged item: ask its session or start one in the project's checkout when agent work can move it.
Put the step only the user can take, or the finish step when the work is done.
This work and stalled work fill the page's Stuck heading; an item still there after a sweep was missed.

#### Waiting work and wake conditions

Every full sweep checks Waiting items unchanged for a day against live reviews, replies, releases and agent rests.
Update moved work and tell the owner. Prepare completion when finished, or a drafted nudge when the user can help.
If the wait remains correct, put a dated checked summary. A three-day person or bot wait usually deserves a nudge unless away.

Every sweep repairs missing `until` values and supported checks on bot, person and release waits.
Replace the whole wait list and tell its owner. Move stale release checks to the next relevant stage and include missing PRs.
Suggested times: next working day for a person or the next stage, a few hours for a bot, release day for a release.
Use a `reached` check on `staging` or `prod` with the change's PRs. Cockpit checks every ten minutes.
For deployed work without a step, wait or active session, ask its owner to check it and put the next move immediately.
After the user tests a change, ensure follow-up and its next wait happened under the configured workflow.

#### Search again when the queue is low

Recheck empty searches before finishing. Report counts for withdrawn work, GitHub, user waits, promises and ticket cleanup.
Inspect withdrawn steps through item history. Close obsolete needs or ask owners to restore valid ones with full text.
Recheck live assigned PRs, unanswered nudges and stricter-than-required waits. Get missing steps for user decisions.
Read available posts from the last three days for promises of follow-up. Prepare missing promised replies.
Group superseded or stale tickets in one cleanup item with verdicts, keeping at most one such item open.

### 8. Close obsolete steps, clear failed messages and blocked calls

Close vanished ordinary needs with a readable reason. Follow the parent's rules for finish steps and handed work.
After a merge, tell its owner to check deployment and continue promised follow-up.
For failed messages: dismiss superseded deliveries; fix and retry your own needed messages; resend other senders' messages through `cockpit send`.
Dismiss an old failure only after successful replacement or when a prepared step carries the remaining user decision.
When a queued user message was already relayed, dismiss it with that explanation so it is not delivered twice.
Failures needing sign-in or another user choice become prepared steps on their item's owning session.
That session prepares a sign-in by the bundled `sign-in-links` skill.
For blocked calls, ask owners to withdraw obsolete calls or use an authorized alternative.
Only-user permission needs get a narrow `permission` step with context. Leave live terminal permission dialogs for the user's answer.

### 9. Prepare work and put the Matters most list

Use Fill's preparation rule when the queue stays short. Keep suggested work below existing obligations.
Replace the Matters most list through `cockpit progress put --as sweep` every sweep.
Choose three to five important items waiting elsewhere without a user ask. Give each a line of at most 90 characters.
Say what it waits for and when the user hears back. Rank by goals and priority; `[]` clears the list.

### 10. Finish and report

Mark handled feedback read using its captured IDs. Finish the sweep before an authorized service restart.
Use `cockpit sweep finish ID --as sweep --summary "Full: ..."`, or `Light:` for a light sweep.
Keep the summary within 300 characters. Include queued steps, ready next, owners asked, quick preparations and failed sends.
Mention work brought forward, low-queue search counts and `prepared: N` when applicable.
Name checked and unavailable sources. Use `--error` when the sweep cannot finish. A full-sweep warning means run one next.
Repeat the summary as the final message, ending with a statement so it creates no stray question step.

## Feedback

`cockpit feedback` lists unread comments. `target` identifies a page, spec block or skill passage; `quote` supplies context.
Read the requested outcome first. The configured `spec_repo` holds code and spec; `skills_repo` and `skills_folder` locate skills.
Prepare changes in a separate worktree. Record lasting behavior in `docs/SPEC.md`, preserving IDs and the eight-column table.
Use neutral shared examples. Test affected behavior with the repository's Python, lint and type checks; inspect interface changes in a browser.
Record unavailable checks. Have an independent reviewer inspect the diff, request and requirements; fix verified findings and recheck.
Follow the user's update policy for commits, pushes and service changes. Use the configured `deploy_branch` and `cockpit deploy` where supported.
Manual services require installation into their environment and a restart through their service manager.
If a choice remains, put concrete options, a recommendation and the prepared result. Name any missing permission or tool.
Mark handled feedback read with `cockpit feedback --mark-read sweep --ids ID,ID`. Finish the sweep before restarting.

## Fill

Fill runs when the user views a queue with at most one ordinary step and nothing ready next.
Use a requested full sweep instead when one is due. Aim to finish in a few minutes.
Start and finish a registered sweep. Read state, goals and Inbox settings; wake stalled owners first.
Upgrade existing quick steps first, then check available assigned PRs, reviews, tickets, replies, withdrawn steps and idle work.
Ask long-running sessions for useful early looks. Prepare one to three concrete short actions, still meeting readiness for their ask.

When fewer than two steps remain and no urgent or high item is about to supply one, prepare one or two new ideas.
Keep at most two suggested items open. Choose uncovered goal work, assigned tasks, promised follow-ups or research ideas.
Check `note:idea-<slug>` first. An existing key means it was offered before; choose another.
Create a planning item saying the sweeper suggests it. Start a session in its checkout with the work and why now.
Prepare reversible work up to one concrete user action: read, research, review, test, build an isolated change or draft a message.
Keep preparation to about an hour, or prepare the first useful piece. Follow existing permissions for shared actions and posting.
Put an ordinary step opening "The sweeper prepared this because your queue ran low."
State the goal, work done, result, user's action and follow-through. Offer that action plus "Not now" as a closing answer. A `merge…` step refuses closing answers: offer the merge alone. If the user declines in a reply, the owner puts a finish step, and the user closes the suggestion item with it.
The user can also reply with questions. Score suggestions below every step something already waits on.
Finish with `Fill:` and source yields, owners asked and `prepared: N`. Cockpit manages later fills; finish without a polling loop.

## Check

A check names the top one or two steps. Check only those; leave sweep registration, collection and ranking for sweeps.
Read `.queue` and each item. Verify CI, conflicts, threads, owner updates, newer replies, body, ask, draft and inline screenshots.
Use the verdicts in sweep step three. A finish step closes by the user's press; rewrite or withdraw when it no longer holds.
Preserve user snoozes and edits. If ranking needs attention, use `cockpit sweep request --as check --reason "what changed"`.
Using `check` lets the request wake the sweeper. End with one line per step: verdict and evidence checked.

## Pause

After two inactive hours, Pause checks the named top steps once. Cockpit manages stopping and restarting sweeps.
User messages, hand-offs, to-dos, goals, feedback, Update and Restart can resume them.
Choose the quickest step that will stay valid while the user is away, preferably finishable in a minute or two.
Use a prepared decision, own-PR merge, to-do or personal review. Pass over moving CI, active reviews and threads.
Among quick stable steps choose the highest score; otherwise choose the highest stable step. Keep the queue if none will hold.
Raise that step's score above others, leaving quick finish steps above it when appropriate.
Use reasons "quick to start with" and "stays valid while you are away".
If needed, yield the lowest unprotected queue place so the highest ready-next step enters. Respect user controls and timers.
End with what is first, its duration and why it will hold. Leave polling to Cockpit.

## Reorder

A reorder rescores existing queue and ready-next work within two minutes. Collection, live checks and owner requests await the next sweep.
Run `cockpit sweep start --as sweep --reorder`, then read state, goals and Inbox settings.
Rescore from existing item and step text. Put only item identifier, need, score and reasons for changed scores.
When the user is still away, keep a quick stable step first under Pause. Sweeps remain paused.
Finish with `cockpit sweep finish ID --as sweep --summary "Reorder: what moved; what is first"`.

## Score and reasons

Score is a zero-to-100 judgment of attention deserved now. Only the sweeper sets score and reasons.
New steps start at 50. Cockpit adds no special points; judge steps together afresh.
Cockpit puts a step whose `expires_at` is still ahead first in the queue and keeps it there; score it as you would with no time.
User-taken steps follow, newest take first. Other steps follow score, ties favoring older steps.
The strongest ready-next fills a place; more than ten points above an unprotected step can displace it.
Returned answers compete by score. Early looks yield to ordinary waiting steps regardless of score.

The user's Inbox factors replace the usual factors below. Weigh every factor that applies, by its place in their list:
a higher factor counts clearly more than a lower one, and lower factors outweigh it only when several add up to a clear case.
Removed defaults stay removed. The usual factors, for a user who set none, are:

- Urgency: deadlines, serious failures and someone blocked on the user.
- Someone waits: quick Add answers, returned replies, prepared questions and promised results. Bring quick answers forward promptly.
- Morning briefing: judge the day's prepared briefing with other work; its starting score is only a starting point.
- Quick to finish: one-press answers, merges and finish steps. Longer work gains weight when already underway.
- User priorities: work serving a goal that holds now gains the most for the top goal, less for later ones.
  Work outside every goal holding now gains nothing here, also when it serves a goal for other hours.
  Work serves a goal when the goal's words cover what the work is, whatever project or goal link it carries.
- Close to done and fresh in mind: recent user effort and the last action before shipping or closure.
- Not forgotten: older valid work can rise enough to remain visible.

Weigh consequences, due dates within two days and assigned work. Keep nearby top scores a few points apart.
When a better task outranks work the user is doing, score it clearly higher and explain the proposed switch in its first reason.
Let the user choose Reorder or Take it. Preserve their current place and timer.
Owners set size and progress. Fill only missing estimates on queued or ready-next steps, counting the user's time alone.
Use up to four reasons of at most 60 characters, naming the actual deciding factors. When a goal lifts a step, or a goal for other hours holds it back, say which goal.
Examples: "question for you", "assigned to you", "last action before completion" and "unblocks a teammate".
Prepare the user's action on assigned work. Offer reassignment only when they cannot act, with the ownership and consequence clear.
