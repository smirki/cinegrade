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

Round 1 finding 16: a stand-in that answers a DIFFERENT shape from the real
server is worse than no stand-in, because every CLI test written against it
then pins the wrong behaviour. This file used to emit `resumed: True,
restarted: False, resumed_from: <done_frames>` for a matte that had FAILED,
which is exactly the bug checkpoint gap 12 fixed in studio/server.py, so a
CLI test of the corrected server would have failed here. The cache decision
below is now the real one's, in the real one's order (stale, covered, dead,
wider, gap), and the frame route falls back to the nearest written frame with
the same `X-Matte-Warning` sentence grade/mattes.py writes.

What is still deliberately NOT here, each one a thing the CLI does not read
and this file therefore does not pretend to have:

  auth and read guards   the real routes call `_guard_read` / `_require_admin`,
                         which are no-ops with logins off (the documented
                         default, and the only mode the CLI is used in).
                         `studio/tests/py/*` covers the guarded mode against
                         the real server.
  DELETE /api/matte/<id> no CLI verb deletes a matte; the route's own guards
                         and the id validation are pinned in
                         studio/tests/py/test_mask_routes.py.
  cancel, stats, render  `mask cancel` is not a CLI verb, and `stats` and
                         `render` run in-process against real ffmpeg in this
                         same test file rather than through a server.
  clips and footage      no ffprobe, no rotation resolution: /api/health hands
                         out whatever paths a test puts in STATE.paths.
  advance on poll        a real track runs on its own thread; this one moves
                         one step per `GET /api/mask/jobs/<id>` ON PURPOSE, so
                         a test that wants a half written matte polls a fixed
                         number of times instead of racing a timer.
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

# The whole fake clip: 9 frames at 24 fps. Small on purpose (a CLI test polls
# a job to completion in three steps), and fixed, because several tests read
# "3 of 9" and "9 of 9" out of the printed progress.
CLIP_FRAMES = 9
MATTE_FPS = 24.0


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


def _ious_for(done: int, total: int, suspect: bool = False) -> list:
    """`ious` the way sam/store.py writes it (checkpoint gap 18, contract C2):
    overlap with the PREVIOUS WRITTEN frame, indexed by absolute frame index,
    None on the first written frame because it has nothing to compare against.

    Round 1 finding 15: this file used to hardcode `low_iou: 0` in the quality
    block and write no `ious` at all, so the one rule of the three that needs
    a per frame array to fire could not fire in any test in the arc. On a
    `suspect` track the object is lost at frame 2 and something several times
    bigger is picked up at frame 4, so frame 4's overlap with what came before
    it is near zero: that is the number the drift rule is for.
    """
    def iou(i: int) -> float | None:
        if i == 0:
            return None
        if not suspect:
            return 0.92
        return 0.05 if i == 4 else 0.88

    return [iou(i) if i < done else None for i in range(total)]


