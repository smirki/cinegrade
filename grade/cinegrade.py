#!/usr/bin/env python3
"""cinegrade - a headless color grading pipeline for Apple Log footage.

The node tree, in order:

    LOG        decode ProRes, normalise range/matrix, exposure as a log-domain shift
    CONVERT    Apple Log / BT.2020 -> Rec.709 via a technical CST LUT
    PRIMARIES  contrast, saturation, temp/tint, lift-gamma-gain
    LOOK       creative .cube LUT (mine, or any purchased one)
    FX         halation, bloom, RGB split, radial blur, vignette
    GRAIN      film grain
    OUT        encode

Every stage is optional and every parameter lives in a JSON preset, so an agent
can grade by editing a small dict instead of hand-writing ffmpeg graphs.

    python cinegrade.py render IN.MOV --preset cinekit -o OUT.mov
    python cinegrade.py still  IN.MOV --preset cinekit --time 2 -o frame.png
    python cinegrade.py compare IN.MOV --looks kodak2383,warm_film --time 2
    python cinegrade.py scopes IN.MOV --preset cinekit --time 2
"""

from __future__ import annotations

import argparse
import json
import math
import shlex
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LUT_TECH = ROOT / "luts" / "technical"
LUT_LOOKS = ROOT / "luts" / "looks"
PRESETS = ROOT / "presets"

# Apple Log code-value delta per stop of exposure, from the published curve
# (gamma * log2(2) where gamma = 0.08492 over the log segment).
APPLE_LOG_STOP = 0.08492


class GradeError(Exception):
    """A user-fixable problem: a missing LUT, an unknown preset, a bad graph.

    Raised rather than sys.exit so the same functions can back a long-running
    process (the studio server) without one bad request taking the whole thing
    down. main() turns it back into the CLI's exit-with-a-message behaviour.
    """


# --------------------------------------------------------------------------
# defaults
# --------------------------------------------------------------------------

DEFAULTS = {
    "convert": {
        "tonemap": "aces", "exposure": 0.0,
        # dwg    = CST in to DaVinci Wide Gamut, grade there, CST out (default)
        # direct = one LUT straight to Rec.709
        # rec709 = the source is ALREADY display referred Rec.709, so there is
        #          no log to undo: no CST at either end, grade in place
        "working_space": "dwg",
        # rec709a for a Mac display, gamma24 for a calibrated external monitor
        "encode": "rec709a",
    },
    "primaries": {
        "contrast": 1.0, "pivot": None, "saturation": 1.0, "vibrance": 0.0,
        "temperature": 0.0, "tint": 0.0, "brightness": 0.0,
        "lift": 0.0, "gamma": 1.0, "gain": 1.0,
        "black_lift": 0.0, "highlight_rolloff": 0.0,
    },
    "look": {"lut": None, "mix": 1.0},
    "detail": {"soften": 0.0, "sharpen": 0.0},
    "fx": {
        "halation": {"enabled": False, "threshold": 0.62, "sigma": 26,
                     "strength": 0.55, "tint": [1.0, 0.34, 0.16]},
        "bloom": {"enabled": False, "threshold": 0.72, "sigma": 70,
                  "strength": 0.28, "tint": [1.0, 0.97, 0.92]},
        "rgb_split": {"enabled": False, "amount": 1.6},
        "radial_blur": {"enabled": False, "sigma": 9, "start": 0.55, "end": 1.0},
        "vignette": {"enabled": False, "amount": 0.45, "radius": 0.85},
    },
    # size is the grain plate's downscale factor. Real film grain has a
    # physical size; per-pixel noise at 4K averages away to nothing the moment
    # the clip is viewed at 1080p, which is why it reads as "no grain".
    "grain": {"enabled": False, "strength": 40, "size": 3, "opacity": 0.5},
    "letterbox": {"enabled": False, "aspect": 2.39},
    # The curves node. Runs after CST OUT, in Rec.709 display code, because a
    # curve is drawn against what you can see. Drawing it in the DWG working
    # space instead would put mid grey at 0.336 and a hand-placed point at 0.5
    # would be pulling on a highlight, not a mid tone.
    "curves": {
        "enabled": False, "interp": "pchip",
        "master": [[0.0, 0.0], [1.0, 1.0]],
        "r": [[0.0, 0.0], [1.0, 1.0]],
        "g": [[0.0, 0.0], [1.0, 1.0]],
        "b": [[0.0, 0.0], [1.0, 1.0]],
    },
    # HSL qualifier (Resolve's secondary). Baked to a .cube rather than run as
    # a per-pixel expression: the key and the correction are both pure
    # functions of one pixel's RGB, so a 33-cube plus tetrahedral interpolation
    # is exact enough and costs a table lookup instead of a geq evaluation per
    # pixel per frame. It is a colour key only, never a shape.
    "secondary": {
        "enabled": False, "show_mask": False, "invert": False,
        "hue_center": 30.0, "hue_width": 40.0, "hue_soft": 15.0,
        "sat_low": 0.10, "sat_high": 1.0, "sat_soft": 0.10,
        "lum_low": 0.0, "lum_high": 1.0, "lum_soft": 0.10,
        "hue_shift": 0.0, "sat_gain": 1.0, "lum_gain": 1.0,
        "tint": [0.0, 0.0, 0.0], "strength": 1.0,
    },
    # Power window: the shape half of a secondary. The qualifier above picks
    # pixels by colour; this picks them by position, and the two multiply, so
    # "the orange only inside this oval" is one grade rather than two.
    #
    # Every geometric value is a FRACTION of the frame, never a pixel count.
    # That is what lets a 960 wide preview and a 3840 wide render agree without
    # scale_for_preview needing a case for any of it: the same numbers describe
    # the same shape at any size. cx/cy are the centre, w/h the FULL extent
    # (not the half axis), rotation is degrees clockwise on screen, softness is
    # the feather width as a fraction of the shape's own radius.
    "window": {
        "enabled": False, "shape": "ellipse",
        "cx": 0.5, "cy": 0.5, "w": 0.6, "h": 0.6,
        "rotation": 0.0, "softness": 0.15, "invert": False,
    },
    "output": {"codec": "prores_ks", "profile": 3, "crf": 16, "preset": "slow"},
}


def deep_merge(base: dict, override: dict) -> dict:
    out = deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_preset(name_or_path: str | None) -> dict:
    if not name_or_path:
        return deepcopy(DEFAULTS)
    p = Path(name_or_path)
    if not p.exists():
        p = PRESETS / f"{name_or_path}.json"
    if not p.exists():
        avail = sorted(x.stem for x in PRESETS.glob("*.json"))
        raise GradeError(
            f"preset not found: {name_or_path}. available: {', '.join(avail)}")
    return deep_merge(DEFAULTS, json.loads(p.read_text()))


# --------------------------------------------------------------------------
# probe
# --------------------------------------------------------------------------

def probe(path: str, autorotate: bool = True) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_streams", "-show_entries", "stream_side_data", "-of", "json", path],
        capture_output=True, text=True, check=True).stdout
    st = json.loads(out)["streams"][0]
    w, h = int(st["width"]), int(st["height"])
    rot = 0
    for sd in st.get("side_data_list", []):
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    if autorotate and abs(rot) in (90, 270):
        w, h = h, w        # ffmpeg auto-rotates, so the graph sees these
    return {
        "width": w, "height": h, "rotation": rot, "autorotate": autorotate,
        "pix_fmt": st.get("pix_fmt"), "color_range": st.get("color_range", "tv"),
        "color_space": st.get("color_space", "bt2020nc"),
        "nb_frames": st.get("nb_frames"), "duration": st.get("duration"),
        "codec": st.get("codec_name"), "profile": st.get("profile"),
    }


# --------------------------------------------------------------------------
# filter graph construction
# --------------------------------------------------------------------------

def source_matrix(info) -> str:
    """The YUV matrix to decode this source with.

    A property of the FILE, not of the grade, which is why it reads the clip's
    own tag rather than the working space. It used to be hard coded to bt2020,
    correct for the Apple Log clips this tool was built for and wrong for an
    ordinary delivery file, where decoding bt709 as bt2020 skews every hue
    before the grade even begins. Anything not explicitly tagged as a display
    matrix keeps the old bt2020 answer, so no camera clip changes.
    """
    return "bt709" if (info or {}).get("color_space") in DISPLAY_MATRICES else "bt2020"


