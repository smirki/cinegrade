#!/usr/bin/env python3
"""The service's safety rules: the lock's ownership, the store's paths, what
/health says about the model, and the two seams the round 1 review found
untested.

    uv run --project sam python sam/tests/test_safety.py

Everything here runs on the stub or on pure functions. The machine wide lock is
redirected to a temporary directory and asserted NOT to be the real one before a
single check runs, so this file can never take, move or delete
/tmp/fixxr-sam-model.lock while a real model is loaded in another process.

One suite per finding, named in the print line, so a failure says which
behaviour went back:

  finding 11   a disabled lock must not report itself as the holder, and must
               not delete the real holder's lock directory on the way out
  finding 13   a matte id is one path segment, and nothing is written outside
               the root the caller named
  finding 23   MLX's allocator is bounded BEFORE the weights load
  finding 24   the attention wrapper can be turned off and retuned in process
  finding 25   /health carries what the wrapper DID, not what it was asked for
  finding 26   a load that fails after the weights are read still frees them
  finding 27   a failed track closes its window
  finding 28   a failure to free a window is logged, not swallowed
  finding 45   ffmpeg's stderr is never an undrained pipe
  finding 47   loading the torch backend does not disarm the module's lock
  gap 20       a pick seeds a track from its mask where the backend can
  gap 21       one honest state per job, plus which job holds the model
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (call, check, free_port, make_frames, make_video,        # noqa: E402
                    report, sam_path, start, stop, wait_for_job)

sam_path()
import frames as framesmod                                                 # noqa: E402
import modellock                                                           # noqa: E402
import store                                                               # noqa: E402
from backends.base import window_meter                                     # noqa: E402
from backends.stub import FAIL_PHRASE, StubBackend                         # noqa: E402
from backends.torch_adapter import TorchAdapter                            # noqa: E402
from modellock import ModelLock                                            # noqa: E402


def quiet(_message: str) -> None:
    pass


def header(frames_count: int = 4) -> dict:
    return {"clip": "/footage/x.mov", "clip_key": "x", "rotation": 0,
            "fps": 24.0, "frames": frames_count, "start_frame": 0,
            "end_frame": frames_count, "width": 16, "height": 8,
            "recipe": {"kind": "track"}, "state": "queued", "model": "test",
            "backend": "stub", "job_id": "j_test", "object_id": "t0_0",
            "label": "x", "kind": "text"}


# ---------------------------------------------------------------------------
# finding 11: the lock's ownership
# ---------------------------------------------------------------------------

def test_lock_ownership(tmp: Path) -> None:
    print("\nfinding 11: a lock nobody took must not be given away")
    real = modellock.LOCK_DIR
    # Round 2 finding 56: the "the real lock is untouched" check at the end of
    # this test used to read `not LOCK_DIR.exists() or owner()["pid"] != mine`,
    # and its FIRST disjunct passes exactly when the real lock has been
    # DELETED, which is the damage the check is named after. The second passes
    # whenever anything else owns it. So it could only ever go red in a case
    # the suite cannot produce. This is the snapshot the check needed: what
    # was there before the redirect, compared against what is there after.
    # It matters more than its severity suggests, because the founder's live
    # grading run is what holds that lock while this suite runs.
    real_existed = real.exists()
    # Named for the LOCK, not for the function: `real_owner` further down is
    # the saved `modellock.owner` function itself, restored after a monkeypatch.
    real_lock_owner = modellock.owner(real)
    modellock.LOCK_DIR = tmp / "model.lock"
    check("the test is not pointed at the real lock",
          modellock.LOCK_DIR != real and not modellock.LOCK_DIR.exists(),
          str(modellock.LOCK_DIR))

    holder = ModelLock(backend="mlx", retry_s=0.05, log=quiet)
    check("a holder takes it", holder.acquire(timeout_s=2) and holder.held)

    off = ModelLock(enabled=False, log=quiet)
    check("a --no-model-lock service is told to go ahead", off.acquire() is True)
    check("but it does NOT claim to hold the lock, which is what /health "
          "reported before and what made an erased lock invisible",
          off.held is False and off.bypassed is True,
          f"held={off.held} bypassed={off.bypassed}")

    off.release()
    check("and its release leaves the real holder's lock directory alone",
          modellock.LOCK_DIR.is_dir(), str(modellock.LOCK_DIR))
    check("with the real holder's own owner file still in it, naming this pid",
          (modellock.owner() or {}).get("pid") == os.getpid(),
          json.dumps(modellock.owner()))
    check("and the holder still believes it holds it, which is now true",
          holder.held is True)

    # A holder whose owner file names somebody else must not remove it either:
    # that is the same bug from the other side.
    (modellock.LOCK_DIR / "owner.json").write_text(json.dumps(
        {"pid": 1, "backend": "someone-else", "since": "2026-09-09T00:00:00Z"}))
    said: list[str] = []
    stranger = ModelLock(backend="mlx", retry_s=0.05, log=said.append)
    stranger.held = True                      # believes it holds it, wrongly
    stranger.release()
    check("a release whose owner file names another live pid removes nothing",
          modellock.LOCK_DIR.is_dir()
          and (modellock.owner() or {}).get("pid") == 1,
          json.dumps(modellock.owner()))
    check("and says so rather than failing silently",
          any("not releasing" in line for line in said), str(said[-1:]))

    holder.held = True
    (modellock.LOCK_DIR / "owner.json").write_text(json.dumps(
        {"pid": os.getpid(), "backend": "mlx", "since": "now"}))
    holder.release()
    check("the real holder can still let go", not modellock.LOCK_DIR.exists())

    print("\nfinding 11: a reclaim re-reads the owner before it deletes")
    modellock.LOCK_DIR.mkdir(parents=True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (modellock.LOCK_DIR / "owner.json").write_text(json.dumps(
        {"pid": dead.pid, "backend": "mlx", "since": "then"}))
    real_owner = modellock.owner
    reads = {"n": 0}

    def racing_owner(lock_dir=None):
        # First read: the dead holder. Second read (the one immediately before
        # the delete): a live holder that took the lock in between.
        reads["n"] += 1
        if reads["n"] == 1:
            return {"pid": dead.pid, "backend": "mlx", "since": "then"}
        return {"pid": os.getpid(), "backend": "mlx", "since": "now"}

    modellock.owner = racing_owner
    try:
        took = modellock._reclaim_if_dead(quiet)
    finally:
        modellock.owner = real_owner
    check("a lock that changed hands between the two reads is NOT reclaimed",
          took is False and modellock.LOCK_DIR.is_dir(), str(took))
    check("and it read the owner twice, which is the whole point",
          reads["n"] == 2, str(reads["n"]))

    modellock._reclaim_if_dead(quiet)
    check("with the owner still dead, the reclaim happens",
          not modellock.LOCK_DIR.exists())

    print("\nfinding 11: the torch backend takes the SAME lock, with an owner")
    import backends.torch_backend as tb
    lock_dir = tmp / "torch.lock"
    was = tb._MODEL_LOCK_DIR
    tb._MODEL_LOCK_DIR = lock_dir
    try:
        tb.acquire_model_lock(poll_s=0.01, log=False)
        info = modellock.owner(lock_dir) or {}
        check("a torch run writes an owner file, so a killed run can be "
              "reclaimed instead of blocking every later run forever",
              lock_dir.is_dir() and info.get("pid") == os.getpid(),
              json.dumps(info))
        tb.release_model_lock()
        check("and it lets go", not lock_dir.exists())

        lock_dir.mkdir()
        (lock_dir / "owner.json").write_text(json.dumps({"pid": 1}))
        tb.release_model_lock()
        check("a release with nothing of ours held removes nothing",
              lock_dir.is_dir())
    finally:
        tb._MODEL_LOCK_DIR = was
        for entry in sorted(lock_dir.glob("*")):
            entry.unlink()
        if lock_dir.exists():
            lock_dir.rmdir()

    modellock.LOCK_DIR = real
    # The pair, unchanged: it is still there if it was there, still gone if it
    # was gone, and still owned by whoever owned it. A suite that deleted the
    # founder's lock now turns this red, which the old form could not.
    check("the real lock still exists exactly as it did before this suite",
          real.exists() == real_existed,
          f"{real}: existed={real_existed} now={real.exists()}")
    check("and it is still owned by whoever owned it before, not by this pid",
          modellock.owner(real) == real_lock_owner,
          f"before={json.dumps(real_lock_owner)} "
          f"after={json.dumps(modellock.owner(real))}")


# ---------------------------------------------------------------------------
# finding 13: a matte id is one path segment
# ---------------------------------------------------------------------------

def test_store_paths(tmp: Path) -> None:
    print("\nfinding 13: ids that mean 'somewhere else' are refused")
    for bad in ("..", ".", "../../ESCAPED", "/tmp/ESCAPED", "a/b", "a\\b", "",
                ".hidden", "m" * 65, "m id"):
        raised = ""
        try:
            store.safe_id(bad)
        except store.StoreError as exc:
            raised = str(exc)
        check(f"{bad!r} is refused", bool(raised), raised[:60])
    for good in ("m_abc123", "t0_0", "semantic.png", "C015-rot0", "k0"):
        ok = True
        try:
            store.safe_id(good)
        except store.StoreError:
            ok = False
        check(f"{good!r} is accepted", ok)

    root = tmp / "store" / "root"
    root.mkdir(parents=True)
    for bad in ("../../ESCAPED-RELATIVE", str(tmp / "ESCAPED-ABSOLUTE")):
        raised = ""
        try:
            store.MatteWriter(root, bad, header())
        except store.StoreError as exc:
            raised = str(exc)
        check(f"MatteWriter refuses the id {bad!r} instead of writing an "
              f"index.json outside its root", bool(raised), raised[:70])
    check("and nothing was created outside the root",
          not (tmp / "ESCAPED-RELATIVE").exists()
          and not (tmp / "ESCAPED-ABSOLUTE").exists()
          and not (root.parent / "ESCAPED-RELATIVE").exists())

    good = store.MatteWriter(root, "m_ok", header())
    check("a real id still writes inside the root",
          good.dir == root / "m_ok" and (good.dir / "index.json").is_file())

    outside = tmp / "store" / "outside"
    outside.mkdir(parents=True)
    (root / "link").symlink_to(outside, target_is_directory=True)
    raised = ""
    try:
        store.under(root, "link")
    except store.StoreError as exc:
        raised = str(exc)
    check("a symlinked entry that leaves the root is refused too: the pattern "
          "stops traversal, this stops a link", bool(raised), raised[:70])


# ---------------------------------------------------------------------------
# findings 23, 24, 25, 26, 46: the MLX seams
# ---------------------------------------------------------------------------

def test_mlx_seams(tmp: Path) -> None:
    print("\nfindings 23 to 26 and 46: the MLX attention and load order")
    import mlx.core as mx

    import backends.mlx_backend as mlxmod
    from backends.mlx_backend import MlxBackend

    # The real floor is 256 MB of attention matrix, and a matrix that big is a
    # 256 MB allocation on a 16 GB machine that is also running the studio. The
    # floor is a module constant read when the wrapper is installed, so the
    # arithmetic below is exercised at 6 MB with tensors of a few hundred KB.
    # What is under test is the DECISION (chunk or leave whole, and the count),
    # which is the same arithmetic at either floor.
    real_floor = mlxmod.ATTENTION_CHUNK_MIN_MB
    mlxmod.ATTENTION_CHUNK_MIN_MB = 6
    heads, dim = 4, 32
    try:
        backend = MlxBackend(log=quiet)
        backend._mx = mx
        first = backend._apply_attention_chunk(mx)
        wrapper = mx.fast.scaled_dot_product_attention
        true_original = wrapper._fixxr_original
        check("the wrapper is installed at the default chunk",
              first["enabled"] and first["chunk"] == 512, str(first))

        retuned = MlxBackend(attention_chunk=256, log=quiet)
        report_256 = retuned._apply_attention_chunk(mx)
        installed = mx.fast.scaled_dot_product_attention
        check("a second load with a different chunk RETUNES the wrapper "
              "rather than keeping the old size and reporting the new one",
              report_256["chunk"] == 256
              and installed._fixxr_chunk == 256, str(report_256))
        check("and it re-wraps MLX's own function, not the previous wrapper, "
              "so taking it off restores the original",
              installed._fixxr_original is true_original)

        off = MlxBackend(attention_chunk=0, log=quiet)
        report_off = off._apply_attention_chunk(mx)
        check("asking for 0 in the same process really removes the wrapper: it "
              "used to leave 512 query chunking in force while /health said "
              "enabled false",
              report_off["enabled"] is False
              and mx.fast.scaled_dot_product_attention is true_original,
              str(report_off))

        # Back on for the counter checks.
        backend._apply_attention_chunk(mx)
        queries = mx.random.normal((1, heads, 1024, dim))
        keys = mx.random.normal((1, heads, 1024, dim))
        values = mx.random.normal((1, heads, 1024, dim))
        mx.eval(queries, keys, values)
        chunkedout = mx.fast.scaled_dot_product_attention(
            queries, keys, values, scale=dim ** -0.5)
        whole = true_original(queries, keys, values, scale=dim ** -0.5)
        mx.eval(chunkedout, whole)
        live = backend.attention_chunk_report()
        check("what the wrapper DID reaches /health, not only what it was "
              "asked for: this call was chunked and nothing was left whole "
              "over the floor", live["installed"] and live["chunked"] == 1
              and live["whole_over_threshold"] == 0, str(live))
        difference = float(mx.max(mx.abs(chunkedout - whole)))
        check("and the chunked result is bit identical to the whole one",
              difference == 0.0, f"max abs diff {difference:.3e}")

        mask = mx.zeros((1, heads, 1024, 1024))
        mx.eval(mx.fast.scaled_dot_product_attention(
            queries, keys, values, scale=dim ** -0.5, mask=mask))
        live = backend.attention_chunk_report()
        check("a big matrix left whole is COUNTED and its reason named, which "
              "is how a future mlx-cv passing a mask would be visible instead "
              "of quietly bringing the 5.2 GB transient back",
              live["whole_over_threshold"] == 1
              and "mask" in (live.get("whole_reason") or ""), str(live))
        del mask

        # finding 46: the batch axis. Per batch this matrix is 4 MB, under the
        # 6 MB floor; two batches is 8 MB, over it. Before the fix the batch
        # was ignored, so a batched call stayed whole for that reason alone.
        batched_q = mx.random.normal((2, heads, 1024, dim))
        batched_k = mx.random.normal((2, heads, 256, dim))
        batched_v = mx.random.normal((2, heads, 256, dim))
        mx.eval(batched_q, batched_k, batched_v)
        before = backend.attention_chunk_report()["chunked"]
        out = mx.fast.scaled_dot_product_attention(
            batched_q, batched_k, batched_v, scale=dim ** -0.5)
        whole = true_original(batched_q, batched_k, batched_v, scale=dim ** -0.5)
        mx.eval(out, whole)
        per_batch_mb = heads * 1024 * 256 * 4 / 1048576
        check("a BATCHED attention is measured with its batch axis, so a "
              f"matrix that is {per_batch_mb:.0f} MB per batch and "
              f"{2 * per_batch_mb:.0f} MB in total is chunked rather than left "
              f"whole under a {mlxmod.ATTENTION_CHUNK_MIN_MB} MB floor",
              backend.attention_chunk_report()["chunked"] == before + 1,
              str(backend.attention_chunk_report()))
        difference = float(mx.max(mx.abs(out - whole)))
        check("and the batched chunked result is still bit identical",
              difference == 0.0, f"max abs diff {difference:.3e}")
        del out, whole, chunkedout, batched_q, batched_k, batched_v
        del queries, keys, values
    finally:
        mlxmod.ATTENTION_CHUNK_MIN_MB = real_floor
        MlxBackend._remove_attention_chunk()
        mx.clear_cache()

    print("\nfindings 23 and 26: the load order and a load that fails late")
    import mlx_cv.hub as hub
    import mlx_cv.models.sam3 as sam3
    sentinel = object()
    seen: dict = {}
    was_resolve = hub.resolve_pretrained
    was_session = sam3.SAM3VideoSession
    # A directory that exists and holds no tokenizer, so load() gets past
    # from_pretrained and then fails, which is the case finding 26 is about.
    empty = tmp / "snapshot-without-tokenizer"
    empty.mkdir(parents=True, exist_ok=True)

    class FakeSession:
        @staticmethod
        def from_pretrained(_repo):
            # Recorded HERE, before the weights would be built: with the limits
            # applied afterwards this dict was empty (round 1 finding 23).
            seen["limits"] = dict(late.limits)
            return sentinel

    late = MlxBackend(cache_limit_mb=777, log=quiet)
    hub.resolve_pretrained = lambda _repo: empty
    sam3.SAM3VideoSession = FakeSession
    raised = ""
    try:
        late.load()
    except Exception as exc:                                   # noqa: BLE001
        raised = f"{type(exc).__name__}: {exc}"
    finally:
        hub.resolve_pretrained = was_resolve
        sam3.SAM3VideoSession = was_session
        MlxBackend._remove_attention_chunk()
    check("a load with no tokenizer beside the weights fails, as before",
          "BackendUnavailable" in raised, raised[:80])
    check("the MLX allocator was already bounded when the weights were asked "
          "for, which is what load()'s docstring always claimed",
          (seen.get("limits") or {}).get("cache_limit_mb") == 777.0,
          json.dumps(seen.get("limits") or {}))
    check("and the session is reachable by close() even though the load failed "
          "after it, so 5 GB of weights are not left resident for the next "
          "backend in --backend auto to load on top of",
          late._session is sentinel)
    late.close()
    check("close() then really frees it", late._session is None)
    mx.clear_cache()


# ---------------------------------------------------------------------------
# finding 27: a failed track closes its window
# ---------------------------------------------------------------------------

class _LosingInner:
    """A torch backend whose object vanishes: every mask is empty, so the
    adapter's second window has nothing to seed from and raises."""

    name = "torch-cpu"
    model = "fake"

    def track(self, frames, fps, prompts, select, on_frame) -> None:
        materialised = list(frames)
        height, width = materialised[0].shape[:2]
        for index in range(len(materialised)):
            on_frame(index, {1: np.zeros((height, width), dtype=np.float32)})


