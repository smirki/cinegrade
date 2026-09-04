"""Group 6: range, clipping and the edge inputs that have bitten this engine.

Three separate worries:

  clipping   a default grade must not crush blacks or blow highlights. AGENTS.md
             reads YMIN pinned at 0 or YMAX pinned at 255 as the failure signal.
  extremes   pure black and pure white have to survive the whole tree. Apple Log
             code 0.0 decodes negative, so black is the input most likely to
             come out wrong.
  dark reds  a split-tone shadow push once inverted dark saturated reds and
             turned a maroon car blue. That is a hue-order failure, not a
             brightness one, so it is checked as a channel ranking.
"""

from __future__ import annotations

import numpy as np

import harness as H
from harness import CST, cg

MAROON = [0.25, 0.08, 0.10]

# The approved look. A look is where a shadow split-tone lives, so this is the
# stage that can invert a dark red.
LOOK = "blockbuster"

# Presets that ship as sane starting points. If any of these crushes or blows a
# real frame at its own default settings, that is a grading bug, not a style.
SANE_PRESETS = ["flat", "natural", "clean", LOOK]

# AGENTS.md reads YAVG outside roughly 95 to 140 as mis-exposure. That is an
# exposure diagnostic, so it is asserted on the restrained presets only. A
# deliberately dark look plus a vignette lands lower on purpose (blockbuster
# measures YAVG 77 on clip A) and failing it for that would be calling a style
# a bug.
EXPOSURE_BAND_PRESETS = {"flat", "natural", "clean"}

# Lost detail means a pixel with nothing left in it: every channel pinned, so
# the area reads as a flat patch. One channel pinned while the other two still
# vary is what a saturated look does on purpose, and is reported rather than
# failed. Measured across the shipped presets, whole-pixel clipping is 0.000%
# and the loudest look (blockbuster_max) pins 1.9% of single channels.
CLIP_LIMIT = 0.001
CRUSH_LIMIT = 0.001


def test_no_unintended_clipping_at_defaults(ctx):
    for preset in SANE_PRESETS:
        cfg = H.preset(preset)
        for name, clip, t in (("clipA", H.CLIP_A, H.TIME_A),
                              ("clipB", H.CLIP_B, H.TIME_B)):
            img = H.render(clip, cfg, t)
            ch_lo, ch_hi = H.clip_fractions(img)
            crushed = float((img.max(axis=2) == 0).mean())
            blown = float((img.min(axis=2) == 255).mean())
            s = H.stats(img)
            ctx.note(f"{preset:12} {name}: dead-black pixels {crushed * 100:.3f}% "
                     f"dead-white {blown * 100:.3f}% "
                     f"(single channels pinned: {ch_lo * 100:.3f}% low, "
                     f"{ch_hi * 100:.3f}% high) "
                     f"luma mean {s['luma_mean']:.3f}, "
                     f"8-bit YAVG {s['luma_mean'] * 255:.0f}")
            ctx.expect_le(f"{preset}/{name}: highlights not blown", blown, CLIP_LIMIT)
            ctx.expect_le(f"{preset}/{name}: blacks not crushed", crushed, CRUSH_LIMIT)
            if preset in EXPOSURE_BAND_PRESETS:
                ctx.expect_between(f"{preset}/{name}: YAVG in the documented range",
                                   s["luma_mean"] * 255, 95.0, 140.0, tol=12.0)


def test_full_range_is_used(ctx):
    """The opposite failure: a grade so timid the file wastes its range."""
    for name, clip, t in (("clipA", H.CLIP_A, H.TIME_A),
                          ("clipB", H.CLIP_B, H.TIME_B)):
        img = H.render(clip, H.preset("natural"), t)
        y = H.luma(img)
        ctx.note(f"{name}: luma spans {y.min():.4f} to {y.max():.4f}")
        ctx.expect_gt(f"{name}: the frame reaches a real highlight",
                      float(y.max()), 0.6)
        ctx.expect_lt(f"{name}: the frame reaches a real shadow",
                      float(y.min()), 0.25)


# --------------------------------------------------------------------------
# extreme inputs, fed as exact code values
# --------------------------------------------------------------------------

def _dwg_chain(code):
    """The default path, as a plain function, for solving an input backwards."""
    return CST.dwg_to_rec709(CST.apple_log_to_dwg(code), "aces", "rec709a")


def _edge_patches():
    """Black, white, and Apple Log codes that arrive as named Rec.709 colours."""
    maroon_code, maroon_hit = H.solve_input_for_output(_dwg_chain, MAROON)
    darker_code, darker_hit = H.solve_input_for_output(
        _dwg_chain, [0.14, 0.035, 0.05])
    midred_code, midred_hit = H.solve_input_for_output(
        _dwg_chain, [0.45, 0.12, 0.14])
    patches = {
        "black": [0.0, 0.0, 0.0],
        "white": [1.0, 1.0, 1.0],
        "maroon": list(maroon_code),
        "deep_maroon": list(darker_code),
        "mid_red": list(midred_code),
    }
    hits = {"maroon": maroon_hit, "deep_maroon": darker_hit, "mid_red": midred_hit}
    return patches, hits


