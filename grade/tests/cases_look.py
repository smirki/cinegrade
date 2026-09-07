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

import os
import tempfile

import numpy as np

import harness as H
from harness import cg

# The founder-approved look, so this stays pinned to something real rather than
# to whichever LUT happens to be first in the directory.
LOOK = "blockbuster"

# A second, distinct real look for the two-slot blend (C5). Distinct from LOOK
# so a bug that reads the wrong slot's LUT is visible rather than accidentally
# passing because both slots happened to point at the same file.
LOOK2 = "teal_orange"

CLIPS = [("clipA", H.CLIP_A, H.TIME_A), ("clipB", H.CLIP_B, H.TIME_B)]


def _frames(clip, t):
    no_look = H.render(clip, H.defaults(), t)
    full = H.render(clip, H.patch(H.defaults(),
                                  {"look": {"lut": LOOK, "mix": 1.0}}), t)
    return no_look, full


def _mix(clip, t, m):
    return H.render(clip, H.patch(H.defaults(),
                                  {"look": {"lut": LOOK, "mix": m}}), t)


def _render16(clip, t, cfg):
    """One graded frame as (h, w, 3) float64 in 16-bit code values (0..65535).

    build_graph's blend runs at 16 bits and the rest of this file reads it
    back at 8, which is exactly the rounding the docstring above budgets one
    code of slack for. "within 1 of 65535" only means what it says if the
    comparison itself is done at 16 bits, so this mirrors harness.render but
    keeps the format at rgb48le instead of downconverting to rgb24.
    """
    info = H.info_for(clip)
    w, h = info["width"], info["height"]
    head = f"[0:v]scale={w}:{h}:flags=bilinear[src]"
    graph = cg.graph_with_mask(cfg, info, tail_extra=["format=rgb48le"],
                               encode_out=False, src_label="src",
                               head_extra=head)
    args = cg.ffmpeg_inputs(str(clip), cfg, info, seek=t)
    args += ["-filter_complex", graph, "-map", "[vout]", "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"]
    raw = H._run_bytes(args)
    return np.frombuffer(raw, dtype="<u2").reshape(h, w, 3).astype(np.float64)


def test_look_lut_accepts_a_run_folder_relative_path(ctx):
    """Round 2 tooling note 10: look.lut also resolves against CONTENT (the
    run folder, the repo checkout), not only an absolute path or one
    relative to the CURRENT DIRECTORY (all `Path(lut).exists()` alone ever
    covered). Copies a real look cube somewhere else in the checkout
    (grade/tests/_work, never grade/luts/looks itself, so this never reads
    as a LUT_LOOKS-by-name hit) and points look.lut at it by a path
    relative to CONTENT, run with the CURRENT DIRECTORY changed to
    somewhere outside CONTENT entirely, so the existing CWD-relative check
    cannot be the one that finds it: only the new CONTENT fallback can."""
    src = H.CONTENT / "grade" / "luts" / "looks" / f"{LOOK}.cube"
    if not src.exists():
        ctx.skip(f"{src} is not present on this machine")
        return
    H.WORK.mkdir(parents=True, exist_ok=True)
    dest = H.WORK / "content_relative_look.cube"
    dest.write_bytes(src.read_bytes())
    rel = str(dest.relative_to(H.CONTENT))
    ctx.note(f"look.lut = {rel!r} (CONTENT-relative, not a LUT_LOOKS name)")

    cfg = H.patch(H.defaults(), {"look": {"lut": rel}})
    elsewhere = tempfile.mkdtemp(prefix="cinegrade-look-elsewhere-")
    old_cwd = os.getcwd()
    os.chdir(elsewhere)
    try:
        filt = cg.f_look(cfg)
    finally:
        os.chdir(old_cwd)
    ctx.expect_eq("exactly one lut3d filter is built", len(filt), 1)
    if filt:
        ctx.expect_true("the filter points at the CONTENT-relative file, "
                        "not a LUT_LOOKS lookup",
                        cg.esc(dest) in filt[0], filt[0])


def test_look_lut_missing_names_both_the_looks_folder_and_the_path_option(ctx):
    """The error for a look.lut that is neither a real path nor a LUT_LOOKS
    stem names both places a caller could have meant: the looks folder
    (with what IS actually there, so a typo is obvious) and that a path
    (absolute or CONTENT-relative) is also accepted, not only a bare
    name."""
    cfg = H.patch(H.defaults(), {"look": {"lut": "not-a-real-look-xyz"}})
    try:
        cg.f_look(cfg)
        ctx.expect_true("a bogus look.lut raises", False, "no exception")
        return
    except cg.GradeError as exc:
        msg = str(exc)
    ctx.note(msg)
    ctx.expect_true("names the looks folder", str(cg.LUT_LOOKS) in msg, msg)
    ctx.expect_true("mentions a path is also accepted",
                    "path" in msg.lower(), msg)
    ctx.expect_true("names the bad value itself",
                    "not-a-real-look-xyz" in msg, msg)