def f_log_stage(cfg, info, normalised=False) -> list[str]:
    """Normalise to full-range RGB and apply exposure in the log domain.

    Exposure is done here, before the CST, because a constant offset on a log
    signal is exactly a stop change. Doing it after the CST would instead
    stretch already tone-mapped values and blow out the highlights.

    normalised=True means the caller already ran the scale/range/matrix step
    (the studio server does this once per source frame and caches it, since it
    does not depend on any grade parameter) and is handing this stage an
    already-normalised gbrp16le-equivalent buffer. Skipping straight to
    format=gbrp16le is a repack, not a value change, so the exposure lut below
    still lands on the exact same numbers either way.

    setparams here is not optional decoration: a buffer that crosses a process
    boundary as raw bytes (the studio server's stdin pipe) arrives with no
    range tag on the link, while a buffer that stayed inside one continuous
    ffmpeg graph still carries "full range" on the link from this same scale
    step (see the non-normalised branch below). Most of this node tree
    (lut3d, blend, curves, colorchannelmixer) works directly in RGB and never
    looks at that tag, but a filter that has to convert to YUV internally
    (unsharp and vignette, both used later in the graph) picks its RGB<->YUV
    range and matrix from it, so an untagged buffer and a tagged one can round
    differently on the exact same pixels.

    Restamping range here (and only range) was chosen after measuring three
    options against every real preset in grade/presets, not just guessed:
    - no stamp at all: unsharp alone is off by up to 1 code value on ~70% of
      pixels; anything that also has grain or vignette is off by up to 14.
    - range AND an explicit colorspace stamp: fixes unsharp cleanly (0 diff),
      but over-corrects vignette. ffmpeg's own auto-inserted RGB->YUV step for
      vignette does not use a stable colorspace hint in the original
      single-pass graph either (it depends on what else is in the graph, see
      below), so forcing one here can make vignette's mismatch worse than
      leaving it alone (up to 7).
    - range only (what ships here): 0 diff on every preset that carries grain
      (cinekit, cinematic, forest, golden_haze, premium, reels) and on every
      preset without vignette. Presets that combine vignette with sharpen and
      no grain (blockbuster, blue_hour, film_portrait, film_tungsten,
      interior, natural, punch, silverblue) still show a small residual: up
      to 5-6 code values out of 255 on a minority of pixels, mean well under 1.
      That residual traces to ffmpeg's own auto_scale: it is a whole-graph
      negotiation, not a per-link one, so what colorspace it silently assigns
      to vignette's internal YUV round trip depends on the exact shape of the
      rest of the graph (grain present or not changes it even in the ORIGINAL
      single-pass path, confirmed with -v 48 debug logs). No single stamp at
      this one point can pin every downstream filter to the value the
      original path happens to land on for every combination, short of
      forcing an explicit format on vignette's own node in the shared graph
      builder below, which would also change what the CLI (not just the
      studio preview) has always rendered for those presets and was ruled out
      for that reason. Range-only is the option that is exactly right most
      often and small everywhere else.
    """
    ws = cfg["convert"]["working_space"]
    if normalised:
        chain = ["setparams=range=full", "format=gbrp16le"]
    else:
        chain = [
            f"scale=in_color_matrix={source_matrix(info)}"
            f":in_range={info['color_range']}:out_range=full",
            "format=gbrp16le",
        ]
    # Exposure and white balance are both per-channel offsets here, because on
    # a log signal an offset IS a linear gain. That makes this the exact
    # equivalent of Resolve's "set the node to linear gamma and use the gain
    # wheel" white balance, applied at the point where the sensor's own gain
    # would have acted rather than after a display transform.
    p = cfg["primaries"]
    ev = float(cfg["convert"]["exposure"])
    temp, tint = float(p["temperature"]), float(p["tint"])
    off_r = ev + temp
    off_g = ev + tint
    off_b = ev - temp
    if any(abs(v) > 1e-6 for v in (off_r, off_g, off_b)):
        if ws == "rec709":
            # There is no log curve here to add an offset to, so the same stop
            # has to be spent as a gain instead. On a display signal that is a
            # multiply, not an add: code = (linear ** (1/g)), so scaling linear
            # by 2**stops scales the code by 2**(stops/g). Keeping the unit as
            # stops means exposure, temperature and tint mean the same thing to
            # the user on both kinds of source, rather than one set of numbers
            # doing something different depending on what was loaded.
            def ex(stops):
                return f"clip(val*{2.0 ** (stops / DISPLAY_GAMMA):.6f},0,maxval)"
        else:
            def ex(stops):
                return f"clip(val+{stops * APPLE_LOG_STOP:.6f}*maxval,0,maxval)"
        chain.append(f"lut=r='{ex(off_r)}':g='{ex(off_g)}':b='{ex(off_b)}'")
    return chain


# 18% grey lands at a different code value in each working space, and the
# contrast pivot has to follow it or contrast also shifts exposure.
#
# Both numbers are the code that PRIMARIES sees, which is not the same as the
# code the viewer sees. build_graph runs primaries between CST IN and CST OUT,
# so on `dwg` the grey has already been through AppleLog_to_DWG (0.3360), but
# on `direct` CST IN is empty and primaries still operate on raw Apple Log
# (0.4883). The direct entry used to be 0.3919, an output-side Rec.709 value,
# which put the pivot 0.096 below the real grey: contrast 1.6 then dragged 18%
# grey from 0.3920 to 0.5211, a shift of 33 in 255, so the one control that
# exists to hold exposure still while contrast moves was doing the opposite.
# Both values are derived in grade/tools/colorlib.py terms: decode 18% scene
# linear back through the same transforms make_cst.py bakes into the LUTs.
# No shipped preset uses working_space "direct", so nothing already graded moves.
# The rec709 entry is 18% grey through the rec709a encoder, which is where an
# already display referred source starts: nothing is undone on that path, so
# what primaries sees is simply the delivery code.
MID_GREY_CODE = {"dwg": 0.3360, "direct": 0.4883, "rec709": 0.4587}

# The transfer a display referred source is assumed to carry. Only used to turn
# a stop into a code multiply on the rec709 path; the grade itself never
# linearises, so an exact match to the file's real transfer is not required.
DISPLAY_GAMMA = 2.4

# The vignette radius that reproduces the falloff this engine shipped with.
# It is the DEFAULTS value on purpose: radius was inert until now, so anything
# else here would silently restyle every preset that already draws a vignette.
VIGNETTE_RADIUS_NEUTRAL = 0.85

# ffmpeg's colorchannelmixer refuses a coefficient outside [-2, 2], and the
# largest coefficient of the saturation matrix is 0.0722 + 0.9278 * sat, so a
# single pass dies just past 2.077 (measured by binary search: the error is
# "Value 2.181100 for parameter 'rr' out of range"). The schema slider goes to
# 2.5, so the top of the range used to crash the render instead of saturating.
SAT_MAX_PER_PASS = 2.07


def _tech_lut(name):
    lut = LUT_TECH / name
    if not lut.exists():
        raise GradeError(f"missing technical LUT {lut}; run tools/make_cst.py")
    return f"lut3d=file={esc(lut)}:interp=tetrahedral"


def _triplet(value, default):
    """Read a primaries value that may be one number or [r, g, b].

    Every preset written before the colour wheels existed stores scalars, and
    a scalar has to keep producing the exact same filter string or those
    grades would silently shift. A three-list is the wheel spelling of the
    same control: it lets lift, gamma, gain and brightness tint a tonal range
    instead of only moving it.
    """
    if value is None:
        value = default
    if isinstance(value, (list, tuple)):
        return [float(value[0]), float(value[1]), float(value[2])]
    return [float(value)] * 3


def f_convert_in(cfg) -> list[str]:
    if cfg["convert"]["working_space"] == "dwg":
        return [_tech_lut("AppleLog_to_DWG.cube")]
    return []


# Apple Log clips come off the camera tagged bt2020nc; an ordinary delivery file
# is tagged bt709. That one field is enough to tell the two apart, and it is the
# only thing standing between a user and a silently ruined picture.
LOG_MATRICES = {"bt2020nc", "bt2020c", "bt2020_ncl", "bt2020_cl"}
DISPLAY_MATRICES = {"bt709", "smpte170m", "bt470bg", "smpte240m", "fcc"}


def check_source_space(cfg, info) -> None:
    """Refuse a source and working space that cannot go together.

    Grading a normal Rec.709 video down the Apple Log path does not error, it
    just quietly produces garbage: the CST out applies a log to display
    transform to a picture that never had a log curve, so the tone map runs
    twice. Measured on a real delivery file, median luma fell from 0.332 to
    0.238 and mean saturation went from 0.33 to 0.59, giving neon greens and
    blown skin. A wrong picture that renders successfully is worse than a
    refusal, because nothing tells the user to look for a cause.

    Only clearly tagged mismatches are refused. An untagged file is left alone
    rather than guessed at, since the caller may well know better than the
    metadata does.
    """
    ws = cfg["convert"]["working_space"]
    matrix = (info or {}).get("color_space") or ""
    if ws == "rec709" and matrix in LOG_MATRICES:
        raise GradeError(
            f"source is tagged {matrix}, which is camera log, but working_space "
            f"is 'rec709' (for footage that is already display referred). "
            f"The log curve would never be undone and the picture would stay "
            f"flat and desaturated. Use working_space 'dwg' or 'direct'.")
    if ws != "rec709" and matrix in DISPLAY_MATRICES:
        raise GradeError(
            f"source is tagged {matrix}, which is already display referred "
            f"Rec.709, but working_space is '{ws}', which expects Apple Log. "
            f"That applies a log to display transform to a picture that never "
            f"had a log curve and silently wrecks it (measured: median luma "
            f"0.332 to 0.238, saturation 0.33 to 0.59). "
            f"Use working_space 'rec709' for this clip.")


