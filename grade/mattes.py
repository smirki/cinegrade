"""grade/mattes.py - reading the matte store (contracts C2 and C6).

The SAM service writes mattes, the studio server and this engine only ever
read them. That split is the whole point of the design: nothing in the
grading loop waits on a model, it reads PNG frames off disk that a background
job wrote earlier.

One matte on disk is a directory:

    <data-dir>/mattes/<clip-key>/<matte-id>/
        index.json
        000000.png
        000001.png
        ...

`index.json` carries what C2 fixed: matte_id, clip, clip_key, rotation, fps,
frames, width, height, recipe, state, done_frames, areas, scores, created,
model, backend. The frames are 8 bit greyscale at the service's working
width, one per SOURCE frame, and the frame at time t is index
round(t * fps). That indexing rule is stated once here (`frame_index`) and
every caller uses it, so the engine, the server's frame route and the CLI
cannot drift into three different ideas of which frame belongs to a moment.

A matte is often INCOMPLETE, and that is a normal state rather than an error:
a track that is still running has written frames 0..n, a pick that has not
been tracked yet has written exactly one frame. So every read here answers
with what exists rather than raising: `frame_path` returns None for a frame
that is not written, `nearest_written` finds the closest one that is, and
`sequence_plan` says what a render can actually do with the matte and how
much of the requested range it covers. Refusing a partial matte is a policy
decision, taken by the caller (the engine's `require_complete_mattes`), not
by this module.

No third party dependency beyond numpy. PIL is used when it is importable
(it already ships in the studio's venv and cinegrade's sheet commands use
it) and there is a small zlib PNG reader behind it, so this module works
inside a service venv that has numpy and nothing else.
"""

from __future__ import annotations

import json
import os
import struct
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

GRADE = Path(__file__).resolve().parent
CONTENT = GRADE.parent

# How a frame file is named. Six digits, zero padded, matching C2. The ffmpeg
# image2 demuxer reads the same set through PATTERN.
FRAME_DIGITS = 6
PATTERN = f"%0{FRAME_DIGITS}d.png"
INDEX_NAME = "index.json"

# The states a matte can be in (C2 / section 2 rule 3 of the plan). Kept as a
# tuple so a caller can validate rather than guess at spellings.
STATES = ("queued", "running", "done", "failed", "stale", "partial")


class MatteError(Exception):
    """A matte that cannot be read at all."""


class MatteMissing(MatteError):
    """No matte with that id under the given root."""


# --------------------------------------------------------------------------
# where the store lives
# --------------------------------------------------------------------------

def matte_root(root=None) -> Path:
    """The `mattes/` directory, resolved the way the studio resolves its data.

    Order: an explicit argument, then CINEGRADE_MATTE_ROOT (the escape hatch
    for a test or a CLI run pointed at somebody else's store), then
    STUDIO_DATA_DIR (which studio/db.py already honours, and which every test
    server sets so it cannot touch the founder's real data), then
    studio/data/mattes.

    Nothing here creates the directory: reading a store that does not exist
    yet is a normal "no mattes" answer, not a reason to write into somebody's
    data folder.
    """
    if root:
        return Path(root).expanduser()
    env = os.environ.get("CINEGRADE_MATTE_ROOT", "").strip()
    if env:
        return Path(env).expanduser()
    data = os.environ.get("STUDIO_DATA_DIR", "").strip()
    if data:
        return Path(data).expanduser() / "mattes"
    return CONTENT / "studio" / "data" / "mattes"


# --------------------------------------------------------------------------
# the index
# --------------------------------------------------------------------------

