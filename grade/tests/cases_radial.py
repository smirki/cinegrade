"""Group 9: the radial blur matte.

The radial blur is a gblur branch merged back over the sharp one through an
8-bit greyscale ramp. Two separate hops sit between "the number the expression
asked for" and "the weight maskedmerge actually applies", and both of them were
silently wrong until 2026-09-04:

  bake        radial_mask ran its geq on the luma plane of a limited range YUV
              source and converted to gray afterwards, so every code came back
              as round((v-16)*255/219) clipped. Measured on a 256 wide identity
              ramp: 248 of 256 codes moved, by up to 20 code values, and only
              220 distinct codes survived. Everything at or below 16 flattened
              to 0 and everything at or above 235 flattened to 255.
  widen       graph_with_mask attached the 8-bit PNG with a bare
              format=gbrp16le, which widens by a left shift of 8. Code 255
              arrived at maskedmerge as 65280 of 65535, so a fully open ramp
              applied 99.61% of the blur and never all of it.

Neither is visible in a picture, which is exactly why they shipped. Both are
now pinned by arithmetic here: the bake must land the code the formula asked
for, and the widen must be a multiply by exactly 257 so that code 255 is 65535.

The engine's quantisation of the ramp itself is NOT a defect and is not being
removed: the matte is a PNG, so it is 8 bit, and ffmpeg's geq truncates, so the
reference is floor(255 * ramp). The GPU port emulates that one step and nothing
else. Measured on a 640x360 ramp: floor is exact on all 230400 pixels, while
round-half-up and rint each miss 86776 and 86770 of them by one code.
"""

from __future__ import annotations

import numpy as np

import harness as H
from harness import cg

CLIP = H.CLIP_A

# Every 8-bit code, once, as a picture. Anything the format hop does to a code
# shows up here whatever the frame it came from.
RAMP_W = 256


def _identity_ramp_png():
    """A 256x1 gray PNG holding codes 0..255 in order.

    Built from raw bytes written here rather than from a geq, so that a
    regression in the bake cannot also move the input the widen test measures.
    """
    path = H.WORK / "radial_identity_ramp.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        raw = path.with_suffix(".gray")
        raw.write_bytes(bytes(range(RAMP_W)))
        H._run_bytes(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo",
                      "-pix_fmt", "gray", "-s", f"{RAMP_W}x1", "-i", str(raw),
                      "-frames:v", "1", str(path)])
        back = _read_gray_png(path, RAMP_W, 1)[0]
        if not (back == np.arange(RAMP_W)).all():
            raise AssertionError("the identity ramp PNG did not survive its own "
                                 "encode, so nothing downstream can be trusted")
    return path


