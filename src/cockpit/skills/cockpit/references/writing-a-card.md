# Writing a card

Open this before you put or rewrite any step. The parent `SKILL.md` applies too.

The user reads a step cold between many tasks. Assume they have forgotten the session and earlier decisions.
The item title names the work; the ask names the next action. The page shows the last message above the body.
The reply box uses `reply_hint` as its placeholder.

Load the bundled `writing-style` skill before drafting. It lives beside this skill, at `../../writing-style/SKILL.md` from this file.
Read nearby good and difficult examples in `cockpit writing list`, including the user's notes.
Use `cockpit writing get` for the whole example. For long work, read an available example transcript too.

## Readiness

A ready step leaves only the user's part. Check these against live state immediately before putting it:

| Check | What must hold |
| --- | --- |
| Agent work finished | Conflicts resolved; latest continuous integration checks, called CI, pass; answerable threads handled; description current; drafts prepared; analysis current. |
| The user needed | A decision, approval, review, merge, personal reply or awaited result remains. |
| One action and piece of work | Ask for one kind of action. Review one PR. Related small cleanups may share a step, with a verdict for each. |
| Someone can continue | Name who acts on the answer and can start immediately. |
| The facts will hold | Resolve moving state and dependencies before asking. |

Hold work while CI runs, someone reviews, a session works on it, or a thread received a reply within an hour.
Also hold steps whose answer depends on a step above, including overlapping PRs and dependent decisions.
State the dependency in the summary. Use `waiting_on` while others work, apart from optional early looks.
A question that blocks that work gets an ordinary step now. Keep handed work under its hand-off protocol.

## Three checks just before the put

1. **Whose is it?** Read live ownership and review requests. Assignment and a review request describe different responsibilities.
   Prepare the user's own action. Offer reassignment when they cannot act, and explain surprising ownership.
2. **Did the user already answer?** Read closed steps and recent relevant session history.
   Check available Slack, Linear and PR replies. Act on earlier answers and ask what remains open.
