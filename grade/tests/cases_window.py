"""Group 8: the power window, the shape half of a secondary.

The window is a matte, and a matte is only worth anything if the same formula
lands on the same pixels everywhere it is evaluated. It is written three times
over the life of this feature: once in numpy (cinegrade.window_matte, the
reference), once as an ffmpeg geq expression that bakes the cached PNG the
render actually reads, and once more in a GPU shader when that port lands. So
the tests here are mostly about agreement rather than about looks:

  identity    with the window off the graph text, the ffmpeg inputs and the
              rendered bytes are the ones that shipped before the stage
              existed. A new node that changes an old render is a regression
              however good it looks.
  gating      inside the shape the secondary is applied in full, outside it is
              not applied at all. Both halves are measured, and "not at all"
              means exactly zero, not nearly zero.
  agreement   the geq matte equals the numpy matte, and the same fractions
              describe the same shape at 960 wide and at full width.

The gating numbers are deliberately absolute. An earlier build of this stage
was off by a mean of 0.186 of 255 inside a fully open window, because ffmpeg
expands an 8-bit matte to 16 bits with a left shift, so matte code 255 reached
maskedmerge as 65280 of 65535 and the correction landed at 99.61%. Nothing that
looks at a picture would ever have caught it; asserting exact zero did.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy

import numpy as np

import harness as H
from harness import cg

CLIP = H.CLIP_A
T = H.TIME_A

# A correction big enough that "applied" and "not applied" are never a judgement
# call: the hue window is wide, saturation is pulled to zero and luma is dropped
# hard, which moves the frame by a mean of about 55 of 255 where it lands.
STRONG_SECONDARY = {"secondary": {
    "enabled": True, "hue_center": 30.0, "hue_width": 300.0, "hue_soft": 30.0,
    "sat_low": 0.0, "sat_soft": 0.3, "lum_soft": 0.3,
    "sat_gain": 0.0, "lum_gain": 0.4}}

# Hard edged on purpose: with no feather every pixel is either fully inside or
# fully outside, so "inside" and "outside" are exact sets and the assertions can
# be exact too.
HARD_ELLIPSE = {"window": {"enabled": True, "shape": "ellipse",
                           "cx": 0.5, "cy": 0.5, "w": 0.5, "h": 0.5,
                           "rotation": 0.0, "softness": 0.0, "invert": False}}


def _sec_base():
    return H.patch(H.defaults(), STRONG_SECONDARY)


def _regions(cfg, width, height):
    """The boolean inside/outside masks for a config's window, from numpy."""
    m = cg.window_matte(cfg, width, height)
    return m == 255, m == 0


def _mean_abs(a, b, where):
    """Mean per-pixel channel-max difference over a region, in 8-bit codes."""
    d = np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2)
    return float(d[where].mean()) if where.any() else 0.0


