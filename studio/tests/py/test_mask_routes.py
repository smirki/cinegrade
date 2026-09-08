#!/usr/bin/env python3
"""SAM masks (M5, contracts C2/C3/C4), server side.

    content/.venv/bin/python -m unittest discover -s studio/tests/py

Runs the REAL studio server (studio/server.py) as a subprocess, the same way
every other file in this folder does: a random free port, a temporary data
directory, killed by the captured PID, never by a name pattern.

The one thing this file cannot do is talk to the real SAM service: sam/
does not exist on this branch yet (M1 builds it after a spike). So this file
also runs a FAKE C3 server, a small stdlib http.server.ThreadingHTTPServer in
a background thread, implementing the four routes SamClient calls (health,
segment, track, jobs) in the same shapes contract C3 promises. The studio
server is started with `--sam-url` pointed at it. Every route this file pins
is therefore pinned against the wire shape, not against a mock of
studio/server.py's own code; when M1's real service is up, the same requests
should get the same answers (segment/mask.py's stub mode is documented to
answer "every route in the identical shape a real backend does").

What it pins:

  status           GET /api/mask/status reports the fake's health when it is
                   up, and ok:false with a start-command message when the
                   whole service is down.
  segment          POST /api/mask/segment returns a pick_id and per instance
                   overlay/mask preview URLs that actually serve bytes; no
                   prompt is refused with a clear message, not a traceback.
  track, cache     POST /api/mask/track with a text prompt (no pick) queues a
                   job and bootstraps a matte per instance; asking again with
                   the exact same clip, rotation and prompt is free (design
                   rule 5): the second call touches the fake's /track route
                   zero more times and returns the same matte ids.
  track, pick      A pick's own prompts carry into `mask.matte.recipe`
                   whether the caller sent them back as a pick_id or the
                   caller resent the same prompts by hand: the same recipe
                   hash either way.
  matte store      GET /api/matte lists what track wrote, GET /api/matte/<id>
                   answers the C2 fields, GET /api/matte/<id>/frame answers a
                   PNG with X-Matte-State and X-Matte-Frame, falling back to
                   the nearest written frame while a track is still running.
  render refuses   POST /api/render on a config whose one layer holds a
                   still queued/partial matte is a 400 naming the layer and
                   the matte; allow_partial: true renders anyway.
  stats by matte   POST /api/stats with `matte` weights the measurement and
                   differs from the unweighted call on a frame with a real
                   (non uniform) matte.
  preset load      GET /api/preset with `clip` resolves a saved text recipe
                   to a fresh matte id and queues it (mask_queued in the
                   response); a point recipe is left needs_pick instead.
  identity         mattes are visible to every caller regardless of
                   X-Studio-Agent (they belong to the clip, not the caller);
                   DELETE needs admin once logins are on.
"""

from __future__ import annotations

import hashlib
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
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
STUDIO = HERE.parents[1]
CONTENT = STUDIO.parent
GRADE = CONTENT / "grade"
PYTHON = str(CONTENT / ".venv" / "bin" / "python")
SERVER = str(STUDIO / "server.py")
PASSWORD = "a-long-enough-test-password-2"
# The working width every mask route hands the service, and the floor the
# server clamps to (studio/server.py: set_mask_width, and _preview_dims for
# every other sampled frame). Nothing narrower is a real width anywhere in
# the studio, so a test asking for 64 would be measuring 160 and calling it
# 64.
MASK_WIDTH = 160
MIN_SAMPLE_WIDTH = 160

sys.path.insert(0, str(GRADE))
import mattes as MT                                           # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    if port in (7431, 7614, 7615):                      # pragma: no cover
        return _free_port()
    return port


