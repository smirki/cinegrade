"""Group: the suite's own machinery, where a bug hides other bugs.

Everything else in this directory tests the engine. These three test the
harness, and they exist because round 1 found three ways this suite could
report green while doing less than it claimed:

  finding 41  `suite.run()` decided a test's status by looking at
              `ctx.skip_reason` BEFORE `err` and `ctx.failures`, so a test
              that recorded a real failure and then hit a skip condition was
              reported SKIP and left the exit code at 0.
  finding 20  four hand rolled "never bind these ports" lists in four files
              disagreed with each other, and none of them held the port a
              live agent seat's studio was on. A test that lands on one of
              those takes down something somebody is using.
  finding 44  every run baked its generated LUT cubes and mattes into
              grade/luts/, and the end of a run deletes what it baked, so two
              runs against this tree deleted each other's cache mid-run.

A harness bug is worth a test of its own precisely because it cannot show up
as a failure anywhere else: it changes what "green" means.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import harness as H
import ports as P
from suite import ERROR, FAIL, PASS, SKIP, Ctx, Suite


def test_a_skip_cannot_hide_a_failure(ctx):
    """Round 1 finding 41, as an assertion on the reporting code itself.

    Four one-test suites are run through the real `Suite.run()`: one that
    only skips, one that fails and then skips, one that raises and then
    skips, and one that passes. Only the first is a SKIP.

    On the old precedence the middle two came back SKIP, so a test that had
    genuinely failed was reported as one that had not run, and the run's exit
    code stayed 0. That is the shape of bug that makes an entire gate
    meaningless, which is why it is pinned here rather than trusted.
    """
    def only_skips(c: Ctx):
        c.skip("nothing to do on this machine")

    def fails_then_skips(c: Ctx):
        c.check(False, "a real failure, recorded before the skip")
        c.skip("and then a skip condition, for example a missing fixture")

    def raises_then_skips(c: Ctx):
        c.skip("a skip recorded before the code blew up")
        raise RuntimeError("the thing under test raised")

    def just_passes(c: Ctx):
        c.check(True, "measured something")

    cases = [("only_skips", only_skips, SKIP),
             ("fails_then_skips", fails_then_skips, FAIL),
             ("raises_then_skips", raises_then_skips, ERROR),
             ("just_passes", just_passes, PASS)]
    for name, fn, want in cases:
        inner = Suite()
        inner.add("inner", name, fn)
        # `Suite.run` prints as it goes; that is the real code path and the
        # extra lines are worth more than a silenced version of it would be.
        results = inner.run()
        got = results[0]["status"]
        ctx.expect_eq(f"a test that {name.replace('_', ' ')} reports {want}",
                      got, want)
    fails = [r for r in Suite().run()]
    ctx.expect_eq("an empty selection reports nothing at all", len(fails), 0)


def test_the_forbidden_port_list_is_one_shared_list(ctx):
    """Round 1 finding 20: one list, in one file, read by everything.

    What is checked is the property, not the copy: this suite's reader, the
    studio route suites' reader and `studio/tests/run.mjs` all name the same
    JSON file, that file holds the three ports the review found missing
    (7431, 7615 and 7632) plus the two the spike script had found real
    servers on, and the picker really does refuse to hand one out.

    A test cannot prove "nobody wrote a fifth list somewhere", but it can
    prove there is no longer a list in the files that used to hold one, which
    is what the greps below do.
    """
    shared = H.CONTENT / "studio" / "tests" / "forbidden-ports.json"
    ctx.expect_true("the shared list exists where every reader looks for it",
                    shared.is_file(), str(shared))
    raw = json.loads(shared.read_text())
    listed = {int(p) for p in raw["ports"]}
    ctx.expect_eq("this suite's reader reads that file and nothing else",
                  P.LIST_PATH.resolve(), shared.resolve())
    ctx.expect_eq("and it reads every port out of it", set(P.FORBIDDEN_PORTS),
                  listed)
    # The three the review named, and the two the spike had already found
    # real servers on. Named individually because "the file has some ports in
    # it" is not the finding: 7632 (a live agent seat's studio) was missing
    # from all four of the old lists.
    for port, why in ((7431, "the founder's live studio"),
                      (7560, "the SAM service a real studio expects"),
                      (7614, "reserved for a studio a person is using"),
                      (7615, "reserved for a studio a person is using"),
                      (7632, "a live agent seat's studio in this worktree"),
                      (22929, "a real server was found on it"),
                      (28958, "a real server was found on it")):
        ctx.expect_true(f"{port} is on the list ({why})", port in listed,
                        sorted(listed))
        ctx.expect_true(f"{port} says why it is on the list",
                        bool(raw.get("why", {}).get(str(port))),
                        sorted(raw.get("why", {})))

    # The readers, one per language, all pointing at the file above.
    py_reader = (H.CONTENT / "studio" / "tests" / "py" / "ports.py").read_text()
    ctx.expect_true("the studio route suites read the same file",
                    "forbidden-ports.json" in py_reader, py_reader[:200])
    run_mjs = (H.CONTENT / "studio" / "tests" / "run.mjs").read_text()
    ctx.expect_true("the browser harness reads the same file, instead of "
                    "picking freely from 20000-60000 as it used to",
                    "forbidden-ports.json" in run_mjs
                    and "FORBIDDEN_PORTS" in run_mjs,
                    run_mjs[:200])
    for name in ("cases_cli_project.py", "cases_cli_agent.py"):
        text = (H.TESTS / name).read_text()
        ctx.expect_true(f"{name} uses the shared picker",
                        "from ports import" in text, name)
        ctx.expect_true(f"{name} no longer keeps a picker of its own",
                        "def _free_port(" not in text, name)

    # And the picker itself: 200 draws, none of them forbidden. It picks from
    # the kernel's ephemeral range, so this is a real sample of what a suite
    # start would have got, not a mock.
    drawn = {P.free_port() for _ in range(200)}
    ctx.expect_true("200 ports drawn, not one of them forbidden",
                    not (drawn & set(P.FORBIDDEN_PORTS)),
                    sorted(drawn & set(P.FORBIDDEN_PORTS)))
    ctx.expect_gt("and the draws are real, not one port 200 times",
                  float(len(drawn)), 1.0)


def test_this_run_has_its_own_generated_cache(ctx):
    """Round 1 finding 44: two runs cannot delete each other's cache.

    `run_tests.py` removes the generated cache files a run baked when it
    finishes. While that cache was grade/luts/ for every run on this tree,
    a second suite running at the same time had its files deleted underneath
    it. The cache root is now inside this run's own per-process scratch.
    """
    root = H.CACHE_ROOT
    ctx.expect_true("this run's cache root is inside its own scratch, so no "
                    "other run can see it", str(root).startswith(str(H.WORK)),
                    f"{root} vs {H.WORK}")
    ctx.expect_true("it is not grade/luts, which every run used to share",
                    Path(root).resolve() != (H.GRADE).resolve(), str(root))
    ctx.expect_eq("the CLI subprocesses this suite spawns are told the same "
                  "root, so a child bakes where the parent cleans up",
                  os.environ.get("CINEGRADE_CACHE_DIR"), str(root))
    # The engine's own resolver, asked live: all three generated caches under
    # this root. `H.cg` is the same module object every test in this suite
    # renders through, so this is the setting in force, not a re-derivation.
    cg = H.cg
    for label, got in (("layer cubes", cg.lut_layers_dir()),
                       ("generated mattes", cg.lut_masks_dir()),
                       ("Color Slice cubes", cg.lut_slice_dir())):
        ctx.expect_true(f"{label} land under this run's own root",
                        str(got).startswith(str(root)), f"{label}: {got}")
    ctx.expect_true("and the two constants legacy_parity.py normalises with "
                    "point at the same place",
                    str(cg.LUT_LAYERS).startswith(str(root))
                    and str(cg.LUT_MASKS).startswith(str(root)),
                    f"{cg.LUT_LAYERS} / {cg.LUT_MASKS}")


def register(suite):
    g = "harness"
    suite.add(g, "a_skip_cannot_hide_a_failure", test_a_skip_cannot_hide_a_failure,
              doc="a test that fails and then skips is reported FAIL, not SKIP")
    suite.add(g, "forbidden_ports_are_one_shared_list",
              test_the_forbidden_port_list_is_one_shared_list,
              doc="one JSON list of ports no test may bind, read by the engine "
                  "suite, the studio route suites and run.mjs")
    suite.add(g, "run_scoped_generated_cache",
              test_this_run_has_its_own_generated_cache,
              doc="this run's baked cubes and mattes live under its own "
                  "scratch, so concurrent runs cannot delete each other's")
