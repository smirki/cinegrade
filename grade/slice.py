"""Color Slice, Tetra and the hue curves: the numpy reference and the bake.

Three look tools that share one property: each is a pure function of a single
pixel's RGB, with no neighbourhood and no geometry. That is exactly the class
the secondary already exploits, so all three are evaluated once on a 33 point
identity grid, written to one .cube, and applied by ffmpeg's `lut3d` with
tetrahedral interpolation and by the GPU's `tetra()` with the same
decomposition. The numpy code here is the reference; ffmpeg and the shader are
measured against it, never the other way round.

Where in the chain
------------------
After CURVES and before the layers (before the secondary on a tree that still
has one). The signal at that point is display referred Rec.709 code in
whatever `convert.encode` produced, which is the same signal the secondary's
qualifier reads, which is why the identity grid is a valid stand-in for the
picture.

The colour model, stated once
-----------------------------
Hue and saturation come from `colorlib.rgb_to_hsv`: H in degrees, S as
(max - min) / max, V as max. That is the same H and S the secondary keys on.

Two different luminances appear, and the difference is deliberate:

  * The quantity that gets MULTIPLIED (density, and the Hue vs Lum curve) is
    HSV V. At a fixed hue and saturation every RGB channel is exactly linear
    in V, so scaling V is a pure RGB scale: it changes brightness and touches
    neither hue nor saturation. It is also the quantity `secondary.lum_gain`
    multiplies today, so "lum" means the same thing in both stages.
  * The quantity used as the X AXIS of Lum vs Sat is Rec.709 luma
    (0.2126 R + 0.7152 G + 0.0722 B, `colorlib.luma709`). That is the quantity
    the secondary's `lum_low` / `lum_high` qualifier reads, so "how bright is
    this pixel" means the same thing in both stages too. V is a channel
    maximum and would call a saturated blue as bright as white.

Everything defaults to the value that reproduces today's output exactly: the
curve lists are empty, every vector is hue 0 / sat 1 / density 0, and the
tetra deltas are zero. `is_identity` reports that state so `build_graph` can
leave the stage out of the tree altogether rather than baking an identity cube
and paying for a lut3d that does nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
LUT_SLICE = ROOT / "luts" / "slice"

sys.path.insert(0, str(ROOT / "tools"))
import colorlib as C          # noqa: E402


# The secondary bakes a 33 cube; so does this. Same size means the same
# quantisation error budget, and the same tetrahedral interpolation on both
# sides, so nothing here can be blamed on a table finer or coarser than the
# one the rest of the chain already trusts.
LUT_SIZE = 33

# The skin vector's centre, in degrees.
#
# Measured, not chosen. The engine already carries one canonical skin colour:
# `grade/tools/match_ref.py` PROBES["skin"] = (0.55, 0.40, 0.32), the swatch
# match_ref refuses to let a match break. Its HSV hue is 20.8696 degrees.
#
# Checked against real footage rather than trusted on its own: on
# footage/A001_09011336_C002.MOV at 2 s and footage/A001_09011832_C003.MOV at
# 8 s, graded at defaults and rendered 640 wide, the pixels whose chroma sits
# within 0.06 of that probe (brightness normalised out) have a circular mean
# hue of 16.159 and 12.894 degrees respectively, and 100 percent of them fall
# inside this vector's 60 degree support. See cases_slice.test_skin_vector.
SKIN_HUE = 20.8696

# The six chromatic vectors sit on the RGB/CMY hue wheel; skin is the seventh
# and deliberately overlaps red and yellow.
VECTOR_CENTRES = (
    ("red", 0.0), ("yellow", 60.0), ("green", 120.0),
    ("cyan", 180.0), ("blue", 240.0), ("magenta", 300.0),
    ("skin", SKIN_HUE),
)

# Distance in degrees from a vector's centre to where its weight reaches zero.
# 60 is the spacing between the six chromatic centres, which makes their raised
# cosines a partition of unity: at any hue the six weights sum to exactly 1
# before the saturation scale, so a uniform push on all six is a global push
# and not a lumpy one. Skin breaks that sum on purpose, the way Resolve's
# seventh vector does.
VECTOR_HALF_WIDTH = 60.0

# The six cube corners Tetra moves, with the corner they start at. Black
# (0, 0, 0) and white (1, 1, 1) are absent because they are pinned.
TETRA_CORNERS = (
    ("r", (1.0, 0.0, 0.0)), ("g", (0.0, 1.0, 0.0)), ("b", (0.0, 0.0, 1.0)),
    ("c", (0.0, 1.0, 1.0)), ("m", (1.0, 0.0, 1.0)), ("y", (1.0, 1.0, 0.0)),
)

# Each curve's axis and its neutral y, which is the value an empty list means.
# A hue offset of 0 changes nothing; a multiplier of 1 changes nothing. The
# neutral is NOT y = x: these curves are corrections, not transfer functions,
# so the flat line through the neutral is the identity and the empty list is
# that flat line without the user having to place two points to say so.
CURVE_AXES = {
    "hue_hue": {"x": "hue", "periodic": True, "neutral": 0.0,
                "unit": "turns of hue offset"},
    "hue_sat": {"x": "hue", "periodic": True, "neutral": 1.0,
                "unit": "saturation multiplier"},
    "hue_lum": {"x": "hue", "periodic": True, "neutral": 1.0,
                "unit": "luminance (HSV V) multiplier"},
    "lum_sat": {"x": "luma709", "periodic": False, "neutral": 1.0,
                "unit": "saturation multiplier"},
    "sat_sat": {"x": "hsv_s", "periodic": False, "neutral": 1.0,
                "unit": "saturation multiplier"},
}
CURVE_KEYS = ("hue_hue", "hue_sat", "hue_lum", "lum_sat", "sat_sat")


# --------------------------------------------------------------------------
# PCHIP, the same spline the rest of the app draws and renders
# --------------------------------------------------------------------------
#
# Fritsch and Carlson monotone cubic, with scipy's `_edge_case` at the two
# ends. That is what ffmpeg's `curves` filter calls pchip, what
# controls.js pchipEval draws and what gpu.js pchipLut bakes, so a curve drawn
# here is the curve that renders. The periodic variant never reaches the edge
# case: on a closed loop every node has a real neighbour on both sides, so the
# ordinary weighted harmonic mean applies everywhere and the ends are not ends.

def _sgn(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _edge_slope(h0: float, h1: float, m0: float, m1: float) -> float:
    """scipy's _edge_case: a one sided three point estimate, clamped."""
    d = ((2.0 * h0 + h1) * m0 - h0 * m1) / (h0 + h1)
    if _sgn(d) != _sgn(m0):
        return 0.0
    if _sgn(m0) != _sgn(m1) and abs(d) > 3.0 * abs(m0):
        return 3.0 * m0
    return d


