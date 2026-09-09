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
    # The node side: ONE reader of the file and ONE picker, shared by both
    # harnesses that start servers. Round 2 finding 58: the parity gate had
    # its own picker over 20000-60000 with no forbidden list at all, so the
    # gate could bind the founder's live studio port while run.mjs beside it
    # could not. The picker moved into lib/util.mjs and both import it, which
    # is what these four checks pin: the reader is the library, and neither
    # harness has grown a picker of its own again.
    util_mjs = (H.CONTENT / "studio" / "tests" / "lib" / "util.mjs").read_text()
    ctx.expect_true("the node harnesses' shared library reads the same file, "
                    "instead of picking freely from 20000-60000 as it used to",
                    "forbidden-ports.json" in util_mjs
                    and "FORBIDDEN_PORTS" in util_mjs
                    and "export async function pickPort" in util_mjs,
                    util_mjs[:200])
    for name in ("run.mjs", "parity-gate.mjs"):
        text = (H.CONTENT / "studio" / "tests" / name).read_text()
        ctx.expect_true(f"{name} takes its ports from that one picker",
                        'pickPort' in text
                        and 'from "./lib/util.mjs"' in text, name)
        ctx.expect_true(f"{name} no longer keeps a picker of its own",
                        "function pickPort" not in text
                        and "findFreePort(" not in text, name)
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


def test_the_cache_prune_refuses_a_symlinked_cache(ctx):
    """Round 2 finding 66, exercised through the real node function.

    `pruneHarnessCache()` deletes files. Its containment guard used to be
    `path.resolve()` plus a string suffix test, and `path.resolve` is
    LEXICAL: it does not look at the disk at all. So a
    `studio/tests/.cache` that was itself a symlink to `studio/cache` passed
    the test, and `readdirSync`/`rmSync` both follow a link, so the prune
    would have deleted the founder's real frame cache. The
    `isSymbolicLink()` skip inside the walk only ever sees entries INSIDE the
    folder, never the folder itself.

    That is not a hypothetical in a worktree: `.venv`, `footage` and `refs`
    at this tree's root are already symlinks into the sibling checkout, so
    "link the warm cache across too" is one shortcut away.

    Run through `node -e` against the real file rather than re-implemented
    here, because a re-implementation would pass while the shipped function
    stayed broken. Three cases: a symlinked cache is refused by name, a
    folder whose real path is somewhere else entirely is refused, and an
    ordinary `studio/tests/.cache` under a temporary root is pruned normally
    (which is what proves the two refusals are the guard talking and not the
    function being broken for everything).
    """
    import shutil                                            # noqa: PLC0415
    import subprocess                                        # noqa: PLC0415
    import tempfile                                          # noqa: PLC0415

    node = shutil.which("node")
    if not node:
        ctx.skip("node is not on PATH, so the node harness cannot be exercised")
        return
    util = H.CONTENT / "studio" / "tests" / "lib" / "util.mjs"
    root = Path(tempfile.mkdtemp(prefix="prune-guard-"))
    try:
        # A real one, at the right path, with a file in it to prune.
        real = root / "studio" / "tests" / ".cache"
        real.mkdir(parents=True)
        (real / "a.png").write_bytes(b"x" * 16)
        # The dangerous one: something precious, and a .cache that is a link
        # to it, at a path that passes any string test.
        precious = root / "studio" / "cache"
        precious.mkdir(parents=True)
        (precious / "keep-me.png").write_bytes(b"y" * 16)
        linked_parent = root / "linked" / "studio" / "tests"
        linked_parent.mkdir(parents=True)
        os.symlink(precious, linked_parent / ".cache")
        # And one that is an honest folder with a dishonest name.
        wrong = root / "somewhere" / "else"
        wrong.mkdir(parents=True)

        script = (
            'const u = await import(process.argv[1]);\n'
            'const out = [];\n'
            'for (const dir of process.argv.slice(2)) {\n'
            '  try { out.push("OK " + JSON.stringify(u.pruneHarnessCache(dir))); }\n'
            '  catch (e) { out.push("THREW " + e.message); }\n'
            '}\n'
            'console.log(out.join("\\n"));\n')
        done = subprocess.run(
            [node, "--input-type=module", "-e", script, str(util),
             str(linked_parent / ".cache"), str(wrong), str(real)],
            capture_output=True, text=True, timeout=120)
        lines = [l for l in done.stdout.splitlines() if l]
        ctx.expect_eq("the node call answered for all three folders",
                      len(lines), 3, )
        if len(lines) != 3:
            ctx.note(done.stdout + done.stderr)
            return
        linked, elsewhere, ordinary = lines
        ctx.expect_true("a .cache that is a symlink is refused, by name",
                        linked.startswith("THREW")
                        and "symlink" in linked, linked[:200])
        ctx.expect_true("and the folder it pointed at still has its file: the "
                        "refusal happened before anything was deleted",
                        (precious / "keep-me.png").is_file(),
                        sorted(p.name for p in precious.iterdir()))
        ctx.expect_true("a folder that is not a studio/tests/.cache is refused",
                        elsewhere.startswith("THREW")
                        and "only ever prunes" in elsewhere, elsewhere[:200])
        ctx.expect_true("and an ordinary studio/tests/.cache is pruned, so the "
                        "two refusals above are the guard and not a function "
                        "that refuses everything",
                        ordinary.startswith("OK"), ordinary[:200])
    finally:
        shutil.rmtree(root, ignore_errors=True)


def register(suite):
    g = "harness"
    suite.add(g, "a_skip_cannot_hide_a_failure", test_a_skip_cannot_hide_a_failure,
              doc="a test that fails and then skips is reported FAIL, not SKIP")
    suite.add(g, "forbidden_ports_are_one_shared_list",
              test_the_forbidden_port_list_is_one_shared_list,
              doc="one JSON list of ports no test may bind, read by the engine "
                  "suite, the studio route suites and run.mjs")
    suite.add(g, "cache_prune_refuses_a_symlinked_cache",
              test_the_cache_prune_refuses_a_symlinked_cache,
              doc="the node harnesses' cache prune refuses a .cache that is a "
                  "symlink, or whose real path is somewhere else")
    suite.add(g, "run_scoped_generated_cache",
              test_this_run_has_its_own_generated_cache,
              doc="this run's baked cubes and mattes live under its own "
                  "scratch, so concurrent runs cannot delete each other's")
