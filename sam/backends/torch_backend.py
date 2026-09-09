"""PyTorch backend for the SAM masks service (studio masks arc, contract C7).

Runs on the Mac GPU (mps) when available, falls back to cpu (founder's
ruling A2: "if mlx or pytorch or any variant of it doesn't work, please use
cpu"). Implements the same `Backend` shape as `sam/backends/base.py` (owned
by lane M1): `name`, `load()`, `segment()`, `track()`, `close()`. M1's
`base.py` did not exist yet when this file was written; see the "Wiring
into sam/server.py" note near the bottom of this docstring and the M1b
checkpoint for the integration point.

Weights and code path (see plan/2026-09-08-studio-masks/checkpoints/M1b.md
for the full reasoning and the licence files that came with the weights):

- Repo: `jetjodh/sam3` on Hugging Face (override with env var
  `SAM_TORCH_REPO_ID`). This is an ungated mirror of the gated
  `facebook/sam3` repo: identical file list (`model.safetensors`,
  `sam3.pt`, `config.json`, tokenizer files), same SHA-shaped content.
  `model.safetensors` is in the exact key layout the `transformers`
  library's own SAM3 model classes expect, so it loads with a plain
  `from_pretrained(repo_id)` and no custom key remapping.
- Code: `transformers.Sam3VideoModel` / `Sam3VideoProcessor` for
  text-prompted work (a single image is treated as a one-frame video
  session; a clip is a many-frame session with `propagate_in_video_iterator`
  -- one class covers both `segment()` and `track()` for text prompts), and
  `transformers.Sam3TrackerVideoModel` / `Sam3TrackerVideoProcessor` for
  point- and box-prompted work (same one-class-covers-both shape). Both
  classes take a normal torch `device` string ("cpu" or "mps") and are
  part of mainline `transformers` (>=5.9 here), not a vendored or gated
  package.
- This is SAM 3 (November 2025), not literally SAM 3.1's Object Multiplex
  variant. The two SAM-3.1-named ungated repos the founder pointed at
  (`Comfy-Org/sam3.1`, `jetjodh/sam3.1`) were evaluated and rejected for
  the CPU-floor backend: their weights only load through the official
  `facebookresearch/sam3` native code's Object Multiplex model tree, and
  that code's only demo-level entry point for it
  (`build_sam3_multiplex_video_predictor`) hard-codes `demo_model.cuda()`
  and defaults to FlashAttention-3 -- CUDA only, no cpu or mps path.
  Reaching cpu/mps for that checkpoint would mean hand-driving the
  ~3500-line `Sam3VideoTrackingMultiplexDemo` session class (init_state,
  add_new_points, propagate_in_video) against undocumented low-level APIs,
  which is exactly the extra machinery the CPU-floor ruling (A2) argues
  against. Object Multiplex is a throughput optimisation for tracking many
  objects at once (SAM 3.1 release notes: "~7x faster at 128 objects on a
  single H100", near-identical accuracy to SAM 3); the studio never tracks
  anywhere near that many objects at once, so the base SAM 3 architecture
  is not a capability loss for this product.

Machine-wide model lock: this Mac is memory bound (16 GB unified memory)
and more than one lane loads a SAM model during this build. Any process
that loads a torch SAM model here takes `/tmp/fixxr-sam-model.lock` (a
`mkdir`-based lock: success means you hold it) before constructing any
model, and removes it when the process is done with the model, success or
failure. `TorchBackend.load()` / `close()` do this automatically; the
`__main__` smoke-test block at the bottom of this file does it for
standalone runs too.
"""

from __future__ import annotations

import gc
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import numpy as np

try:                                    # as a package (sam.backends.…)
    from .. import modellock
except ImportError:                     # with sam/ itself on sys.path
    import modellock                                            # type: ignore

logger = logging.getLogger("sam.backends.torch_backend")

DEFAULT_REPO_ID = "jetjodh/sam3"

# --------------------------------------------------------------------------
# Machine-wide lock: one process holding a loaded SAM model at a time,
# across every lane working on this machine, not just within this file.
# --------------------------------------------------------------------------

_MODEL_LOCK_DIR = Path("/tmp/fixxr-sam-model.lock")

# One entry per lock this process is holding, newest last. A stack rather than
# a single slot because two threads can each hold and release around their own
# model load (the pytest suite does exactly that), and popping the wrong one
# would release a lock its owner still needs.
_HELD: list = []
_HELD_MUTEX = threading.Lock()


