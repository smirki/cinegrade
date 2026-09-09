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
import shutil
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

# Round 1 finding 19: the count this suite is declared to run, checked in
# main(). Three blocks used to be able to vanish on an ImportError and the run
# still exited 0 behind a smaller printed number, which no reader could tell
# from a pass. A FLOOR rather than an equality on purpose: adding checks must
# never turn a suite red, and more than one lane adds to this file, so the
# rule is "never fewer". Raise it deliberately when you add checks; lowering
# it to make a run green is the thing this exists to stop.
# Round 2: 257 -> 272, the exact live count after the ownership cases for
# findings 51 and 6 (preset and sweep), the hostile pick id block for finding
# 69, and the readable-key-on-another-clip case for finding 71.
# Round 4 tooling: 272 -> 289, the live count after the cancelled-not-failed
# rows for gap 26, the cleared-window checks for gap 24 and the frame width
# checks for gap 27.
EXPECTED_CHECKS = 289


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
        # A `*` glob, not `*.png` (round 1 finding 16): the CLI used to take
        # the extension from the preview URL, and the real server's preview
        # URLs (/api/mask/pick/<pick>/<inst>/overlay) have none, so every file
        # was named .png whatever the bytes were. Counting only *.png hid it.
        files = sorted(q for q in Path(d).iterdir() if q.is_file())
        ok("segment -o DIR: downloaded one overlay and one mask per instance",
           len(files) == 4, f"found {len(files)}: {[f.name for f in files]}")
        heads = {q.name: q.read_bytes()[:8] for q in files}
        ok("segment -o DIR: every downloaded preview is real image bytes",
           all(v.startswith(b"\x89PNG") or v.startswith(b"\xff\xd8\xff")
               for v in heads.values()), heads)
        # Every file says what it IS. The overlay arrives as image/jpeg (the
        # server's own content type for an overlay) and the mask preview as
        # image/png, and the name each is written under now comes from the
        # bytes rather than from a URL that carries no extension at all.
        magic = {".png": b"\x89PNG\r\n\x1a\n", ".jpg": b"\xff\xd8\xff"}
        for name, head in sorted(heads.items()):
            suffix = Path(name).suffix
            ok(f"segment -o DIR: {name} really is a {suffix.lstrip('.')} file",
               suffix in magic and head.startswith(magic[suffix]),
               f"{name}: {head!r}")
        ok("segment -o DIR: the overlay is the JPEG the server serves, under "
           "a .jpg name",
           any(n.endswith("-overlay.jpg") for n in heads), sorted(heads))
        ok("segment -o DIR: the mask preview is the PNG the server serves, "
           "under a .png name",
           any(n.endswith("-mask.png") for n in heads), sorted(heads))

    # Round 2 finding 69: the file NAME is built out of two fields that come
    # straight off the wire, and `--url` lets the caller point the CLI at any
    # server. A pick_id of "../../x" wrote outside -o entirely. The fake
    # answers with exactly that shape when a prompt says "escape"; the
    # preview urls stay honest, so what is under test is the name, not the
    # download.
    with tempfile.TemporaryDirectory(prefix="mask_segment_escape_") as d:
        box = Path(d) / "box"
        box.mkdir()
        r_esc = run_cli(["mask", "segment", "C015.mov", "--time", "1.0",
                        "--text", "escape artist", "--json", "-o", str(box)],
                        env=env)
        ok("segment -o DIR with a hostile pick id: still exit 0",
           r_esc.returncode == 0, r_esc.stderr[-300:])
        inside = sorted(q.name for q in box.iterdir() if q.is_file())
        outside = sorted(q.name for q in Path(d).iterdir() if q.is_file())
        ok("segment -o DIR: every file landed INSIDE the folder that was "
           "asked for", len(inside) == 4 and not outside,
           f"inside {inside} / outside {outside}")
        # A literal ".." INSIDE a longer name is harmless (it is one path
        # component either way); what must not survive is a separator, a name
        # that IS a parent, or a leading dot that hides the file.
        ok("segment -o DIR: and every name is one plain path component",
           all("/" not in n and "\\" not in n and not n.startswith(".")
               and Path(n).name == n and n not in ("..", ".")
               for n in inside), inside)
        ok("segment -o DIR: the hostile characters became underscores rather "
           "than being dropped, so two different ids still make two "
           "different names", len(set(inside)) == len(inside), inside)
        ok("segment -o DIR: nothing was written above the temporary root "
           "either", not list(Path(d).parent.glob("escaped-*")),
           str(Path(d).parent))

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
    # Round 1 finding 39: this said `>= 2` when exactly two were queued, so a
    # queue that leaked a job per call passed too. This block is the first one
    # main() runs, so two is the whole queue, and each one's state is the
    # outcome its own track had: one ran to done under --wait, the FAIL_CLIP
    # one failed.
    ok("jobs: exactly the two jobs queued above and no others",
       len(jobs) == 2, jobs)
    ok("jobs: one finished and one failed, which is what was queued",
       sorted(j.get("state") for j in jobs) == ["done", "failed"], jobs)
    # The real server's job view keys this `id` (studio/server.py
    # `_mask_job_view`), and `_cmd_mask_jobs` falls back to it; the fake used
    # to send `job_id` so that fallback was never exercised (finding 16).
    ok("jobs: every row carries the `id` the real server sends",
       all(bool(j.get("id")) for j in jobs), jobs)
    r5t = run_cli(["mask", "jobs"], env=env)
    ok("jobs (text): prints the job ids, not a row of question marks",
       all(str(j["id"]) in r5t.stdout for j in jobs), r5t.stdout[:300])

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

    # DELIBERATELY RE-POINTED (gap 23). This used to assert that an all-zero
    # weight raises StatsError. It does not any more: a mask that happens to
    # cover no pixel of one frame is a normal thing on a moving subject, not a
    # broken call, so frame_stats now answers with a no-coverage row that a
    # caller looping over timestamps can print and move past.
    empty = ST.frame_stats(rgb, weight=np.zeros((8, 8)))
    ok("all-zero weight returns a no-coverage row instead of raising",
       empty.get("no_coverage") is True, empty)
    ok("no-coverage row says coverage is exactly zero",
       empty.get("coverage") == 0.0, empty.get("coverage"))
    ok("no-coverage row nulls every measurement block, it does not zero them",
       all(empty[name] is None for name in ST.MEASURED_BLOCKS),
       {name: empty[name] for name in ST.MEASURED_BLOCKS})
    ok("no-coverage row still carries the definitions block",
       isinstance(empty.get("definitions"), dict) and bool(empty["definitions"]),
       empty.get("definitions"))

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
    print("\n== stats --matte (contract C6) ==")
    # Round 1 finding 19: this block used to open with `try: import mattes
    # except ImportError: note(...); return`, guarding against grade/mattes.py
    # "not being on this branch yet". It is on this branch. All that guard
    # could do was turn a real breakage (a bad import inside mattes.py) into
    # a quieter green run with dozens of checks silently missing, which the
    # exit code and the printed total could not distinguish from a pass. A
    # missing module is now an ImportError that stops the suite.
    import mattes as MT

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
        # Round 1 finding 17. This used to be
        #     row["stats"] != json.loads(r_plain.stdout)["stats"]
        # with r_plain measured at --time 1.0 and r_matte at --time 0.5: two
        # different frames, so the two blocks already differ before --matte
        # does anything. The control below proves that on this very clip (the
        # saturation mean moves between 0.5 s and 1.0 s), which is why the old
        # assertion would have passed with --matte implemented as a no-op.
        #
        # What replaces it is a hand-checkable equality, not an inequality.
        # The fixture matte is the LEFT HALF of the frame and nothing else, so
        # weighting by it must land on what measuring the left half by region
        # lands on, and nowhere near the right half. Measured on this clip:
        # plain 0.5022, matte 0.4035, region left 0.3979, region right 0.6065.
        r_plain_same = run_cli(["stats", str(clip_path), "--time", "0.5",
                               "--json"], env=env)
        plain_same = json.loads(r_plain_same.stdout) if \
            r_plain_same.returncode == 0 else {}
        ok("control: two unweighted measurements at different times already "
           "differ, so comparing across times proves nothing about --matte",
           plain_same.get("stats") != json.loads(r_plain.stdout).get("stats"),
           (plain_same.get("stats", {}).get("saturation", {}).get("mean"),
            json.loads(r_plain.stdout).get("stats", {})
            .get("saturation", {}).get("mean")))
        r_left = run_cli(["stats", str(clip_path), "--time", "0.5",
                         "--region", "0", "0", "0.5", "1", "--json"], env=env)
        r_right = run_cli(["stats", str(clip_path), "--time", "0.5",
                          "--region", "0.5", "0", "1", "1", "--json"], env=env)
        left = json.loads(r_left.stdout) if r_left.returncode == 0 else {}
        right = json.loads(r_right.stdout) if r_right.returncode == 0 else {}
        m_luma = ((row.get("stats") or {}).get("luma") or {}).get("mean")
        p_luma = ((plain_same.get("stats") or {}).get("luma") or {}).get("mean")
        l_luma = ((left.get("stats") or {}).get("luma") or {}).get("mean")
        r_luma = ((right.get("stats") or {}).get("luma") or {}).get("mean")
        ok("stats --matte: the weighted mean is not the whole frame's mean, "
           "measured at the SAME time",
           None not in (m_luma, p_luma) and abs(m_luma - p_luma) > 0.05,
           (m_luma, p_luma))
        ok("stats --matte on a left-half matte reads the left half: it agrees "
           "with --region 0 0 0.5 1 to better than a code value",
           None not in (m_luma, l_luma) and abs(m_luma - l_luma) < 0.01,
           (m_luma, l_luma))
        ok("... and is nowhere near the half it does not cover",
           None not in (m_luma, r_luma) and abs(m_luma - r_luma) > 0.15,
           (m_luma, r_luma))
        # Finding 39, the same rule on the CLI side: C4's coverage is
        # written / span_len, and half the frame is 0.5 exactly.
        ok("stats --matte: coverage is the fraction of the frame the matte "
           "really covers, to the number",
           abs(float(row.get("coverage") or 0.0) - 0.5) < 0.02,
           row.get("coverage"))

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

        # Checkpoint gap 23, DELIBERATELY RE-POINTED. This used to assert a
        # non-zero exit and an error message. A matte that covers nothing in
        # a frame is a normal, expected outcome (a sky matte after the camera
        # tilts down, a person matte inside its own tracking gap), and
        # failing on it meant a script measuring a list of timestamps died on
        # the first empty one. It is now an ordinary row saying so, and the
        # numbers are null rather than zero, which a caller cannot mistake
        # for a real measurement of a black frame.
        r_zero = run_cli(["stats", str(clip_path), "--time", "0.0",
                         "--matte", "m_zero"], env=env)
        ok("stats --matte with a matte that covers nothing: exits 0 (gap 23)",
           r_zero.returncode == 0, r_zero.stderr[-300:])
        ok("stats --matte with nothing covered: the printed row says so",
           "no coverage" in r_zero.stdout.lower(), r_zero.stdout[:300])
        r_zero_j = run_cli(["stats", str(clip_path), "--time", "0.0",
                           "--matte", "m_zero", "--json"], env=env)
        zrow = json.loads(r_zero_j.stdout) if r_zero_j.returncode == 0 else {}
        ok("no coverage: the flag is on the row and inside stats",
           zrow.get("no_coverage") is True
           and zrow.get("stats", {}).get("no_coverage") is True, zrow)
        ok("no coverage: coverage is exactly zero",
           zrow.get("coverage") == 0.0, zrow.get("coverage"))
        ok("no coverage: every measurement block is null, not zero",
           all(zrow.get("stats", {}).get(k, "missing") is None
               for k in ("luma", "saturation", "channels", "families",
                         "clipped", "bands")),
           sorted((zrow.get("stats") or {}).items())[:3])

        # A loop over timestamps has to survive the empty ones: one call, one
        # row per time, the covered frames measured and the empty one flagged.
        r_times = run_cli(["stats", str(clip_path), "--times", "0.0,0.5",
                          "--matte", "m_partial", "--json"], env=env)
        ok("stats --times --matte: exit 0", r_times.returncode == 0,
           r_times.stderr[-300:])
        rows = (json.loads(r_times.stdout).get("results")
                if r_times.returncode == 0 else []) or []
        ok("stats --times --matte: one row per time", len(rows) == 2, len(rows))
        ok("stats --times: every row says its coverage",
           all(r.get("coverage") is not None for r in rows), rows)

        r_missing = run_cli(["stats", str(clip_path), "--time", "0.0",
                            "--matte", "does-not-exist"], env=env)
        ok("stats --matte with an unknown id: exits non zero",
           r_missing.returncode != 0)

        r_image = run_cli(["stats", "--image", str(clip_path), "--matte", "m_partial"],
                          env=env)
        ok("stats --image with --matte: refused",
           r_image.returncode != 0 and "still" in r_image.stderr.lower(),
           r_image.stderr[:200])

        _check_stats_mask(clip_path, env)


