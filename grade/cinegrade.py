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
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import warnings
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# The run folder: the repo checkout itself. studio/server.py has its own
# CONTENT constant (STUDIO.parent) for the identical directory; the two
# never disagree because ROOT is always grade/ inside that same checkout.
CONTENT = ROOT.parent
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
        # What the SOURCE is, which is a fact about the file rather than a
        # choice about the grade. "auto" reads it off the file's own transfer
        # and primaries tags (see resolve_input); the explicit values are
        # there for a file whose tags are missing or lying, and for the five
        # camera logs, which no container tag can describe and which auto
        # therefore never picks. Every one of them decodes to the same scene
        # linear BT.2020 point, so working_space, tonemap and encode below
        # mean exactly what they always meant.
        "input": "auto",
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
    # Two slots blended in parallel, not stacked: A = lerp(base, lut1(base),
    # mix), B = lerp(base, lut2(base), mix2), out = lerp(A, B, balance).
    # lut2 null or balance 0 reproduces exactly today's single-slot output.
    "look": {"lut": None, "mix": 1.0, "lut2": None, "mix2": 1.0, "balance": 0.0},
    # mid_detail is local contrast at a wide gaussian radius (sigma about 2% of
    # the frame width): out = in + mid_detail * MID_DETAIL_K * (in - blur(in)).
    # 0.0 leaves the picture untouched (today's bytes). See f_mid_detail_segment.
    "detail": {"soften": 0.0, "sharpen": 0.0, "mid_detail": 0.0},
    # prep runs on the source before any colour transform (right after decode,
    # before the CST): see f_denoise. Off by default, matching today's bytes.
    # The GPU preview has no hqdn3d, so an enabled denoise always falls back to
    # the ffmpeg-rendered still, the same way grain falls back today.
    "prep": {"denoise": {"enabled": False, "spatial": 0.0, "temporal": 0.0}},
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
    #
    # stock/softness/response/color/seed all default to the value that
    # reproduces today's bytes exactly (see grain_input and build_graph's
    # grain block): stock "custom" leaves strength/size/softness alone,
    # softness 0 skips the blur node, response "flat" skips the luminance
    # weighting node, color 0 skips the independent-channel plate and mixer,
    # seed 0 omits ffmpeg's noise seed option entirely (its own default).
    "grain": {"enabled": False, "strength": 40, "size": 3, "opacity": 0.5,
              "stock": "custom", "softness": 0.0, "response": "flat",
              "color": 0.0, "seed": 0},
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
    # The hue curves (Hue vs Hue, Hue vs Sat, Hue vs Lum, Lum vs Sat, Sat vs
    # Sat). Point lists like curves.master, but the neutral is a FLAT line, not
    # the diagonal: these are corrections, so an empty list is the identity and
    # y is an offset or a multiplier rather than an output level. The hue axes
    # are periodic, so a point near 0 and a point near 1 are neighbours.
    # Units and the full colour model live in grade/slice.py.
    "hue_curves": {
        "enabled": False,
        "hue_hue": [], "hue_sat": [], "hue_lum": [],
        "lum_sat": [], "sat_sat": [],
    },
    # Color Slice (six chromatic vectors plus skin, each with a density) and
    # Tetra (six cube corner moves with black and white pinned). Baked into the
    # same cube as the hue curves above, for the same reason the secondary is
    # baked: all of it is a pure function of one pixel's RGB.
    "slice": {
        "enabled": False,
        "density": 0.0,
        "vectors": {
            "red": {"hue": 0.0, "sat": 1.0, "density": 0.0},
            "yellow": {"hue": 0.0, "sat": 1.0, "density": 0.0},
            "green": {"hue": 0.0, "sat": 1.0, "density": 0.0},
            "cyan": {"hue": 0.0, "sat": 1.0, "density": 0.0},
            "blue": {"hue": 0.0, "sat": 1.0, "density": 0.0},
            "magenta": {"hue": 0.0, "sat": 1.0, "density": 0.0},
            "skin": {"hue": 0.0, "sat": 1.0, "density": 0.0},
        },
        "tetra": {
            "enabled": False,
            "r": [0.0, 0.0, 0.0], "g": [0.0, 0.0, 0.0], "b": [0.0, 0.0, 0.0],
            "c": [0.0, 0.0, 0.0], "m": [0.0, 0.0, 0.0], "y": [0.0, 0.0, 0.0],
        },
    },
    # Masked correction layers, any number of them, applied in array order.
    #
    # This replaces the single `secondary` (an HSL colour key) plus `window`
    # (one power window shape) pair the engine used to carry. A layer IS that
    # pair generalised: a mask (a window, a colour key, either, both or
    # neither) and a correction that is merged back under the mask. One layer
    # whose key and window are the old blocks reproduces the old picture byte
    # for byte, and there can now be as many as the grade needs.
    #
    # Empty by default, which is what makes every existing preset render
    # exactly what it always rendered: an empty list adds no filter, no ffmpeg
    # input and no change to the graph text. A config written before layers
    # existed is migrated on read by migrate_layers(). LAYER_DEFAULTS below is
    # the shape of one layer and the merge base every layer is filled in from.
    "layers": [],
    "output": {"codec": "prores_ks", "profile": 3, "crf": 16, "preset": "slow"},
    # "auto" (today's behaviour, honour the file's display matrix), or "0",
    # "90", "180", "270" to ignore the tag and turn the picture that many
    # degrees clockwise regardless of what it carries. This is a config key,
    # not a request-only field, on purpose (contract G4): rotation is part of
    # a saved grade, so a preset or a grade written with a non-auto value
    # keeps it, and loading that preset or grade back onto its own clip
    # renders it the way its author saw it rather than falling back to
    # whatever the project or the file tag says. config_diff (studio/server.py)
    # compares every key against DEFAULTS, so this one is no exception: "auto"
    # here is stripped from a saved preset or grade exactly like every other
    # untouched default, and a real value survives the round trip for free.
    "rotation": "auto",
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
    # migrate_layers runs on the file's own contents, BEFORE the merge with
    # DEFAULTS, because DEFAULTS always supplies an empty `layers` and the
    # migration's rule is "old keys and no layers key". Not because any
    # shipped preset carries a secondary or a window (none do), but because a
    # preset on disk is exactly the kind of thing someone saved before layers
    # existed. The file itself is never rewritten.
    return deep_merge(DEFAULTS, migrate_layers(json.loads(p.read_text())))


# --------------------------------------------------------------------------
# rotation
# --------------------------------------------------------------------------
#
# Rotation is a setting, not a guess. "auto" is ffmpeg's own behaviour (it
# honours the display matrix the file carries), "0" ignores that tag, and 90,
# 180 and 270 ignore it and then turn the picture that many degrees CLOCKWISE
# as it is seen on screen. That last group exists because a camera can write a
# tag that does not match the picture: this footage carries a -90 display
# matrix on shots that are already upright, so "auto" delivers them sideways
# and "0" delivers the ones that really are tagged correctly sideways instead.
#
# 180 is two clockwise quarter turns rather than hflip,vflip. Both are exact
# reorderings of the same samples (no resampling either way), and doing it as
# 90 twice means this file has one definition of "clockwise" instead of two
# that could drift apart.

ROTATIONS = ("auto", "0", "90", "180", "270")

_ROTATE_FILTERS = {
    "auto": [],
    "0": [],
    "90": ["transpose=1"],                     # 1 = 90 degrees clockwise
    "180": ["transpose=1", "transpose=1"],
    "270": ["transpose=2"],                    # 2 = 90 degrees anticlockwise
}


def normalise_rotation(value=None, default: str = "auto") -> str:
    """One of ROTATIONS out of whatever a caller happens to have.

    Accepts the new rotation strings, the old autorotate boolean (True is
    "auto", False is "0"), and the "1"/"0" query form the browser has always
    sent, so a request written before rotation existed still means exactly
    what it used to mean.
    """
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return "auto" if value else "0"
    s = str(value).strip().lower()
    if s in ROTATIONS:
        return s
    if s in ("true", "yes", "on", "1", "tag"):
        return "auto"
    if s in ("false", "no", "off", "none"):
        return "0"
    try:
        n = int(round(float(s))) % 360
    except ValueError:
        raise GradeError(f"rotation must be one of {', '.join(ROTATIONS)}, "
                         f"got {value!r}") from None
    if n % 90:
        raise GradeError(f"rotation must be a quarter turn "
                         f"({', '.join(ROTATIONS)}), got {value!r}")
    return str(n)


def rotate_filters(rotation) -> list[str]:
    """The ffmpeg filter steps this rotation needs, empty for auto and 0."""
    return list(_ROTATE_FILTERS[normalise_rotation(rotation)])


def rotation_of(info) -> str:
    """The rotation an info dict (probe's shape) was built for.

    Falls back to the old autorotate boolean so an info dict assembled by
    hand, by a tool or by an older caller keeps its meaning.
    """
    info = info or {}
    chosen = info.get("rotate")
    if chosen:
        return normalise_rotation(chosen)
    return "auto" if info.get("autorotate", True) else "0"


def rotate_prefix(info) -> str:
    """The transpose steps for a -vf chain, comma terminated, "" for none.

    For the callers that decode with a plain -vf rather than a filter_complex
    (the studio's source frame, its proxy, the orient sheet). The rotation has
    to come FIRST in those chains: every scale target downstream is computed
    from probe's post-rotation dimensions.
    """
    steps = rotate_filters(rotation_of(info))
    return (",".join(steps) + ",") if steps else ""


def rotate_args(rotation) -> list[str]:
    """The pre-input ffmpeg args. Anything but auto ignores the display tag."""
    return [] if normalise_rotation(rotation) == "auto" else ["-noautorotate"]


# --------------------------------------------------------------------------
# probe
# --------------------------------------------------------------------------

def probe(path: str, autorotate: bool = True, rotation=None) -> dict:
    """Stream facts, with width and height AFTER the chosen rotation.

    rotation wins when it is given; autorotate is the old two-value form of
    the same argument and stays for every caller that has not moved yet.
    """
    mode = normalise_rotation(rotation if rotation is not None
                              else bool(autorotate))
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
    if mode == "auto":
        if abs(rot) in (90, 270):
            w, h = h, w    # ffmpeg auto-rotates, so the graph sees these
    elif mode in ("90", "270"):
        # A fixed quarter turn runs on the untagged stream (-noautorotate),
        # so it is the file's own width and height that swap.
        w, h = h, w
    return {
        "width": w, "height": h, "rotation": rot, "rotate": mode,
        "autorotate": mode == "auto",
        "pix_fmt": st.get("pix_fmt"), "color_range": st.get("color_range", "tv"),
        "color_space": st.get("color_space", "bt2020nc"),
        # The transfer and the primaries, separately, because they answer two
        # different questions and a file can get one right and the other wrong.
        # color_space above is the YUV MATRIX, which is a third thing again and
        # is all this engine used to have. Absent rather than defaulted: an
        # untagged file has to stay distinguishable from a tagged one, since
        # "no transfer tag on a BT.2020 file" is exactly what an Apple Log
        # clip looks like (see resolve_input).
        "color_transfer": st.get("color_transfer", ""),
        "color_primaries": st.get("color_primaries", ""),
        "nb_frames": st.get("nb_frames"), "duration": st.get("duration"),
        "codec": st.get("codec_name"), "profile": st.get("profile"),
    }


# A nonzero display matrix tag on one of these codecs is worth a second look
# (see rotation_tag_suspect below): none of them are what a phone writes.
# ProRes, DNxHD/HR and CineForm are edit-friendly mezzanine codecs a camera
# on a rig or gimbal writes; the raw formats are sensor dumps straight off a
# cinema camera body. A phone shooting native video writes h264 or hevc.
_CINEMA_CODECS = ("prores", "dnxhd", "dnxhr", "cineform", "r3d", "braw",
                  "arriraw", "cinemadng")


def rotation_tag_suspect(tag, codec, coded_width, coded_height) -> bool:
    """Advisory only: does this clip's display-matrix rotation look wrong.

    Nothing reads this to change what gets rendered; it only exists so an
    agent (or `cinegrade orient IN --json`, or the studio's clip list) can
    flag a file worth a human look instead of trusting every tag blindly.
    False positives are fine here (a shrug that says "maybe check this
    one"); a silent false negative on a file that really is sideways is the
    failure this exists to avoid, so the two tells below are deliberately
    generous rather than narrow.

    A tag of 0 or 180 is never suspect: it does not change which side is up
    versus down, only up versus... still up, so there is nothing here for a
    quarter-turn tell to catch. Given a nonzero quarter turn, either tell
    below is enough on its own to flag it:

    1. The file is one of _CINEMA_CODECS. Phones write a rotation tag
       because they physically rotate in the hand; a camera that masters to
       one of these codecs ships flat and gets corrected in an edit, not by
       a display-matrix atom, so a tag here is very often stale or
       hand-entered metadata rather than the picture's own orientation.
       This is exactly A001_09061637_C011.mov: a ProRes file with a -90 tag
       on a shot that is already landscape.
    2. The tag is a quarter turn (90 or 270, either sign) and the CODED
       frame (`coded_width`/`coded_height`: the decoder's own width and
       height, from probing at rotation="0", before any tag is applied) is
       already taller than it is wide. A phone's sensor is fixed landscape,
       so a phone only ever writes a quarter-turn tag on a landscape-coded
       frame; a quarter turn on a frame that is already portrait-coded is
       not a shape a phone produces, so it reads as a copied-over or
       hand-set tag instead.
    """
    try:
        deg = int(tag or 0)
    except (TypeError, ValueError):
        return False
    if deg % 180 == 0:
        return False
    if any(name in str(codec or "").lower() for name in _CINEMA_CODECS):
        return True
    try:
        cw, ch = int(coded_width), int(coded_height)
    except (TypeError, ValueError):
        return False
    return deg % 360 in (90, 270) and ch > cw


def rotation_tag_note(tag, codec, coded_width, coded_height) -> str:
    """One plain sentence explaining a rotation_tag_suspect answer.

    Round 2 tooling item 17: the boolean alone reads as a verdict ("this tag
    is wrong"), which it is not: rotation_tag_suspect's own docstring says a
    false positive is fine and a silent false negative is the failure it is
    built to avoid. This exists so a caller sees why the flag fired, in
    words, next to it. "" for the not-suspect case on purpose, not a
    reassurance ("no issue") that would itself read as a verdict.

    Delegates the yes/no to rotation_tag_suspect() so the two never
    disagree; only decides which of that function's two tells to name in
    the sentence, using the same cinema-codec check.
    """
    if not rotation_tag_suspect(tag, codec, coded_width, coded_height):
        return ""
    deg = int(tag or 0)
    codec_name = str(codec or "unknown")
    if any(name in codec_name.lower() for name in _CINEMA_CODECS):
        return (f"the file carries a {deg} tag on a {codec_name} stream; "
               f"the tag is often wrong on this kind of file, run orient "
               f"and look")
    return (f"the file carries a {deg} tag but the frame is already "
           f"taller than wide before any rotation is applied; a phone "
           f"would not write a quarter turn on that shape, run orient and "
           f"look")


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


# --------------------------------------------------------------------------
# convert.input: what the source actually is
# --------------------------------------------------------------------------
#
# This engine decoded every source as Apple Log, because that is what it was
# built for. An HLG phone clip pushed down that path lands about three stops
# dark with crushed blacks, and nothing says so. The input stage exists to make
# the decode a fact read off the file instead of an assumption.
#
# The rule is one line long: the transfer tag picks the curve, the primaries
# tag picks the matrix, and they are answered separately because a file can
# carry BT.2020 primaries with a Rec.709 transfer or the other way round. The
# YUV matrix (color_space, used by source_matrix above) is a third tag again
# and is not part of this decision.

# The five camera logs, kept as their own tuple because several rules below
# are about "is this a manufacturer's log curve" and not about one name.
#
# None of them is ever the answer to "auto". A container has no tag that tells
# S-Log3 from LogC3 from V-Log: all five come off the camera labelled BT.2020
# primaries with either no transfer tag or a bt2020 one, which is the same
# thing an Apple Log file looks like. Guessing between them would be guessing
# between five different pictures, so they are explicit values only and auto
# keeps answering apple_log for an untagged BT.2020 file exactly as it did.
CAMERA_LOG_INPUTS = ("slog3", "logc3", "vlog", "clog3", "dlog")

INPUTS = ("auto", "apple_log", "hlg", "pq", "rec709") + CAMERA_LOG_INPUTS

# ffprobe's spelling of each transfer characteristic, and the input it means.
# Both HLG spellings appear in the wild: ffprobe prints "arib-std-b67", and
# some tools write "arib_std_b67".
TRANSFER_INPUTS = {
    "arib-std-b67": "hlg",
    "arib_std_b67": "hlg",
    "smpte2084": "pq",
    "bt709": "rec709",
    "bt2020-10": "rec709",   # the BT.2020 OETF is the BT.709 curve at 10 bit
    "bt2020-12": "rec709",
    "bt2020_10bit": "rec709",
    "bt2020_12bit": "rec709",
}

# A transfer tag that says nothing. An Apple Log file carries none of these
# tags at all, which is why an untagged BT.2020 file resolves to apple_log.
UNKNOWN_TRANSFERS = {"", "unknown", "unspecified", "reserved", "n/a"}

# The two primaries sets this engine can build a cube for.
PRIMARIES_TAGS = {
    "bt709": "bt709", "bt470bg": "bt709", "smpte170m": "bt709",
    "smpte240m": "bt709", "unknown": "", "unspecified": "", "reserved": "",
    "bt2020": "bt2020",
}

# Where each input's own standard puts its primaries, used when the file does
# not say. Mirrors colorlib.INPUT_NATIVE_PRIMARIES; stated again here so the
# CLI does not have to import numpy and colour-science to answer it.
NATIVE_PRIMARIES = {"apple_log": "bt2020", "hlg": "bt2020", "pq": "bt2020",
                    "rec709": "bt709", "slog3": "sgamut3cine",
                    "logc3": "awg3", "vlog": "vgamut",
                    "clog3": "cinemagamut", "dlog": "dgamut"}


def source_primaries(info, resolved: str = "apple_log") -> str:
    """The primaries this source is on.

    "bt709" or "bt2020" for the four inputs a container can describe, and the
    camera's own gamut for the five it cannot.

    An untagged or unrecognised file falls back to the resolved input's own
    native primaries, which for apple_log is bt2020, exactly what this engine
    has always assumed.

    A camera log's gamut is NOT read off the file, and that is on purpose.
    There is no code point in any container for S-Gamut3.Cine, ARRI Wide Gamut
    3, V-Gamut, Cinema Gamut or D-Gamut, so those files carry "bt2020" (the
    nearest available label) or nothing at all. Reading that tag would matrix
    a V-Log file as though it were BT.2020 and quietly desaturate it. The
    curve and its gamut are one decision the camera made together, so naming
    the curve names the gamut.
    """
    native = NATIVE_PRIMARIES.get(resolved, "bt2020")
    if native not in ("bt709", "bt2020"):
        return native
    tag = str((info or {}).get("color_primaries") or "").strip().lower()
    return PRIMARIES_TAGS.get(tag, "") or native


def resolve_input(cfg, info=None) -> tuple[str, list[str]]:
    """(input name, warnings) for this config and this file.

    An explicit convert.input wins and is never second-guessed: the caller can
    see the picture and the metadata cannot. "auto" reads the file:

      transfer arib-std-b67                     -> hlg
      transfer smpte2084                        -> pq
      transfer bt709 with bt709 primaries       -> rec709
      no usable transfer tag                    -> apple_log
      anything else                             -> apple_log, with a warning

    "auto" never answers with a camera log. S-Log3, LogC3, V-Log, Canon Log 3
    and D-Log all arrive tagged the same way an Apple Log file is (BT.2020
    primaries, no transfer tag the standards define), so the container cannot
    tell them apart and a guess would be a guess between five different
    pictures. Those five are set by hand or not at all.

    The fourth line is what keeps every existing grade byte identical: an
    Apple Log clip carries BT.2020 primaries and no transfer tag at all, so
    "auto" gives it exactly the decode it has always had, and so does any
    hand-built info dict that has no tags in it either.

    The last line is deliberately a warning and not a refusal. A file with an
    unexpected pair of tags still renders the way it always did, and the
    warning names the tag so the user can set convert.input themselves.
    """
    cfg = cfg or {}
    chosen = str((cfg.get("convert") or {}).get("input") or "auto").lower()
    if chosen not in INPUTS:
        raise GradeError(f"convert.input must be one of {', '.join(INPUTS)}, "
                         f"got {chosen!r}")
    if chosen != "auto":
        return chosen, []

    info = info or {}
    transfer = str(info.get("color_transfer") or "").strip().lower()
    primaries = str(info.get("color_primaries") or "").strip().lower()
    if transfer in UNKNOWN_TRANSFERS:
        return "apple_log", []
    guess = TRANSFER_INPUTS.get(transfer)
    if guess in ("hlg", "pq"):
        return guess, []
    if guess == "rec709" and PRIMARIES_TAGS.get(primaries) == "bt709":
        return "rec709", []
    return "apple_log", [
        f"transfer tag {transfer!r} with primaries {primaries or 'unset'!r} is "
        f"not one this engine recognises on its own, so the source is being "
        f"decoded as Apple Log (what it has always done). Set convert.input to "
        f"one of {', '.join(INPUTS[1:])} if that is wrong."]


def input_of(cfg, info=None) -> str:
    """resolve_input's answer without the warnings, for the graph builders."""
    return resolve_input(cfg, info)[0]


def technical_lut_name(source: str, primaries: str, stage: str,
                       tonemap: str = "aces", encode: str = "") -> str:
    """The file name of one technical cube.

    One function so the generator (grade/tools/make_cst.py) writes the exact
    name the engine later looks for. The Apple Log names are the ones this
    repository already ships and must not move:

        AppleLog_to_DWG.cube
        AppleLog_to_Rec709_aces_rec709a.cube

    A source that is not on its input's native primaries gets an infix naming
    the primaries it really is on, so Rec.709 on BT.2020 primaries is
    Rec709_2020_to_DWG.cube and there is no way to load the wrong matrix.

    A camera log never takes that infix: its gamut comes with its curve (see
    source_primaries), so there is one cube set per camera log and it is named
    SLog3_to_DWG.cube, LogC3_to_DWG.cube and so on.
    """
    label = {"apple_log": "AppleLog", "hlg": "HLG", "pq": "PQ",
             "rec709": "Rec709", "slog3": "SLog3", "logc3": "LogC3",
             "vlog": "VLog", "clog3": "CLog3", "dlog": "DLog"}[source]
    if primaries and primaries != NATIVE_PRIMARIES[source]:
        label += {"bt709": "_709", "bt2020": "_2020"}[primaries]
    if stage == "dwg":
        return f"{label}_to_DWG.cube"
    tail = f"_{encode}" if encode else ""
    return f"{label}_to_Rec709_{tonemap}{tail}.cube"


