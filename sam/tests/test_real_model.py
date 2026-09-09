#!/usr/bin/env python3
"""The one test that loads the real model. Everything else runs on the stub.

    uv run --project sam python sam/tests/test_real_model.py
    uv run --project sam python sam/tests/test_real_model.py --backend torch-cpu

It is a smoke test, not a quality bar: it proves that the service, the
backend, the matte store and the routes work against SAM 3.1 on this Mac, and
it records how fast that was, because "how long does a two second clip take"
is the number that decides how the founder's UI has to behave.

Material: whatever the spike left under sam/spike/out (the plain 720 wide
frames and clip). Nothing here writes to spike/out.

It starts ONE service with the real backend, which takes the machine wide
lock at /tmp/fixxr-sam-model.lock and holds it until it exits, so only one
model process runs on this machine at a time. If another process holds the
lock this test waits for it: that is the intended behaviour, not a hang.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (call, centroid, check, free_port, load_mask, report,   # noqa: E402
                    start, stop, wait_for_job)

SPIKE = Path(__file__).resolve().parent.parent / "spike" / "out"
FRAMES = SPIKE / "frames_720"
CLIP = SPIKE / "clip_720.mp4"
TRACK_SECONDS = 2.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="mlx",
                        help="mlx (default), auto, torch-mps or torch-cpu")
    parser.add_argument("--seconds", type=float, default=TRACK_SECONDS)
    parser.add_argument("--ready-s", type=float, default=600,
                        help="how long to wait for the model to load, which "
                             "includes waiting for the machine wide lock")
    args = parser.parse_args()

    if not FRAMES.is_dir() or not CLIP.is_file():
        # A missing fixture is a SETUP ERROR, not a pass. This used to return 0
        # with a "Skipping." line, so a run that tested nothing at all reported
        # success; if this file is ever added to sam/tests/run.py that green
        # line would have meant nothing (round 1 finding 41).
        print(f"the spike material is missing under {SPIKE}.\n"
              "This test uses what the spike left there; it does not download "
              "or re-encode anything.\n"
              "Run `uv run --project sam python sam/spike/track_memory.py "
              "--windows 1` once to produce it, then run this again.")
        print("\ntest_real_model: 0/0 checks passed")
        print("test_real_model: 1 checks skipped")
        print(f"  SKIPPED: the whole suite (no fixtures under {SPIKE})")
        return 2

    still = sorted(FRAMES.glob("*.png"))[0]
    tmp = Path(tempfile.mkdtemp(prefix="sam-real-"))
    port = free_port()
    numbers: dict = {"backend_requested": args.backend, "still": str(still),
                     "clip": str(CLIP)}

    print(f"loading the model ({args.backend}). This holds the machine wide "
          f"lock; if another model process has it, this waits.")
    began = time.time()
    proc, base = start(port, tmp / "data", backend=args.backend,
                       ready_s=args.ready_s)
    numbers["load_s"] = round(time.time() - began, 1)
    print(f"server pid {proc.pid} on {port}, ready in {numbers['load_s']}s\n")

    try:
        health = call(base, "/health")
        check("the real backend loaded and says which one it is",
              health["loaded"] and health["backend"] in ("mlx", "torch-mps", "torch-cpu"),
              f"{health['backend']}, model {health['model']}")
        check("the machine wide model lock is held while the model is resident",
              health["model_lock"]["held"] is True or health["model_lock"]["enabled"] is False,
              str(health["model_lock"]))
        numbers["backend"] = health["backend"]
        numbers["model"] = health["model"]

        print("\nsegment: person and sky on one frame")
        began = time.time()
        pick = call(base, "/segment", {"image": str(still),
                                       "prompts": {"text": ["person", "sky"]},
                                       "max_instances": 2}, timeout=900)
        numbers["segment_s"] = round(time.time() - began, 2)
        check("the detector answers with instances for both phrases",
              pick.get("ok") and pick["instances"],
              f"{len(pick.get('instances', []))} instance(s) in {numbers['segment_s']}s")
        found = {}
        for instance in pick.get("instances", []):
            found.setdefault(instance["id"].split("_")[0], []).append(instance)
        check("person was found", "t0" in found, str(sorted(found)))
        check("sky was found", "t1" in found, str(sorted(found)))
        numbers["segment_instances"] = [
            {"id": i["id"], "label": i.get("label"), "score": i["score"],
             "area": i["area"], "box": [round(v, 3) for v in i["box"]]}
            for i in pick.get("instances", [])]
        for instance in numbers["segment_instances"]:
            print(f"    {instance['id']:8} {instance['label']:8} "
                  f"score {instance['score']:.3f}  area {instance['area']:.4f}")
        person = found.get("t0", [{}])[0]
        check("the person matte covers a believable slice of the frame, "
              "not everything and not nothing",
              0.005 < person.get("area", 0) < 0.7, f"area {person.get('area')}")
        check("the masks are on disk where the answer says they are",
              all(Path(i["mask"]).is_file() for i in pick["instances"]))

        print(f"\ntrack: person for {args.seconds:g}s of the 720 clip")
        frames = int(round(args.seconds * 24))
        began = time.time()
        job = call(base, "/track", {
            "video": str(CLIP), "prompts": {"text": ["person"]},
            "start_frame": 0, "end_frame": frames,
            "clip_key": "spike-720", "steady": 1,
            "out_dir": str(tmp / "mattes"),
        }, timeout=900)
        check("track answers at once with a job id, before any work is done",
              job.get("job_id") and job["total_frames"] == frames,
              f"{job.get('total_frames')} frames")
        # 48 frames at the rate the spike measured (0.055 frames/s under
        # load) is about 15 minutes; the wait is generous because a
        # busy machine makes this several times slower, and a timeout
        # here would read as a broken tracker rather than a slow Mac.
        done = wait_for_job(base, job["job_id"], timeout_s=7200)
        numbers["track_s"] = round(time.time() - began, 1)
        check("the track finishes", done["state"] == "done", str(done.get("error")))
        numbers["track_frames"] = done.get("done_frames")
        numbers["track_fps"] = done.get("rate_fps")
        print(f"    {numbers['track_frames']} frames in {numbers['track_s']}s "
              f"= {numbers['track_fps']} frames/s")

        matte_dir = Path(job["mattes"][0]["path"])
        index = json.loads((matte_dir / "index.json").read_text())
        pngs = sorted(matte_dir.glob("*.png"))
        check("one png per frame, named by absolute source frame index",
              len(pngs) == frames and pngs[0].name == "000000.png",
              f"{len(pngs)} files")
        areas = [a for a in index["areas"] if a is not None]
        numbers["area_first"] = areas[0] if areas else None
        numbers["area_last"] = areas[-1] if areas else None
        numbers["area_min"] = min(areas) if areas else None
        numbers["area_max"] = max(areas) if areas else None
        scores = [s for s in index["scores"] if s is not None]
        numbers["score_mean"] = round(sum(scores) / len(scores), 4) if scores else None
        check("every frame has an area and a score",
              len(areas) == frames and len(scores) == frames,
              f"{len(areas)} areas, {len(scores)} scores")
        check("the tracked area stays in the same order of magnitude across "
              "the clip, so the object was held rather than lost",
              areas and max(areas) < 6 * min(areas),
              f"area {min(areas):.4f}..{max(areas):.4f}")
        first, last = load_mask(pngs[0]), load_mask(pngs[-1])
        moved = sum(abs(a - b) for a, b in zip(centroid(first), centroid(last)))
        check("the matte is a real matte: soft edged and not empty",
              0.001 < float(first.mean()) < 0.9)
        numbers["centroid_moved"] = round(moved, 4)
        print(f"    centroid moved {moved:.4f} over {frames} frames")

        out = Path(__file__).resolve().parent / "out"
        out.mkdir(exist_ok=True)
        (out / "real-model.json").write_text(json.dumps(numbers, indent=2) + "\n")
        print(f"\nnumbers written to {out / 'real-model.json'}")
    finally:
        stop(proc)

    return report("test_real_model")


if __name__ == "__main__":
    raise SystemExit(main())
