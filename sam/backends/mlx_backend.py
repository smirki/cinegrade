"""The MLX backend: SAM 3.1 through mlx-cv on this Mac's GPU.

Everything odd in here comes from a fact the spike established, and the
comment says which one. In short (SPIKE.md has the evidence):

* Load by the exact repo id `appautomaton/sam3.1-multiplex-bf16-mlx`. The
  model card's `"sam3.1"` alias does not resolve: mlx-cv 0.0.4 ships an empty
  alias table.
* `SAM3Processor.predict(image, text)` is the only text entry point and it is
  images only. `SAM3VideoSession` accepts a box, points or a mask and has no
  text parameter at all. So a text tracked matte is two steps: detect on the
  seed frame, then seed the tracker with the detected box.
* `add_prompt`'s own default object id is 1 based while `propagate_in_video`
  looks ids up in a 0 based table, so the library's default crashes its own
  propagation. Object ids are always passed explicitly here, from 0 upward in
  insertion order.
* `SAM3VideoSession.model.detector` is a complete detector, so the processor
  is built around the already loaded video model rather than loading the
  1.75 GB checkpoint a second time.
* `propagate_in_video` is, verbatim, "clear the memories, then call
  `_run_frame` for each index". This backend runs that loop itself so a job
  can report progress per frame, write matte frames as they are computed, and
  stop when someone presses cancel. If a future mlx-cv drops `_run_frame`,
  `_propagate_public` does the same work through the public call and emits
  every frame at the end (correct, just blind until it finishes).
* mlx-cv preprocesses every frame to 1008x1008 float32, which is 12.2 MB of
  session state per frame. A whole 21 s clip would be 6.4 GB on a 16 GB
  machine, so the clip is tracked in chunks with a one frame overlap: each
  chunk is seeded from the mask of the frame it shares with the chunk before
  it, which is a real conditioning frame rather than a guess.
"""

from __future__ import annotations

import gc
import time
from typing import Any, Iterator

import numpy as np

from .base import (Backend, BackendError, BackendUnavailable, Instance,
                   TrackedMask, mask_area, mask_box, normalize_prompts,
                   plan_objects)

REPO_ID = "appautomaton/sam3.1-multiplex-bf16-mlx"
# The spike tracked 120 frames of 720 wide material in one session: 2200 s,
# 0.055 frames/s, peak 14 GB on a 16 GB machine, because a session's memory
# grows with every frame it holds (1008x1008 float32 is about 12 MB a frame
# before the tracker's own memories). So a clip is tracked in windows, and
# each window's session is dropped before the next one is built.
CHUNK_FRAMES = 48          # 48 frames of 1008x1008 float32 is about 580 MB
SCORE_THRESHOLD = 0.5      # mlx-cv's own default; below it predict() returns nothing


def _log(message: str) -> None:
    print(message, flush=True)


