# sam: the masks service

One process that keeps a SAM 3.1 model in memory and answers two questions
over HTTP on the loopback interface:

* **segment**: "what is in this frame?" One image, some prompts (a phrase, a
  click, a box), an answer in the same request. This is what the founder
  clicking the picture in the studio turns into.
* **track**: "follow that through the clip." A job, a job id straight back, a
  PNG matte per frame written to disk as it goes, and progress while it runs.

Everything else in the masks arc reads those mattes off disk, so this service
is the only thing here that ever loads a model.

Contracts: C3 is the HTTP API, C2 is the matte store on disk, C7 is the
interface a backend implements. The request and response shapes, field by
field, are in `plan/2026-09-08-studio-masks/checkpoints/M1.md`.

## Running it

`sam/` is a uv project. `uv sync` builds `sam/.venv` from `sam/uv.lock`; never
use bare `pip` and never `uv pip install`, or the lockfile stops describing
what is actually installed.

```bash
uv sync --project sam --inexact          # the service itself
uv sync --project sam --extra mlx        # plus the MLX backend (Apple GPU)
uv sync --project sam --extra torch      # plus the PyTorch backend

uv run --project sam python sam/server.py --stub        # no weights, instant
uv run --project sam python sam/server.py               # the real model
```

`--inexact` matters while several people share this venv: a plain `uv sync`
removes anything not in the extras you named, which would uninstall the other
backend under a lane that is using it.

Default port **7560**, loopback only. The service refuses a non loopback
`--host` outright: it runs models on the founder's machine and has no auth,
so it must not be reachable from the network.

