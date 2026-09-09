"""Shared machinery for the cinegrade regression suite.

Everything here is deliberately dependency-light: numpy and colour-science are
the only third-party imports, because the venv this runs in has no scipy, no
matplotlib and no PIL. Pixels come back from ffmpeg as raw rgb24 or rgb48le
bytes and get read straight into numpy, which is also the only way to read an
image at all without PIL.

The suite never calls a private helper of the engine that a caller would not
call. It builds configs the way a preset does and drives the same graph
builders the CLI drives, so a test failing means the shipped path is broken,
not that the test reached around it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path

import numpy as np

TESTS = Path(__file__).resolve().parent
GRADE = TESTS.parent
CONTENT = GRADE.parent
FOOTAGE = CONTENT / "footage"
GOLDENS = TESTS / "goldens"
# Scratch is per process because more than one agent runs this suite at a
# time in this workspace. A shared _work/patches.png means one run rewrites
# the swatch image another run is mid-read on, and the loser reports a
# colour-science failure that is really a file collision.
WORK_ROOT = TESTS / "_work"
WORK = WORK_ROOT / f"run-{os.getpid()}"

sys.path.insert(0, str(GRADE))
sys.path.insert(0, str(GRADE / "tools"))

import cinegrade as cg           # noqa: E402
import colorlib as C             # noqa: E402
import make_cst as CST           # noqa: E402

# The two source clips. Named rather than globbed so a stray file dropped into
# footage/ cannot silently change what the goldens were blessed against.
CLIP_A = FOOTAGE / "A001_09011336_C002.MOV"
CLIP_B = FOOTAGE / "A001_09011832_C003.MOV"

# One frame per clip, picked away from the head so the seek lands on real
# content rather than a black lead-in.
TIME_A = 2.0
TIME_B = 8.0

# Small enough that a render is decode-bound (measured: width barely moves the
# clock, the ProRes decode dominates), large enough that percentiles and hue
# histograms are still stable.
WIDTH = 320


class RenderError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# config helpers
# --------------------------------------------------------------------------

def defaults() -> dict:
    return deepcopy(cg.DEFAULTS)


def preset(name: str) -> dict:
    return cg.load_preset(name)


def patch(cfg: dict, *patches: dict) -> dict:
    """Deep-merge patches over a config, using the engine's own merge."""
    out = deepcopy(cfg)
    for p in patches:
        out = cg.deep_merge(out, p)
    return out


def set_path(cfg: dict, dotted: str, value):
    """Set 'fx.halation.strength' style paths, returning a new config.

    An all-digits component indexes a list, so 'layers.0.correct.exposure'
    reaches into the layer stack. That is the only way a per-parameter sweep
    can address a layer field at all, since `layers` is a list where every
    other config path is a chain of dict keys.
    """
    out = deepcopy(cfg)
    node = out
    keys = dotted.split(".")
    for k in keys[:-1]:
        node = node[int(k)] if k.isdigit() and isinstance(node, list) else node[k]
    last = keys[-1]
    if last.isdigit() and isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value
    return out


def leaf_paths(node: dict, prefix: str = "") -> list[str]:
    """Every dotted leaf key in a config tree, for the coverage check."""
    out = []
    for k, v in node.items():
        p = f"{prefix}{k}"
        if isinstance(v, dict):
            out += leaf_paths(v, p + ".")
        else:
            out.append(p)
    return out


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

_RENDER_CACHE: dict[str, np.ndarray] = {}
_STATS = {"renders": 0, "cache_hits": 0, "ffmpeg_seconds": 0.0}

