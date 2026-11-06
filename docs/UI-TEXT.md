# Words and motion on Cockpit's page

These rules cover labels, hover text, help lines, notices and motion. Step cards
written by agents follow the bundled Cockpit skill. Keep decision context on the
page and use labels and visible state to explain controls.

## The order of choices

For anything the page needs to say, take the first of these that works:

1. **Nothing.** The control's place, icon and label already say it.
2. **The page shows it.** Pressing the control changes what it acts on, visibly and at once, with Undo on the notice. A chip that turns from "planning" to "building" teaches more than a hover text announcing it.
3. **A better label.** One to three words, verb first, in the words the user uses.
4. **A hover text** (rules below).
5. **A help line** on the page (rules below).

## Hover texts

A hover text belongs on:

- an icon-only control: its name, the same as a label would be ("Snooze", "Pin");
- a shortened value: the whole value ("6h 0m" → "6 h 2 min on this item");
- a time or count: the exact time, or what is counted;
- a control whose result the user cannot see from the page: the result, in one line ("Moves it to building").

Write it as:

- one line, at most about 60 characters, plus the key that presses it when there is one;
- the name or result, never a repeat of the visible label, never how Cockpit works inside;
- plain words: no "the sweep", "queue place", "session's turn" unless the label itself uses them.

A value written by an agent or by the user (an item's title, a step's ask, a rule) shows in full up to 100 characters and is cut there; the card or page it opens holds the rest. The tooltip itself shows at most six lines of 120 characters, so a value that runs past that shows its start. `tests/browser/hover_texts.cjs` fails when a hover text that is not a value has more than three lines or a line over 90 characters.

Leave it out when it would repeat the label, describe the obvious ("Open: opens it"), or explain a rule of Cockpit's. Anything the user needs to decide goes on the page, not in a hover text: hover texts vanish and do not exist on a phone.

## Help lines

A help line under a heading or field is one short sentence, at most about 80 characters, with no full stop. Add one only when most visits need it; a first-time explanation belongs in the spec or the docs. An empty list says what to do, not why it is empty.

## Labels and notices

- A control keeps one name everywhere: the button "Snooze" makes the notice "Snoozed", the same verb.
- A notice says what happened in a few words and offers Undo: "Closed: Dense step sentences · Undo".
- Sentence case. No ALL-CAPS labels, no labels above values whose format already says what they are.

## Motion

- Motion answers the user's action and shows what changed: a chip that changes colour, a card that slides out where it went.
- 100 ms for small feedback, 200–300 ms for panels and cards, ease-out on entry, slightly faster exits.
- Animate `transform` and `opacity`. When a size must change, animate the height from its old to its new value; nothing jumps.
- Reserve space for what loads later, so the page holds still while it fills in.
- Under `prefers-reduced-motion`, fades replace movement.
- Pulsing, dots and badges mark real state only.

## Use decides what stays

Cockpit counts which controls the user presses and which hover texts the user stops on (`cockpit ui-usage`). Read it before adding or keeping a control:

- Pressed often: visible, in reach, with a key.
- Pressed rarely but needed: under More, or a key in Search.
- Not pressed in a month: a candidate for removal; ask the user on the item with the numbers.
- A hover text the user stops on often: the control is unclear, so rename or redesign it rather than lengthen the text.

Sources: [NN/g tooltip guidelines](https://www.nngroup.com/articles/tooltip-guidelines/), [NN/g progressive disclosure](https://www.nngroup.com/articles/progressive-disclosure/), [NN/g confirmation dialogs](https://www.nngroup.com/articles/confirmation-dialog/), [NN/g animation duration](https://www.nngroup.com/articles/animation-duration/), [GOV.UK hint text](https://design-system.service.gov.uk/components/text-input/), [web.dev layout shift](https://web.dev/articles/cls), [Refactoring UI on labels](https://www.refactoringui.com/previews/labels-are-a-last-resort), [signs of a vibe-coded UI](https://www.thefountaininstitute.com/blog/signs-vibe-coded-ui).
