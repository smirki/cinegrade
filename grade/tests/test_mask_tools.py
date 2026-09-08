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
                # One panel per second across the frames the matte really
                # wrote (checkpoint gap 13). This fake matte is 9 frames at
                # 24 fps, so its whole span is inside the first second: one
                # panel wide, and taller than the panel because the area
                # curve and the span note sit under it.
                ok("show --strip: image opens and has real size",
                  im.width >= 80 and im.height > 100, im.size)

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

    # A prompt of its own, not test_mask_cli's: a repeat of the same clip and
    # the same words is a CACHE HIT now (checkpoint gap 12), which is the
    # right answer but not what this block is measuring.
    trk = studio.track(clip="C015.mov", text=["client-person"])
    job_id = trk.get("job_id")
    ok("Studio.track: job_id present", bool(job_id), trk)
    ok("Studio.track: a first track is not cached",
       trk.get("cached") is False, trk)

    progress = []
    final = studio.wait(job_id, poll=0.2, on_progress=lambda j: progress.append(j["state"]))
    ok("Studio.wait: reaches done", final.get("state") == "done", final)
    ok("Studio.wait: called on_progress at least once", len(progress) >= 1)

    matte_id = (trk.get("mattes") or [{}])[0].get("matte_id")
    if matte_id:
        mf = studio.matte_frame(matte_id, time=1.0, width=64)
        ok("Studio.matte_frame: returns bytes + state",
          bool(mf.get("data")) and mf.get("state") in ("running", "done"), mf.get("state"))

    # Gap 12 through the client: the same request again is a cache hit, and
    # `force=True` asks for the whole thing over.
    again = studio.track(clip="C015.mov", text=["client-person"])
    ok("Studio.track: the same request again is cached",
       again.get("cached") is True, again)
    forced = studio.track(clip="C015.mov", text=["client-person"], force=True)
    ok("Studio.track(force=True): a real job, not the cache",
       forced.get("cached") is False and bool(forced.get("job_id")), forced)

    # Gap 6 through the client: the list is a summary, `full=True` is the
    # per frame arrays, and one matte by id always carries them.
    lst = studio.mattes(clip="C015.mov")
    ok("Studio.mattes: returns the clip's mattes", bool(lst.get("mattes")), lst)
    first = (lst.get("mattes") or [{}])[0]
    ok("Studio.mattes: summary by default, no per frame arrays",
       "areas" not in first and "span" in first, sorted(first))
    ok("Studio.mattes: the summary counts the span",
       "coverage" in first and "quality" in first, sorted(first))
    lst_full = studio.mattes(clip="C015.mov", full=True)
    ok("Studio.mattes(full=True): per frame arrays are back",
       "areas" in (lst_full.get("mattes") or [{}])[0],
       sorted((lst_full.get("mattes") or [{}])[0]))
    if matte_id:
        one = studio.matte(matte_id)
        ok("Studio.matte: one matte by id keeps its arrays",
          "areas" in one and one.get("matte_id") == matte_id, sorted(one))

    trk_fail = studio.track(clip="FAIL_CLIP", text=["fail-x"])
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
        # Checkpoint gap 11: every measurement now says what width it was
        # measured at, so two numbers taken at different sizes cannot be
        # compared by accident.
        plain = json.loads(r_plain.stdout) if r_plain.returncode == 0 else {}
        ok("stats: the JSON says the width it measured at",
           plain.get("measured_width") == (plain.get("size") or [0])[0],
           [plain.get("measured_width"), plain.get("size")])
        r_text = run_cli(["stats", str(clip_path), "--time", "1.0"], env=env)
        ok("stats: the printed block says the width it measured at",
           "measured at" in r_text.stdout, r_text.stdout[:300])

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

        # The refusal is keyed on COVERAGE OF THE WINDOW ASKED FOR, not on the
        # matte's declared state (the successor lane's --allow-partial
        # finding). 5 frames are written at fps 10, so 2 seconds wants 20 and
        # is genuinely short: that is the case that still refuses.
        out_refused = root / "refused.mov"
        r_refused = run_cli(["render", str(clip_path), "--preset", str(preset_path),
                            "-o", str(out_refused), "-t", "2.0"], env=env)
        ok("render without --allow-partial refuses a window the matte is short of",
           r_refused.returncode != 0, r_refused.stderr[-400:])
        ok("the refusal names allow_partial, the way it was told to",
           "allow_partial" in r_refused.stderr, r_refused.stderr[-300:])
        ok("the refusal counts the frames it has against the frames it wants",
           "covers 5 of 20 frames" in r_refused.stderr, r_refused.stderr[-400:])
        ok("and nothing was written", not out_refused.exists())

        out_allowed = root / "allowed.mov"
        r_allowed = run_cli(["render", str(clip_path), "--preset", str(preset_path),
                            "-o", str(out_allowed), "-t", "2.0", "--allow-partial"],
                            env=env)
        ok("render with --allow-partial succeeds on the short window",
           r_allowed.returncode == 0, r_allowed.stderr[-400:])
        ok("and writes an output file",
           out_allowed.is_file() and out_allowed.stat().st_size > 0)

        # And the case the old rule got wrong: half a second wants 5 frames and
        # all 5 are written, so a matte whose own state is still "running"
        # renders with no flag at all, and says so on stderr.
        out_covered = root / "covered.mov"
        r_covered = run_cli(["render", str(clip_path), "--preset", str(preset_path),
                            "-o", str(out_covered), "-t", "0.5"], env=env)
        ok("a partial matte that covers the whole window renders with no flag",
           r_covered.returncode == 0, r_covered.stderr[-400:])
        ok("and writes an output file",
           out_covered.is_file() and out_covered.stat().st_size > 0)
        ok("and the unfinished track is still reported on stderr",
           "holds its last written frame" in r_covered.stderr,
           r_covered.stderr[-400:])
        ok("the notice is a note, not a refusal", "note:" in r_covered.stderr,
           r_covered.stderr[-300:])


