"""Group: film grain (C3): stock, softness, response, color, seed.

Contract: every field this arc added to grain (stock, softness, response,
color, seed) must default to today's exact bytes. That is checked twice:
`test_defaults_pin_the_pre_c3_graph_text` pins the literal filter graph text
build_graph and grain_input emit when every new field sits at its default,
and the pre-existing `-g golden` fingerprints (unchanged by this arc) prove
the same thing end to end on real footage and real presets.

Everything else here measures what a field actually does, on a rendered
frame, rather than trusting the code comment that explains it:

  softness   blurs the plate before the blend, so the grain EFFECT's own
             high-frequency content should fall even though the picture
             still visibly has grain on it.
  response   "film" scales the plate's deviation from mid grey by a weight
             that eases from 1 in the shadows/mid tones down to 0.25 by full
             white, so the same plate should leave visibly less amplitude on
             a bright patch than on a dark one.
  color      mixes in a second, independently random plate per channel, so a
             flat patch's R, G and B should diverge at color>0 where they
             never do at color=0 (grain is built on a gray/luma-only source).
  seed       reseeds ffmpeg's noise filter (`all_seed`, the option that
             actually has an effect on this build; `c0_seed` measured to do
             nothing), so a different seed must change the plate's bytes
             while leaving its own mean and spread inside a small tolerance.
  stock      replaces size/strength/softness outright, not as a starting
             point sliders then nudge.

All of this is measured on synthetic solid-colour patches pushed through the
real graph_with_mask/ffmpeg_inputs builders (the same ones the CLI drives),
read back as a whole block rather than a single centre pixel, because grain's
own high-frequency texture and per-channel spread need a neighbourhood of
pixels to be measurable at all.
"""

from __future__ import annotations

import numpy as np

import harness as H
from harness import cg

CLIP = H.CLIP_A


def _luma01(block: np.ndarray) -> np.ndarray:
    """Rec.709 luma on a (h, w, 3) array already in 0..1, not 0..255."""
    return 0.2126 * block[..., 0] + 0.7152 * block[..., 1] + 0.0722 * block[..., 2]


def _render_block(cfg: dict, patches: dict, swatch: int = 48) -> dict:
    """Full-frame readback of solid patches, not just the centre pixel.

    harness.render_patches drives the same builders but keeps only the
    centre sample of each swatch, which is enough for a colour anchor and not
    enough for grain: measuring the effect's own high-frequency energy or a
    per-channel spread needs a whole neighbourhood of pixels, not one.
    """
    png, names = H.make_patch_source(patches, swatch)
    info = H.patch_info(png, len(names), swatch)
    graph = cg.graph_with_mask(cfg, info, tail_extra=["format=rgb48le"],
                               encode_out=False)
    args = cg.ffmpeg_inputs(str(png), cfg, info)
    args += ["-filter_complex", graph, "-map", "[vout]", "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"]
    raw = H._run_bytes(args)
    out = np.frombuffer(raw, dtype="<u2").reshape(
        info["height"], info["width"], 3).astype(np.float64) / 65535.0
    return {n: out[:, i * swatch:(i + 1) * swatch, :] for i, n in enumerate(names)}


# --------------------------------------------------------------------------
# byte identity: the compatibility gate
# --------------------------------------------------------------------------

