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
Primaries, Curves, Secondary, Window, Look, FX, Grain, Detail, Letterbox, Output),
which is also the order ffmpeg applies them, so reading top to bottom tells you what
happened to the picture in what order. Window sits directly under Secondary because
it is the shape half of the same node: it gates the qualifier and nothing else.

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

## Three things added to the engine

All are additive and all are off by default, so every existing preset renders byte
for byte the same as before.

- **Curves**, wired to ffmpeg's `curves` filter with `interp=pchip`. The editor uses
  the same monotone cubic the filter uses, so the line you drag is the line you get.
- **HSL qualifier** (`secondary`), a hue / saturation / luma key with softness on each
  window, correcting hue, saturation, luma and tint. It is baked into a 33-cube and
  cached by hash. That makes it a table lookup at render time rather than a per pixel
  expression, at the cost of being exact only to the resolution of that cube. The
  Matte button shows the key.
- **Power window** (`window`), one ellipse or rectangle that gates the qualifier, so
  a correction lands only where the colour key and the shape agree. Centre, extent,
  rotation and feather are all fractions of the frame, which is why the same window
  means the same shape in a 960 wide preview and in the finished render. The matte is
  baked to an 8-bit grey PNG by a `geq` expression and merged with `maskedmerge`, the
  same technique the radial blur ramp uses. Both mattes take the same route into the
  16-bit merge (`format=gray16le,format=gbrp16le`, a multiply by exactly 257), so
  matte code 255 means the whole correction rather than 99.61% of it. The Matte button
  shows the qualifier multiplied by the window, because that is what the grade
  actually selects.

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

## Agent API

The studio exists so an AI agent can grade footage through it, not only a person
dragging sliders. Every route below is plain HTTP against the same server the
browser talks to: nothing about it is agent specific except that nobody is
looking at a page. `studio/tools/agent_grade.py` is a complete, runnable
example: point it at a clip and a target and it measures, patches and saves a
real grade end to end, printing every step.

### Getting a token

With logins off (the default, `./studio.sh` with no `--auth`), nothing is
required: every route below just works. With logins on (`--auth`, or any
non loopback `--host`), an agent authenticates the same way a browser does,
except with a Bearer token instead of a cookie, because a cookie is meant for
a browser to attach automatically and a script should not be relying on that.

```bash
# one time, from a terminal on the machine running the studio
studio/server.py --create-user my-agent --password-stdin --role user

# sign in to get a session cookie, then trade it for a token
curl -s -c cookies.txt -H 'Content-Type: application/json' \
  -d '{"username": "my-agent", "password": "..."}' \
  http://127.0.0.1:7431/api/auth/login

curl -s -b cookies.txt -H 'Content-Type: application/json' \
  -H 'Sec-Fetch-Site: same-origin' -d '{"label": "my-agent-token"}' \
  http://127.0.0.1:7431/api/auth/token
# -> {"token": "...", "id": 2, "label": "my-agent-token"}
```

The token is shown once and stored only as a hash, so save it now. From here
every request the agent makes carries `Authorization: Bearer <token>` instead
of a cookie, and (contract C2) a Bearer request skips the CSRF check that a
cookie authenticated write has to pass, because that check exists to stop a
browser from being tricked into a request it did not mean to send, and a
script sending its own Authorization header was never at risk of that.

### The `by` field

