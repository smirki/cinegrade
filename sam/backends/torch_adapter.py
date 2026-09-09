"""Adapter: lane M1b's `TorchBackend` behind contract C7's slot ids.

M1b wrote `torch_backend.py` in parallel with this lane, before
`backends/base.py` existed, so it speaks in the ids `transformers` gives it:
plain integers, one per tracked object. The rest of this service speaks in
slot ids (`t0_0`, `b1`, `p0`) because those ids are what a matte is named
after, what `select` names, and what a pick returns. This file is the join
between the two, so neither lane has to rewrite the other's file.

What it does, and what it cannot do:

* `segment` calls the torch backend once per text phrase (its text path takes
  one phrase per call), plus once for boxes and once for points, and renames
  every returned instance to its slot id.
* `track` runs ONE group per call: boxes if there are any, else points, else
  the first text phrase. The torch path has no way to seed a box and a phrase
  in the same session, and a second pass would mean decoding the clip twice.
  Slots outside the group are left unfilled, and the service marks their
  mattes failed with the reason, rather than pretending they were tracked.
* The torch backend materialises whatever iterator it is handed
  (`list(frames)`), so this adapter never hands it the whole clip: it feeds
  it one window of frames at a time (`chunk_frames`, default 48) with a one
  frame overlap, and seeds each window after the first from the bounding box
  of the last mask of each object it is still holding. The spike measured a
  single session over 120 frames of 720 wide material at 14 GB peak on a
  16 GB machine, so an unwindowed track is not a slower path here, it is a
  path that takes the whole machine down with it. The cost is that a lost
  object stays lost at a window boundary and re-seeding is by box, which is
  coarser than a mask: MLX is still the better backend on this Mac.
* The torch backend takes the machine wide model lock inside `load()`. The
  service already holds that lock for the whole process, so this adapter
  neutralises the inner acquisition when it does; otherwise the process would
  wait forever for a lock it is holding itself.
"""

from __future__ import annotations

import gc
from typing import Any, Iterator

import numpy as np

from .base import (Backend, BackendError, BackendUnavailable, Instance,
                   TrackedMask, duty_cycle, mask_box, normalize_prompts,
                   plan_objects, window_meter)

CHUNK_FRAMES = 48


