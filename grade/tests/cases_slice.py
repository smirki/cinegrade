"""Group 10: the hue curves, Color Slice and Tetra.

All three tools are pure functions of one pixel's RGB baked into one 33-cube,
so the tests split cleanly in two.

  reference   what each tool does to a colour. Asserted against grade/slice.py
              directly on synthetic colours whose hue, saturation and value are
              known exactly, because real footage cannot supply a clean hue
              sweep and an 8-bit render cannot resolve a two degree rotation.
  agreement   the cube ffmpeg actually reads reproduces that reference. A tool
              that is right in numpy and wrong in the render is worth nothing,
              and the only way to tell them apart is to push known values
              through the real lut3d and compare.

Plus the compatibility rule for this arc: with every new field at its default
the stage is absent from the graph and the rendered bytes are the ones that
shipped before it existed.
"""

from __future__ import annotations

import hashlib
import sys
from copy import deepcopy

import numpy as np

import harness as H
from harness import cg, C

sys.path.insert(0, str(H.GRADE))
import slice as SL          # noqa: E402

CLIP = H.CLIP_A
T = H.TIME_A


# --------------------------------------------------------------------------
# synthetic colour helpers
# --------------------------------------------------------------------------

def hue_sweep(step: float = 1.0, sat: float = 1.0, val: float = 1.0):
    """A ring of fully described colours, one per hue step."""
    h = np.arange(0.0, 360.0, step)
    rgb = C.hsv_to_rgb(h, np.full(h.shape, sat), np.full(h.shape, val))
    return h, rgb


def hsv_of(rgb):
    h, s, v = C.rgb_to_hsv(np.asarray(rgb, dtype=np.float64))
    return h, s, v


def hue_delta(before, after):
    """Signed hue change in degrees, wrapped into [-180, 180]."""
    return ((after - before + 180.0) % 360.0) - 180.0


def slice_cfg(**slice_over):
    """A config carrying only the slice block, enabled."""
    cfg = deepcopy(cg.DEFAULTS["slice"])
    cfg["enabled"] = True
    for k, v in slice_over.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k] = cg.deep_merge(cfg[k], v)
        else:
            cfg[k] = v
    return {"hue_curves": deepcopy(cg.DEFAULTS["hue_curves"]), "slice": cfg}


def curve_cfg(key, points):
    """A config carrying only one hue curve, enabled."""
    hc = deepcopy(cg.DEFAULTS["hue_curves"])
    hc["enabled"] = True
    hc[key] = points
    return {"hue_curves": hc, "slice": deepcopy(cg.DEFAULTS["slice"])}


# --------------------------------------------------------------------------
# (a) the stage is invisible when it is off
# --------------------------------------------------------------------------

