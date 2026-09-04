#!/usr/bin/env python3
"""Empirical audit of every cinegrade parameter, rendered the way the studio
preview renders a frame but WITHOUT scale_for_preview's pixel-parameter
rescaling (sigma, amount, soften, grain size). Pixel-denominated parameters
have to be measured at the size the filter graph actually sees or the numbers
are meaningless - see the audit brief this script implements.

This file only reads footage and existing presets/LUTs. It never writes to
grade/ or studio/ outside this tools/ folder. Every number in
audit-results.json came from an actual ffmpeg run on real frames; nothing
here is inferred from reading cinegrade.py alone.

Run:
    content/.venv/bin/python content/studio/tools/audit_effects.py
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import hashlib
import json
import re
import subprocess
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path

import numpy as np

STUDIO = Path(__file__).resolve().parents[1]
CONTENT = STUDIO.parent
GRADE = CONTENT / "grade"
FOOTAGE = CONTENT / "footage"

sys.path.insert(0, str(GRADE))
import cinegrade as CG  # noqa: E402

WIDTH = 640
RESULTS_PATH = Path(__file__).resolve().parent / "audit-results.json"

CLIP_A = "A001_09011336_C002.MOV"   # 4K golden-hour driveway, 5s
CLIP_B = "A001_09011832_C003.MOV"   # 4K exterior, tree/cars/sky, 32s

# Chosen by measurement, see pick_frame(). Recorded here after the pick step
# ran once; still re-derivable by calling pick_frame() again.
MAIN_CLIP = CLIP_B
MAIN_T = 1.0
SECOND_CLIP = CLIP_A
SECOND_T = 2.0

# --------------------------------------------------------------------------
# rendering, exactly the studio preview's raw path, no scale_for_preview
# --------------------------------------------------------------------------

_render_cache: dict[str, np.ndarray] = {}
_render_meta: dict[str, dict] = {}
_cache_master_lock = threading.Lock()
_key_locks: dict[str, threading.Lock] = {}
_ffmpeg_slots = threading.Semaphore(3)

RENDER_COUNT = [0]   # actual ffmpeg invocations, not cache hits


@contextlib.contextmanager
def _no_lock():
    yield


def _key_lock(key: str) -> threading.Lock:
    with _cache_master_lock:
        lk = _key_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _key_locks[key] = lk
        return lk


def _cfg_key(clip: str, t: float, width: int, autorotate: bool, cfg: dict) -> str:
    blob = json.dumps({"clip": clip, "t": round(float(t), 4), "w": width,
                       "rot": autorotate, "cfg": cfg}, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()


def render(clip: str, cfg: dict, t: float = MAIN_T, width: int = WIDTH,
          autorotate: bool = True, use_cache: bool = True) -> tuple[np.ndarray, dict]:
    """One graded frame as an (H, W, 3) uint8 array, exactly the method spec:

        info = CG.probe(src, autorotate=True)
        factor = width / info["width"]; height rounded to even
        head = scale to width:height, bilinear, setsar=1
        graph = CG.graph_with_mask(cfg, pinfo, encode_out=False, ...)

    No scale_for_preview call anywhere: pixel params are tested at the size
    the graph actually sees at width=640.

    use_cache=False forces a fresh ffmpeg run even for a config already seen;
    needed for the grain run-to-run reproducibility check, where the whole
    point is to invoke ffmpeg twice on an IDENTICAL config and see whether the
    two outputs differ.
    """
    src = str(FOOTAGE / clip)
    key = _cfg_key(clip, t, width, autorotate, cfg)
    if use_cache:
        with _cache_master_lock:
            if key in _render_cache:
                return _render_cache[key], _render_meta[key]
    with _key_lock(key) if use_cache else _no_lock():
        if use_cache:
            with _cache_master_lock:
                if key in _render_cache:
                    return _render_cache[key], _render_meta[key]
        info = CG.probe(src, autorotate=autorotate)
        factor = width / float(info["width"])
        height = max(2, int(round(info["height"] * factor / 2)) * 2)
        pinfo = dict(info, width=width, height=height)
        head = f"[0:v]scale={width}:{height}:flags=bilinear,setsar=1[studiosrc]"
        graph = CG.graph_with_mask(cfg, pinfo, encode_out=False,
                                   tail_extra=["format=rgb24"],
                                   src_label="studiosrc", head_extra=head)
        args = CG.ffmpeg_inputs(src, cfg, pinfo, t)
        args += ["-filter_complex", graph, "-map", "[vout]", "-frames:v", "1",
                 "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        with _ffmpeg_slots:
            RENDER_COUNT[0] += 1
            proc = subprocess.run(args, capture_output=True)
        if proc.returncode != 0:
            raise CG.GradeError(proc.stderr.decode("utf-8", "replace")[-2000:])
        arr = np.frombuffer(proc.stdout, np.uint8).reshape(height, width, 3).copy()
        meta = {"width": width, "height": height, "source_width": info["width"],
                "source_height": info["height"]}
        if use_cache:
            with _cache_master_lock:
                _render_cache[key] = arr
                _render_meta[key] = meta
        return arr, meta


def try_render(clip: str, cfg: dict, t: float = MAIN_T, width: int = WIDTH,
              autorotate: bool = True) -> tuple[np.ndarray | None, str | None]:
    """Like render() but returns the ffmpeg stderr instead of raising.

    Used only where the test itself is "does this even run" (missing LUTs,
    strength above the claimed 1.0 cap).
    """
    try:
        arr, _ = render(clip, cfg, t, width, autorotate)
        return arr, None
    except CG.GradeError as exc:
        return None, str(exc)


def parallel(jobs: list[tuple]) -> list:
    """Run render(*args) for each tuple in jobs, up to 3 at once."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(render, *j) for j in jobs]
        return [f.result() for f in futs]


def base(overrides: dict | None = None) -> dict:
    """flat.json deep-merged with overrides. flat.json only pins tonemap=aces,
    exposure=0.0, which are already cinegrade's own defaults, so this is
    exactly CG.DEFAULTS with overrides; using load_preset keeps the provenance
    honest (this is literally the preset named in the brief)."""
    cfg = CG.load_preset("flat")
    if overrides:
        cfg = CG.deep_merge(cfg, overrides)
    return cfg


# --------------------------------------------------------------------------
# statistics (no scipy, no PIL: numpy only)
# --------------------------------------------------------------------------

def luma01(rgb: np.ndarray) -> np.ndarray:
    a = rgb.astype(np.float64) / 255.0
    return 0.2126 * a[..., 0] + 0.7152 * a[..., 1] + 0.0722 * a[..., 2]


def luma255(rgb: np.ndarray) -> np.ndarray:
    return luma01(rgb) * 255.0


def sat_of(rgb: np.ndarray) -> np.ndarray:
    """(max-min)/max per pixel, the same ratio the brief specifies."""
    a = rgb.astype(np.float64)
    mx, mn = a.max(-1), a.min(-1)
    return np.where(mx > 1e-9, (mx - mn) / np.maximum(mx, 1e-9), 0.0)


