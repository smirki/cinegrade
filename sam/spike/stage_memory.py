#!/usr/bin/env python3
"""Where one window's memory actually goes, stage by stage.

    uv run --project sam python sam/spike/stage_memory.py

`spike/track_memory.py` answers "is the service's memory bounded across
windows" and said yes while the process high water mark still read 13.7 GB per
window. This script answers the next question: **inside** one window, which
step allocates that transient, and does the fix remove it.

It loads the model ONCE and then runs the same window several times, so the
comparison is two numbers from one process rather than two runs minutes apart
on a machine whose other load moved in between. Each run is a real
`MlxBackend.track()` over a real window of real frames, with real PNG mattes
written, not a reconstruction of it: the stages are instrumented inside the
backend (`memstat.StageMeter`, off unless something turns it on) so what is
measured is the code the service runs.

What it measures, per stage:

* MLX's own peak of live buffer bytes, taken with `reset_peak_memory()` at the
  start of the stage and `get_peak_memory()` at the end. That is what the GPU
  allocator saw and it is an upper bound;
* the physical footprint the kernel charged the process, sampled by a
  background thread every few milliseconds so a transient between two frames
  cannot hide. That is the number that decides whether the Mac swaps.

The stages are: frame decode and resize, mlx-cv's frame ingestion (the
1008x1008 float32 `pixel_values` for the whole window), the seed detect, the
per window seeding, each propagate step, mask post processing, the PNG write,
and the free at the end of the window.

There is also a micro probe that runs the ViT trunk on its own, one frame, so
the per frame cost is separated from anything to do with the window at all.

Modes (`--modes`, comma separated):

    baseline     mlx-cv as it ships: the tracker's memory attention computed
                 as one whole matrix, which at this model's head_dim of 32
                 misses MLX's fused kernel and allocates 5.2 GB of attention
                 weights plus 5.2 GB for their softmax
    fixed        the service default: that attention run in blocks of
                 `MlxBackend.attention_chunk` queries (512), which is bit
                 identical arithmetic and a fraction of the memory
    layer-eval   baseline attention plus an `mx.eval` after every trunk layer.
                 Kept because it is the obvious first idea and it is NOT the
                 fix: measured, it moved the window peak by 9 MB and cost 23%
                 of the speed. Off in the service (`--mlx-layer-eval`)

Same rules as every script here: it takes the machine wide model lock and
exits 3 rather than waiting if someone else holds it, it never kills a process
it did not start, every ffmpeg call is bounded by an explicit duration, and it
writes only under `sam/spike/out/`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

SAM = Path(__file__).resolve().parent.parent
if str(SAM) not in sys.path:
    sys.path.insert(0, str(SAM))
if str(SAM / "spike") not in sys.path:
    sys.path.insert(0, str(SAM / "spike"))

import memstat                                                     # noqa: E402
from backends.base import TrackedMask                              # noqa: E402
from backends.mlx_backend import (ATTENTION_CHUNK,      # noqa: E402
                                  MlxBackend)
from frames import FrameSource                                     # noqa: E402
from modellock import ModelLock, owner                             # noqa: E402
from track_memory import CLIP, make_proxy                          # noqa: E402

OUT = SAM / "spike" / "out"


def say(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


# ---------------------------------------------------------------------------
# The micro probe: one frame through the trunk, nothing else
# ---------------------------------------------------------------------------


def vision_probe(backend: MlxBackend, frame: np.ndarray, mx) -> dict:
    """One frame through the vision encoder alone, with the peak it costs.

    This is the number that says whether the transient belongs to the window
    or to a single frame. It uses the loaded session read only: it starts a
    one frame session, runs the trunk, evaluates every output, and drops the
    session again.
    """
    session = backend._session                                     # noqa: SLF001
    state = session.start_session(frames=[frame])
    try:
        pixels = mx.array(state.pixel_values[0:1])
        mx.eval(pixels)
        mx.clear_cache()
        base = mx.get_active_memory() / 1048576.0
        mx.reset_peak_memory()
        began = time.perf_counter()
        vision = session.model.detector.vision_encoder(pixels)
        outputs = []
        for name in ("last_hidden_state", "fpn_hidden_states",
                     "fpn_position_encoding", "interactive_hidden_states",
                     "interactive_position_encoding",
                     "propagation_hidden_states",
                     "propagation_position_encoding"):
            value = getattr(vision, name, None)
            if value is None:
                continue
            outputs.extend(value if isinstance(value, (list, tuple)) else [value])
        mx.eval(*outputs)
        elapsed = time.perf_counter() - began
        peak = mx.get_peak_memory() / 1048576.0
        tokens = int(outputs[0].shape[1]) if outputs and outputs[0].ndim >= 2 else None
        out = {"active_base_mb": round(base, 1),
               "mlx_peak_mb": round(peak, 1),
               "over_base_mb": round(peak - base, 1),
               "seconds": round(elapsed, 3),
               "trunk_tokens": tokens}
    finally:
        vision = outputs = None
        backend._drop_state(session, state)                        # noqa: SLF001
        mx.clear_cache()
    return out


# ---------------------------------------------------------------------------
# One window through the real backend
# ---------------------------------------------------------------------------


def run_window(backend: MlxBackend, source: FrameSource, frames: int,
               text: str, out_dir: Path, mx) -> dict:
    """One window of `frames` frames, every stage measured, mattes written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    backend.chunk_frames = frames
    backend.stages.reset()
    backend.stages.enabled = True
    backend.stages.mx = mx
    backend.stages.start()

    written = {"count": 0}

    def on_frame(index: int, masks: dict) -> None:
        for slot_id, value in masks.items():
            mask = value.mask if isinstance(value, TrackedMask) else value
            clipped = np.clip(np.asarray(mask, dtype=np.float32), 0.0, 1.0)
            Image.fromarray((clipped * 255.0 + 0.5).astype(np.uint8), mode="L") \
                .save(out_dir / f"{slot_id}-{index:06d}.png")
            written["count"] += 1

    mx.clear_cache()
    mx.reset_peak_memory()
    before = memstat.snapshot(mx)
    began = time.perf_counter()
    backend.track(source.frames(0, frames), source.fps,
                  {"text": [text], "max_instances": 1}, None, on_frame)
    elapsed = time.perf_counter() - began
    after = memstat.snapshot(mx)
    backend.stages.stop()
    backend.stages.enabled = False
    window = (backend.meter.windows or [None])[-1] or {}
    rows = backend.stages.rows()
    # The window's own MLX peak has to come from the stages, not from the
    # WindowMeter: with stages on, every stage resets MLX's peak counter, so
    # the meter's reading at the seam only covers the last stage. Max over the
    # stages is the same number the meter would have reported with stages off.
    return {
        "frames": frames,
        "seconds": round(elapsed, 1),
        "mattes_written": written["count"],
        "stages": rows,
        "stage_table": backend.stages.table(),
        "mlx_peak_mb": max([row["mlx_peak_mb"] for row in rows] or [0.0]),
        "footprint_peak_mb": max([row["footprint_peak_mb"] for row in rows] or [0.0]),
        "sampled_peak_footprint_mb": window.get("peak_footprint_mb"),
        "footprint_before_mb": before.get("footprint_mb"),
        "footprint_after_mb": after.get("footprint_mb"),
        "peak_rss_mb": after.get("peak_rss_mb"),
    }