Ports that belong to somebody else are one list for the whole tree, as data in
`studio/tests/forbidden-ports.json` (the studio's node harness reads it too).
`sam/ports.py` reads that file and unions it with its own floor, so nothing
under `sam/` can drift from it and `sam/` still works with no studio tree next
to it. Today: 7431 (the founder's live studio), 7560 (the service this studio
expects), 7614 and 7615 (studio test servers), 7632 (an agent seat's studio
running out of this same worktree), 8756 (the Granite ASR server), 22929 and
28958 (found the hard way by the memory spike). Four hand written copies of
that list used to disagree and none of them covered 7632, which was live at the
time (round 1 finding 20). Add a port to the JSON and every suite and script
here picks it up; `sam/tests/common.py` and `sam/spike/track_memory.py` are the
two readers.

### The flags that matter

| Flag | What it does |
| --- | --- |
| `--stub` | no model at all: synthetic drifting ellipses, so the studio, the CLI and the browser specs can be built and tested without weights |
| `--backend auto\|mlx\|torch-mps\|torch-cpu\|stub` | `auto` (the default) tries mlx, then torch-mps, then torch-cpu, and logs why each one it skipped failed |
| `--data-dir DIR` | where picks and mattes go when a request does not name an `out_dir` |
| `--allow-out-dir DIR` | repeatable. With at least one of these the service is **confined**: an `out_dir` outside the data dir and outside every allowed root is a 400. With none it keeps the C3 behaviour (the studio owns the matte store and names it on every call) but logs the first write outside its data dir, warns on the job and lists it on `/health`. See "Where it may write" |
| `--model REPO` | override the weights (default `appautomaton/sam3.1-multiplex-bf16-mlx`) |
| `--chunk-frames N` | frames per tracking window, default 48. Bigger is slightly faster and costs about 12 MB a frame; smaller survives a busier machine. It is **not** a memory lever for the transient: a 16 frame window and a 48 frame window peak identically (see "Memory") |
| `--mlx-attention-chunk N` | queries per block in the tracker's memory attention. **Default 512**; `0` runs it whole. This is the flag that decides whether a window peaks at 6.3 GB or 13.8 GB. It cannot change a mask (measured: 284 of 284 identical) and it is 23% faster as well. See "Memory" |
| `--mlx-layer-eval` | also put an `mx.eval` after every ViT trunk layer. **Off**, because measured it moved the peak by 9 MB. Here so that can be re-measured rather than re-argued |
| `--mlx-cache-limit-mb N` | how much freed Metal memory MLX may keep for reuse. **Default 1024.** `0` disables reuse; a negative number leaves MLX's own default, which on this Mac is 15564 MB, i.e. the whole machine. See "Memory" below: this one flag is the difference between a 4 GB service and a 13 GB one |
| `--mlx-memory-limit-mb N` | the level at which MLX reclaims from its cache before allocating. Default `0`, meaning the GPU's own max recommended working set (12124 MB here). Negative leaves MLX's default, which is 1.5x that |
| `--quiet` | one flag for sharing this Mac: duty cycle 0.5, nice 15, MLX memory limit 6144 MB, mask width hint 720. Explicit flags win. See "Quiet mode" |
| `--duty-cycle F` | the fraction of wall time the model may own (`0 < F <= 1`, default 1). At 0.5 the GPU is free about half the time and a track takes about twice as long. See "Quiet mode" |
| `--nice N` | `os.nice` at startup, default 0. CPU priority only: it does nothing to Metal work, which is what `--duty-cycle` is for |
| `--mask-width-hint N` | advertise a working width on `/health` and warn on a wider track. Guidance, never enforced |
| `--require-backend` | exit rather than serve with no backend at all |
| `--no-model-lock` | skip the machine wide lock. Tests only |
| `--steal-stale-lock` | remove a lock nobody has claimed for 15 minutes. Read the lock section first |
| `--segment-timeout-s` | how long a pick waits for the model before the caller gets a 504 |

## The routes

```
GET  /health                  backend, model, queue, lock, counts
POST /segment                 one frame, answered in the request
POST /track                   a clip, answered with a job id and matte ids
GET  /jobs                    every job, newest first, filterable by clip_key
GET  /jobs/<id>               one job: state, done_frames, mattes, error
POST /jobs/<id>/cancel        stop it, whether it is running or still queued
GET  /picks/<id>              a previous pick, so a track can be seeded from it
```

Two properties are worth knowing before you build against it:

* **A track answers immediately.** The job id and the matte ids exist before
  any model runs, so the UI can show a row per object straight away and the
  matte directory is a known path from the start.
* **A pick jumps the queue, including past a track that is already running.**
  Picks are served in the gap between two frames of the running track, so
  clicking the picture answers in about the time of one frame rather than
  waiting for a 600 frame clip to finish. Only one model call happens at a
  time either way: the pick runs on the same worker thread.

### Seeding a track from a pick: mask, not box

`POST /track` with `pick` and `select` seeds the track from the **mask pixels**
the pick already wrote to disk, not from its bounding box, on any backend whose
`supports_mask_prompts` is true (mlx and the stub today). The response says
which of the two it used:

```json
{ "seeded_from": "mask", "matte_ids": ["m_..."], "mattes": [{ "object_id": "k0" }] }
```

* `"mask"`: the objects are named `k0`, `k1`, ... in the order they were
  selected, and `index.json` keeps the pick's own instance id in `picked_from`.
* `"box"`: the fallback, when the backend cannot take a mask prompt (torch) or
  the pick's PNGs are gone. The objects are named `b0`, `b1`, ... and the job
  carries a warning in plain words: **the tracked region can grow past the
  shape that was reviewed**. A box is a much weaker description of an object
  than the mask sitting next to it, which is how a reviewed pick could visibly
  spread onto the background and then go empty (checkpoint gap 20). Track a
  text prompt instead when the exact reviewed shape matters.

### Where it may write

Two rules, both always on:

* A matte id, an instance id and a `clip_key` used as a directory name are one
  path segment each, matching `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`, and the joined
  path is resolved and checked to still be inside its root. An id of `../../x`
  or `/tmp/x` is a 400, not a directory somewhere else with an `index.json` in
  it (round 1 finding 13). `/health.paths.matte_id_pattern` is the rule itself.
* `out_dir` must be absolute, must contain no `..`, cannot be a filesystem root
  and cannot be an existing file.

Beyond that, whether a caller may name a root outside `--data-dir` is a launch
decision, because C3 gives the studio the matte store and the studio names it
on every call. With no `--allow-out-dir` the service accepts it, logs it once,
warns on the job and lists it in `/health.paths.external_out_dirs`. With one or
more `--allow-out-dir` roots it refuses anything else.

### What `/health` says about the model

```json
{ "model_holder": { "job_id": "j_ab12", "clip_key": "C015-rot0", "done_frames": 96 },
  "queue": { "running": "j_ab12", "queued": 1,
             "jobs": [ { "job_id": "j_ab12", "state": "running",
                         "holds_model": true, "matte_states": ["running"] },
                       { "job_id": "j_cd34", "state": "queued",
                         "holds_model": false, "matte_states": ["queued"] } ] },
  "model_lock": { "held": false, "enabled": false, "bypassed": true,
                  "dir": "/tmp/fixxr-sam-model.lock", "owner": null },
  "paths": { "data_dir": "...", "allowed_roots": ["..."], "confined": false,
             "external_out_dirs": [] } }
```

* `model_holder` is the one field that answers "is the shared model on my job".
  Two callers polling two jobs both used to read `running` while only one of
  them had the model (checkpoint gap 21).
* `holds_model` says the same thing per job, and `matte_states` is that job's
  own mattes, which can no longer disagree with `state`: the mattes move to
  running before the job does.
* `model_lock.held` is the truth and nothing else. It used to read
  `held or --no-model-lock`, so a service that deliberately took no lock
  reported itself as the holder; `bypassed` is that case said plainly (round 1
  finding 11).

## The matte store (C2)

```
<out_dir>/<matte_id>/000000.png   one 8 bit grey PNG per frame,
<out_dir>/<matte_id>/000001.png   named by ABSOLUTE source frame index
<out_dir>/<matte_id>/index.json
```

`index.json` carries the clip identity (`clip`, `clip_key`, `rotation`), the
recipe that made it, `state`, `done_frames`, and `areas`, `scores` and `ious`
as one slot per frame of the whole clip, null where nothing has been written
yet. A matte that covers frames 48 to 96 of a 600 frame clip therefore has 600
slots with 48 of them filled: an area curve always lines up with the timeline
without the reader having to know where the track started. `ious` is each
written frame's overlap with the previous written frame, computed here because
this is the only place both masks are in memory; it is what a drift check reads
to say "the track jumped onto something else on frame 212".

**A matte belongs to the backend that made it.** `index.json` records `backend`,
`model` and `score_kind`, and the last one changes what the numbers mean:

| `score_kind` | Backends | What `scores[n]` is |
| --- | --- | --- |
| `tracker` | mlx, stub | the tracker's own per frame confidence |
| `presence` | torch-cpu, torch-mps | the mask's own maximum, because the inner backend reports no per frame confidence: "the object is present", not "the tracker is sure" |

Mask softness differs the same way (torch returns a hard binary mask for a text
prompt and a soft one for points). The matte id is a hash of the clip, the
rotation, the width and the recipe and deliberately carries no backend, because
the studio's own recipe cache decides reuse before this service is called at
all; so a resume can land in a matte another backend started. That is allowed
and it says so on the job: "matte m_x was started on backend mlx and is being
continued on torch-cpu ... track it again from scratch if that matters" (round 1
finding 29). Two curves from two backends are not comparable frame by frame.

`seeded_from` (`"mask"`, `"box"`, or null) records which description of a pick
actually started the track: see "Seeding a track from a pick" above.

It is written progressively, at most once a second, atomically (temp file
plus rename), so anything reading it while a job runs sees a complete file,
never half of one. That is what lets the picture play against a matte that is
still being made.

