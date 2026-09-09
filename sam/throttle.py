"""Quiet mode: how much of the wall clock the model is allowed to own.

The founder's order was one sentence: "Lets not run sam3.1 in a way that slows
down my computer." Two lanes before this one made a window FIT (about 4.2 GB
sustained, a 6.3 GB high water mark instead of 13.8 GB, see
`plan/2026-09-08-studio-masks/checkpoints/FIX-MEMORY.md` and `FIX-MEMORY-2.md`).
Fitting is not the same as being polite: a tracking run that fits still holds
the GPU continuously for minutes at a time, and on a single GPU Mac that is
felt as a stuttering scroll and a slow window redraw in whatever the founder is
actually doing.

So this module is not about memory at all. It is about **giving the machine
back**, in three independent ways, each measured rather than asserted:

* **duty cycle** (this file's `DutyCycle`): after a unit of model work that
  took `busy` seconds, sleep `busy * (1/F - 1)` seconds, so that over a run the
  model owns about `F` of the wall clock and the rest of the machine owns
  `1 - F`. At `F = 0.5` a 150 s window is followed by a 150 s rest: the track
  takes twice as long and the GPU is free half the time. This is the only lever
  here that a person can feel immediately.
* **nice** (`apply_nice`): the CPU scheduling priority of the process. It costs
  nothing and helps the CPU side (frame decode, PNG writes, the HTTP server),
  but note what it does NOT do: `nice` is not a GPU priority on macOS, so it
  cannot make Metal work yield. That is why the duty cycle exists.
* **the quiet preset** (`resolve_quiet`): one flag that sets sensible values for
  all of the above plus an MLX memory limit, because three flags nobody
  remembers is the same as no flags.

Everything is off by default: `DutyCycle(1.0)` never sleeps, and its `rest()`
returns without touching the clock beyond one reading. A service that does not
ask for quiet mode behaves exactly as it did.

**Responsiveness is the hard part, and it is why the sleep is an Event.**
The rest happens on the worker thread, which is also the thread that would run
the next window. A plain `time.sleep(150)` would mean a cancel arriving one
second into the rest waits 149 seconds to be noticed, and that reads as a hung
service. So the rest waits on `DutyCycle.wake`, a `threading.Event` the service
sets when the running job is cancelled or the service is stopping: the wait
returns the moment it is set. `/health` and `/jobs/<id>` are served on other
threads and take no lock the worker holds, so they answer during a rest exactly
as fast as they do during a frame.
"""

from __future__ import annotations

import os
import threading
import time

# A duty cycle of 1 is "no throttling at all", and it is the default
# everywhere: quiet mode is something a launcher asks for, never something
# that happens to a caller who did not ask.
DEFAULT_DUTY_CYCLE = 1.0

# What --quiet means, in one place, so the flag, /health, the tests and the
# README cannot disagree about it.
QUIET_DUTY_CYCLE = 0.5
QUIET_NICE = 15
# 6144 MB against the measured numbers: a window's sustained live set is about
# 4.2 GB and its high water mark about 6.3 GB after the attention fix, so 6 GB
# is a guard rail that sits just above normal work rather than a cap that
# strangles it. MLX treats the memory limit as the level at which it reclaims
# from its own cache before allocating, not as a hard failure, so a window that
# genuinely needs more still runs; it just runs having given the cache back
# first. Below about 5 GB it would be reclaiming on every frame for nothing.
QUIET_MLX_MEMORY_LIMIT_MB = 6144
# Guidance, not enforcement: the width the studio decodes its mask proxy at.
# Be honest about what this buys (FIX-MEMORY-2 section 4.3): the model resizes
# every frame to 1008x1008 before the trunk sees it, so a narrower proxy does
# NOT make the model's own memory smaller. What it does make smaller is
# everything around the model: the decoded frames, the numpy buffers and the
# PNG mattes, which is why the process footprint moved with the width
# (11741 MB at 720 against 13798 MB at 1280) while MLX's own peak did not move
# at all. So it is worth asking for on a shared machine, and it is not a fix.
QUIET_MASK_WIDTH = 720

# Below this a sleep is a rounding error: the wait itself costs more than the
# rest is worth, and a log line per frame-length rest is noise.
MIN_REST_S = 0.002
# One rest is never longer than this, however long the window was. A window
# that took 20 minutes on a swapping machine should not park the queue for
# another 20; the measured busy fraction then reads above the asked-for duty
# cycle, and that honest number is on /health rather than being hidden by
# sleeping forever.
MAX_REST_S = 300.0

