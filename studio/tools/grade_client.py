#!/usr/bin/env python3
"""grade_client - the first party HTTP client for Fixxr Studio agents.

Standard library plus numpy. Pillow is optional: `contact_sheet` uses it to
draw a text label above a panel when it is installed (it is, in
`content/.venv`, since 2026-09-06) and silently skips the label when it is
not, because an unlabeled sheet is still a usable sheet.

Nine of the first ten agents to grade footage through the studio wrote their
own copy of this file before they could get to work, and lost turns to the
same three bugs: an `HTTPError` body swallowed as a truncated `urllib`
traceback instead of the server's own JSON `{"error": "..."}` message, an
omitted rotation silently inheriting whatever the shared session last had,
and ffmpeg's `hstack` filter refusing a contact sheet whose panels were not
all the same height. This module exists once so nobody hits any of the three
again.

    from grade_client import Studio, brief, measure, decode

    studio = Studio(base="http://127.0.0.1:7431")          # or STUDIO_URL
    state = studio.state()
    result = studio.stats(clip=state["clips"][0]["name"], time=1.0, config={})
    print(brief(result["stats"]))

Every route this talks to is documented in studio/README.md under "API" and
"Agent API"; `studio/tools/agent_grade.py` beside this file is the runnable,
end to end example, and imports its HTTP call, its ffmpeg decode and its
measurement straight from here instead of carrying its own copies.

    .venv/bin/python studio/tools/grade_client.py http://127.0.0.1:7431
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np

try:
    from PIL import Image, ImageDraw, ImageFont
    _HAVE_PIL = True
except ImportError:                                   # pragma: no cover
    _HAVE_PIL = False

HERE = Path(__file__).resolve().parent
STUDIO = HERE.parent
CONTENT = STUDIO.parent
GRADE = CONTENT / "grade"

DEFAULT_BASE = "http://127.0.0.1:7431"
# Every ffmpeg/ffprobe call in this module is bounded by BOTH a subprocess
# timeout and an explicit -t on any video read, so a bad file or a hung pipe
# cannot leave a caller waiting forever.
FFMPEG_TIMEOUT = 60.0


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------

class StudioError(Exception):
    """An HTTP error from the studio server, carrying its own message.

    The server answers every 4xx/5xx as JSON, `{"error": "..."}`
    (studio/server.py's `_dispatch`), and `message` is exactly that text,
    not a decoded traceback. `status` is the HTTP status code (None for a
    connection failure, never reaching the server at all) and `route` is
    "METHOD /api/path" so a caller can tell two failures on different
    routes apart without parsing the string.

    str(exc) reads as "METHOD /api/path -> HTTP STATUS: message" (or
    "METHOD /api/path -> could not reach BASE: reason" when the server was
    never reached), which is the exact shape a hand rolled wrapper failed to
    produce when it let a bare urllib traceback through instead: that
    failure cost a whole turn to diagnose the first time it happened, and is
    the reason this class exists.
    """

    def __init__(self, message: str, status: int | None = None, route: str = ""):
        self.message = message
        self.status = status
        self.route = route
        if status is not None:
            text = f"{route} -> HTTP {status}: {message}" if route \
                else f"HTTP {status}: {message}"
        else:
            text = f"{route} -> {message}" if route else message
        super().__init__(text)


def _server_message(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(obj, dict) and "error" in obj:
        return str(obj["error"])
    return text


# --------------------------------------------------------------------------
# transport: one free function, stateless, everything else builds on it
# --------------------------------------------------------------------------

def _url(base: str, path: str, params: dict | None = None) -> str:
    url = base.rstrip("/") + path
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    return url


def request(method: str, base: str, path: str, token: str | None = None,
           params: dict | None = None, body: dict | None = None,
           want_json: bool = True, timeout: float = 60.0,
           headers: dict | None = None):
    """One HTTP call against a studio server.

    Free standing on purpose: `studio/tools/agent_grade.py` called exactly
    this shape (`request(method, base, path, token=..., body=...)`) before
    this module existed, so it can import this one and keep every call site
    unchanged. `Studio` below is a thin, stateful convenience wrapper that
    remembers a base URL, a token and the agent/attach/rotation headers so a
    caller does not have to repeat them on every call; every one of its
    methods still ends up here.

    Raises StudioError on a 4xx/5xx or a connection failure; never returns a
    half read body.
    """
    url = _url(base, path, params)
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    if token:
        hdrs.setdefault("Authorization", f"Bearer {token}")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        msg = _server_message(exc.read())
        raise StudioError(msg, exc.code, f"{method} {path}") from None
    except urllib.error.URLError as exc:
        raise StudioError(f"could not reach {base}: {exc.reason}",
                          None, f"{method} {path}") from None
    if not want_json:
        return raw
    if not raw:
        return {}
    return json.loads(raw)


# --------------------------------------------------------------------------
# Studio: one agent's handle on a running server
# --------------------------------------------------------------------------

class Studio:
    """One agent's handle on a running `studio/server.py`.

    `agent` is sent as the `X-Studio-Agent` header on every call (env
    `STUDIO_AGENT` is the default when `agent` is not given explicitly);
    with logins off this lazily creates a `agent:NAME` user row that can
    never log in, and every commit it makes is signed with that name.
    `attach` is sent as `X-Studio-Attach` (logins off only; refused with
    logins on) and makes this agent act on another caller's session,
    workspace and match crops while still signing its own commits.

    `rotation`, once set, is sent as the top level `"rotation"` field on
    every `frame`, `stats`, `match` and `session_patch` call this object
    makes, so it is never silently left to whatever the shared session or
    the open project last had (two lanes in the bakeoff review lost real
    time to exactly that omission).

    Nothing here is a login: with logins off (the default) `token` is
    unused. With logins on, pass the bearer token from `POST /api/auth/token`
    (see "Agent API" > "Getting a token" in studio/README.md).
    """

    def __init__(self, base: str | None = None, token: str | None = None,
                agent: str | None = None, attach: str | None = None,
                rotation: str | None = None, timeout: float = 60.0):
        self.base = (base or os.environ.get("STUDIO_URL")
                    or DEFAULT_BASE).rstrip("/")
        self.token = token
        self.agent = agent if agent is not None \
            else (os.environ.get("STUDIO_AGENT") or None)
        self.attach = attach
        self.rotation = rotation
        self.timeout = timeout

    # -- transport ----------------------------------------------------

    def _headers(self) -> dict:
        headers = {}
        if self.agent:
            headers["X-Studio-Agent"] = self.agent
        if self.attach is not None:
            headers["X-Studio-Attach"] = str(self.attach)
        return headers

    def request(self, method: str, path: str, params: dict | None = None,
               body: dict | None = None, want_json: bool = True):
        """The escape hatch: any route, wrapped or not, through this
        object's base URL, token and agent/attach headers."""
        return request(method, self.base, path, token=self.token,
                      params=params, body=body, want_json=want_json,
                      timeout=self.timeout, headers=self._headers())

    def _with_rotation(self, body: dict | None) -> dict:
        body = dict(body or {})
        if self.rotation is not None and body.get("rotation") in (None, ""):
            body["rotation"] = self.rotation
        return body

    # -- read only ------------------------------------------------------

    def state(self) -> dict:
        """GET /api/state: defaults, clips, presets, looks, refs, caller."""
        return self.request("GET", "/api/state")

    def health(self) -> dict:
        """GET /api/health: {ok, version, clips, uptime_s,
        ffmpeg_slots_free, cache_dir, data_dir, logins} (contract G2)."""
        return self.request("GET", "/api/health")

    def whoami(self) -> dict:
        """GET /api/whoami: {"user", "by", "auth", "project", "caller"}.
        `project` is the open project's content key (a hash); use
        `project()` below, or `project_open()`'s own return value, for the
        clip name that key belongs to."""
        return self.request("GET", "/api/whoami")

    def project_open(self, clip: str, rotation: str | None = None,
                     by: str | None = None) -> dict:
        """POST /api/project/open: open or create a clip's project and make
        it this caller's current one.

        Returns the project dict at the TOP level, with the clip under
        `"name"`, not `["project"]["clip"]`: the shape the first agent to
        call this route without this module guessed and got a `KeyError`
        on (contract G9 friction 1). `{"open": True, "key", "name", ...}`
        on success; `rotation`, given, is set right after opening (a second
        call, `POST /api/project/rotation`).
        """
        body: dict = {"clip": clip}
        if by is not None:
            body["by"] = by
        out = self.request("POST", "/api/project/open", body=body)
        if rotation:
            rot_body: dict = {"rotation": rotation}
            if by is not None:
                rot_body["by"] = by
            out = self.request("POST", "/api/project/rotation", body=rot_body)
        return out

    def project(self) -> dict:
        """GET /api/project: this caller's open project, the same shape
        `project_open()` returns: `{"open": False, ...}` when nothing is
        open yet, `{"open": True, "key", "name", "branch", "head", ...}`
        otherwise."""
        return self.request("GET", "/api/project")

    # -- pictures and numbers --------------------------------------------

    def frame(self, clip: str | None = None, time: float = 0.0,
             config: dict | None = None, width: int = 960, out=None,
             region=None, zoom: float | None = None, mode: str = "graded",
             mask_layer: int | None = None, path: str | None = None,
             format: str | None = None):
        """POST /api/frame: one rendered frame.

        `clip` names a clip from `GET /api/state`; `path` (an absolute file
        outside the footage root, logins off only) reads a file directly
        without registering or touching any session, and wins if both are
        given. Returns the raw response bytes (a JPEG unless `format="raw"`
        asks for rgb24), or writes them to `out` and returns that `Path`.
        """
        body = self._with_rotation({"time": time, "width": width,
                                    "config": config or {}, "mode": mode})
        if path is not None:
            body["path"] = path
        elif clip is not None:
            body["clip"] = clip
        for key, value in (("region", region), ("zoom", zoom),
                          ("mask_layer", mask_layer), ("format", format)):
            if value is not None:
                body[key] = value
        data = self.request("POST", "/api/frame", body=body, want_json=False)
        if out is not None:
            out_path = Path(out)
            out_path.write_bytes(data)
            return out_path
        return data

    def stats(self, clip: str | None = None, time: float = 0.0,
             config: dict | None = None, width: int = 640, region=None,
             path: str | None = None) -> dict:
        """POST /api/stats: {"key", "stats", "size"} for one frame.

        Same argument shape as `frame` on purpose (a measure and a look are
        meant to be one line apart): `clip`/`path`, `time`, `config`,
        `width` and `region` all mean what they mean there.
        """
        body = self._with_rotation({"time": time, "width": width,
                                    "config": config or {}})
        if path is not None:
            body["path"] = path
        elif clip is not None:
            body["clip"] = clip
        if region is not None:
            body["region"] = region
        return self.request("POST", "/api/stats", body=body)

    def stats_at(self, clip: str, times, config: dict | None = None,
                width: int = 640) -> list:
        """POST /api/stats with a `times` list (contract G3): one call, one
        render per time, returns the `results` list, each entry `{time,
        key, size, stats}`. On a server that has not shipped this yet the
        route silently answers the single time shape instead: check for a
        `results` key before trusting the length of what comes back."""
        body = self._with_rotation({"clip": clip, "times": list(times),
                                    "width": width, "config": config or {}})
        result = self.request("POST", "/api/stats", body=body)
        return result.get("results", [])

    def ref_stats(self, name: str, region=None) -> dict:
        """POST /api/stats with `ref` where `clip` goes (contract G3): the
        same measurement, taken on a reference image from `content/refs`
        instead of a rendered frame, so a reference and a clip are
        comparable through the one function."""
        body = self._with_rotation({"ref": name})
        if region is not None:
            body["region"] = region
        return self.request("POST", "/api/stats", body=body)

    def match(self, ref: str, clip: str, time: float = 0.0,
             config: dict | None = None, method: str = "reinhard",
             strength: float = 1.0, luma_preserve: bool = True,
             ref_crop=None, frame_crop=None, name: str | None = None,
             out_dir: str | None = None) -> dict:
        """POST /api/match: fit a look cube toward a reference image.

        `ok: false` in the response means the fit failed its own health
        check; do not apply it (see "Match Reference" in studio/README.md).
        `name` and `out_dir` are forwarded to `match_reference()` (contract
        G3, landed). The response also carries `recommended` (bool) and
        `bands` for the reference and source images alongside the
        pre-existing fields.

        `ref_crop`/`frame_crop` are always sent, `[0.0, 0.0, 1.0, 1.0]`
        (the whole image, a no-op crop) when the caller gives none: the
        server reads an ABSENT key as "use whatever rectangle this project
        last had saved for this reference" and only reads an explicit key
        (even a full frame one) as "this is the rectangle, nothing saved".
        Sending the field unconditionally is what keeps two callers who
        never drew a box from silently inheriting each other's crop.
        """
        body = self._with_rotation({
            "ref": ref, "clip": clip, "time": time, "config": config or {},
            "method": method, "strength": strength,
            "luma_preserve": luma_preserve,
            "ref_crop": list(ref_crop) if ref_crop is not None
                       else [0.0, 0.0, 1.0, 1.0],
            "frame_crop": list(frame_crop) if frame_crop is not None
                         else [0.0, 0.0, 1.0, 1.0],
        })
        if name is not None:
            body["name"] = name
        if out_dir is not None:
            body["out_dir"] = out_dir
        return self.request("POST", "/api/match", body=body)

    # -- saving -----------------------------------------------------------

    def preset_save(self, name: str, config: dict, comment: str = "") -> dict:
        """POST /api/preset: {"saved", "path", "presets"}."""
        return self.request("POST", "/api/preset",
                            body={"name": name, "config": config,
                                  "comment": comment})

    def preset_load(self, name: str, expand: bool = True) -> dict:
        """GET /api/preset: {"name", "comment", "config", "expanded"}
        (contract G4, landed). `"config"` is always the fully defaults
        filled config, expand or not; `"expanded"` is a plain boolean, true
        when `?expand=true` was sent, not a second copy of the config under
        that key. `expand` defaults to True here because it costs nothing
        extra (the config is expanded either way) and tells a caller it can
        rely on `"expanded"` being present to check."""
        return self.request("GET", "/api/preset",
                            params={"name": name,
                                   "expand": "true" if expand else "false"})

    def grade_save(self, clip: str, config: dict, message: str | None = None) -> dict:
        """PUT /api/grade: {"key", "updated_at", "head"}."""
        body = {"clip": clip, "config": config}
        if message is not None:
            body["message"] = message
        return self.request("PUT", "/api/grade", body=body)

    def grade_load(self, clip: str) -> dict:
        """GET /api/grade: {"exists", "key", "config", "updated_at", ...}."""
        return self.request("GET", "/api/grade", params={"clip": clip})

    # -- the live session ---------------------------------------------------

    def session_get(self) -> dict:
        """GET /api/session: this caller's live config, clip and revision."""
        return self.request("GET", "/api/session")

    def session_patch(self, config: dict, if_rev: int | None = None,
                      **extra) -> dict:
        """POST /api/session: deep merges `config` into the live config and
        commits it. `if_rev` (contract G2) is the revision this caller last
        read; a mismatch is refused with a 409 and the current revision
        rather than silently overwriting somebody else's edit in between,
        raised here as a StudioError with `status == 409`.

        `**extra` carries the rest of the wire shape (`clip`, `time`, `by`,
        `message`, `replace`) exactly as `POST /api/session` documents it in
        studio/README.md, so nothing here has to be re-guessed as this
        route grows.
        """
        body = self._with_rotation(dict(extra, config=config))
        if if_rev is not None:
            body["if_rev"] = if_rev
        return self.request("POST", "/api/session", body=body)

    # -- reporting, never choosing ------------------------------------------

    def sweep(self, clip: str, time: float, base_config: dict, param: str,
             values, width: int = 640) -> list:
        """One stats row per value of one dotted config path, holding
        everything else fixed. Deep copies `base_config` for every value so
        the caller's own dict is never mutated, and returns
        `[(value, stats), ...]`, `stats` being the measurement dict
        (`POST /api/stats`'s own `"stats"` field), never the choice of
        which value is "best": that judgement stays with whoever reads the
        table, by design (no numeric distance score, no ranking).
        """
        rows = []
        for value in values:
            cfg = copy.deepcopy(base_config)
            _set_dotted(cfg, param, value)
            result = self.stats(clip=clip, time=time, config=cfg, width=width)
            rows.append((value, result.get("stats", result)))
        return rows


def _set_dotted(cfg: dict, dotted: str, value) -> None:
    """Set `cfg["a"]["b"]["c"] = value` from the dotted path `"a.b.c"`,
    creating any missing intermediate dict along the way."""
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


# --------------------------------------------------------------------------
# reading a stats dict: the summaries four lanes each wrote by hand
# --------------------------------------------------------------------------

def _measurement(stats: dict) -> dict:
    """Unwrap a full `POST /api/stats` response ({"key", "stats", "size"})
    down to the bare measurement dict, so `brief`, `bands` and `diff` take
    either shape: the route's own response, or the `"stats"` sub-dict
    already pulled out of it."""
    if isinstance(stats, dict) and isinstance(stats.get("stats"), dict):
        return stats["stats"]
    return stats or {}


def brief(stats: dict, clip: str | None = None) -> str:
    """The one line p5 p25 p50 p75 p95 sat r g b [clip] summary: what an
    agent prints after every measurement instead of dumping the whole
    dict."""
    m = _measurement(stats)
    luma = m.get("luma") or {}
    sat = m.get("saturation") or {}
    ch = m.get("channels") or {}

    def g(d: dict, key: str) -> str:
        v = d.get(key)
        return f"{v:.4f}" if isinstance(v, (int, float)) else "n/a"

    line = (f"p5={g(luma, 'p5')} p25={g(luma, 'p25')} p50={g(luma, 'p50')} "
            f"p75={g(luma, 'p75')} p95={g(luma, 'p95')} sat={g(sat, 'mean')} "
            f"r={g(ch, 'r')} g={g(ch, 'g')} b={g(ch, 'b')}")
    if clip:
        line += f" clip={clip}"
    return line


def bands(stats: dict) -> list:
    """Printable rows for the `"bands"` block (contract G3): edges,
    saturation, warm, tint, count, one row per luma band. Measurements
    only, never a score: nothing here ranks one band against another.
    Absent on a server that has not shipped it yet, in which case this
    returns a one line note instead of raising, so a caller can print
    `bands(stats)` unconditionally."""
    m = _measurement(stats)
    b = m.get("bands")
    if not b:
        return ["(no bands block: this server has not shipped per band "
                "measurements yet, see contract G3)"]
    edges = b.get("edges") or []
    sat = b.get("saturation") or []
    warm = b.get("warm") or []
    tint = b.get("tint") or []
    count = b.get("count") or []
    n = max(len(sat), len(warm), len(tint), len(count))
    rows = []
    for i in range(n):
        lo = edges[i] if i < len(edges) else None
        hi = edges[i + 1] if i + 1 < len(edges) else None
        span = f"{lo:.3f}-{hi:.3f}" if isinstance(lo, (int, float)) \
            and isinstance(hi, (int, float)) else "?"
        s = sat[i] if i < len(sat) else None
        w = warm[i] if i < len(warm) else None
        t = tint[i] if i < len(tint) else None
        c = count[i] if i < len(count) else None
        rows.append(f"band {i} [{span}] saturation={s} warm={w} tint={t} "
                   f"count={c}")
    return rows


def _numeric_diff(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        out = {}
        for key in sorted(set(a) & set(b)):
            sub = _numeric_diff(a[key], b[key])
            if sub not in (None, {}):
                out[key] = sub
        return out
    is_num = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)
    if is_num(a) and is_num(b):
        return round(float(b) - float(a), 6)
    if (isinstance(a, list) and isinstance(b, list) and len(a) == len(b)
            and a and all(is_num(x) for x in a) and all(is_num(x) for x in b)):
        return [round(float(y) - float(x), 6) for x, y in zip(a, b)]
    return None


def diff(a: dict, b: dict) -> dict:
    """Signed deltas per key, `b` minus `a`, in the same nested shape as
    the stats dicts themselves (`luma.p50`, `channels.r`, a `bands` list
    element by element). The "am I closer" line: a positive
    `luma.p50` means `b` is brighter than `a`. Keys either side does not
    have, and any non numeric leaf, are left out rather than guessed at.

    `"definitions"` (sat_floor, the clip codes, the hue family boundaries)
    and `"bands.edges"` (the luma band boundaries) are measurement
    constants, not measurements: on two frames measured by the same server
    they are always identical, so a numeric diff of them is always zero and
    buries the real deltas in noise (contract G9 friction 4). Both are
    dropped here rather than left for a caller to filter out by hand.
    """
    out = _numeric_diff(_measurement(a), _measurement(b))
    if not isinstance(out, dict):
        return out
    out.pop("definitions", None)
    bands_diff = out.get("bands")
    if isinstance(bands_diff, dict):
        bands_diff.pop("edges", None)
    return out


# --------------------------------------------------------------------------
# decode: ffmpeg rgb24 into numpy, the one place this happens
# --------------------------------------------------------------------------

def _probe_video_wh(path: Path, at_time: float | None, timeout: float) -> tuple:
    args = ["ffprobe", "-v", "error", "-select_streams", "v:0"]
    if at_time is not None:
        args += ["-ss", str(float(at_time))]
    args += ["-show_entries", "stream=width,height", "-of", "json", str(path)]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise StudioError(f"ffprobe could not read {path}: "
                          f"{proc.stderr.strip()}")
    streams = json.loads(proc.stdout).get("streams") or []
    if not streams:
        raise StudioError(f"ffprobe found no video stream in {path}")
    return int(streams[0]["width"]), int(streams[0]["height"])


def decode(path_or_bytes, width: int | None = None,
          time: float | None = None) -> np.ndarray:
    """Decode a still or one video frame to an HxWx3 uint8 rgb24 array.

    `path_or_bytes` is a path (str or Path) or raw bytes already read (a
    JPEG the server just returned, for instance, no round trip to disk
    needed by the caller). `time` picks a frame out of a video, in seconds;
    left at None this reads the first frame, which is what a still needs.
    `width` scales to that width, height following the source's own aspect
    (rounded to an even number, same rule the studio itself scales by);
    left at None this decodes at the source's native size.

    Every ffmpeg and ffprobe call here is bounded: `-t 1` on top of the
    already-single `-frames:v 1` on any read, plus a subprocess timeout, so
    a bad file or a stuck pipe cannot hang a caller.
    """
    tmp_path = None
    try:
        if isinstance(path_or_bytes, (bytes, bytearray)):
            tmp = tempfile.NamedTemporaryFile(suffix=".img", delete=False)
            tmp.write(bytes(path_or_bytes))
            tmp.close()
            tmp_path = Path(tmp.name)
            src = tmp_path
        else:
            src = Path(path_or_bytes)
            if not src.exists():
                raise StudioError(f"decode: no such file: {src}")

        src_w, src_h = _probe_video_wh(src, time, FFMPEG_TIMEOUT)
        if width:
            out_w = int(width)
            out_h = max(2, int(round(out_w * src_h / src_w / 2)) * 2)
        else:
            out_w, out_h = src_w, src_h

        args = ["ffmpeg", "-v", "error", "-y"]
        if time is not None:
            args += ["-ss", str(float(time))]
        args += ["-i", str(src)]
        if width:
            args += ["-vf", f"scale={out_w}:{out_h}:flags=bicubic"]
        args += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-frames:v", "1",
                 "-t", "1", "-"]
        raw = subprocess.run(args, capture_output=True, timeout=FFMPEG_TIMEOUT)
        need = out_w * out_h * 3
        if raw.returncode != 0 or len(raw.stdout) < need:
            raise StudioError(
                f"ffmpeg could not decode {src}: "
                f"{raw.stderr.decode('utf-8', 'replace')[-400:]}")
        arr = np.frombuffer(raw.stdout, dtype=np.uint8)[:need]
        return arr.reshape((out_h, out_w, 3))
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# measure: frame_stats on an array, the server's own code, never a copy
# --------------------------------------------------------------------------

