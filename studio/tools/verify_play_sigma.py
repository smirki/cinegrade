#!/usr/bin/env python3
"""Numeric proof that the playback path scales FX pixel sigmas for preview
width exactly the way the still-frame path already does (scale_for_preview
in server.py). Compares three renders of the same clip/time/width/preset:

  A. still frame, via the real HTTP /api/frame route (format=raw)
  B. the real playback path end to end, via HTTP /api/play/prepare +
     /api/play/stream, decoding the first frame of the returned fragmented
     mp4
  C. a deliberately WRONG playback render that skips scale_for_preview (by
     calling the same in-process functions with the unscaled config), to
     prove this check has teeth: if it did not catch (C) being far off, it
     would not be trustworthy evidence that (B) matching is meaningful either.

Everything here talks to the already-running studio server over real HTTP
except (C), which has no HTTP route on purpose (the server never renders an
unscaled preview) and so is built by importing server.py and cinegrade.py
directly and calling the same functions with the scaling step skipped.

Run with the studio server already up on 127.0.0.1:7431.
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

import numpy as np

STUDIO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STUDIO))
sys.path.insert(0, str(STUDIO.parent / "grade"))

import server as ST            # noqa: E402  (path insert must come first)

BASE = "http://127.0.0.1:7431"
CLIP = "A001_09011832_C003.MOV"
TIME = 1.0
WIDTH = 960
PRESET = "cinematic"


def http_get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return r.read(), dict(r.headers)


def http_post(path, payload):
    req = urllib.request.Request(
        BASE + path, method="POST", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read(), dict(r.headers)


def decode_first_frame_rgb24(mp4_bytes: bytes, width: int, height: int) -> np.ndarray:
    """Decode frame 1 of a fragmented mp4 byte string via a local ffmpeg pass,
    independent of the server: a second, external check on what it produced,
    not a reuse of any code path already under test.
    """
    args = ["ffmpeg", "-v", "error", "-i", "pipe:0", "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    proc = subprocess.run(args, input=mp4_bytes, capture_output=True)
    want = width * height * 3
    if proc.returncode != 0 or len(proc.stdout) < want:
        raise RuntimeError("ffmpeg could not decode the segment: "
                            + proc.stderr.decode("utf-8", "replace")[-800:])
    return np.frombuffer(proc.stdout[:want], np.uint8).reshape(height, width, 3)


def diff_stats(a: np.ndarray, b: np.ndarray) -> dict:
    d = np.abs(a.astype(np.int16) - b.astype(np.int16))
    per_pixel_max = d.max(axis=-1)
    flat = d.reshape(-1)
    return {
        "mean": float(flat.mean()),
        "max": int(flat.max()),
        "p99": float(np.percentile(flat, 99)),
        "pct_pixels_gt2code": float((per_pixel_max > 2).mean() * 100.0),
    }


def main() -> int:
    preset_resp, _ = http_get(f"/api/preset?name={PRESET}")
    cfg = json.loads(preset_resp)["config"]
    fx = cfg.get("fx", {})
    print(f"preset {PRESET}: halation sigma={fx.get('halation', {}).get('sigma')} "
          f"bloom sigma={fx.get('bloom', {}).get('sigma')} "
          f"radial_blur sigma={fx.get('radial_blur', {}).get('sigma')} "
          f"grain size={cfg.get('grain', {}).get('size')}")

    # ---- A: still frame, real HTTP path --------------------------------
    body, headers = http_post("/api/frame", {
        "clip": CLIP, "time": TIME, "width": WIDTH, "autorotate": True,
        "config": cfg, "format": "raw",
    })
    sw, sh = headers["X-Frame-Size"].split("x")
    sw, sh = int(sw), int(sh)
    still = np.frombuffer(body, np.uint8).reshape(sh, sw, 3)
    print(f"A. still frame via /api/frame: {sw}x{sh}, {len(body)} bytes")

    # ---- B: real playback path, end to end via HTTP --------------------
    prep, _ = http_post("/api/play/prepare", {
        "clip": CLIP, "time": TIME, "duration": 1.0, "width": WIDTH,
        "autorotate": True, "config": cfg,
    })
    prep = json.loads(prep)
    pw, ph = prep["width"], prep["height"]
    if (pw, ph) != (sw, sh):
        print(f"!!! playback and still-frame preview dims disagree: "
              f"{pw}x{ph} vs {sw}x{sh}")
        return 1
    seg_bytes, _ = http_get(f"/api/play/stream?key={prep['key']}")
    play_right = decode_first_frame_rgb24(seg_bytes, pw, ph)
    print(f"B. playback (correct, scaled) via /api/play/*: {pw}x{ph}, "
          f"{len(seg_bytes)} bytes segment")

    # ---- C: deliberately WRONG playback render, sigmas NOT scaled ------
    # No HTTP route does this on purpose. This calls the same in-process
    # building blocks the real route uses, with the one line that matters
    # skipped: pcfg is put back to the full-resolution cfg instead of
    # scale_for_preview's output, exactly the bug this whole check exists
    # to catch.
    params = dict(ST._play_params({
        "clip": CLIP, "time": TIME, "duration": 1.0, "width": WIDTH,
        "autorotate": True, "config": cfg,
    }))
    params["pcfg"] = params["cfg"]  # the bug under test: skip scale_for_preview
    args = ST._play_ffmpeg_args(params)
    proc = subprocess.run(args, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError("wrong-path render failed: "
                            + proc.stderr.decode("utf-8", "replace")[-800:])
    play_wrong = decode_first_frame_rgb24(proc.stdout, pw, ph)
    print(f"C. playback (WRONG, unscaled sigmas) built in-process: "
          f"{len(proc.stdout)} bytes segment")

    print()
    print("=== B (correct, scaled) vs A (still frame) ===")
    right_stats = diff_stats(play_right, still)
    print(json.dumps(right_stats, indent=1))

    print("=== C (wrong, unscaled) vs A (still frame) ===")
    wrong_stats = diff_stats(play_wrong, still)
    print(json.dumps(wrong_stats, indent=1))

    print()
    print(f"CORRECT_PATH_MEAN_DIFF: {right_stats['mean']:.3f}")
    print(f"WRONG_PATH_MEAN_DIFF: {wrong_stats['mean']:.3f}")
    print(f"WRONG_PATH_IS_WORSE (proves the check has teeth): "
          f"{wrong_stats['mean'] > right_stats['mean']}")
    # The correct path goes through a different encoder (h264_videotoolbox)
    # and a different decode chain (real ffmpeg -ss seek vs the cached
    # source-frame pipe) than the still frame does, so some small honest
    # difference is expected; the bar here is "close", not "identical".
    print(f"CORRECT_PATH_CLOSE (mean < 8 codes): {right_stats['mean'] < 8}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