def test_disabled_is_byte_identical(ctx):
    """A config carrying hue_curves and slice at their defaults must render
    exactly what a config that has never heard of them renders.

    Graph text, ffmpeg input list and pixels, the same three checks the window
    group makes, because any one of them alone could pass while the render
    still moved.
    """
    info = H.info_for(CLIP)
    combos = []
    for sharpen in (0.0, 0.7):
        for grain in (False, True):
            for curves in (False, True):
                combos.append((f"sharpen={sharpen} grain={grain} curves={curves}",
                               H.patch(H.defaults(), {
                                   "detail": {"sharpen": sharpen},
                                   "grain": {"enabled": grain},
                                   "curves": {"enabled": curves,
                                              "master": [[0.0, 0.0], [0.3, 0.45],
                                                         [1.0, 1.0]]}})))
    graph_diffs, input_diffs = [], []
    for tag, cfg in combos:
        without = deepcopy(cfg)
        without.pop("hue_curves")
        without.pop("slice")
        if cg.graph_with_mask(cfg, info) != cg.graph_with_mask(without, info):
            graph_diffs.append(tag)
        if (cg.ffmpeg_inputs(str(CLIP), cfg, info)
                != cg.ffmpeg_inputs(str(CLIP), without, info)):
            input_diffs.append(tag)
    ctx.note(f"{len(combos)} configs compared with and without the two blocks")
    ctx.expect_true("the graph text is unchanged by the new blocks",
                    not graph_diffs, f"differed: {graph_diffs}")
    ctx.expect_true("the ffmpeg input list is unchanged by the new blocks",
                    not input_diffs, f"differed: {input_diffs}")

    # Enabled but untouched is also identity: a user who opens the section and
    # ticks the box without moving a control must not lose their render.
    live = H.patch(H.defaults(), {"hue_curves": {"enabled": True},
                                  "slice": {"enabled": True,
                                            "tetra": {"enabled": True}}})
    neutral = SL.is_identity(live)
    ctx.expect_true("enabled with every control at neutral is still identity",
                    neutral, "yes" if neutral else "is_identity returned False")
    ctx.expect_eq("an enabled but neutral stage adds no filter",
                  cg.f_slice(live), [])

    with_block = H.patch(H.defaults(), {"curves": {
        "enabled": True, "master": [[0.0, 0.0], [0.3, 0.45], [1.0, 1.0]]}})
    without_block = deepcopy(with_block)
    without_block.pop("hue_curves")
    without_block.pop("slice")
    a = H.render(CLIP, with_block, T)
    b = H.render(CLIP, without_block, T)
    ha = hashlib.sha1(a.tobytes()).hexdigest()[:16]
    hb = hashlib.sha1(b.tobytes()).hexdigest()[:16]
    ctx.note(f"rendered frame hash with the blocks {ha}, without them {hb}")
    ctx.expect_eq("a defaulted stage renders the identical frame", ha, hb)


def test_bake_of_a_neutral_config_is_the_identity_grid(ctx):
    """The bake itself, independent of whether the graph skips it."""
    grid = C.identity_grid(SL.LUT_SIZE)
    for tag, cfg in (
            ("everything off", {"hue_curves": {"enabled": False},
                                "slice": {"enabled": False}}),
            ("enabled, all neutral", {
                "hue_curves": deepcopy(cg.DEFAULTS["hue_curves"]),
                "slice": deepcopy(cg.DEFAULTS["slice"])}),
            ("flat curves", curve_cfg("hue_sat", [[0.0, 1.0], [0.5, 1.0]])),
            ("zero tetra", slice_cfg(tetra={"enabled": True}))):
        err = float(np.abs(SL.bake(cfg) - grid).max())
        ctx.expect_close(f"bake is the identity grid ({tag})", err, 0.0, 0.0)


# --------------------------------------------------------------------------
# (b) the slice vectors
# --------------------------------------------------------------------------

def test_vector_hue_rotation_is_local(ctx):
    """Each vector rotates its own hue family and nothing outside its support.

    "Nothing outside" is asserted as exactly zero, not nearly zero: the raised
    cosine reaches zero at 60 degrees by construction, so any leakage past that
    is a bug in the weight and not a tolerance question.
    """
    h0, rgb = hue_sweep(1.0)
    for name, centre in SL.VECTOR_CENTRES:
        cfg = slice_cfg(vectors={name: {"hue": 20.0}})
        out = SL.apply_all(rgb, cfg)
        d = hue_delta(h0, hsv_of(out)[0])
        dist = np.abs(((h0 - centre + 180.0) % 360.0) - 180.0)
        inside = dist < 1.0
        outside = dist >= SL.VECTOR_HALF_WIDTH
        peak = float(np.abs(d[inside]).max())
        leak = float(np.abs(d[outside]).max())
        ctx.note(f"{name} at {centre:.4f} deg: peak rotation {peak:.4f} deg, "
                 f"{int(outside.sum())} hues outside the support")
        ctx.expect_close(f"{name} rotates its own centre by the full amount",
                         peak, 20.0, 0.05)
        # 1e-9 rather than 0: the hue itself is recovered through a modulo,
        # so an untouched hue comes back a few float ulps from where it left.
        ctx.expect_close(f"{name} leaves every hue past 60 degrees alone",
                         leak, 0.0, 1e-9)