def _check_stats_mask(clip_path: Path, env: dict) -> None:
    """`stats --mask`: a whole mask STACK, not one matte id (gap 19).

    The measurement the round 4 skin anchor needed, "the person matte
    intersected with a skin key", could not be said through the documented
    tools at all, so it was hand built against the engine's internal modules
    and then separately proved against a real server render before any number
    from it could be trusted. What is pinned here is the property that makes
    that proof unnecessary: `--mask` folds the stack with the engine's OWN
    mask_matte, the same code the layer renderer uses, so the one component
    stack that names a single matte measures byte for byte what `--matte`
    measures, and every op then changes the answer in the direction it says.
    """
    print("\n== stats --mask, a whole component stack (gap 19) ==")

    def measure(spec, extra=(), time="0.5"):
        args = ["stats", str(clip_path), "--time", time, "--json"]
        if spec is not None:
            args += ["--mask", json.dumps(spec) if isinstance(spec, dict) else spec]
        r = run_cli(args + list(extra), env=env)
        if r.returncode != 0:
            return None, r
        try:
            return json.loads(r.stdout), r
        except json.JSONDecodeError:
            return None, r

    one_matte = {"components": [{"type": "matte", "op": "add",
                                 "matte": {"id": "m_partial"}}]}
    stack_row, r_stack = measure(one_matte)
    ok("stats --mask (one matte component): exit 0", stack_row is not None,
       r_stack.stderr[-400:])
    r_matte = run_cli(["stats", str(clip_path), "--time", "0.5",
                      "--matte", "m_partial", "--json"], env=env)
    matte_row = json.loads(r_matte.stdout) if r_matte.returncode == 0 else {}
    if stack_row:
        ok("a one-component stack measures exactly what --matte measures: the "
           "stack is not a second implementation of the fold",
           stack_row["stats"] == matte_row.get("stats"),
           (stack_row["stats"]["luma"]["mean"],
            matte_row.get("stats", {}).get("luma", {}).get("mean")))
        ok("stats --mask says which mattes it reached",
           stack_row.get("mask_mattes") == ["m_partial"],
           stack_row.get("mask_mattes"))
        ok("stats --mask reports coverage and the width it measured at",
           stack_row.get("coverage") is not None
           and stack_row.get("measured_width") == stack_row["size"][0],
           (stack_row.get("coverage"), stack_row.get("measured_width")))

    # The matte fixture is the LEFT half of the frame. Intersecting it with a
    # luma key can only take pixels away, subtracting a window can only take
    # pixels away, and inverting the component turns it into the right half:
    # three ops, three directions, all against the one number above.
    base_coverage = (stack_row or {}).get("coverage")
    narrowed, r_narrow = measure({"components": [
        {"type": "matte", "op": "add", "matte": {"id": "m_partial"}},
        {"type": "luma", "op": "intersect",
         "key": {"lum_low": 0.5, "lum_high": 1.0, "lum_soft": 0.02}}]})
    ok("stats --mask (matte intersect luma key): exit 0", narrowed is not None,
       r_narrow.stderr[-400:])
    if narrowed and base_coverage is not None:
        ok("intersecting a key with the matte covers LESS of the frame than "
           "the matte alone", narrowed["coverage"] < base_coverage,
           (narrowed["coverage"], base_coverage))
        ok("and the numbers move with it, they are not the matte's own",
           narrowed["stats"]["luma"]["mean"]
           != (stack_row or {})["stats"]["luma"]["mean"],
           (narrowed["stats"]["luma"]["mean"],
            (stack_row or {})["stats"]["luma"]["mean"]))

    subtracted, r_sub = measure({"components": [
        {"type": "matte", "op": "add", "matte": {"id": "m_partial"}},
        {"type": "window", "op": "subtract",
         "window": {"shape": "rect", "w": 0.5, "h": 0.5, "softness": 0.0}}]})
    ok("stats --mask (matte subtract window): exit 0", subtracted is not None,
       r_sub.stderr[-400:])
    if subtracted and base_coverage is not None:
        ok("subtracting a window covers less than the matte alone",
           subtracted["coverage"] < base_coverage,
           (subtracted["coverage"], base_coverage))

    flipped, r_flip = measure({"components": [
        {"type": "matte", "op": "add", "matte": {"id": "m_partial"},
         "invert": True}]})
    ok("stats --mask (inverted matte component): exit 0", flipped is not None,
       r_flip.stderr[-400:])
    if flipped and stack_row:
        ok("inverting the component measures the other half of the picture",
           flipped["stats"]["luma"]["mean"] != stack_row["stats"]["luma"]["mean"],
           (flipped["stats"]["luma"]["mean"],
            stack_row["stats"]["luma"]["mean"]))

    feathered, r_feather = measure({"components": [
        {"type": "matte", "op": "add", "matte": {"id": "m_partial"},
         "feather": 0.05}]})
    ok("stats --mask honours a component's own feather", feathered is not None,
       r_feather.stderr[-400:])

    # A mask description can also be a FILE, which is what an agent building a
    # stack of several components in a checkpoint actually has on disk.
    spec_file = clip_path.parent / "skin-stack.json"
    spec_file.write_text(json.dumps(one_matte))
    from_file, r_file = measure(str(spec_file))
    ok("stats --mask FILE reads the same description off disk",
       from_file is not None and from_file["stats"] == (stack_row or {}).get("stats"),
       r_file.stderr[-400:])

    # A stack that covers nothing is the gap 23 row, not a crash: the same
    # rule a bare matte id follows.
    empty, r_empty = measure({"components": [
        {"type": "matte", "op": "add", "matte": {"id": "m_zero"}}]}, time="0.0")
    ok("stats --mask on a stack that covers nothing: exit 0 and no_coverage",
       empty is not None and empty.get("no_coverage") is True,
       r_empty.stderr[-300:] if empty is None else empty.get("no_coverage"))

    # Refusals. Every one of these would otherwise be a silently wrong number.
    def refuses(label, args, wanted):
        r = run_cli(["stats", str(clip_path), "--time", "0.5"] + args, env=env)
        ok(f"refused: {label}",
           r.returncode != 0 and wanted in r.stderr.lower(),
           f"exit {r.returncode}: {r.stderr[-220:]}")

    refuses("--mask with an unknown matte id",
            ["--mask", json.dumps({"components": [
                {"type": "matte", "op": "add",
                 "matte": {"id": "no-such-matte"}}]})], "no-such-matte")
    refuses("--mask with a matte component that has no id yet",
            ["--mask", json.dumps({"components": [
                {"type": "matte", "op": "add", "matte": {"id": ""}}]})],
            "no matte id yet")
    refuses("--mask whose stack starts with an intersect (folds from zero)",
            ["--mask", json.dumps({"components": [
                {"type": "luma", "op": "intersect"}]})],
            "no component reaches the matte")
    refuses("--mask '{}' (an empty description would measure everything)",
            ["--mask", "{}"], "nothing to measure through")
    refuses("--mask together with --matte",
            ["--mask", json.dumps(one_matte), "--matte", "m_partial"],
            "both weight the measurement")
    refuses("--mask together with --region",
            ["--mask", json.dumps(one_matte), "--region", "0", "0", "0.5", "1"],
            "do not compose")
    refuses("--mask that is not JSON and not a file",
            ["--mask", "person intersect skin"], "neither an existing file")
    # No clip positional on this one: --image and a positional together is a
    # different refusal ("use one or the other"), and the rule under test here
    # is that a component stack needs a clip's own frames over time.
    r_still = run_cli(["stats", "--image", str(clip_path),
                      "--mask", json.dumps(one_matte)], env=env)
    ok("refused: --mask on a still (--image)",
       r_still.returncode != 0 and "one still" in r_still.stderr,
       f"exit {r_still.returncode}: {r_still.stderr[-220:]}")


