#!/usr/bin/env python3
"""The real model memory verification: three windows of 48 frames on C015.

    uv run --project sam python sam/spike/track_memory.py

Run this when the machine wide model lock is FREE. It takes that lock itself,
for the whole run, exactly the way the service does, and gives it back at the
end (including on failure and on Ctrl-C), so no second SAM model can load
underneath it. If the lock is held it says who holds it and exits 3 without
waiting: two models on a 16 GB Mac do not fail cleanly, they swap until
everything on the machine crawls.

What it does:

* starts a SAM service on a random free port, with the MLX backend and the
  memory flags under test, and its own data directory under `spike/out/`
* makes a plain Rec.709 proxy of `A001_09061900_C015.mov` at 720p, in one
  bounded ffmpeg pass, the same normalisation the studio's own mask proxy
  uses. The camera original is 4K Apple Log in bt2020nc: fed to the detector
  raw it finds nothing at all ("0 instances" for "person"), which is a real
  trap and not a memory problem, so this script never tracks the original
* tracks 142 frames of that proxy with the text prompt "person": with
  `--chunk-frames 48` and the one frame overlap that carries an object across
  a seam, that is exactly three full windows
* after every window closes, samples `top -l 1 -stats pid,mem,cmprs -pid <pid>`,
  the same command a person would run, and reads the service's own /health
* prints a table and a verdict, writes the whole run to `result.json`
* stops the service by the process id it started, and nothing else

The verdict is about the SHAPE, not the size: bounded memory is a flat peak
across windows, a leak is a rising one. A single big number proves nothing on
its own, because the model itself is about 1.7 GB and a window's live set is
another gigabyte.

To compare against the old behaviour, run it twice:

    ... track_memory.py --tag fixed
    ... track_memory.py --tag mlx-default --cache-limit-mb -1

`-1` leaves MLX's own cache limit, which on this Mac is 15564.8 MB, i.e. the
setting that let the service reach 13 GB in the first place.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SAM = Path(__file__).resolve().parent.parent
ROOT = SAM.parent
if str(SAM) not in sys.path:
    sys.path.insert(0, str(SAM))

import memstat                                                     # noqa: E402
from modellock import ModelLock, owner                             # noqa: E402

PYTHON = SAM / ".venv" / "bin" / "python"
OUT = SAM / "spike" / "out"
CLIP = Path("/Users/smirk/Programming/Fixxr-Agent-Workspace/content/"
            "bakeoff/r2/sources/A001_09061900_C015.mov")
# Ports that belong to somebody else's running server and are never taken. One
# list, in sam/ports.py, read by every script and suite that picks a port: this
# one used to keep its own copy and the four copies in the tree disagreed
# (round 1 finding 20).
from ports import FORBIDDEN_PORTS, PORT_RANGE                      # noqa: E402
# `taskpolicy -b` runs a program under the macOS background QoS tier, which is
# what the OS itself uses for Spotlight indexing and Time Machine: throttled
# CPU, throttled disk, and a lower GPU priority. It is a launch wrapper, not a
# code path, which is why it lives in this script and not in the service.
TASKPOLICY = "/usr/sbin/taskpolicy"


def say(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


def free_port() -> int:
    for _ in range(400):
        port = random.randint(*PORT_RANGE)
        if port in FORBIDDEN_PORTS:
            continue
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise SystemExit("no free port")


def get(base: str, path: str, timeout: float = 20) -> dict:
    with urllib.request.urlopen(base + path, timeout=timeout) as response:
        return json.loads(response.read().decode())


def post(base: str, path: str, payload: dict, timeout: float = 60) -> dict:
    request = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": exc.read().decode()[:500], "status": exc.code}


# ---------------------------------------------------------------------------
# The Mac's own numbers
# ---------------------------------------------------------------------------

_SIZE = re.compile(r"^([0-9.]+)([BKMGT]?)[+-]?$")
_SCALE = {"": 1 / 1048576, "B": 1 / 1048576, "K": 1 / 1024, "M": 1.0,
          "G": 1024.0, "T": 1048576.0}


def _mb(token: str) -> float | None:
    match = _SIZE.match(token.strip())
    if not match:
        return None
    return round(float(match.group(1)) * _SCALE.get(match.group(2), 1.0), 1)


def top_sample(pid: int) -> dict:
    """`top -l 1 -stats pid,mem,cmprs -pid <pid>`, parsed.

    The same command a person would run, on purpose: the point of this script
    is that its numbers can be checked by hand against the tool the founder
    already used to find the problem.
    """
    command = ["top", "-l", "1", "-stats", "pid,mem,cmprs", "-pid", str(pid)]
    out = {"mem_mb": None, "cmprs_mb": None, "raw": None}
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except Exception as exc:                                       # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    for row in reversed(done.stdout.splitlines()):
        fields = row.split()
        if len(fields) >= 3 and fields[0] == str(pid):
            out["raw"] = row.strip()
            out["mem_mb"] = _mb(fields[1])
            out["cmprs_mb"] = _mb(fields[2])
            break
    return out


def load_average() -> list[float]:
    """The 1, 5 and 15 minute load averages, the same three numbers `uptime`
    prints. This is the "is the machine usable" reading, and it is the point of
    quiet mode: the service's own footprint can be perfect while the machine is
    unusable, and only this number and the founder's own scroll say so."""
    try:
        return [round(value, 2) for value in os.getloadavg()]
    except OSError:                                                # noqa: BLE001
        return []


