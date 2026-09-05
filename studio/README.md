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

The default is still 127.0.0.1 with no login: nothing changed for the case this was
built for, one person on their own machine. What is new is that the same server can
also run with accounts and a login page, which is what makes putting it on a
network something other than a hazard. Read "Logins and accounts" and "Running it
on the network" below before binding it anywhere but 127.0.0.1. There is no build
step, no npm, no CDN: the page is hand written files served off disk and it works
with the network cable out.

Requirements are the ones the engine already has: the repo venv at
`content/.venv`, plus `ffmpeg` and `ffprobe` on PATH. Clips are read from
`content/footage/`, reference images from `content/refs/`, and renders are written to
`grade/out/`.

## Logins and accounts

Three ways in. Normally you only ever touch the first one.

- **Off (the default).** `./content/studio.sh` with no flags: no login page, every
  request is treated as one local user (id 0, role admin). This is what running it
  on your own Mac has always meant, and still means.
- **`--auth`.** Turns the login page on even though the bind is still 127.0.0.1.
  Useful for testing the login flow itself, or on a laptop more than one person
  uses.
- **`STUDIO_AUTH=1`** (also `true`, `yes` or `on`, checked case-insensitively) is
  the same switch as `--auth`, set as an environment variable instead of a flag,
  for a process manager or a launchd plist that would rather set an env var than
  edit an argument list. Verified:
  `STUDIO_AUTH=1 content/.venv/bin/python content/studio/server.py --port <port>`
  prints `logins  on, N account(s)` on startup exactly like `--auth` does.
- **Any non loopback `--host`.** Binding `0.0.0.0`, a real IP, or anything else
  Python's `ipaddress` does not consider loopback turns auth on whether `--auth`
  was passed or not, and there is no flag that turns it back off: a network bind
  with no login is not a mode this tool offers. See "Running it on the network"
  below for what else that bind refuses.

There is no signup page and no `/api/auth/register`. By design, the only way to
create an account is from a terminal on the machine running the studio:

```bash
content/.venv/bin/python content/studio/server.py --create-user NAME --role admin
#   password:
#   again:

# or, for a script: never put the password on the command line (argv is
# readable by any other process on the box, via `ps`), pipe it in instead.
echo "the-password" | content/.venv/bin/python content/studio/server.py \
  --create-user NAME --role user --password-stdin

content/.venv/bin/python content/studio/server.py --list-users
content/.venv/bin/python content/studio/server.py --delete-user NAME
```

`--create-user`, `--list-users` and `--delete-user` all run before anything binds a
port and then exit, so creating an account never collides with a studio already
listening, and does not need one running at all. `--role` is `admin` or `user`
(default `user`); a password has to be at least 8 characters. Verified against a
throwaway account: plain `--create-user` prompts twice with `getpass` (no echo,
and it refuses if the two do not match) while `--password-stdin` reads one line
from stdin instead; a password under 8 characters was refused with "the password
must be at least 8 characters"; `--list-users` printed one `id  role  name` line
per account and "no accounts yet" with none; `--delete-user` on a name that exists
printed `deleted NAME`, and on a name that does not exist printed `no account
named NAME` and exited non-zero.

Two roles. `admin` can rebuild the technical LUTs (`POST /api/rebuild`) and clear
the shared frame cache (`POST /api/cache/clear`); everything else (grading, per
clip grades, presets, upload, browsing your own footage) needs only to be signed
in, at either role. That is the whole gate, and it exists because those two
actions touch state every other session on the same server reads, not because
grading itself is a privileged act.

A browser session (the cookie `POST /api/auth/login` sets) is good for 30 days of
use, and using it resets that clock. An agent token (`POST /api/auth/token`) never
expires on its own, on purpose: an agent running for days should not have its
credential die mid run. The only way to revoke a token is `DELETE /api/auth/token`.
Both are stored only as a SHA-256 digest, so reading the accounts database back
does not hand out a live credential.

The agent token flow, in full, with the exact routes: sign in once with
`POST /api/auth/login` to get a session cookie, trade that cookie for a token with
`POST /api/auth/token` (a cookie authenticated write needs `Sec-Fetch-Site:
same-origin` or a matching `Origin`, the CSRF rule below), then send
`Authorization: Bearer <token>` on every request after that instead of the cookie.
A bad or unknown Bearer token 401s immediately; it never falls back to whatever a
browser's own cookie happens to say. `studio/tools/agent_grade.py --token TOKEN`
is a script using exactly this: see "Agent API" below for the worked example and
what a token can actually do.

