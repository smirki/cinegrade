"""Shared bits for the service's tests.

Same shape as the studio's own python tests: plain scripts with a `check()`
that prints a line per assertion and collects failures, no test framework and
therefore no extra dependency in the service's uv project.

Every server started here runs on a random high port with its own temporary
data directory, and is stopped by the process id captured at spawn. Ports
7431, 7614, 7615 and 7560 are never used by a test.
"""

from __future__ import annotations

import json
import os
import random
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
SAM = HERE.parent
ROOT = SAM.parent
# The project's own interpreter, so a test never depends on which shell it was
# started from. `uv run --project sam` creates and fills this.
PYTHON = SAM / ".venv" / "bin" / "python"
FORBIDDEN_PORTS = {7431, 7560, 7614, 7615}

FAILED: list[str] = []
STEPS = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global STEPS
    STEPS += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"   {detail}" if detail else ""),
          flush=True)
    if not ok:
        FAILED.append(label + (f" ({detail})" if detail else ""))
    return ok


def free_port() -> int:
    for _ in range(200):
        port = random.randint(20000, 60000)
        if port in FORBIDDEN_PORTS:
            continue
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise SystemExit("no free port")


def call(base: str, path: str, payload=None, method=None, timeout=120):
    url = base + path
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            out = json.loads(response.read().decode())
            out["status"] = response.status
            return out
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            out = json.loads(body)
        except json.JSONDecodeError:
            out = {"error": body[:300]}
        out["status"] = exc.code
        return out


def start(port: int, data_dir: Path, extra: list[str] | None = None,
          backend: str | None = None, ready_s: float = 40):
    """Start the service and wait for /health. Returns (process, base url).

    With no `backend` it is the stub: no weights, no model lock, instant. Pass
    a backend name ("mlx", "auto", "torch-cpu") for the real model test, which
    keeps the machine wide lock on because the point of that lock is that only
    one process on this Mac holds the model at a time. The port only starts
    answering once the model is loaded, so `ready_s` has to cover the load.
    """
    if not PYTHON.is_file():
        raise SystemExit(f"{PYTHON} is missing: run `uv sync --project sam` first")
    command = [str(PYTHON), str(SAM / "server.py"), "--port", str(port),
               "--data-dir", str(data_dir)]
    command += (["--backend", backend, "--require-backend"] if backend
                else ["--stub", "--no-model-lock"])
    command += list(extra or [])
    proc = subprocess.Popen(command, cwd=str(ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + ready_s
    while time.time() < deadline:
        if proc.poll() is not None:
            print(proc.stdout.read())
            raise SystemExit("the service exited before it was ready")
        try:
            urllib.request.urlopen(base + "/health", timeout=2).read()
            return proc, base
        except Exception:                                      # noqa: BLE001
            time.sleep(0.2)
    stop(proc)
    try:
        print(proc.stdout.read()[-4000:])
    except Exception:                                          # noqa: BLE001
        pass
    raise SystemExit("the service never answered /health")


def stop(proc) -> None:
    """By the process id captured at spawn, and nothing else."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def wait_for_job(base: str, job_id: str, timeout_s: float = 60,
                 states=("done", "failed", "cancelled")) -> dict:
    deadline = time.time() + timeout_s
    job = {}
    while time.time() < deadline:
        job = call(base, f"/jobs/{job_id}")
        if job.get("state") in states:
            return job
        time.sleep(0.1)
    return job


def make_frames(directory: Path, count: int = 24, width: int = 160,
                height: int = 90) -> Path:
    """A frame directory that looks like a clip: something moves in it, so a
    real backend pointed at it would have something to find."""
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        frame = np.zeros((height, width, 3), dtype=np.uint8)
        frame[:, :, 2] = 40
        x = int((index / max(1, count - 1)) * (width - 20))
        frame[height // 3: 2 * height // 3, x:x + 20] = (230, 210, 190)
        Image.fromarray(frame).save(directory / f"{index:04d}.png")
    return directory


def make_video(path: Path, seconds: int = 1, fps: int = 24,
               size: str = "160x90") -> Path:
    """A tiny clip through one bounded ffmpeg call, for the decode path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"testsrc=size={size}:rate={fps}",
                    "-t", str(seconds), str(path)],
                   check=True, capture_output=True, timeout=120)
    return path


def report(name: str) -> int:
    print(f"\n{name}: {STEPS - len(FAILED)}/{STEPS} checks passed")
    if FAILED:
        for line in FAILED:
            print(f"  FAILED: {line}")
        return 1
    return 0


def load_mask(path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("L"), dtype=np.float32) / 255.0


def centroid(mask: np.ndarray) -> tuple[float, float]:
    total = mask.sum()
    if total <= 0:
        return (0.0, 0.0)
    ys, xs = np.nonzero(mask > 0.5)
    if len(xs) == 0:
        return (0.0, 0.0)
    return (float(xs.mean()) / mask.shape[1], float(ys.mean()) / mask.shape[0])


def sam_path() -> None:
    """Let a test import the service's own modules."""
    if str(SAM) not in sys.path:
        sys.path.insert(0, str(SAM))
    os.environ.setdefault("PYTHONUNBUFFERED", "1")