def test_vector_hue_rotation_ignores_greys(ctx):
    """The saturation scale is what keeps the vectors off neutrals."""
    grey = np.array([[v, v, v] for v in np.linspace(0.0, 1.0, 33)])
    cfg = slice_cfg(vectors={"red": {"hue": 60.0, "sat": 2.0}})
    out = SL.apply_all(grey, cfg)
    err = float(np.abs(out - grey).max())
    ctx.note(f"33 greys through a 60 degree red rotation, max channel move "
             f"{err * 255:.6f} of 255")
    ctx.expect_close("a neutral pixel is untouched by any vector", err, 0.0, 1e-12)


def test_density_darkens_saturated_and_spares_grey(ctx):
    """L' = L * (1 - density * S * weight): grey has S = 0, so grey is fixed."""
    grey = np.array([[v, v, v] for v in np.linspace(0.05, 1.0, 20)])
    _, sat_rgb = hue_sweep(5.0, sat=1.0, val=0.8)

    for tag, cfg in (
            ("red vector density", slice_cfg(vectors={"red": {"density": 0.6}})),
            ("global density", slice_cfg(density=0.6))):
        g_out = SL.apply_all(grey, cfg)
        ctx.expect_close(f"{tag} leaves grey exactly where it was",
                         float(np.abs(g_out - grey).max()), 0.0, 1e-12)

    # Positive density darkens, negative brightens, and only V moves.
    cfg = slice_cfg(density=0.6)
    out = SL.apply_all(sat_rgb, cfg)
    h_in, s_in, v_in = hsv_of(sat_rgb)
    h_out, s_out, v_out = hsv_of(out)
    ctx.note(f"global density 0.6 on {len(sat_rgb)} fully saturated colours: "
             f"mean V {v_in.mean():.4f} -> {v_out.mean():.4f}")
    ctx.expect_lt("density darkens fully saturated colour", float(v_out.mean()),
                  float(v_in.mean()))
    ctx.expect_close("density leaves hue alone",
                     float(np.abs(hue_delta(h_in, h_out)).max()), 0.0, 1e-9)
    ctx.expect_close("density leaves saturation alone",
                     float(np.abs(s_out - s_in).max()), 0.0, 1e-9)
    ctx.expect_close("V falls by exactly (1 - density * S)",
                     float(np.abs(v_out - v_in * (1.0 - 0.6 * s_in)).max()),
                     0.0, 1e-9)

    bright = SL.apply_all(sat_rgb, slice_cfg(density=-0.4))
    ctx.expect_gt("negative density brightens", float(hsv_of(bright)[2].mean()),
                  float(v_in.mean()))


def test_vector_saturation_gain_is_local(ctx):
    """sat multiplies saturation, weighted, and only inside the support."""
    h0, rgb = hue_sweep(1.0, sat=0.6, val=0.8)
    cfg = slice_cfg(vectors={"green": {"sat": 1.8}})
    out = SL.apply_all(rgb, cfg)
    s_in = hsv_of(rgb)[1]
    s_out = hsv_of(out)[1]
    dist = np.abs(((h0 - 120.0 + 180.0) % 360.0) - 180.0)
    at_centre = float(s_out[dist < 1.0].max() / s_in[dist < 1.0].max())
    leak = float(np.abs(s_out[dist >= SL.VECTOR_HALF_WIDTH]
                        - s_in[dist >= SL.VECTOR_HALF_WIDTH]).max())
    # weight at the centre is the raised cosine (1) times the pixel's own
    # saturation (0.6), so the gain lands at 1 + 0.8 * 0.6 = 1.48, not 1.8.
    ctx.note(f"green sat 1.8 on S=0.6 input: measured gain {at_centre:.4f}")
    ctx.expect_close("the saturation gain is weight scaled, not flat",
                     at_centre, 1.48, 0.005)
    ctx.expect_close("no saturation change past the support", leak, 0.0, 0.0)


