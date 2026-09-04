"""Group 5: what actually lands in the file.

Output tagging regressed silently once: frames came out labelled
color_primaries=bt2020 because the encoder inherits the source's frame
properties, and every colour-managed player then over-saturated the result. The
fix was a setparams retag inside the graph, since the -color_* output flags
alone do not stick. Both halves are asserted: the retag is present in the
graph, and the written file carries the tags.

This group renders real files through the shipped CLI rather than the still
path, because the encoder settings and the tagging only exist there.
"""

from __future__ import annotations

import json
import subprocess
import sys

import harness as H
from harness import cg

PY = str(H.CONTENT / ".venv" / "bin" / "python")
ENGINE = str(H.GRADE / "cinegrade.py")

# Short enough that a 4K ProRes render is a couple of seconds, long enough that
# the encoder writes a real stream rather than a single-frame edge case.
DURATION = "0.12"


def _render(ctx, out_name, preset=None, extra=()):
    out = H.WORK / out_name
    args = [PY, ENGINE, "render", str(H.CLIP_A), "-o", str(out),
            "-t", DURATION, "--no-audio"]
    if preset:
        args += ["-p", str(preset)]
    args += list(extra)
    r = subprocess.run(args, capture_output=True, text=True)
    ctx.expect_eq(f"{out_name}: cinegrade render exits 0", r.returncode, 0)
    if r.returncode != 0:
        ctx.note(r.stderr[-800:] or r.stdout[-800:])
        return None
    ctx.expect_gt(f"{out_name}: the file has content",
                  float(out.stat().st_size if out.exists() else 0), 0.0)
    return out


def _preset_file(name, body):
    path = H.WORK / f"{name}.json"
    path.write_text(json.dumps(body))
    return path


def test_output_is_tagged_bt709(ctx):
    """primaries, transfer and matrix all bt709 in the written file."""
    out = _render(ctx, "tagged.mov")
    if out is None:
        return
    st = H.ffprobe_stream(
        out, "codec_name,color_primaries,color_transfer,color_space,color_range")
    ctx.note(f"ffprobe: {st}")
    for key in ("color_primaries", "color_transfer", "color_space"):
        ctx.expect_eq(f"{key} on the rendered file", st.get(key), "bt709")


def test_h264_output_is_also_tagged_bt709(ctx):
    """The other encoder branch takes a different argument path."""
    pf = _preset_file("h264", {"output": {"codec": "libx264", "crf": 24,
                                          "preset": "ultrafast"}})
    out = _render(ctx, "tagged.mp4", preset=pf)
    if out is None:
        return
    st = H.ffprobe_stream(
        out, "codec_name,color_primaries,color_transfer,color_space")
    ctx.note(f"ffprobe: {st}")
    ctx.expect_eq("codec_name", st.get("codec_name"), "h264")
    for key in ("color_primaries", "color_transfer", "color_space"):
        ctx.expect_eq(f"{key} on the rendered file", st.get(key), "bt709")


def test_graph_retags_inside_the_filter_chain(ctx):
    """The -color_* flags alone do not stick, so the graph must retag too.

    Asserted on the graph string rather than only on the file because the file
    can look right for the wrong reason: if the source were already bt709 the
    inherited tags would pass while the retag was missing, and the next
    bt2020 source would silently regress.
    """
    info = H.info_for(H.CLIP_A)
    graph = cg.graph_with_mask(H.defaults(), info, encode_out=True)
    ctx.expect_true("the encode tail carries a setparams retag",
                    "setparams=" in graph,
                    "setparams present" if "setparams=" in graph else "MISSING")
    for token in ("color_primaries=bt709", "color_trc=bt709", "colorspace=bt709"):
        ctx.expect_true(f"graph sets {token}", token in graph,
                        "present" if token in graph else "MISSING")
    still = cg.graph_with_mask(H.defaults(), info, encode_out=False)
    ctx.expect_true("the still path does not force the encode tail",
                    "setparams=" not in still,
                    "absent as expected" if "setparams=" not in still else "present")


