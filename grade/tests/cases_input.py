"""Group: convert.input, the input transform stage (contracts G1 and G8).

The engine used to decode every source as Apple Log. These tests pin the four
claims that make an HLG, PQ or Rec.709 source render correctly instead, and
then section (f) makes the same four claims for the five camera logs contract
G8 adds on the same mechanism (S-Log3, ARRI LogC3, V-Log, Canon Log 3, D-Log)
and section (g) covers the --input-space CLI flag:

  1. "auto" reads the file's own tags, and a file with no transfer tag on
     BT.2020 primaries still resolves to apple_log, which is what keeps every
     existing grade byte identical.
  2. Every input lands on the same scene linear point, checked by rendering an
     18 percent grey through the real graph in each input's own code values
     and comparing the Rec.709 code that comes out.
  3. One stop is a true doubling of scene light for every input, checked by
     pushing a ramp through the exact lut expression the engine emits and
     decoding both sides with colorlib.
  4. Apple Log's own graph text and its cubes did not move.

Every expected number is derived, never fitted:

  ITU-R BT.2100 Table 5      the HLG OETF and OOTF, system gamma 1.2
  SMPTE ST 2084 / BT.2100    the PQ EOTF
  ITU-R BT.2408              26 cd/m2 reference level, 203 cd/m2 reference
                             white, HLG 38% signal, PQ 58% signal
  ITU-R BT.1886              the 2.4 display gamma for Rec.709
  colorlib.MID_GREY_SCENE    0.18

and for the camera logs, each vendor's own published document (named next to
its constants in colorlib.CAMERA_LOGS), cross checked against colour-science's
independently sourced implementation of the same curve and the same gamut.
"""

from __future__ import annotations

import json
import subprocess

import colour
import numpy as np

import harness as H
from harness import C, cg

from colour.models import log_encoding_AppleLogProfile as log_enc

# 18 percent grey in each input's own code values, from colorlib and nowhere
# else. make_cst.py --anchors prints the same four numbers.
GREY_CODE = {
    "apple_log": float(log_enc(C.MID_GREY_SCENE)),
    "hlg": float(C.hlg_oetf(np.array(
        [(C.MID_GREY_SCENE / C.SCENE_PER_NIT / C.HLG_PEAK_NITS)
         ** (1.0 / C.HLG_SYSTEM_GAMMA)]))[0]),
    "pq": float(C.pq_inverse_eotf(np.array(
        [C.MID_GREY_SCENE / C.SCENE_PER_NIT]))[0]),
    "rec709": float((C.MID_GREY_SCENE / C.REC709_SCENE_SCALE)
                    ** (1.0 / C.BT1886_GAMMA)),
}

# A 65 cube sampled tetrahedrally lands within a fraction of a code value of
# the closed form, and two inputs reach mid grey through two different cubes,
# so the anchor comparison carries the same tolerance cases_color uses for the
# published Rec.709 anchors: 0.0015, about 0.4 of an 8 bit code.
ANCHOR_TOL = 0.0015


def _info(transfer="", primaries="", matrix="bt2020nc"):
    """A probe-shaped dict with just the three tags this stage reads."""
    return {"width": 1920, "height": 1080, "rotation": 0, "rotate": "auto",
            "autorotate": True, "pix_fmt": "yuv422p10le", "color_range": "tv",
            "color_space": matrix, "color_transfer": transfer,
            "color_primaries": primaries, "nb_frames": "1", "duration": "1",
            "codec": "prores", "profile": None}


def _cfg(**convert):
    return H.patch(H.defaults(), {"convert": convert})


# --------------------------------------------------------------------------
# (a) resolution
# --------------------------------------------------------------------------

def test_auto_reads_the_files_own_tags(ctx):
    """Every tag combination in the contract, on synthetic probe dicts.

    The last row is the one that protects everything already graded: an Apple
    Log clip carries BT.2020 primaries and NO transfer tag at all (verified on
    footage/A001_09011336_C002.MOV, whose color_transfer is empty), so auto has
    to answer apple_log for it with no warning.
    """
    cases = [
        ("HLG phone clip", _info("arib-std-b67", "bt2020"), "hlg", 0),
        ("HLG, underscore spelling", _info("arib_std_b67", "bt2020"), "hlg", 0),
        ("PQ master", _info("smpte2084", "bt2020"), "pq", 0),
        ("Rec.709 delivery", _info("bt709", "bt709", "bt709"), "rec709", 0),
        ("Apple Log (no transfer tag)", _info("", "bt2020"), "apple_log", 0),
        ("untagged entirely", _info("", "", "bt2020nc"), "apple_log", 0),
        ("bt709 transfer on bt2020 primaries",
         _info("bt709", "bt2020"), "apple_log", 1),
        ("a transfer nobody knows", _info("log316", "bt2020"), "apple_log", 1),
    ]
    for label, info, want, warns in cases:
        got, msgs = cg.resolve_input(H.defaults(), info)
        ctx.expect_eq(f"auto on {label}", got, want)
        ctx.expect_eq(f"auto on {label}: warnings", len(msgs), warns)
        if msgs:
            ctx.expect_true(f"the warning on {label} names the tag",
                            (info["color_transfer"] or "unset") in msgs[0],
                            msgs[0])


def test_an_explicit_input_is_never_second_guessed(ctx):
    """The file's tags cannot override what the caller said.

    Metadata lies; the person looking at the picture does not. So an explicit
    convert.input wins on every file, including one whose tags say otherwise.
    """
    for name in cg.INPUTS[1:]:
        for label, info in (("an HLG file", _info("arib-std-b67", "bt2020")),
                            ("an untagged file", _info()),
                            ("no info at all", None)):
            got, msgs = cg.resolve_input(_cfg(input=name), info)
            ctx.expect_eq(f"input {name} on {label}", got, name)
            ctx.expect_eq(f"input {name} on {label}: no warning", len(msgs), 0)
    # "redlog" rather than a real camera's name: five of those became valid
    # values in contract G8, and a test whose invalid value can quietly turn
    # valid is a test that stops testing.
    try:
        cg.resolve_input(_cfg(input="redlog"), _info())
        ctx.check(False, "an unknown input was accepted")
    except cg.GradeError as exc:
        ctx.expect_true("an unknown input is refused by name", "redlog" in str(exc),
                        str(exc))
        ctx.expect_true("and the message lists the camera logs it could be",
                        "slog3" in str(exc) and "dlog" in str(exc), str(exc))


def test_primaries_are_read_separately_from_the_transfer(ctx):
    """A file can carry one gamut and another curve, so they are two questions.

    This is the field the old guard never looked at: it read the YUV matrix and
    called BT.2020 "camera log", which is how an HLG delivery file ended up
    refused for asking to be graded as what it is.
    """
    ctx.expect_eq("bt709 tag is bt709",
                  cg.source_primaries(_info("bt709", "bt709", "bt709")), "bt709")
    ctx.expect_eq("bt2020 tag is bt2020",
                  cg.source_primaries(_info("arib-std-b67", "bt2020")), "bt2020")
    ctx.expect_eq("an untagged apple_log file falls back to bt2020",
                  cg.source_primaries(_info(), "apple_log"), "bt2020")
    ctx.expect_eq("an untagged rec709 file falls back to bt709",
                  cg.source_primaries(_info(matrix="bt709"), "rec709"), "bt709")
    ctx.expect_eq("smpte170m is a Rec.709 gamut in practice",
                  cg.source_primaries(_info("bt709", "smpte170m", "bt709")),
                  "bt709")


