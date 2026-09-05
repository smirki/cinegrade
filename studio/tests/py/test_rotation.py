#!/usr/bin/env python3
"""Contract C2, server side: one rotation setting, every legacy form accepted.

    content/.venv/bin/python -m unittest discover -s studio/tests/py

Runs the REAL server (studio/server.py) as a subprocess on a random free port
with a temporary data directory, so nothing here can touch studio/data. The
server is killed by the PID captured at spawn, never by a name pattern.

What it pins:

  the alias      POST /api/frame with {"rotation": "0"} and the same request
                 with the old {"autorotate": false} must come back byte for
                 byte identical. The browser still sends the boolean on every
                 request, so the day that stops being true is the day the
                 studio starts rendering a different picture than the CLI.
  the setting    {"rotation": "90"} must come back with the width and height
                 swapped, which is the whole point of the feature: the
                 founder's camera tags landscape shots as -90.
  precedence     rotation, then the legacy flag, then the open project's
                 rotation, then auto. Checked directly against
                 server.effective_rotation with a stubbed project store, so
                 it holds before and after the project store lands.
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

PREVIEW_WIDTH = 240


def _free_port() -> int:
    """A port nothing is listening on, chosen by the kernel.

    Never 7431: that is the founder's live studio and this suite must not go
    anywhere near it.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    if port == 7431:                                   # pragma: no cover
        return _free_port()
    return port


def _get(url: str, timeout: float = 60.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read(), dict(r.headers)


def _post(url: str, payload: dict, timeout: float = 120.0):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), dict(r.headers)


class RotationServerTest(unittest.TestCase):
    """One server for the whole class: starting it costs a second."""

    proc = None
    data_dir = None
    base = ""
    clip = ""

    @classmethod
    def setUpClass(cls):
        cls.data_dir = tempfile.mkdtemp(prefix="studio-rotation-data-")
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

        clips = json.loads(_get(cls.base + "/clips")[0])["clips"]
        usable = [c for c in clips if not c.get("error") and c.get("autorotate")]
        if not usable:
            cls._stop()
            raise unittest.SkipTest("no clip in footage/ to test rotation on")
        # Prefer a clip that carries a quarter-turn display matrix, since that
        # is the case the whole setting exists for.
        tagged = [c for c in usable if abs(int(c.get("rotation") or 0)) in (90, 270)]
        cls.clip = (tagged or usable)[0]["name"]

    @classmethod
    def _stop(cls):
        if cls.proc is None:
            return
        # By the PID captured at spawn, never by a name pattern: a pattern
        # kill in this workspace has taken out a sibling server before.
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

    def _frame(self, extra: dict):
        body = {"clip": self.clip, "time": 0.5, "width": PREVIEW_WIDTH,
                "format": "raw", "config": {}}
        body.update(extra)
        data, headers = _post(self.base + "/frame", body)
        return data, headers.get("X-Frame-Size", "")

    def test_rotation_zero_and_legacy_autorotate_false_agree(self):
        by_rotation, size_a = self._frame({"rotation": "0"})
        by_flag, size_b = self._frame({"autorotate": False})
        self.assertEqual(size_a, size_b,
                         "rotation 0 and autorotate false must be one size")
        self.assertEqual(len(by_rotation), len(by_flag))
        self.assertEqual(by_rotation, by_flag,
                         "rotation 0 and autorotate false must be the same "
                         "picture, byte for byte")

    def test_rotation_auto_and_legacy_autorotate_true_agree(self):
        by_rotation, size_a = self._frame({"rotation": "auto"})
        by_flag, size_b = self._frame({"autorotate": True})
        self.assertEqual(size_a, size_b)
        self.assertEqual(by_rotation, by_flag)

    def test_no_rotation_field_at_all_is_still_auto(self):
        bare, size_a = self._frame({})
        auto, size_b = self._frame({"rotation": "auto"})
        self.assertEqual(size_a, size_b)
        self.assertEqual(bare, auto)

    def test_rotation_ninety_swaps_the_frame_dimensions(self):
        _zero, size_zero = self._frame({"rotation": "0"})
        _ninety, size_ninety = self._frame({"rotation": "90"})
        zw, zh = (int(v) for v in size_zero.split("x"))
        nw, nh = (int(v) for v in size_ninety.split("x"))
        self.assertNotEqual((zw, zh), (nw, nh),
                            "90 must not come back at the unrotated shape")
        # Both are the preview at PREVIEW_WIDTH of a source whose own aspect
        # is inverted between the two, so the portrait one is the taller one.
        self.assertGreater(nh / nw, zh / zw,
                           f"rotation 90 ({nw}x{nh}) should be taller relative "
                           f"to its width than rotation 0 ({zw}x{zh})")

    def test_rotation_one_eighty_keeps_the_shape_and_changes_the_picture(self):
        zero, size_zero = self._frame({"rotation": "0"})
        one80, size_180 = self._frame({"rotation": "180"})
        self.assertEqual(size_zero, size_180, "180 must not change the shape")
        self.assertNotEqual(zero, one80,
                            "180 must actually turn the picture over")

    def test_the_thumbnail_query_takes_both_forms(self):
        legacy, _ = _get(f"{self.base}/thumb?clip={self.clip}&t=0.5&w=96&rot=0")
        modern, _ = _get(f"{self.base}/thumb?clip={self.clip}"
                         f"&t=0.5&w=96&rotation=0")
        self.assertEqual(legacy, modern,
                         "the thumb strip's rot=0 and rotation=0 are one request")


