"""Test registry, assertions and reporting.

Not pytest, because pytest is not installed in content/.venv and the brief was
to avoid adding a dependency. What is actually needed here is small: a way to
register a test, a way to record the numbers a colour test turns on, and a
report that prints those numbers instead of a row of dots.

Three outcomes exist rather than two. XFAIL marks a test whose expectation is
correct but whose engine behaviour is known-broken today, so the suite stays
green while still carrying the real assertion; if that test starts passing it
reports XPASS loudly, because someone fixed the bug and the marker is now a lie.
"""

from __future__ import annotations

import time
import traceback

PASS, FAIL, XFAIL, XPASS, ERROR, SKIP = (
    "PASS", "FAIL", "XFAIL", "XPASS", "ERROR", "SKIP")


class Ctx:
    """Handed to every test. Assertions record their numbers either way."""

    def __init__(self, name: str):
        self.name = name
        self.notes: list[str] = []
        self.failures: list[str] = []
        self.skip_reason: str | None = None

    # -- recording ---------------------------------------------------------

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    def skip(self, reason: str) -> None:
        self.skip_reason = reason

    def check(self, ok: bool, msg: str) -> bool:
        (self.notes if ok else self.failures).append(msg)
        return ok

    # -- numeric assertions ------------------------------------------------

    def expect_close(self, label, got, want, tol) -> bool:
        d = got - want
        return self.check(
            abs(d) <= tol,
            f"{label}: {got:.5f} vs expected {want:.5f} "
            f"(delta {d:+.5f}, tol {tol:g})")

    def expect_gt(self, label, a, b, margin=0.0) -> bool:
        return self.check(
            a > b + margin,
            f"{label}: {a:.6f} > {b:.6f} required "
            f"(delta {a - b:+.6f}, margin {margin:g})")

    def expect_lt(self, label, a, b, margin=0.0) -> bool:
        return self.check(
            a < b - margin,
            f"{label}: {a:.6f} < {b:.6f} required "
            f"(delta {a - b:+.6f}, margin {margin:g})")

    def expect_ge(self, label, a, b) -> bool:
        return self.check(a >= b, f"{label}: {a:.6f} >= {b:.6f} required "
                                  f"(delta {a - b:+.6f})")

    def expect_le(self, label, a, b) -> bool:
        return self.check(a <= b, f"{label}: {a:.6f} <= {b:.6f} required "
                                  f"(delta {a - b:+.6f})")

    def expect_between(self, label, got, lo, hi, tol=0.0) -> bool:
        return self.check(
            lo - tol <= got <= hi + tol,
            f"{label}: {got:.6f} inside [{lo:.6f}, {hi:.6f}] required "
            f"(tol {tol:g})")

    def expect_true(self, label, cond, detail="") -> bool:
        return self.check(bool(cond), f"{label}: {detail or cond}")

    def expect_eq(self, label, got, want) -> bool:
        return self.check(got == want, f"{label}: {got!r} == {want!r} required")


class Test:
    def __init__(self, group, name, fn, xfail=None, doc=""):
        self.group = group
        self.name = name
        self.fn = fn
        self.xfail = xfail          # reason string when this is expected to fail
        self.doc = doc

    @property
    def full(self) -> str:
        return f"{self.group}.{self.name}"


class Suite:
    def __init__(self):
        self.tests: list[Test] = []
        self.results: list[dict] = []

    def add(self, group, name, fn, xfail=None, doc=""):
        self.tests.append(Test(group, name, fn, xfail, doc))

    def groups(self) -> list[str]:
        seen = []
        for t in self.tests:
            if t.group not in seen:
                seen.append(t.group)
        return seen

    def run(self, only_groups=None, only_names=None, verbose=False):
        for t in self.tests:
            if only_groups and t.group not in only_groups:
                continue
            if only_names and not any(n in t.full for n in only_names):
                continue
            ctx = Ctx(t.full)
            t0 = time.time()
            err = None
            try:
                t.fn(ctx)
            except Exception:
                err = traceback.format_exc()
            elapsed = time.time() - t0

            if ctx.skip_reason:
                status = SKIP
            elif err is not None:
                status = XFAIL if t.xfail else ERROR
                ctx.failures.append(err.strip().splitlines()[-1])
            elif ctx.failures:
                status = XFAIL if t.xfail else FAIL
            else:
                status = XPASS if t.xfail else PASS

            self.results.append({
                "test": t, "status": status, "ctx": ctx,
                "elapsed": elapsed, "traceback": err})
            self._print_one(self.results[-1], verbose)
        return self.results

    # -- reporting ---------------------------------------------------------

    @staticmethod
    def _print_one(res, verbose):
        t, ctx, status = res["test"], res["ctx"], res["status"]
        print(f"[{status:5}] {t.full:52} {res['elapsed']:5.2f}s")
        # A passing test still shows one real measurement. A suite that reports
        # only dots gives you no way to tell a change from a regression when
        # the numbers move but stay inside tolerance.
        if status == PASS and not verbose and ctx.notes:
            print(f"         {ctx.notes[0]}")
        if t.doc and (verbose or status not in (PASS,)):
            print(f"         what: {t.doc}")
        if status == SKIP:
            print(f"         skipped: {ctx.skip_reason}")
        for f in ctx.failures:
            print(f"         FAILED  {f}")
        if t.xfail and status in (XFAIL, XPASS):
            print(f"         expected-fail reason: {t.xfail}")
        if verbose or status in (FAIL, ERROR, XPASS):
            for n in ctx.notes:
                print(f"         ok      {n}")
        if res["traceback"] and status in (ERROR,):
            for line in res["traceback"].strip().splitlines()[-6:]:
                print(f"         | {line}")

    def summary(self) -> dict:
        counts = {}
        for r in self.results:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        return counts

    def print_summary(self, extra_lines=()):
        counts = self.summary()
        total = len(self.results)
        print()
        print("=" * 78)
        order = [PASS, FAIL, XFAIL, XPASS, ERROR, SKIP]
        parts = [f"{k} {counts.get(k, 0)}" for k in order if counts.get(k)]
        print(f"  {total} tests: " + "   ".join(parts))
        for line in extra_lines:
            print(f"  {line}")
        bad = [r for r in self.results if r["status"] in (FAIL, ERROR)]
        if bad:
            print()
            print("  failures:")
            for r in bad:
                print(f"    {r['status']:5}  {r['test'].full}")
                for f in r["ctx"].failures:
                    print(f"           {f}")
        xp = [r for r in self.results if r["status"] == XPASS]
        if xp:
            print()
            print("  expected-fail tests that now PASS "
                  "(the engine bug looks fixed, drop the marker):")
            for r in xp:
                print(f"    {r['test'].full}")
        xf = [r for r in self.results if r["status"] == XFAIL]
        if xf:
            print()
            print("  known-broken, asserted anyway:")
            for r in xf:
                print(f"    {r['test'].full}: {r['test'].xfail}")
        print("=" * 78)
        return counts
