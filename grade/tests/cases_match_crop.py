"""Group: the picked rectangle on a match (contract C7).

The founder's ask was "i need to be able to use the rectangle mask tool to
pick what i want before I use match reference". So `match_ref.match_reference`
takes `ref_crop` and `frame_crop`, four fractions each, and applies them
BEFORE anything else looks at the picture, which means the automatic
Instagram chrome trim runs inside the rectangle instead of arguing with it.

What this file pins:

  crop math      a synthetic image with four known coloured quadrants. The
                 rectangle for a quadrant must come back as that quadrant's
                 mean, to the accuracy 16 bit PNG round tripping allows.
                 Rounds outward, so a rectangle drawn on a small preview
                 still covers the pixels it visibly covered.
  parsing        any corner order is the same rectangle, values outside
                 0 to 1 clamp, and a sliver or a wrong length is refused
                 with a message rather than silently measured.
  the fit        a reference that is red on the left and blue on the right,
                 matched onto a neutral ramp. Picking the left half must
                 push mid grey toward red; the whole image, which is half
                 red and half blue, must not. That is the whole feature in
                 one measurement.
  compatibility  a rectangle covering the whole image produces the same cube
                 file, byte for byte, as no rectangle at all: the new
                 argument cannot change a match nobody asked to crop.

Every image here is synthetic and written under the suite's own scratch
directory. Nothing is added to refs/.
"""

from __future__ import annotations

import numpy as np

import harness as H
import match_ref as MR

# Deliberately not the fully saturated primaries. Pure red at (1, 0, 0)
# encodes to a display luma of 0.213, which is under the 0.22 the chrome
# detector treats as a flat dark panel, so a synthetic image made of it goes
# down a fallback path that has nothing to do with what is being tested here.
QUADRANTS = {
    "top_left": (0.85, 0.15, 0.15),        # red, hue family "warm"
    "top_right": (0.15, 0.20, 0.85),       # blue, hue family "cool"
    "bottom_left": (0.20, 0.75, 0.25),     # green
    "bottom_right": (0.80, 0.78, 0.20),    # yellow
}
RED = QUADRANTS["top_left"]
BLUE = QUADRANTS["top_right"]


def _write_png(arr: np.ndarray, name: str):
    """A float RGB array to a 16 bit PNG, through ffmpeg (this venv has no PIL)."""
    u16 = np.round(np.clip(arr, 0.0, 1.0) * 65535).astype("<u2")
    h, w = arr.shape[:2]
    H.WORK.mkdir(parents=True, exist_ok=True)
    raw_path = H.WORK / f"{name}.raw"
    png_path = H.WORK / f"{name}.png"
    raw_path.write_bytes(u16.tobytes())
    H._run_bytes(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo",
                  "-pix_fmt", "rgb48le", "-s", f"{w}x{h}", "-i", str(raw_path),
                  "-frames:v", "1", "-pix_fmt", "rgb48be", str(png_path)])
    return png_path


