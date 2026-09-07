#!/usr/bin/env python3
"""match_ref - move a source frame toward the colour of a reference image.

The honest version of Resolve's Shot Match.

A reference image does NOT contain the LUT that produced it. Nothing here
recovers a grade. All this does is measure the colour distribution of a
reference photograph and the colour distribution of one frame of your footage,
then fit a smooth, well behaved transform that drags the second distribution
toward the first, and bake that transform into a .cube.

That inference is only meaningful when the two images have broadly comparable
content. A green forest reference dropped onto a shot dominated by a red car
will pull the whole frame green, because "the reference has more green in it"
is literally the only signal available. The tool measures how different the two
hue distributions are and says so, with numbers, before you trust the result.

Two matching methods:

    reinhard   mean and standard deviation transfer per axis in the
               decorrelated l-alpha-beta space (Reinhard et al. 2001).
               A global affine move. Robust, rarely catastrophic, weaker.
    histogram  per channel cumulative histogram matching in display code.
               Monotonic per channel so it cannot invert a hue, stronger,
               and it will posterise if you let it. Slope limited and
               smoothed here so it does not.

Domain, which is the part that invalidates everything if you get it wrong:
the reference is a display referred Rec.709 image, so the source is measured
AFTER the CST out, at exactly the point in the node tree where the LOOK LUT
is applied (look LUT off, FX and grain off, everything upstream of LOOK on).
Never against raw log.

    python match_ref.py detect ../../refs/*.PNG
    python match_ref.py match --ref ../../refs/IMG_2570.PNG \
        --clip ../../footage/A001_09011832_C003.MOV --time 8 --no-autorotate \
        --method histogram --strength 0.8 --luma-preserve
"""

from __future__ import annotations

import argparse
import json
import struct
import subprocess
import sys
import time as _time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent          # content/grade
CONTENT = ROOT.parent                                  # content
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
import colorlib as C                                   # noqa: E402
import cinegrade                                       # noqa: E402  (read only)
from stats import bands as _bands                      # noqa: E402  (read only)

LUT_LOOKS = ROOT / "luts" / "looks"

# Display P3 and Rec.709 are both D65, so this matrix is a pure primaries
# change and the chromatic adaptation is a no-op. It matters anyway: an iPhone
# screenshot tagged P3 carries code values that mean MORE saturation than the
# same numbers in Rec.709, and matching them raw hands the source a saturation
# push that the reference photograph never had.
import colour                                          # noqa: E402
P3_TO_REC709 = colour.matrix_RGB_to_RGB(
    colour.RGB_COLOURSPACES["Display P3"], C.REC709, "CAT02")

# Hue family boundaries in degrees. Warm wraps through 0.
FAMILIES = (
    ("warm", 330.0, 75.0),
    ("green", 75.0, 165.0),
    ("cool", 165.0, 285.0),
    ("magenta", 285.0, 330.0),
)
# Below this saturation a pixel has no hue worth counting, so it goes to
# "neutral" instead of being assigned to whatever family the noise picked.
NEUTRAL_SAT = 0.10

# Blown neutral pixels (white UI text, a clipped specular) carry no colour and
# are exactly what the Instagram chrome contributes. Rejected from BOTH
# distributions so the two sides are always treated identically.
BLOWN_SAT = 0.08
BLOWN_LUMA = 0.95

# Probe colours the output must not break.
PROBES = {
    "maroon": (0.25, 0.08, 0.10),
    "skin": (0.55, 0.40, 0.32),
    "grey18": (0.39, 0.39, 0.39),
    "sky": (0.35, 0.52, 0.72),
    "foliage": (0.22, 0.34, 0.16),
}


class MatchError(Exception):
    """A user-fixable problem: a missing file, an impossible crop, a bad clip."""


# --------------------------------------------------------------------------
# image IO. No PIL in this venv, so every pixel comes through ffmpeg rawvideo.
# --------------------------------------------------------------------------

def _run_raw(args: list[str]) -> bytes:
    r = subprocess.run(args, capture_output=True)
    if r.returncode != 0:
        raise MatchError(f"ffmpeg failed ({r.returncode})\n"
                         f"{r.stderr.decode('utf-8', 'replace')[-3000:]}")
    return r.stdout


def _image_size(path: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True).stdout.strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


def read_image(path: Path) -> np.ndarray:
    """Decode any still to float RGB in [0, 1], shape (h, w, 3).

    rgb48le rather than rgb24 because the references are 16 bit PNGs and the
    statistics are the whole product here. One extra quantisation is free to
    avoid.
    """
    w, h = _image_size(path)
    buf = _run_raw(["ffmpeg", "-v", "error", "-i", str(path),
                    "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"])
    want = w * h * 3
    a = np.frombuffer(buf, dtype="<u2")
    if a.size != want:
        raise MatchError(f"{path.name}: expected {want} samples, got {a.size}")
    return (a.reshape(h, w, 3).astype(np.float32) / 65535.0)


def png_gamut(path: Path) -> str:
    """Read the PNG's own colour tagging: 'p3' or 'srgb'.

    iPhone screenshots are a mix. Five of the seven references here are tagged
    Display P3 and two are sRGB, from the same phone on the same day, so
    assuming either one for the whole folder is wrong for at least two images.
    """
    try:
        data = path.read_bytes()[:1 << 20]
    except OSError:
        return "srgb"
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return "srgb"
    i = 8
    while i + 12 <= len(data):
        length = struct.unpack(">I", data[i:i + 4])[0]
        typ = data[i + 4:i + 8]
        body = data[i + 8:i + 8 + min(length, 64)]
        if typ == b"iCCP" and b"P3" in body:
            return "p3"
        if typ == b"cICP" and len(body) >= 1 and body[0] == 12:
            return "p3"          # colour primaries 12 is P3-D65
        if typ in (b"IDAT", b"IEND"):
            break
        i += 12 + length
    return "srgb"


def decode_gamma22(x: np.ndarray) -> np.ndarray:
    return np.clip(x, 0.0, 1.0) ** 2.2


def decode_rec709_oetf(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return np.where(x < 0.081, x / 4.5, ((x + 0.099) / 1.099) ** (1 / 0.45))


def srgb_decode(x: np.ndarray) -> np.ndarray:
    """Inverse of colorlib.encode_srgb. Not in colorlib, so it lives here."""
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


# The exact inverse of every encoder colorlib offers. Guessing a power law for
# the two non power law encodes would put the Reinhard fit in a slightly wrong
# linear domain, which shows up as a contrast error nobody can trace.
DECODERS = {
    "gamma24": C.decode_gamma24,
    "rec709a": decode_gamma22,
    "rec709": decode_rec709_oetf,
    "srgb": srgb_decode,
}


def load_reference(path: Path, gamut: str = "auto",
                   encode: str = "rec709a") -> tuple[np.ndarray, str]:
    """Reference PNG -> the same display encoding the engine's CST out emits.

    Both P3 and sRGB tagged PNGs use the sRGB transfer function, so the decode
    is the same and only the primaries differ. Re-encoding with the preset's
    own `convert.encode` is what puts the two sides in one comparable domain;
    skipping it compares a 2.2 power law against a 2.4 one and reads as a
    contrast difference that is not really there.
    """
    rgb = read_image(path)
    g = png_gamut(path) if gamut == "auto" else gamut
    lin = srgb_decode(rgb)
    if g == "p3":
        lin = np.clip(C.apply_matrix(lin, P3_TO_REC709), 0.0, 1.0)
    enc = C.ENCODERS.get(encode, C.encode_gamma22)
    return enc(lin).astype(np.float32), g


# --------------------------------------------------------------------------
# source frame, sampled exactly where the LOOK LUT will be applied
# --------------------------------------------------------------------------

def _measure_cfg(preset: str | None) -> dict:
    """The preset with everything downstream of LOOK switched off.

    FX, grain, detail and letterbox all run AFTER the look node, so the LUT
    never sees their output. Measuring with them on would fit the transform to
    a vignette and a grain plate.
    """
    cfg = cinegrade.load_preset(preset)
    cfg["look"]["lut"] = None
    for k, v in cfg["fx"].items():
        if isinstance(v, dict) and "enabled" in v:
            v["enabled"] = False
    cfg["grain"]["enabled"] = False
    cfg["letterbox"]["enabled"] = False
    cfg["detail"] = {"soften": 0.0, "sharpen": 0.0}
    return cfg


def load_source_frame(clip: Path, seconds: float, preset: str | None,
                      autorotate: bool, width: int,
                      rotation=None) -> tuple[np.ndarray, dict]:
    cfg = _measure_cfg(preset)
    # rotation is the full setting (auto, 0, 90, 180, 270); autorotate is its
    # old two-value form and still works. graph_with_mask adds the transpose
    # itself here, since this call passes no head_extra of its own.
    info = cinegrade.probe(str(clip), autorotate=autorotate, rotation=rotation)
    sw = min(width, info["width"])
    sh = int(round(info["height"] * sw / info["width"] / 2)) * 2
    graph = cinegrade.graph_with_mask(
        cfg, info, encode_out=False,
        tail_extra=[f"scale={sw}:{sh}:flags=bicubic", "format=rgb48le"])
    args = cinegrade.ffmpeg_inputs(str(clip), cfg, info, seek=seconds)
    args += ["-filter_complex", graph, "-map", "[vout]", "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"]
    buf = _run_raw(args)
    a = np.frombuffer(buf, dtype="<u2")
    if a.size != sw * sh * 3:
        raise MatchError(f"source frame: expected {sw * sh * 3} samples, "
                         f"got {a.size} (is --time past the end of the clip?)")
    return a.reshape(sh, sw, 3).astype(np.float32) / 65535.0, cfg


def apply_cube(rgb: np.ndarray, cube: Path) -> np.ndarray:
    """Push an image through the real ffmpeg lut3d, not through a python copy.

    The "after" numbers have to include the 33 grid plus tetrahedral
    interpolation the engine will actually run, otherwise the validation is
    measuring a transform that never ships.
    """
    h, w = rgb.shape[:2]
    src = (np.clip(rgb, 0.0, 1.0) * 65535.0 + 0.5).astype("<u2").tobytes()
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb48le",
         "-s", f"{w}x{h}", "-i", "pipe:0",
         "-vf", f"format=gbrp16le,lut3d=file={cinegrade.esc(cube)}"
                f":interp=tetrahedral,format=rgb48le",
         "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"],
        input=src, capture_output=True)
    if r.returncode != 0:
        raise MatchError(f"lut3d apply failed\n{r.stderr.decode()[-2000:]}")
    a = np.frombuffer(r.stdout, dtype="<u2")
    return a.reshape(h, w, 3).astype(np.float32) / 65535.0


