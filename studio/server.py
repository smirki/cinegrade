#!/usr/bin/env python3
"""Fixxr Studio: a local browser GUI for the cinegrade pipeline.

Standard library only, so there is nothing to install and nothing to build.
The colour work is not reimplemented here: this process imports cinegrade.py
and calls the same graph builders the CLI calls, so a grade made in the browser
and the same preset rendered from the terminal are the same ffmpeg command.

    content/.venv/bin/python content/studio/server.py --port 7431

Port 7431 is deliberately not a framework default (3000, 5000, 8000, 8080).
This machine runs several local servers at once and a collision would be a real
problem, not a theoretical one.
"""

from __future__ import annotations

import argparse
import email.utils
import getpass
import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from collections import OrderedDict
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

STUDIO = Path(__file__).resolve().parent
CONTENT = STUDIO.parent
# Parent of content/, i.e. the Fixxr-Agent-Workspace checkout: one of the
# three quick-jump shortcuts the merged Clips/Browse tab keeps (browse_roots
# below), alongside home and content/footage.
WORKSPACE = CONTENT.parent
GRADE = CONTENT / "grade"
FOOTAGE = CONTENT / "footage"
REFS = CONTENT / "refs"
PRESETS = GRADE / "presets"
LOOKS = GRADE / "luts" / "looks"
TECHNICAL = GRADE / "luts" / "technical"
OUT = GRADE / "out"
TOOLS = GRADE / "tools"
STUDIO_TOOLS = STUDIO / "tools"
PARITY_REPORT = STUDIO_TOOLS / "parity-results.json"


def _default_cache_dir() -> Path:
    """Where decoded frames, proxies and play segments are cached.

    studio/cache unless somebody says otherwise, which is what the founder's
    long running server uses and what it keeps using. Two overrides, in this
    order:

    1. STUDIO_CACHE_DIR (or --cache-dir, which sets it in main()).
    2. STUDIO_DATA_DIR (or --data-dir): a run that was given its own data
       folder is a test or a throwaway server, and it gets its own cache
       under that folder too. Without this rule every temp server in a build
       shares one 600 file cache with the real one and evicts the frames the
       person at the keyboard is scrubbing through.

    Read at import so the module level SEGMENTS and PROXY paths below are
    already right; main() calls set_cache_dir() again for the flags, which
    rebinds all three.
    """
    explicit = os.environ.get("STUDIO_CACHE_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    data = os.environ.get("STUDIO_DATA_DIR", "").strip()
    if data:
        return Path(data).expanduser().resolve() / "cache"
    return STUDIO / "cache"


CACHE = _default_cache_dir()
STATIC = STUDIO / "static"

sys.path.insert(0, str(GRADE))
import cinegrade as CG  # noqa: E402  (path has to be set first)
# frame_stats moved to grade/stats.py so the CLI (`cinegrade stats`,
# `cinegrade sweep`) and studio/tools/grade_client.py read a frame through
# the same numbers this server does, instead of each carrying its own copy.
# Unchanged in what it returns; only where it lives moved.
from stats import (  # noqa: E402  (same reason)
    frame_stats, bands as stats_bands, decode_image as stats_decode_image,
    StatsError, HUE_FAMILIES, SAT_FLOOR, CLIP_BLACK, CLIP_WHITE,
)

# Checkpoint gap 22: the engine's GENERATED caches (baked layer cubes, the
# window / flat / radial mattes, the Color Slice cube) used to be hardcoded
# under grade/luts/ whatever run was using them. Told here, once, they land
# under this server's own cache dir instead, beside its frames, proxies and
# segments, so a run started with its own --data-dir or --cache-dir keeps its
# generated files with the rest of its evidence and cannot collide with
# another run on the same clip. Nothing already in grade/luts/ is moved or
# deleted; a bare CLI run with nothing set still reads and writes there.
CG.set_cache_root(CACHE)

# studio/ itself, so `import auth` and `import db` resolve no matter how this
# file was invoked. Running it as a script already puts its own folder on
# sys.path, but at whatever index it landed at before GRADE was pushed in
# front, and importing server.py from elsewhere would not put it there at all.
sys.path.insert(0, str(STUDIO))
import auth as AUTH  # noqa: E402  (same reason)
import db as DB      # noqa: E402  (same reason)
import grades as GRADES  # noqa: E402  (same reason)
import library as LIB  # noqa: E402  (same reason)
import projects as PROJECTS  # noqa: E402  (same reason)
import render_gpu as RG  # noqa: E402  (same reason)
import sam_client as SAMC  # noqa: E402  (same reason; the SAM service client, C3/C4)
import mattes as MT  # noqa: E402  (same reason; grade/mattes.py, contract C6, owned by M2)

# render_gpu drives ffmpeg and the browser worker through this module's own
# helpers (clip_info, scale_for_preview, Job). Handing it the module object is
# how it gets them: this file runs as __main__, so an "import server" inside
# render_gpu would load a SECOND copy with its own JOBS dict and its own
# caches. Binding early is safe because it only stores the object; the
# attributes it reads are looked up when a render starts, long after this file
# has finished executing.
RG.bind(sys.modules[__name__])

# Every cached frame is the output of this exact engine file, so its hash
# belongs in the cache key. Without it, editing cinegrade.py leaves the studio
# serving frames rendered by the previous version, and the picture silently
# disagrees with the code: a change looks like it did nothing, or a fix looks
# like it failed. Cheap because it is read once at import.
ENGINE_HASH = hashlib.sha1(
    (GRADE / "cinegrade.py").read_bytes()).hexdigest()[:12]

VIDEO_EXT = {".mov", ".mp4", ".m4v", ".mxf", ".mkv"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}

# One ffmpeg per core is not the win it looks like: ffmpeg already threads a
# 4K ProRes decode across cores, so more than a few at once just makes every
# preview slower. Three keeps a scrub responsive while thumbnails fill in.
FFMPEG_SLOTS = threading.Semaphore(3)

# Frames on disk are ~1.5MB each at 960 wide. Keeping a few hundred is cheap
# and makes flipping between two configs instant, which is the whole point of
# an A/B slot.
CACHE_MAX_FILES = 600

# A decoded source frame is uint16 x 3 channels, uncompressed: at the studio's
# 960 wide default preview (3840x2160 source -> 960x540) that is
# 960*540*3*2 = 3,110,400 bytes, about 3.0MB. SRC_MEM_MAX=48 caps the
# in-process LRU at roughly 48 * 3.0MB =~ 144MB, which is a small slice of a
# laptop's RAM and covers far more distinct timecodes than one scrub session
# actually revisits. CACHE_SRC_MAX_FILES=300 on disk is the same ~3MB-each
# arithmetic at a bigger, cheaper budget (about 900MB), matching the order of
# magnitude the existing CACHE_MAX_FILES JPEG cache already commits to disk.
SRC_MEM_MAX = 48
CACHE_SRC_MAX_FILES = 300

# Upload caps, both overridable from the command line (see main()). 8 GiB
# covers a single 4K ProRes clip several minutes long; 50 GiB is a generous
# per-account total before an account has to clear something out before
# adding more. Neither is enforced anywhere except this server: an account
# with shell access to the machine can always put a bigger file straight
# into a footage folder.
UPLOAD_MAX_BYTES = 8 * 1024 ** 3
UPLOAD_QUOTA_BYTES = 50 * 1024 ** 3

# A JSON request body over this is refused before it is even read (see
# _body() below). 8MB is far more than any config patch this app sends; the
# one route that legitimately moves more bytes than that is /api/upload,
# which reads the raw socket itself and never calls _body().
BODY_MAX_BYTES = 8 * 1024 * 1024

# A LUT (.cube) body over this is refused before it is read, same rule as
# BODY_MAX_BYTES above but for the one route that reads the raw socket with
# _raw_body() instead: POST /api/look, which imports a .cube file as plain
# text and never calls _body(). A 129 point cube (129**3 lines of "r g b"
# floats) is about 35MB as text, so 64MB comfortably covers the largest LUT
# anyone imports while still refusing an unbounded body.
LOOK_MAX_BYTES = 64 * 1024 * 1024

_probe_cache: dict[tuple[str, bool], dict] = {}
_probe_lock = threading.Lock()

# LRU of decoded, normalised source frames, keyed by (clip, time, width,
# autorotate). This is the expensive half of a preview (measured ~0.22s of a
# ~0.51s knob turn) and it does not depend on any grade parameter, so caching
# it here means changing exposure or contrast only re-runs the filter graph,
# not the ProRes decode.
_src_mem: "OrderedDict[str, tuple[np.ndarray, dict]]" = OrderedDict()
_src_mem_lock = threading.Lock()

# Per-client generation counter. A fast scrub fires a request per frame; the
# stale ones are dropped before they reach ffmpeg rather than after, which is
# the difference between a queue that drains and one that grows.
_latest_gen: dict[str, int] = {}
_gen_lock = threading.Lock()


class StudioError(Exception):
    """A bad request the user can fix. Answered with 400, not a stack trace."""


class HttpError(Exception):
    """A request problem that is not a plain 400: a size cap (413), a missing
    header (400 too, but raised from a place that is not asking for a
    StudioError's fixed code). Kept separate from AUTH.AuthError only
    because that one belongs to the auth module, not to plumbing that has
    nothing to do with who is signed in.
    """

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------
# paths and safety
# --------------------------------------------------------------------------

def safe_name(name: str) -> str:
    """Reject anything that could climb out of the folder it belongs to."""
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise StudioError(f"bad name: {name!r}")
    return name


# Clips opened from outside content/footage, keyed by the display name the rest
# of the app already passes around. Everything downstream (frame, stats, scope,
# thumb, render) identifies a clip by a bare name, so rather than teach every
# one of those to accept an absolute path, an opened file is registered here
# once and then behaves exactly like a file that was sitting in footage/. It is
# deliberately in memory only: a restart should not silently keep reaching into
# folders the user browsed to in an earlier session.
EXTERNAL_CLIPS: dict[str, Path] = {}


def clip_path(name: str) -> Path:
    ext = EXTERNAL_CLIPS.get((name or "").strip())
    if ext is not None:
        if not ext.exists():
            raise StudioError(f"clip no longer on disk: {ext}")
        return ext
    p = FOOTAGE / safe_name(name)
    if not p.exists():
        raise StudioError(f"clip not found: {name}")
    return p


def resolve_clip(name: str) -> tuple[str, str, str]:
    """(content key, display name, path) for a clip name, for projects.py.

    projects.py is handed this rather than working it out itself because clip
    resolution is this module's job: a clip opened from outside content/footage
    exists only in EXTERNAL_CLIPS, in memory, and a project must still be able
    to find it while the server is up.
    """
    p = clip_path(name)
    return GRADES.clip_key(p), str(name).strip(), str(p)


PROJECTS.bind_resolver(resolve_clip)


def library_probe(path: str) -> dict | None:
    """Duration and frame size for one file in a library listing.

    Bound into library.py rather than reimplemented there: this file already
    owns every ffprobe call the studio makes. Best effort by design, because a
    file listing must not fail because one clip in the folder is half
    uploaded or corrupt; those come back with null duration and size and are
    still listed.

    clip_info() is not used here on purpose. It takes a clip NAME, and giving
    every file in every library folder a name would fill EXTERNAL_CLIPS (and
    therefore the old Clips list) just because somebody opened a folder.
    """
    try:
        info = CG.probe(str(path), rotation="auto")
    except Exception:                                          # noqa: BLE001
        return None
    return {"duration": float(info.get("duration") or 0.0),
            "width": int(info.get("width") or 0),
            "height": int(info.get("height") or 0)}


LIB.bind(probe=library_probe, video_ext=VIDEO_EXT)


def register_external_clip(raw: str) -> str:
    """Take an absolute path to a video file and give it a stable display name.

    The name has to survive safe_name(), which rejects separators, so a file
    outside footage/ cannot be addressed by its path. Two different folders can
    hold the same basename, so a colliding name gets a short digest of its
    parent appended rather than silently shadowing the earlier file.
    """
    given = Path(raw).expanduser()
    try:
        p = given.resolve(strict=True)
    except OSError as exc:
        raise StudioError(f"cannot open {raw}: {exc}") from exc
    if not p.is_file():
        raise StudioError(f"not a file: {p}")
    if p.suffix.lower() not in VIDEO_EXT:
        raise StudioError(f"not a video this tool reads: {p.name}")
    # Two ways of already being in the footage folder: the file itself, and a
    # symlink to it sitting in that folder, which is how --footage lets a test
    # run point at a temp folder without copying gigabytes. The second is
    # tested with the folder chain resolved but the last name left alone, so
    # the clip keeps the name the clip list shows it under.
    here = Path(os.path.realpath(str(given.parent))) / given.name
    for cand in (p, here):
        try:
            rel = cand.relative_to(FOOTAGE.resolve())
        except ValueError:
            continue
        # Already reachable the normal way, so do not create a second identity
        # for it: the clip list would then show the same file twice.
        return rel.name
    name = p.name
    if EXTERNAL_CLIPS.get(name, p) != p or (FOOTAGE / name).exists():
        digest = hashlib.sha1(str(p.parent).encode()).hexdigest()[:6]
        name = f"{p.stem}~{digest}{p.suffix}"
    EXTERNAL_CLIPS[name] = p
    return name


def _dir_total_bytes(d: Path) -> int:
    """Sum of file sizes directly inside a folder. Flat, never recursive.

    This WAS the upload quota check, back when a footage folder had no
    subfolders in it. The library arc gave people folders, so the quota moved
    to library.tree_bytes(), which walks the whole tree: a flat sum would be
    walked around by putting the next hundred gigabytes one level down. Kept
    here because studio/static/limits.js names this function in an honesty
    entry, and a name that no longer exists is worse than one that is no
    longer on the hot path.
    """
    total = 0
    if d.exists():
        for p in d.iterdir():
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    return total


def probe_is_video(path: Path) -> bool:
    """A bounded, headers-only check that a freshly uploaded file actually
    decodes as a video, not just a file with a video-looking extension.

    -show_entries limited to codec_type means ffprobe reads container/stream
    headers and stops; it does not decode a single frame. The timeout is the
    real bound: a corrupt or hostile file that makes ffprobe hang instead of
    exiting quickly must not be able to tie up the request thread forever.
    """
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return False
    if proc.returncode != 0:
        return False
    try:
        streams = json.loads(proc.stdout or "{}").get("streams") or []
    except json.JSONDecodeError:
        return False
    return any(s.get("codec_type") == "video" for s in streams)


# Somewhere useful to start browsing from, rather than dropping the user at /
# and making them click down through five levels of system folders.
def browse_roots(user: dict | None = None) -> list[dict]:
    """Three quick-jump shortcuts, down from the old wall of nine root
    buttons (job: file browser, "so many buttons"). Root itself is
    deliberately not one of these: the client's breadcrumb always starts
    with a "/" segment for whatever folder is open, so root -- and from
    there /Volumes, /Applications, anywhere else on the Mac -- is already one
    click away without needing its own dedicated shortcut here too.
    """
    if AUTH.enabled():
        # With logins on, home and workspace are outside what this account is
        # allowed to open, so offering them as shortcuts would be offering
        # buttons that answer 403. The account's own folder takes their place.
        roots = [{"label": "footage", "path": str(FOOTAGE)}]
        if user and user.get("id"):
            roots.append({"label": "my footage",
                          "path": str(AUTH.user_footage(user["id"]))})
        return roots
    return [{"label": "home", "path": str(Path.home())},
            {"label": "workspace", "path": str(WORKSPACE)},
            {"label": "footage", "path": str(FOOTAGE)}]


def browse_dir(raw: str | None, user: dict | None = None) -> dict:
    """List one directory: subfolders, plus only the videos this tool can read.

    There is no path allow-list here on purpose. The user asked to navigate
    anywhere on their own Mac, and the server is bound to 127.0.0.1, so the
    only caller is the browser on this machine, which the user already controls.
    Unreadable folders return an `error` string instead of raising, so a
    permission-protected folder shows a message in the panel rather than
    breaking the whole browser.
    """
    p = Path(raw).expanduser() if raw else FOOTAGE
    try:
        p = p.resolve()
    except OSError as exc:
        raise StudioError(f"bad path: {exc}") from exc
    if not p.is_dir():
        p = p.parent if p.parent.is_dir() else Path.home()
    if AUTH.enabled():
        # The docstring above explains why there is no allow-list when the
        # server is on 127.0.0.1 with no accounts: the only caller is the
        # user's own browser on their own machine. Turn logins on and that
        # stops being true, so the same endpoint becomes a confined browser
        # over the project footage plus this account's own folder. It raises
        # a 403 rather than quietly substituting a different folder, because
        # silently showing somewhere else is how a user comes to believe the
        # confinement is not there.
        p = AUTH.confine(p, AUTH.allowed_roots(user, FOOTAGE))
    dirs, files, error = [], [], ""
    try:
        for entry in sorted(p.iterdir(), key=lambda e: e.name.lower()):
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    dirs.append({"name": entry.name, "path": str(entry)})
                elif entry.suffix.lower() in VIDEO_EXT:
                    # key is the content identity a grade is stored under
                    # (contract C3). grades.py caches it by (realpath, size,
                    # mtime) in memory and in SQLite, so only the first
                    # listing of a folder pays the 2 MiB read per file, and
                    # safe_key gives "" for a file it cannot read rather than
                    # failing the whole listing.
                    files.append({"name": entry.name, "path": str(entry),
                                  "bytes": entry.stat().st_size,
                                  "key": GRADES.safe_key(entry)})
            except OSError:
                continue                       # a broken symlink is not an error
    except PermissionError:
        error = f"no permission to read {p}"
    except OSError as exc:
        error = f"{exc}"
    return {"path": str(p),
            "parent": str(p.parent) if p.parent != p else "",
            "roots": browse_roots(user), "dirs": dirs, "files": files,
            "error": error}


def set_cache_dir(path) -> Path:
    """Point the frame cache somewhere other than studio/cache.

    Three module globals name a piece of that folder (CACHE itself, SEGMENTS
    for play segments, PROXY for proxy encodes) and the last two are computed
    from CACHE at import, so all three are rebound here. Everything else
    reads CACHE at call time, which is why one assignment is enough for the
    rest of the file.

    STUDIO_CACHE_DIR is set as well so a child process (the GPU render's
    browser worker, a tool imported from this module) lands in the same
    place rather than quietly falling back to studio/cache.
    """
    global CACHE, SEGMENTS, PROXY, MASK_PROXY, MASK_FRAME_CACHE, MASK_PICK_CACHE
    CACHE = Path(path).expanduser().resolve()
    SEGMENTS = CACHE / "segments"
    PROXY = CACHE / "proxy"
    # Masks (C4): rebound here for the same reason SEGMENTS and PROXY are, so
    # a test server pointed at its own cache dir never touches another run's
    # plain proxies or pick previews.
    MASK_PROXY = CACHE / "mask_proxy"
    MASK_FRAME_CACHE = CACHE / "mask_frame"
    MASK_PICK_CACHE = CACHE / "mask_pick"
    os.environ["STUDIO_CACHE_DIR"] = str(CACHE)
    # And the engine's own generated caches (gap 22), for the same reason the
    # three globals above are rebound: --cache-dir has to move every file this
    # process bakes, not only the ones this file names.
    CG.set_cache_root(CACHE)
    return CACHE


def set_footage(path) -> None:
    """Point the shared footage folder somewhere other than content/footage.

    There for the test harness. The harness runs with logins OFF, and with
    logins off the library's `mine` root IS the shared footage folder, so
    --data-dir alone does not isolate a test run: a spec that uploads a clip
    or makes a folder writes into the founder's real footage. This gives the
    harness a temp folder of symlinks to point at instead.

    Three modules keep their own name for the folder (this one, library.py
    and projects.py) and every one of them reads it at call time, so setting
    all three here is the whole change; nothing captured it at import.
    """
    global FOOTAGE
    FOOTAGE = Path(path).expanduser().resolve()
    FOOTAGE.mkdir(parents=True, exist_ok=True)
    LIB.FOOTAGE = FOOTAGE
    PROJECTS.FOOTAGE = FOOTAGE


def git_version() -> str:
    """The short commit this tree is on, or "unknown".

    Read out of the .git folder rather than by running git: this is answered
    inside a request, and starting a subprocess per health check would make
    the cheapest route the most expensive one. Three files, in the order git
    itself would look: HEAD (a ref or a detached sha), the loose ref file,
    then packed-refs for a ref that has been packed away.

    Never raises. A tarball with no .git, a worktree, a half written HEAD
    during a rebase: all of them are "unknown", which is the honest answer.
    """
    try:
        git = CONTENT / ".git"
        if git.is_file():
            # A worktree or a submodule: .git is a file saying gitdir: PATH.
            line = git.read_text(errors="replace").strip()
            if line.startswith("gitdir:"):
                git = Path(line.split(":", 1)[1].strip())
                if not git.is_absolute():
                    git = (CONTENT / git).resolve()
        head = (git / "HEAD").read_text(errors="replace").strip()
        if not head.startswith("ref:"):
            return head[:7] if head else "unknown"
        ref = head.split(":", 1)[1].strip()
        loose = git / ref
        if loose.is_file():
            return loose.read_text(errors="replace").strip()[:7] or "unknown"
        packed = git / "packed-refs"
        if packed.is_file():
            for row in packed.read_text(errors="replace").splitlines():
                if row.startswith("#") or " " not in row:
                    continue
                sha, name = row.split(" ", 1)
                if name.strip() == ref:
                    return sha.strip()[:7] or "unknown"
    except Exception:                                         # noqa: BLE001
        pass
    return "unknown"


def clip_count() -> int:
    """How many clips GET /api/state would list, without probing any of them.

    A directory listing and a dict length, so GET /api/health stays the cheap
    call it is meant to be: _clips() runs ffprobe twice per file.
    """
    n = 0
    try:
        if FOOTAGE.exists():
            for p in FOOTAGE.iterdir():
                if p.suffix.lower() in VIDEO_EXT and p.is_file():
                    n += 1
    except OSError:
        pass
    return n + sum(1 for p in EXTERNAL_CLIPS.values() if p.exists())


def ensure_dirs() -> None:
    # DB.DATA and AUTH.USERS_DIR are studio/data and studio/data/users: the
    # accounts database and each account's own footage folder live there, and
    # the whole folder is gitignored because it is user owned data.
    for d in (CACHE / "frames", CACHE / "img", CACHE / "thumbs", CACHE / "refs",
              CACHE / "src", CACHE / "segments", CACHE / "proxy", CACHE / "grain",
              CACHE / "mask_proxy", CACHE / "mask_frame", CACHE / "mask_pick",
              STUDIO_TOOLS, OUT, PRESETS, LOOKS,
              DB.DATA, AUTH.USERS_DIR, MT.matte_root()):
        d.mkdir(parents=True, exist_ok=True)
    # Per clip grades keep their own tables in the same SQLite file the
    # accounts use. Created here rather than lazily on the first request, so a
    # data folder that cannot be written fails at boot with a clear traceback
    # instead of inside a request somebody is waiting on.
    GRADES.init_schema()
    # Same story for the project tables (contract C1). Additive: the accounts
    # and the old grades table are untouched, and a database made by an older
    # build gains the new tables here on its first boot.
    PROJECTS.init_schema()
    # And the library tables (contract E1): teams, shares and the activity
    # feed, plus (through db.init_schema, which this calls first) the default
    # org and the users.org_id column. Additive in the same way: a database
    # from before this arc gains the tables and the column, and loses nothing.
    LIB.init_schema()
    # User 0 is the local no-login account. Its preset folder exists from boot
    # so a "save as" with logins off has somewhere of its own to land instead
    # of writing into the shipped, checked in library.
    GRADES.user_presets_dir(0)
    # A ".tmp" left in segments/ is a playback render a previous server
    # process was still streaming (and writing to disk for the cache) when
    # it stopped; nothing will ever finish it, so unlike everything else in
    # that folder it is not a cache entry, just leftover.
    for p in (CACHE / "segments").glob("*.tmp"):
        p.unlink(missing_ok=True)
    # Same story one folder over, with one hard-won difference. A ".partial"
    # in proxy/ is a proxy encode (see the proxy section below) that was
    # still being written when a process stopped, but this cache is SHARED
    # with any other studio process on the machine, and several run at once
    # during development. Deleting one unconditionally at boot kills an
    # encode another process is writing right now: ffmpeg then fails at the
    # faststart step with "unable to re-open output file for shifting data",
    # which is exactly how this was found (two 85 second encodes, both lost
    # at the last second to an unrelated server starting up). So age is the
    # test, not existence: an in-flight encode's file is being appended to
    # continuously, so its mtime is always recent.
    for p in (CACHE / "proxy").glob("*.partial"):
        try:
            if time.time() - p.stat().st_mtime > 3600:
                p.unlink(missing_ok=True)
        except OSError:
            pass


# --------------------------------------------------------------------------
# rotation
# --------------------------------------------------------------------------

def _project_rotation(user_id) -> str | None:
    """The rotation of the project this account has open, or None.

    Imported lazily and defensively: the project store is a separate module
    and this server has to keep working the same way when it is absent, half
    written, or holding a database it cannot open. Any failure here means
    "no project", which lands on the same "auto" default the studio has
    always used.
    """
    if user_id is None:
        return None
    try:
        import projects as PROJECTS                          # noqa: PLC0415
        return PROJECTS.open_rotation(user_id)
    except Exception:                                        # noqa: BLE001
        return None


def effective_rotation(payload_or_query, user_id=None) -> str:
    """The rotation one request means, from the five places it can come from.

    In order: an explicit "rotation" field, then the legacy autorotate flag
    (the POST body's "autorotate" and the query string's "rot", where true or
    1 means "auto" and false or 0 means "0"), then a non-auto "rotation" on
    the request's own "config" (contract G4: rotation is part of a saved
    grade, so a caller that hands this request a grade or a preset whole,
    with no top level rotation field of its own, still gets that grade's own
    rotation rather than falling through to whatever this account's open
    project or the file's own tag says), then the rotation stored on the
    project this account has open, then "auto" (which is what leaves the
    file's own display matrix tag in charge, unchanged).

    The legacy flag is not deprecated-and-ignored: the browser still sends it
    on every request, and a CLI script written before rotation existed still
    means what it meant. It only loses to an explicit rotation field.
    """
    src = payload_or_query if isinstance(payload_or_query, dict) else {}
    for key in ("rotation", "rot", "autorotate"):
        if src.get(key) not in (None, ""):
            return CG.normalise_rotation(src[key])
    config = src.get("config")
    if isinstance(config, dict) and config.get("rotation") not in (None, ""):
        mode = CG.normalise_rotation(config["rotation"])
        if mode != "auto":
            return mode
    return CG.normalise_rotation(_project_rotation(user_id))


# --------------------------------------------------------------------------
# probe
# --------------------------------------------------------------------------

def clip_info(name: str, rotation) -> dict:
    """probe() one clip at one rotation, cached.

    rotation is the new string form; a bare boolean still works and means
    what autorotate meant, so callers outside this file (the GPU render, the
    audit tools) did not have to move in the same commit.
    """
    rotation = CG.normalise_rotation(rotation)
    key = (name, rotation)
    with _probe_lock:
        if key in _probe_cache:
            return dict(_probe_cache[key])
    path = clip_path(name)
    info = CG.probe(str(path), rotation=rotation)
    info["name"] = name
    info["path"] = str(path)
    info["fps"] = _fps(str(path))
    info["duration"] = float(info.get("duration") or 0.0)
    with _probe_lock:
        _probe_cache[key] = info
    return dict(info)


def _fps(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=avg_frame_rate,r_frame_rate",
         "-of", "json", path], capture_output=True, text=True).stdout
    try:
        st = json.loads(out)["streams"][0]
    except Exception:
        return 24.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        val = st.get(key) or ""
        if "/" in val:
            num, den = val.split("/")
            if float(den) > 0 and float(num) > 0:
                return float(num) / float(den)
    return 24.0


# --------------------------------------------------------------------------
# config helpers
# --------------------------------------------------------------------------

def full_config(cfg: dict | None) -> dict:
    """Fill in everything the caller left out, so the graph builders are safe.

    CG.migrate_layers runs FIRST, on the config as it arrived, because that is
    the shape a pre-layers preset, saved grade or API caller still sends: a
    `secondary` and a `window` block and no `layers`. Merging with DEFAULTS
    first would supply an empty `layers` and make every old config look like a
    new one, and the old grade would silently render as no grade at all. The
    file or database row is not rewritten; the migration is on read, and the
    next save writes the new shape.
    """
    return CG.deep_merge(CG.DEFAULTS, CG.migrate_layers(cfg or {}))


def config_diff(cfg: dict, base: dict | None = None) -> dict:
    """Only what differs from the defaults.

    Presets in this repo are written as partial overrides and deep-merged, and
    a saved preset that spells out all sixty values would be unreadable next to
    them. It also means a later change to a default is inherited rather than
    frozen into every file.
    """
    base = CG.DEFAULTS if base is None else base
    out = {}
    for key, value in cfg.items():
        if key.startswith("_"):
            out[key] = value
            continue
        if key not in base:
            out[key] = value
            continue
        ref = base[key]
        if isinstance(value, dict) and isinstance(ref, dict):
            sub = config_diff(value, ref)
            if sub:
                out[key] = sub
        elif value != ref:
            out[key] = value
    return out


# Parameters measured in pixels. A preview is a smaller frame, so these have to
# shrink with it or a 26 pixel halation on a 960 wide preview reads four times
# wider than the same number does on the 3840 wide render, and the user grades
# against a glow that will not be there in the export.
def scale_for_preview(cfg: dict, factor: float) -> dict:
    if factor >= 0.999:
        return cfg
    out = deepcopy(cfg)
    out["fx"]["halation"]["sigma"] = float(cfg["fx"]["halation"]["sigma"]) * factor
    out["fx"]["bloom"]["sigma"] = float(cfg["fx"]["bloom"]["sigma"]) * factor
    out["fx"]["radial_blur"]["sigma"] = float(cfg["fx"]["radial_blur"]["sigma"]) * factor
    # rgb_split is the one spatial effect that cannot scale down honestly: it is
    # a whole pixel channel shift, so at a 960 preview of a 3840 source the
    # correct scaled amount for the shipped default (1.6) is 0.4 of a pixel,
    # which build_fx rounds to 0 and skips entirely. The slider then did nothing
    # in the preview while the render still split the channels, which is the
    # worst kind of preview error: it hides an effect that is really there.
    # Rounding up to one pixel instead overstates the shift by up to 4x at a
    # quarter scale preview, but it is the only choice that keeps a real effect
    # visible, and the schema tooltip says so rather than leaving the user to
    # discover the discrepancy in a finished render.
    amt = float(cfg["fx"]["rgb_split"]["amount"])
    out["fx"]["rgb_split"]["amount"] = max(1.0, amt * factor) if amt >= 0.5 else amt * factor
    out["detail"]["soften"] = float(cfg["detail"]["soften"]) * factor
    # Grain size is a plate downscale factor, so it shrinks the same way. It is
    # an integer and bottoms out at 1, which is why preview grain looks coarser
    # than the render whenever size * factor drops below 1.
    out["grain"]["size"] = max(1, int(round(float(cfg["grain"]["size"]) * factor)))
    return out


def flat_config(cfg: dict, keep_exposure: bool) -> dict:
    """The "before" frame: the technical conversion and nothing creative.

    Tone map, working space and output encode are copied from the live config
    because those are the conversion, not the grade. Changing them changes what
    "ungraded" even means, so the comparison would be dishonest otherwise.
    """
    flat = deepcopy(CG.DEFAULTS)
    # `input` is copied for the same reason: it says what the source IS, so a
    # bypass frame that decoded it differently from the graded frame would be
    # comparing two different pictures rather than a grade against its start.
    # It is read with a default so a config saved before inputs existed, which
    # carries no convert.input at all, still lands on "auto".
    for key in ("tonemap", "working_space", "encode", "input"):
        flat["convert"][key] = cfg["convert"].get(key, flat["convert"][key])
    if keep_exposure:
        flat["convert"]["exposure"] = cfg["convert"]["exposure"]
    return flat


def mask_preview_config(cfg: dict, index: int | None = None) -> dict:
    """Show one layer's matte with nothing painted on top of it.

    `index` is the layer to show, defaulting to the first enabled one. With no
    layers at all there is no matte to show and the config comes back with
    only the FX and grain stripped.

    The power window is deliberately LEFT ON. What a layer actually selects is
    its colour matte multiplied by its window matte, so a matte view that
    ignored the shape would show a selection the grade will never make. The
    engine handles the multiply: in mask.show mode the maskedmerge composites
    the greyscale colour matte over black instead of over the picture, so the
    two mattes come out multiplied together.

    Layers EARLIER in the pipeline stay on, because they are part of the
    signal the key really sees and turning them off would show a selection the
    grade does not make either. Layers later in the pipeline are turned off,
    since they would paint over the matte. The look is killed for the same
    reason, unless the shown layer runs after it, in which case the look is
    part of what the key sees and the layer replaces the picture afterwards.
    """
    out = deepcopy(cfg)
    layers = out.get("layers") or []
    shown = None
    if layers:
        if index is None:
            index = next((i for i, ly in enumerate(layers) if ly.get("enabled")), 0)
        index = max(0, min(int(index), len(layers) - 1))

        def rank(i):
            ly = CG.deep_merge(CG.LAYER_DEFAULTS, layers[i] or {})
            return (1 if ly["placement"] == "after_look" else 0, i)

        here = rank(index)
        layers[index] = CG.deep_merge(CG.LAYER_DEFAULTS, layers[index] or {})
        layers[index]["mask"]["show"] = True
        shown = layers[index]
        for i, layer in enumerate(layers):
            if i != index and rank(i) > here:
                layer["enabled"] = False
    if not (shown and shown["placement"] == "after_look"):
        out["look"]["lut"] = None
        if "lut2" in out["look"]:
            out["look"]["lut2"] = None
    for name in out["fx"]:
        out["fx"][name]["enabled"] = False
    out["grain"]["enabled"] = False
    out["detail"] = {"soften": 0.0, "sharpen": 0.0}
    return out


# --------------------------------------------------------------------------
# frame rendering
# --------------------------------------------------------------------------

def _cache_path(kind: str, key: str, ext: str) -> Path:
    return CACHE / kind / f"{key}.{ext}"


def _prune_cache(kind: str, max_files: int = CACHE_MAX_FILES) -> None:
    """Oldest-mtime eviction. Tolerant of a file disappearing mid-listing:
    a second server pointed at this same studio/cache, or a concurrent
    request in this one, can unlink an entry between glob() and stat() here
    (or between this prune and a reader elsewhere reading the same file), so
    a file stat() can no longer see is treated as already gone rather than
    raising and failing the request that happened to trigger this prune.
    """
    d = CACHE / kind
    dated = []
    for p in d.glob("*"):
        try:
            dated.append((p.stat().st_mtime, p))
        except FileNotFoundError:
            continue
    dated.sort(key=lambda t: t[0])
    for _, p in dated[:-max_files]:
        try:
            p.unlink()
        except OSError:
            pass


def _preview_dims(info: dict, width: int) -> tuple[int, int]:
    """The clamped preview width and its even-height partner.

    Shared by source_frame and render_raw so a cached source frame and the
    grade pass that consumes it always agree on WxH; computing this twice with
    even a one-pixel drift would make the rawvideo pipe between them
    misinterpret the buffer.
    """
    width = max(160, min(int(width), int(info["width"])))
    factor = width / float(info["width"])
    height = max(2, int(round(info["height"] * factor / 2)) * 2)
    return width, height


def source_frame(clip: str, time_s: float, width: int,
                 rotation) -> tuple[np.ndarray, dict]:
    """The decoded, downscaled, range/matrix-normalised source frame, cached.

    This is exactly the prefix every grade graph used to start with: the
    studio's own scale, followed by the normalisation half of
    cinegrade.f_log_stage (scale=in_color_matrix=...,format=gbrp16le). None of
    that depends on a single grade parameter, only on the clip, timecode,
    preview width and rotation, so a knob turn on an already-viewed frame
    never has to touch ffmpeg's decoder again. Cached in memory (LRU, see
    SRC_MEM_MAX above) and on disk under cache/src/, pruned the same way the
    other caches are.

    Returns uint16 rgb48le-shaped data: (H, W, 3), R G B per pixel. The array
    is read-only (a direct view over the cached bytes) so every caller can
    share the one decode without risking one of them corrupting it for the
    others.
    """
    rotation = CG.normalise_rotation(rotation)
    info = clip_info(clip, rotation)
    width, height = _preview_dims(info, width)
    # The decode matrix is part of the identity of the cached bytes, not a
    # constant: an ordinary bt709 delivery file and an Apple Log clip normalise
    # differently, so leaving it out of the key would serve one clip's frame
    # decoded by the other's rule.
    matrix = CG.source_matrix(info)
    # The rotation goes in the key as its own string, not as the old boolean:
    # "auto" and "0" and "90" are three different pictures, and a key that
    # could only say True or False would hand a rotated request a frame
    # decoded for a different one.
    mem_key = f"{clip}|{round(float(time_s), 4)}|{width}|{rotation}|{matrix}"

    with _src_mem_lock:
        hit = _src_mem.get(mem_key)
        if hit is not None:
            _src_mem.move_to_end(mem_key)
            arr, meta = hit
            return arr, dict(meta)

    disk_key = hashlib.sha1(mem_key.encode()).hexdigest()
    disk_path = _cache_path("src", disk_key, "rgb48")
    meta = {"width": width, "height": height,
            "source_width": info["width"], "source_height": info["height"]}
    want = width * height * 3 * 2  # 3 channels, 2 bytes (uint16) each

    data = None
    if disk_path.exists():
        try:
            os.utime(disk_path, None)
            data = disk_path.read_bytes()
        except FileNotFoundError:
            # A prune (this process or another one sharing studio/cache)
            # deleted it between exists() and the read above; treat that
            # exactly like a cache miss instead of failing the request.
            data = None
    if data is None:
        # The transpose comes first: width and height above are probe's
        # POST-rotation size, so scaling before the turn would squeeze the
        # picture into the wrong aspect.
        vf = (f"{CG.rotate_prefix(info)}"
              f"scale={width}:{height}:flags=bilinear,setsar=1,"
              f"scale=in_color_matrix={matrix}:in_range={info['color_range']}"
              f":out_range=full,format=gbrp16le")
        args = ["ffmpeg", "-v", "error", "-y"] + CG.rotate_args(rotation)
        args += ["-ss", str(time_s), "-i", str(clip_path(clip)),
                 "-vf", vf, "-frames:v", "1",
                 "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"]
        with FFMPEG_SLOTS:
            proc = subprocess.run(args, capture_output=True)
        if proc.returncode != 0 or len(proc.stdout) < want:
            raise StudioError("ffmpeg could not decode that source frame:\n"
                              + proc.stderr.decode("utf-8", "replace")[-1200:])
        data = proc.stdout[:want]
        disk_path.write_bytes(data)
        _prune_cache("src", CACHE_SRC_MAX_FILES)

    arr = np.frombuffer(data, "<u2").reshape(height, width, 3)
    with _src_mem_lock:
        _src_mem[mem_key] = (arr, meta)
        _src_mem.move_to_end(mem_key)
        while len(_src_mem) > SRC_MEM_MAX:
            _src_mem.popitem(last=False)
    return arr, dict(meta)


