"""Group: contract G3, measurement.

grade/stats.py holds `frame_stats` (moved here unchanged from studio/server.py),
`bands` (per luma band colour) and `decode_image` (the one decode function a
still or a video frame goes through). This file pins:

  the move       studio/server.py imports frame_stats from here rather than
                 defining its own copy, and the numbers a fixed synthetic
                 frame produces are pinned, so a future edit to either file
                 cannot quietly drift the two apart again.
  bands          eight equal luma bands by default, each with a saturation, a
                 signed warm (R-B) and tint (G - mean(R,B)), and a pixel
                 count; an empty band reads zero on every field, never NaN.
  decode_image   one bounded ffmpeg call decodes a still and a video frame
                 alike, `time` has no effect on a still (there is no later
                 frame to seek to), and a region crop is exactly the same
                 pixels a manual numpy slice of the uncropped decode gives.
  cmd_stats      the CLI prints the same dict the studio's stats route
                 returns (studio strip, not ffmpeg signalstats), with
                 --image, --json, --region and --times all wired.
  sweep          one parameter, many values, one row per value, never a
                 ranking: reports what changed, not which value to pick.

This is a measurement engine: nothing here checks that a number is "better",
only that it is the number the documented formula gives.
"""

from __future__ import annotations

import json
import subprocess
import sys

import numpy as np

import harness as H

sys.path.insert(0, str(H.GRADE))
import stats as ST                                     # noqa: E402

PY = str(H.CONTENT / ".venv" / "bin" / "python")
ENGINE = str(H.GRADE / "cinegrade.py")
SERVER_PY = H.CONTENT / "studio" / "server.py"
SRC = str(H.CLIP_A)


def _cli(ctx, label, args, expect_stdout=()):
    r = subprocess.run([PY, ENGINE] + args, capture_output=True, text=True)
    ok = ctx.expect_eq(f"{label}: exit code", r.returncode, 0)
    if not ok:
        ctx.note(f"{label} stderr: {r.stderr[-900:]}")
        return None
    for token in expect_stdout:
        ctx.expect_true(f"{label}: stdout mentions {token!r}", token in r.stdout,
                        "found" if token in r.stdout else f"got: {r.stdout[:300]!r}")
    return r


def _fails(ctx, label, args, want_stderr):
    r = subprocess.run([PY, ENGINE] + args, capture_output=True, text=True)
    ctx.expect_true(f"{label}: exits non zero", r.returncode != 0,
                    f"exit {r.returncode}")
    ctx.expect_true(f"{label}: message says {want_stderr!r}",
                    want_stderr in r.stderr, r.stderr.strip()[:200])


