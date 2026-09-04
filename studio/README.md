# Fixxr Studio

A local GUI for the grading engine in `grade/cinegrade.py`. Same pipeline, same
presets, same LUTs: the difference is that you drag a control and watch the frame
change instead of editing JSON and re-running the CLI.

The server imports `cinegrade.py` as a module and calls its graph builders. It does
not have its own copy of the filter chain, so anything you save here renders
identically from the command line, and any engine change shows up in the GUI with no
porting work.

## Run it

```bash
./content/studio.sh              # http://127.0.0.1:7431
./content/studio.sh --port 8123  # if 7431 is taken
```

Then open http://127.0.0.1:7431 in a browser. Stop it with ctrl-C.

It binds 127.0.0.1 only and has no login, because it is meant to be reachable from
this machine and nothing else. There is no build step, no npm, no CDN: the page is
four hand written files served off disk and it works with the network cable out.

Requirements are the ones the engine already has: the repo venv at
`content/.venv`, plus `ffmpeg` and `ffprobe` on PATH. Clips are read from
`content/footage/`, reference images from `content/refs/`, and renders are written to
`grade/out/`.

## Where things are

```
content/
  studio.sh                 launcher, execs the venv python
  studio/
    server.py               the whole backend: routes, ffmpeg calls, stats, jobs
    static/
      index.html            layout, no logic
      style.css             flat dark theme, one accent colour
      controls.js           drag-scrub numbers, sliders, colour wheels, curve editor
      schema.js             one entry per parameter in the engine defaults
      panels.js             builds the right hand panels from that schema
      app.js               state, requests, viewer, timeline, presets, keyboard
    cache/                  derived frames and JPEGs, safe to delete (gitignored)
  grade/
    presets/*.json          shared with the CLI, this is where Save writes
    luts/looks/*.cube       the look list, this is where Import writes
    luts/secondary/         qualifier cubes baked from the HSL panel (gitignored)
    out/                    finished renders
```

The layout follows the pipeline. Panels on the right are in node order (Convert,
Primaries, Curves, Secondary, Look, FX, Grain, Detail, Letterbox, Output), which is
also the order ffmpeg applies them, so reading top to bottom tells you what happened
to the picture in what order.

## What the numbers mean

The stats strip under the viewer is the same measurement the grading notes were
written against:

- luma percentiles at 5, 25, 50, 75 and 95, as 0 to 1
- mean saturation, computed as `(max - min) / max` per pixel
- percent of pixels in each hue family (warm, green, cool, magenta), counting only
  pixels above 10 percent saturation so that grey does not vote
- percent clipped black (code 2 or under) and clipped white (code 253 or over)

Those are measured on the preview frame, at 640 wide and 8-bit, from the same decoded
array the picture and the scopes come from. Picture and numbers therefore cannot
disagree with each other, but they can both differ slightly from the full resolution
10-bit render. The Limits panel in the app spells out where.

## API

Every route is under `/api`. Configs are posted whole and merged over the engine
defaults, so a partial config is legal everywhere.

| Route | Method | What it does |
| --- | --- | --- |
| `state` | GET | defaults, clips, presets, looks, refs, renders, stat definitions |
| `clips` | GET | probe info for everything in `footage/` |
| `frame` | POST | render one preview frame, returns JPEG |
| `stats` | POST | the numbers above for one frame |
| `scope` | POST | histogram, waveform, parade or vectorscope as a JPEG |
| `thumb` | GET | timeline thumbnail, ungraded |
| `ref` | GET | a reference image from `content/refs/` |
| `presets` | GET | list |
| `preset` | GET / POST / DELETE | load, save (defaults stripped), delete |
| `looks` | GET | list, with generated versus imported flagged |
| `look` | GET / POST / DELETE | export a .cube, import one, delete one |
| `rebuild` | POST | re-run `make_looks.py` or `make_cst.py` as a background job |
| `render` | POST | start a background ffmpeg render into `grade/out/` |
| `jobs` | GET | job list with parsed ffmpeg progress |
| `job/cancel` | POST | SIGINT the ffmpeg process and delete the partial file |
| `renders` | GET | finished files in `grade/out/` |
| `cache/clear` | POST | drop the frame cache |
| `browse` | GET | list one directory anywhere on the machine, videos only |
| `open` | POST | register a video from outside `footage/` and return its clip entry |
| `match` | POST | fit a look cube toward a reference image, see Match Reference below |
| `source` | POST | the decoded, downscaled source frame as raw rgb48le |
| `lut` | POST | a technical, look or secondary cube as float32, for the GPU preview |
| `session` | GET / POST | read or change the live config of the open page |
| `session/wait` | GET | long poll, returns as soon as the live config moves |
| `parity/report` | GET / POST | the GPU versus ffmpeg parity numbers |