def test_output_codec_and_profile(ctx):
    """output.codec and output.profile must reach the encoder."""
    hq = _render(ctx, "prores_hq.mov",
                 preset=_preset_file("hq", {"output": {"codec": "prores_ks",
                                                       "profile": 3}}))
    proxy = _render(ctx, "prores_proxy.mov",
                    preset=_preset_file("px", {"output": {"codec": "prores_ks",
                                                          "profile": 0}}))
    x264 = _render(ctx, "x264.mp4",
                   preset=_preset_file("x264", {"output": {"codec": "libx264",
                                                           "crf": 24,
                                                           "preset": "ultrafast"}}))
    if not all((hq, proxy, x264)):
        return
    a = H.ffprobe_stream(hq, "codec_name,profile")
    b = H.ffprobe_stream(proxy, "codec_name,profile")
    c = H.ffprobe_stream(x264, "codec_name,profile")
    ctx.note(f"profile 3 -> {a}, profile 0 -> {b}, libx264 -> {c}")
    ctx.expect_eq("prores profile 3 is HQ", a.get("profile"), "HQ")
    ctx.expect_eq("prores profile 0 is Proxy", b.get("profile"), "Proxy")
    ctx.expect_true("output.profile changes the written profile",
                    a.get("profile") != b.get("profile"),
                    f"{a.get('profile')} vs {b.get('profile')}")
    ctx.expect_eq("output.codec selects h264", c.get("codec_name"), "h264")
    ctx.expect_gt("the HQ profile writes a bigger file than Proxy",
                  float(hq.stat().st_size), float(proxy.stat().st_size))


def test_output_crf_and_preset(ctx):
    """output.crf and output.preset only apply on the non-prores branch."""
    fine = _render(ctx, "crf16.mp4",
                   preset=_preset_file("crf16", {"output": {"codec": "libx264",
                                                            "crf": 16,
                                                            "preset": "veryfast"}}))
    coarse = _render(ctx, "crf40.mp4",
                     preset=_preset_file("crf40", {"output": {"codec": "libx264",
                                                              "crf": 40,
                                                              "preset": "veryfast"}}))
    slow = _render(ctx, "slow.mp4",
                   preset=_preset_file("slow", {"output": {"codec": "libx264",
                                                           "crf": 24,
                                                           "preset": "slow"}}))
    fast = _render(ctx, "fast.mp4",
                   preset=_preset_file("fast", {"output": {"codec": "libx264",
                                                           "crf": 24,
                                                           "preset": "ultrafast"}}))
    if not all((fine, coarse, slow, fast)):
        return
    sizes = {p.name: p.stat().st_size for p in (fine, coarse, slow, fast)}
    ctx.note(f"file sizes: {sizes}")
    ctx.expect_gt("a lower crf writes a bigger file",
                  float(sizes["crf16.mp4"]), float(sizes["crf40.mp4"]))
    ctx.expect_true("output.preset changes the encode",
                    sizes["slow.mp4"] != sizes["fast.mp4"],
                    f"slow {sizes['slow.mp4']} vs ultrafast {sizes['fast.mp4']}")


def register(suite):
    g = "output"
    suite.add(g, "tagged_bt709", test_output_is_tagged_bt709,
              doc="rendered ProRes carries bt709 primaries, transfer and matrix")
    suite.add(g, "tagged_bt709_h264", test_h264_output_is_also_tagged_bt709,
              doc="the h264 branch is tagged bt709 too")
    suite.add(g, "graph_retags", test_graph_retags_inside_the_filter_chain,
              doc="the retag is inside the filter graph, not only on the flags")
    suite.add(g, "codec_and_profile", test_output_codec_and_profile,
              doc="output.codec and output.profile reach the encoder")
    suite.add(g, "crf_and_preset", test_output_crf_and_preset,
              doc="output.crf and output.preset reach the encoder")
