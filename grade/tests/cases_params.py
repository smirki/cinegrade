"""Group 3: every parameter in DEFAULTS does something, in the right direction.

Two assertions per parameter:

  effect     moving the value must change pixels. A parameter that is declared
             and never read is the exact bug that shipped once already with
             look.mix, so "no visible change" is a failure here, never a skip.
  direction  the change must go the way the name promises. Exposure up is
             brighter, saturation up is more saturated, contrast up is a wider
             luma spread, a higher halation threshold is a weaker effect.

A coverage test walks DEFAULTS and fails if any leaf key is missing from the
table, so adding a parameter to the engine without testing it breaks the suite.
"""

from __future__ import annotations

import numpy as np

import harness as H
from harness import cg

CLIP = H.CLIP_A
T = H.TIME_A

# --------------------------------------------------------------------------
# metrics, all called as metric(img, ref)
# --------------------------------------------------------------------------

def _stat(key):
    return lambda img, ref: H.stats(img)[key]


def _delta(img, ref):
    return H.mean_abs_diff(img, ref)


def _changed(img, ref):
    return H.changed_fraction(img, ref, thresh=1)


def _reach(img, ref):
    """Fraction of the frame the effect touches at all.

    Threshold zero, not one: a very wide scatter spreads the same light over so
    many pixels that no single pixel moves a whole 8-bit code, yet the effect
    plainly reaches further. Counting any change is what makes "wider"measurable.
    """
    return H.changed_fraction(img, ref, thresh=0)


def _effect_hf(img, ref):
    """High-frequency energy of the effect alone, not of the picture.

    A wider blur spreads the same energy over more pixels, so the difference
    layer gets smoother. Measuring the whole frame instead would drown that in
    the source's own detail.
    """
    d = img.astype(np.float64) - ref.astype(np.float64)
    return float(np.abs(d - H.box_blur(d, 3)).mean())


def _bars(img, ref):
    return float(H.black_bar_rows(img))


METRICS = {
    "luma_mean": _stat("luma_mean"), "luma_std": _stat("luma_std"),
    "luma_p5": _stat("luma_p5"), "luma_p95": _stat("luma_p95"),
    "luma_p99": _stat("luma_p99"), "luma_max": lambda img, ref: float(H.luma(img).max()),
    "sat_mean": _stat("sat_mean"), "hf": _stat("hf"),
    "clip_high": _stat("clip_high"),
    "r_mean": _stat("r_mean"), "g_mean": _stat("g_mean"), "b_mean": _stat("b_mean"),
    "warmth": _stat("warmth"), "greenness": _stat("greenness"),
    "blueness": _stat("blueness"),
    "delta": _delta, "changed": _changed, "reach": _reach,
    "effect_hf": _effect_hf, "bars": _bars,
}


class P:
    """One parameter's test case."""

    def __init__(self, path, lo, hi, metric=None, direction=None, base=None,
                 ref=None, margin=0.0, clip=None, t=None, xfail=None,
                 extra=None, doc=""):
        self.path = path
        self.lo, self.hi = lo, hi
        self.metric = metric
        self.direction = direction        # "up" or "down", relative to lo -> hi
        self.base = base or {}
        self.ref = ref                    # patch giving the reference frame
        self.margin = margin
        self.clip = clip or CLIP
        self.t = T if t is None else t
        self.xfail = xfail
        self.extra = extra
        self.doc = doc

    @property
    def name(self):
        return self.path.replace(".", "_")


# --------------------------------------------------------------------------
# shared bases
# --------------------------------------------------------------------------

# A reference frame is rendered from base + ref. Turning the stage OFF has to
# be spelled out: an empty patch would re-render the base, which for these
# cases already has the stage on, and every delta would come out zero.
NO_HAL = {"fx": {"halation": {"enabled": False}}}
NO_BLOOM = {"fx": {"bloom": {"enabled": False}}}
NO_SPLIT = {"fx": {"rgb_split": {"enabled": False}}}
NO_RADIAL = {"fx": {"radial_blur": {"enabled": False}}}
NO_GRAIN = {"grain": {"enabled": False}}
NO_SEC = {"secondary": {"enabled": False}}
NO_LOOK = {"look": {"lut": None}}

