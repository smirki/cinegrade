"""A tiny stand-in for studio/server.py's /api/mask/* and /api/matte* routes
(contract C4), used only by grade/tests/test_mask_tools.py to exercise the
CLI's `mask` verbs and studio/tools/grade_client.py's mask methods without a
real studio server or a real SAM service.

Lane M4 owns this: M5 has not built studio/server.py's mask routes yet at
the time this was written, so this is a threading http.server answering the
same JSON and image shapes contract C4 documents, standing in until the real
routes land. It is intentionally NOT a copy of studio/server.py: it knows
nothing about clips, footage, ffmpeg, projects or auth, only the mask/matte
wire shapes this file's own test exercises.

    python grade/tests/fake_studio_server.py --port 0     # picks a free port

Everything is in memory: a track "job" advances its own state a little more
on each poll (queued -> running -> done, or immediately "failed" when the
clip name is FAIL_CLIP), a matte's PNG frames are drawn on demand (an
ellipse that grows and drifts with time, the same idea C3's own --stub
describes for the real SAM service), and /api/frame returns a synthetic
JPEG instead of decoding real footage, so this file has no ffmpeg
dependency at all.
"""

from __future__ import annotations

import io
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw

STATE_LOCK = threading.Lock()


class FakeState:
    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.mattes: dict[str, dict] = {}
        self.picks: dict[str, dict] = {}
        self.next_id = 0
        # What GET /api/health answers for footage_dir / data_dir /
        # matte_root, so a test can point the CLI's own resolution at its
        # temp folders (checkpoint gaps 4 and 5).
        self.paths: dict[str, str] = {}
        # Saved presets, the namespace `--preset NAME` reads before the
        # built-in catalog when STUDIO_URL is set (checkpoint gap 17).
        self.presets: dict[str, dict] = {}
        # recipe key -> matte ids, so a repeat track is a cache hit and a
        # dead matte resumes (checkpoint gap 12).
        self.recipes: dict[str, list] = {}

    def new_id(self, prefix: str) -> str:
        self.next_id += 1
        return f"{prefix}{self.next_id}"


STATE = FakeState()


def _matte_frame_png(t: float, width: int, state: str) -> bytes:
    """An ellipse that grows and drifts rightward with time, 8 bit grey."""
    height = max(2, int(round(width * 9 / 16)))
    img = Image.new("L", (width, height), 0)
    d = ImageDraw.Draw(img)
    cx = int(width * (0.2 + 0.5 * min(1.0, t / 10.0)))
    cy = height // 2
    r = max(4, int(min(width, height) * 0.18))
    if state != "failed":
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=235)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _picture_jpeg(t: float, width: int) -> bytes:
    height = max(2, int(round(width * 9 / 16)))
    shade = int(60 + 20 * (t % 5))
    img = Image.new("RGB", (width, height), (shade, shade + 20, shade + 40))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


def _areas_for(done: int, total: int, suspect: bool = False) -> list:
    """`areas` the way the real store writes it: indexed by ABSOLUTE frame
    index, padded to `total` with None wherever nothing is written yet
    (contract C2). The `None` tail is exactly what made `mask show --strip`
    raise a TypeError on a partial matte (checkpoint gap 13), so the fake
    has to have it or the fix cannot be tested here.

    `suspect` writes a track that went wrong the way the grader's "face"
    matte did (checkpoint gap 18): it holds a small area, loses the subject
    entirely at frame 2, then latches onto something several times bigger.
    Frame 2 trips the zero-area rule and frame 4 trips the area-jump rule.
    """
    def area(i: int) -> float:
        if not suspect:
            return 0.02 + 0.01 * i
        if i == 2:
            return 0.0
        return 0.05 if i < 4 else 0.30 + 0.01 * i

    return [area(i) if i < done else None for i in range(total)]