`steady` smooths a matte over time. The window is **centred**, not trailing:
frame n is the mean of the n frames around it. A trailing window would drag
the matte behind the object, which looks like lag rather than steadiness.

## The machine wide model lock

The model is about 5 GB resident and this Mac has 16 GB shared between
everything. Two models at once does not fail cleanly, it swaps until the
whole machine crawls, so "one model process at a time" is a rule about the
machine, not about this process.

The lock is a directory, `/tmp/fixxr-sam-model.lock`: `mkdir` is atomic, so
creating it is holding it. The service takes it before it loads anything and
holds it until it exits (including on failure, through an exit hook), and it
writes `owner.json` inside with its pid, backend and start time.

```bash
cat /tmp/fixxr-sam-model.lock/owner.json     # who is holding it
```

* If `owner.json` names a **dead** process, the lock is reclaimed
  automatically and a line says whose it was. The owner is read again in the
  instant before anything is deleted: between the two reads another process can
  have taken the lock for itself, and deleting that holder's lock is the bug the
  re-read exists to stop (round 1 finding 11).
* If the directory is **empty**, this service waits rather than stealing it.
  The spike scripts take the same directory with a bare `mkdir` and write no
  owner file, so an empty lock can equally mean "a model really is loaded in a
  process I cannot name" or "a run died and left this behind". Guessing wrong
  costs the whole machine, so it waits and says every few minutes what to check.
  When you have looked and there is no model process,
  `rmdir /tmp/fixxr-sam-model.lock` or start with `--steal-stale-lock`. The
  torch backend's own helper goes through `modellock.ModelLock` now, so a torch
  run writes an owner file too and a killed one can be reclaimed instead of
  blocking every later run on this machine forever.

Only a holder may give the lock back. `release()` reads `owner.json` first and
refuses to remove a directory that names another pid, and a run with the lock
disabled reports `bypassed`, never `held`: a `--no-model-lock` service used to
claim it held the lock and then delete a real holder's directory on the way out,
which left room on this Mac for a second 5 GB model (round 1 finding 11).

`--stub` takes no lock at all.

## When it is slow, and why

The honest version, so nobody debugs a machine that is behaving correctly:

* **Starting is slow, once.** The port does not answer until the weights are
  loaded, so `/health` refusing to connect for the first minute or two of a
  real run is the model loading, not a crash. Everything after that is warm.
* **It waits for the lock.** If another model process on this Mac is running,
  startup blocks until it finishes and says so every few minutes with the pid
  it is waiting on. That is the lock working.
* **A frame every several seconds is normal, not a fault.** This model is
  not realtime on this machine, so everything is a job with progress rather
  than a request that returns a matte, and nothing anywhere should have a
  timeout that assumes otherwise.
* **Tracking is frame by frame, and it says where it is.** `done_frames` out
  of `total_frames` and a measured `rate_fps` update as it goes, and the PNGs
  appear as they are made. A long clip is a long job; nothing is buffered up
  to be written at the end.
* **Memory is bounded by three things, and all three are needed.** Windows
  bound what the tracker holds: a clip is tracked in windows of
  `--chunk-frames` (default 48, about 580 MB of preprocessed frames), each
  seeded from the last mask of the frame it shares with the one before, each
  window's session dropped and its buffers freed before the next is built,
  and frames decoded lazily through one bounded ffmpeg pipe rather than read
  in as a clip. Limits bound what MLX's allocator keeps: without
  `--mlx-cache-limit-mb` the process grows to fill the machine no matter how
  small the windows are. And `--mlx-attention-chunk` bounds the biggest single
  allocation any one frame makes: the tracker's memory attention, which
  without it asks MLX for a 5.2 GB matrix twice per frame. The next section is
  the whole story, because the first version of this service had the windows
  and still reached 13 GB, and the second had the windows and the cap and
  still spiked to 13 GB inside every frame.
* **Cancel takes effect within one frame**, and the matte it leaves behind is
  marked `partial` with the frames it actually wrote, not thrown away.
* **A failed job does not take the service down.** It is marked `failed` with
  the reason, the next job runs, and `/health` counts both.

## Quiet mode: sharing the machine

The founder's order was "let's not run sam3.1 in a way that slows down my
computer". Memory was the first half of that and is fixed below. This is the
other half: even a service whose footprint is flat owns the GPU **continuously**
for as long as a track runs, and on a 16 GB M5 Air that is what makes scrolling
stutter while a clip is being tracked. Nothing was leaking. The model was simply
never letting go.

One flag turns the whole thing on:

```bash
uv run --project sam python sam/server.py --quiet
```

That is the recommended launcher for any track the founder is going to sit
through. It is exactly equivalent to typing:

```bash
uv run --project sam python sam/server.py \
  --duty-cycle 0.5 --nice 15 --mlx-memory-limit-mb 6144 --mask-width-hint 720
```

and any of those four flags given explicitly wins over the preset, so
`--quiet --duty-cycle 0.25` is a quarter of the machine and everything else from
the preset.

| Flag | Default | What it does |
| --- | --- | --- |
| `--quiet` | off | the preset above. **Not** the studio server's `--quiet`, which only silences logging; this one changes how much of the Mac the model may take |
| `--duty-cycle F` | `1.0` | the fraction of wall time the model may own, `0 < F <= 1`. After a window of work that took `busy` seconds the worker sleeps `busy * (1/F - 1)`, so at `0.5` the GPU is free about half the time and the track takes about twice as long. `1` is no throttling and costs nothing |
| `--nice N` | `0` | `os.nice` at startup. CPU priority only |
| `--mask-width-hint N` | none | advertise a working width on `/health` and warn on a track wider than it. Guidance, never enforced |