# --------------------------------------------------------------------------
# (c) tetra
# --------------------------------------------------------------------------

def test_tetra_moves_a_corner_and_pins_black_and_white(ctx):
    corners = {
        "black": [0.0, 0.0, 0.0], "white": [1.0, 1.0, 1.0],
        "r": [1.0, 0.0, 0.0], "g": [0.0, 1.0, 0.0], "b": [0.0, 0.0, 1.0],
        "c": [0.0, 1.0, 1.0], "m": [1.0, 0.0, 1.0], "y": [1.0, 1.0, 0.0],
    }
    names = list(corners)
    pts = np.array([corners[n] for n in names])
    delta = [0.0, 0.15, -0.10]
    cfg = slice_cfg(tetra={"enabled": True, "r": delta})
    out = SL.apply_all(pts, cfg)

    got = dict(zip(names, out))
    want_r = np.clip(np.array(corners["r"]) + np.array(delta), 0.0, 1.0)
    ctx.note(f"red corner {corners['r']} -> {got['r'].round(6).tolist()}")
    ctx.expect_close("the moved corner lands on its delta",
                     float(np.abs(got["r"] - want_r).max()), 0.0, 1e-12)
    for n in ("black", "white", "g", "b", "c", "m", "y"):
        ctx.expect_close(f"the {n} corner does not move",
                         float(np.abs(got[n] - np.array(corners[n])).max()),
                         0.0, 1e-12)

    # Every tetrahedron in the decomposition has black and white as vertices,
    # so the whole neutral axis is pinned, not just its two ends.
    grey = np.array([[v, v, v] for v in np.linspace(0.0, 1.0, 33)])
    moved = SL.apply_all(grey, slice_cfg(tetra={
        "enabled": True, "r": [0.2, -0.2, 0.0], "g": [0.0, 0.2, -0.2],
        "b": [-0.2, 0.0, 0.2], "c": [0.1, 0.1, 0.1], "m": [-0.1, -0.1, -0.1],
        "y": [0.05, -0.05, 0.05]}))
    ctx.note(f"33 greys through six moved corners, max move "
             f"{float(np.abs(moved - grey).max()) * 255:.8f} of 255")
    ctx.expect_close("the neutral axis is pinned for any corner move",
                     float(np.abs(moved - grey).max()), 0.0, 1e-12)


def test_tetra_interpolates_between_the_corners(ctx):
    """A moved corner has to drag its neighbourhood, not stand alone."""
    cfg = slice_cfg(tetra={"enabled": True, "r": [0.0, 0.25, 0.0]})
    near = np.array([[0.9, 0.1, 0.05]])
    out = SL.apply_all(near, cfg)
    lift = float(out[0][1] - near[0][1])
    ctx.note(f"a colour near the red corner gains {lift:.6f} green "
             f"(corner delta 0.25)")
    ctx.expect_gt("the move reaches colours near the corner", lift, 0.05)
    ctx.expect_lt("the move fades with distance from the corner", lift, 0.25)


# --------------------------------------------------------------------------
# (d) the hue curves
# --------------------------------------------------------------------------