def test_defaults_pin_the_pre_c3_graph_text(ctx):
    """stock=custom, softness=0, response=flat, color=0, seed=0: build_graph
    and grain_input must emit the exact two lines that shipped before any of
    these fields existed, character for character.
    """
    cfg = H.patch(H.defaults(), {"grain": {"enabled": True}})
    info = H.info_for(CLIP)
    idx = cg.mask_input_indices(cfg)["grain"]

    args = cg.grain_input(cfg, info)
    ctx.note(f"grain_input: {args[-1]}")
    ctx.expect_eq("grain_input emits exactly one -i pair (no colour plate)",
                  len(args), 4)
    ctx.expect_true("the plate source carries no seed option",
                    "all_seed" not in args[-1], args[-1])
    ctx.expect_true("the noise filter args are the pre-C3 string",
                    args[-1].endswith("noise=c0s=40:c0f=t+u"), args[-1])

    graph, _ = cg.build_graph(cfg, info, encode_out=False)
    want_plate = (f"[{idx}:v]scale={info['width']}:{info['height']}"
                  f":flags=bilinear,format=gbrp16le,setsar=1[grainplate]")
    want_blend = "[grainplate]blend=all_mode=overlay:shortest=1:all_opacity=0.5000[gx]"
    ctx.note(f"plate segment: {want_plate}")
    ctx.expect_true("the plate segment is the pre-C3 line, unchanged",
                    want_plate in graph, graph)
    ctx.expect_true("the final blend is the pre-C3 overlay line, unchanged",
                    want_blend in graph, graph)


# --------------------------------------------------------------------------
# softness
# --------------------------------------------------------------------------

def test_softness_lowers_the_plates_own_high_frequency_energy(ctx):
    """softness blurs the plate before it is blended in. Measured as the
    high-frequency energy of the effect alone (graded minus grain-off, the
    same isolation cases_params._effect_hf uses), not of the whole picture.
    """
    swatch = 64
    patches = {"mid": [0.4, 0.4, 0.4]}
    off = _render_block(H.patch(H.defaults(), {"grain": {"enabled": False}}),
                        patches, swatch)["mid"]

    hfs = {}
    for soft in (0.0, 0.6, 1.2):
        cfg = H.patch(H.defaults(), {"grain": {
            "enabled": True, "strength": 60, "softness": soft}})
        on = _render_block(cfg, patches, swatch)["mid"]
        d = on - off
        hfs[soft] = float(np.abs(d - H.box_blur(d, 3)).mean())
    ctx.note(f"grain effect HF by softness: {hfs}")
    ctx.expect_gt("softness 0.6 has less effect-HF than softness 0.0",
                  hfs[0.0], hfs[0.6])
    ctx.expect_gt("softness 1.2 has less effect-HF than softness 0.6",
                  hfs[0.6], hfs[1.2])


# --------------------------------------------------------------------------
# response
# --------------------------------------------------------------------------

def test_film_response_is_weaker_in_the_highlights(ctx):
    """response=film's weight is 1 in the shadows/mid tones and eases to
    0.25 by full white, so the same grain plate must leave less amplitude on
    a bright patch than on a dark one; response=flat should treat both alike.
    """
    # Input code values, not post-pipeline luma: the working-space and curve
    # stages are not the identity, and 0.05/0.95 measured fully black/at-1.0
    # post-pipeline (crushed by the toe/shoulder of the default curve), which
    # leaves grain no room to move either direction and a zero standard
    # deviation on both. 0.25/0.80 measured post-pipeline luma 0.068 and
    # 0.921 (see cases_grain's own probe): dark but not black, bright but not
    # clipped, so grain can still move the pixel down as well as up.
    swatch = 64
    patches = {"dark": [0.25, 0.25, 0.25], "bright": [0.80, 0.80, 0.80]}
    off = _render_block(H.patch(H.defaults(), {"grain": {"enabled": False}}),
                        patches, swatch)
    ctx.note(f"post-pipeline luma with grain off: dark "
             f"{float(_luma01(off['dark']).mean()):.4f}, bright "
             f"{float(_luma01(off['bright']).mean()):.4f} (must separate for "
             f"this test to mean anything)")
    ctx.expect_gt("the two patches really are dark and bright post-pipeline",
                  float(_luma01(off["bright"]).mean()),
                  float(_luma01(off["dark"]).mean()) + 0.3)

    def amplitude(response):
        cfg = H.patch(H.defaults(), {"grain": {
            "enabled": True, "strength": 60, "response": response}})
        on = _render_block(cfg, patches, swatch)
        return {k: float((on[k] - off[k]).std()) for k in patches}

    flat = amplitude("flat")
    film = amplitude("film")
    ratio_flat = flat["bright"] / flat["dark"]
    ratio_film = film["bright"] / film["dark"]
    ctx.note(f"flat: dark {flat['dark']:.5f} bright {flat['bright']:.5f} "
             f"ratio {ratio_flat:.4f}")
    ctx.note(f"film: dark {film['dark']:.5f} bright {film['bright']:.5f} "
             f"ratio {ratio_film:.4f}")
    ctx.expect_between("flat response leaves the dark/bright ratio near 1",
                        ratio_flat, 0.85, 1.18)
    ctx.expect_lt("film response's bright/dark ratio is well below flat's",
                  ratio_film, ratio_flat - 0.15)
    ctx.expect_between("film response's bright/dark ratio lands near the "
                       "documented 0.25 (W(1)/W(0))", ratio_film, 0.15, 0.45)


