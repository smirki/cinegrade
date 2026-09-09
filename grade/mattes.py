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
import re
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
# `cancelled` is a matte whose job somebody stopped before it wrote anything
# (tooling gap 26): distinct from `failed`, which is a track that tried and
# could not, and from `partial`, which is a cancel that did leave usable
# frames behind. The service writes it (`sam/server.py`, `_end_job`).
STATES = ("queued", "running", "done", "failed", "stale", "partial",
          "cancelled")

# What a matte id may look like, stated ONCE, for every place an id enters a
# process: a URL segment on the studio's matte routes, a --matte argument on
# the CLI, a grade_client call, and resolve() below.
#
# A matte id is a NAME and never a path. The service mints "m_" plus a 12 hex
# digit digest (sam/backends/base.py's recipe_digest) and a fixture uses a
# short readable name of the same shape, so nothing real needs a separator, a
# leading dot or a 200 character id. The pattern is narrow on purpose rather
# than "whatever a filesystem will accept", because every id is joined onto
# the matte root to make a directory: with no "/" and no "\" in the pattern,
# "../../etc" and "/tmp/anything" cannot be spelled at all, and requiring the
# first character to be alphanumeric rules out "." and ".." twice over.
#
# studio/tools/grade_client.py carries this same string as MATTE_ID_PATTERN
# (it is standalone by design, stdlib plus numpy, and cannot import this
# module), and grade/tests/test_mask_tools.py asserts the two are identical,
# so the copy cannot drift away from this one.
MATTE_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}"
MATTE_ID_RE = re.compile(MATTE_ID_PATTERN)


class MatteError(Exception):
    """A matte that cannot be read at all."""


class MatteMissing(MatteError):
    """No matte with that id under the given root."""


def valid_matte_id(matte_id) -> bool:
    """Whether this is a matte id at all. False for anything path shaped."""
    text = str(matte_id or "")
    if text in (".", ".."):
        return False
    return MATTE_ID_RE.fullmatch(text) is not None


def check_matte_id(matte_id) -> str:
    """The id back, stripped, or MatteMissing saying what was refused.

    MatteMissing rather than a new exception type: every caller already
    handles "there is no such matte" gracefully (the engine turns it into a
    render warning, the studio into a 404), and a string that cannot name
    anything in the store is exactly that case. The message says what an id
    looks like, because the two ways to get here are a typo and a caller
    passing a directory where an id belongs.
    """
    text = str(matte_id or "").strip()
    if not text:
        raise MatteMissing("no matte id given")
    if not valid_matte_id(text):
        raise MatteMissing(
            f"{text!r} is not a matte id: an id is a name like "
            f"m_5214be94f217 (letters, digits, underscore, dot and hyphen, "
            f"up to 64 of them, starting with a letter or a digit), never a "
            f"path. A caller that genuinely holds a matte DIRECTORY calls "
            f"info_from_dir() with it instead")
    return text


def is_under(root, path) -> bool:
    """Whether `path` really sits inside `root`, symlinks resolved.

    Both sides are resolved before the comparison, so a matte directory that
    is a symlink pointing somewhere else is not under the root even though
    the string looks like it is. That is the second half of the id rule: the
    pattern above stops a traversal spelled in the id, this stops one spelled
    on disk, and every caller that is about to read or delete a matte
    directory asks both questions rather than either one.
    """
    try:
        r = Path(root).resolve()
        p = Path(path).resolve()
    except OSError:                                       # pragma: no cover
        return False
    return p == r or r in p.parents


# The content key a matte, a project and a saved grade all file a clip under.
# This is studio/grades.py's key, and it has to be BYTE for byte the same one:
# index.json records the studio's `clip_key`, and the engine compares against
# it (cinegrade.matte_clip_refusal) when a bare CLI render or a bare CLI stats
# is about to use a matte. A second definition that drifted would not refuse
# the wrong matte, it would refuse every matte.
#
# Mirrored rather than imported because grade/ must run with no studio/ beside
# it (studio/grades.py opens studio/data/studio.db on import, which a CLI in
# somebody's footage folder has no business creating). The two are asserted
# identical on real bytes by studio/tests/py/test_projects.py (class
# ClipKeyMirror): a file under a megabyte, a file over two megabytes so the
# head and the tail are hashed separately, a file out of this repository, the
# length going into the digest, and the two constants below. Round 2 finding
# 63: this line used to name test_mask_routes.py and no such test existed in
# that file or anywhere else.
CLIP_KEY_CHUNK = 1024 * 1024
CLIP_KEY_LEN = 32
_clip_key_cache: dict = {}