def test_windows_close(tmp: Path) -> None:
    print("\nfinding 27: a window closes however the track ended")
    stub = StubBackend(chunk_frames=4, log=quiet)
    stub.load()
    raised = ""
    try:
        stub.track(iter([np.zeros((8, 16, 3), dtype=np.uint8) for _ in range(10)]),
                   24.0, {"text": [FAIL_PHRASE]}, "all", lambda i, m: None)
    except Exception as exc:                                   # noqa: BLE001
        raised = f"{type(exc).__name__}: {exc}"
    check("the stub's deliberate failure still raises", "RuntimeError" in raised,
          raised[:60])
    check("and /health is not left reporting a window that is not loaded",
          stub.window is None, str(stub.window))
    check("nor a window meter that never closed",
          (stub.meter.stats().get("current")) is None,
          str(stub.meter.stats().get("current")))

    adapter = TorchAdapter(device="cpu", chunk_frames=4, log=quiet)
    adapter._inner = _LosingInner()
    raised = ""
    try:
        adapter.track(iter([np.zeros((8, 16, 3), dtype=np.uint8) for _ in range(10)]),
                      24.0, {"text": ["person"]}, "all", lambda i, m: None)
    except Exception as exc:                                   # noqa: BLE001
        raised = f"{type(exc).__name__}: {exc}"
    check("the adapter raises when every object was lost", "lost" in raised,
          raised[:70])
    check("and it closes its window too: this raise happens while seeding the "
          "next window, which used to be outside the try",
          adapter.window is None, str(adapter.window))
    check("and its meter", (adapter.meter.stats().get("current")) is None,
          str(adapter.meter.stats().get("current")))


