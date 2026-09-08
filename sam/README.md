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

Ports 7431, 7614 and 7615 belong to the studio and its tests. Nothing here
ever binds them, and the tests skip them when picking a random port.

### The flags that matter

| Flag | What it does |
| --- | --- |
| `--stub` | no model at all: synthetic drifting ellipses, so the studio, the CLI and the browser specs can be built and tested without weights |
| `--backend auto\|mlx\|torch-mps\|torch-cpu\|stub` | `auto` (the default) tries mlx, then torch-mps, then torch-cpu, and logs why each one it skipped failed |
| `--data-dir DIR` | where picks and mattes go when a request does not name an `out_dir` |
| `--model REPO` | override the weights (default `appautomaton/sam3.1-multiplex-bf16-mlx`) |
| `--chunk-frames N` | frames per tracking window, default 48. Bigger is slightly faster and costs about 12 MB a frame; smaller survives a busier machine |
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

## The matte store (C2)

```
<out_dir>/<matte_id>/000000.png   one 8 bit grey PNG per frame,
<out_dir>/<matte_id>/000001.png   named by ABSOLUTE source frame index
<out_dir>/<matte_id>/index.json
```

`index.json` carries the clip identity (`clip`, `clip_key`, `rotation`), the
recipe that made it, `state`, `done_frames`, and `areas` and `scores` as one
slot per frame of the whole clip, null where nothing has been written yet. A
matte that covers frames 48 to 96 of a 600 frame clip therefore has 600 slots
with 48 of them filled: an area curve always lines up with the timeline
without the reader having to know where the track started.

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
  automatically and a line says whose it was.
* If the directory is **empty**, this service waits rather than stealing it.
  The spike scripts and the torch backend's own helper take the same
  directory with a bare `mkdir` and write no owner file, so an empty lock can
  equally mean "a model really is loaded in a process I cannot name" or "a
  run died and left this behind". Guessing wrong costs the whole machine, so
  it waits and says every few minutes what to check. When you have looked and
  there is no model process, `rmdir /tmp/fixxr-sam-model.lock` or start with
  `--steal-stale-lock`.

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
* **Memory is bounded by windows, and that is not optional.** The spike
  tracked 120 frames of 720 wide material in ONE session: 2200 s, 0.055
  frames a second, peak 14 GB on a 16 GB machine, because a session's memory
  grows with every frame it holds. So a clip is tracked in windows of
  `--chunk-frames` (default 48, about 580 MB of preprocessed frames), each
  window seeded from the last mask of the frame it shares with the one
  before, and each window's session is dropped and its buffers freed before
  the next is built. Frames are decoded lazily through one bounded ffmpeg
  pipe rather than read in as a clip. `/health` says which window is loaded
  and what this process is costing, so a slow job can be understood while it
  is still running instead of guessed at afterwards.
* **Cancel takes effect within one frame**, and the matte it leaves behind is
  marked `partial` with the frames it actually wrote, not thrown away.
* **A failed job does not take the service down.** It is marked `failed` with
  the reason, the next job runs, and `/health` counts both.

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
(with a fake standing in for torch), and the service itself over real HTTP:
progress, queue order, picks jumping a running track, cancel while running,
cancel while queued, and a failed job that the service survives.

`test_real_model.py` is the only test that loads weights. It takes the lock,
segments "person" and "sky" on one frame of the spike's 720 wide material and
tracks "person" for two seconds, then writes what it measured to
`sam/tests/out/real-model.json`.

Each suite is a plain script that prints a line per assertion, the same shape
as the studio's own python tests, so there is no test framework in this
project's dependencies.

## Layout

| File | What it is |
| --- | --- |
| `server.py` | the HTTP service: routes, the queue, one worker, jobs and picks |
| `store.py` | the matte store (C2): PNGs, `index.json`, `steady` |
| `frames.py` | ffprobe and one bounded ffmpeg pipe: frames without loading a clip |
| `modellock.py` | the machine wide model lock |
| `backends/base.py` | the C7 interface, prompt validation, and the slot id planner both the service and every backend share |
| `backends/mlx_backend.py` | SAM 3.1 through mlx-cv on the Apple GPU |
| `backends/torch_backend.py` | the PyTorch backend (lane M1b) |
| `backends/torch_adapter.py` | fits the PyTorch backend to C7 without editing it, and feeds it one window at a time so it cannot hold a clip in memory |
| `backends/stub.py` | drifting ellipses, no weights |
| `spike/` | the feasibility spike this was built from. Reference, not the service |