# --------------------------------------------------------------------------
# exposure, per input
# --------------------------------------------------------------------------
#
# A stop has to be a doubling of scene light whatever the source is, and the
# only place this graph can spend one is in the source's own encoded domain,
# before the CST cube. So each input gets the expression that IS a doubling
# for its own curve, rather than one constant borrowed from another curve.
#
# APPLE_LOG_STOP is right for Apple Log alone. Using it on an HLG or PQ file
# would still brighten the picture, which is exactly why it would go unnoticed:
# the number of stops printed on the control would simply not be the number of
# stops applied.
#
# The constants below repeat colorlib's, on purpose. colorlib imports numpy and
# colour-science, which this file must not: the CLI has to start instantly and
# the studio server builds graph text on request. The suite asserts the two
# copies agree (cases_input.constants_match_colorlib).

# ITU-R BT.2100 Table 5, HLG.
HLG_A = 0.17883277
HLG_B = 0.28466892
HLG_C = 0.55991073
HLG_SYSTEM_GAMMA = 1.2

# SMPTE ST 2084 / BT.2100 Table 4, PQ.
PQ_M1 = 0.1593017578125
PQ_M2 = 78.84375
PQ_C1 = 0.8359375
PQ_C2 = 18.8515625
PQ_C3 = 18.6875

# The five camera logs, as the six constants and two cuts of the one shape all
# five share (colorlib.CameraLog says the same thing in full, with each
# document quoted next to its numbers):
#
#     y = C * log10(A * x + B) + D      for x >= cut_x
#     y = E * x + F                     for x <  cut_x
#
# x is scene linear reflectance, y is that curve's own code value. Repeated
# here for the same reason the HLG and PQ constants above are: this file must
# not import numpy or colour-science. The suite asserts the two copies agree
# (cases_input.the_engine_and_colorlib_agree_on_every_constant).
CAMERA_LOG_CONSTANTS = {
    # Sony S-Log3 technical summary. 18% grey is 10 bit code 420 (0.410557).
    "slog3": {"A": 1.0 / 0.19, "B": 0.01 / 0.19, "C": 261.5 / 1023.0,
              "D": 420.0 / 1023.0,
              "E": (171.2102946929 - 95.0) / 0.01125 / 1023.0,
              "F": 95.0 / 1023.0, "cut_x": 0.01125,
              "cut_y": 171.2102946929 / 1023.0},
    # ARRI Log C curve usage document, EI 800. 18% grey is code 400 (0.391007).
    "logc3": {"A": 5.555556, "B": 0.052272, "C": 0.247190, "D": 0.385537,
              "E": 5.367655, "F": 0.092809, "cut_x": 0.010591,
              "cut_y": 5.367655 * 0.010591 + 0.092809},
    # Panasonic V-Log/V-Gamut reference manual. 18% grey is 42.3 IRE.
    "vlog": {"A": 1.0, "B": 0.00873, "C": 0.241514, "D": 0.598206,
             "E": 5.6, "F": 0.125, "cut_x": 0.01, "cut_y": 0.181},
    # Canon Log gamma curves white paper, Canon Log 3. 18% grey is 32.8 IRE.
    # The document's 14.98325 and 2.3069815 are stated against reflectance
    # divided by 0.9, so both are divided by 0.9 here and the cut multiplied
    # by it, which is the same curve written against reflectance directly.
    "clog3": {"A": 14.98325 / 0.9, "B": 1.0, "C": 0.42889912,
              "D": 0.069886632, "E": 2.3069815 / 0.9, "F": 0.073059361,
              "cut_x": 0.014 * 0.9, "cut_y": 0.105357102},
    # DJI D-Log white paper. 18% grey is 0.398765.
    "dlog": {"A": 0.9892, "B": 0.0108, "C": 0.256663, "D": 0.584555,
             "E": 6.025, "F": 0.0929, "cut_x": 0.0078, "cut_y": 0.14},
}

# ffmpeg's expression language has log() (natural) and exp() but no log10 and
# no base-10 power, so the two conversions are written once here rather than
# spelled out five times in the string below.
LN10 = 2.302585092994046
LOG10E = 0.4342944819032518


def _camera_log_exposure_expr(source: str, stops: float) -> str:
    """One lut expression that moves a camera log by `stops` stops of light.

    Decode the code value to scene linear with the vendor's own inverse curve,
    multiply the light by 2 ** stops, encode it again with the vendor's own
    forward curve. There is no shortcut for these five the way a constant
    offset is one for Apple Log: every one of them has a linear toe and an
    offset inside the logarithm, so a code space offset would be a different
    number of stops at every code, biggest exactly where the shadows are.

    ld(0) is the incoming signal, ld(2) the scaled scene light. Both halves
    are the published curve, in the shape CAMERA_LOG_CONSTANTS documents.
    """
    k = CAMERA_LOG_CONSTANTS[source]
    a, b, c, d = k["A"], k["B"], k["C"], k["D"]
    e, f = k["E"], k["F"]
    gain = 2.0 ** stops
    # 10 ** z is exp(z * ln 10); the max() floors the log's argument for the
    # same reason the HLG expression above floors its own.
    scene = (f"if(lt(st(0,val/maxval),{k['cut_y']!r}),"
             f"(ld(0)-{f!r})/{e!r},"
             f"(exp(((ld(0)-{d!r})/{c!r})*{LN10})-{b!r})/{a!r})")
    return (f"clip(if(lt(st(2,max({gain:.9f}*{scene},0)),{k['cut_x']!r}),"
            f"{e!r}*ld(2)+{f!r},"
            f"{c!r}*log(max({a!r}*ld(2)+{b!r},1e-9))*{LOG10E}+{d!r})*maxval,"
            f"0,maxval)")


def exposure_expr(source: str, stops: float) -> str:
    """One lut expression that moves `source` by `stops` stops of scene light.

    Apple Log's is a constant code offset, because an offset on a log curve is
    a linear gain and that is the string this engine has always emitted.

    HLG scales the pre-OOTF scene light by 2 ** (stops / 1.2) rather than by
    2 ** stops. That is not a fudge: the BT.2100 OOTF multiplies each channel
    by its own luminance to the power (gamma - 1), so scaling all three scene
    channels by k scales the OOTF's output by exactly k ** gamma, for every
    colour and not just for neutrals. Taking the 1.2th root first is what makes
    the display light, and therefore the scene linear the cube produces, come
    out doubled per stop.

    PQ is absolute, so a stop is a plain doubling of cd/m2 there.

    Rec.709 is a display code, so a stop is the code multiply 2 ** (stops /
    2.4): the same expression the working_space "rec709" branch below writes,
    written once here and used by both.

    The five camera logs decode, scale the light and re-encode with their own
    published curve, which _camera_log_exposure_expr writes from one shape and
    one table of constants.

    st()/ld() are ffmpeg's own expression registers. They are used rather than
    inlining each sub-expression four times because `lut` evaluates this once
    per code value per plane (65536 * 3 on this 16 bit buffer), and because a
    formula written once is a formula that can be checked against the standard
    it cites.
    """
    if source in CAMERA_LOG_CONSTANTS:
        return _camera_log_exposure_expr(source, stops)
    if source == "apple_log":
        return f"clip(val+{stops * APPLE_LOG_STOP:.6f}*maxval,0,maxval)"
    if source == "rec709":
        return f"clip(val*{2.0 ** (stops / DISPLAY_GAMMA):.6f},0,maxval)"
    if source == "hlg":
        k = 2.0 ** (stops / HLG_SYSTEM_GAMMA)
        # ld(0) is the signal, ld(2) the scaled scene light. The two branches
        # are BT.2100's own piecewise OETF and its inverse.
        scene = (f"if(lte(st(0,val/maxval),0.5),ld(0)*ld(0)/3,"
                 f"(exp((ld(0)-{HLG_C})/{HLG_A})+{HLG_B})/12)")
        return (f"clip(if(lte(st(2,{k:.9f}*{scene}),0.083333333),"
                f"sqrt(3*ld(2)),"
                f"{HLG_A}*log(max(12*ld(2)-{HLG_B},1e-9))+{HLG_C})*maxval,"
                f"0,maxval)")
    if source == "pq":
        k = 2.0 ** stops
        # ld(0) is the signal to the power 1/m2, ld(3) the re-encoded scaled
        # luminance to the power m1. Both halves are ST 2084 verbatim.
        nits = (f"pow(max(st(0,pow(val/maxval,{1.0 / PQ_M2!r}))-{PQ_C1},0)"
                f"/({PQ_C2}-{PQ_C3}*ld(0)),{1.0 / PQ_M1!r})")
        return (f"clip(pow(({PQ_C1}+{PQ_C2}*st(3,pow(st(2,min({k:.9f}*{nits},1))"
                f",{PQ_M1})))/(1+{PQ_C3}*ld(3)),{PQ_M2})*maxval,0,maxval)")
    raise GradeError(f"no exposure formula for input {source!r}")


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
            #
            # Decided by the WORKING SPACE and not by convert.input, because
            # this branch is about there being no CST at either end: whatever
            # the file is, nothing undoes its curve, so the code the graph
            # holds is a delivery code and a stop is a code multiply.
            src = "rec709"
        else:
            # Anywhere else a CST does run, so the stop is spent in the
            # source's own encoded domain and has to be that curve's stop.
            # apple_log writes the exact string it always wrote, which is what
            # keeps every existing render byte identical.
            src = input_of(cfg, info)

        def ex(stops):
            return exposure_expr(src, stops)
        chain.append(f"lut=r='{ex(off_r)}':g='{ex(off_g)}':b='{ex(off_b)}'")
    return chain


# prep.denoise maps its 0..1 spatial/temporal sliders onto hqdn3d's own
# luma/chroma spatial/temporal strengths. hqdn3d's own defaults (when given no
# arguments at all) are luma_spatial=4.0, chroma_spatial=3.0, luma_tmp=6.0,
# chroma_tmp=4.5, so 0.5 landing near ffmpeg's own default spatial strength is
# the anchor these numbers were picked against. The buffer at this point in the
# graph is gbrp16le (three RGB planes, not YUV), so there is no real luma vs
# chroma split to honour: every plane gets the SAME strength rather than
# guessing which colour channel stands in for "luma".
DENOISE_SPATIAL_MAX = 8.0
DENOISE_TEMPORAL_MAX = 8.0


def f_denoise(cfg) -> list[str]:
    """prep.denoise: hqdn3d, right after the source is normalised to RGB and
    exposure/white balance is applied, and before the CST (f_convert_in).

    Placed after f_log_stage rather than literally before it so the same
    filter lands at the same point in the graph whether this call is the CLI's
    single-pass render (f_log_stage's non-normalised branch, decoding straight
    off the file) or the studio server's cached-source preview
    (f_log_stage's normalised branch, which is already gbrp16le by the time it
    reaches this graph): both branches finish f_log_stage in the same gbrp16le,
    exposure-applied state, so denoise behaves identically in the final render
    and in the server's fallback still. Exposure is a constant per-channel
    additive shift, which does not meaningfully interact with a spatial or
    temporal filter, so running denoise just after it rather than strictly
    before it does not change what "denoise before the CST" means in practice.

    Temporal denoise (luma_tmp/chroma_tmp) needs a real previous frame to
    compare against, and a still is the very first frame hqdn3d has ever seen,
    so it is NEAR enough a no-op: measured on footage/A001_09011336_C002.MOV at
    1920 wide, temporal 0 vs temporal 1 with spatial 0 differs by a max of
    1065 of 65535 (4.15 of 255), mean 243 of 65535 (0.95 of 255), on one still.
    That residual is ffmpeg's own hqdn3d, isolated with no other filter in the
    chain: max 165 of 65535, mean 12.8 of 65535, so this engine's own graph
    roughly doubles it rather than introducing it. It is real but small next
    to what temporal denoise does across actual frames: on a 12-frame bounded
    render at the same width, frame 6 (temporal 0 vs 1) differs by a mean of
    648 of 65535 (2.52 of 255), max 6285 of 65535 (24.45 of 255), 2.7x the mean
    and 5.9x the max of the single-still residual. See limits.js.
    """
    dn = (cfg.get("prep") or {}).get("denoise", {})
    if not dn.get("enabled"):
        return []
    spatial = max(0.0, min(1.0, float(dn.get("spatial", 0.0))))
    temporal = max(0.0, min(1.0, float(dn.get("temporal", 0.0))))
    if spatial <= 1e-6 and temporal <= 1e-6:
        return []
    sp = spatial * DENOISE_SPATIAL_MAX
    tp = temporal * DENOISE_TEMPORAL_MAX
    return [f"hqdn3d={sp:.3f}:{sp:.3f}:{tp:.3f}:{tp:.3f}"]


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

# On the `direct` path there is no CST IN, so primaries operate on the SOURCE's
# own code values and the pivot has to be that source's own 18% grey. The Apple
# Log entry is MID_GREY_CODE["direct"] above, unchanged. The others are the
# same 0.18 scene linear pushed back through each input's own encode:
#   hlg     BT.2100 OETF of the scene light that the OOTF puts at 26 cd/m2
#   pq      ST 2084 inverse EOTF of 26 cd/m2 (BT.2408 Reference Level)
#   rec709  the BT.709 OETF of 0.18
# Every one is derived, printed by grade/tools/make_cst.py --anchors, and
# asserted against colorlib in cases_input.
#   camera logs  each vendor's own forward curve at 0.18, which is the number
#                its documentation quotes: S-Log3 code 420 of 1023, LogC3 code
#                400 of 1023, V-Log 42.3 IRE, Canon Log 3 32.8 IRE.
MID_GREY_CODE_INPUT = {"apple_log": 0.4883, "hlg": 0.3786, "pq": 0.3800,
                       "rec709": 0.4090, "slog3": 0.4106, "logc3": 0.3910,
                       "vlog": 0.4233, "clog3": 0.3280, "dlog": 0.3988}


def mid_grey_code(cfg, info=None) -> float:
    """The code PRIMARIES sees for an 18% grey card, in this configuration.

    Only the `direct` working space depends on the input: `dwg` has already
    converted every source into DaVinci Wide Gamut by this point, so its grey
    is one number for all of them, and `rec709` never converts anything.
    """
    ws = cfg["convert"]["working_space"]
    if ws != "direct":
        return MID_GREY_CODE[ws]
    return MID_GREY_CODE_INPUT[input_of(cfg, info)]


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


def f_convert_in(cfg, info=None) -> list[str]:
    """CST IN: the source's own curve into the DWG working space.

    info is optional so a caller that has no probe (bake_lut, a hand-built
    config) keeps the Apple Log answer it has always had.
    """
    if cfg["convert"]["working_space"] != "dwg":
        return []
    src = input_of(cfg, info)
    return [_tech_lut(technical_lut_name(src, source_primaries(info, src), "dwg"))]


# Apple Log clips come off the camera tagged bt2020nc; an ordinary delivery file
# is tagged bt709. That one field is the YUV matrix, which is a different tag
# from the transfer and the primaries resolve_input reads.
LOG_MATRICES = {"bt2020nc", "bt2020c", "bt2020_ncl", "bt2020_cl"}
DISPLAY_MATRICES = {"bt709", "smpte170m", "bt470bg", "smpte240m", "fcc"}


def check_source_space(cfg, info) -> tuple[str, list[str]]:
    """Resolve the input, and refuse only what genuinely cannot be rendered.

    Returns (resolved input, warnings) so a caller that wants to show the user
    what happened can, and so the studio server has one place to ask.

    Both refusals it used to make were decided by the YUV MATRIX tag alone,
    which is the wrong question asked of the wrong field: the matrix says
    nothing about the transfer, and "bt2020" covers Apple Log, HLG and PQ,
    which need three different decodes. Each refusal now also asks what the
    source RESOLVED to, so:

    1. working_space "rec709" on a bt2020 file is refused only when the file
       really does resolve to camera log. An HLG or PQ delivery file is
       BT.2020 and is not camera log, so it is allowed with a warning, which
       is the case that used to be refused for no good reason.
    2. A bt709 tagged file on a log working space is refused only when it
       still resolves to "apple_log", which now means its transfer tag was
       missing or unrecognised. A file that carries a real bt709 transfer
       resolves to input "rec709", gets decoded correctly, and needs no
       refusal at all. The danger the old rule protected against is real and
       measured (median luma 0.332 to 0.238, saturation 0.33 to 0.59); what
       changed is that there is now a right answer to offer instead.

    3. A camera log (slog3, logc3, vlog, clog3, dlog) on working_space
       "rec709" is refused for exactly the same reason apple_log is: the
       caller has said in so many words that the source IS a manufacturer's
       log curve, and "rec709" is the one working space that undoes no curve
       at all, so the picture would come out flat and grey with nothing on
       screen to explain it. This refusal cannot change any existing grade:
       none of those five inputs existed before.

    Everything else is a warning. A warning describes a picture that will
    render; a refusal is for a picture that would be wrong with nothing on
    screen to say so.
    """
    ws = cfg["convert"]["working_space"]
    src, warnings = resolve_input(cfg, info)
    matrix = (info or {}).get("color_space") or ""
    if ws == "rec709" and src in CAMERA_LOG_INPUTS:
        raise GradeError(
            f"convert.input is '{src}', which is a camera log curve, but "
            f"working_space is 'rec709' (for footage that is already display "
            f"referred). Nothing on that path undoes a log curve, so the "
            f"picture would stay flat and desaturated with nothing on screen "
            f"to say why. Use working_space 'dwg' or 'direct'.")
    if ws == "rec709" and src == "apple_log" and matrix in LOG_MATRICES:
        raise GradeError(
            f"source is tagged {matrix} with no transfer this engine knows, so "
            f"it resolves to camera log, but working_space is 'rec709' (for "
            f"footage that is already display referred). The log curve would "
            f"never be undone and the picture would stay flat and desaturated. "
            f"Use working_space 'dwg' or 'direct'.")
    if ws != "rec709" and src == "apple_log" and matrix in DISPLAY_MATRICES:
        raise GradeError(
            f"source is tagged {matrix}, which is already display referred "
            f"Rec.709, but it resolves to input 'apple_log' and working_space "
            f"is '{ws}'. That applies a log to display transform to a picture "
            f"that never had a log curve and silently wrecks it (measured: "
            f"median luma 0.332 to 0.238, saturation 0.33 to 0.59). Set "
            f"convert.input to 'rec709', or working_space to 'rec709' to grade "
            f"it in place.")
    if ws == "rec709" and src in ("hlg", "pq"):
        # Not fatal, and no longer a refusal: an HLG or PQ file is BT.2020 but
        # it is NOT camera log, and grading it in place is a legitimate ask (it
        # is how an FX only round trip works). Worth saying out loud that the
        # curve is not being undone, which is all this warning does.
        warnings.append(
            f"working_space is 'rec709' (no conversion at either end) but the "
            f"source resolves to '{src}', whose transfer this grade will not "
            f"undo. Use working_space 'dwg' or 'direct' to convert it.")
    return src, warnings


def f_convert_out(cfg, info=None) -> list[str]:
    """CST OUT. info is optional for the same reason f_convert_in's is: only
    the `direct` path reads it, and only to pick which input's cube to load."""
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
    src = input_of(cfg, info)
    prim = source_primaries(info, src)
    per_encode = LUT_TECH / technical_lut_name(src, prim, "direct",
                                               c["tonemap"], c["encode"])
    if per_encode.exists():
        return [_tech_lut(per_encode.name)]
    return [_tech_lut(technical_lut_name(src, prim, "direct", c["tonemap"]))]


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


def f_primaries(cfg, info=None) -> list[str]:
    """Runs inside the working space, before the output transform.

    Temperature and tint are deliberately absent: they are handled in the log
    stage, where an offset is exactly a linear gain.

    info is optional and is only read for the default pivot on the `direct`
    working space, where what primaries sees is the source's own code values
    and mid grey therefore depends on which input the source is.
    """
    p = cfg["primaries"]
    pivot = p["pivot"] if p.get("pivot") is not None else mid_grey_code(cfg, info)
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


# mid_detail: local contrast at a wide gaussian radius. sigma is a FRACTION of
# the frame width (not a fixed pixel count like the FX sigmas scale_for_preview
# corrects), so computing it from info["width"] at build time already agrees
# between a full render and a smaller preview without any extra scaling step:
# a preview genuinely renders at fewer actual pixels (see server.py
# scale_for_preview's docstring and render_raw's pinfo), so 2% of THAT width is
# already the proportionally-correct radius.
MID_DETAIL_SIGMA_FRAC = 0.02
MID_DETAIL_SIGMA_MIN = 1.0     # pixels; keeps a tiny preview from asking gblur for sigma 0
# Chosen so mid_detail=1.0 is a strong, still-usable local contrast push. A
# single hard-edge pixel (a bright window against a dark interior, in the real
# test footage) already sits near the full 0..65535 swing in (in - blur), so
# ANY k > 0 drives that one outlier close to full clip; the number that
# actually scales with k, and the one that matters for "usable", is the mean
# absolute change over the whole frame. Swept 0.3 to 2.0 on
# footage/A001_09011336_C002.MOV at 1920 wide, mid_detail=1.0: mean |delta|
# rose roughly linearly from 490 of 65535 (1.9 of 255) at k=0.3 to 4490 of
# 65535 (17.5 of 255) at k=2.0. k=2.0 moves the WHOLE frame by an average of
# 17.5 of 255, which reads as a different picture, not a stronger one; k=1.0
# (mean |delta| 1942 of 65535, 7.6 of 255) is the strong end of "usable".
# That sweep ran before the clamp fix below, so it was measuring wrapped
# samples as well as real ones. Re-measured on the same clip and width with
# the clamp in place (t=1.0s, 8-bit render): mid_detail 1.0 moves the frame by
# a mean of 6.7 of 255 and -1.0 by 6.8, where the wrapping build reported 8.8
# for 1.0 against the same 6.8 for -1.0. So the wrap was inflating the
# positive half by about a third and the shape of the sweep, not the choice of
# k=1.0, is what it justified.
MID_DETAIL_K = 1.0


