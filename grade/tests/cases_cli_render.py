"""Group: `cinegrade render` ergonomics (G6): --width/--scale, --codec, the
extension check, and the first-audio-stream-only map.

Six of six bakeoff lanes hit these gaps: a render with no way to shrink the
frame for a quick look, a codec override with no way to pick the matching
extension (so ffmpeg died with an opaque "Invalid argument" instead of a
clear refusal), and a real iPhone file whose second audio stream has no
decoder killing the whole render.
"""

from __future__ import annotations

import json
import subprocess

import harness as H

PY = str(H.CONTENT / ".venv" / "bin" / "python")
ENGINE = str(H.GRADE / "cinegrade.py")
SRC = str(H.CLIP_A)


def _cli(args):
    return subprocess.run([PY, ENGINE] + args, capture_output=True, text=True)


def _audio_streams(path) -> list[dict]:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index,codec_name", "-of", "json", str(path)],
        capture_output=True, text=True)
    if r.returncode != 0:
        return []
    return json.loads(r.stdout).get("streams") or []


def _two_audio_fixture(ctx) -> str:
    """A short synthetic clip with two audio streams, the second one
    carrying a codec tag no installed decoder understands.

    Built entirely with bounded ffmpeg calls (one video, two lavfi sine
    tones) and then one byte-level patch of the second stream's four
    character codec tag (the exact box `sowt`, patched to a code no decoder
    claims), which is the same "muxed fine, cannot be decoded" shape a real
    iPhone spatial-audio (`apac`) track has. No external footage needed, so
    the test does not depend on any file outside grade/tests/_work.
    """
    clean = H.WORK / "two_audio_clean.mov"
    fixture = H.WORK / "two_audio.mov"
    if fixture.exists():
        return str(fixture)
    r = subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", "color=c=blue:size=320x240:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100",
        "-map", "0:v", "-map", "1:a", "-map", "2:a",
        "-c:v", "libx264", "-c:a:0", "aac", "-c:a:1", "pcm_s16le",
        "-t", "1", str(clean),
    ], capture_output=True, text=True)
    ctx.expect_eq("fixture: clean two-audio file built", r.returncode, 0)
    data = clean.read_bytes()
    idx = data.rfind(b"sowt")
    ctx.expect_true("fixture: found the second stream's codec tag to patch",
                    idx >= 0, f"rfind result {idx}")
    patched = data[:idx] + b"zzzz" + data[idx + 4:]
    fixture.write_bytes(patched)
    return str(fixture)


def test_width_scale_mutually_exclusive(ctx):
    r = _cli(["render", SRC, "-o", str(H.WORK / "never_w.mp4"),
             "--width", "200", "--scale", "0.5"])
    ctx.expect_true("--width and --scale together exits non-zero",
                    r.returncode != 0, f"exit {r.returncode}")
    ctx.expect_true("argparse names both conflicting flags",
                    "--width" in r.stderr and "--scale" in r.stderr,
                    r.stderr.strip()[-200:])


def test_extension_mismatch_refused(ctx):
    out = H.WORK / "mismatch.mov"
    r = _cli(["render", SRC, "--codec", "libx264", "-o", str(out)])
    ctx.expect_true("mismatched extension exits non-zero",
                    r.returncode != 0, f"exit {r.returncode}")
    ctx.expect_true("message names the given extension",
                    ".mov" in r.stderr, r.stderr.strip())
    ctx.expect_true("message names the codec",
                    "libx264" in r.stderr, r.stderr.strip())
    ctx.expect_true("message names what the codec actually writes",
                    ".mp4" in r.stderr, r.stderr.strip())
    ctx.expect_true("no ffmpeg was even started (no raw ffmpeg failure text)",
                    "Error sending frames" not in r.stderr, r.stderr.strip())
    ctx.expect_true("not written", not out.exists(), "refused before writing")


def test_no_extension_refused(ctx):
    out = H.WORK / "no_extension"
    r = _cli(["render", SRC, "--codec", "libx264", "-o", str(out)])
    ctx.expect_true("no extension exits non-zero", r.returncode != 0,
                    f"exit {r.returncode}")
    ctx.expect_true("message says there is no extension",
                    "no extension" in r.stderr, r.stderr.strip())