## Running it on the network

The built in server speaks plain HTTP. It has no TLS of its own and will not grow
any: that is a job a reverse proxy already does correctly. `--behind-https-proxy`
does not add TLS, it only tells the server that something else already did, so it
can mark the session cookie `Secure` (a `Secure` cookie is never sent back over
plain HTTP, so setting it without a real TLS front end locks every browser out
with a login that appears to work and then bounces straight back). Verified with
curl against a real server:

```bash
content/.venv/bin/python content/studio/server.py --port <port> --auth --behind-https-proxy
curl -s -i -c cookies.txt -H 'Content-Type: application/json' \
  -d '{"username": "NAME", "password": "..."}' \
  http://127.0.0.1:<port>/api/auth/login | grep -i set-cookie
# Set-Cookie: studio_session=...; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000; Secure
```

The identical login, against the identical server started without
`--behind-https-proxy`, comes back with no `Secure` on the cookie. (`_secure_cookie`
in `studio/server.py` also accepts an `X-Forwarded-Proto: https` header from the
proxy as an alternative to the flag.)

### Refusing to start unconfigured

A non loopback `--host` forces logins on, and then, if no account exists yet,
refuses to start at all rather than open a login page nobody can pass:

```bash
content/.venv/bin/python content/studio/server.py --host 0.0.0.0 --port <port>
# refusing to listen on 0.0.0.0: logins are on but no account exists, so the first
# person to reach this port would meet a login page that nobody can get past.
# Create one first:
#   studio/server.py --create-user NAME --role admin
```

Verified: that message and a non-zero exit is what actually happens, no port gets
bound, and creating one account first is all it takes for the same command to
start normally.

### Which bind to use

| Who reaches it | Command |
| --- | --- |
| Only me, on this machine | `./content/studio.sh` (the default, no flags) |
| My team, on the LAN | studio on `127.0.0.1` with `--auth --behind-https-proxy`, one account per teammate, and a TLS reverse proxy on this machine forwarding to it |
| The open internet | the same as the LAN case, on a real domain with a real certificate; treat every account as one more thing that can be phished, there is no MFA here |

Binding `--host` straight to a real interface, with no proxy in front, is never the
right answer: it is the plain HTTP problem above with nothing between the wire and
the login form.

### Two complete minimal proxy examples

Caddy, which gets and renews the certificate for you:

```
studio.example.com {
    reverse_proxy 127.0.0.1:7431
}
```

nginx, which needs a certificate from somewhere else (certbot or otherwise):

```
server {
    listen 443 ssl;
    server_name studio.example.com;
    ssl_certificate     /etc/letsencrypt/live/studio.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/studio.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:7431;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

Start the studio with `--behind-https-proxy` either way (or lean on the
`X-Forwarded-Proto: https` header the nginx example sets) so the session cookie
gets `Secure`.

### The loopback-through-proxy trap

A route can be gated "answers only a caller on this machine": today that is
`reveal`, which opens a Finder window on whichever machine is running the server,
and a Finder window over a network is somebody else's desktop. That gate works by
checking whether the request's source address is loopback. Put a reverse proxy on
the SAME machine in front of a loopback bind, though, and every request the proxy
forwards, including one that started on the other side of the internet, arrives at
the studio with a loopback source address, because the proxy is what actually
opened that socket, and the proxy is on this machine. The address alone can no
longer tell the server who is local.

This was a real hole: `reveal` trusted the raw address, and `--behind-https-proxy`
did nothing to change that. Fixed in this arc: `AUTH.trusted_loopback` in
`studio/auth.py` is `is_loopback` with one more condition, `and not
behind_https_proxy()`, and `reveal` in `studio/server.py` now calls it instead of
the raw check. When `--behind-https-proxy` is set, every loopback-gated route
refuses every caller, proxy or not, because the server genuinely cannot tell the
difference any more. Verified with curl: the exact same request, from the exact
same real loopback caller, that `reveal` answered `200` with `--auth` alone came
back `403 Forbidden` with `--behind-https-proxy` added and nothing else changed.

## Where things are

```
content/
  studio.sh                 launcher, execs the venv python
  studio/
    server.py               the whole backend: routes, ffmpeg calls, stats, jobs
    auth.py                 accounts, sessions, agent tokens (contract C2)
    db.py                   the one SQLite file auth.py owns the schema for
    grades.py               per clip, per account grades (contract C3)
    data/                   studio.db, users/<id>/{footage,presets}, gitignored
    static/
      index.html            layout, no logic
      style.css             flat dark theme, one accent colour
      controls.js           drag-scrub numbers, sliders, colour wheels, curve editor
      schema.js             one entry per parameter in the engine defaults
      panels.js             builds the right hand panels from that schema
      app.js               state, requests, viewer, timeline, presets, keyboard
      login.html, login.css the login page, served instead of index.html with
                             no valid session
      auth.js               wraps window.fetch, sends a signed out browser to
                             the login page on any 401 from /api/
      grades.js             client half of per clip grades: autosave, the
                             saved indicator, the copy-grade picker
      window-editor.js      the power window drawn and dragged on the picture
      live.js               GPU still preview, the live loop, proxy playback
    tools/
      agent_grade.py         the Agent API proof: measure, patch, save, on a loop
    tests/                  puppeteer-core UI harness against real Chrome, npm test
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
| `upload` | POST | stream a video file in, see Upload below |
| `grade` | GET / PUT / DELETE | this account's saved grade for one clip, see Per clip grades below |
| `grades` | GET | every clip this account has a saved grade for |
| `grade/copy` | POST | copy one clip's saved grade onto another |
| `auth/login`, `auth/logout`, `auth/me`, `auth/token`, `auth/tokens` | see below | see Logins and accounts below |
| `reveal` | POST | open a Finder window on the machine running the server; answers only that same machine, see Running it on the network below |
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