# Round 1 finding 44. The engine caches baked secondary cubes, window and
# radial mattes and Color Slice cubes under a "cache root", named from a hash
# of their settings. That root used to be grade/ for every run on this tree,
# and run_tests.py DELETES the files a run baked when it finishes, so two
# suites running at once against this worktree deleted each other's cache
# entries mid-run: the loser re-baked a file it was about to read, or read a
# half written one. The merge note asking for one run at a time was the
# workaround; this is the fix.
#
# Each run now gets its own root inside its own per-process scratch, for this
# process (set_cache_root, which also rebinds cg.LUT_LAYERS / cg.LUT_MASKS,
# the two names legacy_parity.py normalises out of a graph fingerprint) and
# for every CLI subprocess it spawns (CINEGRADE_CACHE_DIR, which those
# children inherit). Because it lives under WORK, the ordinary end of run
# cleanup takes it away and --keep-work keeps it with the rest of the
# evidence. Nothing under grade/luts/ is read or written by a suite run any
# more, so nothing there can be deleted by one either.
#
# The gap 22 test in cases_cli.py deliberately clears this pin to prove the
# unset default is still grade/luts, and re-pins it in its own finally.
CACHE_ROOT = WORK / "cache"
os.environ["CINEGRADE_CACHE_DIR"] = str(CACHE_ROOT)
cg.set_cache_root(CACHE_ROOT)
CACHE_DIRS = [CACHE_ROOT / "luts" / "layers", CACHE_ROOT / "luts" / "masks",
              CACHE_ROOT / "luts" / "slice"]
_TOUCHED_CACHE: set[str] = set()


def _record_cache_paths(args) -> None:
    roots = tuple(str(d) for d in CACHE_DIRS)
    for a in args:
        if not isinstance(a, str) or not a:
            continue
        for root in roots:
            idx = a.find(root)
            while idx != -1:
                tail = a[idx:]
                end = len(tail)
                for stop in ('"', "'", " ", ":", ",", "]", "["):
                    p = tail.find(stop)
                    if p != -1:
                        end = min(end, p)
                _TOUCHED_CACHE.add(tail[:end].replace("\\", ""))
                idx = a.find(root, idx + 1)


def touched_cache_files() -> set:
    return {Path(p) for p in _TOUCHED_CACHE}


def scaled_info(src: Path, width: int = WIDTH, autorotate: bool = True) -> dict:
    """probe() the clip, then shrink the reported size to the test width.

    The engine sizes its blurs, its radial mask and its grain plate from
    info['width'] and info['height'], so handing it the small size and
    downscaling the source to match is what keeps FX geometry proportional at
    test resolution. This is the same trick the studio preview server uses.
    """
    info = cg.probe(str(src), autorotate=autorotate)
    w = width - (width % 2)
    h = int(round(info["height"] * w / info["width"]))
    h -= h % 2
    small = dict(info)
    small["width"], small["height"] = w, h
    return small


_INFO_CACHE: dict[tuple, dict] = {}


def info_for(src: Path, width: int = WIDTH, autorotate: bool = True) -> dict:
    key = (str(src), width, autorotate)
    if key not in _INFO_CACHE:
        _INFO_CACHE[key] = scaled_info(src, width, autorotate)
    return _INFO_CACHE[key]


def _run_bytes(args: list[str]) -> bytes:
    _record_cache_paths(args)
    t0 = time.time()
    r = subprocess.run(args, capture_output=True)
    _STATS["ffmpeg_seconds"] += time.time() - t0
    if r.returncode != 0:
        raise RenderError(
            f"ffmpeg exit {r.returncode}\n"
            f"{' '.join(args[:12])} ...\n{r.stderr.decode(errors='replace')[-2500:]}")
    return r.stdout


def render(src: Path, cfg: dict, t: float, width: int = WIDTH,
           autorotate: bool = True) -> np.ndarray:
    """One graded frame as an (h, w, 3) uint8 array.

    Results are memoised on the exact config, because the parameter group
    renders the same baseline dozens of times and a decode is the expensive
    part of every call.
    """
    key = hashlib.sha1(json.dumps(
        [str(src), cfg, t, width, autorotate], sort_keys=True,
        default=str).encode()).hexdigest()
    hit = _RENDER_CACHE.get(key)
    if hit is not None:
        _STATS["cache_hits"] += 1
        return hit

    info = info_for(src, width, autorotate)
    w, h = info["width"], info["height"]
    head = f"[0:v]scale={w}:{h}:flags=bilinear[src]"
    graph = cg.graph_with_mask(cfg, info, tail_extra=["format=rgb24"],
                               encode_out=False, src_label="src",
                               head_extra=head)
    args = cg.ffmpeg_inputs(str(src), cfg, info, seek=t)
    args += ["-filter_complex", graph, "-map", "[vout]", "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    raw = _run_bytes(args)
    _STATS["renders"] += 1
    if len(raw) != w * h * 3:
        raise RenderError(f"expected {w * h * 3} bytes, got {len(raw)}")
    img = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 3)
    _RENDER_CACHE[key] = img
    return img