__all__ = ["DEFAULT_DUTY_CYCLE", "DutyCycle", "MAX_REST_S", "MIN_REST_S",
           "QUIET", "QUIET_DUTY_CYCLE", "QUIET_MASK_WIDTH",
           "QUIET_MLX_MEMORY_LIMIT_MB", "QUIET_NICE", "apply_nice",
           "parse_duty_cycle", "resolve_quiet"]


def parse_duty_cycle(value) -> float:
    """`0 < F <= 1`, or a ValueError that says what to type instead.

    0 is refused rather than treated as "never run": a duty cycle of 0 means a
    rest of infinite length after the first window, which is a hung service
    with extra steps. Somebody who wants that wants to not start the job.
    """
    try:
        fraction = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"duty cycle must be a number between 0 and 1, "
                         f"not {value!r}") from None
    if fraction != fraction or fraction <= 0.0 or fraction > 1.0:
        raise ValueError(
            f"duty cycle must be greater than 0 and at most 1, not {value!r}. "
            f"1 is no throttling (the model owns the machine while it runs), "
            f"0.5 is half the wall clock, 0.25 is a quarter.")
    return fraction


class DutyCycle:
    """The busy fraction of wall time the model is allowed, and the record of
    what it actually got.

    Used as: `token = throttle.begin()`, do the work, `throttle.rest(token)`.
    Two calls rather than a context manager on purpose, because in both real
    backends the work ends inside a `finally` that also frees the window's
    memory, and the rest has to happen AFTER that free (resting while still
    holding a window's 4 GB would give the machine back the GPU and not the
    memory, which is half a fix).

    `clock` and `waiter` are injectable so the arithmetic can be tested
    exactly, with no wall time at all; nothing in the service passes them.
    """

    def __init__(self, fraction=DEFAULT_DUTY_CYCLE, log=None,
                 max_rest_s: float = MAX_REST_S, clock=time.monotonic,
                 waiter=None):
        self.fraction = parse_duty_cycle(fraction)
        self.max_rest_s = max(0.0, float(max_rest_s))
        self._log = log
        self._clock = clock
        # The service sets this to stop a rest early. Anything waiting on it
        # is asking "should I still be here?", and the answer arrives at once.
        self.wake = threading.Event()
        self._waiter = waiter or self.wake.wait
        self.busy_s = 0.0
        self.idle_s = 0.0
        self.rests = 0
        self.woken_early = 0
        self.units: dict[str, dict] = {}
        self.last: dict | None = None
        self._rest_until: float | None = None
        self._rest_began: float | None = None
        self._owed: tuple[float, str] | None = None

    def __repr__(self) -> str:
        return f"DutyCycle({self.fraction:g})"

    @property
    def enabled(self) -> bool:
        """A duty cycle of 1 is not throttling, so it never sleeps and never
        logs. This is the branch that keeps quiet mode free when it is off."""
        return self.fraction < 1.0

    def idle_now(self) -> float:
        """Idle seconds including the rest currently in flight.

        `self.idle_s` only grows when a rest FINISHES, and reading that number
        during a rest was actively misleading: three seconds into a hundred
        second sleep the busy fraction read 1.00, which is the opposite of what
        was happening. So anything a person or a poller looks at counts the
        part of the current rest that has already elapsed.

        Read into a local first (round 2 finding 78). The worker thread clears
        `_rest_began` in `rest()`'s `finally` the instant its sleep ends, so
        testing the attribute and then subtracting the attribute are two reads
        of a field another thread owns: a rest that finished in between turned
        this into `self._clock() - None`, a TypeError on a /health served at
        exactly the wrong moment.
        """
        began = self._rest_began
        if began is None:
            return self.idle_s
        return self.idle_s + max(0.0, self._clock() - began)

    @property
    def busy_fraction(self) -> float | None:
        """The MEASURED fraction, which is the point: `duty_cycle` is what was
        asked for and this is what happened. They differ when a rest was cut
        short by a cancel, when a window ran longer than `max_rest_s` allows a
        rest to be, or when nothing has run yet (None)."""
        idle = self.idle_now()
        total = self.busy_s + idle
        return round(self.busy_s / total, 4) if total > 0 else None

    # -- one unit of work --------------------------------------------------

    def begin(self) -> float:
        """Mark the start of a busy stretch. Cheap enough to call per frame."""
        return self._clock()

    def rest_for(self, busy_s: float) -> float:
        """How long to sleep after `busy_s` of work, in seconds.

        `busy / (busy + idle) = F`, solved for idle, is `busy * (1/F - 1)`:
        at F=0.5 rest as long as you worked, at F=0.25 rest three times as
        long, at F=1 do not rest.
        """
        if not self.enabled or busy_s <= 0:
            return 0.0
        return min(self.max_rest_s, busy_s * (1.0 / self.fraction - 1.0))

    def rest(self, token, unit: str = "window") -> bool:
        """Account `token`..now as busy, then sleep for the matching idle time.

        Returns True when the sleep was cut short because `wake` was set, which
        the caller should read as "stop, do not start another window". Always
        accounts the busy time, even when it does not sleep, so the measured
        busy fraction is true whether quiet mode is on or off.
        """
        busy = max(0.0, self._clock() - float(token))
        self.busy_s += busy
        want = self.rest_for(busy)
        slept = 0.0
        woken = False
        # Published BEFORE the sleep, not after it, and then filled in. A
        # record written only at the end meant that during a rest /health said
        # nothing about the window that earned it, which is the one thing
        # somebody watching a stalled-looking service wants to see.
        record = {"unit": unit, "busy_s": round(busy, 3), "rest_s": 0.0,
                  "asked_rest_s": round(want, 3), "woken": False}
        self.last = record
        if want >= MIN_REST_S:
            self.rests += 1
            began = self._clock()
            self._rest_until = began + want
            self._rest_began = began
            try:
                woken = bool(self._waiter(want))
            finally:
                slept = max(0.0, self._clock() - began)
                self._rest_until = None
                self._rest_began = None
            self.idle_s += slept
            if woken:
                self.woken_early += 1
        record["rest_s"] = round(slept, 3)
        record["woken"] = woken
        aggregate = self.units.setdefault(
            unit, {"count": 0, "busy_s": 0.0, "idle_s": 0.0})
        aggregate["count"] += 1
        aggregate["busy_s"] = round(aggregate["busy_s"] + busy, 3)
        aggregate["idle_s"] = round(aggregate["idle_s"] + slept, 3)
        if self._log is not None and want >= MIN_REST_S:
            measured = self.busy_fraction
            self._log(
                f"[quiet] {unit} was busy {busy:.1f}s; resting {slept:.1f}s of "
                f"{want:.1f}s at duty cycle {self.fraction:g}"
                + (" (woken early)" if woken else "")
                + (f"; busy fraction so far {measured:.2f}"
                   if measured is not None else ""))
        return woken

    def owe(self, token, unit: str = "window") -> None:
        """Remember a busy stretch to rest for LATER, once somebody else has
        reported the work finished.

        Two callers, one reason each. A backend owes each window's rest so the
        NEXT window can take it after the previous window's memory is freed,
        not while it is still held. And the LAST window's rest is owed to the
        service: resting inside `track()` would leave the job at
        `done_frames == total_frames` in state `running` for a whole window,
        which reads to a poller as a finished job that will not finish. So the
        service reports the job, and only then does the worker take the rest
        before pulling the next item off the queue. The machine gets the same
        gap either way; the queue reports the truth while it waits.
        """
        self._owed = (float(token), str(unit))

    @property
    def owed(self) -> bool:
        return self._owed is not None

    def settle(self, unit: str | None = None) -> bool:
        """Take the rest `owe()` remembered, if any. Returns as `rest()` does.

        After a cancel this returns at once rather than resting, because the
        wake event is already set: somebody asking for a job to stop should
        not then wait a window's length for the queue to move.
        """
        owed, self._owed = self._owed, None
        if owed is None:
            return False
        token, owed_unit = owed
        return self.rest(token, unit=unit or owed_unit)

    # -- the service's side ------------------------------------------------

    def interrupt(self) -> None:
        """Cut any rest short, now. Called when the running job is cancelled
        or the service is stopping."""
        self.wake.set()

    def resume(self) -> None:
        """Forget a previous interrupt, so one job's cancel cannot stop the
        next job's first rest. Called when a job starts."""
        self.wake.clear()

    @property
    def resting(self) -> bool:
        return self._rest_until is not None

    def stats(self) -> dict:
        """What /health reports. `duty_cycle` is the setting, `busy_fraction`
        is the measurement, and `resting` plus `rest_left_s` are what make a
        deliberately idle service readable as idle rather than as stuck.

        `until` is snapshotted for the same reason `idle_now` snapshots
        `_rest_began` (round 2 finding 78): the worker thread can clear it
        between the None test and the subtraction, and this block also has to
        report `resting` from the SAME read, or /health could say "resting:
        false, rest_left_s: 4.0" from two reads either side of the clear.
        """
        until = self._rest_until
        left = None
        if until is not None:
            left = round(max(0.0, until - self._clock()), 2)
        return {
            "duty_cycle": self.fraction,
            "enabled": self.enabled,
            "busy_s": round(self.busy_s, 2),
            "idle_s": round(self.idle_now(), 2),
            "busy_fraction": self.busy_fraction,
            "rests": self.rests,
            "woken_early": self.woken_early,
            "resting": until is not None,
            "rest_left_s": left,
            "owed": self.owed,
            "max_rest_s": self.max_rest_s,
            "last": dict(self.last) if self.last else None,
            "units": {name: dict(value) for name, value in self.units.items()},
        }