def test_the_guard_stops_refusing_a_bt2020_delivery_file(ctx):
    """check_source_space's two refusals, before and after.

    It used to refuse working_space rec709 on ANY bt2020-tagged file. An HLG
    file is bt2020 and is not camera log, so that refusal was wrong for exactly
    the file this contract exists for. It is now decided by what the source
    resolved to, so the Apple Log refusal is untouched and the HLG one is gone.
    """
    hlg = _info("arib-std-b67", "bt2020")
    src, warns = cg.check_source_space(_cfg(working_space="rec709"), hlg)
    ctx.expect_eq("an HLG file may be graded in place", src, "hlg")
    ctx.expect_eq("and says so once", len(warns), 1)

    log = _info("", "bt2020")
    try:
        cg.check_source_space(_cfg(working_space="rec709"), log)
        ctx.check(False, "camera log on working_space rec709 was allowed")
    except cg.GradeError as exc:
        ctx.expect_true("camera log on working_space rec709 is still refused",
                        "flat and desaturated" in str(exc), str(exc)[:90])

    delivery = _info("", "bt709", "bt709")
    try:
        cg.check_source_space(H.defaults(), delivery)
        ctx.check(False, "an untagged bt709 file on the log path was allowed")
    except cg.GradeError as exc:
        ctx.expect_true("an untagged bt709 file on the log path is still refused",
                        "0.332 to 0.238" in str(exc), str(exc)[:90])

    tagged = _info("bt709", "bt709", "bt709")
    src, warns = cg.check_source_space(H.defaults(), tagged)
    ctx.expect_eq("a properly tagged Rec.709 file is decoded, not refused",
                  src, "rec709")
    ctx.expect_eq("with nothing to warn about", len(warns), 0)


# --------------------------------------------------------------------------
# (b) the constants and the cubes
# --------------------------------------------------------------------------

def test_the_engine_and_colorlib_agree_on_every_constant(ctx):
    """cinegrade carries its own copy of the HLG and PQ constants because it
    must not import numpy or colour-science. Two copies is one more than one,
    so the suite is what stops them drifting."""
    pairs = [("HLG a", cg.HLG_A, C.HLG_A), ("HLG b", cg.HLG_B, C.HLG_B),
             ("HLG c", cg.HLG_C, C.HLG_C),
             ("HLG system gamma", cg.HLG_SYSTEM_GAMMA, C.HLG_SYSTEM_GAMMA),
             ("PQ m1", cg.PQ_M1, C.PQ_M1), ("PQ m2", cg.PQ_M2, C.PQ_M2),
             ("PQ c1", cg.PQ_C1, C.PQ_C1), ("PQ c2", cg.PQ_C2, C.PQ_C2),
             ("PQ c3", cg.PQ_C3, C.PQ_C3)]
    for label, a, b in pairs:
        ctx.expect_close(f"{label} matches colorlib", a, b, 1e-12)
    ctx.expect_close("BT.1886 gamma is the engine's display gamma",
                     cg.DISPLAY_GAMMA, C.BT1886_GAMMA, 1e-12)

    # The direct path's contrast pivot, which is the source's own mid grey.
    for name, want in GREY_CODE.items():
        ctx.expect_close(f"MID_GREY_CODE_INPUT[{name}] is 18% grey in its code",
                         cg.MID_GREY_CODE_INPUT[name], want, 5e-5)
    ctx.expect_close("and apple_log still equals the old direct pivot",
                     cg.MID_GREY_CODE_INPUT["apple_log"],
                     cg.MID_GREY_CODE["direct"], 1e-9)


def test_bt2408_is_where_the_hdr_anchor_comes_from(ctx):
    """The one number HLG and PQ share, checked against the standard.

    BT.2408 publishes reference white at 203 cd/m2 and reference level at 26
    cd/m2, and gives the signal levels they sit at: 75% and 38% for HLG, 58%
    and 38% for PQ. Nothing here is fitted: the OOTF at system gamma 1.2 on a
    1000 cd/m2 display produces those luminances on its own, which is the
    check that the constants are right rather than merely consistent.
    """
    grey = C.hlg_ootf(C.hlg_inverse_oetf(np.array([[0.38] * 3])))[0][0]
    white = C.hlg_ootf(C.hlg_inverse_oetf(np.array([[0.75] * 3])))[0][0]
    ctx.note(f"HLG 38% signal is {grey:.2f} cd/m2, 75% signal is {white:.2f}")
    ctx.expect_close("HLG 38% signal is BT.2408's 26 cd/m2", float(grey), 26.0, 0.5)
    ctx.expect_close("HLG 75% signal is BT.2408's 203 cd/m2", float(white),
                     203.0, 0.5)
    pq58 = float(C.pq_eotf(np.array([0.58]))[0])
    ctx.expect_close("PQ 58% signal is BT.2408's 203 cd/m2", pq58, 203.0, 2.0)
    ctx.expect_close("and 26 cd/m2 is 0.18 scene linear",
                     26.0 * C.SCENE_PER_NIT, C.MID_GREY_SCENE, 1e-12)
    ctx.note(f"so reference white lands at {203.0 * C.SCENE_PER_NIT:.4f} "
             f"scene linear, inside the tone map's shoulder")


def test_every_input_has_the_cubes_it_names(ctx):
    """technical_lut_name is the one place a cube is named, and make_cst.py
    writes exactly those names, so a missing file here means the generator and
    the engine have drifted rather than that someone forgot a flag."""
    wanted = []
    for src, prim in (("apple_log", "bt2020"), ("hlg", "bt2020"),
                      ("pq", "bt2020"), ("rec709", "bt709"),
                      ("rec709", "bt2020")):
        wanted.append(cg.technical_lut_name(src, prim, "dwg"))
        for tm in ("aces", "filmic", "none"):
            for enc in ("", "gamma24", "rec709a"):
                wanted.append(cg.technical_lut_name(src, prim, "direct", tm, enc))
    missing = [n for n in wanted if not (cg.LUT_TECH / n).exists()]
    ctx.note(f"{len(wanted)} technical cubes named, {len(missing)} missing")
    ctx.expect_true("every named technical cube exists", not missing,
                    f"missing: {missing[:6]}" if missing else "none missing")

    ctx.expect_eq("the Apple Log names did not move",
                  cg.technical_lut_name("apple_log", "bt2020", "dwg"),
                  "AppleLog_to_DWG.cube")
    ctx.expect_eq("nor the per encode direct one",
                  cg.technical_lut_name("apple_log", "bt2020", "direct",
                                        "aces", "rec709a"),
                  "AppleLog_to_Rec709_aces_rec709a.cube")
    ctx.expect_eq("HLG follows the same pattern",
                  cg.technical_lut_name("hlg", "bt2020", "direct", "filmic"),
                  "HLG_to_Rec709_filmic.cube")
    ctx.expect_eq("and off-native primaries are named, not assumed",
                  cg.technical_lut_name("rec709", "bt2020", "dwg"),
                  "Rec709_2020_to_DWG.cube")