**What the duty cycle actually is.** Not a rate limit and not a priority: the
worker alternates between working and deliberately doing nothing. The rest is
taken between windows (and after a pick, and after the seed detect pass), never
inside a frame, so no matte is ever half computed while the service sleeps and
nothing about the output changes. Measured on the real model, mattes are **byte
for byte identical** to an unthrottled run of the same window: 11 of 11 PNGs,
duty cycle 0.5 against duty cycle 1, sha256 per file.

**The rest is interruptible.** It is a `threading.Event.wait`, not a
`time.sleep`, so `/health` keeps answering in single digit milliseconds while
the service is resting, a cancel ends the job during the rest rather than after
it, and shutdown is immediate. A rest is never taken while a job would read as
finished either: the last window's rest is *owed* to the service and settled
after the job has been marked `done`, so no poll ever sees
`done_frames == total_frames` on a job still `running`.

**What `--nice` does not do.** It renices the process, which is CPU scheduling,
so it helps the ffmpeg decode, the PNG writes and the HTTP thread get out of the
founder's way. It does nothing whatever to Metal work, and Metal work is where
almost all of a track's time goes. There is no `nice` for the GPU on macOS,
which is the reason the duty cycle exists.

**What the mask width hint does not do.** It does not resize anything. The model
resizes every frame to 1008x1008 internally, so a narrower proxy shrinks the
decode, the frame buffers and the PNGs on disk, and not the model's own work.
720 is what the spike material is; anything wider gets a warning on the job, not
a refusal.

### Reading it on `/health`

```json
"throttle": {
  "duty_cycle": 0.5,             // the setting in force on the backend
  "enabled": true,
  "quiet": true,                 // --quiet was given
  "quiet_applied": {"duty_cycle": 0.5, "nice": 15,
                    "mlx_memory_limit_mb": 6144, "mask_width_hint": 720},
  "nice": {"requested": 15, "nice": 15, "before": 0, "error": null},
  "mask_width_hint": 720,
  "busy_s": 47.45, "idle_s": 35.28,
  "busy_fraction": 0.5735,       // the MEASUREMENT, not the setting
  "rests": 3, "woken_early": 0,
  "resting": true, "rest_left_s": 12.18,  // idle on purpose, not hung
  "owed": false,
  "last": {"unit": "window", "busy_s": 20.372, "asked_rest_s": 20.372,
           "rest_s": 0.0, "woken": false},
  "units": {"window": {...}, "seed detect": {...}, "pick": {...}}
}
```

`duty_cycle` is what was asked for and `busy_fraction` is what happened, and both
are here on purpose. Three things make the measurement read above the setting,
and the block says which: a reading taken while a rest is still running counts
only the part of it that has happened so far (that is this example, 0.5735 with
12.18 s of the rest still to come, which would land at 0.5 once it finishes);
a rest cut short by a cancel or by shutdown, which `woken_early` counts; and a
window longer than `max_rest_s` (300 s), whose rest is clamped. A null
`busy_fraction` means no unit of work has finished yet.

`resting` with a `rest_left_s` is the field that answers the only question this
block exists for: **"why has this track not moved in three minutes?"** If
`resting` is true the service is idle on purpose and will resume in
`rest_left_s` seconds. If it is false and nothing is moving, that is a real
problem. The studio forwards the whole block, so `GET /api/mask/status` carries
`throttle` at the top level next to `service` and the same question can be
answered from the studio without going near the SAM port.

### What it measured on the real model

Two windows of 6 frames (11 frames) of `A001_09061900_C015.mov` through one
shared 720x404 Rec.709 proxy, prompt "person", each launcher a separate service
that took and released the machine wide model lock. `top -stats mem,cmprs`
rather than `ps rss`, because Metal buffers are not in RSS.

| launcher | s/frame busy | busy frac | wall s | top MEM | CMPRS | proc peak | lat p50 | lat p99 | load 1m |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| plain | 6.586 | 1.0 | 92.1 | 2135 MB | 2023 MB | 5013 MB | 6.43 ms | 22.45 ms | 38.87 |
| `--quiet` | 4.314 | 0.574 | 86.0 | 2138 MB | 1832 MB | 5744 MB | 7.48 ms | 18.73 ms | 20.19 |
| `taskpolicy -b --quiet` | 7.195 | 0.654 | 124.9 | 2131 MB | 1998 MB | 5358 MB | 5.28 ms | 15.55 ms | 16.52 |

`lat` is scheduler latency measured from an ordinary competing process
(`spike/quiet_probe.py`): ask to sleep 100 ms, record the overshoot. That
overshoot is what a stuttering scroll is, which is why it is here and load
average is not the headline.

**`taskpolicy -b` is not documented as the launcher, on purpose.** macOS
`/usr/sbin/taskpolicy -b` puts the process in the background QoS tier and it
does work: it execs in place, so the pid is preserved and a watcher can still
stop what it started, and MLX GPU work completes under it. But at this sample
size it cannot be called a clean win: this machine was carrying other lanes
throughout (idle-baseline latency itself moved from p50 4.7 ms to p50 9.5 ms
between the two measurement sessions, and load 1m ranged from 12 to 39), which
is more than the difference between the rows. Its run was also the slowest per
frame. If someone wants to revisit it, it is one flag on the harness
(`spike/quiet_measure.py --taskpolicy`) on an otherwise idle machine, not a code
path in the service.

### Checking it yourself

```bash
# no model, no lock, seconds: the arithmetic, the log line, the cancel path,
# /health during a rest, and one --quiet flag end to end over HTTP
uv run --project sam python sam/tests/test_quiet.py

# does the machine still feel usable? Run it in another shell during anything
uv run --project sam python sam/spike/quiet_probe.py --seconds 30

# the real thing: plain against --quiet against taskpolicy -b, with the
# responsiveness probe alongside and a matte byte comparison at the end.
# Takes the model lock itself. Bound it: two windows is enough to see it
uv run --project sam python sam/spike/quiet_measure.py --windows 2 \
    --chunk-frames 6 --width 720
```