# --------------------------------------------------------------------------
# synthetic patch input
# --------------------------------------------------------------------------

def make_patch_source(patches: dict[str, list], swatch: int = 8) -> tuple[Path, list[str]]:
    """Write a 16-bit PNG of solid Apple Log code-value swatches.

    Colour anchors need an input whose code values are known exactly. Real
    footage cannot supply that, and 8 bits would quantise 0.3919 to +/- 0.004,
    which is the same size as the tolerance being tested. 16-bit rawvideo piped
    into ffmpeg's png encoder gets the values in at full precision without
    needing PIL.
    """
    names = list(patches)
    w, h = swatch * len(names), swatch
    arr = np.zeros((h, w, 3), dtype=np.float64)
    for i, n in enumerate(names):
        arr[:, i * swatch:(i + 1) * swatch, :] = patches[n]
    u16 = np.round(np.clip(arr, 0.0, 1.0) * 65535).astype("<u2")
    WORK.mkdir(parents=True, exist_ok=True)
    raw_path = WORK / "patches.raw"
    png_path = WORK / "patches.png"
    raw_path.write_bytes(u16.tobytes())
    _run_bytes(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo",
                "-pix_fmt", "rgb48le", "-s", f"{w}x{h}", "-i", str(raw_path),
                "-frames:v", "1", "-pix_fmt", "rgb48be", str(png_path)])
    return png_path, names


def patch_info(png: Path, count: int, swatch: int = 8) -> dict:
    """Hand-built info for the patch image.

    color_range is forced to 'full' because the swatch values ARE the code
    values under test. probe() would report 'tv' for a PNG (the tag is absent),
    and the engine's first scale would then stretch 16-235 to 0-255 and move
    every anchor before the CST ever saw it.
    """
    return {"width": swatch * count, "height": swatch, "rotation": 0,
            "autorotate": True, "pix_fmt": "rgb48be", "color_range": "full",
            "color_space": "bt2020nc", "nb_frames": "1", "duration": "1",
            "codec": "png", "profile": None}