_FRAME_STATS_FN = None


def _frame_stats_fn():
    global _FRAME_STATS_FN
    if _FRAME_STATS_FN is not None:
        return _FRAME_STATS_FN
    try:
        if str(GRADE) not in sys.path:
            sys.path.insert(0, str(GRADE))
        from stats import frame_stats as fn        # grade/stats.py, contract G3
    except ImportError:
        # Fallback until grade/stats.py lands: the function moves out of
        # studio/server.py unchanged in output (contract G3), so importing
        # it from there in the meantime is the same code, not a copy of it.
        if str(STUDIO) not in sys.path:
            sys.path.insert(0, str(STUDIO))
        from server import frame_stats as fn        # noqa: PLC0415
    _FRAME_STATS_FN = fn
    return fn


def measure(array: np.ndarray) -> dict:
    """`frame_stats(array)`, the server's own measurement function, on a
    decoded rgb24 array. Imported, never reimplemented: see
    `_frame_stats_fn` for exactly where it comes from on this server."""
    return _frame_stats_fn()(array)


# --------------------------------------------------------------------------
# contact_sheet: several stills or clips, one image, mixed aspect ratios
# --------------------------------------------------------------------------

_LABEL_STRIP_H = 28
_LABEL_BG = (24, 24, 28)
_LABEL_FG = (235, 235, 235)