HAL = {"fx": {"halation": {"enabled": True}}}
BLOOM = {"fx": {"bloom": {"enabled": True}}}
SPLIT = {"fx": {"rgb_split": {"enabled": True}}}
RADIAL = {"fx": {"radial_blur": {"enabled": True}}}
VIG = {"fx": {"vignette": {"enabled": True}}}
GRAIN = {"grain": {"enabled": True, "strength": 40}}
LETTER = {"letterbox": {"enabled": True}}
CURVE_ON = {"curves": {"enabled": True,
                       "master": [[0.0, 0.0], [0.3, 0.5], [1.0, 1.0]]}}
# A shadow point far from its neighbours is what makes a natural spline
# overshoot; pchip cannot, which is why the engine defaults to it.
CURVE_SPARSE = {"curves": {"enabled": True,
                           "master": [[0.0, 0.0], [0.12, 0.42], [1.0, 1.0]]}}
# Clip A is 35% orange by area, so the default 30 degree hue centre keys a
# large, real part of the frame rather than a handful of stray pixels.
SEC_ON = {"secondary": {"enabled": True, "hue_center": 30.0,
                        "sat_gain": 2.2, "lum_gain": 1.25}}
CONTRASTED = {"primaries": {"contrast": 1.45}}


def _sec(**kw):
    base = {"secondary": dict(SEC_ON["secondary"])}
    base["secondary"].update(kw)
    return base