3. **Is the PR ready now?** Check the current head, draft state, conflicts, CI and review threads under [Readiness](#readiness).

## Asking the user

Finish [Readiness](#readiness), write the step, set `session_id` to your UUID, and supply `size` and `progress`.
`session_id` overrides the item's primary session as the answer's destination.
Only the sweeper sets `score` and `reasons`. Leave the user's priority intact. Continue independent work.
For requests for suggestions, give ideas and a recommendation to discuss. Prepare posting text when the user asks for it.
When the ask is a sign-in, load the bundled `sign-in-links` skill before making a link or writing the step.
A Claude Code connector, such as Slack or Linear, signs in through `cockpit signin start`, which makes the link and the card itself.

| `size` | The user's work |
| --- | --- |
| `small` | A glance, click or one-line answer. |
| `medium` | Reading and deciding, or a short review. |
| `large` | A full review, design choice or feature trial. |

`progress` estimates the share of the user's own time already spent on this item, from zero to 100.
Agent time contributes nothing. Use zero for untouched work, 90 for merging after the user's review.
Use 50 after one of two equally long decisions. Supply your own estimate when setting or changing these fields.
Cockpit compares estimates with timer time to calibrate the displayed bar and sizes.

### Decisions

Put a choice that needs the user's judgment, such as a product choice on their own work, in a `decide:` step with a recommendation.
State any missing or conflicting requirement and its practical consequence. The user decides; further sign-off follows their instruction.
Make easy-to-reverse internal choices, and choices the user's own instructions settle, yourself. Record them in the step's details.

### A result the user waits for

Deliver a requested or awaited result as a step in the same turn, even when it needs no decision.
Put the result in the summary too. A terminal answer, document or summary alone does not deliver it.
Use `read:<subject>` and size `small`. Open with the result, its meaning and what happens next.
If related work moved to another item, answer on the asking item and link the related work.
After a merge of duplicates, the asking item is the main item the side item joined.
For a handed question, answer on that same step instead.

| After reading | Controls |
| --- | --- |
| The task ends | Empty answers leave Task done as the completion key. |
| Work continues | Supply non-closing answers naming next moves. Offer a closing answer only if it ends the whole item. |

A `read:` step has no separate acknowledgment key. Keep ordinary status updates in the item summary.
When a continuing task needs an acknowledgment, supply a non-closing answer and handle its hand-off promptly.

### Early looks

Use `early: true` for an early finding, clarification or feedback that could change or save ongoing work.
Use `early:<subject>` and size `small`. State your best guess and continue working on that basis.
An early look takes a place only while no ordinary step waits. It gives that place up when one does.
The user's answer interrupts current work. Act on it, then restart anything still needed.
Rewrite early looks as work advances. Withdraw one superseded by a final result or later question.
Put the final result on its own `read:` step. A blocking question becomes ordinary with `early: false`.
Supply a non-closing answer while the task continues, so Task done is not mistaken for acknowledgment.

### Steps that stop working at a time

Some steps are of no use after a time: a sign-in link that works for ten minutes, a code that runs out, a call that starts.
Give such a step `expires_at`, the time it stops working, with its UTC offset, such as `2026-01-15T10:10:00Z`.
State the same time in the card's text in the user's local time.
Until that time the step leads the user's queue, its Inbox row shows as a warning does, and its card counts the time down.
When the time passes with the step still open, Cockpit tells you once. Then do one of three things:
put the step again with what works now and a new `expires_at`, put it with `expires_at: null` when it is useful without the time, or withdraw it.
Use `expires_at` only for a step that stops working. Work that is due by a date is ordinary; the sweeper weighs its date.
The user may be away for hours. For a limit of a few minutes, prefer a step with no time:
its answer, such as "I am here, make the link", has you make the link the moment the user presses it.
Renew a passed time once at most while the user has not answered, then change to that kind of step.

## The body

The user reads the title and the ask, and often stops there. Each added sentence makes them less likely to read the body at all.
So the body holds only what the user needs to act, and the collapsed sections hold the rest.
The body is short enough that the user reads every word of it, every time.

**Card limits.** The server refuses a body over 80 words or over 900 visible characters.
When a card is over either limit, move whole sentences into a collapsed `details` section until the body fits.
Squeezing the same facts into fewer words makes the card harder to read.
Aim for 40 to 60 words: two to four sentences. Scale the length to the attention the step asks for.
The server counts visible text: link labels and image captions count, link addresses do not.
Quoted `>` lines count toward the 80 words but not toward the 900 characters. On a `reply:` step they count toward neither.

A step that takes the user little time and attention, such as a hand-off or a merge of an approved change, gets fewer words.
A decision that needs more context carries it in the first collapsed section, and the body says in a clause what that section holds.
Give the card short bold headings when it covers more than one topic, such as several questions the user asked.
A body longer than its collapsed sections usually holds context that belongs in them.

Choose what goes on the card by asking what would change the user's answer:

- **The result,** in one sentence: what changed, or the answer to their question.
- **Each question the user asked,** answered directly in one sentence, under their words as a bold heading.
- **The one fact that settles the first likely worry,** when there is one, with its comparison.
- **The recommendation and what the press does:** "Say yes and I merge, then check the live form."

Everything else goes in a collapsed section: background, what happened since the user last looked,
how the work was checked, mechanisms, further numbers, alternatives, follow-up lists and reviewer evidence.
The card already shows the user's last message and a line on earlier history, so leave those out of the body.
Leave out what the ask or the answer buttons already say.

A few kinds of step need one more line on the card. Keep it to that line:

- A posting step names the thread and where the text goes. A reply quotes the message in full with author, place and time; on a `reply:` step the quote counts toward neither visible limit.
- A draft review says what the PR changes and what the review asks its author to do.
- A real choice gives the recommendation and at most one alternative, each with what it changes.
  Use option names that describe the choice, the same in ask, body, details and buttons.
- When wording has two plausible meanings, say which reading you used.
- When the user must do something by hand, write every action on the card in order: where to click, what to type and full links.
  A pointer to a page or box comes only in addition to the steps.

These belong in a collapsed section, with at most a clause on the card:
how a concept works, what changed in recurring work and its load, the order and owner of several follow-ups,
the measured saving of a shortcut and who checked a speed claim, and a return to the user's original plan when a design grew complicated.
For a "step back" request, rewrite from that plan.
Treat agreement on an unclear step as uncertain evidence of intent, and restate the setup before a major trade-off.

For a PR over about 2,000 changed lines, offer a review session through the step's composer to explain it.
Recommend the simplest outcome that improves what people get today. Put minor review points in details.

### Wording on a card

The `writing-style` skill has the rules for sentences, words, lists and tables. A card adds these:

- "Step" means Cockpit's card. Call a migration a migration and a job a job.
- Name work by what it does for people, then give its linked identifier:
  "the sign-up fix ([app#10](https://github.com/example-org/app/pull/10))". Keep internal names in details.
- Explain familiar team systems briefly on first use. When there is a preview or page the user can inspect, link it near the opening.
- Give a measurement that affects the choice its own line or a short table.
- Before a quoted code block, say in a plain sentence what it shows.
- Write full sentences in place of em-dashes, parenthetical clauses and arrows.

### Bold keywords and headings

Bold the keyword of each sentence: the concept or thing the sentence is mainly about, in one to three words.
The user skims the bold words to see what each sentence covers and decides from them which sentences to read.
Bold one keyword per sentence at most, and leave sentences with no central concept plain.
Use section headings when a card or collapsed section covers more than one topic; one topic needs none.

### Second looks and rewrites

"Worth a second look" names one place where the user's judgment changes the outcome.
Give the consequence, likelihood and importance in a sentence. Put code review details and minor cleanups with their owner.
Use that heading when such a choice exists. A simple hand-off takes two or three sentences.
After an unclear hand-back, give every question its own heading on the card, each with a one-sentence answer.

Rewrite a late step from scratch after several updates. Use one worked example and plain choices.
Give related topics on one task a heading and a sentence each on its one card. Keep precise definitions in collapsed sections.
Check names describe what was actually measured. Compare options on the same baseline and explain mixed changes.
For risks, give frequency and typical duration alongside any worst case.
Keep time-sensitive actions in their own short steps when readiness permits.

When you rewrite a step's text after the user's timer ran on it, add `changed`: one line on what changed.
The card shows it in a band when the user read the card recently; Cockpit refuses the rewrite without `changed` when the band would show.
A separate concern goes on its own item, as "One task, one card" in the parent skill describes.

### Collapsed sections

The collapsed sections carry the context the body leaves out, so they are usually longer than the body.
Each one opens by saying what it covers, so it can be read alone. Give each a plain title.
When the user needs background to decide, the first one is "The situation from scratch".
The `writing-style` skill says what goes in it.

### Example

The user asks whether a sign-up form bug affected the page, saved records or both.

> **item title:** Fix saved form fields
> **ask:** form saves names correctly; merge the fix?
> **reply_hint:** Say whether the older saved names need repair too
>
> The fix saves **given and family names** in their right places; **CI and code review** pass.
>
> **Was the form shown wrong?** No, only the saved records were swapped.
>
> **Does this repair older records?** No, that has its own linked task.
>
> Say yes and I **merge**, then check a new sign-up after deployment.
>
> **answers:** "Merge it"
>
> **details:** "The situation from scratch" (how the form saves names, which records are affected, how the fix was tested)

The body is 45 words: the result, one sentence per question, and what the press does. Bold marks each sentence's keyword, and each question is a bold heading. It needs no separate `why`.

### Fields

`cockpit step put --help` lists every field and its limit. These rules decide what goes in them:

- `ask`: aim for 70 characters. A lowercase short line: rough outcome, semicolon, next decision. Use familiar names and keep links in the body.
- `why`: usually empty. The ask and opening sentence explain why the step is here.
- `reply_hint`: describe the expected kind of answer. Supply it whenever typed words are expected.
- `details`: the situation, numbers, limits and reviewer evidence, as [Collapsed sections](#collapsed-sections) describes.
- `draft`: the exact text to post. Prepare it while writing, and keep it understandable to the user.
- `core_links`: the links the step is about, such as the PR to merge or the page to read. The task's other links sit behind "Other".

Visible character limits count link labels. URLs, `**` bold markers, backticks and quoted `>` lines do not count; single `*`, `_` and `__` markers do.
[Card limits](#the-body) says how quoted lines count toward the body's 80 words. `reply_hint` is plain text: its limit counts every character.
Raw source caps still apply: 300 characters for ask and why; 20,000 for body, draft and each details body.

Text in `draft`, which the user posts under their own name, opens with the agent attribution line. Use the actual agent's name:

`**[Agent name]** _written and posted by Agent name on the user's behalf_`

Card text the user reads on Cockpit (ask, why, body and details) opens with the content itself. Cockpit refuses a step whose card text opens with the attribution line.

Never post under the user's name without agent attribution.

## Buttons

A non-closing button hands the step to `session_id`, or the item's primary session.

| Field or button | Rules |
| --- | --- |
| `answers` | Offer obvious likely replies, with the recommendation first. Otherwise use a reply hint, subject to the continuing-task rule below. |
| Permission to act | Name the action: "Merge it", "Approve and merge", "Start the run". State follow-through. Merge and approval asks need a non-closing action answer. A command uses Run. |
| Merge answers | Keep them non-closing while deployment follow-up remains. "I merged it on GitHub" hands back the follow-up. Cockpit refuses closing merge answers. |
| Closing answer | `{"text":"I've sent it myself; close","closes":true}` completes the whole item and every step. Use the user's voice and offer it only when that choice leaves no work on the item. |
| `command` | Prepare the exact authorized command. Its explanation states its overall effect and changes, then explains each part in order. |
| `permission` | Request the narrowest standing rule the work needs. |

A step with no answers has Task done as its only completion key. Use it when acknowledgment should finish the item.
For continuing work, supply non-closing answers and make the continuing work explicit.
For a quick action, give an answer such as "I added it; carry on" with `closes: false`.
When an answer reports that everything worked and no work remains, make it the closing answer; Task done makes way for it.
A non-closing answer says what the user did or what the session does next, and leaves the word "Done" to Task done.
Cockpit refuses a non-closing answer that starts with "Done": the user cannot tell it from Task done.
Process that hand-off promptly, since it blocks new steps. Keep pure status in the summary where possible.
Closing answers settle the entire task. A narrow decision during a long run needs `closes: false`.

## The reader check

An independent reader checks each card: a second model that has not seen the work reads it as the user would.
It checks both the wording and whether the user can act. Run it before the first put of every card, except a quick card,
and before changing what a step asks or explains, including rewrites after confusion.
A stale number or state update needs no new reader.

Write a brief file with a file tool: the reader brief below, then the title, ask, `why`, body, answers, reply hint,
every details section and each of the `core_links` with its label and URL.
Then run this one command, which reads the brief on stdin and writes the reader's answer to a file:

```bash
codex exec --skip-git-repo-check --sandbox read-only -o READER.md - < BRIEF.md
```

When `codex` is missing, run `claude -p --tools "" < BRIEF.md > READER.md` instead. Read `READER.md` with a file tool.

> You are the user. You know the product as its users see it and the team's daily work terms.
> You know nothing of the code, the scripts or this piece of work, and you last looked at it a week ago.
> Read the card below once, between other tasks. Answer each in one line:
> 1. Whose work is this? What are you asked to do? Could someone else settle it?
> 2. What did you last do or say? What happened since?
> 3. In two plain sentences, what changes for people if you say yes?
> 4. What is recommended? What happens after you answer?
> 5. What one thing would stop you acting or make you act wrongly? Otherwise answer "none".
> 6. Which question needs a collapsed section or link before you decide? Otherwise answer "none".
> 7. Where does any part of the card contradict another, such as a link to a different PR than the one described? Otherwise answer "none".
> Then quote the sentences to fix, or answer "none":
> 8. Each sentence you read twice or could not picture as something a person sees or does. Name the cause: undefined term, number without a comparison, internal mechanism or too many facts. Rewrite it so you can picture it, keeping every fact.
> 9. Sentences that repeat another or can go without changing your answer. Mark each "cut".
> 10. Body sentences you could act without, which could move to a collapsed section. Mark each "move".

For an answer to a handed message, also supply "Your message" and "The step you wrote it on", saved before rewriting, and add:

> 11. Quote each point of your message. Is it answered as intended, answered more narrowly, or unanswered?
> 12. Does the step change the request, decide an open choice or omit the earlier state? List each or answer "none".

Fix every valid issue. Take cuts and moves unless a sentence settles a likely worry. Judge other flags against what this user actually knows.
Major issues include an unpictured sentence, a mistaken answer to questions one to four, an obstacle in five or six,
a contradiction in seven, an incomplete answer in eleven, or a changed request in twelve.
After a major issue or a large fix, give the new card to a fresh reader before putting it.
Stop at "none" or only rejected flags, with at most three rounds.
If it still reads badly, settle the missing fact about what happens for people.
When no independent reader is available for a card that may go up quick, put it as a quick card with the reader check skipped, and arrange the missing check.
A merge or approval card waits for the reader: say in the item's summary that the reader check is missing, arrange it, and put the card once it has run.

## Quick cards

A quick card goes up fast because the user is waiting, or because the sweeper asked for one.
Its body opens "Quickly prepared:" and names each check it skipped, such as "Quickly prepared: the reader check and the tests have not run yet."
Run those checks as soon as they can run, without waiting for the sweeper to ask. Keep the line until every skipped check is done, then remove it.
A merge or approval card always gets the full checks, so it never goes up as a quick card.

### Asked for a quick step

Spend about ten minutes preparing from what you already know: ask, links, recommendation and useful answers.
Check live state for every fact you do state. Name each check you skip in the "Quickly prepared:" line.
Then run those checks as [Quick cards](#quick-cards) describes. A sweeper that finds the line still there asks for full preparation.

### Asked to get ready

Finish agent work, run relevant tests, read checks on the latest commit, and try the change where it runs.
Reread the PR, draft or analysis as the user would. Run the reader check.
Put the step once live state meets [Readiness](#readiness).
When only the user can unblock work, put that question. When no user action remains, explain in the summary and request a sweep.
For a "Quickly prepared" step, run every check that line names, check readiness and remove the line.