def clip_key(path) -> str:
    """The content key for a clip file: sha256 of its length, its first
    megabyte and its last megabyte, truncated to 32 hex characters.

    The length goes in first and as a fixed width field, so two files whose
    first megabyte matches cannot collide just because the rest of them is a
    different length. Cached in this process by (realpath, size, mtime), the
    way studio/grades.py caches it, so a `stats` run over ten timestamps reads
    the file once.
    """
    import hashlib                                          # noqa: PLC0415

    rp = os.path.realpath(str(path))
    st = os.stat(rp)
    ck = (rp, st.st_size, st.st_mtime_ns)
    hit = _clip_key_cache.get(ck)
    if hit:
        return hit
    size = st.st_size
    h = hashlib.sha256()
    h.update(size.to_bytes(8, "big"))
    with open(rp, "rb") as fh:
        h.update(fh.read(min(CLIP_KEY_CHUNK, size)))
        if size > CLIP_KEY_CHUNK:
            fh.seek(max(0, size - CLIP_KEY_CHUNK))
            h.update(fh.read(CLIP_KEY_CHUNK))
    key = h.hexdigest()[:CLIP_KEY_LEN]
    _clip_key_cache[ck] = key
    return key


def is_clip_key(value) -> bool:
    """Whether this string is a content key at all: 32 lowercase hex digits.

    The question matters because a matte's recorded `clip_key` is only
    comparable when it IS one. Hand built fixtures in this repo (and any older
    service) file mattes under readable names like `clipA` or `guardkey`, and
    a comparison against those would refuse every one of them. So the engine's
    ownership check asks this first and says nothing about a matte whose key
    it cannot compute an answer for, exactly as the studio route says nothing
    about a matte with no clip_key at all.
    """
    v = str(value or "").strip()
    return (len(v) == CLIP_KEY_LEN
            and all(c in "0123456789abcdef" for c in v))


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

        `cancelled` is in the list for the same reason `failed` is: whatever
        it has on disk, nothing is coming to finish it (tooling gap 26).
        """
        if self.state in ("queued", "running", "partial", "failed", "stale",
                          "cancelled"):
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

    `root` is the `mattes/` directory. Two shapes are accepted, in order:

      1. `root/<id>/`            a flat store, which is what the tests build
      2. `root/<clip-key>/<id>/` the real layout C2 fixes

    Searching rather than requiring the clip key is deliberate: a matte id is
    already unique (it is a hash of the recipe plus the clip identity), and a
    mask component carries the id and its recipe, not the clip key. Making the
    engine reconstruct a clip key it does not otherwise need would be a
    second identity rule to keep in step with the server's.

    An id is ONLY ever an id here (`check_matte_id`), and the directory it
    names has to stay inside `root` (`is_under`). This used to accept a third
    shape, "matte_id is itself a path to a matte directory", and that one line
    made `DELETE /api/matte/<absolute path>` delete any directory on the
    machine: `info_from_dir` builds a MatteInfo for any directory at all, both
    of that route's guards are no-ops with logins off (the documented default
    and the agent case), and the route then rmtree'd `info.path`. A caller that
    genuinely holds a directory rather than an id calls `info_from_dir()`,
    which is what the engine's own fixtures do; nothing in the product ever
    passed a path here.

    Raises MatteMissing when the id is not an id, when there is no such
    directory, and when the directory that matches leaves `root` (a symlink:
    the id can be perfectly well formed and the bytes still be somebody
    else's).

    The result is cached against index.json's mtime, because a render resolves
    the same matte once per layer per pass and re-reading a small JSON file is
    not free inside a playback loop.
    """
    matte_id = check_matte_id(matte_id)

    root = Path(root) if root is not None else matte_root()
    candidates = [root / matte_id]
    if root.is_dir():
        candidates += sorted(p / matte_id for p in root.iterdir() if p.is_dir())
    for cand in candidates:
        if not cand.is_dir():
            continue
        if not is_under(root, cand):
            raise MatteMissing(
                f"matte {matte_id!r} resolves to {cand.resolve()}, which is "
                f"outside the matte store at {Path(root).resolve()}; "
                f"refusing to read it")
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