def acquire_model_lock(poll_s: float = 20.0, log: bool = True):
    """Block until `/tmp/fixxr-sam-model.lock` is ours. Retries forever (this
    is a build-machine safety net, not a request path with a timeout budget).

    This goes through `sam/modellock.ModelLock` rather than a bare `os.mkdir`,
    so the lock carries an `owner.json` naming this pid. A bare mkdir was the
    one case that could never be reclaimed: a killed torch run left a
    directory that named nobody, and every later run on this machine then
    waited on it until a human removed it by hand (round 1 finding 11).
    """
    lock = modellock.ModelLock(
        backend="torch", retry_s=poll_s, lock_dir=_MODEL_LOCK_DIR,
        log=(lambda message: logger.info("%s", message)) if log
        else (lambda _message: None))
    lock.acquire()
    with _HELD_MUTEX:
        _HELD.append(lock)
    return lock


def release_model_lock() -> None:
    with _HELD_MUTEX:
        lock = _HELD.pop() if _HELD else None
    if lock is not None:
        lock.release()
        return
    # Nothing this process took: leave whatever is there alone. Removing it
    # would be removing another process's lock.
    return


def _resolve_device(requested: str) -> str:
    import torch

    if requested == "cpu":
        return "cpu"
    if requested in ("mps", "auto"):
        try:
            if torch.backends.mps.is_available() and torch.backends.mps.is_built():
                # A real allocation, not just is_available(), so a broken
                # Metal driver fails here and not on the first real request.
                probe = torch.zeros(8, device="mps")
                del probe
                return "mps"
        except Exception as exc:  # noqa: BLE001 -- CPU floor: any mps failure falls back
            logger.warning("mps requested but unavailable (%s), falling back to cpu", exc)
        if requested == "mps":
            logger.warning("mps requested but not available, falling back to cpu")
    return "cpu"


def _frac_points_to_pixels(points: list[dict], width: int, height: int):
    xs = [[float(p["x"]) * width, float(p["y"]) * height] for p in points]
    labels = [int(p.get("label", 1)) for p in points]
    return xs, labels


def _frac_box_to_pixels(box: list[float], width: int, height: int) -> list[float]:
    x0, y0, x1, y1 = box
    return [x0 * width, y0 * height, x1 * width, y1 * height]


