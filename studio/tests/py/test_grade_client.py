#!/usr/bin/env python3
"""Contract G5: `studio/tools/grade_client.py`, the first party client module.

    .venv/bin/python -m unittest discover -s studio/tests/py -p 'test_*.py'

Runs the REAL server (studio/server.py) as a subprocess on a random free
port, with its own temporary --data-dir and a --footage directory of
SYMLINKS to the real clips in content/footage (never copies: a clip's
identity is a hash of its own bytes, so the tests have to open the very
same files). The server is stopped by the process id captured at spawn in
tearDownClass, never by a name pattern, and never runs anywhere near port
7431 or 7614.

The pure functions (brief, bands, diff) need no server at all and are
tested first, on synthetic dicts.

Every route grade_client codes to (contract G2: health, agent/attach
headers, if_rev/409; contract G3: bands, ref/times/path on stats, path on
frame, name/out_dir/recommended on match; contract G4: rotation, comment/
expand on preset, clips[].rotation_tag/rotation_tag_suspect/source on
state) has landed on the server this file starts, and every test below
asserts on it directly rather than skipping. This file used to wrap those
calls in try/except and skip with self.skipTest naming the contract line
while the other lanes were still building them; that wrapping is gone now
that the routes are real, not a style choice.
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
from pathlib import Path

HERE = Path(__file__).resolve().parent
STUDIO = HERE.parent.parent
CONTENT = STUDIO.parent
PYTHON = str(CONTENT / ".venv" / "bin" / "python")
SERVER = str(STUDIO / "server.py")
FOOTAGE_SRC = CONTENT / "footage"
REFS_SRC = CONTENT / "refs"

sys.path.insert(0, str(STUDIO / "tools"))

from grade_client import (                                    # noqa: E402
    Studio, StudioError, DEFAULT_BASE, brief, bands, diff, decode, measure,
    contact_sheet, request as raw_request,
)


def _free_port() -> int:
    """A port nothing is listening on, chosen by the kernel. Never 7431 or
    7614: those are real, running servers this suite must not go near."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    if port in (7431, 7614):                          # pragma: no cover
        return _free_port()
    return port


def _make_footage_dir() -> Path:
    """A temp folder of SYMLINKS to content/footage, the pattern
    studio/tests/run.mjs and studio/README.md both document: --footage
    isolates a test run's uploads and registrations from the founder's
    real footage folder, but the files themselves have to be the real
    ones, by content hash, or nothing in them matches a real clip."""
    tmp = Path(tempfile.mkdtemp(prefix="grade-client-test-footage-"))
    if FOOTAGE_SRC.is_dir():
        for p in FOOTAGE_SRC.iterdir():
            if p.is_file() and not p.name.startswith("."):
                (tmp / p.name).symlink_to(p)
    return tmp


