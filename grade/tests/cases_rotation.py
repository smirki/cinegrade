"""Group: rotation as a setting, from probe through to the rendered pixels.

Contract C2. Rotation used to be one boolean, autorotate, which could only say
"honour the file's display matrix" or "ignore it". Every clip off the founder's
camera carries a wrong tag (a -90 display matrix on a shot that is already
upright), so neither answer is the right one on some other clip, and the fix
was a browser-side toggle that no agent and no CLI run could see.

Now there are five values: auto, 0, 90, 180 and 270. What this file pins:

  compatibility  auto and 0 emit the exact commands they emitted before the
                 setting existed (no transpose anywhere, -noautorotate on 0
                 only), and probe reports the same numbers the old boolean
                 did. The golden group covers the pixels; this covers the
                 commands, which is where a regression would start.
  geometry       90 and 270 swap the reported size, 180 does not.
  direction      90 turns the picture CLOCKWISE as it is seen on screen,
                 measured on a synthetic frame whose four corners are four
                 different brightnesses, so the check survives any grade
                 (every stage in the pipeline is monotonic in luma, so the
                 ranking of the four corners cannot change, only their
                 values).
"""

from __future__ import annotations

import numpy as np

import harness as H
from harness import cg

# CLIP_B is the clip the whole feature exists for: it carries a -90 display
# matrix on a landscape picture, so "auto" delivers it sideways.
TAGGED = H.CLIP_B


# --------------------------------------------------------------------------
# compatibility: auto and 0 are the pre-rotation paths
# --------------------------------------------------------------------------

def test_auto_and_zero_match_the_old_boolean(ctx):
    """probe(rotation=...) and probe(autorotate=...) must agree, field by field.

    The boolean is not deprecated: the browser still sends it on every
    request and every script written before this arc still passes it.
    """
    for mode, flag in (("auto", True), ("0", False)):
        new = cg.probe(str(TAGGED), rotation=mode)
        old = cg.probe(str(TAGGED), autorotate=flag)
        ctx.note(f"{mode}: {new['width']}x{new['height']}, "
                 f"tag {new['rotation']} degrees")
        ctx.expect_eq(f"rotation {mode!r} and autorotate {flag} give the "
                      f"same width", new["width"], old["width"])
        ctx.expect_eq(f"rotation {mode!r} and autorotate {flag} give the "
                      f"same height", new["height"], old["height"])
        ctx.expect_eq(f"rotation {mode!r} reports autorotate {flag}",
                      new["autorotate"], flag)
        ctx.expect_eq(f"rotation {mode!r} is what probe recorded",
                      new["rotate"], mode)

    ctx.expect_true("this clip really does carry a quarter-turn tag, so the "
                    "comparison above is not vacuous",
                    abs(cg.probe(str(TAGGED))["rotation"]) in (90, 270),
                    str(cg.probe(str(TAGGED))["rotation"]))


def test_auto_and_zero_emit_the_pre_rotation_commands(ctx):
    """No transpose, and -noautorotate exactly where it always was."""
    cfg = H.defaults()
    for mode, wants_flag in (("auto", False), ("0", True)):
        info = cg.probe(str(TAGGED), rotation=mode)
        graph = cg.graph_with_mask(cfg, info)
        args = cg.ffmpeg_inputs(str(TAGGED), cfg, info)
        ctx.expect_true(f"{mode}: the graph carries no transpose",
                        "transpose" not in graph, graph[:120])
        ctx.expect_true(f"{mode}: the graph has no rotation source segment",
                        "rotsrc" not in graph, graph[:120])
        ctx.expect_eq(f"{mode}: -noautorotate present", "-noautorotate" in args,
                      wants_flag)

    # And the graph text is character for character what an info dict built
    # the old way (a bare autorotate boolean, no "rotate" key at all) makes,
    # which is what a tool outside this repo still hands the engine.
    info = cg.probe(str(TAGGED), rotation="0")
    legacy = {k: v for k, v in info.items() if k != "rotate"}
    ctx.expect_eq("an info dict with no rotate key builds the same graph",
                  cg.graph_with_mask(cfg, legacy), cg.graph_with_mask(cfg, info))
    ctx.expect_eq("an info dict with no rotate key builds the same inputs",
                  cg.ffmpeg_inputs(str(TAGGED), cfg, legacy),
                  cg.ffmpeg_inputs(str(TAGGED), cfg, info))


