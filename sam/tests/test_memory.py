#!/usr/bin/env python3
"""The memory readings, the per window meter, and the flags that bound MLX.

Everything here runs on the stub backend and on plain numpy, so it needs no
weights, no network and no machine wide model lock. What it cannot prove
without the model is the size of the numbers on a real track; what it can
prove is that the numbers exist, are the Mac's own, move when memory moves,
are reported per window, and that the flags that bound the allocator reach
the backend. The real model check is `spike/track_memory.py`, which the
orchestrator runs when the model lock is free.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (FORBIDDEN_PORTS, call, check, free_port, report,  # noqa: E402
                    sam_path, skip, start, stop, wait_for_job)

sam_path()
import memstat                                                        # noqa: E402
from backends import make_backend                                     # noqa: E402
import backends.mlx_backend as mlx_backend                            # noqa: E402
from backends.mlx_backend import (ATTENTION_CHUNK,                  # noqa: E402
                                  ATTENTION_CHUNK_MIN_MB, CACHE_LIMIT_MB,
                                  LAYER_EVAL, MEMORY_LIMIT_MB, MlxBackend)
from backends.torch_adapter import TorchAdapter                       # noqa: E402
from server import build_parser                                       # noqa: E402


def frames(count: int, width: int = 32, height: int = 16):
    for _ in range(count):
        yield np.zeros((height, width, 3), dtype=np.uint8)


def test_readings() -> None:
    print("the Mac's own numbers, not ps rss")
    shot = memstat.snapshot()
    check("footprint is read and is a real number",
          isinstance(shot.get("footprint_mb"), float) and shot["footprint_mb"] > 0,
          str(shot.get("footprint_mb")))
    check("compressed is read too, because that is the half of the 13 GB "
          "that ps never shows", shot.get("compressed_mb") is not None,
          str(shot.get("compressed_mb")))
    check("rss and its peak are still there, so nothing that read them breaks",
          shot.get("rss_mb", 0) > 0 and shot.get("peak_rss_mb", 0) > 0,
          f"{shot.get('rss_mb')} / {shot.get('peak_rss_mb')}")

    before = memstat.snapshot()["footprint_mb"]
    ballast = [bytearray(32 * 1024 * 1024) for _ in range(4)]
    for block in ballast:                       # touch it: untouched pages
        block[::4096] = b"x" * (len(block) // 4096)   # are not footprint
    after = memstat.snapshot()["footprint_mb"]
    check("the footprint moves when memory moves: 128 MB allocated reads as "
          "at least 100 MB more", after - before > 100, f"{before} -> {after}")
    del ballast

    own = memstat.pid_footprint(__import__("os").getpid())
    check("the same number is readable for any pid, which is how a watcher "
          "script measures a service it started",
          own is not None and abs(own / 1048576 - after) < 200,
          f"{None if own is None else round(own / 1048576, 1)} vs {after}")


def test_meter() -> None:
    print("\nthe per window meter")
    meter = memstat.WindowMeter(log=None)
    check("nothing has run yet", meter.stats()["count"] == 0
          and meter.stats()["last"] is None)
    meter.start(start=0, end=48, frames=48)
    check("a window in flight is visible while it runs, not only after it",
          (meter.stats().get("current") or {}).get("start") == 0)
    ballast = bytearray(64 * 1024 * 1024)
    ballast[::4096] = b"x" * (len(ballast) // 4096)
    meter.sample()
    window = meter.finish(freed=lambda: None, note="unit")
    check("the window records its own peak footprint, sampled while it ran",
          window["peak_footprint_mb"] > 0, str(window["peak_footprint_mb"]))
    check("the peak is at least what the window ended at, because it is a "
          "maximum over the frames and not the last reading",
          window["peak_footprint_mb"] >= window["footprint_before_mb"],
          f"{window['peak_footprint_mb']} vs {window['footprint_before_mb']}")
    check("the free is measured, before and after",
          window["footprint_after_mb"] is not None
          and window["footprint_before_mb"] is not None)
    check("extra fields the caller passes are kept", window.get("note") == "unit")
    stats = meter.stats()
    check("one window is counted, and it is both the last and the worst",
          stats["count"] == 1 and stats["last"]["window_index"] == 1
          and stats["peak"]["window_index"] == 1)
    check("nothing is in flight once it is finished", stats["current"] is None)
    del ballast


def test_stub_windows() -> None:
    print("\nthe stub reports windows too, so this is testable without weights")
    backend = make_backend("stub", log=lambda _m: None, chunk_frames=48)
    backend.load()
    seen: list[int] = []
    backend.track(frames(100), 24.0, {"text": ["person"]}, "all",
                  lambda index, masks: seen.append(index))
    stats = backend.window_stats()
    check("100 frames in windows of 48 is three windows",
          stats["count"] == 3, str(stats["count"]))
    check("every frame still came out exactly once, in order",
          seen == list(range(100)), f"{len(seen)} frames")
    check("each window carries a peak footprint",
          all(w.get("peak_footprint_mb") for w in backend.meter.windows))
    check("the window record names the frames it covered",
          stats["last"]["start"] == 96, str(stats["last"].get("start")))
    check("the backend reports its own memory in the shape /health expects",
          set(backend.memory()) >= {"active_mb", "cache_mb", "peak_mb", "limits"},
          str(backend.memory()))
    # The outcome, not the fact that the line was reached: this was a bare
    # check("...", True), which passed whatever release() did (round 1
    # finding 39). Called twice, because the service calls it in a `finally`
    # after a window it may never have opened.
    raised = ""
    try:
        backend.release()
        backend.release()
    except Exception as exc:                                   # noqa: BLE001
        raised = f"{type(exc).__name__}: {exc}"
    check("release is safe to call twice on a backend with nothing to "
          "release, and the window record it already wrote survives it",
          raised == "" and len(backend.meter.windows) == 3,
          raised or f"{len(backend.meter.windows)} windows")


def test_mlx_limits_without_mlx() -> None:
    print("\nthe MLX limits are configuration, readable without the model")
    backend = MlxBackend(log=lambda _m: None)
    check("the default cache limit is 1024 MB, not MLX's own 15564 MB",
          backend.cache_limit_mb == CACHE_LIMIT_MB == 1024,
          str(backend.cache_limit_mb))
    check("the default memory limit is 0, meaning the GPU's recommended "
          "working set", backend.memory_limit_mb == MEMORY_LIMIT_MB == 0,
          str(backend.memory_limit_mb))
    chosen = MlxBackend(cache_limit_mb=256, memory_limit_mb=6000,
                        log=lambda _m: None)
    check("both are settable", chosen.cache_limit_mb == 256
          and chosen.memory_limit_mb == 6000)
    check("with no model loaded the memory report is empty rather than wrong",
          chosen.memory() == {"limits": {}}, str(chosen.memory()))

    print("\nwindows of frames are streamed, never held as a clip")
    windowed = MlxBackend(chunk_frames=4, log=lambda _m: None)
    got = []
    for start, chunk in windowed._chunks(iter(list(frames(10))[1:]),
                                         next(frames(1))):
        got.append((start, len(chunk)))
        chunk.clear()          # what track() does once mlx-cv has copied them
    check("a 10 frame clip comes out as windows of at most 4",
          all(size <= 4 for _, size in got), str(got))
    check("windows overlap by one frame and cover the clip",
          [s for s, _ in got] == [0, 3, 6], str([s for s, _ in got]))
    check("emptying a window in place does not break the next one, which is "
          "what lets the raw frames go as soon as they are copied",
          got[-1][0] == 6)


def test_torch_adapter_release() -> None:
    print("\nthe torch adapter gives its cache back the same way")
    adapter = TorchAdapter(device="cpu", log=lambda _m: None)
    adapter.release()
    check("release on CPU is a safe no op", adapter.memory() == {"device": "cpu"},
          str(adapter.memory()))
    check("the adapter has a window meter like every other backend",
          adapter.window_stats() is not None)


def test_flags_and_health() -> None:
    print("\nthe flags exist and reach the backend")
    args = build_parser().parse_args(
        ["--stub", "--mlx-cache-limit-mb", "256", "--mlx-memory-limit-mb", "6000"])
    check("--mlx-cache-limit-mb is parsed", args.mlx_cache_limit_mb == 256)
    check("--mlx-memory-limit-mb is parsed", args.mlx_memory_limit_mb == 6000)
    check("both default to None, so an unset flag leaves the backend's own "
          "default rather than overwriting it with a zero",
          build_parser().parse_args([]).mlx_cache_limit_mb is None
          and build_parser().parse_args([]).mlx_memory_limit_mb is None)

    print("\nand /health carries the numbers over HTTP")
    port = free_port()
    check("the test never uses a port someone else is on",
          port not in FORBIDDEN_PORTS, str(port))
    data_dir = Path(__import__("tempfile").mkdtemp(prefix="sam-memory-"))
    proc, base = start(port, data_dir, extra=["--mlx-cache-limit-mb", "256"])
    try:
        health = call(base, "/health")
        memory = health.get("memory") or {}
        check("health reports the footprint, which is the number top shows",
              memory.get("footprint_mb", 0) > 0, str(memory.get("footprint_mb")))
        check("and its high water mark, so a leak is visible without top",
              memory.get("peak_footprint_mb", 0) > 0,
              str(memory.get("peak_footprint_mb")))
        check("and compressed, the part of the 13 GB that ps never showed",
              "compressed_mb" in memory)
        check("rss is still reported beside it, unchanged for anything that "
              "already read it", memory.get("rss_mb", 0) > 0)
        check("the backend's own numbers are there, with the limits in force",
              isinstance(memory.get("backend"), dict)
              and "limits" in memory["backend"], str(memory.get("backend")))
        check("the window record is there before any job has run",
              isinstance(memory.get("windows"), dict)
              and memory["windows"]["count"] == 0,
              str(memory.get("windows")))

        clip = data_dir / "frames"
        from common import make_frames
        make_frames(clip, count=60)
        job = call(base, "/track", {"video": str(clip), "fps": 24,
                                    "prompts": {"text": ["person"]},
                                    "out_dir": str(data_dir / "mattes")})
        done = wait_for_job(base, job["job_id"], timeout_s=60)
        check("the track finished", done.get("state") == "done", str(done.get("state")))
        memory = (call(base, "/health").get("memory") or {})
        windows = memory.get("windows") or {}
        check("every window of the job is counted on health",
              windows.get("count") == 2, str(windows.get("count")))
        check("the worst window is named with its peak footprint, which is "
              "the one number that says whether this service is bounded",
              (windows.get("peak") or {}).get("peak_footprint_mb", 0) > 0,
              str((windows.get("peak") or {}).get("peak_footprint_mb")))
        check("the last window says how long it took and what the free bought",
              (windows.get("last") or {}).get("elapsed_s") is not None
              and (windows.get("last") or {}).get("footprint_after_mb") is not None,
              str(windows.get("last")))
    finally:
        stop(proc)




def test_stage_meter() -> None:
    print("\nthe stage meter: which step inside a window costs the memory")
    backend = MlxBackend(log=lambda _m: None)
    check("every MLX backend carries one, and it is OFF by default: a "
          "measured run is something someone asks for, not a tax on every "
          "track", backend.stages is not None and backend.stages.enabled is False)
    with backend.stages.stage("nothing"):
        pass
    check("a disabled stage records nothing at all",
          backend.stages.rows() == [], str(backend.stages.rows()))

    import mmap

    meter = memstat.stage_meter(enabled=True, sample_ms=2.0)
    meter.start()
    for _ in range(3):
        with meter.stage("transient"):
            # mmap, not a bytearray: macOS hands mapped pages straight back on
            # close, while malloc keeps them, and this test is about a
            # transient that is really gone by the end of the stage.
            block = mmap.mmap(-1, 256 * 1024 * 1024)
            block.write(b"x" * (256 * 1024 * 1024))
            block.close()
    meter.stop()
    row = meter.rows()[0]
    check("a stage counts its calls and its time",
          row["calls"] == 3 and row["seconds"] > 0,
          f"{row['calls']} calls in {row['seconds']:.3f}s")
    check("the background sampler catches a transient that is gone again "
          "before the stage ends, which is the whole reason it exists: "
          "256 MB mapped, touched and unmapped inside the stage",
          row["footprint_peak_mb"] - row["footprint_after_mb"] > 100,
          f"peak {row['footprint_peak_mb']} vs after {row['footprint_after_mb']}")
    check("with no mx handed to it the MLX column reads 0, meaning 'made no "
          "MLX allocation' rather than 'used no memory'",
          row["mlx_peak_mb"] == 0.0, str(row["mlx_peak_mb"]))
    check("the table names the stage and has a header",
          "transient" in meter.table() and "mlx peak MB" in meter.table())

    refused = ""
    try:
        with meter.stage("outer"):
            with meter.stage("inner"):
                pass
    except RuntimeError as exc:
        refused = str(exc)
    check("nesting is refused out loud: resetting MLX's peak inside another "
          "stage would make both readings wrong rather than merely odd",
          "must be flat" in refused, refused or "no error raised")
    with meter.stage("after"):
        pass
    check("and the meter still works after refusing one",
          any(r["name"] == "after" for r in meter.rows()))
    meter.reset()
    check("reset clears the stages", meter.rows() == [] and meter.order == [])
    # Twice, with the outcome checked: another bare check("...", True) that
    # passed whatever stop() did (round 1 finding 39). The service stops the
    # meter from an exit path that can run after a normal stop.
    raised = ""
    try:
        meter.stop()
        meter.stop()
    except Exception as exc:                                   # noqa: BLE001
        raised = f"{type(exc).__name__}: {exc}"
    check("stopping a meter twice is safe, and the second stop leaves the "
          "sampler thread gone rather than restarting it",
          raised == "" and meter.rows() == []
          and (meter._thread is None or not meter._thread.is_alive()),
          raised or str(meter.rows()))


class _FakeMx:
    """Enough of mlx.core for the boundary wrap: it records what it was
    asked to evaluate, so the test proves the eval happens without needing
    MLX installed at all."""

    def __init__(self):
        self.evaluated = []

    def eval(self, *args):
        self.evaluated.extend(args)


def test_attention_chunk() -> None:
    print("\nthe chunked attention, which is where the 13.4 GB went")
    check("it is on by default, in blocks of 512 queries: at this model's "
          "head_dim MLX has no fused attention kernel and the fallback "
          "allocates the whole 5.2 GB matrix",
          ATTENTION_CHUNK == 512
          and MlxBackend(log=lambda _m: None).attention_chunk == 512)
    check("and it can be turned off, which is how the old behaviour is "
          "measured", MlxBackend(attention_chunk=0,
                                 log=lambda _m: None).attention_chunk == 0)
    check("a matrix under the threshold is left whole, so a path that was "
          "already using the fused kernel is untouched",
          ATTENTION_CHUNK_MIN_MB == 256)

    try:
        import mlx.core as mx
    except ImportError:
        mx = None
    if mx is None:
        # SKIPPED, not passed. These four used to be recorded as passes when
        # MLX was absent, including the bit-identical claim the REPORT rests on,
        # so an environment with no MLX produced the same green line as one that
        # had proved it (round 1 finding 18). They now print as skips and the
        # gate's summary counts them (round 1 finding 41).
        for label in ("the patch replaces mx.fast.scaled_dot_product_attention",
                      "a big attention really is chunked and a small one is not",
                      "the chunked result is bit identical to the whole one",
                      "and the patch can be taken back off"):
            skip(label, "mlx is not installed in this environment: install it "
                        "with `uv sync --project sam --extra mlx` and run this "
                        "suite again before believing the chunking claim")
        return

    backend = MlxBackend(log=lambda _m: None)
    applied = backend._apply_attention_chunk(mx)
    patched = mx.fast.scaled_dot_product_attention
    try:
        check("the patch replaces mx.fast.scaled_dot_product_attention",
              applied["enabled"] and getattr(patched, "_fixxr_chunked", False),
              str(applied))
        counter = patched._fixxr_counter
        original = patched._fixxr_original
        heads, dim = 8, 32
        big_q = mx.random.normal((1, heads, 2048, dim))
        keys = mx.random.normal((1, heads, 12288, dim))
        values = mx.random.normal((1, heads, 12288, dim))
        small_q = mx.random.normal((1, heads, 64, dim))
        mx.eval(big_q, keys, values, small_q)

        chunked_out = patched(big_q, keys, values, scale=dim ** -0.5)
        whole_out = original(big_q, keys, values, scale=dim ** -0.5)
        patched(small_q, keys, values, scale=dim ** -0.5)
        mx.eval(chunked_out, whole_out)
        check("a big attention really is chunked and a small one is not",
              counter["chunked"] == 1 and counter["whole"] == 1, str(counter))
        difference = float(mx.max(mx.abs(chunked_out - whole_out)))
        check("the chunked result is bit identical to the whole one, which is "
              "why this cannot change a mask: a softmax runs along the key "
              "axis, so splitting the query axis changes no row",
              difference == 0.0, f"max abs diff {difference:.3e}")
        del chunked_out, whole_out, big_q, keys, values, small_q
    finally:
        removed = MlxBackend._remove_attention_chunk()
        mx.clear_cache()
    check("and the patch can be taken back off",
          removed and mx.fast.scaled_dot_product_attention is not patched)


def test_layer_eval() -> None:
    print("\nthe ViT trunk evaluation boundary, measured and left off")
    check("it is OFF by default: on the real model it moved the window's peak "
          "by 9 MB and cost 23% of the time, because the trunk was never "
          "where the memory went",
          LAYER_EVAL is False
          and MlxBackend(log=lambda _m: None).layer_eval is False)
    check("and it can be turned on, so that can be re-measured rather than "
          "re-argued", MlxBackend(layer_eval=True, log=lambda _m: None).layer_eval
          is True)

    import types

    module = types.ModuleType("fixxr_fake_trunk")

    class FakeLayer:
        def __call__(self, value):
            return value * 2

    module.FakeLayer = FakeLayer
    sys.modules["fixxr_fake_trunk"] = module
    original_targets = mlx_backend.EVAL_BOUNDARY_CLASSES
    mlx_backend.EVAL_BOUNDARY_CLASSES = (("fixxr_fake_trunk", "FakeLayer"),)
    try:
        fake = _FakeMx()
        off = MlxBackend(log=lambda _m: None)
        report = off._apply_layer_eval(fake)
        FakeLayer()(3)
        check("with the boundary off nothing is wrapped and nothing is "
              "evaluated", report == {"enabled": False, "wrapped": [],
                                      "missing": []} and fake.evaluated == [],
              str(report))

        backend = MlxBackend(layer_eval=True, log=lambda _m: None)
        report = backend._apply_layer_eval(fake)
        check("with it on the trunk layer is wrapped and named in the report "
              "that reaches /health",
              report["enabled"] and report["wrapped"] == ["FakeLayer"]
              and report["missing"] == [], str(report))
        check("the layer still returns exactly what it returned before: an "
              "eval computes what the graph already says, so no mask can "
              "change", FakeLayer()(3) == 6)
        check("and the boundary really fired, once, on that layer's output",
              fake.evaluated == [6], str(fake.evaluated))

        backend._apply_layer_eval(fake)
        FakeLayer()(5)
        check("applying it twice does not double wrap: one eval per layer, "
              "not two", fake.evaluated == [6, 10], str(fake.evaluated))

        undone = backend._remove_layer_eval()
        FakeLayer()(7)
        check("removing it restores the original call and says what it undid",
              undone == ["FakeLayer"] and fake.evaluated == [6, 10],
              f"{undone} {fake.evaluated}")

        mlx_backend.EVAL_BOUNDARY_CLASSES = (("fixxr_fake_trunk", "Renamed"),)
        report = MlxBackend(layer_eval=True,
                            log=lambda _m: None)._apply_layer_eval(fake)
        check("a future mlx-cv that renames the class is a log line and a "
              "'missing' entry, not a service that will not load a model",
              report["missing"] == ["Renamed"] and report["wrapped"] == [],
              str(report))
    finally:
        mlx_backend.EVAL_BOUNDARY_CLASSES = original_targets
        sys.modules.pop("fixxr_fake_trunk", None)

    args = build_parser().parse_args(
        ["--stub", "--mlx-layer-eval", "--mlx-attention-chunk", "256"])
    check("--mlx-layer-eval parses to True", args.mlx_layer_eval is True)
    check("--mlx-attention-chunk is parsed", args.mlx_attention_chunk == 256)
    check("both default to None, so an unset flag leaves the backend's own "
          "default rather than forcing it either way",
          build_parser().parse_args([]).mlx_layer_eval is None
          and build_parser().parse_args([]).mlx_attention_chunk is None)


def main() -> int:
    test_readings()
    test_meter()
    test_stage_meter()
    test_stub_windows()
    test_mlx_limits_without_mlx()
    test_attention_chunk()
    test_layer_eval()
    test_torch_adapter_release()
    test_flags_and_health()
    return report("test_memory")


if __name__ == "__main__":
    raise SystemExit(main())
