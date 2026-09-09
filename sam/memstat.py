"""What this process costs the machine, in the Mac's own numbers.

`ps -o rss` is the wrong number for this service and reading it was the reason
a 13 GB process looked like a 150 MB one for a whole afternoon. MLX allocates
through Metal, and a Metal buffer is an `IOAccelerator` mapping, not an
anonymous page: it does not appear in RSS at all. What it does appear in is
the process's **physical footprint**, the number Activity Monitor calls Memory
and `top -stats mem` prints, and that is what this module reads.

Measured on this Mac (M5, 16 GB) while the service tracked a clip:

    ps rss                 155 MB
    top MEM (footprint)     13 GB
    vmmap IOAccelerator     13.8 GB in 1209 regions

Two readers, both cheap enough to call per frame (a syscall each, no
subprocess, unlike the `ps` call this replaces):

* `mach_task_self()` + `task_info(TASK_VM_INFO)` gives footprint, compressed
  and resident for THIS process. That is the whole picture and it is what the
  service uses.
* `proc_pid_rusage(pid, RUSAGE_INFO_V0)` gives footprint for ANY pid without
  an entitlement, so a verification script can watch a service it started.

Everything degrades to `None` rather than raising: a memory reading is never
worth taking a service down for.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import resource
import sys
import time

MB = 1048576.0

_libc = None
_TASK_VM_INFO = 22


def _lib():
    global _libc
    if _libc is None:
        try:
            _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib",
                                use_errno=True)
            _libc.mach_task_self.restype = ctypes.c_uint
        except Exception:                                      # noqa: BLE001
            _libc = False
    return _libc or None


class _TaskVMInfo(ctypes.Structure):
    """`struct task_vm_info` from <mach/task_info.h>, up to `max_address`.

    Only the prefix is declared: `task_info` fills as many fields as the
    count says and the ones after `max_address` are revision dependent.
    """

    _size = ctypes.c_uint64
    _fields_ = [
        ("virtual_size", _size),
        ("region_count", ctypes.c_int32),
        ("page_size", ctypes.c_int32),
        ("resident_size", _size),
        ("resident_size_peak", _size),
        ("device", _size),
        ("device_peak", _size),
        ("internal", _size),
        ("internal_peak", _size),
        ("external", _size),
        ("external_peak", _size),
        ("reusable", _size),
        ("reusable_peak", _size),
        ("purgeable_volatile_pmap", _size),
        ("purgeable_volatile_resident", _size),
        ("purgeable_volatile_virtual", _size),
        ("compressed", _size),
        ("compressed_peak", _size),
        ("compressed_lifetime", _size),
        ("phys_footprint", _size),
        ("min_address", _size),
        ("max_address", _size),
    ]


class _RusageInfoV0(ctypes.Structure):
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
    ]


def task_vm() -> dict | None:
    """footprint, compressed, internal and resident bytes for THIS process."""
    lib = _lib()
    if lib is None or sys.platform != "darwin":
        return None
    info = _TaskVMInfo()
    count = ctypes.c_uint(ctypes.sizeof(_TaskVMInfo) // ctypes.sizeof(ctypes.c_uint32))
    try:
        rc = lib.task_info(lib.mach_task_self(), ctypes.c_uint(_TASK_VM_INFO),
                           ctypes.byref(info), ctypes.byref(count))
    except Exception:                                          # noqa: BLE001
        return None
    if rc != 0 or info.page_size <= 0 or info.phys_footprint == 0:
        # A wrong struct layout shows up here as a nonsense page size, and a
        # wrong reading is worse than no reading: memory numbers are what
        # someone decides to restart the machine on.
        return None
    return {"footprint": int(info.phys_footprint),
            "compressed": int(info.compressed),
            "internal": int(info.internal),
            "resident": int(info.resident_size),
            "device": int(info.device)}


def pid_footprint(pid: int) -> int | None:
    """Physical footprint of ANY pid, in bytes. For a watcher process."""
    lib = _lib()
    if lib is None or sys.platform != "darwin":
        return None
    buf = _RusageInfoV0()
    try:
        rc = lib.proc_pid_rusage(ctypes.c_int(int(pid)), ctypes.c_int(0),
                                 ctypes.byref(ctypes.cast(
                                     ctypes.byref(buf),
                                     ctypes.POINTER(ctypes.c_void_p)).contents))
    except Exception:                                          # noqa: BLE001
        return None
    return int(buf.ri_phys_footprint) if rc == 0 else None


def peak_rss_bytes() -> int:
    """ru_maxrss, in bytes. macOS reports bytes, Linux kilobytes; the wrong
    divisor turns 44 MB into 43 GB and reads as a leak, so it is explicit."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak) if sys.platform == "darwin" else int(peak) * 1024


