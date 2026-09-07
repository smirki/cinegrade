#!/usr/bin/env python3
"""One render per cache key, however many callers asked for it at once.

    content/.venv/bin/python -m unittest discover -s studio/tests/py

Every commit the browser makes fires one POST /api/stats and three or four
POST /api/scope with the same clip, time, width and config, so all of them
land on ONE render_raw cache key. Before this they each ran their own ffmpeg
on a cold cache, in parallel, competing for the same cores; the W1-fix lane
measured 800 to 1700 ms each cold against 1 to 2 ms warm, and that is what
made three browser specs read their answer before it had arrived.

Two halves, because the mechanism and the effect are different claims:

  the primitive   server._SingleFlight, driven directly by many threads with
                  a deliberate gap between "is it cached" and "cache it", runs
                  its guarded body exactly once and leaves no entry behind in
                  the module's map afterwards.
  the effect      against a real server on a cold cache, several concurrent
                  scope requests for the SAME frame come back identical and
                  finish in less wall time than the same number of requests
                  for DIFFERENT frames, which is the observable shape of "one
                  render, not four".

The server is a real subprocess on a random free port with its own
--data-dir (so its cache is its own and cold, and studio/data is never
opened) and its own --footage folder of symlinks. It is killed by the PID
captured at spawn, never by a name pattern.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
STUDIO = HERE.parents[1]
CONTENT = STUDIO.parent
PYTHON = str(CONTENT / ".venv" / "bin" / "python")
SERVER = str(STUDIO / "server.py")
FOOTAGE_SRC = CONTENT / "footage"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    if port == 7431:                                        # pragma: no cover
        return _free_port()
    return port


class SingleFlightPrimitiveTest(unittest.TestCase):
    """The lock itself, with no server and no ffmpeg in the way."""

    server_mod = None
    tmp = None

    @classmethod
    def setUpClass(cls):
        # server.py opens a database at import time. STUDIO_DATA_DIR is set
        # first so that database is a throwaway one and studio/data is never
        # touched by importing the module.
        cls.tmp = tempfile.mkdtemp(prefix="studio-singleflight-import-")
        os.environ["STUDIO_DATA_DIR"] = cls.tmp
        sys.path.insert(0, str(STUDIO))
        import server                                       # noqa: PLC0415
        cls.server_mod = server

    @classmethod
    def tearDownClass(cls):
        if cls.tmp:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_many_callers_for_one_key_run_the_body_once(self):
        sf = self.server_mod._SingleFlight
        done = []
        ran = []
        start = threading.Barrier(8)

        def worker():
            start.wait()
            # The shape render_raw has: look, and only if it is not there,
            # queue up and look again before doing the expensive thing.
            if done:
                return
            with sf("test-key"):
                if done:
                    return
                ran.append(1)
                time.sleep(0.05)      # the window a second thread would race
                done.append(1)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(ran), 1,
                         f"the guarded body ran {len(ran)} times for one key")

    def test_two_keys_do_not_block_each_other(self):
        sf = self.server_mod._SingleFlight
        order = []
        held = threading.Event()

        def slow():
            with sf("key-a"):
                held.set()
                time.sleep(0.2)
                order.append("a")

        def quick():
            held.wait(2.0)
            with sf("key-b"):
                order.append("b")

        ta, tb = threading.Thread(target=slow), threading.Thread(target=quick)
        ta.start(); tb.start(); ta.join(); tb.join()
        self.assertEqual(order, ["b", "a"],
                         "a caller on a different key waited for this one")

    def test_the_map_is_empty_once_everyone_has_left(self):
        sf = self.server_mod._SingleFlight
        flight = self.server_mod._flight
        before = len(flight)
        threads = [threading.Thread(target=lambda i=i: self._touch(sf, i))
                   for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(flight), before,
                         "single flight entries were left behind, so a long "
                         "session would accumulate one lock per frame")

    @staticmethod
    def _touch(sf, i):
        with sf(f"leak-check-{i % 2}"):
            time.sleep(0.01)


class SingleFlightOverHttpTest(unittest.TestCase):
    """The effect, against the real server on a cold cache."""

    proc = None
    data_dir = None
    footage_dir = None
    base = ""
    clip = ""
    duration = 0.0

    @classmethod
    def setUpClass(cls):
        clips = sorted(p for p in FOOTAGE_SRC.glob("*")
                       if p.is_file() and not p.name.startswith("."))
        if not clips:
            raise unittest.SkipTest("this test needs a clip in content/footage")
        cls.footage_dir = tempfile.mkdtemp(prefix="studio-singleflight-footage-")
        os.symlink(str(clips[0]), str(Path(cls.footage_dir) / clips[0].name))
        cls.data_dir = tempfile.mkdtemp(prefix="studio-singleflight-data-")
        port = _free_port()
        cls.base = f"http://127.0.0.1:{port}/api"
        cls.proc = subprocess.Popen(
            [PYTHON, SERVER, "--port", str(port), "--data-dir", cls.data_dir,
             "--footage", cls.footage_dir, "--quiet"],
            cwd=str(CONTENT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.time() + 60.0
        last = None
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                _out, err = cls.proc.communicate()
                raise RuntimeError("the studio server exited while starting:\n"
                                   + err.decode("utf-8", "replace")[-2000:])
            try:
                with urllib.request.urlopen(cls.base + "/state", timeout=5) as r:
                    state = json.loads(r.read())
                break
            except Exception as exc:                        # noqa: BLE001
                last = exc
                time.sleep(0.25)
        else:
            cls._stop()
            raise RuntimeError(f"the studio server never answered: {last}")
        usable = [c for c in state["clips"] if not c.get("error")]
        if not usable:
            cls._stop()
            raise unittest.SkipTest("the linked clip did not probe")
        cls.clip = usable[0]["name"]
        # Timecodes are taken from the clip's own length rather than written
        # in: the shortest clip in content/footage is 5 seconds, and a fixed
        # 8.5s would be a 400 rather than a measurement.
        cls.duration = float(usable[0].get("duration") or 0.0)
        if cls.duration < 2.0:
            cls._stop()
            raise unittest.SkipTest("the linked clip is too short to sample")

    @classmethod
    def _stop(cls):
        if cls.proc is None:
            return
        try:                                    # by the captured PID only
            cls.proc.terminate()
            cls.proc.wait(timeout=10)
        except Exception:                                   # noqa: BLE001
            try:
                cls.proc.kill()
            except Exception:                               # noqa: BLE001
                pass
        for pipe in (cls.proc.stdout, cls.proc.stderr):
            if pipe is not None:
                pipe.close()
        cls.proc = None

    @classmethod
    def tearDownClass(cls):
        cls._stop()
        for d in (cls.data_dir, cls.footage_dir):
            if d:
                shutil.rmtree(d, ignore_errors=True)

    def _scope(self, at_time: float) -> bytes:
        body = {"clip": self.clip, "time": at_time, "width": 640,
                "kind": "waveform", "size": 300, "config": {}}
        req = urllib.request.Request(
            self.base + "/scope", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.read()

    def _burst(self, times: list) -> tuple:
        out = [None] * len(times)
        errors = []
        ready = threading.Barrier(len(times))

        def one(i, t):
            try:
                ready.wait()
                out[i] = self._scope(t)
            except Exception as exc:                        # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=one, args=(i, t))
                   for i, t in enumerate(times)]
        t0 = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.time() - t0
        self.assertEqual(errors, [], "a concurrent scope request failed")
        return out, elapsed

    def test_concurrent_requests_for_one_frame_agree_and_cost_less(self):
        n = 6
        span = self.duration * 0.8
        same, t_same = self._burst([span * 0.5] * n)
        self.assertEqual(len(set(same)), 1,
                         "concurrent requests for the identical frame came "
                         "back as different images, so the shared render is "
                         "not being shared safely")
        # Distinct timecodes on the same cold server: n real renders, which
        # is what n concurrent identical requests used to cost too.
        diff, t_diff = self._burst([0.05 + span * i / n for i in range(n)])
        self.assertEqual(len(set(diff)), n,
                         "six different timecodes rendered the same image, so "
                         "this comparison is not measuring what it claims")
        self.assertLess(
            t_same, t_diff,
            f"{n} concurrent requests for ONE frame took {t_same:.2f}s and "
            f"{n} for {n} different frames took {t_diff:.2f}s. The first "
            f"number is supposed to be about one render and the second about "
            f"{n}; if they are the same the single flight is not collapsing "
            f"them")


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main()
