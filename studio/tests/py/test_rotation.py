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
  precedence     rotation, then the legacy flag, then a non-auto
                 config.rotation, then the open project's rotation, then
                 auto (contract G4 added the config step). Checked directly
                 against server.effective_rotation with a stubbed project
                 store, so it holds before and after the project store lands.
  in the grade   PresetAndGradeRotationTest: PUT /api/grade and POST
                 /api/preset persist rotation because it is part of the
                 config now, GET reads it back, an auto value never reaches
                 the preset file on disk (config_diff strips it against
                 DEFAULTS same as any other untouched default), and GET
                 /api/preset always carries `comment` (previously stripped)
                 plus `expanded: true` when asked for it.
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


def _put(url: str, payload: dict, timeout: float = 120.0):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="PUT")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), dict(r.headers)


def _get_json(url: str, timeout: float = 60.0) -> dict:
    return json.loads(_get(url, timeout)[0])


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

    def test_clips_carry_a_rotation_tag_and_a_suspect_flag(self):
        """Contract G4: GET /api/state's clip list (here read through the
        older GET /api/clips, which returns the same per-clip shape) gains
        rotation_tag (a string, "0" when the file has none) and
        rotation_tag_suspect (advisory, never applied automatically). This
        class's own footage is real ProRes off the founder's camera, so the
        tagged clip is expected to trip the heuristic's cinema-codec tell.

        Round 2 tooling item 17 adds a sibling rotation_tag_note: a plain
        sentence explaining rotation_tag_suspect's answer, so the boolean on
        its own never reads as a verdict. Empty exactly when the flag is
        false, non-empty (and naming the actual tag) when it is true.
        """
        clips = json.loads(_get(self.base + "/clips")[0])["clips"]
        mine = [c for c in clips if c["name"] == self.clip][0]
        self.assertIn("rotation_tag", mine)
        self.assertIsInstance(mine["rotation_tag"], str)
        self.assertIn("rotation_tag_suspect", mine)
        self.assertIsInstance(mine["rotation_tag_suspect"], bool)
        self.assertEqual(mine["rotation_tag"], str(int(mine["rotation"] or 0)),
                         "rotation_tag must describe the same tag the older "
                         "'rotation' field already reports")
        if abs(int(mine["rotation"] or 0)) in (90, 270) and mine.get("codec") == "prores":
            self.assertTrue(mine["rotation_tag_suspect"],
                            "a quarter turn tag on a ProRes file is exactly "
                            "the C011 case the heuristic exists for")
        self.assertIn("rotation_tag_note", mine)
        self.assertIsInstance(mine["rotation_tag_note"], str)
        if mine["rotation_tag_suspect"]:
            self.assertNotEqual(mine["rotation_tag_note"], "",
                                "a suspect tag must carry an explanation, "
                                "not just a bare boolean")
            self.assertIn(mine["rotation_tag"], mine["rotation_tag_note"],
                          "the note should name the actual tag it is about")
            self.assertIn("run orient and look", mine["rotation_tag_note"])
        else:
            self.assertEqual(mine["rotation_tag_note"], "",
                             "a note on a tag that is not flagged reads as "
                             "a reassurance, which is itself a verdict")


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
    # Contract G4: a non-auto config.rotation (the grade or preset the
    # request is already carrying whole) is the third place in line, after
    # an explicit rotation and the legacy flag, before the project.
    "config_beats_project": SRV.effective_rotation(
        {"config": {"rotation": "90"}}, 0),
    "config_auto_falls_through_to_project": SRV.effective_rotation(
        {"config": {"rotation": "auto"}}, 0),
    "config_missing_key_falls_through_to_project": SRV.effective_rotation(
        {"config": {}}, 0),
    "config_that_is_not_a_dict_is_ignored": SRV.effective_rotation(
        {"config": "not-a-dict"}, 0),
    "explicit_rotation_beats_config": SRV.effective_rotation(
        {"rotation": "0", "config": {"rotation": "90"}}, 0),
    "legacy_autorotate_beats_config": SRV.effective_rotation(
        {"autorotate": False, "config": {"rotation": "90"}}, 0),
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

        # Contract G4: config.rotation is the third place in line.
        self.assertEqual(got["config_beats_project"], "90",
                         "a non-auto config.rotation must win over the open "
                         "project's own rotation, or a grade posted whole "
                         "would silently lose to whatever this account had "
                         "the project set to")
        self.assertEqual(got["config_auto_falls_through_to_project"], "180",
                         "a config that never set a rotation (still auto "
                         "after full_config's own defaults merge) must not "
                         "shadow the project's rotation")
        self.assertEqual(got["config_missing_key_falls_through_to_project"], "180")
        self.assertEqual(got["config_that_is_not_a_dict_is_ignored"], "180",
                         "a malformed config must not raise or panic a render, "
                         "only fall through to the next source")
        self.assertEqual(got["explicit_rotation_beats_config"], "0",
                         "an explicit top level rotation still outranks the "
                         "one riding along inside config")
        self.assertEqual(got["legacy_autorotate_beats_config"], "0",
                         "the legacy autorotate flag still outranks config "
                         "too, exactly like an explicit rotation field")