def _grade_inputs(width: int, height: int, cfg: dict, info: dict,
                  seek: float | None = None) -> list[str]:
    """The non-source ffmpeg inputs for the grade-only pass.

    Mirrors cinegrade.ffmpeg_inputs' own tail (radial mask, then CG.
    mask_extra_inputs for every window AND component-stack matte, then
    grain) so the extra inputs land at the same index the filter graph
    expects: input 0 is the normalised source piped in over stdin here
    instead of ffmpeg decoding the clip itself, but everything after it has
    to stay in the same order or the graph reads the wrong input and
    produces a wrong picture, or ffmpeg refuses outright with "Invalid file
    index" when a component stack's matte input is missing entirely.
    CG.mask_input_indices is the one place that order is decided; calling
    CG.mask_extra_inputs (not the legacy window-only loop) is what keeps this
    in step with it, since that is the same walk (mask_inputs) both use.
    `strict_mattes` stays False here on purpose: this is a preview/still
    path (the viewer, scopes, stats), not a finished render, so a partial or
    still queued matte falls back to its nearest written frame instead of
    refusing (design rule 4's refusal belongs to start_render only).
    """
    args = ["-f", "rawvideo", "-pix_fmt", "rgb48le",
            "-s", f"{width}x{height}", "-i", "-"]
    if cfg["fx"]["radial_blur"]["enabled"]:
        rb = cfg["fx"]["radial_blur"]
        m = CG.radial_mask(info["width"], info["height"], rb["start"], rb["end"])
        args += ["-i", str(m)]
    args += CG.mask_extra_inputs(cfg, info, seek=seek)
    if cfg["grain"]["enabled"]:
        args += CG.grain_input(cfg, info)
    return args


# --------------------------------------------------------------------------
# single flight: one render per cache key, however many callers asked at once
#
# Every commit the page makes fires one POST /api/stats and three or four
# POST /api/scope with the SAME clip, time, width and config, so they land on
# one render_raw cache key exactly. On a warm cache that is four cheap reads.
# On a cold one it was four full ffmpeg renders of the identical frame, in
# parallel, competing for the same cores (measured 800 to 1700 ms each
# against 1 to 2 ms warm). This makes the first caller render and the rest
# wait for it and then read what it just wrote.
#
# Lock order is always this lock and then FFMPEG_SLOTS, never the other way
# round, so the pair cannot deadlock. Entries are reference counted and
# dropped once the last waiter leaves, so the map cannot grow across a long
# session.

_flight_guard = threading.Lock()
_flight: dict[str, list] = {}


class _SingleFlight:
    """`with _SingleFlight(key):` holds the one lock for that key.

    Not a plain dict of locks: an entry has to be removed when nobody is
    using it or a long session accumulates one lock per frame ever rendered,
    and removing it while somebody is still queued behind it would let two
    threads render the same frame anyway. The count is what makes the removal
    safe, and it is only ever touched under `_flight_guard`.
    """

    __slots__ = ("key", "entry")

    def __init__(self, key: str):
        self.key = key
        self.entry = None

    def __enter__(self):
        with _flight_guard:
            entry = _flight.get(self.key)
            if entry is None:
                entry = [threading.Lock(), 0]
                _flight[self.key] = entry
            entry[1] += 1
        self.entry = entry
        entry[0].acquire()
        return self

    def __exit__(self, *_exc):
        self.entry[0].release()
        with _flight_guard:
            self.entry[1] -= 1
            if self.entry[1] <= 0:
                _flight.pop(self.key, None)
        return False


