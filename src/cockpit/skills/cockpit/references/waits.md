# Waits and repeats

Open this when work waits on someone, rests until a time, or should run every day or week.
The parent `SKILL.md` applies too. `cockpit item put --help` lists the `waiting_on` fields.

## Who the work waits on

Each `waiting_on` entry names the session, bot, person or release in `who`.
Add `what`, a word or two, to name the action awaited, such as "review" or "answer".
The user's part belongs in a step. Agent rests use `until` and `why`.
Every bot, person or release needs `until` and any supported live check.

## Resting work

For work that resumes at a set time, put `until` on its agent entry in `waiting_on`.
Add `why`, at most 60 characters, explaining the rest. Add `by_user: true` when the user chose the time.
That work appears under "Snoozed by you"; other rests show "Agent resumes later".
`until` records the planned rest; it does not wake the session. Set your own wake-up for that time, such as a background job or a scheduled run.

Work becomes stalled 30 minutes after `until`, or after its last activity when no time is set. The sweeper then wakes its session as stalled work.
End a waiting turn with a step, a planned agent rest, or a bot, person or release wait.
Rest for a time the work or user chose. Treat a resource limit as a problem to resolve within granted permissions.
When it needs the user's choice, show the cost, timing and consequence of each feasible option.

## Wake conditions

Every waiting item has a way to wake again. Sessions and stall detection cover active agent work.
Every bot, person or release entry needs `until`, when the item's session should check again.
Where supported, add a `check` that wakes it sooner:

```json
{"kind":"release","who":"the app release","until":"<ISO time>",
 "check":{"kind":"reached","stage":"staging","pulls":["https://github.com/example-org/app/pull/10"]}}
```

A `reached` check holds when all listed PRs reach the stage, including through a promotion or linked cherry-pick.
It works only in repositories with branches named exactly `dev`, `staging` and `prod`. Elsewhere it never holds, so rely on `until` alone.
Use `staging` or `prod` for these release checks. Cockpit checks every ten minutes.
It wakes the session when time comes, the condition holds, or the check fails three consecutive times.
An item without a session gets a new one in its project's checkout.
On waking, check live state and give the item its next move in that turn.
Put the user's step or a new wait with a new `until` and appropriate check.
Cockpit asks again every two hours while the wait remains unchanged. The sweeper repairs missing or stale conditions.

## Work that repeats

For a prompt that should run again every day or every week, put a scheduled prompt with `cockpit schedule put --as NAME`.
Use it in place of a script, crontab line or cloud routine; it needs no Run or Allow from the user.
Each run starts a new session in the schedule's checkout on a fresh item, titled with the run's date, in its project.
Times are in the user's zone. A new schedule first runs at its next time, never at once.
Write the prompt so a session with no other context can do the work.
`cockpit schedule list` shows each schedule's next and last run; `cockpit schedule remove NAME --as NAME` deletes one.
The sidebar lists each run under Background, since its item is `recurring`.
Work that repeats on another timer, such as a crontab line that sends to one item, puts `"recurring": true` on that item so it shows there too.
A scheduled prompt's run with nothing to report closes its own item, as the recurring-run exception under [Only the user closes a task](finishing.md#only-the-user-closes-a-task) says.
An item that several runs share stays open, so the next run can still put a step on it.