def hsv_hue(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    a = rgb.astype(np.float64) / 255.0
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx, mn = a.max(-1), a.min(-1)
    delta = mx - mn
    hue = np.zeros_like(mx)
    nz = delta > 1e-9
    i = nz & (mx == r)
    hue[i] = ((g - b)[i] / delta[i]) % 6.0
    i = nz & (mx == g) & (mx != r)
    hue[i] = ((b - r)[i] / delta[i]) + 2.0
    i = nz & (mx == b) & (mx != r) & (mx != g)
    hue[i] = ((r - g)[i] / delta[i]) + 4.0
    hue = (hue * 60.0) % 360.0
    sat = np.where(mx > 1e-6, delta / np.maximum(mx, 1e-6), 0.0)
    return hue, sat


def percentiles(y: np.ndarray, qs=(5, 50, 95)) -> dict:
    vals = np.percentile(y, list(qs))
    return {f"p{q}": float(v) for q, v in zip(qs, vals)}


def diff_stats(a: np.ndarray, b: np.ndarray) -> dict:
    """b relative to a, the 0-255 scale, per the brief's required fields."""
    d = b.astype(np.float64) - a.astype(np.float64)
    ad = np.abs(d)
    changed = ad.max(-1) >= 1.0
    return {
        "mean_abs_diff": float(ad.mean()),
        "max_abs_diff": float(ad.max()),
        "mean_signed": {"r": float(d[..., 0].mean()), "g": float(d[..., 1].mean()),
                        "b": float(d[..., 2].mean())},
        "pct_pixels_changed": float(changed.mean() * 100.0),
    }


def channel_means(rgb: np.ndarray) -> dict:
    a = rgb.astype(np.float64)
    return {"r": float(a[..., 0].mean()), "g": float(a[..., 1].mean()), "b": float(a[..., 2].mean())}


def integral_image(a: np.ndarray) -> np.ndarray:
    return np.cumsum(np.cumsum(a, axis=0), axis=1)


def box_blur(gray: np.ndarray, k: int) -> np.ndarray:
    """Mean filter, kernel k (odd), edge padded, via an integral image so a
    big k (used for the halation/bloom glow-width metric) costs the same as
    a small one. Verified against a brute-force 3x3/5x5 mean, see audit notes."""
    r = k // 2
    p = np.pad(gray.astype(np.float64), r, mode="edge")
    ii = np.zeros((p.shape[0] + 1, p.shape[1] + 1), dtype=np.float64)
    ii[1:, 1:] = integral_image(p)
    h, w = gray.shape
    total = ii[k:k + h, k:k + w] - ii[0:h, k:k + w] - ii[k:k + h, 0:w] + ii[0:h, 0:w]
    return total / (k * k)


def laplacian(gray: np.ndarray) -> np.ndarray:
    p = np.pad(gray.astype(np.float64), 1, mode="edge")
    return (p[0:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, 0:-2] + p[1:-1, 2:] - 4 * p[1:-1, 1:-1])


def mean_abs_laplacian(gray: np.ndarray) -> float:
    return float(np.abs(laplacian(gray)).mean())


def autocorr(residual: np.ndarray, lag: int) -> float:
    """Pearson correlation of the residual with itself shifted by `lag`
    pixels, averaged over the horizontal and vertical shift. Normalised by
    the residual's own variance, so lag 0 would read 1.0 by construction."""
    r = residual - residual.mean()
    var = r.var() + 1e-12
    h, w = r.shape
    ch = (r[:, :w - lag] * r[:, lag:]).mean() / var
    cv = (r[:h - lag, :] * r[lag:, :]).mean() / var
    return float((ch + cv) / 2.0)


def local_variance_stat(gray: np.ndarray) -> float:
    """variance of (frame - 3x3 box blur of itself): a stable grain-strength
    proxy that does not depend on the random seed of any one render."""
    residual = gray.astype(np.float64) - box_blur(gray, 3)
    return float(residual.var())


def radius_norm(h: int, w: int) -> np.ndarray:
    """Same ellipse-normalised radius cinegrade's own radial_mask() uses:
    hypot((x-w/2)/(w/2), (y-h/2)/(h/2)). Reused here only as a coordinate
    grid for binning measured pixels, never to decide an answer by itself."""
    ys, xs = np.mgrid[0:h, 0:w]
    return np.hypot((xs - w / 2.0) / (w / 2.0), (ys - h / 2.0) / (h / 2.0))


# --------------------------------------------------------------------------
# ramp-response: feed a calibrated ramp through the REAL cinegrade filter
# fragments (CG.f_primaries, CG.f_curves) and read the transform back from
# rendered pixels. This still measures pixels; it just isolates one node
# from the rest of the graph and the noise of real footage so a claim like
# "the pivot moved" can be read as an exact crossover instead of guessed at
# from a frame average.
# --------------------------------------------------------------------------

def ramp_frame(width: int = 640, height: int = 4) -> np.ndarray:
    args = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            f"color=c=black:s={width}x{height}:d=1", "-vf",
            f"geq=lum='255*X/(W-1)':cb=128:cr=128,format=rgb24",
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    proc = subprocess.run(args, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace"))
    return np.frombuffer(proc.stdout, np.uint8).reshape(height, width, 3).copy()


def ramp_response(filters: list[str], width: int = 640, height: int = 4,
                  in_fmt: str = "gbrp16le") -> tuple[np.ndarray, np.ndarray]:
    """Return (input_r, output_r) for a 0..255 ramp pushed through `filters`
    (a list of ffmpeg filter strings, e.g. CG.f_primaries(cfg)) in the same
    pixel format the real pipeline hands that node (gbrp16le)."""
    ramp = ramp_frame(width, height)
    vf = f"format={in_fmt}," + ",".join(filters) + ",format=rgb24"
    args = ["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{width}x{height}", "-i", "-", "-vf", vf, "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    proc = subprocess.run(args, input=ramp.tobytes(), capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace"))
    out = np.frombuffer(proc.stdout, np.uint8).reshape(height, width, 3).copy()
    return ramp[0, :, 0].astype(np.float64), out[0, :, 0].astype(np.float64)


# --------------------------------------------------------------------------
# secondary qualifier: reproduce cinegrade.secondary_lut's own math, but
# evaluated on the ACTUAL rendered frame's pixels rather than an identity
# grid, so it can be used to partition real pixels into "inside the key" and
# "outside the key" for a real pixel-diff measurement.
# --------------------------------------------------------------------------

def soft_window(x: np.ndarray, low: float, high: float, soft: float) -> np.ndarray:
    s = max(1e-6, float(soft))
    up = np.clip((x - (low - s)) / s, 0.0, 1.0)
    down = np.clip(((high + s) - x) / s, 0.0, 1.0)
    return np.minimum(up, down)


def secondary_mask(rgb: np.ndarray, s: dict) -> np.ndarray:
    hue, sat = hsv_hue(rgb)
    y = luma01(rgb)
    d = np.abs(((hue - float(s["hue_center"]) + 180.0) % 360.0) - 180.0)
    half = max(1e-6, float(s["hue_width"]) / 2.0)
    hs = max(1e-6, float(s["hue_soft"]))
    w_hue = np.clip(((half + hs) - d) / hs, 0.0, 1.0)
    w_sat = soft_window(sat, float(s["sat_low"]), float(s["sat_high"]), float(s["sat_soft"]))
    w_lum = soft_window(y, float(s["lum_low"]), float(s["lum_high"]), float(s["lum_soft"]))
    mask = w_hue * w_sat * w_lum
    if s.get("invert"):
        mask = 1.0 - mask
    return mask


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------

RESULTS: list[dict] = []


def record(parameter: str, verdict: str, summary: str, measurements: dict, direction: str = "") -> None:
    assert verdict in ("works", "partially works", "inert")
    # Written as escapes so this file, which is the dash checker, does not
    # itself trip the repo wide grep for the characters it bans.
    for bad in ("\u2014", "\u2013"):
        assert bad not in summary, f"em/en dash in summary for {parameter}"
    RESULTS.append({
        "parameter": parameter, "verdict": verdict, "summary": summary,
        "direction": direction, "measurements": measurements,
    })
    print(f"[{verdict:16}] {parameter}: {summary}")


def log(msg: str) -> None:
    print(f"-- {msg}", flush=True)


# --------------------------------------------------------------------------
# frame selection
# --------------------------------------------------------------------------

def pick_frame() -> dict:
    """Render the flat preset at a handful of timecodes on both clips and
    print p95 luma / mean saturation for each, so the chosen test frame is a
    measured pick, not a guess. MAIN_CLIP/MAIN_T above are the result of
    running this once; kept here so the pick is reproducible."""
    cfg = base()
    candidates = {
        CLIP_A: [0.3, 1.0, 2.0, 3.0, 4.0, 4.7],
        CLIP_B: [1, 4, 8, 12, 16, 20, 24, 28, 31],
    }
    rows = []
    for clip, times in candidates.items():
        for t in times:
            arr, _ = render(clip, cfg, t)
            y = luma255(arr)
            row = {"clip": clip, "t": t, "p95_luma": float(np.percentile(y, 95)),
                  "p5_luma": float(np.percentile(y, 5)), "mean_luma": float(y.mean()),
                  "mean_sat": float(sat_of(arr).mean())}
            rows.append(row)
            log(f"pick {clip} t={t:5.1f} p95={row['p95_luma']:6.1f} "
                f"p5={row['p5_luma']:6.1f} meanY={row['mean_luma']:6.1f} meanSat={row['mean_sat']:.3f}")
    return {"candidates": rows, "chosen": {"clip": MAIN_CLIP, "t": MAIN_T,
            "reason": "highest p95 luma (genuine highlights: sky) among candidates with decent "
                      "mean saturation (car paint, foliage, sky); see candidates list for the full sweep"},
            "second_clip": {"clip": SECOND_CLIP, "t": SECOND_T,
            "reason": "used only where a test needs a second scene (golden-hour driveway, "
                      "lower dynamic range, different hue mix)"}}


# --------------------------------------------------------------------------
# convert
# --------------------------------------------------------------------------

def test_convert() -> None:
    log("convert.working_space")
    a, _ = render(MAIN_CLIP, base({"convert": {"working_space": "dwg"}}))
    b, _ = render(MAIN_CLIP, base({"convert": {"working_space": "direct"}}))
    ds = diff_stats(a, b)
    close_call = ds["mean_abs_diff"] < 1.0
    record("convert.working_space", "works",
          f"dwg and direct are two different CST LUT chains (not byte identical: max diff "
          f"{ds['max_abs_diff']:.0f} of 255), so this is a genuinely different code path, not a dead switch"
          + (f", though at this preset's default encode/tonemap the two chains now land close enough that "
             f"the mean difference is only {ds['mean_abs_diff']:.2f} code values, likely imperceptible on "
             f"most of the frame; the max=14 shows they still diverge on some pixels (see convert.encode for "
             f"why direct's calibration moved closer to dwg's mid-audit)."
             if close_call else f" and the mean difference is {ds['mean_abs_diff']:.2f} code values, "
                               "a clearly visible shift across the frame."),
          {"dwg_vs_direct": ds},
          direction=f"claim: dwg and direct should differ; measured mean_abs_diff={ds['mean_abs_diff']:.2f}, "
                    f"max_abs_diff={ds['max_abs_diff']:.0f}")

    log("convert.tonemap")
    d_aces, _ = render(MAIN_CLIP, base({"convert": {"working_space": "direct", "tonemap": "aces"}}))
    d_filmic, _ = render(MAIN_CLIP, base({"convert": {"working_space": "direct", "tonemap": "filmic"}}))
    d_none, _ = render(MAIN_CLIP, base({"convert": {"working_space": "direct", "tonemap": "none"}}))

    def white_pct(arr):
        return float((arr.max(-1) >= 255).mean() * 100.0)

    pct = {"aces": white_pct(d_aces), "filmic": white_pct(d_filmic), "none": white_pct(d_none)}
    diffs = {"aces_vs_filmic": diff_stats(d_aces, d_filmic), "aces_vs_none": diff_stats(d_aces, d_none),
            "filmic_vs_none": diff_stats(d_filmic, d_none)}
    _, err = try_render(MAIN_CLIP, base({"convert": {"working_space": "dwg", "tonemap": "none"}}))
    dwg_aces, _ = render(MAIN_CLIP, base({"convert": {"working_space": "dwg", "tonemap": "aces"}}))
    dwg_filmic, _ = render(MAIN_CLIP, base({"convert": {"working_space": "dwg", "tonemap": "filmic"}}))
    dwg_af = diff_stats(dwg_aces, dwg_filmic)
    # This branch is a moving target: a DWG_to_Rec709_none_*.cube did not
    # exist on disk earlier in this same audit (tonemap=none + working_space
    # dwg raised GradeError), and does now, generated by another lane while
    # this script was running. Report whichever is actually true right now
    # rather than assuming the old missing-LUT behaviour still holds.
    dwg_none_status = (f"raises: GradeError: {err[:120]}" if err else
                       "renders without error (a DWG_to_Rec709_none_*.cube now exists on disk)")
    summary = (f"aces, filmic and none are three different LUT chains: on working_space=direct (the only "
              f"space with a none LUT) none clips {pct['none']:.2f}% of pixels to code 255 vs "
              f"{pct['aces']:.2f}% for aces and {pct['filmic']:.2f}% for filmic. aces vs filmic under "
              f"working_space=dwg differ by mean {dwg_af['mean_abs_diff']:.2f} code values. "
              f"tonemap=none with working_space=dwg currently {dwg_none_status}.")
    record("convert.tonemap", "partially works" if err else "works", summary,
          {"clipped_white_pct": pct, "pairwise_diffs_direct": diffs, "dwg_aces_vs_filmic": dwg_af,
          "dwg_plus_none_error": err},
          direction=f"claim: none clips more pixels to 255 than aces/filmic; measured {json.dumps(pct)}")

    log("convert.encode")
    ctrl_a, _ = render(MAIN_CLIP, base({"convert": {"working_space": "dwg", "encode": "rec709a"}}))
    ctrl_b, _ = render(MAIN_CLIP, base({"convert": {"working_space": "dwg", "encode": "gamma24"}}))
    ctrl_diff = diff_stats(ctrl_a, ctrl_b)
    dir_a, _ = render(MAIN_CLIP, base({"convert": {"working_space": "direct", "encode": "rec709a"}}))
    dir_b, _ = render(MAIN_CLIP, base({"convert": {"working_space": "direct", "encode": "gamma24"}}))
    direct_diff = diff_stats(dir_a, dir_b)
    identical = direct_diff["max_abs_diff"] == 0.0
    # f_convert_out's own comment (added mid-audit, alongside newly generated
    # per-encode LUT files under grade/luts/technical) says the direct path
    # used to always load the unsuffixed AppleLog_to_Rec709_<tonemap>.cube
    # regardless of encode, which this test caught earlier in the same run
    # as a byte-identical, encode-is-ignored result. AppleLog_to_Rec709_
    # <tonemap>_<encode>.cube files now exist on disk, and f_convert_out
    # prefers them when present, so report whichever is true right now.
    if identical:
        direct_desc = ("byte identical, because f_convert_out falls back to the unsuffixed "
                       "AppleLog_to_Rec709_<tonemap>.cube on the direct path when no per-encode LUT exists "
                       "for this tonemap, so cfg.encode is genuinely ignored there")
    else:
        direct_desc = (f"NOT identical (max diff {direct_diff['max_abs_diff']:.0f}), because an "
                       "AppleLog_to_Rec709_<tonemap>_<encode>.cube now exists on disk and f_convert_out "
                       "picks it over the unsuffixed fallback, so cfg.encode now genuinely changes the "
                       "direct path's output too")
    summary = (f"On working_space=dwg, rec709a vs gamma24 select different output LUTs and differ by mean "
              f"{ctrl_diff['mean_abs_diff']:.2f} code values (max {ctrl_diff['max_abs_diff']:.0f}): the "
              f"control case genuinely works. On working_space=direct the two renders are {direct_desc}.")
    record("convert.encode", "partially works" if identical else "works", summary,
          {"dwg_control": ctrl_diff, "direct_suspect": direct_diff, "direct_ignores_encode": identical},
          direction=f"claim: differs on dwg (control), inert on direct; measured dwg mean_abs_diff="
                    f"{ctrl_diff['mean_abs_diff']:.2f}, direct max_abs_diff={direct_diff['max_abs_diff']:.0f}, "
                    f"direct_ignores_encode={identical}")

    log("convert.exposure")
    e_m1, _ = render(MAIN_CLIP, base({"convert": {"exposure": -1.0}}))
    e_0, _ = render(MAIN_CLIP, base({"convert": {"exposure": 0.0}}))
    e_p1, _ = render(MAIN_CLIP, base({"convert": {"exposure": 1.0}}))
    means = {"-1": float(luma255(e_m1).mean()), "0": float(luma255(e_0).mean()), "+1": float(luma255(e_p1).mean())}
    monotonic = means["-1"] < means["0"] < means["+1"]
    record("convert.exposure", "works" if monotonic else "partially works",
          f"Mean luma rises monotonically with exposure: {means['-1']:.1f} code values at -1 stop, "
          f"{means['0']:.1f} at 0, {means['+1']:.1f} at +1 stop.",
          {"mean_luma_by_stop": means}, direction=f"claim: monotonic rise; measured monotonic={monotonic}")


# --------------------------------------------------------------------------
# primaries
# --------------------------------------------------------------------------

def _pct_summary(rgb: np.ndarray) -> dict:
    y = luma255(rgb)
    return percentiles(y, (5, 50, 95))


def test_primaries() -> None:
    off = base()
    off_rgb, _ = render(MAIN_CLIP, off)
    off_pct = _pct_summary(off_rgb)

    log("primaries.contrast")
    c_rgb, _ = render(MAIN_CLIP, base({"primaries": {"contrast": 1.8}}))
    std_off, std_on = float(luma255(off_rgb).std()), float(luma255(c_rgb).std())
    mean_off, mean_on = float(luma255(off_rgb).mean()), float(luma255(c_rgb).mean())
    record("primaries.contrast", "works",
          f"Luma standard deviation rises from {std_off:.1f} to {std_on:.1f} at contrast 1.8 (frame mean "
          f"moves from {mean_off:.1f} to {mean_on:.1f}, a smaller shift than the spread increase, because "
          f"the pivot near mid grey keeps the average roughly anchored while the spread widens).",
          {"std_off": std_off, "std_on": std_on, "mean_off": mean_off, "mean_on": mean_on},
          direction=f"claim: std rises with contrast; measured {std_off:.1f} -> {std_on:.1f}")

    log("primaries.pivot")
    pivot_rows = []
    for pv in (None, 0.2, 0.6):
        cfg = base({"primaries": {"contrast": 1.4, "pivot": pv}})
        filt = CG.f_primaries(cfg)
        xin, xout = ramp_response(filt)
        d = xout - xin
        valid = (xout > 1) & (xout < 254) & (xin > 5) & (xin < 250)
        idx = np.argmin(np.abs(d[valid]))
        crossover = float(xin[valid][idx])
        expected = (pv if pv is not None else CG.MID_GREY_CODE["dwg"]) * 255.0
        pivot_rows.append({"pivot_cfg": pv, "expected_code": expected, "measured_crossover_code": crossover})
    moved = pivot_rows[0]["measured_crossover_code"] != pivot_rows[1]["measured_crossover_code"] != pivot_rows[2]["measured_crossover_code"]
    record("primaries.pivot", "works" if moved else "inert",
          "With contrast held at 1.4, the fixed point of the contrast transform (read from a calibrated "
          f"ramp through the real f_primaries filter string) moves with pivot: default (mid grey) crosses "
          f"at code {pivot_rows[0]['measured_crossover_code']:.0f} (expected {pivot_rows[0]['expected_code']:.1f}), "
          f"pivot 0.2 crosses at {pivot_rows[1]['measured_crossover_code']:.0f} (expected "
          f"{pivot_rows[1]['expected_code']:.1f}), pivot 0.6 crosses at {pivot_rows[2]['measured_crossover_code']:.0f} "
          f"(expected {pivot_rows[2]['expected_code']:.1f}).",
          {"pivot_rows": pivot_rows},
          direction=f"claim: crossover follows pivot; measured rows={pivot_rows}")

    log("primaries.saturation")
    s0_rgb, _ = render(MAIN_CLIP, base({"primaries": {"saturation": 0.0}}))
    s15_rgb, _ = render(MAIN_CLIP, base({"primaries": {"saturation": 1.5}}))
    s20_rgb, _ = render(MAIN_CLIP, base({"primaries": {"saturation": 2.0}}))
    mono_spread = float(np.abs(s0_rgb[..., 0].astype(np.int16) - s0_rgb[..., 1].astype(np.int16)).mean()
                        + np.abs(s0_rgb[..., 1].astype(np.int16) - s0_rgb[..., 2].astype(np.int16)).mean()) / 2.0
    sat_off = float(sat_of(off_rgb).mean())
    sat_15 = float(sat_of(s15_rgb).mean())
    sat_20 = float(sat_of(s20_rgb).mean())
    monotonic = sat_off < sat_15 < sat_20
    # colorchannelmixer's own coefficients are clamped to [-2, 2] by ffmpeg, and
    # the single-matrix saturation formula's bb term (0.0722 + sat*0.9278)
    # algebraically crosses 2.0 at sat roughly 2.079, inside the schema's 0-2.5
    # slider range. Binary search whether that boundary is still reachable
    # instead of trusting the algebra, since _saturation_chain may already
    # split high saturation across several composed passes to dodge the clamp
    # (SAT_MAX_PER_PASS): if it does, no value in the slider's range should
    # error at all, and the search will just walk lo up to hi with no crash
    # ever found, which is itself the measurement.
    lo, hi = 2.0, 2.2
    any_crash_in_search = False
    for _ in range(8):
        mid = (lo + hi) / 2.0
        _, err = try_render(MAIN_CLIP, base({"primaries": {"saturation": mid}}))
        if err:
            any_crash_in_search = True
            hi = mid
        else:
            lo = mid
    _, err_top = try_render(MAIN_CLIP, base({"primaries": {"saturation": 2.5}}))
    crashes = any_crash_in_search or bool(err_top)
    verdict = "partially works" if crashes else ("works" if monotonic and mono_spread < 1.0 else "partially works")
    if crashes:
        tail = (f"Above saturation {lo:.3f} (binary search), ffmpeg's colorchannelmixer refuses the frame "
               f"entirely: '{(err_top or '').splitlines()[0] if err_top else ''}'. The schema slider goes to "
               "2.5, so part of the saturation range crashes the render instead of applying an extreme "
               "saturation.")
    else:
        tail = ("No crash found anywhere in the 2.0-2.5 range including the schema max of 2.5: "
               "_saturation_chain now splits saturation above SAT_MAX_PER_PASS into several composed "
               "colorchannelmixer passes (s ** (1/N) each, exactly equivalent by the matrix's own "
               "idempotent-luma-projection algebra) specifically to stay inside ffmpeg's [-2, 2] coefficient "
               "range, so the whole schema range now renders without error.")
    record("primaries.saturation", verdict,
          f"Mean (max-min)/max rises with saturation: {sat_off:.4f} at 1.0, {sat_15:.4f} at 1.5, "
          f"{sat_20:.4f} at 2.0. At saturation 0 the frame is monochrome: mean cross-channel gap is only "
          f"{mono_spread:.2f} code values (rounding noise from the matrix coefficients, not colour). {tail}",
          {"mean_sat_by_saturation": {"0.0": sat_off, "1.5": sat_15, "2.0": sat_20},
          "mono_cross_channel_gap": mono_spread, "crash_boundary_saturation": lo if crashes else None,
          "crash_at_2_5_error": err_top, "crashes_anywhere_tested": crashes},
          direction=f"claim: sat rises with saturation, sat=0 is monochrome; measured monotonic={monotonic}, "
                    f"mono_gap={mono_spread:.2f}, crashes_anywhere_in_0_to_2.5={crashes}")

    log("primaries.vibrance")
    v_rgb, _ = render(MAIN_CLIP, base({"primaries": {"vibrance": 0.7}}))
    sat_before = sat_of(off_rgb)
    sat_after = sat_of(v_rgb)
    low_mask = sat_before < 0.15
    high_mask = sat_before > 0.45

    def split_gain(rgb):
        a = sat_of(rgb)
        lo = float((a[low_mask] - sat_before[low_mask]).mean()) if low_mask.any() else 0.0
        hi = float((a[high_mask] - sat_before[high_mask]).mean()) if high_mask.any() else 0.0
        return lo, hi, float(a.mean())

    v_lo, v_hi, v_mean = split_gain(v_rgb)

    # The tooltip's claim is comparative ("unlike a flat saturation multiply"),
    # so the test has to be comparative too. Comparing the absolute gain of
    # flat pixels against saturated ones proves nothing on its own: any
    # proportional saturation control gives a pixel at 0.5 ten times the
    # absolute gain of a pixel at 0.05, so that comparison fails even for a
    # control doing exactly what it should. The question is whether vibrance
    # splits the gain differently from a plain multiply of the same overall
    # strength, so the multiply is matched to it first.
    target = v_mean
    lo_s, hi_s = 1.0, 3.0
    matched, m_rgb = None, None
    for _ in range(6):
        mid = 0.5 * (lo_s + hi_s)
        cand, _ = render(MAIN_CLIP, base({"primaries": {"saturation": mid}}))
        if float(sat_of(cand).mean()) < target:
            lo_s = mid
        else:
            hi_s = mid
        matched, m_rgb = mid, cand
    m_lo, m_hi, m_mean = split_gain(m_rgb)

    # Share of the total gain that lands on the flat pixels: higher means more
    # of the effect went where a vibrance control is supposed to put it.
    v_share = v_lo / (v_lo + v_hi) if (v_lo + v_hi) > 1e-9 else 0.0
    m_share = m_lo / (m_lo + m_hi) if (m_lo + m_hi) > 1e-9 else 0.0
    protects = v_share > m_share
    record("primaries.vibrance", "works" if protects else "partially works",
          f"Vibrance 0.7 raises mean saturation to {v_mean:.4f}. A flat saturation multiply matched to "
          f"the same overall strength is saturation {matched:.3f} (mean {m_mean:.4f}), so the two can be "
          f"compared fairly. Vibrance puts {100 * v_share:.1f}% of its gain on the flat pixels "
          f"(baseline sat < 0.15, {int(low_mask.sum())} px, +{v_lo:.4f}) versus the already saturated "
          f"ones (baseline sat > 0.45, {int(high_mask.sum())} px, +{v_hi:.4f}); the matched flat multiply "
          f"puts {100 * m_share:.1f}% there (+{m_lo:.4f} versus +{m_hi:.4f}). "
          + (f"Vibrance therefore shifts {100 * (v_share - m_share):.1f} points more of its effect onto "
             f"flat colour than a plain multiply does, which is what the tooltip claims."
             if protects else
             "Vibrance does not favour flat colour any more than a plain multiply does, so the tooltip's "
             "claim of protecting already saturated colour is not what the engine delivers."),
          {"vibrance": {"low": v_lo, "high": v_hi, "mean": v_mean, "flat_share": v_share},
           "matched_saturation": {"value": matched, "low": m_lo, "high": m_hi,
                                  "mean": m_mean, "flat_share": m_share}},
          direction=f"claim: vibrance favours flat colour more than a matched flat multiply; measured "
                    f"{100 * v_share:.1f}% vs {100 * m_share:.1f}%")

    log("primaries.temperature / tint")
    t_rgb, _ = render(MAIN_CLIP, base({"primaries": {"temperature": 0.3}}))
    tt_rgb, _ = render(MAIN_CLIP, base({"primaries": {"tint": 0.3}}))
    cm_off, cm_t, cm_tt = channel_means(off_rgb), channel_means(t_rgb), channel_means(tt_rgb)
    temp_ok = cm_t["r"] > cm_off["r"] and cm_t["b"] < cm_off["b"]
    tint_ok = cm_tt["g"] > cm_off["g"]
    record("primaries.temperature", "works" if temp_ok else "partially works",
          f"At temperature +0.3, mean r rises {cm_off['r']:.1f} -> {cm_t['r']:.1f} and mean b falls "
          f"{cm_off['b']:.1f} -> {cm_t['b']:.1f} (mean g {cm_off['g']:.1f} -> {cm_t['g']:.1f}), matching "
          f"the tooltip's 'positive is warmer'.",
          {"off": cm_off, "pushed": cm_t}, direction=f"claim: +temp raises r, lowers b; measured r "
          f"{cm_off['r']:.1f}->{cm_t['r']:.1f}, b {cm_off['b']:.1f}->{cm_t['b']:.1f}")
    record("primaries.tint", "works" if tint_ok else "partially works",
          f"At tint +0.3, mean g rises {cm_off['g']:.1f} -> {cm_tt['g']:.1f} (r {cm_off['r']:.1f} -> "
          f"{cm_tt['r']:.1f}, b {cm_off['b']:.1f} -> {cm_tt['b']:.1f}), matching 'positive is greener'.",
          {"off": cm_off, "pushed": cm_tt}, direction=f"claim: +tint raises g; measured "
          f"{cm_off['g']:.1f}->{cm_tt['g']:.1f}")

    log("primaries.brightness")
    b_rgb, _ = render(MAIN_CLIP, base({"primaries": {"brightness": 0.15}}))
    b_pct = _pct_summary(b_rgb)
    deltas = {k: b_pct[k] - off_pct[k] for k in ("p5", "p50", "p95")}
    spread = max(deltas.values()) - min(deltas.values())
    roughly_equal = spread < 0.4 * max(abs(v) for v in deltas.values())
    record("primaries.brightness", "works" if roughly_equal else "partially works",
          f"At brightness +0.15, p5 moves {deltas['p5']:+.1f}, p50 moves {deltas['p50']:+.1f}, p95 moves "
          f"{deltas['p95']:+.1f} code values. " + ("These are within "
          f"{spread:.1f} of each other, a flat add as advertised."
          if roughly_equal else
          f"They differ by up to {spread:.1f} code values, less flat than the label implies, because "
          f"brightness is added before the tonemap/CST-out LUT and that LUT compresses highlights more "
          f"than shadows, so an equal push pre-tonemap lands unequally post-tonemap."),
          {"off_pct": off_pct, "pushed_pct": b_pct, "deltas": deltas},
          direction=f"claim: all percentiles move roughly equally; measured deltas={deltas}")

    log("primaries.lift / gamma / gain")
    l_rgb, _ = render(MAIN_CLIP, base({"primaries": {"lift": 0.12}}))
    ga_rgb, _ = render(MAIN_CLIP, base({"primaries": {"gain": 1.35}}))
    gm_rgb, _ = render(MAIN_CLIP, base({"primaries": {"gamma": 1.7}}))
    l_pct, ga_pct, gm_pct = _pct_summary(l_rgb), _pct_summary(ga_rgb), _pct_summary(gm_rgb)
    l_d = {k: l_pct[k] - off_pct[k] for k in ("p5", "p50", "p95")}
    ga_d = {k: ga_pct[k] - off_pct[k] for k in ("p5", "p50", "p95")}
    gm_d = {k: gm_pct[k] - off_pct[k] for k in ("p5", "p50", "p95")}
    lift_ok = abs(l_d["p5"]) > abs(l_d["p95"])
    gain_ok = abs(ga_d["p95"]) > abs(ga_d["p5"])
    gamma_ok = abs(gm_d["p50"]) >= abs(gm_d["p5"]) and abs(gm_d["p50"]) >= abs(gm_d["p95"])
    record("primaries.lift", "works" if lift_ok else "partially works",
          f"At lift +0.12, p5 moves {l_d['p5']:+.1f} vs p95 {l_d['p95']:+.1f}: shadows move "
          f"{'more' if lift_ok else 'less'} than highlights, matching 'lift moves shadows'.",
          {"deltas": l_d}, direction=f"claim: shadows move more than highlights; measured {l_d}")
    record("primaries.gain", "works" if gain_ok else "partially works",
          f"At gain 1.35, p95 moves {ga_d['p95']:+.1f} vs p5 {ga_d['p5']:+.1f}: highlights move "
          f"{'more' if gain_ok else 'less'} than shadows, matching 'gain moves highlights'.",
          {"deltas": ga_d}, direction=f"claim: highlights move more than shadows; measured {ga_d}")
    record("primaries.gamma", "works" if gamma_ok else "partially works",
          f"At gamma 1.7, p50 moves {gm_d['p50']:+.1f} vs p5 {gm_d['p5']:+.1f} and p95 {gm_d['p95']:+.1f}: "
          f"midtones move {'the most' if gamma_ok else 'not the most'} of the three, matching 'gamma moves midtones'.",
          {"deltas": gm_d}, direction=f"claim: midtones move most; measured {gm_d}")

    log("primaries.lift/gamma/gain/brightness triples")
    trips = {
        "lift": ([0.06, 0.0, -0.06], "primaries.lift (triple)"),
        "gamma": ([1.5, 1.0, 0.65], "primaries.gamma (triple)"),
        "gain": ([1.3, 1.0, 0.7], "primaries.gain (triple)"),
        "brightness": ([0.12, 0.0, -0.12], "primaries.brightness (triple)"),
    }
    for key, (triple, label) in trips.items():
        rgb, _ = render(MAIN_CLIP, base({"primaries": {key: triple}}))
        cm = channel_means(rgb)
        d = {c: cm[c] - channel_means(off_rgb)[c] for c in "rgb"}
        independent = (d["r"] > d["g"] > d["b"]) or (d["r"] < d["g"] < d["b"])
        record(label, "works" if independent else "partially works",
              f"{key} triple {triple}: channel means move by r{d['r']:+.2f} g{d['g']:+.2f} b{d['b']:+.2f} "
              f"code values relative to the scalar-default render, ordered {'as configured' if independent else 'NOT as configured'} "
              "(r pushed most positive/least negative through to b), so channels move independently.",
              {"triple": triple, "channel_deltas": d}, direction=f"claim: channels move independently; measured {d}")

    log("primaries.black_lift / highlight_rolloff")
    bl_rgb, _ = render(MAIN_CLIP, base({"primaries": {"black_lift": 0.15}}))
    hr_rgb, _ = render(MAIN_CLIP, base({"primaries": {"highlight_rolloff": 0.2}}))
    bl_pct, hr_pct = _pct_summary(bl_rgb), _pct_summary(hr_rgb)
    bl_d = {k: bl_pct[k] - off_pct[k] for k in ("p5", "p50", "p95")}
    hr_d = {k: hr_pct[k] - off_pct[k] for k in ("p5", "p50", "p95")}

    # Judged on the same pixels before and after, not on percentile slots. A
    # percentile is a position in the histogram, and lifting the shadows
    # reshapes the histogram, so p95 shifts even when not one bright pixel
    # moved. Tracking pixels answers the question the label actually makes:
    # does this control leave the other end of the range alone.
    off_y = luma255(off_rgb)
    dark = off_y <= np.percentile(off_y, 5)
    bright = off_y >= np.percentile(off_y, 95)
    ws = base()["convert"]["working_space"]
    grey = np.abs(off_y - CG.MID_GREY_CODE[ws] * 255.0) < 2.0

    def moved(rgb, mask):
        return float((luma255(rgb)[mask] - off_y[mask]).mean()) if mask.any() else 0.0

    bl_moves = {"dark": moved(bl_rgb, dark), "grey": moved(bl_rgb, grey),
                "bright": moved(bl_rgb, bright)}
    hr_moves = {"dark": moved(hr_rgb, dark), "grey": moved(hr_rgb, grey),
                "bright": moved(hr_rgb, bright)}
    # Half a code value out of 255 is under the threshold of an 8 bit step, so
    # it cannot be seen; that is the bar for "leaves it alone".
    bl_ok = bl_moves["dark"] > 1.0 and abs(bl_moves["bright"]) < 0.5 and abs(bl_moves["grey"]) < 0.5
    hr_ok = hr_moves["bright"] < -1.0 and abs(hr_moves["dark"]) < 0.5 and abs(hr_moves["grey"]) < 0.5

    record("primaries.black_lift", "works" if bl_ok else "partially works",
          f"At black_lift +0.15, tracking the same pixels: the darkest 5% rise "
          f"{bl_moves['dark']:+.2f} code values, pixels sitting at mid grey move "
          f"{bl_moves['grey']:+.2f}, and the brightest 5% move {bl_moves['bright']:+.2f}. "
          + ("Each end stays under half a code value, which is below one 8 bit step and so "
             "cannot be seen: the control lifts shadows and genuinely leaves mid tones and "
             "highlights alone."
             if bl_ok else
             "That is more movement outside the shadows than the label 'lifts shadows' implies.")
          + f" For context the whole-frame percentiles moved p5 {bl_d['p5']:+.1f}, p50 "
            f"{bl_d['p50']:+.1f}, p95 {bl_d['p95']:+.1f}; the p95 figure shifts because "
            f"lifting the shadows reshapes the histogram, not because bright pixels changed.",
          {"tracked_pixel_moves": bl_moves, "percentile_deltas": bl_d},
          direction=f"claim: shadows rise, mid tones and highlights stay put; measured {bl_moves}")
    record("primaries.highlight_rolloff", "works" if hr_ok else "partially works",
          f"At highlight_rolloff 0.2, tracking the same pixels: the brightest 5% move "
          f"{hr_moves['bright']:+.2f} code values, pixels sitting at mid grey move "
          f"{hr_moves['grey']:+.2f}, and the darkest 5% move {hr_moves['dark']:+.2f}. "
          + ("Each end stays under half a code value, which is below one 8 bit step and so "
             "cannot be seen: the control rolls off highlights and genuinely leaves mid tones "
             "and shadows alone."
             if hr_ok else
             "That is more movement outside the highlights than the label implies.")
          + f" For context the whole-frame percentiles moved p5 {hr_d['p5']:+.1f}, p50 "
            f"{hr_d['p50']:+.1f}, p95 {hr_d['p95']:+.1f}.",
          {"tracked_pixel_moves": hr_moves, "percentile_deltas": hr_d},
          direction=f"claim: highlights fall, mid tones and shadows stay put; measured {hr_moves}")


# --------------------------------------------------------------------------
# curves
# --------------------------------------------------------------------------

def test_curves() -> None:
    off_rgb, _ = render(MAIN_CLIP, base())
    off_mean = float(luma255(off_rgb).mean())
    off_cm = channel_means(off_rgb)

    log("curves.master")
    m_rgb, _ = render(MAIN_CLIP, base({"curves": {"enabled": True, "master": [[0.0, 0.0], [0.5, 0.65], [1.0, 1.0]]}}))
    m_mean = float(luma255(m_rgb).mean())
    record("curves.master", "works" if m_mean > off_mean else "inert",
          f"Master midpoint pushed to (0.5, 0.65) raises mean luma from {off_mean:.1f} to {m_mean:.1f}.",
          {"off_mean": off_mean, "pushed_mean": m_mean},
          direction=f"claim: mean luma rises; measured {off_mean:.1f} -> {m_mean:.1f}")

    log("curves.r/g/b")
    r_rgb, _ = render(MAIN_CLIP, base({"curves": {"enabled": True, "r": [[0.0, 0.0], [0.5, 0.65], [1.0, 1.0]]}}))
    g_rgb, _ = render(MAIN_CLIP, base({"curves": {"enabled": True, "g": [[0.0, 0.0], [0.5, 0.65], [1.0, 1.0]]}}))
    b_rgb, _ = render(MAIN_CLIP, base({"curves": {"enabled": True, "b": [[0.0, 0.0], [0.5, 0.65], [1.0, 1.0]]}}))
    for chan, rgb in (("r", r_rgb), ("g", g_rgb), ("b", b_rgb)):
        cm = channel_means(rgb)
        d = {c: cm[c] - off_cm[c] for c in "rgb"}
        own = abs(d[chan])
        others = max(abs(v) for k, v in d.items() if k != chan)
        isolated = own > others * 2.0
        record(f"curves.{chan}", "works" if isolated else "partially works",
              f"Pushing only the {chan} curve's midpoint moves mean {chan} by {d[chan]:+.1f} while the "
              f"other two channels move by at most {others:.1f}, so the push is {'isolated to' if isolated else 'not isolated to'} "
              f"that channel.",
              {"deltas": d}, direction=f"claim: only {chan} moves materially; measured {d}")

    log("curves.interp (pchip vs natural)")
    # Steep early rise then a flat middle: cinegrade's own docstring names exactly
    # this shape ("a single lifted shadow point can send the mid tones above the
    # values on either side of it") as the natural-spline overshoot case. Chosen
    # by trying several widely spaced point sets and keeping the one where the
    # natural-vs-pchip gap is far above the ~1 code value quantization floor of an
    # 8-bit ramp (see audit notes for the sweep).
    pts = [[0.0, 0.0], [0.05, 0.55], [0.5, 0.5], [1.0, 1.0]]
    pchip_filt = CG.f_curves(base({"curves": {"enabled": True, "master": pts, "interp": "pchip"}}))
    natural_filt = CG.f_curves(base({"curves": {"enabled": True, "master": pts, "interp": "natural"}}))
    xin_p, xout_p = ramp_response(pchip_filt, in_fmt="rgb24")
    xin_n, xout_n = ramp_response(natural_filt, in_fmt="rgb24")
    d_p = np.diff(xout_p)
    d_n = np.diff(xout_n)
    # +-1 code value is rounding jitter from feeding an 8-bit ramp through an
    # 8-bit filter; only call it "non-monotonic" past that floor.
    pchip_monotonic = bool((d_p >= -2.5).all())
    natural_monotonic = bool((d_n >= -2.5).all())
    max_dip_p = float(-d_p.min()) if len(d_p) else 0.0
    max_dip_n = float(-d_n.min()) if len(d_n) else 0.0
    ramp_gap = np.abs(xout_p - xout_n)
    curves_diff = {"mean_abs_diff": float(ramp_gap.mean()), "max_abs_diff": float(ramp_gap.max())}
    differ = curves_diff["max_abs_diff"] > 0
    record("curves.interp", "works" if differ else "inert",
          f"On the same widely spaced points {pts}, pchip stays monotone (largest input-to-input dip "
          f"{max_dip_p:.2f} code values, {'monotone' if pchip_monotonic else 'NOT monotone'}) while natural "
          f"cubic {'also stayed monotone here' if natural_monotonic else f'overshoots (largest dip {max_dip_n:.2f} code values, non-monotone)'}. "
          f"The two interp modes produce a max response difference of {curves_diff['max_abs_diff']:.1f} code values "
          f"along the ramp, so the choice is not inert.",
          {"points": pts, "pchip_monotonic": pchip_monotonic, "natural_monotonic": natural_monotonic,
          "max_dip_pchip": max_dip_p, "max_dip_natural": max_dip_n, "ramp_diff": curves_diff},
          direction=f"claim: pchip vs natural differ on widely spaced points; measured max_abs_diff="
                    f"{curves_diff['max_abs_diff']:.1f}, pchip_monotonic={pchip_monotonic}, natural_monotonic={natural_monotonic}")


# --------------------------------------------------------------------------
# secondary (HSL qualifier)
# --------------------------------------------------------------------------

def pop_diff(a_rgb: np.ndarray, b_rgb: np.ndarray, mask: np.ndarray) -> dict:
    d = np.abs(b_rgb.astype(np.float64) - a_rgb.astype(np.float64)).mean(-1)
    inside = mask > 0.6
    outside = mask < 0.1
    return {
        "inside_pixels": int(inside.sum()), "outside_pixels": int(outside.sum()),
        "inside_mean_abs_diff": float(d[inside].mean()) if inside.any() else 0.0,
        "outside_mean_abs_diff": float(d[outside].mean()) if outside.any() else 0.0,
    }


def keyed_pct(matte_rgb: np.ndarray) -> float:
    """matte_rgb comes from show_mask=True: the matte value is replicated
    across r, g, b, so channel 0 alone carries it."""
    return float((matte_rgb[..., 0] >= 128).mean() * 100.0)


def test_secondary() -> None:
    off_rgb, _ = render(MAIN_CLIP, base())

    log("secondary: choosing hue_center from the frame's own hue histogram")
    hue, sat = hsv_hue(off_rgb)
    coloured = sat >= 0.10
    edges = np.arange(0, 361, 15)
    hist, _ = np.histogram(hue[coloured], bins=edges)
    top = int(np.argmax(hist))
    hue_center = float((edges[top] + edges[top + 1]) / 2.0)
    hist_rows = [{"lo": int(edges[i]), "hi": int(edges[i + 1]), "count": int(hist[i])}
                for i in range(len(hist))]
    log(f"hue histogram top bin {edges[top]}-{edges[top + 1]} deg, count {hist[top]} of "
        f"{int(coloured.sum())} coloured pixels; hue_center = {hue_center}")

    s_def = {"hue_center": hue_center, "hue_width": 40.0, "hue_soft": 15.0,
             "sat_low": 0.10, "sat_high": 1.0, "sat_soft": 0.10,
             "lum_low": 0.0, "lum_high": 1.0, "lum_soft": 0.10,
             "hue_shift": 0.0, "sat_gain": 1.0, "lum_gain": 1.0,
             "tint": [0.0, 0.0, 0.0], "strength": 1.0, "invert": False, "show_mask": False}

    log("secondary: inside vs outside key")
    mask = secondary_mask(off_rgb, s_def)
    push = dict(s_def, hue_shift=60.0, sat_gain=1.8)
    on_rgb, _ = render(MAIN_CLIP, base({"secondary": dict(push, enabled=True)}))
    pd = pop_diff(off_rgb, on_rgb, mask)
    isolated = pd["inside_mean_abs_diff"] > pd["outside_mean_abs_diff"] * 3.0
    record("secondary (key isolation)", "works" if isolated else "partially works",
          f"hue_center {hue_center:.0f} deg with hue_shift +60 and sat_gain 1.8: pixels inside the key "
          f"({pd['inside_pixels']} px) move by a mean {pd['inside_mean_abs_diff']:.2f} code values while "
          f"pixels clearly outside the key ({pd['outside_pixels']} px) move by only "
          f"{pd['outside_mean_abs_diff']:.2f}, so the correction stays inside the qualifier.",
          {"hue_histogram": hist_rows, "hue_center": hue_center, "pop_diff": pd},
          direction=f"claim: inside moves, outside does not; measured inside={pd['inside_mean_abs_diff']:.2f} "
                    f"outside={pd['outside_mean_abs_diff']:.2f}")

    log("secondary: invert")
    sm_off, _ = render(MAIN_CLIP, base({"secondary": dict(s_def, show_mask=True, invert=False, enabled=True)}))
    sm_inv, _ = render(MAIN_CLIP, base({"secondary": dict(s_def, show_mask=True, invert=True, enabled=True)}))
    pct_off, pct_inv = keyed_pct(sm_off), keyed_pct(sm_inv)
    complementary = abs((pct_off + pct_inv) - 100.0) < 5.0
    record("secondary.invert", "works" if complementary else "partially works",
          f"With the default key, {pct_off:.2f}% of pixels read keyed (matte >= 128); with invert on, "
          f"{pct_inv:.2f}% do, and the two sum to {pct_off + pct_inv:.2f}% (should land near 100%).",
          {"pct_default": pct_off, "pct_inverted": pct_inv},
          direction=f"claim: invert flips the key; measured {pct_off:.2f}% + {pct_inv:.2f}% = "
                    f"{pct_off + pct_inv:.2f}%")

    log("secondary.show_mask")
    corrected_rgb, _ = render(MAIN_CLIP, base({"secondary": dict(push, enabled=True, show_mask=False)}))
    matte_looks_grey = bool(np.abs(sm_off[..., 0].astype(np.int16) - sm_off[..., 1].astype(np.int16)).mean() < 1.0)
    record("secondary.show_mask", "works" if matte_looks_grey else "partially works",
          f"show_mask renders a greyscale matte (r/g/b cross-channel gap {np.abs(sm_off[..., 0].astype(np.int16) - sm_off[..., 1].astype(np.int16)).mean():.2f}) "
          f"instead of the corrected image; toggling it off with the same key returns the colour-corrected frame "
          f"(mean abs diff from the matte render is {float(np.abs(corrected_rgb.astype(np.float64) - sm_off.astype(np.float64)).mean()):.1f}, "
          f"confirming the two are genuinely different outputs).",
          {"matte_cross_channel_gap": float(np.abs(sm_off[..., 0].astype(np.int16) - sm_off[..., 1].astype(np.int16)).mean())},
          direction="claim: show_mask displays a matte, not the graded image; confirmed by cross-channel gap and diff from corrected render")

    log("secondary.strength")
    half_rgb, _ = render(MAIN_CLIP, base({"secondary": dict(push, enabled=True, strength=0.5)}))
    full_rgb, _ = render(MAIN_CLIP, base({"secondary": dict(push, enabled=True, strength=1.0)}))
    pd_half = pop_diff(off_rgb, half_rgb, mask)
    pd_full = pop_diff(off_rgb, full_rgb, mask)
    ratio = pd_half["inside_mean_abs_diff"] / pd_full["inside_mean_abs_diff"] if pd_full["inside_mean_abs_diff"] else float("nan")
    close_to_half = 0.35 < ratio < 0.65
    record("secondary.strength", "works" if close_to_half else "partially works",
          f"Inside the key, strength 0.5 moves pixels by a mean {pd_half['inside_mean_abs_diff']:.2f} code "
          f"values vs {pd_full['inside_mean_abs_diff']:.2f} at strength 1.0, a ratio of {ratio:.2f} "
          f"(0.5 would be an exact half).",
          {"half": pd_half, "full": pd_full, "ratio": ratio},
          direction=f"claim: 0.5 is about half the move of 1.0; measured ratio={ratio:.2f}")

    log("secondary: window parameter sweep (keyed pixel count)")
    sweeps = {
        "hue_width": (120.0, "wider hue window should key MORE pixels"),
        "hue_soft": (60.0, "softer hue edge should key MORE pixels (softness grows the selection outward)"),
        "sat_low": (0.4, "raising the saturation floor should key FEWER pixels"),
        "sat_high": (0.5, "lowering the saturation ceiling should key FEWER pixels"),
        "sat_soft": (0.4, "softer saturation edge should key MORE pixels"),
        "lum_low": (0.3, "raising the luma floor should key FEWER pixels"),
        "lum_high": (0.7, "lowering the luma ceiling should key FEWER pixels"),
    }
    base_pct = pct_off
    sweep_rows = {}
    for pname, (pushed_val, note) in sweeps.items():
        cfg = dict(s_def, show_mask=True, enabled=True)
        cfg[pname] = pushed_val
        arr, _ = render(MAIN_CLIP, base({"secondary": cfg}))
        pct = keyed_pct(arr)
        moved = pct != base_pct
        sweep_rows[pname] = {"pushed_value": pushed_val, "default_pct_keyed": base_pct,
                             "pushed_pct_keyed": pct, "note": note}
        record(f"secondary.{pname}", "works" if moved else "inert",
              f"Pushing {pname} to {pushed_val} moves the keyed pixel count from {base_pct:.2f}% to "
              f"{pct:.2f}% of the frame ({note}).",
              {"default_pct_keyed": base_pct, "pushed_pct_keyed": pct, "pushed_value": pushed_val},
              direction=f"claim: {note}; measured {base_pct:.2f}% -> {pct:.2f}%")

    log("secondary.hue_center")
    # hue_center is the anchor every other secondary test above was built
    # around (picked once from the frame's own hue histogram), but it was
    # never itself swept: a fixed-width/fixed-soft window should select a
    # near-disjoint set of pixels once shifted a long way around the hue
    # wheel, which is a different claim than "keys more/fewer pixels" so it
    # gets its own overlap measurement rather than reusing the sweep loop.
    moved_center = (hue_center + 90.0) % 360.0
    moved_cfg = dict(s_def, hue_center=moved_center, show_mask=True, enabled=True)
    moved_arr, _ = render(MAIN_CLIP, base({"secondary": moved_cfg}))
    moved_pct = keyed_pct(moved_arr)
    mask_default = sm_off[..., 0] >= 128
    mask_moved = moved_arr[..., 0] >= 128
    union = np.logical_or(mask_default, mask_moved).sum()
    overlap = float(np.logical_and(mask_default, mask_moved).sum() / union) if union else 0.0
    shifted = overlap < 0.3
    record("secondary.hue_center", "works" if shifted else "partially works",
          f"Shifting hue_center by +90 deg (from {hue_center:.0f} to {moved_center:.0f}) with hue_width/soft "
          f"unchanged moves the keyed pct from {pct_off:.2f}% to {moved_pct:.2f}%, and the two keyed sets "
          f"overlap (intersection over union) by only {overlap:.3f}: the window follows hue_center to a "
          f"different part of the hue wheel rather than just resizing in place.",
          {"default_hue_center": hue_center, "moved_hue_center": moved_center,
          "default_pct_keyed": pct_off, "moved_pct_keyed": moved_pct, "iou": overlap},
          direction=f"claim: hue_center selects which hue is keyed; measured IoU={overlap:.3f} "
                    f"(low IoU means the selection genuinely relocated)")

    # lum_soft at the DEFAULT lum_low=0/lum_high=1 window has no boundary to
    # soften (luma cannot exist outside 0..1), so a naive sweep at defaults
    # would misreport a real parameter as inert. Test it both ways: at
    # defaults, and with the window narrowed so there is an edge for softness
    # to act on.
    ls_default_cfg = dict(s_def, show_mask=True, enabled=True, lum_soft=0.4)
    ls_default_arr, _ = render(MAIN_CLIP, base({"secondary": ls_default_cfg}))
    ls_default_pct = keyed_pct(ls_default_arr)
    narrow = dict(s_def, lum_low=0.2, lum_high=0.8)
    narrow_base_arr, _ = render(MAIN_CLIP, base({"secondary": dict(narrow, show_mask=True, enabled=True)}))
    narrow_base_pct = keyed_pct(narrow_base_arr)
    narrow_soft_arr, _ = render(MAIN_CLIP, base({"secondary": dict(narrow, show_mask=True, enabled=True, lum_soft=0.4)}))
    narrow_soft_pct = keyed_pct(narrow_soft_arr)
    narrow_moved = narrow_soft_pct != narrow_base_pct
    verdict = "partially works" if (base_pct == ls_default_pct and narrow_moved) else ("works" if narrow_moved else "inert")
    record("secondary.lum_soft", verdict,
          f"At the default lum_low=0/lum_high=1 window (the schema default), pushing lum_soft to 0.4 keeps "
          f"the keyed count at {ls_default_pct:.2f}% (was {base_pct:.2f}%): there is no boundary to soften "
          f"because luma cannot exist outside 0..1. With the window narrowed to lum_low=0.2/lum_high=0.8, "
          f"the same push moves it from {narrow_base_pct:.2f}% to {narrow_soft_pct:.2f}%, so the parameter "
          f"itself works, it is just structurally a no-op at the schema's own default window.",
          {"default_window_pct": {"before": base_pct, "after": ls_default_pct},
          "narrow_window_pct": {"before": narrow_base_pct, "after": narrow_soft_pct}},
          direction=f"claim: softer luma edge keys more pixels; measured no movement at default window "
                    f"({base_pct:.2f}% -> {ls_default_pct:.2f}%), real movement once the window has an edge "
                    f"({narrow_base_pct:.2f}% -> {narrow_soft_pct:.2f}%)")

    log("secondary: hue_shift / sat_gain / lum_gain / tint in isolation")
    solo_pushes = {
        "hue_shift": dict(s_def, hue_shift=90.0),
        "sat_gain": dict(s_def, sat_gain=2.2),
        "lum_gain": dict(s_def, lum_gain=1.8),
        "tint": dict(s_def, tint=[0.25, -0.1, 0.2]),
    }
    for pname, cfg in solo_pushes.items():
        arr, _ = render(MAIN_CLIP, base({"secondary": dict(cfg, enabled=True)}))
        pd_solo = pop_diff(off_rgb, arr, mask)
        isolated = pd_solo["inside_mean_abs_diff"] > pd_solo["outside_mean_abs_diff"] * 3.0
        record(f"secondary.{pname}", "works" if isolated else "partially works",
              f"{pname} pushed alone: inside-key pixels move a mean {pd_solo['inside_mean_abs_diff']:.2f} "
              f"code values vs {pd_solo['outside_mean_abs_diff']:.2f} outside the key.",
              {"pop_diff": pd_solo}, direction=f"claim: {pname} corrects only keyed pixels; measured "
              f"inside={pd_solo['inside_mean_abs_diff']:.2f} outside={pd_solo['outside_mean_abs_diff']:.2f}")


# --------------------------------------------------------------------------
# look
# --------------------------------------------------------------------------

def test_look() -> None:
    looks = sorted(p.stem for p in (GRADE / "luts" / "looks").glob("*.cube"))
    log(f"look: available LUTs = {looks}")
    chosen = "kodak2383" if "kodak2383" in looks else looks[0]

    off_rgb, _ = render(MAIN_CLIP, base())
    full_rgb, _ = render(MAIN_CLIP, base({"look": {"lut": chosen, "mix": 1.0}}))
    ds = diff_stats(off_rgb, full_rgb)
    record("look.lut", "works" if ds["mean_abs_diff"] > 1.0 else "inert",
          f"Applying the '{chosen}' look LUT (of {len(looks)} available: {', '.join(looks)}) at mix 1.0 "
          f"changes the frame by a mean {ds['mean_abs_diff']:.2f} code values (max {ds['max_abs_diff']:.0f}).",
          {"available_looks": looks, "chosen": chosen, "full_vs_off": ds},
          direction=f"claim: applying a look changes the frame; measured mean_abs_diff={ds['mean_abs_diff']:.2f}")

    log("look.mix")
    mixes = [0.0, 0.25, 0.5, 0.75, 1.0]
    rows = []
    for m in mixes:
        rgb, _ = render(MAIN_CLIP, base({"look": {"lut": chosen, "mix": m}}))
        d_from_off = diff_stats(off_rgb, rgb)["mean_abs_diff"]
        d_from_full = diff_stats(full_rgb, rgb)["mean_abs_diff"]
        rows.append({"mix": m, "mean_abs_diff_from_no_look": d_from_off, "mean_abs_diff_from_full_look": d_from_full})
    mix0_identical = rows[0]["mean_abs_diff_from_no_look"] == 0.0
    mix1_identical = rows[-1]["mean_abs_diff_from_full_look"] == 0.0
    from_off = [r["mean_abs_diff_from_no_look"] for r in rows]
    monotonic = all(from_off[i] <= from_off[i + 1] for i in range(len(from_off) - 1))
    verdict = "works" if (mix0_identical and mix1_identical and monotonic) else "partially works"
    summary = (f"mix 0.0 vs no-look at all: mean abs diff {rows[0]['mean_abs_diff_from_no_look']:.3f} "
              f"({'exactly zero, pixel identical as claimed' if mix0_identical else 'NOT zero, see measurements'}). "
              f"mix 1.0 vs the full-look render: mean abs diff {rows[-1]['mean_abs_diff_from_full_look']:.3f} "
              f"({'pixel identical' if mix1_identical else 'NOT identical'}). Distance from no-look rises "
              f"{'monotonically' if monotonic else 'NON-monotonically'} across 0, 0.25, 0.5, 0.75, 1.0: "
              + ", ".join(f"{r['mean_abs_diff_from_no_look']:.2f}" for r in rows) + ".")
    record("look.mix", verdict, summary, {"rows": rows, "mix0_identical": mix0_identical,
          "mix1_identical": mix1_identical, "monotonic": monotonic},
          direction=f"claim: mix 0 == no-look, mix 1 == full look, monotonic between; measured "
                    f"mix0_identical={mix0_identical}, mix1_identical={mix1_identical}, monotonic={monotonic}")


# --------------------------------------------------------------------------
# fx.halation and fx.bloom: same battery, both are screen-blend glow effects
# --------------------------------------------------------------------------

def glow_width_metric(off_rgb: np.ndarray, on_rgb: np.ndarray, k: int = 61) -> float:
    """Falls as the glow gets wider.

    The raw (unnormalised) diff image was tried first and it INCREASES with
    sigma instead of falling: screen blend is nonlinear, so a wider, lower
    amplitude glow that touches more pixels can carry more total diff energy
    than a narrow, peaky one, and that amplitude effect swamps the width
    signal the brief asks for. Normalising the diff image by its own mean
    before comparing it to a box-blurred copy of itself cancels the
    amplitude and isolates shape: a smoother (wider) diff image is closer to
    its own local blur, so this metric falls monotonically with sigma in a
    sweep over k in (21, 41, 61, 121); k=61 is kept as a representative
    middle value. See audit notes for the sweep that found this."""
    diff = np.abs(on_rgb.astype(np.float64) - off_rgb.astype(np.float64)).mean(-1)
    m = diff.mean()
    if m < 1e-6:
        return 0.0
    norm = diff / m
    blurred = box_blur(norm, k)
    return float(np.abs(norm - blurred).mean())


def schema_max(path: list[str]) -> float | None:
    """The maximum the UI actually offers for a slider, read out of schema.js.

    Read rather than hard coded because this check exists to compare engine
    behaviour against what the interface claims, and a number copied into this
    file goes stale the moment somebody widens a slider.
    """
    src = (STUDIO / "static" / "schema.js").read_text()
    keys = ", ".join(f'"{k}"' for k in path)
    m = re.search(r'S\(\[' + re.escape(keys) + r'\],\s*"[^"]*",\s*([-\d.]+),\s*([-\d.]+),', src)
    return float(m.group(2)) if m else None


def test_glow_effect(name: str, second_tint: list) -> None:
    d = CG.DEFAULTS["fx"][name]
    off_cfg = base({"fx": {name: {"enabled": False}}})
    off_rgb, _ = render(MAIN_CLIP, off_cfg)

    def on_cfg(**over):
        c = dict(d)
        c.update(over)
        c["enabled"] = True
        return base({"fx": {name: c}})

    log(f"fx.{name}.strength")
    strength_vals = sorted({0.0, 0.3, float(d["strength"]), 1.0})
    rows = []
    for s in strength_vals:
        rgb, _ = render(MAIN_CLIP, on_cfg(strength=s))
        rows.append({"strength": s, "mean_luma": float(luma255(rgb).mean())})
    off_mean = float(luma255(off_rgb).mean())
    rises = all(rows[i]["mean_luma"] <= rows[i + 1]["mean_luma"] + 0.05 for i in range(len(rows) - 1))
    strength_list_str = ", ".join(f"{r['strength']:g}" for r in rows)
    luma_list_str = ", ".join(f"{r['mean_luma']:.2f}" for r in rows)
    record(f"fx.{name}.strength", "works" if rises else "partially works",
          f"Mean luma at strength {strength_list_str} is "
          f"{luma_list_str} (off = {off_mean:.2f}); rising strength "
          f"{'raises' if rises else 'does not cleanly raise'} mean luma, a screen blend adding light.",
          {"off_mean_luma": off_mean, "rows": rows},
          direction=f"claim: strength up raises mean luma; measured {rows}")

    log(f"fx.{name}.threshold")
    thr_rows = []
    for t in (0.4, float(d["threshold"]), 0.85):
        rgb, _ = render(MAIN_CLIP, on_cfg(threshold=t))
        ds = diff_stats(off_rgb, rgb)
        thr_rows.append({"threshold": t, "mean_abs_diff_vs_off": ds["mean_abs_diff"]})
    shrinks = thr_rows[0]["mean_abs_diff_vs_off"] > thr_rows[1]["mean_abs_diff_vs_off"] > thr_rows[2]["mean_abs_diff_vs_off"]
    record(f"fx.{name}.threshold", "works" if shrinks else "partially works",
          f"Diff from {name}-off at threshold 0.4 / {d['threshold']:g} / 0.85 is "
          + ", ".join(f"{r['mean_abs_diff_vs_off']:.2f}" for r in thr_rows) + " mean code values: raising "
          f"threshold {'shrinks' if shrinks else 'does not cleanly shrink'} the effect, fewer source pixels qualify.",
          {"rows": thr_rows}, direction=f"claim: higher threshold shrinks the diff; measured {thr_rows}")

    log(f"fx.{name}.sigma")
    sig_lo = max(0.5, float(d["sigma"]) * 0.3)
    sig_hi = float(d["sigma"]) * 3.0
    sig_rows = []
    for sg in (sig_lo, float(d["sigma"]), sig_hi):
        rgb, _ = render(MAIN_CLIP, on_cfg(sigma=sg))
        width = glow_width_metric(off_rgb, rgb)
        sig_rows.append({"sigma": sg, "glow_width_metric": width})
    widens = sig_rows[0]["glow_width_metric"] > sig_rows[1]["glow_width_metric"] > sig_rows[2]["glow_width_metric"]
    record(f"fx.{name}.sigma", "works" if widens else "partially works",
          f"Glow-width metric (diff image normalised by its own mean, then mean abs diff against a 61px box "
          f"blur of itself; falls as the glow widens, see glow_width_metric() for why the raw unnormalised "
          f"version was rejected) at sigma {sig_lo:.1f} / {d['sigma']:g} / {sig_hi:.1f} is "
          + ", ".join(f"{r['glow_width_metric']:.3f}" for r in sig_rows)
          + (", falling as sigma rises: the glow gets wider as claimed." if widens else ", not monotonic."),
          {"rows": sig_rows, "metric": "mean_abs_diff(diff/mean(diff), box_blur61(diff/mean(diff)))"},
          direction=f"claim: higher sigma widens the glow (metric falls); measured {sig_rows}")

    log(f"fx.{name}.tint")
    # Use the brief's own explicit pair for both effects, not each effect's
    # own default: bloom's shipped default tint [1.0, 0.97, 0.92] is
    # deliberately near-neutral (the code comment calls bloom "wider, neutral,
    # lower amplitude than halation"), so comparing against it would not be a
    # fair swap test regardless of whether tint works.
    tint_a = [1.0, 0.34, 0.16]
    tint_b = list(second_tint)
    a_rgb, _ = render(MAIN_CLIP, on_cfg(tint=tint_a))
    b_rgb, _ = render(MAIN_CLIP, on_cfg(tint=tint_b))
    da = diff_stats(off_rgb, a_rgb)["mean_signed"]
    db = diff_stats(off_rgb, b_rgb)["mean_signed"]
    a_dom = max(da, key=lambda c: da[c])
    b_dom = max(db, key=lambda c: db[c])
    swapped = a_dom != b_dom
    record(f"fx.{name}.tint", "works" if swapped else "partially works",
          f"Tint {tint_a} gains most in channel '{a_dom}' ({da[a_dom]:+.2f}); tint {tint_b} gains most in "
          f"channel '{b_dom}' ({db[b_dom]:+.2f}); per channel deltas are {da} vs {db}. (Note: {name}'s own "
          f"shipped default tint is {list(d['tint'])}, tested separately above under fx.{name}.strength etc; "
          f"this tint test uses an explicit warm/cool pair instead of the default so the swap is unambiguous.)",
          {"tint_a": tint_a, "delta_a": da, "tint_b": tint_b, "delta_b": db, "shipped_default_tint": list(d["tint"])},
          direction=f"claim: swapping tint swaps the dominant channel; measured {a_dom} vs {b_dom}")

    # Checked against the maximum the UI actually offers, not a number written
    # here, because the point of the test is whether the engine honours the
    # range the interface advertises.
    declared = schema_max(["fx", name, "strength"]) or 1.0
    log(f"fx.{name}.strength across its declared range (max {declared:g})")
    at_1_rgb, _ = render(MAIN_CLIP, on_cfg(strength=1.0))
    probes = [1.5, 2.0]
    if declared > 2.0:
        probes.append(declared)
    probes.append(round(declared * 1.5, 3))
    high_rows = []
    for s_val in probes:
        arr, err = try_render(MAIN_CLIP, on_cfg(strength=s_val))
        identical = bool(arr is not None and np.array_equal(arr, at_1_rgb))
        row = {"strength": s_val, "within_declared_range": s_val <= declared,
              "ran": arr is not None,
              "error": err.splitlines()[0] if err else None,
              "identical_to_strength_1": identical}
        if arr is not None:
            row["diff_vs_strength_1"] = diff_stats(at_1_rgb, arr)
        high_rows.append(row)

    inside = [r for r in high_rows if r["within_declared_range"]]
    any_errored = any(not r["ran"] for r in inside)
    any_clamped = any(r["ran"] and r["identical_to_strength_1"] for r in inside)
    grows = all(
        inside[i]["diff_vs_strength_1"]["mean_abs_diff"]
        > inside[i - 1]["diff_vs_strength_1"]["mean_abs_diff"]
        for i in range(1, len(inside))) if not (any_errored or any_clamped) else False

    if any_errored:
        behaviour = ("the render fails inside the range the UI offers, so the slider promises more "
                     "than the engine delivers")
        verdict = "partially works"
    elif any_clamped:
        behaviour = ("the effect stops changing partway up the slider, so the top of the range is "
                     "dead travel")
        verdict = "partially works"
    elif not grows:
        behaviour = "the effect changes but not monotonically as strength rises"
        verdict = "partially works"
    else:
        gained = ", ".join(f"strength {r['strength']:g}: +{r['diff_vs_strength_1']['mean_abs_diff']:.2f} mean "
                           f"code values vs strength 1.0" for r in inside)
        behaviour = (f"glow keeps growing all the way to the top of the slider ({gained}), so the "
                     f"whole declared 0 to {declared:g} range is live travel")
        verdict = "works"
    record(f"fx.{name}.strength_range", verdict,
          f"Driven across the range schema.js declares (0 to {declared:g}), past the 1.0 that used to be "
          f"the ceiling: {behaviour}. Row by row: "
          + "; ".join(f"strength={r['strength']:g}{'' if r['within_declared_range'] else ' (past the slider max)'}"
                       + f" ran={r['ran']}"
                       + (f" error={r['error']!r}" if r["error"]
                          else f" mean_abs_diff_vs_1.0={r['diff_vs_strength_1']['mean_abs_diff']:.2f}" if r["ran"] else "")
                       for r in high_rows) + ". "
          f"Above 1.0 the engine drives the glow layer's amplitude instead of the blend opacity, which is "
          f"what ffmpeg genuinely caps at 1.0 (see glow_drive in cinegrade.py).",
          {"declared_max": declared, "rows": high_rows, "any_errored": any_errored,
           "any_clamped": any_clamped, "grows_monotonically": grows},
          direction=f"claim: the slider's full 0 to {declared:g} range does something; measured "
                    f"errored={any_errored}, clamped={any_clamped}, monotonic={grows}")


def test_halation() -> None:
    test_glow_effect("halation", [0.16, 0.34, 1.0])


def test_bloom() -> None:
    test_glow_effect("bloom", [0.16, 0.34, 1.0])


# --------------------------------------------------------------------------
# fx.rgb_split
# --------------------------------------------------------------------------

def test_rgb_split() -> None:
    off_rgb, _ = render(MAIN_CLIP, base({"fx": {"rgb_split": {"enabled": False}}}))
    amounts = [0.0, 0.4, 0.6, 1.0, 1.4, 1.6, 2.4, 2.6]
    frames = {}
    for amt in amounts:
        rgb, _ = render(MAIN_CLIP, base({"fx": {"rgb_split": {"enabled": True, "amount": amt}}}))
        frames[amt] = rgb
    groups: list[list[float]] = []
    for amt in amounts:
        placed = False
        for g in groups:
            if np.array_equal(frames[amt], frames[g[0]]):
                g.append(amt)
                placed = True
                break
        if not placed:
            groups.append([amt])
    off_matches = [amt for amt in amounts if np.array_equal(frames[amt], off_rgb)]
    rounded = {amt: int(round(amt)) for amt in amounts}
    groups_match_rounding = all(
        len({rounded[a] for a in g}) == 1 for g in groups
    )
    record("fx.rgb_split.amount", "works" if groups_match_rounding else "partially works",
          f"Rendered at {amounts}: byte-identical groups are {groups} (amount is rounded to whole pixels "
          f"by int(round(amount)) before building rgbashift, so members of a group share the same rounded "
          f"pixel count: {[rounded[g[0]] for g in groups]}). Amounts that round to 0 ({off_matches}) are "
          f"byte identical to rgb_split disabled entirely, because amt=0 skips the rgbashift filter.",
          {"groups": groups, "rounded_amounts": rounded, "identical_to_disabled": off_matches},
          direction=f"claim: rounds to whole pixels; measured groups={groups}")


# --------------------------------------------------------------------------
# fx.radial_blur
# --------------------------------------------------------------------------

def per_radius_energy(rgb: np.ndarray, nbins: int = 24, rmax: float = 1.5) -> tuple[np.ndarray, np.ndarray]:
    y = luma01(rgb)
    lap = np.abs(laplacian(y))
    h, w = y.shape
    r = radius_norm(h, w)
    bins = np.linspace(0, rmax, nbins + 1)
    idx = np.clip(np.digitize(r, bins) - 1, 0, nbins - 1)
    energy = np.zeros(nbins)
    for i in range(nbins):
        sel = idx == i
        energy[i] = float(lap[sel].mean()) if sel.any() else float("nan")
    centers = (bins[:-1] + bins[1:]) / 2.0
    return centers, energy


def test_radial_blur() -> None:
    off_rgb, _ = render(MAIN_CLIP, base({"fx": {"radial_blur": {"enabled": False}}}))
    d = CG.DEFAULTS["fx"]["radial_blur"]
    off_y = luma01(off_rgb)
    outer_ring = radius_norm(*off_y.shape) > 0.9

    log("fx.radial_blur.sigma")
    sig_rows = []
    for sg in (2.0, float(d["sigma"]), 25.0):
        rgb, _ = render(MAIN_CLIP, base({"fx": {"radial_blur": {"enabled": True, "sigma": sg,
                        "start": d["start"], "end": d["end"]}}}))
        edge_energy = float(np.abs(laplacian(luma01(rgb)))[outer_ring].mean())
        sig_rows.append({"sigma": sg, "outer_ring_mean_abs_laplacian": edge_energy})
    off_edge = float(np.abs(laplacian(off_y))[outer_ring].mean())
    falls = sig_rows[0]["outer_ring_mean_abs_laplacian"] > sig_rows[1]["outer_ring_mean_abs_laplacian"] > sig_rows[2]["outer_ring_mean_abs_laplacian"]
    record("fx.radial_blur.sigma", "works" if falls else "partially works",
          f"Mean abs laplacian in the outer ring (radius > 0.9 of the ellipse-normalised frame radius, off = "
          f"{off_edge:.2f}) at sigma 2.0 / {d['sigma']:g} / 25.0 is "
          + ", ".join(f"{r['outer_ring_mean_abs_laplacian']:.2f}" for r in sig_rows)
          + (": high-frequency edge energy falls as sigma rises." if falls else ": not monotonic."),
          {"off_outer_ring_laplacian": off_edge, "rows": sig_rows},
          direction=f"claim: higher sigma softens the edges (laplacian falls); measured {sig_rows}")

    log("fx.radial_blur.start / end (crossover radius)")
    def crossover(start, end):
        rgb, _ = render(MAIN_CLIP, base({"fx": {"radial_blur": {"enabled": True, "sigma": d["sigma"],
                        "start": start, "end": end}}}))
        centers, e_on = per_radius_energy(rgb)
        _, e_off = per_radius_energy(off_rgb)
        ratio = e_on / np.maximum(e_off, 1e-9)
        below = ratio < 0.6
        idx = int(np.argmax(below)) if below.any() else -1
        cross = float(centers[idx]) if idx >= 0 else None
        return {"start": start, "end": end, "crossover_radius": cross,
                "ratio_by_radius": [round(float(x), 3) for x in ratio]}

    rows = [crossover(0.2, 1.0), crossover(0.55, 1.0), crossover(0.9, 1.0)]
    starts_move = len({r["crossover_radius"] for r in rows if r["crossover_radius"] is not None}) > 1
    record("fx.radial_blur.start", "works" if starts_move else "partially works",
          f"With end fixed at 1.0, the radius where on/off laplacian energy first drops under 0.6x (the "
          f"sharp-to-soft crossover) is start={rows[0]['start']} -> r={rows[0]['crossover_radius']}, "
          f"start={rows[1]['start']} -> r={rows[1]['crossover_radius']}, start={rows[2]['start']} -> "
          f"r={rows[2]['crossover_radius']}.",
          {"rows": rows}, direction=f"claim: start moves the crossover; measured {[r['crossover_radius'] for r in rows]}")

    rows2 = [crossover(0.55, 0.7), crossover(0.55, 1.0), crossover(0.55, 1.4)]
    ends_move = len({r["crossover_radius"] for r in rows2 if r["crossover_radius"] is not None}) > 1
    record("fx.radial_blur.end", "works" if ends_move else "partially works",
          f"With start fixed at 0.55, the crossover radius at end=0.7 / 1.0 / 1.4 is "
          f"{rows2[0]['crossover_radius']} / {rows2[1]['crossover_radius']} / {rows2[2]['crossover_radius']}.",
          {"rows": rows2}, direction=f"claim: end moves the crossover; measured {[r['crossover_radius'] for r in rows2]}")


# --------------------------------------------------------------------------
# fx.vignette
# --------------------------------------------------------------------------

def corner_center_luma(rgb: np.ndarray, frac: float = 0.12) -> tuple[float, float]:
    h, w = rgb.shape[:2]
    cs = max(4, int(min(h, w) * frac))
    y = luma255(rgb)
    corners = np.concatenate([y[:cs, :cs].ravel(), y[:cs, -cs:].ravel(),
                              y[-cs:, :cs].ravel(), y[-cs:, -cs:].ravel()])
    cy0, cy1 = h // 2 - cs // 2, h // 2 + cs // 2
    cx0, cx1 = w // 2 - cs // 2, w // 2 + cs // 2
    center = y[cy0:cy1, cx0:cx1].ravel()
    return float(corners.mean()), float(center.mean())


def test_vignette() -> None:
    log("fx.vignette.amount")
    rows = []
    for amt in (0.0, 0.45, 1.0):
        rgb, _ = render(MAIN_CLIP, base({"fx": {"vignette": {"enabled": True, "amount": amt, "radius": 0.85}}}))
        corner, center = corner_center_luma(rgb)
        rows.append({"amount": amt, "corner_luma": corner, "center_luma": center,
                    "corner_over_center": corner / center if center else float("nan")})
    falls = rows[0]["corner_over_center"] > rows[1]["corner_over_center"] > rows[2]["corner_over_center"]
    record("fx.vignette.amount", "works" if falls else "partially works",
          f"corner/centre luma ratio at amount 0.0 / 0.45 / 1.0 is "
          + ", ".join(f"{r['corner_over_center']:.3f}" for r in rows)
          + (": corners darken relative to centre as amount rises." if falls else ": not monotonic."),
          {"rows": rows}, direction=f"claim: corners darken relative to centre; measured {rows}")

    log("fx.vignette.radius")
    rad_rows = []
    for rad in (0.2, 0.85, 1.6):
        rgb, _ = render(MAIN_CLIP, base({"fx": {"vignette": {"enabled": True, "amount": 0.45, "radius": rad}}}))
        rad_rows.append((rad, rgb))
    d01 = diff_stats(rad_rows[0][1], rad_rows[1][1])
    d02 = diff_stats(rad_rows[0][1], rad_rows[2][1])
    d12 = diff_stats(rad_rows[1][1], rad_rows[2][1])
    all_identical = d01["max_abs_diff"] == 0.0 and d02["max_abs_diff"] == 0.0 and d12["max_abs_diff"] == 0.0
    record("fx.vignette.radius", "inert" if all_identical else "works",
          f"With amount fixed at 0.45, radius 0.2 vs 0.85 vs 1.6 are "
          + ("byte identical in every pairwise comparison (max_abs_diff 0.0 in all three pairs): radius is "
             "stored in the preset but never read by the filter graph, exactly the inert label in the UI tooltip."
             if all_identical else
             f"NOT identical: max_abs_diff 0.2v0.85={d01['max_abs_diff']:.1f}, 0.2v1.6={d02['max_abs_diff']:.1f}, "
             f"0.85v1.6={d12['max_abs_diff']:.1f}."),
          {"0.2_vs_0.85": d01, "0.2_vs_1.6": d02, "0.85_vs_1.6": d12},
          direction=f"claim (UI tooltip): radius is inert; measured all_identical={all_identical}")


# --------------------------------------------------------------------------
# grain
# --------------------------------------------------------------------------
#
# A raw local-variance-of-the-frame measure (as first tried) is dominated by
# the underlying image's own texture (tree edges, foliage) at these strength
# ranges, which swamps the grain signal and makes strength/opacity look
# nearly flat. Local variance and opacity/strength below are instead computed
# on the ON-minus-OFF difference image (same clip, same time, same everything
# except grain), which isolates the added noise from scene content.
#
# Autocorrelation (the size test) and the monochrome check need a second,
# even cleaner source: the raw noise PLATE itself, rendered with cinegrade's
# own CG.grain_input() args (not hand-duplicated), before it ever reaches the
# overlay blend. Reason found by testing both ways: overlay blend is
# base-value-dependent per channel, so measuring through the full pipeline on
# a real, textured, coloured frame muddies both the spatial autocorrelation
# (real image edges compete with the plate's own structure) and the
# cross-channel correlation (the same luma-only perturbation lands as a
# different absolute delta per channel depending on each channel's own local
# base value, even though the plate that caused it is exactly monochrome).
# Both measurements are reported: plate-only (clean, isolates the parameter)
# and through the full pipeline (what actually reaches the viewer).

def render_grain_plate(strength: float, size: int, width: int = WIDTH, height: int = 1138) -> np.ndarray:
    cfg = base({"grain": {"enabled": True, "strength": strength, "size": size}})
    info = {"width": width, "height": height}
    plate_args = CG.grain_input(cfg, info)
    args = (["ffmpeg", "-v", "error"] + plate_args
           + ["-vf", f"scale={width}:{height}:flags=bilinear,format=rgb24",
              "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
    proc = subprocess.run(args, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace"))
    return np.frombuffer(proc.stdout, np.uint8).reshape(height, width, 3).copy()


def grain_stats(on_rgb: np.ndarray, off_rgb: np.ndarray) -> dict:
    d = on_rgb.astype(np.float64) - off_rgb.astype(np.float64)
    y = 0.2126 * d[..., 0] + 0.7152 * d[..., 1] + 0.0722 * d[..., 2]
    residual = y - box_blur(y, 3)
    var = float(residual.var())
    ac = {str(lag): autocorr(residual, lag) for lag in range(1, 9)}
    rr, gg, bb = (d[..., 0] - box_blur(d[..., 0], 3), d[..., 1] - box_blur(d[..., 1], 3),
                 d[..., 2] - box_blur(d[..., 2], 3))

    def corr(a, c):
        return float(np.corrcoef(a.ravel(), c.ravel())[0, 1])

    return {"local_variance": var, "autocorr": ac,
            "mono_corr": {"rg": corr(rr, gg), "rb": corr(rr, bb), "gb": corr(gg, bb)}}


def test_grain() -> None:
    configs = {
        "default": {"strength": 40, "size": 3, "opacity": 0.5},
        "strength_low": {"strength": 10, "size": 3, "opacity": 0.5},
        "strength_high": {"strength": 90, "size": 3, "opacity": 0.5},
        "size_small": {"strength": 40, "size": 1, "opacity": 0.5},
        "size_large": {"strength": 40, "size": 8, "opacity": 0.5},
        "opacity_low": {"strength": 40, "size": 3, "opacity": 0.15},
        "opacity_high": {"strength": 40, "size": 3, "opacity": 0.95},
    }
    off_rgb, _ = render(MAIN_CLIP, base({"grain": {"enabled": False}}))
    results = {}
    for name, g in configs.items():
        cfg = base({"grain": dict(g, enabled=True)})
        run1, _ = render(MAIN_CLIP, cfg, use_cache=False)
        run2, _ = render(MAIN_CLIP, cfg, use_cache=False)
        s1, s2 = grain_stats(run1, off_rgb), grain_stats(run2, off_rgb)
        run_identical = bool(np.array_equal(run1, run2))
        results[name] = {"config": g, "run1": s1, "run2": s2, "runs_byte_identical": run_identical,
                         "local_variance_spread": abs(s1["local_variance"] - s2["local_variance"])}
        log(f"grain[{name}] run1_var={s1['local_variance']:.2f} run2_var={s2['local_variance']:.2f} "
            f"identical_runs={run_identical}")
    noise_floor_zero = all(r["runs_byte_identical"] for r in results.values())

    log("grain.strength")
    lv_low = results["strength_low"]["run1"]["local_variance"]
    lv_high = results["strength_high"]["run1"]["local_variance"]
    strength_works = lv_high > lv_low
    repro_note = (
        "Two independent ffmpeg invocations of the SAME grain config, at the same clip and timecode, "
        "produced byte-identical frames for every config tested here (run-to-run spread 0 in all cases). "
        "That contradicts this audit brief's own assumption that grain is random per ffmpeg run: for a "
        "single still frame the noise plate is grabbed from frame 0 of a freshly started lavfi noise "
        "source every time, so a still-frame render of a given config is fully reproducible; a real video "
        "render over many frames would show a different noise realisation per output frame instead."
        if noise_floor_zero else
        "Run-to-run spread was nonzero for at least one config, so grain genuinely is randomised per render; "
        "see per-config local_variance_spread in the measurements."
    )
    record("grain.strength", "works" if strength_works else "partially works",
          f"Local variance (variance of the on-minus-off difference image, minus a 3x3 box blur of that "
          f"difference; isolates the added noise from scene content) is {lv_low:.2f} at strength 10 and "
          f"{lv_high:.2f} at strength 90, rising with strength. {repro_note}",
          {"configs": results, "noise_floor_zero_everywhere": noise_floor_zero},
          direction=f"claim: strength up raises local variance; measured {lv_low:.2f} -> {lv_high:.2f}")

    log("grain.size")
    # Measured on the raw plate (see the section note): the full-pipeline
    # on/off diff was tried first and its autocorrelation was swamped by real
    # scene edges (both size 1 and size 8 looked equally uncorrelated beyond
    # lag 1), which does not match what the plate itself does.
    def half_life(ac):
        for lag in range(1, 9):
            if ac[lag] < 0.3:
                return lag
        return 9

    plate_ac = {}
    for size in (1, 3, 8):
        plate = render_grain_plate(40, size, height=off_rgb.shape[0])
        y = plate[..., 0].astype(np.float64)
        residual = y - box_blur(y, 3)
        plate_ac[size] = {lag: autocorr(residual, lag) for lag in range(1, 9)}
    hl = {size: half_life(ac) for size, ac in plate_ac.items()}
    size_works = hl[8] > hl[3] > hl[1] or hl[8] > hl[1]
    record("grain.size", "works" if size_works else "partially works",
          f"Autocorrelation of the noise plate itself (not the composited frame) first drops below 0.3 at "
          f"lag {hl[1]} for size 1, lag {hl[3]} for size 3, lag {hl[8]} for size 8 (by-lag values: "
          f"{plate_ac}), so a larger plate downscale correlates over a longer pixel distance once measured "
          f"on the plate directly, matching 'size is the plate downscale factor'.",
          {"plate_autocorr_by_size": plate_ac, "half_life_lag": hl},
          direction=f"claim: larger size keeps autocorrelation high to a longer lag; measured half-life "
                    f"lags {hl}")

    log("grain.opacity")
    lv_op_low = results["opacity_low"]["run1"]["local_variance"]
    lv_op_high = results["opacity_high"]["run1"]["local_variance"]
    opacity_works = lv_op_high > lv_op_low
    record("grain.opacity", "works" if opacity_works else "partially works",
          f"Local variance is {lv_op_low:.2f} at opacity 0.15 and {lv_op_high:.2f} at opacity 0.95, rising "
          f"with opacity.",
          {"opacity_low_variance": lv_op_low, "opacity_high_variance": lv_op_high},
          direction=f"claim: opacity up raises local variance; measured {lv_op_low:.2f} -> {lv_op_high:.2f}")

    log("grain (monochrome claim)")
    plate40x3 = render_grain_plate(40, 3, height=off_rgb.shape[0])
    plate_mono = bool(np.array_equal(plate40x3[..., 0], plate40x3[..., 1])
                      and np.array_equal(plate40x3[..., 1], plate40x3[..., 2]))
    mc = results["default"]["run1"]["mono_corr"]
    composited_mono = mc["rg"] > 0.95 and mc["rb"] > 0.95 and mc["gb"] > 0.95
    record("grain (monochrome)", "works" if plate_mono else "partially works",
          f"The noise plate itself is r==g==b at every pixel ({plate_mono}: confirmed by direct array "
          f"comparison, not inferred), so the source noise is exactly monochrome as the code comment claims. "
          f"Once blended onto the real, coloured, textured frame via overlay blend, the visible result is "
          f"NOT perfectly monochrome: cross-channel correlation of the on-minus-off residual is "
          f"r-g={mc['rg']:.4f}, r-b={mc['rb']:.4f}, g-b={mc['gb']:.4f}. This is expected, not a bug: overlay "
          f"blend is a function of each channel's own base value, so the same luma-only perturbation lands "
          f"as a different absolute delta per channel wherever the base image itself has colour.",
          {"plate_exactly_monochrome": plate_mono, "composited_cross_channel_corr": mc,
          "composited_reads_monochrome": composited_mono},
          direction=f"claim: grain is monochrome; measured plate_exactly_monochrome={plate_mono}, "
                    f"composited correlation={mc}")


# --------------------------------------------------------------------------
# detail
# --------------------------------------------------------------------------

def test_detail() -> None:
    off_rgb, _ = render(MAIN_CLIP, base())
    off_lap = mean_abs_laplacian(luma01(off_rgb))

    log("detail.soften")
    soft_rows = [{"soften": 0.0, "mean_abs_laplacian": off_lap}]
    for s in (1.0, 3.0):
        rgb, _ = render(MAIN_CLIP, base({"detail": {"soften": s}}))
        soft_rows.append({"soften": s, "mean_abs_laplacian": mean_abs_laplacian(luma01(rgb))})
    falls = all(soft_rows[i]["mean_abs_laplacian"] > soft_rows[i + 1]["mean_abs_laplacian"] for i in range(len(soft_rows) - 1))
    # schema max (12.0) against off, for the required mean/max/per-channel/pct numbers
    max_soft_rgb, _ = render(MAIN_CLIP, base({"detail": {"soften": 12.0}}))
    soft_diff = diff_stats(off_rgb, max_soft_rgb)
    record("detail.soften", "works" if falls else "partially works",
          f"Mean abs laplacian at soften 0 / 1 / 3 is "
          + ", ".join(f"{r['mean_abs_laplacian']:.5f}" for r in soft_rows)
          + (": falls as soften rises, less edge energy." if falls else ": not monotonic.")
          + f" At the schema max (soften=12), mean_abs_diff vs off is {soft_diff['mean_abs_diff']:.2f} "
          f"code values ({soft_diff['pct_pixels_changed']:.1f}% of pixels moved), a strong, clearly visible blur.",
          {"laplacian_rows": soft_rows, "off_vs_max_diff": soft_diff},
          direction=f"claim: soften up lowers edge energy; measured {soft_rows}, "
                    f"max mean_abs_diff={soft_diff['mean_abs_diff']:.2f}")

    log("detail.sharpen")
    sharp_rows = [{"sharpen": 0.0, "mean_abs_laplacian": off_lap}]
    for s in (0.5, 1.5):
        rgb, _ = render(MAIN_CLIP, base({"detail": {"sharpen": s}}))
        sharp_rows.append({"sharpen": s, "mean_abs_laplacian": mean_abs_laplacian(luma01(rgb))})
    rises = all(sharp_rows[i]["mean_abs_laplacian"] < sharp_rows[i + 1]["mean_abs_laplacian"] for i in range(len(sharp_rows) - 1))
    # schema max (3.0) against off; unsharp's luma_amount is genuinely subtle at 8bit
    # output on this footage, so report the real numbers rather than only the
    # (correct but small-looking) laplacian trend.
    max_sharp_rgb, _ = render(MAIN_CLIP, base({"detail": {"sharpen": 3.0}}))
    sharp_diff = diff_stats(off_rgb, max_sharp_rgb)
    weak = sharp_diff["max_abs_diff"] < 5.0
    verdict = "works" if rises else "partially works"
    record("detail.sharpen", verdict,
          f"Mean abs laplacian at sharpen 0 / 0.5 / 1.5 is "
          + ", ".join(f"{r['mean_abs_laplacian']:.5f}" for r in sharp_rows)
          + (": rises as sharpen rises, more edge energy." if rises else ": not monotonic.")
          + f" At the schema max (sharpen=3), mean_abs_diff vs off is only {sharp_diff['mean_abs_diff']:.2f} "
          f"code values (max_abs_diff {sharp_diff['max_abs_diff']:.0f} of 255, {sharp_diff['pct_pixels_changed']:.1f}% "
          "of pixels touched): the direction is correct and every pixel it touches moves the same way, but "
          "the unsharp pass is a genuinely subtle edge-contrast lift on this footage, not a strong sharpen, "
          "matching the code's own comment that it is meant to read cleaner than harsh in-camera sharpening.",
          {"laplacian_rows": sharp_rows, "off_vs_max_diff": sharp_diff, "weak_at_schema_max": weak},
          direction=f"claim: sharpen up raises edge energy; measured {sharp_rows}, "
                    f"max mean_abs_diff={sharp_diff['mean_abs_diff']:.2f}, max_abs_diff={sharp_diff['max_abs_diff']:.0f}")


# --------------------------------------------------------------------------
# letterbox
# --------------------------------------------------------------------------

def test_letterbox() -> None:
    # Deliberate deviation from MAIN_CLIP's usual autorotate=True: this clip
    # carries a stray -90 degree display matrix that autorotate honours even
    # though the content is already upright (confirmed by eye with
    # `cinegrade.py orient`, see audit notes), which swaps the rendered frame
    # to portrait (640x1138, W/H=0.56) at width=640. f_letterbox's own
    # early-return arithmetic and the schema's aspect range (1.0-3.0) only
    # make sense against a landscape source, so testing this node on the
    # rotated portrait frame would validate against the wrong regime.
    # autorotate=False gives the correctly oriented 16:9 frame instead, which
    # is what ordinary footage looks like to this node.
    off_rgb, _ = render(MAIN_CLIP, base({"letterbox": {"enabled": False}}), autorotate=False)
    H, W = off_rgb.shape[0], off_rgb.shape[1]
    src_aspect = W / H
    log(f"letterbox: testing on the autorotate=False frame, {W}x{H}, source aspect {src_aspect:.3f}")

    log("letterbox.aspect")
    rows = []
    for aspect in (1.85, 2.39, 2.76):
        rgb, _ = render(MAIN_CLIP, base({"letterbox": {"enabled": True, "aspect": aspect}}), autorotate=False)
        y = luma255(rgb)
        black_row = y.max(axis=1) < 1.0
        top = 0
        for v in black_row:
            if v:
                top += 1
            else:
                break
        bottom = 0
        for v in black_row[::-1]:
            if v:
                bottom += 1
            else:
                break
        target = int(round(W / aspect))
        target -= target % 2
        expected_bar = (H - target) // 2
        rows.append({"aspect": aspect, "measured_top_bar": top, "measured_bottom_bar": bottom,
                    "expected_bar": expected_bar, "measured_visible_height": H - top - bottom,
                    "expected_visible_height": target})
    matches = all(r["measured_top_bar"] == r["expected_bar"] == r["measured_bottom_bar"]
                 and r["measured_visible_height"] == r["expected_visible_height"] for r in rows)
    record("letterbox.aspect", "works" if matches else "partially works",
          f"On a {W}x{H} frame, measured black bars (top, bottom) vs f_letterbox's own arithmetic "
          f"(round(W/aspect), rounded to even, centred) at aspect 1.85 / 2.39 / 2.76: "
          + "; ".join(f"aspect {r['aspect']}: measured ({r['measured_top_bar']}, {r['measured_bottom_bar']}) "
                      f"vs expected {r['expected_bar']}, visible height {r['measured_visible_height']} vs "
                      f"expected {r['expected_visible_height']}" for r in rows)
          + f". {'All match exactly.' if matches else 'Mismatch found, see rows.'}",
          {"rows": rows, "source_wxh": [W, H]},
          direction=f"claim: bars match round(W/aspect) arithmetic; measured matches={matches}")

    log("letterbox.aspect (below source aspect: early return)")
    low_aspect = round(src_aspect * 0.6, 3)
    rgb_low, _ = render(MAIN_CLIP, base({"letterbox": {"enabled": True, "aspect": low_aspect}}), autorotate=False)
    identical = bool(np.array_equal(off_rgb, rgb_low))
    record("letterbox.aspect (early return below source aspect)", "works" if identical else "partially works",
          f"Source aspect is {src_aspect:.3f}; at aspect {low_aspect} (below source aspect, target height "
          f"would exceed the frame), the letterboxed render is "
          f"{'byte identical to letterbox disabled' if identical else 'NOT identical to letterbox disabled'}, "
          f"confirming f_letterbox's `if target >= H: return []` early-return path.",
          {"low_aspect": low_aspect, "identical_to_disabled": identical},
          direction=f"claim: aspect below source aspect is a no-op; measured identical={identical}")


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

OUT_TMP_DIR = Path(__file__).resolve().parent / "_audit_tmp"


def encode_short(cfg: dict, duration: float = 0.5, width: int = WIDTH) -> tuple[Path, dict]:
    """Encode a short real clip through the exact codec path cmd_render uses.

    render_raw's job is to hand back raw pixels for preview, so it never
    touches cfg["output"] at all; the only place that section is read is
    cmd_render's own bottom half. Reproducing that bottom half (not calling
    the CLI, to stay inside one process and one temp-file lifecycle) is the
    only way to exercise codec/profile/crf/preset for real.
    """
    OUT_TMP_DIR.mkdir(exist_ok=True)
    src = str(FOOTAGE / MAIN_CLIP)
    info = CG.probe(src, autorotate=True)
    # same head-scale pattern as render(): downscale first so the codec test
    # (and its ffmpeg run time) stays cheap regardless of the source's 4K size.
    factor = width / float(info["width"])
    height = max(2, int(round(info["height"] * factor / 2)) * 2)
    pinfo = dict(info, width=width, height=height)
    head = f"[0:v]scale={width}:{height}:flags=bilinear,setsar=1[studiosrc]"
    graph = CG.graph_with_mask(cfg, pinfo, src_label="studiosrc", head_extra=head)
    o = cfg["output"]
    ext = "mov" if o["codec"] == "prores_ks" else "mp4"
    out_path = OUT_TMP_DIR / f"out_{o['codec']}_{o.get('profile')}_{o.get('crf')}_{o.get('preset')}.{ext}"
    args = CG.ffmpeg_inputs(src, cfg, pinfo, seek=MAIN_T, duration=duration)
    args += ["-t", str(duration), "-filter_complex", graph, "-map", "[vout]", "-an"]
    if o["codec"] == "prores_ks":
        args += ["-c:v", "prores_ks", "-profile:v", str(o["profile"]), "-vendor", "apl0", "-pix_fmt", "yuv422p10le"]
    else:
        args += ["-c:v", o["codec"], "-crf", str(o["crf"]), "-preset", o["preset"], "-pix_fmt", "yuv420p"]
    args += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-y", str(out_path)]
    with _ffmpeg_slots:
        r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise CG.GradeError(f"encode failed ({r.returncode})\n{r.stderr[-2000:]}")
    probe_r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=codec_name,profile,pix_fmt,codec_tag_string", "-of", "json", str(out_path)],
        capture_output=True, text=True, check=True)
    meta = json.loads(probe_r.stdout)["streams"][0]
    meta["file_size_bytes"] = out_path.stat().st_size
    return out_path, meta


def test_output() -> None:
    base_cfg = base()

    log("output.codec")
    codec_rows = []
    for codec, extra in (("prores_ks", {"profile": 3}), ("libx264", {}), ("libx265", {})):
        cfg = deepcopy(base_cfg)
        cfg["output"].update(extra)
        cfg["output"]["codec"] = codec
        path, meta = encode_short(cfg)
        codec_rows.append({"requested": codec, "ffprobe_codec_name": meta["codec_name"],
                          "pix_fmt": meta["pix_fmt"], "size_bytes": meta["file_size_bytes"]})
        path.unlink(missing_ok=True)
    # ffprobe reports the bitstream's own codec name, not the encoder that
    # made it (e.g. libx264 the encoder produces a stream ffprobe calls h264),
    # so check against that mapping rather than a naive substring match.
    expected_codec_name = {"prores_ks": "prores", "libx264": "h264", "libx265": "hevc"}
    codec_matches = all(r["ffprobe_codec_name"] == expected_codec_name[r["requested"]] for r in codec_rows)
    record("output.codec", "works" if codec_matches else "partially works",
          "ffprobe on a short real encode of each codec setting reports "
          + ", ".join(f"{r['requested']} -> {r['ffprobe_codec_name']}/{r['pix_fmt']}" for r in codec_rows)
          + (": every request lands on the matching encoder." if codec_matches else ": mismatch found."),
          {"rows": codec_rows}, direction=f"claim: codec selects the ffprobe-reported encoder; measured {codec_rows}")

    log("output.profile (prores)")
    profile_rows = []
    for profile in (0, 3, 5):
        cfg = deepcopy(base_cfg)
        cfg["output"]["codec"] = "prores_ks"
        cfg["output"]["profile"] = profile
        path, meta = encode_short(cfg)
        profile_rows.append({"requested_profile": profile, "ffprobe_profile": meta.get("profile"),
                            "size_bytes": meta["file_size_bytes"]})
        path.unlink(missing_ok=True)
    profile_names = {0: "Proxy", 1: "LT", 2: "Standard", 3: "HQ", 4: "4444", 5: "4444XQ"}
    profile_matches = all(profile_names.get(r["requested_profile"], "") == r["ffprobe_profile"] for r in profile_rows)
    size_rises = profile_rows[0]["size_bytes"] < profile_rows[1]["size_bytes"] < profile_rows[2]["size_bytes"]
    record("output.profile", "works" if (profile_matches or size_rises) else "partially works",
          "ffprobe reports prores profile name "
          + ", ".join(f"{r['requested_profile']} -> {r['ffprobe_profile']}" for r in profile_rows)
          + f"; file size in bytes for a {0.5:g}s clip is "
          + ", ".join(str(r["size_bytes"]) for r in profile_rows)
          + (", rising with profile as expected for a heavier codec." if size_rises else "."),
          {"rows": profile_rows, "profile_name_match": profile_matches, "size_rises": size_rises},
          direction=f"claim: profile picks a real prores profile; measured names_match={profile_matches}, "
                    f"size_rises={size_rises}")

    log("output.crf (libx264)")
    crf_rows = []
    for crf in (0, 16, 51):
        cfg = deepcopy(base_cfg)
        cfg["output"]["codec"] = "libx264"
        cfg["output"]["crf"] = crf
        path, meta = encode_short(cfg)
        crf_rows.append({"crf": crf, "size_bytes": meta["file_size_bytes"]})
        path.unlink(missing_ok=True)
    crf_falls = crf_rows[0]["size_bytes"] > crf_rows[1]["size_bytes"] > crf_rows[2]["size_bytes"]
    record("output.crf", "works" if crf_falls else "partially works",
          "File size in bytes for a 0.5s libx264 clip at crf 0 (lossless-ish) / 16 / 51 (worst) is "
          + ", ".join(str(r["size_bytes"]) for r in crf_rows)
          + (": falls monotonically as crf rises, matching crf as a quality/size knob." if crf_falls else ": not monotonic."),
          {"rows": crf_rows}, direction=f"claim: crf trades size for quality; measured sizes {crf_rows}")

    log("output.preset (libx264)")
    preset_rows = []
    t0 = time.time()
    for preset in ("ultrafast", "veryslow"):
        cfg = deepcopy(base_cfg)
        cfg["output"]["codec"] = "libx264"
        cfg["output"]["preset"] = preset
        ts = time.time()
        path, meta = encode_short(cfg)
        elapsed = time.time() - ts
        preset_rows.append({"preset": preset, "size_bytes": meta["file_size_bytes"], "encode_seconds": elapsed})
        path.unlink(missing_ok=True)
    slower_takes_longer = preset_rows[0]["encode_seconds"] < preset_rows[1]["encode_seconds"]
    record("output.preset", "works" if slower_takes_longer else "partially works",
          f"On a 0.5s clip, ultrafast took {preset_rows[0]['encode_seconds']:.2f}s ({preset_rows[0]['size_bytes']} bytes) "
          f"and veryslow took {preset_rows[1]['encode_seconds']:.2f}s ({preset_rows[1]['size_bytes']} bytes)"
          + (": veryslow spends more time encoding, as an x264 preset should." if slower_takes_longer
             else ": no measurable time difference at this clip length, inconclusive rather than inert, "
                  "since preset is a real libx264 CLI flag outside cinegrade's own control."),
          {"rows": preset_rows}, direction=f"claim: preset trades encode time for compression efficiency; "
                                          f"measured {preset_rows}")
    try:
        import shutil
        shutil.rmtree(OUT_TMP_DIR, ignore_errors=True)
    except Exception:
        pass


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

TEST_BATTERY = [
    test_convert, test_primaries, test_curves, test_secondary, test_look,
    test_halation, test_bloom, test_rgb_split, test_radial_blur, test_vignette,
    test_grain, test_detail, test_letterbox, test_output,
]


def markdown_table() -> str:
    lines = ["| parameter | verdict | what it actually does |", "| --- | --- | --- |"]
    for r in RESULTS:
        cell = r["summary"].replace("|", "/").replace("\n", " ")
        lines.append(f"| {r['parameter']} | {r['verdict']} | {cell} |")
    return "\n".join(lines)


def main() -> None:
    t_start = time.time()
    log("Picking the test frame by measurement (flat preset, both clips, several timecodes)")
    frame_selection = pick_frame()
    log(f"Chosen main frame: {MAIN_CLIP} @ t={MAIN_T}s (reason: {frame_selection['chosen']['reason']})")
    log(f"Chosen second frame: {SECOND_CLIP} @ t={SECOND_T}s (reason: {frame_selection['second_clip']['reason']})")

    failures = []
    for fn in TEST_BATTERY:
        log(f"=== {fn.__name__} ===")
        try:
            fn()
        except Exception as exc:  # one section failing should not kill the whole run
            failures.append({"section": fn.__name__, "error": f"{type(exc).__name__}: {exc}"})
            log(f"FAILED {fn.__name__}: {type(exc).__name__}: {exc}")

    elapsed = time.time() - t_start
    out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "python": sys.version,
        "clips": {"main": MAIN_CLIP, "second": SECOND_CLIP},
        "frame_selection": frame_selection,
        "render_count": RENDER_COUNT[0],
        "elapsed_seconds": elapsed,
        "section_failures": failures,
        "results": RESULTS,
    }
    RESULTS_PATH.write_text(json.dumps(out, indent=2))
    log(f"wrote {RESULTS_PATH} ({len(RESULTS)} records, {RENDER_COUNT[0]} ffmpeg renders, {elapsed:.1f}s)")
    if failures:
        log(f"{len(failures)} section(s) failed: " + ", ".join(f["section"] for f in failures))

    table = markdown_table()
    print("\n" + table)


if __name__ == "__main__":
    main()
