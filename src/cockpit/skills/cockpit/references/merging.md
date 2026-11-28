# Merging duplicates

Open this when two items turn out to be one task.
The parent `SKILL.md` applies too, with its rule of one card per task.

Merge the side item into the main one with `cockpit item merge`.
Merging needs no confirmation:

```bash
cockpit item merge SIDE_KEY --into MAIN_KEY --note "Both are about the same weekly reminder." --as NAME
```

The note, at most 300 characters, tells the user why the two became one.
The merge moves the side item's messages, images, pages, comments, sessions and links to the main item, then archives the side item.
It moves every image and page even past the limit of ten each; the joined item then refuses new ones until removals bring it below ten.
The side item's open cards are withdrawn and listed on the main item. A handed step moves with its agent and stays handed.
The main item's card shows a pink "Merged in" band, naming the side task, the note and its open cards, until the user's next message or press on that task.
After the merge, rewrite the main item's card so it covers both tasks, including what the user said on the side task.
Put one when the main item has none. When the main item holds a handed step, answer on that step instead.
The CLI lists the main item's open steps and the next move.
Merge before writing the main item's next card, or after the user finishes the card they are on.
While the user's timer runs on either item, Cockpit holds the merge: the CLI prints `waits: …` and exits zero.
The merge applies when the user pauses, changes steps or acts. Write the joint card after it applies.
Writes to a merged item are refused with the number of the item it joined; put them there.
A merge has no undo. When the user says a merge was wrong, create a new item for the part that belongs apart, link the joined item, and move that part's ask there.