# --------------------------------------------------------------------------
# 5. render --allow-partial, end to end through the CLI (integration lane
#    item 2: the render verb used to build its ffmpeg inputs with
#    strict_mattes always False, so a partial matte rendered silently and
#    --allow-partial did not exist as a flag at all)
# --------------------------------------------------------------------------

def test_render_allow_partial() -> None:
    print("\n== render --allow-partial ==")
    # Round 1 finding 19: no ImportError guard. Both modules are on this
    # branch, and a suite that skips its own subject on an import error and
    # still exits 0 is worse than one that stops.
    import cinegrade as cg
    import mattes as MT
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

    # -- gap 27: which frame the model was shown ---------------------------
    # A grader's 0 candidates for words another session had picked with was
    # not the words: that studio ran at --mask-width 720 and the other at the
    # 1280 default, and the model's confidence for those words fell under its
    # own cutoff at the smaller size. Nothing printed said which frame the
    # model saw, so the two sessions had no number to compare. Now both the
    # empty answer and a successful pick name it.
    ok("segment with no matches: the sentence names the frame width, so a "
       "width mismatch is tellable from a model miss",
       f"0 candidates from the model on a {FAKE.MASK_FRAME_WIDTH}px frame"
       in r.stderr, r.stderr[:400])
    ok("segment with no matches: --json carries the width as a field",
       json.loads(r_j.stdout or "{}").get("frame_width")
       == FAKE.MASK_FRAME_WIDTH, r_j.stdout[:200])
    r_w = run_cli(["mask", "segment", "C015.mov", "--time", "1.0",
                  "--text", "person"], env=env)
    ok("segment that found something: exit 0", r_w.returncode == 0,
       r_w.stderr[-300:])
    ok("segment that found something: names the frame width too, so two "
       "sessions comparing picks can compare widths",
       f"on a {FAKE.MASK_FRAME_WIDTH}px frame" in r_w.stdout, r_w.stdout[:300])

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

    # -- tooling gap 24: what --force cleared ------------------------------
    # The grader forced frames 173 to 197 of a matte done over 0 to 288 to
    # repair one second of it, and got back "force: previous frames cleared,
    # tracking again" beside the numbers 173 and 197. That reads as "those
    # frames were cleared"; what had happened was the whole matte going, and
    # the 264 verified frames outside the repair window with it. The answer
    # now names the range that went and whether it was all of it, on both the
    # JSON path and the printed one.
    ok("track --force over the whole clip: says it cleared the whole matte",
       forced.get("cleared_whole_matte") is True, forced)
    ok("track --force over the whole clip: names the range it cleared",
       (forced.get("cleared_start"), forced.get("cleared_end"))
       == (0, FAKE.CLIP_FRAMES), forced)
    lo_t, hi_t = 2 / FAKE.MATTE_FPS, 4 / FAKE.MATTE_FPS
    r_nf = run_cli(["mask", "track", "C015.mov", "--text", "cache-probe",
                   "--start", f"{lo_t:.6f}", "--end", f"{hi_t:.6f}",
                   "--force", "--json"], env=env)
    narrow = json.loads(r_nf.stdout) if r_nf.returncode == 0 else {}
    ok("track --force on a window inside the matte: exit 0",
       r_nf.returncode == 0, r_nf.stderr[-300:])
    ok("track --force on a window inside the matte: clears that window only",
       (narrow.get("cleared_start"), narrow.get("cleared_end"),
        narrow.get("cleared_whole_matte")) == (2, 4, False), narrow)
    ok("and it queues that window only, so the frames outside it are kept "
       "rather than tracked again",
       (narrow.get("start_frame"), narrow.get("end_frame")) == (2, 4), narrow)
    ok("and the message says which frames went and which stayed",
       "frames 2 to 4 cleared" in str(narrow.get("message"))
       and "kept" in str(narrow.get("message")), narrow.get("message"))
    r_nfp = run_cli(["mask", "track", "C015.mov", "--text", "cache-probe",
                    "--start", f"{lo_t:.6f}", "--end", f"{hi_t:.6f}",
                    "--force"], env=env)
    ok("and the printed answer names the cleared range too, not just --json",
       "cleared" in r_nfp.stdout and "frames 2 to 4" in r_nfp.stdout,
       r_nfp.stdout[:400])

    # -- round 1 finding 40: rotation is part of the cache key -------------
    # Design rule 5 keys a track by clip identity, rotation, working width and
    # recipe hash. Every track in this suite ran at one rotation, so the
    # rotation could have been dropped from the key (or from the payload) and
    # nothing would have gone red, while a matte tracked upright answered a
    # request for the rotated clip: the same words, a different picture.
    r_rot = run_cli(["mask", "track", "C015.mov", "--text", "cache-probe",
                    "--rotate", "90", "--json"], env=env)
    ok("track at another rotation: exit 0", r_rot.returncode == 0,
       r_rot.stderr[-300:])
    rotated = json.loads(r_rot.stdout) if r_rot.returncode == 0 else {}
    ok("the same words at a different rotation are NOT a cache hit",
       rotated.get("cached") is False, rotated)
    rotated_matte = (rotated.get("mattes") or [{}])[0].get("matte_id")
    ok("and they get their own matte, not the upright one",
       bool(rotated_matte) and rotated_matte != cached_matte,
       [rotated_matte, cached_matte])
    r_show = run_cli(["mask", "show", rotated_matte or "none", "--json"],
                    env=env)
    shown = json.loads(r_show.stdout or "{}") if r_show.returncode == 0 else {}
    ok("and the matte itself remembers the rotation it was tracked at, so a "
       "caller can check the matte against the request",
       str(shown.get("rotation")) == "90", shown.get("rotation"))
    # Let the rotated track finish before asking again: an unfinished matte
    # is a resume, not a cache hit, and the control below is about the KEY,
    # not about how far the track got.
    run_cli(["mask", "track", "C015.mov", "--text", "cache-probe",
            "--rotate", "90", "--wait", "--json"], env=env)
    r_rot2 = run_cli(["mask", "track", "C015.mov", "--text", "cache-probe",
                     "--rotate", "90", "--json"], env=env)
    rotated_again = json.loads(r_rot2.stdout or "{}")
    ok("the control: that rotated request repeated IS a cache hit, so what "
       "changed above was the rotation and not merely a second call",
       rotated_again.get("cached") is True
       and (rotated_again.get("mattes") or [{}])[0].get("matte_id")
       == rotated_matte, rotated_again)

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
        ok("show: while only the first three frames are written, nothing "
           "claims the subject came back yet (frame 3 is not written)",
           (q.get("reasons") or {}).get("area_recover") == 0, q.get("reasons"))

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

    # -- finding 15: the drift rule that needs an `ious` array -------------
    # `low_iou` is the one of quality()'s three rules that cannot fire from
    # `areas` alone: it needs the per frame overlap the store writes (C2,
    # checkpoint gap 18). Nothing in the arc had ever seen it fire, because
    # the stand-in hardcoded the count to 0 and wrote no ious at all. This
    # matte loses its object at frame 2 and latches onto something several
    # times bigger at frame 4, so frame 4 overlaps almost nothing that came
    # before it.
    r_iou = run_cli(["mask", "show", bad_matte, "--json"], env=env)
    q_all = (json.loads(r_iou.stdout or "{}").get("quality") or {})
    ok("show: the quality block says the ious came from the index",
       q_all.get("iou_source") == "index", q_all.get("iou_source"))
    ok("show: the frame that jumped to another object is flagged low_iou",
       (q_all.get("reasons") or {}).get("low_iou") == 1, q_all.get("reasons"))
    jumped = [f for f in (q_all.get("suspect_frames") or [])
              if "low_iou" in (f.get("reasons") or [])]
    ok("show: the low_iou frame is the one that jumped, and carries its own "
       "overlap number",
       len(jumped) == 1 and jumped[0]["index"] == 4
       and float(jumped[0]["iou"]) < 0.3,
       jumped)
    ok("show: a clean stretch of the same matte is not flagged",
       all(f["index"] in (2, 3, 4) for f in (q_all.get("suspect_frames") or [])),
       q_all.get("suspect_frames"))

    # -- finding 22: the frame the area rule is blind to -------------------
    # Frame 3 is the subject coming back after the empty frame 2. `area_jump`
    # divides by the previous area, and that area is 0, so this frame was the
    # one frame of the three that nothing could ever flag, which is the frame
    # a tracker most often comes back on the WRONG object at.
    ok("show: the frame after the empty one is flagged as a recovery",
       (q_all.get("reasons") or {}).get("area_recover") == 1,
       q_all.get("reasons"))
    back = [f for f in (q_all.get("suspect_frames") or [])
            if "area_recover" in (f.get("reasons") or [])]
    ok("show: it is the frame right after the empty one, and it carries no "
       "jump number because there is nothing to divide by",
       len(back) == 1 and back[0]["index"] == 3 and back[0].get("jump") is None,
       back)
    ok("show: the recovery floor is reported beside the other thresholds, so "
       "a count of 0 can be read against what judged it",
       "area_recover" in (q_all.get("thresholds") or {}),
       q_all.get("thresholds"))
    r_back = run_cli(["mask", "show", bad_matte], env=env)
    ok("show (text): names the frame where the subject came back, rather than "
       "counting it in a list",
       "the subject comes back after an empty frame at frame 3" in r_back.stdout,
       r_back.stdout[:900])
    ok("show (text): and says why that frame is worth looking at first",
       "comes back on the wrong object" in r_back.stdout, r_back.stdout[:900])

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

    # -- gap 26: a cancelled matte is not a failed one ---------------------
    # The grader cancelled a track before it started and read
    # `state=failed 0/N frames` off this command, then went looking for a
    # tracking failure that had never happened. `CANCELLED_CLIP` is the fake's
    # hook for the row the real service now writes for that case; FAIL_CLIP
    # beside it is the row that really did fail, so the two are compared
    # rather than one being read on its own.
    run_cli(["mask", "track", "CANCELLED_CLIP", "--text", "stopped-early"],
            env=env)
    for flag in ([], ["--full"]):
        r_c = run_cli(["mask", "list", "CANCELLED_CLIP"] + flag, env=env)
        label = "list --full" if flag else "list"
        ok(f"{label} (text): a cancelled matte says cancelled, not failed",
           "state=cancelled" in r_c.stdout and "state=failed" not in r_c.stdout,
           r_c.stdout[:400])
        ok(f"{label} (text): with the frames it managed out of the frames it "
           f"was asked for",
           f"0/{FAKE.CLIP_FRAMES} frames" in r_c.stdout, r_c.stdout[:400])
    r_cj = run_cli(["mask", "list", "CANCELLED_CLIP", "--json"], env=env)
    c_row = (json.loads(r_cj.stdout or "{}").get("mattes") or [{}])[0]
    ok("list --json: the state on the wire is cancelled too",
       c_row.get("state") == "cancelled", c_row.get("state"))
    run_cli(["mask", "track", "FAIL_CLIP", "--text", "really-failed"], env=env)
    r_f = run_cli(["mask", "list", "FAIL_CLIP"], env=env)
    ok("list (text): a matte that really failed still says failed, so the two "
       "outcomes are told apart",
       "state=failed" in r_f.stdout and "state=cancelled" not in r_f.stdout,
       r_f.stdout[:400])