def _interior_slopes(h: np.ndarray, d: np.ndarray, n: int) -> np.ndarray:
    """The weighted harmonic mean of the two neighbouring secants."""
    m = np.zeros(n, dtype=np.float64)
    for k in range(1, n - 1):
        if d[k - 1] * d[k] <= 0.0:
            continue
        w1 = 2.0 * h[k] + h[k - 1]
        w2 = h[k] + 2.0 * h[k - 1]
        m[k] = (w1 + w2) / (w1 / d[k - 1] + w2 / d[k])
    return m


def _clean_points(pts) -> list[list[float]]:
    """Sorted [x, y] pairs with duplicate x values collapsed to the last one."""
    out = []
    for p in (pts or []):
        out.append([float(p[0]), float(p[1])])
    out.sort(key=lambda p: p[0])
    kept: list[list[float]] = []
    for p in out:
        if kept and abs(p[0] - kept[-1][0]) < 1e-9:
            kept[-1] = p
        else:
            kept.append(p)
    return kept


def _hermite(xs, x0, x1, y0, y1, m0, m1):
    h = x1 - x0
    t = (xs - x0) / h
    t2 = t * t
    t3 = t2 * t
    return ((2 * t3 - 3 * t2 + 1) * y0 + (t3 - 2 * t2 + t) * h * m0
            + (-2 * t3 + 3 * t2) * y1 + (t3 - t2) * h * m1)