class _ServerCase(unittest.TestCase):
    """One real server per test class: starting it costs a couple of
    seconds, so sharing it across the tests in one class keeps the whole
    file fast without ever touching the founder's real server or data."""

    proc = None
    data_dir = None
    footage_dir = None
    base = ""
    clip = ""

    @classmethod
    def setUpClass(cls):
        cls.data_dir = tempfile.mkdtemp(prefix="grade-client-test-data-")
        cls.footage_dir = _make_footage_dir()
        port = _free_port()
        cls.base = f"http://127.0.0.1:{port}"
        cls.proc = subprocess.Popen(
            [PYTHON, SERVER, "--port", str(port), "--data-dir", cls.data_dir,
             "--footage", str(cls.footage_dir)],
            cwd=str(CONTENT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True)
        deadline = time.time() + 40.0
        last_exc = None
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                out = cls.proc.stdout.read() if cls.proc.stdout else ""
                raise RuntimeError(
                    "the studio server exited before it was ready:\n" + out[-2000:])
            try:
                Studio(base=cls.base).state()
                break
            except Exception as exc:                          # noqa: BLE001
                last_exc = exc
                time.sleep(0.25)
        else:
            cls._stop()
            raise RuntimeError(f"the studio server never answered: {last_exc}")

        state = Studio(base=cls.base).state()
        clips = [c["name"] for c in state.get("clips", []) if not c.get("error")]
        if not clips:
            cls._stop()
            raise unittest.SkipTest("no usable clip in footage/ for this suite")
        cls.clip = clips[0]

    @classmethod
    def _stop(cls):
        """By the process id captured at spawn, and nothing else: a
        pattern kill in this workspace has taken out a sibling server
        before (see MEMORY: lanes-kill-by-pid-only)."""
        if cls.proc is None:
            return
        try:
            cls.proc.terminate()
            cls.proc.wait(timeout=10)
        except Exception:                                     # noqa: BLE001
            try:
                cls.proc.kill()
                cls.proc.wait(timeout=10)
            except Exception:                                 # noqa: BLE001
                pass
        if cls.proc.stdout is not None:
            cls.proc.stdout.close()
        cls.proc = None

    @classmethod
    def tearDownClass(cls):
        cls._stop()
        if cls.data_dir:
            shutil.rmtree(cls.data_dir, ignore_errors=True)
        if cls.footage_dir:
            # rmtree on a folder of symlinks removes the links only, never
            # the real files in content/footage they point at.
            shutil.rmtree(cls.footage_dir, ignore_errors=True)

    def studio(self, **kw) -> Studio:
        return Studio(base=self.base, **kw)


# --------------------------------------------------------------------------
# pure functions: no server needed
# --------------------------------------------------------------------------

class BriefBandsDiff(unittest.TestCase):
    STATS = {
        "luma": {"p5": 0.05, "p25": 0.12, "p50": 0.24, "p75": 0.51, "p95": 0.88},
        "saturation": {"mean": 0.32},
        "channels": {"r": 0.41, "g": 0.38, "b": 0.35},
    }

    def test_brief_is_one_line_with_every_field(self):
        line = brief(self.STATS)
        self.assertNotIn("\n", line)
        for token in ("p5=", "p25=", "p50=", "p75=", "p95=", "sat=", "r=",
                     "g=", "b="):
            self.assertIn(token, line)

    def test_brief_appends_the_clip_name_when_given(self):
        self.assertIn("clip=A001.MOV", brief(self.STATS, clip="A001.MOV"))
        self.assertNotIn("clip=", brief(self.STATS))

    def test_brief_unwraps_a_full_route_envelope(self):
        envelope = {"key": "abc123", "stats": self.STATS, "size": [640, 360]}
        self.assertEqual(brief(envelope), brief(self.STATS))

    def test_brief_does_not_crash_on_missing_fields(self):
        line = brief({})
        self.assertIn("n/a", line)

    def test_bands_reports_a_note_when_the_block_is_absent(self):
        rows = bands(self.STATS)
        self.assertEqual(len(rows), 1)
        self.assertIn("no bands block", rows[0])

    def test_bands_prints_one_row_per_band_when_present(self):
        s = dict(self.STATS, bands={
            "edges": [0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875, 1.0],
            "saturation": [0.1] * 8, "warm": [0.0] * 8, "tint": [0.0] * 8,
            "count": [100] * 8,
        })
        rows = bands(s)
        self.assertEqual(len(rows), 8)
        self.assertIn("band 0", rows[0])
        self.assertIn("0.000-0.125", rows[0])

    def test_diff_is_signed_b_minus_a(self):
        a = self.STATS
        b = {"luma": {"p50": 0.30}, "saturation": {"mean": 0.20},
            "channels": {"r": 0.41}}
        d = diff(a, b)
        self.assertAlmostEqual(d["luma"]["p50"], 0.06, places=6)
        self.assertAlmostEqual(d["saturation"]["mean"], -0.12, places=6)
        # a key both sides share and agree on is still reported, at zero
        self.assertAlmostEqual(d["channels"]["r"], 0.0, places=6)
        # a key only one side has never appears
        self.assertNotIn("g", d.get("channels", {}))

    def test_diff_walks_a_bands_list_element_by_element(self):
        a = {"bands": {"saturation": [0.1, 0.2, 0.3]}}
        b = {"bands": {"saturation": [0.1, 0.3, 0.1]}}
        d = diff(a, b)
        self.assertEqual(d["bands"]["saturation"], [0.0, 0.1, -0.2])

    def test_diff_unwraps_full_route_envelopes_on_both_sides(self):
        a = {"stats": {"luma": {"p50": 0.2}}}
        b = {"stats": {"luma": {"p50": 0.5}}}
        self.assertAlmostEqual(diff(a, b)["luma"]["p50"], 0.3, places=6)

    def test_diff_skips_definitions_and_bands_edges(self):
        """Contract G9 friction 4: `definitions` (sat_floor, the clip
        codes, the hue family boundaries) and `bands.edges` (the luma band
        boundaries) are measurement constants, always identical between two
        frames the same server measured, so a numeric diff of either is
        always zero and buries the real deltas in noise. Both are dropped
        rather than left for a caller to filter out by hand."""
        a = {
            "definitions": {"sat_floor": 0.02, "hue_families": ["warm", "cool"]},
            "bands": {"edges": [0.0, 0.5, 1.0], "saturation": [0.1, 0.2]},
            "luma": {"p50": 0.20},
        }
        b = {
            "definitions": {"sat_floor": 0.02, "hue_families": ["warm", "cool"]},
            "bands": {"edges": [0.0, 0.5, 1.0], "saturation": [0.1, 0.4]},
            "luma": {"p50": 0.30},
        }
        d = diff(a, b)
        self.assertNotIn("definitions", d)
        self.assertNotIn("edges", d.get("bands", {}))
        self.assertAlmostEqual(d["luma"]["p50"], 0.10, places=6)
        self.assertAlmostEqual(d["bands"]["saturation"][1], 0.2, places=6)


# _set_dotted is private; its whole job is visible through sweep's deep
# copy and dotted-path behaviour, tested in
# ServerBacked.test_sweep_deep_copies_and_sets_the_dotted_path below.

# --------------------------------------------------------------------------
# Studio(): base/agent resolution, no server or socket needed
# --------------------------------------------------------------------------

class StudioEnvPrecedence(unittest.TestCase):
    """Studio() already read STUDIO_AGENT this way before this lane; this
    class pins that STUDIO_URL is read the same way (constructor argument
    wins, then the environment variable, then DEFAULT_BASE), the other half
    of cinegrade.py's own --url/--port/STUDIO_URL/STUDIO_PORT precedence
    (contract G9 friction 8)."""

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None)
                       for k in ("STUDIO_URL", "STUDIO_AGENT")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_default_base_with_nothing_set(self):
        self.assertEqual(Studio().base, DEFAULT_BASE)

    def test_studio_url_env_is_read_with_no_base_argument(self):
        os.environ["STUDIO_URL"] = "http://127.0.0.1:55555"
        self.assertEqual(Studio().base, "http://127.0.0.1:55555")

    def test_base_argument_wins_over_studio_url_env(self):
        os.environ["STUDIO_URL"] = "http://127.0.0.1:55555"
        self.assertEqual(Studio(base="http://127.0.0.1:55556").base,
                         "http://127.0.0.1:55556")

    def test_studio_agent_env_is_read_with_no_agent_argument(self):
        os.environ["STUDIO_AGENT"] = "agent:colorbot"
        self.assertEqual(Studio().agent, "agent:colorbot")

    def test_agent_argument_wins_over_studio_agent_env(self):
        os.environ["STUDIO_AGENT"] = "agent:colorbot"
        self.assertEqual(Studio(agent="agent:override").agent, "agent:override")