def sample(pid: int, base: str | None) -> dict:
    """One reading of everything: top, the kernel's own footprint for that
    pid, the machine's load average, and what the service says about itself."""
    shot = {"at": round(time.time(), 3), "top": top_sample(pid),
            "load": load_average()}
    footprint = memstat.pid_footprint(pid)
    shot["footprint_mb"] = round(footprint / 1048576, 1) if footprint else None
    if base:
        try:
            health = get(base, "/health")
            shot["health_memory"] = health.get("memory")
            shot["window"] = health.get("window")
            shot["throttle"] = health.get("throttle")
        except Exception as exc:                                   # noqa: BLE001
            shot["health_error"] = f"{type(exc).__name__}: {exc}"
    return shot


# ---------------------------------------------------------------------------
# The picture
# ---------------------------------------------------------------------------


def probe(clip: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,color_space,color_range,avg_frame_rate",
         "-of", "json", str(clip)],
        capture_output=True, text=True, timeout=60)
    stream = json.loads(out.stdout)["streams"][0]
    rate = stream.get("avg_frame_rate") or "24/1"
    num, den = (rate.split("/") + ["1"])[:2]
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "matrix": stream.get("color_space") or "bt709",
        "range": stream.get("color_range") or "tv",
        "fps": (float(num) / float(den)) if float(den) else 24.0,
    }