def test_each_hue_curve_moves_only_its_own_axis(ctx):
    """Hue vs Hue moves hue, Hue vs Sat moves saturation, and so on.

    Measured in HSV on the output, so "moves only its axis" means the other two
    HSV coordinates come back bit for bit, not merely close.
    """
    h0, rgb = hue_sweep(2.0, sat=0.7, val=0.7)
    h_in, s_in, v_in = hsv_of(rgb)

    checks = [
        ("hue_hue", [[0.0, 0.05], [0.5, 0.0]], "hue"),
        ("hue_sat", [[0.0, 1.4], [0.5, 1.0]], "sat"),
        ("hue_lum", [[0.0, 1.3], [0.5, 1.0]], "val"),
        ("sat_sat", [[0.0, 1.0], [1.0, 0.5]], "sat"),
    ]
    for key, pts, axis in checks:
        out = SL.apply_all(rgb, curve_cfg(key, pts))
        h_out, s_out, v_out = hsv_of(out)
        moves = {
            "hue": float(np.abs(hue_delta(h_in, h_out)).max()),
            "sat": float(np.abs(s_out - s_in).max()),
            "val": float(np.abs(v_out - v_in).max()),
        }
        ctx.note(f"{key}: hue moved {moves['hue']:.5f} deg, sat "
                 f"{moves['sat']:.6f}, val {moves['val']:.6f}")
        ctx.expect_gt(f"{key} moves {axis}", moves[axis], 0.0)
        for other in ("hue", "sat", "val"):
            if other == axis:
                continue
            ctx.expect_close(f"{key} leaves {other} alone", moves[other], 0.0, 1e-9)

    # lum_sat needs a luma ramp rather than a hue ring: its x axis is
    # brightness, so a constant-value sweep would sample one point of it.
    ramp = C.hsv_to_rgb(np.full(21, 30.0), np.full(21, 0.7),
                        np.linspace(0.05, 1.0, 21))
    out = SL.apply_all(ramp, curve_cfg("lum_sat", [[0.0, 1.6], [1.0, 0.6]]))
    h_r, s_r, v_r = hsv_of(ramp)
    h_o, s_o, v_o = hsv_of(out)
    ratio = s_o / np.maximum(s_r, 1e-9)
    ctx.note(f"lum_sat: saturation ratio runs {ratio[0]:.4f} at the dark end "
             f"to {ratio[-1]:.4f} at the bright end")
    ctx.expect_gt("lum_sat lifts saturation in the shadows", float(ratio[0]), 1.0)
    ctx.expect_lt("lum_sat drops saturation in the highlights", float(ratio[-1]), 1.0)
    ctx.expect_close("lum_sat leaves hue alone",
                     float(np.abs(hue_delta(h_r, h_o)).max()), 0.0, 1e-9)
    ctx.expect_close("lum_sat leaves value alone",
                     float(np.abs(v_o - v_r).max()), 0.0, 1e-9)


def test_hue_axes_wrap(ctx):
    """A hue curve is a loop: 0 and 360 degrees are the same place.

    A non-periodic evaluator would hold the end value flat past the last point
    and put a visible seam through pure red, which is the one hue a colourist
    is most likely to be steering.
    """
    xs = np.array([0.0, 1.0 - 1e-12])
    pts = [[0.1, 0.06], [0.6, -0.06]]
    y = SL.pchip_eval_periodic(pts, xs, 0.0)
    ctx.note(f"periodic pchip at x=0 and x=1: {y[0]:.9f} and {y[1]:.9f}")
    ctx.expect_close("the curve closes on itself", float(abs(y[0] - y[1])),
                     0.0, 1e-9)

    # The wrap has to be a real curve across the seam, not a flat hold.
    seam = SL.pchip_eval_periodic(pts, np.linspace(0.6, 1.1, 51), 0.0)
    ctx.expect_gt("the curve keeps moving across the seam",
                  float(seam.max() - seam.min()), 0.01)

    _, rgb = hue_sweep(1.0)
    out = SL.apply_all(rgb, curve_cfg("hue_hue", pts))
    d = hue_delta(hsv_of(rgb)[0], hsv_of(out)[0])
    # 0 and 359 degrees are one degree apart on the wheel, so their rotations
    # must be too. A seam would show as a jump of many degrees here.
    ctx.note(f"rotation at hue 0 {d[0]:+.4f} deg, at hue 359 {d[-1]:+.4f} deg")
    ctx.expect_lt("no seam in the rotation at the top of the wheel",
                  float(abs(d[0] - d[-1])), 0.5)


