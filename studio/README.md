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
a tracked file. Your own saved presets go to `studio/data/users/<id>/presets/`
instead (id `0` when logins are off), gitignored the same way the accounts
database is. Overwriting a library preset writes a user copy that shadows it
rather than editing the shared file, which is the only sane meaning of
Overwrite on a file every other account is also reading.

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
"auth", "project"}`) answers what account is signed in, whether logins are on
at all, and which project this account currently has open; with logins on the
account name always wins regardless of what a caller sends, since a signed in
browser cannot sign somebody else's name to a commit by editing a JSON body.

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
no HTTP client. Every one of them takes `--port` (default 7431), `--by`, and
`--json` (the server's raw JSON instead of a table); `session patch` also
takes `--by` and `--message`, sent as `by` and `message` on the wire.

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

The proxy is encoded already turned to the project's rotation, not always the
file's own tag: the cache key under `studio/cache/proxy/` carries the rotation
string, so there is one proxy file per clip per rotation, and switching a
project's rotation builds a fresh one rather than replaying the wrong turn.
This and every other route in this section (`play/prepare`, `thumb`, the
`range`/`limit` scrub reads) read rotation the same way `frame` and every
render path do (contract C2, see "Render engines" and `grade/README.md`
`--rotate`): a request may send `"rotation": "auto"|"0"|"90"|"180"|"270"` (or
`?rotation=` on a GET); when that is absent the legacy `"autorotate":
true|false` is still honoured (`true` means `auto`, `false` means `0`); only
when NEITHER is sent does the server fall back to the open project's own
rotation. A caller that keeps sending `autorotate` therefore pins itself to
`auto` or `0` forever, because a boolean cannot say `90`, `180` or `270`: it
will never see the project's rotation take effect. `studio/static/live.js`
and the CLI's `project rotate` both send `rotation` and never `autorotate`.

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
CONFIG, "by": NAME, "message": TEXT}` and returns `{"key", "updated_at",
"head"}` (`head` is the commit this save just made, contract C1), read back
with `GET /api/grade?clip=NAME` (`{"exists", "key", "config", "updated_at",
"head", "branch"}`). `POST /api/preset` takes `{"name": NAME, "config":
CONFIG, "comment": TEXT}` and returns `{"saved", "path", "presets"}`, read
back with `GET /api/preset?name=NAME`. An agent talking to an older build
that has not shipped `PUT /api/grade` yet should fall back to a preset save;
either way, follow the write with the matching plain `GET` so the agent is
not just trusting its own POST or PUT, it is confirming the save actually
landed. Remember this route does not wake a watching tab ("The `by` field,
and the project it now writes to" above): for a change meant to be seen live,
`POST /api/session` is the one to use.

```bash
curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"clip": "A001.MOV", "config": {"primaries": {"contrast": 1.1}}}' \
  http://127.0.0.1:7431/api/grade
```

**Projects and history** ("Projects and history" above has the full route
table and a real transcript): `POST /api/project/open {"clip": NAME}` opens
or creates the project and is the read an agent should do first, instead of
guessing what config is live; `GET /api/project/log` is the whole commit
tree; `POST /api/project/checkout|fork|undo|redo` move or branch HEAD;
`GET /api/whoami` answers who a write from this agent currently counts as.
All of them take the same `by` this section already describes.

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