class PresetAndGradeRotationTest(unittest.TestCase):
    """Contract G4: rotation persists because it is part of the config now.

    Its own server and its own temp data dir: these tests write real preset
    files to disk and read them back, and a fresh, empty presets folder is
    what makes "on_disk" mean what it says rather than whatever an earlier
    test in this file happened to leave behind.
    """

    proc = None
    data_dir = None
    base = ""
    clip = ""

    @classmethod
    def setUpClass(cls):
        cls.data_dir = tempfile.mkdtemp(prefix="studio-rotation-preset-data-")
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
        usable = [c for c in clips if not c.get("error")]
        if not usable:
            cls._stop()
            raise unittest.SkipTest("no clip in footage/ to grade")
        cls.clip = usable[0]["name"]

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
            shutil.rmtree(cls.data_dir, ignore_errors=True)

    def _preset_file(self, name: str) -> Path:
        return Path(self.data_dir) / "users" / "0" / "presets" / f"{name}.json"

    def test_preset_round_trip_of_rotation_and_comment(self):
        name = "rotation_roundtrip_test"
        saved, _ = _post(f"{self.base}/preset", {
            "name": name, "config": {"rotation": "90"},
            "comment": "sideways on purpose",
        })
        self.assertEqual(json.loads(saved)["saved"], f"{name}.json",
                         "the route reports the file it wrote, extension "
                         "included, not the bare preset name")

        body = _get_json(f"{self.base}/preset?name={name}")
        self.assertEqual(body["name"], name)
        self.assertEqual(body["comment"], "sideways on purpose",
                         "GET must return the comment POST wrote instead of "
                         "the _comment/comment mismatch it used to strip")
        self.assertEqual(body["config"]["rotation"], "90")
        self.assertNotIn("expanded", body,
                         "no expand=true means no expanded key: the shape "
                         "stays what it was before this contract, plus comment")

        expanded = _get_json(f"{self.base}/preset?name={name}&expand=true")
        self.assertTrue(expanded["expanded"])
        self.assertEqual(expanded["comment"], "sideways on purpose")
        self.assertEqual(expanded["config"]["rotation"], "90")
        # expand does not change what config already was: read_preset()
        # already fills in every default, expand or not.
        self.assertEqual(expanded["config"], body["config"])

    def test_preset_get_without_a_comment_reads_back_empty_not_missing(self):
        name = "rotation_no_comment_test"
        _post(f"{self.base}/preset", {"name": name, "config": {"rotation": "0"}})
        body = _get_json(f"{self.base}/preset?name={name}")
        self.assertEqual(body["comment"], "",
                         "comment is always present now, even when nothing "
                         "was ever written")

    def test_an_auto_rotation_is_not_written_to_the_preset_file(self):
        """config_diff strips every key that still matches DEFAULTS; once
        rotation is a DEFAULTS key, "auto" is stripped the same way an
        untouched contrast or saturation always was, and a real value
        survives for free. This is the "verify that" in contract G4."""
        name = "rotation_auto_not_written_test"
        _post(f"{self.base}/preset", {
            "name": name,
            "config": {"rotation": "auto", "primaries": {"contrast": 1.2}},
        })
        on_disk = json.loads(self._preset_file(name).read_text())
        self.assertNotIn("rotation", on_disk,
                         "an auto rotation must not be written, same as any "
                         "other untouched default")
        self.assertEqual(on_disk.get("primaries", {}).get("contrast"), 1.2,
                         "a real change alongside it must still be written")
        # GET still answers "auto": full_config fills in the default that
        # was correctly never written to disk.
        body = _get_json(f"{self.base}/preset?name={name}")
        self.assertEqual(body["config"]["rotation"], "auto")

    def test_a_non_auto_rotation_is_written_to_the_preset_file(self):
        name = "rotation_non_auto_written_test"
        _post(f"{self.base}/preset", {"name": name, "config": {"rotation": "270"}})
        on_disk = json.loads(self._preset_file(name).read_text())
        self.assertEqual(on_disk.get("rotation"), "270",
                         "a real rotation must survive config_diff, same as "
                         "any other real, non-default change")

    def test_grade_round_trip_of_rotation_and_a_message_in_the_config(self):
        """PUT /api/grade and GET /api/grade round trip rotation because it
        is part of the config now, and a config that carries its own
        _comment (a note some caller attached to the grade itself, distinct
        from the commit `message` field PUT already takes) survives the
        same round trip instead of being silently dropped."""
        saved, _ = _put(f"{self.base}/grade", {
            "clip": self.clip,
            "config": {"rotation": "180", "_comment": "graded for the reel"},
            "by": "test",
        })
        self.assertIn("key", json.loads(saved))

        got = _get_json(f"{self.base}/grade?clip={self.clip}")
        self.assertTrue(got["exists"])
        self.assertEqual(got["config"]["rotation"], "180")
        self.assertEqual(got["config"].get("_comment"), "graded for the reel",
                         "a message riding along in the config must survive "
                         "a PUT and GET round trip")


if __name__ == "__main__":
    unittest.main()
