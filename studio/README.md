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

The first calls worth making against a running server, before anything that reads
a clip: `GET /api/health` (cheap, no ffprobe, just "is it up, which build, where is
its cache and data") and then `GET /api/state` (the full picture: defaults, the
clip list, presets, looks, refs, and, per contract G2, who the server thinks this
caller is). Neither is a guess: `/api/footage` and `/api/clips` alone are not the
first call anything should make. See "API" below for both shapes, and "Agent API"
for the caller identity block `GET /api/state` carries.

Requirements are the ones the engine already has: the repo venv at
`content/.venv`, plus `ffmpeg` and `ffprobe` on PATH. Clips are read from
`content/footage/`, reference images from `content/refs/`, and renders are written to
`grade/out/`. Pillow is the one optional extra: only `cinegrade sheet` and the
frame labels the client module's `contact_sheet()` draws need it, and the venv is
uv made, so it installs the same way every other dependency did:

```bash
uv pip install --python .venv/bin/python numpy colour-science pillow
```

Two flags move the two folders that hold real work, and both exist for the same
reason: a test run must not write into it.

- **`--data-dir DIR`** (env `STUDIO_DATA_DIR`) puts the accounts, grades and
  project database somewhere other than `studio/data`. `studio/data/studio.db`
  is somebody's actual grading history, so a test run that opened it would be
  editing real work.
- **`--footage DIR`** (env `STUDIO_FOOTAGE`) uses `DIR` as the shared footage
  folder instead of `content/footage`. This one is easy to miss and matters just
  as much: with logins OFF the library's own root IS the shared footage folder,
  so `--data-dir` alone does not isolate anything a spec uploads or any folder it
  creates. `studio/tests/run.mjs` and `studio/tests/parity-gate.mjs` both make a
  temp folder of SYMLINKS to the real clips and pass it here. Symlinks rather
  than copies on purpose: a clip's identity is a hash of its bytes, so the links
  have to lead to the very same files or every saved grade and every project in
  the specs would belong to a different clip. Deleting the temp folder afterwards
  removes the links and never the clips.
- The browser harness itself (`studio/tests/run.mjs` and
  `studio/tests/parity-gate.mjs`) points its own `--cache-dir` at
  `studio/tests/.cache` rather than at a fresh temp folder, so repeat runs
  stay warm instead of re-rendering from cold every time; it is gitignored,
  and the founder's own `studio/cache` is never touched by a test run either
  way. Delete `studio/tests/.cache` to force the next harness run cold.
- **`studio/tests/.cache` is bounded, since round 1.** It is the harness's
  scratch copy of everything a run decoded: whole decoded frames, source
  stages, proxies, segments, LUTs, thumbnails and mask stills, each one named
  after a hash of the clip and the settings that made it. Nothing used to
  remove anything from it and it had reached 3.4 GB. Both harnesses now prune
  it before they start their server, oldest first: anything untouched for more
  than `CACHE_MAX_AGE_DAYS` (7) goes, and if what is left is still over
  `CACHE_MAX_BYTES` (2 GiB) the oldest files go until it fits. Both numbers
  live in `studio/tests/lib/util.mjs` and each run prints one line saying what
  it removed. A pruned entry costs a regeneration on the next run that wants
  it and can never cost a wrong answer, because the name IS the inputs. To
  clear it by hand, `rm -rf studio/tests/.cache` (the next run recreates it);
  the prune itself refuses to touch any folder that is not a
  `studio/tests/.cache` and never follows a symlink out of it, so it cannot
  reach `studio/cache`, `studio/data` or `footage/`.

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

