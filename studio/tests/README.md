# Fixxr Studio UI test harness

A browser test harness for the studio front end (`studio/static/*.js`), which had
zero automated tests before this. It drives the real static files in a real
(headless) Chrome through puppeteer-core, against a real `studio/server.py`
instance started on a random local port, and asserts on the real DOM. Nothing
here mocks the front end, the server, or the browser's input pipeline.

## Run it

```
cd content/studio/tests
npm install   # once
npm test
```

Or directly: `node run.mjs` from this directory.

The runner:

1. Picks a free TCP port in `20000..60000`.
2. Spawns `content/.venv/bin/python studio/server.py --port <port>` with `cwd`
   set to `content`, and waits until `GET /api/state` returns 200.
3. Launches the installed Google Chrome (`/Applications/Google Chrome.app/...`)
   headless via puppeteer-core (no Chromium download: `puppeteer-core` has no
   bundled browser, so this only ever drives the browser that is already
   installed on the machine).
4. Loads the page at a 1440x900 viewport, runs every spec in `specs/` in
   order, and prints a PASS / FAIL / SKIP table with one line of evidence
   each.
5. Kills Chrome and the server, even if a spec throws, and exits non-zero if
   any spec reported FAIL. SKIP does not fail the run.

Only dependency: `puppeteer-core` (Apache-2.0 licence). `node_modules` is
gitignored.

## What each spec proves

- **input-probe**: settles whether Chrome, driven only through puppeteer's
  own `page.mouse` API, actually delivers `mousedown`, `mouseup`, `wheel`
  and `pointerdown` to page JavaScript in this headless setup (a window
  level, capture phase listener installed before the page's own scripts
  run, so nothing downstream can hide an event from it by calling
  `stopPropagation`). Every later spec that needs one of these reads the
  result from this spec instead of assuming: if an event type never
  arrives, that spec reports SKIP with the missing type named, never a
  false PASS.
- **boot**: zero `pageerror` events and zero `console.error` messages
  between the initial navigation and the app's first render, and no
  `RangeError` anywhere in that window even if it surfaced at a different
  console level. This is the "catch the next silent crash on page load"
  spec.
- **panels**: every section in `schema.js` (`window.SCHEMA`) is rendered as
  its own `<section class="stage">` inside `#params`, proving the panel
  builder never silently drops or duplicates a stage.
- **sidebar-toggles**: a real click on `#sidebarLeftToggle` and
  `#sidebarRightToggle` flips `data-sidebar-left` / `data-sidebar-right` on
  `<html>`, and at a 1280x720 viewport (the narrowest width this tool
  targets) `document.elementFromPoint` at the centre of
  `#sidebarRightToggle` still resolves to that button, not something
  covering it. Each toggle is clicked twice so the sidebars are left as
  they were found.
- **sidebar-drag**: a real `mousedown`, `mousemove`, `mouseup` sequence on
  the right sidebar's own resize handle (`.sidebar-resize[data-resize-side
  ="right"]`) changes `#sidebarRight`'s rendered width by at least 100px,
  and the new width survives a page reload (`sidebars.js` paints it from
  `localStorage` before first paint). One of the two behaviours the plan
  called out as unverified on this machine; answered here with a real
  drag, not a value assignment.
- **widget-scroll**: a real `wheel` event over `#dock` scrolls
  `.gridcol > .grid-stack` (the single vertical scrollport for the whole
  middle column, per the comment on that CSS rule in `style.css`), and
  `#dock` itself never intercepts the gesture with a scroll of its own
  (`scrollHeight === clientHeight`). The other behaviour the plan called
  out as unverified; answered here with a real `page.mouse.wheel`.
- **scopes-row**: setting `#dockbody.scrollLeft` moves `#stats` and
  `#scopes` (its two children) by the same delta, proving the row scrolls
  as one strip rather than clipping or desyncing either half. A direct DOM
  property write, not a driven gesture, so it does not depend on
  input-probe.
- **gpu-preview**: after the first clip (the first entry from
  `/api/clips`) renders, `#gpuCanvas` is visible (`#stage.gpu-live`, per
  app.js's `setStageLayer`) and its pixels are not all identical, checked
  by copying a sample onto a plain 2D canvas and reading it back with
  `getImageData`. SKIPs if there are no clips to render.
- **undo**: a real click changes the live config and `#undoBtn` reverts
  it. `#params` has no native `<input type=range>` or `<input
  type=number>`: every slider and number field in `controls.js` is a
  custom drag/contenteditable div with no `value` property and no `input`
  or `change` event wired to it, so this spec drives the one real
  `<input type=checkbox>` in the panel (the curves stage's "enabled"
  toggle) instead of dispatching events a slider never listens for. The
  live config is read through the JSON panel (`#jsonBtn` fills
  `#jsonText` with `JSON.stringify(cfg())`), the cleanest read available
  without reaching into a private closure.
- **theme**: a real click on `#themeToggle` flips `data-theme` on `<html>`,
  clicked twice so the run leaves the theme as it found it.
- **reload-clean**: reloads the page and repeats the boot spec's check
  against the fresh navigation, fenced independently of the initial load
  and of every other spec's console output in between.

## Known limitation

Specs run against one shared page and one shared browser session in a
fixed order (matching the numbered file names), not in isolation: several
specs restore the state they changed (sidebar collapse, theme, the curves
checkbox via undo) so later specs see the page as they found it, but a
spec that fails partway through its own state changes could still leave
the page in a state a later spec was not written to expect. Two full runs
are the check for that in practice (see the report this harness's own
build produced), not a per-spec isolation guarantee.
