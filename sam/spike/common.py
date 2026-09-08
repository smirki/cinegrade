"""Shared helpers for the SAM 3.1 feasibility spike scripts."""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np
from PIL import Image

OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(parents=True, exist_ok=True)

REPO_ID = "appautomaton/sam3.1-multiplex-bf16-mlx"

MODEL_LOCK_DIR = Path("/tmp/fixxr-sam-model.lock")


@contextmanager
def model_lock(retry_s: float = 20.0):
    """Machine-wide lock: only one SAM model may be resident at a time,
    because other lanes on this machine load SAM too. mkdir is atomic, so
    "succeeded" means "I hold it"; remove the directory on exit (even on
    failure) so the next waiter can proceed."""
    held = False
    while not held:
        try:
            MODEL_LOCK_DIR.mkdir()
            held = True
        except FileExistsError:
            print(f"[model_lock] held by another process, waiting {retry_s}s...", flush=True)
            time.sleep(retry_s)
    try:
        yield
    finally:
        try:
            MODEL_LOCK_DIR.rmdir()
        except OSError:
            pass


def now() -> float:
    return time.perf_counter()


def save_mask_grey(mask: np.ndarray, path: Path) -> None:
    """mask: (H, W) bool -> 8 bit grey PNG, 255 = inside mask."""
    arr = (mask.astype(np.uint8) * 255)
    Image.fromarray(arr, mode="L").save(path)


def save_overlay(image: np.ndarray, mask: np.ndarray, path: Path, color=(255, 60, 60), alpha=0.5) -> None:
    """image: (H, W, 3) uint8 RGB, mask: (H, W) bool -> RGB PNG with a translucent overlay."""
    base = image.astype(np.float32)
    overlay = base.copy()
    color_arr = np.array(color, dtype=np.float32)
    overlay[mask] = base[mask] * (1 - alpha) + color_arr * alpha
    Image.fromarray(overlay.astype(np.uint8), mode="RGB").save(path)


def mask_area(mask: np.ndarray) -> int:
    return int(mask.sum())


def iou(a: np.ndarray, b: np.ndarray) -> float:
    inter = np.logical_and(a, b).sum()
    union = np.logical_or(a, b).sum()
    if union == 0:
        return 1.0 if inter == 0 else 0.0
    return float(inter) / float(union)


def write_json(obj, path: Path) -> None:
    path.write_text(json.dumps(obj, indent=2, default=_json_default))


def _json_default(o):
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return str(o)