def render_patches(cfg: dict, patches: dict[str, list], swatch: int = 8
                   ) -> dict[str, np.ndarray]:
    """Push the swatch image through the real graph, read the centre of each."""
    png, names = make_patch_source(patches, swatch)
    info = patch_info(png, len(names), swatch)
    graph = cg.graph_with_mask(cfg, info, tail_extra=["format=rgb48le"],
                               encode_out=False)
    # Built by the engine's own input assembler, because a preset with grain or
    # radial blur on adds extra ffmpeg inputs that the graph then refers to by
    # index. Hand-rolling "-i png" alone leaves those dangling.
    args = cg.ffmpeg_inputs(str(png), cfg, info)
    args += ["-filter_complex", graph, "-map", "[vout]", "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"]
    raw = _run_bytes(args)
    _STATS["renders"] += 1
    out = np.frombuffer(raw, dtype="<u2").reshape(
        info["height"], info["width"], 3).astype(np.float64) / 65535.0
    mid = info["height"] // 2
    return {n: out[mid, i * swatch + swatch // 2] for i, n in enumerate(names)}


def render_through_lut(lut_path: Path, patches: dict[str, list],
                       swatch: int = 8) -> dict[str, np.ndarray]:
    """Same swatch trick, but through one bare lut3d.

    Needed for the CST IN anchor: the engine has no mode that stops in the DWG
    working space, so measuring the 0.3360 mid grey means applying that one LUT
    the way the engine applies it (tetrahedral, same file) and stopping there.
    """
    png, names = make_patch_source(patches, swatch)
    info = patch_info(png, len(names), swatch)
    args = ["ffmpeg", "-v", "error", "-y", "-i", str(png), "-vf",
            f"format=gbrp16le,lut3d=file={cg.esc(lut_path)}:interp=tetrahedral,"
            f"format=rgb48le", "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"]
    raw = _run_bytes(args)
    _STATS["renders"] += 1
    out = np.frombuffer(raw, dtype="<u2").reshape(
        info["height"], info["width"], 3).astype(np.float64) / 65535.0
    mid = info["height"] // 2
    return {n: out[mid, i * swatch + swatch // 2] for i, n in enumerate(names)}


# --------------------------------------------------------------------------
# .cube reading, for LUT-level assertions
# --------------------------------------------------------------------------

def read_cube(path: Path) -> tuple[np.ndarray, int]:
    """Load a .cube into a (size, size, size, 3) array indexed [b, g, r]."""
    vals, size = [], None
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("LUT_3D_SIZE"):
                size = int(line.split()[1])
                continue
            if line[0].isalpha():
                continue
            parts = line.split()
            if len(parts) == 3:
                try:
                    vals.append([float(x) for x in parts])
                except ValueError:
                    continue
    if size is None:
        raise ValueError(f"no LUT_3D_SIZE in {path}")
    arr = np.asarray(vals, dtype=np.float64)
    if arr.shape[0] != size ** 3:
        raise ValueError(f"{path}: expected {size ** 3} rows, got {arr.shape[0]}")
    # .cube stores red varying fastest, so the natural reshape is [b][g][r].
    return arr.reshape(size, size, size, 3), size


def sample_cube(lut: np.ndarray, size: int, rgb) -> np.ndarray:
    """Trilinear sample. ffmpeg uses tetrahedral, which agrees on the lattice
    and differs by well under a code value between nodes, so this is close
    enough to pin a hue direction without reimplementing tetrahedral."""
    rgb = np.clip(np.asarray(rgb, dtype=float), 0.0, 1.0)
    x = rgb * (size - 1)
    i0 = np.minimum(np.floor(x).astype(int), size - 2)
    f = x - i0
    r0, g0, b0 = i0
    fr, fg, fb = f
    out = np.zeros(3)
    for db in (0, 1):
        for dg in (0, 1):
            for dr in (0, 1):
                wgt = ((fb if db else 1 - fb) * (fg if dg else 1 - fg)
                       * (fr if dr else 1 - fr))
                out += wgt * lut[b0 + db, g0 + dg, r0 + dr]
    return out


# --------------------------------------------------------------------------
# image metrics
# --------------------------------------------------------------------------

def as_float(img: np.ndarray) -> np.ndarray:
    return img.astype(np.float64) / 255.0


def luma(img: np.ndarray) -> np.ndarray:
    f = as_float(img)
    return 0.2126 * f[..., 0] + 0.7152 * f[..., 1] + 0.0722 * f[..., 2]


def saturation(img: np.ndarray) -> np.ndarray:
    """HSV S. Matches how the engine's own qualifier defines saturation."""
    f = as_float(img)
    mx = f.max(-1)
    mn = f.min(-1)
    return np.where(mx > 1e-9, (mx - mn) / np.maximum(mx, 1e-9), 0.0)


def hue_deg(img: np.ndarray) -> np.ndarray:
    h, _, _ = C.rgb_to_hsv(as_float(img))
    return h


def box_blur(img: np.ndarray, k: int = 3) -> np.ndarray:
    """Separable box blur via an integral image. scipy is not installed."""
    pad = k // 2
    a = np.pad(np.asarray(img, dtype=np.float64),
               ((pad, pad), (pad, pad), (0, 0)), mode="edge")
    cs = np.cumsum(a, axis=0)
    zero_row = np.zeros((1,) + cs.shape[1:])
    rows = cs[k - 1:] - np.concatenate([zero_row, cs[:-k]], axis=0)
    cs2 = np.cumsum(rows, axis=1)
    zero_col = np.zeros((cs2.shape[0], 1, cs2.shape[2]))
    cols = cs2[:, k - 1:] - np.concatenate([zero_col, cs2[:, :-k]], axis=1)
    return cols / float(k * k)


def hf_energy(img: np.ndarray) -> float:
    """Mean distance from a 3x3 box blur: a proxy for sharpness and grain."""
    f = as_float(img)
    return float(np.abs(f - box_blur(f, 3)).mean())


HUE_FAMILIES = [
    ("red", 345.0, 15.0), ("orange", 15.0, 45.0), ("yellow", 45.0, 70.0),
    ("green", 70.0, 165.0), ("cyan", 165.0, 200.0), ("blue", 200.0, 260.0),
    ("purple", 260.0, 290.0), ("magenta", 290.0, 345.0),
]

# Below this saturation a pixel has no meaningful hue, so counting it into a
# family would make the histogram track exposure instead of colour.
HUE_SAT_FLOOR = 0.15
HUE_VAL_FLOOR = 0.05


def hue_family_pct(img: np.ndarray) -> dict[str, float]:
    f = as_float(img)
    h, s, v = C.rgb_to_hsv(f)
    keep = (s >= HUE_SAT_FLOOR) & (v >= HUE_VAL_FLOOR)
    total = float(f.shape[0] * f.shape[1])
    out = {"neutral": round(100.0 * float((~keep).sum()) / total, 4)}
    for name, lo, hi in HUE_FAMILIES:
        if lo > hi:
            sel = (h >= lo) | (h < hi)
        else:
            sel = (h >= lo) & (h < hi)
        out[name] = round(100.0 * float((sel & keep).sum()) / total, 4)
    return out


def clip_fractions(img: np.ndarray) -> tuple[float, float]:
    """Fraction of channel samples pinned at 0 and at 255."""
    n = float(img.size)
    return float((img == 0).sum()) / n, float((img == 255).sum()) / n


def stats(img: np.ndarray) -> dict:
    f = as_float(img)
    y = luma(img)
    s = saturation(img)
    lo, hi = clip_fractions(img)
    out = {
        "luma_mean": float(y.mean()),
        "luma_std": float(y.std()),
        "luma_p1": float(np.percentile(y, 1)),
        "luma_p5": float(np.percentile(y, 5)),
        "luma_p50": float(np.percentile(y, 50)),
        "luma_p95": float(np.percentile(y, 95)),
        "luma_p99": float(np.percentile(y, 99)),
        "sat_mean": float(s.mean()),
        "hf": hf_energy(img),
        "clip_low": lo,
        "clip_high": hi,
    }
    for i, ch in enumerate("rgb"):
        c = f[..., i]
        out[f"{ch}_mean"] = float(c.mean())
        out[f"{ch}_median"] = float(np.median(c))
        out[f"{ch}_p1"] = float(np.percentile(c, 1))
        out[f"{ch}_p5"] = float(np.percentile(c, 5))
        out[f"{ch}_p95"] = float(np.percentile(c, 95))
        out[f"{ch}_p99"] = float(np.percentile(c, 99))
    out["warmth"] = out["r_mean"] - out["b_mean"]
    out["greenness"] = out["g_mean"] - 0.5 * (out["r_mean"] + out["b_mean"])
    out["blueness"] = out["b_mean"] - out["r_mean"]
    return out


# --------------------------------------------------------------------------
# fingerprints
# --------------------------------------------------------------------------

GRID = 12   # downsample side for the golden thumbnail


def thumbnail(img: np.ndarray, grid: int = GRID) -> np.ndarray:
    """Block-mean downsample to grid x grid x 3, in 0..1.

    Storing the thumbnail rather than only its hash is what lets a failure say
    'block (4, 7) green moved 0.031' instead of 'the hash changed'.
    """
    f = as_float(img)
    h, w, _ = f.shape
    ys = np.linspace(0, h, grid + 1).astype(int)
    xs = np.linspace(0, w, grid + 1).astype(int)
    out = np.zeros((grid, grid, 3))
    for i in range(grid):
        for j in range(grid):
            block = f[ys[i]:max(ys[i] + 1, ys[i + 1]),
                      xs[j]:max(xs[j] + 1, xs[j + 1])]
            out[i, j] = block.reshape(-1, 3).mean(axis=0)
    return out


def fingerprint(img: np.ndarray) -> dict:
    thumb = thumbnail(img)
    quant = np.round(thumb * 255).astype(np.uint8)
    return {
        "shape": list(img.shape),
        "stats": {k: round(v, 6) for k, v in stats(img).items()},
        "hues": hue_family_pct(img),
        "thumb": np.round(thumb, 5).tolist(),
        "thumb_hash": hashlib.sha1(quant.tobytes()).hexdigest(),
    }


# How far a fingerprint number may move before it counts as a regression.
# ffmpeg is deterministic on a fixed build, so any real drift is far larger
# than this; the slack only absorbs float formatting in the JSON round trip.
FP_TOL = {
    "default": 0.0015,
    "hue_pct": 0.05,      # percentage points
    "thumb": 0.004,
}


def compare_fingerprint(got: dict, want: dict) -> list[str]:
    """Return a list of human-readable drifts, empty when the frame matches."""
    out = []
    if got["shape"] != want["shape"]:
        out.append(f"shape {want['shape']} -> {got['shape']}")
        return out
    for k, wv in want["stats"].items():
        gv = got["stats"].get(k)
        if gv is None:
            out.append(f"stat {k} disappeared")
            continue
        d = gv - wv
        if abs(d) > FP_TOL["default"]:
            out.append(f"{k} {wv:.5f} -> {gv:.5f} ({d:+.5f})")
    for k, wv in want["hues"].items():
        gv = got["hues"].get(k, 0.0)
        d = gv - wv
        if abs(d) > FP_TOL["hue_pct"]:
            out.append(f"hue {k} {wv:.3f}% -> {gv:.3f}% ({d:+.3f} pts)")
    g = np.asarray(got["thumb"])
    w = np.asarray(want["thumb"])
    if g.shape == w.shape:
        diff = np.abs(g - w)
        if diff.max() > FP_TOL["thumb"]:
            idx = np.unravel_index(int(np.argmax(diff)), diff.shape)
            out.append(
                f"thumbnail block ({idx[0]},{idx[1]}) {'rgb'[idx[2]]} "
                f"{w[idx]:.4f} -> {g[idx]:.4f} (max delta {diff.max():.4f}, "
                f"mean {diff.mean():.5f})")
    if got["thumb_hash"] != want["thumb_hash"] and not out:
        out.append(f"thumbnail hash changed within tolerance "
                   f"({want['thumb_hash'][:12]} -> {got['thumb_hash'][:12]})")
    return out


# --------------------------------------------------------------------------
# misc
# --------------------------------------------------------------------------

def changed_fraction(a: np.ndarray, b: np.ndarray, thresh: int = 1) -> float:
    """Fraction of pixels where any channel moved by more than `thresh`."""
    d = np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2)
    return float((d > thresh).mean())


def mean_abs_diff(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a.astype(np.float64) - b.astype(np.float64)).mean())


def black_bar_rows(img: np.ndarray, thresh: int = 2) -> int:
    """Count fully-black rows at the top plus the bottom of the frame."""
    rows = (img.max(axis=(1, 2)) <= thresh)
    top = 0
    for v in rows:
        if not v:
            break
        top += 1
    bot = 0
    for v in rows[::-1]:
        if not v:
            break
        bot += 1
    return top + bot


def ffprobe_stream(path: Path, entries: str) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", f"stream={entries}", "-of", "json", str(path)],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RenderError(f"ffprobe failed on {path}: {r.stderr[-800:]}")
    streams = json.loads(r.stdout).get("streams") or [{}]
    return streams[0]


def render_stats() -> dict:
    return dict(_STATS)


def solve_input_for_output(fn, target, start=(0.5, 0.4, 0.4), iters=80):
    """Find the input RGB whose transform lands on `target`.

    Newton with a numeric Jacobian. Used to build a source patch that arrives
    at a named Rec.709 colour (the maroon car) after the CST, so an assertion
    about what a look does to that colour is made against that colour and not
    against whatever the footage happened to contain.
    """
    target = np.asarray(target, dtype=float)
    x = np.asarray(start, dtype=float)
    h = 1e-5
    for _ in range(iters):
        f = np.asarray(fn(x[None, :])[0]) - target
        if np.max(np.abs(f)) < 1e-7:
            break
        J = np.zeros((3, 3))
        base = np.asarray(fn(x[None, :])[0])
        for j in range(3):
            xp = x.copy()
            xp[j] += h
            J[:, j] = (np.asarray(fn(xp[None, :])[0]) - base) / h
        try:
            x = np.clip(x - np.linalg.solve(J, f), 0.0, 1.0)
        except np.linalg.LinAlgError:
            break
    return x, np.asarray(fn(x[None, :])[0])


def hue_distance(a, b) -> float:
    """Angular distance between two hues in degrees, wrapping at 0/360.

    Reds straddle the wrap point, so a plain subtraction reports a 348 degree
    "flip" for what is a 12 degree nudge.
    """
    ha, _, _ = C.rgb_to_hsv(np.asarray(a, dtype=float)[None, :])
    hb, _, _ = C.rgb_to_hsv(np.asarray(b, dtype=float)[None, :])
    return float(abs(((float(ha[0]) - float(hb[0]) + 180.0) % 360.0) - 180.0))