def f_convert_out(cfg) -> list[str]:
    c = cfg["convert"]
    if c["working_space"] == "rec709":
        # Already in the delivery space. Applying a technical LUT here would be
        # a second log to display conversion on a picture that never had a log
        # curve, which is the exact failure this mode exists to prevent.
        return []
    if c["working_space"] == "dwg":
        return [_tech_lut(f"DWG_to_Rec709_{c['tonemap']}_{c['encode']}.cube")]
    # The direct path used to ignore `encode` entirely and always load the
    # unsuffixed LUT, which make_cst.py builds with its own default of
    # gamma24. So the control was not merely inert here, it was mislabelling
    # the output: the UI said rec709a while the picture was gamma24. Per
    # encode LUTs fix that. The unsuffixed file is kept as a fallback so a
    # tree whose technical LUTs predate this still renders, and the newly
    # generated _gamma24 cubes are byte identical to the old unsuffixed ones,
    # so nothing that was already correct moves.
    per_encode = LUT_TECH / f"AppleLog_to_Rec709_{c['tonemap']}_{c['encode']}.cube"
    if per_encode.exists():
        return [_tech_lut(per_encode.name)]
    return [_tech_lut(f"AppleLog_to_Rec709_{c['tonemap']}.cube")]


def _saturation_matrix(sat: float) -> str:
    lr, lg, lb = 0.2126, 0.7152, 0.0722
    i = 1.0 - sat
    return (
        f"colorchannelmixer="
        f"rr={i * lr + sat:.5f}:rg={i * lg:.5f}:rb={i * lb:.5f}:"
        f"gr={i * lr:.5f}:gg={i * lg + sat:.5f}:gb={i * lb:.5f}:"
        f"br={i * lr:.5f}:bg={i * lg:.5f}:bb={i * lb + sat:.5f}")


def _saturation_chain(sat: float) -> list[str]:
    """Saturation as one or more luma-preserving matrices.

    One matrix is the whole story below SAT_MAX_PER_PASS and is what this
    engine has always emitted, so nothing already graded moves. Past that,
    ffmpeg rejects the coefficients, so the move is split across several
    passes instead of failing the render.

    Splitting is exact rather than an approximation. The saturation matrix is
    M(s) = (1 - s) * L + s * I where L is the rank one luma projection, and L
    is idempotent, so M(a) * M(b) = M(a * b). N passes of s ** (1/N) therefore
    compose to exactly M(s), which is why this is a fix and not a fudge.
    """
    if sat <= SAT_MAX_PER_PASS:
        return [_saturation_matrix(sat)]
    passes = 2
    while sat ** (1.0 / passes) > SAT_MAX_PER_PASS:
        passes += 1
    step = sat ** (1.0 / passes)
    return [_saturation_matrix(step)] * passes


def _tone_ends_points(bl: float, hr: float, pivot: float, n: int = 6) -> str:
    """Curve points for black lift and highlight roll-off: a toe and a shoulder.

    These two controls promise to take the hard ends off without touching the
    mid tones, and they used to do close to the opposite. The curve was
    `0/bl 0.5/0.5 1/(1-hr)`, drawn inside the WORKING space, where 0.5 is not
    mid grey: in DWG grey sits at 0.336, so the hand-placed middle point landed
    on a highlight and the long span back to the origin dragged the mid tones
    with it. Measured through ffmpeg on a ramp, black_lift 0.10 alone pushed
    grey +6.0/255, highlight_rolloff 0.35 alone pushed it +8.2/255, and together
    they moved it +14.2/255.

    Moving that middle point onto the real mid grey is necessary but not
    sufficient. It pins grey exactly, yet a three point spline still has to bend
    across the whole range to reach it, so the upper mid tones sagged and the
    image median still moved about 0.011. What the controls actually need is for
    each end to fall off and be GONE by mid grey, so the shape here is two
    explicit segments that meet there:

        toe       f(x) = bl + (p - bl) * (x/p) ** k       for x <= p
        shoulder  f(x) = p + (1 - hr - p) * (1 - (1 - u) ** k)   for x >= p

    with u the position between p and white. The exponents are chosen, not
    tuned: each is exactly the value that makes the segment's slope 1 where it
    meets the pivot. That matters because a slope of 1 on both sides means the
    curve passes through mid grey with no kink and no change of gradient, which
    is the precise technical meaning of "does not touch mid-tone contrast".
    Both segments still land exactly on `bl` and `1 - hr` at the ends, so the
    controls keep their advertised effect on the floor and the ceiling.

    Chosen this way rather than as a simpler quadratic bump because a power
    segment stays monotone across the entire advertised slider range
    (black_lift -0.1 to 0.3, highlight_rolloff 0 to 0.5), where a quadratic
    inverts the deep shadows past black_lift 0.168. `interp=pchip` at the call
    site keeps ffmpeg from re-introducing overshoot between the points a
    natural cubic would.
    """
    # Past these the segment has no room left to land on and the exponent blows
    # up or goes negative. The sliders stop short of both, so this only guards
    # a hand-edited config.
    bl = min(bl, pivot - 1e-3)
    hr = min(hr, 1.0 - pivot - 1e-3)

    pts = []
    if abs(bl) > 1e-6:
        k = pivot / (pivot - bl)
        pts += [(v * pivot, bl + (pivot - bl) * v ** k)
                for v in (i / (n - 1) for i in range(n))]
    else:
        # A spread of points on y=x, not a single 0/0. pchip takes each node's
        # slope from its neighbouring secants, so with one lone point down here
        # the shape of this half is dictated by whatever the other half is
        # doing and it drifts off identity. Points that are all exactly on y=x
        # make every local secant exactly 1, which is what keeps an end the
        # user has not touched genuinely untouched.
        pts += [(v * pivot, v * pivot) for v in (i / (n - 1) for i in range(n))]
    pts.append((pivot, pivot))
    if abs(hr) > 1e-6:
        k = (1.0 - pivot) / (1.0 - hr - pivot)
        pts += [(pivot + u * (1.0 - pivot),
                 pivot + (1.0 - hr - pivot) * (1.0 - (1.0 - u) ** k))
                for u in (i / (n - 1) for i in range(1, n))]
    else:
        pts += [(pivot + u * (1.0 - pivot), pivot + u * (1.0 - pivot))
                for u in (i / (n - 1) for i in range(1, n))]

    seen, out = set(), []
    for x, y in sorted(pts):
        key = round(x, 4)
        if key in seen:                # ffmpeg rejects a repeated x coordinate
            continue
        seen.add(key)
        out.append(f"{key:.4f}/{min(1.0, max(0.0, y)):.4f}")
    return " ".join(out)


def f_primaries(cfg) -> list[str]:
    """Runs inside the working space, before the output transform.

    Temperature and tint are deliberately absent: they are handled in the log
    stage, where an offset is exactly a linear gain.
    """
    p = cfg["primaries"]
    ws = cfg["convert"]["working_space"]
    pivot = p["pivot"] if p.get("pivot") is not None else MID_GREY_CODE[ws]
    chain = []

    # Lift and gain are one linear remap: black lands on `lift`, white on
    # `gain`. This used to be a colorlevels call, but colorlevels clamps its
    # output points to [0, 1], so gain above 1.0 (brightening, which is the
    # normal direction for a gain wheel) errored the render out instead of
    # working. The lut form is the same arithmetic with no such cap. No shipped
    # preset sets either value, so nothing already graded moves.
    lift, gain = _triplet(p["lift"], 0.0), _triplet(p["gain"], 1.0)
    if any(abs(v) > 1e-6 for v in lift) or any(abs(v - 1.0) > 1e-6 for v in gain):
        chain.append("lut=" + ":".join(
            f"{c}='clip({lift[i]:.4f}*maxval+val*{gain[i] - lift[i]:.4f},0,maxval)'"
            for i, c in enumerate("rgb")))

    # Contrast around the real mid grey of this space. ffmpeg's eq=contrast
    # pivots on 0.5, which in DWG (grey at 0.336) would darken as it contrasts.
    c = float(p["contrast"])
    if abs(c - 1.0) > 1e-6:
        e = f"clip((val-{pivot:.4f}*maxval)*{c:.4f}+{pivot:.4f}*maxval,0,maxval)"
        chain.append(f"lut=r='{e}':g='{e}':b='{e}'")

    # Brightness is the offset wheel: a flat add, so it moves the whole curve
    # rather than pivoting it the way lift does.
    b = _triplet(p["brightness"], 0.0)
    if any(abs(v) > 1e-6 for v in b):
        chain.append("lut=" + ":".join(
            f"{c}='clip(val+{b[i]:.4f}*maxval,0,maxval)'"
            for i, c in enumerate("rgb")))

    g = _triplet(p["gamma"], 1.0)
    if any(abs(v - 1.0) > 1e-6 for v in g):
        chain.append("lut=" + ":".join(
            f"{c}='pow(val/maxval,{1.0 / g[i]:.4f})*maxval'"
            for i, c in enumerate("rgb")))

    # Saturation as an explicit luma-preserving matrix, so it works in RGB and
    # does not force a detour through YUV the way eq=saturation would.
    sat = float(p["saturation"])
    if abs(sat - 1.0) > 1e-6:
        chain += _saturation_chain(sat)

    # Vibrance protects already-saturated colours and skin, which is the
    # "subtle cinematic saturation" the HSV-gain technique is reaching for.
    vib = float(p["vibrance"])
    if abs(vib) > 1e-6:
        chain.append(f"vibrance=intensity={vib:.4f}")

    # Black lift plus highlight roll-off: a toe and a shoulder, as one curve.
    bl, hr = float(p["black_lift"]), float(p["highlight_rolloff"])
    if abs(bl) > 1e-6 or abs(hr) > 1e-6:
        chain.append(f"curves=all='{_tone_ends_points(bl, hr, pivot)}':interp=pchip")

    return chain