# --------------------------------------------------------------------------
# 6. the checkpoint tooling gaps M8 logged: a zero match segment (3), the
#    summary list (6), the span being said out loud (8 and 10), resume /
#    restart / force on a repeated track (12), a strip of a matte that is
#    still running (13), and the per frame suspect flags (18)
# --------------------------------------------------------------------------

def _advance(base: str, job_id: str, times: int = 1) -> dict:
    """Step the fake server's job forward. It advances one step per
    `GET /api/mask/jobs/<id>`, the same poll `mask track --wait` makes, so a
    test that wants a HALF written matte polls it a fixed number of times
    instead of racing a timer."""
    out = {}
    for _ in range(times):
        with urllib.request.urlopen(f"{base}/api/mask/jobs/{job_id}",
                                    timeout=10) as r:
            out = json.loads(r.read())
    return out


def test_mask_gaps(base: str) -> None:
    print("\n== mask CLI: the tooling gaps (3, 6, 8/10, 12, 13, 18) ==")
    env = {"STUDIO_URL": base}

    # -- gap 3: a prompt that matches nothing ------------------------------
    r = run_cli(["mask", "segment", "C015.mov", "--time", "1.0",
                "--text", "nothing at all"], env=env)
    ok("segment with no matches: exits non zero", r.returncode != 0,
       r.stdout[:200])
    ok("segment with no matches: says no match for the words asked",
       'no match for "nothing at all"' in r.stderr, r.stderr[:300])
    ok("segment with no matches: names the candidate count",
       "0 candidates" in r.stderr, r.stderr[:300])
    r_j = run_cli(["mask", "segment", "C015.mov", "--time", "1.0",
                  "--text", "nothing at all", "--json"], env=env)
    ok("segment with no matches: --json still prints the payload",
       '"candidates": 0' in r_j.stdout, r_j.stdout[:200])
    ok("segment with no matches: --json still exits non zero",
       r_j.returncode != 0)

    # -- gap 12: the same request again ------------------------------------
    r1 = run_cli(["mask", "track", "C015.mov", "--text", "cache-probe",
                 "--wait", "--json"], env=env)
    ok("track (first time): exit 0", r1.returncode == 0, r1.stderr[-300:])
    first = json.loads(r1.stdout) if r1.returncode == 0 else {}
    cached_matte = (first.get("matte_ids") or [None])[0]

    r2 = run_cli(["mask", "track", "C015.mov", "--text", "cache-probe",
                 "--json"], env=env)
    ok("track (same request): exit 0", r2.returncode == 0, r2.stderr[-300:])
    again = json.loads(r2.stdout) if r2.returncode == 0 else {}
    ok("track (same request): answered from the cache",
       again.get("cached") is True, again)
    ok("track (same request): the same matte, not a new one",
       (again.get("mattes") or [{}])[0].get("matte_id") == cached_matte,
       [again.get("mattes"), cached_matte])

    r3 = run_cli(["mask", "track", "C015.mov", "--text", "cache-probe",
                 "--wait"], env=env)
    ok("track --wait on a cache hit: exit 0, no job to wait on",
       r3.returncode == 0, r3.stderr[-300:])
    ok("track --wait on a cache hit: explains itself instead of dying",
       "cached:" in r3.stderr, r3.stderr[:300])

    r4 = run_cli(["mask", "track", "C015.mov", "--text", "cache-probe",
                 "--force", "--json"], env=env)
    ok("track --force: a real job, not the cache", r4.returncode == 0
       and bool(json.loads(r4.stdout or "{}").get("job_id")), r4.stderr[-300:])
    forced = json.loads(r4.stdout) if r4.returncode == 0 else {}
    ok("track --force: says it restarted", forced.get("restarted") is True,
       forced)
    ok("track --force: keeps the same matte id (same recipe, redone)",
       (forced.get("mattes") or [{}])[0].get("matte_id") == cached_matte,
       forced)

    # -- gaps 13 and 18: a matte that is still running, and a bad track ----
    r5 = run_cli(["mask", "track", "SUSPECT_CLIP", "--text", "face",
                 "--json"], env=env)
    ok("track without --wait: exit 0", r5.returncode == 0, r5.stderr[-300:])
    queued = json.loads(r5.stdout) if r5.returncode == 0 else {}
    job_id = queued.get("job_id")
    bad_matte = (queued.get("mattes") or [{}])[0].get("matte_id")
    ok("track without --wait: returns the matte id straight away",
       bool(bad_matte), queued)

    with tempfile.TemporaryDirectory(prefix="mask_strip_partial_") as d:
        out0 = str(Path(d) / "nothing.jpg")
        r6 = run_cli(["mask", "show", bad_matte, "--strip", "-o", out0,
                     "--width", "80"], env=env)
        ok("strip of a matte with nothing written: refused, not crashed",
           r6.returncode != 0 and "TypeError" not in r6.stderr,
           r6.stderr[-300:])
        ok("strip of a matte with nothing written: names the state and count",
           "no frames written yet" in r6.stderr, r6.stderr[-300:])

        _advance(base, job_id, times=2)         # queued -> running, 3 of 9

        r7 = run_cli(["mask", "show", bad_matte, "--json"], env=env)
        ok("show on a running matte: exit 0", r7.returncode == 0,
           r7.stderr[-300:])
        idx = json.loads(r7.stdout) if r7.returncode == 0 else {}
        ok("show: the span comes from the written frames, not the request",
           (idx.get("span") or {}).get("end_frame") == 3, idx.get("span"))
        ok("show: the span says it is frozen outside itself",
           (idx.get("span") or {}).get("frozen_outside_span") is True,
           idx.get("span"))
        q = idx.get("quality") or {}
        ok("show: the lost frame is flagged suspect",
           int(q.get("suspect_count") or 0) >= 1, q)
        ok("show: the reason is the area going to zero",
           "zero_area" in (q.get("reasons") or {}) and
           q["reasons"]["zero_area"] >= 1, q)

        r8 = run_cli(["mask", "show", bad_matte], env=env)
        ok("show (text): prints the span", "span      " in r8.stdout,
           r8.stdout[:400])
        ok("show (text): prints frozen outside span",
           "frozen outside span" in r8.stdout, r8.stdout[:600])
        ok("show (text): prints the suspect frames",
           "SUSPECT frames" in r8.stdout, r8.stdout[:600])

        out1 = str(Path(d) / "partial.jpg")
        r9 = run_cli(["mask", "show", bad_matte, "--strip", "-o", out1,
                     "--width", "80"], env=env)
        ok("strip of a RUNNING matte: exit 0 (this used to be a TypeError)",
           r9.returncode == 0, r9.stderr[-500:])
        ok("strip of a RUNNING matte: wrote a file",
           Path(out1).is_file() and Path(out1).stat().st_size > 0)

    _advance(base, job_id, times=3)             # to done, 9 of 9

    # -- gap 6: the list is a summary; --full is the old payload -----------
    r10 = run_cli(["mask", "list", "SUSPECT_CLIP", "--json"], env=env)
    ok("list: exit 0", r10.returncode == 0, r10.stderr[-300:])
    lst = json.loads(r10.stdout) if r10.returncode == 0 else {}
    row = (lst.get("mattes") or [{}])[0]
    ok("list: no per frame arrays by default",
       "areas" not in row and "scores" not in row, sorted(row))
    ok("list: the summary carries the span, coverage and mean score",
       {"span", "coverage", "mean_score"} <= set(row), sorted(row))
    ok("list: the summary carries the quality block", "quality" in row,
       sorted(row))

    r11 = run_cli(["mask", "list", "SUSPECT_CLIP", "--full", "--json"], env=env)
    full_row = (json.loads(r11.stdout or "{}").get("mattes") or [{}])[0]
    ok("list --full: the per frame arrays are back", "areas" in full_row,
       sorted(full_row))
    ok("list --full: it is the same matte", full_row.get("matte_id") ==
       row.get("matte_id"))

    r12 = run_cli(["mask", "list", "SUSPECT_CLIP"], env=env)
    ok("list (text): one line per matte with its span",
       "span " in r12.stdout and "coverage=" in r12.stdout, r12.stdout[:400])
    ok("list (text): flags the suspect frames", "SUSPECT" in r12.stdout,
       r12.stdout[:400])
    ok("list (text): says every matte is frozen outside its span",
       "frozen outside its span" in r12.stdout, r12.stdout[-300:])