def pchip_eval(pts, xs, neutral: float) -> np.ndarray:
    """Open ended pchip. Empty means neutral everywhere, one point means flat.

    Outside the point range the value is held at the nearest endpoint, which is
    what controls.js does for three or more points. Two points extrapolate the
    line, which is what ffmpeg does and what the editor draws.
    """
    xs = np.asarray(xs, dtype=np.float64)
    p = _clean_points(pts)
    n = len(p)
    if n == 0:
        return np.full(xs.shape, float(neutral))
    if n == 1:
        return np.full(xs.shape, p[0][1])
    px = np.array([q[0] for q in p], dtype=np.float64)
    py = np.array([q[1] for q in p], dtype=np.float64)
    h = np.maximum(1e-9, np.diff(px))
    d = np.diff(py) / h
    if n == 2:
        return py[0] + (xs - px[0]) * d[0]
    m = _interior_slopes(h, d, n)
    m[0] = _edge_slope(h[0], h[1], d[0], d[1])
    m[n - 1] = _edge_slope(h[n - 2], h[n - 3], d[n - 2], d[n - 3])

    idx = np.clip(np.searchsorted(px, xs, side="right") - 1, 0, n - 2)
    out = _hermite(xs, px[idx], px[idx + 1], py[idx], py[idx + 1], m[idx], m[idx + 1])
    out = np.where(xs <= px[0], py[0], out)
    out = np.where(xs >= px[-1], py[-1], out)
    return out


def pchip_eval_periodic(pts, xs, neutral: float, period: float = 1.0) -> np.ndarray:
    """Closed loop pchip: the curve wraps, so 0 and 1 are the same place.

    The point list is extended by one copy of the last point shifted back a
    period and one copy of the first shifted forward, which gives every real
    node two real neighbours. No end slope rule is needed or used, because a
    loop has no ends.
    """
    xs = np.asarray(xs, dtype=np.float64)
    p = _clean_points([[float(q[0]) % period, float(q[1])] for q in (pts or [])])
    n = len(p)
    if n == 0:
        return np.full(xs.shape, float(neutral))
    if n == 1:
        return np.full(xs.shape, p[0][1])

    px = np.array([q[0] for q in p], dtype=np.float64)
    py = np.array([q[1] for q in p], dtype=np.float64)
    ex = np.concatenate([[px[-1] - period], px, [px[0] + period]])
    ey = np.concatenate([[py[-1]], py, [py[0]]])
    en = len(ex)
    h = np.maximum(1e-9, np.diff(ex))
    d = np.diff(ey) / h
    m = _interior_slopes(h, d, en)

    # Fold every query into the one period the extended nodes cover.
    w = ((xs - ex[0]) % period) + ex[0]
    idx = np.clip(np.searchsorted(ex, w, side="right") - 1, 0, en - 2)
    return _hermite(w, ex[idx], ex[idx + 1], ey[idx], ey[idx + 1], m[idx], m[idx + 1])


def curve_eval(key: str, pts, xs) -> np.ndarray:
    """Evaluate one named hue curve on its own axis, honouring its neutral."""
    axis = CURVE_AXES[key]
    if axis["periodic"]:
        return pchip_eval_periodic(pts, xs, axis["neutral"])
    return pchip_eval(pts, np.clip(np.asarray(xs, dtype=np.float64), 0.0, 1.0),
                      axis["neutral"])


# --------------------------------------------------------------------------
# identity tests
# --------------------------------------------------------------------------

def curve_is_identity(key: str, pts) -> bool:
    """True when this list cannot move a pixel.

    An empty list is the identity by definition. A list whose every y sits on
    the neutral is the identity too, because pchip through equal values is a
    horizontal line, which is why a user who drew a curve and then flattened it
    gets the stage removed rather than a wasted lut3d.
    """
    p = _clean_points(pts)
    if not p:
        return True
    neutral = CURVE_AXES[key]["neutral"]
    return all(abs(q[1] - neutral) < 1e-9 for q in p)


