#!/usr/bin/env python3
"""Every test the service has, in one command.

    uv run --project sam python sam/tests/run.py

Nothing here needs weights, a network or the machine wide model lock: all
three suites run against the stub backend, so this is safe to run while
another process holds the model. The real model smoke test is deliberately
NOT in this list, because it loads about 5 GB and takes the lock; run it on
purpose:

    uv run --project sam python sam/tests/test_real_model.py

Most suites are plain scripts that print one line per assertion and exit non
zero if any of them failed, the same shape as the studio's own python tests.
One (`test_torch_backend.py`) is pytest shaped, because it was written by the
lane that built the CPU floor backend and pytest is already in this project's
dev group; it is run here rather than left out, which is how a backend with
its own tests went ungated (round 1 finding 36).

Two things this runner does that a plain "add the numbers up" runner does not,
both from round 1 finding 41:

* A **skip is printed and counted**, never folded into the total. A suite that
  quietly dropped four checks used to shrink its numerator and denominator
  together, so 358/358 stayed 358/358 and a reader could not tell that a
  footage clip had gone missing.
* Every suite declares the number of checks it must not fall below (`floor`).
  A suite that suddenly reports fewer fails the gate and says so, so a check
  that disappears is as loud as a check that breaks. Raise a floor when you
  add checks; lowering one is a decision somebody has to type.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PYTHON = HERE.parent / ".venv" / "bin" / "python"

# The longest any one suite may take. service_e2e.py starts real servers and
# waits on real HTTP, so this is generous on purpose; what it is really there
# for is a suite that hangs forever.
SUITE_TIMEOUT_S = 900

# name, what it covers, the count it must not fall below, how it is run
SUITES = [
    ("test_store.py", "the matte store and steady smoothing (C2)", 53, "script"),
    ("test_model_lock.py", "the machine wide model lock", 17, "script"),
    ("test_stub_backend.py", "the prompt planner and the stub backend (C7)", 31,
     "script"),
    ("test_torch_adapter.py", "the torch adapter's windowing and id mapping", 15,
     "script"),
    ("test_torch_backend.py", "the CPU floor backend's own tests (no weights, "
     "no lock)", 7, "pytest"),
    ("test_rotation.py", "the studio's \"auto\" rotation resolved from a clip's tag",
     19, "script"),
    ("test_memory.py", "the memory readings, the window meter and the MLX limits",
     71, "script"),
    ("test_safety.py", "the lock's ownership, the store's paths, the honest "
     "/health, the pick's mask seed", 93, "script"),
    ("test_quiet.py", "quiet mode: the duty cycle, nice, the preset, responsiveness",
     88, "script"),
    ("service_e2e.py", "the service over real HTTP (C3)", 90, "script"),
]


def _counts(kind: str, out: str) -> tuple[int, int, int] | None:
    """(passed, total, skipped) for one suite, or None when it printed nothing
    this runner can read. `total` counts only checks that actually ran, so a
    skip never reads as a pass."""
    if kind == "pytest":
        # pytest -q ends with a line like "7 passed, 2 skipped in 1.23s".
        def one(word: str) -> int:
            found = re.search(rf"(\d+) {word}", out)
            return int(found.group(1)) if found else 0

        ran = one("passed") + one("failed") + one("error") + one("errors")
        if not ran and not one("skipped"):
            return None
        return one("passed"), ran, one("skipped")
    match = re.search(r"(\d+)/(\d+) checks passed", out)
    if not match:
        return None
    skipped = re.search(r"(\d+) checks skipped", out)
    return (int(match.group(1)), int(match.group(2)),
            int(skipped.group(1)) if skipped else 0)


def main() -> int:
    if not PYTHON.is_file():
        raise SystemExit(f"{PYTHON} is missing: run `uv sync --project sam` first")
    total = passed = skipped = 0
    failures: list[str] = []
    rows: list[tuple] = []
    for name, what, floor, kind in SUITES:
        print(f"\n=== {name}: {what}")
        # `-rs` prints the reason for every skip rather than a bare "s", so a
        # pytest suite is as honest about what did not run as the plain ones
        # are (round 1 finding 41). `-p no:cacheprovider` keeps a .pytest_cache
        # out of the tree.
        command = ([str(PYTHON), "-m", "pytest", str(HERE / name), "-q", "-rs",
                    "-p", "no:cacheprovider"] if kind == "pytest"
                   else [str(PYTHON), str(HERE / name)])
        began = time.time()
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=SUITE_TIMEOUT_S,
                                    # pytest only: `python -m pytest` puts the
                                    # working directory on sys.path, which is
                                    # how `from backends...` resolves without
                                    # a conftest or an installed package. The
                                    # plain scripts put sam/ on the path
                                    # themselves.
                                    cwd=str(HERE.parent) if kind == "pytest"
                                    else None)
        except subprocess.TimeoutExpired:
            # Round 2 finding 74: this used to propagate straight out of
            # main(), so a suite that hung took the remaining suites and the
            # whole summary table with it. The exit code was still non-zero
            # (never a false green), but the person reading it lost every
            # other suite's result to one stuck one. Now it is one red row and
            # the run carries on.
            print(f"{name}: TIMED OUT after {SUITE_TIMEOUT_S}s")
            failures.append(f"{name}: timed out after {SUITE_TIMEOUT_S}s")
            rows.append((name, 0, 0, 0, floor))
            continue
        out = result.stdout + result.stderr
        print(out.rstrip())
        counts = _counts(kind, out)
        if counts is None:
            failures.append(f"{name}: printed no count (exit {result.returncode})")
            rows.append((name, 0, 0, 0, floor))
        else:
            suite_passed, suite_total, suite_skipped = counts
            passed += suite_passed
            total += suite_total
            skipped += suite_skipped
            rows.append((name, suite_passed, suite_total, suite_skipped, floor))
            if suite_total < floor:
                # Fewer checks than last time is a finding, not a detail: this
                # is what made a missing clip look identical to a passing run
                # (round 1 finding 41).
                failures.append(
                    f"{name}: only {suite_total} checks ran and this suite "
                    f"declares a floor of {floor}"
                    + (f" ({suite_skipped} skipped)" if suite_skipped else "")
                    + ". Either the material a check needs is missing, or the "
                      "check is gone; raise or lower the floor in run.py on "
                      "purpose.")
        if result.returncode != 0:
            failures.append(f"{name}: exit {result.returncode}")
        print(f"--- {name} in {time.time() - began:.1f}s")

    print(f"\n{'=' * 60}")
    for name, suite_passed, suite_total, suite_skipped, floor in rows:
        print(f"  {name:24} {suite_passed:>4}/{suite_total:<4} "
              f"floor {floor:<4} "
              + (f"{suite_skipped} skipped" if suite_skipped else ""))
    print(f"\nsam: {passed}/{total} checks passed across {len(SUITES)} suites"
          + (f", {skipped} skipped" if skipped else ", none skipped"))
    if skipped:
        print("  a skip is not a pass: the lines above name each one and why")
    for line in failures:
        print(f"  FAILED {line}")
    return 1 if failures or passed != total else 0


if __name__ == "__main__":
    raise SystemExit(main())
