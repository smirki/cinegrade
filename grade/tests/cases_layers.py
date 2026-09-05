"""Group 11: the layer stack.

A layer is a colour correction under a mask, and the stack is a list of them.
It replaces the single `secondary` block and the single `window` block, so the
first thing this group has to prove is that nothing moved: a config written in
the old shape must bake the same cube and build the same graph it always did,
to the byte, or every approved render and every blessed golden is quietly
wrong.

After that the tests are about the things the old shape could not express:

  order       two layers are applied one after the other, so swapping them
              changes the picture. Serial, not parallel: there is no blend
              between layers, each one grades what the one above handed it.
  placement   a layer runs either before the look or after it, and on a real
              look LUT those are different pictures.
  masking     a blur under a power window changes nothing outside the matte,
              exactly zero rather than nearly zero, and mask.invert selects
              the complement of what the same layer selected without it.
  identity    an empty stack, and a stack of disabled layers, build the graph
              that shipped before layers existed.

The exposure test is the one that pins a number rather than a relationship:
a layer with no mask and exposure 0.5 must be the same multiply the engine
already applies for a stop on a display signal, 2 ** (stops / 2.4), because
the whole point of naming the control "exposure" is that it means the same
thing everywhere it appears.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import numpy as np

import harness as H
from harness import cg

CLIP = H.CLIP_A
T = H.TIME_A

# One secondary in the shape configs were written in before layers existed,
# with every control off its default so nothing can pass by accident.
OLD_SECONDARY = {
    "enabled": True, "show_mask": False, "invert": False,
    "hue_center": 30.0, "hue_width": 90.0, "hue_soft": 20.0,
    "sat_low": 0.05, "sat_high": 0.95, "sat_soft": 0.12,
    "lum_low": 0.02, "lum_high": 0.98, "lum_soft": 0.14,
    "hue_shift": 12.0, "sat_gain": 1.6, "lum_gain": 0.8,
    "tint": [0.05, 0.0, -0.03], "strength": 0.9,
}
OLD_WINDOW = {"enabled": True, "shape": "ellipse", "cx": 0.45, "cy": 0.4,
              "w": 0.5, "h": 0.6, "rotation": 12.0, "softness": 0.2,
              "invert": False}

# Hard edged, so "inside" and "outside" are exact sets and the assertions
# about what a mask leaves alone can be exact too.
HARD_ELLIPSE = {"enabled": True, "shape": "ellipse", "cx": 0.5, "cy": 0.5,
                "w": 0.5, "h": 0.5, "rotation": 0.0, "softness": 0.0,
                "invert": False}
STRONG_KEY = {"enabled": True, "hue_center": 30.0, "hue_width": 300.0,
              "hue_soft": 30.0, "sat_low": 0.0, "sat_soft": 0.3,
              "lum_soft": 0.3}
STRONG_CORRECT = {"sat_gain": 0.0, "lum_gain": 0.4}


def layer(patch=None) -> dict:
    """One layer, LAYER_DEFAULTS plus a patch."""
    return cg.deep_merge(cg.LAYER_DEFAULTS, patch or {})


def stack(*layers) -> dict:
    """A config: the defaults carrying these layers, in this order."""
    return H.patch(H.defaults(), {"layers": list(layers)})


def _regions(win, width, height):
    """The boolean inside/outside masks of a window block, from numpy."""
    m = cg.window_matte(win, width, height)
    return m == 255, m == 0


def _max_diff(a, b, where=None) -> int:
    d = np.abs(a.astype(np.int32) - b.astype(np.int32)).max(axis=2)
    if where is not None:
        return int(d[where].max()) if where.any() else 0
    return int(d.max())


def _mean_abs(a, b, where):
    d = np.abs(a.astype(np.int32) - b.astype(np.int32)).max(axis=2)
    return float(d[where].mean()) if where.any() else 0.0


def _touched(img, ref):
    """Which pixels an effect moved at all, as a boolean mask."""
    return np.abs(img.astype(np.int32) - ref.astype(np.int32)).max(axis=2) > 1


def _cube_lines(path) -> list[str]:
    """The numeric lines of a .cube, in order, exactly as they were written."""
    out = []
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith('"'):
            continue
        if line[0].isalpha():
            continue
        out.append(line)
    return out


def _cube_values(path) -> np.ndarray:
    return np.asarray([[float(x) for x in ln.split()] for ln in _cube_lines(path)])


def _cube_files(graph: str) -> list[str]:
    """Every lut3d file path in a filter graph, in the order it applies them."""
    out = []
    for part in graph.split("lut3d=file=")[1:]:
        out.append(part.split(":interp")[0])
    return out


# --------------------------------------------------------------------------
# (a) the migration moves nothing
# --------------------------------------------------------------------------

def _old_secondary_cube(sec: dict, size: int = 33) -> np.ndarray:
    """The cube the pre-layers engine baked for a secondary.

    grade/luts/ is gitignored, so a fresh clone holds no artefact of the old
    engine to diff against. The old formula is therefore spelled out here
    rather than referenced: this function IS the reference, transcribed from
    secondary_lut as it stood before layers replaced it. The one thing it does
    borrow is _soft_window, which layers did not change and which the params
    group already sweeps.
    """
    grid = H.C.identity_grid(size)
    hue, sat, val = H.C.rgb_to_hsv(grid)
    luma = H.C.luma709(grid)[..., 0]
    d = np.abs(((hue - float(sec["hue_center"]) + 180.0) % 360.0) - 180.0)
    half = max(1e-6, float(sec["hue_width"]) / 2.0)
    hs = max(1e-6, float(sec["hue_soft"]))
    w_hue = np.clip(((half + hs) - d) / hs, 0.0, 1.0)
    w_sat = cg._soft_window(sat, float(sec["sat_low"]), float(sec["sat_high"]),
                            float(sec["sat_soft"]))
    w_lum = cg._soft_window(luma, float(sec["lum_low"]), float(sec["lum_high"]),
                            float(sec["lum_soft"]))
    m = w_hue * w_sat * w_lum
    if sec.get("invert"):
        m = 1.0 - m
    if sec.get("show_mask"):
        return np.clip(np.repeat(m[..., None], 3, axis=-1), 0.0, 1.0)
    hue2 = hue + float(sec["hue_shift"]) * m
    sat2 = sat * (1.0 + (float(sec["sat_gain"]) - 1.0) * m)
    val2 = val * (1.0 + (float(sec["lum_gain"]) - 1.0) * m)
    corrected = H.C.hsv_to_rgb(hue2, np.clip(sat2, 0.0, 1.0),
                               np.clip(val2, 0.0, None))
    corrected = corrected + np.asarray(sec["tint"]) * m[..., None]
    return np.clip(grid + (corrected - grid) * float(sec["strength"]), 0.0, 1.0)


def _migrated(old: dict) -> dict:
    """A config in the old shape, read the way load_preset and the studio's
    full_config read it: migrate the raw config FIRST, then merge the defaults.

    The order is not cosmetic. DEFAULTS always supplies an empty `layers`, so
    merging first would make every old config look like a new one and the
    secondary would be dropped without a word.
    """
    return cg.deep_merge(cg.DEFAULTS, cg.migrate_layers(deepcopy(old)))


def test_migrated_secondary_is_byte_identical(ctx):
    """A pre-layers config must bake the same cube and build the same graph.

    Three claims, in order of strength:

      the cube    every one of the 35937 lines the migrated layer writes is
                  character for character the line the old formula writes.
                  Not "close": the same text, so lut3d reads the same numbers.
      the graph   the migrated key-only config's graph is the no-layers graph
                  with exactly ONE lut3d inserted, which is precisely what a
                  secondary used to add. No split, no extra input, no matte.
      the frame   a real frame rendered from the migrated config and from the
                  hand written layer that means the same thing agree to 0.
    """
    info = H.info_for(CLIP)
    key_only = {"secondary": deepcopy(OLD_SECONDARY)}
    mig = _migrated(key_only)
    lay = mig["layers"][0]

    got_path = cg.layer_lut(lay)
    ref = _old_secondary_cube(OLD_SECONDARY)
    H.WORK.mkdir(parents=True, exist_ok=True)
    ref_path = H.WORK / "old_secondary_reference.cube"
    H.C.write_cube(ref_path, ref, 33, "reference")
    got_lines, ref_lines = _cube_lines(got_path), _cube_lines(ref_path)
    ctx.expect_eq("the migrated cube has the old cube's entry count",
                  len(got_lines), len(ref_lines))
    bad = [i for i, (a, b) in enumerate(zip(got_lines, ref_lines)) if a != b]
    if bad:
        i = bad[0]
        ctx.note(f"first difference at entry {i}: "
                 f"{got_lines[i]!r} vs {ref_lines[i]!r}")
    ctx.note(f"{len(got_lines)} cube entries compared as written text")
    ctx.expect_eq("entries differing from the pre-layers formula", len(bad), 0)

    # The graph: the migrated config against the same config with no layers.
    g_mig = cg.graph_with_mask(mig, info)
    g_off = cg.graph_with_mask(H.patch(mig, {"layers": []}), info)
    frag = f"lut3d=file={cg.esc(got_path)}:interp=tetrahedral"
    ctx.expect_true("a key only layer adds no split and no maskedmerge",
                    "split=2" not in g_mig and "maskedmerge" not in g_mig,
                    g_mig[:300])
    ctx.expect_eq("the migrated graph is the old graph plus one lut3d",
                  g_mig.replace("," + frag, "", 1), g_off)
    ctx.expect_eq("and it adds no ffmpeg input",
                  cg.ffmpeg_inputs(str(CLIP), mig, info),
                  cg.ffmpeg_inputs(str(CLIP), H.patch(mig, {"layers": []}), info))

    # The frame, with the window switched on as well so the matte path is
    # covered too. The hand written layer is what a user would write today.
    old = {"secondary": deepcopy(OLD_SECONDARY), "window": deepcopy(OLD_WINDOW)}
    mig_win = _migrated(old)
    hand = stack(layer({
        "mask": {"window": deepcopy(OLD_WINDOW),
                 "key": dict({k: OLD_SECONDARY[k] for k in (
                     "hue_center", "hue_width", "hue_soft", "sat_low",
                     "sat_high", "sat_soft", "lum_low", "lum_high",
                     "lum_soft")}, enabled=True, invert=False)},
        "correct": {"hue_shift": OLD_SECONDARY["hue_shift"],
                    "sat_gain": OLD_SECONDARY["sat_gain"],
                    "lum_gain": OLD_SECONDARY["lum_gain"],
                    "offset": OLD_SECONDARY["tint"],
                    "strength": OLD_SECONDARY["strength"]}}))
    ctx.expect_eq("the migration produces the layer a user would write today",
                  mig_win["layers"], hand["layers"])
    a = H.render(CLIP, mig_win, T)
    b = H.render(CLIP, hand, T)
    ctx.note(f"windowed migration rendered at {a.shape[1]}x{a.shape[0]}")
    ctx.expect_eq("the migrated frame and the hand written frame agree",
                  _max_diff(a, b), 0)


# --------------------------------------------------------------------------
# (b) two layers, in order
# --------------------------------------------------------------------------

def test_two_layers_apply_in_order(ctx):
    """The stack is serial, so swapping two layers changes the picture.

    The pair is chosen so the order cannot possibly wash out: one layer takes
    all the saturation out, the other pushes red in. Desaturate then push and
    the red survives; push then desaturate and it is taken straight back out
    again. Both configs hold the SAME two cubes, so what is measured is the
    order and nothing else.
    """
    grey = layer({"name": "desaturate", "correct": {"saturation": 0.0}})
    red = layer({"name": "push red", "correct": {"offset": [0.25, 0.0, 0.0]}})
    ab = stack(deepcopy(grey), deepcopy(red))
    ba = stack(deepcopy(red), deepcopy(grey))
    info = H.info_for(CLIP)

    # Only the layers' own cubes: the technical CST cubes are in both graphs
    # in the same place and are not what is being ordered here.
    cubes_ab = [c for c in _cube_files(cg.graph_with_mask(ab, info))
                if "/layers/" in c]
    cubes_ba = [c for c in _cube_files(cg.graph_with_mask(ba, info))
                if "/layers/" in c]
    ctx.note(f"layer cube order desaturate-then-red "
             f"{[Path(c).name for c in cubes_ab]}")
    ctx.expect_eq("both stacks apply the same two layer cubes",
                  sorted(cubes_ab), sorted(cubes_ba))
    ctx.expect_true("in the opposite order", cubes_ab == cubes_ba[::-1],
                    f"{[Path(c).name for c in cubes_ab]} vs "
                    f"{[Path(c).name for c in cubes_ba]}")

    img_ab = H.render(CLIP, ab, T)
    img_ba = H.render(CLIP, ba, T)
    moved = H.changed_fraction(img_ab, img_ba, thresh=0)
    worst = _max_diff(img_ab, img_ba)
    sat_ab = H.stats(img_ab)["sat_mean"]
    sat_ba = H.stats(img_ba)["sat_mean"]
    ctx.note(f"swapping the two layers moved {moved * 100:.3f}% of pixels, "
             f"max {worst} of 255")
    ctx.note(f"mean saturation: desaturate-then-red {sat_ab:.5f}, "
             f"red-then-desaturate {sat_ba:.5f}")
    ctx.expect_gt("swapping two layers changes most of the frame", moved, 0.5)
    ctx.expect_gt("and changes it by a lot, not by a rounding step",
                  float(worst), 8.0)
    ctx.expect_gt("pushing red after the desaturation leaves more saturation",
                  sat_ab, sat_ba, 0.01)


# --------------------------------------------------------------------------
# (c) placement
# --------------------------------------------------------------------------

def test_after_look_differs_from_before_look(ctx):
    """The same layer either side of a real look LUT is two pictures.

    Measured on a preset with a real cube loaded, because a look at identity
    would make the two placements the same by definition and the test would
    pass while proving nothing.
    """
    base = H.preset("blockbuster")
    ctx.expect_true("the preset under test really loads a look LUT",
                    bool(base["look"]["lut"]), str(base["look"]))
    corr = {"mask": {"key": deepcopy(STRONG_KEY)}, "correct": {"sat_gain": 0.2,
                                                               "lum_gain": 1.4}}
    before = H.patch(base, {"layers": [layer(dict(corr, placement="before_look"))]})
    after = H.patch(base, {"layers": [layer(dict(corr, placement="after_look"))]})
    info = H.info_for(CLIP)

    look_cube = _cube_files(cg.graph_with_mask(H.patch(base, {"layers": []}),
                                              info))
    ctx.expect_gt("the look really is one cube in the graph",
                  float(len(look_cube)), 0.0)
    lay_cube = cg.esc(cg.layer_lut(layer(dict(corr, placement="before_look"))))
    g_before = cg.graph_with_mask(before, info)
    g_after = cg.graph_with_mask(after, info)
    ctx.expect_lt("before_look puts the layer's cube ahead of the look's",
                  float(g_before.index(lay_cube)),
                  float(g_before.index(look_cube[-1])))
    ctx.expect_gt("after_look puts it behind the look's",
                  float(g_after.index(lay_cube)),
                  float(g_after.index(look_cube[-1])))

    a = H.render(CLIP, before, T)
    b = H.render(CLIP, after, T)
    moved = H.changed_fraction(a, b, thresh=0)
    ctx.note(f"the same layer either side of {base['look']['lut']} moved "
             f"{moved * 100:.3f}% of pixels, max {_max_diff(a, b)} of 255")
    ctx.expect_gt("placement changes the rendered frame", moved, 0.2)
    ctx.expect_gt("and by more than a rounding step", float(_max_diff(a, b)), 8.0)


# --------------------------------------------------------------------------
# (d) a global layer's exposure
# --------------------------------------------------------------------------

def test_global_exposure_matches_the_formula(ctx):
    """A layer with no mask and exposure 0.5 is the engine's own stop.

    A layer always sees a display referred Rec.709 signal, and on a display
    signal a stop is a code multiply of 2 ** (stops / 2.4), not an offset.
    That is exactly the branch f_log_stage takes for convert.exposure when the
    source is already Rec.709, so the two write the same constant, and this
    test reads that constant out of the engine rather than restating it.

    The difference worth documenting: on a LOG source (dwg or direct, which is
    every real clip here) convert.exposure and primaries.temperature/tint are
    offsets on the log signal, because an offset in log IS a linear gain, and
    that is not the same curve as a display code multiply. A layer cannot use
    the log form: it runs after CST OUT, where there is no log curve left to
    offset. So "the same semantics" means the same stop expressed the only way
    the layer's own domain allows, which is the way the engine itself falls
    back to on Rec.709 footage.

    Three measurements: the cube against the formula, the constant against the
    engine's own, and the ratio the stop actually produces on a real frame.
    """
    stops = 0.5
    lay = layer({"correct": {"exposure": stops}})
    path = cg.layer_lut(lay)
    got = _cube_values(path)
    grid = H.C.identity_grid(33)
    mul = 2.0 ** (stops / cg.DISPLAY_GAMMA)
    want = np.clip(grid * mul, 0.0, 1.0)
    worst = float(np.abs(got - want).max())
    ctx.note(f"exposure {stops} on a display signal is a multiply of "
             f"{mul:.6f}; worst cube entry error {worst:.8f}")
    ctx.expect_le("the cube is the formula, to the six decimals it is written in",
                  worst, 5e-7)

    # The engine's own rec709 branch, read straight out of f_log_stage. That
    # branch is unreachable for these clips (check_source_space refuses a log
    # source with working_space rec709), so the filter is built rather than
    # rendered, which is enough: it is a string containing the constant.
    rec = H.patch(H.defaults(), {"convert": {"working_space": "rec709",
                                             "exposure": stops}})
    chain = cg.f_log_stage(rec, {"width": 1920, "height": 1080,
                                 "color_range": "tv", "normalised": True})
    engine_lut = [f for f in chain if f.startswith("lut=")]
    ctx.expect_eq("the engine spends a stop on a display signal as one lut",
                  len(engine_lut), 1)
    ctx.note(f"engine: {engine_lut[0]}")
    ctx.expect_true("and spends it as the same multiply the layer bakes",
                    f"val*{mul:.6f}" in engine_lut[0], engine_lut[0])

    # And on a real frame: away from black and from the clipping point, every
    # code must come back multiplied by that same number.
    plain = H.render(CLIP, H.defaults(), T)
    lifted = H.render(CLIP, H.patch(H.defaults(), {"layers": [lay]}), T)
    sel = (plain > 8) & (plain < 200)
    ratio = float(np.median(lifted[sel].astype(np.float64)
                            / plain[sel].astype(np.float64)))
    ctx.note(f"{int(sel.sum())} unclipped 8-bit samples, median output/input "
             f"ratio {ratio:.5f} against the formula's {mul:.5f}")
    ctx.expect_close("a real frame comes back multiplied by 2**(stops/2.4)",
                     ratio, mul, 0.005)


# --------------------------------------------------------------------------
# (e) blur under a window
# --------------------------------------------------------------------------

def test_blur_under_a_window_stays_inside_it(ctx):
    """A layer's blur is masked like everything else about the layer.

    A gaussian reads its neighbours, so it is the one control that could leak
    past the matte if the graph blurred before the merge instead of inside the
    graded branch. Outside a hard edged window the assertion is exact zero.
    """
    info = H.info_for(CLIP)
    sig_full = cg.layer_blur_sigma(layer({"correct": {"blur": 30.0}}),
                                   {"width": 1920})
    sig_here = cg.layer_blur_sigma(layer({"correct": {"blur": 30.0}}), info)
    ctx.note(f"blur 30 is {sig_full:.3f} px at 1920 wide and "
             f"{sig_here:.3f} px at this frame's {info['width']}")
    ctx.expect_close("the sigma is quoted at 1920 wide", sig_full, 30.0, 1e-9)
    ctx.expect_close("and scales with the frame like every window fraction",
                     sig_here, 30.0 * info["width"] / 1920.0, 1e-9)

    win = {"mask": {"window": deepcopy(HARD_ELLIPSE)}}
    sharp = stack(layer(dict(deepcopy(win), correct={"blur": 0.0})))
    blurred = stack(layer(dict(deepcopy(win), correct={"blur": 30.0})))
    a = H.render(CLIP, sharp, T)
    b = H.render(CLIP, blurred, T)
    h, w, _ = a.shape
    inside, outside = _regions(HARD_ELLIPSE, w, h)

    out_worst = _max_diff(a, b, outside)
    in_mean = _mean_abs(a, b, inside)
    ctx.note(f"{int(inside.sum())} pixels inside the window, "
             f"{int(outside.sum())} outside")
    ctx.note(f"blur on vs off: inside mean {in_mean:.4f} of 255, "
             f"outside worst {out_worst}")
    ctx.expect_eq("outside the window the blur changes nothing at all",
                  out_worst, 0)
    ctx.expect_gt("inside the window it plainly blurs", in_mean, 1.0)


# --------------------------------------------------------------------------
# (f) mask.invert
# --------------------------------------------------------------------------

def test_mask_invert_flips_which_pixels_change(ctx):
    """mask.invert selects the complement of the whole mask, not of one half.

    With a window AND a key the combined matte is window * key, and 1 - w*k
    does not factor into a colour times a position. It does split, though:
    1 - w*k = (1 - w) * 1 + w * (1 - k), which the engine builds as two graded
    branches merged under the untouched window matte. So outside the window an
    inverted layer must be the FULL correction, byte for byte the same as the
    same layer with no mask at all, and that is asserted exactly rather than
    approximately.
    """
    body = {"mask": {"window": deepcopy(HARD_ELLIPSE),
                     "key": deepcopy(STRONG_KEY)},
            "correct": deepcopy(STRONG_CORRECT)}
    normal = stack(layer(deepcopy(body)))
    flipped = stack(layer(H.patch(deepcopy(body), {"mask": {"invert": True}})))
    everywhere = stack(layer({"correct": deepcopy(STRONG_CORRECT)}))
    plain = H.render(CLIP, H.defaults(), T)
    n = H.render(CLIP, normal, T)
    f = H.render(CLIP, flipped, T)
    g = H.render(CLIP, everywhere, T)
    h, w, _ = n.shape
    inside, outside = _regions(HARD_ELLIPSE, w, h)

    ctx.note(f"normal touches {_touched(n, plain).mean() * 100:.2f}% of pixels, "
             f"inverted {_touched(f, plain).mean() * 100:.2f}%")
    ctx.expect_eq("outside the window the normal layer changes nothing",
                  _max_diff(n, plain, outside), 0)
    ctx.expect_gt("outside the window the inverted layer changes a lot",
                  _mean_abs(f, plain, outside), 10.0)
    ctx.expect_eq("and outside the window it is exactly the unmasked correction",
                  _max_diff(f, g, outside), 0)

    a, b = _touched(n, plain), _touched(f, plain)
    overlap = float((a & b).sum()) / max(1.0, float((a | b).sum()))
    ctx.note(f"the two selections overlap on {overlap * 100:.2f}% of the "
             f"pixels either of them touches")
    ctx.expect_lt("inverting picks a different set of pixels", overlap, 0.35)


# --------------------------------------------------------------------------
# (g) an empty stack is the graph that shipped
# --------------------------------------------------------------------------

def test_empty_layers_is_byte_identical(ctx):
    """No layers, and a stack of disabled layers, build the pre-layers graph.

    Compared three ways, because any one of them alone could pass while the
    render still moved: the graph text, the ffmpeg input list (a stray extra
    -i shifts every later input index), and the pixels.
    """
    info = H.info_for(CLIP)
    combos = []
    for radial in (False, True):
        for grain in (False, True):
            c = H.patch(H.defaults(), {"fx": {"radial_blur": {"enabled": radial}},
                                       "grain": {"enabled": grain}})
            combos.append((f"radial={radial} grain={grain}", c))

    # A disabled layer, and a layer whose combined matte is zero everywhere
    # (mask.invert with nothing to invert), both of which have to drop out.
    dead = [layer({"enabled": False, "mask": {"window": deepcopy(HARD_ELLIPSE),
                                              "key": deepcopy(STRONG_KEY)},
                   "correct": deepcopy(STRONG_CORRECT)}),
            layer({"mask": {"invert": True},
                   "correct": deepcopy(STRONG_CORRECT)})]

    graph_diffs, input_diffs = [], []
    for tag, cfg in combos:
        bare = deepcopy(cfg)
        bare.pop("layers")
        for label, variant in (("empty", H.patch(cfg, {"layers": []})),
                               ("disabled", H.patch(cfg, {"layers": deepcopy(dead)}))):
            if cg.graph_with_mask(variant, info) != cg.graph_with_mask(bare, info):
                graph_diffs.append(f"{tag} {label}")
            if (cg.ffmpeg_inputs(str(CLIP), variant, info)
                    != cg.ffmpeg_inputs(str(CLIP), bare, info)):
                input_diffs.append(f"{tag} {label}")
    ctx.note(f"{len(combos) * 2} configs compared against a config with no "
             f"layers key at all")
    ctx.expect_true("an inert stack leaves the graph text alone",
                    not graph_diffs, f"differed: {graph_diffs}")
    ctx.expect_true("and adds no ffmpeg input",
                    not input_diffs, f"differed: {input_diffs}")

    base = H.preset("cinekit")
    bare = deepcopy(base)
    bare.pop("layers")
    ctx.expect_eq("an inert stack renders the identical frame",
                  _max_diff(H.render(CLIP, H.patch(base, {"layers": deepcopy(dead)}), T),
                            H.render(CLIP, bare, T)), 0)


def register(suite):
    g = "layers"
    suite.add(g, "migrated_secondary_is_byte_identical",
              test_migrated_secondary_is_byte_identical,
              doc="an old secondary bakes the same cube and builds the same graph")
    suite.add(g, "two_layers_apply_in_order", test_two_layers_apply_in_order,
              doc="the stack is serial, so swapping two layers changes the picture")
    suite.add(g, "after_look_differs_from_before_look",
              test_after_look_differs_from_before_look,
              doc="the same layer either side of a real look LUT is two pictures")
    suite.add(g, "global_exposure_matches_the_formula",
              test_global_exposure_matches_the_formula,
              doc="a global layer's exposure is the engine's own 2**(stops/2.4)")
    suite.add(g, "blur_under_a_window_stays_inside_it",
              test_blur_under_a_window_stays_inside_it,
              doc="a layer's gaussian changes nothing outside its matte")
    suite.add(g, "mask_invert_flips_which_pixels_change",
              test_mask_invert_flips_which_pixels_change,
              doc="invert selects the complement, exactly, on both branches")
    suite.add(g, "empty_layers_is_byte_identical",
              test_empty_layers_is_byte_identical,
              doc="an empty or inert stack builds the graph that shipped before")
