#!/usr/bin/env python3
"""The prompt planner (C7) and the stub backend, with no server and no model.

    uv run --project sam python sam/tests/test_stub_backend.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import centroid, check, report, sam_path                      # noqa: E402

sam_path()
from backends import normalize_prompts, plan_objects                      # noqa: E402
from backends.base import Cancelled, TrackedMask, split_mask_score        # noqa: E402
from backends.stub import StubBackend                                     # noqa: E402


def frames(count: int, width: int = 160, height: int = 90):
    for _ in range(count):
        yield np.zeros((height, width, 3), dtype=np.uint8)


def main() -> int:
    print("prompts are validated, not silently clamped")
    for bad, why in [
        ({"points": [{"x": 640, "y": 400}]}, "pixels instead of fractions"),
        ({"boxes": [[0.5, 0.5, 0.2, 0.9]]}, "a box with x1 below x0"),
        ({"points": [{"x": 0.5, "y": 0.5, "label": 7}]}, "a label that is not 0 or 1"),
        ({"boxes": [[0.1, 0.1, 0.5]]}, "a box with three numbers"),
    ]:
        try:
            normalize_prompts(bad)
            check(f"refused: {why}", False, "it was accepted")
        except ValueError as exc:
            check(f"refused: {why}", True, str(exc)[:60])

    print("\nslot ids are the contract")
    prompts = normalize_prompts({"text": ["person", "sky"],
                                 "points": [{"x": 0.5, "y": 0.5},
                                            {"x": 0.1, "y": 0.1, "label": 0}],
                                 "boxes": [[0.1, 0.1, 0.4, 0.9], [0.5, 0.1, 0.9, 0.9]]})
    slots = plan_objects(prompts, max_instances=2)
    check("boxes first, then points as one object, then text per instance",
          [s.id for s in slots] == ["b0", "b1", "p0", "t0_0", "t0_1", "t1_0", "t1_1"],
          str([s.id for s in slots]))
    check("both positive and negative points describe ONE object",
          len([s for s in slots if s.kind == "points"]) == 1)
    check("a text slot carries its phrase as its label",
          [s.label for s in slots if s.kind == "text"][0] == "person")
    check("the same prompts plan the same ids twice",
          [s.id for s in plan_objects(prompts, 2)] == [s.id for s in slots])

    selected = plan_objects(prompts, 2, select=["t1_0", "b1"])
    check("select keeps only what was asked, in the order asked",
          [s.id for s in selected] == ["t1_0", "b1"])
    try:
        plan_objects(prompts, 2, select=["t9_9"])
        check("select refuses an id these prompts cannot produce", False)
    except ValueError as exc:
        check("select refuses an id these prompts cannot produce", True, str(exc)[:60])

    print("\nthe stub segments without any weights")
    backend = StubBackend()
    backend.load()
    image = np.zeros((90, 160, 3), dtype=np.uint8)
    instances = backend.segment(image, {"text": ["person"]}, max_instances=4)
    check("a phrase gives instances with ids, scores, boxes and masks",
          len(instances) >= 1 and instances[0].mask.shape == (90, 160)
          and 0 < instances[0].area < 1, f"{len(instances)} instance(s)")
    check("instances come back best first",
          all(instances[i].score >= instances[i + 1].score
              for i in range(len(instances) - 1)))
    check("the mask has a soft edge, not a hard one",
          0 < float(((instances[0].mask > 0.02) & (instances[0].mask < 0.98)).mean()) < 0.5)
    again = backend.segment(image, {"text": ["person"]}, max_instances=4)
    check("the same call gives the same pixels every time",
          np.array_equal(instances[0].mask, again[0].mask))

    box_instances = backend.segment(image, {"boxes": [[0.2, 0.3, 0.6, 0.8]]}, 1)
    cx, cy = centroid(box_instances[0].mask)
    check("a box prompt puts the matte inside the box",
          0.2 < cx < 0.6 and 0.3 < cy < 0.8, f"centroid {cx:.2f}, {cy:.2f}")

    print("\nthe stub's matte drifts with time (acceptance A1's stub half)")
    seen = {}

    def collect(index, masks):
        for slot_id, value in masks.items():
            mask, score = split_mask_score(value)
            seen.setdefault(slot_id, []).append((index, mask, score))

    backend.track(frames(48), 24.0, {"text": ["person"], "max_instances": 1},
                  "all", collect)
    track = seen.get("t0_0", [])
    check("one call per frame, in order",
          [entry[0] for entry in track] == list(range(48)), f"{len(track)} frames")
    first = centroid(track[0][1])
    last = centroid(track[-1][1])
    moved = abs(first[0] - last[0]) + abs(first[1] - last[1])
    check("the matte has moved by the end of two seconds", moved > 0.05,
          f"centroid {first[0]:.2f},{first[1]:.2f} -> {last[0]:.2f},{last[1]:.2f}")
    areas = [float(entry[1].mean()) for entry in track]
    check("the area curve is a curve, not a flat line",
          max(areas) - min(areas) > 0.001, f"{min(areas):.4f}..{max(areas):.4f}")
    check("scores are reported per frame and stay sane",
          all(0.0 <= entry[2] <= 1.0 for entry in track))
    check("the mask carries its score as a (mask, score) pair",
          isinstance(next(iter(seen['t0_0']))[1], np.ndarray))

    print("\ntracking several objects in one pass")
    seen.clear()
    backend.track(frames(4), 24.0,
                  {"text": ["person", "sky"], "max_instances": 1}, "all", collect)
    check("two phrases give two mattes from one pass",
          sorted(seen) == ["t0_0", "t1_0"], str(sorted(seen)))
    check("the two objects are not the same matte",
          not np.array_equal(seen["t0_0"][0][1], seen["t1_0"][0][1]))

    print("\ncancel travels out of on_frame")
    def cancel_at_two(index, masks):
        if index == 2:
            raise Cancelled("cancelled")

    try:
        backend.track(frames(48), 24.0, {"text": ["person"]}, "all", cancel_at_two)
        check("a Cancelled raised in on_frame stops the track", False, "it kept going")
    except Cancelled:
        check("a Cancelled raised in on_frame stops the track", True)

    print("\nthe planner and the backend agree about unfilled slots")
    seen.clear()
    backend.track(frames(2), 24.0, {"text": ["person"], "max_instances": 4},
                  "all", collect)
    check("the stub fills two instances per phrase and leaves the rest empty, "
          "so the service's 'model found nothing' path is exercised",
          sorted(seen) == ["t0_0", "t0_1"], str(sorted(seen)))

    check("split_mask_score defaults a missing score to the mask's own peak",
          split_mask_score(np.full((2, 2), 0.5, dtype=np.float32))[1] == 0.5)
    check("split_mask_score reads a TrackedMask pair",
          split_mask_score(TrackedMask(np.zeros((2, 2), np.float32), 0.75))[1] == 0.75)

    backend.close()

    print("\n--backend auto walks mlx, then torch-mps, then torch-cpu")
    import backends as registry

    tried: list[str] = []

    class Fake(StubBackend):
        def __init__(self, name, fails):
            super().__init__()
            self.name, self._fails = name, fails

        def load(self):
            tried.append(self.name)
            if self._fails:
                raise RuntimeError(f"{self.name} is not available here")

    real_make = registry.make_backend
    registry.make_backend = lambda name, log=print, outer_lock_held=True, **kw: (
        Fake(name, fails=name in ("mlx", "torch-mps")))
    try:
        chosen, errors = registry.load_backend("auto", log=lambda _m: None)
        check("auto tries them in the order the founder's machine prefers",
              tried == ["mlx", "torch-mps", "torch-cpu"], str(tried))
        check("it lands on the first one that loads",
              chosen is not None and chosen.name == "torch-cpu",
              str(chosen and chosen.name))
        check("and keeps why each earlier one was skipped, so /health can "
              "show it rather than the founder guessing",
              [e["backend"] for e in errors] == ["mlx", "torch-mps"]
              and "not available" in errors[0]["error"], str(errors))

        tried.clear()
        registry.make_backend = lambda name, log=print, outer_lock_held=True, **kw: (
            Fake(name, fails=True))
        chosen, errors = registry.load_backend("auto", log=lambda _m: None)
        check("when nothing loads it says so instead of quietly serving fake "
              "masks: the stub is never a fallback",
              chosen is None and len(errors) == 3, str(chosen))

        tried.clear()
        chosen, errors = registry.load_backend("mlx", log=lambda _m: None)
        check("a named backend is not a walk: it fails as asked",
              chosen is None and tried == ["mlx"], str(tried))
    finally:
        registry.make_backend = real_make

    return report("test_stub_backend")


if __name__ == "__main__":
    raise SystemExit(main())