# ---------------------------------------------------------------------------
# nice
# ---------------------------------------------------------------------------


def apply_nice(increment, log=None) -> dict:
    """`os.nice(increment)` at startup, and the honest report of what happened.

    What it buys and what it does not, because this is easy to over-claim:
    `nice` is a CPU scheduling hint. It helps the parts of this service that
    are ordinary CPU work (ffmpeg decode, the PNG writes, the HTTP server) get
    out of the founder's way, and it does nothing at all to Metal work, which
    is where the model's time goes. It is here because it is free, not because
    it is the fix; the duty cycle is the fix.

    Lowering niceness (a negative increment) needs privilege and fails with a
    PermissionError, which is reported rather than raised: a service that
    refuses to start because it could not become more important would be a
    strange way to be polite.
    """
    out: dict = {"requested": int(increment), "nice": None, "error": None}
    try:
        out["before"] = os.nice(0)
    except OSError as exc:                                     # noqa: BLE001
        out["before"] = None
        out["error"] = f"{type(exc).__name__}: {exc}"
    if not int(increment):
        out["nice"] = out["before"]
        return out
    try:
        out["nice"] = os.nice(int(increment))
    except OSError as exc:
        out["nice"] = out["before"]
        out["error"] = f"{type(exc).__name__}: {exc}"
        if log is not None:
            log(f"[quiet] could not renice by {increment}: {out['error']}. "
                f"Running at nice {out['before']}.")
        return out
    if log is not None:
        log(f"[quiet] nice {out['before']} -> {out['nice']} "
            f"(CPU priority only: Metal work is not niced, which is what the "
            f"duty cycle is for)")
    return out