def f_mid_detail_segment(mid: float, info: dict, cur: str, segs: list[str]) -> str:
    """Append the split/blur/blend sub-graph for mid_detail, return the new
    running label.

    out = in + mid*K*(in - blur(in)). Written as a `blend` all_expr as
    `A*(1+coeff) - B*coeff` (A the sharp branch, B the blurred branch) rather
    than an expression that names `in` and `blur(in)` separately, because that
    form needs no absolute maxval constant inside the arithmetic: `blend`'s
    all_expr language has no such constant (unlike `lut` and `geq`, confirmed
    against `ffmpeg -h filter=blend`).

    The whole expression is wrapped in clip(..., 0, 65535) because ffmpeg's
    blend does NOT clip an out-of-range all_expr result: it casts the float to
    the plane's integer type, so the value wraps modulo 65536 at 16 bit.
    Measured on this build (ffmpeg 8.1.1): a 16-bit ramp blended against a
    constant with all_expr 'A*3-B*2' returns 65492 where the arithmetic says
    -44, and 64733 where it says 130269. An earlier version of this docstring
    read that wrap as a clip, because doubling white (65535*2 = 131070) wraps
    to 65534, which is indistinguishable from a clip to white. On the parity
    clip at 640 wide with mid_detail 1.0, 24140 of 2184960 channel samples
    (1.10 percent) leave the range and every one of them wrapped: pixel
    (70, 216) green computes -242 and came back as 65294 (white), while the
    GPU port clamped it to 0. That was the whole of the mid_detail parity
    failure, and it is 1.10 percent of samples, the exact pctOver1 the harness
    reported for that row.

    The literal 65535 is used rather than a symbolic maximum because every
    filter this segment can follow is 16 bit: the working format is gbrp16le
    (f_log_stage), and the only filter between it and here that changes format
    is unsharp, which is YUV only and negotiates yuv444p16le out of gbrp16le
    (checked with `ffmpeg -v 48`). clip() is part of blend's expression
    evaluator (av_expr), verified on this build.
    """
    sigma = max(MID_DETAIL_SIGMA_FRAC * float(info["width"]), MID_DETAIL_SIGMA_MIN)
    coeff = mid * MID_DETAIL_K
    a, b, blur, out = "mda", "mdb", "mdblur", "mdout"
    segs.append(f"[{cur}]split=2[{a}][{b}]")
    segs.append(f"[{b}]gblur=sigma={sigma:.3f}[{blur}]")
    segs.append(f"[{a}][{blur}]blend=all_expr="
                f"'clip(A*{1.0 + coeff:.4f}-B*{coeff:.4f},0,65535)'[{out}]")
    return out


def f_look(cfg, slot="lut") -> list[str]:
    """Build the lut3d filter for one look slot ("lut" or "lut2").

    Three ways `look.lut` resolves, tried in order (round 2 tooling note
    10): an absolute path, or one relative to the CURRENT DIRECTORY, if
    that already exists (unchanged, and how a browser-launched look never
    behaves, since the studio server never calls this with a relative
    value); failing that, a path relative to CONTENT (the run folder, the
    repo checkout), so a caller does not have to cd into content/ or spell
    an absolute path just to point at a cube that lives somewhere else in
    the checkout; failing that, a bare name (with or without .cube) inside
    LUT_LOOKS, the browser's own looks folder and dropdown
    (`studio/server.py`'s `list_looks()` only ever sends bare stems from
    there, so that lookup, and the browser, are unaffected by the new
    CONTENT fallback above it).
    """
    lut = cfg["look"].get(slot)
    if not lut:
        return []
    p = Path(lut)
    if not p.exists():
        content_relative = CONTENT / lut
        if content_relative.is_file():
            p = content_relative
    if not p.exists():
        p = LUT_LOOKS / (lut if lut.endswith(".cube") else f"{lut}.cube")
    if not p.exists():
        avail = sorted(x.stem for x in LUT_LOOKS.glob("*.cube"))
        raise GradeError(
            f"look LUT not found: {lut!r}. Give a bare name from "
            f"{LUT_LOOKS} ({', '.join(avail)}), or a path (absolute, or "
            f"relative to {CONTENT})")
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


# --- hue curves, Color Slice and Tetra ------------------------------------
#
# One stage, one cube. The five hue curves, the seven slice vectors, the global
# density and the six Tetra corners are all pure functions of a single pixel's
# RGB, so they collapse into a single 33-cube exactly the way the secondary
# does, and cost one lut3d between them instead of five expression filters.
#
# The maths, the colour model and the units are in grade/slice.py, which is the
# reference implementation. Nothing here does colour work; it only turns the
# cached bake into a filter string.

def f_slice(cfg) -> list[str]:
    """The hue curves + Color Slice + Tetra stage, or nothing at all.

    Absent from the graph whenever every control is at its default, so a
    config that carries the two new blocks renders byte identical to one that
    has never heard of them. That is the compatibility rule for this arc and
    it is enforced here rather than trusted to a zeroed LUT.
    """
    sys.path.insert(0, str(ROOT))
    import slice as slice_stage
    if slice_stage.is_identity(cfg):
        return []
    path = slice_stage.slice_lut({"hue_curves": cfg.get("hue_curves") or {},
                                  "slice": cfg.get("slice") or {}})
    return [f"lut3d=file={esc(path)}:interp=tetrahedral"]


# --- layers (a mask plus a correction, any number of them) -----------------
#
# One layer is what used to be the `secondary` block (an HSL colour key and a
# correction, baked to a .cube) plus the `window` block (one shape, baked to a
# grey PNG and applied with maskedmerge). Both halves are unchanged in what
# they compute; what changed is that there can be any number of them, each
# with its own mask, and that each one can sit before or after the look.
#
# The colour half is still baked into a 33-cube rather than run as a per pixel
# expression, for the reason it always was: the key and the correction are
# both pure functions of one pixel's RGB, so a table lookup with tetrahedral
# interpolation is exact enough and costs far less than a geq per pixel per
# frame. The window half stays a spatial matte, because position is the one
# thing a colour cube cannot know.

LUT_LAYERS = ROOT / "luts" / "layers"

# The template every layer is filled in from. A layer in a config may name
# only the fields it changes; config_layers() deep merges each one over this,
# so nothing downstream ever sees a missing key.
#
# The correction runs on the DISPLAY REFERRED Rec.709 signal (after CST OUT
# and the curves node), which is the same signal the old secondary keyed on.
# That is why exposure, temperature and tint here are the code multiply form
# of f_log_stage's rec709 branch rather than the log offset form: there is no
# log curve left at this point in the tree to add an offset to.
LAYER_DEFAULTS = {
    "enabled": True,
    "name": "Layer 1",
    # "before_look" runs between the curves node and the look LUT, which is
    # where the secondary always ran. "after_look" runs between the look and
    # the FX block, so a correction can be made on the graded picture.
    "placement": "before_look",
    "mask": {
        # Render the matte instead of the picture. The engine composites the
        # colour matte over black through the window matte, so what comes out
        # is the product of the two: the selection the layer will really make.
        "show": False,
        # Inverts the COMBINED matte (window times key), not either half.
        "invert": False,
        # Every geometric value is a FRACTION of the frame, never a pixel
        # count. That is what lets a 960 wide preview and a 3840 wide render
        # agree without scale_for_preview needing a case for any of it: the
        # same numbers describe the same shape at any size. cx/cy are the
        # centre, w/h the FULL extent (not the half axis), rotation is degrees
        # clockwise on screen, softness is the feather width as a fraction of
        # the shape's own radius.
        "window": {
            "enabled": False, "shape": "ellipse",
            "cx": 0.5, "cy": 0.5, "w": 0.6, "h": 0.6,
            "rotation": 0.0, "softness": 0.15, "invert": False,
        },
        # The HSL qualifier. A colour key only, never a shape.
        "key": {
            "enabled": False, "invert": False,
            "hue_center": 30.0, "hue_width": 40.0, "hue_soft": 15.0,
            "sat_low": 0.10, "sat_high": 1.0, "sat_soft": 0.10,
            "lum_low": 0.0, "lum_high": 1.0, "lum_soft": 0.10,
        },
    },
    "correct": {
        # Stops, the same unit convert.exposure uses. See LAYER_DEFAULTS'
        # docstring above for why the arithmetic is a multiply here.
        "exposure": 0.0,
        # pivot None means mid grey in the layer's own domain, LAYER_PIVOT.
        "contrast": 1.0, "pivot": None,
        "saturation": 1.0,
        "temperature": 0.0, "tint": 0.0,
        # The old secondary's HSV controls, unchanged.
        "hue_shift": 0.0, "sat_gain": 1.0, "lum_gain": 1.0,
        # The old secondary's "tint" rgb array, renamed so it cannot be
        # confused with the scalar white balance `tint` above it.
        "offset": [0.0, 0.0, 0.0],
        # Gaussian sigma in pixels AT 1920 WIDE, resolved to the frame's own
        # width in layer_blur_sigma(). See that function for why it is a
        # fraction of the width rather than a raw pixel count.
        "blur": 0.0,
        "strength": 1.0,
    },
}

# Mid grey in the layer's domain, which is display referred Rec.709, not the
# working space primaries runs in. MID_GREY_CODE["rec709"] is that number
# (18% scene linear through the rec709a encoder). It is deliberately NOT
# MID_GREY_CODE[working_space]: primaries pivots on the mid grey of the signal
# IT sees, and a layer does the same for the signal it sees. On the dwg path
# those are 0.3360 and 0.4587 respectively, so the same contrast number in the
# two nodes pivots on two different codes, which is correct rather than a bug.
LAYER_PIVOT = MID_GREY_CODE["rec709"]

# The width the `blur` sigma is quoted at. See layer_blur_sigma.
LAYER_BLUR_REF_WIDTH = 1920.0


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


def config_layers(cfg) -> list[dict]:
    """Every layer in a config, each filled in from LAYER_DEFAULTS."""
    return [deep_merge(LAYER_DEFAULTS, layer or {})
            for layer in ((cfg or {}).get("layers") or [])]


def layer_active(layer: dict) -> bool:
    """True when the layer has anything to contribute to the graph.

    A disabled layer is absent, and so is a layer whose combined matte is zero
    everywhere. That second case is exactly mask.invert on a layer with no
    mask components: the matte of a layer with no mask is 1, inverting it
    gives 0, and a correction merged under a matte of 0 is the picture. It
    drops out rather than costing a cube lookup that cannot change a pixel.
    """
    if not layer.get("enabled"):
        return False
    mask = layer["mask"]
    if (mask.get("invert") and not mask["window"].get("enabled")
            and not mask["key"].get("enabled")):
        return False
    return True


def layer_window(layer: dict):
    """The window matte this layer needs, with mask.invert folded in, or None.

    None means the layer needs no spatial matte at all, which is what keeps a
    key only or a global layer from adding an ffmpeg input or a maskedmerge.

    The combined matte is window * key, inverted as a whole by mask.invert.
    A product cannot be inverted inside either half, but it does factor:
    1 - w*k = (1 - w) * 1 + w * (1 - k). With the key OFF that collapses to
    1 - w, which is the window with its own invert flipped, so this returns
    the flipped block and one branch is enough. With the key ON as well,
    layer_branches() grades two branches instead and the window matte itself
    is left alone.
    """
    mask = layer["mask"]
    win = mask["window"]
    if not win.get("enabled"):
        return None
    if mask.get("invert") and not mask["key"].get("enabled"):
        return deep_merge(win, {"invert": not bool(win.get("invert"))})
    return win


def layer_branches(layer: dict) -> list[str]:
    """Which baked cubes this layer needs, in maskedmerge input order.

    One entry is one graded branch: the window matte, if there is one, merges
    it against the untouched picture, which is what the secondary and its
    window always did.

    Two entries is mask.invert with BOTH a window and a key, the one case the
    old shape could not express. The combined matte 1 - w*k factors as
    (1 - w) * 1 + w * (1 - k), so the first branch carries the full correction
    and wins where the window is closed and the second carries the correction
    masked by the inverted key and wins where the window is open. maskedmerge
    returns its first input where the matte is 0 and its second where it is
    maxval, which is precisely that arrangement, so the factorisation is exact
    rather than an approximation of it.
    """
    mask = layer["mask"]
    if not mask.get("invert"):
        return ["key" if mask["key"].get("enabled") else "one"]
    if mask["window"].get("enabled") and mask["key"].get("enabled"):
        return ["one", "inv"]
    # Window only: the flip lives in layer_window. Key only: in the cube.
    return ["one"] if mask["window"].get("enabled") else ["inv"]


def layer_blur_sigma(layer: dict, info: dict) -> float:
    """The layer's blur sigma in pixels at THIS frame's width.

    `blur` is quoted at 1920 wide and resolved against the frame's real width
    here, for the same reason every window parameter is a fraction: the studio
    grades against a preview that is usually 640 or 960 wide, and a sigma that
    did not scale would show a blur several times wider than the render will
    have. Resolving it in the engine rather than in server.scale_for_preview
    means the CLI render, the studio preview and the GPU port all take it from
    one rule and cannot drift apart.
    """
    sigma = float(layer["correct"].get("blur", 0.0))
    if sigma <= 0:
        return 0.0
    return sigma * float(info["width"]) / LAYER_BLUR_REF_WIDTH


def _layer_cube_key(layer: dict, variant: str) -> dict:
    """Everything the baked cube depends on, and nothing else.

    Spelled out rather than "the whole layer minus a few keys": the name, the
    enable flag, the placement, the window and the blur cannot change a single
    cube entry, and folding them into the hash would bake a second identical
    file every time a layer is renamed or dragged up the stack.
    """
    c = layer["correct"]
    return {
        "variant": variant,
        "show": bool(layer["mask"].get("show")),
        "key": layer["mask"]["key"],
        "correct": {k: c[k] for k in (
            "exposure", "contrast", "pivot", "saturation", "temperature",
            "tint", "hue_shift", "sat_gain", "lum_gain", "offset", "strength")},
    }


def layer_lut(layer: dict, variant: str = "key"):
    """Bake one layer's colour matte and correction into a cached 33-cube.

    `variant` picks which colour matte is folded in, which is how mask.invert
    is implemented without a per pixel expression (see layer_branches):
      "key"  the qualifier matte itself, the ordinary case
      "one"  a matte of 1 everywhere, the layer's full correction
      "inv"  1 minus the qualifier matte

    The display domain controls (exposure, contrast, saturation, temperature,
    tint) are each SKIPPED when they sit at their default rather than applied
    as an identity. That is not an optimisation: (v - p) * 1.0 + p is not bit
    for bit v, and a config migrated from the old secondary has to bake the
    exact cube it always baked or every approved render moves by a code.
    """
    import numpy as np
    sys.path.insert(0, str(ROOT / "tools"))
    import colorlib as C
    import hashlib

    layer = deep_merge(LAYER_DEFAULTS, layer or {})
    h = hashlib.sha1(json.dumps(_layer_cube_key(layer, variant),
                                sort_keys=True).encode()).hexdigest()[:16]
    LUT_LAYERS.mkdir(parents=True, exist_ok=True)
    path = LUT_LAYERS / f"layer_{h}.cube"
    if path.exists():
        return path

    k = layer["mask"]["key"]
    c = layer["correct"]
    size = 33
    grid = C.identity_grid(size)
    hue, sat, val = C.rgb_to_hsv(grid)
    luma = C.luma709(grid)[..., 0]

    if not k.get("enabled"):
        m = np.ones(grid.shape[:-1], dtype=np.float64)
    else:
        # Hue is circular, so distance has to wrap. Everything else is a plain
        # range on a bounded quantity.
        d = np.abs(((hue - float(k["hue_center"]) + 180.0) % 360.0) - 180.0)
        half = max(1e-6, float(k["hue_width"]) / 2.0)
        hs = max(1e-6, float(k["hue_soft"]))
        w_hue = np.clip(((half + hs) - d) / hs, 0.0, 1.0)
        w_sat = _soft_window(sat, float(k["sat_low"]), float(k["sat_high"]),
                             float(k["sat_soft"]))
        w_lum = _soft_window(luma, float(k["lum_low"]), float(k["lum_high"]),
                             float(k["lum_soft"]))
        m = w_hue * w_sat * w_lum
        if k.get("invert"):
            m = 1.0 - m
    if variant == "one":
        m = np.ones(grid.shape[:-1], dtype=np.float64)
    elif variant == "inv":
        m = 1.0 - m

    if layer["mask"].get("show"):
        out = np.repeat(m[..., None], 3, axis=-1)
        C.write_cube(path, np.clip(out, 0.0, 1.0), size, f"layer_mask_{h}",
                     comments=["Generated by cinegrade.layer_lut",
                               "Colour matte, shown as greyscale."])
        return path

    # The display domain half of the correction. The qualifier modulates every
    # step the same way it modulates the HSV controls below: each step is
    # lerped by m, so m = 0 leaves the pixel exactly alone and m = 1 applies
    # the whole move.
    cor = grid
    gains = [float(c["exposure"]) + float(c["temperature"]),
             float(c["exposure"]) + float(c["tint"]),
             float(c["exposure"]) - float(c["temperature"])]
    if any(abs(v) > 1e-6 for v in gains):
        # f_log_stage's rec709 branch: on a display signal a stop is a code
        # multiply of 2 ** (stops / gamma), not the log domain offset, and a
        # layer always sees a display signal.
        mul = np.asarray([2.0 ** (v / DISPLAY_GAMMA) for v in gains])
        cor = np.clip(cor * (1.0 + (mul - 1.0) * m[..., None]), 0.0, 1.0)
    con = float(c["contrast"])
    if abs(con - 1.0) > 1e-6:
        pivot = LAYER_PIVOT if c.get("pivot") is None else float(c["pivot"])
        cor = np.clip(cor + ((cor - pivot) * con + pivot - cor) * m[..., None],
                      0.0, 1.0)
    satv = float(c["saturation"])
    if abs(satv - 1.0) > 1e-6:
        # The same luma preserving matrix _saturation_matrix builds, written
        # as the arithmetic it performs rather than as an ffmpeg argument.
        y = (cor * np.asarray([0.2126, 0.7152, 0.0722])).sum(axis=-1)[..., None]
        cor = np.clip(cor + ((y + (cor - y) * satv) - cor) * m[..., None],
                      0.0, 1.0)
    if cor is not grid:
        # The HSV controls act on what the display controls produced. When
        # none of them ran, cor IS grid and this recompute is skipped, so the
        # numbers below are bit for bit the ones the secondary produced.
        hue, sat, val = C.rgb_to_hsv(cor)

    hue2 = hue + float(c["hue_shift"]) * m
    sat2 = sat * (1.0 + (float(c["sat_gain"]) - 1.0) * m)
    val2 = val * (1.0 + (float(c["lum_gain"]) - 1.0) * m)
    corrected = C.hsv_to_rgb(hue2, np.clip(sat2, 0.0, 1.0), np.clip(val2, 0.0, None))
    corrected = corrected + np.asarray(c.get("offset", [0.0, 0.0, 0.0])) * m[..., None]
    strength = float(c.get("strength", 1.0))
    out = np.clip(grid + (corrected - grid) * strength, 0.0, 1.0)

    C.write_cube(path, out, size, f"layer_{h}", comments=[
        "Generated by cinegrade.layer_lut",
        "Domain: Rec.709 gamma 2.4. Applied at the layer's placement point.",
    ])
    return path


# The two blocks a config written before layers existed carried, kept here as
# the merge base the migration fills a partial block in from. They are no
# longer in DEFAULTS: nothing reads them except migrate_layers.
LEGACY_SECONDARY = {
    "enabled": False, "show_mask": False, "invert": False,
    "hue_center": 30.0, "hue_width": 40.0, "hue_soft": 15.0,
    "sat_low": 0.10, "sat_high": 1.0, "sat_soft": 0.10,
    "lum_low": 0.0, "lum_high": 1.0, "lum_soft": 0.10,
    "hue_shift": 0.0, "sat_gain": 1.0, "lum_gain": 1.0,
    "tint": [0.0, 0.0, 0.0], "strength": 1.0,
}
LEGACY_WINDOW = {
    "enabled": False, "shape": "ellipse",
    "cx": 0.5, "cy": 0.5, "w": 0.6, "h": 0.6,
    "rotation": 0.0, "softness": 0.15, "invert": False,
}


def migrate_layers(cfg: dict) -> dict:
    """Rewrite a pre-layers config into the layers shape, on read.

    A config carrying `secondary` and/or `window` and no `layers` key becomes
    one layer: the qualifier goes to mask.key, the shape to mask.window,
    show_mask to mask.show, the HSV controls and the tint push and the
    strength to correct, and the layer is enabled exactly when the SECONDARY
    was. That last detail is the whole of the old window_active rule: a window
    switched on over a secondary switched off rendered nothing at all, because
    a shape with no correction to gate has nothing to do, and this keeps it
    so. Anything else would move renders that are already approved.

    Files on disk are never rewritten. The migration runs where a config is
    read, and saving writes the new shape.

    Run this on the config as it ARRIVED, before any merge with DEFAULTS:
    DEFAULTS always carries an empty `layers`, so a merge first would make
    every old config look like a new one and the secondary would be dropped
    on the floor instead of migrated.
    """
    if not isinstance(cfg, dict):
        return cfg
    if "secondary" not in cfg and "window" not in cfg:
        return cfg
    out = dict(cfg)
    sec = deep_merge(LEGACY_SECONDARY, out.pop("secondary", None) or {})
    win = deep_merge(LEGACY_WINDOW, out.pop("window", None) or {})
    if "layers" in out:
        # Both shapes present: the new one is the truth and the old keys are
        # residue from a client that has not caught up. Dropping them is the
        # migration.
        return out
    layer = deepcopy(LAYER_DEFAULTS)
    layer["enabled"] = bool(sec["enabled"])
    layer["placement"] = "before_look"
    layer["mask"]["show"] = bool(sec["show_mask"])
    layer["mask"]["invert"] = False
    layer["mask"]["window"] = {k: win[k] for k in LEGACY_WINDOW}
    layer["mask"]["key"] = dict(
        {k: sec[k] for k in ("hue_center", "hue_width", "hue_soft",
                             "sat_low", "sat_high", "sat_soft",
                             "lum_low", "lum_high", "lum_soft")},
        enabled=bool(sec["enabled"]), invert=bool(sec["invert"]))
    layer["correct"]["hue_shift"] = sec["hue_shift"]
    layer["correct"]["sat_gain"] = sec["sat_gain"]
    layer["correct"]["lum_gain"] = sec["lum_gain"]
    layer["correct"]["offset"] = list(sec["tint"])
    layer["correct"]["strength"] = sec["strength"]
    out["layers"] = [layer]
    return out


