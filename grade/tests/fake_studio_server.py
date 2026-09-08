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
            m["areas"] = [0.02 + 0.01 * i for i in range(job["done_frames"])]
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
                self._json({"ok": True, "backend": "stub"})
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
                out = [dict(m, matte_id=mid) for mid, m in STATE.mattes.items()
                      if clip is None or m["clip"] == clip]
                self._json({"mattes": out})
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
                self._json(dict(m, matte_id=mid))
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
                self._json({"pick_id": pick_id, "instances": instances})
                return
            if path == "/api/mask/track":
                clip = body.get("clip")
                job_id = STATE.new_id("job")
                fail = clip == "FAIL_CLIP"
                total = 9
                matte_id = STATE.new_id("m_")
                STATE.mattes[matte_id] = {
                    "clip": clip, "clip_key": clip, "rotation": "auto",
                    "fps": 24.0, "frames": total, "width": 240, "height": 135,
                    "recipe": body, "state": "failed" if fail else "queued",
                    "done_frames": 0, "areas": [], "scores": [],
                    "created": time.time(), "model": "stub", "backend": "stub",
                }
                STATE.jobs[job_id] = {
                    "clip": clip, "state": "failed" if fail else "queued",
                    "done_frames": 0, "total_frames": total, "fps": 3.5,
                    "matte_ids": [matte_id], "started": time.time(),
                    "error": "synthetic failure for FAIL_CLIP" if fail else None,
                }
                self._json({"job_id": job_id, "mattes": [
                    {"matte_id": matte_id, "recipe": body,
                    "state": STATE.mattes[matte_id]["state"]}]})
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