# --------------------------------------------------------------------------
# (c) the picture: every input reaches the same scene linear point
# --------------------------------------------------------------------------

def _grey_out(source: str, **convert):
    """18 percent grey in `source`'s code values, through the whole graph."""
    cfg = _cfg(input=source, **convert)
    got = H.render_patches(cfg, {"grey18": [GREY_CODE[source]] * 3})
    return float(got["grey18"][0])


def test_eighteen_percent_grey_lands_in_the_same_place_for_every_input(ctx):
    """The whole contract in one measurement.

    An 18 percent grey card is 0.18 scene linear whatever it was shot on. Each
    input encodes it at a different code (Apple Log 0.4883, HLG 0.3786, PQ
    0.3800, Rec.709 0.4090), so if the input transforms are right, all four
    codes come out of the graph as the same Rec.709 code. If HLG were still
    decoded as Apple Log it would come out around three stops dark instead,
    which is the bug this stage exists to fix.
    """
    ref = _grey_out("apple_log")
    ctx.note(f"apple_log: code {GREY_CODE['apple_log']:.4f} in, {ref:.4f} out")
    for name in ("hlg", "pq", "rec709"):
        got = _grey_out(name)
        ctx.note(f"{name}: code {GREY_CODE[name]:.4f} in, {got:.4f} out")
        ctx.expect_close(f"{name} 18% grey lands where Apple Log's lands",
                         got, ref, ANCHOR_TOL)

    # What it used to do, and still would with the wrong input forced: an HLG
    # grey read as an Apple Log code is a much darker picture.
    wrong = float(H.render_patches(
        _cfg(input="apple_log"), {"grey18": [GREY_CODE["hlg"]] * 3})["grey18"][0])
    ctx.note(f"the same HLG code decoded as Apple Log: {wrong:.4f}")
    ctx.expect_lt("decoding HLG as Apple Log is much darker", wrong, ref,
                  margin=0.05)


def test_the_direct_path_reaches_the_same_place_too(ctx):
    """working_space "direct" has no CST IN, so it loads a per input cube of
    its own. Same assertion, different cube, which is what catches a direct
    cube generated from the wrong transfer."""
    ref = _grey_out("apple_log", working_space="direct")
    for name in ("hlg", "pq", "rec709"):
        got = _grey_out(name, working_space="direct")
        ctx.note(f"direct {name}: {got:.4f} against apple_log {ref:.4f}")
        ctx.expect_close(f"direct: {name} 18% grey matches Apple Log's",
                         got, ref, ANCHOR_TOL)


def test_a_ramp_keeps_its_order_and_its_ends(ctx):
    """A nine step HLG ramp comes out monotone, black stays black and the
    brightest step does not clip. A cube built from a broken piecewise
    boundary shows up here as a kink or an inversion, which the single grey
    anchor above cannot see."""
    codes = [0.0, 0.1, 0.2, 0.3, 0.38, 0.5, 0.6, 0.75, 0.9]
    patches = {f"s{i}": [c] * 3 for i, c in enumerate(codes)}
    got = H.render_patches(_cfg(input="hlg"), patches)
    out = [float(got[f"s{i}"][0]) for i in range(len(codes))]
    ctx.note("HLG ramp out: " + ", ".join(f"{v:.4f}" for v in out))
    for i in range(1, len(out)):
        ctx.expect_gt(f"step {i} is above step {i - 1}", out[i], out[i - 1])
    ctx.expect_lt("signal 0 is black", out[0], 0.01)
    ctx.expect_lt("signal 0.9 has not clipped", out[-1], 1.0, margin=0.002)


# --------------------------------------------------------------------------
# (d) exposure is a true doubling for every input
# --------------------------------------------------------------------------

RAMP = [0.05, 0.1, 0.2, 0.3, 0.38, 0.5, 0.6, 0.7]

# Apple Log gets its own ramp, above the top of its toe. A constant code
# offset is a gain only where the curve is actually logarithmic, and Apple
# Log's is not down in the toe: measured through this same lut, the shipped
# stop is a factor of 3.11 at code 0.20 and 2.53 at code 0.25. That is the
# behaviour this engine has always had and it is left exactly as it is (every
# golden depends on the string), but it is also the reason the three new
# inputs got formulas derived from their own standards instead of one borrowed
# constant.
APPLE_RAMP = [0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9]


def _through_lut(codes, expr):
    """One ffmpeg pass of the engine's own lut expression over a ramp.

    Built as a bare graph rather than through render_patches because what is
    under test is the exposure node alone: putting a CST after it would fold
    the cube's own lattice error into a measurement of an arithmetic claim.
    """
    patches = {f"s{i}": [c] * 3 for i, c in enumerate(codes)}
    png, names = H.make_patch_source(patches)
    info = H.patch_info(png, len(names))
    vf = f"format=gbrp16le,lut=r='{expr}':g='{expr}':b='{expr}',format=rgb48le"
    raw = H._run_bytes(["ffmpeg", "-v", "error", "-y", "-i", str(png),
                        "-vf", vf, "-frames:v", "1", "-f", "rawvideo",
                        "-pix_fmt", "rgb48le", "-"])
    out = np.frombuffer(raw, "<u2").reshape(
        info["height"], info["width"], 3).astype(np.float64) / 65535.0
    mid = info["height"] // 2
    return np.array([out[mid, i * 8 + 4, 0] for i in range(len(names))])


def _stop_ratios(source, stops, codes):
    """Scene light after N stops divided by scene light before, per step.

    Both sides go through the real ffmpeg lut and are then decoded by
    colorlib, so what comes back is a ratio of light and not of code values.
    """
    dec = C.INPUT_DECODERS[source]
    base = _through_lut(codes, cg.exposure_expr(source, 0.0))
    got = _through_lut(codes, cg.exposure_expr(source, stops))
    a = dec(np.repeat(base[:, None], 3, axis=1))[:, 0]
    b = dec(np.repeat(got[:, None], 3, axis=1))[:, 0]
    keep = (a > 1e-5) & (got < 0.999) & (got > 1e-4)
    return b[keep] / a[keep], int(keep.sum())


def test_one_stop_doubles_the_linear_value_for_every_input(ctx):
    """A stop has to be a doubling of scene light, not of whatever the code
    happens to be.

    APPLE_LOG_STOP is a constant code offset, right for Apple Log's curve and
    for no other. On HLG the pre-OOTF scene light is scaled by 2 ** (1 / 1.2),
    because the OOTF raises everything to the 1.2; on PQ the absolute cd/m2 are
    doubled; on Rec.709 the code is multiplied by 2 ** (1 / 2.4).

    The tolerance is 1 percent because ffmpeg's lut is a 65536 entry table: the
    expression is exact, the table it is sampled into rounds.
    """
    for source in ("hlg", "pq", "rec709"):
        for stops in (1.0, -1.0):
            ratio, n = _stop_ratios(source, stops, RAMP)
            want = 2.0 ** stops
            worst = float(np.abs(ratio / want - 1.0).max())
            ctx.note(f"{source} {stops:+.0f} stop: ratio "
                     f"{ratio.min():.4f} to {ratio.max():.4f} on "
                     f"{n} of {len(RAMP)} steps")
            ctx.expect_lt(f"{source}: {stops:+.0f} stop is a factor of {want:g}",
                          worst, 0.01)