# --------------------------------------------------------------------------
# content region detection
# --------------------------------------------------------------------------

def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    pad = np.concatenate(([0], mask.astype(np.int8), [0]))
    idx = np.flatnonzero(np.diff(pad))
    return list(zip(idx[0::2], idx[1::2]))


def _sat(rgb: np.ndarray) -> np.ndarray:
    mx = rgb.max(-1)
    mn = rgb.min(-1)
    return np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0)


# The UI chrome detector's mask. Deliberately tight: Instagram's icons,
# counts and captions are pure white with a drop shadow, so luma over 0.90 at
# almost no saturation separates them from a bright photograph. At the looser
# (0.78, 0.22) a sunlit sky or a white ceiling scores the same as the icon
# column and the detector trims the photograph instead of the chrome.
GLYPH_LUMA = 0.90
GLYPH_SAT = 0.10


def _smooth1d(v: np.ndarray, k: int) -> np.ndarray:
    k = max(3, k | 1)
    return np.convolve(v, np.ones(k) / k, mode="same")


def detect_content_region(rgb: np.ndarray) -> dict:
    """Find the photograph inside an Instagram screenshot.

    Three separate things get thrown away, and they need three different
    tests, because they do not look alike:

      letterbox bars   nearly every pixel in the row is black
      flat UI panels   the comment bar is dark grey, NOT black, so its
                       giveaway is that the row has almost no variance
      overlaid chrome  the like/comment column, the caption, the username row.
                       These sit ON the video, so no row or column of them is
                       flat. They are found as a concentration of pure white
                       glyph pixels measured against the median concentration
                       of the photograph itself, which makes the test relative
                       to each image instead of a fixed threshold.

    Chrome is only ever trimmed inward from an edge, and never by more than a
    quarter of the region per side, because a creator's own big white title
    text is indistinguishable from UI text to any measure this cheap. Losing
    some sampling area is nearly free; keeping the black bars or the white UI
    poisons every statistic downstream, which is the whole product.
    """
    h, w = rgb.shape[:2]
    y = C.luma709(rgb)[..., 0]
    s = _sat(rgb)
    glyph = (y > GLYPH_LUMA) & (s < GLYPH_SAT)
    # The analysis band skips the right icon column and both edges, so the
    # vertical search is not quietly answering a question about the chrome.
    bx0, bx1 = int(0.04 * w), int(0.80 * w)
    yb = y[:, bx0:bx1]

    row_dark = (yb < 0.045).mean(1)
    row_std = yb.std(1)
    row_mean = yb.mean(1)
    bar = (row_dark > 0.90) | ((row_std < 0.020) & (row_mean < 0.22))
    runs = [(a, b) for a, b in _runs(~bar) if (b - a) >= 0.08 * h]
    y0, y1 = max(runs, key=lambda r: r[1] - r[0]) if runs else (0, h)
    notes = []

    # --- right hand action column -----------------------------------------
    col_g = _smooth1d(glyph[y0:y1].mean(0), int(0.01 * w))
    base_col = float(np.median(col_g[int(0.05 * w):int(0.70 * w)]))
    right = col_g[int(0.78 * w):]
    peak = float(right.max()) if right.size else 0.0
    x0, x1 = 0, w
    if peak > max(0.015, 3.0 * base_col):
        # walk left off the peak to the edge of its support, which is the left
        # edge of the icon strip
        i = int(0.78 * w) + int(right.argmax())
        floor = max(0.006, 0.25 * peak)
        while i > int(0.70 * w) and col_g[i] > floor:
            i -= 1
        x1 = max(int(0.70 * w), i - int(0.01 * w))
        notes.append(f"icon column trimmed at x={x1 / w:.3f} "
                     f"(peak {peak:.3f} vs baseline {base_col:.3f})")
    else:
        notes.append(f"no icon column found (peak {peak:.3f} vs baseline "
                     f"{base_col:.3f}); the reference has bright neutral "
                     f"content of its own, so the strip is left in")

    # --- caption and header blocks, trimmed inward from each edge ----------
    # No "but the row is colourful" guard here on purpose. It was tried and it
    # is wrong: a caption is a thin ribbon of text over the photograph, so its
    # row is exactly as colourful as the rows around it, and the guard vetoed
    # every real caption. The quarter-per-side cap below is the safety net
    # instead, which fails by keeping too much photo rather than by keeping UI.
    row_g = glyph[:, bx0:bx1].mean(1)
    base_row = float(np.median(row_g[y0:y1]))
    thr_row = max(0.010, 4.0 * base_row)
    chrome = row_g > thr_row
    margin = int(0.008 * h)
    quarter = int(0.25 * (y1 - y0))
    top_hits = np.flatnonzero(chrome[y0:y0 + quarter])
    if top_hits.size:
        y0 = min(y0 + int(top_hits[-1]) + margin, y0 + quarter)
        notes.append(f"header/status chrome trimmed to y={y0 / h:.3f}")
    bot_hits = np.flatnonzero(chrome[y1 - quarter:y1])
    if bot_hits.size:
        y1 = max(y1 - quarter + int(bot_hits[0]) - margin, y1 - quarter)
        notes.append(f"caption chrome trimmed to y={y1 / h:.3f}")

    # A small inset on every side. Scrim gradients and compression ringing sit
    # right at the boundary and are not part of the photograph.
    pad_y, pad_x = int(0.01 * (y1 - y0)), int(0.01 * (x1 - x0))
    y0, y1 = y0 + pad_y, y1 - pad_y
    x0, x1 = x0 + pad_x, x1 - pad_x
    return {
        "x": int(x0), "y": int(y0), "w": int(x1 - x0), "h": int(y1 - y0),
        "frac": [round(x0 / w, 4), round(y0 / h, 4),
                 round(x1 / w, 4), round(y1 / h, 4)],
        "image": [int(w), int(h)],
        "area_pct": round(100.0 * (x1 - x0) * (y1 - y0) / (w * h), 1),
        "notes": notes,
    }


def crop_to(rgb: np.ndarray, region: dict) -> np.ndarray:
    x, y, w, h = region["x"], region["y"], region["w"], region["h"]
    if w < 16 or h < 16:
        raise MatchError(f"crop region {region} is too small to measure")
    return rgb[y:y + h, x:x + w]


def parse_crop(text: str, w: int, h: int, fraction: bool) -> dict:
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 4:
        raise MatchError("crop must be x,y,w,h")
    v = [float(p) for p in parts]
    if fraction:
        x, yy, cw, ch = v[0] * w, v[1] * h, v[2] * w, v[3] * h
    else:
        x, yy, cw, ch = v
    x, yy, cw, ch = int(x), int(yy), int(cw), int(ch)
    x, yy = max(0, x), max(0, yy)
    cw, ch = min(cw, w - x), min(ch, h - yy)
    return {"x": x, "y": yy, "w": cw, "h": ch,
            "frac": [round(x / w, 4), round(yy / h, 4),
                     round((x + cw) / w, 4), round((yy + ch) / h, 4)],
            "image": [w, h], "area_pct": round(100.0 * cw * ch / (w * h), 1)}


