#!/usr/bin/env python3
"""Measure what one layer costs: ffmpeg render wall time for N active layers.

The per layer cost quoted in studio/static/limits.js ("Each active layer costs
about the same again") comes out of this script. It renders the same short
range through the ffmpeg engine with 0, 1, 2 and 4 active layers, every layer
carrying a power window AND a blur, which is the expensive shape: one baked
cube, one baked matte input, one gblur and one maskedmerge each.

  .venv/bin/python studio/tools/layer_cost_bench.py

Each case is rendered TWICE and only the second run is timed, so the number is
render time and not LUT bake time: a colourist adding a fifth layer to a grade
they have already rendered once is the case the entry is about. Outputs go to
grade/out with an l1-layercost- prefix and nothing is deleted. Every ffmpeg
call is bounded by TIMEOUT.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONTENT = HERE.parent.parent
OUT = CONTENT / "grade" / "out"
sys.path.insert(0, str(CONTENT / "grade"))
import cinegrade as CG                                        # noqa: E402

SRC = str(CONTENT / "footage" / "A001_09011832_C003.MOV")
DURATION = 2.0
WIDTH = 1920
TIMEOUT = 600
COUNTS = (0, 1, 2, 4)


def layer(i: int) -> dict:
    """One masked layer, offset from its neighbours so they do not coincide."""
    return CG.deep_merge(CG.LAYER_DEFAULTS, {
        "name": "cost%d" % i,
        "enabled": True,
        "mask": {"window": {"enabled": True, "shape": "ellipse",
                            "center_x": 0.4 + 0.05 * i, "center_y": 0.5,
                            "width": 0.45, "height": 0.5, "feather": 0.25}},
        "correct": {"exposure": 0.15, "sat_gain": 1.15, "blur": 4.0},
    })


def run_case(n: int) -> tuple[float, int, int, int]:
    cfg = CG.deep_merge(CG.DEFAULTS, {"layers": [layer(i) for i in range(n)]})
    info = CG.probe(SRC)
    out = str(OUT / ("l1-layercost-%d.mp4" % n))
    graph = CG.graph_with_mask(cfg, info, tail_extra=["scale=%d:-2" % WIDTH])
    args = CG.ffmpeg_inputs(SRC, cfg, info, 0.0, DURATION)
    args += ["-t", str(DURATION), "-filter_complex", graph, "-map", "[vout]",
             "-c:v", "libx264", "-crf", "18", "-preset", "medium",
             "-pix_fmt", "yuv420p", out]
    t0 = time.time()
    p = subprocess.run(args, capture_output=True, timeout=TIMEOUT)
    wall = time.time() - t0
    if p.returncode != 0:
        sys.stderr.write(p.stderr.decode()[-2000:])
        raise SystemExit("ffmpeg failed for n=%d" % n)
    return (wall, graph.count("lut3d"), graph.count("gblur"),
            graph.count("maskedmerge"))


def main() -> None:
    print("src %s, %.1fs at %d wide, every layer with a window and a blur"
          % (Path(SRC).name, DURATION, WIDTH))
    base = None
    for n in COUNTS:
        run_case(n)                       # warm the cube and matte cache
        wall, luts, blurs, merges = run_case(n)
        if base is None:
            base = wall
        per = "" if not n else "  (%.2fs per layer over the 0 layer case)" % (
            (wall - base) / n)
        print("layers=%d  wall=%.2fs  lut3d=%d gblur=%d maskedmerge=%d%s"
              % (n, wall, luts, blurs, merges, per))


if __name__ == "__main__":
    main()
