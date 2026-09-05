"""Group: --region and --zoom on the engine's stills (contract E4).

The founder asked for "more zoom controls for the frontend and for the
agent". This file is the agent's half at the CLI: `cinegrade still --region
x0 y0 x1 y1 --zoom N` renders only part of the frame, so an agent can look at
skin, a highlight or a window edge at real resolution instead of squinting at
a 480 pixel wide contact print of the whole shot.

The one thing that can quietly be wrong here is WHEN the crop happens. Every
spatial stage in the graph (power windows, the radial ramp, the vignette, the
grain plate) is sized from the whole frame, so a crop taken BEFORE the grade
would re-centre all of them on the patch: ask for a corner and the vignette
would draw itself around that corner instead of staying dark. So the tests
below do not check that a region "looks about right", they check that the
region is byte for byte the pixels the full render has in that rectangle,
with a vignette turned on precisely because it makes the wrong answer
visible.

What this file pins:

  after the grade  four known quadrants, graded with a vignette, each
                   requested as a region. Every pixel must equal the full
                   render's pixels there.
  after rotation   the fractions are of the frame the viewer sees, so with
                   --rotate 90 a region is a rectangle of the ROTATED frame,
                   in the rotated frame's dimensions.
  compatibility    a region covering the whole frame writes the same file,
                   byte for byte, as no region at all. The new flags cannot
                   change a still nobody asked to crop.
  zoom             --zoom N gives N times the size the region has inside a
                   --width wide frame, and stops at the region's own pixel
                   count, because there are no more pixels than that.
  compare          the same two flags on `compare`, which is the command a
                   human runs to put looks side by side.
  saying no        a sliver under 1% of an axis, and a zoom of zero, exit
                   non zero with a message that names the problem rather
                   than rendering something else.

Every image here is synthetic or the suite's own clip, written under the
suite's scratch directory. Nothing is added to stills/ or refs/.
"""

from __future__ import annotations

import hashlib
import json
import subprocess

import numpy as np

import harness as H
import match_ref as MR

PY = str(H.CONTENT / ".venv" / "bin" / "python")
ENGINE = str(H.GRADE / "cinegrade.py")

# Deliberately not the fully saturated primaries, for the reason
# cases_match_crop gives: pure red sits under the chrome detector's dark
# panel threshold and sends synthetic images down a fallback path.
QUADRANTS = {
    "top_left": (0.85, 0.15, 0.15),        # red
    "top_right": (0.15, 0.20, 0.85),       # blue
    "bottom_left": (0.20, 0.75, 0.25),     # green
    "bottom_right": (0.80, 0.78, 0.20),    # yellow
}
BOXES = {
    "top_left": [0.0, 0.0, 0.5, 0.5],
    "top_right": [0.5, 0.0, 1.0, 0.5],
    "bottom_left": [0.0, 0.5, 0.5, 1.0],
    "bottom_right": [0.5, 0.5, 1.0, 1.0],
}
QW, QH = 64, 48