# --------------------------------------------------------------------------
# 7. where the CLI looks for things: a bare clip name and a matte store
#    resolved through the server (gaps 4 and 5), and --preset's two
#    namespaces (gap 17)
# --------------------------------------------------------------------------

def test_cli_paths(base: str) -> None:
    print("\n== a bare clip name and a matte store through the server ==")
    try:
        import mattes as MT
    except ImportError:
        note("grade/mattes.py is not importable; gaps 4 and 5 not exercised")
        return
    import numpy as np

    with tempfile.TemporaryDirectory(prefix="mask_cli_paths_") as root_s:
        root = Path(root_s)
        footage = root / "footage"
        footage.mkdir()
        clip_path = footage / "bare.mp4"
        _make_synthetic_clip(clip_path)

        data = root / "data"
        matte_dir = data / "mattes" / "bare" / "m_paths"
        matte_dir.mkdir(parents=True)
        mw, mh, fps = 32, 18, 10.0
        for i in range(10):
            arr = np.zeros((mh, mw), dtype=np.uint8)
            arr[:, :mw // 2] = 255
            MT.write_gray_png(matte_dir / MT.frame_name(i), arr)
        (matte_dir / "index.json").write_text(json.dumps({
            "matte_id": "m_paths", "clip": "bare.mp4", "clip_key": "bare",
            "rotation": "auto", "fps": fps, "frames": 10, "width": mw,
            "height": mh, "recipe": {}, "state": "done", "done_frames": 10,
            "areas": [0.5] * 10, "scores": [0.9] * 10, "created": time.time(),
            "model": "stub", "backend": "stub",
        }))

        # What the fake server answers on /api/health, which is where the CLI
        # now asks (one request, cached for the process).
        FAKE.STATE.paths = {"footage_dir": str(footage),
                            "data_dir": str(data),
                            "matte_root": str(data / "mattes")}

        # No CINEGRADE_MATTE_ROOT and no STUDIO_DATA_DIR: the server is the
        # only thing that knows where either of these lives.
        env = {"STUDIO_URL": base, "CINEGRADE_MATTE_ROOT": "",
               "STUDIO_DATA_DIR": ""}

        r = run_cli(["stats", "bare.mp4", "--time", "0.2", "--json"], env=env)
        ok("a bare clip name resolves through the server's footage root",
           r.returncode == 0, r.stderr[-400:])

        r2 = run_cli(["stats", "bare.mp4", "--time", "0.2",
                     "--matte", "m_paths", "--json"], env=env)
        ok("a matte id resolves through the server's data dir",
           r2.returncode == 0, r2.stderr[-400:])
        ok("gap 11: the measurement says how wide it measured",
           json.loads(r2.stdout or "{}").get("measured_width") is not None,
           r2.stdout[:200])

        # An explicit CINEGRADE_MATTE_ROOT still wins outright.
        r3 = run_cli(["stats", str(clip_path), "--time", "0.2",
                     "--matte", "m_paths"],
                     env={"STUDIO_URL": base,
                          "CINEGRADE_MATTE_ROOT": str(root / "empty"),
                          "STUDIO_DATA_DIR": ""})
        ok("an explicit CINEGRADE_MATTE_ROOT beats the server",
           r3.returncode != 0 and "m_paths" in r3.stderr, r3.stderr[-300:])

        r4 = run_cli(["stats", "not-a-clip.mov", "--time", "0.2"], env=env)
        ok("a name nothing has: refused with where it looked",
           r4.returncode != 0 and "Looked in" in r4.stderr, r4.stderr[-300:])

        FAKE.STATE.paths = {}

    print("\n== --preset's two namespaces (gap 17) ==")
    import cinegrade as cg

    saved_marker = 0.4242
    FAKE.STATE.presets["cinekit"] = {"primaries": {"saturation": saved_marker}}
    cg.RUNNING_AS_CLI = True
    cg._SERVER_PATHS = None
    os.environ["STUDIO_URL"] = base
    try:
        auto = cg.load_preset("cinekit")
        ok("auto: a studio saved preset wins over the built-in catalog",
           auto["primaries"]["saturation"] == saved_marker,
           auto["primaries"]["saturation"])
        forced_catalog = cg.load_preset("cinekit", source="catalog")
        ok("--preset-from catalog: the built-in look, whatever the studio has",
           forced_catalog["primaries"]["saturation"] != saved_marker,
           forced_catalog["primaries"]["saturation"])
        forced_studio = cg.load_preset("cinekit", source="studio")
        ok("--preset-from studio: the saved one",
           forced_studio["primaries"]["saturation"] == saved_marker)
        fell_through = cg.load_preset("blockbuster")
        ok("auto: a name the studio does not have falls through to the catalog",
           isinstance(fell_through, dict) and "primaries" in fell_through)
        raised = ""
        try:
            cg.load_preset("blockbuster", source="studio")
        except cg.GradeError as exc:
            raised = str(exc)
        ok("--preset-from studio on a name it has not: refused, no fallthrough",
           "no saved preset" in raised, raised[:200])
        raised2 = ""
        try:
            cg.load_preset("no-such-look-anywhere")
        except cg.GradeError as exc:
            raised2 = str(exc)
        ok("a name in neither namespace: refused, naming both",
           "Looked in" in raised2 and "catalog" in raised2, raised2[:200])
    finally:
        FAKE.STATE.presets.pop("cinekit", None)
        os.environ.pop("STUDIO_URL", None)
        cg._SERVER_PATHS = None
        cg.RUNNING_AS_CLI = False
        cg.set_preset_source("auto")


def main() -> int:
    print("starting the fake studio server...")
    srv, th, port = FAKE.start(0)
    base = f"http://127.0.0.1:{port}"
    print(f"fake studio server up at {base}")
    try:
        test_mask_cli(base)
        test_grade_client(base)
        test_mask_gaps(base)
        test_cli_paths(base)
        test_frame_stats_weight()
        test_stats_matte()
        test_render_allow_partial()
    finally:
        srv.shutdown()

    print(f"\n{PASS} passed, {FAIL} failed" + (f", {len(NOTES)} note(s)" if NOTES else ""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