## Memory: what went wrong and how to read the numbers

The service was measured at **13 GB of physical footprint with 3.1 GB
compressed** on a 16 GB Mac after a few hundred frames, while `ps -o rss`
reported **155 MB**. Both numbers were correct. They just measure different
things, and the second one is useless here.

### Why `ps` lied

MLX allocates through Metal. A Metal buffer is an `IOAccelerator` mapping,
not an anonymous page, so it never appears in RSS. `vmmap` on the running
service showed the truth: 13.8 GB across 1209 `IOAccelerator` regions.

Read the **footprint** instead. It is what Activity Monitor calls Memory,
what `top -stats mem` prints, and what `/health` now reports as
`memory.footprint_mb`.

### The first cause: MLX's reuse cache

Not the tracker's windows: those were already bounded and they worked. MLX
keeps every Metal buffer it frees in a reuse cache, and **on this Mac that
cache's default limit is 15564.8 MB**, which is more memory than the machine
has. So a loop that allocates and frees buffers of many different sizes, one
live at a time, grows the process without growing its live set. That is
exactly what one model pass per frame does, 48 times per window.

`spike/mlx_cache_probe.py` shows it with no model at all, in a few seconds:

| | footprint | MLX cache |
| --- | --- | --- |
| 40 buffers of 16 MB churned, MLX's default limits | 696.4 MB | 652.2 MB |
| `clear_cache()`, read immediately | 680.4 MB | 0 |
| `clear_cache()`, read 3 s later | 17.8 MB | 0 |
| the same 40 buffers with the cache limit set to 0 | 60.7 MB | 0 |

Three things follow, and all three are in the code:

1. **Cap the cache at load.** `--mlx-cache-limit-mb`, default 1024. The model
   is about 1.7 GB of live weights and a window's own live set is roughly a
   gigabyte, so a gigabyte of reusable buffers keeps the hot shapes warm and
   still leaves the service inside about 4 GB.
2. **Empty it between windows and between jobs**, with `gc.collect()` first
   so the session's own arrays are already unreferenced. The memory does come
   back, about three seconds later, which is why the log line after a free
   often still shows the old figure and the number that matters is the next
   window's.
3. **Never reset the peak silently.** `_clear_cache` used to call
   `mx.reset_peak_memory()` on the way past, which meant `get_peak_memory()`
   only ever reported the peak since the last window. The one number that
   would have shown this problem always read low.

That fixed the accumulation: the sustained figure went flat at about 4.2 GB a
window and stayed there. It did not fix the spike, and the spike was the thing
that swapped the machine.

### The second cause: one attention matrix, 5.2 GB, twice over

With the cache capped, every sampled reading inside a window sat near 4.2 GB
while the process high water mark read **13739 MB** and MLX's own
`get_peak_memory()` read **13361 MB in every window**. Something was spiking
between samples.

`spike/stage_memory.py` instruments one window stage by stage: it resets
MLX's peak counter at the start of each stage, reads it at the end, and runs a
background thread sampling the process footprint every 3 ms so a transient
cannot hide between frames. It put the entire spike in one stage, and put the
same number there for a 16 frame window as for a 48 frame one:

| stage | calls | MLX peak | footprint peak | after |
| --- | --- | --- | --- | --- |
| seed detect | 1 | 4788 MB | 5281 MB | 4330 MB |
| decode + resize | 48 | 0 | 4325 MB | 3378 MB |
| ingest (`pixel_values`) | 1 | 0 | 4954 MB | 4954 MB |
| seed / `add_prompt` | 1 | 0 | 3797 MB | 3797 MB |
| **propagate (`_run_frame`)** | 48 | **13362 MB** | **14272 MB** | 4170 MB |
| mask post | 48 | 0 | 4170 MB | 4170 MB |
| emit (png write) | 48 | 0 | 4170 MB | 4170 MB |
| window free | 1 | 0 | 3907 MB | 3447 MB |

A `0` in the MLX column means that stage made no MLX allocation at all, not
that it used no memory: the counter is reset per stage and these stages are
numpy and file writes. The 16 frame window read 13324 MB in the same stage,
9 MB from the 48 frame figure, so **it was never the window size**. The
previous lane's "it grows with the window" (7.6 GB at 8 frames, 13.7 GB at 48)
was one per frame sampler catching the same spike more often on a longer run.

Inside that one frame it is the tracker's memory attention. `_DecoupledLayer`
cross attends the frame's 5184 image tokens to six remembered frames of 5184
tokens each, 8 heads, head_dim 256/8 = **32**, through
`mx.fast.scaled_dot_product_attention`. MLX's fused (flash) kernel covers
head_dim 64, 80 and 128; at 32 it falls back to composing the product by hand,
so it really does allocate 8 x 5184 x 31360 x 4 bytes = **5.2 GB** of
attention weights and another 5.2 GB for the softmax of them. With no model at
all (`spike/frame_probe.py` carries the same shape, q 2048, k 12288, 8 heads):

| | peak over base | time |
| --- | --- | --- |
| head_dim 32, `mx.fast.sdpa` whole | 810 MB | 792 ms |
| head_dim 32, chunked 512 queries | 204 MB | 93 ms |
| head_dim 64, `mx.fast.sdpa` whole | 4 MB | 291 ms |

Run that yourself in about ten seconds, no weights and no lock:
`python sam/spike/frame_probe.py --shapes`. The 200x gap between the two
head_dim 64 and head_dim 32 rows is the whole bug: same work, same answer,
one has a kernel and the other does not.

**The fix is to run that attention in blocks of queries.** A softmax is over
the KEY axis, so each query row is computed from exactly the same numbers no
matter how the query axis is split: the answer is bit identical, and the peak
falls by the block factor. `--mlx-attention-chunk` (default 512) wraps
`mx.fast.scaled_dot_product_attention` process wide, chunks only when the
matrix would exceed 256 MB and there is no mask, and `mx.eval`s each block so
MLX cannot rebuild the whole thing lazily. Measured on the real model, nine
frames at 1280 (long enough for the memory bank to reach its full six frames,
which is the whole point: a two frame probe reads 5.4 GB and proves nothing):