def f_detail(cfg) -> list[str]:
    """Soften then sharpen, the action-camera trick from the tutorial.

    Softening first and adding sharpness back deliberately reads cleaner than
    the harsh in-camera sharpening it replaces.
    """
    d = cfg.get("detail", {})
    chain = []
    soft = float(d.get("soften", 0.0))
    if soft > 1e-6:
        chain.append(f"gblur=sigma={soft:.3f}")
    sharp = float(d.get("sharpen", 0.0))
    if sharp > 1e-6:
        chain.append(f"unsharp=luma_msize_x=5:luma_msize_y=5:luma_amount={sharp:.3f}")
    return chain


def f_look(cfg) -> list[str]:
    lut = cfg["look"]["lut"]
    if not lut:
        return []
    p = Path(lut)
    if not p.exists():
        p = LUT_LOOKS / (lut if lut.endswith(".cube") else f"{lut}.cube")
    if not p.exists():
        avail = sorted(x.stem for x in LUT_LOOKS.glob("*.cube"))
        raise GradeError(
            f"look LUT not found: {lut}. available: {', '.join(avail)}")
    return [f"lut3d=file={esc(p)}:interp=tetrahedral"]


# --- curves ---------------------------------------------------------------

def _curve_is_identity(pts) -> bool:
    if not pts or len(pts) < 2:
        return True
    return all(abs(float(x) - float(y)) < 1e-6 for x, y in pts)


def f_curves(cfg) -> list[str]:
    """The curves node, wired straight to ffmpeg's own spline.

    interp defaults to pchip rather than ffmpeg's `natural`, because a natural
    cubic spline overshoots between widely spaced points: a single lifted
    shadow point can send the mid tones above the values on either side of it,
    which shows up as a bright band the user never asked for. pchip is
    monotone, so the curve only ever goes where the points go.
    """
    c = cfg.get("curves") or {}
    if not c.get("enabled"):
        return []
    parts = []
    for key, opt in (("master", "m"), ("r", "r"), ("g", "g"), ("b", "b")):
        pts = c.get(key)
        if _curve_is_identity(pts):
            continue
        pairs = " ".join(f"{float(x):.4f}/{float(y):.4f}" for x, y in pts)
        parts.append(f"{opt}='{pairs}'")
    if not parts:
        return []
    interp = c.get("interp", "pchip")
    return ["curves=" + ":".join(parts) + f":interp={interp}"]


# --- secondary (HSL qualifier) --------------------------------------------

LUT_SECOND = ROOT / "luts" / "secondary"


def _soft_window(x, low, high, soft):
    """1 inside [low, high], ramping to 0 across `soft` on each side.

    Softness grows the selection outward rather than eating into it, so
    widening the falloff never loses the pixels the user just keyed.
    """
    import numpy as np
    s = max(1e-6, float(soft))
    up = np.clip((x - (low - s)) / s, 0.0, 1.0)
    down = np.clip(((high + s) - x) / s, 0.0, 1.0)
    return np.minimum(up, down)


def secondary_lut(s: dict):
    """Bake the qualifier and its correction into a cached 33-cube."""
    import numpy as np
    sys.path.insert(0, str(ROOT / "tools"))
    import colorlib as C

    key = json.dumps(s, sort_keys=True)
    import hashlib
    h = hashlib.sha1(key.encode()).hexdigest()[:16]
    LUT_SECOND.mkdir(parents=True, exist_ok=True)
    path = LUT_SECOND / f"sec_{h}.cube"
    if path.exists():
        return path

    size = 33
    grid = C.identity_grid(size)
    hue, sat, val = C.rgb_to_hsv(grid)
    luma = C.luma709(grid)[..., 0]

    # Hue is circular, so distance has to wrap. Everything else is a plain
    # range on a bounded quantity.
    d = np.abs(((hue - float(s["hue_center"]) + 180.0) % 360.0) - 180.0)
    half = max(1e-6, float(s["hue_width"]) / 2.0)
    hs = max(1e-6, float(s["hue_soft"]))
    w_hue = np.clip(((half + hs) - d) / hs, 0.0, 1.0)
    w_sat = _soft_window(sat, float(s["sat_low"]), float(s["sat_high"]),
                         float(s["sat_soft"]))
    w_lum = _soft_window(luma, float(s["lum_low"]), float(s["lum_high"]),
                         float(s["lum_soft"]))
    mask = w_hue * w_sat * w_lum
    if s.get("invert"):
        mask = 1.0 - mask

    if s.get("show_mask"):
        out = np.repeat(mask[..., None], 3, axis=-1)
        C.write_cube(path, np.clip(out, 0.0, 1.0), size, f"secondary_mask_{h}",
                     comments=["Generated by cinegrade.secondary_lut",
                               "Qualifier matte, shown as greyscale."])
        return path

    m = mask
    hue2 = hue + float(s["hue_shift"]) * m
    sat2 = sat * (1.0 + (float(s["sat_gain"]) - 1.0) * m)
    val2 = val * (1.0 + (float(s["lum_gain"]) - 1.0) * m)
    corrected = C.hsv_to_rgb(hue2, np.clip(sat2, 0.0, 1.0), np.clip(val2, 0.0, None))
    corrected = corrected + np.asarray(s.get("tint", [0.0, 0.0, 0.0])) * m[..., None]
    strength = float(s.get("strength", 1.0))
    out = np.clip(grid + (corrected - grid) * strength, 0.0, 1.0)

    C.write_cube(path, out, size, f"secondary_{h}", comments=[
        "Generated by cinegrade.secondary_lut",
        "Domain: Rec.709 gamma 2.4. Applied after CURVES, before LOOK.",
    ])
    return path


def f_secondary(cfg) -> list[str]:
    s = cfg.get("secondary") or {}
    if not s.get("enabled"):
        return []
    return [f"lut3d=file={esc(secondary_lut(s))}:interp=tetrahedral"]


# --- power window (the shape half of a secondary) --------------------------
#
# One matte formula, written once here, evaluated three ways: in numpy
# (window_matte, the reference), in an ffmpeg geq expression (window_geq, which
# bakes the cached PNG the render actually uses) and later in a GPU shader.
# The three must agree, so the formula is spelled out in full in
# _window_geometry and both evaluators read the SAME already-rounded constants
# from it. Rounding the constants once rather than at each print is the whole
# trick: geq only ever sees a decimal string, so if numpy kept full precision
# and geq got 12 places, a pixel sitting exactly on a matte step could round
# the other way and the two references would disagree by a code value for no
# reason a reader could ever find.
#
# Coordinates are integer pixel indices with no half-pixel offset, matching
# what geq's X and Y actually are. A shader sampling at pixel centres has to
# subtract the half itself.

LUT_MASKS = ROOT / "luts" / "masks"

# How the 8-bit matte gets into the 16-bit merge. See graph_with_mask.
WINDOW_MASK_FORMAT = "format=gray16le,format=gbrp16le"


def _window_block(cfg) -> dict:
    """Accept either a whole config or the window block on its own."""
    if not isinstance(cfg, dict):
        return deepcopy(DEFAULTS["window"])
    if "window" in cfg:
        return deep_merge(DEFAULTS["window"], cfg["window"] or {})
    if "shape" in cfg or "softness" in cfg:
        return deep_merge(DEFAULTS["window"], cfg)
    return deepcopy(DEFAULTS["window"])


def _window_geometry(win: dict, width: int, height: int) -> dict:
    """The window's parameters resolved to pixels, rounded once.

    Fractions in, pixels out. Every consumer of the formula reads these
    numbers, and they are passed through a decimal round trip so the float a
    numpy expression sees is bit for bit the float ffmpeg parses out of the
    geq string.
    """
    def q(x, places):
        return float(f"{float(x):.{places}f}")

    r = math.radians(float(win.get("rotation", 0.0)))
    soft = max(0.0, float(win.get("softness", 0.0)))
    g = {
        "shape": "rect" if str(win.get("shape", "ellipse")) == "rect" else "ellipse",
        "invert": bool(win.get("invert")),
        "soft": soft,
        "cr": q(math.cos(r), 12),
        "sr": q(math.sin(r), 12),
        "cxp": q(float(win.get("cx", 0.5)) * width, 10),
        "cyp": q(float(win.get("cy", 0.5)) * height, 10),
        # A half axis is clamped to one pixel so a zero width window is a
        # one pixel line rather than a divide by zero.
        "ax": q(max(float(win.get("w", 0.6)) * width / 2.0, 1.0), 10),
        "ay": q(max(float(win.get("h", 0.6)) * height / 2.0, 1.0), 10),
    }
    # The feather edges, precomputed for the same reason: one rounding, shared.
    g["hi"] = q(1.0 + soft, 10)
    g["den"] = q(2.0 * soft, 10) if soft > 0 else 0.0
    return g