class TorchBackend:
    """C7's `Backend` shape, implemented on top of `transformers`.

    `name` is fixed once `load()` resolves the device: "torch-mps" or
    "torch-cpu" (matching the set named in C7: "mlx", "torch-mps",
    "torch-cpu", "stub").
    """

    def __init__(self, device: str = "auto", repo_id: str | None = None,
                 use_model_lock: bool = True):
        self._requested_device = device
        self.repo_id = repo_id or os.environ.get("SAM_TORCH_REPO_ID", DEFAULT_REPO_ID)
        self.name = "torch-cpu"  # corrected in load()
        self.device: str | None = None
        self._kind: str | None = None  # "text" or "point" -- which model is resident
        self._model = None
        self._processor = None
        # False when the caller already holds the machine wide lock (the
        # service does, for as long as the model is resident). This is a
        # constructor argument rather than the caller replacing this module's
        # two lock functions with no-ops: that replacement was never undone, so
        # every later TorchBackend in the process ran unlocked (round 1
        # finding 47).
        self._use_model_lock = bool(use_model_lock)
        self._lock_held = False
        self._next_auto_id = 1

    # -- lifecycle ---------------------------------------------------------

    def load(self) -> None:
        if self._use_model_lock:
            acquire_model_lock()
            self._lock_held = True
        try:
            self.device = _resolve_device(self._requested_device)
        except Exception:
            if self._lock_held:
                release_model_lock()
                self._lock_held = False
            raise
        self.name = "torch-mps" if self.device == "mps" else "torch-cpu"
        logger.info("TorchBackend loaded: device=%s repo=%s", self.device, self.repo_id)

    def close(self) -> None:
        self._evict()
        if self._lock_held:
            release_model_lock()
            self._lock_held = False

    def _evict(self) -> None:
        if self._model is not None:
            del self._model
        self._model = None
        self._processor = None
        self._kind = None
        gc.collect()
        try:
            import torch

            if self.device == "mps":
                torch.mps.empty_cache()
        except Exception:  # noqa: BLE001 -- best-effort cache release
            pass

    def _ensure(self, kind: str) -> None:
        """Load whichever model `kind` ("text" or "point") needs, evicting
        the other kind first. One model resident at a time, matching the
        machine-wide lock's intent inside this one process too."""
        if self._kind == kind and self._model is not None:
            return
        self._evict()
        t0 = time.perf_counter()
        if kind == "text":
            from transformers import Sam3VideoModel, Sam3VideoProcessor

            self._model = Sam3VideoModel.from_pretrained(self.repo_id).to(self.device).eval()
            self._processor = Sam3VideoProcessor.from_pretrained(self.repo_id)
        elif kind == "point":
            from transformers import Sam3TrackerVideoModel, Sam3TrackerVideoProcessor

            self._model = Sam3TrackerVideoModel.from_pretrained(self.repo_id).to(self.device).eval()
            self._processor = Sam3TrackerVideoProcessor.from_pretrained(self.repo_id)
        else:
            raise ValueError(f"unknown model kind {kind!r}")
        self._kind = kind
        logger.info("loaded %s model in %.1fs on %s", kind, time.perf_counter() - t0, self.device)

    # -- segment -------------------------------------------------------

    def segment(self, image: np.ndarray, prompts: dict, max_instances: int) -> list[dict]:
        """`image`: rgb uint8 HxWx3. Returns a list of Instance dicts:
        {id, score, box (fractions xyxy), area (fraction), mask (float32 HxW)}.
        """
        import torch

        text = prompts.get("text")
        points = prompts.get("points") or []
        boxes = prompts.get("boxes") or []
        height, width = image.shape[0], image.shape[1]

        if text:
            self._ensure("text")
            texts = text if isinstance(text, list) else [text]
            phrase = texts[0]
            session = self._processor.init_video_session(
                video=[image],
                inference_device=self.device,
                processing_device="cpu",
                video_storage_device="cpu",
            )
            self._processor.add_text_prompt(inference_session=session, text=phrase)
            with torch.inference_mode():
                model_outputs = next(
                    self._model.propagate_in_video_iterator(inference_session=session, max_frame_num_to_track=1)
                )
            out = self._processor.postprocess_outputs(session, model_outputs)
            instances = []
            obj_ids = out["object_ids"].tolist()
            scores = out["scores"].tolist()
            boxes_xyxy = out["boxes"].tolist()
            masks = out["masks"]  # bool, (N, H, W)
            order = sorted(range(len(obj_ids)), key=lambda i: -scores[i])[:max_instances]
            for i in order:
                mask = masks[i].to("cpu").numpy().astype(np.float32)
                area = float(mask.mean())
                bx = boxes_xyxy[i]
                instances.append(
                    {
                        "id": int(obj_ids[i]),
                        "score": float(scores[i]),
                        "box": [bx[0] / width, bx[1] / height, bx[2] / width, bx[3] / height],
                        "area": area,
                        "mask": mask,
                    }
                )
            return instances

        if points or boxes:
            self._ensure("point")
            session = self._processor.init_video_session(video=[image], inference_device=self.device)
            instances_meta = []  # (obj_id, )
            if boxes:
                # one instance per box, one batched call (matching the
                # transformers doc's multi-object pattern: a single call
                # with all obj_ids together, not one call per object --
                # a second call for a NEW object id on the same frame is
                # not the documented shape and was not exercised here).
                obj_ids_batch = list(range(1, len(boxes) + 1))
                px_boxes = [_frac_box_to_pixels(b, width, height) for b in boxes]
                self._processor.add_inputs_to_inference_session(
                    inference_session=session,
                    frame_idx=0,
                    obj_ids=obj_ids_batch,
                    input_boxes=[px_boxes],
                )
                instances_meta.extend(obj_ids_batch)
            else:
                # one instance, all points refine it (positive + negative)
                obj_id = 1
                xy, labels = _frac_points_to_pixels(points, width, height)
                self._processor.add_inputs_to_inference_session(
                    inference_session=session,
                    frame_idx=0,
                    obj_ids=obj_id,
                    input_points=[[xy]],
                    input_labels=[[labels]],
                )
                instances_meta.append(obj_id)

            with torch.inference_mode():
                outputs = self._model(inference_session=session, frame_idx=0)
            video_res_masks = self._processor.post_process_masks(
                [outputs.pred_masks],
                original_sizes=[[height, width]],
                binarize=False,
            )[0]
            instances = []
            for i, obj_id in enumerate(instances_meta[:max_instances]):
                # binarize=False on Sam3TrackerVideoProcessor.post_process_masks
                # returns raw mask logits, not 0..1 probabilities; sigmoid
                # turns that into the soft float32 alpha C7 wants.
                mask = video_res_masks[i]
                mask = mask.squeeze().to("cpu").float().numpy()
                mask = (1.0 / (1.0 + np.exp(-mask))).astype(np.float32)
                ys, xs = np.where(mask > 0.5)
                if len(xs) > 0:
                    box = [
                        float(xs.min()) / width,
                        float(ys.min()) / height,
                        float(xs.max() + 1) / width,
                        float(ys.max() + 1) / height,
                    ]
                else:
                    box = [0.0, 0.0, 0.0, 0.0]
                instances.append(
                    {
                        "id": obj_id,
                        "score": 1.0,
                        "box": box,
                        "area": float((mask > 0.5).mean()),
                        "mask": mask,
                    }
                )
            return instances

        raise ValueError("segment() needs at least one of prompts.text, prompts.points, prompts.boxes")

    # -- track -----------------------------------------------------------

    def track(
        self,
        frames: Iterator[np.ndarray],
        fps: float,
        prompts: dict,
        select: list[int] | str,
        on_frame: Callable[[int, dict[int, np.ndarray]], None],
    ) -> None:
        frame_list = list(frames)
        if not frame_list:
            return
        height, width = frame_list[0].shape[0], frame_list[0].shape[1]
        text = prompts.get("text")
        points = prompts.get("points") or []
        boxes = prompts.get("boxes") or []
        import torch

        keep_ids = None if select in (None, "all") else set(int(s) for s in select)

        if text:
            self._ensure("text")
            texts = text if isinstance(text, list) else [text]
            session = self._processor.init_video_session(
                video=frame_list,
                inference_device=self.device,
                processing_device="cpu",
                video_storage_device="cpu",
            )
            self._processor.add_text_prompt(inference_session=session, text=texts[0])
            with torch.inference_mode():
                for model_outputs in self._model.propagate_in_video_iterator(inference_session=session):
                    out = self._processor.postprocess_outputs(session, model_outputs)
                    frame_masks = {}
                    obj_ids = out["object_ids"].tolist()
                    masks = out["masks"]
                    for i, obj_id in enumerate(obj_ids):
                        if keep_ids is not None and obj_id not in keep_ids:
                            continue
                        frame_masks[int(obj_id)] = masks[i].to("cpu").numpy().astype(np.float32)
                    on_frame(int(model_outputs.frame_idx), frame_masks)
            return

        if points or boxes:
            self._ensure("point")
            session = self._processor.init_video_session(video=frame_list, inference_device=self.device)
            if boxes:
                obj_ids_batch = list(range(1, len(boxes) + 1))
                px_boxes = [_frac_box_to_pixels(b, width, height) for b in boxes]
                self._processor.add_inputs_to_inference_session(
                    inference_session=session, frame_idx=0, obj_ids=obj_ids_batch, input_boxes=[px_boxes]
                )
            else:
                obj_id = 1
                xy, labels = _frac_points_to_pixels(points, width, height)
                self._processor.add_inputs_to_inference_session(
                    inference_session=session,
                    frame_idx=0,
                    obj_ids=obj_id,
                    input_points=[[xy]],
                    input_labels=[[labels]],
                )
            with torch.inference_mode():
                for out in self._model.propagate_in_video_iterator(inference_session=session):
                    video_res_masks = self._processor.post_process_masks(
                        [out.pred_masks], original_sizes=[[height, width]], binarize=False
                    )[0]
                    frame_masks = {}
                    for i, obj_id in enumerate(session.obj_ids):
                        if keep_ids is not None and obj_id not in keep_ids:
                            continue
                        logits = video_res_masks[i].squeeze().to("cpu").float().numpy()
                        mask = (1.0 / (1.0 + np.exp(-logits))).astype(np.float32)
                        frame_masks[int(obj_id)] = mask
                    on_frame(int(out.frame_idx), frame_masks)
            return

        raise ValueError("track() needs at least one of prompts.text, prompts.points, prompts.boxes")


