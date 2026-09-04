"""Group 1: colour science anchors.

Every expected number here is lifted from the code or the docs, never chosen to
make a test pass:

  README.md "Color science" table   18% grey 0.4883 log -> 0.3919 Rec.709,
                                    90% white -> 0.7993, 4 stops over -> 0.9424,
                                    1% shadow -> 0.0574
  cinegrade.MID_GREY_CODE           {"dwg": 0.3360, "direct": 0.3919}
  cinegrade.APPLE_LOG_STOP          0.08492 code values per stop
  colorlib.apple_log_to_scene_linear  code 0.0 decodes negative, must clamp
  AGENTS.md                         "direct clipped 0.88% against dwg's 0.72%"

The anchors are measured through ffmpeg, not through the python that generated
the LUTs, so a broken .cube or a wrong interpolation mode fails here too.
"""

from __future__ import annotations

import numpy as np

import harness as H
from harness import C, CST, cg


from colour.models import log_encoding_AppleLogProfile as log_enc
from colour.models import log_decoding_AppleLogProfile as log_dec

# Apple Log code values for the scene-linear anchors the README publishes.
GREY18 = float(log_enc(0.18))
WHITE90 = float(log_enc(0.90))
OVER4 = float(log_enc(0.18 * 16))
SHADOW1 = float(log_enc(0.01))
GREY36 = float(log_enc(0.36))

ANCHOR_PATCHES = {
    "code0": [0.0, 0.0, 0.0],
    "code1": [1.0, 1.0, 1.0],
    "grey18": [GREY18] * 3,
    "white90": [WHITE90] * 3,
    "over4": [OVER4] * 3,
    "shadow1": [SHADOW1] * 3,
    "grey36": [GREY36] * 3,
}

# A 65-cube sampled tetrahedrally lands within a fraction of a code value of the
# closed-form transform. 0.0015 is about 0.4 of an 8-bit code, tight enough to
# catch a wrong tone map or a wrong encode and loose enough to ignore lattice
# interpolation.
ANCHOR_TOL = 0.0015


def _direct(cfg_patch=None):
    cfg = H.patch(H.defaults(), {"convert": {"working_space": "direct"}},
                  cfg_patch or {})
    return H.render_patches(cfg, ANCHOR_PATCHES)


def test_readme_anchor_table(ctx):
    """The four published Rec.709 anchors, checked against the encode they
    were actually measured under.

    README's table gives 0.3919 / 0.7993 / 0.9424 / 0.0574. Those are gamma24
    numbers, not rec709a ones, and DEFAULTS ships encode rec709a. The table was
    written when the direct path ignored convert.encode and always loaded the
    unsuffixed gamma24 cube, so it described what that bug produced. Both
    encodes are asserted here: the published column against the published
    numbers, and the shipped default against closed-form maths.
    """
    got24 = _direct({"convert": {"encode": "gamma24"}})
    ctx.note(f"log code inputs: grey18 {GREY18:.4f}, white90 {WHITE90:.4f}, "
             f"4-over {OVER4:.4f}, shadow1 {SHADOW1:.4f}")
    for label, key, want in (("18% grey", "grey18", 0.3919),
                             ("90% white", "white90", 0.7993),
                             ("4 stops over grey", "over4", 0.9424),
                             ("1% deep shadow", "shadow1", 0.0574)):
        ctx.expect_close(f"gamma24: {label} -> Rec.709 (README {want})",
                         float(got24[key][0]), want, ANCHOR_TOL)
    ctx.expect_lt("4 stops over does not clip", float(got24["over4"][0]), 1.0,
                  margin=0.005)

    # The default lane. Expected values come from make_cst's closed form, so a
    # mis-baked cube or a wrong graph cannot agree with the maths by accident.
    gotA = _direct()
    ctx.note("README's table does not name an encode and does not match the "
             "shipped default (encode rec709a): grey18 is "
             f"{float(gotA['grey18'][0]):.4f} there, not 0.3919")
    for label, key, lin in (("18% grey", "grey18", 0.18),
                            ("90% white", "white90", 0.90),
                            ("4 stops over grey", "over4", 0.18 * 16),
                            ("1% deep shadow", "shadow1", 0.01)):
        want = float(CST.apple_log_to_rec709(
            np.array([[[float(log_enc(lin))] * 3]]), "aces", "rec709a")[0, 0, 0])
        ctx.expect_close(f"rec709a: {label} -> Rec.709 (closed form {want:.4f})",
                         float(gotA[key][0]), want, ANCHOR_TOL)