# ---------------------------------------------------------------------------
# finding 28: a failure to free is logged
# ---------------------------------------------------------------------------

def test_free_failure_is_logged() -> None:
    print("\nfinding 28: a window that would not free says so")
    said: list[str] = []
    meter = window_meter(log=said.append)
    meter.start(start=0, end=4, frames=4)

    def raiser():
        raise RuntimeError("mlx-cv moved what the state holds")

    record = meter.finish(freed=raiser)
    check("the window still closes, because a failed free must not fail a job",
          isinstance(record, dict) and record.get("window_index") == 1)
    check("the failure is recorded on the window itself",
          "mlx-cv moved" in (record.get("freed_error") or ""),
          str(record.get("freed_error")))
    check("and logged, naming where to look, instead of leaving a slowly "
          "growing memory footprint as the only symptom",
          any("_drop_state" in line for line in said), str(said[-1:]))


# ---------------------------------------------------------------------------
# finding 45: ffmpeg's stderr
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, payload: bytes):
        self.stdout = _FakeStdout(payload)
        self.stderr = None
        self.pid = -1

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


class _FakeStdout:
    def __init__(self, payload: bytes):
        self._payload = payload
        self._at = 0

    def read(self, size):
        out = self._payload[self._at:self._at + size]
        self._at += len(out)
        return out

    def close(self):
        pass


