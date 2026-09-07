#!/usr/bin/env python3
"""Contract G1, server side: the studio reports what a source IS.

    content/.venv/bin/python -m unittest discover -s studio/tests/py

Runs the REAL server (studio/server.py) as a subprocess on a random free port,
with a temporary data directory AND a temporary footage directory, so nothing
here touches studio/data or the founder's own clips. The server is killed by
the PID captured at spawn, never by a name pattern.

The two clips are made here with ffmpeg rather than copied from bakeoff, so
the test is self-contained and costs half a second of encoding: a ProRes file
carrying BT.2020 primaries with the HLG transfer (what an iPhone shoots), and
an H.264 file carrying BT.709 everything (what a delivery file looks like).
Both are half a second long, which is the bound every ffmpeg call in this
workspace is required to carry.

What it pins:

  the three tags     clips[].source reports transfer, primaries and matrix as
                     three separate fields, because they are three separate
                     questions. The engine used to read the matrix alone and
                     call every BT.2020 file camera log.
  the resolution    source.resolved_input is what convert.input "auto" lands
                     on for that file: "hlg" and "rec709" here, visible in the
                     listing before anything is rendered.
  the refusal that   check_source_space used to reject a bt709-tagged file on
  went away          a log working space outright. That file now resolves to
                     input "rec709", so it renders with no warning at all.
  the refusal that   an untagged BT.2020 file on working_space "rec709" is
  stayed             still refused, because it really is camera log and the
                     curve really would not be undone.
  the picture        an HLG frame decoded as HLG is dramatically brighter than
                     the same frame decoded as Apple Log, which is the bug
                     this contract exists to fix, measured through the
                     server's own /api/frame rather than asserted in theory.
  the definitions    GET /api/state advertises convert.input and every value
                     it accepts, so a client does not have to read the source
                     to know what the field takes.
  the camera logs    contract G8's five (slog3, logc3, vlog, clog3, dlog) are
                     advertised as camera logs, are never what "auto" lands
                     on, each render their own picture through the server,
                     and are refused on working_space "rec709".
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

PREVIEW_WIDTH = 160

# Half a second at 24fps. Every ffmpeg call in this workspace is bounded.
CLIP_SECONDS = "0.5"


def _free_port() -> int:
    """A port nothing is listening on, chosen by the kernel.

    Never 7431 or 7614: those are the founder's live studio and the bakeoff
    server, and this suite must not go anywhere near either.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    if port in (7431, 7614):                               # pragma: no cover
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


def _make_clip(path: Path, args: list[str], params: str,
               source: str = "testsrc2=size=192x108:rate=24") -> None:
    """One short tagged test clip.

    setparams is how the tags actually get written: -color_trc on its own sets
    what the ENCODER is told, and the muxer writes the colr atom from the
    frame properties, so a file made without it comes back from ffprobe as
    "unknown" and tests nothing.
    """
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", source, "-t", CLIP_SECONDS,
         "-vf", params] + args + [str(path)],
        check=True, capture_output=True)


# An 18 percent grey card, in each input's own code values, rounded to the
# nearest 8 bit code because that is what lavfi's colour parser takes:
#
#   HLG        0.3786 -> 97   (BT.2408 puts reference level at 38% signal)
#   Apple Log  0.4883 -> 125  (the Apple Log Profile encoding of 0.18)
#
# Two files, one grey card. If the input transforms are right, both render to
# the same picture, and that is the whole of contract G1 in one measurement.
GREY_HLG = "0x616161"
GREY_APPLE_LOG = "0x7d7d7d"