# --------------------------------------------------------------------------
# 6b. the four things a repeated track can be, through the CLI (round 1
#     findings 5, 16, 21 and 39)
#
#     None of this could be tested here before: the stand-in server answered
#     a DEAD matte with `resumed: True, restarted: False, resumed_from:
#     <done_frames>`, which is the pre-fix server behaviour verbatim, so a CLI
#     test written against the corrected server would have failed against the
#     fixture rather than against the code. The fake now makes the same
#     decision the real route makes, in the same order.
# --------------------------------------------------------------------------

def _raw_headers(url: str) -> tuple:
    """One GET, returning (bytes, headers). The CLI has no verb that prints
    the matte frame headers, and grade_client drops X-Matte-Warning, so the
    header is read off the wire."""
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.read(), dict(r.headers)


def test_mask_resume_paths(base: str) -> None:
    print("\n== a repeated track: resume, widen, restart, cache hit ==")
    env = {"STUDIO_URL": base}

    # -- a DEAD matte restarts, it does not resume (findings 16 and 21) ----
    r1 = run_cli(["mask", "track", "FAIL_CLIP", "--text", "dead-and-retried",
                 "--json"], env=env)
    ok("a track that fails: exit 0 on the queue call itself",
       r1.returncode == 0, r1.stderr[-300:])
    first = json.loads(r1.stdout or "{}")
    dead_matte = (first.get("mattes") or [{}])[0].get("matte_id")
    ok("a track that fails: its matte says failed",
       (first.get("mattes") or [{}])[0].get("state") == "failed", first)

    r2 = run_cli(["mask", "track", "FAIL_CLIP", "--text", "dead-and-retried",
                 "--json"], env=env)
    again = json.loads(r2.stdout or "{}")
    ok("a dead matte is not answered from the cache",
       again.get("cached") is False, again)
    ok("a dead matte RESTARTS: `restarted` true and `resumed` false, not the "
       "other way round", again.get("restarted") is True
       and again.get("resumed") is False, again)
    ok("a dead matte restarts from the start of the window, not from where "
       "the dead attempt stopped", again.get("resumed_from") == 0, again)
    ok("a restart writes back into the same matte, so nothing is orphaned",
       (again.get("mattes") or [{}])[0].get("matte_id") == dead_matte, again)
    ok("and the reason reaches the caller in words",
       "restarting" in (again.get("message") or "")
       and "failed" in (again.get("message") or ""), again.get("message"))

    # -- a WIDER window widens the matte (finding 5) -----------------------
    # 3 frames then 9, of a 9 frame fake clip at 24 fps.
    r3 = run_cli(["mask", "track", "C015.mov", "--text", "widen-me",
                 "--end", "0.125", "--json"], env=env)
    narrow = json.loads(r3.stdout or "{}")
    widen_matte = (narrow.get("mattes") or [{}])[0].get("matte_id")
    ok("a windowed track: the queue answer says which frames it queued",
       narrow.get("start_frame") == 0 and narrow.get("end_frame") == 3, narrow)
    _advance(base, narrow.get("job_id"), times=4)      # queued -> done, 3 of 3
    r3b = run_cli(["mask", "track", "C015.mov", "--text", "widen-me",
                  "--end", "0.125", "--json"], env=env)
    ok("the same narrow window again is a real cache hit",
       json.loads(r3b.stdout or "{}").get("cached") is True, r3b.stdout[:300])

    r4 = run_cli(["mask", "track", "C015.mov", "--text", "widen-me",
                 "--end", "0.375", "--json"], env=env)
    wider = json.loads(r4.stdout or "{}")
    ok("a window WIDER than the matte is not a cache hit: this is finding 5, "
       "and it is the assertion the old fixture could not carry",
       wider.get("cached") is False, wider)
    ok("a wider window WIDENS: `widened` true, `resumed` and `restarted` false",
       wider.get("widened") is True and wider.get("resumed") is False
       and wider.get("restarted") is False, wider)
    ok("a widen starts at the first frame the matte does not have, so every "
       "frame already tracked is kept", wider.get("resumed_from") == 3, wider)
    ok("a widen queues the frames outside the matte and nothing else",
       wider.get("start_frame") == 3 and wider.get("end_frame") == 9, wider)
    ok("a widen names the window asked for, so a reader can check the claim",
       "widening" in (wider.get("message") or "")
       and "9" in (wider.get("message") or ""), wider.get("message"))
    ok("a widen writes back into the same matte",
       (wider.get("mattes") or [{}])[0].get("matte_id") == widen_matte, wider)

    _advance(base, wider.get("job_id"), times=4)       # to done, 9 of 9
    r5 = run_cli(["mask", "track", "C015.mov", "--text", "widen-me",
                 "--end", "0.375", "--json"], env=env)
    wide_again = json.loads(r5.stdout or "{}")
    ok("once it is wide, the wide window is itself a cache hit",
       wide_again.get("cached") is True, wide_again)
    ok("and the cache hit says which frames it decided were covered",
       wide_again.get("start_frame") == 0
       and wide_again.get("end_frame") == 9, wide_again)
    r6 = run_cli(["mask", "track", "C015.mov", "--text", "widen-me",
                 "--end", "0.125", "--json"], env=env)
    ok("a window INSIDE what the matte holds is still a cache hit: widening "
       "must not turn every repeat call into a re-track",
       json.loads(r6.stdout or "{}").get("cached") is True, r6.stdout[:300])

    # -- what the human readable output says about all of that -------------
    r7 = run_cli(["mask", "track", "C015.mov", "--text", "widen-me",
                 "--end", "0.125", "--wait"], env=env)
    ok("track --wait on a cache hit: exit 0", r7.returncode == 0,
       r7.stderr[-300:])
    ok("the cached message names the frames it holds instead of claiming it "
       "'already covers this request' on the word `cached` alone",
       "already holds frames 0 to 3" in r7.stderr, r7.stderr[-300:])
    ok("the printed block says the window and which of the four things "
       "happened", "frames    0 to 3  (cached)" in r7.stdout, r7.stdout[:400])

    # -- the freeze outside the span, and the header that says so (39) -----
    r8 = run_cli(["mask", "track", "C015.mov", "--text", "frozen-probe",
                 "--json"], env=env)
    frozen = json.loads(r8.stdout or "{}")
    frozen_matte = (frozen.get("mattes") or [{}])[0].get("matte_id")
    _advance(base, frozen.get("job_id"), times=2)       # running, 3 of 9
    inside, h_in = _raw_headers(
        f"{base}/api/matte/{frozen_matte}/frame?time=0.0417&width=48")
    ok("a frame inside the span is served as itself, with no warning",
       h_in.get("X-Matte-Frame") == "1" and "X-Matte-Warning" not in h_in,
       {k: v for k, v in h_in.items() if k.startswith("X-Matte")})
    outside, h_out = _raw_headers(
        f"{base}/api/matte/{frozen_matte}/frame?time=0.3&width=48")
    ok("a frame past the span is answered with the nearest written frame, "
       "which is what `frozen outside span` MEANS",
       h_out.get("X-Matte-Frame") == "2", h_out.get("X-Matte-Frame"))
    ok("and the response says out loud that it held an older frame: the one "
       "header the whole arc asserted nowhere",
       "not tracked yet" in (h_out.get("X-Matte-Warning") or ""),
       h_out.get("X-Matte-Warning"))
    ok("the held frame is real image bytes, not an error page",
       outside.startswith(b"\x89PNG"), outside[:8])
    ok("the state header still says the matte is unfinished",
       h_out.get("X-Matte-State") == "running", h_out.get("X-Matte-State"))

    # -- the refusals the real routes make, which the fake now makes too ---
    def refused(label, path, payload, wanted):
        req = urllib.request.Request(
            f"{base}/api/{path}", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
            ok(f"refused: {label}", False, "answered 200")
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read() or b"{}")
            ok(f"refused: {label}",
               exc.code == 400 and wanted in str(body.get("error", "")),
               f"{exc.code}: {body}")

    refused("track with no prompt and no pick", "mask/track",
            {"clip": "C015.mov"}, "at least one prompt")
    refused("track with both a prompt and a pick", "mask/track",
            {"clip": "C015.mov", "prompts": {"text": ["x"]},
             "pick_id": "pick1"}, "not both")
    refused("track from a pick the server has never heard of", "mask/track",
            {"clip": "C015.mov", "pick_id": "pick-nope"}, "no such pick")
    refused("segment with no prompt at all", "mask/segment",
            {"clip": "C015.mov", "time": 1.0}, "at least one prompt")