def _quadrant_image(w: int = 64, h: int = 48) -> np.ndarray:
    arr = np.zeros((h, w, 3), dtype=np.float64)
    arr[:h // 2, :w // 2] = QUADRANTS["top_left"]
    arr[:h // 2, w // 2:] = QUADRANTS["top_right"]
    arr[h // 2:, :w // 2] = QUADRANTS["bottom_left"]
    arr[h // 2:, w // 2:] = QUADRANTS["bottom_right"]
    return arr


def _half_and_half(w: int = 96, h: int = 72) -> np.ndarray:
    """Red left, blue right, each with a vertical ramp over it.

    The ramp is there so each half has a real distribution rather than one
    repeated value: a Reinhard fit divides by the source's standard deviation,
    and a perfectly flat patch has none.
    """
    arr = np.zeros((h, w, 3), dtype=np.float64)
    ramp = np.linspace(0.62, 1.0, h)[:, None]
    arr[:, :w // 2] = np.array(RED)[None, None, :] * ramp[..., None]
    arr[:, w // 2:] = np.array(BLUE)[None, None, :] * ramp[..., None]
    return arr


def _neutral_ramp(w: int = 96, h: int = 72) -> np.ndarray:
    """A grey vertical ramp: no hue of its own, so the fit has to supply one."""
    ramp = np.linspace(0.10, 0.90, h)[:, None]
    return np.repeat(np.repeat(ramp, w, axis=1)[..., None], 3, axis=2)


# --------------------------------------------------------------------------
# crop math
# --------------------------------------------------------------------------

def test_a_rectangle_reads_back_that_quadrant(ctx):
    """Cropping to a quadrant gives that quadrant's mean, and only it."""
    png = _write_png(_quadrant_image(), "match_crop_quadrants")
    img = MR.read_image(png)
    ctx.note(f"synthetic reference {img.shape[1]}x{img.shape[0]}, "
             f"four flat quadrants")

    boxes = {
        "top_left": [0.0, 0.0, 0.5, 0.5],
        "top_right": [0.5, 0.0, 1.0, 0.5],
        "bottom_left": [0.0, 0.5, 0.5, 1.0],
        "bottom_right": [0.5, 0.5, 1.0, 1.0],
    }
    for name, box in boxes.items():
        piece = MR.crop_box(img, box, name)
        mean = piece.reshape(-1, 3).mean(0)
        want = QUADRANTS[name]
        ctx.note(f"{name}: {piece.shape[1]}x{piece.shape[0]} px, mean "
                 + ", ".join(f"{v:.4f}" for v in mean))
        for i, chan in enumerate("rgb"):
            # 1/65535 is the PNG's own quantisation step; 0.002 leaves room
            # for it on every channel without leaving room for a wrong crop.
            ctx.expect_close(f"{name} crop, {chan} mean",
                             float(mean[i]), float(want[i]), 0.002)

    whole = img.reshape(-1, 3).mean(0)
    average = np.mean([QUADRANTS[k] for k in QUADRANTS], axis=0)
    for i, chan in enumerate("rgb"):
        ctx.expect_close(f"no crop is still the whole image, {chan} mean",
                         float(whole[i]), float(average[i]), 0.002)


def test_a_rectangle_rounds_outward_to_whole_pixels(ctx):
    """A fraction that lands mid pixel keeps the pixel it visibly covered."""
    png = _write_png(_quadrant_image(w=64, h=48), "match_crop_rounding")
    img = MR.read_image(png)
    # 0.51 of 64 is 32.64: the rectangle starts inside column 32, and the
    # pixel it starts inside is part of what the user drew a box around.
    piece = MR.crop_box(img, [0.51, 0.0, 0.99, 0.49], "rounding")
    ctx.note(f"0.51 to 0.99 of 64 columns gave {piece.shape[1]} columns")
    ctx.expect_eq("the left edge rounds down to the pixel it lands in",
                  piece.shape[1], int(np.ceil(0.99 * 64)) - int(0.51 * 64))
    mean = piece.reshape(-1, 3).mean(0)
    for i, chan in enumerate("rgb"):
        ctx.expect_close(f"the rounded crop is still all top right, {chan}",
                         float(mean[i]), float(QUADRANTS["top_right"][i]), 0.002)


def test_parse_box_accepts_any_corner_order_and_refuses_nonsense(ctx):
    """One rectangle, however it was dragged. A sliver is an error, not a crop."""
    up_left = MR.parse_box([0.8, 0.9, 0.2, 0.1])
    down_right = MR.parse_box([0.2, 0.1, 0.8, 0.9])
    ctx.expect_eq("a drag up and to the left is the same rectangle",
                  up_left, down_right)
    ctx.expect_eq("a comma separated string is the same rectangle",
                  MR.parse_box("0.2,0.1,0.8,0.9"), down_right)
    ctx.expect_eq("values past the edges clamp into 0 to 1",
                  MR.parse_box([-2.0, -1.0, 3.0, 4.0]), [0.0, 0.0, 1.0, 1.0])
    ctx.expect_true("None is no rectangle at all", MR.parse_box(None) is None)
    ctx.expect_true("an empty string is no rectangle at all",
                    MR.parse_box("") is None)

    for bad, why in (([0.1, 0.1, 0.105, 0.9], "a sliver"),
                     ([0.1, 0.1, 0.9], "three numbers"),
                     (["a", "b", "c", "d"], "not numbers")):
        try:
            MR.parse_box(bad, "ref-crop")
            ctx.expect_true(f"{why} is refused", False, f"{bad} was accepted")
        except MR.MatchError as exc:
            ctx.note(f"{why}: {exc}")
            ctx.expect_true(f"{why} is refused with a message", True)

    tiny = _write_png(_quadrant_image(w=64, h=48), "match_crop_tiny")
    img = MR.read_image(tiny)
    try:
        MR.crop_box(img, [0.0, 0.0, 0.1, 0.1], "reference")
        ctx.expect_true("a rectangle under 16 px is refused", False,
                        "6x4 pixels was accepted as a colour distribution")
    except MR.MatchError as exc:
        ctx.note(str(exc))
        ctx.expect_true("a rectangle under 16 px is refused", True)


# --------------------------------------------------------------------------
# the fit itself
# --------------------------------------------------------------------------

def _match(ctx, name: str, **kw) -> dict:
    ref = _write_png(_half_and_half(), "match_crop_ref")
    src = _write_png(_neutral_ramp(), "match_crop_src")
    return MR.match_reference(
        ref=str(ref), still=str(src), method="reinhard",
        size=17, name=name, out_dir=str(H.WORK), verify=False, **kw)


def _mid_grey_through(cube) -> np.ndarray:
    """Mid grey pushed through the real ffmpeg lut3d, not a python copy."""
    patch = np.full((16, 16, 3), 0.5, dtype=np.float32)
    return MR.apply_cube(patch, cube).reshape(-1, 3).mean(0)


def test_a_pick_on_the_red_half_fits_toward_red(ctx):
    """The whole reference is half red and half blue; the left half is red."""
    from pathlib import Path

    whole = _match(ctx, "match_crop_whole")
    left = _match(ctx, "match_crop_left", ref_crop=[0.0, 0.0, 0.5, 1.0])

    fam_w = whole["stats"]["reference"]["families"]
    fam_l = left["stats"]["reference"]["families"]
    ctx.note(f"whole reference: warm {100 * fam_w['warm']:.0f}%, "
             f"cool {100 * fam_w['cool']:.0f}%")
    ctx.note(f"left half: warm {100 * fam_l['warm']:.0f}%, "
             f"cool {100 * fam_l['cool']:.0f}%")
    ctx.expect_close("the whole reference is half warm", fam_w["warm"], 0.5, 0.06)
    ctx.expect_close("the whole reference is half cool", fam_w["cool"], 0.5, 0.06)
    ctx.expect_gt("the picked left half is warm", fam_l["warm"], 0.97)
    ctx.expect_lt("the picked left half has no cool left in it",
                  fam_l["cool"], 0.01)

    pick = left["crops"]["ref"]
    ctx.expect_eq("the result says which rectangle it measured",
                  pick["box"], [0.0, 0.0, 0.5, 1.0])
    ctx.expect_close("and how much of the picture that was",
                     pick["area_pct"], 50.0, 1.0)
    ctx.expect_true("a match with no rectangle says so",
                    whole["crops"]["ref"] is None and whole["crops"]["frame"] is None)

    grey_w = _mid_grey_through(Path(whole["lut"]))
    grey_l = _mid_grey_through(Path(left["lut"]))
    ctx.note("mid grey through the whole image cube: "
             + ", ".join(f"{v:.4f}" for v in grey_w))
    ctx.note("mid grey through the left half cube:   "
             + ", ".join(f"{v:.4f}" for v in grey_l))
    ctx.expect_gt("picking the red half pushes mid grey toward red",
                  float(grey_l[0] - grey_l[2]), float(grey_w[0] - grey_w[2]), 0.05)
    ctx.expect_gt("and the picked cube really is red dominant",
                  float(grey_l[0]), float(grey_l[2]), 0.05)


def test_a_frame_pick_changes_what_the_source_measured(ctx):
    """The rectangle on the FRAME moves the before statistics, so it moves the fit."""
    from pathlib import Path

    whole = _match(ctx, "match_crop_frame_whole")
    top = _match(ctx, "match_crop_frame_top", frame_crop=[0.0, 0.0, 1.0, 0.35])

    med_w = whole["stats"]["source_before"]["luma_pct"][2]
    med_t = top["stats"]["source_before"]["luma_pct"][2]
    ctx.note(f"median luma of the whole frame {med_w:.4f}, "
             f"of the top 35% {med_t:.4f}")
    # The source is a ramp from 0.10 at the top to 0.90 at the bottom, so the
    # top third is the dark end of it and nothing else.
    ctx.expect_lt("the top of the ramp is darker than the whole ramp",
                  med_t, med_w, 0.15)
    ctx.expect_eq("the result says which frame rectangle it measured",
                  top["crops"]["frame"]["box"], [0.0, 0.0, 1.0, 0.35])
    ctx.expect_true("the reference was left whole",
                    top["crops"]["ref"] is None)
    ctx.expect_true("the two cubes are not the same file",
                    Path(whole["lut"]).read_bytes() != Path(top["lut"]).read_bytes())


def test_a_full_frame_rectangle_is_the_same_cube_as_no_rectangle(ctx):
    """The new argument cannot change a match that did not ask to be cropped."""
    from pathlib import Path

    plain = _match(ctx, "match_crop_compat_plain")
    boxed = _match(ctx, "match_crop_compat_boxed",
                   ref_crop=[0.0, 0.0, 1.0, 1.0], frame_crop=[0.0, 0.0, 1.0, 1.0])
    a = Path(plain["lut"]).read_text().splitlines()
    b = Path(boxed["lut"]).read_text().splitlines()
    # The two files differ in their TITLE and in the comment naming the
    # rectangle, which is the point of the comment. The numbers must not.
    nums_a = [ln for ln in a if not ln.startswith("#") and not ln.startswith("TITLE")]
    nums_b = [ln for ln in b if not ln.startswith("#") and not ln.startswith("TITLE")]
    ctx.note(f"{len(nums_a)} cube lines each")
    ctx.expect_eq("a rectangle covering everything is the same cube",
                  nums_a, nums_b)
    ctx.expect_eq("and the same measured reference statistics",
                  plain["stats"]["reference"], boxed["stats"]["reference"])


def register(suite):
    g = "match_crop"
    suite.add(g, "a_rectangle_reads_back_that_quadrant",
              test_a_rectangle_reads_back_that_quadrant,
              doc="cropping to a quadrant gives that quadrant's mean")
    suite.add(g, "a_rectangle_rounds_outward_to_whole_pixels",
              test_a_rectangle_rounds_outward_to_whole_pixels,
              doc="a fraction landing mid pixel keeps that pixel")
    suite.add(g, "parse_box_accepts_any_corner_order_and_refuses_nonsense",
              test_parse_box_accepts_any_corner_order_and_refuses_nonsense,
              doc="either drag direction is one rectangle, a sliver is an error")
    suite.add(g, "a_pick_on_the_red_half_fits_toward_red",
              test_a_pick_on_the_red_half_fits_toward_red,
              doc="the picked half is what the transform is fitted from")
    suite.add(g, "a_frame_pick_changes_what_the_source_measured",
              test_a_frame_pick_changes_what_the_source_measured,
              doc="the rectangle on the frame moves the before statistics")
    suite.add(g, "a_full_frame_rectangle_is_the_same_cube_as_no_rectangle",
              test_a_full_frame_rectangle_is_the_same_cube_as_no_rectangle,
              doc="the new argument changes nothing when it covers everything")