def test_mid_grey_code_table(ctx):
    """cinegrade.MID_GREY_CODE is the contrast pivot; it must be the real
    mid grey of each space or contrast silently shifts exposure."""
    ctx.note(f"cinegrade.MID_GREY_CODE = {cg.MID_GREY_CODE}")
    # DWG: measured through the CST IN LUT the engine actually loads.
    dwg = H.render_through_lut(H.GRADE / "luts" / "technical" / "AppleLog_to_DWG.cube",
                               ANCHOR_PATCHES)
    ctx.expect_close("18% grey in the DWG working space (MID_GREY_CODE dwg 0.3360)",
                     float(dwg["grey18"][0]), cg.MID_GREY_CODE["dwg"], ANCHOR_TOL)
    # Closed form, so a bad LUT and bad maths cannot agree with each other.
    closed = C.dwg_encode(np.clip(C.apply_matrix(
        np.array([[0.18, 0.18, 0.18]]), C.BT2020_TO_DWG), 0.0, None))
    ctx.expect_close("closed-form DWG mid grey matches the table",
                     float(closed[0][0]), cg.MID_GREY_CODE["dwg"], 0.0001)
    # direct: primaries run between CST IN and CST OUT, and CST IN is empty on
    # this path, so the working space IS Apple Log and the pivot has to be the
    # Apple Log code for 18% grey. It read 0.3919 (the Rec.709 OUTPUT code)
    # until the pivot fix, which is what made contrast shift exposure there.
    ctx.expect_close("MID_GREY_CODE direct equals the Apple Log code for 18% grey",
                     cg.MID_GREY_CODE["direct"], GREY18, ANCHOR_TOL)
    ctx.note(f"direct pivot {cg.MID_GREY_CODE['direct']} is the working-space "
             f"code, not the {float(_direct()['grey18'][0]):.4f} it renders to")


def test_dwg_two_lut_roundtrip_matches_direct(ctx):
    """The working-space pair must be a factorisation of the single LUT.

    Both encodes are compared now that the direct path honours convert.encode.
    This used to pin both sides to gamma24 because direct ignored the setting,
    which meant the default lane (rec709a) was never actually compared.
    """
    worst_all = 0.0
    for enc in ("rec709a", "gamma24"):
        direct = _direct({"convert": {"encode": enc}})
        dwg = H.render_patches(
            H.patch(H.defaults(), {"convert": {"working_space": "dwg",
                                               "encode": enc}}),
            ANCHOR_PATCHES)
        worst = max(float(np.abs(np.asarray(direct[n]) - np.asarray(dwg[n])).max())
                    for n in ANCHOR_PATCHES)
        worst_all = max(worst_all, worst)
        ctx.note(f"{enc}: grey18 direct {float(direct['grey18'][0]):.4f} "
                 f"vs dwg {float(dwg['grey18'][0]):.4f}, worst anchor {worst:.5f}")
    # Two chained 65-cubes accumulate a little more interpolation error than one.
    ctx.expect_le("worst anchor disagreement between the one-LUT and two-LUT paths",
                  worst_all, 0.004)

    # And the closed form of both, which has no interpolation error at all.
    grid = C.identity_grid(17)
    for enc in ("rec709a", "gamma24"):
        one = CST.apple_log_to_rec709(grid, "aces", enc)
        two = CST.dwg_to_rec709(CST.apple_log_to_dwg(grid), "aces", enc)
        ctx.expect_le(f"closed-form one-LUT vs two-LUT max error, {enc}",
                      float(np.abs(one - two).max()), 1e-6)