# --------------------------------------------------------------------------
# color
# --------------------------------------------------------------------------

def test_color_makes_channels_differ(ctx):
    """color=0 (default) keeps grain monochrome: the plate is built on a
    gray/luma-only source, so R, G and B move together on a flat patch.
    color=1 mixes in an independently random plate per channel, so the same
    flat patch should come back with R, G and B visibly different from one
    another where they never were at color=0.
    """
    swatch = 64
    patches = {"mid": [0.4, 0.4, 0.4]}

    def channel_spread(color):
        cfg = H.patch(H.defaults(), {"grain": {
            "enabled": True, "strength": 60, "color": color}})
        block = _render_block(cfg, patches, swatch)["mid"]
        return float((block.max(axis=-1) - block.min(axis=-1)).mean())

    mono = channel_spread(0.0)
    full = channel_spread(1.0)
    ctx.note(f"mean per-pixel R/G/B spread: color=0 {mono:.6f}, "
             f"color=1 {full:.6f}")
    ctx.expect_close("color=0 keeps R, G and B locked together (monochrome)",
                     mono, 0.0, 1e-6)
    ctx.expect_gt("color=1 makes R, G and B differ where they were equal",
                  full, mono)


# --------------------------------------------------------------------------
# seed
# --------------------------------------------------------------------------

def test_seed_changes_the_plate_not_its_statistics(ctx):
    """A different seed is a different random draw (measured to change the
    plate's bytes), not a different amplitude parameter, so the plate's own
    mean and standard deviation must stay within a small, documented
    tolerance across seeds even though the bytes themselves move.
    """
    swatch = 64
    patches = {"mid": [0.4, 0.4, 0.4]}
    seeds = (0, 1, 999999)
    blocks = {}
    for s in seeds:
        cfg = H.patch(H.defaults(), {"grain": {
            "enabled": True, "strength": 60, "seed": s}})
        blocks[s] = _render_block(cfg, patches, swatch)["mid"]

    d01 = float(np.abs(blocks[0] - blocks[1]).mean())
    ctx.note(f"mean |seed0 - seed1| = {d01:.6f} (0 would mean seed had no "
             f"effect at all)")
    ctx.expect_gt("a different seed changes the plate's bytes", d01, 0.0005)

    means = {s: float(blocks[s].mean()) for s in seeds}
    stds = {s: float(blocks[s].std()) for s in seeds}
    ctx.note(f"means by seed: {means}")
    ctx.note(f"stds by seed: {stds}")
    base_mean, base_std = means[0], stds[0]
    for s in seeds[1:]:
        ctx.expect_close(
            f"seed {s} keeps the block mean within tolerance of seed 0",
            means[s], base_mean, 0.01)
        ctx.expect_close(
            f"seed {s} keeps the block std within tolerance of seed 0",
            stds[s], base_std, 0.01)


# --------------------------------------------------------------------------
# stock
# --------------------------------------------------------------------------

