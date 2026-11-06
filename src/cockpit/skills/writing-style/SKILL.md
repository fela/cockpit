---
name: writing-style
description: >-
  Write clearly for humans. Load before drafting substantial text: PRs, reports, results,
  tickets, memos, Cockpit steps and multi-paragraph replies. This includes investigation
  findings, diagnoses, explanations and PR reviews. Use it from the first draft.
---

Apply this skill to the human-facing text in the request. Otherwise apply it to the text you are writing.
A report in chat is still a report. Load these rules before drafting so the first version follows them.

## The model to copy

Give each idea enough room and put the reader's questions first. These examples illustrate the difference:

| Compressed text | An explanation a reader can use |
| --- | --- |
| Fixed labels such as Context and Implementation | The reader's questions as headings, each answered in two to four sentences. |
| "The new forecast has lower error: 12 versus 18." | "Average error measures how many deliveries the forecast misses by. Lower is better. The new forecast misses by 12 deliveries; today's forecast misses by 18." |
| Undefined terms such as "cold routes" | "A new route has no delivery history yet." Define the term before using it. |
| Many numbers in one paragraph | A couple of numbers per point, each with a comparison. Put the rest in a table. |
| A new internal name for familiar work | The reader's existing name, with any necessary distinction explained once. |

Length follows the explanation the reader needs. A longer page with clear answers can be easier than a dense card.
When Cockpit is available, read nearby examples in `cockpit writing list`, including good and difficult examples and the user's notes.
Use `cockpit writing get EXAMPLE_ID` for the whole text. Read an available example transcript when drafting long work.
Copy what good examples do. Supply missing definitions, context and direct answers in difficult examples.

## The shape: answers first, then the situation from scratch

Use this order for short and long explanations. Include each part that has something useful to say.
Name each section's subject again so it stands alone.

1. **Answer the reader's questions.** Restate their question and use their words as its heading.
   Start directly, then add two or three supporting sentences. If nobody asked, explain what this is and what changes for whom.
   End the short answers with your recommendation. A reader who stops here should understand the decision.
2. **Explain the situation from scratch.** State the question the system answers and who uses it.
   For example: "How many deliveries should the dispatcher plan for tomorrow?"
   Explain data through what people see or do: "Drivers record completed deliveries in the app."
   Define terms in a short list, including meaning, range, source and when data is missing.
   Compare current versions in a table when several do the same job. Say how each works and who uses it.
3. **Say what the work does.** Use full sentences about what a person, page or system does differently.
   Separate today's behavior from the change and its resulting effect.
4. **Rank problems and risks.** Start each with a bold claim, then support it with evidence and a comparison.
   For example: "The forecast struggles on new routes." Say how often, for whom, where and when it was measured.
5. **List what follows, in order.** Explain why each action comes then, who acts and what follows it.
   If your recommendation changed, say what evidence changed it.
6. **State the decision.** Put the recommendation first and use the same words as any answer buttons.

## Give each idea the room it needs

Write full sentences and define their terms. Move a long explanation into its own section or linked page.
Keep a one-line answer where the reader needs it. Expand shorthand that requires the session's context.
Use headings in the reader's words, short paragraphs, lists for parallel points and tables for comparisons.
On a page, give short answers their own boxes and the decision its own box.
Put paths, code names, table names, commit hashes and session IDs in reviewer sections or appendices.
Keep the main text about what the work means for people.

## Words

Write for a competent person who has seen none of the session, tool output or plan.
Introduce names before relying on them. Use these rules:

- **Use one name per thing.** Keep the reader's existing name. Name system parts by their job, such as "the forecast".
- **Define terms before use.** Give each definition its own sentence or term-list entry. Replace invented session names with descriptions.
- **Use people as subjects.** Say what a customer, dispatcher, teammate or page does or sees.
  Give mechanisms an explicit actor: "The database reads the newest orders first." Put internal mechanics in reviewer details.
- **Use short sentences.** Aim for about 20 words and one idea each. Prefer active voice and present tense when accurate.
- **Choose plain words.** Say "safe to run again", "creates" and "uses" when those convey the meaning.
  Explain the concrete effect: "This deletes saved results without recording what was removed."
- **State facts directly.** Say "The forecast ignores holidays today." Give the reader the evidence needed to judge it.
- **Restate context when needed.** Repeat a short definition where a reader may have forgotten it.
  Start from the user's picture of the system, then confirm or correct it in those words.

Use full stops and plain connections between ideas. Write without em-dashes.

## Numbers a reader can judge

Define a metric at first use. Say what it measures and which direction is better.
Explain endpoints when useful: "Success rate is the share of completed deliveries; 100% means every delivery finished."
Give a simple baseline beside results, such as today's behavior or a fixed guess.
Say where and when each number was measured. Label illustrative figures as examples.
For example: "In last week's test, the forecast missed by 12 deliveries per day; today's forecast missed by 18."
Use a couple of numbers per point. Put the rest in tables or appendices.
Judge impact by frequency and cost per occurrence. Compare choices on the same baseline and explain mixed changes.
State what a shortcut saves and what evidence it loses. Identify who checked a speed claim.
When a saving is unmeasured, keep the evidence and reduce the work's volume instead.

## Where the work came from, and honesty

Say near the top who asked and where, such as a message, review comment or Cockpit step.
Link tickets and PRs by their work's name, with the clickable identifier after it. Include requesters only when known.
Say who made each decision and what prompted it. Distinguish author choices from agent suggestions they accepted.
Say who wrote the code and who checked it. Attribute decisions only when established.
Name choices worth a second look and explain their trade-off. State any change beyond the request and its consequence.
When work becomes complicated, quote the user's original plan and identify the added rule before explaining options.
State what you checked and what it showed. Label inferences as inferences and connect claims to evidence.
Keep claims verifiable through the diff, source or a stated check. Resolve uncertainty or mark it clearly.
Keep secrets, credentials, personal data and private URLs out of text, quoted code and appendices.

## Code, for the readers who need it

Put code supporting a claim below the main explanation in a reviewer section.
Quote the few relevant lines with `file:line`, enough for the reviewer to assess the claim.
In a Cockpit step, use a collapsed section. In a PR, title it "For the reviewer".
For work built by prompting an AI, include prompts in an appendix, in order.
Tidy their presentation while preserving wording. Append prompts that led to later commits.

## Reviews and investigation findings

Lead with the verdict: merge, hold or the root cause. Rank problems after it.
Each finding stands alone: claim, concrete failure and a cheap check through a query, log or test.
Quote interacting code in data-flow order in the reviewer section.
Keep practical consequences and their likelihood in the main explanation.

## Final pass

Read as someone who has never seen the work. Fix sentences depending on session context.
A self-check is enough for chat replies. For durable text, use two independent editor passes.
This includes PR bodies, docs, tickets, reports and pages.
Give each independent second model the draft and the same brief, separately.
For example, use a read-only `codex exec` or `claude -p` call. Give the second editor only the draft and brief.
Ask for flags with locations. The writer judges and fixes them.

> Read as an outside reader. Flag terms used before definition, numbers without a baseline or case,
> sentences needing session context, and places that could be simpler. Give flags and locations only.

Weigh both editors' flags against the source and fix the ones that hold.
Record unavailable independent checks as incomplete and arrange the missing review before treating the text as checked.
Give any model rewriting text these rules:

> Preserve every true source fact. Keep metrics, filenames and claims grounded in the source.
> Mark ambiguity [unclear]. Reorder, merge, split and rephrase freely. List terms you could not expand.

Check every rewrite against its source before using it.