def _matte_summary(mid: str, m: dict, full: bool = True) -> dict:
    """The C4 matte record, summary by default (checkpoint gap 6).

    A small, independent copy of what studio/server.py's own
    `_matte_summary` answers: span and coverage of it, the mean score, and
    the per frame quality flag block (gap 18). Independent on purpose, the
    same way this whole file is: it pins the WIRE shape the CLI parses, not
    the server's implementation of it.
    """
    areas = list(m.get("areas") or [])
    written = [i for i, v in enumerate(areas) if v is not None]
    fps = float(m.get("fps") or 24.0)
    total = int(m.get("frames") or 0)
    first = written[0] if written else 0
    end = (written[-1] + 1) if written else 0
    span_len = max(0, end - first)
    thresholds = {"area_jump": 0.5, "min_iou": 0.3}
    flagged = []
    prev = None
    for i in written:
        reasons = []
        v = float(areas[i])
        if v <= 0.0:
            reasons.append("zero_area")
        if prev is not None and prev > 0 and abs(v - prev) / prev > thresholds["area_jump"]:
            reasons.append("area_jump")
        if reasons:
            flagged.append({"index": i, "time": round(i / fps, 4),
                            "reasons": reasons, "area": v, "prev_area": prev,
                            "jump": None, "iou": None})
        prev = v
    out = dict(m, matte_id=mid)
    out["span"] = {"start_frame": first, "end_frame": end,
                   "written": len(written), "declared_frames": total,
                   "contiguous": span_len == len(written),
                   "start_s": round(first / fps, 4),
                   "end_s": round(end / fps, 4) if written else 0.0,
                   "frozen_outside_span": True}
    out["frozen_outside_span"] = True
    out["coverage"] = round(len(written) / span_len, 4) if span_len else 0.0
    scores = [v for v in (m.get("scores") or []) if v is not None]
    out["mean_score"] = round(sum(scores) / len(scores), 6) if scores else None
    out["total_frames"] = total
    out["written_count"] = len(written)
    out["is_partial"] = len(written) < total or m.get("state") != "done"
    out["quality"] = {
        "thresholds": thresholds, "checked": len(written), "iou_source": "none",
        "suspect_count": len(flagged), "suspect_frames": flagged[:8],
        "truncated": len(flagged) > 8,
        "first_suspect_index": flagged[0]["index"] if flagged else None,
        "first_suspect_time": flagged[0]["time"] if flagged else None,
        "reasons": {"zero_area": sum(1 for f in flagged
                                     if "zero_area" in f["reasons"]),
                    "area_jump": sum(1 for f in flagged
                                     if "area_jump" in f["reasons"]),
                    "low_iou": 0},
    }
    if not full:
        for key in ("areas", "scores", "ious"):
            out.pop(key, None)
    return out