def render_raw(clip: str, time_s: float, width: int, cfg: dict,
               rotation) -> tuple[np.ndarray, dict]:
    """One graded frame as an RGB array, cached on disk.

    Everything else the UI shows (the JPEG in the viewer, every scope, every
    statistic) is derived from this one array, so the numbers and the picture
    can never disagree, and a scope costs a cheap re-encode instead of another
    decode and LUT pass.

    Split into two ffmpeg processes: source_frame() does the decode and
    downscale (cached, see above), and this function feeds that cached array
    back in over stdin and runs only the grade-dependent part of the graph.
    The finished-frame cache below is unchanged: this is a second, cheaper
    layer underneath it, not a replacement for it.
    """
    cfg = full_config(cfg)
    rotation = CG.normalise_rotation(rotation)
    info = clip_info(clip, rotation)
    width, height = _preview_dims(info, width)
    factor = width / float(info["width"])
    pinfo = dict(info, width=width, height=height)
    pcfg = scale_for_preview(cfg, factor)

    key = hashlib.sha1(json.dumps({
        "clip": clip, "t": round(float(time_s), 4), "w": width,
        "rot": rotation, "cfg": pcfg, "engine": ENGINE_HASH,
    }, sort_keys=True).encode()).hexdigest()

    raw_path = _cache_path("frames", key, "rgb")
    meta = {"key": key, "width": width, "height": height,
            "source_width": info["width"], "source_height": info["height"]}

    def cached():
        """The finished frame off disk, or None if it is not there.

        Read twice: once before queueing behind any other caller wanting this
        exact frame, and once after, because the caller ahead has by then
        written it.
        """
        if not raw_path.exists():
            return None
        try:
            os.utime(raw_path, None)
            return np.frombuffer(raw_path.read_bytes(), np.uint8).reshape(
                height, width, 3)
        except FileNotFoundError:
            # pruned between exists() and read; treat it as a miss
            return None

    hit = cached()
    if hit is not None:
        return hit, meta

    with _SingleFlight("frame:" + key):
        hit = cached()
        if hit is not None:
            return hit, meta

        # source_frame has already turned the picture, so the grade graph must
        # not turn it again: src_normalised says exactly that.
        src, _smeta = source_frame(clip, time_s, width, rotation)

        graph = CG.graph_with_mask(pcfg, pinfo, encode_out=False,
                                   tail_extra=["format=rgb24"],
                                   src_label="0:v", src_normalised=True)
        args = ["ffmpeg", "-v", "error", "-y"]
        args += _grade_inputs(width, height, pcfg, pinfo, seek=time_s)
        args += ["-filter_complex", graph, "-map", "[vout]", "-frames:v", "1",
                 "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]

        with FFMPEG_SLOTS:
            proc = subprocess.run(args, input=src.tobytes(), capture_output=True)
        if proc.returncode != 0 or len(proc.stdout) < width * height * 3:
            raise StudioError("ffmpeg could not render that frame:\n"
                              + proc.stderr.decode("utf-8", "replace")[-1200:])

        data = proc.stdout[:width * height * 3]
        raw_path.write_bytes(data)
        _prune_cache("frames")
        return np.frombuffer(data, np.uint8).reshape(height, width, 3), meta


# --------------------------------------------------------------------------
# region and zoom: one patch of the frame, close up
#
# The agent-facing half of contract E4. `region` is four fractions of the
# frame AFTER rotation and `zoom` says how many times its fit size to render
# it at, exactly as `cinegrade still --region --zoom` defines them: the
# parsing, the outward pixel rounding and the cap all come from cinegrade
# itself (CG.normalise_region, CG.normalise_zoom, CG.region_pixels) so the
# CLI and this server cannot drift into two definitions of the same words.
#
# The frame is rendered WHOLE and then cropped, never cropped and then
# rendered. Every spatial stage in the engine (windows, the radial ramp,
# vignette, grain) is sized from the whole frame, so a crop before the grade
# would move all four relative to the picture. Cropping the finished render
# gives back exactly the pixels the full render has there, which is the only
# thing that makes a measurement on a patch mean anything.
# --------------------------------------------------------------------------

def region_render_width(width: int, zoom: float, source_width: int) -> int:
    """The width to render the WHOLE frame at so the region lands at `zoom`
    times its fit size. _preview_dims clamps this to the source anyway; the
    clamp is repeated here so the number in the cache key is the real one.
    """
    return max(160, min(int(round(width * zoom)), int(source_width)))


def crop_to_region(rgb: np.ndarray, region) -> tuple[np.ndarray, list[int]]:
    """The region of a rendered frame, plus the pixel box it came from."""
    h, w = rgb.shape[:2]
    x, y, cw, ch = CG.region_pixels(region, {"width": w, "height": h})
    return np.ascontiguousarray(rgb[y:y + ch, x:x + cw]), [x, y, cw, ch]


def region_cache_suffix(region, zoom: float) -> str:
    """What a region adds to a cache key.

    Empty for a plain full frame, so every key written before this feature
    existed is still the key that request produces. Without this a region
    request and a full frame request at the same render width would collide
    in the JPEG cache (encode_jpeg keys on the frame key alone) and one
    would be served as the other.
    """
    if region is None and zoom == 1.0:
        return ""
    parts = []
    if region is not None:
        parts.append("r" + ",".join(f"{v:.6f}" for v in region))
    if zoom != 1.0:
        parts.append(f"z{zoom:g}")
    return "_" + "_".join(parts)


def render_region(clip: str, time_s: float, width: int, cfg: dict, rotation,
                  region=None, zoom=None) -> tuple[np.ndarray, dict]:
    """render_raw, plus the region crop and the zoom's bigger render width.

    With no region and zoom 1 this IS render_raw, same cache entry, same
    bytes: `meta` only grows the extra keys when something was asked for.
    """
    region = CG.normalise_region(region)
    zoom = CG.normalise_zoom(zoom)
    if region is None and zoom == 1.0:
        return render_raw(clip, time_s, width, cfg, rotation)
    info = clip_info(clip, CG.normalise_rotation(rotation))
    rgb, meta = render_raw(clip, time_s,
                           region_render_width(width, zoom, info["width"]),
                           cfg, rotation)
    meta = dict(meta)
    meta["full_width"], meta["full_height"] = meta["width"], meta["height"]
    if region is not None:
        rgb, box = crop_to_region(rgb, region)
        meta["region"] = list(region)
        meta["region_pixels"] = box
        meta["width"], meta["height"] = box[2], box[3]
    meta["zoom"] = zoom
    meta["key"] = meta["key"] + region_cache_suffix(region, zoom)
    return rgb, meta


def render_raw_legacy(clip: str, time_s: float, width: int, cfg: dict,
                      rotation) -> tuple[np.ndarray, dict]:
    """The pre-split single-pass render: decode, downscale and grade in one
    ffmpeg call. Kept only so the parity check script can render the same
    config both ways and diff the pixels; no route calls this any more.
    """
    cfg = full_config(cfg)
    rotation = CG.normalise_rotation(rotation)
    info = clip_info(clip, rotation)
    width, height = _preview_dims(info, width)
    factor = width / float(info["width"])
    pinfo = dict(info, width=width, height=height)
    pcfg = scale_for_preview(cfg, factor)

    meta = {"key": "legacy", "width": width, "height": height,
            "source_width": info["width"], "source_height": info["height"]}

    # The rotation heads this chain, before the scale, for the same reason it
    # heads source_frame's: width and height are the post-rotation size.
    head = (f"[0:v]{CG.rotate_prefix(pinfo)}"
            f"scale={width}:{height}:flags=bilinear,"
            f"setsar=1[studiosrc]")
    graph = CG.graph_with_mask(pcfg, pinfo, encode_out=False,
                               tail_extra=["format=rgb24"],
                               src_label="studiosrc", head_extra=head)
    args = CG.ffmpeg_inputs(str(clip_path(clip)), pcfg, pinfo, float(time_s))
    args += ["-filter_complex", graph, "-map", "[vout]", "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]

    with FFMPEG_SLOTS:
        proc = subprocess.run(args, capture_output=True)
    if proc.returncode != 0 or len(proc.stdout) < width * height * 3:
        raise StudioError("ffmpeg could not render that frame:\n"
                          + proc.stderr.decode("utf-8", "replace")[-1200:])

    data = proc.stdout[:width * height * 3]
    return np.frombuffer(data, np.uint8).reshape(height, width, 3), meta


# --------------------------------------------------------------------------
# grain plate (C3): the GPU preview's own copy of ffmpeg's grain plate
# --------------------------------------------------------------------------

def _grain_plate_command(g: dict, width: int, height: int) -> tuple[list[str], str]:
    """The exact ffmpeg command that builds just the grain plate.

    Reuses CG.build_graph itself for the plate-construction segment (format,
    optional gblur for softness, optional colour mix, final scale), so a
    change to the engine's own grain block cannot silently drift away from
    what this route serves: only the input indices are remapped, because this
    standalone command has no picture input, only the plate's own lavfi
    source(s) at index 0 (and 1, when a colour plate is generated too).

    response is always left at "flat" here no matter what the caller's own
    config asks for: response is a weight computed from the PICTURE's own
    luminance, which this route never sees (it renders a plate, not a
    frame), so gpu.js applies that weighting itself against its own live
    picture texture, using the plain plate this route serves.
    """
    cfg = deepcopy(CG.DEFAULTS)
    cfg["grain"] = CG.deep_merge(CG.DEFAULTS["grain"], dict(g, enabled=True, response="flat"))
    # A stand-in info dict, probe()'s shape: build_graph builds the WHOLE
    # graph text (every stage, not only grain) before this function extracts
    # just the plate segment out of it, so every field build_graph's other
    # stages read has to be present even though none of those stages'
    # output is ever used or run.
    # color_space "bt2020nc" (probe()'s own fallback for an untagged camera
    # log source, the same value harness.py's patch_info uses for its own
    # synthetic swatch sources) keeps this consistent with DEFAULTS'
    # working_space "dwg": a bt709-tagged source there trips build_graph's
    # own log-vs-display guard, since this stand-in picture was never really
    # shot in any log at all and only exists to make build_graph run.
    info = {"width": width, "height": height, "rotation": 0,
            "autorotate": True, "pix_fmt": "rgb48le", "color_range": "full",
            "color_space": "bt2020nc", "nb_frames": "1", "duration": "1",
            "codec": None, "profile": None}
    idx_g = CG.mask_input_indices(cfg)["grain"]
    full, _ = CG.build_graph(cfg, info, encode_out=False)
    prefix = f"[{idx_g}:v]"
    kept: list[str] = []
    plate_label = None
    started = False
    for seg in full.split(";"):
        if not started:
            if not seg.startswith(prefix):
                continue
            started = True
        kept.append(seg)
        if seg.endswith("[grainplate]"):
            plate_label = "grainplate"
        elif seg.endswith("[gscaled]"):
            plate_label = "gscaled"
        if plate_label:
            break
    if not started or plate_label is None:
        raise StudioError("could not isolate the grain plate segment from "
                          "build_graph; grain.enabled must have been dropped "
                          "somewhere above")
    graph = ";".join(kept).replace(f"[{idx_g}:v]", "[0:v]") \
                          .replace(f"[{idx_g + 1}:v]", "[1:v]")
    args = ["ffmpeg", "-v", "error", "-y"] + CG.grain_input(cfg, info)
    args += ["-filter_complex", graph, "-map", f"[{plate_label}]",
             "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"]
    return args, plate_label


def grain_plate(g: dict, width: int, height: int) -> bytes:
    """The plate ffmpeg's grain block would build at width x height, cached.

    One bounded ffmpeg call (FFMPEG_SLOTS, the same semaphore every other
    render on this server waits on; -frames:v 1 and an explicit lavfi
    duration from CG.grain_input bound its length, the same guarantee the
    real render has). Cached on disk under studio/cache/grain, pruned with
    the same oldest-mtime helper every other cache on this server uses, so
    scrubbing the grain controls cannot grow that folder without bound.
    """
    width = max(2, min(int(width), 3840))
    height = max(2, min(int(height), 3840))
    key = hashlib.sha1(json.dumps(
        {"g": g, "w": width, "h": height}, sort_keys=True).encode()).hexdigest()
    path = _cache_path("grain", key, "rgb48")
    want = width * height * 3 * 2
    if path.exists():
        try:
            os.utime(path, None)
            return path.read_bytes()
        except FileNotFoundError:
            pass  # pruned between exists() and read; fall through and re-render
    args, _label = _grain_plate_command(g, width, height)
    with FFMPEG_SLOTS:
        proc = subprocess.run(args, capture_output=True)
    if proc.returncode != 0 or len(proc.stdout) < want:
        raise StudioError("ffmpeg could not render the grain plate:\n"
                          + proc.stderr.decode("utf-8", "replace")[-1200:])
    data = proc.stdout[:want]
    path.write_bytes(data)
    _prune_cache("grain")
    return data


def encode_jpeg(rgb: np.ndarray, key: str, quality: int = 2) -> bytes:
    """Re-encode an already rendered array. No decode, no LUT, a few ms.

    4:4:4 rather than the usual 4:2:0: chroma subsampling smears exactly the
    fine colour detail a grading decision turns on.
    """
    path = _cache_path("img", f"{key}_q{quality}", "jpg")
    if path.exists():
        try:
            os.utime(path, None)
            return path.read_bytes()
        except FileNotFoundError:
            pass  # pruned between exists() and read; re-encode instead of failing
    h, w = rgb.shape[:2]
    args = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{w}x{h}", "-i", "-", "-frames:v", "1",
            "-q:v", str(quality), "-pix_fmt", "yuvj444p", "-f", "mjpeg", "-"]
    proc = subprocess.run(args, input=rgb.tobytes(), capture_output=True)
    if proc.returncode != 0:
        raise StudioError(proc.stderr.decode("utf-8", "replace")[-800:])
    path.write_bytes(proc.stdout)
    _prune_cache("img")
    return proc.stdout


# --------------------------------------------------------------------------
# live loop range (GPU-graded playback)
#
# The GPU preview (static/gpu.js, wired into the viewer through static/live.js)
# grades a frame in a few milliseconds, so once the source pixels for a range
# are sitting in the browser, dragging a knob during playback is just another
# render() call with no server round trip. This is the only piece the server
# has to provide for that: the decoded, normalised source frames for a time
# range, in the exact rgb48le shape StudioGPU.setSource expects, in one
# response so the client is not making one HTTP request per frame of a loop.
#
# Deliberately NOT graded here. Grading happens client side per frame through
# gpu.js; this route is the multi-frame twin of source_frame() above (same
# scale + colour-matrix-normalise vf chain, so a loop frame and a still of the
# same timecode decode to the same bytes) and nothing else. A grade parameter
# never invalidates this cache, only clip/start/duration/width/rotation do.
# --------------------------------------------------------------------------

# A decoded rgb48le frame is width * height * 3 channels * 2 bytes each. This
# footage is portrait 4K (2160x3840 upright), so a 960 wide preview frame is
# 960x1706, not the 960x540 a 16:9 assumption would give: about 9.8MB/frame
# rather than about 2MB, so the same second count costs roughly 5x more here
# than on a landscape clip. 700MB gives about 3s at the 960 default preview
# width for this footage (about 6.7s at 640, about 27s at 320), while
# staying a small, bounded fraction of a modern machine's RAM (a few percent
# of 16GB+) rather than the unbounded growth that once filled the disk cache
# in this project; both this process's subprocess buffer and the browser's
# copy of the range have to fit inside it.
RANGE_MAX_BYTES = 700_000_000


def _range_bytes_per_frame(width: int, height: int) -> int:
    return width * height * 3 * 2


def range_budget(clip: str, width: int, rotation) -> dict:
    """What a caller can ask for at this width, computed from the real clip
    (fps and dimensions), before anyone commits to a decode. Used by the
    client to clamp the loop range in the UI, and mirrored by the hard check
    inside source_range() below so a request built by hand (or a client that
    skipped the preflight) still gets the same answer, never a silent
    truncation or a hang.
    """
    info = clip_info(clip, rotation)
    pwidth, pheight = _preview_dims(info, width)
    fps = float(info.get("fps") or 24.0)
    per_frame = _range_bytes_per_frame(pwidth, pheight)
    max_frames = max(1, RANGE_MAX_BYTES // per_frame)
    return {
        "width": pwidth, "height": pheight, "fps": fps,
        "bytes_per_frame": per_frame, "max_frames": int(max_frames),
        "max_seconds": max_frames / fps, "cap_bytes": RANGE_MAX_BYTES,
    }


def source_range(clip: str, start: float, duration: float, width: int,
                 rotation) -> tuple[np.ndarray, dict]:
    """The decoded, normalised source frames for [start, start+duration),
    one ffmpeg process, at the preview width. Mirrors source_frame()'s own
    vf chain exactly (scale, then the colour-matrix normalise into full
    range gbrp16le) so a loop frame and a cached still of the same timecode
    are byte for byte the same picture.

    Returns uint16 data shaped (N, H, W, 3), R G B per pixel, plus meta with
    the actual frame count and fps so the client can compute a real playback
    rate rather than assuming one.
    """
    rotation = CG.normalise_rotation(rotation)
    info = clip_info(clip, rotation)
    pwidth, pheight = _preview_dims(info, width)
    fps = float(info.get("fps") or 24.0)

    start = max(0.0, float(start))
    total_dur = float(info.get("duration") or 0.0)
    remaining = max(1.0 / fps, total_dur - start) if total_dur else float(duration)
    duration = max(1.0 / fps, min(float(duration), remaining))
    want_frames = max(1, round(duration * fps))

    per_frame = _range_bytes_per_frame(pwidth, pheight)
    max_frames = max(1, RANGE_MAX_BYTES // per_frame)
    if want_frames > max_frames:
        max_seconds = max_frames / fps
        raise StudioError(
            f"That loop range is {want_frames} frames at {pwidth}x{pheight} "
            f"({want_frames * per_frame / 1_000_000:.0f} MB), over the "
            f"{RANGE_MAX_BYTES / 1_000_000:.0f} MB loop budget. At {pwidth} wide you "
            f"can loop up to {max_seconds:.1f} s at a time. Drop the preview width "
            f"(fewer bytes per frame) to fit more time in the same budget, or loop "
            f"a shorter range.")

    matrix = CG.source_matrix(info)
    # Rotation first, exactly as in source_frame, so a loop frame and a still
    # of the same timecode stay byte for byte the same picture.
    vf = (f"{CG.rotate_prefix(info)}"
          f"scale={pwidth}:{pheight}:flags=bilinear,setsar=1,"
          f"scale=in_color_matrix={matrix}:in_range={info['color_range']}"
          f":out_range=full,format=gbrp16le")
    args = ["ffmpeg", "-v", "error", "-y"] + CG.rotate_args(rotation)
    # -t bounds this the same way every other ffmpeg call in this file is
    # bounded (house rule: a call with no duration limit never terminates on
    # its own). -vsync cfr + -r locks the output to one frame per fps tick
    # even over a variable frame rate source, so want_frames above is what
    # actually comes back, not however many the source happened to encode.
    args += ["-ss", str(start), "-i", str(clip_path(clip)), "-t", str(duration),
             "-vf", vf, "-vsync", "cfr", "-r", str(fps),
             "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"]

    with FFMPEG_SLOTS:
        proc = subprocess.run(args, capture_output=True)
    if proc.returncode != 0:
        raise StudioError("ffmpeg could not decode that range:\n"
                          + proc.stderr.decode("utf-8", "replace")[-1200:])

    got_frames = len(proc.stdout) // per_frame
    if got_frames < 1:
        raise StudioError("ffmpeg returned no frames for that range "
                          f"(clip {clip!r} at {start:.2f}s)")
    got_frames = min(got_frames, want_frames)
    data = proc.stdout[:got_frames * per_frame]

    arr = np.frombuffer(data, "<u2").reshape(got_frames, pheight, pwidth, 3)
    meta = {"width": pwidth, "height": pheight, "frames": got_frames,
            "fps": fps, "start": start, "duration": got_frames / fps,
            "source_width": info["width"], "source_height": info["height"]}
    return arr, meta


# --------------------------------------------------------------------------
# playback (streamed segments)
#
# A still frame and a played segment are the same picture, over and over: a
# preview render, downscaled BEFORE the graph runs so pixel-denominated FX
# (halation and bloom sigma above all) stay the same relative size at 960
# wide as at the source's full width. render_raw() above proves that by
# calling _preview_dims then scale_for_preview; _play_params below makes the
# exact same two calls before building a segment's ffmpeg command, so a
# played frame and a still of the same timecode agree (checked numerically
# in tools/verify_play_sigma.py, not just asserted here).
#
# Unlike a still, a segment is seconds of video, so it is not worth the
# studio's usual two-pass split (source_frame() cached, then a second
# process grades it): one ffmpeg process doing decode, scale, grade and
# h264_videotoolbox encode together, in that order, is the 1.5x-realtime
# path this was measured against. Streaming that process's stdout straight
# into the HTTP response (fragmented mp4: movflags
# frag_keyframe+empty_moov+default_base_moof) is what lets the browser start
# playing after the first fragment instead of after the whole segment
# finishes rendering.
# --------------------------------------------------------------------------

SEGMENTS = CACHE / "segments"

# A segment is a lot more bytes than a JPEG preview frame (seconds of h264
# versus one still), so unlike CACHE_MAX_FILES this cache is capped by total
# size, not file count: a handful of long segments could blow past a
# file-count cap while staying small in bytes, or many short ones blow past
# a byte cap while staying under a small file count either way. 1.5GB is a
# generous local scratch budget and, unlike a file count, is the number that
# actually answers "did this fill the disk again" -- the frame cache already
# did that once in this project (see CACHE_MAX_FILES above).
SEGMENT_CACHE_MAX_BYTES = 1_500_000_000

# Registered by /api/play/prepare, read by /api/play/stream on a cache miss.
# The stream route only gets a hash key over HTTP (deliberately GET, so a
# plain <video src> can point straight at it without a JSON body), and the
# ffmpeg args need the full resolved config, too much to round-trip through
# a URL. An OrderedDict LRU rather than growing forever: every press of Play
# adds one entry, and a long session presses Play a lot. 40 is generous for
# "keys from the last few minutes", which is the only lookback a stream
# request should ever need -- the browser sets video.src within the same
# prepare() response handler that registered the key.
_play_pending: "OrderedDict[str, dict]" = OrderedDict()
_play_pending_lock = threading.Lock()
PLAY_PENDING_MAX = 40


def _segment_path(key: str) -> Path:
    return SEGMENTS / f"{key}.mp4"


def _register_pending(params: dict) -> None:
    with _play_pending_lock:
        _play_pending[params["key"]] = params
        _play_pending.move_to_end(params["key"])
        while len(_play_pending) > PLAY_PENDING_MAX:
            _play_pending.popitem(last=False)


# Which clip a playback segment key or a proxy key belongs to, contract E1.
#
# Both of those files are served by a GET that carries only the cache key,
# because a <video src> cannot send a body. The key is a hash, so on its own
# it says nothing about whose clip it is, and the read rule below needs a clip
# to answer. Every client asks `prepare` before it fetches either file, so
# `prepare` is where the answer is written down.
_key_clip: "OrderedDict[str, str]" = OrderedDict()
_key_clip_lock = threading.Lock()
KEY_CLIP_MAX = 400


def _remember_key(key: str, clip: str) -> None:
    if not key or not clip:
        return
    with _key_clip_lock:
        _key_clip[key] = clip
        _key_clip.move_to_end(key)
        while len(_key_clip) > KEY_CLIP_MAX:
            _key_clip.popitem(last=False)


def _clip_for_key(key: str) -> str | None:
    """The clip behind a cache key, or None when this server has not been
    asked to prepare it in this run.

    None is not "allowed": the two callers refuse an unknown key outright
    when logins are on, because the file it names may have been written by
    another account (the frame, segment and proxy caches are one folder for
    the whole machine) and there would be nothing left to check it against.
    """
    with _key_clip_lock:
        clip = _key_clip.get(key)
        if clip is not None:
            _key_clip.move_to_end(key)
            return clip
    with _play_pending_lock:
        params = _play_pending.get(key)
    return params["clip"] if params else None


def _prune_cache_bytes(kind: str, max_bytes: int, pattern: str = "*.mp4") -> None:
    """Byte-capped LRU eviction, the segment cache's counterpart to the
    file-count _prune_cache above. Oldest mtime first, same as that one, and
    tolerant of the same race: a file another process (or another thread in
    this one) already deleted by the time stat() runs here is treated as
    already evicted rather than raising out of this walk.
    """
    d = CACHE / kind
    dated = []
    for p in d.glob(pattern):
        try:
            dated.append((p.stat().st_mtime, p.stat().st_size, p))
        except FileNotFoundError:
            continue
    dated.sort(key=lambda t: t[0])
    total = sum(size for _, size, _ in dated)
    i = 0
    while total > max_bytes and i < len(dated):
        _, size, p = dated[i]
        try:
            p.unlink()
            total -= size
        except FileNotFoundError:
            total -= size          # already gone, someone else's prune got it first
        except OSError:
            pass
        i += 1


def _play_bitrate(width: int, height: int) -> str:
    """A generous constant bitrate for a local preview stream.

    0.35 bits per pixel per frame at 24fps sits well above typical
    "visually clean" h264 (roughly 0.1-0.2 bpp for ordinary delivery): this
    is a grading tool, so a compression artefact getting mistaken for a
    grading problem is a worse failure than a bigger cache file, and the
    segment cache is already capped by total bytes above rather than
    leaning on a stingy bitrate to keep the disk in check.
    """
    bits = width * height * 24.0 * 0.35
    return str(max(2_000_000, int(bits)))


def _play_params(payload: dict, user_id=None) -> dict:
    """Resolve one /api/play/prepare request into everything the ffmpeg
    command and the cache key need.

    Calls _preview_dims and scale_for_preview exactly as render_raw() does,
    which is the one thing this whole feature has to get right: skip that
    and the preview plays a grade with roughly 4x too much halation and
    bloom relative to what the still-frame viewer (and the real render) show
    at the same width, because those sigmas are absolute pixel counts, not
    fractions of the frame.
    """
    clip = payload["clip"]
    rotation = effective_rotation(payload, user_id)
    info = clip_info(clip, rotation)
    cfg = full_config(payload.get("config"))
    start = max(0.0, float(payload.get("time") or 0.0))
    fps = float(info.get("fps") or 24.0)
    dur_req = float(payload.get("duration") or 5.0)
    remaining = max(1.0 / fps, info["duration"] - start) if info.get("duration") else dur_req
    duration = max(1.0 / fps, min(dur_req, remaining))

    width = int(payload.get("width", 960))
    pwidth, pheight = _preview_dims(info, width)
    factor = pwidth / float(info["width"])
    pcfg = scale_for_preview(cfg, factor)
    pinfo = dict(info, width=pwidth, height=pheight)

    key = hashlib.sha1(json.dumps({
        "clip": clip, "start": round(start, 4), "duration": round(duration, 4),
        "w": pwidth, "rot": rotation, "cfg": pcfg, "engine": ENGINE_HASH,
    }, sort_keys=True).encode()).hexdigest()

    return {"key": key, "clip": clip, "start": start, "duration": duration,
            "rotation": rotation, "cfg": cfg, "pcfg": pcfg,
            "info": info, "pinfo": pinfo, "fps": fps}


def _play_extra_inputs(cfg: dict, info: dict, seek: float | None = None,
                       duration: float | None = None) -> list[str]:
    """The mask/grain -i args for a preview-scaled segment.

    Mirrors _grade_inputs' own tail above: same order (radial mask, then
    CG.mask_extra_inputs for every window AND component-stack matte, then
    grain), same reason (build_graph fixes those input indices, so the order
    here has to match what the filter graph expects, and a component stack
    with no matching -i is an ffmpeg "Invalid file index" refusal, not a
    silently wrong picture). Kept as its own small copy rather than shared
    with _grade_inputs, because that function's first input is a rawvideo
    pipe from an already-decoded source frame and this one decodes the clip
    itself as input 0; the two pipelines only share what comes after input 0.
    strict_mattes stays False: a play preview falls back to the nearest
    written frame the same as the still viewer, it does not refuse.
    """
    args = []
    if cfg["fx"]["radial_blur"]["enabled"]:
        rb = cfg["fx"]["radial_blur"]
        m = CG.radial_mask(info["width"], info["height"], rb["start"], rb["end"])
        args += ["-i", str(m)]
    args += CG.mask_extra_inputs(cfg, info, seek=seek, duration=duration)
    if cfg["grain"]["enabled"]:
        args += CG.grain_input(cfg, info)
    return args


def _play_ffmpeg_args(params: dict) -> list[str]:
    """The single ffmpeg command a playback segment is: decode, scale,
    grade and h264_videotoolbox-encode in one pass, piped to stdout as
    fragmented mp4. Building the graph here (CG.graph_with_mask) is also
    what surfaces a bad config (an unreadable LUT, a working-space
    mismatch) as a StudioError/GradeError before any HTTP header goes out,
    since the caller runs this before _stream_start.
    """
    pcfg, pinfo = params["pcfg"], params["pinfo"]
    width, height = pinfo["width"], pinfo["height"]
    # The turn heads this chain, before the scale (the scale target is the
    # post-rotation size), so graph_with_mask must not add a second one:
    # head_extra already means "this caller owns its own source chain".
    head = (f"[0:v]{CG.rotate_prefix(pinfo)}"
            f"scale={width}:{height}:flags=bicubic,setsar=1[studiosrc]")
    graph = CG.graph_with_mask(pcfg, pinfo, src_label="studiosrc", head_extra=head)

    args = ["ffmpeg", "-v", "error", "-y"] + CG.rotate_args(params["rotation"])
    args += ["-ss", str(params["start"]), "-i", str(clip_path(params["clip"]))]
    args += _play_extra_inputs(pcfg, pinfo, seek=params["start"],
                               duration=params["duration"])
    # An output option here (it comes after every -i), so it caps how much
    # ENCODED output ffmpeg produces no matter how long the source clip
    # runs on: the house rule this project already broke once, at 2.6GB, is
    # never starting ffmpeg with grain (or anything else) enabled and no
    # duration bound.
    args += ["-t", str(params["duration"])]
    args += ["-filter_complex", graph, "-map", "[vout]", "-an"]
    # ~1s between keyframes: frag_keyframe below cuts a new fragment at
    # each one, so the browser can start playing after roughly a keyframe's
    # worth of encode instead of waiting for the whole segment to finish
    # muxing.
    gop = max(1, int(round(params["fps"])))
    args += [
        "-c:v", "h264_videotoolbox", "-b:v", _play_bitrate(width, height),
        "-g", str(gop), "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "-",
    ]
    return args


# --------------------------------------------------------------------------
# scopes and statistics
# --------------------------------------------------------------------------

SCOPE_GRAPHS = {
    "histogram": ("format=gbrp,histogram=display_mode=stack:levels_mode=linear"
                  ":components=7,format=rgb24"),
    "waveform": ("format=yuv422p10le,waveform=intensity=0.08:mode=column"
                 ":display=overlay:components=1:filter=lowpass"
                 ":graticule=green:flags=numbers+dots,format=rgb24"),
    "parade": ("format=gbrp,waveform=intensity=0.08:mode=column"
               ":display=parade:components=7:filter=lowpass"
               ":graticule=green:flags=numbers+dots,format=rgb24"),
    "vectorscope": ("format=yuv422p10le,vectorscope=mode=color3:graticule=color"
                    ":flags=name+white:envelope=instant:intensity=0.22,"
                    "format=rgb24"),
}

# Height as a fraction of width. The vectorscope has to come out square: it is
# a polar plot, so stretching it moves every hue off the graticule leg it is
# supposed to be read against, and the skin-tone line stops meaning anything.
SCOPE_ASPECT = {"histogram": 0.72, "waveform": 0.56, "parade": 0.42,
                "vectorscope": 1.0}


def render_scope(rgb: np.ndarray, key: str, kind: str, size: int) -> bytes:
    if kind not in SCOPE_GRAPHS:
        raise StudioError(f"unknown scope: {kind}")
    path = _cache_path("img", f"{key}_{kind}_{size}", "jpg")
    if path.exists():
        try:
            os.utime(path, None)
            return path.read_bytes()
        except FileNotFoundError:
            pass  # pruned between exists() and read; re-render instead of failing
    h, w = rgb.shape[:2]
    out_h = max(2, int(size * SCOPE_ASPECT[kind]) // 2 * 2)
    vf = SCOPE_GRAPHS[kind] + f",scale={size}:{out_h}"
    args = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{w}x{h}", "-i", "-", "-vf", vf, "-frames:v", "1",
            "-q:v", "4", "-pix_fmt", "yuvj420p", "-f", "mjpeg", "-"]
    with FFMPEG_SLOTS:
        proc = subprocess.run(args, input=rgb.tobytes(), capture_output=True)
    if proc.returncode != 0:
        raise StudioError(proc.stderr.decode("utf-8", "replace")[-800:])
    path.write_bytes(proc.stdout)
    _prune_cache("img")
    return proc.stdout


# frame_stats(), HUE_FAMILIES, SAT_FLOOR, CLIP_BLACK and CLIP_WHITE moved to
# grade/stats.py (imported above), unchanged in what they return: nothing
# else in this repo referenced them by this module's name.


# --------------------------------------------------------------------------
# path source: a read only frame or stats measurement of a file this server
# never registers anywhere, for /api/frame and /api/stats' `path` field
# --------------------------------------------------------------------------

def path_source_frame(payload: dict, user_id) -> np.ndarray:
    """The decoded frame for a request's read-only `path` field.

    `path` names an absolute file outside anything this server tracks: no
    footage root, no EXTERNAL_CLIPS entry, no project opened, no session
    write. It exists for measuring or previewing a file this server was
    never told about (an intermediate render, a clip mid-copy), which is
    exactly why it is refused the moment logins are on: there is no account
    to check the file against, so honouring it would be a way past every
    other route's read guard. Rotation follows the same rule a clip does
    (effective_rotation), since nothing about that resolution depends on the
    source being a registered clip. The grading `config` a clip request can
    carry is not applied here: this is a raw decode of the file as it is on
    disk, the same guarantee `decode_image` gives the CLI and the client
    module, so a caller reads the same bytes no matter which of the three
    asked for them.
    """
    if AUTH.enabled():
        raise AUTH.AuthError(403, "the path field only works with logins off")
    raw = str(payload.get("path") or "")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise StudioError(f"path must be absolute, got: {raw!r}")
    if not p.is_file():
        raise StudioError(f"file not found: {p}")
    return stats_decode_image(
        str(p), width=int(payload.get("width", 960)),
        region=payload.get("region"), time=float(payload.get("time", 0)),
        rotation=effective_rotation(payload, user_id))


def path_source_key(payload: dict) -> str:
    """A stable identity key for a `path` sourced frame or stats call, for
    the same X-Frame-Key / cache-key role `render_raw`'s key plays for a clip.
    Not written into any cache on disk (this route caches nothing), only
    used to give the client something stable to compare across two calls.
    """
    raw = str(payload.get("path") or "")
    bits = "|".join(str(payload.get(k)) for k in
                    ("time", "width", "region", "rotation"))
    return "path:" + hashlib.sha1(f"{raw}|{bits}".encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# presets
# --------------------------------------------------------------------------

def _preset_entry(p: Path, library: bool) -> dict:
    try:
        raw = json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        return {"name": p.stem, "error": str(exc), "library": library}
    return {"name": p.stem, "comment": raw.get("_comment", ""),
            "look": (raw.get("look") or {}).get("lut"), "library": library}


def list_presets(user_id: int = 0) -> list[dict]:
    """The shipped library plus this account's own presets, library first.

    grade/presets/ is checked into the repository and shared by every account,
    so it is read only here: `library: true` says so, and delete refuses those
    with a 403. Anything the user saves lands in
    studio/data/users/<id>/presets/ and comes back with `library: false`. With
    logins off that is user 0, so the founder's own saves still get a folder of
    their own instead of editing tracked files.

    A user preset with the same name as a library one shadows it, and only the
    user's copy is listed: two identically named entries in one dropdown would
    be a picker where the right answer is unknowable.
    """
    items = [_preset_entry(p, True) for p in sorted(PRESETS.glob("*.json"))]
    mine = sorted(GRADES.user_presets_dir(user_id).glob("*.json"))
    names = {p.stem for p in mine}
    items = [it for it in items if it["name"] not in names]
    items.extend(_preset_entry(p, False) for p in mine)
    return items


def preset_path(name: str, user_id: int = 0) -> tuple[Path, bool]:
    """Where one preset actually is, and whether it is a library file.

    The user's own folder is looked at first so a saved preset shadows a
    shipped one of the same name, matching what list_presets shows.
    """
    name = safe_name(name)
    mine = GRADES.user_presets_dir(user_id) / f"{name}.json"
    if mine.exists():
        return mine, False
    return PRESETS / f"{name}.json", True


def read_preset(name: str, user_id: int = 0) -> dict:
    p, _library = preset_path(name, user_id)
    if not p.exists():
        raise StudioError(f"preset not found: {name}")
    # full_config, not a bare deep_merge, so a preset saved before layers
    # existed is migrated on read like every other config that reaches here.
    cfg = full_config(json.loads(p.read_text()))
    # _comment describes the FILE, not the grade. Leaving it in the config
    # that becomes the live session meant loading a preset silently poisoned
    # every future save (Save As with a blank comment, Overwrite on a
    # different preset) with the text of whatever was loaded most recently.
    cfg.pop("_comment", None)
    return cfg


def read_preset_comment(name: str, user_id: int = 0) -> str:
    """The `_comment` a preset file carries, "" when it has none.

    Split from read_preset() because read_preset() strips _comment on
    purpose (its own docstring: the comment describes the FILE, not a config
    about to become someone's live grade). GET /api/preset wants both at
    once (contract G4: the documented read back could not confirm the thing
    POST just wrote, since GET silently dropped it), so it reads the file a
    second time here rather than changing what read_preset() hands the
    editor.
    """
    p, _library = preset_path(name, user_id)
    if not p.exists():
        raise StudioError(f"preset not found: {name}")
    try:
        return json.loads(p.read_text()).get("_comment", "") or ""
    except json.JSONDecodeError:
        return ""


def write_preset(name: str, cfg: dict, comment: str = "",
                 user_id: int = 0) -> Path:
    name = safe_name(name)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise StudioError("preset names may use letters, digits, dot, dash and "
                          "underscore only")
    body = config_diff(full_config(cfg))
    # The live config must never be trusted for "the existing comment": since
    # read_preset now strips _comment on load, a config with one on it would
    # only mean the caller injected it directly, and even before that fix a
    # config carried in from a DIFFERENT loaded preset could still have that
    # preset's comment attached. The only honest source for "the comment this
    # preset already has" is the preset's own file on disk, keyed by the name
    # being written, not by whatever was last loaded into the editor.
    body.pop("_comment", None)
    p = GRADES.user_presets_dir(user_id) / f"{name}.json"
    if not comment and p.exists():
        try:
            comment = json.loads(p.read_text()).get("_comment", "") or ""
        except (json.JSONDecodeError, OSError):
            comment = ""
    if comment:
        body = {"_comment": comment, **body}
    # Always the account's own folder, never the shipped library: same file
    # format, same indent, same trailing newline as before, just somewhere the
    # user owns. "Overwrite" on a library preset therefore writes a user copy
    # that shadows it, which is the only sane meaning of overwrite on a file
    # every other account is also reading.
    p.write_text(json.dumps(body, indent=2) + "\n")
    return p


# --------------------------------------------------------------------------
# look LUTs
# --------------------------------------------------------------------------

CUBE_SIZE_RE = re.compile(r"^\s*LUT_3D_SIZE\s+(\d+)", re.M)


def list_looks() -> list[dict]:
    items = []
    for p in sorted(LOOKS.glob("*.cube")):
        head = p.open("r", errors="replace").read(2048)
        m = CUBE_SIZE_RE.search(head)
        title = re.search(r'^\s*TITLE\s+"?([^"\n]+)"?', head, re.M)
        items.append({
            "name": p.stem,
            "size": int(m.group(1)) if m else None,
            "title": title.group(1).strip() if title else p.stem,
            "bytes": p.stat().st_size,
            "generated": "make_looks.py" in head,
        })
    return items


def validate_cube(text: str) -> int:
    """Refuse a file that is not a 3D cube before it lands in the looks folder.

    An unreadable .cube does not fail at import, it fails on the next render,
    by which point the user is debugging their grade instead of their file.
    """
    m = CUBE_SIZE_RE.search(text)
    if not m:
        raise StudioError("not a 3D LUT: no LUT_3D_SIZE line found")
    size = int(m.group(1))
    if not 2 <= size <= 128:
        raise StudioError(f"LUT_3D_SIZE {size} is out of range")
    rows = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line[0].isalpha() or line[0] == '"':
            continue
        parts = line.split()
        if len(parts) == 3:
            try:
                [float(x) for x in parts]
            except ValueError:
                continue
            rows += 1
    if rows != size ** 3:
        raise StudioError(f"expected {size ** 3} data rows for a {size}-cube, "
                          f"found {rows}")
    return size


def parse_cube_data(path: Path) -> tuple[int, np.ndarray]:
    """Read a .cube file into (size, flat float32 array), file order preserved.

    File order is already what a shader wants for a 3D texture upload (red
    index fastest, per the .cube spec), so this does no reordering: it is the
    same row-skip logic as validate_cube, just keeping the numbers instead of
    only counting them.
    """
    text = path.read_text()
    m = CUBE_SIZE_RE.search(text)
    if not m:
        raise StudioError(f"not a 3D LUT: {path}")
    size = int(m.group(1))
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line[0].isalpha() or line[0] == '"':
            continue
        parts = line.split()
        if len(parts) == 3:
            try:
                rows.append([float(x) for x in parts])
            except ValueError:
                continue
    if len(rows) != size ** 3:
        raise StudioError(f"expected {size ** 3} data rows for a {size}-cube, "
                          f"found {len(rows)}")
    return size, np.asarray(rows, dtype="<f4").reshape(-1)


# Parsed cubes keyed by (path, mtime). An edit changes the mtime, so the old
# entry for that path just ages out under a different key rather than needing
# an explicit invalidation; the LUT library here is a few dozen files, small
# enough that unbounded growth is not a real concern for a local dev tool.
_lut_cache: dict[tuple[str, int], tuple[int, np.ndarray]] = {}
_lut_cache_lock = threading.Lock()


def cached_cube(path: Path) -> tuple[int, np.ndarray]:
    key = (str(path), path.stat().st_mtime_ns)
    with _lut_cache_lock:
        hit = _lut_cache.get(key)
        if hit is not None:
            return hit
    parsed = parse_cube_data(path)
    with _lut_cache_lock:
        _lut_cache[key] = parsed
    return parsed


# --------------------------------------------------------------------------
# background jobs
# --------------------------------------------------------------------------

class Job:
    def __init__(self, kind: str, label: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.label = label
        self.status = "running"
        self.progress = 0.0
        self.message = ""
        self.output = ""
        self.started = time.time()
        self.finished = None
        self.proc: subprocess.Popen | None = None
        self.log: list[str] = []
        # Additive, kind specific fields (mask_track's matte_ids, for one)
        # that do not belong on every job, merged into as_dict() below so a
        # kind agnostic reader still gets one flat dict.
        self.extra: dict = {}

    def as_dict(self) -> dict:
        out = {
            "id": self.id, "kind": self.kind, "label": self.label,
            "status": self.status, "progress": round(self.progress, 4),
            "message": self.message, "output": self.output,
            "started": self.started, "finished": self.finished,
            "log": self.log[-12:],
        }
        if self.extra:
            out.update(self.extra)
        return out


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def _register(job: Job) -> Job:
    with JOBS_LOCK:
        JOBS[job.id] = job
        # Keep the list short enough to render, oldest finished ones first.
        done = [j for j in JOBS.values() if j.status != "running"]
        for j in sorted(done, key=lambda j: j.finished or 0)[:-30]:
            JOBS.pop(j.id, None)
    return job


def start_render(payload: dict, user_id=None) -> Job:
    # Engine "gpu" is the same render through a headless Chrome running
    # gpu.js instead of ffmpeg's filter graph (studio/render_gpu.py). Every
    # other option in this payload means the same thing to both engines, and
    # ffmpeg stays the default and the reference.
    #
    # The rotation is resolved here, once, and written back into the payload
    # as an explicit field so both engines render the same orientation even
    # when the request only carried the legacy flag or nothing at all.
    rotation = effective_rotation(payload, user_id)
    payload = dict(payload, rotation=rotation)
    clip = payload["clip"]
    cfg = full_config(payload.get("config"))
    info = clip_info(clip, rotation)
    start = float(payload.get("start") or 0.0)
    duration = payload.get("duration")
    duration = float(duration) if duration not in (None, "") else None
    # Design rule 4: a render refuses a matte that has not finished covering
    # the range it is asked for, unless the caller says allow_partial. One
    # call, ahead of both engines, using the policy function the engine
    # already owns (grade/cinegrade.py's require_complete_mattes, C1) rather
    # than a second copy of the same walk here; it raises CG.GradeError,
    # which _dispatch already turns into a 400 naming the layer and matte.
    if not payload.get("allow_partial"):
        CG.require_complete_mattes(cfg, info, seek=start, duration=duration)
    # The coverage rule above says whether a matte is finished. It does not say
    # whether it is a matte of THIS clip, and nothing outside the browser
    # checked that: a render could stretch a portrait matte tracked on another
    # clip over this picture and write the file with no warning at all.
    # Unconditional, for both engines, because allow_partial means "I accept an
    # unfinished matte" and never "I accept the wrong clip's matte".
    _require_render_mattes_match(cfg, clip)
    if str(payload.get("engine") or "ffmpeg").lower() == "gpu":
        return RG.start_gpu_render(payload, user_id=user_id)
    scale = payload.get("scale")            # optional preview downscale
    name = safe_name(payload.get("name") or f"studio_{int(time.time())}")
    codec = cfg["output"]["codec"]
    ext = ".mov" if codec == "prores_ks" else ".mp4"
    out_path = OUT / (name + ext)

    rcfg, rinfo, head, src = cfg, info, None, "0:v"
    if scale and int(scale) < info["width"]:
        width = int(scale) // 2 * 2
        factor = width / float(info["width"])
        height = max(2, int(round(info["height"] * factor / 2)) * 2)
        rinfo = dict(info, width=width, height=height)
        rcfg = scale_for_preview(cfg, factor)
        # Rotation before the scale here too; without the head_extra branch
        # graph_with_mask puts it in front of the graph itself.
        head = (f"[0:v]{CG.rotate_prefix(rinfo)}"
                f"scale={width}:{height}:flags=bicubic,setsar=1[studiosrc]")
        src = "studiosrc"

    graph = CG.graph_with_mask(rcfg, rinfo, src_label=src, head_extra=head)
    args = CG.ffmpeg_inputs(str(clip_path(clip)), rcfg, rinfo,
                            start if start else None)
    if duration:
        args += ["-t", str(duration)]
    args += ["-filter_complex", graph, "-map", "[vout]"]
    if not payload.get("no_audio"):
        args += ["-map", "0:a?", "-c:a", "aac", "-b:a", "256k"]
    o = cfg["output"]
    if codec == "prores_ks":
        args += ["-c:v", "prores_ks", "-profile:v", str(o["profile"]),
                 "-vendor", "apl0", "-pix_fmt", "yuv422p10le"]
    else:
        args += ["-c:v", codec, "-crf", str(o["crf"]),
                 "-preset", o["preset"], "-pix_fmt", "yuv420p"]
    args += ["-color_primaries", "bt709", "-color_trc", "bt709",
             "-colorspace", "bt709", "-progress", "pipe:1", "-nostats",
             str(out_path)]

    total = duration if duration else max(0.1, info["duration"] - start)
    job = _register(Job("render", f"{name}{ext}"))
    job.output = str(out_path)

    def worker():
        try:
            job.proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True)
            for line in job.proc.stdout:
                line = line.strip()
                if line.startswith("out_time_us="):
                    try:
                        secs = int(line.split("=", 1)[1]) / 1e6
                        job.progress = min(1.0, secs / total)
                        job.message = f"{secs:.1f}s of {total:.1f}s"
                    except ValueError:
                        pass
                elif line.startswith("frame="):
                    job.log.append(line)
            job.proc.wait()
            err = job.proc.stderr.read()
            if job.status == "cancelled":
                Path(out_path).unlink(missing_ok=True)
            elif job.proc.returncode == 0:
                job.status = "done"
                job.progress = 1.0
                size = out_path.stat().st_size if out_path.exists() else 0
                job.message = f"{size / 1e6:.1f} MB"
            else:
                job.status = "failed"
                job.message = (err or "ffmpeg failed").strip()[-600:]
        except Exception as exc:                              # noqa: BLE001
            job.status = "failed"
            job.message = f"{exc}"
        finally:
            job.finished = time.time()

    threading.Thread(target=worker, daemon=True).start()
    return job


# --------------------------------------------------------------------------
# playback proxy (the GPU playback path, live.js Mode 3)
#
# The segment stream above renders the whole grade chain on the CPU and
# streams the RESULT, which measured about 2 fps on a 4K chain and cannot be
# re-graded once it is encoded. This is the other half of the answer: one
# bounded ffmpeg pass per clip writes an UNGRADED proxy, the browser plays it
# in a hidden <video>, and every frame goes through gpu.js. The grade then
# costs nothing per frame that a still render did not already cost, and a
# knob turn shows up on the next presented frame instead of restarting an
# encode.
#
# The one thing this has to get right is the pixel domain. source_frame()
# above hands the GPU full range RGB, produced by
# scale=in_color_matrix=<clip matrix>:in_range=<clip range>:out_range=full.
# The proxy runs that exact same normalisation and then converts to Y'CbCr
# with a bt709 matrix that is TAGGED on the file, so the browser's own
# decoder undoes precisely that conversion and the shader sees the same
# numbers. Getting this wrong is silent: the picture still looks like a
# picture, it is just graded from different source values than the still.
# --------------------------------------------------------------------------

PROXY = CACHE / "proxy"

# One proxy per (clip, width, range tag). Bigger than the segment budget
# because a proxy is per clip rather than per press of Play, so the same
# handful of files stay useful for a whole session instead of turning over
# on every grade change. Measured on the 5s 4K test clip: 960 wide, CRF 18,
# 1.1MB. A cache of this size therefore holds hours of proxy.
PROXY_CACHE_MAX_BYTES = 1_500_000_000
PROXY_DEFAULT_WIDTH = 960

# CRF 18 rather than something visually lossless: measured against the 16-bit
# still path, CRF 12 costs 3.1x the bytes and buys a mean improvement of
# about 0.09 of 255, because the error is dominated by the 8-bit 4:2:0 round
# trip and not by the compression (a LOSSLESS 4:4:4 encode of the same frame
# still differs by mean 0.85/0.48/1.15 R/G/B). Spending bytes on that floor
# would be spending them on nothing.
PROXY_CRF = 18

# ~0.5s between keyframes at 24fps. Short on purpose: a seek has to decode
# from the previous keyframe, so this is what makes scrubbing cheap. It costs
# bitrate, which is the trade this file is happy to make.
PROXY_GOP = 12

# Chrome is the client, and how it converts Y'CbCr back to RGB is decided by
# the tags on the file, so this is a measured choice, not a preference. See
# the honesty entry in limits.js for the two numbers it was chosen on.
PROXY_RANGE = "full"

# In the cache key, so changing any of the encode decisions above cannot
# leave a stale proxy on disk that no longer matches what this code makes.
PROXY_VERSION = "1"

# proxy key -> job id, so a second prepare for a clip that is already
# encoding joins the running job instead of starting a duplicate ffmpeg.
_proxy_jobs: dict[str, str] = {}
_proxy_lock = threading.Lock()


def _proxy_path(key: str) -> Path:
    return PROXY / f"{key}.mp4"


# clip name -> the frame rate its FRAMES are spaced at, cached.
_proxy_fps_cache: dict[str, float] = {}


def _proxy_fps(clip: str, info: dict) -> float:
    """The rate the frames of this clip actually sit at, which is not always
    the rate clip_info reports.

    clip_info's fps comes from ffprobe's avg_frame_rate, which is frames
    divided by CONTAINER duration. Measured on this project's own footage:
    A001_09011832_C003.MOV has 744 frames spaced exactly 1/24s apart, but its
    container runs 32.254s, so avg_frame_rate reports 23.067. Frame index
    arithmetic on 23.067 drifts a whole frame within half a second, and a
    whole frame is what makes a scrub show a different picture on the proxy
    than on the still path (measured: mean difference 26 of 255 on a
    mismatched frame against 4.2 on a matched one).

    r_frame_rate is ffprobe's answer to "what is the smallest frame interval
    here", which is the number this arithmetic needs. It is nonsense on
    genuinely variable frame rate material (a screen recording tags 600), so
    it is only trusted when it is close to the average.
    """
    hit = _proxy_fps_cache.get(clip)
    if hit is not None:
        return hit
    avg = float(info.get("fps") or 24.0)
    fps = avg
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0",
             str(clip_path(clip))], capture_output=True, text=True, timeout=20).stdout
        # ffprobe's csv writer puts a trailing comma on this one-field
        # query ("24/1,"), so the fraction is picked out rather than split on.
        m = re.search(r"(\d+)\s*/\s*(\d+)", out)
        r = (float(m.group(1)) / float(m.group(2))) if m and float(m.group(2)) else 0.0
        if r > 0 and avg > 0 and r / avg <= 1.5:
            fps = r
    except Exception:                                         # noqa: BLE001
        pass
    _proxy_fps_cache[clip] = fps
    return fps


def _proxy_params(payload: dict, user_id=None) -> dict:
    """Resolve a proxy request into dimensions, duration and a cache key.

    No grade config anywhere in here on purpose: the proxy is the source, not
    the picture. That is what makes it one file per clip instead of one per
    grade, and what lets a knob turn during playback cost nothing.

    The rotation IS part of it, though: the proxy is encoded already turned,
    so it is one file per clip per rotation, and the rotation is in the key
    so a 90 request can never be served the auto file that is on disk.
    """
    clip = payload["clip"]
    rotation = effective_rotation(payload, user_id)
    info = clip_info(clip, rotation)
    rng = "limited" if str(payload.get("range") or PROXY_RANGE) == "limited" else "full"

    # Rounded to even BEFORE _preview_dims so the proxy asks that function the
    # same question the still path asks it: 4:2:0 needs even dimensions, and
    # sneaking the rounding in afterwards would hand the GPU a proxy one pixel
    # narrower than the still it is being compared against.
    width = max(160, int(payload.get("width") or PROXY_DEFAULT_WIDTH)) // 2 * 2
    pwidth, pheight = _preview_dims(info, width)
    if pwidth % 2:
        # Only reachable on a source whose own width is odd, where the clamp
        # inside _preview_dims wins. The still path would use pwidth+1 there.
        pwidth -= 1

    fps = _proxy_fps(clip, info)
    # Bounded even when the probe says nothing: an unbounded ffmpeg wrote
    # 2.6GB in this project once, and "the clip has no duration" is exactly
    # the case where that happens.
    duration = float(info.get("duration") or 0.0)
    if not duration > 0:
        duration = 60.0
    want = payload.get("duration")
    if want not in (None, "", 0):
        duration = max(1.0 / fps, min(duration, float(want)))

    matrix = CG.source_matrix(info)
    key = hashlib.sha1(json.dumps({
        "clip": clip, "w": pwidth, "h": pheight, "rot": rotation,
        "dur": round(duration, 3), "range": rng, "matrix": matrix,
        "src_range": info["color_range"], "crf": PROXY_CRF, "gop": PROXY_GOP,
        "v": PROXY_VERSION,
    }, sort_keys=True).encode()).hexdigest()

    return {"key": key, "clip": clip, "rotation": rotation, "range": rng,
            "width": pwidth, "height": pheight, "duration": duration,
            "fps": fps, "matrix": matrix, "info": info}


def _proxy_ffmpeg_args(params: dict, out_path: Path) -> list[str]:
    """The single bounded pass that writes one proxy.

    The filter chain is three deliberate steps:
      1. scale to the preview size with the same flags source_frame uses,
      2. the identical normalisation source_frame runs (clip matrix and clip
         range in, FULL range RGB out) so the pixels are in the GPU's domain,
      3. back to Y'CbCr with a bt709 matrix, which the -colorspace and
         -color_range tags below then tell the browser to undo.
    Step 3 is a lossy hop (8-bit, and 4:2:0 for the chroma) and it is the only
    place the proxy path differs from the still path. Its size is measured,
    not assumed: see the honesty entry.
    """
    info = params["info"]
    out_range = "full" if params["range"] == "full" else "limited"
    # Step 0, ahead of all three: the rotation, so the file the browser plays
    # is already the right way up and the width and height below (the
    # post-rotation size) are the size it is actually scaled to.
    vf = (f"{CG.rotate_prefix(info)}"
          f"scale={params['width']}:{params['height']}:flags=bilinear,setsar=1,"
          f"scale=in_color_matrix={params['matrix']}"
          f":in_range={info['color_range']}:out_range=full,format=gbrp16le,"
          f"scale=out_color_matrix=bt709:out_range={out_range},format=yuv420p")

    args = ["ffmpeg", "-v", "error", "-y"] + CG.rotate_args(params["rotation"])
    args += ["-i", str(clip_path(params["clip"]))]
    # An output option, after every -i, so it bounds the ENCODE and not just
    # the seek: the house rule for every ffmpeg call in this file.
    args += ["-t", str(params["duration"])]
    args += ["-vf", vf, "-an", "-sn", "-dn"]
    args += [
        "-c:v", "libx264", "-crf", str(PROXY_CRF), "-preset", "veryfast",
        # sc_threshold 0 with a fixed -g means the keyframes land on an exact
        # grid, so "how far back does a seek have to decode from" has one
        # answer (at most PROXY_GOP frames) instead of depending on the cut.
        "-g", str(PROXY_GOP), "-keyint_min", str(PROXY_GOP), "-sc_threshold", "0",
        "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        "-color_range", "pc" if params["range"] == "full" else "tv",
        # The moov atom up front: without it the browser has to fetch the end
        # of the file before it can play or seek at all.
        "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats",
        # Explicit, because the file is written to a ".partial" name (renamed
        # on success) and ffmpeg refuses to guess a muxer from that extension.
        "-f", "mp4", str(out_path),
    ]
    return args


def start_proxy(params: dict) -> Job:
    """Encode one proxy in the background, through the ordinary Job machinery
    so the jobs panel shows it and a stuck encode is visible rather than a
    Play button that silently never lights up.
    """
    key = params["key"]
    with _proxy_lock:
        existing = _proxy_jobs.get(key)
        if existing:
            job = JOBS.get(existing)
            if job is not None and job.status == "running":
                return job
    cache_path = _proxy_path(key)
    tmp_path = PROXY / f"{key}.{uuid.uuid4().hex[:8]}.partial"
    args = _proxy_ffmpeg_args(params, tmp_path)
    label = f"proxy {params['clip']} {params['width']}px"
    job = _register(Job("proxy", label))
    job.output = str(cache_path)
    total = max(0.1, params["duration"])
    with _proxy_lock:
        _proxy_jobs[key] = job.id

    def worker():
        try:
            with FFMPEG_SLOTS:
                job.proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, text=True)
                for line in job.proc.stdout:
                    line = line.strip()
                    if line.startswith("out_time_us="):
                        try:
                            secs = int(line.split("=", 1)[1]) / 1e6
                            job.progress = min(1.0, secs / total)
                            job.message = f"{secs:.1f}s of {total:.1f}s"
                        except ValueError:
                            pass
                job.proc.wait()
                err = job.proc.stderr.read()
            ok = job.proc.returncode == 0 and tmp_path.exists() \
                and tmp_path.stat().st_size > 0
            if job.status == "cancelled" or not ok:
                tmp_path.unlink(missing_ok=True)
                if job.status != "cancelled":
                    job.status = "failed"
                    job.message = (err or "ffmpeg failed").strip()[-600:]
            else:
                # Rename last: a reader either sees no file or sees a complete
                # one, never a half written mp4 the browser would fail on.
                tmp_path.replace(cache_path)
                _prune_cache_bytes("proxy", PROXY_CACHE_MAX_BYTES)
                job.status = "done"
                job.progress = 1.0
                size = cache_path.stat().st_size if cache_path.exists() else 0
                job.message = f"{size / 1e6:.1f} MB"
        except Exception as exc:                              # noqa: BLE001
            tmp_path.unlink(missing_ok=True)
            job.status = "failed"
            job.message = f"{exc}"
        finally:
            job.finished = time.time()

    threading.Thread(target=worker, daemon=True).start()
    return job


def proxy_state(params: dict) -> dict:
    """What /api/proxy/prepare answers: where the file is, and whether it is
    there yet. Starting the encode is this function's side effect, on purpose:
    the client asks one question ("can I play this clip") and gets either a
    URL it can use now or a job id it can watch.
    """
    key = params["key"]
    path = _proxy_path(key)
    ready = path.exists() and path.stat().st_size > 0
    job = None
    if ready:
        os.utime(path, None)
    else:
        job = start_proxy(params)
    out = {
        "key": key, "ready": ready,
        "url": f"/api/proxy/{key}.mp4",
        "width": params["width"], "height": params["height"],
        "duration": params["duration"], "fps": params["fps"],
        "range": params["range"], "crf": PROXY_CRF, "gop": PROXY_GOP,
        "source_width": params["info"]["width"],
        "source_height": params["info"]["height"],
        "bytes": path.stat().st_size if ready else 0,
    }
    if job is not None:
        out["job"] = job.as_dict()
    return out


# --------------------------------------------------------------------------
# masks (SAM 3.1): the plain proxy, the matte store, picking and tracking
#
# Contracts C2 (the matte store), C3 (the SAM service, sam/server.py, its own
# process on its own port), C4 (this section, and the routes below). The
# split that matters: the SAM service WRITES matte frames, this file only
# ever registers a matte (a bootstrap index.json the instant a track is
# accepted, before the service has written a single frame) and mirrors the
# service's own progress onto it while a background thread polls. Reading a
# matte once it exists is grade/mattes.py (M2, contract C6), imported as MT
# and not duplicated here.
# --------------------------------------------------------------------------

MASK_WORKING_WIDTH = int(os.environ.get("STUDIO_MASK_WIDTH") or 1280)


def set_mask_width(width: int) -> int:
    """--mask-width / STUDIO_MASK_WIDTH: the fixed width the plain proxy and
    every SAM call use, independent of whatever the browser happens to be
    previewing at (design rule 1: nothing the model reads is the picture).
    """
    global MASK_WORKING_WIDTH
    MASK_WORKING_WIDTH = max(160, int(width))
    return MASK_WORKING_WIDTH


MASK_PROXY = CACHE / "mask_proxy"
MASK_FRAME_CACHE = CACHE / "mask_frame"
MASK_PICK_CACHE = CACHE / "mask_pick"

# Smaller cap than the playback proxy: this file is per clip per rotation,
# never per grade, and nobody watches it, so a session touches at most a
# handful of them.
MASK_PROXY_CACHE_MAX_BYTES = 800_000_000
MASK_PROXY_CRF = 20
MASK_PROXY_GOP = 48
MASK_PROXY_VERSION = "1"

_mask_proxy_jobs: dict[str, str] = {}
_mask_proxy_lock = threading.Lock()


def _mask_proxy_path(key: str) -> Path:
    return MASK_PROXY / f"{key}.mp4"


def _mask_proxy_params(clip: str, rotation) -> dict:
    """Resolve a plain Rec.709 proxy request: fixed working width, the whole
    clip (a track names its own range later, at track time, not at proxy
    time), one file per clip per rotation.

    Deliberately not `_proxy_params`'s file: that one is keyed to whatever
    live.js's playback knobs currently ask for and is the picture, not the
    source. This mirrors its shape (same normalisation, same bounded ffmpeg
    pass, same Job machinery) so the mask routes get the same guarantees
    (bounded, cached, visible in the jobs panel) without inventing a second
    way to encode a clip.
    """
    rot = CG.normalise_rotation(rotation)
    info = clip_info(clip, rot)
    width, height = _preview_dims(info, MASK_WORKING_WIDTH)
    if width % 2:
        width -= 1
    fps = _proxy_fps(clip, info)
    duration = float(info.get("duration") or 0.0)
    if not duration > 0:
        duration = 3600.0          # bounded even when the probe says nothing
    matrix = CG.source_matrix(info)
    key = hashlib.sha1(json.dumps({
        "clip": clip, "w": width, "h": height, "rot": rot,
        "dur": round(duration, 3), "matrix": matrix,
        "src_range": info["color_range"], "v": MASK_PROXY_VERSION,
    }, sort_keys=True).encode()).hexdigest()
    return {"key": key, "clip": clip, "rotation": rot, "width": width,
            "height": height, "duration": duration, "fps": fps,
            "matrix": matrix, "info": info}


def _mask_proxy_ffmpeg_args(params: dict, out_path: Path) -> list[str]:
    """One bounded pass: the exact source_frame/flat_config normalisation,
    baked into a plain Rec.709 file with nothing studio specific in it. Full
    range in, tv range out, same as the playback proxy's own middle step;
    unlike that one this file is never shown to a person, so there is no
    "what does the browser assume" question and no range CHOICE to make.
    """
    info = params["info"]
    vf = (f"{CG.rotate_prefix(info)}"
          f"scale={params['width']}:{params['height']}:flags=bilinear,setsar=1,"
          f"scale=in_color_matrix={params['matrix']}"
          f":in_range={info['color_range']}:out_range=full,format=gbrp16le,"
          f"scale=out_color_matrix=bt709:out_range=limited,format=yuv420p")
    args = ["ffmpeg", "-v", "error", "-y"] + CG.rotate_args(params["rotation"])
    args += ["-i", str(clip_path(params["clip"]))]
    args += ["-t", str(params["duration"])]
    args += ["-vf", vf, "-an", "-sn", "-dn"]
    args += [
        "-c:v", "libx264", "-crf", str(MASK_PROXY_CRF), "-preset", "veryfast",
        "-g", str(MASK_PROXY_GOP), "-keyint_min", str(MASK_PROXY_GOP),
        "-sc_threshold", "0", "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        "-color_range", "tv", "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats", "-f", "mp4", str(out_path),
    ]
    return args


def start_mask_proxy(params: dict) -> Job:
    """Encode one plain proxy in the background, through the same Job
    machinery as everything else that shells out to ffmpeg (design rule 7:
    the queue is visible)."""
    key = params["key"]
    with _mask_proxy_lock:
        existing = _mask_proxy_jobs.get(key)
        if existing:
            job = JOBS.get(existing)
            if job is not None and job.status == "running":
                return job
    cache_path = _mask_proxy_path(key)
    tmp_path = MASK_PROXY / f"{key}.{uuid.uuid4().hex[:8]}.partial"
    args = _mask_proxy_ffmpeg_args(params, tmp_path)
    label = f"mask proxy {params['clip']} {params['width']}px"
    job = _register(Job("mask_proxy", label))
    job.output = str(cache_path)
    total = max(0.1, params["duration"])
    with _mask_proxy_lock:
        _mask_proxy_jobs[key] = job.id

    def worker():
        try:
            with FFMPEG_SLOTS:
                job.proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, text=True)
                for line in job.proc.stdout:
                    line = line.strip()
                    if line.startswith("out_time_us="):
                        try:
                            secs = int(line.split("=", 1)[1]) / 1e6
                            job.progress = min(1.0, secs / total)
                            job.message = f"{secs:.1f}s of {total:.1f}s"
                        except ValueError:
                            pass
                job.proc.wait()
                err = job.proc.stderr.read()
            ok = job.proc.returncode == 0 and tmp_path.exists() \
                and tmp_path.stat().st_size > 0
            if job.status == "cancelled" or not ok:
                tmp_path.unlink(missing_ok=True)
                if job.status != "cancelled":
                    job.status = "failed"
                    job.message = (err or "ffmpeg failed").strip()[-600:]
            else:
                tmp_path.replace(cache_path)
                _prune_cache_bytes("mask_proxy", MASK_PROXY_CACHE_MAX_BYTES)
                job.status = "done"
                job.progress = 1.0
                size = cache_path.stat().st_size if cache_path.exists() else 0
                job.message = f"{size / 1e6:.1f} MB"
        except Exception as exc:                              # noqa: BLE001
            tmp_path.unlink(missing_ok=True)
            job.status = "failed"
            job.message = f"{exc}"
        finally:
            job.finished = time.time()

    threading.Thread(target=worker, daemon=True).start()
    return job


def mask_proxy_state(params: dict) -> dict:
    key = params["key"]
    path = _mask_proxy_path(key)
    ready = path.exists() and path.stat().st_size > 0
    job = None
    if ready:
        os.utime(path, None)
    else:
        job = start_mask_proxy(params)
    out = {"key": key, "ready": ready, "path": str(path),
           "width": params["width"], "height": params["height"],
           "duration": params["duration"], "fps": params["fps"]}
    if job is not None:
        out["job"] = job.as_dict()
    return out


