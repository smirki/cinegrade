#!/usr/bin/env python3
"""Measure the GPU final render against the ffmpeg one: parity, speed, depth.

Every number quoted in the GPU render entries in studio/static/limits.js comes
out of this script. It drives a RUNNING studio over its own HTTP API (so it
measures the shipped path, not a private copy of it), writes its outputs to
grade/out with a w3b- prefix, and prints a report.

  .venv/bin/python studio/tools/gpu_render_bench.py --base http://127.0.0.1:PORT --all

Thresholds are the parity harness's own (studio/static/parity.js): EXACT means
no channel differs by more than 1 of 255; CLOSE means the worst channel is
within 16, at most 0.5 percent of channels are off by more than 1 and at most
0.02 percent by more than 4.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
CONTENT = HERE.parent.parent
OUT = CONTENT / "grade" / "out"
sys.path.insert(0, str(CONTENT / "grade"))
import cinegrade as CG                                        # noqa: E402

TH = {"exactMax": 1, "closeMax": 16, "closePctOver1": 0.5, "closePctOver4": 0.02}


def verdict(mx: float, p1: float, p4: float) -> str:
    if mx <= TH["exactMax"]:
        return "EXACT"
    if (mx <= TH["closeMax"] and p1 <= TH["closePctOver1"]
            and p4 <= TH["closePctOver4"]):
        return "CLOSE"
    return "FAILED"


def api(base: str, path: str, body=None):
    if body is None:
        return json.load(urllib.request.urlopen(base + path, timeout=120))
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=120))


def render(base: str, engine: str, clip: str, cfg: dict, name: str,
           scale: int | None, start: float, duration: float,
           timeout: float = 1800) -> dict:
    """One render through the API, timed end to end. Returns the job dict."""
    payload = {"clip": clip, "config": cfg, "engine": engine, "start": start,
               "duration": duration, "no_audio": True, "name": name}
    if scale:
        payload["scale"] = scale
    t0 = time.time()
    job = api(base, "/api/render", payload)["job"]
    while time.time() - t0 < timeout:
        time.sleep(0.5)
        jobs = {j["id"]: j for j in api(base, "/api/jobs")["jobs"]}
        cur = jobs.get(job["id"])
        if cur is None:
            raise SystemExit(f"the {engine} job disappeared from /api/jobs")
        if cur["status"] != "running":
            cur["wall"] = time.time() - t0
            if cur["status"] != "done":
                raise SystemExit(f"{engine} render {name} {cur['status']}: "
                                 f"{cur['message']}")
            return cur
    raise SystemExit(f"{engine} render {name} did not finish in {timeout}s")


def decode(path: str, frames: int) -> np.ndarray:
    """The first `frames` frames of a file as (n, H, W, 3) uint16 rgb48le."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,pix_fmt", "-of", "json", path],
        capture_output=True, text=True).stdout
    st = json.loads(probe)["streams"][0]
    w, h = int(st["width"]), int(st["height"])
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-frames:v", str(frames),
         "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"], capture_output=True)
    want = frames * w * h * 3
    arr = np.frombuffer(proc.stdout, "<u2")[:want]
    n = arr.size // (w * h * 3)
    return arr[:n * w * h * 3].reshape(n, h, w, 3)


