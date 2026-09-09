"""Where a track's pixels come from: a video file, or a directory of frames.

The service never decodes a container inside the model process's memory: it
pipes raw frames out of one ffmpeg process and hands them to the backend one
at a time. That keeps a long clip's memory flat, lets the first frame reach
the tracker immediately instead of after a whole extraction pass, and leaves
no temporary files to clean up if a job is cancelled.

Every ffmpeg call made here is bounded by an explicit duration, and the only
process ever killed is the one whose pid this module captured itself.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator

import numpy as np
from PIL import Image


def _tail(handle, limit: int = 400) -> str:
    """The last of whatever ffmpeg said, for an error message. Never raises:
    an unreadable stderr must not replace the real failure."""
    try:
        handle.seek(0)
        text = handle.read().decode("utf-8", "replace").strip()
    except Exception:                                          # noqa: BLE001
        return ""
    return text[-limit:]

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
PROBE_TIMEOUT_S = 30
DEFAULT_FPS = 24.0


class VideoError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: int) -> str:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise VideoError(f"{cmd[0]} is not installed on this machine") from exc
    except subprocess.TimeoutExpired as exc:
        raise VideoError(f"{cmd[0]} did not answer within {timeout}s") from exc
    if done.returncode != 0:
        raise VideoError(f"{cmd[0]} failed: {done.stderr.strip()[:400]}")
    return done.stdout


def probe(path: Path) -> dict:
    """fps, frame count and size of a video file, through ffprobe."""
    out = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries",
                "stream=width,height,avg_frame_rate,r_frame_rate,nb_frames:format=duration",
                "-of", "json", str(path)], PROBE_TIMEOUT_S)
    try:
        data = json.loads(out)
        stream = data["streams"][0]
    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        raise VideoError(f"{path} has no video stream ffprobe can read") from exc

    def rate(value: str | None) -> float:
        if not value or value in ("0/0", "N/A"):
            return 0.0
        if "/" in value:
            num, den = value.split("/", 1)
            return float(num) / float(den) if float(den) else 0.0
        return float(value)

    fps = rate(stream.get("avg_frame_rate")) or rate(stream.get("r_frame_rate")) or DEFAULT_FPS
    duration = float(data.get("format", {}).get("duration") or 0.0)
    frames = int(stream.get("nb_frames") or 0)
    if frames <= 0:
        frames = int(round(duration * fps)) if duration else 0
    return {"fps": fps, "frames": frames, "duration": duration,
            "width": int(stream.get("width") or 0),
            "height": int(stream.get("height") or 0)}


def probe_rotation_tag(path: str | Path) -> int:
    """The clip's own display-matrix rotation tag, 0..350 in steps of 90.

    Reads the exact field `CG.probe()` reads on the studio side
    (`stream_side_data` -> a `"rotation"` entry), the last one found, same
    as the studio's own loop. This is how the service resolves the studio's
    "auto" rotation to a concrete number when it is asked to: probe the
    clip itself rather than guess. Never raises: no side data, no video
    stream, a missing file or a bad ffprobe all read as "no tag", which is
    0, the same default an untagged file gets everywhere else in this
    codebase.
    """
    try:
        out = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream_side_data", "-of", "json",
                    str(path)], PROBE_TIMEOUT_S)
        stream = json.loads(out)["streams"][0]
    except (VideoError, json.JSONDecodeError, KeyError, IndexError):
        return 0
    rot = 0
    for side_data in stream.get("side_data_list", []):
        if "rotation" in side_data:
            try:
                rot = int(float(side_data["rotation"]))
            except (TypeError, ValueError):
                rot = 0
    return rot % 360


class FrameSource:
    """A clip's frames, described first and read lazily.

    `kind` is "video" or "dir". `total` is how many frames the whole clip has,
    so a job can report progress out of a real total from the moment it is
    queued.
    """

    def __init__(self, path: str | Path, fps: float | None = None,
                 width: int | None = None):
        self.path = Path(path)
        self.scale_width = int(width) if width else None
        self._files: list[Path] = []
        if not self.path.exists():
            raise VideoError(f"there is no clip at {self.path}")
        if self.path.is_dir():
            self.kind = "dir"
            self._files = sorted(p for p in self.path.iterdir()
                                 if p.suffix.lower() in IMAGE_SUFFIXES)
            if not self._files:
                raise VideoError(f"{self.path} holds no frames this service can read")
            self.fps = float(fps or DEFAULT_FPS)
            self.total = len(self._files)
            with Image.open(self._files[0]) as first:
                self.width, self.height = first.size
        else:
            self.kind = "video"
            info = probe(self.path)
            self.fps = float(fps or info["fps"] or DEFAULT_FPS)
            self.total = info["frames"] or int(round(info["duration"] * self.fps))
            self.width, self.height = info["width"], info["height"]
        if self.scale_width and self.width:
            self.height = int(round(self.height * self.scale_width / self.width / 2) * 2)
            self.width = self.scale_width

    def range(self, start_frame: int = 0, end_frame: int | None = None) -> tuple[int, int]:
        start = max(0, int(start_frame or 0))
        end = self.total if end_frame is None else min(int(end_frame), self.total)
        if end <= start:
            raise VideoError(f"the frame range {start}..{end} is empty "
                             f"(the clip has {self.total} frames)")
        return start, end

    def frames(self, start_frame: int = 0, end_frame: int | None = None) -> Iterator[np.ndarray]:
        start, end = self.range(start_frame, end_frame)
        if self.kind == "dir":
            return self._frames_from_dir(start, end)
        return self._frames_from_video(start, end)

    def _frames_from_dir(self, start: int, end: int) -> Iterator[np.ndarray]:
        for path in self._files[start:end]:
            with Image.open(path) as image:
                image = image.convert("RGB")
                if self.scale_width:
                    image = image.resize((self.width, self.height), Image.BILINEAR)
                yield np.asarray(image, dtype=np.uint8)

    def _frames_from_video(self, start: int, end: int) -> Iterator[np.ndarray]:
        """One ffmpeg process, raw rgb down a pipe, always time bounded."""
        duration = (end - start) / self.fps
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
        if start:
            cmd += ["-ss", f"{start / self.fps:.6f}"]
        cmd += ["-i", str(self.path), "-t", f"{duration:.6f}"]
        if self.scale_width:
            cmd += ["-vf", f"scale={self.width}:-2"]
        cmd += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]

        if not (self.width and self.height):
            raise VideoError(f"ffprobe could not read the frame size of {self.path}")
        # stderr goes to a temporary FILE, never a pipe. A pipe nobody drains
        # holds 64 KB, and a chattier ffmpeg (a `-loglevel` change, a warning
        # per frame) would fill it and block ffmpeg while this loop waits on
        # stdout: a decode that hangs with no timeout, since `-t` bounds the
        # output duration and not the wall clock (round 1 finding 45). A file
        # cannot fill, and it means the message is there to read when a decode
        # produces nothing.
        errors = tempfile.TemporaryFile()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errors)
        size = self.width * self.height * 3
        wanted = end - start
        got = 0
        try:
            for _ in range(wanted):
                buffer = proc.stdout.read(size)
                if not buffer or len(buffer) < size:
                    if got == 0:
                        raise VideoError(
                            f"ffmpeg decoded no frame at all from {self.path} "
                            f"(frames {start}..{end}): "
                            f"{_tail(errors) or 'it printed nothing'}")
                    break
                got += 1
                # copy(): frombuffer hands back a read only view over the
                # pipe's buffer, and a backend may want to write into it.
                yield np.frombuffer(buffer, dtype=np.uint8).reshape(
                    self.height, self.width, 3).copy()
        finally:
            # Only this process, by the pid we started, and only ours.
            try:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=10)
            except Exception:                                  # noqa: BLE001
                pass
            for handle in (proc.stdout, proc.stderr, errors):
                try:
                    if handle is not None:
                        handle.close()
                except Exception:                              # noqa: BLE001
                    pass

    def still(self, index: int) -> np.ndarray:
        """One frame, for a pick on a clip rather than on a still image."""
        for frame in self.frames(index, index + 1):
            return frame
        raise VideoError(f"frame {index} is past the end of {self.path}")


def read_image(path: str | Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"), dtype=np.uint8)