def ensure_mask_proxy_ready(clip: str, rotation, timeout: float = 600.0
                            ) -> tuple[Path, dict]:
    """Blocking wait for the plain Rec.709 proxy (design rule 6: made once
    by a bounded ffmpeg job). Called from a mask route's own handler thread,
    not from playback: this is the one place in this file allowed to wait on
    an encode, because a pick or a track cannot start without the file the
    SAM service is going to read.

    Cheap on every call after the first: `_mask_proxy_params` hashes the
    clip, rotation, size and encode settings into `key`, so a second call
    for the same clip finds the file already on disk and returns at once.
    """
    params = _mask_proxy_params(clip, rotation)
    state = mask_proxy_state(params)
    if state["ready"]:
        return _mask_proxy_path(state["key"]), params
    job_id = state["job"]["id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if job is None:
            raise StudioError("the plain proxy job disappeared before it finished")
        if job.status == "done":
            return _mask_proxy_path(params["key"]), params
        if job.status in ("failed", "cancelled"):
            raise StudioError(f"could not prepare the plain proxy for masking: "
                              f"{job.message or job.status}")
        time.sleep(0.2)
    raise HttpError(504, f"the plain proxy for {clip} did not finish within "
                    f"{timeout:.0f}s")


# --------------------------------------------------------------------------
# the matte store (contract C2). The SAM service writes matte frames; this
# file only ever writes an index.json (the bootstrap entry a track needs to
# exist before the service has written a single frame, and the progress
# mirror while it runs). Reading is entirely grade/mattes.py (MT), imported
# once at the top of this file and not duplicated here.
# --------------------------------------------------------------------------

_matte_index_locks: dict[str, threading.Lock] = {}
_matte_jobs: dict[str, str] = {}         # recipe hash -> this run's job id


def mask_clip_key(clip: str) -> str:
    """The content key a matte is filed under (C2): the same clip_key
    projects and saved grades already use, so a matte and a project agree on
    which clip they are about even across a rename or a move.
    """
    return GRADES.clip_key(clip_path(clip))


def _matte_dir(clip_key: str, matte_id: str) -> Path:
    return MT.matte_root() / clip_key / matte_id


def _write_matte_index(clip_key: str, matte_id: str, **fields) -> dict:
    """Atomic merge write of one matte's index.json.

    The SAM service is the normal writer of this file (contract C2): it is
    handed clip/clip_key/rotation/width/recipe on the wire and keeps state,
    done_frames and error current on its own. Studio calls this only for a
    matte the service's own response never described (an old or --stub
    service) and when the service goes unreachable mid job, where nothing
    else will ever write a terminal state. Read-modify-write under a lock
    keyed by the matte's own directory, so a concurrent write from this
    process cannot interleave into a half written JSON file. MT.forget_cache()
    afterwards, so the next MT.resolve() sees this write immediately instead
    of the dataclass cache from before it.
    """
    d = _matte_dir(clip_key, matte_id)
    d.mkdir(parents=True, exist_ok=True)
    lock = _matte_index_locks.setdefault(str(d), threading.Lock())
    with lock:
        p = d / MT.INDEX_NAME
        try:
            raw = json.loads(p.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            raw = {}
        raw.update(fields)
        raw.setdefault("matte_id", matte_id)
        raw.setdefault("clip_key", clip_key)
        raw.setdefault("created", time.time())
        tmp = d / f".{MT.INDEX_NAME}.{uuid.uuid4().hex[:8]}.tmp"
        tmp.write_text(json.dumps(raw, allow_nan=False))
        tmp.replace(p)
    MT.forget_cache()
    return raw


def _matte_info(matte_id: str):
    """One matte by id, or a 404, for every route that names one.

    The id is checked as an ID (MT.check_matte_id) before it is ever joined
    onto a path, and MT.resolve then refuses a directory that leaves the
    store. Both, not either: the pattern stops a traversal written into the
    URL and the containment check stops a symlink on disk. The check is
    repeated here rather than left to resolve() because this is the funnel
    every `matte/...` route goes through, and the route segment arrives
    straight off the wire (_dispatch does not unquote it, so a plain absolute
    path in the URL used to reach this function intact).
    """
    try:
        MT.check_matte_id(matte_id)
        return MT.resolve(MT.matte_root(), matte_id)
    except MT.MatteMissing as exc:
        raise HttpError(404, str(exc)) from exc


def _require_deletable_matte(info) -> None:
    """The last thing asked before DELETE /api/matte/<id> removes a directory.

    Everything else on that route can be a no-op: `_guard_read` and
    `_require_admin` both return immediately when logins are off, which is the
    documented default and the agent case, so with logins off this is the only
    thing between the request and an `rmtree`. It asks two questions of the
    RESOLVED PATH rather than of the id, so a symlink cannot answer them for
    somebody else's directory:

      is it inside the matte store, and does it look like a matte at all
      (an index.json, or at least one NNNNNN.png frame)

    A no to either is a 400 rather than a 404: the directory was found, and
    refusing to delete it is a statement about the request. This is deliberate
    belt and braces on top of MT.check_matte_id and MT.resolve's own
    containment check, because it is the one route in the studio that destroys
    data and the cost of asking twice is two `stat` calls.
    """
    root = MT.matte_root()
    path = Path(info.path)
    if not MT.is_under(root, path):
        raise HttpError(400, f"refusing to delete {path}: it is not inside "
                             f"the matte store at {root}")
    frames = "[0-9]" * MT.FRAME_DIGITS + ".png"
    if not (path / MT.INDEX_NAME).is_file() and not any(path.glob(frames)):
        raise HttpError(400, f"refusing to delete {path}: it holds no "
                             f"{MT.INDEX_NAME} and no matte frames, so it is "
                             f"not a matte this store wrote")


def _matte_clip_refusal(info, clip_key: str, clip_name: str = "") -> str | None:
    """The sentence to refuse with when a matte belongs to a different clip.

    index.json records the clip and the clip_key the track ran on (C2) and
    until now nothing outside the browser compared either with the clip in
    front of it: a landscape clip measured through a portrait matte tracked on
    another clip answered with numbers, no warning and exit 0, and a render
    stretched that matte over the wrong picture and wrote the file. The only
    thing that ever objected was the picker, which simply never offers a
    mismatched matte, so every scripted caller was unprotected.

    Compared on clip_key, the content key a matte, a project and a saved grade
    already share, so a rename or a move does not read as a mismatch. A matte
    with no clip_key recorded (a hand built fixture, an older service) cannot
    be checked and is allowed through: this can only ever refuse a matte that
    positively names a different clip.

    Returns None when there is nothing to refuse.
    """
    have = str(getattr(info, "clip_key", "") or "").strip()
    want = str(clip_key or "").strip()
    if not have or not want or have == want:
        return None
    return (f"matte {info.matte_id} was tracked on "
            f"{info.clip or have} and this is {clip_name or want}: a matte is "
            f"a per clip thing (a frame sequence at that clip's own rate and "
            f"framing), so using it here would stretch another clip's subject "
            f"over this picture. Track the subject on "
            f"{clip_name or want} and use that matte")


def _matte_infos_for_stack(mask: dict) -> list:
    """Every matte a mask STACK reaches, resolved, skipping what cannot be.

    Missing ids and ids that name nothing are somebody else's job: the stats
    route's own `_mask_stack_weight` 404s on them and the render's coverage
    rule refuses them by name. This is only here to answer "whose clip is
    this matte" about the ones that do resolve.
    """
    out = []
    try:
        layer = CG.mask_stack_layer(mask)
    except (CG.GradeError, StudioError, ValueError):
        return out
    for matte_id in CG.mask_stack_matte_ids(layer):
        if not MT.valid_matte_id(matte_id):
            continue
        try:
            out.append(MT.resolve(MT.matte_root(), matte_id))
        except MT.MatteError:
            continue
    return out


def _require_stats_mattes_match(clip, matte_info, mask_param) -> None:
    """Refuse a measurement weighted by another clip's matte (400).

    Both ways a measurement can be weighted: `matte: ID`, already resolved by
    the route, and `mask: {...}`, a component stack whose matte ids are
    resolved here. `clip` is a clip NAME; the `path` and `ref` forms of
    POST /api/stats carry no clip this server can key, so they are left alone
    rather than guessed at.
    """
    name = str(clip or "").strip()
    if not name:
        return
    try:
        key = mask_clip_key(name)
    except (StudioError, HttpError, OSError):
        return              # a name this server cannot resolve: the route's own error
    infos = [matte_info] if matte_info is not None else []
    if isinstance(mask_param, dict):
        infos += _matte_infos_for_stack(mask_param)
    for info in infos:
        message = _matte_clip_refusal(info, key, name)
        if message:
            raise StudioError(message)


def _require_render_mattes_match(cfg: dict, clip) -> None:
    """Refuse a render whose mask reaches another clip's matte (400).

    Same shape as CG.require_complete_mattes' refusal, deliberately: one line
    per bad component naming the layer, the component and both clips, so the
    message says what to fix without opening index.json. Runs for both engines
    and whatever `allow_partial` says, because allow_partial means "I accept an
    unfinished matte", never "I accept the wrong clip's matte".
    """
    try:
        key = mask_clip_key(clip)
    except (StudioError, HttpError, OSError):
        return
    bad = []
    for entry in CG.mask_inputs(cfg):
        if entry.get("kind") != "matte":
            continue
        matte_id = str((entry.get("matte") or {}).get("id") or "").strip()
        if not MT.valid_matte_id(matte_id):
            continue        # no id yet, or an unusable one: the coverage rule's job
        try:
            info = MT.resolve(MT.matte_root(), matte_id)
        except MT.MatteError:
            continue        # not in the store: the coverage rule's job too
        message = _matte_clip_refusal(info, key, str(clip))
        if message:
            bad.append(f"  layer {entry['layer']} component "
                       f"{entry['component']}: {message}")
    if bad:
        raise StudioError("this render's mask reaches a matte that was tracked "
                          "on another clip:\n" + "\n".join(bad))


def list_all_mattes() -> list:
    root = MT.matte_root()
    out = []
    if not root.is_dir():
        return out
    for clip_dir in root.iterdir():
        if not clip_dir.is_dir():
            continue
        for matte_dir in clip_dir.iterdir():
            if not (matte_dir / MT.INDEX_NAME).is_file():
                continue
            try:
                out.append(MT.info_from_dir(matte_dir, matte_dir.name))
            except MT.MatteError:
                continue
    return out


def list_clip_mattes(clip_key: str) -> list:
    root = MT.matte_root() / clip_key
    out = []
    if not root.is_dir():
        return out
    for matte_dir in root.iterdir():
        if not (matte_dir / MT.INDEX_NAME).is_file():
            continue
        try:
            out.append(MT.info_from_dir(matte_dir, matte_dir.name))
        except MT.MatteError:
            continue
    return out


def _mean_of(values) -> float | None:
    good = [float(v) for v in (values or []) if v is not None]
    return round(sum(good) / len(good), 6) if good else None


def _matte_summary(info, full: bool = True, quality_limit: int | None = 64
                   ) -> dict:
    """One matte's record for `GET /api/matte/<id>` and the list route.

    `full` decides whether the per frame arrays come with it (checkpoint gap
    6): `GET /api/matte/<id>` is one matte and always sends them, since
    `mask show --strip` plots the area curve from exactly this response.
    `GET /api/matte?clip=` defaults to the summary and only sends the arrays
    for `?full=1`, because a clip with four mattes over 384 frames was
    answering with more than 3000 mostly-null numbers just to report four
    states, which is why M8 polled `mask jobs` instead of this route.

    `span`, `coverage` and `frozen_outside_span` (checkpoint gaps 8 and 10)
    say which seconds the matte really answers for and state, rather than
    imply, that it holds its nearest written frame outside them. `quality`
    (gap 18) is the per frame flag summary; `suspect_frames` inside it is
    capped by `quality_limit` so a list route cannot answer with a hundred
    rows per matte, with `truncated` saying when that happened.
    """
    sp = MT.span(info)
    span_len = max(0, sp["end_frame"] - sp["start_frame"])
    out = {
        "matte_id": info.matte_id, "clip": info.clip, "clip_key": info.clip_key,
        "rotation": info.rotation, "fps": info.fps, "frames": info.frames,
        "width": info.width, "height": info.height, "state": info.state,
        "done_frames": info.done_frames, "written_count": info.written_count,
        "total_frames": info.total_frames, "is_partial": info.is_partial,
        "recipe": info.recipe,
        "created": info.created, "model": info.model, "backend": info.backend,
        "span": sp,
        "frozen_outside_span": True,
        "coverage": (round(sp["written"] / span_len, 4) if span_len else 0.0),
        "mean_score": _mean_of(info.scores),
        "mean_area": _mean_of(info.areas),
        "quality": MT.quality(info, limit=quality_limit),
        # Not one of MatteInfo's own dataclass fields; index.json carries it
        # straight from the service on a failed matte ("no instance for text
        # 'shirt'"), and a caller (the jobs panel, a UI badge) needs it to
        # say more than just "failed".
        "object_id": info.raw.get("object_id"), "label": info.raw.get("label"),
        "kind": info.raw.get("kind"), "error": info.raw.get("error"),
    }
    if full:
        out["areas"] = info.areas
        out["scores"] = info.scores
        out["ious"] = info.raw.get("ious")
    return out


def matte_frame_png(matte_id: str, time_s: float, width: int | None
                    ) -> tuple[bytes, dict]:
    """PNG bytes and headers for GET /api/matte/<id>/frame (C4): the matte at
    a moment, with M2's own nearest-written-frame fallback for a still
    running (partial) matte.
    """
    info = _matte_info(matte_id)
    size = None
    if width:
        w = max(1, int(width))
        h = max(1, round(w * info.height / max(1, info.width)))
        size = (w, h)
    try:
        arr, served, warning = MT.load_time(info, time_s, size)
    except MT.MatteMissing as exc:
        raise HttpError(404, str(exc)) from exc
    tmp = MASK_FRAME_CACHE / f"_tmp_{uuid.uuid4().hex}.png"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        MT.write_gray_png(tmp, arr)
        png = tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)
    headers = {"X-Matte-State": info.state, "X-Matte-Frame": str(served)}
    if warning:
        headers["X-Matte-Warning"] = warning
    return png, headers


# --------------------------------------------------------------------------
# recipes, cached (design rule 5): a track is keyed by clip identity,
# rotation, the working width and the recipe itself, so the same request
# never runs twice. `<clip-key>/_recipe_cache.json` maps that hash to the
# matte ids it produced, alongside (not instead of) the matte directories
# themselves, because a text prompt can come back as more than one instance
# and the service, not this file, decides how many.
# --------------------------------------------------------------------------

_RECIPE_CACHE_NAME = "_recipe_cache.json"


def _recipe_cache_path(clip_key: str) -> Path:
    return MT.matte_root() / clip_key / _RECIPE_CACHE_NAME


def _norm_prompts(prompts) -> dict:
    """Only the fields C3's prompts object defines, in a stable order, so a
    hash built from this never differs because of dict insertion order or an
    extra unknown field a client happened to pass through.
    """
    if not isinstance(prompts, dict):
        return {}
    out = {}
    if prompts.get("text"):
        out["text"] = list(prompts["text"])
    if prompts.get("points"):
        out["points"] = prompts["points"]
    if prompts.get("boxes"):
        out["boxes"] = prompts["boxes"]
    if prompts.get("exemplars"):
        out["exemplars"] = prompts["exemplars"]
    return out


def _normalise_track_recipe(prompts, select, steady) -> dict:
    return {"prompts": _norm_prompts(prompts),
            "select": select if select is not None else "all",
            "steady": int(steady) if steady else None}


def _recipe_hash(clip_key: str, rotation: str, recipe: dict) -> str:
    payload = json.dumps({"clip_key": clip_key, "rotation": rotation,
                          "width": MASK_WORKING_WIDTH, "recipe": recipe},
                         sort_keys=True)
    return hashlib.sha1(payload.encode()).hexdigest()[:20]


def _recipe_cache_get(clip_key: str, rhash: str) -> list[str] | None:
    p = _recipe_cache_path(clip_key)
    try:
        raw = json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    ids = raw.get(rhash)
    if not ids:
        return None
    good = []
    for mid in ids:
        try:
            good.append(MT.resolve(MT.matte_root(), mid).matte_id)
        except MT.MatteMissing:
            continue           # a matte this cache remembers was deleted since
    return good or None


def _request_frame_window(params: dict, start, end) -> tuple[int, int, int]:
    """`(total_frames, start_frame, end_frame)` for a track request, in the
    frame numbering of the clip's own mask proxy.

    One function, called both by the cache decision and by the wire call, so
    "the window this call asked for" can never mean two different ranges in
    one request. `end_frame` is an EXCLUSIVE upper bound (C3), and both ends
    are clamped to the clip's real frame count, which is what keeps a request
    for more than the clip has (`--end 20` on a 16 second clip) from reading
    as a permanently missing tail and re-queueing on every repeat call.

    Round 1 finding 5: the clamp used to be against the MATTE's own declared
    window instead of the clip's. That made every wider ask look already
    covered, so `mask track --end 6` after `--end 2` answered `cached: true`
    and the matte still stopped at 2 seconds. Clamping to the clip keeps the
    "more than the clip has" protection and drops the false coverage.
    """
    fps = float(params.get("fps") or 0.0)
    total = max(1, int(round(float(params.get("duration") or 0.0) * fps)))
    start_frame = 0
    if start is not None:
        start_frame = min(total, max(0, int(round(float(start) * fps))))
    end_frame = total
    if end is not None:
        end_frame = min(total, max(start_frame, int(round(float(end) * fps))))
    return total, start_frame, end_frame


def _matte_declared_window(info) -> tuple[int, int]:
    """The `[start_frame, end_frame)` this matte says it is for.

    From index.json when the service wrote it (it always does, C2), falling
    back to `[0, frames)` for a matte written by hand or by an older service.
    """
    raw = info.raw or {}
    m_start = int(raw.get("start_frame") or 0)
    m_end = int(raw.get("end_frame") or info.total_frames or 0)
    return m_start, max(m_start, m_end)


def _matte_reaches_past(info, w_start: int, w_end: int) -> bool:
    """True when `[w_start, w_end)` asks for frames outside this matte's own
    declared window, at either end: the request is WIDER than the matte, so
    no amount of what is on disk can answer it (round 1 finding 5)."""
    m_start, m_end = _matte_declared_window(info)
    return w_start < m_start or w_end > m_end


def _matte_first_missing(info, w_start: int, w_end: int) -> int | None:
    """The first frame of `[w_start, w_end)` this matte has not written, or
    None when every frame of it is on disk.

    Frame indices, not seconds: the caller resolves the request to frames
    once (`_request_frame_window`) so the cache decision and the track call
    cannot disagree about what was asked for.
    """
    written = set(info.written_indices(refresh=True))
    for i in range(int(w_start), int(w_end)):
        if i not in written:
            return i
    return None


# States that mean "this matte will never finish on its own". `cancelled` is
# not one of C2's own states: a cancelled job settles as `partial` when it
# wrote something and `failed` (error "cancelled") when it did not, which is
# why both spellings are here and why `partial` alone is not enough to judge
# on (a partial matte that covers the window asked for is fine, checkpoint
# gap 12 and the render refusal).
# Round 1 finding 21: `stale` is in this tuple for callers that only ask "is
# this matte finished", but the track cache decision in `queue_mask_track`
# judges `stale` BEFORE coverage and before this tuple, so a stale matte that
# happens to cover the window still gets re-tracked with its own message
# ("the recipe changed") instead of the generic dead one.
_MATTE_DEAD_STATES = ("failed", "stale", "cancelled")


def _recipe_cache_put(clip_key: str, rhash: str, matte_ids: list) -> None:
    d = MT.matte_root() / clip_key
    d.mkdir(parents=True, exist_ok=True)
    lock = _matte_index_locks.setdefault(str(d) + "/_recipe_cache",
                                         threading.Lock())
    with lock:
        p = _recipe_cache_path(clip_key)
        try:
            raw = json.loads(p.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            raw = {}
        raw[rhash] = [str(m) for m in matte_ids]
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, allow_nan=False))
        tmp.replace(p)


# --------------------------------------------------------------------------
# picking and tracking (C3/C4)
# --------------------------------------------------------------------------

PICKS: dict[str, dict] = {}
PICKS_LOCK = threading.Lock()
PICKS_MAX = 64


def _tint_overlay(rgb: np.ndarray, mask01: np.ndarray,
                  color=(0, 200, 255), alpha: float = 0.5) -> np.ndarray:
    """A quick preview: `rgb` (uint8 HxWx3) with `mask01` (0..1 HxW) tinted
    on top, so a pick's instances are tellable apart at a glance without the
    caller decoding a raw greyscale matte itself.
    """
    m = np.clip(mask01, 0.0, 1.0).astype(np.float32)[..., None]
    tint = np.array(color, dtype=np.float32)
    out = rgb.astype(np.float32) * (1.0 - m * alpha) + tint * (m * alpha)
    return np.clip(out, 0, 255).astype(np.uint8)


def mask_segment(clip: str, time_s: float, rotation, prompts: dict,
                 max_instances=None) -> dict:
    """POST /api/mask/segment (C4): one frame, synchronous. The plain
    Rec.709 frame at the working width (flat_config: the technical
    conversion and nothing creative, same definition the rest of the studio
    already uses for "before"), handed to the SAM service, instances handed
    back with an overlay and a raw mask preview a caller can look at before
    spending a track on one of them.
    """
    rot = CG.normalise_rotation(rotation)
    cfg = flat_config(full_config({}), False)
    rgb, _meta = render_region(clip, float(time_s), MASK_WORKING_WIDTH, cfg,
                               rot, None, None)
    norm_prompts = _norm_prompts(prompts)
    if not norm_prompts:
        raise StudioError("mask segment needs at least one prompt: text, "
                          "points, boxes or exemplars")
    frame_key = uuid.uuid4().hex[:12]
    MASK_FRAME_CACHE.mkdir(parents=True, exist_ok=True)
    frame_path = MASK_FRAME_CACHE / f"{frame_key}.jpg"
    frame_path.write_bytes(encode_jpeg(rgb, f"mask_pick_{frame_key}"))
    try:
        resp = SAMC.client().segment(str(frame_path), norm_prompts, max_instances)
    except SAMC.SamUnavailable as exc:
        raise HttpError(503, str(exc)) from exc
    except SAMC.SamError as exc:
        raise StudioError(f"the SAM service refused this pick: {exc}") from exc
    # The pick id is the SAM service's own (it keeps the last 32 in memory,
    # per C3): a track later naming this pick has to send back the exact id
    # the service gave out, not one studio made up itself, or the service
    # has never heard of it. Only a bare `uuid4` fallback for an old or stub
    # response that omits pick_id, so this route still answers something.
    pick_id = str(resp.get("pick_id") or uuid.uuid4().hex[:12])
    entry = {"clip": clip, "rotation": rot, "time": float(time_s),
             "prompts": norm_prompts, "frame": str(frame_path),
             "created": time.time(), "instances": {}}
    instances_out = []
    MASK_PICK_CACHE.mkdir(parents=True, exist_ok=True)
    for inst in (resp.get("instances") or []):
        inst_id = str(inst.get("id"))
        mask_path = inst.get("mask")
        overlay_url = mask_url = None
        if mask_path and Path(str(mask_path)).is_file():
            mask_arr = MT.read_gray_png(Path(str(mask_path)))
            full = 65535.0 if mask_arr.dtype == np.uint16 else 255.0
            mask01 = mask_arr.astype(np.float32) / full
            if mask01.shape != rgb.shape[:2]:
                mask01 = MT.resize_bilinear(mask01, rgb.shape[1], rgb.shape[0])
            overlay = _tint_overlay(rgb, mask01)
            overlay_path = MASK_PICK_CACHE / f"{pick_id}_{inst_id}_overlay.jpg"
            mask_out_path = MASK_PICK_CACHE / f"{pick_id}_{inst_id}_mask.png"
            overlay_path.write_bytes(
                encode_jpeg(overlay, f"mask_pick_{pick_id}_{inst_id}_ov"))
            MT.write_gray_png(mask_out_path, mask01)
            entry["instances"][inst_id] = {"overlay": str(overlay_path),
                                           "mask": str(mask_out_path)}
            overlay_url = f"/api/mask/pick/{pick_id}/{inst_id}/overlay"
            mask_url = f"/api/mask/pick/{pick_id}/{inst_id}/mask"
        instances_out.append({"id": inst_id, "score": inst.get("score"),
                              "box": inst.get("box"), "area": inst.get("area"),
                              "overlay": overlay_url, "mask": mask_url})
    with PICKS_LOCK:
        PICKS[pick_id] = entry
        if len(PICKS) > PICKS_MAX:
            oldest = sorted(PICKS.items(), key=lambda kv: kv[1]["created"])
            for pid, _entry in oldest[:-PICKS_MAX]:
                PICKS.pop(pid, None)
    out = {"pick_id": pick_id, "instances": instances_out,
           "candidates": len(instances_out),
           "elapsed_s": resp.get("elapsed_s")}
    # Checkpoint gap 3: an empty list said nothing about WHY it was empty,
    # so "the model looked and found nothing" and "this phrase meant
    # nothing to the model" read identically. The service's own warnings
    # come through unchanged; `message` is the one sentence a caller can
    # print, and the CLI exits non zero on it.
    if resp.get("warnings"):
        out["warnings"] = list(resp["warnings"])
    if not instances_out:
        asked = ", ".join(f'"{t}"' for t in (norm_prompts.get("text") or []))
        if not asked:
            kinds = [k for k in ("points", "boxes", "exemplars")
                     if norm_prompts.get(k)]
            asked = " + ".join(kinds) or "these prompts"
        out["message"] = (
            f"no match for {asked} at {float(time_s):g}s: 0 candidates from "
            f"the model. Try another word for the same thing, a different "
            f"--time, or a --point/--box prompt on the pixels themselves.")
    return out


_POLL_INTERVAL = 1.0


def _poll_sam_job(job: Job, clip_key: str, sam_job_id) -> None:
    """Mirrors the SAM service's own job state onto the studio Job (visible
    in the jobs panel, design rule 7: the queue is visible). Runs until the
    service reports a terminal job state, or stops answering.

    Deliberately does NOT write a matte's own index.json in the normal case:
    the SAM service is the sole writer of that file (contract C2, "clip,
    clip_key, rotation, recipe stored verbatim by the service", "index.json
    rewritten atomically... while a job runs"), and `queue_mask_track`
    already handed the service everything it needs to do that from the
    start. Two writers on one file is exactly the race this avoids. The one
    exception is when the service itself goes unreachable mid job: nothing
    else will ever finalise that matte's state then, so this falls back to
    writing "failed" itself, same as `_write_matte_index`'s docstring always
    described as its purpose.
    """
    matte_ids = job.extra.get("matte_ids", [])
    if not sam_job_id:
        # A --stub or a synchronous backend can finish inside the enqueue
        # call itself and hand back no job id at all: nothing to poll, the
        # track is already done. queue_mask_track already trusted the
        # service's own synchronous response for the matte index in this
        # case (or wrote a fallback bootstrap entry if the response did not
        # carry one), so this is just the studio Job bookkeeping.
        job.status = "done"
        job.progress = 1.0
        job.finished = time.time()
        return
    while True:
        if job.status == "cancelled":
            try:
                SAMC.client().job_cancel(sam_job_id)
            except (SAMC.SamUnavailable, SAMC.SamError) as exc:
                # Could not even ask the service to stop: nobody else will
                # ever finalise this matte's state, so studio has to.
                job.message = str(exc)
                job.finished = time.time()
                for mid in matte_ids:
                    _write_matte_index(clip_key, mid, state="failed",
                                       error=f"could not cancel: {exc}")
                return
            # Cancellation was accepted; keep polling below for the terminal
            # state the service settles on (partial if it wrote any frame,
            # failed with error "cancelled" otherwise, per C3), rather than
            # guessing it here. "cancelled" is checked again each loop, so a
            # second cancel request while this is still running is harmless.
        try:
            state = SAMC.client().job(sam_job_id)
        except (SAMC.SamUnavailable, SAMC.SamError) as exc:
            job.status = "failed"
            job.message = str(exc)
            job.finished = time.time()
            for mid in matte_ids:
                _write_matte_index(clip_key, mid, state="failed", error=str(exc))
            return
        st = str(state.get("state") or "running")
        done_frames = state.get("done_frames") or 0
        total_frames = state.get("total_frames") or 0
        job.message = f"{done_frames} of {total_frames} frames"
        if total_frames:
            job.progress = min(1.0, done_frames / total_frames)
        job.extra["sam_state"] = state
        # Per matte state, kept for the jobs panel only (_mask_job_view):
        # one text slot can fail ("no instance for text 'shirt'") while its
        # siblings finish, and the job as a whole is not "failed" for that.
        # The matte's own GET /api/matte/<id> reads index.json, which the
        # service is keeping current on its own.
        if state.get("mattes"):
            job.extra["mattes"] = state["mattes"]
        if st in ("done", "failed", "cancelled"):
            job.status = "done" if st == "done" else "failed"
            if st == "done":
                job.progress = 1.0
            job.finished = time.time()
            return
        time.sleep(_POLL_INTERVAL)


def queue_mask_track(clip: str, rotation, prompts=None, select=None,
                     steady=None, pick_id=None, start=None, end=None,
                     force: bool = False) -> dict:
    """POST /api/mask/track's implementation (C3/C4).

    Two request shapes fold into one recipe: a bare prompts dict (the
    portable, auto-queueable text case, design rule 8) and a pick_id +
    select (the pixel specific case). The pick's own remembered prompts are
    kept in the STORED recipe (index.json, the recipe cache) so the recipe
    stays remakeable after this process restarts, but the WIRE call to the
    SAM service sends `pick` + `select`, not those prompts again: a pick
    names the exact instance a person already chose from `/segment`'s own
    boxes, and re-sending prompts would ask the service to detect fresh
    instead of tracking the one already picked (per C3, "either prompts or
    pick + select").

    `start`/`end` are seconds, matching what `Studio.track()` sends; frame
    indices for the wire call are computed from the mask proxy's own fps
    once it is ready, clamped to the proxy's actual frame range.

    Synchronous up to the point the SAM service has ENQUEUED the job (its own
    /track call is meant to be quick, "queued, not run inline", per C3): this
    call blocks for that, plus for the plain proxy if this is the first
    request for this clip and rotation, then returns. The actual multi-frame
    tracking work happens on a background thread this function starts before
    returning. The matte's own index.json is written by the SAM service
    itself (contract C2: clip, clip_key, rotation, width and recipe are
    handed to it on the wire so it can do that from the start), not by
    studio; the one exception is a matte the response did not describe at
    all, when a fallback bootstrap write here is the only thing keeping
    GET /api/matte/<id> from 404ing on something the track call did accept.

    Returns {"job_id": str|None, "mattes": [...]}, plus `cached: true` on a
    pure cache hit (design rule 5: that call cost nothing, there is no fresh
    job, and the caller reads the existing matte's own state instead) or
    `resumed`/`widened`/`restarted` with a `message` when this call picked a
    stalled or too-short matte back up. job_id is None only on a cache hit,
    and a cache hit means every frame of the window THIS call asked for is on
    disk (or a live job is tracking a window that contains it), never merely
    that a matte for this recipe exists: `--end 6` after `--end 2` widens the
    matte rather than reporting the shorter one as already covering the
    request (round 1 finding 5).

    `force` is the full redo (checkpoint gap 12): the previously written
    frames for this recipe are deleted and the whole window tracked again,
    for the case where the matte on disk is not wanted at all rather than
    merely unfinished.
    """
    rot = CG.normalise_rotation(rotation)
    clip_key = mask_clip_key(clip)
    use_pick = None
    if pick_id:
        with PICKS_LOCK:
            entry = PICKS.get(str(pick_id))
        if not entry:
            raise StudioError(f"no such pick: {pick_id}")
        prompts = entry.get("prompts") or {}
        use_pick = str(pick_id)
    recipe = _normalise_track_recipe(prompts, select, steady)
    p = recipe["prompts"]
    if not (p.get("text") or p.get("points") or p.get("boxes") or p.get("exemplars")):
        raise StudioError("mask track needs at least one prompt (text, "
                          "points, boxes or exemplars) or a pick_id")
    rhash = _recipe_hash(clip_key, rot, recipe)

    cached_ids = _recipe_cache_get(clip_key, rhash)
    cached_infos = []
    for mid in (cached_ids or []):
        try:
            cached_infos.append(MT.resolve(MT.matte_root(), mid))
        except MT.MatteMissing:
            continue

    # The window this call asks for, in frames, resolved ONCE from the clip's
    # own mask proxy parameters. `_mask_proxy_params` is a cached probe and no
    # encode, so reading it here (before the cache decision) costs nothing;
    # `ensure_mask_proxy_ready` below returns the same dict and the same
    # numbers, which is why the cache decision and the wire call cannot
    # disagree about what "this request" means.
    req_params = _mask_proxy_params(clip, rot)
    _, req_start, req_end = _request_frame_window(req_params, start, end)

    # Checkpoint gap 12, and round 1 findings 5 and 21. The cache used to hand
    # back whatever it remembered with no look at that matte's own state, so a
    # cancelled or failed track answered an identical retry with the same dead
    # matte and job_id None, and the only way to get a fresh attempt was to
    # change the prompt text. Now the cache is a hit only while the answer is
    # still usable, and the order the questions are asked in is the fix:
    #
    #   stale                             RESTART: stale means "made for
    #                                     something else", so it is wrong
    #                                     however many frames it holds. Read
    #                                     FIRST, before coverage, or a stale
    #                                     matte that happens to cover the
    #                                     window is served as a cache hit and
    #                                     grades the wrong clip (finding 21).
    #   a live job covering this window    hit (asking twice is free, and the
    #                                     work is already happening)
    #   every frame of the window written hit, whatever the state says
    #   failed / cancelled                RESTART: re-queue the whole window
    #   a window WIDER than the matte     WIDEN: re-queue the frames outside
    #                                     the matte's own declared range,
    #                                     keeping the ones inside it
    #   a gap in the window               RESUME: re-queue from the first
    #                                     missing frame, keeping every frame
    #                                     already on disk
    #   force                             RESTART, frames deleted first
    #
    # The dead states are read BEFORE the gap, not after: a failed matte has
    # a gap by definition (that is what failing means), so testing the gap
    # first would resume every dead matte from its first missing frame and
    # the RESTART row could never be reached. A matte that is merely partial
    # or stalled, which is how a cancelled job that wrote something settles,
    # is not a dead state and does resume.
    #
    # A resume, a widen and a restart all go through the same track call below
    # with `matte_ids` naming the existing mattes, so the frames land back in
    # the same matte and the state moves to queued/running again instead of
    # staying dead.
    resume_from = None
    resume_kind = ""       # "resume", "widen", "restart" or "force"
    restart_reason = ""
    resume_ids: dict[str, str] = {}
    if cached_infos and not force:
        live = JOBS.get(_matte_jobs.get(rhash) or "")
        live_now = live is not None and live.status in ("queued", "running")
        # Only a live job whose OWN window covers this request answers it. A
        # job tracking [0, 48) is not an answer to a request for [0, 144):
        # counting it as one is finding 5 again, one step further along. Jobs
        # registered before this field existed (a server upgraded under a
        # running job) read as covering, which is the old behaviour and the
        # safe direction: it can delay a widen by one job, never lose one.
        live_window = (live.extra or {}).get("window") if live_now else None
        live_covers = live_now and (
            live_window is None
            or (int(live_window[0]) <= req_start and int(live_window[1]) >= req_end))
        stale = [i for i in cached_infos if i.state == "stale"]
        dead = [i for i in cached_infos if i.state in _MATTE_DEAD_STATES
                and i.state != "stale"]
        wider = [i for i in cached_infos
                 if _matte_reaches_past(i, req_start, req_end)]
        gaps = [g for g in (_matte_first_missing(i, req_start, req_end)
                            for i in cached_infos) if g is not None]
        # "Covered" is frames really on disk, not merely an absence of gaps:
        # a matte whose declared window is empty (a bootstrap index written
        # before the service said how long the track is) has no missing
        # frame to find, and must not read as a hit on that technicality.
        covered = not gaps and all(i.written_count for i in cached_infos)
        if stale:
            resume_from = req_start
            resume_kind = "restart"
            restart_reason = (
                f"restarting from frame {resume_from}: "
                f"{len(stale)} matte(s) are stale (tracked for a different "
                f"clip, rotation or working width than this request)")
        elif live_covers or covered:
            return {"job_id": _matte_jobs.get(rhash), "cached": True,
                    "start_frame": req_start, "end_frame": req_end,
                    "mattes": [{"matte_id": i.matte_id, "recipe": i.recipe,
                                "state": i.state} for i in cached_infos]}
        elif dead:
            resume_from = req_start
            resume_kind = "restart"
            restart_reason = (
                f"restarting from frame {resume_from}: "
                f"{', '.join(sorted({i.state for i in dead}))}")
        elif wider:
            # Round 1 finding 5. This is the ask that used to be answered
            # `cached: true` with the words "already covers this request"
            # while the matte stopped a third of the way in.
            resume_from = min(gaps) if gaps else req_start
            resume_kind = "widen"
            widest = [f"{i.matte_id} covers frames "
                      f"{_matte_declared_window(i)[0]} to "
                      f"{_matte_declared_window(i)[1]}" for i in wider]
            restart_reason = (
                f"widening from frame {resume_from}: this request wants frames "
                f"{req_start} to {req_end} and {'; '.join(widest)}")
        elif not gaps:
            resume_from = req_start
            resume_kind = "restart"
            restart_reason = (
                f"restarting from frame {resume_from}: "
                f"{', '.join(sorted({i.state for i in cached_infos}))}")
        else:
            resume_from = min(gaps)
            resume_kind = "resume"
            restart_reason = (f"resuming from frame {resume_from}: "
                              f"{', '.join(sorted({i.state for i in cached_infos}))}")
    elif cached_infos and force:
        # A full redo. The service derives the same matte id for the same
        # recipe and window, so without clearing the frames first a shorter
        # re-track would leave the old attempt's tail sitting on disk and
        # reading as tracked. The index goes with them; the track call below
        # writes a fresh one.
        for i in cached_infos:
            shutil.rmtree(i.path, ignore_errors=True)
        MT.forget_cache()
        resume_kind = "force"
        restart_reason = "force: previous frames cleared, tracking again"

    if cached_infos:
        for i in cached_infos:
            oid = str(i.raw.get("object_id") or "").strip()
            if oid:
                resume_ids[oid] = i.matte_id

    path, params = ensure_mask_proxy_ready(clip, rot)
    out_dir = MT.matte_root() / clip_key
    # end_frame is an EXCLUSIVE upper bound (C3: "frames (=end_frame,
    # exclusive upper bound)"), so the full clip is [0, total_frames), not
    # [0, total_frames - 1] as an inclusive last index would read. Resolved by
    # the same helper the cache decision above used, against the same params
    # dict, so the window this call asked for is one number pair and not two
    # derivations that can drift apart.
    total_frames, start_frame, end_frame = _request_frame_window(params, start, end)
    if resume_from is not None:
        # Re-queue the missing part only, and keep the matte's own declared
        # end so its `frames` (and therefore its area/score arrays, and
        # every index a caller already holds) do not move under it.
        declared_end = max([int((i.raw or {}).get("end_frame") or i.total_frames)
                            for i in cached_infos] + [end_frame])
        start_frame = max(0, min(int(resume_from), total_frames))
        end_frame = min(total_frames, max(start_frame, declared_end))
    try:
        resp = SAMC.client().track(
            str(path), str(out_dir), fps=params["fps"],
            start_frame=start_frame, end_frame=end_frame,
            prompts=None if use_pick else recipe["prompts"], pick=use_pick,
            select=recipe["select"], steady=recipe["steady"],
            clip=clip, clip_key=clip_key, rotation=rot,
            # `clip` above is the bare name stored verbatim in the matte's
            # own index.json (contract C2, and test_matte_list_get_and_frame
            # pins that shape); it is not a path the SAM service can ffprobe
            # from its own working directory. `rotation_probe_path` is a
            # SEPARATE field, the real absolute file this clip actually is,
            # purely so the service's rotation="auto" resolution
            # (sam/server.py's resolve_rotation, C3) can read the clip's own
            # display-matrix tag when studio sends "auto" (its own default,
            # effective_rotation()) rather than a concrete quarter turn.
            # Without this a track queued at the studio's default rotation
            # wrote a matte whose index.json rotation (silently resolved to
            # 0, the fallback for a tag that could not be probed) disagreed
            # with the working proxy's own portrait pixels: found and fixed
            # in INTEGRATION-A while cross checking `stats --matte` against
            # a real tracked matte end to end.
            rotation_probe_path=str(clip_path(clip)),
            width=params["width"], recipe=recipe,
            # Empty on a first track; on a resume or a restart it names the
            # mattes to write back into, so the frames that survived stay
            # where they are instead of being orphaned in an old directory
            # (checkpoint gap 12).
            matte_ids=resume_ids or None)
    except SAMC.SamUnavailable as exc:
        raise HttpError(503, str(exc)) from exc
    except SAMC.SamError as exc:
        raise StudioError(f"the SAM service refused this track: {exc}") from exc

    sam_job_id = resp.get("job_id")
    sam_mattes = resp.get("mattes") or []
    matte_ids = [str(m.get("matte_id")) for m in sam_mattes if m.get("matte_id")]
    if not matte_ids:
        matte_ids = [str(m) for m in (resp.get("matte_ids") or [])]
    if not matte_ids:
        raise StudioError("the SAM service accepted this track but returned "
                          "no matte ids to watch")
    label = f"mask track {clip} {','.join(matte_ids)[:60]}"
    job = _register(Job("mask_track", label))
    job.extra = {"matte_ids": matte_ids, "clip": clip, "clip_key": clip_key,
                "rotation": rot, "sam_job_id": sam_job_id, "mattes": sam_mattes,
                # The frame window THIS job is tracking, so the cache decision
                # above can tell "the work is already happening" from "a job
                # is running for a narrower window than you just asked for"
                # (round 1 finding 5).
                "window": [int(start_frame), int(end_frame)]}
    _matte_jobs[rhash] = job.id
    _recipe_cache_put(clip_key, rhash, matte_ids)

    by_id = {str(m.get("matte_id")): m for m in sam_mattes if m.get("matte_id")}
    mattes_out = []
    for mid in matte_ids:
        m = by_id.get(mid)
        if m is not None:
            mattes_out.append({"matte_id": mid, "recipe": recipe,
                               "state": m.get("state", "queued"),
                               "object_id": m.get("object_id"),
                               "label": m.get("label"), "kind": m.get("kind")})
        else:
            # The response did not describe this matte (an older or --stub
            # service): only then does studio write index.json itself, so
            # the store has something to answer GET /api/matte/<id> with.
            raw = _write_matte_index(
                clip_key, mid, clip=clip, rotation=rot, fps=params["fps"],
                width=params["width"], height=params["height"],
                state="queued", done_frames=0, recipe=recipe)
            mattes_out.append({"matte_id": mid, "recipe": recipe, "state": raw["state"]})

    threading.Thread(target=_poll_sam_job, args=(job, clip_key, sam_job_id),
                     daemon=True).start()
    out = {"job_id": job.id, "mattes": mattes_out, "cached": False,
           "start_frame": start_frame, "end_frame": end_frame}
    if resume_kind:
        # Exactly one of these three is true, and which one it is says what
        # happened: resumed (a hole in the window this matte already declares),
        # widened (a window bigger than the one it declares) or restarted (the
        # matte was unusable, or `force`). They are three separate flags rather
        # than "not resumed means restarted" because a caller has to be able to
        # tell a widen from a redo: a widen keeps every frame already tracked.
        out["resumed"] = resume_kind == "resume"
        out["widened"] = resume_kind == "widen"
        out["restarted"] = resume_kind in ("restart", "force")
        out["resumed_from"] = resume_from
        out["message"] = restart_reason
    return out