Preview requests carry `X-Studio-Client` and `X-Studio-Gen` headers. The server drops
a request whose generation is already stale, so dragging a slider does not queue up a
line of doomed ffmpeg runs behind the one you actually want.

## How a preview frame is made

One expensive step, then everything cheap hangs off it.

1. Scale the source down before the graph runs, and scale the pixel-denominated
   parameters (halation and bloom sigma, radial blur sigma, RGB split, soften, grain
   size) by the same factor, so the effects stay the same relative size. Grading the
   960 wide frame instead of the 3840 wide one is what makes this usable: about 0.54
   seconds instead of about 1.95.
2. Run the real `cinegrade` graph on it and take rawvideo rgb24 out of ffmpeg into
   numpy. That array is cached on disk under a hash of clip, time, width and config.
3. The JPEG you see, the scopes and the statistics are all derived from that one
   array. A frame you have already looked at comes back in about 10ms.

## Two things added to the engine

Both are additive and both are off by default, so every existing preset renders byte
for byte the same as before.

- **Curves**, wired to ffmpeg's `curves` filter with `interp=pchip`. The editor uses
  the same monotone cubic the filter uses, so the line you drag is the line you get.
- **HSL qualifier** (`secondary`), a hue / saturation / luma key with softness on each
  window, correcting hue, saturation, luma and tint. It is baked into a 33-cube and
  cached by hash. That makes it a table lookup at render time rather than a per pixel
  expression, at the cost of being exact only to the resolution of that cube. The
  Matte button shows the key.

While wiring the primaries panel a real bug turned up in the engine: `lift` and
`gain` were implemented with `colorlevels`, whose output points are capped at 1.0, so
any gain above 1 (the normal direction for brightening) failed the render outright.
They are now `lut` expressions doing the same arithmetic without the cap, and they
take a scalar or a per channel triple. No shipped preset set either value, and all
nine baseline stills stayed byte identical across the change.

## Grading footage that is not Apple Log

The engine was built for Apple Log and used to assume it silently. Loading an
ordinary Rec.709 mp4 did not error, it applied a log to display conversion to a
picture that never had a log curve: measured on a real delivery file, median luma
fell from 0.332 to 0.238 and mean saturation rose from 0.33 to 0.59, giving neon
colour and blown skin.

Set `convert.working_space` to `rec709` for that footage. It skips the log stage,
CST IN and CST OUT and grades in place, while primaries, curves, the secondary,
the look LUT, FX, grain, detail and letterbox all still run. Exposure, temperature
and tint keep working and keep meaning stops; on this path a stop is a code
multiply rather than a log offset, because there is no log curve to offset.

The two are not interchangeable and picking the wrong one is now refused with a
message naming the right mode, rather than rendered wrong. The test is the clip's
own matrix tag: `bt2020nc` is camera log, `bt709` is already display referred.
An untagged file is left alone rather than guessed at.

This is also the FX only round trip described in `grade/FREE-RESOLVE.md`: grade
somewhere else, export Rec.709, bring it back here for halation, bloom, grain and
a vignette without touching the colour.

## Driving the open page from outside

The config used to live only in the browser tab, so nothing else could see it.
The server now keeps a copy, which makes this work:

```bash
./cinegrade session get
./cinegrade session patch '{"primaries": {"temperature": 0.25}}'
```

The patch is deep merged, so send only the knob you care about. The open page
picks the change up over a long poll without a refresh. It talks to a server that
is already bound to 127.0.0.1 and adds no listener of its own.

## Match Reference

`POST /api/match` fits a look cube that moves the current frame's colour toward a
reference image, and drops it in the look slot. It is not extracting a LUT from
the reference, because a JPEG does not contain one: it fits a transform between
two colour distributions and reports how far it got. A real call moved a frame
71.1% closer to its reference in 1.91 seconds. The `warnings` it returns are the
useful part and are shown verbatim; `ok: false` means do not apply the result.
The full contract is in `grade/tools/MATCH-REF-INTEGRATION.md`.

## Limits

The Limits button in the app splits this into three answers that used to be run
together: what is genuinely not possible here (editing, audio, tracking, motion,
compositing), what is possible but not built yet (keyframes via `sendcmd`, drawn
power windows via `geq` and `maskedmerge`, several secondaries, HDR, node
reordering, and real time playback, which turns out to be a design consequence
rather than a hard limit since sequential decoding runs at 29.6 fps against 4 fps
for the current seek per frame), and what is still broken or approximate. Each
claim in there was measured on this build. Notably, hardware accelerated final
render is possible but only worth about 16%, because the filters are roughly 80%
of the time and VideoToolbox does not accelerate filters at all.