CASES = [
    # --- convert ---------------------------------------------------------
    P("convert.tonemap", "aces", "none", "clip_high", "up",
      base={"convert": {"exposure": 1.5, "working_space": "direct"}},
      doc="AGENTS.md: 'none' clips anything over diffuse white, aces rolls off. "
          "Run on the direct path because the dwg pair has no 'none' LUT, "
          "which is its own test below"),
    P("convert.exposure", 0.0, 0.8, "luma_mean", "up",
      doc="exposure up is brighter"),
    P("convert.working_space", "dwg", "direct", None, None,
      doc="changes the graph; the headroom direction is color.direct_clips_more_than_dwg"),
    P("convert.encode", "rec709a", "gamma24", "luma_mean", "up",
      base={"convert": {"working_space": "dwg"}},
      doc="gamma24 encodes higher codes than rec709a (direct path: see color group)"),

    # --- primaries -------------------------------------------------------
    P("primaries.contrast", 1.0, 1.45, "luma_std", "up",
      doc="contrast up widens the luma spread"),
    P("primaries.pivot", None, 0.62, "luma_mean", "down", base=CONTRASTED,
      doc="a higher contrast pivot pushes more of the frame down"),
    P("primaries.saturation", 1.0, 1.6, "sat_mean", "up",
      doc="saturation up is more saturated"),
    P("primaries.vibrance", 0.0, 0.6, "sat_mean", "up",
      doc="vibrance up is more saturated, sparing what is already saturated"),
    P("primaries.temperature", 0.0, 0.12, "warmth", "up",
      doc="AGENTS.md: temperature positive is warmer"),
    P("primaries.tint", 0.0, 0.12, "greenness", "up",
      doc="AGENTS.md: tint positive is greener"),
    P("primaries.brightness", 0.0, 0.10, "luma_mean", "up",
      doc="brightness is the offset wheel, a flat add"),
    P("primaries.lift", 0.0, 0.12, "luma_p5", "up",
      doc="lift raises where black lands"),
    P("primaries.gamma", 1.0, 1.5, "luma_mean", "up",
      doc="gamma up is brighter mids"),
    P("primaries.gain", 1.0, 1.4, "luma_p95", "up",
      doc="gain up raises white. Gain above 1.0 used to error out under "
          "colorlevels, so this also pins that regression"),
    P("primaries.black_lift", 0.0, 0.10, "luma_p5", "up",
      doc="black lift raises the bottom of the curve"),
    P("primaries.highlight_rolloff", 0.0, 0.35, "luma_max", "down",
      doc="highlight roll-off takes the top off"),

    # --- look ------------------------------------------------------------
    P("look.lut", None, "blockbuster", None, None,
      doc="selecting a look LUT changes the picture"),
    P("look.mix", 0.0, 1.0, "delta", "up",
      base={"look": {"lut": "blockbuster"}}, ref=NO_LOOK,
      doc="mix is look strength; the pixel-exact ends are the look group"),

    # --- detail ----------------------------------------------------------
    P("detail.soften", 0.0, 3.0, "hf", "down", doc="soften removes detail"),
    P("detail.sharpen", 0.0, 1.5, "hf", "up", doc="sharpen adds edge energy"),

    # --- fx.halation -----------------------------------------------------
    P("fx.halation.enabled", False, True, "delta", "up", ref=NO_HAL,
      doc="the toggle must change pixels"),
    P("fx.halation.threshold", 0.40, 0.92, "delta", "down", base=HAL, ref=NO_HAL,
      doc="a higher threshold selects fewer highlights, so a weaker glow"),
    P("fx.halation.sigma", 4, 60, "reach", "up", base=HAL, ref=NO_HAL,
      doc="a wider scatter is a smoother difference layer"),
    P("fx.halation.strength", 0.10, 0.90, "delta", "up", base=HAL, ref=NO_HAL,
      doc="strength is the screen-blend opacity"),
    P("fx.halation.tint", [1.0, 0.34, 0.16], [0.16, 0.34, 1.0], "blueness", "up",
      base=H.patch(HAL, {"fx": {"halation": {"strength": 0.9, "threshold": 0.5}}}),
      doc="the tint colours the glow, so a blue tint pushes blue up"),

    # --- fx.bloom --------------------------------------------------------
    P("fx.bloom.enabled", False, True, "delta", "up", ref=NO_BLOOM,
      doc="the toggle must change pixels"),
    P("fx.bloom.threshold", 0.45, 0.95, "delta", "down", base=BLOOM, ref=NO_BLOOM,
      doc="a higher threshold selects fewer highlights"),
    P("fx.bloom.sigma", 8, 160, "reach", "up", base=BLOOM, ref=NO_BLOOM,
      doc="a wider bloom is a smoother difference layer"),
    P("fx.bloom.strength", 0.08, 0.80, "delta", "up", base=BLOOM, ref=NO_BLOOM,
      doc="strength is the screen-blend opacity"),
    P("fx.bloom.tint", [1.0, 0.97, 0.92], [0.30, 0.60, 1.0], "blueness", "up",
      base=H.patch(BLOOM, {"fx": {"bloom": {"strength": 0.8, "threshold": 0.5}}}),
      doc="the tint colours the bloom"),

    # --- fx.rgb_split ----------------------------------------------------
    P("fx.rgb_split.enabled", False, True, "delta", "up", ref=NO_SPLIT,
      doc="the toggle must change pixels"),
    P("fx.rgb_split.amount", 1.0, 6.0, "delta", "up", base=SPLIT, ref=NO_SPLIT,
      doc="a larger lateral shift moves more pixels further"),

    # --- fx.radial_blur --------------------------------------------------
    P("fx.radial_blur.enabled", False, True, "delta", "up", ref=NO_RADIAL,
      doc="the toggle must change pixels"),
    P("fx.radial_blur.sigma", 2, 30, "hf", "down", base=RADIAL,
      doc="a bigger blur sigma is less detail at the edges"),
    P("fx.radial_blur.start", 0.20, 0.92, "delta", "down", base=RADIAL, ref=NO_RADIAL,
      doc="start is where the blur begins, so pushing it out blurs less"),
    P("fx.radial_blur.end", 1.0, 3.0, "delta", "down",
      base=H.patch(RADIAL, {"fx": {"radial_blur": {"start": 0.2}}}), ref=NO_RADIAL,
      doc="end is where the blur reaches full strength, so pushing it out "
          "stretches the ramp and blurs less"),

    # --- fx.vignette -----------------------------------------------------
    P("fx.vignette.enabled", False, True, "luma_mean", "down",
      doc="AGENTS.md: vignette lowers YAVG, a 10 to 15 drop is expected"),
    P("fx.vignette.amount", 0.10, 0.90, "luma_mean", "down", base=VIG,
      doc="more vignette is darker corners"),
    # Was xfail while radius was inert. It reaches the ffmpeg vignette now, as
    # a divisor on the angle, so a bigger radius pulls the darkening back into
    # the corners and the frame gets brighter. Asserted on luma_mean rather than
    # on a delta from the default, because a delta only proves the parameter is
    # connected to something, not that it moves the picture the way the label
    # says it does.
    P("fx.vignette.radius", 0.50, 1.50, "luma_mean", "up", base=VIG,
      doc="radius sets how far the darkening reaches in from the corners, so a "
          "bigger radius darkens less of the frame"),

    # --- grain -----------------------------------------------------------
    P("grain.enabled", False, True, "hf", "up",
      doc="grain adds high-frequency energy"),
    P("grain.strength", 5, 90, "hf", "up", base=GRAIN,
      doc="stronger grain is more high-frequency energy"),
    P("grain.size", 1, 8, "hf", "down", base=GRAIN,
      doc="size is the plate's downscale factor, so bigger grain is coarser "
          "and carries less per-pixel energy"),
    P("grain.opacity", 0.10, 0.90, "delta", "up", base=GRAIN,
      ref={"grain": {"enabled": False}},
      doc="opacity is the overlay blend weight"),

    # --- letterbox -------------------------------------------------------
    P("letterbox.enabled", False, True, "bars", "up",
      doc="letterbox adds black bars"),
    P("letterbox.aspect", 1.85, 2.39, "bars", "up", base=LETTER,
      doc="a wider target aspect crops more, so taller bars"),

    # --- curves ----------------------------------------------------------
    P("curves.enabled", False, True, "luma_mean", "up", base=CURVE_ON,
      doc="the curves node must apply its points"),
    P("curves.interp", "pchip", "natural", "luma_p95", "up", base=CURVE_SPARSE,
      doc="a natural spline overshoots between widely spaced points; pchip is "
          "monotone and cannot, which is why pchip is the default"),
    P("curves.master", [[0.0, 0.0], [1.0, 1.0]], [[0.0, 0.0], [0.3, 0.5], [1.0, 1.0]],
      "luma_mean", "up", base={"curves": {"enabled": True}},
      doc="a lifted master curve is brighter"),
    P("curves.r", [[0.0, 0.0], [1.0, 1.0]], [[0.0, 0.0], [0.3, 0.5], [1.0, 1.0]],
      "r_mean", "up", base={"curves": {"enabled": True}},
      doc="the red curve moves red"),
    P("curves.g", [[0.0, 0.0], [1.0, 1.0]], [[0.0, 0.0], [0.3, 0.5], [1.0, 1.0]],
      "g_mean", "up", base={"curves": {"enabled": True}},
      doc="the green curve moves green"),
    P("curves.b", [[0.0, 0.0], [1.0, 1.0]], [[0.0, 0.0], [0.3, 0.5], [1.0, 1.0]],
      "b_mean", "up", base={"curves": {"enabled": True}},
      doc="the blue curve moves blue"),

    # --- secondary -------------------------------------------------------
    P("secondary.enabled", False, True, "delta", "up", base=SEC_ON, ref=NO_SEC,
      doc="the qualifier must apply its correction"),
    P("secondary.hue_center", 30.0, 210.0, None, None, base=SEC_ON,
      doc="moving the key to a different hue keys different pixels"),
    P("secondary.hue_width", 20.0, 200.0, "changed", "up", base=SEC_ON, ref=NO_SEC,
      doc="a wider hue window keys more of the frame"),
    P("secondary.hue_soft", 2.0, 70.0, "changed", "up", base=SEC_ON, ref=NO_SEC,
      doc="softness grows the selection outward, per _soft_window's docstring"),
    P("secondary.sat_low", 0.0, 0.65, "changed", "down", base=SEC_ON, ref=NO_SEC,
      doc="raising the saturation floor keys fewer pixels"),
    P("secondary.sat_high", 1.0, 0.25, "changed", "down", base=SEC_ON, ref=NO_SEC,
      doc="lowering the saturation ceiling keys fewer pixels"),
    P("secondary.sat_soft", 0.02, 0.45, "changed", "up",
      base=_sec(sat_low=0.25), ref=NO_SEC,
      doc="softness grows the saturation window outward"),
    P("secondary.lum_low", 0.0, 0.55, "changed", "down", base=SEC_ON, ref=NO_SEC,
      doc="raising the luma floor keys fewer pixels"),
    P("secondary.lum_high", 1.0, 0.30, "changed", "down", base=SEC_ON, ref=NO_SEC,
      doc="lowering the luma ceiling keys fewer pixels"),
    P("secondary.lum_soft", 0.02, 0.45, "changed", "up",
      base=_sec(lum_low=0.30), ref=NO_SEC,
      doc="softness grows the luma window outward"),
    P("secondary.hue_shift", 0.0, 90.0, "delta", "up",
      base=_sec(sat_gain=1.0, lum_gain=1.0, hue_width=90.0), ref=NO_SEC,
      doc="hue shift rotates the keyed hue"),
    P("secondary.sat_gain", 1.0, 2.5, "sat_mean", "up",
      base=_sec(sat_gain=1.0, lum_gain=1.0, hue_width=90.0),
      doc="saturation gain on the keyed range"),
    P("secondary.lum_gain", 1.0, 1.8, "luma_mean", "up",
      base=_sec(sat_gain=1.0, lum_gain=1.0, hue_width=90.0),
      doc="luma gain on the keyed range"),
    P("secondary.tint", [0.0, 0.0, 0.0], [0.25, 0.0, 0.0], "r_mean", "up",
      base=_sec(sat_gain=1.0, lum_gain=1.0, hue_width=90.0),
      doc="tint adds a flat colour to the keyed range"),
    P("secondary.strength", 0.2, 1.0, "delta", "up",
      base=_sec(hue_width=90.0), ref=NO_SEC,
      doc="strength mixes the correction back toward the original"),
    P("secondary.invert", False, True, None, None,
      base=_sec(hue_width=90.0),
      doc="invert keys the complement of the selection"),
    P("secondary.show_mask", False, True, None, None, base=SEC_ON,
      doc="show_mask replaces the picture with the greyscale matte"),
]