def _mask_job_view(job: Job) -> dict:
    """The C3-mirrored single job shape GET /api/mask/jobs/<id> answers with,
    built from the studio Job plus the SAM state the poller last saw.

    `mattes` carries each matte's OWN state (object_id, label, kind, state,
    done_frames, error): the service can finish one text slot while another
    fails ("no instance for text 'shirt'"), and the job as a whole is not
    "failed" just because one of several matte slots was. A caller wanting
    one matte's authoritative state still reads GET /api/matte/<id>, which
    the service keeps current directly; this is the jobs-panel view.
    """
    sam_state = job.extra.get("sam_state") or {}
    state = ("cancelled" if job.status == "cancelled" else
             "failed" if job.status == "failed" else
             "done" if job.status == "done" else "running")
    error = None
    if state == "failed":
        error = sam_state.get("error") or job.message or None
    return {
        "id": job.id, "label": job.label, "state": state,
        "progress": round(job.progress, 4), "message": job.message,
        "matte_ids": job.extra.get("matte_ids", []),
        "mattes": job.extra.get("mattes", []),
        "clip": job.extra.get("clip"), "clip_key": job.extra.get("clip_key"),
        "rotation": job.extra.get("rotation"),
        "sam_job_id": job.extra.get("sam_job_id"),
        "done_frames": sam_state.get("done_frames"),
        "total_frames": sam_state.get("total_frames"),
        "fps": sam_state.get("fps"), "elapsed_s": sam_state.get("elapsed_s"),
        "queue_position": sam_state.get("queue_position"),
        "error": error, "started": job.started, "finished": job.finished,
    }


def _matte_weight_for(info, time_s: float, meta: dict):
    """The HxW weight array for `frame_stats`'s `weight` argument (C6): full
    frame size first, so the matte and the picture agree on where a pixel
    is, THEN the same region crop `resolve_stats_frame` already applied
    (region crops first, then the matte weights what is left).
    """
    full_w = meta.get("full_width", meta["width"])
    full_h = meta.get("full_height", meta["height"])
    try:
        arr, served, warning = MT.load_time(info, time_s, (full_w, full_h))
    except MT.MatteMissing as exc:
        raise StudioError(str(exc)) from exc
    if "region_pixels" in meta:
        x, y, w, h = meta["region_pixels"]
        arr = np.ascontiguousarray(arr[y:y + h, x:x + w])
    return arr, served, warning


def _mask_stack_weight(mask: dict, time_s: float, rgb, guard=None):
    """The HxW weight array for a whole mask STACK (checkpoint gap 19).

    `mask` is a layer's own `mask` block: a list of matte, key, luma and
    window components combined with add, intersect and subtract, each with
    its own `invert` and `feather`, plus the `finesse` block on the result.
    `POST /api/stats {"matte": ID}` measures through one stored matte, which
    cannot say "the person matte intersected with a skin key" at all; this
    can, in one call, in the same dict the layer that will render it carries.

    Composed by `CG.mask_matte`, the engine's own numpy reference for a
    layer's matte (the one the parity suite renders against through ffmpeg),
    at the measured frame's own size, so the weight and the picture line up
    with nothing resampled between them and a measurement agrees with a
    render by construction rather than by a separate check.

    `guard` is called with each matte component's clip so this route's own
    read guard runs on every matte the stack reaches, not only on a single
    `matte` field. Every matte id is resolved first: an id that names nothing
    is a 404, where `mask_matte` on its own would treat it as a black matte
    and answer "no coverage", which for a typo is the worst possible answer.
    Returns (weight, warnings, matte_ids).
    """
    layer = CG.mask_stack_layer(mask)                 # refuses a no-op mask
    warnings_out = []
    matte_ids = CG.mask_stack_matte_ids(layer)
    for matte_id in matte_ids:
        info = _matte_info(matte_id)                  # 404 when unknown
        if guard is not None:
            guard(info.clip)
        try:
            _served, warn = MT.served_frame(info, time_s)
        except MT.MatteMissing as exc:
            raise StudioError(str(exc)) from exc
        if warn:
            warnings_out.append(warn)
    for _j, comp in CG.stack_components(layer):
        if CG.component_type(comp) == "matte" and not str(
                CG.component_matte_ref(comp).get("id") or "").strip():
            raise StudioError(
                "mask: a matte component has no matte id yet (it needs a pick "
                "and a track first); measuring through it would measure a "
                "black matte and report no coverage")
    h, w = int(rgb.shape[0]), int(rgb.shape[1])
    weight = CG.mask_matte(layer, {"width": w, "height": h},
                           np.asarray(rgb, dtype=np.float64) / 255.0, time_s)
    return np.asarray(weight, dtype=np.float64), warnings_out, matte_ids


def _stamp_coverage(row: dict) -> dict:
    """Hoist `coverage` and `no_coverage` out of a stats block onto the row.

    `frame_stats` puts both inside `stats` on any WEIGHTED measurement
    (checkpoint gaps 19 and 23) and neither on an unweighted one, so this
    copies them up when they are there and adds nothing when they are not:
    an unweighted answer keeps exactly the envelope it always had. Beside
    `measured_width`, a row then says what it looked at as well as what it
    found, and a script walking a list of timestamps can skip a frame the
    mask covers nothing of with one read instead of digging.
    """
    stats_out = row.get("stats") or {}
    if stats_out.get("coverage") is not None:
        row["coverage"] = stats_out["coverage"]
        row["no_coverage"] = bool(stats_out.get("no_coverage"))
    return row


def resolve_preset_mask_recipes(clip: str, rotation, cfg: dict) -> tuple[dict, list]:
    """"Recipe resolution on preset load" (C4), the I/O half of
    projects.py's pure `resolve_mask_recipes`: this function is the
    `lookup` PROJECTS calls per mask.matte component, and the thing that
    actually queues a track for every portable (text only) recipe it could
    not resolve to an existing matte.

    `lookup` tries the component's own saved matte id first (only good when
    it names a matte that belongs to THIS clip: a preset is reusable across
    clips, so a matte id saved against a different clip is exactly the
    "needs a fresh pick" case, not a hit), then the recipe cache for a
    matte this exact clip, rotation and recipe already produced.
    """
    rot = CG.normalise_rotation(rotation)
    clip_key = mask_clip_key(clip)

    def lookup(recipe, existing_id):
        norm = _normalise_track_recipe(
            (recipe or {}).get("prompts", recipe),
            (recipe or {}).get("select"), (recipe or {}).get("steady"))
        if existing_id:
            try:
                info = MT.resolve(MT.matte_root(), str(existing_id))
                if info.clip_key == clip_key:
                    return info.matte_id
            except MT.MatteMissing:
                pass
        cached = _recipe_cache_get(clip_key, _recipe_hash(clip_key, rot, norm))
        return cached[0] if cached else None

    resolved, to_queue = PROJECTS.resolve_mask_recipes(cfg, lookup)
    queued = []
    for item in to_queue:
        recipe = item["recipe"] or {}
        prompts = recipe.get("prompts", recipe)
        try:
            result = queue_mask_track(clip, rot, prompts, recipe.get("select"),
                                      recipe.get("steady"))
        except (StudioError, HttpError) as exc:
            queued.append({"layer": item["layer"], "component": item["component"],
                           "error": str(exc)})
            continue
        # Stamp the matte id straight onto the resolved config so a caller
        # sees it without a second round trip back to this same route.
        mattes = result.get("mattes") or []
        if mattes:
            layer = resolved.get("layers", [])[item["layer"]]
            for comp in (layer.get("mask", {}).get("components") or []):
                if comp.get("id") == item["component"] and comp.get("type") == "matte":
                    comp["matte"]["id"] = mattes[0]["matte_id"]
                    comp["matte"]["needs_pick"] = False
        queued.append({"layer": item["layer"], "component": item["component"],
                       "job_id": result.get("job_id"), "mattes": mattes})
    return resolved, queued


# --------------------------------------------------------------------------
# live session
#
# The config the user is looking at lived only in browser memory, so nothing
# outside the tab could read it or change it. Keeping a copy here is what lets
# an outside agent run `cinegrade session patch` against the open studio and
# have the picture actually move. It is a mirror, not the source of truth: the
# browser still owns the config and republishes on every change.
# --------------------------------------------------------------------------

# One live session PER USER, keyed by user id, each with exactly the shape the
# single global LIVE dict used to have. With logins off a plain request still
# resolves to user 0, so the browser tab's entry is the one it always was;
# a caller that named itself with X-Studio-Agent gets its own (contract G2).
# With logins on, two people grading at once would otherwise share one mirror:
# one browser's publish would land in the other's long poll and overwrite the
# picture they were working on.
LIVE: dict[int, dict] = {}


class LiveRevMismatch(Exception):
    """A session write arrived with an if_rev that is no longer current.

    Carries the state the caller should have been looking at, so the route
    can answer 409 with the current revision in one round trip rather than
    making the caller ask again to find out what it missed.
    """

    def __init__(self, rev: int, state: dict):
        self.rev = int(rev)
        self.state = state
        super().__init__(
            f"the session has moved on: you sent if_rev but the current "
            f"revision is {self.rev}. Read the session again and re-send. "
            f"Nothing was changed")


_live_lock = threading.Lock()
# Woken on every change so a waiting long poll returns immediately instead of
# the browser having to poll on a timer and lag behind by up to that interval.
# One condition for every user rather than one each: a wake is cheap, each
# waiter rechecks its own revision, and a per user condition would have to be
# created under a lock anyway.
_live_changed = threading.Condition(_live_lock)


def _live_state(user_id: int) -> dict:
    """This user's live state, created on first touch. Call with the lock held."""
    st = LIVE.get(int(user_id))
    if st is None:
        # project, head, rotation and branch are contract C1: the live state is
        # now a VIEW of the open project rather than a free floating config, so
        # a tab (or an agent) reading /api/session learns which project it is
        # looking at and which commit it is standing on without a second call.
        # The four are None until a project is opened, which is exactly what a
        # server with nothing open should say.
        st = {"rev": 0, "config": None, "clip": None, "time": 0.0,
              "by": "server", "project": None, "head": None, "rotation": None,
              "branch": None}
        LIVE[int(user_id)] = st
    return st


def _live_from_project(state: dict, proj: dict | None) -> None:
    """Point one live state at a project record. Call with the lock held."""
    if not proj:
        return
    state["project"] = proj["key"]
    state["head"] = PROJECTS.short(proj["head"])
    state["rotation"] = proj["rotation"]
    state["branch"] = proj["branch"]
    state["config"] = proj["config"]
    if proj["name"]:
        state["clip"] = proj["name"]
    state["time"] = float(proj["time"])


def live_touch(user_id: int, proj: dict, by: str) -> dict:
    """Publish a project move (checkout, undo, redo, fork, rotation, open).

    Bumps the revision so the long poll returns at once and the tab repaints
    from the new HEAD's config. Without this, going back a commit from the CLI
    would change the database and leave the picture on screen unchanged, which
    is exactly the "who is editing what" confusion this arc is here to end.
    """
    with _live_lock:
        state = _live_state(user_id)
        _live_from_project(state, proj)
        state["by"] = str(by or "server")
        state["rev"] += 1
        _live_changed.notify_all()
        return deepcopy(state)


def live_sync(user_id: int, proj: dict) -> None:
    """Bring the live mirror in line with a project WITHOUT waking anybody.

    Used where the caller already has this config on screen: the tab's own
    autosave (PUT /api/grade) writes what it just published. Bumping the
    revision there would send the tab its own edit back as somebody else's,
    which reads as a toast and an undo entry for a change nobody made.
    """
    with _live_lock:
        state = _live_state(user_id)
        clip_before = state["clip"]
        _live_from_project(state, proj)
        if clip_before:
            state["clip"] = clip_before


def live_rebuild() -> None:
    """Rebuild the live state from the workspace table at boot.

    A restart used to lose the open clip, the playhead and the config, so the
    first tab to connect started from nothing and republished whatever it had.
    The workspace table already knows which project each account had open, so
    the mirror can be rebuilt exactly, with revision 0: nobody is woken, and
    the first long poll waits like it always did.
    """
    try:
        open_projects = PROJECTS.workspaces()
    except Exception as exc:                                  # noqa: BLE001
        print(f"warning: could not restore open projects: {exc}", file=sys.stderr)
        return
    for user_id, key in open_projects.items():
        try:
            proj = PROJECTS.state(key)
        except Exception:                                     # noqa: BLE001
            continue
        if not proj:
            continue
        with _live_lock:
            state = _live_state(user_id)
            _live_from_project(state, proj)
            state["by"] = "server"


def live_get(user_id: int = 0) -> dict:
    with _live_lock:
        return deepcopy(_live_state(user_id))


def live_set(payload: dict, user_id: int = 0, author: str | None = None) -> dict:
    """Merge a patch into the live config, COMMIT it, and wake the waiters.

    The wire shape is unchanged: same body, same fields back, plus the four
    project fields. What changed underneath is that a session write is now the
    way an edit enters the history. `clip` opens that clip's project, `config`
    becomes a commit on it (deduped, so a republish of the same config is not
    a commit), `time` moves the playhead, and `message` names the commit when
    the caller has something better to say than the generated description.

    `author` is who the server decided the caller is (the account name with
    logins on). `by` stays exactly what the caller sent, because session.js
    filters its own echo on it and changing it would make the tab fight itself.

    `if_rev` is optional and is the whole of the concurrency story (contract
    G2): send the revision you last read and the write is refused, untouched,
    if anything landed in between. Checked under the same lock the write
    takes, so there is no window between the check and the change.
    """
    with _live_lock:
        state = _live_state(user_id)
        if payload.get("if_rev") is not None:
            try:
                want = int(payload["if_rev"])
            except (TypeError, ValueError):
                raise StudioError("if_rev is a revision number") from None
            if want != int(state["rev"]):
                raise LiveRevMismatch(int(state["rev"]), deepcopy(state))
        patch = payload.get("config")
        if patch is not None:
            if payload.get("replace") or state["config"] is None:
                state["config"] = full_config(patch)
            else:
                # Deep merged so a caller can send just the one knob it cares
                # about, which is the whole point of the CLI surface.
                state["config"] = CG.deep_merge(state["config"], patch)
        for key in ("clip", "time"):
            if payload.get(key) is not None:
                state[key] = payload[key]
        state["by"] = str(payload.get("by") or "cli")

        # --- the project side of the same write ---------------------------
        #
        # Wrapped rather than allowed to fail the request: a clip that cannot
        # be resolved (an external clip from a previous server run, a drive
        # that went away) used to leave the live mirror working, and it still
        # does. The session route is what the tab depends on to show anything
        # at all, so it degrades to the old behaviour instead of erroring.
        who = str(author or state["by"] or "cli")
        proj = None
        try:
            key = None
            if payload.get("clip") is not None:
                ckey, cname, cpath = resolve_clip(str(payload["clip"]))
                PROJECTS.ensure(ckey, cname, cpath)
                if PROJECTS.workspace_key(user_id) != ckey:
                    PROJECTS.set_workspace(user_id, ckey)
                key = ckey
            else:
                key = PROJECTS.workspace_key(user_id)
            if key:
                if payload.get("time") is not None:
                    PROJECTS.set_time(user_id, key, payload["time"])
                if patch is not None:
                    message = payload.get("message")
                    proj = PROJECTS.commit(user_id, key, state["config"], who,
                                           message=(str(message) if message else None))
                else:
                    proj = PROJECTS.state(key)
        except Exception as exc:                              # noqa: BLE001
            if VERBOSE:
                print(f"session write: no project for this write ({exc})",
                      file=sys.stderr)
        if proj:
            clip_before = state["clip"]
            _live_from_project(state, proj)
            # The caller's clip name wins over the project's stored one: a clip
            # opened from outside footage/ is known by the name THIS server run
            # gave it, and the project may have been created under another.
            if payload.get("clip") is not None:
                state["clip"] = payload["clip"]
            elif clip_before:
                state["clip"] = clip_before
            if payload.get("time") is not None:
                state["time"] = payload["time"]

        state["rev"] += 1
        _live_changed.notify_all()
        return deepcopy(state)


def live_wait(since: int, timeout: float = 25.0, user_id: int = 0) -> dict:
    """Block until the live config has moved past `since`, or time out.

    A long poll rather than a timer in the browser: the tab picks up an outside
    change as soon as it happens, and costs one idle connection rather than a
    request every second forever.
    """
    deadline = time.time() + timeout
    with _live_lock:
        state = _live_state(user_id)
        while state["rev"] <= since:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            _live_changed.wait(remaining)
        return deepcopy(state)


def stored_match_crops(payload: dict, user_id=None) -> dict:
    """The rectangles this project has saved, per reference (contract C7).

    Lives in the project's extras bag under `match_crops`, written by the tab
    through POST /api/project/extra and readable by an agent in
    GET /api/project. Shaped {REF_NAME: {"ref": [x0,y0,x1,y1],
    "frame": [x0,y0,x1,y1]}}, either key optional.

    Best effort by design: a match must not fail because the project store had
    a bad day, so anything unreadable here means "no stored rectangle" and the
    match runs on the whole image, which is what it did before this existed.
    """
    try:
        key = None
        clip = payload.get("clip")
        if clip:
            key = PROJECTS.resolve(str(clip))[0]
        if not key and user_id is not None:
            key = PROJECTS.workspace_key(user_id)
        if not key:
            return {}
        st = PROJECTS.state(key) or {}
        bag = (st.get("extras") or {}).get("match_crops")
        return bag if isinstance(bag, dict) else {}
    except Exception:                                       # noqa: BLE001
        return {}


def _crop_from(payload: dict, field: str, stored):
    """One rectangle and where it came from.

    Three cases, and the difference between the last two matters: a field the
    caller did not send falls back to what the project saved (that is how the
    browser tab keeps measuring the rectangle the person drew, on the call the
    page makes without repeating it), while a field sent as null is the caller
    saying "this one, whole image", which must not be quietly overridden by a
    saved rectangle they cannot see.

    `stored` arrives as None for an agent caller, so that fallback never fires
    for one: see `match_reference_job`'s `use_stored_crops`.
    """
    if field in payload:
        value = payload.get(field)
        return (value, "request") if value else (None, "none")
    return (stored, "project") if stored else (None, "none")


def match_reference_job(payload: dict, user_id=None,
                        use_stored_crops: bool = True) -> dict:
    """Fit a look cube that moves the current frame toward a reference image.

    Synchronous rather than a background Job because the tool costs 1.4 to 3.0
    seconds and the user is standing on the button waiting for a LUT to appear
    in the look slot. A job would need polling to deliver the one thing the
    caller wants, which is the result.

    Imported lazily so the studio still starts if match_ref.py is missing or
    broken: everything else in this server works without it.
    """
    try:
        sys.path.insert(0, str(GRADE / "tools"))
        from match_ref import match_reference, MatchError, METHODS   # noqa: PLC0415
    except Exception as exc:                                # noqa: BLE001
        raise StudioError(f"match_ref.py is not usable: {exc}") from exc

    ref = payload.get("ref")
    if not ref:
        raise StudioError("no reference image given")
    ref_path = Path(ref)
    if not ref_path.is_absolute():
        ref_path = REFS / safe_name(ref)
    if not ref_path.exists():
        raise StudioError(f"reference not found: {ref}")

    cfg = full_config(payload.get("config"))
    # The fit has to run through the same CST the shot will actually be graded
    # through, and match_reference takes a preset by name or path rather than a
    # dict. Writing the LIVE config to a temp preset is what makes the match
    # honour convert moves the user has made since loading a preset, instead of
    # fitting through whatever the named preset happened to say.
    tmp = CACHE / "match-live-config.json"
    tmp.write_text(json.dumps(cfg))
    # crop_frac is "x,y,w,h" as fractions of the reference, drawn by hand on
    # the ref pane (static/app.js). When the user has drawn a box it is what
    # they mean by "match this", so it overrides the auto content detector
    # rather than stacking with it; with no box, auto_crop stays on and the
    # detector runs exactly as before this option existed.
    crop_frac = payload.get("crop_frac")
    # ref_crop and frame_crop are contract C7's picked rectangles, four
    # fractions [x0, y0, x1, y1] each: the reference one is applied before the
    # automatic chrome trim (so the trim runs INSIDE the rectangle rather than
    # arguing with it), the frame one crops the frame the fit measures. Absent
    # means "use whatever this project saved for this reference"; explicitly
    # null means "whole image". With neither, this call is byte for byte the
    # call it was before the rectangles existed.
    #
    # use_stored_crops is False for an agent caller (the route passes
    # `not caller_agent`). The stored rectangles live on the PROJECT, which is
    # shared by every caller with logins off, so an agent that never drew a box
    # was silently measuring a rectangle a person drew in a browser tab it
    # cannot see, and two agents on the same clip inherited each other's. The
    # browser tab keeps the fallback because the rectangle it would inherit is
    # the one it drew itself, one call earlier, on the pane in front of it. An
    # agent that does want the person's rectangle reads it from
    # GET /api/project (extras.match_crops) and sends it explicitly, which is
    # also what makes its own notes say which rectangle it measured.
    saved = stored_match_crops(payload, user_id) if use_stored_crops else {}
    for_ref = saved.get(str(ref)) or saved.get(ref_path.name) or {}
    if not isinstance(for_ref, dict):
        for_ref = {}
    ref_crop, ref_from = _crop_from(payload, "ref_crop", for_ref.get("ref"))
    frame_crop, frame_from = _crop_from(payload, "frame_crop",
                                        for_ref.get("frame"))
    # The match panel (static/index.html, static/app.js) exposes method,
    # strength and luma_preserve as real controls: match_ref.py actually
    # takes all three (METHODS is "reinhard" or "histogram"), so validated
    # here rather than hardcoded and forwarded as given.
    method = payload.get("method", "reinhard")
    if method not in METHODS:
        raise StudioError(f"unknown method {method!r}, expected one of "
                          f"{', '.join(METHODS)}")
    try:
        strength = float(payload.get("strength", 1.0))
    except (TypeError, ValueError):
        raise StudioError(
            f"strength must be a number, got {payload.get('strength')!r}")
    strength = max(0.0, min(1.0, strength))
    luma_preserve = bool(payload.get("luma_preserve", True))
    # A default name that carries the caller's own identity, not just ref,
    # clip and method: those three alone are exactly what let thirteen
    # concurrent bakeoff agents overwrite each other's cube under the same
    # name in the shared looks folder. With logins off every caller (browser
    # tab or agent) still has a distinct user id (contract G2), so this is
    # collision free the moment two callers are actually two identities.
    uid = 0 if user_id is None else int(user_id)
    name = payload.get("name") or (
        f"match_u{uid}_{_match_name_part(ref_path.stem)}_"
        f"{_match_name_part(Path(payload['clip']).stem)}_{method}")
    # out_dir: a relative path lands inside the repo (content/), an absolute
    # path is trusted as the caller's own folder. Either way it is created
    # if missing, so a caller does not have to mkdir before it can match.
    out_dir = payload.get("out_dir")
    if out_dir:
        dest_dir = Path(out_dir)
        if not dest_dir.is_absolute():
            dest_dir = CONTENT / dest_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        out_dir = str(dest_dir)
    try:
        result = match_reference(
            ref=str(ref_path),
            clip=str(clip_path(payload["clip"])),
            time=float(payload.get("time", 0)),
            # The fit has to measure the frame the user is looking at, which
            # means the frame at the rotation they are looking at it in.
            rotation=effective_rotation(payload, user_id),
            preset=str(tmp),
            method=method,
            # Baking anything other than full strength was previously
            # disallowed on the theory that look.mix (schema.js, the same
            # axis) makes a strength control redundant. The match panel now
            # exposes strength directly, so this bakes exactly what the user
            # asked for, clamped to what the slider offers; look.mix still
            # gives a zero cost strength change on top of the baked result
            # afterwards.
            strength=strength,
            luma_preserve=luma_preserve,
            crop_frac=crop_frac,
            auto_crop=crop_frac is None,
            ref_crop=ref_crop,
            frame_crop=frame_crop,
            name=name,
            out_dir=out_dir,
        )
    except MatchError as exc:
        raise StudioError(str(exc)) from exc
    finally:
        tmp.unlink(missing_ok=True)
    # Where each rectangle came from, so the panel can say "the one you drew"
    # against "the one this project had saved" instead of leaving the user to
    # guess which picture was measured.
    crops = result.setdefault("crops", {"ref": None, "frame": None})
    crops["ref_source"] = ref_from if crops.get("ref") else "none"
    crops["frame_source"] = frame_from if crops.get("frame") else "none"
    result["looks"] = list_looks()
    # A measurement, not a ranking: this is a single boolean gate on the one
    # fit that was just run, from numbers match_reference already computed
    # (gain_colour_pct, lut_health.probes_ok), never a score and never a
    # comparison against any other candidate.
    gain = (result.get("distance") or {}).get("gain_colour_pct")
    probes_ok = ((result.get("lut_health") or {}).get("probes_ok", True))
    result["recommended"] = bool(probes_ok and not (gain is not None and gain < 0))
    return result


def _match_name_part(stem: str) -> str:
    """The same sanitiser match_ref.py's own default name uses, so a name
    this route builds and a name match_ref.py would have built look alike."""
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stem).lower()


REBUILD_STEPS = [
    # Exactly the commands in grade/AGENTS.md, so the button and the docs
    # cannot drift apart.
    (["--stage", "in", "--size", "65",
      "--out", str(TECHNICAL / "AppleLog_to_DWG.cube")], "make_cst.py"),
]
# Every tonemap the CLI advertises, on both working spaces, at both encodes.
# This loop used to skip `none` on the output stage and skip `encode` entirely
# on the direct stage, which meant `--tonemap none` on the default working
# space asked for a cube nothing ever built and the render died, while the
# direct path quietly loaded make_cst.py's own default of gamma24 no matter
# what the encode said. Covering the full product is cheap (65-cube each) and
# removes a whole class of "the option exists but the file does not" failure.
for _tm in ("aces", "filmic", "none"):
    # The unsuffixed direct cube is still written because older trees and the
    # f_convert_out fallback look for it by that name.
    REBUILD_STEPS.append((
        ["--stage", "direct", "--tonemap", _tm, "--size", "65",
         "--out", str(TECHNICAL / f"AppleLog_to_Rec709_{_tm}.cube")], "make_cst.py"))
    for _enc in ("rec709a", "gamma24"):
        REBUILD_STEPS.append((
            ["--stage", "direct", "--tonemap", _tm, "--encode", _enc, "--size", "65",
             "--out", str(TECHNICAL / f"AppleLog_to_Rec709_{_tm}_{_enc}.cube")],
            "make_cst.py"))
        REBUILD_STEPS.append((
            ["--stage", "out", "--tonemap", _tm, "--encode", _enc, "--size", "65",
             "--out", str(TECHNICAL / f"DWG_to_Rec709_{_tm}_{_enc}.cube")],
            "make_cst.py"))
# The non Apple Log inputs (convert.input): HLG, PQ and Rec.709, each with the
# same product of tone map and encode as above. One step rather than forty
# because make_cst.py's batch mode writes them all from a single process, which
# costs one numpy and colour-science import instead of forty. The Apple Log
# cubes are NOT in this batch: they are the sixteen steps above, unchanged.
REBUILD_STEPS.append((["--batch", "--size", "65",
                       "--out-dir", str(TECHNICAL)], "make_cst.py"))
REBUILD_STEPS.append(([], "make_looks.py"))


def start_rebuild(which: str) -> Job:
    steps = [s for s in REBUILD_STEPS
             if which == "all"
             or (which == "cst" and s[1] == "make_cst.py")
             or (which == "looks" and s[1] == "make_looks.py")]
    if not steps:
        raise StudioError(f"unknown rebuild target: {which}")
    job = _register(Job("rebuild", f"rebuild {which}"))

    def worker():
        TECHNICAL.mkdir(parents=True, exist_ok=True)
        try:
            for i, (extra, script) in enumerate(steps):
                job.message = f"{script} ({i + 1} of {len(steps)})"
                job.progress = i / len(steps)
                proc = subprocess.run([sys.executable, str(TOOLS / script)] + extra,
                                      capture_output=True, text=True)
                job.log.extend((proc.stdout or "").strip().splitlines()[-4:])
                if proc.returncode != 0:
                    job.status = "failed"
                    job.message = (proc.stderr or "").strip()[-600:]
                    return
            job.status = "done"
            job.progress = 1.0
            job.message = f"{len(steps)} steps"
            # A rebuilt cube is a different cube; anything cached under the old
            # one is now a lie about what the pipeline produces.
            clear_frame_cache()
        except Exception as exc:                              # noqa: BLE001
            job.status = "failed"
            job.message = f"{exc}"
        finally:
            job.finished = time.time()

    threading.Thread(target=worker, daemon=True).start()
    return job


def clear_frame_cache() -> int:
    n = 0
    for kind in ("frames", "img", "thumbs", "src"):
        d = CACHE / kind
        if not d.exists():
            continue
        for p in d.glob("*"):
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
    # The in-memory source LRU is a separate layer from the disk cache above;
    # clearing only the disk half would leave a "cleared" cache that is still
    # warm in RAM, which would make a cold-cache speed measurement a lie.
    with _src_mem_lock:
        _src_mem.clear()
    return n


# --------------------------------------------------------------------------
# thumbnails and references
# --------------------------------------------------------------------------

def thumbnail(clip: str, time_s: float, width: int, rotation) -> bytes:
    rotation = CG.normalise_rotation(rotation)
    key = hashlib.sha1(f"{clip}|{time_s:.3f}|{width}|{rotation}".encode()).hexdigest()
    path = _cache_path("thumbs", key, "jpg")
    if path.exists():
        try:
            os.utime(path, None)
            return path.read_bytes()
        except FileNotFoundError:
            pass  # pruned between exists() and read; re-render instead of failing
    info = clip_info(clip, rotation)
    height = max(2, int(round(info["height"] * width / info["width"] / 2)) * 2)
    # Thumbnails run the conversion only. They are a "where am I in the clip"
    # index, and running the full grade on every one would make the strip cost
    # more than the frame the user is actually looking at.
    cfg = flat_config(full_config({}), keep_exposure=False)
    graph = CG.graph_with_mask(cfg, dict(info, width=width, height=height),
                               encode_out=False, tail_extra=["format=rgb24"],
                               src_label="studiosrc",
                               head_extra=f"[0:v]{CG.rotate_prefix(info)}"
                                          f"scale={width}:{height}"
                                          f":flags=bilinear,setsar=1[studiosrc]")
    args = CG.ffmpeg_inputs(str(clip_path(clip)), cfg, info, float(time_s))
    args += ["-filter_complex", graph, "-map", "[vout]", "-frames:v", "1",
             "-q:v", "5", "-pix_fmt", "yuvj420p", "-f", "mjpeg", "-"]
    with FFMPEG_SLOTS:
        proc = subprocess.run(args, capture_output=True)
    if proc.returncode != 0:
        raise StudioError(proc.stderr.decode("utf-8", "replace")[-600:])
    path.write_bytes(proc.stdout)
    _prune_cache("thumbs")
    return proc.stdout


def list_refs() -> list[dict]:
    items = []
    for p in sorted(REFS.iterdir()):
        if p.suffix.lower() in IMAGE_EXT and p.is_file():
            items.append({"name": p.name, "bytes": p.stat().st_size})
    return items