def window_matte(cfg, width: int, height: int):
    """The reference implementation of the window matte, as 8-bit gray.

    This is the definition. The geq expression below and any later GPU port
    are ports of it, and the suite asserts they land on the same bytes.
    """
    import numpy as np

    g = _window_geometry(_window_block(cfg), width, height)
    X = np.arange(width, dtype=np.float64)[None, :]
    Y = np.arange(height, dtype=np.float64)[:, None]
    dx = X - g["cxp"]
    dy = Y - g["cyp"]
    ux = dx * g["cr"] + dy * g["sr"]
    uy = dy * g["cr"] - dx * g["sr"]
    if g["shape"] == "rect":
        d = np.maximum(np.abs(ux) / g["ax"], np.abs(uy) / g["ay"])
    else:
        # np.hypot and C's hypot() are the same libm call, which is why the
        # geq side can use hypot() and still match to the last bit.
        d = np.hypot(ux / g["ax"], uy / g["ay"])
    if g["soft"] <= 0:
        m = (d <= 1.0).astype(np.float64)
    else:
        m = np.clip((g["hi"] - d) / g["den"], 0.0, 1.0)
    if g["invert"]:
        m = 1.0 - m
    # floor(x + 0.5), not numpy's rint: rint rounds halves to even, ffmpeg's
    # expression language has no such function, and "round" in the contract has
    # to mean one thing in both places or the two references disagree on every
    # pixel that lands exactly on a half.
    return np.floor(m * 255.0 + 0.5).astype(np.uint8)


def window_geq(cfg, width: int, height: int) -> str:
    """The same matte as an ffmpeg geq expression, on a gray frame.

    Written against the gray plane directly (see window_mask) rather than a
    yuv one: geq writing 0-255 into a limited-range luma plane and then
    converting to gray costs a tv-to-full expansion, which was measured at up
    to 20 code values of error against the reference before the format=gray
    was moved in front of the geq.
    """
    g = _window_geometry(_window_block(cfg), width, height)
    ux = (f"((X-{g['cxp']:.10f})*{g['cr']:.12f}"
          f"+(Y-{g['cyp']:.10f})*{g['sr']:.12f})")
    uy = (f"((Y-{g['cyp']:.10f})*{g['cr']:.12f}"
          f"-(X-{g['cxp']:.10f})*{g['sr']:.12f})")
    if g["shape"] == "rect":
        d = f"max(abs({ux})/{g['ax']:.10f},abs({uy})/{g['ay']:.10f})"
    else:
        d = f"hypot({ux}/{g['ax']:.10f},{uy}/{g['ay']:.10f})"
    if g["soft"] <= 0:
        m = f"lte({d},1)"
    else:
        m = f"clip(({g['hi']:.10f}-({d}))/{g['den']:.10f},0,1)"
    if g["invert"]:
        m = f"(1-({m}))"
    return f"floor(255*({m})+0.5)"


def window_mask(cfg, w: int, h: int):
    """A cached greyscale window matte, generated the way radial_mask is.

    Same technique as the radial-blur ramp: geq bakes a still once, ffmpeg
    reads it back as an ordinary input, and the per-frame cost is a decode of a
    small PNG instead of an expression evaluated per pixel per frame. The cache
    key carries the size because the matte is a picture, even though the
    parameters that made it are all fractions.
    """
    import hashlib

    win = _window_block(cfg)
    key = json.dumps(win, sort_keys=True)
    tag = hashlib.sha1(key.encode()).hexdigest()[:16]
    LUT_MASKS.mkdir(parents=True, exist_ok=True)
    p = LUT_MASKS / f"window_{w}x{h}_{tag}.png"
    if not p.exists():
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
             "-i", f"color=c=black:s={w}x{h}:d=1",
             "-vf", f"format=gray,geq=lum='{window_geq(win, w, h)}'",
             "-frames:v", "1", str(p)], check=True)
    return p


def window_active(cfg) -> bool:
    """True when the window stage has something to do.

    The window gates the SECONDARY and only the secondary, so with the
    qualifier off there is nothing for a shape to gate and the stage drops out
    entirely: no extra ffmpeg input, no extra filter, no change to the graph
    text. Three separate places have to agree on that answer (the input list,
    the graph builder, and the studio server's own input list), and they
    disagreeing would not raise: ffmpeg would just read the wrong input index
    and render a wrong picture silently. Hence one function.
    """
    win = (cfg or {}).get("window") or {}
    sec = (cfg or {}).get("secondary") or {}
    return bool(win.get("enabled")) and bool(sec.get("enabled"))


def mask_input_indices(cfg) -> dict:
    """Which ffmpeg input index each generated still lands on.

    Input 0 is the picture. Everything after it is appended in this order by
    ffmpeg_inputs, and build_graph and graph_with_mask read the indices back
    from here rather than counting again by hand.
    """
    idx = 1
    out = {}
    if cfg["fx"]["radial_blur"]["enabled"]:
        out["radial"] = idx
        idx += 1
    if window_active(cfg):
        out["window"] = idx
        idx += 1
    out["grain"] = idx
    return out


def esc(path) -> str:
    """ffmpeg filter args need : and \\ escaped inside the graph."""
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace(",", "\\,")


def highlight_pass(threshold: float, gain: float = 1.0) -> str:
    """Isolate everything above `threshold` and rescale it to full swing.

    Built as a `lut` (a real lookup table) rather than `geq`, so it costs one
    table build instead of an expression evaluated per pixel.
    """
    t = max(0.0, min(0.99, threshold))
    e = f"clip((val-{t:.4f}*maxval)*{gain / max(1e-3, 1.0 - t):.4f},0,maxval)"
    return f"lut=r='{e}':g='{e}':b='{e}'"


def glow_drive(strength: float) -> tuple[float, str]:
    """Split a glow `strength` into a blend opacity and a pre-blend amplitude.

    ffmpeg's blend `all_opacity` is hard limited to [0, 1] and refuses the
    render outright above it (measured: "Value 1.500000 for parameter
    'all_opacity' out of range [0 - 1]"). That capped halation and bloom long
    before they were visible, because a gaussian spreads a small bright area
    over a large radius and drops its peak amplitude in proportion: at the
    maximum opacity of 1.0 a 26 pixel halation still only moved the frame by a
    mean 3.7 code values out of 255.

    Screen blend with ffmpeg's opacity rule collapses to
    `out = base + opacity * glow * (1 - base)`, so opacity and a gain on the
    glow layer are the same lever. Below 1.0 this returns exactly what the
    graph used to emit, which is why every shipped preset (highest halation
    strength 0.7, highest bloom strength 0.4) renders byte for byte the same.
    Above 1.0 the opacity pins at 1.0 and the extra goes on the glow layer,
    where it clips at white the way a real blown highlight does.
    """
    s = max(0.0, float(strength))
    opacity = min(1.0, s)
    amp = max(1.0, s)
    if amp <= 1.0 + 1e-6:
        return opacity, ""
    e = f"clip(val*{amp:.4f},0,maxval)"
    return opacity, f",lut=r='{e}':g='{e}':b='{e}'"


