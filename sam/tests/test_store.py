#!/usr/bin/env python3
"""The matte store (C2) and its temporal smoothing, with no server and no model.

    uv run --project sam python sam/tests/test_store.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import FAILED, check, report, sam_path                        # noqa: E402

sam_path()
from store import MatteWriter, _Steady, frame_name, read_index            # noqa: E402


def header(frames: int) -> dict:
    return {"clip": "/tmp/clip.mov", "clip_key": "clip-1", "rotation": 0,
            "fps": 24.0, "frames": frames, "start_frame": 0, "end_frame": frames,
            "width": 8, "height": 4, "recipe": {"kind": "track"},
            "state": "queued", "model": "stub", "backend": "stub",
            "job_id": "j_1", "object_id": "t0_0", "label": "person",
            "kind": "text"}


def ramp(value: float) -> np.ndarray:
    return np.full((4, 8), value, dtype=np.float32)


def box(y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
    """A rectangle of ones in a 4x8 frame, so an area and an IoU can be
    worked out by hand and written into the check."""
    a = np.zeros((4, 8), dtype=np.float32)
    a[y0:y1, x0:x1] = 1.0
    return a


def half(left: bool) -> np.ndarray:
    """Half the frame covered, on one side or the other. Two of these in a
    row overlap in nothing, which is the shape change the per frame IoU
    (checkpoint gap 18) exists to notice."""
    a = np.zeros((4, 8), dtype=np.float32)
    if left:
        a[:, :4] = 1.0
    else:
        a[:, 4:] = 1.0
    return a


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="sam-store-"))

    print("frame naming and index shape")
    check("frames are named by absolute source index",
          frame_name(48) == "000048.png", frame_name(48))
    writer = MatteWriter(root, "m_test", header(10), steady=1)
    index = read_index(writer.dir)
    check("index.json exists at creation with state queued",
          index["state"] == "queued" and index["done_frames"] == 0)
    check("areas and scores are one slot per frame of the clip",
          len(index["areas"]) == 10 and all(a is None for a in index["areas"]))
    check("C2's required keys are all present",
          all(key in index for key in ("matte_id", "clip", "clip_key", "rotation",
                                       "fps", "frames", "width", "height", "recipe",
                                       "state", "done_frames", "areas", "scores",
                                       "created", "model", "backend")),
          ", ".join(sorted(index)))

    print("\nwriting frames")
    writer.set_state("running")
    for i in range(10):
        writer.push(i, ramp(0.5), 0.9)
    writer.finish("done")
    index = read_index(writer.dir)
    files = sorted(p.name for p in writer.dir.glob("*.png"))
    check("one png per frame", len(files) == 10, str(files[:3]))
    check("first file is the first source frame", files[0] == "000000.png")
    check("state done, done_frames counted", index["state"] == "done"
          and index["done_frames"] == 10)
    check("area is the fraction of the frame covered",
          abs(index["areas"][0] - 0.5) < 0.01, str(index["areas"][0]))
    check("scores are recorded per frame", index["scores"][0] == 0.9)

    print("\na matte that starts part way through the clip")
    offset = MatteWriter(root, "m_offset", dict(header(120), start_frame=48,
                                                end_frame=52), steady=1)
    for i in range(48, 52):
        offset.push(i, ramp(1.0), 1.0)
    offset.finish("partial", "cancelled")
    index = read_index(offset.dir)
    names = sorted(p.name for p in offset.dir.glob("*.png"))
    check("file names carry the absolute frame index", names[0] == "000048.png", names[0])
    check("areas are indexed by absolute frame index, null elsewhere",
          index["areas"][0] is None and index["areas"][48] is not None
          and index["areas"][52] is None)
    check("a partial matte says so, with the reason",
          index["state"] == "partial" and index["error"] == "cancelled")

    print("\ntemporal smoothing (steady)")
    steady = _Steady(1)
    out = steady.push(0, ramp(1.0), 1.0)
    check("steady 1 writes straight through", len(out) == 1 and out[0][0] == 0)

    steady = _Steady(3)
    emitted = []
    values = [0.0, 1.0, 0.0, 1.0, 0.0]
    for i, value in enumerate(values):
        emitted += steady.push(i, ramp(value), 1.0)
    emitted += steady.drain()
    check("steady 3 emits every frame exactly once, in order",
          [e[0] for e in emitted] == list(range(5)), str([e[0] for e in emitted]))
    middles = [float(e[1].mean()) for e in emitted[1:4]]
    # A 0, 1, 0, 1, 0 flicker becomes 1/3, 2/3, 1/3: the swing shrinks from
    # 1.0 to 1/3, which is what "steady" is for.
    check("steady 3 averages the neighbours, so an alternating signal flattens",
          all(0.3 < v < 0.7 for v in middles)
          and max(middles) - min(middles) < 0.4,
          str([round(v, 3) for v in middles]))
    check("the smoothing window is centred, not trailing: frame 1 already "
          "carries frame 2's value",
          abs(float(emitted[1][1].mean()) - 0.3333) < 0.01)

    steady = _Steady(5)
    emitted = []
    for i in range(12):
        emitted += steady.push(i, ramp(float(i)), 1.0)
        check_buffer = len(steady.buffer) <= 5
        if not check_buffer:
            break
    emitted += steady.drain()
    check("the smoothing buffer never grows past n frames", check_buffer)
    check("steady 5 emits every frame once", [e[0] for e in emitted] == list(range(12)))
    check("an interior frame is the mean of its five neighbours",
          abs(float(emitted[6][1].mean()) - 6.0) < 1e-4, str(float(emitted[6][1].mean())))

    print("\nper frame IoU with the previous written frame (gap 18)")
    iou_writer = MatteWriter(root, "m_iou", header(4), steady=1)
    iou_writer.push(0, half(True), 0.9)      # left half
    iou_writer.push(1, half(True), 0.9)      # the same half: no change
    iou_writer.push(2, half(False), 0.9)     # the other half: nothing shared
    iou_writer.push(3, half(True), 0.9)      # back again
    iou_writer.finish("done")
    index = read_index(iou_writer.dir)
    check("ious is one slot per frame, indexed like areas and scores",
          len(index["ious"]) == 4, str(index["ious"]))
    check("the first written frame has nothing to compare against",
          index["ious"][0] is None, str(index["ious"][0]))
    check("an unchanged mask reads as an IoU of 1",
          abs(index["ious"][1] - 1.0) < 1e-6, str(index["ious"][1]))
    check("a mask that jumps to a disjoint region reads as an IoU of 0",
          abs(index["ious"][2] - 0.0) < 1e-6, str(index["ious"][2]))
    check("and jumping back is just as suspect",
          abs(index["ious"][3] - 0.0) < 1e-6, str(index["ious"][3]))
    check("the store writes ious itself, so nothing has to re-read the pngs "
          "to judge a matte",
          "ious" in index)

    print("\nwhat a drift check reads off a matte that lost its subject "
          "(finding 15, producer side)")
    # The three rules `grade/mattes.py::quality()` applies (a zero area frame, a
    # big area jump, a low IoU) are only as real as the arrays this store
    # writes. Round 1 finding 15 found every assertion about `quality()` running
    # against a fake index.json with NO areas and NO ious, which forces
    # suspect_count to 0 whatever the rules do. So this proves the producer:
    # after a track that latches onto something else, the numbers on disk are
    # the ones those rules need, with the values spelled out here so the engine
    # side can be unit tested against exactly these.
    drift = MatteWriter(root, "m_drift", header(6), steady=1)
    drift.set_state("running")
    drift.push(0, box(1, 3, 1, 3), 0.90)      # the subject: 4 of 32 pixels
    drift.push(1, box(1, 3, 1, 3), 0.88)      # holding
    drift.push(2, np.zeros((4, 8), dtype=np.float32), 0.10)   # lost outright
    drift.push(3, box(0, 4, 0, 8), 0.55)      # latched onto the background
    drift.push(4, box(0, 4, 0, 8), 0.57)      # holding the wrong thing
    drift.push(5, box(1, 3, 1, 3), 0.86)      # back on the subject
    drift.finish("done")
    d = read_index(drift.dir)
    areas, ious = d["areas"], d["ious"]
    check("a frame where the object was lost is an area of exactly 0, which is "
          "the zero_area rule's input",
          areas[2] == 0.0, str(areas[2]))
    check("the frame that latched onto the background is an 8x area jump off "
          "the last non zero frame, which is the area_jump rule's input",
          areas[1] > 0 and areas[3] / areas[1] == 8.0,
          f"{areas[1]} -> {areas[3]}")
    check("the jump reads as an IoU of 0 against the frame before it, because "
          "the frame before it was the empty one",
          abs(ious[3] - 0.0) < 1e-6, str(ious[3]))
    check("and the recovery is the low_iou rule's own case, with no empty "
          "frame in between: 4 pixels inside 32 is an IoU of 0.125 while the "
          "score looks healthy again at 0.86",
          abs(ious[5] - 0.125) < 1e-6 and areas[5] == areas[0]
          and d["scores"][5] == 0.86,
          f"area {areas[5]} iou {ious[5]} score {d['scores'][5]}")
    check("every frame of the track has a number in all three arrays, so a "
          "drift check reads no Nones inside the tracked span",
          all(v is not None for v in areas)
          and all(v is not None for v in d["scores"])
          and all(v is not None for v in ious[1:]),
          f"{areas} {ious}")

    print("\nre-opening a matte id is a resume, not a restart (gap 12)")
    first = MatteWriter(root, "m_resume", header(10), steady=1)
    first.set_state("running")
    for i in range(4):
        first.push(i, ramp(0.5), 0.9)
    first.finish("partial", "cancelled")
    stopped = read_index(first.dir)
    check("the cancelled attempt wrote four frames",
          stopped["done_frames"] == 4 and stopped["areas"][3] is not None
          and stopped["areas"][4] is None, str(stopped["done_frames"]))

    second = MatteWriter(root, "m_resume", header(10), steady=1)
    check("re-opening it does not blank the frames already written",
          second.done == 4, str(second.done))
    resumed_open = read_index(second.dir)
    check("and does not blank the arrays either",
          resumed_open["areas"][0] is not None
          and resumed_open["areas"][3] is not None,
          str(resumed_open["areas"][:5]))
    second.set_state("running")
    for i in range(4, 10):
        second.push(i, ramp(0.75), 0.8)
    second.finish("done")
    index = read_index(second.dir)
    files = sorted(p.name for p in second.dir.glob("*.png"))
    check("the tail lands in the same directory as the head",
          len(files) == 10 and files[0] == "000000.png"
          and files[-1] == "000009.png", str(len(files)))
    check("done_frames counts what is on disk, head and tail together",
          index["done_frames"] == 10, str(index["done_frames"]))
    check("the earlier attempt's own areas survive the resume",
          abs(index["areas"][0] - 0.5) < 0.01, str(index["areas"][0]))
    check("and the tail's areas are the new run's",
          abs(index["areas"][9] - 0.75) < 0.01, str(index["areas"][9]))
    check("the resumed matte reads done, not partial",
          index["state"] == "done" and index["error"] is None)

    print("\na resumed range that overlaps is not counted twice")
    third = MatteWriter(root, "m_resume", header(10), steady=1)
    for i in range(2, 10):                     # re-writes 2..9 over the top
        third.push(i, ramp(0.6), 0.7)
    third.finish("done")
    index = read_index(third.dir)
    check("done_frames is the number of frames on disk, not the number of "
          "writes",
          index["done_frames"] == 10, str(index["done_frames"]))
    check("an overwritten frame takes the new run's value",
          abs(index["areas"][2] - 0.6) < 0.01, str(index["areas"][2]))

    print("\na different length is a different track, not a resume")
    other = MatteWriter(root, "m_resume", header(20), steady=1)
    check("re-opening the same id with another frame count starts clean",
          other.done == 0, str(other.done))
    fresh = read_index(other.dir)
    check("and its arrays are the new length, all empty",
          len(fresh["areas"]) == 20 and all(a is None for a in fresh["areas"]))

    print("\na matte that starts part way through keeps the earlier start")
    late = MatteWriter(root, "m_startkeep",
                       dict(header(120), start_frame=48, end_frame=52), steady=1)
    late.push(48, ramp(1.0), 1.0)
    late.finish("partial", "cancelled")
    resumed_late = MatteWriter(root, "m_startkeep",
                               dict(header(120), start_frame=49, end_frame=52),
                               steady=1)
    check("start_frame keeps the earliest of the two ranges, so the span a "
          "caller already read does not move",
          resumed_late.index["start_frame"] == 48,
          str(resumed_late.index["start_frame"]))

    print("\nan interior re-track keeps the matte's declared span (gap 24)")
    # The shape a narrow `mask track --force` makes: the studio clears the
    # frames of one window and re-queues only that window, naming the same
    # matte id. The header for that run declares the WINDOW, not the matte,
    # so a store that read the shorter length as "a different track" blanked
    # the arrays and reported 0 done with 7 files sitting in the folder, and
    # the matte's own span shrank to the repair window.
    whole = MatteWriter(root, "m_interior", header(10), steady=1)
    for i in range(10):
        whole.push(i, ramp(0.5), 0.9)
    whole.finish("done")
    for i in (4, 5, 6):
        (whole.dir / frame_name(i)).unlink()
    inner = MatteWriter(root, "m_interior",
                        dict(header(7), start_frame=4, end_frame=7), steady=1)
    check("the matte still declares the ten frames it was tracked over, not "
          "the three the repair asked for",
          inner.index["frames"] == 10 and inner.index["end_frame"] == 10
          and inner.index["start_frame"] == 0,
          f"{inner.index['frames']} frames, "
          f"{inner.index['start_frame']}..{inner.index['end_frame']}")
    check("its arrays are still ten long, so every index a caller already "
          "holds still means the same frame",
          len(inner.index["areas"]) == 10 and len(inner.index["ious"]) == 10,
          f"{len(inner.index['areas'])} areas")
    check("the frames outside the repair window are counted, not forgotten",
          inner.done == 7, str(inner.done))
    check("and their numbers survive",
          inner.index["areas"][0] is not None
          and inner.index["areas"][9] is not None,
          str(inner.index["areas"]))
    inner.set_state("running")
    for i in (4, 5, 6):
        inner.push(i, ramp(0.8), 0.7)
    inner.finish("done")
    repaired = read_index(inner.dir)
    check("after the repair the matte is whole again and says so",
          repaired["done_frames"] == 10 and repaired["state"] == "done"
          and repaired["frames"] == 10,
          f"{repaired['state']}, {repaired['done_frames']}/{repaired['frames']}")
    def _near(value, want: float) -> bool:
        # A red run of this block reads None here, so the comparison has to
        # answer False rather than raise: a failed check names itself, a
        # traceback takes the rest of the suite with it (round 1 finding 39).
        return isinstance(value, (int, float)) and abs(value - want) < 0.01

    check("the repaired frames carry the new run's numbers and the rest carry "
          "the first run's",
          _near(repaired["areas"][5], 0.8) and _near(repaired["areas"][0], 0.5),
          f"{repaired['areas'][5]} inside, {repaired['areas'][0]} outside")
    files = sorted(p.name for p in inner.dir.glob("*.png"))
    check("and every frame file is back on disk, the kept ones untouched",
          len(files) == 10 and files[0] == "000000.png"
          and files[-1] == "000009.png", str(len(files)))

    print("\na window that only PARTLY overlaps the matte is a different "
          "track (gap 24, round 4 finding 89)")
    # The third shape of `mask track --force`, and the reason the studio refuses
    # it instead of clearing anything: the two above are an equal window (a
    # resume) and an interior one (a repair), and BOTH carry the matte forward.
    # A window that hangs off one end carries nothing forward, by this store's
    # own blessed rule ("a different length is a different track"), so the
    # frames the studio kept on disk for it end up either past the end of the
    # new arrays or inside them with no numbers. Neither is wrong here; both
    # are wrong to ask for, which is what studio/server.py's force branch now
    # says in words (studio/tests/py/test_mask_routes.py's two refusal tests).
    # Reproduction one, with the round 4 verdict's own frame numbers: a matte
    # tracked over frames 173 to 197, forced at 144 to 192. The force clears
    # the overlap (173 to 191), keeps 192 to 196, and re-queues 144 to 192.
    tracked = MatteWriter(root, "m_lowend",
                          dict(header(197), start_frame=173, end_frame=197),
                          steady=1)
    tracked.set_state("running")
    for i in range(173, 197):
        tracked.push(i, ramp(0.5), 0.9)
    tracked.finish("done")
    check("a matte tracked over frames 173 to 197 declares that window and "
          "has 24 frames of numbers",
          read_index(tracked.dir)["start_frame"] == 173
          and read_index(tracked.dir)["end_frame"] == 197
          and read_index(tracked.dir)["done_frames"] == 24,
          json.dumps({k: read_index(tracked.dir)[k]
                      for k in ("start_frame", "end_frame", "done_frames")}))
    for i in range(173, 192):
        (tracked.dir / frame_name(i)).unlink()
    hangs_low = MatteWriter(root, "m_lowend",
                            dict(header(192), start_frame=144, end_frame=192),
                            steady=1)
    check("a window that starts before the matte carries nothing forward: the "
          "declared window moves to the request",
          hangs_low.index["frames"] == 192
          and hangs_low.index["start_frame"] == 144
          and hangs_low.index["end_frame"] == 192,
          f"{hangs_low.index['frames']} frames, "
          f"{hangs_low.index['start_frame']}..{hangs_low.index['end_frame']}")
    check("its arrays are the request's length, so the five frames the force "
          "kept (192 to 196) have no slot in them at all, and the numbers the "
          "first run measured are gone",
          len(hangs_low.index["areas"]) == 192
          and all(a is None for a in hangs_low.index["areas"]),
          f"{len(hangs_low.index['areas'])} areas, "
          f"{sum(a is not None for a in hangs_low.index['areas'])} of them set")
    hangs_low.set_state("running")
    for i in range(144, 192):
        hangs_low.push(i, ramp(0.8), 0.7)
    hangs_low.finish("done")
    low_final = read_index(hangs_low.dir)
    low_files = sorted(p.name for p in hangs_low.dir.glob("*.png"))
    check("so after the re-track the matte reads done over a window it cannot "
          "describe: 53 files on disk, 192 declared frames, 48 done",
          low_final["state"] == "done" and len(low_files) == 53
          and low_final["frames"] == 192 and low_final["done_frames"] == 48,
          f"{low_final['state']}, {len(low_files)} files, "
          f"{low_final['done_frames']}/{low_final['frames']}")
    check("and the five kept frames are orphaned: their PNGs sit past the end "
          "of every array, so no reader can reach them",
          len(low_final["areas"]) == 192
          and low_files[-1] == "000196.png"
          and [n for n in low_files if int(n[:6]) >= 192]
          == ["%06d.png" % i for i in range(192, 197)],
          f"{len(low_final['areas'])} areas, last file {low_files[-1]}")

    # Reproduction two, the mirror image, which orphans nothing and is quieter
    # for it: a matte over frames 0 to 288 forced at 173 to 400. Every kept
    # frame lands inside the new arrays, with no number in them.
    over = MatteWriter(root, "m_highend", header(288), steady=1)
    over.set_state("running")
    for i in range(288):
        over.push(i, ramp(0.5), 0.9)
    over.finish("done")
    for i in range(173, 288):
        (over.dir / frame_name(i)).unlink()
    hangs_high = MatteWriter(root, "m_highend",
                             dict(header(400), start_frame=173, end_frame=400),
                             steady=1)
    hangs_high.set_state("running")
    for i in range(173, 400):
        hangs_high.push(i, ramp(0.8), 0.7)
    hangs_high.finish("done")
    high_final = read_index(hangs_high.dir)
    high_files = sorted(p.name for p in hangs_high.dir.glob("*.png"))
    check("a window that runs past the matte reads done with every frame on "
          "disk: 400 files against 400 declared frames",
          high_final["state"] == "done" and len(high_files) == 400
          and high_final["frames"] == 400,
          f"{high_final['state']}, {len(high_files)} files, "
          f"{high_final['frames']} declared")
    check("and the 173 frames it kept have a PNG each and no area, score or "
          "IoU: nothing measured them, and done_frames counts only this run",
          all(high_final["areas"][i] is None for i in range(173))
          and all(high_final["scores"][i] is None for i in range(173))
          and all(high_final["ious"][i] is None for i in range(173))
          and high_final["done_frames"] == 227,
          f"areas[0:3] {high_final['areas'][:3]}, "
          f"{high_final['done_frames']}/{high_final['frames']}")
    check("so the matte's own declared start moved past frames it still has "
          "on disk: it says it starts at 173 with 173 files before that",
          high_final["start_frame"] == 173
          and len([n for n in high_files if int(n[:6]) < 173]) == 173,
          f"start_frame {high_final['start_frame']}, "
          f"{len([n for n in high_files if int(n[:6]) < 173])} files before it")

    print("\nindex.json is never seen half written")
    writer = MatteWriter(root, "m_atomic", header(4), steady=1)
    # The outcome is checked, not merely reached: this used to be a bare
    # check("...", True) after the loop, so a half written index.json would
    # have crashed the suite with a traceback instead of failing one named
    # check, and a loop that parsed nothing would have passed (round 1
    # finding 39).
    parsed = []
    trouble = ""
    for i in range(4):
        writer.push(i, ramp(0.25), 0.5)
        try:
            parsed.append(json.loads((writer.dir / "index.json").read_text()))
        except (OSError, ValueError) as exc:
            trouble = f"frame {i}: {type(exc).__name__}: {exc}"
            break
    writer.finish("done")
    final = read_index(writer.dir)
    check("index.json parses as whole JSON after every single frame, with the "
          "keys a reader needs still in it",
          not trouble and len(parsed) == 4
          and all({"state", "done_frames", "areas"} <= set(doc)
                  for doc in parsed),
          trouble or f"{len(parsed)} reads")
    check("the rewrite is throttled to at most once a second while a job "
          "runs, so those four reads cost one write, and finish() always "
          "writes: done_frames is 4 only at the end",
          [doc["done_frames"] for doc in parsed] == [0, 0, 0, 0]
          and final["done_frames"] == 4,
          f"{[doc['done_frames'] for doc in parsed]} then {final['done_frames']}")
    check("no temp files are left behind",
          not list(writer.dir.glob("*.tmp*")), str(list(writer.dir.glob("*.tmp*"))))

    return report("test_store")


if __name__ == "__main__":
    raise SystemExit(main())

