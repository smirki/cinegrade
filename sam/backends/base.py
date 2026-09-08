"""Contract C7: what a SAM backend must do, and the prompt planning every
backend and the service share.

A backend knows nothing about HTTP, jobs, mattes or grading. It takes pixels
and prompts and gives back masks. Everything about queues, disk, states and
recipes lives in `sam/server.py` and `sam/store.py`.

The one piece of shared logic that lives here rather than in the service is
`plan_objects()`: the rule that turns a prompt block into a fixed, ordered
list of object slots with stable ids. The service uses it to allocate one
matte per slot at the moment a track is enqueued (before any model has run),
and every backend uses it to know what to seed. If the two disagreed, matte
ids and tracked objects would drift apart, so there is exactly one function.
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, NamedTuple

import numpy as np

# `sam/` is on sys.path when the service runs (server.py puts it there) but
# not when a test imports `backends` on its own, so this file makes sure of
# it rather than leaving every backend to guess. Same pattern server.py uses.
_SAM_DIR = str(Path(__file__).resolve().parent.parent)
if _SAM_DIR not in sys.path:
    sys.path.insert(0, _SAM_DIR)

from memstat import (StageMeter, WindowMeter, mlx_memory,   # noqa: E402
                     snapshot, stage_meter)

__all__ = [
    "Backend",
    "BackendError",
    "BackendUnavailable",
    "Cancelled",
    "Instance",
    "ObjectSlot",
    "StageMeter",
    "TrackedMask",
    "WindowMeter",
    "mask_area",
    "mask_box",
    "mlx_memory",
    "normalize_prompts",
    "plan_objects",
    "snapshot",
    "split_mask_score",
    "stage_meter",
    "window_meter",
]


def window_meter(log=None, mx=None) -> WindowMeter:
    """One per backend: the per window memory record C3's /health reports."""
    return WindowMeter(mx=mx, log=log)


class BackendError(RuntimeError):
    """The backend was there but the work failed."""


class BackendUnavailable(BackendError):
    """This backend cannot run here: missing package, missing weights, no
    device. `--backend auto` catches this, logs it, and tries the next one."""


class Cancelled(Exception):
    """Raised by the service's `on_frame` when a running job is cancelled.

    Backends must let it travel: do not wrap `on_frame` in a bare `except`.
    It is the only way to stop a track between frames.
    """


@dataclass
class Instance:
    """One thing the model found. Coordinates are fractions of the image."""

    id: str
    score: float
    box: tuple[float, float, float, float]
    area: float
    mask: np.ndarray                      # float32 HxW, 0..1
    label: str = ""
    prompt: dict = field(default_factory=dict)


class TrackedMask(NamedTuple):
    """What a backend may hand `on_frame` instead of a bare array when it
    knows the tracker's confidence for that frame. It is a plain tuple, so
    `(mask, score)` works just as well and a backend that returns only an
    array is still correct."""

    mask: np.ndarray
    score: float


def split_mask_score(value: Any) -> tuple[np.ndarray, float]:
    """Accept either an array or a (mask, score) pair from `on_frame`.

    With no score reported, `max(mask)` is used: for a binary mask that is 1.0
    while the object is present and 0.0 on a frame where it vanished, which is
    exactly the signal the matte's score curve is meant to show.
    """
    if isinstance(value, tuple) and len(value) == 2:
        mask, score = value
        mask = np.asarray(mask, dtype=np.float32)
        return mask, float(score)
    mask = np.asarray(value, dtype=np.float32)
    return mask, (float(mask.max()) if mask.size else 0.0)


def mask_area(mask: np.ndarray) -> float:
    """Fraction of the frame the matte covers, 0..1."""
    if mask.size == 0:
        return 0.0
    return float(np.clip(mask, 0.0, 1.0).mean())


def mask_box(mask: np.ndarray, threshold: float = 0.5) -> tuple[float, float, float, float]:
    """Bounding box of the mask as fractions (x0, y0, x1, y1)."""
    if mask.size == 0:
        return (0.0, 0.0, 0.0, 0.0)
    height, width = mask.shape[:2]
    rows = np.any(mask > threshold, axis=1)
    cols = np.any(mask > threshold, axis=0)
    if not rows.any() or not cols.any():
        return (0.0, 0.0, 0.0, 0.0)
    y0, y1 = int(np.argmax(rows)), int(len(rows) - np.argmax(rows[::-1]))
    x0, x1 = int(np.argmax(cols)), int(len(cols) - np.argmax(cols[::-1]))
    return (x0 / width, y0 / height, x1 / width, y1 / height)


# ---------------------------------------------------------------------------
# Prompts and slots
# ---------------------------------------------------------------------------