def test_apple_logs_own_stop_is_left_exactly_as_it_was(ctx):
    """The one input whose exposure did not change, measured the same way.

    Apple Log keeps the constant code offset this engine has always emitted,
    because every golden fingerprint is downstream of that exact string. It is
    a true doubling only where the curve is logarithmic, and it is a rounded
    constant even there (0.08492 rather than the curve's own slope), so it
    misses by up to 1.4 percent even in the highlights and runs away entirely
    in the toe.

    So the tolerance here is 2 percent where the three new inputs meet 1: the
    gap is the measurement that justifies giving HLG, PQ and Rec.709 formulas
    derived from their own standards rather than one shared constant. Both
    numbers are pinned rather than left implicit, the first saying the Apple
    Log path is still as good as it always was, the second saying how good
    that is.
    """
    for stops in (1.0, -1.0):
        ratio, n = _stop_ratios("apple_log", stops, APPLE_RAMP)
        want = 2.0 ** stops
        worst = float(np.abs(ratio / want - 1.0).max())
        ctx.note(f"apple_log {stops:+.0f} stop above the toe: ratio "
                 f"{ratio.min():.4f} to {ratio.max():.4f} on {n} steps")
        ctx.expect_lt(f"apple_log: {stops:+.0f} stop is within 2 percent of a "
                      f"factor of {want:g} where the curve is logarithmic",
                      worst, 0.02)

    toe, _ = _stop_ratios("apple_log", 1.0, [0.20, 0.25, 0.30])
    ctx.note(f"the same offset in the toe: {toe.max():.2f}x at code 0.20, "
             f"which is why a borrowed constant is not an exposure control")
    ctx.expect_gt("a constant code offset is not a stop in Apple Log's toe",
                  float(toe.max()), 2.5)


def test_the_log_stage_writes_the_expression_it_advertises(ctx):
    """The ramp above measures exposure_expr; this is what says the graph
    actually contains it, per channel, for the resolved input."""
    for source, info in (("apple_log", _info("", "bt2020")),
                         ("hlg", _info("arib-std-b67", "bt2020")),
                         ("pq", _info("smpte2084", "bt2020")),
                         ("rec709", _info("bt709", "bt709", "bt709"))):
        cfg = H.patch(H.defaults(), {"convert": {"exposure": 0.5}})
        chain = cg.f_log_stage(cfg, info)
        luts = [f for f in chain if f.startswith("lut=")]
        ctx.expect_eq(f"{source}: one exposure node", len(luts), 1)
        want = cg.exposure_expr(source, 0.5)
        ctx.expect_true(f"{source}: on all three channels",
                        luts[0] == f"lut=r='{want}':g='{want}':b='{want}'",
                        luts[0][:80])

    # working_space rec709 is decided by the working space and not by the
    # input, because on that path nothing undoes any curve.
    flat = H.patch(H.defaults(),
                   {"convert": {"exposure": 0.5, "working_space": "rec709",
                                "input": "hlg"}})
    chain = cg.f_log_stage(flat, _info("arib-std-b67", "bt2020"))
    want = cg.exposure_expr("rec709", 0.5)
    ctx.expect_true("working_space rec709 spends a stop as a code multiply",
                    f"lut=r='{want}'" in chain[-1], chain[-1][:80])


# --------------------------------------------------------------------------
# (e) nothing Apple Log moved
# --------------------------------------------------------------------------

def test_apple_log_graph_text_is_exactly_what_it_was(ctx):
    """The strings, not the pixels, because a string is what the golden
    fingerprints are ultimately a hash of. Written out in full rather than
    round-tripped through the engine, so this fails if either side changes."""
    info = H.info_for(H.CLIP_A)
    cfg = H.patch(H.defaults(), {"convert": {"exposure": 1.0},
                                 "primaries": {"temperature": 0.05}})
    chain = cg.f_log_stage(cfg, info)
    ctx.expect_eq("the decode and normalise step", chain[0],
                  f"scale=in_color_matrix=bt2020:in_range={info['color_range']}"
                  f":out_range=full")
    ctx.expect_eq("the repack", chain[1], "format=gbrp16le")
    ctx.expect_eq(
        "exposure is still the Apple Log code offset", chain[2],
        "lut=r='clip(val+0.089166*maxval,0,maxval)'"
        ":g='clip(val+0.084920*maxval,0,maxval)'"
        ":b='clip(val+0.080674*maxval,0,maxval)'")
    ctx.expect_true("CST IN is still the Apple Log cube",
                    "AppleLog_to_DWG.cube" in cg.f_convert_in(cfg, info)[0],
                    cg.f_convert_in(cfg, info)[0])
    ctx.expect_true("CST OUT is still the DWG pair's own cube",
                    "DWG_to_Rec709_aces_rec709a.cube"
                    in cg.f_convert_out(cfg, info)[0],
                    cg.f_convert_out(cfg, info)[0])

    direct = H.patch(cfg, {"convert": {"working_space": "direct"}})
    ctx.expect_true("and the direct path's cube did not move either",
                    "AppleLog_to_Rec709_aces_rec709a.cube"
                    in cg.f_convert_out(direct, info)[0],
                    cg.f_convert_out(direct, info)[0])
    ctx.expect_close("the direct pivot is still the Apple Log grey",
                     cg.mid_grey_code(direct, info), 0.4883, 1e-9)


def test_a_caller_with_no_probe_still_gets_apple_log(ctx):
    """Half the engine's callers hand these functions a hand-built info dict
    (the grain plate's stand-in, bake_lut, the studio's normalised source) and
    some hand them nothing at all. Every one of them has to keep the answer it
    had before this stage existed."""
    cfg = H.defaults()
    for label, info in (("no info", None), ("empty dict", {}),
                        ("the layer tests' minimal dict",
                         {"width": 1920, "height": 1080, "color_range": "tv"})):
        ctx.expect_eq(f"{label} resolves to apple_log",
                      cg.input_of(cfg, info), "apple_log")
        ctx.expect_true(f"{label} loads the Apple Log cube",
                        "AppleLog_to_DWG.cube" in cg.f_convert_in(cfg, info)[0],
                        cg.f_convert_in(cfg, info)[0])
    ctx.expect_true("f_convert_in still works with no info argument at all",
                    "AppleLog_to_DWG.cube" in cg.f_convert_in(cfg)[0],
                    cg.f_convert_in(cfg)[0])
    ctx.expect_true("and so does f_convert_out",
                    "DWG_to_Rec709" in cg.f_convert_out(cfg)[0],
                    cg.f_convert_out(cfg)[0])