Where the code itself lives, in one line before the tree: the engine core (the
whole grading pipeline, the CLI, presets, DEFAULTS) is `grade/cinegrade.py`; the
server and the API glue around it is `studio/*.py`; the frame measurement both of
them share is `grade/stats.py`; the first party client for driving the server from
outside is `studio/tools/grade_client.py`. `studio/cinegrade.py` does not exist:
the server imports the engine from `grade/` (see "How a preview frame is made"
below for the exact import), it does not carry its own copy.

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
      app.js               state, requests, viewer, presets, keyboard
      timeline.js           the timeline: ruler, scrub, filmstrip, mark flags,
                             the loop range, the J K L shuttle
      login.html, login.css the login page, served instead of index.html with
                             no valid session
      auth.js               wraps window.fetch, sends a signed out browser to
                             the login page on any 401 from /api/
      grades.js             client half of per clip grades: autosave, the
                             saved indicator, the copy-grade picker
      window-editor.js      the selected layer's window, drawn and dragged on the picture
      masks.js              the mask component stack in each layer, the SAM pick
                             and track UI, the viewer's overlay tint
      live.js               GPU still preview, the live loop, proxy playback
      mobile.js             the phone layout's page bar and floating preview
    tools/
      grade_client.py        first party client: Studio class, brief/bands/diff,
                             decode/measure, contact_sheet. See "Agent API" below
      agent_grade.py         the Agent API proof: measure, patch, save, on a loop,
                             now built on grade_client.py instead of its own copies
    tests/                  puppeteer-core UI harness against real Chrome, npm test
      run.mjs                the spec suite: real server, real SAM stub, real Chrome
      parity-gate.mjs        GPU versus ffmpeg, every stage, plus the mask
                             component fixtures (on by default, a FAILED row
                             fails the gate)
      mask-stack-ref.mjs     the mask model v2 arithmetic (C1) on plain arrays,
                             no browser and no GPU: the fold from zero, the three
                             ops, invert before feather, the finesse order, the
                             two roundings, the frame index and the frame cache.
                             Run it directly or as spec 31 of run.mjs
    cache/                  derived frames and JPEGs, safe to delete (gitignored,
                             keyed off --data-dir/--cache-dir, see "Run it" above)
  grade/
    cinegrade.py             the engine: pipeline, CLI, DEFAULTS, presets
    stats.py                 frame_stats, bands, decode_image: the one shared
                             measurement, imported by the server, the CLI and
                             grade_client.py alike
    presets/*.json          shared with the CLI, this is where Save writes
    luts/looks/*.cube       the look list, this is where Import writes
    luts/layers/            layer correction cubes, one 33 point cube per layer (gitignored)
    luts/masks/             layer mask and window mattes, baked per layer (gitignored)
    luts/slice/             Color Slice / Tetra cubes, 33 point (gitignored)
                             (these three are GENERATED and hash named; they live
                             here only for the bare CLI. Under the studio, or with
                             CINEGRADE_CACHE_DIR/STUDIO_CACHE_DIR/STUDIO_DATA_DIR
                             set, they live in that run's own cache instead, see
                             "Per caller identity" below)
    luts/technical/         input transform cubes (HLG, PQ, Rec.709), from
                             grade/tools/make_cst.py, see "Input transforms" below
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
written against, and it is the exact dict `POST /api/stats`, `cinegrade stats` and
`studio/tools/grade_client.py`'s `stats()`/`brief()` all return (`grade/stats.py`
owns it once, nobody re-derives it). On the wire (`POST /api/stats`, and
`cinegrade stats --json` on a clip or on `--image`) this dict is one level
down, wrapped as `{"key", "size", "stats"}` (plus `region`/`region_pixels`
when a `region` was given): read `luma` off `response["stats"]`, not off the
top level, or the first field access is a `KeyError`. `grade_client.py`'s
`Studio.stats()` returns that same wrapped shape (`.stats(...)["stats"]`);
its `brief()` and `diff()` expect the unwrapped inner dict, matching the
usage in "The first party client module" below. A WEIGHTED measurement (by
`matte` or by `mask`, below) adds two more fields at both levels: `coverage`,
the mean of the weight over the frame, which is the share of the picture the
numbers were taken from, and `no_coverage`, true on a frame the weight is
zero everywhere on. On a `no_coverage` row every measurement block is `null`
rather than 0, because nothing was measured there; see "A frame the mask
covers nothing of" below. The list below describes what lives inside
`stats`:

- luma percentiles at 5, 25, 50, 75 and 95, as 0 to 1
- mean saturation, computed as `(max - min) / max` per pixel. This falls
  under ANY toe lift, `primaries.black_lift` included (measured under
  "Which way is which" below: 0.1843 to 0.1716 to 0.1502 as `black_lift`
  goes 0, 0.1, 0.2, with luma barely moving), because raising the floor
  raises `min` toward `max` on every lifted pixel; a falling saturation mean
  does not by itself mean a grade went duller, check `black_lift`/
  `highlight_rolloff`/a curve's toe before reading it as a colour change.
  ACES (`convert.tonemap: aces`) desaturates highlights the same way past
  about +1 stop of `convert.exposure`: measured on real footage, saturation
  mean fell 23 percent over the first stop pushed (0.1843 to 0.1421) and 31
  percent over the second (to 0.0984), an accelerating fall rather than a
  steady one.
- percent of pixels in each hue family (warm, green, cool, magenta), counting only
  pixels above 10 percent saturation so that grey does not vote
- percent clipped black (code 2 or under) and clipped white (code 253 or over)
- `bands`: the same frame sliced into eight equal brightness bands (0 = pure
  black, 1 = pure white). This is NOT eight per-band dicts: `bands` is one
  dict of four parallel eight-item lists (`saturation`, `warm`, `tint`,
  `count`, plus the nine-item `edges` the bands sit between), so band 3's
  numbers are `saturation[3]`, `warm[3]`, `tint[3]`, `count[3]`, not a
  fourth entry in some `bands[3]`. Read as:
  - **saturation**: how colourful that brightness range is on average (0 = no
    colour, higher = more vivid). Near zero in a shadow band or a highlight band
    is normal, not a fault: it just means little colour lives there.
  - **warm**: mean red minus mean blue in that band. Positive is warmer, negative
    is cooler, zero is neither.
  - **tint**: mean green minus the average of red and blue in that band. Positive
    leans green, negative leans magenta, zero is neutral.
  - **count**: how many of the frame's pixels actually fall in that band. A band
    with very few or zero pixels is a genuinely dark or bright frame with little
    content there; its saturation/warm/tint read as flat zero rather than a
    number computed from almost no pixels.

  None of the four is ever compared against another band's, or used to call one
  band "wrong": a band is a readout, the same way the luma percentiles and hue
  family percentages above it already are. A whole frame number, banded or not,
  also moves with framing: a hue family percentage tracks how much sky or skin is
  in shot as much as it tracks the grade, so a wide shot and a close-up of the
  same grade will not read the same. See "Match Reference" below for
  `hue_divergence`, the check for a metric that improved only because a fit's
  target content does not match the shot's.

Those are measured on the preview frame, at 640 wide and 8-bit by default (`stats`
on the CLI measures the source's own resolution instead, since there is no preview
render to piggyback on) from the same decoded array the picture and the scopes come
from. Picture and numbers therefore cannot disagree with each other, but they can
both differ slightly from the full resolution 10-bit render. The Limits panel in
the app spells out where.

`region` (four fractions of the frame, `X0 Y0 X1 Y1`, after rotation, 0 0 1 1 being
the whole frame) narrows every one of these numbers to one rectangle: on
`POST /api/frame` and `POST /api/stats` it is the `region` field, honoured whether
the request is measuring a clip, a `ref`, or an outside `path`; on the CLI it is
`--region X0 Y0 X1 Y1` on `still`, `compare` and `stats`. The crop runs AFTER the
whole grade and before any output scale, so the patch is exactly the pixels the
full render puts there: windows, vignette and grain sit where the full frame would
show them, not where they would sit in an isolated crop. `zoom` (a request field on
`POST /api/frame`/`POST /api/stats`, or `--zoom` alongside `--region` on `still`
and `compare`) renders the region at that many times the size it would have inside
a plain render at the requested width, default 1, capped by how many real source
pixels the region actually has.

## API

Every route is under `/api`. Configs are posted whole and merged over the engine
defaults, so a partial config is legal everywhere.

| Route | Method | What it does |
| --- | --- | --- |
| `health` | GET | `{ok, version, clips, uptime_s, ffmpeg_slots_free, cache_dir, data_dir, logins}`; the cheapest possible "is it up" call, no ffprobe. See "Run it" above and "Per caller identity" below |
| `state` | GET | defaults, clips (each with a `source` block and rotation tags, see "Input transforms" and "Rotation" below), presets, looks, refs, renders, stat definitions, and this caller's own identity under `caller` |
| `clips` | GET | probe info for everything in `footage/` |
| `frame` | POST | render one preview frame, returns JPEG. Takes `region`/`zoom` (see "What the numbers mean" above), `mask_layer` when `mode` is `"mask"` (which layer's matte to show; absent is the first enabled one), and a read only `path` (an absolute file outside the footage root, logins off only, see "Reading a file directly" below) |
| `stats` | POST | the numbers above for one frame. Takes `ref` (a name under `content/refs`, measured instead of a clip), `times` (a list of seconds, answers `{"results": [...]}` instead of one block), `region`, `width`, the same read only `path`, and one of `matte` (an id) or `mask` (a whole component stack, see `POST /api/stats` below) to weight the measurement |
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
| `library` | GET | one folder of a library, or the shared / team / trash index, see Library and sharing below |
| `library/mkdir`, `library/rename`, `library/move`, `library/trash`, `library/restore` | POST | folders and files in a library |
| `library/share` | GET / POST | the grants on one item, and granting one |
| `library/unshare` | POST | take one grant off |
| `people` | GET | users and teams in the caller's org, for the share picker |
| `activity` | GET | the org's library feed, newest first |
| `grade` | GET / PUT / DELETE | this account's saved grade for one clip, see Per clip grades below |
| `grades` | GET | every clip this account has a saved grade for |
| `grade/copy` | POST | copy one clip's saved grade onto another |
| `auth/login`, `auth/logout`, `auth/me`, `auth/token`, `auth/tokens` | see below | see Logins and accounts below |
| `reveal` | POST | open a Finder window on the machine running the server; answers only that same machine, see Running it on the network below |
| `match` | POST | fit a look cube toward a reference image, see Match Reference below. Takes `name`/`out_dir` (where the fitted cube lands) and answers with `recommended` (bool) and `bands` for the reference and the source frame |
| `source` | POST | the decoded, downscaled source frame as raw rgb48le |
| `lut` | POST | a technical, look, layer or slice (Color Slice / Tetra) cube as float32, for the GPU preview |
| `grain/plate` | GET | a raw rgb48le grain plate for one stock/size/strength/seed/softness/color, for the GPU preview |
| `session` | GET / POST | read or change the live config of the open page. `POST` takes an optional `if_rev`: a write that would land on a revision other than the one given is refused 409 with the current `rev` and the current session, instead of silently overwriting it |
| `session/wait` | GET | long poll, returns as soon as the live config moves |
| `parity/report` | GET / POST | the GPU versus ffmpeg parity numbers |

Preview requests carry `X-Studio-Client` and `X-Studio-Gen` headers. The server drops
a request whose generation is already stale, so dragging a slider does not queue up a
line of doomed ffmpeg runs behind the one you actually want.

### `mask_layer`, and reading a file directly

`POST /api/frame` with `"mode": "mask"` shows a layer's matte instead of the graded
picture (the picture is `"graded"`, the default; the ungraded source is `"flat"`).
`"mask_layer"` picks WHICH layer's matte to show, by its index in `layers`; leave it
out and the first enabled layer's matte is what comes back. Without it, checking a
second layer's mask meant disabling every other layer and rendering twice.

`"path"` on `POST /api/frame` and `POST /api/stats` is a different thing from
`clip` or `ref`: an absolute file the server was never told about (a render mid
copy, a frame from outside `content/footage/` entirely). It is decoded and
measured or served exactly as it sits on disk, no grading `config` applied, no
project opened, no session touched, nothing registered anywhere, and it is
refused 403 the moment logins are on (naming itself, not the ordinary 401 every
other route gives with no session): with an account system there is no login to
check the file against, so honouring an arbitrary path would be a way past every
other route's read guard. This is the server side of the one decode function
`grade/stats.py`'s `decode_image()` gives the CLI's `stats --image` and the client
module's `decode()` too, so a file measured through any of the three is the same
bytes and the same numbers.

Measuring a `path` raw, ignoring whatever `config` is sent, is by design and
stays that way: threading a config through a `path` read would mean grading an
arbitrary file with the server's own defaults (an aces tonemap and an Apple
Log CST land on a plain PNG the moment `config` is `{}`, which every existing
caller already sends), it would give `path` an escape hatch around the same
read guard that refuses it 403 with logins on, and the render cache has no
file mtime in its key, so a second call after the file changed on disk would
serve the first call's pixels. To measure a graded intermediate: render it
first with the CLI (`cinegrade still FILE -o out.png --preset ...`, which
takes any path, not only a registered clip), then measure `out.png`, either
`POST /api/stats {"path": "/abs/out.png"}` or `cinegrade stats --image
out.png`. Render, then measure; a raw `path` read never grades.

## CLI reference

`grade/cinegrade.py` is the primary agent surface; the server mirrors it, not
the other way round. Every subcommand below takes `input` (the clip or still
to grade, positional) plus a shared set of grading flags unless noted:
`--preset/-p NAME_OR_PATH`, `--preset-from {auto,file,studio,catalog}`
(which namespace a bare `--preset` name may come from, see below),
`--look/-l NAME`, `--exposure/-e STOPS`,
`--tonemap {aces,filmic,none}`, `--working-space {dwg,direct,rec709}`,
`--contrast`, `--saturation`, `--temperature`, `--tint`, `--rotate
{auto,0,90,180,270}` (beats the config's own `rotation`, see "Rotation"
above), `--no-autorotate` (alias for `--rotate 0`), `--input-space {auto,
apple_log,hlg,pq,rec709,slog3,logc3,vlog,clog3,dlog}` (overrides
`convert.input`, including whatever `--preset` carries; see "Input
transforms" above; on `render`, `still`, `compare`, `scopes`, `stats`,
`orient` and `sweep`), `--verbose/-v` (also un-silences colour-science's own
scipy/matplotlib startup notice, silent by default on every command).
`NAME_OR_PATH` on `--preset` resolves through three namespaces, in this
order (`load_preset` in `grade/cinegrade.py`):

1. **a literal path**, always first, so a file `grade save`/`preset save`
   wrote, a `PUT /api/grade`/`POST /api/preset` body saved to disk, or any
   hand written JSON goes straight to `--preset` with no need to copy it into
   `grade/presets/` first, including for a field with no dedicated flag (see
   the measured rows added to "Which way is which" below for a worked
   example);
2. **a preset saved on the studio** named by `STUDIO_URL`
   (`GET /api/preset?name=&expand=true`);
3. **the built-in look catalog**, `grade/presets/NAME.json`.

Whichever answered is printed to stderr (never stdout, which carries
`--json`), so a checkpoint that records the command also records which preset
it got; the catalog line only prints when `STUDIO_URL` is set, because that is
the only time a bare name could have meant two things. `--preset-from
{auto,file,studio,catalog}` forces one namespace and refuses instead of
falling through, which is how you say "the built-in `cinekit`, not the one
somebody saved on this server". A name in neither namespace is refused with
both places named and the catalog listed.

`--agent`/`--attach`/`--if-rev`, and `--port`/`--url` (env `STUDIO_PORT`/
`STUDIO_URL`), are documented under "Per caller identity" in Agent API
below, since they only apply to `session`, `whoami`, `project`, `match`,
`preset`, `grade` and `mask`, the seven commands that talk to a running
server rather than grading a file directly. One rule worth repeating here
since it is easy to trip on a mixed command line: naming `--agent`/
`STUDIO_AGENT` on any of those seven without also naming a server
(`--port`, `--url`, `STUDIO_PORT` or `STUDIO_URL`) is refused before any
request goes out, rather than silently landing on the port 7431 default,
which is a human's own live studio.

| Command | Flags beyond the shared set | What it does |
| --- | --- | --- |
| `render IN -o OUT` | `--start`, `--duration/-t` (seconds); `--allow-partial` (render anyway when a matte layer does not cover the window asked for); `--no-audio`; `--codec NAME` (this render only, never the preset file; extension must agree, `prores_ks` to `.mov`, else `.mp4`, or the command refuses before ffmpeg runs); `--width N` / `--scale F` (mutually exclusive; scales the pixel denominated FX params the same way the studio preview does); audio maps only the first stream (`0:a:0?`), so a second, undecodable stream (an iPhone spatial audio `apac` track) no longer kills the render | writes a finished file to `grade/out/` (or wherever `-o` points), streaming `render Xs of Ys` progress to STDERR once per second of rendered output while it runs (stdout carries nothing until the end), then prints the one final `rendered (input INPUT, rotation ROTATION) -> PATH` line on stdout, naming the resolved input transform (`Input transforms` above) and the rotation mode actually used, not only `rendered -> PATH` |
| `still IN -o OUT` | `--time` (default 0); `--width`; `--region X0 Y0 X1 Y1`, `--zoom F` (see "What the numbers mean" above) | one graded frame as a PNG |
| `compare IN -o OUT` | `--time`; `--width` (default 560); `--region`, `--zoom`; `--looks a,b,c`; `--presets a,b,c`; `--open` (Preview.app) | a grid of the same frame under several looks or presets |
| `scopes IN -o OUT` | `--time`; `--width` (default 700); `--open` | histogram, waveform, parade and vectorscope as one image |
| `orient IN` | `--time`; `--height` (default 600); `--open`; `--json` (prints `{tag, candidates, rotation_tag_suspect, rotation_tag_note, ...}` instead of rendering the default sheet; see "Rotation" above); `--sheet OUT.jpg` (a labelled 2x2 of the four fixed candidates, 0/90/180/270; independent of `--json`, both together write both and the JSON dict gains a `"sheet"` key naming the path) | the default hand drawn row sheet, a labelled 2x2, the JSON facts, or (with both flags) all of the JSON plus the 2x2; `rotation_tag_suspect` is a prompt to go look, not a verdict, and `rotation_tag_note` says why in one sentence, see "Rotation" |
| `stats [IN]` | `--time`; `--image FILE` (measure a still instead of a clip; `input` becomes optional and a clip positional given alongside `--image` is refused, naming both; `--preset` and the look/primaries flags are ignored); `--json`; `--region X0 Y0 X1 Y1`; `--times a,b,c` (a list of seconds, prints one row per time instead of one block; not with `--image`); `--matte ID` (weights every percentile, band and hue family by that matte's value, resolved straight off disk under `grade/mattes.py`, no running server needed; `region` crops first, then `matte` weights what is left; refused together with `--image`, a matte measures a clip over time, a still is one frame; a time past what the matte has tracked so far falls back to its nearest written frame and the response gains a `warnings` field saying so; a matte that covers nothing at the requested region and time is a NO COVERAGE row, not a refusal: `coverage` 0, `no_coverage: true`, every measurement block `null`, printed and returned like any other row, so a loop over timestamps survives a frame the subject has walked out of); `--mask JSON_OR_FILE` (a whole mask description instead of one matte id: the same `mask` block a graded layer carries, `{"components": [{"type": "matte|key|luma|window", "op": "add|intersect|subtract", ...}], "finesse": {...}}`, each component with its own `invert` and `feather`, folded by the engine's own `mask_matte`, the code the layer renderer uses, so a measurement and a render agree by construction. Inline JSON or a path to a JSON file; a pasted whole layer dict is accepted and its `mask` block taken. `--mask` with one matte component measures exactly what `--matte ID` measures. The response adds `coverage`, `no_coverage` and `mask_mattes` (the matte ids the stack reached, in stack order) beside `measured_width`. Refused together with `--matte` (two ways to say one thing), with `--region` (a stack is written in the whole frame's coordinates: say the rectangle with a `window` component instead) and with `--image`; refused when the stack starts with an `intersect` or a `subtract` (the fold starts at zero, so the first enabled component has to be an `add`), when the description selects nothing at all (`{}` would measure the whole frame), and when a matte component names an id that does not exist or has no id yet) | the same measurement dict `POST /api/stats` returns, see "What the numbers mean" above; `--json` on a single clip or a single `--image` prints exactly the `{"key", "size", "measured_width", "stats"}` envelope, the numbers live one level down under `stats`; `measured_width` is the width the measurement was actually taken at (the printed block says `1920x1080  measured at 1920 wide`), so two numbers taken at different sizes cannot be compared by accident; `--times` rows come back as `{"results": [...]}`, one `{"time", "key", "size", "stats"}` row per second. A still-format file (`.jpg/.jpeg/.png/.tif/.tiff/.webp`) passed as the clip positional, not via `--image`, is refused and told to use `--image` instead. `--image` on a still is measured as display referred rec709 (a stderr line says so) unless `--input-space`/`--working-space` is given explicitly, in which case that flag now really applies the transform |
| `sweep IN` | `--time`; `--param DOTTED.PATH` (required, e.g. `fx.halation.strength` or `layers.0.correct.exposure`); `--values v1,v2,...` (required, comma separated: a bool, a number or a string, tried in that order; a leading negative parses unquoted, `--values -0.1,0,0.1`, as well as with an `=`); `--width` (default 640, scaled down from the source, matching `POST /api/stats`'s own default; this used to always measure at the source's full resolution); `--json` (stdout stays pure JSON even with `--sheet`, which then prints its path to stderr instead); `--sheet OUT.jpg` (a labelled panel per value, through the same code `sheet` uses) | one stats row per value; reports what each value measures, never which to pick (no numeric distance score exists anywhere in this tool on purpose) |
| `sheet A B C -o OUT` | `inputs` (one or more: PNG, JPG, or any ffmpeg-readable video, one frame at `--time` from each); `--height N` / `--width N` (mutually exclusive; default height 480; `--height` fixes every panel's height, `--width` fixes the sheet's own width and solves the shared height); `--grid COLSxROWS` (e.g. `2x3`; default is one row); `--labels a,b,c` (default: each input's filename stem); `--time` (default 0, for any video input); `--region X0 Y0 X1 Y1` (crops each panel to fractions of ITS OWN size, after loading, before the shared height is solved; no `--zoom`, a cropped panel is already rescaled to the shared height afterwards) | a labelled comparison image, common height, padded, mixed aspect ratios never fail |
| `docs [SECTION]` | `SECTION` (a heading's text, matched case insensitively at any level, skipping headings inside fenced code blocks; omit to list); `--list` (list every heading and exit; a `SECTION` that matches nothing also lists them, rather than failing) | prints one section of this file, or the whole table of contents |
| `session {get,patch} [JSON]` | `JSON` for `patch` (a partial config, deep merged into the live one; `-` reads it from stdin); `--port`/`--url` (default port 7431 with neither, env `STUDIO_PORT`/`STUDIO_URL`, see "Per caller identity" below); `--replace` (overwrite instead of merging); `--by`; `--message` (for `patch`, a readable commit message); see "Per caller identity" below for `--agent`/`--attach`/`--if-rev` | reads or changes a running server's live config, see "Driving the open page from outside" below |
| `whoami` | `--port`/`--url`; `--json` (carries `"project_clip"` next to the project key once one is open); see "Per caller identity" below for `--agent`/`--attach` | who a write from this shell counts as, and what project is open (with its clip name), see "Identity" above |
| `project {open,show,log,checkout,fork,undo,redo,rotate,time}` | `open CLIP [--rotation auto\|0\|90\|180\|270]`; `log [--limit N] [--all]`; `checkout ID`; `fork [NAME] [--from ID]`; `rotate auto\|0\|90\|180\|270`; `time SECONDS`; every one takes `--port`/`--url`, `--by`, `--json`, and (see below) `--agent`/`--attach` | a clip's git style history, see "Projects and history" below |
| `match REF CLIP` | `--time`; `-p/--preset` (a preset name or JSON file, sent as this call's config); `--method {reinhard,histogram}` (default `reinhard`); `--rotate {auto,0,90,180,270}` (sent as this call's own `rotation` field; falls back to `--preset`'s own config `rotation`, then `auto`, same order every other subcommand's `--rotate` falls back through; `match` previously had no rotation handling at all); `--strength N` (default 1.0); `--luma-preserve`/`--no-luma-preserve` (default on); `--ref-crop X0 Y0 X1 Y1`, `--frame-crop X0 Y0 X1 Y1` (whole frame, `[0,0,1,1]`, when neither is given, never a browser tab's saved rectangle, see "Match Reference" above); `--name`, `--out-dir`; `--json`; `--port`/`--url`/`--agent`/`--attach` | `POST /api/match`: no local equivalent exists, so this is a thin wrapper, the one place the server is the primary surface and the CLI mirrors it, not the other way round |
| `preset {save,load} NAME` | `save NAME -p grade.json --comment TEXT`; `load NAME [--expand] [-o file.json]`; both take `--json`, `--port`/`--url`/`--agent`/`--attach` | `POST`/`GET /api/preset`: the shared, named grade store, read and written by every account and agent alike |
| `grade {save,load} CLIP` | `save CLIP -p grade.json --message TEXT`; `load CLIP [-o file.json]`; both take `--json`, `--port`/`--url`/`--agent`/`--attach` | `PUT`/`GET /api/grade` (contract C3): one clip's own per clip grade, distinct from the shared `preset` above and from `session patch` (the live config a browser tab is watching; `grade save` never wakes it) |
| `mask segment CLIP` | `--time` (default 0); `--text "PROMPT"` (repeatable); `--point X,Y[,neg]` (repeatable, fraction of the frame, trailing `,neg` for a negative point); `--box X0,Y0,X1,Y1` (repeatable); `--rotate`; `-o DIR` (downloads every instance's `overlay`/`mask` preview image); `--json`; `--port`/`--url`/`--agent`/`--attach` | `POST /api/mask/segment` (contract C4): SAM's synchronous pick on one frame, `{"pick_id", "instances": [{"id", "score", "box", "area", "overlay", "mask"}, ...], "candidates": N}`, nothing tracked or saved yet, look at the previews before choosing an id to track. A prompt the model matches nothing for is NOT a silent empty list: the response carries `"candidates": 0` and a `message` (`no match for "shirt" at 3s: 0 candidates from the model. Try another word ...`), and the CLI raises that sentence, so `mask segment` exits 1. With `--json` the payload still prints first, then the command exits 1, so a JSON caller loses nothing and a shell caller gets a real failure |
| `mask track CLIP` | `--text "PROMPT"` (repeatable) or `--pick PICK --select IDS` (comma separated ids, or `all`), not both; `--start`, `--end` (seconds); `--steady N` (temporal smoothing frames); `--rotate`; `--wait` (blocks, prints progress to stderr the way `render` does, exits non zero on a `failed` job; a cache hit has no job to wait on and says so on stderr rather than failing); `--force` (redo a matte this recipe already has, frames deleted first); `--json`; `--port`/`--url`/`--agent`/`--attach` | `POST /api/mask/track`: starts a background SAM track, returns `{"job_id", "mattes": [{"matte_id", "recipe", "state"}, ...], "cached": false, "start_frame", "end_frame"}` immediately unless `--wait`. Cached by clip identity, rotation and recipe, and the cache is a hit only while its answer is still usable: a live job for this recipe, or every frame of the window already written, is free (`{"job_id", "cached": true, "mattes": [...]}`); a HOLE in the window resumes from the first missing frame; a `failed`/`stale`/`cancelled` matte restarts the window; `force` restarts it after deleting the frames. A resume or a restart answers with `resumed`, `restarted`, `resumed_from` and a `message`, writes back into the SAME matte id, and moves the state to `queued`/`running` again. Before this, an identical retry of a dead track handed back the same dead matte and `job_id: null`, and the only way to get another attempt was to change the words |
| `mask jobs` | `--json`; `--port`/`--url`/`--agent`/`--attach` | `GET /api/mask/jobs`: every queued or running track job, visible to every caller |
| `mask list CLIP` | `--full` (the per frame arrays as well as the summary); `--json`; `--port`/`--url`/`--agent`/`--attach` | `GET /api/matte?clip=`: that clip's mattes, a SUMMARY per matte by default (`matte_id`, `state`, `done_frames`/`frames`, `span`, `coverage` of that span, `mean_score`, `mean_area`, `quality`, `recipe`), not the per frame arrays: four mattes over 384 frames used to be thousands of mostly-null numbers just to read four states. `--full` (`?full=1` on the route) adds `areas`, `scores` and `ious` back; `GET /api/matte/<id>` always carries them. The printed line also flags suspect frames (`SUSPECT 3 frames from 4.25s`) and closes with the reminder that every matte is frozen outside its span. Mattes belong to the clip, shared by every caller |
| `mask show ID` | `--strip` (one panel per second of the frames the matte really wrote, matte tinted over the picture, with the tracked area curve underneath, needs `-o` and Pillow); `-o/--output OUT.jpg` (required with `--strip`); `--width N` (default 220, panel width for `--strip`); `--json`; `--port`/`--url`/`--agent`/`--attach` | `GET /api/matte/<id>`: one matte's index (state, frame count, recipe), plus its `span`, `coverage` and `quality`. Works on a matte that is still running or only partly written: the printed block names the span in seconds, says "frozen outside span" out loud, and lists the suspect frames; the strip walks only the written span, labels a panel `held <frame>` when the server served a frozen one, marks suspect frames in red on the curve, and refuses with a sentence (naming the state and the count) when nothing has been written yet. A matte with nothing written used to be drawn as if the whole requested length existed, and the `None` tail raised a `TypeError`, so the mattes most in need of a look over time were the ones that could not be looked at. With `--strip`, the verification pass to run before grading on a matte, see the Masks section of `.claude/skills/studio-grading/SKILL.md` |

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

## Input transforms

The engine was built for Apple Log, and for a while it assumed that silently. The
grade now starts by deciding what the source actually IS: that decision is
`convert.input`, a top level config key, one of `"auto"` (the default),
`"apple_log"`, `"hlg"`, `"pq"`, `"rec709"`, or one of five manufacturer camera
logs, `"slog3"`, `"logc3"`, `"vlog"`, `"clog3"`, `"dlog"` (see "Camera logs"
below). `GET /api/state`'s `convert_definitions.input` carries this exact list
under `values`, the five camera logs again under their own `camera_logs`, and a
`note` field stating the same auto rule in one paragraph, so an agent can read
what the field accepts instead of guessing or reading the source. `"auto"`
reads the file's own tags and never resolves to a camera log:

| what ffprobe says | `resolved_input` |
| --- | --- |
| transfer `arib-std-b67` | `hlg` |
| transfer `smpte2084` | `pq` |
| transfer `bt709` with `bt709` primaries | `rec709` |
| no usable transfer tag (what an Apple Log or camera log file carries) | `apple_log` |
| anything else | `apple_log`, plus a warning naming the tag |

The last line is what keeps every existing grade byte identical: an Apple Log
clip has always had bt2020 primaries and no transfer tag, so `auto` decodes it
exactly as it always has, and an unrecognised pair of tags still renders the way
it always did rather than refusing, with the warning telling you what to set
`convert.input` to if that guess is wrong. A camera log file carries the exact
same tags (bt2020 primaries, no transfer tag), so `auto` decodes it as
`apple_log` too, wrongly: name the camera log by hand (in the Input select, in
`convert.input`, or with `--input-space` on the CLI, see "Choosing the input
from the CLI" below) whenever the source is not really Apple Log. An explicit
value is never second guessed. Every clip in `GET /api/state` (and the app's
clip list) shows its own `source` block: the three raw tags (`transfer`,
`primaries`, `matrix`), `resolved_input`, and any `warnings`, so you can see the
decode before rendering anything.

Every input decodes to the same scene linear point on BT.2020 primaries that
the Apple Log path has always reached, so `working_space`, `tonemap`, `encode`
and every creative control below mean exactly what they always meant: an 18
percent grey card renders the same whether the source was shot Apple Log, HLG,
PQ, delivered as Rec.709, or shot on one of the five camera logs. The standards
behind each decode, so this is checkable rather than asserted:

- **HLG**: ITU-R BT.2100's inverse OETF, then the BT.2100 OOTF at system gamma
  1.2 on a nominal 1000 cd/m2 display.
- **PQ**: SMPTE ST 2084's EOTF to absolute cd/m2.
- **Rec.709**: ITU-R BT.1886 (a pure 2.4 gamma) to display linear, then one
  documented scale so a BT.709 encoded 18 percent grey card lands on 0.18 scene
  linear instead of the 0.62 stops dark it would otherwise land at.
- The shared anchor across all of them is ITU-R BT.2408: 26 cd/m2 (its Reference
  Level) is 0.18 scene linear, which puts 203 cd/m2 (its Reference White) at
  1.405. Run `.venv/bin/python grade/tools/make_cst.py --anchors` on a live
  checkout to print 18 percent grey in each input's own code values (apple_log
  0.4883, hlg 0.3786, pq 0.3800, rec709 0.4090, slog3 0.4106, logc3 0.3910,
  vlog 0.4233, clog3 0.3280, dlog 0.3988) as a standing cross check that these
  constants are anchored on the standard or the vendor's own document, not
  fitted to a picture. Each camera log number is cross checked a second way:
  against colour-science 0.4.7's own independent implementation of the same
  curve and gamut, agreeing to 1e-8 or better, and that cross check runs on
  every test suite run so a future edit to a constant has to survive it.

Primaries are a separate question from the transfer curve, handled separately: a
file whose primaries are `bt709` is matrixed into BT.2020 before anything else
runs; a file already on `bt2020` primaries passes through unchanged. A file that
carries one input's curve on another's primaries (an HLG file tagged with bt709
primaries, say) loads its own cube for that exact combination, so there is no way
to apply the wrong matrix by accident.

Exposure is a true doubling of scene light per stop for every input, not the
Apple Log constant borrowed for everything else: measured through a real ffmpeg
render, one stop comes out between 1.991 and 2.000 on HLG, PQ and Rec.709 alike
(the residual is the LUT's own 16-bit rounding). None of the five camera logs
can spend a stop as a simple code offset the way Apple Log does (each has a
linear toe and an offset inside its own logarithm, so an offset would be a
different number of stops at every code, worst exactly where the shadows are),
so their exposure runs the vendor's own inverse curve, a multiply by
`2 ** stops`, and the vendor's own forward curve, as one expression: measured on
seven patches from three stops under mid grey to three over, one stop comes out
1.9946 to 1.9999, one stop down 0.4967 to 0.5013.

`convert.working_space: "rec709"` is the other half of this and still means what
it always meant: the source is ALREADY display referred, so skip the log stage,
CST IN and CST OUT and grade in place, while primaries, curves, hue curves, Color
Slice, layers, the look LUT, FX, grain, detail and letterbox all still run.
Exposure, temperature and tint keep working and keep meaning stops; on this path
a stop is a code multiply rather than a log offset, because there is no log curve
to offset. What changed is which files this is allowed on: asking for `rec709`
working space on an HLG or PQ file (both BT.2020) used to be refused outright and
now is allowed, with a warning, because neither one is camera log; asking for it
on a file that resolves to `apple_log` because its transfer tag is missing or
unrecognised is still refused, naming the right mode, because that file really is
camera log and the log curve would never be undone (measured on a real delivery
file wrongly graded this way: median luma fell from 0.332 to 0.238 and mean
saturation rose from 0.33 to 0.59, giving neon colour and blown skin). An
explicit camera log value gets the exact same refusal as `apple_log`, for the
exact same reason: that path undoes no curve at all, so a log source would
render flat and grey with nothing on screen to say why, and naming a camera log
already says in so many words that the source has one. Use `dwg` or `direct`
instead. An untagged file is never guessed at either way.

### Camera logs

Five manufacturer log curves are `convert.input` values alongside `apple_log`,
`hlg`, `pq` and `rec709`, and they decode to the same scene linear point every
other input reaches, so everything downstream means what it always meant:

| value | what it is | the document it comes from | 18% grey code | gamut |
| --- | --- | --- | --- | --- |
| `slog3` | Sony S-Log3 | Sony's S-Log3 technical summary | 0.4106 (10 bit code 420) | S-Gamut3.Cine |
| `logc3` | ARRI LogC3 at EI 800 | ARRI's Log C curve usage document | 0.3910 (10 bit code 400) | ARRI Wide Gamut 3 |
| `vlog` | Panasonic V-Log | the V-Log/V-Gamut reference manual | 0.4233 (42.3 IRE) | V-Gamut |
| `clog3` | Canon Log 3 | Canon's Canon Log gamma curves white paper | 0.3280 (32.8 IRE) | Cinema Gamut |
| `dlog` | DJI D-Log | DJI's D-Log white paper | 0.3988 | D-Gamut |

Two things to know about them.

**`auto` never picks one.** No container tag can tell S-Log3 from LogC3 from
V-Log: all five come off the camera looking exactly like an Apple Log file
(BT.2020 primaries, no transfer tag), so guessing would be guessing between
five different pictures. Name the one the camera actually shot, in the Input
select, in `convert.input`, or with `--input-space` on the CLI (below).

**The gamut comes with the curve.** There is no code point in any container for
S-Gamut3.Cine or ARRI Wide Gamut 3 or the other three, so those files are
tagged `bt2020` because that is the nearest label available, not because it is
true. The engine does not read that tag for these five: naming the curve names
the gamut, and there is exactly one technical cube set per camera log
(`SLog3_to_DWG.cube` and so on, with no primaries infix, since reading the tag
would matrix the file as though it were plain BT.2020 and quietly desaturate
it).

One caveat that is about the file rather than the curve: the engine normalises
a source to full scale using the file's own `color_range` tag before any curve
runs, exactly like HLG, PQ and Rec.709 already do. A camera log file that is
tagged legal range while actually carrying data levels (several camera formats
do) decodes a fraction of a stop off. Fix the tag on the file; the curve is not
the thing to adjust.

### Choosing the input from the CLI

    cinegrade still IN.MOV -o out.png --input-space vlog

`--input-space`, not `--input`: every subcommand already has an `input`
positional for the clip itself. It is on `render`, `still`, `compare`,
`scopes`, `stats`, `orient` and `sweep`, takes any `convert.input` value
(`auto`, `apple_log`, `hlg`, `pq`, `rec709`, `slog3`, `logc3`, `vlog`, `clog3`,
`dlog`), and overrides whatever a `--preset` carries.

`cinegrade orient IN --json` also prints what the file resolves to (`transfer`,
`primaries`, `resolved_input`, `source_primaries`, `warnings`) next to the
rotation tag, honouring `--input-space`, so the one command an agent runs
before it knows anything about a file answers both questions at once (see
"Rotation" below for the rest of that command's output).

### Generating the technical cubes

    .venv/bin/python grade/tools/make_cst.py --batch --size 65 \
        --out-dir grade/luts/technical
    .venv/bin/python grade/tools/make_cst.py --anchors

The first writes every non Apple Log cube, 90 files (HLG, PQ and Rec.709 on
both gamuts, plus the five camera logs, times the tonemap and encode
combinations each needs). Apple Log's own cubes are not touched and stay byte
identical. The second prints 18 percent grey in each input's own code values,
including the five camera logs with the document each number came from, listed
above under "The standards behind each decode" and "Camera logs".

This is also the FX only round trip described in `grade/FREE-RESOLVE.md`: grade
somewhere else, export Rec.709, bring it back here for halation, bloom, grain and
a vignette without touching the colour.

### The GPU live preview and the file's tags

Fixed everywhere: the clip's resolved input (`clips[].source.resolved_input`,
the same answer the render uses) is handed to every GPU path, still, loop and
proxy playback alike, so a preview asks for the cube the server would,
including on `auto` for HLG, PQ, or a named camera log. Nothing changes for an
Apple Log clip. Server renders, CLI renders and stills were always correct
regardless; the live GPU preview now agrees with them in every mode, not only
the still one.

## Rotation

`"rotation"` is a top level config key, alongside `convert` and `primaries`, not
only a request field: one of `"auto"` (default, honour the file's own display
matrix tag, today's behaviour), `"0"`, `"90"`, `"180"`, `"270"` (ignore the tag
and turn the picture that many degrees clockwise regardless of what it says).
Because it lives in the config, it is part of a saved grade or preset: `PUT
/api/grade` and `POST /api/preset` persist it, and loading either back applies
it, the same way loading a preset replaces the rest of the config ("Presets: the
comment rule and the Load rule" below). Loading a preset or grade that carries a
real value moves the rotation control to match it; one that carries `"auto"` (or
predates this feature, filled in as `"auto"` by the defaults merge) leaves the
control exactly where it was, since `"auto"` means "let the project or the
file's tag keep deciding", not "force zero". It is also stripped from a saved
preset exactly like every other untouched default (presets are diffs against
the engine's defaults), so a preset nobody ever set a real rotation on stays
silent about it, and a preset that did carry a real one survives the round trip.

Resolution order, most specific wins: an explicit `rotation` field on the
request itself, then a non-auto `rotation` sitting inside the request's own
`config`, then the open project's stored rotation, then the file's own display
matrix tag. This is one level deeper than "Playback" above describes for the
proxy and scrub routes (request field, then legacy `autorotate` boolean, then
the project): the config-level value now sits between the request field and the
project, everywhere a route accepts a `config`. On the CLI, `--rotate` beats
everything including the config; `--no-autorotate` forces `"0"` and also beats
the config; with neither flag, `render`, `still`, `stats`, `scopes`, `compare`
and `orient` all honour a non-auto `rotation` sitting in the preset or config
they were given.

`cinegrade orient IN.MOV --json` skips rendering its usual contact sheet and
prints one JSON object instead: `{"tag": ..., "candidates": {"0": {"width":
W, "height": H}, "90": {...}, "180": {...}, "270": {...}}, "rotation_tag_suspect":
bool, "rotation_tag_note": "..."}`. `"tag"` is the file's own raw display matrix
value exactly as ffprobe reports it (it can be negative, e.g. `-90`).
`rotation_tag_suspect` is advisory only and NOTHING ever applies a rotation
because of it: it flags a tag that looks like it does not belong on this file,
for either of two reasons: the codec is a professional or cinema one (ProRes,
DNxHD/HR, CineForm, R3D, BRAW, ARRIRAW, CinemaDNG) carrying any quarter turn
tag, since those tools do not normally write one for a phone-style reason; or
the file's own raw (pre rotation) coded dimensions are already landscape while
the tag asks for a turn into portrait. False positives are expected and fine
(it is advisory); false negatives are not meant to happen on real footage.
`GET /api/state` exposes the same numbers on every clip as `rotation_tag` (a
string, `"0"` when the file carries none), `rotation_tag_suspect` (bool) and
`rotation_tag_note` (string), computed by the exact same functions `orient`
uses, so the CLI and the studio UI can never disagree about one clip.

`rotation_tag_note` (round 2 tooling item 17; the field name stays
`rotation_tag_suspect`, unchanged) is one plain sentence saying WHY
`rotation_tag_suspect` fired, naming the tell that caught it (the cinema
codec, or the already-portrait coded frame), so the boolean alone never has
to be read as a verdict either way. It is `""` when `rotation_tag_suspect` is
`False`, and that empty string is NOT a reassurance that the tag is correct:
the two tells above are deliberately generous rather than exhaustive (a false
positive is fine, a silent false negative on a genuinely sideways file is the
failure they exist to avoid), so an empty note only means neither tell fired
this time, not that the tag was checked and found good.

On a camera that always writes a quarter turn tag on a professional codec
(every clip shot on the ProRes fixtures this repo ships with, for example),
`rotation_tag_suspect` is `true` for every single clip from that camera, not
only the ones that are actually sideways: each of those calls is individually
correct given the two reasons above, but on a camera like that the flag is the
normal answer, not an exceptional one. Read it as "run `orient` and look at
the frame before trusting the tag", never as "something is broken here".

`cinegrade orient IN.MOV --sheet OUT.jpg` writes a labelled 2x2 of the four
fixed candidates (0, 90, 180, 270; not `auto`, the same four `--json`'s own
`candidates` already names), through the same contact-sheet code `cinegrade
sheet` uses, so "always look" (see the studio-grading skill) is one command
instead of decoding `--json`'s numbers by hand or reading the default hand
drawn row sheet. `--sheet` works independently of `--json`: with both given,
both are written, and the JSON dict gains a `"sheet"` key naming the path.

## Per clip grades

Every clip has its own grade per account (contract C3, `studio/grades.py`).
Switching clips no longer carries the previous clip's look across silently, and
two accounts working from the same `footage/` folder never see each other's
changes.

Since "Projects and history" below (contract C1), this table is the MIGRATION
SOURCE for a clip's project, not the live grade any more. The first time
anybody opens a clip, its most recently saved row here becomes the project's
root commit; from then on the saved grade IS the project's HEAD, and every
save is a commit. Nothing here is rewritten or deleted by that: `PUT
/api/grade` still writes this row (so `GET /api/grades` and the copy picker
below keep working) as well as recording a commit, `GET /api/grade` answers
from the project's HEAD once one exists and falls back to this table only for
a clip nobody has opened yet, and `DELETE /api/grade` now commits a reset to
the engine defaults instead of removing a row, so a shared project's history
is never destroyed by one account clearing its grade.

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
a tracked file. Your own saved presets go to `<data-dir>/users/<id>/presets/`
instead (`studio/data/users/0/presets/` with no `--data-dir` and logins off,
which is the ordinary local case: id `0` is shared by the browser tab and every
agent alike, contract G2, so an agent's "save as" shows up where the person at
the keyboard will see it and the other way round), gitignored the same way the
accounts database is. Overwriting a library preset writes a user copy that
shadows it rather than editing the shared file, which is the only sane meaning
of Overwrite on a file every other account is also reading.

A preset file on disk is not a full config: it stores only the branches that
differ from the engine's own defaults (`DEFAULTS` in `grade/cinegrade.py`), the
same diff `config_diff` computes everywhere else. A preset that only touches
`primaries.saturation` is a one-line JSON file, not a dump of every parameter
the engine has; reading the file back and expecting the whole config to be
sitting there is a naive equality check that fails for a reason that has
nothing to do with the save being broken. `GET /api/preset?name=NAME` always
answers with the FULLY EXPANDED config regardless (`full_config()` runs on
every read, so a preset saved before a field existed is migrated the same way
a live config is), never the bare diff on disk; add `?expand=true` and the
response also carries `"expanded": true`, a flag, not a second copy of the
config under a different key. The response is `{"name", "comment", "config",
"expanded"}`; `"comment"` is always present now, which is a different thing
from the `_comment`-stripped-on-Load rule below: that rule is about what rides
into the editor's live config; this is the route itself, which used to write
`_comment` on save and silently drop it on read, so a caller confirming its own
save had to read the file on disk instead of trusting the API.

### Presets: the comment rule and the Load rule

A preset's `_comment` describes the FILE, not the grade sitting in the editor
right now. Loading a preset strips `_comment` out of the live config, so it
never rides along into whatever you save next: Save As with a blank comment
field writes no comment at all, and Overwrite with a blank comment field
keeps only that preset's OWN prior comment (read from its file on disk),
never the comment of some other preset you had loaded earlier. Before this,
loading `nature_cinema` and then Save-As-ing a new grade with no typed
comment silently kept describing it as `nature_cinema`, which is what the
founder saw and reported.

Load always replaces the WHOLE config, not just the fields the preset file
happens to set: `layers` and the second look slot (`look.lut2`) included.
The server fills in every field a preset omits with the engine defaults
(`full_config()`) before the page ever sees it, so loading a plain preset
after a layered one clears the layers and the second LUT rather than leaving
them stacked on top.

Routes, one curl example each, run and read back on a real test server:

**`GET /api/grade?clip=NAME`** returns `{exists, key, config, updated_at}`, plus
`head` and `branch` once a project exists for that clip (the config is then its
HEAD's, not this table's row). Verified on a clip with no saved grade: `{"exists":
false, "key": "05fe6fe1e893172ddf254f196a9a121d", "config": null, "updated_at":
null}`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:7431/api/grade?clip=A001.MOV"
```

**`PUT /api/grade {clip, config, by, message}`** returns `{key, updated_at,
head}`: `head` is the short id of the commit this save just made (contract C1).
`by` names who made it (else `cli`) and `message` overrides the generated one
line description; the default message is `saved grade`. Verified: `{"key":
"05fe6fe1e893172ddf254f196a9a121d", "updated_at": 1788614339.734303, "head":
"f054566"}`, and a follow-up `GET` on the same clip came back `{"exists": true,
"head": "f054566", "branch": "main", ...}`.

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

**`DELETE /api/grade?clip=NAME`** returns `{deleted, key, head}`. Once a
project exists this commits a reset to the engine defaults rather than
removing anything (a shared project's history is not one account's to
destroy), so `head` is the short id of that new commit. Verified:
`{"deleted": true, "key": "05fe6fe1e893172ddf254f196a9a121d", "head":
"69faa3f"}`.

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

## Projects and history

Contract C1. "Per clip grades" above answers "what does this clip look like
right now"; a PROJECT answers the four things that one saved row cannot: who
changed it, what it looked like before, whose value a person and an agent
editing at once are each looking at, and (reload the page) put it back exactly
as it was. All four are one missing thing: a history with names on it, git
style, so this section borrows git's words on purpose (commit, branch, HEAD,
fork, checkout) rather than inventing new ones for the same ideas.

A PROJECT is everything the studio knows about one clip: its rotation, its
playhead, the loaded preset's name, a small bag of extras other tools use (a
match reference crop is one, contract C7), and a history of COMMITS on
BRANCHES. It is keyed by the clip's content key (the same 32 hex digest "Per
clip grades" uses), so a rename or a move keeps the same project, and it is
SHARED by every account, founder's call: two people and an agent working on
the same clip see ONE tree, not one each. What is per account is only which
project that account currently has open (opening a clip opens or creates its
project and makes it that account's open one).

A COMMIT is one committed change: the whole config after the change (not a
diff), its parent commit, its branch, an author, a timestamp, the list of
dotted paths that moved with their old and new values, and a readable, auto
generated one line message (`saturation 1.00 to 1.20`, `layer "Sky" added`,
`loaded preset nature_cinema`, `3 changes: contrast, pivot, highlight
rolloff`) that a caller can override by sending its own. "Committed" is
whatever the app already treats as committed: a slider release, a checkbox, a
preset load, a layer add, an outside `POST /api/session` patch. A drag frame
is never a commit. Publishing the identical config twice in a row is a no op,
not an empty commit.

HEAD is the commit whose config is live right now. A BRANCH is a named line
of commits with a tip; every project starts with `main`. Editing from HEAD
when HEAD is already the tip of its branch just extends that branch. Editing
from an older HEAD (after a checkout, or after somebody else moved the tip
without you noticing) automatically FORKS a new branch (`fork-1`, `fork-2`,
...) so the newer work you did not build on is never lost underneath yours.

- **Undo** steps HEAD back to its parent, along the branch you are standing
  on. The branch's tip is left alone, which is what makes **redo** possible:
  it steps HEAD forward again, toward that tip. Both are no ops at either end
  of the line and say so in plain text rather than silently doing nothing.
- **Checkout** moves HEAD to any commit, on any branch. Non destructive:
  nothing is deleted or rewritten, so looking at an old point and coming back
  costs nothing.
- **Fork** explicitly branches off HEAD, or off a given commit, under a new
  name (auto named `fork-N` when none is given). This is the same thing an
  edit from a non tip HEAD does automatically; `fork` is for doing it on
  purpose, before making the edit.

The right sidebar's `History` tab (beside `Grade`, contract C3) draws the
WHOLE tree, every branch, newest first, with `Go here` and `Fork from here` on
every row, and refreshes the moment an outside commit lands so a person
watches an agent's edits arrive with a name on each one. This is the direct
answer to "the agent doesnt need to have anxiety on whos editing what and what
preset the person has selected" (the founder's framing for this arc): there
is one project, one HEAD, one history, and the tab, a second person's tab, and
an agent are three views of the exact same thing, told apart only by the
`author` on each commit they wrote.

### Identity

Every write names an author. A browser tab signs as the account name (or
`studio` with logins off); `grade/cinegrade.py` signs as `--by` if given, else
the `CINEGRADE_AGENT` environment variable, else `cli` (`resolve_by()` in
`cinegrade.py`, used by `session patch` and every `project` command the same
way). Nothing on the server adds an `agent:` prefix for you: an agent that
wants to read as one spells it out itself, `--by agent:colorbot-3` or
`CINEGRADE_AGENT=agent:colorbot-3`, the same convention "The `by` field" below
already uses for `POST /api/session`. `GET /api/whoami` (`{"user", "by",
"auth", "project", "caller"}`) answers what account is signed in, whether
logins are on at all, and which project this account currently has open; with
logins on the account name always wins regardless of what a caller sends,
since a signed in browser cannot sign somebody else's name to a commit by
editing a JSON body. `project` on the route is only the project's own content
key, an unreadable hash on its own; `cinegrade whoami --json` makes one more
call, `GET /api/project`, and adds `"project_clip"` next to it (and a
parenthetical on the plain text `project` line), so seeing which clip a
project key means, or that two agents have different clips open, needs no
second command from the caller's own side.

`by` here is a free text label on ONE commit; it is not the same thing as the
`caller` block next to it, which is the server's own per caller identity with
logins off (contract G2, `X-Studio-Agent`/`X-Studio-Attach`: see "Per caller
identity" under Agent API below). The two compose: setting `--agent NAME` on
the CLI (or the `STUDIO_AGENT` environment variable) also becomes the default
for `--by` when `--by` is not given, so the label a commit carries matches the
identity that made the write without having to say both.

One rule matters more than the others: **the edit path is `POST
/api/session`, never `PUT /api/grade`.** `PUT /api/grade` still works and
still records a commit ("Per clip grades" above), but it deliberately does
NOT bump the live revision, so a browser tab open on that clip is not handed
back its own autosave as if it were somebody else's edit. An agent that wants
its change to appear live, the instant it lands, the way a person's slider
does, patches through `POST /api/session` exactly as "The routes an agent
actually needs" below describes; `PUT /api/grade` is for a save that nobody
needs to watch happen.

### The routes

All plain JSON, all under the same auth and CSRF rules as `/api/session`
("Getting a token" below).

| Route | Body | Answers |
| --- | --- | --- |
| `GET /api/project` | (`?clip=` optional, else the open one) | the open project: key, name, path, rotation, time, preset, `head`/`head_full`/`head_commit`, branch, `branches`, `extras`, `config` (HEAD's), `dims` |
| `POST /api/project/open` | `{"clip": NAME}` | opens or creates that clip's project and makes it this account's open one; same body as `GET` |
| `POST /api/project/rotation` | `{"rotation": "auto"\|"0"\|"90"\|"180"\|"270"}` | sets the project's rotation (a field, not a commit) |
| `POST /api/project/time` | `{"time": SECONDS}` | sets the playhead (a field, not a commit; no revision bump) |
| `POST /api/project/checkout` | `{"commit": ID}` (full or a unique short prefix) | moves HEAD, non destructive |
| `POST /api/project/fork` | `{"commit": optional, "name": optional}` | branches off HEAD or `commit`, under `name` or an auto `fork-N` |
| `POST /api/project/undo` / `.../redo` | `{}` | steps HEAD; the answer carries `moved` (bool) and, at either end, a `note` explaining why nothing moved |
| `POST /api/project/extra` | `{"name": ..., "value": ...}` | a small JSON bag of per project settings other tools use, read back under `extras` |
| `GET /api/project/log` | `?limit=N` (default 200, max 2000) | `{key, name, head, branch, branches[{name, tip, short, commits, is_current}], commits[{id, short, parent, branch, author, ts, message, changes[{path, label, old, new}], is_head, is_tip}], total, limit}`, the WHOLE tree, every branch, newest first |
| `GET /api/whoami` | none | `{user, by, auth, project}` |

`extras` is a small per project bag other tools read and write through
`POST /api/project/extra`; today's only entry is `match_crops` (contract C7,
Match Reference above), keyed by reference name with an optional `ref` and an
optional `frame` rectangle each: `POST /api/match` uses `ref_crop`/
`frame_crop` from the request when given, falls back to the matching
rectangle saved here when either is ABSENT from the request, and an explicit
`null` means the whole image even when one is saved; `grade/tools/match_ref.py`
takes the same two rectangles on the command line as `--ref-crop X0 Y0 X1 Y1`
and `--frame-crop X0 Y0 X1 Y1`.

Every write above (`open`, `rotation`, `checkout`, `fork`, `undo`, `redo`)
takes an optional `"by"` the same way `POST /api/session` does, and wakes any
browser tab long polling `GET /api/session/wait` the same instant a session
patch does, since all of them move what that tab has to show.

Verified against a real server on a random port with a temporary `--data-dir`
(the same server `grade/tests/cases_cli_project.py` starts and stops by its
own captured process id): opening a clip returns a root commit on `main` at
`auto` rotation; a `session patch` with `"by": "lane"` and `"message": "warm
it"` shows up in the very next `GET /api/project/log` as `lane ... warm it`;
checking out that root commit and patching again creates `fork-1` and the
log marks the root as the point it forked from; undo returns to the root
(`moved: true`), a second undo says `already at the first commit of this
project` and reports `moved: false`.

### The CLI

`grade/cinegrade.py` has the same routes as commands, so a shell script needs
no HTTP client. Every one of them takes `--port`/`--url` (env `STUDIO_PORT`/
`STUDIO_URL`; port 7431 with none of the four), `--by`, and `--json` (the
server's raw JSON instead of a table); `session patch` also takes `--by`,
`--message` and `--if-rev N` (the optimistic revision check, see "`session`"
in the API table above), sent as `by`, `message` and `if_rev` on the wire.
`session`, `whoami`, every `project` subcommand, and `match`/`preset`/`grade`
also take `--agent NAME` (env `STUDIO_AGENT`) and `--attach USER`, so this
shell gets its own live session, project and saved crops instead of sharing
the browser tab's; naming an agent without naming a server (`--port`,
`--url`, or either environment variable) is refused rather than silently
defaulting to 7431. See "Per caller identity" under Agent API below for the
full shape of that.

```
cinegrade.py whoami
cinegrade.py project open CLIP [--rotation auto|0|90|180|270]
cinegrade.py project show
cinegrade.py project log [--limit N] [--all]
cinegrade.py project checkout ID
cinegrade.py project fork [NAME] [--from ID]
cinegrade.py project undo
cinegrade.py project redo
cinegrade.py project rotate auto|0|90|180|270
cinegrade.py project time SECONDS
```

A real transcript against a running server, in order (identity, then a
history):

```
$ cinegrade.py whoami
user     (no account, logins are off)
by       cli
logins   off
project  (none open)

$ CINEGRADE_AGENT=agent:colorbot-3 cinegrade.py whoami
by       agent:colorbot-3

$ cinegrade.py project open A001_09011336_C002.MOV --by studio
opened A001_09011336_C002.MOV
key       05fe6fe1e893172ddf254f196a9a121d
branch    main
head      eea98e8
rotation  auto

$ cinegrade.py session patch '{"primaries": {"saturation": 1.2}}' \
    --by lane --message "warm it"
{"config": {...}, "by": "lane", "head": "ea22e17", "branch": "main", ...}

$ cinegrade.py project log
project 05fe6fe1e893172ddf254f196a9a121d  A001_09011336_C002.MOV
branches  main (tip ea22e17, 2 commits, current)

ea22e17  main       lane               just now       HEAD  warm it
eea98e8  main       server             just now             root

2 commits total
```

`whoami`'s `by` line is the CLI's own answer, not the server's: the route has
no write to attach one to, so it always reports its quiet default (`cli`);
what actually signs the next commit is `resolve_by()` above, which is what
`whoami` shows instead. `--json` on any of these prints the server's answer
verbatim, unmodified, for a script that wants to parse it rather than read it.

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
- **Key** (`mask.key`), the HSL qualifier: its own `enabled` (bool, default
  `false`: a layer with a key left off, same as one with no window, keys
  nothing out and grades the whole frame), `hue_center`/`hue_width`/
  `hue_soft` (degrees), `sat_low`/`sat_high`/`sat_soft` and `lum_low`/
  `lum_high`/`lum_soft` (0 to 1), and its own `invert`. The shipped default
  (`hue_center: 30, hue_width: 40, hue_soft: 15`) keys only a band of warm
  hues around orange: turned on with nothing else changed, a key silently
  restricts a lum/sat only correction to that band instead of the whole
  frame. `hue_width` is the covered arc's full width in degrees against a
  hue distance that is capped at 180 (the far side of the wheel from
  `hue_center`), so a lum/sat only key with NO hue restriction at all needs
  `hue_width: 360` (`hue_soft` then stops mattering: at the arc's own edge,
  180 degrees out, the ratio that drives the falloff is exactly 1
  regardless of softness), not 180, which still only covers one side of the
  wheel.

`mask.invert` inverts the COMBINED matte (window times key) as a whole, after
both halves are computed, rather than either half on its own. With neither
window nor key on, the matte is open everywhere and the layer is a global
grade. All geometry is a fraction of the frame, never a pixel count, which is
what lets a 960 wide preview and a 3840 wide render agree on the same shape
with no special case in the preview scaler.

`mask.show` stores "show this layer's matte" in the preset; for a quick look
without touching the config, use the Matte button (`#maskBtn`) over the
viewer instead, which shows the SELECTED layer's mask, not merely the first
enabled one. An outside caller with no "selected layer" to speak of asks
`POST /api/frame` for `"mode": "mask"` and names the layer with `"mask_layer"`
(its index in `layers`; absent is the first enabled one), see "mask_layer,
and reading a file directly" above.

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

### The mask panel: the component stack, SAM picks and tracking

Every layer's body opens with a **Mask stack** panel
(`studio/static/masks.js`). It is the whole user side of contract C1's
`mask.components`: the list of components that make the layer's matte, the
SAM 3.1 selections that get tracked through the clip, and the two ways of
looking at the result. The legacy Window and Key folds below it stay, and
their summaries say `not in use, the mask stack replaces it` whenever the
stack is not empty, because that is exactly what both engines do with them.

Every number the panel writes goes out through `Layers.emit`, the same per
layer write a slider in the Window group makes, so undo, the per clip
autosave, the session publish and the re-render cover a mask edit the way
they cover a slider drag. Nothing in this panel is a second definition of the
mask: the matte itself is rendered by `grade/cinegrade.py` for a real render
and by `gpu.js` for the preview.

**The stack.** One row per component, folded top to bottom from 0. Each row
has a thumbnail (the matte at the playhead, or a sketch of the shape), an
editable name, the type, the op (`add` takes the larger of the two,
`intersect` multiplies them, `subtract` takes this one away), `invert` (which
flips THIS component before it combines, not the finished matte), `enabled`,
`feather` (a gaussian on this component alone, as a fraction of frame width,
capped at 0.10 of the frame width: the slider stops there because all three
engines clamp there, and a grade saved before the cap existed displays its
feather clamped rather than showing a number the picture was not made with),
move up, move down and delete. Reorder and delete write the whole array in
one go, so each is one undo step.

**The add menu** offers, in order: Select subject, Sky, Background; People,
Face, Hair, Lips, Eyes, Teeth, Clothes; Object (click or box on the picture),
Text phrase; then Colour range, Luminance range, Linear gradient, Radial
gradient and Rectangle, which are the keys and windows the studio already had,
written as components so they combine with the SAM ones through the same
three ops. Everything in the first two groups is a text prompt, so choosing
one queues its track straight away: the model is slow and the useful thing to
do with a named selection is to get it running while the grade happens.

**Picking on the picture.** Object (click or box) starts in `needs_pick` and
puts the viewer into pick mode: a click adds a positive point, an alt click
(or a right click) a negative one, and a drag makes a box. `Find` sends them
to `POST /api/mask/segment` and the instances come back as chips showing the
server's own tinted overlay of each one; clicking a chip tracks that
instance, `use all N` tracks every one of them and each becomes its own
component with op `add`, so they can then be feathered or subtracted
separately. Nothing about the layer is touched while picking, so leaving pick
mode puts back exactly the window that was on screen before it.

**Tracking.** `Track` starts a background job. Nothing in the grading loop
waits on it: the row shows done of total, the MEASURED rate (`done_frames /
elapsed_s`, not the clip's fps, which on the real model are 0.06 and 24 and
must never be confused), how long is left at that rate, the service's own
window and resident memory from `/api/mask/status`, and a `Cancel`. While the
matte is not finished the badge reads **static until tracked**: the engines
hold the nearest written frame, so the correction still works, it just stops
moving past the tip of the track. A cancelled track leaves a partial matte and
the row says how far it got (`tracked to 26 of 120 frames, held past there`).
When a job finishes, the matte id on the component has not changed, so the
layer swaps to the tracked matte with no click. `steady` is the temporal
smoothing width on the track, centred on the frame so the matte does not lag
the picture; 1 is off.

**The states the panel shows**, all from the server, never guessed:
`queued`, `running`, `done`, `failed` (with the service's own reason on the
row), `partial`, `stale`, `needs_pick`, and the service being down at all. A
matte is stale when it was tracked at a different rotation or for a different
clip, and the row says which; the fix is `Track again`. "auto" is not a
rotation but "honour the file's own tag", so it is resolved to that tag before
the two are compared: clicking the rotation a clip was already tagged with
changes no pixels and does not make anything stale. With the service
down, the panel says `mask service not running` and shows the start command
carried by the 503 itself (so the instruction cannot drift from the code that
prints it), with a `Retry`; `Track` and `Find` go dead and everything already
tracked keeps working, because a matte on disk needs no service.

**Finesse** (`mask.finesse`, on the COMBINED matte, in the order clean, then
grow, then blur): blur, grow (negative shrinks), clean black, clean white.
Blur carries the same 0.10 of frame width cap feather does, and its slider
stops there for the same reason. Feather is per component and lives in its own
row, because that is where C1 puts it.

**Display modes**, three buttons at the top of the panel:

- **Off**: nothing over the picture.
- **Overlay**: `#maskOverlay`, a canvas over the picture tinted with the
  theme accent where the layer's tracked mattes are open. It follows the
  playhead on its own animation frame loop, so it moves with the subject
  under BOTH playback engines (the server `<video>` and the GPU proxy). It
  decodes each matte frame once, caches 48 of them and reads 8 ahead along the
  play direction; a frame that has not arrived reuses the last one and writes
  `matte lagging` into the status line next to the Matte button rather than
  stalling the picture. It draws the union of the ENABLED matte components
  only, so with keys, shapes, ops other than add, or finesse in the stack it
  is the quick answer and the panel says so.
- **Matte**: the black and white matte itself. This writes the layer's own
  `mask.show`, which both engines already render, so it is the EXACT combined
  matte and it plays in sync with the picture with nothing drawn on top.

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

Each curve's own list (`hue_hue`, `hue_sat`, `hue_lum`, `lum_sat`,
`sat_sat`) is a plain `[x, y]` pair list, unsorted input allowed (sorted and
de-duplicated by x on read). X is 0 to 1 for all five, but what 0 to 1 MEANS
differs by curve, read from source (`CURVE_AXES` in `grade/slice.py`): for
the three hue-wheel curves it is the pixel's own hue divided by 360 (0 and 1
are both red, which is why those three wrap); for Lum vs Sat it is Rec.709
luma; for Sat vs Sat it is the pixel's own HSV saturation, `(max - min) /
max`. Y is turns of hue offset for Hue vs Hue, a multiplier around the 1.0
neutral for the other four, exactly as stated above.

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

For a pale colour such as a hazy sky, reach for a layer's `mask.key` instead
of a slice vector: a layer's `correct` is a per channel gain, applied flat
regardless of how saturated the pixel already is, where a slice vector's
weight (above) is scaled by the pixel's own saturation and so barely moves a
pale pixel at all (measured under "Which way is which" below).

## Which way is which

Positive and negative are easy to get backwards from reading a formula, so
every row below was set on one control at a time, on top of an otherwise
default config, and MEASURED through a real render
(`cinegrade still content/footage/A001_09011336_C002.MOV --time 2 --width 320
-o OUT.png`, then `cinegrade stats --image OUT.png --json`), not read off the
code and trusted:

| Control | Positive means | Measured |
| --- | --- | --- |
| `primaries.temperature` | warmer: more red, less blue | `--temperature 0.3`: mean R 0.393 to 0.472, mean B 0.356 to 0.303 |
| `primaries.tint` | greener: more green relative to red and blue | `--tint 0.3`: mean G 0.376 to 0.430, R and B nearly unmoved |
| `slice.vectors.<name>.hue` | rotates that hue family up the wheel, toward the next hue (red toward yellow/green) | red vector `hue: 15`: mean G 0.3757 to 0.3773, luma and saturation unchanged (a pure hue move, no brightness or vividness change) |
| `slice.vectors.<name>.density`, and the global `slice.density` | darkens that hue family (or the whole frame, for the global control); negative brightens, already stated above in "Hue curves, Color Slice and Tetra" | red vector `density: 0.5`: mean luma 0.3780 to 0.3752, every channel down |
| `hue_curves.hue_sat` (and, by the same code declared convention, `hue_lum`/`lum_sat`/`sat_sat`: all four are multipliers around a neutral of 1.0, `CURVE_AXES` in `grade/slice.py`) | a y value above 1.0 raises saturation (or luminance); below 1.0 lowers it | one point at `y: 1.6`: mean saturation 0.1843 to 0.2949 |
| `layers.N.correct.exposure` | brighter, the same stops unit `convert.exposure` uses | `exposure: 1.0`: mean luma 0.3780 to 0.5029 |
| `layers.N.correct.temperature`/`tint` | the same sign as `primaries.temperature`/`tint` above | not a second render: `layer_lut`'s own R/G/B gains are `exposure + temperature`, `exposure + tint`, `exposure - temperature`, the identical formula run on the display side instead of the working space, confirmed by reading `grade/cinegrade.py`'s `layer_lut`; see "Correct" under Layers for its own range table |
| `primaries.black_lift` | raises the shadow floor (a toe lift) and pushes saturation down as it lifts | `black_lift: 0.1`: p5 0.0978 to 0.1160, p50 0.4201 to 0.4190, p95 0.6618 to 0.6617, sat mean 0.1843 to 0.1716; `black_lift: 0.2`: p5 to 0.1580, p50 to 0.4167, p95 to 0.6613, sat mean to 0.1502 (midtones and highlights barely move; the toe is exactly what moves) |
| `primaries.highlight_rolloff` | compresses the highlights down (a shoulder), leaves the shadow floor untouched | `highlight_rolloff: 0.15`: p5 unmoved at 0.0978, p50 0.4201 to 0.4190, p95 0.6618 to 0.6543, sat mean 0.1843 to 0.1826; `highlight_rolloff: 0.35`: p5 still 0.0978, p95 to 0.6343, sat mean to 0.1781 |
| `curves.master` | a point ABOVE the identity diagonal raises luma at that x and lowers saturation; a point BELOW it lowers luma and raises saturation | one point at `[0.5, 0.65]` (above): p5 0.0978 to 0.1519, p50 0.4201 to 0.5711, p95 0.6618 to 0.7898, sat mean 0.1843 to 0.1549; the mirror point `[0.5, 0.35]` (below): p5 to 0.0462, p50 to 0.2789, p95 to 0.5212, sat mean to 0.2277 |

`hue_curves.hue_hue` (an additive offset in turns, not a multiplier: see
`CURVE_AXES` again) rotates hue the same direction Color Slice's own `hue`
does, by the same code declared convention as the measured row above; it was
not separately re-rendered, since it is the same rotation on a different
curve editor.

The three new rows above were MEASURED the same way as the rest of the
table, on top of an otherwise default config, `black_lift` and
`highlight_rolloff` having no dedicated CLI flag: a one line JSON file
(e.g. `{"primaries": {"black_lift": 0.1}}`, or `{"curves": {"enabled":
true, "master": [[0.0,0.0],[0.5,0.65],[1.0,1.0]]}}`) handed to `--preset`
(a literal path works, see "CLI reference" below), then `cinegrade still
content/footage/A001_09011336_C002.MOV --time 2 --width 320 --preset
FILE.json -o OUT.png`, then `cinegrade stats --image OUT.png --input-space
rec709 --working-space rec709 --json`. State `--input-space rec709 --working-space rec709` explicitly whenever a
JPEG or PNG goes to `--image`: both are already display referred Rec.709
code, so the flags change nothing here, but naming them costs nothing and
holds regardless of what a still with no flags at all currently resolves to.

### Color Slice's saturation falloff

A vector's hue weight carries the pixel's own saturation (stated above), so
the same requested rotation lands harder on a vivid pixel than a pale one.
MEASURED: three synthetic 64x64 patches, hue 240 (Color Slice's own `blue`
centre) and HSV value 0.75, at source saturation 0.1, 0.3 and 0.6, built
with `ffmpeg -f lavfi -i color=c=0x...:s=64x64:d=1` encoded to a one frame
ProRes `.mov` tagged `bt709`, run through `cinegrade still PATCH.mov
--input-space rec709 --working-space rec709 --preset FILE.json -o OUT.png`
(`FILE.json` setting `slice.enabled: true` and `slice.vectors.blue.hue:
-55`), against the same patch with no slice at all, hue read off the mean
pixel of each PNG with `colorsys.rgb_to_hsv`:

| Source saturation | Delivered rotation | Fraction of the -55 requested |
| --- | --- | --- |
| 0.1 | -3.3 degrees | 6% |
| 0.3 | -17.3 degrees | 31% |
| 0.6 | -33.4 degrees | 61% |

A pale sky sits at the low end of this table: a vector's -55 lands as a few
degrees, not 55, for the same reason the falloff above exists at all.

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

`detail.soften` is a raw gblur sigma IN PIXELS (`f_detail` in
`grade/cinegrade.py` passes it straight to ffmpeg's `gblur=sigma=`, no
frame-width scaling the way `mid_detail`'s sigma below gets), so its unit
depends on the width of whatever is being rendered. `scale_for_preview` in
`studio/server.py` divides a preview render's copy of it by
`source_width / preview_width` (multiplies by the inverse, `preview_width /
source_width`) so a small preview shows the same relative softness a full
render would, the same treatment halation and bloom's sigmas get; at the
640 wide default preview against 3840 wide source footage that factor is
about 1/6, so a `soften` of 0.15 (visibly soft on the full render) becomes
a sigma near 0.025 in the preview panel, too small to see: the same number
reads sharp in the preview and soft in the delivered file.

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

Both slots' `lut` value (round 2 tooling item 8) resolves in this order,
stopping at the first hit: an absolute path, or one already valid relative
to the current directory (unchanged; a browser-launched look never sends
one of these, only a bare name); then a path relative to `content/` (the
repo checkout), so a caller does not have to `cd` into `content/` or spell
an absolute path to point at a cube living elsewhere in the checkout; then
a bare name (with or without `.cube`) looked up in `grade/luts/looks/`, the
folder the browser's own dropdown is built from. Nothing not found at any
of those three stops is an error naming the value that failed and the
folder and the bare names actually available there, not a generic "file not
found."

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

## The timeline

One transport row and one ruler (`studio/static/timeline.js`). It replaced a
native `<input type="range">` with 1000 steps, a "secs" text box for the play
length, a second row of from/to text boxes for the GPU loop, a horizontally
scrolling filmstrip and a wrapping row of mark chips.

The ruler (`#scrub`) is one pointer surface. A press anywhere in it puts the
playhead there and a drag follows the pointer until it is released, on mouse,
trackpad and touch alike (pointer capture, so leaving the element or the
window does not drop the gesture). Every position is snapped to a frame,
because the still path addresses frames and a playhead between two of them
makes the label and the picture disagree. The proxy answers each move in the
time of a seek, and the exact 16-bit render lands on its own debounce once the
pointer stops, which is why a drag feels like a player rather than a slide
show. Three lanes, top to bottom:

- **Ticks.** The step is chosen from a ladder of round numbers (0.1s up to
  10 minutes) so a label always has about 62px to itself, and the ruler is
  rebuilt only when that choice or the clip changes, off a `ResizeObserver`.
- **Filmstrip.** Sixteen tiles from `GET /api/thumb`, tile `i` covering
  exactly the i-th sixteenth of the clip, so a picture always sits under the
  moment it belongs to. It is not a pointer target: the whole ruler is one
  scrub surface and a strip that swallowed the press would put a dead stripe
  across the middle of it.
- **Loop range.** A band with a draggable out handle. Drag in the empty lane
  to draw a range, drag the handle to lengthen it, drag it back past the start
  to loop the whole clip again. The Range button does it from the playhead to
  the next mark, and clears it when pressed again.

Hovering the ruler shows the time under the pointer in a small bubble, so a
press lands where it was aimed rather than where it turned out to be.

Marks are flags on the ruler at the time they mark: click one to jump to it,
or the small circle above it to remove it. `S.marks` in `app.js` is still the
only copy and the contact sheet still reads it unchanged.

Nothing here owns state. The playhead is still `S.time` and moves only through
`app.js`'s `setTime`. The loop range's in point IS the playhead, because that
is what the engine has always done (`playLoopRange`: the whole clip when no
length is set, otherwise the playhead plus that length), so the band is drawn
hanging off the playhead line rather than floating free. The length itself is
still the value of `#playDur`, still saved as `extras.play_secs` on the
project, and the GPU loop still reads `#loopStart`/`#loopEnd`; those three are
hidden inputs now and the ruler is the only thing that writes them, through
the same `change` event a person leaving the old text box fired. No route,
payload or persisted key changed.

Keys (the full list is also in the Keys panel, `?`):

| Key | What it does |
| --- | --- |
| `space`, `p` | play or pause from the playhead, looping the range |
| `j` `k` `l` | shuttle back, stop, shuttle forward. Press `j` or `l` again for 2x, 4x, 8x |
| `,` `.` | one frame back or forward |
| `shift ,` `shift .` | ten frames |
| `home` `end` | first or last frame |
| `m` | mark this frame |
| `[` `]` | previous or next mark |
| `shift L` | GPU loop of the range (mode 2) |
| arrows, `shift` arrows | one or ten frames, with the ruler focused |
| `page up` `page down` | one second forward or back, with the ruler focused |
| wheel over the ruler | one frame per notch, ten with `shift` |

Forward at 1x is the real playback engine; every other shuttle rate is one
proxy seek per animation frame, because a `<video>` cannot play backwards and
the proxy answers a seek far faster than a decode.

Three older bindings moved to make room, and each one kept its letter under
`shift`: `shift J` is the raw JSON view, `shift K` the selected layer's mask,
`shift L` the GPU loop. `v` (held) is the "see the ungraded frame" peek that
used to be on the space bar.

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

The proxy is encoded already turned to the project's rotation, not always the
file's own tag: the cache key under `studio/cache/proxy/` carries the rotation
string, so there is one proxy file per clip per rotation, and switching a
project's rotation builds a fresh one rather than replaying the wrong turn.
This and every other route in this section (`play/prepare`, `thumb`, the
`range`/`limit` scrub reads) read rotation the same way `frame` and every
render path do (contract C2, see "Render engines" and `grade/README.md`
`--rotate`): a request may send `"rotation": "auto"|"0"|"90"|"180"|"270"` (or
`?rotation=` on a GET); when that is absent a non-auto `rotation` inside the
request's own `config` wins next (contract G4, see "Rotation" above); when
that is also absent the legacy `"autorotate": true|false` is still honoured
(`true` means `auto`, `false` means `0`); only when none of those three are
sent does the server fall back to the open project's own rotation. A caller
that keeps sending `autorotate` therefore pins itself to `auto` or `0`
forever, because a boolean cannot say `90`, `180` or `270`: it will never see
the project's rotation take effect. `studio/static/live.js` and the CLI's
`project rotate` both send `rotation` and never `autorotate`.

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

Since the library arc the upload also takes `?root=&path=`, which puts the file
in a FOLDER of a library instead of at its top: `POST /api/upload?root=mine&path=Shoot`.
An editor on somebody else's shared folder can upload into it with
`?root=user:<id>&path=Shoot`, and the file belongs to that folder's owner, not to
the person who uploaded it. With neither parameter the behaviour is exactly what
it was before, so nothing that already worked had to change. One thing did
change: the quota is now measured over the whole library tree rather than one
flat folder, because with folders in the picture a flat sum would be walked
around by making a subfolder.

## Library and sharing

The library is the file side of the studio: folders and clips per account,
shared to people and teams the way a shared drive is. It is all in
`studio/library.py`; the routes are thin and do no path or permission work of
their own.

### The model, in four words

- **Library.** One account's own tree of files on this server. With logins off
  there is one account (id 0) and its library is `content/footage/`, exactly
  where the files already are. With logins on an account's library is
  `studio/data/users/<id>/footage/`, exactly where uploads already landed.
  Nothing moved when this arc shipped.
- **Org.** The tenant. Every account is in exactly one (`users.org_id`, default
  org 1 named `default`). Shares, teams, the people picker and the activity feed
  all stop at the org boundary, so two orgs on one server never see each other.
- **Team.** A named group inside an org. Sharing to a team shares to everyone in
  it, including whoever joins later.
- **Share.** A grant on one folder or one file in an owner's library, to a user
  or a team in the same org, as `viewer` or `editor`. A grant on a folder covers
  everything under it. Only the owner grants, and only on their own paths: there
  is no re-sharing of something you were given, which is what keeps "who can
  reach this file" answerable by looking at one owner's rows.

Roles mean exactly this:

| | viewer | editor | owner |
| --- | --- | --- | --- |
| open, view, scrub, read the history | yes | yes | yes |
| commit a grade (session write, save, checkout, undo, redo, fork, rotation, extras) | **no** | yes | yes |
| upload into the folder, rename and move inside it | no | yes | yes |
| share it, trash it | no | no | yes |

A viewer who tries to commit gets a 403 with one sentence:
`viewer access: ask the owner for edit rights`.

**With logins off every one of these checks passes.** There is one account, one
org and one library, so there is nobody to check against, and the founder's local
use is byte for byte what it was: one early return in `library.best_role()` and
one in `library.guard_edit()`, not a permission system sprinkled through the code.

### Why a shared clip shows up in the same history

A project is keyed by the clip's CONTENT (contract C3's clip key), not by who
owns the file. So the same clip reached through your own library and through
somebody else's share is one project with one commit tree, and an editor's commit
appears in the owner's History panel with the editor's name on it. That is the
founder's "when people make updates it shows up in history", and it needed no new
mechanism: opening a shared clip resolves it to the same key.

### The Files pane

The left rail's Clips tab became **Files** (`studio/static/files.js`). It is the
same library as the routes above, drawn as a drive: a root switcher (My files,
Shared with me, one chip per team, Trash, and This Mac, which is the old
anywhere-on-disk browser kept as it was), a breadcrumb, a toolbar (new folder,
upload, list or grid, sort), and rows with a thumbnail, the clip's project head,
and hover actions (open, rename, move, share, trash). Drag a video from the
desktop onto the pane to upload it into the folder you are looking at, with a
progress row per file; a refused file keeps its row and says why. Drag a row onto
a folder to move it. Share opens a dialog that lists people and teams in your org
with a viewer or editor role; a viewer who tries to grade sees the 403 sentence as
a toast. Trash is a root, not a delete: everything in it has Restore. The History
tab carries the org activity feed underneath the commit graph
(`studio/static/activity.js`). On a phone the same pane is the Clips page of
mobile mode, with row actions always visible.

### Reading is checked too, not just writing

The permission rule has two halves and they are enforced in different places for
a reason worth stating plainly. Every route that CHANGES a project (a session
write, a grade save, checkout, fork, undo, redo, rotation, extras) goes through
one guard that refuses a viewer. Every route that HANDS BACK PIXELS or facts
about a file goes through a second guard that refuses an account with no grant at
all: `frame`, `stats`, `scope`, `source`, `range`, `range/limit`, `match`,
`thumb`, `render`, `play/prepare`, `proxy/prepare`, `POST /api/open` and
`POST /api/project/open`.

The reason the second guard exists: clips are addressed by bare FILE NAME, and
the name table is one table for the whole server. So the moment one account opens
`holiday.mov`, that name resolves for every other account on the same server, and
without a check the only thing between somebody else's footage and a stranger is
guessing its file name. The check runs on the path the name resolves to, so it
answers the same question the write guard answers: your own library, a viewer or
editor grant, the shared `content/footage` common area, and anything opened from
elsewhere on the machine all pass; anything else is a 403 saying the item is in
another account's library and has not been shared with you.

`play/stream` and `proxy/<key>.mp4` are plain GETs carrying only a cache key,
because a `<video src>` cannot send a body. Those two answer the key against the
`prepare` call that minted it, and with logins on a key this server has not
prepared in this run is refused rather than served: the segment and proxy caches
are one folder for the whole machine, so an unknown key may name a file another
account built and there would be nothing left to check it against. Every client
prepares before it fetches, so pressing play again is the whole recovery.

The GPU render worker's own routes (`render/gpu/*`) are not checked here and do
not need to be. They carry the one-off token minted for a render that already
passed the check at `POST /api/render`, and they serve nothing but that job.

With logins off all of this is skipped, so nothing about running the studio on
your own Mac changes.

### Routes

Paths are POSIX and relative to the library root: no leading slash, no `..`. An
absolute looking path is read as relative to the library, never as a path on the
machine. Every one of them goes through one confinement helper that resolves
symlinks first and compares with `commonpath`, never with a string prefix. One
deliberate exception, and only with logins OFF: the LAST element of a path may be
a symlink to a file elsewhere on the machine. There is one library and one person
when logins are off, so "outside your library" means nothing for a link that
person put there themselves, and it is what lets a test run point `--footage` at
a folder of links instead of copies. The folder chain above the last element is
still resolved and still has to be inside the root, so a symlinked FOLDER is
never walked through, and with logins on the exception does not apply at all.

```
GET  /api/library?root=&path=      one folder, or one index view
GET  /api/library/share?path=      the grants on one item in my library
POST /api/library/share            {path, kind, target_kind, target_id, role}
POST /api/library/unshare          {share_id}
POST /api/library/mkdir            {root, path, name}
POST /api/library/rename           {root, path, name}
POST /api/library/move             {root, path, to}
POST /api/library/trash            {root, path}
POST /api/library/restore          {path}          (path is the trash entry)
POST /api/upload?root=&path=       the file body, as before
GET  /api/people                   users and teams in my org, for the picker
GET  /api/activity?limit=          the org feed I am allowed to see
```

`root` is one of:

- `mine`: my own library.
- `shared`: every grant I hold, direct or through a team, as top level rows.
  It is an index, not a folder: each row carries `root: "user:<owner id>"`, which
  is what you navigate into.
- `team:<id>`: the same index, filtered to grants given to that one team.
- `user:<id>`: somebody else's library, browsable exactly as far as a grant
  reaches and no further. Walking above the grant is a 403.
- `trash`: my own trash.

A listing answers with `{root, path, parent, owner_id, owner, role, can_edit,
crumbs[], folders[], files[], error}`. A file row carries `name, path, key,
bytes, mtime, duration, width, height, project, shared_by_me, shared_with_me`,
where `project` is `{key, head, head_full, branch, updated}` or null for a clip
nobody has graded. A folder row carries `name, path, count, mtime` and the same
two share flags. `parent` is null at the top of whatever the caller may see.

`GET /api/thumb` and `POST /api/project/open` take `{root, path}` as an
alternative to a clip name, which is how a clip in a shared folder is opened and
drawn without the client ever seeing a path on disk.

Trash never deletes. An item moves to `<library>/.trash/<ts>_<name>`, where the
name holds where it came from (`/` and `%` are escaped, so a file from the top of
a library is literally `<ts>_<name>`), and restore puts it back there. Two
consequences worth knowing: the grants on a trashed item are REMOVED rather than
followed into the trash, because throwing something away should stop other people
reading it; and a restore does not bring those grants back.

A rename or a move made THROUGH the app carries every grant on the item with it
(prefix rewrite, so a folder carries the grants on everything inside it). A
rename made behind the app's back, in Finder or a shell, cannot be followed: that
share is reported in the `shared` listing with `missing: true` rather than
quietly disappearing, so it can be seen and fixed.

### Activity

`GET /api/activity` is the org's feed of library events, newest first: `upload`,
`mkdir`, `rename`, `move`, `trash`, `restore`, `share`, `unshare`, each with the
actor, the owner, the path and a small `detail` object. You see events on your own
library plus anything covered by a grant you hold. Grade edits are deliberately
NOT in here: they are already commits in the project history with an author on
each one, and writing them twice would make the feed a worse copy of the History
panel.

### Admin flags

Orgs and teams are created from a terminal on the machine running the studio, the
same way accounts are, and never over HTTP:

```bash
S="content/.venv/bin/python content/studio/server.py"

$S --create-org acme
$S --list-orgs

# an account belongs to exactly one org; --org names it (default: default)
echo "the-password" | $S --create-user quinn --role user --org acme --password-stdin
$S --list-users                       # now prints each account's org

$S --create-team editors --org default
$S --add-to-team quinn editors        # by account name and team name
$S --remove-from-team quinn editors
$S --list-teams                       # teams, their org, and their members
```

There is no way to create an org, a team or an account over the network, and no
password ever goes on the command line.

### What this does not cover, stated plainly

- **A clip's NAME is a shared namespace.** Opening any clip (through the
  anywhere browser, through an upload, or now through a share) registers it in
  one in-memory table keyed by its bare file name, and the render routes
  (`frame`, `stats`, `scope`, `thumb`, `play`, `render`) identify a clip by that
  name with no library check of their own. So with logins on, a signed in
  account that GUESSES the file name of a clip somebody else has opened on this
  server can fetch frames of it. That table predates this arc and is what makes
  a clip openable from anywhere on the Mac at all; sharing widened what can land
  in it. Closing it properly means either a per account clip namespace or a read
  check on every render route, which is a design decision for the arc rather
  than something to slip in quietly. Until then: this is a local tool, and the
  network deployment story is still "put it behind a proxy and trust the people
  you gave accounts to".
- **The shared `content/footage/` is common ground.** With logins on it belongs
  to nobody, so every account can list it, open it and grade what is in it. That
  is exactly what it did before this arc; the library rules apply to per account
  folders, not to that shared one.
- **A share is not a copy.** Trashing or deleting the owner's file takes it away
  from everyone it was shared with, and there is no "make a copy in my library"
  action yet.

### Schema

Additive, created with `IF NOT EXISTS` on every boot, safe on a database made
before this arc existed. `orgs` and the `users.org_id` column live in `db.py`
(that file owns the `users` table); `teams`, `team_members`, `shares` and
`activity` live in `library.py`. The migration inserts org 1 `default` and adds
`org_id INTEGER NOT NULL DEFAULT 1` to `users` only when the column is not
already there. Nothing is rewritten, dropped or reordered, so running it against
a real database cannot lose anything.

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

Rotation (contract C2) is a decode and encode time argument on both engines,
not a filter either one could disagree about: `studio/render_gpu.py` passes
the same `rotation` string to ffmpeg's own decode and encode stages that the
ffmpeg engine uses for the whole render, so a render request that leaves
`rotation` unset picks up the open project's rotation on either engine the
same way. `POST /api/render` and the GPU render route both take `rotation`
(`auto|0|90|180|270`) and the legacy `autorotate` boolean, with the same
"absent `rotation` falls back to `autorotate`, absent both falls back to the
project" order as every other route in this file.

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

`name` and `out_dir` are forwarded straight to `match_reference()`, which
already accepted both: send them to say where the fitted cube lands instead
of trusting the shared filename `match_reference()` would otherwise pick
(`match_u<uid>_<ref>_<clip>_<method>`, which already carries the caller's own
id, contract G2, so two agents matching at once do not collide even with no
name given). A relative `out_dir` lands inside `content/`; an absolute one is
trusted as the caller's own folder; either way it is created if missing.

`ref_crop` and `frame_crop` (contract C7, four fractions each, `[X0, Y0, X1,
Y1]`) are the rectangles a person draws by hand on the reference and the
frame in the studio UI, saved on the open project. They are a browser tab
convenience, not something a match call inherits by default the moment a
`ref` and a `clip` are named: a caller sending `X-Studio-Agent` gets the
whole image compared unless it sends `ref_crop`/`frame_crop` itself, even
when the project already has rectangles saved. A bare browser style caller
still falls back to whatever is saved, exactly as before, since the
rectangle it would inherit is one it (or the person at that tab) drew
itself. Attaching to another user's session (`--attach`) does not change
this: an attached agent is still an agent, and still gets the whole frame
unless it sends the rectangles explicitly. An agent that wants the person's
own rectangle reads it first, from `GET /api/project` (`extras.match_crops`),
and sends it back on the match call, which also means its own notes can say
which rectangle it measured rather than "whatever happened to be saved."
`studio/tools/grade_client.py`'s `Studio.match()` already sends `[0.0, 0.0,
1.0, 1.0]` (the whole frame) whenever the caller gives neither crop, so an
agent using the client module never depended on this fallback either way.

`rotation` (one of `auto`, `0`, `90`, `180`, `270`) turns the clip before the
fit measures it, the same field `effective_rotation()` reads on every other
route; `cinegrade match --rotate` sends it, falling back to `--preset`'s own
config `rotation` and then `auto` when not given, see "CLI reference" above.

The full response also carries the entire looks catalogue (about 60 entries,
the same list `GET /api/looks` returns) UNLESS the caller identified itself as
an agent (`X-Studio-Agent`/`?agent=`, logins off): for that caller the
`"looks"` key is dropped from this one response (round 2 tooling item 9, see
"Per caller identity, with logins off" below for the full rule, including a
Bearer token caller's response, which is never trimmed). A browser tab's own
`matchReference()` repaints its LUT dropdown straight from this response, so
that behaviour is unchanged; an agent that wants the catalogue calls
`GET /api/looks` for it, once, rather than paying for it on every match.

The response also carries `recommended` (bool) and `bands` (for the reference
image and the source frame, before and after). `recommended` is a yes or no
reading of whether this ONE fit actually helped: it is `false` when the fit's
own `gain_colour_pct` came back negative (the shot moved further from the
reference, not closer) or when `lut_health.probes_ok` is false (a technically
broken LUT, a probe pinned to hard black or white). It is a measurement, not a
score: it is never a comparison against a different reference, method or
strength, and it is not `ok`'s replacement, it is one more reading alongside
it, from numbers `match_reference()` already computes. `bands` is the same
per-band block "What the numbers mean" above describes, so a caller can see
whether the fit's colour move landed evenly across the tonal range or only in
one part of it, without a second decode.

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

### Per caller identity, with logins off

Logins off has always meant every caller is user id 0: one shared live
session, one shared open project per clip, one shared bag of match crops.
That was fine for one person, and wrong the moment a second agent (or a
person and an agent) touched the same server: one editing the session the
other had open, or a match landing on whichever project happened to be open
rather than the caller's own. Contract G2 fixes this without turning logins
on.

Send `X-Studio-Agent: NAME` on any request and the server treats you as agent
`NAME`: your own live session, your own open project (branch, playhead), your
own saved match crops, your own rotation fallback. Also accepted as
`?agent=NAME` on a GET and as a body field `"agent"` on a POST or PUT. The
name goes through the same check every other name in this file does (no
slashes, no leading dot, up to 64 characters); a bad one is refused 400. Send
nothing and you are user 0, exactly as before, which is what the browser tab
does, so the tab is unaffected by any of this.

```bash
curl -s -H "X-Studio-Agent: colorbot-3" http://127.0.0.1:7431/api/state \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["caller"])'
# -> {"id": 1, "name": "agent:colorbot-3", "attached_to": null}
```

An agent is a row in the accounts table named `agent:NAME`, with no password
and a `kind` of `agent`. It is created the first time that name calls, and it
can never sign in, even after logins are turned on later: `kind` is checked
before any password is, so there are two independent locks. Shared between
every caller with logins off, on purpose (contract G2's own list): the
footage folder, presets (always `users/0/presets`, read and write, so an
agent's "save as" shows up in the browser tab too), refs, looks, and project
histories. A commit an agent makes is authored `agent:NAME` regardless of what
`by` a request sends, so a person's tab watching that project's history sees
exactly who changed what.

Being agent `NAME` (this header, or `?agent=`, or the body field, ALL logins
off only, see "Getting a token" above for the separate logins-on path) also
trims two response bodies, round 2 tooling item 9: `POST /api/match` drops
the `"looks"` key (about 60 entries, the whole LUT catalogue, otherwise
present on every response so the browser tab's dropdown can repaint without
a second call) and `POST /api/preset` answers `{"name", "comment", "path",
"ok"}` instead of `{"saved", "path", "presets"}`, dropping the entire preset
library listing. Both were free to a browser tab's own dropdown repaint and
expensive, per call, to an agent with no dropdown to repaint. An agent that
wants either list asks for it once, `GET /api/looks` or `GET /api/presets`.
Nothing about what gets saved or matched changes, only the response; a bare
caller (no header at all) and a Bearer token caller (logins on, "Getting a
token" above) both keep the untrimmed shape, byte for byte, since this is
gated on the same `X-Studio-Agent`/`?agent=` identity as everything else in
this section, which a Bearer token request never sets.

`X-Studio-Attach: 0` (or an account name) makes an agent act on THAT
session, workspace and match crops, while still signing every write with its
own name: this is how an agent drives what a specific human already has open,
rather than always working in its own private sandbox. Logins off only: with
logins on the header is refused 403, because a signed in caller's token or
cookie already IS the attachment, and honouring a second header on top of it
would be a way to act as somebody else without ever signing in as them.

`POST /api/session` also accepts `if_rev`: send back the revision you last
read, and a write that would land on top of a change you never saw is refused
409, with the current `rev` and the current session in the body, rather than
silently overwriting it. This is the fix for the exact failure mode a shared
session invites: two callers patching from the same stale read.

`GET /api/health` is the cheap first call (see "Run it" above):
`{ok, version, clips, uptime_s, ffmpeg_slots_free, cache_dir, data_dir,
logins}`, no ffprobe, just "is it up, which build, where did its cache and
data go". `GET /api/state` and `GET /api/whoami` both carry the same `caller:
{id, name, attached_to}` block. The server also isolates its own frame cache
per data dir (`--cache-dir DIR`, env `STUDIO_CACHE_DIR`; with `--data-dir` and
no explicit `--cache-dir` the cache becomes `<data-dir>/cache`; with neither
it stays `studio/cache`, unaffected), which is what stops a throwaway test
server from evicting a real one's cached frames just by running alongside it.

That now covers the LUTs the ENGINE bakes as well as the frames the server
decodes. The three generated cube caches (`luts/layers` for layer
corrections, `luts/masks` for layer mask and window mattes, `luts/slice` for
Color Slice and Tetra) are hash named, and they used to be fixed folders
under `grade/`, so every run on one machine baked into the same three places
whatever `--data-dir` it was started with: two runs on one clip could land on
one file, and a run had no folder of its own to point at. They resolve from
the run's cache root instead. The order is `CINEGRADE_CACHE_DIR`, then
`STUDIO_CACHE_DIR` (which is what the server sets for its own children), then
`<STUDIO_DATA_DIR>/cache`, then `grade/` itself as the last fallback for the
bare CLI, so a plain `cinegrade render` with nothing set writes exactly where
it always did. Nothing already in `grade/luts` moved or was deleted; the
shipped inputs (`luts/looks`, `luts/technical`) are not caches and never
resolve this way.

The request log is one line per request on stderr by default:
`HH:MM:SS caller METHOD /route clip=NAME status Nms`, with `>N` appended after
the caller's name while attached to user `N`. `--quiet` turns it off, except
an attached request, which is always logged regardless, since "who did that
to somebody else's session" is exactly the question this log exists to
answer.

On the CLI: `--agent NAME` (env `STUDIO_AGENT`) and `--attach USER` on
`session`, `whoami`, every `project` subcommand, and `match`, `preset
save`/`load`, `grade save`/`load` (below); `session patch` also takes
`--if-rev N`. `cinegrade whoami` prints a `caller` line and, while attached,
an `attached` line.

```bash
STUDIO_AGENT=colorbot-3 cinegrade.py whoami
# caller   agent:colorbot-3 (id 1)

cinegrade.py session patch '{"primaries": {"contrast": 1.1}}' \
  --agent colorbot-3 --attach 0
# writes into user 0's live session (what the browser tab shows),
# still signed agent:colorbot-3
```

Every one of those subcommands also takes `--port`, and now `--url` (env
`STUDIO_URL`) alongside the older `--port`'s own env, `STUDIO_PORT`: a URL
wins outright over a port, from either source, and within either tier a
flag wins over its matching environment variable; with none of the four the
default is port 7431, a human's own live studio. That silent default is
where the port trap lived: a plain `session patch` with no `--port` used to
land on the founder's own live server with no way to tell it apart from a
throwaway one. It is now refused instead, but ONLY for a call that also
named an agent: passing `--agent` or setting `STUDIO_AGENT` with no
`--port`, `--url`, `STUDIO_PORT` or `STUDIO_URL` at all raises before any
request goes out, with a message saying an agent must name its server. A
human calling with no agent identity keeps the old silent 7431 default,
completely unaffected. Set `STUDIO_URL` (or `STUDIO_PORT`) once per shell
alongside `STUDIO_AGENT`, rather than repeating `--port`/`--url` on every
call, and this refusal never fires by accident.
`studio/tools/grade_client.py`'s `Studio` class reads `STUDIO_URL` the same
way it already reads `STUDIO_AGENT`: a constructor argument wins, else the
environment variable, else the 7431 default; it has no equivalent refusal,
since a caller building a `Studio` object has already had to name a `base`
or accept that default on purpose.

One cosmetic gap, left as is on purpose: a plain `GET /api/whoami` with no
`X-Studio-Agent` header and no session cookie reports `"by": "cli"` even
when the caller is an ordinary browser style request, because a GET has no
body to read a real `by` off of. Harmless: nothing that actually signs a
write reads this field, every real write goes through `session`, `project`
or `grade save`, each of which sends its own `by`.

### The `by` field, and the project it now writes to

`POST /api/session` and the config it returns carry a `by` field: a short,
free text label saying who made this change. A human dragging a slider in the
browser sends `"cli"` or leaves it out; an agent should send its own name
(`"agent"` is the default in `agent_grade.py`, but a fleet running several
agents at once should use something more specific, like `"agent:colorbot-3"`).
It costs nothing to set and it is the only way anything watching the session
(a person's open tab, `GET /api/session`, another agent) can tell an outside
patch apart from a local one.

Since contract C1 ("Projects and history" above), every session patch that
carries a `config` is also a COMMIT: `by` is the commit's author, and an
optional `message` (also on the wire, also read by `grade/cinegrade.py`
`session patch --message`) becomes its readable one line description instead
of the auto generated one. This is the whole answer to "the agent doesnt need
to have anxiety on whos editing what and what preset the person has
selected" (the founder's framing for this arc): an agent opens the project
(`POST /api/project/open`), reads HEAD's config off the response instead of
guessing what a person last loaded, and commits under its own name; the
person's tab shows that commit land, with the agent's name on it, over the
same long poll it already watches. `POST /api/session` is the edit path for
this reason; `PUT /api/grade` still records a commit too but deliberately
does not wake that long poll, so it is for a save nobody needs to watch
happen, never for an edit meant to be seen live. `GET /api/whoami` and the
CLI's `project` commands are documented in full there.

### The routes an agent actually needs

**`GET /api/health`** first, if there is any doubt the server is even up: no
ffprobe, just `{ok, version, clips, uptime_s, ffmpeg_slots_free, cache_dir,
data_dir, logins}` (see "Per caller identity" above). Then:

**`GET /api/state`**: defaults, the clip list (each with a `source` block and
rotation tags, "Input transforms" and "Rotation" above), the ref list, the
preset list, this caller's own identity under `caller`, and (with an account)
that the token is even valid, since a bad token 401s here before anything
else does.

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
asked for it. `clip` can be swapped for `ref` (a name under `content/refs`,
so the reference itself is measured the same way) or `path` (an absolute
file outside the footage root, logins off only); `times` (a list of seconds)
answers `{"results": [...]}` instead of one block; `region` narrows any of
the three to one rectangle.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"clip": "A001.MOV", "time": 3.0, "width": 480, "config": {}}' \
  http://127.0.0.1:7431/api/stats
```

**`POST /api/frame`**: the same request shape as `stats`, plus optional
`"mode"` (`graded`, `flat` or `mask`, with `"mask_layer"` picking which
layer's matte to show), `"region"`/`"zoom"` for a close up patch, `"path"`
for the same read only outside file `stats` takes, and `"format": "raw"` to
skip JPEG encoding. Returns the rendered picture instead of numbers. An agent
that wants to look at the frame rather than only measure it uses this, and
should: "How a preview frame is made" above and the standing warning in "What
the numbers mean" both say the numbers alone can mislead, on purpose.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"clip": "A001.MOV", "time": 3.0, "width": 480, "config": {}}' \
  http://127.0.0.1:7431/api/frame -o frame.jpg
```

**`POST /api/match`**: `{"ref": NAME, "clip": NAME, "time": SECONDS,
"config": CONFIG, "name": NAME, "out_dir": DIR}` fits a look cube toward a
reference image (see Match Reference above) and returns `{"ok", "name",
"lut", "warnings", "stats", "distance", "recommended", "bands", ...}`. `ok:
false` means the fit failed its own health check and should not be applied;
`recommended: false` is a second, narrower reading (the fit measurably moved
the shot further from the reference, or the LUT itself is unsound) worth
checking even when `ok` is true. On a good fit an agent applies it the same
way the browser does, `POST /api/session` with
`{"config": {"look": {"lut": result.name}}}`. Send `name`/`out_dir` so the
fitted cube lands somewhere this agent owns rather than the shared default
name (see Match Reference above). The shape above (with `"looks"`, the whole
catalogue) is what the Bearer token caller shown below gets; a logins-off
`X-Studio-Agent`/`?agent=` caller gets the same dict with `"looks"` dropped
("Per caller identity, with logins off" above).

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"ref": "IMG_2570.PNG", "clip": "A001.MOV", "time": 3.0}' \
  http://127.0.0.1:7431/api/match
```

This route sends the whole frame unless the caller sends `ref_crop`/
`frame_crop` itself; it never inherits a rectangle a person drew in a
browser tab (see "Match Reference" above). `cinegrade match` is the CLI
equivalent, and `Studio.match()` the client one (below); this is the one
place the server is the primary surface and the CLI mirrors it, not the
other way round, since the fit itself only exists here.

**`PUT /api/grade`** (contract C3, per clip saves) or **`POST /api/preset`**
(a shared, named grade): `PUT /api/grade` takes `{"clip": NAME, "config":
CONFIG, "by": NAME, "message": TEXT}` and returns `{"key", "updated_at",
"head"}` (`head` is the commit this save just made, contract C1), read back
with `GET /api/grade?clip=NAME` (`{"exists", "key", "config", "updated_at",
"head", "branch"}`). `POST /api/preset` takes `{"name": NAME, "config":
CONFIG, "comment": TEXT}` and returns `{"saved", "path", "presets"}` (the
whole preset library) to a Bearer token caller or a bare one; a logins-off
`X-Studio-Agent`/`?agent=` caller instead gets `{"name", "comment", "path",
"ok"}`, no library ("Per caller identity, with logins off" above). Either
shape is read back with `GET /api/preset?name=NAME`. An agent talking to an
older build that has not shipped `PUT /api/grade` yet should fall back to a
preset save;
either way, follow the write with the matching plain `GET` so the agent is
not just trusting its own POST or PUT, it is confirming the save actually
landed. Remember this route does not wake a watching tab ("The `by` field,
and the project it now writes to" above): for a change meant to be seen live,
`POST /api/session` is the one to use. `cinegrade grade save`/`load` and
`cinegrade preset save`/`load` are the CLI equivalents, see "CLI reference"
above for their flags.

```bash
curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"clip": "A001.MOV", "config": {"primaries": {"contrast": 1.1}}}' \
  http://127.0.0.1:7431/api/grade
```

**Projects and history** ("Projects and history" above has the full route
table and a real transcript): `POST /api/project/open {"clip": NAME}` opens
or creates the project and is the read an agent should do first, instead of
guessing what config is live; `GET /api/project` reads the same caller's open
project back without opening or creating anything. Both return the project
dict at the TOP level, with the clip under `"name"` (`{"open": true, "key",
"name", "branch", "head", ...}`), not nested under a `"project"` key: an
earlier agent guessed the nested shape by hand and hit a `KeyError`. `GET
/api/project/log` is the whole commit tree; `POST
/api/project/checkout|fork|undo|redo` move or branch HEAD; `GET /api/whoami`
answers who a write from this agent currently counts as, and, once a project
is open, carries `"project_clip"` next to the project's own key (a hash on
its own is not readable) so proving two agents have different clips open
needs no second call. All of them take the same `by` this section already
describes.

**`POST /api/mask/segment`**: `{"clip": NAME, "time": SECONDS, "rotation":
R, "prompts": {"text": [...], "points": [{"x", "y", "label"}], "boxes":
[[x0, y0, x1, y1]]}}` runs SAM's synchronous pick on one frame and returns
`{"pick_id", "instances": [{"id", "score", "box", "area", "overlay": URL,
"mask": URL}, ...]}`, nothing tracked or saved to the matte store yet. Look
at the `overlay`/`mask` previews before choosing an instance to track,
especially on a text prompt (`"person"` can match more than one thing in a
crowded frame; a point or box prompt is unambiguous by construction).

**`POST /api/mask/track`**: `{"clip": NAME, "rotation": R, "prompts": {...}
| "pick_id": ID, "select": [ids] | "all", "start": SECONDS, "end": SECONDS,
"steady": N}` starts a background SAM track over the clip and returns
`{"job_id", "mattes": [{"matte_id", "recipe", "state"}, ...]}` immediately;
it does not block. `steady` is temporal smoothing over that many frames.
Object instances tracked together each get their own matte id, all reported
under the same job. Cached by clip identity, rotation and recipe: the same
request twice returns the already running or already finished matte rather
than starting a duplicate job, so firing every track a grade will need as
early as possible costs nothing extra.

```bash
curl -s -H 'Content-Type: application/json' \
  -d '{"clip": "A001.MOV", "prompts": {"text": ["person"]}}' \
  http://127.0.0.1:7431/api/mask/track
```

**`GET /api/mask/status`**: the SAM service's own health plus its queue
(`{ok, backend, model, loaded, busy, queue}`), the first thing to check
before assuming a stuck job is a problem with this clip rather than the
service being down or out of capacity. It also reports where this server
keeps things and what it judges mattes by: `data_dir`, `matte_root`,
`footage_dir`, `mask_width` (the working width tracks are run at) and
`quality_thresholds`. `GET /api/health` carries the same three paths, which
is how the CLI resolves a bare clip name and a matte id through whichever
server it was told about (see below).

**`GET /api/mask/jobs`**, **`GET /api/mask/jobs/<id>`** (`{"state",
"done_frames", "total_frames", "fps", "elapsed_s", "matte_ids", "error"}`,
`state` one of `queued`, `running`, `done`, `failed`, `cancelled`),
**`POST /api/mask/jobs/<id>/cancel`**: the queue view, all jobs visible to
every caller (jobs are not per identity; mattes belong to the clip, the same
way footage does).

**`GET /api/matte?clip=`** (list), **`GET /api/matte/<id>`** (one index:
`matte_id`, `clip`, `clip_key`, `rotation`, `fps`, `frames`, `width`,
`height`, `recipe`, `state`, `done_frames`, `written_count`, `total_frames`,
`is_partial`, `areas`, `scores`, `ious`, `created`, `model`, `backend`, plus
`span`, `frozen_outside_span`, `coverage`, `mean_score`, `mean_area` and
`quality`), **`DELETE /api/matte/<id>`** (admin only, with logins on): a
matte's own record, `state` one of `queued`, `running`, `done`, `failed`,
`stale` (its recipe no longer matches anything live) alongside
`queued`/`running`'s own `partial` reading (servable by its nearest already
written frame, never empty).

The LIST route answers a summary per matte and leaves the per frame arrays
out (`{"mattes": [...], "full": false}`); `?full=1` puts `areas`, `scores`
and `ious` back. One matte by id always carries them, because `cinegrade mask
show ID --strip` plots its area curve from exactly that response.

`GET /api/matte` with NO `clip` lists the whole store, filtered to the mattes
the caller may read: the same read guard `?clip=` runs, per matte, dropping
what this account has not been shown rather than refusing the whole list. It
matters because a summary carries the clip's own file name and the matte's
recipe, which for a text prompt is the prompt words. With logins off the guard
is a no-op, so a local or agent caller sees everything, as before.

**A matte id is an id, never a path.** One pattern everywhere an id enters
(these three routes, `cinegrade --matte`, `grade_client.matte`/`matte_frame`/
`stats(matte=)`, and `mattes.resolve()` itself): a name of letters, digits,
underscore, dot and hyphen, up to 64 of them, starting with a letter or a
digit (`grade/mattes.py`'s `MATTE_ID_PATTERN`, the service's own ids being
`m_` plus a 12 hex digit digest). Anything else is a 404 on all three
`matte/...` routes, and a matte directory that leaves the store through a
symlink is a 404 too. `DELETE /api/matte/<id>` then asks the same two
questions again of the directory it is about to remove and answers 400 when it
is not inside the store or holds no `index.json` and no frames: with logins
off both the read guard and the admin gate are no-ops, so that check is the
only thing standing between the request and an `rmtree`. Before this an id was
allowed to BE a path, so `DELETE /api/matte//path/to/anything` deleted any
directory on the machine, footage and `studio/data` included.

**A matte belongs to one clip.** `POST /api/stats` (both `matte: ID` and a
`mask` component stack) and `POST /api/render` (both engines, whatever
`allow_partial` says) refuse a matte whose `clip_key` names a different clip,
in a message naming both clips, and the render's refusal also names the layer
and the component. Nothing outside the browser used to check this, so a
landscape clip measured through a portrait matte from another clip answered
with numbers, no warning and exit 0. A matte with no `clip_key` recorded (a
hand built fixture) cannot be checked and is allowed through, so the refusal
only ever fires on a matte that positively names another clip.

`span` is the window the matte really answers for, computed from the frames on
disk rather than the declared `frames` count: `{"start_frame", "end_frame"
(exclusive), "written", "declared_frames", "contiguous", "start_s", "end_s",
"frozen_outside_span": true}`. Outside it the engines hold the matte's nearest
written frame, which is what keeps a correction working while a track is still
running, and is also why it is stated in every response instead of left to be
discovered by measuring a frozen mask at 20 seconds and believing the number.
`coverage` is `span.written` over the span's own length, so a matte with a
hole in the middle reads below 1.0.

`quality` is the per frame flag block: `{"thresholds": {"area_jump",
"area_recover", "min_iou"}, "checked", "iou_source", "suspect_count",
"suspect_frames":
[{"index", "time", "reasons", "area", "prev_area", "jump", "iou"}, ...],
"truncated", "first_suspect_index", "first_suspect_time", "reasons":
{"zero_area", "area_jump", "area_recover", "low_iou"}}`. A written frame is
**suspect** when its area is zero inside the span, when its area moved by more
than `area_jump` of the previous written frame's area, when it holds something
and the frame before it held nothing (`area_recover`: the tracker found an
object again, and the area rule is blind to that frame because it divides by
the previous area), or when its IoU with the previous written frame is below
`min_iou`. `area_recover` is a pointer rather than a verdict, since a track
that recovers correctly trips it too, and it is the frame to look at first:
it is where a tracker most often comes back on the wrong object.
Defaults are `0.5` and `0.3`,
measured on the arc's own bakeoff mattes (a matte that lost its subject and
latched onto a tree flags 45 of 144 frames; a clean sky matte flags none),
and are overridable per call and by `CINEGRADE_MATTE_AREA_JUMP` /
`CINEGRADE_MATTE_MIN_IOU`. The thresholds always travel with the counts, so a
number can never be read without knowing what judged it. `iou_source` says
which IoU actually ran: `index` (the `ious` the SAM service wrote as it
tracked), `frames` (read off disk on demand, `mask show` only), or `none`, so
an absent rule is stated rather than silently passed. The list route caps
`suspect_frames` and sets `truncated`; the counts are always the true ones.
This exists because a track that lost its subject reported nothing at all,
and a grade was measured against the wrong subject without anybody knowing.

**`GET /api/matte/<id>/frame?time=&width=`**: one grey PNG matte frame,
nearest written frame served when `time` is past what a still-running track
has reached, with `X-Matte-State` (the matte's state) and `X-Matte-Frame`
(the index actually served) as response headers, so a caller can tell a
fallback happened without re-parsing anything. `width` has a ceiling of 3840
(one 4K frame) and a floor of 1, and both are a CLAMP rather than a refusal:
a caller asking for an absurd preview wants a picture. The answer says which
it got. `X-Matte-Width` is the width actually served whenever a `width` was
asked for, and `X-Matte-Width-Asked` is present only when the ask was not
honoured, so its presence is how a clamp is told apart from a matte that
happens to be 3840 wide. `cinegrade mask show ID
--strip` builds a whole clip's worth of these, one per second, tinted over
the picture, as a single verification image.

**Rendering on a matte that is not finished.** `POST /api/render` (and
`cinegrade render`) refuses on COVERAGE OF THE WINDOW ASKED FOR, not on the
matte's declared state. Every warning `mask_warnings` produces carries a
`kind` and a `blocking` flag: `missing` (an id that names nothing, or no id
yet), `pending` (no frames at all) and `partial` (the window has missing
frames) are blocking; `unfinished` (the window is covered, the track is still
going) is not. So a matte with 259 of 384 frames written renders a 0 to 10.79
second window with no flag at all, and prints a `note:` line on stderr saying
the track is unfinished and holds its last written frame past the window.
`allow_partial` (`--allow-partial`) still exists for the genuinely uncovered
case. Before this the refusal keyed on the state, so a render whose every
frame was on disk was refused for the frames it never asked for, and
`--allow-partial` was needed to ship a fully covered window.

**`POST /api/stats`** accepts an optional `"width"` (default unchanged: the
source's own width, or the still's) and every answer, single or `times`
row, carries `measured_width`, the width the numbers were really taken at.

**`POST /api/stats`** also accepts `"matte": ID` alongside `region`
(contract C4): weights every percentile, band and hue family by that
matte's value instead of measuring the whole frame flat, `region` crops
first and `matte` weights what is left, and `times` works with it the same
way it does without a matte. `POST /api/frame`'s shape is unchanged; a
`warnings` field may name a matte that fell back to its nearest written
frame.

**`POST /api/stats {"mask": {...}}`** measures through a whole mask
DESCRIPTION rather than one matte id. `mask` is the same block a graded layer
carries: `components`, a list of `{"type": "matte"|"key"|"luma"|"window",
"op": "add"|"intersect"|"subtract"}` entries, each with its own `invert` and
`feather`, plus the `finesse` block (`clean_black`, `clean_white`, `grow`,
`blur`) and a final `invert` on the result. So "the person matte intersected
with a skin key", the measurement a skin anchor is actually built on, is one
documented call:

```json
{"clip": "A001.mov", "time": 1.5, "width": 960, "config": {},
 "mask": {"components": [
   {"type": "matte", "op": "add", "matte": {"id": "m_5214be94f217"}},
   {"type": "key", "op": "intersect",
    "key": {"hue_center": 17, "hue_width": 26, "hue_soft": 5,
            "sat_low": 0.18, "sat_high": 0.85,
            "lum_low": 0.12, "lum_high": 0.9}}]}}
```

The stack is folded by `cinegrade.mask_matte`, the engine's own numpy
reference for a layer's matte and the code the parity suite renders against,
at the measured frame's own size, so a measurement and a render agree by
construction instead of by a separate proof. A one component matte stack
returns exactly what `"matte": ID` returns; that shortcut form keeps working
untouched. The answer adds `coverage` (the share of the frame the weight
covers), `no_coverage`, and `mask_mattes` (the matte ids the stack reached,
in stack order) beside `measured_width`, on a single row and on every `times`
row. Refused 400: `mask` together with `matte`, `mask` together with `region`
(a stack is written in the whole frame's coordinates, so composing it inside
a crop would move every window and misalign every matte: say the rectangle
with a `window` component instead), a `mask` that is not an object, a stack
whose first enabled component is an `intersect` or a `subtract` (the fold
starts at zero, so nothing would reach the matte), and a description that
selects nothing at all, including `{}`, which would otherwise measure the
whole frame while the caller believed a mask was applied. A matte id in the
stack that names nothing is 404, and a matte component with no id yet (still
waiting on a pick and a track) is refused rather than measured as black. Two
size limits are refused with the engine's own sentence as well: a stack
carrying more than 32 components, and a request whose layers carry more than
128 components between them. Each component is a fold (and, at the feather
cap, about 59 ms of numpy at 960x540) on the thread serving the request, and
the biggest stack in any real grade here is two components with six in a whole
grade, so a request past either limit is a mistake rather than a grade.
`POST /api/render` refuses the same config in the same words.
`cinegrade stats --mask JSON_OR_FILE` and `Studio.stats(..., mask=...)` /
`Studio.stats_at(..., mask=...)` are the same thing on the CLI and in the
client.

**A frame the mask covers nothing of.** A weight that sums to zero (the
subject left the frame, the key found no skin at this second) used to be a
`StatsError`, which meant a 400 on the route and a non zero exit on the CLI,
which killed any loop over timestamps at the first empty frame. It is a
normal answer now: `coverage` 0, `no_coverage: true`, and every measurement
block (`luma`, `saturation`, `channels`, `families`, `clipped`, `bands`)
`null` rather than 0, so nothing averages a frame that was never measured.
The CLI prints `no coverage: the mask covers no pixel of this frame ...` in
place of the numbers and exits 0. Only a weight of the wrong SHAPE is still
an error, since that is a caller bug rather than a fact about the frame.

`cinegrade mask segment|track|jobs|list|show` and `cinegrade stats --matte`
are the CLI equivalents ("CLI reference" above); `.claude/skills/
studio-grading/SKILL.md`'s Masks section covers the method: verifying a
matte over time before grading on it, starting tracks early, a hold by
matte following the subject, a matte intersect key for skin, measuring by
matte, and what to do when the service is down or a job fails.

**Where the CLI looks for a clip and for a matte.** A server started with
`--footage` or `--data-dir` keeps its clips and its mattes somewhere the CLI's
own defaults know nothing about, so with `STUDIO_URL` set the CLI asks it (one
cached `GET /api/health` per process) instead of failing to find a matte it
just tracked.

A bare clip name (`cinegrade stats bare.mp4`) resolves: an existing path as
given, then the server's `footage_dir`, then `content/footage`. A multi-part
path that does not exist is refused as `no such file` rather than having its
basename hunted for elsewhere, and a name nothing has is refused with every
place it looked.

A matte store (`stats --matte`, and a render whose layer holds a matte)
resolves: `CINEGRADE_MATTE_ROOT`, then `STUDIO_DATA_DIR`, then the server's
own `data_dir` (set into `STUDIO_DATA_DIR` for that process only), then
`content/studio/data/mattes`. An explicit variable always wins outright, and
nothing is created or written on the way. Only the CLI ever asks: the server
imports the same module and must never end up calling itself.

### The first party client module

`studio/tools/grade_client.py` (stdlib plus numpy; Pillow optional, only for
`contact_sheet`'s text labels, see "Run it" above) wraps every route above so
an agent talks to the server in a few lines instead of hand rolling `curl` or
`urllib`. It sends `agent`/`attach`/`rotation` on every call once set on the
`Studio` object, so a rotation is never silently inherited from whatever the
shared session last had, and it decodes an `HTTPError` body and raises the
server's own message rather than a truncated traceback.

```python
import sys
sys.path.insert(0, "studio/tools")
from grade_client import Studio, brief, diff

studio = Studio(base="http://127.0.0.1:7431", agent="colorbot-3")
state = studio.state()
clip = state["clips"][0]["name"]

before = studio.stats(clip=clip, time=1.0, config={})["stats"]
print(brief(before, clip=clip))

studio.session_patch({"primaries": {"saturation": 1.1}}, clip=clip, time=1.0,
                     by="agent:colorbot-3")
after = studio.stats(clip=clip, time=1.0,
                     config={"primaries": {"saturation": 1.1}})["stats"]
print(diff(before, after))          # signed deltas, after minus before

studio.grade_save(clip, {"primaries": {"saturation": 1.1}}, message="warmer")
```

Methods: `state`, `health`, `whoami`, `project_open(clip, rotation=None,
by=None)`, `project`, `frame`, `stats(..., matte=None,
mask=None)`, `stats_at(clip, times, ..., matte=None, mask=None)` (`mask` is a
whole component stack, see `POST /api/stats` above; one of the two, not
both), `ref_stats`, `match`, `segment(clip, time=0.0,
prompts=None, text=None, points=None, boxes=None, exemplars=None,
rotation=None)`, `track(clip, text=None, pick_id=None, select=None,
start=None, end=None, steady=None, rotation=None, prompts=None,
force=False)`, `wait(job_id, poll=1.0, timeout=None, on_progress=None)`,
`mattes(clip, full=False)` (the summary list, `full=True` for the per frame
arrays), `matte(matte_id)` (one matte, arrays always included),
`matte_frame(id, time=0.0, width=None, out=None)`, `preset_save`/`preset_load`,
`grade_save`/`grade_load`, `session_get`/`session_patch`, `sweep`, and the
escape hatch `request(method, path, ...)` for anything not wrapped yet.
`segment`/`track` return the routes' own shapes unchanged (`{"pick_id",
"instances"}` and `{"job_id", "mattes"}`); `wait` polls `GET
/api/mask/jobs/<id>` until it leaves `queued`/`running`, calling
`on_progress(job)` once per poll if given, and raises `StudioError` on a
`failed` job rather than returning it, so a caller does not have to check
`state` itself just to find out a track it is blocking on already failed;
`matte_frame` returns `{"data": bytes, "state", "frame"}`, `state` and
`frame` read off that response's own `X-Matte-State`/`X-Matte-Frame`
headers. `project_open` and `project` both
return the project dict at the top level with the clip under `"name"`, the
same shape `GET`/`POST /api/project*` return on the wire ("The routes an
agent actually needs" above): this module is the one place that shape is
already unwrapped correctly, so an agent importing it never has to guess.
`stats_at`, by contrast, hands back `POST /api/stats`'s own `{"results":
[...]}` envelope unchanged (round 2 tooling item 2: it used to unwrap that
list for the caller, the one place in this module that did not hand back
exactly what the route answers); check for the `"results"` key rather than
assuming it, since an older server without the `times` route simply omits
it.
Module level functions, usable without a server at all: `brief(stats)` (one
readable line), `bands(stats)`, `diff(a, b)` (signed, b minus a; drops
`definitions` and `bands.edges` from the result, since both are measurement
constants that never differ between two frames from the same server and
would otherwise print as a wall of zeros burying the real deltas),
`decode(path, width=None)` and `measure(array)` (the exact `grade/stats.py`
functions, imported once, never a second copy), and `contact_sheet(paths,
out, labels=None, height=480, grid=None)` (the same common-height,
mixed-aspect labelled sheet `cinegrade sheet` builds).
`studio/tools/agent_grade.py` is built on this module instead of carrying
its own copies of the same three things.

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
parity harness writes after an actual run, not typed by hand. That path belongs
to a studio running on its own defaults; a server started with `--data-dir`
(which is every test harness, including the parity gate) writes
`parity-results.json` into that directory instead, so running the gate never
edits a tracked file. When this file and
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