def test_ffmpeg_stderr(tmp: Path) -> None:
    print("\nfinding 45: ffmpeg's stderr is a file, never an undrained pipe")
    video = make_video(tmp / "tiny.mp4", seconds=1)
    source = framesmod.FrameSource(str(video))
    captured: dict = {}
    was = framesmod.subprocess.Popen
    size = source.width * source.height * 3

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        captured["cmd"] = cmd
        return _FakeProc(b"\x00" * size)

    framesmod.subprocess.Popen = fake_popen
    try:
        got = list(source.frames(0, 1))
    finally:
        framesmod.subprocess.Popen = was
    check("one frame comes back through the fake", len(got) == 1)
    check("stderr is NOT subprocess.PIPE: a pipe nobody drains holds 64 KB and "
          "then blocks ffmpeg while this loop waits on stdout, with no timeout "
          "anywhere",
          captured.get("stderr") is not subprocess.PIPE,
          str(captured.get("stderr")))
    check("it is a real file, so it can never fill",
          hasattr(captured.get("stderr"), "fileno"))

    def failing_popen(cmd, **kwargs):
        handle = kwargs.get("stderr")
        handle.write(b"tiny.mp4: Invalid data found when processing input\n")
        return _FakeProc(b"")

    framesmod.subprocess.Popen = failing_popen
    raised = ""
    try:
        list(source.frames(0, 1))
    except framesmod.VideoError as exc:
        raised = str(exc)
    finally:
        framesmod.subprocess.Popen = was
    check("a decode that produced nothing now says what ffmpeg said, rather "
          "than handing back an empty iterator",
          "Invalid data" in raised, raised[:90])