def _advance_job(job: dict) -> None:
    if job["state"] == "queued":
        job["state"] = "running"
        return
    if job["state"] == "running":
        step = max(1, job["total_frames"] // 3)
        job["done_frames"] = min(job["total_frames"], job["done_frames"] + step)
        for mid in job["matte_ids"]:
            m = STATE.mattes[mid]
            m["done_frames"] = job["done_frames"]
            m["areas"] = _areas_for(job["done_frames"], int(m["frames"]),
                                    suspect=job["clip"] == "SUSPECT_CLIP")
            m["state"] = "running"
        if job["done_frames"] >= job["total_frames"]:
            job["state"] = "done"
            for mid in job["matte_ids"]:
                STATE.mattes[mid]["state"] = "done"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):                      # noqa: A003
        pass  # quiet: the test's own output is what matters

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, body: bytes, ctype: str, status=200, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw) if raw else {}

    def _query(self) -> dict:
        q = urllib.parse.urlparse(self.path).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}

    # -- routing ----------------------------------------------------------

    def do_GET(self):                                        # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        q = self._query()
        with STATE_LOCK:
            if path == "/api/health":
                # footage_dir / data_dir / matte_root are what let the CLI
                # resolve a bare clip name and a matte id through the server
                # instead of its own defaults (checkpoint gaps 4 and 5). A
                # test sets STATE.paths to point them at its own temp dirs.
                self._json({"ok": True, "backend": "stub", **STATE.paths})
                return
            if path == "/api/preset":
                # Checkpoint gap 17: `--preset NAME` reads the studio's own
                # saved presets before the built-in catalog when STUDIO_URL
                # is set. STATE.presets is what `preset save` would have
                # written; an unknown name is a 404, so `auto` falls through.
                name = q.get("name") or ""
                cfg = STATE.presets.get(name)
                if cfg is None:
                    self._json({"error": f"no preset {name}"}, 404)
                    return
                self._json({"name": name, "comment": "", "config": cfg,
                            "expanded": True})
                return
            if path == "/api/mask/jobs":
                jobs = [dict(j, job_id=jid) for jid, j in STATE.jobs.items()]
                self._json({"jobs": jobs})
                return
            if path.startswith("/api/mask/jobs/"):
                jid = path.rsplit("/", 1)[-1]
                job = STATE.jobs.get(jid)
                if job is None:
                    self._json({"error": f"no job {jid}"}, 404)
                    return
                _advance_job(job)
                self._json({
                    "state": job["state"], "done_frames": job["done_frames"],
                    "total_frames": job["total_frames"], "fps": job["fps"],
                    "elapsed_s": round(time.time() - job["started"], 2),
                    "matte_ids": job["matte_ids"], "error": job.get("error"),
                })
                return
            if path == "/api/matte":
                clip = q.get("clip")
                full = str(q.get("full", "")).lower() in ("1", "true", "yes")
                out = [_matte_summary(mid, m, full=full)
                      for mid, m in STATE.mattes.items()
                      if clip is None or m["clip"] == clip]
                self._json({"mattes": out, "full": full})
                return
            if path.startswith("/api/matte/") and path.endswith("/frame"):
                mid = path.split("/")[3]
                m = STATE.mattes.get(mid)
                if m is None:
                    self._json({"error": f"no matte {mid}"}, 404)
                    return
                t = float(q.get("time", 0.0))
                width = int(q.get("width", 240))
                png = _matte_frame_png(t, width, m["state"])
                idx = int(round(t * m["fps"]))
                self._bytes(png, "image/png", headers={
                    "X-Matte-State": m["state"], "X-Matte-Frame": idx})
                return
            if path.startswith("/api/matte/"):
                mid = path.rsplit("/", 1)[-1]
                m = STATE.mattes.get(mid)
                if m is None:
                    self._json({"error": f"no matte {mid}"}, 404)
                    return
                self._json(_matte_summary(mid, m, full=True))
                return
            if path.startswith("/previews/"):
                # segment's overlay/mask preview images: server-relative,
                # not under /api/, on purpose (tests _fetch_bytes's join).
                png = _matte_frame_png(1.0, 96, "done")
                self._bytes(png, "image/png")
                return
        self._json({"error": f"no such route: GET {path}"}, 404)

    def do_POST(self):                                       # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        body = self._body()
        with STATE_LOCK:
            if path == "/api/mask/segment":
                pick_id = STATE.new_id("pick")
                texts = ((body.get("prompts") or {}).get("text") or [])
                if any("nothing" in str(t) for t in texts):
                    # Checkpoint gap 3: zero matches carries a sentence and a
                    # candidates count, not just an empty list.
                    asked = ", ".join(f'"{t}"' for t in texts)
                    self._json({
                        "pick_id": pick_id, "instances": [], "candidates": 0,
                        "warnings": ["the model found nothing for these prompts"],
                        "message": (f"no match for {asked} at "
                                    f"{float(body.get('time', 0)):g}s: 0 "
                                    f"candidates from the model. Try another "
                                    f"word for the same thing, a different "
                                    f"--time, or a --point/--box prompt on the "
                                    f"pixels themselves.")})
                    return
                instances = [
                    {"id": "1", "score": 0.94, "box": [0.1, 0.1, 0.6, 0.9],
                    "area": 0.32,
                    "overlay": f"/previews/{pick_id}/1/overlay.png",
                    "mask": f"/previews/{pick_id}/1/mask.png"},
                    {"id": "2", "score": 0.61, "box": [0.55, 0.2, 0.9, 0.8],
                    "area": 0.11,
                    "overlay": f"/previews/{pick_id}/2/overlay.png",
                    "mask": f"/previews/{pick_id}/2/mask.png"},
                ]
                STATE.picks[pick_id] = {"clip": body.get("clip"),
                                        "instances": instances}
                self._json({"pick_id": pick_id, "instances": instances,
                            "candidates": len(instances)})
                return
            if path == "/api/mask/track":
                clip = body.get("clip")
                fail = clip == "FAIL_CLIP"
                total = 9
                force = bool(body.get("force"))
                # The recipe cache, keyed the way the real server keys it
                # (clip, rotation, prompts/pick), so a repeat request can be
                # a hit, a resume, or a forced redo (checkpoint gap 12).
                key = json.dumps([clip, body.get("rotation"),
                                  body.get("prompts"), body.get("pick_id"),
                                  body.get("select")], sort_keys=True)
                known = [mid for mid in STATE.recipes.get(key, [])
                        if mid in STATE.mattes]
                if known and not force:
                    dead = [mid for mid in known
                           if STATE.mattes[mid]["state"] in
                           ("failed", "stale", "cancelled")]
                    short = [mid for mid in known
                            if STATE.mattes[mid]["done_frames"] <
                            int(STATE.mattes[mid]["frames"])]
                    if not dead and not short:
                        self._json({"job_id": None, "cached": True, "mattes": [
                            {"matte_id": mid, "recipe": STATE.mattes[mid]["recipe"],
                            "state": STATE.mattes[mid]["state"]}
                            for mid in known]})
                        return
                job_id = STATE.new_id("job")
                resumed = bool(known) and not force
                if known:
                    matte_ids = known
                    for mid in matte_ids:
                        STATE.mattes[mid]["state"] = "queued"
                        if force:
                            STATE.mattes[mid]["done_frames"] = 0
                            STATE.mattes[mid]["areas"] = _areas_for(0, total)
                else:
                    matte_ids = [STATE.new_id("m_")]
                    STATE.mattes[matte_ids[0]] = {
                        "clip": clip, "clip_key": clip, "rotation": "auto",
                        "fps": 24.0, "frames": total, "width": 240, "height": 135,
                        "recipe": body, "state": "failed" if fail else "queued",
                        "done_frames": 0, "areas": _areas_for(0, total),
                        "scores": [], "created": time.time(),
                        "model": "stub", "backend": "stub",
                    }
                STATE.recipes[key] = matte_ids
                first = STATE.mattes[matte_ids[0]]
                STATE.jobs[job_id] = {
                    "clip": clip, "state": "failed" if fail else "queued",
                    "done_frames": first["done_frames"], "total_frames": total,
                    "fps": 3.5, "matte_ids": matte_ids, "started": time.time(),
                    "error": "synthetic failure for FAIL_CLIP" if fail else None,
                }
                out = {"job_id": job_id, "cached": False, "mattes": [
                    {"matte_id": mid, "recipe": STATE.mattes[mid]["recipe"],
                    "state": STATE.mattes[mid]["state"]} for mid in matte_ids]}
                if known:
                    out["resumed"] = resumed
                    out["restarted"] = bool(force)
                    out["resumed_from"] = first["done_frames"]
                    out["message"] = ("force: previous frames cleared, "
                                      "tracking again" if force else
                                      f"resuming from frame {first['done_frames']}")
                self._json(out)
                return
            if path == "/api/frame":
                t = float(body.get("time", 0.0))
                width = int(body.get("width", 320))
                self._bytes(_picture_jpeg(t, width), "image/jpeg")
                return
        self._json({"error": f"no such route: POST {path}"}, 404)


def start(port: int = 0) -> tuple[ThreadingHTTPServer, threading.Thread, int]:
    """Start the fake server in a background thread; returns (server,
    thread, actual_port). Caller shuts it down: `server.shutdown()`."""
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, th, srv.server_address[1]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7699)
    a = ap.parse_args()
    srv, th, port = start(a.port)
    print(f"fake studio server on http://127.0.0.1:{port}")
    try:
        th.join()
    except KeyboardInterrupt:
        srv.shutdown()
