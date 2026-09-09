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
import re
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
        # Per TRACK (job id, matte directory), per frame: area, score, IoU and
        # the mask that produced them, so index.json can carry the real arrays
        # the service writes (contract C2) instead of nothing at all.
        #
        # Per track, not per directory, since round 5 finding 99. Keeping it
        # per directory gave this fake a memory of every frame any run had ever
        # written into that matte, which the real store does not have: it
        # rebuilds the arrays from the index on disk and carries them forward
        # only where its own rule says the two runs are the same matte. So a
        # re-track that dropped every earlier frame's numbers in the real store
        # could not drop one here, and the widen that lost them was invisible
        # to every test in this file. Now a run knows only what it wrote, and
        # what survives from earlier runs survives through `_fake_index_write`
        # mirroring the store's carry forward, which is where the real rule is.
        self.frame_stats: dict[tuple, dict] = {}
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
        # The frame window the last /track asked for, so a test can prove what
        # was actually re-queued rather than inferring it from what landed
        # (tooling gap 24: a narrow `--force` must ask for the narrow window).
        self.last_track_window = None
        # The frame file the last /segment was handed, so a test can measure
        # what the model was really shown rather than trusting the number the
        # answer reports about itself (tooling gap 27).
        self.last_segment_image = None

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
                self.state.last_segment_image = body.get("image")
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
            # `start_frame`/`end_frame` are the window THIS call was asked for,
            # which the real service writes into index.json from the same place
            # (C2) and which the studio's cache decision, its widen test and
            # its force split all read as "the window this matte declares".
            # Without them every fake matte declared from frame 0 whatever
            # window it was tracked over, so the shape round 4 finding 89 is
            # about (a force that hangs off one end of a matte that does not
            # start at 0) could not be reached through this route at all.
            # `width` is the working width this call was given, which is what
            # the real service records (`"width": source.width`, and its
            # `FrameSource` takes its width from the body). Round 5 finding
            # 100: the studio predicts this field to decide whether the store
            # will carry a matte forward before it clears anything, so a fake
            # that wrote a fixed 20 here made every matte look like it was
            # tracked at another width. The frames themselves stay 20x15 and
            # `height` keeps their aspect, which is all the frame route reads
            # the pair for.
            asked_width = int(body.get("width") or 20)
            common = {"clip": body.get("clip"), "clip_key": body.get("clip_key"),
                     "rotation": body.get("rotation"), "fps": fps,
                     "frames": end, "start_frame": start, "end_frame": end,
                     "width": asked_width,
                     "height": max(2, int(round(asked_width * 15 / 20))),
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
                self.state.last_track_window = (start, end)
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
            # "hold" waits after HOLD_AFTER frames; "hold0" waits before the
            # first one, so a cancel can land on a track with nothing written.
            hold_at_zero = "hold0" in texts
            hold = "hold" in texts or hold_at_zero
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
                    kind, gate, HOLD_AT_ZERO if hold_at_zero else HOLD_AFTER),
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
                        _write_frame(self.state, job_id, d, i,
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
                                      **_frame_arrays(self.state, job_id, d,
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
                            # Tooling gap 26, and this is the C3 shape the
                            # real service writes (sam/server.py `_end_job`):
                            # a cancel with nothing on disk is `cancelled`,
                            # which is not `failed` (it tried and could not)
                            # and not `partial` (there is something usable).
                            job["matte_state"][mid] = "cancelled"
                            job["matte_error"][mid] = "cancelled"
                            _fake_index_write(Path(m["path"]), matte_id=mid,
                                              state="cancelled",
                                              error="cancelled")
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


def _jpeg_size(path) -> tuple:
    """(width, height) read from a JPEG's own SOF marker.

    Tooling gap 27 needs the size of the frame the SAM service was actually
    handed, and this venv has no image decoder guaranteed to be importable
    here, so the two numbers come off the file's own header: 0xFFC0..0xFFCF
    (bar the four that are not start of frame markers) carries height then
    width as big endian 16 bit values after a one byte precision field.
    """
    data = Path(str(path)).read_bytes()
    i = 2                                        # past the SOI marker
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9,
                      0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return w, h
        if marker == 0x01 or 0xD0 <= marker <= 0xD9:
            i += 2                               # markers with no payload
            continue
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    raise AssertionError(f"no start of frame marker in {path}")


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


def _write_frame(state: "_FakeSamState", job_id: str, dir_path: Path,
                 index: int, arr, score: float = 0.9) -> None:
    """Write one matte frame AND the per frame numbers that go with it.

    A mirror of sam/store.py's `MatteWriter._write`: the area is the mean of
    the clipped mask, the score is the model's own confidence, and the IoU is
    the overlap with the frame written before it, all rounded the way the real
    store rounds them. Round 1 finding 15: this fake wrote none of the three,
    so `quality()` through the real route was always judging an empty `areas`
    list and could only ever answer "nothing suspect" no matter what the
    frames held. Every quality assertion in this file was written against
    that.

    Kept per TRACK, the way the store keeps its previous mask on the writer
    object: the first frame a resumed or widened run writes has no predecessor
    in memory and gets no IoU, exactly as the real store reports it. This used
    to be kept per matte DIRECTORY and compared against whatever any earlier
    run had written, which reads as a denser IoU curve and is a fake that
    cannot lose a number the real store loses (round 5 finding 99).
    """
    MT.write_gray_png(dir_path / MT.frame_name(index), arr)
    cur = np.asarray(arr) >= 0.5
    with state.lock:
        rec = state.frame_stats.setdefault((str(job_id), str(dir_path)), {})
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


def _frame_arrays(state: "_FakeSamState", job_id: str, dir_path: Path,
                  frames) -> dict:
    """`areas`, `scores` and `ious` for index.json (contract C2), for the
    frames THIS track wrote.

    Indexed by ABSOLUTE frame index and padded with None to the declared
    frame count, which is what makes a partial matte obvious in the response
    and what `quality()` reads. What an earlier track measured is not in here:
    it is on disk, and it survives only where `_fake_index_write` carries it
    forward, which is the store's rule and not this fake's memory (round 5
    finding 99).
    """
    with state.lock:
        rec = {i: dict(v) for i, v in
               state.frame_stats.get((str(job_id), str(dir_path)), {}).items()}
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


def _same_picture(raw: dict, fields: dict) -> bool:
    """sam/store.py's `same_picture`, mirrored: are the index on disk and the
    header this write carries pictures of the same thing?

    The store carries an earlier run's per frame numbers forward for any window
    of the same picture and for nothing else, so this is the whole of its rule
    (round 5 finding 99; round 4 finding 89 mirrored the window half of the
    older rule here, which is what the store no longer asks). Same three
    fields, same "a key the older index does not carry is not a difference",
    same string comparison. The real one is pinned against the real store in
    `sam/tests/test_store.py`; this exists so a re-track through this route
    keeps and loses exactly what a re-track through the service would.
    """
    for key in ("clip_key", "rotation", "width"):
        if key in raw and str(raw.get(key)) != str(fields.get(key)):
            return False
    return True


def _fake_index_write(dir_path: Path, **fields) -> None:
    """A minimal stand-in for the real SAM service's own index.json writer
    (C2): merge-write under a lock, atomic rename, so a matte directory this
    fake has started never reads as state "partial" just because no index
    exists yet (mattes.info_from_dir's fallback for a bare directory of
    frames with no index.json at all, which is a real but different state:
    "a hand made matte a test built" per its own docstring, not "the service
    that owns this file hasn't written it yet").

    A write that carries a window (`frames`, `start_frame`, `end_frame`) goes
    through sam/store.py's `_carry_forward`, mirrored: for the same picture the
    matte spans the UNION of the two windows and every number the earlier run
    measured stays in its own absolute slot; for a different picture nothing is
    carried and this run's own window and arrays stand. That mirror is the
    point of this function since round 5 finding 99: `_frame_arrays` above now
    reports only what THIS track wrote, so what an earlier track measured
    survives here or nowhere, exactly as it does in the store.
    """
    dir_path.mkdir(parents=True, exist_ok=True)
    p = dir_path / MT.INDEX_NAME
    try:
        raw = json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        raw = {}
    same = bool(raw) and _same_picture(raw, fields)
    declared = raw.get("frames")
    incoming = fields.get("frames")
    length = int(incoming) if incoming is not None else 0
    if same and declared is not None and incoming is not None:
        # The longer of the two declarations, whichever way this window hangs:
        # a repair inside a longer matte keeps the matte's length (tooling gap
        # 24) and a widen past its end keeps the frames it already measured
        # (round 5 finding 99).
        length = max(int(declared), int(incoming))
        fields = dict(fields, frames=length)
    if same and raw.get("start_frame") is not None \
            and fields.get("start_frame") is not None:
        fields = dict(fields,
                      start_frame=min(int(raw["start_frame"]),
                                      int(fields["start_frame"])))
    if same and raw.get("end_frame") is not None \
            and fields.get("end_frame") is not None:
        fields = dict(fields,
                      end_frame=max(int(raw["end_frame"]),
                                    int(fields["end_frame"])))
    if same:
        for key in ("areas", "scores", "ious"):
            new = fields.get(key)
            if not isinstance(new, list):
                continue          # this write carries no arrays: leave disk
            old = raw.get(key)
            merged = list(new) + [None] * max(0, length - len(new))
            for i, value in enumerate(list(old or [])[:len(merged)]):
                if value is not None and merged[i] is None:
                    merged[i] = value
            fields = dict(fields, **{key: merged})
    raw.update({k: v for k, v in fields.items() if v is not None})
    raw.setdefault("created", time.time())
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(raw))
    tmp.replace(p)


HOLD_AFTER = 2                 # frames a held track writes before it waits
HOLD_TIMEOUT = 30.0            # and how long it waits before giving up
# A track whose prompts carry "hold0" waits before writing ANY frame, so a
# test can cancel a track that has not started without racing the writer
# (tooling gap 26: a cancel with nothing on disk is its own outcome).
HOLD_AT_ZERO = 0


def _run_slow_track(state: _FakeSamState, job_id: str, out_dir: Path,
                    mattes: list, total: int, common: dict,
                    start: int = 0, kind: str = "normal",
                    gate: threading.Event | None = None,
                    hold_after: int = HOLD_AFTER) -> None:
    """The timed writer: one frame every half second, cancellable.

    `gate` (a track whose prompts carry the word "hold") stops after
    `hold_after` frames and waits, so a test can cancel a track at a KNOWN
    number of written frames instead of racing it. The wait is bounded and a
    gate that is never released ends the thread rather than leaving it writing
    into a matte a later test reads. `hold_after` is `HOLD_AT_ZERO` for a
    track that must hold before its first frame ("hold0"), which is the only
    way to cancel a track that has written nothing without racing it.
    """
    for m in mattes:
        _fake_index_write(Path(m["path"]), matte_id=m["matte_id"], state="running")
    for i in range(total):
        with state.lock:
            job = state.jobs.get(job_id)
            if job is None or job["state"] == "cancelled":
                return
        # Read BEFORE this frame is written, so `hold_after` frames are on
        # disk when the wait starts whichever number it is, zero included.
        if gate is not None and i >= hold_after:
            if not gate.wait(timeout=HOLD_TIMEOUT):
                return                      # nobody released it: write no more
            gate = None                     # released: normal pace from here
            with state.lock:
                job = state.jobs.get(job_id)
                if job is None or job["state"] == "cancelled":
                    return
        for m in mattes:
            d = Path(m["path"])
            _write_frame(state, job_id, d, start + i,
                         _fake_mask(20, 15, start + i, kind=kind))
            _fake_index_write(d, matte_id=m["matte_id"], done_frames=i + 1,
                              **common,
                              **_frame_arrays(state, job_id, d,
                                              common.get("frames")))
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
            # Counted off the directory, not `total`: a resumed tail of 10
            # frames landing in a matte that now holds 12 has to report 12,
            # the way sam/store.py's `_carry_forward` recounts.
            on_disk = len([q for q in d.glob("*.png") if q.stem.isdigit()])
            _fake_index_write(d, matte_id=m["matte_id"], state="done",
                              done_frames=on_disk, **common,
                              **_frame_arrays(state, job_id, d,
                                              common.get("frames")))


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

    def test_segment_names_the_width_of_the_frame_it_showed_the_model(self):
        """Tooling gap 27: the same words, two widths, two different asks.

        A grader's `mask segment` returned 0 candidates for a phrase a
        previous session had picked with, and the phrase was not the
        difference: that studio was started with `--mask-width 720` and the
        other ran the 1280 default, and the model's own confidence for those
        words fell below its internal cutoff at the smaller size. Nothing in
        the answer said which frame the model had been shown, so the two
        sessions had no number to compare and the investigation took a day.

        The answer now carries the width and height of the frame that was
        actually served, read off the frame rather than off the setting, so
        the two cannot disagree. This measures the JPEG the fake service was
        handed: the number in the response has to be the number in the file.
        """
        out = self._segment(prompts={"text": ["two"]})
        served = self.sam_state.last_segment_image
        self.assertTrue(served and Path(served).is_file(),
                        f"the fake service was handed no frame: {served!r}")
        width, height = _jpeg_size(served)
        self.assertEqual((out["frame_width"], out["frame_height"]),
                         (width, height),
                         "the reported size is not the served frame's size")
        # Reported on a normal answer too, not only an empty one: comparing
        # two sessions' picks means comparing the widths they picked at, and
        # a session that found something still has to be comparable.
        self.assertGreater(out["candidates"], 0)
        # And it is the width this studio runs masks at, which is what
        # /api/mask/status reports as `mask_width` (the same setting
        # --mask-width and STUDIO_MASK_WIDTH carry), unless the source itself
        # is narrower than that, in which case the frame is what there was.
        status = _get(self.base + "/mask/status")
        self.assertLessEqual(out["frame_width"], int(status["mask_width"]))

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
        # Tooling gap 27: and which frame the model was looking at when it
        # found nothing, so "the words meant nothing to the model" can be
        # told apart from "the model was shown a smaller frame than the
        # session this is being compared with".
        width, _height = _jpeg_size(self.sam_state.last_segment_image)
        self.assertEqual(out["frame_width"], width)
        self.assertIn(f"0 candidates from the model on a {width}px frame",
                      out["message"])
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

    def _matte_dir(self, matte_id: str) -> Path:
        """The one directory on disk this matte's frames live in."""
        matches = sorted(self._matte_root().rglob(f"{matte_id}/{MT.INDEX_NAME}"))
        self.assertEqual(len(matches), 1, f"expected one {matte_id} on disk")
        return matches[0].parent

    def _frame_mtimes(self, matte_id: str) -> dict:
        return {p.name: p.stat().st_mtime_ns
                for p in self._matte_dir(matte_id).glob("*.png")}

    def _matte_index(self, matte_id: str) -> dict:
        """One matte's index.json as it is on disk.

        The declared window (`start_frame`, `end_frame`) is not on the route's
        summary, which reports the span read off the FILES, and the difference
        between those two is what round 4 finding 89 is about.
        """
        return json.loads((self._matte_dir(matte_id) / MT.INDEX_NAME).read_text())

    def _declared_window(self, matte_id: str) -> tuple:
        raw = self._matte_index(matte_id)
        return int(raw.get("start_frame") or 0), int(
            raw.get("end_frame") or raw.get("frames") or 0)

    def _wait_job_finished(self, job_id, timeout: float = 20.0) -> dict:
        """Poll GET /api/mask/jobs/<id> until the poller has settled it.

        A force refuses while a job for the same recipe is live (round 4
        finding 91), and the studio's own job status lags the frames on disk by
        up to one poll: the fake service finishes inside the POST while
        `_poll_sam_job` is still sleeping. So a test that means "the track is
        over" has to wait for the JOB, not only for the frames, or it is racing
        a refusal that is doing its job.
        """
        deadline = time.time() + timeout
        view = None
        while time.time() < deadline:
            view = _get(self.base + f"/mask/jobs/{job_id}")
            if view.get("finished") or view["state"] in (
                    "done", "failed", "cancelled"):
                return view
            time.sleep(0.2)
        raise AssertionError(f"mask job {job_id} never finished: {view}")

    def test_force_on_a_window_inside_the_matte_clears_only_that_window(self):
        """Tooling gap 24: `--force` narrower than the matte deleted all of it.

        The grader had a matte done over frames 0 to 288 and forced frames 173
        to 197 to repair one second of it. The answer named 173 and 197, which
        reads as "that window was cleared", and afterwards the matte declared
        197 frames over 7.21s to 8.21s with 24 PNGs in its folder: the other
        264 verified frames were gone, and the repair cost a full re-track.

        A force whose window covers the whole matte still throws it all away
        (the test below this one), because that is what it is for. This one is
        the narrower ask: clear the window, keep everything outside it, and
        say which frames went.
        """
        prompts = {"text": ["narrow force subject"]}
        first = self._track(prompts=prompts, start=0, end=0.5)
        matte_id = first["mattes"][0]["matte_id"]
        declared_end = first["end_frame"]
        done = self._wait_written(matte_id, declared_end)
        self.assertEqual(done["written_count"], declared_end)
        self.assertEqual(done["state"], "done")
        self._wait_job_finished(first["job_id"])
        fps = float(done["fps"])
        self.assertGreater(declared_end, 5,
                           "the window has to be longer than the repair, or "
                           "this is the whole matte force test")
        lo, hi = 2, 4                      # the repair window, in frames
        before = self._frame_mtimes(matte_id)
        self.assertEqual(len(before), declared_end)
        calls = self.sam_state.track_calls

        forced = self._track(prompts=prompts, start=lo / fps, end=hi / fps,
                            force=True)
        self.assertFalse(forced["cached"])
        self.assertTrue(forced["restarted"])
        self.assertEqual(self.sam_state.track_calls, calls + 1)
        self.assertEqual(forced["mattes"][0]["matte_id"], matte_id,
                         "a force writes back into the same matte")
        # What was cleared, as a frame range, which is the thing the old
        # message did not say: "previous frames cleared" was true of the whole
        # matte while the two numbers beside it named the window.
        self.assertEqual((forced["cleared_start"], forced["cleared_end"]),
                         (lo, hi))
        self.assertFalse(forced["cleared_whole_matte"])
        self.assertIn(f"frames {lo} to {hi} cleared", forced["message"])
        self.assertEqual((forced["start_frame"], forced["end_frame"]), (lo, hi))
        # And the wire call asked for that window and nothing else: this is
        # the difference between "the rest survived" and "the rest was tracked
        # again", which cost the grader the whole span.
        self.assertEqual(self.sam_state.last_track_window, (lo, hi))
        self.assertEqual(self.sam_state.last_matte_ids.get("0"), matte_id)

        after_info = self._wait_written(matte_id, declared_end)
        self.assertEqual(after_info["total_frames"], declared_end,
                         "the matte still declares the span it was tracked "
                         "over: this is the number that read 197 of 288")
        self.assertEqual(after_info["written_count"], declared_end,
                         "and every frame is on disk again")
        self.assertEqual(after_info["span"],
                         dict(after_info["span"], start_frame=0,
                              end_frame=declared_end, contiguous=True))
        after = self._frame_mtimes(matte_id)
        self.assertEqual(sorted(after), sorted(before))
        for name, was in before.items():
            index = int(Path(name).stem)
            if lo <= index < hi:
                self.assertNotEqual(after[name], was,
                                    f"frame {index} is inside the forced "
                                    f"window and was not written again")
            else:
                self.assertEqual(after[name], was,
                                 f"frame {index} is outside the forced window "
                                 f"and must not have been touched")
        full = _get(self.base + f"/matte/{matte_id}")
        self.assertEqual(len(full["areas"]), declared_end)
        self.assertTrue(all(a is not None for a in full["areas"]),
                        f"every frame has a number again: {full['areas']}")

    def test_force_over_the_whole_matte_still_throws_all_of_it_away(self):
        """The other half of gap 24: today's behaviour, kept.

        `--force` over the span the matte already covers is the documented
        escape hatch for "this matte is wrong, do it again". It deletes the
        frames, which is why every frame file is newer afterwards, and it says
        so with the same frame range the narrow case reports.
        """
        prompts = {"text": ["whole force subject"]}
        first = self._track(prompts=prompts, start=0, end=0.5)
        matte_id = first["mattes"][0]["matte_id"]
        declared_end = first["end_frame"]
        self._wait_written(matte_id, declared_end)
        self._wait_job_finished(first["job_id"])
        before = self._frame_mtimes(matte_id)

        forced = self._track(prompts=prompts, start=0, end=0.5, force=True)
        self.assertTrue(forced["restarted"])
        self.assertTrue(forced["cleared_whole_matte"])
        self.assertEqual((forced["cleared_start"], forced["cleared_end"]),
                         (0, declared_end))
        self.assertIn("the whole matte", forced["message"])
        self.assertEqual((forced["start_frame"], forced["end_frame"]),
                         (0, declared_end))
        after_info = self._wait_written(matte_id, declared_end)
        self.assertEqual(after_info["total_frames"], declared_end)
        after = self._frame_mtimes(matte_id)
        self.assertEqual(sorted(after), sorted(before))
        for name, was in before.items():
            self.assertNotEqual(after[name], was,
                                f"{name} survived a force over the whole span")

    def _force_refusal(self, prompts, start, end):
        """POST a force and return the 400's own sentence."""
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._track(prompts=prompts, start=start, end=end, force=True)
        self.assertEqual(caught.exception.code, 400)
        return json.loads(caught.exception.read()).get("error", "")

    def test_force_that_only_partly_overlaps_the_matte_is_refused(self):
        """Round 4 finding 89, the first of its two shapes: a force window that
        hangs off the LOW end of what the matte declares.

        The grader's matte was tracked over frames 173 to 197 (a `mask track
        --start 7.2 --end 8.2`, which is the normal way to make a matte that
        does not start at frame 0), and forcing frames 144 to 192 on it is the
        obvious next move. That is neither of the two shapes force had: not
        inside the declared span, not a superset of it. It took the narrow
        branch, so the studio kept the frames outside the window on disk and
        then asked the service for a window the store reads as a different
        track (`_inside_previous` refuses it, `_carry_forward` returns early),
        and the kept frames ended up past the end of the new arrays: five files
        the index could not describe at all, `done_frames` 48 against 53 files,
        `span` reporting end_frame 197 against declared_frames 192, and the
        answer on screen saying those frames were kept. Measured against the
        real store in `sam/tests/test_store.py`'s non-interior block.

        So it is refused, before a file is deleted, and the sentence names both
        ranges and the two asks that do work. This test proves the refusal AND
        that nothing moved, because a refusal after the delete would be worse
        than the bug.
        """
        prompts = {"text": ["partial overlap low subject"]}
        # The matte the grader had: the FIRST track for this recipe is a window
        # in the middle of the clip, so its declared span starts above zero.
        # The frame numbers come from the answer rather than from an fps this
        # test assumes, and the seconds below are computed back through the
        # matte's own fps, which is the proxy's.
        late = self._track(prompts=prompts, start=0.25, end=0.6)
        matte_id = late["mattes"][0]["matte_id"]
        lo, hi = late["start_frame"], late["end_frame"]
        self.assertGreaterEqual(lo, 5, "the window has to start above frame 0")
        self.assertGreaterEqual(hi - lo, 5,
                                "the window has to be long enough to have a "
                                "repair strictly inside it")
        info = self._wait_written(matte_id, hi - lo)
        fps = float(info["fps"])
        self._wait_job_finished(late["job_id"])
        self.assertEqual(self._declared_window(matte_id), (lo, hi),
                         "this test needs a matte that declares a window "
                         "starting above frame 0, or there is no low end to "
                         "hang off")
        before = self._frame_mtimes(matte_id)
        areas_before = _get(self.base + f"/matte/{matte_id}")["areas"]
        calls = self.sam_state.track_calls

        # It overlaps the first four frames of the matte and hangs off the front
        # by four, which is the grader's 144 to 192 against a 173 to 197 matte.
        req_lo, req_hi = lo - 4, lo + 4
        sentence = self._force_refusal(prompts, req_lo / fps, req_hi / fps)
        self.assertIn(f"frames {req_lo} to {req_hi}", sentence,
                      f"the refusal has to name what was asked for: {sentence}")
        self.assertIn(f"frames {lo} to {hi}", sentence,
                      f"and what the matte declares: {sentence}")
        self.assertIn(f"frames {lo} to {req_hi}", sentence,
                      f"and the repair that would work: {sentence}")
        self.assertIn(f"frames {req_lo} to {hi}", sentence,
                      f"and the redo that would work: {sentence}")
        self.assertEqual(self.sam_state.track_calls, calls,
                         "a refused force must not reach the SAM service")
        self.assertEqual(self._frame_mtimes(matte_id), before,
                         "a refused force must not touch one frame file")
        self.assertEqual(self._declared_window(matte_id), (lo, hi),
                         "and must not move the matte's declared window")
        after = _get(self.base + f"/matte/{matte_id}")
        self.assertEqual(after["areas"], areas_before)
        self.assertEqual(after["state"], "done")

        # And the repair the sentence names really does work: the intersection
        # is inside the declared span, so it clears its own window only.
        repair = self._track(prompts=prompts, start=lo / fps, end=req_hi / fps,
                            force=True)
        self.assertEqual((repair["cleared_start"], repair["cleared_end"]),
                         (lo, req_hi))
        self.assertFalse(repair["cleared_whole_matte"])
        self.assertEqual(repair["mattes"][0]["matte_id"], matte_id)
        self._wait_written(matte_id, hi - lo)
        self._wait_job_finished(repair["job_id"])
        repaired = _get(self.base + f"/matte/{matte_id}")
        self.assertEqual(self._declared_window(matte_id), (lo, hi),
                         "the repair kept the matte's own declared window")
        after_repair = self._frame_mtimes(matte_id)
        self.assertEqual(sorted(after_repair), sorted(before))
        for name, was in before.items():
            index = int(Path(name).stem)
            if index >= req_hi:
                self.assertEqual(after_repair[name], was,
                                 f"frame {index} is outside the repair and "
                                 f"must not have been touched")
            else:
                self.assertNotEqual(after_repair[name], was,
                                    f"frame {index} is inside the repair and "
                                    f"was not written again")
        self.assertTrue(all(a is not None for a in repaired["areas"][lo:hi]),
                        f"every frame of the span has a number: "
                        f"{repaired['areas']}")

    def test_force_that_runs_past_the_end_of_the_matte_is_refused(self):
        """Round 4 finding 89's mirror image, which is the quieter one.

        The grader's other matte was done over frames 0 to 288 of a 400 frame
        clip, and a force from 173 to past the end orphans nothing: the studio
        keeps frames 0 to 172 and their numbers, the store then rebuilds the
        arrays at length 400, and those 173 frames end up with a PNG each and
        no area, no score and no IoU. The matte reads `done`, 400 of 400 on
        disk, and `quality` reports zero suspect frames over frames nothing
        ever looked at: a clean bill of health on an unmeasured matte, which is
        worse than an obvious loss.

        Refused, with the widen named as the request that does what was meant.
        """
        prompts = {"text": ["partial overlap high subject"]}
        first = self._track(prompts=prompts, start=0, end=0.25)
        matte_id = first["mattes"][0]["matte_id"]
        end = first["end_frame"]
        info = self._wait_written(matte_id, end)
        fps = float(info["fps"])
        self._wait_job_finished(first["job_id"])
        before = self._frame_mtimes(matte_id)
        areas_before = _get(self.base + f"/matte/{matte_id}")["areas"]
        calls = self.sam_state.track_calls

        past = end + 6
        sentence = self._force_refusal(prompts, (end - 2) / fps, past / fps)
        self.assertIn(f"frames {end - 2} to {past}", sentence, sentence)
        self.assertIn(f"frames 0 to {end}", sentence, sentence)
        self.assertIn("without force", sentence.lower(),
                      f"the refusal names the widen, which is the request "
                      f"that keeps every frame already tracked: {sentence}")
        self.assertEqual(self.sam_state.track_calls, calls)
        self.assertEqual(self._frame_mtimes(matte_id), before)
        self.assertEqual(_get(self.base + f"/matte/{matte_id}")["areas"],
                         areas_before)

        # The widen the sentence points at: every frame already tracked stays
        # on disk WITH the numbers measured for it, and the new ones land in
        # the same matte. Round 5 finding 99: the files half of this was
        # asserted here and the numbers half was not, and the numbers were what
        # the widen threw away, against a fake that kept its own memory of
        # every area and so could not lose one. The fake now reports only the
        # frames each track wrote and carries the rest forward by the store's
        # own rule (`_same_picture`), which is the rule
        # `sam/tests/test_store.py` drives against the real store.
        wider = self._track(prompts=prompts, start=0, end=past / fps)
        self.assertTrue(wider["widened"])
        self.assertEqual(wider["mattes"][0]["matte_id"], matte_id)
        after = self._wait_written(matte_id, past)
        self.assertEqual(after["written_count"], past)
        self._wait_job_finished(wider["job_id"])
        grown = _get(self.base + f"/matte/{matte_id}")
        self.assertEqual(self._declared_window(matte_id), (0, past),
                         "the widened matte declares the union of the two "
                         "windows")
        self.assertEqual(len(grown["areas"]), past)
        self.assertTrue(all(a is not None for a in grown["areas"]),
                        f"a widen keeps every frame's area, not only its "
                        f"file: {grown['areas']}")
        self.assertEqual(grown["areas"][:end], areas_before,
                         "and the frames tracked before it keep the numbers "
                         "they already had")
        self.assertEqual(grown["done_frames"], past,
                         "and done_frames counts the matte, not this run")
        quality = grown["quality"]
        self.assertEqual(quality["checked"], past,
                         "so the quality pass judges every frame it claims to "
                         "have checked")

    def test_force_that_does_not_overlap_the_matte_at_all_says_so(self):
        """Round 5 finding 101: the refusal for a partly overlapping force was
        being used for windows that do not overlap the matte at all.

        It then asserted something false ("so it overlaps part of the matte and
        hangs off it") and named a repair computed as the intersection, which
        for these shapes is an empty range ("ask for frames 200 to 200"), an
        inverted one ("frames 100 to 50"), or the request itself. Forcing the
        frames after a matte's end is an ordinary ask, and following advice
        that cannot be typed is a loop.

        Four shapes, which is all of them: touching the low edge, touching the
        high edge, disjoint before, disjoint after. Every range the sentence
        names has to be a real range, whichever shape it is, and the two asks
        that do work for a window with nothing of this matte in it are the
        widen and the whole-span redo.
        """
        prompts = {"text": ["no overlap subject"]}
        made = self._track(prompts=prompts, start=0.25, end=0.6)
        matte_id = made["mattes"][0]["matte_id"]
        lo, hi = made["start_frame"], made["end_frame"]
        self.assertGreaterEqual(lo, 5, "the window has to start above frame 0")
        info = self._wait_written(matte_id, hi - lo)
        fps = float(info["fps"])
        self._wait_job_finished(made["job_id"])
        before = self._frame_mtimes(matte_id)
        calls = self.sam_state.track_calls

        for name, (req_lo, req_hi) in (
                ("touching the low edge", (lo - 4, lo)),
                ("touching the high edge", (hi, hi + 4)),
                ("disjoint before", (0, lo - 3)),
                ("disjoint after", (hi + 2, hi + 6))):
            sentence = self._force_refusal(prompts, req_lo / fps, req_hi / fps)
            self.assertIn("does not overlap the matte at all", sentence,
                          f"{name}: {sentence}")
            self.assertNotIn("overlaps part of the matte", sentence,
                             f"{name}: {sentence}")
            self.assertNotIn("To repair part of it", sentence,
                             f"{name}: there is nothing of this matte inside "
                             f"the window, so no repair can be named: "
                             f"{sentence}")
            self.assertIn(f"frames {req_lo} to {req_hi}", sentence,
                          f"{name}: the refusal names what was asked for: "
                          f"{sentence}")
            self.assertIn(f"frames {lo} to {hi}", sentence,
                          f"{name}: and what the matte declares: {sentence}")
            self.assertIn(f"frames {min(req_lo, lo)} to {max(req_hi, hi)}",
                          sentence,
                          f"{name}: and the redo that would work: {sentence}")
            self.assertIn("without force", sentence.lower(),
                          f"{name}: and the widen, which is the request that "
                          f"tracks these frames as well: {sentence}")
            # The finding itself, as a rule rather than as four literals: every
            # range this sentence names has to be a range somebody can type.
            named = re.findall(r"frames (\d+) to (\d+)", sentence)
            self.assertTrue(named, sentence)
            for a, b in named:
                self.assertLess(int(a), int(b),
                                f"{name}: the refusal names the empty or "
                                f"inverted range {a} to {b}: {sentence}")
            self.assertEqual(self.sam_state.track_calls, calls,
                             f"{name}: a refused force must not reach the SAM "
                             f"service")
            self.assertEqual(self._frame_mtimes(matte_id), before,
                             f"{name}: a refused force must not touch a frame")
            self.assertEqual(self._declared_window(matte_id), (lo, hi),
                             f"{name}: or move the declared window")

    def test_force_on_a_matte_recorded_at_another_working_width_is_refused(self):
        """Round 5 finding 100: the studio decided whether a force was an
        interior repair from the frame window alone, while sam/store.py also
        asks whether the two runs are the same PICTURE (clip key, rotation,
        working width) before it carries any number forward.

        A matte whose recorded width is not the width this track will send
        passes a window-only gate, has its window cleared, and is then read by
        the store as a different track: the frames the studio kept lose their
        area, their score and their IoU, which is round 4 finding 89's own loss
        through a door the frame numbers cannot see. The studio asks the
        store's own rule now (`picture_differences`, imported from
        sam/store.py), so this refuses instead.

        The recorded width is edited on disk here rather than reached through a
        second server, because the recipe hash keys this cache entry by the
        working width the studio ASKED for: two studios at different widths do
        not share a cache entry at all, which is that layer of the defence and
        is pinned by
        `test_the_track_cache_key_includes_rotation_and_the_working_width`.
        What is edited is what the SERVICE recorded, which is the field the
        store compares.
        """
        prompts = {"text": ["picture gap subject"]}
        made = self._track(prompts=prompts, start=0, end=0.5)
        matte_id = made["mattes"][0]["matte_id"]
        lo, hi = made["start_frame"], made["end_frame"]
        info = self._wait_written(matte_id, hi - lo)
        fps = float(info["fps"])
        self._wait_job_finished(made["job_id"])
        self.assertGreaterEqual(hi - lo, 4, "the matte needs an interior")
        raw = self._matte_index(matte_id)
        self.assertEqual(int(raw["width"]), MASK_WIDTH,
                         "the service records the working width it was sent, "
                         "which is the field the store compares")
        index_path = self._matte_dir(matte_id) / MT.INDEX_NAME

        # The same matte, recorded at another working width.
        index_path.write_text(json.dumps(dict(raw, width=MASK_WIDTH * 2)))
        before = self._frame_mtimes(matte_id)
        calls = self.sam_state.track_calls
        inner = (lo + 1, hi - 1)
        sentence = self._force_refusal(prompts, inner[0] / fps, inner[1] / fps)
        self.assertIn("width", sentence, sentence)
        self.assertIn(str(MASK_WIDTH * 2), sentence,
                      f"the refusal names the width the matte was tracked at: "
                      f"{sentence}")
        self.assertIn(str(MASK_WIDTH), sentence,
                      f"and the width this track works at: {sentence}")
        self.assertIn(f"frames {lo} to {hi}", sentence,
                      f"and the redo that would work: {sentence}")
        self.assertEqual(self.sam_state.track_calls, calls,
                         "a refused force must not reach the SAM service")
        self.assertEqual(self._frame_mtimes(matte_id), before,
                         "a refused force must not touch one frame file")

        # And it is the width that refused it, not the window: put the
        # recorded width back and the same interior force is accepted.
        index_path.write_text(json.dumps(raw))
        repair = self._track(prompts=prompts, start=inner[0] / fps,
                             end=inner[1] / fps, force=True)
        self.assertEqual((repair["cleared_start"], repair["cleared_end"]),
                         inner)
        self.assertFalse(repair["cleared_whole_matte"])
        self.assertEqual(repair["mattes"][0]["matte_id"], matte_id)
        self._wait_job_finished(repair["job_id"])

    def test_force_while_a_track_for_that_recipe_is_running_is_refused(self):
        """Round 4 finding 91: `_clear_matte_window`'s docstring said the
        frames it deletes are ones the service is not touching, and nothing
        checked it.

        The service's `MatteWriter` is built in its own track route, before the
        job is queued, and rewrites its whole in-memory index at least once a
        second while it runs, so a force landing mid track gives one matte
        directory two writers: the running one puts `areas`, `scores` and
        `ious` back for frames whose PNGs the studio has just deleted. The
        non-force branch already asks whether a live job covers the request;
        force deletes files, so it refuses on any live job for the recipe.

        "slow" plus "hold" holds the fake's writer after two frames, so the job
        here is provably still running rather than probably, and the frame count
        the force lands on is arithmetic.
        """
        prompts = {"text": ["slow", "hold", "live force subject"]}
        first = self._track(prompts=prompts, start=0, end=1.0)
        matte_id = first["mattes"][0]["matte_id"]
        job_id = first["job_id"]
        held = self._wait_written(matte_id, HOLD_AFTER)
        self.assertEqual(held["written_count"], HOLD_AFTER,
                         "the gate holds the writer here, so the force below "
                         "is not a race")
        live = _get(self.base + f"/mask/jobs/{job_id}")
        self.assertIn(live["state"], ("queued", "running"),
                      "this test needs a live job to force against")
        before = self._frame_mtimes(matte_id)
        calls = self.sam_state.track_calls

        sentence = self._force_refusal(prompts, 0, 1.0)
        self.assertIn(job_id, sentence,
                      f"the refusal names the job to cancel: {sentence}")
        self.assertIn("cancel", sentence.lower(), sentence)
        self.assertEqual(self.sam_state.track_calls, calls)
        for name, was in before.items():
            self.assertEqual(self._frame_mtimes(matte_id).get(name), was,
                             f"{name} was cleared under a running writer")

        # Cancelled, the way the sentence says, and then the force is allowed.
        _post(self.base + f"/mask/jobs/{job_id}/cancel", {})
        self._wait_job_finished(job_id)
        forced = self._track(prompts=prompts, start=0, end=1.0, force=True)
        self.assertTrue(forced["restarted"])
        self.assertEqual(forced["mattes"][0]["matte_id"], matte_id)
        self._post_cancel_if_live(forced["job_id"])

    def _post_cancel_if_live(self, job_id) -> None:
        """Stop a track this test started, so it is not still writing while the
        next test runs. Its own job id, never a pattern, never anything else's.
        """
        view = _get(self.base + f"/mask/jobs/{job_id}")
        if view["state"] in ("queued", "running"):
            _post(self.base + f"/mask/jobs/{job_id}/cancel", {})
        self._wait_job_finished(job_id)

    def test_force_with_nothing_cached_names_an_empty_cleared_range(self):
        """Round 4 finding 94: `--force`'s help says the answer names the range
        that was cleared either way, and a force with nothing in the recipe
        cache never entered the force branch at all, so `cleared_whole_matte`
        was a MISSING key rather than `false` for a scripted caller.

        Nothing was cleared, so the range is empty (`cleared_end` equals
        `cleared_start`), which is the same arithmetic a caller already does to
        see how much went.
        """
        prompts = {"text": ["first force with no cache"]}
        out = self._track(prompts=prompts, start=0, end=0.25, force=True)
        self.assertFalse(out["cached"])
        self.assertIn("cleared_whole_matte", out,
                      "a documented field cannot be absent on one of the two "
                      "paths that documents it")
        self.assertFalse(out["cleared_whole_matte"])
        self.assertEqual(out["cleared_start"], out["cleared_end"],
                         "nothing was cached, so nothing was cleared")
        self.assertEqual(out["cleared_start"], out["start_frame"])
        self._wait_job_finished(out["job_id"])

    def test_force_on_a_matte_that_declares_no_frames_keeps_nothing_back(self):
        """Round 4 finding 94's second half: the `kept` clause was built from
        the declared span without asking whether the span holds anything.

        A matte whose index was bootstrapped by this studio for an older or
        `--stub` service has no `frames` and no `end_frame`, and nothing has
        been written yet, so `_matte_declared_window` answers `(0, 0)` (its
        fallback is the highest written frame plus one, and there is none).
        Forcing frames 100 to 200 on it used to take the narrow branch and
        report "frames 0 to 100 kept" while zero files were removed and zero
        frames existed. An empty declaration has nothing to keep, so force
        takes the directory, which is also what makes it usable on the one
        matte it is most needed for.

        Built by tracking and then reducing what is on disk to that state,
        because this studio has no route that writes a bootstrap index: the
        three keys go and so do the frames, which is the shape a service that
        died before writing its first frame leaves behind.
        """
        prompts = {"text": ["a matte that declares nothing"]}
        first = self._track(prompts=prompts, start=0, end=0.25)
        matte_id = first["mattes"][0]["matte_id"]
        self._wait_job_finished(first["job_id"])
        folder = self._matte_dir(matte_id)
        path = folder / MT.INDEX_NAME
        raw = json.loads(path.read_text())
        for key in ("frames", "start_frame", "end_frame"):
            raw.pop(key, None)
        path.write_text(json.dumps(raw))
        for png in folder.glob("*.png"):
            png.unlink()
        MT.forget_cache()
        self.assertEqual(self._declared_window(matte_id), (0, 0),
                         "this test needs an index that declares no window")

        out = self._track(prompts=prompts, start=0.1, end=0.2, force=True)
        self.assertTrue(out["cleared_whole_matte"],
                        f"an empty declaration has nothing to keep: {out}")
        self.assertEqual((out["cleared_start"], out["cleared_end"]), (0, 0),
                         f"and nothing to name as cleared either: {out}")
        self.assertNotIn("kept", out["message"],
                         f"nothing was kept, so nothing may claim it: "
                         f"{out['message']}")
        self.assertIn("declares no frames of its own", out["message"])
        self._wait_job_finished(out["job_id"])

    def test_a_job_cancelled_before_it_wrote_anything_reads_cancelled(self):
        """Tooling gap 26: a cancelled track used to read as a failed one.

        The grader cancelled a track before it started and `mask list` showed
        `state=failed 0/N frames`, so the next reader went looking for a
        tracking failure that had never happened. Two halves, and both are
        here because either one alone still prints `failed`:

        * the service ends such a matte as `cancelled` (its own suite pins
          that: `sam/tests/service_e2e.py`, "and its matte says cancelled";
          the fake here writes the same C3 shape), and
        * this studio must not flatten it on the way through. Its poller
          mapped every non `done` service state onto `job.status = "failed"`,
          so the job the panel and `mask jobs` show went from `cancelled` to
          `failed` one poll after the cancel was accepted.

        The prompts carry "hold0", the fake's own gate before the FIRST frame,
        so "nothing written" is arithmetic rather than a race against a writer.
        """
        prompts = {"text": ["slow", "hold0", "cancel before it starts"]}
        first = self._track(prompts=prompts, start=0, end=0.5)
        job_id = first["job_id"]
        matte_id = first["mattes"][0]["matte_id"]
        requested = first["end_frame"]
        self.assertGreater(requested, 0)
        # Nothing on disk, and nothing about to be: the gate holds the writer
        # before frame 0. Read it rather than assume it.
        early = _get(self.base + f"/matte/{matte_id}")
        self.assertEqual(early["written_count"], 0,
                         "this test is about a track that wrote nothing")

        _post(self.base + f"/mask/jobs/{job_id}/cancel", {})
        stopped = self._wait_matte_state(matte_id, ("cancelled", "failed"),
                                        timeout=25.0)
        self.assertEqual(stopped["state"], "cancelled",
                         "a cancelled matte that wrote nothing is cancelled, "
                         "not failed: failed means the tracker tried and "
                         "could not")
        self.assertEqual(stopped["done_frames"], 0)
        self.assertEqual(stopped["total_frames"], requested,
                         "and the frames it was ASKED for are still there to "
                         "read, so a row can say 0 of N")

        # The list route, plain and --full, which is what the grader read.
        for query in (f"/matte?clip={self.clip}",
                      f"/matte?clip={self.clip}&full=1"):
            listing = _get(self.base + query)
            row = next(m for m in listing["mattes"]
                       if m["matte_id"] == matte_id)
            self.assertEqual(row["state"], "cancelled", query)
            self.assertEqual(row["done_frames"], 0, query)
            self.assertEqual(row["total_frames"], requested, query)

        # And the job itself keeps saying cancelled AFTER the poller has seen
        # the service settle. `finished` is set by the poller's own return, so
        # waiting for it is waiting for exactly the write that used to turn
        # this row into "failed".
        deadline = time.time() + 20.0
        view = None
        while time.time() < deadline:
            view = _get(self.base + f"/mask/jobs/{job_id}")
            if view.get("finished"):
                break
            time.sleep(0.2)
        self.assertIsNotNone(view)
        self.assertTrue(view.get("finished"),
                        "the poller never finished, so this proves nothing "
                        "about what it wrote")
        self.assertEqual(view["state"], "cancelled")
        self.assertIsNone(view["error"],
                          "a cancel is not an error to report; the reason "
                          "lives on the matte")

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
        self._wait_job_finished(first["job_id"])
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
        # Round 4 finding 95: zero is a number like any other, and it used to
        # be the one value that got neither the floor nor a header. The query
        # value arrives as the STRING "0", which is truthy, so it became the
        # integer 0 and then read as "no width asked for" one function down: a
        # documented floor of 1 that skipped the one value most likely to be
        # sent by a caller computing a width.
        width, headers = png_and_headers(
            self.base + f"/matte/{matte_id}/frame?time=0&width=0")
        self.assertEqual(width, 1, "zero clamps to the documented floor")
        self.assertEqual(headers.get("X-Matte-Width"), "1")
        self.assertEqual(headers.get("X-Matte-Width-Asked"), "0",
                         "and says what was asked for, the same as -5 above")
        # A request with no width at all is untouched: nothing was asked, so
        # nothing is announced. Same for `?width=` with nothing after it, which
        # names no number.
        for url in (f"/matte/{matte_id}/frame?time=0",
                    f"/matte/{matte_id}/frame?time=0&width="):
            _png, headers = _post_none_get_raw(self.base + url)
            self.assertIsNone(headers.get("X-Matte-Width"), url)
            self.assertIsNone(headers.get("X-Matte-Width-Asked"), url)

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