| attention chunk | MLX peak | footprint peak | mask pixels changed |
| --- | --- | --- | --- |
| whole (0) | 13201 MB | 12210 MB | reference |
| 1024 | 4459 MB | 5809 MB | 0 |
| **512** | **4539 MB** | **5489 MB** | **0** |
| 256 | 4459 MB | 5809 MB | 0 |

Zero pixels differ at every block size, which is what "bit identical" means in
practice: it is the same arithmetic in a different order of evaluation, not an
approximation, so there is no tracking quality trade to weigh.

Two things that were tried and are **not** the cause, recorded so they are not
tried again:

* An `mx.eval` after every one of the 32 ViT trunk layers, to stop MLX
  building the whole trunk graph before evaluating any of it. It moved the
  window peak from 13324 MB to 13315 MB and made a 16 frame window 23% slower.
  The trunk was never where the memory went. Kept as `--mlx-layer-eval`, off.
* `--chunk-frames`. 16 and 48 frame windows peak within 9 MB of each other, so
  shrinking the window buys nothing and costs tracking quality (each window is
  re-seeded from the previous one's last mask). Left at 48. See the checkpoint
  `plan/2026-09-08-studio-masks/checkpoints/FIX-MEMORY-2.md`.

### The numbers on `/health`

```json
"memory": {
  "footprint_mb": 2470.2,        // what top and Activity Monitor show
  "peak_footprint_mb": 13739.5,  // high water mark, all job. This one is
                                 // from a whole matrix attention run: see
                                 // "The second cause"
  "compressed_mb": 2335.0,       // the part ps never showed
  "rss_mb": 143.4,               // kept, because the gap IS the diagnosis
  "peak_rss_mb": 2496.1,
  "backend": {
    "active_mb": 1665.5,         // live MLX arrays: mostly the model
    "cache_mb": 0.0,             // freed buffers kept for reuse, 1035 mid job
    "peak_mb": 13361.5,          // high water of active, reset each window,
                                 // also a whole matrix attention reading
    "limits": {"cache_limit_mb": 1024.0, "cache_limit_was_mb": 15564.8,
               "memory_limit_mb": 12124.0, "recommended_working_set_mb": 12124.2,
               "attention_chunk": {"chunk": 512, "min_matrix_mb": 256,
                                   "enabled": true},
               "layer_eval": {"enabled": false, "wrapped": [], "missing": []}}
  },
  "windows": {
    "count": 3,
    "last": {"window_index": 3, "start": 94, "end": 142, "elapsed_s": 212.26,
             "peak_footprint_mb": 4177.9, "footprint_before_mb": 3777.0,
             "footprint_after_mb": 3459.0, "samples": 48},
    "peak": { ... the worst window so far, same shape ... },
    "current": { ... the window in flight, or null ... }
  }
}
```

`windows.last.peak_footprint_mb` is sampled **once per frame**, not at the
seam, because the allocator grows inside a window and a reading taken only
between windows would show the tidy number and never the one that swaps the
machine. The service logs the same record as one line per window:

```
[memory] window 2 frames 47..95 in 205.49s: peak footprint 4184 MB, 3969 MB
before the free, 3506 MB after; mlx active 1904.1 MB cache 1034.9 MB peak
13361.5 MB, cache 0.0 MB after
```

**How to read it.** Bounded memory is a flat peak across windows. A rising
peak means a window is keeping something the next one has to live with. The
absolute number is never the finding on its own: the model alone is 1.7 GB
resident before any frame is tracked.

Read `windows.peak` and `peak_footprint_mb` together, because they answer
different questions and the gap between them is a finding in itself.
`windows.peak.peak_footprint_mb` is sampled: per frame, and at the two seams
of the window. `peak_footprint_mb` is the process high water mark, so it
catches whatever happens between those samples. A large gap means a transient
the sampler is missing, which is exactly how the attention spike below was
found: windows reading 4.2 GB against a process reading 13.7 GB. When the two
are close, what you see per window is what the machine actually pays.

### Checking it against the machine

```bash
# no model, no lock, a few seconds: proves the cache mechanism
uv run --project sam python sam/spike/mlx_cache_probe.py

# the real model, three windows of 48 frames on C015, ~10 minutes.
# Takes the machine wide model lock itself and gives it back; refuses with
# exit 3 rather than waiting if another process already holds it.
uv run --project sam python sam/spike/track_memory.py --tag fixed

# the same run with MLX's own cache limit, i.e. the first unfixed behaviour
uv run --project sam python sam/spike/track_memory.py --tag mlx-default \
    --cache-limit-mb -1

# the same run with whole-matrix attention, i.e. the second unfixed behaviour
uv run --project sam python sam/spike/track_memory.py --tag before2 \
    --attention-chunk 0

# one window, stage by stage, both ways, with a 3 ms footprint sampler.
# This is the script that localised the spike; run it before believing any
# theory about where a window's memory goes.
uv run --project sam python sam/spike/stage_memory.py --frames 16 48

# no model, no lock: the attention shapes on their own, and a real 9 frame
# session compared mask by mask against unchunked attention
uv run --project sam python sam/spike/frame_probe.py --frames 9
```

`track_memory.py` samples `top -l 1 -stats pid,mem,cmprs -pid <pid>` after
every window, the same command a person would run, prints a table, writes
`result.json`, and stops the service by the process id it started.

One trap it encodes: the camera original is 4K Apple Log in `bt2020nc`, and
the detector finds **nothing at all** in it ("0 instances" for "person"). The
script makes a plain Rec.709 proxy first, in one bounded ffmpeg pass, the
same normalisation the studio's own mask proxy uses. A "the model found
nothing" failure on raw camera footage is a colour problem, not a model one.

### The verified runs

**Cache cap, 2026-09-08.** `spike/track_memory.py --tag fixed`, three windows
of 48 frames (142 frames) of `A001_09061900_C015.mov` at 1280 wide, prompt
"person", on the M5 with 16 GB while other work was running. `top MEM` and
`top CMPRS` are the same two columns a person would read.

| window | frames | took | top MEM | top CMPRS | window peak | after the free |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 0..48 | 201 s | 5668 MB | 1720 MB | 4180 MB | 3478 MB |
| 2 | 47..95 | 205 s | 5647 MB | 1724 MB | 4184 MB | 3506 MB |
| 3 | 94..142 | 212 s | 2470 MB | 2335 MB | 4178 MB | 3459 MB |

Model loaded and idle: 1782 MB. After the job and the release: 2470 MB.
**Peak footprint per window 4180, 4184, 4178 MB: first to last -2 MB.** The
MLX cache sat at 1035 MB all run (the 1024 MB cap) and read 0 after every
window's `clear_cache()`. Before that fix the same shape of work grew to
12 GB with 2.6 GB compressed and stayed there.

**The attention fix, before and after, 2026-09-08.** Three windows of 48
frames (142 frames) each, one run at a time with nothing else on the GPU, the
only difference being `--attention-chunk 0`. `proc peak` is the process high
water mark, the number a per frame sampler cannot see; `mlx peak` is MLX's own
peak inside the window.

| run | width | window peaks | **proc peak** | **MLX peak** | 142 frames took |
| --- | --- | --- | --- | --- | --- |
| `before2-1280` | 1280 | 4076, 4189, 4185 MB | 13798 MB | 13362 MB | 681 s |
| `fixed2-1280` | 1280 | 4409, 4283, 4716 MB | **6341 MB** | **4788 MB** | **524 s** |
| `before2-720` | 720 | 4881, 5964, 5876 MB | 11741 MB | 13362 MB | 778 s |
| `fixed2-720` | 720 | 5453, 5512, 5253 MB | **7008 MB** | **4788 MB** | **666 s** |

**All 284 mattes are byte identical between each before and after pair.** Not
within a threshold: the same bytes.

Three things to read out of that table:

* **the transient is gone**: 13.8 GB of high water mark on a 16 GB Mac meant
  swapping every window, 6.3 GB does not;
* **MLX's peak is identical at both widths** (13362 before, 4788 after) because
  the model resizes every frame to 1008x1008 before the trunk sees it. The
  proxy width changes the numpy buffers around the model, not the model's own
  memory, so lowering `STUDIO_MASK_WIDTH` is not a fix for anything here;
* **the sampled window peak went up by about 300 MB** and that is the trade:
  the finished blocks stay alive until the concatenate, so the sustained live
  set is slightly larger. 300 MB of sustained memory for 7.5 GB of transient.

The new ceiling is the **detector**, not the tracker: 4788 MB is exactly what
the stage table measures for `seed detect`, the phrase to instance pass on the
window's first frame. Whoever chases the next gigabyte should start there.

### What is still open

Nothing measured in this section is. Two smaller things worth knowing:

* the `--mlx-attention-chunk` help text in `server.py` itself still says the
  blocking saves memory rather than time. It saves both (524 s against 681 s
  for the same 142 frames); that wording predates the measurement and was left
  alone because another lane was editing `server.py` at the time;
* `stage_memory.py --modes baseline,fixed` would give the stage table a second
  column, which is the cheapest possible proof that nothing moved into a
  different stage rather than being removed. The window level numbers say it
  did not, since the totals fell.

## What it actually measured

One real run, 2026-09-08, MLX backend, `appautomaton/sam3.1-multiplex-bf16-mlx`,
on the spike's 720x404 material, on a machine with other work going on. These
are the numbers the rest of the arc should design against, not a benchmark:

| | |
| --- | --- |
| loading the model | 103.6 s, once, before the port answers |
| segment, two phrases ("person" and "sky") on one frame | 90.5 s for both |
| person | score 0.970, area 0.288 of the frame |
| sky | score 0.940, area 0.153 of the frame |
| track "person", 48 frames (2 s at 24 fps) | 856 s, **0.056 frames a second** |
| the tracked area over those 48 frames | 0.212 to 0.368, mean track score 0.999 |

Read the third row again: about **18 seconds per frame**. A 10 s clip is over
an hour of tracking. That is what the queue, the progress reporting, the
partial mattes and the cancel path are all for, and it is why nothing should
be built here that waits synchronously on a track.

The spike's own run is the other half of the picture: 120 frames in ONE
session took 2200 s and peaked at 14 GB. The windowed tracker gets the same
work done without the peak.

`sam/tests/out/real-model.json` is written by the real model test and holds
the full set.

## Tests

```bash
uv run --project sam python sam/tests/run.py          # everything, on the stub
uv run --project sam python sam/tests/test_real_model.py
```

`run.py` needs no weights, no network and no lock, so it is safe to run while
another process holds the model. It covers the matte store and its smoothing,
the model lock (including a dead holder and an unowned one), the prompt
planner and the stub backend, the torch adapter's windowing and id mapping
(with a fake standing in for torch), the CPU floor backend's own pytest suite,
the memory readings and the per window meter, the safety rules (below), and the
service itself over real HTTP: progress, queue order, picks jumping a running
track, cancel while running, cancel while queued, and a failed job that the
service survives.

Two rules the runner itself enforces, both from round 1 finding 41:

* **A skip is printed and counted, never folded into the total.** A suite that
  quietly dropped four checks when a clip went missing used to shrink its
  numerator and its denominator together, so the gate line stayed identical and
  nobody could see that anything had stopped being tested. Every skip now
  prints its reason and the summary ends with "N skipped".
* **Every suite declares a floor.** Fewer checks than the floor fails the gate
  and names the suite. Raise a floor when you add checks; lowering one has to be
  typed on purpose.

`test_safety.py` is the round 1 safety pass, all on the stub or on pure
functions: that a lock nobody took is never reported as held and never deletes
the real holder's directory, that a reclaim re-reads the owner before deleting,
that a matte id or `clip_key` naming a path is a 400 and writes nothing, that
`--allow-out-dir` confines the service and `/health.paths` says so, that the
MLX attention wrapper can be turned off and retuned in one process and that
`/health` carries what it DID (including a batched matrix measured with its
batch axis), that MLX's allocator is bounded before the weights load and a load
that fails after them still frees them, that a failed track closes its window,
that a failure to free is logged, that ffmpeg's stderr is a file and never an
undrained pipe, that loading torch no longer replaces the module's lock
functions, and, over real HTTP, that a pick seeds a track from its mask (with
the box fallback saying the shape can grow) and that a job's state, its mattes
and `model_holder` never disagree. It redirects the machine wide lock to a
temporary directory and asserts it is not the real one before a single check
runs, so it can never touch a lock a real model is holding.

`test_memory.py` is the memory half of that: it checks the footprint reading
against a real 128 MB allocation, that the same number is readable for any
pid (which is how a watcher script measures a service it started), that a
window records a peak sampled while it ran rather than at the seam, that a
100 frame track on the stub is three windows on `/health`, that the MLX
limits default to 1024 MB and the GPU's recommended working set rather than
MLX's own, and that emptying a window's frame list in place does not break
the next window. Three of its checks are about the attention fix:

* the stage meter catches a transient that a start-and-end reading misses. It
  maps 256 MB with `mmap`, touches it and unmaps it inside a stage, then
  asserts the stage's peak is above its own end reading. (`bytearray` will not
  do: freeing one does not return the pages to macOS, so the test would pass
  for the wrong reason.)