## Per clip grades

Every clip has its own grade per account (contract C3, `studio/grades.py`).
Switching clips no longer carries the previous clip's look across silently, and
two accounts working from the same `footage/` folder never see each other's
changes.

A clip's identity is a content hash, not its name or path: the first 32 hex
characters of `sha256(size || first 1 MiB || last 1 MiB)`. Renaming a file,
moving it into another folder, or opening it from a different mount all land on
the same grade, because none of that touches the bytes hashed. What it does not
survive is a re-encode: a transcoded copy is a different file and starts with no
grade, which is recorded as a limit rather than hidden.

The editor autosaves: 600 ms after the last committed change (a slider release or
a checkbox, not every pixel of a drag), the whole config is written with a `PUT`.
The dot next to the clip list (`#gradeSaveState`) says which of four things is
true right now: `unsaved` (the debounce timer is running), `saving` (the write is
in flight), `saved` (the server has it), or `not saved` (the last write failed, so
what is on screen exists only in this tab). "Copy grade here" replaces the current
clip's grade with another graded clip's, picked from a dropdown of every clip you
have a saved grade for.

Presets are two separate places on purpose. `grade/presets/*.json` is the shipped
library, checked into the repository and shared by every account: it is where
`cinegrade.py`'s own CLI presets live, and a save there would be a shared edit to
a tracked file. Your own saved presets go to `studio/data/users/<id>/presets/`
instead (id `0` when logins are off), gitignored the same way the accounts
database is.

Routes, one curl example each, run and read back on a real test server:

**`GET /api/grade?clip=NAME`** returns `{exists, key, config, updated_at}`.
Verified on a clip with no saved grade: `{"exists": false, "key":
"05fe6fe1e893172ddf254f196a9a121d", "config": null, "updated_at": null}`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:7431/api/grade?clip=A001.MOV"
```

**`PUT /api/grade {clip, config}`** returns `{key, updated_at}`. Verified:
`{"key": "05fe6fe1e893172ddf254f196a9a121d", "updated_at": 1788570771.2311392}`,
and a follow-up `GET` on the same clip came back `exists: true` with that same
config.

```bash
curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"clip": "A001.MOV", "config": {"primaries": {"contrast": 1.1}}}' \
  http://127.0.0.1:7431/api/grade
```

**`GET /api/grades`** lists every clip this account has a saved grade for, newest
first, without the configs (it feeds a picker, not a bulk export). Verified:
`{"grades": [{"clip_key": "...", "clip_name": "A001.MOV", "updated_at": ...}]}`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:7431/api/grades
```

**`DELETE /api/grade?clip=NAME`** returns `{deleted, key}`. Verified:
`{"deleted": true, "key": "05fe6fe1e893172ddf254f196a9a121d"}`.

```bash
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:7431/api/grade?clip=A001.MOV"
```