class TorchAdapter(Backend):
    # `torch_backend.track` takes text, points and boxes and refuses anything
    # else, so a reviewed pick cannot be seeded by its mask on this backend and
    # the service says so on the job instead (checkpoint gap 20).
    supports_mask_prompts = False
    # The inner backend reports no per frame confidence, so `split_mask_score`
    # falls back to the mask's own maximum: that is "the object is present",
    # not "the tracker is sure" (round 1 finding 29).
    score_kind = "presence"

    def __init__(self, device: str = "auto", repo_id: str | None = None,
                 outer_lock_held: bool = True, chunk_frames: int = CHUNK_FRAMES,
                 duty_cycle_fraction: float = 1.0, log=print):
        self._device = device
        self.chunk_frames = max(2, int(chunk_frames))
        self._repo_id = repo_id
        self._outer_lock_held = outer_lock_held
        self._log = log
        self._inner = None
        self.meter = window_meter(log=log)
        # Quiet mode, the same as the MLX backend: see sam/throttle.py. 1.0
        # (the default) never sleeps.
        self.throttle = duty_cycle(duty_cycle_fraction, log=log)
        self.name = "torch-mps" if device == "mps" else "torch-cpu"
        self.model = repo_id or "jetjodh/sam3"

    def load(self) -> None:
        try:
            from . import torch_backend as module
        except ImportError as exc:
            raise BackendUnavailable(f"the torch backend is not importable: {exc}") from exc
        try:
            import torch                                        # noqa: F401
            import transformers                                 # noqa: F401
        except ImportError as exc:
            raise BackendUnavailable(
                f"torch and transformers are not installed ({exc}). "
                "Install them with: uv sync --project sam --extra torch"
            ) from exc

        try:
            # `use_model_lock=False` when we are already inside the machine
            # wide lock, so the inner backend does not deadlock against
            # ourselves. This used to replace the module's two lock functions
            # with no-ops for the whole process and never put them back, which
            # left every later TorchBackend in that process unlocked (round 1
            # finding 47).
            self._inner = module.TorchBackend(
                device=self._device, repo_id=self._repo_id,
                use_model_lock=not self._outer_lock_held)
            self._inner.load()
        except Exception as exc:                                # noqa: BLE001
            raise BackendUnavailable(f"the torch backend would not load: {exc}") from exc
        self.name = getattr(self._inner, "name", self.name)
        self.model = getattr(self._inner, "repo_id", self.model)
        if self._device == "mps" and self.name != "torch-mps":
            raise BackendUnavailable("the Mac GPU (mps) is not usable here")

    def close(self) -> None:
        if self._inner is not None:
            self._inner.close()
            self._inner = None
        gc.collect()
        self._empty_cache()

    # -- memory ------------------------------------------------------------

    def _empty_cache(self) -> None:
        """Hand the Mac GPU's allocator cache back, the torch equivalent of
        MLX's `clear_cache`.

        torch's MPS allocator caches freed blocks exactly the way MLX's does,
        so a windowed track that never empties it grows for the same reason
        the MLX one did: the live set is one window, the process is every
        window. On CPU there is nothing to empty and this is a no op.
        """
        if self._device != "mps":
            return
        try:
            import torch

            torch.mps.empty_cache()
        except Exception:                                       # noqa: BLE001
            pass

    def memory(self) -> dict:
        """What torch says it is holding on the Mac GPU, in MB."""
        if self._device != "mps":
            return {"device": "cpu"}
        try:
            import torch

            return {
                "device": "mps",
                "active_mb": round(torch.mps.current_allocated_memory() / 1048576, 1),
                "driver_mb": round(torch.mps.driver_allocated_memory() / 1048576, 1),
            }
        except Exception:                                       # noqa: BLE001
            return {"device": "mps"}

    def release(self) -> None:
        gc.collect()
        self._empty_cache()

    # -- one frame ---------------------------------------------------------

    def segment(self, image: np.ndarray, prompts: dict,
                max_instances: int) -> list[Instance]:
        prompts = normalize_prompts(prompts)
        slots = plan_objects(prompts, max_instances=max_instances)
        out: list[Instance] = []

        boxes = [s for s in slots if s.kind == "box"]
        if boxes:
            found = self._inner.segment(
                image, {"boxes": [list(s.box) for s in boxes]}, len(boxes))
            for slot, raw in zip(boxes, found):
                out.append(self._instance(slot, raw))

        points = [s for s in slots if s.kind == "points"]
        if points:
            found = self._inner.segment(
                image, {"points": points[0].points}, 1)
            for slot, raw in zip(points, found):
                out.append(self._instance(slot, raw))

        by_phrase: dict[int, list] = {}
        for slot in slots:
            if slot.kind == "text":
                by_phrase.setdefault(slot.phrase_index, []).append(slot)
        for _, phrase_slots in sorted(by_phrase.items()):
            found = self._inner.segment(
                image, {"text": [phrase_slots[0].text]}, len(phrase_slots))
            found = sorted(found, key=lambda raw: -float(raw.get("score", 0.0)))
            for slot, raw in zip(phrase_slots, found):
                out.append(self._instance(slot, raw))

        out.sort(key=lambda inst: -inst.score)
        return out

    @staticmethod
    def _instance(slot, raw: dict) -> Instance:
        mask = np.asarray(raw["mask"], dtype=np.float32)
        box = raw.get("box") or (0.0, 0.0, 0.0, 0.0)
        return Instance(id=slot.id, score=float(raw.get("score", 0.0)),
                        box=tuple(float(v) for v in box),
                        area=float(raw.get("area", float(mask.mean()) if mask.size else 0.0)),
                        mask=mask, label=slot.label, prompt=slot.as_dict())

    # -- a clip ------------------------------------------------------------

    def track(self, frames: Iterator[np.ndarray], fps: float, prompts: dict,
              select: Any, on_frame) -> None:
        prompts = normalize_prompts(prompts)
        slots = plan_objects(prompts,
                             max_instances=prompts.get("max_instances") or 1,
                             select=select)
        boxes = [s for s in slots if s.kind == "box"]
        points = [s for s in slots if s.kind == "points"]
        texts = [s for s in slots if s.kind == "text"]

        if boxes:
            group, inner_prompts = boxes, {"boxes": [list(s.box) for s in boxes]}
        elif points:
            group, inner_prompts = points, {"points": points[0].points}
        elif texts:
            phrase_index = texts[0].phrase_index
            group = [s for s in texts if s.phrase_index == phrase_index]
            inner_prompts = {"text": [group[0].text]}
            if len(group) != len(texts):
                self._log("[torch] this backend tracks one phrase per pass; "
                          f"only {group[0].text!r} is tracked in this job")
        else:
            raise ValueError("track needs at least one prompt")

        last_mask: dict[str, np.ndarray] = {}

        for window_index, (start, window) in enumerate(self._windows(frames)):
            # Quiet mode's gap, between two windows and after the previous
            # one's memory has gone: see sam/throttle.py and the same call in
            # mlx_backend.track. A no op at the default duty cycle of 1.
            self.settle()
            token = self.throttle.begin()
            window_size = len(window)
            self.window = {"start": start, "end": start + window_size,
                           "frames": window_size, "size": self.chunk_frames}
            self.meter.start(start=start, end=start + window_size,
                             frames=window_size)
            # Everything from here to the `finally` is inside the window, so a
            # raise anywhere in it still closes the window on /health and still
            # closes the meter. The seeding below can raise (every object
            # lost), and it used to raise BEFORE this try: /health then
            # reported a window that was not loaded any more, and
            # memory.windows.current never closed, in exactly the situation
            # somebody reads /health to understand (round 1 finding 27).
            try:
                if window_index == 0:
                    seeded, order = inner_prompts, None
                else:
                    # Later windows are seeded from where the objects ended up,
                    # because the previous session is gone: box prompts, in a
                    # known order, so the returned integer ids map back without
                    # any discovery.
                    held = [slot for slot in group
                            if last_mask.get(slot.id) is not None
                            and last_mask[slot.id].any()]
                    if not held:
                        raise BackendError("every tracked object was lost before "
                                           "the end of the clip")
                    seeded = {"boxes": [list(mask_box(last_mask[slot.id]))
                                        for slot in held]}
                    order = held

                mapping: dict[int, str] = {}

                def relabel(index: int, masks: dict, start=start,
                            window_index=window_index, order=order,
                            mapping=mapping) -> None:
                    if index == 0 and window_index > 0:
                        return              # the overlap frame, already emitted
                    renamed = {}
                    for object_id, value in masks.items():
                        key = int(object_id)
                        if key not in mapping:
                            if order is not None:
                                # Box prompts come back as 1..n in the order they
                                # were given (see torch_backend's obj_ids_batch).
                                position = key - 1
                                if not 0 <= position < len(order):
                                    continue
                                mapping[key] = order[position].id
                            elif len(mapping) < len(group):
                                mapping[key] = group[len(mapping)].id
                            else:
                                continue    # more instances than slots: ignore
                        mask = value.mask if isinstance(value, TrackedMask) else value
                        mask = np.asarray(mask, dtype=np.float32)
                        renamed[mapping[key]] = mask
                        if mask.any():
                            last_mask[mapping[key]] = mask
                    on_frame(start + index, renamed)
                    self.meter.sample()

                self._inner.track(iter(window), fps, seeded, "all", relabel)
            finally:
                # The inner backend materialises whatever iterator it is given
                # (`list(frames)`), so this list is a second copy of the same
                # window until it goes. Emptying it in place drops the copy the
                # generator is still holding too.
                window.clear()
                self.meter.finish(freed=self.release)
                # The next window takes this rest, or the service does once
                # the job is reported (Service._run_job).
                self.throttle.owe(token)
                self.window = None

    def _windows(self, frames: Iterator[np.ndarray]):
        """Windows of frames with a one frame overlap, and the absolute index
        of each window's first frame. The overlap frame is what carries an
        object across the seam: it is tracked twice and emitted once."""
        buffer: list = []
        start = 0
        for frame in frames:
            buffer.append(frame)
            if len(buffer) >= self.chunk_frames:
                tail, size = buffer[-1], len(buffer)
                yield start, buffer          # the caller may empty this list
                start += size - 1
                buffer = [tail]
                tail = None
        if len(buffer) > 1 or start == 0:
            yield start, buffer
