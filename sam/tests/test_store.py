#!/usr/bin/env python3
"""The matte store (C2) and its temporal smoothing, with no server and no model.

    uv run --project sam python sam/tests/test_store.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import FAILED, check, report, sam_path                        # noqa: E402

sam_path()
from store import MatteWriter, _Steady, frame_name, read_index            # noqa: E402


def header(frames: int) -> dict:
    return {"clip": "/tmp/clip.mov", "clip_key": "clip-1", "rotation": 0,
            "fps": 24.0, "frames": frames, "start_frame": 0, "end_frame": frames,
            "width": 8, "height": 4, "recipe": {"kind": "track"},
            "state": "queued", "model": "stub", "backend": "stub",
            "job_id": "j_1", "object_id": "t0_0", "label": "person",
            "kind": "text"}


def ramp(value: float) -> np.ndarray:
    return np.full((4, 8), value, dtype=np.float32)


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="sam-store-"))

    print("frame naming and index shape")
    check("frames are named by absolute source index",
          frame_name(48) == "000048.png", frame_name(48))
    writer = MatteWriter(root, "m_test", header(10), steady=1)
    index = read_index(writer.dir)
    check("index.json exists at creation with state queued",
          index["state"] == "queued" and index["done_frames"] == 0)
    check("areas and scores are one slot per frame of the clip",
          len(index["areas"]) == 10 and all(a is None for a in index["areas"]))
    check("C2's required keys are all present",
          all(key in index for key in ("matte_id", "clip", "clip_key", "rotation",
                                       "fps", "frames", "width", "height", "recipe",
                                       "state", "done_frames", "areas", "scores",
                                       "created", "model", "backend")),
          ", ".join(sorted(index)))

    print("\nwriting frames")
    writer.set_state("running")
    for i in range(10):
        writer.push(i, ramp(0.5), 0.9)
    writer.finish("done")
    index = read_index(writer.dir)
    files = sorted(p.name for p in writer.dir.glob("*.png"))
    check("one png per frame", len(files) == 10, str(files[:3]))
    check("first file is the first source frame", files[0] == "000000.png")
    check("state done, done_frames counted", index["state"] == "done"
          and index["done_frames"] == 10)
    check("area is the fraction of the frame covered",
          abs(index["areas"][0] - 0.5) < 0.01, str(index["areas"][0]))
    check("scores are recorded per frame", index["scores"][0] == 0.9)

    print("\na matte that starts part way through the clip")
    offset = MatteWriter(root, "m_offset", dict(header(120), start_frame=48,
                                                end_frame=52), steady=1)
    for i in range(48, 52):
        offset.push(i, ramp(1.0), 1.0)
    offset.finish("partial", "cancelled")
    index = read_index(offset.dir)
    names = sorted(p.name for p in offset.dir.glob("*.png"))
    check("file names carry the absolute frame index", names[0] == "000048.png", names[0])
    check("areas are indexed by absolute frame index, null elsewhere",
          index["areas"][0] is None and index["areas"][48] is not None
          and index["areas"][52] is None)
    check("a partial matte says so, with the reason",
          index["state"] == "partial" and index["error"] == "cancelled")

    print("\ntemporal smoothing (steady)")
    steady = _Steady(1)
    out = steady.push(0, ramp(1.0), 1.0)
    check("steady 1 writes straight through", len(out) == 1 and out[0][0] == 0)

    steady = _Steady(3)
    emitted = []
    values = [0.0, 1.0, 0.0, 1.0, 0.0]
    for i, value in enumerate(values):
        emitted += steady.push(i, ramp(value), 1.0)
    emitted += steady.drain()
    check("steady 3 emits every frame exactly once, in order",
          [e[0] for e in emitted] == list(range(5)), str([e[0] for e in emitted]))
    middles = [float(e[1].mean()) for e in emitted[1:4]]
    # A 0, 1, 0, 1, 0 flicker becomes 1/3, 2/3, 1/3: the swing shrinks from
    # 1.0 to 1/3, which is what "steady" is for.
    check("steady 3 averages the neighbours, so an alternating signal flattens",
          all(0.3 < v < 0.7 for v in middles)
          and max(middles) - min(middles) < 0.4,
          str([round(v, 3) for v in middles]))
    check("the smoothing window is centred, not trailing: frame 1 already "
          "carries frame 2's value",
          abs(float(emitted[1][1].mean()) - 0.3333) < 0.01)

    steady = _Steady(5)
    emitted = []
    for i in range(12):
        emitted += steady.push(i, ramp(float(i)), 1.0)
        check_buffer = len(steady.buffer) <= 5
        if not check_buffer:
            break
    emitted += steady.drain()
    check("the smoothing buffer never grows past n frames", check_buffer)
    check("steady 5 emits every frame once", [e[0] for e in emitted] == list(range(12)))
    check("an interior frame is the mean of its five neighbours",
          abs(float(emitted[6][1].mean()) - 6.0) < 1e-4, str(float(emitted[6][1].mean())))

    print("\nindex.json is never seen half written")
    writer = MatteWriter(root, "m_atomic", header(4), steady=1)
    for i in range(4):
        writer.push(i, ramp(0.25), 0.5)
        json.loads((writer.dir / "index.json").read_text())
    writer.finish("done")
    check("index.json parses after every single frame", True)
    check("no temp files are left behind",
          not list(writer.dir.glob("*.tmp*")), str(list(writer.dir.glob("*.tmp*"))))

    return report("test_store")


if __name__ == "__main__":
    raise SystemExit(main())

