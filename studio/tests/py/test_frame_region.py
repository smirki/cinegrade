#!/usr/bin/env python3
"""Contract E4, server side: region and zoom on the frame and stats routes.

    content/.venv/bin/python -m unittest discover -s studio/tests/py

Runs the REAL server (studio/server.py) as a subprocess on a random free port
with a temporary data directory, so nothing here can touch studio/data. The
server is killed by the PID captured at spawn, never by a name pattern.

The founder asked for "more zoom controls for the frontend and for the agent".
This is the agent's half: `region` picks a patch of the frame and `zoom` says
how big to render it, so an agent can inspect skin or a highlight closely and
can measure a patch instead of guessing from a scope of the whole shot.

What it pins:

  compatibility  a request with no region and no zoom answers with exactly the
                 frame key it answered with before this existed, and a region
                 covering the whole frame is the same size as no region. The
                 new fields cannot change a request that did not ask for them.
  dimensions     a region half the frame on each axis comes back half the
                 frame's pixels on each axis, and zoom 2 doubles that, up to
                 the cap the source's own resolution is.
  the cache key  a region request and a full frame request at the same render
                 width do NOT share a key. Without that they would collide in
                 the JPEG cache (encode_jpeg keys on the frame key alone) and
                 one would be served as the other. Measured as two different
                 X-Frame-Key values and two different JPEG bodies.
  measurements   POST /api/stats with a region measures only that patch: on a
                 real clip the two halves of the frame have different means,
                 and the whole frame's mean sits between them.
  saying no      a sliver under 1% of an axis, a region of the wrong length
                 and a zoom of zero are refused with a 400 that names what is
                 wrong, not silently measured somewhere else.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
STUDIO = HERE.parents[1]
CONTENT = STUDIO.parent
PYTHON = str(CONTENT / ".venv" / "bin" / "python")
SERVER = str(STUDIO / "server.py")

HALF = [0.25, 0.25, 0.75, 0.75]
WIDTH = 480


def _free_port() -> int:
    """A port nothing is listening on, chosen by the kernel. Never 7431."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    if port == 7431:                                   # pragma: no cover
        return _free_port()
    return port