def build_layers(cfg, info, placement, src_label, pending, out_label):
    """Emit the layer stack for one placement point.

    `pending` is a filter chain nothing has written into a segment yet: the
    colour head, at the before_look point. A layer that needs no split appends
    to it, so a config whose only layer is a plain colour correction still
    produces the ONE chain the engine emitted before layers existed, filter
    for filter. That is what makes a migrated secondary byte identical rather
    than merely equivalent.

    Labels are named from the layer's index in the array (`ly3a`, `lw3`),
    never from a running counter, so graph_with_mask can name the same matte
    labels without replaying this function's control flow.

    Returns (segments, the label the picture is now on).
    """
    segs = []
    chain = list(pending)
    cur = src_label
    for i, layer in enumerate(config_layers(cfg)):
        if not layer_active(layer) or layer["placement"] != placement:
            continue
        show = bool(layer["mask"].get("show"))
        win = layer_window(layer)
        # The blur is a picture operation. In matte view there is no picture,
        # only the selection, and blurring that would misreport how soft the
        # mask really is, so it is left out of this branch.
        sigma = 0.0 if show else layer_blur_sigma(layer, info)
        blur = [f"gblur=sigma={sigma:.3f}"] if sigma > 0 else []
        cubes = [f"lut3d=file={esc(layer_lut(layer, v))}:interp=tetrahedral"
                 for v in layer_branches(layer)]
        if win is None:
            chain += cubes[:1] + blur
            continue
        tag = f"ly{i}"
        segs.append(f"[{cur}]{','.join(chain) if chain else 'null'}[{tag}i]")
        chain = []
        cur = f"{tag}i"
        segs.append(f"[{cur}]split=2[{tag}a][{tag}b]")
        if len(cubes) > 1:
            segs.append(f"[{tag}a]{','.join(cubes[:1] + blur)}[{tag}a2]")
            base = f"{tag}a2"
        elif show:
            # In matte view the graded branch IS the colour matte, so
            # compositing it over the picture would show the picture wherever
            # the window is closed. Against black the same merge reads as
            # colour matte times window matte, which is the selection the
            # layer will really make.
            segs.append(f"[{tag}a]colorchannelmixer=rr=0:gg=0:bb=0[{tag}a2]")
            base = f"{tag}a2"
        else:
            base = f"{tag}a"
        segs.append(f"[{tag}b]{','.join(cubes[-1:] + blur)}[{tag}b2]")
        # maskedmerge returns the FIRST input where the matte is 0 and the
        # second where it is maxval, so the un-graded branch has to be first.
        segs.append(f"[{base}][{tag}b2][lw{i}]maskedmerge[{tag}o]")
        cur = f"{tag}o"
    if chain:
        segs.append(f"[{cur}]{','.join(chain)}[{out_label}]")
        cur = out_label
    return segs, cur


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

# How an 8-bit matte gets into the 16-bit merge: a multiply by exactly 257, so
# code 255 arrives as 65535 and applies the whole correction. Both mattes use
# it, the window's and the radial blur ramp's. Named for the window because the
# window stage measured it first. See graph_with_mask.
WINDOW_MASK_FORMAT = "format=gray16le,format=gbrp16le"


WINDOW_TEMPLATE = LAYER_DEFAULTS["mask"]["window"]


def _window_block(cfg) -> dict:
    """Accept a bare window block, a mask block, a layer, or {"window": ...}.

    The window used to be one top level config key, so one shape per grade;
    it now lives at layers[i].mask.window and there is one per layer. Taking
    any of those spellings keeps every caller (the matte reference, the geq
    baker, the tests) able to hand over whichever level it is holding.
    """
    if not isinstance(cfg, dict):
        return deepcopy(WINDOW_TEMPLATE)
    if isinstance(cfg.get("mask"), dict):
        cfg = cfg["mask"]
    if isinstance(cfg.get("window"), dict):
        cfg = cfg["window"]
    if "shape" in cfg or "softness" in cfg or "enabled" in cfg or "cx" in cfg:
        return deep_merge(WINDOW_TEMPLATE, cfg)
    return deepcopy(WINDOW_TEMPLATE)


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


def window_layers(cfg) -> list[tuple]:
    """(array index, resolved window block) for every layer that needs a matte.

    One function, because three separate places have to agree on the answer
    (the ffmpeg input list, the graph builder, and the studio server's own
    input list) and disagreeing would not raise: ffmpeg would simply read the
    wrong input index and render a wrong picture in silence.

    Array order, not placement order. A before_look and an after_look layer
    are graded at different points in the tree but their mattes are ordinary
    inputs, so keeping the input order tied to the array keeps it independent
    of anything the graph builder decides later.
    """
    out = []
    for i, layer in enumerate(config_layers(cfg)):
        if not layer_active(layer):
            continue
        win = layer_window(layer)
        if win is not None:
            out.append((i, win))
    return out


def mask_input_indices(cfg) -> dict:
    """Which ffmpeg input index each generated still lands on.

    Input 0 is the picture. Everything after it is appended in this order by
    ffmpeg_inputs, and build_graph and graph_with_mask read the indices back
    from here rather than counting again by hand. "layers" maps a layer's
    array index to the input its window matte lands on.
    """
    idx = 1
    out = {}
    if cfg["fx"]["radial_blur"]["enabled"]:
        out["radial"] = idx
        idx += 1
    windows = {}
    for i, _win in window_layers(cfg):
        windows[i] = idx
        idx += 1
    out["layers"] = windows
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
    """A cached greyscale radial ramp used as the radial-blur matte.

    `format=gray` runs BEFORE the geq for the same reason it does in
    window_mask. The lavfi colour source is yuv, so writing 0-255 into its luma
    plane and converting to gray afterwards costs a tv-to-full expansion: the
    code the expression asked for comes back as round((v-16)*255/219) clipped
    to 0-255. Measured on a 256 wide identity ramp: 248 of 256 codes moved, up
    to 20 code values, and only 220 distinct codes survived, so everything at
    or below 16 flattened to 0 and everything at or above 235 flattened to 255.
    Writing straight into a gray plane costs nothing and lands the exact code.
    The matte is still an 8-bit PNG; this is the scaling, not the depth.
    """
    d = ROOT / "luts" / "masks"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"radial_{w}x{h}_{start:.2f}_{end:.2f}.png"
    if not p.exists():
        expr = (f"255*clip((hypot((X-{w}/2)/({w}/2),(Y-{h}/2)/({h}/2))"
                f"-{start})/({max(1e-3, end - start)}),0,1)")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
             "-i", f"color=c=black:s={w}x{h}:d=1",
             "-vf", f"format=gray,geq=lum='{expr}'",
             "-frames:v", "1", str(p)], check=True)
    return p


# Stock presets set size, strength and softness only, and REPLACE those three
# numbers outright rather than nudging them: see grain_effective. Measured on
# footage/A001_09011336_C002.MOV (grade/tests/harness.CLIP_A, TIME_A=2.0),
# rendered at 1920 wide (harness.scaled_info), grain-only configs (everything
# else at engine defaults), comparing against the same frame with grain off.
# "diff std" is the std of (graded - ungraded) over the whole 1920x1080 frame;
# "hf" is the existing 3x3 high-frequency proxy (harness.hf_energy) on the
# graded frame. Swept size 1..8 at strength 40 and strength 10..70 at size 3
# first (see grade/tests/cases_grain.py STOCK_SWEEP_NOTES for the full table):
# hf falls as size grows even where amplitude (diff std) rises, because a
# bigger downscale factor spreads the same random draw over a bigger block,
# so pixel-to-pixel high-frequency energy drops while the per-blob amplitude
# can still be larger. That is genuine film behaviour (coarse grain reads as
# "big soft blobs", not as more per-pixel noise), so it is what the presets
# lean on: 35mm reproduces today's shipped default exactly, on purpose (this
# engine's numbers were already tuned against a 35mm-equivalent scan).
#   65mm  size 2 strength 20 softness 0.0 -> diff std 1.502, hf 0.00246 (finest, lightest)
#   35mm  size 3 strength 40 softness 0.0 -> diff std 3.195, hf 0.00281 (today's default, unchanged)
#   16mm  size 6 strength 55 softness 0.35 -> diff std 4.245, hf 0.00193 (coarsest/heaviest,
#         blob edges rounded off by the softness so they read as clumps, not squares)
GRAIN_STOCKS = {
    "16mm": {"size": 6, "strength": 55, "softness": 0.35},
    "35mm": {"size": 3, "strength": 40, "softness": 0.0},
    "65mm": {"size": 2, "strength": 20, "softness": 0.0},
}


def grain_effective(cfg) -> dict:
    """size, strength and softness after a stock preset override.

    A stock other than "custom" REPLACES these three numbers outright: it is
    not a starting point the sliders then nudge, it is what the panel's own
    tooltip says a stock is. "custom" (the default) leaves the raw fields
    alone, which is what keeps a plain grain.enabled=True config byte
    identical to before these fields existed.
    """
    g = cfg["grain"]
    stock = g.get("stock", "custom")
    if stock in GRAIN_STOCKS:
        p = GRAIN_STOCKS[stock]
        return {"size": int(p["size"]), "strength": int(p["strength"]),
                "softness": float(p["softness"])}
    return {"size": max(1, int(g.get("size", 3))),
            "strength": int(g.get("strength", 40)),
            "softness": max(0.0, float(g.get("softness", 0.0)))}


def _grain_color_active(g: dict) -> bool:
    return float(g.get("color", 0.0) or 0.0) > 1e-6


def grain_input(cfg, info):
    """The grey noise plate, and, with color > 0, a second independent one.

    Blending a grey plate in overlay mode is how grain actually works: grey is
    neutral, so the plate modulates the image rather than washing it.

    The noise goes on component 0 only. The plate is yuv, so component 0 is
    luma and 1 and 2 are chroma; `alls` hits all three, which shakes the colour
    difference channels and lands as visible red and blue speckle rather than
    grain. Perturbing luma alone keeps R, G and B equal, so the grain is
    monochrome the way a film grain plate is.

    seed 0 (the default) omits ffmpeg's own seed option entirely, which is
    what keeps this string byte identical to before the field existed. Any
    other value adds `all_seed=<seed>`. Measured on this build (ffmpeg 8.1.1):
    the PER COMPONENT seed option (`c0_seed`) has no effect at all here (seed
    0 vs 1 vs 999999 on `c0s=...:c0f=t+u:c0_seed=N` were byte identical every
    time); `all_seed` (the "set every component" option) does change the
    plate, and the unset default is byte identical to explicit `all_seed=0`,
    both reproducible run to run across separate ffmpeg processes.

    color > 0 (contract C3) needs a second, independently random plate: noise
    applied directly on an rgb24 (three real plane) source with c0s/c1s/c2s
    draws three different values per pixel even from the same seed, because
    the RNG stream simply advances differently per plane. That second input is
    added ONLY when color is active, so a color=0 config (today's default)
    emits exactly the one line it always has.
    """
    g = cfg["grain"]
    eff = grain_effective(cfg)
    size = eff["size"]
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
    seed = int(g.get("seed", 0) or 0)
    seed_arg = f":all_seed={seed}" if seed != 0 else ""
    args = ["-f", "lavfi", "-i",
            f"color=c=gray:s={w}x{h}:r=24:d={seconds:.3f},"
            f"noise=c0s={eff['strength']}:c0f=t+u{seed_arg}"]
    if _grain_color_active(g):
        args += ["-f", "lavfi", "-i",
                 f"color=c=gray:s={w}x{h}:r=24:d={seconds:.3f},format=rgb24,"
                 f"noise=c0s={eff['strength']}:c1s={eff['strength']}:"
                 f"c2s={eff['strength']}:c0f=t+u:c1f=t+u:c2f=t+u{seed_arg}"]
    return args


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

        LOG -> PREP -> CST IN -> PRIMARIES -> CST OUT -> CURVES -> SLICE
             -> LAYERS(before_look) -> LOOK -> LAYERS(after_look)
             -> FX -> GRAIN -> DETAIL -> LETTERBOX -> OUT

    PREP is prep.denoise (hqdn3d), which runs on the source before any colour
    transform: after f_log_stage's decode-normalise and exposure, before
    f_convert_in (the CST). DETAIL is soften, sharpen, then mid_detail (a
    split/blur/blend local-contrast pass, see f_mid_detail_segment), in that
    order.

    A layer with a window splits the tree at its own placement point, grades
    one branch through its baked cube, and maskedmerges the two back together
    under the window matte. A layer without one is a single cube in the
    running chain, which is exactly what the secondary used to be, so the
    graph text for a migrated grade is the graph text that always shipped.

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
    head = (f_log_stage(cfg, info, normalised=src_normalised) + f_denoise(cfg)
            + f_convert_in(cfg, info)
            + f_primaries(cfg, info) + f_convert_out(cfg, info)
            + f_curves(cfg) + f_slice(cfg))
    # The before_look layers. With no layers this is the one head chain the
    # engine has always emitted, ending on [cst], so nothing already graded
    # moves; build_layers only splits the tree when a layer needs a matte.
    segs, cur = build_layers(cfg, info, "before_look", src_label, head, "cst")
    pre_look = cur

    # LOOK, with a real opacity. A creative LUT at full strength is almost
    # always too strong; dialling it to 0.4-0.6 is the normal working state.
    #
    # Two slots blend in PARALLEL, not stacked: both read pre_look (the same
    # pre-look signal), never each other's output. A = lerp(base, lut1(base),
    # mix), B = lerp(base, lut2(base), mix2), out = lerp(A, B, balance). Slot
    # 2 is skipped entirely (lut2 unset or balance ~0) so the graph and the
    # bytes match today's single-slot output exactly, not merely closely: no
    # split is inserted at all in that case, so the graph TEXT is unchanged.
    lut2 = cfg["look"].get("lut2")
    balance = float(cfg["look"].get("balance", 0.0))
    look2_active = bool(lut2) and balance > 1e-6
    # At balance >= 0.999 the output is lut2 alone (same "skip the blend at
    # the extreme" trick as mix): A is never read, so slot 1 must not be
    # built at all, not just left unread. ffmpeg treats an output label
    # nothing reads as a hard error ("Filter ... has output ... unconnected"),
    # not a harmless no-op, which is what building it and then discarding it
    # hit before this guard existed.
    need_slot1 = not (look2_active and balance >= 0.999)
    if look2_active and need_slot1:
        # Both slot 1 and slot 2 (and, with no slot 1 LUT, the final balance
        # blend below) need their own read of pre_look. A bare second
        # reference to the same output label does NOT fan out in ffmpeg: it
        # silently rebinds to the raw, unscaled input instead of the same
        # link. Measured: without this split, the second consumer's branch
        # reports the source's native resolution (3840x2160 on this footage)
        # instead of the graph's working size, and blend refuses to run.
        segs.append(f"[{pre_look}]split=2[pll1][pll2]")
        look_in, look2_in = "pll1", "pll2"
    else:
        look_in = look2_in = pre_look

    look = f_look(cfg) if need_slot1 else []
    if look:
        mix = float(cfg["look"].get("mix", 1.0))
        if mix >= 0.999:
            segs.append(f"[{look_in}]{','.join(look)}[lk]")
        else:
            segs.append(f"[{look_in}]split=2[lka][lkb]")
            segs.append(f"[lkb]{','.join(look)}[lkc]")
            # ffmpeg's normal blend is dst = in0*opacity + in1*(1-opacity),
            # so the graded branch has to be in0 for `mix` to mean look
            # strength. Verified empirically; the reverse silently makes
            # mix=0 identical to mix=1.
            segs.append(
                f"[lkc][lka]blend=all_mode=normal:all_opacity={mix:.4f}[lk]")
        cur = "lk"
    else:
        cur = look_in
    a_label = cur

    if look2_active:
        look2 = f_look(cfg, "lut2")
        mix2 = float(cfg["look"].get("mix2", 1.0))
        if mix2 >= 0.999:
            segs.append(f"[{look2_in}]{','.join(look2)}[lk2]")
        else:
            segs.append(f"[{look2_in}]split=2[lk2a][lk2b]")
            segs.append(f"[lk2b]{','.join(look2)}[lk2c]")
            segs.append(
                f"[lk2c][lk2a]blend=all_mode=normal:all_opacity={mix2:.4f}[lk2]")
        if balance >= 0.999:
            # Same "skip the blend at the extreme" trick as mix above: this
            # is what makes balance=1 exactly equal to lut2 alone rather than
            # only close to it.
            cur = "lk2"
        else:
            # out = lerp(A, B, balance): B (lk2) has to be in0 for opacity to
            # mean "balance is the slot 2 weight", mirroring the mix blend.
            segs.append(
                f"[lk2][{a_label}]blend=all_mode=normal:all_opacity={balance:.4f}[lkbal]")
            cur = "lkbal"

    # The after_look layers. Nothing is pending here (the look wrote its own
    # segment), so with no after_look layers this adds not one character to
    # the graph.
    post_segs, cur = build_layers(cfg, info, "after_look", cur, [], "post")
    segs += post_segs

    fx_segs, cur, _ = build_fx(cfg, info, cur, 0)
    needs_mask = cfg["fx"]["radial_blur"]["enabled"]
    segs += fx_segs

    # Retag as Rec.709 inside the graph. The -color_primaries output flags
    # alone do not stick: the encoder inherits BT.2020 frame properties from
    # the source, and the file ends up claiming a gamut it is not in, which
    # every color-managed player then over-saturates.
    if cfg["grain"]["enabled"]:
        g = cfg["grain"]
        eff = grain_effective(cfg)
        idx_g = mask_input_indices(cfg)["grain"]
        color_amt = float(g.get("color", 0.0) or 0.0)
        color_on = _grain_color_active(g)
        soft = max(0.0, float(eff["softness"]))
        response = g.get("response", "flat")
        # The byte-identical fast path: with softness, color and response all
        # at their defaults this is the exact two lines that shipped before
        # any of C3's new fields existed, unchanged character for character.
        if not (soft > 1e-6 or color_on or response == "film"):
            segs.append(f"[{idx_g}:v]scale={info['width']}:{info['height']}"
                        f":flags=bilinear,format=gbrp16le,setsar=1[grainplate]")
            grain_label = "grainplate"
        else:
            # format=gbrp16le FIRST, at the small plate resolution, so gblur
            # (softness) and the color mix both run in the same working
            # format as the rest of this chain, before the upscale spreads
            # one plate sample over `size` output pixels the way it always
            # has. Blurring at plate resolution is also what makes softness
            # a sigma "in plate pixels": it does not need to change with the
            # frame size, only with size (the same thing the un-blurred plate
            # already does).
            segs.append(f"[{idx_g}:v]format=gbrp16le[gmono]")
            plate_pre_scale = "gmono"
            if color_on:
                idx_c = idx_g + 1
                segs.append(f"[{idx_c}:v]format=gbrp16le[gcol]")
                mono_src, color_src = "gmono", "gcol"
                if soft > 1e-6:
                    segs.append(f"[gmono]gblur=sigma={soft:.4f}[gmonob]")
                    segs.append(f"[gcol]gblur=sigma={soft:.4f}[gcolb]")
                    mono_src, color_src = "gmonob", "gcolb"
                # color mixes between the mono plate (color=0) and the
                # independent RGB plate (color=1): all_mode=normal is a plain
                # lerp, dst = BOTTOM*(1-opacity) + TOP*opacity, so the
                # independent plate has to be TOP (in0) for `color` to mean
                # "how much of the independent plate" and opacity=0 to be
                # exactly the mono plate. Same convention f_look's mix uses.
                segs.append(f"[{color_src}][{mono_src}]blend=all_mode=normal"
                            f":all_opacity={color_amt:.4f}[gmix]")
                plate_pre_scale = "gmix"
            elif soft > 1e-6:
                segs.append(f"[gmono]gblur=sigma={soft:.4f}[gmonob]")
                plate_pre_scale = "gmonob"
            segs.append(f"[{plate_pre_scale}]scale={info['width']}:{info['height']}"
                        f":flags=bilinear,setsar=1[gscaled]")
            grain_label = "gscaled"
            if response == "film":
                # response=film scales the plate's deviation from mid grey by
                # a weight that is 1 in the shadows and midtones (L <= 0.5)
                # and eases down to 0.25 by L = 1, measured on the pixel's own
                # luminance AFTER the look (this stage's `cur`, which by this
                # point in build_graph already ran curves/slice/layers/
                # look/fx). W(L) = 1 for L<=0.5, easing with a
                # smoothstep (3t^2-2t^3, t=clip(2*(L-0.5),0,1)) to 0.25 at
                # L=1: this exact formula is also in schema.js's tooltip for
                # grain.response and gpu.js's STAGE_NOTES.grain, so all three
                # describe the same curve. Computed in ONE blend node using
                # ffmpeg's st()/ld() scratch registers (confirmed available
                # in the blend filter's all_expr on this build): A is the
                # luma-replicated `cur` (top), B is the scaled plate (bottom).
                segs.append(f"[{cur}]split=2[glumasrc][gcarry]")
                segs.append(
                    "[glumasrc]colorchannelmixer="
                    "rr=0.2126:rg=0.7152:rb=0.0722:"
                    "gr=0.2126:gg=0.7152:gb=0.0722:"
                    "br=0.2126:bg=0.7152:bb=0.0722[glumamap]")
                weight_expr = (
                    "st(0,clip((A/65535-0.5)*2,0,1));"
                    "st(1,1-0.75*(3*ld(0)*ld(0)-2*ld(0)*ld(0)*ld(0)));"
                    "0.5*65535+ld(1)*(B-0.5*65535)"
                )
                segs.append(f"[glumamap][gscaled]blend=all_expr='{weight_expr}'"
                            f":shortest=1[gweighted]")
                grain_label = "gweighted"
                cur = "gcarry"
        # shortest=1 is what actually guarantees the render terminates: it ends
        # the blend when the graded stream ends rather than when every input
        # does, so a grain plate that outlives the picture cannot run the
        # output on forever. The picture is input 0 here and the plate is always
        # generated longer, so this can never truncate a render.
        #
        # overlay+opacity: dst = cur*(1-opacity) + overlay(cur,plate)*opacity,
        # measured against ffmpeg's own integer blend on 2000 random 16-bit
        # pairs at opacity 0.5 (max |predicted-actual| 1.49 of 65535, mean
        # 0.57): this is the formula gpu.js's grain pass reproduces.
        segs.append(f"[{cur}][{grain_label}]blend=all_mode=overlay:shortest=1"
                    f":all_opacity={float(g.get('opacity', 0.5)):.4f}[gx]")
        cur = "gx"

    # mid_detail needs a split/blur/blend sub-graph (real labels), which a
    # flat comma-joined chain cannot express, so it only enters the graph at
    # all when non-zero: every existing config (mid_detail defaults to 0.0)
    # takes the exact original code path below, unchanged graph text included.
    mid_detail = float((cfg.get("detail") or {}).get("mid_detail", 0.0))
    if abs(mid_detail) > 1e-6:
        det_chain = f_detail(cfg)
        if det_chain:
            segs.append(f"[{cur}]{','.join(det_chain)}[det1]")
            cur = "det1"
        cur = f_mid_detail_segment(mid_detail, info, cur, segs)
        tail = f_letterbox(cfg, info)
    else:
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
    #
    # Every rotation but auto ignores the tag here; the quarter turns 90, 180
    # and 270 then transpose in the graph (graph_with_mask), because ffmpeg
    # has no pre-input filter to do it with.
    args += rotate_args(rotation_of(info))
    if seek is not None:
        args += ["-ss", str(seek)]
    args += ["-i", src]
    if cfg["fx"]["radial_blur"]["enabled"]:
        rb = cfg["fx"]["radial_blur"]
        m = radial_mask(info["width"], info["height"], rb["start"], rb["end"])
        args += ["-i", str(m)]
    for _i, win in window_layers(cfg):
        args += ["-i", str(window_mask(win, info["width"], info["height"]))]
    if cfg["grain"]["enabled"]:
        args += grain_input(cfg, info)
    return args