def test_rec709a_and_gamma24_differ_in_the_documented_direction(ctx):
    """rec709a is a 1/2.2 encode, gamma24 a 1/2.4 one.

    For the same display-linear value, the smaller exponent (1/2.4) yields the
    higher code, so a gamma24 file read on a 2.2 display is the "washed out in
    QuickTime" case AGENTS.md warns about. The direction, not just the
    difference, is what pins which is which.
    """
    x = np.linspace(0.02, 0.98, 25)
    g24 = C.encode_gamma24(x)
    r709a = C.encode_gamma22(x)
    ctx.expect_true("gamma24 code >= rec709a code at every display-linear value",
                    bool(np.all(g24 > r709a)),
                    f"min margin {float((g24 - r709a).min()):+.5f}, "
                    f"max margin {float((g24 - r709a).max()):+.5f}")

    # Same statement, measured through the engine on the dwg path where the
    # encode choice is honoured.
    out = {}
    for enc in ("rec709a", "gamma24"):
        cfg = H.patch(H.defaults(), {"convert": {"working_space": "dwg",
                                                 "encode": enc}})
        out[enc] = H.render_patches(cfg, ANCHOR_PATCHES)
    for name in ("shadow1", "grey18", "white90"):
        a = float(out["gamma24"][name][0])
        b = float(out["rec709a"][name][0])
        ctx.expect_gt(f"{name}: gamma24 output brighter than rec709a", a, b,
                      margin=0.002)


def test_apple_log_zero_clamps(ctx):
    """Apple Log code 0.0 decodes to a negative linear value.

    colorlib clamps it before the gamut matrix. Left in, the negative leaks
    through the matrix into the other two channels and the shadows go coloured.
    """
    raw = float(log_dec(np.array([0.0]))[0])
    clamped = float(C.apple_log_to_scene_linear(np.array([0.0]))[0])
    ctx.expect_lt("raw Apple Log decode of code 0.0 really is negative", raw, 0.0)
    ctx.expect_close("clamped decode of code 0.0", clamped, 0.0, 1e-12)
    ctx.note(f"raw decode {raw:.6f} -> clamped {clamped:.6f}")

    for ws in ("direct", "dwg"):
        cfg = H.patch(H.defaults(), {"convert": {"working_space": ws}})
        got = H.render_patches(cfg, ANCHOR_PATCHES)
        v = np.asarray(got["code0"])
        ctx.expect_le(f"{ws}: code 0.0 renders at or below black", float(v.max()),
                      0.0005)
        ctx.expect_le(f"{ws}: code 0.0 stays neutral (no channel spread)",
                      float(v.max() - v.min()), 0.0005)


def test_exposure_is_a_log_domain_stop(ctx):
    """APPLE_LOG_STOP: +1 stop of exposure is a flat code offset on log.

    Apple Log has a toe, so a single constant offset can only be an exact stop
    in the pure-log part of the curve. What has to hold is that a stop is a
    stop where footage actually lives, and that the engine and the curve agree
    about it. Bounded at a tenth of a stop, which is below what a colourist
    would call a mis-exposure.
    """
    from colour.models.rgb.transfer_functions.apple_log_profile import (
        CONSTANTS_APPLE_LOG_PROFILE as K)
    published = float(K["gamma"])
    ctx.note(f"APPLE_LOG_STOP = {cg.APPLE_LOG_STOP}; colour-science publishes "
             f"gamma = {published:.8f} (delta {cg.APPLE_LOG_STOP - published:+.8f}); "
             f"the code comment cites 0.08492 as that published gamma")

    for lin in (0.09, 0.18, 0.36, 0.72):
        got = float(log_dec(np.array([float(log_enc(lin)) + cg.APPLE_LOG_STOP]))[0])
        delivered = float(np.log2(got / lin))
        ctx.note(f"at {lin:.3f} scene linear, +1 stop delivers {delivered:+.4f} stops")
    mid = float(log_dec(np.array([GREY18 + cg.APPLE_LOG_STOP]))[0])
    ctx.expect_close("stops delivered by +1 stop of exposure at 18% grey",
                     float(np.log2(mid / 0.18)), 1.0, 0.1)

    # And end to end: an 18% grey pushed a stop should read as a 36% grey.
    base = H.render_patches(H.defaults(), ANCHOR_PATCHES)
    up = H.render_patches(
        H.patch(H.defaults(), {"convert": {"exposure": 1.0}}), ANCHOR_PATCHES)
    ctx.note(f"grey18 +1 stop renders {float(up['grey18'][0]):.5f}, "
             f"an unpushed grey36 renders {float(base['grey36'][0]):.5f}")
    ctx.expect_close("18% grey +1 stop lands on an unpushed 36% grey",
                     float(up["grey18"][0]), float(base["grey36"][0]), 0.012)


