"""Group: midtone detail and noise reduction (C4): detail.mid_detail and
prep.denoise.

Contract: both fields must default to today's exact bytes. That is checked
directly here (a config that carries the new keys at their defaults renders
the identical graph text and the identical pixels as a config that has never
heard of them) and again end to end by the pre-existing `-g golden`
fingerprints, which this arc leaves unchanged.

Everything else here measures what a field actually does, on a rendered
frame or a small synthetic one, rather than trusting the docstring that
explains it:

  mid_detail   a wide gaussian local-contrast push. Direction is shown two
               ways: the real footage sweep in cases_params.py (hf energy on
               the whole frame), and a synthetic soft edge built here, where
               the transition width and the flanking flat levels are known
               exactly, so "sharper" and "softer" are unambiguous.
  denoise      hqdn3d, spatial and temporal. Spatial is shown to lower the
               spread of a flat patch of real footage. Temporal needs real
               motion: on a single still it is measured close to a no-op
               (not exactly one, see grade/cinegrade.py f_denoise's
               docstring and the Limits panel for the real numbers), and the
               same comparison on an actual multi-frame render moves by
               several times as much.
"""

from __future__ import annotations

from copy import deepcopy

import numpy as np

import harness as H
from harness import cg

CLIP = H.CLIP_A
T = H.TIME_A


def _drop_c4(cfg):
    """The same config with mid_detail and prep.denoise written nowhere."""
    out = deepcopy(cfg)
    detail = out.get("detail")
    if isinstance(detail, dict):
        detail.pop("mid_detail", None)
    out.pop("prep", None)
    return out


# --------------------------------------------------------------------------
# (a) the stage is invisible at its defaults
# --------------------------------------------------------------------------

def test_defaults_are_byte_identical(ctx):
    """A config carrying mid_detail=0.0 and prep.denoise at its defaults must
    render exactly what a config that has never heard of either key renders.

    Three things are compared, following the same pattern the window group
    uses: the filter graph text, the ffmpeg input list, and the rendered
    pixels, across a handful of configs that also exercise soften, sharpen,
    grain and a layer, so the new branch in build_graph is proven inert
    however the rest of the graph is shaped.
    """
    info = H.info_for(CLIP)
    combos = []
    for soften in (0.0, 1.2):
        for sharpen in (0.0, 0.8):
            for grain in (False, True):
                c = H.patch(H.defaults(), {
                    "detail": {"soften": soften, "sharpen": sharpen},
                    "grain": {"enabled": grain}})
                combos.append((f"soften={soften} sharpen={sharpen} grain={grain}", c))

    graph_diffs, input_diffs = [], []
    for tag, cfg in combos:
        without = _drop_c4(cfg)
        if cg.graph_with_mask(cfg, info) != cg.graph_with_mask(without, info):
            graph_diffs.append(tag)
        if (cg.ffmpeg_inputs(str(CLIP), cfg, info)
                != cg.ffmpeg_inputs(str(CLIP), without, info)):
            input_diffs.append(tag)
    ctx.note(f"{len(combos)} configs compared with and without the new keys")
    ctx.expect_true("the graph text is unchanged by mid_detail=0 / denoise defaults",
                    not graph_diffs, f"differed: {graph_diffs}" if graph_diffs else "")
    ctx.expect_true("the ffmpeg input list is unchanged too",
                    not input_diffs, f"differed: {input_diffs}" if input_diffs else "")

    # And the pixels, not just the text that is meant to produce them, on the
    # plain default config.
    base = H.defaults()
    a = H.render(CLIP, base, T)
    b = H.render(CLIP, _drop_c4(base), T)
    d = H.mean_abs_diff(a, b)
    ctx.note(f"defaults vs no-C4-keys, real render: mean |delta| {d:.6f} of 255")
    ctx.expect_close("the rendered bytes are identical, not merely close", d, 0.0, 0.0)


# --------------------------------------------------------------------------
# (b) mid_detail's direction, on a synthetic soft edge
# --------------------------------------------------------------------------

