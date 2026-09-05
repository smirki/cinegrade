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
      huecurve.js            the Hue vs Hue/Sat/Lum, Lum vs Sat, Sat vs Sat editor
      schema.js             one entry per parameter in the engine defaults
      panels.js             builds the right hand panels from that schema
      layers.js              the layers list: add, duplicate, move, remove, rename,
                             select, the migration fallback for old presets
      gpu.js                 WebGL2 re-implementation of the ffmpeg filter chain,
                             used for the live preview
      app.js               state, requests, viewer, timeline, presets, keyboard
      login.html, login.css the login page, served instead of index.html with
                             no valid session
      auth.js               wraps window.fetch, sends a signed out browser to
                             the login page on any 401 from /api/
      grades.js             client half of per clip grades: autosave, the
                             saved indicator, the copy-grade picker
      window-editor.js      the selected layer's window, drawn and dragged on the picture
      live.js               GPU still preview, the live loop, proxy playback
    tools/
      agent_grade.py         the Agent API proof: measure, patch, save, on a loop
    tests/                  puppeteer-core UI harness against real Chrome, npm test
    cache/                  derived frames and JPEGs, safe to delete (gitignored)
  grade/
    presets/*.json          shared with the CLI, this is where Save writes
    luts/looks/*.cube       the look list, this is where Import writes
    luts/layers/            layer correction cubes, one 33 point cube per layer (gitignored)
    luts/slice/             Color Slice / Tetra cubes, 33 point (gitignored)
    out/                    finished renders
```

The layout follows the pipeline. Panels on the right are in node order (Convert,
Primaries, Curves, Hue curves, Color Slice, Layers, Look, FX, Grain, Detail,
Letterbox, Output), which is also the order ffmpeg applies them, so reading top
to bottom tells you what happened to the picture in what order. See "Pipeline
order" below for the exact stage list as `build_graph` actually assembles it,
including the one place it does not match this reading order.

## Pipeline order

This is the order `build_graph` in `grade/cinegrade.py` actually assembles the
ffmpeg filter chain in, read straight from the code rather than copied from the
plan (the plan and the code disagreed on one step, see below):

```
LOG -> PREP -> CST IN -> PRIMARIES -> CST OUT -> CURVES -> SLICE
     -> LAYERS(before_look) -> LOOK -> LAYERS(after_look)
     -> FX -> GRAIN -> DETAIL -> LETTERBOX -> OUT
```

LOG is decode-normalise and exposure (`f_log_stage`). PREP is `prep.denoise`
(hqdn3d): it runs right after LOG and before the CST in, not before LOG. The
panel order in "Where things are" above (Convert, Primaries, Curves, Hue
curves, Color Slice, Layers, Look, FX, Grain, Detail, Letterbox, Output) does
not show Prep or Log as their own rows because they sit ahead of Convert and
have no panel of their own; everything from Convert onward in that list is in
the same order the graph runs it in. DETAIL is soften, sharpen, then
mid_detail (a split, blur, blend local contrast pass), in that order.

The one disagreement found writing this: an earlier draft of the
`build_graph` docstring's own summary line said `PREP -> LOG`, while the code
and the rest of that same docstring's prose (which says PREP runs "after
f_log_stage's decode-normalise and exposure") both put LOG first. Code and the
matching prose win; the summary line was wrong and has been corrected in
place, not moved.

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
| `lut` | POST | a technical, look, layer or slice (Color Slice / Tetra) cube as float32, for the GPU preview |
| `grain/plate` | GET | a raw rgb48le grain plate for one stock/size/strength/seed/softness/color, for the GPU preview |
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
- **Layers** (`layers`), any number of masked correction layers, each a hue /
  saturation / luma key and/or a window gating where a correction lands. This
  replaced the old single HSL qualifier plus one power window pair; see Layers
  below for the full shape, the UI, the migration rule for old presets, and why a
  layer's exposure, contrast, temperature and tint differ from the primaries panel's.

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
CST IN and CST OUT and grades in place, while primaries, curves, hue curves,
Color Slice, layers, the look LUT, FX, grain, detail and letterbox all still run.
Exposure, temperature and tint keep working and keep meaning stops; on this path
a stop is a code multiply rather than a log offset, because there is no log
curve to offset.

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

## Layers

Layers (`layers`, an array) replace the old single `secondary` HSL qualifier
plus one `window` shape pair with any number of masked correction layers.
Each entry in the array is one layer: a mask that decides WHERE a correction
lands, and a `correct` block that says what the correction actually is.
Layers apply serially in array order, so re-ordering the list is the only way
to re-order the maths (the engine test `layers.two_layers_apply_in_order`
renders the same two layers in both orders and requires the pictures to
differ).

### Mask: window and/or key

A layer's mask is the product of two independent parts, either of which can
be on, off, or both together:

- **Window** (`mask.window`), one shape, ellipse or rectangle: `shape`
  (`ellipse` or `rect`), `cx`/`cy` (centre, fraction of the frame), `w`/`h`
  (full extent, not the half axis, fraction of the frame), `rotation`
  (degrees clockwise on screen), `softness` (feather width as a fraction of
  the shape's own radius), and its own `invert` (grades outside the shape
  instead of inside).
- **Key** (`mask.key`), the HSL qualifier: `hue_center`/`hue_width`/
  `hue_soft` (degrees), `sat_low`/`sat_high`/`sat_soft` and `lum_low`/
  `lum_high`/`lum_soft` (0 to 1), and its own `invert`.

`mask.invert` inverts the COMBINED matte (window times key) as a whole, after
both halves are computed, rather than either half on its own. With neither
window nor key on, the matte is open everywhere and the layer is a global
grade. All geometry is a fraction of the frame, never a pixel count, which is
what lets a 960 wide preview and a 3840 wide render agree on the same shape
with no special case in the preview scaler.

`mask.show` stores "show this layer's matte" in the preset; for a quick look
without touching the config, use the Matte button (`#maskBtn`) over the
viewer instead, which shows the SELECTED layer's mask, not merely the first
enabled one.

### Correct

All fields below are under a layer's `correct` block:

| Field | Range | Unit |
| --- | --- | --- |
| `exposure` | -4 to 4 | stops |
| `contrast` | 0.3 to 2.5 | pivots on `pivot`, or mid grey in the layer's own domain when `pivot` is null |
| `saturation` | 0 to 2.5 | multiplier |
| `temperature` | -0.5 to 0.5 | stops, positive is warmer |
| `tint` | -0.5 to 0.5 | stops, positive is greener |
| `hue_shift` | -180 to 180 | degrees |
| `sat_gain` | 0 to 3 | multiplier |
| `lum_gain` | 0 to 3 | multiplier |
| `offset` | -0.4 to 0.4 each | an R, G, B push |
| `blur` | 0 to 40 (a UI convenience, not an engine limit) | gaussian sigma in pixels, quoted at a 1920 wide frame |
| `strength` | 0 to 1 | overall blend of the correction |

`blur` runs on the corrected branch only, before the matte merge, so it stays
inside the mask: a blur under a window softens inside the shape and leaves
every pixel outside it byte identical. Because it is quoted at 1920 wide and
resolved against the frame's real width (`layer_blur_sigma` in
`grade/cinegrade.py`), a grade dialled on a 640 preview renders the same
relative softness at delivery size instead of several times sharper.

### Placement, and why a layer's exposure differs from the primaries panel's

`placement` is `"before_look"` (between the curves/Color Slice node and the
look LUT, where the old secondary always ran) or `"after_look"` (between the
look and FX, correcting the graded picture). Either way, a layer runs after
CST OUT: it always sees display referred Rec.709 code, never the log or
working space signal primaries sees. A layer's exposure, contrast,
temperature and tint are display side, and that changes two formulas, not
just two controls: a layer's stop is a code multiply (2 to the power of
stops divided by 2.4) rather than an offset on a log curve, because there is
no log curve left to offset at that point in the graph, and contrast pivots
on 0.4587 (Rec.709 mid grey) where primaries pivots on the mid grey of
whatever working space it is in (0.3360 on the DaVinci Wide Gamut path). The
same number typed into the two nodes is the same intent, not the same
arithmetic; matching a primaries move with a layer has to be dialled by eye.

### Cost

Layers apply serially, and any number are allowed: nothing in the code caps
the count. Each active layer costs about the same again: measured through
the ffmpeg engine at 1920 wide, timed render (second run of each case, to
exclude bake time): 0 layers 6.07s, 1 layer 9.30s, 2 layers 11.87s, 4 layers
22.45s, each layer adding one `lut3d`, and, only when it carries a window,
one `gblur` and one `maskedmerge`. Nothing amortises across layers, so four
masked layers is roughly a three times longer render than none.

### The UI

The Layers panel (`studio/static/layers.js`) has an Add layer button, and
each layer row has duplicate, move up, move down and remove buttons plus a
rename field. Exactly one layer is selected at a time (tracked by a stable
id, not by array index, so an undo cannot leave the marker on the wrong
row); clicking a row's header selects it. The Window button (`#windowBtn`)
over the picture targets the selected layer: with no layers at all it
creates one and selects it, otherwise it just makes sure a real layer is
selected, the same as clicking that layer's own header would.

### Migrating an old preset or grade

A config that still carries `secondary` and/or `window` and no `layers` key
is rewritten into one layer on READ (`migrate_layers` in
`grade/cinegrade.py`): the qualifier becomes `mask.key`, the shape becomes
`mask.window`, and the new layer is enabled exactly when the old secondary
was, which reproduces the old rule that a window switched on over a
secondary switched off rendered no change at all. Files on disk are never
rewritten; saving from the app writes the new shape. A config that somehow
carries both `layers` and the old keys keeps only `layers`, the old keys are
dropped as residue from a client that has not caught up.

### A rendered example

This two-layer config (`layers[0]` a colour key with no window, before the
look; `layers[1]` an inverted window with a blur, after the look) was
rendered through `./cinegrade still` end to end, not just written by hand and
assumed to work:

```json
{
  "layers": [
    {
      "name": "Cool shadows",
      "placement": "before_look",
      "mask": {
        "key": {
          "enabled": true,
          "hue_center": 210.0, "hue_width": 60.0, "hue_soft": 20.0,
          "lum_low": 0.0, "lum_high": 0.35, "lum_soft": 0.1
        }
      },
      "correct": {"temperature": -0.15, "lum_gain": 0.9, "strength": 1.0}
    },
    {
      "name": "Edge warmth",
      "placement": "after_look",
      "mask": {
        "window": {
          "enabled": true, "shape": "ellipse",
          "cx": 0.5, "cy": 0.5, "w": 0.7, "h": 0.7,
          "softness": 0.3, "invert": true
        }
      },
      "correct": {"temperature": 0.2, "exposure": -0.2, "blur": 40.0}
    }
  ]
}
```

```bash
./cinegrade still footage/A001_09011336_C002.MOV --preset two-layer.json \
  --time 2 --width 640 -o out.png
```

Verified: a 640 wide PNG rendered without error, and the ffmpeg filter graph
it printed showed exactly what the config asks for. "Cool shadows" (key only,
no window) baked as a single `lut3d` appended straight into the existing
chain. "Edge warmth" (window only, inverted) came out as a `split`, a
`lut3d` on one branch, a `gblur` on that branch, and a `maskedmerge` back
together under the window matte. The source is a 2160 wide portrait clip,
and the blur sigma in that printed graph was 45.000, matching `40 * 2160 /
1920` exactly.

## Hue curves, Color Slice and Tetra

Two stages, baked into the same kind of cube as Layers:

**Hue curves** (`hue_curves`), five curves against the picture's own hue
wheel: Hue vs Hue, Hue vs Sat, Hue vs Lum, Lum vs Sat, Sat vs Sat. The
neutral is a flat olive line, not a diagonal: a point on Hue vs Hue is an
offset in turns of the wheel, a point on any of the other four is a
multiplier around 1.0, and no points at all is the identity. The three hue
axes wrap, so a point near the left edge and one near the right edge are
neighbours.

**Color Slice** (`slice`), six hue vectors (red, yellow, green, cyan, blue,
magenta) plus a measured skin vector, each with its own `hue` (degrees of
rotation, -60 to 60), `sat` (multiplier, 0 to 2) and `density` (-1 to 1,
darkens; negative brightens), plus one global `density` (-1 to 1) that acts
on every pixel at once regardless of hue. A pixel's weight for a vector is a
raised cosine of its hue distance from that vector's centre, zero at 60
degrees and scaled by the pixel's own saturation, so a grey pixel never
moves; the six chromatic weights sum to exactly 1 at every hue. Density is
`L' = L * (1 - density * S)` for the global control and
`L' = L * (1 - density * S * weight)` for a vector, and because a vector's
weight already carries the pixel's saturation, a vector carries saturation
twice where the global carries it once: measured on real footage at 640
wide, all six vector densities at 1 darken mean luma by 0.01369, the global
density at 1 by 0.06414, 4.68 times stronger at the same number over the
same pixels.

The skin vector's centre is measured, not chosen: 20.8696 degrees, the hue of
the engine's own skin probe (`grade/tools/match_ref.py`,
`PROBES["skin"] = (0.55, 0.40, 0.32)`), the same colour `match_ref` already
refuses to let a match break.

**Tetra**, a fold inside Color Slice, moves the six RGB cube corners (red,
yellow, green, cyan, blue, magenta), each an R/G/B trio from -1 to 1, with
black and white pinned so the whole neutral axis stays fixed, interpolated
tetrahedrally over the cube.

Both stages are pure functions of one pixel's RGB, so the whole of Hue
curves, the seven Color Slice vectors, the global density and Tetra collapse
into a single 33 point `.cube` (`grade/slice.py`, cached under
`grade/luts/slice/`), read with `lut3d=interp=tetrahedral` in ffmpeg and the
same tetrahedral maths on the GPU. That is what keeps the cost flat no
matter how many of these controls are on, and the price is quantisation:
baking the same worst case hue sweep at three cube sizes gave a max code
difference of 4.222 at 17 points, 2.216 at 33 (the size this engine uses),
and 0.689 at 65.

This is not Resolve's Color Slice, and Tetra here is not the Tetra DCTL: no
comparison against either was run, because neither is installed on this
machine, and the names are borrowed only because they describe what the
controls are for.

## Grain

New fields on `grain`: `stock` (`custom`, `16mm`, `35mm`, `65mm`),
`softness`, `response` (`flat` or `film`), `color`, `seed`.

A stock preset REPLACES `size`, `strength` and `softness` outright rather
than nudging them; `opacity`, `response`, `color` and `seed` are unaffected.
Measured on real footage at 1920 wide: `65mm` is size 2, strength 20,
softness 0.0 (finest, lightest); `35mm` is size 3, strength 40, softness 0.0
(this panel's old default, unchanged); `16mm` is size 6, strength 55,
softness 0.35 (coarsest, heaviest).

`response: "film"` weights the grain's amplitude by the pixel's own
luminance after the look and FX have already run: full strength at and below
mid grey, easing on a smoothstep to a quarter strength at white. `color` is
0 (one grey plate, identical on red, green and blue) to 1 (three independent
plates, one per channel), mixing between them in between. `seed` 0
reproduces the exact plate this panel has always rendered; any other value
reseeds ffmpeg's noise generator to a different plate with the same
statistics.

The GPU preview renders grain from a real server-baked plate rather than
approximating it: `GET /api/grain/plate` returns the same build_graph text
ffmpeg's own grain block runs, as raw rgb48le, so the two cannot drift
apart. Parity against ffmpeg on a rendered still, across four grain configs
(the defaults, the 35mm stock, `response: "film"`, `color: 1`) at 640 and
1280 wide: mean channel difference 0.006 of 255, max 1, 0 percent of
channels over 1. That is for a still only: the plate is one frame's worth,
cached, not one plate per output frame, so a playing or looping preview
reuses that plate on every frame. A multi-frame render (`render_gpu.py`)
still routes grain to ffmpeg on purpose, which is what gives every output
frame ffmpeg's own per-frame noise instead of one frozen plate repeated.

## Detail and denoise

`detail.mid_detail` (-1 to 1) is local contrast: the same split, blur, blend
idea as sharpen, but on a wide gaussian, sigma 2 percent of the frame width
(with a 1.0 pixel floor) rather than a fixed radius, so it moves midtone
texture instead of edges. Measured on real footage at 1920 wide: +1.0 moves
the whole frame by a mean of 6.7 of 255, -1.0 by 6.8, roughly the same
either way; a hard edge already in the shot can still swing far more than
that at that one edge. It is one radius only, not Resolve's multi-scale
midtone detail tool.

GPU parity for mid_detail was FAILING, up to 1.10 percent of channel samples
off by the full code range, until a fix landed on `f_mid_detail_segment`:
ffmpeg's `blend` filter does not clip an out of range `all_expr` result, it
casts the float to the plane's integer type, so a value like -242 wraps to
65294 (16 bit) instead of clamping to 0. Wrapping `clip(...,0,65535)` around
the same blend expression made both engines clamp at the same point. All six
mid_detail parity rows (0.5, 1.0 and -0.5, at 640 and 1280 wide) now measure
EXACT: max 1 code value, 0 percent of channel samples off by more than 1,
mean 0.0056 to 0.0060 of 255.

`prep.denoise` maps `spatial` and `temporal` (each 0 to 1) onto ffmpeg's
hqdn3d, both scaled 0 to 8 (0.5 lands near hqdn3d's own no-argument default
of 4.0), luma and chroma set equal since there is no separate split on this
RGB buffer. It runs right after the log stage and before the CST in, not
before the log stage (see Pipeline order above). Denoise is not part of the
GPU preview: turning it on always falls back to a server render, though
hqdn3d itself is not what makes that slow (measured 0.513s with denoise on
against 0.515s off, rendering the same still five times each way at 1920
wide); the server round trip, not hqdn3d, is the cost.

Temporal denoise cannot show properly on a still: it needs real motion
between frames to do anything. Measured max difference on a single still is
4.15 of 255, mean 0.95, which is hqdn3d's own rounding on its first frame,
not a bug; on an actual render it moves a mid frame by a mean of 2.52 of 255
and a max of 24.45, several times the still's residual.

## Two look slots

`look` now has a second slot, `lut2`/`mix2`, blended against the first with
`balance`, not stacked in series: `A = lerp(base, lut1(base), mix)`,
`B = lerp(base, lut2(base), mix2)`, `out = lerp(A, B, balance)`. Both slots
read the same pre-look signal and never see each other's output.

With `lut2` unset, the graph text is unchanged, not merely close: 0 of 255
max difference against today's single-slot output on both test clips, even
with `balance` and `mix2` pushed away from their defaults. At `balance` 1.0
with `mix2` 1.0 the output is 0 of 255 max difference against `lut2` applied
alone. At `balance` 0.5 the output matches the pixel average of the two
branches to within 0.50 of 65535, read back at 16 bit. The GPU shader port
renders the same parallel blend: both new parity rows (`balance` 0.5,
`balance` 1.0 with `mix2` 1.0) measure EXACT at 640 and 1280 wide.

## Apple Log 2

Not shipped. The rule for adding a new transfer function is that only
Apple's own published document counts as a source for the curve, the
primaries chromaticities and the sample values, never a third party page
quoting constants, and Apple's own document could not be fetched: its Apple
Log 2 page (avfoundation/avcapturecolorspace/applelog2) is real but prose
only, "an Apple defined Log curve" with no formula or numbers, and
developer.apple.com's downloads search for the white paper redirects to an
Apple ID sign-in wall; guessed direct PDF URLs following the existing Apple
Log white paper's naming pattern also redirect to an unauthorized page.
Nothing was invented to fill the gap: no `apple_log2` value exists for
`convert.working_space`, no code claims to decode it, and neither clip in
`footage/` is shot on it.

What would unblock it: Apple's own Apple Log 2 white paper PDF, placed
somewhere this engine can read it, since that download is gated behind an
Apple developer login the founder would have to use.

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

`deep_merge` only recurses into dict values: a list-valued field, `layers`
being the one an agent is most likely to touch, is REPLACED WHOLE by whatever
list the patch sends, not merged entry by entry. Verified against a real
running server: seeding two layers (`layers: [{"name": "L1"}, {"name":
"L2"}]`) and then patching with one (`layers: [{"name": "OnlyOne"}]`) left
the live config with exactly one layer, `OnlyOne`, not three and not the
first patch's two layers with the second's appended. An agent that wants to
add or remove one layer has to read the current `layers` array first
(`GET /api/session` or the `config` a prior response already carries), edit
it in Python or JS, and send the whole array back.

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
compositing), what is possible but not built yet (keyframes via `sendcmd`, HDR,
and node reordering), and what is still broken or approximate. Drawn, draggable
windows, the layer stack that replaced the single secondary, hue curves, Color
Slice, grain stocks, and GPU proxy playback all moved out of "not built yet"
during this arc: see "Layers", "Hue curves, Color Slice and Tetra", "Grain" and
"Playback" above for what shipped and what each still leaves out (one shape per
layer with no tracking or keyframes for a window; an 8-bit proxy rather than the
16-bit still path for playback). Each claim in there was measured on this
build. `studio/static/limits.js` still has one stale line of its own from
before this arc closed: its "Several look slots." entry (in the "possible but
not built" list) says "Several LOOK slots is still not built", which a later
entry in the same file ("Look now has two slots, but they blend in parallel,
not a stack.") contradicts and cross-references directly; see Two look slots
below for what actually shipped. Notably, hardware accelerated final render is
possible but only worth about 16%, because the filters are roughly 80% of the
time and VideoToolbox does not accelerate filters at all.