* chunked attention is **bit identical** to whole-matrix attention on real
  MLX arrays, that a big matrix is actually chunked and a small one is not,
  and that removing the patch puts MLX's own function back. Without mlx
  installed these degrade to honest skips rather than silent passes.
* `--mlx-layer-eval` is off by default, and when it is on it really does wrap
  the class it names and can be unwrapped again (checked with a fake module,
  so it needs no weights).

`test_quiet.py` is quiet mode: the duty cycle arithmetic and what it refuses
to parse, that a rest is readable while it is happening (so a resting service
reads as idle rather than as hung), the owe-and-settle rule that keeps a job off
100 percent while it is still running, `os.nice` (applied in a subprocess, so
the test run's own priority is untouched), the preset and its overrides, that
the stub really does sleep, and then the whole thing over real HTTP: the setting
on `/health`, the mask width warning, `/health` still answering in under 250 ms
mid rest, a queued job still reporting its position, a cancel during a rest
ending the job in under a second, and one `--quiet` flag end to end.

What none of it can check without weights is the size of the numbers on a real
track; `spike/track_memory.py`, `spike/stage_memory.py`,
`spike/frame_probe.py` and `spike/quiet_measure.py` do that.

`test_real_model.py` is the only test that loads weights. It takes the lock,
segments "person" and "sky" on one frame of the spike's 720 wide material and
tracks "person" for two seconds, then writes what it measured to
`sam/tests/out/real-model.json`.

Most suites are plain scripts that print a line per assertion, the same shape
as the studio's own python tests. One is pytest shaped
(`tests/test_torch_backend.py`, written with the CPU floor backend), and the
runner runs it too rather than leaving a backend's own tests ungated (round 1
finding 36); pytest is already in this project's dev group. Its two real-weights
tests print as skips with their reason, behind `SAM_TORCH_TEST_REAL=1`.