@dataclass
class MatteInfo:
    """index.json as attributes, plus where it was found.

    `path` is the matte's own directory, so a caller that wants a frame never
    has to rebuild the path from the id and the clip key.

    The written frame list is NOT read here. It is a directory scan, it
    changes while a job runs, and most callers only want one frame, so it is
    computed lazily by `written_indices()` and cached against the directory's
    mtime.
    """

    matte_id: str
    path: Path
    clip: str = ""
    clip_key: str = ""
    rotation: str = "auto"
    fps: float = 0.0
    frames: int = 0
    width: int = 0
    height: int = 0
    state: str = "done"
    done_frames: int = 0
    recipe: dict = field(default_factory=dict)
    areas: list = field(default_factory=list)
    scores: list = field(default_factory=list)
    created: float = 0.0
    model: str = ""
    backend: str = ""
    raw: dict = field(default_factory=dict)

    # cache for written_indices(), keyed by the directory's mtime
    _scan: tuple = field(default=(), repr=False, compare=False)
    _scan_key: tuple = field(default=(), repr=False, compare=False)

    # -- frames on disk ----------------------------------------------------

    def written_indices(self, refresh: bool = False) -> tuple:
        """Every frame index actually on disk, ascending.

        Scanned rather than taken from `done_frames`, because a track over a
        range writes frames start..end (not 0..n) and a pick writes exactly
        one frame somewhere in the middle. The count in index.json is a
        progress number; this is the truth.

        Cached against the directory's mtime so a render that asks about
        several layers does not stat the same directory ten times, and a job
        that writes another frame invalidates the cache by touching the
        directory.
        """
        try:
            st = self.path.stat()
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = ()
        if refresh or key != self._scan_key or not self._scan_key:
            out = []
            try:
                for p in self.path.iterdir():
                    if p.suffix.lower() != ".png":
                        continue
                    stem = p.stem
                    if stem.isdigit():
                        out.append(int(stem))
            except OSError:
                out = []
            out.sort()
            self._scan = tuple(out)
            self._scan_key = key
        return self._scan

    @property
    def written_count(self) -> int:
        return len(self.written_indices())

    @property
    def total_frames(self) -> int:
        """How many frames the matte is meant to have.

        `frames` from index.json when it is there; otherwise the highest
        written index plus one, so a store written by hand (a test, a service
        that crashed before rewriting the index) still reports something
        usable instead of zero.
        """
        if self.frames:
            return int(self.frames)
        idx = self.written_indices()
        return (idx[-1] + 1) if idx else 0

    @property
    def is_partial(self) -> bool:
        """True when the matte does not cover every frame it claims to have.

        Both halves matter: a state of `partial`, `queued` or `running` says
        the service knows it is not finished, and a short frame list says so
        whatever the state field claims (a job killed between the last frame
        and the index rewrite leaves `running` on disk forever).
        """
        if self.state in ("queued", "running", "partial", "failed", "stale"):
            return True
        total = self.total_frames
        return total > 0 and self.written_count < total