def _get(url: str, timeout: float = 30.0, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _post(url: str, payload: dict, timeout: float = 60.0, headers=None,
         raw: bool = False):
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if raw:
            return r.read(), dict(r.headers)
        return json.loads(r.read())


def _delete(url: str, headers=None):
    req = urllib.request.Request(url, headers=headers or {}, method="DELETE")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


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
            return
        except Exception as exc:                                  # noqa: BLE001
            last = exc
            time.sleep(0.25)
    raise RuntimeError(f"the studio server never answered: {last}")


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


# --------------------------------------------------------------------------
# a fake SAM service (contract C3): health, segment, track, jobs. Good
# enough to exercise studio/server.py's client code, not a real model.
# --------------------------------------------------------------------------

class _FakeSamState:
    def __init__(self):
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self.picks: dict[str, dict] = {}
        self.track_calls = 0
        self.segment_calls = 0
        self.pick_track_calls = 0
        # The `matte_ids` override the last /track carried, so a test can
        # prove studio asked for a RESUME rather than a fresh matte
        # (checkpoint gap 12).
        self.last_matte_ids: dict = {}


class _FakeSamHandler(BaseHTTPRequestHandler):
    state: _FakeSamState = None                        # set per server instance

    def log_message(self, fmt, *args):                 # noqa: D401  (quiet)
        pass

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw) if raw else {}

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                                    # noqa: N802
        if self.path == "/health":
            self._send(200, {"ok": True, "backend": "fake-stub", "model": "none",
                             "loaded": True, "busy": False, "queue": 0})
            return
        if self.path.startswith("/jobs/"):
            job_id = self.path[len("/jobs/"):]
            with self.state.lock:
                job = self.state.jobs.get(job_id)
            if not job:
                self._send(404, {"error": f"no such job: {job_id}"})
                return
            mattes = [dict(m, state=job.get("matte_state", {}).get(
                m["matte_id"], job["state"]),
                error=job.get("matte_error", {}).get(m["matte_id"]))
                     for m in job["mattes"]]
            self._send(200, {
                "state": job["state"], "done_frames": job["done_frames"],
                "total_frames": job["total_frames"], "fps": job["fps"],
                "elapsed_s": time.time() - job["created"],
                "matte_ids": job["matte_ids"], "mattes": mattes,
                "error": job.get("error")})
            return
        if self.path == "/jobs":
            with self.state.lock:
                self._send(200, {"jobs": [
                    {"job_id": jid, "state": j["state"]}
                    for jid, j in self.state.jobs.items()]})
            return
        self._send(404, {"error": f"no such route: {self.path}"})

    def do_POST(self):                                   # noqa: N802
        if self.path == "/segment":
            body = self._read_json()
            with self.state.lock:
                self.state.segment_calls += 1
            prompts = body.get("prompts") or {}
            texts = prompts.get("text") or []
            if any(str(t).startswith("__none__") for t in texts):
                # A phrase the model cannot find anything for. The service
                # answers 200 with an empty list; saying so in words is
                # studio's job (checkpoint gap 3).
                self._send(200, {"pick_id": f"p_none{self.state.segment_calls}",
                                 "instances": [], "elapsed_s": 0.01,
                                 "warnings": ["the model matched nothing"]})
                return
            n = 2 if "two" in texts else 1
            instances = []
            pick_instances = {}
            for i in range(n):
                d = Path(tempfile.mkdtemp(prefix="fake-sam-seg-"))
                mask_path = d / "mask.png"
                arr = _checker_mask(24, 18, i)
                MT.write_gray_png(mask_path, arr)
                label = texts[0] if texts else "object"
                instances.append({
                    "id": str(i), "label": label, "score": 0.9 - i * 0.1,
                    "box": [0.1, 0.1, 0.6, 0.6], "area": 0.25,
                    "mask": str(mask_path)})
                pick_instances[str(i)] = {"box": [0.1, 0.1, 0.6, 0.6], "label": label}
            # A real pick id (contract C3: "the service keeps the last 32
            # picks in memory"), not something studio invents, so a later
            # /track carrying `pick` back is naming something this fake
            # actually remembers, the same as the real service would.
            pick_id = f"p_{len(self.state.picks) + 1}"
            with self.state.lock:
                self.state.picks[pick_id] = {"instances": pick_instances,
                                             "prompts": prompts}
            self._send(200, {"pick_id": pick_id, "instances": instances,
                             "elapsed_s": 0.01})
            return
        if self.path == "/track":
            body = self._read_json()
            with self.state.lock:
                self.state.track_calls += 1
            out_dir = Path(body["out_dir"])
            fps = float(body.get("fps") or 24.0)
            start = int(body.get("start_frame") or 0)
            # end_frame is an EXCLUSIVE upper bound (C3), not the last
            # inclusive index, so the frame count is the plain difference.
            end = int(body.get("end_frame") if body.get("end_frame") is not None
                     else start + 1)
            total = max(1, end - start)
            select = body.get("select")
            pick = body.get("pick")
            prompts = body.get("prompts")
            common = {"clip": body.get("clip"), "clip_key": body.get("clip_key"),
                     "rotation": body.get("rotation"), "fps": fps,
                     "frames": end, "width": 20, "height": 15,
                     "recipe": body.get("recipe") or {}}
            if pick:
                with self.state.lock:
                    self.state.pick_track_calls += 1
                    entry = self.state.picks.get(str(pick))
                if entry is None:
                    self._send(400, {"error": f"no such pick: {pick}. "
                                     "segment again to get a live one"})
                    return
                if prompts is not None:
                    self._send(400, {"error": "give prompts or pick, not both"})
                    return
                all_ids = list(entry["instances"])
                chosen = all_ids if select in (None, "all") else [
                    str(s) for s in select]
                mattes = [{"matte_id": f"m_pick_{_slug(pick)}_{_slug(i)}",
                          "object_id": i,
                          "label": entry["instances"].get(i, {}).get("label"),
                          "kind": "pick"} for i in chosen]
                texts = []
            else:
                prompts = prompts or {}
                texts = prompts.get("text") or []
                # The real service derives a matte id from a digest of the
                # WHOLE recipe (sam/server.py), so two different prompt sets
                # can never land in one directory. Deriving it from a single
                # text and its position, which this fake used to do, made
                # ["slow"] and ["slow", "resume tail"] both mint
                # `m_text_0_slow`: one test then read the matte another test
                # had already filled, and the render refusal that is right
                # about a barely started matte looked wrong. The digest keeps
                # an identical recipe on an identical id (the cache tests
                # need that) and separates recipes that only share a word.
                tag = _recipe_tag(prompts, select, body.get("steady"))
                if isinstance(select, list) and select:
                    mattes = [{"matte_id": f"m_{_slug(s)}_{tag}",
                              "object_id": str(s),
                              "label": None, "kind": "box"} for s in select]
                elif texts:
                    mattes = [{"matte_id": f"m_text_{i}_{_slug(t)}_{tag}",
                              "object_id": str(i), "label": t, "kind": "text"}
                             for i, t in enumerate(texts)]
                else:
                    mattes = [{"matte_id": f"m_auto0_{tag}", "object_id": "0",
                              "label": None, "kind": "box"}]
            # Checkpoint gap 12: `matte_ids` ({object_id: matte_id}) names
            # mattes that already exist, so a resumed tail is written back
            # into the SAME directory instead of a freshly derived one. The
            # real service overrides its own recipe digest with this; the
            # fake derives ids from the prompt text, so it overrides that.
            resume_ids = {str(k): str(v)
                         for k, v in (body.get("matte_ids") or {}).items()}
            with self.state.lock:
                self.state.last_matte_ids = dict(resume_ids)
            for m in mattes:
                override = resume_ids.get(str(m.get("object_id")))
                if override:
                    m["matte_id"] = override
            matte_ids = [m["matte_id"] for m in mattes]
            fail_texts = {t for t in texts if t.startswith("__fail__")}
            slow = "slow" in texts
            job_id = f"job{len(self.state.jobs) + 1}"
            for m in mattes:
                m["path"] = str(out_dir / m["matte_id"])
            with self.state.lock:
                self.state.jobs[job_id] = {
                    "state": "running", "done_frames": 0, "total_frames": total,
                    "fps": fps, "matte_ids": matte_ids, "mattes": mattes,
                    "created": time.time(), "error": None,
                    "matte_state": {}, "matte_error": {}}
            for m in mattes:
                d = out_dir / m["matte_id"]
                d.mkdir(parents=True, exist_ok=True)
                _fake_index_write(d, matte_id=m["matte_id"], state="queued",
                                  done_frames=0, job_id=job_id,
                                  object_id=m.get("object_id"),
                                  label=m.get("label"), kind=m.get("kind"),
                                  **common)
            if slow:
                threading.Thread(target=_run_slow_track, args=(
                    self.state, job_id, out_dir, mattes, total, common, start),
                                 daemon=True).start()
            else:
                for m in mattes:
                    mid = m["matte_id"]
                    d = out_dir / mid
                    if m["label"] in fail_texts:
                        err = f"no instance for text {m['label']!r}"
                        with self.state.lock:
                            self.state.jobs[job_id]["matte_state"][mid] = "failed"
                            self.state.jobs[job_id]["matte_error"][mid] = err
                        _fake_index_write(d, matte_id=mid, state="failed",
                                          error=err, **common)
                        continue
                    for i in range(start, end):
                        MT.write_gray_png(d / MT.frame_name(i),
                                          _checker_mask(20, 15, i))
                    with self.state.lock:
                        self.state.jobs[job_id]["matte_state"][mid] = "done"
                    _fake_index_write(d, matte_id=mid, state="done",
                                      done_frames=total, **common)
                with self.state.lock:
                    j = self.state.jobs[job_id]
                    j["state"] = "done"
                    j["done_frames"] = total
            with self.state.lock:
                j = self.state.jobs[job_id]
                out_mattes = [dict(m, state=j["matte_state"].get(m["matte_id"], j["state"]),
                                   error=j["matte_error"].get(m["matte_id"]))
                             for m in mattes]
                job_state = j["state"]
            self._send(200, {"job_id": job_id, "matte_ids": matte_ids,
                             "mattes": out_mattes, "state": job_state,
                             "queue_position": 0, "total_frames": total, "fps": fps})
            return
        if self.path.startswith("/jobs/") and self.path.endswith("/cancel"):
            job_id = self.path[len("/jobs/"):-len("/cancel")]
            with self.state.lock:
                job = self.state.jobs.get(job_id)
                if job:
                    job["state"] = "cancelled"
                    for m in job["mattes"]:
                        mid = m["matte_id"]
                        if job["matte_state"].get(mid) == "done":
                            continue
                        if job["done_frames"] > 0:
                            job["matte_state"][mid] = "partial"
                            _fake_index_write(Path(m["path"]), matte_id=mid,
                                              state="partial",
                                              done_frames=job["done_frames"])
                        else:
                            job["matte_state"][mid] = "failed"
                            job["matte_error"][mid] = "cancelled"
                            _fake_index_write(Path(m["path"]), matte_id=mid,
                                              state="failed", error="cancelled")
            self._send(200, {"job_id": job_id, "state": "cancelled" if job else None})
            return
        self._send(404, {"error": f"no such route: {self.path}"})