def mlx_memory(mx) -> dict:
    """MLX's own view: what is live, what its allocator is hoarding, the peak.

    `active` is memory held by live arrays, `cache` is freed buffers the
    allocator kept for reuse (the thing that grows to fill the machine when
    its limit is left at the default), `peak` is the high water mark of
    active memory since the last reset.
    """
    if mx is None:
        return {}
    out = {}
    for key, name in (("active_mb", "get_active_memory"),
                      ("cache_mb", "get_cache_memory"),
                      ("peak_mb", "get_peak_memory")):
        fn = getattr(mx, name, None)
        if callable(fn):
            try:
                out[key] = round(fn() / MB, 1)
            except Exception:                                  # noqa: BLE001
                pass
    return out


def snapshot(mx=None) -> dict:
    """One reading, cheap enough for a per frame sample."""
    vm = task_vm()
    out = {
        "footprint_mb": round(vm["footprint"] / MB, 1) if vm else None,
        "compressed_mb": round(vm["compressed"] / MB, 1) if vm else None,
        "rss_mb": round(vm["resident"] / MB, 1) if vm else None,
        "peak_rss_mb": round(peak_rss_bytes() / MB, 1),
    }
    if vm is None:
        own = pid_footprint(os.getpid())
        out["footprint_mb"] = round(own / MB, 1) if own else None
    if mx is not None:
        out["mlx"] = mlx_memory(mx)
    return out


class WindowMeter:
    """The peak footprint of one window of a track, sampled as it runs.

    A window is where this service's memory is won or lost: MLX's allocator
    grows inside one and is emptied between two. Sampling only at the seam
    would therefore miss the whole problem, so `sample()` is called once per
    frame (two syscalls, microseconds) and the window reports the maximum it
    saw, not the value at the end.

    `stats()` is what `/health` shows and what the per window log line says:
    how many windows have run, the last one, and the worst one.
    """

    def __init__(self, mx=None, log=None, keep: int = 40):
        self.mx = mx
        self._log = log
        self.keep = int(keep)
        self.windows: list[dict] = []
        self.peak: dict | None = None
        self.count = 0
        self._current: dict | None = None

    # -- one window --------------------------------------------------------

    def start(self, **label) -> None:
        now = snapshot(self.mx)
        self._current = dict(label)
        self._current.update({
            "started_at": time.time(),
            "footprint_start_mb": now["footprint_mb"],
            "peak_footprint_mb": now["footprint_mb"],
            "compressed_mb": now["compressed_mb"],
            "samples": 1,
        })

    def sample(self) -> None:
        current = self._current
        if current is None:
            return
        now = snapshot(self.mx)
        current["samples"] += 1
        for key, value in (("peak_footprint_mb", now["footprint_mb"]),
                           ("compressed_mb", now["compressed_mb"])):
            if value is not None and (current.get(key) is None or value > current[key]):
                current[key] = value

    def finish(self, freed=None, **extra) -> dict | None:
        """Close the window. `freed` is a callable that drops the window's
        state; it runs between the "before" and "after" readings so the log
        line shows what the free actually bought."""
        current = self._current
        self._current = None
        if current is None:
            return None
        before = snapshot(self.mx)
        current["footprint_before_mb"] = before["footprint_mb"]
        if before["footprint_mb"] is not None and (
                current.get("peak_footprint_mb") is None
                or before["footprint_mb"] > current["peak_footprint_mb"]):
            current["peak_footprint_mb"] = before["footprint_mb"]
        if before.get("mlx"):
            current["mlx_before"] = before["mlx"]
        if freed is not None:
            try:
                freed()
            except Exception as exc:                           # noqa: BLE001
                # Said out loud, not swallowed. This callable is the one that
                # drops a window's session state, and the failure mode it
                # guards against (mlx-cv moving what SAM3VideoSessionState
                # holds) produced no exception, no log line and slowly growing
                # memory: the hardest kind of regression to find from a
                # checkpoint that says to look here first (round 1 finding 28).
                current["freed_error"] = f"{type(exc).__name__}: {exc}"
                if self._log:
                    self._log(f"[memory] freeing window "
                              f"{current.get('start')}..{current.get('end')} "
                              f"raised {type(exc).__name__}: {exc}. The window's "
                              f"state is still held; sam/backends/mlx_backend.py "
                              f"_drop_state is the first place to look.")
        after = snapshot(self.mx)
        current.update({
            "footprint_after_mb": after["footprint_mb"],
            "rss_after_mb": after["rss_mb"],
            "elapsed_s": round(time.time() - current.pop("started_at"), 2),
        })
        if after.get("mlx"):
            current["mlx_after"] = after["mlx"]
        current.update(extra)
        self.count += 1
        current["window_index"] = self.count
        self.windows.append(current)
        del self.windows[:-self.keep]
        peak = current.get("peak_footprint_mb")
        if peak is not None and (self.peak is None
                                 or peak > (self.peak.get("peak_footprint_mb") or 0)):
            self.peak = dict(current)
        if self._log:
            self._log(self._line(current))
        return current

    @staticmethod
    def _line(window: dict) -> str:
        def mb(value):
            return "?" if value is None else f"{value:.0f} MB"

        mlx_before = window.get("mlx_before") or {}
        mlx_after = window.get("mlx_after") or {}
        return ("[memory] window {index} frames {start}..{end} in {elapsed}s: "
                "peak footprint {peak}, {before} before the free, {after} after"
                "{mlx}").format(
            index=window.get("window_index"),
            start=window.get("start"), end=window.get("end"),
            elapsed=window.get("elapsed_s"),
            peak=mb(window.get("peak_footprint_mb")),
            before=mb(window.get("footprint_before_mb")),
            after=mb(window.get("footprint_after_mb")),
            mlx=(f"; mlx active {mlx_before.get('active_mb')} MB "
                 f"cache {mlx_before.get('cache_mb')} MB "
                 f"peak {mlx_before.get('peak_mb')} MB, "
                 f"cache {mlx_after.get('cache_mb')} MB after"
                 if mlx_before else ""))

    # -- what /health shows ------------------------------------------------

    def stats(self) -> dict:
        return {
            "count": self.count,
            "last": self.windows[-1] if self.windows else None,
            "peak": self.peak,
            "current": dict(self._current) if self._current else None,
        }