def _soft_edge_source(width=200, height=8, transition=8, low=0.30, high=0.70):
    """A 16-bit PNG of a single smooth transition: flat low, a raised-cosine
    ramp `transition` pixels wide centred on the frame, flat high. Every row
    is identical, so the picture is really one 1D profile.

    Values are Apple Log code values, the same convention
    harness.make_patch_source uses for its swatches, so the default
    convert.working_space="dwg" decodes them exactly like any other clip
    instead of needing a special-cased config for this one test.
    """
    x = np.arange(width, dtype=np.float64)
    centre = width / 2.0
    t = np.clip((x - centre) / float(transition) + 0.5, 0.0, 1.0)
    ramp = low + (high - low) * (0.5 - 0.5 * np.cos(np.pi * t))
    row = np.stack([ramp, ramp, ramp], axis=-1)
    arr = np.tile(row, (height, 1, 1))
    u16 = np.round(np.clip(arr, 0.0, 1.0) * 65535).astype("<u2")
    H.WORK.mkdir(parents=True, exist_ok=True)
    raw_path = H.WORK / "detail_soft_edge.raw"
    png_path = H.WORK / "detail_soft_edge.png"
    raw_path.write_bytes(u16.tobytes())
    H._run_bytes(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo",
                  "-pix_fmt", "rgb48le", "-s", f"{width}x{height}",
                  "-i", str(raw_path), "-frames:v", "1", "-pix_fmt", "rgb48be",
                  str(png_path)])
    return png_path, width, height


def _soft_edge_info(width, height):
    return {"width": width, "height": height, "rotation": 0,
            "autorotate": True, "pix_fmt": "rgb48be", "color_range": "full",
            "color_space": "bt2020nc", "nb_frames": "1", "duration": "1",
            "codec": "png", "profile": None}


def _render_soft_edge(cfg, png, width, height):
    info = _soft_edge_info(width, height)
    graph = cg.graph_with_mask(cfg, info, tail_extra=["format=rgb48le"],
                               encode_out=False)
    args = cg.ffmpeg_inputs(str(png), cfg, info)
    args += ["-filter_complex", graph, "-map", "[vout]", "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"]
    raw = H._run_bytes(args)
    return np.frombuffer(raw, dtype="<u2").reshape(height, width, 3).astype(np.float64) / 65535.0


def test_mid_detail_direction_on_synthetic_soft_edge(ctx):
    """Positive mid_detail must sharpen the transition, negative must soften
    it, on an edge whose shape is known exactly rather than inferred from
    real footage.

    hf_energy (mean distance from a 3x3 box blur) is the same metric the
    real-footage sweep in cases_params.py uses for soften and sharpen: a
    sharper transition has more high-frequency content, a softer one less.
    """
    png, w, h = _soft_edge_source()
    base = H.patch(H.defaults(), {"convert": {"working_space": "dwg"}})

    img_neg = _render_soft_edge(H.patch(base, {"detail": {"mid_detail": -0.8}}), png, w, h)
    img_flat = _render_soft_edge(H.patch(base, {"detail": {"mid_detail": 0.0}}), png, w, h)
    img_pos = _render_soft_edge(H.patch(base, {"detail": {"mid_detail": 0.8}}), png, w, h)

    hf_neg = H.hf_energy(img_neg)
    hf_flat = H.hf_energy(img_flat)
    hf_pos = H.hf_energy(img_pos)
    ctx.note(f"synthetic soft edge, hf energy: mid_detail -0.8 -> {hf_neg:.6f}, "
             f"0.0 -> {hf_flat:.6f}, +0.8 -> {hf_pos:.6f}")
    ctx.expect_gt("positive mid_detail raises hf energy above the flat baseline",
                  hf_pos, hf_flat)
    ctx.expect_lt("negative mid_detail lowers hf energy below the flat baseline",
                  hf_neg, hf_flat)

    # The profile itself, not just its energy: the transition's own span
    # (a fixed window bracketing the ramp) should show a bigger swing at
    # positive mid_detail and a smaller one at negative, on the identical
    # row every row of this source repeats.
    mid = h // 2
    lo_x, hi_x = int(w / 2 - 2 * 8), int(w / 2 + 2 * 8)
    swing_neg = float(img_neg[mid, hi_x, 0] - img_neg[mid, lo_x, 0])
    swing_flat = float(img_flat[mid, hi_x, 0] - img_flat[mid, lo_x, 0])
    swing_pos = float(img_pos[mid, hi_x, 0] - img_pos[mid, lo_x, 0])
    ctx.note(f"swing across the transition window: -0.8 -> {swing_neg:.4f}, "
             f"0.0 -> {swing_flat:.4f}, +0.8 -> {swing_pos:.4f}")
    ctx.expect_gt("the swing across the edge is bigger at +0.8 than at 0",
                  swing_pos, swing_flat)
    ctx.expect_lt("and smaller at -0.8 than at 0", swing_neg, swing_flat)


