"""Model load timing: SAM3Processor (image/text detector) and SAM3VideoSession
(detector + multiplex tracker). Run each function as its OWN process invocation
(see run_load_timing.sh) so "cold" means a fresh process with the OS file cache
already warm from the earlier download (weights are NOT re-downloaded; this
machine has no way to drop the page cache without root), and "warm" means a
second load inside the SAME process after the first is already resident.
"""
from __future__ import annotations

import sys
import time

from common import REPO_ID, write_json, OUT, model_lock


def time_processor_load():
    t0 = time.perf_counter()
    from mlx_cv.models.sam3 import SAM3Processor
    t_import = time.perf_counter()
    p = SAM3Processor.from_pretrained(REPO_ID)
    t_cold = time.perf_counter()
    # warm: build a second processor in the same process (weights file now in
    # this process's own memory space is irrelevant; what matters is the OS
    # page cache, already warm from the cold load above).
    p2 = SAM3Processor.from_pretrained(REPO_ID)
    t_warm = time.perf_counter()
    return {
        "import_s": t_import - t0,
        "cold_load_s": t_cold - t_import,
        "warm_reload_s": t_warm - t_cold,
    }


def time_video_session_load():
    t0 = time.perf_counter()
    from mlx_cv.models.sam3 import SAM3VideoSession
    t_import = time.perf_counter()
    s = SAM3VideoSession.from_pretrained(REPO_ID)
    t_cold = time.perf_counter()
    s2 = SAM3VideoSession.from_pretrained(REPO_ID)
    t_warm = time.perf_counter()
    return {
        "import_s": t_import - t0,
        "cold_load_s": t_cold - t_import,
        "warm_reload_s": t_warm - t_cold,
    }


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "processor"
    with model_lock():
        if which == "processor":
            result = time_processor_load()
        elif which == "video":
            result = time_video_session_load()
        else:
            raise SystemExit(f"unknown target {which!r}")
    write_json(result, OUT / f"load_timing_{which}.json")
    print(result)
