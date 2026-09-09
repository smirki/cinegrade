"""The stub backend: no weights, no model, no network, instant.

Contract C3 asks for it in one line: "`--stub` starts the service without the
model: it answers with synthetic masks (an ellipse that drifts with time) so
every test runs without weights."

It exists so the rest of the arc can be built and tested without a 5 GB model
and a machine wide lock in the way: the engine, the routes, the GPU preview,
the CLI and the browser specs all need a matte that behaves like a real one,
not a real one. Two properties make it useful rather than decorative:

* **It drifts.** The ellipse moves and breathes as a function of frame index
  and fps, so a browser spec can assert that the overlay in the viewer moves
  between frames (acceptance A1's first half) and a matte's area curve is a
  curve rather than a flat line.
* **It is deterministic.** The same slot on the same frame at the same size is
  the same pixels every time, so fixtures and the parity gate can rely on it.

The edge is soft, not binary, so feather, grow, shrink and the finesse
controls have something real to act on.
"""

from __future__ import annotations

import hashlib
import math
import time
from typing import Any, Iterator

import numpy as np

from .base import (Backend, Instance, ObjectSlot, TrackedMask, duty_cycle,
                   mask_area, mask_box, normalize_prompts, plan_objects,
                   window_meter)

# How far the ellipse wanders, as a fraction of the frame, and how fast.
DRIFT_X = 0.18
DRIFT_Y = 0.07
DRIFT_HZ = 0.25          # one full cycle every four seconds
BREATHE = 0.12           # how much the radius grows and shrinks
EDGE = 0.08              # soft edge width, in units of the ellipse radius

# A pick shows a couple of instances per phrase, not one and not eight: enough
# for a picker UI to have a choice, few enough to read.
STUB_INSTANCES_PER_PHRASE = 2


def _seed(*parts: Any) -> int:
    blob = "|".join(str(p) for p in parts).encode()
    return int.from_bytes(hashlib.sha256(blob).digest()[:4], "big")


def _unit(value: int, salt: int) -> float:
    """A stable pseudo random number in 0..1 from an integer seed."""
    return ((value * 2654435761 + salt * 40503) % 100003) / 100003.0


class _Ellipse:
    """The geometry of one slot, before time is applied."""

    def __init__(self, slot: ObjectSlot):
        seed = _seed(slot.id, slot.label, slot.text or "", slot.kind)
        self.seed = seed
        self.phase = _unit(seed, 1) * 2 * math.pi
        self.phase_y = _unit(seed, 2) * 2 * math.pi
        self.phase_score = _unit(seed, 3) * 2 * math.pi

        if slot.box is not None:
            x0, y0, x1, y1 = slot.box
            self.cx, self.cy = (x0 + x1) / 2, (y0 + y1) / 2
            self.rx, self.ry = max(0.02, (x1 - x0) / 2), max(0.02, (y1 - y0) / 2)
        elif slot.points:
            positives = [p for p in slot.points if p.get("label", 1) == 1] or slot.points
            self.cx = sum(p["x"] for p in positives) / len(positives)
            self.cy = sum(p["y"] for p in positives) / len(positives)
            self.rx = 0.14 + 0.06 * _unit(seed, 4)
            self.ry = 0.20 + 0.08 * _unit(seed, 5)
        else:
            self.cx = 0.30 + 0.40 * _unit(seed, 6)
            self.cy = 0.32 + 0.36 * _unit(seed, 7)
            self.rx = 0.12 + 0.08 * _unit(seed, 8)
            self.ry = 0.16 + 0.10 * _unit(seed, 9)

    def at(self, time_s: float) -> tuple[float, float, float, float]:
        angle = 2 * math.pi * DRIFT_HZ * time_s
        cx = self.cx + DRIFT_X * math.sin(angle + self.phase)
        cy = self.cy + DRIFT_Y * math.sin(0.7 * angle + self.phase_y)
        breathe = 1.0 + BREATHE * math.sin(0.45 * angle + self.phase)
        rx, ry = self.rx * breathe, self.ry * breathe
        # Keep it inside the frame so the area curve is about the drift, not
        # about the ellipse falling off the edge.
        cx = min(max(cx, rx * 0.5), 1.0 - rx * 0.5)
        cy = min(max(cy, ry * 0.5), 1.0 - ry * 0.5)
        return cx, cy, rx, ry

    def score(self, time_s: float) -> float:
        return 0.90 + 0.08 * math.sin(2 * math.pi * 0.13 * time_s + self.phase_score)

    def render(self, width: int, height: int, time_s: float) -> np.ndarray:
        cx, cy, rx, ry = self.at(time_s)
        xs = (np.arange(width, dtype=np.float32) + 0.5) / width
        ys = (np.arange(height, dtype=np.float32) + 0.5) / height
        dx = (xs[None, :] - cx) / rx
        dy = (ys[:, None] - cy) / ry
        distance = np.sqrt(dx * dx + dy * dy)
        alpha = np.clip((1.0 + EDGE - distance) / (2.0 * EDGE), 0.0, 1.0)
        return (alpha * alpha * (3.0 - 2.0 * alpha)).astype(np.float32)


