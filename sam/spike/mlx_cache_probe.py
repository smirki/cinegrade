#!/usr/bin/env python3
"""Why the service grew to 13 GB, measured with no model at all.

    uv run --project sam python sam/spike/mlx_cache_probe.py

Takes no model lock and loads no weights: it allocates and frees plain MLX
arrays, so it is safe to run while another process holds the model. It costs
under a gigabyte and a few seconds.

What it shows, and what the fix rests on:

1. MLX keeps every Metal buffer it frees in a reuse cache. On this Mac that
   cache's default limit is 15564.8 MB, which is more of the machine than
   exists, so nothing bounds it.
2. Churning buffers of many different sizes, one live at a time, therefore
   grows the PROCESS while the LIVE SET stays tiny. That is exactly what one
   window of SAM tracking does, once per frame, at much bigger sizes.
3. `clear_cache()` does give the memory back, but a couple of seconds later,
   so a reading taken immediately after it looks like it did nothing.
4. Capping the cache stops the growth happening in the first place.

Run on 2026-09-08, M5 with 16 GB:

    40 distinct shapes, default limits     footprint  696.4 MB   cache 652.2
    clear_cache() immediately              footprint  680.4 MB   cache   0.0
    clear_cache() + 3 s                    footprint   17.8 MB   cache   0.0
    40 distinct shapes, cache limit 0      footprint   60.7 MB   cache   0.0
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

SAM = Path(__file__).resolve().parent.parent
if str(SAM) not in sys.path:
    sys.path.insert(0, str(SAM))

import memstat                                                  # noqa: E402

try:
    import mlx.core as mx
except ImportError as exc:                                      # pragma: no cover
    raise SystemExit(f"mlx is not installed here ({exc}). "
                     "uv sync --project sam --extra mlx") from exc


def line(tag: str) -> None:
    shot = memstat.snapshot(mx)
    m = shot["mlx"]
    print(f"{tag:38s} footprint {shot['footprint_mb']:8.1f} MB   "
          f"active {m.get('active_mb'):7.1f}   cache {m.get('cache_mb'):7.1f}   "
          f"peak {m.get('peak_mb'):7.1f}", flush=True)


def churn(shapes: int, megabytes: int) -> None:
    """Allocate and free `shapes` buffers of slightly different sizes, one
    live at a time. The live set never exceeds one buffer."""
    floats = megabytes * 1048576 // 4
    for k in range(shapes):
        array = mx.zeros((floats + k * 1024,), dtype=mx.float32)
        mx.eval(array)
        del array


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--shapes", type=int, default=40)
    parser.add_argument("--mb", type=int, default=16, help="per buffer")
    parser.add_argument("--settle-s", type=float, default=3.0)
    args = parser.parse_args(argv)

    info = dict(mx.device_info())
    recommended = int(info.get("max_recommended_working_set_size") or 0)
    print(f"device {info.get('device_name')}  "
          f"memory {int(info.get('memory_size', 0)) / 1048576:.0f} MB  "
          f"recommended working set {recommended / 1048576:.0f} MB")

    was_cache = mx.set_cache_limit(1 << 40)
    mx.set_cache_limit(was_cache)
    was_memory = mx.set_memory_limit(1 << 40)
    mx.set_memory_limit(was_memory)
    print(f"MLX defaults: cache limit {was_cache / 1048576:.1f} MB, "
          f"memory limit {was_memory / 1048576:.1f} MB")
    print(f"the work below allocates {args.mb} MB at a time, "
          f"one buffer live at any moment\n")

    line("start")
    churn(args.shapes, args.mb)
    line(f"{args.shapes} distinct shapes, default limits")
    mx.clear_cache()
    line("clear_cache() immediately")
    time.sleep(args.settle_s)
    line(f"clear_cache() + {args.settle_s:.0f} s")

    print()
    mx.set_cache_limit(0)
    line("set_cache_limit(0)")
    churn(args.shapes, args.mb)
    line(f"{args.shapes} distinct shapes, cache limit 0")
    time.sleep(args.settle_s)
    line(f"+{args.settle_s:.0f} s")

    mx.set_cache_limit(was_cache)
    print("\nThe two 'distinct shapes' lines are the whole finding: the same "
          "work, the same live set,\nand a process that is many times bigger "
          "when nothing bounds the allocator's cache.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