def hue_curves_is_identity(hc) -> bool:
    hc = hc or {}
    if not hc.get("enabled"):
        return True
    return all(curve_is_identity(k, hc.get(k)) for k in CURVE_KEYS)


def tetra_is_identity(te) -> bool:
    te = te or {}
    if not te.get("enabled"):
        return True
    for name, _ in TETRA_CORNERS:
        v = te.get(name) or [0.0, 0.0, 0.0]
        if any(abs(float(c)) > 1e-9 for c in v):
            return False
    return True


def vectors_are_identity(sl) -> bool:
    sl = sl or {}
    if abs(float(sl.get("density", 0.0))) > 1e-9:
        return False
    vecs = sl.get("vectors") or {}
    for name, _ in VECTOR_CENTRES:
        p = vecs.get(name) or {}
        if (abs(float(p.get("hue", 0.0))) > 1e-9
                or abs(float(p.get("sat", 1.0)) - 1.0) > 1e-9
                or abs(float(p.get("density", 0.0))) > 1e-9):
            return False
    return True


def slice_is_identity(sl) -> bool:
    sl = sl or {}
    if not sl.get("enabled"):
        return True
    return vectors_are_identity(sl) and tetra_is_identity(sl.get("tetra"))


def is_identity(cfg) -> bool:
    """True when the whole stage would bake an identity cube."""
    cfg = cfg or {}
    return (hue_curves_is_identity(cfg.get("hue_curves"))
            and slice_is_identity(cfg.get("slice")))


# --------------------------------------------------------------------------
# the three tools
# --------------------------------------------------------------------------

def apply_hue_curves(rgb: np.ndarray, hc: dict) -> np.ndarray:
    """Five curves, all read off the SOURCE pixel.

    Every curve takes its x from the pixel as it arrived, not from the pixel a
    previous curve already moved. Otherwise Hue vs Sat would read a hue Hue vs
    Hue had just rotated, and the two controls would fight in a way no user
    could predict from the graphs on screen.
    """
    rgb = np.asarray(rgb, dtype=np.float64)
    h, s, v = C.rgb_to_hsv(rgb)
    lum = C.luma709(rgb)[..., 0]
    hx = h / 360.0

    h2 = h + 360.0 * curve_eval("hue_hue", hc.get("hue_hue"), hx)
    s2 = s * curve_eval("hue_sat", hc.get("hue_sat"), hx)
    v2 = v * curve_eval("hue_lum", hc.get("hue_lum"), hx)
    s2 = s2 * curve_eval("lum_sat", hc.get("lum_sat"), lum)
    s2 = s2 * curve_eval("sat_sat", hc.get("sat_sat"), s)
    return C.hsv_to_rgb(h2, np.clip(s2, 0.0, 1.0), np.clip(v2, 0.0, None))


def vector_weight(hue: np.ndarray, sat: np.ndarray, centre: float) -> np.ndarray:
    """A raised cosine on hue distance, scaled by the pixel's saturation.

    The saturation scale is what keeps the six vectors off greys: a neutral
    pixel has no hue to steer, and a tool that moved it would turn every grey
    card into whichever vector happened to win the rounding.
    """
    d = np.abs(((hue - centre + 180.0) % 360.0) - 180.0)
    cosw = np.where(d >= VECTOR_HALF_WIDTH, 0.0,
                    0.5 * (1.0 + np.cos(np.pi * np.clip(d, 0.0, VECTOR_HALF_WIDTH)
                                        / VECTOR_HALF_WIDTH)))
    return cosw * sat