def graph_with_mask(cfg, info, out_label="vout", tail_extra=None, encode_out=True,
                    src_label="0:v", head_extra=None, src_normalised=False,
                    src_rotated=None):
    """The whole filter_complex, including the matte inputs and the rotation.

    src_rotated says the source this graph is handed has ALREADY been turned
    (the studio's cached source frame, or a caller whose own head_extra chain
    does the transpose before its scale). Left as None it means "True when
    head_extra is given", because a caller building its own source chain is
    exactly the caller whose scale target would be wrong if the turn happened
    after it: the transpose has to run before any resize, since every scale
    target in this engine is computed from probe's post-rotation size.

    For auto and 0 there is no transpose at all, so the graph text is byte for
    byte what it was before rotation existed.
    """
    if src_rotated is None:
        src_rotated = head_extra is not None
    rotate = ([] if (src_normalised or src_rotated)
              else rotate_filters(rotation_of(info)))
    rotate_seg = None
    if rotate:
        rotate_seg = f"[{src_label}]{','.join(rotate)},setsar=1[rotsrc]"
        src_label = "rotsrc"
    graph, needs_mask = build_graph(cfg, info, out_label, tail_extra, encode_out,
                                    src_label, src_normalised)
    idxs = mask_input_indices(cfg)
    # Both mattes take the same hop into the 16-bit merge, and the hop is not
    # decoration. Going straight from the 8-bit matte to gbrp16le expands by a
    # left shift of 8, so a matte code of 255 arrives at maskedmerge as 65280
    # of 65535 and a fully open matte applies only 99.61% of the correction.
    # Measured on the window: a mean residual of 0.186 of 255 on 18.6% of
    # pixels against the un-windowed render, where it should be exactly zero.
    # Through gray16le the expansion is a multiply by 257 instead, verified
    # exact across all 256 codes (max |v - 257n| = 0), so 255 is the whole
    # correction and 0 is none of it. The matte still carries 256 levels: this
    # is the scaling, not the depth. ffmpeg's lut filter cannot do the same job
    # here, it clips its own output at 65280 on this build.
    #
    # The radial ramp shipped on the shift until 2026-09-04 and now shares the
    # window's hop. That moved approved renders (max 20 of 255 on the ramp
    # region, from the tv-to-full expansion radial_mask also carried), so it
    # was a founder call, taken and approved.
    #
    # One matte segment per layer that has a window, labelled from the layer's
    # array index so build_layers and this function name the same link without
    # either having to replay the other's control flow.
    pre = []
    if rotate_seg:
        pre.append(rotate_seg)
    if needs_mask:
        pre.append(f"[{idxs['radial']}:v]{WINDOW_MASK_FORMAT},setsar=1[mask]")
    for i, _win in window_layers(cfg):
        pre.append(f"[{idxs['layers'][i]}:v]{WINDOW_MASK_FORMAT},setsar=1[lw{i}]")
    if pre:
        graph = ";".join(pre) + ";" + graph
    if head_extra:
        graph = head_extra + ";" + graph
    return graph


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cli_rotation(a, cfg: dict | None = None) -> str:
    """The rotation one parsed command line, and optionally its config, asks for.

    --rotate wins when it is given. --no-autorotate stays as the alias of
    --rotate 0 it always was, so every script and every note written before
    this flag existed keeps working unchanged. With neither flag present, a
    non-auto `cfg["rotation"]` (contract G4: rotation lives in the config, the
    same preset or grade the rest of the render reads) is next, so a preset
    saved with a real rotation renders correctly with no flag at all. `cfg`
    is optional and defaults to None so every existing caller (and every
    existing test) that passes one argument is unaffected.
    """
    chosen = getattr(a, "rotate", None)
    if chosen:
        return normalise_rotation(chosen)
    if getattr(a, "no_autorotate", False):
        return "0"
    if cfg is not None:
        from_cfg = cfg.get("rotation")
        if from_cfg not in (None, ""):
            mode = normalise_rotation(from_cfg)
            if mode != "auto":
                return mode
    return "auto"


# --------------------------------------------------------------------------
# region and zoom: looking closely at part of a frame
#
# An agent judging skin, a highlight roll off or a single edge needs the
# pixels of one patch at a usable size, not a 560px wide postcard of the
# whole shot. --region picks the patch and --zoom says how big to render it.
#
# The crop runs AFTER the whole grade and after the rotation, and before the
# output scale. That ordering is the whole correctness argument: every
# spatial stage in this engine (power windows, the radial ramp, vignette,
# the grain plate) is sized from probe's post-rotation frame, so cropping
# first would hand those stages a smaller frame and move the window, the
# vignette and the grain relative to the picture. Cropping the finished
# render cannot: the patch is exactly the pixels the full render has there.
# --------------------------------------------------------------------------

MIN_REGION = 0.01     # a region under 1% of an axis is refused, not measured


def normalise_region(box):
    """Four fractions of the frame AFTER rotation, as [x0, y0, x1, y1].

    Either drag direction is the same rectangle (the corners are sorted),
    values outside 0..1 clamp, and anything that is not four numbers, or is
    a sliver under MIN_REGION on an axis, raises rather than being silently
    measured somewhere else.
    """
    if box is None:
        return None
    if isinstance(box, str):
        box = box.replace(",", " ").split()
    try:
        vals = [float(v) for v in box]
    except (TypeError, ValueError):
        raise GradeError("region wants four numbers: x0 y0 x1 y1")
    if len(vals) != 4:
        raise GradeError(f"region wants four numbers, got {len(vals)}")
    vals = [min(1.0, max(0.0, v)) for v in vals]
    x0, x1 = min(vals[0], vals[2]), max(vals[0], vals[2])
    y0, y1 = min(vals[1], vals[3]), max(vals[1], vals[3])
    if x1 - x0 < MIN_REGION or y1 - y0 < MIN_REGION:
        raise GradeError(
            f"region is too small to render: {x1 - x0:.3f} by {y1 - y0:.3f} "
            f"of the frame, the minimum is {MIN_REGION} on each axis")
    return [x0, y0, x1, y1]


def normalise_zoom(value):
    """How many times the size the region gets, 1 meaning its fit size."""
    if value is None:
        return 1.0
    try:
        z = float(value)
    except (TypeError, ValueError):
        raise GradeError(f"zoom wants a number, got {value!r}")
    if not (z > 0):
        raise GradeError(f"zoom must be greater than zero, got {z:g}")
    return z


def region_pixels(region, info):
    """A region as whole pixels of the post-rotation frame: (x, y, w, h).

    Rounds OUTWARD (floor the start, ceil the end), the same convention
    match_ref.crop_box uses, so a rectangle drawn on a small preview still
    covers every pixel it visibly covered.
    """
    fw, fh = int(info["width"]), int(info["height"])
    x0 = max(0, min(fw - 1, int(math.floor(region[0] * fw))))
    y0 = max(0, min(fh - 1, int(math.floor(region[1] * fh))))
    x1 = max(x0 + 1, min(fw, int(math.ceil(region[2] * fw))))
    y1 = max(y0 + 1, min(fh, int(math.ceil(region[3] * fh))))
    return x0, y0, x1 - x0, y1 - y0


def region_tail(region, zoom, width, info):
    """The crop and scale filters a still's tail needs, in order.

    Replaces the plain `scale={width}:-2` a still used before this existed.
    With no region and zoom 1 it IS that plain scale, character for
    character, so an unzoomed still is byte identical to the one this engine
    rendered before the flags existed.

    The zoom cap is the source itself: a region cannot be rendered wider
    than the pixels it actually contains, because there are no more of them.
    Without --width there is no scale at all, which is already the cap, so
    zoom has nothing left to do there.
    """
    region = normalise_region(region)
    zoom = normalise_zoom(zoom)
    if region is None:
        if not width:
            return []
        if zoom == 1.0:
            return [f"scale={width}:-2"]
        target = max(2, min(int(round(width * zoom)), int(info["width"])))
        return [f"scale={target}:-2"]
    x, y, w, h = region_pixels(region, info)
    out = [f"crop={w}:{h}:{x}:{y}"]
    if width:
        # The region's fit size is the share of a `width` wide frame it
        # covers; zoom multiplies that, and the region's own pixel width is
        # where it stops.
        target = max(2, min(int(round(width * zoom * w / float(info["width"]))), w))
        out.append(f"scale={target}:-2")
    return out


def cmd_render(a):
    cfg = apply_overrides(load_preset(a.preset), a)
    rotation_mode = cli_rotation(a, cfg)
    info = probe(a.input, rotation=rotation_mode)
    # --codec only swaps the encoder for this invocation; the preset dict
    # (and file, if any) is never written back to.
    codec = a.codec or cfg["output"]["codec"]
    expect_ext = ".mov" if codec == "prores_ks" else ".mp4"
    out_ext = Path(a.output).suffix.lower()
    if out_ext != expect_ext:
        raise GradeError(
            f"-o {a.output} needs a {expect_ext} extension for --codec "
            f"{codec}, not {out_ext or 'no extension'}")
    rcfg, rinfo, head, src = cfg, info, None, "0:v"
    if a.width is not None or a.scale is not None:
        # Reuse the preview path's own scaler (studio/server.py) rather than
        # a second one, so a scaled render is the same picture the studio
        # would show at that size: same fraction off halation/bloom sigma,
        # radial blur, RGB split, soften and grain size.
        sys.path.insert(0, str(ROOT.parent / "studio"))
        import server as studio_server
        raw_width = a.width if a.width is not None else info["width"] * a.scale
        width = max(2, int(raw_width) // 2 * 2)
        factor = width / float(info["width"])
        height = max(2, int(round(info["height"] * factor / 2)) * 2)
        rinfo = dict(info, width=width, height=height)
        rcfg = studio_server.scale_for_preview(cfg, factor)
        head = (f"[0:v]{rotate_prefix(rinfo)}"
               f"scale={width}:{height}:flags=bicubic,setsar=1[studiosrc]")
        src = "studiosrc"
    graph = graph_with_mask(rcfg, rinfo, src_label=src, head_extra=head)
    o = dict(cfg["output"], codec=codec)
    args = ffmpeg_inputs(a.input, rcfg, rinfo, a.start, a.duration)
    if a.duration:
        args += ["-t", str(a.duration)]
    args += ["-filter_complex", graph, "-map", "[vout]"]
    if not a.no_audio:
        # First audio stream only: a phone clip whose second stream has no
        # decoder used to kill the whole render.
        args += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "256k"]
    if codec == "prores_ks":
        args += ["-c:v", "prores_ks", "-profile:v", str(o["profile"]),
                 "-vendor", "apl0", "-pix_fmt", "yuv422p10le"]
    else:
        args += ["-c:v", codec, "-crf", str(o["crf"]),
                 "-preset", o["preset"], "-pix_fmt", "yuv420p"]
    args += ["-color_primaries", "bt709", "-color_trc", "bt709",
             "-colorspace", "bt709", a.output]
    start = a.start or 0.0
    total = a.duration if a.duration else max(0.1, info["duration"] - start)
    run_render(args, a.verbose, total=total)
    # The single most consequential decision a mixed folder render makes:
    # which input transform it resolved to. Before this line the only way
    # to see it was --verbose plus reading the cube name out of the middle
    # of the printed ffmpeg command (contract G9 friction 7).
    resolved_input = resolve_input(cfg, info)[0]
    print(f"rendered (input {resolved_input}, rotation {rotation_mode}) "
         f"-> {a.output}")


def cmd_still(a):
    cfg = apply_overrides(load_preset(a.preset), a)
    info = probe(a.input, rotation=cli_rotation(a, cfg))
    extra = region_tail(getattr(a, "region", None), getattr(a, "zoom", None),
                        a.width, info) + ["format=rgb24"]
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
    info = probe(a.input, rotation=cli_rotation(
        a, apply_input_space(load_preset(a.preset), a)))
    if a.presets:
        variants = [(v, {"preset": v}) for v in a.presets.split(",")]
    elif a.looks:
        variants = [(v, {"look": v}) for v in a.looks.split(",")]
    else:
        variants = [(v.stem, {"look": v.stem}) for v in sorted(LUT_LOOKS.glob("*.cube"))]

    tmp = ROOT / "stills" / "_compare"
    tmp.mkdir(parents=True, exist_ok=True)
    # One tail for every panel: the panels are hstacked, so a region that
    # resolved to a different pixel size per panel would not stack at all.
    tail = region_tail(getattr(a, "region", None), getattr(a, "zoom", None),
                       a.width, info) + ["format=rgb24"]
    paths = []
    for name, spec in variants:
        cfg = apply_input_space(load_preset(spec.get("preset") or a.preset), a)
        if "look" in spec:
            cfg["look"]["lut"] = spec["look"]
        graph = graph_with_mask(cfg, info, encode_out=False, tail_extra=tail)
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
    info = probe(a.input, rotation=cli_rotation(a, cfg))
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


def _measure_region_size(region, info, width=None) -> tuple[int, int]:
    """The pixel size a stats measurement at this region and width comes
    back as: the whole frame, or exactly the region's own pixels, scaled to
    `width` (round 2 tooling note 3: `sweep` used to always measure at full
    resolution) by the identical arithmetic `region_tail`'s own `scale`
    filter runs, so the buffer this decodes never mismatches what ffmpeg
    actually wrote. `width=None` is the no-scale case, unchanged."""
    if region is None:
        w, h = int(info["width"]), int(info["height"])
    else:
        reg = normalise_region(region)
        _, _, w, h = region_pixels(reg, info)
    if not width:
        return w, h
    if region is None:
        tw = max(2, int(width))
    else:
        tw = max(2, min(int(round(width * w / float(info["width"]))), w))
    th = max(2, int(round(h * tw / float(w) / 2)) * 2)
    return tw, th


def _grade_frame_stats(a, cfg, info, t: float, region=None, path=None,
                       width=None) -> dict:
    """One graded frame of `a.input`, measured through grade.stats.frame_stats.

    The same numbers `POST /api/stats` returns for the same clip, config,
    time and region: both read the array through the one function, not two
    hand written copies of it.

    `path` overrides `a.input` as the file actually read: `cmd_stats`'s
    `--image` branch measures `a.image` through this same graph builder
    (round 2 tooling note 1, an explicit --input-space/--working-space on a
    still) while `a.input` stays whatever the parsed command line carried,
    which may be nothing at all on that branch.

    `width` scales the measured frame down before it is read (round 2
    tooling note 3: `sweep` used to always measure at the source's full
    resolution); `None` measures at the source's own size, unchanged.
    """
    import numpy as np                                        # noqa: PLC0415
    from stats import frame_stats                            # noqa: PLC0415
    src = path if path is not None else a.input
    w, h = _measure_region_size(region, info, width=width)
    extra = region_tail(region, None, width, info) + ["format=rgb24"]
    graph = graph_with_mask(cfg, info, tail_extra=extra, encode_out=False)
    args = ffmpeg_inputs(src, cfg, info, t)
    args += ["-filter_complex", graph, "-map", "[vout]", "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    r = subprocess.run(args, capture_output=True, timeout=60)
    want = w * h * 3
    if r.returncode != 0 or len(r.stdout) < want:
        raise GradeError("cinegrade could not measure that frame:\n"
                         + r.stderr.decode("utf-8", "replace")[-1200:])
    rgb = np.frombuffer(r.stdout[:want], np.uint8).reshape(h, w, 3)
    return {"time": t, "key": f"{Path(src).name}@{t:g}s",
            "size": [w, h], "stats": frame_stats(rgb)}


def _print_stats_block(label: str, row: dict) -> None:
    s = row["stats"]
    w, h = row["size"]
    lu, sa, ch, fam = s["luma"], s["saturation"], s["channels"], s["families"]
    cl, bd = s["clipped"], s["bands"]
    print(f"{label}  {w}x{h}")
    print(f"  luma     p5 {lu['p5']:.4f}  p25 {lu['p25']:.4f}  p50 {lu['p50']:.4f}  "
          f"p75 {lu['p75']:.4f}  p95 {lu['p95']:.4f}  mean {lu['mean']:.4f} "
          f"({lu['mean8']:.1f}/255)")
    print(f"  sat      mean {sa['mean']:.4f}  mean(coloured) {sa['mean_coloured']:.4f}  "
          f"p95 {sa['p95']:.4f}")
    print(f"  rgb      r {ch['r']:.4f}  g {ch['g']:.4f}  b {ch['b']:.4f}")
    print("  hue      " + "  ".join(f"{k} {v:.1f}%" for k, v in fam.items()))
    print(f"  clipped  black {cl['black']:.2f}%  white {cl['white']:.2f}%")
    print("  bands    luma edges " + " ".join(f"{e:.3f}" for e in bd["edges"]))
    print("    sat    " + "  ".join(f"{v:.3f}" for v in bd["saturation"]))
    print("    warm   " + "  ".join(f"{v:+.3f}" for v in bd["warm"]))
    print("    tint   " + "  ".join(f"{v:+.3f}" for v in bd["tint"]))


def _print_stats(a, payload) -> None:
    if getattr(a, "json", False):
        print(json.dumps(payload, indent=2))
        return
    if "results" in payload:
        for row in payload["results"]:
            _print_stats_block(f"t={row['time']:g}s  {row['key']}", row)
            print()
        return
    _print_stats_block(payload["key"], payload)


# The still image formats `stats` treats as display referred by default
# (round 2 tooling note 1): a JPEG, PNG, TIFF or WebP is, in practice,
# always an export or a screenshot already in display code values, never
# a raw log dump. Anything else (a video container, or a format not on
# this list) keeps today's behaviour: no default is guessed for it.
STILL_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}


def cmd_stats(a):
    """Numeric readback: the same numbers `POST /api/stats` returns, for a
    graded clip frame or a plain reference still, whichever the caller asked
    for. Both go through grade.stats.frame_stats, not two implementations of
    the same handful of numbers: seven of ten bakeoff agents hand wrote this
    formula once each before this command measured anything itself.

    `--json` on a single clip frame or a single `--image` both come back as
    exactly the route's own envelope, `{"key", "size", "stats"}`: every
    number lives one level deeper, under `"stats"`, whichever input this
    command measured (contract G9 friction 3 and 6). `--times` still adds a
    `"results"` list, one `{"time", "key", "size", "stats"}` row per time,
    because there `"time"` is the whole point of asking; a single call has
    no second time to distinguish itself from, so it carries none.

    `--image` on a JPEG, PNG, TIFF or WebP is measured as display referred
    (rec709 in, rec709 working space, i.e. the raw decoded bytes, no CST)
    unless `--input-space` or `--working-space` is given explicitly, in
    which case that flag wins and the still is measured through the real
    convert stage instead (round 2 tooling note 1: the flag used to have no
    effect at all on `--image`, so a log encoded still silently measured as
    if it were already graded). A clip positional that is itself one of
    those still formats is refused rather than quietly run through the
    default Apple Log path (round 2 tooling note 4): use `--image` for it.
    """
    from stats import frame_stats, decode_image              # noqa: PLC0415

    region = getattr(a, "region", None)
    if a.image and a.input:
        raise GradeError(
            f"stats got both a clip ({a.input!r}) and --image "
            f"({a.image!r}); use one or the other, not both")
    if a.image:
        if getattr(a, "times", None):
            raise GradeError("--times measures a clip; --image is one still")
        suffix = Path(a.image).suffix.lower()
        explicit_space = bool(getattr(a, "input_space", None)) or \
            bool(getattr(a, "working_space", None))
        if explicit_space:
            cfg = deepcopy(DEFAULTS)
            if getattr(a, "input_space", None):
                cfg["convert"]["input"] = a.input_space
            if getattr(a, "working_space", None):
                cfg["convert"]["working_space"] = a.working_space
            info = probe(a.image, rotation="auto")
            row = _grade_frame_stats(a, cfg, info, 0.0, region, path=a.image)
            row.pop("time", None)
            row["key"] = Path(a.image).name
        else:
            if suffix in STILL_IMAGE_EXTS:
                print(f"stats --image: measuring {Path(a.image).name} as "
                     f"display referred (rec709); pass --input-space/"
                     f"--working-space if it is log encoded instead.",
                     file=sys.stderr)
            rgb = decode_image(a.image, region=region)
            row = {"key": Path(a.image).name,
                  "size": [int(rgb.shape[1]), int(rgb.shape[0])],
                  "stats": frame_stats(rgb)}
        _print_stats(a, row)
        return

    if not a.input:
        raise GradeError("stats needs either a clip, or --image FILE")
    if Path(a.input).suffix.lower() in STILL_IMAGE_EXTS:
        raise GradeError(
            f"{a.input}: use --image FILE for a still, not the clip "
            f"positional (which always goes through the video grading path)")
    cfg = apply_overrides(load_preset(a.preset), a)
    info = probe(a.input, rotation=cli_rotation(a, cfg))
    if getattr(a, "times", None):
        times = [float(t) for t in a.times.split(",")]
        results = [_grade_frame_stats(a, cfg, info, t, region) for t in times]
        _print_stats(a, {"results": results})
        return
    row = _grade_frame_stats(a, cfg, info, a.time, region)
    # _grade_frame_stats always stamps "time" on, because the --times list
    # above needs it on every row; a single frame has no second row to tell
    # itself apart from, so it is dropped here to match --image's envelope
    # exactly rather than carrying a field the route itself never returns.
    row.pop("time", None)
    _print_stats(a, row)


