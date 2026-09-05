"""The GPU final render path: ffmpeg decodes, gpu.js grades, ffmpeg encodes.

The shipped render (server.start_render) is one ffmpeg process running the
whole filter graph, which measured about 2.3 fps at 4K. This module keeps the
two ends of that pipeline (ffmpeg decodes the clip, ffmpeg encodes the file)
and replaces the middle with the WebGL2 port in static/gpu.js, driven in a
headless Chrome by tools/render_worker.mjs.

Nothing here is on the ffmpeg path. Engine "ffmpeg" is untouched and stays the
reference; engine "gpu" is this file.

Three things decide whether the output is honest, and all three were measured
rather than assumed (numbers in studio/static/limits.js):

1. The frames coming back from the browser are in the format ffmpeg's own
   graph is carrying at the point this path cuts it, which is not one format
   but three (_chain_cut below, read out of `ffmpeg -v debug` on the engine's
   real graph): gbrp16le when the chain never leaves 16 bit, yuv444p when a
   vignette and a detail stage make ffmpeg drop to 8 bit YUV, rgb24 when a
   vignette alone makes it drop to 8 bit RGB. Two measurements force this:
   - PLANAR, not packed. swscale reaches yuv422p10le by a different path from
     a packed 16 bit RGB input than from a planar one: on the cinematic preset
     at 640 wide, planar reproduces the single process render exactly (max
     0.000 of 255) and packed is off by up to 17.4 of 255 on 3.7 percent of
     channels.
   - the right DEPTH. Handing the encoder 16 bit RGB where ffmpeg had an 8 bit
     buffer makes the finished file about 3.5 of 1023 brighter in luma, a
     systematic offset rather than noise.

2. The final RGB to YUV conversion is a whole graph negotiation in ffmpeg, not
   a per link one (the same effect f_log_stage's docstring documents for
   unsharp). Measured against the single pass render of the same config:
   - no grain, so the encode graph is just the format tail: the pipe has to be
     stamped with the clip's own colorspace (setparams=colorspace=<clip>) to
     land where the reference lands. 0.000 with the stamp, 8.8 of 255 on 56.9
     percent of channels without it.
   - grain on, so the encode graph carries the noise plate input and the blend:
     NO stamp is correct. 0.000 without it, 10.6 of 255 with it.
   Hence _tail_stamp() below, which is a measurement, not a preference.

3. Grain stays on ffmpeg and is applied here, between the GPU's picture and
   the encoder, in the same place build_graph puts it (after FX, before detail
   and letterbox). When grain is on the GPU is told to skip detail and
   letterbox so the order of operations is the engine's order.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

STUDIO = Path(__file__).resolve().parent
CONTENT = STUDIO.parent
TESTS = STUDIO / "tests"
WORKER_SCRIPT = STUDIO / "tools" / "render_worker.mjs"

# The server module, handed over by server.py at import time. A plain
# "import server" would load a SECOND copy of it (the server runs as
# __main__), with its own JOBS dict and its own caches, so the binding is
# explicit instead.
SRV = None


def bind(module) -> None:
    global SRV
    SRV = module


# --------------------------------------------------------------------------
# where Chrome and node live
# --------------------------------------------------------------------------

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]

NODE_CANDIDATES = [
    "/opt/homebrew/bin/node",
    "/usr/local/bin/node",
    "/usr/bin/node",
]


def find_chrome() -> str | None:
    env = os.environ.get("STUDIO_CHROME")
    if env and Path(env).exists():
        return env
    for path in CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    return None


def find_node() -> str | None:
    env = os.environ.get("STUDIO_NODE")
    if env and Path(env).exists():
        return env
    found = shutil.which("node")
    if found:
        return found
    for path in NODE_CANDIDATES:
        if Path(path).exists():
            return path
    return None


def availability() -> dict:
    """Why the GPU engine can or cannot run, in words a dialog can show."""
    chrome, node = find_chrome(), find_node()
    reasons = []
    if not chrome:
        reasons.append("no Chrome (looked in " + CHROME_CANDIDATES[0] + ")")
    if not node:
        reasons.append("no node on PATH")
    if not WORKER_SCRIPT.exists():
        reasons.append("missing " + str(WORKER_SCRIPT))
    if not (TESTS / "node_modules" / "puppeteer-core").exists():
        reasons.append("puppeteer-core is not installed under studio/tests")
    return {"available": not reasons, "reasons": reasons,
            "chrome": chrome or "", "node": node or ""}


# --------------------------------------------------------------------------
# per job worker credentials
# --------------------------------------------------------------------------

# job id -> _Session. Every live GPU render, and nothing else.
SESSIONS: dict[str, "_Session"] = {}
SESSIONS_LOCK = threading.Lock()

# The routes a render worker's page is allowed to reach with its job token.
# "lut" is on the list because gpu.js fetches its own technical cubes; without
# it the worker would need a real login, which is the thing this avoids.
WORKER_ROUTES = {"render/gpu/plan", "render/gpu/next", "render/gpu/frame", "lut"}


def _session_for_token(token: str) -> "_Session | None":
    if not token:
        return None
    with SESSIONS_LOCK:
        for sess in SESSIONS.values():
            if sess.token and secrets.compare_digest(sess.token, token):
                return sess
    return None


def _bearer(handler) -> str:
    raw = handler.headers.get("Authorization", "") or ""
    return raw[7:].strip() if raw[:7].lower() == "bearer " else ""


def worker_authorised(handler, route: str, method: str) -> bool:
    """True when this request is a live render worker's, on its own routes.

    Deliberately narrow, because it runs BEFORE the login gate in
    server._api. Three conditions, all required: the route is one of the four
    a worker uses, the caller is on the loopback interface (the worker's
    Chrome is a child of this process, so it always is, and nobody on the
    network ever is), and the Authorization header carries the random token
    minted for a render that is still running. The token dies with the job.
    """
    if route not in WORKER_ROUTES or method not in ("GET", "POST"):
        return False
    addr = handler.client_address[0] if handler.client_address else ""
    if not _is_loopback(addr):
        return False
    return _session_for_token(_bearer(handler)) is not None


def _is_loopback(addr: str) -> bool:
    """Same rule the reveal route uses: local, and provably local.

    trusted_loopback rather than is_loopback because with
    --behind-https-proxy a reverse proxy on this machine makes every request
    from every device arrive from 127.0.0.1, so the peer address stops
    proving anything. Under a proxy this returns False for everyone and the
    GPU render worker (which is a child of this process and could otherwise
    connect) is refused along with the rest: a render that cannot start is
    the right failure, an open frame route is not. The per render token is
    checked as well, never instead.
    """
    try:
        return SRV.AUTH.trusted_loopback(addr)
    except AttributeError:
        return SRV.AUTH.is_loopback(addr)
    except Exception:                                         # noqa: BLE001
        return addr in ("127.0.0.1", "::1", "localhost")


# --------------------------------------------------------------------------
# building the two ffmpeg command lines
# --------------------------------------------------------------------------

# setparams refuses a colorspace it does not know, and an untagged clip has to
# stay untagged rather than be guessed at.
SETPARAMS_COLORSPACES = {
    "bt709", "fcc", "bt470bg", "smpte170m", "smpte240m", "ycgco",
    "bt2020nc", "bt2020c", "smpte2085", "chroma-derived-nc",
    "chroma-derived-c", "ictcp",
}


def _tail_stamp(cfg: dict, info: dict) -> str:
    """The setparams the encode graph needs in front of the format tail.

    See the module docstring: this is a measured value. With the grain plate
    in the graph the reference lands on ffmpeg's default matrix and a stamp
    moves it away; without the plate the reference keeps the clip's own
    colorspace and only a stamp reproduces it.
    """
    if cfg["grain"]["enabled"]:
        return ""
    cs = str((info or {}).get("color_space") or "").lower()
    return f"setparams=colorspace={cs}," if cs in SETPARAMS_COLORSPACES else ""


# What ffmpeg's own graph is carrying at the point this path cuts it, which
# is what the browser has to hand back. Mirrors chainPlan() in gpu.js, and
# both were checked against `ffmpeg -v debug` on the engine's real graph:
#   vignette and detail -> yuv444p, full range, 8 bit
#   vignette alone      -> rgb24, 8 bit
#   otherwise           -> gbrp16le, and the chain never left 16 bit
# The two sides compute this independently. If they ever disagree the frame
# size check in handle() below refuses the frame rather than writing a
# misinterpreted buffer into the encoder.
def _chain_cut(gpu_cfg: dict) -> dict:
    vignette = bool(gpu_cfg["fx"]["vignette"]["enabled"])
    detail = (float(gpu_cfg["detail"].get("soften", 0)) > 1e-6
              or float(gpu_cfg["detail"].get("sharpen", 0)) > 1e-6)
    if vignette and detail:
        return {"format": "yuv444p", "bytes": 3, "yuv": True}
    if vignette:
        return {"format": "rgb24", "bytes": 3, "yuv": False}
    return {"format": "gbrp16le", "bytes": 6, "yuv": False}


def _decode_args(clip_file: str, cfg: dict, info: dict, rinfo: dict,
                 start: float | None, duration: float | None,
                 scaled: bool) -> list[str]:
    """ffmpeg decode: the clip in, normalised planar 16 bit GBR frames out.

    This is exactly the prefix the ffmpeg render runs before the first graded
    pixel: the studio's own downscale (bicubic, the same flags start_render's
    head_extra uses) followed by the normalisation half of f_log_stage. What
    comes out of this pipe is what StudioGPU.setSource is fed in the preview,
    so the browser grades the same numbers the ffmpeg path grades.
    """
    W, H = rinfo["width"], rinfo["height"]
    steps = []
    if scaled:
        steps.append(f"scale={W}:{H}:flags=bicubic,setsar=1")
    steps.append(f"scale=in_color_matrix={SRV.CG.source_matrix(info)}"
                 f":in_range={info['color_range']}:out_range=full")
    steps.append("format=gbrp16le")
    args = ["ffmpeg", "-v", "error", "-y"]
    if not info.get("autorotate", True):
        args += ["-noautorotate"]
    if start:
        args += ["-ss", str(start)]
    args += ["-i", clip_file]
    if duration:
        args += ["-t", str(duration)]
    # rgb48le on the way OUT of this process, interleaved, because that is
    # what StudioGPU.setSource takes and what the preview path already caches
    # (server.source_frame ends the same way). The conversion from the
    # gbrp16le the filter just produced is a reorder of the same 16 bit
    # values, so nothing is lost. The frames coming BACK from the browser are
    # planar gbrp16le instead, and that asymmetry is not an oversight: see
    # the module docstring for the measurement that forces it.
    args += ["-vf", ",".join(steps), "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"]
    return args


def _encode_args(out_path: Path, clip_file: str, cfg: dict, rcfg: dict,
                 info: dict, rinfo: dict, start: float | None,
                 duration: float | None, fps: float, no_audio: bool,
                 cut: dict) -> list[str]:
    """ffmpeg encode: graded frames in over stdin, the finished file out.

    Same codec, profile, pixel format, colour tags and audio mapping as
    start_render, because those are what the user actually receives. The only
    filters here are the ones that come AFTER the GPU's last stage: grain (when
    it is on), then detail and letterbox (only when grain is on, because when
    grain is off the GPU has already done them in the engine's order).
    """
    W, H = rinfo["width"], rinfo["height"]
    o = cfg["output"]
    codec = o["codec"]
    args = ["ffmpeg", "-v", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", cut["format"],
            "-s", f"{W}x{H}", "-r", f"{fps:.6f}"]
    if cut["yuv"]:
        # The 8 bit YUV ffmpeg hands vignette and unsharp is FULL range, and
        # the encoder wants limited, so the pipe has to say which one it is or
        # the picture arrives with its contrast stretched. The matrix tag
        # comes along for the same reason the RGB branch stamps one below.
        args += ["-color_range", "pc"]
        cs = str((info or {}).get("color_space") or "").lower()
        if cs in SETPARAMS_COLORSPACES:
            args += ["-colorspace", cs]
    args += ["-i", "-"]

    grain = bool(cfg["grain"]["enabled"])
    if grain:
        args += SRV.CG.grain_input(rcfg, rinfo)
    audio_idx = None
    if not no_audio:
        audio_idx = 2 if grain else 1
        if not info.get("autorotate", True):
            args += ["-noautorotate"]
        if start:
            args += ["-ss", str(start)]
        args += ["-i", clip_file]
    if duration:
        # Bounds every stream, exactly as start_render's -t does. Without it
        # the audio input would run to the end of the clip.
        args += ["-t", str(duration)]

    tail = SRV.CG.f_detail(rcfg) + SRV.CG.f_letterbox(rcfg, rinfo) if grain else []
    tail += ["format=yuv422p10le",
             "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709"]
    segs = []
    if grain:
        segs.append(f"[1:v]scale={W}:{H}:flags=bilinear,format=gbrp16le,"
                    f"setsar=1[grainplate]")
        segs.append(f"[0:v][grainplate]blend=all_mode=overlay:shortest=1"
                    f":all_opacity={float(rcfg['grain'].get('opacity', 0.5)):.4f}[gx]")
        cur = "gx"
    else:
        stamp = "" if cut["yuv"] else _tail_stamp(cfg, info)
        segs.append(f"[0:v]{stamp}null[gx]")
        cur = "gx"
    segs.append(f"[{cur}]{','.join(tail)}[vout]")

    args += ["-filter_complex", ";".join(segs), "-map", "[vout]"]
    if audio_idx is not None:
        args += ["-map", f"{audio_idx}:a?", "-c:a", "aac", "-b:a", "256k"]
    if codec == "prores_ks":
        args += ["-c:v", "prores_ks", "-profile:v", str(o["profile"]),
                 "-vendor", "apl0", "-pix_fmt", "yuv422p10le"]
    else:
        args += ["-c:v", codec, "-crf", str(o["crf"]),
                 "-preset", o["preset"], "-pix_fmt", "yuv420p"]
    args += ["-color_primaries", "bt709", "-color_trc", "bt709",
             "-colorspace", "bt709", "-progress", "pipe:1", "-nostats",
             str(out_path)]
    return args


def _gpu_config(cfg: dict) -> dict:
    """What the browser is asked to render.

    Grain is never on the GPU (the shader port has no noise stage). When grain
    is on, detail and letterbox come off the GPU too, because ffmpeg has to run
    them after the grain blend to keep the engine's order of operations.
    """
    from copy import deepcopy
    out = deepcopy(cfg)
    out["grain"] = dict(out["grain"], enabled=False)
    if cfg["grain"]["enabled"]:
        out["detail"] = dict(out["detail"], sharpen=0.0, soften=0.0)
        out["letterbox"] = dict(out["letterbox"], enabled=False)
    return out


# --------------------------------------------------------------------------
# the render itself
# --------------------------------------------------------------------------

# How many frames may sit in memory at once, on each side of the browser. A
# 4K frame is 50 MB of planar 16 bit, so this is a byte budget rather than a
# frame count: enough overlap for decode, render and encode to run at the same
# time, not enough to swap the machine.
INFLIGHT_BYTES = 150_000_000
INFLIGHT_MIN = 2
INFLIGHT_MAX = 6


def _read_pipe(stream, size: int) -> bytes | None:
    """Exactly `size` bytes from an unbuffered pipe, or None at the end.

    Popen with bufsize=0 hands back a RAW stream, and a raw read on a pipe
    returns whatever the kernel has (64 KB at a time here), not what was
    asked for. Treating a short read as the end of the stream is how the
    first version of this decoded exactly zero frames of a 4.4 MB frame.
    """
    buf = bytearray(size)
    view = memoryview(buf)
    got = 0
    while got < size:
        n = stream.readinto(view[got:])
        if not n:
            return None
        got += n
    # The bytearray itself, not a bytes() copy of it: at 4K that copy is
    # another 50 MB memcpy per frame for nothing. Nobody mutates it after
    # this point.
    return buf


class _Session:
    """One GPU render: two ffmpeg processes, one Chrome, and the frames."""

    def __init__(self, job, plan: dict):
        self.job = job
        self.plan = plan
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Condition()
        self.pending: deque = deque()      # (index, bytes) waiting for a GET
        self.outbuf: dict[int, bytes] = {}  # index -> bytes waiting for stdin
        self.next_out = 0                  # index the encoder wants next
        self.decoded = 0
        self.written = 0
        self.total = None                  # set when the decoder ends
        self.failed = None
        self.decode = None
        self.encode = None
        self.worker = None
        self.worker_log: list[str] = []
        self.encode_err: list[str] = []
        self.decode_err: list[str] = []
        self.renderer = ""
        self.started = time.time()
        self.first_frame_at = None
        window = int(INFLIGHT_BYTES
                     // max(1, plan["src_bytes"] + plan["out_bytes"]))
        self.window = max(INFLIGHT_MIN, min(INFLIGHT_MAX, window))

    # --- the frame queue ---------------------------------------------

    def take_frame(self, timeout: float = 30.0):
        """The next undelivered source frame, or None when there are no more."""
        deadline = time.time() + timeout
        with self.lock:
            while True:
                if self.failed or self.job.status in ("cancelled", "failed"):
                    return None
                if self.pending:
                    item = self.pending.popleft()
                    self.lock.notify_all()
                    return item
                if self.total is not None and self.decoded >= self.total:
                    return None
                left = deadline - time.time()
                if left <= 0:
                    return None
                self.lock.wait(min(left, 1.0))

    def put_frame(self, index: int, data: bytes) -> None:
        with self.lock:
            # Backpressure: a page that renders faster than the encoder writes
            # would otherwise pile finished frames up in memory. Blocking the
            # POST here is what makes the browser wait instead.
            while (len(self.outbuf) >= self.window
                   and self.job.status == "running" and not self.failed):
                self.lock.wait(1.0)
            self.outbuf[index] = data
            self.lock.notify_all()

    def fail(self, message: str) -> None:
        with self.lock:
            if not self.failed:
                self.failed = message
            self.lock.notify_all()

    # --- threads -----------------------------------------------------

    def _read_decode(self) -> None:
        size = self.plan["src_bytes"]
        idx = 0
        try:
            while True:
                if self.job.status != "running" or self.failed:
                    break
                with self.lock:
                    while (len(self.pending) >= self.window
                           and self.job.status == "running" and not self.failed):
                        self.lock.wait(1.0)
                    if self.job.status != "running" or self.failed:
                        break
                buf = _read_pipe(self.decode.stdout, size)
                if buf is None:
                    break
                with self.lock:
                    self.pending.append((idx, buf))
                    self.decoded = idx + 1
                    self.lock.notify_all()
                idx += 1
        except Exception as exc:                              # noqa: BLE001
            self.fail(f"decode read failed: {exc}")
        finally:
            with self.lock:
                self.total = idx
                self.lock.notify_all()

    def _write_encode(self) -> None:
        try:
            while True:
                with self.lock:
                    while True:
                        if self.failed or self.job.status != "running":
                            return
                        if self.next_out in self.outbuf:
                            break
                        if (self.total is not None
                                and self.next_out >= self.total):
                            return
                        self.lock.wait(1.0)
                    data = self.outbuf.pop(self.next_out)
                    self.next_out += 1
                    self.lock.notify_all()
                self.encode.stdin.write(data)
                with self.lock:
                    self.written += 1
                    if self.first_frame_at is None:
                        self.first_frame_at = time.time()
                total = self.plan["expected"] or 1
                self.job.progress = min(0.999, self.written / float(total))
                self.job.message = f"{self.written} of {total} frames"
        except (BrokenPipeError, ValueError) as exc:
            self.fail(f"the encoder stopped accepting frames: {exc}")
        except Exception as exc:                              # noqa: BLE001
            self.fail(f"encode write failed: {exc}")

    def _drain(self, stream, sink: list, limit: int = 60) -> None:
        try:
            for line in stream:
                text = line.decode("utf-8", "replace").rstrip() if isinstance(line, bytes) else line.rstrip()
                if not text:
                    continue
                sink.append(text)
                if len(sink) > limit:
                    del sink[0]
        except Exception:                                     # noqa: BLE001
            pass

    def _watch_worker(self) -> None:
        for raw in self.worker.stdout:
            line = raw.rstrip()
            if not line:
                continue
            self.worker_log.append(line)
            if len(self.worker_log) > 80:
                del self.worker_log[0]
            if line.startswith("{"):
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("renderer"):
                    self.renderer = str(msg["renderer"])
                    self.job.log.append("renderer: " + self.renderer)
                if msg.get("error"):
                    self.fail("browser: " + str(msg["error"])[:400])
            else:
                self.job.log.append(line[:200])

    # --- the run -----------------------------------------------------

    def run(self) -> None:
        job, plan = self.job, self.plan
        out_path = Path(job.output)
        try:
            self.decode = subprocess.Popen(
                plan["decode_args"], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0)
            self.encode = subprocess.Popen(
                plan["encode_args"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            # The cancel route signals job.proc, so it has to be the encoder:
            # SIGINT there ends the file the same way it does on the ffmpeg
            # path, and the supervisor loop below tears down the rest.
            job.proc = self.encode
            threads = [
                threading.Thread(target=self._read_decode, daemon=True),
                threading.Thread(target=self._write_encode, daemon=True),
                threading.Thread(target=self._drain,
                                 args=(self.decode.stderr, self.decode_err), daemon=True),
                threading.Thread(target=self._drain,
                                 args=(self.encode.stderr, self.encode_err), daemon=True),
                threading.Thread(target=self._drain,
                                 args=(self.encode.stdout, []), daemon=True),
            ]
            for t in threads:
                t.start()

            self.worker = subprocess.Popen(
                plan["worker_args"], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
                env=dict(os.environ, STUDIO_RENDER_TOKEN=self.token))
            threading.Thread(target=self._watch_worker, daemon=True).start()

            while True:
                code = self.worker.poll()
                if code is not None:
                    break
                if job.status == "cancelled" or self.failed:
                    self.worker.terminate()
                    try:
                        self.worker.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self.worker.kill()
                    break
                time.sleep(0.2)

            # The worker is gone: no more frames will arrive. Let the writer
            # drain whatever is already in hand, then close the encoder.
            for _ in range(300):
                with self.lock:
                    done = (self.total is not None
                            and self.next_out >= self.total) or self.failed
                if done or job.status != "running":
                    break
                time.sleep(0.1)
            with self.lock:
                self.lock.notify_all()
            try:
                self.encode.stdin.close()
            except Exception:                                 # noqa: BLE001
                pass
            if self.decode.poll() is None:
                self.decode.terminate()
            self.encode.wait(timeout=600)

            code = self.worker.returncode
            worker_tail = "\n".join(self.worker_log[-6:])
            if job.status == "cancelled":
                out_path.unlink(missing_ok=True)
                job.message = "cancelled"
            elif self.failed:
                job.status = "failed"
                job.message = self.failed[:600]
            elif code not in (0, None):
                job.status = "failed"
                job.message = ("the render worker exited " + str(code) + ": "
                               + worker_tail)[-600:]
            elif self.encode.returncode != 0:
                job.status = "failed"
                job.message = ("ffmpeg encode failed: "
                               + "\n".join(self.encode_err[-6:]))[-600:]
            elif self.written == 0:
                job.status = "failed"
                job.message = ("no frames reached the encoder. "
                               + " ".join(self.decode_err[-3:])
                               + " " + worker_tail)[-600:]
            else:
                job.status = "done"
                job.progress = 1.0
                size = out_path.stat().st_size if out_path.exists() else 0
                secs = max(1e-6, time.time() - (self.first_frame_at or self.started))
                job.message = (f"{size / 1e6:.1f} MB, {self.written} frames, "
                               f"{self.written / secs:.2f} fps"
                               + (f", {self.renderer}" if self.renderer else ""))
        except Exception as exc:                              # noqa: BLE001
            job.status = "failed"
            job.message = f"{type(exc).__name__}: {exc}"
        finally:
            for proc in (self.decode, self.encode, self.worker):
                try:
                    if proc and proc.poll() is None:
                        proc.kill()
                except Exception:                             # noqa: BLE001
                    pass
            job.finished = time.time()
            with SESSIONS_LOCK:
                SESSIONS.pop(job.id, None)
            self.token = ""


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def start_gpu_render(payload: dict, base_url: str = ""):
    """Same payload as start_render, plus engine="gpu". Returns a Job."""
    clip = payload["clip"]
    cfg = SRV.full_config(payload.get("config"))
    autorotate = bool(payload.get("autorotate", True))
    info = SRV.clip_info(clip, autorotate)
    info = dict(info, duration=float(info.get("duration") or 0.0),
                autorotate=autorotate)
    start = float(payload.get("start") or 0.0)
    duration = payload.get("duration")
    duration = float(duration) if duration not in (None, "") else None
    scale = payload.get("scale")
    name = SRV.safe_name(payload.get("name") or f"studio_gpu_{int(time.time())}")
    codec = cfg["output"]["codec"]
    ext = ".mov" if codec == "prores_ks" else ".mp4"
    out_path = SRV.OUT / (name + ext)

    rcfg, rinfo, scaled = cfg, info, False
    if scale and int(scale) < info["width"]:
        width = int(scale) // 2 * 2
        factor = width / float(info["width"])
        height = max(2, int(round(info["height"] * factor / 2)) * 2)
        rinfo = dict(info, width=width, height=height)
        rcfg = SRV.scale_for_preview(cfg, factor)
        scaled = True

    # The same door every other render goes through, so an impossible config
    # fails here with the engine's own message instead of inside a browser.
    SRV.CG.check_source_space(rcfg, rinfo)

    job = SRV._register(SRV.Job("render", f"{name}{ext}"))
    job.output = str(out_path)
    job.log.append("engine: gpu (headless Chrome)")

    ready = availability()
    if not ready["available"]:
        job.status = "failed"
        job.finished = time.time()
        job.message = ("the GPU render engine needs Chrome and node on this "
                       "machine: " + "; ".join(ready["reasons"])
                       + ". The ffmpeg engine still works.")
        return job

    W, H = rinfo["width"], rinfo["height"]
    gpu_cfg = _gpu_config(cfg)
    cut = _chain_cut(gpu_cfg)
    fps = SRV._proxy_fps(clip, info)
    span = duration if duration else max(0.0, info["duration"] - start)
    expected = max(1, int(round(span * fps))) if span else 1
    plan = {
        "clip": clip,
        "width": W, "height": H,
        "src_bytes": W * H * 3 * 2,          # rgb48le, into the browser
        "out_bytes": W * H * cut["bytes"],   # cut["format"], back out of it
        "out_format": cut["format"],
        "expected": expected,
        "pixel_scale": W / float(info["width"]),
        "config": gpu_cfg,
        "defaults": SRV.CG.DEFAULTS,
        "decode_args": _decode_args(str(SRV.clip_path(clip)), cfg, info, rinfo,
                                    start if start else None, duration, scaled),
        "encode_args": _encode_args(out_path, str(SRV.clip_path(clip)), cfg,
                                    rcfg, info, rinfo,
                                    start if start else None, duration, fps,
                                    bool(payload.get("no_audio")), cut),
    }
    sess = _Session(job, plan)
    port = getattr(SRV, "SERVER_PORT", 0) or int(payload.get("port") or 0)
    base = base_url or f"http://127.0.0.1:{port}"
    plan["worker_args"] = [
        ready["node"], str(WORKER_SCRIPT),
        "--base", base, "--job", job.id,
        "--chrome", ready["chrome"],
    ]
    if payload.get("headful"):
        plan["worker_args"].append("--headful")

    with SESSIONS_LOCK:
        SESSIONS[job.id] = sess
    threading.Thread(target=sess.run, daemon=True).start()
    return job


# --------------------------------------------------------------------------
# the three worker routes
# --------------------------------------------------------------------------

def handle(handler, method: str, route: str, query: dict) -> bool:
    """The render worker's routes. Returns True when it answered."""
    if not route.startswith("render/gpu/"):
        return False
    sess = _session_for_token(_bearer(handler))
    addr = handler.client_address[0] if handler.client_address else ""
    if sess is None or not _is_loopback(addr):
        handler._json({"error": "this route belongs to a running GPU render "
                                "worker and needs that render's own token"}, 403)
        return True
    if query.get("job") and query["job"] != sess.job.id:
        handler._json({"error": "that token is for a different render"}, 403)
        return True

    if route == "render/gpu/plan" and method == "GET":
        plan = sess.plan
        handler._json({
            "job": sess.job.id,
            "width": plan["width"], "height": plan["height"],
            "frameBytes": plan["src_bytes"],
            "outBytes": plan["out_bytes"],
            "outFormat": plan["out_format"],
            "expected": plan["expected"],
            "pixelScale": plan["pixel_scale"],
            "config": plan["config"],
            "defaults": plan["defaults"],
            "window": sess.window,
        })
        return True

    if route == "render/gpu/next" and method == "GET":
        item = sess.take_frame()
        if item is None:
            handler._send(204, b"", "application/octet-stream")
            return True
        index, data = item
        handler._send(200, data, "application/octet-stream", {
            "X-Frame-Index": str(index),
            "X-Frame-Size": f"{sess.plan['width']}x{sess.plan['height']}",
        })
        return True

    if route == "render/gpu/frame" and method == "POST":
        try:
            index = int(query.get("i", "-1"))
        except ValueError:
            index = -1
        length = int(handler.headers.get("Content-Length") or 0)
        want = sess.plan["out_bytes"]
        sent_format = handler.headers.get("X-Frame-Format", "")
        if index < 0 or length != want or sent_format != sess.plan["out_format"]:
            # Read and drop the body first: leaving it on the socket would
            # make the next request start reading half a frame.
            _drain_body(handler, length)
            handler._json({"error": f"frame {index} should be {want} bytes of "
                                    f"{sess.plan['out_format']}, got {length} "
                                    f"bytes of {sent_format or 'no format'}"}, 400)
            return True
        data = _read_exact(handler, length)
        if data is None:
            handler._json({"error": "the frame body ended early"}, 400)
            return True
        sess.put_frame(index, data)
        handler._json({"ok": True, "index": index})
        return True

    handler._json({"error": "no such render worker route"}, 404)
    return True


def _read_exact(handler, length: int) -> bytes | None:
    chunks, got = [], 0
    while got < length:
        buf = handler.rfile.read(min(1 << 20, length - got))
        if not buf:
            return None
        chunks.append(buf)
        got += len(buf)
    return b"".join(chunks)


def _drain_body(handler, length: int) -> None:
    got = 0
    while got < length:
        buf = handler.rfile.read(min(1 << 20, length - got))
        if not buf:
            return
        got += len(buf)