def test_stock_overrides_raw_fields(ctx):
    """A named stock replaces size/strength/softness outright. Proved by
    setting the raw fields to values that deliberately do NOT match the
    stock's own numbers: the render must still come back exactly like the
    stock alone (or an explicit "custom" config carrying the stock's own
    numbers), because the panel's tooltip promises three fixed numbers, not
    a starting point the sliders then nudge.
    """
    swatch = 64
    patches = {"mid": [0.4, 0.4, 0.4]}
    stocked = H.patch(H.defaults(), {"grain": {
        "enabled": True, "stock": "35mm",
        "size": 1, "strength": 5, "softness": 1.5}})
    plain = H.patch(H.defaults(), {"grain": {
        "enabled": True, "stock": "custom",
        "size": 3, "strength": 40, "softness": 0.0}})
    a = _render_block(stocked, patches, swatch)["mid"]
    b = _render_block(plain, patches, swatch)["mid"]
    d = float(np.abs(a - b).max())
    ctx.note(f"stock=35mm with deliberately wrong raw fields vs a plain "
             f"config carrying 35mm's own numbers: max |delta| {d:.6f}")
    ctx.expect_close("stock=35mm ignores the raw fields entirely", d, 0.0, 1e-9)


# --------------------------------------------------------------------------
# determinism (brief step 3): three separate ffmpeg processes, one config
# --------------------------------------------------------------------------

def test_plate_is_deterministic_across_processes(ctx):
    """Softness, color and a non-zero seed all engaged at once, rendered as
    three genuinely separate ffmpeg invocations (not memoised): the plate
    must come back byte for byte every time. A render that is not
    reproducible run to run cannot be tested, or reviewed, at all.
    """
    swatch = 48
    patches = {"mid": [0.4, 0.4, 0.4]}
    cfg = H.patch(H.defaults(), {"grain": {
        "enabled": True, "strength": 50, "softness": 0.5,
        "color": 0.6, "seed": 42}})
    runs = [_render_block(cfg, patches, swatch)["mid"] for _ in range(3)]
    d01 = float(np.abs(runs[0] - runs[1]).max())
    d02 = float(np.abs(runs[0] - runs[2]).max())
    ctx.note(f"max |run0 - run1| {d01:.8f}, max |run0 - run2| {d02:.8f}")
    ctx.expect_eq("three separate ffmpeg processes agree on every byte (run 1)",
                  d01, 0.0)
    ctx.expect_eq("three separate ffmpeg processes agree on every byte (run 2)",
                  d02, 0.0)


def register(suite):
    g = "grain"
    suite.add(g, "defaults_pin_the_pre_c3_graph_text",
              test_defaults_pin_the_pre_c3_graph_text,
              doc="every new grain field at its default reproduces the exact "
                  "pre-C3 filter graph text, byte for byte")
    suite.add(g, "softness_lowers_the_plates_own_high_frequency_energy",
              test_softness_lowers_the_plates_own_high_frequency_energy,
              doc="softness blurs the plate before the blend, lowering the "
                  "grain effect's own high-frequency energy")
    suite.add(g, "film_response_is_weaker_in_the_highlights",
              test_film_response_is_weaker_in_the_highlights,
              doc="response=film leaves less grain amplitude on a bright "
                  "patch than on a dark one; response=flat treats them alike")
    suite.add(g, "color_makes_channels_differ",
              test_color_makes_channels_differ,
              doc="color>0 mixes in an independent plate per channel, so R, "
                  "G and B diverge on a flat patch that color=0 keeps locked")
    suite.add(g, "seed_changes_the_plate_not_its_statistics",
              test_seed_changes_the_plate_not_its_statistics,
              doc="a different seed changes the plate's bytes but leaves its "
                  "mean and spread within a documented tolerance")
    suite.add(g, "stock_overrides_raw_fields",
              test_stock_overrides_raw_fields,
              doc="a named stock replaces size/strength/softness outright, "
                  "even against deliberately different raw field values")
    suite.add(g, "plate_is_deterministic_across_processes",
              test_plate_is_deterministic_across_processes,
              doc="the same grain config renders byte-identical plates "
                  "across three separate ffmpeg processes, softness/color/"
                  "seed all engaged at once")