def test_codec_extension_pick_succeeds(ctx):
    out = H.WORK / "codec_pick.mp4"
    r = _cli(["render", SRC, "-p", "natural", "--codec", "libx264",
             "-t", "0.12", "--no-audio", "-o", str(out)])
    ctx.expect_eq("libx264 to .mp4: exit code", r.returncode, 0)
    st = H.ffprobe_stream(out, "codec_name,width,height")
    ctx.note(f"codec_pick: {st}")
    ctx.expect_eq("codec actually written is libx264 (h264)",
                  st.get("codec_name"), "h264")

    out_mov = H.WORK / "codec_default.mov"
    r2 = _cli(["render", SRC, "-p", "natural", "-t", "0.12", "--no-audio",
              "-o", str(out_mov)])
    ctx.expect_eq("default codec (prores_ks) to .mov: exit code",
                  r2.returncode, 0)
    st2 = H.ffprobe_stream(out_mov, "codec_name")
    ctx.expect_eq("default codec written is prores", st2.get("codec_name"),
                  "prores")


def test_width_scales_the_render(ctx):
    out = H.WORK / "scaled_width.mp4"
    r = _cli(["render", SRC, "-p", "natural", "--codec", "libx264",
             "--width", "320", "-t", "0.12", "--no-audio", "-o", str(out)])
    ctx.expect_eq("render --width 320: exit code", r.returncode, 0)
    if r.returncode != 0:
        ctx.note(f"stderr: {r.stderr[-600:]}")
        return
    st = H.ffprobe_stream(out, "width,height")
    ctx.note(f"scaled render: {st}")
    ctx.expect_le("width is at most the asked width (even, rounded down)",
                  float(int(st.get("width", 0))), 320.0)
    ctx.expect_gt("width used most of the budget",
                  float(int(st.get("width", 0))), 300.0)


def test_scale_fraction_matches_width_math(ctx):
    """--scale 0.5 on a probed source should land on the same width the
    preview path (studio's scale_for_preview) would compute for that
    fraction, not a second, possibly different, formula.

    --rotate 0 pins the source's own stored width as the one the graph
    scales from, so the expected number does not also have to reproduce
    probe()'s auto-rotation swap.
    """
    probe = H.ffprobe_stream(SRC, "width,height")
    src_w = int(probe.get("width", 0))
    ctx.expect_gt("source has a real width", float(src_w), 0.0)
    if src_w <= 0:
        return
    expect_w = max(2, int(src_w * 0.5) // 2 * 2)

    out = H.WORK / "scaled_fraction.mp4"
    r = _cli(["render", SRC, "-p", "natural", "--codec", "libx264",
             "--rotate", "0", "--scale", "0.5", "-t", "0.12", "--no-audio",
             "-o", str(out)])
    ctx.expect_eq("render --scale 0.5: exit code", r.returncode, 0)
    if r.returncode != 0:
        ctx.note(f"stderr: {r.stderr[-600:]}")
        return
    st = H.ffprobe_stream(out, "width")
    ctx.expect_eq("--scale 0.5 width matches the preview path's own math",
                  int(st.get("width", 0)), expect_w)


def test_audio_maps_first_stream_only(ctx):
    """A second audio stream with no decoder must not kill the render, and
    --no-audio is not required to get a usable file."""
    src = _two_audio_fixture(ctx)
    out = H.WORK / "two_audio_render.mp4"
    r = _cli(["render", src, "-t", "1", "--codec", "libx264",
             "--rotate", "0", "-o", str(out)])
    ctx.note(f"exit {r.returncode}, stderr: {r.stderr.strip()[-400:]}")
    ctx.expect_eq("render succeeds without --no-audio", r.returncode, 0)
    if r.returncode != 0:
        return
    streams = _audio_streams(out)
    ctx.note(f"audio streams in output: {streams}")
    ctx.expect_eq("exactly one audio stream made it through (not zero, "
                  "not both)", len(streams), 1)
    if streams:
        ctx.expect_eq("the one mapped audio stream decoded to aac",
                      streams[0].get("codec_name"), "aac")


def register(suite):
    g = "cli_render"
    suite.add(g, "width_scale_mutually_exclusive",
              test_width_scale_mutually_exclusive,
              doc="--width and --scale together is a usage error")
    suite.add(g, "extension_mismatch_refused", test_extension_mismatch_refused,
              doc="a codec/extension mismatch is refused in one sentence "
                  "naming both, before ffmpeg ever runs")
    suite.add(g, "no_extension_refused", test_no_extension_refused,
              doc="an -o with no extension is refused the same way")
    suite.add(g, "codec_extension_pick_succeeds",
              test_codec_extension_pick_succeeds,
              doc="--codec picks the matching extension the way "
                  "start_render does (prores_ks -> .mov, else .mp4)")
    suite.add(g, "width_scales_the_render", test_width_scales_the_render,
              doc="--width actually renders at that width")
    suite.add(g, "scale_fraction_matches_width_math",
              test_scale_fraction_matches_width_math,
              doc="--scale reuses the preview path's own pixel math")
    suite.add(g, "audio_maps_first_stream_only", test_audio_maps_first_stream_only,
              doc="a second, undecodable audio stream no longer kills the "
                  "render; --no-audio is optional again")