def ref_image(name: str, width: int) -> bytes:
    name = safe_name(name)
    src = REFS / name
    if not src.exists():
        raise StudioError(f"reference not found: {name}")
    key = hashlib.sha1(f"{name}|{width}|{src.stat().st_mtime_ns}".encode()).hexdigest()
    path = _cache_path("refs", key, "jpg")
    if path.exists():
        try:
            os.utime(path, None)
            return path.read_bytes()
        except FileNotFoundError:
            pass  # pruned between exists() and read; re-render instead of failing
    args = ["ffmpeg", "-v", "error", "-y", "-i", str(src),
            "-vf", f"scale={width}:-2:flags=bicubic", "-frames:v", "1",
            "-q:v", "3", "-pix_fmt", "yuvj444p", "-f", "mjpeg", "-"]
    with FFMPEG_SLOTS:
        proc = subprocess.run(args, capture_output=True)
    if proc.returncode != 0:
        raise StudioError(proc.stderr.decode("utf-8", "replace")[-600:])
    path.write_bytes(proc.stdout)
    return proc.stdout


def ref_source_frame(payload: dict) -> tuple[np.ndarray, dict]:
    """The decoded reference still for `POST /api/stats`' `ref` field.

    `ref` is a name resolved through the exact rules `list_refs` and
    `safe_name` already use, so a caller cannot ask for anything outside
    content/refs and cannot ask for anything list_refs would not show. Images
    only: a reference is a still by definition. Measured through
    stats_decode_image, the same one decode function `path` and the CLI use,
    so a reference measured through the studio and the same reference
    measured through `cinegrade stats --image` are the same numbers.
    """
    name = safe_name(payload["ref"])
    known = {r["name"] for r in list_refs()}
    if name not in known:
        raise StudioError(f"reference not found: {name}")
    rgb = stats_decode_image(str(REFS / name), width=payload.get("width"),
                             region=payload.get("region"))
    return rgb, {"key": f"ref:{name}"}


def resolve_stats_frame(payload: dict, uid: int, time_s: float) -> tuple[np.ndarray, dict]:
    """One measured frame for `POST /api/stats`, from whichever source field
    the request used: `path` (read only, logins off only, registers
    nothing), `ref` (a still under content/refs) or `clip` (a footage clip,
    graded through the live config exactly as the frame route grades it).
    `time_s` is read by the clip and path forms; a ref is always the same
    still no matter what time was sent.
    """
    if payload.get("path") is not None:
        at_time = dict(payload, time=time_s)
        rgb = path_source_frame(at_time, uid)
        return rgb, {"key": path_source_key(at_time)}
    if payload.get("ref") is not None:
        return ref_source_frame(payload)
    if "clip" not in payload:
        raise StudioError("stats needs one of: clip, ref, path")
    cfg = full_config(payload.get("config"))
    if payload.get("mode") == "flat":
        cfg = flat_config(cfg, bool(payload.get("keep_exposure")))
    return render_region(payload["clip"], time_s,
                         int(payload.get("width", 640)), cfg,
                         effective_rotation(payload, uid),
                         payload.get("region"), payload.get("zoom"))