def test_an_empty_curve_is_the_identity(ctx):
    """The neutral is a flat line at 0 or 1, not the diagonal."""
    for key in SL.CURVE_KEYS:
        neutral = SL.CURVE_AXES[key]["neutral"]
        y = SL.curve_eval(key, [], np.linspace(0.0, 1.0, 11))
        ctx.expect_close(f"{key} with no points evaluates to its neutral",
                         float(np.abs(y - neutral).max()), 0.0, 0.0)
        flat = SL.curve_is_identity(key, [])
        ctx.expect_true(f"{key} with no points reports identity", flat,
                        "yes" if flat else "curve_is_identity returned False")


# --------------------------------------------------------------------------
# (e) the cube ffmpeg reads reproduces the reference
# --------------------------------------------------------------------------

# Everything on at once, so the agreement test covers the composition and not
# just one tool at a time.
EVERYTHING = {
    "hue_curves": {"enabled": True,
                   "hue_hue": [[0.0, 0.03], [0.35, -0.02], [0.7, 0.01]],
                   "hue_sat": [[0.05, 1.3], [0.5, 0.8]],
                   "hue_lum": [[0.1, 1.15], [0.6, 0.9]],
                   "lum_sat": [[0.0, 1.25], [1.0, 0.75]],
                   "sat_sat": [[0.0, 1.1], [0.5, 1.0], [1.0, 0.85]]},
    "slice": {"enabled": True, "density": 0.12,
              "vectors": {"red": {"hue": 8.0, "sat": 1.2, "density": 0.2},
                          "yellow": {"hue": 0.0, "sat": 1.0, "density": 0.0},
                          "green": {"hue": -6.0, "sat": 0.7, "density": 0.0},
                          "cyan": {"hue": 0.0, "sat": 1.0, "density": 0.0},
                          "blue": {"hue": 4.0, "sat": 1.15, "density": -0.1},
                          "magenta": {"hue": 0.0, "sat": 1.0, "density": 0.0},
                          "skin": {"hue": -3.0, "sat": 1.1, "density": 0.05}},
              "tetra": {"enabled": True,
                        "r": [0.0, 0.06, -0.03], "g": [-0.04, 0.0, 0.02],
                        "b": [0.02, -0.02, 0.0], "c": [0.0, 0.0, 0.0],
                        "m": [0.03, 0.0, -0.01], "y": [0.0, -0.03, 0.02]}},
}


def _lattice_sweep(n: int = SL.LUT_SIZE):
    """33 colours on each of four lines through the cube, all on the lattice.

    On a lattice point tetrahedral interpolation returns the node itself, so
    any difference between ffmpeg and the reference is the .cube's six decimal
    places and the 16-bit round trip, and nothing else.
    """
    k = np.arange(n) / float(n - 1)
    lines = {
        "grey": np.stack([k, k, k], axis=-1),
        "red_ramp": np.stack([k, np.zeros(n), np.zeros(n)], axis=-1),
        "warm": np.stack([k, k * (24.0 / 32.0), k * (8.0 / 32.0)], axis=-1),
        "teal": np.stack([k * (8.0 / 32.0), k, k * (28.0 / 32.0)], axis=-1),
    }
    out = {}
    for name, arr in lines.items():
        # Snap to the lattice: k * (j/32) is not a lattice value in general.
        snapped = np.round(arr * (n - 1)) / (n - 1)
        for i in range(n):
            out[f"{name}{i:02d}"] = snapped[i].tolist()
    return out


