#!/usr/bin/env python3
"""Quiet mode: the duty cycle, nice, the preset, and staying responsive.

Everything here runs on the stub backend, on plain arithmetic, and on one
subprocess for `os.nice` (which is a one way door for an unprivileged process,
so it is never applied to the test runner itself). No weights, no network, no
machine wide model lock, so this is safe to run while another process holds the
model.

What it can prove without the model, and does:

* the arithmetic, exactly, with an injected clock and no wall time at all;
* the real sleep, on the stub, which is why the stub honours the duty cycle:
  otherwise the only way to check the timing would be to load 1.7 GB of weights
  and take the machine wide lock, and the gate would never check it;
* that a rest does not make the service unresponsive, which is the whole risk
  in this feature: /health and /jobs answer during a rest, a queued job keeps
  reporting its place in the queue, and a cancel ends the job in milliseconds
  instead of waiting the rest out;
* that a job is never reported at 100 percent and still running, which is what
  the owe / settle pair exists for.

What it cannot prove is the effect on the founder's machine. That is the real
model measurement in the checkpoint (`QUIET-MODE.md`), not something a stub can
answer.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (PYTHON, ROOT, SAM, call, check, free_port,   # noqa: E402
                    make_frames, report, sam_path, start, stop, wait_for_job)

sam_path()
import throttle                                                 # noqa: E402
from backends import make_backend                                # noqa: E402
from backends.base import Cancelled                              # noqa: E402
from server import build_parser                                  # noqa: E402


class FakeClock:
    """A clock that only moves when something asks it to, and a waiter that
    moves it by exactly the time it was asked to wait.

    This is what makes the arithmetic checkable to the millisecond: a real
    sleep of 0.2 s measures 0.205 s and every assertion would need a tolerance
    wide enough to hide a real error.
    """

    def __init__(self) -> None:
        self.now = 1000.0
        self.waits: list[float] = []
        self.interrupt_after: float | None = None
        # The real waiter is Event.wait, so the fake one has to answer the
        # event too or it would pass a cancel path that does not work.
        self.event = None

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)

    def wait(self, timeout: float) -> bool:
        """Stand in for Event.wait: returns True when it was woken early."""
        self.waits.append(round(float(timeout), 6))
        if self.event is not None and self.event.is_set():
            return True
        if self.interrupt_after is not None and self.interrupt_after < timeout:
            self.now += self.interrupt_after
            return True
        self.now += float(timeout)
        return False


def a_throttle(fraction, clock=None, **kwargs):
    clock = clock or FakeClock()
    duty = throttle.DutyCycle(fraction, clock=clock, waiter=clock.wait,
                              **kwargs)
    clock.event = duty.wake
    return duty, clock


# ---------------------------------------------------------------------------


def test_parsing() -> None:
    print("0 < F <= 1, and a sentence when it is not")
    check("1 is the default and is accepted",
          throttle.parse_duty_cycle(1) == 1.0
          and throttle.DEFAULT_DUTY_CYCLE == 1.0)
    check("a string from a config file parses",
          throttle.parse_duty_cycle("0.25") == 0.25)
    for bad in (0, 0.0, -0.5, 1.5, 2, "half", None, float("nan")):
        try:
            throttle.parse_duty_cycle(bad)
            check(f"{bad!r} is refused", False, "it was accepted")
        except ValueError as exc:
            check(f"{bad!r} is refused", True, str(exc)[:48])
    try:
        throttle.parse_duty_cycle(0)
    except ValueError as exc:
        check("the refusal says what to type instead, because a bad number "
              "here is a typo and not a design question",
              "0.5" in str(exc) and "no throttling" in str(exc))


def test_arithmetic() -> None:
    print("\nthe arithmetic: busy / (busy + idle) = F")
    off, _ = a_throttle(1.0)
    check("a duty cycle of 1 is not throttling", off.enabled is False)
    check("and it never sleeps, however long the work took",
          off.rest_for(1000.0) == 0.0)

    half, clock = a_throttle(0.5)
    check("at 0.5 the rest is as long as the work", half.rest_for(10.0) == 10.0)
    quarter, _ = a_throttle(0.25)
    check("at 0.25 the rest is three times the work",
          quarter.rest_for(10.0) == 30.0)
    tenth, _ = a_throttle(0.1)
    check("at 0.1 the rest is nine times the work", tenth.rest_for(1.0) == 9.0)

    capped, _ = a_throttle(0.5, max_rest_s=5.0)
    check("one rest is never longer than max_rest_s, so a window that took "
          "twenty minutes on a swapping machine cannot park the queue for "
          "another twenty", capped.rest_for(600.0) == 5.0)

    token = half.begin()
    clock.advance(4.0)
    woken = half.rest(token)
    check("a 4 s stretch at 0.5 sleeps 4 s and is not woken",
          clock.waits == [4.0] and woken is False)
    check("the busy and idle seconds are both accounted",
          (half.busy_s, half.idle_s) == (4.0, 4.0),
          f"{half.busy_s} / {half.idle_s}")
    check("and the MEASURED fraction is the asked-for one",
          half.busy_fraction == 0.5)
    check("the last unit of work is on the record for /health",
          half.last == {"unit": "window", "busy_s": 4.0, "rest_s": 4.0,
                        "asked_rest_s": 4.0, "woken": False}, str(half.last))

    token = half.begin()
    clock.advance(2.0)
    half.rest(token, unit="pick")
    check("units are counted separately, so a run's picks and its windows can "
          "be told apart",
          half.units["window"]["count"] == 1 and half.units["pick"]["count"] == 1
          and half.units["pick"]["busy_s"] == 2.0, str(half.units))
    check("two windows and a pick, and the fraction still holds",
          half.busy_fraction == 0.5, str(half.busy_fraction))

    # A duty cycle of 1 still ACCOUNTS the busy time, which is what makes the
    # measured fraction meaningful when quiet mode is off: 1.0, honestly.
    off, off_clock = a_throttle(1.0)
    token = off.begin()
    off_clock.advance(7.0)
    off.rest(token)
    check("with quiet mode off the work is still measured, so /health can say "
          "the model owned all of the wall clock rather than saying nothing",
          off.busy_s == 7.0 and off.idle_s == 0.0 and off.busy_fraction == 1.0)
    check("and no wait was ever issued", off_clock.waits == [])


def test_reading_it_while_it_rests() -> None:
    print("\nreading the numbers DURING a rest")
    half, clock = a_throttle(0.5)
    # A rest of 100 s that is interrupted 25 s in.
    clock.interrupt_after = 25.0
    token = half.begin()
    clock.advance(100.0)
    woken = half.rest(token)
    check("an interrupted rest says so and counts only the time it slept",
          woken is True and half.idle_s == 25.0 and half.woken_early == 1,
          f"idle {half.idle_s}")
    check("so the measured fraction is HIGHER than the setting, which is the "
          "honest reading of a run full of cancels",
          half.busy_fraction == 0.8, str(half.busy_fraction))

    # The in-flight part of a rest counts. Without this, three seconds into a
    # hundred second sleep /health read busy_fraction 1.00, the opposite of
    # what was happening.
    live, live_clock = a_throttle(0.5)

    def peek(timeout):
        live_clock.advance(timeout / 2.0)
        peek.mid = live.stats()
        live_clock.advance(timeout / 2.0)
        return False

    live._waiter = peek
    token = live.begin()
    live_clock.advance(10.0)
    live.rest(token)
    check("halfway through a rest the reading is halfway, not 1.0",
          peek.mid["busy_fraction"] == 0.6667
          and peek.mid["idle_s"] == 5.0, str(peek.mid["busy_fraction"]))
    check("and it says it is resting, with the seconds left, so a deliberately "
          "idle service reads as idle rather than as stuck",
          peek.mid["resting"] is True and peek.mid["rest_left_s"] == 5.0,
          f"{peek.mid['resting']} {peek.mid['rest_left_s']}")
    check("once the rest is over it is not resting any more",
          live.stats()["resting"] is False
          and live.stats()["rest_left_s"] is None)

    # Round 2 finding 78. The worker thread clears _rest_began and _rest_until
    # in rest()'s finally the instant its sleep ends, and /health is served on
    # another thread. Reading the field twice (once to test it for None, once
    # to subtract it) is a torn read: the rest can end in between. `TearingClock`
    # is that race made deterministic, because it clears both fields exactly
    # when the clock is read, which is the middle of both expressions.
    class TearingClock:
        def __init__(self, duty):
            self.duty = duty
            self.now = 1000.0

        def __call__(self):
            self.duty._rest_began = None
            self.duty._rest_until = None
            return self.now

    torn = throttle.DutyCycle(0.5, clock=lambda: 1000.0)
    torn._clock = TearingClock(torn)
    torn.idle_s = 4.0
    torn._rest_began = 990.0
    torn._rest_until = 1010.0
    try:
        idle = torn.idle_now()
        blew_up = ""
    except TypeError as exc:
        idle, blew_up = None, str(exc)
    check("a rest that finishes while /health is being served does not blow "
          "the reading up: idle_now snapshots the start of the rest before it "
          "subtracts it, instead of reading the field a second time",
          not blew_up and idle == 14.0, blew_up or str(idle))

    torn._rest_began = 990.0
    torn._rest_until = 1010.0
    block = torn.stats()
    check("and the rest block is one consistent read, never 'not resting' with "
          "ten seconds still left on the clock",
          (block["resting"] is True) == (block["rest_left_s"] is not None),
          f"resting {block['resting']} left {block['rest_left_s']}")


def test_owe_and_settle() -> None:
    print("\nowe and settle: the last window's rest, after the job is reported")
    half, clock = a_throttle(0.5)
    check("nothing is owed to start with", half.owed is False)
    check("and settling nothing is free",
          half.settle() is False and clock.waits == [])

    token = half.begin()
    clock.advance(6.0)
    half.owe(token, unit="seed detect")
    check("owing does not sleep: that is the point, the caller has something "
          "to report first", half.owed is True and clock.waits == [])
    half.settle()
    check("settling takes the rest, under the unit it was owed as",
          clock.waits == [6.0] and half.units["seed detect"]["count"] == 1
          and half.owed is False, str(half.units))

    cancelled, cancelled_clock = a_throttle(0.5)
    token = cancelled.begin()
    cancelled_clock.advance(120.0)
    cancelled.owe(token)
    cancelled.interrupt()
    check("after a cancel the wake event is already set, so settling returns "
          "at once rather than making the queue wait a window's length",
          cancelled.settle() is True and cancelled.idle_s == 0.0,
          f"idle {cancelled.idle_s}")
    cancelled.resume()
    check("resume clears it, so one job's cancel cannot abort the next job's "
          "first rest", cancelled.wake.is_set() is False)

    # Real Event.wait, no fake anywhere, because the responsiveness claim is
    # about the actual sleep and not about the arithmetic around it.
    live = throttle.DutyCycle(0.5)
    token = live.begin()
    time.sleep(0.08)
    live.owe(token)
    live.interrupt()
    began = time.time()
    woken = live.settle()
    check("and on the real threading.Event the interrupted sleep returns in "
          "milliseconds, not when the timeout runs out",
          woken is True and time.time() - began < 0.05,
          f"{(time.time() - began) * 1000:.0f} ms of an 80 ms rest")


def test_nice() -> None:
    print("\nnice: what it does and what it does not")
    here = throttle.apply_nice(0)
    check("asking for 0 reports the current niceness and changes nothing",
          here["requested"] == 0 and here["nice"] == here["before"]
          and here["error"] is None, str(here))
    # In a subprocess, because os.nice is a one way door for an unprivileged
    # process: renicing the test runner would slow every suite after this one.
    done = subprocess.run(
        [str(PYTHON), "-c",
         "import sys; sys.path.insert(0, %r);\n"
         "import throttle, json;\n"
         "print(json.dumps([throttle.apply_nice(7), throttle.apply_nice(-5)]))"
         % str(SAM)],
        capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    check("a subprocess renices itself", done.returncode == 0,
          done.stderr.strip()[:200])
    if done.returncode == 0:
        import json

        up, down = json.loads(done.stdout.strip().splitlines()[-1])
        check("+7 is applied and reported", up["nice"] == 7 and up["before"] == 0
              and up["error"] is None, str(up))
        check("a negative increment needs privilege, and it is REPORTED rather "
              "than raised: a service refusing to start because it could not "
              "become more important would be a strange way to be polite",
              down["error"] is not None and down["nice"] == 7, str(down))


def test_preset() -> None:
    print("\nthe --quiet preset, and explicit flags beating it")
    check("the preset is defined in one place, so the flag, /health, the "
          "README and this test cannot disagree",
          throttle.QUIET == {"duty_cycle": 0.5, "nice": 15,
                             "mlx_memory_limit_mb": 6144,
                             "mask_width_hint": 720}, str(throttle.QUIET))

    plain = build_parser().parse_args(["--stub"])
    check("every flag the preset covers defaults to None, which is what makes "
          "'explicit wins' true without a precedence table",
          (plain.duty_cycle, plain.nice, plain.mask_width_hint,
           plain.mlx_memory_limit_mb) == (None, None, None, None))
    applied = throttle.resolve_quiet(plain)
    check("with no --quiet the floors are 1.0 and 0, and nothing was applied",
          applied == {} and plain.duty_cycle == 1.0 and plain.nice == 0
          and plain.mask_width_hint is None)

    quiet = build_parser().parse_args(["--stub", "--quiet"])
    applied = throttle.resolve_quiet(quiet)
    check("--quiet alone sets all four",
          applied == throttle.QUIET and quiet.duty_cycle == 0.5
          and quiet.nice == 15 and quiet.mlx_memory_limit_mb == 6144
          and quiet.mask_width_hint == 720, str(applied))

    mixed = build_parser().parse_args(
        ["--stub", "--quiet", "--duty-cycle", "0.2", "--nice", "3",
         "--mlx-memory-limit-mb", "8192", "--mask-width-hint", "1280"])
    applied = throttle.resolve_quiet(mixed)
    check("every explicit flag beats the preset, and the preset then reports "
          "having applied nothing",
          applied == {} and mixed.duty_cycle == 0.2 and mixed.nice == 3
          and mixed.mlx_memory_limit_mb == 8192
          and mixed.mask_width_hint == 1280, str(applied))

    bad = build_parser().parse_args(["--stub", "--duty-cycle", "1.4"])
    try:
        throttle.resolve_quiet(bad)
        check("a duty cycle out of range is refused at startup", False,
              "it was accepted")
    except ValueError as exc:
        check("a duty cycle out of range is refused at startup, before a model "
              "is loaded", True, str(exc)[:44])


def test_stub_really_sleeps() -> None:
    print("\nthe stub honours it for real, which is what makes this testable")
    import numpy as np

    def frames(count):
        for _ in range(count):
            yield np.zeros((16, 24, 3), dtype=np.uint8)

    backend = make_backend("stub", log=lambda _m: None, delay_ms=8.0,
                           chunk_frames=6, duty_cycle_fraction=0.5)
    backend.load()
    check("the duty cycle reaches the stub through make_backend, the same "
          "kwarg every backend takes",
          backend.throttle.fraction == 0.5 and backend.throttle.enabled)
    seen: list[int] = []
    began = time.time()
    backend.track(frames(18), 24.0, {"text": ["person"]}, "all",
                  lambda index, masks: seen.append(index))
    elapsed = time.time() - began
    stats = backend.throttle.stats()
    check("every frame was still emitted, so the sleep changed the timing and "
          "nothing else", seen == list(range(18)), f"{len(seen)} frames")
    check("it slept between windows: two of the three windows rested, the "
          "third is owed to the service",
          stats["rests"] == 2 and stats["owed"] is True, str(stats["rests"]))
    check("the measured busy fraction is near the asked-for 0.5",
          0.4 <= stats["busy_fraction"] <= 0.62, str(stats["busy_fraction"]))
    check("and the wall clock really did roughly double",
          elapsed >= stats["busy_s"] * 1.6, f"{elapsed:.3f}s wall, "
          f"{stats['busy_s']}s busy")

    off = make_backend("stub", log=lambda _m: None, delay_ms=8.0,
                       chunk_frames=6)
    off.load()
    seen = []
    began = time.time()
    off.track(frames(18), 24.0, {"text": ["person"]}, "all",
              lambda index, masks: seen.append(index))
    plain = time.time() - began
    check("with no duty cycle asked for it never sleeps and never rests",
          off.throttle.stats()["rests"] == 0 and off.throttle.enabled is False)
    check("so the same work is quicker than the throttled run",
          plain < elapsed, f"{plain:.3f}s against {elapsed:.3f}s")

    # A cancel during a rest has to come out of the backend as Cancelled, the
    # same exception a cancelled on_frame raises, or the service would report a
    # cancelled job as failed.
    late = make_backend("stub", log=lambda _m: None, delay_ms=8.0,
                        chunk_frames=6, duty_cycle_fraction=0.05)
    late.load()
    late.throttle.interrupt()
    try:
        late.track(frames(18), 24.0, {"text": ["person"]}, "all",
                   lambda index, masks: None)
        check("a woken rest raises Cancelled", False, "it returned normally")
    except Cancelled as exc:
        check("a woken rest raises Cancelled, so a cancel during a rest ends "
              "the job the same way a cancel between two frames does",
              True, str(exc))


# ---------------------------------------------------------------------------
# over real HTTP
# ---------------------------------------------------------------------------


def test_health_and_responsiveness() -> None:
    print("\nover real HTTP: /health, cancel and a queued job, during a rest")
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="sam-quiet-"))
    clip = make_frames(root / "clip", count=18, width=48, height=32)
    port = free_port()
    # 6 frames a window at 25 ms a frame is 0.15 s of work, and a duty cycle of
    # 0.1 turns that into 1.35 s of rest: long enough to catch in flight, short
    # enough that the whole suite stays quick.
    proc, base = start(port, root / "svc",
                       ["--stub-delay-ms", "25", "--duty-cycle", "0.1",
                        "--chunk-frames", "6", "--mask-width-hint", "32"])
    try:
        block = call(base, "/health")["throttle"]
        check("/health carries the setting", block["duty_cycle"] == 0.1
              and block["enabled"] is True, str(block.get("duty_cycle")))
        check("and the measured fraction, null until something has run",
              "busy_fraction" in block and block["busy_fraction"] is None)
        check("and what nice did, so a launcher's own log is not the only "
              "record of it", isinstance(block.get("nice"), dict)
              and block["nice"]["requested"] == 0, str(block.get("nice")))
        check("--quiet was not passed, and /health says so rather than leaving "
              "it to be guessed from the numbers",
              block["quiet"] is False and block["quiet_applied"] == {})

        first = call(base, "/track", {"video": str(clip), "fps": 24,
                                      "prompts": {"text": ["person"]},
                                      "out_dir": str(root / "m1")})
        check("a track wider than the mask width hint says so, as a warning "
              "on the job rather than a refusal: the service cannot choose the "
              "caller's width and is honest that a narrower one does not "
              "shrink the model",
              any("quiet mode suggests" in w for w in first["warnings"]),
              str(first["warnings"]))
        second = call(base, "/track", {"video": str(clip), "fps": 24,
                                       "prompts": {"text": ["cat"]},
                                       "out_dir": str(root / "m2")})

        # Watch until the first window's rest, sampling everything that a
        # person or a poller would look at while it sleeps.
        latency = []
        queued_positions = []
        resting_seen = None
        deadline = time.time() + 30
        while time.time() < deadline:
            began = time.time()
            health = call(base, "/health")
            latency.append(time.time() - began)
            block = health["throttle"]
            job = call(base, f"/jobs/{first['job_id']}")
            if block["resting"]:
                resting_seen = dict(block, job_done=job["done_frames"],
                                    job_state=job["state"])
                queued = call(base, f"/jobs/{second['job_id']}")
                queued_positions.append((queued["state"],
                                         queued["queue_position"]))
                break
            time.sleep(0.02)

        check("a rest is visible while it is happening",
              resting_seen is not None and resting_seen["rest_left_s"] > 0,
              str(resting_seen and resting_seen["rest_left_s"]))
        if resting_seen:
            check("with the window that earned it, and how long it means to "
                  "rest, on the record WHILE it rests rather than only "
                  "afterwards",
                  (resting_seen.get("last") or {}).get("unit") == "window"
                  and resting_seen["last"]["busy_s"] > 0
                  and resting_seen["last"]["asked_rest_s"] > 1.0,
                  str(resting_seen.get("last")))
            check("the running job keeps reporting its progress through the "
                  "rest, so a poller sees a slow job and not a dead one",
                  resting_seen["job_state"] == "running"
                  and resting_seen["job_done"] == 6,
                  f"{resting_seen['job_state']} {resting_seen['job_done']}")
            check("and the queued job behind it keeps reporting its place in "
                  "the queue", queued_positions == [("queued", 1)],
                  str(queued_positions))
        check("/health answered every time and never took longer than 250 ms, "
              "so the sleep is on the worker thread and holds no lock",
              latency and max(latency) < 0.25,
              f"worst {max(latency) * 1000:.0f} ms over {len(latency)} calls")

        # Cancel DURING the rest. The rest still had over a second left.
        began = time.time()
        call(base, f"/jobs/{first['job_id']}/cancel", {})
        job = wait_for_job(base, first["job_id"], timeout_s=10)
        took = time.time() - began
        check("a cancel arriving during a rest ends the job in well under the "
              "rest it interrupted, rather than being noticed when the sleep "
              "runs out", job["state"] == "cancelled" and took < 1.0,
              f"{job['state']} after {took * 1000:.0f} ms")
        check("its matte kept the frames it did write",
              job["done_frames"] == 6, str(job["done_frames"]))

        # Round 2 finding 57. `hundred_percent_running` was appended to only
        # inside the loop above, and that loop BREAKS on the first observed
        # rest, which is after window 1 of 3 at 6 frames of 18. The condition
        # it looked for (done_frames >= total_frames) can only happen at the
        # END of a job, which that loop never reaches: the first job is then
        # cancelled at 6 frames and the second was drained through
        # wait_for_job, which records nothing. So the list was empty by the
        # shape of the harness and not by the behaviour of owe/settle, and the
        # check could not fail.
        #
        # The second job IS sampled to the end now, and the claim is stated as
        # the thing that would actually be wrong: how long the job sits at
        # 100 percent of its frames before it is reported done. The last
        # window's rest is OWED to the service and taken after `_end_job`, so
        # that gap is a matte flush (milliseconds). If the rest were taken
        # inside `track()` instead, the gap would be the whole asked rest,
        # which this run measured above at over a second.
        first_full = None
        reported_done = None
        samples = 0
        deadline = time.time() + 60
        second_job = None
        while time.time() < deadline:
            job = call(base, f"/jobs/{second['job_id']}")
            samples += 1
            total = job.get("total_frames") or 0
            if total and job["done_frames"] >= total and first_full is None:
                first_full = time.time()
                first_full_state = job["state"]
            if job["state"] in ("done", "failed", "cancelled"):
                reported_done = time.time()
                second_job = job
                break
            time.sleep(0.02)
        check("the queue drains afterwards: the job behind the cancelled one "
              "ran to the end", second_job is not None
              and second_job["state"] == "done"
              and second_job["done_frames"] == 18,
              f"{second_job and second_job['state']} "
              f"{second_job and second_job['done_frames']} "
              f"over {samples} samples")
        check("and it really was sampled on the way, rather than read once "
              "after it had already finished", samples > 1, str(samples))
        check("a sample was taken with every frame written, which is the "
              "sample the claim below is about",
              first_full is not None, str(first_full))
        if first_full is not None and reported_done is not None:
            gap = reported_done - first_full
            asked = (resting_seen or {}).get("last", {}).get("asked_rest_s", 0)
            check("a job at 100 percent of its frames is reported done "
                  "immediately, not after the last window's rest: that rest "
                  "is owed to the service and taken afterwards, so a poller "
                  "never watches a finished job that will not finish",
                  gap < 0.5 and asked > 1.0,
                  f"{gap * 1000:.0f} ms at 100 percent, against an asked rest "
                  f"of {asked}s (state at that sample: {first_full_state})")

        block = call(base, "/health")["throttle"]
        check("the run's own numbers are on /health at the end: rests taken, "
              "busy and idle seconds, and the fraction actually achieved",
              block["rests"] >= 3 and block["busy_s"] > 0
              and block["idle_s"] > 0 and block["busy_fraction"] is not None,
              f"{block['rests']} rests, busy {block['busy_s']}s, "
              f"idle {block['idle_s']}s, fraction {block['busy_fraction']}")
        check("and the units are broken out, so a run's picks and its windows "
              "can be told apart", "window" in (block.get("units") or {}),
              str(block.get("units")))
        check("one rest was woken early by the cancel, and it is counted",
              block["woken_early"] >= 1, str(block["woken_early"]))
    finally:
        stop(proc)


def test_a_pick_after_a_cancel_still_rests() -> None:
    """Round 2 finding 62: after a cancel, picks stopped resting.

    `interrupt()` sets the wake event and the only `wake.clear()` in the tree
    is `resume()`, which `_run_job` called and `_run_pick` did not. So the
    event stayed set from a cancel until the next JOB started, and every pick
    served in between rested on an already-set event: the wait returned at
    once, `woken_early` went up and the recorded rest was 0.

    In plain words, somebody who cancels a track and then keeps clicking the
    picture to pick objects gets no throttling at all on those clicks, at the
    exact moment they are most likely to be doing something else on the
    machine. Which is the whole point of quiet mode.

    Measured through /health rather than by timing the call, because the rest
    is taken AFTER the answer is on its way: the pick itself is fast either
    way, and what moved is the gap the machine gets afterwards.
    """
    print("\nover real HTTP: a pick after a cancel still rests (finding 62)")
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="sam-quiet-cancel-"))
    clip = make_frames(root / "clip", count=18, width=48, height=32)
    port = free_port()
    # Duty cycle 0.1: a rest of nine times the work, so one 25 ms pick asks
    # for about 0.2 s and the difference between resting and not resting is
    # far outside any timing noise.
    proc, base = start(port, root / "svc",
                       ["--stub-delay-ms", "25", "--duty-cycle", "0.1",
                        "--chunk-frames", "6", "--mask-width-hint", "32"])
    try:
        job = call(base, "/track", {"video": str(clip), "fps": 24,
                                    "prompts": {"text": ["person"]},
                                    "out_dir": str(root / "m1")})
        # Cancel it while it is RUNNING, which is the only cancel that
        # interrupts a rest, and wait until the queue is empty again so the
        # pick below is served with no job in flight.
        deadline = time.time() + 30
        state = ""
        while time.time() < deadline:
            state = call(base, f"/jobs/{job['job_id']}")["state"]
            if state == "running":
                break
            time.sleep(0.02)
        check("the track reached running, so its cancel is the kind that "
              "interrupts a rest", state == "running", state)
        call(base, f"/jobs/{job['job_id']}/cancel", {})
        ended = wait_for_job(base, job["job_id"], timeout_s=20)
        check("and it ended as cancelled", ended["state"] == "cancelled",
              ended["state"])

        before = call(base, "/health")["throttle"]
        picks_before = (before.get("units") or {}).get("pick") or {"count": 0,
                                                                   "idle_s": 0.0}
        woken_before = before["woken_early"]

        call(base, "/segment", {"image": str(clip / "0000.png"),
                                "prompts": {"text": ["person"]}})

        # The rest is taken after the answer, so poll until the pick has been
        # accounted rather than reading /health once and racing it.
        picks = picks_before
        last = None
        woken_after = woken_before
        deadline = time.time() + 30
        while time.time() < deadline:
            block = call(base, "/health")["throttle"]
            picks = (block.get("units") or {}).get("pick") or {"count": 0,
                                                               "idle_s": 0.0}
            if picks["count"] > picks_before["count"]:
                woken_after = block["woken_early"]
                last = block.get("last")
                break
            time.sleep(0.02)

        check("the pick was accounted as a pick, not as a window",
              picks["count"] == picks_before["count"] + 1,
              f"{picks_before['count']} -> {picks['count']}")
        rested = picks["idle_s"] - picks_before["idle_s"]
        check("and it really rested afterwards, rather than the cancel's own "
              "wake event making its rest return instantly",
              rested > 0.01, f"{rested:.3f}s of rest after the pick")
        # The sharp version of the same claim: the pick's own record says it
        # slept nearly all of what it asked for and was not woken. A stub pick
        # is a few milliseconds of work, so the asked rest is small; what
        # matters is that it was taken rather than skipped.
        check("the pick's own record says it slept what it asked for",
              isinstance(last, dict) and last.get("unit") == "pick"
              and last.get("woken") is False
              and last.get("rest_s", 0) >= 0.8 * last.get("asked_rest_s", 0)
              and last.get("asked_rest_s", 0) > 0.0, str(last))
        check("so nothing was counted as woken early by a cancel that had "
              "already been served", woken_after == woken_before,
              f"{woken_before} -> {woken_after}")
    finally:
        stop(proc)


def test_quiet_flag_over_http() -> None:
    print("\nover real HTTP: one --quiet flag, and a refused duty cycle")
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="sam-quiet-preset-"))
    port = free_port()
    proc, base = start(port, root / "svc", ["--quiet", "--stub-delay-ms", "1",
                                            "--chunk-frames", "6"])
    try:
        block = call(base, "/health")["throttle"]
        check("--quiet reaches the running service as a duty cycle of 0.5",
              block["duty_cycle"] == 0.5 and block["quiet"] is True,
              str(block["duty_cycle"]))
        check("and it says which values it filled in, so a run can be checked "
              "against what was asked for rather than inferred from how it "
              "behaved",
              block["quiet_applied"] == throttle.QUIET,
              str(block["quiet_applied"]))
        check("nice 15 was applied to the service process",
              block["nice"]["nice"] == 15 and block["nice"]["error"] is None,
              str(block["nice"]))
        check("and the mask width guidance is advertised for a caller to read",
              block["mask_width_hint"] == 720, str(block["mask_width_hint"]))
        check("the backend really carries it, not just the flag record",
              call(base, "/health")["throttle"]["enabled"] is True)
    finally:
        stop(proc)

    done = subprocess.run(
        [str(PYTHON), str(SAM / "server.py"), "--stub", "--no-model-lock",
         "--duty-cycle", "0", "--port", str(free_port())],
        capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    check("a duty cycle of 0 refuses to start, with a sentence, rather than "
          "starting a service that sleeps forever after its first window",
          done.returncode != 0 and "duty cycle must be" in
          (done.stdout + done.stderr), (done.stdout + done.stderr)[-160:])


def main() -> int:
    test_parsing()
    test_arithmetic()
    test_reading_it_while_it_rests()
    test_owe_and_settle()
    test_nice()
    test_preset()
    test_stub_really_sleeps()
    test_health_and_responsiveness()
    test_a_pick_after_a_cancel_still_rests()
    test_quiet_flag_over_http()
    return report("test_quiet")


if __name__ == "__main__":
    raise SystemExit(main())