def test_lut2_null_is_byte_identical_to_today(ctx):
    """The compatibility gate for C5: lut2 unset must match today's single
    slot output exactly, even when balance and mix2 are pushed away from
    their defaults, because nothing in build_graph may read them unless
    lut2 names a real LUT (look2_active is False whenever lut2 is falsy).
    """
    for name, clip, t in CLIPS:
        _, full = _frames(clip, t)
        cfg = H.patch(H.defaults(), {"look": {
            "lut": LOOK, "mix": 1.0, "lut2": None, "mix2": 0.3,
            "balance": 0.7}})
        got = H.render(clip, cfg, t)
        delta = int(np.abs(got.astype(np.int16) - full.astype(np.int16)).max())
        ctx.expect_eq(f"{name}: lut2=None is pixel-identical to today "
                      f"regardless of balance/mix2", delta, 0)


def test_balance_one_is_lut2_alone(ctx):
    """balance=1.0 with mix2=1.0 must equal lut2 alone: the same "skip the
    blend at the extreme" trick mix already uses at 0.999, applied to slot 1
    instead (need_slot1 is False once balance >= 0.999, so A is never built).
    """
    for name, clip, t in CLIPS:
        two_slot = H.render(clip, H.patch(H.defaults(), {"look": {
            "lut": LOOK, "mix": 1.0, "lut2": LOOK2, "mix2": 1.0,
            "balance": 1.0}}), t)
        lut2_alone = H.render(clip, H.patch(H.defaults(),
                                            {"look": {"lut": LOOK2, "mix": 1.0}}), t)
        delta = int(np.abs(two_slot.astype(np.int16)
                           - lut2_alone.astype(np.int16)).max())
        ctx.expect_eq(f"{name}: balance=1.0, mix2=1.0 is pixel-identical to "
                      f"lut2 alone", delta, 0)


def test_balance_half_is_the_average_of_the_two_branches(ctx):
    """out = lerp(A, B, balance): at balance=0.5 with both slots at mix 1.0,
    out must equal (A + B) / 2 to within 1 sixteen-bit code, read back at 16
    bits so the tolerance is not swamped by the 8-bit test frames' own
    rounding.
    """
    for name, clip, t in CLIPS:
        cfg_a = H.patch(H.defaults(), {"look": {"lut": LOOK, "mix": 1.0}})
        cfg_b = H.patch(H.defaults(), {"look": {"lut": LOOK2, "mix": 1.0}})
        cfg_both = H.patch(H.defaults(), {"look": {
            "lut": LOOK, "mix": 1.0, "lut2": LOOK2, "mix2": 1.0,
            "balance": 0.5}})
        a16 = _render16(clip, t, cfg_a)
        b16 = _render16(clip, t, cfg_b)
        got16 = _render16(clip, t, cfg_both)
        avg16 = (a16 + b16) / 2.0
        err = np.abs(got16 - avg16)
        ctx.note(f"{name}: balance=0.5 vs (A+B)/2 max error "
                 f"{err.max():.2f} of 65535, mean {err.mean():.4f}")
        ctx.expect_le(f"{name}: balance=0.5 matches the average of the two "
                      f"branches within 1 of 65535", float(err.max()), 1.0)


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
    suite.add(g, "look_lut_accepts_a_run_folder_relative_path",
              test_look_lut_accepts_a_run_folder_relative_path,
              doc="look.lut also resolves a path relative to CONTENT, the "
                  "run folder")
    suite.add(g, "look_lut_missing_names_both_the_looks_folder_and_the_path_option",
              test_look_lut_missing_names_both_the_looks_folder_and_the_path_option,
              doc="the not-found error names LUT_LOOKS and that a path is "
                  "also accepted")
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
    suite.add(g, "lut2_null_is_byte_identical", test_lut2_null_is_byte_identical_to_today,
              doc="C5: lut2=None must be pixel-identical to today regardless "
                  "of balance/mix2")
    suite.add(g, "balance_one_is_lut2_alone", test_balance_one_is_lut2_alone,
              doc="C5: balance=1.0, mix2=1.0 must be pixel-identical to lut2 alone")
    suite.add(g, "balance_half_is_the_average", test_balance_half_is_the_average_of_the_two_branches,
              doc="C5: balance=0.5 must equal the average of the two branches "
                  "within 1 of 65535")