def _synthetic_png(name="stats_quadrants"):
    """A four colour chart, the same trick cases_region.py uses (this venv
    has no PIL): a 16 bit rawvideo pipe into ffmpeg's PNG encoder."""
    colours = [(0.85, 0.15, 0.15), (0.15, 0.20, 0.85),
              (0.20, 0.75, 0.25), (0.80, 0.78, 0.20)]
    w, h = 64, 48
    arr = np.zeros((h, w, 3), dtype=np.float64)
    arr[:h // 2, :w // 2] = colours[0]
    arr[:h // 2, w // 2:] = colours[1]
    arr[h // 2:, :w // 2] = colours[2]
    arr[h // 2:, w // 2:] = colours[3]
    u16 = np.round(np.clip(arr, 0.0, 1.0) * 65535).astype("<u2")
    H.WORK.mkdir(parents=True, exist_ok=True)
    raw = H.WORK / f"{name}.raw"
    png = H.WORK / f"{name}.png"
    raw.write_bytes(u16.tobytes())
    H._run_bytes(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo",
                  "-pix_fmt", "rgb48le", "-s", f"{w}x{h}", "-i", str(raw),
                  "-frames:v", "1", "-pix_fmt", "rgb48be", str(png)])
    return png, colours, w, h


# --------------------------------------------------------------------------
# the move: one implementation, pinned numbers
# --------------------------------------------------------------------------

def test_server_imports_frame_stats_rather_than_defining_it(ctx):
    src = SERVER_PY.read_text()
    ctx.expect_true("server.py imports frame_stats from stats",
                    "from stats import" in src and "frame_stats" in src,
                    "no `from stats import ... frame_stats` line found")
    has_own = "\ndef frame_stats(" in src
    ctx.expect_true("server.py does not carry a second frame_stats",
                    not has_own,
                    "no def frame_stats( found" if not has_own
                    else "found a def frame_stats( in studio/server.py")


def _band_fixture() -> np.ndarray:
    """8 rows of 4 pixels, one row centred in each default luma band.

    Every row is neutral grey at the band's own midpoint except band 2
    (pushed warm: R up, B down by the same amount) and band 6 (pushed cool),
    so the fixture pins a real signed value on top of the neutral zero case.
    """
    mids = [(i + 0.5) / 8.0 for i in range(8)]
    rows = []
    for i, y in enumerate(mids):
        if i == 2:
            r, g, b = y + 0.10, y, y - 0.10
        elif i == 6:
            r, g, b = y - 0.08, y, y + 0.08
        else:
            r = g = b = y
        rows.append(np.tile(np.array([[r, g, b]]) * 255.0, (4, 1)))
    return np.stack(rows, axis=0).astype(np.uint8)          # (8, 4, 3)


def test_frame_stats_matches_a_pinned_fixture(ctx):
    """A fixed synthetic frame's numbers, hand verified once and pinned.

    Not a random array: numpy's RNG algorithm is not part of this project's
    contract, a hand built fixture with known band membership is.
    """
    arr = _band_fixture()
    fs = ST.frame_stats(arr)
    ctx.expect_close("luma mean", fs["luma"]["mean"], 0.4984, 1e-4)
    ctx.expect_close("saturation mean", fs["saturation"]["mean"], 0.0833, 1e-4)
    ctx.expect_close("warm family pct", fs["families"]["warm"], 12.5, 1e-6)
    ctx.expect_close("cool family pct", fs["families"]["cool"], 12.5, 1e-6)
    ctx.expect_close("neutral family pct", fs["families"]["neutral"], 75.0, 1e-6)
    ctx.expect_eq("no clipping in a fixture with no extreme codes",
                  (fs["clipped"]["black"], fs["clipped"]["white"]), (0.0, 0.0))

    want_bands = {
        "edges": [0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875, 1.0],
        "saturation": [0.0, 0.0, 0.4857, 0.0, 0.0, 0.0, 0.1806, 0.0],
        "warm": [0.0, 0.0, 0.2, 0.0, 0.0, 0.0, -0.1608, 0.0],
        "count": [4, 4, 4, 4, 4, 4, 4, 4],
    }
    got_bands = fs["bands"]
    ctx.expect_eq("bands.edges", got_bands["edges"], want_bands["edges"])
    ctx.expect_eq("bands.count: 4 pixels per band, 32 total", got_bands["count"],
                  want_bands["count"])
    for i in range(8):
        ctx.expect_close(f"bands.saturation[{i}]", got_bands["saturation"][i],
                         want_bands["saturation"][i], 1e-3)
        ctx.expect_close(f"bands.warm[{i}]", got_bands["warm"][i],
                         want_bands["warm"][i], 1e-3)
    ctx.note(f"bands.tint stayed near zero on a fixture with no green push: "
             f"{got_bands['tint']}")
    for t in got_bands["tint"]:
        ctx.expect_close("bands.tint is near zero (no green push in this fixture)",
                         t, 0.0, 0.01)


# --------------------------------------------------------------------------
# no coverage, and the coverage number itself (checkpoint gap 23)
# --------------------------------------------------------------------------

def test_zero_weight_is_a_no_coverage_row_not_an_error(ctx):
    """A matte covering nothing in this frame is a NORMAL outcome.

    A sky matte after the camera tilts down genuinely has no sky, and a
    person matte inside its own tracking gap genuinely has nobody. This used
    to raise StatsError, so a script measuring a dozen timestamps died on the
    first empty one and lost every row it had not written yet (which is what
    happened to a round 4 measurement helper). It now returns a row that says
    so, and the numbers are None rather than zero: a zero here would read as
    a real measurement of a black frame.
    """
    arr = _band_fixture()
    out = ST.frame_stats(arr, weight=np.zeros(arr.shape[:2]))
    ctx.expect_eq("no_coverage flag is set", out["no_coverage"], True)
    ctx.expect_eq("coverage is exactly zero", out["coverage"], 0.0)
    for name in ST.MEASURED_BLOCKS:
        ctx.expect_true(f"{name} is present and None, not missing and not 0",
                        name in out and out[name] is None, repr(out.get(name)))
    ctx.expect_true("definitions survive (they describe the formula, not "
                    "this frame)", "families" in out["definitions"],
                    sorted(out["definitions"]))
    ctx.expect_eq("no_coverage_result() is the documented shape this returns",
                  set(out), set(ST.no_coverage_result()))


def test_a_weighted_measurement_says_how_much_it_covered(ctx):
    """`coverage` is the mean of the weight: what share of the frame the mask
    covers, counting a half lit pixel as half. On an unweighted call neither
    key appears at all, so every envelope written before this is unchanged."""
    arr = _band_fixture()
    h, w = arr.shape[:2]
    plain = ST.frame_stats(arr)
    ctx.expect_true("an unweighted answer carries no coverage keys",
                    "coverage" not in plain and "no_coverage" not in plain,
                    sorted(plain))
    solid = ST.frame_stats(arr, weight=np.ones((h, w)))
    ctx.expect_eq("a weight of all ones covers the whole frame",
                  solid["coverage"], 1.0)
    ctx.expect_eq("and is not flagged as empty", solid["no_coverage"], False)
    top = np.zeros((h, w))
    top[:h // 2] = 1.0
    half = ST.frame_stats(arr, weight=top)
    ctx.expect_close("half the frame, solid, reads 0.5", half["coverage"],
                     0.5, 1e-9)
    faint = ST.frame_stats(arr, weight=np.full((h, w), 0.5))
    ctx.expect_close("the whole frame at half strength also reads 0.5 (the "
                     "same weight, spread differently)", faint["coverage"],
                     0.5, 1e-9)
    ctx.expect_true("the two 0.5 coverages measured different pixels, so the "
                    "numbers themselves differ",
                    half["luma"]["mean"] != faint["luma"]["mean"],
                    (half["luma"]["mean"], faint["luma"]["mean"]))


def _detail_fixture() -> tuple[np.ndarray, np.ndarray]:
    """A 16x16 grey frame with an 8x8 checkerboard region, and its mask.

    Inside the region every pixel is either 0.2 or 0.8 and none is near the
    middle, so the region's mean sits at 0.5 with nothing at 0.5. Outside it
    the frame is flat 0.5 apart from one black and one white pixel, which are
    there to prove the masked figures come from the mask: the extremes of the
    frame are 0.0 and 1.0 and the extremes of the region are 0.2 and 0.8.
    """
    h = w = 16
    y = np.full((h, w), 0.5)
    y[0, 0] = 0.0
    y[0, 1] = 1.0
    tile = np.indices((8, 8)).sum(axis=0) % 2
    y[4:12, 4:12] = np.where(tile == 0, 0.2, 0.8)
    mask = np.zeros((h, w))
    mask[4:12, 4:12] = 1.0
    return y, mask


def _to_rgb(y: np.ndarray) -> np.ndarray:
    """A grey frame as rgb24. Grey means the luma weights sum onto the same
    value, so the luma of each pixel is exactly the number written here."""
    return np.round(np.clip(y, 0.0, 1.0) * 255.0).astype(np.uint8)[..., None] \
        .repeat(3, axis=-1)


def test_luma_spread_reports_detail_the_mean_cannot_see(ctx):
    """Tooling gap 25: a strong contrast reduction with the mean standing still.

    A per layer contrast below 1 pulls every pixel toward the pivot
    (`cor' = pivot + (cor - pivot) * contrast`, the arithmetic in
    cinegrade.py's layer stage), so with the pivot at the region's own mean
    the mean does not move at all and the internal variation goes. Measured
    through the matte, the row a grader read carried percentiles and a mean,
    and the mean was identical before and after: the number that changed was
    not on the row. `std` and `p5_p95` are that number, on the same basis the
    mean already had.

    Measurement only: nothing here says a spread of 0.06 is too little or
    that 0.6 was right, only that the two readings differ and by how much.
    """
    y, mask = _detail_fixture()
    pivot = 0.5
    flat = pivot + (y - pivot) * 0.1
    before = ST.frame_stats(_to_rgb(y), weight=mask)
    after = ST.frame_stats(_to_rgb(flat), weight=mask)

    ctx.expect_close("the masked mean before the reduction",
                     before["luma"]["mean"], 0.5, 1e-3)
    ctx.expect_close("the masked mean after it: the same number, which is "
                     "why this change was invisible on the old row",
                     after["luma"]["mean"], before["luma"]["mean"], 2e-3)
    ctx.expect_close("the masked standard deviation before", before["luma"]["std"],
                     0.3, 1e-3)
    ctx.expect_lt("and after: the detail inside the mask is gone",
                  after["luma"]["std"], before["luma"]["std"] / 5.0)
    ctx.expect_close("the masked p5 to p95 spread before",
                     before["luma"]["p5_p95"], 0.6, 1e-3)
    ctx.expect_lt("and after", after["luma"]["p5_p95"],
                  before["luma"]["p5_p95"] / 5.0)
    ctx.note(f"masked luma: mean {before['luma']['mean']} -> "
             f"{after['luma']['mean']}, sd {before['luma']['std']} -> "
             f"{after['luma']['std']}, p5_p95 {before['luma']['p5_p95']} -> "
             f"{after['luma']['p5_p95']}")

    # The two figures are on the mask's basis, not the frame's, the same as
    # every other number in a weighted answer.
    whole = ST.frame_stats(_to_rgb(y))
    ctx.expect_true("an unweighted call carries them too",
                    whole["luma"]["std"] is not None
                    and whole["luma"]["p5_p95"] is not None, whole["luma"])
    ctx.expect_true("and reads a different spread, because it measured "
                    "different pixels",
                    whole["luma"]["std"] != before["luma"]["std"],
                    (whole["luma"]["std"], before["luma"]["std"]))
    solid = ST.frame_stats(_to_rgb(y), weight=np.ones(y.shape))
    ctx.expect_close("a weight of all ones reports the unweighted standard "
                     "deviation (one formula, two paths, same number)",
                     solid["luma"]["std"], whole["luma"]["std"], 1e-4)

    # Found while adding the above: min and max were the whole frame's under
    # a weight, in a block where every other figure was the mask's.
    ctx.expect_close("the masked min is the region's darkest pixel, not the "
                     "frame's", before["luma"]["min"], 0.2, 1e-3)
    ctx.expect_close("the masked max is the region's brightest, not the "
                     "frame's", before["luma"]["max"], 0.8, 1e-3)
    ctx.expect_close("and the unweighted min is still the frame's own",
                     whole["luma"]["min"], 0.0, 1e-6)
    ctx.expect_close("and the unweighted max likewise",
                     whole["luma"]["max"], 1.0, 1e-6)


def _feather_fixture():
    """A letterboxed frame, a mask on the subject, and that mask feathered.

    32 rows of 64: a black bar over the top two rows and a white bar over the
    bottom two (the letterbox), mid grey between them, and a subject whose own
    luma ramps 0.35 to 0.65 so its extremes are nowhere near the bars'. The
    feather is the engine's own (`mask_blur_sigma` then `gaussian_blur2d`, the
    definition the render, the CLI and studio/static/gpu.js share), so the
    weights outside the shape are the ones a graded frame really carries
    rather than numbers picked here to make a point.
    """
    h, w = 32, 64
    y = np.full((h, w), 0.5)
    y[:2, :] = 0.0
    y[h - 2:, :] = 1.0
    subject = np.zeros((h, w))
    subject[11:21, 20:44] = 1.0
    y[11:21, 20:44] = np.tile(np.linspace(0.35, 0.65, 24), (10, 1))
    sigma = H.cg.mask_blur_sigma(0.05, w)
    return y, subject, H.cg.gaussian_blur2d(subject, sigma), sigma


def test_feathered_min_and_max_read_the_mask_not_its_outer_tail(ctx):
    """Round 4 finding 90: a feather's tail is not a pixel the mask selects.

    Narrowing `luma.min`/`luma.max` to the weighted pixels (gap 25 above) used
    a hard support, any weight over zero. A feather is a gaussian whose kernel
    reaches three sigma, so the support of a feathered mask is wider than the
    shape by that much, and a black bar the mask does not cover came back as
    `min` 0.0 through a weight of a thousandth: the one figure a single pixel
    can carry was the only one in the block that was not weight proportional.
    The basis is `MASK_CORE_WEIGHT` and above, which for a gaussian is the
    shape that was feathered.

    Measurement only: this says which pixels the two numbers came from, not
    that a feather of 0.05 or a subject at 0.5 is the right thing to grade.
    """
    y, subject, soft, sigma = _feather_fixture()
    yq = _to_rgb(y)[..., 0] / 255.0        # what frame_stats actually sees
    hard = ST.frame_stats(_to_rgb(y), weight=subject)
    feathered = ST.frame_stats(_to_rgb(y), weight=soft)

    bars = np.zeros(y.shape, dtype=bool)
    bars[:2, :] = True
    bars[y.shape[0] - 2:, :] = True
    ctx.expect_true("the feather really does reach the bars: they carry a "
                    "weight above zero, which is all the old rule asked for",
                    float(soft[bars].max()) > 0.0,
                    f"sigma {sigma}, largest weight on a bar "
                    f"{float(soft[bars].max()):.3e}")
    ctx.expect_lt("and that weight is a rounding error of the total, so the "
                  "mean, the spread and the percentiles cannot feel it",
                  float(soft[bars].sum() / soft.sum()), 1e-3)
    ctx.expect_close("which the mean shows: feathering the mask moves it by "
                     "almost nothing",
                     feathered["luma"]["mean"], hard["luma"]["mean"], 3e-3)

    ctx.expect_eq("MASK_CORE_WEIGHT is the documented half weight",
                  ST.MASK_CORE_WEIGHT, 0.5)
    core = soft >= ST.MASK_CORE_WEIGHT
    # Tolerance is the row's own rounding: this block reports four places.
    ctx.expect_close("the feathered min is the darkest pixel of the mask's "
                     "core", feathered["luma"]["min"],
                     float(yq[core].min()), 1e-4)
    ctx.expect_close("and the feathered max its brightest",
                     feathered["luma"]["max"], float(yq[core].max()), 1e-4)
    ctx.expect_true("so neither is a bar's: the support's own extremes are "
                    "0.0 and 1.0 and the row reports neither",
                    feathered["luma"]["min"] > 0.0
                    and feathered["luma"]["max"] < 1.0,
                    f"support {float(yq[soft > 0].min())} to "
                    f"{float(yq[soft > 0].max())}, reported "
                    f"{feathered['luma']['min']} to "
                    f"{feathered['luma']['max']}")
    ctx.note(f"hard mask min/max {hard['luma']['min']}/{hard['luma']['max']}, "
             f"feathered {feathered['luma']['min']}/"
             f"{feathered['luma']['max']}, feather support "
             f"{float(yq[soft > 0].min())}/{float(yq[soft > 0].max())}")

    # A feather wider than the shape it feathers never reaches the core at
    # all. That is a mask, not an error, so the two figures fall back to the
    # pixels this weight holds above every other.
    line = np.zeros(y.shape)
    line[11, 20:44] = 1.0
    faint = H.cg.gaussian_blur2d(line, sigma)
    ctx.expect_lt("a one row mask feathered this far never reaches the core",
                  float(faint.max()), ST.MASK_CORE_WEIGHT)
    weak = ST.frame_stats(_to_rgb(y), weight=faint)
    peak = faint >= float(faint.max())
    ctx.expect_close("its min is the darkest of the pixels it weighs most",
                     weak["luma"]["min"], float(yq[peak].min()), 1e-4)
    ctx.expect_close("and its max the brightest of them",
                     weak["luma"]["max"], float(yq[peak].max()), 1e-4)
    ctx.expect_true("still not the bar it reaches, and still a number the "
                    "mask can account for",
                    0.0 < weak["luma"]["min"] <= weak["luma"]["max"] < 1.0,
                    f"{weak['luma']['min']} to {weak['luma']['max']} from "
                    f"{int(peak.sum())} pixels, support "
                    f"{float(yq[faint > 0].min())} to "
                    f"{float(yq[faint > 0].max())}")


def test_a_bad_weight_shape_is_still_an_error(ctx):
    """Gap 23 turned an empty matte into a row, not every weight problem into
    one: a weight that is not the frame's size is a caller bug, and nothing a
    frame can honestly be, so it still raises."""
    arr = _band_fixture()
    try:
        ST.frame_stats(arr, weight=np.ones((3, 3)))
        ctx.check(False, "a mismatched weight shape was accepted")
    except ST.StatsError as exc:
        ctx.expect_true("the message names both shapes",
                        "does not match the frame" in str(exc), str(exc))


def test_a_weight_that_is_not_a_number_is_refused_by_name(ctx):
    """Round 5 finding 103: a NaN in the weight used to raise out of the middle
    of the luma block.

    `w.sum()` is NaN and `NaN <= 0.0` is False, so the no-coverage answer is
    skipped; `w.max()` is NaN and every comparison against NaN is False, so the
    core selection is empty and `min` over nothing raises `zero-size array to
    reduction operation minimum`, which names nothing a caller can act on. A
    weight that is not a number is a caller bug like a wrong shape, and is
    refused the same way, before any figure is computed: every percentage in
    the row would have a NaN total under it, not only the two extremes that
    happened to raise.
    """
    arr = _band_fixture()
    w = np.ones(arr.shape[:2], dtype=np.float64)
    w[0, 0] = np.nan
    try:
        ST.frame_stats(arr, weight=w)
        ctx.check(False, "a weight holding a NaN was accepted")
    except ST.StatsError as exc:
        ctx.expect_true("the message says the weight is not a finite number",
                        "not a finite number" in str(exc), str(exc))
        ctx.expect_true("and separates it from the mask that selects nothing, "
                        "which is a row rather than an error",
                        "no_coverage" in str(exc), str(exc))
    except Exception as exc:                                   # noqa: BLE001
        ctx.check(False, f"{type(exc).__name__} rather than a StatsError: "
                         f"{exc}")
    # And the neighbouring case still answers with a row: a weight of zeros
    # selects nothing, which is something a frame can honestly be.
    empty = ST.frame_stats(arr, weight=np.zeros(arr.shape[:2]))
    ctx.expect_true("a weight of zeros is still no_coverage, not an error",
                    empty.get("no_coverage") is True, str(empty)[:120])


def test_bands_neutral_rows_are_exactly_zero(ctx):
    """The six untouched rows are grey: warm and tint read exactly 0.0, not
    merely small, proving the neutral case is not just "close" by luck."""
    arr = _band_fixture()
    b = ST.bands(arr)
    for i in (0, 1, 3, 4, 5, 7):
        ctx.expect_eq(f"band {i} warm is exactly zero (a grey row)",
                      b["warm"][i], 0.0)
        ctx.expect_eq(f"band {i} tint is exactly zero (a grey row)",
                      b["tint"][i], 0.0)


def test_bands_empty_band_is_zero_not_nan(ctx):
    """A band no pixel falls in reads zero on every field, so a caller can
    print all of them without checking count first."""
    arr = np.zeros((4, 4, 3), dtype=np.uint8)          # every pixel luma 0
    b = ST.bands(arr, edges=[0.0, 0.3, 0.6, 1.0])
    ctx.expect_eq("count: everything in band 0, nothing in 1 or 2",
                  b["count"], [16, 0, 0])
    for i in (1, 2):
        for field in ("saturation", "warm", "tint"):
            v = b[field][i]
            ctx.expect_true(f"band {i} {field} is a real zero, not NaN",
                            v == 0.0 and not (isinstance(v, float) and v != v),
                            repr(v))


def test_bands_needs_at_least_two_edges(ctx):
    arr = np.zeros((2, 2, 3), dtype=np.uint8)
    try:
        ST.bands(arr, edges=[0.5])
        ctx.check(False, "bands accepted a single edge instead of raising")
    except ST.StatsError as exc:
        ctx.expect_true("the message says how many edges it needs",
                        "at least two edges" in str(exc), str(exc))


# --------------------------------------------------------------------------
# decode_image
# --------------------------------------------------------------------------

def test_decode_image_on_a_still(ctx):
    png, colours, w, h = _synthetic_png()
    rgb = ST.decode_image(str(png))
    ctx.expect_eq("decoded size matches the PNG", (rgb.shape[1], rgb.shape[0]),
                  (w, h))
    corners = [rgb[2, 2], rgb[2, w - 3], rgb[h - 3, 2], rgb[h - 3, w - 3]]
    for i, (got, want) in enumerate(zip(corners, colours)):
        want8 = np.round(np.array(want) * 255.0)
        diff = float(np.abs(got.astype(np.float64) - want8).max())
        ctx.expect_le(f"quadrant {i}: within 2 code values of the PNG's own "
                      f"colour after the 16-to-8-bit round trip", diff, 2.0)


def test_decode_image_ignores_time_on_a_still(ctx):
    """A still has one frame and no later one to seek to: the documented
    contract is that `time` changes nothing, and this pins that it really
    does not (a seek that lands past a still's fabricated ~0.04s duration
    used to come back with zero bytes before this was fixed)."""
    png, *_ = _synthetic_png()
    a = ST.decode_image(str(png), time=0.0)
    b = ST.decode_image(str(png), time=5.0)
    ctx.expect_eq("time=0 and time=5 decode the same still identically",
                  a.tobytes(), b.tobytes())


def test_decode_image_region_is_a_slice_of_the_whole_decode(ctx):
    png, *_ = _synthetic_png()
    whole = ST.decode_image(str(png))
    box = [0.0, 0.0, 0.5, 0.5]
    cropped = ST.decode_image(str(png), region=box)
    x, y, cw, ch = H.cg.region_pixels(H.cg.normalise_region(box),
                                      {"width": whole.shape[1],
                                       "height": whole.shape[0]})
    ctx.expect_eq("cropped size matches region_pixels' own arithmetic",
                  (cropped.shape[1], cropped.shape[0]), (cw, ch))
    ctx.expect_eq("cropped pixels are exactly that slice of the whole decode",
                  cropped.tobytes(), whole[y:y + ch, x:x + cw].tobytes())


def test_decode_image_on_a_video_frame(ctx):
    rgb = ST.decode_image(str(H.CLIP_A), width=H.WIDTH, time=H.TIME_A)
    info = H.info_for(H.CLIP_A, H.WIDTH)
    ctx.expect_eq("scaled to the same size probe+width arithmetic gives",
                  (rgb.shape[1], rgb.shape[0]), (info["width"], info["height"]))
    y = ST.frame_stats(rgb)["luma"]["mean"]
    ctx.note(f"a real clip frame is not blank: luma mean {y:.4f}")
    ctx.expect_between("plausible mid-tone (not a black or a blown frame)",
                       y, 0.02, 0.98)


# --------------------------------------------------------------------------
# cinegrade stats
# --------------------------------------------------------------------------

def test_cmd_stats_image_json(ctx):
    png, *_ = _synthetic_png()
    r = _cli(ctx, "stats --image --json",
             ["stats", "--image", str(png), "--json"])
    if r is None:
        return
    out = json.loads(r.stdout)
    ctx.expect_true("has key/size/stats", {"key", "size", "stats"} <= set(out),
                    str(set(out)))
    ctx.expect_true("stats carries bands", "bands" in out["stats"],
                    str(set(out["stats"])))


def test_cmd_stats_clip_and_image_envelopes_agree(ctx):
    """Contract G9 friction 3 and 6: `stats --json` for a single clip frame
    and for a single --image both come back as exactly the route's own
    envelope, {"key", "size", "stats"}, one level of nesting either way.

    Before this test, `_grade_frame_stats` also stamped a "time" key onto
    the singular clip case (needed by --times, which returns a list of
    these), so an agent that read the printed strip first (which never
    showed "time") and then switched to --json for the clip form saw one
    extra key that --image never had. Same key set, same nesting, on two
    different inputs: this is the one place that would show them drifting
    apart again.
    """
    png, *_ = _synthetic_png()
    r_img = _cli(ctx, "stats --image --json (envelope)",
                ["stats", "--image", str(png), "--json"])
    r_clip = _cli(ctx, "stats CLIP --json (envelope)",
                 ["stats", SRC, "-p", "natural", "--time", str(H.TIME_A),
                  "--json"])
    if r_img is None or r_clip is None:
        return
    img_out = json.loads(r_img.stdout)
    clip_out = json.loads(r_clip.stdout)
    ctx.expect_eq("image and clip stats envelopes carry the same keys",
                  set(img_out), set(clip_out))
    # measured_width joined the envelope with checkpoint gap 11: the server
    # route measures a 640 wide preview by default and this CLI measures the
    # source's own resolution, so both now state the width they read rather
    # than leaving it to be inferred from `size`.
    ctx.expect_eq("the envelope is exactly key/size/measured_width/stats",
                  set(clip_out), {"key", "size", "measured_width", "stats"})
    ctx.expect_true("no stray time key on a singular clip measurement",
                    "time" not in clip_out, str(set(clip_out)))


def test_cmd_stats_image_text(ctx):
    png, *_ = _synthetic_png()
    _cli(ctx, "stats --image (text)", ["stats", "--image", str(png)],
         expect_stdout=("luma", "sat", "rgb", "hue", "clipped", "bands"))


def test_cmd_stats_image_still_defaults_to_display_referred(ctx):
    """Round 2 tooling note 1: a still (`--image` on a JPEG/PNG/TIFF/WebP)
    with no --input-space/--working-space is measured as already display
    referred (rec709 in, rec709 working space): the identity, so its
    numbers match the same file measured with those two flags given
    explicitly. Stderr says the assumption was made, rather than leaving it
    silent."""
    png, *_ = _synthetic_png()
    r = subprocess.run([PY, ENGINE, "stats", "--image", str(png), "--json"],
                       capture_output=True, text=True)
    ctx.expect_eq("stats --image (default still): exit code", r.returncode, 0)
    ctx.note(f"stderr: {r.stderr.strip()[:200]}")
    ctx.expect_true("stderr says it measured this as display referred",
                    "display referred" in r.stderr, r.stderr[:300])
    if r.returncode != 0:
        return
    default_out = json.loads(r.stdout)

    r2 = subprocess.run(
        [PY, ENGINE, "stats", "--image", str(png), "--input-space", "rec709",
         "--working-space", "rec709", "--json"],
        capture_output=True, text=True)
    ctx.expect_eq("stats --image --input-space rec709 --working-space rec709: "
                 "exit code", r2.returncode, 0)
    if r2.returncode != 0:
        return
    explicit_out = json.loads(r2.stdout)
    ctx.expect_true(
        "the silent default and the same flags given explicitly measure "
        "the same luma mean",
        abs(default_out["stats"]["luma"]["mean"]
            - explicit_out["stats"]["luma"]["mean"]) < 0.01,
        f"{default_out['stats']['luma']['mean']} vs "
        f"{explicit_out['stats']['luma']['mean']}")


def test_cmd_stats_image_explicit_input_space_changes_the_reading(ctx):
    """Round 2 tooling note 1: --input-space used to have no effect at all
    on `stats --image` (the still was always a raw decode); an explicit
    --input-space now actually runs the convert stage, so a still forced
    through apple_log reads different numbers than the same file measured
    as display referred, and prints no "display referred" note (the flag,
    not the default, decided)."""
    png, *_ = _synthetic_png()
    r_default = subprocess.run(
        [PY, ENGINE, "stats", "--image", str(png), "--json"],
        capture_output=True, text=True)
    r_log = subprocess.run(
        [PY, ENGINE, "stats", "--image", str(png), "--input-space", "apple_log",
         "--json"],
        capture_output=True, text=True)
    ctx.expect_eq("stats --image --input-space apple_log: exit code",
                 r_log.returncode, 0)
    if r_default.returncode != 0 or r_log.returncode != 0:
        return
    default_out = json.loads(r_default.stdout)
    log_out = json.loads(r_log.stdout)
    delta = abs(default_out["stats"]["luma"]["mean"]
               - log_out["stats"]["luma"]["mean"])
    ctx.expect_gt("an explicit --input-space measures different numbers "
                 "than the display-referred default", delta, 0.01)
    ctx.expect_true("no display-referred note once the flag decided it",
                    "display referred" not in r_log.stderr, r_log.stderr[:200])


def test_cmd_stats_region_matches_the_route_contract(ctx):
    """Contract E4 at the CLI: a region measures only that patch, the way
    POST /api/stats already does."""
    def _one(region=None):
        args = ["stats", SRC, "-p", "natural", "--time", str(H.TIME_A), "--json"]
        if region:
            args += ["--region"] + [str(v) for v in region]
        r = _cli(ctx, f"stats region={region}", args)
        return None if r is None else json.loads(r.stdout)

    whole = _one()
    top = _one([0.0, 0.0, 1.0, 0.5])
    bottom = _one([0.0, 0.5, 1.0, 1.0])
    if not (whole and top and bottom):
        return
    a = top["stats"]["luma"]["mean"]
    b = bottom["stats"]["luma"]["mean"]
    mid = whole["stats"]["luma"]["mean"]
    ctx.expect_true("the two halves did not measure identically",
                    abs(a - b) > 1e-4, f"{a} vs {b}")
    ctx.expect_true("the whole frame sits between its own two halves",
                    min(a, b) - 1e-6 <= mid <= max(a, b) + 1e-6,
                    f"{a}, {mid}, {b}")


def test_cmd_stats_times(ctx):
    r = _cli(ctx, "stats --times", ["stats", SRC, "-p", "natural",
                                    "--times", f"0.2,{H.TIME_A}", "--json"])
    if r is None:
        return
    out = json.loads(r.stdout)
    ctx.expect_eq("one row per time, in order",
                  [row["time"] for row in out["results"]], [0.2, H.TIME_A])
    for row in out["results"]:
        ctx.expect_true("every row carries bands", "bands" in row["stats"],
                        str(set(row["stats"])))


def test_cmd_stats_needs_a_clip_or_an_image(ctx):
    _fails(ctx, "stats with neither clip nor --image", ["stats"],
          "needs either a clip, or --image")


def test_cmd_stats_times_and_image_together_is_refused(ctx):
    png, *_ = _synthetic_png()
    _fails(ctx, "stats --image with --times",
          ["stats", "--image", str(png), "--times", "0,1"],
          "measures a clip")


def test_cmd_stats_clip_and_image_together_is_refused(ctx):
    """Round 2 tooling note 4: a positional clip and --image both given used
    to be silently accepted (the --image branch ran and the clip was
    dropped on the floor); one sentence names both instead."""
    png, *_ = _synthetic_png()
    r = subprocess.run([PY, ENGINE, "stats", SRC, "--image", str(png)],
                       capture_output=True, text=True)
    ctx.expect_true("exits non zero", r.returncode != 0, f"exit {r.returncode}")
    ctx.expect_true("stderr is not empty", bool(r.stderr.strip()), repr(r.stderr))
    ctx.expect_true("message names both a clip and --image",
                    "use one or the other" in r.stderr, r.stderr.strip()[:200])


def test_cmd_stats_still_image_positional_is_refused(ctx):
    """Round 2 tooling note 4: a still image format passed as the clip
    positional (no --image) used to silently run through the default Apple
    Log grading path and report wrong numbers for a plain PNG; it is
    refused up front now, pointing at --image."""
    png, *_ = _synthetic_png()
    _fails(ctx, "stats with a still passed positionally",
          ["stats", str(png)], "use --image FILE for a still")


def test_cmd_stats_still_positional_with_times_is_not_an_empty_reason(ctx):
    """The concrete bug this closes: `stats STILL.png --times 0,1` used to
    fail with an EMPTY stderr tail (ffmpeg silently writes 0 bytes seeking
    past a still's one frame, so `_grade_frame_stats`'s own error message
    had nothing to append). The still positional is refused before any of
    that runs now, with the same one sentence a still positional gets
    without --times."""
    png, *_ = _synthetic_png()
    r = subprocess.run([PY, ENGINE, "stats", str(png), "--times", "0,1"],
                       capture_output=True, text=True)
    ctx.expect_true("exits non zero", r.returncode != 0, f"exit {r.returncode}")
    ctx.expect_true("stderr is not empty", bool(r.stderr.strip()), repr(r.stderr))
    ctx.expect_true("message says to use --image for a still",
                    "use --image FILE for a still" in r.stderr,
                    r.stderr.strip()[:200])


# --------------------------------------------------------------------------
# sweep
# --------------------------------------------------------------------------

def test_sweep_two_values_json(ctx):
    r = _cli(ctx, "sweep --json",
             ["sweep", SRC, "-p", "natural", "--time", str(H.TIME_A),
              "--param", "primaries.saturation", "--values", "0.5,1.5",
              "--json"])
    if r is None:
        return
    out = json.loads(r.stdout)
    ctx.expect_eq("param echoed", out["param"], "primaries.saturation")
    ctx.expect_eq("one row per value", [row["value"] for row in out["results"]],
                  [0.5, 1.5])
    lo = out["results"][0]["stats"]["saturation"]["mean"]
    hi = out["results"][1]["stats"]["saturation"]["mean"]
    ctx.note(f"saturation.mean at 0.5 -> {lo:.4f}, at 1.5 -> {hi:.4f}")
    ctx.expect_gt("more primaries.saturation measures more saturation "
                  "(a real engine effect, not a synthetic one)", hi, lo)
    for row in out["results"]:
        ctx.expect_true("every sweep row carries bands",
                        "bands" in row["stats"], str(set(row["stats"])))


def test_sweep_sheet_writes_a_labelled_comparison(ctx):
    out = H.WORK / "sweep_sheet.jpg"
    r = _cli(ctx, "sweep --sheet",
             ["sweep", SRC, "-p", "natural", "--time", str(H.TIME_A),
              "--param", "primaries.saturation", "--values", "0.5,1.0,1.5",
              "--sheet", str(out)],
             expect_stdout=("sweep sheet",))
    if r is None:
        return
    ctx.expect_true("the sheet file exists", out.exists(), str(out))
    if not out.exists():
        return
    st = H.ffprobe_stream(out, "width,height")
    w, h = int(st.get("width", 0)), int(st.get("height", 0))
    ctx.note(f"sweep sheet: {w}x{h}, {out.stat().st_size} bytes")
    ctx.expect_gt("three panels side by side is wide", float(w), float(h))


def test_sweep_negative_leading_value_without_quoting(ctx):
    """Round 2 tooling note 3: `--values -0.1,0,0.1` as two shell words
    used to be refused by argparse as an unrecognised flag; it is read as
    the value now, the same as the already-working `--values=-0.1,0,0.1`
    spelling."""
    r = subprocess.run(
        [PY, ENGINE, "sweep", SRC, "-p", "natural", "--time", str(H.TIME_A),
         "--param", "primaries.temperature", "--values", "-0.1,0,0.1", "--json"],
        capture_output=True, text=True)
    ctx.expect_eq("sweep --values -0.1,0,0.1 (unquoted): exit code",
                 r.returncode, 0)
    if r.returncode != 0:
        ctx.note(f"stderr: {r.stderr.strip()[:300]}")
        return
    out = json.loads(r.stdout)
    ctx.expect_eq("values round trip in order, negative included",
                  [row["value"] for row in out["results"]], [-0.1, 0, 0.1])


def test_sweep_json_sheet_stdout_stays_pure_json(ctx):
    """Round 2 tooling note 3: with --sheet, --json used to print a text
    line ("sweep sheet (...) -> OUT.jpg") after the JSON block, which broke
    a caller doing json.loads(stdout). That line now goes to stderr."""
    out = H.WORK / "sweep_sheet_json.jpg"
    r = subprocess.run(
        [PY, ENGINE, "sweep", SRC, "-p", "natural", "--time", str(H.TIME_A),
         "--param", "primaries.saturation", "--values", "0.5,1.5",
         "--json", "--sheet", str(out)],
        capture_output=True, text=True)
    ctx.expect_eq("sweep --json --sheet: exit code", r.returncode, 0)
    if r.returncode != 0:
        return
    parsed = None
    try:
        parsed = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        ctx.note(f"stdout: {r.stdout.strip()[-300:]}")
        ctx.check(False, f"stdout is not pure JSON: {exc}")
        return
    ctx.expect_true("stdout parsed as JSON with the sweep results",
                    "results" in parsed, str(set(parsed)))
    ctx.expect_true("the sheet path note went to stderr instead",
                    "sweep sheet" in r.stderr, r.stderr.strip()[:200])
    ctx.expect_true("the sheet file exists", out.exists(), str(out))


def test_sweep_measures_at_640_wide_by_default(ctx):
    """Round 2 tooling note 3: sweep used to always measure at the source's
    full resolution; the default is now 640 wide, the same as what POST
    /api/stats measures a clip at."""
    r = _cli(ctx, "sweep (default width)",
             ["sweep", SRC, "-p", "natural", "--time", str(H.TIME_A),
              "--param", "primaries.saturation", "--values", "1.0", "--json"])
    if r is None:
        return
    out = json.loads(r.stdout)
    w, h = out["results"][0]["size"]
    ctx.expect_eq("measured width is 640 by default", w, 640)
    ctx.expect_true("height came down with it, not full 4K",
                    h < 2000, f"{w}x{h}")


def test_sweep_width_flag_changes_the_measured_size(ctx):
    r = _cli(ctx, "sweep --width 320",
             ["sweep", SRC, "-p", "natural", "--time", str(H.TIME_A),
              "--param", "primaries.saturation", "--values", "1.0",
              "--width", "320", "--json"])
    if r is None:
        return
    out = json.loads(r.stdout)
    w, _h = out["results"][0]["size"]
    ctx.expect_eq("--width is honoured", w, 320)


def register(suite):
    g = "stats"
    suite.add(g, "server_imports_frame_stats_rather_than_defining_it",
              test_server_imports_frame_stats_rather_than_defining_it,
              doc="one implementation, not a second copy in server.py")
    suite.add(g, "frame_stats_matches_a_pinned_fixture",
              test_frame_stats_matches_a_pinned_fixture,
              doc="a fixed synthetic frame's numbers, hand verified and pinned")
    suite.add(g, "zero_weight_is_a_no_coverage_row_not_an_error",
              test_zero_weight_is_a_no_coverage_row_not_an_error,
              doc="an empty matte reports no coverage instead of raising")
    suite.add(g, "a_weighted_measurement_says_how_much_it_covered",
              test_a_weighted_measurement_says_how_much_it_covered,
              doc="coverage is the mean weight; absent on an unweighted call")
    suite.add(g, "luma_spread_reports_detail_the_mean_cannot_see",
              test_luma_spread_reports_detail_the_mean_cannot_see,
              doc="std and p5_p95 move when a contrast reduction leaves the "
                  "mean where it was (gap 25)")
    suite.add(g, "feathered_min_and_max_read_the_mask_not_its_outer_tail",
              test_feathered_min_and_max_read_the_mask_not_its_outer_tail,
              doc="a feather's outer tail does not decide min and max "
                  "(round 4 finding 90)")
    suite.add(g, "a_bad_weight_shape_is_still_an_error",
              test_a_bad_weight_shape_is_still_an_error,
              doc="a wrong sized weight is a caller bug and still raises")
    suite.add(g, "a_weight_that_is_not_a_number_is_refused_by_name",
              test_a_weight_that_is_not_a_number_is_refused_by_name,
              doc="a NaN weight is a named refusal, not a ValueError out of "
                  "the luma block (round 5 finding 103)")
    suite.add(g, "bands_neutral_rows_are_exactly_zero",
              test_bands_neutral_rows_are_exactly_zero,
              doc="a grey row reads exactly 0.0 warm and tint")
    suite.add(g, "bands_empty_band_is_zero_not_nan",
              test_bands_empty_band_is_zero_not_nan,
              doc="a band with no pixels reads zero on every field")
    suite.add(g, "bands_needs_at_least_two_edges",
              test_bands_needs_at_least_two_edges,
              doc="a bad edges list is refused by name")
    suite.add(g, "decode_image_on_a_still",
              test_decode_image_on_a_still,
              doc="decodes a still to the colours it actually has")
    suite.add(g, "decode_image_ignores_time_on_a_still",
              test_decode_image_ignores_time_on_a_still,
              doc="a seek past a still's one frame changes nothing")
    suite.add(g, "decode_image_region_is_a_slice_of_the_whole_decode",
              test_decode_image_region_is_a_slice_of_the_whole_decode,
              doc="region crop matches region_pixels' own arithmetic exactly")
    suite.add(g, "decode_image_on_a_video_frame",
              test_decode_image_on_a_video_frame,
              doc="a real clip frame decodes to the probed, scaled size")
    suite.add(g, "cmd_stats_image_json",
              test_cmd_stats_image_json,
              doc="--image --json prints the same shape the route returns")
    suite.add(g, "cmd_stats_clip_and_image_envelopes_agree",
              test_cmd_stats_clip_and_image_envelopes_agree,
              doc="a clip frame and a still both come back key/size/stats, "
                  "no stray time key on the singular clip case")
    suite.add(g, "cmd_stats_image_text",
              test_cmd_stats_image_text,
              doc="the default text is the studio strip, not signalstats")
    suite.add(g, "cmd_stats_image_still_defaults_to_display_referred",
              test_cmd_stats_image_still_defaults_to_display_referred,
              doc="a JPEG/PNG/TIFF/WebP --image with no space flags reads "
                  "as rec709 (identity) and says so on stderr")
    suite.add(g, "cmd_stats_image_explicit_input_space_changes_the_reading",
              test_cmd_stats_image_explicit_input_space_changes_the_reading,
              doc="an explicit --input-space on --image now actually runs "
                  "the convert stage")
    suite.add(g, "cmd_stats_region_matches_the_route_contract",
              test_cmd_stats_region_matches_the_route_contract,
              doc="contract E4 holds at the CLI too")
    suite.add(g, "cmd_stats_times",
              test_cmd_stats_times,
              doc="--times measures a clip at each one and lists the rows")
    suite.add(g, "cmd_stats_needs_a_clip_or_an_image",
              test_cmd_stats_needs_a_clip_or_an_image,
              doc="a user error is a message, not a traceback")
    suite.add(g, "cmd_stats_times_and_image_together_is_refused",
              test_cmd_stats_times_and_image_together_is_refused,
              doc="--times measures a clip; --image is one still")
    suite.add(g, "cmd_stats_clip_and_image_together_is_refused",
              test_cmd_stats_clip_and_image_together_is_refused,
              doc="a clip positional and --image together name both in "
                  "one sentence, rather than silently dropping the clip")
    suite.add(g, "cmd_stats_still_image_positional_is_refused",
              test_cmd_stats_still_image_positional_is_refused,
              doc="a still passed as the clip positional points at --image "
                  "instead of quietly grading it as video")
    suite.add(g, "cmd_stats_still_positional_with_times_is_not_an_empty_reason",
              test_cmd_stats_still_positional_with_times_is_not_an_empty_reason,
              doc="the concrete empty-stderr bug this closes")
    suite.add(g, "sweep_two_values_json",
              test_sweep_two_values_json,
              doc="one row per value, reporting a real engine effect")
    suite.add(g, "sweep_sheet_writes_a_labelled_comparison",
              test_sweep_sheet_writes_a_labelled_comparison,
              doc="--sheet reuses cinegrade sheet's own contact sheet code")
    suite.add(g, "sweep_negative_leading_value_without_quoting",
              test_sweep_negative_leading_value_without_quoting,
              doc="--values -0.1,0,0.1 as two shell words works unquoted")
    suite.add(g, "sweep_json_sheet_stdout_stays_pure_json",
              test_sweep_json_sheet_stdout_stays_pure_json,
              doc="--json --sheet: stdout is JSON only, the sheet note "
                  "goes to stderr")
    suite.add(g, "sweep_measures_at_640_wide_by_default",
              test_sweep_measures_at_640_wide_by_default,
              doc="sweep no longer measures at the source's full resolution")
    suite.add(g, "sweep_width_flag_changes_the_measured_size",
              test_sweep_width_flag_changes_the_measured_size,
              doc="--width overrides the 640 default")
