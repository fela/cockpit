# Pages and images

Open this when a step carries screenshots, charts or an HTML page.
The parent `SKILL.md` and [Writing a card](writing-a-card.md) apply too.
`cockpit image --help` and `cockpit page --help` list the commands.

## Screenshots and images

Show more screenshots, not fewer. Take them where the work runs, or on a preview with realistic data.
When a live page needs a sign-in you lack, say so and show the preview instead.
Attach screenshots and graphs with `cockpit image add`. Place each relied-on image where the reader needs it:
`![The corrected sign-up form](image:ID)`. Other images remain under the item's collapsed image list.
A screenshot on another page or PR is an extra link; put the deciding screenshot in the step itself.
Cockpit refuses a new image once an item holds ten; a merge can carry more (see [Merging duplicates](merging.md)). Only the agent that added an image removes it, under the same `--as` name.

## Pages

Share reports, plans and interactive charts as one self-contained HTML file through `cockpit page add`.
Before writing a page, load the `frontend-design` skill when it is installed, with any UI skill the user's instructions name.
Give the page Cockpit's look: its colours, type and spacing from `static/style.css` and `static/look.css` in the Cockpit package.
Use inline scripts and styles, or supported assets from cdnjs, jsdelivr or unpkg.
Cockpit opens pages in a sandbox with the installation's access controls.
Link the page from the step on a line of its own: `[The plan and its decisions](page:ID)`, or the returned page URL.
Cockpit draws that line as a button with the page's name and "Open page". The button opens the page over Cockpit.
The step's text says in a sentence what the page holds and what the user does there.
Page comments return to the step that opened the page. `cockpit page update` preserves its link; resend the title.
Use the same `--as` name that added it. Cockpit refuses a new page once an item holds ten; a merge can carry more.
When more are needed, create a linked item for the additional pages.

Only the user shares a page, from its Share menu on the Pages tab. A page starts shared with nobody.
The user can share it with anyone who has its link, or with specific people who sign in with Google.
It then opens at a separate address. Sessions see a page's `access` (`me`, `people` or `anyone`), never its link or list.
Cockpit refuses an agent's update to a shared page. To change one, ask the user, or run
`cockpit page unpublish PAGE_ID`, update it, and ask them to share it again. Its link stays the same.
A shared page loads nothing from other websites, so put its scripts, styles and fonts inside the file.
It can't save with `localStorage`, open another window or start a download. It can ask with `confirm()`.

For page choices, use named form controls with values and descriptive labels.
Custom controls call `cockpit.choose(name, value, label)`. Choices belong to the step that opened the page.
They arrive with the user's next answer. Read them any time with `cockpit page choices PAGE_ID`.
Reopened controls show saved choices. Scripts read `cockpit.choices` after the `cockpitchoices` event.

## Figures in the step

Use a chart, table or screenshot when it explains faster than words.
`![Weekly sign-ups](page:ID)` embeds a page; `![The new form](image:ID)` embeds an image.
The caption says what it shows.
Embed a page only when it is small and the user takes it in at a glance: a chart, a table, a short interactive piece or a one-tap try-out.
It fits its frame without scrolling, at 700 pixels wide and on a phone.
A page with decisions, several sections, a report or any workflow gets a link on a line of its own, as [Pages](#pages) describes.
Cockpit shows an embedded page taller than 900 pixels as that same button, with the caption as its name.
Under each embedded page, Cockpit adds "Open full size", which shows the page over Cockpit and fills a phone's screen.
A page for trying things, such as versions of an animation, works in one tap at the card's width:
each option has a large button that plays it at full size, with `document.documentElement.requestFullscreen()`
on that tap. It fills the frame when `requestFullscreen` is missing, as on iPhone, or its promise rejects. Choose good defaults so nothing needs setting first.
The step's text says in one sentence how to try it, and which options to compare first.
Give it a background and `color-scheme: light dark`.
A step references only its own item's pages and images. Cockpit refuses references to other items' assets.
