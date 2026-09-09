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

# Where "the mask selects this pixel" starts for `luma.min` and `luma.max`
# under a weight (round 4 finding 90).
#
# Every other figure in the luma block is weight proportional: a pixel at
# weight 5e-08 moves the mean, the standard deviation and the percentiles by
# essentially nothing. A min and a max are one pixel each, so on a hard `w > 0`
# support the outermost tail of a feather counts exactly as much as the middle
# of the mask, and a feather is a gaussian whose kernel reaches three sigma
# (`gaussian_blur2d`): a mask feathered at 0.05 of frame width has a support
# some 60 pixels wider than itself on a 400 pixel frame. So the letterboxed
# case these two were narrowed for (a mask on a face reporting min 0.0 from a
# black bar it does not cover) came straight back as soon as the mask was
# feathered, which for a grading mask is the ordinary case.
#
# 0.5 because that is the weight a gaussian leaves at the edge of the shape it
# blurred, so the core of a feathered mask is the shape that was feathered.
# It is a different basis from the weighted figures beside it, which is why
# the printed row and studio/README.md both say so.
MASK_CORE_WEIGHT = 0.5

# Nine edges, eight equal luma bands over 0 to 1, the default `bands()` uses
# when a caller does not hand it its own edges.
DEFAULT_BAND_EDGES = [i / 8.0 for i in range(9)]


# The measurement blocks a normal answer carries. Named once here because the
# "no coverage" answer (below) has to null out exactly these and nothing else.
MEASURED_BLOCKS = ("luma", "saturation", "channels", "families", "clipped",
                   "bands")


def no_coverage_result(definitions: dict | None = None) -> dict:
    """The documented answer for "the mask covers nothing here" (gap 23).

    A matte that covers no pixel of a frame is a NORMAL, expected outcome,
    not a bad request: a sky matte after the camera tilts down has genuinely
    no sky left to measure, and a person matte inside its own tracking gap
    has nobody. `frame_stats` used to raise `StatsError` on it, so every
    script that loops over timestamps had to wrap each row in a try/except
    and checkpoint its own partial results or lose them (which is exactly
    what happened to one round 4 measurement helper, see the checkpoint).

    The shape:

        {"no_coverage": True, "coverage": 0.0,
         "luma": None, "saturation": None, "channels": None,
         "families": None, "clipped": None, "bands": None,
         "definitions": {...}}

    Every measurement block is present and None rather than absent, so a
    caller reading `row["stats"]["luma"]` gets None (falsy, and loud the
    moment it is subscripted) instead of a KeyError, and never a zero that
    could be mistaken for a real measurement of a black frame. `definitions`
    is kept because it describes the formulas, not this frame.
    """
    out = {"no_coverage": True, "coverage": 0.0}
    for name in MEASURED_BLOCKS:
        out[name] = None
    out["definitions"] = definitions if definitions is not None else {
        "sat_floor": SAT_FLOOR,
        "clip_black_code": CLIP_BLACK,
        "clip_white_code": CLIP_WHITE,
        "families": {n: [lo, hi] for n, lo, hi in HUE_FAMILIES},
    }
    return out


def _weighted_percentiles(values: np.ndarray, weight: np.ndarray, qs) -> list[float]:
    """`qs` percentiles (0..100) of `values`, weighted by `weight`.

    Both are already flat and the same length. The "linear" weighted
    percentile: sort once, walk the weighted cumulative distribution (each
    sample credited from the midpoint of its own weight, same convention
    `np.percentile`'s default `linear` method uses on an unweighted array),
    then `np.interp` the requested percentiles off that curve. An all-zero
    weight (nothing selected: a matte with no coverage of this frame) has no
    percentile to report, so the caller (`frame_stats`) checks for that
    before calling this, not here.
    """
    order = np.argsort(values, kind="stable")
    v = values[order]
    w = weight[order].astype(np.float64)
    total = float(w.sum())
    cw = (np.cumsum(w) - 0.5 * w) / total * 100.0
    return [float(x) for x in np.interp(qs, cw, v)]


def _wmean(values: np.ndarray, weight: np.ndarray | None) -> float:
    """mean(values), or the weighted mean when `weight` is given. `weight`
    summing to zero (nothing selected) reads as 0.0 rather than raising:
    the same "empty selection reads as zero" rule `bands()` already used
    for a luma band nothing fell in."""
    if weight is None:
        return float(values.mean()) if values.size else 0.0
    total = float(weight.sum())
    if total <= 0:
        return 0.0
    return float(np.sum(values.astype(np.float64) * weight) / total)


