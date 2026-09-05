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
CACHE = STUDIO / "cache"
STATIC = STUDIO / "static"

sys.path.insert(0, str(GRADE))
import cinegrade as CG  # noqa: E402  (path has to be set first)

# studio/ itself, so `import auth` and `import db` resolve no matter how this
# file was invoked. Running it as a script already puts its own folder on
# sys.path, but at whatever index it landed at before GRADE was pushed in
# front, and importing server.py from elsewhere would not put it there at all.
sys.path.insert(0, str(STUDIO))
import auth as AUTH  # noqa: E402  (same reason)
import db as DB      # noqa: E402  (same reason)
import grades as GRADES  # noqa: E402  (same reason)
import render_gpu as RG  # noqa: E402  (same reason)

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


def register_external_clip(raw: str) -> str:
    """Take an absolute path to a video file and give it a stable display name.

    The name has to survive safe_name(), which rejects separators, so a file
    outside footage/ cannot be addressed by its path. Two different folders can
    hold the same basename, so a colliding name gets a short digest of its
    parent appended rather than silently shadowing the earlier file.
    """
    p = Path(raw).expanduser()
    try:
        p = p.resolve(strict=True)
    except OSError as exc:
        raise StudioError(f"cannot open {raw}: {exc}") from exc
    if not p.is_file():
        raise StudioError(f"not a file: {p}")
    if p.suffix.lower() not in VIDEO_EXT:
        raise StudioError(f"not a video this tool reads: {p.name}")
    try:
        rel = p.relative_to(FOOTAGE.resolve())
    except ValueError:
        pass
    else:
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
    """Sum of file sizes directly inside a folder, for the upload quota check.

    Not recursive: a footage folder here is flat, and this server never
    writes a subfolder into one on its own.
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


def ensure_dirs() -> None:
    # DB.DATA and AUTH.USERS_DIR are studio/data and studio/data/users: the
    # accounts database and each account's own footage folder live there, and
    # the whole folder is gitignored because it is user owned data.
    for d in (CACHE / "frames", CACHE / "img", CACHE / "thumbs", CACHE / "refs",
              CACHE / "src", CACHE / "segments", CACHE / "proxy", STUDIO_TOOLS,
              OUT, PRESETS, LOOKS,
              DB.DATA, AUTH.USERS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    # Per clip grades keep their own tables in the same SQLite file the
    # accounts use. Created here rather than lazily on the first request, so a
    # data folder that cannot be written fails at boot with a clear traceback
    # instead of inside a request somebody is waiting on.
    GRADES.init_schema()
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
# probe
# --------------------------------------------------------------------------

def clip_info(name: str, autorotate: bool) -> dict:
    key = (name, autorotate)
    with _probe_lock:
        if key in _probe_cache:
            return dict(_probe_cache[key])
    path = clip_path(name)
    info = CG.probe(str(path), autorotate=autorotate)
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
    """Fill in everything the caller left out, so the graph builders are safe."""
    return CG.deep_merge(CG.DEFAULTS, cfg or {})


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
    for key in ("tonemap", "working_space", "encode"):
        flat["convert"][key] = cfg["convert"][key]
    if keep_exposure:
        flat["convert"]["exposure"] = cfg["convert"]["exposure"]
    return flat


def mask_preview_config(cfg: dict) -> dict:
    """Show the qualifier matte with nothing painted on top of it.

    The power window is deliberately LEFT ON. What the secondary actually
    selects is the qualifier matte multiplied by the window matte, so a matte
    view that ignored the shape would show a selection the grade will never
    make. The engine handles the multiply: in show_mask mode the window's
    maskedmerge composites the greyscale qualifier over black instead of over
    the picture, so the two mattes come out multiplied together.
    """
    out = deepcopy(cfg)
    out["secondary"]["show_mask"] = True
    out["look"]["lut"] = None
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
                 autorotate: bool) -> tuple[np.ndarray, dict]:
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
    info = clip_info(clip, autorotate)
    width, height = _preview_dims(info, width)
    # The decode matrix is part of the identity of the cached bytes, not a
    # constant: an ordinary bt709 delivery file and an Apple Log clip normalise
    # differently, so leaving it out of the key would serve one clip's frame
    # decoded by the other's rule.
    matrix = CG.source_matrix(info)
    mem_key = f"{clip}|{round(float(time_s), 4)}|{width}|{autorotate}|{matrix}"

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
        vf = (f"scale={width}:{height}:flags=bilinear,setsar=1,"
              f"scale=in_color_matrix={matrix}:in_range={info['color_range']}"
              f":out_range=full,format=gbrp16le")
        args = ["ffmpeg", "-v", "error", "-y"]
        if not autorotate:
            args += ["-noautorotate"]
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


def _grade_inputs(width: int, height: int, cfg: dict, info: dict) -> list[str]:
    """The non-source ffmpeg inputs for the grade-only pass.

    Mirrors cinegrade.ffmpeg_inputs' own tail (radial mask, window matte, then
    grain) so the extra inputs land at the same index the filter graph expects:
    input 0 is the normalised source piped in over stdin here instead of ffmpeg
    decoding the clip itself, but everything after it has to stay in the same
    order or the graph reads the wrong input and produces a wrong picture
    silently. CG.mask_input_indices is the one place that order is decided.
    """
    args = ["-f", "rawvideo", "-pix_fmt", "rgb48le",
            "-s", f"{width}x{height}", "-i", "-"]
    if cfg["fx"]["radial_blur"]["enabled"]:
        rb = cfg["fx"]["radial_blur"]
        m = CG.radial_mask(info["width"], info["height"], rb["start"], rb["end"])
        args += ["-i", str(m)]
    if CG.window_active(cfg):
        args += ["-i", str(CG.window_mask(cfg, info["width"], info["height"]))]
    if cfg["grain"]["enabled"]:
        args += CG.grain_input(cfg, info)
    return args


def render_raw(clip: str, time_s: float, width: int, cfg: dict,
               autorotate: bool) -> tuple[np.ndarray, dict]:
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
    info = clip_info(clip, autorotate)
    width, height = _preview_dims(info, width)
    factor = width / float(info["width"])
    pinfo = dict(info, width=width, height=height)
    pcfg = scale_for_preview(cfg, factor)

    key = hashlib.sha1(json.dumps({
        "clip": clip, "t": round(float(time_s), 4), "w": width,
        "rot": autorotate, "cfg": pcfg, "engine": ENGINE_HASH,
    }, sort_keys=True).encode()).hexdigest()

    raw_path = _cache_path("frames", key, "rgb")
    meta = {"key": key, "width": width, "height": height,
            "source_width": info["width"], "source_height": info["height"]}
    if raw_path.exists():
        try:
            os.utime(raw_path, None)
            return np.frombuffer(raw_path.read_bytes(), np.uint8).reshape(
                height, width, 3), meta
        except FileNotFoundError:
            pass  # pruned between exists() and read; fall through and re-render

    src, _smeta = source_frame(clip, time_s, width, autorotate)

    graph = CG.graph_with_mask(pcfg, pinfo, encode_out=False,
                               tail_extra=["format=rgb24"],
                               src_label="0:v", src_normalised=True)
    args = ["ffmpeg", "-v", "error", "-y"]
    args += _grade_inputs(width, height, pcfg, pinfo)
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


def render_raw_legacy(clip: str, time_s: float, width: int, cfg: dict,
                      autorotate: bool) -> tuple[np.ndarray, dict]:
    """The pre-split single-pass render: decode, downscale and grade in one
    ffmpeg call. Kept only so the parity check script can render the same
    config both ways and diff the pixels; no route calls this any more.
    """
    cfg = full_config(cfg)
    info = clip_info(clip, autorotate)
    width, height = _preview_dims(info, width)
    factor = width / float(info["width"])
    pinfo = dict(info, width=width, height=height)
    pcfg = scale_for_preview(cfg, factor)

    meta = {"key": "legacy", "width": width, "height": height,
            "source_width": info["width"], "source_height": info["height"]}

    head = (f"[0:v]scale={width}:{height}:flags=bilinear,"
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
# never invalidates this cache, only clip/start/duration/width/autorotate do.
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


def range_budget(clip: str, width: int, autorotate: bool) -> dict:
    """What a caller can ask for at this width, computed from the real clip
    (fps and dimensions), before anyone commits to a decode. Used by the
    client to clamp the loop range in the UI, and mirrored by the hard check
    inside source_range() below so a request built by hand (or a client that
    skipped the preflight) still gets the same answer, never a silent
    truncation or a hang.
    """
    info = clip_info(clip, autorotate)
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
                 autorotate: bool) -> tuple[np.ndarray, dict]:
    """The decoded, normalised source frames for [start, start+duration),
    one ffmpeg process, at the preview width. Mirrors source_frame()'s own
    vf chain exactly (scale, then the colour-matrix normalise into full
    range gbrp16le) so a loop frame and a cached still of the same timecode
    are byte for byte the same picture.

    Returns uint16 data shaped (N, H, W, 3), R G B per pixel, plus meta with
    the actual frame count and fps so the client can compute a real playback
    rate rather than assuming one.
    """
    info = clip_info(clip, autorotate)
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
    vf = (f"scale={pwidth}:{pheight}:flags=bilinear,setsar=1,"
          f"scale=in_color_matrix={matrix}:in_range={info['color_range']}"
          f":out_range=full,format=gbrp16le")
    args = ["ffmpeg", "-v", "error", "-y"]
    if not autorotate:
        args += ["-noautorotate"]
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


def _play_params(payload: dict) -> dict:
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
    autorotate = bool(payload.get("autorotate", True))
    info = clip_info(clip, autorotate)
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
        "w": pwidth, "rot": autorotate, "cfg": pcfg, "engine": ENGINE_HASH,
    }, sort_keys=True).encode()).hexdigest()

    return {"key": key, "clip": clip, "start": start, "duration": duration,
            "autorotate": autorotate, "cfg": cfg, "pcfg": pcfg,
            "info": info, "pinfo": pinfo, "fps": fps}


def _play_extra_inputs(cfg: dict, info: dict) -> list[str]:
    """The mask/grain -i args for a preview-scaled segment.

    Mirrors _grade_inputs' own tail above: same order (radial mask, window
    matte, grain), same reason (build_graph fixes those input indices, so the
    order here has to match what the filter graph expects). Kept as its own
    small copy rather than shared with _grade_inputs, because that function's
    first input is a rawvideo pipe from an already-decoded source frame and
    this one decodes the clip itself as input 0; the two pipelines only share
    what comes after input 0.
    """
    args = []
    if cfg["fx"]["radial_blur"]["enabled"]:
        rb = cfg["fx"]["radial_blur"]
        m = CG.radial_mask(info["width"], info["height"], rb["start"], rb["end"])
        args += ["-i", str(m)]
    if CG.window_active(cfg):
        args += ["-i", str(CG.window_mask(cfg, info["width"], info["height"]))]
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
    head = f"[0:v]scale={width}:{height}:flags=bicubic,setsar=1[studiosrc]"
    graph = CG.graph_with_mask(pcfg, pinfo, src_label="studiosrc", head_extra=head)

    args = ["ffmpeg", "-v", "error", "-y"]
    if not params["autorotate"]:
        args += ["-noautorotate"]
    args += ["-ss", str(params["start"]), "-i", str(clip_path(params["clip"]))]
    args += _play_extra_inputs(pcfg, pinfo)
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


# Hue families, in degrees. The boundaries are stated here and shown in the UI
# so the warm/cool/green split is a definition the user can check, not a number
# that appeared from somewhere.
HUE_FAMILIES = [
    ("warm", 345.0, 75.0),
    ("green", 75.0, 165.0),
    ("cool", 165.0, 285.0),
    ("magenta", 285.0, 345.0),
]
SAT_FLOOR = 0.10          # below this a pixel counts as neutral, not coloured
CLIP_BLACK = 2            # 8-bit code at or under which a pixel reads as crushed
CLIP_WHITE = 253


def frame_stats(rgb: np.ndarray) -> dict:
    a = rgb.astype(np.float32) / 255.0
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b

    mx = a.max(-1)
    mn = a.min(-1)
    delta = mx - mn
    sat = np.where(mx > 1e-6, delta / np.maximum(mx, 1e-6), 0.0)

    hue = np.zeros_like(mx)
    nz = delta > 1e-9
    i = nz & (mx == r)
    hue[i] = ((g - b)[i] / delta[i]) % 6.0
    i = nz & (mx == g) & (mx != r)
    hue[i] = ((b - r)[i] / delta[i]) + 2.0
    i = nz & (mx == b) & (mx != r) & (mx != g)
    hue[i] = ((r - g)[i] / delta[i]) + 4.0
    hue = (hue * 60.0) % 360.0

    total = float(y.size)
    coloured = sat >= SAT_FLOOR
    families = {}
    for name, lo, hi in HUE_FAMILIES:
        if lo < hi:
            sel = (hue >= lo) & (hue < hi)
        else:                                   # the warm family wraps past 360
            sel = (hue >= lo) | (hue < hi)
        families[name] = round(float((sel & coloured).sum()) / total * 100.0, 2)
    families["neutral"] = round(float((~coloured).sum()) / total * 100.0, 2)

    p5, p25, p50, p75, p95 = (float(v) for v in np.percentile(y, [5, 25, 50, 75, 95]))
    raw = rgb
    clipped_black = float((raw.max(-1) <= CLIP_BLACK).sum()) / total * 100.0
    clipped_white = float((raw.min(-1) >= CLIP_WHITE).sum()) / total * 100.0

    return {
        "luma": {
            "p5": round(p5, 4), "p25": round(p25, 4), "p50": round(p50, 4),
            "p75": round(p75, 4), "p95": round(p95, 4),
            "mean": round(float(y.mean()), 4),
            "mean8": round(float(y.mean()) * 255.0, 1),
            "min": round(float(y.min()), 4), "max": round(float(y.max()), 4),
        },
        "saturation": {
            "mean": round(float(sat.mean()), 4),
            "mean_coloured": round(float(sat[coloured].mean()) if coloured.any() else 0.0, 4),
            "p95": round(float(np.percentile(sat, 95)), 4),
        },
        "channels": {
            "r": round(float(r.mean()), 4),
            "g": round(float(g.mean()), 4),
            "b": round(float(b.mean()), 4),
        },
        "families": families,
        "clipped": {
            "black": round(clipped_black, 3),
            "white": round(clipped_white, 3),
        },
        "definitions": {
            "sat_floor": SAT_FLOOR,
            "clip_black_code": CLIP_BLACK,
            "clip_white_code": CLIP_WHITE,
            "families": {n: [lo, hi] for n, lo, hi in HUE_FAMILIES},
        },
    }


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
    return CG.deep_merge(CG.DEFAULTS, json.loads(p.read_text()))


def write_preset(name: str, cfg: dict, comment: str = "",
                 user_id: int = 0) -> Path:
    name = safe_name(name)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise StudioError("preset names may use letters, digits, dot, dash and "
                          "underscore only")
    body = config_diff(full_config(cfg))
    # A config carried in from a loaded preset still has that preset's comment
    # attached. An explicit new comment has to win over it, otherwise "save as"
    # would silently keep describing the grade it came from.
    existing = body.pop("_comment", "")
    comment = comment or existing
    if comment:
        body = {"_comment": comment, **body}
    # Always the account's own folder, never the shipped library: same file
    # format, same indent, same trailing newline as before, just somewhere the
    # user owns. "Overwrite" on a library preset therefore writes a user copy
    # that shadows it, which is the only sane meaning of overwrite on a file
    # every other account is also reading.
    p = GRADES.user_presets_dir(user_id) / f"{name}.json"
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

    def as_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "label": self.label,
            "status": self.status, "progress": round(self.progress, 4),
            "message": self.message, "output": self.output,
            "started": self.started, "finished": self.finished,
            "log": self.log[-12:],
        }


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


def start_render(payload: dict) -> Job:
    # Engine "gpu" is the same render through a headless Chrome running
    # gpu.js instead of ffmpeg's filter graph (studio/render_gpu.py). Every
    # other option in this payload means the same thing to both engines, and
    # ffmpeg stays the default and the reference.
    if str(payload.get("engine") or "ffmpeg").lower() == "gpu":
        return RG.start_gpu_render(payload)
    clip = payload["clip"]
    cfg = full_config(payload.get("config"))
    autorotate = bool(payload.get("autorotate", True))
    info = clip_info(clip, autorotate)
    start = float(payload.get("start") or 0.0)
    duration = payload.get("duration")
    duration = float(duration) if duration not in (None, "") else None
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
        head = f"[0:v]scale={width}:{height}:flags=bicubic,setsar=1[studiosrc]"
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


def _proxy_params(payload: dict) -> dict:
    """Resolve a proxy request into dimensions, duration and a cache key.

    No grade config anywhere in here on purpose: the proxy is the source, not
    the picture. That is what makes it one file per clip instead of one per
    grade, and what lets a knob turn during playback cost nothing.
    """
    clip = payload["clip"]
    autorotate = bool(payload.get("autorotate", True))
    info = clip_info(clip, autorotate)
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
        "clip": clip, "w": pwidth, "h": pheight, "rot": autorotate,
        "dur": round(duration, 3), "range": rng, "matrix": matrix,
        "src_range": info["color_range"], "crf": PROXY_CRF, "gop": PROXY_GOP,
        "v": PROXY_VERSION,
    }, sort_keys=True).encode()).hexdigest()

    return {"key": key, "clip": clip, "autorotate": autorotate, "range": rng,
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
    vf = (f"scale={params['width']}:{params['height']}:flags=bilinear,setsar=1,"
          f"scale=in_color_matrix={params['matrix']}"
          f":in_range={info['color_range']}:out_range=full,format=gbrp16le,"
          f"scale=out_color_matrix=bt709:out_range={out_range},format=yuv420p")

    args = ["ffmpeg", "-v", "error", "-y"]
    if not params["autorotate"]:
        args += ["-noautorotate"]
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
# live session
#
# The config the user is looking at lived only in browser memory, so nothing
# outside the tab could read it or change it. Keeping a copy here is what lets
# an outside agent run `cinegrade session patch` against the open studio and
# have the picture actually move. It is a mirror, not the source of truth: the
# browser still owns the config and republishes on every change.
# --------------------------------------------------------------------------

# One live session PER USER, keyed by user id, each with exactly the shape the
# single global LIVE dict used to have. With logins off every request resolves
# to user 0, so there is one entry and the behaviour is what it always was.
# With logins on, two people grading at once would otherwise share one mirror:
# one browser's publish would land in the other's long poll and overwrite the
# picture they were working on.
LIVE: dict[int, dict] = {}
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
        st = {"rev": 0, "config": None, "clip": None, "time": 0.0,
              "by": "server"}
        LIVE[int(user_id)] = st
    return st


def live_get(user_id: int = 0) -> dict:
    with _live_lock:
        return deepcopy(_live_state(user_id))


def live_set(payload: dict, user_id: int = 0) -> dict:
    """Merge a patch into the live config and wake anything waiting on it."""
    with _live_lock:
        state = _live_state(user_id)
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


def match_reference_job(payload: dict) -> dict:
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
    try:
        result = match_reference(
            ref=str(ref_path),
            clip=str(clip_path(payload["clip"])),
            time=float(payload.get("time", 0)),
            autorotate=bool(payload.get("autorotate", True)),
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
        )
    except MatchError as exc:
        raise StudioError(str(exc)) from exc
    finally:
        tmp.unlink(missing_ok=True)
    result["looks"] = list_looks()
    return result


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

def thumbnail(clip: str, time_s: float, width: int, autorotate: bool) -> bytes:
    key = hashlib.sha1(f"{clip}|{time_s:.3f}|{width}|{autorotate}".encode()).hexdigest()
    path = _cache_path("thumbs", key, "jpg")
    if path.exists():
        try:
            os.utime(path, None)
            return path.read_bytes()
        except FileNotFoundError:
            pass  # pruned between exists() and read; re-render instead of failing
    info = clip_info(clip, autorotate)
    height = max(2, int(round(info["height"] * width / info["width"] / 2)) * 2)
    # Thumbnails run the conversion only. They are a "where am I in the clip"
    # index, and running the full grade on every one would make the strip cost
    # more than the frame the user is actually looking at.
    cfg = flat_config(full_config({}), keep_exposure=False)
    graph = CG.graph_with_mask(cfg, dict(info, width=width, height=height),
                               encode_out=False, tail_extra=["format=rgb24"],
                               src_label="studiosrc",
                               head_extra=f"[0:v]scale={width}:{height}"
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

    def log_message(self, fmt, *args):                        # noqa: A003
        if VERBOSE:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

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
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
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
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StudioError(f"bad JSON body: {exc}")

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

        dest_dir = AUTH.user_footage(self.user["id"]) if AUTH.enabled() else FOOTAGE
        dest_dir.mkdir(parents=True, exist_ok=True)
        existing = _dir_total_bytes(dest_dir)
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
        clip_name = register_external_clip(str(final_path))
        self._json({"name": clip_name,
                    "clip": self._clip_entry(clip_path(clip_name), clip_name),
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
        try:
            if AUTH.enabled():
                self.user, self.auth_method = AUTH.user_from_request(
                    self.headers.get("Cookie", ""),
                    self.headers.get("Authorization", ""))
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
        except CG.GradeError as exc:
            self._json({"error": str(exc)}, 400)
        except BrokenPipeError:
            pass
        except Exception as exc:                              # noqa: BLE001
            traceback.print_exc()
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

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
        """The account this request belongs to.

        With logins off _dispatch leaves self.user as None and everything is
        the local user, id 0. Every per user store (grades, presets, the live
        session) keys off this one number, so "auth off changes nothing" holds
        without a single "is auth on" branch inside those stores.
        """
        return int(self.user["id"]) if self.user else 0

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

        if route == "state" and method == "GET":
            self._json({
                "defaults": CG.DEFAULTS,
                "clips": self._clips(),
                "presets": list_presets(self._uid()),
                "looks": list_looks(),
                "refs": list_refs(),
                "renders": list_renders(),
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
            name = register_external_clip(raw_open)
            self._json({"name": name, "clip": self._clip_entry(clip_path(name), name),
                        "clips": self._clips()})
            return

        if route == "upload" and method == "POST":
            self._handle_upload()
            return

        if route == "frame" and method == "POST":
            if self._stale():
                self._json({"stale": True}, 409)
                return
            payload = self._body()
            cfg = full_config(payload.get("config"))
            mode = payload.get("mode", "graded")
            if mode == "flat":
                cfg = flat_config(cfg, bool(payload.get("keep_exposure")))
            elif mode == "mask":
                cfg = mask_preview_config(cfg)
            rgb, meta = render_raw(payload["clip"], float(payload.get("time", 0)),
                                   int(payload.get("width", 960)), cfg,
                                   bool(payload.get("autorotate", True)))
            headers = {
                "X-Frame-Key": meta["key"],
                "X-Frame-Size": f"{meta['width']}x{meta['height']}",
            }
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
            arr, meta = source_frame(payload["clip"], float(payload.get("time", 0)),
                                     int(payload.get("width", 960)),
                                     bool(payload.get("autorotate", True)))
            self._send(200, arr.tobytes(), "application/octet-stream", {
                "X-Frame-Size": f"{meta['width']}x{meta['height']}",
            })
            return

        # New, self-contained: the live GPU loop's own two routes. Neither
        # touches source_frame's cache or render_raw's, and neither takes a
        # config: this range is ungraded pixels, graded per frame in the
        # browser by gpu.js (see static/live.js).
        if route == "range/limit" and method == "GET":
            self._json(range_budget(q["clip"], int(q.get("width", 960)),
                                    q.get("rot", "1") == "1"))
            return

        if route == "range" and method == "POST":
            payload = self._body()
            arr, meta = source_range(payload["clip"], float(payload.get("time", 0)),
                                     float(payload.get("duration", 2.0)),
                                     int(payload.get("width", 960)),
                                     bool(payload.get("autorotate", True)))
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
            elif kind == "secondary":
                # deep_merge against the secondary defaults so a caller can
                # send a partial block, same tolerance every other config
                # entry point into this server already gives the UI.
                scfg = CG.deep_merge(CG.DEFAULTS["secondary"], payload.get("config") or {})
                p = Path(CG.secondary_lut(scfg))
            else:
                raise StudioError(f"unknown lut kind: {kind!r}")
            if not p.exists():
                raise StudioError(f"lut not found: {p.name}")
            size, data = cached_cube(p)
            body = size.to_bytes(4, "little") + data.tobytes()
            self._send(200, body, "application/octet-stream",
                       {"X-Lut-Size": str(size)})
            return

        # The live session is per account (wave 2). The wire shape is
        # unchanged, including the `by` field session.js filters its own
        # echoes on: only the state it reads and writes is now this user's.
        if route == "session" and method == "GET":
            self._json(live_get(self._uid()))
            return

        if route == "session" and method == "POST":
            self._json(live_set(self._body(), self._uid()))
            return

        if route == "session/wait" and method == "GET":
            # Held open until something changes, so the browser sees an outside
            # edit immediately instead of on its next poll tick.
            self._json(live_wait(int(q.get("since", 0)),
                                 float(q.get("timeout", 25)), self._uid()))
            return

        # --- per clip grades, contract C3 -------------------------------
        #
        # A grade belongs to (this account, this clip's content key). The
        # client talks in clip NAMES because that is what every other route
        # here takes; the key is resolved server side so a rename cannot
        # separate a clip from its grade.
        if route == "grade" and method == "GET":
            key = self._grade_key(q.get("clip", ""))
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
            self._json({"key": key, "updated_at": updated})
            return

        if route == "grades" and method == "GET":
            self._json({"grades": GRADES.list_grades(self._uid())})
            return

        if route == "grade" and method == "DELETE":
            key = self._grade_key(q.get("clip", ""))
            self._json({"deleted": GRADES.delete_grade(self._uid(), key),
                        "key": key})
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
            self._json(match_reference_job(self._body()))
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
            cfg = full_config(payload.get("config"))
            if payload.get("mode") == "flat":
                cfg = flat_config(cfg, bool(payload.get("keep_exposure")))
            rgb, meta = render_raw(payload["clip"], float(payload.get("time", 0)),
                                   int(payload.get("width", 640)), cfg,
                                   bool(payload.get("autorotate", True)))
            self._json({"key": meta["key"], "stats": frame_stats(rgb),
                        "size": [meta["width"], meta["height"]]})
            return

        if route == "scope" and method == "POST":
            payload = self._body()
            cfg = full_config(payload.get("config"))
            rgb, meta = render_raw(payload["clip"], float(payload.get("time", 0)),
                                   int(payload.get("width", 640)), cfg,
                                   bool(payload.get("autorotate", True)))
            body = render_scope(rgb, meta["key"], payload.get("kind", "waveform"),
                                int(payload.get("size", 480)))
            self._send(200, body, "image/jpeg")
            return

        if route == "thumb" and method == "GET":
            body = thumbnail(q["clip"], float(q.get("t", 0)),
                             int(q.get("w", 160)), q.get("rot", "1") == "1")
            self._send(200, body, "image/jpeg")
            return

        if route == "ref" and method == "GET":
            self._send(200, ref_image(q["name"], int(q.get("w", 900))), "image/jpeg")
            return

        if route == "presets" and method == "GET":
            self._json({"presets": list_presets(self._uid())})
            return

        if route == "preset" and method == "GET":
            self._json({"name": q["name"],
                        "config": read_preset(q["name"], self._uid())})
            return

        if route == "preset" and method == "POST":
            payload = self._body()
            p = write_preset(payload["name"], payload.get("config"),
                             payload.get("comment", ""), self._uid())
            self._json({"saved": p.name, "path": str(p),
                        "presets": list_presets(self._uid())})
            return

        if route == "preset" and method == "DELETE":
            p, library = preset_path(q["name"], self._uid())
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
                        "presets": list_presets(self._uid())})
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
            # The GPU engine spawns a browser that has to fetch frames back
            # from this server, and only the request knows which port that is
            # (the studio takes --port, and the tests run it on a random one).
            body.setdefault("port", self.server.server_address[1])
            job = start_render(body)
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
                       GRADES.user_presets_dir(self._uid()).resolve()]
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
            params = _play_params(self._body())
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
            self._play_stream(q.get("key", ""))
            return

        # Playback proxy (live.js Mode 3). prepare is the only POST: it
        # answers with a URL when the file is already on disk and with a job
        # to watch when it is not, so the client has one call to make either
        # way. The mp4 itself is a plain GET so a <video src> can point at it.
        if route == "proxy/prepare" and method == "POST":
            self._json(proxy_state(_proxy_params(self._body())))
            return

        if route.startswith("proxy/") and method == "GET":
            self._serve_proxy(route[len("proxy/"):])
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
            print("%4d  %-5s  %s" % (u["id"], u["role"], u["name"]))
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
        try:
            user = AUTH.create_user(args.create_user, password, args.role)
        except AUTH.AuthError as exc:
            sys.exit(str(exc))
        print(f"created {user['name']} (id {user['id']}, role {user['role']})")
        return True

    return False


def main() -> None:
    global VERBOSE, UPLOAD_MAX_BYTES, UPLOAD_QUOTA_BYTES
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
    ap.add_argument("--upload-max-bytes", type=int, default=UPLOAD_MAX_BYTES,
                    help="reject a single POST /api/upload larger than this "
                         "many bytes (default 8 GiB)")
    ap.add_argument("--upload-quota-bytes", type=int, default=UPLOAD_QUOTA_BYTES,
                    help="reject an upload that would put one account's "
                         "footage folder over this many bytes total "
                         "(default 50 GiB)")
    args = ap.parse_args()
    VERBOSE = args.verbose
    UPLOAD_MAX_BYTES = args.upload_max_bytes
    UPLOAD_QUOTA_BYTES = args.upload_quota_bytes

    if _user_cli(args):
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
          f"  out     {OUT}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
