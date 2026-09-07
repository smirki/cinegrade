#!/usr/bin/env python3
"""Contract C7, server side: the picked rectangle reaches the matcher.

    content/.venv/bin/python -m unittest discover -s studio/tests/py

Runs the REAL server (studio/server.py) as a subprocess on a random free port
with a temporary data directory, so nothing here can touch studio/data. The
server is killed by the PID captured at spawn, never by a name pattern. Real
reference images from refs/ and a real clip from footage/ go through the real
match, because the thing under test is whether the rectangle survives the whole
chain (browser body, route, match_reference, ffmpeg) and lands on the pixels.

What it pins:

  no rectangle    two matches with no rectangle produce the same cube byte for
                  byte, and both say they measured the whole image. That is the
                  compatibility claim: nothing about a match nobody cropped
                  changed when this feature landed.
  a rectangle     ref_crop in the body produces a different cube, and the
                  response says which rectangle it measured and that the
                  rectangle came from the request.
  the fall back   with the same rectangle saved on the project (the tab writes
                  it there through POST /api/project/extra), a match whose body
                  says nothing about rectangles produces the SAME cube as the
                  explicit one, and says the rectangle came from the project.
                  That is what lets the tab keep measuring the box the person
                  drew on the call the page makes without repeating it.
  who inherits    the fall back is for the browser tab only. A caller that
                  sends X-Studio-Agent gets the whole image unless it sends a
                  rectangle itself, because the stored rectangles sit on the
                  shared project and an agent has no way to know whose they
                  are; sending the box it read from GET /api/project puts it
                  back on the tab's own cube.
  saying no       a body with ref_crop null means whole image even when the
                  project has a rectangle saved, so the tab's own readout can
                  never be a lie about what was measured.

The cubes are written where the app writes them (grade/luts/looks). Every file
that was not there before this suite ran is removed at the end; nothing that
was already there is touched.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
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
LOOKS = CONTENT / "grade" / "luts" / "looks"

LEFT_HALF = [0.0, 0.0, 0.5, 1.0]
TOP_HALF = [0.0, 0.0, 1.0, 0.5]


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


def _post(url: str, payload: dict, timeout: float = 180.0, agent: str = ""):
    headers = {"Content-Type": "application/json"}
    if agent:
        headers["X-Studio-Agent"] = agent
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


class MatchCropTest(unittest.TestCase):
    """One server for the whole class: each match costs a few seconds."""

    proc = None
    data_dir = None
    base = ""
    clip = ""
    ref = ""
    before_looks: set = set()

    @classmethod
    def setUpClass(cls):
        cls.before_looks = set(LOOKS.glob("*.cube")) if LOOKS.exists() else set()
        cls.data_dir = tempfile.mkdtemp(prefix="studio-matchcrop-data-")
        port = _free_port()
        cls.base = f"http://127.0.0.1:{port}/api"
        env = dict(os.environ, STUDIO_DATA_DIR=cls.data_dir)
        cls.proc = subprocess.Popen(
            [PYTHON, SERVER, "--port", str(port), "--data-dir", cls.data_dir],
            cwd=str(CONTENT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.time() + 30.0
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
        refs = state.get("refs") or []
        if not clips or not refs:
            cls._stop()
            raise unittest.SkipTest(
                "this test needs a clip in footage/ and an image in refs/")
        cls.clip = clips[0]["name"]
        # The smallest reference on disk: every match here decodes it in full
        # and the point being tested has nothing to do with its size.
        cls.ref = sorted(refs, key=lambda r: r.get("bytes") or 0)[0]["name"]

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
        # Only the cubes this run created, never one that was already there.
        if LOOKS.exists():
            for p in set(LOOKS.glob("*.cube")) - cls.before_looks:
                try:
                    p.unlink()
                except OSError:                               # pragma: no cover
                    pass

    # -- helpers ----------------------------------------------------------

    def _match(self, agent: str = "", **extra):
        body = {"clip": self.clip, "ref": self.ref, "time": 0.5,
                "config": {}, "method": "reinhard", "strength": 1.0,
                "luma_preserve": True}
        body.update(extra)
        result = _post(self.base + "/match", body, agent=agent)
        cube = Path(result["lut"]).read_bytes()
        return result, cube

    def _store(self, value):
        """Write (or clear) this project's match_crops the way the tab does."""
        _post(self.base + "/project/open", {"clip": self.clip})
        return _post(self.base + "/project/extra",
                     {"clip": self.clip, "name": "match_crops", "value": value})

    # -- the tests --------------------------------------------------------

    def test_no_rectangle_is_the_same_cube_twice_and_says_whole_image(self):
        self._store(None)
        first, cube_a = self._match()
        second, cube_b = self._match()
        self.assertEqual(cube_a, cube_b,
                         "two identical matches must write the same cube")
        for r in (first, second):
            self.assertIsNone(r["crops"]["ref"],
                              "no rectangle was asked for or saved")
            self.assertIsNone(r["crops"]["frame"])
            self.assertEqual(r["crops"]["ref_source"], "none")
            self.assertEqual(r["crops"]["frame_source"], "none")
        # The reference's own automatic content detector still ran, which is
        # the behaviour every match had before rectangles existed.
        self.assertIn(first["crop"]["source"], ("auto", "none"))

    def test_a_reference_rectangle_changes_the_cube_and_is_reported(self):
        self._store(None)
        _plain, cube_plain = self._match()
        picked, cube_picked = self._match(ref_crop=LEFT_HALF)
        self.assertNotEqual(
            cube_plain, cube_picked,
            "measuring half the reference must not produce the same transform "
            "as measuring all of it")
        pick = picked["crops"]["ref"]
        self.assertIsNotNone(pick, "the response must say what it measured")
        self.assertEqual(pick["box"], LEFT_HALF)
        self.assertEqual(picked["crops"]["ref_source"], "request")
        self.assertAlmostEqual(pick["area_pct"], 50.0, delta=1.0)
        self.assertIsNone(picked["crops"]["frame"])
        # The chrome trim runs INSIDE the rectangle, so the reported content
        # region is measured against the cropped image, not the whole one.
        self.assertLessEqual(pick["pixels"][0], pick["image"][0])

    def test_a_frame_rectangle_changes_the_cube_and_is_reported(self):
        self._store(None)
        _plain, cube_plain = self._match()
        picked, cube_picked = self._match(frame_crop=TOP_HALF)
        self.assertNotEqual(cube_plain, cube_picked,
                            "measuring half the frame must move the fit")
        pick = picked["crops"]["frame"]
        self.assertIsNotNone(pick)
        self.assertEqual(pick["box"], TOP_HALF)
        self.assertEqual(picked["crops"]["frame_source"], "request")
        self.assertIsNone(picked["crops"]["ref"])

    def test_a_saved_rectangle_is_used_when_the_body_is_silent(self):
        self._store(None)
        explicit, cube_explicit = self._match(ref_crop=LEFT_HALF)
        self.assertEqual(explicit["crops"]["ref_source"], "request")

        self._store({self.ref: {"ref": LEFT_HALF}})
        saved = _get(self.base + f"/project?clip={self.clip}")
        self.assertEqual(saved["extras"]["match_crops"][self.ref]["ref"],
                         LEFT_HALF,
                         "GET /api/project is where an agent reads the pick")

        fell_back, cube_fell_back = self._match()
        self.assertEqual(fell_back["crops"]["ref_source"], "project")
        self.assertEqual(fell_back["crops"]["ref"]["box"], LEFT_HALF)
        self.assertEqual(
            cube_explicit, cube_fell_back,
            "the saved rectangle and the same rectangle in the body must "
            "produce the same cube, byte for byte")
        self._store(None)

    def test_an_explicit_null_beats_the_saved_rectangle(self):
        self._store(None)
        _plain, cube_plain = self._match()
        self._store({self.ref: {"ref": LEFT_HALF}})
        said_no, cube_said_no = self._match(ref_crop=None, frame_crop=None)
        self.assertEqual(said_no["crops"]["ref_source"], "none")
        self.assertIsNone(said_no["crops"]["ref"])
        self.assertEqual(
            cube_plain, cube_said_no,
            "a request that says null must measure the whole image even with "
            "a rectangle saved on the project")
        self._store(None)

    def test_an_agent_does_not_inherit_the_projects_rectangle(self):
        """The fallback is a browser tab convenience, not a default.

        The stored rectangles live on the PROJECT, which every caller shares
        with logins off, so an agent that never drew a box was measuring
        somebody else's rectangle without any way of knowing. A caller that
        sends X-Studio-Agent now gets the whole image unless it sends a
        rectangle itself; the bare caller in the same breath still falls back,
        which is what keeps the tab's behaviour exactly as it was.
        """
        # One pinned name for every call here. The default name carries the
        # caller's own user id (match_u0_... against match_u1_...), and that
        # id is written into the cube's own TITLE line, so two cubes with
        # identical numbers would still differ in their first bytes and the
        # comparison would be about naming rather than about pixels.
        pin = {"name": "crop_fallback_probe"}
        self._store(None)
        _plain, cube_plain = self._match(**pin)
        self._store({self.ref: {"ref": LEFT_HALF}})

        as_agent, cube_agent = self._match(agent="crop-fallback-probe", **pin)
        self.assertEqual(as_agent["crops"]["ref_source"], "none",
                         "an agent inherited the project's rectangle")
        self.assertIsNone(as_agent["crops"]["ref"])
        self.assertEqual(
            cube_agent, cube_plain,
            "an agent that sent no rectangle must measure the whole image, "
            "byte for byte the same cube as a match with nothing saved")

        as_tab, cube_tab = self._match(**pin)
        self.assertEqual(as_tab["crops"]["ref_source"], "project",
                         "the browser tab lost its own saved rectangle")
        self.assertEqual(as_tab["crops"]["ref"]["box"], LEFT_HALF)
        self.assertNotEqual(cube_tab, cube_plain,
                            "this fixture only means something if the saved "
                            "rectangle actually changes the cube")

        # An agent that does want it reads it and sends it, and then the
        # cube is the tab's cube again.
        saved = _get(self.base + f"/project?clip={self.clip}")
        box = saved["extras"]["match_crops"][self.ref]["ref"]
        told, cube_told = self._match(agent="crop-fallback-probe",
                                      ref_crop=box, **pin)
        self.assertEqual(told["crops"]["ref_source"], "request")
        self.assertEqual(cube_told, cube_tab)
        self._store(None)

    def test_a_rectangle_too_small_to_measure_is_refused_by_name(self):
        self._store(None)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._match(ref_crop=[0.5, 0.5, 0.5005, 0.9])
        body = json.loads(caught.exception.read())
        self.assertIn("ref-crop", body.get("error", ""),
                      f"the error should name the field, got {body!r}")


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main()