# ---------------------------------------------------------------------------
# the preset
# ---------------------------------------------------------------------------


#: What one `--quiet` means. Read by `resolve_quiet`, by /health and by the
#: tests, so there is exactly one definition of the preset.
QUIET = {
    "duty_cycle": QUIET_DUTY_CYCLE,
    "nice": QUIET_NICE,
    "mlx_memory_limit_mb": QUIET_MLX_MEMORY_LIMIT_MB,
    "mask_width_hint": QUIET_MASK_WIDTH,
}


def resolve_quiet(args) -> dict:
    """Fill in the quiet preset wherever the caller said nothing, and say what
    it filled in.

    Every flag the preset covers defaults to None in the parser rather than to
    its own value, which is what makes "explicit flags override --quiet" true
    without a precedence table: a value that is not None was typed by a person
    and is left alone. The returned dict is what /health shows as
    `throttle.quiet_applied`, so a run's own log can be checked against what
    was asked for instead of inferred from its behaviour.
    """
    quiet = bool(getattr(args, "quiet", False))
    applied: dict = {}
    if quiet:
        for name, value in QUIET.items():
            if getattr(args, name, None) is None:
                setattr(args, name, value)
                applied[name] = value
    # The floors, applied whether or not --quiet was given, so the rest of the
    # service can read plain numbers instead of "None means the default".
    if getattr(args, "duty_cycle", None) is None:
        args.duty_cycle = DEFAULT_DUTY_CYCLE
    else:
        args.duty_cycle = parse_duty_cycle(args.duty_cycle)
    if getattr(args, "nice", None) is None:
        args.nice = 0
    return applied