# Parameters asserted in another group instead of by a lo/hi sweep here.
# The output ones are only visible in an encoded file. The window ones are all
# one geometry, so sweeping them one at a time would say much less than the
# window group's own matte and gating assertions do.
COVERED_ELSEWHERE = {
    "output.codec": "output.codec_and_profile",
    "output.profile": "output.codec_and_profile",
    "output.crf": "output.crf_and_preset",
    "output.preset": "output.crf_and_preset",
    "window.enabled": "window.disabled_is_byte_identical",
    "window.shape": "window.rect_rotated_45_moves_the_corners",
    "window.cx": "window.matte_scales_with_the_frame",
    "window.cy": "window.matte_scales_with_the_frame",
    "window.w": "window.geq_matte_matches_numpy",
    "window.h": "window.geq_matte_matches_numpy",
    "window.rotation": "window.rect_rotated_45_moves_the_corners",
    "window.softness": "window.softness_zero_is_binary",
    "window.invert": "window.invert_swaps_inside_and_outside",
}


# --------------------------------------------------------------------------
# extra assertions for the cases that need more than a scalar direction
# --------------------------------------------------------------------------

def _extra_show_mask(ctx, lo, hi, ref):
    f = hi.astype(np.int16)
    spread = int(np.abs(f - f[..., :1]).max())
    ctx.expect_le("show_mask output is greyscale (max channel spread, 8-bit)",
                  float(spread), 1.0)


