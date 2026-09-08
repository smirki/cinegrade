#!/usr/bin/env python3
"""Lane M4's own tests for the `mask` CLI verbs, studio/tools/grade_client.py's
mask methods, `stats --matte`, and `stats.frame_stats(weight=...)`.

Not part of run_tests.py's golden-frame suite (that harness is tied to real
ffmpeg renders against footage/goldens, and lives in M2's part of this
directory): this is a standalone script, run directly.

    content/.venv/bin/python grade/tests/test_mask_tools.py

Contract C4 (the studio routes `mask`/`grade_client` call) is not built yet
on this branch as lane M5's own work; `fake_studio_server.py` beside this
file is a small threading http.server answering the same C4 shapes, written
by this lane so the CLI and the client can be tested against something,
documented in the M4 checkpoint. Once M5's real routes land, re-running this
file still passes (the CLI and the client only assume the wire shapes C4
fixes, not which process answers them), and `bakeoff/` or `studio/tests/py`
integration tests can additionally point at the real server.

`stats --matte` needs `grade/mattes.py` (contract C6, owned by lane M2): if
it is missing this file's matte tests are skipped with a note, not failed,
since that module landing is outside this lane's own delivery.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

TESTS = Path(__file__).resolve().parent
GRADE = TESTS.parent
CONTENT = GRADE.parent
STUDIO_TOOLS = CONTENT / "studio" / "tools"

sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(GRADE))
sys.path.insert(0, str(STUDIO_TOOLS))

import fake_studio_server as FAKE                            # noqa: E402

PY = sys.executable
ENGINE = str(GRADE / "cinegrade.py")

PASS = 0
FAIL = 0
NOTES: list[str] = []


def ok(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))


def note(text: str) -> None:
    NOTES.append(text)
    print(f"  note  {text}")


def run_cli(args: list, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run([PY, ENGINE] + args, capture_output=True,
                          text=True, env=full_env, timeout=60)


# --------------------------------------------------------------------------
# 1. mask CLI verbs against the fake server
# --------------------------------------------------------------------------

def test_mask_cli(base: str) -> None:
    print("\n== mask CLI against the fake studio server ==")
    env = {"STUDIO_URL": base}

    r = run_cli(["mask", "segment", "C015.mov", "--time", "1.0",
                "--text", "person", "--json"], env=env)
    ok("segment: exit 0", r.returncode == 0, r.stderr[-300:])
    try:
        seg = json.loads(r.stdout)
    except json.JSONDecodeError:
        seg = {}
    ok("segment: pick_id present", bool(seg.get("pick_id")), r.stdout[:200])
    ok("segment: two instances", len(seg.get("instances") or []) == 2,
       json.dumps(seg)[:200])

    with tempfile.TemporaryDirectory(prefix="mask_segment_dl_") as d:
        r2 = run_cli(["mask", "segment", "C015.mov", "--time", "1.0",
                     "--text", "person", "--json", "-o", d], env=env)
        ok("segment -o DIR: exit 0", r2.returncode == 0, r2.stderr[-300:])
        files = list(Path(d).glob("*.png"))
        ok("segment -o DIR: downloaded overlay+mask files", len(files) == 4,
           f"found {len(files)}: {[f.name for f in files]}")

    r3 = run_cli(["mask", "track", "C015.mov", "--text", "person",
                 "--wait", "--json"], env=env)
    ok("track --wait: exit 0", r3.returncode == 0, r3.stderr[-500:])
    ok("track --wait: progress on stderr", "frames" in r3.stderr,
       r3.stderr[:200])
    try:
        trk = json.loads(r3.stdout)
    except json.JSONDecodeError:
        trk = {}
    ok("track --wait: state done", trk.get("state") == "done", trk)
    mattes = trk.get("matte_ids") or [m.get("matte_id") for m in
                                      (trk.get("mattes") or [])]
    matte_id = (mattes or [None])[0]
    ok("track --wait: has a matte id", bool(matte_id), trk)

    r4 = run_cli(["mask", "track", "FAIL_CLIP", "--text", "x", "--wait"],
                env=env)
    ok("track --wait on a failing job: exits non zero", r4.returncode != 0)
    ok("track --wait on a failing job: message says failed",
       "failed" in r4.stderr.lower(), r4.stderr[:200])

    r5 = run_cli(["mask", "jobs", "--json"], env=env)
    ok("jobs: exit 0", r5.returncode == 0, r5.stderr[-300:])
    jobs = json.loads(r5.stdout).get("jobs") or []
    ok("jobs: at least the two jobs queued above", len(jobs) >= 2, jobs)

    r6 = run_cli(["mask", "list", "C015.mov", "--json"], env=env)
    ok("list: exit 0", r6.returncode == 0, r6.stderr[-300:])
    mattes6 = json.loads(r6.stdout).get("mattes") or []
    ok("list: the clip's matte shows up", any(
        m.get("matte_id") == matte_id for m in mattes6), mattes6)

    if matte_id:
        r7 = run_cli(["mask", "show", matte_id, "--json"], env=env)
        ok("show: exit 0", r7.returncode == 0, r7.stderr[-300:])
        idx = json.loads(r7.stdout)
        ok("show: state done", idx.get("state") == "done", idx)
        ok("show: frames written", idx.get("done_frames") == idx.get("frames"),
           idx)

        with tempfile.TemporaryDirectory(prefix="mask_show_strip_") as d:
            out = str(Path(d) / "strip.jpg")
            r8 = run_cli(["mask", "show", matte_id, "--strip", "-o", out,
                         "--width", "80"], env=env)
            ok("show --strip: exit 0", r8.returncode == 0, r8.stderr[-500:])
            ok("show --strip: wrote a file", Path(out).is_file())
            if Path(out).is_file():
                from PIL import Image
                im = Image.open(out)
                ok("show --strip: image opens and has real size",
                  im.width > 80 and im.height > 20, im.size)

    r9 = run_cli(["mask", "segment", "C015.mov"],
                env={"STUDIO_AGENT": "test-agent"})
    ok("agent with no server named: refused, exit non zero", r9.returncode != 0)
    ok("agent with no server named: refusal message",
       "must name its server" in r9.stderr, r9.stderr[:200])


# --------------------------------------------------------------------------
# 2. grade_client.py's mask methods, called directly (not through the CLI)
# --------------------------------------------------------------------------

def test_grade_client(base: str) -> None:
    print("\n== grade_client.Studio mask methods ==")
    import grade_client as GC

    studio = GC.Studio(base=base, agent="m4-test")
    seg = studio.segment(clip="C015.mov", time=1.0, text=["person"])
    ok("Studio.segment: pick_id present", bool(seg.get("pick_id")), seg)
    ok("Studio.segment: instances present", len(seg.get("instances") or []) == 2)

    trk = studio.track(clip="C015.mov", text=["person"])
    job_id = trk.get("job_id")
    ok("Studio.track: job_id present", bool(job_id), trk)

    progress = []
    final = studio.wait(job_id, poll=0.2, on_progress=lambda j: progress.append(j["state"]))
    ok("Studio.wait: reaches done", final.get("state") == "done", final)
    ok("Studio.wait: called on_progress at least once", len(progress) >= 1)

    matte_id = (trk.get("mattes") or [{}])[0].get("matte_id")
    if matte_id:
        mf = studio.matte_frame(matte_id, time=1.0, width=64)
        ok("Studio.matte_frame: returns bytes + state",
          bool(mf.get("data")) and mf.get("state") in ("running", "done"), mf.get("state"))

    trk_fail = studio.track(clip="FAIL_CLIP", text=["x"])
    fail_job = trk_fail.get("job_id")
    raised = False
    try:
        studio.wait(fail_job, poll=0.1)
    except GC.StudioError as exc:
        raised = True
        ok("Studio.wait: raises StudioError on a failed job",
          "failed" in str(exc).lower(), str(exc))
    ok("Studio.wait: did raise", raised)


# --------------------------------------------------------------------------
# 3. frame_stats(weight=...) - pure numpy, no server needed
# --------------------------------------------------------------------------

def test_frame_stats_weight() -> None:
    print("\n== stats.frame_stats(weight=...) ==")
    import numpy as np
    import stats as ST

    rgb = np.zeros((8, 8, 3), dtype=np.uint8)
    rgb[:, :4] = (220, 40, 40)     # left half: warm, bright
    rgb[:, 4:] = (20, 20, 60)      # right half: cool, dark

    plain = ST.frame_stats(rgb)
    ones = ST.frame_stats(rgb, weight=np.ones((8, 8)))
    ok("weight=ones matches weight=None on families",
       plain["families"] == ones["families"], (plain["families"], ones["families"]))
    ok("weight=ones matches weight=None on clipped",
       plain["clipped"] == ones["clipped"])

    left = np.zeros((8, 8))
    left[:, :4] = 1.0
    only_left = ST.frame_stats(rgb, weight=left)
    ok("weight on the left half reads as 100% warm",
       only_left["families"]["warm"] == 100.0, only_left["families"])
    r, g, b = (rgb[:, :4, 0].astype(float) / 255.0,
              rgb[:, :4, 1].astype(float) / 255.0,
              rgb[:, :4, 2].astype(float) / 255.0)
    expect_luma = float((0.2126 * r + 0.7152 * g + 0.0722 * b).mean())
    ok("weight on the left half's mean luma is the left patch's own luma",
       abs(only_left["luma"]["mean"] - expect_luma) < 1e-3,
       (only_left["luma"]["mean"], expect_luma))

    try:
        ST.frame_stats(rgb, weight=np.zeros((8, 8)))
        ok("all-zero weight raises StatsError", False)
    except ST.StatsError:
        ok("all-zero weight raises StatsError", True)

    try:
        ST.frame_stats(rgb, weight=np.ones((3, 3)))
        ok("mismatched weight shape raises StatsError", False)
    except ST.StatsError:
        ok("mismatched weight shape raises StatsError", True)

    b = ST.bands(rgb, weight=left)
    ok("bands(weight=...) matches frame_stats(weight=...)'s own bands block",
       b == only_left["bands"], (b, only_left["bands"]))


# --------------------------------------------------------------------------
# 4. stats --matte, end to end against a hand built matte fixture
# --------------------------------------------------------------------------

def _make_synthetic_clip(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc=size=64x48:rate=10:duration=3",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, timeout=30)


def test_stats_matte() -> None:
    print("\n== stats --matte (needs grade/mattes.py, contract C6) ==")
    try:
        import mattes as MT
    except ImportError:
        note("grade/mattes.py is not on this branch yet (owned by lane M2, "
            "contract C6); stats --matte was tested for its CLI wiring "
            "and refusal paths only, see checkpoint")
        return

    with tempfile.TemporaryDirectory(prefix="mask_matte_root_") as root_s:
        root = Path(root_s)
        clip_dir = root
        clip_path = clip_dir / "synth.mp4"
        _make_synthetic_clip(clip_path)

        matte_dir = root / "m_partial"
        matte_dir.mkdir(parents=True)
        mw, mh, fps, frames = 32, 18, 10.0, 30
        left = None
        import numpy as np
        for i in range(15):                    # half written: a partial track
            arr = np.zeros((mh, mw), dtype=np.uint8)
            arr[:, :mw // 2] = 255               # left half only
            MT.write_gray_png(matte_dir / MT.frame_name(i), arr)
        (matte_dir / "index.json").write_text(json.dumps({
            "matte_id": "m_partial", "clip": "synth.mp4", "clip_key": "synth",
            "rotation": "auto", "fps": fps, "frames": frames,
            "width": mw, "height": mh, "recipe": {"prompts": {"text": ["x"]}},
            "state": "partial", "done_frames": 15,
            "areas": [0.5] * 15, "scores": [], "created": time.time(),
            "model": "stub", "backend": "stub",
        }))

        zero_dir = root / "m_zero"
        zero_dir.mkdir(parents=True)
        MT.write_gray_png(zero_dir / MT.frame_name(0),
                          np.zeros((mh, mw), dtype=np.uint8))
        (zero_dir / "index.json").write_text(json.dumps({
            "matte_id": "m_zero", "clip": "synth.mp4", "clip_key": "synth",
            "rotation": "auto", "fps": fps, "frames": 1, "width": mw,
            "height": mh, "recipe": {}, "state": "done", "done_frames": 1,
            "areas": [0.0], "scores": [], "created": time.time(),
            "model": "stub", "backend": "stub",
        }))

        env = {"CINEGRADE_MATTE_ROOT": str(root)}

        r_plain = run_cli(["stats", str(clip_path), "--time", "1.0", "--json"],
                          env=env)
        ok("stats plain: exit 0", r_plain.returncode == 0, r_plain.stderr[-300:])

        r_matte = run_cli(["stats", str(clip_path), "--time", "0.5",
                          "--matte", "m_partial", "--json"], env=env)
        ok("stats --matte (written frame): exit 0", r_matte.returncode == 0,
           r_matte.stderr[-500:])
        try:
            row = json.loads(r_matte.stdout)
        except json.JSONDecodeError:
            row = {}
        ok("stats --matte: differs from the plain measurement",
           row.get("stats") != json.loads(r_plain.stdout).get("stats"))

        r_fallback = run_cli(["stats", str(clip_path), "--time", "2.0",
                             "--matte", "m_partial", "--json"], env=env)
        ok("stats --matte (frame not written yet): exit 0",
           r_fallback.returncode == 0, r_fallback.stderr[-500:])
        row_fb = json.loads(r_fallback.stdout) if r_fallback.returncode == 0 else {}
        ok("stats --matte: warns about the nearest-written fallback",
           bool(row_fb.get("warnings")), row_fb)

        r_region = run_cli(["stats", str(clip_path), "--time", "0.5",
                           "--matte", "m_partial", "--region", "0", "0", "1", "1",
                           "--json"], env=env)
        ok("stats --matte + --region together: exit 0",
           r_region.returncode == 0, r_region.stderr[-500:])

        r_zero = run_cli(["stats", str(clip_path), "--time", "0.0",
                         "--matte", "m_zero"], env=env)
        ok("stats --matte with a matte that covers nothing: exits non zero",
           r_zero.returncode != 0)
        ok("stats --matte with nothing covered: message says so",
           "nothing" in r_zero.stderr.lower(), r_zero.stderr[:300])

        r_missing = run_cli(["stats", str(clip_path), "--time", "0.0",
                            "--matte", "does-not-exist"], env=env)
        ok("stats --matte with an unknown id: exits non zero",
           r_missing.returncode != 0)

        r_image = run_cli(["stats", "--image", str(clip_path), "--matte", "m_partial"],
                          env=env)
        ok("stats --image with --matte: refused",
           r_image.returncode != 0 and "still" in r_image.stderr.lower(),
           r_image.stderr[:200])


# --------------------------------------------------------------------------
# 5. render --allow-partial, end to end through the CLI (integration lane
#    item 2: the render verb used to build its ffmpeg inputs with
#    strict_mattes always False, so a partial matte rendered silently and
#    --allow-partial did not exist as a flag at all)
# --------------------------------------------------------------------------

def test_render_allow_partial() -> None:
    print("\n== render --allow-partial ==")
    try:
        import mattes as MT
        import cinegrade as cg
    except ImportError as exc:
        note(f"grade/mattes.py or grade/cinegrade.py not importable: {exc}; "
            "render --allow-partial was not exercised")
        return
    import numpy as np
    from copy import deepcopy

    with tempfile.TemporaryDirectory(prefix="mask_render_partial_") as root_s:
        root = Path(root_s)
        clip_path = root / "synth.mp4"
        _make_synthetic_clip(clip_path)

        mw, mh, fps = 64, 48, 10.0
        matte_dir = root / "m_short"
        matte_dir.mkdir(parents=True)
        for i in range(5):                      # a track that stopped early
            arr = np.zeros((mh, mw), dtype=np.uint8)
            arr[:, :mw // 2] = 255
            MT.write_gray_png(matte_dir / MT.frame_name(i), arr)
        (matte_dir / "index.json").write_text(json.dumps({
            "matte_id": "m_short", "clip": "synth.mp4", "clip_key": "synth",
            "rotation": "auto", "fps": fps, "frames": 30,
            "width": mw, "height": mh, "recipe": {"prompts": {"text": ["x"]}},
            "state": "running", "done_frames": 5,
            "areas": [0.5] * 5, "scores": [], "created": time.time(),
            "model": "stub", "backend": "stub",
        }))

        layer = deepcopy(cg.LAYER_DEFAULTS)
        layer["mask"] = cg.deep_merge(layer["mask"], {
            "components": [{"id": "c1", "type": "matte", "op": "add",
                            "enabled": True, "invert": False, "feather": 0.0,
                            "matte": {"id": "m_short"}}]})
        layer["correct"] = cg.deep_merge(layer["correct"],
                                         {"exposure": 0.5, "lum_gain": 0.4})
        preset = cg.deep_merge(cg.DEFAULTS, {"layers": [layer]})
        preset_path = root / "preset.json"
        preset_path.write_text(json.dumps(preset))

        env = {"CINEGRADE_MATTE_ROOT": str(root)}
        out_refused = root / "refused.mov"
        r_refused = run_cli(["render", str(clip_path), "--preset", str(preset_path),
                            "-o", str(out_refused), "-t", "0.5"], env=env)
        ok("render without --allow-partial refuses a partial matte",
           r_refused.returncode != 0, r_refused.stderr[-400:])
        ok("the refusal names allow_partial, the way it was told to",
           "allow_partial" in r_refused.stderr, r_refused.stderr[-300:])
        ok("and nothing was written", not out_refused.exists())

        out_allowed = root / "allowed.mov"
        r_allowed = run_cli(["render", str(clip_path), "--preset", str(preset_path),
                            "-o", str(out_allowed), "-t", "0.5", "--allow-partial"],
                            env=env)
        ok("render with --allow-partial succeeds",
           r_allowed.returncode == 0, r_allowed.stderr[-400:])
        ok("and writes an output file",
           out_allowed.is_file() and out_allowed.stat().st_size > 0)


def main() -> int:
    print("starting the fake studio server...")
    srv, th, port = FAKE.start(0)
    base = f"http://127.0.0.1:{port}"
    print(f"fake studio server up at {base}")
    try:
        test_mask_cli(base)
        test_grade_client(base)
        test_frame_stats_weight()
        test_stats_matte()
        test_render_allow_partial()
    finally:
        srv.shutdown()

    print(f"\n{PASS} passed, {FAIL} failed" + (f", {len(NOTES)} note(s)" if NOTES else ""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
