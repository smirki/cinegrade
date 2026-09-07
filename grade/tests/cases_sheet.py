"""Group: `cinegrade sheet` (G6), the labelled comparison image.

Ten of ten bakeoff lanes built this by hand; three broke it on mismatched
panel heights. These tests cover a plain strip of three mixed-aspect stills,
a --grid layout, a video frame decoded through the same command, and the
docs command's heading lookup.
"""

from __future__ import annotations

import json
import subprocess

import harness as H

PY = str(H.CONTENT / ".venv" / "bin" / "python")
ENGINE = str(H.GRADE / "cinegrade.py")
REFS = H.CONTENT / "refs"
SRC = str(H.CLIP_A)


def _cli(args):
    return subprocess.run([PY, ENGINE] + args, capture_output=True, text=True)


def _dims(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(path)],
        capture_output=True, text=True)
    st = json.loads(r.stdout).get("streams") or [{}]
    return int(st[0].get("width", 0)), int(st[0].get("height", 0))


def _mixed_aspect_inputs(ctx):
    """Three real stills with three different aspect ratios, already in the
    repo's refs/ (never written to or deleted here)."""
    wide = REFS / "bakeoff-asteroid-city.jpg"       # 1920x1280, 3:2
    tall = REFS / "small_IMG_2562.png"              # 322x700, portrait
    huge = REFS / "nature_cinema_sunrise.jpg"       # 5612x3741, landscape
    for p in (wide, tall, huge):
        ctx.expect_true(f"fixture exists: {p.name}", p.exists(), str(p))
    return [wide, tall, huge]


def test_mixed_aspect_strip_with_labels(ctx):
    a, b, c = _mixed_aspect_inputs(ctx)
    out = H.WORK / "sheet_strip.jpg"
    r = _cli(["sheet", str(a), str(b), str(c), "-o", str(out),
             "--height", "300", "--labels", "wide,tall,huge"])
    ctx.note(f"exit {r.returncode}, stdout {r.stdout.strip()}, "
            f"stderr {r.stderr.strip()[-300:]}")
    ctx.expect_eq("sheet exits 0 on mixed aspect inputs", r.returncode, 0)
    if r.returncode != 0 or not out.exists():
        return
    w, h = _dims(out)
    ctx.note(f"sheet: {w}x{h}, {out.stat().st_size} bytes")
    # Three panels, each height 300 plus one label strip; the label strip
    # is the same for all three, so the whole sheet is exactly one height.
    ctx.expect_gt("sheet has real width", float(w), 300.0)
    ctx.expect_gt("sheet is taller than the bare panel height "
                  "(a label strip was added)", float(h), 300.0)


def test_grid_layout(ctx):
    a, b, c = _mixed_aspect_inputs(ctx)
    d = a  # a fourth panel, reusing one of the three is fine for a grid test
    out = H.WORK / "sheet_grid.jpg"
    r = _cli(["sheet", str(a), str(b), str(c), str(d), "-o", str(out),
             "--grid", "2x2", "--width", "1200"])
    ctx.note(f"exit {r.returncode}, stdout {r.stdout.strip()}")
    ctx.expect_eq("grid sheet exits 0", r.returncode, 0)
    if r.returncode != 0 or not out.exists():
        return
    w, h = _dims(out)
    ctx.note(f"grid sheet: {w}x{h}")
    ctx.expect_ge("grid sheet is at least the asked width", float(w), 1190.0)
    ctx.expect_gt("grid sheet has two rows worth of height", float(h),
                  float(w) / 4)


def test_video_frame_input(ctx):
    """A clip alongside a still: the frame is decoded through one bounded
    ffmpeg call, not through PIL trying (and failing) to open a .MOV."""
    a, _, _ = _mixed_aspect_inputs(ctx)
    out = H.WORK / "sheet_video.jpg"
    r = _cli(["sheet", SRC, str(a), "-o", str(out), "--height", "240",
             "--time", "0.5"])
    ctx.note(f"exit {r.returncode}, stderr {r.stderr.strip()[-300:]}")
    ctx.expect_eq("sheet with a video input exits 0", r.returncode, 0)
    if r.returncode == 0 and out.exists():
        w, h = _dims(out)
        ctx.expect_gt("video+still sheet has real width", float(w), 0.0)