def _quadrant_png(name: str = "region_quadrants"):
    """A four colour test chart as a 16 bit PNG (this venv has no PIL)."""
    arr = np.zeros((QH, QW, 3), dtype=np.float64)
    arr[:QH // 2, :QW // 2] = QUADRANTS["top_left"]
    arr[:QH // 2, QW // 2:] = QUADRANTS["top_right"]
    arr[QH // 2:, :QW // 2] = QUADRANTS["bottom_left"]
    arr[QH // 2:, QW // 2:] = QUADRANTS["bottom_right"]
    u16 = np.round(np.clip(arr, 0.0, 1.0) * 65535).astype("<u2")
    H.WORK.mkdir(parents=True, exist_ok=True)
    raw = H.WORK / f"{name}.raw"
    png = H.WORK / f"{name}.png"
    raw.write_bytes(u16.tobytes())
    H._run_bytes(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo",
                  "-pix_fmt", "rgb48le", "-s", f"{QW}x{QH}", "-i", str(raw),
                  "-frames:v", "1", "-pix_fmt", "rgb48be", str(png)])
    return png


def _vignette_preset(name: str = "region_vignette"):
    """A preset whose vignette is on, so a crop before the grade would show.

    The vignette is drawn from the centre of whatever frame the filter sees.
    Crop first and the patch grows its own dark corners; crop last and the
    patch keeps the corners the full frame gave it.
    """
    H.WORK.mkdir(parents=True, exist_ok=True)
    cfg = {"fx": {"vignette": {"enabled": True, "amount": 0.85,
                               "radius": 0.55}}}
    path = H.WORK / f"{name}.json"
    path.write_text(json.dumps(cfg))
    return path


def _still(ctx, label, args):
    """Run `cinegrade still`, answering with the output path or None."""
    r = subprocess.run([PY, ENGINE, "still"] + [str(a) for a in args],
                       capture_output=True, text=True)
    if not ctx.expect_eq(f"{label}: exit code", r.returncode, 0):
        ctx.note(f"{label} stderr: {r.stderr[-900:]}")
        return None
    return r


def _size(path):
    st = H.ffprobe_stream(path, "width,height")
    return int(st["width"]), int(st["height"])


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# the crop runs after the grade
# --------------------------------------------------------------------------

def test_a_region_is_the_pixels_the_full_render_has_there(ctx):
    """Each quadrant comes back exactly as the full render drew it."""
    src = _quadrant_png()
    preset = _vignette_preset()
    whole = H.WORK / "region_whole.png"
    if _still(ctx, "whole frame",
              [src, "-p", preset, "-o", whole]) is None:
        return
    full = MR.read_image(whole)
    ctx.note(f"full render {full.shape[1]}x{full.shape[0]}, vignette on at "
             f"amount 0.85, radius 0.55")

    for name, box in BOXES.items():
        out = H.WORK / f"region_{name}.png"
        if _still(ctx, name, [src, "-p", preset, "-o", out,
                              "--region"] + box) is None:
            continue
        got = MR.read_image(out)
        w, h = got.shape[1], got.shape[0]
        ctx.expect_eq(f"{name}: size", f"{w}x{h}", f"{QW // 2}x{QH // 2}")
        if (w, h) != (QW // 2, QH // 2):
            continue
        x0 = int(box[0] * QW)
        y0 = int(box[1] * QH)
        want = full[y0:y0 + h, x0:x0 + w]
        diff = float(np.abs(got - want).max())
        # Both files come out of the same graph with no scale in either, so
        # the only allowance here is PNG quantisation, which is zero.
        ctx.expect_le(f"{name}: worst pixel difference from the full render",
                      diff, 0.0)
        corner = float(H.luma(want[:4, :4]).mean())
        centre = float(H.luma(full[QH // 2 - 2:QH // 2 + 2,
                                   QW // 2 - 2:QW // 2 + 2]).mean())
        ctx.note(f"{name}: {w}x{h}, mean "
                 + ", ".join(f"{v:.4f}" for v in got.reshape(-1, 3).mean(0)))
        if name == "top_left":
            # The proof the vignette was not re-centred: the patch's own
            # outer corner is darker than the frame's centre.
            ctx.expect_lt("the vignette stayed where the full frame put it: "
                          "the patch corner is darker than the frame centre",
                          corner, centre)


def test_a_region_is_taken_after_the_rotation(ctx):
    """With --rotate 90 the fractions are of the picture the viewer sees."""
    src = _quadrant_png()
    whole = H.WORK / "region_rot_whole.png"
    if _still(ctx, "rotated whole frame",
              [src, "-p", "flat", "--rotate", "90", "-o", whole]) is None:
        return
    full = MR.read_image(whole)
    ctx.expect_eq("the rotated frame is turned on its side",
                  f"{full.shape[1]}x{full.shape[0]}", f"{QH}x{QW}")

    out = H.WORK / "region_rot_topleft.png"
    if _still(ctx, "rotated region",
              [src, "-p", "flat", "--rotate", "90", "-o", out,
               "--region", 0.0, 0.0, 0.5, 0.5]) is None:
        return
    got = MR.read_image(out)
    ctx.expect_eq("the region is a quarter of the ROTATED frame",
                  f"{got.shape[1]}x{got.shape[0]}",
                  f"{QH // 2}x{QW // 2}")
    if got.shape[:2] != (QW // 2, QH // 2):
        return
    want = full[:QW // 2, :QH // 2]
    ctx.expect_le("worst pixel difference from the rotated full render",
                  float(np.abs(got - want).max()), 0.0)
    # Turning the frame clockwise puts the source's bottom left quadrant in
    # the top left, so this rectangle must read green, not red.
    mean = got.reshape(-1, 3).mean(0)
    ctx.note("rotated top left region mean "
             + ", ".join(f"{v:.4f}" for v in mean))
    ctx.expect_gt("the rotated top left is the source's bottom left (green)",
                  float(mean[1]), float(mean[0]))


# --------------------------------------------------------------------------
# compatibility
# --------------------------------------------------------------------------

def test_a_whole_frame_region_is_the_same_file_as_no_region(ctx):
    """The new flags cannot change a still that does not use them."""
    plain = H.WORK / "region_none.png"
    whole = H.WORK / "region_all.png"
    args = [str(H.CLIP_A), "-p", "natural", "--time", str(H.TIME_A),
            "--width", "240"]
    if _still(ctx, "no region", args + ["-o", plain]) is None:
        return
    if _still(ctx, "region 0 0 1 1",
              args + ["-o", whole, "--region", 0, 0, 1, 1]) is None:
        return
    if _still(ctx, "zoom 1, no region",
              args + ["-o", H.WORK / "region_zoom1.png", "--zoom", 1]) is None:
        return
    a, b = _sha(plain), _sha(whole)
    ctx.note(f"no region {a[:16]}, whole frame region {b[:16]}")
    ctx.expect_eq("a region covering everything writes the same bytes", b, a)
    ctx.expect_eq("zoom 1 with no region writes the same bytes",
                  _sha(H.WORK / "region_zoom1.png"), a)
    ctx.expect_eq("and it is still the width that was asked for",
                  _size(plain)[0], 240)


# --------------------------------------------------------------------------
# zoom
# --------------------------------------------------------------------------

def test_zoom_scales_the_region_and_stops_at_its_own_pixels(ctx):
    """N times the size the region has in a --width wide frame, then the cap."""
    box = [0.25, 0.25, 0.75, 0.75]
    # The engine's own probe, so the numbers here are the frame AFTER the
    # display matrix is honoured. This clip is stored landscape and shown
    # portrait, so the stream's width is not the width a region divides.
    info = H.cg.probe(str(H.CLIP_A), rotation="auto")
    region_w = H.cg.region_pixels(box, info)[2]
    ctx.note(f"source {info['width']}x{info['height']} after rotation, "
             f"the middle half is {region_w} columns of it")
    args = [str(H.CLIP_A), "-p", "natural", "--time", str(H.TIME_A),
            "--width", "480"]
    sizes = {}
    for label, zoom in (("fit", 1), ("2x", 2), ("999x", 999)):
        out = H.WORK / f"region_zoom_{label}.png"
        if _still(ctx, f"zoom {label}",
                  args + ["-o", out, "--region"] + box + ["--zoom", zoom]) is None:
            return
        sizes[label] = _size(out)
        ctx.note(f"zoom {label}: {sizes[label][0]}x{sizes[label][1]}")
    # Half the frame inside a 480 wide picture is 240 wide.
    ctx.expect_close("zoom 1 is the region's share of a 480 wide frame",
                     float(sizes["fit"][0]), 240.0, 2.0)
    ctx.expect_close("zoom 2 is twice that", float(sizes["2x"][0]),
                     float(sizes["fit"][0]) * 2, 3.0)
    # The cap is exact: the region owns that many columns and no more, so a
    # silly zoom lands on the region's own pixels rather than upscaling them.
    ctx.expect_eq("no zoom invents pixels the source does not have",
                  sizes["999x"][0], region_w)
    ctx.expect_gt("the capped render is still the biggest of the three",
                  float(sizes["999x"][0]), float(sizes["2x"][0]))


# --------------------------------------------------------------------------
# compare
# --------------------------------------------------------------------------

def test_compare_takes_a_region_too(ctx):
    """The side by side command crops the same way the still does."""
    src = _quadrant_png()
    full = H.WORK / "region_compare_full.png"
    part = H.WORK / "region_compare_part.png"
    common = [str(src), "-p", "flat", "--presets", "flat,natural",
              "--width", "560"]
    for label, out, extra in (("compare, whole frame", full, []),
                              ("compare, left half", part,
                               ["--region", "0", "0", "0.5", "1"])):
        r = subprocess.run([PY, ENGINE, "compare"] + common
                           + ["-o", str(out)] + extra,
                           capture_output=True, text=True)
        if not ctx.expect_eq(f"{label}: exit code", r.returncode, 0):
            ctx.note(f"{label} stderr: {r.stderr[-900:]}")
            return
    fw, fh = _size(full)
    pw, ph = _size(part)
    ctx.note(f"compare sheet {fw}x{fh} whole, {pw}x{ph} for the left half")
    ctx.expect_gt("a region makes a narrower sheet than the whole frame",
                  float(fw), float(pw))
    # Two panels side by side, each the left half of this 64 wide chart, so
    # the sheet is exactly two half frames wide and one frame tall.
    ctx.expect_eq("the sheet is two panels of the region", pw, QW)
    ctx.expect_eq("and it kept the region's full height", ph, QH)


# --------------------------------------------------------------------------
# saying no
# --------------------------------------------------------------------------

def test_a_region_that_cannot_be_rendered_is_refused(ctx):
    """A sliver and a zoom of zero are named, not rendered as something else."""
    src = _quadrant_png()
    out = H.WORK / "region_refused.png"
    cases = [
        ("a sliver", ["--region", "0.2", "0.2", "0.2005", "0.9"], "too small"),
        ("zoom zero", ["--region", "0", "0", "0.5", "0.5", "--zoom", "0"],
         "greater than zero"),
    ]
    for label, extra, want in cases:
        r = subprocess.run([PY, ENGINE, "still", str(src), "-p", "flat",
                            "-o", str(out)] + extra,
                           capture_output=True, text=True)
        text = (r.stderr + r.stdout).lower()
        ctx.expect_gt(f"{label}: exit code is non zero", float(r.returncode), 0.0)
        ctx.expect_true(f"{label}: the message says {want!r}", want in text,
                        "found" if want in text else f"got: {text[-200:]!r}")


def register(suite):
    g = "region"
    suite.add(g, "a_region_is_the_pixels_the_full_render_has_there",
              test_a_region_is_the_pixels_the_full_render_has_there,
              doc="the crop runs after the grade, vignette and all")
    suite.add(g, "a_region_is_taken_after_the_rotation",
              test_a_region_is_taken_after_the_rotation,
              doc="the fractions are of the picture the viewer sees")
    suite.add(g, "a_whole_frame_region_is_the_same_file_as_no_region",
              test_a_whole_frame_region_is_the_same_file_as_no_region,
              doc="the new flags change nothing when nobody uses them")
    suite.add(g, "zoom_scales_the_region_and_stops_at_its_own_pixels",
              test_zoom_scales_the_region_and_stops_at_its_own_pixels,
              doc="zoom N is N times the fit size, capped by the source")
    suite.add(g, "compare_takes_a_region_too",
              test_compare_takes_a_region_too,
              doc="the side by side sheet crops the same way")
    suite.add(g, "a_region_that_cannot_be_rendered_is_refused",
              test_a_region_that_cannot_be_rendered_is_refused,
              doc="a sliver and a zero zoom exit with a message")