# --------------------------------------------------------------------------
# 7. where the CLI looks for things: a bare clip name and a matte store
#    resolved through the server (gaps 4 and 5), and --preset's two
#    namespaces (gap 17)
# --------------------------------------------------------------------------

def test_cli_paths(base: str) -> None:
    print("\n== a bare clip name and a matte store through the server ==")
    # Round 1 finding 19: the third of the three guards that could delete a
    # block of checks and leave the suite green. Deleted.
    import mattes as MT
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


# --------------------------------------------------------------------------
# 8. a matte id is an ID, never a path (round 1 blocker 1)
#
#    `mattes.resolve()` used to accept a matte id that was itself a filesystem
#    path, which is what let `DELETE /api/matte//tmp/.../VICTIM` delete any
#    directory on the machine. The CLI and the client are the other two doors
#    the same id walks through, so the pattern is checked at all three.
# --------------------------------------------------------------------------

def test_matte_id_is_never_a_path(base: str) -> None:
    print("\n== a matte id is an id, never a path ==")
    import mattes as MT
    import numpy as np

    import grade_client as GC

    ok("the client's id pattern is grade/mattes.py's, character for character",
       GC.MATTE_ID_PATTERN == MT.MATTE_ID_PATTERN,
       f"{GC.MATTE_ID_PATTERN!r} vs {MT.MATTE_ID_PATTERN!r}")
    for bad in ("/tmp", "/tmp/mattes/m_x", "..", "../m_x", "a/b", "a\\b",
                ".", ".hidden", "", "m" * 65):
        ok(f"not an id: {bad!r}", not MT.valid_matte_id(bad))
    for good in ("m_5214be94f217", "m_paths", "m_m7_left", "not-a-real-matte"):
        ok(f"still an id: {good!r}", MT.valid_matte_id(good))

    with tempfile.TemporaryDirectory(prefix="mask_id_guard_") as root_s:
        root = Path(root_s)
        clip_path = root / "guard.mp4"
        _make_synthetic_clip(clip_path)
        store = root / "mattes"
        matte_dir = store / "guardkey" / "m_guard"
        matte_dir.mkdir(parents=True)
        mw, mh, fps, frames = 32, 24, 10.0, 10
        for i in range(frames):
            arr = np.zeros((mh, mw), dtype=np.uint8)
            arr[:, :mw // 2] = 255
            MT.write_gray_png(matte_dir / MT.frame_name(i), arr)
        (matte_dir / "index.json").write_text(json.dumps({
            "matte_id": "m_guard", "clip": "guard.mp4",
            "clip_key": "guardkey", "rotation": "auto", "fps": fps,
            "frames": frames, "width": mw, "height": mh, "recipe": {},
            "state": "done", "done_frames": frames,
            "areas": [0.5] * frames, "scores": [0.9] * frames,
            "created": time.time(), "model": "stub", "backend": "stub",
        }))
        # A matte directory OUTSIDE the store, reachable through a symlink
        # inside it: the id is well formed and the bytes are somebody else's.
        outside = root / "outside" / "m_escape"
        outside.mkdir(parents=True)
        shutil.copy(matte_dir / "index.json", outside / "index.json")
        shutil.copy(matte_dir / MT.frame_name(0),
                    outside / MT.frame_name(0))
        (store / "m_escape").symlink_to(outside, target_is_directory=True)

        env = {"CINEGRADE_MATTE_ROOT": str(store), "STUDIO_URL": ""}

        r_id = run_cli(["stats", str(clip_path), "--time", "0.2",
                       "--matte", "m_guard", "--json"], env=env)
        ok("stats --matte <id>: measures", r_id.returncode == 0,
           r_id.stderr[-300:])

        r_path = run_cli(["stats", str(clip_path), "--time", "0.2",
                         "--matte", str(matte_dir), "--json"], env=env)
        ok("stats --matte <the matte's own directory>: refused",
           r_path.returncode != 0, r_path.stdout[:200])
        ok("... and says an id is not a path",
           "not a matte id" in r_path.stderr, r_path.stderr[-300:])

        r_up = run_cli(["stats", str(clip_path), "--time", "0.2",
                       "--matte", "../guardkey/m_guard", "--json"], env=env)
        ok("stats --matte ../<something>: refused", r_up.returncode != 0,
           r_up.stdout[:200])

        r_link = run_cli(["stats", str(clip_path), "--time", "0.2",
                         "--matte", "m_escape", "--json"], env=env)
        ok("a matte directory that is a symlink out of the store: refused",
           r_link.returncode != 0, r_link.stdout[:200])
        ok("... and says it left the store",
           "outside the matte store" in r_link.stderr, r_link.stderr[-300:])

    studio = GC.Studio(base=base)
    for bad in ("/tmp/anything", "..", "m_ok/../..", ""):
        for label, call_it in (("matte", lambda: studio.matte(bad)),
                              ("matte_frame",
                               lambda: studio.matte_frame(bad, time=0.0)),
                              ("stats(matte=)",
                               lambda: studio.stats(clip="C015.mov",
                                                    matte=bad))):
            try:
                call_it()
                ok(f"client {label}({bad!r}): refused", False, "no error")
            except GC.StudioError as exc:
                ok(f"client {label}({bad!r}): refused before the request",
                   "not a matte id" in str(exc) and exc.status is None,
                   str(exc)[:160])


# --------------------------------------------------------------------------
# 9. a matte belongs to the clip it was tracked on (round 1 finding 6, the
#    engine half the security lane deferred to this one)
#
#    The studio's routes have refused a cross clip matte since round 1, but
#    the engine run bare, with no server anywhere, did not: `stats --matte`
#    weighted the measurement with another clip's subject and returned
#    numbers with exit 0 (an anchor somebody then grades to), and `render`
#    stretched that subject over the picture and wrote the file. Both now ask
#    the same question the route asks, comparing the matte's recorded
#    clip_key (C2) against the content key of the file in front of them, and
#    refuse in the same sentence.
# --------------------------------------------------------------------------

def _make_other_clip(path: Path) -> None:
    """A second clip whose BYTES differ, so its content key differs too.

    clip_key hashes the size and the first and last mebibyte, so two renders
    of the same testsrc would share a key and prove nothing.
    """
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "smptebars=size=64x48:rate=10:duration=3",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, timeout=30)