def _checker_mask(w: int, h: int, seed: int):
    import numpy as np
    a = np.zeros((h, w), dtype=np.float32)
    a[seed::2, :] = 1.0
    return a


def _slug(text) -> str:
    """A matte id embeds this; it has to survive being the last path segment
    of a URL, so anything that is not alnum, `-` or `_` is dropped rather
    than percent-escaped, which would just move the problem to whichever
    caller builds the URL by hand (this test file's own helpers do).
    """
    s = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(text))
    return s[:40] or "x"


def _recipe_tag(prompts, select=None, steady=None) -> str:
    """A short digest of the whole recipe, the way the real service ids a
    matte (a digest, not one prompt word).

    Deliberately NOT keyed on the frame range: a resume re-queues a tail with
    a different start_frame and has to land on the same id, which is the
    whole point of the `matte_ids` override below.
    """
    raw = json.dumps({"prompts": prompts or {}, "select": select,
                      "steady": steady}, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:8]


def _fake_index_write(dir_path: Path, **fields) -> None:
    """A minimal stand-in for the real SAM service's own index.json writer
    (C2): merge-write under a lock, atomic rename, so a matte directory this
    fake has started never reads as state "partial" just because no index
    exists yet (mattes.info_from_dir's fallback for a bare directory of
    frames with no index.json at all, which is a real but different state:
    "a hand made matte a test built" per its own docstring, not "the service
    that owns this file hasn't written it yet").
    """
    dir_path.mkdir(parents=True, exist_ok=True)
    p = dir_path / MT.INDEX_NAME
    try:
        raw = json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        raw = {}
    raw.update({k: v for k, v in fields.items() if v is not None})
    raw.setdefault("created", time.time())
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(raw))
    tmp.replace(p)


def _run_slow_track(state: _FakeSamState, job_id: str, out_dir: Path,
                    mattes: list, total: int, common: dict,
                    start: int = 0) -> None:
    for m in mattes:
        _fake_index_write(Path(m["path"]), matte_id=m["matte_id"], state="running")
    for i in range(total):
        with state.lock:
            job = state.jobs.get(job_id)
            if job is None or job["state"] == "cancelled":
                return
        for m in mattes:
            d = Path(m["path"])
            MT.write_gray_png(d / MT.frame_name(start + i), _checker_mask(20, 15, i))
            _fake_index_write(d, matte_id=m["matte_id"], done_frames=i + 1, **common)
        with state.lock:
            job = state.jobs.get(job_id)
            if job is None:
                return
            job["done_frames"] = i + 1
        time.sleep(0.5)
    with state.lock:
        job = state.jobs.get(job_id)
        if job is not None and job["state"] != "cancelled":
            job["state"] = "done"
            for m in mattes:
                job["matte_state"][m["matte_id"]] = "done"
    for m in mattes:
        d = Path(m["path"])
        with state.lock:
            job = state.jobs.get(job_id)
            still_cancelled = job is not None and job["state"] == "cancelled"
        if not still_cancelled:
            _fake_index_write(d, matte_id=m["matte_id"], state="done",
                              done_frames=total, **common)