def _wstd(values: np.ndarray, weight: np.ndarray | None, mean: float) -> float:
    """The standard deviation of `values` about `mean`, weighted by `weight`.

    Population form (divided by the total weight, not by one less than it),
    which is what `np.std` returns on an unweighted array, so an all-ones
    weight and no weight at all report the same number instead of two that
    differ by a factor nobody can see. Zero total weight reads as 0.0, the
    same "empty selection reads as zero" rule `_wmean` follows; a caller
    measuring through a matte that covers nothing never reaches here anyway
    (`frame_stats` answers that with `no_coverage`, gap 23).
    """
    if values.size == 0:
        return 0.0
    if weight is None:
        return float(values.std())
    total = float(weight.sum())
    if total <= 0:
        return 0.0
    d = values.astype(np.float64) - float(mean)
    return float(np.sqrt(float(np.sum(weight * d * d)) / total))


def _wsum_pct(selected: np.ndarray, weight: np.ndarray | None, total) -> float:
    """The share of `total` (pixel count, or total weight) that falls in a
    boolean selection, as a percentage: `sum(weight[selected]) / total * 100`
    weighted, or `count(selected) / total * 100` unweighted, one formula
    both `families` and `clipped` below share."""
    if weight is None:
        return float(selected.sum()) / total * 100.0
    return float(weight[selected].sum()) / total * 100.0