# ---------------------------------------------------------------------------
# finding 47: loading torch does not disarm the module's lock
# ---------------------------------------------------------------------------

def test_torch_lock_not_disarmed() -> None:
    print("\nfinding 47: the adapter no longer replaces the lock functions")
    import backends.torch_backend as tb
    before = (tb.acquire_model_lock, tb.release_model_lock)
    asked: list[bool] = []
    real_backend = tb.TorchBackend

    class Recorder:
        """Stands in for the real TorchBackend, so this check costs nothing: a
        real load is 5 GB of weights and the machine wide lock."""

        name = "torch-cpu"
        repo_id = "fake"

        def __init__(self, device=None, repo_id=None, use_model_lock=True):
            asked.append(bool(use_model_lock))

        def load(self):
            raise RuntimeError("not loading anything in a test")

    tb.TorchBackend = Recorder
    try:
        for outer_held in (True, False):
            adapter = TorchAdapter(device="cpu", outer_lock_held=outer_held,
                                   log=quiet)
            raised = ""
            try:
                adapter.load()
            except Exception as exc:                           # noqa: BLE001
                raised = type(exc).__name__
            check(f"a load with outer_lock_held={outer_held} reaches the inner "
                  f"backend", raised == "BackendUnavailable" and len(asked) > 0,
                  raised)
    finally:
        tb.TorchBackend = real_backend
    check("the inner backend is told whether to take the lock through an "
          "ARGUMENT: False when this process already holds it, True when it "
          "does not", asked == [False, True], str(asked))
    check("and the module's own lock functions are the real ones afterwards: "
          "they used to be replaced with no-ops for the whole process and "
          "never put back, so every later TorchBackend in this process ran "
          "unlocked", (tb.acquire_model_lock, tb.release_model_lock) == before)
    check("nothing in the adapter assigns to them any more",
          "acquire_model_lock" not in inspect.getsource(TorchAdapter.load))