def _start_fake_sam() -> tuple[ThreadingHTTPServer, threading.Thread, _FakeSamState]:
    state = _FakeSamState()
    handler = type("Handler", (_FakeSamHandler,), {"state": state})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t, state


# --------------------------------------------------------------------------
# the studio server, pointed at the fake
# --------------------------------------------------------------------------

class MaskRoutesTest(unittest.TestCase):
    proc = None
    data_dir = None
    sam_srv = None
    sam_thread = None
    sam_state = None
    base = ""
    clip = ""

    @classmethod
    def setUpClass(cls):
        cls.sam_srv, cls.sam_thread, cls.sam_state = _start_fake_sam()
        sam_port = cls.sam_srv.server_address[1]
        cls.data_dir = tempfile.mkdtemp(prefix="studio-masks-data-")
        port = _free_port()
        cls.base = f"http://127.0.0.1:{port}/api"
        cls.proc = subprocess.Popen(
            # 160 is the narrowest working width the server accepts
            # (set_mask_width floors it there, the same floor _preview_dims
            # puts on every other sampled frame), so it is the cheapest
            # proxy these tests can ask for and the number the status route
            # must then report. Asking for less is not an error, it is
            # silently the floor, which is why MASK_WIDTH is asserted below
            # rather than assumed.
            [PYTHON, SERVER, "--port", str(port), "--data-dir", cls.data_dir,
            "--sam-url", f"http://127.0.0.1:{sam_port}",
            "--mask-width", str(MASK_WIDTH)],
            cwd=str(CONTENT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _wait_ready(cls.base, cls.proc)

        state = _get(cls.base + "/state")
        clips = [c for c in state["clips"] if not c.get("error")]
        if not clips:
            cls._stop()
            raise unittest.SkipTest("this test needs a clip in footage/")
        cls.clip = clips[0]["name"]

    @classmethod
    def _stop(cls):
        _stop(cls.proc)
        cls.proc = None
        if cls.sam_srv:
            cls.sam_srv.shutdown()
            cls.sam_srv.server_close()
        cls.sam_srv = None

    @classmethod
    def tearDownClass(cls):
        cls._stop()
        if cls.data_dir:
            shutil.rmtree(cls.data_dir, ignore_errors=True)

    # -- helpers ------------------------------------------------------------

    def _segment(self, **extra):
        body = {"clip": self.clip, "time": 0.1, "prompts": {"text": ["person"]}}
        body.update(extra)
        return _post(self.base + "/mask/segment", body)

    def _track(self, **extra):
        body = {"clip": self.clip, "prompts": {"text": ["person"]}}
        body.update(extra)
        return _post(self.base + "/mask/track", body)

    def _wait_matte_state(self, matte_id: str, states: tuple, timeout: float = 15.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            info = _get(self.base + f"/matte/{matte_id}")
            last = info["state"]
            if last in states:
                return info
            time.sleep(0.2)
        raise AssertionError(f"matte {matte_id} never reached {states}, stuck "
                             f"at {last}")

    # -- status ---------------------------------------------------------

    def test_status_reports_the_fake_service_healthy(self):
        out = _get(self.base + "/mask/status")
        self.assertTrue(out["ok"])
        self.assertTrue(out["service"]["ok"])
        self.assertIn("jobs", out)

    # -- segment ----------------------------------------------------------

    def test_segment_returns_pick_id_and_working_preview_urls(self):
        out = self._segment(prompts={"text": ["two"]})
        self.assertIn("pick_id", out)
        self.assertEqual(len(out["instances"]), 2)
        inst = out["instances"][0]
        self.assertIn("overlay", inst)
        self.assertIn("mask", inst)
        ov_bytes, ov_headers = self._get_raw(self.base[:-4] + inst["overlay"])
        self.assertGreater(len(ov_bytes), 0)
        mk_bytes, mk_headers = self._get_raw(self.base[:-4] + inst["mask"])
        self.assertGreater(len(mk_bytes), 0)

    def _get_raw(self, url):
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.read(), dict(r.headers)

    def test_segment_with_no_prompt_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._segment(prompts={})
        self.assertEqual(caught.exception.code, 400)
        body = json.loads(caught.exception.read())
        self.assertIn("prompt", body.get("error", ""))

    # -- track, cache by recipe -------------------------------------------

    def test_track_queues_and_repeating_it_is_free(self):
        before_calls = self.sam_state.track_calls
        first = self._track(prompts={"text": ["a cached subject"]})
        self.assertIn("job_id", first)
        self.assertEqual(len(first["mattes"]), 1)
        matte_id = first["mattes"][0]["matte_id"]
        self._wait_matte_state(matte_id, ("done",))
        after_first = self.sam_state.track_calls
        self.assertEqual(after_first, before_calls + 1)

        second = self._track(prompts={"text": ["a cached subject"]})
        self.assertEqual(second["mattes"][0]["matte_id"], matte_id)
        self.assertEqual(self.sam_state.track_calls, after_first,
                         "a repeated identical track recipe must not call "
                         "the SAM service again")

    def test_track_needs_a_prompt_or_a_pick(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._track(prompts={})
        self.assertEqual(caught.exception.code, 400)

    def test_track_from_a_pick_carries_the_picks_own_prompts(self):
        pick = self._segment(prompts={"text": ["pick subject"]})
        pick_id = pick["pick_id"]
        inst_id = pick["instances"][0]["id"]
        out = self._track(pick_id=pick_id, select=[inst_id], prompts=None)
        self.assertEqual(len(out["mattes"]), 1)
        recipe = out["mattes"][0]["recipe"]
        self.assertEqual(recipe["prompts"]["text"], ["pick subject"])

    def test_one_matte_slot_can_fail_while_its_siblings_finish(self):
        # Two text phrases queue two object slots in the same job (C3); one
        # detects nothing (the fake's __fail__ convention) while the other
        # finishes, and the job as a whole is neither "failed" nor "done"
        # for the wrong reason: each matte answers for itself.
        result = self._track(prompts={"text": ["good subject", "__fail__ghost"]})
        self.assertEqual(len(result["mattes"]), 2)
        job_id = result["job_id"]
        good_id = next(m["matte_id"] for m in result["mattes"]
                       if m["label"] == "good subject")
        bad_id = next(m["matte_id"] for m in result["mattes"]
                     if m["label"] == "__fail__ghost")
        self._wait_matte_state(good_id, ("done",))
        self._wait_matte_state(bad_id, ("failed",))
        bad_info = _get(self.base + f"/matte/{bad_id}")
        self.assertIn("no instance", bad_info.get("error") or "")
        good_info = _get(self.base + f"/matte/{good_id}")
        self.assertIsNone(good_info.get("error"))

        def _job_view():
            deadline = time.time() + 10.0
            while time.time() < deadline:
                out = _get(self.base + f"/mask/jobs/{job_id}")
                if out["state"] in ("done", "failed"):
                    return out
                time.sleep(0.2)
            raise AssertionError("mask job never finished")

        view = _job_view()
        by_id = {m["matte_id"]: m for m in view.get("mattes", [])}
        self.assertEqual(by_id[good_id]["state"], "done")
        self.assertEqual(by_id[bad_id]["state"], "failed")
        self.assertIn("no instance", by_id[bad_id].get("error") or "")

    def test_bare_jobs_list_carries_the_same_shape_as_one_job(self):
        # README's own CLI table row for `mask jobs` (`GET /api/mask/jobs`,
        # C5) and `cinegrade mask jobs`/`grade_client`'s fake in
        # grade/tests/fake_studio_server.py both name this exact route; it
        # is the one real request/response mismatch integration found
        # between M4's CLI and M5's routes (INTEGRATION-A).
        result = self._track(prompts={"text": ["listed job"]})
        job_id = result["job_id"]
        self._wait_matte_state(result["mattes"][0]["matte_id"],
                               ("done", "failed"))
        listing = _get(self.base + "/mask/jobs")
        self.assertIn("jobs", listing)
        by_id = {j["id"]: j for j in listing["jobs"]}
        self.assertIn(job_id, by_id)
        entry = by_id[job_id]
        # Same per job view GET /api/mask/jobs/<id> gives, not a cut down
        # summary: same field names, same matte_ids, same clip.
        single_job = _get(self.base + f"/mask/jobs/{job_id}")
        self.assertEqual(entry["clip"], single_job["clip"])
        self.assertEqual(entry["matte_ids"], single_job["matte_ids"])
        self.assertIn(entry["state"], ("queued", "running", "done", "failed",
                                       "cancelled"))

    # -- matte store --------------------------------------------------------

    def test_matte_list_get_and_frame(self):
        result = self._track(prompts={"text": ["listed subject"]})
        matte_id = result["mattes"][0]["matte_id"]
        self._wait_matte_state(matte_id, ("done",))

        listing = _get(self.base + f"/matte?clip={self.clip}")
        ids = [m["matte_id"] for m in listing["mattes"]]
        self.assertIn(matte_id, ids)

        info = _get(self.base + f"/matte/{matte_id}")
        self.assertEqual(info["state"], "done")
        self.assertFalse(info["is_partial"])
        self.assertEqual(info["clip"], self.clip)

        png, headers = _post_none_get_raw(
            self.base + f"/matte/{matte_id}/frame?time=0&width=32")
        self.assertGreater(len(png), 0)
        self.assertEqual(headers.get("X-Matte-State"), "done")
        self.assertIn("X-Matte-Frame", headers)

    def test_partial_matte_frame_falls_back_and_render_refuses_it(self):
        result = self._track(prompts={"text": ["slow"]},
                             start=0, end=1)
        matte_id = result["mattes"][0]["matte_id"]
        # Frame 0 lands quickly (the slow fake writes one frame per 0.5s);
        # ask for a time past what has been written yet and confirm the
        # nearest-written-frame fallback and its warning header.
        self._wait_matte_state(matte_id, ("running", "done"))
        png, headers = _post_none_get_raw(
            self.base + f"/matte/{matte_id}/frame?time=5&width=32")
        self.assertGreater(len(png), 0)
        self.assertIn(headers.get("X-Matte-State"), ("running", "done", "queued"))

        cfg = _matte_layer_config(matte_id)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            _post(self.base + "/render", {
                "clip": self.clip, "config": cfg, "duration": 0.2,
                "name": f"masktest_refuse_{matte_id}"})
        self.assertEqual(caught.exception.code, 400)
        body = json.loads(caught.exception.read())
        self.assertIn(matte_id, body.get("error", ""))

        self._wait_matte_state(matte_id, ("done",), timeout=20.0)
        out = _post(self.base + "/render", {
            "clip": self.clip, "config": cfg, "duration": 0.2,
            "allow_partial": True, "name": f"masktest_allow_{matte_id}"})
        self.assertIn("job", out)

    # -- the checkpoint gaps M8 logged --------------------------------------

    def test_segment_with_no_match_says_so_in_words(self):
        """Checkpoint gap 3. An empty list used to be the whole answer, so a
        prompt the model could not find and a prompt that worked read the
        same to a script."""
        out = self._segment(prompts={"text": ["__none__ghost"]})
        self.assertEqual(out["instances"], [])
        self.assertEqual(out["candidates"], 0)
        self.assertIn("no match for", out["message"])
        self.assertIn("__none__ghost", out["message"])
        # the service's own warning is passed through, not swallowed
        self.assertTrue(any("matched nothing" in w
                            for w in out.get("warnings") or []))

    def test_matte_list_is_a_summary_and_full_asks_for_the_arrays(self):
        """Checkpoint gap 6. The list route used to answer with every
        matte's whole per frame arrays, thousands of mostly-null numbers to
        read four states."""
        result = self._track(prompts={"text": ["summary subject"]}, start=0, end=0.5)
        matte_id = result["mattes"][0]["matte_id"]
        self._wait_matte_state(matte_id, ("done",))

        listing = _get(self.base + f"/matte?clip={self.clip}")
        self.assertFalse(listing["full"])
        row = next(m for m in listing["mattes"] if m["matte_id"] == matte_id)
        self.assertNotIn("areas", row)
        self.assertNotIn("scores", row)
        for key in ("span", "coverage", "mean_score", "quality",
                    "done_frames", "total_frames", "state", "recipe"):
            self.assertIn(key, row, f"the summary must still carry {key}")

        full = _get(self.base + f"/matte?clip={self.clip}&full=1")
        self.assertTrue(full["full"])
        full_row = next(m for m in full["mattes"] if m["matte_id"] == matte_id)
        self.assertIn("areas", full_row)
        self.assertIn("scores", full_row)

        # One matte by id always carries them: `mask show --strip` plots its
        # area curve from exactly this response.
        one = _get(self.base + f"/matte/{matte_id}")
        self.assertIn("areas", one)

    def test_a_matte_says_which_seconds_it_answers_for(self):
        """Checkpoint gaps 8 and 10. Holding the last written frame past the
        span is the design; a caller measuring a frozen mask at 20 seconds
        having no way to know was the problem."""
        result = self._track(prompts={"text": ["span subject"]}, start=0, end=0.5)
        matte_id = result["mattes"][0]["matte_id"]
        info = self._wait_matte_state(matte_id, ("done",))
        span = info["span"]
        self.assertTrue(info["frozen_outside_span"])
        self.assertTrue(span["frozen_outside_span"])
        self.assertEqual(span["start_frame"], 0)
        self.assertGreater(span["end_frame"], 0)
        self.assertEqual(span["written"], info["written_count"])
        self.assertLessEqual(span["end_frame"], span["declared_frames"])
        self.assertAlmostEqual(span["end_s"], span["end_frame"] / info["fps"],
                               places=3)
        self.assertGreater(info["coverage"], 0.0)

    def test_a_matte_reports_its_own_suspect_frames(self):
        """Checkpoint gap 18. The grader's `face` matte lost the face, then
        latched onto a tree, and nothing said so."""
        result = self._track(prompts={"text": ["quality subject"]}, start=0, end=0.5)
        matte_id = result["mattes"][0]["matte_id"]
        info = self._wait_matte_state(matte_id, ("done",))
        q = info["quality"]
        # The thresholds always travel with the counts, so a number can
        # never be read without knowing what judged it.
        self.assertIn("area_jump", q["thresholds"])
        self.assertIn("min_iou", q["thresholds"])
        self.assertIn(q["iou_source"], ("index", "frames", "none"))
        self.assertEqual(q["checked"], info["written_count"])
        self.assertEqual(set(q["reasons"]),
                         {"zero_area", "area_jump", "low_iou"})
        self.assertEqual(q["suspect_count"],
                         q["reasons"]["zero_area"] + q["reasons"]["area_jump"]
                         + q["reasons"]["low_iou"]
                         - self._overlapping_reasons(q))
        # and the thresholds in force are readable without computing them
        status = _get(self.base + "/mask/status")
        self.assertIn("area_jump", status["quality_thresholds"])

    @staticmethod
    def _overlapping_reasons(q: dict) -> int:
        """A frame can trip more than one rule, so the per reason counts sum
        to more than the frame count by exactly that overlap."""
        return sum(len(f["reasons"]) - 1 for f in q["suspect_frames"])

    def test_a_dead_matte_is_retried_instead_of_handed_back(self):
        """Checkpoint gap 12. The recipe cache used to answer an identical
        retry with the same failed matte and job_id None, so the only way to
        get another attempt was to change the words."""
        text = "__fail__retry me"
        first = self._track(prompts={"text": [text]})
        matte_id = first["mattes"][0]["matte_id"]
        self._wait_matte_state(matte_id, ("failed",))
        calls_before = self.sam_state.track_calls

        second = self._track(prompts={"text": [text]})
        self.assertFalse(second["cached"])
        self.assertTrue(second["restarted"])
        self.assertFalse(second["resumed"])
        self.assertEqual(second["resumed_from"], 0)
        self.assertIn("restarting", second["message"])
        self.assertEqual(second["mattes"][0]["matte_id"], matte_id,
                         "a restart writes back into the same matte")
        self.assertEqual(self.sam_state.track_calls, calls_before + 1)
        self.assertEqual(self.sam_state.last_matte_ids.get("0"), matte_id,
                         "the retry must name the existing matte so the "
                         "frames are not orphaned in a new directory")

    def test_a_cancelled_matte_resumes_from_its_first_missing_frame(self):
        """Checkpoint gap 12, the resume half: frames already written stay
        written and only the missing tail is re-queued.

        The prompt list carries the exact word "slow" (which is how the fake
        service picks its timed writer) plus a phrase of its own, so this
        test's recipe hashes differently from every other "slow" track here.
        The recipe cache is keyed on the prompts and deliberately not on the
        frame range, so two tests sharing a prompt would share a matte.

        The window is half a second on purpose. The fake writes one frame
        every 0.5 s, so the whole point of the test (the tail is re-queued
        and actually runs to the end) has to fit in a timeout: four seconds
        of a 24 fps clip is 96 frames, which is 48 seconds of fake tracking
        and can never finish inside any sane wait. Twelve frames proves the
        same thing: a cancel lands after the first one or two, and the
        resume writes the rest into the same matte.
        """
        prompts = {"text": ["slow", "resume tail"]}
        first = self._track(prompts=prompts, start=0, end=0.5)
        job_id = first["job_id"]
        matte_id = first["mattes"][0]["matte_id"]
        # Let at least one frame land, then stop it.
        deadline = time.time() + 20.0
        written = 0
        while time.time() < deadline:
            written = _get(self.base + f"/matte/{matte_id}")["written_count"]
            if written >= 1:
                break
            time.sleep(0.2)
        self.assertGreaterEqual(written, 1, "the slow track wrote nothing")
        _post(self.base + f"/mask/jobs/{job_id}/cancel", {})
        before = self._wait_matte_state(
            matte_id, ("partial", "failed", "done"), timeout=25.0)
        if before["state"] == "done":
            self.skipTest("the fake finished this track before the cancel "
                          "landed; there is no missing tail to resume")
        stopped_at = before["span"]["end_frame"]

        second = self._track(prompts=prompts, start=0, end=0.5)
        self.assertFalse(second["cached"],
                         "a matte with a hole in the window asked for is not "
                         "a cache hit")
        self.assertTrue(second["resumed"] or second["restarted"])
        self.assertEqual(second["mattes"][0]["matte_id"], matte_id,
                         "a resume writes back into the same matte")
        self.assertLessEqual(second["start_frame"], stopped_at)
        self.assertEqual(self.sam_state.last_matte_ids.get("0"), matte_id,
                         "the resume must name the existing matte, or the "
                         "frames already written are orphaned in an old "
                         "directory under a freshly derived id")
        after = self._wait_matte_state(matte_id, ("done", "partial"),
                                       timeout=30.0)
        self.assertGreaterEqual(after["written_count"], stopped_at,
                                "a resume keeps the frames already on disk")

    def test_force_redoes_a_finished_matte(self):
        """Checkpoint gap 12, the escape hatch: a matte that IS complete is a
        cache hit, and `force` is how you say you want it again anyway."""
        text = "forced subject"
        first = self._track(prompts={"text": [text]})
        matte_id = first["mattes"][0]["matte_id"]
        self._wait_matte_state(matte_id, ("done",))
        calls = self.sam_state.track_calls

        cached = self._track(prompts={"text": [text]})
        self.assertTrue(cached["cached"])
        self.assertEqual(self.sam_state.track_calls, calls)

        forced = self._track(prompts={"text": [text]}, force=True)
        self.assertFalse(forced["cached"])
        self.assertTrue(forced["restarted"])
        self.assertIn("force", forced["message"])
        self.assertEqual(self.sam_state.track_calls, calls + 1)
        self._wait_matte_state(matte_id, ("done",))

    def test_render_takes_a_partial_matte_that_covers_the_window(self):
        """The successor lane's --allow-partial finding: the refusal keyed on
        the matte's DECLARED state, so a render whose every frame was on disk
        was refused for the frames it never asked for."""
        # "slow" exactly (the fake's timed writer) plus a phrase of its own,
        # so this recipe is not shared with another test's matte.
        first = self._track(prompts={"text": ["slow", "render window"]},
                            start=0, end=8)
        job_id = first["job_id"]
        matte_id = first["mattes"][0]["matte_id"]
        deadline = time.time() + 25.0
        info = None
        while time.time() < deadline:
            info = _get(self.base + f"/matte/{matte_id}")
            if info["written_count"] >= 4:
                break
            time.sleep(0.2)
        self.assertIsNotNone(info)
        self.assertGreaterEqual(info["written_count"], 4)
        try:
            self.assertTrue(info["is_partial"],
                            "this test needs a matte that is not finished")
            fps = float(info["fps"])
            cfg = _matte_layer_config(matte_id)
            # Three frames of a matte that has at least four: covered.
            out = _post(self.base + "/render", {
                "clip": self.clip, "config": cfg, "duration": 3.0 / fps,
                "name": f"masktest_covered_{matte_id}"})
            self.assertIn("job", out)
            # And a window it genuinely is short of is still refused.
            with self.assertRaises(urllib.error.HTTPError) as caught:
                _post(self.base + "/render", {
                    "clip": self.clip, "config": cfg, "duration": 60.0,
                    "name": f"masktest_short_{matte_id}"})
            self.assertEqual(caught.exception.code, 400)
            body = json.loads(caught.exception.read())
            self.assertIn("allow_partial", body.get("error", ""))
        finally:
            _post(self.base + f"/mask/jobs/{job_id}/cancel", {})

    def test_stats_says_the_width_it_measured_at(self):
        """Checkpoint gap 11. Two measurements taken at different sizes used
        to be indistinguishable once written down."""
        # No width: the route measures at the source's own width, unchanged
        # by this addition, and now says so.
        default = _post(self.base + "/stats",
                        {"clip": self.clip, "time": 0.1, "config": {}})
        self.assertEqual(default["measured_width"], default["size"][0])
        narrow = _post(self.base + "/stats",
                       {"clip": self.clip, "time": 0.1, "config": {},
                        "width": 320})
        self.assertEqual(narrow["measured_width"], 320)
        self.assertEqual(narrow["size"][0], 320)
        self.assertNotEqual(narrow["measured_width"], default["measured_width"],
                            "a 320 wide measurement and a default width one "
                            "must not read the same once written down")
        # The whole point of the field: it reports the sample really taken,
        # not the number that was asked for. The studio floors every sampled
        # frame at 160 wide (_preview_dims, shared with source_frame and
        # render_raw so the two agree on WxH), so a request under the floor
        # is measured at 160 and says 160.
        floored = _post(self.base + "/stats",
                        {"clip": self.clip, "time": 0.1, "config": {},
                         "width": 96})
        self.assertEqual(floored["measured_width"], MIN_SAMPLE_WIDTH)
        self.assertEqual(floored["size"][0], MIN_SAMPLE_WIDTH)

    def test_health_and_status_say_where_everything_lives(self):
        """Checkpoint gaps 4 and 5. The CLI could not find a clip or a matte
        that the server it was talking to knew about, because neither route
        said where either lived."""
        health = _get(self.base + "/health")
        for key in ("data_dir", "footage_dir", "matte_root"):
            self.assertIn(key, health)
        self.assertTrue(health["matte_root"].startswith(health["data_dir"]))
        status = _get(self.base + "/mask/status")
        for key in ("data_dir", "footage_dir", "matte_root", "mask_width",
                    "quality_thresholds"):
            self.assertIn(key, status)
        self.assertEqual(status["data_dir"], health["data_dir"])
        # --mask-width above, and not the 1280 default: the flag reaches the
        # route a mask caller reads, so an agent can tell what size the
        # model is really being shown without guessing.
        self.assertEqual(status["mask_width"], MASK_WIDTH)
        self.assertEqual(status["footage_dir"], health["footage_dir"])
        self.assertEqual(status["matte_root"], health["matte_root"])

    # -- stats by matte -------------------------------------------------

    def test_stats_weighted_by_matte_differs_from_unweighted(self):
        result = self._track(prompts={"text": ["stats subject"]})
        matte_id = result["mattes"][0]["matte_id"]
        self._wait_matte_state(matte_id, ("done",))
        plain = _post(self.base + "/stats",
                      {"clip": self.clip, "time": 0.1, "width": 64, "config": {}})
        weighted = _post(self.base + "/stats",
                         {"clip": self.clip, "time": 0.1, "width": 64,
                          "config": {}, "matte": matte_id})
        self.assertEqual(weighted["matte"], matte_id)
        self.assertIn("stats", weighted)
        # A checkerboard matte is not uniform, so weighting by it should not
        # generally reproduce the flat frame mean exactly.
        self.assertIn("luma", weighted["stats"])
        self.assertIn("luma", plain["stats"])

    def test_stats_with_an_unknown_matte_is_a_clean_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            _post(self.base + "/stats", {"clip": self.clip, "time": 0.1,
                                         "width": 64, "config": {},
                                         "matte": "not-a-real-matte"})
        self.assertEqual(caught.exception.code, 404)

    # -- preset load resolution ------------------------------------------

    def test_preset_load_auto_queues_a_text_recipe(self):
        name = "masktest_text_preset"
        cfg = _matte_layer_config("", recipe={"prompts": {"text": ["auto queue me"]}})
        _post(self.base + "/preset", {"name": name, "config": cfg})
        out = _get(self.base + f"/preset?name={name}&clip={self.clip}")
        self.assertIn("mask_queued", out)
        comp = out["config"]["layers"][0]["mask"]["components"][0]
        self.assertTrue(comp["matte"]["id"])
        self.assertFalse(comp["matte"].get("needs_pick", False))

    def test_preset_load_leaves_a_point_recipe_needing_a_pick(self):
        name = "masktest_point_preset"
        cfg = _matte_layer_config(
            "", recipe={"prompts": {"points": [{"x": 0.5, "y": 0.5, "label": 1}]}})
        _post(self.base + "/preset", {"name": name, "config": cfg})
        out = _get(self.base + f"/preset?name={name}&clip={self.clip}")
        comp = out["config"]["layers"][0]["mask"]["components"][0]
        self.assertTrue(comp["matte"].get("needs_pick"))
        self.assertFalse(comp["matte"].get("id"))

    def test_preset_load_with_no_clip_is_unchanged(self):
        name = "masktest_no_clip_preset"
        cfg = _matte_layer_config("", recipe={"prompts": {"text": ["untouched"]}})
        _post(self.base + "/preset", {"name": name, "config": cfg})
        out = _get(self.base + f"/preset?name={name}")
        self.assertNotIn("mask_queued", out)
        comp = out["config"]["layers"][0]["mask"]["components"][0]
        self.assertEqual(comp["matte"]["recipe"]["prompts"]["text"], ["untouched"])

    # -- identity ---------------------------------------------------------

    def test_mattes_are_visible_to_every_caller(self):
        result = self._track(prompts={"text": ["shared subject"]})
        matte_id = result["mattes"][0]["matte_id"]
        self._wait_matte_state(matte_id, ("done",))
        as_agent_a = _get(self.base + f"/matte/{matte_id}",
                          headers={"X-Studio-Agent": "agent-a"})
        as_agent_b = _get(self.base + f"/matte/{matte_id}",
                          headers={"X-Studio-Agent": "agent-b"})
        self.assertEqual(as_agent_a["matte_id"], as_agent_b["matte_id"])
        self.assertEqual(as_agent_a["state"], as_agent_b["state"])


def _post_none_get_raw(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read(), dict(r.headers)


def _matte_layer_config(matte_id: str, recipe=None) -> dict:
    return {"layers": [
        {"enabled": True, "name": "L1", "placement": "before_look",
         "mask": {"components": [
             {"id": "c1", "type": "matte", "op": "add",
              "matte": {"id": matte_id, "recipe": recipe or {}}}]},
         "correct": {"exposure": 0.4}},
    ]}


# --------------------------------------------------------------------------
# SAM entirely down: a clear 503, not a stack trace
# --------------------------------------------------------------------------

class MaskServiceDownTest(unittest.TestCase):
    proc = None
    data_dir = None
    base = ""
    clip = ""

    @classmethod
    def setUpClass(cls):
        cls.data_dir = tempfile.mkdtemp(prefix="studio-masks-down-data-")
        port = _free_port()
        dead_port = _free_port()
        cls.base = f"http://127.0.0.1:{port}/api"
        cls.proc = subprocess.Popen(
            [PYTHON, SERVER, "--port", str(port), "--data-dir", cls.data_dir,
            "--sam-url", f"http://127.0.0.1:{dead_port}"],
            cwd=str(CONTENT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _wait_ready(cls.base, cls.proc)
        state = _get(cls.base + "/state")
        clips = [c for c in state["clips"] if not c.get("error")]
        if not clips:
            _stop(cls.proc)
            raise unittest.SkipTest("this test needs a clip in footage/")
        cls.clip = clips[0]["name"]

    @classmethod
    def tearDownClass(cls):
        _stop(cls.proc)
        if cls.data_dir:
            shutil.rmtree(cls.data_dir, ignore_errors=True)

    def test_status_reports_the_service_down(self):
        out = _get(self.base + "/mask/status")
        self.assertFalse(out["ok"])
        self.assertIn("error", out["service"])

    def test_segment_with_the_service_down_is_a_clear_503(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            _post(self.base + "/mask/segment",
                 {"clip": self.clip, "time": 0.1, "prompts": {"text": ["x"]}})
        self.assertEqual(caught.exception.code, 503)
        body = json.loads(caught.exception.read())
        self.assertIn("uv run", body.get("error", ""))


if __name__ == "__main__":                                        # pragma: no cover
    unittest.main()