def frame_stats(rgb: np.ndarray, weight: np.ndarray | None = None) -> dict:
    """Luma, saturation, hue family and clipping numbers for one rgb24 frame.

    `weight`, given, is an HxW float array in 0..1 the same size as `rgb`
    (contract C6): every percentile, band and hue family below is measured
    over `rgb` weighted by it instead of over the whole frame evenly, which
    is what `cinegrade stats --matte ID` and `POST /api/stats {"matte": ID}`
    use to measure "her face" or "the sky" as the matte says it moves,
    rather than a fixed rectangle that has to be redrawn at every timestamp.
    `region` and `weight` compose: crop `rgb` (and `weight`, to the same
    rectangle) before calling this, this function itself does not crop.

    `weight=None` (the default) is exactly today's unweighted behaviour,
    computed by the original unweighted formulas rather than routed through
    the weighted path with every weight equal to 1: the two give the same
    numbers, but this way a caller that never passes `weight` gets output
    that cannot drift from what it always was.

    A `weight` that sums to zero (a matte with no coverage anywhere in this
    frame, or in `region` if one was applied first) returns
    `no_coverage_result()` above rather than raising (checkpoint gap 23):
    `{"no_coverage": True, "coverage": 0.0}` with every measurement block
    None. There is still nothing to measure and no percentile of an empty
    selection is invented; what changed is that a script looping over
    timestamps gets a row it can write down and carry on, instead of an
    exception on the frame where the sky genuinely left the picture.

    Every WEIGHTED answer, empty or not, also carries `coverage` (the mean
    of the weight over THE MEASURED AREA, which is the region when one is
    given and the whole frame when one is not: 1.0 for a weight of all ones,
    0.25 for a matte covering a quarter of that area solidly, and the same
    0.25 for a matte covering half of it at half strength) and `no_coverage`
    (False on a real measurement). Round 2 finding 81: this used to say "of
    the frame" and the weight is cropped to `region` before the mean is
    taken, so the same matte reads higher inside a tight region than it does
    over the whole frame, and two anchors taken with and without a region are
    not comparable on this number. An UNWEIGHTED call is byte for byte
    what it always was: neither key appears, because neither means anything
    without a mask.

    `luma` carries two spread figures beside its mean (tooling gap 25):
    `std`, the standard deviation of the measured luma, and `p5_p95`, the
    distance from `p5` to `p95`. Both are on the same basis as the mean, so
    a weighted call reports the spread INSIDE the mask and an unweighted one
    the spread of the frame. `min` and `max` are the extremes of the measured
    pixels as well; weighted, they used to be the whole frame's.
    """
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

    w = None
    if weight is not None:
        w = np.asarray(weight, dtype=np.float64)
        if w.shape != y.shape:
            raise StatsError(
                f"weight shape {w.shape} does not match the frame {y.shape} "
                f"(contract C6: weight is HxW at the picture's own size)")
        if float(w.sum()) <= 0.0:
            # Gap 23: an expected outcome, reported, not an exception. The
            # mismatched SHAPE above stays an error, because that is a caller
            # bug rather than something a frame can honestly be.
            return no_coverage_result()

    total = float(y.size) if w is None else float(w.sum())
    coloured = sat >= SAT_FLOOR
    families = {}
    for name, lo, hi in HUE_FAMILIES:
        if lo < hi:
            sel = (hue >= lo) & (hue < hi)
        else:                                   # the warm family wraps past 360
            sel = (hue >= lo) | (hue < hi)
        families[name] = round(_wsum_pct(sel & coloured, w, total), 2)
    families["neutral"] = round(_wsum_pct(~coloured, w, total), 2)

    if w is None:
        p5, p25, p50, p75, p95 = (
            float(v) for v in np.percentile(y, [5, 25, 50, 75, 95]))
        sat_p95 = float(np.percentile(sat, 95))
    else:
        p5, p25, p50, p75, p95 = _weighted_percentiles(
            y.ravel(), w.ravel(), [5, 25, 50, 75, 95])
        (sat_p95,) = _weighted_percentiles(sat.ravel(), w.ravel(), [95])
    raw = rgb
    clipped_black = _wsum_pct(raw.max(-1) <= CLIP_BLACK, w, total)
    clipped_white = _wsum_pct(raw.min(-1) >= CLIP_WHITE, w, total)

    y_mean = _wmean(y, w)
    # The extremes of what was MEASURED, which under a weight is the pixels
    # the weight selects and not the whole frame: a mask on a face in a frame
    # with black bars used to report min 0.0, a number from outside the mask,
    # in a block where every other figure was weighted. Found while adding
    # the two spread figures below, which would otherwise disagree with the
    # min and max sitting next to them.
    #
    # "Selects" is MASK_CORE_WEIGHT and above, not any weight above zero
    # (round 4 finding 90): a feather's outer tail weighs nothing in every
    # other figure here and would weigh everything in these two.
    if w is None:
        y_sel = y
    else:
        core = w >= MASK_CORE_WEIGHT
        if not core.any():
            # A mask that never reaches the core (a very soft key, a feather
            # wider than the shape it feathers) still has pixels it selects
            # more than any other, and they are still the mask's own rather
            # than the frame's. `w.sum() > 0` above guarantees this is not
            # empty.
            core = w >= float(w.max())
        y_sel = y[core]
    out = {
        "luma": {
            "p5": round(p5, 4), "p25": round(p25, 4), "p50": round(p50, 4),
            "p75": round(p75, 4), "p95": round(p95, 4),
            "mean": round(y_mean, 4),
            "mean8": round(y_mean * 255.0, 1),
            # Tooling gap 25. Two figures for how spread out the luma inside
            # the measurement is, on the same basis as the mean beside them:
            # `std` over every measured pixel, `p5_p95` the distance between
            # the two percentiles already in this block. A mean can sit still
            # while both of these collapse (a contrast reduction pulls every
            # pixel toward the pivot), which is the case that read as "no
            # change" on a stats row that carried the mean alone.
            "std": round(_wstd(y, w, y_mean), 4),
            "p5_p95": round(p95 - p5, 4),
            "min": round(float(y_sel.min()), 4),
            "max": round(float(y_sel.max()), 4),
        },
        "saturation": {
            "mean": round(_wmean(sat, w), 4),
            "mean_coloured": round(
                _wmean(sat[coloured], None if w is None else w[coloured])
                if coloured.any() else 0.0, 4),
            "p95": round(sat_p95, 4),
        },
        "channels": {
            "r": round(_wmean(r, w), 4),
            "g": round(_wmean(g, w), 4),
            "b": round(_wmean(b, w), 4),
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
        "bands": bands(rgb, weight=w),
    }
    if w is not None:
        # Gap 23 / gap 19: a weighted answer says how much of the MEASURED
        # AREA it measured (the region when one was given, the whole frame
        # otherwise: `w` was cropped with the picture above), so a number and
        # the mask it came from travel together.
        # Absent on an unweighted call on purpose: every envelope written
        # before this change is unchanged there.
        out["coverage"] = round(float(w.mean()), 6)
        out["no_coverage"] = False
    return out


# --------------------------------------------------------------------------
# bands: per luma band colour, so "the highlights are desaturated" is a
# number instead of a guess from a scope of everything (bakeoff finding B6)
# --------------------------------------------------------------------------

def bands(rgb: np.ndarray, edges: list[float] | None = None,
         weight: np.ndarray | None = None) -> dict:
    """Eight equal luma bands by default, each one's mean saturation, warm,
    tint and pixel count.

    Uses the same BT.709 luma frame_stats does, so a band boundary here is
    the same brightness a luma percentile means elsewhere in the same dict.
    `warm` is mean(R) - mean(B) inside the band; `tint` is mean(G) -
    (mean(R) + mean(B)) / 2, both signed and both zero for a neutral band.
    An empty band (no pixel of the frame falls in it) reports zeros rather
    than a NaN from an empty-slice mean, so a caller can print every band
    without checking count first.

    `weight`, given (contract C6: HxW, same size as `rgb`, already validated
    by `frame_stats` when it calls this), weights `saturation`, `warm` and
    `tint` within each band by it. `count` stays the plain pixel count
    either way, so it keeps meaning "how many pixels sit in this band" and
    not "how much weight": a band a matte barely touches still shows its
    real pixel count, with the weighted color numbers showing that the
    matte itself contributed almost nothing. A band whose weight inside it
    sums to zero (pixels present, but the matte covers none of them) reads
    as empty the same way a band with zero pixels does.

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
        band_weight = None if weight is None else weight[sel]
        empty = c == 0 or (band_weight is not None and float(band_weight.sum()) <= 0.0)
        if empty:
            saturation.append(0.0)
            warm.append(0.0)
            tint.append(0.0)
            continue
        saturation.append(round(_wmean(sat[sel], band_weight), 4))
        rm = _wmean(r[sel], band_weight)
        gm = _wmean(g[sel], band_weight)
        bm = _wmean(b[sel], band_weight)
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