# --------------------------------------------------------------------------
# sweep: one parameter, many values, one table. Reports, never chooses.
# --------------------------------------------------------------------------

def _set_dotted(cfg: dict, dotted: str, value):
    """Set 'fx.halation.strength' style paths in a config, in place.

    An all-digits path component indexes a list, so 'layers.0.correct.exposure'
    reaches into the layer stack; every other component is a dict key. The
    same convention the test suite's own sweep helper uses, kept as a small
    independent copy here because cinegrade.py does not import grade/tests/.
    """
    node = cfg
    keys = dotted.split(".")
    for k in keys[:-1]:
        node = node[int(k)] if k.isdigit() and isinstance(node, list) else node[k]
    last = keys[-1]
    if last.isdigit() and isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value
    return cfg


def _parse_sweep_value(text: str):
    """A sweep value from the command line: a bool, a number, or a string,
    in that order, so `--values true,false`, `--values 0.5,1.0` and
    `--values aces,filmic,none` all do what they look like they do."""
    text = text.strip()
    low = text.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        f = float(text)
        return int(f) if f.is_integer() and "." not in text and "e" not in low else f
    except ValueError:
        return text


def cmd_sweep(a):
    """One parameter, many values, one stats table. Never chooses: this
    prints what each value measures and leaves the read to whoever asked."""
    from stats import frame_stats                            # noqa: PLC0415

    base = apply_input_space(
        load_preset(a.preset) if a.preset else deepcopy(DEFAULTS), a)
    values = [_parse_sweep_value(v) for v in a.values.split(",") if v.strip() != ""]
    if not values:
        raise GradeError("sweep needs at least one value in --values")

    rows = []
    sheet_paths, sheet_labels, tmp_dir = [], [], None
    if a.sheet:
        tmp_dir = tempfile.mkdtemp(prefix="cinegrade_sweep_")
    try:
        width = getattr(a, "width", None) or 640
        for v in values:
            cfg = _set_dotted(deepcopy(base), a.param, v)
            info = probe(a.input, rotation=cli_rotation(a, cfg))
            rows.append({"param": a.param, "value": v,
                        **_grade_frame_stats(a, cfg, info, a.time,
                                             width=width)})
            if a.sheet:
                png = Path(tmp_dir) / f"sweep_{len(rows)}.png"
                graph = graph_with_mask(cfg, info, tail_extra=["format=rgb24"],
                                        encode_out=False)
                args = ffmpeg_inputs(a.input, cfg, info, a.time)
                args += ["-filter_complex", graph, "-map", "[vout]",
                         "-frames:v", "1", str(png)]
                run(args, a.verbose)
                sheet_paths.append(png)
                sheet_labels.append(f"{a.param}={v}")
        if a.sheet:
            from PIL import Image
            images = [Image.open(p).convert("RGB") for p in sheet_paths]
            sheet = build_contact_sheet(images, sheet_labels, len(images), 1)
            sheet.save(a.sheet)
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    if getattr(a, "json", False):
        print(json.dumps({"param": a.param, "results": rows}, indent=2))
    else:
        print(f"sweep {a.param} on {Path(a.input).name} @ {a.time:g}s")
        print(f"{'value':>12}  {'p5':>6} {'p25':>6} {'p50':>6} {'p75':>6} "
              f"{'p95':>6}  {'sat':>6}  {'r':>6} {'g':>6} {'b':>6}  "
              f"{'clip%':>7}")
        for row in rows:
            s = row["stats"]
            lu, sa, ch, cl = s["luma"], s["saturation"], s["channels"], s["clipped"]
            print(f"{row['value']!s:>12}  {lu['p5']:6.3f} {lu['p25']:6.3f} "
                  f"{lu['p50']:6.3f} {lu['p75']:6.3f} {lu['p95']:6.3f}  "
                  f"{sa['mean']:6.3f}  {ch['r']:6.3f} {ch['g']:6.3f} "
                  f"{ch['b']:6.3f}  {cl['black'] + cl['white']:7.2f}")
        edges = rows[0]["stats"]["bands"]["edges"]
        print("\nbands saturation, luma edges " + " ".join(f"{e:.2f}" for e in edges))
        for row in rows:
            bs = row["stats"]["bands"]["saturation"]
            print(f"{row['value']!s:>12}  " + "  ".join(f"{v:.3f}" for v in bs))
    if a.sheet:
        # --json keeps stdout pure JSON (round 2 tooling note 3: this line
        # used to print after the JSON block and broke a caller's parse),
        # so with --json this goes to stderr instead; without it, stdout is
        # already the plain text table above and this stays alongside it.
        dest = sys.stderr if getattr(a, "json", False) else sys.stdout
        print(f"\nsweep sheet ({len(rows)} up) -> {a.sheet}", file=dest)



# A 5x7 bitmap font, only the glyphs the orient sheet's labels use. This
# ffmpeg build has no drawtext filter (no libfreetype) and the venv has no
# Pillow, so the labels are drawn here, as pixels, rather than not drawn at
# all: an unlabelled five-up contact sheet is a puzzle, not a diagnosis.
_ORIENT_GLYPHS = {
    "0": "01110 10001 10011 10101 11001 10001 01110",
    "1": "00100 01100 00100 00100 00100 00100 01110",
    "2": "01110 10001 00001 00010 00100 01000 11111",
    "3": "11111 00010 00100 00010 00001 10001 01110",
    "4": "00010 00110 01010 10010 11111 00010 00010",
    "5": "11111 10000 11110 00001 00001 10001 01110",
    "6": "00110 01000 10000 11110 10001 10001 01110",
    "7": "11111 00001 00010 00100 01000 01000 01000",
    "8": "01110 10001 10001 01110 10001 10001 01110",
    "9": "01110 10001 10001 01111 00001 00010 01100",
    "a": "00000 00000 01110 00001 01111 10001 01111",
    "o": "00000 00000 01110 10001 10001 10001 01110",
    "t": "01000 01000 11100 01000 01000 01001 00110",
    "u": "00000 00000 10001 10001 10001 10011 01101",
    "x": "00000 00000 10001 01010 00100 01010 10001",
    " ": "00000 00000 00000 00000 00000 00000 00000",
}
_ORIENT_GLYPH_W, _ORIENT_GLYPH_H = 5, 7


def _orient_label_png(text: str, width: int, scale: int, out: Path,
                      verbose=False) -> int:
    """Write a dark strip of the given width with text drawn in white.

    Returns the strip's height so the caller can vstack it onto its panel.
    Written as rawvideo through ffmpeg's png encoder, the same no-Pillow trick
    the test harness uses for its patch sources.
    """
    import numpy as np                                       # noqa: PLC0415
    pad = 2 * scale
    height = _ORIENT_GLYPH_H * scale + 2 * pad
    strip = np.zeros((height, width, 3), dtype=np.uint8)
    strip[:, :] = (24, 24, 28)
    x = pad
    for ch in text.lower():
        rows = _ORIENT_GLYPHS.get(ch)
        if rows is None:
            x += (_ORIENT_GLYPH_W + 1) * scale
            continue
        for ry, row in enumerate(rows.split()):
            for rx, bit in enumerate(row):
                if bit != "1":
                    continue
                x0 = x + rx * scale
                y0 = pad + ry * scale
                if x0 + scale > width:
                    continue
                strip[y0:y0 + scale, x0:x0 + scale] = (235, 235, 235)
        x += (_ORIENT_GLYPH_W + 1) * scale
    run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{width}x{height}", "-i", "-", "-frames:v", "1", str(out)],
        verbose, stdin_bytes=strip.tobytes())
    return height


