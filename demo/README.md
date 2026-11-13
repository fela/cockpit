# Cockpit demo

The demo is one HTML file that anyone can open to try Cockpit. It shows a week of invented work for an invented person
called Sam. Visitors can answer steps, hand work to agents, run timers, pick a design and open every view. No model
runs and no server answers. Everything a visitor does stays in their browser tab, and a reload or **Start over**
returns to the first state.

The demo lets anyone try Cockpit with invented data and invented next steps, with no model and no backend running.

## Build it

```sh
.venv/bin/python demo/build.py
```

The build writes `demo/dist/cockpit-demo.html`, about 4 MB. `dist/` is ignored by git, so rebuild after changing the
app. Two options exist:

- `--out PATH` writes the file somewhere else.
- `--state PATH` also writes the seeded state and the recorded replies as JSON beside `PATH`, for reading.

The build does not touch your own Cockpit. It points `HOME` at a new temporary folder before it imports the app, so the
database, settings and spec it reads are all invented. It deletes the folder when it finishes.

## Check it

```sh
bun demo/check.cjs            # under the sandbox policy Cockpit gives its pages
bun demo/check.cjs --plain    # with no policy, as a plain static host serves it
```

The check opens the built file in headless Chromium and clicks through the main features on a desktop and a phone
screen. It fails if the page logs an error, requests anything outside the file, or calls the backend in a way the demo
cannot answer. Only Chromium is tested, at desktop and phone sizes; Safari, Firefox and real phones are not. It needs
`playwright-core` and a Chromium binary. Set `PLAYWRIGHT_CORE` and `CHROMIUM` to their paths
when they are not installed where Node and Playwright look by default.

## Publish it

After building the demo, copy `demo/dist/cockpit-demo.html` to `index.html` on your static host. Copy `_headers`
beside it when the host supports that file; it supplies the page policy and asks search engines to leave the demo
unlisted. Run `bun demo/check.cjs --public` to check the page under that policy before publishing.

The demo contains invented work. Rebuild and check it whenever the Cockpit source changes.

### On every push, with GitHub Actions

`.github/workflows/demo.yml` does this for the demo at `https://try-cockpit.pages.dev`. On each push to `main` it
builds the page, deploys it to the Cloudflare Pages project `try-cockpit` with wrangler, and fails unless the address
then serves that build. Pull requests do not run it, and forks skip it.

It reads two repository secrets, set under Settings, Secrets and variables, Actions:

- `CLOUDFLARE_API_TOKEN`: a Cloudflare API token with one permission: Account, Cloudflare Pages, Edit.
- `CLOUDFLARE_ACCOUNT_ID`: the Cloudflare account that holds the Pages project.

To deploy your own copy this way, make a Pages project, put its name and address in the workflow, remove the
workflow's fork condition and set the two secrets in your repository.

## How it works

The demo runs Cockpit's real pages, scripts and styles, so it shows the current app rather than a copy of it.

1. **Invented work.** `seed.py` writes Sam's projects, items, steps, sessions and transcripts into a temporary database.
   Every name, link and address is made up. Links point at `example.com`, `example.test` or `git.example.com`, and the
   home folder is `/home/sam`.
2. **Recorded replies.** `build.py` runs the app in the same process, on the clock of the build, and asks it every question
   the pages ask, such as `/api/state` or a transcript. It also plays each action once, such as an answer or a snooze,
   and records how the app's reply changes. Cockpit's own code decides what the demo shows.
3. **A backend in the browser.** The file holds those replies and two small scripts. `demo-shell.js` runs in the outer
   page and plays the server: it keeps the state, applies each action, and makes the invented agents finish their work
   after a few seconds. `demo-child.js` runs inside each Cockpit page and turns its network calls, links, forms and
   storage into messages to the shell.
4. **One page at a time.** Each Cockpit page loads into a sandboxed frame from inline HTML. Moving to another view, or
   pressing Start over, replaces that frame with a new one.

The top band says the work is invented and offers Start over. A link to an invented address shows a short note
instead of opening, and so do signing in and signing out.

## Where it differs from the real app

These are known and accepted. None of them leaks real data.

- **Invented agents work for 7 seconds.** After that the step comes back finished or with its next ask.
- **A sent answer starts sooner if the page misses its moment.** As in the real app, Take back is on offer while its
  note shows, about 8 seconds, and the answer goes when the note does. The real server sends it after 60 seconds if
  the page never says so; the demo waits 20.
- **Steps closed before the demo starts cannot be reopened.** Steps the visitor closes can be.
- **An agent's progress resets that item's messages and closed rows.** When an invented agent moves an item on, the
  demo shows the messages and closed steps the build recorded for that point, in place of the visitor's.
- **A comment left unsent in a Pages layer is lost** when the layer closes.
- **Fork is refused** with a note, because it would start a new session.
- **A link inside a nested page opens in the main view**, not inside the nested page. The one exception is the
  briefing's list of earlier days: as in Cockpit, another day replaces the one in the briefing's layer.
- **The browser's Back does not close a layer.** In Cockpit it does. In the demo the layer keeps its history inside
  the page, so the visitor closes it with its X or Escape.
- **Start over logs harmless messages.** Under Cockpit's strict page policy, Chromium reports blocked logo images
  while it removes the old frame. The new frame draws every logo; the check counts these separately.

## Files

| File | Job |
| --- | --- |
| `build.py` | Seeds the work, records the app's replies and writes the HTML file. |
| `seed.py` | The invented work. |
| `demo-shell.js` | The backend in the browser: state, actions, invented agents, Start over. |
| `demo-child.js` | Runs inside each page and passes its calls, links and forms to the shell. |
| `check.cjs` | The headless browser check. |