@dataclass
class ObjectSlot:
    """One tracked object, and therefore one matte.

    `id` is stable and predictable, because it is the id that appears in a
    segment response, in a track's `select`, in every `on_frame` dict, and in
    the matte's `index.json`.
    """

    id: str
    kind: str                              # "box" | "points" | "mask" | "text"
    label: str = ""
    text: str | None = None
    phrase_index: int | None = None
    instance_index: int = 0
    box: tuple[float, float, float, float] | None = None
    points: list[dict] | None = None
    mask_path: str | None = None

    def as_dict(self) -> dict:
        out = {"id": self.id, "kind": self.kind, "label": self.label,
               "instance_index": self.instance_index}
        if self.text is not None:
            out["text"] = self.text
        if self.box is not None:
            out["box"] = list(self.box)
        if self.points is not None:
            out["points"] = self.points
        if self.mask_path is not None:
            out["mask"] = self.mask_path
        return out


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [value]
    if isinstance(value, dict):
        return [value]
    return list(value)


def normalize_prompts(prompts: Any) -> dict:
    """Validate the wire prompt block and give back a canonical one.

    Raises ValueError with a sentence a person can act on. Every coordinate is
    a fraction of the image; anything outside 0..1 is a mistake worth naming
    rather than silently clamping, because a caller sending pixels would
    otherwise get a mask in the corner and no explanation.
    """
    if prompts is None:
        prompts = {}
    if not isinstance(prompts, dict):
        raise ValueError("prompts must be an object")

    text = [str(t).strip() for t in _as_list(prompts.get("text")) if str(t).strip()]

    points: list[dict] = []
    for raw in _as_list(prompts.get("points")):
        if isinstance(raw, dict):
            x, y = raw.get("x"), raw.get("y")
            label = raw.get("label", 1)
        elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
            x, y = raw[0], raw[1]
            label = raw[2] if len(raw) > 2 else 1
        else:
            raise ValueError("each point must be {x, y, label} or [x, y, label]")
        try:
            x, y, label = float(x), float(y), int(label)
        except (TypeError, ValueError):
            raise ValueError("point x and y must be numbers and label 0 or 1") from None
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ValueError(f"point ({x}, {y}) is outside the frame: coordinates "
                             "are fractions of the image, not pixels")
        if label not in (0, 1):
            raise ValueError("point label must be 1 (positive) or 0 (negative)")
        points.append({"x": x, "y": y, "label": label})

    boxes: list[list[float]] = []
    for raw in _as_list(prompts.get("boxes")):
        if isinstance(raw, dict):
            raw = [raw.get("x0"), raw.get("y0"), raw.get("x1"), raw.get("y1")]
        if not isinstance(raw, (list, tuple)) or len(raw) != 4:
            raise ValueError("each box must be [x0, y0, x1, y1] as fractions")
        try:
            x0, y0, x1, y1 = (float(v) for v in raw)
        except (TypeError, ValueError):
            raise ValueError("box coordinates must be numbers") from None
        if min(x0, y0, x1, y1) < 0.0 or max(x0, y0, x1, y1) > 1.0:
            raise ValueError(f"box {[x0, y0, x1, y1]} is outside the frame: "
                             "coordinates are fractions of the image, not pixels")
        if x1 <= x0 or y1 <= y0:
            raise ValueError("box needs x1 > x0 and y1 > y0")
        boxes.append([x0, y0, x1, y1])

    masks = [str(m) for m in _as_list(prompts.get("masks"))]
    exemplars = _as_list(prompts.get("exemplars"))

    # C7's `track` has no max_instances argument, so the service carries it on
    # the prompt block. 0 means "the caller did not say"; a backend reads it
    # with `prompts.get("max_instances") or 1`.
    try:
        max_instances = int(prompts.get("max_instances", 0) or 0)
    except (TypeError, ValueError):
        raise ValueError("max_instances must be a whole number") from None

    return {"text": text, "points": points, "boxes": boxes,
            "masks": masks, "exemplars": exemplars,
            "max_instances": max(0, max_instances)}