# --------------------------------------------------------------------------
# (f) the camera logs (contract G8)
# --------------------------------------------------------------------------
#
# Five vendor curves on the same mechanism: S-Log3, ARRI LogC3 at EI 800,
# Panasonic V-Log, Canon Log 3 and DJI D-Log. Each is checked three ways:
#
#   1. the constants are the published ones, cross checked against
#      colour-science's independently sourced implementation of the same
#      curve and the same gamut,
#   2. the published 18 percent grey code decodes to the same 0.18 scene
#      linear the Apple Log path reaches, measured through the real graph,
#   3. one stop of convert.exposure is a true doubling of light, measured
#      through the exact ffmpeg lut expression the engine emits.
#
# colour-science is a second opinion, not the source: the constants in
# colorlib are written from the vendor documents and this is what says the
# two agree. Where colour's API takes a container convention as an argument
# (Canon Log 3's out_normalised_code_value) the raw curve is asked for,
# because the engine has already normalised the file to full scale by the
# time the curve runs, exactly as it has for HLG and PQ since contract G1.

from colour.models import (                                   # noqa: E402
    log_encoding_SLog3, log_encoding_ARRILogC3, log_encoding_VLog,
    log_encoding_CanonLog3, log_encoding_DJIDLog,
    log_decoding_SLog3, log_decoding_ARRILogC3, log_decoding_VLog,
    log_decoding_CanonLog3, log_decoding_DJIDLog)

REFERENCE_ENCODE = {
    "slog3": log_encoding_SLog3,
    "logc3": log_encoding_ARRILogC3,
    "vlog": log_encoding_VLog,
    "clog3": lambda x: log_encoding_CanonLog3(x, out_normalised_code_value=False),
    "dlog": log_encoding_DJIDLog,
}
REFERENCE_DECODE = {
    "slog3": log_decoding_SLog3,
    "logc3": log_decoding_ARRILogC3,
    "vlog": log_decoding_VLog,
    "clog3": lambda y: log_decoding_CanonLog3(y, in_normalised_code_value=False),
    "dlog": log_decoding_DJIDLog,
}

# The gamut each camera log is on, as the vendor publishes its chromaticities.
# Written out again here, away from colorlib, so this test fails if either
# copy is edited rather than passing because it read the same table twice.
PUBLISHED_GAMUTS = {
    "sgamut3cine": [[0.766, 0.275], [0.225, 0.800], [0.089, -0.087]],
    "awg3": [[0.6840, 0.3130], [0.2210, 0.8480], [0.0861, -0.1020]],
    "vgamut": [[0.730, 0.280], [0.165, 0.840], [0.100, -0.030]],
    "cinemagamut": [[0.7400, 0.2700], [0.1700, 1.1400], [0.0800, -0.1000]],
    "dgamut": [[0.7100, 0.3100], [0.2100, 0.8800], [0.0900, -0.0800]],
}

# The 18 percent grey code each document quotes, rounded the way the document
# does. Written as literals so a change to the constants has to be justified
# against the page rather than against itself.
PUBLISHED_GREY_CODE = {
    "slog3": 420.0 / 1023.0,      # Sony: 10 bit code 420
    "logc3": 400.0 / 1023.0,      # ARRI: 10 bit code 400 at EI 800
    "vlog": 0.4233,               # Panasonic: 42.3 IRE
    "clog3": 0.3280,              # Canon: 32.8 IRE
    "dlog": 0.3988,               # DJI
}

# Seven patches from three stops under an 18 percent grey card to three over,
# encoded with each curve's own forward formula. Scene referred rather than
# code referred on purpose: a fixed code ramp lands in the deep toe of some of
# these curves and nowhere near it on others, and a stop of light down there
# is a fraction of a code value, which measures ffmpeg's table rounding rather
# than the formula. Every patch here sits in the log segment of all five.
CAMERA_STOPS = [-3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0]


def _camera_codes(name: str) -> list[float]:
    log = C.CAMERA_LOGS[name]
    scene = C.MID_GREY_SCENE * (2.0 ** np.array(CAMERA_STOPS))
    return [float(v) for v in log.from_scene_linear(scene)]


def test_the_camera_log_constants_are_the_published_ones(ctx):
    """Every constant in the five curves, against a second implementation.

    colorlib carries the numbers (written from the vendor documents) and
    cinegrade carries a second copy of them, because cinegrade must not import
    numpy or colour-science. So there are three sources that have to agree:
    colorlib, cinegrade, and colour-science's own reading of the same
    documents. This checks all three.
    """
    for name, log in C.CAMERA_LOGS.items():
        k = cg.CAMERA_LOG_CONSTANTS[name]
        for field in ("A", "B", "C", "D", "E", "F"):
            ctx.expect_close(f"{name} {field}: cinegrade matches colorlib",
                             k[field], getattr(log, field), 1e-12)
        ctx.expect_close(f"{name} cut_x: cinegrade matches colorlib",
                         k["cut_x"], log.cut_x, 1e-12)
        ctx.expect_close(f"{name} cut_y: cinegrade matches colorlib",
                         k["cut_y"], log.cut_y, 1e-12)

        # The forward curve against colour-science's own, over four decades of
        # light. 1e-7 rather than 1e-12 because Canon Log 3's third segment
        # (sub-black, deliberately not carried: see colorlib) is the residual.
        x = np.array([0.0, 0.005, 0.02, 0.05, 0.18, 0.5, 0.9, 2.0, 8.0, 16.0])
        got = log.from_scene_linear(x)
        want = np.asarray(REFERENCE_ENCODE[name](x), dtype=np.float64)
        worst = float(np.max(np.abs(got - want)))
        ctx.note(f"{name}: forward curve differs from colour-science by "
                 f"at most {worst:.2e} over 0 to 16 scene linear")
        ctx.expect_lt(f"{name}: the forward curve is the published one",
                      worst, 1e-7)

        # And the decode, wherever the answer is not simply clamped black.
        # 5e-5 rather than 1e-9 for one reason, and it is D-Log's: its own
        # published toe reaches 0.139995 where its published decode threshold
        # is 0.14, so the two branches disagree by 5e-6 of light on the single
        # sample that falls in that gap. Both sides use the document's own
        # threshold; the seam is the document's.
        y = np.linspace(0.0, 1.0, 101)
        mine = log.to_scene_linear(y)
        theirs = np.asarray(REFERENCE_DECODE[name](y), dtype=np.float64)
        live = mine > 1e-9
        rel = float(np.max(np.abs(mine[live] / theirs[live] - 1.0)))
        ctx.note(f"{name}: decode differs from colour-science by at most "
                 f"{rel:.2e} relative")
        ctx.expect_lt(f"{name}: the decode is the published one", rel, 5e-5)

        # And the two are actually inverses, which no cross check can give.
        rt = log.to_scene_linear(log.from_scene_linear(x))
        ctx.expect_lt(f"{name}: encode and decode round trip",
                      float(np.max(np.abs(rt - x))), 1e-9)


def test_eighteen_percent_grey_is_where_each_document_says_it_is(ctx):
    """The anchor, as a number a reader can check against the page.

    Sony puts 18 percent grey at 10 bit code 420, ARRI at code 400 for EI 800,
    Panasonic at 42.3 IRE, Canon at 32.8 IRE. Those are the numbers this
    engine's curves have to produce from 0.18, and the numbers the direct
    path's contrast pivot has to carry.
    """
    for name, log in C.CAMERA_LOGS.items():
        got = log.grey_code()
        ctx.note(f"{name}: 18% grey at code {got:.6f} ({log.doc})")
        ctx.expect_close(f"{name}: 18% grey is the published code",
                         got, PUBLISHED_GREY_CODE[name], 6e-5)
        ctx.expect_close(f"{name}: the direct path's pivot is that code",
                         cg.MID_GREY_CODE_INPUT[name], got, 5e-5)
        back = float(log.to_scene_linear(np.array([got]))[0])
        ctx.expect_close(f"{name}: and it decodes back to 0.18",
                         back, C.MID_GREY_SCENE, 1e-9)