def _extra_invert(ctx, lo, hi, ref):
    """The inverted key must touch a different set of pixels, not merely differ."""
    base = ref if ref is not None else lo
    a = np.abs(lo.astype(np.int16) - base.astype(np.int16)).max(axis=2) > 1
    b = np.abs(hi.astype(np.int16) - base.astype(np.int16)).max(axis=2) > 1
    overlap = float((a & b).sum()) / max(1.0, float((a | b).sum()))
    ctx.note(f"normal key touches {a.mean() * 100:.2f}% of pixels, "
             f"inverted touches {b.mean() * 100:.2f}%")
    ctx.expect_lt("overlap between the normal and inverted selections", overlap, 0.35)


def _extra_gain_above_one(ctx, lo, hi, ref):
    ctx.note("gain 1.4 rendered without an ffmpeg error, which is the "
             "colorlevels output-point cap regression")


EXTRAS = {
    "secondary.show_mask": _extra_show_mask,
    "secondary.invert": _extra_invert,
    "primaries.gain": _extra_gain_above_one,
}


# --------------------------------------------------------------------------
# the test body
# --------------------------------------------------------------------------

def _make_test(case: P):
    def run(ctx):
        base_cfg = H.patch(H.defaults(), case.base)
        cfg_lo = H.set_path(base_cfg, case.path, case.lo)
        cfg_hi = H.set_path(base_cfg, case.path, case.hi)
        img_lo = H.render(case.clip, cfg_lo, case.t)
        img_hi = H.render(case.clip, cfg_hi, case.t)

        changed = H.changed_fraction(img_lo, img_hi, thresh=0)
        ctx.note(f"{case.path}: {case.lo!r} -> {case.hi!r} moved "
                 f"{changed * 100:.3f}% of pixels "
                 f"(mean |delta| {H.mean_abs_diff(img_lo, img_hi):.4f} of 255)")
        ctx.expect_gt("the parameter changes the rendered frame", changed, 0.0)

        ref = None
        if case.ref is not None:
            ref = H.render(case.clip, H.patch(base_cfg, case.ref), case.t)

        if case.metric:
            fn = METRICS[case.metric]
            m_lo, m_hi = fn(img_lo, ref), fn(img_hi, ref)
            if case.direction == "up":
                ctx.expect_gt(f"{case.metric} rises with {case.path}",
                              m_hi, m_lo, case.margin)
            else:
                ctx.expect_lt(f"{case.metric} falls with {case.path}",
                              m_hi, m_lo, case.margin)

        extra = EXTRAS.get(case.path)
        if extra:
            extra(ctx, img_lo, img_hi, ref)

    return run