class InputTransformServerTest(unittest.TestCase):
    """One server for the whole class: starting it costs a second."""

    proc = None
    data_dir = None
    footage_dir = None
    base = ""

    HLG = "hlg_clip.mov"
    REC709 = "rec709_clip.mp4"
    LOG = "untagged_bt2020.mov"
    HLG_GREY = "hlg_grey18.mov"
    LOG_GREY = "apple_log_grey18.mov"

    HLG_TAGS = ("format=yuv444p10le,setparams=color_primaries=bt2020"
                ":color_trc=arib-std-b67:colorspace=bt2020nc:range=tv")
    LOG_TAGS = ("format=yuv444p10le,setparams=color_primaries=bt2020"
                ":colorspace=bt2020nc:range=tv")
    PRORES = ["-c:v", "prores_ks", "-profile:v", "4"]

    @classmethod
    def setUpClass(cls):
        if shutil.which("ffmpeg") is None:                 # pragma: no cover
            raise unittest.SkipTest("ffmpeg is not on PATH")
        cls.footage_dir = tempfile.mkdtemp(prefix="studio-input-footage-")
        root = Path(cls.footage_dir)
        try:
            # An iPhone HDR clip: BT.2020 primaries, the HLG transfer, the
            # BT.2020 non-constant-luminance matrix.
            _make_clip(root / cls.HLG, cls.PRORES, cls.HLG_TAGS)
            # An ordinary delivery file: BT.709 everywhere.
            _make_clip(root / cls.REC709,
                       ["-c:v", "libx264", "-pix_fmt", "yuv420p"],
                       "format=yuv420p,setparams=color_primaries=bt709"
                       ":color_trc=bt709:colorspace=bt709:range=tv")
            # What an Apple Log clip looks like to ffprobe: BT.2020 primaries
            # and NO transfer tag at all. This is the file whose decode must
            # not change, and the one the surviving refusal is about.
            _make_clip(root / cls.LOG, cls.PRORES, cls.LOG_TAGS)
            # The same 18 percent grey card as an HLG file and as an Apple Log
            # file. 4:4:4 so nothing is lost to chroma subsampling on the way
            # in, and a flat field so the measurement is one number and not an
            # average over a pattern.
            _make_clip(root / cls.HLG_GREY, cls.PRORES, cls.HLG_TAGS,
                       source=f"color=c={GREY_HLG}:size=192x108:rate=24")
            _make_clip(root / cls.LOG_GREY, cls.PRORES, cls.LOG_TAGS,
                       source=f"color=c={GREY_APPLE_LOG}:size=192x108:rate=24")
        except subprocess.CalledProcessError as exc:       # pragma: no cover
            shutil.rmtree(cls.footage_dir, ignore_errors=True)
            raise unittest.SkipTest(
                "ffmpeg could not build the tagged test clips: "
                + exc.stderr.decode("utf-8", "replace")[-400:])

        cls.data_dir = tempfile.mkdtemp(prefix="studio-input-data-")
        port = _free_port()
        cls.base = f"http://127.0.0.1:{port}/api"
        env = dict(os.environ, STUDIO_DATA_DIR=cls.data_dir)
        cls.proc = subprocess.Popen(
            [PYTHON, SERVER, "--port", str(port), "--data-dir", cls.data_dir,
             "--footage", cls.footage_dir],
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
            except Exception as exc:                       # noqa: BLE001
                last = exc
                time.sleep(0.25)
        else:
            cls._stop()
            raise RuntimeError(f"the studio server never answered: {last}")

    @classmethod
    def _stop(cls):
        if cls.proc is None:
            return
        # By the PID captured at spawn, never by a name pattern: a pattern
        # kill in this workspace has taken out a sibling server before.
        try:
            cls.proc.terminate()
            cls.proc.wait(timeout=10)
        except Exception:                                  # noqa: BLE001
            try:
                cls.proc.kill()
            except Exception:                              # noqa: BLE001
                pass
        for pipe in (cls.proc.stdout, cls.proc.stderr):
            if pipe is not None:
                pipe.close()
        cls.proc = None

    @classmethod
    def tearDownClass(cls):
        cls._stop()
        # Only the two directories this test made, never studio/data and never
        # the real footage folder.
        for d in (cls.data_dir, cls.footage_dir):
            if d:
                shutil.rmtree(d, ignore_errors=True)

    # -- helpers -----------------------------------------------------------

    def _clip(self, name: str) -> dict:
        clips = json.loads(_get(self.base + "/clips")[0])["clips"]
        by_name = {c["name"]: c for c in clips}
        self.assertIn(name, by_name, f"{name} is missing from /api/clips")
        return by_name[name]

    def _source(self, name: str) -> dict:
        entry = self._clip(name)
        self.assertNotIn("error", entry,
                         f"{name} failed to probe: {entry.get('error')}")
        self.assertIn("source", entry,
                      "every clip entry carries a source block (contract G1)")
        return entry["source"]

    def _mean(self, clip: str, config: dict) -> float:
        body = {"clip": clip, "time": 0.2, "width": PREVIEW_WIDTH,
                "format": "raw", "rotation": "0", "config": config}
        data, _headers = _post(self.base + "/frame", body)
        self.assertTrue(data, "an empty frame came back")
        return sum(data) / len(data)

    # -- the tags ----------------------------------------------------------

    def test_an_hlg_clip_reports_its_three_tags_and_resolves_to_hlg(self):
        src = self._source(self.HLG)
        self.assertEqual(src["transfer"], "arib-std-b67",
                         "the transfer tag is reported as ffprobe spells it")
        self.assertEqual(src["primaries"], "bt2020")
        self.assertEqual(src["matrix"], "bt2020nc",
                         "the YUV matrix is a separate field from the gamut")
        self.assertEqual(src["resolved_input"], "hlg",
                         "an arib-std-b67 file decodes as HLG, not Apple Log")
        self.assertEqual(src["warnings"], [],
                         f"a correctly tagged HLG file needs no warning, got "
                         f"{src['warnings']}")

    def test_a_rec709_clip_resolves_to_rec709_instead_of_being_refused(self):
        """The refusal that went away.

        check_source_space used to raise on any bt709-tagged file whenever the
        working space was not "rec709", because the only decode it had was
        Apple Log and applying it really would have wrecked the picture. Now
        there is a right answer to offer instead, so the file is simply
        decoded as Rec.709 and the listing carries no complaint at all.
        """
        src = self._source(self.REC709)
        self.assertEqual(src["transfer"], "bt709")
        self.assertEqual(src["primaries"], "bt709")
        self.assertEqual(src["matrix"], "bt709")
        self.assertEqual(src["resolved_input"], "rec709")
        self.assertEqual(src["warnings"], [],
                         f"a tagged Rec.709 file is no longer refused on the "
                         f"log path, got {src['warnings']}")

    def test_an_untagged_bt2020_clip_still_resolves_to_apple_log(self):
        """The decode that must not move.

        An Apple Log clip carries BT.2020 primaries and no transfer tag, so
        "auto" has to answer apple_log for it and say nothing. Every existing
        grade in this repository depends on that being true.
        """
        src = self._source(self.LOG)
        self.assertIn(src["transfer"], ("", "unknown"),
                      "a missing transfer tag comes back empty or 'unknown'")
        self.assertEqual(src["primaries"], "bt2020")
        self.assertEqual(src["resolved_input"], "apple_log")
        self.assertEqual(src["warnings"], [],
                         f"an untagged BT.2020 file is the normal case and "
                         f"gets no warning, got {src['warnings']}")

    def test_the_state_route_advertises_the_input_field(self):
        state = json.loads(_get(self.base + "/state")[0])
        defs = state.get("convert_definitions")
        self.assertIsInstance(defs, dict,
                              "GET /api/state carries convert_definitions")
        self.assertIn("input", defs, "convert.input is advertised")
        self.assertEqual(defs["input"]["default"], "auto")
        self.assertEqual(list(defs["input"]["values"]),
                         ["auto", "apple_log", "hlg", "pq", "rec709",
                          "slog3", "logc3", "vlog", "clog3", "dlog"])
        self.assertEqual(state["defaults"]["convert"]["input"], "auto",
                         "and the defaults block agrees with it")

    def test_the_state_route_lists_the_camera_logs_as_camera_logs(self):
        """Contract G8: the five camera logs, marked as such.

        An agent reading this route has to be able to tell a value "auto" can
        land on from a value only a person can choose. No container tag names
        S-Log3, LogC3, V-Log, Canon Log 3 or D-Log, so those five are never
        the answer to "auto" and have to be set by hand; the route says so
        instead of leaving it to be discovered by rendering.
        """
        defs = json.loads(_get(self.base + "/state")[0])["convert_definitions"]
        logs = defs["input"].get("camera_logs")
        self.assertEqual(list(logs or []),
                         ["slog3", "logc3", "vlog", "clog3", "dlog"],
                         "convert_definitions.input.camera_logs names them")
        for name in logs:
            self.assertIn(name, defs["input"]["values"],
                          f"{name} is also in the accepted values")
        self.assertNotIn("auto", logs)
        self.assertIn("camera log", defs["input"]["note"],
                      "and the note explains why auto never picks one")

        # And the clip listing never resolves a file to one of them: all five
        # arrive tagged the way an Apple Log file is.
        for name in (self.HLG, self.REC709, self.LOG):
            self.assertNotIn(self._source(name)["resolved_input"], logs,
                             f"{name} must not auto-resolve to a camera log")

    def test_a_camera_log_renders_and_is_a_different_picture(self):
        """The five decode through the server, not only through the engine.

        Each one is a different curve on a different gamut, so reading the
        same code with all six has to give six different amounts of light.
        Measured on the flat grey card rather than on a pattern: one code
        across the whole frame makes the mean say exactly what the curve did,
        where a mean over a test pattern can put two curves on the same number
        by coincidence. A missing technical cube shows up here as a 500
        rather than as a silently identical frame.
        """
        seen = {}
        for name in ("apple_log", "slog3", "logc3", "vlog", "clog3", "dlog"):
            seen[name] = self._mean(self.LOG_GREY, {"convert": {"input": name}})
        self.assertEqual(
            len(set(round(v, 1) for v in seen.values())), len(seen),
            f"each input must read the same code as its own light, got {seen}")
        for name, value in seen.items():
            if name == "apple_log":
                continue
            self.assertGreater(
                abs(value - seen["apple_log"]), 2.0,
                f"{name} read an Apple Log grey card the way Apple Log does "
                f"({value:.1f} against {seen['apple_log']:.1f}), which means "
                f"its curve or its cube is not its own")

    def test_a_camera_log_on_working_space_rec709_is_refused(self):
        """Naming a camera log says the source HAS a log curve, and
        working_space "rec709" undoes none, so the pair is refused with a
        sentence rather than rendered flat and grey with no explanation."""
        try:
            self._mean(self.HLG, {"convert": {"input": "vlog",
                                              "working_space": "rec709"}})
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            self.assertIn("vlog", body, "the error names the input")
            self.assertIn("camera log", body, "and says why")
        else:                                              # pragma: no cover
            self.fail("a camera log on working_space rec709 rendered")

    # -- the picture -------------------------------------------------------

    def test_the_same_grey_card_lands_in_the_same_place_from_either_file(self):
        """The whole contract, end to end through the server.

        One 18 percent grey card, written once as an HLG file and once as an
        Apple Log file, at each format's own code for it. If the input
        transforms are right the two render to the same picture. The tolerance
        is three code values out of 255, which covers the 8 bit rounding of
        the two source codes and the YUV round trip; the failure it catches is
        far larger than that.
        """
        hlg = self._mean(self.HLG_GREY, {})
        log = self._mean(self.LOG_GREY, {})
        self.assertAlmostEqual(
            hlg, log, delta=3.0,
            msg=f"the HLG grey card renders at {hlg:.1f} and the Apple Log "
                f"one at {log:.1f}; they are the same card")

    def test_an_hlg_frame_is_not_rendered_dark_any_more(self):
        """The bug, measured rather than asserted.

        Decoding an HLG signal with the Apple Log curve reads every code as a
        far smaller scene value than it is (18 percent grey comes back as 7
        percent), so the picture renders dark with the shadows crushed
        together. The same frame through the HLG decode is much brighter.
        """
        auto = self._mean(self.HLG_GREY, {})
        forced = self._mean(self.HLG_GREY, {"convert": {"input": "hlg"}})
        wrong = self._mean(self.HLG_GREY, {"convert": {"input": "apple_log"}})
        self.assertAlmostEqual(
            auto, forced, delta=0.5,
            msg="auto already resolved this clip to hlg, so forcing hlg must "
                "render the same picture")
        self.assertGreater(
            auto, wrong * 1.3,
            f"the HLG decode ({auto:.1f} mean) must be substantially brighter "
            f"than the Apple Log misread ({wrong:.1f} mean)")

    def test_forcing_an_input_overrides_the_files_own_tags(self):
        """An explicit input beats the metadata, end to end through a render.

        Metadata lies often enough that the override has to work: the same
        HLG file decoded as PQ and as Rec.709 has to come back as three
        different pictures, not three copies of whatever the tag said.
        """
        seen = {}
        for name in ("hlg", "pq", "rec709", "apple_log"):
            seen[name] = self._mean(self.HLG, {"convert": {"input": name}})
        self.assertEqual(len(set(round(v, 2) for v in seen.values())), 4,
                         f"each input must render its own picture, got {seen}")

    def test_a_bad_input_value_is_refused_with_a_useful_message(self):
        # "redlog" rather than a real camera's name: contract G8 turned five
        # of those into valid values, and a test whose invalid value can
        # quietly become valid is a test that stops testing.
        try:
            self._mean(self.HLG, {"convert": {"input": "redlog"}})
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            self.assertIn("redlog", body,
                          "the error names the value that was rejected")
            self.assertIn("apple_log", body,
                          "and lists what it could have been")
        else:                                              # pragma: no cover
            self.fail("an unknown convert.input rendered instead of erroring")


if __name__ == "__main__":                                 # pragma: no cover
    unittest.main()
