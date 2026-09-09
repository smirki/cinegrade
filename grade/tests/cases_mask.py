"""Group 23: the mask component stack (contract C1) and the matte store (C2).

A layer's mask used to be one window times one colour key. C1 makes it a
stack of components combined with add, intersect and subtract, each with its
own invert and feather, over a matte read from disk, a colour key, a window
(now including a linear gradient) or a luminance range, with matte finesse on
the result. This group is about four claims:

  parity      a config with no components emits the graph the engine emitted
              before any of this existed, names the same baked files, and
              renders the same bytes. Proved against a fingerprint blessed
              from the PRE-change engine, not against this engine's own
              opinion of itself (see legacy_parity.py).
  arithmetic  add is max, intersect is a product, subtract is a product with
              the complement, folded from zero top to bottom. Asserted
              against exact regions: inside the selection the correction is
              applied IN FULL and outside it is not applied AT ALL, to zero
              tolerance, the same standard cases_window holds the window to.
  agreement   the ffmpeg graph and the numpy reference (cinegrade.mask_matte)
              land on the same matte, so the GPU port and the server's
              measurement have one definition to follow rather than three.
  the store   a matte that is missing, partial, or written only for one frame
              is a normal state: the render falls back to the nearest written
              frame, says so in the warnings, and refuses only when the
              caller asked for a finished render.

The matte fixtures are built here, in the run's own scratch folder, with
CINEGRADE_MATTE_ROOT pointed at it. The suite therefore cannot read (or
write) the real matte store under studio/data, whatever else is on this
machine.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path

import numpy as np

import harness as H
from harness import cg

import legacy_parity

CLIP = H.CLIP_A
T = H.TIME_A

# Every matte this group builds lives under the run's own scratch directory.
# Set before any test runs, so cinegrade.resolve_matte never looks at
# studio/data/mattes: a test suite must not be able to see somebody's real
# tracked mattes, and it must not be able to leave one behind either.
MATTE_ROOT = H.WORK / "mattes"
os.environ["CINEGRADE_MATTE_ROOT"] = str(MATTE_ROOT)

MT = cg._mattes()

# A correction big enough that "applied" and "not applied" are never a
# judgement call. No key: the component stack IS the selection now, so the
# cube is the layer's full correction (variant "one").
STRONG = {"sat_gain": 0.0, "lum_gain": 0.4, "exposure": 0.5}

# Two hard edged halves of the frame, as window components. Hard edged so
# every pixel is either fully in or fully out and the assertions can be
# exact. `w`/`h` of 2.0 means "past both edges", which is how a rect covers a
# whole axis.
LEFT = {"shape": "rect", "cx": 0.25, "cy": 0.5, "w": 0.5, "h": 2.0,
        "rotation": 0.0, "softness": 0.0, "invert": False}
TOP = {"shape": "rect", "cx": 0.5, "cy": 0.25, "w": 2.0, "h": 0.5,
       "rotation": 0.0, "softness": 0.0, "invert": False}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _layer(components, mask=None, correct=None, **kw):
    """One layer whose mask is a component stack."""
    out = deepcopy(cg.LAYER_DEFAULTS)
    out["mask"] = cg.deep_merge(out["mask"], mask or {})
    out["mask"]["components"] = components
    out["correct"] = cg.deep_merge(out["correct"], correct or STRONG)
    out.update(kw)
    return out


def _cfg(layer):
    return H.patch(H.defaults(), {"layers": [layer]})


def _win(block, op="add", **kw):
    return dict({"type": "window", "op": op, "window": block}, **kw)


def _cfg_plain():
    return H.defaults()


def _cfg_full():
    """The same correction with no mask at all: the layer applied everywhere."""
    return _cfg(_layer([_win(dict(LEFT, w=4.0, h=4.0))]))


def _reference(layer, img):
    """cinegrade.mask_matte for a rendered frame's size."""
    h, w, _ = img.shape
    info = dict(H.info_for(CLIP), width=w, height=h)
    return cg.mask_matte(layer, info, rgb=H.as_float(img), time_s=T)


def _mean_abs(a, b, where):
    d = np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2)
    return float(d[where].mean()) if where.any() else 0.0


def _gating(ctx, layer, label, tol_in=0.0, tol_out=0.0):
    """The one assertion this whole group is about.

    Renders the layer, and where the reference matte says 1 the frame must
    equal the fully applied correction exactly; where it says 0 it must equal
    the ungraded frame exactly. Returns the reference matte so a caller can
    make further claims about its shape.
    """
    cfg = _cfg(layer)
    got = H.render(CLIP, cfg, T)
    plain = H.render(CLIP, _cfg_plain(), T)
    full = H.render(CLIP, _cfg_full(), T)
    ref = _reference(layer, got)
    inside, outside = ref >= 0.999, ref <= 0.001
    ctx.note(f"{label}: matte selects {ref.mean() * 100:.2f}% of the frame "
             f"({int(inside.sum())} full, {int(outside.sum())} empty, "
             f"{int(((ref > 0.001) & (ref < 0.999)).sum())} partial)")
    in_full = _mean_abs(got, full, inside)
    out_plain = _mean_abs(got, plain, outside)
    ctx.note(f"{label}: inside vs the applied correction {in_full:.4f}, "
             f"outside vs the ungraded frame {out_plain:.4f} of 255")
    ctx.expect_close(f"{label}: inside the matte the correction is applied in full",
                     in_full, 0.0, tol_in)
    ctx.expect_close(f"{label}: outside the matte nothing at all is applied",
                     out_plain, 0.0, tol_out)
    return ref, got, plain, full


def make_matte(name, frames: dict, width=64, height=36, fps=24.0,
               total=None, state="done", clip_key="clipA",
               areas=None, ious=None) -> str:
    """Write a matte fixture into the run's store and return its id.

    `frames` maps a frame index to a (height, width) uint8 array. Anything
    not in it is simply not written, which is how a partial matte is built:
    the store's own answer to "what is missing" is the directory listing.

    `areas` and `ious` are the per frame arrays the SAM service writes
    alongside the frames (contract C2, checkpoint gap 18), aligned to the
    absolute frame index and padded with None past what is written. They are
    what `quality()` judges a track by, so a fixture that omits them can only
    ever produce "nothing suspect" (round 1 finding 15: every assertion in
    the arc was written against exactly that).
    """
    root = MATTE_ROOT / clip_key / name
    root.mkdir(parents=True, exist_ok=True)
    for idx, arr in frames.items():
        MT.write_gray_png(root / MT.frame_name(idx), arr)
    index = {
        "matte_id": name, "clip": CLIP.name, "clip_key": clip_key,
        "rotation": "auto", "fps": fps,
        "frames": total if total is not None else (max(frames) + 1),
        "width": width, "height": height, "recipe": {"text": ["person"]},
        "state": state, "done_frames": len(frames),
        "created": 0.0, "model": "test", "backend": "stub"}
    if areas is not None:
        index["areas"] = list(areas)
    if ious is not None:
        index["ious"] = list(ious)
    (root / "index.json").write_text(json.dumps(index))
    MT.forget_cache()
    return name


