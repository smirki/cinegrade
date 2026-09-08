#!/usr/bin/env python3
"""Every test the service has, in one command.

    uv run --project sam python sam/tests/run.py

Nothing here needs weights, a network or the machine wide model lock: all
three suites run against the stub backend, so this is safe to run while
another process holds the model. The real model smoke test is deliberately
NOT in this list, because it loads about 5 GB and takes the lock; run it on
purpose:

    uv run --project sam python sam/tests/test_real_model.py

Each suite is a plain script that prints one line per assertion and exits
non zero if any of them failed, the same shape as the studio's own python
tests, so there is no test framework in this project's dependencies.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PYTHON = HERE.parent / ".venv" / "bin" / "python"

SUITES = [
    ("test_store.py", "the matte store and steady smoothing (C2)"),
    ("test_model_lock.py", "the machine wide model lock"),
    ("test_stub_backend.py", "the prompt planner and the stub backend (C7)"),
    ("test_torch_adapter.py", "the torch adapter's windowing and id mapping"),
    ("test_rotation.py", "the studio's \"auto\" rotation resolved from a clip's tag"),
    ("test_memory.py", "the memory readings, the window meter and the MLX limits"),
    ("service_e2e.py", "the service over real HTTP (C3)"),
]


def main() -> int:
    if not PYTHON.is_file():
        raise SystemExit(f"{PYTHON} is missing: run `uv sync --project sam` first")
    total = passed = 0
    failures: list[str] = []
    for name, what in SUITES:
        print(f"\n=== {name}: {what}")
        began = time.time()
        result = subprocess.run([str(PYTHON), str(HERE / name)],
                                capture_output=True, text=True, timeout=900)
        out = result.stdout + result.stderr
        print(out.rstrip())
        match = re.search(r"(\d+)/(\d+) checks passed", out)
        if match:
            passed += int(match.group(1))
            total += int(match.group(2))
        else:
            failures.append(f"{name}: printed no count (exit {result.returncode})")
        if result.returncode != 0:
            failures.append(f"{name}: exit {result.returncode}")
        print(f"--- {name} in {time.time() - began:.1f}s")

    print(f"\n{'=' * 60}\nsam: {passed}/{total} checks passed "
          f"across {len(SUITES)} suites")
    for line in failures:
        print(f"  FAILED {line}")
    return 1 if failures or passed != total else 0


if __name__ == "__main__":
    raise SystemExit(main())