def test_ffmpeg_lut3d_matches_the_numpy_reference(ctx):
    """The render and the reference must agree to under one 16-bit code."""
    patches = _lattice_sweep()
    lut = SL.slice_lut(EVERYTHING)
    got = H.render_through_lut(lut, patches)

    names = list(patches)
    # The PNG writer quantises to 16 bit, so the value ffmpeg actually saw is
    # the rounded one. Comparing against the unrounded request would charge the
    # LUT for the encoder's rounding.
    seen = np.round(np.array([patches[n] for n in names]) * 65535.0) / 65535.0
    want = SL.apply_all(seen, EVERYTHING)
    have = np.array([got[n] for n in names])

    err = np.abs(have - want) * 65535.0
    ctx.note(f"{len(names)} lattice colours through lut3d=interp=tetrahedral: "
             f"max error {err.max():.4f} of 65535, mean {err.mean():.4f}")
    ctx.note(f"worst colour: {names[int(err.max(axis=1).argmax())]}")
    # Two forms of the same claim, because the error has exactly two sources
    # and lumping them together would hide which one is which.
    #
    # ffmpeg's lut3d TRUNCATES its 16-bit output rather than rounding it:
    # measured over these colours the signed error against the continuous
    # reference runs -1.0166 to +0.3370 codes and averages -0.4471, which is
    # the shape of a floor and not of a rounding. Against a reference put
    # through the same floor the max is 1 code and the mean is 0.0152, the
    # 1.5 percent of samples where the .cube's six decimal places (worth
    # 0.0328 of a code) push a value across an integer boundary.
    have_codes = np.rint(have * 65535.0)
    floor_err = np.abs(have_codes - np.floor(want * 65535.0))
    ctx.note(f"against a floor-quantised reference: max {floor_err.max():.0f} "
             f"of 65535, mean {floor_err.mean():.4f}")
    ctx.expect_le("ffmpeg's lut3d matches the numpy reference within 1 of 65535",
                  float(floor_err.max()), 1.0)
    # 1.04 = one code of truncation plus the 0.0328 the cube's six decimals
    # cost. Anything above that is a real disagreement, not a rounding.
    ctx.expect_le("the raw difference stays inside truncation plus cube precision",
                  float(err.max()), 1.04)

    # The same cube must not be an identity by accident: a bake that did
    # nothing would pass the line above trivially.
    moved = np.abs(want - seen).max() * 65535.0
    ctx.note(f"the reference moves these colours by up to {moved:.1f} of 65535")
    ctx.expect_gt("the test config actually does something", float(moved), 500.0)


def test_the_stage_reaches_a_real_render(ctx):
    """End to end on footage: the filter is in the graph and pixels move."""
    base = H.defaults()
    on = H.patch(base, EVERYTHING)
    graph = cg.graph_with_mask(on, H.info_for(CLIP))
    in_graph = "luts/slice/slice_" in graph.replace("\\", "")
    ctx.expect_true("the slice cube is in the filter graph", in_graph,
                    "found" if in_graph else "no slice lut3d in the graph")
    a = H.render(CLIP, base, T)
    b = H.render(CLIP, on, T)
    changed = H.changed_fraction(a, b, thresh=0)
    ctx.note(f"the stage moves {changed * 100:.2f}% of the frame, mean "
             f"|delta| {H.mean_abs_diff(a, b):.4f} of 255")
    ctx.expect_gt("the stage changes the rendered frame", changed, 0.5)


# --------------------------------------------------------------------------
# (f) the skin vector centre
# --------------------------------------------------------------------------