def test_grid_bad_spec_fails_cleanly(ctx):
    r = _cli(["sheet", str(REFS / "bakeoff-asteroid-city.jpg"),
             "-o", str(H.WORK / "never_grid.jpg"), "--grid", "not-a-grid"])
    ctx.expect_true("a bad --grid exits non-zero", r.returncode != 0,
                    f"exit {r.returncode}")
    ctx.expect_true("message names --grid", "--grid" in r.stderr,
                    r.stderr.strip())


def test_labels_count_mismatch_fails_cleanly(ctx):
    a, b, _ = _mixed_aspect_inputs(ctx)
    r = _cli(["sheet", str(a), str(b), "-o", str(H.WORK / "never_labels.jpg"),
             "--labels", "onlyone"])
    ctx.expect_true("a labels count mismatch exits non-zero",
                    r.returncode != 0, f"exit {r.returncode}")
    ctx.expect_true("message states both counts",
                    "1" in r.stderr and "2" in r.stderr, r.stderr.strip())


def test_height_width_mutually_exclusive(ctx):
    a, _, _ = _mixed_aspect_inputs(ctx)
    r = _cli(["sheet", str(a), "-o", str(H.WORK / "never_size.jpg"),
             "--height", "100", "--width", "200"])
    ctx.expect_true("--height and --width together exits non-zero",
                    r.returncode != 0, f"exit {r.returncode}")


def test_docs_known_heading(ctx):
    r = _cli(["docs", "layers"])
    ctx.expect_eq("docs layers: exit code", r.returncode, 0)
    ctx.expect_true("prints the Layers heading", "## Layers" in r.stdout,
                    r.stdout[:200])
    ctx.expect_true("prints body text under it",
                    "mask" in r.stdout.lower(), r.stdout[:400])


def test_docs_case_insensitive_multi_word(ctx):
    r = _cli(["docs", "match reference"])
    ctx.expect_eq("docs 'match reference': exit code", r.returncode, 0)
    ctx.expect_true("matched the heading case-insensitively",
                    "Match Reference" in r.stdout, r.stdout[:200])


def test_docs_miss_lists_sections(ctx):
    r = _cli(["docs", "this heading does not exist anywhere"])
    ctx.expect_true("a miss does not crash", r.returncode == 0,
                    f"exit {r.returncode}")
    ctx.expect_true("says no section matched",
                    "no section named" in r.stdout, r.stdout[:200])
    ctx.expect_true("lists real headings instead",
                    "Layers" in r.stdout or "Run it" in r.stdout,
                    r.stdout[:400])


def test_docs_list(ctx):
    r = _cli(["docs", "--list"])
    ctx.expect_eq("docs --list: exit code", r.returncode, 0)
    ctx.expect_true("lists more than a couple of headings",
                    len(r.stdout.strip().splitlines()) > 5,
                    f"{len(r.stdout.strip().splitlines())} lines")
    ctx.expect_true("does not pick up a shell comment inside a code fence "
                    "as a heading",
                    "password:" not in r.stdout, r.stdout[:200])


def register(suite):
    g = "sheet"
    suite.add(g, "mixed_aspect_strip_with_labels",
              test_mixed_aspect_strip_with_labels,
              doc="a common height, mixed aspect ratios, labels above each panel")
    suite.add(g, "grid_layout", test_grid_layout,
              doc="--grid COLSxROWS with --width for the whole sheet")
    suite.add(g, "video_frame_input", test_video_frame_input,
              doc="a video input decodes one frame through a bounded ffmpeg call")
    suite.add(g, "grid_bad_spec_fails_cleanly", test_grid_bad_spec_fails_cleanly,
              doc="a malformed --grid is a message, not a traceback")
    suite.add(g, "labels_count_mismatch_fails_cleanly",
              test_labels_count_mismatch_fails_cleanly,
              doc="a --labels count that does not match the inputs is refused")
    suite.add(g, "height_width_mutually_exclusive",
              test_height_width_mutually_exclusive,
              doc="--height and --width together is a usage error")
    suite.add(g, "docs_known_heading", test_docs_known_heading,
              doc="docs prints one section by heading name")
    suite.add(g, "docs_case_insensitive_multi_word",
              test_docs_case_insensitive_multi_word,
              doc="docs matches a multi-word heading case-insensitively")
    suite.add(g, "docs_miss_lists_sections", test_docs_miss_lists_sections,
              doc="a miss lists the real headings instead of failing")
    suite.add(g, "docs_list", test_docs_list,
              doc="docs --list enumerates every heading, ignoring code-fence "
                  "comments that only look like headings")