**`POST /api/grade/copy {from, to}`** copies one clip's saved grade onto another
(both addressed by name or by key) and returns `{key, from, config, updated_at}`
for the destination. Verified end to end against two real clips: the copy
returned the source's config under the destination's key, and `GET /api/grades`
immediately listed both.

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"from": "A001.MOV", "to": "A002.MOV"}' \
  http://127.0.0.1:7431/api/grade/copy
```

## Power windows

A window here is one shape, ellipse or rectangle, with a soft feather at its edge,
and it does exactly one job: it limits WHERE the secondary correction (the HSL
qualifier) applies, so "the orange only inside this oval" is one grade rather than
two. Centre, extent, rotation and feather are all fractions of the frame, which is
why the same numbers draw the same shape in a 960 wide preview and in the finished
4K render.

Draw one with the Window button (`#windowBtn`) in the toolbar: it shows the shape
overlaid on the picture even before the window is switched on, so it can be
positioned first and turned on once it is where it should be
(`studio/static/window-editor.js`). Drag the centre to move it, the four edge
handles to resize it, the small grip above it to rotate, and the ring around it to
feather; arrow keys nudge by a fraction of a pixel, shift-arrow by ten. Dragging
any handle auto-enables the window and ticks its checkbox, the same auto-enable a
slider drag gets everywhere else in the app.

It does not track and it does not keyframe: one shape, fixed for the whole clip.
Following a moving subject, or animating the shape over time, is not built (see
the Limits page).

Config keys, all under `"window"` in a clip's config:

```json
{
  "enabled": false, "shape": "ellipse",
  "cx": 0.5, "cy": 0.5, "w": 0.6, "h": 0.6,
  "rotation": 0.0, "softness": 0.15, "invert": false
}
```

`shape` is `"ellipse"` or `"rect"`; `cx`/`cy` are the centre; `w`/`h` are the FULL
extent, not the half axis; `rotation` is degrees clockwise on screen; `softness`
is the feather width as a fraction of the shape's own radius; `invert` swaps which
side of the edge the correction lands on.

## Playback

Pressing Play prepares one small 8-bit H.264 proxy of the whole clip (one ffmpeg
pass, cached per clip and preview width in `studio/cache/proxy/`), points a hidden
`<video>` at it, and grades every frame the browser decodes on the GPU as it plays
(`studio/static/live.js`, mode 3). Turning a knob while it plays changes what the
next frame looks like with no stall and no restart, because the proxy holds the
source pixels only and the grade is applied live rather than baked in.

Seeking during proxy playback is frame exact: a `<video>` element and the still
frame path name a timecode differently by up to a whole frame, so scrubbing has to
land deliberately on the same frame index the timeline addresses rather than its
neighbour, which is what `live.js`'s `clipToProxyTime`/`proxyToClipTime` do. The
pixels themselves are not pixel-identical to a still render at the same time,
because the proxy is an 8-bit, 4:2:0 lossy re-encode; how far off is a measured
number, not a guess, and it lives on the Limits page rather than here, because
that page is generated from a real run and this file is not.

If there is no usable WebGL2 context, GPU proxy playback is unavailable (tried
once at boot, against the real canvas, and it fails by returning "not ok" with a
reason rather than throwing) and Play falls back to the server stream: `POST
/api/play/prepare` renders and encodes the segment on the server itself, since
there is no GPU to hand either job to, which is slower and has no live knobs, but
plays. The same fallback also fires mid session if a config turns on a stage the
GPU path cannot render.

## Upload

The Upload button (`#uploadClipBtn`) sends the raw file body, not a multipart
form, to `POST /api/upload`, so a size-cap rejection can be answered before a
single byte of the body is consumed. Rejected outright: an extension the tool
does not read (anything outside `.mov`, `.mp4`, `.m4v`, `.mxf`, `.mkv`), a single
file over `--upload-max-bytes` (default 8 GiB), an upload that would put the
destination folder over `--upload-quota-bytes` (default 50 GiB total), or a file
ffprobe cannot read once it has landed (removed rather than kept as a clip the app
cannot open). A same-name collision gets a short random suffix instead of
overwriting the existing clip.

Where it lands depends on logins: with logins off, straight into the shared
`content/footage/`; with logins on, into that account's own
`studio/data/users/<id>/footage/`, so two accounts uploading a clip with the same
name never collide and never see each other's files.

## Render engines