# --------------------------------------------------------------------------
# (c) spatial denoise, on a flat patch of real footage
# --------------------------------------------------------------------------

def _flattest_patch(img, size=16):
    """Coordinates of the lowest-variance size x size block on a grid,
    skipping near-black and near-white blocks where clipping would flatten
    the statistic for a reason that has nothing to do with denoising.
    """
    h, w, _ = img.shape
    best = None
    f = img.astype(np.float64)
    for yy in range(size, h - size, size):
        for xx in range(size, w - size, size):
            block = f[yy:yy + size, xx:xx + size, :]
            m = block.mean()
            if m < 20 or m > 235:
                continue
            sd = float(block.std())
            if best is None or sd < best[2]:
                best = (yy, xx, sd)
    return best


def test_spatial_denoise_lowers_a_flat_patch_std(ctx):
    """Spatial denoise on a flat patch of real footage must lower its spread.

    The patch location is found on the rendered frame itself rather than
    hardcoded, so the test does not depend on which width the suite renders
    at.
    """
    base = H.defaults()
    plain = H.render(CLIP, base, T)
    found = _flattest_patch(plain)
    ctx.expect_true("a usable flat patch exists in this frame", found is not None)
    if found is None:
        return
    y, x, sd0 = found
    size = 16

    denoised_cfg = H.patch(base, {"prep": {"denoise": {
        "enabled": True, "spatial": 0.5, "temporal": 0.0}}})
    denoised = H.render(CLIP, denoised_cfg, T)

    sd1 = float(denoised[y:y + size, x:x + size, :].astype(np.float64).std())
    ctx.note(f"flat {size}x{size} patch at (y={y}, x={x}): std dev "
             f"{sd0:.3f} -> {sd1:.3f} of 255 with spatial denoise 0.5")
    ctx.expect_lt("spatial denoise lowers the patch's std dev", sd1, sd0)

    changed = H.changed_fraction(plain, denoised, thresh=0)
    ctx.expect_gt("and it does change the rendered frame", changed, 0.0)


# --------------------------------------------------------------------------
# (d) temporal denoise needs motion
# --------------------------------------------------------------------------

