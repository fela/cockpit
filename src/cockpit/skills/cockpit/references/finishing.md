# Finishing

Open this when work is done, or when the user presses Task done or archives your step.
The parent `SKILL.md` applies too.

## Only the user closes a task

The user closes an item through Task done, a closing answer or Close item.
A message saying "done" still needs that press, subject to the merged-PR, fire-and-forget and recurring-run exceptions.
Outside those exceptions, every session proposing completion writes its own finish step, including the sweeper and cleanup sessions.
Use need `complete`, then `complete:2` and onward after earlier finish needs close.

An agent put of `stage: done` or `archived: true` usually needs an open finish step first.
It leaves the item awaiting the user's press. Ordinarily, the finish step alone is enough.
Keep the actual work stage. Use a done put only for the exceptions or to answer a handed finish step with a new note.
Cockpit refuses leaving `prod` work with no wait and no open step. Put the finish step before that change.
When the last step is handed, the complete put can close it as [Handed a step](handoffs.md#handed-a-step) describes.

Put any remaining trial or check before completion. Keep agent, person or release work in `waiting_on`.
Withdraw an unhanded finish step while work remains, then restore it with complete text when ready.
Keep a handed finish step until its request is fulfilled.

**Merged-PR exception.** Cockpit checks GitHub on a done put. A merged PR can close its item without another confirmation.
All remaining open steps must be finish steps. Close other resolved needs first and supply a one-line note.
The CLI prints `closed: …`; the user sees the note above the next step.

**Fire-and-forget exception.** A to-do added that way makes its session responsible for completion.
Choose routine details and work to the end. Share a step only for a question, issue or action requiring the user.
Keep early looks, read steps and finish steps out of this flow.
Close resolved steps, then put `stage: done` with a one-line note. Cockpit accepts the close and shows the note.

**Recurring-run exception.** The session of a scheduled prompt's run, whose item is `recurring` and its own, closes it itself when the run has nothing to report.
Close resolved steps, then put `stage: done` with a one-line note saying so. Cockpit accepts the close only when no step but a finish step is open on the item, none of them handed back to an agent and none under the user's timer; it refuses the close without a note.
The exception covers the schedule's latest run only: once the next run starts, an earlier run's item closes with a finish step.
A run with something for the user puts its step as usual.

Cockpit refuses `done` or `dismiss` on a finish step the user did not hand over.
Give every ordinary step close a readable reason. After the user's close, a further close asks nothing.
A final check may reveal follow-up work. Create a linked item and request a sweep when needed.
An item archived as unnecessary stays archived; explain renewed importance in a sweep request.

A step's `closes` proposes other completed items for the user to close together. The user can untick items.
Use it only when each selected task is finished.

## The last step of finished work

Write the finish step after the work is done and checked, including unnecessary tasks.
Merge a duplicate into the item that holds its work with `cockpit item merge` instead, as [Merging duplicates](merging.md) describes.
Read closed steps, user messages and session conclusions first, even when another session did the work.
Name the outcome in the ask: "form saves both fields correctly; check it and close".
The body answers these in order, about a sentence each, and keeps the rest for a collapsed section:

- What the user requested and when. Quote their words when they bear on completion.
- What was done and how it was checked.
- What remains and who has it, linking any separate item carrying that work.

Show visible features working with screenshots from where they run. Attach them and place them in the body.
Put the main screenshot right after the opening sentence, so the user sees the result before reading on.
For a job or API, show its result through a log, table or sample output.
Run the [reader check](writing-a-card.md#the-reader-check). Set size `small`.

| The user presses | Result |
| --- | --- |
| Task done | Closes every step, marks the item done and informs its session. Follow any accompanying final message in that turn. |
| Close item | Archives it with the user's reason. |
| Send | Hands the step to its session; the task stays open. |

Task done already appears among the controls. A finish step usually needs no answer buttons.
Add a closing answer only when it conveys something extra, such as "I've sent it myself; close".
Dismiss and Close item belong to the user. On completion, save authorized work and end without another step.
Commit or push only where the task and repository policy authorize it.
Put work left after Task done on a new item that links the finished one.

## Archived by the user

When Cockpit says the user archived your last step, it stops the session after that turn.
Save authorized work, update the item's status, and end without a question. A later message resumes the session.
The sweeper checks archived sessions for unfinished work.