def apply_slice(rgb: np.ndarray, sl: dict) -> np.ndarray:
    """Six vectors plus skin, plus the global density.

    Hue offsets add, saturation gains and densities multiply, and every vector
    reads the same source hue and saturation, so the vectors are parallel
    rather than serial and reordering the dict cannot change the picture.

    Density is C2's formula exactly: L' = L * (1 - density * S * weight), with
    `weight` the saturation scaled vector weight above, and weight 1 for the
    global control. The per vector form therefore carries saturation twice
    (once in the weight, once written out in the formula) while the global form
    carries it once, so at equal settings the global control bites harder on a
    partly saturated pixel. That is the contract as written; the ratio is
    measured in the honesty list rather than quietly smoothed away.
    """
    rgb = np.asarray(rgb, dtype=np.float64)
    h, s, v = C.rgb_to_hsv(rgb)
    hue_delta = np.zeros_like(h)
    sat_mul = np.ones_like(h)
    lum_mul = np.ones_like(h)

    vecs = sl.get("vectors") or {}
    for name, centre in VECTOR_CENTRES:
        p = vecs.get(name) or {}
        hue_g = float(p.get("hue", 0.0))
        sat_g = float(p.get("sat", 1.0))
        den = float(p.get("density", 0.0))
        if abs(hue_g) < 1e-12 and abs(sat_g - 1.0) < 1e-12 and abs(den) < 1e-12:
            continue
        w = vector_weight(h, s, centre)
        if hue_g:
            hue_delta = hue_delta + hue_g * w
        if sat_g != 1.0:
            sat_mul = sat_mul * (1.0 + (sat_g - 1.0) * w)
        if den:
            lum_mul = lum_mul * (1.0 - den * s * w)

    glob = float(sl.get("density", 0.0))
    if glob:
        lum_mul = lum_mul * (1.0 - glob * s)

    return C.hsv_to_rgb(h + hue_delta,
                        np.clip(s * sat_mul, 0.0, 1.0),
                        np.clip(v * lum_mul, 0.0, None))


def apply_tetra(rgb: np.ndarray, te: dict) -> np.ndarray:
    """Move six cube corners and interpolate the inside tetrahedrally.

    The standard six tetrahedra decomposition of the unit cube, the same one
    `lut3d=interp=tetrahedral` and gpu.js `tetra()` use, so the tool and the
    interpolation that carries it are the same geometry. Black and white are
    pinned: every tetrahedron has both of them as vertices, so a pinned pair
    means no setting can tint a neutral or lift the black point, which is the
    whole reason to reach for Tetra instead of six vector pushes.

    Inputs clamp to [0, 1]; outside the cube there are no corners to weigh.
    """
    x = np.clip(np.asarray(rgb, dtype=np.float64), 0.0, 1.0)
    r, g, b = x[..., 0], x[..., 1], x[..., 2]

    corner = {}
    for name, base in TETRA_CORNERS:
        d = te.get(name) or [0.0, 0.0, 0.0]
        corner[name] = np.asarray(base, dtype=np.float64) + np.asarray(
            [float(d[0]), float(d[1]), float(d[2])], dtype=np.float64)
    white = np.array([1.0, 1.0, 1.0])

    conds = [
        (r > g) & (g > b),
        (r > g) & (g <= b) & (r > b),
        (r > g) & (g <= b) & (r <= b),
        (r <= g) & (b > g),
        (r <= g) & (b <= g) & (b > r),
        (r <= g) & (b <= g) & (b <= r),
    ]
    z = np.zeros_like(r)

    def sel(*vals):
        return np.select(conds, list(vals), default=z)

    # The black corner's weight is computed and then dropped: it is pinned at
    # (0, 0, 0), so its contribution is zero however heavy the weight is.
    w_r = sel(r - g, r - b, z, z, z, z)              # (1, 0, 0)
    w_y = sel(g - b, z, z, z, z, r - b)              # (1, 1, 0)
    w_m = sel(z, b - g, r - g, z, z, z)              # (1, 0, 1)
    w_b = sel(z, z, b - r, b - g, z, z)              # (0, 0, 1)
    w_c = sel(z, z, z, g - r, b - r, z)              # (0, 1, 1)
    w_g = sel(z, z, z, z, g - b, g - r)              # (0, 1, 0)
    w_w = sel(b, g, g, r, r, b)                      # (1, 1, 1)

    out = (w_r[..., None] * corner["r"] + w_y[..., None] * corner["y"]
           + w_m[..., None] * corner["m"] + w_b[..., None] * corner["b"]
           + w_c[..., None] * corner["c"] + w_g[..., None] * corner["g"]
           + w_w[..., None] * white)
    return np.clip(out, 0.0, 1.0)