# --------------------------------------------------------------------------
# the picked rectangle (contract C7)
#
# A rectangle the user drew, on the reference and/or on the frame, as
# [x0, y0, x1, y1] fractions of the image (0 to 1, top left origin). It is
# applied FIRST, before anything else looks at the picture, so the automatic
# chrome detector above runs INSIDE the rectangle rather than fighting it:
# "match this part of this photograph" and "throw away Instagram's icons" are
# two different questions and both still get answered.
#
# Fractions, not pixels, because the rectangle is drawn on a preview that is
# some arbitrary size and on a frame whose size depends on the rotation, and a
# fraction means the same region at every one of those sizes.
# --------------------------------------------------------------------------

def parse_box(value, what: str = "crop") -> list | None:
    """[x0, y0, x1, y1] fractions, or None for the whole image.

    Accepts a list, a tuple or a comma separated string, in any corner order:
    a rectangle dragged up and to the left arrives with x1 < x0 and means the
    same region as the same drag made the other way.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        parts = [p for p in text.replace(",", " ").split() if p]
    else:
        try:
            parts = list(value)
        except TypeError:
            raise MatchError(f"{what} must be x0,y0,x1,y1 as fractions "
                             f"of the image") from None
    if not parts:
        return None
    if len(parts) != 4:
        raise MatchError(f"{what} must be four numbers x0,y0,x1,y1 as "
                         f"fractions of the image, got {len(parts)}")
    try:
        v = [float(p) for p in parts]
    except (TypeError, ValueError):
        raise MatchError(f"{what} must be four numbers, got {value!r}") from None
    if not all(np.isfinite(v)):
        raise MatchError(f"{what} has a non finite number in it: {value!r}")
    x0, x1 = sorted((v[0], v[2]))
    y0, y1 = sorted((v[1], v[3]))
    box = [min(max(n, 0.0), 1.0) for n in (x0, y0, x1, y1)]
    if (box[2] - box[0]) < 0.01 or (box[3] - box[1]) < 0.01:
        raise MatchError(
            f"{what} {box} is thinner than 1% of the image on one axis, "
            f"which is not a region anything can be measured from")
    return box


def crop_box(rgb: np.ndarray, box, what: str = "crop") -> np.ndarray:
    """The part of the image inside a fraction rectangle.

    Rounds outward to whole pixels, so a rectangle drawn on a 200px preview
    still covers the pixels it visibly covered on a 4000px original.
    """
    if box is None:
        return rgb
    h, w = rgb.shape[:2]
    x0 = int(np.floor(box[0] * w))
    y0 = int(np.floor(box[1] * h))
    x1 = int(np.ceil(box[2] * w))
    y1 = int(np.ceil(box[3] * h))
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w, max(x1, x0 + 1)), min(h, max(y1, y0 + 1))
    if (x1 - x0) < 16 or (y1 - y0) < 16:
        raise MatchError(
            f"the {what} rectangle {box} is {x1 - x0}x{y1 - y0} pixels of a "
            f"{w}x{h} image, too small to measure a colour distribution from")
    return rgb[y0:y1, x0:x1]


def box_report(box, rgb_before, rgb_after) -> dict | None:
    """What a picked rectangle actually kept, for the report and the UI."""
    if box is None:
        return None
    hb, wb = rgb_before.shape[:2]
    ha, wa = rgb_after.shape[:2]
    return {
        "box": [round(float(v), 4) for v in box],
        "pixels": [int(wa), int(ha)],
        "image": [int(wb), int(hb)],
        "area_pct": round(100.0 * (wa * ha) / float(wb * hb), 1),
    }


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------

def _flat(rgb: np.ndarray, reject_blown: bool, cap: int = 900_000):
    px = rgb.reshape(-1, 3).astype(np.float32)
    rejected = 0.0
    if reject_blown:
        s = _sat(px)
        y = C.luma709(px)[..., 0]
        keep = ~((s < BLOWN_SAT) & (y > BLOWN_LUMA))
        rejected = float(100.0 * (1.0 - keep.mean()))
        if keep.sum() > 1000:
            px = px[keep]
    if px.shape[0] > cap:
        step = px.shape[0] // cap + 1
        px = px[::step]
    return px, rejected


def _tonal_density(chan: np.ndarray) -> float:
    """Effective 8 bit levels divided by the range they sit in, 0 to 1.

    Three cheaper tests were tried and all three are wrong. Counting distinct
    codes fails because a transform that merely compresses a channel's range
    loses codes without creating a single flat step. Counting distinct codes
    per unit of range fails because tetrahedral interpolation fills every code
    in between anyway: a LUT built to posterise on purpose still measured a
    perfect score. What actually changes under posterisation is the shape of
    the histogram, which piles up on a few codes and empties the ones between,
    so this uses the exponential of the histogram entropy (the effective
    number of levels actually carrying pixels) normalised by the range. A
    genuine 6 step LUT scores near zero on it; a strong but smooth grade does
    not move it much.
    """
    v = (np.clip(chan, 0.0, 1.0) * 255.0).astype(np.uint8)
    lo, hi = int(np.percentile(v, 1)), int(np.percentile(v, 99))
    if hi <= lo:
        return 0.0
    h = np.bincount(v[(v >= lo) & (v <= hi)] - lo, minlength=hi - lo + 1)
    p = h[h > 0] / h.sum()
    return float(np.exp(-(p * np.log(p)).sum()) / (hi - lo + 1))


def measure(rgb: np.ndarray, reject_blown: bool = True) -> dict:
    px, rejected = _flat(rgb, reject_blown)
    y = C.luma709(px)[..., 0]
    h, s, _ = C.rgb_to_hsv(px)
    pct = [float(v) for v in np.percentile(y, [5, 25, 50, 75, 95])]
    fam = {}
    coloured = s >= NEUTRAL_SAT
    n = float(px.shape[0])
    for name, lo, hi in FAMILIES:
        if lo < hi:
            sel = (h >= lo) & (h < hi)
        else:
            sel = (h >= lo) | (h < hi)
        fam[name] = float((sel & coloured).sum() / n)
    fam["neutral"] = float((~coloured).sum() / n)
    return {
        "luma_pct": [round(v, 4) for v in pct],
        "mean_sat": round(float(s.mean()), 4),
        "families": {k: round(v, 4) for k, v in fam.items()},
        "clip_low_pct": round(float(100.0 * (px.min(-1) <= 1.0 / 255).mean()), 3),
        "clip_high_pct": round(float(100.0 * (px.max(-1) >= 254.0 / 255).mean()), 3),
        "levels": [int(len(np.unique((px[:, c] * 255).astype(np.uint8))))
                   for c in range(3)],
        "tonal_density": [round(_tonal_density(px[:, c]), 3) for c in range(3)],
        "rejected_blown_pct": round(rejected, 3),
        "pixels": int(px.shape[0]),
    }


def stat_distance(a: dict, b: dict) -> dict:
    """One number per axis, and one number overall, all in 0 to 1 units.

    Everything being a fraction or a code value means the three terms are
    already commensurate, so the total is a plain rms with no invented weights.
    d_colour exists separately because --luma-preserve deliberately refuses to
    move the luma term, and judging it on a total that includes luma would
    score a working match as a failure.
    """
    dl = np.array(a["luma_pct"]) - np.array(b["luma_pct"])
    ds = a["mean_sat"] - b["mean_sat"]
    keys = [k for k, _, _ in FAMILIES] + ["neutral"]
    df = np.array([a["families"][k] - b["families"][k] for k in keys])
    d_luma = float(np.sqrt((dl ** 2).mean()))
    d_sat = float(abs(ds))
    d_hue = float(np.sqrt((df ** 2).mean()))
    return {
        "luma": round(d_luma, 4), "sat": round(d_sat, 4), "hue": round(d_hue, 4),
        "total": round(float(np.sqrt((d_luma ** 2 + d_sat ** 2 + d_hue ** 2) / 3)), 4),
        "colour": round(float(np.sqrt((d_sat ** 2 + d_hue ** 2) / 2)), 4),
    }


def hue_divergence(src: dict, ref: dict) -> float:
    """Total variation distance between the two hue family distributions.

    0 is the same mix of hues, 1 is no overlap at all. This is the number the
    unreliable-match warning is based on: it is a property of the two images,
    measurable before any LUT exists.
    """
    keys = [k for k, _, _ in FAMILIES] + ["neutral"]
    p = np.array([src["families"][k] for k in keys])
    q = np.array([ref["families"][k] for k in keys])
    return float(0.5 * np.abs(p - q).sum())


# --------------------------------------------------------------------------
# method 1: Reinhard mean/std transfer in l-alpha-beta
# --------------------------------------------------------------------------

RGB_TO_LMS = np.array([[0.3811, 0.5783, 0.0402],
                       [0.1967, 0.7244, 0.0782],
                       [0.0241, 0.1288, 0.8444]])
LMS_TO_RGB = np.linalg.inv(RGB_TO_LMS)
_B = np.array([[1.0, 1.0, 1.0], [1.0, 1.0, -2.0], [1.0, -1.0, 0.0]])
LMS_TO_LAB = np.diag([1 / np.sqrt(3), 1 / np.sqrt(6), 1 / np.sqrt(2)]) @ _B
LAB_TO_LMS = np.linalg.inv(LMS_TO_LAB)

# The log floor. 1e-4 linear is about 12 stops under mid grey, well below
# anything a display referred image holds, but finite: with a true zero the
# log axis runs to minus infinity and the affine transfer sends pure black
# somewhere arbitrary.
LOG_FLOOR = 1e-4


def _to_lab(disp: np.ndarray, decode) -> np.ndarray:
    lin = np.maximum(decode(disp), 0.0)
    lms = np.maximum(C.apply_matrix(lin, RGB_TO_LMS), LOG_FLOOR)
    return C.apply_matrix(np.log10(lms), LMS_TO_LAB)


def _encode_ext(x: np.ndarray, encode) -> np.ndarray:
    """Display encode that keeps the overshoot instead of clipping it.

    This is load bearing. Every encoder in colorlib clips to [0, 1] first, so
    a fit that lands a third of the way past white came back reading exactly
    1.0, the limiter saw nothing to roll off, and the top of the ramp arrived
    already flat with no way to tell why. Continuing the curve past white with
    its own slope keeps the overshoot visible in display code, which is the
    only place the shoulder can be sized correctly.
    """
    eps = 1e-3
    slope = float((1.0 - encode(np.array(1.0 - eps))) / eps)
    out = encode(np.clip(x, 0.0, 1.0))
    out = np.where(x > 1.0, 1.0 + (x - 1.0) * slope, out)
    return np.where(x < 0.0, x * slope, out)


def _from_lab(lab: np.ndarray, encode) -> np.ndarray:
    lms = 10.0 ** C.apply_matrix(lab, LAB_TO_LMS)
    lin = C.apply_matrix(np.maximum(lms - LOG_FLOOR, 0.0), LMS_TO_RGB)
    return _encode_ext(lin, encode)


# The band of the neutral ramp that has to stay separated. Below the toe and
# above the shoulder a film curve compresses on purpose, and any limiter that
# keeps a fit inside [0, 1] has to spend its compression somewhere. Demanding
# full separation at 0.97 refuses every fit that reaches white at all, so the
# test is on the working range and the ends are reported as numbers instead.
WORK_LOW = 0.03
WORK_HIGH = 0.90


def _work_steps(out: np.ndarray, size: int) -> np.ndarray:
    """Neutral ramp steps whose input sits inside the working range."""
    x = np.linspace(0.0, 1.0, size)
    mid = 0.5 * (x[:-1] + x[1:])
    sel = (mid >= WORK_LOW) & (mid <= WORK_HIGH)
    return np.diff(out, axis=0)[sel]


def _ramp_ok(out: np.ndarray, size: int) -> bool:
    """Is this neutral ramp something a LUT can carry without collapsing."""
    return bool(_work_steps(out, size).min() >= 0.15 / (size - 1))


def _range_scale(lab_ramp, mu_s, gain, mu_r, encode, size) -> float:
    """The largest fraction of the fit whose neutral ramp still holds up.

    The gain clamp alone is not enough. A fit can be perfectly reasonable in
    the middle and still ask for values a third of the way past white, and
    those land on top of each other however they are limited: every tone above
    75% comes out the same red, which is a quarter of the ramp with no
    separation in it. Rolling that off harder only moves the collapse around.

    So the fit itself gets scaled back toward identity until the ramp is
    clean, by bisection, and the scale is reported. Scaling in this space
    keeps the direction of the match (the hue the reference is pulling
    toward) and only gives up magnitude, which is exactly what a colourist
    does when a match asks for more than the format holds.
    """
    def out_for(k):
        g = 1.0 + (gain - 1.0) * k
        m = mu_s + (mu_r - mu_s) * k
        raw = _from_lab((lab_ramp - mu_s) * g + m, encode)
        return soft_range(raw, *limits_for(raw))

    if _ramp_ok(out_for(1.0), size):
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(24):
        mid = 0.5 * (lo + hi)
        if _ramp_ok(out_for(mid), size):
            lo = mid
        else:
            hi = mid
    return lo


def build_reinhard(src_px, ref_px, decode, encode, luma_preserve,
                   gain_clamp=1.5, size=33):
    """Fit the affine move, return a function on display code RGB.

    The std ratio is clamped at 1.5. An unclamped ratio is how this method
    goes catastrophic: a flat low contrast source against a punchy reference
    asks for a 4x stretch on an axis and every shadow in the frame either
    clips or turns a colour that was never in the scene. 1.5 was picked by
    measurement, not taste: at 1.8 the IMG_2570 match drove 4.6% of the frame
    to clipped white and zeroed 28% of the cube's entries.
    """
    a = _to_lab(src_px, decode)
    b = _to_lab(ref_px, decode)
    mu_s, sd_s = a.mean(0), a.std(0)
    mu_r, sd_r = b.mean(0), b.std(0)
    raw = sd_r / np.maximum(sd_s, 1e-6)
    gain = np.clip(raw, 1.0 / gain_clamp, gain_clamp)
    if luma_preserve:
        # axis 0 is the achromatic axis of this space, so leaving it alone is
        # exactly "match the colour, do not touch the exposure".
        gain[0] = 1.0
        mu_r = mu_r.copy()
        mu_r[0] = mu_s[0]

    ramp = np.repeat(np.linspace(0.0, 1.0, size)[:, None], 3, axis=1)
    k = _range_scale(_to_lab(ramp, decode), mu_s, gain, mu_r, encode, size)
    gain = 1.0 + (gain - 1.0) * k
    mu_r = mu_s + (mu_r - mu_s) * k

    def fn(rgb: np.ndarray) -> np.ndarray:
        lab = _to_lab(rgb, decode)
        return _from_lab((lab - mu_s) * gain + mu_r, encode)

    info = {
        "src_mean": [round(float(v), 4) for v in mu_s],
        "ref_mean": [round(float(v), 4) for v in mu_r],
        "raw_gain": [round(float(v), 4) for v in raw],
        "used_gain": [round(float(v), 4) for v in gain],
        "gain_clamped": bool(np.any(np.abs(raw - gain) > 1e-6)),
        "range_scale": round(float(k), 3),
    }
    return fn, info


SOFT_TOE = 0.030
SOFT_SHOULDER = 0.980


def limits_for(ramp_out: np.ndarray) -> tuple[float, float]:
    """Pick the toe and shoulder from how far the fit actually overshot.

    A fixed shoulder at 0.92 is fine for a fit that lands near white and
    terrible for one that lands a third of the way past it: the whole
    overshoot gets squeezed into the top 8% of the range and every tone above
    75% comes out the same value. Widening the shoulder in proportion to the
    overshoot spreads that compression over enough range that no two adjacent
    LUT nodes land on top of each other. It costs highlight contrast, which is
    the honest price of a reference brighter than the format holds.
    """
    w = float(np.max(ramp_out))
    m = float(np.min(ramp_out))
    knee = float(np.clip(min(SOFT_SHOULDER, 2.0 - w), 0.50, SOFT_SHOULDER))
    # a fit that already lands inside the format needs no shoulder at all,
    # so the knee sits high enough to leave the neutral ramp alone and still
    # catch the out of gamut corners of the cube
    toe = float(np.clip(max(SOFT_TOE, -m), SOFT_TOE, 0.25))
    return toe, knee


def soft_range(x: np.ndarray, toe: float = SOFT_TOE,
               knee: float = SOFT_SHOULDER) -> np.ndarray:
    """Roll the two ends into (0, 1) instead of clipping them there.

    A hard clip is how a transfer function turns into a broken LUT: every
    value the fit pushed past white lands on exactly 1.0, so a whole region of
    the frame becomes one flat colour with no detail, and the same at the
    bottom. Both branches are C1 at the join and asymptotic at the ends, so
    the table stays smooth, stays monotonic, and can never contain a hard 0 or
    a hard 1 no matter how far the fit overshot.
    """
    out = np.asarray(x, dtype=np.float64).copy()
    hi = out > knee
    out[hi] = knee + (1.0 - knee) * np.tanh((out[hi] - knee) / (1.0 - knee))
    lo = out < toe
    out[lo] = toe * (np.tanh((out[lo] - toe) / toe) + 1.0)
    return out


# --------------------------------------------------------------------------
# method 2: per channel cumulative histogram matching
# --------------------------------------------------------------------------

CURVE_N = 257
MIN_SLOPE = 0.25          # below this the curve flattens and the image posterises
MAX_SLOPE = 3.20          # above this it stretches noise into banding and clips


def _smooth(v: np.ndarray, passes: int = 6) -> np.ndarray:
    """Binomial smoothing with edge replication. No scipy in this venv.

    Applied to the mapping curve, not to the image: a CDF match built from a
    finite histogram is a staircase, and the staircase is what posterises.
    """
    out = v.astype(np.float64).copy()
    for _ in range(passes):
        p = np.concatenate(([out[0]], out, [out[-1]]))
        out = 0.25 * p[:-2] + 0.5 * p[1:-1] + 0.25 * p[2:]
    return out


def _shape_curve(curve: np.ndarray, lo: float = None, hi: float = None,
                 passes: int = 3) -> np.ndarray:
    """Clamp the derivative, smooth the derivative, integrate it back.

    Slope zero means many input values map to one output value, which is the
    definition of posterisation, and a hard clip to [0, 1] manufactures
    exactly that at whichever end the fit overshot. So the work happens in the
    slope domain: clamping there gives a floor, smoothing an already clamped
    slope keeps it inside the same bounds (an average of values in a range
    stays in that range), and the ends are rolled off with soft_range instead
    of clipped. The result cannot go flat, cannot fold back on itself, and
    cannot pin at 0 or 1.

    The ceiling matters too: a very steep segment stretches a narrow band of
    input across a wide band of output, which turns 8 bit quantisation steps
    into visible bands in a sky.
    """
    lo = MIN_SLOPE if lo is None else lo
    hi = MAX_SLOPE if hi is None else hi
    n = len(curve)
    step = 1.0 / (n - 1)
    d = _smooth(np.clip(np.diff(curve) / step, lo, hi), passes)
    out = np.concatenate(([curve[0]], curve[0] + np.cumsum(d) * step))
    # The slope floor can push the endpoint past 1.0 on a low contrast
    # reference: raising every shallow segment to 0.25 has to end up
    # somewhere. Fitting the whole curve back with one affine scale is the
    # cheap fix, because it divides every slope by the same small factor
    # (1.05 in practice) instead of dumping the entire overshoot into a
    # shoulder, which is what flattens the top of the ramp.
    lo0, hi0 = float(out[0]), float(out[-1])
    t_lo, t_hi = max(0.0, lo0), min(1.0, hi0)
    if hi0 - lo0 > 1e-6 and (abs(t_lo - lo0) > 1e-9 or abs(t_hi - hi0) > 1e-9):
        out = t_lo + (out - lo0) * (t_hi - t_lo) / (hi0 - lo0)
    return out


def _channel_curve(src: np.ndarray, ref: np.ndarray, bins: int = 512) -> np.ndarray:
    edges = np.linspace(0.0, 1.0, bins + 1)
    centres = 0.5 * (edges[:-1] + edges[1:])
    hs, _ = np.histogram(np.clip(src, 0, 1), bins=edges)
    hr, _ = np.histogram(np.clip(ref, 0, 1), bins=edges)
    # A uniform prior stops an empty bin in the reference from producing an
    # infinite jump in the inverse CDF.
    hs = hs + hs.sum() * 1e-4 / bins
    hr = hr + hr.sum() * 1e-4 / bins
    cs = np.cumsum(hs) / hs.sum()
    cr = np.cumsum(hr) / hr.sum()
    x = np.linspace(0.0, 1.0, CURVE_N)
    p = np.interp(x, centres, cs, left=0.0, right=1.0)
    return np.interp(p, cr, centres, left=centres[0], right=centres[-1])


def build_histogram(src_px, ref_px, luma_preserve):
    x = np.linspace(0.0, 1.0, CURVE_N)
    curves = np.stack([_channel_curve(src_px[:, c], ref_px[:, c])
                       for c in range(3)])
    # smooth the raw map first: a CDF match built from a finite histogram is a
    # staircase, and the staircase is what posterises
    curves = np.stack([_smooth(c) for c in curves])
    if luma_preserve:
        # Subtracting the common tone response leaves only what separates the
        # three channels, which is the colour half of the match. The average
        # curve becomes the identity, so overall exposure and contrast stay
        # where the source had them. Doing this before the slope work is
        # deliberate: the subtraction can push a channel out of range, and
        # _shape_curve is the thing that puts it back without a flat spot.
        common = curves.mean(0)
        curves = curves - (common - x)
    curves = np.stack([_shape_curve(c) for c in curves])

    def fn(rgb: np.ndarray) -> np.ndarray:
        return np.stack(
            [np.interp(rgb[..., c], x, curves[c]) for c in range(3)], axis=-1)

    slopes = np.diff(curves, axis=1) * (CURVE_N - 1)
    # the core is where posterisation would be visible. The ends are allowed
    # to go shallow because that is soft_range rolling them off on purpose.
    core = (curves[:, :-1] > 0.05) & (curves[:, :-1] < 0.90)
    info = {
        "curve_endpoints": [[round(float(c[0]), 4), round(float(c[-1]), 4)]
                            for c in curves],
        "min_slope_core": round(float(slopes[core].min()) if core.any() else 0.0, 3),
        "min_slope": round(float(slopes.min()), 3),
        "max_slope": round(float(slopes.max()), 3),
        "monotonic": bool(np.all(slopes > 0.0)),
    }
    return fn, info


METHODS = ("reinhard", "histogram")


# --------------------------------------------------------------------------
# LUT construction and health checks
# --------------------------------------------------------------------------

def build_grid(fn, size: int, strength: float) -> tuple[np.ndarray, dict]:
    grid = C.identity_grid(size)
    # The limiter is fitted on the neutral ramp, not on the whole grid: the
    # saturated corners of a cube routinely land far out of gamut and letting
    # them set the shoulder would crush the tones anyone can actually see.
    ramp = np.repeat(np.linspace(0.0, 1.0, size)[:, None], 3, axis=1)
    toe, knee = limits_for(fn(ramp))
    # soft_range before the strength blend, so strength 0 is still the exact
    # identity and only the transform itself is limited
    out = np.clip(soft_range(fn(grid), toe, knee), 0.0, 1.0)
    # Strength is a straight blend toward identity. Both transforms are
    # monotone, and a convex combination of monotone functions is monotone, so
    # partial strength cannot introduce a fold that full strength did not have.
    return (np.clip(grid + (out - grid) * float(strength), 0.0, 1.0),
            {"toe": round(toe, 4), "shoulder": round(knee, 4)})


def lut_health(grid_out: np.ndarray, size: int) -> dict:
    """Everything that makes a LUT unshippable, measured on the table itself."""
    g = grid_out.reshape(size, size, size, 3)      # [b, g, r, channel]
    diag = np.stack([g[i, i, i] for i in range(size)])
    d_diag = np.diff(diag, axis=0)
    probes = {}
    ok = True
    for name, rgb in PROBES.items():
        v = np.asarray(rgb, dtype=np.float64)[None, :]
        o = np.clip(sample_lut(grid_out, size, v)[0], 0.0, 1.0)
        chroma_in = float(v[0].max() - v[0].min())
        # A neutral probe has no dominant channel to keep, so asking whether it
        # kept one just reads back argsort's tie break. The right question for
        # a neutral is whether it stayed neutral.
        if chroma_in < 0.05:
            keeps = bool(float(o.max() - o.min()) < 0.12)
        else:
            keeps = bool(np.argmax(v[0]) == np.argmax(o))
        pinned = bool(np.any(o <= 0.0005) or np.any(o >= 0.9995))
        hin, _, _ = C.rgb_to_hsv(v)
        hout, _, _ = C.rgb_to_hsv(o[None, :])
        d_hue = float(abs(((hout[0] - hin[0] + 180.0) % 360.0) - 180.0))
        # a hue angle on a near neutral is noise, so it is not reported as one.
        # None, not NaN: this result is serialised to JSON for the studio, and
        # a bare NaN is not valid JSON, so a browser rejects the whole response
        # rather than just this field.
        hue_out: float | None = round(d_hue, 1)
        if chroma_in < 0.05 or float(o.max() - o.min()) < 0.05:
            hue_out = None
        probes[name] = {
            "in": [round(float(x), 4) for x in v[0]],
            "out": [round(float(x), 4) for x in o],
            "dominant_channel_kept": keeps,
            "hue_shift_deg": hue_out,
            "pinned": pinned,
        }
        # maroon and skin are the two the brief pins down, and no probe at all
        # may be driven to a hard 0 or 1. sky and foliage are reported but not
        # fatal: a real match legitimately moves a sky, and failing the run for
        # that would mean no strong match ever passes.
        # Only the two the brief pins down are fatal, plus any hard 0 or 1.
        # A neutral picking up a cast is what a match to a warm reference does
        # on purpose, so it is reported and not failed.
        if pinned or (name in ("maroon", "skin") and not keeps):
            ok = False
    if probes["skin"]["out"][0] < probes["skin"]["out"][1] or \
            probes["skin"]["out"][1] < probes["skin"]["out"][2]:
        probes["skin"]["plausible"] = False
        ok = False
    else:
        probes["skin"]["plausible"] = True
    # A flat step on the neutral ramp is posterisation that no image based
    # measure can see reliably: it only shows when the shot happens to hold
    # tones in that band. Reading it off the table catches it either way.
    # The first and last steps are excluded: a toe and a shoulder live there
    # in every real film curve, and compressing the last 3% of the ramp is a
    # highlight roll-off, not a defect.
    step = 1.0 / (size - 1)
    inner = _work_steps(diag, size)
    x = np.linspace(0.0, 1.0, size)
    sel = np.flatnonzero(((0.5 * (x[:-1] + x[1:])) >= WORK_LOW)
                         & ((0.5 * (x[:-1] + x[1:])) <= WORK_HIGH))
    worst = int(sel[int(np.argmin(inner.min(axis=1)))])
    return {
        # Tolerance, not sloppiness: the toe can invert by a few parts in a
        # million where the fit sends two adjacent dark nodes to the same
        # place. A .cube is written at six decimals, so a step of -4e-6 is not
        # a hue inversion anyone can see, it is arithmetic noise. A real fold
        # is orders of magnitude bigger and still fails this.
        "neutral_monotonic": bool(np.all(d_diag >= -1e-5)),
        "neutral_min_step": round(float(inner.min()), 5),
        "toe_step": round(float(d_diag[:2].min()), 5),
        "shoulder_step": round(float(d_diag[-4:].min()), 5),
        "neutral_flat": bool(inner.min() < 0.15 * step),
        "neutral_flat_at": round(worst * step, 3),
        "black_point": [round(float(x), 4) for x in g[0, 0, 0]],
        "white_point": [round(float(x), 4) for x in g[-1, -1, -1]],
        # An identity cube is already 3.03% zero at size 33 (every face where
        # a channel is 0), so the baseline is carried alongside or the number
        # reads as damage that is not there.
        "zeroed_entries_pct": round(float(100.0 * (grid_out <= 0.0005).mean()), 3),
        "zeroed_identity_pct": round(float(100.0 / size), 3),
        "pinned_entries_pct": round(float(100.0 * (grid_out >= 0.9995).mean()), 3),
        "identity_drift": round(float(np.abs(
            grid_out - C.identity_grid(size)).mean()), 4),
        "probes": probes,
        "probes_ok": ok,
    }


def sample_lut(grid_out: np.ndarray, size: int, rgb: np.ndarray) -> np.ndarray:
    """Trilinear sample of a .cube table, for the probe checks."""
    g = grid_out.reshape(size, size, size, 3)
    v = np.clip(rgb, 0.0, 1.0) * (size - 1)
    i0 = np.floor(v).astype(int)
    i0 = np.minimum(i0, size - 2)
    f = v - i0
    out = np.zeros_like(rgb, dtype=np.float64)
    for dr in (0, 1):
        for dg in (0, 1):
            for db in (0, 1):
                w = ((f[:, 0] if dr else 1 - f[:, 0])
                     * (f[:, 1] if dg else 1 - f[:, 1])
                     * (f[:, 2] if db else 1 - f[:, 2]))
                out += w[:, None] * g[i0[:, 2] + db, i0[:, 1] + dg, i0[:, 0] + dr]
    return out


# --------------------------------------------------------------------------
# the match itself
# --------------------------------------------------------------------------

def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name).lower()


def _bands_u8(rgb01: np.ndarray) -> dict:
    """bands() on this module's own float-[0,1] images.

    grade/stats.bands() takes the uint8 0-255 array every ffmpeg rgb24 decode
    in the studio and the CLI produces, the same convention frame_stats()
    uses; this file's own images are float in [0, 1] (read_image's own
    docstring), so the one conversion happens here rather than changing what
    either side means by "an image".
    """
    return _bands(np.clip(rgb01 * 255.0, 0.0, 255.0).astype(np.uint8))


def match_reference(ref: str, clip: str | None = None, time: float = 0.0,
                    still: str | None = None, preset: str | None = None,
                    method: str = "reinhard", strength: float = 1.0,
                    luma_preserve: bool = False, crop: str | None = None,
                    crop_frac: str | None = None, auto_crop: bool = True,
                    ref_crop=None, frame_crop=None,
                    size: int = 33, name: str | None = None,
                    autorotate: bool = True, rotation=None,
                    out_dir: str | None = None,
                    width: int = 960, ref_gamut: str = "auto",
                    reject_blown: bool = True, verify: bool = True) -> dict:
    """Build one LUT and measure whether it actually helped.

    Returns a dict with the LUT path, the before/after statistics, the two
    distance numbers, and every warning. Never raises for a bad-looking match:
    a weak or unreliable result is reported, not hidden.
    """
    t0 = _time.time()
    if method not in METHODS:
        raise MatchError(f"unknown method {method}, expected one of "
                         f"{', '.join(METHODS)}")
    ref_path = Path(ref)
    if not ref_path.exists():
        raise MatchError(f"reference not found: {ref}")

    cfg = _measure_cfg(preset)
    encode_name = cfg["convert"]["encode"]
    encode = C.ENCODERS.get(encode_name, C.encode_gamma22)

    decode = DECODERS.get(encode_name, decode_gamma22)

    ref_img, gamut = load_reference(ref_path, ref_gamut, encode_name)
    # Contract C7: the rectangle the user picked comes off first, and every
    # later step (the chrome detector, the explicit --crop, the measurement)
    # sees only what is inside it. Nothing happens when it is None, which is
    # what keeps every call made before this option existed byte identical.
    ref_box = parse_box(ref_crop, "ref-crop")
    ref_full = ref_img
    if ref_box is not None:
        ref_img = crop_box(ref_img, ref_box, "reference")
    ref_pick = box_report(ref_box, ref_full, ref_img)
    rh, rw = ref_img.shape[:2]
    if crop:
        region = parse_crop(crop, rw, rh, fraction=False)
        region["source"] = "explicit"
    elif crop_frac:
        region = parse_crop(crop_frac, rw, rh, fraction=True)
        region["source"] = "explicit-fraction"
    elif auto_crop:
        region = detect_content_region(ref_img)
        region["source"] = "auto"
    else:
        region = {"x": 0, "y": 0, "w": rw, "h": rh, "frac": [0, 0, 1, 1],
                  "image": [rw, rh], "area_pct": 100.0, "source": "none"}
    ref_crop = crop_to(ref_img, region)

    if still:
        src_img = read_image(Path(still))
        src_label = f"still {Path(still).name}"
    elif clip:
        src_img, cfg = load_source_frame(Path(clip), time, preset, autorotate,
                                         width, rotation)
        src_label = f"{Path(clip).name} @ {time}s"
    else:
        raise MatchError("give either --clip (with --time) or --source-still")

    # The frame's own picked rectangle, in fractions of the frame AS SHOWN
    # (after the rotation above), so a rectangle drawn on the viewer covers
    # the same part of the picture the matcher measures.
    frame_box = parse_box(frame_crop, "frame-crop")
    src_full = src_img
    if frame_box is not None:
        src_img = crop_box(src_img, frame_box, "frame")
    frame_pick = box_report(frame_box, src_full, src_img)

    m_ref = measure(ref_crop, reject_blown)
    m_src = measure(src_img, reject_blown)
    before = stat_distance(m_src, m_ref)
    div = hue_divergence(m_src, m_ref)

    warnings: list[str] = []
    # Not warnings: plain statements of what was measured, kept apart so the
    # warn coloured list in the studio stays a list of things that are wrong.
    notes_used: list[str] = []
    if div > 0.35:
        gaps = sorted(m_src["families"].keys(),
                      key=lambda k: -abs(m_src["families"][k]
                                         - m_ref["families"][k]))[:2]
        detail = ", ".join(
            f"{k} {100 * m_src['families'][k]:.0f}% in the shot against "
            f"{100 * m_ref['families'][k]:.0f}% in the reference" for k in gaps)
        warnings.append(
            f"content mismatch: hue family total variation distance {div:.2f} "
            f"(over 0.35). biggest gaps are {detail}. the match can only "
            f"close that by dragging the whole frame, which is a content "
            f"difference being treated as a grade")
    dmed = abs(m_src["luma_pct"][2] - m_ref["luma_pct"][2])
    if dmed > 0.15 and not luma_preserve:
        warnings.append(
            f"exposure gap: median luma {m_src['luma_pct'][2]:.3f} vs "
            f"{m_ref['luma_pct'][2]:.3f} (delta {dmed:.3f}). matching "
            f"brightness across different scenes is usually wrong, consider "
            f"--luma-preserve")
    if region["area_pct"] < 12.0:
        warnings.append(
            f"crop kept only {region['area_pct']:.1f}% of the reference, so the "
            f"statistics come from a small sample. check it with `detect`")
    # What the picked rectangles kept, said in the warnings as well as in the
    # result, because "which part of the picture did this actually measure" is
    # the one thing a picked match can silently get wrong.
    for label, pick in (("reference", ref_pick), ("frame", frame_pick)):
        if pick is None:
            continue
        b = pick["box"]
        line = (f"{label} picked rectangle {b[0]:.2f} {b[1]:.2f} {b[2]:.2f} "
                f"{b[3]:.2f}, {pick['area_pct']:.1f}% of the image "
                f"({pick['pixels'][0]}x{pick['pixels'][1]} of "
                f"{pick['image'][0]}x{pick['image'][1]} pixels)")
        if pick["area_pct"] < 1.0:
            warnings.append(line + ": under 1% of the picture, so this is a "
                                   "very small sample to fit a transform from")
        else:
            notes_used.append(line)

    ref_px, _ = _flat(ref_crop, reject_blown)
    src_px, _ = _flat(src_img, reject_blown)
    if method == "reinhard":
        fn, minfo = build_reinhard(src_px, ref_px, decode, encode,
                                   luma_preserve, size=size)
    else:
        fn, minfo = build_histogram(src_px, ref_px, luma_preserve)

    grid_out, limits = build_grid(fn, size, strength)
    minfo.update(limits)
    health = lut_health(grid_out, size)

    stem = name or (f"match_{_safe(ref_path.stem)}_"
                    f"{_safe(Path(clip or still).stem)}_{method}"
                    + ("_lp" if luma_preserve else ""))
    dest_dir = Path(out_dir) if out_dir else LUT_LOOKS
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{stem}.cube"
    C.write_cube(dest, grid_out, size, stem, comments=[
        "Generated by content/grade/tools/match_ref.py",
        "Domain: Rec.709 display code. Apply AFTER the CST, never to raw log.",
        f"Reference: {ref_path.name} ({gamut}) crop "
        f"{region['x']},{region['y']},{region['w']},{region['h']}"
        + ("" if ref_pick is None
           else " inside picked rectangle "
                + " ".join(f"{v:.3f}" for v in ref_pick["box"])),
        # Only when there is one: a match with no picked rectangle writes the
        # comment block it has always written, byte for byte.
        *([] if frame_pick is None else
          ["Frame: picked rectangle "
           + " ".join(f"{v:.3f}" for v in frame_pick["box"])]),
        f"Source: {src_label} preset={preset or 'defaults'} "
        f"encode={encode_name}",
        f"Method: {method} strength={strength} "
        f"luma_preserve={bool(luma_preserve)}",
        "This is an inferred distribution match, not the reference's own LUT.",
    ])

    result = {
        "ok": True,
        "lut": str(dest),
        "name": stem,
        "method": method,
        "strength": strength,
        "luma_preserve": bool(luma_preserve),
        "reference": str(ref_path),
        "ref_gamut": gamut,
        "source": src_label,
        "preset": preset or "defaults",
        "encode": encode_name,
        "crop": region,
        # Contract C7. `crops` is what the studio's match readout prints and
        # what an agent reads back to know which part of which picture this
        # cube was fitted from. Both are None for a whole image match.
        "crops": {"ref": ref_pick, "frame": frame_pick},
        "hue_divergence": round(div, 4),
        "method_info": minfo,
        "lut_health": health,
        "stats": {"reference": m_ref, "source_before": m_src},
        # Per luma band colour for both images, the pixels already decoded
        # and cropped above, no second decode. "the highlights are
        # desaturated" against "the shot has less colour in it" is a number
        # here, not a guess from a scope of everything.
        "bands": {"reference": _bands_u8(ref_crop), "source_before": _bands_u8(src_img)},
        "distance": {"before": before},
        "warnings": warnings,
    }

    if verify:
        after_img = apply_cube(src_img, dest)
        m_after = measure(after_img, reject_blown)
        after = stat_distance(m_after, m_ref)
        result["stats"]["source_after"] = m_after
        result["bands"]["source_after"] = _bands_u8(after_img)
        result["distance"]["after"] = after
        result["distance"]["improved"] = bool(after["total"] < before["total"])
        result["distance"]["improved_colour"] = bool(
            after["colour"] < before["colour"])
        result["distance"]["gain_pct"] = round(
            100.0 * (1.0 - after["total"] / max(before["total"], 1e-9)), 1)
        result["distance"]["gain_colour_pct"] = round(
            100.0 * (1.0 - after["colour"] / max(before["colour"], 1e-9)), 1)
        lost = [i for i in range(3)
                if m_after["tonal_density"][i] < 0.70 * m_src["tonal_density"][i]]
        if lost:
            warnings.append(
                f"posterisation: channels {lost} lost tonal detail. effective "
                f"level density {m_src['tonal_density']} -> "
                f"{m_after['tonal_density']} (1.0 is a perfectly even "
                f"histogram over the range, a hard staircase reads near 0)")
        if m_after["clip_high_pct"] > m_src["clip_high_pct"] + 1.0:
            warnings.append(
                f"clipping highlights: {m_src['clip_high_pct']:.2f}% -> "
                f"{m_after['clip_high_pct']:.2f}% of pixels at the top")
        if m_after["clip_low_pct"] > m_src["clip_low_pct"] + 1.0:
            warnings.append(
                f"crushing shadows: {m_src['clip_low_pct']:.2f}% -> "
                f"{m_after['clip_low_pct']:.2f}% of pixels at the bottom")
        for fam in ("warm", "green", "cool"):
            b0 = m_src["families"][fam]
            a0 = m_after["families"][fam]
            t0f = m_ref["families"][fam]
            # overshoot means the after value ended up on the far side of the
            # reference from where it started, not merely short of it
            if (a0 - t0f) * (b0 - t0f) < 0 and abs(a0 - t0f) > 0.15:
                warnings.append(
                    f"overshoot on {fam}: {100 * b0:.1f}% -> {100 * a0:.1f}% "
                    f"straight past the reference's {100 * t0f:.1f}%. the "
                    f"transform is being asked to fix a content difference")
        if not result["distance"]["improved_colour"]:
            warnings.append(
                "colour distance did not improve. the transform moved the "
                "frame somewhere other than toward the reference, which "
                "usually means the two images have nothing in common")
    if not health["probes"]["grey18"]["dominant_channel_kept"]:
        o = health["probes"]["grey18"]["out"]
        warnings.append(
            f"neutral cast: mid grey 0.39 came out {o}, a spread of "
            f"{max(o) - min(o):.3f}. that is the match tinting neutrals, "
            f"expected against a strongly coloured reference, wrong if you "
            f"wanted the grey card to stay grey")
    if not health["probes_ok"]:
        bad = [k for k, v in health["probes"].items()
               if v["pinned"] or v.get("plausible") is False
               or (k in ("maroon", "skin") and not v["dominant_channel_kept"])]
        detail = "; ".join(
            f"{k} {health['probes'][k]['in']} -> {health['probes'][k]['out']}"
            for k in bad)
        warnings.append(
            f"probe failure on {', '.join(bad)}: a probe changed dominant "
            f"channel, lost the R>=G>=B order that makes skin read as skin, "
            f"or hit a hard 0 / 1. {detail}")
        result["ok"] = False
    if not health["neutral_monotonic"]:
        warnings.append("the neutral ramp is not monotonic, this LUT will band")
        result["ok"] = False
    elif health["shoulder_step"] < 0.15 / (size - 1):
        warnings.append(
            f"highlight compression: the top of the neutral ramp moves "
            f"{health['shoulder_step']:.5f} per LUT node against a nominal "
            f"{1.0 / (size - 1):.5f}, because the fit asked for values past "
            f"white and the shoulder had to open to "
            f"{minfo.get('shoulder', 0):.2f}. tones above that point keep "
            f"their colour but lose separation")
    if health["neutral_flat"]:
        warnings.append(
            f"the neutral ramp goes nearly flat around input "
            f"{health['neutral_flat_at']:.2f} (step "
            f"{health['neutral_min_step']:.5f} against a nominal "
            f"{1.0 / (size - 1):.5f}). tones there will collapse together")
        result["ok"] = False
    result["notes"] = notes_used
    result["elapsed_s"] = round(_time.time() - t0, 2)
    return result


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def _deg(v) -> str:
    return "  n/a  " if v is None or v != v else f"{v:5.1f}d"


def _fam_line(m: dict) -> str:
    f = m["families"]
    return (f"warm {100 * f['warm']:5.1f}%  green {100 * f['green']:5.1f}%  "
            f"cool {100 * f['cool']:5.1f}%  mag {100 * f['magenta']:5.1f}%  "
            f"neutral {100 * f['neutral']:5.1f}%")


def print_report(r: dict) -> None:
    print(f"\n{'=' * 78}")
    print(f"{r['name']}")
    print(f"  reference  {Path(r['reference']).name} ({r['ref_gamut']}) "
          f"crop {r['crop']['x']},{r['crop']['y']} "
          f"{r['crop']['w']}x{r['crop']['h']} "
          f"= v{r['crop']['frac'][1]:.3f}-{r['crop']['frac'][3]:.3f} "
          f"h{r['crop']['frac'][0]:.3f}-{r['crop']['frac'][2]:.3f} "
          f"[{r['crop']['source']}]")
    crops = r.get("crops") or {}
    for label in ("ref", "frame"):
        pick = crops.get(label)
        if not pick:
            continue
        b = pick["box"]
        print(f"  {label + ' pick':10} "
              f"{b[0]:.3f} {b[1]:.3f} {b[2]:.3f} {b[3]:.3f}  "
              f"{pick['area_pct']:.1f}% of the image "
              f"({pick['pixels'][0]}x{pick['pixels'][1]} px)")
    print(f"  source     {r['source']}  preset={r['preset']} "
          f"encode={r['encode']}")
    print(f"  method     {r['method']} strength={r['strength']} "
          f"luma_preserve={r['luma_preserve']}")
    rows = [("reference", r["stats"]["reference"]),
            ("before", r["stats"]["source_before"])]
    if "source_after" in r["stats"]:
        rows.append(("after", r["stats"]["source_after"]))
    print(f"  {'':9} {'luma p5/p25/p50/p75/p95':34} {'sat':>6}  hue families")
    for label, m in rows:
        pct = "/".join(f"{v:.3f}" for v in m["luma_pct"])
        print(f"  {label:9} {pct:34} {m['mean_sat']:6.3f}  {_fam_line(m)}")
    d = r["distance"]
    if "after" in d:
        print(f"  distance   before {d['before']['total']:.4f} -> after "
              f"{d['after']['total']:.4f}  ({d['gain_pct']:+.0f}% closer)   "
              f"colour {d['before']['colour']:.4f} -> {d['after']['colour']:.4f}"
              f"  ({d['gain_colour_pct']:+.0f}%)")
        print(f"             luma {d['before']['luma']:.4f}->{d['after']['luma']:.4f}  "
              f"sat {d['before']['sat']:.4f}->{d['after']['sat']:.4f}  "
              f"hue {d['before']['hue']:.4f}->{d['after']['hue']:.4f}")
    else:
        print(f"  distance   before {d['before']['total']:.4f} (not verified)")
    h = r["lut_health"]
    print(f"  lut        drift {h['identity_drift']:.4f}  black "
          f"{h['black_point']}  white {h['white_point']}  "
          f"monotonic {h['neutral_monotonic']}  zeroed "
          f"{h['zeroed_entries_pct']}% (identity {h['zeroed_identity_pct']}%)  "
          f"pinned {h['pinned_entries_pct']}%")
    print(f"  fit        {json.dumps(r['method_info'])}")
    for k, v in h["probes"].items():
        print(f"  probe {k:8} {v['in']} -> {v['out']}  "
              f"dominant kept {v['dominant_channel_kept']}  "
              f"hue shift {_deg(v['hue_shift_deg'])}  pinned {v['pinned']}")
    print(f"  hue divergence source vs reference: {r['hue_divergence']:.3f} "
          f"(over 0.35 means the two images are not comparable content)")
    for w in r["warnings"]:
        print(f"  WARNING: {w}")
    print(f"  wrote {r['lut']}  ({r['elapsed_s']}s)")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cmd_detect(a) -> None:
    print(f"{'image':22} {'size':11} {'crop x,y,w,h':22} "
          f"{'v-frac':15} {'h-frac':15} area")
    for p in a.images:
        path = Path(p)
        img, gamut = load_reference(path, a.ref_gamut, "rec709a")
        reg = detect_content_region(img)
        f = reg["frac"]
        print(f"{path.name:22} {reg['image'][0]}x{reg['image'][1]:<6} "
              f"{reg['x']},{reg['y']},{reg['w']},{reg['h']:<10} "
              f"{f[1]:.3f}-{f[3]:.3f}    {f[0]:.3f}-{f[2]:.3f}    "
              f"{reg['area_pct']:.1f}%  [{gamut}]")


def cmd_match(a) -> None:
    methods = METHODS if a.method == "both" else (a.method,)
    results = []
    for m in methods:
        r = match_reference(
            ref=a.ref, clip=a.clip, time=a.time, still=a.source_still,
            preset=a.preset, method=m, strength=a.strength,
            luma_preserve=a.luma_preserve, crop=a.crop, crop_frac=a.crop_frac,
            auto_crop=not a.no_auto_crop,
            ref_crop=a.ref_crop, frame_crop=a.frame_crop,
            size=a.size, name=a.name,
            autorotate=not a.no_autorotate, rotation=a.rotate,
            out_dir=a.out_dir, width=a.width,
            ref_gamut=a.ref_gamut, reject_blown=not a.no_reject_blown,
            verify=not a.no_verify)
        results.append(r)
        if not a.quiet:
            print_report(r)
    if a.report:
        Path(a.report).write_text(json.dumps(
            results if len(results) > 1 else results[0], indent=2))
        print(f"report -> {a.report}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("detect", help="print the auto detected content region")
    d.add_argument("images", nargs="+")
    d.add_argument("--ref-gamut", default="auto", choices=["auto", "p3", "srgb"])
    d.set_defaults(fn=cmd_detect)

    m = sub.add_parser("match", help="build a matching LUT")
    m.add_argument("--ref", required=True)
    m.add_argument("--clip")
    m.add_argument("--time", type=float, default=0.0)
    m.add_argument("--source-still")
    m.add_argument("--preset", "-p")
    m.add_argument("--method", default="reinhard",
                   choices=list(METHODS) + ["both"])
    m.add_argument("--strength", type=float, default=1.0,
                   help="0 to 1, baked into the LUT. multiplies with look.mix")
    m.add_argument("--luma-preserve", action="store_true",
                   help="match colour without moving exposure")
    m.add_argument("--crop", help="x,y,w,h in pixels of the reference")
    m.add_argument("--crop-frac", help="x,y,w,h as fractions of the reference")
    m.add_argument("--no-auto-crop", action="store_true")
    m.add_argument("--ref-crop", nargs=4, type=float,
                   metavar=("X0", "Y0", "X1", "Y1"),
                   help="the rectangle to measure on the REFERENCE, as "
                        "fractions of it (0 to 1). Applied before the "
                        "automatic chrome trim, so the trim runs inside it")
    m.add_argument("--frame-crop", nargs=4, type=float,
                   metavar=("X0", "Y0", "X1", "Y1"),
                   help="the rectangle to measure on the FRAME, as fractions "
                        "of the frame as shown at the chosen rotation")
    m.add_argument("--rotate", choices=list(cinegrade.ROTATIONS),
                   help="auto honours the clip's display matrix, 0 ignores "
                        "it, 90/180/270 ignore it and turn the picture "
                        "clockwise (see cinegrade orient)")
    m.add_argument("--no-autorotate", action="store_true",
                   help="alias of --rotate 0")
    m.add_argument("--size", type=int, default=33)
    m.add_argument("--name")
    m.add_argument("--out-dir")
    m.add_argument("--width", type=int, default=960,
                   help="analysis width for the source frame")
    m.add_argument("--ref-gamut", default="auto", choices=["auto", "p3", "srgb"])
    m.add_argument("--no-reject-blown", action="store_true")
    m.add_argument("--no-verify", action="store_true",
                   help="skip applying the LUT back and measuring it")
    m.add_argument("--report", help="write the full JSON report here")
    m.add_argument("--quiet", action="store_true")
    m.set_defaults(fn=cmd_match)

    a = ap.parse_args()
    try:
        a.fn(a)
    except (MatchError, cinegrade.GradeError) as exc:
        sys.exit(f"match_ref: {exc}")


if __name__ == "__main__":
    main()