def _still_from_input(path: Path, out_path: Path, height: int) -> None:
    """One frame from a still or a video, scaled to `height`, width
    following aspect (`scale=-2:HEIGHT` keeps it even). Bounded: `-t 1` on
    a video read; a still has no duration to bound."""
    suffix = path.suffix.lower()
    is_video = suffix not in (".png", ".jpg", ".jpeg", ".bmp", ".gif",
                             ".tif", ".tiff", ".webp")
    args = ["ffmpeg", "-v", "error", "-y"]
    if is_video:
        args += ["-ss", "0", "-t", "1"]
    args += ["-i", str(path), "-vf",
             f"scale=-2:{int(height)}:flags=bicubic,setsar=1",
             "-frames:v", "1", str(out_path)]
    proc = subprocess.run(args, capture_output=True, timeout=FFMPEG_TIMEOUT)
    if proc.returncode != 0:
        raise StudioError(f"contact_sheet could not read {path}: "
                          f"{proc.stderr.decode('utf-8', 'replace')[-400:]}")


def _draw_label(image_path: Path, text: str) -> None:
    """Add one text label in a strip above the still. A silent no-op
    without Pillow: an unlabeled sheet is still a usable sheet, which is
    the fallback the brief for this module asks for."""
    if not _HAVE_PIL:
        return
    im = Image.open(image_path).convert("RGB")
    w, h = im.size
    canvas = Image.new("RGB", (w, h + _LABEL_STRIP_H), _LABEL_BG)
    canvas.paste(im, (0, _LABEL_STRIP_H))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((max(0, (w - tw) / 2), (_LABEL_STRIP_H - th) / 2 - bbox[1]),
             text, fill=_LABEL_FG, font=font)
    canvas.save(image_path)