def build_fx(cfg, info, base_label: str, idx: int) -> tuple[list[str], str, int]:
    """Return (graph segments, output label, next index) for the FX block."""
    fx = cfg["fx"]
    segs: list[str] = []
    cur = base_label
    W, H = info["width"], info["height"]

    def new(tag):
        nonlocal idx
        idx += 1
        return f"{tag}{idx}"

    # --- halation: highlights scatter into the red layer of the emulsion ---
    if fx["halation"]["enabled"]:
        h = fx["halation"]
        # Bloom-type effects are low frequency, so blur at quarter res and
        # scale back. Visually identical, roughly 16x less work.
        dw, dh = max(2, W // 4), max(2, H // 4)
        r, g, b = h["tint"]
        a, bl, o = new("hb"), new("hl"), new("ho")
        opacity, amp = glow_drive(h["strength"])
        segs.append(f"[{cur}]split=2[{a}][{bl}]")
        segs.append(
            f"[{bl}]{highlight_pass(h['threshold'])},"
            f"scale={dw}:{dh}:flags=bilinear,"
            f"gblur=sigma={max(0.3, h['sigma'] / 4):.3f},"
            f"scale={W}:{H}:flags=bicubic,"
            f"colorchannelmixer=rr={r:.3f}:gg={g:.3f}:bb={b:.3f}{amp}[{o}]"
        )
        out = new("hx")
        segs.append(
            f"[{a}][{o}]blend=all_mode=screen:all_opacity={opacity:.3f}[{out}]")
        cur = out

    # --- bloom: wider, neutral, lower amplitude than halation ---
    if fx["bloom"]["enabled"]:
        bcfg = fx["bloom"]
        dw, dh = max(2, W // 8), max(2, H // 8)
        r, g, b = bcfg["tint"]
        a, bl, o = new("bb"), new("bl"), new("bo")
        opacity, amp = glow_drive(bcfg["strength"])
        segs.append(f"[{cur}]split=2[{a}][{bl}]")
        segs.append(
            f"[{bl}]{highlight_pass(bcfg['threshold'])},"
            f"scale={dw}:{dh}:flags=bilinear,"
            f"gblur=sigma={max(0.3, bcfg['sigma'] / 8):.3f},"
            f"scale={W}:{H}:flags=bicubic,"
            f"colorchannelmixer=rr={r:.3f}:gg={g:.3f}:bb={b:.3f}{amp}[{o}]"
        )
        out = new("bx")
        segs.append(
            f"[{a}][{o}]blend=all_mode=screen:all_opacity={opacity:.3f}[{out}]")
        cur = out

    # --- radial blur: sharp centre falling off to soft edges ---
    if fx["radial_blur"]["enabled"]:
        rb = fx["radial_blur"]
        mask = radial_mask(W, H, rb["start"], rb["end"])
        sharp, soft = new("rs"), new("rf")
        segs.append(f"[{cur}]split=2[{sharp}][{soft}]")
        segs.append(f"[{soft}]gblur=sigma={rb['sigma']:.3f}[{soft}b]")
        out = new("rx")
        segs.append(
            f"[{sharp}][{soft}b][mask]maskedmerge[{out}]")
        cur = out

    # --- RGB split: lateral chromatic aberration ---
    if fx["rgb_split"]["enabled"]:
        amt = int(round(fx["rgb_split"]["amount"]))
        if amt:
            out = new("cx")
            segs.append(f"[{cur}]rgbashift=rh={amt}:bh={-amt}[{out}]")
            cur = out

    # --- vignette ---
    if fx["vignette"]["enabled"]:
        v = fx["vignette"]
        out = new("vx")
        # ffmpeg's vignette angle is the falloff; larger angle = stronger corners.
        #
        # radius used to be stored in every preset and never read, which the
        # audit confirmed: 0.2, 0.85 and 1.6 rendered byte identical. It is real
        # now, and it costs nothing, because ffmpeg's vignette factor is
        # cos(angle * d)^4 with d the distance to the corner normalised to 1.
        # Scaling d is therefore algebraically the same as scaling the angle, so
        # radius rides in on the angle rather than needing a second filter.
        # VIGNETTE_RADIUS_NEUTRAL is the shipped default, which makes the whole
        # expression collapse to the old one: every preset that already used a
        # vignette (all of them leave radius at 0.85) renders unchanged.
        angle = (0.2 + float(v["amount"]) * 1.1) * (
            VIGNETTE_RADIUS_NEUTRAL / max(0.05, float(v.get("radius", VIGNETTE_RADIUS_NEUTRAL))))
        # ffmpeg refuses an angle past pi/2, and pi/2 is already fully black
        # corners, so clamping here saturates the control instead of erroring.
        angle = min(angle, 1.5707)
        segs.append(f"[{cur}]vignette=angle={angle:.4f}:mode=forward[{out}]")
        cur = out

    return segs, cur, idx


def radial_mask(w, h, start, end):
    """A cached greyscale radial ramp used as the radial-blur matte."""
    d = ROOT / "luts" / "masks"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"radial_{w}x{h}_{start:.2f}_{end:.2f}.png"
    if not p.exists():
        expr = (f"255*clip((hypot((X-{w}/2)/({w}/2),(Y-{h}/2)/({h}/2))"
                f"-{start})/({max(1e-3, end - start)}),0,1)")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
             "-i", f"color=c=black:s={w}x{h}:d=1",
             "-vf", f"geq=lum='{expr}':cb=128:cr=128,format=gray",
             "-frames:v", "1", str(p)], check=True)
    return p