def read_gray_png(path, w, h):
    """A baked matte back as uint8, through the harness' own ffmpeg runner so
    the file is registered for the run's cache cleanup."""
    raw = H._run_bytes(["ffmpeg", "-v", "error", "-i", str(path),
                        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"])
    return np.frombuffer(raw[:w * h], np.uint8).reshape(h, w)


def band(width, height, x0, x1, value=255):
    """A vertical band of `value`, everything else black."""
    a = np.zeros((height, width), np.uint8)
    a[:, int(x0):int(x1)] = value
    return a


def _render_frames(cfg, n, t=T, width=H.WIDTH):
    """`n` consecutive frames of a grade, as a list of (h, w, 3) uint8.

    The harness renders one frame; a moving matte can only be proved over
    several, so this is the same call with -frames:v n. Bounded by n, and n
    is never more than a handful.
    """
    info = H.info_for(CLIP, width)
    w, h = info["width"], info["height"]
    head = f"[0:v]scale={w}:{h}:flags=bilinear[src]"
    graph = cg.graph_with_mask(cfg, info, tail_extra=["format=rgb24"],
                               encode_out=False, src_label="src",
                               head_extra=head)
    args = cg.ffmpeg_inputs(str(CLIP), cfg, info, seek=t)
    args += ["-filter_complex", graph, "-map", "[vout]", "-frames:v", str(n),
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    raw = H._run_bytes(args)
    got = len(raw) // (w * h * 3)
    if got < n:
        raise H.RenderError(f"asked for {n} frames, got {got}")
    return [np.frombuffer(raw, np.uint8, w * h * 3, i * w * h * 3).reshape(h, w, 3)
            for i in range(n)]


# --------------------------------------------------------------------------
# (a) parity: nothing that has no components may move
# --------------------------------------------------------------------------

def test_legacy_parity_against_the_pre_change_engine(ctx):
    """Every legacy case emits the same graph, inputs and baked files.

    The fingerprint on disk was produced by the engine as it stood BEFORE
    components existed (legacy_parity.py --engine, run against a scratch copy
    of that checkout), so this is a real before and after and not this
    engine agreeing with itself. Graph text plus the sha1 of every cube and
    matte the graph names IS the render: two engines that agree on both
    cannot produce different pixels.
    """
    fixture = H.TESTS / "fixtures" / "legacy_parity.json"
    raw_blessed = json.loads(fixture.read_text())
    raw_got = legacy_parity.collect(Path(cg.ROOT))
    # One generated file has deliberately been renamed since the blessing: the
    # radial ramp, whose name used to round its own identity to two decimals
    # (round 2 finding 67). `stable()` folds that name to a placeholder on both
    # sides and folds nothing else, so what is compared is still the graph, the
    # input ORDER and the sha1 of every file's bytes.
    blessed = legacy_parity.stable(raw_blessed)
    got = legacy_parity.stable(raw_got)
    ctx.expect_true("the blessed fixture really is the pre-change engine's "
                    "output, carrying the old rounded radial name that the "
                    "fold exists for", raw_blessed != blessed,
                    "nothing to fold: either the fixture was re-blessed or "
                    "the fold has stopped matching")
    ctx.expect_true("and this engine's radial name is the hashed one, so the "
                    "fold is hiding a rename and not a missing file",
                    raw_got != got, "nothing to fold on this side")
    ctx.note(f"{len(blessed)} blessed cases, {len(got)} rebuilt "
             f"({sum(len(c['files']) for c in got.values())} referenced files "
             f"hashed)")
    ctx.expect_true("the same set of cases", set(blessed) == set(got),
                    f"missing {sorted(set(blessed) - set(got))}, "
                    f"extra {sorted(set(got) - set(blessed))}")
    diffs = []
    for name in sorted(set(blessed) & set(got)):
        for part in ("graph", "inputs", "files"):
            if blessed[name][part] != got[name][part]:
                diffs.append(f"{name}.{part}")
    ctx.expect_true("every legacy case is byte identical to the blessed one",
                    not diffs, f"moved: {diffs[:8]}" if diffs else "none moved")


def test_the_legacy_fold_cannot_lose_a_file(ctx):
    """Round 3 finding 87: `stable()` folds a generated file name to a
    placeholder, and `files` is a dict KEYED by that name.

    Today every legacy case that carries a radial carries exactly one, so no
    two keys fold together and the fold is harmless. The risk is a future
    case with two radials at the same width and height, which is precisely
    what finding 67 added the hash to distinguish: both would fold to
    `radial_320x180_<ID>.png`, the second would silently replace the first in
    the dict, and the comparison would then be made on one file's bytes while
    believing it had checked two. On both sides, so it would not even fail.

    The fold now numbers a collision instead of swallowing it, in the order
    the graph references the files, so two radials stay two entries and their
    bytes are still compared one for one.
    """
    two = {"files": {"<MASKS>/radial_320x180_aaaaaaaaaaaaaaaa.png": "sha-one",
                     "<MASKS>/radial_320x180_bbbbbbbbbbbbbbbb.png": "sha-two",
                     "<LAYERS>/layer_0_deadbeef.cube": "sha-cube"}}
    folded = legacy_parity.stable(two)["files"]
    ctx.expect_eq("two radials at one size stay two entries", len(folded), 3)
    ctx.expect_eq("and both files' bytes are still in the comparison",
                  sorted(folded.values()), ["sha-cube", "sha-one", "sha-two"])
    ctx.expect_true("the folded names are numbered in graph order",
                    all("<ID0>" in k or "<ID1>" in k or k.endswith(".cube")
                        for k in folded),
                    str(sorted(folded)))
    ctx.expect_eq("the first radial the graph names is <ID0>",
                  list(folded)[0], "<MASKS>/radial_320x180_<ID0>.png")

    # The case that actually exists is untouched, so the blessed fixture is
    # still compared exactly as it was.
    one = {"files": {"<MASKS>/radial_320x180_0.55_1.00.png": "sha"}}
    ctx.expect_eq("one radial folds to the plain placeholder, as before",
                  list(legacy_parity.stable(one)["files"]),
                  ["<MASKS>/radial_320x180_<ID>.png"])
    ctx.expect_eq("and a folded string is unchanged by any of this",
                  legacy_parity.stable("gblur=...radial_320x180_abc.png"),
                  "gblur=...radial_320x180_<ID>.png")


def test_empty_components_list_is_the_legacy_mask(ctx):
    """`components: []` must be exactly "no components", not "a new mask".

    C1's wording is "absent or empty", and the two spellings arrive from
    different clients: a preset written before the mask work has no key at
    all, a UI that has drawn the panel and had nothing added writes the empty
    list. They have to render the same file.
    """
    info = H.info_for(CLIP)
    base = deepcopy(cg.LAYER_DEFAULTS)
    base["mask"] = cg.deep_merge(base["mask"], {
        "window": {"enabled": True, "softness": 0.2},
        "key": {"enabled": True, "hue_width": 200.0}})
    base["correct"] = cg.deep_merge(base["correct"], STRONG)
    absent = deepcopy(base)
    absent["mask"].pop("components")
    for name, layer in (("empty list", base), ("absent key", absent)):
        cfg = H.patch(H.defaults(), {"layers": [layer]})
        ctx.expect_eq(f"{name}: the graph is the legacy graph",
                      cg.graph_with_mask(cfg, info),
                      cg.graph_with_mask(
                          H.patch(H.defaults(), {"layers": [absent]}), info))
    a = H.render(CLIP, H.patch(H.defaults(), {"layers": [base]}), T)
    b = H.render(CLIP, H.patch(H.defaults(), {"layers": [absent]}), T)
    ha = hashlib.sha1(a.tobytes()).hexdigest()[:16]
    hb = hashlib.sha1(b.tobytes()).hexdigest()[:16]
    ctx.note(f"rendered hash with the empty list {ha}, without the key {hb}")
    ctx.expect_eq("the rendered frame is identical", ha, hb)


# --------------------------------------------------------------------------
# (b) the three ops
# --------------------------------------------------------------------------

def test_add_is_the_union(ctx):
    layer = _layer([_win(LEFT), _win(TOP, op="add")])
    ref, _got, _plain, _full = _gating(ctx, layer, "add")
    h, w = ref.shape
    ctx.expect_close("the union covers three quarters of the frame",
                     float(ref.mean()), 0.75, 0.01)
    ctx.expect_close("bottom right is out", float(ref[int(h * 0.8), int(w * 0.8)]),
                     0.0, 0.0)
    ctx.expect_close("bottom left is in", float(ref[int(h * 0.8), int(w * 0.2)]),
                     1.0, 0.0)


def test_intersect_is_the_product(ctx):
    layer = _layer([_win(LEFT), _win(TOP, op="intersect")])
    ref, _g, _p, _f = _gating(ctx, layer, "intersect")
    h, w = ref.shape
    ctx.expect_close("the intersection is one quarter of the frame",
                     float(ref.mean()), 0.25, 0.01)
    ctx.expect_close("top left is in", float(ref[int(h * 0.2), int(w * 0.2)]),
                     1.0, 0.0)
    ctx.expect_close("bottom left is out", float(ref[int(h * 0.8), int(w * 0.2)]),
                     0.0, 0.0)


def test_subtract_removes_the_second(ctx):
    layer = _layer([_win(LEFT), _win(TOP, op="subtract")])
    ref, _g, _p, _f = _gating(ctx, layer, "subtract")
    h, w = ref.shape
    ctx.expect_close("left minus top is one quarter of the frame",
                     float(ref.mean()), 0.25, 0.01)
    ctx.expect_close("bottom left is in", float(ref[int(h * 0.8), int(w * 0.2)]),
                     1.0, 0.0)
    ctx.expect_close("top left is out", float(ref[int(h * 0.2), int(w * 0.2)]),
                     0.0, 0.0)


def test_the_stack_folds_from_zero(ctx):
    """A stack whose first component is not an add selects nothing.

    C1: "The stack is evaluated top to bottom starting from 0". Intersecting
    with nothing is nothing and subtracting from nothing is nothing, which is
    also how Lightroom behaves (the first component is always an add). The
    engine has to agree with the contract rather than quietly promote the
    first component, because a UI that reorders a stack would otherwise
    change the picture in a way the numbers do not explain.
    """
    info = H.info_for(CLIP)
    for op in ("intersect", "subtract"):
        layer = _layer([_win(LEFT, op=op)])
        ctx.expect_eq(f"a lone {op} component reaches nothing",
                      cg.stack_components(layer), [])
        ctx.expect_true(f"a layer whose stack is empty is inactive ({op})",
                        not cg.layer_active(layer), "layer_active is False")
        cfg = _cfg(layer)
        ctx.expect_eq(f"{op}: no mask input is added",
                      cg.ffmpeg_inputs(str(CLIP), cfg, info),
                      cg.ffmpeg_inputs(str(CLIP), H.defaults(), info))
        a = H.render(CLIP, cfg, T)
        b = H.render(CLIP, _cfg_plain(), T)
        ctx.expect_eq(f"{op}: the frame is the ungraded frame",
                      hashlib.sha1(a.tobytes()).hexdigest()[:12],
                      hashlib.sha1(b.tobytes()).hexdigest()[:12])
    # ... and the same stack inverted is a GLOBAL correction, because the
    # complement of nothing is everything.
    inv = _layer([_win(LEFT, op="intersect")], mask={"invert": True})
    ctx.expect_true("inverted, the same empty stack is active",
                    cg.layer_active(inv), "layer_active is True")
    got = H.render(CLIP, _cfg(inv), T)
    full = H.render(CLIP, _cfg_full(), T)
    d = float(np.abs(got.astype(np.int16) - full.astype(np.int16)).max())
    ctx.note(f"inverted empty stack vs the correction everywhere: max {d:.0f} of 255")
    ctx.expect_le("an inverted empty stack is the correction everywhere", d, 1.0)


def test_an_empty_stack_refuses_to_build_a_graph(ctx):
    """mask_stack_segments raises rather than emitting a "[None]" label.

    The fold starts at zero, so a stack with nothing enabled (or one whose
    first enabled component is not an add) has no stream to hang the finesse
    on. build_layers checks stack_components() before it calls in, which is
    why the case above renders correctly today, but a function that formats
    a label it never set is a trap for the next caller: the graph would name
    a filter "None" and ffmpeg would fail somewhere far from the cause. The
    guard belongs in the function, so this asserts it there and asserts the
    working paths still work.
    """
    info = H.info_for(CLIP)
    for name, layer in (
            ("all disabled", _layer([_win(LEFT, enabled=False)])),
            ("no components", _layer([])),
            ("first is an intersect", _layer([_win(LEFT, op="intersect")])),
    ):
        ctx.expect_eq(f"{name}: the stack folds to nothing",
                      cg.stack_components(layer), [])
        try:
            cg.mask_stack_segments(layer, 0, info, {})
            ctx.check(False, f"{name}: a graph was built out of nothing")
        except cg.GradeError as exc:
            ctx.expect_true(f"{name}: it refuses and says which layer",
                            "layer 0" in str(exc), str(exc)[:60])
    # And the guard is not in the way of anything real: the same layer with
    # an add in front builds, and the empty ones still render as before.
    segs = cg.mask_stack_segments(_layer([_win(LEFT)]), 0, info, {})
    ctx.expect_true("one add still builds its segments", bool(segs),
                    f"{len(segs)} segments, tail {segs[-1][-6:]}")
    ctx.expect_true("no segment carries a None label",
                    not any("None" in seg for seg in segs), "no None labels")
    empty = _cfg(_layer([_win(LEFT, enabled=False)]))
    a = H.render(CLIP, empty, T)
    b = H.render(CLIP, _cfg_plain(), T)
    ctx.expect_eq("a layer with nothing enabled still renders the plain frame",
                  hashlib.sha1(a.tobytes()).hexdigest()[:12],
                  hashlib.sha1(b.tobytes()).hexdigest()[:12])


def test_component_invert_flips_only_that_component(ctx):
    """A component's own invert is not the same as inverting the result.

    left minus (inverted top) is the bottom left quadrant's complement inside
    the left half, which is the top left quadrant: a different selection from
    inverting the whole of "left minus top". Both are rendered and asserted
    so the two controls cannot be collapsed into one.
    """
    comp_inv = _layer([_win(LEFT), _win(TOP, op="subtract", invert=True)])
    ref_a, _g, _p, _f = _gating(ctx, comp_inv, "component invert")
    h, w = ref_a.shape
    ctx.expect_close("top left survives", float(ref_a[int(h * .2), int(w * .2)]),
                     1.0, 0.0)
    ctx.expect_close("bottom left is subtracted",
                     float(ref_a[int(h * .8), int(w * .2)]), 0.0, 0.0)

    mask_inv = _layer([_win(LEFT), _win(TOP, op="subtract")],
                      mask={"invert": True})
    ref_b, _g, _p, _f = _gating(ctx, mask_inv, "mask invert")
    ctx.expect_close("mask invert selects three quarters",
                     float(ref_b.mean()), 0.75, 0.01)
    overlap = float((( ref_a > 0.5) & (ref_b > 0.5)).sum()) / max(
        1.0, float(((ref_a > 0.5) | (ref_b > 0.5)).sum()))
    ctx.note(f"component invert and mask invert overlap on {overlap * 100:.1f}% "
             f"of the pixels either selects")
    ctx.expect_lt("the two inverts are different selections", overlap, 0.5)


def test_feather_softens_one_component_only(ctx):
    """feather is a blur on the component, in fractions of frame width.

    The claim is about the EDGE: with a feather the boundary between selected
    and not selected stops being one pixel wide, while the far interior stays
    fully selected and the far exterior stays fully unselected. Asserted on
    the reference matte and then on the render, where the interior has to be
    exact even though the edge is not.
    """
    hard = _layer([_win(LEFT)])
    soft = _layer([_win(LEFT, feather=0.05)])
    img = H.render(CLIP, _cfg(soft), T)
    ref_hard = _reference(hard, img)
    ref_soft = _reference(soft, img)
    part_hard = float(((ref_hard > 0.01) & (ref_hard < 0.99)).mean())
    part_soft = float(((ref_soft > 0.01) & (ref_soft < 0.99)).mean())
    ctx.note(f"partial pixels: hard {part_hard * 100:.2f}%, "
             f"feathered {part_soft * 100:.2f}%")
    ctx.expect_lt("a hard component has essentially no ramp", part_hard, 0.02)
    ctx.expect_gt("a feathered component has a real ramp", part_soft, 0.05)
    full = H.render(CLIP, _cfg_full(), T)
    plain = H.render(CLIP, _cfg_plain(), T)
    deep_in = ref_soft >= 0.9999
    deep_out = ref_soft <= 0.0001
    ctx.note(f"deep inside {int(deep_in.sum())} px, deep outside "
             f"{int(deep_out.sum())} px")
    ctx.expect_close("far inside the feather the correction is still full",
                     _mean_abs(img, full, deep_in), 0.0, 0.6)
    ctx.expect_close("far outside it is still nothing",
                     _mean_abs(img, plain, deep_out), 0.0, 0.6)


# --------------------------------------------------------------------------
# (c) the key component
# --------------------------------------------------------------------------

def test_the_feather_and_blur_cap(ctx):
    """Round 2 finding 52: feather and finesse.blur are capped, in all three
    implementations, and a value outside [0, 1] is refused at the door.

    Why it is a cap and not just a big number. Both controls are a fraction of
    frame WIDTH, and the gaussian pads by three sigma on each side, so a
    feather of 50 on a 1920 frame asks for a sigma of 96000 pixels and a
    padded float64 plane of roughly 300 gigabytes. `POST /api/stats` takes a
    whole mask block from the wire and measures it on the request thread, so
    one request could have done that to the machine the studio and the model
    share. The cap is on the CONTROL and quoted as a fraction, the same shape
    as the grow cap, so the browser preview at 640 and the render at 3840
    clamp at the same fraction of the picture rather than at the same pixel
    count.

    Four claims: the arithmetic, the numpy reference really going through it,
    the ffmpeg graph really going through it, and the refusal for a value that
    is a typo rather than an intention.
    """
    ctx.expect_eq("the cap is a tenth of frame width", cg.MASK_BLUR_MAX, 0.10)
    ctx.expect_close("a feather under the cap is granted in full, in pixels",
                     cg.mask_blur_sigma(0.02, 640), 12.8, 1e-9)
    ctx.expect_close("a feather over the cap gets the cap",
                     cg.mask_blur_sigma(50, 640), 64.0, 1e-9)
    ctx.expect_close("and the same ask at a render width gets the same "
                     "FRACTION, which is the point of quoting it as one",
                     cg.mask_blur_sigma(50, 3840) / 3840.0,
                     cg.mask_blur_sigma(50, 640) / 640.0, 1e-12)
    ctx.expect_eq("a negative feather is zero, not a blur the other way",
                  cg.mask_blur_sigma(-2, 640), 0.0)
    ctx.expect_eq("zero costs nothing", cg.mask_blur_sigma(0, 640), 0.0)

    # The numpy reference: an absurd feather has to compose EXACTLY as a
    # feather at the cap, or the clamp is only in the helper and not on the
    # path mask_matte takes.
    img = H.render(CLIP, _cfg_plain(), T)
    capped = _reference(_layer([_win(LEFT, feather=cg.MASK_BLUR_MAX)]), img)
    absurd = _reference(_layer([_win(LEFT, feather=50.0)]), img)
    soft = _reference(_layer([_win(LEFT, feather=0.01)]), img)
    ctx.expect_true("the reference composes an absurd feather exactly as one "
                    "at the cap", bool(np.array_equal(capped, absurd)),
                    f"max difference {float(np.abs(capped - absurd).max()):.6f}")
    ctx.expect_gt("and a feather under the cap is a different matte, so the "
                  "check above is comparing something",
                  float(np.abs(capped - soft).max()), 0.01)
    # The same for the finesse blur, which is a second call site.
    fin_capped = _reference(_layer([_win(LEFT)],
                                   mask={"finesse": {"blur": cg.MASK_BLUR_MAX}}),
                            img)
    fin_absurd = _reference(_layer([_win(LEFT)],
                                   mask={"finesse": {"blur": 50.0}}), img)
    ctx.expect_true("finesse.blur is capped on the same path",
                    bool(np.array_equal(fin_capped, fin_absurd)),
                    f"max difference "
                    f"{float(np.abs(fin_capped - fin_absurd).max()):.6f}")

    # The ffmpeg graph: the sigma it names is the clamped one. This is the
    # third implementation, and the one that would otherwise hand the number
    # straight to gblur.
    segs = cg.mask_stack_segments(_layer([_win(LEFT, feather=50.0)]), 0,
                                  dict(H.info_for(CLIP), width=640, height=360),
                                  {})
    graph = " ".join(str(x) for x in segs)
    ctx.expect_true("the filter graph asks gblur for the capped sigma, not "
                    "for the sigma that was typed",
                    "sigma=64.000" in graph and "sigma=32000" not in graph,
                    graph[:300])

    # And the door. A value outside [0, 1] is a typo, so it is refused with a
    # sentence rather than silently clamped: this is the function every
    # scripted caller comes through (POST /api/stats's `mask`, `cinegrade
    # stats --mask`).
    for label, block in (
            ("a component feather",
             {"components": [_win(LEFT, feather=50.0)]}),
            ("finesse.blur",
             {"components": [_win(LEFT)], "finesse": {"blur": 50.0}}),
            ("a NEGATIVE feather past the range",
             {"components": [_win(LEFT, feather=-3.0)]})):
        raised = ""
        try:
            cg.mask_stack_layer(block)
        except cg.GradeError as exc:
            raised = str(exc)
        ctx.expect_true(f"{label} of 50 is refused", bool(raised),
                        raised[:80] or "nothing raised")
        ctx.expect_true(f"and the refusal for {label} says what the number "
                        f"means and what the range is",
                        "fraction" in raised.lower()
                        and "between 0 and 1" in raised, raised[:200])
    ok = ""
    try:
        cg.mask_stack_layer({"components": [_win(LEFT, feather=0.5)],
                             "finesse": {"blur": 1.0}})
    except cg.GradeError as exc:
        ok = str(exc)
    ctx.expect_true("a feather of 0.5 and a blur of 1.0 are still accepted: "
                    "the refusal is for typos, not for wide blurs", not ok, ok)

    # A disabled component is checked too: somebody who typed 50 into a
    # switched off component wants to hear about it before enabling it.
    off = ""
    try:
        cg.mask_stack_layer({"components": [_win(LEFT),
                                            _win(TOP, feather=50.0,
                                                 enabled=False)]})
    except cg.GradeError as exc:
        off = str(exc)
    ctx.expect_true("a switched off component's feather is checked as well",
                    bool(off), off[:80] or "nothing raised")


def test_the_component_count_cap(ctx):
    """Round 3 finding 82: the blur cap bounds ONE component and nothing
    bounded how many components a request asks for.

    The cost the round 2 cap left open, measured on this tree: one component
    at `feather = MASK_BLUR_MAX` on a 960x540 frame costs about 59 ms in the
    numpy reference (the same fold `POST /api/stats` runs on its request
    thread), a component with distinct numbers serialises to roughly 180
    bytes, and the studio's body cap is 8 MB. That is about 46,000 components
    in one legal POST: roughly 46 minutes of CPU held by one request on a
    threaded server that is also serving a live grading session. Every
    component was individually inside the round 2 limits.

    Two caps, because there are two ways to ask: one enormous stack, and a
    config full of legal stacks. They count different things (round 4 finding
    92): the per stack cap counts components, because that is what a stack
    carries, and the per request budget counts FOLDS, because that is what
    costs. A gaussian and a legacy window and key mask were free under a count
    of components. Both are refused rather than truncated: no
    real grade is anywhere near them (the biggest stack in bakeoff/ is two
    components, the biggest whole grade six), so a request that reaches one
    is a mistake and silently measuring a truncated version of it would be a
    worse answer than an error.
    """
    ctx.expect_eq("a stack may carry at most 32 components",
                  cg.MASK_STACK_MAX_COMPONENTS, 32)
    ctx.expect_eq("and one request at most 128 folds across its layers",
                  cg.MASK_REQUEST_MAX_FOLDS, 128)
    ctx.expect_gt("the request budget is the wider of the two, or a single "
                  "legal stack could not be measured at all",
                  cg.MASK_REQUEST_MAX_FOLDS, cg.MASK_STACK_MAX_COMPONENTS)

    # The door every scripted caller comes through: `cinegrade stats --mask`
    # and POST /api/stats {"mask": ...} both build their layer here.
    at_cap = [_win(LEFT, op="add" if i == 0 else "intersect")
              for i in range(cg.MASK_STACK_MAX_COMPONENTS)]
    ok = ""
    try:
        cg.mask_stack_layer({"components": at_cap})
    except cg.GradeError as exc:
        ok = str(exc)
    ctx.expect_true("a stack exactly at the cap is still built", not ok, ok[:200])

    over = ""
    try:
        cg.mask_stack_layer({"components": at_cap + [_win(TOP, op="add")]})
    except cg.GradeError as exc:
        over = str(exc)
    ctx.expect_true("one component past the cap is refused", bool(over),
                    over[:80] or "nothing raised")
    ctx.expect_true("and the refusal says how many it carried and what the "
                    "limit is",
                    "33" in over and "at most 32" in over, over[:200])

    # A disabled component counts, the same way _mask_blur_controls checks a
    # disabled component's feather: it is one toggle from being folded.
    off = ""
    try:
        cg.mask_stack_layer({"components": at_cap + [_win(TOP, op="add",
                                                          enabled=False)]})
    except cg.GradeError as exc:
        off = str(exc)
    ctx.expect_true("a switched off component counts towards the cap",
                    bool(off), off[:80] or "nothing raised")

    # The request budget: every stack legal, the total not. This is the shape
    # a per stack cap alone would let straight through.
    legal_stack = [_win(LEFT, op="add" if i == 0 else "intersect")
                   for i in range(cg.MASK_STACK_MAX_COMPONENTS)]
    layers = [_layer(deepcopy(legal_stack)) for _ in range(5)]
    total = ""
    try:
        cg.check_mask_components({"layers": layers})
    except cg.GradeError as exc:
        total = str(exc)
    ctx.expect_true("five legal stacks in one config are refused as a total",
                    bool(total), total[:80] or "nothing raised")
    ctx.expect_true("and the refusal names the total and says no single "
                    "stack was over the limit",
                    "160" in total and "at most 128" in total, total[:200])

    # And the shape of the biggest real grade on this machine passes: three
    # layers, two components each (bakeoff/masks/grade-C015-work.json).
    real = {"layers": [_layer([_win(LEFT), _win(TOP, op="intersect")])
                       for _ in range(3)]}
    refused = ""
    try:
        cg.check_mask_components(real, {"components": [_win(LEFT),
                                                       _win(TOP, op="intersect")]})
    except cg.GradeError as exc:
        refused = str(exc)
    ctx.expect_true("the biggest real grade here (3 layers, 6 components) "
                    "plus a two component measurement mask is untouched",
                    not refused, refused[:200])
    counts = cg.mask_component_counts(real, {"components": [_win(LEFT)]})
    ctx.expect_eq("and the counter sees every stack in the request",
                  [n for _what, n in counts], [2, 2, 2, 1])

    # Round 4 finding 92: the per stack cap counts components, and the request
    # budget counts what a request actually asks a machine to fold. Those are
    # not the same number, and counting components for both left two shapes
    # free: a gaussian, which is the expensive fold, and the legacy window and
    # key pair, which has no components to count at all.
    plain = _layer([_win(LEFT), _win(TOP, op="intersect")])
    blurred = _layer([_win(LEFT, feather=cg.MASK_BLUR_MAX),
                      _win(TOP, op="intersect")],
                     mask={"finesse": {"blur": cg.MASK_BLUR_MAX}})
    ctx.expect_eq("two components and no gaussian costs two folds",
                  [n for _what, n in cg.mask_fold_counts({"layers": [plain]})],
                  [2])
    ctx.expect_eq("the same stack with a feather and a finesse blur costs "
                  "four, because each gaussian is its own whole frame pass",
                  [n for _what, n in cg.mask_fold_counts({"layers": [blurred]})],
                  [4])
    ctx.expect_eq("while the per stack count still sees the two components it "
                  "is about",
                  [n for _what, n in
                   cg.mask_component_counts({"layers": [blurred]})], [2])

    legacy = deepcopy(cg.LAYER_DEFAULTS)
    legacy["mask"]["window"]["enabled"] = True
    legacy["mask"]["key"]["enabled"] = True
    legacy["mask"]["finesse"]["blur"] = cg.MASK_BLUR_MAX
    ctx.expect_eq("a legacy window and key mask carries no components at all",
                  [n for _what, n in
                   cg.mask_component_counts({"layers": [legacy]})], [0])
    ctx.expect_eq("and costs two folds: one matte out of the pair however "
                  "many halves are on, plus its blur",
                  [n for _what, n in cg.mask_fold_counts({"layers": [legacy]})],
                  [2])
    idle = _layer([])
    ctx.expect_eq("a mask that asks for nothing is absent from the folds, not "
                  "counted as zero",
                  cg.mask_fold_counts({"layers": [idle]}), [])

    at_budget = {"layers": [deepcopy(legacy)
                            for _ in range(cg.MASK_REQUEST_MAX_FOLDS // 2)]}
    edge = ""
    try:
        cg.check_mask_components(at_budget)
    except cg.GradeError as exc:
        edge = str(exc)
    ctx.expect_true("64 of them is exactly the budget and passes", not edge,
                    edge[:200])
    over_budget = {"layers": at_budget["layers"] + [deepcopy(legacy)]}
    legacy_total = ""
    try:
        cg.check_mask_components(over_budget)
    except cg.GradeError as exc:
        legacy_total = str(exc)
    ctx.expect_true("65 is refused, where a count of components saw zero of "
                    "them and let the whole body through",
                    bool(legacy_total),
                    legacy_total[:80] or "nothing raised")
    ctx.expect_true("and the refusal names the total in the unit it counted",
                    "130" in legacy_total and "at most 128" in legacy_total
                    and "fold" in legacy_total, legacy_total[:240])

    # Round 5 finding 104: the rule, the refusal and the README are one
    # sentence, not three paraphrases. The counter charges a fold for a NON
    # ZERO feather or blur and an extra fold to a mask with no COMPONENTS; the
    # refusal used to say "one per feather or finesse blur, and one for a mask
    # made of neither", which charged a zero feather and read as though the
    # legacy window and key form cost one fold when it costs two.
    for phrase in ("one per component",
                   "one per non zero feather or finesse blur",
                   "one for a mask that carries no components"):
        ctx.expect_true(f"the refusal states the rule it counts: {phrase!r}",
                        phrase in legacy_total, legacy_total[:240])
    ctx.expect_true("and no longer says a mask is charged for being made of "
                    "neither, which named the wrong thing",
                    "made of neither" not in legacy_total, legacy_total[:240])
    # The README wraps its lines, so compare on collapsed whitespace rather
    # than on where the paragraph happens to break.
    readme = " ".join((H.CONTENT / "studio" / "README.md").read_text().split())
    for phrase in ("one per component",
                   "one per non zero feather or finesse blur",
                   "one for a mask that carries no components"):
        ctx.expect_true(f"and studio/README.md gives the same rule: {phrase!r}",
                        phrase in readme, "not found in studio/README.md")
    # The two shapes the wording is about, proved rather than described.
    zero_feather = _layer([_win(LEFT, feather=0.0), _win(TOP, op="intersect")])
    ctx.expect_eq("a feather of zero is charged nothing, as the words say",
                  [n for _what, n in
                   cg.mask_fold_counts({"layers": [zero_feather]})], [2])


def test_key_component_matches_the_qualifier(ctx):
    """A key component selects what the legacy qualifier selects.

    Not byte identical to the legacy path and it cannot be: the legacy key is
    folded INSIDE the correction cube (the matte modulates the colour move in
    cube space), while a component keys the picture into a matte and
    maskedmerge lerps the two branches. What must hold is that the same
    pixels are chosen, which is what the overlap number below measures.
    """
    key = {"enabled": True, "hue_center": 30.0, "hue_width": 120.0,
           "hue_soft": 20.0, "sat_low": 0.05, "sat_soft": 0.2,
           "lum_low": 0.0, "lum_high": 1.0, "lum_soft": 0.1}
    comp = _layer([{"type": "key", "op": "add", "key": key}])
    legacy = deepcopy(cg.LAYER_DEFAULTS)
    legacy["mask"] = cg.deep_merge(legacy["mask"], {"key": key})
    legacy["correct"] = cg.deep_merge(legacy["correct"], STRONG)

    got = H.render(CLIP, _cfg(comp), T)
    old = H.render(CLIP, H.patch(H.defaults(), {"layers": [legacy]}), T)
    plain = H.render(CLIP, _cfg_plain(), T)
    a = np.abs(got.astype(np.int16) - plain.astype(np.int16)).max(axis=2) > 2
    b = np.abs(old.astype(np.int16) - plain.astype(np.int16)).max(axis=2) > 2
    overlap = float((a & b).sum()) / max(1.0, float((a | b).sum()))
    delta = float(np.abs(got.astype(np.int16) - old.astype(np.int16)).mean())
    ctx.note(f"component key touches {a.mean() * 100:.2f}% of pixels, the "
             f"legacy key {b.mean() * 100:.2f}%, overlap {overlap * 100:.1f}%, "
             f"mean difference {delta:.3f} of 255")
    ctx.expect_gt("the key really selects something", float(a.mean()), 0.02)
    ctx.expect_gt("the two routes select the same pixels", overlap, 0.9)
    ctx.expect_lt("and land on nearly the same picture", delta, 2.0)


def test_luma_component_is_a_key_with_hue_and_sat_open(ctx):
    """`luma` selects by brightness alone, whatever the colour.

    Asserted on the matte rather than on a render: the point is that hue and
    saturation do not narrow the selection, which is a statement about the
    qualifier and not about a picture.
    """
    comp = {"type": "luma", "op": "add",
            "key": {"lum_low": 0.0, "lum_high": 0.35, "lum_soft": 0.05}}
    k = cg.component_key(comp)
    hue = np.linspace(0.0, 359.0, 64)[None, :].repeat(8, 0)
    sat = np.linspace(0.0, 1.0, 64)[None, :].repeat(8, 0)
    dark = np.full_like(hue, 0.2)
    bright = np.full_like(hue, 0.8)
    m_dark = cg.key_matte(k, hue, sat, dark)
    m_bright = cg.key_matte(k, hue, sat, bright)
    ctx.note(f"dark pixels select {m_dark.mean():.4f}, bright "
             f"{m_bright.mean():.4f}, over every hue and saturation")
    ctx.expect_close("every dark pixel is selected, whatever its colour",
                     float(m_dark.min()), 1.0, 1e-9)
    ctx.expect_close("no bright pixel is", float(m_bright.max()), 0.0, 1e-9)


# --------------------------------------------------------------------------
# (d) the linear window
# --------------------------------------------------------------------------

def test_linear_window_geq_matches_numpy(ctx):
    """The baked matte and the reference agree on every pixel, at every angle.

    The same standard the ellipse and the rect are held to: the geq
    expression bakes the PNG the render reads, so if it and the numpy
    definition disagree, the picture disagrees with the measurement and
    nothing downstream can be trusted.
    """
    w, h = 160, 90
    worst = 0
    for angle in (0.0, 45.0, 90.0, 180.0, 270.0, 315.0):
        for soft in (0.0, 1.0, 2.0):
            block = {"shape": "linear", "cx": 0.5, "cy": 0.5, "w": 0.4,
                     "angle": angle, "softness": soft}
            want = cg.window_matte(block, w, h)
            path = cg.window_mask(block, w, h)
            got = read_gray_png(path, w, h)
            d = int(np.abs(want.astype(np.int16) - got.astype(np.int16)).max())
            worst = max(worst, d)
    ctx.note(f"18 linear mattes compared, worst disagreement {worst} of 255")
    ctx.expect_le("the geq matte equals the numpy matte everywhere", worst, 0)


def test_linear_window_points_where_the_angle_says(ctx):
    """Angle 0 selects the top, 90 the right, clockwise on screen.

    A gradient with no stated direction is unusable, so the direction is
    asserted rather than left to whoever reads the code next.
    """
    w, h = 120, 80
    for angle, hot, cold in ((0.0, (5, 60), (75, 60)),
                             (90.0, (40, 110), (40, 10)),
                             (180.0, (75, 60), (5, 60)),
                             (270.0, (40, 10), (40, 110))):
        m = cg.window_matte({"shape": "linear", "cx": 0.5, "cy": 0.5,
                             "w": 0.3, "angle": angle, "softness": 1.0}, w, h)
        ctx.expect_eq(f"angle {angle:.0f}: the selected side is 255",
                      int(m[hot]), 255)
        ctx.expect_eq(f"angle {angle:.0f}: the other side is 0",
                      int(m[cold]), 0)
    m = cg.window_matte({"shape": "linear", "cx": 0.5, "cy": 0.5, "w": 0.3,
                         "angle": 0.0, "softness": 1.0}, w, h)
    mid = m[:, w // 2].astype(float) / 255.0
    ctx.note(f"the ramp runs {mid[0]:.2f} at the top to {mid[-1]:.2f} at the "
             f"bottom, {int(((mid > 0.02) & (mid < 0.98)).sum())} rows of "
             f"transition")
    ctx.expect_close("the centre line sits at half", float(m[h // 2, w // 2]),
                     128.0, 1.5)
    ctx.expect_true("the ramp never rises going down",
                    bool(np.all(np.diff(mid) <= 1e-9)), "monotone")


def test_linear_window_softness_is_the_curve(ctx):
    """softness shapes the falloff; the transition WIDTH is `w`.

    Two different jobs, deliberately separated: widening the transition is a
    geometry change and easing it is a taste change. The test asserts that
    the width of the non-binary band does not move with softness while the
    shape of the ramp does.
    """
    w, h = 200, 40
    widths, mids = {}, {}
    for soft in (0.5, 1.0, 3.0):
        m = cg.window_matte({"shape": "linear", "cx": 0.5, "cy": 0.5,
                             "w": 0.4, "angle": 90.0, "softness": soft},
                            w, h).astype(float) / 255.0
        row = m[h // 2]
        widths[soft] = int(((row > 0.02) & (row < 0.98)).sum())
        mids[soft] = float(row[int(w * 0.6)])
    ctx.note(f"transition width by softness: {widths}; value at 60% of the "
             f"frame: { {k: round(v, 3) for k, v in mids.items()} }")
    ctx.expect_le("softness barely changes how wide the transition is",
                  abs(widths[3.0] - widths[0.5]) / max(1, widths[1.0]), 0.5)
    ctx.expect_gt("a higher softness eases the ends", mids[3.0], mids[0.5])
    hard = cg.window_matte({"shape": "linear", "cx": 0.5, "cy": 0.5, "w": 0.4,
                            "angle": 90.0, "softness": 0.0}, w, h)
    ctx.expect_eq("softness 0 is a hard edge",
                  sorted(set(np.unique(hard).tolist())), [0, 255])


def test_linear_window_renders_as_a_gradient(ctx):
    """Through the real graph, not just in numpy: the correction fades."""
    layer = _layer([_win({"shape": "linear", "cx": 0.5, "cy": 0.5, "w": 0.6,
                          "angle": 90.0, "softness": 1.0})])
    got = H.render(CLIP, _cfg(layer), T)
    plain = H.render(CLIP, _cfg_plain(), T)
    full = H.render(CLIP, _cfg_full(), T)
    h, w, _ = got.shape
    cols = []
    for frac in (0.05, 0.35, 0.5, 0.65, 0.95):
        x = int(w * frac)
        d_plain = float(np.abs(got[:, x].astype(np.int16)
                               - plain[:, x].astype(np.int16)).mean())
        d_full = float(np.abs(got[:, x].astype(np.int16)
                              - full[:, x].astype(np.int16)).mean())
        cols.append((frac, d_plain, d_full))
    ctx.note("column, distance from ungraded, distance from fully graded: "
             + ", ".join(f"{f:.2f}/{a:.1f}/{b:.1f}" for f, a, b in cols))
    ctx.expect_close("the far left is untouched", cols[0][1], 0.0, 0.0)
    ctx.expect_close("the far right has the whole correction", cols[-1][2],
                     0.0, 0.0)
    ctx.expect_gt("the middle is somewhere in between", cols[2][1], 1.0)
    ctx.expect_gt("and is not the full correction either", cols[2][2], 1.0)


# --------------------------------------------------------------------------
# (e) matte finesse
# --------------------------------------------------------------------------

def test_finesse_defaults_add_no_filters(ctx):
    """Every finesse control at its default emits nothing at all."""
    info = H.info_for(CLIP)
    ctx.expect_eq("no filters at the defaults",
                  cg.finesse_filters(None, info), [])
    ctx.expect_eq("nor when they are spelled out",
                  cg.finesse_filters(dict(cg.MASK_FINESSE_DEFAULTS), info), [])
    layer = _layer([_win(LEFT)])
    with_block = _layer([_win(LEFT)], mask={"finesse": {"blur": 0.0, "grow": 0.0}})
    ctx.expect_eq("and the graph text is the same either way",
                  cg.graph_with_mask(_cfg(layer), info),
                  cg.graph_with_mask(_cfg(with_block), info))


def test_finesse_grow_and_shrink_move_the_edge(ctx):
    """grow dilates the matte, a negative grow erodes it, in frame widths.

    Two claims, because there are two regimes (round 1 minor 31). Under the
    cap a grow moves the edge by exactly the fraction of the width it asked
    for. Over the cap the cap decides, and since the cap became a fraction of
    the width rather than a flat 32 passes at whatever width each side happens
    to run at, "over the cap" now means the same ASK at every width, which is
    what makes the browser preview and this engine agree.
    """
    base = _layer([_win(LEFT)])
    img = H.render(CLIP, _cfg(base), T)
    h, w, _ = img.shape
    areas = {}
    for grow in (-0.05, 0.0, 0.01, 0.05):
        layer = _layer([_win(LEFT)], mask={"finesse": {"grow": grow}})
        ref = _reference(layer, img)
        areas[grow] = float(ref.mean())
        got = H.render(CLIP, _cfg(layer), T)
        rendered = np.abs(got.astype(np.int16)
                          - H.render(CLIP, _cfg_plain(), T).astype(np.int16)
                          ).max(axis=2) > 2
        ctx.note(f"grow {grow:+.2f}: reference selects {areas[grow] * 100:.2f}%, "
                 f"the render moves {rendered.mean() * 100:.2f}% of pixels")
    ctx.expect_gt("a positive grow selects more", areas[0.05], areas[0.0])
    ctx.expect_lt("a negative grow selects less", areas[-0.05], areas[0.0])
    cap = cg.mask_grow_passes(1.0, w)          # the most this width allows
    small, big = cg.mask_grow_passes(0.01, w), cg.mask_grow_passes(0.05, w)
    ctx.note(f"width {w}: cap {cap} passes, 1% asks {int(round(0.01 * w))} "
             f"and gets {small}, 5% asks {int(round(0.05 * w))} and gets {big}")
    ctx.expect_eq("a 1% grow at this width is under the cap and is granted "
                  "in full", small, int(round(0.01 * w)))
    ctx.expect_close("and it moves the edge by the pixels it asked for",
                     (areas[0.01] - areas[0.0]) * w, float(small), 1.5)
    ctx.expect_lt("a 5% grow at this width is over the cap", big,
                  int(round(0.05 * w)))
    ctx.expect_close("and then the edge moves by the cap, not by the ask",
                     (areas[0.05] - areas[0.0]) * w, float(big), 1.5)
    ctx.expect_close("the cap is the same fraction of the frame at any width "
                     "(that is the whole point of it: preview and render run "
                     "at different widths)",
                     cg.mask_grow_passes(1.0, 1920) / 1920.0, cap / float(w),
                     0.002)


def test_finesse_clean_black_and_white_are_the_identity_at_zero(ctx):
    """The knee is continuous: zero cleaning must not bend the matte.

    A curve that jumped to a smoothstep the moment either control left zero
    would move every soft edge in a grade the first time somebody nudged it,
    which is why clean_curve blends the knee in by how much was asked for.
    """
    v = np.linspace(0.0, 1.0, 101)
    ident = cg.clean_curve(v, 0.0, 0.0)
    ctx.expect_close("clean 0/0 is the identity",
                     float(np.abs(ident - v).max()), 0.0, 1e-9)
    tiny = cg.clean_curve(v, 0.01, 0.0)
    ctx.note(f"a 1% clean_black moves the curve by at most "
             f"{float(np.abs(tiny - v).max()):.4f}")
    ctx.expect_lt("a small clean is a small move", float(np.abs(tiny - v).max()),
                  0.05)
    hard = cg.clean_curve(v, 0.3, 0.3)
    ctx.expect_close("below clean_black the matte is 0", float(hard[20]), 0.0, 1e-9)
    ctx.expect_close("above clean_white it is 1", float(hard[80]), 1.0, 1e-9)


def test_finesse_clean_runs_in_the_graph(ctx):
    """The geq knee is applied by ffmpeg the way clean_curve says it is.

    Rendered through a feathered component so there is a real ramp to clean,
    then read back through the matte view, which is the only way to see the
    engine's own matte rather than infer it from a picture.
    """
    soft = _layer([_win(LEFT, feather=0.06)], mask={"show": True})
    cleaned = _layer([_win(LEFT, feather=0.06)],
                     mask={"show": True,
                           "finesse": {"clean_black": 0.3, "clean_white": 0.3}})
    a = H.luma(H.render(CLIP, _cfg(soft), T))
    b = H.luma(H.render(CLIP, _cfg(cleaned), T))
    mid_a = float(((a > 0.02) & (a < 0.98)).mean())
    mid_b = float(((b > 0.02) & (b < 0.98)).mean())
    ctx.note(f"partial matte pixels: {mid_a * 100:.2f}% before cleaning, "
             f"{mid_b * 100:.2f}% after")
    ctx.expect_lt("cleaning pushes the ramp towards 0 and 1", mid_b, mid_a)
    ctx.expect_close("full white stays full white", float(b.max()), 1.0, 0.01)
    ctx.expect_close("full black stays full black", float(b.min()), 0.0, 0.01)


def test_finesse_blur_softens_the_whole_matte(ctx):
    hard = _layer([_win(LEFT)], mask={"show": True})
    blurred = _layer([_win(LEFT)], mask={"show": True,
                                         "finesse": {"blur": 0.02}})
    a = H.luma(H.render(CLIP, _cfg(hard), T))
    b = H.luma(H.render(CLIP, _cfg(blurred), T))
    mid_a = float(((a > 0.02) & (a < 0.98)).mean())
    mid_b = float(((b > 0.02) & (b < 0.98)).mean())
    ctx.note(f"partial matte pixels: {mid_a * 100:.2f}% hard, "
             f"{mid_b * 100:.2f}% blurred")
    ctx.expect_lt("a hard matte has almost no ramp", mid_a, 0.02)
    ctx.expect_gt("blur gives it one", mid_b, 0.04)


# --------------------------------------------------------------------------
# (f) the matte store in the graph
# --------------------------------------------------------------------------

def test_matte_component_gates_the_layer(ctx):
    """A matte read from disk selects exactly the pixels it is white in.

    The fixture is a quarter width band at 64x36; the render is 320 wide, so
    this also proves the scale to the output. The assertions are made away
    from the edge, because scaling a matte up is a soft edge by design (C1)
    and asserting on the ramp would be asserting on the scaler.
    """
    matte = make_matte("m_band", {48: band(64, 36, 0, 16)}, total=1, state="done")
    layer = _layer([{"type": "matte", "op": "add", "matte": {"id": matte}}])
    got = H.render(CLIP, _cfg(layer), T)
    plain = H.render(CLIP, _cfg_plain(), T)
    full = H.render(CLIP, _cfg_full(), T)
    h, w, _ = got.shape
    inside = np.zeros((h, w), bool)
    inside[:, :int(w * 0.24)] = True
    outside = np.zeros((h, w), bool)
    outside[:, int(w * 0.26):] = True
    ctx.note(f"matte 64x36 scaled to {w}x{h}; band covers "
             f"{inside.mean() * 100:.0f}% of the frame")
    ctx.expect_close("inside the band the correction is applied in full",
                     _mean_abs(got, full, inside), 0.0, 0.0)
    ctx.expect_close("outside it nothing is applied",
                     _mean_abs(got, plain, outside), 0.0, 0.0)


def test_matte_component_follows_the_clip(ctx):
    """The engine's half of acceptance A1: the matte moves with the picture.

    The fixture's band steps right by four matte pixels a frame. Three frames
    are rendered from one command and the graded region has to step with
    them, which is what proves the sequence input is aligned to the render's
    own start frame and read at the matte's fps rather than held static.
    """
    frames = {48 + i: band(64, 36, 4 + i * 8, 20 + i * 8) for i in range(6)}
    matte = make_matte("m_moving", frames, total=64, state="done")
    layer = _layer([{"type": "matte", "op": "add", "matte": {"id": matte}}])
    shots = _render_frames(_cfg(layer), 3)
    # The ungraded frames of the SAME moments. Comparing every frame against
    # one still would report the camera's own movement as the matte's.
    plains = _render_frames(_cfg_plain(), 3)
    centres = []
    for i, img in enumerate(shots):
        moved = np.abs(img.astype(np.int16)
                       - plains[i].astype(np.int16)).max(axis=2) > 2
        xs = np.where(moved.any(axis=0))[0]
        centres.append(float(xs.mean()) if xs.size else float("nan"))
    ctx.note(f"the graded band's centre column by frame: "
             f"{[round(c, 1) for c in centres]} of {shots[0].shape[1]}")
    ctx.expect_gt("the matte moves between frame 0 and 1", centres[1], centres[0] + 5)
    ctx.expect_gt("and again between 1 and 2", centres[2], centres[1] + 5)


def test_matte_input_is_aligned_to_the_render_start(ctx):
    """-start_number is round(seek * the matte's own fps), not the render's."""
    matte = make_matte("m_align", {i: band(64, 36, 0, 8) for i in range(0, 60)},
                       total=60, fps=24.0)
    layer = _layer([{"type": "matte", "op": "add", "matte": {"id": matte}}])
    info = H.info_for(CLIP)
    for seek, want in ((0.0, "0"), (1.0, "24"), (2.0, "48"), (2.02, "48")):
        args = cg.ffmpeg_inputs(str(CLIP), _cfg(layer), info, seek=seek)
        idx = args.index("-start_number")
        ctx.expect_eq(f"seek {seek}s reads matte frame {want}",
                      args[idx + 1], want)
    ctx.note(f"input tail: {' '.join(args[-6:])}")
    ctx.expect_true("read at the matte's own fps", "-framerate" in args,
                    f"{args[args.index('-framerate') + 1]} fps")


def test_partial_matte_holds_the_nearest_written_frame(ctx):
    """A frame the tracker has not reached yet reuses the last one it did.

    C1: "A missing or partial matte gives a warnings entry on the frame
    response and the nearest available frame". Here the render seeks to a
    moment the matte has no frame for, and the picture still comes back
    gated by the frame the tracker did write, with a warning that names the
    matte and both frame numbers.
    """
    matte = make_matte("m_partial", {10: band(64, 36, 0, 16)}, total=120,
                       state="partial")
    layer = _layer([{"type": "matte", "op": "add", "matte": {"id": matte}}])
    info = H.info_for(CLIP)
    args = cg.ffmpeg_inputs(str(CLIP), _cfg(layer), info, seek=T)
    ctx.expect_true("the input is the nearest written frame, held",
                    args[-1].endswith("000010.png"), args[-1])
    warns = cg.mask_warnings(_cfg(layer), info, seek=T)
    ctx.note(f"{len(warns)} warning(s): "
             + "; ".join(w["message"] for w in warns))
    ctx.expect_eq("one warning, on the right layer", len(warns), 1)
    ctx.expect_eq("named as partial", warns[0]["kind"], "partial")
    ctx.expect_eq("with the layer index", warns[0]["layer"], 0)
    ctx.expect_eq("and the component index", warns[0]["component"], 0)
    got = H.render(CLIP, _cfg(layer), T)
    full = H.render(CLIP, _cfg_full(), T)
    h, w, _ = got.shape
    inside = np.zeros((h, w), bool)
    inside[:, :int(w * 0.24)] = True
    ctx.expect_close("and the held frame really gates the render",
                     _mean_abs(got, full, inside), 0.0, 0.0)


def test_missing_matte_renders_black_and_warns(ctx):
    """An id that names nothing must not stop a preview.

    A grade is often opened while its tracks are still queued, so a component
    whose matte is not there yet contributes nothing and says so. The refusal
    belongs to the render, not to the preview, which is the next test.
    """
    info = H.info_for(CLIP)
    for name, ref in (("no id yet", {"id": ""}),
                      ("an id that is not in the store", {"id": "m_nope"})):
        layer = _layer([{"type": "matte", "op": "add", "matte": ref}])
        cfg = _cfg(layer)
        args = cg.ffmpeg_inputs(str(CLIP), cfg, info, seek=T)
        ctx.expect_true(f"{name}: a flat still stands in",
                        args[-1].endswith(".png"), args[-1])
        warns = cg.mask_warnings(cfg, info, seek=T)
        ctx.expect_eq(f"{name}: one warning", len(warns), 1)
        ctx.expect_eq(f"{name}: kind", warns[0]["kind"], "missing")
        got = H.render(CLIP, cfg, T)
        plain = H.render(CLIP, _cfg_plain(), T)
        ctx.expect_eq(f"{name}: the frame is the ungraded frame",
                      hashlib.sha1(got.tobytes()).hexdigest()[:12],
                      hashlib.sha1(plain.tobytes()).hexdigest()[:12])
    ctx.note("a preview of a grade whose track is still queued renders, and "
             "the warnings say which matte is not there")


def test_render_refuses_a_partial_matte(ctx):
    """strict_mattes is the allow_partial gate, and it names the layer.

    "An agent cannot ship a clip whose subject hold ran out at frame 200
    without saying so." The refusal has to carry the layer, the component and
    the matte id, because a grade with six layers and one bad track is
    otherwise a hunt.
    """
    matte = make_matte("m_short", {i: band(64, 36, 0, 16) for i in range(0, 55)},
                       total=240, state="running")
    layer = _layer([{"type": "matte", "op": "add", "matte": {"id": matte}}])
    cfg = _cfg(layer)
    info = H.info_for(CLIP)
    ok = cg.ffmpeg_inputs(str(CLIP), cfg, info, seek=T)
    ctx.expect_true("a preview still builds its inputs", bool(ok), f"{len(ok)} args")
    try:
        cg.ffmpeg_inputs(str(CLIP), cfg, info, seek=T, strict_mattes=True)
        ctx.check(False, "a render on a partial matte was allowed")
    except cg.GradeError as exc:
        msg = str(exc)
        ctx.note(f"refused with: {msg.splitlines()[1].strip()}")
        ctx.expect_true("the message names the layer", "layer 0" in msg, msg[:80])
        ctx.expect_true("and the matte", matte in msg, msg[:80])
        ctx.expect_true("and says how to proceed", "allow_partial" in msg, msg[:80])
    done = make_matte("m_done", {i: band(64, 36, 0, 16) for i in range(0, 60)},
                      total=60, state="done")
    good = _cfg(_layer([{"type": "matte", "op": "add", "matte": {"id": done}}]))
    ctx.expect_eq("a complete matte raises nothing", cg.mask_warnings(good, info), [])
    cg.ffmpeg_inputs(str(CLIP), good, info, seek=0.0, strict_mattes=True)
    ctx.check(True, "and a strict render of it builds")


def test_matte_intersect_key_is_the_skin_recipe(ctx):
    """The combination the anchored grading framework asked for.

    A tracked matte for the subject, intersected with a colour key for skin,
    is the mask an exposure hold rides on. Worth a test of its own because it
    is the one stack that mixes a disk matte with a picture derived matte,
    which is where the two halves of the graph meet.
    """
    matte = make_matte("m_subject", {48: band(64, 36, 0, 32)}, total=1)
    key = {"enabled": True, "hue_center": 25.0, "hue_width": 60.0,
           "hue_soft": 20.0, "sat_low": 0.1, "sat_soft": 0.2}
    layer = _layer([{"type": "matte", "op": "add", "matte": {"id": matte}},
                    {"type": "key", "op": "intersect", "key": key}])
    got = H.render(CLIP, _cfg(layer), T)
    plain = H.render(CLIP, _cfg_plain(), T)
    h, w, _ = got.shape
    moved = np.abs(got.astype(np.int16) - plain.astype(np.int16)).max(axis=2) > 2
    right = moved[:, int(w * 0.55):]
    matte_only = _layer([{"type": "matte", "op": "add",
                          "matte": {"id": matte}}])
    matte_alone = np.abs(H.render(CLIP, _cfg(matte_only), T).astype(np.int16)
                         - plain.astype(np.int16)).max(axis=2) > 2
    key_only = _layer([{"type": "key", "op": "add", "key": key}])
    only = np.abs(H.render(CLIP, _cfg(key_only), T).astype(np.int16)
                  - plain.astype(np.int16)).max(axis=2) > 2
    ctx.note(f"the intersection moves {moved.mean() * 100:.2f}% of the frame, "
             f"{float(right.mean()) * 100:.3f}% of it outside the matte; the "
             f"matte alone moves {matte_alone.mean() * 100:.2f}% and the key "
             f"alone {only.mean() * 100:.2f}%")
    # Round 1 finding 42: this used to be `> 0.0`, which one non-zero pixel
    # out of 181,760 satisfies. The floor is a real region of the picture
    # (measured on this footage: 38.65% of the frame, of a matte that covers
    # half of it), so a stack that selected almost nothing now fails instead
    # of passing on a stray pixel.
    ctx.expect_gt("something is really selected, not one stray pixel",
                  float(moved.mean()), 0.10)
    ctx.expect_close("nothing outside the matte is touched",
                     float(right.mean()), 0.0, 0.0)
    # Both halves of "intersect": smaller than each of the two components on
    # its own. Without the second of these, a key that was silently ignored
    # (the intersection collapsing to the matte) still passed.
    ctx.expect_lt("and the intersection is smaller than the key alone",
                  float(moved.mean()), float(only.mean()))
    ctx.expect_lt("and smaller than the matte alone: the key really narrowed "
                  "it, it did not just fold to the matte",
                  float(moved.mean()), float(matte_alone.mean()))


# --------------------------------------------------------------------------
# (g) the matte view and the input map
# --------------------------------------------------------------------------

def test_show_renders_the_stack_as_a_matte(ctx):
    """mask.show on a stack draws the matte itself, in greyscale.

    Not the colour matte times the window, which is what the legacy matte
    view is: with components the mask IS the stack, so the view is the stack
    at full swing, black where nothing is selected and white where all of it
    is.
    """
    layer = _layer([_win(LEFT), _win(TOP, op="intersect")], mask={"show": True})
    img = H.render(CLIP, _cfg(layer), T)
    spread = int(np.abs(img.astype(np.int16) - img[..., :1].astype(np.int16)).max())
    lum = H.luma(img)
    ref = _reference(_layer([_win(LEFT), _win(TOP, op="intersect")]), img)
    err = float(np.abs(lum - ref).max())
    ctx.note(f"matte view: channel spread {spread} of 255, worst disagreement "
             f"with the reference matte {err:.4f}")
    ctx.expect_le("the matte view is greyscale", float(spread), 1.0)
    ctx.expect_le("and it is the reference matte", err, 0.01)


def test_input_indices_stay_in_step_with_the_inputs(ctx):
    """The index map and the input list must be one walk, not two.

    A wrong index does not raise: ffmpeg reads a different input and renders
    a wrong picture in silence, which is the failure this map exists to
    prevent. The check is that every generated still lands where the map
    says, with the radial ramp first, the mask stills in array order and the
    grain plate last.
    """
    matte = make_matte("m_idx", {48: band(64, 36, 0, 16)}, total=1)
    cfg = H.patch(H.defaults(), {
        "fx": {"radial_blur": {"enabled": True}},
        "grain": {"enabled": True},
        "layers": [
            _layer([_win(LEFT), {"type": "key", "op": "intersect",
                                 "key": {"enabled": True}},
                    {"type": "matte", "op": "add", "matte": {"id": matte}}]),
            cg.deep_merge(cg.LAYER_DEFAULTS,
                          {"mask": {"window": {"enabled": True}},
                           "correct": STRONG}),
        ]})
    info = H.info_for(CLIP)
    idxs = cg.mask_input_indices(cfg)
    args = cg.ffmpeg_inputs(str(CLIP), cfg, info, seek=T)
    inputs = [args[i + 1] for i, a in enumerate(args) if a == "-i"]
    ctx.note(f"{len(inputs)} inputs, map: radial {idxs.get('radial')}, "
             f"components {idxs['components']}, legacy windows {idxs['layers']}, "
             f"grain {idxs['grain']}")
    ctx.expect_eq("the radial ramp is input 1", idxs.get("radial"), 1)
    ctx.expect_eq("the stack's window component is input 2",
                  idxs["components"][0][0], 2)
    ctx.expect_true("the key component takes no input",
                    1 not in idxs["components"][0], str(idxs["components"][0]))
    ctx.expect_eq("its matte component is input 3", idxs["components"][0][2], 3)
    ctx.expect_eq("the legacy layer's window is input 4", idxs["layers"][1], 4)
    ctx.expect_eq("the grain plate is last", idxs["grain"], 5)
    # inputs[0] is the clip itself, so an input INDEX addresses this list
    # directly: that is what "input 3" means to the filter graph.
    ctx.expect_true("the matte input really is the matte",
                    "m_idx" in inputs[idxs["components"][0][2]],
                    inputs[idxs["components"][0][2]])
    graph = cg.graph_with_mask(cfg, info)
    for label in ("cm0_0", "cm0_2", "lw0", "lw1"):
        ctx.expect_true(f"the graph carries [{label}]", f"[{label}]" in graph,
                        "present")
    got = H.render(CLIP, cfg, T)
    ctx.expect_true("and the whole thing renders", got.shape[2] == 3,
                    f"{got.shape[1]}x{got.shape[0]}")


def test_two_stacks_on_two_layers(ctx):
    """Two component layers in one grade, at both placements.

    Labels are built from the layer's array index, so two stacks that both
    have a component 0 must not collide, and a stack before the look and a
    stack after it must both find their own matte.
    """
    layer_a = _layer([_win(LEFT)], correct={"lum_gain": 0.3})
    layer_b = _layer([_win(TOP)], correct={"lum_gain": 1.9},
                     placement="after_look")
    cfg = H.patch(H.defaults(), {"layers": [layer_a, layer_b]})
    got = H.render(CLIP, cfg, T)
    plain = H.render(CLIP, _cfg_plain(), T)
    h, w, _ = got.shape
    dark = float(H.luma(got)[int(h * .8), :int(w * .4)].mean())
    dark0 = float(H.luma(plain)[int(h * .8), :int(w * .4)].mean())
    bright = float(H.luma(got)[:int(h * .2), int(w * .6):].mean())
    bright0 = float(H.luma(plain)[:int(h * .2), int(w * .6):].mean())
    ctx.note(f"bottom left {dark0:.4f} -> {dark:.4f}, "
             f"top right {bright0:.4f} -> {bright:.4f}")
    ctx.expect_lt("the first stack darkens its own quarter", dark, dark0)
    ctx.expect_gt("the second brightens its own", bright, bright0)


# --------------------------------------------------------------------------
# (h) grade/mattes.py itself
# --------------------------------------------------------------------------

def test_matte_reader_reads_the_store(ctx):
    """resolve, frame_index, frame_path, nearest_written, load_frame (C6)."""
    frames = {i: band(64, 36, i, i + 8) for i in list(range(0, 5)) + [9]}
    name = make_matte("m_reader", frames, total=12, fps=24.0, state="partial")
    info = MT.resolve(MT.matte_root(), name)
    ctx.expect_eq("the id round trips", info.matte_id, name)
    ctx.expect_eq("the index is read", (info.fps, info.frames, info.width),
                  (24.0, 12, 64))
    ctx.expect_eq("every written frame is found", list(info.written_indices()),
                  [0, 1, 2, 3, 4, 9])
    ctx.expect_true("and it knows it is partial", info.is_partial, info.state)
    ctx.expect_eq("frame_index rounds", MT.frame_index(info, 0.1), 2)
    ctx.expect_eq("and clamps to the last frame", MT.frame_index(info, 99.0), 11)
    ctx.expect_true("an unwritten frame has no path",
                    MT.frame_path(info, 7) is None, "None")
    ctx.expect_eq("nearest_written picks the closer side",
                  MT.nearest_written(info, 7), 9)
    ctx.expect_eq("and leans back on a tie", MT.nearest_written(info, 6), 4)
    arr = MT.load_frame(info, 2)
    ctx.expect_eq("a frame loads at its own size", arr.shape, (36, 64))
    ctx.expect_between("as float 0..1", float(arr.max()), 1.0, 1.0, 1e-6)
    big = MT.load_frame(info, 2, size=(128, 72))
    ctx.expect_eq("and resamples on request", big.shape, (72, 128))
    _a, served, warn = MT.load_time(info, 7 / 24.0)
    ctx.expect_eq("load_time falls back to the nearest frame", served, 9)
    ctx.expect_true("and says so", bool(warn) and name in warn, str(warn))
    try:
        MT.resolve(MT.matte_root(), "m_not_here")
        ctx.check(False, "a missing matte resolved")
    except MT.MatteMissing as exc:
        ctx.check(True, f"a missing matte raises MatteMissing: {str(exc)[:50]}")


def test_quality_flags_the_frames_that_went_wrong(ctx):
    """Round 1 finding 15: `quality()`, the function REPORT.md section 8 opens
    with, had no test anywhere that could fail.

    Every assertion written against it in the arc ran on a fixture with no
    `areas` and no `ious` at all, which forces `suspect_count: 0`, all three
    reason counts 0 and `iou_source: "none"`; the assertions then checked that
    a key was present, that a value was one of the three values that exist,
    and that 0 == 0 + 0 + 0. A function returning those constants passed.

    So: hand built arrays with answers worked out on paper. The story is the
    grader's own face matte (checkpoint gap 18): a small steady area, the
    subject lost at frame 2, then a latch onto something thirteen times
    bigger at frame 4 that also barely overlaps what came before it.

      frame  area   why
      0      0.05   the subject
      1      0.05   steady
      2      0.00   lost:            zero_area, and area_jump too, because a
                                     drop to nothing is a 100% change and 1.0
                                     is over the 0.5 threshold
      3      0.05   back:            area_recover, and only that: the jump
                                     rule needs a non zero frame BEFORE it to
                                     divide by, so it cannot see this frame
      4      0.65   the tree:        area_jump (12.0 against 0.5) and
                                     low_iou (0.05 against 0.3)
      5      0.66   steady on it

    Three suspect FRAMES (2, 3 and 4) and five suspect REASONS across them:
    the difference between those two numbers is the thing the route test's
    `suspect_count == zero + jump + iou - overlap` arithmetic was standing in
    for, and here both are stated outright.

    Frames 2 and 3 are the pair worth reading twice. Losing the subject is
    counted under two reasons at once, and coming back under one reason of its
    own (`area_recover`, round 1 finding 22) rather than under none: the area
    rule divides by the previous area and 0 is not a base to measure a change
    against, so before that reason existed the single frame most worth looking
    at, the one where a tracker comes back on the wrong object, was the one
    frame nothing flagged. That asymmetry is the real behaviour of the shipped
    function; it is written down here so that a future change to it fails this
    test instead of quietly changing what the studio reports.
    """
    frames = {i: band(64, 36, 0, 8 + i) for i in range(6)}
    areas = [0.05, 0.05, 0.0, 0.05, 0.65, 0.66]
    ious = [None, 0.9, 0.85, 0.88, 0.05, 0.91]
    name = make_matte("m_drift", frames, total=6, fps=24.0,
                      areas=areas, ious=ious)
    info = MT.resolve(MT.matte_root(), name)
    q = MT.quality(info)
    ctx.note(f"suspect frames {[f['index'] for f in q['suspect_frames']]}, "
             f"reasons {q['reasons']}, iou_source {q['iou_source']}")
    ctx.expect_eq("every written frame is judged", q["checked"], 6)
    ctx.expect_eq("the ious came from the index the service wrote",
                  q["iou_source"], "index")
    ctx.expect_eq("three frames are suspect, not none and not all six",
                  q["suspect_count"], 3)
    ctx.expect_eq("and they are the three that went wrong",
                  [f["index"] for f in q["suspect_frames"]], [2, 3, 4])
    ctx.expect_eq("the reason counts name what went wrong on each",
                  q["reasons"], {"zero_area": 1, "area_jump": 2,
                                 "area_recover": 1, "low_iou": 1})
    lost, back, tree = q["suspect_frames"]
    ctx.expect_eq("the lost frame is flagged for its area being zero, and for "
                  "the drop that got it there", lost["reasons"],
                  ["zero_area", "area_jump"])
    ctx.expect_close("that drop being the whole of the previous area",
                     float(lost["jump"]), 1.0, 1e-6)
    ctx.expect_eq("the frame the subject comes back on is flagged as a "
                  "recovery, which is the frame the area rule is blind to",
                  back["reasons"], ["area_recover"])
    ctx.expect_true("with no jump reported on it, because there is no non "
                    "zero area before it to divide by",
                    back["jump"] is None, back["jump"])
    ctx.expect_close("and the area it came back at", float(back["area"]),
                     0.05, 1e-6)
    ctx.expect_eq("the latch is flagged for BOTH the size change and the "
                  "shape moving", tree["reasons"], ["area_jump", "low_iou"])
    ctx.expect_close("and the jump is reported as a fraction of the frame "
                     "before it", float(tree["jump"]), 12.0, 1e-6)
    ctx.expect_close("with the overlap that went with it", float(tree["iou"]),
                     0.05, 1e-6)
    ctx.expect_eq("the first suspect frame is named for a caller that only "
                  "wants the headline", q["first_suspect_index"], 2)
    ctx.expect_close("with its time in seconds",
                     float(q["first_suspect_time"]), 2 / 24.0, 1e-4)

    # The thresholds really are the thresholds: each rule can be turned off
    # by moving its own number past the data, and nothing else moves.
    loose = MT.quality(info, area_jump=20.0, min_iou=0.001)
    ctx.expect_eq("with both thresholds moved past the data, the empty frame "
                  "and the frame after it are what is left",
                  [f["index"] for f in loose["suspect_frames"]], [2, 3])
    ctx.expect_eq("and they are left for the two reasons a threshold cannot "
                  "turn off: an empty matte is empty at any threshold, and a "
                  "subject that comes back came back",
                  loose["reasons"], {"zero_area": 1, "area_jump": 0,
                                     "area_recover": 1, "low_iou": 0})
    ctx.expect_eq("and the thresholds in force come back with the report",
                  loose["thresholds"], {"area_jump": 20.0, "area_recover": 0.0,
                                        "min_iou": 0.001})
    tight = MT.quality(info, area_jump=0.005, min_iou=0.95)
    ctx.expect_eq("and with both tightened past every frame, every frame "
                  "after the first is suspect",
                  tight["suspect_count"], 5)

    # `limit` caps the list and nothing else: the counts stay true, and the
    # report says it was capped. A route that lists a clip's mattes relies on
    # this, and "truncated" is how a reader knows not to trust the list length.
    capped = MT.quality(info, limit=1)
    ctx.expect_eq("limit caps the list", len(capped["suspect_frames"]), 1)
    ctx.expect_eq("but not the count", capped["suspect_count"], 3)
    ctx.expect_true("and says it was capped", capped["truncated"], "truncated")

    # A clean matte reports zero, so the flags mean something when they are
    # absent as well as when they are present.
    clean = make_matte("m_clean", frames, total=6, fps=24.0,
                       areas=[0.05, 0.051, 0.052, 0.053, 0.054, 0.055],
                       ious=[None, 0.95, 0.94, 0.96, 0.95, 0.93])
    qc = MT.quality(MT.resolve(MT.matte_root(), clean))
    ctx.expect_eq("a clean track reports nothing suspect", qc["suspect_count"], 0)
    ctx.expect_eq("with every reason at zero", qc["reasons"],
                  {"zero_area": 0, "area_jump": 0, "area_recover": 0,
                   "low_iou": 0})
    ctx.expect_eq("and it still says where its ious came from",
                  qc["iou_source"], "index")

    # And the honest "this rule did not run" case: the same drift, no ious in
    # the index. `low_iou` is 0 because the rule could not run, and
    # `iou_source` is the only thing that says so. Reading the count without
    # the source is finding 15 in one line.
    no_iou = make_matte("m_drift_no_iou", frames, total=6, fps=24.0,
                        areas=areas)
    qn = MT.quality(MT.resolve(MT.matte_root(), no_iou))
    ctx.expect_eq("with no ious written, the shape rule does not run",
                  qn["reasons"]["low_iou"], 0)
    ctx.expect_eq("so the tree frame is flagged on its size alone",
                  qn["suspect_frames"][-1]["reasons"], ["area_jump"])
    ctx.expect_eq("and the report says so rather than implying a clean shape",
                  qn["iou_source"], "none")
    ctx.expect_eq("the area rules still run", qn["suspect_count"], 3)


def test_frame_ious_reads_the_shapes_off_disk(ctx):
    """The fallback for a matte tracked before the service wrote `ious`.

    Round 1 finding 15's second half: `frame_ious()` is reachable only through
    `quality(..., compute_iou=True)`, which nothing in the product passes, so
    it was dead code with a docstring naming a caller it did not have. It is
    kept because it is the only way to judge drift on an older matte, and it
    is tested here directly, on overlaps computed on paper:

      frames 0 and 1  the same band            iou 1.0
      frame 2         a disjoint band          iou 0.0
      frame 3         half overlapping frame 2 iou 1/3

    1/3 is deliberately just ABOVE the 0.3 threshold, so the boundary is
    pinned in the direction that matters: a frame that moved a bit is not
    called drift.
    """
    frames = {0: band(64, 36, 0, 32), 1: band(64, 36, 0, 32),
              2: band(64, 36, 32, 64), 3: band(64, 36, 16, 48)}
    name = make_matte("m_shapes", frames, total=4, fps=24.0,
                      areas=[0.5, 0.5, 0.5, 0.5])
    info = MT.resolve(MT.matte_root(), name)
    got = MT.frame_ious(info)
    ctx.note(f"ious off disk: {got}")
    ctx.expect_true("the first written frame has nothing to compare against, "
                    "so it is absent rather than 1.0", 0 not in got,
                    sorted(got))
    ctx.expect_close("an identical shape overlaps itself completely",
                     float(got[1]), 1.0, 0.02)
    ctx.expect_close("a shape that moved somewhere else overlaps nothing",
                     float(got[2]), 0.0, 0.02)
    ctx.expect_close("and a half overlap is a third: intersection over UNION, "
                     "not over either one of them", float(got[3]), 1 / 3, 0.03)

    # Through quality(), which is how a caller would ever see these.
    q = MT.quality(info, compute_iou=True)
    ctx.expect_eq("quality(compute_iou=True) says the numbers came from the "
                  "frames, not the index", q["iou_source"], "frames")
    ctx.expect_eq("the frame that moved away is flagged, and only it",
                  [f["index"] for f in q["suspect_frames"]], [2])
    ctx.expect_eq("for the shape, not for its size", q["reasons"],
                  {"zero_area": 0, "area_jump": 0, "area_recover": 0,
                   "low_iou": 1})
    ctx.expect_eq("and with the fallback switched off (the default), the rule "
                  "does not run at all",
                  MT.quality(info)["iou_source"], "none")


def test_matte_reader_has_no_dependencies(ctx):
    """The zlib PNG reader and PIL agree, byte for byte.

    grade/mattes.py is meant to work inside a venv that has numpy and nothing
    else (the SAM service's own environment), so the fallback decoder is not
    decoration and gets asserted rather than assumed.
    """
    rng = np.random.default_rng(7)
    arr = rng.integers(0, 256, (23, 41), dtype=np.uint8)
    path = H.WORK / "png_roundtrip.png"
    MT.write_gray_png(path, arr)
    zlib_read = MT.decode_png(path.read_bytes())
    pil_read = MT.read_gray_png(path)
    ctx.expect_true("the written PNG reads back unchanged",
                    bool(np.array_equal(zlib_read, arr)),
                    f"max diff {int(np.abs(zlib_read.astype(int) - arr).max())}")
    ctx.expect_true("and both readers agree",
                    bool(np.array_equal(zlib_read, pil_read)), "identical")
    # A PIL written PNG uses adaptive row filters, which is the path the
    # service's own writer will take: decode it with the fallback too.
    try:
        from PIL import Image
        p2 = H.WORK / "png_filtered.png"
        Image.fromarray(arr).save(p2, optimize=True)
        ctx.expect_true("a filtered PNG decodes with the fallback reader too",
                        bool(np.array_equal(MT.decode_png(p2.read_bytes()), arr)),
                        "identical")
    except ImportError:
        ctx.note("PIL is not installed, the adaptive filter case was skipped")


def test_matte_resize_lands_where_the_scaler_lands(ctx):
    """mattes.resize_bilinear and ffmpeg's bilinear scale agree.

    The engine scales a matte with ffmpeg and the server measures one with
    numpy. If they disagreed, a hold measured at the working width would not
    be the hold the render applied.
    """
    a = band(64, 36, 0, 32).astype(np.float32) / 255.0
    up = MT.resize_bilinear(a, 320, 180)
    path = H.WORK / "resize_src.png"
    MT.write_gray_png(path, (a * 255).astype(np.uint8))
    raw = H._run_bytes(["ffmpeg", "-v", "error", "-i", str(path), "-vf",
                        "scale=320:180:flags=bilinear,format=gray",
                        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"])
    ff = np.frombuffer(raw[:320 * 180], np.uint8).reshape(180, 320) / 255.0
    d = float(np.abs(up - ff).max())
    mean = float(np.abs(up - ff).mean())
    ctx.note(f"worst disagreement {d:.3e}, mean {mean:.3e} of 1.0")
    ctx.expect_lt("the two scalers agree to well under a code value", mean, 0.004)
    # Round 1 finding 42: the mean was the only assertion, and this fixture is
    # one hard edge in a flat frame, so a disagreement confined to the edge
    # (a half pixel offset, which is exactly how a resampler bug looks and is
    # the one place the two engines are known to differ) averaged away to
    # nothing. Measured here: worst 7.2e-08, i.e. float rounding, against a
    # limit of one 8-bit code value.
    ctx.expect_lt("and the WORST pixel agrees too, not just the average",
                  d, 1.0 / 255.0)


# --------------------------------------------------------------------------

def test_radial_matte_names_carry_the_exact_numbers(ctx):
    """Round 2 finding 67: `radial_mask` named its cached file
    `radial_{w}x{h}_{start:.2f}_{end:.2f}.png`.

    `start` and `end` are free floats out of the config, so two radials that
    differ in the third decimal shared a file name, and the cache is a plain
    `if not p.exists()`: the second caller silently got the FIRST caller's
    ramp. That is a wrong picture with no warning anywhere, and gap 22's per
    run cache makes it more reachable rather than less, because which of the
    two wins now depends on the order one run happens to bake them in.

    `window_mask` already does this right (it hashes the exact block), so the
    check is that a radial behaves the same way: different numbers, different
    file, different pixels; the same numbers, the same file.
    """
    w, h = 96, 64
    a = H.cg.radial_mask(w, h, 0.301, 0.7)
    b = H.cg.radial_mask(w, h, 0.304, 0.7)
    again = H.cg.radial_mask(w, h, 0.301, 0.7)
    ctx.expect_true("0.301 and 0.304 do not share a cache file",
                    Path(a) != Path(b), f"{Path(a).name} vs {Path(b).name}")
    ctx.expect_eq("the same numbers still hit the same cache file",
                  str(again), str(a))
    for q in (a, b):
        ctx.expect_true(f"{Path(q).name} was written", Path(q).is_file(),
                        str(q))
    ramp_a = read_gray_png(a, w, h).astype(np.int16)
    ramp_b = read_gray_png(b, w, h).astype(np.int16)
    diff = int(np.abs(ramp_a - ramp_b).max())
    ctx.expect_gt("and the two ramps really are different pictures, so the "
                  "shared name was handing back the wrong one",
                  float(diff), 0.0)
    ctx.note(f"max difference between the two ramps: {diff} code values")
    # The name has to be stable across processes as well, or the cache never
    # hits: same inputs, same hash, no run counter or object id in it.
    ctx.expect_true("the name is derived from the numbers, not from the run",
                    Path(a).name.startswith(f"radial_{w}x{h}_"), Path(a).name)


def register(suite):
    g = "mask"
    suite.add(g, "legacy_parity", test_legacy_parity_against_the_pre_change_engine,
              doc="every legacy case matches the fingerprint blessed from the "
                  "pre-component engine: graph, inputs and every baked file")
    suite.add(g, "legacy_fold_collision", test_the_legacy_fold_cannot_lose_a_file,
              doc="two generated files that fold to one name stay two entries "
                  "in the fingerprint instead of one silently replacing the "
                  "other")
    suite.add(g, "empty_components_is_legacy",
              test_empty_components_list_is_the_legacy_mask,
              doc="components: [] renders the identical frame to no key at all")
    suite.add(g, "add_is_the_union", test_add_is_the_union,
              doc="add is max: the union, applied in full inside and not at all out")
    suite.add(g, "intersect_is_the_product", test_intersect_is_the_product,
              doc="intersect is a product: the overlap only")
    suite.add(g, "subtract_removes_the_second", test_subtract_removes_the_second,
              doc="subtract is a product with the complement")
    suite.add(g, "stack_folds_from_zero", test_the_stack_folds_from_zero,
              doc="a stack whose first component is not an add selects nothing")
    suite.add(g, "empty_stack_refuses", test_an_empty_stack_refuses_to_build_a_graph,
              doc="a stack that folds to nothing raises instead of naming a filter None")
    suite.add(g, "component_invert", test_component_invert_flips_only_that_component,
              doc="a component's invert and the mask's invert are different masks")
    suite.add(g, "feather", test_feather_softens_one_component_only,
              doc="feather makes a ramp at the edge and leaves the interior exact")
    suite.add(g, "blur_cap", test_the_feather_and_blur_cap,
              doc="feather and finesse.blur are capped at a fraction of frame "
                  "width in all three implementations, and a value outside "
                  "[0, 1] is refused")
    suite.add(g, "component_cap", test_the_component_count_cap,
              doc="a mask stack may carry at most 32 components and one "
                  "request at most 128 folds (components plus gaussians, and "
                  "a legacy mask is not free), both refused with a sentence "
                  "rather than truncated")
    suite.add(g, "key_component", test_key_component_matches_the_qualifier,
              doc="a key component selects what the legacy qualifier selects")
    suite.add(g, "luma_component", test_luma_component_is_a_key_with_hue_and_sat_open,
              doc="luma selects by brightness alone, over every hue and saturation")
    suite.add(g, "linear_geq_matches_numpy", test_linear_window_geq_matches_numpy,
              doc="the linear gradient's baked matte equals its numpy reference")
    suite.add(g, "linear_direction", test_linear_window_points_where_the_angle_says,
              doc="angle 0 selects the top, 90 the right, clockwise on screen")
    suite.add(g, "linear_softness", test_linear_window_softness_is_the_curve,
              doc="softness eases the ramp, w sets how wide the transition is")
    suite.add(g, "linear_renders", test_linear_window_renders_as_a_gradient,
              doc="through the real graph the correction fades across the frame")
    suite.add(g, "finesse_defaults_are_free", test_finesse_defaults_add_no_filters,
              doc="every finesse control at its default emits no filter at all")
    suite.add(g, "finesse_grow", test_finesse_grow_and_shrink_move_the_edge,
              doc="grow dilates and a negative grow erodes, by the pixels asked for")
    suite.add(g, "finesse_clean_curve",
              test_finesse_clean_black_and_white_are_the_identity_at_zero,
              doc="the clean knee is the identity at zero and continuous after it")
    suite.add(g, "finesse_clean_renders", test_finesse_clean_runs_in_the_graph,
              doc="ffmpeg applies the knee, and full white stays full white")
    suite.add(g, "finesse_blur", test_finesse_blur_softens_the_whole_matte,
              doc="blur on the combined matte turns a hard edge into a ramp")
    suite.add(g, "matte_gates_the_layer", test_matte_component_gates_the_layer,
              doc="a matte from the store gates the correction and scales to the frame")
    suite.add(g, "matte_follows_the_clip", test_matte_component_follows_the_clip,
              doc="over three frames the graded region moves with the matte (A1)")
    suite.add(g, "matte_alignment", test_matte_input_is_aligned_to_the_render_start,
              doc="the sequence starts at round(seek * fps) of the matte's own fps")
    suite.add(g, "partial_matte_holds", test_partial_matte_holds_the_nearest_written_frame,
              doc="an untracked frame holds the nearest written one, with a warning")
    suite.add(g, "missing_matte_warns", test_missing_matte_renders_black_and_warns,
              doc="a matte that is not there renders nothing and says so")
    suite.add(g, "render_refuses_partial", test_render_refuses_a_partial_matte,
              doc="strict_mattes refuses, naming the layer, the component and the matte")
    suite.add(g, "matte_intersect_key", test_matte_intersect_key_is_the_skin_recipe,
              doc="a tracked matte intersected with a skin key, the hold recipe")
    suite.add(g, "show_is_the_stack", test_show_renders_the_stack_as_a_matte,
              doc="the matte view draws the folded stack in greyscale")
    suite.add(g, "input_indices", test_input_indices_stay_in_step_with_the_inputs,
              doc="every generated still lands on the index the map says it does")
    suite.add(g, "two_stacks", test_two_stacks_on_two_layers,
              doc="two component layers, one before the look and one after")
    suite.add(g, "matte_reader", test_matte_reader_reads_the_store,
              doc="grade/mattes.py: resolve, index, nearest written, load (C6)")
    suite.add(g, "matte_reader_standalone", test_matte_reader_has_no_dependencies,
              doc="the zlib PNG reader agrees with PIL on both writers' files")
    suite.add(g, "quality_flags_drift",
              test_quality_flags_the_frames_that_went_wrong,
              doc="quality() flags a lost subject, a 12x area jump and a "
                  "shape that moved, on hand built arrays with known answers")
    suite.add(g, "frame_ious_off_disk", test_frame_ious_reads_the_shapes_off_disk,
              doc="frame_ious() computes intersection over union off the "
                  "frames themselves, and quality(compute_iou=True) uses it")
    suite.add(g, "matte_resize", test_matte_resize_lands_where_the_scaler_lands,
              doc="the numpy resample and ffmpeg's bilinear scaler agree")
    suite.add(g, "radial_cache_name", test_radial_matte_names_carry_the_exact_numbers,
              doc="two radials three thousandths apart get two cache files, "
                  "not one shared ramp (round 2 finding 67)")