def _pivot_case(ctx, ws):
    base = H.patch(H.defaults(), {"convert": {"working_space": ws}})
    flat = float(H.render_patches(base, ANCHOR_PATCHES)["grey18"][0])
    hard = float(H.render_patches(
        H.patch(base, {"primaries": {"contrast": 1.6}}),
        ANCHOR_PATCHES)["grey18"][0])
    ctx.note(f"{ws}: f_primaries pivots on MID_GREY_CODE['{ws}'] = "
             f"{cg.MID_GREY_CODE[ws]}; 18% grey {flat:.4f} -> {hard:.4f} "
             f"at contrast 1.6")
    ctx.expect_close(f"{ws}: contrast 1.6 leaves 18% grey where it was",
                     hard, flat, 0.01)


def test_contrast_pivots_on_mid_grey_dwg(ctx):
    """Contrast must not move mid grey. That is what a pivot is for."""
    _pivot_case(ctx, "dwg")


def test_contrast_pivots_on_mid_grey_direct(ctx):
    """Same invariant on the direct path.

    build_graph runs PRIMARIES between CST IN and CST OUT. On the direct path
    CST IN is empty, so contrast is applied to Apple Log code values, where 18%
    grey sits at 0.4883. The pivot it uses is MID_GREY_CODE['direct'] = 0.3919,
    which is the Rec.709 OUTPUT code for mid grey, not the input code.
    """
    _pivot_case(ctx, "direct")


def _white_ceiling(ws: str) -> float:
    """The brightest output this working space can produce from legal log.

    Apple Log code 1.0 is 12.0 scene linear and tone maps to about 0.992.
    DaVinci Intermediate code 1.0 is 100.0 scene linear and tone maps past 1.0,
    so it lands on a hard 255. Counting samples "at 255" therefore compares two
    different ceilings and inverts the answer. Measuring against each path's own
    white point counts the thing that matters: pixels with no highlight
    separation left.
    """
    cfg = H.patch(H.defaults(), {"convert": {"working_space": ws}})
    return float(np.max(H.render_patches(cfg, ANCHOR_PATCHES)["code1"]))


def test_direct_path_clips_more_than_dwg(ctx):
    """AGENTS.md headroom claim: DWG holds 100.0 scene linear at code 1.0
    against Apple Log's 12.0, so a push made inside the working space has about
    three more stops before the space itself clips.

    The push has to be a working-space one for the claim to mean anything.
    convert.exposure is applied in the log stage, ahead of CST IN, so both paths
    receive the same values and clip identically; primaries.gain multiplies code
    values after CST IN, which is where the headroom lives. Encode is held equal
    on both sides so the only variable is the working space.
    """
    ceilings, lost = {}, {}
    for ws in ("dwg", "direct"):
        base = {"convert": {"working_space": ws}}
        ceilings[ws] = _white_ceiling(ws)
        cfg = H.patch(H.defaults(), base, {"primaries": {"gain": 1.8}})
        img = H.render(H.CLIP_A, cfg, H.TIME_A)
        lost[ws] = float((img.astype(np.float64) >= ceilings[ws] * 255.0 - 0.5).mean())
        ctx.note(f"{ws}: white ceiling {ceilings[ws]:.5f}, gain 1.8 pins "
                 f"{lost[ws] * 100:.3f}% of samples at it")
    ctx.expect_gt("direct loses more highlight than dwg on a working-space gain",
                  lost["direct"], lost["dwg"], margin=0.02)

    # AGENTS.md quotes 0.88% direct against 0.72% dwg for a +1.2 stop, 1.45
    # contrast push. That measurement predates the contrast pivot fix: the old
    # direct pivot sat 0.096 below real grey, so contrast dragged the whole
    # image up and the extra clipping came from the pivot, not from headroom.
    # With the pivot correct the two paths clip the same on that push, so the
    # published pair of numbers is recorded here rather than asserted.
    old_push = {"convert": {"exposure": 1.2}, "primaries": {"contrast": 1.45}}
    same = {}
    for ws in ("dwg", "direct"):
        cfg = H.patch(H.defaults(), {"convert": {"working_space": ws}}, old_push)
        img = H.render(H.CLIP_A, cfg, H.TIME_A)
        same[ws] = float((img.astype(np.float64) >= ceilings[ws] * 255.0 - 0.5).mean())
    ctx.note(f"AGENTS.md's +1.2 stop 1.45 contrast push now clips "
             f"dwg {same['dwg'] * 100:.3f}% vs direct {same['direct'] * 100:.3f}% "
             f"(published: 0.72% vs 0.88%), so those figures no longer reproduce")