def test_every_camera_gamut_is_the_published_one(ctx):
    """The primaries, and the one property a matrix cannot fake.

    Every one of these gamuts is D65, as BT.2020 is, so the matrix into
    BT.2020 has to leave a neutral exactly neutral. A transposed digit in a
    chromaticity shows up as a tint on white, which is the failure a picture
    would show first and the one this catches without rendering anything.
    """
    for gamut, published in PUBLISHED_GAMUTS.items():
        prim = C.CAMERA_GAMUT_PRIMARIES[gamut]
        ctx.expect_true(f"{gamut}: the chromaticities are the published ones",
                        bool(np.allclose(prim, np.array(published), atol=1e-9)),
                        f"{prim.tolist()} against {published}")
        ref = colour.RGB_COLOURSPACES[C.CAMERA_GAMUT_REFERENCE[gamut]]
        ctx.expect_true(f"{gamut}: and colour-science agrees",
                        bool(np.allclose(prim, ref.primaries, atol=1e-9)),
                        f"{prim.tolist()} against {ref.primaries.tolist()}")
        white = C.to_bt2020(np.array([1.0, 1.0, 1.0]), gamut)
        ctx.note(f"{gamut}: white -> BT.2020 {np.round(white, 6).tolist()}")
        ctx.expect_lt(f"{gamut}: a neutral stays neutral into BT.2020",
                      float(np.max(np.abs(white - 1.0))), 1e-6)
        # Against colour-science's own matrix for the same gamut. 2e-4 rather
        # than zero because three of these vendors also publish a ROUNDED RGB
        # to XYZ matrix, which is what colour uses, and it disagrees with the
        # chromaticities it is derived from in the fourth decimal.
        theirs = colour.matrix_RGB_to_RGB(ref, C.APPLE_LOG_GAMUT, "CAT02")
        ctx.expect_lt(f"{gamut}: the matrix matches colour-science",
                      float(np.max(np.abs(C.CAMERA_GAMUT_TO_BT2020[gamut]
                                          - theirs))), 2e-4)

    for name, log in C.CAMERA_LOGS.items():
        ctx.expect_eq(f"{name} is on its own gamut, not the file's tag",
                      cg.source_primaries(_info("bt2020", "bt2020"), name),
                      log.gamut)
        ctx.expect_eq("and colorlib says the same",
                      C.INPUT_NATIVE_PRIMARIES[name], log.gamut)


def test_every_camera_log_has_the_cubes_it_names(ctx):
    """One cube set per camera log, named without a primaries infix.

    A camera log's gamut comes with its curve, so unlike Rec.709 (which really
    can turn up on two different gamuts) there is exactly one answer and the
    name says only the curve.
    """
    wanted = []
    for name in cg.CAMERA_LOG_INPUTS:
        prim = cg.NATIVE_PRIMARIES[name]
        wanted.append(cg.technical_lut_name(name, prim, "dwg"))
        for tm in ("aces", "filmic", "none"):
            for enc in ("", "gamma24", "rec709a"):
                wanted.append(cg.technical_lut_name(name, prim, "direct", tm, enc))
    missing = [n for n in wanted if not (cg.LUT_TECH / n).exists()]
    ctx.note(f"{len(wanted)} camera log cubes named, {len(missing)} missing")
    ctx.expect_true("every camera log cube exists", not missing,
                    f"missing: {missing[:6]}" if missing else "none missing")
    ctx.expect_eq("S-Log3's CST IN is named for the curve alone",
                  cg.technical_lut_name("slog3", "sgamut3cine", "dwg"),
                  "SLog3_to_DWG.cube")
    ctx.expect_eq("and the direct set follows the same pattern",
                  cg.technical_lut_name("dlog", "dgamut", "direct", "filmic",
                                        "rec709a"),
                  "DLog_to_Rec709_filmic_rec709a.cube")


def test_a_camera_log_grey_card_lands_where_apple_logs_does(ctx):
    """The whole of contract G8 in one measurement, through the real graph.

    An 18 percent grey card is 0.18 scene linear whether it was shot on a
    Sony, an ARRI, a Panasonic, a Canon, a DJI or an iPhone. Each camera
    writes it at a different code, so if the five decodes are right all five
    codes come out of the graph as the same Rec.709 code Apple Log's does.
    """
    ref = _grey_out("apple_log")
    for name in cg.CAMERA_LOG_INPUTS:
        code = C.CAMERA_LOGS[name].grey_code()
        got = float(H.render_patches(_cfg(input=name),
                                     {"grey18": [code] * 3})["grey18"][0])
        ctx.note(f"{name}: code {code:.4f} in, {got:.4f} out "
                 f"(apple_log {ref:.4f})")
        ctx.expect_close(f"{name} 18% grey lands where Apple Log's lands",
                         got, ref, ANCHOR_TOL)


def test_the_direct_path_reaches_the_same_place_for_a_camera_log_too(ctx):
    """working_space "direct" loads a per input cube instead of the DWG pair,
    so this is the same assertion through a different cube: it catches a
    direct cube baked from the wrong curve or the wrong gamut."""
    ref = _grey_out("apple_log", working_space="direct")
    for name in cg.CAMERA_LOG_INPUTS:
        code = C.CAMERA_LOGS[name].grey_code()
        got = float(H.render_patches(_cfg(input=name, working_space="direct"),
                                     {"grey18": [code] * 3})["grey18"][0])
        ctx.note(f"direct {name}: {got:.4f} against apple_log {ref:.4f}")
        ctx.expect_close(f"direct: {name} 18% grey matches Apple Log's",
                         got, ref, ANCHOR_TOL)


def test_one_stop_doubles_the_linear_value_for_every_camera_log(ctx):
    """A stop is a doubling of light on all five, measured through ffmpeg.

    None of these curves can spend a stop as a code offset the way Apple Log
    does: every one has a linear toe and an offset inside its logarithm, so an
    offset would be a different number of stops at every code. The engine
    decodes, scales the light and re-encodes with the vendor's own formula,
    and this is what says that is what actually happens.

    The tolerance is 1 percent. Of that, 0.27 percent at one stop and 0.54 at
    two is a systematic every input in this engine shares: ffmpeg's `lut`
    filter reports maxval as 255 << 8 (65280) on a 16 bit RGB buffer rather
    than 65535, so a code the expression writes as 1.0 lands at 0.9961. It
    is the same 0.4 percent the HLG, PQ and Apple Log measurements above
    carry, and it is not this stage's to fix.
    """
    for name in cg.CAMERA_LOG_INPUTS:
        codes = _camera_codes(name)
        for stops in (1.0, -1.0):
            ratio, n = _stop_ratios(name, stops, codes)
            want = 2.0 ** stops
            worst = float(np.abs(ratio / want - 1.0).max())
            ctx.note(f"{name} {stops:+.0f} stop: ratio {ratio.min():.4f} to "
                     f"{ratio.max():.4f} on {n} of {len(codes)} patches")
            ctx.expect_lt(f"{name}: {stops:+.0f} stop is a factor of {want:g}",
                          worst, 0.01)