PRECEDENCE_SCRIPT = r'''
import json, sys, types

sys.path.insert(0, sys.argv[1])          # studio/
import server as SRV

# Replace the project store with a stub AFTER server.py is imported, because
# server.py puts studio/ at the front of sys.path itself and imports the real
# module. Swapping it in sys.modules is what makes this test independent of
# whether a project happens to be open in anyone's database.
stub = types.ModuleType("projects")
stub.open_rotation = lambda uid: "180" if uid == 0 else None
sys.modules["projects"] = stub

out = {
    "explicit_beats_everything": SRV.effective_rotation(
        {"rotation": "270", "autorotate": True}, 0),
    "legacy_true_is_auto": SRV.effective_rotation({"autorotate": True}, 0),
    "legacy_false_is_zero": SRV.effective_rotation({"autorotate": False}, 0),
    "query_rot_one_is_auto": SRV.effective_rotation({"rot": "1"}, 0),
    "query_rot_zero_is_zero": SRV.effective_rotation({"rot": "0"}, 0),
    "project_when_nothing_asked": SRV.effective_rotation({}, 0),
    "auto_when_no_project": SRV.effective_rotation({}, 999),
    "auto_when_no_user": SRV.effective_rotation({}, None),
}

# And a store that blows up must not take a render down with it.
broken = types.ModuleType("projects")


def _boom(uid):
    raise RuntimeError("the project store is unreadable")


broken.open_rotation = _boom
sys.modules["projects"] = broken
out["auto_when_the_store_raises"] = SRV.effective_rotation({}, 0)

print(json.dumps(out))
'''

class EffectiveRotationPrecedenceTest(unittest.TestCase):
    """The fallback order, checked without a server and without a database.

    Run in a subprocess with a stubbed project store, so this holds whether
    or not studio/projects.py exists yet, whatever is open in it, and without
    ever touching the founder's database.
    """

    def test_the_fallback_order(self):
        tmp = tempfile.mkdtemp(prefix="studio-rotation-stub-")
        data = tempfile.mkdtemp(prefix="studio-rotation-data-")
        try:
            script = Path(tmp) / "precedence.py"
            script.write_text(PRECEDENCE_SCRIPT)
            env = dict(os.environ, STUDIO_DATA_DIR=data)
            r = subprocess.run([PYTHON, str(script), str(STUDIO)],
                               cwd=str(CONTENT), env=env,
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
            got = json.loads(r.stdout.strip().splitlines()[-1])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.rmtree(data, ignore_errors=True)

        self.assertEqual(got["explicit_beats_everything"], "270")
        self.assertEqual(got["legacy_true_is_auto"], "auto")
        self.assertEqual(got["legacy_false_is_zero"], "0")
        self.assertEqual(got["query_rot_one_is_auto"], "auto")
        self.assertEqual(got["query_rot_zero_is_zero"], "0")
        self.assertEqual(got["project_when_nothing_asked"], "180",
                         "with nothing in the request, the open project's "
                         "rotation is what a render must use")
        self.assertEqual(got["auto_when_no_project"], "auto")
        self.assertEqual(got["auto_when_no_user"], "auto")
        self.assertEqual(got["auto_when_the_store_raises"], "auto",
                         "a project store that cannot be read is a reason to "
                         "fall back to auto, never a reason to fail a render")


if __name__ == "__main__":
    unittest.main()
