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
  stats by mask    POST /api/stats with `mask` (a whole component stack, gap
                   19) folds it with the engine's own mask_matte: a one
                   component stack measures exactly what `matte` measures,
                   an intersect narrows it, coverage and mask_mattes come
                   back on the row, a stack that covers nothing is a
                   no_coverage row rather than a 400, and mask alongside
                   matte or region, a mask that is not an object, a no-op
                   mask and an unknown matte id inside a stack are all
                   refused.
  generated caches the LUTs this server bakes (layers, masks, slice) land
                   under its own --data-dir cache and not in grade/luts
                   (gap 22), so two runs on one clip cannot collide.
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

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(GRADE))
import numpy as np                                            # noqa: E402
import mattes as MT                                           # noqa: E402
# Round 1 finding 20: this file's own list avoided 7431, 7614 and 7615 and not
# 7560 or 7632, the stub service suite's list avoided a different four, and
# neither covered the port a live agent seat's studio was actually on. There
# is one list now (studio/tests/forbidden-ports.json) and one picker.
from ports import FORBIDDEN_PORTS, free_port as _free_port    # noqa: E402,F401


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
        # Per matte directory, per frame: area, score, IoU and the mask that
        # produced them, so index.json can carry the real arrays the service
        # writes (contract C2) instead of nothing at all.
        self.frame_stats: dict[str, dict] = {}
        # job_id to an Event a timed track waits on before writing past
        # `HOLD_AFTER` frames. Round 1 finding 34: without this the cancel
        # test raced the writer, so it accepted "resumed OR restarted" and
        # skipped itself whenever the track happened to finish first, which
        # meant the resume path it exists to prove was never asserted.
        self.gates: dict[str, threading.Event] = {}
        # The working width and rotation the last /track carried. Design rule
        # 5 keys a track by clip, rotation, working width and recipe; round 1
        # finding 40 found nothing that proved the last two were on the wire
        # at all, let alone in the key.
        self.last_track_width = None
        self.last_track_rotation = None

    def release_gates(self) -> None:
        """Let every held track run to the end (called from the test)."""
        with self.lock:
            gates = list(self.gates.values())
        for g in gates:
            g.set()


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
                arr = _fake_mask(24, 18, i)
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
            if any(str(t).startswith("__escape__") for t in texts):
                # Round 2 finding 65: a service that answers with ids that are
                # PATHS. Studio joins pick_id and an instance id into a file
                # name under MASK_PICK_CACHE, and `Path("/a") / "/b"` is
                # `Path("/b")`, so an absolute id relocates the write.
                out = [dict(instances[0], id="/tmp/fixxr-escaped-inst")]
                if len(instances) > 1:
                    out.append(dict(instances[1], id="../../evil"))
                self._send(200, {"pick_id": "../../escaped-pick",
                                 "instances": out, "elapsed_s": 0.01})
                return
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
                tag = _recipe_tag(prompts, select, body.get("steady"),
                                  body.get("clip_key"), body.get("rotation"),
                                  body.get("width"))
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
                self.state.last_track_width = body.get("width")
                self.state.last_track_rotation = body.get("rotation")
            for m in mattes:
                override = resume_ids.get(str(m.get("object_id")))
                if override:
                    m["matte_id"] = override
            if any(str(t).startswith("__escape__") for t in texts):
                # The same shape on the other route: a matte id that is a
                # path. `_matte_dir` is matte_root() / clip_key / matte_id.
                mattes = [dict(mattes[0], matte_id="../../escaped-matte")]
                matte_ids = [m["matte_id"] for m in mattes]
                self._send(200, {"job_id": "job_escape", "state": "queued",
                                 "mattes": mattes, "matte_ids": matte_ids,
                                 "total_frames": total, "fps": fps})
                return
            matte_ids = [m["matte_id"] for m in mattes]
            fail_texts = {t for t in texts if t.startswith("__fail__")}
            slow = "slow" in texts
            # Marker words that choose the mask content this track writes
            # (see _fake_mask): a track whose frames really drift, one that
            # covers everything, one that covers half. Markers rather than
            # separate routes because the content has to arrive through the
            # ordinary /track path the studio uses (round 1 findings 15 and
            # 17).
            kind = "normal"
            for mark in ("drift", "full", "half"):
                if any(str(t).startswith(f"__{mark}__") for t in texts):
                    kind = mark
                    break
            hold = "hold" in texts
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
                gate = None
                if hold:
                    gate = threading.Event()
                    with self.state.lock:
                        self.state.gates[job_id] = gate
                threading.Thread(target=_run_slow_track, args=(
                    self.state, job_id, out_dir, mattes, total, common, start,
                    kind, gate), daemon=True).start()
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
                        _write_frame(self.state, d, i,
                                     _fake_mask(20, 15, i, kind=kind))
                    with self.state.lock:
                        self.state.jobs[job_id]["matte_state"][mid] = "done"
                    # Counted off the directory, not `total`, because the real
                    # store recounts too (sam/store.py `_carry_forward`): a
                    # resume or a widen writes a tail of 12 frames into a
                    # matte that now holds 18, and reporting 12 would make a
                    # correct widen look like a matte that lost its head.
                    on_disk = len([q for q in d.glob("*.png") if q.stem.isdigit()])
                    _fake_index_write(d, matte_id=mid, state="done",
                                      done_frames=on_disk, **common,
                                      **_frame_arrays(self.state, d,
                                                      common.get("frames")))
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
                gate = self.state.gates.get(job_id)
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
            # A held writer is waiting on its gate; wake it so it notices the
            # cancel now rather than at the end of its bounded wait.
            if gate is not None:
                gate.set()
            self._send(200, {"job_id": job_id, "state": "cancelled" if job else None})
            return
        self._send(404, {"error": f"no such route: {self.path}"})


# Where the injected drift lands in a `__drift__` track, as offsets from
# frame 0. Named here because the test that reads them names them too, and a
# number that has to agree in two places should be one number.
DRIFT_LOST = 2                 # the subject is gone: nothing tracked at all
DRIFT_ELSEWHERE = 4            # the same size, somewhere else entirely
DRIFT_LATCH = 6                # the whole frame: latched onto the background


