"""Contract C2: the matte store on disk.

One directory per matte:

    <out_dir>/<matte_id>/000000.png 000001.png ...   8 bit grey
    <out_dir>/<matte_id>/index.json

Two decisions worth stating because everything downstream depends on them:

* **Frame files are named by the absolute source frame index**, `%06d.png`,
  index = round(time * fps). A track over frames 48 to 120 writes
  `000048.png` first. So a reader turns a time into a file name without
  knowing anything about which range was tracked.
* **`areas` and `scores` are indexed by absolute frame index too**, length
  `frames`, with `null` wherever no frame has been written: outside the
  tracked range, or not computed yet. That makes the area curve directly
  plottable against the clip's timeline and makes a partial matte obvious.

`index.json` is rewritten atomically (temp file then `os.replace`) at most
once a second while a job runs, and always when the state changes, so a
reader never catches a half written file. The engine and the studio read
these files; the model never touches them again once written.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import numpy as np
from PIL import Image

INDEX_NAME = "index.json"
FRAME_GLOB = "*.png"
WRITE_EVERY_S = 1.0
MAX_STEADY = 31


def frame_name(index: int) -> str:
    return f"{int(index):06d}.png"


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_index(matte_dir: Path) -> dict:
    return json.loads((Path(matte_dir) / INDEX_NAME).read_text())


def write_json_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.name + f".tmp{os.getpid()}")
    temporary.write_text(json.dumps(payload, indent=2, default=_default))
    os.replace(temporary, path)


def _default(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, Path):
        return str(value)
    return str(value)


class _Steady:
    """Temporal smoothing over n frames, centred on the frame.

    Centred, not trailing, on purpose: a trailing average would make the matte
    lag the picture, which is exactly the artefact a grader would blame on the
    tracker. The cost is that frame i can only be written once frame i + n//2
    has been computed, so the last few frames land when the job finishes.
    """

    def __init__(self, n: int):
        self.n = max(1, min(int(n), MAX_STEADY))
        self.back = (self.n - 1) // 2
        self.forward = self.n // 2
        self.buffer: list[tuple] = []
        self.next = 0            # position in `buffer` of the next frame out

    def push(self, index: int, mask: np.ndarray, score: float) -> list[tuple]:
        self.buffer.append((index, mask, score))
        out = []
        # A frame can go out once `forward` newer frames have arrived behind it.
        while self.next < len(self.buffer) and \
                (len(self.buffer) - 1 - self.next) >= self.forward:
            out.append(self._emit(self.next))
            self.next += 1
        # Anything older than the oldest window we will still need is dropped,
        # so the buffer never holds more than n frames.
        while self.next > self.back:
            self.buffer.pop(0)
            self.next -= 1
        return out

    def _emit(self, position: int) -> tuple:
        index, _, score = self.buffer[position]
        low = max(0, position - self.back)
        high = min(len(self.buffer), position + self.forward + 1)
        window = [entry[1] for entry in self.buffer[low:high]]
        if len(window) == 1:
            return index, window[0], score
        return index, np.mean(np.stack(window), axis=0).astype(np.float32), score

    def drain(self) -> list[tuple]:
        """Flush the tail, each frame with whatever window it can still get."""
        out = []
        while self.next < len(self.buffer):
            out.append(self._emit(self.next))
            self.next += 1
        self.buffer.clear()
        self.next = 0
        return out


class MatteWriter:
    """One matte: its frames, its index.json, its state."""

    def __init__(self, root: Path, matte_id: str, header: dict, steady: int = 1):
        self.dir = Path(root) / matte_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.matte_id = matte_id
        self.steady = _Steady(steady)
        frames = int(header.get("frames") or 0)
        self.index = dict(header)
        self.index.update({
            "matte_id": matte_id,
            "state": header.get("state", "queued"),
            "done_frames": 0,
            "steady": self.steady.n,
            "areas": [None] * frames,
            "scores": [None] * frames,
            "created": header.get("created") or utc_now(),
            "updated": utc_now(),
            "error": None,
        })
        self.done = 0
        self._last_write = 0.0
        self.save(force=True)

    # -- state -------------------------------------------------------------

    @property
    def state(self) -> str:
        return self.index["state"]

    def set_state(self, state: str, error: str | None = None) -> None:
        self.index["state"] = state
        if error is not None:
            self.index["error"] = error
        self.save(force=True)

    def save(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_write < WRITE_EVERY_S:
            return
        self._last_write = now
        self.index["updated"] = utc_now()
        self.index["done_frames"] = self.done
        write_json_atomic(self.dir / INDEX_NAME, self.index)

    # -- frames ------------------------------------------------------------

    def push(self, index: int, mask: np.ndarray, score: float) -> None:
        """Offer one frame. It reaches disk now, or after the smoothing
        window catches up."""
        mask = np.asarray(mask, dtype=np.float32)
        if "height" not in self.index or self.index.get("height") in (None, 0):
            self.index["height"], self.index["width"] = int(mask.shape[0]), int(mask.shape[1])
        for ready in self.steady.push(index, mask, score):
            self._write(*ready)

    def flush(self) -> None:
        for ready in self.steady.drain():
            self._write(*ready)

    def _write(self, index: int, mask: np.ndarray, score: float) -> None:
        clipped = np.clip(mask, 0.0, 1.0)
        Image.fromarray((clipped * 255.0 + 0.5).astype(np.uint8), mode="L") \
            .save(self.dir / frame_name(index))
        if 0 <= index < len(self.index["areas"]):
            self.index["areas"][index] = round(float(clipped.mean()), 6)
            self.index["scores"][index] = round(float(score), 4)
        self.done += 1
        self.save()

    # -- finish ------------------------------------------------------------

    def finish(self, state: str, error: str | None = None) -> None:
        self.flush()
        self.index["state"] = state
        self.index["error"] = error
        self.save(force=True)

    def summary(self) -> dict:
        return {"matte_id": self.matte_id, "state": self.index["state"],
                "done_frames": self.done, "path": str(self.dir),
                "object_id": self.index.get("object_id"),
                "label": self.index.get("label"),
                "error": self.index.get("error")}
