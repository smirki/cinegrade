"""The machine wide model lock.

This Mac has 16 GB of unified memory and a resident SAM 3.1 model costs about
5 GB of it. Several agents work on this machine at once, and more than one of
them loads a SAM model, so "one model process at a time" is a rule about the
whole machine, not about one process.

The lock is a directory: `mkdir` is atomic on macOS, so a successful mkdir is
the lock. The holder writes `owner.json` inside it (pid, backend, when, what
started it) for two reasons: so a person looking at a stuck machine can see
who is holding it, and so a lock left behind by a process that was killed can
be reclaimed instead of blocking every later run forever.

Reclaiming is deliberately narrow, and no process is ever signalled or killed
here. The lock is taken from a previous holder in one case only: `owner.json`
exists AND names a process id that is no longer alive.

A lock directory with nothing in it is a different situation, and this module
does NOT resolve it by itself. Other code in this codebase (and the spike
scripts) takes the same directory with a bare `mkdir` and never writes an
owner file, so an empty lock directory can mean either "a model is loaded in
a process this module cannot name" or "a run died and left it behind". Those
look identical from the outside, and guessing wrong means two 5 GB models on
a 16 GB machine, which does not fail cleanly: it swaps until everything on
the Mac crawls. So an unowned lock is waited on, with a line every few
minutes saying exactly what to check and how to clear it by hand, and a
caller that is certain can opt in with `steal_stale=True`.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import sys
import time
from pathlib import Path

LOCK_DIR = Path("/tmp/fixxr-sam-model.lock")
OWNER_FILE = "owner.json"
RETRY_S = 20.0
# How long an ownerless lock directory has to sit untouched before
# --steal-stale-lock will remove it, and how often the wait says out loud
# what is going on.
STALE_S = 900.0
NAG_S = 300.0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Somebody else's process, but it exists.
        return True
    except OSError as exc:                                     # noqa: BLE001
        return exc.errno != errno.ESRCH
    return True


def _dir(lock_dir=None) -> Path:
    """Which directory IS the lock. Resolved at call time, never cached, so a
    test that points `modellock.LOCK_DIR` at a temporary directory (and
    `torch_backend`, which keeps its own constant) both work without either
    of them reaching inside this module."""
    return Path(lock_dir) if lock_dir is not None else LOCK_DIR


def owner(lock_dir=None) -> dict | None:
    """Who holds the lock right now, as far as the filesystem can say."""
    try:
        raw = (_dir(lock_dir) / OWNER_FILE).read_text()
    except OSError:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _age_s(lock_dir=None) -> float:
    """How long the lock directory has sat there untouched."""
    here = _dir(lock_dir)
    try:
        newest = here.stat().st_mtime
        for entry in here.iterdir():
            newest = max(newest, entry.stat().st_mtime)
    except OSError:
        return 0.0
    return max(0.0, time.time() - newest)


def _reclaim_if_dead(log, steal_stale: bool = False,
                    stale_s: float = STALE_S, lock_dir=None) -> bool:
    """True when a lock left behind by a holder that is definitely gone was
    removed. See the module docstring."""
    here = _dir(lock_dir)
    info = owner(here)
    if not info:
        age = _age_s(here)
        if not steal_stale or age < stale_s:
            return False
        log(f"[model-lock] --steal-stale-lock was given and the lock at "
            f"{here} has named no owner for {age / 60:.0f} minutes, so it "
            f"is being removed. If a model really is loaded in another "
            f"process, stop this one now.")
        try:
            shutil.rmtree(here)
        except OSError:
            return False
        return True
    pid = int(info.get("pid", 0) or 0)
    if _pid_alive(pid):
        return False
    # Read the owner again, immediately before deleting anything. Between the
    # first read above and this line another process can have taken the lock
    # for itself (the dead holder's directory removed and remade), and
    # deleting THAT holder's lock is the bug this re-read exists to stop:
    # round 1 finding 11's third part.
    fresh = owner(here)
    if fresh != info:
        return False
    log(f"[model-lock] reclaiming the lock from pid {pid}, which is gone "
        f"(held since {info.get('since')}, backend {info.get('backend')})")
    try:
        shutil.rmtree(here)
    except OSError:
        return False
    return True


class ModelLock:
    """Hold the lock for as long as the model is resident.

    Use it as a context manager, or call `acquire()` and `release()`. Release
    is idempotent and safe to call from an exit hook.
    """

    def __init__(self, backend: str = "?", retry_s: float = RETRY_S,
                 enabled: bool = True, log=None, steal_stale: bool = False,
                 stale_s: float = STALE_S, lock_dir=None):
        self.backend = backend
        self.retry_s = float(retry_s)
        self.enabled = bool(enabled)
        self.steal_stale = bool(steal_stale)
        self.stale_s = float(stale_s)
        # `held` means "this object took the lock and owns the directory".
        # `bypassed` means "the lock is disabled, so nothing is in our way".
        # They are separate because they were once the same flag, and a
        # disabled run then reported itself as the holder AND deleted the real
        # holder's lock directory on the way out (round 1 finding 11).
        self.held = False
        self.bypassed = False
        self._lock_dir = lock_dir
        self._log = log or (lambda message: print(message, flush=True))

    @property
    def dir(self) -> Path:
        """The directory this lock is, resolved now rather than at
        construction, so `modellock.LOCK_DIR` can be redirected by a test."""
        return _dir(self._lock_dir)

    def acquire(self, timeout_s: float | None = None) -> bool:
        """Block until the lock is ours. Returns True when held.

        True means "go ahead and load a model", which is also the answer when
        the lock is disabled. Whether this process actually OWNS the lock
        directory is `held`, and only a holder may release it.

        `timeout_s` None means wait forever, which is what a service wants:
        the other holder is another model run on this machine and it will
        finish. A timeout returns False rather than raising so the caller can
        decide (the tests use a short one).
        """
        if not self.enabled:
            self.bypassed = True
            return True
        if self.held:
            return True
        here = self.dir
        deadline = None if timeout_s is None else time.time() + timeout_s
        waited = False
        last_said = 0.0
        while True:
            try:
                here.mkdir()
            except FileExistsError:
                if _reclaim_if_dead(self._log, self.steal_stale, self.stale_s,
                                    lock_dir=here):
                    continue
                if deadline is not None and time.time() >= deadline:
                    return False
                if not waited or time.time() - last_said >= NAG_S:
                    info = owner(here) or {}
                    who = (f"pid {info.get('pid')} ({info.get('backend')}, since "
                           f"{info.get('since')})" if info else "a process that "
                           "left no owner file")
                    self._log(f"[model-lock] held by {who}; waiting, retry every "
                              f"{self.retry_s:.0f}s. Only one SAM model may be "
                              f"resident on this machine at a time.")
                    if not info:
                        self._log(f"[model-lock] nothing in {here} says who "
                                  f"holds it (it has sat there "
                                  f"{_age_s(here) / 60:.0f} "
                                  f"minutes). Check for a python process with a "
                                  f"model loaded; if there is none, remove it by "
                                  f"hand with `rmdir {here}`, or start this "
                                  f"with --steal-stale-lock.")
                    waited = True
                    last_said = time.time()
                time.sleep(min(self.retry_s, 1.0 if deadline is not None else self.retry_s))
                continue
            self.held = True
            try:
                (here / OWNER_FILE).write_text(json.dumps({
                    "pid": os.getpid(),
                    "backend": self.backend,
                    "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "argv": sys.argv[:6],
                }, indent=2))
            except OSError:
                pass
            if waited:
                self._log("[model-lock] acquired")
            return True

    def release(self) -> None:
        """Give the lock back, but only if it is ours to give.

        The owner file is checked first, by pid. A process that never took the
        lock (`--no-model-lock`, or a second `release()` after the first) must
        not remove the directory: doing that erased a real holder's lock while
        a 5 GB model was resident in it, and the machine then had room for a
        second one (round 1 finding 11).
        """
        self.bypassed = False
        if not self.held:
            return
        self.held = False
        here = self.dir
        info = owner(here)
        if info is not None and int(info.get("pid", 0) or 0) != os.getpid():
            self._log(f"[model-lock] not releasing {here}: it says pid "
                      f"{info.get('pid')} holds it and this is pid "
                      f"{os.getpid()}")
            return
        try:
            (here / OWNER_FILE).unlink()
        except OSError:
            pass
        try:
            here.rmdir()
        except OSError:
            # Not ours any more, or not empty: leave it rather than take
            # somebody else's lock away.
            pass

    def __enter__(self) -> "ModelLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