The render dialog has an Engine choice: ffmpeg by default, "GPU (headless
Chrome)" as the alternative. ffmpeg is untouched and stays the reference, one
process running the whole filter graph from decode through encode exactly as it
always has. The GPU engine (`studio/render_gpu.py`) keeps ffmpeg on both ends of
the pipeline, decoding the clip and encoding the finished file, and replaces
only the middle: every frame is handed to the same `gpu.js` the live preview
uses, run inside a headless Chrome that `studio/tools/render_worker.mjs` spawns
using the `puppeteer-core` already installed under `studio/tests`. The encode is
the same codec, profile, pixel format, colour tags and audio mapping as the
ffmpeg path, so the file that comes out is the same kind of file, not a lesser
one.

It really is 10 bit, measured rather than assumed: counting distinct luma code
values inside a smooth gradient patch of the same rendered frame gives 390 for
the GPU engine against 394 for ffmpeg, over a span of about 502 codes, where the
same patch pushed through an 8 bit path holds only 123.

It is slower than ffmpeg, not faster, and that is measured too: on the same clip
and machine, wall clock from `POST /api/render` to the job reaching done, at
1920 wide ffmpeg ran 29.4s (2.45 fps) against the GPU's 81.3s (0.89 fps), and at
the clip's native width ffmpeg ran 31.3s (2.30 fps) against the GPU's 101.1s
(0.71 fps). That is 2.8x slower at 1920 and 3.2x slower at native width, and the
whole difference is the cost of moving every frame out to the browser and back:
both engines run the identical decoder and the identical encoder.

It does not match the ffmpeg render pixel for pixel at delivery size either,
also measured on the encoded output rather than on anything held in memory: at
1920 wide, the cinematic preset with grain off runs max 36.0, mean 2.26 to 2.31;
a natural preset with a power window and a secondary runs max 21.4, mean 1.49 to
1.60. Both fail the parity harness's own CLOSE threshold. At preview sizes the
two engines agree (the shipped test renders 640 wide and passes CLOSE), so the
gap is real and it grows with the size of the picture. Today the GPU render is a
second, independent opinion on the same grade, not a substitute for the ffmpeg
one, which is exactly why ffmpeg stays the default.

Grain is still applied by ffmpeg either way. When a clip's grain is on, the GPU
engine hands off to the same grain, detail and letterbox stages the ffmpeg path
uses, in the same order, rather than reimplementing ffmpeg's noise generator in
a shader.

The GPU engine needs Chrome and node on the machine it runs on, and checks for
both, plus the worker script and `puppeteer-core`, before a render starts:
missing any of them fails the job immediately with a message naming what is
missing, for example "the GPU render engine needs Chrome and node on this
machine: no Chrome (looked in ...); no node on PATH. The ffmpeg engine still
works." The worker routes under `/api/render/gpu/` exist only for that render's
own worker: they answer a random token minted for the one render in progress,
on top of the same `trusted_loopback` check the rest of the local only routes
use (see "Running it on the network" above), and refuse everything else,
including every other render's own token.

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
# one time, from a terminal on the machine running the studio (see "Logins
# and accounts" above for --create-user, --list-users and --delete-user)
echo "the-password" | content/.venv/bin/python content/studio/server.py \
  --create-user my-agent --password-stdin --role user

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

The Limits button in the app is the honesty list. `studio/static/limits.js`
builds it from three arrays (what genuinely is not possible here, what is
possible but not built, and what is broken or approximate), and the parity
sentence inside it is read from `studio/tools/parity-results.json`, the file the
parity harness writes after an actual run, not typed by hand. When this file and
that page disagree, trust the page: it is generated from the code and from a live
report, and this file is prose written about a moment in time.

The Limits button splits this into three answers that used to be run together:
what is genuinely not possible here (editing, audio, tracking, motion,
compositing), what is possible but not built yet (keyframes via `sendcmd`,
several secondaries, HDR, and node reordering), and what is still broken or
approximate. Drawn, draggable power windows and GPU proxy playback both moved out
of "not built yet" during this arc: see "Power windows" and "Playback" above for
what shipped and what each still leaves out (one shape with no tracking or
keyframes for a window; an 8-bit proxy rather than the 16-bit still path for
playback). Each claim in there was measured on this build. Notably, hardware
accelerated final render is possible but only worth about 16%, because the
filters are roughly 80% of the time and VideoToolbox does not accelerate filters
at all.
