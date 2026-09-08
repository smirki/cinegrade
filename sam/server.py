#!/usr/bin/env python3
"""Contract C3: the SAM masks service.

    uv sync --project sam                 # numpy and pillow: the stub runs
    uv sync --project sam --extra mlx     # + mlx-cv: the real model runs
    uv run --project sam python sam/server.py --stub --port 7560
    uv run --project sam python sam/server.py --backend auto --port 7560

One process, one model, one queue. The studio, the CLI and the browser never
wait on the model: they ask this service for a matte and read PNGs off disk
(contract C2). A pick (one frame) is synchronous and jumps ahead of tracks in
the queue; a track (a clip) is a job with progress, cancel and a state.

Why it is shaped this way, in one paragraph: SAM 3.1 is not realtime on this
Mac. A single text detect on a 1280 wide frame took 3.3 to 7.3 seconds on an
idle machine during the spike, and up to a minute with other agents running.
Tracking is one model pass per frame. So the design is not "make it fast", it
is "never let anything in the grading loop wait on it": queue the work, report
progress honestly, write each matte frame the moment it exists, and let the
picture keep playing off whatever is already on disk.

Standard library HTTP, like the studio's own server: nothing to install and
nothing to build. Loopback only; a non loopback host is refused at startup.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import resource
import subprocess
import signal
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, PriorityQueue

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from backends import (Cancelled, KNOWN, load_backend, normalize_prompts,   # noqa: E402
                      plan_objects, split_mask_score)
from backends.base import recipe_digest                                    # noqa: E402
from frames import FrameSource, VideoError, probe_rotation_tag, read_image  # noqa: E402
from modellock import ModelLock                                            # noqa: E402
from store import MatteWriter, utc_now                                     # noqa: E402

DEFAULT_PORT = 7560
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
MAX_PICKS = 32
MAX_JOBS_KEPT = 200
API_VERSION = 1


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


class Job:
    """One track. Its mattes are `MatteWriter`s that exist from the moment the
    job is queued, so the studio can show a queued matte before any model has
    run."""

    def __init__(self, job_id: str, request: dict, source: FrameSource,
                 start: int, end: int, mattes: list, prompts: dict,
                 select, warnings: list):
        self.id = job_id
        self.kind = "track"
        self.state = "queued"
        self.request = request
        self.source = source
        self.start = start
        self.end = end
        self.total_frames = end - start
        self.done_frames = 0
        self.mattes = mattes
        self.prompts = prompts
        self.select = select
        self.warnings = list(warnings)
        self.error = None
        self.cancel_requested = False
        self.created = utc_now()
        self.started = None
        self.finished = None
        self.created_at = time.time()
        self.started_at = None
        self.finished_at = None

    @property
    def elapsed_s(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    @property
    def rate_fps(self) -> float:
        elapsed = self.elapsed_s
        return round(self.done_frames / elapsed, 4) if elapsed > 0 and self.done_frames else 0.0

    def as_dict(self, queue_position: int | None = None) -> dict:
        rate = self.rate_fps
        left = max(0, self.total_frames - self.done_frames)
        return {
            "job_id": self.id,
            "kind": self.kind,
            "state": self.state,
            "done_frames": self.done_frames,
            "total_frames": self.total_frames,
            "fps": round(self.source.fps, 4),
            "rate_fps": rate,
            "elapsed_s": round(self.elapsed_s, 3),
            "eta_s": round(left / rate, 1) if rate else None,
            "matte_ids": [m.matte_id for m in self.mattes],
            "mattes": [m.summary() for m in self.mattes],
            "error": self.error,
            "warnings": self.warnings,
            "clip": self.request.get("clip") or str(self.source.path),
            "clip_key": self.request.get("clip_key"),
            "rotation": self.request.get("rotation", 0),
            "start_frame": self.start,
            "end_frame": self.end,
            "queue_position": queue_position,
            "cancel_requested": self.cancel_requested,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
        }


_MEMORY_CACHE = {"at": 0.0, "value": None}


def memory() -> dict:
    """What this process is costing the machine right now.

    On /health because the honest answer to "why is this taking minutes" is
    usually memory: the model is about 5 GB resident on a 16 GB Mac, and once
    it swaps, everything (this service, the studio, ffmpeg) crawls together.
    Peak comes from getrusage, which is free; the current figure comes from
    `ps`, which is not, so it is cached for a couple of seconds because the
    studio polls health while a job runs.
    """
    now = time.time()
    if _MEMORY_CACHE["value"] is not None and now - _MEMORY_CACHE["at"] < 2.0:
        return _MEMORY_CACHE["value"]
    rss_mb = None
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                             capture_output=True, text=True, timeout=2)
        rss_mb = round(int(out.stdout.strip()) / 1024, 1)
    except Exception:                                          # noqa: BLE001
        pass
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is bytes on macOS and kilobytes on Linux. This runs on the
    # founder's Mac, but the wrong divisor turns 44 MB into 43 GB, which
    # would be read as a memory leak, so it is worth being explicit.
    peak_mb = round(peak / (1048576 if sys.platform == "darwin" else 1024), 1)
    value = {"rss_mb": rss_mb, "peak_rss_mb": peak_mb}
    _MEMORY_CACHE.update(at=now, value=value)
    return value


def resolve_rotation(value, clip_path: str | None) -> int:
    """rotation as a plain int, the studio's "auto" resolved the same way
    the studio itself resolves it: probe the clip's own display-matrix
    tag (frames.probe_rotation_tag, the same field CG.probe() reads).

    A concrete quarter turn ("0", "90", "180", "270", or a bare number,
    string or int) passes straight through with int(). "auto" (any case)
    or a missing value probes clip_path; with no clip_path to probe at all
    it falls back to 0, same as an untagged file everywhere else in this
    codebase. This is the fix for the studio's default rotation ("auto")
    being refused: the service used to do `int(value or 0)`, which raises
    on the literal string "auto" instead of resolving it.
    """
    if value is None or value == "":
        return probe_rotation_tag(clip_path) if clip_path else 0
    s = str(value).strip().lower()
    if s == "auto":
        return probe_rotation_tag(clip_path) if clip_path else 0
    return int(value)


class _Pick:
    """One synchronous segment call waiting for the worker."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.done = threading.Event()
        self.result = None
        self.error = None


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class Service:
    def __init__(self, args, backend_kwargs: dict | None = None):
        self.args = args
        self.backend_kwargs = dict(backend_kwargs or {})
        self.data_dir = Path(args.data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.requested_backend = "stub" if args.stub else args.backend
        self.backend = None
        self.backend_errors: list[dict] = []
        self.lock = ModelLock(backend=self.requested_backend,
                              retry_s=args.lock_retry_s,
                              enabled=not args.no_model_lock and self.requested_backend != "stub",
                              steal_stale=getattr(args, "steal_stale_lock", False),
                              log=log)
        self.queue: PriorityQueue = PriorityQueue()
        # Picks live in the queue (so they are served first when the worker is
        # idle) AND in this list, which a running track drains between frames.
        # Without it a pick asked for while a 600 frame track runs would wait
        # for the whole track: the founder clicks the picture and nothing
        # happens for minutes. One model call still happens at a time, because
        # the pick runs on the worker thread, in the gap between two frames.
        self.pending_picks: list = []
        self.sequence = 0
        self.jobs: OrderedDict[str, Job] = OrderedDict()
        self.picks: OrderedDict[str, dict] = OrderedDict()
        self.current: Job | None = None
        self.busy = False
        self.jobs_done = 0
        self.jobs_failed = 0
        self.started_at = time.time()
        self.stopping = threading.Event()
        self.mutex = threading.Lock()
        self.worker = threading.Thread(target=self._work, name="sam-worker", daemon=True)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self.requested_backend != "stub":
            # Held for as long as the model is resident: that is what "one
            # model process at a time on this machine" means in practice.
            self.lock.acquire()
        self.backend, self.backend_errors = load_backend(
            self.requested_backend, log=log,
            outer_lock_held=self.lock.held or self.args.no_model_lock,
            **self.backend_kwargs)
        if self.backend is None:
            self.lock.release()
            reasons = "; ".join(f"{e['backend']}: {e['error']}" for e in self.backend_errors)
            log(f"[service] NO BACKEND LOADED. {reasons}")
            log("[service] the service will answer /health but refuse work with 503.")
            if self.args.require_backend:
                raise SystemExit(2)
        self.worker.start()

    def stop(self) -> None:
        self.stopping.set()
        with self.mutex:
            for job in self.jobs.values():
                if job.state in ("queued", "running"):
                    job.cancel_requested = True
        if self.backend is not None:
            try:
                self.backend.close()
            except Exception:                                  # noqa: BLE001
                pass
        self.lock.release()

    # -- queue -------------------------------------------------------------

    def _submit(self, priority: int, item) -> None:
        with self.mutex:
            self.sequence += 1
            self.queue.put((priority, self.sequence, item))

    def _take_pending_pick(self):
        with self.mutex:
            while self.pending_picks:
                pick = self.pending_picks.pop(0)
                if not pick.done.is_set():
                    return pick
        return None

    def _serve_pending_picks(self) -> None:
        pick = self._take_pending_pick()
        while pick is not None:
            self._run_pick(pick)
            pick = self._take_pending_pick()

    def queued_jobs(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.state == "queued"]

    def queue_position(self, job: Job) -> int | None:
        """1 for the next job to run, 0 while it is running, null once it is
        finished."""
        if job.state == "running":
            return 0
        if job.state != "queued":
            return None
        waiting = self.queued_jobs()
        return waiting.index(job) + 1 if job in waiting else None

    def health(self) -> dict:
        running = self.current.id if self.current else None
        return {
            "ok": self.backend is not None,
            "backend": self.backend.name if self.backend else None,
            "requested_backend": self.requested_backend,
            "model": self.backend.model if self.backend else None,
            "loaded": self.backend is not None,
            "busy": self.busy,
            "queue": {
                "running": running,
                "queued": len(self.queued_jobs()),
                "jobs": [j.as_dict(self.queue_position(j))
                         for j in list(self.jobs.values())[-20:][::-1]],
            },
            "window": getattr(self.backend, "window", None) if self.backend else None,
            "memory": memory(),
            "backend_errors": self.backend_errors,
            "data_dir": str(self.data_dir),
            "uptime_s": round(time.time() - self.started_at, 1),
            "jobs_done": self.jobs_done,
            "jobs_failed": self.jobs_failed,
            "model_lock": {"held": self.lock.held, "enabled": self.lock.enabled},
            "version": API_VERSION,
        }

    # -- the worker --------------------------------------------------------

    def _work(self) -> None:
        while not self.stopping.is_set():
            try:
                _, _, item = self.queue.get(timeout=0.5)
            except Empty:
                continue
            try:
                if isinstance(item, _Pick):
                    self._run_pick(item)
                else:
                    self._run_job(item)
            except Exception as exc:                           # noqa: BLE001
                # A failed job must never take the service down with it.
                log(f"[worker] unhandled: {type(exc).__name__}: {exc}")
                log(traceback.format_exc(limit=4))
            finally:
                self.busy = False
                self.current = None

    def _run_pick(self, pick: _Pick) -> None:
        if pick.done.is_set():
            # Already served between two frames of a running track.
            return
        with self.mutex:
            if pick in self.pending_picks:
                self.pending_picks.remove(pick)
        self.busy = True
        try:
            pick.result = self._segment(pick.payload)
        except Exception as exc:                               # noqa: BLE001
            pick.error = f"{type(exc).__name__}: {exc}"
            log(f"[pick] failed: {pick.error}")
        finally:
            pick.done.set()

    def _run_job(self, job: Job) -> None:
        if job.cancel_requested:
            self._end_job(job, "cancelled", "cancelled before it started")
            return
        self.current = job
        self.busy = True
        job.state = "running"
        job.started = utc_now()
        job.started_at = time.time()
        for matte in job.mattes:
            matte.set_state("running")
        log(f"[job {job.id}] running: {job.total_frames} frames, "
            f"{len(job.mattes)} matte(s), backend {self.backend.name}")

        by_slot = {m.index.get("object_id"): m for m in job.mattes}
        seen: set[str] = set()
        blank = np.zeros((job.source.height, job.source.width), dtype=np.float32)

        def on_frame(index: int, masks: dict) -> None:
            if job.cancel_requested or self.stopping.is_set():
                raise Cancelled("cancelled")
            absolute = job.start + int(index)
            for slot_id, value in masks.items():
                writer = by_slot.get(slot_id)
                if writer is None:
                    continue
                mask, score = split_mask_score(value)
                seen.add(slot_id)
                writer.push(absolute, mask, score)
            for slot_id in seen - set(masks):
                # Tracked but not present on this frame: an honest black
                # frame keeps the matte contiguous and the area curve true.
                by_slot[slot_id].push(absolute, blank, 0.0)
            job.done_frames = int(index) + 1
            if self.pending_picks:
                self._serve_pending_picks()

        try:
            self.backend.track(
                job.source.frames(job.start, job.end),
                job.source.fps, dict(job.prompts, max_instances=job.request.get("max_instances", 1)),
                job.select, on_frame)
        except Cancelled:
            self._end_job(job, "cancelled", "cancelled")
            return
        except Exception as exc:                               # noqa: BLE001
            log(f"[job {job.id}] failed: {type(exc).__name__}: {exc}")
            log(traceback.format_exc(limit=6))
            self._end_job(job, "failed", f"{type(exc).__name__}: {exc}")
            return
        self._end_job(job, "done", None)

    def _end_job(self, job: Job, state: str, error: str | None) -> None:
        expected = job.total_frames
        for matte in job.mattes:
            matte.flush()
            written = matte.done
            if written >= expected and state == "done":
                matte.finish("done")
            elif written == 0:
                if state == "done":
                    matte.finish("failed", f"the model found nothing for "
                                           f"{matte.index.get('label') or matte.index.get('object_id')}")
                else:
                    matte.finish("failed", error or state)
            else:
                matte.finish("partial", error or (None if state == "done" else state))
        job.state = state
        job.error = error
        job.finished = utc_now()
        job.finished_at = time.time()
        if state == "done":
            self.jobs_done += 1
        elif state == "failed":
            self.jobs_failed += 1
        log(f"[job {job.id}] {state}: {job.done_frames}/{expected} frames in "
            f"{job.elapsed_s:.1f}s ({job.rate_fps} frames/s)"
            + (f" -- {error}" if error else ""))

    # -- segment -----------------------------------------------------------

    def _segment(self, payload: dict) -> dict:
        image_path = payload["image"]
        prompts = payload["prompts"]
        max_instances = payload["max_instances"]
        out_dir = payload["out_dir"]
        warnings = list(payload.get("warnings", []))

        image = read_image(image_path)
        height, width = image.shape[:2]
        started = time.perf_counter()
        instances = self.backend.segment(image, prompts, max_instances)
        elapsed = time.perf_counter() - started

        pick_id = payload["pick_id"]
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out = []
        union = None
        for instance in instances:
            mask = np.clip(np.asarray(instance.mask, dtype=np.float32), 0.0, 1.0)
            path = out_dir / f"{instance.id}.png"
            Image.fromarray((mask * 255.0 + 0.5).astype(np.uint8), mode="L").save(path)
            union = mask if union is None else np.maximum(union, mask)
            out.append({
                "id": instance.id,
                "label": instance.label,
                "score": round(float(instance.score), 4),
                "box": [round(float(v), 6) for v in instance.box],
                "area": round(float(instance.area), 6),
                "mask": str(path),
                "prompt": instance.prompt,
            })
        semantic = None
        if union is not None:
            semantic = out_dir / "semantic.png"
            Image.fromarray((union * 255.0 + 0.5).astype(np.uint8), mode="L").save(semantic)
        if not out:
            warnings.append("the model found nothing for these prompts")

        with self.mutex:
            self.picks[pick_id] = {"pick_id": pick_id, "image": str(image_path),
                                   "instances": out, "created": utc_now(),
                                   "width": width, "height": height}
            while len(self.picks) > MAX_PICKS:
                self.picks.popitem(last=False)

        return {"ok": True, "pick_id": pick_id, "instances": out,
                "semantic": str(semantic) if semantic else None,
                "elapsed_s": round(elapsed, 3), "backend": self.backend.name,
                "width": width, "height": height, "warnings": warnings}

    # -- request handling --------------------------------------------------

    def segment(self, body: dict) -> dict:
        if self.backend is None:
            raise ServiceError(503, self._no_backend())
        image = body.get("image")
        if not image:
            raise ServiceError(400, "segment needs an image path")
        if not Path(image).is_file():
            raise ServiceError(400, f"there is no image at {image}")
        try:
            prompts = normalize_prompts(body.get("prompts"))
        except ValueError as exc:
            raise ServiceError(400, str(exc)) from None
        if not any(prompts[k] for k in ("text", "points", "boxes", "masks")):
            raise ServiceError(400, "segment needs at least one of prompts.text, "
                                    "prompts.points, prompts.boxes")
        max_instances = int(body.get("max_instances") or 8)
        warnings = []
        if prompts["exemplars"]:
            warnings.append("exemplar prompts are accepted and ignored: mlx-cv "
                            "0.0.4 has no exemplar entry point")
        pick_id = "p_" + uuid.uuid4().hex[:8]
        out_dir = body.get("out_dir") or (self.data_dir / "picks" / pick_id)

        pick = _Pick({"image": image, "prompts": prompts, "max_instances": max_instances,
                      "out_dir": str(out_dir), "pick_id": pick_id, "warnings": warnings})
        with self.mutex:
            self.pending_picks.append(pick)
        self._submit(0, pick)          # priority 0: a pick jumps ahead of tracks
        if not pick.done.wait(timeout=self.args.segment_timeout_s):
            with self.mutex:
                if pick in self.pending_picks:
                    self.pending_picks.remove(pick)
            raise ServiceError(504, f"the pick did not finish within "
                                    f"{self.args.segment_timeout_s}s; it is still "
                                    f"running and the model is busy")
        if pick.error:
            raise ServiceError(500, pick.error)
        return pick.result

    def track(self, body: dict) -> dict:
        if self.backend is None:
            raise ServiceError(503, self._no_backend())
        video = body.get("video") or body.get("clip")
        if not video:
            raise ServiceError(400, "track needs a video path (a file or a "
                                    "directory of frames)")
        warnings: list[str] = []

        # Prompts, either given outright or taken from a previous pick.
        pick_id = body.get("pick")
        select = body.get("select", "all")
        picked_from: dict[str, str] = {}
        labels: dict[str, str] = {}
        if pick_id:
            pick = self.picks.get(pick_id)
            if pick is None:
                raise ServiceError(400, f"pick {pick_id} is not in this service's "
                                        f"memory any more; segment again")
            wanted = list(pick["instances"]) if select in (None, "all") else \
                [i for i in pick["instances"] if i["id"] in set(select)]
            if not wanted:
                raise ServiceError(400, f"none of {select} is an instance of pick {pick_id}")
            prompts = {"boxes": [i["box"] for i in wanted]}
            for position, instance in enumerate(wanted):
                picked_from[f"b{position}"] = instance["id"]
                labels[f"b{position}"] = instance["label"] or instance["id"]
            select = "all"
            warnings.append("seeded from a pick: the tracked objects are named "
                            "b0, b1, ... in the order they were selected; "
                            "index.json keeps the pick's own instance id in "
                            "picked_from")
        else:
            try:
                prompts = normalize_prompts(body.get("prompts"))
            except ValueError as exc:
                raise ServiceError(400, str(exc)) from None
            if not any(prompts[k] for k in ("text", "points", "boxes", "masks")):
                raise ServiceError(400, "track needs at least one of prompts.text, "
                                        "prompts.points, prompts.boxes, or a pick")

        try:
            prompts = normalize_prompts(prompts)
        except ValueError as exc:
            raise ServiceError(400, str(exc)) from None
        max_instances = int(body.get("max_instances") or 1)

        try:
            source = FrameSource(video, fps=body.get("fps"), width=body.get("width"))
            start, end = source.range(body.get("start_frame") or 0, body.get("end_frame"))
        except VideoError as exc:
            raise ServiceError(400, str(exc)) from None
        if source.kind == "dir" and not body.get("fps"):
            warnings.append(f"no fps given for a frame directory: assuming {source.fps}")

        try:
            slots = plan_objects(prompts, max_instances=max_instances, select=select)
        except ValueError as exc:
            raise ServiceError(400, str(exc)) from None
        if not slots:
            raise ServiceError(400, "these prompts describe no object to track")

        steady = max(1, int(body.get("steady") or 1))
        clip_key = str(body.get("clip_key") or Path(video).name)
        # rotation_probe_path (studio/sam_client.py, INTEGRATION-A) wins when
        # given: studio's own "clip" field is a bare display name stored
        # verbatim in the matte record below, never a path this process can
        # open, while a direct caller (a test, an agent) that already sent a
        # real path in "clip" keeps working exactly as before.
        rotation = resolve_rotation(
            body.get("rotation"),
            body.get("rotation_probe_path") or body.get("clip") or video)
        out_dir = Path(body.get("out_dir") or (self.data_dir / "mattes" / clip_key))
        # The prompt block carries max_instances for the backends; the recipe
        # states it once at the top level instead of twice with two meanings.
        recipe_prompts = {k: v for k, v in prompts.items() if k != "max_instances"}
        recipe = body.get("recipe") or {
            "kind": "track", "prompts": recipe_prompts, "select": select,
            "max_instances": max_instances, "steady": steady,
            "start_frame": start, "end_frame": end,
        }

        job_id = "j_" + uuid.uuid4().hex[:8]
        mattes = []
        for slot in slots:
            matte_id = "m_" + recipe_digest(clip_key, rotation, source.width,
                                            recipe, slot.id, steady, start, end)
            header = {
                "clip": str(body.get("clip") or source.path),
                "clip_key": clip_key,
                "rotation": rotation,
                "fps": round(source.fps, 6),
                "frames": end,
                "start_frame": start,
                "end_frame": end,
                "width": source.width,
                "height": source.height,
                "recipe": recipe,
                "state": "queued",
                "model": self.backend.model,
                "backend": self.backend.name,
                "job_id": job_id,
                "object_id": slot.id,
                "label": labels.get(slot.id) or slot.label,
                "kind": slot.kind,
                "picked_from": picked_from.get(slot.id),
                "pick": pick_id,
            }
            mattes.append(MatteWriter(out_dir, matte_id, header, steady=steady))

        job = Job(job_id, dict(body, max_instances=max_instances, rotation=rotation), source,
                  start, end, mattes, prompts, select, warnings)
        with self.mutex:
            self.jobs[job_id] = job
            while len(self.jobs) > MAX_JOBS_KEPT:
                self.jobs.popitem(last=False)
        self._submit(1, job)
        log(f"[job {job_id}] queued: {video} frames {start}..{end}, "
            f"{[m.matte_id for m in mattes]}")
        return {
            "ok": True, "job_id": job_id,
            "matte_ids": [m.matte_id for m in mattes],
            "mattes": [dict(m.summary(), kind=m.index.get("kind"),
                            recipe=m.index.get("recipe")) for m in mattes],
            "state": job.state,
            "queue_position": self.queue_position(job),
            "total_frames": job.total_frames,
            "fps": round(source.fps, 4),
            "warnings": warnings,
        }

    def cancel(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if job is None:
            raise ServiceError(404, f"no job {job_id}")
        if job.state in ("done", "failed", "cancelled"):
            return {"ok": True, "job_id": job_id, "state": job.state}
        job.cancel_requested = True
        if job.state == "queued":
            self._end_job(job, "cancelled", "cancelled while queued")
        return {"ok": True, "job_id": job_id, "state": job.state}

    def _no_backend(self) -> str:
        reasons = "; ".join(f"{e['backend']}: {e['error']}" for e in self.backend_errors)
        return ("no SAM backend loaded, so there is nothing to segment with. "
                + (reasons or "no backend was tried"))


class ServiceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "sam-service/1"
    protocol_version = "HTTP/1.1"

    @property
    def service(self) -> Service:
        return self.server.service

    def log_message(self, fmt, *args):
        if self.server.service.args.verbose:
            log("[http] " + (fmt % args))

    # -- plumbing ----------------------------------------------------------

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _error(self, status: int, message: str, detail=None) -> None:
        payload = {"ok": False, "error": message}
        if detail:
            payload["detail"] = detail
        self._send(status, payload)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ServiceError(400, f"the request body is not JSON: {exc}") from None
        if not isinstance(body, dict):
            raise ServiceError(400, "the request body must be a JSON object")
        return body

    # -- routes ------------------------------------------------------------

    def do_GET(self):                                          # noqa: N802
        path = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(path.query)
        try:
            if path.path in ("/health", "/api/health", "/"):
                return self._send(200, self.service.health())
            if path.path == "/jobs":
                clip_key = (query.get("clip_key") or [None])[0]
                jobs = list(self.service.jobs.values())[::-1]
                if clip_key:
                    jobs = [j for j in jobs if j.request.get("clip_key") == clip_key]
                return self._send(200, {
                    "ok": True,
                    "jobs": [j.as_dict(self.service.queue_position(j)) for j in jobs],
                    "running": self.service.current.id if self.service.current else None,
                    "queued": len(self.service.queued_jobs()),
                })
            if path.path.endswith("/cancel"):
                return self._error(405, "cancel is a POST, not a GET")
            if path.path.startswith("/jobs/"):
                job_id = path.path.split("/")[2]
                job = self.service.jobs.get(job_id)
                if job is None:
                    return self._error(404, f"no job {job_id}")
                return self._send(200, dict(job.as_dict(self.service.queue_position(job)),
                                            ok=True, backend=self.service.backend.name
                                            if self.service.backend else None))
            if path.path.startswith("/picks/"):
                pick_id = path.path.split("/")[2]
                pick = self.service.picks.get(pick_id)
                if pick is None:
                    return self._error(404, f"no pick {pick_id}")
                return self._send(200, dict(pick, ok=True))
            return self._error(404, f"no route {path.path}")
        except ServiceError as exc:
            return self._error(exc.status, exc.message)
        except Exception as exc:                               # noqa: BLE001
            log(traceback.format_exc(limit=4))
            return self._error(500, f"{type(exc).__name__}: {exc}")

    def do_POST(self):                                         # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self._body()
            if path == "/segment":
                return self._send(200, self.service.segment(body))
            if path == "/track":
                return self._send(200, self.service.track(body))
            if path.startswith("/jobs/") and path.endswith("/cancel"):
                return self._send(200, self.service.cancel(path.split("/")[2]))
            return self._error(404, f"no route {path}")
        except ServiceError as exc:
            return self._error(exc.status, exc.message)
        except ValueError as exc:
            return self._error(400, str(exc))
        except Exception as exc:                               # noqa: BLE001
            log(traceback.format_exc(limit=4))
            return self._error(500, f"{type(exc).__name__}: {exc}")


class SamServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, service: Service):
        self.service = service
        super().__init__(address, handler)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SAM 3.1 masks service: one model, one queue, PNG mattes.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"default {DEFAULT_PORT}; never a framework default")
    parser.add_argument("--host", default="127.0.0.1",
                        help="loopback only: 127.0.0.1, localhost or ::1")
    parser.add_argument("--backend", default="auto", choices=list(KNOWN),
                        help="auto walks mlx, torch-mps, torch-cpu and logs each failure")
    parser.add_argument("--stub", action="store_true",
                        help="no model at all: synthetic drifting mattes, for tests")
    parser.add_argument("--data-dir", default=str(Path(os.environ.get("TMPDIR", "/tmp")) / "fixxr-sam"),
                        help="where picks and mattes go when the caller does not say")
    parser.add_argument("--model", default=None, help="override the weights repo id")
    parser.add_argument("--chunk-frames", type=int, default=None,
                        help="frames per tracker chunk (memory against restarts)")
    parser.add_argument("--no-model-lock", action="store_true",
                        help="do not take /tmp/fixxr-sam-model.lock (tests only)")
    parser.add_argument("--lock-retry-s", type=float, default=20.0)
    parser.add_argument("--steal-stale-lock", action="store_true",
                        help="remove the model lock when it has named no owner "
                             "for 15 minutes. Only when you have checked that "
                             "no model is loaded in another process: two models "
                             "on this machine swap it into the ground.")
    parser.add_argument("--segment-timeout-s", type=float, default=900.0)
    parser.add_argument("--require-backend", action="store_true",
                        help="exit rather than serve with no backend")
    parser.add_argument("--stub-delay-ms", type=float, default=0.0,
                        help="make the stub take this long per frame, so a "
                             "test can watch progress and cancel it (tests only)")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.host not in LOOPBACK:
        raise SystemExit(f"--host {args.host} is not loopback. This service "
                         f"binds {', '.join(sorted(LOOPBACK))} only: it runs "
                         f"models on the founder's machine and has no auth.")
    # Only the MLX backend takes these, so they are dropped for the others
    # rather than handed to a constructor that would reject them.
    backend_name = "stub" if args.stub else args.backend
    backend_kwargs = {}
    if backend_name in ("auto", "mlx"):
        if args.model:
            backend_kwargs["repo_id"] = args.model
        if args.chunk_frames:
            backend_kwargs["chunk_frames"] = args.chunk_frames
    elif backend_name == "stub":
        if args.stub_delay_ms:
            backend_kwargs["delay_ms"] = args.stub_delay_ms
        if args.chunk_frames:
            backend_kwargs["chunk_frames"] = args.chunk_frames

    service = Service(args, backend_kwargs)
    service.start()
    atexit.register(service.stop)

    server = SamServer((args.host, args.port), Handler, service)

    def shutdown(signum, _frame):
        log(f"[service] signal {signum}: stopping")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    backend_name = service.backend.name if service.backend else "none"
    log(f"[service] http://{args.host}:{args.port}  backend {backend_name}  "
        f"data {service.data_dir}")
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        service.stop()
        server.server_close()
        log("[service] stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