def _render_frames(cfg, n, width=H.WIDTH, t0=T):
    info = H.info_for(CLIP, width)
    head = f"[0:v]scale={info['width']}:{info['height']}:flags=bilinear[src]"
    graph = cg.graph_with_mask(cfg, info, tail_extra=["format=rgb24"],
                               encode_out=False, src_label="src", head_extra=head)
    args = cg.ffmpeg_inputs(str(CLIP), cfg, info, seek=t0)
    args += ["-filter_complex", graph, "-map", "[vout]", "-frames:v", str(n),
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    raw = H._run_bytes(args)
    frame_bytes = info["width"] * info["height"] * 3
    frames = []
    for i in range(n):
        chunk = raw[i * frame_bytes:(i + 1) * frame_bytes]
        if len(chunk) < frame_bytes:
            break
        frames.append(np.frombuffer(chunk, dtype=np.uint8)
                       .reshape(info["height"], info["width"], 3))
    return frames


def test_temporal_denoise_needs_motion(ctx):
    """Temporal denoise on a single still is close to a no-op, not exactly
    one, and the same comparison on an actual multi-frame render moves the
    picture by several times as much.

    The plan this arc started from assumed an exact no-op on a still. That
    is not what hqdn3d does on its own first frame (confirmed with a bare
    ffmpeg run outside this engine too), so this asserts a bound instead of
    exact zero, and pins the real effect elsewhere so a regression that
    makes temporal denoise silently do nothing on real footage is caught.
    """
    base = H.defaults()
    t0_cfg = H.patch(base, {"prep": {"denoise": {
        "enabled": True, "spatial": 0.0, "temporal": 0.0}}})
    t1_cfg = H.patch(base, {"prep": {"denoise": {
        "enabled": True, "spatial": 0.0, "temporal": 1.0}}})

    still_t0 = H.render(CLIP, t0_cfg, T)
    still_t1 = H.render(CLIP, t1_cfg, T)
    still_mean = H.mean_abs_diff(still_t0, still_t1)
    still_max = float(np.abs(still_t0.astype(np.int16) - still_t1.astype(np.int16)).max())
    ctx.note(f"temporal 0 vs 1 on a single still: mean |delta| {still_mean:.4f} "
             f"of 255, max {still_max:.1f}")
    ctx.expect_lt("temporal denoise stays close to a no-op on a still (mean)",
                  still_mean, 2.0)
    ctx.expect_lt("...and on a still (max)", still_max, 12.0)

    n = 12
    frames_t0 = _render_frames(t0_cfg, n)
    frames_t1 = _render_frames(t1_cfg, n)
    ctx.expect_true(f"the {n}-frame render produced {n} frames each way",
                    len(frames_t0) == n and len(frames_t1) == n,
                    f"got {len(frames_t0)} / {len(frames_t1)}")
    if len(frames_t0) < 7 or len(frames_t1) < 7:
        return
    idx = 6
    motion_mean = H.mean_abs_diff(frames_t0[idx], frames_t1[idx])
    ctx.note(f"temporal 0 vs 1 on frame {idx} of a {n}-frame render: "
             f"mean |delta| {motion_mean:.4f} of 255 "
             f"(still was {still_mean:.4f})")
    ctx.expect_gt("with real motion to use, temporal denoise moves the frame "
                  "materially more than it did on a still",
                  motion_mean, still_mean * 1.5)


# --------------------------------------------------------------------------
# (e) mid_detail overshoot clamps, it does not wrap
# --------------------------------------------------------------------------

def _hard_edge_source(width=200, height=8, dark=0.25, bright=0.75):
    """A 16-bit PNG of a single hard edge: flat dark left half, flat bright
    right half, no ramp at all. Apple Log code values, same convention as
    _soft_edge_source above.

    A hard edge is the case that makes mid_detail's arithmetic leave the
    valid range: out = A + mid*K*(A - blur(A)), and at a step the wide blur
    sits far below A on the bright side and far above it on the dark side,
    so at mid_detail 1.0 the bright side computes above 65535 and the dark
    side below 0.

    dark is 0.25 rather than something closer to black because the CST
    crushes Apple Log 0.15 and below to code 0, and a side that is already 0
    cannot be seen to undershoot: measured through this same render, dark
    0.15 lands the flat side on 0 and drives 0 samples onto the floor, while
    0.25 lands it on 4461 of 65535 and drives 120.
    """
    x = np.arange(width, dtype=np.float64)
    ramp = np.where(x < width / 2.0, dark, bright)
    row = np.stack([ramp, ramp, ramp], axis=-1)
    arr = np.tile(row, (height, 1, 1))
    u16 = np.round(np.clip(arr, 0.0, 1.0) * 65535).astype("<u2")
    H.WORK.mkdir(parents=True, exist_ok=True)
    raw_path = H.WORK / "detail_hard_edge.raw"
    png_path = H.WORK / "detail_hard_edge.png"
    raw_path.write_bytes(u16.tobytes())
    H._run_bytes(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo",
                  "-pix_fmt", "rgb48le", "-s", f"{width}x{height}",
                  "-i", str(raw_path), "-frames:v", "1", "-pix_fmt", "rgb48be",
                  str(png_path)])
    return png_path, width, height


def _render_codes(cfg, png, width, height):
    """_render_soft_edge's render, kept in 16-bit code values rather than
    normalised floats, because this test is about the exact clamp endpoints
    (0 and 65535) and not about a ratio."""
    info = _soft_edge_info(width, height)
    graph = cg.graph_with_mask(cfg, info, tail_extra=["format=rgb48le"],
                               encode_out=False)
    args = cg.ffmpeg_inputs(str(png), cfg, info)
    args += ["-filter_complex", graph, "-map", "[vout]", "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"]
    raw = H._run_bytes(args)
    return np.frombuffer(raw, dtype="<u2").reshape(height, width, 3).astype(np.int64)