def test_no_autorotate_is_the_alias_of_rotate_zero(ctx):
    """The CLI's old flag and the new one have to mean the same thing."""
    class A:
        pass

    old, new, both, neither = A(), A(), A(), A()
    old.no_autorotate, old.rotate = True, None
    new.no_autorotate, new.rotate = False, "0"
    both.no_autorotate, both.rotate = True, "90"
    neither.no_autorotate, neither.rotate = False, None

    ctx.expect_eq("--no-autorotate means rotate 0", cg.cli_rotation(old), "0")
    ctx.expect_eq("--rotate 0 means rotate 0", cg.cli_rotation(new), "0")
    ctx.expect_eq("--rotate wins when both are given", cg.cli_rotation(both), "90")
    ctx.expect_eq("neither flag means auto", cg.cli_rotation(neither), "auto")


def test_normalise_accepts_every_form_a_caller_sends(ctx):
    """One function has to read the browser's flag, an agent's string and a
    hand-written number, because all three reach the server today."""
    cases = [
        (True, "auto"), (False, "0"), ("1", "auto"), ("0", "0"),
        ("true", "auto"), ("false", "0"), ("auto", "auto"),
        ("90", "90"), (90, "90"), (180, "180"), ("270", "270"),
        (-90, "270"), (360, "0"), (None, "auto"), ("", "auto"),
    ]
    for value, want in cases:
        got = cg.normalise_rotation(value)
        ctx.expect_eq(f"normalise_rotation({value!r})", got, want)

    for bad in ("45", "sideways", "1.5"):
        try:
            cg.normalise_rotation(bad)
            ctx.check(False, f"normalise_rotation({bad!r}) should have refused")
        except cg.GradeError as exc:
            ctx.expect_true(f"normalise_rotation({bad!r}) refuses with a "
                            f"readable message", "rotation must be" in str(exc),
                            str(exc)[:120])


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------

def test_probe_reports_the_size_after_the_rotation(ctx):
    """90 and 270 swap the file's own size; 180 and 0 do not."""
    raw = cg.probe(str(TAGGED), rotation="0")
    rw, rh = raw["width"], raw["height"]
    ctx.note(f"file is {rw}x{rh} before any rotation")
    want = {"0": (rw, rh), "90": (rh, rw), "180": (rw, rh), "270": (rh, rw)}
    for mode, (w, h) in want.items():
        info = cg.probe(str(TAGGED), rotation=mode)
        ctx.expect_eq(f"rotation {mode}: width", info["width"], w)
        ctx.expect_eq(f"rotation {mode}: height", info["height"], h)


def test_fixed_rotations_head_the_graph_with_one_transpose_segment(ctx):
    """The turn has to be the first filter, before anything sized in pixels.

    Every scale target, every window matte and the grain plate are built from
    probe's POST-rotation size, so a transpose that ran later would be
    applying the right turn to a picture that had already been resized to the
    wrong shape.
    """
    cfg = H.defaults()
    want = {"90": 1, "180": 2, "270": 1}
    for mode, count in want.items():
        info = cg.probe(str(TAGGED), rotation=mode)
        graph = cg.graph_with_mask(cfg, info)
        args = cg.ffmpeg_inputs(str(TAGGED), cfg, info)
        head = graph.split(";")[0]
        ctx.note(f"{mode}: {head}")
        ctx.expect_true(f"{mode}: the first segment reads the raw input",
                        head.startswith("[0:v]"), head[:60])
        ctx.expect_true(f"{mode}: the first segment produces the rotated source",
                        head.endswith("[rotsrc]"), head[-30:])
        ctx.expect_eq(f"{mode}: transpose count", head.count("transpose="), count)
        ctx.expect_true(f"{mode}: the display tag is ignored",
                        "-noautorotate" in args, " ".join(args[:6]))
    ctx.expect_true("90 is a clockwise transpose",
                    "transpose=1" in cg.graph_with_mask(
                        cfg, cg.probe(str(TAGGED), rotation="90")))
    ctx.expect_true("270 is an anticlockwise transpose",
                    "transpose=2" in cg.graph_with_mask(
                        cfg, cg.probe(str(TAGGED), rotation="270")))