def _fake_mask(w: int, h: int, index: int, kind: str = "normal"):
    """The mask this fake writes for one frame of a track.

    Alternate rows of the whole width (a matte that is not uniform, which is
    what the stats tests weight by) plus one odd row that moves with the frame
    number, so consecutive frames overlap the way a tracked subject does:
    the area holds steady at 9 rows of 15, and the intersection over union
    with the frame before it is 8/10.

    This used to be `a[index::2] = 1`, which had two properties nobody had
    reason to notice while the fake wrote no per frame arrays: every even
    frame was DISJOINT from the odd frame beside it (an IoU of 0.0, which the
    drift rule calls a lost track), and every frame past the frame height was
    entirely EMPTY (an area of 0.0, which the drift rule calls a lost
    subject). Round 1 finding 15 asked for those arrays to be real, and
    against the old content every frame of every matte in this suite would
    have come back flagged.

    `kind` is how a test asks for content it can check by hand, chosen by a
    marker word in the track's own prompts so it travels the ordinary /track
    path rather than a back door:

      "drift"  the three failures the quality rules exist to catch, at the
               fixed offsets above:
                 frame 2  nothing at all    the subject is lost
                 frame 4  the other rows    the same size, one row of overlap
                 frame 6  the whole frame   six tenths of the frame to all
      "full"   every pixel, every frame. Weighting a measurement by this
               matte has to reproduce the unweighted measurement EXACTLY,
               which is the one comparison that cannot pass by accident.
      "half"   the top half of the frame, every frame, so a weighted
               measurement has something real to differ from.
    """
    a = np.zeros((h, w), dtype=np.float32)
    odd = list(range(1, h, 2))
    if kind == "full":
        a[:, :] = 1.0
        return a
    if kind == "half":
        a[:max(1, h // 2), :] = 1.0
        return a
    if kind == "drift" and index == DRIFT_LOST:
        return a
    if kind == "drift" and index == DRIFT_ELSEWHERE:
        a[1::2, :] = 1.0
        return a
    if kind == "drift" and index == DRIFT_LATCH:
        a[:, :] = 1.0
        return a
    a[0::2, :] = 1.0
    if odd:
        a[odd[index % len(odd)], :] = 1.0
    return a


def _write_frame(state: "_FakeSamState", dir_path: Path, index: int, arr,
                 score: float = 0.9) -> None:
    """Write one matte frame AND the per frame numbers that go with it.

    A mirror of sam/store.py's `MatteWriter._write`: the area is the mean of
    the clipped mask, the score is the model's own confidence, and the IoU is
    the overlap with the frame written before it, all rounded the way the real
    store rounds them. Round 1 finding 15: this fake wrote none of the three,
    so `quality()` through the real route was always judging an empty `areas`
    list and could only ever answer "nothing suspect" no matter what the
    frames held. Every quality assertion in this file was written against
    that.

    One deliberate difference from the store: the store keeps the previous
    mask on the writer object, so the first frame of a resumed tail has no
    predecessor and gets no IoU. This keeps the numbers per matte DIRECTORY
    and compares against the previous frame index, so a resumed tail's first
    frame is compared with the frame before it on disk. That is the same
    answer on a track that runs straight through, and a better one on a
    resume, and it means a widen cannot silently drop a matte's IoU curve.
    """
    MT.write_gray_png(dir_path / MT.frame_name(index), arr)
    cur = np.asarray(arr) >= 0.5
    with state.lock:
        rec = state.frame_stats.setdefault(str(dir_path), {})
        earlier = [i for i in rec if i < index]
        iou = None
        if earlier:
            prev = rec[max(earlier)]["mask"]
            if prev.shape == cur.shape:
                union = float(np.logical_or(prev, cur).sum())
                inter = float(np.logical_and(prev, cur).sum())
                iou = round(inter / union, 4) if union > 0 else 1.0
        rec[index] = {"area": round(float(np.clip(arr, 0.0, 1.0).mean()), 6),
                      "score": round(float(score), 4), "iou": iou,
                      "mask": cur}


def _frame_arrays(state: "_FakeSamState", dir_path: Path, frames) -> dict:
    """`areas`, `scores` and `ious` for index.json (contract C2).

    Indexed by ABSOLUTE frame index and padded with None to the declared
    frame count, which is what makes a partial matte obvious in the response
    and what `quality()` reads.
    """
    with state.lock:
        rec = {i: dict(v) for i, v in
               state.frame_stats.get(str(dir_path), {}).items()}
    n = max(int(frames or 0), (max(rec) + 1) if rec else 0)
    out = {"areas": [None] * n, "scores": [None] * n, "ious": [None] * n}
    for i, entry in rec.items():
        if 0 <= i < n:
            out["areas"][i] = entry["area"]
            out["scores"][i] = entry["score"]
            out["ious"][i] = entry["iou"]
    return out


def _slug(text) -> str:
    """A matte id embeds this; it has to survive being the last path segment
    of a URL, so anything that is not alnum, `-` or `_` is dropped rather
    than percent-escaped, which would just move the problem to whichever
    caller builds the URL by hand (this test file's own helpers do).
    """
    s = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(text))
    return s[:40] or "x"


def _recipe_tag(prompts, select=None, steady=None, clip_key=None,
                rotation=None, width=None) -> str:
    """A short digest of the whole recipe, the way the real service ids a
    matte (a digest, not one prompt word).

    The real derivation is `recipe_digest(clip_key, rotation, source.width,
    recipe, slot.id, steady, start, end)` in sam/server.py, so the clip, the
    ROTATION and the WORKING WIDTH are part of the id: the same words at two
    rotations are two different pictures and must not share a directory.
    Round 1 finding 40 is that nothing anywhere proved either of those two
    were part of any key; this fake left them out too, so a test that tracked
    one prompt at two rotations would have had the second track write into the
    first one's matte and the test would have proved the opposite of what it
    read.

    Deliberately NOT keyed on the frame range, which is the one place this
    departs from the real digest: a resume re-queues a tail with a different
    start_frame and has to land on the same id. The real service handles that
    with the `matte_ids` override below, which this fake honours too.
    """
    raw = json.dumps({"prompts": prompts or {}, "select": select,
                      "steady": steady, "clip_key": clip_key,
                      "rotation": rotation, "width": width},
                     sort_keys=True, default=str)
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


HOLD_AFTER = 2                 # frames a held track writes before it waits
HOLD_TIMEOUT = 30.0            # and how long it waits before giving up


def _run_slow_track(state: _FakeSamState, job_id: str, out_dir: Path,
                    mattes: list, total: int, common: dict,
                    start: int = 0, kind: str = "normal",
                    gate: threading.Event | None = None) -> None:
    """The timed writer: one frame every half second, cancellable.

    `gate` (a track whose prompts carry the word "hold") stops after
    `HOLD_AFTER` frames and waits, so a test can cancel a track at a KNOWN
    number of written frames instead of racing it. The wait is bounded and a
    gate that is never released ends the thread rather than leaving it writing
    into a matte a later test reads.
    """
    for m in mattes:
        _fake_index_write(Path(m["path"]), matte_id=m["matte_id"], state="running")
    for i in range(total):
        with state.lock:
            job = state.jobs.get(job_id)
            if job is None or job["state"] == "cancelled":
                return
        for m in mattes:
            d = Path(m["path"])
            _write_frame(state, d, start + i,
                         _fake_mask(20, 15, start + i, kind=kind))
            _fake_index_write(d, matte_id=m["matte_id"], done_frames=i + 1,
                              **common,
                              **_frame_arrays(state, d, common.get("frames")))
        with state.lock:
            job = state.jobs.get(job_id)
            if job is None:
                return
            job["done_frames"] = i + 1
        if gate is not None and (i + 1) >= HOLD_AFTER:
            if not gate.wait(timeout=HOLD_TIMEOUT):
                return                      # nobody released it: write no more
            gate = None                     # released: normal pace from here
            continue
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
            # Counted off the directory, not `total`: a resumed tail of 10
            # frames landing in a matte that now holds 12 has to report 12,
            # the way sam/store.py's `_carry_forward` recounts.
            on_disk = len([q for q in d.glob("*.png") if q.stem.isdigit()])
            _fake_index_write(d, matte_id=m["matte_id"], state="done",
                              done_frames=on_disk, **common,
                              **_frame_arrays(state, d, common.get("frames")))


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

    def _wait_written(self, matte_id: str, at_least: int,
                      timeout: float = 20.0):
        """Poll until the matte has at least `at_least` frames on disk.

        A separate waiter from `_wait_matte_state` because a re-track (a
        resume or a widen) starts from a matte that is ALREADY in the state
        it will end in: waiting for "done" on one of those returns
        immediately, before a single new frame is written, and the assertion
        that follows reads the old matte and passes for the wrong reason.
        """
        deadline = time.time() + timeout
        last = -1
        info = None
        while time.time() < deadline:
            info = _get(self.base + f"/matte/{matte_id}")
            last = info["written_count"]
            if last >= at_least:
                return info
            time.sleep(0.2)
        raise AssertionError(f"matte {matte_id} has {last} frames on disk, "
                             f"waited for {at_least}")

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
        """Round 1 finding 39: this promised "the nearest-written-frame
        fallback and its warning header" and then asserted that the state
        header was one of the three states it could possibly be in. The
        warning header was asserted nowhere in the arc.

        The track is held at a known frame count now (the fake waits on a
        gate for a prompt carrying "hold"), so the frame the fallback serves
        is a number this test can name rather than whatever the clock left on
        disk.
        """
        result = self._track(prompts={"text": ["slow", "hold"]},
                             start=0, end=0.25)
        matte_id = result["mattes"][0]["matte_id"]
        window_end = result["end_frame"]
        self.assertGreater(window_end, HOLD_AFTER)
        held = self._wait_written(matte_id, HOLD_AFTER)
        self.assertEqual(held["written_count"], HOLD_AFTER)
        png, headers = _post_none_get_raw(
            self.base + f"/matte/{matte_id}/frame?time=5&width=32")
        self.assertGreater(len(png), 0)
        self.assertIn(headers.get("X-Matte-State"), ("running", "queued"))
        self.assertEqual(headers.get("X-Matte-Frame"), str(HOLD_AFTER - 1),
                         "the last frame written so far answers for every "
                         "moment past it")
        self.assertIn("is not tracked yet",
                      headers.get("X-Matte-Warning") or "",
                      "a frame served by the fallback has to say so, or a "
                      "caller measures a frozen mask and never knows")

        cfg = _matte_layer_config(matte_id)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            _post(self.base + "/render", {
                "clip": self.clip, "config": cfg, "duration": 0.2,
                "name": f"masktest_refuse_{matte_id}"})
        self.assertEqual(caught.exception.code, 400)
        body = json.loads(caught.exception.read())
        self.assertIn(matte_id, body.get("error", ""))

        out = _post(self.base + "/render", {
            "clip": self.clip, "config": cfg, "duration": 0.2,
            "allow_partial": True, "name": f"masktest_allow_{matte_id}"})
        self.assertIn("job", out)

        # And once the held track is let go it finishes the window, so the
        # refusal above was about this matte being unfinished and not about
        # anything permanent.
        self.sam_state.release_gates()
        done = self._wait_written(matte_id, window_end, timeout=30.0)
        self.assertEqual(done["written_count"], window_end)
        self._wait_matte_state(matte_id, ("done",), timeout=20.0)
        finished = _post(self.base + "/render", {
            "clip": self.clip, "config": cfg, "duration": 0.2,
            "name": f"masktest_finished_{matte_id}"})
        self.assertIn("job", finished)

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
        # Round 1 finding 39: `coverage > 0` passes for any matte with a
        # single frame in it. Coverage is written frames over the LENGTH OF
        # THE SPAN, so on a matte with no holes it is exactly 1.0, and it says
        # nothing at all about whether the track finished (this one declares
        # 12 frames and answers for 12; the freeze test below has a matte
        # whose coverage is also 1.0 while it holds 2 of 12).
        span_len = span["end_frame"] - span["start_frame"]
        self.assertEqual(info["coverage"],
                         round(span["written"] / span_len, 4))
        self.assertEqual(info["coverage"], 1.0)
        self.assertTrue(span["contiguous"])

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
        # Round 1 finding 15: this line used to be the whole assertion about
        # the IoU rule, and "one of the three values that exist" is true of
        # every possible answer. The service writes `ious` into index.json as
        # it tracks (C2, and the fake mirrors it now), so the only correct
        # source here is "index"; reading "none" would mean the rule did not
        # run and a low_iou count of 0 meant nothing.
        self.assertEqual(q["iou_source"], "index")
        self.assertEqual(q["checked"], info["written_count"])
        # This subject does not drift (the fake writes a steady matte for an
        # ordinary prompt), so the honest answer is zero on every rule. That
        # is the assertion the arithmetic identity below cannot make: 0 == 0
        # + 0 + 0 - 0 was what it checked before the arrays were real.
        self.assertEqual(q["suspect_count"], 0, q["suspect_frames"])
        self.assertEqual(q["reasons"], {"zero_area": 0, "area_jump": 0,
                                        "area_recover": 0, "low_iou": 0})
        self.assertIsNone(q["first_suspect_index"])
        self.assertEqual(set(q["reasons"]),
                         {"zero_area", "area_jump", "area_recover", "low_iou"})
        self.assertEqual(q["suspect_count"],
                         q["reasons"]["zero_area"] + q["reasons"]["area_jump"]
                         + q["reasons"]["area_recover"]
                         + q["reasons"]["low_iou"]
                         - self._overlapping_reasons(q))
        # and the thresholds in force are readable without computing them
        status = _get(self.base + "/mask/status")
        self.assertIn("area_jump", status["quality_thresholds"])

    def test_a_drifting_matte_names_the_frames_that_went_wrong(self):
        """Round 1 finding 15, through the route: the drift rules FIRING.

        Everything else in this file asked a healthy matte whether it had
        anything to report, so every count was 0 and every assertion held
        whatever the rules did (the fake wrote no `areas` at all, which forces
        0 by itself). This tracks a matte that really goes wrong and names the
        frame and the reason for each failure.

        The fake writes 9 rows of 15 for a steady frame, an area of 0.6, and
        moves one row per frame so consecutive frames overlap at 0.8. Three
        frames are deliberately wrong, and the whole expected table is
        arithmetic on row counts:

          frame 2  nothing written    area 0 (zero_area), the whole of the
                                      previous area lost (area_jump 1.0) and
                                      no overlap at all (low_iou 0.0)
          frame 3  steady again       the subject is back after an empty
                                      frame, which is area_recover (round 1
                                      finding 22: the jump rule cannot divide
                                      by an area of 0, so this frame used to
                                      be flagged for its shape alone), and it
                                      overlaps that empty frame, so low_iou
          frame 4  the other rows     7 rows of 15, so no jump worth flagging
                                      (0.22), one row of overlap out of 15
                                      (low_iou 0.0667)
          frame 5  steady again       the same one row of overlap, low_iou
          frame 6  the whole frame    0.6 to 1.0 is a jump of 0.67, and it
                                      still overlaps what came before, so
                                      area_jump alone

        Losing a subject trips three rules at once and coming back from it
        trips two: that is the real behaviour of the shipped rules, not a
        rounding of it, and writing it down means a change to any one rule
        fails here instead of quietly changing what the studio reports.
        """
        result = self._track(prompts={"text": ["__drift__ face"]},
                             start=0, end=0.5)
        matte_id = result["mattes"][0]["matte_id"]
        info = self._wait_matte_state(matte_id, ("done",))
        q = info["quality"]
        self.assertEqual(q["iou_source"], "index",
                         "the fake writes ious the way sam/store.py does, so "
                         "the rule has to run off the index")
        self.assertEqual(q["thresholds"], {"area_jump": 0.5, "min_iou": 0.3,
                                           "area_recover": 0.0})
        by_index = {f["index"]: f["reasons"] for f in q["suspect_frames"]}
        self.assertEqual(by_index, {
            DRIFT_LOST: ["zero_area", "area_jump", "low_iou"],
            DRIFT_LOST + 1: ["area_recover", "low_iou"],
            DRIFT_ELSEWHERE: ["low_iou"],
            DRIFT_ELSEWHERE + 1: ["low_iou"],
            DRIFT_LATCH: ["area_jump"],
        })
        self.assertEqual(q["suspect_count"], 5)
        self.assertEqual(q["reasons"], {"zero_area": 1, "area_jump": 2,
                                        "area_recover": 1, "low_iou": 4})
        # The recovery frame is the one the area rule is blind to: it carries
        # no `jump` at all, because there is no previous area to divide by.
        came_back = next(f for f in q["suspect_frames"]
                         if f["index"] == DRIFT_LOST + 1)
        self.assertIsNone(came_back["jump"])
        self.assertEqual(came_back["prev_area"], 0.0)
        self.assertGreater(came_back["area"], 0.0)
        self.assertEqual(q["first_suspect_index"], DRIFT_LOST)
        self.assertAlmostEqual(q["first_suspect_time"],
                              DRIFT_LOST / info["fps"], places=3)
        lost = next(f for f in q["suspect_frames"] if f["index"] == DRIFT_LOST)
        self.assertEqual(lost["area"], 0.0)
        self.assertAlmostEqual(lost["prev_area"], 0.6, places=3)
        self.assertAlmostEqual(lost["jump"], 1.0, places=3)
        self.assertEqual(lost["iou"], 0.0)
        latch = next(f for f in q["suspect_frames"] if f["index"] == DRIFT_LATCH)
        self.assertEqual(latch["area"], 1.0)
        self.assertAlmostEqual(latch["jump"], 2.0 / 3.0, places=2)

        # The arrays the rules read are on the wire too, so a caller plotting
        # the curve (`mask show --strip`) sees the same numbers the flags came
        # from, and an absent `ious` would be visible rather than silent.
        one = _get(self.base + f"/matte/{matte_id}")
        self.assertEqual(one["areas"][DRIFT_LOST], 0.0)
        self.assertEqual(one["areas"][DRIFT_LATCH], 1.0)
        self.assertEqual(one["ious"][DRIFT_LOST], 0.0)
        self.assertIsNone(one["ious"][0],
                          "the first frame has nothing before it to overlap")
        # And the list route carries the summary of the same judgement, capped
        # but with the true count.
        listing = _get(self.base + f"/matte?clip={self.clip}")
        row = next(m for m in listing["mattes"] if m["matte_id"] == matte_id)
        self.assertEqual(row["quality"]["suspect_count"], 5)
        self.assertEqual(row["quality"]["first_suspect_index"], DRIFT_LOST)

    def test_a_matte_with_no_ious_has_them_read_off_its_own_frames(self):
        """Round 1 finding 15's deferred line: `compute_iou=full`.

        `frame_ious()` (grade/mattes.py) is the only way to judge the SHAPE
        half of drift on a matte tracked before the SAM service started
        writing `ious` into index.json, and nothing in the product called it,
        so on such a matte `low_iou` was 0 for want of a rule rather than for
        want of drift and only `iou_source` said so.

        The single matte route reads the frames off disk for it now, and the
        LIST route deliberately still does not: `full` already means "this is
        one matte, send its per frame arrays", and a clip with four mattes of
        384 frames cannot pay for four disk walks to answer "what state are
        these in".

        The fixture is a real tracked matte with its `ious` taken back out of
        index.json, which is exactly what an older matte looks like on disk.
        """
        result = self._track(prompts={"text": ["iou fallback subject"]},
                             start=0, end=0.25)
        matte_id = result["mattes"][0]["matte_id"]
        self._wait_matte_state(matte_id, ("done",))
        self.assertEqual(
            _get(self.base + f"/matte/{matte_id}")["quality"]["iou_source"],
            "index", "the control: with ious in the index they are used")

        matches = sorted(self._matte_root().rglob(f"{matte_id}/{MT.INDEX_NAME}"))
        self.assertEqual(len(matches), 1, f"expected one {matte_id} on disk")
        index_path = matches[0]
        raw = json.loads(index_path.read_text())
        self.assertTrue(raw.get("ious"),
                        "this test removes the ious, so they have to be there "
                        "to start with")
        raw.pop("ious")
        index_path.write_text(json.dumps(raw))

        one = _get(self.base + f"/matte/{matte_id}")
        self.assertIsNone(one.get("ious"),
                          "the arrays on the wire are the index's own; only "
                          "the judgement falls back")
        self.assertEqual(one["quality"]["iou_source"], "frames",
                         "with no ious in the index the single matte route "
                         "reads the shapes off disk; 'none' is the pre-fix "
                         "answer and means the shape rule did not run at all")
        self.assertGreater(one["quality"]["checked"], 0)

        listing = _get(self.base + f"/matte?clip={self.clip}")
        row = next(m for m in listing["mattes"] if m["matte_id"] == matte_id)
        self.assertEqual(row["quality"]["iou_source"], "none",
                         "the list route must not read every frame of every "
                         "matte to answer what state they are in")

        # Round 2 finding 55: the assertion above only ever exercised the
        # DEFAULT list route, and the read was wired to `full`, which the
        # route takes straight from `?full=1`. So the one branch a caller
        # actually reaches for ("send the arrays too") was the branch that
        # decoded every PNG of every matte on the clip, inside the request
        # handler. `?full=1` means the per frame arrays index.json already
        # holds and nothing more.
        full_listing = _get(self.base + f"/matte?clip={self.clip}&full=1")
        self.assertTrue(full_listing["full"],
                        "the control: this really is the full=1 branch")
        full_row = next(m for m in full_listing["mattes"]
                        if m["matte_id"] == matte_id)
        self.assertEqual(full_row["quality"]["iou_source"], "none",
                         "?full=1 asks for the arrays the index already "
                         "holds, never for a decode of every frame of every "
                         "matte on the clip")
        self.assertIn("areas", full_row,
                      "and it still sends those arrays, which is the whole "
                      "point of the flag")

    def test_a_partial_matte_freezes_past_its_span_and_says_which_frame(self):
        """Round 1 finding 39: `assertTrue(info["frozen_outside_span"])` is a
        literal `True` in the response, so it passed whatever the studio
        actually did with a moment past the tracked window.

        This asks for one. The matte is stopped at two frames of a declared
        twelve, and the frame route is asked for frame 10:

          the bytes come back 200, not a 404 (the freeze is the design: a
          correction keeps working while a track is still running),
          X-Matte-Frame says 1, the last frame really tracked, not 10, and
          X-Matte-Warning names the frame asked for, the frame served and how
          much of the matte exists.

        Inside the span there is no warning at all, which is what makes the
        warning readable as "you are outside the tracked window".
        """
        prompts = {"text": ["slow", "hold", "freeze past the span"]}
        queued = self._track(prompts=prompts, start=0, end=0.5)
        matte_id = queued["mattes"][0]["matte_id"]
        held = self._wait_written(matte_id, HOLD_AFTER)
        self.assertEqual(held["written_count"], HOLD_AFTER,
                         "the gate holds the fake at a known frame count, so "
                         "this test never races the writer")
        _post(self.base + f"/mask/jobs/{queued['job_id']}/cancel", {})
        info = self._wait_matte_state(matte_id, ("partial",), timeout=25.0)
        span = info["span"]
        self.assertEqual(span["end_frame"], HOLD_AFTER)
        self.assertGreater(info["total_frames"], span["end_frame"],
                          "this test needs a matte that declares more frames "
                          "than it answers for")
        self.assertTrue(info["frozen_outside_span"])
        # Coverage is 1.0 on a matte holding 2 of 12 frames, because coverage
        # is about HOLES inside the span, not about being finished. Asserted
        # here so the two numbers are never read as the same claim.
        self.assertEqual(info["coverage"], 1.0)
        self.assertTrue(info["is_partial"])

        fps = float(info["fps"])
        outside = 10
        self.assertGreater(outside, span["end_frame"] - 1)
        png, headers = _post_none_get_raw(
            self.base + f"/matte/{matte_id}/frame?time={outside / fps}&width=32")
        self.assertGreater(len(png), 0)
        self.assertEqual(headers.get("X-Matte-Frame"),
                         str(span["end_frame"] - 1),
                         "past the span the LAST TRACKED frame is served, "
                         "which is what frozen_outside_span means")
        warning = headers.get("X-Matte-Warning") or ""
        self.assertIn(matte_id, warning)
        self.assertIn(f"frame {outside} is not tracked yet", warning)
        self.assertIn(f"showing frame {span['end_frame'] - 1}", warning)
        self.assertIn(f"{info['written_count']} of {info['total_frames']}",
                      warning)

        inside, inside_headers = _post_none_get_raw(
            self.base + f"/matte/{matte_id}/frame?time=0&width=32")
        self.assertEqual(inside_headers.get("X-Matte-Frame"), "0")
        self.assertIsNone(inside_headers.get("X-Matte-Warning"),
                          "a frame inside the span is not a fallback, so a "
                          "warning there would train callers to ignore it")
        self.assertNotEqual(inside, png,
                            "the frozen frame and frame 0 are different "
                            "pictures, so the fallback really moved")
        self.sam_state.release_gates()

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

    def test_asking_for_a_wider_window_widens_the_matte(self):
        """Round 1 finding 5. `mask track --end 2` then `--end 6` answered
        `cached: true` with the words "already covers this request" and the
        matte still stopped at two seconds.

        The cause was one clamp: the requested window was clipped to the
        MATTE's own declared length before being compared with it, so every
        wider ask compared equal. The fix clamps to the CLIP's frame count
        instead (which is what the clamp was really for: `--end 20` on a 16
        second clip must not re-queue forever) and re-queues the frames
        outside the matte, keeping the ones inside it.

        Six frames then eighteen, both written by the fake service inside the
        POST, so this test is arithmetic and not a race.
        """
        prompts = {"text": ["widen me"]}
        first = self._track(prompts=prompts, start=0, end=0.25)
        matte_id = first["mattes"][0]["matte_id"]
        narrow_end = first["end_frame"]
        self.assertEqual(first["start_frame"], 0)
        self.assertGreaterEqual(narrow_end, 2,
                                "this test needs a window of at least two "
                                "frames to have an outside")
        self._wait_matte_state(matte_id, ("done",))
        self._wait_written(matte_id, narrow_end)
        calls = self.sam_state.track_calls

        # The control: the SAME window again really is a cache hit, and the
        # answer now says which frames it decided were covered, so a reader
        # (and the CLI, which prints them) can check the claim.
        again = self._track(prompts=prompts, start=0, end=0.25)
        self.assertTrue(again["cached"])
        self.assertEqual(again["start_frame"], 0)
        self.assertEqual(again["end_frame"], narrow_end)
        self.assertEqual(self.sam_state.track_calls, calls,
                         "an identical window must not call the service again")

        wider = self._track(prompts=prompts, start=0, end=0.75)
        self.assertFalse(
            wider["cached"],
            "a window wider than the matte is not a cache hit: this is the "
            "finding, and on the old code this assertion is what fails")
        self.assertTrue(wider["widened"])
        self.assertFalse(wider["resumed"])
        self.assertFalse(wider["restarted"])
        self.assertEqual(wider["resumed_from"], narrow_end,
                         "a widen starts at the first frame the matte does "
                         "not have, so the frames it does have are kept")
        self.assertEqual(wider["start_frame"], narrow_end)
        self.assertGreater(wider["end_frame"], narrow_end)
        self.assertIn("widening", wider["message"])
        self.assertIn(str(wider["end_frame"]), wider["message"],
                      "the message has to name the window asked for, or the "
                      "reader cannot tell a widen from a redo")
        self.assertEqual(wider["mattes"][0]["matte_id"], matte_id,
                         "a widen writes back into the same matte")
        self.assertEqual(self.sam_state.track_calls, calls + 1,
                         "a widen has to reach the SAM service; that call is "
                         "the whole point of the finding")
        self.assertEqual(self.sam_state.last_matte_ids.get("0"), matte_id,
                         "the widen must name the existing matte so the "
                         "frames already tracked are not orphaned")
        after = self._wait_written(matte_id, wider["end_frame"])
        self.assertEqual(after["written_count"], wider["end_frame"],
                         "every frame from 0 to the new end is on disk: the "
                         "kept ones and the newly tracked ones")
        self.assertEqual(after["span"]["end_frame"], wider["end_frame"])

        # And once it is wide, the wide window is itself a cache hit.
        third = self._track(prompts=prompts, start=0, end=0.75)
        self.assertTrue(third["cached"])
        self.assertEqual(third["end_frame"], wider["end_frame"])
        self.assertEqual(self.sam_state.track_calls, calls + 1)

    def test_a_narrower_window_inside_a_wide_matte_is_still_a_cache_hit(self):
        """The other side of finding 5: widening must not turn every repeat
        call into a re-track. A window INSIDE what the matte already holds is
        answered from the cache, with the frames it checked."""
        prompts = {"text": ["already wide"]}
        first = self._track(prompts=prompts, start=0, end=0.75)
        matte_id = first["mattes"][0]["matte_id"]
        wide_end = first["end_frame"]
        self._wait_matte_state(matte_id, ("done",))
        self._wait_written(matte_id, wide_end)
        calls = self.sam_state.track_calls

        inner = self._track(prompts=prompts, start=0, end=0.25)
        self.assertTrue(inner["cached"])
        self.assertLess(inner["end_frame"], wide_end)
        self.assertEqual(self.sam_state.track_calls, calls)

    def test_a_stale_matte_is_re_tracked_even_when_it_covers_the_window(self):
        """Round 1 finding 21: the cache asked "is every frame here" BEFORE
        "is this matte still valid", so a stale matte (one the store marked
        as made for a different clip, rotation or working width) with a full
        set of frames was served as a cache hit and graded the wrong pixels.

        `stale` is now read first, and it restarts with its own message
        rather than the generic dead one, so the reason reaches the caller.
        """
        prompts = {"text": ["stale subject"]}
        first = self._track(prompts=prompts, start=0, end=0.25)
        matte_id = first["mattes"][0]["matte_id"]
        end = first["end_frame"]
        self._wait_matte_state(matte_id, ("done",))
        info = self._wait_written(matte_id, end)

        # Mark it stale in place, the way the store does when the identity it
        # recomputes no longer matches what the index says (C2). Every frame
        # stays on disk: that is exactly the case the old order got wrong.
        matches = sorted(self._matte_root().rglob(f"{matte_id}/{MT.INDEX_NAME}"))
        self.assertEqual(len(matches), 1, f"expected one {matte_id} on disk")
        index_path = matches[0]
        raw = json.loads(index_path.read_text())
        raw["state"] = "stale"
        index_path.write_text(json.dumps(raw))
        self.assertEqual(_get(self.base + f"/matte/{matte_id}")["state"],
                         "stale")
        self.assertEqual(
            _get(self.base + f"/matte/{matte_id}")["written_count"], end,
            "the point of this test is a stale matte that IS fully written")
        calls = self.sam_state.track_calls

        again = self._track(prompts=prompts, start=0, end=0.25)
        self.assertFalse(
            again["cached"],
            "a stale matte is wrong however many frames it holds; on the old "
            "order coverage was read first and this came back cached")
        self.assertTrue(again["restarted"])
        self.assertFalse(again["resumed"])
        self.assertFalse(again["widened"])
        self.assertEqual(again["resumed_from"], 0,
                         "a stale matte is re-tracked from the start of the "
                         "window, not resumed from a hole it does not have")
        self.assertIn("stale", again["message"],
                      "the caller has to be told the recipe identity changed, "
                      "not just handed a generic restart")
        self.assertEqual(again["mattes"][0]["matte_id"], matte_id)
        self.assertEqual(self.sam_state.track_calls, calls + 1)

    def test_a_cancelled_matte_resumes_from_its_first_missing_frame(self):
        """Checkpoint gap 12, the resume half: frames already written stay
        written and only the missing tail is re-queued.

        Round 1 finding 34: this test used to accept a RESTART. It cancelled a
        timed track after "at least one frame", which is a race against a
        writer running on a clock, so it could not know how many frames were
        on disk, could not name the frame the resume had to start from, and
        skipped itself outright whenever the fake finished first. It asserted
        `resumed or restarted`, and a restart is the opposite outcome: every
        frame tracked so far thrown away. The resume path it exists to prove
        was therefore never asserted by it at all.

        The fake now holds a track whose prompts carry the word "hold" after
        exactly `HOLD_AFTER` frames and waits to be released, so the cancel
        lands at a KNOWN frame count and every number below is arithmetic:
        two frames written, the tail re-queued from frame 2, and the frames
        already on disk kept.

        The prompt list carries the exact word "slow" (the fake's timed
        writer) plus a phrase of its own, so this test's recipe hashes
        differently from every other slow track here: the recipe cache is
        keyed on the prompts and deliberately not on the frame range, so two
        tests sharing a prompt would share a matte.
        """
        prompts = {"text": ["slow", "hold", "resume tail"]}
        first = self._track(prompts=prompts, start=0, end=0.5)
        job_id = first["job_id"]
        matte_id = first["mattes"][0]["matte_id"]
        declared_end = first["end_frame"]
        self.assertGreater(declared_end, HOLD_AFTER + 1,
                          "the window has to be longer than the hold, or "
                          "there is no tail to resume")
        held = self._wait_written(matte_id, HOLD_AFTER)
        self.assertEqual(held["written_count"], HOLD_AFTER,
                         "the gate holds the writer here, so the cancel below "
                         "is not a race")
        _post(self.base + f"/mask/jobs/{job_id}/cancel", {})
        before = self._wait_matte_state(matte_id, ("partial",), timeout=25.0)
        self.assertEqual(before["state"], "partial",
                         "a cancel with frames on disk is partial, never done: "
                         "the old version of this test skipped itself here")
        self.assertEqual(before["span"]["end_frame"], HOLD_AFTER)
        calls = self.sam_state.track_calls

        second = self._track(prompts=prompts, start=0, end=0.5)
        self.assertFalse(second["cached"],
                         "a matte with a hole in the window asked for is not "
                         "a cache hit")
        self.assertTrue(second["resumed"],
                        "the tail is re-queued and the head is kept: that is "
                        "a resume, and this is the assertion finding 34 was "
                        "about")
        self.assertFalse(second["restarted"],
                         "a restart would throw away the frames already "
                         "tracked, which is the outcome this test used to "
                         "accept as equivalent")
        self.assertFalse(second["widened"])
        self.assertEqual(second["resumed_from"], HOLD_AFTER,
                         "a resume starts at the FIRST MISSING frame")
        self.assertEqual(second["start_frame"], HOLD_AFTER)
        self.assertEqual(second["end_frame"], declared_end,
                         "and still ends where the request asked")
        self.assertIn("resuming from frame", second["message"])
        self.assertEqual(second["mattes"][0]["matte_id"], matte_id,
                         "a resume writes back into the same matte")
        self.assertEqual(self.sam_state.track_calls, calls + 1)
        self.assertEqual(self.sam_state.last_matte_ids.get("0"), matte_id,
                         "the resume must name the existing matte, or the "
                         "frames already written are orphaned in an old "
                         "directory under a freshly derived id")

        # Let the resumed tail run to the end and check what landed: every
        # frame of the window, and the head's own per frame numbers still in
        # place (contract C2's carry forward, which is what stops a resume
        # from blanking the area curve before the tail).
        self.sam_state.release_gates()
        after = self._wait_written(matte_id, declared_end, timeout=40.0)
        self.assertEqual(after["written_count"], declared_end)
        self.assertEqual(after["span"], dict(after["span"], start_frame=0,
                                            end_frame=declared_end,
                                            contiguous=True))
        full = _get(self.base + f"/matte/{matte_id}")
        self.assertIsNotNone(full["areas"][0],
                             "the frames tracked before the cancel keep their "
                             "areas: a resume that blanks them loses the "
                             "curve for the head of the clip")
        self.assertIsNotNone(full["areas"][declared_end - 1])
        self.assertEqual(full["quality"]["iou_source"], "index")

    def test_the_track_cache_key_includes_rotation_and_the_working_width(self):
        """Round 1 finding 40. Design rule 5 keys a track by clip identity,
        rotation, working width and recipe hash, and `_recipe_hash` does
        include all four. Nothing proved the middle two.

        Every track in this arc ran at one rotation and one working width
        (`--mask-width 160`, in both suites), so deleting `"rotation"` or
        `"width": MASK_WORKING_WIDTH` from that payload left every test green
        while the studio served a matte tracked at one rotation, or measured
        at one width, for a request that asked for another. Both are wrong
        pixels reported as a cache hit.

        Three tracks of ONE prompt: the same request twice (free, the
        control), the same request rotated (a different picture, so a
        different matte and a real service call), and the same request again
        on a second server whose working width is 320 and whose data
        directory is the same one (a different measurement, so a different
        matte again). The second server is the only way to vary the width: it
        is a server flag, not a request field.
        """
        prompts = {"text": ["rotation and width subject"]}
        calls = self.sam_state.track_calls
        at0 = self._track(prompts=prompts, rotation=0, start=0, end=0.25)
        id0 = at0["mattes"][0]["matte_id"]
        self.assertFalse(at0["cached"])
        self.assertEqual(self.sam_state.track_calls, calls + 1)
        self.assertEqual(self.sam_state.last_track_width, MASK_WIDTH,
                         "the server's working width has to be on the wire "
                         "before it can be part of any key")
        self._wait_matte_state(id0, ("done",))

        # The control: the identical request is free, so the differences
        # below are the rotation and the width and nothing else.
        again = self._track(prompts=prompts, rotation=0, start=0, end=0.25)
        self.assertTrue(again["cached"])
        self.assertEqual(again["mattes"][0]["matte_id"], id0)
        self.assertEqual(self.sam_state.track_calls, calls + 1)

        at90 = self._track(prompts=prompts, rotation=90, start=0, end=0.25)
        self.assertFalse(at90["cached"],
                         "the same words on a rotated clip are a different "
                         "picture: answering it from the cache hands back a "
                         "matte made for the other orientation")
        id90 = at90["mattes"][0]["matte_id"]
        self.assertNotEqual(id90, id0)
        self.assertEqual(self.sam_state.track_calls, calls + 2,
                         "and it has to reach the service")
        self.assertEqual(str(self.sam_state.last_track_rotation), "90")
        self._wait_matte_state(id90, ("done",))
        # Both mattes exist side by side, each remembering its own rotation.
        self.assertEqual(_get(self.base + f"/matte/{id0}")["rotation"], "0")
        self.assertEqual(_get(self.base + f"/matte/{id90}")["rotation"], "90")

        # A second studio on the SAME data directory (so the same matte store
        # and the same recipe cache file), working at 320 instead of 160.
        wide_width = 320
        port = _free_port()
        wide_base = f"http://127.0.0.1:{port}/api"
        sam_port = self.sam_srv.server_address[1]
        proc = subprocess.Popen(
            [PYTHON, SERVER, "--port", str(port), "--data-dir", self.data_dir,
            "--sam-url", f"http://127.0.0.1:{sam_port}",
            "--mask-width", str(wide_width)],
            cwd=str(CONTENT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            _wait_ready(wide_base, proc)
            self.assertEqual(_get(wide_base + "/mask/status")["mask_width"],
                             wide_width)
            before = self.sam_state.track_calls
            wide = _post(wide_base + "/mask/track",
                        {"clip": self.clip, "prompts": prompts,
                         "rotation": 0, "start": 0, "end": 0.25})
            self.assertFalse(wide["cached"],
                             "a matte tracked at a 160 wide working size is "
                             "not an answer to a request measured at 320")
            self.assertNotEqual(wide["mattes"][0]["matte_id"], id0)
            self.assertEqual(self.sam_state.track_calls, before + 1)
            self.assertEqual(self.sam_state.last_track_width, wide_width)
            # And the same request on the wide server twice is still free, so
            # what changed is the width and not simply "a second server".
            twice = _post(wide_base + "/mask/track",
                         {"clip": self.clip, "prompts": prompts,
                          "rotation": 0, "start": 0, "end": 0.25})
            self.assertTrue(twice["cached"])
            self.assertEqual(twice["mattes"][0]["matte_id"],
                             wide["mattes"][0]["matte_id"])
        finally:
            _stop(proc)

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
        """Round 1 finding 17: a test named "differs" that asserted no
        difference.

        What it checked was that the key "luma" was present in both answers,
        which is true of every stats response the route can produce, weighted
        or not. A route that ignored `matte` entirely passed it.

        There are two comparisons here instead, and the first is the one that
        cannot pass by accident: a matte that covers EVERY pixel has to
        reproduce the unweighted measurement exactly, block for block. That is
        the identity case of a weighted mean, so it pins the weighting as
        correct and not merely present. Then a matte covering the top half of
        the frame has to move the numbers, and by more than float noise.
        """
        width = self.STACK_WIDTH
        body = {"clip": self.clip, "time": 0.1, "width": width, "config": {}}
        plain = _post(self.base + "/stats", dict(body))

        full = self._track(prompts={"text": ["__full__ every pixel"]},
                           start=0, end=0.25)
        full_id = full["mattes"][0]["matte_id"]
        self._wait_matte_state(full_id, ("done",))
        by_full = _post(self.base + "/stats", dict(body, matte=full_id))
        self.assertEqual(by_full["matte"], full_id)
        self.assertEqual(by_full["coverage"], 1.0,
                         "a matte over the whole frame covers all of it")
        for block in ("luma", "saturation", "channels", "families", "clipped",
                      "bands"):
            self.assertEqual(by_full["stats"][block], plain["stats"][block],
                             f"weighting by a matte that covers everything has "
                             f"to reproduce the unweighted {block} exactly")

        half = self._track(prompts={"text": ["__half__ top of frame"]},
                           start=0, end=0.25)
        half_id = half["mattes"][0]["matte_id"]
        self._wait_matte_state(half_id, ("done",))
        by_half = _post(self.base + "/stats", dict(body, matte=half_id))
        # The fake writes 15 rows and covers the top 7 of them, so the
        # coverage this route reports is a number that can be checked by hand.
        self.assertAlmostEqual(by_half["coverage"], 7 / 15, delta=0.01)
        moved = abs(by_half["stats"]["luma"]["mean"]
                    - plain["stats"]["luma"]["mean"])
        # Measured at 0.038 on the clip in footage/ before this floor was
        # written down. The floor is deliberately well under that and well
        # over rounding (the means are reported to 4 decimals), so it fails on
        # a route that stops weighting and does not depend on the clip.
        self.assertGreater(moved, 0.005,
                           f"the top half of the frame reads the same as the "
                           f"whole frame to within {moved:.4f}: nothing is "
                           f"being weighted")
        self.assertNotEqual(by_half["stats"]["luma"], plain["stats"]["luma"])

    def test_stats_with_an_unknown_matte_is_a_clean_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            _post(self.base + "/stats", {"clip": self.clip, "time": 0.1,
                                         "width": 64, "config": {},
                                         "matte": "not-a-real-matte"})
        self.assertEqual(caught.exception.code, 404)

    # -- stats by a whole mask stack (checkpoint gap 19) -------------------

    # Everything below asks for the same recipe on purpose: track is free the
    # second time (design rule 5), so these share one matte instead of
    # queueing a fresh track per test.
    STACK_PROMPT = "mask stack subject"
    STACK_WIDTH = 240                  # over the 160 floor, small enough to be cheap

    def _stack_matte(self) -> str:
        result = self._track(prompts={"text": [self.STACK_PROMPT]})
        matte_id = result["mattes"][0]["matte_id"]
        self._wait_matte_state(matte_id, ("done",))
        return matte_id

    def _stats(self, **extra):
        body = {"clip": self.clip, "time": 0.1, "width": self.STACK_WIDTH,
                "config": {}}
        body.update(extra)
        return _post(self.base + "/stats", body)

    def _stats_error(self, **extra):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._stats(**extra)
        return caught.exception.code, json.loads(caught.exception.read()).get("error", "")

    def test_stats_by_a_one_component_mask_stack_matches_the_matte_shortcut(self):
        """Checkpoint gap 19. `matte: ID` could only ever say one stored
        matte, so "the person matte intersected with a skin key" (the round 4
        skin anchor's own measurement) had to be hand built against the
        engine's internal modules and then separately proved against a real
        render. `mask` takes the same component stack a graded layer carries,
        folded by the engine's OWN mask_matte, which is why the one component
        stack that names a single matte has to land on exactly the numbers the
        shortcut lands on: it is not a second implementation of the fold.
        """
        matte_id = self._stack_matte()
        shortcut = self._stats(matte=matte_id)
        stack = self._stats(mask={"components": [
            {"type": "matte", "op": "add", "matte": {"id": matte_id}}]})
        self.assertEqual(stack["stats"], shortcut["stats"])
        self.assertEqual(stack["mask_mattes"], [matte_id])
        self.assertEqual(stack["measured_width"], stack["size"][0])
        self.assertIsNotNone(stack.get("coverage"))
        self.assertFalse(stack["no_coverage"])
        # And both weighted rows say how much they covered, which an
        # unweighted row does not claim at all.
        self.assertEqual(stack["coverage"], shortcut["coverage"])
        self.assertNotIn("coverage", self._stats())

    def test_an_intersect_in_the_stack_narrows_what_was_measured(self):
        """The op in the stack has to change the answer in the direction it
        says, otherwise `mask` is decoration. The fake matte covers alternate
        rows of the WHOLE width, so intersecting it with a window over the
        middle half can only take pixels away.
        """
        matte_id = self._stack_matte()
        whole = self._stats(mask={"components": [
            {"type": "matte", "op": "add", "matte": {"id": matte_id}}]})
        narrowed = self._stats(mask={"components": [
            {"type": "matte", "op": "add", "matte": {"id": matte_id}},
            {"type": "window", "op": "intersect",
             "window": {"shape": "rect", "w": 0.5, "h": 1.0, "softness": 0.0}}]})
        self.assertLess(narrowed["coverage"], whole["coverage"])
        self.assertNotEqual(narrowed["stats"]["luma"]["mean"],
                            whole["stats"]["luma"]["mean"],
                            "a narrower mask that reads the same mean is not "
                            "weighting anything")

    def test_a_mask_stack_that_covers_nothing_is_a_row_not_an_error(self):
        """Checkpoint gap 23, through the route. Subtracting a window that
        covers the frame leaves a weight of exactly zero, which used to be a
        StatsError (a 400) and stopped a script walking timestamps dead. It is
        an ordinary 200 now, flagged, with the measurement blocks null rather
        than zero so nobody averages a frame that was never measured.
        """
        matte_id = self._stack_matte()
        out = self._stats(mask={"components": [
            {"type": "matte", "op": "add", "matte": {"id": matte_id}},
            {"type": "window", "op": "subtract",
             "window": {"shape": "rect", "w": 1.5, "h": 1.5,
                        "softness": 0.0}}]})
        self.assertTrue(out["no_coverage"])
        self.assertEqual(out["coverage"], 0.0)
        self.assertTrue(out["stats"]["no_coverage"])
        for block in ("luma", "saturation", "channels", "families", "clipped",
                      "bands"):
            self.assertIsNone(out["stats"][block],
                              f"{block} must be null on a frame the mask "
                              f"covers nothing of, not zero")
        # measured_width still says what was looked at, so the empty row is
        # comparable with the rows around it.
        self.assertEqual(out["measured_width"], out["size"][0])

    def test_stats_times_with_a_mask_answers_one_row_per_time(self):
        matte_id = self._stack_matte()
        out = self._stats(times=[0.1, 0.2],
                          mask={"components": [
                              {"type": "matte", "op": "add",
                               "matte": {"id": matte_id}}]})
        self.assertEqual(len(out["results"]), 2)
        for row in out["results"]:
            self.assertEqual(row["mask_mattes"], [matte_id])
            self.assertIsNotNone(row.get("coverage"))
            self.assertIn("no_coverage", row)
            self.assertEqual(row["measured_width"], row["size"][0])

    def test_an_unknown_matte_inside_a_mask_stack_is_a_clean_404(self):
        """mask_matte on its own treats a matte it cannot resolve as a black
        matte, so a typo would come back as an honest looking "no coverage"
        row. Every id in the stack is resolved before the first render for
        exactly that reason.
        """
        code, message = self._stats_error(mask={"components": [
            {"type": "matte", "op": "add",
             "matte": {"id": "not-a-real-matte"}}]})
        self.assertEqual(code, 404)
        self.assertIn("not-a-real-matte", message)

    def test_stats_refuses_a_mask_it_cannot_honestly_measure(self):
        """Five ways to ask for a measurement that would come back wrong
        rather than refused. The empty dict is the sharp one: a truthiness
        check drops it and measures the WHOLE frame while the caller believes
        a mask was applied, so `mask` is tested for presence, never for truth.
        """
        matte_id = self._stack_matte()
        one = {"components": [{"type": "matte", "op": "add",
                               "matte": {"id": matte_id}}]}
        code, message = self._stats_error(mask=one, matte=matte_id)
        self.assertEqual(code, 400)
        self.assertIn("send one", message)
        code, message = self._stats_error(mask=one, region=[0, 0, 0.5, 1.0])
        self.assertEqual(code, 400)
        self.assertIn("do not compose", message)
        code, message = self._stats_error(mask="person intersect skin")
        self.assertEqual(code, 400)
        self.assertIn("has to be an object", message)
        code, message = self._stats_error(mask={})
        self.assertEqual(code, 400)
        self.assertIn("nothing to measure through", message)
        code, message = self._stats_error(mask={"components": [
            {"type": "luma", "op": "intersect"}]})
        self.assertEqual(code, 400)
        self.assertIn("no component reaches the matte", message)

    def test_stats_refuses_more_mask_components_than_it_will_fold(self):
        """Round 3 finding 82: the round 2 blur cap bounds the cost of ONE
        component and nothing counted them.

        One component at the blur cap costs about 59 ms of numpy on the
        request thread, a component with distinct numbers is roughly 180
        bytes of JSON, and BODY_MAX_BYTES is 8 MB, so one legal POST could
        ask this handler to fold about 46,000 of them: roughly 46 minutes on
        one thread of a ThreadingHTTPServer that is also serving a live
        grading session. Every value in that request is inside the round 2
        limits.

        Both doors are asked here, because they refuse different requests:
        one stack that is too long, and a config whose layers are each legal
        and whose total is not. The sentence is the engine's own
        (CG.check_mask_components), so `cinegrade stats --mask` refuses the
        same request in the same words.
        """
        def window(i):
            return {"type": "window", "op": "add" if i == 0 else "intersect",
                    "feather": 0.001 * (i + 1),
                    "window": {"enabled": True, "shape": "rect",
                               "cx": 0.5, "cy": 0.5, "w": 0.6, "h": 0.6,
                               "softness": 0.1}}

        over = [window(i) for i in range(33)]
        code, message = self._stats_error(mask={"components": over})
        self.assertEqual(code, 400)
        self.assertIn("at most 32", message)
        self.assertIn("33 components", message)

        # Every stack legal, the total not: this is the request a per stack
        # cap on its own lets straight through.
        layers = [{"enabled": True, "mask": {"components": [window(i) for i in range(30)]}}
                  for _ in range(5)]
        code, message = self._stats_error(config={"layers": layers})
        self.assertEqual(code, 400)
        self.assertIn("at most 128", message)

        # And a render of the same config is refused as well, so the cap is
        # not something a caller walks around by asking for a file instead of
        # a measurement.
        with self.assertRaises(urllib.error.HTTPError) as caught:
            _post(self.base + "/render",
                  {"clip": self.clip, "config": {"layers": layers},
                   "start": 0, "duration": 0.1})
        self.assertEqual(caught.exception.code, 400)
        self.assertIn("at most 128",
                      json.loads(caught.exception.read()).get("error", ""))

        # The measurement a real grade asks for is untouched: a two component
        # stack still comes back with numbers.
        matte_id = self._stack_matte()
        ok = self._stats(mask={"components": [
            {"type": "matte", "op": "add", "matte": {"id": matte_id}},
            {"type": "luma", "op": "intersect",
             "key": {"lum_low": 0.0, "lum_high": 1.0}}]})
        self.assertIn("stats", ok)

    # -- generated caches are per run (checkpoint gap 22) ------------------

    def test_the_luts_this_server_bakes_land_in_its_own_cache(self):
        """Checkpoint gap 22. LUT_LAYERS, LUT_MASKS and the slice cache were
        module level constants under grade/, so every studio run on one clip
        baked into the same three folders no matter which data directory it
        was started with: two runs could collide on one hash-named file, and a
        run's own generated files were impossible to point at. They resolve
        from the cache root the server already owns now, which for this test
        is a temporary --data-dir, so nothing here can be satisfied by a stale
        file in grade/luts.
        """
        health = _get(self.base + "/health")
        cache = Path(health["cache_dir"])
        self.assertTrue(str(cache).startswith(str(Path(self.data_dir).resolve())),
                        f"{cache} is not inside this run's data dir")
        shipped = GRADE / "luts"
        before = {name: sorted(q.name for q in (shipped / name).glob("*"))
                  for name in ("layers", "masks", "slice")}
        # A layer with a correction bakes a layer cube, its window component
        # bakes a mask, and the hue curves bake a slice cube: the three
        # generated caches this gap is about, in one frame.
        cfg = {"layers": [
            {"enabled": True, "name": "L1", "placement": "before_look",
             "mask": {"components": [
                 {"id": "c1", "type": "window", "op": "add",
                  "window": {"shape": "rect", "w": 0.6, "h": 0.6,
                             "softness": 0.2}}]},
             "correct": {"exposure": 0.4, "contrast": 1.1}}],
            "hue_curves": {"enabled": True,
                           "hue_sat": [[30.0, 1.25], [210.0, 0.85]]}}
        body, _headers = _post(self.base + "/frame",
                               {"clip": self.clip, "time": 0.1, "width": 320,
                                "config": cfg}, raw=True, timeout=180.0)
        self.assertGreater(len(body), 0)
        for name in ("layers", "masks", "slice"):
            baked = sorted((cache / "luts" / name).glob("*"))
            self.assertTrue(baked, f"nothing baked into {cache / 'luts' / name}")
            after = sorted(q.name for q in (shipped / name).glob("*"))
            self.assertEqual(
                after, before[name],
                f"the render added files to {shipped / name}; generated LUTs "
                f"belong to the run, not to the checkout")

    # -- the matte routes as ROUTES: what an id may be, what DELETE may
    #    remove, and whose clip a matte is (round 1 blocker 1, majors 6
    #    and 12, minor 38)
    # ----------------------------------------------------------------------

    def _matte_root(self) -> Path:
        return Path(self.data_dir) / "mattes"

    def test_delete_of_a_path_shaped_id_is_refused_and_the_directory_lives(self):
        """The review's own reproduction, as a test.

        `mattes.resolve()` used to accept a matte id that was itself a
        filesystem path, and `info_from_dir` builds a MatteInfo for ANY
        directory (no index.json needed), so
        `DELETE /api/matte//tmp/.../VICTIM` answered `{"deleted": ...}` and the
        directory and its contents were gone. The route's two guards
        (`_guard_read`, `_require_admin`) both return immediately with logins
        off, which is the documented default and the way every agent runs this
        server, so nothing else was in the way: pointed at footage, grade/out
        or studio/data it deleted the founder's files.

        On the old code this fails on the surviving-directory assertion.
        """
        victim = Path(self.data_dir) / "VICTIM"
        victim.mkdir(parents=True, exist_ok=True)
        keeper = victim / "founder-footage.mov"
        keeper.write_text("not a matte, not yours to delete")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            _delete(self.base + f"/matte/{victim}")
        self.assertEqual(caught.exception.code, 404)
        self.assertTrue(victim.is_dir(),
                        "DELETE /api/matte/<path> removed a directory that is "
                        "not a matte")
        self.assertTrue(keeper.is_file(), "the file inside it went too")

    def test_the_matte_routes_refuse_an_id_that_is_not_an_id(self):
        """One pattern, all three `matte/...` routes, checked before the id is
        ever joined onto a path. `_dispatch` does not unquote the route, so a
        plain absolute path in the URL reaches the handler intact and `%2F` is
        not even needed."""
        for bad in (str(Path(self.data_dir)), "/etc", "..", "../..",
                    ".hidden", "m" * 90):
            for suffix in ("", "/frame"):
                with self.assertRaises(urllib.error.HTTPError,
                                       msg=f"GET {bad}{suffix}") as caught:
                    _get(self.base + f"/matte/{bad}{suffix}")
                self.assertEqual(caught.exception.code, 404, f"GET {bad}")
            with self.assertRaises(urllib.error.HTTPError,
                                   msg=f"DELETE {bad}") as caught:
                _delete(self.base + f"/matte/{bad}")
            self.assertEqual(caught.exception.code, 404, f"DELETE {bad}")

    def test_delete_refuses_a_directory_in_the_store_that_is_not_a_matte(self):
        """The id is well formed and the directory is inside the store, and it
        still is not a matte: no index.json and no frames. 400, and the files
        are still there afterwards."""
        junk = self._matte_root() / "notaclipkey" / "m_notamatte"
        junk.mkdir(parents=True, exist_ok=True)
        notes = junk / "notes.txt"
        notes.write_text("somebody's working files")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            _delete(self.base + "/matte/m_notamatte")
        self.assertEqual(caught.exception.code, 400)
        self.assertIn("not a matte",
                      json.loads(caught.exception.read()).get("error", ""))
        self.assertTrue(notes.is_file())
        shutil.rmtree(junk.parent, ignore_errors=True)

    def test_delete_removes_a_real_matte(self):
        """The other half of the guard: it refuses what is not a matte and
        still deletes what is one. Without this, deleting the rmtree
        altogether would pass every refusal test above."""
        result = self._track(prompts={"text": ["delete me please"]})
        matte_id = result["mattes"][0]["matte_id"]
        info = self._wait_matte_state(matte_id, ("done",))
        path = self._matte_root() / info["clip_key"] / matte_id
        self.assertTrue(path.is_dir(), f"no matte directory at {path}")
        out = _delete(self.base + f"/matte/{matte_id}")
        self.assertEqual(out["deleted"], matte_id)
        self.assertFalse(path.exists(), "the matte directory is still there")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            _get(self.base + f"/matte/{matte_id}")
        self.assertEqual(caught.exception.code, 404)

    def test_a_matte_tracked_on_another_clip_is_refused(self):
        """Major 6. index.json records the clip and the clip_key the track ran
        on and nothing outside the browser compared either with the clip in
        front of it, so a landscape clip measured through a portrait matte from
        a different clip answered with numbers, no warning and exit 0, and a
        render stretched that matte over the wrong picture and wrote the file.
        Both weight forms and the render path, with `allow_partial` on so this
        cannot be the coverage refusal in disguise."""
        other = _write_fixture_matte(self._matte_root(), "notthisclipskey",
                                     "m_otherclip", "somebody-elses-clip.mov")
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                _post(self.base + "/stats",
                      {"clip": self.clip, "time": 0.1, "width": 64,
                       "config": {}, "matte": "m_otherclip"})
            self.assertEqual(caught.exception.code, 400)
            message = json.loads(caught.exception.read()).get("error", "")
            self.assertIn("somebody-elses-clip.mov", message)
            self.assertIn(self.clip, message)

            # The same matte as a component stack: the other way a
            # measurement can be weighted, refused the same way.
            with self.assertRaises(urllib.error.HTTPError) as caught:
                _post(self.base + "/stats",
                      {"clip": self.clip, "time": 0.1, "width": 64,
                       "config": {},
                       "mask": {"components": [
                           {"type": "matte", "op": "add",
                            "matte": {"id": "m_otherclip"}}]}})
            self.assertEqual(caught.exception.code, 400)
            self.assertIn("somebody-elses-clip.mov",
                          json.loads(caught.exception.read()).get("error", ""))

            with self.assertRaises(urllib.error.HTTPError) as caught:
                _post(self.base + "/render",
                      {"clip": self.clip,
                       "config": _matte_layer_config("m_otherclip"),
                       "duration": 0.2, "allow_partial": True,
                       "name": "masktest_wrong_clip"})
            self.assertEqual(caught.exception.code, 400)
            message = json.loads(caught.exception.read()).get("error", "")
            self.assertIn("layer 0 component 0", message)
            self.assertIn("somebody-elses-clip.mov", message)
            self.assertIn(self.clip, message)

            # And a matte of THIS clip still measures, so the check refuses a
            # mismatch rather than every matte.
            good = self._track(prompts={"text": ["same clip subject"]})
            good_id = good["mattes"][0]["matte_id"]
            self._wait_matte_state(good_id, ("done",))
            ok = _post(self.base + "/stats",
                       {"clip": self.clip, "time": 0.1, "width": 64,
                        "config": {}, "matte": good_id})
            self.assertEqual(ok["matte"], good_id)
        finally:
            shutil.rmtree(other.parent, ignore_errors=True)

    def test_stats_refuses_a_config_carrying_another_clips_matte(self):
        """Round 2 finding 51, and the rest of round 1's finding 6.

        The ownership guard only ever looked at the `matte` and `mask`
        REQUEST parameters. A measurement can be weighted a third way: the
        `config` itself, whose layers each carry a mask block with matte
        components, and `_grade_frame_stats` grades the frame through those
        layers before it measures. So the exact thing finding 6 exists to
        stop (a landscape clip measured through a portrait matte tracked on
        somebody else's clip, answered with numbers and exit 0) was still
        reachable by putting the matte in the config instead of in `matte`.
        `render` checked it; `stats` and `sweep` did not.

        The refusal names the layer and the component, because a config can
        carry a dozen of them and "one of your mattes is wrong" is not an
        answer somebody can act on.
        """
        other = _write_fixture_matte(self._matte_root(), "notthisclipskey",
                                     "m_cfgclip", "somebody-elses-clip.mov")
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                _post(self.base + "/stats",
                      {"clip": self.clip, "time": 0.1, "width": 64,
                       "config": _matte_layer_config("m_cfgclip")})
            self.assertEqual(caught.exception.code, 400)
            message = json.loads(caught.exception.read()).get("error", "")
            self.assertIn("somebody-elses-clip.mov", message)
            self.assertIn(self.clip, message)
            self.assertIn("layer 0 component 0", message)

            # The `times` form is the same route and the same guard, and it
            # is the one an agent loops through, so it is pinned too.
            with self.assertRaises(urllib.error.HTTPError) as caught:
                _post(self.base + "/stats",
                      {"clip": self.clip, "times": [0.1, 0.2], "width": 64,
                       "config": _matte_layer_config("m_cfgclip")})
            self.assertEqual(caught.exception.code, 400)
            self.assertIn("somebody-elses-clip.mov",
                          json.loads(caught.exception.read()).get("error", ""))

            # And a config carrying a matte of THIS clip still measures, so
            # this refuses a mismatch and not every config with a matte in it.
            good = self._track(prompts={"text": ["config matte subject"]})
            good_id = good["mattes"][0]["matte_id"]
            self._wait_matte_state(good_id, ("done",))
            ok = _post(self.base + "/stats",
                       {"clip": self.clip, "time": 0.1, "width": 64,
                        "config": _matte_layer_config(good_id)})
            self.assertIn("stats", ok)
        finally:
            shutil.rmtree(other.parent, ignore_errors=True)

    def test_the_matte_frame_route_clamps_an_absurd_width(self):
        """Round 2 finding 53: `GET /api/matte/<id>/frame?width=` was passed
        to ffmpeg's scale with only `max(1, int(width))` on it.

        A width of 100000 on a 16:9 matte is a 100000 x 56250 plane: about
        5.6 gigapixels, and the gray16 intermediate ffmpeg scales through is
        two bytes a pixel, so one GET could ask this machine (which is also
        holding the model and the founder's live grading run) for more than
        ten gigabytes. It is an unauthenticated GET with logins off, which is
        how the studio runs on a laptop.

        The cap is 3840, one 4K frame, which is wider than any preview or
        render the studio itself asks for. It is a CLAMP rather than a
        refusal because a caller asking for a huge matte preview wants a
        picture, not an error, and the picture at 3840 is the same picture.
        """
        matte_id = self._track(prompts={"text": ["wide subject"]}
                               )["mattes"][0]["matte_id"]
        self._wait_matte_state(matte_id, ("done",))

        def png_and_headers(url):
            png, headers = _post_none_get_raw(url)
            # The IHDR width is bytes 16 to 20 of any PNG, so this reads the
            # real picture rather than trusting a header the route sets.
            self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
            return int.from_bytes(png[16:20], "big"), headers

        def png_width(url):
            return png_and_headers(url)[0]

        self.assertEqual(
            png_width(self.base + f"/matte/{matte_id}/frame?time=0&width=200"),
            200)
        self.assertEqual(
            png_width(self.base
                      + f"/matte/{matte_id}/frame?time=0&width=100000"),
            3840)
        # A negative or zero width still lands on a real picture rather than
        # a scale filter of 0, which is the other end of the same guard.
        self.assertGreaterEqual(
            png_width(self.base + f"/matte/{matte_id}/frame?time=0&width=-5"),
            1)

        # Round 3 finding 84: the clamp said nothing on a route whose whole
        # design is to announce a fallback (X-Matte-State and X-Matte-Frame
        # exist so a caller can tell a nearest written frame from an exact
        # one). A tool asking for 8000 got 3840 pixels with no way to tell a
        # clamp from a matte that happens to be 3840 wide. The served width is
        # always stated, and the width that was ASKED for appears only when it
        # was not honoured, so the header's presence IS the signal.
        width, headers = png_and_headers(
            self.base + f"/matte/{matte_id}/frame?time=0&width=200")
        self.assertEqual(headers.get("X-Matte-Width"), str(width))
        self.assertIsNone(headers.get("X-Matte-Width-Asked"),
                          "a width that was honoured must not look clamped")
        width, headers = png_and_headers(
            self.base + f"/matte/{matte_id}/frame?time=0&width=100000")
        self.assertEqual(headers.get("X-Matte-Width"), "3840")
        self.assertEqual(width, 3840)
        self.assertEqual(headers.get("X-Matte-Width-Asked"), "100000",
                         "a clamped width has to say what was asked for, or a "
                         "clamp is indistinguishable from a 3840 wide matte")
        # And the floor announces itself the same way.
        width, headers = png_and_headers(
            self.base + f"/matte/{matte_id}/frame?time=0&width=-5")
        self.assertEqual(headers.get("X-Matte-Width"), str(width))
        self.assertEqual(headers.get("X-Matte-Width-Asked"), "-5")
        # A request with no width at all is untouched: nothing was asked, so
        # nothing is announced.
        _png, headers = _post_none_get_raw(
            self.base + f"/matte/{matte_id}/frame?time=0")
        self.assertIsNone(headers.get("X-Matte-Width"))
        self.assertIsNone(headers.get("X-Matte-Width-Asked"))

    def test_ids_from_the_service_are_never_used_as_paths(self):
        """Round 2 finding 65: the studio joined ids that came off the SAM
        wire straight into filesystem paths.

        `_matte_dir` is `matte_root() / clip_key / matte_id`, and the pick
        cache writes `MASK_PICK_CACHE / f"{pick_id}_{inst_id}_overlay.jpg"`.
        `Path("/a") / "/b"` is `Path("/b")`, which is the exact reasoning
        sam/store.py writes out for its own side: round 1 finding 13 hardened
        the service and left the studio trusting whatever the service
        answered. `--sam-url` is a configuration decision rather than a
        request, so this is a hardening rather than a live hole, but it is
        the same two lines on both sides of one wire.

        The fake service answers with a matte id of `../../escaped-matte`, a
        pick id of `../../escaped-pick` and an instance id of
        `/tmp/fixxr-escaped-inst` when a prompt says so.
        """
        strays = [Path("/tmp/fixxr-escaped-inst_overlay.jpg"),
                  Path(self.data_dir).parent / "escaped-matte",
                  Path(self.data_dir) / "escaped-matte"]
        for stray in strays:
            self.assertFalse(stray.exists(), f"{stray} exists before the test")

        # track: refused outright, because a matte id is how every later
        # route names this matte and there is nothing safe to substitute.
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._track(prompts={"text": ["__escape__ subject"]})
        self.assertEqual(caught.exception.code, 400)
        message = json.loads(caught.exception.read()).get("error", "")
        self.assertIn("matte id", message)

        # segment: answered, but under this studio's own id. An instance id
        # that is not a name is skipped rather than substituted, because an
        # instance id is how a caller asks for that instance back.
        out = self._segment(prompts={"text": ["__escape__ two"]})
        self.assertNotIn("..", str(out.get("pick_id")))
        self.assertNotIn("/", str(out.get("pick_id")))
        for inst in out.get("instances") or []:
            self.assertNotIn("/", str(inst.get("id")))
            self.assertNotIn("..", str(inst.get("id")))

        cache = Path(self.data_dir) / "cache" / "mask_pick"
        if cache.is_dir():
            for f in cache.iterdir():
                self.assertEqual(f.name, Path(f.name).name)
                self.assertNotIn("..", f.name)
        for stray in strays:
            self.assertFalse(stray.exists(),
                             f"{stray} was written outside the store")
        # And the ordinary path still works, so this refuses hostile ids and
        # not every id.
        good = self._segment(prompts={"text": ["person"]})
        self.assertTrue(good.get("instances"))

    def test_the_matte_list_with_no_clip_still_answers_with_logins_off(self):
        """Major 12's fix filters the no-clip list by the same read guard the
        `?clip=` branch uses. With logins off that guard is a no-op, so this
        server (and every agent's) sees exactly what it saw before: the
        refusal half is proved on a server with logins on, in
        test_identity.py."""
        matte_id = self._track(prompts={"text": ["listed everywhere"]}
                               )["mattes"][0]["matte_id"]
        self._wait_matte_state(matte_id, ("done",))
        listing = _get(self.base + "/matte")
        self.assertIn(matte_id, [m["matte_id"] for m in listing["mattes"]])

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


def _write_fixture_matte(root: Path, clip_key: str, matte_id: str, clip: str,
                        frames: int = 4, fps: float = 10.0,
                        width: int = 16, height: int = 9) -> Path:
    """A matte directory written by hand, for a case a track cannot make.

    The fake service only ever tracks the one clip this suite opened, so "a
    matte belonging to a DIFFERENT clip" has to be built on disk. Real
    index.json fields (C2) and real frames, so the routes read it exactly as
    they read a tracked one; everything lands under the test server's own
    --data-dir and the caller removes it again.
    """
    import numpy as np                                        # noqa: PLC0415

    d = Path(root) / clip_key / matte_id
    d.mkdir(parents=True, exist_ok=True)
    for i in range(frames):
        arr = np.zeros((height, width), dtype=np.uint8)
        arr[:, :width // 2] = 255
        MT.write_gray_png(d / MT.frame_name(i), arr)
    (d / MT.INDEX_NAME).write_text(json.dumps({
        "matte_id": matte_id, "clip": clip, "clip_key": clip_key,
        "rotation": 0, "fps": fps, "frames": frames,
        "start_frame": 0, "end_frame": frames,
        "width": width, "height": height,
        "recipe": {"prompts": {"text": ["somebody else's subject"]}},
        "state": "done", "done_frames": frames,
        "areas": [0.5] * frames, "scores": [0.9] * frames,
        "created": time.time(), "model": "fixture", "backend": "fixture",
    }))
    return d


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