def _probe_still_wh(path: Path) -> tuple:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
    if proc.returncode != 0:
        raise StudioError(f"ffprobe failed on {path}: {proc.stderr.strip()}")
    streams = json.loads(proc.stdout).get("streams") or []
    if not streams:
        raise StudioError(f"no video stream in {path}")
    return int(streams[0]["width"]), int(streams[0]["height"])


def _hstack(paths: list, out_path: Path) -> None:
    if len(paths) == 1:
        shutil.copy(paths[0], out_path)
        return
    args = ["ffmpeg", "-v", "error", "-y"]
    for p in paths:
        args += ["-i", str(p)]
    graph = "".join(f"[{i}:v]" for i in range(len(paths))) \
        + f"hstack=inputs={len(paths)}[o]"
    args += ["-filter_complex", graph, "-map", "[o]", "-frames:v", "1",
             str(out_path)]
    proc = subprocess.run(args, capture_output=True, timeout=FFMPEG_TIMEOUT)
    if proc.returncode != 0:
        raise StudioError(f"contact_sheet hstack failed: "
                          f"{proc.stderr.decode('utf-8', 'replace')[-400:]}")


def _vstack(paths: list, out_path: Path) -> None:
    if len(paths) == 1:
        shutil.copy(paths[0], out_path)
        return
    args = ["ffmpeg", "-v", "error", "-y"]
    for p in paths:
        args += ["-i", str(p)]
    graph = "".join(f"[{i}:v]" for i in range(len(paths))) \
        + f"vstack=inputs={len(paths)}[o]"
    args += ["-filter_complex", graph, "-map", "[o]", "-frames:v", "1",
             str(out_path)]
    proc = subprocess.run(args, capture_output=True, timeout=FFMPEG_TIMEOUT)
    if proc.returncode != 0:
        raise StudioError(f"contact_sheet vstack failed: "
                          f"{proc.stderr.decode('utf-8', 'replace')[-400:]}")