def grain_input(cfg, info):
    """A grey noise plate, generated at reduced size then scaled up.

    Blending a grey plate in overlay mode is how grain actually works: grey is
    neutral, so the plate modulates the image rather than washing it.

    The noise goes on component 0 only. The plate is yuv, so component 0 is
    luma and 1 and 2 are chroma; `alls` hits all three, which shakes the colour
    difference channels and lands as visible red and blue speckle rather than
    grain. Perturbing luma alone keeps R, G and B equal, so the grain is
    monochrome the way a film grain plate is.
    """
    g = cfg["grain"]
    size = max(1, int(g.get("size", 3)))
    w, h = max(2, info["width"] // size), max(2, info["height"] // size)
    # The lavfi source has to be bounded or `cinegrade render` never finishes:
    # blend's frame sync waits for every input to reach EOF, so an endless
    # grain plate means an endless output. Reproduced on a 12 frame clip, it
    # wrote 11 MB in 25 seconds and was still going. The margin is deliberate:
    # too SHORT a plate would silently freeze the grain on its last frame
    # instead of erroring, which is a worse failure than a long one. Clips with
    # no duration in their metadata fall back to an hour, and the `shortest`
    # flag on the blend in build_graph is the real guarantee either way.
    seconds = float(info.get("duration") or 0.0) + 10.0
    if seconds <= 10.0:
        seconds = 3600.0
    return ["-f", "lavfi", "-i",
            f"color=c=gray:s={w}x{h}:r=24:d={seconds:.3f},"
            f"noise=c0s={int(g['strength'])}:c0f=t+u"]


def f_letterbox(cfg, info) -> list[str]:
    """Crop to a scope aspect and pad the bars back in.

    Kept as the last geometric step so the FX above still see the full frame.
    """
    lb = cfg.get("letterbox", {})
    if not lb.get("enabled"):
        return []
    W, H = info["width"], info["height"]
    target = int(round(W / float(lb.get("aspect", 2.39))))
    if target >= H:
        return []
    target -= target % 2
    off = (H - target) // 2
    return [f"crop={W}:{target}:0:{off}", f"pad={W}:{H}:0:{off}:black"]


def build_graph(cfg, info, out_label="vout", tail_extra=None, encode_out=True,
                src_label="0:v", src_normalised=False):
    """Assemble the whole node tree into one filter_complex string.

        LOG -> CST IN -> PRIMARIES -> CST OUT -> CURVES -> SECONDARY
             -> LOOK -> FX -> GRAIN -> DETAIL -> LETTERBOX -> OUT

    The power window, when it is on, wraps the SECONDARY only: the tree splits
    just before it, runs the qualifier cube on one branch, and maskedmerges the
    two back together through the window matte.

    src_label exists so a caller can feed the tree something other than the
    raw first input. The studio server prepends its own downscale and points
    the tree at that, which is the difference between a two second preview and
    a half second one.

    src_normalised passes straight through to f_log_stage: set it when
    src_label already carries the scaled, range/matrix-normalised source (the
    studio server's cached source frame), so this graph only has to run the
    part that actually depends on the grade.
    """
    # Checked here rather than in each command, because this is the one door
    # every render, still, preview and scope goes through.
    check_source_space(cfg, info)
    head = (f_log_stage(cfg, info, normalised=src_normalised) + f_convert_in(cfg)
            + f_primaries(cfg) + f_convert_out(cfg)
            + f_curves(cfg))
    sec = f_secondary(cfg)
    if window_active(cfg):
        # The window stage. Split before the qualifier cube, grade one branch,
        # and let the matte decide per pixel which branch survives. maskedmerge
        # returns the FIRST input where the mask is 0 and the second where it
        # is maxval, so the ungraded branch has to be first.
        segs = [f"[{src_label}]{','.join(head) if head else 'null'}[secin]"]
        segs.append("[secin]split=2[wina][winb]")
        segs.append(f"[winb]{','.join(sec)}[winb2]")
        base = "wina"
        if (cfg.get("secondary") or {}).get("show_mask"):
            # In matte view the graded branch IS the qualifier matte, so
            # compositing it over the picture would show the picture wherever
            # the window is closed. Against black the same merge reads as
            # qualifier times window, which is what the matte view promises.
            segs.append("[wina]colorchannelmixer=rr=0:gg=0:bb=0[winbg]")
            base = "winbg"
        segs.append(f"[{base}][winb2][wmask]maskedmerge[cst]")
    else:
        segs = [f"[{src_label}]{','.join(head + sec)}[cst]"]
    cur = "cst"

    # LOOK, with a real opacity. A creative LUT at full strength is almost
    # always too strong; dialling it to 0.4-0.6 is the normal working state.
    look = f_look(cfg)
    if look:
        mix = float(cfg["look"].get("mix", 1.0))
        if mix >= 0.999:
            segs.append(f"[{cur}]{','.join(look)}[lk]")
        else:
            segs.append(f"[{cur}]split=2[lka][lkb]")
            segs.append(f"[lkb]{','.join(look)}[lkc]")
            # ffmpeg's normal blend is dst = in0*opacity + in1*(1-opacity),
            # so the graded branch has to be in0 for `mix` to mean look
            # strength. Verified empirically; the reverse silently makes
            # mix=0 identical to mix=1.
            segs.append(
                f"[lkc][lka]blend=all_mode=normal:all_opacity={mix:.4f}[lk]")
        cur = "lk"

    fx_segs, cur, _ = build_fx(cfg, info, cur, 0)
    needs_mask = cfg["fx"]["radial_blur"]["enabled"]
    segs += fx_segs

    # Retag as Rec.709 inside the graph. The -color_primaries output flags
    # alone do not stick: the encoder inherits BT.2020 frame properties from
    # the source, and the file ends up claiming a gamut it is not in, which
    # every color-managed player then over-saturates.
    if cfg["grain"]["enabled"]:
        idx_g = mask_input_indices(cfg)["grain"]
        segs.append(f"[{idx_g}:v]scale={info['width']}:{info['height']}"
                    f":flags=bilinear,format=gbrp16le,setsar=1[grainplate]")
        # shortest=1 is what actually guarantees the render terminates: it ends
        # the blend when the graded stream ends rather than when every input
        # does, so a grain plate that outlives the picture cannot run the
        # output on forever. The picture is input 0 here and the plate is always
        # generated longer, so this can never truncate a render.
        segs.append(f"[{cur}][grainplate]blend=all_mode=overlay:shortest=1"
                    f":all_opacity={float(cfg['grain'].get('opacity', 0.5)):.4f}[gx]")
        cur = "gx"

    tail = f_detail(cfg) + f_letterbox(cfg, info)
    if encode_out:
        tail += [
            "format=yuv422p10le",
            "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709",
        ]
    tail += list(tail_extra or [])
    segs.append(f"[{cur}]{','.join(tail) if tail else 'null'}[{out_label}]")
    return ";".join(segs), needs_mask


def ffmpeg_inputs(src, cfg, info, seek=None, duration=None):
    args = ["ffmpeg", "-v", "error", "-y"]
    # Must precede -i. Some clips carry a display matrix whose content is
    # already upright, in which case honouring the tag rotates it INTO being
    # sideways. Check with `cinegrade orient` before a long render.
    if not info.get("autorotate", True):
        args += ["-noautorotate"]
    if seek is not None:
        args += ["-ss", str(seek)]
    args += ["-i", src]
    if cfg["fx"]["radial_blur"]["enabled"]:
        rb = cfg["fx"]["radial_blur"]
        m = radial_mask(info["width"], info["height"], rb["start"], rb["end"])
        args += ["-i", str(m)]
    if window_active(cfg):
        args += ["-i", str(window_mask(cfg, info["width"], info["height"]))]
    if cfg["grain"]["enabled"]:
        args += grain_input(cfg, info)
    return args


def graph_with_mask(cfg, info, out_label="vout", tail_extra=None, encode_out=True,
                    src_label="0:v", head_extra=None, src_normalised=False):
    graph, needs_mask = build_graph(cfg, info, out_label, tail_extra, encode_out,
                                    src_label, src_normalised)
    idxs = mask_input_indices(cfg)
    if window_active(cfg):
        # The gray16le hop is not decoration. Going straight from the 8-bit
        # matte to gbrp16le, which is what the radial ramp does, expands by a
        # left shift of 8, so a matte code of 255 arrives at maskedmerge as
        # 65280 of 65535 and a fully open window applies only 99.61% of the
        # correction. Measured: a mean residual of 0.186 of 255 on 18.6% of
        # pixels against the un-windowed render, where it should be exactly
        # zero. Through gray16le the expansion is a multiply by 257 instead,
        # verified exact across all 256 codes, so 255 is the whole correction
        # and 0 is none of it. The matte still carries 256 levels: this is the
        # scaling, not the depth. ffmpeg's lut filter cannot do the same job
        # here, it clips its own output at 65280 on this build.
        graph = (f"[{idxs['window']}:v]{WINDOW_MASK_FORMAT},setsar=1[wmask];"
                 + graph)
    if needs_mask:
        graph = f"[{idxs['radial']}:v]format=gbrp16le,setsar=1[mask];" + graph
    if head_extra:
        graph = head_extra + ";" + graph
    return graph


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_render(a):
    cfg = apply_overrides(load_preset(a.preset), a)
    info = probe(a.input, autorotate=not getattr(a, 'no_autorotate', False))
    graph = graph_with_mask(cfg, info)
    o = cfg["output"]
    args = ffmpeg_inputs(a.input, cfg, info, a.start, a.duration)
    if a.duration:
        args += ["-t", str(a.duration)]
    args += ["-filter_complex", graph, "-map", "[vout]"]
    if not a.no_audio:
        args += ["-map", "0:a?", "-c:a", "aac", "-b:a", "256k"]
    if o["codec"] == "prores_ks":
        args += ["-c:v", "prores_ks", "-profile:v", str(o["profile"]),
                 "-vendor", "apl0", "-pix_fmt", "yuv422p10le"]
    else:
        args += ["-c:v", o["codec"], "-crf", str(o["crf"]),
                 "-preset", o["preset"], "-pix_fmt", "yuv420p"]
    args += ["-color_primaries", "bt709", "-color_trc", "bt709",
             "-colorspace", "bt709", a.output]
    run(args, a.verbose)
    print(f"rendered -> {a.output}")


def cmd_still(a):
    cfg = apply_overrides(load_preset(a.preset), a)
    info = probe(a.input, autorotate=not getattr(a, 'no_autorotate', False))
    extra = ([f"scale={a.width}:-2"] if a.width else []) + ["format=rgb24"]
    graph = graph_with_mask(cfg, info, tail_extra=extra, encode_out=False)
    args = ffmpeg_inputs(a.input, cfg, info, a.time)
    args += ["-filter_complex", graph, "-map", "[vout]", "-frames:v", "1", a.output]
    run(args, a.verbose)
    print(a.output)


def cmd_compare(a):
    """Render the same frame through several looks or presets, side by side.

    This is the agent feedback loop: one image showing every candidate, so a
    grading decision is a single look instead of N separate renders.
    """
    info = probe(a.input, autorotate=not getattr(a, 'no_autorotate', False))
    if a.presets:
        variants = [(v, {"preset": v}) for v in a.presets.split(",")]
    elif a.looks:
        variants = [(v, {"look": v}) for v in a.looks.split(",")]
    else:
        variants = [(v.stem, {"look": v.stem}) for v in sorted(LUT_LOOKS.glob("*.cube"))]

    tmp = ROOT / "stills" / "_compare"
    tmp.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, spec in variants:
        cfg = load_preset(spec.get("preset") or a.preset)
        if "look" in spec:
            cfg["look"]["lut"] = spec["look"]
        graph = graph_with_mask(cfg, info, encode_out=False,
                                tail_extra=[f"scale={a.width}:-2", "format=rgb24"])
        out = tmp / f"{name}.png"
        args = ffmpeg_inputs(a.input, cfg, info, a.time)
        args += ["-filter_complex", graph, "-map", "[vout]", "-frames:v", "1", str(out)]
        run(args, a.verbose)
        paths.append((name, out))
        print(f"  {name}")

    inputs = []
    for _, pth in paths:
        inputs += ["-i", str(pth)]
    n = len(paths)
    graph = "".join(f"[{i}:v]" for i in range(n)) + f"hstack=inputs={n}[o]"
    run(["ffmpeg", "-v", "error", "-y"] + inputs
        + ["-filter_complex", graph, "-map", "[o]", "-frames:v", "1", a.output], a.verbose)
    print(f"contact sheet ({n} up, left to right: "
          + ", ".join(nm for nm, _ in paths) + f") -> {a.output}")
    if a.open:
        subprocess.run(["open", "-a", "Preview", a.output])


def cmd_scopes(a):
    """Waveform + vectorscope + the frame, so a grade can be read numerically."""
    cfg = apply_overrides(load_preset(a.preset), a)
    info = probe(a.input, autorotate=not getattr(a, 'no_autorotate', False))
    graph = graph_with_mask(cfg, info, encode_out=False)
    W = a.width
    graph += (
        f";[vout]split=3[s1][s2][s3];"
        # the scope column is two W-by-W panels, so the frame must match that
        # height or hstack refuses to configure
        f"[s1]scale=-2:{2 * W},format=rgb24[img];"
        f"[s2]format=yuv422p10le,waveform=intensity=0.06:mode=column:"
        f"display=overlay:components=7:filter=lowpass,"
        f"scale={W}:{W},format=rgb24[wf];"
        f"[s3]format=yuv422p10le,vectorscope=mode=color3:graticule=green:"
        f"flags=name:envelope=instant,scale={W}:{W},format=rgb24[vs];"
        f"[wf][vs]vstack[scopes];[img][scopes]hstack[sheet]"
    )
    args = ffmpeg_inputs(a.input, cfg, info, a.time)
    args += ["-filter_complex", graph, "-map", "[sheet]", "-frames:v", "1", a.output]
    run(args, a.verbose)
    print(f"scopes -> {a.output}")
    if a.open:
        subprocess.run(["open", "-a", "Preview", a.output])


def cmd_stats(a):
    """Numeric readback: an agent can verify a grade without looking at it."""
    cfg = apply_overrides(load_preset(a.preset), a)
    info = probe(a.input, autorotate=not getattr(a, 'no_autorotate', False))
    graph = graph_with_mask(cfg, info, encode_out=False, tail_extra=[
        "format=yuv420p", "signalstats", "metadata=mode=print:file=-"])
    args = ffmpeg_inputs(a.input, cfg, info, a.time)
    args += ["-filter_complex", graph, "-map", "[vout]",
             "-frames:v", "1", "-f", "null", "-"]
    r = subprocess.run(args, capture_output=True, text=True)
    wanted = ("YMIN", "YLOW", "YAVG", "YHIGH", "YMAX", "UAVG", "VAVG", "SATAVG", "SATMAX")
    vals = {}
    for line in r.stdout.splitlines():
        if "lavfi.signalstats." in line:
            k, _, v = line.partition("=")
            vals[k.strip().split(".")[-1]] = v.strip()
    print(f"preset={a.preset or 'defaults'} look={cfg['look']['lut']} t={a.time}s")
    print("  8-bit code values (0-255):")
    for k in wanted:
        if k in vals:
            print(f"    {k:8} {vals[k]}")
    if "YAVG" in vals:
        y = float(vals["YAVG"])
        note = ("under" if y < 90 else "over" if y > 150 else "in range")
        print(f"  mid-tone check: YAVG {y:.1f} is {note} "
              f"(a normal Rec.709 frame averages roughly 95-140)")



def cmd_orient(a):
    """Render the same frame both ways so orientation is decided by looking."""
    outs = []
    for rotate in (True, False):
        info = probe(a.input, autorotate=rotate)
        args = ["ffmpeg", "-v", "error", "-y"]
        if not rotate:
            args += ["-noautorotate"]
        out = f"/tmp/orient_{'auto' if rotate else 'raw'}.png"
        args += ["-ss", str(a.time), "-i", a.input,
                 "-vf", f"scale=-2:{a.height},format=rgb24", "-frames:v", "1", out]
        run(args, a.verbose)
        outs.append(out)
        print(f"  {'autorotate (default)' if rotate else '--no-autorotate':22} "
              f"{info['width']}x{info['height']}")
    run(["ffmpeg", "-v", "error", "-y", "-i", outs[0], "-i", outs[1],
         "-filter_complex", "[0:v][1:v]hstack,format=rgb24",
         "-frames:v", "1", a.output], a.verbose)
    print(f"left = autorotate, right = --no-autorotate  -> {a.output}")
    rot = probe(a.input)["rotation"]
    if rot:
        print(f"note: this clip carries a {rot} degree display matrix. If the "
              f"right frame is the upright one, pass --no-autorotate.")
    if a.open:
        subprocess.run(["open", "-a", "Preview", a.output])


def apply_overrides(cfg, a):
    if getattr(a, "look", None):
        cfg["look"]["lut"] = a.look
    if getattr(a, "exposure", None) is not None:
        cfg["convert"]["exposure"] = a.exposure
    if getattr(a, "tonemap", None):
        cfg["convert"]["tonemap"] = a.tonemap
    if getattr(a, "working_space", None):
        cfg["convert"]["working_space"] = a.working_space
    for k in ("contrast", "saturation", "temperature", "tint"):
        v = getattr(a, k, None)
        if v is not None:
            cfg["primaries"][k] = v
    return cfg


def run(args, verbose=False):
    if verbose:
        print(" ".join(shlex.quote(x) for x in args), file=sys.stderr)
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise GradeError(f"ffmpeg failed ({r.returncode})\n{r.stderr[-4000:]}")
    return r


# The studio server's default port. Only used to talk to an already running
# one, so there is nothing to bind or configure here.
STUDIO_PORT = 7431


def cmd_session(a):
    """Read or write the live config of a running studio server.

    This exists so a second person, or an agent, can adjust the grade someone
    already has open in a browser: before this the live config lived only in
    that tab's memory and nothing outside it could see or change it.

    Deliberately plain HTTP to 127.0.0.1 and nothing else. It talks to a server
    that is already running and is already bound to the loopback interface, so
    it adds no listener, no port and no remote surface of its own.
    """
    import urllib.error                                     # noqa: PLC0415
    import urllib.request                                   # noqa: PLC0415

    url = f"http://127.0.0.1:{a.port}/api/session"
    body = None
    if a.action == "patch":
        raw = a.json
        if raw is None:
            raise GradeError("session patch needs a JSON object, or - for stdin")
        if raw == "-":
            raw = sys.stdin.read()
        try:
            patch = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GradeError(f"that is not valid JSON: {exc}") from exc
        if not isinstance(patch, dict):
            raise GradeError("session patch expects a JSON object")
        # A bare {"primaries": ...} is the natural thing to type, so accept it
        # as shorthand for a config patch rather than making every caller wrap
        # it in {"config": ...}.
        if not set(patch) & {"config", "clip", "time"}:
            patch = {"config": patch}
        patch.setdefault("by", "cli")
        patch["replace"] = bool(a.replace)
        body = json.dumps(patch).encode()

    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:400]
        raise GradeError(f"studio refused it: {detail}") from exc
    except urllib.error.URLError as exc:
        raise GradeError(
            f"no studio server answering on 127.0.0.1:{a.port} ({exc.reason}). "
            f"Start one with ./studio.sh, or pass --port.") from exc

    if out.get("config") is None:
        print("the studio is running but no browser has published a config yet; "
              "open the page once, then try again", file=sys.stderr)
    print(json.dumps(out, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("input")
        p.add_argument("--preset", "-p")
        p.add_argument("--look", "-l")
        p.add_argument("--exposure", "-e", type=float)
        p.add_argument("--tonemap", choices=["aces", "filmic", "none"])
        p.add_argument("--working-space", choices=["dwg", "direct", "rec709"],
                       help="rec709 for footage that is already display "
                            "referred (an mp4, a Rec.709 export coming back "
                            "for FX only); dwg and direct both expect Apple Log")
        p.add_argument("--contrast", type=float)
        p.add_argument("--saturation", type=float)
        p.add_argument("--temperature", type=float)
        p.add_argument("--tint", type=float)
        p.add_argument("--no-autorotate", action="store_true",
                       help="ignore the display matrix (see: cinegrade orient)")
        p.add_argument("--verbose", "-v", action="store_true")

    r = sub.add_parser("render"); common(r)
    r.add_argument("--output", "-o", required=True)
    r.add_argument("--start", type=float)
    r.add_argument("--duration", "-t", type=float)
    r.add_argument("--no-audio", action="store_true")
    r.set_defaults(fn=cmd_render)

    s = sub.add_parser("still"); common(s)
    s.add_argument("--output", "-o", required=True)
    s.add_argument("--time", type=float, default=0.0)
    s.add_argument("--width", type=int)
    s.set_defaults(fn=cmd_still)

    c = sub.add_parser("compare"); common(c)
    c.add_argument("--output", "-o", default=str(ROOT / "stills" / "compare.png"))
    c.add_argument("--time", type=float, default=0.0)
    c.add_argument("--width", type=int, default=560)
    c.add_argument("--looks", help="comma separated look names")
    c.add_argument("--presets", help="comma separated preset names")
    c.add_argument("--open", action="store_true", help="open in Preview.app")
    c.set_defaults(fn=cmd_compare)

    sc = sub.add_parser("scopes"); common(sc)
    sc.add_argument("--output", "-o", default=str(ROOT / "stills" / "scopes.png"))
    sc.add_argument("--time", type=float, default=0.0)
    sc.add_argument("--width", type=int, default=700)
    sc.add_argument("--open", action="store_true")
    sc.set_defaults(fn=cmd_scopes)

    o = sub.add_parser("orient"); common(o)
    o.add_argument("--output", "-o", default=str(ROOT / "stills" / "orient.png"))
    o.add_argument("--time", type=float, default=0.0)
    o.add_argument("--height", type=int, default=600)
    o.add_argument("--open", action="store_true")
    o.set_defaults(fn=cmd_orient)

    st = sub.add_parser("stats"); common(st)
    st.add_argument("--time", type=float, default=0.0)
    st.set_defaults(fn=cmd_stats)

    ss = sub.add_parser(
        "session",
        help="read or change the config of a running studio server, so an "
             "outside agent can drive the grade someone else has open")
    ss.add_argument("action", choices=["get", "patch"])
    ss.add_argument("json", nargs="?",
                    help="for patch: a partial config object, deep merged into "
                         "the live one. '-' reads it from stdin.")
    ss.add_argument("--port", type=int, default=STUDIO_PORT)
    ss.add_argument("--replace", action="store_true",
                    help="overwrite the live config instead of merging into it")
    ss.set_defaults(fn=cmd_session)

    a = ap.parse_args()
    try:
        a.fn(a)
    except GradeError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