def _write_matte_fixture(matte_dir: Path, clip_key: str, clip_name: str,
                         frames: int = 30, fps: float = 10.0) -> None:
    import mattes as MT
    import numpy as np

    matte_dir.mkdir(parents=True, exist_ok=True)
    mw, mh = 32, 24
    for i in range(frames):
        arr = np.zeros((mh, mw), dtype=np.uint8)
        arr[:, :mw // 2] = 255
        MT.write_gray_png(matte_dir / MT.frame_name(i), arr)
    (matte_dir / "index.json").write_text(json.dumps({
        "matte_id": matte_dir.name, "clip": clip_name, "clip_key": clip_key,
        "rotation": "auto", "fps": fps, "frames": frames,
        "width": mw, "height": mh, "recipe": {"prompts": {"text": ["x"]}},
        "state": "done", "done_frames": frames,
        "areas": [0.5] * frames, "scores": [0.9] * frames,
        "created": time.time(), "model": "stub", "backend": "stub",
    }))


def test_matte_belongs_to_this_clip() -> None:
    print("\n== a matte belongs to the clip it was tracked on ==")
    import cinegrade as cg
    import mattes as MT
    from copy import deepcopy

    with tempfile.TemporaryDirectory(prefix="mask_matte_owner_") as root_s:
        root = Path(root_s)
        mine, other = root / "mine.mp4", root / "other.mp4"
        _make_synthetic_clip(mine)
        _make_other_clip(other)

        mine_key, other_key = MT.clip_key(mine), MT.clip_key(other)
        ok("two different clips have two different content keys",
           mine_key != other_key, f"{mine_key} vs {other_key}")
        ok("and the engine's key is the studio's 32 hex shape",
           MT.is_clip_key(mine_key) and MT.is_clip_key(other_key),
           f"{mine_key} / {other_key}")

        store = root / "mattes"
        _write_matte_fixture(store / "m_mine", mine_key, "mine.mp4")
        _write_matte_fixture(store / "m_other", other_key, "other.mp4")
        # A hand built or pre-C2 matte, filed under a readable name rather
        # than a content key. The guard cannot answer the question for one of
        # these, so it must not pretend the answer is "no".
        _write_matte_fixture(store / "m_legacy", "readable-key", "mine.mp4")
        # The same unreadable key, but recorded against the OTHER clip. Round
        # 2 finding 71: the engine used to return "allow" for any key that was
        # not 32 hex, before it looked at anything else, so this one sailed
        # through the CLI while the studio refused it. Now both halves fall
        # back to the recorded file NAME for exactly this case, so both refuse
        # it and both still allow m_legacy above.
        _write_matte_fixture(store / "m_legacy_other", "readable-key",
                             "other.mp4")

        env = {"CINEGRADE_MATTE_ROOT": str(store), "STUDIO_URL": ""}

        # --- stats --matte -------------------------------------------------
        r_bad = run_cli(["stats", str(mine), "--time", "0.2",
                        "--matte", "m_other", "--json"], env=env)
        ok("stats --matte <another clip's matte>: refused",
           r_bad.returncode != 0, r_bad.stdout[:200])
        ok("... in the route's own sentence, naming both clips",
           "was tracked on" in r_bad.stderr and "other.mp4" in r_bad.stderr
           and "mine.mp4" in r_bad.stderr, r_bad.stderr[-400:])
        ok("... and it says why, not just that it will not",
           "per clip thing" in r_bad.stderr, r_bad.stderr[-400:])
        ok("... and no numbers came back to be graded to",
           "coverage" not in r_bad.stdout, r_bad.stdout[:200])

        r_ok = run_cli(["stats", str(mine), "--time", "0.2",
                       "--matte", "m_mine", "--json"], env=env)
        ok("stats --matte <this clip's own matte>: measures",
           r_ok.returncode == 0, r_ok.stderr[-400:])
        ok("... and reports the coverage it measured through",
           '"coverage"' in r_ok.stdout, r_ok.stdout[:200])

        r_legacy = run_cli(["stats", str(mine), "--time", "0.2",
                           "--matte", "m_legacy", "--json"], env=env)
        ok("a matte filed under a readable key is measured, not judged",
           r_legacy.returncode == 0, r_legacy.stderr[-400:])

        r_legacy_other = run_cli(["stats", str(mine), "--time", "0.2",
                                 "--matte", "m_legacy_other", "--json"],
                                 env=env)
        ok("but one whose readable key belongs to another clip's NAME is "
           "still refused, the way the studio refuses it (finding 71)",
           r_legacy_other.returncode != 0, r_legacy_other.stdout[:200])
        ok("... in the same sentence as a key mismatch",
           "was tracked on" in r_legacy_other.stderr
           and "other.mp4" in r_legacy_other.stderr,
           r_legacy_other.stderr[-400:])
        ok("... and the studio asks the engine the same question rather than "
           "carrying its own copy of the rule",
           "CG.matte_clip_refusal(" in (CONTENT / "studio"
                                       / "server.py").read_text(),
           "studio/server.py")

        # --- stats --mask, the same matte reached through a stack ----------
        stack = json.dumps({"components": [
            {"id": "c1", "type": "matte", "op": "add", "enabled": True,
             "invert": False, "feather": 0.0, "matte": {"id": "m_other"}}]})
        r_stack = run_cli(["stats", str(mine), "--time", "0.2",
                          "--mask", stack, "--json"], env=env)
        ok("stats --mask through a stack that reaches it: refused as well",
           r_stack.returncode != 0, r_stack.stdout[:200])
        ok("... in the same words", "was tracked on" in r_stack.stderr,
           r_stack.stderr[-400:])

        # --- render --------------------------------------------------------
        def _preset_for(matte_id: str, path: Path) -> Path:
            layer = deepcopy(cg.LAYER_DEFAULTS)
            layer["mask"] = cg.deep_merge(layer["mask"], {"components": [
                {"id": "c1", "type": "matte", "op": "add", "enabled": True,
                 "invert": False, "feather": 0.0,
                 "matte": {"id": matte_id}}]})
            layer["correct"] = cg.deep_merge(layer["correct"],
                                             {"exposure": 0.5})
            path.write_text(json.dumps(
                cg.deep_merge(cg.DEFAULTS, {"layers": [layer]})))
            return path

        bad_preset = _preset_for("m_other", root / "preset_other.json")
        out_bad = root / "wrong.mov"
        r_render = run_cli(["render", str(mine), "--preset", str(bad_preset),
                           "-o", str(out_bad), "-t", "0.5",
                           "--allow-partial"], env=env)
        ok("render with another clip's matte: refused",
           r_render.returncode != 0, r_render.stderr[-400:])
        ok("... naming the layer and the component that reaches it",
           "layer 0 component 0" in r_render.stderr, r_render.stderr[-400:])
        ok("... in the same sentence the route and stats use",
           "was tracked on" in r_render.stderr, r_render.stderr[-400:])
        ok("... and --allow-partial did not buy it (that flag means an "
           "unfinished matte, never the wrong clip's)",
           "--allow-partial" not in r_render.stderr, r_render.stderr[-400:])
        ok("... and nothing was written", not out_bad.exists())

        # --- the config's OWN layers (round 2 finding 51) -----------------
        # No --matte and no --mask: the matte is in the preset the
        # measurement grades through. `_grade_frame_stats` builds the same
        # layer graph `render` builds, so this weighted the numbers with
        # another clip's subject exactly as render would have, and it was the
        # one path with no guard on it. `sweep` runs through the same
        # function, so it is pinned here too rather than assumed.
        r_cfg = run_cli(["stats", str(mine), "--time", "0.2",
                        "--preset", str(bad_preset), "--json"], env=env)
        ok("stats through a PRESET carrying another clip's matte: refused",
           r_cfg.returncode != 0, r_cfg.stdout[:200])
        ok("... in the same sentence, naming the layer and both clips",
           "was tracked on" in r_cfg.stderr
           and "layer 0 component 0" in r_cfg.stderr
           and "other.mp4" in r_cfg.stderr and "mine.mp4" in r_cfg.stderr,
           r_cfg.stderr[-400:])
        ok("... and no numbers came back to be graded to",
           "coverage" not in r_cfg.stdout and '"luma"' not in r_cfg.stdout,
           r_cfg.stdout[:200])

        r_sweep = run_cli(["sweep", str(mine), "--time", "0.2",
                          "--preset", str(bad_preset), "--param", "exposure",
                          "--values", "0,0.5", "--json"], env=env)
        ok("sweep through the same preset: refused as well, and before it "
           "rendered a single variant",
           r_sweep.returncode != 0, r_sweep.stdout[:200])
        ok("... in the same words", "was tracked on" in r_sweep.stderr,
           r_sweep.stderr[-400:])

        good_preset = _preset_for("m_mine", root / "preset_mine.json")
        r_cfg_ok = run_cli(["stats", str(mine), "--time", "0.2",
                           "--preset", str(good_preset), "--json"], env=env)
        ok("stats through a preset carrying THIS clip's matte: measures",
           r_cfg_ok.returncode == 0, r_cfg_ok.stderr[-400:])

        legacy_preset = _preset_for("m_legacy", root / "preset_legacy.json")
        r_cfg_legacy = run_cli(["stats", str(mine), "--time", "0.2",
                               "--preset", str(legacy_preset), "--json"],
                               env=env)
        ok("and a preset carrying a matte filed under a readable key is "
           "still measured, not judged: the guard refuses a mismatch it can "
           "prove, never every matte",
           r_cfg_legacy.returncode == 0, r_cfg_legacy.stderr[-400:])


        out_good = root / "right.mov"
        r_good = run_cli(["render", str(mine), "--preset", str(good_preset),
                         "-o", str(out_good), "-t", "0.5"], env=env)
        ok("render with this clip's own matte: renders",
           r_good.returncode == 0, r_good.stderr[-400:])
        ok("... and writes an output file",
           out_good.is_file() and out_good.stat().st_size > 0)


def main() -> int:
    print("starting the fake studio server...")
    srv, th, port = FAKE.start(0)
    base = f"http://127.0.0.1:{port}"
    print(f"fake studio server up at {base}")
    try:
        test_mask_cli(base)
        test_grade_client(base)
        test_mask_gaps(base)
        test_mask_resume_paths(base)
        test_cli_paths(base)
        test_frame_stats_weight()
        test_stats_matte()
        test_render_allow_partial()
        test_matte_id_is_never_a_path(base)
        test_matte_belongs_to_this_clip()
    finally:
        srv.shutdown()

    total = PASS + FAIL
    print(f"\n{PASS} passed, {FAIL} failed" + (f", {len(NOTES)} note(s)" if NOTES else ""))
    if total < EXPECTED_CHECKS:
        print(f"  FAIL  this run made {total} checks, fewer than the "
              f"{EXPECTED_CHECKS} this file declares: a whole block did not "
              f"run. A suite that quietly shrinks is a suite that quietly "
              f"stops testing things.")
        return 1
    if total > EXPECTED_CHECKS:
        print(f"  note  {total} checks, {total - EXPECTED_CHECKS} more than "
              f"the declared {EXPECTED_CHECKS}: raise EXPECTED_CHECKS.")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