# --------------------------------------------------------------------------
# decode(): no server needed, just ffmpeg and a real file
# --------------------------------------------------------------------------

class Decode(unittest.TestCase):
    def test_decode_a_png_returns_an_rgb24_array(self):
        pngs = sorted(REFS_SRC.glob("*.PNG")) or sorted(REFS_SRC.glob("*.png"))
        if not pngs:
            self.skipTest("no PNG in content/refs to decode")
        arr = decode(pngs[0], width=200)
        self.assertEqual(arr.dtype.name, "uint8")
        self.assertEqual(arr.ndim, 3)
        self.assertEqual(arr.shape[2], 3)
        self.assertEqual(arr.shape[1], 200)
        self.assertGreater(arr.shape[0], 0)

    def test_decode_accepts_raw_bytes_too(self):
        pngs = sorted(REFS_SRC.glob("*.PNG")) or sorted(REFS_SRC.glob("*.png"))
        if not pngs:
            self.skipTest("no PNG in content/refs to decode")
        data = pngs[0].read_bytes()
        arr = decode(data, width=100)
        self.assertEqual(arr.shape[1], 100)

    def test_decode_refuses_a_missing_file(self):
        with self.assertRaises(StudioError):
            decode("/no/such/file/anywhere.png")


# --------------------------------------------------------------------------
# contact_sheet(): no server needed, just ffmpeg and real files
# --------------------------------------------------------------------------