class MlxBackend(Backend):
    name = "mlx"

    def __init__(self, repo_id: str = REPO_ID, chunk_frames: int = CHUNK_FRAMES,
                 score_threshold: float = SCORE_THRESHOLD, log=_log):
        self.model = repo_id
        self.repo_id = repo_id
        self.chunk_frames = max(2, int(chunk_frames))
        self.score_threshold = float(score_threshold)
        self._log = log
        self._session = None
        self._processor = None
        self._mx = None

    # -- lifecycle ---------------------------------------------------------

    def load(self) -> None:
        """The machine wide model lock is NOT taken here: the service holds it
        for as long as the model is resident (see sam/server.py). A script
        using this class directly must take it itself."""
        try:
            import mlx.core as mx
            from mlx_cv.hub import resolve_pretrained
            from mlx_cv.models.sam3 import SAM3Processor, SAM3VideoSession
        except ImportError as exc:
            raise BackendUnavailable(
                f"mlx-cv is not installed in this environment ({exc}). "
                "Install it with: uv sync --project sam --extra mlx"
            ) from exc

        started = time.perf_counter()
        try:
            snapshot = resolve_pretrained(self.repo_id)
        except Exception as exc:                               # noqa: BLE001
            raise BackendUnavailable(
                f"cannot resolve the SAM 3.1 weights {self.repo_id!r}: {exc}. "
                "They live in the Hugging Face cache; the machine may be "
                "offline and the cache empty."
            ) from exc
        try:
            session = SAM3VideoSession.from_pretrained(self.repo_id)
        except Exception as exc:                               # noqa: BLE001
            raise BackendUnavailable(
                f"SAM 3.1 weights would not load through mlx-cv: {exc}") from exc

        bpe = snapshot / "bpe_simple_vocab_16e6.txt.gz" if snapshot.is_dir() else None
        if bpe is None or not bpe.is_file():
            raise BackendUnavailable(
                f"the text tokenizer vocabulary is missing from {snapshot}: "
                "text prompts cannot work without bpe_simple_vocab_16e6.txt.gz")
        # One checkpoint, one load: the video session's `detector` IS the
        # text detector, so the processor wraps it instead of loading again.
        processor = SAM3Processor(session.model.detector, bpe_path=bpe,
                                  score_threshold=self.score_threshold)

        self._mx = mx
        self._session = session
        self._processor = processor
        self._log(f"[mlx] loaded {self.repo_id} in {time.perf_counter() - started:.2f}s "
                  f"(device {mx.default_device()})")

    def close(self) -> None:
        self._session = None
        self._processor = None
        self._clear_cache()
        self._mx = None

    def _clear_cache(self) -> None:
        mx = self._mx
        if mx is None:
            return
        for name in ("clear_cache", "reset_peak_memory"):
            fn = getattr(mx, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:                              # noqa: BLE001
                    pass

    def _ready(self):
        if self._session is None or self._processor is None:
            raise BackendError("the MLX backend is not loaded")
        return self._session, self._processor

    # -- one frame ---------------------------------------------------------

    def segment(self, image: np.ndarray, prompts: dict,
                max_instances: int) -> list[Instance]:
        session, processor = self._ready()
        prompts = normalize_prompts(prompts)
        if not any(prompts[k] for k in ("text", "points", "boxes", "masks")):
            raise ValueError("segment needs at least one of prompts.text, "
                             "prompts.points, prompts.boxes")
        image = np.ascontiguousarray(image)
        height, width = image.shape[:2]
        slots = plan_objects(prompts, max_instances=max_instances)
        out: list[Instance] = []

        # Text: the detector, one call per phrase (SAM 3.1 segments instances
        # of ONE phrase per call; it is not a multi class detector).
        by_phrase: dict[int, list] = {}
        for slot in slots:
            if slot.kind == "text":
                by_phrase.setdefault(slot.phrase_index, []).append(slot)
        for phrase_index, phrase_slots in sorted(by_phrase.items()):
            phrase = phrase_slots[0].text
            found = processor.predict(image, phrase)
            order = np.argsort(-np.asarray(found.detections.scores)) \
                if len(found.detections) else []
            for slot, detection_index in zip(phrase_slots, order):
                mask = np.asarray(found.masks.data[int(detection_index)], dtype=np.float32)
                box = np.asarray(found.detections.boxes[int(detection_index)], dtype=np.float64)
                out.append(Instance(
                    id=slot.id,
                    score=float(found.detections.scores[int(detection_index)]),
                    box=(float(box[0]) / width, float(box[1]) / height,
                         float(box[2]) / width, float(box[3]) / height),
                    area=mask_area(mask),
                    mask=mask,
                    label=phrase,
                    prompt={"kind": "text", "text": phrase,
                            "instance": slot.instance_index},
                ))

        # Boxes, points and mask prompts: the video session on one frame. The
        # image predictor has no point or box entry point at all.
        geometric = [s for s in slots if s.kind in ("box", "points", "mask")]
        if geometric:
            state = session.start_session(frames=[image])
            try:
                added = [slot for slot in geometric
                         if self._add_prompt(session, state, slot, 0, width, height)]
                result = self._run_frame(session, state, 0)
                masks, scores = self._frame_masks(result, added)
                for slot in added:
                    mask = masks.get(slot.id)
                    if mask is None:
                        continue
                    out.append(Instance(
                        id=slot.id, score=float(scores.get(slot.id, 0.0)),
                        box=mask_box(mask), area=mask_area(mask), mask=mask,
                        label=slot.label, prompt=slot.as_dict()))
            finally:
                session.sessions.pop(state.session_id, None)
                self._clear_cache()

        out.sort(key=lambda inst: -inst.score)
        return out

    # -- a clip ------------------------------------------------------------

    def track(self, frames: Iterator[np.ndarray], fps: float, prompts: dict,
              select: Any, on_frame) -> None:
        session, processor = self._ready()
        prompts = normalize_prompts(prompts)
        slots = plan_objects(prompts,
                             max_instances=prompts.get("max_instances") or 1,
                             select=select)
        if not slots:
            raise ValueError("track needs at least one prompt")

        iterator = iter(frames)
        try:
            seed_frame = next(iterator)
        except StopIteration:
            return
        seed_frame = np.ascontiguousarray(seed_frame)
        height, width = seed_frame.shape[:2]

        live = self._seed_slots(processor, seed_frame, slots, width, height)
        if not live:
            raise BackendError(
                "the detector found nothing for any of these prompts on the "
                "seed frame, so there is nothing to track")

        last_mask: dict[str, np.ndarray] = {}
        for chunk_start, chunk in self._chunks(iterator, seed_frame):
            # What the service reports on /health, so a person watching a slow
            # job can see which window of the clip is loaded right now.
            self.window = {"start": chunk_start, "end": chunk_start + len(chunk),
                           "frames": len(chunk), "size": self.chunk_frames}
            state = session.start_session(frames=chunk)
            try:
                order = []
                for slot in live:
                    if chunk_start == 0:
                        seeded = self._add_prompt(session, state, slot, 0, width, height)
                    else:
                        previous = last_mask.get(slot.id)
                        if previous is None or not previous.any():
                            continue      # lost the object; it stays lost
                        session.add_prompt(state.session_id, frame_index=0,
                                           object_id=len(state.active_object_ids),
                                           mask=previous)
                        seeded = True
                    if seeded:
                        order.append(slot)
                if not order:
                    raise BackendError(
                        "every tracked object was lost before the end of the clip")
                # Object ids must be 0..n-1 in insertion order (mlx-cv 0.0.4
                # bug, see the module docstring), which is what `order` is.
                state.memories.clear()
                for index in range(len(chunk)):
                    result = self._run_frame(session, state, index)
                    masks, scores = self._frame_masks(result, order)
                    for slot_id, mask in masks.items():
                        if mask.any():
                            last_mask[slot_id] = mask
                    if index == 0 and chunk_start > 0:
                        continue          # the overlap frame, already emitted
                    on_frame(chunk_start + index,
                             {slot_id: TrackedMask(mask, float(scores.get(slot_id, 0.0)))
                              for slot_id, mask in masks.items()})
            finally:
                # Everything this window held goes before the next one is
                # built: the session state, its preprocessed frames, the raw
                # frames, and MLX's own buffers. On a 16 GB machine the
                # difference between freeing here and freeing "eventually" is
                # the difference between tracking and swapping.
                session.sessions.pop(state.session_id, None)
                state = None
                chunk = None
                self.window = None
                gc.collect()
                self._clear_cache()

    # -- the pieces --------------------------------------------------------

    def _chunks(self, iterator, first):
        """Chunks of frames with a one frame overlap, and the absolute index
        of each chunk's first frame."""
        buffer = [first]
        start = 0
        for frame in iterator:
            buffer.append(np.ascontiguousarray(frame))
            if len(buffer) >= self.chunk_frames:
                yield start, buffer
                start += len(buffer) - 1
                buffer = [buffer[-1]]
        if len(buffer) > 1 or start == 0:
            yield start, buffer

    def _seed_slots(self, processor, seed_frame, slots, width, height) -> list:
        """Fill every text slot with a detected box; drop the ones the model
        cannot fill. Box, point and mask slots need no detection."""
        live: list = []
        by_phrase: dict[int, list] = {}
        for slot in slots:
            if slot.kind == "text":
                by_phrase.setdefault(slot.phrase_index, []).append(slot)
        for phrase_index, phrase_slots in sorted(by_phrase.items()):
            phrase = phrase_slots[0].text
            started = time.perf_counter()
            found = processor.predict(seed_frame, phrase)
            count = len(found.detections)
            self._log(f"[mlx] seed detect {phrase!r}: {count} instance(s) in "
                      f"{time.perf_counter() - started:.2f}s")
            if not count:
                continue
            order = np.argsort(-np.asarray(found.detections.scores))
            for slot, detection_index in zip(phrase_slots, order):
                box = np.asarray(found.detections.boxes[int(detection_index)],
                                 dtype=np.float64)
                slot.box = (float(box[0]) / width, float(box[1]) / height,
                            float(box[2]) / width, float(box[3]) / height)
                live.append(slot)
        for slot in slots:
            if slot.kind in ("box", "points", "mask"):
                live.append(slot)
        # Keep the caller's order, not the phrase grouping.
        position = {slot.id: i for i, slot in enumerate(slots)}
        live.sort(key=lambda slot: position[slot.id])
        return live

    def _add_prompt(self, session, state, slot, frame_index, width, height) -> bool:
        """One prompt for one object. Coordinates arrive as fractions and go
        to mlx-cv in the frame's own pixels."""
        object_id = len(state.active_object_ids)
        if slot.box is not None:
            x0, y0, x1, y1 = slot.box
            session.add_prompt(state.session_id, frame_index=frame_index,
                               object_id=object_id,
                               box=[x0 * width, y0 * height, x1 * width, y1 * height])
            return True
        if slot.points:
            coordinates = [[p["x"] * width, p["y"] * height] for p in slot.points]
            labels = [int(p.get("label", 1)) for p in slot.points]
            session.add_prompt(state.session_id, frame_index=frame_index,
                               object_id=object_id, points=coordinates, labels=labels)
            return True
        if slot.mask_path:
            from PIL import Image

            mask = np.asarray(Image.open(slot.mask_path).convert("L"),
                              dtype=np.float32) / 255.0
            if mask.shape[:2] != (height, width):
                mask = np.asarray(Image.fromarray((mask * 255).astype(np.uint8))
                                  .resize((width, height), Image.BILINEAR),
                                  dtype=np.float32) / 255.0
            session.add_prompt(state.session_id, frame_index=frame_index,
                               object_id=object_id, mask=mask)
            return True
        return False

    def _run_frame(self, session, state, index):
        runner = getattr(session, "_run_frame", None)
        if callable(runner):
            return runner(state, index)
        return self._propagate_public(session, state, index)

    def _propagate_public(self, session, state, index):
        """Fallback for a future mlx-cv without `_run_frame`: one public call
        per frame. Correct only for the first frame of a chunk, so it is used
        for a single frame segment and otherwise reports the whole chunk at
        once."""
        result = session.propagate_in_video(state.session_id,
                                            start_frame_index=index,
                                            max_frame_num_to_track=1)
        return result.frames[0]

    @staticmethod
    def _frame_masks(result, slots) -> tuple[dict[str, np.ndarray], dict[str, float]]:
        """mlx-cv returns masks in the order of `tracks.ids`, which are the
        0 based object ids we assigned in insertion order."""
        masks: dict[str, np.ndarray] = {}
        scores: dict[str, float] = {}
        ids = list(result.tracks.ids)
        for position, object_id in enumerate(ids):
            index = int(object_id)
            if index >= len(slots):
                continue
            slot = slots[index]
            masks[slot.id] = np.asarray(result.masks.data[position], dtype=np.float32)
            try:
                scores[slot.id] = float(result.tracks.scores[position])
            except Exception:                                  # noqa: BLE001
                scores[slot.id] = 0.0
        return masks, scores