def list_renders() -> list[dict]:
    items = []
    if OUT.exists():
        for p in sorted(OUT.iterdir(), key=lambda x: -x.stat().st_mtime):
            if p.suffix.lower() in VIDEO_EXT:
                st = p.stat()
                items.append({"name": p.name, "bytes": st.st_size,
                              "mtime": st.st_mtime, "path": str(p)})
    return items


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _nonfinite_path(o, path: str = "") -> str | None:
    """Where the first NaN or Infinity lives, as a dotted path, or None.

    Only called once a dump has already failed, so the cost of walking the
    object does not land on healthy responses. It exists because "Out of range
    float values are not JSON compliant" on its own does not tell you which of
    several hundred numbers in a match report was the bad one.
    """
    if isinstance(o, float):
        return path or "(root)" if not math.isfinite(o) else None
    if isinstance(o, dict):
        for k, v in o.items():
            hit = _nonfinite_path(v, f"{path}.{k}" if path else str(k))
            if hit:
                return hit
        return None
    if isinstance(o, (list, tuple)):
        for i, v in enumerate(o):
            hit = _nonfinite_path(v, f"{path}[{i}]")
            if hit:
                return hit
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "FixxrStudio/1.0"
    protocol_version = "HTTP/1.1"

    # Set fresh on every request by _dispatch(). Class level defaults so a
    # route that reads self.user cannot trip over a missing attribute if it
    # is ever reached by a path that skipped _dispatch. With logins off
    # self.user stays None everywhere and every check below is a no-op, which
    # is what "auth off changes nothing" means in practice.
    user = None
    auth_method = ""            # "cookie", "bearer" or ""

    # Caller identity, contract G2. Set fresh on every request by
    # _resolve_caller(), class level defaults for the same reason as above.
    # caller_id is WHO IS CALLING; _uid() below is WHOSE STATE this request
    # acts on, and the two differ only while attached.
    caller_id = 0
    caller_name = "local"
    caller_agent = ""           # the bare NAME from X-Studio-Agent, or ""
    attached_to = None          # a user id while X-Studio-Attach is in play
    _body_cache = None          # the parsed JSON body, read at most once
    _body_read = False          # ... and whether that read has happened
    _caller_used = False        # _uid() has been answered, so it cannot move
    _status = 0                 # last status sent, for the request log

    def log_message(self, fmt, *args):                        # noqa: A003
        if VERBOSE:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_response(self, code, message=None):              # noqa: A003
        """Remember the status so the request log can report it.

        Overridden rather than recorded in _send() because a response can
        leave by four other doors: the 304 in the static handler, the chunked
        stream starter, the range responses, and BaseHTTPRequestHandler's own
        error path. All five call this.
        """
        self._status = int(code)
        super().send_response(code, message)

    # --- plumbing ---------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str, extra=None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        extra = extra or {}
        # Everything dynamic (API responses, rendered frames, streamed
        # segments) defaults to no-store. A caller that wants a different
        # Cache-Control (the static file handler below wants no-cache, which
        # means "revalidate every time", not "do not store") puts it in
        # extra and this does not also send the no-store default on top of it.
        if "Cache-Control" not in extra:
            self.send_header("Cache-Control", "no-store")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass                       # the user scrubbed on; nothing to do

    def _json(self, obj, code: int = 200) -> None:
        # allow_nan=False on purpose. Python's default writes a bare NaN or
        # Infinity token, which no browser's JSON parser accepts, so a single
        # undefined measurement makes the whole response unreadable and the
        # failure surfaces in the page as an unrelated looking parse error.
        # Failing here instead turns that into a named server side bug.
        try:
            body = json.dumps(obj, allow_nan=False)
        except ValueError:
            where = _nonfinite_path(obj) or "unknown field"
            body = json.dumps({"error":
                "the server built a response containing a value that cannot be "
                "represented in JSON (NaN or Infinity) at " + where + ". That is "
                "a bug in the endpoint, not in your input. The response was not "
                "sent because a browser could not have parsed it."})
            self._send(500, body.encode(), "application/json")
            return
        self._send(code, body.encode(), "application/json")

    def _body(self) -> dict:
        # Cached: the body is one read off a socket, so a route that asks for
        # it twice used to get {} the second time. It is also where a caller
        # is allowed to name itself (contract G2: body field "agent"), and
        # that has to be applied exactly once.
        if self._body_read:
            return self._body_cache
        self._body_read = True
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            self._body_cache = {}
            return self._body_cache
        # Checked against the declared length before a single byte is read,
        # so an oversized JSON body is refused instead of the server sitting
        # there waiting for bytes that may never come. The one route that
        # legitimately sends more than this (uploading a clip) reads the raw
        # socket itself in _handle_upload and never reaches this method.
        if n > BODY_MAX_BYTES:
            raise HttpError(413, f"request body is {n} bytes, over the "
                                 f"{BODY_MAX_BYTES} byte cap for this endpoint")
        raw = self.rfile.read(n)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StudioError(f"bad JSON body: {exc}")
        self._body_cache = parsed
        if isinstance(parsed, dict) and parsed.get("agent"):
            self._agent_from_body(str(parsed["agent"]))
        return parsed

    def _raw_body(self, max_bytes: int | None = None) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        # Same cap check as _body() above, before a byte is read, so a client
        # sending an oversized body over its declared Content-Length gets a
        # 413 instead of the server reading the whole thing first. max_bytes
        # is optional (default: no cap) so callers that already validate size
        # a different way, like the upload handler, are unaffected.
        if max_bytes is not None and n > max_bytes:
            raise HttpError(413, f"request body is {n} bytes, over the "
                                 f"{max_bytes} byte cap for this endpoint")
        return self.rfile.read(n)

    def _query(self) -> dict:
        q = urllib.parse.urlparse(self.path).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}

    def _stale(self) -> bool:
        """True if a newer request from the same tab has already arrived."""
        cid = self.headers.get("X-Studio-Client")
        gen = self.headers.get("X-Studio-Gen")
        if not cid or not gen:
            return False
        try:
            gen = int(gen)
        except ValueError:
            return False
        with _gen_lock:
            latest = _latest_gen.get(cid, 0)
            if gen > latest:
                _latest_gen[cid] = gen
                return False
            return gen < latest

    # --- playback streaming -------------------------------------------------

    def _stream_start(self, code: int, ctype: str, extra=None) -> None:
        """Start a chunked response with no Content-Length up front.

        _send() above always knows its body's exact length before it writes
        a byte; a playback segment does not, because its length is not
        decided until ffmpeg finishes, and the whole point of streaming is
        not waiting for that. Transfer-Encoding: chunked is HTTP/1.1's
        answer to exactly this. The connection is closed afterward rather
        than kept alive: a write that can run for several seconds is not
        worth the risk of leaving this thread's socket state disagreeing
        with whatever request lands on it next.
        """
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.close_connection = True

    def _stream_chunk(self, data: bytes) -> bool:
        """Write one HTTP/1.1 chunk. False means the client is gone."""
        if not data:
            return True
        try:
            self.wfile.write(b"%x\r\n" % len(data))
            self.wfile.write(data)
            self.wfile.write(b"\r\n")
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _stream_end(self) -> None:
        try:
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _serve_segment_file(self, path: Path) -> None:
        """A cache hit: this segment was already rendered, so it is just a
        file on disk. Range is honoured (a browser sometimes probes with
        one even on a fresh <video src>) though nothing in this app's own
        UI seeks within a segment yet.
        """
        os.utime(path, None)
        data = path.read_bytes()
        total = len(data)
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            try:
                lo, _, hi = rng[6:].partition("-")
                start = int(lo) if lo else 0
                end = min(int(hi), total - 1) if hi else total - 1
            except ValueError:
                start, end = 0, total - 1
            if start > end or start >= total:
                self._send(416, b"", "video/mp4", {"Content-Range": f"bytes */{total}"})
                return
            self._send(206, data[start:end + 1], "video/mp4", {
                "Content-Range": f"bytes {start}-{end}/{total}",
                "Accept-Ranges": "bytes",
            })
            return
        self._send(200, data, "video/mp4", {"Accept-Ranges": "bytes"})

    def _serve_proxy(self, name: str) -> None:
        """GET /api/proxy/<key>.mp4, with byte ranges.

        A <video> element cannot seek without them: Chrome asks for the tail
        of the file to read the index, then for the byte range around the
        keyframe it wants, and a server that answers every one of those with
        200 and the whole file makes scrubbing quadratic in file size (and,
        on some builds, makes currentTime= silently do nothing). The stdlib
        server this file is built on has no Range support of its own, so this
        is it.
        """
        key = name[:-4] if name.endswith(".mp4") else name
        if not re.fullmatch(r"[0-9a-f]{8,64}", key):
            raise StudioError("that is not a proxy key")
        path = _proxy_path(key)
        # Gone is a 404 with a one line reason, never a traceback, and that
        # covers two different ways of being gone: never built, and pruned
        # between this check and the read below by _prune_cache_bytes in
        # another request or another server sharing studio/cache. The client
        # answer to both is the same, so they get the same message: prepare
        # it again. Every read here goes through this one handler, so the
        # race cannot escape as a 500 from some other line.
        gone = HttpError(404, "that proxy has not been built yet, or has been "
                              "evicted from the cache; ask for it again with "
                              "POST /api/proxy/prepare")
        try:
            total = path.stat().st_size
            os.utime(path, None)
        except FileNotFoundError:
            raise gone
        rng = (self.headers.get("Range") or "").strip()
        if not rng.lower().startswith("bytes="):
            try:
                whole = path.read_bytes()
            except FileNotFoundError:
                raise gone
            self._send(200, whole, "video/mp4", {"Accept-Ranges": "bytes"})
            return
        spec = rng[6:].strip()
        # A multi range request gets the FIRST range, which RFC 9110 allows
        # (a server may answer with a single part). Building a
        # multipart/byteranges body would be more code for a case no browser
        # media element actually sends.
        if "," in spec:
            spec = spec.split(",")[0].strip()
        lo, _, hi = spec.partition("-")
        try:
            if lo == "":
                # "bytes=-N": the last N bytes, which is how a player reads
                # an index at the end of a file.
                start = max(0, total - int(hi))
                end = total - 1
            else:
                start = int(lo)
                end = int(hi) if hi else total - 1
        except ValueError:
            self._send(416, b"", "video/mp4",
                       {"Content-Range": f"bytes */{total}",
                        "Accept-Ranges": "bytes"})
            return
        end = min(end, total - 1)
        if start > end or start >= total:
            self._send(416, b"", "video/mp4",
                       {"Content-Range": f"bytes */{total}",
                        "Accept-Ranges": "bytes"})
            return
        # Seek and read only what was asked for: a proxy is one file for a
        # whole clip, so reading all of it to answer a 64KB range would be
        # the thing that makes seeking expensive.
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                data = fh.read(end - start + 1)
        except FileNotFoundError:
            raise gone
        self._send(206, data, "video/mp4", {
            "Content-Range": f"bytes {start}-{end}/{total}",
            "Accept-Ranges": "bytes",
        })

    def _play_stream(self, key: str) -> None:
        if not key:
            raise StudioError("no playback key given")
        cache_path = _segment_path(key)
        if cache_path.exists():
            try:
                self._serve_segment_file(cache_path)
                return
            except FileNotFoundError:
                # A prune (this process or another one sharing studio/cache)
                # deleted the segment between exists() and the read inside
                # _serve_segment_file, before any response header went out.
                # Fall through exactly like a cache miss: re-render if the
                # params to do that are still remembered below.
                pass

        with _play_pending_lock:
            params = _play_pending.get(key)
        if params is None:
            raise StudioError("that playback key is unknown or has expired; "
                              "press play again")

        # Built (and can raise StudioError/GradeError, e.g. an unreadable
        # LUT or a working-space mismatch) before any header goes out, so a
        # bad config still comes back as a normal 400 with a real message
        # instead of an empty video the browser just fails to play.
        args = _play_ffmpeg_args(params)

        self._stream_start(200, "video/mp4", {"X-Studio-Segment-Key": key})
        tmp_path = SEGMENTS / f"{key}.{uuid.uuid4().hex[:8]}.tmp"
        proc = None
        ok = False
        client_gone = False
        with FFMPEG_SLOTS:
            try:
                proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE)
                with open(tmp_path, "wb") as tmp:
                    while True:
                        chunk = proc.stdout.read(65536)
                        if not chunk:
                            break
                        tmp.write(chunk)
                        # Keep draining and caching even after the viewer
                        # hangs up (paused, or a grade change called
                        # video.load() to abort the fetch): ffmpeg is
                        # already most of the way through a short, bounded
                        # render, and finishing it means the next press of
                        # Play on this same grade is a cache hit instead of
                        # a second render.
                        if not client_gone and not self._stream_chunk(chunk):
                            client_gone = True
                proc.wait(timeout=30)
                ok = proc.returncode == 0
            except Exception:                                  # noqa: BLE001
                ok = False
            finally:
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait()
        if ok and tmp_path.exists() and tmp_path.stat().st_size > 0:
            tmp_path.replace(cache_path)
            _prune_cache_bytes("segments", SEGMENT_CACHE_MAX_BYTES)
        else:
            tmp_path.unlink(missing_ok=True)
        self._stream_end()

    # --- upload -------------------------------------------------------------

    def _handle_upload(self) -> None:
        """POST /api/upload: the raw file body streamed straight to disk.

        No multipart parser: the client sends the whole file as the request
        body, with the display name in X-File-Name (percent-encoded, since a
        header value has to stay on one line and plain ASCII) and always
        application/octet-stream for Content-Type. That keeps this stdlib
        only and lets the size cap below answer before a single byte of the
        body is read, which a multipart body (the boundary parser has to
        start consuming the stream before it knows anything) cannot do.

        Auth, the login-required 401 and the CSRF check already ran in
        _api() before this route was reached, exactly like every other POST,
        so logins-on with no session or a cross site post never gets this
        far. Which folder this writes into is decided the same way `open`
        decides what a browse path is allowed to resolve to: this account's
        own footage folder with logins on, the shared FOOTAGE folder without.
        """
        raw_name = self.headers.get("X-File-Name", "")
        name = safe_name(urllib.parse.unquote(raw_name))
        ext = Path(name).suffix.lower()
        if ext not in VIDEO_EXT:
            raise StudioError(f"not a video extension this tool reads: {name}")

        length_hdr = self.headers.get("Content-Length")
        if not length_hdr or not length_hdr.isdigit():
            raise HttpError(400, "Content-Length is required for an upload")
        length = int(length_hdr)
        if length <= 0:
            raise HttpError(400, "empty upload")
        if length > UPLOAD_MAX_BYTES:
            # Checked against the header alone, before a byte of the body is
            # read: an oversized upload is refused instantly rather than
            # after however long it takes to stream gigabytes nobody wants.
            raise HttpError(413, f"upload is {length} bytes, over the "
                                 f"{UPLOAD_MAX_BYTES} byte cap")

        # Contract E1: ?root=&path= puts the file in a folder of the library
        # instead of at its top, and an editor on a shared folder may upload
        # into it. With neither parameter this is exactly the old rule (this
        # account's own folder with logins on, content/footage without), so
        # every existing client keeps working without knowing about any of it.
        q = self._query()
        dest_dir, owner_id, rel_dir = LIB.upload_target(
            self._uid(), q.get("root"), q.get("path"))
        # The quota is the whole library tree, not one folder: with folders in
        # the picture, a flat sum would be evaded by making a subfolder.
        existing = LIB.tree_bytes(LIB.root_for(owner_id))
        if existing + length > UPLOAD_QUOTA_BYTES:
            raise HttpError(413, "this upload would put the footage folder "
                                 f"over its {UPLOAD_QUOTA_BYTES} byte total; "
                                 "delete something first")

        final_name = name
        if (dest_dir / final_name).exists():
            # Do not silently overwrite an existing clip of the same name;
            # give the new file its own identity instead, the same call
            # register_external_clip makes for a colliding external file.
            stem, suffix = Path(name).stem, Path(name).suffix
            final_name = f"{stem}_{uuid.uuid4().hex[:6]}{suffix}"
        final_path = dest_dir / final_name
        tmp_path = dest_dir / f".upload-{uuid.uuid4().hex[:8]}.tmp"

        written = 0
        try:
            with open(tmp_path, "wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break                    # the connection dropped early
                    f.write(chunk)
                    written += len(chunk)
                    remaining -= len(chunk)
                    # Belt and braces: written can only reach `length`, which
                    # was already checked above, but the cap is enforced here
                    # too rather than trusted to the header alone, in case a
                    # future caller of this method ever changes that.
                    if written > UPLOAD_MAX_BYTES:
                        raise HttpError(413, "upload exceeded the size cap "
                                             "while it was being received")
            if written != length:
                raise StudioError("the upload connection dropped before the "
                                  "declared size arrived; try again")
            tmp_path.replace(final_path)
        except Exception:                                      # noqa: BLE001
            tmp_path.unlink(missing_ok=True)
            raise

        if not probe_is_video(final_path):
            final_path.unlink(missing_ok=True)
            raise StudioError(f"{name} is not a video ffprobe can read; "
                              "nothing was kept")

        # Answered the same shape as POST /api/open: a clip entry the client
        # can select immediately, plus the refreshed clip list it belongs in.
        # `path` and `root` are added for the files pane, which needs to know
        # where the file landed to refresh the folder it is showing.
        clip_name = register_external_clip(str(final_path))
        rel = f"{rel_dir}/{final_name}" if rel_dir else final_name
        LIB.record(self._uid(), "upload", owner_id, rel,
                   {"name": final_name, "bytes": written})
        self._json({"name": clip_name,
                    "clip": self._clip_entry(clip_path(clip_name), clip_name),
                    "path": rel,
                    "root": ("mine" if owner_id == self._uid()
                             else f"user:{owner_id}"),
                    "clips": self._clips()})

    def do_GET(self):                                          # noqa: N802
        self._dispatch("GET")

    def do_POST(self):                                         # noqa: N802
        self._dispatch("POST")

    def do_PUT(self):                                          # noqa: N802
        # PUT exists for one route, PUT /api/grade: saving a clip's grade is
        # an idempotent write of the whole thing at a known address, which is
        # exactly what PUT means. Without this method BaseHTTPRequestHandler
        # answers 501 and the autosave fails silently in the page.
        self._dispatch("PUT")

    def do_DELETE(self):                                       # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str):
        path = urllib.parse.urlparse(self.path).path
        self.user = None
        self.auth_method = ""
        self._status = 0
        started = time.time()
        try:
            if AUTH.enabled():
                self.user, self.auth_method = AUTH.user_from_request(
                    self.headers.get("Cookie", ""),
                    self.headers.get("Authorization", ""))
            # Contract G2, and before any route runs: with logins off a
            # caller that named itself is its own user from here on, so the
            # live session, the workspace and the stored crops it touches are
            # its own and not the browser tab's.
            self._resolve_caller()
            if path.startswith("/api/"):
                self._api(method, path)
            else:
                self._static(path)
        except AUTH.AuthError as exc:
            # 401, 403 and 429 mean three different things to a client, so
            # they cannot all collapse into StudioError's 400 below.
            self._json({"error": str(exc)}, exc.code)
        except HttpError as exc:
            if exc.code == 413:
                # Answered without reading the declared body, so whatever the
                # client was about to send is still sitting unread on the
                # socket. Keeping the connection alive would feed those
                # leftover bytes to the parser as the start of the next
                # request; closing instead is what actually avoids the wait
                # this check exists to avoid.
                self.close_connection = True
            self._json({"error": str(exc)}, exc.code)
        except StudioError as exc:
            self._json({"error": str(exc)}, 400)
        except ValueError as exc:
            # projects.py says no to a rotation that is not one of the five, a
            # commit id nobody has, or a branch name already taken by raising
            # ValueError. Those are the caller's mistake, so they answer 400
            # with the message rather than a 500 and a traceback.
            self._json({"error": str(exc)}, 400)
        except CG.GradeError as exc:
            self._json({"error": str(exc)}, 400)
        except BrokenPipeError:
            pass
        except Exception as exc:                              # noqa: BLE001
            traceback.print_exc()
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        finally:
            self._log_request(method, path, started)

    def _log_request(self, method: str, path: str, started: float) -> None:
        """One line per request on stderr, contract G2.

        On by default: several agents and a person now share one server, and
        a silent server makes "who moved my picture" unanswerable. --quiet
        turns it off, EXCEPT for an attached request, which is always logged
        because acting as somebody else is the one thing that should never be
        invisible.

        Never raises: a logging bug must not turn a served request into a
        500 after the response has already gone out.
        """
        attached = self.attached_to is not None
        if QUIET and not attached:
            return
        try:
            ms = (time.time() - started) * 1000.0
            who = self.caller_name or "local"
            if attached:
                who = f"{who}>{self.attached_to}"
            clip = ""
            src = self._body_cache if isinstance(self._body_cache, dict) else {}
            name = src.get("clip") or src.get("ref") or ""
            if not name:
                q = self._query()
                name = q.get("clip") or q.get("ref") or ""
            if name:
                clip = f" clip={name}"
            sys.stderr.write("%s %s %s %s%s %d %.0fms\n" % (
                time.strftime("%H:%M:%S"), who, method, path, clip,
                self._status or 0, ms))
            sys.stderr.flush()
        except Exception:                                     # noqa: BLE001
            pass

    # --- static -----------------------------------------------------------

    def _static(self, path: str):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        # The app shell is the one static file that is gated. With logins on
        # and no valid session, / and /index.html serve the login page
        # instead. Every other file under static/ is served unconditionally,
        # because the login page itself needs style.css, the fonts and
        # theme.js, and none of them disclose anything.
        if AUTH.enabled() and rel == "index.html" and self.user is None:
            rel = "login.html"
        target = (STATIC / rel).resolve()
        if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
            self._send(404, b"not found", "text/plain")
            return
        # no-cache (not no-store): the browser is still allowed to keep a
        # copy, but it has to revalidate with If-Modified-Since before
        # using it. Without this Chrome falls back to heuristic caching and
        # keeps serving a page or script from before the last edit.
        mtime = int(target.stat().st_mtime)
        last_modified = self.date_time_string(mtime)
        since = self.headers.get("If-Modified-Since")
        if since:
            parsed = email.utils.parsedate_tz(since)
            since_ts = email.utils.mktime_tz(parsed) if parsed else None
            if since_ts is not None and mtime <= since_ts:
                self.send_response(304)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Last-Modified", last_modified)
                self.end_headers()
                return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype,
                   {"Cache-Control": "no-cache", "Last-Modified": last_modified})

    # --- auth ---------------------------------------------------------------

    def _secure_cookie(self) -> bool:
        """Whether to mark the session cookie Secure.

        A Secure cookie is not sent over plain HTTP, so setting it while the
        studio is genuinely being reached over http:// would lock the user
        out with a login that appears to succeed and then bounces straight
        back. It is therefore on only when the deployment says it is behind
        TLS: the --behind-https-proxy flag, or the X-Forwarded-Proto header a
        terminating proxy adds.
        """
        return (AUTH.behind_https_proxy()
                or self.headers.get("X-Forwarded-Proto", "").strip().lower()
                == "https")

    def _uid(self) -> int:
        """WHOSE state this request acts on.

        With logins on that is the signed in account, exactly as before. With
        logins off it is user 0 for a plain caller (so the browser tab is
        untouched), the agent's own row for a caller that sent
        X-Studio-Agent, and the attached user's row while X-Studio-Attach is
        in play. Every per caller store (the live session, the workspace, the
        stored match crops, the rotation fallback) keys off this one number,
        so none of them needs an "is this an agent" branch.

        Who is CALLING is caller_id and caller_name, and that is what signs a
        commit. The two differ only while attached, which is the whole point
        of attaching: act as them, sign as yourself.
        """
        self._caller_used = True
        if self.attached_to is not None:
            return int(self.attached_to)
        return int(self.caller_id)

    def _presets_uid(self) -> int:
        """Whose preset folder to read and write (contract G2).

        Presets are SHARED with logins off: agents save into users/0/presets
        so the person at the browser tab sees what an agent saved, and an
        agent sees what the person saved. With logins on nothing changes:
        each account keeps its own folder.
        """
        return self._uid() if AUTH.enabled() else 0

    def _caller_block(self) -> dict:
        """The `caller` block GET /api/state and GET /api/whoami answer with."""
        return {"id": int(self.caller_id),
                "name": str(self.caller_name),
                "attached_to": (int(self.attached_to)
                                if self.attached_to is not None else None)}

    def _agent_from_body(self, raw: str) -> None:
        """Apply a body field "agent" as the caller, if it is still allowed.

        The header is the normal way in; the body field exists so a caller
        that cannot set headers still has one. It arrives late, because the
        body is only read once a route asks for it, so it is refused rather
        than half applied if this request has already acted as somebody.
        """
        if AUTH.enabled() or self.caller_agent or not raw.strip():
            return
        if self._caller_used:
            raise StudioError(
                "this route resolved the caller before it read the body, so "
                "the body field \"agent\" came too late. Send the name as the "
                "X-Studio-Agent header instead")
        self._set_agent(raw)

    def _set_agent(self, raw: str) -> None:
        """Become agent NAME: find or create its row, keep its name."""
        name = safe_name(raw)
        if len(name) > 64:
            raise StudioError("an agent name is up to 64 characters")
        row = AUTH.ensure_agent(name)
        self.caller_id = int(row["id"])
        self.caller_name = str(row["name"])
        self.caller_agent = name

    def _resolve_caller(self) -> None:
        """Decide who this request is, before any route runs (contract G2).

        Header first, then ?agent=NAME on the query string; the body field is
        applied later by _body() because the body has not been read yet here.
        Nothing in this method touches the socket, so it cannot interfere
        with a route that reads the body itself.
        """
        self.caller_id = int(self.user["id"]) if self.user else 0
        self.caller_name = (str(self.user["name"]) if self.user
                            else ("anonymous" if AUTH.enabled() else "local"))
        self.caller_agent = ""
        self.attached_to = None
        self._body_cache = None
        self._body_read = False
        self._caller_used = False

        attach = (self.headers.get("X-Studio-Attach") or "").strip()
        if AUTH.enabled():
            # With logins on a token or a cookie IS the identity, so there is
            # nothing for these two headers to add and one of them would be a
            # way around the login: refuse the attach outright and ignore the
            # agent name.
            if attach:
                raise AUTH.AuthError(
                    403, "X-Studio-Attach is only for a server running "
                         "without logins. With logins on, sign in as that "
                         "account or use its agent token instead")
            return

        raw = (self.headers.get("X-Studio-Agent") or "").strip()
        if not raw:
            raw = (self._query().get("agent") or "").strip()
        if raw:
            self._set_agent(raw)

        if attach:
            self.attached_to = self._resolve_attach(attach)

    def _resolve_attach(self, raw: str) -> int:
        """The user id behind X-Studio-Attach, or a refusal.

        `0` is the local no-login user, which is the founder's own browser
        tab, and is the case this header mostly exists for: "look at what I
        am looking at". Any other value is an exact account or agent name.
        """
        raw = raw.strip()
        if raw == "0" or raw.lower() == "local":
            return 0
        uid = AUTH.user_id_by_name(raw)
        if uid is None:
            raise StudioError(
                f"cannot attach to {raw!r}: there is no account or agent of "
                f"that name. Attach to 0 for the local user")
        return int(uid)

    def _require_admin(self) -> None:
        if not AUTH.enabled():
            return
        if not self.user or self.user.get("role") != "admin":
            raise AUTH.AuthError(403, "that action needs an admin account")

    def _auth_api(self, method: str, route: str, q: dict):
        if route == "auth/me" and method == "GET":
            if not AUTH.enabled():
                # The shape is identical to the signed in case on purpose, so
                # the front end has one code path and no "is auth on" branch
                # scattered through it.
                self._json({"user": {"id": 0, "name": "local", "role": "admin"},
                            "auth_required": False})
                return
            if self.user is None:
                raise AUTH.AuthError(401, "not signed in")
            self._json({"user": self.user, "auth_required": True})
            return

        if route == "auth/login" and method == "POST":
            if not AUTH.enabled():
                raise StudioError("this studio is running without logins, so "
                                  "there is nothing to sign in to")
            body = self._body()
            name = str(body.get("username", "") or "")
            password = str(body.get("password", "") or "")
            addr = self.client_address[0] if self.client_address else "unknown"
            if AUTH.rate_blocked(name, addr):
                raise AUTH.AuthError(429, "too many failed sign ins. Wait five "
                                          "minutes and try again")
            user = AUTH.check_login(name, password)
            if user is None:
                AUTH.rate_record_failure(name, addr)
                # One message for a wrong name and a wrong password alike:
                # telling them apart is a free list of valid account names.
                raise AUTH.AuthError(401, "invalid credentials")
            AUTH.rate_clear(name)
            token = AUTH.create_session(user["id"])
            self._send(200, json.dumps({"user": user}).encode(),
                       "application/json",
                       {"Set-Cookie": AUTH.cookie_header(
                           token, self._secure_cookie())})
            return

        if route == "auth/logout" and method == "POST":
            AUTH.delete_session(AUTH.session_token_from_cookie(
                self.headers.get("Cookie", "")))
            self._send(204, b"", "application/json",
                       {"Set-Cookie": AUTH.clear_cookie_header(
                           self._secure_cookie())})
            return

        if route == "auth/token" and method == "POST":
            if not AUTH.enabled():
                raise StudioError("agent tokens need logins on: start the "
                                  "server with --auth")
            if self.user is None:
                raise AUTH.AuthError(401, "sign in first")
            made = AUTH.create_token(self.user["id"],
                                     str(self._body().get("label", "") or ""))
            # The only time the secret is ever readable. It is stored hashed,
            # so there is no endpoint that can show it again later.
            self._json({"token": made["token"], "id": made["id"],
                        "label": made["label"]})
            return

        if route == "auth/tokens" and method == "GET":
            if not AUTH.enabled() or self.user is None:
                raise AUTH.AuthError(401, "sign in first")
            self._json({"tokens": AUTH.list_tokens(self.user["id"])})
            return

        if route == "auth/token" and method == "DELETE":
            if not AUTH.enabled():
                raise StudioError("agent tokens need logins on: start the "
                                  "server with --auth")
            if self.user is None:
                raise AUTH.AuthError(401, "sign in first")
            raw_id = q.get("id", "")
            bearer = ""
            hdr = self.headers.get("Authorization", "").strip()
            if hdr.lower().startswith("bearer "):
                bearer = hdr[7:].strip()
            # With no id, an agent deletes the very token it is calling with,
            # which is how a script retires its own credential without having
            # been told a row number.
            gone = AUTH.delete_token(
                self.user["id"],
                int(raw_id) if raw_id.isdigit() else None, bearer)
            self._json({"deleted": gone,
                        "tokens": AUTH.list_tokens(self.user["id"])})
            return

        raise StudioError(f"no route for {method} /api/{route}")

    # --- api --------------------------------------------------------------

    def _api(self, method: str, path: str):
        q = self._query()
        route = path[len("/api/"):]

        # Cross site request check, contract C2. It runs before the auth
        # routes so that logout and token creation are covered too, and it
        # only applies to a request authenticated by COOKIE: a cookie is what
        # a browser attaches automatically to somebody else's form post, and
        # an Authorization header is not. Sign in itself carries no cookie
        # yet, so it passes through here untouched.
        if (AUTH.enabled() and method in ("POST", "PUT", "DELETE")
                and self.auth_method == "cookie"
                and not AUTH.csrf_ok(self.headers.get("Sec-Fetch-Site", ""),
                                     self.headers.get("Origin", ""),
                                     self.headers.get("Host", ""))):
            raise AUTH.AuthError(403, "refused: this write did not come from "
                                      "the studio page itself. If you are "
                                      "scripting the studio, use an agent "
                                      "token instead of the session cookie")

        if route.startswith("auth/"):
            self._auth_api(method, route, q)
            return

        # A GPU render's browser worker is not a person and has no session.
        # It carries the random token minted for that one render, it is a
        # child of this process so it is always on loopback, and the token
        # opens four routes and nothing else (render_gpu.WORKER_ROUTES). Every
        # other caller still meets the login gate below.
        if not RG.worker_authorised(self, route, method):
            if AUTH.enabled() and self.user is None:
                raise AUTH.AuthError(401, "sign in to use the studio")

        if route.startswith("render/gpu/") and RG.handle(self, method, route, q):
            return

        # The cheap first call (contract G2). GET /api/state probes every clip
        # with ffprobe; this one touches nothing but a directory listing, so
        # it is what a client should use to find out whether the server is up,
        # which build it is running and where its cache and data went.
        if route == "health" and method == "GET":
            self._json({
                "ok": True,
                "version": git_version(),
                "clips": clip_count(),
                "uptime_s": round(time.time() - START_TIME, 1),
                # The semaphore's own counter, which is how many ffmpeg runs
                # could start right now without waiting. Reading the private
                # attribute is deliberate: threading.Semaphore has no public
                # way to ask, and being wrong about it costs a log line, not
                # a render.
                "ffmpeg_slots_free": int(getattr(FFMPEG_SLOTS, "_value", 0)),
                "cache_dir": str(CACHE),
                "data_dir": str(DB.DATA),
                # Checkpoint gaps 4 and 5: the standalone CLI has its own
                # idea of both of these (content/footage and
                # studio/data/mattes) and a server started with --footage or
                # --data-dir does not. Answering them here is what lets a
                # CLI with STUDIO_URL set resolve a bare clip name and a
                # matte id the same way the server would, instead of the
                # caller exporting STUDIO_DATA_DIR by hand and getting an
                # ffprobe traceback when they forget.
                "footage_dir": str(FOOTAGE),
                "matte_root": str(MT.matte_root()),
                "logins": AUTH.enabled(),
            })
            return

        if route == "state" and method == "GET":
            self._json({
                "defaults": CG.DEFAULTS,
                "clips": self._clips(),
                "presets": list_presets(self._presets_uid()),
                "looks": list_looks(),
                "refs": list_refs(),
                "renders": list_renders(),
                # Who the server decided this caller is (contract G2). `id` is
                # the caller's own row, `attached_to` is whose state it is
                # acting on while X-Studio-Attach is set and null otherwise.
                # A plain browser tab with logins off sees {0, "local", null},
                # which is what it always effectively was.
                "caller": self._caller_block(),
                "paths": {
                    "content": str(CONTENT), "presets": str(PRESETS),
                    "looks": str(LOOKS), "out": str(OUT), "refs": str(REFS),
                    # "presets" above is always the shared, read only library
                    # (grade/presets/). A signed in user's own saved presets
                    # live in a separate per account folder (see
                    # GRADES.user_presets_dir); this tells an agent where
                    # that is, and is null with logins off since there is no
                    # account to key it by.
                    "user_presets": str(GRADES.user_presets_dir(self._uid()))
                                     if self.user else None,
                },
                "stat_definitions": {
                    "sat_floor": SAT_FLOOR,
                    "clip_black_code": CLIP_BLACK,
                    "clip_white_code": CLIP_WHITE,
                    "families": {n: [lo, hi] for n, lo, hi in HUE_FAMILIES},
                },
                # The convert stage's list-valued fields, so a caller can see
                # what a field ACCEPTS and not only what it currently is.
                # `defaults` above answers the second question already; this
                # answers the first, which an agent otherwise has to guess at
                # or read out of the source.
                "convert_definitions": {
                    "input": {
                        "values": list(CG.INPUTS),
                        "default": CG.DEFAULTS["convert"]["input"],
                        "note": "what the SOURCE is. 'auto' reads the file's "
                                "own transfer and primaries tags; the rest "
                                "override that. Every value decodes to the "
                                "same scene linear point, so working_space, "
                                "tonemap and encode are unaffected. Each "
                                "clip's own answer is clips[].source. 'auto' "
                                "never picks one of the five camera logs "
                                "(slog3, logc3, vlog, clog3, dlog): no "
                                "container tag tells them apart, so they are "
                                "named here or not used, and each one brings "
                                "its own camera gamut with it.",
                        "camera_logs": list(CG.CAMERA_LOG_INPUTS),
                    },
                    "working_space": {
                        "values": ["dwg", "direct", "rec709"],
                        "default": CG.DEFAULTS["convert"]["working_space"],
                    },
                    "tonemap": {
                        "values": ["aces", "filmic", "none"],
                        "default": CG.DEFAULTS["convert"]["tonemap"],
                    },
                    "encode": {
                        "values": ["rec709a", "gamma24"],
                        "default": CG.DEFAULTS["convert"]["encode"],
                    },
                },
                "scope_aspect": SCOPE_ASPECT,
            })
            return

        if route == "clips" and method == "GET":
            self._json({"clips": self._clips()})
            return

        if route == "browse" and method == "GET":
            self._json(browse_dir(q.get("path"), self.user))
            return

        if route == "open" and method == "POST":
            raw_open = self._body().get("path", "")
            if AUTH.enabled():
                raw_open = str(AUTH.confine(
                    raw_open, AUTH.allowed_roots(self.user, FOOTAGE)))
            # Contract E1: allowed_roots already keeps this inside the roots
            # this account may browse; the library rule is what says whether
            # a file inside one of them has been shared with it.
            LIB.guard_read(self._uid(), raw_open)
            name = register_external_clip(raw_open)
            self._json({"name": name, "clip": self._clip_entry(clip_path(name), name),
                        "clips": self._clips()})
            return

        if route == "upload" and method == "POST":
            self._handle_upload()
            return

        # --- the library, contract E1 -----------------------------------
        #
        # Folders and clips on this server, per account, shared to people and
        # teams the way a drive is. Every one of these routes hands the work
        # to library.py, which owns the path confinement and the permission
        # rule; nothing here builds a path or decides who may do what.
        if route == "library" and method == "GET":
            self._json(LIB.listing(self._uid(), q.get("root", "mine"),
                                   q.get("path", "")))
            return

        if route == "library/mkdir" and method == "POST":
            b = self._body()
            self._json(LIB.mkdir(self._uid(), b.get("root", "mine"),
                                 b.get("path", ""), b.get("name", "")))
            return

        if route == "library/rename" and method == "POST":
            b = self._body()
            self._json(LIB.rename(self._uid(), b.get("root", "mine"),
                                  b.get("path", ""), b.get("name", "")))
            return

        if route == "library/move" and method == "POST":
            b = self._body()
            self._json(LIB.move(self._uid(), b.get("root", "mine"),
                                b.get("path", ""), b.get("to", "")))
            return

        if route == "library/trash" and method == "POST":
            b = self._body()
            self._json(LIB.trash(self._uid(), b.get("root", "mine"),
                                 b.get("path", "")))
            return

        if route == "library/restore" and method == "POST":
            b = self._body()
            self._json(LIB.restore(self._uid(), b.get("path", ""),
                                   b.get("root", "trash")))
            return

        if route == "library/share" and method == "GET":
            self._json(LIB.item_shares(self._uid(), q.get("path", "")))
            return

        if route == "library/share" and method == "POST":
            b = self._body()
            self._json(LIB.share_item(self._uid(), b.get("path", ""),
                                      b.get("kind", "folder"),
                                      b.get("target_kind", "user"),
                                      b.get("target_id", 0),
                                      b.get("role", "viewer")))
            return

        if route == "library/unshare" and method == "POST":
            b = self._body()
            gone = LIB.unshare(self._uid(), int(b.get("share_id", 0) or 0))
            self._json({"removed": gone})
            return

        if route == "people" and method == "GET":
            self._json(LIB.people(self._uid()))
            return

        if route == "activity" and method == "GET":
            self._json(LIB.activity_feed(self._uid(), int(q.get("limit", 50))))
            return

        if route == "frame" and method == "POST":
            if self._stale():
                self._json({"stale": True}, 409)
                return
            payload = self._body()
            if payload.get("path") is not None:
                # Read only: an absolute file outside the footage root,
                # decoded and served without opening a project, registering
                # an EXTERNAL_CLIPS entry or touching this account's session.
                # Logins off only (see path_source_frame's own docstring).
                rgb = path_source_frame(payload, self._uid())
                key = path_source_key(payload)
                headers = {"X-Frame-Key": key,
                          "X-Frame-Size": f"{rgb.shape[1]}x{rgb.shape[0]}"}
                if payload.get("format") == "raw":
                    self._send(200, rgb.tobytes(), "application/octet-stream", headers)
                    return
                body = encode_jpeg(rgb, key, int(payload.get("quality", 2)))
                self._send(200, body, "image/jpeg", headers)
                return
            self._guard_read(payload.get("clip"))
            cfg = full_config(payload.get("config"))
            mode = payload.get("mode", "graded")
            if mode == "flat":
                cfg = flat_config(cfg, bool(payload.get("keep_exposure")))
            elif mode == "mask":
                # mask_layer is the layer whose matte to show. Absent means
                # the first enabled one, which is what a client that has only
                # ever had one layer will send.
                ml = payload.get("mask_layer")
                cfg = mask_preview_config(cfg, None if ml is None else int(ml))
            # region and zoom (contract E4): a close up of one patch, for the
            # four frame viewer and for an agent inspecting skin or a
            # highlight. Absent, this is exactly the render_raw call it was.
            rgb, meta = render_region(payload["clip"], float(payload.get("time", 0)),
                                      int(payload.get("width", 960)), cfg,
                                      effective_rotation(payload, self._uid()),
                                      payload.get("region"), payload.get("zoom"))
            headers = {
                "X-Frame-Key": meta["key"],
                "X-Frame-Size": f"{meta['width']}x{meta['height']}",
            }
            if "region" in meta:
                headers["X-Frame-Region"] = ",".join(f"{v:.6f}" for v in meta["region"])
                headers["X-Frame-Region-Pixels"] = ",".join(str(v) for v in meta["region_pixels"])
                headers["X-Frame-Full-Size"] = f"{meta['full_width']}x{meta['full_height']}"
            if payload.get("format") == "raw":
                # No JPEG in the way, so a parity harness can diff against the
                # true ffmpeg render pixel for pixel instead of through 4:4:4
                # JPEG's own (small but nonzero) quantisation error.
                self._send(200, rgb.tobytes(), "application/octet-stream", headers)
                return
            body = encode_jpeg(rgb, meta["key"], int(payload.get("quality", 2)))
            self._send(200, body, "image/jpeg", headers)
            return

        if route == "source" and method == "POST":
            payload = self._body()
            self._guard_read(payload.get("clip"))
            arr, meta = source_frame(payload["clip"], float(payload.get("time", 0)),
                                     int(payload.get("width", 960)),
                                     effective_rotation(payload, self._uid()))
            self._send(200, arr.tobytes(), "application/octet-stream", {
                "X-Frame-Size": f"{meta['width']}x{meta['height']}",
            })
            return

        # New, self-contained: the live GPU loop's own two routes. Neither
        # touches source_frame's cache or render_raw's, and neither takes a
        # config: this range is ungraded pixels, graded per frame in the
        # browser by gpu.js (see static/live.js).
        if route == "range/limit" and method == "GET":
            self._guard_read(q.get("clip"))
            self._json(range_budget(q["clip"], int(q.get("width", 960)),
                                    effective_rotation(q, self._uid())))
            return

        if route == "range" and method == "POST":
            payload = self._body()
            self._guard_read(payload.get("clip"))
            arr, meta = source_range(payload["clip"], float(payload.get("time", 0)),
                                     float(payload.get("duration", 2.0)),
                                     int(payload.get("width", 960)),
                                     effective_rotation(payload, self._uid()))
            self._send(200, arr.tobytes(), "application/octet-stream", {
                "X-Frame-Size": f"{meta['width']}x{meta['height']}",
                "X-Frame-Count": str(meta["frames"]),
                "X-Fps": f"{meta['fps']:.6f}",
                "X-Range-Start": f"{meta['start']:.6f}",
                "X-Range-Duration": f"{meta['duration']:.6f}",
            })
            return

        if route == "lut" and method == "POST":
            payload = self._body()
            kind = payload.get("kind")
            if kind == "technical":
                p = TECHNICAL / safe_name(payload["name"])
            elif kind == "look":
                name = re.sub(r"\.cube$", "", safe_name(payload["name"]), flags=re.I)
                p = LOOKS / f"{name}.cube"
            elif kind == "layer":
                # deep_merge against LAYER_DEFAULTS so a caller can send a
                # partial layer, the same tolerance every other config entry
                # point into this server already gives the UI. `variant` is
                # which colour matte to fold in ("key", "one" or "inv"); see
                # cinegrade.layer_branches for when a layer needs two cubes.
                lcfg = CG.deep_merge(CG.LAYER_DEFAULTS, payload.get("config") or {})
                variant = payload.get("variant") or "key"
                if variant not in ("key", "one", "inv"):
                    raise StudioError(f"unknown layer lut variant: {variant!r}")
                p = Path(CG.layer_lut(lcfg, variant))
            elif kind == "slice":
                # The hue curves + Color Slice + Tetra stage. The GPU gets the
                # cube the engine itself would hand ffmpeg, baked by the same
                # function, so the preview cannot drift from the render by
                # reimplementing the maths in JavaScript. Same partial-config
                # tolerance as the layer above.
                import slice as SLICE_STAGE
                raw = payload.get("config") or {}
                p = Path(SLICE_STAGE.slice_lut({
                    "hue_curves": CG.deep_merge(CG.DEFAULTS["hue_curves"],
                                                raw.get("hue_curves") or {}),
                    "slice": CG.deep_merge(CG.DEFAULTS["slice"],
                                           raw.get("slice") or {}),
                }))
            else:
                raise StudioError(f"unknown lut kind: {kind!r}")
            if not p.exists():
                raise StudioError(f"lut not found: {p.name}")
            size, data = cached_cube(p)
            body = size.to_bytes(4, "little") + data.tobytes()
            self._send(200, body, "application/octet-stream",
                       {"X-Lut-Size": str(size)})
            return

        # Film grain (C3): the plate ffmpeg's own grain block would build for
        # a still at these dimensions, so the GPU preview overlays the same
        # bytes the render does instead of falling back to the server for
        # every grain-on config. response is never accepted here: it is a
        # weight computed from the picture's own luminance, applied by the
        # caller against its own live texture, not baked into this plate.
        if route == "grain/plate" and method == "GET":
            g: dict = {}
            if q.get("stock"):
                g["stock"] = q["stock"]
            for key in ("size", "strength", "seed"):
                if key in q:
                    g[key] = int(q[key])
            for key in ("softness", "color"):
                if key in q and q[key] not in (None, ""):
                    g[key] = float(q[key])
            width = int(q.get("width", 960))
            height = int(q.get("height", 540))
            data = grain_plate(g, width, height)
            self._send(200, data, "application/octet-stream",
                       {"X-Frame-Size": f"{width}x{height}"})
            return

        # The live session is per account (wave 2). The wire shape is
        # unchanged, including the `by` field session.js filters its own
        # echoes on: only the state it reads and writes is now this user's.
        if route == "session" and method == "GET":
            self._json(live_get(self._uid()))
            return

        if route == "session" and method == "POST":
            payload = self._body()
            # Contract E1: a session write is a commit, so somebody holding
            # this clip as a viewer is refused here rather than discovering it
            # after the fact when their edit is not in the history.
            self._guard_edit(clip=payload.get("clip"))
            # An agent that sent no `by` is labelled with its own name rather
            # than the generic `cli` (contract G2, the default `by`). The tab
            # filters its own echo on this field, so a caller that DID send
            # one keeps it exactly.
            if self.caller_agent and not str(payload.get("by") or "").strip():
                payload = dict(payload)
                payload["by"] = self.caller_name
            try:
                self._json(live_set(payload, self._uid(),
                                    author=self._author(payload.get("by"))))
            except LiveRevMismatch as exc:
                # 409, and the config is untouched: the caller read revision
                # N, somebody else has since written N+1, and merging blind
                # would silently throw their edit away.
                self._json({"error": str(exc), "rev": exc.rev,
                            "session": exc.state}, 409)
            return

        if route == "session/wait" and method == "GET":
            # Held open until something changes, so the browser sees an outside
            # edit immediately instead of on its next poll tick.
            self._json(live_wait(int(q.get("since", 0)),
                                 float(q.get("timeout", 25)), self._uid()))
            return

        # --- projects and history, contract C1 --------------------------
        #
        # A project is one clip's whole state: rotation, playhead, loaded
        # preset, extras, and the commit tree. Shared by every account, keyed
        # by the clip's content, and the thing the live session above is now a
        # view of. The auth and CSRF checks at the top of _api already cover
        # every route here, the same way they cover /api/session.
        if route == "whoami" and method == "GET":
            self._json({
                "user": (self.user["name"] if self.user else None),
                "by": self._author(None),
                "auth": AUTH.enabled(),
                "project": PROJECTS.workspace_key(self._uid()),
                # Same block as GET /api/state, so one call answers "who does
                # this server think I am" whichever of the two you reach for.
                "caller": self._caller_block(),
            })
            return

        if route == "project" and method == "GET":
            self._json(self._project_body(q.get("clip")))
            return

        if route == "project/open" and method == "POST":
            payload = self._body()
            clip = str(payload.get("clip", "") or "")
            if not clip and payload.get("path") is not None:
                # Contract E1: a clip can also be addressed as a library item,
                # which is how a file opened from somebody else's shared
                # folder gets in. The content key is the same whoever owns the
                # file, so the project and its history are shared
                # automatically: that is the founder's "when people make
                # updates it shows up in history".
                clip = self._library_clip(payload.get("root", "mine"),
                                          payload.get("path", ""))
            if not clip:
                raise StudioError("no clip given")
            self._guard_read(clip)
            proj = PROJECTS.open(self._uid(), clip)
            live_touch(self._uid(), proj, self._author(payload.get("by")))
            self._json(self._project_body(clip, proj))
            return

        if route == "project/rotation" and method == "POST":
            payload = self._body()
            key = self._edit_key(payload.get("clip"))
            value = payload.get("rotation")
            if value is None and payload.get("autorotate") is not None:
                # The old wire word, still accepted everywhere in this arc:
                # true is "honour the file's tag", false is "no rotation".
                value = "auto" if payload.get("autorotate") else "0"
            proj = PROJECTS.set_rotation(self._uid(), key, str(value))
            live_touch(self._uid(), proj, self._author(payload.get("by")))
            self._json(self._project_body(None, proj))
            return

        if route == "project/time" and method == "POST":
            payload = self._body()
            key = self._open_key(payload.get("clip"))
            proj = PROJECTS.set_time(self._uid(), key, payload.get("time"))
            # Deliberately no revision bump: the playhead moves continuously
            # while somebody scrubs, and waking every long poll on each frame
            # would be a stream of wakeups for something the tab already knows.
            with _live_lock:
                _live_state(self._uid())["time"] = float(proj["time"])
            self._json(self._project_body(None, proj))
            return

        if route == "project/preset" and method == "POST":
            payload = self._body()
            key = self._open_key(payload.get("clip"))
            proj = PROJECTS.set_preset(self._uid(), key, payload.get("name"))
            self._json(self._project_body(None, proj))
            return

        if route == "project/extra" and method == "POST":
            payload = self._body()
            key = self._edit_key(payload.get("clip"))
            proj = PROJECTS.set_extra(self._uid(), key,
                                      str(payload.get("name", "") or ""),
                                      payload.get("value"))
            self._json(self._project_body(None, proj))
            return

        if route == "project/log" and method == "GET":
            key = self._open_key(q.get("clip"))
            self._json(PROJECTS.log(self._uid(), key,
                                    int(q.get("limit", 200))))
            return

        if route == "project/checkout" and method == "POST":
            payload = self._body()
            key = self._edit_key(payload.get("clip"))
            proj = PROJECTS.checkout(self._uid(), key,
                                     str(payload.get("commit", "") or ""))
            live_touch(self._uid(), proj, self._author(payload.get("by")))
            self._json(self._project_body(None, proj))
            return

        if route == "project/fork" and method == "POST":
            payload = self._body()
            key = self._edit_key(payload.get("clip"))
            proj = PROJECTS.fork(self._uid(), key,
                                 from_commit=(payload.get("commit") or None),
                                 name=(payload.get("name") or None))
            live_touch(self._uid(), proj, self._author(payload.get("by")))
            self._json(self._project_body(None, proj))
            return

        if route in ("project/undo", "project/redo") and method == "POST":
            payload = self._body()
            key = self._edit_key(payload.get("clip"))
            step = PROJECTS.undo if route.endswith("undo") else PROJECTS.redo
            proj = step(self._uid(), key)
            if proj.get("moved"):
                live_touch(self._uid(), proj, self._author(payload.get("by")))
            out = self._project_body(None, proj)
            out["moved"] = bool(proj.get("moved"))
            if proj.get("note"):
                out["note"] = proj["note"]
            self._json(out)
            return

        # --- per clip grades, contract C3 -------------------------------
        #
        # A grade belongs to (this account, this clip's content key). The
        # client talks in clip NAMES because that is what every other route
        # here takes; the key is resolved server side so a rename cannot
        # separate a clip from its grade.
        if route == "grade" and method == "GET":
            key = self._grade_key(q.get("clip", ""))
            # Contract C1: once a project exists, HEAD is the grade. The old
            # per account row is only consulted for a clip nobody has opened
            # yet, which is also the row that becomes that project's root the
            # moment somebody does.
            proj = PROJECTS.state(key)
            if proj is not None:
                self._json({"exists": True, "key": key,
                            "config": proj["config"],
                            "updated_at": proj["updated"],
                            "head": proj["head_short"],
                            "branch": proj["branch"]})
                return
            row = GRADES.get_grade(self._uid(), key)
            if row is None:
                self._json({"exists": False, "key": key, "config": None,
                            "updated_at": None})
                return
            self._json({"exists": True, "key": key, "config": row["config"],
                        "updated_at": row["updated_at"]})
            return

        if route == "grade" and method == "PUT":
            payload = self._body()
            name = str(payload.get("clip", "") or "")
            # Contract E1, same rule as a session write: saving a grade is a
            # commit, and a viewer does not get to make one.
            self._guard_edit(clip=(name if not GRADES.is_key(name) else None),
                             key=(name.lower() if GRADES.is_key(name) else None))
            key = self._grade_key(name)
            # A caller that addressed the clip by key (a backup being put
            # back, an agent working from GET /api/grades) can still say what
            # the clip was called, so the picker does not end up listing raw
            # hex. Addressing by name is the normal case and needs none of it.
            if GRADES.is_key(name):
                prev = GRADES.get_grade(self._uid(), key)
                name = (str(payload.get("clip_name") or "")
                        or (prev["clip_name"] if prev else ""))
            # full_config so a partial patch is stored whole: a later read
            # then loads into the UI without depending on whatever the engine
            # defaults happened to be on the day it was saved.
            cfg = full_config(payload.get("config"))
            updated = GRADES.put_grade(self._uid(), key, name, cfg)
            # Contract C1: a save is a commit. The old row is written too, and
            # deliberately: GET /api/grades and the copy picker are built from
            # it, and it is the migration source for any account that opens
            # this clip on a server that has never seen it.
            head = None
            try:
                path = ""
                try:
                    path = str(clip_path(name)) if name else ""
                except StudioError:
                    path = ""
                PROJECTS.ensure(key, name, path)
                proj = PROJECTS.commit(self._uid(), key, cfg,
                                       self._author(payload.get("by") or "cli"),
                                       message=str(payload.get("message")
                                                   or "saved grade"))
                head = proj["head_short"]
                if PROJECTS.workspace_key(self._uid()) == key:
                    live_sync(self._uid(), proj)
            except Exception as exc:                          # noqa: BLE001
                if VERBOSE:
                    print(f"grade save: no project recorded ({exc})",
                          file=sys.stderr)
            self._json({"key": key, "updated_at": updated, "head": head})
            return

        if route == "grades" and method == "GET":
            self._json({"grades": GRADES.list_grades(self._uid())})
            return

        if route == "grade" and method == "DELETE":
            key = self._grade_key(q.get("clip", ""))
            gone = GRADES.delete_grade(self._uid(), key)
            # Contract C1 keeps this route's promise: after a delete, the clip
            # reads as ungraded. HEAD is the grade now, so the delete is a
            # commit back to the defaults rather than a row removal, which is
            # the same result WITHOUT destroying a shared project's history.
            # Deleting the tree instead was considered and refused: the UI
            # harness clears every grade at the start of each run, and that
            # would have made a test run wipe the founder's history.
            head = None
            try:
                proj = PROJECTS.state(key)
                if proj is not None:
                    proj = PROJECTS.commit(self._uid(), key,
                                           full_config({}),
                                           self._author(q.get("by")))
                    head = proj["head_short"]
                    if PROJECTS.workspace_key(self._uid()) == key:
                        live_sync(self._uid(), proj)
            except Exception as exc:                          # noqa: BLE001
                if VERBOSE:
                    print(f"grade delete: no project reset ({exc})",
                          file=sys.stderr)
            self._json({"deleted": gone, "key": key, "head": head})
            return

        if route == "grade/copy" and method == "POST":
            payload = self._body()
            src = self._grade_key(str(payload.get("from", "") or ""))
            dst_name = str(payload.get("to", "") or "")
            dst = self._grade_key(dst_name)
            if GRADES.is_key(dst_name):
                # The caller addressed the destination by key, so there is no
                # display name in the request. Keep whatever name that clip
                # was last saved under rather than writing the key into the
                # name column and making the picker unreadable.
                prev = GRADES.get_grade(self._uid(), dst)
                dst_name = prev["clip_name"] if prev else ""
            row = GRADES.get_grade(self._uid(), src)
            if row is None:
                raise StudioError("that clip has no saved grade to copy")
            if src == dst:
                raise StudioError("source and destination are the same clip")
            updated = GRADES.put_grade(self._uid(), dst, dst_name,
                                       full_config(row["config"]))
            self._json({"key": dst, "from": src, "config": row["config"],
                        "updated_at": updated})
            return

        if route == "match" and method == "POST":
            payload = self._body()
            self._guard_read(payload.get("clip"))
            # The project's stored rectangles are a browser tab convenience,
            # not a default: an agent gets them only by sending them.
            result = match_reference_job(
                payload, self._uid(),
                use_stored_crops=not self.caller_agent)
            # Round 2 tooling item 5: "looks" is the whole ~60 entry look
            # catalogue, put here so the browser tab's dropdown can refresh
            # from this one response (see fillLooks(result.looks) in
            # static/app.js) instead of a second GET /api/looks. An agent has
            # no dropdown and was paying thousands of tokens for it on every
            # match; drop it for an agent caller only, so the browser's
            # response is unchanged byte for byte.
            if self.caller_agent:
                result.pop("looks", None)
            self._json(result)
            return

        if route == "parity/report" and method == "POST":
            payload = self._body()
            STUDIO_TOOLS.mkdir(parents=True, exist_ok=True)
            PARITY_REPORT.write_text(json.dumps(payload, indent=2))
            self._json({"ok": True})
            return

        if route == "parity/report" and method == "GET":
            if PARITY_REPORT.exists():
                self._json(json.loads(PARITY_REPORT.read_text()))
            else:
                self._json({})
            return

        if route == "stats" and method == "POST":
            payload = self._body()
            # path and ref are both read only and register nothing; only the
            # clip form touches this account's read guard.
            if payload.get("path") is None and payload.get("ref") is None:
                self._guard_read(payload.get("clip"))
            # C6, "measure by matte": weights every percentile, band and hue
            # family by the matte's value at that frame instead of measuring
            # the whole picture flat. Resolved once, outside the times loop,
            # since the matte itself does not change per requested time.
            matte_param = payload.get("matte")
            matte_info = None
            if matte_param:
                matte_info = _matte_info(str(matte_param))
                self._guard_read(matte_info.clip)
            # Checkpoint gap 19, "measure by the whole mask": `mask` is a
            # layer's own mask block (a component stack), which says the one
            # thing `matte` cannot, "this matte intersected with this colour
            # key". Refused alongside `matte` (two ways to say one thing) and
            # alongside `region` (a stack is written in the whole frame's
            # coordinates, so composing it inside a crop would move every
            # window and misalign every matte; a `window` component says any
            # rectangle the stack needs). Validated here, before the first
            # render, so a bad description costs no ffmpeg.
            mask_param = payload.get("mask")
            # `is not None` throughout, never truthiness: {"mask": {}} would
            # be dropped by a falsy check and the whole frame measured while
            # the caller believed a mask was applied. It is refused instead.
            has_mask = mask_param is not None
            if has_mask and not isinstance(mask_param, dict):
                raise StudioError(
                    "mask has to be an object: a layer's own mask block, "
                    "{\"components\": [...], \"finesse\": {...}}")
            if has_mask and matte_param:
                raise StudioError(
                    "matte and mask both weight the measurement; send one. "
                    "\"matte\": ID is the one-component shortcut for "
                    "{\"components\": [{\"type\": \"matte\", \"op\": \"add\", "
                    "\"matte\": {\"id\": ID}}]}")
            if has_mask and payload.get("region") is not None:
                raise StudioError(
                    "mask and region do not compose: a component stack is "
                    "written in the whole frame's coordinates, so composing it "
                    "inside a crop would move every window and misalign every "
                    "matte. Say the rectangle with a window component inside "
                    "mask instead, or drop region")
            if has_mask:
                CG.mask_stack_layer(mask_param)      # refuse a no-op now
            # Whichever way the weight was asked for, it has to be a matte of
            # THIS clip: measuring one clip through another clip's matte used
            # to answer with numbers, no warning and exit 0. Once, here,
            # before the first frame is rendered, so a mismatch costs no
            # ffmpeg.
            _require_stats_mattes_match(payload.get("clip"), matte_info,
                                       mask_param)
            times = payload.get("times")
            if times:
                results = []
                for t in times:
                    rgb, meta = resolve_stats_frame(payload, self._uid(), float(t))
                    row = {"time": float(t), "key": meta["key"]}
                    weight = None
                    warns = []
                    if matte_info is not None:
                        weight, _served, warn = _matte_weight_for(
                            matte_info, float(t), meta)
                        if warn:
                            warns.append(warn)
                    elif has_mask:
                        weight, warns, mask_ids = _mask_stack_weight(
                            mask_param, float(t), rgb, self._guard_read)
                        row["mask_mattes"] = mask_ids
                    if warns:
                        row["warnings"] = warns
                    try:
                        row["stats"] = frame_stats(rgb, weight=weight)
                    except StatsError as exc:
                        raise StudioError(str(exc)) from exc
                    row["size"] = [meta.get("width", rgb.shape[1]),
                                   meta.get("height", rgb.shape[0])]
                    # Checkpoint gap 11: this route measures a 640 wide
                    # preview unless `width` says otherwise, while the
                    # standalone CLI measures the source's own resolution.
                    # Both now state the width they measured at, so two
                    # numbers from the two paths cannot be compared without
                    # noticing they came from different samples.
                    row["measured_width"] = int(row["size"][0])
                    _stamp_coverage(row)                    # gaps 19 and 23
                    if "region" in meta:
                        row["region"] = meta["region"]
                        row["region_pixels"] = meta["region_pixels"]
                    results.append(row)
                self._json({"results": results})
                return
            time_used = float(payload.get("time", 0))
            rgb, meta = resolve_stats_frame(payload, self._uid(), time_used)
            weight = None
            warnings = []
            mask_ids = None
            if matte_info is not None:
                weight, _served, warn = _matte_weight_for(matte_info, time_used, meta)
                if warn:
                    warnings.append(warn)
            elif has_mask:
                weight, warnings, mask_ids = _mask_stack_weight(
                    mask_param, time_used, rgb, self._guard_read)
            try:
                stats_out = frame_stats(rgb, weight=weight)
            except StatsError as exc:
                raise StudioError(str(exc)) from exc
            out = {"key": meta["key"], "stats": stats_out,
                   "size": [meta.get("width", rgb.shape[1]),
                            meta.get("height", rgb.shape[0])]}
            out["measured_width"] = int(out["size"][0])       # gap 11, above
            _stamp_coverage(out)                             # gaps 19 and 23
            if "region" in meta:
                out["region"] = meta["region"]
                out["region_pixels"] = meta["region_pixels"]
            if matte_info is not None:
                out["matte"] = matte_info.matte_id
            if mask_ids is not None:
                out["mask_mattes"] = mask_ids
            if warnings:
                out["warnings"] = warnings
            self._json(out)
            return

        if route == "scope" and method == "POST":
            payload = self._body()
            self._guard_read(payload.get("clip"))
            cfg = full_config(payload.get("config"))
            rgb, meta = render_raw(payload["clip"], float(payload.get("time", 0)),
                                   int(payload.get("width", 640)), cfg,
                                   effective_rotation(payload, self._uid()))
            body = render_scope(rgb, meta["key"], payload.get("kind", "waveform"),
                                int(payload.get("size", 480)))
            self._send(200, body, "image/jpeg")
            return

        if route == "thumb" and method == "GET":
            # Contract E1: a thumbnail can be asked for by clip name (what it
            # always took) or by library item, which is how the files pane
            # draws a row for a clip in a folder it does not otherwise have a
            # name for. The permission check is the library's read rule.
            clip = q.get("clip") or ""
            if not clip and q.get("path") is not None:
                clip = self._library_clip(q.get("root", "mine"),
                                          q.get("path", ""))
            if not clip:
                raise StudioError("thumb needs a clip, or a root and a path")
            self._guard_read(clip)
            body = thumbnail(clip, float(q.get("t", 0)),
                             int(q.get("w", 160)),
                             effective_rotation(q, self._uid()))
            self._send(200, body, "image/jpeg")
            return

        if route == "ref" and method == "GET":
            self._send(200, ref_image(q["name"], int(q.get("w", 900))), "image/jpeg")
            return

        # Presets are the one per user store that is SHARED between callers
        # when logins are off (contract G2): _presets_uid() is 0 for the
        # browser tab and for every agent alike, so an agent's "save as" lands
        # where the person at the keyboard will see it, and the other way
        # round. With logins on it is the account, unchanged.
        if route == "presets" and method == "GET":
            self._json({"presets": list_presets(self._presets_uid())})
            return

        if route == "preset" and method == "GET":
            name = q["name"]
            uid = self._presets_uid()
            cfg = read_preset(name, uid)
            body = {"name": name,
                    "comment": read_preset_comment(name, uid),
                    "config": cfg}
            if str(q.get("expand", "")).strip().lower() in ("1", "true", "yes"):
                body["expanded"] = True
            # "Recipe resolution on preset load" (C4): optional, additive.
            # Every existing caller (no `clip` on the query) sees exactly
            # today's response; a caller that names the clip it is applying
            # this preset to gets its mask components resolved against that
            # clip's own matte store, with a portable (text only) recipe
            # queued to track right away instead of waiting for a person to
            # notice a "needs pick" badge that does not apply to it.
            clip = q.get("clip")
            if clip and isinstance(cfg, dict):
                self._guard_read(clip)
                rotation = effective_rotation(q, self._uid())
                resolved, queued = resolve_preset_mask_recipes(clip, rotation, cfg)
                body["config"] = resolved
                if queued:
                    body["mask_queued"] = queued
            self._json(body)
            return

        if route == "preset" and method == "POST":
            payload = self._body()
            p = write_preset(payload["name"], payload.get("config"),
                             payload.get("comment", ""), self._presets_uid())
            if self.caller_agent:
                # Round 2 tooling item 5: "presets" below is the entire
                # library listing, sent back on every single save. The
                # browser tab's savePreset() reads it (fillPresets(j.presets)
                # repaints the dropdown); an agent does not have a dropdown
                # and was paying for the whole library on every save. An
                # agent gets confirmation of the call it made instead: the
                # name it saved under, the comment that ended up on disk
                # (write_preset() can keep an existing comment when this
                # call sent none), the path, and ok. Ask GET /api/presets
                # for the list.
                self._json({"name": p.stem,
                            "comment": read_preset_comment(
                                p.stem, self._presets_uid()),
                            "path": str(p), "ok": True})
                return
            self._json({"saved": p.name, "path": str(p),
                        "presets": list_presets(self._presets_uid())})
            return

        if route == "preset" and method == "DELETE":
            p, library = preset_path(q["name"], self._presets_uid())
            if not p.exists():
                raise StudioError(f"preset not found: {q['name']}")
            if library:
                # 403, not 400: the request is well formed and the file is
                # right there, the caller is simply not allowed to remove it.
                # grade/presets/ is checked into the repository and shared by
                # every account, so deleting one here would be one user
                # deleting a tracked file out from under everybody else.
                raise AUTH.AuthError(
                    403, f"{q['name']} is a shipped library preset and is read "
                         "only. Save your own copy under the same name to "
                         "shadow it, and delete that instead")
            p.unlink()
            self._json({"deleted": q["name"],
                        "presets": list_presets(self._presets_uid())})
            return

        if route == "looks" and method == "GET":
            self._json({"looks": list_looks()})
            return

        if route == "look" and method == "GET":
            p = LOOKS / f"{safe_name(q['name'])}.cube"
            if not p.exists():
                raise StudioError(f"look not found: {q['name']}")
            self._send(200, p.read_bytes(), "text/plain; charset=utf-8",
                       {"Content-Disposition":
                        f'attachment; filename="{p.name}"'})
            return

        if route == "look" and method == "POST":
            name = safe_name(q.get("name") or "imported")
            name = re.sub(r"\.cube$", "", name, flags=re.I)
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
                raise StudioError("LUT names may use letters, digits, dot, "
                                  "dash and underscore only")
            text = self._raw_body(LOOK_MAX_BYTES).decode("utf-8", "replace")
            size = validate_cube(text)
            (LOOKS / f"{name}.cube").write_text(text)
            self._json({"imported": name, "size": size, "looks": list_looks()})
            return

        if route == "look" and method == "DELETE":
            name = safe_name(q["name"])
            p = LOOKS / f"{name}.cube"
            if not p.exists():
                raise StudioError(f"look not found: {name}")
            p.unlink()
            self._json({"deleted": name, "looks": list_looks()})
            return

        if route == "rebuild" and method == "POST":
            # Rebuilding the technical LUTs rewrites files every other
            # session on this server reads, so it is an admin action once
            # more than one person is connected.
            self._require_admin()
            job = start_rebuild(self._body().get("which", "all"))
            self._json({"job": job.as_dict()})
            return

        if route == "render" and method == "POST":
            body = self._body()
            # Both engines come through here, so this one check also covers
            # the GPU plan built by render_gpu.start_gpu_render. The worker's
            # own routes (render/gpu/*) are not checked here and do not need
            # to be: they carry the one-off token minted for a render that
            # already passed this line, and they serve nothing but that job.
            self._guard_read(body.get("clip"))
            # The GPU engine spawns a browser that has to fetch frames back
            # from this server, and only the request knows which port that is
            # (the studio takes --port, and the tests run it on a random one).
            body.setdefault("port", self.server.server_address[1])
            job = start_render(body, self._uid())
            self._json({"job": job.as_dict()})
            return

        if route == "jobs" and method == "GET":
            with JOBS_LOCK:
                jobs = [j.as_dict() for j in
                        sorted(JOBS.values(), key=lambda j: -j.started)]
            self._json({"jobs": jobs, "renders": list_renders()})
            return

        if route == "job/cancel" and method == "POST":
            job = JOBS.get(self._body().get("id", ""))
            if not job:
                raise StudioError("no such job")
            job.status = "cancelled"
            if job.proc and job.proc.poll() is None:
                job.proc.send_signal(signal.SIGINT)
            self._json({"job": job.as_dict()})
            return

        if route == "renders" and method == "GET":
            self._json({"renders": list_renders()})
            return

        if route == "reveal" and method == "POST":
            # Reveal opens a Finder window on the machine running the server.
            # Over a network that is somebody else's desktop, so it answers
            # only a browser on this same machine. With logins off the server
            # is bound to 127.0.0.1 and every caller is loopback, so this
            # changes nothing for local use. trusted_loopback (not
            # is_loopback) also refuses everyone when --behind-https-proxy is
            # set: a same-machine proxy forwarding to this loopback bind
            # makes every request, including one relayed from the internet,
            # arrive with a loopback peer address, so the address alone can
            # no longer prove the caller is local.
            if not AUTH.trusted_loopback(
                    self.client_address[0] if self.client_address else ""):
                raise AUTH.AuthError(403, "Reveal opens a Finder window on the "
                                          "computer running the studio, so it "
                                          "only answers a browser on that same "
                                          "computer")
            target = Path(self._body().get("path", ""))
            allowed = [OUT.resolve(), PRESETS.resolve(), LOOKS.resolve(),
                       REFS.resolve(),
                       # This account's own preset folder. A preset saved
                       # since wave 2 lands there rather than in the shipped
                       # library, so without this entry "reveal" on a preset
                       # the user just saved would refuse to show it.
                       GRADES.user_presets_dir(self._presets_uid()).resolve()]
            if not any(str(target.resolve()).startswith(str(a)) for a in allowed):
                raise StudioError("that path is outside the studio folders")
            subprocess.Popen(["open", "-R", str(target)])
            self._json({"revealed": str(target)})
            return

        if route == "cache/clear" and method == "POST":
            # Same reasoning as rebuild: the frame cache is shared, so one
            # user clearing it slows every other session down.
            self._require_admin()
            self._json({"cleared": clear_frame_cache()})
            return

        if route == "play/prepare" and method == "POST":
            body = self._body()
            self._guard_read(body.get("clip"))
            params = _play_params(body, self._uid())
            _remember_key(params["key"], params["clip"])
            cache_path = _segment_path(params["key"])
            cached = cache_path.exists()
            if cached:
                os.utime(cache_path, None)
            else:
                # The stream route (GET, so a <video src> can hit it
                # directly) only carries the key; this is where the ffmpeg
                # args it needs on a cache miss get parked for it.
                _register_pending(params)
            self._json({
                "key": params["key"], "cached": cached,
                "start": params["start"], "duration": params["duration"],
                "width": params["pinfo"]["width"], "height": params["pinfo"]["height"],
            })
            return

        if route == "play/stream" and method == "GET":
            self._guard_read_key(q.get("key", ""))
            self._play_stream(q.get("key", ""))
            return

        # Playback proxy (live.js Mode 3). prepare is the only POST: it
        # answers with a URL when the file is already on disk and with a job
        # to watch when it is not, so the client has one call to make either
        # way. The mp4 itself is a plain GET so a <video src> can point at it.
        if route == "proxy/prepare" and method == "POST":
            body = self._body()
            self._guard_read(body.get("clip"))
            params = _proxy_params(body, self._uid())
            _remember_key(params["key"], params["clip"])
            self._json(proxy_state(params))
            return

        if route.startswith("proxy/") and method == "GET":
            name = route[len("proxy/"):]
            self._guard_read_key(name[:-4] if name.endswith(".mp4") else name)
            self._serve_proxy(name)
            return

        # --- masks: SAM 3.1, contracts C3/C4 ------------------------------
        #
        # /api/mask/* is picking and tracking, ephemeral except for the
        # matte ids and jobs it produces. /api/matte/* reads and manages the
        # matte store itself (C2). Neither touches the live session or the
        # project history: a matte is per clip and shared, not per caller
        # (design rule: "per caller session untouched").

        if route == "mask/status" and method == "GET":
            try:
                health = SAMC.client().health()
                ok = bool(health.get("ok", True))
            except SAMC.SamUnavailable as exc:
                health = {"ok": False, "error": str(exc)}
                ok = False
            with JOBS_LOCK:
                jobs = []
                for j in JOBS.values():
                    if j.kind not in ("mask_track", "mask_proxy"):
                        continue
                    view = j.as_dict()
                    # The service's own answer for this job, forwarded rather
                    # than re-derived, so this route and the SAM service can
                    # never disagree about it (checkpoint gap 21). A studio job
                    # reads `status: "running"` from the moment it is queued,
                    # which is honest about the studio and says nothing about
                    # the model: `service_state` is the SAM side's one state,
                    # `holds_model` is false while the job waits its turn behind
                    # another one, `matte_states` is that job's own mattes (they
                    # move to running before the job does, so they cannot lag
                    # behind it), and `queue_position` is how many are ahead.
                    sam_state = view.get("sam_state") or {}
                    if sam_state:
                        view["service_state"] = sam_state.get("state")
                        view["holds_model"] = bool(sam_state.get("holds_model"))
                        view["matte_states"] = sam_state.get("matte_states") or []
                        view["queue_position"] = sam_state.get("queue_position")
                    jobs.append(view)
            self._json({"ok": ok, "service": health, "jobs": jobs,
                        # Which mask job the shared model is on RIGHT NOW, or
                        # null, straight from /health. Two callers polling two
                        # tracks both read "running" while only one of them has
                        # the model, and "the model is busy with somebody else's
                        # clip" is the answer to "why is mine not moving"
                        # (checkpoint gap 21).
                        "model_holder": health.get("model_holder"),
                        # Quiet mode, lifted out of `service` to the top level
                        # because it is the answer to "why has this track not
                        # moved in three minutes": under a duty cycle the
                        # service is deliberately asleep between windows, and a
                        # poller that only reads `jobs` cannot tell that from a
                        # stuck model. `duty_cycle` is the setting,
                        # `busy_fraction` is what actually happened, `resting`
                        # and `rest_left_s` are the live state.
                        "throttle": health.get("throttle"),
                        # The same three paths GET /api/health carries, so a
                        # mask-only caller (the CLI's `mask` group, an agent
                        # already polling this route) does not need a second
                        # request to find the store or the footage root
                        # (checkpoint gaps 4 and 5).
                        "data_dir": str(DB.DATA),
                        "matte_root": str(MT.matte_root()),
                        "footage_dir": str(FOOTAGE),
                        "mask_width": MASK_WORKING_WIDTH,
                        "quality_thresholds": MT.quality_thresholds()})
            return

        if route == "mask/segment" and method == "POST":
            payload = self._body()
            self._guard_read(payload.get("clip"))
            rotation = effective_rotation(payload, self._uid())
            self._json(mask_segment(
                payload["clip"], float(payload.get("time", 0)), rotation,
                payload.get("prompts") or {}, payload.get("max_instances")))
            return

        if route == "mask/track" and method == "POST":
            payload = self._body()
            self._guard_read(payload.get("clip"))
            rotation = effective_rotation(payload, self._uid())
            self._json(queue_mask_track(
                payload["clip"], rotation, payload.get("prompts"),
                payload.get("select"), payload.get("steady"),
                payload.get("pick_id"), payload.get("start"), payload.get("end"),
                force=bool(payload.get("force"))))
            return

        if route.startswith("mask/pick/") and method == "GET":
            # /api/mask/pick/<pick_id>/<instance_id>/<overlay|mask>: the
            # preview images GET /api/mask/segment's own response points at.
            parts = route[len("mask/pick/"):].split("/")
            if len(parts) != 3:
                raise StudioError(f"no route for {method} {path}")
            pick_id, inst_id, kind = parts
            with PICKS_LOCK:
                entry = PICKS.get(pick_id)
            if not entry:
                raise HttpError(404, f"no such pick: {pick_id}")
            self._guard_read(entry.get("clip"))
            inst = entry.get("instances", {}).get(inst_id)
            if not inst or kind not in ("overlay", "mask"):
                raise HttpError(404, f"no {kind} for pick {pick_id} instance {inst_id}")
            p = Path(inst[kind])
            if not p.is_file():
                raise HttpError(404, f"{kind} image for pick {pick_id} instance "
                                f"{inst_id} is gone")
            ctype = "image/jpeg" if kind == "overlay" else "image/png"
            self._send(200, p.read_bytes(), ctype)
            return

        if route == "mask/jobs" and method == "GET":
            # The whole queue, every caller's mask track jobs together
            # (README's own CLI table row for `mask jobs`, and the shape
            # `cinegrade mask jobs` has always parsed): the same per job
            # view GET /api/mask/jobs/<id> gives, one entry per still
            # queued or running (or recently finished, same 30 job cap as
            # every other job list) mask_track job, so a caller reads one
            # shape whether it asks for one job or all of them.
            with JOBS_LOCK:
                jobs = [_mask_job_view(j) for j in JOBS.values()
                       if j.kind == "mask_track"]
            self._json({"jobs": jobs})
            return

        if route.startswith("mask/jobs/") and method == "GET":
            job_id = route[len("mask/jobs/"):]
            job = JOBS.get(job_id)
            if not job or job.kind != "mask_track":
                raise HttpError(404, f"no mask track job: {job_id}")
            self._json(_mask_job_view(job))
            return

        if (route.startswith("mask/jobs/") and route.endswith("/cancel")
                and method == "POST"):
            # README's own documented route (C4). Functionally this was
            # already reachable through the generic POST /api/job/cancel
            # (body {"id": job_id}), which _poll_sam_job's loop already
            # watches job.status == "cancelled" for; this is the same one
            # line, scoped to a mask track job id in the path and answering
            # in the mask jobs panel's own shape rather than the generic
            # job dict, so a caller reads one response shape whichever
            # cancel route it used.
            job_id = route[len("mask/jobs/"):-len("/cancel")]
            job = JOBS.get(job_id)
            if not job or job.kind != "mask_track":
                raise HttpError(404, f"no mask track job: {job_id}")
            job.status = "cancelled"
            self._json(_mask_job_view(job))
            return

        if route == "matte" and method == "GET":
            clip = q.get("clip")
            if clip:
                self._guard_read(clip)
                infos = list_clip_mattes(mask_clip_key(clip))
            else:
                # The `?clip=` branch above guards, so this one has to as
                # well, or it is simply the way around it: a matte summary
                # carries the clip's own file name and the matte's recipe,
                # which for a text prompt is the prompt words themselves. Any
                # signed in account could read back the name of every clip
                # anyone on this server has ever tracked a matte on, which is
                # exactly what _guard_read exists to stop.
                #
                # Per matte, dropping what this caller may not read rather
                # than refusing the whole list: a shared server's list route
                # has to stay usable while somebody else's matte is in the
                # store. With logins off _guard_read is a no-op, so the local
                # and the agent case answer what they always answered.
                infos = [i for i in list_all_mattes() if self._may_read(i.clip)]
            # Checkpoint gap 6: the summary is the default here and the per
            # frame arrays are opt in (`?full=1`). `GET /api/matte/<id>`
            # below is unchanged and still carries them, so the one caller
            # that plots the curve (`mask show --strip`) reads the same
            # bytes it always did.
            full = str(q.get("full", "")).strip().lower() in ("1", "true", "yes")
            self._json({"mattes": [_matte_summary(i, full=full,
                                                  quality_limit=8)
                                   for i in infos],
                        "full": full})
            return

        if route.startswith("matte/") and route.endswith("/frame") and method == "GET":
            matte_id = route[len("matte/"):-len("/frame")]
            info = _matte_info(matte_id)
            self._guard_read(info.clip)
            width = q.get("width")
            png, headers = matte_frame_png(
                matte_id, float(q.get("time", 0)), int(width) if width else None)
            self._send(200, png, "image/png", headers)
            return

        if route.startswith("matte/") and method == "GET":
            matte_id = route[len("matte/"):]
            info = _matte_info(matte_id)
            self._guard_read(info.clip)
            self._json(_matte_summary(info))
            return

        if route.startswith("matte/") and method == "DELETE":
            matte_id = route[len("matte/"):]
            info = _matte_info(matte_id)
            self._guard_read(info.clip)
            # Design rule: mattes are shared by every caller, so deleting one
            # needs admin whenever logins are on; with logins off (the local
            # and the agent case both) this is a no-op, same as every other
            # admin gate in this file.
            self._require_admin()
            # Both guards above return immediately with logins off, so this is
            # the only check left in the default configuration: the directory
            # about to go has to be a real matte inside the store. Without it
            # this route deleted any directory on the machine whose path was
            # spelled in the URL (footage, grade/out, studio/data), because a
            # matte id used to be allowed to BE a path.
            _require_deletable_matte(info)
            shutil.rmtree(info.path, ignore_errors=True)
            MT.forget_cache()
            self._json({"deleted": matte_id})
            return

        raise StudioError(f"no route for {method} {path}")

    def _grade_key(self, value: str) -> str:
        """Turn whatever the client sent into a clip key.

        A clip NAME is the normal case and is resolved to a path and hashed.
        A bare 32 hex key is also accepted, because the copy picker is built
        from GET /api/grades, whose rows are keys: a clip that has a saved
        grade but is not currently in the clip list (a drive that is not
        mounted, a file moved somewhere the browser has not visited) has no
        name this server can resolve, and refusing to copy from it would make
        the picker offer entries it then rejects.
        """
        value = (value or "").strip()
        if not value:
            raise StudioError("no clip given")
        if GRADES.is_key(value):
            return value.lower()
        return GRADES.clip_key(clip_path(value))

    # --- projects, contract C1 -----------------------------------------

    def _author(self, by) -> str:
        """Who this write is FROM, decided by the server, not by the body.

        With logins on the account name wins: a signed in browser cannot sign
        somebody else's name to a commit by editing a JSON body. With logins
        off, a caller that named itself with X-Studio-Agent signs `agent:NAME`
        and cannot sign anything else, INCLUDING while attached to somebody
        else's session: acting as them does not mean writing history in their
        name. Only an unnamed local caller falls back to its own label, which
        is `studio` from the tab and `--by`, CINEGRADE_AGENT or `cli` from the
        command line. That is the whole identity model, and it is deliberately
        small: this is a local colour tool, not a bank.
        """
        if AUTH.enabled() and self.user:
            return str(self.user["name"])
        if self.caller_agent:
            return str(self.caller_name)
        label = str(by or "").strip()
        return label or "cli"

    def _open_key(self, clip=None) -> str:
        """The project this request is about: the named clip, or the open one."""
        clip = str(clip or "").strip()
        if clip:
            return PROJECTS.resolve(clip)[0]
        key = PROJECTS.workspace_key(self._uid())
        if not key:
            raise StudioError("no project is open. Open a clip first "
                              "(POST /api/project/open)")
        return key

    # --- the library permission rule, contract E1 -----------------------

    def _guard_edit(self, key=None, clip=None) -> None:
        """403 if this account only reaches the clip through a viewer grant.

        A project is shared across accounts by the clip's content, so what
        tells two people apart is not the project, it is the FILE each of them
        reaches it through. That is why the check runs on a path.

        Everything about finding the path is best effort and swallows its own
        errors: a clip that has gone offline is not a permission answer, and
        the routes below already behave sensibly when it has. The refusal
        itself comes from library.guard_edit, outside the try, so it is never
        swallowed by accident.
        """
        if not AUTH.enabled():
            return
        path = ""
        if clip:
            try:
                path = str(clip_path(str(clip)))
            except Exception:                                 # noqa: BLE001
                path = ""
        if not path:
            # No clip named, or a name this server cannot resolve: fall back
            # to the project the request will actually land on, so a bad name
            # cannot be used to walk past the check.
            try:
                k = key or PROJECTS.workspace_key(self._uid())
                if k:
                    path = (PROJECTS.state(k) or {}).get("path") or ""
            except Exception:                                 # noqa: BLE001
                path = ""
        LIB.guard_edit(self._uid(), path)

    def _guard_read(self, name) -> None:
        """403 if this account cannot READ the clip it just named.

        Clips are keyed by bare file name in one table shared by every
        caller, so without this a signed in account could name a file it has
        never been shown and get its frames, its scopes, its stats, its
        thumbnail, a playable segment of it or a full render of it back. This
        is the read half of the same rule _guard_edit enforces on writes, and
        it runs on the same thing: the path the name resolves to.

        Passes for your own library, for a viewer or an editor grant, for the
        shared `content/footage` common area, for a file opened from anywhere
        else on this Mac, and for every caller when logins are off.

        Resolution is best effort in one direction only: a name this server
        cannot turn into a path is left to the route, which answers "no such
        clip" the way it always did. It can never turn into an allow for a
        name that DOES resolve, because the refusal comes from
        library.guard_read outside the try.
        """
        if not AUTH.enabled():
            return
        path = ""
        try:
            path = str(clip_path(str(name or "")))
        except Exception:                                     # noqa: BLE001
            path = ""
        LIB.guard_read(self._uid(), path)

    def _may_read(self, name) -> bool:
        """_guard_read asked as a question, for a LIST route.

        The refusal shape is right for a route that was handed one clip and
        wrong for one that answers with many: `GET /api/matte` with no clip
        has to drop what this account may not see, not 403 the whole list
        because somebody else's matte is in the store. Same rule, same
        function, so the two cannot drift apart; True for everyone when
        logins are off, since _guard_read returns immediately then.
        """
        try:
            self._guard_read(name)
            return True
        except AUTH.AuthError:
            return False

    def _guard_read_key(self, key: str) -> None:
        """The same rule for a route that carries only a cache key.

        play/stream and proxy/<key>.mp4 are plain GETs so a <video src> can
        point straight at them, which means the clip is not in the request.
        _clip_for_key answers it from the prepare call that minted the key.
        A key this run has never prepared is refused rather than served: the
        segment and proxy caches are one folder for the whole machine, so an
        unknown key may name a file another account built, and there would be
        nothing left to check it against. Every client prepares before it
        fetches, so pressing play again is the whole recovery.
        """
        if not AUTH.enabled():
            return
        clip = _clip_for_key(key or "")
        if clip is None:
            raise StudioError("this server has not prepared that key in this "
                              "run, so it cannot tell whose clip it is; ask "
                              "for it again with prepare")
        self._guard_read(clip)

    def _edit_key(self, clip=None) -> str:
        """_open_key for a route that CHANGES the project, so it also guards."""
        key = self._open_key(clip)
        self._guard_edit(key=key)
        return key

    def _library_clip(self, root, path) -> str:
        """A library item to a clip name every other route already understands.

        The read permission is checked by library.open_target; after that the
        file is registered exactly the way POST /api/open registers a file
        picked in the anywhere browser, so frame, stats, scope, thumb and
        render need no idea that shares exist.
        """
        target, _owner, _rel, _role = LIB.open_target(self._uid(), root, path)
        return register_external_clip(str(target))

    def _project_body(self, clip=None, proj: dict | None = None) -> dict:
        """What GET /api/project answers with, for a clip or the open project."""
        if proj is None:
            clip = str(clip or "").strip()
            if clip:
                key = PROJECTS.resolve(clip)[0]
                proj = PROJECTS.state(key)
            else:
                key = PROJECTS.workspace_key(self._uid())
                proj = PROJECTS.state(key) if key else None
        if not proj:
            return {"open": False, "key": None, "name": None, "config": None,
                    "rotation": "auto", "branch": None, "head": None,
                    "branches": [], "extras": {},
                    "note": "no project is open on this account yet"}
        out = {"open": True, "key": proj["key"], "name": proj["name"],
               "path": proj["path"], "rotation": proj["rotation"],
               "time": proj["time"], "preset": proj["preset"],
               "head": proj["head_short"], "head_full": proj["head"],
               "head_commit": proj["head_commit"], "branch": proj["branch"],
               "branches": proj["branches"], "extras": proj["extras"],
               "config": proj["config"], "created": proj["created"],
               "updated": proj["updated"]}
        out["dims"] = self._rotation_dims(proj["name"], proj["rotation"])
        return out

    def _rotation_dims(self, name: str, rotation: str) -> dict | None:
        """The frame size this project's rotation actually produces.

        Best effort: a clip that has gone offline, or a probe that fails, costs
        this one field and not the whole request. 90 and 270 are the raw frame
        turned on its side, which is why the raw probe is the base for them
        rather than the autorotated one.
        """
        if not name:
            return None
        try:
            info = clip_info(name, rotation == "auto")
        except Exception:                                     # noqa: BLE001
            return None
        width, height = int(info["width"]), int(info["height"])
        if rotation in ("90", "270"):
            width, height = height, width
        return {"width": width, "height": height, "rotation": rotation}

    def _clip_entry(self, p: Path, name: str) -> dict:
        entry = {"name": name, "bytes": p.stat().st_size, "path": str(p),
                 "external": name in EXTERNAL_CLIPS,
                 # Content key (contract C3): the identity this clip's saved
                 # grade is filed under, stable across a rename or a move.
                 "key": GRADES.safe_key(p)}
        for rot in (True, False):
            try:
                info = clip_info(name, rot)
            except Exception as exc:                          # noqa: BLE001
                entry["error"] = str(exc)
                break
            entry["autorotate" if rot else "raw"] = {
                "width": info["width"], "height": info["height"],
            }
            entry.setdefault("rotation", info["rotation"])
            entry.setdefault("duration", info["duration"])
            entry.setdefault("fps", info["fps"])
            entry.setdefault("codec", info["codec"])
            entry.setdefault("profile", info["profile"])
            entry.setdefault("pix_fmt", info["pix_fmt"])
            entry.setdefault("color_range", info["color_range"])
            entry.setdefault("color_space", info["color_space"])
            entry.setdefault("color_transfer", info.get("color_transfer", ""))
            entry.setdefault("color_primaries", info.get("color_primaries", ""))
        # source (contract G1): what this file IS, as three separate tags plus
        # the answer the engine reads out of them. Separate because they are
        # separate: color_space is the YUV matrix, color_transfer is the curve
        # and color_primaries is the gamut, and a file can get any one of them
        # right while the others are missing or wrong. resolved_input is what
        # convert.input "auto" lands on for this file, so the UI and an agent
        # can see the decode BEFORE rendering anything, and `warnings` carries
        # the sentence the engine would print about an unrecognised tag.
        if "error" not in entry:
            src, warns = CG.resolve_input(CG.DEFAULTS, info)
            try:
                # The same door every render goes through, asked with the
                # default working space, so a clip that would be refused says
                # so in the listing instead of only when someone renders it.
                _src, warns = CG.check_source_space(CG.DEFAULTS, info)
            except CG.GradeError as exc:
                warns = warns + [str(exc)]
            entry["source"] = {
                "transfer": entry.get("color_transfer", ""),
                "primaries": entry.get("color_primaries", ""),
                "matrix": entry.get("color_space", ""),
                "range": entry.get("color_range", ""),
                "resolved_input": src,
                "warnings": warns,
            }
        # rotation_tag (contract G4): the display matrix tag as a string,
        # "0" when the file has none, and rotation_tag_suspect, the advisory
        # boolean from CG.rotation_tag_suspect(): the exact function
        # `cinegrade orient IN --json` prints through, so the clip list and
        # the CLI never disagree about which files are worth a second look.
        # Nothing here ever changes what gets rendered; "raw" (the mode "0"
        # block just above, the file's own coded width and height before any
        # tag is applied) is what the codec tell in that heuristic needs.
        # rotation_tag_note (round 2 tooling item 17): the same tell, in one
        # plain sentence, "" when not suspect, so the boolean alone never
        # reads as a verdict either way. CG.rotation_tag_note() delegates
        # its yes/no to CG.rotation_tag_suspect() itself, so the two fields
        # cannot disagree.
        if "error" in entry:
            entry.setdefault("rotation_tag", "0")
            entry.setdefault("rotation_tag_suspect", False)
            entry.setdefault("rotation_tag_note", "")
        else:
            entry["rotation_tag"] = str(int(entry.get("rotation") or 0))
            raw_dims = entry.get("raw") or {}
            entry["rotation_tag_suspect"] = CG.rotation_tag_suspect(
                entry.get("rotation"), entry.get("codec"),
                raw_dims.get("width"), raw_dims.get("height"))
            entry["rotation_tag_note"] = CG.rotation_tag_note(
                entry.get("rotation"), entry.get("codec"),
                raw_dims.get("width"), raw_dims.get("height"))
        # The two dimension blocks above are the two the app has always shown
        # (autorotate and raw). Once a project exists, the rotation it stores
        # can be a quarter turn that is neither of them, so its size is
        # reported as a third block rather than by quietly redefining one of
        # the first two, which the clip picker and proxy-fidelity.mjs read.
        # Absent entirely when there is no project, so today's shape is
        # unchanged for anyone who has not opened one.
        chosen = _project_rotation(self._uid())
        if chosen and "error" not in entry:
            try:
                info = clip_info(name, chosen)
                entry["effective"] = {
                    "rotation": CG.normalise_rotation(chosen),
                    "width": info["width"], "height": info["height"],
                }
            except Exception:                                 # noqa: BLE001
                pass
        return entry

    def _clips(self) -> list[dict]:
        clips = []
        if FOOTAGE.exists():
            for p in sorted(FOOTAGE.iterdir()):
                if p.suffix.lower() not in VIDEO_EXT or not p.is_file():
                    continue
                clips.append(self._clip_entry(p, p.name))
        # Files opened from elsewhere on the machine sort after the project's
        # own footage, so the familiar list does not reshuffle as clips are
        # opened during a session.
        for name, p in sorted(EXTERNAL_CLIPS.items()):
            if p.exists():
                clips.append(self._clip_entry(p, name))
        return clips


