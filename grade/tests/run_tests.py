#!/usr/bin/env python3
"""cinegrade regression suite.

    content/.venv/bin/python content/grade/tests/run_tests.py
    content/.venv/bin/python content/grade/tests/run_tests.py --group color params
    content/.venv/bin/python content/grade/tests/run_tests.py --group golden --update

Exit code is 0 only when nothing failed. XFAIL (a known engine bug with a
correct test written against it) does not fail the run; XPASS does, because it
means the marker is stale.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
import warnings
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

# colour-science prints a startup warning about the missing scipy and
# matplotlib extras. Neither is used here, and the noise buries the report.
warnings.filterwarnings("ignore", module="colour")

import harness as H           # noqa: E402
from suite import Suite, FAIL, ERROR, XPASS   # noqa: E402

import cases_color            # noqa: E402
import cases_golden           # noqa: E402
import cases_look             # noqa: E402
import cases_params           # noqa: E402
import cases_output           # noqa: E402
import cases_range            # noqa: E402
import cases_cli              # noqa: E402

MODULES = [cases_color, cases_golden, cases_params, cases_look,
           cases_range, cases_output, cases_cli]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--group", "-g", nargs="*", help="only these groups")
    ap.add_argument("--name", "-k", nargs="*", help="substring match on test name")
    ap.add_argument("--update", action="store_true",
                    help="re-bless the golden fingerprints instead of checking them")
    ap.add_argument("--list", action="store_true", help="list tests and exit")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="print the passing assertions too, with their numbers")
    ap.add_argument("--keep-work", action="store_true",
                    help="keep tests/_work/ (renders, patch images) after the run")
    a = ap.parse_args()

    for missing in [p for p in (H.CLIP_A, H.CLIP_B) if not p.exists()]:
        sys.exit(f"missing source clip: {missing}\n"
                 f"the suite grades real footage, it cannot run without it")

    cases_golden.UPDATE = a.update

    suite = Suite()
    for m in MODULES:
        m.register(suite)

    if a.list:
        for t in suite.tests:
            mark = "  (expected-fail)" if t.xfail else ""
            print(f"{t.full:56} {t.doc}{mark}")
        print(f"\n{len(suite.tests)} tests in groups: "
              f"{', '.join(suite.groups())}")
        return 0

    if a.update:
        print("UPDATE MODE: golden fingerprints will be rewritten, not checked.\n")

    H.WORK.mkdir(parents=True, exist_ok=True)
    # Files the engine's caches already held before the run. Anything in that
    # set is left alone even if the suite used it.
    pre_existing = set()
    for d in H.CACHE_DIRS:
        if d.exists():
            pre_existing |= set(d.glob("*"))

    suite.run(only_groups=a.group, only_names=a.name, verbose=a.verbose)

    rs = H.render_stats()
    counts = suite.print_summary(extra_lines=[
        f"{rs['renders']} ffmpeg renders, {rs['cache_hits']} served from cache, "
        f"{rs['ffmpeg_seconds']:.1f}s in ffmpeg",
    ])

    if not a.keep_work:
        if H.WORK.exists():
            shutil.rmtree(H.WORK, ignore_errors=True)
        # Only this run's own scratch is removed above. Sweep siblings only
        # once they are far older than a run takes, so a suite running
        # concurrently in another process keeps its files.
        stale = time.time() - 3600
        for d in H.WORK_ROOT.glob("*"):
            if d.stat().st_mtime >= stale:
                continue
            shutil.rmtree(d, ignore_errors=True) if d.is_dir() else d.unlink()
        removed = 0
        for f in H.touched_cache_files() - pre_existing:
            if f.exists():
                f.unlink()
                removed += 1
        if removed:
            print(f"  cleaned up {removed} cache files this run baked "
                  f"under grade/luts/")

    bad = counts.get(FAIL, 0) + counts.get(ERROR, 0) + counts.get(XPASS, 0)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