def plan_objects(prompts: Any, max_instances: int = 1,
                 select: Any = "all") -> list[ObjectSlot]:
    """Turn a prompt block into the ordered list of objects to track.

    The order and the ids are the contract (they are in the M1 checkpoint):

      boxes   -> one slot each, `b0`, `b1`, ...
      points  -> one slot for all of them together, `p0`
                 (positive and negative points describe ONE object)
      masks   -> one slot each, `k0`, `k1`, ...
      text    -> `max_instances` slots per phrase,
                 `t<phrase index>_<instance index>`, e.g. `t0_0`

    `select` is "all" or a list of slot ids; unknown ids raise, because a
    caller asking for an instance that does not exist has a bug and a silent
    empty track would hide it.
    """
    p = prompts if isinstance(prompts, dict) and "text" in prompts and "points" in prompts \
        else normalize_prompts(prompts)
    max_instances = max(1, int(max_instances))

    slots: list[ObjectSlot] = []
    for i, box in enumerate(p["boxes"]):
        slots.append(ObjectSlot(id=f"b{i}", kind="box", label=f"box {i + 1}",
                                box=tuple(box)))
    if p["points"]:
        slots.append(ObjectSlot(id="p0", kind="points", label="points",
                                points=list(p["points"])))
    for i, mask_path in enumerate(p["masks"]):
        slots.append(ObjectSlot(id=f"k{i}", kind="mask", label=f"mask {i + 1}",
                                mask_path=mask_path))
    for phrase_index, phrase in enumerate(p["text"]):
        for instance_index in range(max_instances):
            slots.append(ObjectSlot(
                id=f"t{phrase_index}_{instance_index}", kind="text", label=phrase,
                text=phrase, phrase_index=phrase_index, instance_index=instance_index))

    if select is None or select == "all" or select == ["all"]:
        return slots
    wanted = [str(s) for s in _as_list(select)]
    by_id = {slot.id: slot for slot in slots}
    unknown = [s for s in wanted if s not in by_id]
    if unknown:
        raise ValueError(f"select names objects that these prompts do not "
                         f"produce: {unknown} (available: {sorted(by_id)})")
    return [by_id[s] for s in wanted]


def recipe_digest(*parts: Any) -> str:
    """A short stable digest, used for matte ids and pick ids."""
    import json

    blob = json.dumps(parts, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


# ---------------------------------------------------------------------------
# The interface itself
# ---------------------------------------------------------------------------


OnFrame = Callable[[int, dict], None]


class Backend:
    """C7. Subclasses implement four methods and nothing else is expected.

    `name` is one of "mlx", "torch-mps", "torch-cpu", "stub" and is what
    `/health` reports.
    """

    # Which window of the clip is loaded right now, or None when the
    # backend is idle. A tracker that holds the whole clip in memory
    # cannot run on this machine, so every real backend works through
    # bounded windows and says which one it is on: the service puts
    # this straight on /health, because "why is this slow" is a
    # question a person asks while it is still running.
    window: dict | None = None

    name: str = "base"
    model: str = ""

    def load(self) -> None:
        """Make the backend ready to answer. Raise `BackendUnavailable` when
        this machine cannot run it, so `--backend auto` can move on."""
        raise NotImplementedError

    def segment(self, image: np.ndarray, prompts: dict,
                max_instances: int) -> list[Instance]:
        """One frame, right now. `image` is rgb uint8 HxWx3."""
        raise NotImplementedError

    def track(self, frames: Iterator[np.ndarray], fps: float, prompts: dict,
              select: Any, on_frame: OnFrame) -> None:
        """Propagate through the clip, calling
        `on_frame(index, {slot_id: mask})` once per frame in order.

        `index` is the position within the frames given, 0 based; the service
        adds the range's start frame to get the absolute index. `mask` is
        float32 HxW in 0..1, or a `TrackedMask(mask, score)` pair.

        `frames` is a lazy iterator and must be consumed lazily (in chunks at
        worst): a whole clip preprocessed at once does not fit in this
        machine's memory. Raising anything fails the job; letting a
        `Cancelled` from `on_frame` travel stops it cleanly.
        """
        raise NotImplementedError

    def close(self) -> None:
        """Release the model and anything else held."""

    # -- memory, added by the 2026-09-08 memory fix -------------------------
    #
    # Optional and additive: a backend that implements neither behaves as it
    # did before. The service calls `release()` when a job ends and puts
    # `memory()` and `meter.stats()` on /health, so the founder can see what
    # a window cost without running `top` against the pid.

    #: A `memstat.WindowMeter`, or None on a backend that does not window.
    meter = None

    def memory(self) -> dict:
        """The backend's own view of its memory. Empty when it has none."""
        return {}

    def release(self) -> None:
        """Give back everything that is not the model. Called between jobs."""

    def window_stats(self) -> dict | None:
        return self.meter.stats() if self.meter is not None else None

    # Small conveniences shared by every backend.

    @staticmethod
    def chunked(frames: Iterable[np.ndarray], size: int) -> Iterator[list[np.ndarray]]:
        batch: list[np.ndarray] = []
        for frame in frames:
            batch.append(frame)
            if len(batch) >= size:
                yield batch
                batch = []
        if batch:
            yield batch
