"""The MLX backend: SAM 3.1 through mlx-cv on this Mac's GPU.

Everything odd in here comes from a fact the spike established, and the
comment says which one. In short (SPIKE.md has the evidence):

* Load by the exact repo id `appautomaton/sam3.1-multiplex-bf16-mlx`. The
  model card's `"sam3.1"` alias does not resolve: mlx-cv 0.0.4 ships an empty
  alias table.
* `SAM3Processor.predict(image, text)` is the only text entry point and it is
  images only. `SAM3VideoSession` accepts a box, points or a mask and has no
  text parameter at all. So a text tracked matte is two steps: detect on the
  seed frame, then seed the tracker with the detected box.
* `add_prompt`'s own default object id is 1 based while `propagate_in_video`
  looks ids up in a 0 based table, so the library's default crashes its own
  propagation. Object ids are always passed explicitly here, from 0 upward in
  insertion order.
* `SAM3VideoSession.model.detector` is a complete detector, so the processor
  is built around the already loaded video model rather than loading the
  1.75 GB checkpoint a second time.
* `propagate_in_video` is, verbatim, "clear the memories, then call
  `_run_frame` for each index". This backend runs that loop itself so a job
  can report progress per frame, write matte frames as they are computed, and
  stop when someone presses cancel. If a future mlx-cv drops `_run_frame`,
  `_propagate_public` does the same work through the public call and emits
  every frame at the end (correct, just blind until it finishes).
* mlx-cv preprocesses every frame to 1008x1008 float32, which is 12.2 MB of
  session state per frame. A whole 21 s clip would be 6.4 GB on a 16 GB
  machine, so the clip is tracked in chunks with a one frame overlap: each
  chunk is seeded from the mask of the frame it shares with the chunk before
  it, which is a real conditioning frame rather than a guess.
* **MLX's allocator cache, not the session, was what filled the machine.**
  Windowing bounds what the session holds, and it did; the service still grew
  to 13 GB because every Metal buffer MLX frees goes into a reuse cache whose
  default limit on this Mac is 15564.8 MB, i.e. the whole machine. Measured
  here with no model at all (`spike/mlx_cache_probe.py`): churning 40 buffers
  of about 16 MB, one live at a time, takes the process footprint to 696 MB
  with the default limit and to 60.7 MB with the cache capped, an 11x
  difference for the identical work. `set_cache_limit` at load is therefore
  the fix; `clear_cache()` between windows is the mop, and it does return the
  memory to the OS, just not instantly (about 3 s later on this machine),
  which is why a reading taken right after it looks like it did nothing.
"""

from __future__ import annotations

import gc
import time
from typing import Any, Iterator

import numpy as np

from .base import (Backend, BackendError, BackendUnavailable, Instance,
                   TrackedMask, mask_area, mask_box, normalize_prompts,
                   mlx_memory, plan_objects, stage_meter, window_meter)

# `next(iterator, _DONE)` rather than a for loop, so the decode of one frame
# can be timed and measured on its own without a stage context wrapping the
# yield as well. Nothing else in this file uses it.
_DONE = object()

REPO_ID = "appautomaton/sam3.1-multiplex-bf16-mlx"
# The spike tracked 120 frames of 720 wide material in one session: 2200 s,
# 0.055 frames/s, peak 14 GB on a 16 GB machine, because a session's memory
# grows with every frame it holds (1008x1008 float32 is about 12 MB a frame
# before the tracker's own memories). So a clip is tracked in windows, and
# each window's session is dropped before the next one is built.
CHUNK_FRAMES = 48          # 48 frames of 1008x1008 float32 is about 580 MB
SCORE_THRESHOLD = 0.5      # mlx-cv's own default; below it predict() returns nothing

# How much freed Metal memory MLX may keep for reuse, in MB. The default it
# picks on this Mac is 15564.8 MB (1.5 times the GPU's 12124 MB recommended
# working set), which is more than the machine has, so the cache is bounded by
# nothing and the process grows until macOS starts compressing and swapping.
# 1024 MB is chosen against the two sizes that matter: the model is about
# 1.7 GB of live weights and a 48 frame window's own live set is roughly a
# gigabyte, so a gigabyte of reusable buffers keeps the hot shapes warm and
# still leaves the whole service inside about 4 GB on a 16 GB machine that is
# also running the studio. Lower it (0 disables reuse entirely) if something
# else needs the room; raise it only with a measurement in hand.
CACHE_LIMIT_MB = 1024
# 0 means "the GPU's own max recommended working set" (12124 MB here), which
# is the point past which Metal itself starts paging. MLX treats the memory
# limit as the level at which it reclaims from its cache before allocating,
# not as a hard failure, so this is a guard rail rather than a cap.
MEMORY_LIMIT_MB = 0

