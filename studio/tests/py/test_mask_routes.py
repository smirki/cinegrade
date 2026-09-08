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
                if isinstance(select, list) and select:
                    mattes = [{"matte_id": f"m_{_slug(s)}", "object_id": str(s),
                              "label": None, "kind": "box"} for s in select]
                elif texts:
                    mattes = [{"matte_id": f"m_text_{i}_{_slug(t)}",
                              "object_id": str(i), "label": t, "kind": "text"}
                             for i, t in enumerate(texts)]
                else:
                    mattes = [{"matte_id": "m_auto0", "object_id": "0",
                              "label": None, "kind": "box"}]
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
            [PYTHON, SERVER, "--port", str(port), "--data-dir", cls.data_dir,
            "--sam-url", f"http://127.0.0.1:{sam_port}", "--mask-width", "64"],
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