def _pad_to_width(image_path: Path, out_path: Path, target_w: int) -> None:
    args = ["ffmpeg", "-v", "error", "-y", "-i", str(image_path), "-vf",
            f"pad={target_w}:ih:0:0:color=black", "-frames:v", "1",
            str(out_path)]
    proc = subprocess.run(args, capture_output=True, timeout=FFMPEG_TIMEOUT)
    if proc.returncode != 0:
        raise StudioError(f"contact_sheet padding failed: "
                          f"{proc.stderr.decode('utf-8', 'replace')[-400:]}")


def contact_sheet(paths, out, labels=None, height: int = 480,
                  grid: str | None = None) -> Path:
    """One image from several stills or video files, mixed aspect ratios
    included.

    Every panel is scaled to the same HEIGHT, never a common width: hstack
    (ffmpeg's own side by side filter) refuses inputs of different heights,
    which is exactly the bug that sent one of the bakeoff lanes down a
    rabbit hole before this function existed. `grid` is `"COLSxROWS"`
    (ffmpeg's own `tile` convention); left at None every panel goes in one
    row. A row shorter than the widest one is padded with black on the
    right (never stretched) before the rows are stacked, so a grid with an
    uneven last row still comes out rectangular.

    `labels`, given, draws one line of text above each panel with Pillow
    when it is installed and is silently skipped when it is not: see
    `_draw_label`.
    """
    paths = [Path(p) for p in paths]
    if not paths:
        raise StudioError("contact_sheet needs at least one input")
    if labels is not None and len(labels) != len(paths):
        raise StudioError("contact_sheet: labels must be the same length "
                          "as paths")

    if grid:
        cols_s, _, rows_s = str(grid).lower().partition("x")
        cols = int(cols_s)
        if cols <= 0:
            raise StudioError(f"contact_sheet: bad grid {grid!r}")
    else:
        cols = len(paths)

    out_path = Path(out)
    tmp = Path(tempfile.mkdtemp(prefix="grade_client_sheet_"))
    try:
        panels = []
        for i, p in enumerate(paths):
            still = tmp / f"panel_{i:03d}.png"
            _still_from_input(p, still, height)
            if labels is not None:
                _draw_label(still, str(labels[i]))
            panels.append(still)

        rows = [panels[i:i + cols] for i in range(0, len(panels), cols)]
        row_images = []
        for r, chunk in enumerate(rows):
            row_path = tmp / f"row_{r:03d}.png"
            _hstack(chunk, row_path)
            row_images.append(row_path)

        if len(row_images) == 1:
            shutil.copy(row_images[0], out_path)
        else:
            widths = [_probe_still_wh(r)[0] for r in row_images]
            max_w = max(widths)
            padded = []
            for r, w in zip(row_images, widths):
                if w == max_w:
                    padded.append(r)
                else:
                    pad_path = tmp / f"pad_{r.stem}.png"
                    _pad_to_width(r, pad_path, max_w)
                    padded.append(pad_path)
            _vstack(padded, out_path)
        return out_path
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# __main__: the first thing an agent should run against a new server
# --------------------------------------------------------------------------

def _main() -> None:
    print(__doc__.strip().splitlines()[0])
    print("usage: .venv/bin/python studio/tools/grade_client.py "
         "[BASE_URL]  (default http://127.0.0.1:7431, or env STUDIO_URL)")
    base = sys.argv[1] if len(sys.argv) > 1 else None
    studio = Studio(base=base)
    print(f"\nbase: {studio.base}")

    try:
        health = studio.health()
        print(f"GET /api/health: {json.dumps(health)}")
    except StudioError as exc:
        print(f"GET /api/health not available on this server ({exc}); "
             "continuing with GET /api/state alone")

    state = studio.state()
    caller = state.get("caller") or {}
    print(f"GET /api/state: caller={caller.get('name')!r} "
         f"{len(state.get('clips', []))} clip(s)")
    for clip in state.get("clips", []):
        note = clip.get("error") or ""
        print(f"  {clip.get('name')}" + (f"  ({note})" if note else ""))


if __name__ == "__main__":
    try:
        _main()
    except StudioError as exc:
        sys.exit(f"grade_client.py: {exc}")