# THE TRANSIENT: one attention matrix, 5.2 GB, twice over.
#
# The service's steady state is bounded (about 4.2 GB a window) but its high
# water mark read 13.7 GB per window, which on a 16 GB Mac is a swap event
# every window. Instrumenting every stage of a window (spike/stage_memory.py)
# put all of it in one stage, `_run_frame`, and put the same number there for
# a 16 frame window as for a 48 frame one:
#
#     stage                    calls   mlx peak MB   footprint peak MB
#     seed detect                  1          4788                4973
#     ingest (pixel_values)        1             0                4954
#     propagate (_run_frame)      48         13362               14272
#     mask post / png write       48             0                4170
#
# So it never was the window: it is one frame, and the previous lane's
# "it grows with the window" (7.6 GB at 8 frames, 13.7 GB at 48) was a per
# frame sampler catching the same spike more often on a longer run.
#
# Inside that frame it is the tracker's memory attention. `_DecoupledLayer`
# cross attends the frame's 5184 image tokens to six memory frames of 5184
# tokens each, 8 heads, head_dim 256/8 = 32, through
# `mx.fast.scaled_dot_product_attention`. MLX's fused (flash) kernel supports
# head_dim 64, 80 and 128; at 32 it falls back to composing the thing by hand,
# which means it really does allocate 8 x 5184 x 31360 x 4 bytes = 5.2 GB for
# the attention weights and another 5.2 GB for the softmax of them. Measured
# with no model at all (spike-shaped probe, q 2048, k 12288, 8 heads):
#
#     head_dim 32    mx.fast.sdpa   peak  810 MB over base    792 ms
#     head_dim 32    chunked        peak  204 MB over base     93 ms
#     head_dim 64    mx.fast.sdpa   peak    4 MB over base    291 ms
#
# The fix is to run that attention in blocks of queries. A softmax is over the
# KEY axis, so every query row is computed from exactly the same numbers no
# matter how the query axis is split: the result is bit identical (measured,
# max abs difference 0.000e+00, on the probe and on the real model's masks),
# and the peak falls by the block factor. It is also faster: 142 frames took
# 524 s with the blocks against 681 s without them (-23%), because the fallback
# path is slow as well as large. Measured end to end at 1280 wide, the numbers
# are in plan/2026-09-08-studio-masks/checkpoints/FIX-MEMORY-2.md section 4.
ATTENTION_CHUNK = 512
# Only chunk when the matrix MLX would otherwise allocate is worth chunking.
# Below this the fallback is small enough that the extra concatenate is the
# more expensive half.
ATTENTION_CHUNK_MIN_MB = 256

# The other thing tried, kept because it is the obvious next idea and someone
# will have it again: an `mx.eval` after every ViT trunk layer, to stop MLX
# building the whole 32 layer graph before evaluating any of it. Measured on
# the real model it moved the window's peak by nothing at all (13324 MB
# without it, 13315 MB with it) and made a 16 frame window 23% slower
# (52 s to 65 s), because the trunk was never where the memory went. So it is
# OFF, and `--mlx-layer-eval` turns it on for anyone who wants to see that for
# themselves.
LAYER_EVAL = False
# Where that boundary goes. Written as (module, class) rather than an import
# so a future mlx-cv that renames it logs one line and carries on rather than
# failing to load the model at all.
EVAL_BOUNDARY_CLASSES = (
    ("mlx_cv.models.sam3.real_vision", "Sam3ViTLayer"),
)


def _log(message: str) -> None:
    print(message, flush=True)