def test_mid_detail_overshoot_clamps_and_does_not_wrap(ctx):
    """At mid_detail 1.0 on a hard bright edge the formula leaves the valid
    range at both ends, and both ends must CLAMP.

    This is the regression guard for the bug that made every positive
    mid_detail parity row FAILED: ffmpeg's blend filter does not clip an out
    of range all_expr result, it casts the float to the plane's integer type,
    so the value wraps modulo 65536 (measured on ffmpeg 8.1.1: an expression
    worth -242 came back as 65294). A wrap turns the bright side of an edge
    black and the dark side white, which is why the GPU port (which clamps)
    and the engine disagreed by the full 255 rather than by a code or two.
    f_mid_detail_segment now wraps the same expression in clip(...,0,65535).
    """
    png, w, h = _hard_edge_source()
    base = H.patch(H.defaults(), {"convert": {"working_space": "dwg"}})
    flat = _render_codes(H.patch(base, {"detail": {"mid_detail": 0.0}}), png, w, h)
    pushed = _render_codes(H.patch(base, {"detail": {"mid_detail": 1.0}}), png, w, h)

    half = w // 2
    dark_level = int(np.median(flat[:, :half // 2, :]))
    bright_level = int(np.median(flat[:, half + half // 2:, :]))
    ctx.note(f"hard edge through the CST: dark side {dark_level} of 65535, "
             f"bright side {bright_level} of 65535")

    # The overshoot is real, not hypothetical: samples that were below the
    # ceiling before mid_detail sit exactly on it after, and samples that
    # were above the floor sit exactly on it.
    hit_top = int(np.count_nonzero((pushed == 65535) & (flat < 65535)))
    hit_bottom = int(np.count_nonzero((pushed == 0) & (flat > 0)))
    ctx.note(f"mid_detail 1.0 drove {hit_top} samples onto the 65535 ceiling "
             f"and {hit_bottom} onto the 0 floor")
    ctx.expect_gt("the formula really does overshoot the top of the range here",
                  hit_top, 0)
    ctx.expect_gt("...and undershoot the bottom", hit_bottom, 0)

    # No wrap. A wrapped overshoot lands at the OTHER end of the range: the
    # bright half would go black and the dark half white.
    bright_min = int(pushed[:, half:, :].min())
    dark_max = int(pushed[:, :half, :].max())
    ctx.note(f"after the push: bright half minimum {bright_min}, dark half "
             f"maximum {dark_max}, whole frame max {int(pushed.max())}")
    ctx.expect_eq("the brightest samples are the ceiling itself, 65535",
                  int(pushed.max()), 65535)
    ctx.expect_ge("no sample on the bright side of the edge fell below the "
                  "dark side's own level (that is what a wrap looks like)",
                  bright_min, dark_level)
    ctx.expect_le("and no sample on the dark side rose above the bright "
                  "side's level either", dark_max, bright_level)


def register(suite):
    g = "detail"
    suite.add(g, "defaults_are_byte_identical", test_defaults_are_byte_identical,
              doc="mid_detail=0 and prep.denoise at their defaults reproduce today's bytes")
    suite.add(g, "mid_detail_direction_on_synthetic_soft_edge",
              test_mid_detail_direction_on_synthetic_soft_edge,
              doc="positive mid_detail sharpens a known edge, negative softens it")
    suite.add(g, "mid_detail_overshoot_clamps_and_does_not_wrap",
              test_mid_detail_overshoot_clamps_and_does_not_wrap,
              doc="mid_detail 1.0 on a hard edge clamps at 0 and 65535 rather "
                  "than wrapping modulo 65536 the way ffmpeg's blend does")
    suite.add(g, "spatial_denoise_lowers_a_flat_patch_std",
              test_spatial_denoise_lowers_a_flat_patch_std,
              doc="spatial denoise lowers the spread of a flat patch of real footage")
    suite.add(g, "temporal_denoise_needs_motion", test_temporal_denoise_needs_motion,
              doc="near no-op on a still, several times bigger once there is real motion")