def _get(url: str, timeout: float = 60.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


class FrameRegionTest(unittest.TestCase):
    """One server for the whole class: each render costs a few seconds."""

    proc = None
    data_dir = None
    base = ""
    clip = ""

    @classmethod
    def setUpClass(cls):
        cls.data_dir = str(Path(os.environ.get("TMPDIR", "/tmp"))
                           / f"studio-region-data-{os.getpid()}")
        Path(cls.data_dir).mkdir(parents=True, exist_ok=True)
        port = _free_port()
        cls.base = f"http://127.0.0.1:{port}/api"
        env = dict(os.environ, STUDIO_DATA_DIR=cls.data_dir)
        cls.proc = subprocess.Popen(
            [PYTHON, SERVER, "--port", str(port), "--data-dir", cls.data_dir],
            cwd=str(CONTENT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.time() + 40.0
        last = None
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                out, err = cls.proc.communicate()
                raise RuntimeError("the studio server exited while starting:\n"
                                   + err.decode("utf-8", "replace")[-2000:])
            try:
                _get(cls.base + "/state", timeout=5.0)
                break
            except Exception as exc:                          # noqa: BLE001
                last = exc
                time.sleep(0.25)
        else:
            cls._stop()
            raise RuntimeError(f"the studio server never answered: {last}")

        state = _get(cls.base + "/state")
        clips = [c for c in state["clips"] if not c.get("error")]
        if not clips:
            cls._stop()
            raise unittest.SkipTest("this test needs a clip in footage/")
        cls.clip = clips[0]["name"]

    @classmethod
    def _stop(cls):
        if cls.proc is None:
            return
        # By the PID captured at spawn, never by a name pattern.
        try:
            cls.proc.terminate()
            cls.proc.wait(timeout=10)
        except Exception:                                     # noqa: BLE001
            try:
                cls.proc.kill()
            except Exception:                                 # noqa: BLE001
                pass
        for pipe in (cls.proc.stdout, cls.proc.stderr):
            if pipe is not None:
                pipe.close()
        cls.proc = None

    @classmethod
    def tearDownClass(cls):
        cls._stop()
        if cls.data_dir:
            # Only the directory this test made, never studio/data.
            shutil.rmtree(cls.data_dir, ignore_errors=True)

    # -- helpers ----------------------------------------------------------

    def _frame(self, **extra):
        """POST /api/frame, answering with the headers and the JPEG body."""
        body = {"clip": self.clip, "time": 0.5, "width": WIDTH, "config": {}}
        body.update(extra)
        req = urllib.request.Request(
            self.base + "/frame", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=180) as r:
            data = r.read()
            size = r.headers.get("X-Frame-Size").split("x")
            return {
                "key": r.headers.get("X-Frame-Key"),
                "width": int(size[0]), "height": int(size[1]),
                "region": r.headers.get("X-Frame-Region"),
                "pixels": r.headers.get("X-Frame-Region-Pixels"),
                "full": r.headers.get("X-Frame-Full-Size"),
                "body": data,
            }

    def _stats(self, **extra):
        body = {"clip": self.clip, "time": 0.5, "width": 320, "config": {}}
        body.update(extra)
        req = urllib.request.Request(
            self.base + "/stats", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read())

    def _refused(self, route: str, **extra):
        """The error message from a request the server should turn down."""
        body = {"clip": self.clip, "time": 0.5, "width": WIDTH, "config": {}}
        body.update(extra)
        req = urllib.request.Request(
            self.base + "/" + route, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                r.read()
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read()).get("error", "")
        return 200, ""

    # -- tests ------------------------------------------------------------

    def test_no_region_is_the_frame_it_always_was(self):
        """The new fields cannot change a request that does not use them."""
        plain = self._frame()
        again = self._frame(zoom=1)
        whole = self._frame(region=[0.0, 0.0, 1.0, 1.0])
        self.assertEqual(plain["key"], again["key"],
                         "zoom 1 changed the frame key, so it changed the cache entry")
        self.assertIsNone(plain["region"], "a frame with no region reported one")
        self.assertEqual((plain["width"], plain["height"]),
                         (whole["width"], whole["height"]),
                         "a region covering everything is not the size of no region")

    def test_a_half_region_is_half_the_pixels(self):
        """A region half the frame on each axis is half its pixels on each."""
        whole = self._frame()
        half = self._frame(region=HALF)
        self.assertAlmostEqual(half["width"], round(whole["width"] / 2), delta=2)
        self.assertAlmostEqual(half["height"], round(whole["height"] / 2), delta=2)
        self.assertEqual(half["full"], f"{whole['width']}x{whole['height']}",
                         "the region did not come out of a whole frame render")
        self.assertEqual([int(v) for v in half["pixels"].split(",")][2:],
                         [half["width"], half["height"]],
                         "the reported pixel box and the picture disagree")

    def test_zoom_multiplies_the_region_up_to_the_source(self):
        """zoom N is N times the fit size, and the source is the ceiling."""
        half = self._frame(region=HALF)
        twice = self._frame(region=HALF, zoom=2)
        self.assertAlmostEqual(twice["width"], half["width"] * 2, delta=3)
        # The cap: no zoom can invent pixels the source does not have.
        state = _get(self.base + "/state")
        entry = [c for c in state["clips"] if c["name"] == self.clip][0]
        source_w = entry["autorotate"]["width"]
        huge = self._frame(region=HALF, zoom=999)
        self.assertLessEqual(huge["width"], round(source_w * 0.5) + 2,
                             "a region was rendered wider than its own source pixels")

    def test_a_region_does_not_share_a_cache_key_with_the_whole_frame(self):
        """Two different pictures, two different keys, two different bodies."""
        # Same RENDER width for both: zoom 1 on a half region renders the whole
        # frame at WIDTH and crops it, which is the exact collision the key
        # suffix exists to stop.
        whole = self._frame()
        half = self._frame(region=HALF)
        self.assertNotEqual(whole["key"], half["key"])
        self.assertNotEqual(whole["body"], half["body"])
        self.assertTrue(half["key"].startswith(whole["key"]),
                        "the region key is not the frame key plus the region: "
                        f"{half['key']} vs {whole['key']}")

    def test_stats_measures_only_the_region(self):
        """A patch's numbers are the patch's, not the whole frame's."""
        whole = self._stats()
        top = self._stats(region=[0.0, 0.0, 1.0, 0.5])
        bottom = self._stats(region=[0.0, 0.5, 1.0, 1.0])
        self.assertEqual(top["region"], [0.0, 0.0, 1.0, 0.5])
        self.assertEqual(top["size"][1], round(whole["size"][1] / 2))
        self.assertEqual(top["size"][0], whole["size"][0])
        mid = whole["stats"]["luma"]["mean"]
        a = top["stats"]["luma"]["mean"]
        b = bottom["stats"]["luma"]["mean"]
        self.assertNotAlmostEqual(
            a, b, places=4,
            msg="the two halves of a real frame measured identically, which "
                "means the region was not applied")
        self.assertGreaterEqual(mid, min(a, b) - 1e-6)
        self.assertLessEqual(mid, max(a, b) + 1e-6)

    def test_a_region_that_cannot_be_rendered_is_refused(self):
        """Named, with a 400, rather than silently measured somewhere else."""
        code, msg = self._refused("frame", region=[0.2, 0.2, 0.2005, 0.9])
        self.assertEqual(code, 400)
        self.assertIn("too small", msg)
        code, msg = self._refused("frame", region=[0.1, 0.1, 0.9])
        self.assertEqual(code, 400)
        self.assertIn("four numbers", msg)
        code, msg = self._refused("frame", region=HALF, zoom=0)
        self.assertEqual(code, 400)
        self.assertIn("greater than zero", msg)
        code, msg = self._refused("stats", region=[0.5, 0.5, 0.5001, 0.5001])
        self.assertEqual(code, 400)
        self.assertIn("too small", msg)


if __name__ == "__main__":                                    # pragma: no cover
    unittest.main()
