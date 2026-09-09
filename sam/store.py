"""Contract C2: the matte store on disk.

One directory per matte:

    <out_dir>/<matte_id>/000000.png 000001.png ...   8 bit grey
    <out_dir>/<matte_id>/index.json

Two decisions worth stating because everything downstream depends on them:

* **Frame files are named by the absolute source frame index**, `%06d.png`,
  index = round(time * fps). A track over frames 48 to 120 writes
  `000048.png` first. So a reader turns a time into a file name without
  knowing anything about which range was tracked.
* **`areas`, `scores` and `ious` are indexed by absolute frame index too**,
  length `frames`, with `null` wherever no frame has been written: outside
  the tracked range, or not computed yet. That makes the area curve directly
  plottable against the clip's timeline and makes a partial matte obvious.
  `ious` is each written frame's overlap with the previous written frame,
  computed here because this is the only place both masks are in memory;
  `grade/mattes.py::quality` reads it to flag a frame where the track
  jumped onto something else (checkpoint gap 18).

`index.json` is rewritten atomically (temp file then `os.replace`) at most
once a second while a job runs, and always when the state changes, so a
reader never catches a half written file. The engine and the studio read
these files; the model never touches them again once written.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import numpy as np
from PIL import Image

INDEX_NAME = "index.json"
FRAME_GLOB = "*.png"
WRITE_EVERY_S = 1.0
MAX_STEADY = 31

# A matte id is ONE path segment and nothing else. It is used as a directory
# name, so anything that can mean "somewhere else" is refused: a separator, a
# `..`, a leading dot, an absolute path. Without this an id of
# `../../ESCAPED` walked up out of the store and an id of `/tmp/anything`
# replaced the root entirely, because `Path("/a") / "/b"` is `/b`. Both were
# proved against this file in round 1 finding 13.
ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
_ID_RE = re.compile(f"^{ID_PATTERN}$")


class StoreError(ValueError):
    """A caller asked for something this store will not write."""


def safe_id(value, what: str = "matte id") -> str:
    """`value` as a plain path segment, or raise. Never returns a path."""
    text = str(value or "")
    if not _ID_RE.match(text) or text in (".", ".."):
        raise StoreError(
            f"{what} {text!r} is not usable as a directory name: it must be 1 "
            f"to 64 characters of letters, digits, '_', '-' or '.', starting "
            f"with a letter or digit, and it may not contain a path separator")
    return text


def under(root, *parts) -> Path:
    """`root/parts...`, refused unless the result really is inside `root`.

    The pattern check above stops traversal; this stops a symlink. Both, not
    either: a `mattes/` directory whose entry is a link into somebody's
    footage folder is not something this store should follow.
    """
    base = Path(root).expanduser()
    joined = base.joinpath(*[safe_id(p) for p in parts])
    try:
        resolved = joined.resolve()
        anchor = base.resolve()
    except OSError as exc:                                     # noqa: BLE001
        raise StoreError(f"{joined} cannot be resolved: {exc}") from exc
    if resolved != anchor and not resolved.is_relative_to(anchor):
        raise StoreError(f"{joined} resolves to {resolved}, which is outside "
                         f"{anchor}")
    return joined


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
    """One matte: its frames, its index.json, its state.

    Opening a matte id that already has a directory is a RESUME (checkpoint
    gap 12): the frames already on disk stay, the per frame arrays are
    carried forward instead of blanked, `start_frame` keeps the earliest of
    the two ranges, and `done_frames` counts what is really there rather
    than only what this run writes. Without that, re-queueing the missing
    tail of a cancelled track would report 125 of 384 while 384 files sat in
    the folder, and the area curve would lose every point before the tail.
    """

    def __init__(self, root: Path, matte_id: str, header: dict, steady: int = 1):
        # Checked here as well as in the service's own route, because this is
        # the line that creates a directory and writes a file: a store that
        # trusts its caller's id is one bad request away from writing an
        # index.json anywhere on the machine (round 1 finding 13).
        self.matte_id = safe_id(matte_id)
        self.dir = under(root, self.matte_id)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.steady = _Steady(steady)
        frames = int(header.get("frames") or 0)
        try:
            previous = read_index(self.dir)
        except (OSError, ValueError):
            previous = {}
        self.index = dict(header)
        self.index.update({
            "matte_id": self.matte_id,
            "state": header.get("state", "queued"),
            "done_frames": 0,
            "steady": self.steady.n,
            "areas": [None] * frames,
            "scores": [None] * frames,
            # Overlap with the PREVIOUS WRITTEN frame, same indexing as
            # areas/scores (checkpoint gap 18). Written here because this is
            # the one place both masks are already in memory: computing it
            # later means re-reading every PNG off disk. First written frame
            # has nothing to compare against and stays None.
            "ious": [None] * frames,
            "created": header.get("created") or utc_now(),
            "updated": utc_now(),
            "error": None,
        })
        self._carry_forward(previous, frames)
        self._last_write = 0.0
        # The previous written frame as a boolean array, for the per frame
        # IoU above. Kept at the mask's own resolution: it is one array, and
        # the comparison is two numpy reductions, so this costs a frame of
        # memory rather than a second pass over the whole matte.
        self._prev_mask = None
        self.save(force=True)

    def _carry_forward(self, previous: dict, frames: int) -> None:
        """A resume keeps what the earlier attempt already produced.

        Two shapes count as the same track. The earlier index agrees about
        how long the matte is (`frames`), which is a re-queue of the same
        window; or the window THIS run was asked for sits inside the window
        the earlier index declares (`_inside_previous`), which is a re-track
        of one part of a longer matte, and then the matte keeps the LONGER
        declaration. Any other length is a different track, and merging two
        of those by index would put one attempt's numbers at another's
        timestamps. `done_frames` is recounted off the directory, because
        that is the only number that survives a process dying.
        """
        self.done = 0
        # Frames this matte has on disk, so `done_frames` counts what is
        # really there and a resumed range that overwrites a frame does not
        # count it twice.
        self._written: set[int] = set()
        if not previous:
            return
        length = int(frames)
        declared = int(previous.get("frames") or 0)
        if declared != length:
            # Tooling gap 24: a repair of one second of a twelve second matte
            # arrives here declaring the repair window, and this used to read
            # as "a different track": the arrays were blanked, `done` restarted
            # at 0 with every kept frame still on disk, and the matte's own
            # span shrank to the repair. It keeps its own span instead, and
            # this run writes into the middle of it. Only INSIDE, never wider:
            # a longer window is a widen, whose own re-track covers everything
            # from the first missing frame to the new end anyway.
            if not self._inside_previous(previous):
                return
            length = declared
            self.index["frames"] = declared
            self.index["end_frame"] = max(
                int(self.index.get("end_frame") or 0),
                int(previous.get("end_frame") or declared))
            for key in ("areas", "scores", "ious"):
                self.index[key] = [None] * declared
        for key in ("areas", "scores", "ious"):
            old = previous.get(key)
            if isinstance(old, list) and len(old) == length:
                merged = list(self.index.get(key) or [None] * length)
                for i, value in enumerate(old):
                    if value is not None and merged[i] is None:
                        merged[i] = value
                self.index[key] = merged
        old_start = previous.get("start_frame")
        new_start = self.index.get("start_frame")
        if old_start is not None and new_start is not None:
            self.index["start_frame"] = min(int(old_start), int(new_start))
        try:
            self._written = {int(p.stem) for p in self.dir.glob(FRAME_GLOB)
                             if p.stem.isdigit()}
        except OSError:
            self._written = set()
        self.done = len(self._written)

    def _inside_previous(self, previous: dict) -> bool:
        """True when this run's window sits inside the window `previous`
        declares, so the two are the same matte and this run is a repair of
        part of it (tooling gap 24).

        Read off the two indexes' own `start_frame`/`end_frame`, plus the
        three fields that say which PICTURE a matte is of: a matte tracked for
        another clip, another rotation or another working width is not this
        one whatever its frame numbers say, and merging its numbers in by
        index would be the worst kind of wrong answer. The equal length case
        above does not ask (it never did), because an id is only re-opened by
        a caller naming it, and the studio judges a mismatched recipe as
        `stale` and re-tracks it before this store is asked at all.
        """
        for key in ("clip_key", "rotation", "width"):
            if key in previous and str(previous.get(key)) != \
                    str(self.index.get(key)):
                return False
        try:
            prev_start = int(previous.get("start_frame") or 0)
            prev_end = int(previous.get("end_frame")
                           or previous.get("frames") or 0)
            start = int(self.index.get("start_frame") or 0)
            end = int(self.index.get("end_frame")
                      or self.index.get("frames") or 0)
        except (TypeError, ValueError):
            return False
        return prev_end > prev_start and prev_start <= start and end <= prev_end

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
        cur = clipped >= 0.5
        if 0 <= index < len(self.index["areas"]):
            self.index["areas"][index] = round(float(clipped.mean()), 6)
            self.index["scores"][index] = round(float(score), 4)
            ious = self.index.setdefault("ious", [None] * len(self.index["areas"]))
            if self._prev_mask is not None and self._prev_mask.shape == cur.shape:
                union = float(np.logical_or(self._prev_mask, cur).sum())
                inter = float(np.logical_and(self._prev_mask, cur).sum())
                if 0 <= index < len(ious):
                    ious[index] = round(inter / union, 4) if union > 0 else 1.0
        self._prev_mask = cur
        self._written.add(int(index))
        self.done = len(self._written)
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