class ContactSheet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        clips = sorted(FOOTAGE_SRC.iterdir()) if FOOTAGE_SRC.is_dir() else []
        clips = [p for p in clips if p.is_file() and not p.name.startswith(".")]
        pngs = sorted(REFS_SRC.glob("*.PNG")) or sorted(REFS_SRC.glob("*.png"))
        if not clips or len(pngs) < 2:
            raise unittest.SkipTest(
                "need at least one clip and two ref images for a mixed "
                "aspect contact sheet")
        # One landscape video and two portrait stills: genuinely mixed
        # aspect ratios, the case hstack used to fail on.
        cls.inputs = [clips[0], pngs[0], pngs[1]]
        cls.tmp = Path(tempfile.mkdtemp(prefix="grade-client-test-sheet-"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _dims(self, path: Path) -> tuple:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30)
        streams = json.loads(proc.stdout)["streams"]
        return int(streams[0]["width"]), int(streams[0]["height"])

    def test_mixed_aspect_without_labels(self):
        out = self.tmp / "sheet_nolabel.jpg"
        result = contact_sheet(self.inputs, out, height=120)
        self.assertEqual(result, out)
        self.assertTrue(out.exists())
        w, h = self._dims(out)
        self.assertEqual(h, 120)
        self.assertGreater(w, 0)

    def test_mixed_aspect_with_labels(self):
        out = self.tmp / "sheet_label.jpg"
        contact_sheet(self.inputs, out, labels=["a", "b", "c"], height=120)
        self.assertTrue(out.exists())
        w, h = self._dims(out)
        # A label strip is added above every panel, so the labelled sheet
        # is taller than the unlabelled one by exactly that strip height.
        self.assertGreater(h, 120)

    def test_a_grid_pads_a_short_row_instead_of_failing(self):
        out = self.tmp / "sheet_grid.jpg"
        contact_sheet(self.inputs, out, height=100, grid="2x2")
        self.assertTrue(out.exists())
        w, h = self._dims(out)
        # two rows (2 then 1 panel), each row height ~100, stacked
        self.assertGreater(h, 100)

    def test_labels_must_match_the_input_count(self):
        with self.assertRaises(StudioError):
            contact_sheet(self.inputs, self.tmp / "bad.jpg", labels=["only-one"])


# --------------------------------------------------------------------------
# server backed: routes that exist today
# --------------------------------------------------------------------------

class ServerBacked(_ServerCase):
    def test_state_lists_the_clip_and_a_caller_block(self):
        state = self.studio().state()
        self.assertIn(self.clip, [c["name"] for c in state["clips"]])
        self.assertIn("caller", state)

    def test_the_module_level_request_function_works_standalone(self):
        """The free function agent_grade.py imports and calls directly,
        with no Studio object at all: `request(method, base, path, ...)`,
        the same shape it called before this module existed."""
        state = raw_request("GET", self.base, "/api/state")
        self.assertIn(self.clip, [c["name"] for c in state["clips"]])
        with self.assertRaises(StudioError):
            raw_request("GET", self.base, "/api/no-such-route")

    def test_frame_returns_bytes(self):
        data = self.studio().frame(clip=self.clip, time=0.5, config={}, width=160)
        self.assertIsInstance(data, (bytes, bytearray))
        self.assertGreater(len(data), 0)
        # a JPEG, not raw pixels, since format was not asked for
        self.assertEqual(data[:2], b"\xff\xd8")

    def test_frame_writes_to_a_file_when_out_is_given(self):
        out = Path(tempfile.mkdtemp(prefix="grade-client-test-out-")) / "f.jpg"
        try:
            result = self.studio().frame(clip=self.clip, time=0.5, config={},
                                         width=160, out=out)
            self.assertEqual(result, out)
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 0)
        finally:
            shutil.rmtree(out.parent, ignore_errors=True)

    def test_stats_returns_the_measurement_shape(self):
        result = self.studio().stats(clip=self.clip, time=0.5, config={}, width=320)
        self.assertIn("stats", result)
        self.assertIn("key", result)
        self.assertIn("luma", result["stats"])
        self.assertIn("p50", result["stats"]["luma"])

    def test_session_get_reports_a_revision(self):
        session = self.studio().session_get()
        self.assertIn("rev", session)

    def test_preset_save_and_load_round_trip(self):
        s = self.studio()
        name = "grade_client_test_preset"
        cfg = {"primaries": {"saturation": 1.23}}
        saved = s.preset_save(name, cfg, comment="grade_client test comment")
        self.assertTrue(saved.get("saved"))
        loaded = s.preset_load(name, expand=True)
        # contract G4, landed: {"name", "comment", "config", "expanded"}.
        # "config" is always the fully defaults filled config, expand or
        # not; "expanded" is the literal boolean, never a second copy of
        # the config under that key.
        self.assertEqual(loaded["config"]["primaries"]["saturation"], 1.23)
        self.assertIn("convert", loaded["config"])
        self.assertEqual(loaded["comment"], "grade_client test comment")
        self.assertIs(loaded["expanded"], True)

    def test_a_bad_clip_name_raises_studio_error_with_the_server_message(self):
        with self.assertRaises(StudioError) as ctx:
            self.studio().stats(clip="not-a-real-clip.mov", time=0, config={})
        exc = ctx.exception
        self.assertEqual(exc.status, 400)
        self.assertIn("not-a-real-clip.mov", exc.message)
        self.assertIn("HTTP 400", str(exc))

    def _raw_frame(self, **body_extra) -> tuple:
        """The one place this file reaches past Studio for response headers:
        Studio.frame() deliberately only returns bytes (or a Path), so a
        test that needs X-Frame-Size (to reshape a raw frame) or
        X-Frame-Key (proof of what the server actually resolved the request
        to, cache key and all) goes straight through urllib, the same
        transport grade_client.request() itself uses underneath. Returns
        (bytes, size_header, key_header)."""
        import urllib.request as _ur
        body = {"clip": self.clip, "time": 0.5, "width": 200, "config": {},
                "format": "raw"}
        body.update(body_extra)
        req = _ur.Request(
            self.base + "/api/frame", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with _ur.urlopen(req, timeout=30) as resp:
            return (resp.read(), resp.headers.get("X-Frame-Size"),
                    resp.headers.get("X-Frame-Key"))

    def test_measure_matches_the_servers_own_stats_on_the_identical_frame(self):
        """decode()+measure() on the exact bytes /api/frame?format=raw sends
        equals /api/stats's own "stats" for the same clip/time/width/config:
        same cache key, same array, same frame_stats() function either way.
        """
        import numpy as np
        raw, size, _key = self._raw_frame()
        w, h = (int(x) for x in size.split("x"))
        arr = np.frombuffer(raw, dtype=np.uint8)[:w * h * 3].reshape((h, w, 3))
        local = measure(arr)

        server = self.studio().stats(clip=self.clip, time=0.5, config={},
                                     width=200)["stats"]
        self.assertEqual(json.dumps(local, sort_keys=True),
                         json.dumps(server, sort_keys=True))

    def test_sweep_deep_copies_and_sets_the_dotted_path(self):
        s = self.studio()
        base_cfg = {"primaries": {"saturation": 1.0}}
        rows = s.sweep(self.clip, 0.5, base_cfg, "primaries.saturation",
                       [0.5, 1.6], width=200)
        self.assertEqual(len(rows), 2)
        values = [v for v, _ in rows]
        self.assertEqual(values, [0.5, 1.6])
        sats = [st["saturation"]["mean"] for _, st in rows]
        # a real, monotonic difference: the higher saturation value measures
        # higher mean saturation on the rendered frame
        self.assertLess(sats[0], sats[1])
        # the caller's own dict was never mutated by any of it
        self.assertEqual(base_cfg["primaries"]["saturation"], 1.0)

    @staticmethod
    def _preview_dims(source_w: int, source_h: int, requested_width: int) -> tuple:
        """The exact width/height studio/server.py's own `_preview_dims`
        computes: the preview width clamped to [160, source width], the
        even height that keeps the source's aspect. Reproduced here (not
        imported) so this test proves the server's actual number against an
        independently worked out expectation, not against itself."""
        w = max(160, min(int(requested_width), int(source_w)))
        h = max(2, int(round(source_h * w / source_w / 2)) * 2)
        return w, h

    def test_rotation_is_sent_on_every_call_once_set(self):
        """Studio(rotation=...) forwards it on every frame/stats/match call
        without the caller repeating it.

        Compares two EXPLICIT values, "0" against "90", on the same clip,
        rather than "whatever this clip's own tag already resolves to"
        against "90": a clip whose file tag already turns it to portrait
        makes "auto" and "90" agree by coincidence, which is exactly the
        false pass a bare picture size comparison can produce.

        A real 90 degree turn does not simply swap the two numbers a
        caller sees, because the preview width is independently reclamped
        for whichever orientation is being rendered (a landscape source and
        its own 90 degree turn both scale down to the SAME requested
        width here, since both exceed it); the two expected shapes are
        instead worked out from the clip's own raw, pre-rotation size
        (GET /api/state's "raw" block) through the exact formula
        studio/server.py's `_preview_dims` uses, and asserted precisely.
        Proven two ways: the real, worked out pixel dimensions, and the
        server's own X-Frame-Key, which folds the resolved rotation into
        its cache hash (render_raw's key includes "rot"), so two different
        keys is proof the field actually reached and was used by the
        server, not an inference from the JPEG's dimensions alone.
        """
        state = self.studio().state()
        clip_entry = next(c for c in state["clips"] if c["name"] == self.clip)
        raw_w, raw_h = clip_entry["raw"]["width"], clip_entry["raw"]["height"]
        want_plain = self._preview_dims(raw_w, raw_h, 160)
        # a genuine 90 degree turn swaps the SOURCE dimensions themselves
        # before the preview width is clamped, not the previous call's
        # already-clamped output
        want_rotated = self._preview_dims(raw_h, raw_w, 160)

        _, plain_size, plain_key = self._raw_frame(width=160, rotation="0")
        _, rotated_size, rotated_key = self._raw_frame(width=160, rotation="90")
        self.assertNotEqual(plain_key, rotated_key)

        plain_w, plain_h = (int(x) for x in plain_size.split("x"))
        rot_w, rot_h = (int(x) for x in rotated_size.split("x"))
        self.assertEqual((plain_w, plain_h), want_plain)
        self.assertEqual((rot_w, rot_h), want_rotated)
        self.assertNotEqual((plain_w, plain_h), (rot_w, rot_h))

        # and through the actual public Studio object, with rotation set
        # once on construction rather than repeated on every call: the
        # bytes that come back have to be the rotated frame's own size.
        rotated_studio = self.studio(rotation="90")
        data = rotated_studio.frame(clip=self.clip, time=0.5, config={},
                                    width=160, format="raw")
        self.assertEqual(len(data), rot_w * rot_h * 3)


# --------------------------------------------------------------------------
# server backed: routes that already exist AND landed while this lane ran
# (verified live against a real server; not assumed from the plan doc)
# --------------------------------------------------------------------------

class IdentityAndConcurrency(_ServerCase):
    def test_distinct_agents_get_distinct_caller_identities(self):
        a = self.studio(agent="grade-client-test-a")
        b = self.studio(agent="grade-client-test-b")
        ca = a.state()["caller"]
        cb = b.state()["caller"]
        self.assertNotEqual(ca["id"], cb["id"])
        self.assertIn("grade-client-test-a", ca["name"])
        self.assertIn("grade-client-test-b", cb["name"])

    def test_if_rev_mismatch_is_refused_with_409(self):
        s = self.studio(agent="grade-client-test-ifrev")
        session = s.session_get()
        rev = session.get("rev", 0)
        with self.assertRaises(StudioError) as ctx:
            s.session_patch({"primaries": {"contrast": 1.01}},
                            if_rev=rev + 999, clip=self.clip,
                            by="grade-client-test-ifrev")
        self.assertEqual(ctx.exception.status, 409)
        # a real if_rev match is accepted
        ok = s.session_patch({"primaries": {"contrast": 1.01}}, if_rev=rev,
                             clip=self.clip, by="grade-client-test-ifrev")
        self.assertIn("rev", ok)


# --------------------------------------------------------------------------
# whoami and project: lane G10's own additions to this module
# --------------------------------------------------------------------------

class ProjectAndWhoami(_ServerCase):
    def test_project_open_returns_a_top_level_project_dict(self):
        """Contract G9 friction 1: the first agent to call this route
        without this module guessed `["project"]["clip"]` and got a
        KeyError. The clip name is under `"name"`, at the top level."""
        s = self.studio(agent="grade-client-test-open")
        out = s.project_open(self.clip)
        self.assertIs(out.get("open"), True)
        self.assertEqual(out.get("name"), self.clip)
        self.assertIn("key", out)
        self.assertNotIn("project", out)

    def test_project_reads_the_open_project_back(self):
        s = self.studio(agent="grade-client-test-show")
        opened = s.project_open(self.clip)
        shown = s.project()
        self.assertIs(shown.get("open"), True)
        self.assertEqual(shown.get("name"), self.clip)
        self.assertEqual(shown.get("key"), opened.get("key"))

    def test_project_reports_closed_before_anything_is_open(self):
        s = self.studio(agent="grade-client-test-nothing-open")
        shown = s.project()
        self.assertIs(shown.get("open"), False)

    def test_project_open_with_rotation_sets_it_in_the_same_call(self):
        s = self.studio(agent="grade-client-test-open-rotation")
        out = s.project_open(self.clip, rotation="90")
        self.assertEqual(out.get("rotation"), "90")

    def test_whoami_reports_caller_and_project(self):
        s = self.studio(agent="grade-client-test-whoami")
        before = s.whoami()
        self.assertIn("caller", before)
        self.assertIn("grade-client-test-whoami", before["caller"]["name"])
        self.assertIsNone(before.get("project"))

        opened = s.project_open(self.clip)
        after = s.whoami()
        # whoami's own "project" is the content key (a hash), the same
        # value project_open/project return under "key", not the clip name:
        # a caller wanting the name calls project() (or reads project_open's
        # own return), which is exactly what this module's methods are for.
        self.assertEqual(after.get("project"), opened.get("key"))


# --------------------------------------------------------------------------
# contract G3/G4 routes: landed. These used to wrap each call and skip
# with self.skipTest naming the contract line while another lane was still
# building the route; every one of those routes is real now, so every test
# here asserts directly instead.
# --------------------------------------------------------------------------

class ContractRoutesLanded(_ServerCase):
    def test_health_route(self):
        health = self.studio().health()
        self.assertIn("ok", health)
        self.assertIn("clips", health)
        self.assertIn("ffmpeg_slots_free", health)

    def test_ref_stats_measures_a_reference_image(self):
        refs = self.studio().state().get("refs") or []
        if not refs:
            self.skipTest("no reference image registered on this server")
        result = self.studio().ref_stats(refs[0]["name"])
        self.assertIn("stats", result)
        self.assertIn("luma", result["stats"])

    def test_stats_at_measures_several_times_in_one_call(self):
        out = self.studio().stats_at(self.clip, [0.2, 0.8], config={},
                                     width=200)
        # stats_at returns the route's own envelope unchanged (round 2
        # tooling note 2), not the bare "results" list: a caller that reads
        # out["results"] is reading exactly what POST /api/stats answered.
        self.assertEqual(set(out), {"results"})
        results = out["results"]
        self.assertEqual(len(results), 2)
        self.assertIn("time", results[0])
        self.assertIn("stats", results[0])

    def test_frame_path_reads_a_file_outside_footage(self):
        pngs = sorted(REFS_SRC.glob("*.PNG")) or sorted(REFS_SRC.glob("*.png"))
        if not pngs:
            self.skipTest("no PNG in content/refs to read by path")
        data = self.studio().frame(path=str(pngs[0].resolve()), time=0,
                                   config={}, width=160)
        self.assertGreater(len(data), 0)

    def test_stats_path_reads_a_file_outside_footage(self):
        pngs = sorted(REFS_SRC.glob("*.PNG")) or sorted(REFS_SRC.glob("*.png"))
        if not pngs:
            self.skipTest("no PNG in content/refs to read by path")
        result = self.studio().stats(path=str(pngs[0].resolve()),
                                     config={}, width=160)
        self.assertIn("stats", result)

    def test_match_name_and_out_dir_are_forwarded(self):
        refs = self.studio().state().get("refs") or []
        if not refs:
            self.skipTest("no reference image registered on this server")
        # Under this test's own temp dir, never grade/luts/looks: the
        # server creates out_dir if it does not exist, so nothing is left
        # behind in the shared looks folder for this call.
        out_dir = tempfile.mkdtemp(prefix="grade-client-test-match-")
        try:
            wanted_name = "grade_client_test_match_name"
            result = self.studio().match(ref=refs[0]["name"], clip=self.clip,
                                         time=0.5, config={},
                                         name=wanted_name, out_dir=out_dir)
            self.assertEqual(result["name"], wanted_name)
            cube = Path(out_dir) / f"{wanted_name}.cube"
            self.assertTrue(cube.exists())
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_match_reports_recommended_and_bands(self):
        refs = self.studio().state().get("refs") or []
        if not refs:
            self.skipTest("no reference image registered on this server")
        out_dir = tempfile.mkdtemp(prefix="grade-client-test-match-")
        try:
            result = self.studio().match(ref=refs[0]["name"], clip=self.clip,
                                         time=0.5, config={},
                                         name="grade_client_test_match_rec",
                                         out_dir=out_dir)
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)
        self.assertIsInstance(result["recommended"], bool)
        self.assertIn("reference", result["bands"])
        self.assertIn("source_before", result["bands"])

    def test_bands_block_present_in_stats(self):
        result = self.studio().stats(clip=self.clip, time=0.5, config={},
                                     width=200)
        self.assertIn("bands", result["stats"])
        rows = bands(result["stats"])
        self.assertGreater(len(rows), 0)
        self.assertNotIn("no bands block", rows[0])

    def test_state_clips_carry_rotation_tag_and_source(self):
        state = self.studio().state()
        clip = next(c for c in state["clips"] if c["name"] == self.clip)
        self.assertIsInstance(clip["rotation_tag"], str)
        self.assertIsInstance(clip["rotation_tag_suspect"], bool)
        source = clip["source"]
        for key in ("transfer", "primaries", "matrix", "range",
                   "resolved_input", "warnings"):
            self.assertIn(key, source)


if __name__ == "__main__":
    unittest.main()
