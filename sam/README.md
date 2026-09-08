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
| `--chunk-frames N` | frames per tracking window, default 48. Bigger is slightly faster and costs about 12 MB a frame; smaller survives a busier machine. It is **not** a memory lever for the transient: a 16 frame window and a 48 frame window peak identically (see "Memory") |
| `--mlx-attention-chunk N` | queries per block in the tracker's memory attention. **Default 512**; `0` runs it whole. This is the flag that decides whether a window peaks at 6.3 GB or 13.8 GB. It cannot change a mask (measured: 284 of 284 identical) and it is 23% faster as well. See "Memory" |
| `--mlx-layer-eval` | also put an `mx.eval` after every ViT trunk layer. **Off**, because measured it moved the peak by 9 MB. Here so that can be re-measured rather than re-argued |
| `--mlx-cache-limit-mb N` | how much freed Metal memory MLX may keep for reuse. **Default 1024.** `0` disables reuse; a negative number leaves MLX's own default, which on this Mac is 15564 MB, i.e. the whole machine. See "Memory" below: this one flag is the difference between a 4 GB service and a 13 GB one |
| `--mlx-memory-limit-mb N` | the level at which MLX reclaims from its cache before allocating. Default `0`, meaning the GPU's own max recommended working set (12124 MB here). Negative leaves MLX's default, which is 1.5x that |
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
(with a fake standing in for torch), the memory readings and the per window
meter, and the service itself over real HTTP: progress, queue order, picks
jumping a running track, cancel while running, cancel while queued, and a
failed job that the service survives.

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

What none of it can check without weights is the size of the numbers on a real
track; `spike/track_memory.py`, `spike/stage_memory.py` and
`spike/frame_probe.py` do that.

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
| `memstat.py` | the Mac's own memory numbers: footprint, compressed, MLX active/cache/peak, the per window meter and the per stage meter |
| `spike/track_memory.py` | starts a real service, tracks three windows, reports what `top` and `/health` say per window |
| `spike/stage_memory.py` | one window, stage by stage, with a 3 ms footprint sampler. The script that localised the 13 GB spike |
| `spike/frame_probe.py` | attention shapes on their own, and a 9 frame real session compared mask by mask across attention chunk sizes |
| `spike/` | the feasibility spike this was built from. Reference, not the service |
