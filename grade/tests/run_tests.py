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
from suite import Suite, FAIL, ERROR, XPASS, SKIP   # noqa: E402

import cases_color            # noqa: E402
import cases_golden           # noqa: E402
import cases_look             # noqa: E402
import cases_params           # noqa: E402
import cases_output           # noqa: E402
import cases_range            # noqa: E402
import cases_cli              # noqa: E402
import cases_window           # noqa: E402
import cases_radial           # noqa: E402
import cases_slice            # noqa: E402
import cases_grain            # noqa: E402
import cases_layers           # noqa: E402
import cases_detail           # noqa: E402
import cases_rotation         # noqa: E402
import cases_match_crop       # noqa: E402
import cases_region           # noqa: E402
import cases_cli_project      # noqa: E402
import cases_cli_render       # noqa: E402
import cases_cli_agent        # noqa: E402
import cases_sheet            # noqa: E402
import cases_input            # noqa: E402
import cases_stats            # noqa: E402
import cases_mask             # noqa: E402
import cases_harness          # noqa: E402

# Round 1 finding 41: the number of tests this suite is declared to
# register, checked at the end of a full run. A FLOOR, not an equality: adding
# tests must never turn a run red, and several lanes add to these modules.
# What it catches is the opposite case, a module that quietly stops
# registering (a rename, a bad merge, a `register()` that returns early), which
# used to shrink both the numerator and the denominator of the printed total
# and stay green.
# Round 2: 341 -> 345, the exact live count after the three cases the round 2
# fixes added (cli_render.width_keeps_the_generated_cache for finding 61,
# mask.radial_cache_name for 67, mask.blur_cap for 52) plus
# harness.cache_prune_refuses_a_symlinked_cache for 66.
# Round 3: 345 -> 347, the exact live count after mask.component_cap (finding
# 82) and mask.legacy_fold_collision (finding 87).
# Round 4 tooling: 347 -> 348, stats.luma_spread_reports_detail_the_mean_cannot_see
# (gap 25).
# Round 4 fixes: 348 -> 349,
# stats.feathered_min_and_max_read_the_mask_not_its_outer_tail (finding 90).
# Round 5 fixes: 349 -> 350,
# stats.a_weight_that_is_not_a_number_is_refused_by_name (finding 103).
EXPECTED_TESTS = 350

MODULES = [cases_color, cases_golden, cases_params, cases_look,
           cases_range, cases_output, cases_window, cases_radial,
           cases_slice, cases_grain, cases_layers, cases_detail,
           cases_rotation, cases_match_crop, cases_region, cases_cli,
           cases_cli_project, cases_cli_render, cases_cli_agent,
           cases_sheet, cases_input, cases_stats, cases_mask,
           cases_harness]


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
    # Round 1 finding 44: this run's generated caches live under its own
    # scratch (harness.CACHE_ROOT), so nothing here can delete a file another
    # run is using. `pre_existing` is kept for the case where a test points
    # the engine somewhere else on purpose.
    pre_existing = set()
    for d in H.CACHE_DIRS:
        if d.exists():
            pre_existing |= set(d.glob("*"))

    suite.run(only_groups=a.group, only_names=a.name, verbose=a.verbose)

    rs = H.render_stats()
    counts = suite.summary()
    skipped = [r for r in suite.results if r["status"] == SKIP]
    extra = [f"{rs['renders']} ffmpeg renders, {rs['cache_hits']} served from "
             f"cache, {rs['ffmpeg_seconds']:.1f}s in ffmpeg",
             f"generated caches under {H.CACHE_ROOT} (this run's own)"]
    # Round 1 finding 41: a skip used to be a quieter green line and nothing
    # else, so a fixture going missing shrank the reported total with no
    # reader able to see it. Every skip is now named in the summary, and the
    # declared floor below fails the run when the suite shrinks at all.
    if skipped:
        extra.append(f"{len(skipped)} SKIPPED, each one a check that did not "
                     f"run:")
        extra += [f"  {r['test'].full}: {r['ctx'].skip_reason}"
                  for r in skipped]
    ran = len(suite.results)
    selected = bool(a.group or a.name)
    if not selected:
        extra.append(f"{ran} of {len(suite.tests)} registered tests ran "
                     f"(declared floor {EXPECTED_TESTS})")
    counts = suite.print_summary(extra_lines=extra)

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
                  f"outside its own cache root")

    bad = counts.get(FAIL, 0) + counts.get(ERROR, 0) + counts.get(XPASS, 0)
    if not selected and len(suite.tests) < EXPECTED_TESTS:
        print(f"  FAIL  {len(suite.tests)} tests are registered, fewer than "
              f"the {EXPECTED_TESTS} this suite declares: a cases_*.py module "
              f"stopped registering. Raise EXPECTED_TESTS when you add tests; "
              f"lowering it to make a run green is what it exists to stop.")
        bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