# ---------------------------------------------------------------------------


MODES = ("baseline", "fixed", "layer-eval")


def set_mode(backend, mode: str, mx, say) -> str:
    """Put the backend into one of MODES and say what that means in words.

    Both patches are process wide and both are reversible, so the modes can be
    switched inside one loaded model. That is the point of this script: two
    numbers from one process, not two runs minutes apart on a machine whose
    other load moved in between.
    """
    backend._remove_layer_eval()                                   # noqa: SLF001
    backend.layer_eval = False
    backend._remove_attention_chunk()                              # noqa: SLF001
    if mode == "fixed":
        backend.attention_chunk = ATTENTION_CHUNK
        backend._apply_attention_chunk(mx)                         # noqa: SLF001
        return (f"memory attention in blocks of {ATTENTION_CHUNK} queries "
                f"(the service default)")
    backend.attention_chunk = 0
    if mode == "layer-eval":
        backend.layer_eval = True
        backend._apply_layer_eval(mx)                              # noqa: SLF001
        return ("whole matrix attention, plus an mx.eval after every trunk "
                "layer (the dead end, kept for comparison)")
    return "whole matrix attention, mlx-cv exactly as it ships"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--clip", default=str(CLIP))
    parser.add_argument("--proxy", default=None,
                        help="a plain Rec.709 proxy to track. Made from --clip "
                             "when not given.")
    parser.add_argument("--width", type=int, default=1280,
                        help="proxy width. 1280 is the studio's own mask "
                             "working width (STUDIO_MASK_WIDTH).")
    parser.add_argument("--windows", default="16,48",
                        help="window sizes to measure, comma separated")
    parser.add_argument("--modes", default="baseline,fixed",
                        help="baseline = whole matrix memory attention, as "
                             "mlx-cv ships; fixed = that attention in blocks "
                             "of 512 queries, the service default; layer-eval "
                             "= baseline plus an mx.eval per trunk layer, the "
                             "dead end kept for comparison")
    parser.add_argument("--text", default="person")
    parser.add_argument("--sample-ms", type=float, default=3.0)
    parser.add_argument("--lock-timeout-s", type=float, default=60.0)
    parser.add_argument("--tag", default="stages")
    args = parser.parse_args(argv)

    sizes = [int(v) for v in args.windows.split(",") if v.strip()]
    modes = [v.strip() for v in args.modes.split(",") if v.strip()]
    for mode in modes:
        if mode not in MODES:
            raise SystemExit(f"unknown mode {mode!r}: one of "
                             f"{', '.join(MODES)}")

    clip = Path(args.clip)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = OUT / f"stage-memory-{args.tag}-{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    if args.proxy:
        proxy = Path(args.proxy)
        if not proxy.is_file():
            raise SystemExit(f"there is no proxy at {proxy}")
    else:
        if not clip.is_file():
            raise SystemExit(f"there is no clip at {clip}")
        proxy = make_proxy(clip, run_dir / "proxy.mp4", args.width,
                           max(sizes) + 2)

    lock = ModelLock(backend="mlx (spike/stage_memory.py)", log=say)
    if not lock.acquire(timeout_s=args.lock_timeout_s):
        info = owner() or {}
        who = (f"pid {info.get('pid')} ({info.get('backend')}, since "
               f"{info.get('since')})") if info else \
            "a process that left no owner file"
        say(f"the machine wide model lock is held by {who}. Nothing was "
            f"started and nothing was killed. Run this when it is free.")
        return 3

    result = {"tag": args.tag, "clip": str(clip), "proxy": str(proxy),
              "width": args.width, "windows": sizes, "modes": modes,
              "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "runs": [], "probes": {}}
    try:
        import mlx.core as mx

        say("loading the model (minutes on a busy Mac)")
        backend = MlxBackend(log=say)
        backend.load()
        backend.stages.sample_ms = args.sample_ms
        result["limits"] = backend.limits
        say(f"loaded. limits {backend.limits}")

        source = FrameSource(proxy)
        seed = source.still(0)
        say(f"proxy {source.width}x{source.height}, {source.total} frames, "
            f"{source.fps:.3f} fps")

        for mode in modes:
            say(f"[{mode}] {set_mode(backend, mode, mx, say)}")
            probe = vision_probe(backend, seed, mx)
            result["probes"][mode] = probe
            say(f"[{mode}] ViT trunk on one frame: mlx peak "
                f"{probe['mlx_peak_mb']:.0f} MB ({probe['over_base_mb']:.0f} MB "
                f"over base), {probe['seconds']:.2f}s, "
                f"{probe['trunk_tokens']} tokens")
            for size in sizes:
                say(f"[{mode}] window of {size} frames")
                row = run_window(backend, source, size, args.text,
                                 run_dir / f"{mode}-{size}", mx)
                row["mode"] = mode
                result["runs"].append(row)
                print()
                print(row["stage_table"])
                print(f"window: footprint peak {row['footprint_peak_mb']:.0f} MB "
                      f"(3 ms sampler), {row['sampled_peak_footprint_mb']} MB "
                      f"(per frame sampler), mlx peak "
                      f"{row['mlx_peak_mb']:.0f} MB, {row['seconds']:.0f}s")
                print()
        backend.close()
    finally:
        lock.release()
        say("model lock released")

    print()
    print(f"{'mode':<10}{'frames':>8}{'seconds':>9}{'fp peak MB':>12}"
          f"{'fp sampled MB':>15}{'mlx peak MB':>13}")
    for row in result["runs"]:
        print(f"{row['mode']:<10}{row['frames']:>8}{row['seconds']:>9.0f}"
              f"{row['footprint_peak_mb']:>12.0f}"
              f"{(row.get('sampled_peak_footprint_mb') or 0):>15.0f}"
              f"{row['mlx_peak_mb']:>13.0f}")
    print()
    for mode, probe in result["probes"].items():
        print(f"ViT trunk alone, one frame, {mode}: mlx peak "
              f"{probe['mlx_peak_mb']:.0f} MB, {probe['seconds']:.2f}s")
    (run_dir / "result.json").write_text(json.dumps(result, indent=2, default=str))
    print(f"\nwritten: {run_dir / 'result.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