def served_frame(info: MatteInfo, time_s: float) -> tuple:
    """Which frame answers a moment, and the warning to carry if it is not
    the one asked for. Returns (index_served, warning_or_None).

    The "nearest written frame" rule and its sentence, written once. Split
    out of `load_time` (which now calls it) so a caller that only needs to
    know WHETHER a fallback happened does not have to decode the PNG to find
    out: the composed mask path (`stats` with a `mask` stack) walks several
    components and would otherwise read every frame twice, once for the
    warning and once for the pixels. Costs a couple of `is_file()` checks.
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
    return served, warn


def load_time(info: MatteInfo, time_s: float,
              size: tuple[int, int] | None = None) -> tuple:
    """The matte at a moment, with the fallback applied and reported.

    Returns (array, index_served, warning_or_None). The warning is the
    sentence a caller puts in a `warnings` list: which matte, which frame was
    asked for, which was served. This is the one place the "nearest written
    frame" rule is implemented (in `served_frame` above, which this calls),
    so the engine, the server's frame route and the CLI all fall back the
    same way.
    """
    served, warn = served_frame(info, time_s)
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
# the span a matte really covers, and what happens outside it
# --------------------------------------------------------------------------

def span(info: MatteInfo) -> dict:
    """The window of the clip this matte actually answers for.

    Written frames, not the declared `frames` count: a track queued for
    `--start 0 --end 6` writes frames 0..143 and declares 144, and a track
    that stopped at 259 of 384 declares 384 but answers for 259. Both are
    normal; the number a caller needs before trusting a matte at a moment is
    "which seconds are really tracked", and that is this.

    Outside `[start_s, end_s)` the engines hold the nearest written frame
    (`nearest_written`, and ffmpeg's own framesync past the end of a
    sequence). That is deliberate, not a bug: it is what keeps a correction
    working while a track is still running. It does mean the mask stops
    MOVING out there, so `frozen_outside_span` is stated in every summary
    that carries a span, and every human readable printout says it in words.
    """
    idx = info.written_indices()
    fps = float(info.fps or 0.0)
    first = int(idx[0]) if idx else 0
    last = int(idx[-1]) if idx else -1
    end = last + 1
    return {
        "start_frame": first,
        "end_frame": end,                       # exclusive, like C3's end_frame
        "written": len(idx),
        "declared_frames": int(info.total_frames),
        "contiguous": bool(idx) and (last - first + 1) == len(idx),
        "start_s": round(first / fps, 4) if fps else 0.0,
        "end_s": round(end / fps, 4) if (fps and idx) else 0.0,
        "frozen_outside_span": True,
    }


# --------------------------------------------------------------------------
# per frame quality flags (checkpoint gap 18)
#
# The M8 grader tracked "face", got a matte that lost the face for two
# seconds, latched onto a tree, then tracked the whole person from three
# seconds on, and measured skin luma against it without knowing: nothing in
# `mask list`, `mask show`, the job or the matte route said a word. These
# three rules turn that into something a caller can see before it grades.
#
# The rules, each against the PREVIOUS WRITTEN frame (so a gap in a partial
# matte compares across the gap rather than inventing a break):
#
#   zero_area   the tracked area is 0 inside the span: the subject is gone
#               and the matte holds nothing, which reads as "no correction
#               here" rather than as an error anywhere else.
#   area_jump   the area changed by more than `area_jump` as a FRACTION of
#               the previous frame's area: a face becoming a whole person
#               is a jump of about 1.1, a tree grab is a jump of 13.
#   area_recover  the previous frame held NOTHING and this one holds
#               something: the tracker found an object again after losing
#               it. The area rule cannot see this frame at all, because it
#               divides by the previous area and 0 is not a base to measure
#               a change against, so the frame worth looking at hardest (the
#               one where a tracker most often comes back on the wrong
#               object: the tree, not the face) was the one frame nothing
#               flagged. Round 1 finding 22. It is a POINTER, not a verdict:
#               a track that recovers correctly trips it too, which is why
#               it is a reason of its own rather than another area_jump.
#   low_iou     the mask's overlap with the previous frame fell under
#               `min_iou`: the shape moved somewhere else entirely, which
#               an area test alone misses when the new thing happens to be
#               the same size as the old one.
#
# Defaults measured on the four real mattes under
# bakeoff/masks/studio-data/mattes (M8's own run, 1280x720, 24fps):
#
#   matte     frames  area jump p50 / p90 / max     flags at 0.5
#   sky       144     0.018 / 0.046 / 0.094         0
#   pavement  144     0.028 / 0.095 / 0.781         3 jumps + 15 zeros
#   person    384     0.026 / 0.068 / 13.58         3 jumps (its real
#                                                   t=1.2-2.0s trough)
#   face      144     0.026 / 0.074 / 1.124         1 jump + 44 zeros
#
# so 0.5 flags the face's own latch onto the tree (1.124) and the person's
# genuine occlusion dip, and leaves the sky (max 0.094) alone entirely.
# `min_iou` 0.3 is the shape half of the same call: two masks of the same
# size in different places overlap far below it, while frame to frame
# tracking of one steady object on this footage stays well above it.
# --------------------------------------------------------------------------

SUSPECT_AREA_JUMP = 0.5
SUSPECT_MIN_IOU = 0.3
# The floor both `zero_area` and `area_recover` judge against: an area at or
# under it is "the matte holds nothing". Not configurable, because it is not a
# taste question the way the other two are: 0 is 0.
SUSPECT_AREA_FLOOR = 0.0

# Reasons, spelled once so a caller can switch on them rather than on prose.
SUSPECT_REASONS = ("zero_area", "area_jump", "area_recover", "low_iou")


def quality_thresholds(area_jump=None, min_iou=None) -> dict:
    """The thresholds `quality()` will use, resolved the way `matte_root`
    resolves its own path: an explicit argument, then the environment
    (`CINEGRADE_MATTE_AREA_JUMP`, `CINEGRADE_MATTE_MIN_IOU`), then the
    measured defaults above. Returned as part of every report so a number
    can never be read without the threshold it was judged against.
    """
    def pick(value, env_name, fallback):
        if value is not None:
            return float(value)
        raw = os.environ.get(env_name, "").strip()
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
        return float(fallback)

    return {"area_jump": pick(area_jump, "CINEGRADE_MATTE_AREA_JUMP",
                              SUSPECT_AREA_JUMP),
            # The recovery rule's own number, reported for the same reason the
            # other two are: so a count of 0 can be read against what it was
            # judged by. A frame whose area is over this floor, straight after
            # one at or under it, is a recovery.
            "area_recover": SUSPECT_AREA_FLOOR,
            "min_iou": pick(min_iou, "CINEGRADE_MATTE_MIN_IOU",
                            SUSPECT_MIN_IOU)}


def frame_ious(info: MatteInfo, width: int = 128) -> dict:
    """Intersection over union with the previous written frame, per frame.

    Read off disk, downsampled to `width` first (the shape question is "is
    this the same thing in the same place", which survives a small raster;
    a full 1280x720 read of 384 frames costs about a second and a half and
    buys nothing here). Returns `{index: iou}` for every written frame after
    the first.

    Only called when a caller asks for it (`quality(..., compute_iou=True)`),
    because a list of a clip's mattes must not pay for reading every frame of
    every one of them: the SAM service writes its own `ious` into index.json
    as it tracks, and `quality()` prefers those whenever they are there.

    Round 1 finding 15: this docstring used to say the caller was "`mask show`
    and the strip, one matte at a time", which was not true of any code path
    at the time: nothing in the product passed `compute_iou=True`, so this
    function only ran from the tests covering it directly. It is true now.
    `studio/server.py`'s `_matte_summary` passes `compute_iou=full`, so
    `GET /api/matte/<id>` (and therefore `mask show` and its strip) reads the
    frames off disk when index.json carries no `ious`, and the LIST route
    still does not: one matte can pay for a read of its own frames, a clip
    with four mattes over 384 frames each cannot.
    """
    idx = info.written_indices()
    out: dict[int, float] = {}
    prev = None
    for i in idx:
        try:
            arr = load_frame(info, i)
        except MatteError:
            prev = None
            continue
        h = max(1, round(arr.shape[0] * width / max(1, arr.shape[1])))
        small = resize_bilinear(arr, max(1, int(width)), h) \
            if arr.shape[1] != width else arr
        cur = (small >= 0.5)
        if prev is not None:
            union = float(np.logical_or(prev, cur).sum())
            inter = float(np.logical_and(prev, cur).sum())
            out[int(i)] = round(inter / union, 4) if union > 0 else 1.0
        prev = cur
    return out


def quality(info: MatteInfo, area_jump=None, min_iou=None,
            compute_iou: bool = False, ious=None, limit: int | None = None
            ) -> dict:
    """Per frame quality flags for one matte, and the summary of them.

        {"thresholds": {"area_jump", "min_iou"},
         "checked": 144, "iou_source": "index"|"frames"|"none",
         "suspect_count": 45, "suspect_frames": [...],
         "first_suspect_index": 4, "first_suspect_time": 0.1667,
         "reasons": {"zero_area": 44, "area_jump": 1, "area_recover": 1,
                     "low_iou": 0},
         "truncated": false}

    Each entry in `suspect_frames` is `{"index", "time", "reasons": [...],
    "area", "prev_area", "jump", "iou"}`; `jump` and `iou` are None when
    that rule had nothing to compare against.

    `ious` (a list aligned to `areas`, or a dict of index to value) comes
    from index.json when the SAM service wrote one. `compute_iou` reads the
    frames off disk instead, for a matte tracked before the service started
    writing them. With neither, the IoU rule simply does not run and
    `iou_source` says `"none"`: an absent rule is stated, never silently
    passed. Read `iou_source` before believing a `low_iou` count of 0; that
    is what it is for, and round 1 finding 15 was a whole arc of tests that
    asserted the count without ever asserting the source.

    `limit` caps `suspect_frames` (the counts and the first suspect are
    always the true ones) so a route that lists many mattes cannot answer
    with a hundred rows per matte.
    """
    th = quality_thresholds(area_jump, min_iou)
    fps = float(info.fps or 0.0)
    areas = list(info.areas or [])
    written = set(info.written_indices())

    iou_by_index: dict[int, float] = {}
    iou_source = "none"
    if ious is None:
        ious = info.raw.get("ious")
    if isinstance(ious, dict) and ious:
        iou_by_index = {int(k): float(v) for k, v in ious.items()
                        if v is not None}
        iou_source = "index"
    elif isinstance(ious, (list, tuple)) and any(v is not None for v in ious):
        iou_by_index = {i: float(v) for i, v in enumerate(ious)
                        if v is not None}
        iou_source = "index"
    elif compute_iou:
        iou_by_index = frame_ious(info)
        iou_source = "frames" if iou_by_index else "none"

    # Every frame the matte really answers for: a written frame, or one
    # index.json recorded an area for. The two agree on a healthy matte and
    # the union is the honest set when they do not.
    indices = sorted(written | {i for i, v in enumerate(areas)
                                if v is not None})
    counts = {r: 0 for r in SUSPECT_REASONS}
    flagged: list[dict] = []
    prev_area = None
    for i in indices:
        area = areas[i] if i < len(areas) else None
        reasons = []
        jump = None
        if area is not None:
            if float(area) <= th["area_recover"]:
                reasons.append("zero_area")
            if prev_area is not None and prev_area > th["area_recover"]:
                jump = abs(float(area) - prev_area) / prev_area
                if jump > th["area_jump"]:
                    reasons.append("area_jump")
            elif prev_area is not None and float(area) > th["area_recover"]:
                # The subject came back. `jump` stays None on purpose: there
                # is no previous area to divide by, which is exactly why the
                # area rule above cannot see this frame (finding 22).
                reasons.append("area_recover")
        iou = iou_by_index.get(i)
        if iou is not None and iou < th["min_iou"]:
            reasons.append("low_iou")
        if reasons:
            for r in reasons:
                counts[r] += 1
            flagged.append({
                "index": int(i),
                "time": round(i / fps, 4) if fps else None,
                "reasons": reasons,
                "area": None if area is None else round(float(area), 6),
                "prev_area": None if prev_area is None else round(prev_area, 6),
                "jump": None if jump is None else round(jump, 4),
                "iou": None if iou is None else round(float(iou), 4),
            })
        if area is not None:
            prev_area = float(area)

    first = flagged[0] if flagged else None
    shown = flagged if limit is None else flagged[:max(0, int(limit))]
    return {
        "thresholds": th,
        "checked": len(indices),
        "iou_source": iou_source,
        "suspect_count": len(flagged),
        "suspect_frames": shown,
        "truncated": len(shown) < len(flagged),
        "first_suspect_index": None if first is None else first["index"],
        "first_suspect_time": None if first is None else first["time"],
        "reasons": counts,
    }


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
