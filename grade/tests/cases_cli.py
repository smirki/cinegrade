"""Group 7: every CLI subcommand runs and writes something valid.

A smoke test, not a picture test. What it catches is the class of breakage
where a graph change is fine for `still` and leaves `scopes` or `compare`
unable to configure its filter chain, which is easy to miss because those two
are the commands a human runs and an agent does not.

Every output is checked for being a real decodable image with sensible
dimensions, not merely for existing: a zero-byte or truncated PNG is exactly
what a half-failed ffmpeg run leaves behind.
"""

from __future__ import annotations

import subprocess

import harness as H

PY = str(H.CONTENT / ".venv" / "bin" / "python")
ENGINE = str(H.GRADE / "cinegrade.py")
SRC = str(H.CLIP_A)


def _cli(ctx, label, args, expect_stdout=()):
    r = subprocess.run([PY, ENGINE] + args, capture_output=True, text=True)
    ok = ctx.expect_eq(f"{label}: exit code", r.returncode, 0)
    if not ok:
        ctx.note(f"{label} stderr: {r.stderr[-900:]}")
        return None
    ctx.expect_gt(f"{label}: wrote something to stdout",
                  float(len(r.stdout.strip())), 0.0)
    for token in expect_stdout:
        ctx.expect_true(f"{label}: stdout mentions {token!r}", token in r.stdout,
                        "found" if token in r.stdout else f"got: {r.stdout[:200]!r}")
    return r


def _check_image(ctx, label, path, min_w=1, min_h=1):
    if not path.exists():
        ctx.check(False, f"{label}: {path} was not written")
        return
    size = path.stat().st_size
    ctx.expect_gt(f"{label}: file is not empty", float(size), 0.0)
    st = H.ffprobe_stream(path, "width,height,codec_name")
    w, h = int(st.get("width", 0)), int(st.get("height", 0))
    ctx.note(f"{label}: {path.name} {w}x{h} {st.get('codec_name')} {size} bytes")
    ctx.expect_ge(f"{label}: width", float(w), float(min_w))
    ctx.expect_ge(f"{label}: height", float(h), float(min_h))


def test_still(ctx):
    out = H.WORK / "cli_still.png"
    _cli(ctx, "still", ["still", SRC, "-p", "natural", "--time", str(H.TIME_A),
                        "--width", "240", "-o", str(out)],
         expect_stdout=("cli_still.png",))
    _check_image(ctx, "still", out, min_w=240)


def test_stats(ctx):
    """`stats` now prints the studio strip (grade/stats.py's frame_stats),
    the same numbers POST /api/stats returns, not ffmpeg signalstats. See
    the `stats` test group (cases_stats.py) for the numeric fixture; this
    test only pins that the CLI's own smoke output still looks like a real
    measurement of the clip it was pointed at."""
    r = _cli(ctx, "stats", ["stats", SRC, "-p", "natural", "--time", str(H.TIME_A),
                            "--json"],
             expect_stdout=("luma", "bands"))
    if r is None:
        return
    import json
    out = json.loads(r.stdout)
    ctx.expect_true("has key/size/stats", {"key", "size", "stats"} <= set(out),
                    str(set(out)))
    y = out["stats"]["luma"]["mean8"]
    ctx.note(f"YAVG-equivalent (luma mean8): {y}")
    ctx.expect_between("the mean is a plausible 8-bit code", y, 1.0, 254.0)
    ctx.expect_true("bands are part of the strip", "bands" in out["stats"],
                    str(set(out["stats"])))