def test_a_caller_that_rotates_its_own_source_is_not_rotated_twice(ctx):
    """head_extra means "I built my own source chain", so the engine keeps out.

    The studio server scales before it grades, and its scale target is the
    post-rotation size, so it puts the transpose at the head of its own chain.
    If graph_with_mask added a second one the picture would come back turned
    180 degrees more than asked for, which is exactly the kind of bug that
    only shows up on the one clip nobody tested.
    """
    cfg = H.defaults()
    info = cg.probe(str(TAGGED), rotation="90")
    own = cg.graph_with_mask(cfg, info, src_label="studiosrc",
                             head_extra="[0:v]transpose=1,scale=64:114[studiosrc]")
    ctx.expect_eq("exactly one transpose, the caller's own",
                  own.count("transpose="), 1)
    ctx.expect_true("and no rotation segment of the engine's own",
                    "rotsrc" not in own, own[:120])

    normalised = cg.graph_with_mask(cfg, info, src_normalised=True)
    ctx.expect_eq("an already-normalised source is not rotated either",
                  normalised.count("transpose="), 0)


# --------------------------------------------------------------------------
# direction, on pixels
# --------------------------------------------------------------------------

CORNERS = ("top_left", "top_right", "bottom_left", "bottom_right")

# Where each corner of the source ends up, per rotation, clockwise on screen.
EXPECTED = {
    "0": {"top_left": "top_left", "top_right": "top_right",
          "bottom_left": "bottom_left", "bottom_right": "bottom_right"},
    "90": {"top_left": "top_right", "top_right": "bottom_right",
           "bottom_right": "bottom_left", "bottom_left": "top_left"},
    "180": {"top_left": "bottom_right", "top_right": "bottom_left",
            "bottom_right": "top_left", "bottom_left": "top_right"},
    "270": {"top_left": "bottom_left", "bottom_left": "bottom_right",
            "bottom_right": "top_right", "top_right": "top_left"},
}
# Read the mapping above as source corner -> where it lands. 90 clockwise
# takes the top left of the picture to the top right of the frame, which is
# what "turned clockwise" means when you look at it.

# Four brightnesses, well apart, so a corner is identified by its rank rather
# than by a value the grade is free to move.
LEVELS = {"top_left": 0.92, "top_right": 0.68,
          "bottom_left": 0.40, "bottom_right": 0.08}


