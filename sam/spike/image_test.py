"""Image mode: text-prompted detection+segmentation via SAM3Processor, plus
point/box/negative-point prompting via a single-frame SAM3VideoSession (the
image predictor itself is text-only; there is no point/box entry point on it,
see SPIKE.md "API surface").
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import mlx.core as mx

from common import OUT, REPO_ID, save_mask_grey, save_overlay, write_json, model_lock

STILLS = {
    "t2": OUT / "still_t2.png",
    "t10": OUT / "still_t10.png",
    "t18": OUT / "still_t18.png",
}
TEXT_PROMPTS = ["person", "sky", "road", "face", "hair", "shirt"]


def run_text_prompts():
    from mlx_cv.models.sam3 import SAM3Processor
    from PIL import Image as PILImage

    mx.reset_peak_memory()
    t0 = time.perf_counter()
    processor = SAM3Processor.from_pretrained(REPO_ID)
    load_s = time.perf_counter() - t0
    load_peak_mem = mx.get_peak_memory()

    results = {"model_load_s": load_s, "model_load_peak_mem_bytes": load_peak_mem, "calls": []}

    for still_name, still_path in STILLS.items():
        image = np.asarray(PILImage.open(still_path).convert("RGB"))
        for prompt in TEXT_PROMPTS:
            mx.reset_peak_memory()
            t0 = time.perf_counter()
            pred = processor.predict(image, prompt)
            mx.eval  # no-op reference; predict() already evaluates internally
            elapsed = time.perf_counter() - t0
            peak_mem = mx.get_peak_memory()
            n = len(pred.detections)
            scores = pred.detections.scores.tolist() if pred.detections.scores is not None else []
            boxes = pred.detections.boxes.tolist()
            entry = {
                "still": still_name,
                "prompt": prompt,
                "latency_s": elapsed,
                "peak_mem_bytes": peak_mem,
                "instance_count": n,
                "scores": scores,
                "boxes_xyxy": boxes,
            }
            results["calls"].append(entry)
            print(f"[text] {still_name} '{prompt}': {elapsed:.3f}s, {n} instance(s), scores={scores}")

            for i in range(n):
                mask = pred.masks.data[i]
                stem = f"image_{still_name}_{prompt}_{i}"
                save_mask_grey(mask, OUT / f"{stem}_mask.png")
                save_overlay(image, mask, OUT / f"{stem}_overlay.png")

    write_json(results, OUT / "image_text_prompts.json")
    return results


def _box_from_text(processor, image, text):
    pred = processor.predict(image, text)
    if len(pred.detections) == 0:
        return None
    best = int(np.argmax(pred.detections.scores))
    return pred.detections.boxes[best]


def run_point_box_prompts():
    """Point / box / negative-point prompting on a still image via a
    single-frame SAM3VideoSession (see SPIKE.md: this is the only entry
    point for point/box conditioning in mlx-cv 0.0.4)."""
    from mlx_cv.models.sam3 import SAM3Processor, SAM3VideoSession
    from PIL import Image as PILImage

    image_path = STILLS["t10"]
    image = np.asarray(PILImage.open(image_path).convert("RGB"))

    processor = SAM3Processor.from_pretrained(REPO_ID)
    person_box = _box_from_text(processor, image, "person")
    if person_box is None:
        raise RuntimeError("text prompt 'person' found nothing on still_t10; cannot derive a point")
    x0, y0, x1, y1 = person_box
    center_point = [float((x0 + x1) / 2), float((y0 + y1) / 2)]
    # A point clearly off the person (top-left corner region, background/wall).
    negative_point = [30.0, 30.0]

    results = {"cases": []}

    def run_case(name, **prompt_kwargs):
        session = SAM3VideoSession.from_pretrained(REPO_ID)
        mx.reset_peak_memory()
        t0 = time.perf_counter()
        state = session.start_session(frames=[image])
        # object_id=0: mlx-cv 0.0.4's SAM3VideoSession._run_frame indexes its
        # bucket-assignment lookup by 0-based model index but the *default*
        # add_prompt(object_id=None) auto-assigns 1-based ids, so the natural
        # default call always raises KeyError on the first propagate_in_video
        # call. Passing object_id=0 (matching the object's 0-based insertion
        # position) is the workaround; see SPIKE.md "Failures".
        session.add_prompt(state.session_id, frame_index=0, object_id=0, **prompt_kwargs)
        video_result = session.propagate_in_video(state.session_id, start_frame_index=0, max_frame_num_to_track=1)
        elapsed = time.perf_counter() - t0
        peak_mem = mx.get_peak_memory()
        frame = video_result.frames[0]
        mask = frame.masks.data[0]
        stem = f"image_point_{name}"
        save_mask_grey(mask, OUT / f"{stem}_mask.png")
        save_overlay(image, mask, OUT / f"{stem}_overlay.png")
        entry = {
            "case": name,
            "prompt_kwargs_summary": {k: (v if not hasattr(v, "tolist") else v.tolist() if hasattr(v, "tolist") else v) for k, v in prompt_kwargs.items()},
            "latency_s": elapsed,
            "peak_mem_bytes": peak_mem,
            "mask_area_px": int(mask.sum()),
            "track_score": float(frame.tracks.scores[0]),
        }
        results["cases"].append(entry)
        print(f"[point/box] {name}: {elapsed:.3f}s, area={entry['mask_area_px']}px, score={entry['track_score']:.4f}")

    # 1. single positive point on the person's center
    run_case("single_positive_point", points=[center_point], labels=[1])
    # 2. box prompt (the text-detected person box)
    run_case("box", box=[float(v) for v in person_box])
    # 3. positive point on the person PLUS a negative point far off it, single
    #    add_prompt call (add_prompt accepts a coords/labels array together;
    #    this is the correct way to combine positive+negative points, since a
    #    second add_prompt call on the same object replaces the prompt rather
    #    than accumulating with the first, see SPIKE.md).
    run_case("positive_plus_negative_point", points=[center_point, negative_point], labels=[1, 0])

    write_json(results, OUT / "image_point_box_prompts.json")
    return results


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "text"
    with model_lock():
        if which == "text":
            run_text_prompts()
        elif which == "points":
            run_point_box_prompts()
        else:
            raise SystemExit(f"unknown mode {which!r}")