def test_direct_path_honours_convert_encode(ctx):
    """convert.encode must change the output on every working space.

    f_convert_out used to build 'AppleLog_to_Rec709_{tonemap}.cube' on the
    direct path, with no encode in the name, so the choice could not reach the
    LUT and the shipped default (rec709a) silently rendered gamma24. Per encode
    direct cubes fixed that. Kept as a live test because the failure was
    invisible in the picture: both encodes look plausible, just wrong.
    """
    a = H.render_patches(
        H.patch(H.defaults(), {"convert": {"working_space": "direct",
                                           "encode": "rec709a"}}),
        ANCHOR_PATCHES)
    b = H.render_patches(
        H.patch(H.defaults(), {"convert": {"working_space": "direct",
                                           "encode": "gamma24"}}),
        ANCHOR_PATCHES)
    delta = max(float(np.abs(np.asarray(a[n]) - np.asarray(b[n])).max())
                for n in ANCHOR_PATCHES)
    ctx.note(f"direct rec709a grey18 {float(a['grey18'][0]):.4f}, "
             f"gamma24 grey18 {float(b['grey18'][0]):.4f}")
    ctx.expect_gt("switching convert.encode changes the direct path output",
                  delta, 0.002)


def register(suite):
    g = "color"
    suite.add(g, "readme_anchor_table", test_readme_anchor_table,
              doc="the four published Rec.709 anchors on the direct aces path")
    suite.add(g, "mid_grey_code_table", test_mid_grey_code_table,
              doc="MID_GREY_CODE matches the real mid grey of each space")
    suite.add(g, "dwg_roundtrip_matches_direct",
              test_dwg_two_lut_roundtrip_matches_direct,
              doc="the DWG CST pair factorises the single direct LUT")
    suite.add(g, "rec709a_vs_gamma24_direction",
              test_rec709a_and_gamma24_differ_in_the_documented_direction,
              doc="gamma24 encodes higher than rec709a at equal display linear")
    suite.add(g, "apple_log_zero_clamps", test_apple_log_zero_clamps,
              doc="code 0.0 clamps instead of going negative through the matrix")
    suite.add(g, "exposure_is_a_log_stop", test_exposure_is_a_log_domain_stop,
              doc="APPLE_LOG_STOP: +1 stop equals doubling scene linear")
    suite.add(g, "contrast_pivot_dwg", test_contrast_pivots_on_mid_grey_dwg,
              doc="contrast must not shift 18% grey in the DWG working space")
    suite.add(g, "contrast_pivot_direct", test_contrast_pivots_on_mid_grey_direct,
              doc="contrast must not shift 18% grey on the direct path")
    suite.add(g, "direct_clips_more_than_dwg", test_direct_path_clips_more_than_dwg,
              doc="AGENTS.md headroom claim: direct 0.88% vs dwg 0.72%")
    suite.add(g, "direct_honours_convert_encode",
              test_direct_path_honours_convert_encode,
              doc="convert.encode must reach the direct path LUT choice")