VERBOSE = False
# The request log is ON by default (contract G2): with several agents and a
# person sharing one server, a silent server makes "who moved my picture" an
# unanswerable question. --quiet turns it off for anybody who wants the old
# silence back. VERBOSE is unrelated and keeps meaning what it meant.
QUIET = False
# Process start, for GET /api/health's uptime. Read at import so it is the
# real start rather than the moment the port was bound.
START_TIME = time.time()


class StudioServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """Swallow the hang-ups, keep the real errors.

        Scrubbing abandons requests by design, and a browser closing a
        keep-alive socket is not a fault. Left alone, socketserver prints a
        full traceback for each one and buries anything that actually matters.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError,
                            ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def _user_cli(args) -> bool:
    """Handle --list-users / --delete-user / --create-user and stop.

    These run before the ffmpeg check and before anything binds a port, so
    an account can be created on a machine where the render tools are not
    installed yet, and creating one never collides with a studio already
    listening.

    A password is never taken from argv: argv is visible in `ps` to every
    other process on the box. getpass reads it from the terminal without
    echoing; --password-stdin reads one line from stdin for scripts and for
    the tests, which cannot type into a terminal.
    """
    if args.list_users:
        users = AUTH.list_users()
        if not users:
            print("no accounts yet. Create one with:\n"
                  "  studio/server.py --create-user NAME --role admin")
        for u in users:
            print("%4d  %-5s  %-20s org %s" % (u["id"], u["role"], u["name"],
                                               u.get("org") or "default"))
        return True

    if args.delete_user:
        if AUTH.delete_user(args.delete_user):
            print(f"deleted {args.delete_user}")
            return True
        sys.exit(f"no account named {args.delete_user}")

    if args.create_user:
        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\n")
        else:
            password = getpass.getpass("password: ")
            if password != getpass.getpass("again: "):
                sys.exit("the two passwords did not match, so no account was "
                         "created")
        org = _org_or_exit(getattr(args, "org", None) or "default")
        try:
            user = AUTH.create_user(args.create_user, password, args.role,
                                    org["id"])
        except AUTH.AuthError as exc:
            sys.exit(str(exc))
        print(f"created {user['name']} (id {user['id']}, role {user['role']}, "
              f"org {org['name']})")
        return True

    return False


def _org_or_exit(name) -> dict:
    """An org by name (or id), or a message saying how to make one.

    Every account belongs to exactly one org, so a typo here would otherwise
    silently create the account in the default tenant, which is the one
    mistake in this whole area that is hard to notice and annoying to undo.
    """
    LIB.init_schema()
    org = LIB.org_by_name(name or "default")
    if org is None:
        sys.exit(f"no org called {name}. Make one first:\n"
                 f"  studio/server.py --create-org {name}")
    return org


def _org_cli(args) -> bool:
    """Handle the org and team admin flags and stop, contract E1.

    Same rules as the account flags above: they run before anything binds a
    port, they print what they did, and none of them takes a secret on the
    command line (there is no secret here to take).
    """
    if args.create_org:
        LIB.init_schema()
        try:
            org = LIB.create_org(args.create_org)
        except ValueError as exc:
            sys.exit(str(exc))
        print(f"created org {org['name']} (id {org['id']})")
        return True

    if args.list_orgs:
        LIB.init_schema()
        for o in LIB.list_orgs():
            print("%4d  %-24s %d account(s)" % (o["id"], o["name"], o["users"]))
        return True

    if args.create_team:
        org = _org_or_exit(getattr(args, "org", None) or "default")
        try:
            team = LIB.create_team(args.create_team, org["id"])
        except ValueError as exc:
            sys.exit(str(exc))
        print(f"created team {team['name']} (id {team['id']}) in org "
              f"{org['name']}")
        return True

    if args.list_teams:
        LIB.init_schema()
        teams = LIB.list_teams()
        if not teams:
            print("no teams yet. Make one with:\n"
                  "  studio/server.py --create-team NAME --org ORG")
        for t in teams:
            print("%4d  %-24s org %-16s %s" % (
                t["id"], t["name"], t["org"],
                ", ".join(t["members"]) or "no members yet"))
        return True

    for flag, add in ((args.add_to_team, True), (args.remove_from_team, False)):
        if not flag:
            continue
        LIB.init_schema()
        who, team_name = flag[0], flag[1]
        user = next((u for u in AUTH.list_users()
                     if u["name"].lower() == who.strip().lower()), None)
        if user is None:
            sys.exit(f"no account named {who}")
        team = LIB.team_by_name(team_name, LIB.org_of(user["id"]))
        if team is None:
            sys.exit(f"no team called {team_name} in org "
                     f"{user.get('org') or 'default'}")
        if add:
            try:
                LIB.add_to_team(user["id"], team["id"])
            except ValueError as exc:
                sys.exit(str(exc))
            print(f"{user['name']} is now in {team['name']}")
        else:
            gone = LIB.remove_from_team(user["id"], team["id"])
            print(f"{user['name']} removed from {team['name']}" if gone
                  else f"{user['name']} was not in {team['name']}")
        return True

    return False


def main() -> None:
    global VERBOSE, QUIET, UPLOAD_MAX_BYTES, UPLOAD_QUOTA_BYTES
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=7431)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--auth", action="store_true",
                    help="require a login. Also turned on by STUDIO_AUTH=1, "
                         "and forced on by any non loopback --host")
    ap.add_argument("--behind-https-proxy", action="store_true",
                    help="a TLS terminating proxy is in front, so mark the "
                         "session cookie Secure")
    ap.add_argument("--create-user", metavar="NAME",
                    help="create an account and exit (password is prompted "
                         "for, never passed on the command line)")
    ap.add_argument("--role", choices=AUTH.ROLES, default="user",
                    help="role for --create-user (default user)")
    ap.add_argument("--password-stdin", action="store_true",
                    help="read the password for --create-user from stdin "
                         "instead of prompting")
    ap.add_argument("--list-users", action="store_true",
                    help="list accounts and exit")
    ap.add_argument("--delete-user", metavar="NAME",
                    help="delete an account and exit")
    # Orgs and teams, contract E1. An org is the tenant: accounts in one org
    # never see accounts in another. A team is a group inside an org that a
    # folder can be shared with in one go.
    ap.add_argument("--org", metavar="ORG",
                    help="which org --create-user or --create-team belongs "
                         "to, by name or id (default: default)")
    ap.add_argument("--create-org", metavar="NAME",
                    help="create an org and exit")
    ap.add_argument("--list-orgs", action="store_true",
                    help="list orgs and exit")
    ap.add_argument("--create-team", metavar="NAME",
                    help="create a team in --org and exit")
    ap.add_argument("--list-teams", action="store_true",
                    help="list teams, their org and their members, and exit")
    ap.add_argument("--add-to-team", nargs=2, metavar=("USER", "TEAM"),
                    help="put an account in a team in its own org and exit")
    ap.add_argument("--remove-from-team", nargs=2, metavar=("USER", "TEAM"),
                    help="take an account out of a team and exit")
    ap.add_argument("--data-dir", metavar="DIR",
                    help="put the accounts, grades and project database "
                         "somewhere other than studio/data. For tests: this "
                         "database is real work, and a test run must never "
                         "open it. Also settable as STUDIO_DATA_DIR")
    ap.add_argument("--footage", metavar="DIR",
                    help="use DIR as the shared footage folder instead of "
                         "content/footage. For tests: with logins off the "
                         "library's own root IS that folder, so without this "
                         "a test upload lands in real footage. Also settable "
                         "as STUDIO_FOOTAGE")
    ap.add_argument("--cache-dir", metavar="DIR",
                    help="put the frame, proxy and segment cache somewhere "
                         "other than studio/cache. Defaults to "
                         "<data-dir>/cache when --data-dir is given, so a "
                         "temp server stops evicting the frames a real one "
                         "is scrubbing through. Also settable as "
                         "STUDIO_CACHE_DIR")
    ap.add_argument("--upload-max-bytes", type=int, default=UPLOAD_MAX_BYTES,
                    help="reject a single POST /api/upload larger than this "
                         "many bytes (default 8 GiB)")
    ap.add_argument("--upload-quota-bytes", type=int, default=UPLOAD_QUOTA_BYTES,
                    help="reject an upload that would put one account's "
                         "footage folder over this many bytes total "
                         "(default 50 GiB)")
    ap.add_argument("--quiet", action="store_true",
                    help="turn the one line per request log off. The log is "
                         "on by default so a shared server can say who did "
                         "what; --verbose is a separate, noisier thing")
    ap.add_argument("--sam-url", metavar="URL",
                    help="base URL of the SAM masking service (contract C3). "
                         "Also settable as STUDIO_SAM_URL. Default "
                         f"{SAMC.DEFAULT_SAM_URL}")
    ap.add_argument("--mask-width", type=int, default=None,
                    help="working width for the plain Rec.709 proxy the mask "
                         f"routes hand to the SAM service (default "
                         f"{MASK_WORKING_WIDTH}, pending the spike's own "
                         "number). Also settable as STUDIO_MASK_WIDTH")
    args = ap.parse_args()
    VERBOSE = args.verbose
    QUIET = args.quiet
    UPLOAD_MAX_BYTES = args.upload_max_bytes
    UPLOAD_QUOTA_BYTES = args.upload_quota_bytes
    if args.sam_url:
        os.environ["STUDIO_SAM_URL"] = args.sam_url
        SAMC.reset_default()
    if args.mask_width:
        set_mask_width(args.mask_width)

    # Before anything opens the database, including --create-user below.
    if args.data_dir:
        DB.set_data_dir(args.data_dir)
        AUTH.USERS_DIR = DB.DATA / "users"
        GRADES.USERS_DIR = DB.DATA / "users"

    # And the cache, resolved once here so nothing downstream has to ask.
    # --cache-dir wins over everything; otherwise a run with its own data
    # folder gets a cache inside it, and a run without one keeps studio/cache
    # exactly as before. The import time default already covered the two
    # environment variables, so this only has to handle the flags.
    if args.cache_dir:
        set_cache_dir(args.cache_dir)
    elif args.data_dir and not os.environ.get("STUDIO_CACHE_DIR", "").strip():
        set_cache_dir(DB.DATA / "cache")

    # Same idea for the footage folder, and for the same reason: a test run
    # must not write into real work. Before ensure_dirs and before the first
    # request, so nothing has read the old value yet.
    footage_dir = args.footage or os.environ.get("STUDIO_FOOTAGE", "").strip()
    if footage_dir:
        set_footage(footage_dir)

    if _org_cli(args) or _user_cli(args):
        return

    # Three ways in, one of which is not a choice: binding anywhere reachable
    # from another machine turns logins on whether or not they were asked for.
    loopback = AUTH.is_loopback(args.host)
    auth_on = (args.auth
               or os.environ.get("STUDIO_AUTH", "").strip().lower()
               in ("1", "true", "yes", "on")
               or not loopback)
    AUTH.set_enabled(auth_on)
    AUTH.set_behind_https_proxy(args.behind_https_proxy)

    if not loopback:
        # The hard rule. This server is a file browser, a Finder opener and a
        # subprocess launcher; putting it on a LAN with no accounts is handing
        # all three to every device on that network. Refuse, and say what to
        # do about it, rather than starting and hoping nobody looks.
        # auth_on cannot be False here: a non loopback --host is itself one of
        # the three ways logins get turned on, three lines up. So the only way
        # a network bind can fail this gate is an empty account table.
        if AUTH.user_count() == 0:
            sys.exit(f"refusing to listen on {args.host}: logins are on but no "
                     "account exists, so the first person to reach this port "
                     "would meet a login page that nobody can get past. "
                     "Create one first:\n"
                     "  studio/server.py --create-user NAME --role admin")

    ensure_dirs()
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"{tool} is not on PATH; the studio cannot render without it")
    if not TECHNICAL.exists() or not any(TECHNICAL.glob("*.cube")):
        print("warning: no technical LUTs yet. Use Rebuild LUTs in the UI, or "
              "run grade/tools/make_cst.py.", file=sys.stderr)

    # Contract C1: the open project survives a restart. Done after ensure_dirs
    # (the tables have to exist) and before the port is bound, so the first
    # request already sees the restored state rather than an empty mirror.
    live_rebuild()

    httpd = StudioServer((args.host, args.port), Handler)
    # flush explicitly: piped into a log file Python block-buffers stdout, and a
    # launcher that prints the URL only after you kill it is no use to anybody.
    print(f"Fixxr Studio on http://{args.host}:{args.port}", flush=True)
    if AUTH.enabled():
        print(f"  logins  on, {AUTH.user_count()} account(s). This server "
              "speaks plain HTTP: put a TLS reverse proxy in front before "
              "using it over a network you do not control.", flush=True)
    else:
        print("  logins  off (local use). Pass --auth to require one.",
              flush=True)
    print(f"  footage {FOOTAGE}\n  presets {PRESETS}\n  looks   {LOOKS}\n"
          f"  out     {OUT}\n  cache   {CACHE}\n  data    {DB.DATA}", flush=True)
    print(f"  sam     {SAMC.client().base} (mask width {MASK_WORKING_WIDTH}px)",
          flush=True)
    print("  log     " + ("off (--quiet)" if QUIET else
                          "one line per request on stderr"), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