def test_the_log_stage_writes_each_camera_logs_own_formula(ctx):
    """The graph carries the expression the measurement above measured, on all
    three channels, and it is a different expression per input rather than one
    shared string with the constants swapped in the wrong places."""
    seen = {}
    for name in cg.CAMERA_LOG_INPUTS:
        cfg = H.patch(H.defaults(),
                      {"convert": {"exposure": 0.5, "input": name}})
        chain = cg.f_log_stage(cfg, _info("", "bt2020"))
        luts = [f for f in chain if f.startswith("lut=")]
        ctx.expect_eq(f"{name}: one exposure node", len(luts), 1)
        want = cg.exposure_expr(name, 0.5)
        ctx.expect_true(f"{name}: on all three channels",
                        luts[0] == f"lut=r='{want}':g='{want}':b='{want}'",
                        luts[0][:80])
        seen[name] = want
    ctx.expect_eq("five inputs, five different expressions",
                  len(set(seen.values())), len(cg.CAMERA_LOG_INPUTS))
    ctx.expect_true("and none of them is Apple Log's code offset",
                    all(cg.exposure_expr("apple_log", 0.5) != v
                        for v in seen.values()),
                    list(seen)[0])


def test_auto_never_answers_with_a_camera_log(ctx):
    """The rule that keeps every existing grade byte identical.

    A camera log file is tagged the way an Apple Log file is: BT.2020
    primaries and no transfer tag the standards define. So auto cannot tell
    them apart, does not try, and keeps answering apple_log. These five are
    set by hand or not at all.
    """
    tags = [("no transfer tag at all", _info("", "bt2020")),
            ("a bt2020-10 transfer", _info("bt2020-10", "bt2020")),
            ("nothing tagged", _info()),
            ("an unknown transfer", _info("log316", "bt2020"))]
    for label, info in tags:
        got, _msgs = cg.resolve_input(H.defaults(), info)
        ctx.expect_true(f"auto on {label} is not a camera log",
                        got not in cg.CAMERA_LOG_INPUTS, got)
    for name in cg.CAMERA_LOG_INPUTS:
        ctx.expect_true(f"{name} is not reachable from any transfer tag",
                        name not in cg.TRANSFER_INPUTS.values(), name)
        ctx.expect_true(f"but {name} is an accepted explicit value",
                        name in cg.INPUTS, cg.INPUTS)


def test_a_camera_log_on_working_space_rec709_is_refused(ctx):
    """The same refusal apple_log gets, for the same reason.

    working_space "rec709" undoes no curve at all. Naming a camera log says in
    so many words that the source HAS one, so that pairing renders a flat grey
    picture with nothing on screen to say why, which is what a refusal is for.
    The other two working spaces are fine and say nothing.
    """
    for name in cg.CAMERA_LOG_INPUTS:
        try:
            cg.check_source_space(_cfg(input=name, working_space="rec709"),
                                  _info("", "bt2020"))
            ctx.check(False, f"{name} on working_space rec709 was allowed")
        except cg.GradeError as exc:
            ctx.expect_true(f"{name} on working_space rec709 is refused by name",
                            name in str(exc), str(exc)[:90])
        for ws in ("dwg", "direct"):
            src, warns = cg.check_source_space(
                _cfg(input=name, working_space=ws), _info("", "bt2020"))
            ctx.expect_eq(f"{name} on {ws} is fine", src, name)
            ctx.expect_eq(f"{name} on {ws}: nothing to warn about",
                          len(warns), 0)


# --------------------------------------------------------------------------
# (g) the --input-space flag (contract G8)
# --------------------------------------------------------------------------

PY = str(H.CONTENT / ".venv" / "bin" / "python")
ENGINE = str(H.GRADE / "cinegrade.py")

# Every subcommand that builds a grade from the shared flags. `orient` is here
# because it answers "what IS this file", which is the question the flag is
# about, even though it renders no grade.
GRADE_COMMANDS = ("render", "still", "compare", "scopes", "stats", "orient",
                  "sweep")


def _cli(args):
    return subprocess.run([PY, ENGINE] + args, capture_output=True, text=True)


def test_input_space_is_on_every_grade_command(ctx):
    """--input-space, not --input: every subcommand already has an `input`
    positional for the clip, and argparse would make one shadow the other.
    That is why contract G1 left the flag out; this is it going back in."""
    for cmd in GRADE_COMMANDS:
        r = _cli([cmd, "--help"])
        ctx.expect_eq(f"{cmd} --help exits clean", r.returncode, 0)
        ctx.expect_true(f"{cmd} carries --input-space",
                        "--input-space" in r.stdout, r.stdout[:200])
        ctx.expect_true(f"{cmd} does not carry a bare --input",
                        "--input " not in r.stdout and "--input=" not in r.stdout,
                        r.stdout[:200])
    bad = _cli(["still", str(H.CLIP_A), "-o", "/dev/null",
                "--input-space", "redlog"])
    ctx.expect_true("an unknown value is refused by argparse",
                    bad.returncode != 0 and "redlog" in bad.stderr,
                    bad.stderr[-200:])


def test_input_space_changes_what_the_grade_decodes(ctx):
    """The flag has to reach cfg["convert"]["input"] and not merely parse.

    Measured through `stats`, which renders the frame through the whole graph
    and prints the same numbers POST /api/stats returns: decoding an Apple Log
    clip as V-Log instead reads every code as a different amount of light, so
    the median luma moves. `sweep` is measured the same way because it builds
    its config on a different path (one per swept value) and would otherwise
    ignore the flag.
    """
    src = str(H.CLIP_A)
    base = _cli(["stats", src, "--time", "1.0", "--json"])
    ctx.expect_true("stats runs", base.returncode == 0, base.stderr[-300:])
    forced = _cli(["stats", src, "--time", "1.0", "--json",
                   "--input-space", "vlog"])
    ctx.expect_true("stats with --input-space runs", forced.returncode == 0,
                    forced.stderr[-300:])
    a = json.loads(base.stdout)["stats"]["luma"]["p50"]
    b = json.loads(forced.stdout)["stats"]["luma"]["p50"]
    ctx.note(f"stats p50 luma: apple_log {a:.4f}, forced vlog {b:.4f}")
    ctx.expect_gt("decoding the clip as V-Log is a different picture",
                  abs(b - a), 0.01)

    sw = ["sweep", src, "--time", "1.0", "--param", "convert.exposure",
          "--values", "0", "--json"]
    s0 = _cli(sw)
    ctx.expect_true("sweep runs", s0.returncode == 0, s0.stderr[-300:])
    s1 = _cli(sw + ["--input-space", "vlog"])
    ctx.expect_true("sweep with --input-space runs", s1.returncode == 0,
                    s1.stderr[-300:])
    c = json.loads(s0.stdout)["results"][0]["stats"]["luma"]["p50"]
    d = json.loads(s1.stdout)["results"][0]["stats"]["luma"]["p50"]
    ctx.note(f"sweep p50 luma: apple_log {c:.4f}, forced vlog {d:.4f}")
    ctx.expect_gt("sweep honours it too", abs(d - c), 0.01)
    ctx.expect_close("and sweep and stats agree with each other", d, b, 0.01)


