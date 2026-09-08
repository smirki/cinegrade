#!/usr/bin/env python3
"""Contract C3 end to end: the real service, the real routes, over real HTTP,
with the stub backend so no weights and no model lock are involved.

    uv run --project sam python sam/tests/service_e2e.py

Deliberately not named test_*.py: it starts servers and takes seconds. It runs
two of them, both on random high ports with their own temporary data
directories, both stopped by the process id captured at spawn. One is instant
(the default stub) for the shape of every route; the second one is slowed to a
few frames a second so progress, the queue and cancel can be watched happening
rather than inferred.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (FAILED, call, centroid, check, free_port, load_mask,     # noqa: E402
                    make_frames, make_video, report, start, stop, wait_for_job)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="sam-e2e-"))
    frames_dir = make_frames(tmp / "clip_frames", count=24)
    video = make_video(tmp / "clip.mp4", seconds=1)
    port = free_port()
    proc, base = start(port, tmp / "data")
    print(f"data {tmp}\nport {port}\nserver pid {proc.pid}\n")

    try:
        print("health")
        health = call(base, "/health")
        check("health names the backend that actually loaded",
              health["backend"] == "stub" and health["loaded"] is True,
              str(health.get("backend")))
        check("health reports the queue", isinstance(health["queue"], dict)
              and health["queue"]["queued"] == 0 and health["queue"]["running"] is None)
        check("health names the model", health["model"] == "stub-ellipse")
        check("health says whether the model lock is held",
              health["model_lock"]["held"] is False)

        print("\nsegment: one frame, right now")
        pick = call(base, "/segment", {
            "image": str(frames_dir / "0000.png"),
            "prompts": {"text": ["person", "sky"],
                        "points": [{"x": 0.5, "y": 0.5, "label": 1}],
                        "boxes": [[0.1, 0.1, 0.4, 0.9]],
                        "exemplars": [{"anything": 1}]},
            "max_instances": 2,
        })
        check("segment answers with instances", pick.get("ok") and pick["instances"],
              f"{len(pick.get('instances', []))} instance(s)")
        check("every instance has an id, score, box, area and a mask on disk",
              all(set(("id", "score", "box", "area", "mask")) <= set(i)
                  and Path(i["mask"]).is_file() for i in pick["instances"]))
        check("boxes and areas are fractions of the image",
              all(0.0 <= v <= 1.0 for i in pick["instances"] for v in i["box"])
              and all(0.0 <= i["area"] <= 1.0 for i in pick["instances"]))
        check("a semantic union mask is written too",
              pick["semantic"] and Path(pick["semantic"]).is_file())
        check("the call is timed", pick["elapsed_s"] >= 0)
        check("a pick id comes back, so a track can be seeded from it",
              pick["pick_id"].startswith("p_"))
        check("exemplars are accepted and the answer says they did nothing",
              any("exemplar" in w for w in pick["warnings"]), str(pick["warnings"]))
        ids = [i["id"] for i in pick["instances"]]
        check("the ids are the planner's slot ids",
              "b0" in ids and "p0" in ids and "t0_0" in ids, str(ids))

        print("\nsegment: what it refuses")
        bad = call(base, "/segment", {"prompts": {"text": ["person"]}})
        check("no image is a 400 with a sentence", bad["status"] == 400
              and "image" in bad["error"], bad["error"])
        bad = call(base, "/segment", {"image": str(frames_dir / "0000.png"),
                                      "prompts": {}})
        check("no prompt at all is a 400", bad["status"] == 400, bad["error"])
        bad = call(base, "/segment", {"image": str(frames_dir / "0000.png"),
                                      "prompts": {"points": [{"x": 640, "y": 400}]}})
        check("pixel coordinates are refused, not silently clamped",
              bad["status"] == 400 and "fractions" in bad["error"], bad["error"])
        bad = call(base, "/nope")
        check("an unknown route is a 404 in json", bad["status"] == 404)

        print("\ntrack: a clip, as a job")
        job = call(base, "/track", {
            "video": str(frames_dir), "fps": 24,
            "prompts": {"text": ["person"]},
            "clip": "/footage/C015.mov", "clip_key": "C015-rot0", "rotation": 0,
            "steady": 3, "out_dir": str(tmp / "data" / "mattes" / "C015-rot0"),
        })
        check("track answers at once with a job id and matte ids",
              job["job_id"].startswith("j_") and len(job["matte_ids"]) == 1,
              str(job["matte_ids"]))
        check("the matte exists in state queued before any work is done",
              job["mattes"][0]["state"] in ("queued", "running", "done"))
        check("the job knows how many frames it will do", job["total_frames"] == 24)
        matte_dir = Path(job["mattes"][0]["path"])
        check("the matte's directory is under the out_dir the caller gave",
              str(matte_dir).startswith(str(tmp / "data" / "mattes" / "C015-rot0")))

        done = wait_for_job(base, job["job_id"], 60)
        check("the job finishes", done["state"] == "done", str(done.get("error")))
        check("it counted every frame",
              done["done_frames"] == done["total_frames"] == 24)
        check("it reports a measured rate as well as the clip's fps",
              done["rate_fps"] > 0 and done["fps"] == 24.0,
              f"{done['rate_fps']} frames/s at {done['fps']} fps")

        index = json.loads((matte_dir / "index.json").read_text())
        pngs = sorted(matte_dir.glob("*.png"))
        check("one png per frame on disk", len(pngs) == 24, f"{len(pngs)} files")
        check("named by absolute frame index", pngs[0].name == "000000.png")
        check("the matte's state is done and its frames are counted",
              index["state"] == "done" and index["done_frames"] == 24)
        check("the clip identity the caller gave is stored verbatim",
              index["clip_key"] == "C015-rot0" and index["clip"] == "/footage/C015.mov"
              and index["rotation"] == 0)
        check("the recipe that made it is stored, so it can be remade",
              isinstance(index["recipe"], dict) and index["recipe"]["prompts"]["text"] == ["person"])
        check("steady is recorded", index["steady"] == 3)
        check("areas and scores are one per frame with no gaps",
              len(index["areas"]) == 24 and all(a is not None for a in index["areas"]))
        check("the model and backend that made it are named",
              index["backend"] == "stub" and index["model"] == "stub-ellipse")

        first = load_mask(pngs[0])
        last = load_mask(pngs[-1])
        moved = sum(abs(a - b) for a, b in zip(centroid(first), centroid(last)))
        check("the tracked matte moves through the clip, which is what a "
              "browser spec will assert in the viewer", moved > 0.02,
              f"centroid moved {moved:.3f}")

        print("\ntrack: matte_ids is a resume, not a new matte (gap 12)")
        existing_id = job["matte_ids"][0]
        out_dir = tmp / "data" / "mattes" / "C015-rot0"
        resumed = call(base, "/track", {
            "video": str(frames_dir), "fps": 24,
            "prompts": {"text": ["person"]},
            "clip": "/footage/C015.mov", "clip_key": "C015-rot0", "rotation": 0,
            "steady": 3, "out_dir": str(out_dir),
            # Only the tail. Without the override below the derived id would
            # differ (it hashes the frame range too), so these frames would
            # land in a second directory and the first twelve would be
            # orphaned: the whole reason this parameter exists.
            "start_frame": 12, "end_frame": 24,
            "matte_ids": {index["object_id"]: existing_id},
        })
        check("a named matte id is used instead of the derived one",
              resumed["matte_ids"] == [existing_id],
              str(resumed["matte_ids"]))
        check("so the tail is written into the same directory",
              Path(resumed["mattes"][0]["path"]) == matte_dir,
              resumed["mattes"][0]["path"])
        check("and the matte goes back to queued or running, not left dead",
              resumed["mattes"][0]["state"] in ("queued", "running"),
              resumed["mattes"][0]["state"])
        resumed_done = wait_for_job(base, resumed["job_id"], 60)
        check("the resumed job finishes", resumed_done["state"] == "done",
              str(resumed_done.get("error")))
        check("it only did the frames it was asked for",
              resumed_done["total_frames"] == 12,
              str(resumed_done["total_frames"]))
        resumed_index = json.loads((matte_dir / "index.json").read_text())
        check("every frame is still on disk, head and tail together",
              len(sorted(matte_dir.glob("*.png"))) == 24)
        check("done_frames counts what is on disk, not just this run's 12",
              resumed_index["done_frames"] == 24,
              str(resumed_index["done_frames"]))
        check("the head's own areas survived the resume",
              all(a is not None for a in resumed_index["areas"]),
              str(resumed_index["areas"][:3]))
        check("start_frame keeps the earlier attempt's start, so the span a "
              "caller already read does not move",
              resumed_index["start_frame"] == 0,
              str(resumed_index["start_frame"]))
        check("the per frame ious are written too (gap 18)",
              isinstance(resumed_index.get("ious"), list)
              and len(resumed_index["ious"]) == 24
              and any(v is not None for v in resumed_index["ious"]),
              str((resumed_index.get("ious") or [])[:4]))
        check("an ellipse drifting across the frame overlaps itself heavily "
              "from one frame to the next, so nothing here reads as a jump",
              all(v > 0.5 for v in resumed_index["ious"] if v is not None),
              str(min(v for v in resumed_index["ious"] if v is not None)))

        print("\ntrack: rotation \"auto\" (the studio's own default; used to "
              "raise ValueError out of int(\"auto\"))")
        auto_job = call(base, "/track", {
            "video": str(frames_dir), "fps": 24,
            "prompts": {"text": ["person"]},
            "clip": "/footage/C015.mov", "clip_key": "C015-rot-auto", "rotation": "auto",
            "out_dir": str(tmp / "data" / "mattes" / "C015-rot-auto"),
        })
        check("a track with rotation \"auto\" is accepted, not a 500",
              auto_job.get("job_id", "").startswith("j_"), str(auto_job))
        auto_done = wait_for_job(base, auto_job["job_id"], 60)
        check("it finishes", auto_done["state"] == "done", str(auto_done.get("error")))
        check("the job's own rotation field echoes back a plain int, not the "
              "literal string \"auto\"", auto_done["rotation"] == 0,
              str(auto_done["rotation"]))
        auto_index = json.loads(
            (Path(auto_job["mattes"][0]["path"]) / "index.json").read_text())
        check("with no real file to probe, \"auto\" resolves to 0 and is "
              "stored as the JSON integer, not the string \"auto\"",
              auto_index["rotation"] == 0, str(auto_index["rotation"]))

        real_clip = Path(__file__).resolve().parent.parent.parent / "footage" / \
            "A001_09011336_C002.MOV"
        if real_clip.is_file():
            tagged_job = call(base, "/track", {
                "video": str(frames_dir), "fps": 24,
                "prompts": {"text": ["person"]},
                "clip": str(real_clip), "clip_key": "C002-rot-auto", "rotation": "auto",
                "out_dir": str(tmp / "data" / "mattes" / "C002-rot-auto"),
            })
            tagged_done = wait_for_job(base, tagged_job["job_id"], 60)
            check("it finishes", tagged_done["state"] == "done",
                  str(tagged_done.get("error")))
            tagged_index = json.loads(
                (Path(tagged_job["mattes"][0]["path"]) / "index.json").read_text())
            check("\"auto\" against a real clip resolves to that clip's own "
                  "display-matrix tag (90 on this file), the same field "
                  "CG.probe() reads on the studio side",
                  tagged_index["rotation"] == 90, str(tagged_index["rotation"]))
        else:
            print(f"  skip: {real_clip} is not on this machine")

        print("\ntrack: seeded from a pick")
        seeded = call(base, "/track", {
            "video": str(frames_dir), "fps": 24, "pick": pick["pick_id"],
            "select": ["t0_0"], "clip_key": "C015-rot0",
            "out_dir": str(tmp / "data" / "mattes" / "C015-rot0"),
        })
        check("a pick plus select gives one matte", len(seeded["matte_ids"]) == 1)
        done = wait_for_job(base, seeded["job_id"], 60)
        check("the seeded track finishes", done["state"] == "done", str(done.get("error")))
        seeded_index = json.loads((Path(seeded["mattes"][0]["path"]) / "index.json").read_text())
        check("the matte remembers which pick instance it came from",
              seeded_index["picked_from"] == "t0_0" and seeded_index["pick"] == pick["pick_id"],
              str(seeded_index.get("picked_from")))
        check("and it keeps the human label from the pick, not 'box 1'",
              seeded_index["label"] == "person", seeded_index["label"])

        stale = call(base, "/track", {"video": str(frames_dir), "pick": "p_gone",
                                      "select": ["t0_0"]})
        check("a pick the service has forgotten is a 400 that says to pick again",
              stale["status"] == 400 and "segment again" in stale["error"],
              stale["error"])

        print("\ntrack: the video decode path")
        from_video = call(base, "/track", {
            "video": str(video), "prompts": {"boxes": [[0.2, 0.2, 0.6, 0.8]]},
            "clip_key": "tiny", "out_dir": str(tmp / "data" / "mattes" / "tiny")})
        check("fps and frame count are probed from the file",
              from_video["fps"] == 24.0 and from_video["total_frames"] == 24,
              f"{from_video['fps']} fps, {from_video['total_frames']} frames")
        done = wait_for_job(base, from_video["job_id"], 60)
        check("a track straight off an mp4 finishes", done["state"] == "done",
              str(done.get("error")))
        check("with a frame written for every frame of the clip",
              len(list(Path(from_video["mattes"][0]["path"]).glob("*.png"))) == 24)

        print("\ntrack: a range of the clip, not the whole thing")
        ranged = call(base, "/track", {
            "video": str(frames_dir), "fps": 24, "start_frame": 8, "end_frame": 16,
            "prompts": {"text": ["person"]}, "clip_key": "range",
            "out_dir": str(tmp / "data" / "mattes" / "range")})
        done = wait_for_job(base, ranged["job_id"], 60)
        ranged_dir = Path(ranged["mattes"][0]["path"])
        names = sorted(p.name for p in ranged_dir.glob("*.png"))
        ranged_index = json.loads((ranged_dir / "index.json").read_text())
        check("only the asked for frames are written", names == [f"{i:06d}.png"
              for i in range(8, 16)], f"{len(names)} files starting {names[0]}")
        check("areas are still indexed by absolute frame index",
              ranged_index["areas"][0] is None and ranged_index["areas"][8] is not None)

        print("\na slot the model cannot fill fails on its own, and the job does not")
        many = call(base, "/track", {
            "video": str(frames_dir), "fps": 24, "max_instances": 4,
            "prompts": {"text": ["person"]}, "clip_key": "many",
            "out_dir": str(tmp / "data" / "mattes" / "many")})
        check("one matte per requested instance, decided at enqueue",
              len(many["matte_ids"]) == 4, str(len(many["matte_ids"])))
        done = wait_for_job(base, many["job_id"], 60)
        states = {m["object_id"]: m["state"] for m in done["mattes"]}
        check("the job itself is done", done["state"] == "done")
        check("the instances the model found are done, the rest are failed",
              states.get("t0_0") == "done" and states.get("t0_2") == "failed", str(states))
        empty = [m for m in done["mattes"] if m["state"] == "failed"][0]
        check("and the failed matte says why in a sentence",
              "found nothing" in (empty["error"] or ""), str(empty["error"]))

        print("\na failed job does not take the service down")
        broken = call(base, "/track", {
            "video": str(frames_dir), "fps": 24,
            "prompts": {"text": ["__fail__"]}, "clip_key": "broken",
            "out_dir": str(tmp / "data" / "mattes" / "broken")})
        done = wait_for_job(base, broken["job_id"], 60)
        check("the job is failed with the reason", done["state"] == "failed"
              and "on purpose" in (done["error"] or ""), str(done.get("error")))
        check("its matte is partial, because some frames did get written",
              done["mattes"][0]["state"] in ("partial", "failed"),
              done["mattes"][0]["state"])
        after = call(base, "/track", {
            "video": str(frames_dir), "fps": 24, "prompts": {"text": ["person"]},
            "clip_key": "after", "out_dir": str(tmp / "data" / "mattes" / "after")})
        done = wait_for_job(base, after["job_id"], 60)
        check("the very next job still runs", done["state"] == "done")
        health = call(base, "/health")
        check("health counts what failed and what did not",
              health["jobs_failed"] >= 1 and health["jobs_done"] >= 5,
              f"{health['jobs_done']} done, {health['jobs_failed']} failed")

        print("\nthe jobs view")
        jobs = call(base, "/jobs")
        check("every job is listed, newest first", len(jobs["jobs"]) >= 7
              and jobs["jobs"][0]["job_id"] == after["job_id"])
        filtered = call(base, "/jobs?clip_key=C015-rot0")
        check("and can be filtered to one clip",
              all(j["clip_key"] == "C015-rot0" for j in filtered["jobs"])
              and len(filtered["jobs"]) == 3, str(len(filtered["jobs"])))
        check("an unknown job is a 404", call(base, "/jobs/j_nope")["status"] == 404)
        check("cancelling a finished job is not an error",
              call(base, f"/jobs/{after['job_id']}/cancel", {}, method="POST")["state"] == "done")
    finally:
        stop(proc)

    # ---------------------------------------------------------------------
    # A slow service, so progress, the queue and cancel can be watched.
    # ---------------------------------------------------------------------
    port = free_port()
    # 200 frames at 60 ms each is 12 s of runway per job: long enough that the
    # checks below never race the work, short enough that the suite stays quick.
    slow_frames = make_frames(tmp / "slow_frames", count=200)
    proc, base = start(port, tmp / "slow", ["--stub-delay-ms", "60"])
    print(f"\nslow server pid {proc.pid} on {port}")

    def slow_track(name: str, text: str = "person") -> dict:
        return call(base, "/track", {
            "video": str(slow_frames), "fps": 24, "prompts": {"text": [text]},
            "clip_key": name, "out_dir": str(tmp / "slow" / "mattes" / name)})

    try:
        print("\nprogress while a job runs, and cancelling it")
        running = slow_track("slow")
        seen_running = False
        partial_frames = 0
        for _ in range(100):
            state = call(base, f"/jobs/{running['job_id']}")
            if state["state"] == "running" and 0 < state["done_frames"] < 200:
                seen_running = True
                partial_frames = state["done_frames"]
                break
            if state["state"] in ("done", "failed"):
                break
            time.sleep(0.1)
        check("a running job reports frames done out of the total",
              seen_running, f"{partial_frames}/200")
        matte_dir = Path(running["mattes"][0]["path"])
        check("matte frames appear on disk while the job is still running, "
              "which is what lets the picture play against a partial matte",
              len(list(matte_dir.glob("*.png"))) > 0)
        health_busy = call(base, "/health")
        check("health says which window of the clip is loaded, so a slow job "
              "can be understood rather than guessed at",
              isinstance(health_busy.get("window"), dict)
              and health_busy["window"]["start"] == 0,
              str(health_busy.get("window")))
        check("health reports what this process is costing the machine, which "
              "is usually the answer to why it is slow",
              health_busy["memory"]["rss_mb"] > 0
              and health_busy["memory"]["peak_rss_mb"] > 0,
              str(health_busy.get("memory")))
        check("health says it is busy", call(base, "/health")["busy"] is True)

        cancelled = call(base, f"/jobs/{running['job_id']}/cancel", {}, method="POST")
        check("cancel is accepted", cancelled["ok"])
        final = wait_for_job(base, running["job_id"], 30)
        check("the running job ends cancelled", final["state"] == "cancelled",
              final["state"])
        check("it stopped early", final["done_frames"] < 200, str(final["done_frames"]))
        index = json.loads((matte_dir / "index.json").read_text())
        check("its matte is partial, with the frames it did write",
              index["state"] == "partial" and index["done_frames"] > 0,
              f"{index['state']}, {index['done_frames']} frames")
        check("a partial matte's areas have real values then nulls",
              index["areas"][0] is not None and index["areas"][-1] is None)

        print("\na pick jumps the queue ahead of queued tracks")
        first_track = slow_track("queue1")
        wait_for_job(base, first_track["job_id"], 10, states=("running",))
        second = slow_track("queue2", "sky")
        check("the second track waits its turn",
              call(base, f"/jobs/{second['job_id']}")["state"] == "queued")
        began = time.time()
        picked = call(base, "/segment", {"image": str(slow_frames / "0000.png"),
                                         "prompts": {"text": ["person"]}})
        waited = time.time() - began
        first_state = call(base, f"/jobs/{first_track['job_id']}")["state"]
        second_state = call(base, f"/jobs/{second['job_id']}")["state"]
        check("a pick is served between two frames of the running track, not "
              "after it, so clicking the picture answers while a long track "
              "is still going",
              picked.get("ok") and first_state == "running" and waited < 5,
              f"the first track was {first_state} and the pick took {waited:.1f}s")
        check("and the queued track behind it did not jump ahead",
              second_state == "queued", f"the second track was {second_state}")

        print("\ncancelling a job that has not started")
        call(base, f"/jobs/{second['job_id']}/cancel", {}, method="POST")
        final = wait_for_job(base, second["job_id"], 10)
        check("a job cancelled while still queued never runs",
              final["state"] == "cancelled" and final["done_frames"] == 0,
              f"{final['state']}, {final['done_frames']} frames")
        second_index = json.loads((Path(second["mattes"][0]["path"]) / "index.json").read_text())
        check("and its matte is failed rather than pretending to be partial",
              second_index["state"] == "failed", second_index["state"])

        call(base, f"/jobs/{first_track['job_id']}/cancel", {}, method="POST")
        wait_for_job(base, first_track["job_id"], 30)
        check("the queue drains afterwards",
              call(base, "/health")["queue"]["queued"] == 0)
    finally:
        stop(proc)

    return report("service_e2e")


if __name__ == "__main__":
    raise SystemExit(main())