## Layout

| File | What it is |
| --- | --- |
| `server.py` | the HTTP service: routes, the queue, one worker, jobs and picks |
| `store.py` | the matte store (C2): PNGs, `index.json`, `steady` |
| `frames.py` | ffprobe and one bounded ffmpeg pipe: frames without loading a clip |
| `modellock.py` | the machine wide model lock |
| `ports.py` | the ports on this machine that belong to somebody else: `studio/tests/forbidden-ports.json` unioned with this project's own floor, read by every suite and script here that picks a random port |
| `backends/base.py` | the C7 interface, prompt validation, and the slot id planner both the service and every backend share |
| `backends/mlx_backend.py` | SAM 3.1 through mlx-cv on the Apple GPU |
| `backends/torch_backend.py` | the PyTorch backend (lane M1b) |
| `backends/torch_adapter.py` | fits the PyTorch backend to C7 without editing it, and feeds it one window at a time so it cannot hold a clip in memory |
| `backends/stub.py` | drifting ellipses, no weights |
| `memstat.py` | the Mac's own memory numbers: footprint, compressed, MLX active/cache/peak, the per window meter and the per stage meter |
| `throttle.py` | quiet mode: the duty cycle (an interruptible rest between windows), `os.nice`, and the `--quiet` preset |
| `spike/track_memory.py` | starts a real service, tracks three windows, reports what `top` and `/health` say per window |
| `spike/stage_memory.py` | one window, stage by stage, with a 3 ms footprint sampler. The script that localised the 13 GB spike |
| `spike/frame_probe.py` | attention shapes on their own, and a 9 frame real session compared mask by mask across attention chunk sizes |
| `spike/quiet_probe.py` | how usable the machine still feels: scheduler latency overshoot and CPU service time from an ordinary competing process |
| `spike/quiet_measure.py` | plain against `--quiet` against `taskpolicy -b`: per frame seconds, busy fraction, footprint, load, responsiveness, and a matte byte comparison |
| `spike/` | the feasibility spike this was built from. Reference, not the service |