def test_render(ctx):
    out = H.WORK / "cli_render.mov"
    r = _cli(ctx, "render", ["render", SRC, "-p", "natural", "-o", str(out),
                             "-t", "0.12", "--no-audio"],
             expect_stdout=("rendered (input", "rotation", "->"))
    # contract G9 friction 7: render now names the input transform it
    # resolved and the rotation mode it applied, on its one line of output.
    if r is not None:
        ctx.expect_true("render names the resolved input",
                        "input apple_log" in r.stdout, r.stdout)
    if not out.exists():
        ctx.check(False, "render: no file written")
        return
    st = H.ffprobe_stream(out, "width,height,codec_name,nb_frames,duration")
    ctx.note(f"render: {st}, {out.stat().st_size} bytes")
    ctx.expect_gt("render: file has content", float(out.stat().st_size), 0.0)
    ctx.expect_eq("render: codec", st.get("codec_name"), "prores")
    ctx.expect_ge("render: wrote at least one frame",
                  float(st.get("nb_frames") or 0), 1.0)


def test_compare(ctx):
    out = H.WORK / "cli_compare.png"
    _cli(ctx, "compare", ["compare", SRC, "--looks", "neutral,blockbuster",
                          "--time", str(H.TIME_A), "--width", "200",
                          "-o", str(out)],
         expect_stdout=("contact sheet", "left to right"))
    # Two panels side by side, so the sheet has to be about twice as wide.
    _check_image(ctx, "compare", out, min_w=390)


def test_scopes(ctx):
    out = H.WORK / "cli_scopes.png"
    _cli(ctx, "scopes", ["scopes", SRC, "-p", "natural", "--time", str(H.TIME_A),
                         "--width", "200", "-o", str(out)],
         expect_stdout=("scopes ->",))
    # The frame column plus a 200 wide scope column, two scopes tall.
    _check_image(ctx, "scopes", out, min_w=210, min_h=390)


def test_orient(ctx):
    """orient now builds a five-up: auto plus every fixed rotation.

    The sheet is taller than the panels because each one carries a drawn
    label strip, which is the whole point of it: a wrong display matrix is
    supposed to be readable off one image without counting panels.
    """
    out = H.WORK / "cli_orient.png"
    _cli(ctx, "orient", ["orient", SRC, "--time", str(H.TIME_A),
                         "--height", "200", "-o", str(out)],
         expect_stdout=("--rotate auto", "--rotate 90", "--rotate 270",
                        "left to right"))
    _check_image(ctx, "orient", out, min_h=200)


def test_unknown_preset_fails_cleanly(ctx):
    """A bad preset must be a message and a non-zero exit, not a traceback."""
    r = subprocess.run([PY, ENGINE, "still", SRC, "-p", "no_such_preset",
                        "-o", str(H.WORK / "never.png")],
                       capture_output=True, text=True)
    ctx.note(f"exit {r.returncode}, stderr: {r.stderr.strip()[:220]}")
    ctx.expect_true("unknown preset exits non-zero", r.returncode != 0,
                    f"exit {r.returncode}")
    # Gap 17 (checkpoints/FIX-GAPS.md) rewrote this message to name every
    # place it looked (a file path, the built-in catalog, and a studio's
    # saved presets when STUDIO_URL is set) and to list the catalog by name
    # rather than use the literal word "available"; this assertion follows
    # that documented change.
    ctx.expect_true("the error names the problem and lists what is available",
                    "Looked in" in r.stderr and "catalog:" in r.stderr,
                    r.stderr.strip()[:160])
    ctx.expect_true("no raw traceback reaches the user",
                    "Traceback" not in r.stderr, "clean")


def register(suite):
    g = "cli"
    suite.add(g, "still", test_still, doc="still writes a valid PNG at the asked width")
    suite.add(g, "stats", test_stats, doc="stats prints real signalstats numbers")
    suite.add(g, "render", test_render, doc="render writes a decodable ProRes file")
    suite.add(g, "compare", test_compare, doc="compare builds a multi-panel sheet")
    suite.add(g, "scopes", test_scopes, doc="scopes builds the waveform sheet")
    suite.add(g, "orient", test_orient,
              doc="orient builds the labelled five-up rotation sheet")
    suite.add(g, "bad_preset_fails_cleanly", test_unknown_preset_fails_cleanly,
              doc="a user error is a message and a non-zero exit")
