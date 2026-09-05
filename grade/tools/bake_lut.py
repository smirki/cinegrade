#!/usr/bin/env python3
"""Bake the per-pixel half of a cinegrade config into one .cube for free Resolve.

Free DaVinci Resolve cannot load OFX plugins and ships without most of the
Studio-only ResolveFX, so a grade built here cannot be rebuilt node for node
over there. What it CAN do is apply a 3D LUT, and the engine's chain splits
cleanly at that boundary:

    LOG -> CST IN -> PRIMARIES -> CST OUT -> CURVES -> LAYERS -> LOOK
        every one of those is a pure function of one pixel's RGB, so the whole
        run collapses into a single 3D LUT with no loss beyond interpolation

    FX -> GRAIN -> DETAIL -> LETTERBOX
        every one of those reads neighbouring pixels (or a noise plate, or the
        frame geometry), so no 3D LUT can hold them at any grid size

This tool bakes the first half and refuses to pretend about the second.

Two domain modes, and picking the wrong one silently ruins the image:

  --domain applelog   The LUT eats RAW Apple Log. It carries the log to display
                      conversion itself, so in Resolve it goes on a node with
                      NOTHING in front of it. Putting a Color Space Transform
                      before it converts the footage twice and the result is
                      washed out and wrongly saturated.

  --domain rec709     The LUT eats footage that is ALREADY Rec.709. It is built
                      by composing the grade with the inverse of the technical
                      conversion, so it must sit AFTER a conversion node. Drop
                      it straight on log footage and you get mud, which is the
                      same failure mode as applying any creative LUT to log.

Grid size. The default is 65 whenever the LUT has to carry a log to display
transfer (always for --domain applelog, and for --domain rec709 unless the
config leaves everything upstream of CST OUT at identity). That transfer is
the steepest part of the chain: a stop of scene light near the toe moves the
output code far more than a stop near the shoulder, and a 33 grid samples that
curve too coarsely, which shows as banding in gradients and a visible error in
the deep shadows. A look-only LUT is a gentle Rec.709 to Rec.709 remap, so 33
is genuinely enough there and is a 8x smaller file.

    python grade/tools/bake_lut.py --preset cinekit --domain applelog
    python grade/tools/bake_lut.py --preset natural --domain rec709
    python grade/tools/bake_lut.py --config my.json --domain applelog --size 33
    python grade/tools/bake_lut.py --preset premium --domain applelog --no-verify
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

TOOLS = Path(__file__).resolve().parent
GRADE = TOOLS.parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(GRADE))

import colorlib as C          # noqa: E402
import make_cst               # noqa: E402
import cinegrade              # noqa: E402
from cinegrade import GradeError  # noqa: E402

from colour.models import log_encoding_AppleLogProfile as apple_log_encode  # noqa: E402

EXPORT = GRADE / "luts" / "export"

# Every filter the engine is allowed to put in the per-pixel head. If a future
# stage lands there under a name that is not on this list, the bake would drop
# it without a word and the .cube would quietly be the wrong grade, so an
# unknown name is a hard failure instead.
BAKEABLE_FILTERS = {"lut", "lut3d", "curves", "colorchannelmixer", "vibrance"}

# Stages the engine runs after the look. Each one reads more than the pixel
# under it, which is exactly what a 3D LUT cannot express.
SPATIAL_REASONS = {
    "fx.halation": "blurs isolated highlights across the frame",
    "fx.bloom": "blurs isolated highlights across the frame",
    "fx.rgb_split": "shifts the R and B planes sideways",
    "fx.radial_blur": "blur strength depends on distance from frame centre",
    "fx.vignette": "darkening depends on pixel position",
    "grain": "adds a per-frame random noise plate",
    "detail.soften": "gaussian blur, reads neighbouring pixels",
    "detail.sharpen": "unsharp mask, reads neighbouring pixels",
    "letterbox": "crop and pad, changes frame geometry",
}


# --------------------------------------------------------------------------
# what can and cannot go in the cube
# --------------------------------------------------------------------------

def unbakeable_stages(cfg: dict) -> list[tuple[str, str]]:
    """Enabled stages that no 3D LUT can hold, with the reason for each."""
    out = []
    fx = cfg.get("fx") or {}
    for name in ("halation", "bloom", "rgb_split", "radial_blur", "vignette"):
        if (fx.get(name) or {}).get("enabled"):
            out.append((f"fx.{name}", SPATIAL_REASONS[f"fx.{name}"]))
    if (cfg.get("grain") or {}).get("enabled"):
        out.append(("grain", SPATIAL_REASONS["grain"]))
    d = cfg.get("detail") or {}
    if float(d.get("soften", 0.0)) > 1e-6:
        out.append(("detail.soften", SPATIAL_REASONS["detail.soften"]))
    if float(d.get("sharpen", 0.0)) > 1e-6:
        out.append(("detail.sharpen", SPATIAL_REASONS["detail.sharpen"]))
    if (cfg.get("letterbox") or {}).get("enabled"):
        out.append(("letterbox", SPATIAL_REASONS["letterbox"]))
    return out


def check_config_is_understood(cfg: dict) -> None:
    """Refuse on any config section this tool has not been taught to classify.

    Silently baking an unknown section as "not present" produces a .cube that
    looks right and grades wrong, which is far worse than an error.
    """
    known = set(cinegrade.DEFAULTS) | {"_comment"}
    unknown = sorted(set(cfg) - known)
    if unknown:
        raise GradeError(
            "cannot bake: this config has sections bake_lut.py does not know "
            f"how to classify: {', '.join(unknown)}.\n"
            "Teach bake_lut.py whether each one is per-pixel (bake it) or "
            "spatial (report it) before trusting a LUT from this config.")


def filter_name(f: str) -> str:
    return f.split("=", 1)[0]


def look_path(cfg: dict) -> Path | None:
    """Resolve the look .cube the engine will load.

    f_look hands back an ffmpeg fragment with the path escaped inside it, and
    unpicking that string is more fragile than repeating the two-line lookup.
    """
    lut = cfg["look"]["lut"]
    if not lut:
        return None
    p = Path(lut)
    if not p.exists():
        p = cinegrade.LUT_LOOKS / (lut if lut.endswith(".cube") else f"{lut}.cube")
    return p if p.exists() else None


def read_cube(path: Path) -> tuple[np.ndarray, int]:
    vals, size = [], None
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith('"'):
                continue
            if line.startswith("LUT_3D_SIZE"):
                size = int(line.split()[1])
                continue
            if line[0].isalpha():
                continue
            vals.append([float(x) for x in line.split()])
    if size is None:
        raise GradeError(f"{path} has no LUT_3D_SIZE")
    return np.asarray(vals).reshape(size, size, size, 3), size


# Curvature above this reliably costs more than a code value of accuracy in a
# combination bake. Calibrated against the shipped looks: the smooth ones sit
# at 0.03 to 0.06 and land under 2.5 codes, blockbuster is at 0.18 and lands at
# 10, blockbuster_max is at 0.30 and lands at 17.
ROUGH_CURVATURE = 0.05


def cube_roughness(path: Path) -> tuple[int, float, float]:
    """Grid size, biggest neighbour-to-neighbour step, and biggest curvature.

    Curvature (the second difference along each axis) is the number that
    matters, because tetrahedral interpolation is exact on anything linear and
    its error is driven by how fast the slope changes. Both are reported since
    the step size is the one a human can picture: a step of 0.17 is 45 code
    values crossed inside a single cell.
    """
    data, size = read_cube(path)
    step = max(float(np.abs(np.diff(data, axis=ax)).max()) for ax in (0, 1, 2))
    curve = max(float(np.abs(np.diff(data, n=2, axis=ax)).max()) for ax in (0, 1, 2))
    return size, step, curve


def rough_stages(cfg: dict) -> list[tuple[str, int, float, float]]:
    """Every baked stage that is itself a coarse .cube, with its roughness.

    These are the only stages a combination bake struggles with. They are
    authored on a grid that is uniform in Rec.709, but a --domain applelog
    cube samples them on a grid that is uniform in LOG, and the log to display
    curve is steepest through the mid tones: around 18% grey one step of a 65
    cube covers MORE display range than one step of a 33 cube, so a kink in
    the look falls between two bake nodes and gets chorded across. Nothing
    about the bake can fix that, it is a property of the two grids.
    """
    out = []
    p = look_path(cfg)
    if p is not None:
        size, step, curve = cube_roughness(p)
        out.append((f"look {cfg['look']['lut']}", size, step, curve))
    for i, layer in enumerate(cinegrade.config_layers(cfg)):
        if not cinegrade.layer_active(layer):
            continue
        variants = cinegrade.layer_branches(layer)
        for v in variants:
            size, step, curve = cube_roughness(cinegrade.layer_lut(layer, v))
            label = f"layer {i + 1} {layer['name']}"
            out.append((f"{label} ({v})" if len(variants) > 1 else label,
                        size, step, curve))
    return out


def baked_summary(cfg: dict, meta: dict) -> list[str]:
    """One list of what actually went into the cube, for both the console and
    the .cube header, so the two can never disagree."""
    c, p = cfg["convert"], cfg["primaries"]
    out = []
    ev = float(c["exposure"])
    temp, tint = float(p["temperature"]), float(p["tint"])
    if abs(ev) > 1e-6:
        out.append(f"exposure {ev:+.2f} stops (a log-domain offset applied "
                   f"before the conversion)")
    if abs(temp) > 1e-6 or abs(tint) > 1e-6:
        out.append(f"white balance: temperature {temp:+.3f}, tint {tint:+.3f} "
                   f"(also log-domain, also before the conversion)")
    out.append(f"conversion via {meta['working_space']}, tone map "
               f"{meta['tonemap']}, output transfer {meta['encode']}")
    prim = [f"{k} {float(p[k]):.3f}" for k in
            ("contrast", "saturation", "vibrance", "black_lift",
             "highlight_rolloff")
            if abs(float(p[k]) - (1.0 if k in ("contrast", "saturation") else 0.0))
            > 1e-6]
    for k, ident in (("lift", 0.0), ("gain", 1.0), ("gamma", 1.0),
                     ("brightness", 0.0)):
        v = cinegrade._triplet(p[k], ident)
        if any(abs(x - ident) > 1e-6 for x in v):
            prim.append(f"{k} {v}")
    out.append("primaries: " + (", ".join(prim) if prim else "none, left at identity"))
    if (cfg.get("curves") or {}).get("enabled"):
        out.append("curves")
    for i, layer in enumerate(cinegrade.config_layers(cfg)):
        if cinegrade.layer_active(layer):
            out.append(f"layer {i + 1} {layer['name']} at {layer['placement']} "
                       f"(a colour correction under a colour matte, so it is "
                       f"per-pixel and bakes)")
    out.append(f"look {meta['look']}" if meta["look"] else "no look LUT")
    return out


def per_pixel_head(cfg: dict, info: dict) -> list[str]:
    """The engine's own head chain, minus the frame normalisation prefix.

    Built by calling cinegrade's stage builders rather than reimplementing
    them, so the bake tracks the engine instead of drifting away from it. The
    leading scale/format pair is dropped because it is a container-level
    decode (limited to full range, BT.2020 matrix, 16 bit planar), not a
    colour operation: it defines the LUT's input domain rather than living
    inside it.
    """
    log = cinegrade.f_log_stage(cfg, info)
    prefix = [f for f in log if filter_name(f) in ("scale", "format")]
    body = [f for f in log if filter_name(f) not in ("scale", "format")]
    if len(prefix) != 2 or not prefix[0].startswith("scale=in_color_matrix"):
        raise GradeError(
            "cannot bake: the engine's LOG stage no longer starts with the "
            f"expected range/matrix normalisation pair, it starts with {prefix}. "
            "bake_lut.py strips exactly that prefix, so it must be checked by "
            "hand before the bake can be trusted.")

    chain = (body + cinegrade.f_convert_in(cfg) + cinegrade.f_primaries(cfg)
             + cinegrade.f_convert_out(cfg) + cinegrade.f_curves(cfg)
             + layers_chain(cfg, info, "before_look"))
    return check_bakeable(chain)


def check_bakeable(chain: list[str]) -> list[str]:
    """Refuse a chain that contains anything a cube cannot hold."""
    bad = sorted({filter_name(f) for f in chain} - BAKEABLE_FILTERS)
    if bad:
        raise GradeError(
            "cannot bake: the per-pixel chain contains filters that are not "
            f"known to be pure per-pixel functions: {', '.join(bad)}.\n"
            "A 3D LUT can only hold a function of one pixel's own RGB. Add the "
            "filter to BAKEABLE_FILTERS only after confirming that is what it "
            "is; otherwise this stage has to stay in cinegrade.")
    return chain


def layers_chain(cfg: dict, info: dict, placement: str) -> list[str]:
    """The layer stack at one placement point, as filters, when it is per-pixel.

    A layer with no power window is one lut3d, which a cube holds exactly. A
    window is a matte that depends on where the pixel is, so no grid size can
    express it and the bake has to refuse rather than hand back a LUT that is
    missing a stage. A blur is caught a step later by check_bakeable, which
    already names it.

    The filters come from the engine's own build_layers, not from a second
    implementation here: one plain segment in means that segment IS the chain,
    and anything else means the stack needed a split, which is the window.
    """
    segs, _ = cinegrade.build_layers(cfg, info, placement, "in", [], "out")
    if not segs:
        return []
    if len(segs) != 1 or not (segs[0].startswith("[in]")
                              and segs[0].endswith("[out]")):
        raise GradeError(
            f"cannot bake: a {placement} layer uses a power window, and the "
            "matte it merges under depends on where the pixel is rather than "
            "on its colour. A 3D LUT holds a function of one pixel's own RGB "
            "and nothing else, so that layer has to stay in cinegrade.")
    return segs[0][len("[in]"):-len("[out]")].split(",")


# --------------------------------------------------------------------------
# running a grid through the real ffmpeg chain
# --------------------------------------------------------------------------

def grid_shape(size: int) -> tuple[int, int]:
    """A frame that holds size**3 pixels. Layout is irrelevant to a per-pixel
    chain, so the only requirement is that the pixel order survives."""
    return size * size, size


def run_chain(codes: np.ndarray, chain: list[str], size: int) -> np.ndarray:
    """Push (size**3, 3) float codes through an ffmpeg filter chain.

    The bake is done by evaluating the ENGINE'S OWN filter graph on an identity
    grid rather than by recomputing the colour maths in numpy. Two chains that
    agree on paper still disagree in practice (ffmpeg's lut3d interpolates the
    technical LUTs, its curves filter has its own spline, its 16 bit lut tables
    round), and a numpy re-derivation would be measuring the wrong thing. This
    way the only difference the verification can find is the interpolation cost
    of the baked cube itself, which is the number that actually matters.
    """
    w, h = grid_shape(size)
    buf = np.clip(np.rint(codes * 65535.0), 0, 65535).astype("<u2")
    args = ["ffmpeg", "-v", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb48le", "-s", f"{w}x{h}", "-i", "-",
            "-vf", ",".join(["format=gbrp16le"] + chain + ["format=rgb48le"]),
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"]
    r = subprocess.run(args, input=buf.tobytes(), capture_output=True)
    if r.returncode != 0:
        raise GradeError(f"ffmpeg failed baking the grid ({r.returncode})\n"
                         f"{r.stderr.decode()[-4000:]}")
    want = size ** 3 * 3 * 2
    if len(r.stdout) != want:
        raise GradeError(f"grid readback was {len(r.stdout)} bytes, expected {want}")
    return np.frombuffer(r.stdout, "<u2").astype(np.float64).reshape(-1, 3) / 65535.0


# --------------------------------------------------------------------------
# the technical conversion and its inverse (for --domain rec709)
# --------------------------------------------------------------------------

def technical_encode(cfg: dict) -> str:
    """Which display transfer the config's CST OUT actually applies.

    On the dwg path that is convert.encode. On the direct path the engine picks
    a pre-generated file whose encode was fixed when make_cst.py wrote it and
    is not in the config at all, so the value is read back out of the file's
    own header comment rather than guessed.
    """
    if cfg["convert"]["working_space"] == "dwg":
        return cfg["convert"]["encode"]
    name = f"AppleLog_to_Rec709_{cfg['convert']['tonemap']}.cube"
    path = cinegrade.LUT_TECH / name
    if not path.exists():
        raise GradeError(f"missing technical LUT {path}; run tools/make_cst.py")
    with open(path) as fh:
        for line in fh:
            if not line.startswith("#"):
                break
            if "Out:" in line and "transfer" in line:
                return line.split("primaries,")[-1].replace("transfer", "").strip()
    raise GradeError(
        f"cannot read the display transfer out of {path}. A --domain rec709 "
        "bake has to invert exactly the conversion the engine applied, so the "
        "encode cannot be assumed. Regenerate the LUT with make_cst.py.")


def _aces_inverse(y: np.ndarray) -> np.ndarray:
    """Invert Stephen Hill's ACES fit. Both branches of the quadratic are real
    for every y in [0, 1]; only the positive root is a physical scene value."""
    a = 1.0 - 0.983729 * y
    b = 0.0245786 - 0.432951 * y
    c = -0.000090537 - 0.238081 * y
    disc = np.maximum(b * b - 4.0 * a * c, 0.0)
    # a hits zero at y = 1.0165, the fit's horizontal asymptote: no finite
    # scene value maps there, so anything at or past it is treated as clipped.
    safe = np.where(np.abs(a) > 1e-9, a, 1e-9)
    x = (-b + np.sqrt(disc)) / (2.0 * safe)
    return np.where(a > 1e-9, x, 1e6)


def _filmic_inverse(x: np.ndarray, white: float = 12.0) -> np.ndarray:
    toe = 0.002
    m = np.clip(x, 0.0, None) * (1.0 - toe) + toe
    w2 = white * white
    b = w2 * (1.0 - m)
    return (-b + np.sqrt(b * b + 4.0 * m * w2)) / 2.0


DECODERS = {
    "gamma24": lambda x: np.clip(x, 0.0, 1.0) ** 2.4,
    "rec709a": lambda x: np.clip(x, 0.0, 1.0) ** 2.2,
    "rec709": lambda x: np.where(np.clip(x, 0.0, 1.0) < 0.081,
                                 np.clip(x, 0.0, 1.0) / 4.5,
                                 ((np.clip(x, 0.0, 1.0) + 0.099) / 1.099) ** (1 / 0.45)),
    "srgb": lambda x: np.where(np.clip(x, 0.0, 1.0) <= 0.04045,
                               np.clip(x, 0.0, 1.0) / 12.92,
                               ((np.clip(x, 0.0, 1.0) + 0.055) / 1.055) ** 2.4),
}

REC709_TO_BT2020 = np.linalg.inv(C.BT2020_TO_REC709)
AP1_TO_BT2020 = np.linalg.inv(C.BT2020_TO_AP1)
REC709_TO_AP1 = np.linalg.inv(C.AP1_TO_REC709)


def cst_inverse(code: np.ndarray, tonemap: str, encode: str) -> np.ndarray:
    """Rec.709 display code -> the Apple Log code that produced it.

    The exact algebraic inverse of make_cst.apple_log_to_rec709, walked
    backwards through the same stages. It is a true inverse only where the
    forward transform was injective: everything the CST already crushed to
    black or clipped to white collapses onto one log value, which is why a
    --domain rec709 LUT cannot recover detail a conversion node threw away.
    """
    lin = DECODERS[encode](code)
    if tonemap == "aces":
        ap1 = C.apply_matrix(lin, REC709_TO_AP1)
        scene_ap1 = _aces_inverse(np.clip(ap1, None, 1.0))
        scene = C.apply_matrix(np.clip(scene_ap1, 0.0, None), AP1_TO_BT2020)
    elif tonemap == "filmic":
        scene = C.apply_matrix(_filmic_inverse(lin), REC709_TO_BT2020)
    else:
        scene = C.apply_matrix(lin, REC709_TO_BT2020)
    return np.clip(apple_log_encode(np.clip(scene, 0.0, None)), 0.0, 1.0)


def write_companion_cst(tonemap: str, encode: str, size: int = 65) -> Path:
    """The conversion a --domain rec709 LUT assumes ran in front of it.

    Exported as a .cube rather than left to Resolve's own Color Space Transform
    node because the two are not the same transform: this engine tone maps with
    the Hill ACES fit, Resolve tone maps with its own curve. Chaining the LUT
    behind a Resolve CST still works and still looks graded, but it will not
    match what cinegrade renders. Behind this file it matches.
    """
    EXPORT.mkdir(parents=True, exist_ok=True)
    path = EXPORT / f"CST_AppleLog_to_Rec709_{tonemap}_{encode}.cube"
    grid = C.identity_grid(size)
    out = make_cst.apple_log_to_rec709(grid, tonemap, encode)
    C.write_cube(path, out, size, f"CST_AppleLog_to_Rec709_{tonemap}_{encode}",
                 comments=[
                     "Generated by content/grade/tools/bake_lut.py",
                     "In:  Apple Log, BT.2020 primaries, FULL range",
                     f"Out: Rec.709 primaries, {encode} transfer, "
                     f"{tonemap} tone map",
                     "Technical conversion only, no creative grade.",
                     "Resolve: node 1, with a --domain rec709 look LUT on node 2.",
                 ])
    return path


# --------------------------------------------------------------------------
# the bake
# --------------------------------------------------------------------------

def bake(cfg: dict, domain: str, size: int) -> tuple[np.ndarray, dict]:
    info = {"width": 1920, "height": 1080, "color_range": "tv"}
    chain = per_pixel_head(cfg, info)
    grid = C.identity_grid(size)

    meta = {"tonemap": cfg["convert"]["tonemap"],
            "encode": technical_encode(cfg),
            "working_space": cfg["convert"]["working_space"]}

    if domain == "rec709":
        source = cst_inverse(grid, meta["tonemap"], meta["encode"])
    else:
        source = grid

    out = run_chain(source, chain, size)

    look = cinegrade.f_look(cfg)
    if look:
        mix = float(cfg["look"].get("mix", 1.0))
        looked = run_chain(out, look, size)
        # Same arithmetic as the engine's blend node, done here in float so the
        # opacity does not pick up a second rounding pass.
        out = out + (looked - out) * mix
        meta["look"] = f"{cfg['look']['lut']} @ mix {mix:.2f}"
    else:
        meta["look"] = None

    # Layers placed after the look run on what the look produced, so they are
    # baked here rather than folded into the head. They are still per-pixel,
    # so the cube holds them exactly as it holds the head.
    post = check_bakeable(layers_chain(cfg, info, "after_look"))
    if post:
        out = run_chain(out, post, size)

    return np.clip(out, 0.0, 1.0), meta


def default_size(cfg: dict, domain: str) -> tuple[int, str]:
    """33 only when the cube is a plain Rec.709 remap of other 33 cubes.

    Then every node of the look and the secondary lands exactly on a node of
    the bake and the copy is essentially free. Everything else gets 65: a
    curve is a continuous spline with no nodes to line up with, a finer source
    cube would be thrown away, and anything upstream of CST OUT forces the
    bake to undo and redo the log to display transfer, which is the steepest
    thing in the chain.
    """
    if domain == "applelog":
        return 65, "combination LUT, carries the Apple Log to Rec.709 transfer"
    if not look_only(cfg):
        return 65, ("carries an inverted log to display transfer, because the "
                    "config grades upstream of CST OUT")
    if (cfg.get("curves") or {}).get("enabled"):
        return 65, ("the curves node is a continuous spline, so there are no "
                    "nodes for a 33 grid to line up with")
    fine = [r for r in rough_stages(cfg) if r[1] > 33]
    if fine:
        return 65, (f"{fine[0][0]} is a {fine[0][1]} cube, finer than a 33 grid "
                    f"could carry")
    return 33, "look-only LUT, a gentle Rec.709 to Rec.709 remap"


def look_only(cfg: dict) -> bool:
    """True when nothing upstream of CST OUT moves the image.

    Only then is a Rec.709 domain LUT a plain display-space remap. As soon as
    exposure, white balance or primaries are in play, the Rec.709 LUT has to
    undo the conversion, apply them, and redo it, which is every bit as steep
    as the combination LUT and needs the same grid.
    """
    p = cfg["primaries"]
    if abs(float(cfg["convert"]["exposure"])) > 1e-6:
        return False
    scalars = {"contrast": 1.0, "saturation": 1.0, "vibrance": 0.0,
               "temperature": 0.0, "tint": 0.0, "black_lift": 0.0,
               "highlight_rolloff": 0.0}
    for k, ident in scalars.items():
        if abs(float(p[k]) - ident) > 1e-6:
            return False
    for k, ident in (("lift", 0.0), ("gain", 1.0), ("gamma", 1.0),
                     ("brightness", 0.0)):
        if any(abs(v - ident) > 1e-6 for v in cinegrade._triplet(p[k], ident)):
            return False
    return True


NODE_NOTES = {
    "applelog": [
        "Resolve node tree: [ Clip ] -> [ Node 1: this LUT ] -> done.",
        "There must be NO Color Space Transform in front of it. This LUT does",
        "the Apple Log decode itself; converting first applies it twice.",
        "Project must be DaVinci YRGB, not DaVinci YRGB Color Managed.",
    ],
    "rec709": [
        "Resolve node tree: [ Clip ] -> [ Node 1: Apple Log to Rec.709 ]",
        "                            -> [ Node 2: this LUT ] -> done.",
        "This LUT expects Rec.709 in. On raw log it produces mud.",
        "Node 1 should be the exported CST_AppleLog_to_Rec709_*.cube for an",
        "exact match; a Resolve Color Space Transform node works but tone maps",
        "differently, so the result drifts from what cinegrade renders.",
    ],
}

# Resolve ships with Trilinear selected. Every number this tool measures was
# measured against tetrahedral, which is what cinegrade's lut3d uses, and the
# gap is real rather than theoretical: on a smooth look the same cube read
# trilinearly was four times further from the reference.
INTERP_NOTE = [
    "Set Project Settings > Color Management > 3D Lookup Table Interpolation",
    "to Tetrahedral. Resolve's default is Trilinear, which measurably widens",
    "the gap between this cube and what cinegrade renders.",
]


def write_lut(path: Path, data: np.ndarray, size: int, cfg: dict, meta: dict,
              domain: str, source_label: str, size_reason: str,
              missing: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    baked = baked_summary(cfg, meta)

    comments = [
        "Baked by content/grade/tools/bake_lut.py",
        f"Date: {_dt.date.today().isoformat()}",
        f"Config: {source_label}",
        "",
        f"DOMAIN: {'RAW APPLE LOG' if domain == 'applelog' else 'REC.709'}",
    ]
    comments += NODE_NOTES[domain]
    comments += [""] + INTERP_NOTE
    comments += [
        "",
        f"Grid: {size}^3. {size_reason}.",
        "",
        "Baked into this cube (per-pixel, exact to interpolation):",
    ]
    comments += [f"  - {b}" for b in baked]
    if missing:
        comments += [
            "",
            "NOT in this cube. No 3D LUT can hold these, they are still",
            "configured in the preset and still have to be rendered by",
            "cinegrade:",
        ]
        comments += [f"  - {name}: {why}" for name, why in missing]
    C.write_cube(path, data, size, path.stem, comments=comments)


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------

def fx_disabled(cfg: dict) -> dict:
    """The same config with every stage a LUT cannot hold turned off.

    This is the reference render: if the bake is correct, it is exactly what
    the LUT alone produces.
    """
    flat = json.loads(json.dumps(cfg))
    for name in ("halation", "bloom", "rgb_split", "radial_blur", "vignette"):
        flat["fx"][name]["enabled"] = False
    flat["grain"]["enabled"] = False
    flat["detail"] = {"soften": 0.0, "sharpen": 0.0}
    flat["letterbox"] = {"enabled": False, "aspect": 2.39}
    return flat


def read_frame(args: list[str], w: int, h: int) -> np.ndarray:
    r = subprocess.run(args, capture_output=True)
    if r.returncode != 0:
        raise GradeError(f"ffmpeg failed ({r.returncode})\n{r.stderr.decode()[-4000:]}")
    want = w * h * 3 * 2
    if len(r.stdout) != want:
        raise GradeError(f"frame readback was {len(r.stdout)} bytes, expected {want} "
                         f"for {w}x{h}")
    return np.frombuffer(r.stdout, "<u2").astype(np.float64).reshape(h, w, 3) / 65535.0


def verify(cfg: dict, lut_path: Path, domain: str, clip: str, time: float,
           meta: dict, cst_path: Path | None, autorotate: bool) -> dict:
    """Render one frame both ways and measure the gap.

    The reference is cinegrade's own graph with the spatial stages switched
    off, driven through the same ffmpeg that renders every other frame here.
    The test path is the raw decode followed by nothing but the baked cube.
    Anything the two disagree on is bake error, not preference.
    """
    info = cinegrade.probe(clip, autorotate=autorotate)
    w, h = info["width"], info["height"]
    flat = fx_disabled(cfg)

    ref_graph = cinegrade.graph_with_mask(flat, info, encode_out=False,
                                          tail_extra=["format=rgb48le"])
    base = ["ffmpeg", "-v", "error", "-y"]
    if not autorotate:
        base += ["-noautorotate"]
    base += ["-ss", str(time), "-i", clip]
    tail = ["-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"]
    ref = read_frame(base + ["-filter_complex", ref_graph, "-map", "[vout]"] + tail,
                     w, h)

    # The test path repeats only the container-level decode the LUT's domain is
    # defined against, then hands the frame to the cube and nothing else.
    decode = (f"scale=in_color_matrix=bt2020:in_range={info['color_range']}"
              f":out_range=full,format=gbrp16le")
    luts = []
    if domain == "rec709":
        luts.append(f"lut3d=file={cinegrade.esc(cst_path)}:interp=tetrahedral")
    luts.append(f"lut3d=file={cinegrade.esc(lut_path)}:interp=tetrahedral")
    test_vf = ",".join([decode] + luts + ["format=rgb48le"])
    test = read_frame(base + ["-vf", test_vf] + tail, w, h)

    diff = np.abs(test - ref)
    luma = C.luma709(ref)[..., 0]
    hi = luma > np.quantile(luma, 0.90)
    lo = luma < np.quantile(luma, 0.10)
    worst_px = diff.max(axis=2)
    return {
        "clip": clip, "time": time, "pixels": w * h,
        "max": diff.reshape(-1, 3).max(axis=0),
        "mean": diff.reshape(-1, 3).mean(axis=0),
        "p999": np.quantile(diff.reshape(-1, 3), 0.999, axis=0),
        "max_hi": diff[hi].max() if hi.any() else 0.0,
        "max_lo": diff[lo].max() if lo.any() else 0.0,
        # A bare maximum is a bad summary here. The extreme is almost always a
        # few hundred pixels sitting exactly where one output channel clips at
        # 1.0, which is a hard kink no interpolant can follow. The count of
        # pixels that miss by more than one 8-bit step says whether that is a
        # rounding artefact or an actual region of the picture.
        "over1": int((worst_px > 1.0 / 255.0).sum()),
        "over4": int((worst_px > 4.0 / 255.0).sum()),
    }


def print_verify(v: dict) -> None:
    def codes(a):
        return " ".join(f"{x * 255.0:6.3f}" for x in np.atleast_1d(a))

    n = v["pixels"]
    print(f"  frame: {Path(v['clip']).name} at t={v['time']}s, {n:,} pixels")
    print("                        R      G      B     (8-bit code units, 0-255)")
    print(f"    max abs diff   {codes(v['max'])}")
    print(f"    mean abs diff  {codes(v['mean'])}")
    print(f"    99.9th pct     {codes(v['p999'])}")
    print(f"    worst in the brightest 10% of the frame: "
          f"{v['max_hi'] * 255.0:.3f} codes")
    print(f"    worst in the darkest 10% of the frame:   "
          f"{v['max_lo'] * 255.0:.3f} codes")
    print(f"    pixels off by more than 1 code: {v['over1']:,} "
          f"({v['over1'] / n * 100:.4f}%), by more than 4: {v['over4']:,} "
          f"({v['over4'] / n * 100:.4f}%)")


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def default_clip() -> str | None:
    clips = sorted((GRADE.parent / "footage").glob("*.MOV"))
    return str(clips[0]) if clips else None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--preset", "-p", help="a name from grade/presets/")
    src.add_argument("--config", help="path to a config JSON")
    ap.add_argument("--domain", required=True, choices=["applelog", "rec709"])
    ap.add_argument("--size", type=int,
                    help="cube grid size (default 65, or 33 for a look-only "
                         "rec709 LUT)")
    ap.add_argument("--out", "-o", help="output .cube path")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero if the config has stages a LUT cannot hold")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--verify-clip", default=None)
    ap.add_argument("--verify-time", type=float, default=2.0)
    ap.add_argument("--no-autorotate", action="store_true")
    a = ap.parse_args()

    name = a.preset or Path(a.config).stem
    cfg = cinegrade.load_preset(a.preset or a.config)
    label = (f'preset "{a.preset}" ({cinegrade.PRESETS / (a.preset + ".json")})'
             if a.preset else str(Path(a.config).resolve()))

    check_config_is_understood(cfg)
    domain_is_log = a.domain == "applelog"
    size, size_reason = default_size(cfg, a.domain)
    if a.size:
        size = a.size
        size_reason = f"set by hand with --size {a.size}"
    if size < 2 or size > 129:
        raise GradeError("--size must be between 2 and 129")

    missing = unbakeable_stages(cfg)
    out = Path(a.out) if a.out else EXPORT / f"{name}_{a.domain}_{size}.cube"

    print(f"baking {label}")
    print(f"  domain {a.domain}, grid {size}^3 ({size_reason})")

    data, meta = bake(cfg, a.domain, size)

    cst_path = None
    if a.domain == "rec709":
        cst_path = write_companion_cst(meta["tonemap"], meta["encode"])

    write_lut(out, data, size, cfg, meta, a.domain, label, size_reason, missing)

    print()
    print("=" * 72)
    if a.domain == "applelog":
        print("  WROTE A COMBINATION LUT. IT EXPECTS RAW APPLE LOG.")
    else:
        print("  WROTE A REC.709 LUT. IT EXPECTS ALREADY-CONVERTED FOOTAGE.")
    print("=" * 72)
    for line in NODE_NOTES[a.domain] + [""] + INTERP_NOTE:
        print(f"  {line}" if line else "")
    print()
    print(f"  file: {out}")
    if cst_path:
        print(f"  put this on node 1: {cst_path}")
    print("=" * 72)

    print()
    print("baked into the cube:")
    for part in baked_summary(cfg, meta):
        print(f"  + {part}")

    rough = [r for r in rough_stages(cfg) if r[3] > ROUGH_CURVATURE]
    if rough:
        print()
        print("WARNING: this grade contains stages that are themselves coarse")
        print("  .cube tables, and a bake has to resample them onto its own grid:")
        for label, lsize, step, curve in rough:
            print(f"    {label:22} {lsize} grid, biggest step "
                  f"{step * 255:.0f} of 255 code values inside one cell, "
                  f"curvature {curve:.3f}")
        if domain_is_log:
            print("  A combination LUT samples on a LOG-spaced grid, which through")
            print("  the mid tones is coarser in display terms than the 33 grid")
            print("  these were authored on, so their kinks fall between nodes.")
            print("  Expect several code values of error below. --domain rec709")
            print("  samples in their own space and does much better.")
        else:
            print("  A Rec.709 grid lines up with them exactly when the config")
            print("  leaves everything upstream of CST OUT alone. Exposure,")
            print("  white balance and primaries pull it out of alignment.")

    if missing:
        print()
        print("NOT baked. A 3D LUT cannot hold any of these, and they are")
        print("still in the preset. Keep rendering them with cinegrade:")
        for stage, why in missing:
            print(f"  - {stage:18} {why}")
        if a.strict:
            print()
            print("--strict: refusing to call this a complete grade.")
            return 2

    if a.no_verify:
        print()
        print("verification skipped (--no-verify). The LUT is unproven.")
        return 0

    clip = a.verify_clip or default_clip()
    if not clip:
        print()
        print("verification skipped: no clip in content/footage/. "
              "Pass --verify-clip.")
        return 0

    print()
    print("verifying: full cinegrade chain with the spatial stages off, "
          "against the cube alone")
    v = verify(cfg, out, a.domain, clip, a.verify_time, meta, cst_path,
               autorotate=not a.no_autorotate)
    print_verify(v)
    worst = float(np.max(v["max"]) * 255.0)
    mean = float(np.mean(v["mean"]) * 255.0)
    # The bare maximum is judged on, but not by itself: on a real frame it is
    # set by a few hundred pixels sitting on an output clipping edge, where no
    # interpolant can follow the kink. The 99.9th percentile is what tells you
    # whether a REGION of the picture is wrong rather than a speckle.
    tail = float(np.max(v["p999"]) * 255.0)
    verdict = ("indistinguishable" if tail < 0.5 else
               "close enough to grade with" if tail < 1.0 else
               "TOO LARGE, do not ship this cube")
    print(f"    verdict: 99.9% of pixels are within {tail:.2f} of 255 code "
          f"values, {verdict}")
    print(f"             mean {mean:.3f} codes, single worst pixel {worst:.2f} codes")
    if tail >= 1.0:
        print()
        print("  More than one pixel in a thousand is off by a visible amount,")
        print("  so this cube is not a faithful copy of the grade. What to do:")
        if rough:
            print("    - the rough stages listed above are the cause. Bake")
            print("      --domain rec709 instead, which samples in their own")
            print("      space, and do the primaries in Resolve.")
        if domain_is_log:
            print("    - raising --size does help (the error roughly halves from")
            print("      65 to 129) but Resolve documents 17, 33 and 65 as the")
            print("      sizes it writes, and nothing confirms it loads larger")
            print("      ones, so treat that as untested rather than a fix.")
        print("    - or render this preset with cinegrade and hand Resolve the")
        print("      finished file, which has no bake error at all.")
    return 0 if tail < 1.0 else 3


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GradeError as exc:
        sys.exit(str(exc))