def _corner_source() -> tuple:
    """A landscape PNG whose four quadrants are four flat brightnesses."""
    w, h = 48, 24
    arr = np.zeros((h, w, 3), dtype=np.float64)
    arr[:h // 2, :w // 2] = LEVELS["top_left"]
    arr[:h // 2, w // 2:] = LEVELS["top_right"]
    arr[h // 2:, :w // 2] = LEVELS["bottom_left"]
    arr[h // 2:, w // 2:] = LEVELS["bottom_right"]
    u16 = np.round(np.clip(arr, 0.0, 1.0) * 65535).astype("<u2")
    H.WORK.mkdir(parents=True, exist_ok=True)
    raw_path = H.WORK / "rotation_corners.raw"
    png_path = H.WORK / "rotation_corners.png"
    raw_path.write_bytes(u16.tobytes())
    H._run_bytes(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo",
                  "-pix_fmt", "rgb48le", "-s", f"{w}x{h}", "-i", str(raw_path),
                  "-frames:v", "1", "-pix_fmt", "rgb48be", str(png_path)])
    return png_path, w, h


def _render_corners(png, mode: str) -> tuple:
    """Grade the corner image at one rotation and read its four quadrants."""
    info = cg.probe(str(png), rotation=mode)
    # Same overrides patch_info uses: a PNG carries no range or matrix tag, and
    # guessing 'tv' here would stretch every level before the CST saw it.
    info = dict(info, color_range="full", color_space="bt2020nc",
                pix_fmt="rgb48be", nb_frames="1", duration="1")
    cfg = H.defaults()
    graph = cg.graph_with_mask(cfg, info, tail_extra=["format=rgb48le"],
                               encode_out=False)
    args = cg.ffmpeg_inputs(str(png), cfg, info)
    args += ["-filter_complex", graph, "-map", "[vout]", "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"]
    raw = H._run_bytes(args)
    w, h = info["width"], info["height"]
    if len(raw) != w * h * 3 * 2:
        raise H.RenderError(f"rotation {mode}: expected {w * h * 3 * 2} bytes "
                            f"for {w}x{h}, got {len(raw)}")
    out = np.frombuffer(raw, dtype="<u2").reshape(h, w, 3).astype(np.float64)
    qw, qh = w // 4, h // 4
    read = {
        "top_left": out[qh, qw], "top_right": out[qh, w - qw - 1],
        "bottom_left": out[h - qh - 1, qw],
        "bottom_right": out[h - qh - 1, w - qw - 1],
    }
    lum = {k: float(0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2])
           for k, v in read.items()}
    return lum, w, h


def test_rotation_turns_the_picture_clockwise(ctx):
    """A synthetic frame with four ranked corners, turned four ways."""
    png, sw, sh = _corner_source()
    ctx.note(f"source {sw}x{sh}, corners bright to dark: "
             + ", ".join(sorted(LEVELS, key=LEVELS.get, reverse=True)))

    # The rank of each source corner, brightest first. Every stage of the
    # pipeline is monotonic in luma, so these ranks are what survives a grade.
    order = sorted(LEVELS, key=LEVELS.get, reverse=True)
    rank = {name: i for i, name in enumerate(order)}

    for mode in ("0", "90", "180", "270"):
        lum, w, h = _render_corners(png, mode)
        want_size = (sh, sw) if mode in ("90", "270") else (sw, sh)
        ctx.expect_eq(f"rotation {mode}: rendered width", w, want_size[0])
        ctx.expect_eq(f"rotation {mode}: rendered height", h, want_size[1])

        got_order = sorted(CORNERS, key=lambda c: lum[c], reverse=True)
        got_rank = {name: i for i, name in enumerate(got_order)}
        ctx.note(f"rotation {mode}: {w}x{h}, brightest to darkest "
                 + ", ".join(got_order))
        for source_corner, lands_at in EXPECTED[mode].items():
            ctx.expect_eq(
                f"rotation {mode}: the {source_corner.replace('_', ' ')} of the "
                f"source is now the {lands_at.replace('_', ' ')}",
                got_rank[lands_at], rank[source_corner])


def test_auto_on_an_untagged_source_is_the_same_picture_as_zero(ctx):
    """No display matrix means auto and 0 are the same request.

    Worth pinning because it is the case every synthetic test and every
    exported still falls into: if the two ever diverged there, the whole
    "auto is today's behaviour" claim would be false for most files.
    """
    png, _, _ = _corner_source()
    auto, aw, ah = _render_corners(png, "auto")
    zero, zw, zh = _render_corners(png, "0")
    ctx.expect_eq("auto and 0 agree on width", aw, zw)
    ctx.expect_eq("auto and 0 agree on height", ah, zh)
    for corner in CORNERS:
        ctx.expect_close(f"auto and 0 agree on the {corner.replace('_', ' ')}",
                         auto[corner], zero[corner], 0.5)


def test_the_real_clip_renders_at_the_rotated_shape(ctx):
    """The whole path on the founder's own footage, not only on a synthetic.

    Rendered short (120 lines) so this stays one decode of one frame rather
    than a 4K readback, which is enough: what is under test is whether the
    decode, the display tag and the transpose agree on the shape, and that is
    visible in the aspect ratio.
    """
    cfg = H.defaults()
    shapes = {}
    for mode in ("0", "90"):
        info = cg.probe(str(TAGGED), rotation=mode)
        graph = cg.graph_with_mask(cfg, info, encode_out=False,
                                   tail_extra=["scale=-2:120", "format=rgb24"])
        args = cg.ffmpeg_inputs(str(TAGGED), cfg, info, seek=H.TIME_B)
        args += ["-filter_complex", graph, "-map", "[vout]", "-frames:v", "1",
                 "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        raw = H._run_bytes(args)
        width = len(raw) // (120 * 3)
        shapes[mode] = width
        ctx.note(f"rotation {mode}: probe says {info['width']}x{info['height']}, "
                 f"rendered {width}x120")
        want = int(round(120 * info["width"] / info["height"]))
        ctx.expect_close(f"rotation {mode}: the rendered aspect matches probe",
                         float(width), float(want), 2.0)
    ctx.expect_true("0 is the landscape one and 90 is the portrait one on "
                    "this clip", shapes["0"] > shapes["90"],
                    f"0 -> {shapes['0']} wide, 90 -> {shapes['90']} wide")


def register(suite):
    g = "rotation"
    suite.add(g, "auto_and_zero_match_the_old_boolean",
              test_auto_and_zero_match_the_old_boolean,
              doc="probe(rotation=) and probe(autorotate=) agree field by field")
    suite.add(g, "auto_and_zero_emit_the_pre_rotation_commands",
              test_auto_and_zero_emit_the_pre_rotation_commands,
              doc="no transpose, and -noautorotate exactly where it always was")
    suite.add(g, "no_autorotate_is_the_alias_of_rotate_zero",
              test_no_autorotate_is_the_alias_of_rotate_zero,
              doc="the old CLI flag still resolves to rotate 0")
    suite.add(g, "normalise_accepts_every_form_a_caller_sends",
              test_normalise_accepts_every_form_a_caller_sends,
              doc="booleans, query strings, numbers and nonsense all resolve or refuse")
    suite.add(g, "probe_reports_the_size_after_the_rotation",
              test_probe_reports_the_size_after_the_rotation,
              doc="90 and 270 swap the reported size, 0 and 180 do not")
    suite.add(g, "fixed_rotations_head_the_graph_with_one_transpose_segment",
              test_fixed_rotations_head_the_graph_with_one_transpose_segment,
              doc="the turn is the first filter, ahead of every pixel-sized stage")
    suite.add(g, "a_caller_that_rotates_its_own_source_is_not_rotated_twice",
              test_a_caller_that_rotates_its_own_source_is_not_rotated_twice,
              doc="head_extra and src_normalised both mean the engine keeps out")
    suite.add(g, "rotation_turns_the_picture_clockwise",
              test_rotation_turns_the_picture_clockwise,
              doc="four ranked corners land where a clockwise turn puts them")
    suite.add(g, "auto_on_an_untagged_source_is_the_same_picture_as_zero",
              test_auto_on_an_untagged_source_is_the_same_picture_as_zero,
              doc="with no display matrix, auto and 0 are one request")
    suite.add(g, "the_real_clip_renders_at_the_rotated_shape",
              test_the_real_clip_renders_at_the_rotated_shape,
              doc="the founder's own footage decodes and turns to the shape probe promised")
