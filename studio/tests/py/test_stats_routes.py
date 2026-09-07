#!/usr/bin/env python3
"""Contract G3, server side: measurement, not ranking.

    content/.venv/bin/python -m unittest discover -s studio/tests/py

Runs the REAL server (studio/server.py) as a subprocess on a random free port
with a temporary data directory, so nothing here can touch studio/data. The
server is killed by the PID captured at spawn, never by a name pattern. Real
footage, real refs, real match_ref.py: the thing under test is whether the
numbers these routes hand back are the numbers grade/stats.py and
grade/tools/match_ref.py actually computed, not a mock of them.

What it pins:

  ref              POST /api/stats with `ref` instead of `clip` measures a
                   still under content/refs, resolved through the same rules
                   list_refs/safe_name already use, and touches no read guard.
  times            POST /api/stats with `times` (a list) measures a clip at
                   each one and answers with one row per time, the single-time
                   shape untouched.
  region           `region` narrows the measurement on the clip form, exactly
                   as contract E4 already pins on /api/frame.
  path             POST /api/frame and POST /api/stats accept a read-only
                   `path` field (an absolute file outside anything this server
                   tracks) with logins off, and register nothing: two path
                   calls do not change GET /api/state's clip count.
  path, logins on  the same field is refused with a 403 naming itself, not
                   the generic "sign in" 401 every other route gives a caller
                   with no session.
  bands            every stats form (clip, ref, path) carries `bands`: eight
                   equal luma bands, each with a saturation, warm, tint and
                   count entry.
  match name       with no `name` in the body, /api/match's default is
                   match_u<uid>_<ref>_<clip>_<method>, sanitised the same way
                   match_ref.py's own default is.
  match out_dir    an explicit `out_dir` is where the cube lands, created if
                   missing, never grade/luts/looks for these two tests.
  recommended      a real match that improves colour distance measures
                   recommended: true; a real match that makes it worse (a
                   genuine negative gain_colour_pct, not a synthetic one)
                   measures recommended: false. Never a comparison between
                   two candidates, only this one fit against itself before
                   and after.

Two server classes, because the last two `path` cases need opposite settings:

  StatsRoutesTest      logins off. Everything but the 403.
  StatsRoutesAuthTest  logins on, one signed-in account, a bearer token (a
                       cookie POST here would fail CSRF for an unrelated
                       reason and be mistaken for the refusal under test).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
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
REFS_DIR = CONTENT / "refs"
LOOKS = CONTENT / "grade" / "luts" / "looks"

REF_GOOD = "bakeoff-asteroid-city.jpg"     # a gentle match improves on this one
REF_BAD = "bakeoff-savant-1.jpg"           # strength 1, no luma preserve: a
                                            # real, reproducible negative gain
PASSWORD = "a-long-enough-test-password-2"


def _free_port() -> int:
    """A port nothing is listening on, chosen by the kernel. Never 7431."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    if port == 7431:                                   # pragma: no cover
        return _free_port()
    return port