def make_proxy(clip: Path, out_path: Path, width: int, frames: int) -> Path:
    """One bounded ffmpeg pass: the camera original into plain Rec.709.

    The same normalisation `studio/server.py::_mask_proxy_ffmpeg_args` does,
    written out here rather than imported so this script depends on nothing
    but ffmpeg. Bounded by `-t` like every ffmpeg call in this codebase, and
    only as long as the frames actually being tracked.
    """
    info = probe(clip)
    height = int(round(info["height"] * width / info["width"] / 2) * 2)
    duration = frames / (info["fps"] or 24.0) + 1.0
    vf = (f"scale={width}:{height}:flags=bilinear,setsar=1,"
          f"scale=in_color_matrix={info['matrix']}:in_range={info['range']}"
          f":out_range=full,format=gbrp16le,"
          f"scale=out_color_matrix=bt709:out_range=limited,format=yuv420p")
    command = ["ffmpeg", "-v", "error", "-y", "-i", str(clip),
               "-t", f"{duration:.3f}", "-vf", vf, "-an", "-sn", "-dn",
               "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
               "-g", "48", "-keyint_min", "48", "-sc_threshold", "0",
               "-pix_fmt", "yuv420p", "-color_primaries", "bt709",
               "-color_trc", "bt709", "-colorspace", "bt709",
               "-color_range", "tv", "-movflags", "+faststart", str(out_path)]
    say(f"making a {width}x{height} Rec.709 proxy of {clip.name} "
        f"({duration:.1f}s from {info['matrix']}/{info['range']})")
    began = time.time()
    done = subprocess.run(command, capture_output=True, text=True, timeout=900)
    if done.returncode != 0 or not out_path.is_file():
        raise SystemExit(f"the proxy pass failed: {done.stderr.strip()[:400]}")
    say(f"proxy ready in {time.time() - began:.1f}s: {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def start_service(args, port: int, data_dir: Path, log_path: Path):
    command = [str(PYTHON), str(SAM / "server.py"),
               "--port", str(port), "--host", "127.0.0.1",
               "--backend", "mlx", "--require-backend",
               "--no-model-lock",              # this script holds it instead
               "--data-dir", str(data_dir),
               "--chunk-frames", str(args.chunk_frames)]
    if args.cache_limit_mb is not None:
        command += ["--mlx-cache-limit-mb", str(args.cache_limit_mb)]
    if args.memory_limit_mb is not None:
        command += ["--mlx-memory-limit-mb", str(args.memory_limit_mb)]
    if args.attention_chunk is not None:
        command += ["--mlx-attention-chunk", str(args.attention_chunk)]
    if args.layer_eval:
        command += ["--mlx-layer-eval"]
    if args.quiet:
        command += ["--quiet"]
    if args.duty_cycle is not None:
        command += ["--duty-cycle", str(args.duty_cycle)]
    if args.nice is not None:
        command += ["--nice", str(args.nice)]
    if args.taskpolicy:
        # In front of the interpreter, so the whole service (and therefore its
        # Metal work) inherits the background tier. `taskpolicy` execs in place,
        # so proc.pid is still the python process and stop_service still stops
        # the thing it started rather than a wrapper.
        if not Path(TASKPOLICY).exists():
            raise SystemExit(f"{TASKPOLICY} is missing; --taskpolicy needs it")
        command = [TASKPOLICY, "-b"] + command
    say("starting: " + " ".join(command))
    handle = log_path.open("w")
    proc = subprocess.Popen(command, cwd=str(ROOT), stdout=handle,
                            stderr=subprocess.STDOUT, text=True)
    return proc, handle


def wait_ready(proc, base: str, ready_s: float) -> None:
    deadline = time.time() + ready_s
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"the service exited with {proc.returncode} before "
                             f"it was ready; read the log")
        try:
            get(base, "/health", timeout=5)
            return
        except Exception:                                          # noqa: BLE001
            time.sleep(1.0)
    raise SystemExit(f"the service never answered /health within {ready_s:.0f}s")


def stop_service(proc, handle) -> None:
    """By the process id this script started, and nothing else. No pkill,
    no killall, no pattern matching: other agents run servers on this Mac."""
    if proc.poll() is None:
        say(f"stopping the service, pid {proc.pid}")
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=30)
    try:
        handle.close()
    except Exception:                                              # noqa: BLE001
        pass


def table(rows: list[dict]) -> str:
    head = (f"{'window':>6}  {'frames':>12}  {'took':>8}  "
            f"{'top MEM':>9}  {'top CMPRS':>10}  {'svc peak':>9}  "
            f"{'proc peak':>10}  {'mlx peak':>9}  "
            f"{'after free':>10}  {'mlx active':>10}  {'mlx cache':>9}")
    lines = [head, "-" * len(head)]
    for row in rows:
        def show(value, unit=" MB"):
            return "?" if value is None else f"{value:.0f}{unit}"

        lines.append(
            f"{str(row.get('window')):>6}  "
            f"{str(row.get('frames')):>12}  "
            f"{show(row.get('elapsed_s'), ' s'):>8}  "
            f"{show(row.get('top_mem_mb')):>9}  "
            f"{show(row.get('top_cmprs_mb')):>10}  "
            f"{show(row.get('peak_footprint_mb')):>9}  "
            f"{show(row.get('process_peak_mb')):>10}  "
            f"{show(row.get('mlx_peak_mb')):>9}  "
            f"{show(row.get('footprint_after_mb')):>10}  "
            f"{show(row.get('mlx_active_mb')):>10}  "
            f"{show(row.get('mlx_cache_mb')):>9}")
    return "\n".join(lines)


