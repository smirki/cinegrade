#!/usr/bin/env python3
"""The torch adapter's windowing and id mapping, with no torch installed.

    uv run --project sam python sam/tests/test_torch_adapter.py

`backends/torch_adapter.py` (this lane) is what lets `--backend auto` reach
lane M1b's `torch_backend.py` without either file having to know about the
other. Two things about it are worth a test that does not need 3 GB of
weights:

* it never hands the whole clip to a backend that does `list(frames)`, and
* the integer object ids that backend speaks come back out as slot ids, so a
  matte is named the same whichever backend made it.

A fake stands in for the torch backend: it records what it was given per
call and answers with integer ids, which is the whole of the contract this
adapter depends on.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import check, report, sam_path                                # noqa: E402

sam_path()
from backends.base import Cancelled                                       # noqa: E402
from backends.torch_adapter import TorchAdapter                           # noqa: E402


class FakeTorch:
    """What torch_backend.py looks like from the adapter's side: integer
    object ids, and a `list(frames)` of whatever iterator it is handed."""

    name = "torch-cpu"
    model = "fake"

    def __init__(self, instances: int = 1, first_id: int = 1):
        self.calls: list[dict] = []
        self.instances = instances
        self.first_id = first_id
        self.emitted = 0        # keeps counting across windows, like an object
                                # that keeps moving across the seam

    def track(self, frames, fps, prompts, select, on_frame) -> None:
        materialised = list(frames)                # exactly what the real one does
        self.calls.append({"frames": len(materialised), "prompts": prompts})
        height, width = materialised[0].shape[:2]
        for index in range(len(materialised)):
            masks = {}
            for n in range(self.instances):
                mask = np.zeros((height, width), dtype=np.float32)
                # A blob that moves right, so the box seeded into the next
                # window is different from the one seeded into this one.
                x = min(width - 4, 2 + self.emitted + 2 * n)
                mask[2:6, x:x + 3] = 1.0
                masks[self.first_id + n] = mask
            self.emitted += 1
            on_frame(index, masks)


def frames(count: int, width: int = 32, height: int = 16):
    for _ in range(count):
        yield np.zeros((height, width, 3), dtype=np.uint8)


def adapter(fake, chunk_frames: int = 4) -> TorchAdapter:
    made = TorchAdapter(device="cpu", chunk_frames=chunk_frames, log=lambda _m: None)
    made._inner = fake
    return made


def main() -> int:
    print("a clip is fed in windows, never whole")
    fake = FakeTorch()
    seen: list[tuple[int, list[str]]] = []
    adapter(fake).track(frames(10), 24.0, {"text": ["person"]}, "all",
                        lambda i, m: seen.append((i, sorted(m))))
    check("the backend is called once per window, not once per clip",
          len(fake.calls) == 3, f"{len(fake.calls)} calls")
    check("no window is bigger than chunk_frames, so a backend that does "
          "list(frames) cannot hold the clip",
          all(call["frames"] <= 4 for call in fake.calls),
          str([call["frames"] for call in fake.calls]))
    check("every frame of the clip is emitted exactly once, in order",
          [index for index, _ in seen] == list(range(10)),
          str([index for index, _ in seen]))
    check("the overlap frame is tracked twice and emitted once",
          sum(call["frames"] for call in fake.calls) == 12)

    print("\nwindows after the first are seeded from where the object got to")
    check("the first window gets the caller's own prompt",
          fake.calls[0]["prompts"] == {"text": ["person"]},
          str(fake.calls[0]["prompts"]))
    check("later windows are seeded by box, because the previous session is "
          "gone", "boxes" in fake.calls[1]["prompts"]
          and len(fake.calls[1]["prompts"]["boxes"]) == 1,
          str(fake.calls[1]["prompts"]))
    box = fake.calls[1]["prompts"]["boxes"][0]
    check("the seed box is in fractions, inside the frame",
          all(0.0 <= v <= 1.0 for v in box) and box[0] < box[2] and box[1] < box[3],
          str([round(v, 3) for v in box]))
    check("and it follows the object rather than repeating the first seed",
          fake.calls[2]["prompts"]["boxes"][0][0] > box[0],
          f"{box[0]:.3f} then {fake.calls[2]['prompts']['boxes'][0][0]:.3f}")

    print("\ninteger object ids come back out as slot ids")
    check("a text phrase's instance is named for its slot",
          all(ids == ["t0_0"] for _, ids in seen), str(seen[0]))

    fake = FakeTorch(instances=2)
    seen.clear()
    adapter(fake).track(frames(10), 24.0,
                        {"boxes": [[0.1, 0.1, 0.4, 0.9], [0.5, 0.1, 0.9, 0.9]]},
                        "all", lambda i, m: seen.append((i, sorted(m))))
    check("two boxes give two slots, in the order they were asked for",
          all(ids == ["b0", "b1"] for _, ids in seen), str(seen[0]))
    check("the mapping survives a window boundary, so a matte does not swap "
          "objects half way through a clip",
          seen[0][1] == seen[-1][1] == ["b0", "b1"])

    fake = FakeTorch(first_id=17)
    seen.clear()
    adapter(fake).track(frames(3), 24.0, {"text": ["person"]}, "all",
                        lambda i, m: seen.append((i, sorted(m))))
    check("whatever integers the backend happens to use, the slot id is the "
          "same", all(ids == ["t0_0"] for _, ids in seen), str(seen[:1]))

    print("\ncancel and refusals")
    fake = FakeTorch()

    def cancel_at_five(index, _masks):
        if index == 5:
            raise Cancelled("cancelled")

    try:
        adapter(fake).track(frames(20), 24.0, {"text": ["person"]}, "all",
                            cancel_at_five)
        check("Cancelled travels out through the windows", False, "it kept going")
    except Cancelled:
        check("Cancelled travels out through the windows", True,
              f"stopped after {len(fake.calls)} window(s)")

    try:
        adapter(FakeTorch()).track(frames(4), 24.0, {}, "all", lambda i, m: None)
        check("no prompt at all is refused", False)
    except ValueError as exc:
        check("no prompt at all is refused", True, str(exc)[:50])

    made = adapter(FakeTorch())
    check("the adapter reports which window is loaded while it works, and "
          "nothing when it is done", made.window is None)

    return report("test_torch_adapter")


if __name__ == "__main__":
    raise SystemExit(main())