class StageMeter:
    """Where one window's memory actually goes, stage by stage.

    `WindowMeter` above answers "is memory bounded across windows", and it
    said yes while the process high water mark still read 13.7 GB. That is
    the question this class answers instead: **inside** one window, which
    step allocates the transient. A per frame sample cannot see it, because
    the spike happens between two samples and is gone before the next one.

    Two readings per stage, and they measure different things on purpose:

    * `mx.reset_peak_memory()` at the start and `mx.get_peak_memory()` at the
      end give MLX's own high water mark of live buffer bytes for that stage.
      That is the number the GPU allocator saw, and it is an UPPER bound: a
      Metal buffer counts the moment MLX hands it out. `reset_peak_memory()`
      sets the counter to zero rather than to what is already live, so a stage
      that allocates nothing at all through MLX reads 0 (not the resident
      model), while a stage that allocates anything reads the whole live set
      including the weights. A 0 in that column means "made no MLX
      allocation", never "used no memory".
    * a background thread reading `task_vm()` gives the physical footprint
      the kernel actually charged the process, sampled fast enough (every few
      milliseconds) to catch a transient a per frame sample misses. That is
      the number that decides whether the Mac swaps.

    Disabled by default and cheap when disabled: one boolean test per stage,
    no thread, no syscall. The service leaves it off; the spike turns it on.
    Stages are flat, never nested, because resetting MLX's peak inside
    another stage would erase the outer one's reading.
    """

    def __init__(self, mx=None, enabled: bool = False, sample_ms: float = 3.0,
                 log=None):
        self.mx = mx
        self.enabled = bool(enabled)
        self.sample_ms = max(0.5, float(sample_ms))
        self._log = log
        self.stages: dict[str, dict] = {}
        self.order: list[str] = []
        self._thread = None
        self._stop = None
        self._lock = None
        self._fp_max = 0.0
        self._open: str | None = None

    # -- the sampler -------------------------------------------------------

    def start(self) -> None:
        """Begin sampling the footprint in the background. Idempotent."""
        if not self.enabled or self._thread is not None:
            return
        import threading

        self._lock = threading.Lock()
        self._stop = threading.Event()

        def run():
            interval = self.sample_ms / 1000.0
            while not self._stop.wait(interval):
                vm = task_vm()
                if not vm:
                    continue
                value = vm["footprint"] / MB
                with self._lock:
                    if value > self._fp_max:
                        self._fp_max = value

        # A daemon thread so a crash in the measured code can never leave the
        # process alive waiting for the meter. It only reads a syscall; no MLX
        # call is made off the main thread.
        self._thread = threading.Thread(target=run, name="stage-meter",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def _take_footprint_max(self, seed: float) -> float:
        if self._lock is None:
            return seed
        with self._lock:
            value = max(self._fp_max, seed)
            self._fp_max = seed
        return value

    # -- one stage ---------------------------------------------------------

    def stage(self, name: str):
        """A context manager around one stage. `with meter.stage("decode"):`"""
        if not self.enabled:
            return _NULL_STAGE
        return _Stage(self, name)

    def _begin(self, name: str) -> dict:
        if self._open is not None:
            # Nested stages would make both readings meaningless rather than
            # merely wrong, so say so instead of reporting a number.
            raise RuntimeError(f"stage {name!r} opened inside stage "
                               f"{self._open!r}: stages must be flat")
        self._open = name
        mlx = mlx_memory(self.mx)
        now = task_vm()
        footprint = (now["footprint"] / MB) if now else 0.0
        self._take_footprint_max(footprint)
        if self.mx is not None:
            fn = getattr(self.mx, "reset_peak_memory", None)
            if callable(fn):
                try:
                    fn()
                except Exception:                              # noqa: BLE001
                    pass
        return {"name": name, "started": time.perf_counter(),
                "active_before_mb": mlx.get("active_mb"),
                "footprint_before_mb": round(footprint, 1)}

    def _end(self, opened: dict) -> None:
        elapsed = time.perf_counter() - opened["started"]
        mlx = mlx_memory(self.mx)
        now = task_vm()
        footprint = (now["footprint"] / MB) if now else 0.0
        peak_fp = self._take_footprint_max(footprint)
        self._open = None
        name = opened["name"]
        row = self.stages.get(name)
        if row is None:
            row = {"name": name, "calls": 0, "seconds": 0.0,
                   "mlx_peak_mb": 0.0, "mlx_active_after_mb": 0.0,
                   "footprint_peak_mb": 0.0, "footprint_after_mb": 0.0,
                   "mlx_growth_mb": 0.0}
            self.stages[name] = row
            self.order.append(name)
        row["calls"] += 1
        row["seconds"] += elapsed
        # Peaks are the worst of every call to this stage; the "after"
        # readings are the most recent one, because a maximum of "what was
        # left afterwards" would read like a peak and is not one.
        for key, value in (("mlx_peak_mb", mlx.get("peak_mb")),
                           ("footprint_peak_mb", round(peak_fp, 1))):
            if value is not None and value > row[key]:
                row[key] = value
        for key, value in (("mlx_active_after_mb", mlx.get("active_mb")),
                           ("footprint_after_mb", round(footprint, 1))):
            if value is not None:
                row[key] = value
        before, after = opened.get("active_before_mb"), mlx.get("active_mb")
        if before is not None and after is not None:
            row["mlx_growth_mb"] = max(row["mlx_growth_mb"],
                                       round(after - before, 1))
        if self._log:
            self._log(f"[stage] {name}: {elapsed * 1000:.0f} ms, mlx peak "
                      f"{mlx.get('peak_mb')} MB, footprint peak "
                      f"{peak_fp:.0f} MB")

    # -- what it found -----------------------------------------------------

    def rows(self) -> list[dict]:
        return [self.stages[name] for name in self.order]

    def reset(self) -> None:
        self.stages, self.order = {}, []

    def table(self, title: str = "") -> str:
        head = (f"{'stage':<26}{'calls':>6}{'total s':>9}"
                f"{'mlx peak MB':>13}{'mlx after MB':>14}"
                f"{'fp peak MB':>12}{'fp after MB':>13}")
        lines = ([title] if title else []) + [head, "-" * len(head)]
        for row in self.rows():
            lines.append(
                f"{row['name']:<26}{row['calls']:>6}{row['seconds']:>9.1f}"
                f"{row['mlx_peak_mb']:>13.0f}{row['mlx_active_after_mb']:>14.0f}"
                f"{row['footprint_peak_mb']:>12.0f}{row['footprint_after_mb']:>13.0f}")
        return "\n".join(lines)


class _Stage:
    __slots__ = ("meter", "name", "opened")

    def __init__(self, meter: StageMeter, name: str):
        self.meter, self.name, self.opened = meter, name, None

    def __enter__(self):
        self.opened = self.meter._begin(self.name)
        return self

    def __exit__(self, *exc) -> bool:
        if self.opened is not None:
            self.meter._end(self.opened)
        return False


class _NullStage:
    """What `stage()` returns when the meter is off: no syscall, no branch
    inside the measured code, nothing to remember to turn off in production."""

    __slots__ = ()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


_NULL_STAGE = _NullStage()


def stage_meter(mx=None, enabled: bool = False, log=None,
                sample_ms: float = 3.0) -> StageMeter:
    return StageMeter(mx=mx, enabled=enabled, log=log, sample_ms=sample_ms)