def test_orient_json_reports_what_the_file_resolves_to(ctx):
    """`orient --json` is the one command an agent runs BEFORE it knows what
    a file is, so it reports the resolution next to the rotation tag, with
    --input-space honoured. Additive: every field contract G4 put there is
    still there."""
    src = str(H.CLIP_A)
    r = _cli(["orient", src, "--json"])
    ctx.expect_true("orient --json runs", r.returncode == 0, r.stderr[-300:])
    out = json.loads(r.stdout)
    for key in ("tag", "candidates", "rotation_tag_suspect"):
        ctx.expect_true(f"orient --json still carries {key}", key in out,
                        sorted(out))
    ctx.expect_eq("and now what the file resolves to",
                  out.get("resolved_input"), "apple_log")
    ctx.expect_eq("on the primaries that input's standard puts it on",
                  out.get("source_primaries"), "bt2020")

    forced = json.loads(_cli(["orient", src, "--json",
                              "--input-space", "clog3"]).stdout)
    ctx.expect_eq("--input-space is honoured here too",
                  forced.get("resolved_input"), "clog3")
    ctx.expect_eq("and it brings its own gamut",
                  forced.get("source_primaries"), "cinemagamut")


def register(suite):
    g = "input"
    suite.add(g, "auto_reads_the_files_own_tags",
              test_auto_reads_the_files_own_tags,
              doc="every tag combination in the contract, warnings included")
    suite.add(g, "an_explicit_input_is_never_second_guessed",
              test_an_explicit_input_is_never_second_guessed,
              doc="the caller beats the metadata, and a bad name is refused")
    suite.add(g, "primaries_are_read_separately_from_the_transfer",
              test_primaries_are_read_separately_from_the_transfer,
              doc="three tags, three questions")
    suite.add(g, "the_guard_stops_refusing_a_bt2020_delivery_file",
              test_the_guard_stops_refusing_a_bt2020_delivery_file,
              doc="HLG is allowed, camera log is still refused")
    suite.add(g, "the_engine_and_colorlib_agree_on_every_constant",
              test_the_engine_and_colorlib_agree_on_every_constant,
              doc="two copies of the BT.2100 and ST 2084 constants, one value")
    suite.add(g, "bt2408_is_where_the_hdr_anchor_comes_from",
              test_bt2408_is_where_the_hdr_anchor_comes_from,
              doc="the OOTF reproduces 26 and 203 cd/m2 on its own")
    suite.add(g, "every_input_has_the_cubes_it_names",
              test_every_input_has_the_cubes_it_names,
              doc="the generator and the engine name the same files")
    suite.add(g, "eighteen_percent_grey_lands_in_the_same_place_for_every_input",
              test_eighteen_percent_grey_lands_in_the_same_place_for_every_input,
              doc="the whole contract, measured through the real graph")
    suite.add(g, "the_direct_path_reaches_the_same_place_too",
              test_the_direct_path_reaches_the_same_place_too,
              doc="same assertion through the per input direct cubes")
    suite.add(g, "a_ramp_keeps_its_order_and_its_ends",
              test_a_ramp_keeps_its_order_and_its_ends,
              doc="an HLG ramp stays monotone across the OETF's own boundary")
    suite.add(g, "one_stop_doubles_the_linear_value_for_every_input",
              test_one_stop_doubles_the_linear_value_for_every_input,
              doc="a stop is light, not code, on hlg, pq and rec709 too")
    suite.add(g, "apple_logs_own_stop_is_left_exactly_as_it_was",
              test_apple_logs_own_stop_is_left_exactly_as_it_was,
              doc="the old constant offset, still good above the toe")
    suite.add(g, "the_log_stage_writes_the_expression_it_advertises",
              test_the_log_stage_writes_the_expression_it_advertises,
              doc="the graph carries the formula the ramp measured")
    suite.add(g, "apple_log_graph_text_is_exactly_what_it_was",
              test_apple_log_graph_text_is_exactly_what_it_was,
              doc="every string on the Apple Log path, written out in full")
    suite.add(g, "a_caller_with_no_probe_still_gets_apple_log",
              test_a_caller_with_no_probe_still_gets_apple_log,
              doc="hand-built info dicts and missing ones keep their answer")

    # contract G8: the five camera logs
    suite.add(g, "the_camera_log_constants_are_the_published_ones",
              test_the_camera_log_constants_are_the_published_ones,
              doc="colorlib, cinegrade and colour-science agree on all five")
    suite.add(g, "eighteen_percent_grey_is_where_each_document_says_it_is",
              test_eighteen_percent_grey_is_where_each_document_says_it_is,
              doc="S-Log3 code 420, LogC3 code 400, V-Log 42.3, CLog3 32.8")
    suite.add(g, "every_camera_gamut_is_the_published_one",
              test_every_camera_gamut_is_the_published_one,
              doc="the chromaticities, and white stays white into BT.2020")
    suite.add(g, "every_camera_log_has_the_cubes_it_names",
              test_every_camera_log_has_the_cubes_it_names,
              doc="one cube set per camera log, no primaries infix")
    suite.add(g, "a_camera_log_grey_card_lands_where_apple_logs_does",
              test_a_camera_log_grey_card_lands_where_apple_logs_does,
              doc="the whole of G8, measured through the real graph")
    suite.add(g, "the_direct_path_reaches_the_same_place_for_a_camera_log_too",
              test_the_direct_path_reaches_the_same_place_for_a_camera_log_too,
              doc="same assertion through the per input direct cubes")
    suite.add(g, "one_stop_doubles_the_linear_value_for_every_camera_log",
              test_one_stop_doubles_the_linear_value_for_every_camera_log,
              doc="a stop is light on all five, through the real ffmpeg lut")
    suite.add(g, "the_log_stage_writes_each_camera_logs_own_formula",
              test_the_log_stage_writes_each_camera_logs_own_formula,
              doc="five inputs, five expressions, none of them Apple Log's")
    suite.add(g, "auto_never_answers_with_a_camera_log",
              test_auto_never_answers_with_a_camera_log,
              doc="no container tag tells them apart, so auto does not guess")
    suite.add(g, "a_camera_log_on_working_space_rec709_is_refused",
              test_a_camera_log_on_working_space_rec709_is_refused,
              doc="the same refusal apple_log gets, for the same reason")
    suite.add(g, "input_space_is_on_every_grade_command",
              test_input_space_is_on_every_grade_command,
              doc="the CLI flag contract G1 left out, on all seven")
    suite.add(g, "input_space_changes_what_the_grade_decodes",
              test_input_space_changes_what_the_grade_decodes,
              doc="it reaches the config on the stats and sweep paths")
    suite.add(g, "orient_json_reports_what_the_file_resolves_to",
              test_orient_json_reports_what_the_file_resolves_to,
              doc="the resolution next to the rotation tag, flag honoured")