`POST /api/session` and the config it returns carry a `by` field: a short,
free text label saying who made this change. A human dragging a slider in the
browser sends `"cli"` or leaves it out; an agent should send its own name
(`"agent"` is the default in `agent_grade.py`, but a fleet running several
agents at once should use something more specific, like `"agent:colorbot-3"`).
It costs nothing to set and it is the only way anything watching the session
(a person's open tab, `GET /api/session`, another agent) can tell an outside
patch apart from a local one.

### The routes an agent actually needs

**`GET /api/state`**: defaults, the clip list, the ref list, the preset list,
and (with an account) that the token is even valid, since a bad token 401s
here before anything else does.

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:7431/api/state
```

**`POST /api/session`**: `{"clip": NAME, "time": SECONDS, "config": PATCH,
"by": "agent"}`. `config` is deep merged into the live config unless
`"replace": true` is sent, so an agent only ever has to send the one branch of
the tree it is changing (`{"primaries": {"contrast": 1.1}}`, not the whole
config). This is also what makes the studio a live, remote controllable
picture: a browser tab open on the same clip is long polling
`GET /api/session/wait` and repaints the instant this patch lands, with no
refresh. `GET /api/session` reads the current value back without changing it.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"clip": "A001.MOV", "time": 3.0, "config": {"primaries": {"contrast": 1.1}}, "by": "agent"}' \
  http://127.0.0.1:7431/api/session
```

**`POST /api/stats`**: `{"clip": NAME, "time": SECONDS, "width": 480,
"config": CONFIG}` renders that one frame and returns
`{"key", "stats", "size"}`. This is how an agent measures whether a patch
helped: see "What the numbers mean" above for what each field in `stats`
actually is, it is the identical measurement whether a person or a script
asked for it.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"clip": "A001.MOV", "time": 3.0, "width": 480, "config": {}}' \
  http://127.0.0.1:7431/api/stats
```

**`POST /api/frame`**: the same request shape as `stats`, plus optional
`"mode"` (`graded`, `flat` or `mask`) and `"format": "raw"` to skip JPEG
encoding, returns the rendered picture instead of numbers. An agent that
wants to look at the frame rather than only measure it uses this.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"clip": "A001.MOV", "time": 3.0, "width": 480, "config": {}}' \
  http://127.0.0.1:7431/api/frame -o frame.jpg
```

**`POST /api/match`**: `{"ref": NAME, "clip": NAME, "time": SECONDS,
"config": CONFIG}` fits a look cube toward a reference image (see
Match Reference above) and returns `{"ok", "name", "lut", "warnings",
"stats", "distance", ...}`. `ok: false` means the fit failed its own health
check and should not be applied; on `ok: true` an agent applies it the same
way the browser does, `POST /api/session` with
`{"config": {"look": {"lut": result.name}}}`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"ref": "IMG_2570.PNG", "clip": "A001.MOV", "time": 3.0}' \
  http://127.0.0.1:7431/api/match
```

**`PUT /api/grade`** (contract C3, per clip saves) or **`POST /api/preset`**
(a shared, named grade): `PUT /api/grade` takes `{"clip": NAME, "config":
CONFIG}` and returns `{"key", "updated_at"}`, read back with
`GET /api/grade?clip=NAME` (`{"exists", "key", "config", "updated_at"}`).
`POST /api/preset` takes `{"name": NAME, "config": CONFIG, "comment": TEXT}`
and returns `{"saved", "path", "presets"}`, read back with
`GET /api/preset?name=NAME`. An agent talking to an older build that has not
shipped `PUT /api/grade` yet should fall back to a preset save; either way,
follow the write with the matching plain `GET` so the agent is not just
trusting its own POST or PUT, it is confirming the save actually landed.

```bash
curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"clip": "A001.MOV", "config": {"primaries": {"contrast": 1.1}}}' \
  http://127.0.0.1:7431/api/grade
```

### The proof

`studio/tools/agent_grade.py` runs the whole loop against a real server:
measure the clip's current stats, compute a small patch from five
deterministic rules over the primaries (exposure, contrast, saturation,
temperature, tint), each damped and clamped to that control's own range,
push it live with `by: agent`, measure again, and repeat until every tracked
metric is within tolerance or the total error stalls. It targets either a
named preset (measured on the clip itself), a saved stats file, or a
reference image (fits a look first, then refines primaries toward the
reference's own measured stats). See the honesty entry in the Limits panel
for exactly what this rule set does and does not reach.

```bash
content/.venv/bin/python content/studio/tools/agent_grade.py A001.MOV \
  --preset-target cinematic --save grade
content/.venv/bin/python content/studio/tools/agent_grade.py A001.MOV \
  --ref IMG_2570.PNG --save preset
```

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
