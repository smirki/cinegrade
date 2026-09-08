#!/usr/bin/env python3
"""The machine wide model lock, with no model and no server.

    uv run --project sam python sam/tests/test_model_lock.py

The lock keeps a second 5 GB model off a 16 GB machine, so the interesting
cases are the unhappy ones: a holder that died, and a lock nobody claims.
This test never touches the real lock directory at /tmp/fixxr-sam-model.lock,
because a real model process on this machine may be holding it right now: it
points the module at a temporary directory instead.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import check, report, sam_path                                # noqa: E402

sam_path()
import modellock                                                          # noqa: E402
from modellock import ModelLock                                           # noqa: E402


def quiet(_message: str) -> None:
    pass


def main() -> int:
    real = modellock.LOCK_DIR
    tmp = Path(tempfile.mkdtemp(prefix="sam-lock-"))
    modellock.LOCK_DIR = tmp / "model.lock"
    check("the test is not pointed at the real lock",
          modellock.LOCK_DIR != real, str(modellock.LOCK_DIR))

    print("one holder at a time")
    first = ModelLock(backend="mlx", retry_s=0.05, log=quiet)
    check("the first process gets the lock", first.acquire(timeout_s=2))
    check("the holder writes down who it is, so a stuck machine can be read",
          modellock.owner()["pid"] == os.getpid()
          and modellock.owner()["backend"] == "mlx",
          json.dumps(modellock.owner()))

    second = ModelLock(backend="torch-cpu", retry_s=0.05, log=quiet)
    began = time.time()
    check("a second process waits and gives up at its timeout rather than "
          "loading a model beside the first",
          second.acquire(timeout_s=0.5) is False,
          f"waited {time.time() - began:.1f}s")

    first.release()
    check("the directory is gone once the holder releases it",
          not modellock.LOCK_DIR.exists())
    check("release twice is not an error", first.release() is None)
    check("the next process gets it straight away", second.acquire(timeout_s=2))
    second.release()

    print("\na holder that died")
    modellock.LOCK_DIR.mkdir(parents=True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (modellock.LOCK_DIR / "owner.json").write_text(json.dumps(
        {"pid": dead.pid, "backend": "mlx", "since": "2026-09-08T00:00:00Z"}))
    said: list[str] = []
    third = ModelLock(backend="mlx", retry_s=0.05, log=said.append)
    check("a lock whose owner is gone is reclaimed, not waited on forever",
          third.acquire(timeout_s=2))
    check("and it says whose lock it took", any("reclaiming" in line for line in said),
          str(said[:1]))
    third.release()

    print("\na lock nobody claims")
    modellock.LOCK_DIR.mkdir(parents=True)          # no owner.json: a bare mkdir
    said.clear()
    fourth = ModelLock(backend="mlx", retry_s=0.05, log=said.append)
    check("an unowned lock is WAITED on, never stolen: it may be a model this "
          "module cannot name, and two models on this Mac swap it to a halt",
          fourth.acquire(timeout_s=0.5) is False)
    check("and the wait says how to clear it by hand",
          any("rmdir" in line for line in said), str(said[-1:]))

    old = os.stat(modellock.LOCK_DIR).st_mtime - 3600
    os.utime(modellock.LOCK_DIR, (old, old))
    check("an unowned lock is still not stolen when it is old, by default",
          ModelLock(backend="mlx", retry_s=0.05, log=quiet).acquire(0.3) is False)

    fifth = ModelLock(backend="mlx", retry_s=0.05, log=said.append, steal_stale=True)
    check("--steal-stale-lock takes an unowned lock that has sat there past "
          "the stale window", fifth.acquire(timeout_s=2))
    fifth.release()

    modellock.LOCK_DIR.mkdir(parents=True)
    young = ModelLock(backend="mlx", retry_s=0.05, log=quiet, steal_stale=True)
    check("even then, a lock made moments ago is left alone: that is a holder "
          "part way through writing its owner file",
          young.acquire(timeout_s=0.3) is False)
    modellock.LOCK_DIR.rmdir()

    print("\nturned off")
    off = ModelLock(enabled=False, log=quiet)
    check("a stub run takes no lock at all", off.acquire() and not modellock.LOCK_DIR.exists())

    with ModelLock(backend="mlx", retry_s=0.05, log=quiet) as held:
        check("it works as a context manager", held.held and modellock.LOCK_DIR.exists())
    check("and lets go at the end of the block", not modellock.LOCK_DIR.exists())

    return report("test_model_lock")


if __name__ == "__main__":
    raise SystemExit(main())
