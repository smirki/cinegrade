#!/usr/bin/env python3
"""One tracked frame, measured under different memory strategies.

    uv run --project sam python sam/spike/frame_probe.py --shapes   # seconds
    uv run --project sam python sam/spike/frame_probe.py --frames 9  # minutes

`--shapes` is the finding on its own: the attention shape the tracker uses,
with no model and no lock, whole against chunked, at head_dim 32 and 64.
Without it the rest of this script is a lot of machinery around one kernel.

`spike/stage_memory.py` said the whole transient lives in one stage: the
propagate step, `_run_frame`, one frame at a time. That is a per frame cost,
not a per window one, so the lever is where MLX is allowed to stop building
graph and start computing. This script measures exactly that, cheaply: it
loads the model once (the weights are memory mapped, so that is under a
second) and runs the SAME two frames over and over, once per candidate set of
evaluation boundaries.

Two kinds of candidate are measured. A BOUNDARY is an `mx.eval` on the output
of a named mlx-cv class; a CHUNK runs the memory attention in blocks of that
many queries. Neither can change a mask. `mx.eval` computes what the graph
already says, and a softmax is over the key axis, so splitting the query axis
gives the same rows back: the only thing either changes is WHEN the arithmetic
happens and therefore how many intermediates are alive at the same instant.
The masks are compared frame by frame anyway, because "cannot change a mask"
is an argument and the comparison is evidence.

Frame 0 is the interactive frame (the box prompt) and frame 1 is a
propagation frame; they run different halves of the model, so both are
reported. Every run starts from a fresh session so the tracker memories are
identical each time.

Same rules as every script here: it takes the machine wide model lock and
exits 3 rather than waiting, it kills only processes it started (it starts
none), every ffmpeg call is bounded by an explicit duration, and it writes
only under `sam/spike/out/`.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from pathlib import Path

SAM = Path(__file__).resolve().parent.parent
if str(SAM) not in sys.path:
    sys.path.insert(0, str(SAM))
if str(SAM / "spike") not in sys.path:
    sys.path.insert(0, str(SAM / "spike"))

import memstat                                                     # noqa: E402
from backends.mlx_backend import MlxBackend                        # noqa: E402
from frames import FrameSource                                     # noqa: E402
from modellock import ModelLock, owner                             # noqa: E402
from track_memory import CLIP, make_proxy                          # noqa: E402

OUT = SAM / "spike" / "out"

VIT = ("mlx_cv.models.sam3.real_vision", "Sam3ViTLayer")
NECK = ("mlx_cv.models.sam3.real_vision", "Sam3VisionNeck")
DETR = ("mlx_cv.models.sam3.real_detr", "Sam3DetrEncoderLayer")
DOWNSAMPLER = ("mlx_cv.models.sam3.sam31_tracker", "_MaskDownsampler")
CXBLOCK = ("mlx_cv.models.sam3.sam31_tracker", "_CXBlock")
MEMORY_BACKBONE = ("mlx_cv.models.sam3.sam31_tracker", "_MaskMemoryBackbone")
DECOUPLED = ("mlx_cv.models.sam3.sam31_tracker", "_DecoupledLayer")

# name, eval boundaries, attention chunk (0 = run the attention whole)
CANDIDATES: list[dict] = [
    {"name": "none", "targets": (), "chunk": 0},
    {"name": "vit-eval", "targets": (VIT,), "chunk": 0},
    {"name": "decoupled-eval", "targets": (DECOUPLED,), "chunk": 0},
    {"name": "memory-eval", "targets": (MEMORY_BACKBONE, DOWNSAMPLER), "chunk": 0},
    {"name": "chunk-1024", "targets": (), "chunk": 1024},
    {"name": "chunk-512", "targets": (), "chunk": 512},
    {"name": "chunk-256", "targets": (), "chunk": 256},
    {"name": "chunk-512+vit", "targets": (VIT,), "chunk": 512},
]


def say(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------


def wrap(target: tuple, mx) -> bool:
    module_name, class_name = target
    try:
        cls = getattr(importlib.import_module(module_name), class_name)
    except Exception as exc:                                       # noqa: BLE001
        say(f"  no {module_name}.{class_name}: {exc}")
        return False
    if getattr(cls, "_probe_wrapped", False):
        return True
    original = cls.__call__

    def wrapped(inner_self, *args, _original=original, **kwargs):
        result = _original(inner_self, *args, **kwargs)
        try:
            mx.eval(result)
        except Exception:                                          # noqa: BLE001
            # A class that returns something MLX cannot walk is simply not a
            # usable boundary; it is not a reason to fail the probe.
            pass
        return result

    wrapped._probe_original = original
    cls.__call__ = wrapped
    cls._probe_wrapped = True
    return True


def unwrap_all() -> None:
    for target in {VIT, NECK, DETR, DOWNSAMPLER, CXBLOCK, MEMORY_BACKBONE,
                   DECOUPLED}:
        module_name, class_name = target
        try:
            cls = getattr(importlib.import_module(module_name), class_name)
        except Exception:                                          # noqa: BLE001
            continue
        original = getattr(cls.__call__, "_probe_original", None)
        if original is not None:
            cls.__call__ = original
        cls._probe_wrapped = False


# ---------------------------------------------------------------------------
# One measurement
# ---------------------------------------------------------------------------


def shape_probe(mx, queries: int = 2048, keys: int = 12288, heads: int = 8,
                chunk: int = 512, head_dims=(32, 64)) -> list[dict]:
    """The attention shapes on their own: no model, no weights, no lock.

    This is the whole finding in ten seconds. The tracker's memory attention
    is 5184 queries against 31360 keys with 8 heads and head_dim 32; the
    shapes below are the same thing scaled down so the measurement fits on a
    16 GB Mac next to whatever else is running. head_dim 64 is measured for
    contrast, because that is the number MLX has a fused kernel for.
    """
    rows: list[dict] = []
    for head_dim in head_dims:
        shape = (1, heads, queries, head_dim)
        keys_shape = (1, heads, keys, head_dim)
        scale = float(head_dim) ** -0.5
        q = mx.random.normal(shape, dtype=mx.float32)
        k = mx.random.normal(keys_shape, dtype=mx.float32)
        v = mx.random.normal(keys_shape, dtype=mx.float32)
        mx.eval(q, k, v)
        for how in ("whole", f"chunked {chunk}"):
            mx.clear_cache()
            base = mx.get_active_memory() / 1048576.0
            mx.reset_peak_memory()
            started = time.perf_counter()
            if how == "whole":
                out = mx.fast.scaled_dot_product_attention(q, k, v, scale=scale)
            else:
                pieces = []
                for start in range(0, queries, chunk):
                    piece = mx.fast.scaled_dot_product_attention(
                        q[..., start:start + chunk, :], k, v, scale=scale)
                    mx.eval(piece)
                    pieces.append(piece)
                out = mx.concatenate(pieces, axis=-2)
            mx.eval(out)
            seconds = time.perf_counter() - started
            peak = mx.get_peak_memory() / 1048576.0
            rows.append({"head_dim": head_dim, "how": how,
                         "peak_over_base_mb": round(peak - base, 1),
                         "ms": round(seconds * 1000.0, 1),
                         "queries": queries, "keys": keys, "heads": heads})
            if how == "whole":
                whole = out
            else:
                difference = float(mx.max(mx.abs(out - whole)).item())
                rows[-1]["max_abs_diff"] = difference
                whole = None
            del out
        del q, k, v
        mx.clear_cache()
    return rows


def print_shapes(rows: list[dict]) -> None:
    print(f"\n{'head_dim':>9}{'how':>14}{'peak over base':>17}"
          f"{'ms':>9}{'max abs diff':>15}")
    for row in rows:
        difference = row.get("max_abs_diff")
        shown = "reference" if difference is None else f"{difference:.3e}"
        print(f"{row['head_dim']:>9}{row['how']:>14}"
              f"{row['peak_over_base_mb']:>14.0f} MB{row['ms']:>9.0f}"
              f"{shown:>15}")
    print("\nMLX's fused attention kernel covers head_dim 64, 80 and 128. At "
          "32 it\nfalls back and materialises the whole matrix, which is why "
          "the peak at\nhead_dim 32 is hundreds of times the peak at 64 for "
          "the same work.")


def measure(backend: MlxBackend, frames, box, width, height, mx,
            meter: memstat.StageMeter) -> dict:
    """A fresh session over `frames`, every frame measured on its own.

    Two numbers come out, because a track has two different costs. Frame 0 is
    the interactive frame: the box prompt, no tracker memory yet. Every frame
    after it cross attends to the last SIX memory frames, so the memory
    attention only reaches its real size once six of them exist: 5184 queries
    over 6 x 5184 keys, not over 5184. A two frame probe therefore reads about
    5 GB where a real window reads 13 GB, which is why this runs enough frames
    to fill the memory and reports the worst of them.
    """
    import numpy as np

    session = backend._session                                     # noqa: SLF001
    mx.clear_cache()
    base = mx.get_active_memory() / 1048576.0
    state = session.start_session(frames=list(frames))
    out = {"active_base_mb": round(base, 1), "masks": {}, "per_frame": []}
    try:
        session.add_prompt(state.session_id, frame_index=0, object_id=0,
                           box=[box[0] * width, box[1] * height,
                                box[2] * width, box[3] * height])
        state.memories.clear()
        last = len(frames) - 1
        for index in range(len(frames)):
            meter.reset()
            with meter.stage(f"frame{index}"):
                result = backend._run_frame(session, state, index)  # noqa: SLF001
            row = meter.rows()[0]
            record = {"index": index,
                      "memories": len(state.memories),
                      "mlx_peak_mb": row["mlx_peak_mb"],
                      "footprint_peak_mb": row["footprint_peak_mb"],
                      "seconds": round(row["seconds"], 2),
                      "score": round(float(result.tracks.scores[0]), 6)}
            out["per_frame"].append(record)
            if index in (0, last):
                out["masks"][f"frame{index}"] = np.asarray(
                    result.masks.data[0], dtype=np.float32)
            result = None
        propagated = out["per_frame"][1:] or out["per_frame"]
        out["frame0_interactive"] = out["per_frame"][0]
        out["worst_propagated"] = max(propagated,
                                      key=lambda r: r["mlx_peak_mb"])
        out["seconds_per_frame"] = round(
            sum(r["seconds"] for r in propagated) / len(propagated), 2)
    finally:
        backend._drop_state(session, state)                        # noqa: SLF001
        mx.clear_cache()
    return out


def compare(reference: dict, candidate: dict) -> dict:
    """Did the candidate change the answer? Pixel differences and IoU against
    the unpatched run, per frame."""
    import numpy as np

    out = {}
    for label, base_mask in (reference.get("masks") or {}).items():
        other = (candidate.get("masks") or {}).get(label)
        if other is None or other.shape != base_mask.shape:
            out[label] = {"comparable": False}
            continue
        a, b = base_mask > 0.5, other > 0.5
        union = int(np.logical_or(a, b).sum())
        out[label] = {
            "comparable": True,
            "max_abs_diff": float(np.max(np.abs(base_mask - other))),
            "pixels_differing": int(np.logical_xor(a, b).sum()),
            "iou": 1.0 if union == 0 else
                   round(float(np.logical_and(a, b).sum()) / union, 6),
        }
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--clip", default=str(CLIP))
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--frames", type=int, default=8,
                        help="frames per candidate. At least 7: the tracker's "
                             "memory attention only reaches its real size once "
                             "six memory frames exist, and that attention is "
                             "the whole point of this probe.")
    parser.add_argument("--text", default="person")
    parser.add_argument("--only", default=None,
                        help="comma separated candidate names to run")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--sample-ms", type=float, default=3.0)
    parser.add_argument("--lock-timeout-s", type=float, default=60.0)
    parser.add_argument("--tag", default="frames")
    parser.add_argument("--shapes", action="store_true",
                        help="measure the attention shapes on their own and "
                             "stop. No model, no weights, no lock, seconds.")
    args = parser.parse_args(argv)

    if args.shapes:
        import mlx.core as mx
        print_shapes(shape_probe(mx))
        return 0

    wanted = None if not args.only else {v.strip() for v in args.only.split(",")}
    candidates = [c for c in CANDIDATES if wanted is None or c["name"] in wanted]
    if not candidates:
        raise SystemExit(f"no candidate matches {args.only!r}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = OUT / f"frame-probe-{args.tag}-{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    if args.proxy:
        proxy = Path(args.proxy)
        if not proxy.is_file():
            raise SystemExit(f"there is no proxy at {proxy}")
    else:
        clip = Path(args.clip)
        if not clip.is_file():
            raise SystemExit(f"there is no clip at {clip}")
        proxy = make_proxy(clip, run_dir / "proxy.mp4", args.width,
                           args.frames + 2)

    lock = ModelLock(backend="mlx (spike/frame_probe.py)", log=say)
    if not lock.acquire(timeout_s=args.lock_timeout_s):
        info = owner() or {}
        who = (f"pid {info.get('pid')} ({info.get('backend')}, since "
               f"{info.get('since')})") if info else \
            "a process that left no owner file"
        say(f"the machine wide model lock is held by {who}. Nothing was "
            f"started and nothing was killed. Run this when it is free.")
        return 3

    result = {"tag": args.tag, "proxy": str(proxy), "width": args.width,
              "rows": [],
              "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        import mlx.core as mx

        # The backend applies its own boundary at load; take it off so the
        # candidates below are the only thing under test.
        backend = MlxBackend(layer_eval=False, log=say)
        backend.load()
        backend._remove_layer_eval()                               # noqa: SLF001
        unwrap_all()
        source = FrameSource(proxy)
        frames = list(source.frames(0, args.frames))
        if len(frames) < 2:
            raise SystemExit(f"{proxy} gave only {len(frames)} frame(s)")
        height, width = frames[0].shape[:2]
        found = backend._processor.predict(frames[0], args.text)   # noqa: SLF001
        if not len(found.detections):
            raise SystemExit(f"the detector found no {args.text!r} on frame 0")
        import numpy as np

        best = int(np.argmax(np.asarray(found.detections.scores)))
        raw = np.asarray(found.detections.boxes[best], dtype=np.float64)
        box = (raw[0] / width, raw[1] / height, raw[2] / width, raw[3] / height)
        found = None
        say(f"seeded from a detected box at {tuple(round(v, 3) for v in box)}")

        meter = memstat.stage_meter(mx=mx, enabled=True, sample_ms=args.sample_ms)
        meter.start()
        reference = None
        for candidate in candidates:
            name = candidate["name"]
            unwrap_all()
            backend._remove_attention_chunk()                      # noqa: SLF001
            for target in candidate["targets"]:
                wrap(target, mx)
            backend.attention_chunk = candidate["chunk"]
            if candidate["chunk"]:
                backend._apply_attention_chunk(mx)                 # noqa: SLF001
            for repeat in range(args.repeat):
                row = measure(backend, frames, box, width, height, mx, meter)
                row["boundaries"] = name
                row["repeat"] = repeat
                if reference is None:
                    # Keep the reference masks out of `row`, which has its own
                    # copy dropped below: they are the same objects, and
                    # popping them from the row would empty the reference too.
                    reference = {"masks": dict(row["masks"])}
                row["vs_reference"] = compare(reference, row)
                zero = row["frame0_interactive"]
                one = row["worst_propagated"]
                same = all(v.get("comparable") and v["pixels_differing"] == 0
                           for v in row["vs_reference"].values())
                say(f"{name:<18} seed mlx {zero['mlx_peak_mb']:>7.0f} MB "
                    f"fp {zero['footprint_peak_mb']:>7.0f} MB   worst frame "
                    f"{one['index']} ({one['memories']} memories) mlx "
                    f"{one['mlx_peak_mb']:>7.0f} MB fp "
                    f"{one['footprint_peak_mb']:>7.0f} MB   "
                    f"{row['seconds_per_frame']:>5.2f} s/frame   masks "
                    f"{'identical' if same else 'CHANGED'}")
                row.pop("masks", None)
                result["rows"].append(row)
        meter.stop()
        unwrap_all()
        backend._remove_attention_chunk()                          # noqa: SLF001
        backend.close()
    finally:
        lock.release()
        say("model lock released")

    print()
    head = (f"{'boundaries':<18}{'seed mlx MB':>13}{'seed fp MB':>12}"
            f"{'worst mlx MB':>14}{'worst fp MB':>13}{'s/frame':>9}"
            f"{'mask px changed':>17}")
    print(head)
    print("-" * len(head))
    for row in result["rows"]:
        zero, one = row["frame0_interactive"], row["worst_propagated"]
        changed = sum(v.get("pixels_differing", 0)
                      for v in row.get("vs_reference", {}).values())
        print(f"{row['boundaries']:<18}{zero['mlx_peak_mb']:>13.0f}"
              f"{zero['footprint_peak_mb']:>12.0f}"
              f"{one['mlx_peak_mb']:>14.0f}{one['footprint_peak_mb']:>13.0f}"
              f"{row['seconds_per_frame']:>9.2f}{changed:>17}")
    (run_dir / "result.json").write_text(json.dumps(result, indent=2, default=str))
    print(f"\nwritten: {run_dir / 'result.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
