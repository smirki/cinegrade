"""Group 4: look.mix, pinned to the bug that already shipped.

look.mix was declared and never read, and when it was implemented the blend was
inverted, so mix=0.0 produced output identical to mix=1.0. AGENTS.md now states
the contract: "This is real blending: mix 0.0 is pixel-identical to no look,
1.0 to the full look."

Pixel-identical means exactly that here. Not "close": build_graph takes a
shortcut at mix >= 0.999 and feeds the blend a split of one source at mix 0, so
both ends are exact operations and any drift is a real change.
"""

from __future__ import annotations

import numpy as np

import harness as H

# The founder-approved look, so this stays pinned to something real rather than
# to whichever LUT happens to be first in the directory.
LOOK = "blockbuster"

CLIPS = [("clipA", H.CLIP_A, H.TIME_A), ("clipB", H.CLIP_B, H.TIME_B)]


def _frames(clip, t):
    no_look = H.render(clip, H.defaults(), t)
    full = H.render(clip, H.patch(H.defaults(),
                                  {"look": {"lut": LOOK, "mix": 1.0}}), t)
    return no_look, full


def _mix(clip, t, m):
    return H.render(clip, H.patch(H.defaults(),
                                  {"look": {"lut": LOOK, "mix": m}}), t)


def test_mix_zero_is_no_look(ctx):
    for name, clip, t in CLIPS:
        no_look, full = _frames(clip, t)
        got = _mix(clip, t, 0.0)
        delta = int(np.abs(got.astype(np.int16) - no_look.astype(np.int16)).max())
        apart = int(np.abs(full.astype(np.int16) - no_look.astype(np.int16)).max())
        ctx.note(f"{name}: the look moves pixels by up to {apart} codes, "
                 f"so an inverted blend would be obvious here")
        ctx.expect_eq(f"{name}: mix=0.0 is pixel-identical to no look at all",
                      delta, 0)


def test_mix_one_is_the_full_look(ctx):
    for name, clip, t in CLIPS:
        _, full = _frames(clip, t)
        got = _mix(clip, t, 1.0)
        delta = int(np.abs(got.astype(np.int16) - full.astype(np.int16)).max())
        ctx.expect_eq(f"{name}: mix=1.0 is pixel-identical to the full look",
                      delta, 0)


def test_mix_just_under_one_is_still_almost_the_full_look(ctx):
    """The shortcut at mix >= 0.999 hides an inverted blend.

    mix=1.0 skips the blend entirely and applies the LUT straight, so it would
    pass even with the operands swapped. 0.998 goes through the real blend and
    must land next to the full look, not next to the ungraded frame.
    """
    for name, clip, t in CLIPS:
        no_look, full = _frames(clip, t)
        got = _mix(clip, t, 0.998)
        to_full = H.mean_abs_diff(got, full)
        to_none = H.mean_abs_diff(got, no_look)
        ctx.note(f"{name}: mix=0.998 sits {to_full:.4f} from the full look and "
                 f"{to_none:.4f} from no look")
        ctx.expect_lt(f"{name}: mix=0.998 is nearer the full look than the source",
                      to_full, to_none)


def test_mix_half_is_strictly_between(ctx):
    for name, clip, t in CLIPS:
        no_look, full = _frames(clip, t)
        half = _mix(clip, t, 0.5)
        lo = np.minimum(no_look.astype(np.int16), full.astype(np.int16))
        hi = np.maximum(no_look.astype(np.int16), full.astype(np.int16))
        h = half.astype(np.int16)
        # One code of slack: the blend runs at 16 bits and the readback is 8,
        # so an exact midpoint can round to either neighbour.
        inside = float(((h >= lo - 1) & (h <= hi + 1)).mean())
        to_none = H.mean_abs_diff(half, no_look)
        to_full = H.mean_abs_diff(half, full)
        both = H.mean_abs_diff(full, no_look)
        ctx.note(f"{name}: mix=0.5 is {to_none:.4f} from no look, "
                 f"{to_full:.4f} from the full look, which are "
                 f"{both:.4f} apart")
        ctx.expect_ge(f"{name}: mix=0.5 lies between the two everywhere",
                      inside, 0.999)
        ctx.expect_gt(f"{name}: mix=0.5 differs from no look", to_none, 0.0)
        ctx.expect_gt(f"{name}: mix=0.5 differs from the full look", to_full, 0.0)
        ctx.expect_close(f"{name}: mix=0.5 sits at the midpoint",
                         to_none / max(1e-9, both), 0.5, 0.05)


def test_mix_is_monotone(ctx):
    """Rising mix must move steadily away from the source, with no reversal."""
    clip, t = H.CLIP_A, H.TIME_A
    no_look, _ = _frames(clip, t)
    seq = []
    for m in (0.0, 0.25, 0.5, 0.75, 1.0):
        d = H.mean_abs_diff(_mix(clip, t, m), no_look)
        seq.append((m, d))
        ctx.note(f"mix {m:.2f}: {d:.4f} from the ungraded frame")
    for (m0, d0), (m1, d1) in zip(seq, seq[1:]):
        ctx.expect_gt(f"mix {m1} is further from the source than mix {m0}", d1, d0)


def register(suite):
    g = "look"
    suite.add(g, "mix_zero_is_no_look", test_mix_zero_is_no_look,
              doc="mix=0.0 must be pixel-identical to no look at all")
    suite.add(g, "mix_one_is_full_look", test_mix_one_is_the_full_look,
              doc="mix=1.0 must be pixel-identical to the full look")
    suite.add(g, "mix_just_under_one", test_mix_just_under_one_is_still_almost_the_full_look,
              doc="0.998 goes through the real blend, so it catches an inversion")
    suite.add(g, "mix_half_between", test_mix_half_is_strictly_between,
              doc="mix=0.5 lies strictly between the two ends")
    suite.add(g, "mix_monotone", test_mix_is_monotone,
              doc="look strength rises with mix, with no reversal")