# Two hooks a test double needs and a real backend does not. A stub whose
# work is instant cannot be watched making progress or cancelled halfway, and
# a service that survives a failed job has to be given a job that fails.
FAIL_PHRASE = "__fail__"


class StubBackend(Backend):
    name = "stub"
    model = "stub-ellipse"
    # It accepts a mask prompt (one slot per mask, like every other prompt
    # kind), so the pick-to-track path the studio's own tests drive is the same
    # path the MLX backend takes. The pixels are ignored here for the same
    # reason a box's pixels are: there is no model, only a drifting ellipse.
    supports_mask_prompts = True
    # The stub reports a real per frame number of its own, so the score curve
    # has the same MEANING here as on MLX even though the value is synthetic.
    score_kind = "tracker"

    def __init__(self, delay_ms: float = 0.0, chunk_frames: int = 48,
                 duty_cycle_fraction: float = 1.0, log=print) -> None:
        self._loaded = False
        self.delay_ms = float(delay_ms or 0.0)
        self._log = log
        # The stub honours the duty cycle for real, sleeping the same way the
        # MLX backend does. That is deliberate: without it, quiet mode could
        # only be tested by loading 1.7 GB of weights and taking the machine
        # wide model lock, so the timing would never be checked in the gate.
        # With it, `--stub --stub-delay-ms 20 --duty-cycle 0.5` is a two second
        # test of the arithmetic, the log line and the cancel path.
        self.throttle = duty_cycle(duty_cycle_fraction, log=log)
        # The stub has no MLX allocator to bound, but it does report the same
        # per window memory record the real backends report, so the /health
        # fields, the CLI and the browser can be built and tested against it
        # without weights and without the machine wide model lock.
        self.meter = window_meter(log=log)
        # The stub has no memory problem, so it does not need windows. It
        # reports them anyway, because a real backend must track in bounded
        # windows on this machine and the studio shows which one is loaded:
        # if the stub reported nothing, that part of the UI could only ever
        # be built against the real model.
        self.chunk_frames = max(2, int(chunk_frames))

    def load(self) -> None:
        self._loaded = True

    def close(self) -> None:
        self._loaded = False

    def memory(self) -> dict:
        """No model, so no model memory: the shape is here, the numbers are
        honest zeros. `limits` is empty rather than absent, because a caller
        that reads /health should not have to branch on the backend."""
        return {"active_mb": 0.0, "cache_mb": 0.0, "peak_mb": 0.0, "limits": {}}

    def release(self) -> None:
        import gc

        gc.collect()

    # -- one frame ---------------------------------------------------------

    def segment(self, image: np.ndarray, prompts: dict,
                max_instances: int) -> list[Instance]:
        prompts = normalize_prompts(prompts)
        if not any(prompts[k] for k in ("text", "points", "boxes", "masks")):
            raise ValueError("segment needs at least one of prompts.text, "
                             "prompts.points, prompts.boxes")
        if FAIL_PHRASE in prompts["text"]:
            raise RuntimeError("the stub was asked to fail on purpose")
        height, width = image.shape[:2]
        slots = plan_objects(prompts, max_instances=max_instances)
        out: list[Instance] = []
        for slot in slots:
            if slot.kind == "text" and slot.instance_index >= STUB_INSTANCES_PER_PHRASE:
                # The stub invents two instances per phrase. Slots past that
                # stay empty on purpose: the service's "a slot the model could
                # not fill" path gets exercised without any weights.
                continue
            mask = _Ellipse(slot).render(width, height, 0.0)
            out.append(Instance(
                id=slot.id,
                score=round(_Ellipse(slot).score(0.0), 4),
                box=mask_box(mask),
                area=mask_area(mask),
                mask=mask,
                label=slot.label,
                prompt=slot.as_dict(),
            ))
        out.sort(key=lambda inst: -inst.score)
        return out

    # -- a clip ------------------------------------------------------------

    def track(self, frames: Iterator[np.ndarray], fps: float, prompts: dict,
              select: Any, on_frame) -> None:
        prompts = normalize_prompts(prompts)
        # C7's track signature has no max_instances, so it rides on the prompt
        # block (see normalize_prompts); one instance per phrase by default.
        slots = plan_objects(prompts,
                             max_instances=prompts.get("max_instances") or 1,
                             select=select)
        live = [s for s in slots
                if not (s.kind == "text" and s.instance_index >= STUB_INSTANCES_PER_PHRASE)]
        ellipses = {slot.id: _Ellipse(slot) for slot in live}
        fps = float(fps) if fps and fps > 0 else 24.0

        token = self.throttle.begin()
        try:
            for index, frame in enumerate(frames):
                if index % self.chunk_frames == 0:
                    if self.window is not None:
                        self.meter.finish()
                        # After the window's own memory is accounted for, not
                        # before: resting while still holding a window would
                        # give the machine back the GPU and not the memory.
                        try:
                            self.rest(token)
                        finally:
                            # Refreshed even when the rest raises Cancelled, so
                            # the `finally` below cannot owe a stretch that has
                            # already been rested for and count it twice.
                            token = self.throttle.begin()
                    self.window = {"start": index, "end": index + self.chunk_frames,
                                   "frames": self.chunk_frames,
                                   "size": self.chunk_frames}
                    self.meter.start(start=index, end=index + self.chunk_frames,
                                     frames=self.chunk_frames)
                if FAIL_PHRASE in prompts["text"] and index >= 2:
                    raise RuntimeError("the stub was asked to fail on purpose")
                if self.delay_ms:
                    time.sleep(self.delay_ms / 1000.0)
                height, width = frame.shape[:2]
                time_s = index / fps
                masks = {slot_id: TrackedMask(ell.render(width, height, time_s),
                                              round(ell.score(time_s), 4))
                         for slot_id, ell in ellipses.items()}
                on_frame(index, masks)
                self.meter.sample()
            if self.window is not None:
                self.meter.finish()
            self.window = None
        finally:
            # The window closes however this returned. The deliberate failure
            # above (FAIL_PHRASE) used to leave both the window and the meter
            # open forever, so /health reported a window that was not loaded
            # and memory.windows.current never closed (round 1 finding 27).
            # A no-op on the ordinary path, which has already closed both.
            if self.window is not None:
                self.meter.finish()
                self.window = None
            # The last window is owed a rest rather than taking one here,
            # whether the clip finished, failed or was cancelled: the service
            # takes it after the job has been reported, so no job ever reads
            # as 100 percent complete and still running.
            self.throttle.owe(token)