# --------------------------------------------------------------------------
# the bake
# --------------------------------------------------------------------------

def apply_all(rgb: np.ndarray, cfg: dict) -> np.ndarray:
    """The whole stage on arbitrary RGB, in pipeline order.

    Hue curves first (they are the broad shaping), then the slice vectors and
    the global density, then Tetra last, because Tetra is a corner move on the
    cube the other two just produced and applying it first would let a later
    hue rotation drag a pinned corner off its pin.
    """
    out = np.asarray(rgb, dtype=np.float64)
    hc = (cfg or {}).get("hue_curves") or {}
    sl = (cfg or {}).get("slice") or {}
    if hc.get("enabled") and not hue_curves_is_identity(hc):
        out = apply_hue_curves(out, hc)
    if sl.get("enabled"):
        if not vectors_are_identity(sl):
            out = apply_slice(out, sl)
        te = sl.get("tetra") or {}
        if te.get("enabled") and not tetra_is_identity(te):
            out = apply_tetra(out, te)
    return np.clip(out, 0.0, 1.0)


def bake(cfg: dict, size: int = LUT_SIZE) -> np.ndarray:
    """The stage as a (size**3, 3) cube in .cube order, red varying fastest.

    Identity in, identity out: with nothing enabled this returns the grid
    unchanged, which is what makes `is_identity` safe to trust.
    """
    return apply_all(C.identity_grid(size), cfg)


def _cache_key(cfg: dict, size: int = LUT_SIZE) -> str:
    """Hash the settings AND the cube size.

    Size is in the key because it was once left out, and the bug was invisible
    in normal use and completely silent when it bit: every caller asks for the
    default 33, so the first measurement that asked for 17 and 65 got the
    cached 33-point file back three times and printed three identical error
    figures (max 2.216 of 255 for all three sizes), which reads as "cube size
    does not matter" rather than as "the cache ignored you".
    """
    hc = (cfg or {}).get("hue_curves") or {}
    sl = (cfg or {}).get("slice") or {}
    blob = json.dumps({"hue_curves": hc, "slice": sl, "size": int(size)},
                      sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def slice_lut(cfg: dict, size: int = LUT_SIZE) -> Path:
    """Bake to a cached .cube and hand back the path ffmpeg should read.

    Same cache discipline as the secondary: the file name is a hash of the
    settings, so an unchanged grade never rebakes and two grades that differ
    anywhere never collide.

    The write is ATOMIC: a uniquely named temporary file in the same directory,
    then os.replace. Measured, not theoretical: the parity harness asks the
    studio for the same config twice at once (the GPU wants the cube over
    /api/lut while the server is rendering the reference frame through the
    engine), and with a plain open-and-write the second caller saw
    `path.exists()` go true the instant the file was created and read 32215 of
    the 35937 rows. os.replace on the same filesystem is atomic, so a reader
    either sees no file at all and bakes it itself, or sees a complete one.
    """
    h = _cache_key(cfg, size)
    LUT_SLICE.mkdir(parents=True, exist_ok=True)
    path = LUT_SLICE / f"slice_{h}.cube"
    if path.exists():
        return path
    out = bake(cfg, size)
    tmp = LUT_SLICE / f".slice_{h}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        C.write_cube(tmp, out, size, f"slice_{h}", comments=[
            "Generated by grade/slice.py slice_lut",
            "Hue curves, Color Slice and Tetra, in that order.",
            "Domain: the display referred Rec.709 signal after CURVES.",
        ])
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return path