def row_from(window: dict, shot: dict) -> dict:
    before = window.get("mlx_before") or {}
    health = shot.get("health_memory") or {}
    return {
        # The process high water mark, which is the number that decides
        # whether a 16 GB Mac swaps. The window's own peak below is sampled
        # once per frame and misses a transient that lives inside one frame;
        # this one cannot miss it, because the kernel keeps it.
        "process_peak_mb": health.get("peak_footprint_mb"),
        "window": window.get("window_index"),
        "frames": f"{window.get('start')}..{window.get('end')}",
        "elapsed_s": window.get("elapsed_s"),
        "peak_footprint_mb": window.get("peak_footprint_mb"),
        "footprint_after_mb": window.get("footprint_after_mb"),
        "mlx_active_mb": before.get("active_mb"),
        "mlx_cache_mb": before.get("cache_mb"),
        "mlx_peak_mb": before.get("peak_mb"),
        "top_mem_mb": (shot.get("top") or {}).get("mem_mb"),
        "top_cmprs_mb": (shot.get("top") or {}).get("cmprs_mb"),
        "top_raw": (shot.get("top") or {}).get("raw"),
        "kernel_footprint_mb": shot.get("footprint_mb"),
        "load": shot.get("load"),
        "busy_fraction": (shot.get("throttle") or {}).get("busy_fraction"),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--clip", default=str(CLIP))
    parser.add_argument("--proxy", default=None,
                        help="a plain Rec.709 proxy to track. Made from --clip "
                             "when not given.")
    parser.add_argument("--width", type=int, default=1280,
                        help="the studio's mask working width. 1280 makes a "
                             "1280x720 proxy, which is what '720' means here "
                             "and what the studio itself tracks.")
    parser.add_argument("--chunk-frames", type=int, default=48)
    parser.add_argument("--windows", type=int, default=3,
                        help="how many full windows to track (default 3)")
    parser.add_argument("--text", default="person")
    parser.add_argument("--cache-limit-mb", type=int, default=None,
                        help="passed to the service. -1 leaves MLX's own "
                             "default, which is the unfixed behaviour")
    parser.add_argument("--memory-limit-mb", type=int, default=None)
    parser.add_argument("--attention-chunk", type=int, default=None,
                        help="queries per block in the tracker's memory "
                             "attention. 0 runs it whole, which is the "
                             "behaviour that made every frame peak at 13.2 GB.")
    parser.add_argument("--layer-eval", action="store_true",
                        help="also put an mx.eval after every ViT trunk layer")
    parser.add_argument("--quiet", action="store_true",
                        help="run the service under its own --quiet preset "
                             "(duty cycle 0.5, nice 15, MLX memory limit "
                             "6144 MB, mask width hint 720)")
    parser.add_argument("--duty-cycle", type=float, default=None,
                        help="the fraction of wall time the model may own; "
                             "overrides --quiet's 0.5")
    parser.add_argument("--nice", type=int, default=None)
    parser.add_argument("--taskpolicy", action="store_true",
                        help="launch the service under `taskpolicy -b`, the "
                             "macOS background QoS tier. A launch wrapper, "
                             "never a code path: this measures it rather than "
                             "adopting it.")
    parser.add_argument("--lock-timeout-s", type=float, default=60.0,
                        help="give up rather than wait for the model lock")
    parser.add_argument("--ready-s", type=float, default=900.0,
                        help="the model load can take minutes on a busy Mac")
    parser.add_argument("--timeout-s", type=float, default=7200.0)
    parser.add_argument("--tag", default="run")
    args = parser.parse_args(argv)

    clip = Path(args.clip)
    if not clip.is_file():
        raise SystemExit(f"there is no clip at {clip}")
    if not PYTHON.is_file():
        raise SystemExit(f"{PYTHON} is missing: run `uv sync --project sam "
                         f"--extra mlx` first")

    # 142 frames with a 48 frame window and a one frame overlap is exactly
    # three windows: 0..48, 47..95, 94..142.
    total_frames = (args.chunk_frames - 1) * args.windows + 1
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = OUT / f"track-memory-{args.tag}-{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "service.log"

    if args.proxy:
        proxy = Path(args.proxy)
        if not proxy.is_file():
            raise SystemExit(f"there is no proxy at {proxy}")
    else:
        proxy = make_proxy(clip, run_dir / "proxy.mp4", args.width,
                           total_frames)

    lock = ModelLock(backend="mlx (spike/track_memory.py)", log=say)
    if not lock.acquire(timeout_s=args.lock_timeout_s):
        info = owner() or {}
        who = (f"pid {info.get('pid')} ({info.get('backend')}, since "
               f"{info.get('since')})") if info else \
            "a process that left no owner file"
        say(f"the machine wide model lock is held by {who}. Nothing was "
            f"started and nothing was killed. Run this when it is free.")
        return 3

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    proc = handle = None
    result = {"tag": args.tag, "clip": str(clip), "proxy": str(proxy),
              "width": args.width,
              "chunk_frames": args.chunk_frames, "windows_asked": args.windows,
              "total_frames": total_frames, "port": port,
              "cache_limit_mb": args.cache_limit_mb,
              "memory_limit_mb": args.memory_limit_mb,
              "attention_chunk": args.attention_chunk,
              "layer_eval": bool(args.layer_eval),
              "quiet": bool(args.quiet),
              "duty_cycle": args.duty_cycle,
              "nice": args.nice,
              "taskpolicy": bool(args.taskpolicy),
              "load_before": load_average(),
              "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    say(f"load average before anything started: {result['load_before']}")
    rows: list[dict] = []
    try:
        proc, handle = start_service(args, port, run_dir / "data", log_path)
        result["pid"] = proc.pid
        say(f"waiting for the model to load (pid {proc.pid}, log {log_path})")
        wait_ready(proc, base, args.ready_s)
        health = get(base, "/health")
        result["backend"] = health.get("backend")
        result["limits"] = ((health.get("memory") or {}).get("backend") or {}) \
            .get("limits")
        result["throttle_at_start"] = health.get("throttle")
        say(f"backend {health.get('backend')}, limits {result['limits']}")
        say(f"throttle {result['throttle_at_start']}")

        loaded = sample(proc.pid, base)
        result["after_load"] = loaded
        say(f"model loaded: top MEM {loaded['top'].get('mem_mb')} MB, "
            f"CMPRS {loaded['top'].get('cmprs_mb')} MB")

        job = post(base, "/track", {
            "video": str(proxy),
            "start_frame": 0, "end_frame": total_frames,
            "prompts": {"text": [args.text]}, "max_instances": 1,
            "clip_key": f"track-memory-{stamp}",
            "out_dir": str(run_dir / "mattes"),
        })
        if not job.get("ok"):
            raise SystemExit(f"the track was refused: {job}")
        job_id = job["job_id"]
        say(f"job {job_id}: {total_frames} frames at {args.width} wide, "
            f"prompt {args.text!r}. This is one model pass per frame; on this "
            f"Mac that is minutes per window.")

        seen_windows = 0
        deadline = time.time() + args.timeout_s
        state = "queued"
        while time.time() < deadline:
            time.sleep(5.0)
            try:
                health = get(base, "/health")
            except Exception as exc:                               # noqa: BLE001
                say(f"health did not answer: {exc}")
                continue
            windows = ((health.get("memory") or {}).get("windows") or {})
            count = windows.get("count") or 0
            if count > seen_windows:
                # A window has just closed: take the Mac's own reading now,
                # while the service is between windows.
                shot = sample(proc.pid, base)
                record = windows.get("last") or {}
                rows.append(row_from(record, shot))
                seen_windows = count
                say(f"window {count} done: peak footprint "
                    f"{record.get('peak_footprint_mb')} MB, top MEM "
                    f"{shot['top'].get('mem_mb')} MB, CMPRS "
                    f"{shot['top'].get('cmprs_mb')} MB")
            job_now = get(base, f"/jobs/{job_id}")
            state = job_now.get("state")
            if state in ("done", "failed", "cancelled"):
                result["job"] = job_now
                break
        else:
            result["job"] = {"state": "timed out"}
            say(f"the job did not finish within {args.timeout_s:.0f}s")

        say(f"job {job_id} ended {state}")
        time.sleep(5.0)      # MLX gives the memory back a moment after the free
        result["after_job"] = sample(proc.pid, base)
        result["rows"] = rows
        final = get(base, "/health")
        result["health_final"] = final.get("memory")
        result["throttle_final"] = final.get("throttle")
        result["load_after"] = load_average()
    finally:
        if proc is not None:
            stop_service(proc, handle)
        lock.release()
        say("model lock released")

    print()
    print(f"clip {clip.name} through {proxy.name}, {total_frames} frames at "
          f"{args.width} wide, windows of {args.chunk_frames}, "
          f"prompt {args.text!r}")
    print(f"MLX limits in force: {result.get('limits')}")
    print()
    print(table(rows))
    print()
    after = (result.get("after_load") or {}).get("top", {})
    end = (result.get("after_job") or {}).get("top", {})
    print(f"model loaded, idle:      top MEM {after.get('mem_mb')} MB   "
          f"CMPRS {after.get('cmprs_mb')} MB")
    print(f"after the job and free:  top MEM {end.get('mem_mb')} MB   "
          f"CMPRS {end.get('cmprs_mb')} MB")

    peaks = [row["peak_footprint_mb"] for row in rows
             if row.get("peak_footprint_mb") is not None]
    if len(peaks) >= 2:
        growth = peaks[-1] - peaks[0]
        print(f"\npeak footprint per window: "
              f"{', '.join(f'{p:.0f} MB' for p in peaks)}")
        print(f"first to last: {growth:+.0f} MB. Bounded memory is a flat "
              f"line here; a rising one means a window is still keeping "
              f"something the next one has to live with.")
        result["peak_growth_mb"] = round(growth, 1)
    process = [row["process_peak_mb"] for row in rows
               if row.get("process_peak_mb") is not None]
    mlx_peaks = [row["mlx_peak_mb"] for row in rows
                 if row.get("mlx_peak_mb") is not None]
    if process:
        print(f"process high water mark: {max(process):.0f} MB. This is the "
              f"one a per frame sampler cannot see and the kernel cannot "
              f"forget; on a 16 GB Mac anything near 13 GB is a swap event "
              f"every window.")
        result["process_peak_mb"] = max(process)
    if mlx_peaks:
        print(f"MLX's own peak, worst window: {max(mlx_peaks):.0f} MB.")
        result["mlx_peak_mb"] = max(mlx_peaks)

    quiet = result.get("throttle_final") or {}
    if quiet:
        print(f"\nduty cycle asked for: {quiet.get('duty_cycle')} "
              f"(enabled {quiet.get('enabled')}, nice {quiet.get('nice')})")
        print(f"busy fraction MEASURED: {quiet.get('busy_fraction')} "
              f"over {quiet.get('busy_s')}s busy and {quiet.get('idle_s')}s "
              f"idle in {quiet.get('rests')} rests")
    frames_done = (result.get("job") or {}).get("done_frames") or 0
    busy = quiet.get("busy_s") or 0
    if frames_done and busy:
        result["s_per_frame_busy"] = round(busy / frames_done, 3)
        print(f"per frame, model time only: {result['s_per_frame_busy']} s "
              f"over {frames_done} frames (the number to compare between "
              f"launchers: wall time under a duty cycle is busy plus rest)")
    loads = [result.get("load_before")] + \
        [row.get("load") for row in rows] + [result.get("load_after")]
    print(f"load average before / per window / after: "
          + " | ".join(str(entry) for entry in loads if entry))
    (run_dir / "result.json").write_text(json.dumps(result, indent=2, default=str))
    print(f"\nwritten: {run_dir / 'result.json'}")
    print(f"service log: {log_path}")
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