# ---------------------------------------------------------------------------
# gaps 20 and 21, over real HTTP
# ---------------------------------------------------------------------------

def test_service_paths_and_states(tmp: Path) -> None:
    print("\nfinding 13 over HTTP: what a request may ask the service to write")
    data = tmp / "svc" / "data"
    clip = make_frames(tmp / "svc" / "clip", count=12, width=48, height=32)
    port = free_port()
    proc, base = start(port, data)
    try:
        health = call(base, "/health")
        paths = health.get("paths") or {}
        check("/health says where this service may write",
              paths.get("allowed_roots") == [str(data.resolve())]
              and paths.get("confined") is False, json.dumps(paths))
        check("and it no longer claims to hold a lock it never took: this "
              "service was started with --stub --no-model-lock, and `held` "
              "used to read `held or --no-model-lock`",
              health["model_lock"]["held"] is False
              and health["model_lock"]["enabled"] is False,
              json.dumps(health["model_lock"]))

        bad = call(base, "/track", {"video": str(clip), "fps": 24,
                                    "prompts": {"text": ["person"]},
                                    "out_dir": "mattes/here"})
        check("a relative out_dir is a 400", bad["status"] == 400, str(bad)[:90])
        bad = call(base, "/track", {"video": str(clip), "fps": 24,
                                    "prompts": {"text": ["person"]},
                                    "out_dir": str(data / ".." / "escape")})
        check("an out_dir containing '..' is a 400", bad["status"] == 400,
              str(bad)[:90])
        bad = call(base, "/track", {"video": str(clip), "fps": 24,
                                    "prompts": {"text": ["person"]},
                                    "clip_key": "../../pwned"})
        check("a clip_key used as a directory name is a 400 too",
              bad["status"] == 400, str(bad)[:90])
        bad = call(base, "/track", {
            "video": str(clip), "fps": 24, "prompts": {"text": ["person"]},
            "out_dir": str(data / "mattes" / "x"),
            "matte_ids": {"t0_0": "../../../pwned"}})
        check("a matte id that is a path is a 400, not a directory somewhere "
              "else with an index.json in it", bad["status"] == 400,
              str(bad)[:90])
        check("and nothing was created where it pointed",
              not (tmp / "pwned").exists() and not (tmp / "svc" / "pwned").exists())

        elsewhere = tmp / "elsewhere" / "mattes"
        allowed = call(base, "/track", {
            "video": str(clip), "fps": 24, "prompts": {"text": ["person"]},
            "clip_key": "elsewhere", "out_dir": str(elsewhere)})
        check("a caller's own root outside the data dir is still accepted, "
              "because the studio owns the matte store and names it on every "
              "call", allowed["status"] == 200, str(allowed)[:90])
        check("but it is warned about on the job rather than written silently",
              any("outside this service's data dir" in w
                  for w in allowed["warnings"]), str(allowed["warnings"]))
        paths = (call(base, "/health").get("paths") or {})
        check("and named on /health, so a person can see where this service "
              "has been writing",
              any("elsewhere" in p for p in paths.get("external_out_dirs") or []),
              json.dumps(paths.get("external_out_dirs")))
        wait_for_job(base, allowed["job_id"], 30)
    finally:
        stop(proc)

    print("\nfinding 13 over HTTP: --allow-out-dir makes it a hard rule")
    port = free_port()
    confined_root = tmp / "confined"
    proc, base = start(port, tmp / "confined-data",
                       ["--allow-out-dir", str(confined_root)])
    try:
        paths = (call(base, "/health").get("paths") or {})
        check("/health says it is confined and to what",
              paths.get("confined") is True
              and len(paths.get("allowed_roots") or []) == 2,
              json.dumps(paths.get("allowed_roots")))
        ok = call(base, "/track", {
            "video": str(clip), "fps": 24, "prompts": {"text": ["person"]},
            "out_dir": str(confined_root / "mattes" / "a")})
        check("an out_dir inside an allowed root is accepted",
              ok["status"] == 200, str(ok)[:90])
        refused = call(base, "/track", {
            "video": str(clip), "fps": 24, "prompts": {"text": ["person"]},
            "out_dir": str(tmp / "anywhere")})
        check("and anything else is refused with the roots named",
              refused["status"] == 400
              and "allow-out-dir" in str(refused.get("error")),
              str(refused)[:120])
        wait_for_job(base, ok["job_id"], 30)
    finally:
        stop(proc)

    print("\ngap 20: a pick seeds a track from its mask, not its box")
    port = free_port()
    data = tmp / "seed" / "data"
    proc, base = start(port, data)
    try:
        still = tmp / "seed" / "still.png"
        still.parent.mkdir(parents=True, exist_ok=True)
        from PIL import Image
        Image.fromarray(np.zeros((32, 48, 3), dtype=np.uint8)).save(still)
        pick = call(base, "/segment", {"image": str(still),
                                       "prompts": {"text": ["person"]}})
        instance = pick["instances"][0]
        seeded = call(base, "/track", {
            "video": str(clip), "fps": 24, "pick": pick["pick_id"],
            "select": [instance["id"]], "clip_key": "seed"})
        check("the response says which of the pick's two descriptions seeded "
              "the track, instead of leaving it to be discovered from the "
              "result", seeded.get("seeded_from") == "mask",
              str(seeded.get("seeded_from")))
        check("and the object is named for a mask slot",
              seeded["mattes"][0]["object_id"] == "k0",
              str(seeded["mattes"][0]["object_id"]))
        wait_for_job(base, seeded["job_id"], 30)
        index = json.loads((Path(seeded["mattes"][0]["path"]) / "index.json").read_text())
        check("the matte remembers it was seeded from the mask, and what its "
              "score curve means on this backend",
              index["seeded_from"] == "mask" and index["score_kind"] == "tracker",
              f"{index.get('seeded_from')} / {index.get('score_kind')}")
        check("and it still keeps the pick's own instance id and label",
              index["picked_from"] == instance["id"]
              and index["pick"] == pick["pick_id"],
              str(index.get("picked_from")))

        # The fallback: the pick's mask files are gone, so the box is all there
        # is, and the response has to say so.
        for path in Path(pick["instances"][0]["mask"]).parent.glob("*.png"):
            path.unlink()
        boxed = call(base, "/track", {
            "video": str(clip), "fps": 24, "pick": pick["pick_id"],
            "select": [instance["id"]], "clip_key": "seed-boxed"})
        check("with the pick's mask gone it falls back to the box",
              boxed.get("seeded_from") == "box", str(boxed.get("seeded_from")))
        check("and says the tracked region can grow past the reviewed shape, "
              "which is the thing that cost a whole tracking pass to find out",
              any("grow past the shape that was reviewed" in w
                  for w in boxed["warnings"]), str(boxed["warnings"]))
        wait_for_job(base, boxed["job_id"], 30)
    finally:
        stop(proc)

    print("\ngap 21: one honest state per job, plus who has the model")
    port = free_port()
    slow_clip = make_frames(tmp / "slow" / "clip", count=60, width=48, height=32)
    proc, base = start(port, tmp / "slow" / "data", ["--stub-delay-ms", "40"])
    try:
        first = call(base, "/track", {"video": str(slow_clip), "fps": 24,
                                      "prompts": {"text": ["person"]},
                                      "clip_key": "one"})
        second = call(base, "/track", {"video": str(slow_clip), "fps": 24,
                                       "prompts": {"text": ["cat"]},
                                       "clip_key": "two"})
        disagreed = []
        holder_named = False
        queued_holds_model = []
        running_holds_model = []
        for _ in range(200):
            health = call(base, "/health")
            holder = health.get("model_holder")
            jobs = {j["job_id"]: j for j in health["queue"]["jobs"]}
            a = jobs.get(first["job_id"], {})
            b = jobs.get(second["job_id"], {})
            if holder and holder["job_id"] == first["job_id"]:
                holder_named = True
            for job in (a, b):
                if job.get("state") == "running" and \
                        job.get("matte_states") == ["queued"]:
                    disagreed.append(job)
            if b.get("state") == "queued":
                queued_holds_model.append(b.get("holds_model"))
                if a.get("state") == "running":
                    running_holds_model.append(a.get("holds_model"))
            if b.get("state") in ("done", "failed", "cancelled"):
                break
            time.sleep(0.05)
        check("/health names the job the model is actually working on",
              holder_named)
        check("the job the worker took says the model is on it, on every "
              "sample taken while the second job waited",
              running_holds_model and all(running_holds_model),
              f"{len(running_holds_model)} samples")
        check("a job waiting its turn never claims to hold the model, which is "
              "the confusion this gap was about",
              queued_holds_model and not any(queued_holds_model),
              f"{len(queued_holds_model)} samples")
        check("and no sample ever showed a running job whose own mattes still "
              "said queued", not disagreed, str(disagreed[:1]))
        idle = call(base, "/health")
        for _ in range(40):
            if idle.get("model_holder") is None:
                break
            time.sleep(0.05)
            idle = call(base, "/health")
        check("with the queue drained nothing holds the model",
              idle.get("model_holder") is None
              and idle["queue"]["running"] is None, str(idle.get("model_holder")))
    finally:
        stop(proc)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="sam-safety-"))
    try:
        test_lock_ownership(tmp)
        test_store_paths(tmp)
        test_mlx_seams(tmp)
        test_windows_close(tmp)
        test_free_failure_is_logged()
        test_ffmpeg_stderr(tmp)
        test_torch_lock_not_disarmed()
        test_service_paths_and_states(tmp)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return report("test_safety")


if __name__ == "__main__":
    raise SystemExit(main())