def _get(url: str, timeout: float = 60.0, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _post(url: str, payload: dict, timeout: float = 180.0, headers=None):
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        size = r.headers.get("X-Frame-Size")
        key = r.headers.get("X-Frame-Key")
        if r.headers.get("Content-Type", "").startswith("application/json"):
            return json.loads(r.read())
        return {"bytes": r.read(), "key": key, "size": size}


def _wait_ready(base: str, proc: subprocess.Popen, headers=None) -> None:
    deadline = time.time() + 40.0
    last = None
    while time.time() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate()
            raise RuntimeError("the studio server exited while starting:\n"
                               + err.decode("utf-8", "replace")[-2000:])
        try:
            _get(base + "/health", timeout=5.0, headers=headers)
            return
        except urllib.error.HTTPError:
            return                          # a 401 still means it is up
        except Exception as exc:                          # noqa: BLE001
            last = exc
            time.sleep(0.25)
    raise RuntimeError(f"the studio server never answered: {last}")


def _stop(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    # By the PID captured at spawn, never by a name pattern.
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:                                     # noqa: BLE001
        try:
            proc.kill()
        except Exception:                                 # noqa: BLE001
            pass
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            pipe.close()


def _bands_ok(case: unittest.TestCase, bands: dict) -> None:
    case.assertEqual(set(bands), {"edges", "saturation", "warm", "tint", "count"})
    case.assertEqual(len(bands["edges"]), 9)
    for key in ("saturation", "warm", "tint", "count"):
        case.assertEqual(len(bands[key]), 8, f"bands.{key} is not 8 long")


# --------------------------------------------------------------------------
# logins off: ref, times, region, path allowed, bands, match name/out_dir,
# recommended
# --------------------------------------------------------------------------

class StatsRoutesTest(unittest.TestCase):
    """One server for the whole class: each render costs a few seconds."""

    proc = None
    data_dir = None
    base = ""
    clip = ""
    before_looks: set = set()

    @classmethod
    def setUpClass(cls):
        if not (REFS_DIR / REF_GOOD).is_file() or not (REFS_DIR / REF_BAD).is_file():
            raise unittest.SkipTest(
                f"this suite needs {REF_GOOD} and {REF_BAD} under content/refs")
        cls.before_looks = set(LOOKS.glob("*.cube")) if LOOKS.exists() else set()
        cls.data_dir = tempfile.mkdtemp(prefix="studio-stats-data-")
        port = _free_port()
        cls.base = f"http://127.0.0.1:{port}/api"
        env = dict(os.environ, STUDIO_DATA_DIR=cls.data_dir)
        cls.proc = subprocess.Popen(
            [PYTHON, SERVER, "--port", str(port), "--data-dir", cls.data_dir],
            cwd=str(CONTENT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _wait_ready(cls.base, cls.proc)

        state = _get(cls.base + "/state")
        clips = [c for c in state["clips"] if not c.get("error")]
        if not clips:
            cls._stop()
            raise unittest.SkipTest("this test needs a clip in footage/")
        cls.clip = clips[0]["name"]
        cls.clip_count = len(state["clips"])

    @classmethod
    def _stop(cls):
        _stop(cls.proc)
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

    # -- helpers ------------------------------------------------------------

    def _stats(self, **extra):
        body = {"clip": self.clip, "time": 0.5, "width": 320, "config": {}}
        body.update(extra)
        return _post(self.base + "/stats", body)

    def _frame_bytes(self, **extra):
        body = {"time": 0.5, "width": 320, "format": "raw"}
        body.update(extra)
        req = urllib.request.Request(
            self.base + "/frame", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.read(), dict(r.headers)

    def _match(self, **extra):
        body = {"clip": self.clip, "ref": REF_GOOD, "time": 2.0, "config": {},
               "method": "reinhard", "strength": 0.6, "luma_preserve": True}
        body.update(extra)
        req = urllib.request.Request(
            self.base + "/match", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read())

    # -- ref ------------------------------------------------------------

    def test_ref_field_measures_the_reference_image(self):
        out = self._stats(ref=REF_GOOD, clip=None)
        self.assertTrue(out["key"].startswith("ref:"), out["key"])
        self.assertIn(REF_GOOD, out["key"])
        self.assertIn("stats", out)
        _bands_ok(self, out["stats"]["bands"])

    def test_an_unknown_ref_is_refused_by_name(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._stats(ref="not-a-real-reference.jpg", clip=None)
        body = json.loads(caught.exception.read())
        self.assertIn("reference not found", body.get("error", ""))

    # -- times ------------------------------------------------------------

    def test_times_returns_one_row_per_time(self):
        single = self._stats(time=0.5)
        multi = self._stats(times=[0.2, 0.5, 1.0])
        self.assertNotIn("results", single)
        self.assertEqual([r["time"] for r in multi["results"]], [0.2, 0.5, 1.0])
        for row in multi["results"]:
            self.assertIn("stats", row)
            self.assertIn("key", row)
            self.assertIn("size", row)
            _bands_ok(self, row["stats"]["bands"])
        # times=[0.5] alone must measure the same frame the single-time form
        # does: the list form is not a second implementation.
        only = self._stats(times=[0.5])
        self.assertEqual(only["results"][0]["stats"], single["stats"])

    # -- region ------------------------------------------------------------

    def test_region_narrows_the_clip_measurement(self):
        whole = self._stats()
        top = self._stats(region=[0.0, 0.0, 1.0, 0.5])
        bottom = self._stats(region=[0.0, 0.5, 1.0, 1.0])
        self.assertEqual(top["region"], [0.0, 0.0, 1.0, 0.5])
        a = top["stats"]["luma"]["mean"]
        b = bottom["stats"]["luma"]["mean"]
        mid = whole["stats"]["luma"]["mean"]
        self.assertNotAlmostEqual(a, b, places=4,
                                  msg="the two halves measured identically")
        self.assertGreaterEqual(mid, min(a, b) - 1e-6)
        self.assertLessEqual(mid, max(a, b) + 1e-6)

    def test_region_narrows_the_ref_measurement(self):
        whole = self._stats(ref=REF_GOOD, clip=None)
        left = self._stats(ref=REF_GOOD, clip=None, region=[0.0, 0.0, 0.5, 1.0])
        self.assertNotEqual(whole["size"], left["size"])
        self.assertEqual(left["size"][0], round(whole["size"][0] / 2))

    # -- path, logins off ---------------------------------------------------

    def test_path_field_measures_a_file_with_logins_off(self):
        target = REFS_DIR / REF_GOOD
        body, headers = self._frame_bytes(path=str(target))
        self.assertTrue(headers.get("X-Frame-Key", "").startswith("path:"))
        self.assertGreater(len(body), 0)

        out = self._stats(path=str(target), clip=None)
        self.assertTrue(out["key"].startswith("path:"))
        self.assertIn("stats", out)
        _bands_ok(self, out["stats"]["bands"])

    def test_path_field_rejects_a_relative_path(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._stats(path="refs/" + REF_GOOD, clip=None)
        body = json.loads(caught.exception.read())
        self.assertIn("absolute", body.get("error", ""))

    def test_path_registers_nothing(self):
        target = REFS_DIR / REF_GOOD
        before = _get(self.base + "/state")
        first = self._stats(path=str(target), clip=None)
        second = self._stats(path=str(target), clip=None)
        after = _get(self.base + "/state")
        self.assertEqual(len(before["clips"]), len(after["clips"]),
                         "a path call changed the clip list, so it registered "
                         "something")
        self.assertEqual(first["key"], second["key"],
                         "the same file measured twice got two different keys")
        self.assertEqual(first["stats"], second["stats"])

    # -- match: name, out_dir, recommended, bands ---------------------------

    def test_match_default_name_follows_the_documented_pattern(self):
        result = self._match()
        stem = Path(result["lut"]).stem
        self.assertEqual(
            stem, f"match_u0_bakeoff-asteroid-city_{Path(self.clip).stem.lower()}_reinhard")
        self.assertEqual(Path(result["lut"]).parent.resolve(), LOOKS.resolve())

    def test_match_custom_name_and_out_dir_are_honoured(self):
        with tempfile.TemporaryDirectory(prefix="studio-stats-outdir-") as tmp:
            out_dir = str(Path(tmp) / "not-yet-created")
            result = self._match(name="my_custom_match_name", out_dir=out_dir)
            lut = Path(result["lut"])
            self.assertEqual(lut.name, "my_custom_match_name.cube")
            self.assertEqual(lut.parent.resolve(), Path(out_dir).resolve())
            self.assertTrue(lut.is_file())

    def test_match_response_carries_bands_for_both_images(self):
        result = self._match()
        self.assertIn("bands", result)
        self.assertIn("reference", result["bands"])
        self.assertIn("source_before", result["bands"])
        _bands_ok(self, result["bands"]["reference"])
        _bands_ok(self, result["bands"]["source_before"])
        # Every field a caller already depended on is still there.
        self.assertIn("stats", result)
        self.assertIn("distance", result)
        self.assertIn("lut_health", result)
        self.assertIn("hue_divergence", result)

    def test_match_recommended_true_on_a_good_match(self):
        # A gentle, luma-preserving reinhard fit against a plausible reference:
        # a real match_ref.py run, not a synthetic result.
        result = self._match(ref=REF_GOOD, method="reinhard", strength=0.6,
                             luma_preserve=True)
        gain = result["distance"].get("gain_colour_pct")
        self.assertIsNotNone(gain)
        self.assertGreaterEqual(gain, 0)
        self.assertTrue(result["lut_health"]["probes_ok"])
        self.assertTrue(result["recommended"])

    def test_match_recommended_false_on_a_regression(self):
        # A real, reproducible negative gain_colour_pct: strength 1 with no
        # luma preservation against a reference far enough from the clip's own
        # content that the fit moves colour distance the wrong way. Not a
        # synthetic result: this is match_ref.py's own arithmetic on real
        # frames, the same call /api/match always makes.
        result = self._match(ref=REF_BAD, method="reinhard", strength=1.0,
                             luma_preserve=False)
        gain = result["distance"].get("gain_colour_pct")
        self.assertIsNotNone(gain)
        self.assertLess(gain, 0, "this fixture is pinned to a real negative "
                                 "gain; if match_ref.py's arithmetic changed "
                                 "and this now improves, pick a new fixture "
                                 "rather than loosening this assertion")
        self.assertFalse(result["recommended"])


# --------------------------------------------------------------------------
# logins on: path is refused by name, not the generic "sign in" 401
# --------------------------------------------------------------------------

class StatsRoutesAuthTest(unittest.TestCase):
    proc = None
    tmp = None
    base = ""
    token = ""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="studio-stats-auth-"))
        made = subprocess.run(
            [PYTHON, SERVER, "--data-dir", str(cls.tmp),
            "--create-user", "statsroutes", "--role", "user",
            "--password-stdin"],
            cwd=str(CONTENT), input=PASSWORD + "\n",
            capture_output=True, text=True)
        if made.returncode != 0:
            raise unittest.SkipTest(
                f"could not create the test account: {made.stderr}")

        port = _free_port()
        cls.base = f"http://127.0.0.1:{port}/api"
        cls.proc = subprocess.Popen(
            [PYTHON, SERVER, "--port", str(port), "--data-dir", str(cls.tmp),
            "--auth"],
            cwd=str(CONTENT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _wait_ready(cls.base, cls.proc)

        login = _post(cls.base + "/auth/login",
                      {"username": "statsroutes", "password": PASSWORD})
        # login() itself is unauthenticated (no cookie yet), so it never hits
        # the cookie-write CSRF gate; the Set-Cookie header is read straight
        # off the response, the way a browser would.
        cookie = ""
        req = urllib.request.Request(
            cls.base + "/auth/login",
            data=json.dumps({"username": "statsroutes",
                            "password": PASSWORD}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
            cookie = (r.headers.get("Set-Cookie") or "").split(";")[0]
        if not cookie:
            cls._stop()
            raise unittest.SkipTest("login did not return a session cookie")

        # A bearer token, not the cookie, for the actual test below: a cookie
        # POST with no Origin/Sec-Fetch-Site header fails this server's CSRF
        # check (by design, see auth.csrf_ok) and would be refused with a
        # DIFFERENT 403 than the one this suite is pinning. Minting the token
        # itself is a cookie POST, so it needs the same-origin fetch header a
        # real browser tab would have sent.
        token_req = urllib.request.Request(
            cls.base + "/auth/token",
            data=json.dumps({"label": "test_stats_routes"}).encode(),
            headers={"Content-Type": "application/json", "Cookie": cookie,
                    "Sec-Fetch-Site": "same-origin"},
            method="POST")
        with urllib.request.urlopen(token_req, timeout=30) as r:
            cls.token = json.loads(r.read())["token"]

    @classmethod
    def _stop(cls):
        _stop(cls.proc)
        cls.proc = None

    @classmethod
    def tearDownClass(cls):
        cls._stop()
        if cls.tmp:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def _auth_headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def test_path_field_is_refused_with_logins_on(self):
        target = REFS_DIR / REF_GOOD
        for route, extra in (("frame", {"time": 0.5, "width": 320}),
                             ("stats", {"time": 0.5, "width": 320})):
            body = {"path": str(target)}
            body.update(extra)
            req = urllib.request.Request(
                f"{self.base}/{route}", data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                        **self._auth_headers()},
                method="POST")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(req, timeout=30)
            self.assertEqual(caught.exception.code, 403,
                             f"POST /api/{route} with path should be a 403 "
                             f"naming itself, not a generic sign-in failure")
            msg = json.loads(caught.exception.read()).get("error", "")
            self.assertIn("logins off", msg, msg)

    def test_a_caller_with_no_session_gets_the_generic_401_not_this_403(self):
        """The path field's own 403 is not reachable without a session at
        all: an unauthenticated caller is stopped by the ordinary login gate
        first, so the two refusals are never confused with each other."""
        req = urllib.request.Request(
            self.base + "/stats",
            data=json.dumps({"path": str(REFS_DIR / REF_GOOD)}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=30)
        self.assertEqual(caught.exception.code, 401)


if __name__ == "__main__":                                    # pragma: no cover
    unittest.main()