def decode_y10(path: str, frames: int) -> np.ndarray:
    """The luma plane as 10 bit codes, straight from the file, no RGB hop."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "json", path],
        capture_output=True, text=True).stdout
    st = json.loads(probe)["streams"][0]
    w, h = int(st["width"]), int(st["height"])
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-frames:v", str(frames),
         "-f", "rawvideo", "-pix_fmt", "yuv422p10le", "-"], capture_output=True)
    per = w * h * 2                      # Y plane plus two half width planes
    arr = np.frombuffer(proc.stdout, "<u2")
    n = arr.size // per
    out = np.empty((n, h, w), "<u2")
    for i in range(n):
        out[i] = arr[i * per:i * per + w * h].reshape(h, w)
    return out


def compare(a: np.ndarray, b: np.ndarray, label: str) -> dict:
    n = min(len(a), len(b))
    rows = []
    for i in range(n):
        d = np.abs(a[i].astype(np.int32) - b[i].astype(np.int32)) / 257.0
        rows.append({
            "frame": i,
            "max": float(d.max()),
            "mean": float(d.mean()),
            "pct1": float((d > 1).mean() * 100),
            "pct4": float((d > 4).mean() * 100),
        })
    mx = max(r["max"] for r in rows)
    p1 = max(r["pct1"] for r in rows)
    p4 = max(r["pct4"] for r in rows)
    print(f"\n{label}: {n} frames compared, in 8 bit codes")
    for r in rows:
        print(f"  frame {r['frame']}: max {r['max']:6.3f}  mean {r['mean']:7.4f}"
              f"  over1 {r['pct1']:7.4f}%  over4 {r['pct4']:7.4f}%")
    print(f"  worst frame: max {mx:.3f}, over1 {p1:.4f}%, over4 {p4:.4f}%"
          f"  ->  {verdict(mx, p1, p4)}")
    return {"label": label, "max": mx, "pct1": p1, "pct4": p4,
            "verdict": verdict(mx, p1, p4), "frames": rows}


def smooth_patch(y: np.ndarray, size: int = 96) -> tuple[int, int]:
    """Where the picture is smoothest but still has a gradient to band.

    Banding shows up as a lost code in a slow ramp, so the patch this looks
    for is the one with the widest range of values among the ones with the
    smallest neighbour to neighbour steps.
    """
    h, w = y.shape
    best, coords = -1.0, (0, 0)
    for top in range(0, h - size, max(size, (h - size) // 12 or size)):
        for left in range(0, w - size, max(size, (w - size) // 12 or size)):
            p = y[top:top + size, left:left + size].astype(np.int32)
            step = np.abs(np.diff(p, axis=1)).mean()
            span = float(p.max() - p.min())
            if step > 6 or span < 24:
                continue                 # too busy, or nothing to band
            if span > best:
                best, coords = span, (top, left)
    return coords


def bit_depth(path: str, label: str, size: int = 96) -> dict:
    y = decode_y10(path, 1)[0]
    top, left = smooth_patch(y, size)
    patch = y[top:top + size, left:left + size]
    distinct = int(np.unique(patch).size)
    span = int(patch.max() - patch.min())
    # What the same patch would hold if the frames had come back from the GPU
    # at 8 bit: quantise to 8 bit and expand again, then count.
    eight = np.unique(np.round(patch.astype(np.float64) / 4.0) * 4).size
    print(f"\n{label} bit depth, {size}x{size} smooth patch at ({top},{left}):")
    print(f"  distinct 10 bit luma codes: {distinct}  (range {span} codes)")
    print(f"  the same patch forced through 8 bit: {int(eight)} codes")
    return {"label": label, "distinct": distinct, "span": span,
            "eight_bit_equivalent": int(eight), "patch": [top, left, size]}


def preset_grain_off(name: str) -> dict:
    cfg = CG.load_preset(name)
    cfg.setdefault("grain", {})["enabled"] = False
    return cfg


def preset_window_secondary(name: str) -> dict:
    """A preset plus a real power window gating a real secondary."""
    cfg = preset_grain_off(name)
    cfg["secondary"] = dict(CG.DEFAULTS["secondary"], enabled=True,
                            hue_center=210.0, hue_width=70.0, hue_soft=20.0,
                            sat_low=0.05, sat_gain=1.25, hue_shift=-8.0,
                            lum_gain=1.05)
    cfg["window"] = dict(CG.DEFAULTS["window"], enabled=True, cx=0.45, cy=0.42,
                         w=0.55, h=0.5, rotation=12.0, softness=0.25)
    return cfg


def run_parity(args) -> list:
    results = []
    cases = [
        ("cinematic-grainoff", preset_grain_off("cinematic")),
        ("natural-window-sec", preset_window_secondary("natural")),
    ]
    for tag, cfg in cases:
        ff = render(args.base, "ffmpeg", args.clip, cfg, f"w3b-{tag}-ffmpeg",
                    args.width, args.start, args.duration)
        gp = render(args.base, "gpu", args.clip, cfg, f"w3b-{tag}-gpu",
                    args.width, args.start, args.duration)
        print(f"\n=== {tag} at {args.width} wide, {args.duration}s ===")
        print(f"  ffmpeg: {ff['wall']:.1f}s  {ff['message']}")
        print(f"  gpu:    {gp['wall']:.1f}s  {gp['message']}")
        a = decode(ff["output"], args.frames)
        b = decode(gp["output"], args.frames)
        results.append(compare(a, b, f"{tag}: gpu vs ffmpeg"))
        results[-1]["ffmpeg_wall"] = ff["wall"]
        results[-1]["gpu_wall"] = gp["wall"]
    return results


def run_grain(args) -> dict:
    cfg = CG.load_preset("cinematic")
    cfg["grain"] = dict(CG.DEFAULTS["grain"], enabled=True, strength=40,
                        size=3, opacity=0.5)
    ff = render(args.base, "ffmpeg", args.clip, cfg, "w3b-grain-ffmpeg",
                args.width, args.start, args.duration)
    gp = render(args.base, "gpu", args.clip, cfg, "w3b-grain-gpu",
                args.width, args.start, args.duration)
    a = decode(ff["output"], args.frames).astype(np.float64)
    b = decode(gp["output"], args.frames).astype(np.float64)
    n = min(len(a), len(b))
    print(f"\n=== grain on, {n} frames, statistics only ===")
    print(f"  ffmpeg: {ff['wall']:.1f}s   gpu: {gp['wall']:.1f}s")
    stats = []
    for i in range(n):
        d = (b[i] - a[i]) / 257.0
        stats.append({"frame": i, "mean": float(d.mean()),
                      "sd": float(d.std()), "absmax": float(np.abs(d).max()),
                      "absmean": float(np.abs(d).mean())})
        print(f"  frame {i}: mean {stats[-1]['mean']:+8.4f}  sd "
              f"{stats[-1]['sd']:7.4f}  abs mean {stats[-1]['absmean']:7.4f}"
              f"  abs max {stats[-1]['absmax']:7.3f}")
    return {"frames": stats}


def run_speed(args) -> list:
    cfg = preset_grain_off("cinematic")
    rows = []
    for width in args.speed_widths:
        for engine in ("ffmpeg", "gpu"):
            job = render(args.base, engine, args.clip, cfg,
                         f"w3b-speed-{width}-{engine}", width, args.start,
                         args.duration)
            frames = int(subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-count_packets", "-show_entries", "stream=nb_read_packets",
                 "-of", "csv=p=0", job["output"]],
                capture_output=True, text=True).stdout.strip().rstrip(",") or 0)
            fps = frames / job["wall"] if job["wall"] else 0.0
            rows.append({"width": width, "engine": engine, "frames": frames,
                         "wall": job["wall"], "fps": fps,
                         "message": job["message"]})
            print(f"  {engine:6s} {width or 'native':>6} wide: {frames} frames "
                  f"in {job['wall']:.1f}s = {fps:.2f} fps   {job['message']}")
    return rows


def run_depth(args) -> list:
    cfg = preset_grain_off("cinematic")
    out = []
    for engine in ("ffmpeg", "gpu"):
        job = render(args.base, engine, args.clip, cfg,
                     f"w3b-depth-{engine}", args.width, args.start, 0.5)
        out.append(bit_depth(job["output"], engine))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="http://127.0.0.1:PORT")
    ap.add_argument("--clip", default="A001_09011832_C003.MOV")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--start", type=float, default=2.0)
    ap.add_argument("--duration", type=float, default=3.0)
    ap.add_argument("--frames", type=int, default=5)
    ap.add_argument("--speed-widths", type=int, nargs="*", default=[1920, 0])
    ap.add_argument("--parity", action="store_true")
    ap.add_argument("--grain", action="store_true")
    ap.add_argument("--speed", action="store_true")
    ap.add_argument("--depth", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    if args.all:
        args.parity = args.grain = args.speed = args.depth = True

    report = {"clip": args.clip, "width": args.width,
              "duration": args.duration, "when": time.strftime("%Y-%m-%d %H:%M")}
    if args.parity:
        report["parity"] = run_parity(args)
    if args.grain:
        report["grain"] = run_grain(args)
    if args.speed:
        print("\n=== speed ===")
        report["speed"] = run_speed(args)
    if args.depth:
        report["depth"] = run_depth(args)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1))
        print("\nwrote " + args.json)


if __name__ == "__main__":
    main()