class MlxBackend(Backend):
    name = "mlx"

    def __init__(self, repo_id: str = REPO_ID, chunk_frames: int = CHUNK_FRAMES,
                 score_threshold: float = SCORE_THRESHOLD,
                 cache_limit_mb: int = CACHE_LIMIT_MB,
                 memory_limit_mb: int = MEMORY_LIMIT_MB,
                 layer_eval: bool = LAYER_EVAL,
                 attention_chunk: int = ATTENTION_CHUNK, log=_log):
        self.model = repo_id
        self.repo_id = repo_id
        self.chunk_frames = max(2, int(chunk_frames))
        self.score_threshold = float(score_threshold)
        self.cache_limit_mb = int(cache_limit_mb)
        self.memory_limit_mb = int(memory_limit_mb)
        self.layer_eval = bool(layer_eval)
        self.attention_chunk = int(attention_chunk)
        self.limits: dict = {}
        self._log = log
        self._session = None
        self._processor = None
        self._mx = None
        self.meter = window_meter(log=log)
        # Off by default: `stage()` then returns a shared do-nothing object,
        # so an unmeasured run pays one attribute read per stage and makes no
        # syscall. `spike/stage_memory.py` turns it on.
        self.stages = stage_meter(log=None, enabled=False)

    # -- lifecycle ---------------------------------------------------------

    def load(self) -> None:
        """The machine wide model lock is NOT taken here: the service holds it
        for as long as the model is resident (see sam/server.py). A script
        using this class directly must take it itself."""
        try:
            import mlx.core as mx
            from mlx_cv.hub import resolve_pretrained
            from mlx_cv.models.sam3 import SAM3Processor, SAM3VideoSession
        except ImportError as exc:
            raise BackendUnavailable(
                f"mlx-cv is not installed in this environment ({exc}). "
                "Install it with: uv sync --project sam --extra mlx"
            ) from exc

        started = time.perf_counter()
        try:
            snapshot = resolve_pretrained(self.repo_id)
        except Exception as exc:                               # noqa: BLE001
            raise BackendUnavailable(
                f"cannot resolve the SAM 3.1 weights {self.repo_id!r}: {exc}. "
                "They live in the Hugging Face cache; the machine may be "
                "offline and the cache empty."
            ) from exc
        try:
            session = SAM3VideoSession.from_pretrained(self.repo_id)
        except Exception as exc:                               # noqa: BLE001
            raise BackendUnavailable(
                f"SAM 3.1 weights would not load through mlx-cv: {exc}") from exc

        bpe = snapshot / "bpe_simple_vocab_16e6.txt.gz" if snapshot.is_dir() else None
        if bpe is None or not bpe.is_file():
            raise BackendUnavailable(
                f"the text tokenizer vocabulary is missing from {snapshot}: "
                "text prompts cannot work without bpe_simple_vocab_16e6.txt.gz")
        # One checkpoint, one load: the video session's `detector` IS the
        # text detector, so the processor wraps it instead of loading again.
        processor = SAM3Processor(session.model.detector, bpe_path=bpe,
                                  score_threshold=self.score_threshold)

        self._mx = mx
        self._session = session
        self._processor = processor
        self.meter.mx = mx
        self.stages.mx = mx
        self.limits = self._apply_limits(mx)
        self.limits["layer_eval"] = self._apply_layer_eval(mx)
        self.limits["attention_chunk"] = self._apply_attention_chunk(mx)
        self._log(f"[mlx] loaded {self.repo_id} in {time.perf_counter() - started:.2f}s "
                  f"(device {mx.default_device()})")

    # -- memory ------------------------------------------------------------

    def _apply_limits(self, mx) -> dict:
        """Bound MLX's allocator before any work runs.

        The defaults MLX picks are sized for a machine that has nothing else
        on it. This one runs the studio, ffmpeg and a browser, so the cache
        gets a real limit and the memory limit is pulled back to what the GPU
        itself calls its recommended working set. Both are logged with the
        values they replaced, because "why is it 13 GB" should be answerable
        from the log rather than from a memory of what the defaults were.
        """
        try:
            device = dict(mx.device_info())
        except Exception:                                      # noqa: BLE001
            device = {}
        recommended = int(device.get("max_recommended_working_set_size") or 0)
        out = {"device": device.get("device_name"),
               "recommended_working_set_mb": round(recommended / 1048576, 1)
               if recommended else None}

        def apply(name: str, value_mb: int | None, key: str) -> None:
            fn = getattr(mx, name, None)
            if not callable(fn):
                out[key] = None
                return
            if value_mb is None:                # leave MLX's own default
                try:
                    previous = fn(1 << 40)      # set high, read back, restore
                    fn(previous)
                except Exception:                              # noqa: BLE001
                    previous = None
                out[key + "_mb"] = round(previous / 1048576, 1) if previous else None
                out[key + "_source"] = "mlx default"
                return
            try:
                previous = fn(int(value_mb) * 1048576)
            except Exception as exc:                           # noqa: BLE001
                self._log(f"[mlx] could not set {name}: {exc}")
                return
            out[key + "_mb"] = float(value_mb)
            out[key + "_was_mb"] = round(previous / 1048576, 1) if previous else None
            out[key + "_source"] = "service"
            self._log(f"[mlx] {name} {value_mb} MB "
                      f"(was {round(previous / 1048576, 1) if previous else '?'} MB)")

        apply("set_cache_limit",
              None if self.cache_limit_mb < 0 else self.cache_limit_mb, "cache_limit")
        memory_mb = self.memory_limit_mb
        if memory_mb == 0:
            memory_mb = round(recommended / 1048576) if recommended else None
        elif memory_mb < 0:
            memory_mb = None
        apply("set_memory_limit", memory_mb, "memory_limit")
        return out

    def _apply_attention_chunk(self, mx) -> dict:
        """Run a big attention in blocks of queries instead of all at once.

        See ATTENTION_CHUNK above for why: at the head_dim this model uses,
        MLX's fused attention kernel does not apply and the fallback allocates
        the whole 5.2 GB attention matrix. Splitting the QUERY axis cannot
        change a number (a softmax runs along the key axis, so each query row
        sees exactly the same keys either way), and it divides the transient by
        the number of blocks.

        This replaces `mx.fast.scaled_dot_product_attention` for the whole
        process, because mlx-cv reaches it through `mx.fast` at call time and
        there is no seam closer to the model. Anything that does NOT need
        chunking (a small matrix, a mask, a query axis shorter than one block)
        goes straight to the original call, so a path that was already using
        the fused kernel is untouched.
        """
        fast = getattr(mx, "fast", None)
        original = getattr(fast, "scaled_dot_product_attention", None)
        out = {"chunk": self.attention_chunk,
               "min_matrix_mb": ATTENTION_CHUNK_MIN_MB,
               "enabled": bool(self.attention_chunk > 0 and callable(original))}
        if not out["enabled"]:
            if not callable(original):
                self._log("[mlx] mx.fast.scaled_dot_product_attention is not "
                          "there to chunk; the tracker's memory attention will "
                          "allocate its whole matrix")
            return out
        if getattr(original, "_fixxr_chunked", False):
            return out                      # already patched in this process
        chunk = self.attention_chunk
        threshold = ATTENTION_CHUNK_MIN_MB * 1048576
        log = self._log
        counter = {"chunked": 0, "whole": 0}

        def chunked(queries, keys, values, **kwargs):
            try:
                heads = int(queries.shape[-3])
                query_length = int(queries.shape[-2])
                key_length = int(keys.shape[-2])
            except Exception:                                  # noqa: BLE001
                return original(queries, keys, values, **kwargs)
            matrix = heads * query_length * key_length * 4
            if (query_length <= chunk or matrix < threshold
                    or kwargs.get("mask") is not None):
                # A mask would have to be sliced along the query axis too, and
                # nothing in this model passes one; refusing to chunk is the
                # safe answer rather than a clever one.
                counter["whole"] += 1
                return original(queries, keys, values, **kwargs)
            if not counter["chunked"]:
                log(f"[mlx] attention in blocks of {chunk} queries: "
                    f"{heads} heads x {query_length} x {key_length} would be "
                    f"{matrix / 1048576:.0f} MB in one piece")
            counter["chunked"] += 1
            pieces = []
            for start in range(0, query_length, chunk):
                piece = original(queries[..., start:start + chunk, :],
                                 keys, values, **kwargs)
                # Without this the blocks stay lazy and MLX materialises all
                # of them at the end, which is the thing being avoided.
                mx.eval(piece)
                pieces.append(piece)
            return mx.concatenate(pieces, axis=-2)

        chunked._fixxr_chunked = True                          # noqa: SLF001
        chunked._fixxr_original = original                     # noqa: SLF001
        chunked._fixxr_counter = counter                       # noqa: SLF001
        fast.scaled_dot_product_attention = chunked
        self._log(f"[mlx] scaled_dot_product_attention will run in blocks of "
                  f"{chunk} queries when the attention matrix would be over "
                  f"{ATTENTION_CHUNK_MIN_MB} MB")
        return out

    @staticmethod
    def _remove_attention_chunk() -> bool:
        """Undo `_apply_attention_chunk`. The memory spike uses this to
        measure both behaviours in one process."""
        import mlx.core as mx

        fast = getattr(mx, "fast", None)
        current = getattr(fast, "scaled_dot_product_attention", None)
        original = getattr(current, "_fixxr_original", None)
        if original is None:
            return False
        fast.scaled_dot_product_attention = original
        return True

    def _apply_layer_eval(self, mx) -> dict:
        """Put an evaluation boundary after every ViT trunk layer.

        See LAYER_EVAL above for why. This wraps the class, not an instance,
        because mlx-cv builds the 32 layers as a plain list inside
        `Sam3ViTModel` and rebinding the list would take them out of the
        module's parameter tree. The wrap is idempotent (a second `load()` in
        the same process does nothing) and reversible (`_remove_layer_eval`,
        which the memory spike uses to measure the two behaviours back to back
        in one process rather than two model loads).
        """
        out = {"enabled": bool(self.layer_eval), "wrapped": [],
               "missing": []}
        if not self.layer_eval:
            return out
        import importlib

        for module_name, class_name in EVAL_BOUNDARY_CLASSES:
            try:
                target = getattr(importlib.import_module(module_name), class_name)
            except Exception as exc:                           # noqa: BLE001
                # A renamed class means the graph is unbounded again, which is
                # slow and memory hungry but still correct, so it is a log line
                # and not a failure to load the model.
                self._log(f"[mlx] no {module_name}.{class_name} to bound the "
                          f"lazy graph at ({exc}); the ViT trunk will build "
                          f"its whole graph before evaluating it")
                out["missing"].append(class_name)
                continue
            out["wrapped"].append(class_name)
            if getattr(target, "_fixxr_layer_eval", False):
                continue
            original = target.__call__

            def wrapped(layer_self, *args, _original=original, **kwargs):
                result = _original(layer_self, *args, **kwargs)
                mx.eval(result)
                return result

            wrapped._fixxr_original = original                 # noqa: SLF001
            target.__call__ = wrapped
            target._fixxr_layer_eval = True
            self._log(f"[mlx] lazy graph bounded at {class_name}: mx.eval "
                      f"after every layer")
        return out

    @staticmethod
    def _remove_layer_eval() -> list[str]:
        """Undo `_apply_layer_eval`. Only the memory spike calls this."""
        import importlib

        undone = []
        for module_name, class_name in EVAL_BOUNDARY_CLASSES:
            try:
                target = getattr(importlib.import_module(module_name), class_name)
            except Exception:                                  # noqa: BLE001
                continue
            original = getattr(target.__call__, "_fixxr_original", None)
            if original is None:
                continue
            target.__call__ = original
            target._fixxr_layer_eval = False
            undone.append(class_name)
        return undone

    def memory(self) -> dict:
        """MLX's own numbers plus the limits in force, for /health."""
        out = dict(mlx_memory(self._mx))
        out["limits"] = self.limits
        return out

    def release(self) -> None:
        """Everything this backend can give back without unloading the model.

        Called between jobs by the service. `close()` is the harder version
        that drops the model too.
        """
        gc.collect()
        self._clear_cache()

    def close(self) -> None:
        self._session = None
        self._processor = None
        gc.collect()
        self._clear_cache()
        self._mx = None
        self.meter.mx = None
        self.stages.mx = None

    def _clear_cache(self) -> None:
        """Hand MLX's reuse cache back to the OS.

        `reset_peak_memory` is deliberately NOT called here: it used to be,
        and it meant `get_peak_memory()` reported the peak of whatever had
        happened since the last window rather than the peak of the job, so
        the one number that would have shown this problem always read low.
        The peak is reset once per window, after it has been recorded.
        """
        mx = self._mx
        if mx is None:
            return
        fn = getattr(mx, "clear_cache", None)
        if callable(fn):
            try:
                fn()
            except Exception:                                  # noqa: BLE001
                pass

    def _reset_peak(self) -> None:
        mx = self._mx
        if mx is None:
            return
        fn = getattr(mx, "reset_peak_memory", None)
        if callable(fn):
            try:
                fn()
            except Exception:                                  # noqa: BLE001
                pass

    def _ready(self):
        if self._session is None or self._processor is None:
            raise BackendError("the MLX backend is not loaded")
        return self._session, self._processor

    # -- one frame ---------------------------------------------------------

    def segment(self, image: np.ndarray, prompts: dict,
                max_instances: int) -> list[Instance]:
        session, processor = self._ready()
        prompts = normalize_prompts(prompts)
        if not any(prompts[k] for k in ("text", "points", "boxes", "masks")):
            raise ValueError("segment needs at least one of prompts.text, "
                             "prompts.points, prompts.boxes")
        image = np.ascontiguousarray(image)
        height, width = image.shape[:2]
        slots = plan_objects(prompts, max_instances=max_instances)
        out: list[Instance] = []

        # Text: the detector, one call per phrase (SAM 3.1 segments instances
        # of ONE phrase per call; it is not a multi class detector).
        by_phrase: dict[int, list] = {}
        for slot in slots:
            if slot.kind == "text":
                by_phrase.setdefault(slot.phrase_index, []).append(slot)
        for phrase_index, phrase_slots in sorted(by_phrase.items()):
            phrase = phrase_slots[0].text
            found = processor.predict(image, phrase)
            order = np.argsort(-np.asarray(found.detections.scores)) \
                if len(found.detections) else []
            for slot, detection_index in zip(phrase_slots, order):
                mask = np.asarray(found.masks.data[int(detection_index)], dtype=np.float32)
                box = np.asarray(found.detections.boxes[int(detection_index)], dtype=np.float64)
                out.append(Instance(
                    id=slot.id,
                    score=float(found.detections.scores[int(detection_index)]),
                    box=(float(box[0]) / width, float(box[1]) / height,
                         float(box[2]) / width, float(box[3]) / height),
                    area=mask_area(mask),
                    mask=mask,
                    label=phrase,
                    prompt={"kind": "text", "text": phrase,
                            "instance": slot.instance_index},
                ))

        # Boxes, points and mask prompts: the video session on one frame. The
        # image predictor has no point or box entry point at all.
        geometric = [s for s in slots if s.kind in ("box", "points", "mask")]
        if geometric:
            state = session.start_session(frames=[image])
            try:
                added = [slot for slot in geometric
                         if self._add_prompt(session, state, slot, 0, width, height)]
                result = self._run_frame(session, state, 0)
                masks, scores = self._frame_masks(result, added)
                for slot in added:
                    mask = masks.get(slot.id)
                    if mask is None:
                        continue
                    out.append(Instance(
                        id=slot.id, score=float(scores.get(slot.id, 0.0)),
                        box=mask_box(mask), area=mask_area(mask), mask=mask,
                        label=slot.label, prompt=slot.as_dict()))
            finally:
                self._drop_state(session, state)
                state = None
                result = masks = scores = None
                gc.collect()
                self._clear_cache()

        out.sort(key=lambda inst: -inst.score)
        return out

    @staticmethod
    def _drop_state(session, state) -> None:
        """Let go of one video session's memory now, not at the next garbage
        collection. `pixel_values` alone is 585 MB for a 48 frame window
        (1008x1008 float32 x 3 channels x 48) and `memories` holds up to 16
        frames of Metal tensors; leaving either to a reference cycle means the
        next window is built on top of the last one."""
        if state is None:
            return
        try:
            session.sessions.pop(state.session_id, None)
        except Exception:                                      # noqa: BLE001
            pass
        for attribute in ("memories", "prompts", "active_object_ids"):
            try:
                getattr(state, attribute).clear()
            except Exception:                                  # noqa: BLE001
                pass
        try:
            state.pixel_values = None
            state.context = None
        except Exception:                                      # noqa: BLE001
            pass

    # -- a clip ------------------------------------------------------------

    def track(self, frames: Iterator[np.ndarray], fps: float, prompts: dict,
              select: Any, on_frame) -> None:
        session, processor = self._ready()
        prompts = normalize_prompts(prompts)
        slots = plan_objects(prompts,
                             max_instances=prompts.get("max_instances") or 1,
                             select=select)
        if not slots:
            raise ValueError("track needs at least one prompt")

        iterator = iter(frames)
        try:
            seed_frame = next(iterator)
        except StopIteration:
            return
        seed_frame = np.ascontiguousarray(seed_frame)
        height, width = seed_frame.shape[:2]

        with self.stages.stage("seed detect"):
            live = self._seed_slots(processor, seed_frame, slots, width, height)
        if not live:
            raise BackendError(
                "the detector found nothing for any of these prompts on the "
                "seed frame, so there is nothing to track")

        last_mask: dict[str, np.ndarray] = {}
        for chunk_start, chunk in self._chunks(iterator, seed_frame):
            # What the service reports on /health, so a person watching a slow
            # job can see which window of the clip is loaded right now.
            chunk_frames_here = len(chunk)
            self.window = {"start": chunk_start, "end": chunk_start + chunk_frames_here,
                           "frames": chunk_frames_here, "size": self.chunk_frames}
            self.meter.start(start=chunk_start, end=chunk_start + chunk_frames_here,
                             frames=chunk_frames_here)
            state = None
            try:
                with self.stages.stage("ingest (pixel_values)"):
                    state = session.start_session(frames=chunk)
                # start_session has copied every frame into `pixel_values` at
                # 1008x1008. Holding the raw window as well is a second copy
                # of the same pictures (133 MB at 720 wide, 400 MB at 1280),
                # so it goes now. `_chunks` kept the overlap frame separately.
                chunk.clear()
                # Sampled at the seams as well as per frame. Measured on the
                # real model: the per frame readings sit at about 4.2 GB all
                # window, while the process high water mark on /health reaches
                # 7.6 GB for an 8 frame window and 13.7 GB for a 48 frame one.
                # So something between the frames costs multiples of a frame,
                # it grows with the window, and a meter that only looked at
                # frames would report the small number and call it bounded.
                # `memory.peak_footprint_mb` is the safety net that catches it.
                self.meter.sample()
                order = []
                with self.stages.stage("seed / add_prompt"):
                    for slot in live:
                        if chunk_start == 0:
                            seeded = self._add_prompt(session, state, slot, 0,
                                                      width, height)
                        else:
                            previous = last_mask.get(slot.id)
                            if previous is None or not previous.any():
                                continue      # lost the object; it stays lost
                            session.add_prompt(state.session_id, frame_index=0,
                                               object_id=len(state.active_object_ids),
                                               mask=previous)
                            seeded = True
                        if seeded:
                            order.append(slot)
                    # Object ids must be 0..n-1 in insertion order (mlx-cv
                    # 0.0.4 bug, see the module docstring), which is what
                    # `order` is.
                    state.memories.clear()
                if not order:
                    raise BackendError(
                        "every tracked object was lost before the end of the clip")
                self.meter.sample()      # seeding is the other seam cost
                for index in range(chunk_frames_here):
                    with self.stages.stage("propagate (_run_frame)"):
                        result = self._run_frame(session, state, index)
                    with self.stages.stage("mask post"):
                        masks, scores = self._frame_masks(result, order)
                        for slot_id, mask in masks.items():
                            if mask.any():
                                last_mask[slot_id] = mask
                    if index == 0 and chunk_start > 0:
                        result = masks = scores = None
                        continue          # the overlap frame, already emitted
                    with self.stages.stage("emit (png write)"):
                        on_frame(chunk_start + index,
                                 {slot_id: TrackedMask(mask, float(scores.get(slot_id, 0.0)))
                                  for slot_id, mask in masks.items()})
                    # Sampled per frame because MLX's allocator grows INSIDE a
                    # window; a reading taken only at the seam would show the
                    # tidy number and never the one that swaps the machine.
                    result = masks = scores = None
                    self.meter.sample()
            finally:
                # Everything this window held goes before the next one is
                # built: the session state, its preprocessed frames, the raw
                # frames, and MLX's own buffers. On a 16 GB machine the
                # difference between freeing here and freeing "eventually" is
                # the difference between tracking and swapping.
                result = masks = scores = None
                self.window = None
                self.meter.finish(freed=lambda held=state: self._free_window(session, held))
                state = None
                self._reset_peak()

    def _free_window(self, session, state) -> None:
        with self.stages.stage("window free"):
            self._drop_state(session, state)
            gc.collect()
            self._clear_cache()

    # -- the pieces --------------------------------------------------------

    def _chunks(self, iterator, first):
        """Chunks of frames with a one frame overlap, and the absolute index
        of each chunk's first frame.

        The list handed out is the caller's to empty: `track` clears it as
        soon as mlx-cv has copied the frames into its own session, and this
        generator keeps the overlap frame separately so that clearing is
        safe. Nothing here ever holds more than one window of pictures.
        """
        buffer = [first]
        start = 0
        while True:
            with self.stages.stage("decode + resize"):
                frame = next(iterator, _DONE)
                if frame is not _DONE:
                    buffer.append(np.ascontiguousarray(frame))
            if frame is _DONE:
                break
            if len(buffer) >= self.chunk_frames:
                tail, size = buffer[-1], len(buffer)
                yield start, buffer          # the caller may empty this list
                start += size - 1
                buffer = [tail]
                tail = None
        if len(buffer) > 1 or start == 0:
            yield start, buffer

    def _seed_slots(self, processor, seed_frame, slots, width, height) -> list:
        """Fill every text slot with a detected box; drop the ones the model
        cannot fill. Box, point and mask slots need no detection."""
        live: list = []
        by_phrase: dict[int, list] = {}
        for slot in slots:
            if slot.kind == "text":
                by_phrase.setdefault(slot.phrase_index, []).append(slot)
        for phrase_index, phrase_slots in sorted(by_phrase.items()):
            phrase = phrase_slots[0].text
            started = time.perf_counter()
            found = processor.predict(seed_frame, phrase)
            count = len(found.detections)
            self._log(f"[mlx] seed detect {phrase!r}: {count} instance(s) in "
                      f"{time.perf_counter() - started:.2f}s")
            if not count:
                continue
            order = np.argsort(-np.asarray(found.detections.scores))
            for slot, detection_index in zip(phrase_slots, order):
                box = np.asarray(found.detections.boxes[int(detection_index)],
                                 dtype=np.float64)
                slot.box = (float(box[0]) / width, float(box[1]) / height,
                            float(box[2]) / width, float(box[3]) / height)
                live.append(slot)
        for slot in slots:
            if slot.kind in ("box", "points", "mask"):
                live.append(slot)
        # Keep the caller's order, not the phrase grouping.
        position = {slot.id: i for i, slot in enumerate(slots)}
        live.sort(key=lambda slot: position[slot.id])
        return live

    def _add_prompt(self, session, state, slot, frame_index, width, height) -> bool:
        """One prompt for one object. Coordinates arrive as fractions and go
        to mlx-cv in the frame's own pixels."""
        object_id = len(state.active_object_ids)
        if slot.box is not None:
            x0, y0, x1, y1 = slot.box
            session.add_prompt(state.session_id, frame_index=frame_index,
                               object_id=object_id,
                               box=[x0 * width, y0 * height, x1 * width, y1 * height])
            return True
        if slot.points:
            coordinates = [[p["x"] * width, p["y"] * height] for p in slot.points]
            labels = [int(p.get("label", 1)) for p in slot.points]
            session.add_prompt(state.session_id, frame_index=frame_index,
                               object_id=object_id, points=coordinates, labels=labels)
            return True
        if slot.mask_path:
            from PIL import Image

            mask = np.asarray(Image.open(slot.mask_path).convert("L"),
                              dtype=np.float32) / 255.0
            if mask.shape[:2] != (height, width):
                mask = np.asarray(Image.fromarray((mask * 255).astype(np.uint8))
                                  .resize((width, height), Image.BILINEAR),
                                  dtype=np.float32) / 255.0
            session.add_prompt(state.session_id, frame_index=frame_index,
                               object_id=object_id, mask=mask)
            return True
        return False

    def _run_frame(self, session, state, index):
        runner = getattr(session, "_run_frame", None)
        if callable(runner):
            return runner(state, index)
        return self._propagate_public(session, state, index)

    def _propagate_public(self, session, state, index):
        """Fallback for a future mlx-cv without `_run_frame`: one public call
        per frame. Correct only for the first frame of a chunk, so it is used
        for a single frame segment and otherwise reports the whole chunk at
        once."""
        result = session.propagate_in_video(state.session_id,
                                            start_frame_index=index,
                                            max_frame_num_to_track=1)
        return result.frames[0]

    @staticmethod
    def _frame_masks(result, slots) -> tuple[dict[str, np.ndarray], dict[str, float]]:
        """mlx-cv returns masks in the order of `tracks.ids`, which are the
        0 based object ids we assigned in insertion order."""
        masks: dict[str, np.ndarray] = {}
        scores: dict[str, float] = {}
        ids = list(result.tracks.ids)
        for position, object_id in enumerate(ids):
            index = int(object_id)
            if index >= len(slots):
                continue
            slot = slots[index]
            masks[slot.id] = np.asarray(result.masks.data[position], dtype=np.float32)
            try:
                scores[slot.id] = float(result.tracks.scores[position])
            except Exception:                                  # noqa: BLE001
                scores[slot.id] = 0.0
        return masks, scores