def _read_gray_png(path, w, h) -> np.ndarray:
    """Read a gray PNG back as uint8, through the harness' own ffmpeg runner so
    the file is registered for the run's cache cleanup."""
    raw = H._run_bytes(["ffmpeg", "-v", "error", "-i", str(path),
                        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"])
    return np.frombuffer(raw[:w * h], np.uint8).reshape(h, w)


# --------------------------------------------------------------------------
# (a) the stage is invisible when it is off
# --------------------------------------------------------------------------

def test_disabled_window_is_byte_identical(ctx):
    """A config carrying the window block at its defaults must render exactly
    what a config that has never heard of a window renders.

    Three things are compared, because any one of them alone could pass while
    the render still moved: the filter graph text, the ffmpeg input list (a
    stray extra -i shifts every later input index), and the pixels.
    """
    info = H.info_for(CLIP)
    combos = []
    for radial in (False, True):
        for grain in (False, True):
            for sec in (False, True):
                c = H.patch(H.defaults(), {
                    "fx": {"radial_blur": {"enabled": radial}},
                    "grain": {"enabled": grain},
                    "secondary": {"enabled": sec}})
                combos.append((f"radial={radial} grain={grain} sec={sec}", c))

    graph_diffs, input_diffs = [], []
    for tag, cfg in combos:
        without = deepcopy(cfg)
        without.pop("window")
        if cg.graph_with_mask(cfg, info) != cg.graph_with_mask(without, info):
            graph_diffs.append(tag)
        if (cg.ffmpeg_inputs(str(CLIP), cfg, info)
                != cg.ffmpeg_inputs(str(CLIP), without, info)):
            input_diffs.append(tag)
    ctx.note(f"{len(combos)} configs compared with and without the window block")
    ctx.expect_true("the graph text is unchanged by the window block",
                    not graph_diffs, f"differed: {graph_diffs}")
    ctx.expect_true("the ffmpeg input list is unchanged by the window block",
                    not input_diffs, f"differed: {input_diffs}")

    with_block = _sec_base()
    without_block = deepcopy(with_block)
    without_block.pop("window")
    a = H.render(CLIP, with_block, T)
    b = H.render(CLIP, without_block, T)
    ha = hashlib.sha1(a.tobytes()).hexdigest()[:16]
    hb = hashlib.sha1(b.tobytes()).hexdigest()[:16]
    ctx.note(f"rendered frame hash with the block {ha}, without it {hb}")
    ctx.expect_eq("a disabled window renders the identical frame", ha, hb)


# --------------------------------------------------------------------------
# (b) the window gates the secondary
# --------------------------------------------------------------------------

def test_ellipse_gates_the_secondary(ctx):
    base = _sec_base()
    windowed = H.patch(base, HARD_ELLIPSE)
    plain = H.render(CLIP, H.defaults(), T)          # no secondary at all
    full = H.render(CLIP, base, T)                   # secondary everywhere
    got = H.render(CLIP, windowed, T)
    h, w, _ = got.shape
    inside, outside = _regions(windowed, w, h)
    ctx.note(f"{w}x{h} frame, {int(inside.sum())} pixels inside the ellipse, "
             f"{int(outside.sum())} outside")

    in_vs_plain = _mean_abs(got, plain, inside)
    out_vs_plain = _mean_abs(got, plain, outside)
    in_vs_full = _mean_abs(got, full, inside)
    out_vs_full = _mean_abs(got, full, outside)
    ctx.note(f"vs the ungraded frame: inside {in_vs_plain:.4f}, "
             f"outside {out_vs_plain:.4f} of 255")
    ctx.note(f"vs the un-windowed secondary: inside {in_vs_full:.4f}, "
             f"outside {out_vs_full:.4f} of 255")

    ctx.expect_gt("inside the shape the correction really lands",
                  in_vs_plain, 10.0)
    ctx.expect_close("outside the shape nothing at all is applied",
                     out_vs_plain, 0.0, 0.0)
    ctx.expect_close("inside the shape the correction is applied in full",
                     in_vs_full, 0.0, 0.0)
    ctx.expect_gt("outside the shape the un-windowed grade is visibly different",
                  out_vs_full, 10.0)


# --------------------------------------------------------------------------
# (c) invert
# --------------------------------------------------------------------------

def test_invert_swaps_inside_and_outside(ctx):
    base = _sec_base()
    normal = H.patch(base, HARD_ELLIPSE)
    flipped = H.patch(normal, {"window": {"invert": True}})
    plain = H.render(CLIP, H.defaults(), T)
    n = H.render(CLIP, normal, T)
    f = H.render(CLIP, flipped, T)
    h, w, _ = n.shape
    inside, outside = _regions(normal, w, h)

    n_in, n_out = _mean_abs(n, plain, inside), _mean_abs(n, plain, outside)
    f_in, f_out = _mean_abs(f, plain, inside), _mean_abs(f, plain, outside)
    ctx.note(f"normal window vs ungraded: inside {n_in:.4f}, outside {n_out:.4f}")
    ctx.note(f"inverted window vs ungraded: inside {f_in:.4f}, outside {f_out:.4f}")
    ctx.expect_close("inverted, the inside is left alone", f_in, 0.0, 0.0)
    ctx.expect_gt("inverted, the outside is graded", f_out, 10.0)
    ctx.expect_close("normal, the outside is left alone", n_out, 0.0, 0.0)
    ctx.expect_gt("normal, the inside is graded", n_in, 10.0)

    # The two together must cover the frame exactly once: every pixel is graded
    # by one of them and by only one of them.
    full = H.render(CLIP, base, T)
    union = np.where(inside[..., None], n, f)
    ctx.expect_eq("normal plus inverted reconstructs the un-windowed grade",
                  int(np.abs(union.astype(np.int16)
                             - full.astype(np.int16)).max()), 0)


# --------------------------------------------------------------------------
# (d) rotation
# --------------------------------------------------------------------------

def test_rect_rotated_45_moves_the_corners(ctx):
    """A square window turned 45 degrees drops its own corners.

    Measured on a square frame so the two half axes are equal and the shape is
    a real square: the corner of the unrotated square sits at a normalised
    distance of 1 on both axes, and after a 45 degree turn the same point is at
    sqrt(2) on one of them, which is outside.
    """
    size = 512
    rect = {"window": {"enabled": True, "shape": "rect", "cx": 0.5, "cy": 0.5,
                       "w": 0.5, "h": 0.5, "softness": 0.0, "invert": False}}
    flat = cg.window_matte(rect, size, size)
    turned = cg.window_matte(H.patch(rect, {"window": {"rotation": 45.0}}),
                             size, size)
    half = int(0.5 * size / 2) - 2          # just inside the unrotated corner
    c = size // 2
    corners = [(c - half, c - half), (c - half, c + half),
               (c + half, c - half), (c + half, c + half)]
    flat_vals = [int(flat[y, x]) for x, y in corners]
    turn_vals = [int(turned[y, x]) for x, y in corners]
    ctx.note(f"corner matte codes, unrotated {flat_vals}, at 45 degrees {turn_vals}")
    ctx.expect_true("the unrotated square covers its own corners",
                    all(v == 255 for v in flat_vals), str(flat_vals))
    ctx.expect_true("turned 45 degrees it no longer covers them",
                    all(v == 0 for v in turn_vals), str(turn_vals))

    # Area is preserved by a rotation, so a wrong rotation matrix that also
    # rescaled would be caught here rather than only at the corners.
    a_flat = float(flat.mean()) / 255.0
    a_turn = float(turned.mean()) / 255.0
    ctx.note(f"covered area, unrotated {a_flat:.5f}, at 45 degrees {a_turn:.5f}")
    ctx.expect_close("rotation preserves the covered area", a_turn, a_flat, 0.002)

    # And it has to reach the render, not only the matte.
    base = _sec_base()
    r0 = H.render(CLIP, H.patch(base, rect), T)
    r45 = H.render(CLIP, H.patch(base, rect, {"window": {"rotation": 45.0}}), T)
    moved = H.changed_fraction(r0, r45, thresh=0)
    ctx.note(f"rotating the rendered window moved {moved * 100:.3f}% of pixels")
    ctx.expect_gt("rotation changes the rendered frame", moved, 0.02)


# --------------------------------------------------------------------------
# (e) softness
# --------------------------------------------------------------------------

def test_softness_zero_is_binary_and_half_is_a_ramp(ctx):
    size_w, size_h = 960, 540
    hard = cg.window_matte(HARD_ELLIPSE, size_w, size_h)
    narrow = cg.window_matte(H.patch(HARD_ELLIPSE, {"window": {"softness": 0.15}}),
                             size_w, size_h)
    soft = cg.window_matte(H.patch(HARD_ELLIPSE, {"window": {"softness": 0.5}}),
                           size_w, size_h)
    hard_vals = np.unique(hard).tolist()
    soft_levels = int(len(np.unique(soft)))
    total = float(soft.size)

    def partial(m):
        return int(((m > 0) & (m < 255)).sum())

    mid_hard, mid_narrow, mid_soft = partial(hard), partial(narrow), partial(soft)
    ctx.note(f"softness 0: matte codes present {hard_vals}, "
             f"{mid_hard} pixels between 0 and 255")
    ctx.note(f"softness 0.15: {mid_narrow} partial pixels "
             f"({mid_narrow / total * 100:.2f}% of the frame)")
    ctx.note(f"softness 0.5: {soft_levels} distinct codes, {mid_soft} partial "
             f"pixels ({mid_soft / total * 100:.2f}% of {int(total)})")
    ctx.expect_true("softness 0 produces only 0 and 255",
                    hard_vals == [0, 255], str(hard_vals))
    ctx.expect_eq("softness 0 has no partial pixels at all", mid_hard, 0)
    ctx.expect_gt("softness 0.5 spreads the edge over many codes",
                  float(soft_levels), 200.0)
    # Measured on this shape: 11.73% of the frame is partial at softness 0.15
    # and 39.12% at 0.5. The assertion is the ordering plus a floor well under
    # the measurement, so a real change in the ramp fails and noise does not.
    ctx.expect_gt("a wider feather covers more of the frame in partial values",
                  mid_soft / total, mid_narrow / total, 0.1)
    ctx.expect_gt("softness 0.5 leaves a large share of the frame partial",
                  mid_soft / total, 0.25)


# --------------------------------------------------------------------------
# (f) geq against numpy
# --------------------------------------------------------------------------

def test_geq_matte_matches_the_numpy_matte(ctx):
    """The ffmpeg expression and the reference must agree bit for bit.

    They are two implementations of one formula, and the GPU port in the next
    wave will be a third. If the two that exist today drift, there is no
    correct answer for the third to be checked against.
    """
    shapes = [
        ("ellipse centred, feathered",
         {"shape": "ellipse", "cx": 0.5, "cy": 0.5, "w": 0.6, "h": 0.6,
          "rotation": 0.0, "softness": 0.15, "invert": False}),
        ("ellipse off centre, rotated, wide feather",
         {"shape": "ellipse", "cx": 0.35, "cy": 0.62, "w": 0.4, "h": 0.85,
          "rotation": 23.0, "softness": 0.35, "invert": False}),
        ("rect at 45 degrees",
         {"shape": "rect", "cx": 0.5, "cy": 0.5, "w": 0.5, "h": 0.3,
          "rotation": 45.0, "softness": 0.2, "invert": False}),
        ("rect, no feather",
         {"shape": "rect", "cx": 0.5, "cy": 0.5, "w": 0.5, "h": 0.3,
          "rotation": 45.0, "softness": 0.0, "invert": False}),
        ("inverted ellipse",
         {"shape": "ellipse", "cx": 0.5, "cy": 0.5, "w": 0.6, "h": 0.6,
          "rotation": 0.0, "softness": 0.5, "invert": True}),
    ]
    worst = 0
    for w, h in ((1920, 1080), (320, 568)):
        for label, win in shapes:
            cfg = {"window": dict(win, enabled=True)}
            ref = cg.window_matte(cfg, w, h)
            png = cg.window_mask(cfg, w, h)
            got = _read_gray_png(png, w, h)
            d = int(np.abs(ref.astype(np.int16) - got.astype(np.int16)).max())
            worst = max(worst, d)
            ctx.check(d == 0, f"{w}x{h} {label}: max |geq - numpy| = {d}")
    ctx.note(f"worst disagreement over 10 matte/size combinations: {worst} of 255")


# --------------------------------------------------------------------------
# (g) the fractions really are fractions
# --------------------------------------------------------------------------

def test_matte_scales_with_the_frame(ctx):
    """A 960 wide preview and the full width render must describe one shape.

    This is the whole reason every window parameter is a fraction rather than a
    pixel count: scale_for_preview has to fix halation sigma and grain size at
    a smaller preview, and it must not need a case for the window.
    """
    win = {"window": {"enabled": True, "shape": "ellipse", "cx": 0.42,
                      "cy": 0.55, "w": 0.5, "h": 0.7, "rotation": 17.0,
                      "softness": 0.25, "invert": False}}
    big = cg.window_matte(win, 1920, 1080).astype(np.float64)
    small = cg.window_matte(win, 960, 540).astype(np.float64)

    area_big = big.mean() / 255.0
    area_small = small.mean() / 255.0
    ctx.note(f"covered area 1920x1080 {area_big:.6f}, 960x540 {area_small:.6f}")
    ctx.expect_close("the shape covers the same share of the frame at both sizes",
                     area_small, area_big, 0.002)

    # A plain 2x box average of the large matte against the natively generated
    # small one. Any disagreement can only be at the feather, where a half
    # pixel of the larger grid falls on a different step of the ramp.
    boxed = big.reshape(540, 2, 960, 2).mean(axis=(1, 3))
    d = np.abs(boxed - small)
    ctx.note(f"2x box downsample of the large matte vs the small one: "
             f"max {d.max():.3f}, mean {d.mean():.4f} of 255")
    ctx.expect_lt("the two sizes agree pixel for pixel after resizing",
                  float(d.max()), 4.0)
    ctx.expect_lt("and agree closely on average", float(d.mean()), 0.5)


# --------------------------------------------------------------------------
# the matte view
# --------------------------------------------------------------------------

def test_mask_view_is_qualifier_times_window(ctx):
    """What the studio's Matte button shows has to be the real selection.

    The qualifier matte alone would claim pixels the grade will never touch,
    because the window closes over them. The engine composites the greyscale
    qualifier over black through the window matte, so the view is the product
    of the two, which is exactly what the secondary now selects.
    """
    base = H.patch(_sec_base(), {"secondary": {"show_mask": True}})
    qualifier = H.render(CLIP, base, T)
    windowed = H.render(CLIP, H.patch(base, HARD_ELLIPSE), T)
    h, w, _ = windowed.shape
    inside, outside = _regions(H.patch(base, HARD_ELLIPSE), w, h)

    out_level = float(windowed[outside].max()) if outside.any() else 0.0
    in_diff = _mean_abs(windowed, qualifier, inside)
    q_out = float(qualifier[outside].mean())
    ctx.note(f"qualifier matte outside the window averages {q_out:.3f} of 255, "
             f"so it does claim pixels the window closes over")
    ctx.note(f"matte view: brightest pixel outside the window {out_level:.1f}, "
             f"mean difference from the plain qualifier inside {in_diff:.4f}")
    ctx.expect_close("outside the window the matte view is pure black",
                     out_level, 0.0, 0.0)
    ctx.expect_close("inside the window it is the qualifier matte untouched",
                     in_diff, 0.0, 0.0)


def register(suite):
    g = "window"
    suite.add(g, "disabled_is_byte_identical", test_disabled_window_is_byte_identical,
              doc="with the window off the graph, the inputs and the pixels are unchanged")
    suite.add(g, "ellipse_gates_the_secondary", test_ellipse_gates_the_secondary,
              doc="the correction lands inside the shape and nowhere else")
    suite.add(g, "invert_swaps_inside_and_outside", test_invert_swaps_inside_and_outside,
              doc="invert grades the complement, and the two halves tile the frame")
    suite.add(g, "rect_rotated_45_moves_the_corners", test_rect_rotated_45_moves_the_corners,
              doc="a square turned 45 degrees drops its corners and keeps its area")
    suite.add(g, "softness_zero_is_binary", test_softness_zero_is_binary_and_half_is_a_ramp,
              doc="softness 0 gives only 0 and 255, softness 0.5 gives a real ramp")
    suite.add(g, "geq_matte_matches_numpy", test_geq_matte_matches_the_numpy_matte,
              doc="the ffmpeg expression and the numpy reference agree on every pixel")
    suite.add(g, "matte_scales_with_the_frame", test_matte_scales_with_the_frame,
              doc="the same fractions describe the same shape at 960 and at full width")
    suite.add(g, "mask_view_is_qualifier_times_window", test_mask_view_is_qualifier_times_window,
              doc="the matte view shows the qualifier multiplied by the window")