def _window(body: dict) -> tuple[int, int]:
    """`[start_frame, end_frame)` a track request asks for, in frames.

    `end_frame` is an exclusive upper bound (C3) and both ends are clamped to
    the fake clip's own length, which is what studio/server.py's
    `_request_frame_window` does against the real clip's frame count. Round 1
    finding 5 is precisely a clamp against the wrong thing (the MATTE's
    length), so the stand-in has to clamp against the same thing the server
    does or a CLI test of the widen cannot mean anything.
    """
    def at(value, default: int) -> int:
        if value is None:
            return default
        return max(0, min(CLIP_FRAMES, int(round(float(value) * MATTE_FPS))))

    start = at(body.get("start"), 0)
    end = max(start, at(body.get("end"), CLIP_FRAMES))
    return start, end


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
    ious = list(m.get("ious") or [])
    flagged = []
    prev = None
    for i in written:
        reasons = []
        v = float(areas[i])
        iou = ious[i] if i < len(ious) else None
        if v <= 0.0:
            reasons.append("zero_area")
        if prev is not None and prev > 0 and abs(v - prev) / prev > thresholds["area_jump"]:
            reasons.append("area_jump")
        # The third rule (round 1 finding 15): overlap with the previous
        # written frame, from the array the store writes. Read here rather
        # than hardcoded to 0, so a track that jumps to a different object
        # is flagged for the reason it is actually wrong.
        if iou is not None and float(iou) < thresholds["min_iou"]:
            reasons.append("low_iou")
        if reasons:
            flagged.append({"index": i, "time": round(i / fps, 4),
                            "reasons": reasons, "area": v, "prev_area": prev,
                            "jump": None, "iou": iou})
        prev = v
    out = dict(m, matte_id=mid)
    out["span"] = {"start_frame": first, "end_frame": end,
                   "written": len(written), "declared_frames": total,
                   # The real formula (grade/mattes.py span()): an empty
                   # matte is NOT contiguous, where `span_len == len(written)`
                   # called 0 == 0 contiguous.
                   "contiguous": bool(written) and span_len == len(written),
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
        "thresholds": thresholds, "checked": len(written),
        "iou_source": "index" if any(v is not None for v in ious) else "none",
        "suspect_count": len(flagged), "suspect_frames": flagged[:8],
        "truncated": len(flagged) > 8,
        "first_suspect_index": flagged[0]["index"] if flagged else None,
        "first_suspect_time": flagged[0]["time"] if flagged else None,
        "reasons": {"zero_area": sum(1 for f in flagged
                                     if "zero_area" in f["reasons"]),
                    "area_jump": sum(1 for f in flagged
                                     if "area_jump" in f["reasons"]),
                    "low_iou": sum(1 for f in flagged
                                   if "low_iou" in f["reasons"])},
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
            suspect = job["clip"] == "SUSPECT_CLIP"
            m["done_frames"] = job["done_frames"]
            m["areas"] = _areas_for(job["done_frames"], int(m["frames"]),
                                    suspect=suspect)
            m["ious"] = _ious_for(job["done_frames"], int(m["frames"]),
                                  suspect=suspect)
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
                # `id`, not `job_id`: that is what studio/server.py's
                # `_mask_job_view` emits, and `_cmd_mask_jobs` reads
                # `j.get("job_id", j.get("id", "?"))`. While this file sent
                # `job_id` the real server's branch of that fallback was
                # exercised by nothing (round 1 finding 16).
                jobs = [dict(j, id=jid, label=f"mask track {j.get('clip')}")
                       for jid, j in STATE.jobs.items()]
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
                    "id": jid, "label": f"mask track {job.get('clip')}",
                    "state": job["state"], "done_frames": job["done_frames"],
                    "total_frames": job["total_frames"], "fps": job["fps"],
                    "elapsed_s": round(time.time() - job["started"], 2),
                    "matte_ids": job["matte_ids"], "error": job.get("error"),
                    "clip": job.get("clip"),
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
                fps = float(m.get("fps") or MATTE_FPS)
                # The nearest written frame, and the warning that says so
                # (round 1 findings 16 and 39). This used to answer
                # `round(t * fps)` for any time at all, with no fallback and
                # no `X-Matte-Warning`, so "frozen outside the span" was a
                # claim the CLI printed and nothing in this file could check.
                # The sentence is grade/mattes.py `served_frame`'s, word for
                # word, because a caller reads it.
                written = [i for i, v in enumerate(m.get("areas") or [])
                          if v is not None]
                if not written:
                    self._json({"error": f"matte {mid} has no frames on disk"},
                               404)
                    return
                want = max(0, min(int(m.get("frames") or 1) - 1,
                                  int(round(t * fps))))
                served = want if want in written else min(
                    written, key=lambda i: (abs(i - want), i))
                headers = {"X-Matte-State": m["state"],
                          "X-Matte-Frame": served}
                if served != want:
                    headers["X-Matte-Warning"] = (
                        f"matte {mid}: frame {want} is not tracked yet, "
                        f"showing frame {served} ({m['state']}, "
                        f"{len(written)} of {int(m.get('frames') or 0)} frames)")
                png = _matte_frame_png(served / fps, width, m["state"])
                self._bytes(png, "image/png", headers=headers)
                return
            if path.startswith("/api/matte/"):
                mid = path.rsplit("/", 1)[-1]
                m = STATE.mattes.get(mid)
                if m is None:
                    self._json({"error": f"no matte {mid}"}, 404)
                    return
                self._json(_matte_summary(mid, m, full=True))
                return
            if path.startswith("/api/mask/pick/"):
                # The real URL shape and the real content types (round 1
                # finding 16): /api/mask/pick/<pick_id>/<instance_id>/<kind>,
                # no extension, and the OVERLAY is a JPEG while the mask is a
                # PNG (studio/server.py's mask/pick route). While this file
                # served `/previews/<pick>/<inst>/overlay.png` as image/png,
                # `mask segment -o DIR` looked correct here and writes JPEG
                # bytes into a `.png` name against the real server. That
                # mislabel is in `_cmd_mask_segment`, which this lane does not
                # own; it is recorded in ROUND1-FIXES-tests.md for the CLI
                # lane, and the test below counts downloaded files with a `*`
                # glob so it neither hides the bug nor asserts it is correct.
                parts = path[len("/api/mask/pick/"):].split("/")
                if len(parts) != 3 or parts[2] not in ("overlay", "mask"):
                    self._json({"error": f"no such route: GET {path}"}, 404)
                    return
                pick_id, inst_id, kind = parts
                pick = STATE.picks.get(pick_id)
                if not pick or not any(str(i["id"]) == inst_id
                                       for i in pick["instances"]):
                    self._json({"error": f"no {kind} for pick {pick_id} "
                                f"instance {inst_id}"}, 404)
                    return
                if kind == "overlay":
                    self._bytes(_picture_jpeg(1.0, 96), "image/jpeg")
                else:
                    self._bytes(_matte_frame_png(1.0, 96, "done"), "image/png")
                return
        self._json({"error": f"no such route: GET {path}"}, 404)

    def do_POST(self):                                       # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        body = self._body()
        with STATE_LOCK:
            if path == "/api/mask/segment":
                prompts = body.get("prompts") or {}
                if not (prompts.get("text") or prompts.get("points")
                        or prompts.get("boxes") or prompts.get("exemplars")):
                    # The real route's own refusal (round 1 finding 16: this
                    # file validated nothing, so the 400 the real server
                    # answers with was reachable in no CLI test).
                    self._json({"error": "mask segment needs at least one "
                                "prompt (text, points, boxes or exemplars)"},
                               400)
                    return
                pick_id = STATE.new_id("pick")
                texts = (prompts.get("text") or [])
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
                    "overlay": f"/api/mask/pick/{pick_id}/1/overlay",
                    "mask": f"/api/mask/pick/{pick_id}/1/mask"},
                    {"id": "2", "score": 0.61, "box": [0.55, 0.2, 0.9, 0.8],
                    "area": 0.11,
                    "overlay": f"/api/mask/pick/{pick_id}/2/overlay",
                    "mask": f"/api/mask/pick/{pick_id}/2/mask"},
                ]
                STATE.picks[pick_id] = {"clip": body.get("clip"),
                                        "instances": instances}
                self._json({"pick_id": pick_id, "instances": instances,
                            "candidates": len(instances)})
                return
            if path == "/api/mask/track":
                clip = body.get("clip")
                fail = clip == "FAIL_CLIP"
                force = bool(body.get("force"))
                prompts = body.get("prompts")
                pick_id = body.get("pick_id")
                # The real route's three refusals, in its own words, so a CLI
                # or client call that should be a 400 is one here too (round 1
                # finding 16: this route accepted anything at all).
                has_prompt = isinstance(prompts, dict) and (
                    prompts.get("text") or prompts.get("points")
                    or prompts.get("boxes") or prompts.get("exemplars"))
                if not has_prompt and not pick_id:
                    self._json({"error": "mask track needs at least one prompt "
                                "(text, points, boxes or exemplars) or a "
                                "pick_id"}, 400)
                    return
                if has_prompt and pick_id:
                    self._json({"error": "mask track takes prompts or a "
                                "pick_id, not both"}, 400)
                    return
                if pick_id and pick_id not in STATE.picks:
                    self._json({"error": f"no such pick: {pick_id}"}, 400)
                    return
                # The window this call asks for, clamped to the clip, exactly
                # as studio/server.py resolves it before the cache decision.
                start_frame, end_frame = _window(body)
                # The recipe cache, keyed the way the real server keys it
                # (clip, rotation, prompts/pick), so a repeat request can be
                # a hit, a resume, a widen or a forced redo (checkpoint gap 12
                # and round 1 finding 5). Deliberately NOT keyed on the frame
                # range: a narrower or wider ask for the same subject is the
                # same recipe, which is the whole point.
                key = json.dumps([clip, body.get("rotation"), prompts, pick_id,
                                  body.get("select")], sort_keys=True)
                known = [mid for mid in STATE.recipes.get(key, [])
                        if mid in STATE.mattes]
                resume_kind = ""            # resume | widen | restart | force
                resume_from = start_frame
                message = ""
                if known and not force:
                    # studio/server.py's order, and the order matters: `stale`
                    # is read BEFORE coverage (round 1 finding 21, a stale
                    # matte is wrong however many frames it holds), the dead
                    # states are read before the gap (a failed matte has a gap
                    # by definition, so a gap test first would resume every
                    # dead matte and the restart branch would be unreachable),
                    # and a window WIDER than the matte is a widen rather than
                    # a cache hit (finding 5).
                    def _m(mid):
                        return STATE.mattes[mid]
                    stale = [mid for mid in known if _m(mid)["state"] == "stale"]
                    dead = [mid for mid in known
                           if _m(mid)["state"] in ("failed", "cancelled")]
                    # `done_frames` counts frames 0..done-1 on disk, which is
                    # the only coverage model this file has: a real matte can
                    # have a hole anywhere, and the frame level version of that
                    # is pinned against the real server in
                    # studio/tests/py/test_mask_routes.py.
                    have = min(int(_m(mid)["done_frames"]) for mid in known)
                    covered = have >= end_frame
                    wider = [mid for mid in known
                            if end_frame > int(_m(mid)["frames"] or 0)]
                    if stale:
                        resume_kind, resume_from = "restart", start_frame
                        message = (f"restarting from frame {resume_from}: "
                                   f"{len(stale)} matte(s) are stale (tracked "
                                   f"for a different clip, rotation or working "
                                   f"width than this request)")
                    elif covered:
                        self._json({
                            "job_id": None, "cached": True,
                            "start_frame": start_frame, "end_frame": end_frame,
                            "mattes": [
                                {"matte_id": mid, "recipe": _m(mid)["recipe"],
                                "state": _m(mid)["state"]} for mid in known]})
                        return
                    elif dead:
                        resume_kind, resume_from = "restart", start_frame
                        message = (f"restarting from frame {resume_from}: "
                                   + ", ".join(sorted({_m(mid)["state"]
                                                       for mid in dead})))
                    elif wider:
                        resume_kind, resume_from = "widen", have
                        covers = "; ".join(
                            f"{mid} covers frames 0 to {int(_m(mid)['frames'] or 0)}"
                            for mid in wider)
                        message = (f"widening from frame {resume_from}: this "
                                   f"request wants frames {start_frame} to "
                                   f"{end_frame} and {covers}")
                    else:
                        resume_kind, resume_from = "resume", have
                        message = (f"resuming from frame {resume_from}: "
                                   + ", ".join(sorted({_m(mid)["state"]
                                                       for mid in known})))
                elif known and force:
                    resume_kind, resume_from = "force", start_frame
                    message = "force: previous frames cleared, tracking again"
                job_id = STATE.new_id("job")
                if known:
                    matte_ids = known
                    for mid in matte_ids:
                        m = STATE.mattes[mid]
                        m["state"] = "queued"
                        m["error"] = None
                        # The matte now declares the wider of the two windows,
                        # the way sam/store.py's resume carries the range
                        # forward instead of blanking it.
                        m["frames"] = max(int(m.get("frames") or 0), end_frame)
                        m["end_frame"] = m["frames"]
                        m["start_frame"] = min(int(m.get("start_frame") or 0),
                                               start_frame)
                        if resume_kind in ("force", "restart"):
                            # A dead or stale matte is re-tracked, not resumed:
                            # its frames go, and `resumed_from` is the start of
                            # the window, not whatever the old attempt reached.
                            # This branch used to report `resumed: True` with
                            # `resumed_from: <done_frames>` for a FAILED matte,
                            # which is the pre-fix server behaviour verbatim
                            # (round 1 finding 16).
                            m["done_frames"] = 0
                            m["areas"] = _areas_for(0, int(m["frames"]))
                            m["ious"] = _ious_for(0, int(m["frames"]))
                else:
                    matte_ids = [STATE.new_id("m_")]
                    STATE.mattes[matte_ids[0]] = {
                        "clip": clip, "clip_key": clip,
                        # The rotation this track was asked for, not a
                        # hardcoded "auto": design rule 5 keys a matte by it,
                        # so a matte that cannot say which rotation it was
                        # made at cannot be checked against the request that
                        # asked for it (round 1 finding 40).
                        "rotation": str(body.get("rotation") or "auto"),
                        "fps": MATTE_FPS, "frames": end_frame,
                        "start_frame": start_frame, "end_frame": end_frame,
                        "width": 240, "height": 135,
                        "recipe": body, "state": "failed" if fail else "queued",
                        "done_frames": 0, "areas": _areas_for(0, end_frame),
                        "ious": _ious_for(0, end_frame),
                        "scores": [], "created": time.time(),
                        "model": "stub", "backend": "stub",
                        "error": ("synthetic failure for FAIL_CLIP"
                                  if fail else None),
                    }
                STATE.recipes[key] = matte_ids
                first = STATE.mattes[matte_ids[0]]
                STATE.jobs[job_id] = {
                    "clip": clip, "state": "failed" if fail else "queued",
                    "done_frames": first["done_frames"],
                    "total_frames": int(first["frames"]),
                    "fps": 3.5, "matte_ids": matte_ids, "started": time.time(),
                    "error": "synthetic failure for FAIL_CLIP" if fail else None,
                }
                out = {"job_id": job_id, "cached": False,
                       "start_frame": resume_from if resume_kind else start_frame,
                       "end_frame": end_frame,
                       "mattes": [
                           {"matte_id": mid, "recipe": STATE.mattes[mid]["recipe"],
                           "state": STATE.mattes[mid]["state"]}
                           for mid in matte_ids]}
                if resume_kind:
                    # Three separate flags, exactly one true, the same as the
                    # real response: a caller has to be able to tell a widen
                    # (every tracked frame kept) from a redo (all of them gone).
                    out["resumed"] = resume_kind == "resume"
                    out["widened"] = resume_kind == "widen"
                    out["restarted"] = resume_kind in ("restart", "force")
                    out["resumed_from"] = resume_from
                    out["message"] = message
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