def _index_of(dir_path: Path) -> dict:
    p = dir_path / INDEX_NAME
    try:
        return json.loads(p.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise MatteError(f"unreadable {p}: {exc}") from exc


def _parse_created(value) -> float:
    """`created` as a float epoch, however the writer spelled it.

    The SAM service (sam/store.py, `MatteWriter.__init__`) always writes
    this field as an ISO 8601 UTC string (`utc_now()`), never a number; a
    hand built or older fixture may still use a plain epoch float. Both
    round trip here rather than one of them raising `ValueError` out of
    `info_from_dir`, which is what a bare `float(...)` did on any real
    matte a real service produced.
    """
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _parse_rotation(value) -> str:
    """`rotation` as MatteInfo's own string, "auto" only when truly absent.

    sam/server.py's own /track handler resolves rotation to a plain int
    (`int(body.get("rotation") or 0)`) before ever writing it to index.json,
    so a real service's own record is the JSON integer 0 for "no rotation",
    not the string "0". `raw.get("rotation") or "auto"` treats that 0 as
    falsy and silently rewrites every real, correctly recorded rotation-0
    matte as "auto", which then never again matches a viewer actually at
    rotation 0 (masks.js's componentState, the client's own stale check):
    the matte reports itself stale forever, for the single most common
    concrete rotation there is. `None` and `""` are the only "not recorded"
    cases; 0 (int or string) is a real, valid answer and must round trip.
    """
    if value is None or value == "":
        return "auto"
    return str(value)


def info_from_dir(dir_path, matte_id: str | None = None) -> MatteInfo:
    """Build a MatteInfo from a matte directory, index.json or not.

    A directory with frames and no index still reads: the id falls back to
    the directory name and the size to the first frame's own size. That is
    what makes a hand made matte (a test fixture, a service still mid write)
    usable instead of a special case every caller has to carry.
    """
    dir_path = Path(dir_path)
    if not dir_path.is_dir():
        raise MatteMissing(f"no matte directory at {dir_path}")
    raw = _index_of(dir_path)
    info = MatteInfo(
        matte_id=str(raw.get("matte_id") or matte_id or dir_path.name),
        path=dir_path,
        clip=str(raw.get("clip") or ""),
        clip_key=str(raw.get("clip_key") or ""),
        rotation=_parse_rotation(raw.get("rotation")),
        fps=float(raw.get("fps") or 0.0),
        frames=int(raw.get("frames") or 0),
        width=int(raw.get("width") or 0),
        height=int(raw.get("height") or 0),
        state=str(raw.get("state") or ("done" if raw else "partial")),
        done_frames=int(raw.get("done_frames") or 0),
        recipe=raw.get("recipe") or {},
        areas=list(raw.get("areas") or []),
        scores=list(raw.get("scores") or []),
        created=_parse_created(raw.get("created")),
        model=str(raw.get("model") or ""),
        backend=str(raw.get("backend") or ""),
        raw=raw,
    )
    if not info.width or not info.height:
        idx = info.written_indices()
        if idx:
            arr = load_frame(info, idx[0])
            info.height, info.width = arr.shape
    return info


_RESOLVE_CACHE: dict[tuple, MatteInfo] = {}


def resolve(root, matte_id: str, use_cache: bool = True) -> MatteInfo:
    """Find a matte by id under `root` (contract C6).

    `root` is the `mattes/` directory. Three shapes are accepted, in order:

      1. `root/<id>/`            a flat store, which is what the tests build
      2. `root/<clip-key>/<id>/` the real layout C2 fixes
      3. `matte_id` itself being a path to a matte directory

    Searching rather than requiring the clip key is deliberate: a matte id is
    already unique (it is a hash of the recipe plus the clip identity), and a
    mask component carries the id and its recipe, not the clip key. Making the
    engine reconstruct a clip key it does not otherwise need would be a
    second identity rule to keep in step with the server's.

    Raises MatteMissing when there is no such directory.

    The result is cached against index.json's mtime, because a render resolves
    the same matte once per layer per pass and re-reading a small JSON file is
    not free inside a playback loop.
    """
    if not matte_id:
        raise MatteMissing("no matte id given")
    direct = Path(matte_id)
    if direct.is_dir():
        return info_from_dir(direct)

    root = Path(root) if root is not None else matte_root()
    candidates = [root / matte_id]
    if root.is_dir():
        candidates += sorted(p / matte_id for p in root.iterdir() if p.is_dir())
    for cand in candidates:
        if not cand.is_dir():
            continue
        key = ()
        if use_cache:
            try:
                st = (cand / INDEX_NAME).stat()
                key = (str(cand), st.st_mtime_ns, st.st_size)
                hit = _RESOLVE_CACHE.get(key)
                if hit is not None:
                    return hit
            except OSError:
                key = ()
        info = info_from_dir(cand, matte_id)
        if key:
            _RESOLVE_CACHE[key] = info
            if len(_RESOLVE_CACHE) > 256:
                _RESOLVE_CACHE.pop(next(iter(_RESOLVE_CACHE)))
        return info
    raise MatteMissing(f"no matte {matte_id!r} under {root}")


def forget_cache() -> None:
    """Drop the resolve cache. For tests that rewrite a store in place."""
    _RESOLVE_CACHE.clear()


# --------------------------------------------------------------------------
# frames
# --------------------------------------------------------------------------

def frame_name(index: int) -> str:
    return f"{int(index):0{FRAME_DIGITS}d}.png"


def frame_index(info: MatteInfo, time_s: float) -> int:
    """Which matte frame belongs to a moment (contract C6).

    round(time * fps), clamped into the matte's own range. The rounding is
    floor(x + 0.5) rather than Python's banker's rounding for the same reason
    window_matte spells its rounding out: "round" has to mean one thing in
    the engine, in the server and in the GPU port, and Python's round() sends
    exact halves to even while every other implementation does not.

    fps of 0 (an index that never got written) gives frame 0 rather than a
    divide by nothing.
    """
    fps = float(info.fps or 0.0)
    if fps <= 0:
        return 0
    idx = int(np.floor(float(time_s) * fps + 0.5))
    total = info.total_frames
    if total > 0:
        idx = min(idx, total - 1)
    return max(0, idx)


def frame_path(info: MatteInfo, index: int) -> Path | None:
    """The file for that frame, or None when it is not written yet (C6)."""
    p = info.path / frame_name(index)
    return p if p.is_file() else None


def nearest_written(info: MatteInfo, index: int) -> int | None:
    """The closest written frame to `index`, or None when none exist (C6).

    Ties go to the EARLIER frame: on a partial matte the frames before the
    playhead are the tracked ones and the ones after are guesses, so leaning
    backwards holds the last real answer rather than jumping forward to a
    frame that only exists because a later range was picked.
    """
    idx = info.written_indices()
    if not idx:
        return None
    best = None
    best_d = None
    for i in idx:
        d = abs(i - index)
        if best_d is None or d < best_d or (d == best_d and i < best):
            best, best_d = i, d
    return best


def load_frame(info: MatteInfo, index: int, size: tuple[int, int] | None = None
               ) -> np.ndarray:
    """One matte frame as float32 0..1, shape (h, w) (contract C6).

    `size` is (width, height) to resample to, bilinear, which is what gives
    the soft edge when a matte made at 1280 wide is used on a 3840 wide
    render. Without it the frame comes back at the store's own size.

    Raises MatteMissing when that frame is not on disk. A caller that wants
    the partial-matte fallback asks nearest_written() first; making this
    function silently substitute another frame would hide exactly the thing
    the warnings exist to report.
    """
    p = frame_path(info, index)
    if p is None:
        raise MatteMissing(
            f"matte {info.matte_id}: frame {index} is not written "
            f"({info.written_count} of {info.total_frames} frames on disk)")
    arr = read_gray_png(p)
    full = 65535.0 if arr.dtype == np.uint16 else 255.0
    out = arr.astype(np.float32) / full
    if size is not None:
        out = resize_bilinear(out, int(size[0]), int(size[1]))
    return out


def load_time(info: MatteInfo, time_s: float,
              size: tuple[int, int] | None = None) -> tuple:
    """The matte at a moment, with the fallback applied and reported.

    Returns (array, index_served, warning_or_None). The warning is the
    sentence a caller puts in a `warnings` list: which matte, which frame was
    asked for, which was served. This is the one place the "nearest written
    frame" rule is implemented, so the engine, the server's frame route and
    the CLI all fall back the same way.
    """
    want = frame_index(info, time_s)
    served = want if frame_path(info, want) is not None else nearest_written(info, want)
    if served is None:
        raise MatteMissing(f"matte {info.matte_id} has no frames on disk")
    warn = None
    if served != want:
        warn = (f"matte {info.matte_id}: frame {want} is not tracked yet, "
                f"showing frame {served} ({info.state}, {info.written_count} "
                f"of {info.total_frames} frames)")
    return load_frame(info, served, size), served, warn


def sequence_plan(info: MatteInfo, start_index: int, count: int | None = None
                  ) -> dict:
    """What a render starting at `start_index` can read, and what is missing.

    A render wants a contiguous run of frames starting at the moment it seeks
    to. Three answers are possible and each one is a different ffmpeg input:

      mode "sequence"  frame start_index exists, so the image2 demuxer can
                       read start_index, start_index+1, ... `available` says
                       how many run on contiguously from there. When the run
                       ends early ffmpeg's framesync holds the last frame,
                       which IS the nearest written frame fallback for every
                       frame past the end.
      mode "still"     start_index is not written, so the nearest written
                       frame is used as a single image for the whole render.
                       This is the "static until tracked" state: a pick with
                       one frame lands here.
      mode "none"      nothing is written at all.

    `count` is how many frames the render wants (None means "to the end").
    `missing` is how many of those are not really tracked.
    """
    idx = info.written_indices()
    total = info.total_frames
    want = int(count) if count else max(0, total - int(start_index))
    if not idx:
        return {"mode": "none", "start": int(start_index), "path": None,
                "pattern": str(info.path / PATTERN), "available": 0,
                "missing": want, "wanted": want, "state": info.state,
                "matte_id": info.matte_id}
    start = int(start_index)
    if frame_path(info, start) is not None:
        run = 0
        i = start
        have = set(idx)
        while i in have:
            run += 1
            i += 1
        return {"mode": "sequence", "start": start, "path": None,
                "pattern": str(info.path / PATTERN),
                "available": run, "missing": max(0, want - run), "wanted": want,
                "state": info.state, "matte_id": info.matte_id}
    near = nearest_written(info, start)
    return {"mode": "still", "start": near, "path": info.path / frame_name(near),
            "pattern": str(info.path / PATTERN), "available": 1,
            "missing": max(0, want - 1), "wanted": want,
            "state": info.state, "matte_id": info.matte_id}


# --------------------------------------------------------------------------
# reading a PNG without depending on anything
# --------------------------------------------------------------------------

def resize_bilinear(a: np.ndarray, width: int, height: int) -> np.ndarray:
    """Bilinear resample of a 2D array, sampling at pixel centres.

    Written out rather than pulled from scipy (not a dependency here) or PIL
    (optional), so a matte scaled in Python and the same matte scaled by
    ffmpeg's bilinear scaler land on the same picture to within rounding.
    Pixel centres, not corners: sampling at corners shifts a matte by half a
    pixel per axis, which shows up as a matte that does not sit on its
    subject at the edges of a big upscale.
    """
    ih, iw = a.shape[:2]
    if (iw, ih) == (width, height):
        return a.astype(np.float32, copy=False)
    if width <= 0 or height <= 0:
        raise ValueError(f"bad resize target {width}x{height}")
    x = (np.arange(width, dtype=np.float64) + 0.5) * iw / width - 0.5
    y = (np.arange(height, dtype=np.float64) + 0.5) * ih / height - 0.5
    x0 = np.floor(x).astype(int)
    y0 = np.floor(y).astype(int)
    fx = (x - x0).astype(np.float32)[None, :]
    fy = (y - y0).astype(np.float32)[:, None]
    x0c = np.clip(x0, 0, iw - 1)
    x1c = np.clip(x0 + 1, 0, iw - 1)
    y0c = np.clip(y0, 0, ih - 1)
    y1c = np.clip(y0 + 1, 0, ih - 1)
    src = a.astype(np.float32, copy=False)
    top = src[y0c][:, x0c] * (1.0 - fx) + src[y0c][:, x1c] * fx
    bot = src[y1c][:, x0c] * (1.0 - fx) + src[y1c][:, x1c] * fx
    return top * (1.0 - fy) + bot * fy


def read_gray_png(path) -> np.ndarray:
    """A greyscale PNG as a 2D uint8 (or uint16) array.

    PIL when it is importable, because it is faster and handles every PNG
    variant; the zlib reader below when it is not. Both return the same
    numbers for the 8 bit greyscale frames the matte store holds, which the
    suite asserts rather than assumes.
    """
    path = Path(path)
    try:
        from PIL import Image
    except ImportError:
        return decode_png(path.read_bytes())
    with Image.open(path) as im:
        if im.mode not in ("L", "I;16", "I;16B", "I"):
            im = im.convert("L")
        return np.asarray(im)


def decode_png(data: bytes) -> np.ndarray:
    """A minimal PNG reader: 8 or 16 bit, greyscale or RGB(A), not interlaced.

    Here so this module works in a venv that has numpy and nothing else (the
    SAM service's own environment, a bare CI box). It is not a general PNG
    library and says so by raising rather than guessing: a palette or an
    interlaced file is not something the matte store ever writes.

    A colour PNG comes back as its green channel rather than a luma mix. A
    matte written as RGB is the same value in all three channels, and taking
    one plane is what the ffmpeg path does too (extractplanes=g), so the two
    readers agree exactly instead of to within a rounding of the luma
    coefficients.
    """
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise MatteError("not a PNG")
    pos = 8
    header = None
    idat = bytearray()
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif ctype == b"IDAT":
            idat += body
        elif ctype == b"IEND":
            break
    if header is None:
        raise MatteError("PNG with no IHDR")
    w, h, depth, colour, comp, filt, interlace = header
    if interlace:
        raise MatteError("interlaced PNG: install PIL or write it progressive")
    if colour == 3:
        raise MatteError("palette PNG: the matte store writes greyscale")
    if depth not in (8, 16):
        raise MatteError(f"PNG bit depth {depth} is not supported here")
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[colour]
    bpp = channels * depth // 8
    stride = w * bpp
    raw = zlib.decompress(bytes(idat))
    if len(raw) < (stride + 1) * h:
        raise MatteError("PNG data is short")

    out = np.empty((h, stride), dtype=np.uint8)
    prev = np.zeros(stride, dtype=np.uint8)
    for y in range(h):
        off = y * (stride + 1)
        ftype = raw[off]
        line = np.frombuffer(raw, np.uint8, stride, off + 1).copy()
        if ftype == 0:
            pass
        elif ftype == 2:                      # Up: whole row at once
            line += prev
        elif ftype in (1, 3, 4):
            # Sub, Average and Paeth all read the pixel to the left, which is
            # a value this loop is still computing, so they cannot be
            # vectorised along x. This is the slow path and it is only ever
            # taken when PIL is absent.
            for i in range(stride):
                left = int(line[i - bpp]) if i >= bpp else 0
                up = int(prev[i])
                ul = int(prev[i - bpp]) if i >= bpp else 0
                if ftype == 1:
                    add = left
                elif ftype == 3:
                    add = (left + up) // 2
                else:
                    p = left + up - ul
                    pa, pb, pc = abs(p - left), abs(p - up), abs(p - ul)
                    add = left if (pa <= pb and pa <= pc) else (up if pb <= pc else ul)
                line[i] = (int(line[i]) + add) & 0xFF
        else:
            raise MatteError(f"PNG filter type {ftype} is not valid")
        out[y] = line
        prev = line

    if depth == 8:
        img = out.reshape(h, w, channels)
        return np.ascontiguousarray(img[..., 1] if channels >= 3 else img[..., 0])
    img = out.view(">u2").reshape(h, w, channels)
    return np.ascontiguousarray(img[..., 1] if channels >= 3 else img[..., 0]
                                ).astype(np.uint16)


def write_gray_png(path, arr) -> Path:
    """Write a 2D uint8 array as an 8 bit greyscale PNG, with no dependency.

    The service owns writing real mattes; this exists so a test (and an agent
    building a fixture by hand) can make a store without PIL and without
    shelling out to ffmpeg. Filter type 0 on every row, which is what makes
    the reader above's fast path the one that runs.
    """
    a = np.asarray(arr)
    if a.ndim != 2:
        raise ValueError("write_gray_png wants a 2D array")
    if a.dtype != np.uint8:
        a = np.clip(np.floor(np.asarray(a, dtype=np.float64) * 255.0 + 0.5),
                    0, 255).astype(np.uint8) if a.dtype.kind == "f" else \
            np.clip(a, 0, 255).astype(np.uint8)
    h, w = a.shape
    rows = bytearray()
    for y in range(h):
        rows.append(0)
        rows += a[y].tobytes()

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
           + chunk(b"IEND", b""))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return path
