#!/usr/bin/env python3
"""Lane M7 (tests): SAM masks, end to end through the REAL stub service.

    content/.venv/bin/python -m unittest discover -s studio/tests/py

M5's own `test_mask_routes.py` pins every `/api/mask/*` and `/api/matte*`
route against a hand written FAKE C3 server, because `sam/` did not exist yet
on this branch when that lane wrote its tests. It says so plainly in its own
checkpoint (`plan/2026-09-08-studio-masks/checkpoints/M5.md`, section 7): "The
one true SAM-service round trip (a real sam/server.py, even in --stub mode)
has never been run against these routes."

This file is that round trip. It starts a REAL `sam/server.py --stub`
subprocess (the project's own uv-managed interpreter, `sam/.venv/bin/python`,
no weights, no model lock) and a REAL `studio/server.py` subprocess pointed
at it with `--sam-url`, then drives the same C4 routes M5 pins, over real
HTTP, against real footage (one short clip, `A001_09011336_C002.MOV`, 5
seconds at 3840x2160, isolated into its own `--footage` folder so this never
touches the shared footage listing or writes into it).

Both server processes are started and stopped by the PID captured at spawn,
never by a name pattern, and never on ports 7431, 7560, 7614 or 7615 (the
founder's live studio and the two other reserved ports, plus the SAM
service's own default port, so a stray run here can never collide with a
real one somebody left running).

What this file covers, all against the real stub, none of it against a fake:

  status         GET /api/mask/status reports the real stub's own health
                 shape (backend "stub"), and a clear ok:false + start-command
                 503 when the whole service is down.
  segment        POST /api/mask/segment returns real candidate instances
                 from the real stub's ellipse detector, with overlay/mask
                 preview URLs that serve real bytes.
  track, states  POST /api/mask/track against the real stub (slowed with
                 --stub-delay-ms so the job does not finish before the first
                 poll lands) moves a matte through queued/running/done, and
                 a track using the stub's own __fail__ test hook (a real
                 backend feature, not something this file invents) leaves a
                 matte "partial" with the frames it wrote before the crash,
                 exactly as sam/server.py's own _end_job documents.
  frame route    GET /api/matte/<id>/frame?time=&width= on a matte the real
                 stub has not finished serves the nearest written frame and
                 reports X-Matte-State / X-Matte-Frame for it.
  render refuse  POST /api/render on a config naming a still-partial matte
                 is refused (400, names the matte) unless allow_partial.
  stats by matte matches a HAND COMPUTED weighted number: three synthetic
                 (hand built, not tracked) mattes -- left half, right half,
                 and their union -- are dropped straight into the real data
                 dir, and the union's weighted luma mean is checked against
                 the pixel-count-weighted combination of the two halves'
                 own means, computed independently in this file. That
                 identity (mean_C = (nA*meanA + nB*meanB) / (nA+nB) for two
                 disjoint full-weight regions covering all of C) holds for
                 ANY picture, so it proves the server's real weighting math
                 without this file needing to know or predict a single
                 pixel of the real footage it measures.
  preset load    GET /api/preset?clip= resolves a saved text recipe to a
                 freshly queued real matte; a point recipe is left
                 needs_pick, same as M5 pins against the fake.
  service down   the whole SAM service unreachable is a clean 503 whose
                 message names the real start command
                 (studio/sam_client.py's own START_COMMAND).

Not covered here (out of this lane's ownership, per the plan): the real MLX
or torch backend (sam/tests/test_real_model.py, M1's own file), the GPU
preview reading a matte (studio/tests/parity-gate.mjs, M3's file), and the
`mask` CLI verbs (grade/tests/test_mask_tools.py, M4's file, which itself
still runs against ITS OWN fake for the same reason M5's checkpoint gives).
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

import numpy as np

HERE = Path(__file__).resolve().parent
STUDIO = HERE.parents[1]
CONTENT = STUDIO.parent
GRADE = CONTENT / "grade"
SAM = CONTENT / "sam"
PYTHON = str(CONTENT / ".venv" / "bin" / "python")
SERVER = str(STUDIO / "server.py")
SAM_PYTHON = str(SAM / ".venv" / "bin" / "python")
SAM_SERVER = str(SAM / "server.py")
FIXTURE_CLIP = "A001_09011336_C002.MOV"

sys.path.insert(0, str(GRADE))
import mattes as MT                                           # noqa: E402

FORBIDDEN_PORTS = (7431, 7560, 7614, 7615)


# --------------------------------------------------------------------------
# process and HTTP helpers (deliberately not imported from test_mask_routes.py:
# that file is M5's own, this lane adds new files rather than extending theirs)
# --------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    if port in FORBIDDEN_PORTS:
        return _free_port()
    return port


def _get(url: str, timeout: float = 30.0, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _get_raw(url: str, timeout: float = 30.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read(), dict(r.headers)


def _post(url: str, payload: dict, timeout: float = 60.0, headers=None):
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _wait_http_ok(base_health_url: str, proc: subprocess.Popen, what: str,
                  timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate()
            raise RuntimeError(f"{what} exited while starting:\n"
                               + (err or b"").decode("utf-8", "replace")[-2000:])
        try:
            _get(base_health_url, timeout=5.0)
            return
        except urllib.error.HTTPError:
            return  # answering, even with an error status, is "up"
        except Exception as exc:                                  # noqa: BLE001
            last = exc
            time.sleep(0.25)
    raise RuntimeError(f"{what} never answered {base_health_url}: {last}")


def _stop(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:                                             # noqa: BLE001
        try:
            proc.kill()
        except Exception:                                         # noqa: BLE001
            pass
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            pipe.close()


def _matte_layer_config(matte_id: str, recipe=None) -> dict:
    return {"layers": [
        {"enabled": True, "name": "L1", "placement": "before_look",
         "mask": {"components": [
             {"id": "c1", "type": "matte", "op": "add",
              "matte": {"id": matte_id, "recipe": recipe or {}}}]},
         "correct": {"exposure": 0.4}},
    ]}


def _isolated_footage_dir() -> str:
    """One real, short, small clip, symlinked into its own temp folder, so
    this file's server never lists (or could ever write into) the shared
    footage/ directory. C002 is 5 seconds at 3840x2160: short enough that
    the plain Rec.709 mask proxy (which encodes the WHOLE clip once, per
    clip and rotation, see studio/server.py's _mask_proxy_params) builds in
    a few seconds rather than minutes, which every track test in this file
    then reuses for free (one server, one proxy, many tracks).
    """
    src = CONTENT / "footage" / FIXTURE_CLIP
    if not src.is_file():
        raise unittest.SkipTest(f"fixture clip missing: {src}")
    d = tempfile.mkdtemp(prefix="studio-masks-m7-footage-")
    os.symlink(src, Path(d) / FIXTURE_CLIP)
    return d


# --------------------------------------------------------------------------
# the real stub service + the real studio server, both up together
# --------------------------------------------------------------------------

class MaskStubE2ETest(unittest.TestCase):
    sam_proc = None
    sam_data_dir = None
    proc = None
    data_dir = None
    footage_dir = None
    base = ""
    sam_base = ""
    clip = ""
    clip_key = ""

    @classmethod
    def setUpClass(cls):
        cls.footage_dir = _isolated_footage_dir()

        cls.sam_data_dir = tempfile.mkdtemp(prefix="sam-stub-m7-data-")
        sam_port = _free_port()
        cls.sam_base = f"http://127.0.0.1:{sam_port}"
        cls.sam_proc = subprocess.Popen(
            [SAM_PYTHON, SAM_SERVER, "--port", str(sam_port),
             "--data-dir", cls.sam_data_dir, "--stub", "--no-model-lock",
             # Slowed on purpose: an instant stub finishes before the first
             # poll ever lands, and this file's whole point is to observe
             # queued/running states and a still-partial matte for real.
             "--stub-delay-ms", "150"],
            cwd=str(CONTENT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _wait_http_ok(cls.sam_base + "/health", cls.sam_proc, "the real SAM stub service")

        cls.data_dir = tempfile.mkdtemp(prefix="studio-masks-m7-data-")
        port = _free_port()
        cls.base = f"http://127.0.0.1:{port}/api"
        cls.proc = subprocess.Popen(
            [PYTHON, SERVER, "--port", str(port), "--data-dir", cls.data_dir,
             "--footage", cls.footage_dir, "--sam-url", cls.sam_base,
             # _preview_dims floors this at 160 regardless, but asking for
             # less keeps the intent honest and matches M5's own tests.
             "--mask-width", "160"],
            cwd=str(CONTENT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _wait_http_ok(cls.base + "/state", cls.proc, "studio/server.py")

        state = _get(cls.base + "/state")
        clips = [c for c in state["clips"] if not c.get("error")]
        if not clips:
            cls._stop_all()
            raise unittest.SkipTest(
                "the isolated fixture clip failed to probe: " + json.dumps(
                    [c for c in state["clips"] if c.get("error")]))
        cls.clip = clips[0]["name"]
        cls.clip_key = clips[0]["key"]

    @classmethod
    def _stop_all(cls):
        _stop(cls.proc)
        cls.proc = None
        _stop(cls.sam_proc)
        cls.sam_proc = None

    @classmethod
    def tearDownClass(cls):
        cls._stop_all()
        for d in (cls.data_dir, cls.sam_data_dir, cls.footage_dir):
            if d:
                shutil.rmtree(d, ignore_errors=True)

    # -- helpers -------------------------------------------------------

    def _track(self, **extra):
        # "rotation": 0 is a deliberate default for THIS file, kept even
        # after INTEGRATION-A fixed the "auto" bug it used to work around
        # (see test_track_with_default_rotation_resolves_the_clips_own_tag
        # below, which now pins that fix): most tests here want a fixed,
        # predictable frame size and pixel content (self.clip's own tag is
        # 90, so "auto" would track the rotated, taller proxy instead), not
        # the arithmetic of resolving a tag on every run.
        body = {"clip": self.clip, "prompts": {"text": ["m7 subject"]}, "rotation": 0}
        body.update(extra)
        return _post(self.base + "/mask/track", body)

    def _wait_matte_state(self, matte_id: str, states: tuple, timeout: float = 30.0) -> dict:
        deadline = time.time() + timeout
        last = None
        info = None
        while time.time() < deadline:
            info = _get(self.base + f"/matte/{matte_id}")
            last = info["state"]
            if last in states:
                return info
            time.sleep(0.2)
        raise AssertionError(f"matte {matte_id} never reached {states}, "
                             f"stuck at {last}: {info}")

    def _make_hand_matte(self, matte_id: str, arr: np.ndarray, width: int,
                         height: int) -> str:
        """A synthetic (hand built, never tracked) matte at exactly the
        picture's own render size, so grade/mattes.py's resize is an
        identity: (iw, ih) == (width, height) returns the array unchanged
        (grade/mattes.py, resize_bilinear). No SAM call happens for this
        matte at all; it exists only as a fixture for the weighting math.
        """
        d = Path(self.data_dir) / "mattes" / self.clip_key / matte_id
        d.mkdir(parents=True, exist_ok=True)
        MT.write_gray_png(d / MT.frame_name(0), arr)
        (d / MT.INDEX_NAME).write_text(json.dumps({
            "matte_id": matte_id, "clip": self.clip, "clip_key": self.clip_key,
            "rotation": "auto", "fps": 24.0, "frames": 1, "width": width,
            "height": height, "recipe": {}, "state": "done", "done_frames": 1,
            "areas": [float(arr.mean()) / 255.0], "scores": [1.0],
            "created": time.time(), "model": "m7-hand-fixture",
            "backend": "m7-hand-fixture",
        }))
        return matte_id

    # -- status -----------------------------------------------------------

    def test_status_reports_the_real_stub_service_healthy(self):
        out = _get(self.base + "/mask/status")
        self.assertTrue(out["ok"])
        self.assertTrue(out["service"]["ok"])
        self.assertEqual(out["service"]["backend"], "stub")
        self.assertIn("jobs", out)

    # -- segment: candidates from the real stub detector -------------------

    def test_segment_returns_real_candidates_with_working_previews(self):
        out = _post(self.base + "/mask/segment", {
            "clip": self.clip, "time": 0.2, "prompts": {"text": ["a subject"]}})
        self.assertIn("pick_id", out)
        self.assertGreaterEqual(len(out["instances"]), 1)
        inst = out["instances"][0]
        for key in ("id", "score", "box", "area", "overlay", "mask"):
            self.assertIn(key, inst)
        self.assertTrue(0.0 <= inst["score"] <= 1.0)
        self.assertEqual(len(inst["box"]), 4)
        self.assertTrue(all(0.0 <= v <= 1.0 for v in inst["box"]))

        ov_bytes, _ = _get_raw(self.base[:-4] + inst["overlay"])
        self.assertGreater(len(ov_bytes), 0)
        mk_bytes, _ = _get_raw(self.base[:-4] + inst["mask"])
        self.assertGreater(len(mk_bytes), 0)

    def test_segment_with_no_prompt_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            _post(self.base + "/mask/segment",
                 {"clip": self.clip, "time": 0.2, "prompts": {}})
        self.assertEqual(caught.exception.code, 400)
        body = json.loads(caught.exception.read())
        self.assertIn("prompt", body.get("error", ""))

    # -- track: a real job, real states, over real HTTP --------------------

    def test_track_creates_a_job_and_the_matte_moves_through_real_states(self):
        # A prompt phrase used nowhere else in this file. queue_mask_track's
        # own recipe cache (design rule 5, "identical prompts/select/steady
        # on the same clip and rotation reuse the same matte") is keyed by
        # clip_key + rotation + prompts/select/steady, NOT start/end; the
        # default "m7 subject" text is also used (with a different end) by
        # test_render_refuses_a_partial_matte_unless_allow_partial, which
        # sorts before this test and waits its own job to "done". Reusing
        # that same default text here would be a cache HIT against that
        # already finished matte (job_id None, state already "done" on the
        # very first read), not the fresh job this test means to observe
        # mid flight. A distinct phrase guarantees a fresh matte instead.
        result = self._track(start=0.0, end=0.6,
                             prompts={"text": ["m7 states subject"]})
        self.assertIn("job_id", result)
        self.assertIsNotNone(result["job_id"])
        self.assertEqual(len(result["mattes"]), 1)
        matte_id = result["mattes"][0]["matte_id"]

        seen = set()
        deadline = time.time() + 30.0
        info = None
        while time.time() < deadline:
            info = _get(self.base + f"/matte/{matte_id}")
            seen.add(info["state"])
            if info["state"] == "done":
                break
            time.sleep(0.1)
        self.assertEqual(info["state"], "done", f"states observed: {seen}")
        self.assertFalse(info["is_partial"])
        self.assertGreater(info["done_frames"], 0)
        # --stub-delay-ms 150 on a ~14 frame track (0.6s at 24fps) is a
        # little over 2 seconds of real wall clock: the 0.1s poll loop
        # above must have caught the job somewhere short of "done" at
        # least once, or this whole file is not proving what it claims to.
        self.assertTrue(seen - {"done"}, f"job finished before it could be "
                        f"observed running; states seen: {seen}")

    def test_dedicated_cancel_route_matches_the_readmes_documented_path(self):
        # INTEGRATION-A found studio/README.md documents a dedicated
        # POST /api/mask/jobs/<id>/cancel that studio/server.py did not
        # implement (the generic POST /api/job/cancel already covered the
        # same behaviour functionally). Part B added the thin route per
        # PLAN.md C4's own list; this proves it end to end against the real
        # stub rather than just reading the code.
        result = self._track(start=0.0, end=0.6,
                             prompts={"text": ["m7 cancel route subject"]})
        job_id = result["job_id"]
        self.assertIsNotNone(job_id)
        out = _post(self.base + f"/mask/jobs/{job_id}/cancel", {})
        self.assertEqual(out["id"], job_id)
        self.assertIn(out["state"], ("cancelled", "failed", "done"))
        # Whatever terminal state the stub settles on, the job must leave
        # "queued"/"running" behind: this is the same field and vocabulary
        # GET /api/mask/jobs/<id> uses, so a caller reads one shape either
        # way (M1's checkpoint contract, mirrored by _mask_job_view).
        deadline = time.time() + 15.0
        final = out
        while final["state"] not in ("cancelled", "failed", "done") and time.time() < deadline:
            time.sleep(0.2)
            final = _get(self.base + f"/mask/jobs/{job_id}")
        self.assertIn(final["state"], ("cancelled", "failed", "done"))

    def test_cancel_route_404s_for_an_unknown_job_id(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            _post(self.base + "/mask/jobs/no-such-job/cancel", {})
        self.assertEqual(caught.exception.code, 404)

    def test_track_needs_a_prompt_or_a_pick(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._track(prompts={})
        self.assertEqual(caught.exception.code, 400)

    def test_track_with_default_rotation_resolves_the_clips_own_tag(self):
        """A real integration bug this file found (Bug B, checkpoints/M6.md),
        now fixed; this test pins the fix rather than the bug it replaced.

        POST /api/mask/track with no "rotation" field at all is the normal,
        common case: effective_rotation() (studio/server.py) defaults to the
        STRING "auto" (design intent: let the file's own display matrix tag
        decide), and queue_mask_track() sends that string straight through
        as the wire "rotation" field to the real SAM service, alongside a
        NEW "rotation_probe_path" field (studio/sam_client.py, added by the
        same fix) carrying the clip's real file path, since the "clip" field
        alone is a bare display name the matte record keeps forever, not a
        path this process can open. sam/server.py's own /track handler
        (resolve_rotation, C3) now ffprobes that path for the clip's own
        display-matrix tag instead of raising `int("auto")`. self.clip
        (A001_09011336_C002.MOV, this file's own module docstring) is
        tagged rotation 90; a track with no rotation field at all resolves
        to exactly that, not the silent 0 a probe of an unresolvable path
        would fall back to.
        """
        result = _post(self.base + "/mask/track",
                       {"clip": self.clip,
                        "prompts": {"text": ["default rotation subject"]}})
        matte_id = result["mattes"][0]["matte_id"]
        info = self._wait_matte_state(matte_id, ("done", "partial", "failed"))
        self.assertEqual(info["rotation"], "90", info)

    def test_matte_at_rotation_zero_reads_back_as_rotation_zero_not_auto(self):
        """A second real integration bug this file found, this one fixed.

        Every _track() call in this file already sends "rotation": 0 (the
        Bug B workaround above), so sam/server.py's own /track handler
        writes the JSON INTEGER 0 into index.json's "rotation" field, not
        the string "0". grade/mattes.py's info_from_dir() used to read
        that field with `str(raw.get("rotation") or "auto")`: 0 is falsy in
        Python, so a matte tracked at rotation 0 (SAM's normal encoding of
        "no rotation", not an edge case) permanently reported itself as
        made at "auto" through GET /api/matte/<id>. On the browser side
        masks.js's componentState() compares that field against the
        viewer's own current rotation and calls anything that disagrees
        "stale"; with the field corrupted to "auto" a matte made and
        watched at rotation 0 went stale forever, never reaching "done" in
        the UI even though the job itself finished cleanly (state=="done"
        underneath the wrong rotation label). No test in this file caught
        it before now because nothing here asserted on info["rotation"]
        itself, only on info["state"]. Fixed in grade/mattes.py with a
        `_parse_rotation` helper that only defaults to "auto" when the
        field is truly absent (None or ""), mirroring the file's own
        `_parse_created` fix for the unrelated `created`-field bug (Bug A,
        checkpoints/M6.md). This test pins the fix: a fix that regresses
        it back to the `or "auto"` shape fails here immediately.
        """
        result = self._track(start=0.0, end=0.3,
                             prompts={"text": ["m7 rotation zero subject"]})
        matte_id = result["mattes"][0]["matte_id"]
        info = self._wait_matte_state(matte_id, ("done", "partial", "failed"))
        self.assertEqual(info["rotation"], "0", info)

    def test_track_from_a_pick_selects_the_picked_instance(self):
        pick = _post(self.base + "/mask/segment", {
            "clip": self.clip, "time": 0.2, "prompts": {"text": ["pick me"]}})
        pick_id = pick["pick_id"]
        inst_id = pick["instances"][0]["id"]
        result = self._track(pick_id=pick_id, select=[inst_id], prompts=None,
                             start=0.0, end=0.3)
        self.assertEqual(len(result["mattes"]), 1)
        matte_id = result["mattes"][0]["matte_id"]
        info = self._wait_matte_state(matte_id, ("done",), timeout=20.0)
        self.assertEqual(info["state"], "done")

    def _track_a_crashing_job(self) -> dict:
        """__fail__ is a real feature of the real stub backend
        (sam/backends/stub.py: "if FAIL_PHRASE in prompts['text'] and index
        >= 2: raise"), not a fake standing in for one: frames 0 and 1 get
        written for real, then the whole track call raises, and
        sam/server.py's own _end_job turns that into matte state "partial"
        (frames written, but short of the range) rather than "failed"
        (nothing written at all). Shared by two tests below, each starting
        its own job so neither depends on the other's timing.
        """
        result = self._track(prompts={"text": ["__fail__"]}, start=0.0, end=1.0)
        matte_id = result["mattes"][0]["matte_id"]
        info = self._wait_matte_state(matte_id, ("partial", "failed"), timeout=20.0)
        self.assertEqual(info["state"], "partial", info)
        self.assertGreater(info["done_frames"], 0)
        self.assertTrue(info["is_partial"])
        return {"matte_id": matte_id, "info": info}

    def test_a_crashing_track_leaves_a_partial_matte_with_the_frames_it_wrote(self):
        self._track_a_crashing_job()

    # -- the frame route: nearest written frame, real partial headers ------

    def test_frame_route_serves_nearest_written_frame_with_state_headers(self):
        matte_id = self._track_a_crashing_job()["matte_id"]
        # frame 999 of a matte that only ever wrote frames 0 and 1: the
        # nearest-written-frame fallback (grade/mattes.py's load_time,
        # shared by this route) must serve frame 1 rather than 404 or 500.
        png, headers = _get_raw(
            self.base + f"/matte/{matte_id}/frame?time=999&width=32")
        self.assertGreater(len(png), 0)
        self.assertEqual(headers.get("X-Matte-State"), "partial")
        self.assertIn("X-Matte-Frame", headers)
        self.assertLessEqual(int(headers["X-Matte-Frame"]), 1)

    # -- render refuses a partial matte unless allow_partial ---------------

    def test_render_refuses_a_partial_matte_unless_allow_partial(self):
        result = self._track(start=0.0, end=1.0)
        matte_id = result["mattes"][0]["matte_id"]
        # Catch it mid flight: 24 frames * 150ms/frame is a few seconds, so
        # a render attempt right after enqueue lands well before "done".
        self._wait_matte_state(matte_id, ("queued", "running"), timeout=10.0)

        cfg = _matte_layer_config(matte_id)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            _post(self.base + "/render", {
                "clip": self.clip, "config": cfg, "duration": 0.2,
                "name": f"m7_render_refuse_{matte_id}"})
        self.assertEqual(caught.exception.code, 400)
        body = json.loads(caught.exception.read())
        self.assertIn(matte_id, body.get("error", ""))

        out = _post(self.base + "/render", {
            "clip": self.clip, "config": cfg, "duration": 0.2,
            "allow_partial": True, "name": f"m7_render_allow_{matte_id}"})
        self.assertIn("job", out)
        self._wait_matte_state(matte_id, ("done",), timeout=25.0)

    # -- stats by matte: matches a hand computed weighted number -----------

    def test_stats_weighted_by_matte_matches_a_hand_computed_number(self):
        probe = _post(self.base + "/stats", {
            "clip": self.clip, "time": 0.2, "width": 160, "config": {}})
        width, height = probe["size"]
        half = width // 2
        self.assertGreater(half, 0)
        self.assertGreater(width - half, 0)

        weight_left = np.zeros((height, width), dtype=np.uint8)
        weight_left[:, :half] = 255
        weight_right = np.zeros((height, width), dtype=np.uint8)
        weight_right[:, half:] = 255
        weight_union = np.zeros((height, width), dtype=np.uint8)
        weight_union[:, :] = 255       # left and right together cover it all

        id_left = self._make_hand_matte("m_m7_left", weight_left, width, height)
        id_right = self._make_hand_matte("m_m7_right", weight_right, width, height)
        id_union = self._make_hand_matte("m_m7_union", weight_union, width, height)

        def weighted_mean(matte_id: str) -> float:
            out = _post(self.base + "/stats", {
                "clip": self.clip, "time": 0.2, "width": 160, "config": {},
                "matte": matte_id})
            self.assertEqual(out["matte"], matte_id)
            return out["stats"]["luma"]["mean"]

        mean_left = weighted_mean(id_left)
        mean_right = weighted_mean(id_right)
        mean_union = weighted_mean(id_union)

        # The hand computed number: for two DISJOINT, full-weight (0 or 1)
        # regions whose union covers the whole frame, the union's weighted
        # mean is exactly the pixel-count-weighted combination of the two
        # regions' own means. This holds for ANY picture -- it is an
        # identity of the weighted-mean formula itself
        # (grade/stats.py: _wmean = sum(y*w) / sum(w)) -- so it proves the
        # real server's real weighting math without this file needing to
        # predict a single pixel of the real footage it is measuring.
        count_left = int((weight_left > 0).sum())
        count_right = int((weight_right > 0).sum())
        expected_union = ((count_left * mean_left + count_right * mean_right)
                          / (count_left + count_right))
        self.assertAlmostEqual(mean_union, expected_union, delta=0.001,
                              msg=f"left={mean_left} right={mean_right} "
                                  f"union={mean_union} expected={expected_union}")

        # A second, independent identity: the union matte weights every
        # pixel by exactly 1, so it must also match the plain, unweighted
        # measurement of the same frame.
        plain = _post(self.base + "/stats", {
            "clip": self.clip, "time": 0.2, "width": 160, "config": {}})
        self.assertAlmostEqual(mean_union, plain["stats"]["luma"]["mean"],
                              delta=0.001)

    def test_stats_with_an_unknown_matte_is_a_clean_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            _post(self.base + "/stats", {"clip": self.clip, "time": 0.2,
                                         "width": 160, "config": {},
                                         "matte": "m7-does-not-exist"})
        self.assertEqual(caught.exception.code, 404)

    # -- preset load resolution, against the real service -------------------

    def test_preset_load_auto_queues_a_text_recipe_via_the_real_service(self):
        name = "m7_text_preset"
        cfg = _matte_layer_config(
            "", recipe={"prompts": {"text": ["preset queued subject"]}})
        _post(self.base + "/preset", {"name": name, "config": cfg})
        # &rotation=0: the same real-service rotation bug _track() works
        # around (see its own comment). GET /api/preset?clip= resolves
        # rotation the same way (effective_rotation(query, uid)) and this
        # route queues a track exactly the way POST /api/mask/track does,
        # so it hits the identical int("auto") crash without this.
        out = _get(self.base + f"/preset?name={name}&clip={self.clip}&rotation=0")
        self.assertIn("mask_queued", out)
        queued = out["mask_queued"]
        self.assertFalse(any(item.get("error") for item in queued), queued)
        comp = out["config"]["layers"][0]["mask"]["components"][0]
        self.assertTrue(comp["matte"]["id"])
        self.assertFalse(comp["matte"].get("needs_pick", False))
        self._wait_matte_state(comp["matte"]["id"], ("done",), timeout=20.0)

    def test_preset_load_leaves_a_point_recipe_needing_a_pick(self):
        name = "m7_point_preset"
        cfg = _matte_layer_config(
            "", recipe={"prompts": {"points": [{"x": 0.5, "y": 0.5, "label": 1}]}})
        _post(self.base + "/preset", {"name": name, "config": cfg})
        out = _get(self.base + f"/preset?name={name}&clip={self.clip}")
        comp = out["config"]["layers"][0]["mask"]["components"][0]
        self.assertTrue(comp["matte"].get("needs_pick"))
        self.assertFalse(comp["matte"].get("id"))


# --------------------------------------------------------------------------
# the SAM service entirely down: a clean 503 naming the real start command
# --------------------------------------------------------------------------

class MaskServiceDownTest(unittest.TestCase):
    proc = None
    data_dir = None
    footage_dir = None
    base = ""
    clip = ""

    @classmethod
    def setUpClass(cls):
        cls.footage_dir = _isolated_footage_dir()
        cls.data_dir = tempfile.mkdtemp(prefix="studio-masks-m7-down-data-")
        port = _free_port()
        dead_port = _free_port()
        cls.base = f"http://127.0.0.1:{port}/api"
        cls.proc = subprocess.Popen(
            [PYTHON, SERVER, "--port", str(port), "--data-dir", cls.data_dir,
             "--footage", cls.footage_dir,
             "--sam-url", f"http://127.0.0.1:{dead_port}"],
            cwd=str(CONTENT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _wait_http_ok(cls.base + "/state", cls.proc, "studio/server.py")
        state = _get(cls.base + "/state")
        clips = [c for c in state["clips"] if not c.get("error")]
        if not clips:
            _stop(cls.proc)
            raise unittest.SkipTest("the isolated fixture clip failed to probe")
        cls.clip = clips[0]["name"]

    @classmethod
    def tearDownClass(cls):
        _stop(cls.proc)
        for d in (cls.data_dir, cls.footage_dir):
            if d:
                shutil.rmtree(d, ignore_errors=True)

    def test_status_reports_the_service_down(self):
        out = _get(self.base + "/mask/status")
        self.assertFalse(out["ok"])
        self.assertIn("error", out["service"])

    def test_segment_with_the_service_down_is_a_clear_503_naming_the_start_command(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            _post(self.base + "/mask/segment",
                 {"clip": self.clip, "time": 0.2, "prompts": {"text": ["x"]}})
        self.assertEqual(caught.exception.code, 503)
        body = json.loads(caught.exception.read())
        self.assertIn("uv run --project sam", body.get("error", ""))
        self.assertIn("sam/server.py", body.get("error", ""))

    def test_track_with_the_service_down_is_a_clear_503_naming_the_start_command(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            _post(self.base + "/mask/track",
                 {"clip": self.clip, "prompts": {"text": ["x"]}})
        self.assertEqual(caught.exception.code, 503)
        body = json.loads(caught.exception.read())
        self.assertIn("uv run --project sam", body.get("error", ""))


if __name__ == "__main__":                                        # pragma: no cover
    unittest.main()