def _orient_write_sheet(a, raw):
    """--sheet OUT.jpg (round 2 tooling note 8): the four fixed candidate
    rotations (0, 90, 180, 270; not auto, the same four the --json
    `candidates` dict already reports) as one labelled 2x2 sheet, built
    through `build_contact_sheet`, the same PIL contact-sheet code
    `cmd_sheet` uses, rather than the hand-drawn single-row PNG the plain
    (non-json) run below writes to `--output`. Written from its own
    function so it runs the same way whether or not --json is also given:
    the --json branch in cmd_orient returns before that hand-drawn PNG is
    ever built, so this is the only sheet a `--json --sheet` run produces.
    The first label carries the tag and the rotation_tag_suspect flag, so
    the file this sheet came from is identifiable without reading anything
    else.
    """
    try:
        from PIL import Image
    except ImportError as exc:
        raise GradeError(
            "orient --sheet needs Pillow: .venv/bin/pip install pillow") from exc
    tmp = Path(tempfile.mkdtemp(prefix="cinegrade-orient-sheet-"))
    try:
        images = []
        for mode in ("0", "90", "180", "270"):
            info = probe(a.input, rotation=mode)
            out = tmp / f"orient_sheet_{mode}.png"
            args = ["ffmpeg", "-v", "error", "-y"] + rotate_args(mode)
            args += ["-ss", str(a.time), "-i", a.input,
                     "-vf", f"{rotate_prefix(info)}scale=-2:{a.height},format=rgb24",
                     "-frames:v", "1", str(out)]
            run(args, a.verbose)
            images.append(Image.open(out).convert("RGB"))
        suspect = rotation_tag_suspect(
            raw["rotation"], raw["codec"], raw["width"], raw["height"])
        tag = raw["rotation"] or "0"
        labels = [f"0 tag={tag}{' suspect' if suspect else ''}",
                 "90", "180", "270"]
        sheet = build_contact_sheet(images, labels, 2, 2, height=a.height)
        sheet.save(a.sheet)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_orient(a):
    """One contact sheet of every rotation, so a wrong tag is read off it.

    auto is what ffmpeg does on its own (it honours the file's display
    matrix); 0 ignores that tag; 90, 180 and 270 ignore it and then turn the
    picture clockwise. The panel that is upright names the --rotate value to
    pass to every other command.

    --json skips the default (--output) contact sheet entirely and prints
    the tag, the four fixed candidate rotations (their post-rotation width
    and height, the same numbers the printed panel list already gave),
    rotation_tag_suspect and rotation_tag_note, through the identical
    functions GET /api/state's clip list calls, so an agent can branch on
    the same answer the studio would show without decoding an image.

    --sheet OUT.jpg writes a labelled 2x2 sheet of the four candidates,
    independently of --json: it works whether or not --json is also given
    (see _orient_write_sheet).
    """
    want_sheet = bool(getattr(a, "sheet", None))
    want_json = bool(getattr(a, "json", False))

    if want_sheet or want_json:
        raw = probe(a.input, rotation="0")

    if want_sheet:
        _orient_write_sheet(a, raw)

    if want_json:
        candidates = {mode: {"width": probe(a.input, rotation=mode)["width"],
                             "height": probe(a.input, rotation=mode)["height"]}
                     for mode in ("0", "90", "180", "270")}
        # orient renders no grade, so there is nothing here for the rest of
        # the shared flags to change. --input-space is the exception: this is
        # the one command an agent runs BEFORE it knows what the file is, and
        # what a file resolves to is exactly the kind of fact it comes here
        # for, so the resolution is reported (with the flag honoured) next to
        # the rotation tag rather than needing a second command.
        resolved, warnings = resolve_input(
            apply_input_space(deepcopy(DEFAULTS), a), raw)
        out = {
            "tag": raw["rotation"],
            "candidates": candidates,
            "rotation_tag_suspect": rotation_tag_suspect(
                raw["rotation"], raw["codec"], raw["width"], raw["height"]),
            "rotation_tag_note": rotation_tag_note(
                raw["rotation"], raw["codec"], raw["width"], raw["height"]),
            "transfer": raw.get("color_transfer", ""),
            "primaries": raw.get("color_primaries", ""),
            "resolved_input": resolved,
            "source_primaries": source_primaries(raw, resolved),
            "warnings": warnings,
        }
        if want_sheet:
            out["sheet"] = a.sheet
        print(json.dumps(out))
        return

    tmp = Path(tempfile.mkdtemp(prefix="cinegrade-orient-"))
    try:
        panels = []
        for mode in ROTATIONS:
            info = probe(a.input, rotation=mode)
            out = tmp / f"orient_{mode}.png"
            args = ["ffmpeg", "-v", "error", "-y"] + rotate_args(mode)
            args += ["-ss", str(a.time), "-i", a.input,
                     "-vf", f"{rotate_prefix(info)}scale=-2:{a.height},format=rgb24",
                     "-frames:v", "1", str(out)]
            run(args, a.verbose)
            shot = probe(str(out))
            label = f"{mode} {info['width']}x{info['height']}"
            panels.append({"mode": mode, "path": out, "label": label,
                           "width": shot["width"]})
            print(f"  --rotate {mode:<5} {info['width']}x{info['height']}")

        # One scale for every label, chosen from the narrowest panel, so no
        # label runs off the end of the picture it belongs to.
        longest = max(len(p["label"]) for p in panels)
        narrowest = min(p["width"] for p in panels)
        scale = max(1, min(5, (narrowest - 8) // (longest * (_ORIENT_GLYPH_W + 1))))

        args = ["ffmpeg", "-v", "error", "-y"]
        segs, cols = [], []
        for i, p in enumerate(panels):
            strip = tmp / f"label_{p['mode']}.png"
            _orient_label_png(p["label"], p["width"], scale, strip, a.verbose)
            args += ["-i", str(strip), "-i", str(p["path"])]
            segs.append(f"[{2 * i}:v][{2 * i + 1}:v]vstack[c{i}]")
            cols.append(f"[c{i}]")
        segs.append("".join(cols) + f"hstack=inputs={len(panels)},format=rgb24[sheet]")
        args += ["-filter_complex", ";".join(segs), "-map", "[sheet]",
                 "-frames:v", "1", a.output]
        run(args, a.verbose)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"left to right: {', '.join(ROTATIONS)}  -> {a.output}")
    rot = probe(a.input)["rotation"]
    if rot:
        print(f"note: this clip carries a {rot} degree display matrix, which "
              f"is what the auto panel honours. If a different panel is the "
              f"upright one, pass its --rotate value.")
    if a.open:
        subprocess.run(["open", "-a", "Preview", a.output])


def apply_input_space(cfg, a):
    """--input-space, on its own, because two commands need only this one.

    `compare` builds a fresh config per panel and `sweep` builds one per swept
    value, and neither runs the rest of apply_overrides today. Widening what
    those two honour would change what they already render, so they call this
    instead: the input transform is a statement about the FILE, and a file
    does not become a different file from one panel to the next.
    """
    value = getattr(a, "input_space", None)
    if value:
        cfg["convert"]["input"] = value
    return cfg


def apply_overrides(cfg, a):
    if getattr(a, "look", None):
        cfg["look"]["lut"] = a.look
    if getattr(a, "exposure", None) is not None:
        cfg["convert"]["exposure"] = a.exposure
    if getattr(a, "tonemap", None):
        cfg["convert"]["tonemap"] = a.tonemap
    apply_input_space(cfg, a)
    if getattr(a, "working_space", None):
        cfg["convert"]["working_space"] = a.working_space
    for k in ("contrast", "saturation", "temperature", "tint"):
        v = getattr(a, k, None)
        if v is not None:
            cfg["primaries"][k] = v
    return cfg


def run(args, verbose=False, stdin_bytes=None):
    """Run one ffmpeg command, raising on a non-zero exit.

    stdin_bytes feeds the process raw bytes (the orient sheet's label strips
    go in as rawvideo); the text pipe is only used when there is no binary
    input, since the two cannot be mixed on one call.
    """
    if verbose:
        print(" ".join(shlex.quote(x) for x in args), file=sys.stderr)
    if stdin_bytes is None:
        r = subprocess.run(args, capture_output=True, text=True)
        err = r.stderr
    else:
        r = subprocess.run(args, input=stdin_bytes, capture_output=True)
        err = r.stderr.decode("utf-8", "replace")
    if r.returncode != 0:
        raise GradeError(f"ffmpeg failed ({r.returncode})\n{err[-4000:]}")
    return r


def run_render(args, verbose=False, total=None):
    """Run one ffmpeg render, streaming "render Xs of Ys" progress lines to
    stderr as it goes (round 2 tooling note 9), so a poller watching this
    process can tell alive-but-slow from stuck without parsing ffmpeg's own
    -stats output. `args` is the same argument list `run()` would take
    (ending in the real output path); this inserts `-progress pipe:1
    -nostats` itself, ahead of the output path, the same flags
    studio/server.py's own render job worker already puts on its ffmpeg
    call, so the two read the identical `out_time_us=` stream.

    stdout carries none of this: ffmpeg's own progress stream is read from
    its stdout pipe and never echoed there, only summarised to stderr, so
    the caller's own final line (cmd_render's "rendered ... -> OUT",
    printed by the caller after this returns) stays the only thing on
    stdout, the same as every other subcommand.

    One line per integer second of rendered OUTPUT (not wall clock time:
    out_time_us is ffmpeg's own encoded-so-far position), deduplicated so a
    fast source does not spam a line per progress tick.
    """
    if verbose:
        print(" ".join(shlex.quote(x) for x in args), file=sys.stderr)
    progress_args = args[:-1] + ["-progress", "pipe:1", "-nostats", args[-1]]
    proc = subprocess.Popen(progress_args, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    last_shown = None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("out_time_us="):
                continue
            try:
                secs = int(line.split("=", 1)[1]) / 1e6
            except ValueError:
                continue
            shown = int(secs)
            if total and shown != last_shown:
                print(f"render {secs:.1f}s of {total:.1f}s", file=sys.stderr)
                last_shown = shown
    finally:
        proc.wait()
    err = proc.stderr.read()
    if proc.returncode != 0:
        raise GradeError(f"ffmpeg failed ({proc.returncode})\n{err[-4000:]}")
    return subprocess.CompletedProcess(progress_args, proc.returncode, "", err)


# The studio server's default port. Only used to talk to an already running
# one, so there is nothing to bind or configure here.
STUDIO_PORT = 7431


def resolve_by(a) -> str:
    """Who a server write is FROM, decided the same way by every command here.

    `--by` wins. Failing that, the `CINEGRADE_AGENT` environment variable, so
    a fleet of agents can each export their own name once and never type
    `--by` on every call. Failing that, `cli`. The server does not add an
    `agent:` prefix by itself (studio/projects.py stores whatever label it is
    given), so an agent that wants to read as one spells it out itself, either
    on the command line or in the environment variable: `agent:colorbot-3`.
    """
    by = getattr(a, "by", None)
    if by:
        return str(by)
    agent = resolve_agent(a)
    if agent:
        # A named caller signs as itself. The server decides the author for
        # real (it writes agent:NAME whatever the body says), so this only
        # makes the label the tab echoes on match what the history will say.
        return f"agent:{agent}"
    env = os.environ.get("CINEGRADE_AGENT")
    if env:
        return env
    return "cli"


def resolve_agent(a) -> str:
    """The agent name this shell calls as, or "" for the plain local caller.

    `--agent` wins, then the STUDIO_AGENT environment variable, so a lane can
    export its name once and never type it again. Nothing is invented: with
    neither set the caller is user 0, exactly as it was before contract G2.
    """
    name = getattr(a, "agent", None)
    if name:
        return str(name).strip()
    return os.environ.get("STUDIO_AGENT", "").strip()


def studio_headers(a) -> dict:
    """The identity headers every studio call from this CLI carries."""
    headers = {}
    agent = resolve_agent(a)
    if agent:
        headers["X-Studio-Agent"] = agent
    attach = getattr(a, "attach", None)
    if attach is not None and str(attach).strip() != "":
        headers["X-Studio-Attach"] = str(attach).strip()
    return headers


def add_server_flags(p) -> None:
    """--port, --url, --agent and --attach: the four flags shared by every
    subcommand that talks to a running studio server (contract G2).

    No subcommand hardcodes 7431 as an argparse default any more: `--port`
    defaults to None here, so "nothing was said on this call" is a value
    `resolve_server` below can see, which is what lets `require_server` tell
    an agent's own choice of server apart from silently inheriting the
    human's (contract G9 friction 8: session/whoami/project used to default
    onto the founder's live studio with no way to redirect them).
    """
    p.add_argument("--port", type=int, default=None,
                   help="talk to the studio server on 127.0.0.1 at this "
                        "port. Also settable as STUDIO_PORT (--port wins "
                        "when both are given). A human gets the 7431 "
                        "default with neither; an agent (--agent or "
                        "STUDIO_AGENT) must set one of --port, --url, "
                        "STUDIO_PORT or STUDIO_URL")
    p.add_argument("--url",
                   help="talk to the studio server at this base URL "
                        "instead of building one from --port. Also "
                        "settable as STUDIO_URL; a URL always wins over a "
                        "port, from either source")
    p.add_argument("--agent", metavar="NAME",
                   help="call as agent NAME, so this shell gets its own "
                        "live session and open project instead of sharing "
                        "the browser tab's. Also settable as STUDIO_AGENT")
    p.add_argument("--attach", metavar="USER",
                   help="act on USER's session/project instead of your "
                        "own: 0 for the local browser tab, or an account "
                        "name. Needs a server with logins off")


def resolve_server(a) -> tuple[str, bool]:
    """The base URL this CLI call talks to, and whether the caller named it.

    A URL beats a port, and either beats the hardcoded 7431 default: `--url`
    or `STUDIO_URL` wins outright (the flag first); failing that, `--port`
    or `STUDIO_PORT` (again the flag first); failing that, port 7431, the
    founder's own live studio (this CLI never binds anything of its own, it
    only ever talks to a server that is already running).

    `given` is False only in that last, nothing-was-said case. That is what
    lets `require_server` below tell a human's silent default apart from an
    agent that forgot to name its server.
    """
    url = (getattr(a, "url", None) or os.environ.get("STUDIO_URL") or "").strip()
    if url:
        return url.rstrip("/"), True
    port = getattr(a, "port", None)
    if port is not None:
        return f"http://127.0.0.1:{port}", True
    env_port = os.environ.get("STUDIO_PORT", "").strip()
    if env_port:
        return f"http://127.0.0.1:{env_port}", True
    return f"http://127.0.0.1:{STUDIO_PORT}", False


def require_server(a) -> str:
    """resolve_server(a), refused before any request goes out when an agent
    named itself but never named a server.

    A human calling with no --agent and no STUDIO_AGENT still gets today's
    silent 7431 default, unchanged: nothing about a person's own use of this
    CLI is any different. An agent (--agent given, or STUDIO_AGENT set) gets
    that same silent default ONLY when it also named a server itself; the
    whole point of this check is that guessing 7431 for an unnamed agent
    means guessing the founder's live studio, which is exactly the trap
    contract G2's own integration review found and asked to close.
    """
    base, given = resolve_server(a)
    if resolve_agent(a) and not given:
        raise GradeError(
            "an agent must name its server: pass --port or --url, or set "
            "STUDIO_PORT or STUDIO_URL; the 7431 default is a human's live "
            "studio, not a guess for an agent to inherit")
    return base


def _studio_call(base: str, path: str, method: str = "GET",
                 payload: dict | None = None,
                 headers: dict | None = None) -> dict:
    """One HTTP round trip to a running studio server at `base` (a full
    "http://host:port", from resolve_server/require_server above).

    Same reasoning as cmd_session below: plain HTTP and nothing else,
    against a server that is already running, so this adds no listener, no
    port and no remote surface of its own. A 4xx from the server (a bad
    clip name, no project open, an unknown commit id) comes back as a
    message, not a traceback.
    """
    import urllib.error                                     # noqa: PLC0415
    import urllib.request                                   # noqa: PLC0415

    url = f"{base}/api/{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    sent = dict(headers or {})
    if data is not None:
        sent["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=sent, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:400]
        raise GradeError(f"studio refused it: {detail}") from exc
    except urllib.error.URLError as exc:
        raise GradeError(
            f"no studio server answering at {base} ({exc.reason}). "
            f"Start one with ./studio.sh, or pass --port/--url.") from exc


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

    base = require_server(a)
    url = f"{base}/api/session"
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
        patch.setdefault("by", resolve_by(a))
        if getattr(a, "message", None):
            patch.setdefault("message", a.message)
        if getattr(a, "if_rev", None) is not None:
            patch.setdefault("if_rev", int(a.if_rev))
        patch["replace"] = bool(a.replace)
        body = json.dumps(patch).encode()

    headers = studio_headers(a)
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:400]
        raise GradeError(f"studio refused it: {detail}") from exc
    except urllib.error.URLError as exc:
        raise GradeError(
            f"no studio server answering at {base} ({exc.reason}). "
            f"Start one with ./studio.sh, or pass --port/--url.") from exc

    if out.get("config") is None:
        print("the studio is running but no browser has published a config yet; "
              "open the page once, then try again", file=sys.stderr)
    print(json.dumps(out, indent=2))


def cmd_whoami(a):
    """Who a write from this shell counts as, and what project is open.

    `user` and `auth` and `project` are the server's own answer: an account
    name (or none, with logins off), whether logins are on at all, and the
    content key of whatever project this account currently has open. `by` is
    NOT read back from the server: the whoami route has no write to attach it
    to, so it always reports its own quiet default (`cli`) regardless of what
    a caller might send. What actually signs a commit is decided per write, by
    resolve_by() above, the same way for `session patch` and every `project`
    command, so that is what is shown here: the account name when logins are
    on (nothing overrides that, ever), otherwise --by, else CINEGRADE_AGENT,
    else cli.

    A project key alone is an unreadable hash, so when one is open this
    makes one more call, to GET /api/project, and shows the clip name next
    to it (contract G9 friction 6: proving "these two agents have different
    clips open" used to need a second call from the agent itself; the
    second call still happens, it now happens in here so the agent's own
    turn is one command).
    """
    base = require_server(a)
    out = _studio_call(base, "whoami", headers=studio_headers(a))
    project_clip = None
    if out.get("project"):
        try:
            proj = _studio_call(base, "project", headers=studio_headers(a))
            project_clip = proj.get("name")
        except GradeError:
            project_clip = None
    effective_by = out.get("user") if (out.get("auth") and out.get("user")) \
        else resolve_by(a)
    if a.json:
        shown = dict(out)
        shown["by"] = effective_by
        shown["project_clip"] = project_clip
        print(json.dumps(shown, indent=2))
        return
    caller = out.get("caller") or {}
    print(f"user     {out.get('user') or '(no account, logins are off)'}")
    print(f"by       {effective_by}")
    print(f"logins   {'on' if out.get('auth') else 'off'}")
    # Contract G2: who the server thinks is calling, which is not the same
    # question as who signs a write. `caller` is the row this shell's session
    # and open project belong to; `attached` says it is acting as somebody
    # else while still signing as itself.
    print(f"caller   {caller.get('name') or 'local'} "
          f"(id {caller.get('id', 0)})")
    if caller.get("attached_to") is not None:
        print(f"attached {caller['attached_to']}")
    project_line = out.get("project") or "(none open)"
    if project_clip:
        project_line += f"  ({project_clip})"
    print(f"project  {project_line}")


def _relative_time(ts: float) -> str:
    """A short, human age for a commit timestamp. No exact clock needed."""
    delta = max(0.0, time.time() - float(ts or 0.0))
    if delta < 5:
        return "just now"
    steps = (
        (60, 1, "second"), (3600, 60, "minute"), (86400, 3600, "hour"),
        (604800, 86400, "day"), (2629800, 604800, "week"),
        (31557600, 2629800, "month"),
    )
    for limit, unit_seconds, name in steps:
        if delta < limit:
            value = int(delta // unit_seconds)
            return f"{value} {name}{'' if value == 1 else 's'} ago"
    value = int(delta // 31557600)
    return f"{value} year{'' if value == 1 else 's'} ago"


def _print_project_state(out: dict) -> None:
    """The fields `project show`, `open`, `rotate`, `undo` and friends share.

    Same order every time: key, clip, branch, head, rotation, time, preset,
    branches, so a script tailing this output always finds a field on the
    same line.
    """
    if not out.get("open"):
        print(out.get("note") or "no project is open")
        return
    print(f"key       {out.get('key')}")
    print(f"clip      {out.get('name')}")
    print(f"branch    {out.get('branch')}")
    print(f"head      {out.get('head')}")
    print(f"rotation  {out.get('rotation')}")
    if out.get("time") is not None:
        print(f"time      {float(out['time']):.2f}s")
    print(f"preset    {out.get('preset') or '(none)'}")
    branches = out.get("branches") or []
    if branches:
        bits = []
        for b in branches:
            tag = "current" if b.get("is_current") else "other"
            bits.append(f"{b.get('name')} (tip {b.get('short')}, {tag})")
        print(f"branches  {', '.join(bits)}")
    if "moved" in out:
        print(f"moved     {'yes' if out['moved'] else 'no'}")
    if out.get("note"):
        print(f"note      {out['note']}")
    _print_saved_picks(out)


def _print_saved_picks(out: dict) -> None:
    """`extras.match_crops` (contract C7): the rectangles saved per reference.

    Shaped `{REF_NAME: {"ref": [x0, y0, x1, y1], "frame": [...]}}`, either key
    optional. Fractions of the image, printed to two decimals, no dashes or
    arrows: `POST /api/match` inherits these when a request leaves ref_crop
    or frame_crop out, so an agent reading them here knows what a plain match
    call is actually going to measure.
    """
    crops = ((out.get("extras") or {}).get("match_crops") or {})
    if not crops:
        return
    print("saved picks")
    for ref in sorted(crops):
        sides = crops[ref] or {}
        bits = []
        for side in ("ref", "frame"):
            box = sides.get(side)
            if box and len(box) == 4:
                x0, y0, x1, y1 = (float(v) for v in box)
                bits.append(f"{side} {x0:.2f} {y0:.2f} {x1:.2f} {y1:.2f}")
        if bits:
            print(f"  {ref}  {', '.join(bits)}")


def _project_output(a, out: dict, header: str | None) -> None:
    if a.json:
        print(json.dumps(out, indent=2))
        return
    if header:
        print(header)
    _print_project_state(out)


def _fork_points(commits: list) -> dict:
    """Commit id to the list of branch names that split off it.

    A commit is a fork point when some OTHER commit names it as parent but
    carries a different branch: that is the moment `project fork` happened.
    Only commits inside the fetched page can be seen this way, which is fine:
    a fork far outside the window a human asked to see is not what they are
    looking at anyway.
    """
    by_id = {c["id"]: c for c in commits}
    out: dict = {}
    for c in commits:
        parent = c.get("parent")
        if parent and parent in by_id and by_id[parent]["branch"] != c["branch"]:
            out.setdefault(parent, []).append(c["branch"])
    return out


def _print_log(out: dict, showed_all: bool) -> None:
    if not out.get("total"):
        print("no commits yet")
        return
    print(f"project {out.get('key')}  {out.get('name')}")
    branches = out.get("branches") or []
    if branches:
        bits = []
        for b in branches:
            tag = "current" if b.get("is_current") else "other"
            bits.append(f"{b.get('name')} (tip {b.get('short')}, "
                        f"{b.get('commits')} commits, {tag})")
        print("branches  " + "; ".join(bits))
    print()
    commits = out.get("commits") or []
    forks = _fork_points(commits)
    for c in commits:
        marks = []
        if c.get("is_head"):
            marks.append("HEAD")
        if c.get("is_tip") and not c.get("is_head"):
            marks.append("tip")
        tag = " ".join(marks)
        when = _relative_time(c.get("ts"))
        print(f"{c['short']:<8} {c['branch']:<10} {c['author']:<18} "
              f"{when:<14} {tag:<5} {c['message']}")
        for child_branch in forks.get(c["id"], ()):
            print(f"         forked into {child_branch} from here")
    seen = len(commits)
    if showed_all or seen >= out.get("total", seen):
        print(f"\n{out.get('total')} commits total")
    else:
        print(f"\n{seen} of {out.get('total')} commits shown "
              f"(pass --all or a larger --limit to see the rest)")


def cmd_project(a):
    """Everything in the `project` command group: one function, one dispatch.

    Plain HTTP to the studio port, the same shape cmd_session uses, so a
    project command is exactly as safe to run alongside a browser tab open on
    the same clip: it is the same server, the same routes, the same commits.
    """
    base = require_server(a)
    by = resolve_by(a)
    cmd = a.project_cmd
    # Contract G2: --agent and --attach ride on every call in this group, so
    # an agent's open project is its own and `--attach 0` works the whole way
    # through rather than on the first call only.
    hdr = studio_headers(a)

    if cmd == "open":
        out = _studio_call(base, "project/open", "POST",
                           {"clip": a.clip, "by": by}, headers=hdr)
        if a.rotation:
            out = _studio_call(base, "project/rotation", "POST",
                               {"rotation": a.rotation, "by": by}, headers=hdr)
        _project_output(a, out, f"opened {a.clip}")
        return

    if cmd == "show":
        out = _studio_call(base, "project", "GET", headers=hdr)
        _project_output(a, out, None)
        return

    if cmd == "log":
        limit = 2000 if a.all else max(1, int(a.limit))
        out = _studio_call(base, f"project/log?limit={limit}", "GET",
                           headers=hdr)
        if a.json:
            print(json.dumps(out, indent=2))
        else:
            _print_log(out, a.all)
        return

    if cmd == "checkout":
        out = _studio_call(base, "project/checkout", "POST",
                           {"commit": a.commit, "by": by}, headers=hdr)
        _project_output(a, out, f"checked out {a.commit}")
        return

    if cmd == "fork":
        payload = {"by": by}
        if a.name:
            payload["name"] = a.name
        if a.from_commit:
            payload["commit"] = a.from_commit
        out = _studio_call(base, "project/fork", "POST", payload, headers=hdr)
        _project_output(a, out, f"forked {out.get('branch', '')}".rstrip())
        return

    if cmd == "undo":
        out = _studio_call(base, "project/undo", "POST", {"by": by},
                           headers=hdr)
        _project_output(a, out, "undo")
        return

    if cmd == "redo":
        out = _studio_call(base, "project/redo", "POST", {"by": by},
                           headers=hdr)
        _project_output(a, out, "redo")
        return

    if cmd == "rotate":
        out = _studio_call(base, "project/rotation", "POST",
                           {"rotation": a.rotation, "by": by}, headers=hdr)
        _project_output(a, out, f"rotation set to {a.rotation}")
        return

    if cmd == "time":
        out = _studio_call(base, "project/time", "POST",
                           {"time": a.seconds, "by": by}, headers=hdr)
        _project_output(a, out, f"time set to {a.seconds:g}s")
        return

    raise GradeError(f"unknown project command {cmd!r}")


# --------------------------------------------------------------------------
# match, preset and grade: thin wrappers over the server's own routes
#
# No local equivalent exists for any of these (the fit in match, the shared
# preset store, and the per clip saved grade all live on the server), so
# founder decision 4 ("the CLI is the primary agent surface, the server
# mirrors it") runs the other way here on purpose: these three commands
# exist so an agent never has to fall back to curl the way one bakeoff
# lane did (contract G9 friction 5).
# --------------------------------------------------------------------------

def cmd_match(a):
    """POST /api/match: fit a look cube from a reference image toward a clip
    frame, the same call studio/tools/grade_client.py's Studio.match() makes.

    Crops are always sent explicitly, the whole frame when neither
    --ref-crop nor --frame-crop is given: an absent crop key reads to the
    server as "use whatever this project last had saved for this
    reference", which is a browser tab's rectangle this CLI cannot see
    (contract G9 leftover 3), so this command never leaves the key out.

    `--rotate` (round 2 tooling note 6) is sent as the top level `rotation`
    field the server's `effective_rotation()` reads first, the same field
    `grade_client.Studio.match()` sends when it was constructed with a
    `rotation`. With no `--rotate`, this falls back to `cfg["rotation"]`
    (a non-auto rotation saved on the --preset/config given) and then to
    "auto", exactly the fallback order `cli_rotation` already gives every
    other subcommand that measures a frame off local disk; match is the one
    command that measures over HTTP instead, so it could not reuse that
    fallback until it had a --rotate flag of its own to feed it.
    """
    base = require_server(a)
    hdr = studio_headers(a)
    cfg = load_preset(a.preset) if a.preset else {}
    ref_crop = list(a.ref_crop) if a.ref_crop else [0.0, 0.0, 1.0, 1.0]
    frame_crop = list(a.frame_crop) if a.frame_crop else [0.0, 0.0, 1.0, 1.0]
    payload = {
        "ref": a.ref, "clip": a.clip, "time": a.time, "config": cfg,
        "method": a.method, "strength": a.strength,
        "luma_preserve": a.luma_preserve,
        "ref_crop": ref_crop, "frame_crop": frame_crop,
        "rotation": cli_rotation(a, cfg),
    }
    if a.name:
        payload["name"] = a.name
    if a.out_dir:
        payload["out_dir"] = a.out_dir
    out = _studio_call(base, "match", "POST", payload, headers=hdr)
    if a.json:
        print(json.dumps(out, indent=2))
        return
    gain = (out.get("distance") or {}).get("gain_colour_pct")
    line = (f"match {out.get('name')} -> {out.get('lut')}  "
           f"ok={out.get('ok')} recommended={out.get('recommended')}")
    if gain is not None:
        line += f" gain_colour_pct={gain}"
    print(line)
    for w in out.get("warnings") or []:
        print(f"  ! {w}")


def cmd_preset(a):
    """`preset save` (POST /api/preset) and `preset load` (GET /api/preset):
    the shared, named grade store every account and agent reads and writes
    (contract G2: always users/0/presets with logins off, so an agent's
    save shows up for the person at the keyboard and the other way round).
    """
    import urllib.parse                                       # noqa: PLC0415

    base = require_server(a)
    hdr = studio_headers(a)
    if a.preset_cmd == "save":
        cfg = load_preset(a.preset)
        out = _studio_call(base, "preset", "POST",
                           {"name": a.name, "config": cfg,
                            "comment": a.comment or ""}, headers=hdr)
        if a.json:
            print(json.dumps(out, indent=2))
        else:
            print(f"saved preset {out.get('saved')} -> {out.get('path')}")
        return
    if a.preset_cmd == "load":
        q = f"name={urllib.parse.quote(a.name)}"
        if a.expand:
            q += "&expand=true"
        out = _studio_call(base, f"preset?{q}", headers=hdr)
        if a.output:
            Path(a.output).write_text(json.dumps(out.get("config"), indent=2))
        if a.json:
            print(json.dumps(out, indent=2))
            return
        print(f"preset   {out.get('name')}")
        print(f"comment  {out.get('comment') or '(none)'}")
        print(f"expanded {bool(out.get('expanded'))}")
        if a.output:
            print(f"config   -> {a.output}")
        else:
            print(json.dumps(out.get("config"), indent=2))
        return
    raise GradeError(f"unknown preset command {a.preset_cmd!r}")


def cmd_grade(a):
    """`grade save` (PUT /api/grade) and `grade load` (GET /api/grade): the
    per clip saved grade, contract C3, distinct from `preset` above (a
    shared, named config) and from `session patch` (the LIVE config a
    browser tab is watching). This route does not wake a watching tab; use
    `session patch` for a change meant to be seen live.
    """
    import urllib.parse                                       # noqa: PLC0415

    base = require_server(a)
    hdr = studio_headers(a)
    if a.grade_cmd == "save":
        cfg = load_preset(a.preset)
        payload = {"clip": a.clip, "config": cfg}
        if a.message:
            payload["message"] = a.message
        out = _studio_call(base, "grade", "PUT", payload, headers=hdr)
        if a.json:
            print(json.dumps(out, indent=2))
        else:
            print(f"saved grade for {a.clip}: key {out.get('key')}, "
                 f"head {out.get('head')}")
        return
    if a.grade_cmd == "load":
        out = _studio_call(base, f"grade?clip={urllib.parse.quote(a.clip)}",
                           headers=hdr)
        if a.output and out.get("config") is not None:
            Path(a.output).write_text(json.dumps(out["config"], indent=2))
        if a.json:
            print(json.dumps(out, indent=2))
            return
        if not out.get("exists"):
            print(f"no saved grade for {a.clip}")
            return
        print(f"grade    {a.clip}")
        print(f"key      {out.get('key')}")
        if out.get("head"):
            print(f"head     {out['head']}")
        if a.output:
            print(f"config   -> {a.output}")
        else:
            print(json.dumps(out.get("config"), indent=2))
        return
    raise GradeError(f"unknown grade command {a.grade_cmd!r}")


# --------------------------------------------------------------------------
# sheet: a labelled comparison image out of stills, frames or both
# --------------------------------------------------------------------------

_SHEET_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
_SHEET_GAP = 10
_SHEET_BG = (18, 18, 18)
_SHEET_FG = (230, 230, 230)
_SHEET_LABEL_PAD = 6
_SHEET_DEFAULT_HEIGHT = 480


def _parse_grid(spec: str, n: int):
    """'COLSxROWS' -> (cols, rows), the same order ImageMagick's -tile uses."""
    try:
        cols_s, rows_s = spec.lower().split("x")
        cols, rows = int(cols_s), int(rows_s)
    except ValueError:
        raise GradeError(f"--grid wants COLSxROWS, e.g. 2x3, got {spec!r}") from None
    if cols < 1 or rows < 1:
        raise GradeError(f"--grid needs at least one column and one row, "
                         f"got {spec!r}")
    if n > cols * rows:
        raise GradeError(f"--grid {spec} has {cols * rows} slots for {n} inputs")
    return cols, rows


def _sheet_load(path: Path, t: float, tmp_dir: Path, verbose: bool):
    """One PIL image for a sheet panel: opened directly, or decoded from a
    video through one bounded ffmpeg call at time `t`."""
    from PIL import Image
    if path.suffix.lower() in _SHEET_IMAGE_EXTS:
        return Image.open(path).convert("RGB")
    frame = tmp_dir / f"{path.stem}.png"
    run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", str(path),
        "-t", "1", "-frames:v", "1", str(frame)], verbose)
    return Image.open(frame).convert("RGB")


def _sheet_region_crop(img, region):
    """Crop one already-loaded sheet panel to `--region` (round 2 tooling
    note 7): four fractions of THAT PANEL'S OWN size, the same meaning
    `still --region` gives a graded frame, applied after every panel is
    loaded rather than before any of them are decoded. Mixed aspect inputs
    each crop to their own frame this way, not to one shared pixel box, and
    no --zoom is needed: a sheet panel is already scaled to the sheet's own
    shared height by build_contact_sheet, so there is nothing a zoom would
    do that picking the region tighter does not already do."""
    reg = normalise_region(region)
    x, y, w, h = region_pixels(reg, {"width": img.width, "height": img.height})
    return img.crop((x, y, x + w, y + h))


def _label_metrics(font):
    """One label strip height for every panel, from a reference string with
    both an ascender and a descender, so real labels never disagree with it
    and every panel ends up the exact same total height."""
    from PIL import Image, ImageDraw
    d = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    bbox = d.textbbox((0, 0), "Ag", font=font)
    return (bbox[3] - bbox[1]) + 2 * _SHEET_LABEL_PAD, bbox[1]


def _sheet_panel(img, label, height, font, label_h, top_offset):
    from PIL import Image, ImageDraw
    w = max(1, round(img.width * height / img.height))
    img = img.resize((w, height), Image.LANCZOS)
    panel = Image.new("RGB", (w, label_h + height), _SHEET_BG)
    d = ImageDraw.Draw(panel)
    d.text((_SHEET_LABEL_PAD, _SHEET_LABEL_PAD - top_offset), label,
           fill=_SHEET_FG, font=font)
    panel.paste(img, (0, label_h))
    return panel


def build_contact_sheet(images, labels, cols, rows, height=None, width=None):
    """Scale every input to one common height (never a common width), pad
    to the tallest row and widest row, mixed aspect inputs never fail.

    `height` fixes the shared panel height directly. `width` instead fixes
    the whole sheet's width: the shared height is solved from whichever row
    of scaled panels would add up to the widest total, so nothing overflows.
    With neither, panels default to _SHEET_DEFAULT_HEIGHT.
    """
    from PIL import Image, ImageFont

    rows_of = [list(zip(images[r * cols:(r + 1) * cols],
                        labels[r * cols:(r + 1) * cols]))
              for r in range(rows)]
    rows_of = [row for row in rows_of if row]

    if height:
        target_h = int(height)
    elif width:
        best = 0.0
        for row in rows_of:
            aspect_sum = sum(im.width / im.height for im, _ in row)
            gaps = _SHEET_GAP * max(0, len(row) - 1)
            if aspect_sum:
                best = max(best, (width - gaps) / aspect_sum)
        target_h = max(2, int(round(best))) if best > 0 else _SHEET_DEFAULT_HEIGHT
    else:
        target_h = _SHEET_DEFAULT_HEIGHT

    font = ImageFont.load_default()
    label_h, top_offset = _label_metrics(font)

    panel_rows = [[_sheet_panel(im, lb, target_h, font, label_h, top_offset)
                  for im, lb in row] for row in rows_of]

    row_heights = [max((p.height for p in row), default=0) for row in panel_rows]
    row_widths = [sum(p.width for p in row) + _SHEET_GAP * max(0, len(row) - 1)
                 for row in panel_rows]
    canvas_w = max(row_widths, default=0)
    if width and width > canvas_w:
        canvas_w = int(width)
    canvas_h = sum(row_heights) + _SHEET_GAP * max(0, len(row_heights) - 1)

    sheet = Image.new("RGB", (max(1, canvas_w), max(1, canvas_h)), _SHEET_BG)
    y = 0
    for row, rh in zip(panel_rows, row_heights):
        x = 0
        for p in row:
            sheet.paste(p, (x, y))
            x += p.width + _SHEET_GAP
        y += rh + _SHEET_GAP
    return sheet


def cmd_sheet(a):
    try:
        import PIL  # noqa: F401
    except ImportError as exc:
        raise GradeError("sheet needs Pillow: .venv/bin/pip install pillow") from exc

    paths = [Path(p) for p in a.inputs]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        raise GradeError(f"sheet input not found: {', '.join(missing)}")

    cols, rows = (len(paths), 1) if not a.grid else _parse_grid(a.grid, len(paths))

    if a.labels:
        labels = [s.strip() for s in a.labels.split(",")]
        if len(labels) != len(paths):
            raise GradeError(
                f"--labels has {len(labels)} names for {len(paths)} inputs")
    else:
        labels = [p.stem for p in paths]

    with tempfile.TemporaryDirectory(prefix="cinegrade_sheet_") as tmp_s:
        tmp = Path(tmp_s)
        images = [_sheet_load(p, a.time, tmp, a.verbose) for p in paths]
        if getattr(a, "region", None):
            images = [_sheet_region_crop(im, a.region) for im in images]
        sheet = build_contact_sheet(images, labels, cols, rows,
                                    height=a.height, width=a.width)
        sheet.save(a.output)
    print(f"sheet ({len(paths)} up, {cols}x{rows}) -> {a.output}")


# --------------------------------------------------------------------------
# docs: print one section of studio/README.md by heading name
# --------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def _readme_headings(text: str):
    """[(line_index, level, title), ...] for every ATX heading, skipping
    anything inside a fenced code block (a shell comment starting with
    `#` inside a ```bash fence is not a heading)."""
    headings = []
    in_fence = False
    fence = None
    for i, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if not in_fence:
                in_fence, fence = True, marker
            elif marker == fence:
                in_fence, fence = False, None
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if m:
            headings.append((i, len(m.group(1)), m.group(2).strip()))
    return headings


def cmd_docs(a):
    readme = ROOT.parent / "studio" / "README.md"
    if not readme.exists():
        raise GradeError(f"missing {readme}")
    lines = readme.read_text().splitlines()
    headings = _readme_headings("\n".join(lines))
    if not headings:
        raise GradeError(f"no headings found in {readme}")

    def list_headings():
        for _, level, title in headings:
            print(f"{'  ' * (level - 1)}{title}")

    if a.list or not a.section:
        list_headings()
        return

    target = a.section.strip().lower()
    match = next((h for h in headings if h[2].lower() == target), None)
    if match is None:
        print(f"no section named {a.section!r} in {readme}; sections:")
        list_headings()
        return

    idx, level, _title = match
    end = len(lines)
    for j, lvl, _ in headings:
        if j > idx and lvl <= level:
            end = j
            break
    print("\n".join(lines[idx:end]).rstrip())


def _fix_negative_values_flag(argv: list[str]) -> list[str]:
    """`sweep --values -0.1,0,0.1` (round 2 tooling note 3): argparse decides
    whether a token is an option before `--values`'s own action ever runs,
    and `-0.1,0,0.1` starts with a dash, so it is refused as an unrecognised
    flag rather than read as the value. `--values=-0.1,0,0.1` (one token,
    the `=` form) already works today; this rewrites the two token spelling
    into that one before argparse ever sees it, so both work identically and
    a caller never has to know the difference or quote anything.

    Only `--values` is touched, and only when the very next token starts
    with a minus followed by a digit or a decimal point: every other flag,
    and a `--values` whose first number is not negative, passes through
    unchanged.
    """
    out = []
    i = 0
    neg = re.compile(r"^-\.?\d")
    while i < len(argv):
        tok = argv[i]
        if tok == "--values" and i + 1 < len(argv) and neg.match(argv[i + 1]):
            out.append(f"--values={argv[i + 1]}")
            i += 2
            continue
        out.append(tok)
        i += 1
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, required_input=True):
        p.add_argument("input", nargs=None if required_input else "?",
                       help=None if required_input else
                       "omit with --image, which measures a still instead "
                       "of a clip")
        p.add_argument("--preset", "-p")
        p.add_argument("--look", "-l")
        p.add_argument("--exposure", "-e", type=float)
        p.add_argument("--tonemap", choices=["aces", "filmic", "none"])
        # Not --input: every subcommand already has an `input` positional for
        # the clip, and argparse would make one shadow the other.
        p.add_argument("--input-space", choices=list(INPUTS),
                       help="what the SOURCE is (convert.input): auto reads "
                            "the file's own transfer and primaries tags, and "
                            "the rest override that. auto never picks one of "
                            "the five camera logs (slog3, logc3, vlog, clog3, "
                            "dlog) because no container tag tells them apart, "
                            "so those are named here or not used")
        p.add_argument("--working-space", choices=["dwg", "direct", "rec709"],
                       help="rec709 for footage that is already display "
                            "referred (an mp4, a Rec.709 export coming back "
                            "for FX only); dwg and direct both expect Apple Log")
        p.add_argument("--contrast", type=float)
        p.add_argument("--saturation", type=float)
        p.add_argument("--temperature", type=float)
        p.add_argument("--tint", type=float)
        p.add_argument("--rotate", choices=list(ROTATIONS),
                       help="auto honours the file's display matrix (the "
                            "default), 0 ignores it, and 90/180/270 ignore it "
                            "and turn the picture that many degrees clockwise "
                            "(see: cinegrade orient)")
        p.add_argument("--no-autorotate", action="store_true",
                       help="alias of --rotate 0: ignore the display matrix")
        p.add_argument("--verbose", "-v", action="store_true")

    r = sub.add_parser("render"); common(r)
    r.add_argument("--output", "-o", required=True)
    r.add_argument("--start", type=float)
    r.add_argument("--duration", "-t", type=float)
    r.add_argument("--no-audio", action="store_true")
    r.add_argument("--codec",
                   help="override output.codec for this render only, never "
                        "the preset file; picks the output extension the "
                        "same way the studio server does (prores_ks -> "
                        ".mov, anything else -> .mp4)")
    r_scale = r.add_mutually_exclusive_group()
    r_scale.add_argument("--width", type=int,
                         help="render at this width instead of the source's, "
                              "scaling the pixel-denominated FX params "
                              "(halation/bloom sigma, radial blur, RGB "
                              "split, soften, grain size) the same way the "
                              "studio's preview does")
    r_scale.add_argument("--scale", type=float,
                         help="render at this fraction of the source width "
                              "(0.5 = half size), scaling the same "
                              "pixel-denominated FX params as --width")
    r.set_defaults(fn=cmd_render)

    def region_flags(p):
        """--region and --zoom, identical on still and compare.

        Both are about looking closely at part of the frame: the crop runs
        after the whole grade and before the output scale, so the patch is
        exactly the pixels the full render has there (see region_tail).
        """
        p.add_argument("--region", nargs=4, type=float,
                       metavar=("X0", "Y0", "X1", "Y1"),
                       help="render only this rectangle: four fractions of "
                            "the frame AFTER rotation, 0 0 1 1 being the "
                            "whole frame. Cropped after the grade, so "
                            "windows, vignette and grain sit where the full "
                            "render puts them")
        p.add_argument("--zoom", type=float, default=1.0,
                       help="render the region N times the size it would "
                            "have inside a --width wide frame (default 1). "
                            "Capped by the region's own pixels: there are no "
                            "more of them than the source has")

    s = sub.add_parser("still"); common(s)
    s.add_argument("--output", "-o", required=True)
    s.add_argument("--time", type=float, default=0.0)
    s.add_argument("--width", type=int)
    region_flags(s)
    s.set_defaults(fn=cmd_still)

    c = sub.add_parser("compare"); common(c)
    c.add_argument("--output", "-o", default=str(ROOT / "stills" / "compare.png"))
    c.add_argument("--time", type=float, default=0.0)
    c.add_argument("--width", type=int, default=560)
    region_flags(c)
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
    o.add_argument("--json", action="store_true",
                   help="print the tag, the four candidate rotations, "
                        "rotation_tag_suspect and rotation_tag_note; the "
                        "default (--output) contact sheet is skipped, but "
                        "--sheet still writes if given")
    o.add_argument("--sheet",
                   help="write the four candidate rotations (0, 90, 180, "
                        "270) as one labelled 2x2 sheet to this path, "
                        "through the same code cmd_sheet uses; works "
                        "alongside --json")
    o.set_defaults(fn=cmd_orient)

    st = sub.add_parser(
        "stats",
        help="the studio strip for a clip frame or a still: luma, "
             "saturation, hue families, clipping and per-band colour, as "
             "numbers. The same measurement POST /api/stats returns")
    common(st, required_input=False)
    st.add_argument("--time", type=float, default=0.0)
    st.add_argument("--image", help="measure this still instead of INPUT; "
                                    "no grade is applied, --preset and the "
                                    "look/primaries flags are ignored")
    st.add_argument("--json", action="store_true")
    st.add_argument("--region", nargs=4, type=float,
                    metavar=("X0", "Y0", "X1", "Y1"),
                    help="measure only this rectangle: four fractions of "
                         "the frame, 0 0 1 1 being the whole frame. Same "
                         "meaning as still's --region")
    st.add_argument("--times", help="comma separated seconds; measures a "
                                    "clip at each one. --json prints "
                                    "{\"results\": [...]} instead of the "
                                    "single-frame {key, size, stats} "
                                    "envelope, one {time, key, size, stats} "
                                    "row per second (not with --image)")
    st.set_defaults(fn=cmd_stats)

    sw = sub.add_parser(
        "sweep",
        help="one config parameter, many values, one stats table per "
             "value: reports what each value measures, never which to pick")
    common(sw)
    sw.add_argument("--time", type=float, default=0.0)
    sw.add_argument("--param", required=True,
                    help="dotted path into the config, e.g. "
                         "fx.halation.strength or layers.0.correct.exposure")
    sw.add_argument("--values", required=True,
                    help="comma separated values tried at --param, e.g. "
                         "0,0.25,0.5,0.75,1.0. A leading negative works "
                         "either spelled out (--values -0.1,0,0.1) or with "
                         "an = (--values=-0.1,0,0.1); both reach this the "
                         "same way")
    sw.add_argument("--width", type=int, default=640,
                    help="measure each value at this width, scaled down "
                         "from the source (default 640, the same as what "
                         "POST /api/stats measures a clip at); round 2 "
                         "tooling note 3, this used to always measure at "
                         "the source's full resolution")
    sw.add_argument("--json", action="store_true")
    sw.add_argument("--sheet", metavar="OUT.jpg",
                    help="also write a labelled contact sheet, one panel "
                         "per value, through the same sheet code cinegrade "
                         "sheet uses. With --json this path is printed to "
                         "stderr, so stdout stays pure JSON")
    sw.set_defaults(fn=cmd_sweep)

    ss = sub.add_parser(
        "session",
        help="read or change the config of a running studio server, so an "
             "outside agent can drive the grade someone else has open")
    ss.add_argument("action", choices=["get", "patch"])
    ss.add_argument("json", nargs="?",
                    help="for patch: a partial config object, deep merged into "
                         "the live one. '-' reads it from stdin.")
    add_server_flags(ss)
    ss.add_argument("--replace", action="store_true",
                    help="overwrite the live config instead of merging into it")
    ss.add_argument("--by",
                    help="who this write is from (else CINEGRADE_AGENT, else cli)")
    ss.add_argument("--if-rev", type=int, metavar="N", dest="if_rev",
                    help="for patch: refuse the write, unchanged, unless the "
                         "session is still at revision N (409 otherwise)")
    ss.add_argument("--message",
                    help="for patch: a readable message for the commit this "
                         "write records, sent as message; without it the "
                         "server writes its own one line description")
    ss.set_defaults(fn=cmd_session)

    def common_project(p):
        """--port, --url, --by, --agent, --attach and --json, identical on
        every project command and on whoami, so an agent's own wrapper
        script has one shape to build."""
        add_server_flags(p)
        p.add_argument("--by",
                       help="who this write is from (else CINEGRADE_AGENT, "
                            "else cli); an agent should send agent:NAME")
        p.add_argument("--json", action="store_true",
                       help="print the server's raw JSON instead of a table")

    wh = sub.add_parser(
        "whoami",
        help="who a write from this shell counts as, and what project is open")
    common_project(wh)
    wh.set_defaults(fn=cmd_whoami)

    pr = sub.add_parser(
        "project",
        help="open, inspect and move through a clip's git style history: "
             "one shared project per clip, seen by every account and agent")
    proj_sub = pr.add_subparsers(dest="project_cmd", required=True)

    po = proj_sub.add_parser(
        "open", help="open or create the project for a clip, and make it "
                     "this account's current one")
    po.add_argument("clip")
    po.add_argument("--rotation", choices=list(ROTATIONS),
                    help="set the project's rotation right after opening")
    common_project(po)
    po.set_defaults(fn=cmd_project)

    psh = proj_sub.add_parser(
        "show", help="the open project: key, clip, branch, head, rotation, "
                     "time, preset, branches")
    common_project(psh)
    psh.set_defaults(fn=cmd_project)

    pl = proj_sub.add_parser(
        "log", help="the whole commit tree of the open project, newest first")
    pl.add_argument("--limit", type=int, default=50,
                    help="how many commits to show, newest first (default 50)")
    pl.add_argument("--all", action="store_true",
                    help="show the whole tree instead of just the newest ones")
    common_project(pl)
    pl.set_defaults(fn=cmd_project)

    pc = proj_sub.add_parser(
        "checkout", help="move HEAD to a commit, without losing anything")
    pc.add_argument("commit", help="a commit id, or its short form")
    common_project(pc)
    pc.set_defaults(fn=cmd_project)

    pf = proj_sub.add_parser(
        "fork", help="branch off HEAD, or off --from, under a new name")
    pf.add_argument("name", nargs="?",
                    help="the branch name; left out, the server names it "
                         "fork-N")
    pf.add_argument("--from", dest="from_commit",
                    help="fork from this commit instead of HEAD")
    common_project(pf)
    pf.set_defaults(fn=cmd_project)

    pu = proj_sub.add_parser("undo", help="HEAD steps back one commit")
    common_project(pu)
    pu.set_defaults(fn=cmd_project)

    pd = proj_sub.add_parser("redo", help="HEAD steps forward one commit")
    common_project(pd)
    pd.set_defaults(fn=cmd_project)

    prt = proj_sub.add_parser(
        "rotate", help="set the project's rotation (the only rotation "
                       "control there is, once a project exists)")
    prt.add_argument("rotation", choices=list(ROTATIONS))
    common_project(prt)
    prt.set_defaults(fn=cmd_project)

    pt = proj_sub.add_parser("time", help="set the project's playhead")
    pt.add_argument("seconds", type=float)
    common_project(pt)
    pt.set_defaults(fn=cmd_project)

    mt = sub.add_parser(
        "match",
        help="fit a look cube from a reference image onto a clip frame, "
             "through the studio server's POST /api/match: no local "
             "equivalent exists, so this is a thin wrapper (see "
             "grade_client.Studio.match for the client module's own copy)")
    mt.add_argument("ref", help="a reference image name under content/refs, "
                                "or an absolute path")
    mt.add_argument("clip", help="a clip name from GET /api/state")
    mt.add_argument("--time", type=float, default=0.0)
    mt.add_argument("--preset", "-p",
                    help="a preset name or a JSON config file, sent as this "
                         "call's config; left out, the server's own defaults")
    mt.add_argument("--method", choices=["reinhard", "histogram"],
                    default="reinhard")
    mt.add_argument("--rotate", choices=list(ROTATIONS),
                    help="auto honours the clip's own display matrix (the "
                         "default), 0 ignores it, and 90/180/270 ignore it "
                         "and turn the picture that many degrees clockwise "
                         "before the fit measures it; sent as this call's "
                         "rotation field, falling back to --preset's own "
                         "config.rotation and then to auto, the same order "
                         "every other subcommand's --rotate falls back "
                         "through (see: cinegrade orient)")
    mt.add_argument("--strength", type=float, default=1.0)
    mt.add_argument("--luma-preserve", dest="luma_preserve",
                    action="store_true", default=True)
    mt.add_argument("--no-luma-preserve", dest="luma_preserve",
                    action="store_false")
    mt.add_argument("--ref-crop", nargs=4, type=float,
                    metavar=("X0", "Y0", "X1", "Y1"))
    mt.add_argument("--frame-crop", nargs=4, type=float,
                    metavar=("X0", "Y0", "X1", "Y1"))
    mt.add_argument("--name", help="the cube's own name; the server's "
                                   "default carries this caller's id, the "
                                   "reference, the clip and the method")
    mt.add_argument("--out-dir", dest="out_dir",
                    help="where the fitted cube lands; the server's "
                         "default is the shared grade/luts/looks")
    mt.add_argument("--json", action="store_true")
    add_server_flags(mt)
    mt.set_defaults(fn=cmd_match)

    pset = sub.add_parser(
        "preset",
        help="save or load a shared, named grade (POST/GET /api/preset), "
             "read and written by every account and agent alike")
    preset_sub = pset.add_subparsers(dest="preset_cmd", required=True)

    psv = preset_sub.add_parser("save", help="POST /api/preset")
    psv.add_argument("name")
    psv.add_argument("--preset", "-p", required=True,
                     help="a preset name or a JSON config file: what gets "
                          "saved under NAME")
    psv.add_argument("--comment", default="")
    psv.add_argument("--json", action="store_true")
    add_server_flags(psv)
    psv.set_defaults(fn=cmd_preset)

    psl = preset_sub.add_parser("load", help="GET /api/preset")
    psl.add_argument("name")
    psl.add_argument("--expand", action="store_true",
                     help="ask the server to mark the response expanded "
                          "(the config itself is always fully expanded)")
    psl.add_argument("--output", "-o",
                     help="write the config to this file instead of "
                          "printing it")
    psl.add_argument("--json", action="store_true")
    add_server_flags(psl)
    psl.set_defaults(fn=cmd_preset)

    grd = sub.add_parser(
        "grade",
        help="save or load one clip's own per clip grade (PUT/GET "
             "/api/grade, contract C3); distinct from `preset` above")
    grade_sub = grd.add_subparsers(dest="grade_cmd", required=True)

    grs = grade_sub.add_parser("save", help="PUT /api/grade")
    grs.add_argument("clip")
    grs.add_argument("--preset", "-p", required=True,
                     help="a preset name or a JSON config file: what gets "
                          "saved for CLIP")
    grs.add_argument("--message")
    grs.add_argument("--json", action="store_true")
    add_server_flags(grs)
    grs.set_defaults(fn=cmd_grade)

    grl = grade_sub.add_parser("load", help="GET /api/grade")
    grl.add_argument("clip")
    grl.add_argument("--output", "-o",
                     help="write the config to this file instead of "
                          "printing it")
    grl.add_argument("--json", action="store_true")
    add_server_flags(grl)
    grl.set_defaults(fn=cmd_grade)

    sh = sub.add_parser(
        "sheet",
        help="a labelled comparison image: several stills or clip frames "
             "side by side, scaled to a common height so mixed aspect "
             "inputs never fail")
    sh.add_argument("inputs", nargs="+",
                    help="PNG, JPG or any ffmpeg-readable video (one frame "
                         "at --time)")
    sh.add_argument("--output", "-o", required=True)
    sh_size = sh.add_mutually_exclusive_group()
    sh_size.add_argument("--height", type=int,
                         help="every panel's height in pixels")
    sh_size.add_argument("--width", type=int,
                         help="the whole sheet's width in pixels; the "
                              "shared panel height is solved from it")
    sh.add_argument("--grid", help="COLSxROWS, e.g. 2x3; default is one row")
    sh.add_argument("--labels",
                    help="comma separated labels, one per input, in order; "
                         "default is each file's stem")
    sh.add_argument("--time", type=float, default=0.0,
                    help="the frame time for any video input")
    sh.add_argument("--region", nargs=4, type=float,
                    metavar=("X0", "Y0", "X1", "Y1"),
                    help="crop every panel to this rectangle after loading "
                         "it: four fractions of THAT PANEL'S OWN size, 0 0 "
                         "1 1 being the whole panel, same meaning as "
                         "still's --region. No --zoom is needed here: each "
                         "panel is already scaled to the sheet's shared "
                         "height afterwards")
    sh.add_argument("--verbose", "-v", action="store_true")
    sh.set_defaults(fn=cmd_sheet)

    dc = sub.add_parser(
        "docs", help="print one section of studio/README.md by name")
    dc.add_argument("section", nargs="?",
                    help="a heading's text, case insensitive; omit to list")
    dc.add_argument("--list", action="store_true",
                    help="list every heading and exit")
    dc.set_defaults(fn=cmd_docs)

    a = ap.parse_args(_fix_negative_values_flag(sys.argv[1:]))
    # colour-science prints a notice about the missing scipy and matplotlib
    # extras the first time something imports it (grade/slice.py does, via
    # colorlib, on nearly every command). Neither extra is used here; keep
    # the notice silent unless the command asked for --verbose (see how
    # grade/tests/run_tests.py filters the same warning for the suite).
    if getattr(a, "verbose", False):
        warnings.filterwarnings("always", module="colour")
    else:
        warnings.filterwarnings("ignore", module="colour")
    try:
        a.fn(a)
    except GradeError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