def test_skin_vector(ctx):
    """Where the skin vector sits, measured rather than chosen.

    The centre is the hue of the engine's own skin probe, the swatch match_ref
    already refuses to let a match break. Real footage is then checked against
    it: pixels whose chroma matches that probe have to fall inside the vector's
    support, or the vector would be aimed at a colour the camera never sees.
    """
    import match_ref as MR
    probe = np.array(MR.PROBES["skin"], dtype=np.float64)
    ph = float(C.rgb_to_hsv(probe.reshape(1, 3))[0][0])
    ctx.note(f"match_ref PROBES['skin'] = {probe.tolist()} has hue "
             f"{ph:.4f} degrees")
    ctx.expect_close("SKIN_HUE is the probe's own hue", SL.SKIN_HUE, ph, 5e-4)

    for clip, t in ((H.CLIP_A, H.TIME_A), (H.CLIP_B, H.TIME_B)):
        img = H.render(clip, H.defaults(), t, width=640)
        f = img.astype(np.float64) / 255.0
        h, s, v = C.rgb_to_hsv(f)
        # Brightness normalised out: skin in shade and skin in sun are the same
        # chroma at different V, and only the chroma is what a hue vector aims at.
        scaled = f / np.maximum(v, 1e-9)[..., None] * float(probe.max())
        near = (np.sqrt(((scaled - probe) ** 2).sum(-1)) < 0.06) & (v > 0.08)
        n = int(near.sum())
        if n < 500:
            ctx.check(False, f"{clip.name}: only {n} probe-like pixels to measure")
            continue
        ang = np.deg2rad(h[near])
        mean = float(np.rad2deg(np.arctan2(np.sin(ang).mean(),
                                           np.cos(ang).mean())) % 360.0)
        d = np.abs(((h[near] - SL.SKIN_HUE + 180.0) % 360.0) - 180.0)
        inside = float((d <= SL.VECTOR_HALF_WIDTH).mean())
        ctx.note(f"{clip.name}: {n} probe-like pixels, circular mean hue "
                 f"{mean:.3f} deg, {inside * 100:.1f}% inside the support")
        ctx.expect_close(f"{clip.name} skin sits inside the vector's support",
                         inside, 1.0, 0.0)


def register(suite):
    g = "slice"
    suite.add(g, "disabled_is_byte_identical", test_disabled_is_byte_identical,
              doc="with both blocks at their defaults the graph, inputs and pixels are unchanged")
    suite.add(g, "neutral_bake_is_identity", test_bake_of_a_neutral_config_is_the_identity_grid,
              doc="a neutral config bakes the identity grid exactly")
    suite.add(g, "vector_hue_rotation_is_local", test_vector_hue_rotation_is_local,
              doc="each vector rotates its own hue family and nothing past 60 degrees")
    suite.add(g, "vectors_ignore_greys", test_vector_hue_rotation_ignores_greys,
              doc="the saturation scale keeps every vector off neutral pixels")
    suite.add(g, "density_darkens_saturated", test_density_darkens_saturated_and_spares_grey,
              doc="density darkens saturated colour, leaves grey exactly where it was")
    suite.add(g, "vector_saturation_is_local", test_vector_saturation_gain_is_local,
              doc="the saturation gain is weight scaled and stops at the support")
    suite.add(g, "tetra_pins_black_and_white", test_tetra_moves_a_corner_and_pins_black_and_white,
              doc="a corner moves by its delta, the neutral axis does not move at all")
    suite.add(g, "tetra_interpolates", test_tetra_interpolates_between_the_corners,
              doc="a moved corner drags its neighbourhood and fades with distance")
    suite.add(g, "hue_curves_stay_on_their_axis", test_each_hue_curve_moves_only_its_own_axis,
              doc="each curve moves its own HSV coordinate and leaves the other two")
    suite.add(g, "hue_axes_wrap", test_hue_axes_wrap,
              doc="the hue curves are loops, with no seam at 0 and 360 degrees")
    suite.add(g, "empty_curve_is_identity", test_an_empty_curve_is_the_identity,
              doc="an empty point list is the flat neutral, not the diagonal")
    suite.add(g, "ffmpeg_matches_numpy", test_ffmpeg_lut3d_matches_the_numpy_reference,
              doc="the baked cube through lut3d agrees with the reference to 1 of 65535")
    suite.add(g, "stage_reaches_a_real_render", test_the_stage_reaches_a_real_render,
              doc="the cube is in the graph and the footage frame really moves")
    suite.add(g, "skin_vector", test_skin_vector,
              doc="the skin centre is the engine's own probe hue, checked on real footage")