def _read_gray_png(path, w, h) -> np.ndarray:
    raw = H._run_bytes(["ffmpeg", "-v", "error", "-i", str(path),
                        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"])
    return np.frombuffer(raw[:w * h], np.uint8).reshape(h, w).astype(int)


def _ramp_reference(w, h, start, end) -> np.ndarray:
    """radial_mask's formula in numpy, quantised the way geq quantises."""
    X, Y = np.meshgrid(np.arange(w), np.arange(h))
    d = np.hypot((X - w / 2) / (w / 2), (Y - h / 2) / (h / 2))
    r = np.clip((d - start) / max(1e-3, end - start), 0.0, 1.0)
    return np.floor(r * 255.0).astype(int)


# --------------------------------------------------------------------------
# (a) the matte arrives at 257n
# --------------------------------------------------------------------------

def test_matte_widens_by_257_not_by_a_shift(ctx):
    """Every 8-bit code must arrive at maskedmerge as exactly 257 times itself.

    257 is the only multiplier that maps 255 onto 65535, and 255 is the code
    that means "use the blurred pixel and none of the sharp one". A shift of 8
    maps it to 65280 instead, which is not an approximation of the right answer
    so much as a different answer: the branch the matte fully selects never
    fully arrives.
    """
    png = _identity_ramp_png()
    raw = H._run_bytes(["ffmpeg", "-v", "error", "-i", str(png), "-frames:v", "1",
                        "-vf", cg.WINDOW_MASK_FORMAT,
                        "-f", "rawvideo", "-pix_fmt", "gbrp16le", "-"])
    got = np.frombuffer(raw, dtype="<u2")[:RAMP_W].astype(int)
    want = np.arange(RAMP_W) * 257

    ctx.note(f"hop under test: {cg.WINDOW_MASK_FORMAT}")
    ctx.note(f"code 0 -> {got[0]}, 128 -> {got[128]}, 235 -> {got[235]}, "
             f"255 -> {got[255]}")
    ctx.expect_eq("max |arrived - 257n| over all 256 codes",
                  int(np.abs(got - want).max()), 0)
    ctx.expect_eq("a fully open matte arrives as the full 16-bit range",
                  int(got[255]), 65535)
    ctx.expect_eq("a fully closed matte arrives as zero", int(got[0]), 0)
    # The failure this replaced, spelled out so a reader can see why the plain
    # hop is not good enough rather than having to take it on faith.
    shift = np.arange(RAMP_W) * 256
    ctx.note(f"a bare left shift would have put 255 at {int(shift[255])}, "
             f"{100.0 * shift[255] / 65535.0:.2f}% of the correction")


def test_maskedmerge_at_255_takes_the_whole_overlay(ctx):
    """The arithmetic end to end, through the real filter.

    A base pinned at 16-bit 0 and an overlay pinned at 16-bit 65535 make the
    merge weight readable directly as a pixel value: whatever maskedmerge does
    with the matte is what comes out. Both constants are written with a geq on
    a gray plane and widened through the same hop, because lavfi's own
    color=c=white lands at 65283 in gbrp16le, not at 65535, and a reference
    that is itself 252 codes short cannot measure a 255 code shortfall.
    """
    png = _identity_ramp_png()
    hop = cg.WINDOW_MASK_FORMAT
    graph = (f"[0:v]format=gray,geq=lum='0',{hop},setsar=1[a];"
             f"[1:v]format=gray,geq=lum='255',{hop},setsar=1[b];"
             f"[2:v]{hop},setsar=1[m];"
             f"[a][b][m]maskedmerge[o]")
    raw = H._run_bytes([
        "ffmpeg", "-v", "error",
        "-f", "lavfi", "-i", f"color=c=black:s={RAMP_W}x1:d=1",
        "-f", "lavfi", "-i", f"color=c=black:s={RAMP_W}x1:d=1",
        "-i", str(png),
        "-filter_complex", graph, "-map", "[o]", "-frames:v", "1",
        "-f", "rawvideo", "-pix_fmt", "gbrp16le", "-"])
    got = np.frombuffer(raw, dtype="<u2")[:RAMP_W].astype(int)

    ctx.note(f"merged 0 under 65535: code 0 -> {got[0]}, 128 -> {got[128]}, "
             f"254 -> {got[254]}, 255 -> {got[255]} of 65535")
    ctx.expect_eq("matte 255 leaves nothing of the base behind",
                  int(got[255]), 65535)
    ctx.expect_eq("matte 0 leaves the base untouched", int(got[0]), 0)
    ctx.expect_eq("the merged weight is 257n end to end",
                  int(np.abs(got - np.arange(RAMP_W) * 257).max()), 0)
    ctx.expect_true("the merge weight is monotonic in the code",
                    bool((np.diff(got) > 0).all()),
                    f"{int((np.diff(got) <= 0).sum())} codes did not increase")


# --------------------------------------------------------------------------
# (b) the bake lands the code the formula asked for
# --------------------------------------------------------------------------

def test_baked_ramp_has_no_range_expansion(ctx):
    """The PNG on disk must hold floor(255 * ramp), not a tv-to-full stretch of
    it. Several sizes and spans, because the expansion was a property of the
    pixel format and not of the geometry, so any one of them would have caught
    it and all of them should stay clean."""
    combos = [(640, 360, 0.20, 1.00), (320, 568, 0.55, 1.00),
              (256, 256, 0.00, 1.00), (480, 270, 0.66, 1.05)]
    worst = 0
    for w, h, start, end in combos:
        path = cg.radial_mask(w, h, start, end)
        got = _read_gray_png(path, w, h)
        want = _ramp_reference(w, h, start, end)
        d = int(np.abs(got - want).max())
        worst = max(worst, d)
        ctx.note(f"{w}x{h} start {start} end {end}: max |png - numpy| = {d}, "
                 f"{len(np.unique(got))} distinct codes, "
                 f"min {int(got.min())} max {int(got.max())}")
    ctx.expect_eq("the baked ramp equals floor(255*ramp) everywhere", worst, 0)

    # The codes the tv-to-full expansion used to destroy. A full span ramp
    # visits every code, so their absence is the signature of the old bug.
    full = _read_gray_png(cg.radial_mask(256, 256, 0.0, 1.0), 256, 256)
    present = set(np.unique(full).tolist())
    ctx.expect_eq("the 256 code ramp still carries all 256 codes",
                  len(present), 256)
    ctx.expect_true("the codes at the bottom of the range survive the bake",
                    set(range(1, 17)) <= present,
                    f"missing: {sorted(set(range(1, 17)) - present)}")
    ctx.expect_true("the codes at the top of the range survive the bake",
                    set(range(235, 256)) <= present,
                    f"missing: {sorted(set(range(235, 256)) - present)}")


# --------------------------------------------------------------------------
# (c) the graph keeps using the hop
# --------------------------------------------------------------------------

def test_graph_attaches_the_radial_mask_through_the_hop(ctx):
    """A refactor that reintroduces a bare format=gbrp16le on the matte input
    would move every render with a radial blur in it and nothing else in the
    suite would notice, so the graph text itself is pinned."""
    info = H.info_for(CLIP)
    on = H.patch(H.defaults(), {"fx": {"radial_blur": {"enabled": True}}})
    off = H.patch(H.defaults(), {"fx": {"radial_blur": {"enabled": False}}})
    idx = cg.mask_input_indices(on)["radial"]
    head = f"[{idx}:v]{cg.WINDOW_MASK_FORMAT},setsar=1[mask]"
    graph_on = cg.graph_with_mask(on, info)
    graph_off = cg.graph_with_mask(off, info)

    ctx.note(f"radial matte input: {graph_on.split(';')[0]}")
    ctx.expect_true("the matte is attached through the 257 hop",
                    graph_on.startswith(head + ";"),
                    f"graph starts with {graph_on.split(';')[0]}")
    ctx.expect_true("no bare 8-bit to 16-bit shift is left on the matte",
                    f"[{idx}:v]format=gbrp16le" not in graph_on)
    ctx.expect_true("the hop is absent when the radial blur is off",
                    "[mask]" not in graph_off)


def register(suite):
    g = "radial"
    suite.add(g, "matte_widens_by_257", test_matte_widens_by_257_not_by_a_shift,
              doc="every matte code arrives at maskedmerge as exactly 257 times itself")
    suite.add(g, "maskedmerge_at_255_is_the_whole_overlay",
              test_maskedmerge_at_255_takes_the_whole_overlay,
              doc="a fully open ramp applies the whole blur, not 99.61% of it")
    suite.add(g, "baked_ramp_has_no_range_expansion",
              test_baked_ramp_has_no_range_expansion,
              doc="the PNG holds floor(255*ramp), with no tv-to-full stretch")
    suite.add(g, "graph_attaches_the_mask_through_the_hop",
              test_graph_attaches_the_radial_mask_through_the_hop,
              doc="the filter graph keeps the gray16le hop on the matte input")
