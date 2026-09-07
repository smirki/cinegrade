"""grade/stats.py - the studio's own frame measurement, in one place.

`frame_stats()` used to live inside studio/server.py. Seven of ten bakeoff
agents hand carried their own copy of it into their own tooling because
nothing else measured a still the way the studio does, and none of those
copies agreed with each other or with the server. It moved here unchanged in
what it returns, so studio/server.py imports it instead of defining it, and
`cinegrade stats` / `cinegrade sweep` and studio/tools/grade_client.py read a
frame through the exact same numbers the browser does.

    from stats import frame_stats, bands, decode_image

`decode_image()` is the one decode function every caller shares: the CLI's
`stats --image` and `sweep --sheet`, the client module, and the server's
read-only `path` field on /api/frame and /api/stats. One bounded ffmpeg rgb24
call, nothing decodes a still or a video frame any other way.

Measurements only. Nothing here scores or ranks a result; a caller that wants
that is the wrong caller for this file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

import cinegrade as CG   # sibling module in grade/: probe and region math only


class StatsError(Exception):
    """A bad still, a bad region, or a file ffmpeg cannot decode."""


# --------------------------------------------------------------------------
# frame_stats: moved from studio/server.py, unchanged in what it returns
# --------------------------------------------------------------------------

# Hue families, in degrees. The boundaries are stated here and shown in the UI
# so the warm/cool/green split is a definition the user can check, not a number
# that appeared from somewhere.
HUE_FAMILIES = [
    ("warm", 345.0, 75.0),
    ("green", 75.0, 165.0),
    ("cool", 165.0, 285.0),
    ("magenta", 285.0, 345.0),
]
SAT_FLOOR = 0.10          # below this a pixel counts as neutral, not coloured
CLIP_BLACK = 2            # 8-bit code at or under which a pixel reads as crushed
CLIP_WHITE = 253

# Nine edges, eight equal luma bands over 0 to 1, the default `bands()` uses
# when a caller does not hand it its own edges.
DEFAULT_BAND_EDGES = [i / 8.0 for i in range(9)]


def frame_stats(rgb: np.ndarray) -> dict:
    a = rgb.astype(np.float32) / 255.0
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b

    mx = a.max(-1)
    mn = a.min(-1)
    delta = mx - mn
    sat = np.where(mx > 1e-6, delta / np.maximum(mx, 1e-6), 0.0)

    hue = np.zeros_like(mx)
    nz = delta > 1e-9
    i = nz & (mx == r)
    hue[i] = ((g - b)[i] / delta[i]) % 6.0
    i = nz & (mx == g) & (mx != r)
    hue[i] = ((b - r)[i] / delta[i]) + 2.0
    i = nz & (mx == b) & (mx != r) & (mx != g)
    hue[i] = ((r - g)[i] / delta[i]) + 4.0
    hue = (hue * 60.0) % 360.0

    total = float(y.size)
    coloured = sat >= SAT_FLOOR
    families = {}
    for name, lo, hi in HUE_FAMILIES:
        if lo < hi:
            sel = (hue >= lo) & (hue < hi)
        else:                                   # the warm family wraps past 360
            sel = (hue >= lo) | (hue < hi)
        families[name] = round(float((sel & coloured).sum()) / total * 100.0, 2)
    families["neutral"] = round(float((~coloured).sum()) / total * 100.0, 2)

    p5, p25, p50, p75, p95 = (float(v) for v in np.percentile(y, [5, 25, 50, 75, 95]))
    raw = rgb
    clipped_black = float((raw.max(-1) <= CLIP_BLACK).sum()) / total * 100.0
    clipped_white = float((raw.min(-1) >= CLIP_WHITE).sum()) / total * 100.0

    return {
        "luma": {
            "p5": round(p5, 4), "p25": round(p25, 4), "p50": round(p50, 4),
            "p75": round(p75, 4), "p95": round(p95, 4),
            "mean": round(float(y.mean()), 4),
            "mean8": round(float(y.mean()) * 255.0, 1),
            "min": round(float(y.min()), 4), "max": round(float(y.max()), 4),
        },
        "saturation": {
            "mean": round(float(sat.mean()), 4),
            "mean_coloured": round(float(sat[coloured].mean()) if coloured.any() else 0.0, 4),
            "p95": round(float(np.percentile(sat, 95)), 4),
        },
        "channels": {
            "r": round(float(r.mean()), 4),
            "g": round(float(g.mean()), 4),
            "b": round(float(b.mean()), 4),
        },
        "families": families,
        "clipped": {
            "black": round(clipped_black, 3),
            "white": round(clipped_white, 3),
        },
        "definitions": {
            "sat_floor": SAT_FLOOR,
            "clip_black_code": CLIP_BLACK,
            "clip_white_code": CLIP_WHITE,
            "families": {n: [lo, hi] for n, lo, hi in HUE_FAMILIES},
        },
        "bands": bands(rgb),
    }


# --------------------------------------------------------------------------
# bands: per luma band colour, so "the highlights are desaturated" is a
# number instead of a guess from a scope of everything (bakeoff finding B6)
# --------------------------------------------------------------------------

def bands(rgb: np.ndarray, edges: list[float] | None = None) -> dict:
    """Eight equal luma bands by default, each one's mean saturation, warm,
    tint and pixel count.

    Uses the same BT.709 luma frame_stats does, so a band boundary here is
    the same brightness a luma percentile means elsewhere in the same dict.
    `warm` is mean(R) - mean(B) inside the band; `tint` is mean(G) -
    (mean(R) + mean(B)) / 2, both signed and both zero for a neutral band.
    An empty band (no pixel of the frame falls in it) reports zeros rather
    than a NaN from an empty-slice mean, so a caller can print every band
    without checking count first.

    Measurement only: no band is flagged good or bad here, and nothing here
    compares one band against another.
    """
    if edges is None:
        edges = DEFAULT_BAND_EDGES
    edges = [float(e) for e in edges]
    if len(edges) < 2:
        raise StatsError(f"bands needs at least two edges, got {len(edges)}")

    a = rgb.astype(np.float32) / 255.0
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    mx = a.max(-1)
    mn = a.min(-1)
    sat = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0)

    n = len(edges) - 1
    saturation, warm, tint, count = [], [], [], []
    for i in range(n):
        lo, hi = edges[i], edges[i + 1]
        # The top band is closed on both ends so a pixel at luma exactly 1.0
        # is not silently dropped by every band being half-open.
        sel = (y >= lo) & (y <= hi) if i == n - 1 else (y >= lo) & (y < hi)
        c = int(sel.sum())
        count.append(c)
        if c == 0:
            saturation.append(0.0)
            warm.append(0.0)
            tint.append(0.0)
            continue
        saturation.append(round(float(sat[sel].mean()), 4))
        rm, gm, bm = float(r[sel].mean()), float(g[sel].mean()), float(b[sel].mean())
        warm.append(round(rm - bm, 4))
        tint.append(round(gm - 0.5 * (rm + bm), 4))
    return {
        "edges": [round(e, 4) for e in edges],
        "saturation": saturation,
        "warm": warm,
        "tint": tint,
        "count": count,
    }


# --------------------------------------------------------------------------
# decode_image: the one decode function a still or a video frame goes through
# --------------------------------------------------------------------------

# Still extensions, the same set cinegrade.py's own contact sheet loader
# (_SHEET_IMAGE_EXTS) uses to tell a still from a video by name. ffmpeg
# demuxes any of these through image2, which reports a fabricated ~0.04s
# "duration" and produces zero bytes if asked to seek into it at all: a
# still has no later frame to seek to, seeking one is simply empty.
STILL_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}


def decode_image(path, width: int | None = None, region=None,
                 time: float = 0.0, rotation="auto") -> np.ndarray:
    """One bounded ffmpeg rgb24 call, into a (h, w, 3) uint8 array.

    Works on a plain still (`time` is then ignored: a still has one frame and
    no later one to seek to, so this never asks ffmpeg to seek into one) or
    on one frame of a video at `time` seconds. `width` scales the long side
    down (aspect kept, even dimensions); with no width the file's own probed
    size is used. `region` crops the decoded frame afterwards, as four
    fractions [x0, y0, x1, y1] of it, the same meaning `cinegrade still
    --region` gives. `rotation` follows cinegrade's own rotation strings
    ("auto", "0", "90", "180", "270"); auto honours the file's own display
    tag.

    This is the one decode function the CLI, the client module and the
    server's `path` field all call: a still measured through the CLI and the
    same still measured through the server are the same numbers because they
    are the same bytes.
    """
    path = Path(path)
    if not path.is_file():
        raise StatsError(f"file not found: {path}")
    try:
        info = CG.probe(str(path), rotation=rotation)
    except subprocess.CalledProcessError as exc:
        raise StatsError(f"ffprobe could not read {path}: {exc}") from exc
    w, h = int(info["width"]), int(info["height"])
    if width:
        w = max(2, int(width) // 2 * 2)
        h = max(2, int(round(info["height"] * w / info["width"] / 2)) * 2)

    parts = []
    if width:
        parts.append(f"scale={w}:{h}:flags=bicubic")
    parts.append("format=rgb24")
    vf = CG.rotate_prefix(info) + ",".join(parts)

    still = path.suffix.lower() in STILL_EXTS
    args = ["ffmpeg", "-v", "error", "-y"] + CG.rotate_args(rotation)
    if time and not still:
        args += ["-ss", str(float(time))]
    args += ["-i", str(path), "-vf", vf, "-frames:v", "1", "-t", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    proc = subprocess.run(args, capture_output=True, timeout=30)
    want = w * h * 3
    if proc.returncode != 0 or len(proc.stdout) < want:
        raise StatsError(
            f"ffmpeg could not decode {path}:\n"
            f"{proc.stderr.decode('utf-8', 'replace')[-800:]}")
    rgb = np.frombuffer(proc.stdout[:want], np.uint8).reshape(h, w, 3)
    if region is not None:
        reg = CG.normalise_region(region)
        x, y, cw, ch = CG.region_pixels(reg, {"width": w, "height": h})
        rgb = np.ascontiguousarray(rgb[y:y + ch, x:x + cw])
    return rgb