# --------------------------------------------------------------------------
# Wiring into sam/server.py (for M1): `--backend auto` tries mlx, then
# torch-mps, then torch-cpu, logging each failure (C7). `TorchBackend(device=
# "mps")` and `TorchBackend(device="cpu")` are the two concrete attempts;
# `TorchBackend(device="auto")` (the default) does the mps-then-cpu probe
# itself and sets `.name` to whichever it landed on, so a caller that only
# wants "give me the best available torch backend" can construct one
# instance and call `load()`. `sam/backends/base.py` did not exist yet when
# this file was written (M1 had not started); this class is written to
# C7's shape exactly (name, load, segment, track, close) so `class
# TorchBackend(Backend):` needs at most an added base class, no logic
# changes, once `base.py` lands.
# --------------------------------------------------------------------------


if __name__ == "__main__":
    import argparse
    import json

    from PIL import Image

    parser = argparse.ArgumentParser(description="torch_backend smoke test")
    parser.add_argument("mode", choices=["segment-text", "segment-point", "track-text"])
    parser.add_argument("--image", type=str, help="path to an RGB image (still)")
    parser.add_argument("--frames-dir", type=str, help="directory of 000000.png... frames")
    parser.add_argument("--fps", type=float, default=25.0)
    parser.add_argument("--text", type=str, default="person")
    parser.add_argument("--point", type=str, default=None, help="x,y as fractions 0..1")
    parser.add_argument("--device", type=str, default="auto", choices=["auto", "cpu", "mps"])
    parser.add_argument("--out-dir", type=str, default="spike/torch-out")
    parser.add_argument("--max-frames", type=int, default=0)
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    backend = TorchBackend(device=args.device)
    t_load0 = time.perf_counter()
    backend.load()
    load_s = time.perf_counter() - t_load0
    print(f"[load] device={backend.device} name={backend.name} elapsed={load_s:.2f}s")

    try:
        if args.mode == "segment-text":
            image = np.asarray(Image.open(args.image).convert("RGB"))
            t0 = time.perf_counter()
            instances = backend.segment(image, {"text": args.text}, max_instances=8)
            elapsed = time.perf_counter() - t0
            print(f"[segment-text] '{args.text}': {elapsed:.3f}s, {len(instances)} instance(s)")
            for inst in instances:
                print(f"  id={inst['id']} score={inst['score']:.3f} area={inst['area']:.4f} box={inst['box']}")
                mpath = out_dir / f"segment_text_{args.text}_{inst['id']}_mask.png"
                Image.fromarray((inst["mask"] * 255).astype(np.uint8)).save(mpath)
            (out_dir / f"segment_text_{args.text}.json").write_text(
                json.dumps({"elapsed_s": elapsed, "n": len(instances)}, indent=2)
            )

        elif args.mode == "segment-point":
            image = np.asarray(Image.open(args.image).convert("RGB"))
            x_s, y_s = args.point.split(",")
            prompts = {"points": [{"x": float(x_s), "y": float(y_s), "label": 1}]}
            t0 = time.perf_counter()
            instances = backend.segment(image, prompts, max_instances=1)
            elapsed = time.perf_counter() - t0
            print(f"[segment-point] {args.point}: {elapsed:.3f}s, {len(instances)} instance(s)")
            for inst in instances:
                print(f"  id={inst['id']} score={inst['score']:.3f} area={inst['area']:.4f} box={inst['box']}")
                mpath = out_dir / f"segment_point_{inst['id']}_mask.png"
                Image.fromarray((inst["mask"] * 255).astype(np.uint8)).save(mpath)
            (out_dir / "segment_point.json").write_text(json.dumps({"elapsed_s": elapsed, "n": len(instances)}, indent=2))

        elif args.mode == "track-text":
            frame_paths = sorted(Path(args.frames_dir).glob("*.png"))
            if args.max_frames:
                frame_paths = frame_paths[: args.max_frames]
            frames = [np.asarray(Image.open(p).convert("RGB")) for p in frame_paths]
            print(f"[track-text] loaded {len(frames)} frames from {args.frames_dir}")
            per_frame = {}
            t0 = time.perf_counter()

            def on_frame(idx, masks):
                per_frame[idx] = masks
                for obj_id, mask in masks.items():
                    Image.fromarray((mask * 255).astype(np.uint8)).save(
                        out_dir / f"track_text_{args.text}_{obj_id}_{idx:06d}.png"
                    )

            backend.track(iter(frames), args.fps, {"text": args.text}, "all", on_frame)
            elapsed = time.perf_counter() - t0
            n_frames_done = len(per_frame)
            per_s = elapsed / max(n_frames_done, 1)
            print(f"[track-text] '{args.text}': {elapsed:.3f}s total, {n_frames_done} frames, {per_s:.3f}s/frame")
            (out_dir / f"track_text_{args.text}.json").write_text(
                json.dumps(
                    {"elapsed_s": elapsed, "n_frames": n_frames_done, "s_per_frame": per_s},
                    indent=2,
                )
            )
    finally:
        backend.close()