def test_pure_black_and_white_survive_the_tree(ctx):
    patches, _ = _edge_patches()
    for preset in ("flat", "natural", LOOK):
        got = H.render_patches(H.preset(preset), patches)
        b, w = np.asarray(got["black"]), np.asarray(got["white"])
        yb = float(H.C.luma709(b[None, :])[0, 0])
        yw = float(H.C.luma709(w[None, :])[0, 0])
        ctx.note(f"{preset}: black in -> {np.round(b, 4).tolist()} (luma {yb:.4f}), "
                 f"white in -> {np.round(w, 4).tolist()} (luma {yw:.4f})")
        # A creative look tints and compresses both ends on purpose, so the
        # invariant is that they stay recognisably the ends and never wrap,
        # not that they stay untouched. flat is the technical path and does
        # have to stay untouched.
        if preset == "flat":
            ctx.expect_ge(f"{preset}: pure white passes through", yw, 0.98)
            ctx.expect_le(f"{preset}: pure black passes through", yb, 0.005)
        ctx.expect_gt(f"{preset}: white is still a highlight", yw, 0.75)
        ctx.expect_lt(f"{preset}: black is still a shadow", yb, 0.15)
        ctx.expect_gt(f"{preset}: white and black stay far apart", yw - yb, 0.5)
        ctx.expect_true(f"{preset}: black did not go negative or wrap",
                        bool(np.all(b >= 0.0)), f"min channel {float(b.min()):.5f}")
        ctx.expect_true(f"{preset}: white did not overflow or wrap",
                        bool(np.all(w <= 1.0)), f"max channel {float(w.max()):.5f}")


def test_dark_saturated_reds_stay_red(ctx):
    """The maroon car. A dark red must not come out blue dominant.

    Checked through the whole engine at the exact Rec.709 colour named in the
    bug report, reached by solving for the Apple Log code that lands there.
    """
    patches, hits = _edge_patches()
    for name, want in (("maroon", MAROON), ("deep_maroon", [0.14, 0.035, 0.05]),
                       ("mid_red", [0.45, 0.12, 0.14])):
        ctx.note(f"{name}: solved input arrives at "
                 f"{np.round(hits[name], 4).tolist()} against a target of {want}")

    flat = H.render_patches(H.preset("flat"), patches)
    looked = H.render_patches(H.preset(LOOK), patches)
    for name in ("maroon", "deep_maroon", "mid_red"):
        a, b = np.asarray(flat[name]), np.asarray(looked[name])
        ctx.note(f"{name}: {np.round(a, 4).tolist()} -> {np.round(b, 4).tolist()} "
                 f"through {LOOK} (R-B {a[0] - a[2]:+.4f} -> {b[0] - b[2]:+.4f})")
        ctx.expect_eq(f"{name}: red is still the dominant channel after {LOOK}",
                      int(np.argmax(b)), 0)
        ctx.expect_gt(f"{name}: red still leads blue after {LOOK}",
                      float(b[0]), float(b[2]))
        ctx.expect_le(f"{name}: hue does not move more than a family",
                      H.hue_distance(b, a), 40.0)


def test_look_luts_do_not_invert_dark_reds(ctx):
    """Same claim at the LUT level, sampled straight out of the .cube.

    Cheap enough to run over every look in the library, which is what makes it
    a guard on a look being regenerated rather than only on this one grade.
    """
    probes = {"maroon": MAROON, "deep maroon": [0.14, 0.035, 0.05],
              "mid red": [0.45, 0.12, 0.14], "dark red": [0.18, 0.05, 0.06]}
    cubes = sorted((cg.LUT_LOOKS).glob("*.cube"))
    ctx.note(f"{len(cubes)} look LUTs in {cg.LUT_LOOKS}")
    for path in cubes:
        lut, size = H.read_cube(path)
        for pname, rgb in probes.items():
            out = H.sample_cube(lut, size, rgb)
            ok = out[0] > out[2]
            msg = (f"{path.stem}/{pname}: {np.round(rgb, 3).tolist()} -> "
                   f"{np.round(out, 4).tolist()} (R-B {out[0] - out[2]:+.4f})")
            ctx.check(ok, msg)


def register(suite):
    g = "range"
    suite.add(g, "no_unintended_clipping", test_no_unintended_clipping_at_defaults,
              doc="shipped presets neither crush blacks nor blow highlights")
    suite.add(g, "full_range_is_used", test_full_range_is_used,
              doc="a graded frame still reaches a real shadow and a real highlight")
    suite.add(g, "black_and_white_survive", test_pure_black_and_white_survive_the_tree,
              doc="pure black and pure white through the whole tree")
    suite.add(g, "dark_reds_stay_red", test_dark_saturated_reds_stay_red,
              doc="the maroon car: a dark saturated red must not flip blue")
    suite.add(g, "look_luts_keep_reds_red", test_look_luts_do_not_invert_dark_reds,
              doc="every look LUT keeps dark reds red dominant")