def test_every_default_is_covered(ctx):
    """A parameter added to DEFAULTS without a test is a hole in this suite."""
    declared = set(H.leaf_paths(cg.DEFAULTS))
    covered = {c.path for c in CASES} | set(COVERED_ELSEWHERE)
    missing = sorted(declared - covered)
    stale = sorted(covered - declared)
    ctx.note(f"{len(declared)} leaf parameters in DEFAULTS, "
             f"{len(CASES)} tested here, "
             f"{len(COVERED_ELSEWHERE)} tested in the output and window groups")
    ctx.expect_true("every DEFAULTS parameter has a test",
                    not missing, f"untested: {missing}" if missing else "none missing")
    ctx.expect_true("no test targets a parameter that no longer exists",
                    not stale, f"stale: {stale}" if stale else "none stale")


def test_every_advertised_tonemap_renders(ctx):
    """--tonemap offers aces, filmic and none. All three must work.

    The CLI advertises the choice, so a choice that cannot be built is a bug
    the user only finds when a render dies. f_convert_out asks for
    DWG_to_Rec709_{tonemap}_{encode}.cube, and the DWG pair is only generated
    for aces and filmic (AGENTS.md "Rebuilding LUTs" loops over those two).
    """
    for ws in ("dwg", "direct"):
        for tm in ("aces", "filmic", "none"):
            cfg = H.patch(H.defaults(),
                          {"convert": {"tonemap": tm, "working_space": ws}})
            try:
                img = H.render(CLIP, cfg, T)
                ctx.check(True, f"{ws}/{tm}: rendered, "
                                f"mean luma {H.stats(img)['luma_mean']:.4f}")
            except Exception as exc:
                ctx.check(False, f"{ws}/{tm}: {str(exc).splitlines()[0][:160]}")


def test_tone_ends_spline_leaves_mid_tones_alone(ctx):
    """black_lift and highlight_rolloff must not drag the mid tones.

    f_primaries builds them as one curve, 0/bl 0.5/0.5 1/(1-hr), and its comment
    says it "takes the hard ends off without touching mid-tone contrast". That
    curve runs inside the working space, where the DEFAULTS comment on the
    curves node warns that "a hand-placed point at 0.5 would be pulling on a
    highlight, not a mid tone", because DWG mid grey is 0.336.
    """
    base = H.render(CLIP, H.defaults(), T)
    p50_0 = H.stats(base)["luma_p50"]
    for name, value in (("black_lift", 0.10), ("highlight_rolloff", 0.35)):
        img = H.render(CLIP, H.patch(H.defaults(), {"primaries": {name: value}}), T)
        st = H.stats(img)
        ctx.note(f"{name}={value}: p1 {st['luma_p1']:.4f} p50 {st['luma_p50']:.4f} "
                 f"p99 {st['luma_p99']:.4f} max {float(H.luma(img).max()):.4f}")
        ctx.expect_close(f"{name} leaves the median where it was",
                         st["luma_p50"], p50_0, 0.01)


def register(suite):
    g = "params"
    suite.add(g, "coverage_of_defaults", test_every_default_is_covered,
              doc="every leaf key in DEFAULTS is exercised by some test")
    suite.add(g, "tonemap_choices_all_render", test_every_advertised_tonemap_renders,
              doc="every --tonemap the CLI offers must build on both working spaces")
    suite.add(g, "tone_ends_leave_mids_alone",
              test_tone_ends_spline_leaves_mid_tones_alone,
              doc="black_lift and highlight_rolloff must not move the median")
    for case in CASES:
        suite.add(g, case.name, _make_test(case), xfail=case.xfail, doc=case.doc)
