"""Video mode: SAM3VideoSession propagation over the 5s Rec.709 clips.

There is no text-prompt entry point on the video session (add_prompt only
takes box/points/mask, see SPIKE.md). To seed a text-named object we run
SAM3Processor.predict on frame 0 to get a box, then add_prompt(box=...).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image as PILImage
import mlx.core as mx

from common import OUT, REPO_ID, save_mask_grey, save_overlay, iou, mask_area, write_json, model_lock

FRAME_DIRS = {
    "1280": OUT / "frames_1280",
    "720": OUT / "frames_720",
}
FPS = 24


def load_frames(width_key: str):
    frame_dir = FRAME_DIRS[width_key]
    paths = sorted(frame_dir.glob("frame_*.png"))
    frames = [np.asarray(PILImage.open(p).convert("RGB")) for p in paths]
    return frames


def _box_from_text(processor, image, text):
    pred = processor.predict(image, text)
    if len(pred.detections) == 0:
        return None
    best = int(np.argmax(pred.detections.scores))
    return [float(v) for v in pred.detections.boxes[best]]


def _matte_strip_indices(n_frames: int, fps: int) -> list[int]:
    """One frame index per second of the clip, per the brief."""
    return list(range(0, n_frames, fps))


def run_single_object(width_key: str, prompt_text: str = "person"):
    from mlx_cv.models.sam3 import SAM3Processor, SAM3VideoSession

    frames = load_frames(width_key)
    n = len(frames)

    processor = SAM3Processor.from_pretrained(REPO_ID)
    box = _box_from_text(processor, frames[0], prompt_text)
    if box is None:
        raise RuntimeError(f"text prompt {prompt_text!r} found nothing on frame 0 of {width_key}")
    del processor
    mx.reset_peak_memory()

    session = SAM3VideoSession.from_pretrained(REPO_ID)
    state = session.start_session(frames=frames)
    # object_id=0: see the note in image_test.py / SPIKE.md "Failures" --
    # mlx-cv 0.0.4 needs 0-based ids matching insertion order or
    # propagate_in_video raises KeyError.
    session.add_prompt(state.session_id, frame_index=0, object_id=0, box=box)

    t0 = time.perf_counter()
    video_result = session.propagate_in_video(state.session_id, start_frame_index=0)
    elapsed = time.perf_counter() - t0
    peak_mem = mx.get_peak_memory()

    fps_achieved = n / elapsed
    strip_indices = _matte_strip_indices(n, FPS)
    strip_dir = OUT / f"video_{width_key}_{prompt_text}_strip"
    strip_dir.mkdir(exist_ok=True)

    per_frame = []
    prev_mask = None
    anomalies = []
    for i, result in enumerate(video_result.frames):
        mask = result.masks.data[0]
        area = mask_area(mask)
        iou_prev = None if prev_mask is None else iou(mask, prev_mask)
        per_frame.append({
            "frame": i,
            "area_px": area,
            "iou_prev": iou_prev,
            "track_score": float(result.tracks.scores[0]),
        })
        if prev_mask is not None:
            prev_area = mask_area(prev_mask)
            area_ratio = area / prev_area if prev_area > 0 else float("inf")
            if iou_prev is not None and iou_prev < 0.5:
                anomalies.append({"frame": i, "reason": "iou_drop", "iou_prev": iou_prev})
            if prev_area > 0 and (area_ratio > 1.6 or area_ratio < 0.4):
                anomalies.append({"frame": i, "reason": "area_jump", "ratio": area_ratio})
            if area == 0 and prev_area > 0:
                anomalies.append({"frame": i, "reason": "dropout"})
        prev_mask = mask
        if i in strip_indices:
            save_overlay(frames[i], mask, strip_dir / f"sec_{i // FPS:02d}_frame_{i:04d}.png")

    summary = {
        "width": width_key,
        "prompt": prompt_text,
        "n_frames": n,
        "total_time_s": elapsed,
        "fps_achieved": fps_achieved,
        "peak_mem_bytes": peak_mem,
        "seed_box_xyxy": box,
        "anomalies": anomalies,
        "per_frame": per_frame,
    }
    write_json(summary, OUT / f"video_{width_key}_{prompt_text}_single.json")
    print(f"[video single] {width_key} '{prompt_text}': {elapsed:.2f}s total, {fps_achieved:.3f} fps, "
          f"{len(anomalies)} anomalies, peak_mem={peak_mem/1e9:.2f}GB")
    return summary


def run_multi_object(width_key: str, prompt_texts=("person", "sky")):
    from mlx_cv.models.sam3 import SAM3Processor, SAM3VideoSession

    frames = load_frames(width_key)
    n = len(frames)

    processor = SAM3Processor.from_pretrained(REPO_ID)
    boxes = {}
    for text in prompt_texts:
        box = _box_from_text(processor, frames[0], text)
        if box is None:
            raise RuntimeError(f"text prompt {text!r} found nothing on frame 0 of {width_key}")
        boxes[text] = box
    del processor
    mx.reset_peak_memory()

    session = SAM3VideoSession.from_pretrained(REPO_ID)
    state = session.start_session(frames=frames)
    object_ids = {}
    # 0-based ids in insertion order (see the object_id=0 note above).
    for idx, text in enumerate(prompt_texts):
        session.add_prompt(state.session_id, frame_index=0, object_id=idx, box=boxes[text])
        object_ids[idx] = text

    t0 = time.perf_counter()
    video_result = session.propagate_in_video(state.session_id, start_frame_index=0)
    elapsed = time.perf_counter() - t0
    peak_mem = mx.get_peak_memory()
    fps_achieved = n / elapsed

    strip_indices = _matte_strip_indices(n, FPS)
    strip_dir = OUT / f"video_{width_key}_multi_strip"
    strip_dir.mkdir(exist_ok=True)

    prev_masks = {oid: None for oid in object_ids}
    per_object_per_frame = {oid: [] for oid in object_ids}
    anomalies = []
    for i, result in enumerate(video_result.frames):
        tracks = result.tracks
        for slot, oid in enumerate(tracks.ids):
            oid = int(oid)
            mask = result.masks.data[slot]
            area = mask_area(mask)
            prev = prev_masks[oid]
            iou_prev = None if prev is None else iou(mask, prev)
            per_object_per_frame[oid].append({
                "frame": i, "area_px": area, "iou_prev": iou_prev,
                "track_score": float(tracks.scores[slot]),
            })
            if prev is not None:
                prev_area = mask_area(prev)
                ratio = area / prev_area if prev_area > 0 else float("inf")
                if iou_prev is not None and iou_prev < 0.5:
                    anomalies.append({"frame": i, "object": object_ids[oid], "reason": "iou_drop", "iou_prev": iou_prev})
                if prev_area > 0 and (ratio > 1.6 or ratio < 0.4):
                    anomalies.append({"frame": i, "object": object_ids[oid], "reason": "area_jump", "ratio": ratio})
            prev_masks[oid] = mask
            if i in strip_indices:
                save_overlay(frames[i], mask, strip_dir / f"sec_{i // FPS:02d}_frame_{i:04d}_{object_ids[oid]}.png")

    summary = {
        "width": width_key,
        "prompts": list(prompt_texts),
        "n_frames": n,
        "n_objects": len(object_ids),
        "total_time_s": elapsed,
        "fps_achieved": fps_achieved,
        "peak_mem_bytes": peak_mem,
        "seed_boxes": boxes,
        "anomalies": anomalies,
        "per_object_per_frame": per_object_per_frame,
    }
    write_json(summary, OUT / f"video_{width_key}_multi.json")
    print(f"[video multi] {width_key} {prompt_texts}: {elapsed:.2f}s total, {fps_achieved:.3f} fps, "
          f"{len(anomalies)} anomalies, peak_mem={peak_mem/1e9:.2f}GB")
    return summary


def run_backward_and_streaming_probe(width_key: str = "1280", prompt_text: str = "person"):
    """Prompt a later frame and propagate backward; probe whether calling
    propagate_in_video twice on the same session can stream incrementally."""
    from mlx_cv.models.sam3 import SAM3Processor, SAM3VideoSession

    frames = load_frames(width_key)
    n = len(frames)
    mid = n // 2

    processor = SAM3Processor.from_pretrained(REPO_ID)
    box = _box_from_text(processor, frames[mid], prompt_text)
    del processor
    if box is None:
        raise RuntimeError("no box found on mid frame")

    session = SAM3VideoSession.from_pretrained(REPO_ID)
    state = session.start_session(frames=frames)
    session.add_prompt(state.session_id, frame_index=mid, object_id=0, box=box)

    t0 = time.perf_counter()
    backward = session.propagate_in_video(state.session_id, start_frame_index=mid, reverse=True)
    t_back = time.perf_counter() - t0
    print(f"[backward] prompted frame {mid}, propagated backward over {len(backward.frames)} frames in {t_back:.2f}s")

    # Streaming probe: call propagate_in_video again for the remaining forward
    # frames on the SAME session/state, to see whether memory persists across
    # separate calls (the streaming question in the brief).
    streaming_result = {"prompted_frame": mid}
    try:
        t1 = time.perf_counter()
        forward_continue = session.propagate_in_video(state.session_id, start_frame_index=mid + 1)
        t_fwd = time.perf_counter() - t1
        streaming_result["second_call_succeeded"] = True
        streaming_result["second_call_time_s"] = t_fwd
        streaming_result["second_call_frame_count"] = len(forward_continue.frames)
        print(f"[streaming probe] second propagate_in_video call succeeded ({len(forward_continue.frames)} frames, {t_fwd:.2f}s) "
              "-- but note propagate_in_video clears session memory on every call (see SPIKE.md); "
              "check whether frame 0 result used stale/incorrect conditioning.")
    except Exception as exc:
        streaming_result["second_call_succeeded"] = False
        streaming_result["second_call_error"] = f"{type(exc).__name__}: {exc}"
        print(f"[streaming probe] second propagate_in_video call FAILED as predicted: {type(exc).__name__}: {exc}")

    result = {
        "backward": {
            "prompted_frame": mid,
            "frames_propagated": len(backward.frames),
            "time_s": t_back,
            "frame_indices_seen": [int(f.tracks.frame_index) for f in backward.frames],
        },
        "streaming_probe": streaming_result,
    }
    write_json(result, OUT / "video_backward_and_streaming_probe.json")
    return result


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "single"
    width = sys.argv[2] if len(sys.argv) > 2 else "1280"
    # Machine-wide lock: only one SAM model resident at a time across every
    # lane on this box, held for this whole process (one model load +
    # inference), released on exit.
    with model_lock():
        if which == "single":
            run_single_object(width)
        elif which == "multi":
            run_multi_object(width)
        elif which == "backward":
            run_backward_and_streaming_probe(width)
        else:
            raise SystemExit(f"unknown mode {which!r}")
